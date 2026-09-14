"""JellyfinMarkerPublisher against an autospec'd JellyfinServer (Bridge plugin HTTP contract, spec §3.2, §6.3)."""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec

import pytest
import requests

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, ItemNotFoundError, PublishError
from media_preview_generator.markers.publishers.jellyfin import (
    TICKS_PER_MS,
    JellyfinMarkerPublisher,
    bridge_key,
    core_key,
    segment_times,
)
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.jellyfin import JellyfinServer

T = MarkerType
CFG = ServerConfig(id="jf-1", type=ServerType.JELLYFIN, name="JF", enabled=True, url="http://j", auth={})
MISSING = "/definitely/not/here.mkv"
READY_INFO = {"installed": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}
INTRO = {"type": "Intro", "startTicks": 1_267_710_000, "endTicks": 1_570_680_000}
OUTRO = {"type": "Outro", "startTicks": 12_950_000_000, "endTicks": 13_214_720_000}
INTRO_MARKER = Marker(T.INTRO, 126_771, 157_068, ("a",))
OUTRO_MARKER = Marker(T.CREDITS, 1_295_000, 1_321_472, ("a",))


def _server():
    server = create_autospec(JellyfinServer, instance=True)
    server.get_bridge_info.return_value = READY_INFO
    server.get_bridge_markers_access.return_value = "ok"
    server.get_media_segments.return_value = []
    server.get_bridge_marker_state.return_value = {"segments": [], "fileSize": None, "stale": False}
    return server


def _pub(server, enabled=True):
    return JellyfinMarkerPublisher(server, CFG, ServerMarkersSettings(enabled, None, None, "restore"))


def _resp(status, body=None, *, json_error=False):
    r = MagicMock(status_code=status)
    if json_error:
        r.json.side_effect = ValueError("no JSON")
    else:
        r.json.return_value = body
    return r


def _core(*segments, other_provider=()):
    """Core /MediaSegments rows for bridge-shaped segments (plus rows another provider serves)."""
    rows = [
        {"Id": f"id{i}", "ItemId": "abc", "Type": s["type"], "StartTicks": s["startTicks"], "EndTicks": s["endTicks"]}
        for i, s in enumerate([*segments, *other_provider])
    ]
    return rows


def _media(tmp_path, size=4321):
    path = tmp_path / "S01E01.mkv"
    path.write_bytes(b"\0" * size)
    return str(path)


def test_ticks_per_ms_is_jellyfin_tick_size():
    assert TICKS_PER_MS == 10_000


@pytest.mark.parametrize(
    ("enabled", "info", "access", "state"),
    [
        pytest.param(False, READY_INFO, "ok", Capability.DISABLED, id="disabled"),
        pytest.param(True, None, "ok", Capability.UNREACHABLE, id="unreachable"),
        pytest.param(
            True, {"installed": False, "version": None, "features": []}, "ok", Capability.NEEDS_PLUGIN, id="no-plugin"
        ),
        pytest.param(
            True,
            {"installed": True, "version": "10.11.0.3", "features": []},
            "ok",
            Capability.PLUGIN_OUTDATED,
            id="no-markers-feature",
        ),
        pytest.param(True, READY_INFO, "ok", Capability.READY, id="ready-10.11"),
        pytest.param(True, {**READY_INFO, "version": "12.0.1.0"}, "ok", Capability.READY, id="ready-12.0"),
        pytest.param(True, READY_INFO, "forbidden", Capability.MISCONFIGURED, id="not-admin"),
        pytest.param(True, READY_INFO, "unauthorized", Capability.MISCONFIGURED, id="credentials-rejected"),
        pytest.param(True, READY_INFO, None, Capability.UNREACHABLE, id="access-unknown"),
        pytest.param(True, READY_INFO, "teapot", Capability.UNREACHABLE, id="access-unexpected-value"),
    ],
)
def test_capability_matrix(enabled, info, access, state):
    server = _server()
    server.get_bridge_info.return_value = info
    server.get_bridge_markers_access.return_value = access
    report = _pub(server, enabled).capability()
    assert report.state is state
    if state is Capability.READY:
        assert report.details == {
            "plugin_version": info["version"],
            "can_show": ["intro", "credits", "recap", "preview"],
        }
    if state is Capability.DISABLED:
        # Turned off: the server isn't contacted at all.
        server.get_bridge_info.assert_not_called()
    if state in (Capability.UNREACHABLE, Capability.NEEDS_PLUGIN, Capability.PLUGIN_OUTDATED) and access == "ok":
        server.get_bridge_markers_access.assert_not_called()


def test_capability_outdated_messages():
    server = _server()
    server.get_bridge_info.return_value = {"installed": True, "version": "10.11.0.3", "features": []}
    report = _pub(server).capability()
    assert report.details == {"plugin_version": "10.11.0.3"}
    assert "10.11.0.3" in report.message

    server.get_bridge_info.return_value = {"installed": True, "version": None, "features": ["trickplay"]}
    assert "unknown" in _pub(server).capability().message


@pytest.mark.parametrize(
    ("access", "message_part", "not_in_message"),
    [
        pytest.param("forbidden", "administrator rights", "reconnect", id="403"),
        pytest.param("unauthorized", "rejected this server's credentials; reconnect it", "administrator", id="401"),
    ],
)
def test_capability_access_messages(access, message_part, not_in_message):
    server = _server()
    server.get_bridge_markers_access.return_value = access
    report = _pub(server).capability()
    assert message_part in report.message
    assert not_in_message not in report.message


def test_publisher_identity():
    assert JellyfinMarkerPublisher.name == "jellyfin_bridge"
    assert JellyfinMarkerPublisher.supported_types == frozenset(MarkerType)
    # The plugin may have stored markers before an error, so a failed write leaves the server state unknown.
    assert JellyfinMarkerPublisher.atomic_writes is False


def test_write_posts_all_types_in_ticks_with_file_size(tmp_path):
    server = _server()
    server.put_bridge_markers.return_value = _resp(200, {"stored": 4})
    sent = [
        {"type": "Recap", "startTicks": 0, "endTicks": 200_000_000},
        INTRO,
        OUTRO,
        {"type": "Preview", "startTicks": 13_100_000_000, "endTicks": 13_200_000_000},
    ]
    server.get_media_segments.return_value = _core(*sent)
    path = _media(tmp_path, size=4321)
    markers = [
        OUTRO_MARKER,
        INTRO_MARKER,
        Marker(T.PREVIEW, 1_310_000, 1_320_000, ("a",)),
        Marker(T.RECAP, 0, 20_000, ("a",)),
    ]
    ours = _pub(server).write("abc", markers, previous=[], duration_ms=1_321_472, canonical_path=path)
    assert ours == [
        Marker(T.RECAP, 0, 20_000, ("a",)),
        INTRO_MARKER,
        OUTRO_MARKER,
        Marker(T.PREVIEW, 1_310_000, 1_320_000, ("a",)),
    ]
    server.put_bridge_markers.assert_called_once_with("abc", sent, file_size=4321)
    server.get_media_segments.assert_called_once_with("abc")
    server.get_bridge_marker_state.assert_not_called()
    server.delete_bridge_markers.assert_not_called()


def test_write_omits_file_size_when_file_cannot_be_statted():
    server = _server()
    server.put_bridge_markers.return_value = _resp(200, {"stored": 1})
    server.get_media_segments.return_value = _core(INTRO)
    ours = _pub(server).write("abc", [INTRO_MARKER], previous=[], duration_ms=None, canonical_path=MISSING)
    assert ours == [INTRO_MARKER]
    server.put_bridge_markers.assert_called_once_with("abc", [INTRO], file_size=None)


def test_write_accepts_other_providers_segments_next_to_ours():
    server = _server()
    server.put_bridge_markers.return_value = _resp(200, {"stored": 1})
    server.get_media_segments.return_value = _core(
        INTRO, other_provider=[{"type": "Intro", "startTicks": 1, "endTicks": 900_000_000}]
    )
    _pub(server).write("abc", [INTRO_MARKER], previous=[], duration_ms=1_321_472, canonical_path=MISSING)
    server.get_media_segments.assert_called_once_with("abc")


def test_write_replaces_even_when_previous_had_other_types(tmp_path):
    # One POST replaces everything the plugin stored, so a type dropped since last time goes with it.
    server = _server()
    server.put_bridge_markers.return_value = _resp(200, {"stored": 1})
    server.get_media_segments.return_value = _core(INTRO)
    ours = _pub(server).write(
        "abc", [INTRO_MARKER], previous=[OUTRO_MARKER], duration_ms=1_321_472, canonical_path=_media(tmp_path)
    )
    assert ours == [INTRO_MARKER]
    assert server.put_bridge_markers.call_args.args[1] == [INTRO]
    server.delete_bridge_markers.assert_not_called()


STALE = {"segments": [INTRO, OUTRO], "fileSize": 5, "stale": True}
NOT_STALE = {"segments": [INTRO, OUTRO], "fileSize": 5, "stale": False}


# Only "couldn't read /MediaSegments" marks the server; the rest are per item (a provider is switched off per library).
@pytest.mark.parametrize(
    ("core", "bridge_state", "state", "message_part", "reads_state"),
    [
        pytest.param(None, NOT_STALE, Capability.UNREACHABLE, "couldn't confirm", False, id="core-read-fails"),
        pytest.param([], STALE, None, "different file size", True, id="none-served-stale"),
        pytest.param([], NOT_STALE, None, "media segment provider", True, id="none-served-provider-off"),
        pytest.param(
            _core(other_provider=[{"type": "Intro", "startTicks": 1, "endTicks": 900_000_000}]),
            NOT_STALE,
            None,
            "media segment provider",
            True,
            id="only-other-providers-served",
        ),
        pytest.param(
            [], None, None, "stored 2 markers but serves only 0; check the Jellyfin log", True, id="state-unknown"
        ),
        pytest.param(
            _core(INTRO), STALE, None, "stored 2 markers but serves only 1; check the Jellyfin log", False, id="partial"
        ),
        pytest.param(
            _core({**INTRO, "endTicks": INTRO["endTicks"] + 1}, OUTRO),
            NOT_STALE,
            None,
            "stored 2 markers but serves only 1",
            False,
            id="served-times-differ",
        ),
        pytest.param(
            _core({**INTRO, "type": "Outro"}, OUTRO),
            NOT_STALE,
            None,
            "stored 2 markers but serves only 1",
            False,
            id="served-type-differs",
        ),
    ],
)
def test_write_verifies_jellyfin_serves_what_was_stored(core, bridge_state, state, message_part, reads_state):
    server = _server()
    server.put_bridge_markers.return_value = _resp(200, {"stored": 2})
    server.get_media_segments.return_value = core
    # Like the real plugin: once the cleanup DELETE ran, the stored state (and its stale flag) is gone.
    server.get_bridge_marker_state.side_effect = lambda _item: (
        {"segments": [], "fileSize": None, "stale": False} if server.delete_bridge_markers.called else bridge_state
    )
    server.delete_bridge_markers.return_value = _resp(204)
    with pytest.raises(PublishError) as ei:
        _pub(server).write(
            "abc", [INTRO_MARKER, OUTRO_MARKER], previous=[], duration_ms=1_321_472, canonical_path=MISSING
        )
    assert type(ei.value) is PublishError
    assert ei.value.state is state
    assert message_part in str(ei.value)
    server.get_media_segments.assert_called_once_with("abc")
    if reads_state:
        server.get_bridge_marker_state.assert_called_once_with("abc")
    else:
        server.get_bridge_marker_state.assert_not_called()
    # Nothing unconfirmed stays in the plugin store for a later scan to serve.
    server.delete_bridge_markers.assert_called_once_with("abc")


@pytest.mark.parametrize(
    "delete_outcome",
    [
        pytest.param({"side_effect": requests.ConnectionError("refused")}, id="delete-transport-error"),
        pytest.param({"return_value": _resp(500, None, json_error=True)}, id="delete-http-500"),
    ],
)
def test_failed_cleanup_still_reports_the_verification_failure(delete_outcome):
    server = _server()
    server.put_bridge_markers.return_value = _resp(200, {"stored": 1})
    server.get_media_segments.return_value = []
    server.get_bridge_marker_state.return_value = NOT_STALE
    for attr, value in delete_outcome.items():
        setattr(server.delete_bridge_markers, attr, value)
    with pytest.raises(PublishError) as ei:
        _pub(server).write("abc", [INTRO_MARKER], previous=[], duration_ms=1_321_472, canonical_path=MISSING)
    assert "media segment provider" in str(ei.value)
    assert ei.value.state is None
    server.delete_bridge_markers.assert_called_once_with("abc")


def test_write_empty_with_previous_deletes_without_verifying():
    server = _server()
    server.delete_bridge_markers.return_value = _resp(204)
    assert _pub(server).write("abc", [], previous=[INTRO_MARKER], duration_ms=1, canonical_path=MISSING) == []
    server.delete_bridge_markers.assert_called_once_with("abc")
    server.put_bridge_markers.assert_not_called()
    server.get_media_segments.assert_not_called()


def test_write_empty_with_unknown_previous_deletes():
    # previous=None means "don't know what we published here", so clear whatever the plugin may hold.
    server = _server()
    server.delete_bridge_markers.return_value = _resp(204)
    assert _pub(server).write("abc", [], previous=None, duration_ms=None, canonical_path=MISSING) == []
    server.delete_bridge_markers.assert_called_once_with("abc")
    server.put_bridge_markers.assert_not_called()


def test_write_empty_without_previous_does_nothing():
    server = _server()
    assert _pub(server).write("abc", [], previous=[], duration_ms=1, canonical_path=MISSING) == []
    server.put_bridge_markers.assert_not_called()
    server.delete_bridge_markers.assert_not_called()
    server.get_media_segments.assert_not_called()


@pytest.mark.parametrize(
    ("resp", "exc", "state", "message_part"),
    [
        pytest.param(_resp(404, {"error": "item not found"}), ItemNotFoundError, None, "abc", id="404-item"),
        pytest.param(
            _resp(404, None, json_error=True), PublishError, Capability.NEEDS_PLUGIN, "update", id="404-route"
        ),
        pytest.param(
            _resp(404, {"title": "Not Found"}), PublishError, Capability.NEEDS_PLUGIN, "update", id="404-other"
        ),
        # Only the plugin's exact "item not found" means "retry once Jellyfin knows the item".
        pytest.param(
            _resp(404, {"error": "no such thing"}),
            PublishError,
            Capability.NEEDS_PLUGIN,
            "update",
            id="404-other-error",
        ),
        pytest.param(
            _resp(400, {"error": "unsupported segment type 'X'"}), PublishError, None, "unsupported segment", id="400"
        ),
        pytest.param(_resp(500, None, json_error=True), PublishError, None, "HTTP 500", id="500"),
        pytest.param(_resp(501, None, json_error=True), PublishError, None, "HTTP 501", id="501"),
        pytest.param(_resp(502, None, json_error=True), PublishError, Capability.UNREACHABLE, "unavailable", id="502"),
        pytest.param(_resp(503, None, json_error=True), PublishError, Capability.UNREACHABLE, "unavailable", id="503"),
        pytest.param(_resp(504, None, json_error=True), PublishError, Capability.UNREACHABLE, "unavailable", id="504"),
        pytest.param(_resp(403, None, json_error=True), PublishError, None, "administrator rights", id="403"),
        pytest.param(
            _resp(401, None, json_error=True), PublishError, None, "rejected this server's credentials", id="401"
        ),
    ],
)
def test_write_errors(resp, exc, state, message_part):
    server = _server()
    server.put_bridge_markers.return_value = resp
    with pytest.raises(exc) as ei:
        _pub(server).write("abc", [INTRO_MARKER], previous=[], duration_ms=1, canonical_path=MISSING)
    assert type(ei.value) is exc
    assert ei.value.state is state
    assert message_part in str(ei.value)
    server.get_media_segments.assert_not_called()


@pytest.mark.parametrize(
    ("resp", "exc", "state"),
    [
        pytest.param(_resp(404, {"error": "item not found"}), ItemNotFoundError, None, id="404-item"),
        pytest.param(_resp(503, None, json_error=True), PublishError, Capability.UNREACHABLE, id="503"),
    ],
)
def test_delete_errors_are_mapped_the_same_way(resp, exc, state):
    server = _server()
    server.delete_bridge_markers.return_value = resp
    with pytest.raises(exc) as ei:
        _pub(server).write("abc", [], previous=[INTRO_MARKER], duration_ms=1, canonical_path=MISSING)
    assert ei.value.state is state


@pytest.mark.parametrize("call", ["put_bridge_markers", "delete_bridge_markers"])
def test_transport_failure_is_unreachable(call):
    server = _server()
    getattr(server, call).side_effect = requests.ConnectionError("refused")
    markers = [INTRO_MARKER] if call == "put_bridge_markers" else []
    previous = [] if markers else [INTRO_MARKER]
    with pytest.raises(PublishError) as ei:
        _pub(server).write("abc", markers, previous=previous, duration_ms=1, canonical_path=MISSING)
    assert ei.value.state is Capability.UNREACHABLE


def test_read_returns_only_stored_segments_jellyfin_serves():
    server = _server()
    recap = {"type": "Recap", "startTicks": 0, "endTicks": 200_000_000}
    preview = {"type": "Preview", "startTicks": 13_100_000_000, "endTicks": 13_200_000_000}
    server.get_bridge_markers.return_value = [
        OUTRO,
        INTRO,
        recap,
        preview,
        {"type": "Commercial", "startTicks": 1, "endTicks": 2},
        {"type": "Intro", "startTicks": "x", "endTicks": 2},
    ]
    # Recap stored but not served (e.g. stale); Preview served with different times by another provider.
    server.get_media_segments.return_value = _core(
        OUTRO,
        INTRO,
        {"type": "Commercial", "startTicks": 1, "endTicks": 2},
        other_provider=[{**preview, "startTicks": 13_000_000_000}],
    )
    assert _pub(server).read("abc") == [
        Marker(T.INTRO, 126_771, 157_068, ("jellyfin",)),
        Marker(T.CREDITS, 1_295_000, 1_321_472, ("jellyfin",)),
    ]
    server.get_bridge_markers.assert_called_once_with("abc")
    server.get_media_segments.assert_called_once_with("abc")


def test_read_all_four_types_when_served():
    server = _server()
    recap = {"type": "Recap", "startTicks": 0, "endTicks": 200_000_000}
    preview = {"type": "Preview", "startTicks": 13_100_000_000, "endTicks": 13_200_000_000}
    server.get_bridge_markers.return_value = [preview, OUTRO, recap, INTRO]
    server.get_media_segments.return_value = _core(INTRO, OUTRO, recap, preview)
    assert _pub(server).read("abc") == [
        Marker(T.RECAP, 0, 20_000, ("jellyfin",)),
        Marker(T.INTRO, 126_771, 157_068, ("jellyfin",)),
        Marker(T.CREDITS, 1_295_000, 1_321_472, ("jellyfin",)),
        Marker(T.PREVIEW, 1_310_000, 1_320_000, ("jellyfin",)),
    ]


def test_read_empty_store_is_empty_without_reading_segments():
    server = _server()
    server.get_bridge_markers.return_value = []
    assert _pub(server).read("abc") == []
    server.get_media_segments.assert_not_called()


@pytest.mark.parametrize(
    ("stored", "core"),
    [pytest.param(None, [], id="store-unreadable"), pytest.param([INTRO], None, id="segments-unreadable")],
)
def test_read_failure_raises_instead_of_claiming_no_markers(stored, core):
    server = _server()
    server.get_bridge_markers.return_value = stored
    server.get_media_segments.return_value = core
    with pytest.raises(PublishError):
        _pub(server).read("abc")


def test_segment_keys():
    assert bridge_key(INTRO) == ("Intro", 1_267_710_000, 1_570_680_000)
    assert core_key(_core(INTRO)[0]) == ("Intro", 1_267_710_000, 1_570_680_000)
    assert bridge_key({}) == (None, None, None) == core_key({})


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(("Intro", 1_267_710_000, 1_570_680_000), (T.INTRO, 126_771, 157_068), id="intro"),
        pytest.param(("Outro", 10_000, 29_999), (T.CREDITS, 1, 2), id="outro-floors"),
        pytest.param(("Recap", 0, 10_000), (T.RECAP, 0, 1), id="recap"),
        pytest.param(("Preview", 20_000, 30_000), (T.PREVIEW, 2, 3), id="preview"),
        pytest.param(("Commercial", 1, 2), None, id="commercial"),
        pytest.param(("intro", 1, 2), None, id="wrong-case"),
        pytest.param((None, 1, 2), None, id="no-type"),
        pytest.param(("Intro", "1", 2), None, id="string-start"),
        pytest.param(("Intro", 1, 2.0), None, id="float-end"),
        pytest.param(("Intro", True, 2), None, id="bool-start"),
        pytest.param(("Intro", 1, None), None, id="missing-end"),
        pytest.param((["Intro"], 1, 2), None, id="unhashable-type"),
    ],
)
def test_segment_times(args, expected):
    assert segment_times(*args) == expected


@pytest.mark.parametrize(
    ("markers", "previous"),
    [
        pytest.param([INTRO_MARKER], [], id="post"),
        pytest.param([], [INTRO_MARKER], id="delete"),
        pytest.param([], [], id="nothing-to-do"),
    ],
)
def test_own_previous_changes_nothing(markers, previous):
    # Jellyfin item ids are per version, so there's no other file's markers on the item to clean up.
    calls = []
    for own_previous in (None, [OUTRO_MARKER]):
        server = _server()
        server.put_bridge_markers.return_value = _resp(200, {"stored": 1})
        server.delete_bridge_markers.return_value = _resp(204)
        server.get_media_segments.return_value = _core(INTRO)
        kwargs = {} if own_previous is None else {"own_previous": own_previous}
        ours = _pub(server).write(
            "abc", markers, previous=previous, duration_ms=1_321_472, canonical_path=MISSING, **kwargs
        )
        calls.append((ours, server.mock_calls))
    assert calls[0] == calls[1]
