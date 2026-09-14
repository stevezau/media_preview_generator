"""KeyedLocks: one lock per key, forgotten once nobody holds or waits for it."""

import threading

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
