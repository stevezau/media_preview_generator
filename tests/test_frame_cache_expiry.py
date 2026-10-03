"""The frame cache drops what has outlived its TTL when frames are stored, not only when they're looked up.

Expiry used to be checked only in ``get()``: frames of a file nobody asked for again stayed on disk until the 2 GB cap
pushed them out, and a restart forgot the directories of the process before it. Now ``put()`` and start-up sweep
expired entries and expired slot directories no entry owns. That sweep never removes a slot a dispatcher is writing or
reading (it holds that path's generation lock).
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from media_preview_generator.processing.frame_cache import FrameCache

_EXTRACTION_KEY = (10, 4, "hable")
_TTL_S = 100


@contextmanager
def _seconds_later(seconds: float):
    later = time.time() + seconds
    with patch("media_preview_generator.processing.frame_cache.time.time", return_value=later):
        yield


def _store(cache: FrameCache, media: Path) -> Path:
    """Extract "frames" for ``media`` into its slot and record them, as the dispatcher does."""
    media.write_bytes(b"x")
    slot = cache.frame_dir_for(str(media))
    slot.mkdir(parents=True, exist_ok=True)
    (slot / "0000000000.jpg").write_bytes(b"\xff\xd8\xff")
    cache.put(str(media), frame_dir=slot, frame_count=1, extraction_key=_EXTRACTION_KEY)
    return slot


def _leftover(base: Path, name: str, age_s: float) -> Path:
    """A slot directory a previous process left behind, last written ``age_s`` seconds ago."""
    slot = base / name
    slot.mkdir(parents=True)
    (slot / "0000000000.jpg").write_bytes(b"\xff\xd8\xff")
    then = time.time() - age_s
    os.utime(slot, (then, then))
    return slot


class TestPutEvictsExpiredEntries:
    def test_entry_older_than_the_ttl_is_evicted_when_another_is_stored(self, tmp_path):
        cache = FrameCache(tmp_path / "cache", ttl_seconds=_TTL_S)
        old_slot = _store(cache, tmp_path / "old.mkv")

        with _seconds_later(_TTL_S + 1):
            new_slot = _store(cache, tmp_path / "new.mkv")

        assert not old_slot.exists()
        assert new_slot.is_dir()
        assert len(cache) == 1
        assert cache.get(str(tmp_path / "new.mkv"), extraction_key=_EXTRACTION_KEY) is not None

    def test_entry_younger_than_the_ttl_is_kept_when_another_is_stored(self, tmp_path):
        cache = FrameCache(tmp_path / "cache", ttl_seconds=_TTL_S)
        fresh_slot = _store(cache, tmp_path / "fresh.mkv")

        with _seconds_later(_TTL_S - 1):
            _store(cache, tmp_path / "new.mkv")

        assert fresh_slot.is_dir()
        assert len(cache) == 2

    def test_the_entry_just_stored_is_kept_even_with_no_ttl(self, tmp_path):
        """The caller is about to publish from these frames."""
        cache = FrameCache(tmp_path / "cache", ttl_seconds=0)
        media = tmp_path / "m.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        slot.mkdir(parents=True)
        (slot / "0000000000.jpg").write_bytes(b"\xff\xd8\xff")

        with patch("media_preview_generator.processing.frame_cache.time.time", side_effect=[1000.0, 1001.0, 1002.0]):
            cache.put(str(media), frame_dir=slot, frame_count=1, extraction_key=_EXTRACTION_KEY)

        assert slot.is_dir()
        assert len(cache) == 1

    def test_expired_entry_a_dispatcher_is_using_survives_until_it_is_done(self, tmp_path):
        cache = FrameCache(tmp_path / "cache", ttl_seconds=_TTL_S)
        in_use = tmp_path / "in_use.mkv"
        in_use_slot = _store(cache, in_use)
        idle_slot = _store(cache, tmp_path / "idle.mkv")

        reading = cache.generation_lock(str(in_use))
        reading.acquire()  # a dispatcher that hit the cache is still publishing from it
        try:
            with _seconds_later(_TTL_S + 1):
                _store(cache, tmp_path / "other.mkv")

            assert in_use_slot.is_dir()
            assert (in_use_slot / "0000000000.jpg").is_file()
            assert not idle_slot.exists()
        finally:
            reading.release()

        with _seconds_later(_TTL_S + 2):
            _store(cache, tmp_path / "another.mkv")

        assert not in_use_slot.exists()
        assert reading.acquire(blocking=False), "the sweep must release every lock it took"


class TestLeftoversOfAPreviousProcess:
    def test_expired_leftovers_are_cleared_at_start_up(self, tmp_path):
        base = tmp_path / "cache"
        expired = _leftover(base, "frames-0123456789abcdef", age_s=_TTL_S + 60)
        recent = _leftover(base, "frames-fedcba9876543210", age_s=_TTL_S - 60)

        cache = FrameCache(base, ttl_seconds=_TTL_S)

        assert not expired.exists()
        assert recent.is_dir(), "younger than the TTL: another process may still be writing it"
        assert len(cache) == 0

    def test_a_leftover_is_cleared_by_a_later_store_once_it_has_expired(self, tmp_path):
        base = tmp_path / "cache"
        recent = _leftover(base, "frames-fedcba9876543210", age_s=0)
        cache = FrameCache(base, ttl_seconds=_TTL_S)
        assert recent.is_dir()

        with _seconds_later(_TTL_S + 1):
            new_slot = _store(cache, tmp_path / "new.mkv")

        assert not recent.exists()
        assert new_slot.is_dir()

    def test_only_slot_directories_are_touched(self, tmp_path):
        base = tmp_path / "cache"
        not_ours = [
            _leftover(base, "frames-not-a-slot-name", age_s=_TTL_S + 60),
            _leftover(base, "frames-0123456789ABCDEF", age_s=_TTL_S + 60),
            _leftover(base, "other", age_s=_TTL_S + 60),
        ]
        a_file = base / "frames-00000000000000aa"
        a_file.write_bytes(b"not a directory")
        then = time.time() - (_TTL_S + 60)
        os.utime(a_file, (then, then))

        cache = FrameCache(base, ttl_seconds=_TTL_S)
        with _seconds_later(_TTL_S + 1):
            _store(cache, tmp_path / "new.mkv")

        assert all(path.is_dir() for path in not_ours)
        assert a_file.is_file()

    def test_expired_leftover_a_dispatcher_is_writing_survives_until_it_is_done(self, tmp_path):
        """A slow extraction can leave its slot untouched for longer than a short TTL."""
        base = tmp_path / "cache"
        cache = FrameCache(base, ttl_seconds=_TTL_S)
        extracting = tmp_path / "extracting.mkv"
        slot = _leftover(base, cache.frame_dir_for(str(extracting)).name, age_s=_TTL_S + 60)

        writing = cache.generation_lock(str(extracting))
        writing.acquire()  # a dispatcher is extracting into this slot; it has not stored an entry yet
        try:
            _store(cache, tmp_path / "other.mkv")
            assert (slot / "0000000000.jpg").is_file()
        finally:
            writing.release()

        _store(cache, tmp_path / "another.mkv")
        assert not slot.exists()
        assert writing.acquire(blocking=False), "the sweep must release every lock it took"
