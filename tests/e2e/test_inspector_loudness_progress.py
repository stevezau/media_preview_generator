"""Real loudness runner, workers and SocketIO update an open Inspector over HTTP."""

# ruff: noqa: F811 - imported fixtures
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from xml.etree import ElementTree as ET

import pytest
from playwright.sync_api import Page, expect
from werkzeug.serving import make_server

from media_preview_generator.loudness import job
from media_preview_generator.servers.plex import PlexServer
from media_preview_generator.web.app import socketio
from media_preview_generator.web.jobs import JobStatus
from tests.loudness.test_job_lifecycle import lifecycle  # noqa: F401
from tests.test_api_inspector import _reset_singletons, app, authed_client, client  # noqa: F401


@pytest.mark.e2e
@pytest.mark.parametrize("source", ["library", "mapped_sender"])
@pytest.mark.parametrize("open_during_work", [False, True])
def test_real_worker_paths_drive_live_inspector(
    page: Page, app, authed_client, lifecycle, monkeypatch, source: str, open_during_work: bool
) -> None:
    from media_preview_generator.web.routes import api_inspector, api_markers

    env = lifecycle
    env.api_ready = True
    paths = [env.add_file("first.mkv"), env.add_file("second.mkv")]
    registry = job._build_multi_server_registry(None)
    cfg = registry.get_config("plex")
    cfg.path_mappings = [
        {"remote_prefix": str(env.media), "local_prefix": str(env.media), "webhook_prefixes": ["/sender"]}
    ]
    client = registry.get("plex")
    native_query = env.query

    def query(url: str) -> ET.Element:
        root = native_query(url)
        if url == "/identity":
            return root
        wrapped = ET.Element("MediaContainer")
        video = ET.SubElement(wrapped, "Video", ratingKey=url.rsplit("/", 1)[1])
        media = ET.SubElement(video, "Media", id="1")
        for part in root:
            part.set("size", str(Path(part.get("file")).stat().st_size))
            media.append(part)
        return wrapped

    def resolve(path: str, *, library_ids=None) -> str:
        with sqlite3.connect(env.database) as conn:
            return str(conn.execute("SELECT media_item_id FROM media_parts WHERE file=?", (path,)).fetchone()[0])

    client._connect = lambda: SimpleNamespace(query=query)
    client.resolve_remote_path_to_item_id = resolve
    client.get_bundle_metadata = lambda item_id: []
    # Simulate Plex's external listing response; enumeration and canonical-path resolution stay real.
    movies = [
        SimpleNamespace(ratingKey=str(i + 1), title=Path(path).stem, locations=[path]) for i, path in enumerate(paths)
    ]
    section = SimpleNamespace(
        key="1", title="Movies", locations=[str(env.media)], METADATA_TYPE="movie", search=lambda **kwargs: movies
    )
    monkeypatch.setattr(
        PlexServer, "_connect", lambda self: SimpleNamespace(library=SimpleNamespace(sections=lambda: [section]))
    )
    monkeypatch.setattr(api_inspector, "_registry", lambda **kwargs: registry)
    monkeypatch.setattr(api_markers, "_registry", lambda **kwargs: registry)
    monkeypatch.setattr(api_inspector, "MEDIA_ROOT", str(env.media))
    monkeypatch.setattr(api_markers, "MEDIA_ROOT", str(env.media))
    env.manager.set_socketio(socketio)
    env.dispatcher.worker_pool.add_workers("CPU", 1)
    release = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()
    starts = []
    original_popen = env.popen

    def popen(command: list[str], **kwargs):
        process = original_popen(command, **kwargs)
        wait_for_exit = process.wait
        with lock:
            starts.append(command[command.index("-i") + 1])
            if len(starts) == 2:
                both_started.set()

        def wait(**kwargs):
            assert release.wait(15), "test did not release simulated FFmpeg"
            return wait_for_exit(**kwargs)

        process.wait = wait
        return process

    monkeypatch.setattr(job.analyze.subprocess, "Popen", popen)
    config = {"source": "manual", "libraries": [{"server_id": "plex", "library_id": "1"}]}
    if source == "mapped_sender":
        config = {"source": "sonarr", "file_paths": ["/sender/first.mkv", "/sender/second.mkv"]}
    parent = env.manager.create_job(library_name="Live loudness", kind="loudness", config=config)
    runner = threading.Thread(target=job.run_loudness_job, args=(parent.id,), daemon=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    web_thread = threading.Thread(target=server.serve_forever, daemon=True)
    web_thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    cookie = authed_client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    page.context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": url}])
    try:
        if open_during_work:
            runner.start()
            assert both_started.wait(10), env.manager.get_file_results(parent.id)
        page.goto(f"{url}/inspector?path={quote(paths[0])}")
        card = page.locator("#inspLoudness [data-server-id='plex']")
        expect(card).to_contain_text("Partial measurements")
        page.wait_for_function(
            "performance.getEntriesByType('resource').some(e => e.name.includes('socket.io/?') && e.name.includes('sid='))"
        )
        if not open_during_work:
            runner.start()
            assert both_started.wait(10), env.manager.get_file_results(parent.id)
        expect(page.locator("#inspJobBanner")).to_contain_text("Plex loudness job")
        assert sorted(parent.progress.current_files) == sorted(paths)
        assert api_inspector.active_job_for(paths[0])["id"] == parent.id
        assert api_inspector.active_job_for(paths[1])["id"] == parent.id
        release.set()
        expect(card).to_contain_text("Available in Plex", timeout=10000)
        expect(page.locator("#inspJobBanner")).to_be_hidden()
        runner.join(timeout=10)
        assert not runner.is_alive()
        assert parent.status is JobStatus.COMPLETED
        assert parent.progress.current_files == []
        assert env.manager.get_file_results(parent.id)
        page.reload()
        expect(card).to_contain_text("Available in Plex")
        expect(page.locator("#inspJobBanner")).to_be_hidden()
    finally:
        release.set()
        runner.join(timeout=10) if runner.ident else None
        server.shutdown()
        server.server_close()
        web_thread.join(timeout=5)
