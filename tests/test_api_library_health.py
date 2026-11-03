"""Library health API routes."""

from __future__ import annotations

import pytest

from media_preview_generator.library_health.models import CheckProgress, Feature, ServerResult, TodoFile
from media_preview_generator.library_health.runner import RereadRefusal


def _server(server_id: str, name: str, type_: str) -> ServerResult:
    return ServerResult(
        server_id=server_id, name=name, type=type_, libraries=[], todo=[], checked_at=5.0, duration_s=2.0
    )


class FakeRunner:
    def __init__(self) -> None:
        self.reread_result: tuple[bool, object] = (False, RereadRefusal("Nothing to re-read", 400))
        self.running: CheckProgress | None = None
        self.failed: list[tuple[str, str, str, str]] = []

    def server_error(self, server_id: str) -> str:
        return "boom" if server_id == "b" else ""

    def failed_servers(self):
        return self.failed

    def progress(self):
        return self.running

    def last_error(self) -> str:
        return "last"

    def start_check(self, reason: str = "manual"):
        return True, CheckProgress(kind="check", started_at=1.0, reason=reason)

    def cancel(self) -> bool:
        return True

    def start_reread(self, server_id: str):
        return self.reread_result


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chapter_todo: list[TodoFile] = []

    def load(self):
        return [
            _server("j", "Zed", "jellyfin"),
            _server("b", "beta", "emby"),
            _server("p2", "Zulu", "plex"),
            _server("p1", "alpha", "plex"),
        ]

    def todo_for(self, server_id, library_id, feature):
        self.calls.append({"todo_for": (server_id, library_id, feature)})
        return self.chapter_todo

    def list_todo(self, server_id, library_id, feature, **kwargs):
        self.calls.append({"server_id": server_id, "library_id": library_id, "feature": feature, **kwargs})
        return 1, [TodoFile(feature=feature, path="/m/a.mkv", title="A")]


@pytest.fixture
def fakes(monkeypatch):
    from media_preview_generator.web.routes import api_library_health as mod

    runner, store = FakeRunner(), FakeStore()
    monkeypatch.setattr(mod, "get_runner", lambda: runner)
    monkeypatch.setattr(mod, "default_store", lambda: store)
    return runner, store


ROUTES = [
    ("get", "/api/library-health"),
    ("post", "/api/library-health/check"),
    ("post", "/api/library-health/cancel"),
    ("get", "/api/library-health/files"),
    ("post", "/api/library-health/plex-reread"),
    ("post", "/api/library-health/chapters-job"),
]


@pytest.mark.parametrize(("method", "url"), ROUTES)
def test_auth_required(client, fakes, method, url):
    assert getattr(client, method)(url, headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_get_shape_sorted_with_errors(client, auth_headers, fakes, monkeypatch):
    from media_preview_generator.web.routes import api_library_health as mod

    monkeypatch.setattr(mod, "job_next_run", lambda _id: None)
    body = client.get("/api/library-health", headers=auth_headers).get_json()
    assert [s["server_id"] for s in body["servers"]] == ["p1", "p2", "b", "j"]
    assert {s["server_id"]: s["error"] for s in body["servers"]}["b"] == "boom"
    assert body["servers"][0]["checked_at"] == 5.0
    assert body["servers"][0]["duration_s"] == 2.0
    assert body["running"] is None
    assert body["last_error"] == "last"
    assert body["path_job_limit"] == 1000
    assert body["next_nightly_at"] is None


def test_get_adds_stub_for_failed_server_without_stored_row(client, auth_headers, fakes, monkeypatch):
    from media_preview_generator.web.routes import api_library_health as mod

    monkeypatch.setattr(mod, "job_next_run", lambda _id: None)
    runner, _store = fakes
    runner.failed = [("b", "beta", "emby", "boom"), ("new", "Fresh", "emby", "unreachable")]
    servers = client.get("/api/library-health", headers=auth_headers).get_json()["servers"]
    assert [s["server_id"] for s in servers].count("b") == 1
    stub = next(s for s in servers if s["server_id"] == "new")
    assert (stub["name"], stub["type"], stub["error"], stub["libraries"]) == ("Fresh", "emby", "unreachable", [])


def test_get_reports_configured_server_count(client, auth_headers, fakes, monkeypatch):
    from media_preview_generator.web.routes import api_library_health as mod

    monkeypatch.setattr(mod, "job_next_run", lambda _id: None)
    monkeypatch.setattr(mod, "_configured_server_count", lambda: 0)
    body = client.get("/api/library-health", headers=auth_headers).get_json()
    assert body["servers_configured"] == 0


def test_get_reports_next_nightly_with_timezone(client, auth_headers, fakes, monkeypatch):
    from datetime import UTC, datetime

    from media_preview_generator.web.routes import api_library_health as mod

    monkeypatch.setattr(mod, "job_next_run", lambda _id: datetime(2026, 10, 11, 3, 0, tzinfo=UTC))
    body = client.get("/api/library-health", headers=auth_headers).get_json()
    assert body["next_nightly_at"] == "2026-10-11T03:00:00+00:00"


def test_check_returns_202(client, auth_headers, fakes):
    resp = client.post("/api/library-health/check", headers=auth_headers)
    assert resp.status_code == 202
    assert resp.get_json()["started"] is True


def test_cancel(client, auth_headers, fakes):
    resp = client.post("/api/library-health/cancel", headers=auth_headers)
    assert resp.get_json() == {"cancelled": True}


def test_files_returns_page(client, auth_headers, fakes):
    _, store = fakes
    resp = client.get(
        "/api/library-health/files?server_id=s&library_id=1&feature=previews&q=a&not_showing=1",
        headers=auth_headers,
    )
    assert resp.get_json() == {"total": 1, "files": [{"path": "/m/a.mkv", "title": "A"}]}
    call = store.calls[0]
    assert call["feature"] == Feature("previews")
    assert call["not_showing"] is True
    assert call["limit"] == 100


@pytest.mark.parametrize(
    "query",
    [
        "server_id=s&library_id=1&feature=nope",
        "library_id=1&feature=previews",
        "server_id=s&feature=previews",
        "server_id=s&library_id=1&feature=previews&offset=-1",
        "server_id=s&library_id=1&feature=previews&offset=x",
        "server_id=s&library_id=1&feature=previews&not_showing=2",
        "server_id=s&library_id=1&feature=previews&q=" + "a" * 201,
    ],
)
def test_files_rejects_bad_params(client, auth_headers, fakes, query):
    assert client.get(f"/api/library-health/files?{query}", headers=auth_headers).status_code == 400


@pytest.mark.parametrize(("raw", "expected"), [("9999", 500), ("0", 1)])
def test_files_clamps_limit(client, auth_headers, fakes, raw, expected):
    _, store = fakes
    client.get(f"/api/library-health/files?server_id=s&library_id=1&feature=previews&limit={raw}", headers=auth_headers)
    assert store.calls[0]["limit"] == expected


@pytest.mark.parametrize(
    ("refusal", "status"),
    [
        (RereadRefusal("A check or re-read is already running", 409), 409),
        (RereadRefusal("Nothing to re-read", 400), 400),
        (RereadRefusal("Only Plex servers can be asked to re-read", 400), 400),
        (RereadRefusal("That server isn't available", 404), 404),
    ],
)
def test_reread_refusal_maps_to_its_own_status(client, auth_headers, fakes, refusal, status):
    runner, _ = fakes
    runner.reread_result = (False, refusal)
    resp = client.post("/api/library-health/plex-reread", json={"server_id": "p1"}, headers=auth_headers)
    assert resp.status_code == status
    assert resp.get_json() == {"error": refusal.message}


def test_reread_started(client, auth_headers, fakes):
    runner, _ = fakes
    runner.reread_result = (True, CheckProgress(kind="reread", started_at=1.0, reason="manual"))
    resp = client.post("/api/library-health/plex-reread", json={"server_id": "p1"}, headers=auth_headers)
    assert resp.status_code == 202


@pytest.fixture
def chapter_server(monkeypatch):
    from media_preview_generator.library_health import runner as runner_mod
    from media_preview_generator.servers.base import Library, ServerConfig, ServerType

    cfg = ServerConfig(
        id="p1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://x",
        auth={},
        libraries=[Library(id="2", name="TV Shows", remote_paths=(), enabled=True, kind="show")],
        output={"chapter_thumbnails": True},
    )
    started: list[tuple] = []
    monkeypatch.setattr(runner_mod, "_settings_registry", lambda: type("R", (), {"configs": lambda self: [cfg]})())
    monkeypatch.setattr(runner_mod, "start_files_job", lambda *args: started.append(args))
    return cfg, started


def test_chapters_job_starts_preview_jobs_with_item_ids(client, auth_headers, fakes, chapter_server):
    _, store = fakes
    cfg, started = chapter_server
    store.chapter_todo = [
        TodoFile(Feature.CHAPTERS, "/tv/a.mkv", "A", "11"),
        TodoFile(Feature.CHAPTERS, "/tv/b.mkv", "B", "12"),
    ]
    resp = client.post(
        "/api/library-health/chapters-job", json={"server_id": "p1", "library_id": "2"}, headers=auth_headers
    )
    assert resp.status_code == 202 and resp.get_json() == {"started": True, "files": 2}
    assert store.calls == [{"todo_for": ("p1", "2", Feature.CHAPTERS)}]
    assert started == [(cfg, [("/tv/a.mkv", "11"), ("/tv/b.mkv", "12")], "Chapter thumbnails for TV Shows")]


@pytest.mark.parametrize(
    "body,status",
    [
        ({"server_id": "p1"}, 400),
        ({"server_id": "nope", "library_id": "2"}, 404),
        ({"server_id": "p1", "library_id": "2"}, 400),
    ],
)
def test_chapters_job_refusals(client, auth_headers, fakes, chapter_server, body, status):
    _, started = chapter_server
    resp = client.post("/api/library-health/chapters-job", json=body, headers=auth_headers)
    assert resp.status_code == status
    assert started == []


def test_chapters_job_refused_when_chapters_off(client, auth_headers, fakes, chapter_server):
    _, store = fakes
    cfg, started = chapter_server
    cfg.output["chapter_thumbnails"] = False
    store.chapter_todo = [TodoFile(Feature.CHAPTERS, "/tv/a.mkv", "A", "11")]
    resp = client.post(
        "/api/library-health/chapters-job", json={"server_id": "p1", "library_id": "2"}, headers=auth_headers
    )
    assert resp.status_code == 400 and "off" in resp.get_json()["error"]
    assert started == []


def test_get_reports_whether_the_nightly_check_is_on(client, auth_headers, fakes, monkeypatch):
    from media_preview_generator.web.routes import api_library_health as mod

    monkeypatch.setattr(mod, "job_next_run", lambda _id: None)
    monkeypatch.setattr(
        mod, "load_builtin_jobs", lambda: {"library_health": {"enabled": False, "time": "03:00", "catch_up": True}}
    )
    assert client.get("/api/library-health", headers=auth_headers).get_json()["nightly_enabled"] is False
    monkeypatch.setattr(
        mod, "load_builtin_jobs", lambda: {"library_health": {"enabled": True, "time": "03:00", "catch_up": True}}
    )
    assert client.get("/api/library-health", headers=auth_headers).get_json()["nightly_enabled"] is True
