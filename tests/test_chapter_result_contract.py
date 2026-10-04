"""Chapter details survive file persistence and retry aggregation."""

import pytest

from media_preview_generator.jobs.orchestrator import _publisher_rows_from_result, merge_chain_publishers_best_per_path
from media_preview_generator.processing.multi_server import (
    MultiServerResult,
    MultiServerStatus,
    PublisherResult,
    PublisherStatus,
)
from media_preview_generator.web.jobs import JobManager


def test_chapter_artifacts_survive_serialization_and_jsonl(tmp_path):
    artifacts = {
        "bif": {"status": "published"},
        "chapters": {
            "status": "pending",
            "completed": 2,
            "total": 3,
            "message": "One chapter is pending",
        },
    }
    result = MultiServerResult(
        "/movie.mkv",
        MultiServerStatus.PUBLISHED,
        [
            PublisherResult(
                "plex",
                "Plex",
                "plex_bundle",
                PublisherStatus.PUBLISHED_PENDING_CHAPTERS,
                artifacts=artifacts,
            )
        ],
    )
    rows = _publisher_rows_from_result(result, "/movie.mkv")
    assert rows[0]["artifacts"] == artifacts
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Chapters")
    manager.record_file_result(job.id, "/movie.mkv", "generated", servers=rows)
    stored = manager.get_file_results(job.id)[0]["servers"][0]
    assert stored["status"] == "published_pending_chapters"
    assert stored["artifacts"] == artifacts


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["published", "published_pending_chapters"], "published_pending_chapters"),
        (["published_pending_chapters", "published_chapters_failed"], "published_chapters_failed"),
        (["published_pending_chapters", "published"], "published"),
        (["published_pending_chapters", "skipped_output_exists"], "skipped_output_exists"),
    ],
)
def test_retry_merge_does_not_hide_incomplete_chapters_behind_bif_success(statuses, expected):
    rows = [{"file": "/movie.mkv", "servers": [{"id": "plex", "status": status}]} for status in statuses]
    aggregate = merge_chain_publishers_best_per_path(rows)
    assert aggregate[0]["counts"] == {expected: 1}


@pytest.mark.parametrize("original_source", ["extracted", "cache_hit", "output_existed"])
def test_chapter_only_repair_keeps_original_bif_provenance(tmp_path, original_source):
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Chapters")
    for status, source in [("published_pending_chapters", original_source), ("published", "output_existed")]:
        manager.record_file_result(
            job.id,
            "/movie.mkv",
            "generated",
            servers=[
                {
                    "server_id": "plex",
                    "status": status,
                    "frame_source": source,
                }
            ],
        )
    rows = manager.get_file_results(job.id, dedup_by_path=False)
    aggregate = merge_chain_publishers_best_per_path(rows)
    assert aggregate[0]["counts"] == {"published": 1}
    assert aggregate[0]["frame_sources"] == {original_source: 1}
