"""Native Plex loudness at the real Inspector route boundary."""

# ruff: noqa: F811 - imported pytest fixtures are used as test parameters.

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest

from media_preview_generator.inspector import previews
from media_preview_generator.servers.plex import PlexServer
from media_preview_generator.web.settings_manager import get_settings_manager
from tests.test_api_inspector import (  # noqa: F401 - shared route fixtures
    _emby_entry,
    _reset_singletons,
    app,
    authed_client,
    client,
    film,
    media,
)


@pytest.fixture
def native(app, media, film, monkeypatch):
    from media_preview_generator.web.routes import api_inspector

    monkeypatch.setattr(api_inspector, "_get_plex_config_folder", lambda: "")
    remote = "/plex/movies/" + os.path.relpath(film, media / "movies")
    cfg = {
        "id": "plex-loudness",
        "type": "plex",
        "name": "Plex native",
        "enabled": True,
        "url": "http://127.0.0.1:9",
        "auth": {"method": "token", "token": "private-plex-token"},
        "libraries": [{"id": "7", "name": "Movies", "remote_paths": ["/plex/movies"]}],
        "path_mappings": [{"remote_prefix": "/plex/movies", "local_prefix": str(media / "movies")}],
        "loudness": {"enabled": False},
        "output": {},
    }
    get_settings_manager().set("plex_config_folder", "")
    get_settings_manager().set("media_servers", [cfg, _emby_entry(media)])
    data = ET.Element("MediaContainer")
    item = ET.SubElement(data, "Video", ratingKey="42")
    media_node = ET.SubElement(item, "Media", id="55")
    part = ET.SubElement(media_node, "Part", id="66", file=remote, size=str(os.path.getsize(film)))
    audio = ET.SubElement(
        part,
        "Stream",
        {
            "id": "77",
            "index": "1",
            "streamType": "2",
            "codec": "eac3",
            "language": "English",
            "channels": "6",
            "default": "1",
            "displayTitle": "English (EAC3 5.1)",
            "loudness": "-23.1",
            "peak": "-2.5",
            "lra": "8.2",
            "threshold": "-33.6",
            "gainOffset": "0.4",
            "loudnessAnalysisVersion": "0.02",
            "canNormalizeLoudness": "1",
        },
    )
    ET.SubElement(part, "Stream", id="78", index="0", streamType="1", loudness="-99")
    resolver = Mock(return_value="/library/metadata/42")
    monkeypatch.setattr(PlexServer, "resolve_remote_path_to_item_id", resolver)
    query = Mock(return_value=data)
    monkeypatch.setattr(PlexServer, "_connect", lambda self: SimpleNamespace(query=query))
    previews._resting_since.clear()
    return SimpleNamespace(cfg=cfg, data=data, item=item, part=part, audio=audio, query=query, resolver=resolver)


def _read(client, film):
    response = client.get("/api/inspector/file", query_string={"path": film})
    assert response.status_code == 200
    # Disallow Python's nonstandard Infinity/NaN JSON acceptance.
    payload = json.loads(response.data, parse_constant=lambda value: pytest.fail(f"Invalid JSON number: {value}"))
    return payload["loudness"]


def test_native_values_are_read_with_optin_off_and_no_database(authed_client, film, native, monkeypatch):
    from media_preview_generator.loudness import guard, job

    monkeypatch.setattr(guard, "create_loudness_db", lambda *args: pytest.fail("Inspector opened Plex database"))
    monkeypatch.setattr(job, "create_loudness_job", lambda *args, **kwargs: pytest.fail("Inspector started analysis"))
    rows = _read(authed_client, film)
    plex, emby = rows
    assert plex["state"] == "available"
    assert plex["enabled"] is False
    assert plex["source"] == "plex_api"
    assert plex["source_note"] == "Reported by Plex"
    assert emby["state"] == "unsupported"
    stream = plex["streams"][0]
    assert len(plex["streams"]) == 1
    assert (stream["item_id"], stream["part_id"], stream["stream_id"], stream["index"]) == ("42", "66", "77", 1)
    assert [
        stream[key] for key in ("integrated_lufs", "true_peak_dbtp", "lra_lu", "threshold_lufs", "gain_offset_db")
    ] == [-23.1, -2.5, 8.2, -33.6, 0.4]
    assert stream["normalization_available"] is True
    assert stream["analysis_version"] == "0.02"
    native.resolver.assert_called_once_with(film, library_ids=["7"])
    native.query.assert_called_once_with("/library/metadata/42")


def test_exact_version_and_each_audio_stream(authed_client, film, native):
    another = ET.SubElement(native.item, "Media", id="56")
    wrong_part = ET.SubElement(another, "Part", id="67", file="/other/Film.mkv")
    ET.SubElement(wrong_part, "Stream", dict(native.audio.attrib, id="88", loudness="-99"))
    ET.SubElement(native.part, "Stream", id="79", index="2", streamType="2", codec="aac", channels="2")
    row = _read(authed_client, film)[0]
    assert row["state"] == "partial"
    assert [(s["stream_id"], s["state"]) for s in row["streams"]] == [("77", "available"), ("79", "not_analysed")]
    assert row["streams"][1]["integrated_lufs"] is None
    assert row["streams"][1]["normalization_available"] is None


@pytest.mark.parametrize(
    ("change", "state"),
    [
        ({"loudnessAnalysisVersion": "9"}, "unsupported"),
        ({"peak": "NaN"}, "partial"),
        ({"loudness": "Infinity"}, "partial"),
        ({"canNormalizeLoudness": "0"}, "partial"),
        ({"canNormalizeLoudness": "weird"}, "partial"),
        ({"loudness": "-inf", "peak": "-inf", "gainOffset": "inf"}, "available"),
    ],
)
def test_native_values_states_and_json_safety(authed_client, film, native, change, state):
    native.audio.attrib.update(change)
    row = _read(authed_client, film)[0]
    stream = row["streams"][0]
    assert row["state"] == stream["state"] == state
    if change.get("peak") == "NaN":
        assert stream["true_peak_dbtp"] is None
    if change.get("loudness") == "Infinity":
        assert stream["integrated_lufs"] is None
    if change.get("loudness") == "-inf":
        assert (stream["integrated_lufs"], stream["true_peak_dbtp"], stream["gain_offset_db"]) == (
            "-inf",
            "-inf",
            "inf",
        )
        assert "Silent or very short" in stream["note"]


@pytest.mark.parametrize(
    "problem",
    [
        "wrong_item",
        "wrong_part",
        "wrong_size",
        "missing_id",
        "duplicate",
        "changed_file",
        "changed_same_size",
        "offline",
    ],
)
def test_mismatched_or_unavailable_metadata_never_verified(authed_client, film, native, problem):
    if problem == "wrong_item":
        native.item.set("ratingKey", "999")
    elif problem == "wrong_part":
        native.part.set("file", "/other/" + os.path.basename(film))
    elif problem == "wrong_size":
        native.part.set("size", "999999")
    elif problem == "missing_id":
        del native.audio.attrib["id"]
    elif problem == "duplicate":
        ET.SubElement(native.part, "Stream", dict(native.audio.attrib))
    elif problem == "changed_file":

        def change(_url):
            with open(film, "ab") as output:
                output.write(b"changed")
            return native.data

        native.query.side_effect = change
    elif problem == "changed_same_size":

        def change(_url):
            before = os.stat(film)
            with open(film, "r+b") as output:
                output.write(b"X")
            os.utime(film, ns=(before.st_atime_ns, before.st_mtime_ns))
            return native.data

        native.query.side_effect = change
    elif problem == "offline":
        native.query.side_effect = RuntimeError("private-plex-token")
    row = _read(authed_client, film)[0]
    assert row["state"] == "unavailable"
    assert row["streams"] == []
    assert "private-plex-token" not in json.dumps(row)


def test_unauthenticated_or_outside_library_never_queries_plex(client, authed_client, film, media, native):
    with client.session_transaction() as session:
        session.clear()
    assert client.get("/api/inspector/file", query_string={"path": film}).status_code == 401
    with client.session_transaction() as session:
        session["authenticated"] = True
    assert client.get("/api/inspector/file", query_string={"path": "/etc/passwd"}).status_code == 400
    assert _read(client, str(media / "other" / "loose.mkv")) == []
    native.query.assert_not_called()


def test_existing_preview_item_lookup_is_reused(authed_client, film, native, monkeypatch, tmp_path):
    from media_preview_generator.web.routes import api_inspector

    monkeypatch.setattr(api_inspector, "_get_plex_config_folder", lambda: str(tmp_path / "plex"))
    row = _read(authed_client, film)[0]
    assert row["state"] == "available"
    native.resolver.assert_called_once_with(film, library_ids=["7"])
    assert [call.args for call in native.query.call_args_list] == [
        ("/library/metadata/42/tree",),
        ("/library/metadata/42",),
    ]


def test_one_plex_failure_keeps_other_plex_measurements(authed_client, film, native, monkeypatch):
    broken = dict(native.cfg, id="plex-offline", name="Plex offline")
    get_settings_manager().set("media_servers", [broken, native.cfg])

    def connect(server):
        if server.id == "plex-offline":
            raise RuntimeError("private-plex-token")
        return SimpleNamespace(query=native.query)

    monkeypatch.setattr(PlexServer, "_connect", connect)
    rows = _read(authed_client, film)
    assert [(row["server_id"], row["state"]) for row in rows] == [
        ("plex-offline", "unavailable"),
        ("plex-loudness", "available"),
    ]
    assert rows[1]["streams"][0]["integrated_lufs"] == -23.1


def test_no_fields_is_not_analysed_with_normalization_false(authed_client, film, native):
    for key in ("loudness", "peak", "lra", "threshold", "gainOffset", "loudnessAnalysisVersion"):
        del native.audio.attrib[key]
    native.audio.set("canNormalizeLoudness", "0")
    row = _read(authed_client, film)[0]
    assert row["state"] == row["streams"][0]["state"] == "not_analysed"
    assert row["streams"][0]["normalization_available"] is False


def test_slow_server_uses_shared_deadline_and_rests(monkeypatch):
    from concurrent.futures import TimeoutError

    from media_preview_generator.inspector.loudness import file_loudness
    from media_preview_generator.servers.base import ServerConfig, ServerType

    cfg = ServerConfig(
        id="plex-slow-loudness", name="Slow Plex", type=ServerType.PLEX, enabled=True, url="http://plex", auth={}
    )
    future = Mock()
    future.result.side_effect = TimeoutError()
    submit = Mock(return_value=future)
    monkeypatch.setattr(previews._LOOKUPS, "submit", submit)
    previews._resting_since.clear()
    try:
        row = file_loudness("/media/movie.mkv", [(cfg, object(), [])], [], timeout_s=0)[0]
        assert row["state"] == "unavailable"
        assert "too long" in row["note"]
        assert future.result.call_args.kwargs == {"timeout": 0.0}
        assert file_loudness("/media/movie.mkv", [(cfg, object(), [])], [])[0]["state"] == "unavailable"
        assert submit.call_count == 1
    finally:
        previews._resting_since.clear()


@pytest.mark.parametrize(("selection", "enabled"), [([], False), (["7"], True)])
def test_automatic_eligibility_is_per_library_independent_of_preview_selection(
    authed_client, film, native, selection, enabled
):
    native.cfg["libraries"][0].update(enabled=False, kind="movie")
    native.cfg["loudness"] = {"enabled": True, "library_ids": selection}
    get_settings_manager().set("media_servers", [native.cfg])
    row = _read(authed_client, film)[0]
    assert row["state"] == "available"
    assert row["enabled"] is enabled
    assert row["streams"][0]["integrated_lufs"] == -23.1


def test_preview_metadata_failure_does_not_hide_native_loudness(authed_client, film, native, monkeypatch, tmp_path):
    from media_preview_generator.web.routes import api_inspector

    monkeypatch.setattr(api_inspector, "_get_plex_config_folder", lambda: str(tmp_path / "plex"))

    def unavailable(*args):
        raise PermissionError("Preview tree unavailable")

    monkeypatch.setattr(PlexServer, "get_bundle_metadata", unavailable)
    row = _read(authed_client, film)[0]
    assert row["state"] == "available"
    assert row["streams"][0]["integrated_lufs"] == -23.1
    native.query.assert_called_once_with("/library/metadata/42")


def test_unreadable_local_file_is_not_reported_as_a_plex_failure(tmp_path):
    from media_preview_generator.inspector.loudness import _server_loudness
    from media_preview_generator.servers.base import ServerConfig, ServerType

    cfg = ServerConfig(id="plex-gone", name="Plex", type=ServerType.PLEX, enabled=True, url="http://plex", auth={})
    row = _server_loudness(cfg, object(), str(tmp_path / "missing.mkv"), [], "1")
    assert row["state"] == "unavailable"
    assert "disk" in row["note"]
