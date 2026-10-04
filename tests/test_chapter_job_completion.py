"""Chapter failures warn without turning an already published scrubber into a failure."""

from __future__ import annotations

import pytest

from media_preview_generator.jobs.orchestrator import count_successes, merge_chain_publishers_best_per_path
from media_preview_generator.web.jobs import JobManager, JobStatus
from media_preview_generator.web.routes.job_runner import _chapter_completion_warning, _classify_job_completion


@pytest.mark.parametrize(
    "status,include_pending,phrase",
    [
        ("published_chapters_failed", True, "chapter thumbnails failed for 1"),
        ("published_chapters_failed", False, "chapter thumbnails failed for 1"),
        ("published_pending_chapters", True, "chapter thumbnails are still pending for 1"),
        ("published_pending_chapters", False, None),
        ("published", True, None),
        ("skipped_output_exists", True, None),
    ],
)
def test_direct_completion_preserves_success_and_warns_for_incomplete_chapters(
    tmp_path, status, include_pending, phrase
):
    publishers = [{"server_id": "plex", "counts": {status: 1}}]
    warning = _chapter_completion_warning(publishers, include_pending=include_pending)
    if phrase is None:
        assert warning is None
        return
    assert phrase in warning
    tally = {"generated": 1}
    assert count_successes(tally) == 1
    assert (
        _classify_job_completion(
            failures=[],
            outcome=tally,
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=1,
            resolved_count=1,
        )
        == "warning"
    )
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Movies")
    manager.set_job_outcome(job.id, tally)
    completed = manager.complete_job(job.id, warning=warning)
    assert completed.status is JobStatus.COMPLETED
    assert completed.error == warning
    assert completed.progress.outcome == tally


@pytest.mark.parametrize("final_status", ["published_chapters_failed", "published", "skipped_output_exists"])
def test_chain_uses_latest_artifact_result_and_clears_warning_after_success(tmp_path, final_status):
    rows = [
        {"file": "/movie.mkv", "servers": [{"id": "plex", "status": status}]}
        for status in ("published_pending_chapters", final_status)
    ]
    publishers = merge_chain_publishers_best_per_path(rows)
    warning = _chapter_completion_warning(publishers, include_pending=True)
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Movies")
    manager.set_job_outcome(job.id, {"generated": 1})
    manager.complete_job(job.id, warning="Chapters pending")
    completed = manager.upsert_retry_chain_job(
        canonical_path="",
        basename="Movies",
        attempt=1,
        max_attempts=3,
        next_run_at=None,
        wait_seconds=None,
        outcome="completed",
        reason=warning,
        originating_job_id=job.id,
        publishers=publishers,
        successes=1,
    )
    assert completed.status is JobStatus.COMPLETED
    assert completed.progress.outcome == {"generated": 1}
    assert completed.error == warning
    if final_status == "published_chapters_failed":
        assert "chapter thumbnails failed" in warning
    else:
        assert warning is None


def test_mixed_servers_count_outputs_without_double_counting_successes():
    publishers = [
        {"counts": {"published_chapters_failed": 2, "published_pending_chapters": 1}},
        {"counts": {"published_chapters_failed": 1, "published": 8}},
        {"counts": {"published_chapters_failed": "bad"}},
        None,
    ]
    warning = _chapter_completion_warning(publishers, include_pending=True)
    assert "failed for 3 server item(s)" in warning
    assert "pending for 1 server item(s)" in warning


@pytest.mark.parametrize("reseed", [False, True])
def test_large_success_scan_does_not_drop_chapter_failure_or_its_later_repair(tmp_path, monkeypatch, reseed):
    monkeypatch.setattr(JobManager, "_FILE_RESULTS_PER_OUTCOME_CAP", 3)
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Movies")
    for index in range(200):
        manager.record_file_result(job.id, f"/ok-{index}.mkv", "generated")
    broken = {"server_id": "plex", "status": "published_chapters_failed"}
    manager.record_file_result(job.id, "/chapters.mkv", "generated", servers=[broken])
    if reseed:
        manager._file_result_counts.clear()
    waiting = {"server_id": "plex", "status": "published_pending_chapters"}
    manager.record_file_result(job.id, "/waiting.mkv", "generated", servers=[waiting])
    rows = manager.get_file_results(job.id, dedup_by_path=False)
    assert {row["file"] for row in rows} >= {"/chapters.mkv", "/waiting.mkv"}
    assert len(rows) == 6  # Three successes, their cap marker, and both incomplete artifacts.
    warning = _chapter_completion_warning(merge_chain_publishers_best_per_path(rows), include_pending=True)
    assert "failed for 1" in warning and "pending for 1" in warning
    for path in ("/chapters.mkv", "/waiting.mkv"):
        manager.record_file_result(
            job.id, path, "generated", servers=[{"server_id": "plex", "status": "published"}], uncapped=True
        )
    rows = manager.get_file_results(job.id, dedup_by_path=False)
    assert _chapter_completion_warning(merge_chain_publishers_best_per_path(rows), include_pending=True) is None


def test_chapter_bucket_is_bounded_and_omitted_failures_keep_chain_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(JobManager, "_FILE_RESULTS_PER_OUTCOME_CAP", 2)
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Movies")
    for index in range(20):
        manager.record_file_result(
            job.id,
            f"/failed-{index}.mkv",
            "generated",
            servers=[{"server_id": "plex", "status": "published_chapters_failed"}],
        )
    rows = manager.get_file_results(job.id, dedup_by_path=False)
    assert len(rows) == 3
    assert sum(row["outcome"] == "truncated:chapter_incomplete" for row in rows) == 1
    for index in range(2):
        manager.record_file_result(
            job.id,
            f"/failed-{index}.mkv",
            "generated",
            servers=[{"server_id": "plex", "status": "published"}],
            uncapped=True,
        )
    rows = manager.get_file_results(job.id, dedup_by_path=False)
    warning = _chapter_completion_warning(
        merge_chain_publishers_best_per_path(rows),
        include_pending=True,
        truncated=any(row["outcome"] == "truncated:chapter_incomplete" for row in rows),
    )
    assert "additional chapter thumbnails remain incomplete" in warning
