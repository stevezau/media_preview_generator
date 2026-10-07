"""Every kind with work and open workers gets a slot first, except when only the high-priority reserved slot is free or cap=1; kind-less waiters are not held back."""

import threading
import time

import pytest

from media_preview_generator.web.job_gate import JobGate
from media_preview_generator.web.jobs import PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_NORMAL

HIGH, NORMAL, LOW = PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW


def _admitted_after_queueing(
    cap: int, limits: dict, jobs: list[tuple[str, str, int]], not_ready: frozenset[str] = frozenset()
) -> list[str]:
    """Register every job first (as job_runner's restart drain does), then let each runner ask for a slot."""
    gate = JobGate(lambda: cap, kind_capacity_provider=lambda kind: limits[kind])
    gate._POLL_SECONDS = 0.005
    for index, (name, kind, priority) in enumerate(jobs):
        gate.register_request(
            name,
            created_at=f"2026-10-07T00:00:{index:02d}",
            priority=priority,
            kind=kind,
            policy=lambda _done, priority=priority, name=name: (priority, name not in not_ready),
        )
    admitted: list[str] = []
    stop = threading.Event()

    def run(name: str, kind: str, priority: int) -> None:
        if gate.acquire(priority, stop.is_set, kind=kind, request_id=name):
            admitted.append(name)

    threads = [threading.Thread(target=run, args=job, daemon=True) for job in jobs]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(admitted) < cap:
        time.sleep(0.01)
    time.sleep(0.1)  # nothing else may sneak in once the gate is full
    stop.set()
    for thread in threads:
        thread.join(1)
    return admitted


def _jobs(loudness: int, loud_priority: int, previews: int, preview_priority: int) -> list[tuple[str, str, int]]:
    return [(f"L{i}", "loudness", loud_priority) for i in range(1, loudness + 1)] + [
        (f"P{i}", "previews", preview_priority) for i in range(1, previews + 1)
    ]


@pytest.mark.parametrize(
    ("cap", "loud_priority", "preview_priority", "expected"),
    [
        # The owner's fear: urgent loudness must not hold every slot while GPU preview workers sit idle.
        (3, HIGH, NORMAL, {"loudness": 2, "previews": 1}),
        (5, HIGH, NORMAL, {"loudness": 4, "previews": 1}),
        # The production mix (webhook previews default to High): loudness keeps one slot for the CPU workers.
        (3, NORMAL, HIGH, {"loudness": 1, "previews": 2}),
        (5, NORMAL, HIGH, {"loudness": 1, "previews": 4}),
        # Equal priority already shared slots; the third/fifth slot stays reserved for high priority.
        (3, NORMAL, NORMAL, {"loudness": 1, "previews": 1}),
        (5, NORMAL, NORMAL, {"loudness": 3, "previews": 1}),
    ],
)
def test_waiting_kind_with_free_workers_gets_a_slot_when_twenty_loudness_jobs_are_ahead(
    cap, loud_priority, preview_priority, expected
):
    admitted = _admitted_after_queueing(
        cap, {"loudness": 5, "previews": 5}, _jobs(20, loud_priority, 10, preview_priority)
    )
    assert {kind: sum(name.startswith(kind[0].upper()) for name in admitted) for kind in expected} == expected


def test_one_kind_alone_still_uses_every_slot():
    admitted = _admitted_after_queueing(5, {"loudness": 5, "previews": 5}, _jobs(10, HIGH, 0, NORMAL))
    assert len(admitted) == 5


def test_kind_that_cannot_run_now_does_not_hold_a_slot_back():
    # Previews have no open workers (off-hours or no compatible member): loudness may take every slot.
    admitted = _admitted_after_queueing(5, {"loudness": 5, "previews": 0}, _jobs(10, HIGH, 3, HIGH))
    assert sorted(admitted) == ["L1", "L2", "L3", "L4", "L5"]


def test_high_priority_reservation_still_holds_back_the_last_slot_for_urgent_work():
    admitted = _admitted_after_queueing(3, {"loudness": 5, "previews": 5}, _jobs(5, NORMAL, 5, NORMAL))
    assert len(admitted) == 2  # one loudness, one preview; the third slot stays free for priority 1


def test_zero_slot_kind_that_is_not_ready_does_not_hold_a_slot_back():
    # Previews hold no slot but their policy says not ready, so loudness may still fill every slot.
    jobs = _jobs(10, HIGH, 3, HIGH)
    not_ready = frozenset(name for name, kind, _ in jobs if kind == "previews")
    admitted = _admitted_after_queueing(5, {"loudness": 5, "previews": 5}, jobs, not_ready)
    assert sorted(admitted) == ["L1", "L2", "L3", "L4", "L5"]


@pytest.mark.parametrize(
    ("cap", "loud_priority", "preview_priority", "expected"),
    [
        # cap=1: the reservation and the kind rule are both off; priority then FIFO rotates the single slot.
        (1, HIGH, NORMAL, ["L1"]),
        (1, NORMAL, HIGH, ["P1"]),
        (1, NORMAL, NORMAL, ["L1"]),
        # cap=2: both slots are open to HIGH, so the kind rule still gives each kind one slot.
        (2, HIGH, NORMAL, ["L1", "P1"]),
        (2, NORMAL, HIGH, ["L1", "P1"]),
        # cap=2 at equal NORMAL priority: one slot is HIGH-reserved, so only one kind runs and the rule cannot help.
        (2, NORMAL, NORMAL, ["L1"]),
        # LOW previews behind NORMAL loudness: one slot each, the third stays reserved for HIGH.
        (3, NORMAL, LOW, ["L1", "P1"]),
    ],
)
def test_small_caps_and_low_priority_admit_the_real_outcome(cap, loud_priority, preview_priority, expected):
    admitted = _admitted_after_queueing(
        cap, {"loudness": 5, "previews": 5}, _jobs(3, loud_priority, 3, preview_priority)
    )
    assert sorted(admitted) == sorted(expected)


def _gate_with_held_slots(cap: int, held: list[tuple[str, int]]) -> JobGate:
    gate = JobGate(lambda: cap, kind_capacity_provider=lambda kind: 5)
    gate._POLL_SECONDS = 0.005
    for kind, priority in held:
        assert gate.acquire(priority, lambda: False, kind=kind)
    return gate


def _admission_order_after_release(
    gate: JobGate, waiters: list[tuple[str, str, int]], release: tuple[int, str]
) -> list[str]:
    admitted: list[str] = []
    stop = threading.Event()

    def run(name: str, kind: str, priority: int) -> None:
        if gate.acquire(priority, stop.is_set, kind=kind):
            admitted.append(name)

    threads = [threading.Thread(target=run, args=w, daemon=True) for w in waiters]
    for thread in threads:
        thread.start()
        time.sleep(0.02)  # stagger so FIFO order is the registration order
    time.sleep(0.1)
    assert admitted == []  # gate is full before the release
    gate.release(release[0], kind=release[1])
    time.sleep(0.1)
    stop.set()
    for thread in threads:
        thread.join(1)
    return admitted


def test_release_admits_the_next_waiter_of_the_kind_that_just_lost_its_only_slot():
    gate = _gate_with_held_slots(3, [("loudness", HIGH), ("loudness", HIGH), ("previews", NORMAL)])
    waiters = [("L3", "loudness", HIGH), ("L4", "loudness", HIGH), ("P2", "previews", NORMAL)]
    admitted = _admission_order_after_release(gate, waiters, (NORMAL, "previews"))
    assert admitted == ["P2"]  # ahead of the higher-priority, earlier loudness waiters


def test_release_hands_the_freed_slot_to_the_releasing_kind_even_over_a_high_priority_waiter():
    # Deliberate contract: loudness just dropped to zero slots, so its LOW waiter goes before HIGH previews,
    # which already hold two slots. The kind rule outranks priority until every kind with work has a slot.
    gate = _gate_with_held_slots(3, [("previews", HIGH), ("previews", NORMAL), ("loudness", NORMAL)])
    waiters = [("P3", "previews", HIGH), ("L2", "loudness", LOW)]
    admitted = _admission_order_after_release(gate, waiters, (NORMAL, "loudness"))
    assert admitted == ["L2"]
