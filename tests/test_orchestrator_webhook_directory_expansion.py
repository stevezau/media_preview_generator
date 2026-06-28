"""Regression: a manual/webhook job pointed at a *folder* must expand the
folder into the video files inside it before dispatch.

Issue #266: a user ran Manual Generation against a TV show folder
(``…/Ben 10 (2005)``) instead of a single file. The unified dispatch engine
(introduced in #243) builds ``ProcessableItem``s directly and never called
``_expand_directory_to_media_files`` — that helper only ran in the legacy
``get_media_items_by_paths`` path. So the folder reached
``process_canonical_path`` as-is, failed ``os.path.isfile`` and was reported
as "Source video file is missing on disk", then retried forever.

These tests pin the wiring: ``_run_webhook_paths_phase`` must turn a folder
into one dispatch item per contained video file.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from media_preview_generator.jobs.orchestrator import _run_webhook_paths_phase


def _fresh_totals() -> dict:
    return {"processed": 0, "successful": 0, "failed": 0, "cancelled": False}


def _run(config, registry, dispatch_items):
    """Invoke the phase with the owner-resolution + logging helpers stubbed
    so the test exercises only the directory-expansion wiring."""
    with (
        patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            side_effect=lambda path, cfgs, **kw: (path, [SimpleNamespace(server_id="s1")]),
        ),
        patch("media_preview_generator.jobs.orchestrator._log_webhook_owning_servers"),
    ):
        return _run_webhook_paths_phase(
            config,
            registry,
            dispatch_items=dispatch_items,
            progress_callback=None,
            cancel_check=None,
            job_id="job-1",
            totals=_fresh_totals(),
            aggregate_outcome={},
        )


def test_directory_path_expands_into_contained_video_files(tmp_path):
    """A folder is dispatched as one item per video file, not as the folder."""
    show = tmp_path / "Ben 10 (2005)"
    season = show / "Season 01"
    season.mkdir(parents=True)
    (season / "S01E01.mkv").write_text("")
    (season / "S01E02.mp4").write_text("")
    (season / "S01E01.srt").write_text("")  # non-video, must be ignored

    config = MagicMock()
    config.webhook_paths = [str(show)]
    config.webhook_item_id_hints = {}

    server_cfg = SimpleNamespace(path_mappings=[])
    registry = MagicMock()
    registry.configs.return_value = [server_cfg]

    captured: dict = {}

    def fake_dispatch(items, label):
        captured["items"] = items
        return {"completed": len(items), "failed": 0, "cancelled": False, "outcome": {}}

    _run(config, registry, fake_dispatch)

    dispatched_paths = sorted(item.canonical_path for item in captured["items"])
    assert dispatched_paths == [
        str(season / "S01E01.mkv"),
        str(season / "S01E02.mp4"),
    ]


def test_plain_file_path_passes_through_unchanged(tmp_path):
    """A file path (the common webhook case) is dispatched as-is."""
    movie = tmp_path / "Movie (2024)" / "movie.mkv"
    movie.parent.mkdir(parents=True)
    movie.write_text("")

    config = MagicMock()
    config.webhook_paths = [str(movie)]
    config.webhook_item_id_hints = {}

    registry = MagicMock()
    registry.configs.return_value = [SimpleNamespace(path_mappings=[])]

    captured: dict = {}

    def fake_dispatch(items, label):
        captured["items"] = items
        return {"completed": len(items), "failed": 0, "cancelled": False, "outcome": {}}

    _run(config, registry, fake_dispatch)

    assert [item.canonical_path for item in captured["items"]] == [str(movie)]


def test_directory_expanded_via_server_path_mappings(tmp_path):
    """A folder that only exists under a mapped local prefix is still expanded."""
    local_root = tmp_path / "data_16tb" / "TV" / "Show (2024)"
    local_root.mkdir(parents=True)
    (local_root / "S01E01.mkv").write_text("")

    config = MagicMock()
    config.webhook_paths = ["/data/TV/Show (2024)"]
    config.webhook_item_id_hints = {}

    server_cfg = SimpleNamespace(
        path_mappings=[{"plex_prefix": "/data", "local_prefix": str(tmp_path / "data_16tb"), "webhook_prefixes": []}]
    )
    registry = MagicMock()
    registry.configs.return_value = [server_cfg]

    captured: dict = {}

    def fake_dispatch(items, label):
        captured["items"] = items
        return {"completed": len(items), "failed": 0, "cancelled": False, "outcome": {}}

    _run(config, registry, fake_dispatch)

    assert [item.canonical_path for item in captured["items"]] == [str(local_root / "S01E01.mkv")]
