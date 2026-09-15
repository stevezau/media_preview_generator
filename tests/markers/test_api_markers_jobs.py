"""POST /api/markers/jobs and /api/markers/reconcile — start Intro & Credits jobs from the UI or the API."""

from types import SimpleNamespace

import pytest

from tests.markers.conftest import api_headers as _api_headers


class _FakeJob:
    def __init__(self, kw):
        self.kw = kw

    def to_dict(self):
        return {"id": "x", "kind": "intro_credits", "priority": self.kw["priority"]}


@pytest.fixture
def created(monkeypatch):
    from media_preview_generator.markers import triggers

    calls = []
    monkeypatch.setattr(triggers, "create_intro_credits_job", lambda **kw: calls.append(kw) or _FakeJob(kw))
    return calls


@pytest.fixture
def media(tmp_path):
    root = tmp_path.resolve() / "media"
    (root / "tv").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"libraries": [{"server_id": "plex-1", "library_id": "1"}]},
            {
                "library_name": "Intro & Credits: 1 library",
                "priority": 3,
                "source": "manual",
                "libraries": [{"server_id": "plex-1", "library_id": "1"}],
                "file_paths": [],
                "force": False,
            },
        ),
        (
            {
                "libraries": [{"server_id": "plex-1", "library_id": 1}, {"server_id": "jf-1", "library_id": "abc"}],
                "priority": 2,
                "force": True,
                "library_name": "TV everywhere",
            },
            {
                "library_name": "TV everywhere",
                "priority": 2,
                "source": "manual",
                "libraries": [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "jf-1", "library_id": "abc"}],
                "file_paths": [],
                "force": True,
            },
        ),
        (
            {},  # every library on servers with Intro & Credits on
            {
                "library_name": "Intro & Credits: all libraries",
                "priority": 3,
                "source": "manual",
                "libraries": [],
                "file_paths": [],
                "force": False,
            },
        ),
        (
            {"libraries": [], "priority": "LOW", "force": "false"},
            {
                "library_name": "Intro & Credits: all libraries",
                "priority": 3,
                "source": "manual",
                "libraries": [],
                "file_paths": [],
                "force": False,
            },
        ),
    ],
)
def test_create_marker_job_for_libraries(client, created, body, expected):
    resp = client.post("/api/markers/jobs", json=body, headers=_api_headers())
    assert resp.status_code == 201, resp.get_json()
    assert created == [expected]
    assert resp.get_json() == {"id": "x", "kind": "intro_credits", "priority": expected["priority"]}


@pytest.mark.parametrize(("priority", "expected"), [(1, 1), (3, 3), ("high", 1), ("normal", 2), (None, 3)])
def test_create_marker_job_for_files(client, created, media, priority, expected):
    episode = media / "tv" / "a.mkv"
    body = {"file_paths": [f"  {episode}  ", str(media / "tv")], "force": True}
    if priority is not None:
        body["priority"] = priority
    resp = client.post("/api/markers/jobs", json=body, headers=_api_headers())
    assert resp.status_code == 201, resp.get_json()
    assert created == [
        {
            "library_name": "Intro & Credits: 2 files",
            "priority": expected,
            "source": "manual",
            "libraries": [],
            "file_paths": [str(episode), str(media / "tv")],
            "force": True,
        }
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"libraries": [{"library_id": "1"}]},
        {"libraries": [{"server_id": "plex-1"}]},
        {"libraries": [{"server_id": "", "library_id": "1"}]},
        {"libraries": ["1"]},
        {"libraries": "1"},
        {"file_paths": "x"},
        {"file_paths": ["  "]},
        {"file_paths": [7]},
        {"libraries": [{"server_id": "a", "library_id": "1"}], "file_paths": ["/x"]},
        {"priority": "urgent"},
        {"priority": 0},
        {"priority": True},
        {"priority": "1"},
        {"library_name": 5},
        {"file_paths": ["/media/a\x00.mkv"]},
    ],
)
def test_create_marker_job_rejects_bad_bodies(client, created, body):
    resp = client.post("/api/markers/jobs", json=body, headers=_api_headers())
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["error"]
    assert created == []


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        ('{"libraries": [{"server_id": "plex-1", "library_id": "1"}]}', None),  # curl -d without a JSON type
        ('{"libraries": [],}', "application/json"),  # trailing comma
        ("libraries=1", "application/x-www-form-urlencoded"),
    ],
)
def test_body_that_is_not_json_is_rejected_instead_of_starting_every_library(client, created, data, content_type):
    headers = {"Authorization": _api_headers()["Authorization"]}
    if content_type:
        headers["Content-Type"] = content_type
    resp = client.post("/api/markers/jobs", data=data, headers=headers)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "The request body must be JSON"}
    assert created == []


@pytest.mark.parametrize("content_type", [None, "application/json"])
def test_empty_body_means_every_library(client, created, content_type):
    headers = {"Authorization": _api_headers()["Authorization"]}
    if content_type:
        headers["Content-Type"] = content_type
    resp = client.post("/api/markers/jobs", data="", headers=headers)
    assert resp.status_code == 201
    assert created[0]["libraries"] == [] and created[0]["library_name"] == "Intro & Credits: all libraries"


def test_non_object_body_is_rejected(client, created):
    resp = client.post("/api/markers/jobs", data="[1, 2]", headers=_api_headers())
    assert resp.status_code == 400
    assert created == []


def test_file_outside_the_media_root_is_rejected(client, created, media, monkeypatch):
    from media_preview_generator.web.routes import api_markers

    monkeypatch.setattr(api_markers, "MEDIA_ROOT", str(media))
    resp = client.post(
        "/api/markers/jobs", json={"file_paths": [str(media / ".." / "secrets.mkv")]}, headers=_api_headers()
    )
    assert resp.status_code == 400
    assert "outside" in resp.get_json()["error"]
    assert created == []


def test_file_inside_the_media_root_is_accepted(client, created, media, monkeypatch):
    from media_preview_generator.web.routes import api_markers

    monkeypatch.setattr(api_markers, "MEDIA_ROOT", str(media))
    resp = client.post("/api/markers/jobs", json={"file_paths": [str(media / "tv" / "a.mkv")]}, headers=_api_headers())
    assert resp.status_code == 201
    assert created[0]["file_paths"] == [str(media / "tv" / "a.mkv")]
    assert created[0]["library_name"] == "Intro & Credits: 1 file"


@pytest.mark.parametrize(
    ("url", "csrf_exempt"),
    [
        ("/api/markers/jobs", True),  # token API, exempt like POST /api/jobs
        ("/api/markers/reconcile", True),  # Check servers from a script: the same token API
        ("/api/markers/item/redetect", False),  # browser-only: proves CSRF is really on in this test
    ],
)
def test_token_scripts_can_create_marker_jobs_with_csrf_protection_on(app, created, url, csrf_exempt):
    # The app ships with WTF_CSRF_CHECK_DEFAULT off, so this turns checking on to test the exemption list itself.
    app.config.update(WTF_CSRF_ENABLED=True, WTF_CSRF_CHECK_DEFAULT=True)
    resp = app.test_client().post(url, json={"libraries": []}, headers=_api_headers())
    body = resp.get_data(as_text=True)
    assert ("CSRF" not in body) is csrf_exempt, (resp.status_code, body[:200])
    if url == "/api/markers/jobs":
        assert resp.status_code == 201 and len(created) == 1


def test_requires_authentication(app, created):
    resp = app.test_client().post("/api/markers/jobs", json={}, headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert created == []


def test_unwritable_config_refuses_to_start_a_job(client, created, monkeypatch):
    import media_preview_generator.web.config_health as config_health

    monkeypatch.setattr(
        config_health,
        "probe_config_health",
        lambda config_dir: {"writable": False, "detail": "/config is read-only", "hint": "fix the mount"},
    )
    resp = client.post("/api/markers/jobs", json={}, headers=_api_headers())
    assert resp.status_code == 503
    assert created == []


def test_creates_a_real_intro_credits_job(client, monkeypatch):
    from unittest.mock import patch

    from media_preview_generator.web.jobs import get_job_manager

    with patch("media_preview_generator.markers.triggers.start_intro_credits_job_async") as start:
        resp = client.post(
            "/api/markers/jobs",
            json={"libraries": [{"server_id": "jf-1", "library_id": "abc"}], "priority": "normal"},
            headers=_api_headers(),
        )
    assert resp.status_code == 201
    body = resp.get_json()
    job = get_job_manager().get_job(body["id"])
    assert job.kind == "intro_credits" and body["kind"] == "intro_credits"
    assert job.priority == 2
    assert job.config["libraries"] == [{"server_id": "jf-1", "library_id": "abc"}]
    start.assert_called_once_with(job.id)


class TestCheckServersRoute:
    """POST /api/markers/reconcile — queue Intro & Credits · Check servers now."""

    @pytest.fixture
    def run(self, monkeypatch):
        from media_preview_generator.markers import reconcile

        state = SimpleNamespace(calls=[], job_id="r1", created=True)

        def run_markers_reconcile(**kw):
            state.calls.append(kw)
            return reconcile.ReconcileQueued(state.job_id, state.job_id is not None and state.created)

        monkeypatch.setattr(reconcile, "run_markers_reconcile", run_markers_reconcile)
        return state

    @pytest.mark.parametrize(
        ("kwargs", "priority"),
        [
            ({}, 3),
            ({"data": "", "headers": {"Authorization": "Bearer test-token-12345678"}}, 3),
            ({"json": {}}, 3),
            ({"json": {"priority": "high"}}, 1),
            ({"json": {"priority": 2}}, 2),
        ],
        ids=["no-body", "empty-body", "empty-object", "high", "normal"],
    )
    def test_queues_the_check_servers_job(self, client, run, kwargs, priority):
        kwargs.setdefault("headers", _api_headers())
        resp = client.post("/api/markers/reconcile", **kwargs)
        assert resp.status_code == 202 and resp.get_json() == {"job_id": "r1", "already_queued": False}
        assert run.calls == [{"priority": priority}]

    def test_a_job_already_queued_is_named_as_such(self, client, run):
        run.created = False
        resp = client.post("/api/markers/reconcile", json={"priority": "high"}, headers=_api_headers())
        assert resp.status_code == 202 and resp.get_json() == {"job_id": "r1", "already_queued": True}

    def test_a_pending_check_servers_job_in_the_job_manager_is_reused(self, client):
        from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [{"id": "jf-1", "type": "jellyfin", "name": "JF", "enabled": True, "markers": {"enabled": True}}],
        )
        jm = get_job_manager()
        pending = jm.create_job(
            library_name="Intro & Credits · Check servers", config={"reconcile": True}, kind=JOB_KIND_INTRO_CREDITS
        )
        before = {job.id for job in jm.get_all_jobs()}
        resp = client.post("/api/markers/reconcile", headers=_api_headers())
        assert resp.status_code == 202 and resp.get_json() == {"job_id": pending.id, "already_queued": True}
        assert {job.id for job in jm.get_all_jobs()} == before

    def test_nothing_to_check_says_why(self, client, run):
        run.job_id = None
        resp = client.post("/api/markers/reconcile", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json() == {"job_id": None, "reason": "Intro & Credits is off on every server"}

    @pytest.mark.parametrize(
        ("data", "content_type"),
        [('{"priority": "low",}', "application/json"), ("[1]", "application/json"), ('{"priority": "urgent"}',
          "application/json"), ('{"priority": true}', "application/json"), ("priority=1", "text/plain")],
        ids=["trailing-comma", "not-an-object", "unknown-priority", "bool-priority", "not-json"],
    )  # fmt: skip
    def test_bad_bodies_are_refused_without_queueing(self, client, run, data, content_type):
        headers = {"Authorization": _api_headers()["Authorization"], "Content-Type": content_type}
        resp = client.post("/api/markers/reconcile", data=data, headers=headers)
        assert resp.status_code == 400 and resp.get_json()["error"]
        assert run.calls == []

    def test_refused_when_the_config_folder_isnt_writable(self, client, run, monkeypatch):
        import media_preview_generator.web.config_health as config_health

        monkeypatch.setattr(
            config_health, "probe_config_health", lambda config_dir: {"writable": False, "detail": "ro", "hint": "fix"}
        )
        assert client.post("/api/markers/reconcile", headers=_api_headers()).status_code == 503
        assert run.calls == []

    def test_needs_auth(self, app, run):
        assert app.test_client().post("/api/markers/reconcile").status_code == 401
        assert run.calls == []
