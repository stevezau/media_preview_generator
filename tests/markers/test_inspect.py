"""Payload builders for the server Edit tab (status) and the Inspector tab (item), without Flask."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers import inspect
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.publishers.base import Capability, CapabilityReport
from media_preview_generator.markers.publishers.jellyfin import MARKERS_FEATURE
from media_preview_generator.markers.publishers.plex_db import SAME_HOST_PATH_ADVICE
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.fakes import FakeRegistry, server_config

T = MarkerType
CONFIRMED = "2026-09-13T00:00:00+00:00"
FETCHED = "2026-09-14T00:00:00+00:00"
DURATION = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters", "theintrodb"))
CREDITS = Marker(T.CREDITS, 1_290_000, DURATION, ("chapters",))
RECAP = Marker(T.RECAP, 0, 9_000, ("chapters",))


def _plex_markers(*, enabled, confirmed=CONFIRMED, library_ids=None, redetect="restore"):
    return {
        "enabled": enabled,
        "library_ids": library_ids,
        "plex": {"db_write_confirmed_at": confirmed, "on_plex_redetect": redetect},
    }


class _PublisherFactory:
    """Stands in for ``publisher_for``: records every call, answers a report per server type."""

    def __init__(self, reports=None, *, raises=None):
        self.calls = []
        self.reports = reports or {}
        self.raises = raises

    def __call__(self, server, config, **kwargs):
        self.calls.append({"server": server, "config": config, **kwargs})
        if config.type is ServerType.EMBY:
            return None
        pub = MagicMock(name=f"publisher-{config.id}")
        if self.raises is not None:
            pub.capability.side_effect = self.raises
        else:
            pub.capability.return_value = self.reports.get(config.type, CapabilityReport(Capability.READY, "ok"))
        return pub


@pytest.fixture
def factory(monkeypatch):
    fake = _PublisherFactory()
    monkeypatch.setattr(inspect, "publisher_for", fake)
    return fake


# --------------------------------------------------------------------------- server_status_payload


def test_plex_disabled_but_configured_is_checked_as_if_enabled(factory):
    factory.reports[ServerType.PLEX] = CapabilityReport(
        Capability.READY,
        "Written into this Plex server's database",
        {"db_path": "/plex/db", "plex_pass": True, "plex_version": "1.43.0", "detection": {"intro": "1"}},
    )
    libraries = [
        Library("1", "TV Shows", ("/media/tv",), kind="show"),
        Library("2", "Sports", ("/media/sports",), kind="show"),
        Library("3", "Movies", ("/media/movies",), enabled=False, kind="movie"),
    ]
    cfg = server_config(
        "plex",
        ServerType.PLEX,
        markers=_plex_markers(enabled=False, confirmed=None, redetect="keep_plex"),
        libraries=libraries,
    )
    server = MagicMock(name="plex-client")

    payload = inspect.server_status_payload(server, cfg)

    [call] = factory.calls
    assert call["server"] is server and call["config"] is cfg
    preview = call["settings"]
    assert preview.enabled is True
    assert preview.db_write_confirmed_at == "preview"
    assert preview.library_ids is None
    assert preview.on_plex_redetect == "keep_plex"
    assert payload == {
        "server_id": "plex",
        "server_type": "plex",
        "enabled": False,
        "settings": {
            "enabled": False,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": None, "on_plex_redetect": "keep_plex"},
        },
        "capability": {
            "state": "ready",
            "message": "Written into this Plex server's database",
            "details": {
                "db_path": "/plex/db",
                "plex_pass": True,
                "plex_version": "1.43.0",
                "detection": {"intro": "1"},
            },
            "warning": "",
        },
        "can_show": ["intro", "credits"],
        "libraries": [
            {"id": "1", "name": "TV Shows", "kind": "show", "default_selected": True},
            {"id": "2", "name": "Sports", "kind": "show", "default_selected": False},
            {"id": "3", "name": "Movies", "kind": "movie", "default_selected": True},
        ],
    }
    server.get_server_status.assert_not_called()  # the report already carries the version


def test_plex_enabled_keeps_its_stored_confirmation_and_library_choice(factory):
    cfg = server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=True, library_ids=["1", "2"]))
    payload = inspect.server_status_payload(MagicMock(), cfg)
    preview = factory.calls[0]["settings"]
    assert (preview.enabled, preview.db_write_confirmed_at, preview.library_ids) == (True, CONFIRMED, ("1", "2"))
    assert payload["enabled"] is True
    assert payload["settings"] == {
        "enabled": True,
        "library_ids": ["1", "2"],
        "plex": {"db_write_confirmed_at": CONFIRMED, "on_plex_redetect": "restore"},
    }


def test_invalid_stored_block_shows_the_disabled_defaults_the_app_uses(factory):
    # enabled without the DB-write confirmation fails validation; the app treats the server as off.
    cfg = server_config("plex", ServerType.PLEX, markers={"enabled": True, "library_ids": None})
    payload = inspect.server_status_payload(MagicMock(), cfg)
    assert payload["enabled"] is False
    assert payload["settings"] == {
        "enabled": False,
        "library_ids": None,
        "plex": {"db_write_confirmed_at": None, "on_plex_redetect": "restore"},
    }
    assert factory.calls[0]["settings"].enabled is True


_LOCK_DOMAIN = {
    "db_path": "/plex/db",
    "fs_type": "ext4",
    "lock_holder": False,
    "plex_pass": True,
    "plex_version": "1.43.0",
}


@pytest.mark.parametrize(
    ("report", "expect_hint"),
    [
        (
            CapabilityReport(
                Capability.NEEDS_LOCAL_DB, "Plex is running, but not with the database file", _LOCK_DOMAIN
            ),
            True,
        ),
        (
            # The Plex publisher's own lock-domain message already says to mount the same host path: no second hint.
            CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                f"Plex is running, but not with the database file this app sees at /plex/db. {SAME_HOST_PATH_ADVICE}",
                _LOCK_DOMAIN,
            ),
            False,
        ),
        (
            CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                "Plex's database is on a network share (nfs4).",
                {"db_path": "/plex/db", "fs_type": "nfs4", "plex_pass": True, "plex_version": "1.43.0"},
            ),
            False,
        ),
        (
            CapabilityReport(
                Capability.NEEDS_PASS,
                "No Plex Pass",
                {"db_path": "/plex/db", "plex_pass": False, "plex_version": "1.43.0"},
            ),
            False,
        ),
    ],
)
def test_plex_capability_states_and_the_same_host_path_hint(factory, report, expect_hint):
    factory.reports[ServerType.PLEX] = report
    cfg = server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=False))
    capability = inspect.server_status_payload(MagicMock(), cfg)["capability"]
    assert capability["state"] == report.state.value
    assert capability["message"] == report.message
    expected = dict(report.details)
    if expect_hint:
        expected["hint"] = inspect.PLEX_SAME_HOST_PATH_HINT
    assert capability["details"] == expected
    assert "identical host path" in inspect.PLEX_SAME_HOST_PATH_HINT
    assert "/mnt/user" in inspect.PLEX_SAME_HOST_PATH_HINT and "/mnt/cache" in inspect.PLEX_SAME_HOST_PATH_HINT


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"plex_pass": True, "version": "1.43.1"}, {"plex_pass": True, "plex_version": "1.43.1"}),
        (None, {"plex_pass": None, "plex_version": None}),
    ],
)
def test_plex_early_failure_still_shows_pass_and_version(status, expected):
    # Real publisher: no Plex config folder → MISCONFIGURED before it asks Plex anything.
    cfg = server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=False))
    server = MagicMock(name="plex-client")
    server.get_server_status.return_value = status
    capability = inspect.server_status_payload(server, cfg)["capability"]
    assert capability["state"] == "misconfigured"
    assert "Plex database not found" in capability["message"]
    assert capability["details"] == expected
    server.get_server_status.assert_called_once_with()


@pytest.mark.parametrize(
    ("stype", "state", "pass_known", "warning"),
    [
        (ServerType.PLEX, Capability.READY, False, inspect.PLEX_PASS_UNCHECKED_WARNING),
        (ServerType.PLEX, Capability.READY, True, ""),
        # Not ready: the message already says what's wrong, and nothing would be written either way.
        (ServerType.PLEX, Capability.NEEDS_LOCAL_DB, False, ""),
        (ServerType.JELLYFIN, Capability.READY, False, ""),
    ],
    ids=["plex-ready-pass-unknown", "plex-ready-pass-known", "plex-not-ready", "jellyfin"],
)
def test_ready_plex_whose_plex_pass_couldnt_be_checked_warns(factory, stype, state, pass_known, warning):
    details = {"db_path": "/plex/db", "plex_pass": True if pass_known else None, "plex_version": "1.43.0"}
    if stype is ServerType.JELLYFIN:
        details = {"plugin_version": "1.4.0"}
    factory.reports[stype] = CapabilityReport(state, "", details)
    cfg = server_config("srv", stype)
    capability = inspect.server_status_payload(MagicMock(), cfg)["capability"]
    assert (capability["state"], capability["warning"]) == (state.value, warning)
    assert capability["details"] == details
    assert "Plex Pass" in inspect.PLEX_PASS_UNCHECKED_WARNING


def test_plex_status_error_while_filling_details_is_not_fatal(factory):
    factory.reports[ServerType.PLEX] = CapabilityReport(Capability.MISCONFIGURED, "Plex database not found at /x")
    server = MagicMock()
    server.get_server_status.side_effect = RuntimeError("boom")
    cfg = server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=True))
    capability = inspect.server_status_payload(server, cfg)["capability"]
    assert capability["details"] == {"plex_pass": None, "plex_version": None}


def _jellyfin_client(*, info, access="ok"):
    server = MagicMock(name="jellyfin-client")
    server.get_bridge_info.return_value = info
    server.get_bridge_markers_access.return_value = access
    return server


_INSTALLED = {"installed": True, "version": "1.4.0", "features": [MARKERS_FEATURE]}


@pytest.mark.parametrize(
    ("info", "access", "state", "message_part"),
    [
        (_INSTALLED, "ok", "ready", "Media Preview Bridge plugin"),
        (_INSTALLED, "forbidden", "misconfigured", "needs administrator rights"),
        (_INSTALLED, "unauthorized", "misconfigured", "rejected this server's credentials"),
        (_INSTALLED, None, "unreachable", "markers endpoint"),
        (
            {"installed": False, "version": None, "features": []},
            "ok",
            "needs_plugin",
            "Install the Media Preview Bridge",
        ),
        ({"installed": True, "version": "1.0.0", "features": []}, "ok", "plugin_outdated", "installed 1.0.0"),
        (None, "ok", "unreachable", "Can't reach this Jellyfin server"),
    ],
)
@pytest.mark.parametrize("stored_enabled", [False, True])
def test_jellyfin_capability_messages_pass_through(info, access, state, message_part, stored_enabled):
    cfg = server_config("jf", ServerType.JELLYFIN, markers={"enabled": stored_enabled, "library_ids": None})
    payload = inspect.server_status_payload(_jellyfin_client(info=info, access=access), cfg)
    assert payload["enabled"] is stored_enabled
    assert payload["capability"]["state"] == state
    assert message_part in payload["capability"]["message"]
    assert payload["can_show"] == ["intro", "credits", "recap", "preview"]
    assert payload["settings"] == {"enabled": stored_enabled, "library_ids": None}


def test_jellyfin_ready_details(factory):
    factory.reports[ServerType.JELLYFIN] = CapabilityReport(
        Capability.READY, "Media Preview Bridge plugin", {"plugin_version": "1.4.0"}
    )
    cfg = server_config("jf", ServerType.JELLYFIN, markers={"enabled": False, "library_ids": ["a"]})
    payload = inspect.server_status_payload(MagicMock(), cfg)
    assert factory.calls[0]["settings"].enabled is True
    assert factory.calls[0]["settings"].library_ids == ("a",)
    assert payload["capability"] == {
        "state": "ready",
        "message": "Media Preview Bridge plugin",
        "details": {"plugin_version": "1.4.0"},
        "warning": "",
    }
    assert payload["can_show"] == ["intro", "credits", "recap", "preview"]


def test_emby_without_the_plugin_needs_it_and_says_whether_the_catalog_has_it():
    cfg = server_config(
        "emby",
        ServerType.EMBY,
        markers={"enabled": False, "library_ids": None, "emby": {"on_emby_redetect": "keep_emby"}},
    )
    server = MagicMock(name="emby-client")
    server.get_bridge_info.return_value = {"installed": False, "version": None, "features": []}
    server.bridge_catalog_listed.return_value = False
    payload = inspect.server_status_payload(server, cfg)
    assert payload["capability"]["state"] == "needs_plugin"
    assert payload["capability"]["message"] == "Install the Media Preview Bridge for Emby plugin"
    assert payload["capability"]["details"] == {"catalog_listed": False}
    assert payload["can_show"] == ["intro", "credits"]
    assert payload["settings"] == {"enabled": False, "library_ids": None, "emby": {"on_emby_redetect": "keep_emby"}}


def test_a_server_type_without_a_publisher_says_so(monkeypatch):
    monkeypatch.setattr(inspect, "publisher_for", lambda server, config, **kwargs: None)
    payload = inspect.server_status_payload(MagicMock(), server_config("jf", ServerType.JELLYFIN))
    assert payload["capability"]["state"] == "needs_plugin"
    assert payload["capability"]["message"] == "No marker publisher for this server type"


def test_server_turned_off_is_never_probed(factory):
    cfg = server_config("jf", ServerType.JELLYFIN, enabled=False)
    server = MagicMock()
    payload = inspect.server_status_payload(server, cfg)
    assert factory.calls == []
    assert server.method_calls == []
    assert payload["capability"] == {
        "state": "disabled",
        "message": "This server is turned off on the Servers page",
        "details": {},
        "warning": "",
    }


def test_missing_client_is_misconfigured(factory):
    cfg = server_config("jf", ServerType.JELLYFIN)
    payload = inspect.server_status_payload(None, cfg)
    assert factory.calls == []
    assert payload["capability"]["state"] == "misconfigured"


_UNKNOWN_CAPABILITY = {
    "state": "unknown",
    "message": "Couldn't check this server (RuntimeError)",
    "details": {},
    "warning": "",
}


def test_capability_check_that_raises_reads_as_unknown_and_is_not_cached(monkeypatch):
    fake = _PublisherFactory(raises=RuntimeError("secret detail"))
    monkeypatch.setattr(inspect, "publisher_for", fake)
    cfg = server_config("jf", ServerType.JELLYFIN)
    assert inspect.server_status_payload(MagicMock(), cfg)["capability"] == _UNKNOWN_CAPABILITY
    assert inspect.server_status_payload(MagicMock(), cfg)["capability"] == _UNKNOWN_CAPABILITY
    assert len(fake.calls) == 2


@pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY])
def test_publisher_for_raising_still_gives_a_status_payload(monkeypatch, stype):
    calls = []

    def boom(server, config, **kwargs):
        calls.append(config.id)
        raise RuntimeError(f"token=abc path={config.url}")

    monkeypatch.setattr(inspect, "publisher_for", boom)
    cfg = server_config("s1", stype, libraries=[Library("1", "Sports", ("/media",))])
    payload = inspect.server_status_payload(MagicMock(), cfg)
    assert payload["capability"] == _UNKNOWN_CAPABILITY
    assert payload["server_id"] == "s1" and payload["server_type"] == stype.value
    assert payload["libraries"] == [{"id": "1", "name": "Sports", "kind": None, "default_selected": False}]
    inspect.server_status_payload(MagicMock(), cfg)
    assert calls == ["s1", "s1"]


# --------------------------------------------------------------------------- resolve_local_path


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    (root / "tv" / "Show").mkdir(parents=True)
    episode = root / "tv" / "Show" / "S01E01.mkv"
    episode.write_bytes(b"x")
    return root


def test_resolve_local_path_maps_through_path_mappings(media):
    cfg = server_config("plex", ServerType.PLEX)
    cfg.path_mappings.append({"remote_prefix": "/data/tv", "local_prefix": str(media / "tv")})
    server = MagicMock()
    server.resolve_item_to_remote_path.return_value = "/data/tv/Show/S01E01.mkv"
    assert inspect.resolve_local_path(server, cfg, "42") == str(media / "tv" / "Show" / "S01E01.mkv")
    server.resolve_item_to_remote_path.assert_called_once_with("42")


def test_resolve_local_path_picks_the_candidate_that_exists(media):
    cfg = server_config("plex", ServerType.PLEX)
    cfg.path_mappings.extend(
        [
            {"remote_prefix": "/data/tv", "local_prefix": str(media / "missing-disk")},
            {"remote_prefix": "/data/tv", "local_prefix": str(media / "tv")},
        ]
    )
    server = MagicMock()
    server.resolve_item_to_remote_path.return_value = "/data/tv/Show/S01E01.mkv"
    assert inspect.resolve_local_path(server, cfg, "42") == str(media / "tv" / "Show" / "S01E01.mkv")


def test_resolve_local_path_without_mapping_uses_the_server_path(media):
    cfg = server_config("jf", ServerType.JELLYFIN)
    server = MagicMock()
    server.resolve_item_to_remote_path.return_value = str(media / "tv" / "Show" / "S01E01.mkv")
    assert inspect.resolve_local_path(server, cfg, "abc") == str(media / "tv" / "Show" / "S01E01.mkv")


@pytest.mark.parametrize(
    "remote",
    [None, "", "/data/tv/Show/S01E02.mkv", RuntimeError("down")],
)
def test_resolve_local_path_none_when_nothing_exists(media, remote):
    cfg = server_config("plex", ServerType.PLEX)
    cfg.path_mappings.append({"remote_prefix": "/data/tv", "local_prefix": str(media / "tv")})
    server = MagicMock()
    if isinstance(remote, Exception):
        server.resolve_item_to_remote_path.side_effect = remote
    else:
        server.resolve_item_to_remote_path.return_value = remote
    assert inspect.resolve_local_path(server, cfg, "42") is None


def test_resolve_local_path_ignores_a_folder(media):
    cfg = server_config("jf", ServerType.JELLYFIN)
    server = MagicMock()
    server.resolve_item_to_remote_path.return_value = str(media / "tv" / "Show")
    assert inspect.resolve_local_path(server, cfg, "abc") is None


# --------------------------------------------------------------------------- item_payload

PATH = "/media/tv/Show/Season 01/Show - S01E01.mkv"


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"), clock=lambda: datetime(2026, 9, 14, tzinfo=timezone.utc))
    yield s
    s.close()


def _registry(*configs):
    return FakeRegistry({c.id: c for c in configs})


def _decided(marker):
    return TypeDecision(marker.type, DecisionStatus.DECIDED, marker, None, "agreed")


def _none(mtype, status=DecisionStatus.NO_EVIDENCE, reason="nothing found"):
    return TypeDecision(mtype, status, None, None, reason)


def _known_file(store, decisions=None):
    rec = store.upsert_file(
        FileIdentity(PATH, 100, 200), duration_ms=DURATION, season_key="/media/tv/Show", is_movie=False
    )
    store.replace_evidence(
        rec.id, Source.CHAPTERS, [Candidate(T.INTRO, 11_000, 37_000, Source.CHAPTERS, origin="Opening")]
    )
    store.replace_evidence(rec.id, Source.THEINTRODB, [], detail="no data")
    store.save_decisions(
        rec.id,
        decisions
        or {
            T.INTRO: _decided(INTRO),
            T.CREDITS: _decided(CREDITS),
            T.RECAP: _none(T.RECAP),
            T.PREVIEW: _none(T.PREVIEW, DecisionStatus.DISABLED, "not detected"),
        },
        settings_fingerprint="f",
    )
    return rec


def _published(
    store, rec, server_id, item_id, ours, *, basis_for=None, status="written", message="2 marker(s)", kept=None
):
    store.set_publish_state(rec.id, server_id, item_id=item_id, markers=list(ours), status=status, message=message)
    version = store.set_item_publish_state(server_id, item_id, list(ours), "written", kept_types=kept)
    if basis_for is not None:
        store.set_publish_basis(
            rec.id, server_id, decided_hash=MarkerStore.markers_hash(basis_for), item_version=version
        )
    return version


def _plex_rows(*markers, final_credits=True):
    return [
        {
            "type": m.type.value,
            "start_ms": m.start_ms,
            "end_ms": m.end_ms,
            "final": m.type is T.CREDITS and final_credits,
        }
        for m in markers
    ]


def _jf_rows(*markers):
    names = {T.INTRO: "Intro", T.CREDITS: "Outro", T.RECAP: "Recap", T.PREVIEW: "Preview"}
    return [{"Type": names[m.type], "StartTicks": m.start_ms * 10_000, "EndTicks": m.end_ms * 10_000} for m in markers]


def _row(payload, server_id):
    return next(r for r in payload["servers"] if r["server_id"] == server_id)


def test_unknown_file_lists_every_owning_server(store, factory):
    plex = server_config("plex", ServerType.PLEX)
    jf = server_config("jf", ServerType.JELLYFIN, markers={"enabled": False, "library_ids": None})
    elsewhere = server_config("other", ServerType.JELLYFIN, root="/elsewhere")
    turned_off = server_config("off", ServerType.PLEX, enabled=False)
    registry = _registry(plex, jf, elsewhere, turned_off)
    factory.reports[ServerType.JELLYFIN] = CapabilityReport(Capability.DISABLED, "off")

    payload = inspect.item_payload(PATH, registry=registry, store=store)

    assert payload["known"] is False
    assert payload["canonical_path"] == PATH
    assert payload["duration_ms"] is None and payload["is_movie"] is None
    assert payload["evidence"] == []
    assert payload["decisions"] == {
        t.value: {"status": None, "reason": "", "marker": None, "proposed": None, "shortened_by": None}
        for t in MarkerType
    }
    assert payload["servers"] == [
        {
            "server_id": "plex",
            "server_name": "PLEX",
            "server_type": "plex",
            "markers_enabled": True,
            "capability_state": "ready",
            "can_show": ["intro", "credits"],
            "current": [],
            "published": [],
            "publish_status": None,
            "publish_message": "",
            "item_status": None,
            "plan": "nothing_to_publish",
            "plan_reason": "",
            "version_count": None,  # the fake Plex doesn't say
            "error": None,
        },
        {
            "server_id": "jf",
            "server_name": "JF",
            "server_type": "jellyfin",
            "markers_enabled": False,
            "capability_state": "disabled",
            "can_show": ["intro", "credits", "recap", "preview"],
            "current": [],
            "published": [],
            "publish_status": None,
            "publish_message": "",
            "item_status": None,
            "plan": "not_enabled",
            "plan_reason": "Intro & Credits is off for this server",
            "version_count": None,
            "error": None,
        },
    ]
    # Capability uses each server's stored settings (no preview override) for the Inspector.
    assert [(c["config"].id, c.get("settings")) for c in factory.calls] == [("plex", None), ("jf", None)]
    registry.get("plex").resolve_remote_path_to_item_id.assert_called_once_with(PATH, library_ids=["1"])
    registry.get("plex").get_markers.assert_called_once_with("item-plex")
    registry.get("jf").get_media_segments.assert_called_once_with("item-jf")


def test_library_with_previews_off_still_owns_the_file(store, factory):
    lib = Library("1", "TV Shows", ("/media",), enabled=False)
    payload = inspect.item_payload(
        PATH, registry=_registry(server_config("plex", ServerType.PLEX, libraries=[lib])), store=store
    )
    assert [r["server_id"] for r in payload["servers"]] == ["plex"]
    assert _row(payload, "plex")["markers_enabled"] is True


@pytest.mark.parametrize(
    ("library_ids", "name", "enabled", "reason"),
    [
        (["2"], "TV Shows", True, "This library isn't selected for Intro & Credits on this server"),
        (None, "Sports", True, "This library isn't selected for Intro & Credits on this server"),
        (["1"], "Sports", True, None),  # an explicit choice includes a sports library
        (None, "TV Shows", True, None),
    ],
)
def test_library_selection_decides_markers_enabled(store, factory, library_ids, name, enabled, reason):
    _known_file(store)
    cfg = server_config(
        "jf",
        ServerType.JELLYFIN,
        markers={"enabled": enabled, "library_ids": library_ids},
        libraries=[Library("1", name, ("/media",))],
    )
    row = _row(inspect.item_payload(PATH, registry=_registry(cfg), store=store), "jf")
    if reason is None:
        assert row["markers_enabled"] is True
        assert row["plan"] == "will_add"
    else:
        assert row["markers_enabled"] is False
        assert (row["plan"], row["plan_reason"]) == ("not_enabled", reason)


def test_excluded_path_is_not_listed(store, factory):
    cfg = server_config("plex", ServerType.PLEX, exclude_paths=[{"value": "/media/tv/Show", "type": "path"}])
    assert inspect.item_payload(PATH, registry=_registry(cfg), store=store)["servers"] == []


def test_missing_client_is_not_listed(store, factory):
    registry = _registry(server_config("plex", ServerType.PLEX))
    registry.get = lambda sid: None
    assert inspect.item_payload(PATH, registry=registry, store=store)["servers"] == []


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (
            DecisionStatus.DECIDED,
            "chapters; start shortened to the server's own marker (plex)",
            {"servers": ["PLEX"]},
        ),
        # a server that has since been removed keeps its id
        (
            DecisionStatus.DECIDED,
            "sources agree: skipdb, introdb; start shortened to the server's own marker (gone, plex)",
            {"servers": ["gone", "PLEX"]},
        ),
        (DecisionStatus.DECIDED, "chapters", None),
        # not published: the shortened marker failed sanity
        (DecisionStatus.NEEDS_REVIEW, "start shortened to the server's own marker (plex) fails sanity checks", None),
    ],
    ids=["one-server", "removed-server", "not-shortened", "needs-review"],
)
def test_decisions_name_the_servers_whose_own_markers_shortened_them(store, factory, status, reason, expected):
    rec = _known_file(store)
    marker = CREDITS if status is DecisionStatus.DECIDED else None
    proposed = None if marker else CREDITS
    store.save_decisions(
        rec.id, {T.CREDITS: TypeDecision(T.CREDITS, status, marker, proposed, reason)}, settings_fingerprint="f"
    )
    registry = _registry(server_config("plex", ServerType.PLEX))
    payload = inspect.item_payload(PATH, registry=registry, store=store)
    assert payload["decisions"]["credits"]["reason"] == reason
    assert payload["decisions"]["credits"]["shortened_by"] == expected
    assert payload["decisions"]["intro"]["shortened_by"] is None


def test_known_file_decisions_and_evidence(store, factory):
    rec = _known_file(store)
    store.save_decisions(
        rec.id,
        {
            T.RECAP: TypeDecision(
                T.RECAP, DecisionStatus.NEEDS_REVIEW, None, Marker(T.RECAP, 1_000, 8_000, ("x",)), "disagree"
            )
        },
        settings_fingerprint="f",
    )
    payload = inspect.item_payload(PATH, registry=_registry(), store=store)
    assert payload["known"] is True
    assert (payload["duration_ms"], payload["is_movie"]) == (DURATION, False)
    assert payload["decisions"] == {
        "intro": {
            "status": "decided",
            "reason": "agreed",
            "marker": {
                "type": "intro",
                "start_ms": 11_000,
                "end_ms": 37_000,
                "decided_by": ["chapters", "theintrodb"],
                "locked": False,
            },
            "proposed": None,
            "shortened_by": None,
        },
        "credits": {
            "status": "decided",
            "reason": "agreed",
            "marker": {
                "type": "credits",
                "start_ms": 1_290_000,
                "end_ms": DURATION,
                "decided_by": ["chapters"],
                "locked": False,
            },
            "proposed": None,
            "shortened_by": None,
        },
        "recap": {
            "status": "needs_review",
            "reason": "disagree",
            "marker": None,
            "proposed": {"start_ms": 1_000, "end_ms": 8_000},
            "shortened_by": None,
        },
        "preview": {
            "status": "disabled",
            "reason": "not detected",
            "marker": None,
            "proposed": None,
            "shortened_by": None,
        },
    }
    assert payload["evidence"] == [
        {
            "source": "chapters",
            "origin": "",
            "type": "intro",
            "start_ms": 11_000,
            "end_ms": 37_000,
            "confidence": 1.0,
            "detail": "",
            "fetched_at": FETCHED,
            "label": "Opening",
        },
        {
            "source": "theintrodb",
            "origin": "",
            "type": None,
            "start_ms": None,
            "end_ms": None,
            "confidence": None,
            "detail": "no data",
            "fetched_at": FETCHED,
            "label": "",
        },
    ]


def test_locked_marker_shows_as_locked_and_is_published(store, factory):
    rec = _known_file(store, {t: _none(t) for t in MarkerType})
    store.lock_marker(rec.id, Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True))
    payload = inspect.item_payload(PATH, registry=_registry(server_config("plex", ServerType.PLEX)), store=store)
    assert payload["decisions"]["intro"]["marker"] == {
        "type": "intro",
        "start_ms": 5_000,
        "end_ms": 30_000,
        "decided_by": ["user"],
        "locked": True,
    }
    assert _row(payload, "plex")["plan"] == "will_add"


def test_plex_up_to_date_when_current_equals_published_equals_decided(store, factory):
    rec = _known_file(store)
    _published(store, rec, "plex", "rk-1", [INTRO, CREDITS], basis_for=[INTRO, CREDITS])
    registry = _registry(server_config("plex", ServerType.PLEX))
    registry.get("plex").get_markers.return_value = _plex_rows(INTRO, CREDITS)

    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")

    assert row["current"] == [
        {"type": "intro", "start_ms": 11_000, "end_ms": 37_000},
        {"type": "credits", "start_ms": 1_290_000, "end_ms": None},
    ]
    assert row["published"] == [
        {"type": "intro", "start_ms": 11_000, "end_ms": 37_000},
        {"type": "credits", "start_ms": 1_290_000, "end_ms": DURATION},
    ]
    assert (row["publish_status"], row["publish_message"], row["plan"]) == ("written", "2 marker(s)", "up_to_date")
    # The file's publish state already knows the item: no lookup by path.
    registry.get("plex").resolve_remote_path_to_item_id.assert_not_called()
    registry.get("plex").get_markers.assert_called_once_with("rk-1")


def test_plex_will_replace_when_plex_shows_its_own_intro(store, factory):
    # South Park S01E01: Plex's own detection put the intro at 1:16.5–1:52.7; the sources decided 0:11–0:37.
    _known_file(store)
    registry = _registry(server_config("plex", ServerType.PLEX))
    plex_intro = Marker(T.INTRO, 76_500, 112_700, ("plex",))
    registry.get("plex").get_markers.return_value = _plex_rows(plex_intro)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert row["current"] == [{"type": "intro", "start_ms": 76_500, "end_ms": 112_700}]
    assert row["plan"] == "will_replace"
    assert row["published"] == [] and row["publish_status"] is None


def test_plex_own_marker_of_a_type_we_did_not_decide_is_left_out_of_the_comparison(store, factory):
    rec = _known_file(
        store,
        {T.INTRO: _none(T.INTRO), T.CREDITS: _decided(CREDITS), T.RECAP: _none(T.RECAP), T.PREVIEW: _none(T.PREVIEW)},
    )
    _published(store, rec, "plex", "rk-1", [CREDITS], basis_for=[CREDITS])
    registry = _registry(server_config("plex", ServerType.PLEX))
    registry.get("plex").get_markers.return_value = _plex_rows(Marker(T.INTRO, 76_500, 112_700, ()), CREDITS)
    assert _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")["plan"] == "up_to_date"


def test_will_add_when_the_server_shows_nothing(store, factory):
    _known_file(store)
    registry = _registry(server_config("plex", ServerType.PLEX))
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert (row["current"], row["plan"]) == ([], "will_add")


@pytest.mark.parametrize("failure", ["none", "raises"])
def test_current_is_null_when_the_live_read_fails(store, factory, failure):
    _known_file(store)
    registry = _registry(server_config("plex", ServerType.PLEX))
    if failure == "none":
        registry.get("plex").get_markers.return_value = None
    else:
        registry.get("plex").get_markers.side_effect = RuntimeError("down")
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert row["current"] is None
    assert row["plan"] == "unknown"


@pytest.mark.parametrize("lookup", [None, RuntimeError("down")])
def test_unknown_item_id_means_no_live_read(store, factory, lookup):
    _known_file(store)
    registry = _registry(server_config("plex", ServerType.PLEX))
    server = registry.get("plex")
    if isinstance(lookup, Exception):
        server.resolve_remote_path_to_item_id.side_effect = lookup
    else:
        server.resolve_remote_path_to_item_id.return_value = lookup
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert row["current"] is None
    assert row["plan"] == "unknown"
    server.get_markers.assert_not_called()


def test_jellyfin_current_includes_our_own_served_segments(store, factory):
    rec = _known_file(store)
    _published(store, rec, "jf", "jf-item", [INTRO, CREDITS], basis_for=[INTRO, CREDITS])
    registry = _registry(server_config("jf", ServerType.JELLYFIN))
    server = registry.get("jf")
    server.get_media_segments.return_value = _jf_rows(INTRO, CREDITS)
    server.get_bridge_markers.return_value = [
        {"type": "Intro", "startTicks": INTRO.start_ms * 10_000, "endTicks": INTRO.end_ms * 10_000},
        {"type": "Outro", "startTicks": CREDITS.start_ms * 10_000, "endTicks": CREDITS.end_ms * 10_000},
    ]
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "jf")
    assert row["current"] == [
        {"type": "intro", "start_ms": 11_000, "end_ms": 37_000},
        {"type": "credits", "start_ms": 1_290_000, "end_ms": DURATION},
    ]
    assert row["plan"] == "up_to_date"
    server.get_media_segments.assert_called_once_with("jf-item")
    server.get_bridge_markers.assert_not_called()


def test_jellyfin_can_show_recaps(store, factory):
    _known_file(
        store,
        {T.INTRO: _decided(INTRO), T.CREDITS: _none(T.CREDITS), T.RECAP: _decided(RECAP), T.PREVIEW: _none(T.PREVIEW)},
    )
    registry = _registry(server_config("jf", ServerType.JELLYFIN), server_config("plex", ServerType.PLEX))
    registry.get("jf").get_media_segments.return_value = _jf_rows(RECAP, INTRO)
    payload = inspect.item_payload(PATH, registry=registry, store=store)
    assert _row(payload, "jf")["plan"] == "up_to_date"
    # Plex can't show recaps: the recap isn't part of what it would get.
    registry.get("plex").get_markers.return_value = _plex_rows(INTRO)
    assert _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")["plan"] == "up_to_date"


def test_emby_row_reads_chapter_markers_and_needs_its_plugin(store, factory):
    _known_file(store)
    registry = _registry(server_config("emby", ServerType.EMBY))
    registry.get("emby").get_chapter_markers.return_value = [
        {"marker_type": "IntroStart", "start_ms": 11_000},
        {"marker_type": "IntroEnd", "start_ms": 37_000},
        {"marker_type": "CreditsStart", "start_ms": 1_290_000},
    ]
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "emby")
    assert row["capability_state"] == "needs_plugin"
    assert row["can_show"] == ["intro", "credits"]
    assert row["current"] == [
        {"type": "intro", "start_ms": 11_000, "end_ms": 37_000},
        {"type": "credits", "start_ms": 1_290_000, "end_ms": None},
    ]
    assert row["plan"] == "up_to_date"


@pytest.mark.parametrize(
    ("stype", "decided_credits", "shown", "plan"),
    [
        # Lab: Plex's own credits detection kept the start but stopped before the end; ours ran to the end.
        (ServerType.PLEX, CREDITS, {"start_ms": 1_290_000, "end_ms": 1_300_000, "final": False}, "will_replace"),
        (ServerType.PLEX, CREDITS, {"start_ms": 1_290_400, "end_ms": 1_319_000, "final": True}, "up_to_date"),
        # Ours stop inside the file; Plex's final flag says its credits run to the end.
        (
            ServerType.PLEX,
            Marker(T.CREDITS, 1_290_000, 1_300_000, ("c",)),
            {"start_ms": 1_290_000, "end_ms": 1_300_000, "final": True},
            "will_replace",
        ),
        (
            ServerType.PLEX,
            Marker(T.CREDITS, 1_290_000, 1_300_000, ("c",)),
            {"start_ms": 1_290_000, "end_ms": 1_300_500, "final": False},
            "up_to_date",
        ),
        # Emby has no credits end at all, so only the start can be compared.
        (ServerType.EMBY, Marker(T.CREDITS, 1_290_000, 1_300_000, ("c",)), {"start_ms": 1_290_000}, "up_to_date"),
    ],
    ids=["plex-ends-early", "plex-final", "plex-final-vs-inside", "plex-inside", "emby-no-end"],
)
def test_a_credits_end_that_differs_is_not_up_to_date(store, factory, stype, decided_credits, shown, plan):
    _known_file(
        store,
        {
            T.INTRO: _none(T.INTRO),
            T.CREDITS: _decided(decided_credits),
            T.RECAP: _none(T.RECAP),
            T.PREVIEW: _none(T.PREVIEW),
        },
    )
    sid = stype.value
    registry = _registry(server_config(sid, stype))
    if stype is ServerType.PLEX:
        registry.get(sid).get_markers.return_value = [{"type": "credits", **shown}]
    else:
        registry.get(sid).get_chapter_markers.return_value = [
            {"marker_type": "CreditsStart", "start_ms": shown["start_ms"]}
        ]
    assert _row(inspect.item_payload(PATH, registry=registry, store=store), sid)["plan"] == plan


PLEX_CREDITS = Marker(T.CREDITS, 1_250_000, 1_280_000, ())
PLEX_INTRO = Marker(T.INTRO, 60_000, 90_000, ())
EMBY_INTRO = PLEX_INTRO


def _emby_rows(*markers):
    rows = []
    for m in markers:
        if m.type is T.INTRO:
            rows += [
                {"marker_type": "IntroStart", "start_ms": m.start_ms},
                {"marker_type": "IntroEnd", "start_ms": m.end_ms},
            ]
        else:
            rows.append({"marker_type": "CreditsStart", "start_ms": m.start_ms})
    return rows


def test_emby_plan_says_emby_skips_to_the_end_of_the_file_for_credits_that_end_before_it(store, factory):
    early = Marker(T.CREDITS, 1_250_000, 1_290_000, ("chapters",))  # a scene follows: ends 30 s before the file does
    _known_file(
        store,
        {T.INTRO: _decided(INTRO), T.CREDITS: _decided(early), T.RECAP: _none(T.RECAP), T.PREVIEW: _none(T.PREVIEW)},
    )
    registry = _registry(server_config("emby", ServerType.EMBY), server_config("jf", ServerType.JELLYFIN))
    registry.get("emby").get_chapter_markers.return_value = _emby_rows(INTRO, early)
    registry.get("jf").get_media_segments.return_value = _jf_rows(INTRO, early)
    payload = inspect.item_payload(PATH, registry=registry, store=store)
    # Emby gets the credits start anyway (owner decision 2026-09-14) and says what its player does with it.
    assert (_row(payload, "emby")["plan"], _row(payload, "emby")["plan_reason"]) == (
        "up_to_date",
        "Emby skips to the end of the file",
    )
    assert (_row(payload, "jf")["plan"], _row(payload, "jf")["plan_reason"]) == ("up_to_date", "")
    registry.get("emby").get_chapter_markers.return_value = _emby_rows(INTRO)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "emby")
    assert (row["plan"], row["plan_reason"]) == ("will_replace", "Emby skips to the end of the file")
    registry.get("emby").get_chapter_markers.return_value = None
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "emby")
    assert (row["plan"], row["plan_reason"]) == ("unknown", "Emby skips to the end of the file")


def test_emby_keep_rule_compares_credits_by_their_start(store, factory):
    # Emby keeps no credits end: a credits start equal to ours is ours, not Emby's, even for credits ending early.
    early = Marker(T.CREDITS, 1_250_000, 1_290_000, ("chapters",))
    _known_file(
        store,
        {T.INTRO: _decided(INTRO), T.CREDITS: _decided(early), T.RECAP: _none(T.RECAP), T.PREVIEW: _none(T.PREVIEW)},
    )
    markers = {"enabled": True, "library_ids": None, "emby": {"on_emby_redetect": "keep_emby"}}
    registry = _registry(server_config("emby", ServerType.EMBY, markers=markers))
    registry.get("emby").get_chapter_markers.return_value = _emby_rows(INTRO, early)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "emby")
    assert (row["plan"], row["plan_reason"]) == ("up_to_date", "Emby skips to the end of the file")


def test_emby_credits_that_run_to_the_end_carry_no_note(store, factory):
    _known_file(store)
    registry = _registry(server_config("emby", ServerType.EMBY))
    registry.get("emby").get_chapter_markers.return_value = _emby_rows(INTRO, CREDITS)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "emby")
    assert (row["plan"], row["plan_reason"]) == ("up_to_date", "")


@pytest.mark.parametrize(
    ("setting", "ours", "kept", "shown", "plan", "reason"),
    [
        ("keep_emby", [INTRO, CREDITS], None, [EMBY_INTRO, CREDITS], "keeps_emby", "Keeping Emby's intro"),
        ("restore", [INTRO, CREDITS], None, [EMBY_INTRO, CREDITS], "will_replace", ""),
        ("keep_emby", [INTRO, CREDITS], None, [CREDITS], "will_replace", ""),  # gone, not replaced: ours go back
        ("keep_emby", [INTRO, CREDITS], None, [INTRO, CREDITS], "up_to_date", ""),
        ("keep_emby", [CREDITS], {T.INTRO}, [EMBY_INTRO, CREDITS], "keeps_emby", "Keeping Emby's intro"),
        ("keep_emby", [CREDITS], {T.INTRO}, [CREDITS], "will_replace", ""),  # Emby dropped its intro
        ("restore", [CREDITS], {T.INTRO}, [EMBY_INTRO, CREDITS], "will_replace", ""),  # switched to Use ours
        # A refresh deleted Emby's intro and the plugin wrote back the intro it stores: ours again, not Emby's.
        ("keep_emby", [CREDITS], {T.INTRO}, [INTRO, CREDITS], "up_to_date", ""),
    ],
    ids=["keep-replaced", "restore-replaced", "keep-missing", "keep-ours", "keep-recorded", "keep-recorded-gone", "restore-recorded", "keep-recorded-ours-again"],
)  # fmt: skip
def test_emby_markers_of_its_own_follow_the_setting(store, factory, setting, ours, kept, shown, plan, reason):
    rec = _known_file(store)
    markers = {"enabled": True, "library_ids": None, "emby": {"on_emby_redetect": setting}}
    registry = _registry(server_config("emby", ServerType.EMBY, markers=markers))
    _published(store, rec, "emby", "item-emby", ours, basis_for=[INTRO, CREDITS], kept=kept)
    registry.get("emby").get_chapter_markers.return_value = _emby_rows(*shown)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "emby")
    assert (row["plan"], row["plan_reason"]) == (plan, reason)


@pytest.mark.parametrize(
    ("redetect", "ours", "kept", "shown", "plan", "reason"),
    [
        ("keep_plex", [INTRO, CREDITS], None, [INTRO, PLEX_CREDITS], "keeps_plex", "Keeping Plex's credits"),
        ("restore", [INTRO, CREDITS], None, [INTRO, PLEX_CREDITS], "will_replace", ""),
        # Gone, not replaced: nothing of Plex's to keep, so ours are written again.
        ("keep_plex", [INTRO, CREDITS], None, [INTRO], "will_replace", ""),
        ("keep_plex", [INTRO, CREDITS], None, [INTRO, CREDITS], "up_to_date", ""),
        # Plex's intro replaced ours and a rescan dropped the credits: the credits go back, the intro stays Plex's.
        ("keep_plex", [INTRO, CREDITS], None, [PLEX_INTRO], "will_add", "Keeping Plex's intro"),
        (
            "keep_plex",
            [INTRO, CREDITS],
            None,
            [PLEX_INTRO, PLEX_CREDITS],
            "keeps_plex",
            "Keeping Plex's intro and credits",
        ),
        # Kept on an earlier run: the item record holds only the intro.
        ("keep_plex", [INTRO], {T.CREDITS}, [INTRO, PLEX_CREDITS], "keeps_plex", "Keeping Plex's credits"),
        ("keep_plex", [INTRO], {T.CREDITS}, [INTRO], "will_replace", ""),  # Plex dropped its credits: ours go back
        ("restore", [INTRO], {T.CREDITS}, [INTRO, PLEX_CREDITS], "will_replace", ""),  # switched to restore
        # Plex's kept credits happen to match the decision: still Plex's (unlike Emby, nothing writes ours back there).
        ("keep_plex", [INTRO], {T.CREDITS}, [INTRO, CREDITS], "keeps_plex", "Keeping Plex's credits"),
    ],
    ids=[
        "keep-replaced",
        "restore-replaced",
        "keep-missing",
        "keep-ours-shown",
        "keep-mixed",
        "keep-both",
        "keep-recorded",
        "keep-recorded-gone",
        "restore-recorded",
        "keep-recorded-equal-to-decision",
    ],
)
def test_plex_markers_replaced_by_plex_follow_on_plex_redetect(
    store, factory, redetect, ours, kept, shown, plan, reason
):
    rec = _known_file(store)
    _published(store, rec, "plex", "rk-1", ours, basis_for=[INTRO, CREDITS], kept=kept)
    markers = _plex_markers(enabled=True, redetect=redetect)
    registry = _registry(server_config("plex", ServerType.PLEX, markers=markers))
    registry.get("plex").get_markers.return_value = _plex_rows(*shown, final_credits=False)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert (row["plan"], row["plan_reason"]) == (plan, reason)


@pytest.mark.parametrize(
    ("redetect", "credits_decided", "recorded", "shown", "plan", "reason"),
    [
        # No record of ours on the item (a first publish, a reset markers.db): Plex's differing intro is kept.
        ("keep_plex", True, None, [PLEX_INTRO, CREDITS], "keeps_plex", "Keeping Plex's intro"),
        ("restore", True, None, [PLEX_INTRO, CREDITS], "will_replace", ""),
        # We removed our intro ourselves (the record lost it), then Plex filled it.
        ("keep_plex", True, [CREDITS], [PLEX_INTRO, CREDITS], "keeps_plex", "Keeping Plex's intro"),
        # The record is stale but Plex already shows our decision: ours, not Plex's.
        ("keep_plex", True, [PLEX_INTRO, CREDITS], [INTRO, CREDITS], "up_to_date", ""),
        # Plex still shows what we left and the decision changed since: ours, so the job replaces it.
        ("keep_plex", True, [PLEX_INTRO, CREDITS], [PLEX_INTRO, CREDITS], "will_replace", ""),
        # Credits aren't decided any more and Plex replaced ours: the job leaves Plex's credits alone.
        ("keep_plex", False, [INTRO, CREDITS], [INTRO, PLEX_CREDITS], "up_to_date", ""),
    ],
    ids=[
        "keep-no-record",
        "restore-no-record",
        "keep-after-our-removal",
        "keep-stale-record",
        "keep-decision-changed",
        "keep-undecided-type-replaced",
    ],
)
def test_plex_markers_we_have_no_record_of_follow_on_plex_redetect(
    store, factory, redetect, credits_decided, recorded, shown, plan, reason
):
    decisions = {
        T.INTRO: _decided(INTRO),
        T.CREDITS: _decided(CREDITS) if credits_decided else _none(T.CREDITS),
        T.RECAP: _none(T.RECAP),
        T.PREVIEW: _none(T.PREVIEW),
    }
    rec = _known_file(store, decisions)
    if recorded is not None:
        _published(store, rec, "plex", "rk-1", recorded)  # no basis: the file publishes again on its next run
    registry = _registry(server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=True, redetect=redetect)))
    registry.get("plex").resolve_remote_path_to_item_id.return_value = "rk-1"
    registry.get("plex").get_markers.return_value = _plex_rows(*shown, final_credits=False)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert (row["plan"], row["plan_reason"]) == (plan, reason)


def test_keep_plex_holds_on_every_path_not_only_an_unchanged_file(store, factory):
    # A forced run, a skipped or failed attempt, or another version's publish all go through the publisher's keep rule.
    rec = _known_file(store)
    _published(store, rec, "plex", "rk-1", [INTRO, CREDITS], basis_for=[INTRO])
    registry = _registry(
        server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=True, redetect="keep_plex"))
    )
    registry.get("plex").get_markers.return_value = _plex_rows(INTRO, PLEX_CREDITS, final_credits=False)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert (row["plan"], row["plan_reason"]) == ("keeps_plex", "Keeping Plex's credits")


def test_plex_waiting_on_versions_leaves_out_the_kept_types(store, factory):
    rec = _known_file(store)
    registry = _registry(
        server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=True, redetect="keep_plex"))
    )
    registry.get("plex").get_markers.return_value = _plex_rows(PLEX_INTRO)
    _published(store, rec, "plex", "rk-1", [], basis_for=[INTRO, CREDITS], status="waiting", kept={T.INTRO})
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert (row["plan"], row["plan_reason"]) == ("waiting", "versions don't agree yet; keeping Plex's intro")


@pytest.mark.parametrize(
    ("stype", "known_item", "versions", "count"),
    [
        (ServerType.PLEX, True, 1, 1),
        (ServerType.PLEX, True, 2, 2),
        (ServerType.PLEX, True, None, None),  # Plex didn't answer
        (ServerType.PLEX, False, 2, None),  # no item: nothing to ask
        (ServerType.JELLYFIN, True, 2, None),  # one item per version: never asked
    ],
    ids=["plex-one", "plex-two", "plex-unreadable", "plex-no-item", "jellyfin"],
)
def test_plex_row_carries_the_items_version_count(store, factory, stype, known_item, versions, count):
    # Versions, not parts: a stacked file or an optimized copy isn't another version (PlexServer.get_version_count).
    _known_file(store)
    sid = stype.value
    registry = _registry(server_config(sid, stype))
    server = registry.get(sid)
    server.get_version_count.return_value = versions
    if not known_item:
        server.resolve_remote_path_to_item_id.return_value = None
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), sid)
    assert row["version_count"] == count
    if stype is ServerType.PLEX and known_item:
        server.get_version_count.assert_called_once_with(f"item-{sid}")
    else:
        server.get_version_count.assert_not_called()
    server.get_part_durations.assert_not_called()


def test_capability_check_that_raises_reads_as_unknown_in_the_inspector(store, monkeypatch):
    fake = _PublisherFactory(raises=RuntimeError("x"))
    monkeypatch.setattr(inspect, "publisher_for", fake)
    registry = _registry(server_config("plex", ServerType.PLEX))
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert (row["capability_state"], row["error"]) == ("unknown", None)
    inspect.item_payload(PATH, registry=registry, store=store)
    assert len(fake.calls) == 2  # a failed check isn't cached


def test_published_comes_from_the_item_row_when_the_item_is_known(store, factory):
    # Another version's publish left only the intro on the shared Plex item after this file wrote both, then that
    # version's next write failed (Plex busy). This file's own row still says what happened to this file.
    rec = _known_file(store)
    store.set_publish_state(
        rec.id, "plex", item_id="rk-1", markers=[INTRO, CREDITS], status="written", message="2 marker(s)"
    )
    store.set_item_publish_state("plex", "rk-1", [INTRO, CREDITS], "written")
    store.set_item_publish_state("plex", "rk-1", [INTRO], "failed")
    registry = _registry(server_config("plex", ServerType.PLEX))
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert row["published"] == [{"type": "intro", "start_ms": 11_000, "end_ms": 37_000}]
    assert (row["publish_status"], row["publish_message"]) == ("written", "2 marker(s)")
    assert row["item_status"] == "failed"


def test_published_falls_back_to_the_file_state_without_an_item_row(store, factory):
    rec = _known_file(store)
    store.set_publish_state(
        rec.id, "plex", item_id=None, markers=[INTRO], status="waiting", message="Not in this server's library yet"
    )
    registry = _registry(server_config("plex", ServerType.PLEX))
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert row["published"] == [{"type": "intro", "start_ms": 11_000, "end_ms": 37_000}]
    assert (row["publish_status"], row["publish_message"]) == ("waiting", "Not in this server's library yet")
    assert row["item_status"] is None
    registry.get("plex").resolve_remote_path_to_item_id.assert_called_once_with(PATH, library_ids=["1"])


@pytest.mark.parametrize(
    ("stype", "waits"), [(ServerType.PLEX, True), (ServerType.EMBY, False), (ServerType.JELLYFIN, False)]
)
def test_only_plex_waits_for_its_versions_to_agree(store, factory, stype, waits):
    # Plex shows one marker set per item; Emby and Jellyfin versions are items with their own.
    rec = _known_file(store)
    sid = stype.value
    _published(store, rec, sid, "rk-1", [INTRO], basis_for=[INTRO, CREDITS], status="waiting", message="Waiting")
    registry = _registry(server_config(sid, stype))
    registry.get(sid).resolve_remote_path_to_item_id.return_value = "rk-1"
    registry.get(sid).get_markers.return_value = _plex_rows(INTRO)
    registry.get(sid).get_chapter_markers.return_value = _emby_rows(INTRO)
    registry.get(sid).get_media_segments.return_value = _jf_rows(INTRO)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), sid)
    assert (row["plan"], row["plan_reason"]) == (
        ("waiting", "versions don't agree yet") if waits else ("will_replace", "")
    )


def test_plex_decision_changed_since_publish_is_not_waiting(store, factory):
    # Credits were decided after the last publish: the next run adds them, it doesn't wait.
    rec = _known_file(store)
    _published(store, rec, "plex", "rk-1", [INTRO], basis_for=[INTRO])
    registry = _registry(server_config("plex", ServerType.PLEX))
    registry.get("plex").get_markers.return_value = _plex_rows(INTRO)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert row["plan"] == "will_replace"
    assert row["plan_reason"] == ""


def test_jellyfin_never_waits_for_versions(store, factory):
    rec = _known_file(store)
    _published(store, rec, "jf", "jf-item", [INTRO], basis_for=[INTRO, CREDITS])
    registry = _registry(server_config("jf", ServerType.JELLYFIN))
    registry.get("jf").get_media_segments.return_value = _jf_rows(INTRO)
    assert _row(inspect.item_payload(PATH, registry=registry, store=store), "jf")["plan"] == "will_replace"


@pytest.mark.parametrize(("ours", "plan"), [([INTRO], "will_remove"), ([], "nothing_to_publish")])
def test_nothing_decided_removes_what_is_ours(store, factory, ours, plan):
    rec = _known_file(store, {t: _none(t) for t in MarkerType})
    if ours:
        _published(store, rec, "plex", "rk-1", ours)
    registry = _registry(server_config("plex", ServerType.PLEX))
    registry.get("plex").get_markers.return_value = _plex_rows(*ours)
    assert _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")["plan"] == plan


@pytest.mark.parametrize(
    ("shown", "plan"),
    [
        ([INTRO, CREDITS, Marker(T.INTRO, 1_000, 9_000, ())], "up_to_date"),
        ([INTRO, CREDITS, Marker(T.CREDITS, 1_000_000, 1_100_000, ())], "up_to_date"),
        ([Marker(T.INTRO, 1_000, 9_000, ()), CREDITS], "will_replace"),  # ours isn't served: the job sends it
        ([CREDITS], "will_replace"),
    ],
    ids=["other-intro", "other-credits", "only-others-intro", "intro-missing"],
)
def test_jellyfin_another_providers_segment_beside_ours_is_up_to_date(store, factory, shown, plan):
    # Jellyfin serves every provider's segments side by side; the job reads ours as shown (compare_shown).
    rec = _known_file(store)
    _published(store, rec, "jf", "jf-item", [INTRO, CREDITS], basis_for=[INTRO, CREDITS])
    registry = _registry(server_config("jf", ServerType.JELLYFIN))
    registry.get("jf").get_media_segments.return_value = _jf_rows(*shown)
    assert _row(inspect.item_payload(PATH, registry=registry, store=store), "jf")["plan"] == plan


@pytest.mark.parametrize(
    ("current", "plan"),
    [
        # ends inside the file compare within 1 s; credits that run to the end match anything within 2 s of it
        ([Marker(T.INTRO, 11_900, 37_900, ()), Marker(T.CREDITS, 1_290_500, DURATION - 1_500, ())], "up_to_date"),
        # Jellyfin serves credits that stop 20 s before the end: not what this app sent (lab: a rescan's segments)
        ([Marker(T.INTRO, 11_900, 37_900, ()), Marker(T.CREDITS, 1_290_500, 1_300_000, ())], "will_replace"),
        ([Marker(T.INTRO, 12_100, 37_000, ()), CREDITS], "will_replace"),
        ([Marker(T.INTRO, 11_000, 38_100, ()), CREDITS], "will_replace"),
        ([INTRO], "will_replace"),
    ],
)
def test_comparison_tolerance(store, factory, current, plan):
    _known_file(store)
    registry = _registry(server_config("jf", ServerType.JELLYFIN))
    registry.get("jf").get_media_segments.return_value = _jf_rows(*current)
    assert _row(inspect.item_payload(PATH, registry=registry, store=store), "jf")["plan"] == plan


@pytest.mark.parametrize(
    ("setup", "plan"),
    [
        ("off", "not_enabled"),
        ("nothing_decided", "nothing_to_publish"),
        ("ours_left", "will_remove"),
        ("versions_waiting", "waiting"),
        ("decided", "unknown"),
    ],
)
def test_plans_that_do_not_need_the_live_read_survive_a_failed_read(store, factory, setup, plan):
    none = {t: _none(t) for t in MarkerType}
    rec = _known_file(store, none if setup in ("nothing_decided", "ours_left") else None)
    markers = {"enabled": False, "library_ids": None} if setup == "off" else None
    if setup == "ours_left":
        _published(store, rec, "plex", "rk-1", [INTRO])
    if setup == "versions_waiting":
        _published(store, rec, "plex", "rk-1", [INTRO], basis_for=[INTRO, CREDITS])
    registry = _registry(server_config("plex", ServerType.PLEX, markers=markers))
    registry.get("plex").get_markers.return_value = None
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), "plex")
    assert row["current"] is None
    assert row["plan"] == plan


# --------------------------------------------------------------------------- one failing server


class _Boom(RuntimeError):
    pass


_SECRET_IN_ERROR = f"token=abc123 path={PATH}"


@pytest.mark.parametrize("failure", ["get_publish_state", "get_item_publish_state", "get_publish_basis", "load_server"])
def test_one_failing_server_gives_a_degraded_row(store, factory, monkeypatch, failure):
    rec = _known_file(store)
    _published(store, rec, "plex", "rk-1", [INTRO, CREDITS], basis_for=[INTRO, CREDITS])
    _published(store, rec, "jf", "jf-item", [INTRO, CREDITS], basis_for=[INTRO, CREDITS])
    if failure == "load_server":
        real = inspect.load_server

        def load_server(raw, server_type):
            if server_type == "plex":
                raise _Boom(_SECRET_IN_ERROR)
            return real(raw, server_type)

        monkeypatch.setattr(inspect, "load_server", load_server)
    else:
        real = getattr(store, failure)

        def failing(*args):
            if "plex" in args:
                raise _Boom(_SECRET_IN_ERROR)
            return real(*args)

        monkeypatch.setattr(store, failure, failing)
    registry = _registry(server_config("plex", ServerType.PLEX), server_config("jf", ServerType.JELLYFIN))
    registry.get("plex").get_markers.return_value = _plex_rows(INTRO, CREDITS)
    registry.get("jf").get_media_segments.return_value = _jf_rows(INTRO, CREDITS)

    payload = inspect.item_payload(PATH, registry=registry, store=store)

    assert payload["known"] is True and payload["decisions"]["intro"]["status"] == "decided"
    assert _row(payload, "plex") == {
        "server_id": "plex",
        "server_name": "PLEX",
        "server_type": "plex",
        "markers_enabled": False,
        "capability_state": "unknown",
        "can_show": ["intro", "credits"],
        "current": None,
        "published": [],
        "publish_status": None,
        "publish_message": "",
        "item_status": None,
        "plan": "unknown",
        "plan_reason": "",
        "version_count": None,
        "error": "Couldn't read this server's Intro & Credits state (_Boom)",
    }
    jf = _row(payload, "jf")
    assert (jf["plan"], jf["capability_state"], jf["error"]) == ("up_to_date", "ready", None)
    assert [r["server_id"] for r in payload["servers"]] == ["plex", "jf"]


# --------------------------------------------------------------------------- capability cache


@pytest.fixture
def clock(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(inspect, "_CAPABILITY_CACHE", inspect.CapabilityCache(ttl_s=60.0, clock=lambda: now[0]))
    return now


def test_status_capability_is_reused_for_60_seconds(factory, clock):
    cfg = server_config("jf", ServerType.JELLYFIN)
    server = MagicMock()
    first = inspect.server_status_payload(server, cfg)["capability"]
    clock[0] += 59.9
    assert inspect.server_status_payload(server, cfg)["capability"] == first
    assert len(factory.calls) == 1
    clock[0] += 0.2
    inspect.server_status_payload(server, cfg)
    assert len(factory.calls) == 2


def test_cached_capability_is_a_copy(factory, clock):
    cfg = server_config("plex", ServerType.PLEX)
    factory.reports[ServerType.PLEX] = CapabilityReport(
        Capability.READY, "ok", {"plex_version": "1", "plex_pass": True}
    )
    for _ in range(2):  # a miss, then a hit: neither answer may change what the cache holds
        inspect.server_status_payload(MagicMock(), cfg)["capability"]["details"]["plex_version"] = "changed"
    assert inspect.server_status_payload(MagicMock(), cfg)["capability"]["details"]["plex_version"] == "1"
    assert len(factory.calls) == 1


def test_saved_server_settings_miss_the_cache(factory, clock):
    cfg = server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=False))
    inspect.server_status_payload(MagicMock(), cfg)
    saved = server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=True))
    inspect.server_status_payload(MagicMock(), saved)
    moved = server_config("plex", ServerType.PLEX, markers=_plex_markers(enabled=True))
    moved.output["plex_config_folder"] = "/elsewhere"
    inspect.server_status_payload(MagicMock(), moved)
    assert len(factory.calls) == 3
    inspect.server_status_payload(MagicMock(), moved)
    assert len(factory.calls) == 3


def test_cache_is_per_server_and_separate_for_status_and_inspector(store, factory, clock):
    plex, jf = server_config("plex", ServerType.PLEX), server_config("jf", ServerType.JELLYFIN)
    registry = _registry(plex, jf)
    inspect.item_payload(PATH, registry=registry, store=store)
    inspect.item_payload(PATH, registry=registry, store=store)
    assert [c["config"].id for c in factory.calls] == ["plex", "jf"]
    # The Edit tab checks as if on, the Inspector with the stored settings: never each other's answer.
    inspect.server_status_payload(registry.get("plex"), plex)
    assert [(c["config"].id, "settings" in c) for c in factory.calls] == [
        ("plex", False),
        ("jf", False),
        ("plex", True),
    ]
    clock[0] += 61
    inspect.item_payload(PATH, registry=registry, store=store)
    assert len(factory.calls) == 5


def test_concurrent_requests_check_a_server_once(monkeypatch, clock):
    import threading
    import time

    started = threading.Event()
    calls = []

    def slow_factory(server, config, **kwargs):
        pub = MagicMock()

        def capability():
            calls.append(config.id)
            started.set()
            time.sleep(0.05)
            return CapabilityReport(Capability.READY, "ok")

        pub.capability.side_effect = capability
        return pub

    monkeypatch.setattr(inspect, "publisher_for", slow_factory)
    cfg = server_config("jf", ServerType.JELLYFIN)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(inspect.server_status_payload(MagicMock(), cfg)["capability"]))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == ["jf"]
    assert len(results) == 5 and all(r["state"] == "ready" for r in results)


def test_clear_capability_cache(factory):
    cfg = server_config("jf", ServerType.JELLYFIN)
    inspect.server_status_payload(MagicMock(), cfg)
    inspect.clear_capability_cache()
    inspect.server_status_payload(MagicMock(), cfg)
    assert len(factory.calls) == 2


def test_clear_forgets_entries_and_locks():
    cache = inspect.CapabilityCache(clock=lambda: 0.0)
    for sid in ("a", "b"):
        cache.get(server_config(sid, ServerType.JELLYFIN), "status", lambda: "ready")
    assert set(cache._locks) == {("a", "status"), ("b", "status")}
    cache.clear()
    assert cache._entries == {} and cache._locks == {}


def test_fingerprint_change_drops_the_keys_lock():
    cache = inspect.CapabilityCache(clock=lambda: 0.0)
    before = server_config("a", ServerType.JELLYFIN, markers={"enabled": False, "library_ids": None})
    after = server_config("a", ServerType.JELLYFIN, markers={"enabled": True, "library_ids": None})
    other = server_config("b", ServerType.JELLYFIN)
    cache.get(before, "status", lambda: "old")
    cache.get(other, "status", lambda: "other")
    old_lock = cache._locks[("a", "status")]
    assert cache.get(after, "status", lambda: "new") == "new"
    assert ("a", "status") not in cache._locks  # evicted with its entry
    assert cache._entries[("a", "status")][2] == "new"
    assert ("b", "status") in cache._locks  # other keys keep theirs
    assert cache.get(after, "status", lambda: "unused") == "new"
    assert cache._locks[("a", "status")] is not old_lock


def test_failed_check_after_a_save_still_evicts_the_old_answer_and_lock():
    cache = inspect.CapabilityCache(clock=lambda: 0.0)
    before = server_config("a", ServerType.JELLYFIN, markers={"enabled": False, "library_ids": None})
    after = server_config("a", ServerType.JELLYFIN, markers={"enabled": True, "library_ids": None})
    cache.get(before, "status", lambda: "old")

    def failing():
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        cache.get(after, "status", failing)
    assert ("a", "status") not in cache._entries and ("a", "status") not in cache._locks
    # Saving the old settings back must check again, not revive the answer from before the save.
    assert cache.get(before, "status", lambda: "fresh") == "fresh"


# --------------------------------------------------------------------------- Plex keeps agreeing times (audit C MED-3)

INTRO_KEPT = Marker(T.INTRO, 60_000, 90_000, ("chapters",))
INTRO_DECIDED_LATER = Marker(T.INTRO, 60_000, 91_500, ("chapters", "theintrodb"))


def _intro_only(marker):
    return {
        T.INTRO: _decided(marker),
        T.CREDITS: _none(T.CREDITS),
        T.RECAP: _none(T.RECAP),
        T.PREVIEW: _none(T.PREVIEW),
    }


@pytest.mark.parametrize(
    ("stype", "ours", "decided", "current", "plan"),
    [
        # The Plex publisher keeps what is already ours when it agrees within 2 s: nothing will change.
        (ServerType.PLEX, [INTRO_KEPT], INTRO_DECIDED_LATER, INTRO_KEPT, "up_to_date"),
        # Plex's own marker isn't ours: it is replaced by the decided times.
        (ServerType.PLEX, [], INTRO_DECIDED_LATER, Marker(T.INTRO, 60_000, 93_000, ()), "will_replace"),
        # Ours no longer agrees with the decision (more than 2 s): it is replaced.
        (ServerType.PLEX, [INTRO_KEPT], Marker(T.INTRO, 60_000, 92_500, ()), INTRO_KEPT, "will_replace"),
        # Ours agrees but Plex shows something else now: compared with the decided times as before.
        (ServerType.PLEX, [INTRO_KEPT], INTRO_DECIDED_LATER, Marker(T.INTRO, 75_000, 110_000, ()), "will_replace"),
        # Plex shows the decided times, but the publisher writes the kept ones back: a write will happen.
        (ServerType.PLEX, [INTRO_KEPT], INTRO_DECIDED_LATER, INTRO_DECIDED_LATER, "will_replace"),
        # Jellyfin and Emby write the decided times whatever they had: no keep rule.
        (ServerType.JELLYFIN, [INTRO_KEPT], INTRO_DECIDED_LATER, INTRO_KEPT, "will_replace"),
        (ServerType.EMBY, [INTRO_KEPT], INTRO_DECIDED_LATER, INTRO_KEPT, "will_replace"),
    ],
    ids=[
        "plex-kept",
        "plex-own-marker",
        "plex-ours-disagrees",
        "plex-shows-other",
        "plex-shows-decided",
        "jellyfin-no-keep",
        "emby-no-keep",
    ],
)
def test_plan_mirrors_the_plex_keep_rule(store, factory, stype, ours, decided, current, plan):
    rec = _known_file(store, _intro_only(decided))
    sid = {ServerType.PLEX: "plex", ServerType.JELLYFIN: "jf", ServerType.EMBY: "emby"}[stype]
    if ours:
        _published(store, rec, sid, "item-1", ours, basis_for=[decided])
    registry = _registry(server_config(sid, stype))
    if stype is ServerType.PLEX:
        registry.get(sid).get_markers.return_value = _plex_rows(current)
    elif stype is ServerType.EMBY:
        registry.get(sid).get_chapter_markers.return_value = _emby_rows(current)
    else:
        registry.get(sid).get_media_segments.return_value = _jf_rows(current)
    row = _row(inspect.item_payload(PATH, registry=registry, store=store), sid)
    assert (row["plan"], row["plan_reason"]) == (plan, "")


# --------------------------------------------------------------------------- capability cache: short-lived problems


@pytest.mark.parametrize(
    ("variant", "ready", "problem"),
    [
        (
            "status",
            {"state": "ready", "message": "", "details": {}},
            {"state": "needs_plugin", "message": "", "details": {}},
        ),
        ("inspector", "ready", "unreachable"),
    ],
)
def test_answers_other_than_ready_are_reused_for_5_seconds_only(variant, ready, problem):
    now = [0.0]
    cache = inspect.CapabilityCache(ttl_s=60.0, clock=lambda: now[0])
    cfg, other = server_config("jf", ServerType.JELLYFIN), server_config("jf2", ServerType.JELLYFIN)
    computed = []

    def compute(answer):
        def run():
            computed.append(answer)
            return answer

        return run

    cache.get(cfg, variant, compute(problem))
    cache.get(other, variant, compute(ready))
    now[0] = 4.9
    assert cache.get(cfg, variant, compute(ready)) == problem
    now[0] = 5.1
    assert cache.get(cfg, variant, compute(ready)) == ready  # Jellyfin is back after its restart
    assert cache.get(other, variant, compute(problem)) == ready  # a ready answer still lasts 60 s
    now[0] = 59.0
    assert cache.get(cfg, variant, compute(problem)) == ready
    assert computed == [problem, ready, ready]


def test_a_ready_status_with_a_warning_is_reused_for_5_seconds_only():
    now = [0.0]
    cache = inspect.CapabilityCache(ttl_s=60.0, clock=lambda: now[0])
    cfg = server_config("plex", ServerType.PLEX)
    unchecked = {"state": "ready", "message": "", "details": {"plex_pass": None}, "warning": "Plex Pass unchecked"}
    checked = {"state": "ready", "message": "", "details": {"plex_pass": True}, "warning": ""}
    cache.get(cfg, "status", lambda: unchecked)
    now[0] = 4.9
    assert cache.get(cfg, "status", lambda: checked) == unchecked
    now[0] = 5.1
    assert cache.get(cfg, "status", lambda: checked) == checked  # Plex is back after its restart
    now[0] = 60.0
    assert cache.get(cfg, "status", lambda: unchecked) == checked


def test_forget_capability_drops_one_servers_answers():
    now = [0.0]
    cache = inspect.CapabilityCache(ttl_s=60.0, clock=lambda: now[0])
    jf, plex = server_config("jf", ServerType.JELLYFIN), server_config("plex", ServerType.PLEX)
    for cfg in (jf, plex):
        for variant in ("status", "inspector"):
            cache.get(cfg, variant, lambda: "ready")
    cache.forget("jf")
    assert set(cache._entries) == {("plex", "status"), ("plex", "inspector")}
    assert cache.get(jf, "inspector", lambda: "needs_plugin") == "needs_plugin"
    assert cache.get(plex, "inspector", lambda: "unused") == "ready"


def test_forget_capability_uses_the_shared_cache(factory):
    cfg = server_config("jf", ServerType.JELLYFIN)
    inspect.server_status_payload(MagicMock(), cfg)
    inspect.server_status_payload(MagicMock(), cfg)
    inspect.forget_capability("other")
    inspect.server_status_payload(MagicMock(), cfg)
    assert len(factory.calls) == 1
    inspect.forget_capability("jf")
    inspect.server_status_payload(MagicMock(), cfg)
    assert len(factory.calls) == 2


# --------------------------------------------------------------------------- season_payload


class TestSeasonPayload:
    @pytest.fixture
    def season(self, tmp_path, store):
        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        paths = []
        for e in (1, 2, 3):
            p = folder / f"Show (2020) - S01E{e:02d}.mkv"
            p.write_bytes(b"x")
            paths.append(str(p))
        (folder / "Show (2020) - S01E01-sample.mkv").write_bytes(b"x")
        root = str(tmp_path / "media")
        reg = _registry(
            server_config("plex-1", ServerType.PLEX, root=root),
            server_config("jf-1", ServerType.JELLYFIN, root=root, markers={"enabled": False, "library_ids": None}),
        )
        return SimpleNamespace(folder=str(folder), paths=paths, root=root, reg=reg, store=store)

    @staticmethod
    def _decide(store, path, intro=None, *, credits_review=False, evidence=()):
        st = os.stat(path)
        rec = store.upsert_file(
            FileIdentity(path, st.st_size, st.st_mtime_ns),
            duration_ms=DURATION,
            season_key=os.path.dirname(path),
            is_movie=False,
        )
        for source in (Source.SEASON_AUDIO, Source.SKIPDB):
            store.replace_evidence(rec.id, source, [c for c in evidence if c.source is source])
        server = [c for c in evidence if c.source is Source.SERVER_MARKERS]
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, server, origin="plex-1")
        credits = (
            TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None, Marker(T.CREDITS, 1_296_000, DURATION, ("skipdb",)), "disagree")
            if credits_review
            else _none(T.CREDITS)
        )  # fmt: skip
        store.save_decisions(
            rec.id,
            {T.INTRO: _decided(intro) if intro else _none(T.INTRO), T.CREDITS: credits},
            settings_fingerprint="f",
        )
        return rec

    def test_lists_the_folders_episodes_with_decisions_chips_dots_and_counts(self, season):
        intro = Marker(T.INTRO, 127_000, 157_000, ("season_audio", "skipdb"))
        audio = Candidate(T.INTRO, 127_000, 157_000, Source.SEASON_AUDIO, 1.0, "2/2")
        skip = Candidate(T.INTRO, 128_000, 157_500, Source.SKIPDB, 0.9)
        on_plex = Candidate(T.INTRO, 76_000, 112_000, Source.SERVER_MARKERS, 1.0)
        e1 = self._decide(season.store, season.paths[0], intro, evidence=(audio, skip, on_plex))
        season.store.set_publish_state(
            e1.id, "plex-1", item_id="7", markers=[intro], status="written", message="1 marker(s)"
        )
        e2 = self._decide(season.store, season.paths[1], intro, credits_review=True, evidence=(audio,))
        season.store.set_publish_state(
            e2.id, "plex-1", item_id="8", markers=None, status="waiting", message="Not in this server's library yet"
        )

        payload = inspect.season_payload(season.paths[1], registry=season.reg, store=season.store)

        assert payload["folder"] == season.folder
        assert (payload["show"], payload["season"]) == ("Show (2020) {tvdb-1}", "Season 01")
        assert payload["servers"] == [
            {"server_id": "plex-1", "server_name": "PLEX-1", "server_type": "plex", "markers_enabled": True},
            {"server_id": "jf-1", "server_name": "JF-1", "server_type": "jellyfin", "markers_enabled": False},
        ]
        eps = payload["episodes"]
        assert [e["episode"] for e in eps] == ["E01", "E02", "E03"]  # the -sample extra isn't listed
        assert [(e["path"], e["name"]) for e in eps] == [(p, os.path.basename(p)) for p in season.paths]
        assert eps[0]["intro"] == {
            "status": "decided",
            "reason": "agreed",
            "marker": {"type": "intro", "start_ms": 127_000, "end_ms": 157_000, "decided_by": ["season_audio", "skipdb"], "locked": False},
            "proposed": None,
        }  # fmt: skip
        assert (eps[0]["known"], eps[0]["duration_ms"]) == (True, DURATION)
        # Markers already on a server are the dots, not chips; only season audio carries its "2/2" label.
        assert eps[0]["evidence"] == [{"source": "season_audio", "label": "2/2"}, {"source": "skipdb", "label": ""}]
        assert eps[0]["servers"] == {
            "plex-1": {"state": "ok", "message": "1 marker(s)"},
            "jf-1": {"state": "off", "message": ""},
        }
        assert eps[1]["credits"] == {
            "status": "needs_review",
            "reason": "disagree",
            "marker": None,
            "proposed": {"start_ms": 1_296_000, "end_ms": DURATION},
        }
        assert eps[1]["servers"]["plex-1"] == {"state": "waiting", "message": "Not in this server's library yet"}
        assert (eps[2]["known"], eps[2]["duration_ms"], eps[2]["evidence"]) == (False, None, [])
        assert eps[2]["intro"] == {"status": None, "reason": "", "marker": None, "proposed": None}
        assert eps[2]["servers"]["plex-1"] == {"state": "none", "message": ""}
        assert payload["counts"] == {"episodes": 3, "ready": 1, "needs_review": 1}

    @pytest.mark.parametrize(
        ("status", "markers", "state"),
        [("written", [], "none"), ("failed", None, "failed"), ("skipped", None, "skipped")],
    )
    def test_dot_states(self, season, status, markers, state):
        rec = self._decide(season.store, season.paths[0])
        season.store.set_publish_state(rec.id, "plex-1", item_id="7", markers=markers, status=status, message="m")
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=season.store)
        assert payload["episodes"][0]["servers"]["plex-1"] == {"state": state, "message": "m"}
        # Known, but nothing decided: not ready.
        assert payload["counts"] == {"episodes": 3, "ready": 0, "needs_review": 0}

    def test_a_failed_publish_keeps_its_last_markers_but_is_failed(self, season):
        intro = Marker(T.INTRO, 127_000, 157_000, ("skipdb",))
        rec = self._decide(season.store, season.paths[0], intro)
        season.store.set_publish_state(rec.id, "plex-1", item_id="7", markers=[intro], status="written", message="1")
        season.store.set_publish_state(rec.id, "plex-1", item_id="7", markers=None, status="failed", message="boom")
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=season.store)
        assert payload["episodes"][0]["servers"]["plex-1"] == {"state": "failed", "message": "boom"}

    def test_a_server_turned_off_on_the_servers_page_isnt_listed(self, season):
        # Like the Inspector's episode tab: a disabled server owns nothing and is never shown.
        season.reg.configs_by_id["plex-1"] = server_config("plex-1", ServerType.PLEX, root=season.root, enabled=False)
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=season.store)
        assert [s["server_id"] for s in payload["servers"]] == ["jf-1"]
        assert {tuple(e["servers"]) for e in payload["episodes"]} == {("jf-1",)}

    def test_a_library_not_selected_for_intro_and_credits_is_off(self, season):
        markers = {"enabled": True, "library_ids": ["other"], "plex": {"db_write_confirmed_at": CONFIRMED}}
        season.reg.configs_by_id["plex-1"] = server_config("plex-1", ServerType.PLEX, root=season.root, markers=markers)
        rec = self._decide(season.store, season.paths[0])
        season.store.set_publish_state(rec.id, "plex-1", item_id="7", markers=[INTRO], status="written", message="1")
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=season.store)
        assert payload["servers"][0] == {
            "server_id": "plex-1", "server_name": "PLEX-1", "server_type": "plex", "markers_enabled": False,
        }  # fmt: skip
        assert {e["servers"]["plex-1"]["state"] for e in payload["episodes"]} == {"off"}

    def test_an_episode_a_server_excludes_is_off_there_only(self, season):
        excluded = [{"value": season.paths[1], "type": "path"}]
        season.reg.configs_by_id["plex-1"] = server_config(
            "plex-1", ServerType.PLEX, root=season.root, exclude_paths=excluded
        )
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=season.store)
        assert [e["servers"]["plex-1"]["state"] for e in payload["episodes"]] == ["none", "off", "none"]

    def test_chips_skip_markers_on_servers_and_types_the_view_doesnt_show(self, season):
        st = os.stat(season.paths[0])
        rec = season.store.upsert_file(
            FileIdentity(season.paths[0], st.st_size, st.st_mtime_ns), duration_ms=DURATION, season_key=None, is_movie=False
        )  # fmt: skip
        store = season.store
        store.replace_evidence(
            rec.id, Source.CHAPTERS, [Candidate(T.INTRO, 1_000, 30_000, Source.CHAPTERS, origin="Opening")]
        )
        hint = Candidate(T.INTRO, 1_000, 30_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4")
        store.replace_evidence(rec.id, Source.SEASON_AUDIO, [])
        store.replace_evidence(rec.id, Source.SEASON_AUDIO_PREVIOUS, [hint])
        store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.RECAP, 0, 9_000, Source.SKIPDB)])
        store.replace_evidence(rec.id, Source.THEINTRODB, [], detail="no data")
        imported = Candidate(T.INTRO, 1_000, 30_000, Source.SERVER_MARKERS_IMPORTED)
        store.replace_evidence(rec.id, Source.SERVER_MARKERS_IMPORTED, [imported], origin="jf-1")
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=store)
        # The chapter's title isn't a chip label, the previous season's hint keeps its "4/4".
        assert payload["episodes"][0]["evidence"] == [
            {"source": "chapters", "label": ""},
            {"source": "season_audio_previous", "label": "4/4"},
        ]
