"""Journey tests for the start-up JobGate.

The gate bounds how many jobs are starting up (config load, server queries, building the file list) at once, and a
job gives its slot back as soon as its files are submitted to the dispatcher. It exists to stop webhook bursts from
hammering media-server APIs.

Each test drives real jobs through ``_start_job_async`` with ``run_processing`` mocked to block on a
``threading.Event`` so the test controls release timing. A stub that calls ``on_dispatch_start`` stands in for a job
whose files were submitted. External boundaries (Plex API, FFmpeg, publishers) are mocked; the Flask app, JobManager,
JobGate, and ``_start_job_async`` itself run for real.

Matrix:
  1. basic_slots            — 2 slots + 3 jobs still starting up → 2 enter, 1 PENDING with the "Queued" text
  2. drain_on_complete      — finishing 1 starting-up job admits the waiting 3rd
  3. priority_at_gate       — 1 slot, 3 waiters; high-pri jumps normal/low
  4. cancel_while_waiting   — cancelled waiter releases without consuming
  5. pause_skips_gate       — global pause bails before gate entirely
  6. run_processing_raises  — gate released on exception path
  7. startup_requeue_flood  — 12 simultaneous starts with 3 slots serialise
  8. slot_back_after_submit — a job whose files are submitted no longer blocks the next job's start-up
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Each test starts with fresh settings + job + scheduler + gate singletons.

    Teardown also drains any daemon ``run_job`` threads this test spawned so
    a surviving thread from test N doesn't mutate the freshly-built manager
    in test N+1. Threads here are always waiting on either the blocker's
    Event or the gate's Condition — both wake within 1s of release.
    """
    import threading as _threading

    reset_settings_manager()
    import media_preview_generator.web.job_gate as gate_mod
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.routes.job_runner as jr_mod
    import media_preview_generator.web.scheduler as sched_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    gate_mod.reset_job_gate()
    # Kind-aware admission has separate real-runner and contention coverage.
    gate_mod._gate = gate_mod.JobGate(kind_capacity_provider=None)
    threads_before = {t.ident for t in _threading.enumerate()}
    yield

    # Teardown: drain any daemon run_job threads this test spawned so
    # they can't bleed state into the next test (settings singleton,
    # log handlers, etc). Our tests always call blocker.release_all()
    # at the end, but releasing doesn't guarantee the thread has
    # fully unwound the outer finally block yet.
    def _leftover_threads() -> list:
        # Not a Timer: the JobManager's hourly retention timer is also named "Thread-N", never ends on its own, and
        # is stopped by closing the manager below.
        return [
            t
            for t in _threading.enumerate()
            if t.ident not in threads_before
            and t.name.startswith(("run_job", "Thread-"))
            and not isinstance(t, _threading.Timer)
            and t.is_alive()
        ]

    # First, poke the gate to wake any stuck acquirers (belt-and-
    # braces — tests should already have released them, but a missing
    # release_all or an error before it would leave a thread stuck in
    # Condition.wait forever).
    snap = gate_mod.get_job_gate().snapshot() if gate_mod._gate else (0, 0, 0)
    if snap[1] > 0:
        # Forcibly wake all waiters so they observe their cancel_check.
        with gate_mod._gate._cond:
            gate_mod._gate._cond.notify_all()

    deadline = time.time() + 15.0
    while time.time() < deadline and _leftover_threads():
        time.sleep(0.05)

    # Any thread still alive here is leaking. join() each one with
    # a fresh budget — run_job's finally block already ran release()
    # and triggered the inflight-discard, it's likely just the
    # loguru handler tear-down (~100ms per thread). Without this
    # explicit join, a straggler that re-enters get_settings_manager()
    # after our reset_settings_manager() would pollute the next test.
    stragglers = _leftover_threads()
    if stragglers:
        for t in stragglers:
            try:
                t.join(timeout=5.0)
            except Exception:
                pass
        still_alive = _leftover_threads()
        if still_alive:
            import sys as _sys

            print(
                f"WARNING: {len(still_alive)} run_job threads still alive after 20s teardown",
                file=_sys.stderr,
            )

    # The _inflight_jobs set is a process-global — clear it so the next
    # test doesn't short-circuit duplicate-spawn detection on recycled ids.
    with jr_mod._inflight_lock:
        jr_mod._inflight_jobs.clear()

    reset_settings_manager()
    with jobs_mod._job_lock:
        if jobs_mod._job_manager is not None:
            jobs_mod._job_manager.close()
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None
    gate_mod.reset_job_gate()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex Main",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                        "output": {
                            "adapter": "plex_bundle",
                            "plex_config_folder": str(tmp_path / "plex_cfg"),
                        },
                    }
                ],
            }
        )
    )
    (config_dir / "auth.json").write_text(json.dumps({"token": "test-token-12345678"}))
    (tmp_path / "plex_cfg" / "Media" / "localhost").mkdir(parents=True, exist_ok=True)
    # CONFIG_DIR is pointed at this folder only once its settings.json exists: a thread still finishing the
    # previous test can create the settings singleton at any moment, and one created for an empty folder
    # would be kept by create_app (same config dir) with none of these settings.
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    reset_settings_manager()
    return create_app(config_dir=str(config_dir))


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _set_slots(monkeypatch, slots: int) -> None:
    import media_preview_generator.web.job_gate as gate_mod

    monkeypatch.setattr(gate_mod, "STARTUP_SLOTS", slots)


class _BlockingRunProcessing:
    """Build a ``run_processing`` stub that blocks until the test releases it.

    Each job gets its own Event and records its entry + release. The gate
    tests need this because ``run_processing``'s real implementation does
    real work — we only care that the *gate* admission happened.

    ``release_all`` is sticky: releasing the running jobs frees their
    slots, so the gate admits a waiting job right after, and that job
    must not block for the full 30 s wait (the teardown's drain would run
    past the test timeout).

    ``submits`` makes the stub call ``on_dispatch_start`` first, as the orchestrator does right before it submits a
    job's files; that is where the job gives its start-up slot back.
    """

    def __init__(self, submits: bool = False):
        self._submits = submits
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._entered: list[str] = []
        self._released_all = False

    def _event_for(self, job_id: str) -> threading.Event:
        with self._lock:
            if job_id not in self._events:
                self._events[job_id] = threading.Event()
                if self._released_all:
                    self._events[job_id].set()
            return self._events[job_id]

    def entered(self) -> list[str]:
        with self._lock:
            return list(self._entered)

    def release(self, job_id: str) -> None:
        self._event_for(job_id).set()

    def release_all(self) -> None:
        with self._lock:
            self._released_all = True
            for ev in self._events.values():
                ev.set()

    def __call__(self, config, selected_gpus, **kwargs):
        job_id = kwargs.get("job_id") or ""
        with self._lock:
            self._entered.append(job_id)
        if self._submits:
            kwargs["on_dispatch_start"]()
        self._event_for(job_id).wait(timeout=30.0)
        return {"outcome": {"generated": 0}}


@pytest.mark.real_job_async
@pytest.mark.integration
@pytest.mark.slow
class TestStartupGate:
    """Pin the start-up slot contract. Each test narrates the shape it's guarding.

    All tests opt out of the conftest ``_sync_start_job_async`` shim —
    the gate's whole point is admitting one thread while another blocks,
    which requires real daemon threads. Marked ``integration`` so the
    default xdist-parallel ``pytest`` run excludes them (their 15-20s
    per-test thread-drain teardowns caused flakiness when xdist workers
    ran them concurrently with unrelated tests that share the settings
    singleton). Run explicitly with ``pytest -m integration`` or
    ``pytest tests/journeys/test_journey_startup_gate.py -n 0``.
    """

    def test_basic_slots_hold_excess_in_pending(self, app, monkeypatch):
        """2 start-up slots, 3 jobs still starting up → exactly 2 enter run_processing; the 3rd stays PENDING with
        the "Queued" current_item message counting the busy slots.

        Submits the first 2 jobs, waits for them to BOTH reach run_processing (so the slots are definitely taken),
        THEN submits the waiter. This avoids races where the waiter's config-load runs faster/slower than its peers
        — by the time its acquire() is called, the slots are already gone and it enters the "Queued —" state
        deterministically.
        """
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 2)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            active_ids = [jm.create_job(library_name=f"Active {i}", config={}).id for i in range(2)]
            for jid in active_ids:
                _start_job_async(jid, None)
            assert _wait_for(lambda: len(blocker.entered()) == 2, timeout=10.0), (
                f"First 2 jobs must reach run_processing with 2 start-up slots; got {len(blocker.entered())}"
            )

            waiter_id = jm.create_job(library_name="Waiter", config={}).id
            _start_job_async(waiter_id, None)

            expected = "Queued — waiting to start (2 of 2 jobs starting up)"
            assert _wait_for(
                lambda: jm.get_job(waiter_id).progress.current_item == expected,
                timeout=10.0,
            ), f"Waiter must show {expected!r}; got {jm.get_job(waiter_id).progress.current_item!r}"
            assert jm.get_job(waiter_id).status is JobStatus.PENDING
            assert len(blocker.entered()) == 2, (
                f"Waiter must not leak into run_processing; entered={len(blocker.entered())}"
            )

            blocker.release_all()
            _wait_for(
                lambda: all(jm.get_job(j).status.value in ("completed", "cancelled") for j in (*active_ids, waiter_id)),
                timeout=10.0,
            )

    def test_waiting_job_is_admitted_when_active_completes(self, app, monkeypatch):
        """Finishing a running job must wake the queued waiter within 1s
        (the gate's poll interval) and actually call run_processing.

        With 2 start-up slots, 3 jobs = 2 starting up + 1 waiting — the shape this test needs.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 2)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            job_ids = [jm.create_job(library_name=f"Job {i}", config={}).id for i in range(3)]
            for jid in job_ids:
                _start_job_async(jid, None)

            assert _wait_for(lambda: len(blocker.entered()) == 2, timeout=3.0)
            waiting_id = [j for j in job_ids if j not in blocker.entered()][0]
            already_entered = set(blocker.entered())

            # Release one — the queued 3rd must now enter run_processing.
            blocker.release(blocker.entered()[0])
            assert _wait_for(
                lambda: waiting_id in blocker.entered(),
                timeout=3.0,
            ), (
                f"Waiting job {waiting_id[:8]} was never admitted after a peer finished. "
                f"Entered set: {blocker.entered()}, started with: {already_entered}"
            )

            blocker.release_all()
            _wait_for(
                lambda: all(jm.get_job(j).status.value in ("completed", "cancelled") for j in job_ids),
                timeout=5.0,
            )

    def test_priority_breaks_ties_at_gate(self, app, monkeypatch):
        """cap=1, submit pri=3 first (hogs slot), then pri=3, pri=1, pri=2
        as waiters. After hog releases, admission order must be 1 → 2 → 3
        (NOT FIFO). This is the "Sonarr webhook jumps the scheduled
        full-scan" behaviour the plan explicitly called out."""
        from media_preview_generator.web.jobs import PRIORITY_NORMAL, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        PRIORITY_HIGH = 1
        PRIORITY_LOW = 3
        _set_slots(monkeypatch, 1)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            # First job holds the single slot.
            hog = jm.create_job(library_name="Hog", config={}, priority=PRIORITY_LOW)
            _start_job_async(hog.id, None)
            assert _wait_for(lambda: hog.id in blocker.entered(), timeout=3.0)

            # Now queue three waiters in NON-priority order.
            low = jm.create_job(library_name="Low", config={}, priority=PRIORITY_LOW)
            high = jm.create_job(library_name="High", config={}, priority=PRIORITY_HIGH)
            normal = jm.create_job(library_name="Normal", config={}, priority=PRIORITY_NORMAL)
            _start_job_async(low.id, None)
            _start_job_async(high.id, None)
            _start_job_async(normal.id, None)

            # Let the waiters settle into the gate's heap.
            assert _wait_for(
                lambda: all(jm.get_job(j.id).progress.current_item.startswith("Queued —") for j in (low, high, normal)),
                timeout=3.0,
            )

            # Release the hog; the next admit must be HIGH (pri=1).
            before = set(blocker.entered())
            blocker.release(hog.id)
            assert _wait_for(
                lambda: high.id in blocker.entered() and high.id not in before,
                timeout=3.0,
            ), (
                f"Priority inversion: high-pri job was not admitted next. "
                f"Entered after hog release: {[j for j in blocker.entered() if j not in before]!r} — "
                f"expected high={high.id[:8]} first."
            )

            # Release high; next admit must be NORMAL (pri=2).
            before = set(blocker.entered())
            blocker.release(high.id)
            assert _wait_for(
                lambda: normal.id in blocker.entered() and normal.id not in before,
                timeout=3.0,
            ), (
                f"Priority inversion: normal-pri should follow high-pri, not low-pri. "
                f"Newly entered: {[j for j in blocker.entered() if j not in before]!r}"
            )

            # Release normal; low (queued first) finally gets its turn.
            before = set(blocker.entered())
            blocker.release(normal.id)
            assert _wait_for(
                lambda: low.id in blocker.entered() and low.id not in before,
                timeout=3.0,
            )

            blocker.release_all()
            _wait_for(
                lambda: all(
                    jm.get_job(j.id).status.value in ("completed", "cancelled") for j in (hog, low, high, normal)
                ),
                timeout=5.0,
            )

    def test_same_priority_waiters_admit_in_submission_order(self, app, monkeypatch):
        """The gate's heap tiebreak is ``(priority, seq, token)``. The
        priority-inversion test above exercises distinct priorities —
        this pins the FIFO-within-priority cell. Without it, a future
        change to the seq key (e.g. swapping to a time-based tiebreak)
        could regress silently.
        """
        from media_preview_generator.web.jobs import PRIORITY_NORMAL, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 1)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            hog = jm.create_job(library_name="Hog", config={}, priority=PRIORITY_NORMAL)
            _start_job_async(hog.id, None)
            assert _wait_for(lambda: hog.id in blocker.entered(), timeout=5.0)

            # Submit three waiters at the SAME priority in deliberate order.
            first = jm.create_job(library_name="First waiter", config={}, priority=PRIORITY_NORMAL)
            _start_job_async(first.id, None)
            # Give each submission a tiny gap so their gate `seq` values
            # are reliably ordered (the gate's seq counter increments
            # atomically but we need each thread to hit acquire before
            # the next submit, otherwise thread-start jitter can invert
            # the order).
            assert _wait_for(
                lambda: (jm.get_job(first.id).progress.current_item or "").startswith("Queued —"),
                timeout=5.0,
            )
            second = jm.create_job(library_name="Second waiter", config={}, priority=PRIORITY_NORMAL)
            _start_job_async(second.id, None)
            assert _wait_for(
                lambda: (jm.get_job(second.id).progress.current_item or "").startswith("Queued —"),
                timeout=5.0,
            )
            third = jm.create_job(library_name="Third waiter", config={}, priority=PRIORITY_NORMAL)
            _start_job_async(third.id, None)
            assert _wait_for(
                lambda: (jm.get_job(third.id).progress.current_item or "").startswith("Queued —"),
                timeout=5.0,
            )

            # Release the hog → first waiter should admit.
            before = set(blocker.entered())
            blocker.release(hog.id)
            assert _wait_for(
                lambda: first.id in blocker.entered() and first.id not in before,
                timeout=3.0,
            ), "First-submitted same-priority waiter must admit first (FIFO-within-priority)"

            before = set(blocker.entered())
            blocker.release(first.id)
            assert _wait_for(
                lambda: second.id in blocker.entered() and second.id not in before,
                timeout=3.0,
            )

            before = set(blocker.entered())
            blocker.release(second.id)
            assert _wait_for(
                lambda: third.id in blocker.entered() and third.id not in before,
                timeout=3.0,
            )

            blocker.release_all()
            _wait_for(
                lambda: all(
                    jm.get_job(j.id).status.value in ("completed", "cancelled") for j in (hog, first, second, third)
                ),
                timeout=5.0,
            )

    def test_cancel_while_waiting_releases_cleanly(self, app, monkeypatch):
        """Cancelling a job that's queued at the gate must:
        1. Transition it to CANCELLED within the poll tick.
        2. NOT consume an _active slot (the hog still holds the only one).
        3. Let subsequent waiters advance when the hog finishes."""
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 1)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            hog = jm.create_job(library_name="Hog", config={})
            _start_job_async(hog.id, None)
            assert _wait_for(lambda: hog.id in blocker.entered(), timeout=3.0)

            waiter = jm.create_job(library_name="Waiter", config={})
            _start_job_async(waiter.id, None)
            assert _wait_for(
                lambda: jm.get_job(waiter.id).progress.current_item.startswith("Queued —"),
                timeout=3.0,
            )

            # Before cancel: gate shows 1 active, 1 waiting.
            active_before, waiting_before, _ = get_job_gate().snapshot()
            assert active_before == 1 and waiting_before == 1, (
                f"Gate snapshot before cancel should be (active=1, waiting=1); got ({active_before}, {waiting_before})"
            )

            jm.request_cancellation(waiter.id)
            jm.cancel_job(waiter.id)

            # Within the 1s poll tick, the waiter exits acquire() without
            # consuming a slot. The cancel_check inside acquire sees True,
            # heap is re-heapified, waiting_count drops to 0, active stays 1.
            assert _wait_for(
                lambda: get_job_gate().snapshot()[1] == 0,
                timeout=3.0,
            ), "Cancelled waiter must be removed from the gate's heap within one poll tick"
            active_after, waiting_after, _ = get_job_gate().snapshot()
            assert active_after == 1 and waiting_after == 0, (
                f"Gate snapshot after cancel should be (active=1, waiting=0); "
                f"got ({active_after}, {waiting_after}). The hog's slot must be intact."
            )
            assert jm.get_job(waiter.id).status is JobStatus.CANCELLED

            blocker.release_all()

    def test_pause_skips_gate_entirely(self, app, monkeypatch):
        """When global processing_paused=True, jobs bail BEFORE the gate
        (line 143 of job_runner.py). Gate's _active must stay at 0 even
        though a job was 'started'."""
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async
        from media_preview_generator.web.settings_manager import get_settings_manager

        _set_slots(monkeypatch, 3)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            get_settings_manager().processing_paused = True
            jm = get_job_manager()
            job = jm.create_job(library_name="Paused Job", config={})
            _start_job_async(job.id, None)

            # Give the thread time to run past line 143 and exit.
            time.sleep(0.5)
            assert blocker.entered() == [], (
                f"run_processing must NOT be called while globally paused; entered={blocker.entered()}"
            )
            active, waiting, _ = get_job_gate().snapshot()
            assert active == 0 and waiting == 0, (
                f"Paused-out job must not touch the gate; snapshot=({active}, {waiting})"
            )

    def test_run_processing_raises_releases_slot(self, app, monkeypatch):
        """An exception in run_processing must still release the slot.
        The outer finally at job_runner.py:~972 handles this via the
        ``if _slot_held`` guard; without it, one crashed job would
        wedge the cap forever."""
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 1)

        def boom(config, selected_gpus, **kwargs):
            raise RuntimeError("simulated run_processing failure")

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=boom,
            ),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="Boom", config={})
            _start_job_async(job.id, None)
            # Wait for the job to fail + thread to unwind through the
            # outer finally that releases the gate.
            assert _wait_for(
                lambda: jm.get_job(job.id).status.value in ("failed", "completed"),
                timeout=3.0,
            ), f"Crashing job never reached a terminal state: {jm.get_job(job.id).status!r}"
            # Give the finally block a beat to actually call release().
            assert _wait_for(
                lambda: get_job_gate().snapshot()[0] == 0,
                timeout=2.0,
            ), f"Gate _active must drop to 0 after a crashed job unwinds. snapshot={get_job_gate().snapshot()}"

    def test_startup_requeue_flood_is_paced_by_gate(self, app, monkeypatch):
        """Simulate the _requeue_interrupted_on_startup path: 12 jobs started in rapid succession with 3 start-up
        slots. Exactly 3 should reach run_processing; the other 9 must sit queued. This is the exact regression that
        prompted the gate — without it, 30+ simultaneous enumerations would hammer Jellyfin's plugin endpoint."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 3)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            ids = [jm.create_job(library_name=f"flood-{i}", config={}).id for i in range(12)]
            for jid in ids:
                _start_job_async(jid, None)

            assert _wait_for(lambda: len(blocker.entered()) == 3, timeout=3.0), (
                f"With 3 start-up slots, exactly 3 of the 12 flood jobs must enter run_processing; "
                f"got {len(blocker.entered())}"
            )
            time.sleep(1.0)
            assert len(blocker.entered()) == 3, (
                f"Flood must stay paced at 3 — no admissions without releases. entered={len(blocker.entered())}"
            )
            queued = [j for j in ids if j not in blocker.entered()]
            queued_messages = [jm.get_job(j).progress.current_item for j in queued]
            assert all(m.startswith("Queued —") for m in queued_messages), (
                f"All 9 waiting flood jobs must show a 'Queued —' message; got {queued_messages!r}"
            )

            blocker.release_all()
            _wait_for(
                lambda: all(jm.get_job(j).status.value in ("completed", "cancelled") for j in ids),
                timeout=8.0,
            )

    def test_start_up_slot_is_released_once_files_are_submitted(self, app, monkeypatch):
        """With ONE start-up slot, job 1 submits its files and keeps running; job 2 must still get to start up.

        Before the gate only bounded start-up, job 1 held the slot until it finished, so job 2 waited behind it.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 1)
        blocker = _BlockingRunProcessing(submits=True)

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            first = jm.create_job(library_name="Big scan", config={})
            _start_job_async(first.id, None)
            assert _wait_for(lambda: first.id in blocker.entered(), timeout=5.0)

            second = jm.create_job(library_name="Webhook", config={})
            _start_job_async(second.id, None)
            assert _wait_for(lambda: second.id in blocker.entered(), timeout=5.0), (
                "Job 2 must start up while job 1 is still running its submitted files"
            )
            assert jm.get_job(first.id).status.value == "running"

            blocker.release_all()
            _wait_for(
                lambda: all(jm.get_job(j.id).status.value in ("completed", "cancelled") for j in (first, second)),
                timeout=5.0,
            )

    def test_start_up_slot_is_kept_until_files_are_submitted(self, app, monkeypatch):
        """The counterpart: a job still starting up (nothing submitted) keeps its slot, so the next job waits."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 1)
        blocker = _BlockingRunProcessing(submits=False)

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            first = jm.create_job(library_name="Still listing", config={})
            _start_job_async(first.id, None)
            assert _wait_for(lambda: first.id in blocker.entered(), timeout=5.0)

            second = jm.create_job(library_name="Waiting", config={})
            _start_job_async(second.id, None)
            assert _wait_for(
                lambda: (jm.get_job(second.id).progress.current_item or "").startswith("Queued —"), timeout=5.0
            )
            assert second.id not in blocker.entered()

            blocker.release_all()
            _wait_for(
                lambda: all(jm.get_job(j.id).status.value in ("completed", "cancelled") for j in (first, second)),
                timeout=5.0,
            )

    def test_start_up_slot_is_released_even_when_start_job_raises_at_dispatch(self, app, monkeypatch):
        """``_on_dispatch_start`` releases in a ``finally``: a failing ``start_job`` must not leak the slot."""
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import JobManager, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_slots(monkeypatch, 1)
        real_start_job = JobManager.start_job
        calls = []

        def start_job(self, job_id, *args, **kwargs):
            calls.append(job_id)
            if len(calls) == 2:  # the first call is the runner's own, on admission
                raise RuntimeError("simulated start_job failure")
            return real_start_job(self, job_id, *args, **kwargs)

        monkeypatch.setattr(JobManager, "start_job", start_job)
        slot_while_running = []
        release = threading.Event()

        def run_processing(config, selected_gpus, **kwargs):
            try:
                kwargs["on_dispatch_start"]()
            except RuntimeError:
                pass
            slot_while_running.append(get_job_gate().snapshot()[0])
            release.wait(timeout=10)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=run_processing),
        ):
            job = get_job_manager().create_job(library_name="Raises", config={})
            _start_job_async(job.id, None)
            try:
                assert _wait_for(lambda: bool(slot_while_running), timeout=5.0)
                assert len(calls) >= 2
                assert slot_while_running == [0], "the slot leaked because start_job raised before the release"
            finally:
                release.set()
