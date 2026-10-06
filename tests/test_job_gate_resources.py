"""Resource admission leaves room for other work without defeating priority."""

import threading

import pytest

from media_preview_generator.web.job_gate import JobGate
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
def setup_gate():
    waiters = []

    def make(cap=5, limits=None):
        limits = limits if limits is not None else {"loudness": 1, "previews": 4}
        gate = JobGate(lambda: cap, kind_capacity_provider=lambda kind: limits[kind])
        gate._POLL_SECONDS = 0.01

        def waiter(kind, priority=PRIORITY_NORMAL, **kwargs):
            held = Waiter(gate, kind, priority, **kwargs)
            waiters.append(held)
            return held

        return gate, waiter

    yield make
    for waiter in waiters:
        waiter.stop()


def test_busy_loudness_does_not_block_preview_and_same_kind_stays_fifo(setup_gate):
    gate, waiter = setup_gate()
    first = waiter("loudness")
    first.ready()
    second = waiter("loudness")
    second.waiting()
    third = waiter("loudness")
    third.waiting()
    preview = waiter("previews")
    preview.ready()
    assert gate.snapshot() == (2, 2, 5)
    gate.release(PRIORITY_NORMAL, kind="loudness")
    second.ready()
    third.waiting()
    gate.release(PRIORITY_NORMAL, kind="loudness")
    third.ready()


def test_higher_priority_can_reach_dispatcher_but_equal_or_lower_waits(setup_gate):
    gate, waiter = setup_gate()
    waiter("loudness", PRIORITY_LOW).ready()
    waiter("loudness", PRIORITY_NORMAL).ready()
    waiter("loudness", PRIORITY_HIGH).ready()
    waiter("loudness", PRIORITY_HIGH).waiting()
    waiter("loudness", PRIORITY_NORMAL).waiting()
    waiter("loudness", PRIORITY_LOW).waiting()
    assert gate.snapshot() == (3, 3, 5)
    gate.release(PRIORITY_HIGH, kind="loudness")


def test_two_cpu_workers_cannot_fill_every_ordinary_slot_across_priority_tiers(setup_gate):
    gate, waiter = setup_gate(limits={"loudness": 2, "previews": 4})
    waiter("loudness", PRIORITY_LOW).ready()
    waiter("loudness", PRIORITY_LOW).ready()
    waiter("loudness", PRIORITY_NORMAL).ready()
    waiter("loudness", PRIORITY_NORMAL).waiting()
    waiter("previews").ready()
    waiter("loudness", PRIORITY_HIGH).ready()
    assert gate.snapshot() == (5, 1, 5)


def test_default_cap_normal_webhook_can_pass_a_low_loudness_scan(setup_gate):
    gate, waiter = setup_gate(cap=3)
    waiter("loudness", PRIORITY_LOW).ready()
    waiter("loudness", PRIORITY_NORMAL).ready()
    waiter("loudness", PRIORITY_NORMAL).waiting()
    waiter("previews", PRIORITY_HIGH).ready()
    assert gate.snapshot() == (3, 1, 3)


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_small_global_caps_remain_usable_and_keep_the_reservation(setup_gate, cap):
    gate, waiter = setup_gate(cap=cap, limits={"loudness": 4, "previews": 4})
    waiter("loudness").ready()
    waiter("loudness").waiting()
    preview = waiter("previews")
    if cap == 3:
        preview.ready()
        assert gate.snapshot()[0] == 2
    else:
        preview.waiting()
        assert gate.snapshot()[0] == 1
    high = waiter("previews", PRIORITY_HIGH)
    if cap > 1:
        high.ready()
        assert gate.snapshot()[0] == cap
    else:
        high.waiting()


def test_dynamic_capacity_and_cancelled_waiter_recover_without_leaking_slots(setup_gate):
    limits = {"loudness": 0, "previews": 4}
    gate, waiter = setup_gate(limits=limits)
    messages = []
    blocked = waiter("loudness", on_resource_wait=lambda: messages.append("resource"))
    blocked.waiting()
    assert messages
    blocked.stop()
    assert gate.snapshot() == (0, 0, 5)
    live = waiter("loudness")
    live.waiting()
    limits["loudness"] = 1
    live.ready()
    next_job = waiter("loudness")
    next_job.waiting()
    limits["loudness"] = 2
    next_job.ready()
    assert gate.snapshot() == (2, 0, 5)


def test_capacity_provider_runs_outside_condition_and_failure_fails_closed():
    broken = True

    def capacity(kind):
        assert kind == "loudness"
        assert not gate._cond._is_owned()
        if broken:
            raise RuntimeError("policy unavailable")
        return 1

    gate = JobGate(lambda: 5, kind_capacity_provider=capacity)
    gate._POLL_SECONDS = 0.01
    held = Waiter(gate, "loudness")
    try:
        held.waiting()
        assert gate.snapshot() == (0, 1, 5)
        broken = False
        held.ready()
        gate.release(PRIORITY_NORMAL, kind="loudness")
        assert gate.snapshot() == (0, 0, 5)
    finally:
        held.stop()


def test_unknown_legacy_capacity_keeps_prior_admission_behavior():
    gate = JobGate(lambda: 5, kind_capacity_provider=lambda kind: None)
    for _ in range(4):
        assert gate.acquire(PRIORITY_NORMAL, lambda: False, kind="loudness")
    assert gate.snapshot() == (4, 0, 5)


@pytest.mark.parametrize("kind", ["loudness", "intro_credits"])
def test_shared_pause_handoff_releases_kind_and_reacquires_at_live_priority(monkeypatch, kind):
    from types import SimpleNamespace

    from media_preview_generator.markers import job_runner

    gate = JobGate(lambda: 5, kind_capacity_provider=lambda _: 1)
    assert gate.acquire(PRIORITY_LOW, lambda: False, kind=kind)
    slot = {"held": True, "priority": PRIORITY_LOW, "kind": kind}
    paused = True
    calls = 0

    def wait(timeout):
        nonlocal paused, calls
        calls += 1
        if calls == 2:
            assert gate.snapshot()[0] == 0
            paused = False
        return calls == 3

    manager = SimpleNamespace(is_pause_requested=lambda _: paused, add_log=lambda *_: None)
    monkeypatch.setattr(job_runner, "get_job_manager", lambda: manager)
    monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
    tracker = SimpleNamespace(wait=wait, done_event=threading.Event())
    job_runner.wait_releasing_slot_while_paused(
        tracker,
        job_id="held",
        slot=slot,
        live_priority=lambda: PRIORITY_HIGH,
        cancel_check=lambda: False,
        on_wait=lambda *_: None,
    )
    assert slot == {"held": True, "priority": PRIORITY_HIGH, "kind": kind}
    assert gate.snapshot() == (1, 0, 5)
    assert gate._kind_active[(kind, PRIORITY_LOW)] == 0
    assert gate._kind_active[(kind, PRIORITY_HIGH)] == 1
    gate.release(slot["priority"], kind=slot["kind"])
    assert gate.snapshot() == (0, 0, 5)
