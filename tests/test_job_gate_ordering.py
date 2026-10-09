"""Named admission requests preserve queue order without reserving capacity."""

import threading
from unittest.mock import Mock


def register(gate, name, *, created="2026-01-01", priority=3, ready=True, kind="loudness"):
    state = {"priority": priority, "ready": ready}
    gate.register_request(
        name, created_at=created, priority=priority, kind=kind, policy=lambda _done: (state["priority"], state["ready"])
    )
    return state


def test_registered_older_request_precedes_thread_that_reaches_gate_first(make_gate):
    gate = make_gate(1)
    register(gate, "old", created="2026-01-01")
    register(gate, "new", created="2026-01-02")
    waiting = threading.Event()
    admitted = threading.Event()
    thread = threading.Thread(
        target=lambda: (
            gate.acquire(3, lambda: False, on_wait=lambda *_: waiting.set(), request_id="new", kind="loudness"),
            admitted.set(),
        )
    )
    thread.start()
    assert waiting.wait(2)
    assert not admitted.is_set()
    assert gate.snapshot()[0] == 0
    assert gate.acquire(3, lambda: False, request_id="old", kind="loudness")
    gate.release()
    assert admitted.wait(2)
    thread.join(2)
    gate.release()
    assert gate.snapshot() == (0, 0, 1)


def test_deferred_or_incompatible_old_request_does_not_block_ready_other_kind(make_gate):
    gate = make_gate(5, lambda kind: 0 if kind == "loudness" else 1)
    register(gate, "old", kind="loudness")
    register(gate, "new", created="2026-01-02", kind="previews")
    assert gate.acquire(3, lambda: False, request_id="new", kind="previews")
    gate.release()
    assert gate.snapshot()[0] == 0


def test_duplicate_cleanup_cannot_remove_winning_runner(make_gate):
    gate = make_gate(1)
    register(gate, "old")
    owner = gate.claim_request("old")
    assert gate.claim_request("old") is None
    gate.finish_request("old", None)
    assert gate.has_request("old")
    gate.finish_request("old", owner)
    assert not gate.has_request("old")
    assert gate.snapshot() == (0, 0, 1)


def test_already_cancelled_request_never_takes_available_slot(make_gate):
    gate = make_gate(1)
    register(gate, "cancelled")
    assert not gate.acquire(3, lambda: True, request_id="cancelled", kind="loudness")
    assert gate.snapshot() == (0, 0, 1)


def test_actual_backoff_wait_remains_ineligible_until_runner_finishes_preflight(make_gate):
    gate = make_gate(1)
    register(gate, "old")
    gate.defer_preflight("old")
    register(gate, "new", created="2026-01-02")
    assert gate.acquire(3, lambda: False, request_id="new", kind="loudness")
    gate.release()
    gate.complete_preflight("old")
    assert gate.acquire(3, lambda: False, request_id="old", kind="loudness")
    gate.release()


def test_refresh_coalesces_waiters_and_reads_capacity_once_per_kind(monkeypatch, make_gate):
    capacity = Mock(return_value=1)
    gate = make_gate(5, capacity)
    monkeypatch.setattr("media_preview_generator.web.job_gate.time.monotonic", lambda: 10.0)
    for index in range(50):
        register(gate, str(index), kind="loudness")
    for _ in range(50):
        gate._refresh_requests()
    capacity.assert_called_once_with("loudness")


def test_priority_update_during_policy_snapshot_cannot_be_overwritten(make_gate):
    gate = make_gate(2)
    entered, proceed = threading.Event(), threading.Event()

    def policy(_done):
        entered.set()
        assert proceed.wait(2)
        return 3, True

    gate.register_request("old", created_at="2026-01-01", priority=3, kind="loudness", policy=policy)
    refresh = threading.Thread(target=gate._refresh_requests)
    refresh.start()
    assert entered.wait(2)
    gate.reprioritize("old", 1)
    proceed.set()
    refresh.join(2)
    request = gate._requests["old"]
    assert request.priority == 1
    assert not request.ready


def test_policy_and_capacity_callbacks_run_outside_gate_condition(make_gate):
    gate = make_gate(1)

    def policy(_done):
        acquired = threading.Event()

        def inspect():
            gate.snapshot()
            acquired.set()

        other = threading.Thread(target=inspect)
        other.start()
        assert acquired.wait(2), "Policy callback held the gate condition"
        other.join(2)
        return 3, True

    gate.register_request("old", created_at="2026-01-01", priority=3, kind="loudness", policy=policy)
    assert gate.acquire(3, lambda: False, request_id="old", kind="loudness")
    gate.release()


def test_terminal_unclaimed_reservation_is_pruned_before_next_admission(make_gate):
    gate = make_gate(1)
    gate.register_request(
        "cancelled-paused", created_at="2026-01-01", priority=3, kind="loudness", policy=lambda _done: None
    )
    register(gate, "new", created="2026-01-02")
    assert gate.acquire(3, lambda: False, request_id="new", kind="loudness")
    assert not gate.has_request("cancelled-paused")
    assert gate.snapshot()[1] == 0
    gate.release()


def test_terminal_snapshot_does_not_remove_owned_runner_request(make_gate):
    gate = make_gate(1)
    gate.register_request("owned", created_at="2026-01-01", priority=3, kind="loudness", policy=lambda _done: None)
    owner = gate.claim_request("owned")
    gate._refresh_requests()
    assert gate.has_request("owned")
    gate.finish_request("owned", owner)
    assert not gate.has_request("owned")
