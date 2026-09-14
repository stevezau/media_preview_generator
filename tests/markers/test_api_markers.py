"""Intro & Credits API for the UI: server status, plugin install, source usage, Inspector item data, re-detect."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.settings import SECRET_MASK
from tests.markers.conftest import api_headers as _api_headers

PLEX_TOKEN = "plex-token-SECRET-1234"
JF_KEY = "jf-api-key-SECRET-5678"
TIDB_KEY = "tidb-key-SECRET-9012"


@pytest.fixture
def media(tmp_path):
    root = tmp_path.resolve() / "media"
    (root / "tv" / "Show").mkdir(parents=True)
    (root / "movies" / "Film").mkdir(parents=True)
    (root / "tv" / "Show" / "S01E01.mkv").write_bytes(b"x")
    (root / "movies" / "Film" / "Film.mkv").write_bytes(b"x")
    (tmp_path / "secret.mkv").write_bytes(b"x")
    return root


@pytest.fixture
def servers(app, media, tmp_path):
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [
        {
            "id": "plex-1",
            "type": "plex",
            "name": "Plex",
            "enabled": True,
            "url": "http://127.0.0.1:9",
            "auth": {"method": "token", "token": PLEX_TOKEN},
            "libraries": [{"id": "1", "name": "TV Shows", "remote_paths": ["/data/tv"]}],
            "path_mappings": [{"remote_prefix": "/data/tv", "local_prefix": str(media / "tv")}],
            "output": {"plex_config_folder": str(tmp_path / "plexcfg")},
        },
        {
            "id": "jf-1",
            "type": "jellyfin",
            "name": "Jellyfin",
            "enabled": True,
            "url": "http://127.0.0.1:9",
            "auth": {"method": "api_key", "api_key": JF_KEY},
            "libraries": [{"id": "m", "name": "Movies", "remote_paths": [str(media / "movies")], "enabled": False}],
        },
        {
            "id": "emby-1",
            "type": "emby",
            "name": "Emby",
            "enabled": True,
            "url": "http://127.0.0.1:9",
            "auth": {"method": "api_key", "api_key": "emby-key"},
            "libraries": [],
        },
        {
            "id": "jf-off",
            "type": "jellyfin",
            "name": "Jellyfin (off)",
            "enabled": False,
            "url": "http://127.0.0.1:9",
            "auth": {"method": "api_key", "api_key": JF_KEY},
            "libraries": [{"id": "x", "name": "Other", "remote_paths": [str(tmp_path)]}],
        },
    ]
    get_settings_manager().set("media_servers", entries)
    return entries


def _secret_free(resp):
    text = resp.get_data(as_text=True)
    for secret in (PLEX_TOKEN, JF_KEY, TIDB_KEY):
        assert secret not in text
    return resp


# --------------------------------------------------------------------------- status


def test_status_for_an_unknown_server_is_404(client, servers):
    resp = client.get("/api/markers/servers/nope/status", headers=_api_headers())
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "server not found"}


@pytest.mark.parametrize(
    ("server_id", "client_class"), [("plex-1", "PlexServer"), ("jf-1", "JellyfinServer"), ("emby-1", "EmbyServer")]
)
def test_status_uses_that_servers_client_and_config(client, servers, monkeypatch, server_id, client_class):
    from media_preview_generator.markers import inspect

    calls = []

    def fake(server, config):
        calls.append((server, config))
        return {"server_id": config.id, "capability": {"state": "ready"}}

    monkeypatch.setattr(inspect, "server_status_payload", fake)
    resp = client.get(f"/api/markers/servers/{server_id}/status", headers=_api_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"server_id": server_id, "capability": {"state": "ready"}}
    [(server, config)] = calls
    assert config.id == server_id
    assert type(server).__name__ == client_class and server.id == server_id


def test_status_never_carries_the_servers_credentials(client, servers, monkeypatch):
    from media_preview_generator.markers import inspect

    monkeypatch.setattr(
        inspect,
        "server_status_payload",
        lambda server, config: {"capability": {"message": f"401 for url http://x/?X-Plex-Token={PLEX_TOKEN}"}},
    )
    resp = _secret_free(client.get("/api/markers/servers/plex-1/status", headers=_api_headers()))
    assert resp.get_json()["capability"]["message"] == "401 for url http://x/?X-Plex-Token=****"


def test_status_with_a_failing_capability_check_is_still_200(client, servers, monkeypatch):
    from media_preview_generator.markers import inspect

    def boom(server, config, **kwargs):
        raise RuntimeError(f"connect failed token={PLEX_TOKEN}")

    monkeypatch.setattr(inspect, "publisher_for", boom)
    resp = _secret_free(client.get("/api/markers/servers/plex-1/status", headers=_api_headers()))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["capability"] == {
        "state": "unknown",
        "message": "Couldn't check this server (RuntimeError)",
        "details": {},
        "warning": "",
    }
    assert body["server_id"] == "plex-1" and body["libraries"][0]["name"] == "TV Shows"


def test_status_crash_is_a_static_json_error(client, servers, monkeypatch):
    from media_preview_generator.markers import inspect

    def boom(raw, server_type):
        raise RuntimeError(f"bad block token={PLEX_TOKEN}")

    monkeypatch.setattr(inspect, "load_server", boom)
    resp = _secret_free(client.get("/api/markers/servers/plex-1/status", headers=_api_headers()))
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Couldn't check this server's Intro & Credits status"}


def test_status_real_payload_for_a_turned_off_server(client, servers):
    resp = client.get("/api/markers/servers/jf-off/status", headers=_api_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["capability"]["state"] == "disabled"
    assert body["libraries"] == [{"id": "x", "name": "Other", "kind": None, "default_selected": True}]


def test_the_duplicate_install_plugin_route_is_gone(client, servers):
    # The UI installs the Jellyfin plugin through POST /api/servers/<id>/install-plugin.
    resp = client.post("/api/markers/servers/jf-1/install-plugin", headers=_api_headers())
    assert resp.status_code in (404, 405)


# --------------------------------------------------------------------------- source usage


class _Limiter:
    def __init__(self, usage):
        self._usage = usage

    def usage(self):
        return dict(self._usage)


@pytest.fixture
def limiters(monkeypatch):
    from media_preview_generator.markers.sources import ratelimit

    table = {
        "theintrodb": _Limiter(
            {"day": "2026-09-14", "used": 3, "limit": None, "remaining": None, "blocked_until_s": 0.0}
        ),
        "introdb": _Limiter({"day": "2026-09-14", "used": 12, "limit": 1000, "remaining": 988, "blocked_until_s": 0.0}),
        "skipdb": _Limiter({"day": "2026-09-14", "used": 0, "limit": None, "remaining": None, "blocked_until_s": 0.0}),
    }
    asked = []
    monkeypatch.setattr(ratelimit, "get_limiter", lambda source_id: asked.append(source_id) or table[source_id])
    return asked


@pytest.mark.parametrize(("stored_key", "has_key"), [(TIDB_KEY, True), ("", False), (None, False)])
def test_source_usage(client, limiters, loguru_caplog, stored_key, has_key):
    from media_preview_generator.markers.store import get_marker_store
    from media_preview_generator.web.settings_manager import get_settings_manager

    source = {"id": "theintrodb", "enabled": True}
    if stored_key is not None:
        source["api_key"] = stored_key
    get_settings_manager().set("markers", {"sources": [source]})
    store = get_marker_store()
    store.record_source_usage("theintrodb", day="2026-09-14", used=40, limit=500, remaining=460)
    store.record_source_usage("introdb", day="2026-09-14", used=5, limit=900, remaining=895)
    store.record_source_usage("skipdb", day="2026-09-13", used=7, limit=50, remaining=43)  # yesterday: not shown

    resp = _secret_free(client.get("/api/markers/sources/usage", headers=_api_headers()))

    assert resp.status_code == 200
    assert resp.get_json() == {
        "theintrodb": {"day": "2026-09-14", "used": 40, "limit": 500, "remaining": 460, "has_key": has_key},
        "introdb": {"day": "2026-09-14", "used": 12, "limit": 1000, "remaining": 988, "has_key": False},
        "skipdb": {"day": "2026-09-14", "used": 0, "limit": None, "remaining": None, "has_key": False},
    }
    assert SECRET_MASK not in resp.get_data(as_text=True)
    assert sorted(limiters) == ["introdb", "skipdb", "theintrodb"]
    assert TIDB_KEY not in loguru_caplog.text


# --------------------------------------------------------------------------- item


@pytest.fixture
def item_calls(monkeypatch):
    from media_preview_generator.markers import inspect

    calls = []

    def fake(canonical_path, *, registry, store):
        calls.append({"path": canonical_path, "registry": registry, "store": store})
        return {"known": False, "canonical_path": canonical_path, "note": f"token {PLEX_TOKEN}"}

    monkeypatch.setattr(inspect, "item_payload", fake)
    return calls


def test_item_by_path(client, servers, media, item_calls):
    from media_preview_generator.markers.store import get_marker_store

    episode = str(media / "tv" / "Show" / "S01E01.mkv")
    resp = _secret_free(client.get("/api/markers/item", query_string={"path": f" {episode} "}, headers=_api_headers()))
    assert resp.status_code == 200
    assert resp.get_json() == {"known": False, "canonical_path": episode, "note": "token ****"}
    [call] = item_calls
    assert call["path"] == episode
    assert [c.id for c in call["registry"].configs()] == ["plex-1", "jf-1", "emby-1", "jf-off"]
    assert call["store"] is get_marker_store()


def test_item_in_a_library_with_previews_off_is_allowed(client, servers, media, item_calls):
    film = str(media / "movies" / "Film" / "Film.mkv")
    resp = client.get("/api/markers/item", query_string={"path": film}, headers=_api_headers())
    assert resp.status_code == 200
    assert item_calls[0]["path"] == film


@pytest.mark.parametrize(
    "path",
    [
        "{tmp}/secret.mkv",  # exists, but in no library (only the turned-off server's library holds it)
        "{media}/tv/../../secret.mkv",
        "{media}/tv/Show/missing.mkv",
        "{media}/tv/Show",
        "{media}/tv/Show/S01E01.mkv\x00",
        "tv/Show/S01E01.mkv",
        "/etc/passwd",
        "",
    ],
)
def test_item_path_outside_every_library_is_400(client, servers, media, tmp_path, item_calls, path):
    raw = path.format(media=media, tmp=tmp_path.resolve())
    resp = client.get("/api/markers/item", query_string={"path": raw} if raw else {}, headers=_api_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"]
    assert item_calls == []


def test_item_path_outside_the_media_root_is_400(client, servers, media, tmp_path, item_calls, monkeypatch):
    from media_preview_generator.web.routes import api_markers

    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(api_markers, "MEDIA_ROOT", str(tmp_path / "elsewhere"))
    resp = client.get(
        "/api/markers/item", query_string={"path": str(media / "tv" / "Show" / "S01E01.mkv")}, headers=_api_headers()
    )
    assert resp.status_code == 400
    assert item_calls == []


@pytest.fixture
def resolve_calls(monkeypatch):
    from media_preview_generator.markers import inspect

    calls = []
    answers = {}

    def fake(server, config, item_id):
        calls.append({"server": server, "config": config, "item_id": item_id})
        return answers.get((config.id, item_id))

    monkeypatch.setattr(inspect, "resolve_local_path", fake)
    return calls, answers


@pytest.mark.parametrize(("server_id", "client_class"), [("plex-1", "PlexServer"), ("jf-1", "JellyfinServer")])
def test_item_by_server_item(client, servers, media, item_calls, resolve_calls, server_id, client_class):
    calls, answers = resolve_calls
    episode = str(media / "tv" / "Show" / "S01E01.mkv")
    answers[(server_id, "42")] = episode
    resp = client.get(
        "/api/markers/item", query_string={"server_id": server_id, "item_id": "42"}, headers=_api_headers()
    )
    assert resp.status_code == 200
    [call] = calls
    assert call["config"].id == server_id and call["item_id"] == "42"
    assert type(call["server"]).__name__ == client_class and call["server"].id == server_id
    assert item_calls[0]["path"] == episode


def test_item_by_server_item_resolving_to_nothing_is_404(client, servers, item_calls, resolve_calls):
    resp = client.get(
        "/api/markers/item", query_string={"server_id": "plex-1", "item_id": "404"}, headers=_api_headers()
    )
    assert resp.status_code == 404
    assert item_calls == []


def test_item_by_server_item_outside_every_library_is_400(client, servers, tmp_path, item_calls, resolve_calls):
    _calls, answers = resolve_calls
    answers[("plex-1", "7")] = str(tmp_path.resolve() / "secret.mkv")
    resp = client.get("/api/markers/item", query_string={"server_id": "plex-1", "item_id": "7"}, headers=_api_headers())
    assert resp.status_code == 400
    assert item_calls == []


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ({"server_id": "nope", "item_id": "1"}, 404),
        ({"server_id": "plex-1"}, 400),
        ({"item_id": "1"}, 400),
        ({"server_id": "jf-off", "item_id": "1"}, 409),
    ],
)
def test_item_by_server_item_refusals(client, servers, item_calls, resolve_calls, query, status):
    calls, _answers = resolve_calls
    resp = client.get("/api/markers/item", query_string=query, headers=_api_headers())
    assert resp.status_code == status
    assert resp.get_json()["error"]
    assert calls == [] and item_calls == []


@pytest.mark.parametrize(
    ("server_id", "item_id", "status"),
    [
        ("plex-1", "42", 404),
        # Plex's resolver takes a bare rating key (int(item_id)): the metadata key form would only ever 404.
        ("plex-1", "/library/metadata/42", 400),
        ("plex-1", "-42", 400),
        ("plex-1", " 42", 400),
        ("plex-1", "abc", 400),
        ("plex-1", "42?x=1", 400),
        ("plex-1", "../../library/sections", 400),
        ("jf-1", "0123456789abcdef0123456789ABCDEF", 404),
        ("jf-1", "01234567-89ab-cdef-0123-456789abcdef", 404),
        ("jf-1", "1234", 404),
        ("jf-1", "../../System/Configuration?x=", 400),
        ("jf-1", "abc xyz", 400),
        ("jf-1", "g123", 400),
        ("jf-1", "a" * 37, 400),
        ("emby-1", "5678", 404),
        ("emby-1", "5678/../../System/Info", 400),
    ],
)
def test_item_by_server_item_checks_the_item_id_shape(
    client, servers, item_calls, resolve_calls, server_id, item_id, status
):
    # 404 = the id was accepted and looked up (the fake resolves nothing); 400 = refused before any lookup.
    calls, _answers = resolve_calls
    resp = client.get(
        "/api/markers/item", query_string={"server_id": server_id, "item_id": item_id}, headers=_api_headers()
    )
    assert resp.status_code == status
    assert resp.get_json()["error"]
    if status == 400:
        assert calls == []
    else:
        assert [c["item_id"] for c in calls] == [item_id]
    assert item_calls == []


def test_item_payload_crash_is_a_json_error_without_details(client, servers, media, monkeypatch):
    from media_preview_generator.markers import inspect

    def boom(canonical_path, *, registry, store):
        raise RuntimeError(f"db locked at {canonical_path} token={PLEX_TOKEN}")

    monkeypatch.setattr(inspect, "item_payload", boom)
    episode = str(media / "tv" / "Show" / "S01E01.mkv")
    resp = _secret_free(client.get("/api/markers/item", query_string={"path": episode}, headers=_api_headers()))
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Couldn't build the Intro & Credits data for this file"}
    assert episode not in resp.get_data(as_text=True)


def test_one_failing_server_still_returns_the_other_rows(client, servers, media, monkeypatch):
    from media_preview_generator.markers import inspect
    from media_preview_generator.markers.publishers.base import Capability, CapabilityReport
    from media_preview_generator.markers.store import MarkerStore
    from media_preview_generator.servers.jellyfin import JellyfinServer
    from media_preview_generator.servers.plex import PlexServer
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [dict(e) for e in servers]
    entries[1]["libraries"] = [{"id": "t", "name": "TV", "remote_paths": [str(media / "tv")]}]
    entries[1]["markers"] = {"enabled": True, "library_ids": None}
    get_settings_manager().set("media_servers", entries)
    publisher = MagicMock()
    publisher.capability.return_value = CapabilityReport(Capability.READY, "ok")
    monkeypatch.setattr(inspect, "publisher_for", lambda server, cfg, **kw: publisher)
    monkeypatch.setattr(inspect, "read_server_markers", lambda server, cfg, item_id, include_ours: [])
    monkeypatch.setattr(PlexServer, "resolve_remote_path_to_item_id", lambda self, path, *, library_ids: "1")
    monkeypatch.setattr(JellyfinServer, "resolve_remote_path_to_item_id", lambda self, path, *, library_ids: "2")
    real_state = MarkerStore.get_publish_state

    def get_publish_state(self, file_id, server_id):
        if server_id == "plex-1":
            raise RuntimeError(f"disk I/O error token={PLEX_TOKEN}")
        return real_state(self, file_id, server_id)

    monkeypatch.setattr(MarkerStore, "get_publish_state", get_publish_state)
    from media_preview_generator.markers.models import FileIdentity
    from media_preview_generator.markers.store import get_marker_store

    episode = str(media / "tv" / "Show" / "S01E01.mkv")
    get_marker_store().upsert_file(FileIdentity(episode, 1, 1), duration_ms=1_000_000, season_key=None, is_movie=False)

    resp = _secret_free(client.get("/api/markers/item", query_string={"path": episode}, headers=_api_headers()))

    assert resp.status_code == 200
    rows = {r["server_id"]: r for r in resp.get_json()["servers"]}
    assert (rows["plex-1"]["plan"], rows["plex-1"]["capability_state"]) == ("unknown", "unknown")
    assert rows["plex-1"]["error"] == "Couldn't read this server's Intro & Credits state (RuntimeError)"
    assert (rows["jf-1"]["plan"], rows["jf-1"]["capability_state"], rows["jf-1"]["error"]) == (
        "nothing_to_publish",
        "ready",
        None,
    )


# --------------------------------------------------------------------------- redetect


@pytest.fixture
def created(monkeypatch):
    from media_preview_generator.markers import triggers

    calls = []

    class _Job:
        id = "job-123"

    monkeypatch.setattr(triggers, "create_intro_credits_job", lambda **kw: calls.append(kw) or _Job())
    return calls


def test_redetect_creates_a_forced_high_priority_single_file_job(client, servers, media, created):
    episode = str(media / "tv" / "Show" / "S01E01.mkv")
    resp = client.post("/api/markers/item/redetect", json={"path": episode}, headers=_api_headers())
    assert resp.status_code == 202
    assert resp.get_json() == {"job_id": "job-123"}
    assert created == [
        {
            "library_name": "Intro & Credits: S01E01.mkv",
            "priority": 1,
            "source": "inspector",
            "file_paths": [episode],
            "force": True,
        }
    ]


def test_redetect_clicked_again_while_queued_returns_the_same_job(client, servers, media, tmp_path, monkeypatch):
    from media_preview_generator.markers import triggers
    from media_preview_generator.web.jobs import JobManager

    jm = JobManager(config_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
    body = {"path": str(media / "tv" / "Show" / "S01E01.mkv")}

    first = client.post("/api/markers/item/redetect", json=body, headers=_api_headers())
    second = client.post("/api/markers/item/redetect", json=body, headers=_api_headers())

    assert (first.status_code, second.status_code) == (202, 202)
    assert second.get_json() == first.get_json()
    (job,) = jm.get_all_jobs()
    assert job.id == first.get_json()["job_id"] and job.priority == 1


@pytest.mark.parametrize(
    "body",
    [
        {"path": "{tmp}/secret.mkv"},
        {"path": "{media}/tv/../../secret.mkv"},
        {"path": "{media}/tv/Show"},
        {"path": 7},
        {},
    ],
)
def test_redetect_invalid_path_is_400(client, servers, media, tmp_path, created, body):
    if isinstance(body.get("path"), str):
        body = {"path": body["path"].format(media=media, tmp=tmp_path.resolve())}
    resp = client.post("/api/markers/item/redetect", json=body, headers=_api_headers())
    assert resp.status_code == 400
    assert created == []


def test_redetect_non_object_body_is_400(client, servers, created):
    resp = client.post("/api/markers/item/redetect", data="[1]", headers=_api_headers())
    assert resp.status_code == 400
    assert created == []


def test_redetect_refuses_when_config_is_unwritable(client, servers, media, created, monkeypatch):
    import media_preview_generator.web.config_health as config_health

    monkeypatch.setattr(
        config_health, "probe_config_health", lambda config_dir: {"writable": False, "detail": "ro", "hint": "fix"}
    )
    resp = client.post(
        "/api/markers/item/redetect", json={"path": str(media / "tv" / "Show" / "S01E01.mkv")}, headers=_api_headers()
    )
    assert resp.status_code == 503
    assert created == []


# --------------------------------------------------------------------------- auth


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/api/markers/servers/plex-1/status"),
        ("get", "/api/markers/sources/usage"),
        ("get", "/api/markers/item?path=/x"),
        ("post", "/api/markers/item/redetect"),
    ],
)
def test_every_route_needs_authentication(app, servers, created, method, url):
    resp = getattr(app.test_client(), method)(url, json={"path": "/x"} if method == "post" else None)
    assert resp.status_code == 401
    assert created == []


def test_bearer_token_is_accepted(app, servers, monkeypatch):
    from media_preview_generator.markers import inspect

    monkeypatch.setattr(inspect, "server_status_payload", lambda server, config: {"ok": True})
    resp = app.test_client().get("/api/markers/servers/plex-1/status", headers=_api_headers())
    assert resp.status_code == 200


# --------------------------------------------------------------------------- plugin install forgets the capability


@pytest.mark.parametrize("route", ["install-plugin", "uninstall-plugin"])
@pytest.mark.parametrize("outcome", ["ok", "raises"])
def test_plugin_install_routes_forget_the_cached_capability(client, servers, monkeypatch, route, outcome):
    from media_preview_generator.markers import inspect
    from media_preview_generator.servers.base import ServerType
    from media_preview_generator.servers.jellyfin import JellyfinServer
    from tests.markers.fakes import server_config

    def plugin_call(self):
        if outcome == "raises":
            raise RuntimeError("Jellyfin said no")
        return {"ok": True, "steps": [], "error": ""}

    monkeypatch.setattr(JellyfinServer, route.replace("-", "_"), plugin_call)
    jf, plex = server_config("jf-1", ServerType.JELLYFIN), server_config("plex-1", ServerType.PLEX)
    for cfg in (jf, plex):
        inspect._CAPABILITY_CACHE.get(cfg, "status", lambda: {"state": "ready", "message": "", "details": {}})
    resp = client.post(f"/api/servers/jf-1/{route}", headers=_api_headers())
    assert resp.status_code == 200
    assert set(inspect._CAPABILITY_CACHE._entries) == {("plex-1", "status")}
