"""Journey tests for the concurrency-cap JobGate.

Pin the complete contract for the ``max_concurrent_jobs`` semaphore
gate introduced to stop webhook bursts from hammering media-server
APIs. See /home/data/.claude/plans/piped-humming-flame.md for the
full design rationale.

Each test drives real jobs through ``_start_job_async`` with
``run_processing`` mocked to block on a ``threading.Event`` so the
test controls release timing. External boundaries (Plex API, FFmpeg,
publishers) are mocked; the Flask app, JobManager, JobGate, and
``_start_job_async`` itself run for real.

Matrix (from the approved plan, extended for the issue #285 reservation):
  1. basic_cap              — cap=3 + 3 normal jobs → 2 RUNNING, 1 PENDING
  2. drain_on_complete      — finishing 1 active admits the waiting 3rd
  3. priority_at_gate       — cap=1, 3 waiters; high-pri jumps normal/low
  4. cancel_while_waiting   — cancelled waiter releases without consuming
  5. pause_skips_gate       — global pause bails before gate entirely
  6. runtime_cap_change     — cap lowered at runtime stops new admissions
  7. run_processing_raises  — gate released on exception path
  8. startup_requeue_flood  — 12 simultaneous starts with cap=3 serialise
  9. high_takes_reserved    — high-pri admits into the slot normals can't
 10. reserved_slot_reused   — the reservation survives a high job finishing

Reservation contract (issue #285): while the cap is above 1, normal/low
jobs are admitted only up to ``cap - 1``; the remaining slot is held for
priority-1 work so a webhook never waits out a multi-hour full scan. The
tests below encode the resulting counts explicitly — a "cap=3 → 3 active"
expectation anywhere in this file would mean the reservation regressed.
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
    threads_before = {t.ident for t in _threading.enumerate()}
    yield

    # Teardown: drain any daemon run_job threads this test spawned so
    # they can't bleed state into the next test (settings singleton,
    # log handlers, etc). Our tests always call blocker.release_all()
    # at the end, but releasing doesn't guarantee the thread has
    # fully unwound the outer finally block yet.
    def _leftover_threads() -> list:
        return [
            t
            for t in _threading.enumerate()
            if t.ident not in threads_before and t.name.startswith(("run_job", "Thread-")) and t.is_alive()
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
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                # Default cap; individual tests override via the live manager.
                "max_concurrent_jobs": 3,
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
    return create_app(config_dir=str(config_dir))


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _set_cap(cap: int) -> None:
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().update({"max_concurrent_jobs": cap})


class _BlockingRunProcessing:
    """Build a ``run_processing`` stub that blocks until the test releases it.

    Each job gets its own Event and records its entry + release. The gate
    tests need this because ``run_processing``'s real implementation does
    real work — we only care that the *gate* admission happened.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._entered: list[str] = []

    def _event_for(self, job_id: str) -> threading.Event:
        with self._lock:
            if job_id not in self._events:
                self._events[job_id] = threading.Event()
            return self._events[job_id]

    def entered(self) -> list[str]:
        with self._lock:
            return list(self._entered)

    def release(self, job_id: str) -> None:
        self._event_for(job_id).set()

    def release_all(self) -> None:
        with self._lock:
            for ev in self._events.values():
                ev.set()

    def __call__(self, config, selected_gpus, **kwargs):
        job_id = kwargs.get("job_id") or ""
        with self._lock:
            self._entered.append(job_id)
        self._event_for(job_id).wait(timeout=30.0)
        return {"outcome": {"generated": 0}}


@pytest.mark.real_job_async
@pytest.mark.integration
@pytest.mark.slow
class TestMaxConcurrentGate:
    """Pin the complete cap contract. Each test narrates the shape it's guarding.

    All tests opt out of the conftest ``_sync_start_job_async`` shim —
    the gate's whole point is admitting one thread while another blocks,
    which requires real daemon threads. Marked ``integration`` so the
    default xdist-parallel ``pytest`` run excludes them (their 15-20s
    per-test thread-drain teardowns caused flakiness when xdist workers
    ran them concurrently with unrelated tests that share the settings
    singleton). Run explicitly with ``pytest -m integration`` or
    ``pytest tests/journeys/test_journey_max_concurrent_gate.py -n 0``.
    """

    def test_basic_cap_holds_excess_in_pending(self, app):
        """cap=3, 3 normal jobs submitted → exactly 2 enter run_processing;
        the 3rd stays PENDING with a "Queued" current_item message that
        names the reserved high-priority slot.

        Only 2 admit because of the issue #285 reservation: at cap=3 the
        normal/low budget is ``cap - 1``. Submits the first 2 jobs, waits
        for them to BOTH reach run_processing (so the normal budget is
        definitely spent), THEN submits the waiter. This avoids races
        where the waiter's config-load runs faster/slower than its peers
        — by the time its acquire() is called, the normal budget is
        already gone and it enters the "Queued —" state deterministically.
        """
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(3)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            # Start the first 2 — they should both enter run_processing
            # once config-load + gate-admission complete.
            active_ids = [jm.create_job(library_name=f"Active {i}", config={}).id for i in range(2)]
            for jid in active_ids:
                _start_job_async(jid, None)
            assert _wait_for(lambda: len(blocker.entered()) == 2, timeout=10.0), (
                f"First 2 normal jobs must reach run_processing under cap=3 (budget = cap - 1); "
                f"got {len(blocker.entered())}"
            )

            # Normal budget is now spent. Submit the waiter.
            waiter_id = jm.create_job(library_name="Waiter", config={}).id
            _start_job_async(waiter_id, None)

            # The waiter's thread has to run past config-load, tmp-folder,
            # etc. before reaching the gate. Give it a generous window
            # for cold-cache first test runs.
            import re

            queued_re = re.compile(
                r"Queued — waiting for active slot \((\d+) of (\d+) busy, (\d+) reserved for high priority\)"
            )
            assert _wait_for(
                lambda: queued_re.match(jm.get_job(waiter_id).progress.current_item or "") is not None,
                timeout=10.0,
            ), (
                f"Waiter must show the fully-formatted 'Queued — waiting for active slot "
                f"(X of Y busy, Z reserved for high priority)' message — the counters AND the "
                f"reservation clause are the SUT's contract, not just the prefix. "
                f"got {jm.get_job(waiter_id).progress.current_item!r}"
            )
            # The counters must match the gate's view: 2 active / 3 cap,
            # 1 of which is reserved (which is WHY this job is waiting —
            # without the clause the user sees "2 of 3 busy" and files a
            # bug about the gate refusing to use a free slot).
            match = queued_re.match(jm.get_job(waiter_id).progress.current_item)
            assert (match.group(1), match.group(2), match.group(3)) == ("2", "3", "1"), (
                f"Counter must report (2 of 3 busy, 1 reserved for high priority); got {match.group(0)!r}"
            )
            # Waiter stays PENDING because on_dispatch_start hasn't fired.
            assert jm.get_job(waiter_id).status is JobStatus.PENDING
            # No extra admission — still exactly 2 in run_processing.
            assert len(blocker.entered()) == 2, (
                f"Waiter must not leak into run_processing; entered={len(blocker.entered())}"
            )

            blocker.release_all()
            _wait_for(
                lambda: all(jm.get_job(j).status.value in ("completed", "cancelled") for j in (*active_ids, waiter_id)),
                timeout=10.0,
            )

    def test_waiting_job_is_admitted_when_active_completes(self, app):
        """Finishing a running job must wake the queued waiter within 1s
        (the gate's poll interval) and actually call run_processing.

        cap=3 gives normal-priority jobs a budget of 2 (the third slot is
        reserved for high priority), so 3 normal jobs = 2 active + 1
        waiting — the shape this test needs.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(3)
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

    def test_priority_breaks_ties_at_gate(self, app):
        """cap=1, submit pri=3 first (hogs slot), then pri=3, pri=1, pri=2
        as waiters. After hog releases, admission order must be 1 → 2 → 3
        (NOT FIFO). This is the "Sonarr webhook jumps the scheduled
        full-scan" behaviour the plan explicitly called out."""
        from media_preview_generator.web.jobs import PRIORITY_NORMAL, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        PRIORITY_HIGH = 1
        PRIORITY_LOW = 3
        _set_cap(1)
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

    def test_same_priority_waiters_admit_in_submission_order(self, app):
        """The gate's heap tiebreak is ``(priority, seq, token)``. The
        priority-inversion test above exercises distinct priorities —
        this pins the FIFO-within-priority cell. Without it, a future
        change to the seq key (e.g. swapping to a time-based tiebreak)
        could regress silently.
        """
        from media_preview_generator.web.jobs import PRIORITY_NORMAL, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(1)
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

    def test_cancel_while_waiting_releases_cleanly(self, app):
        """Cancelling a job that's queued at the gate must:
        1. Transition it to CANCELLED within the poll tick.
        2. NOT consume an _active slot (the hog still holds the only one).
        3. Let subsequent waiters advance when the hog finishes."""
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(1)
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

    def test_pause_skips_gate_entirely(self, app):
        """When global processing_paused=True, jobs bail BEFORE the gate
        (line 143 of job_runner.py). Gate's _active must stay at 0 even
        though a job was 'started'."""
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async
        from media_preview_generator.web.settings_manager import get_settings_manager

        _set_cap(3)
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

    def test_runtime_cap_change_takes_effect_without_restart(self, app):
        """Dropping cap from 4 → 1 at runtime must stop new admissions.
        Raising back to 4 must wake queued waiters. Validates the
        cap_provider closure in JobGate._cap.

        cap=4 rather than 3 so the normal-priority budget (``cap - 1``)
        is 3 and the original 3-active / 2-waiting shape still holds
        under the issue #285 reservation.
        """
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(4)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            ids = [jm.create_job(library_name=f"J{i}", config={}).id for i in range(5)]
            for jid in ids:
                _start_job_async(jid, None)

            # With cap=4, the normal budget is 3: 3 enter; 2 wait.
            assert _wait_for(lambda: len(blocker.entered()) == 3, timeout=3.0)

            # Drop cap to 1 — running jobs keep running, but new admissions
            # stop. The gate reads cap on every wake via cap_provider.
            _set_cap(1)
            first_three = list(blocker.entered())

            # Release one of the active jobs. Under cap=1 with 3 already
            # active, the release brings _active to 2 — still above cap.
            # No new admission should happen.
            blocker.release(first_three[0])
            time.sleep(1.5)  # Two gate poll ticks.
            assert len(blocker.entered()) == 3, (
                f"After lowering cap to 1 with 2 still active, no new admits should happen. "
                f"entered={len(blocker.entered())}, expected=3"
            )

            # Raise cap back to 4 — queued waiters should now re-admit as
            # each release happens.
            _set_cap(4)
            blocker.release(first_three[1])
            blocker.release(first_three[2])
            # Both waiters should now enter (cap=4 → normal budget 3,
            # _active went 3→1 via two releases, so 2 more fit). Give a
            # generous 3 polls worth.
            assert _wait_for(
                lambda: len(blocker.entered()) == 5,
                timeout=5.0,
            ), (
                f"After restoring cap=4 and releasing two active jobs, all 5 should have run. "
                f"entered={len(blocker.entered())}"
            )
            blocker.release_all()
            get_job_gate()  # ensure singleton closure drop is fine

    def test_run_processing_raises_releases_slot(self, app):
        """An exception in run_processing must still release the slot.
        The outer finally at job_runner.py:~972 handles this via the
        ``if _slot_held`` guard; without it, one crashed job would
        wedge the cap forever."""
        from media_preview_generator.web.job_gate import get_job_gate
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(1)

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

    def test_startup_requeue_flood_is_paced_by_gate(self, app):
        """Simulate the _requeue_interrupted_on_startup path: 12 jobs
        started in rapid succession with cap=3. Exactly 2 should reach
        run_processing (the normal budget, ``cap - 1``); the other 10
        must sit queued. This is the exact regression that prompted the
        gate — without it, 30+ simultaneous enumerations would hammer
        Jellyfin's plugin endpoint. It also pins that a revive flood
        can never eat the slot held for incoming webhook work."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(3)
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

            assert _wait_for(lambda: len(blocker.entered()) == 2, timeout=3.0), (
                f"Under cap=3, exactly 2 of the 12 flood jobs must enter run_processing "
                f"(normal budget = cap - 1); got {len(blocker.entered())}"
            )
            # Give it a second — no more should squeeze in.
            time.sleep(1.0)
            assert len(blocker.entered()) == 2, (
                f"Flood must stay paced at 2 — no admissions without releases, and the third "
                f"slot stays reserved for high priority. entered={len(blocker.entered())}"
            )
            queued = [j for j in ids if j not in blocker.entered()]
            queued_messages = [jm.get_job(j).progress.current_item for j in queued]
            assert all(m.startswith("Queued —") for m in queued_messages), (
                f"All 10 waiting flood jobs must show a 'Queued —' message; got {queued_messages!r}"
            )

            blocker.release_all()
            _wait_for(
                lambda: all(jm.get_job(j).status.value in ("completed", "cancelled") for j in ids),
                timeout=8.0,
            )

    def test_high_priority_admits_into_the_slot_normals_cannot_take(self, app):
        """The issue #285 reservation, end to end.

        cap=3 with three normal jobs: two run, one waits. A high-priority
        job submitted afterwards must start immediately — it takes the
        reserved third slot — while the normal waiter stays queued. This
        is the reporter's scenario: a Sonarr import landing mid-full-scan
        should not have to wait out the scan.
        """
        from media_preview_generator.web.jobs import PRIORITY_HIGH, PRIORITY_NORMAL, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(3)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            scans = [jm.create_job(library_name=f"Scan {i}", config={}, priority=PRIORITY_NORMAL) for i in range(2)]
            for job in scans:
                _start_job_async(job.id, None)
            assert _wait_for(lambda: len(blocker.entered()) == 2, timeout=10.0)

            waiter = jm.create_job(library_name="Third scan", config={}, priority=PRIORITY_NORMAL)
            _start_job_async(waiter.id, None)
            assert _wait_for(
                lambda: (jm.get_job(waiter.id).progress.current_item or "").startswith("Queued —"),
                timeout=10.0,
            ), "Third normal job must be held by the reservation, not admitted"

            webhook = jm.create_job(library_name="Sonarr import", config={}, priority=PRIORITY_HIGH)
            _start_job_async(webhook.id, None)
            assert _wait_for(lambda: webhook.id in blocker.entered(), timeout=10.0), (
                f"High-priority job must take the reserved slot without waiting for a scan to "
                f"finish. entered={blocker.entered()!r}"
            )
            assert waiter.id not in blocker.entered(), (
                "The reserved slot belongs to high priority — the queued normal job must not "
                "have slipped in alongside it"
            )
            assert len(blocker.entered()) == 3, (
                f"Total in flight must never exceed the user's cap of 3; entered={len(blocker.entered())}"
            )

            blocker.release_all()
            _wait_for(
                lambda: all(
                    jm.get_job(j.id).status.value in ("completed", "cancelled") for j in (*scans, waiter, webhook)
                ),
                timeout=10.0,
            )

    def test_finished_high_job_does_not_widen_the_normal_budget(self, app):
        """Releasing a high-priority slot must settle up against the
        high counter, not the normal one.

        If ``release()`` decremented only the total, a finished
        high-priority job would leave the gate believing one of the
        remaining normal jobs was the high one — and the queued normal
        job would be admitted into the reserved slot, putting three
        normal jobs on a cap of 3. The reservation would then quietly
        evaporate after the first webhook of the session.
        """
        from media_preview_generator.web.jobs import PRIORITY_HIGH, PRIORITY_NORMAL, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        _set_cap(3)
        blocker = _BlockingRunProcessing()

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=blocker,
            ),
        ):
            jm = get_job_manager()
            scans = [jm.create_job(library_name=f"Scan {i}", config={}, priority=PRIORITY_NORMAL) for i in range(2)]
            for job in scans:
                _start_job_async(job.id, None)
            assert _wait_for(lambda: len(blocker.entered()) == 2, timeout=10.0)

            webhook = jm.create_job(library_name="Sonarr import", config={}, priority=PRIORITY_HIGH)
            _start_job_async(webhook.id, None)
            assert _wait_for(lambda: webhook.id in blocker.entered(), timeout=10.0)

            waiter = jm.create_job(library_name="Third scan", config={}, priority=PRIORITY_NORMAL)
            _start_job_async(waiter.id, None)
            assert _wait_for(
                lambda: (jm.get_job(waiter.id).progress.current_item or "").startswith("Queued —"),
                timeout=10.0,
            )

            # The high job finishes. Two normal jobs remain active, which
            # is already the whole normal budget — the waiter must stay put.
            blocker.release(webhook.id)
            assert _wait_for(
                lambda: jm.get_job(webhook.id).status.value in ("completed", "cancelled"),
                timeout=10.0,
            )
            time.sleep(1.5)  # Two gate poll ticks.
            assert waiter.id not in blocker.entered(), (
                f"Normal waiter was admitted into the reserved slot after the high job finished "
                f"— release() is not decrementing the high-slot counter. entered={blocker.entered()!r}"
            )

            # A normal job finishing does free the waiter.
            blocker.release(scans[0].id)
            assert _wait_for(lambda: waiter.id in blocker.entered(), timeout=10.0), (
                f"Waiter must admit once a NORMAL peer finishes; entered={blocker.entered()!r}"
            )

            blocker.release_all()
            _wait_for(
                lambda: all(
                    jm.get_job(j.id).status.value in ("completed", "cancelled") for j in (*scans, waiter, webhook)
                ),
                timeout=10.0,
            )
