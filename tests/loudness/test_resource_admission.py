"""Busy CPU loudness jobs must leave admission available for independent GPU work."""

import threading
import time

from media_preview_generator.loudness import job
from media_preview_generator.web.job_gate import JobGate
from media_preview_generator.web.jobs import JobStatus

from .test_job_lifecycle import Lifecycle, lifecycle  # noqa: F401


def test_one_cpu_loudness_lane_does_not_fill_four_ordinary_job_slots(lifecycle: Lifecycle, monkeypatch):  # noqa: F811
    lifecycle.api_ready = True
    gate = JobGate(lambda: 5, kind_capacity_provider=lambda kind: 1 if kind == "loudness" else 4)
    monkeypatch.setattr(job, "get_job_gate", lambda: gate)
    started = threading.Event()
    finish = threading.Event()
    original = lifecycle.popen

    def blocked_analysis(command, **kwargs):
        process = original(command, **kwargs)
        communicate = process.communicate

        def wait(**options):
            started.set()
            assert finish.wait(10), "test did not release simulated FFmpeg"
            return communicate(**options)

        process.communicate = wait
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
        threads[0].start()
        assert started.wait(3)
        for thread in threads[1:]:
            thread.start()
        # Every real runner must reach admission before we check occupancy.
        deadline = time.monotonic() + 3
        while sum(gate.snapshot()[:2]) < 4 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert sum(gate.snapshot()[:2]) == 4
        assert sum(entry.status is JobStatus.RUNNING for entry in entries) == 1
        assert gate.snapshot()[0] == 1
        assert sum(entry.status is JobStatus.PENDING for entry in entries) == 3
        preview_deadline = time.monotonic() + 1
        assert gate.acquire(priority=2, kind="previews", cancel_check=lambda: time.monotonic() > preview_deadline), (
            "GPU work must pass the older loudness jobs waiting for the only CPU"
        )
        assert gate.snapshot()[0] == 2
        gate.release(2, kind="previews")
    finally:
        for entry in entries:
            lifecycle.manager.request_cancellation(entry.id)
            lifecycle.manager.cancel_job(entry.id)
        finish.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=3)
        assert not any(thread.is_alive() for thread in threads)
