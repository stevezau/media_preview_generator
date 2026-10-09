"""Busy CPU loudness jobs must leave start-up available for independent GPU work."""

import threading
import time

from media_preview_generator.loudness import job
from media_preview_generator.web.jobs import JobStatus

from .test_job_lifecycle import Lifecycle, lifecycle  # noqa: F401


def test_busy_cpu_loudness_lane_holds_no_start_up_slot(lifecycle: Lifecycle, make_gate, monkeypatch):  # noqa: F811
    """Loudness jobs queued behind the one CPU worker have all started up (listed their files) and given their
    slot back, so independent GPU work starts up straight away."""
    lifecycle.api_ready = True
    gate = make_gate(1, lambda kind: 1 if kind == "loudness" else 4)
    monkeypatch.setattr(job, "get_job_gate", lambda: gate)
    started = threading.Event()
    finish = threading.Event()
    original = lifecycle.popen

    def blocked_analysis(command, **kwargs):
        process = original(command, **kwargs)
        wait_for_exit = process.wait

        def wait(**options):
            started.set()
            assert finish.wait(10), "test did not release simulated FFmpeg"
            return wait_for_exit(**options)

        process.wait = wait
        return process

    monkeypatch.setattr(job.analyze.subprocess, "Popen", blocked_analysis)
    entries = [
        lifecycle.manager.create_job(
            kind="loudness",
            priority=2,
            config={"source": "manual", "file_paths": [lifecycle.add_file(f"audio-{index}.mkv")]},
        )
        for index in range(4)
    ]
    threads = [threading.Thread(target=job.run_loudness_job, args=(entry.id,), daemon=True) for entry in entries]
    try:
        for thread in threads:
            thread.start()
        assert started.wait(3)
        deadline = time.monotonic() + 3
        while sum(entry.status is JobStatus.RUNNING for entry in entries) < 4 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert sum(entry.status is JobStatus.RUNNING for entry in entries) == 4
        assert gate.snapshot() == (0, 0, 1), "every job gave its start-up slot back after submitting its files"
        preview_deadline = time.monotonic() + 1
        assert gate.acquire(priority=2, kind="previews", cancel_check=lambda: time.monotonic() > preview_deadline), (
            "GPU work must start up straight away while the CPU lane is busy"
        )
        gate.release()
    finally:
        for entry in entries:
            lifecycle.manager.request_cancellation(entry.id)
            lifecycle.manager.cancel_job(entry.id)
        finish.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=3)
        assert not any(thread.is_alive() for thread in threads)
