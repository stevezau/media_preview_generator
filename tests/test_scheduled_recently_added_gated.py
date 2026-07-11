"""Tests for ``_start_recently_added_job_async`` — the gated helper that
replaced the inline ``_run_recently_added_multi_server`` call previously
fired straight from the APScheduler worker thread.

Pre-fix the inline path was the ONE remaining work source that bypassed
the JobGate and skipped Job-row creation entirely (no UI visibility,
no cancellation). The helper closes both gaps: gate-acquire before the
scan runs, real Job row in the JobManager, ``config.source =
"scheduled_recently_added"`` for the source badge.

These tests pin the contract:
  1. The helper creates a Job with the correct shape.
  2. The daemon thread acquires the JobGate before invoking the scan.
  3. The scan is invoked with the kwargs the operator configured.
  4. The gate slot is released on completion.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Drop process-wide singletons between tests so timers / threads
    from one test don't bleed into another."""
    import media_preview_generator.web.job_gate as gate_mod
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.routes.job_runner as jr_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    gate_mod.reset_job_gate()
    with jr_mod._inflight_lock:
        jr_mod._inflight_jobs.clear()
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    gate_mod.reset_job_gate()
    with jr_mod._inflight_lock:
        jr_mod._inflight_jobs.clear()


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestStartRecentlyAddedJobAsync:
    def test_creates_job_with_scheduled_recently_added_source(self, tmp_path):
        """The Job row must carry ``config.source =
        "scheduled_recently_added"`` so the source-badge palette in app.js
        renders the "Scheduled scan" pill — without this the row appears
        unlabelled in the Job Queue table and the operator can't tell
        what triggered it."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))
        jm = jobs_mod._job_manager

        scan_called = threading.Event()
        scan_kwargs: dict = {}

        def fake_scan(*args, **kwargs):
            scan_kwargs.update(kwargs)
            scan_called.set()
            return {}

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=fake_scan,
            ),
            patch("media_preview_generator.config.load_config", return_value=MagicMock()),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
        ):
            job_id = _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id="plex-1",
                library_ids=["2"],
                lookback_hours=2.0,
                library_name="Recently added: TV",
            )
            assert scan_called.wait(timeout=3.0), "scan must run after gate admission"

        job = jm.get_job(job_id)
        assert job is not None
        assert job.config.get("source") == "scheduled_recently_added", (
            f"Job.config.source must be 'scheduled_recently_added' so the source-badge "
            f"palette in app.js renders the pill; got {job.config.get('source')!r}"
        )
        assert job.config.get("parent_schedule_id") == "sched-1"
        assert job.library_name == "Recently added: TV"
        # Scan was forwarded the right kwargs.
        assert scan_kwargs.get("server_id_filter") == "plex-1"
        assert scan_kwargs.get("library_ids") == ["2"]
        assert scan_kwargs.get("lookback_hours") == 2.0
        assert scan_kwargs.get("job_id") == job_id, (
            "job_id must thread through to the scan so progress callbacks land on this Job's row"
        )

    def test_acquires_gate_before_running_scan(self, tmp_path):
        """The gate must be acquired BEFORE the scan runs — otherwise
        the cap doesn't actually bound concurrent work."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        call_log: list[str] = []
        scan_done = threading.Event()

        gate_mock = MagicMock()
        gate_mock.acquire = MagicMock(side_effect=lambda **kw: call_log.append("acquire") or True)
        gate_mock.release = lambda: call_log.append("release")

        def fake_scan(*args, **kwargs):
            call_log.append("scan")
            scan_done.set()
            return {}

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=fake_scan,
            ),
            patch("media_preview_generator.config.load_config", return_value=MagicMock()),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
            patch(
                "media_preview_generator.web.job_gate.get_job_gate",
                return_value=gate_mock,
            ),
        ):
            _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id=None,
                library_ids=None,
                lookback_hours=1.0,
                library_name="Recently added: all libraries",
            )
            assert scan_done.wait(timeout=3.0)
            # Give the finally clause a tick to run release().
            assert _wait_for(lambda: "release" in call_log, timeout=2.0)

        assert "acquire" in call_log and "scan" in call_log and "release" in call_log, (
            f"acquire / scan / release must all run; got {call_log!r}"
        )
        assert call_log.index("acquire") < call_log.index("scan"), (
            f"gate.acquire MUST happen before the scan runs — got {call_log!r}"
        )
        assert call_log.index("scan") < call_log.index("release"), (
            f"gate.release MUST happen after the scan returns — got {call_log!r}"
        )

    @pytest.mark.parametrize(
        "passed_priority,expected",
        [
            (1, 1),  # High pin must reach the Job (issue #259)
            (3, 3),  # Low pin must reach the Job
            (None, 2),  # unset -> PRIORITY_NORMAL (parse_priority default)
        ],
    )
    def test_priority_forwarded_to_created_job(self, tmp_path, passed_priority, expected):
        """The schedule's priority must reach the spawned Job (issue #259).

        Pre-fix the recently-added branch dropped ``priority`` entirely:
        the helper had no parameter for it and ``create_job`` fell back to
        PRIORITY_NORMAL, so a High/Low schedule always ran at Normal. The
        gate admits at ``job.priority``, so this also governs queue order.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))
        jm = jobs_mod._job_manager

        scan_called = threading.Event()

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=lambda *a, **k: scan_called.set() or {},
            ),
            patch("media_preview_generator.config.load_config", return_value=MagicMock()),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
        ):
            job_id = _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id=None,
                library_ids=None,
                lookback_hours=1.0,
                library_name="Recently added: all libraries",
                priority=passed_priority,
            )
            assert scan_called.wait(timeout=3.0)

        job = jm.get_job(job_id)
        assert job is not None
        assert job.priority == expected, (
            f"schedule priority {passed_priority!r} must reach the Job as {expected}; got {job.priority}"
        )

    @pytest.mark.parametrize(
        "server_id,expected_filter",
        [
            ("plex-1", "plex-1"),  # pinned -> publish only to that server (issue #259)
            (None, None),  # unpinned -> leave fan-out behaviour untouched
        ],
    )
    def test_pinned_server_sets_config_server_id_filter(self, tmp_path, server_id, expected_filter):
        """A pinned recently-added scan must set ``config.server_id_filter``.

        Enumeration is already filtered by the explicit ``server_id_filter``
        kwarg, but the per-item PUBLISH target is resolved from
        ``config.server_id_filter`` (``resolve_per_item_pin``). With two Plex
        servers owning the same files, a Plex-pinned scan that left this
        unset fanned out and published to both (issue #259 comment). When
        unpinned we must NOT set it, so unpinned scans keep fanning out.
        """
        from types import SimpleNamespace

        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        scan_called = threading.Event()
        captured: dict = {}

        def fake_scan(config, *args, **kwargs):
            captured["config"] = config
            scan_called.set()
            return {}

        fake_config = SimpleNamespace(server_id_filter=None)

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=fake_scan,
            ),
            patch("media_preview_generator.config.load_config", return_value=fake_config),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
        ):
            _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id=server_id,
                library_ids=None,
                lookback_hours=1.0,
                library_name="Recently added: all libraries",
            )
            assert scan_called.wait(timeout=3.0)

        assert captured["config"].server_id_filter == expected_filter, (
            f"server_id={server_id!r} must yield config.server_id_filter={expected_filter!r}; "
            f"got {captured['config'].server_id_filter!r}"
        )

    def test_cancel_during_gate_wait_skips_scan_and_does_not_release(self, tmp_path):
        """If the user cancels the Job while it's waiting for a gate
        slot, ``acquire`` returns False and the scan must NOT run. The
        Job transitions to CANCELLED; no slot was ever held so release
        must not fire (would leak a slot from the cap)."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))
        jm = jobs_mod._job_manager

        scan_called = threading.Event()
        gate_mock = MagicMock()
        gate_mock.acquire = MagicMock(return_value=False)
        gate_mock.release = MagicMock()

        def fake_scan(*args, **kwargs):
            scan_called.set()
            return {}

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=fake_scan,
            ),
            patch("media_preview_generator.config.load_config", return_value=MagicMock()),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
            patch(
                "media_preview_generator.web.job_gate.get_job_gate",
                return_value=gate_mock,
            ),
        ):
            job_id = _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id=None,
                library_ids=None,
                lookback_hours=1.0,
                library_name="Recently added: all libraries",
            )
            # Let the daemon settle.
            assert _wait_for(
                lambda: jm.get_job(job_id).status.value in ("cancelled", "completed", "failed"),
                timeout=3.0,
            ), f"Job did not settle; status={jm.get_job(job_id).status.value!r}"

        assert not scan_called.is_set(), "scan must NOT run when gate.acquire returns False (cancel-during-wait)"
        assert gate_mock.release.call_count == 0, (
            f"gate.release must NOT fire when no slot was acquired — got {gate_mock.release.call_count} calls"
        )
        # Job should land in CANCELLED, not COMPLETED.
        assert jm.get_job(job_id).status.value == "cancelled"


class TestWorkerCallbackImportsResolve:
    """Regression for #267. The ``worker_callback`` closure in
    ``_start_recently_added_job_async`` did ``from ...jobs import WorkerStatus``
    (one dot too many), resolving to the top-level ``jobs`` *package* — where
    ``WorkerStatus`` is NOT defined — instead of ``..jobs`` -> ``web.jobs``. At
    runtime the dispatcher swallowed the ImportError as "dispatch loop iteration
    failed; continuing", so worker-status UI updates silently vanished for
    scheduled recently-added scans while preview generation still completed.

    The fix hoists ``WorkerStatus`` to the module-level ``from ..jobs import``,
    so any wrong target now fails at module import time (which this test
    forces) rather than being deferred into a closure the mocked scans never
    fire. The two checks below pin (a) the symbol is the real class from
    ``web.jobs`` and (b) no stray function-local import re-introduces the
    wrong-depth footgun.
    """

    def test_workerstatus_is_live_at_module_level(self):
        import media_preview_generator.web.routes.job_runner as jr_mod
        from media_preview_generator.web.jobs import WorkerStatus

        assert jr_mod.WorkerStatus is WorkerStatus, (
            "job_runner.WorkerStatus must be the class from web.jobs — a wrong "
            "relative-import depth would either shadow it or fail import (issue #267)"
        )

    def test_no_workerstatus_import_resolves_wrong(self):
        import ast
        import importlib
        from pathlib import Path

        import media_preview_generator.web.routes.job_runner as jr_mod

        tree = ast.parse(Path(jr_mod.__file__).read_text())
        base_pkg = "media_preview_generator.web.routes"

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not any(alias.name == "WorkerStatus" for alias in node.names):
                continue
            # Resolve the relative module the same way Python would.
            parent = base_pkg.rsplit(".", node.level - 1)[0] if node.level > 1 else base_pkg
            target = f"{parent}.{node.module}" if node.module else parent
            mod = importlib.import_module(target)
            assert hasattr(mod, "WorkerStatus"), (
                f"`from {'.' * node.level}{node.module or ''} import WorkerStatus` at "
                f"job_runner.py:{node.lineno} resolves to {target!r}, which has no "
                f"WorkerStatus (issue #267 — wrong relative-import depth)"
            )
