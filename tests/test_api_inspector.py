"""Inspector API: search-row statuses, a show's episodes, one file's previews, exact frames; and the Inspector pages."""

from __future__ import annotations

import base64
import json
import os
import struct
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from media_preview_generator.bif_reader import BIF_MAGIC
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
from media_preview_generator.web.app import create_app
from media_preview_generator.web.routes._helpers import limiter
from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

TOKEN = "test-token-12345678"
EMBY_KEY = "emby-key-SECRET-4242"
FFMPEG = "/opt/ffmpeg/bin/ffmpeg"


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _write_bif(path, *, frames: int, multiplier_ms: int, timestamps: list[int] | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    timestamps = timestamps if timestamps is not None else list(range(frames))
    jpegs = [b"\xff\xd8\xff" + bytes([i]) * 10 for i in range(frames)]
    header = BIF_MAGIC + struct.pack("<III", 0, frames, multiplier_ms) + b"\x00" * 44
    offset = len(header) + 8 * (frames + 1)
    index = b""
    for ts, jpeg in zip(timestamps, jpegs, strict=True):
        index += struct.pack("<II", ts, offset)
        offset += len(jpeg)
    index += struct.pack("<II", 0xFFFFFFFF, offset)
    with open(path, "wb") as f:
        f.write(header + index + b"".join(jpegs))
    return str(path)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset_singletons():
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.scheduler as sched_mod
    from media_preview_generator.markers.store import reset_marker_store
    from media_preview_generator.web.routes import api_inspector, clear_gpu_cache

    def reset():
        reset_settings_manager()
        reset_marker_store()
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        with sched_mod._schedule_lock:
            if sched_mod._schedule_manager is not None:
                try:
                    sched_mod._schedule_manager.stop()
                except Exception:
                    pass
                sched_mod._schedule_manager = None
        clear_gpu_cache()
        with api_inspector._known_lock:
            api_inspector._known_paths.clear()

    reset()
    yield
    reset()


@pytest.fixture
def app(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "auth.json").write_text(json.dumps({"token": TOKEN}))
    (config_dir / "settings.json").write_text(
        json.dumps({"setup_complete": True, "plex_config_folder": str(tmp_path / "plex")})
    )
    with patch.dict(os.environ, {"CONFIG_DIR": str(config_dir), "WEB_AUTH_TOKEN": TOKEN, "WEB_PORT": "8099"}):
        flask_app = create_app(config_dir=str(config_dir))
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        # The rate limits count across apps in one process; each test starts with none used.
        limiter.reset()
        yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def authed_client(client):
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


@pytest.fixture
def media(tmp_path, monkeypatch):
    """A media folder (the media root) with a film, a show and a folder no library holds; a file outside the root."""
    from media_preview_generator.web.routes import api_inspector, api_markers

    root = tmp_path.resolve() / "media"
    film_dir = root / "movies" / "Film (2020) {tmdb-1}"
    film_dir.mkdir(parents=True)
    (film_dir / "Film (2020) {tmdb-1} [Bluray-2160p][DV].mkv").write_bytes(b"film")
    (film_dir / "notes.txt").write_text("not a video")
    show = root / "tv" / "Show (2021)"
    for rel in ("Season 01/Show - S01E02.mkv", "Season 01/Show - S01E01.mkv", "Specials/Show - S00E01.mkv"):
        (show / rel).parent.mkdir(parents=True, exist_ok=True)
        (show / rel).write_bytes(b"ep")
    (root / "other").mkdir()
    (root / "other" / "loose.mkv").write_bytes(b"x")
    (tmp_path / "secret.mkv").write_bytes(b"x")
    monkeypatch.setattr(api_markers, "MEDIA_ROOT", str(root))
    monkeypatch.setattr(api_inspector, "MEDIA_ROOT", str(root))
    return root


@pytest.fixture
def film(media) -> str:
    return str(media / "movies" / "Film (2020) {tmdb-1}" / "Film (2020) {tmdb-1} [Bluray-2160p][DV].mkv")


@pytest.fixture
def sidecar(film) -> str:
    """Where the Emby server keeps the film's preview (width 320, interval 10)."""
    return os.path.join(os.path.dirname(film), "Film (2020) {tmdb-1} [Bluray-2160p][DV]-320-10.bif")


def _emby_entry(media) -> dict:
    return {
        "id": "emby-1",
        "type": "emby",
        "name": "Emby",
        "enabled": True,
        "url": "http://127.0.0.1:9",
        "auth": {"method": "api_key", "api_key": EMBY_KEY},
        "libraries": [
            {"id": "m", "name": "Movies", "remote_paths": [str(media / "movies")]},
            {"id": "t", "name": "TV", "remote_paths": [str(media / "tv")]},
        ],
        "output": {"width": 320, "frame_interval": 10},
    }


@pytest.fixture
def servers(app, media):
    entries = [_emby_entry(media)]
    get_settings_manager().set("media_servers", entries)
    return entries


@pytest.fixture
def emby_lookups(monkeypatch):
    """Record every Emby item lookup; ``answer[0]`` is what it returns."""
    from media_preview_generator.servers.emby import EmbyServer

    calls: list[dict] = []
    answer: list[str | None] = [None]

    def fake(self, remote_path, *, library_ids=None):
        calls.append({"server_id": self.id, "path": remote_path, "library_ids": list(library_ids or [])})
        return answer[0]

    monkeypatch.setattr(EmbyServer, "resolve_remote_path_to_item_id", fake)
    return calls, answer


def _known(path: str, *, duration_ms: int | None = 100_000, is_movie: bool = True, decisions: dict | None = None):
    from media_preview_generator.markers.store import get_marker_store

    store = get_marker_store()
    rec = store.upsert_file(FileIdentity(path, 1, 1), duration_ms=duration_ms, season_key=None, is_movie=is_movie)
    if decisions:
        store.save_decisions(rec.id, decisions, settings_fingerprint="f")
    return rec


CREDITS_DECIDED = {
    MarkerType.CREDITS: TypeDecision(
        MarkerType.CREDITS,
        DecisionStatus.DECIDED,
        Marker(MarkerType.CREDITS, 90_000, 100_000, ("chapters",)),
        None,
        "agreed",
    ),
    MarkerType.INTRO: TypeDecision(MarkerType.INTRO, DecisionStatus.NO_EVIDENCE, None, None, "nothing found"),
}


# --------------------------------------------------------------------------- auth


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("post", "/api/inspector/status"),
        ("post", "/api/inspector/show"),
        ("get", "/api/inspector/file?path=/x.mkv"),
        ("get", "/api/inspector/frames?path=/x.mkv"),
    ],
)
def test_every_route_is_401_without_auth(client, method, url):
    kwargs = {"json": {"paths": ["/x"]}} if method == "post" else {}

    resp = getattr(client, method)(url, **kwargs)

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Authentication required"}


def test_bearer_token_is_accepted(client, servers, film):
    resp = client.post("/api/inspector/status", json={"paths": [film]}, headers=_headers())

    assert resp.status_code == 200


# --------------------------------------------------------------------------- status


@pytest.mark.parametrize(
    "body",
    [
        {"data": "paths=/x", "content_type": "text/plain"},
        {"json": ["/x"]},
        {"json": {}},
        {"json": {"paths": []}},
        {"json": {"paths": "/x"}},
        {"json": {"paths": ["/x", 3]}},
        {"json": {"paths": ["  "]}},
        {"json": {"paths": [f"/m/{i}.mkv" for i in range(31)]}},
    ],
)
def test_status_bad_body_is_400(authed_client, servers, body):
    resp = authed_client.post("/api/inspector/status", **body)

    assert resp.status_code == 400
    assert resp.get_json()["error"]


def test_status_accepts_30_paths(authed_client, servers, media):
    paths = [str(media / "other" / f"{i}.mkv") for i in range(30)]

    resp = authed_client.post("/api/inspector/status", json={"paths": paths})

    assert resp.status_code == 200
    assert len(resp.get_json()["items"]) == 30


def test_status_outside_every_library_says_only_that(authed_client, servers, media, tmp_path):
    # One is on disk, one isn't: the answers must be the same, or the route would say which paths exist.
    on_disk = str(media / "other" / "loose.mkv")
    not_on_disk = str(media / "other" / "nope.mkv")
    outside_root = str(tmp_path.resolve() / "secret.mkv")

    resp = authed_client.post("/api/inspector/status", json={"paths": [on_disk, not_on_disk, outside_root]})

    assert resp.status_code == 200
    assert resp.get_json()["items"] == {
        on_disk: {"in_library": False},
        not_on_disk: {"in_library": False},
        outside_root: {"in_library": False},
    }


def test_status_of_a_library_file_with_its_emby_preview(authed_client, servers, film, sidecar):
    _write_bif(sidecar, frames=12, multiplier_ms=10_000)
    _known(film, decisions=CREDITS_DECIDED)

    resp = authed_client.post("/api/inspector/status", json={"paths": [film]})

    assert resp.status_code == 200
    assert resp.get_json()["items"][film] == {
        "in_library": True,
        "exists": True,
        "kind": "movie",
        "quality": "2160p Dolby Vision",
        "servers": ["Emby"],
        "preview": {"state": "ready", "frames": 12, "servers": ["Emby"]},
        "markers": {"state": "credits", "label": "Credits set"},
    }


def test_status_of_a_library_file_without_its_preview(authed_client, servers, film):
    resp = authed_client.post("/api/inspector/status", json={"paths": [film]})

    item = resp.get_json()["items"][film]
    assert item["preview"] == {"state": "missing", "frames": None, "servers": []}
    assert item["markers"] == {"state": "not_checked", "label": "Not checked yet"}


def test_status_is_unknown_when_a_server_cannot_be_asked(authed_client, app, media, film, tmp_path, monkeypatch):
    from media_preview_generator.servers.plex import PlexServer

    def boom(self, remote_path, *, library_ids=None):
        raise RuntimeError(f"connect failed key={EMBY_KEY}")

    monkeypatch.setattr(PlexServer, "resolve_remote_path_to_item_id", boom)
    plex = {
        "id": "plex-1",
        "type": "plex",
        "name": "Plex",
        "enabled": True,
        "url": "http://127.0.0.1:9",
        "auth": {"method": "token", "token": "plex-token-SECRET"},
        "libraries": [{"id": "1", "name": "Movies", "remote_paths": [str(media / "movies")]}],
        "output": {"plex_config_folder": str(tmp_path / "plexcfg")},
    }
    get_settings_manager().set("media_servers", [plex, _emby_entry(media)])

    resp = authed_client.post("/api/inspector/status", json={"paths": [film]})

    item = resp.get_json()["items"][film]
    assert item["servers"] == ["Plex", "Emby"]
    assert item["preview"] == {"state": "unknown", "frames": None, "servers": []}
    assert EMBY_KEY not in resp.get_data(as_text=True)


# --------------------------------------------------------------------------- show


def test_show_lists_the_seasons_of_a_library_folder(authed_client, servers, media):
    show = media / "tv" / "Show (2021)"
    _known(str(show / "Season 01" / "Show - S01E02.mkv"), is_movie=False, decisions=CREDITS_DECIDED)

    resp = authed_client.post("/api/inspector/show", json={"paths": [str(show)]})

    assert resp.status_code == 200
    seasons = resp.get_json()["seasons"]
    assert [(s["season"], s["label"]) for s in seasons] == [(1, "Season 1"), (0, "Specials")]
    assert [(e["code"], e["path"]) for e in seasons[0]["episodes"]] == [
        ("E01", str(show / "Season 01" / "Show - S01E01.mkv")),
        ("E02", str(show / "Season 01" / "Show - S01E02.mkv")),
    ]
    assert seasons[0]["episodes"][1]["markers"] == {"state": "credits", "label": "Credits only"}


def test_show_skips_folders_outside_libraries_when_one_is_inside(authed_client, servers, media):
    show = media / "tv" / "Show (2021)"

    resp = authed_client.post("/api/inspector/show", json={"paths": [str(media / "other"), str(show)]})

    assert resp.status_code == 200
    assert [s["season"] for s in resp.get_json()["seasons"]] == [1, 0]


@pytest.mark.parametrize(
    "raw",
    [
        "{media}/other",
        "{media}/tv/../other",
        "{media}/tv/../../",
        "{tmp}",
        "{media}/tv/Show (2021)/Season 01/Show - S01E01.mkv",
        "{media}/tv/Missing Show",
        "tv/Show (2021)",
    ],
)
def test_show_with_no_folder_inside_a_library_is_400(authed_client, servers, media, tmp_path, raw):
    path = raw.format(media=media, tmp=tmp_path.resolve())

    resp = authed_client.post("/api/inspector/show", json={"paths": [path]})

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "None of these is a folder inside a server library"}


def test_show_bad_body_is_400(authed_client, servers):
    resp = authed_client.post("/api/inspector/show", json={"paths": [f"/m/{i}" for i in range(11)]})

    assert resp.status_code == 400


# --------------------------------------------------------------------------- file


@pytest.mark.parametrize(
    "raw",
    [
        "movies/Film.mkv",
        "{media}/../secret.mkv",
        "{media}/movies/../../secret.mkv",
        "{media}/movies/Film.mkv\x00",
        "{tmp}/secret.mkv",
        "/etc/passwd",
        "",
    ],
)
def test_file_bad_path_is_400(authed_client, servers, media, tmp_path, raw):
    path = raw.format(media=media, tmp=tmp_path.resolve())

    resp = authed_client.get("/api/inspector/file", query_string={"path": path})

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Give the file's full path inside the media folder"}


def test_file_outside_libraries_and_markers_db_does_not_say_whether_it_exists(authed_client, servers, media):
    on_disk = str(media / "other" / "loose.mkv")
    not_on_disk = str(media / "other" / "nope.mkv")

    answers = [
        authed_client.get("/api/inspector/file", query_string={"path": p}).get_json() for p in (on_disk, not_on_disk)
    ]

    for body in answers:
        assert body["exists"] is None
        assert body["in_library"] is False
        assert body["known"] is False
        assert (body["previews"], body["preview"], body["versions"], body["job"]) == ([], None, [], None)
    # Nothing else in the answer differs between the file that is there and the one that isn't.
    drop = ("canonical_path", "title")
    assert {k: v for k, v in answers[0].items() if k not in drop} == {
        k: v for k, v in answers[1].items() if k not in drop
    }


def test_file_known_to_markers_db_but_gone_from_disk(authed_client, servers, media, emby_lookups):
    gone = str(media / "movies" / "Gone (2019)" / "Gone (2019).mkv")
    _known(gone, duration_ms=5_000_000)

    body = authed_client.get("/api/inspector/file", query_string={"path": gone}).get_json()

    assert body["exists"] is False
    assert body["known"] is True
    assert body["in_library"] is True
    assert body["duration_ms"] == 5_000_000
    assert body["previews"] == []
    assert emby_lookups[0] == []


def test_file_with_a_preview_whose_header_leaves_the_interval_out(authed_client, servers, film, sidecar):
    # Header interval 0 and every timestamp 0: the file itself can't tell; markers.db's length must.
    _write_bif(sidecar, frames=10, multiplier_ms=0, timestamps=[0] * 10)
    _known(film, duration_ms=100_000)

    resp = authed_client.get("/api/inspector/file", query_string={"path": film})

    assert resp.status_code == 200
    body = resp.get_json()
    assert (body["canonical_path"], body["exists"], body["in_library"], body["known"]) == (film, True, True, True)
    assert body["title"] == "Film (2020)"
    assert (body["kind"], body["quality"], body["duration_ms"]) == ("movie", "2160p Dolby Vision", 100_000)
    [row] = body["previews"]
    assert (row["server_id"], row["path"], row["exists"], row["interval_ms"]) == ("emby-1", sidecar, True, 0)
    assert body["preview"]["path"] == sidecar
    assert body["preview"]["frame_count"] == 10
    assert body["preview"]["interval_ms"] == 10_000
    assert body["versions"] == []
    assert body["job"] is None
    assert EMBY_KEY not in resp.get_data(as_text=True)


def test_file_not_in_markers_db_takes_its_length_from_the_preview(authed_client, servers, film, sidecar):
    _write_bif(sidecar, frames=4, multiplier_ms=5_000)

    body = authed_client.get("/api/inspector/file", query_string={"path": film}).get_json()

    assert body["known"] is False
    assert body["preview"]["interval_ms"] == 5_000
    assert body["duration_ms"] == 20_000


def test_file_without_a_preview_has_none_chosen(authed_client, servers, film):
    body = authed_client.get("/api/inspector/file", query_string={"path": film}).get_json()

    assert [r["exists"] for r in body["previews"]] == [False]
    assert body["preview"] is None
    assert body["duration_ms"] is None


def test_file_shows_the_pending_job_started_for_it(authed_client, servers, film):
    from media_preview_generator.web.jobs import get_job_manager

    job = get_job_manager().create_job(library_name="Previews: Film", config={"file_paths": [film]})
    get_job_manager().create_job(library_name="Someone else", config={"file_paths": [film + ".other"]})

    body = authed_client.get("/api/inspector/file", query_string={"path": film}).get_json()

    assert body["job"] == {
        "id": job.id,
        "kind": job.kind,
        "status": "pending",
        "name": "Previews: Film",
        "percent": 0.0,
    }


def test_file_prefers_the_running_job_working_on_it(authed_client, servers, film):
    from media_preview_generator.web.jobs import JobStatus, get_job_manager

    manager = get_job_manager()
    manager.create_job(library_name="Queued", config={"file_paths": [film]})
    done = manager.create_job(library_name="Done", config={"webhook_paths": [film]})
    done.status = JobStatus.COMPLETED
    running = manager.create_job(library_name="Library scan", config={})
    manager.start_job(running.id)
    manager.update_progress(running.id, percent=40.0, current_file=film)

    body = authed_client.get("/api/inspector/file", query_string={"path": film}).get_json()

    assert body["job"] == {
        "id": running.id,
        "kind": running.kind,
        "status": "running",
        "name": "Library scan",
        "percent": 40.0,
    }


# --------------------------------------------------------------------------- frames


@pytest.fixture
def exact(monkeypatch):
    """Stand-in for ``inspector.frames.exact_frames``; ``result[0]`` is its answer (or an exception to raise)."""
    from media_preview_generator.inspector import frames as frames_mod

    calls: list[dict] = []
    result: list = [[(5_000, b"\xff\xd8one"), (6_000, b"\xff\xd8two")]]

    def fake(path, **kwargs):
        calls.append({"path": path, **kwargs})
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    monkeypatch.setattr(frames_mod, "exact_frames", fake)
    monkeypatch.setattr("media_preview_generator.config._resolve_ffmpeg_path", lambda: FFMPEG)
    return calls, result


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"path": ""},
        {"path": "/media/../etc/passwd"},
        {"path": "{media}/movies/../other/loose.mkv"},
        {"path": "{media}/other/loose.mkv"},
        {"path": "{tmp}/secret.mkv"},
        {"path": "{media}/movies/Film (2020) {{tmdb-1}}/notes.txt"},
        {"path": "{media}/movies/Film (2020) {{tmdb-1}}/missing.mkv"},
        {"path": "{film}", "start_ms": "abc"},
        {"path": "{film}", "start_ms": "1.5"},
        {"path": "{film}", "start_ms": "-1"},
        {"path": "{film}", "count": "x"},
        {"path": "{film}", "count": "0"},
        {"path": "{film}", "count": "15"},
        {"path": "{film}", "width": "300"},
        {"path": "{film}", "width": "wide"},
    ],
)
def test_frames_bad_query_is_400(authed_client, servers, media, film, tmp_path, exact, emby_lookups, query):
    _known(film)
    query = {k: v.format(media=media, tmp=tmp_path.resolve(), film=film) for k, v in query.items()}

    resp = authed_client.get("/api/inspector/frames", query_string=query)

    assert resp.status_code == 400
    assert resp.get_json()["error"]
    assert exact[0] == []
    assert emby_lookups[0] == []


def test_frames_for_a_file_no_one_knows_is_404(authed_client, servers, film, exact, emby_lookups):
    calls, _answer = emby_lookups

    resp = authed_client.get("/api/inspector/frames", query_string={"path": film})

    assert resp.status_code == 404
    assert calls == [{"server_id": "emby-1", "path": film, "library_ids": ["m"]}]
    assert exact[0] == []


def test_frames_for_a_file_markers_db_has(authed_client, servers, film, exact, emby_lookups):
    _known(film)

    resp = authed_client.get(
        "/api/inspector/frames", query_string={"path": film, "start_ms": "5000", "count": "2", "width": "480"}
    )

    assert resp.status_code == 200
    assert exact[0] == [
        {"path": film, "start_ms": 5000, "count": 2, "width": 480, "ffmpeg": FFMPEG, "tonemap": "hable"}
    ]
    body = resp.get_json()
    assert (body["path"], body["start_ms"], body["step_ms"]) == (film, 5000, 1000)
    assert [f["t_ms"] for f in body["frames"]] == [5_000, 6_000]
    assert all(f["src"].startswith("data:image/jpeg;base64,") for f in body["frames"])
    assert base64.b64decode(body["frames"][1]["src"].split(",", 1)[1]) == b"\xff\xd8two"
    assert emby_lookups[0] == []


def test_frames_defaults(authed_client, servers, film, exact):
    _known(film)

    resp = authed_client.get("/api/inspector/frames", query_string={"path": film, "start_ms": "", "count": ""})

    assert resp.status_code == 200
    assert exact[0] == [{"path": film, "start_ms": 0, "count": 7, "width": 320, "ffmpeg": FFMPEG, "tonemap": "hable"}]


def test_frames_for_a_file_a_server_lists_is_remembered(authed_client, servers, film, exact, emby_lookups):
    calls, answer = emby_lookups
    answer[0] = "item-77"

    first = authed_client.get("/api/inspector/frames", query_string={"path": film})
    second = authed_client.get("/api/inspector/frames", query_string={"path": film, "start_ms": "3000"})

    assert (first.status_code, second.status_code) == (200, 200)
    assert calls == [{"server_id": "emby-1", "path": film, "library_ids": ["m"]}]
    assert [c["start_ms"] for c in exact[0]] == [0, 3000]


def test_frames_busy_is_503(authed_client, servers, film, exact):
    from media_preview_generator.inspector.frames import FramesBusyError

    _known(film)
    exact[1][0] = FramesBusyError("busy")

    resp = authed_client.get("/api/inspector/frames", query_string={"path": film})

    assert resp.status_code == 503
    assert resp.get_json() == {"error": "Busy reading other frames; try again in a moment"}


def test_frames_ffmpeg_failure_is_502_with_its_message(authed_client, servers, film, exact):
    from media_preview_generator.inspector.frames import FramesError

    _known(film)
    exact[1][0] = FramesError("ffmpeg couldn't read frames there")

    resp = authed_client.get("/api/inspector/frames", query_string={"path": film})

    assert resp.status_code == 502
    assert resp.get_json() == {"error": "ffmpeg couldn't read frames there"}


# --------------------------------------------------------------------------- pages


def test_inspector_page_renders(authed_client):
    resp = authed_client.get("/inspector")

    assert resp.status_code == 200
    assert 'id="inspQuery"' in resp.get_data(as_text=True)


def test_inspector_page_needs_a_login(client):
    resp = client.get("/inspector")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_old_bif_viewer_address_redirects_to_the_inspector_without_tab(authed_client):
    resp = authed_client.get("/bif-viewer", query_string={"file": "/x", "tab": "markers"})

    assert resp.status_code == 302
    location = urlsplit(resp.headers["Location"])
    assert location.path == "/inspector"
    assert parse_qs(location.query) == {"file": ["/x"]}


def test_old_bif_viewer_address_keeps_the_bif_link(authed_client):
    resp = authed_client.get("/bif-viewer", query_string={"bif": "/plex/index-sd.bif"})

    assert resp.status_code == 302
    location = urlsplit(resp.headers["Location"])
    assert location.path == "/inspector"
    assert parse_qs(location.query) == {"bif": ["/plex/index-sd.bif"]}


@pytest.mark.parametrize(
    "query",
    [
        {"endpoint": "x", "file": "/y"},
        {"_external": "1", "_scheme": "javascript", "file": "/y"},
        {"_method": "POST", "file": "/y"},
    ],
)
def test_old_bif_viewer_query_keys_never_steer_the_redirect(authed_client, query):
    resp = authed_client.get("/bif-viewer", query_string=query)

    assert resp.status_code == 302
    location = urlsplit(resp.headers["Location"])
    assert (location.scheme, location.path) == ("", "/inspector")
    assert parse_qs(location.query) == {k: [v] for k, v in query.items()}


def test_frames_start_past_two_days_is_400(authed_client, servers, film, exact):
    _known(film)

    resp = authed_client.get("/api/inspector/frames", query_string={"path": film, "start_ms": str(10**400)})

    assert resp.status_code == 400
    assert exact[0] == []


def test_frames_value_error_from_the_reader_is_400(authed_client, servers, film, exact):
    _known(film)
    exact[1][0] = ValueError("path must be absolute")

    resp = authed_client.get("/api/inspector/frames", query_string={"path": film})

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "path must be absolute"}


@pytest.mark.parametrize(("stored", "sent"), [("mobius", "mobius"), ("REINHARD ", "reinhard"), ("x,drop", "hable")])
def test_frames_tone_map_with_the_previews_setting(authed_client, servers, film, exact, stored, sent):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("tonemap_algorithm", stored)
    _known(film)

    resp = authed_client.get("/api/inspector/frames", query_string={"path": film})

    assert resp.status_code == 200
    assert exact[0][0]["tonemap"] == sent
