"""Sanitized fixtures for README screenshot regeneration.

Produces a settings.json + jobs.db pair with plausible — but entirely
fake — data for docs/images/ captures. No real hostnames, no real
server names. See ``regen_readme.py`` for the capture driver that
consumes these helpers.

The fake hosts below are RFC-1918 private addresses (``192.168.1.0/24``)
— exactly what a real home LAN install would show, but not routable
anywhere public, so they cannot leak. Each vendor gets its own address
(``PLEX_HOST`` / ``JELLYFIN_HOST`` / ``EMBY_HOST``) so the Servers page
renders three visibly distinct cards; ``APP_HOST`` is the address the
app itself would be reached at (used for webhook URLs in
``regen_readme.py``). The three vendors (plex / emby / jellyfin) are
seeded so the Servers page renders a multi-vendor card row, matching
the README claim that the tool supports all three.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

APP_HOST = "192.168.1.10"
APP_PORT = 8080
PLEX_HOST = "192.168.1.20"
JELLYFIN_HOST = "192.168.1.21"
EMBY_HOST = "192.168.1.22"

FAKE_SERVERS: list[dict[str, Any]] = [
    {
        "id": "plex-home",
        "type": "plex",
        "name": "Home Plex",
        "enabled": True,
        "url": f"http://{PLEX_HOST}:32400",
        "auth": {"token": "x" * 20},
        "verify_ssl": False,
        "timeout": 30,
        "libraries": [
            {"id": "1", "title": "Movies", "type": "movie", "enabled": True},
            {"id": "2", "title": "TV Shows", "type": "show", "enabled": True},
            {"id": "3", "title": "Kids", "type": "movie", "enabled": False},
        ],
        "path_mappings": [
            {"local": "/media/movies", "remote": "/data/movies"},
            {"local": "/media/tv", "remote": "/data/tv"},
        ],
        "exclude_paths": [],
        "output": {"plex_config_folder": "/plex"},
        "server_identity": "plex-identity-fake",
    },
    {
        "id": "jellyfin-home",
        "type": "jellyfin",
        "name": "Home Jellyfin",
        "enabled": True,
        "url": f"http://{JELLYFIN_HOST}:8096",
        "auth": {"api_key": "y" * 32},
        "verify_ssl": True,
        "timeout": 30,
        "libraries": [
            {"id": "a1", "title": "Movies", "type": "movie", "enabled": True},
            {"id": "a2", "title": "Shows", "type": "show", "enabled": True},
        ],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {},
        "server_identity": "jellyfin-identity-fake",
    },
    {
        "id": "emby-home",
        "type": "emby",
        "name": "Home Emby",
        "enabled": True,
        "url": f"http://{EMBY_HOST}:8096",
        "auth": {"api_key": "z" * 32},
        "verify_ssl": True,
        "timeout": 30,
        "libraries": [
            {"id": "b1", "title": "Films", "type": "movie", "enabled": True},
        ],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {},
        "server_identity": "emby-identity-fake",
    },
]

# Clean, non-truncated GPU names for the live-detection API stub
# (``/api/system/status``) in ``regen_readme.py``. Real ``lspci``/``nvidia-smi``
# output is often a long raw string (e.g. "Intel Corporation Raptor Lake-S GT1
# [UHD Graphics 770] (rev 04)") that a narrow sidebar column truncates with an
# ellipsis — not something we want in a marketing screenshot. ``device``
# matches the ``device`` key on the corresponding ``gpu_config`` entry above
# so saved per-GPU worker/thread counts bind to the right card instead of
# falling back to defaults.
FAKE_GPUS: list[dict[str, Any]] = [
    {"type": "nvidia", "device": "cuda:0", "name": "NVIDIA TITAN RTX", "status": "ok"},
    {"type": "intel", "device": "/dev/dri/renderD128", "name": "Intel UHD Graphics 770", "status": "ok"},
]
# Per-GPU worker count, aligned index-for-index with FAKE_GPUS. Single
# source of truth for both the gpu_config below and the /api/jobs/workers
# stub in regen_readme.py (the dashboard's "WORKERS" panel) — otherwise the
# two can silently drift and the panel shows a worker count that doesn't
# match the "Workers" steppers shown elsewhere on the same page.
FAKE_GPU_WORKERS = [3, 1]
FAKE_CPU_THREADS = 4


def _base_settings() -> dict[str, Any]:
    return {
        "setup_complete": True,
        "media_servers": FAKE_SERVERS,
        # Legacy Plex fast-path keys still inspected by is_configured().
        # Kept aligned with media_servers[0] so the old code path matches.
        "plex_url": FAKE_SERVERS[0]["url"],
        "plex_token": FAKE_SERVERS[0]["auth"]["token"],
        "plex_config_folder": "/plex",
        "plex_verify_ssl": False,
        "thumbnail_interval": 10,
        "thumbnail_quality": 4,
        "regenerate_thumbnails": False,
        "cpu_threads": FAKE_CPU_THREADS,
        "gpu_config": [
            {
                "index": 0,
                "device": FAKE_GPUS[0]["device"],
                "vendor": "NVIDIA",
                "model": FAKE_GPUS[0]["name"],
                "enabled": True,
                "workers": FAKE_GPU_WORKERS[0],
                "ffmpeg_threads": 2,
            },
            {
                "index": 1,
                "device": FAKE_GPUS[1]["device"],
                "vendor": "Intel",
                "model": FAKE_GPUS[1]["name"],
                "enabled": True,
                "workers": FAKE_GPU_WORKERS[1],
                "ffmpeg_threads": 2,
            },
        ],
        "webhook_enabled": True,
        "webhook_delay": 60,
        "webhook_retry_count": 3,
        "webhook_secret": "",
        "dismissed_notifications": [],
    }


def write_settings(config_dir: str | Path) -> Path:
    """Write a sanitized settings.json into ``config_dir``.

    Returns the path of the written file. The caller is responsible for
    creating ``config_dir`` first if it does not exist.
    """
    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    target = config_path / "settings.json"
    with open(target, "w") as fh:
        json.dump(_base_settings(), fh, indent=2)
    return target


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def seed_jobs(config_dir: str | Path) -> dict[str, Any]:
    """Seed fake job rows into the ``jobs.db`` under ``config_dir``.

    Uses ``JobStorage.upsert`` so the schema stays in lockstep with
    whatever the app currently expects. Safe to call on a fresh config
    directory — JobStorage creates the DB if missing. Also records a
    per-server publish result for the Tears of Steel job, for the Publish
    screenshot's Files tab.

    Returns ``{"count": <rows seeded>, "tears_job_id": <Tears of Steel job id>}``.
    """
    # Imported lazily so importing this module doesn't drag in the whole
    # web app just to read fixture data.
    from media_preview_generator.web.jobs import (  # noqa: PLC0415
        Job,
        JobProgress,
        JobStatus,
        JobStorage,
    )

    db_path = str(Path(config_dir) / "jobs.db")
    storage = JobStorage(db_path)
    try:
        now = datetime.now(UTC)
        rows: list[Job] = []

        # 6 completed jobs: 5 single-file Radarr imports of the lab's open
        # films (one per vendor, matching the screenshots' worker/publish
        # rows) plus one scheduled library pass so the table still shows
        # scale next to the single-file rows.
        completed_fixtures = [
            ("Tears of Steel (2012)", "plex-home", "Home Plex", "plex", 1, 1),
            ("Sintel (2010)", "jellyfin-home", "Home Jellyfin", "jellyfin", 1, 1),
            ("Big Buck Bunny (2008)", "emby-home", "Home Emby", "emby", 1, 1),
            ("Elephants Dream (2006)", "plex-home", "Home Plex", "plex", 1, 1),
            ("Cosmos Laundromat (2015)", "jellyfin-home", "Home Jellyfin", "jellyfin", 1, 1),
            ("Movies", "plex-home", "Home Plex", "plex", 842, 842),
        ]
        for i, (lib, sid, sname, stype, total, processed) in enumerate(completed_fixtures):
            created = now - timedelta(hours=(i + 1) * 3, minutes=7 * i)
            started = created + timedelta(seconds=4)
            finished = started + timedelta(minutes=6 + i * 2)
            rows.append(
                Job(
                    id=str(uuid.uuid4()),
                    status=JobStatus.COMPLETED,
                    created_at=_iso(created),
                    started_at=_iso(started),
                    completed_at=_iso(finished),
                    library_id=f"lib-{i}",
                    library_name=lib,
                    server_id=sid,
                    server_name=sname,
                    server_type=stype,
                    progress=JobProgress(
                        percent=100.0,
                        total_items=total,
                        processed_items=processed,
                        outcome={"created": processed, "skipped": 0, "failed": 0},
                    ),
                    config={"trigger": "manual", "path_count": total},
                )
            )

        # Three more completed jobs to bring the list to 9. All seeded
        # rows MUST be COMPLETED — ``JobManager._load_from_disk`` flips
        # any RUNNING row to FAILED on startup (jobs.py:557-564) and
        # PENDING rows sit in the "interrupted jobs" list. Both outcomes
        # muddy the marketing shot; COMPLETED is the only status that
        # survives the boot flip cleanly.
        extra_completed = [
            ("Stand-up Comedy", "jellyfin-home", "Home Jellyfin", "jellyfin", 61, 61),
            ("Documentaries", "emby-home", "Home Emby", "emby", 138, 138),
            ("Anime", "plex-home", "Home Plex", "plex", 224, 224),
        ]
        for j, (lib, sid, sname, stype, total, processed) in enumerate(extra_completed):
            created = now - timedelta(hours=(6 + j), minutes=11 * j)
            started = created + timedelta(seconds=3)
            finished = started + timedelta(minutes=4 + j)
            rows.append(
                Job(
                    id=str(uuid.uuid4()),
                    status=JobStatus.COMPLETED,
                    created_at=_iso(created),
                    started_at=_iso(started),
                    completed_at=_iso(finished),
                    library_id=f"lib-extra-{j}",
                    library_name=lib,
                    server_id=sid,
                    server_name=sname,
                    server_type=stype,
                    progress=JobProgress(
                        percent=100.0,
                        total_items=total,
                        processed_items=processed,
                        outcome={"created": processed, "skipped": 0, "failed": 0},
                    ),
                    config={"trigger": "manual", "path_count": total},
                )
            )

        for row in rows:
            storage.upsert(row)

        # Record a per-server publish for the Tears of Steel job (D9 shape,
        # Worker._capture_publishers in media_preview_generator/jobs/worker.py:528-566)
        # so its Files tab shows one pill per server — the Publish screenshot.
        from media_preview_generator.web.jobs import JobManager  # noqa: PLC0415

        tears = next(row for row in rows if row.library_name == "Tears of Steel (2012)")
        video = "/media/movies/Tears of Steel (2012)/Tears of Steel (2012).mp4"
        publishers = [
            {"server_id": "plex-home", "server_name": "Home Plex", "server_type": "plex", "adapter_name": "plex_bundle",
             "status": "published", "message": "", "frame_source": "extracted", "canonical_path": video,
             "output_paths": ["/plex/Media/localhost/3/f1c2a9e0d4b7.bundle/Contents/Indexes/index-sd.bif"]},
            {"server_id": "jellyfin-home", "server_name": "Home Jellyfin", "server_type": "jellyfin",
             "adapter_name": "jellyfin_trickplay", "status": "published", "message": "", "frame_source": "extracted",
             "canonical_path": video, "output_paths": ["/media/movies/Tears of Steel (2012)/Tears of Steel (2012).trickplay"]},
            {"server_id": "emby-home", "server_name": "Home Emby", "server_type": "emby", "adapter_name": "emby_sidecar",
             "status": "published", "message": "", "frame_source": "extracted", "canonical_path": video,
             "output_paths": ["/media/movies/Tears of Steel (2012)/Tears of Steel (2012)-320-10.bif"]},
        ]  # fmt: skip
        JobManager(config_dir=str(config_dir)).record_file_result(
            tears.id, video, "generated", worker="GPU Worker 1 (NVIDIA TITAN RTX)", servers=publishers
        )

        return {"count": len(rows), "tears_job_id": tears.id}
    finally:
        storage.close()


__all__ = [
    "APP_HOST",
    "APP_PORT",
    "PLEX_HOST",
    "JELLYFIN_HOST",
    "EMBY_HOST",
    "FAKE_SERVERS",
    "FAKE_GPUS",
    "FAKE_GPU_WORKERS",
    "FAKE_CPU_THREADS",
    "write_settings",
    "seed_jobs",
]
