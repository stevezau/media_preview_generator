"""Opt-in real pipeline proof against the dedicated chapter spike fixture.

Run from the project root: python tests/integration/verify_plex_chapters.py --run
Uses only the disposable ``pr287-chapter-spike`` container and synthetic 30s,
three-chapter movie at /tmp/pr287-chapter-spike. See the design evidence README
for fixture setup. Refuses a claimed server, other version, extra libraries,
wrong Docker mounts/port, or unexpected movie metadata. It regenerates outputs,
removes one JPEG and clears synthetic chapter refs to prove recovery. Random
helper credentials live only in memory. The sanitized report stays in /tmp.
"""

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
from plexapi.server import PlexServer as PlexApi
from werkzeug.serving import make_server

from media_preview_generator.bif_reader import read_bif_metadata
from media_preview_generator.config import Config
from media_preview_generator.output.plex_bundle import PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash
from media_preview_generator.processing import chapters, multi_server
from media_preview_generator.servers import ServerRegistry


def verify_helper_navigation(url, token, root, raw):
    """Drive the real browser from chapter Setup Health into helper settings."""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url + "/login")
            page.locator("#token").fill(token)
            page.locator('button[type="submit"]').click()
            page.goto(url + "/servers")
            page.locator('.edit-server-btn[data-id="chapter-lab"]').click()
            expect(page.locator("#markersEnabled")).not_to_be_checked()
            page.locator("#markersAgentUrl").fill("http://127.0.0.1:1")
            page.locator("#editServerSave").click()
            expect(page.locator("#editServerModal")).to_be_hidden(timeout=10000)
            saved = json.loads((root / "settings.json").read_text())["media_servers"][0]
            assert saved["markers"]["enabled"] is False
            assert saved["markers"]["plex"]["agent"]["token"] == raw["markers"]["plex"]["agent"]["token"]
            page.locator('.edit-server-btn[data-id="chapter-lab"]').click()
            page.locator('[data-bs-target="#edit-tab-processing"]').click()
            page.locator("#editPlexChapterHealthLink").click()
            expect(page.locator("#edit-tab-health")).to_be_visible()
            link = page.locator(".chapter-helper-link")
            expect(link).to_be_visible(timeout=30000)
            link.click()
            expect(page.locator("#edit-tab-general")).to_be_visible()
            expect(page.locator("#markersPlexAgentGroup")).to_be_visible()
            expect(page.locator("#markersEnabled")).not_to_be_checked()
        finally:
            browser.close()


def verify_web_job(raw, source, chapter_folder, bifpath):
    """Use the real Flask manual-job route, worker, persistence and Files API."""
    with tempfile.TemporaryDirectory(prefix="chapter-web-proof-") as temporary:
        root = Path(temporary)
        settings = {
            "setup_complete": True,
            "media_servers": [raw],
            "cpu_threads": 1,
            "gpu_threads": 0,
            "ffmpeg_threads": 2,
            "thumbnail_interval": 5,
            "thumbnail_quality": 4,
            "tonemap_algorithm": "hable",
            "gpu_config": [],
            "path_mappings": [],
            "exclude_paths": [],
            "auto_requeue_on_restart": False,
        }
        (root / "settings.json").write_text(json.dumps(settings))
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        token = secrets.token_hex(24)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(root),
            "CONFIG_DIR": str(root),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "WEB_AUTH_TOKEN": token,
            "MEDIA_ROOT": str(source.parents[1]),
        }
        boot = (
            "import dotenv; dotenv.load_dotenv=lambda *a,**k:None; "
            "from media_preview_generator.web.app import run_server; "
            f"run_server(host='127.0.0.1',port={port})"
        )
        with (root / "app.log").open("w+") as log:
            process = subprocess.Popen([sys.executable, "-c", boot], env=env, cwd=root, stdout=log, stderr=log)
            url = f"http://127.0.0.1:{port}"
            headers = {"X-Auth-Token": token}
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        if requests.get(url + "/api/jobs", headers=headers, timeout=1).ok:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(0.2)
                else:
                    raise AssertionError("Isolated Flask app did not start")
                before_bif = (bifpath.stat().st_mtime_ns, hashlib.sha256(bifpath.read_bytes()).hexdigest())
                (chapter_folder / "chapter2.jpg").unlink()
                response = requests.post(
                    url + "/api/jobs/manual",
                    headers=headers,
                    json={"file_paths": [str(source)], "server_id": raw["id"]},
                    timeout=10,
                )
                response.raise_for_status()
                job_id = response.json()["id"]
                deadline = time.monotonic() + 60
                job = {}
                while time.monotonic() < deadline:
                    response = requests.get(url + f"/api/jobs/{job_id}", headers=headers, timeout=5)
                    response.raise_for_status()
                    job = response.json()
                    if job.get("status") in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.2)
                response = requests.get(url + f"/api/jobs/{job_id}/files", headers=headers, timeout=5)
                response.raise_for_status()
                files = response.json()["files"]
                assert job.get("status") == "completed", {"status": job.get("status"), "error": job.get("error")}
                assert len(files) == 1 and files[0]["servers"][0]["artifacts"]["chapters"]["status"] == "ready", files
                assert before_bif == (bifpath.stat().st_mtime_ns, hashlib.sha256(bifpath.read_bytes()).hexdigest())
                assert (chapter_folder / "chapter2.jpg").exists()
                verify_helper_navigation(url, token, root, raw)
                return {
                    "status": job["status"],
                    "files": len(files),
                    "artifacts": files[0]["servers"][0]["artifacts"],
                    "bif_unchanged": True,
                    "missing_chapter_recovered": True,
                    "browser_helper_navigation_markers_disabled": True,
                }
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main():
    """Verify real generation, backfill, registration repair and disabled behavior."""
    ROOT = Path("/tmp/pr287-chapter-spike")
    FOLDER = ROOT / "config/Library/Application Support/Plex Media Server"
    SOURCE = ROOT / "media/Chapter Spike (2026)/Chapter Spike (2026).mkv"
    URL = "http://127.0.0.1:32587"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    if not parser.parse_args().run:
        parser.error("Pass --run to mutate only the dedicated synthetic fixture")
    container = json.loads(subprocess.check_output(["docker", "inspect", "pr287-chapter-spike"]))[0]
    mounts = {entry["Destination"]: entry["Source"] for entry in container["Mounts"]}
    assert mounts["/config"] == str(ROOT / "config") and mounts["/media"] == str(ROOT / "media")
    assert container["NetworkSettings"]["Ports"]["32400/tcp"][0]["HostPort"] == "32587"
    plex = PlexApi(URL)
    identity = plex.query("/identity")
    assert identity.get("claimed") == "0" and identity.get("version") == "1.43.4.10903-e5521bd8c"
    assert plex.query("/library/sections")[0].get("title") == "PR287 Synthetic"
    assert SOURCE.is_file()
    probe = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(SOURCE)]
        )
    )
    assert len(probe["chapters"]) == 3 and abs(float(probe["format"]["duration"]) - 30) < 0.1
    assert len(plex.query("/library/sections")) == 1
    assert plex.query("/library/metadata/1")[0].get("title") == "Chapter Spike (2026)"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plex-marker-agent"))
    import plex_marker_agent

    key = secrets.token_hex(24)
    http = make_server("127.0.0.1", 0, plex_marker_agent.create_app(config_dir=str(FOLDER), token=key), threaded=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    raw = {
        "id": "chapter-lab",
        "type": "plex",
        "name": "Chapter Lab",
        "enabled": True,
        "url": URL,
        # The unclaimed fixture accepts local requests with this synthetic
        # placeholder; the normal app configuration requires a token value.
        "auth": {"token": "isolated-unclaimed-fixture"},
        "server_identity": identity.get("machineIdentifier"),
        "libraries": [{"id": "1", "name": "PR287 Synthetic", "enabled": True, "remote_paths": ["/media"]}],
        "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(ROOT / "media")}],
        "output": {
            "adapter": "plex_bundle",
            "plex_config_folder": str(FOLDER),
            "chapter_thumbnails": True,
            "frame_interval": 5,
        },
        "markers": {
            "enabled": False,
            "plex": {"agent": {"enabled": True, "url": f"http://127.0.0.1:{http.server_port}", "token": key}},
        },
    }
    registry = ServerRegistry.from_settings([raw])
    registry.get("chapter-lab")._plex = plex
    cfg = Config(
        plex_url=URL,
        plex_token="",
        plex_timeout=10,
        plex_verify_ssl=True,
        plex_libraries=["PR287 Synthetic"],
        plex_config_folder=str(FOLDER),
        plex_local_videos_path_mapping="",
        plex_videos_path_mapping="",
        path_mappings=raw["path_mappings"],
        plex_bif_frame_interval=5,
        thumbnail_quality=4,
        regenerate_thumbnails=False,
        sort_by=None,
        gpu_threads=0,
        cpu_threads=1,
        ffmpeg_threads=2,
        tmp_folder=str(ROOT / "work"),
        tmp_folder_created_by_us=False,
        ffmpeg_path="/usr/bin/ffmpeg",
        log_level="INFO",
    )
    Path(cfg.tmp_folder).mkdir(exist_ok=True)
    cfg.working_tmp_folder = cfg.tmp_folder
    counts = {"bif": 0, "chapter": 0}
    original_bif = multi_server.generate_images
    original_chapter = chapters.extract_chapter_frame

    # Spies count calls while executing the real functions and real FFmpeg unchanged.
    def bif(*args, **kwargs):
        counts["bif"] += 1
        return original_bif(*args, **kwargs)

    def chapter(*args, **kwargs):
        counts["chapter"] += 1
        return original_chapter(*args, **kwargs)

    multi_server.generate_images = bif
    chapters.extract_chapter_frame = chapter
    results = {}

    def run(label, **kwargs):
        counts.update(bif=0, chapter=0)
        result = multi_server.process_canonical_path(str(SOURCE), registry, cfg, use_frame_cache=False, **kwargs)
        results[label] = {
            "status": result.status.value,
            "counts": dict(counts),
            "publishers": [{"status": p.status.value, "artifacts": p.artifacts} for p in result.publishers],
        }
        print(label, results[label], flush=True)
        return result

    try:
        bifpath = PlexBundleAdapter.bundle_bif_path(str(FOLDER), calculate_plex_hash(SOURCE))
        folder = bifpath.parent.parent / "Chapters"
        result = run("initial_real_generation", regenerate=True)
        for _ in range(3):
            if result.publishers[0].artifacts.get("chapters", {}).get("status") == "ready":
                break
            time.sleep(1)
            result = run("initial_followup")
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        assert read_bif_metadata(str(bifpath))
        bif_before = (bifpath.stat().st_mtime_ns, hashlib.sha256(bifpath.read_bytes()).hexdigest())
        (folder / "chapter2.jpg").unlink()
        result = run("chapter_backfill")
        assert counts == {"bif": 0, "chapter": 1}
        # The real BIF publication may have requested a concurrent partial
        # scan. A changed DB snapshot correctly defers registration; retry
        # its verification without decoding the already complete images.
        for _ in range(5):
            if result.publishers[0].artifacts["chapters"]["status"] == "ready":
                break
            time.sleep(1)
            result = run("chapter_backfill_registration_followup")
            assert counts == {"bif": 0, "chapter": 0}
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        assert bif_before == (bifpath.stat().st_mtime_ns, hashlib.sha256(bifpath.read_bytes()).hexdigest())
        db = FOLDER / "Plug-in Support/Databases/com.plexapp.plugins.library.db"
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            assert conn.execute("SELECT file FROM media_parts WHERE id=1").fetchone() == (
                "/media/Chapter Spike (2026)/Chapter Spike (2026).mkv",
            )
            conn.execute(
                "UPDATE taggings SET thumb_url='' WHERE metadata_item_id=1 AND tag_id IN (SELECT id FROM tags WHERE tag_type=9)"
            )
            conn.commit()
        result = run("registration_repair")
        assert counts == {"bif": 0, "chapter": 0}
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        before_analyze = [
            c.get("thumb") for c in plex.query("/library/metadata/1?includeChapters=1").findall(".//Chapter")
        ]
        response = requests.put(URL + "/library/metadata/1/analyze", timeout=10)
        response.raise_for_status()
        time.sleep(5)
        after_analyze = [
            c.get("thumb") for c in plex.query("/library/metadata/1?includeChapters=1").findall(".//Chapter")
        ]
        result = run("after_real_plex_analyze")
        assert counts == {"bif": 0, "chapter": 0}
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        results["after_real_plex_analyze"]["references_changed_by_analyze"] = before_analyze != after_analyze
        assert bif_before == (bifpath.stat().st_mtime_ns, hashlib.sha256(bifpath.read_bytes()).hexdigest())
        result = run("idempotent_replay")
        assert counts == {"bif": 0, "chapter": 0}
        assert result.status is multi_server.MultiServerStatus.SKIPPED
        results["real_flask_manual_job"] = verify_web_job(raw, SOURCE, folder, bifpath)
        registry.get_config("chapter-lab").output["chapter_thumbnails"] = False
        refs_before = [
            c.get("thumb") for c in plex.query("/library/metadata/1?includeChapters=1").findall(".//Chapter")
        ]
        result = run("disabled")
        assert counts == {"bif": 0, "chapter": 0} and result.publishers[0].artifacts == {}
        assert [
            c.get("thumb") for c in plex.query("/library/metadata/1?includeChapters=1").findall(".//Chapter")
        ] == refs_before
        direct = []
        for index, ref in enumerate(refs_before, 1):
            response = requests.get(URL + ref, timeout=10)
            response.raise_for_status()
            assert response.content == (folder / f"chapter{index}.jpg").read_bytes()
            photo = requests.get(
                URL + "/photo/:/transcode", params={"url": ref, "width": 320, "height": 180}, timeout=10
            )
            photo.raise_for_status()
            direct.append(
                {
                    "index": index,
                    "direct_sha256": hashlib.sha256(response.content).hexdigest(),
                    "photo_bytes": len(photo.content),
                }
            )
        results["served_source_frames"] = direct
        (ROOT / "evidence/pipeline-live.json").write_text(json.dumps(results, indent=2) + "\n")
        print("REAL_PIPELINE_PROOF_PASSED", flush=True)
    finally:
        multi_server.generate_images = original_bif
        chapters.extract_chapter_frame = original_chapter
        http.shutdown()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
