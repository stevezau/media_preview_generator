"""Opt-in live verification against a disposable, unclaimed Plex container.

Run from the repository root with the project Python environment::

    python tests/integration/verify_plex_local_hash.py --run \
        --image plexinc/pms-docker:latest --report /tmp/plex-local-hash.json

Requires an already downloaded Plex image, Docker, and FFmpeg. Creates only its
own temporary config/media and container; no existing server or credentials are
used. Reports the exact image/version. Plex serves unclaimed local requests on
an automatically assigned loopback-only host port. Its container and files are
removed on exit; a container cleanup failure preserves files and reports their path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def command(*args: str) -> str:
    """Run a local command, returning its standard output."""
    return subprocess.check_output(args, text=True).strip()


ProbeResult = TypeVar("ProbeResult")


def wait_until(check: Callable[[], ProbeResult], *, timeout: int = 60) -> ProbeResult:
    """Poll a local server condition within a bounded deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (requests.RequestException, ET.ParseError):
            pass
        time.sleep(0.5)
    raise AssertionError("Disposable Plex did not reach the expected state")


def main() -> None:
    """Exercise local hashing, offline publication, adoption, and cleanup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicitly create the disposable live test environment")
    parser.add_argument("--image", default="plexinc/pms-docker:latest")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to opt into Docker and live Plex verification")
    image_id = json.loads(command("docker", "image", "inspect", args.image))[0]["Id"]
    root = Path(tempfile.mkdtemp(prefix="plex-local-hash-"))
    name = "plex-local-hash-" + uuid.uuid4().hex[:10]
    media = root / "media"
    media.mkdir()
    plex_config = root / "config/Library/Application Support/Plex Media Server"
    plex_config.mkdir(parents=True)
    (plex_config / "Preferences.xml").write_text(
        f'<Preferences allowedNetworks="0.0.0.0/0" FriendlyName="DisposableLocalHashProbe" ProcessedMachineIdentifier="{name}" '
        'GenerateBIFBehavior="never" FSEventLibraryUpdatesEnabled="0" '
        'ScheduledLibraryUpdatesEnabled="0" />'
    )
    os.environ["CONFIG_DIR"] = str(root / "appconfig")
    from media_preview_generator.config import Config
    from media_preview_generator.output import BifBundle, PlexBundleAdapter
    from media_preview_generator.output.journal import get_plex_refresh_pending
    from media_preview_generator.output.plex_hash import calculate_plex_hash
    from media_preview_generator.processing.generator import failure_scope
    from media_preview_generator.processing.multi_server import (
        MultiServerStatus,
        PublisherStatus,
        _publish_one,
        process_canonical_path,
    )
    from media_preview_generator.servers import Library, PlexServer, ServerConfig, ServerRegistry, ServerType

    report: dict = {"image_id": image_id}
    started = False
    try:
        command(
            "docker",
            "run",
            "--pull",
            "never",
            "-d",
            "--name",
            name,
            "--label",
            "com.centurylinklabs.watchtower.enable=false",
            "-p",
            "127.0.0.1::32400",
            "-v",
            f"{root / 'config'}:/config",
            "-v",
            f"{media}:/media:ro",
            "-e",
            "TZ=UTC",
            image_id,
        )
        started = True
        port = command("docker", "port", name, "32400/tcp").rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"

        def request(method: str, endpoint: str, **kwargs: Any) -> requests.Response:
            response = requests.request(method, url + endpoint, timeout=kwargs.pop("timeout", 10), **kwargs)
            if not response.ok:
                raise requests.HTTPError(f"{method} {endpoint}: {response.status_code} {response.text[:200]}")
            return response

        def xml(endpoint: str) -> ET.Element:
            return ET.fromstring(request("GET", endpoint).content)

        wait_until(lambda: xml("/identity").get("startState") is None, timeout=120)
        report["plex_version"] = xml("/identity").get("version")
        small = media / "Small (2026).mp4"
        command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=1",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(small),
        )
        small_bytes = small.read_bytes()
        assert len(small_bytes) < 65535
        for size in (65535, 65536, 65537, 100000):
            (media / f"Boundary{size} (2026).mp4").write_bytes(small_bytes + bytes(size - len(small_bytes)))
        before = media / "BeforeScan (2026).mkv"
        command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-t",
            "15",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(before),
        )
        frames = root / "frames"
        frames.mkdir()
        command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(before),
            "-vf",
            "fps=1/5,scale=320:-1",
            str(frames / "%010d.jpg"),
        )
        adapter = PlexBundleAdapter(str(plex_config), 5)

        def publish(path: Path) -> Path:
            bundle = BifBundle(str(path), frames, None, 5, 320, 180, len(list(frames.glob("*.jpg"))))
            outputs = adapter.compute_output_paths(bundle, None, None)
            adapter.publish(bundle, outputs, None)
            return outputs[0]

        before_bif = publish(before)
        request("POST", "/butler/CleanOldBundles")
        wait_until(lambda: not before_bif.exists())
        report["unindexed_cleanup_removed_bif"] = True
        before_bif = publish(before)
        request(
            "POST",
            "/library/sections",
            timeout=45,
            params={
                "name": "Disposable Synthetic",
                "type": "movie",
                "agent": "tv.plex.agents.movie",
                "scanner": "Plex Movie",
                "language": "en-US",
                "location": "/media",
            },
        )
        section = xml("/library/sections")[0].get("key")

        def items() -> list[tuple[str, ET.Element]]:
            return [
                (video.get("ratingKey", ""), part)
                for video in xml(f"/library/sections/{section}/all")
                for part in video.findall("./Media/Part")
            ]

        def find_part(filename: str) -> tuple[str, ET.Element] | None:
            return next(((key, part) for key, part in items() if Path(part.get("file", "")).name == filename), None)

        wait_until(lambda: len(items()) == 6)
        report["hash_vectors"] = []
        database = plex_config / "Plug-in Support/Databases/com.plexapp.plugins.library.db"
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            for file, size, expected in connection.execute("SELECT file,size,hash FROM media_parts"):
                actual = calculate_plex_hash(media / Path(file).name)
                assert actual == expected, (file, expected, actual)
                report["hash_vectors"].append(
                    {"file": Path(file).name, "size": size, "plex": expected, "local": actual}
                )

        def verify_served(filename: str, expected: Path) -> dict:
            _, part = wait_until(lambda: find_part(filename))
            assert part.get("indexes") == "sd", part.attrib
            response = request("GET", f"/library/parts/{part.get('id')}/indexes/sd")
            assert response.content == expected.read_bytes()
            assert response.content[:8] == b"\x89BIF\r\n\x1a\n"
            return {"bytes": len(response.content), "sha256": hashlib.sha256(response.content).hexdigest()}

        report["before_scan_adoption"] = verify_served(before.name, before_bif)
        request("POST", "/butler/CleanOldBundles")
        time.sleep(2)
        report["indexed_cleanup_preserved_bif"] = verify_served(before.name, before_bif)

        small_key, small_part = find_part(small.name)
        assert small_part.get("indexes") is None
        small_bif = publish(small)
        assert request("GET", f"/library/parts/{small_part.get('id')}/indexes/sd").content == small_bif.read_bytes()
        report["indexed_publish_flag_without_analyze"] = find_part(small.name)[1].get("indexes")
        request("PUT", f"/library/metadata/{small_key}/analyze")
        wait_until(lambda: find_part(small.name)[1].get("indexes") == "sd")
        report["indexed_publish_after_analyze"] = verify_served(small.name, small_bif)

        server = PlexServer(
            ServerConfig(
                id="queue-test",
                name="Disposable Queue",
                type=ServerType.PLEX,
                enabled=True,
                url=url,
                auth={},
                timeout=2,
                libraries=[Library(id=section, name="Synthetic", remote_paths=("/media",))],
                path_mappings=[{"plex_prefix": "/media", "local_prefix": str(media)}],
            )
        )
        report["queued_activation"] = []
        for filename, hinted in [("Boundary65535 (2026).mp4", True), ("Boundary100000 (2026).mp4", False)]:
            key, part = find_part(filename)
            assert part.get("indexes") is None
            source = media / filename
            bundle = BifBundle(str(source), frames, None, 5, 320, 180, len(list(frames.glob("*.jpg"))))
            began = time.monotonic()
            result = _publish_one(server, adapter, bundle, key if hinted else None, skip_if_exists=False)
            seconds = time.monotonic() - began
            assert result.status is PublisherStatus.PUBLISHED
            wait_until(lambda filename=filename: find_part(filename)[1].get("indexes") == "sd")
            served = verify_served(filename, result.output_paths[0])
            wait_until(
                lambda result=result, source=source: get_plex_refresh_pending(
                    result.output_paths, str(source), server.id
                )
                is None
            )
            report["queued_activation"].append(
                {"hinted": hinted, "publish_seconds": seconds, "pending_notification_cleared": True, **served}
            )

        replacement_key, _ = find_part(before.name)
        old_hash = calculate_plex_hash(before)
        replacement = root / "replacement.mkv"
        command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-vf",
            "hue=h=90",
            "-t",
            "19",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(replacement),
        )
        replacement.replace(before)
        replacement_frames = root / "replacement-frames"
        replacement_frames.mkdir()
        command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(before),
            "-vf",
            "fps=1/5,scale=320:-1",
            str(replacement_frames / "%010d.jpg"),
        )
        new_hash = calculate_plex_hash(before)
        assert new_hash != old_hash
        replacement_bundle = BifBundle(
            str(before),
            replacement_frames,
            None,
            5,
            320,
            180,
            len(list(replacement_frames.glob("*.jpg"))),
            prefetched_bundle_metadata=((old_hash, "/media/" + before.name),),
        )
        replacement_result = _publish_one(
            server,
            adapter,
            replacement_bundle,
            replacement_key,
            skip_if_exists=False,
        )
        assert replacement_result.status is PublisherStatus.PUBLISHED
        assert replacement_result.output_paths[0] != before_bif

        def indexed_replacement_hash() -> str | None:
            tree = xml(f"/library/metadata/{replacement_key}/tree")
            return next(
                part.get("hash")
                for part in tree.findall(".//MediaPart")
                if Path(part.get("file", "")).name == before.name
            )

        report["same_path_replacement"] = {"old_hash": old_hash, "new_local_hash": new_hash}
        try:
            wait_until(lambda: indexed_replacement_hash() == new_hash, timeout=30)
        except AssertionError:
            report["same_path_replacement"]["observed_plex_hash"] = indexed_replacement_hash()
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report["same_path_replacement"]), flush=True)
            raise
        report["same_path_replacement"].update(
            plex_hash=indexed_replacement_hash(),
            **verify_served(before.name, replacement_result.output_paths[0]),
        )
        wait_until(lambda: get_plex_refresh_pending(replacement_result.output_paths, str(before), server.id) is None)
        report["same_path_replacement"]["pending_notification_cleared"] = True

        offline = media / "OfflinePipeline (2026).mkv"
        command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-t",
            "20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(offline),
        )
        config = Config(
            plex_url=url,
            plex_token="",
            plex_timeout=1,
            plex_verify_ssl=True,
            plex_libraries=[],
            plex_config_folder=str(plex_config),
            plex_local_videos_path_mapping="",
            plex_videos_path_mapping="",
            path_mappings=[],
            plex_bif_frame_interval=5,
            thumbnail_quality=4,
            regenerate_thumbnails=False,
            sort_by=None,
            gpu_threads=0,
            cpu_threads=1,
            ffmpeg_threads=2,
            tmp_folder=str(root / "tmp"),
            tmp_folder_created_by_us=True,
            ffmpeg_path=shutil.which("ffmpeg") or "ffmpeg",
            log_level="INFO",
            working_tmp_folder=str(root / "tmp"),
        )
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "isolated",
                    "name": "Disposable",
                    "enabled": True,
                    "type": "plex",
                    "url": url,
                    "auth": {"token": ""},
                    "timeout": 1,
                    "libraries": [{"id": section, "name": "Synthetic", "remote_paths": ["/media"], "enabled": True}],
                    "path_mappings": [{"plex_prefix": "/media", "local_prefix": str(media)}],
                    "output": {"adapter": "plex_bundle", "plex_config_folder": str(plex_config), "frame_interval": 5},
                }
            ]
        )
        command("docker", "stop", "-t", "10", name)
        began = time.monotonic()
        with failure_scope("plex-local-hash-verification"):
            outcome = process_canonical_path(
                str(offline), registry, config, use_frame_cache=False, schedule_retry_on_not_indexed=False
            )
        assert outcome.status is MultiServerStatus.PUBLISHED, outcome
        assert get_plex_refresh_pending(outcome.publishers[0].output_paths, str(offline), "isolated") is not None
        report["offline_pipeline"] = {
            "status": outcome.status.value,
            "frames": outcome.frame_count,
            "seconds": round(time.monotonic() - began, 3),
            "pending_notification_retained": True,
        }
        command("docker", "start", name)
        port = command("docker", "port", name, "32400/tcp").rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        wait_until(lambda: xml("/identity").get("startState") is None, timeout=120)
        request("GET", f"/library/sections/{section}/refresh")
        wait_until(lambda: find_part(offline.name))
        report["offline_pipeline_adoption"] = verify_served(offline.name, outcome.publishers[0].output_paths[0])
        encoded = json.dumps(report, indent=2)
        if args.report:
            args.report.write_text(encoded + "\n")
        print(encoded)
    finally:
        verification_failed = sys.exc_info()[0] is not None
        try:
            if started:
                command("docker", "rm", "-f", name)
        except subprocess.SubprocessError:
            print(
                f"Could not remove disposable container {name}; preserved its synthetic files at {root}",
                file=sys.stderr,
            )
            if not verification_failed:
                raise
        else:
            shutil.rmtree(root)


if __name__ == "__main__":
    main()
