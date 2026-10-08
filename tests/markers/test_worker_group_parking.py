"""Real shared dispatch/JobManager parking keeps forced marker work and promises."""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from media_preview_generator.job_kinds import ItemOutcome, KindHandlers
from media_preview_generator.jobs import group_runtime
from media_preview_generator.jobs.dispatcher import JobDispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.markers import job_runner
from media_preview_generator.markers.models import MarkerType
from media_preview_generator.markers.outcomes import VERIFY_LATER, FileOutcome
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType
from media_preview_generator.web import jobs
from media_preview_generator.web.job_gate import JobGate
from media_preview_generator.web.jobs import JobManager, JobStatus
from tests.markers.fakes import FakeRegistry, server_config
from tests.markers.test_pipeline import _ctx


def test_forced_marker_job_parks_then_keeps_counts_and_followup_obligations(tmp_path, monkeypatch):
    groups = [
        {
            "id": "cpu",
            "name": "Markers",
            "enabled": True,
            "resource": "cpu",
            "device": None,
            "count": 1,
            "job_types": ["intro_credits"],
            "availability": {"mode": "always"},
        }
    ]
    monkeypatch.setattr(group_runtime, "current_groups", lambda config=None: groups)
    monkeypatch.setattr(group_runtime, "current_group_policy", lambda config=None: (groups, None))
    monkeypatch.setattr(
        group_runtime, "runtime_capacity", lambda kind: group_runtime.capacity_for_groups(groups, [], kind)
    )
    monkeypatch.setattr(job_runner, "runtime_capacity", group_runtime.runtime_capacity)
    manager = JobManager(str(tmp_path))
    gate = JobGate(lambda: 1)
    dispatcher = JobDispatcher(WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[]))
    dispatcher.worker_pool.reconcile_groups(groups, [])
    registry = FakeRegistry({"jf": server_config("jf", ServerType.JELLYFIN, root=str(tmp_path))})
    store = MarkerStore(str(tmp_path / "markers.db"))
    config = SimpleNamespace(
        cpu_threads=1, scan_workers=1, gpu_threads=0, ffmpeg_path="ffmpeg", regenerate_thumbnails=False
    )
    settings = SimpleNamespace(
        processing_paused=False,
        locked=nullcontext,
        get=lambda key, default=None: "INFO" if key == "log_level" else default,
    )
    paths = [str(tmp_path / "first.mkv"), str(tmp_path / "second.mkv")]
    extra = str(tmp_path / "sibling.mkv")
    contexts, processed = [], []

    def context(**kwargs):
        ctx = _ctx(store, registry, force=kwargs["force"])
        contexts.append(ctx)
        return ctx

    def handlers(ctx):
        def process(item, **kwargs):
            assert ctx.force
            processed.append(item.canonical_path)
            ctx.decided_by.add({MarkerType.INTRO: "chapters"})
            rows = [{"server_id": "jf", "server_type": "jellyfin", "status": "markers_written"}]
            if len(processed) == 1:
                ctx.request_followups([paths[0], extra])
                ctx._budget_rechecks.add(extra)
                ctx._budget_refused_at = datetime(2026, 10, 5, tzinfo=UTC)
                rows[0][VERIFY_LATER] = True
                groups[0]["enabled"] = False
                dispatcher.worker_pool.reconcile_groups(groups, [])
                group_runtime.wake_group_runtime()
            return ItemOutcome(FileOutcome.PUBLISHED.value, publisher_rows=rows)

        return KindHandlers(lambda *args, **kwargs: None, process, (FileOutcome.PUBLISHED.value, "failed"))

    for name, value in {
        "get_job_manager": lambda: manager,
        "get_job_gate": lambda: gate,
        "get_settings_manager": lambda: settings,
        "load_config": lambda: config,
        "_build_multi_server_registry": lambda cfg: registry,
        "build_context": context,
        "kind_handlers": handlers,
        "run_detector_checks": lambda value: None,
        "_ensure_gpu_cache": lambda: [],
        "_build_selected_gpus": lambda *args, **kw: [],
        "get_or_create_dispatcher": lambda *args: dispatcher,
        "build_items": lambda *args, **kw: ([ProcessableItem(path, "jf") for path in paths], [], {}),
        "_start_fingerprint_sweep": lambda *args: None,
    }.items():
        monkeypatch.setattr(job_runner, name, value)
    monkeypatch.setattr(jobs, "get_job_manager", lambda: manager)
    verify = MagicMock()
    monkeypatch.setattr(job_runner, "_queue_verify", verify)
    parent = manager.create_job(kind="intro_credits", config={"force": True, "file_paths": paths, "source": "sonarr"})
    runner = threading.Thread(target=job_runner.run_intro_credits_job, args=(parent.id,))
    runner.start()
    try:
        deadline = time.monotonic() + 8
        while not parent.config.get("parked_checkpoint") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert parent.status is JobStatus.PENDING and parent.config.get("parked_checkpoint"), parent.error
        assert gate.snapshot()[0] == 0 and parent.id not in dispatcher._trackers
        assert parent.progress.processed_items == 1
        groups[0]["enabled"] = True
        group_runtime.wake_group_runtime()
        runner.join(8)
        assert not runner.is_alive()
        assert parent.status is JobStatus.COMPLETED, parent.error
        assert processed == paths and len(contexts) == 2
        assert parent.progress.outcome[FileOutcome.PUBLISHED.value] == 2
        assert parent.progress.marker_sources == {"intro": {"chapters": 2}}
        assert [job.id for job in manager.get_all_jobs()] == [parent.id]
        verify.assert_called_once()
        assert verify.call_args.args[0] is parent
        assert verify.call_args.args[2:] == ({paths[0]}, {})
    finally:
        manager.request_cancellation(parent.id)
        group_runtime.wake_group_runtime()
        runner.join(3)
        dispatcher.shutdown()
        manager.close()
        store.close()
