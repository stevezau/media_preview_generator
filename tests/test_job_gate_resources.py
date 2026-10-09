"""The start-up gate: priority then FIFO, kinds with no open workers wait, and nothing is held back for anyone."""

import threading

import pytest

from media_preview_generator.web.jobs import PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_NORMAL


class Waiter:
    def __init__(self, gate, kind, priority=PRIORITY_NORMAL, **kwargs):
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.admitted = False

        def run():
            self.admitted = gate.acquire(priority, self.cancel.is_set, kind=kind, **kwargs)
            self.done.set()

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def ready(self):
        assert self.done.wait(1)
        assert self.admitted

    def waiting(self):
        assert not self.done.wait(0.03)

    def stop(self):
        self.cancel.set()
        self.thread.join(1)
        assert not self.thread.is_alive()


@pytest.fixture
def setup_gate(make_gate):
    waiters = []

    def make(slots=3, limits=None):
        limits = limits if limits is not None else {"loudness": 1, "previews": 4}
        gate = make_gate(slots, lambda kind: limits[kind])
        gate._POLL_SECONDS = 0.01

        def waiter(kind, priority=PRIORITY_NORMAL, **kwargs):
            held = Waiter(gate, kind, priority, **kwargs)
            waiters.append(held)
            return held

        return gate, waiter

    yield make
    for waiter in waiters:
        waiter.stop()


def test_a_busy_kind_does_not_limit_its_own_start_up_and_same_priority_stays_fifo(setup_gate):
    gate, waiter = setup_gate(slots=2)
    waiter("loudness").ready()
    waiter("loudness").ready()
    third = waiter("loudness")
    third.waiting()
    fourth = waiter("loudness")
    fourth.waiting()
    gate.release()
    third.ready()
    fourth.waiting()
    gate.release()
    fourth.ready()


def test_priority_decides_who_starts_up_next_when_a_slot_frees(setup_gate):
    gate, waiter = setup_gate(slots=1)
    waiter("previews").ready()
    low = waiter("previews", PRIORITY_LOW)
    low.waiting()
    normal = waiter("previews", PRIORITY_NORMAL)
    normal.waiting()
    high = waiter("previews", PRIORITY_HIGH)
    high.waiting()
    gate.release()
    high.ready()
    normal.waiting()
    gate.release()
    normal.ready()
    low.waiting()


def test_no_slot_is_held_back_for_high_priority(setup_gate):
    gate, waiter = setup_gate(slots=3)
    for _ in range(3):
        waiter("previews", PRIORITY_NORMAL).ready()
    assert gate.snapshot() == (3, 0, 3)
    waiter("previews", PRIORITY_HIGH).waiting()


def test_a_kind_with_no_open_workers_waits_and_lets_other_kinds_pass(setup_gate):
    gate, waiter = setup_gate(limits={"loudness": 0, "previews": 4})
    messages = []
    blocked = waiter("loudness", PRIORITY_HIGH, on_resource_wait=lambda: messages.append("resource"))
    blocked.waiting()
    assert messages
    waiter("previews", PRIORITY_LOW).ready()
    assert gate.snapshot() == (1, 1, 3)


def test_dynamic_capacity_and_cancelled_waiter_recover_without_leaking_slots(setup_gate):
    limits = {"loudness": 0, "previews": 4}
    gate, waiter = setup_gate(limits=limits)
    blocked = waiter("loudness")
    blocked.waiting()
    blocked.stop()
    assert gate.snapshot() == (0, 0, 3)
    live = waiter("loudness")
    live.waiting()
    limits["loudness"] = 1
    live.ready()
    assert gate.snapshot() == (1, 0, 3)


def test_capacity_provider_runs_outside_condition_and_failure_fails_closed(make_gate):
    broken = True

    def capacity(kind):
        assert kind == "loudness"
        assert not gate._cond._is_owned()
        if broken:
            raise RuntimeError("policy unavailable")
        return 1

    gate = make_gate(3, capacity)
    gate._POLL_SECONDS = 0.01
    held = Waiter(gate, "loudness")
    try:
        held.waiting()
        assert gate.snapshot() == (0, 1, 3)
        broken = False
        held.ready()
        gate.release()
        assert gate.snapshot() == (0, 0, 3)
    finally:
        held.stop()


def test_unknown_legacy_capacity_keeps_prior_admission_behavior(make_gate):
    gate = make_gate(5, lambda kind: None)
    for _ in range(4):
        assert gate.acquire(PRIORITY_NORMAL, lambda: False, kind="loudness")
    assert gate.snapshot() == (4, 0, 5)


def test_gate_reads_no_settings(monkeypatch):
    from media_preview_generator.web import job_gate, settings_manager

    def explode(*_args, **_kwargs):
        raise AssertionError("the gate must not read settings")

    monkeypatch.setattr(settings_manager, "get_settings_manager", explode)
    job_gate.reset_job_gate()
    try:
        gate = job_gate.get_job_gate()
        assert gate.acquire(PRIORITY_NORMAL, lambda: False)
        assert gate.snapshot() == (1, 0, job_gate.STARTUP_SLOTS)
        gate.release()
    finally:
        job_gate.reset_job_gate()


def test_release_slot_gives_back_a_held_slot_once(make_gate):
    from media_preview_generator.web import job_gate

    gate = make_gate(1)
    assert gate.acquire(PRIORITY_NORMAL, lambda: False)
    slot = {"held": True}
    job_gate.release_slot(slot, gate)
    job_gate.release_slot(slot, gate)
    assert slot == {"held": False}
    assert gate.snapshot() == (0, 0, 1)
