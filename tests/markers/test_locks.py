"""KeyedLocks: one lock per key, forgotten once nobody holds or waits for it."""

import threading
import time

import pytest

from media_preview_generator.markers.locks import KeyedLocks


def test_same_key_is_exclusive_and_different_keys_are_not():
    locks = KeyedLocks()
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def first():
        with locks.hold("a"):
            order.append("first-in")
            entered.set()
            release.wait(5)
            order.append("first-out")

    def second_holder():
        with locks.hold("a"):
            order.append("second-in")

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(5)
    with locks.hold("b"):
        order.append("other-key")
    second = threading.Thread(target=second_holder)
    second.start()
    second.join(0.2)
    assert order == ["first-in", "other-key"]
    release.set()
    t.join(5)
    second.join(5)
    assert order == ["first-in", "other-key", "first-out", "second-in"]


def test_lock_entry_is_dropped_when_released():
    locks = KeyedLocks()
    with locks.hold(7):
        assert 7 in locks._locks
    assert locks._locks == {}


def test_lock_factory_is_used():
    made = []

    def factory():
        made.append(threading.Lock())
        return made[-1]

    locks = KeyedLocks(lock_factory=factory)
    with locks.hold("x"):
        pass
    assert len(made) == 1


def test_try_hold_takes_a_free_lock_and_holds_it_exclusively():
    locks = KeyedLocks()
    blocked = threading.Event()

    with locks.try_hold("a", 1.0) as taken:
        assert taken is True

        def other():
            with locks.try_hold("a", 0.05) as second:
                if not second:
                    blocked.set()

        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        assert blocked.is_set(), "try_hold handed the same key to two callers at once"
    assert locks._locks == {}


def test_try_hold_gives_up_without_holding_anything_and_leaves_the_key_usable():
    """The cell that matters: a not-acquired try_hold must not leak the lock, or every later caller deadlocks."""
    locks = KeyedLocks()
    held = threading.Event()
    release = threading.Event()

    def holder():
        with locks.hold("a"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    with locks.try_hold("a", 0.05) as taken:
        assert taken is False
    release.set()
    t.join(5)
    assert locks._locks == {}  # the waiter's entry is dropped again
    with locks.try_hold("a", 1.0) as taken:
        assert taken is True  # nothing was left behind


@pytest.mark.parametrize("raising", [False, True], ids=["normal-exit", "exception"])
def test_try_hold_releases_the_lock_for_a_caller_already_waiting(raising):
    """Leaving the lock held has to be caught with a caller ALREADY waiting on the key.

    Once the last caller leaves, the key's whole entry (lock object included) is dropped, so a leaked lock is
    invisible to anyone arriving later -- they get a brand-new lock. The caller it strands is the one that was
    already blocked on the old one.
    """
    locks = KeyedLocks()
    got_it = threading.Event()

    def waiter():
        with locks.hold("a"):
            got_it.set()

    # Daemon: with the release dropped this thread never comes back, and the run has to fail rather than hang.
    t = threading.Thread(target=waiter, daemon=True)
    try:
        with locks.try_hold("a", 1.0) as taken:
            assert taken is True
            t.start()
            deadline = time.monotonic() + 5
            while locks._locks.get("a", [None, 0])[1] < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert locks._locks["a"][1] == 2, "the waiter never registered on the key"
            assert not got_it.is_set()  # and it is still waiting, not holding
            if raising:
                raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert got_it.wait(5), "try_hold left the lock held, so the waiting caller is stuck for good"
    t.join(5)
    assert locks._locks == {}


class _StopWaiting(Exception):
    pass


def test_a_wait_ends_when_while_waiting_raises_and_leaves_the_key_as_it_was():
    # A job frozen by its schedule's stop time can hold a key for hours: a waiter must be able to stop (its own cancel,
    # or a holder held longer than any running one takes) without holding or leaking anything.
    locks = KeyedLocks()
    holding, release = threading.Event(), threading.Event()

    def holder():
        with locks.hold("a"):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert holding.wait(5)
    asked = []

    def while_waiting():
        asked.append(time.monotonic())
        if len(asked) == 3:
            raise _StopWaiting

    with pytest.raises(_StopWaiting):
        with locks.hold("a", while_waiting=while_waiting, poll_s=0.05):
            pytest.fail("held a lock another caller holds")
    assert len(asked) == 3
    assert locks._locks["a"][1] == 1  # only the holder counts
    release.set()
    t.join(5)
    assert "a" not in locks._locks
    with locks.hold("a", while_waiting=while_waiting):
        pass  # a free key is taken at once, without asking


def test_while_waiting_is_never_asked_for_a_free_key():
    locks = KeyedLocks()
    with locks.hold("a", while_waiting=lambda: pytest.fail("asked while the key was free")):
        assert locks._locks["a"][1] == 1
    assert "a" not in locks._locks
