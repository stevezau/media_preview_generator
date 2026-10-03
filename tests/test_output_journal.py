"""Tests for the per-output journal (smart dedup)."""

from __future__ import annotations

import json
import os

import pytest

from media_preview_generator.output.journal import (
    JOURNAL_SCHEMA_VERSION,
    _meta_path_for,
    clear_meta,
    outputs_fresh_for_source,
    write_meta,
)
from media_preview_generator.output.plex_hash import get_source_fingerprint


class TestMetaPath:
    def test_meta_path_appends_meta_suffix(self, tmp_path):
        bif = tmp_path / "movie-320-10.bif"
        assert _meta_path_for(bif).name == "movie-320-10.bif.meta"

    def test_meta_path_handles_no_extension(self, tmp_path):
        f = tmp_path / "noext"
        assert _meta_path_for(f).name == "noext.meta"


class TestWriteMeta:
    def test_writes_one_meta_per_output(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 1234)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"a")
        out_b.write_bytes(b"b")

        write_meta([out_a, out_b], str(source), publisher="emby_sidecar")

        meta_a = json.loads(_meta_path_for(out_a).read_text())
        meta_b = json.loads(_meta_path_for(out_b).read_text())
        assert meta_a["source_size"] == 1234
        assert meta_a["source_path"] == str(source)
        assert meta_a["publisher"] == "emby_sidecar"
        assert meta_a["schema"] == JOURNAL_SCHEMA_VERSION
        assert meta_b["source_size"] == 1234
        assert meta_a["source_fingerprint"] == list(get_source_fingerprint(source))

    def test_silently_skips_when_source_missing(self, tmp_path):
        out = tmp_path / "a.bif"
        out.write_bytes(b"bif")
        write_meta([out], str(tmp_path / "ghost.mkv"))
        assert not _meta_path_for(out).exists()

    def test_failure_to_write_one_meta_does_not_block_others(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"data")
        good = tmp_path / "good.bif"
        good.write_bytes(b"bif")

        # Make a path whose parent doesn't exist so .meta write fails.
        bad = tmp_path / "subdir-not-created" / "bad.bif"

        write_meta([good, bad], str(source))

        assert _meta_path_for(good).exists()
        assert not _meta_path_for(bad).exists()


class TestOutputsFreshForSource:
    @pytest.mark.parametrize("require_fingerprint", [False, True])
    def test_same_size_replacement_preserving_mtime_invalidates_new_journal(self, tmp_path, require_fingerprint):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"old bytes")
        before = source.stat()
        out = tmp_path / "out.bif"
        out.write_bytes(b"old frames")
        write_meta([out], str(source))
        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"new bytes")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, source)

        assert not outputs_fresh_for_source([out], str(source), require_source_fingerprint=require_fingerprint)

    @pytest.mark.parametrize("metadata", ["absent", "legacy", "corrupt", "strong"])
    def test_cross_publisher_reuse_requires_strong_matching_metadata(self, tmp_path, metadata):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"source")
        out = tmp_path / "out.bif"
        out.write_bytes(b"frames")
        if metadata in ("legacy", "strong"):
            write_meta([out], str(source))
        if metadata == "legacy":
            payload = json.loads(_meta_path_for(out).read_text())
            del payload["source_fingerprint"]
            for source_record in payload["sources"]:
                del source_record["source_fingerprint"]
            _meta_path_for(out).write_text(json.dumps(payload))
        if metadata == "corrupt":
            _meta_path_for(out).write_text("not json")

        assert outputs_fresh_for_source([out], str(source)) is True
        assert outputs_fresh_for_source([out], str(source), require_source_fingerprint=True) is (metadata == "strong")

    def test_journal_uses_extraction_snapshot_when_source_changes_before_stamping(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"old bytes")
        expected = get_source_fingerprint(source)
        out = tmp_path / "out.bif"
        out.write_bytes(b"frames from old source")
        source.write_bytes(b"new different bytes")

        write_meta([out], str(source), source_fingerprint=expected)

        payload = json.loads(_meta_path_for(out).read_text())
        assert payload["source_fingerprint"] == list(expected)
        assert not outputs_fresh_for_source([out], str(source))

    def test_fresh_when_meta_matches(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        write_meta([out], str(source))

        assert outputs_fresh_for_source([out], str(source)) is True

    def test_stale_when_source_replaced(self, tmp_path):
        """Sonarr quality upgrade: file replaced in place → not fresh."""
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        write_meta([out], str(source))

        # Replace source with different size + new mtime.
        source.write_bytes(b"y" * 99999)

        assert outputs_fresh_for_source([out], str(source)) is False

    def test_stale_when_source_grew(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        write_meta([out], str(source))

        with source.open("ab") as f:
            f.write(b"y" * 50)

        assert outputs_fresh_for_source([out], str(source)) is False

    def test_legacy_outputs_with_no_meta_treated_as_fresh(self, tmp_path):
        """Pre-journal outputs: outputs exist but no .meta sidecar.

        Upgrade migration: don't force regen on the first webhook after
        the journal feature ships. Stamp on the next publish so future
        calls go through the strict check.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        # No write_meta call.
        assert outputs_fresh_for_source([out], str(source)) is True

    def test_not_fresh_when_output_missing(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        # output never created
        assert outputs_fresh_for_source([out], str(source)) is False

    def test_not_fresh_when_source_missing(self, tmp_path):
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        assert outputs_fresh_for_source([out], str(tmp_path / "ghost.mkv")) is False

    def test_handles_corrupt_meta_as_legacy(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        _meta_path_for(out).write_text("not json {")
        # Corrupt meta is ignored entirely; behaves as legacy.
        assert outputs_fresh_for_source([out], str(source)) is True

    def test_one_match_is_enough_when_others_have_no_meta(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"bif")
        out_b.write_bytes(b"bif")
        # Only out_a stamped.
        write_meta([out_a], str(source))
        assert outputs_fresh_for_source([out_a, out_b], str(source)) is True

    def test_mismatch_on_one_meta_invalidates_freshness(self, tmp_path):
        """If even one .meta says source changed, treat as stale.

        Conservative: avoids a corner case where a publisher updated
        once with an old source version, the source then changed, and
        another publisher hasn't yet run.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"bif")
        out_b.write_bytes(b"bif")
        write_meta([out_a, out_b], str(source))
        # Source replaced after stamping.
        source.write_bytes(b"y" * 200)
        assert outputs_fresh_for_source([out_a, out_b], str(source)) is False

    def test_not_fresh_when_no_outputs(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"")
        assert outputs_fresh_for_source([], str(source)) is False

    def test_match_beats_mismatch_for_same_source(self, tmp_path):
        """Mutation-testing closer (journal.py:142 — `saw_match = True`).

        When ONE output's .meta matches and ANOTHER output's .meta mismatches
        for the same source, ``saw_match`` must short-circuit to ``True``
        (production policy: match wins). The inverse-only existing test
        ``test_mismatch_on_one_meta_invalidates_freshness`` uses an
        all-mismatches scenario and so does not exercise the asymmetric
        case. Without this test, mutating ``saw_match = True`` to
        ``saw_match = False`` survives because the only saw_match-True path
        is silently broken.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"bif")
        out_b.write_bytes(b"bif")
        # out_a: stamp matches the live source.
        write_meta([out_a], str(source))
        # out_b: hand-write a mismatching .meta with a clearly-wrong
        # fingerprint (same schema so the schema-guard at L139 is passed).
        _meta_path_for(out_b).write_text(
            json.dumps(
                {
                    "schema": JOURNAL_SCHEMA_VERSION,
                    "source_mtime": 1,
                    "source_size": 1,
                }
            )
        )

        # Match wins: True even though out_b records a stale fingerprint.
        assert outputs_fresh_for_source([out_a, out_b], str(source)) is True, (
            "Production policy: when one .meta matches and another mismatches for the SAME source, "
            "saw_match must short-circuit to True. A regression that flipped `saw_match = True` to `False` "
            "would let saw_mismatch dominate and return False here."
        )

    def test_outputs_with_old_schema_treated_as_legacy(self, tmp_path):
        """Mutation-testing closer (journal.py:38 — JOURNAL_SCHEMA_VERSION constant).

        The schema constant is read in two places — write_meta writes it,
        outputs_fresh_for_source compares it. Every existing test sets up
        data via write_meta, so writer and reader move in lockstep.
        Mutating the constant globally (e.g. ``1 → 2``) just shifts both
        ends symmetrically and the round-trip still works.

        Pin the *literal* schema number ``1`` in the .meta data so a
        regression that bumps the constant to 2 (without a real migration)
        falls into the schema-mismatch branch and is treated as legacy
        (returns True because the .meta is unreadable for freshness).
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        # Hand-write a .meta with literal schema=0 (old / unknown version).
        # Production: ``int(data.get("schema", 0)) != JOURNAL_SCHEMA_VERSION
        # → continue`` → no readable .meta → legacy fallback returns True.
        _meta_path_for(out).write_text(
            json.dumps(
                {
                    "schema": 0,  # literal — NOT JOURNAL_SCHEMA_VERSION
                    "source_mtime": int(source.stat().st_mtime),
                    "source_size": 100,
                }
            )
        )

        # The schema mismatch makes this .meta invisible to the freshness
        # check → falls through to the legacy branch → True.
        assert outputs_fresh_for_source([out], str(source)) is True, (
            "A .meta with schema != current must be ignored (treated as legacy). "
            "If the constant were silently bumped, write_meta would also bump and "
            "tests pass — pinning a literal schema=0 here catches the regression."
        )

    def test_meta_missing_size_field_treated_as_mismatch(self, tmp_path):
        """Mutation-testing closer (journal.py:141 — `data.get('source_size', -1)` default).

        A partially-valid JSON .meta missing the ``source_size`` key (e.g. a
        future schema upgrade or write-corruption that left a dangling
        record) must be treated as a fingerprint mismatch — i.e. the
        ``-1`` default value branch is the freshness-fail path. Production
        path:

            int(data.get("source_size", -1)) == src_size   # -1 != 100 → mismatch

        Without this test, mutations like ``-1 → 0`` survive because no
        existing test triggers a partial-key meta — every ``.meta`` written
        by ``write_meta`` always carries every key, so the default is dead
        code in the happy path.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        # Hand-write a partial .meta — has schema + source_mtime, MISSING
        # source_size. The dict.get() default fires → -1 → mismatch.
        _meta_path_for(out).write_text(
            json.dumps(
                {
                    "schema": JOURNAL_SCHEMA_VERSION,
                    "source_mtime": int(source.stat().st_mtime),
                    # source_size intentionally absent
                }
            )
        )

        # The single .meta records a mismatch → saw_mismatch=True →
        # outputs_fresh_for_source returns False (NOT the legacy True
        # fallback, because at least one .meta WAS readable but mismatched).
        assert outputs_fresh_for_source([out], str(source)) is False, (
            "A .meta missing source_size must be treated as a mismatch (defaults to -1, "
            "compared against real source_size, so any non--1 size triggers the mismatch branch). "
            "A regression that changed the default to 0 would falsely match a 0-byte source."
        )


class TestClearMeta:
    def test_removes_existing_metas(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"bif")
        write_meta([out], str(source))
        assert _meta_path_for(out).exists()

        clear_meta([out])
        assert not _meta_path_for(out).exists()
        # Output itself untouched.
        assert out.exists()

    def test_silent_on_missing_metas(self, tmp_path):
        out = tmp_path / "ghost.bif"
        # Never created.
        clear_meta([out])  # no exception


def _copy_of(original, path, *, mtime: int):
    """Write a byte-identical copy of ``original`` at ``path`` with its own mtime."""
    path.write_bytes(original.read_bytes())
    os.utime(path, (mtime, mtime))
    return path


class TestOutputSharedByCopies:
    """Plex names a bundle after a hash of the file's content, so copies of one
    video share an ``index-sd.bif``. Live case: Boxing S2026E94 and its "pt2"
    copy (same 12,170,979,064 bytes, same Plex part hash) rebuilt the shared
    BIF every night because each found the other's fingerprint on it."""

    def test_copies_sharing_an_output_both_stay_fresh_when_each_has_published(self, tmp_path):
        original = tmp_path / "Event - S2026E94.mkv"
        original.write_bytes(b"x" * 500)
        os.utime(original, (1_000_000, 1_000_000))
        copy = _copy_of(original, tmp_path / "Event - S2026E94 - pt2.mkv", mtime=2_000_000)
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")

        write_meta([bif], str(original), publisher="plex_bundle")
        write_meta([bif], str(copy), publisher="plex_bundle")

        assert outputs_fresh_for_source([bif], str(original)) is True
        assert outputs_fresh_for_source([bif], str(copy)) is True
        sources = json.loads(_meta_path_for(bif).read_text())["sources"]
        assert [(s["path"], s["mtime"], s["size"]) for s in sources] == [
            (str(original), 1_000_000, 500),
            (str(copy), 2_000_000, 500),
        ]

    def test_replacing_one_shared_source_does_not_borrow_another_copys_fingerprint(self, tmp_path):
        original = tmp_path / "a.mkv"
        original.write_bytes(b"old bytes")
        copy = _copy_of(original, tmp_path / "b.mkv", mtime=int(original.stat().st_mtime))
        os.utime(original, ns=(copy.stat().st_atime_ns, copy.stat().st_mtime_ns))
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"frames")
        write_meta([bif], str(original), publisher="plex_bundle")
        write_meta([bif], str(copy), publisher="plex_bundle")
        assert outputs_fresh_for_source([bif], str(original), require_source_fingerprint=True)
        assert outputs_fresh_for_source([bif], str(copy), require_source_fingerprint=True)

        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"new bytes")
        os.utime(replacement, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns))
        os.replace(replacement, original)

        assert not outputs_fresh_for_source([bif], str(original))
        assert not outputs_fresh_for_source([bif], str(original), require_source_fingerprint=True)
        assert outputs_fresh_for_source([bif], str(copy), require_source_fingerprint=True)

    def test_copy_not_yet_recorded_is_not_fresh_when_output_has_another_source(self, tmp_path):
        original = tmp_path / "a.mkv"
        original.write_bytes(b"x" * 500)
        os.utime(original, (1_000_000, 1_000_000))
        copy = _copy_of(original, tmp_path / "b.mkv", mtime=2_000_000)
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")

        write_meta([bif], str(original))

        assert outputs_fresh_for_source([bif], str(copy)) is False

    def test_source_of_a_different_size_is_dropped_when_output_rebuilt(self, tmp_path):
        """The output now holds frames from the new publisher only; an entry
        whose size differs can't be a copy of it, so it must not vouch for it."""
        first = tmp_path / "a.mkv"
        first.write_bytes(b"x" * 500)
        os.utime(first, (1_000_000, 1_000_000))
        other = tmp_path / "b.mkv"
        other.write_bytes(b"y" * 900)
        os.utime(other, (2_000_000, 2_000_000))
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")

        write_meta([bif], str(first))
        write_meta([bif], str(other))

        assert outputs_fresh_for_source([bif], str(first)) is False
        assert outputs_fresh_for_source([bif], str(other)) is True
        sources = json.loads(_meta_path_for(bif).read_text())["sources"]
        assert [s["path"] for s in sources] == [str(other)]

    def test_republish_replaces_its_own_entry_when_source_replaced(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 500)
        os.utime(source, (1_000_000, 1_000_000))
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")
        write_meta([bif], str(source))

        os.utime(source, (3_000_000, 3_000_000))  # same size, new file
        write_meta([bif], str(source))

        sources = json.loads(_meta_path_for(bif).read_text())["sources"]
        assert [(s["path"], s["mtime"]) for s in sources] == [(str(source), 3_000_000)]

    def test_meta_written_before_sources_existed_is_read_and_kept_when_copy_publishes(self, tmp_path):
        """Upgrading must not force a regeneration storm: a single-source
        ``.meta`` from the previous release still proves freshness, and a copy
        publishing next to it keeps that entry."""
        original = tmp_path / "a.mkv"
        original.write_bytes(b"x" * 500)
        os.utime(original, (1_000_000, 1_000_000))
        copy = _copy_of(original, tmp_path / "b.mkv", mtime=2_000_000)
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")
        _meta_path_for(bif).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "source_path": str(original),
                    "source_mtime": 1_000_000,
                    "source_size": 500,
                    "publisher": "plex_bundle",
                }
            )
        )
        assert outputs_fresh_for_source([bif], str(original)) is True

        write_meta([bif], str(copy), publisher="plex_bundle")

        assert outputs_fresh_for_source([bif], str(original)) is True
        assert outputs_fresh_for_source([bif], str(copy)) is True

    def test_keeps_both_copies_when_they_publish_at_the_same_moment(self, tmp_path):
        """Each writer must merge into what the other wrote, not into what it read before."""
        import threading
        from unittest.mock import patch

        from media_preview_generator.output import journal

        original = tmp_path / "a.mkv"
        original.write_bytes(b"x" * 500)
        os.utime(original, (1_000_000, 1_000_000))
        copy = _copy_of(original, tmp_path / "b.mkv", mtime=2_000_000)
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")

        both_read = threading.Barrier(2, timeout=0.5)
        real_read = journal._read_sources

        def read_then_wait(meta_path):
            sources = real_read(meta_path)
            try:
                both_read.wait()  # without the lock, both reads see no sources
            except threading.BrokenBarrierError:
                pass  # the lock keeps the other writer out; carry on alone
            return sources

        with patch.object(journal, "_read_sources", side_effect=read_then_wait):
            writers = [threading.Thread(target=write_meta, args=([bif], str(p))) for p in (original, copy)]
            for w in writers:
                w.start()
            for w in writers:
                w.join()

        sources = json.loads(_meta_path_for(bif).read_text())["sources"]
        assert sorted(s["path"] for s in sources) == [str(original), str(copy)]

    def test_does_not_raise_when_the_existing_meta_cannot_be_read(self, tmp_path):
        from unittest.mock import patch

        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x")
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")

        with patch(
            "media_preview_generator.output.journal._read_sources", side_effect=PermissionError("stale NFS handle")
        ):
            write_meta([bif], str(source))  # must not raise

        assert not _meta_path_for(bif).exists()

    def test_leaves_no_temp_files_behind_when_written(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x")
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")

        write_meta([bif], str(source))
        write_meta([bif], str(source))

        assert sorted(p.name for p in tmp_path.iterdir()) == ["index-sd.bif", "index-sd.bif.meta", "movie.mkv"]


class TestEmptyOutputIsNotFresh:
    """A 0-byte output holds no preview, so it is never fresh.

    A power loss shortly after a publish left 0-byte ``index-sd.bif`` files with 0-byte ``.meta`` next to them; an
    existence-only check called them fresh on every scan, so they were never rebuilt.
    """

    @pytest.mark.parametrize("meta", ["no_meta", "empty_meta", "matching_meta"])
    def test_not_fresh_when_output_is_zero_bytes(self, tmp_path, meta):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "index-sd.bif"
        out.write_bytes(b"real bif")
        if meta == "matching_meta":
            write_meta([out], str(source))
        elif meta == "empty_meta":
            _meta_path_for(out).write_bytes(b"")
        out.write_bytes(b"")

        assert outputs_fresh_for_source([out], str(source)) is False

    def test_not_fresh_when_one_of_several_outputs_is_zero_bytes(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        good_a = tmp_path / "a.bif"
        empty = tmp_path / "b.bif"
        good_c = tmp_path / "c.bif"
        for out in (good_a, empty, good_c):
            out.write_bytes(b"real bif")
        write_meta([good_a, empty, good_c], str(source))
        empty.write_bytes(b"")

        assert outputs_fresh_for_source([good_a, empty, good_c], str(source)) is False

    def test_fresh_when_output_has_data_and_no_meta(self, tmp_path):
        """Outputs from before the journal existed have no ``.meta``; they must not all regenerate."""
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "index-sd.bif"
        out.write_bytes(b"real bif")

        assert outputs_fresh_for_source([out], str(source)) is True

    def test_fresh_when_output_is_a_bif_with_no_thumbnails(self, tmp_path):
        """A 72-byte BIF (header and an empty index) is small but complete: only 0 bytes counts as empty."""
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "index-sd.bif"
        out.write_bytes(b"\x89BIF\r\n\x1a\n" + bytes(64))

        assert out.stat().st_size == 72
        assert outputs_fresh_for_source([out], str(source)) is True


class TestMetaForcedToDisk:
    """The ``.meta`` data is on disk before its name is: a power loss can't leave an empty sidecar in place."""

    def test_fsyncs_the_temp_file_before_renaming_it_into_place(self, tmp_path):
        from unittest.mock import patch

        from media_preview_generator.output import journal

        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            synced = os.fstat(fd)
            events.append(("fsync", synced.st_ino, synced.st_size))
            real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", os.stat(src).st_ino, str(dst)))
            real_replace(src, dst)

        with patch.object(journal.os, "fsync", side_effect=fsync), patch.object(journal.os, "replace", replace):
            write_meta([bif], str(source))

        meta = _meta_path_for(bif)
        written = meta.stat()
        assert events == [("fsync", written.st_ino, written.st_size), ("replace", written.st_ino, str(meta))]
        assert written.st_size > 0

    def test_meta_still_written_when_fsync_is_not_supported(self, tmp_path):
        from unittest.mock import patch

        from media_preview_generator.output import journal

        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        bif = tmp_path / "index-sd.bif"
        bif.write_bytes(b"bif")

        with patch.object(journal.os, "fsync", side_effect=OSError(22, "Invalid argument")) as fsync:
            write_meta([bif], str(source))

        fsync.assert_called_once()
        assert json.loads(_meta_path_for(bif).read_text())["source_size"] == 100
        assert outputs_fresh_for_source([bif], str(source)) is True
        assert sorted(p.name for p in tmp_path.iterdir()) == ["index-sd.bif", "index-sd.bif.meta", "movie.mkv"]


class TestPlexPendingNotifications:
    @pytest.fixture
    def publication(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"video")
        output = tmp_path / "index-sd.bif"
        output.write_bytes(b"frames")
        write_meta([output], str(source), publisher="plex_bundle")
        return source, output

    @pytest.mark.parametrize("journal", ["absent", "legacy", "unmarked", "corrupt"])
    def test_existing_output_without_marker_never_requests_analyze(self, publication, journal):
        from media_preview_generator.output.journal import get_plex_refresh_pending

        source, output = publication
        meta = _meta_path_for(output)
        if journal == "absent":
            meta.unlink()
        elif journal == "legacy":
            meta.write_text(json.dumps({"schema": 1, "source_size": 5, "source_mtime": int(source.stat().st_mtime)}))
        elif journal == "corrupt":
            meta.write_text("broken json")
        assert get_plex_refresh_pending([output], str(source), "plex-a") is None

    def test_acknowledgement_clears_only_matching_source_and_server(self, publication, tmp_path):
        from media_preview_generator.output.journal import (
            clear_plex_refresh_pending,
            get_plex_refresh_pending,
            mark_plex_refresh_pending,
        )

        source, output = publication
        first = mark_plex_refresh_pending([output], str(source), "plex-a")
        second = mark_plex_refresh_pending([output], str(source), "plex-b")
        copy = tmp_path / "copy.mkv"
        copy.write_bytes(source.read_bytes())
        write_meta([output], str(copy), publisher="plex_bundle")
        copied = mark_plex_refresh_pending([output], str(copy), "plex-a")
        assert first and second and copied
        assert len({first, second, copied}) == 3
        assert get_plex_refresh_pending([output], str(source), "plex-a") == first
        clear_plex_refresh_pending([output], str(copy), "plex-b", first)
        assert get_plex_refresh_pending([output], str(source), "plex-a") == first
        clear_plex_refresh_pending([output], str(source), "plex-a", first)
        assert get_plex_refresh_pending([output], str(source), "plex-a") is None
        assert get_plex_refresh_pending([output], str(source), "plex-b") == second
        assert get_plex_refresh_pending([output], str(copy), "plex-a") == copied
        assert outputs_fresh_for_source([output], str(source), require_source_fingerprint=True)
        assert outputs_fresh_for_source([output], str(copy), require_source_fingerprint=True)

    def test_late_ack_cannot_clear_regenerated_same_source(self, publication):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from media_preview_generator.output.journal import (
            clear_plex_refresh_pending,
            get_plex_refresh_pending,
            mark_plex_refresh_pending,
        )

        source, output = publication
        fingerprint = get_source_fingerprint(source)
        old = mark_plex_refresh_pending([output], str(source), "plex", source_fingerprint=fingerprint)
        waiting, analyzed = Event(), Event()

        def old_request():
            waiting.set()
            assert analyzed.wait(5)
            clear_plex_refresh_pending([output], str(source), "plex", old, source_fingerprint=fingerprint)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old_request)
            assert waiting.wait(5)
            write_meta([output], str(source), publisher="plex_bundle", source_fingerprint=fingerprint)
            new = mark_plex_refresh_pending([output], str(source), "plex", source_fingerprint=fingerprint)
            assert old and new and old != new
            analyzed.set()
            future.result(timeout=5)
        assert get_plex_refresh_pending([output], str(source), "plex") == new

    def test_replaced_source_does_not_reuse_marker_or_ack_wrong_fingerprint(self, publication):
        from media_preview_generator.output.journal import (
            clear_plex_refresh_pending,
            get_plex_refresh_pending,
            mark_plex_refresh_pending,
        )

        source, output = publication
        old_fingerprint = get_source_fingerprint(source)
        old = mark_plex_refresh_pending([output], str(source), "plex")
        source.write_bytes(b"replacement")
        assert get_plex_refresh_pending([output], str(source), "plex") is None
        assert mark_plex_refresh_pending([output], str(source), "plex") is None
        write_meta([output], str(source), publisher="plex_bundle")
        new = mark_plex_refresh_pending([output], str(source), "plex")
        assert new and new != old
        clear_plex_refresh_pending([output], str(source), "plex", new, source_fingerprint=old_fingerprint)
        assert get_plex_refresh_pending([output], str(source), "plex") == new

    def test_unrecorded_source_cannot_claim_shared_output(self, publication, tmp_path):
        from media_preview_generator.output.journal import get_plex_refresh_pending, mark_plex_refresh_pending

        source, output = publication
        other = tmp_path / "other.mkv"
        other.write_bytes(source.read_bytes())
        assert mark_plex_refresh_pending([output], str(other), "plex") is None
        assert get_plex_refresh_pending([output], str(other), "plex") is None

    def test_regeneration_retains_other_pending_markers_but_invalidates_freshness(self, publication):
        from media_preview_generator.output.journal import get_plex_refresh_pending, mark_plex_refresh_pending

        source, output = publication
        token = mark_plex_refresh_pending([output], str(source), "other-plex")
        clear_meta([output], preserve_plex_refresh_pending=True)
        assert not outputs_fresh_for_source([output], str(source))
        assert get_plex_refresh_pending([output], str(source), "other-plex") == token
        write_meta([output], str(source), publisher="plex_bundle")
        assert get_plex_refresh_pending([output], str(source), "other-plex") == token
        assert outputs_fresh_for_source([output], str(source))

    def test_failed_marker_update_keeps_previous_journal(self, publication, monkeypatch):
        from media_preview_generator.output import journal

        source, output = publication
        before = _meta_path_for(output).read_bytes()

        def fail_replace(*args):
            raise PermissionError("read-only journal")

        monkeypatch.setattr(journal.os, "replace", fail_replace)
        assert journal.mark_plex_refresh_pending([output], str(source), "plex") is None
        assert _meta_path_for(output).read_bytes() == before
        assert not _meta_path_for(output).with_suffix(".meta.tmp").exists()

    def test_concurrent_servers_retain_both_pending_markers(self, publication):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from media_preview_generator.output.journal import get_plex_refresh_pending, mark_plex_refresh_pending

        source, output = publication
        ready = Barrier(2)

        def mark(server_id):
            ready.wait(timeout=5)
            return mark_plex_refresh_pending([output], str(source), server_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(mark, "plex-a")
            b = pool.submit(mark, "plex-b")
            first, second = a.result(timeout=5), b.result(timeout=5)
        assert first and second and first != second
        assert get_plex_refresh_pending([output], str(source), "plex-a") == first
        assert get_plex_refresh_pending([output], str(source), "plex-b") == second
