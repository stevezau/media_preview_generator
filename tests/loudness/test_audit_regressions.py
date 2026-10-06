"""Regression checks found while auditing production loudness backlogs."""

import threading
from pathlib import Path
from xml.etree.ElementTree import Element

import pytest

from media_preview_generator.loudness import job
from media_preview_generator.web.jobs import JobStatus

from .test_job_lifecycle import Lifecycle, lifecycle  # noqa: F401


def test_unreadable_retry_input_preserves_full_parent_totals(lifecycle: Lifecycle) -> None:  # noqa: F811
    manager = lifecycle.manager
    head = manager.create_job(kind="loudness", config={"is_retry_chain": True})
    totals = {job.WRITTEN: 10000, job.WAITING: 501}
    manager.set_job_outcome(head.id, totals)
    manager.record_file_result(head.id, "/media/one-visible-success.mkv", job.WRITTEN)
    paths = [f"/media/waiting-{index}.mkv" for index in range(501)]
    baseline = {
        "outcome": totals,
        "publishers": [],
        "files": {path: {"file": path, "outcome": job.WAITING, "servers": []} for path in paths},
    }
    child = job.create_loudness_job(
        library_name="Retry",
        priority=2,
        source="manual",
        file_paths=paths,
        parent_job_id=head.id,
        retry_attempt=1,
        max_retries=1,
        retry_baseline=baseline,
    )
    (Path(manager.config_dir) / "loudness_inputs" / child.config["file_paths_ref"]).unlink()

    job.run_loudness_job(child.id)

    assert child.status is JobStatus.FAILED
    assert head.progress.outcome == totals
    assert head.error and "501" in head.error
    assert lifecycle.analyses == []


def test_restart_keeps_publishers_for_files_finished_before_interruption(lifecycle: Lifecycle) -> None:  # noqa: F811
    lifecycle.api_ready = True
    first = lifecycle.add_file("first.mkv")
    second = lifecycle.add_file("second.mkv")
    parent = lifecycle.start([first])
    # A restart recovers the same job and its first file's durable result,
    # then enumerates both its finished and unfinished files again.
    lifecycle.manager.merge_job_config(parent.id, {"file_paths": [first, second]})
    parent.status = JobStatus.PENDING
    parent.completed_at = None

    job.run_loudness_job(parent.id)

    assert parent.status is JobStatus.COMPLETED
    assert parent.progress.outcome[job.WRITTEN] == 1
    assert parent.progress.outcome[job.UP_TO_DATE] == 1
    assert sum(parent.publishers[0]["counts"].values()) == 2
    assert lifecycle.analyses == [(first, 1), (second, 1)]


def test_restart_rechecks_a_finished_path_replaced_before_resume(lifecycle: Lifecycle) -> None:  # noqa: F811
    lifecycle.api_ready = True
    path = lifecycle.add_file("replaced.mkv")
    parent = lifecycle.start([path])
    unfinished = lifecycle.add_file("unfinished.mkv")
    lifecycle.media.joinpath("replaced.mkv").write_bytes(b"a new source Plex has not indexed")
    lifecycle.manager.merge_job_config(parent.id, {"file_paths": [path, unfinished]})
    parent.status = JobStatus.PENDING
    parent.completed_at = None

    job.run_loudness_job(parent.id)

    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome.get(job.WAITING) == 1
    assert lifecycle.children(parent)


def test_restart_after_last_file_does_not_wait_forever_on_an_empty_tracker(lifecycle: Lifecycle) -> None:  # noqa: F811
    lifecycle.api_ready = True
    path = lifecycle.add_file("finished.mkv")
    parent = lifecycle.start([path])
    parent.status = JobStatus.PENDING
    parent.completed_at = None
    finished = threading.Event()

    def resume() -> None:
        try:
            job.run_loudness_job(parent.id)
        finally:
            finished.set()

    runner = threading.Thread(target=resume, daemon=True)
    runner.start()
    try:
        assert finished.wait(timeout=2), "Restart submitted zero files and kept the job running"
        assert parent.status is JobStatus.COMPLETED
    finally:
        lifecycle.manager.cancel_job(parent.id)
        lifecycle.dispatcher.cancel_job(parent.id)
        runner.join(timeout=3)


@pytest.mark.parametrize("native_complete", [False, True], ids=["new-analysis", "existing-native"])
def test_source_replaced_during_api_verification_is_not_reported_complete(
    lifecycle: Lifecycle,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    native_complete: bool,
) -> None:
    lifecycle.api_ready = True
    path = lifecycle.add_file("changing.mkv")
    if native_complete:
        lifecycle.start([path])
    original = lifecycle.query

    def query(endpoint: str) -> Element:
        response = original(endpoint)
        if endpoint.startswith("/library/metadata/"):
            lifecycle.media.joinpath("changing.mkv").write_bytes(b"source replaced during Plex response")
        return response

    monkeypatch.setattr(lifecycle, "query", query)
    parent = lifecycle.start([path])

    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome[job.WAITING] == 1
    assert len(lifecycle.children(parent)) == 1
