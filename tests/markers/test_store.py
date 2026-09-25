import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from media_preview_generator.markers import store as store_mod
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.sources.server_markers import imported_detail
from media_preview_generator.markers.store import MarkerStore, get_marker_store, reset_marker_store

T = MarkerType


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _ident(path="/m/Show/Season 01/S01E01.mkv", size=100, mtime_ns=1):
    return FileIdentity(path, size, mtime_ns)


def _decided(mtype, start, end, by=("chapters",)):
    return TypeDecision(mtype, DecisionStatus.DECIDED, Marker(mtype, start, end, by), None, "chapters")


def _locked_decision(mtype=T.INTRO, start=1000, end=30_000):
    """What `decide()` returns for a type the user locked."""
    return TypeDecision(
        mtype, DecisionStatus.DECIDED, Marker(mtype, start, end, ("user",), locked=True), None, store_mod.LOCKED_BY_USER
    )


class _FailOnce:
    """Connection proxy that raises once on one SQL statement (sqlite3.Connection attributes are read-only)."""

    def __init__(self, conn, statement):
        self._conn = conn
        self._statement = statement
        self.fired = False

    def execute(self, sql, *args):
        if sql == self._statement and not self.fired:
            self.fired = True
            raise sqlite3.OperationalError(f"injected failure on {sql}")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _FailOnPrefix(_FailOnce):
    """Same, but matching the start of a statement (for the long parameterised INSERTs)."""

    def execute(self, sql, *args):
        if sql.startswith(self._statement) and not self.fired:
            self.fired = True
            raise sqlite3.OperationalError(f"injected failure on {sql}")
        return self._conn.execute(sql, *args)


def test_failed_begin_releases_the_lock_for_other_threads(store):
    store._conn = _FailOnce(store._conn, "BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        store.upsert_file(_ident("/m/a.mkv"), duration_ms=1, season_key=None, is_movie=False)
    done = threading.Event()

    def other_thread():
        store.upsert_file(_ident("/m/b.mkv"), duration_ms=1, season_key=None, is_movie=False)
        done.set()

    threading.Thread(target=other_thread, daemon=True).start()
    assert done.wait(timeout=5), "store lock was never released after BEGIN failed"
    assert store.get_file("/m/b.mkv") is not None


def test_failed_commit_is_rolled_back_and_the_next_write_works(store):
    store._conn = _FailOnce(store._conn, "COMMIT")
    with pytest.raises(sqlite3.OperationalError):
        store.upsert_file(_ident("/m/a.mkv"), duration_ms=1, season_key=None, is_movie=False)
    assert store.get_file("/m/a.mkv") is None
    store.upsert_file(_ident("/m/b.mkv"), duration_ms=1, season_key=None, is_movie=False)
    assert store.get_file("/m/b.mkv") is not None


def test_new_file_then_same_identity_is_not_changed(store):
    a = store.upsert_file(_ident(), duration_ms=1_320_000, season_key="/m/Show/Season 01", is_movie=False)
    store.replace_evidence(a.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)], version=2)
    b = store.upsert_file(_ident(), duration_ms=1_320_000, season_key="/m/Show/Season 01", is_movie=False)
    assert b.id == a.id
    assert len(store.get_evidence(a.id)) == 1 and store.evidence_version(a.id, Source.SKIPDB) == 2
    assert store.get_file(_ident().canonical_path).duration_ms == 1_320_000


def test_normal_open_uses_wal(store):
    with store._lock:
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("change", [{"size": 101}, {"mtime_ns": 2}])
def test_identity_change_invalidates_evidence_decisions_and_unlocked_markers(store, change):
    rec = store.upsert_file(_ident(), duration_ms=1_000_000, season_key=None, is_movie=True)
    store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)], version=1)
    store.save_decisions(rec.id, {T.CREDITS: _decided(T.CREDITS, 900_000, 1_000_000)}, settings_fingerprint="f")
    store.lock_marker(rec.id, Marker(T.INTRO, 1000, 30_000, ("user",), locked=True))
    published = [Marker(T.CREDITS, 900_000, 1_000_000, ("chapters",))]
    store.set_publish_state(rec.id, "plex-1", item_id="7", markers=published, status="written")
    store.set_publish_basis(rec.id, "plex-1", decided_hash="h", item_version=1)

    # A second, untouched file must survive this file's invalidation unscathed -- catches a WHERE
    # clause dropped from any of the DELETE/UPDATE statements below.
    other = store.upsert_file(_ident("/m/other.mkv"), duration_ms=1_000_000, season_key=None, is_movie=True)
    store.replace_evidence(other.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)], version=1)
    store.save_decisions(other.id, {T.CREDITS: _decided(T.CREDITS, 900_000, 1_000_000)}, settings_fingerprint="f")
    store.set_publish_state(other.id, "plex-1", item_id="9", markers=published, status="written")
    store.set_publish_basis(other.id, "plex-1", decided_hash="h", item_version=1)

    new = store.upsert_file(_ident(**change), duration_ms=1_100_000, season_key=None, is_movie=True)

    assert new.id == rec.id
    assert store.get_evidence(rec.id) == []
    assert store.evidence_version(rec.id, Source.SKIPDB) is None
    assert store.get_decisions(rec.id) == {}
    assert store.get_markers(rec.id) == {T.INTRO: Marker(T.INTRO, 1000, 30_000, ("user",), locked=True)}
    kept = store.get_publish_state(rec.id, "plex-1")  # markers/status kept, so the next publish knows what to
    # replace; the basis is cleared so an in-place rewrite landing on identical times still re-publishes.
    assert kept.markers == tuple(published) and kept.status == "written"
    assert store.get_publish_basis(rec.id, "plex-1") is None
    assert store.get_file_by_id(rec.id).duration_ms == 1_100_000

    assert len(store.get_evidence(other.id)) == 1
    assert store.evidence_version(other.id, Source.SKIPDB) == 1
    assert T.CREDITS in store.get_decisions(other.id)
    assert store.get_publish_basis(other.id, "plex-1") == ("h", 1)


def test_replace_evidence_is_scoped_to_source_and_origin(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    plex = Candidate(T.INTRO, 76_508, 112_748, Source.SERVER_MARKERS, origin="plex-1")
    emby = Candidate(T.INTRO, 11_000, 37_000, Source.SERVER_MARKERS, origin="emby-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [plex], origin="plex-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [emby], origin="emby-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="plex-1", detail="no markers")
    assert store.get_evidence(rec.id) == [emby]
    assert store.evidence_fetched_at(rec.id, Source.SERVER_MARKERS, "plex-1") is not None
    rows = store.evidence_rows(rec.id)
    assert {(r.origin, r.type, r.detail) for r in rows} == {("plex-1", None, "no markers"), ("emby-1", T.INTRO, "")}


def test_a_stale_server_marker_keeps_its_flag_and_says_why(store):
    # A server's marker made for an earlier file at this path: stored with its own detail, read back flagged, so
    # decide skips it. Fresh ones of the same answer are stored as before.
    rec = store.upsert_file(_ident(), duration_ms=2_498_304, season_key=None, is_movie=False)
    fresh = Candidate(T.CREDITS, 2_464_000, None, Source.SERVER_MARKERS, origin="plex-1")
    stale = Candidate(T.INTRO, 322_622, 354_901, Source.SERVER_MARKERS, origin="plex-1", stale=True)

    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [stale, fresh], origin="plex-1")

    assert sorted(store.get_evidence(rec.id), key=lambda c: c.start_ms) == [stale, fresh]
    details = {r.type: r.detail for r in store.evidence_rows(rec.id)}
    assert details == {T.INTRO: store_mod.STALE_SERVER_MARKERS_DETAIL, T.CREDITS: ""}


def test_never_looked_up_source_has_no_fetched_at(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
    assert store.evidence_fetched_at(rec.id, Source.THEINTRODB) is None


def test_open_ended_candidate_round_trips(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    c = Candidate(T.CREDITS, 1_298_000, None, Source.THEINTRODB, confidence=0.5)
    store.replace_evidence(rec.id, Source.THEINTRODB, [c])
    assert store.get_evidence(rec.id) == [c]


def test_save_decisions_matrix(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    store.save_decisions(
        rec.id,
        {T.INTRO: _decided(T.INTRO, 11_000, 37_000), T.CREDITS: _decided(T.CREDITS, 1_299_000, 1_320_000)},
        settings_fingerprint="f1",
    )
    proposed = Marker(T.CREDITS, 1_290_000, 1_320_000, ("skipdb",))
    store.save_decisions(
        rec.id,
        {
            T.INTRO: _decided(T.INTRO, 12_000, 38_000, ("theintrodb", "skipdb")),
            T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None, proposed, "sources don't agree yet"),
            T.RECAP: TypeDecision(T.RECAP, DecisionStatus.DISABLED, None, None, "detection off"),
        },
        settings_fingerprint="f2",
    )
    markers = store.get_markers(rec.id)
    assert markers == {T.INTRO: Marker(T.INTRO, 12_000, 38_000, ("theintrodb", "skipdb"))}
    d = store.get_decisions(rec.id)
    assert d[T.CREDITS].status is DecisionStatus.NEEDS_REVIEW
    assert (d[T.CREDITS].proposed_start_ms, d[T.CREDITS].proposed_end_ms) == (1_290_000, 1_320_000)
    assert d[T.RECAP].status is DecisionStatus.DISABLED and d[T.INTRO].settings_fingerprint == "f2"


def test_save_decisions_never_overwrites_locked_marker(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    locked = Marker(T.INTRO, 1000, 30_000, ("user",), locked=True)
    store.lock_marker(rec.id, locked)
    store.save_decisions(rec.id, {T.INTRO: _decided(T.INTRO, 11_000, 37_000)}, settings_fingerprint="f")
    assert store.get_markers(rec.id)[T.INTRO] == locked
    assert store.get_locked(rec.id) == {T.INTRO: locked}


class TestUserMarkers:
    """The Inspector editor's save and unlock (`save_user_markers` / `unlock_markers`)."""

    def test_save_locks_every_type_and_records_the_decision_in_one_transaction(self, store):
        rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
        store.save_decisions(
            rec.id,
            {
                T.INTRO: TypeDecision(
                    T.INTRO, DecisionStatus.NEEDS_REVIEW, None, Marker(T.INTRO, 5, 6, ("skipdb",)), "x"
                )
            },
            settings_fingerprint="old",
        )
        saved = store.save_user_markers(
            rec.id,
            [Marker(T.INTRO, 1_000, 30_000, ("user",)), Marker(T.CREDITS, 1_290_000, 1_320_000, ("user",))],
            settings_fingerprint="fp-now",
        )
        assert saved == {
            T.INTRO: Marker(T.INTRO, 1_000, 30_000, ("user",), locked=True),
            T.CREDITS: Marker(T.CREDITS, 1_290_000, 1_320_000, ("user",), locked=True),
        }
        assert store.get_locked(rec.id) == saved
        for mtype in (T.INTRO, T.CREDITS):
            row = store.get_decisions(rec.id)[mtype]
            assert (row.status, row.reason) is not None and row.status is DecisionStatus.DECIDED
            assert row.reason == store_mod.LOCKED_BY_USER
            assert (row.proposed_start_ms, row.proposed_end_ms, row.decided_by) == (None, None, ())
            assert row.settings_fingerprint == "fp-now"
        assert set(store.locked_at(rec.id)) == {T.INTRO, T.CREDITS}

    def test_the_saved_reason_is_the_one_a_later_run_decides_with(self, store):
        """A save and the next detection run must agree word for word, or the Inspector's text flips after a run."""
        from media_preview_generator.markers.decide import DecisionContext, decide

        locked = Marker(T.INTRO, 1_000, 30_000, ("user",), locked=True)
        ctx = DecisionContext(1_320_000, False, "high", frozenset({T.INTRO}), ("chapters",), None)
        assert decide([], ctx, {T.INTRO: locked})[T.INTRO].reason == store_mod.LOCKED_BY_USER

    def test_a_save_whose_decision_write_fails_leaves_no_marker_either(self, store):
        """P-R1: the save is all or nothing -- a half-saved edit (locked, but still "Needs review") is worse than
        a refused one."""
        rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
        store._conn = _FailOnPrefix(store._conn, "INSERT OR REPLACE INTO decisions")
        with pytest.raises(sqlite3.OperationalError):
            store.save_user_markers(rec.id, [Marker(T.INTRO, 1_000, 30_000, ("user",))], settings_fingerprint="fp")
        assert store.get_markers(rec.id) == {}
        assert store.get_decisions(rec.id) == {}

    def test_unlock_drops_the_lock_and_sends_the_type_back_to_needs_review(self, store):
        rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
        store.save_user_markers(rec.id, [Marker(T.INTRO, 1_000, 30_000, ("user",))], settings_fingerprint="fp")
        assert store.unlock_markers(rec.id, [T.INTRO]) == frozenset({T.INTRO})
        assert store.get_markers(rec.id) == {} and store.get_locked(rec.id) == {}
        row = store.get_decisions(rec.id)[T.INTRO]
        assert row.status is DecisionStatus.NEEDS_REVIEW
        assert row.reason == store_mod.UNLOCKED_PENDING
        # An empty fingerprint can never equal a real one, so the next run always re-decides and re-publishes.
        assert row.settings_fingerprint == ""

    def test_after_unlock_the_next_run_decides_and_republishes_the_type(self, store):
        """The empty fingerprint is the mechanism: it makes the next run re-save the type (and, with the marker gone,
        publish whatever it finds -- or take the user's marker off the servers when it finds nothing)."""
        from media_preview_generator.markers.pipeline import _decisions_changed

        rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
        store.save_user_markers(rec.id, [Marker(T.INTRO, 1_000, 30_000, ("user",))], settings_fingerprint="fp")
        assert not _decisions_changed(store, rec.id, {T.INTRO: _locked_decision()}, "fp")  # a run changes nothing
        store.unlock_markers(rec.id, [T.INTRO])
        same = {T.INTRO: TypeDecision(T.INTRO, DecisionStatus.NEEDS_REVIEW, None, None, store_mod.UNLOCKED_PENDING)}
        assert _decisions_changed(store, rec.id, same, "fp")

    def test_unlock_leaves_an_unlocked_marker_and_its_decision_alone(self, store):
        """A mutant dropping ``AND locked=1`` from the DELETE would wipe a detected marker."""
        rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
        store.save_decisions(rec.id, {T.CREDITS: _decided(T.CREDITS, 1_290_000, 1_320_000)}, settings_fingerprint="f")
        assert store.unlock_markers(rec.id, [T.CREDITS]) == frozenset()
        assert store.get_markers(rec.id)[T.CREDITS].start_ms == 1_290_000
        assert store.get_decisions(rec.id)[T.CREDITS].status is DecisionStatus.DECIDED

    def test_unlock_only_touches_the_types_it_was_given(self, store):
        rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
        store.save_user_markers(
            rec.id,
            [Marker(T.INTRO, 1_000, 30_000, ("user",)), Marker(T.CREDITS, 1_290_000, 1_320_000, ("user",))],
            settings_fingerprint="fp",
        )
        assert store.unlock_markers(rec.id, [T.INTRO]) == frozenset({T.INTRO})
        assert set(store.get_locked(rec.id)) == {T.CREDITS}


def test_save_decisions_stores_the_proposals_own_sources(store):
    """L100: the editor shows what a proposal was based on, and a proposal has no row in `markers`."""
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    proposed = Marker(T.CREDITS, 1_290_000, 1_320_000, ("chapters", "theintrodb"))
    store.save_decisions(
        rec.id,
        {
            T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None, proposed, "sources disagree"),
            T.INTRO: TypeDecision(T.INTRO, DecisionStatus.NO_EVIDENCE, None, None, "nothing found"),
        },
        settings_fingerprint="f",
    )
    rows = store.get_decisions(rec.id)
    assert rows[T.CREDITS].decided_by == ("chapters", "theintrodb")
    assert rows[T.INTRO].decided_by == ()


def test_markers_hash_is_order_independent_and_sensitive_to_times(store):
    a = Marker(T.INTRO, 1, 2, ("x",))
    b = Marker(T.CREDITS, 3, 4, ("y",))
    assert MarkerStore.markers_hash([a, b]) == MarkerStore.markers_hash([b, a])
    assert MarkerStore.markers_hash([a]) != MarkerStore.markers_hash([Marker(T.INTRO, 1, 3, ("x",))])
    assert MarkerStore.markers_hash([a]) == MarkerStore.markers_hash([Marker(T.INTRO, 1, 2, ("other",))])


def test_publish_state_upsert_and_list(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
    first = [Marker(T.INTRO, 1, 5000, ("chapters",))]
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=first, status="written")
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=None, status="failed", message="plugin missing")
    row = store.get_publish_state(rec.id, "jf-1")
    assert (row.status, row.message, row.markers) == ("failed", "plugin missing", tuple(first))
    second = [Marker(T.INTRO, 2, 6000, ("chapters",))]
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=second, status="written")
    row = store.get_publish_state(rec.id, "jf-1")
    assert (row.status, row.message, row.markers) == ("written", "", tuple(second))
    assert [r.server_id for r in store.publish_states(rec.id)] == ["jf-1"]
    assert store.get_publish_state(rec.id, "other") is None


def test_files_in_season(store):
    for i in range(3):
        store.upsert_file(
            _ident(f"/m/S/Season 01/E0{i}.mkv"), duration_ms=1, season_key="/m/S/Season 01", is_movie=False
        )
    store.upsert_file(_ident("/m/S/Season 02/E01.mkv"), duration_ms=1, season_key="/m/S/Season 02", is_movie=False)
    assert [f.canonical_path for f in store.files_in_season("/m/S/Season 01")] == [
        "/m/S/Season 01/E00.mkv",
        "/m/S/Season 01/E01.mkv",
        "/m/S/Season 01/E02.mkv",
    ]


def test_source_usage_round_trip(store):
    store.record_source_usage("theintrodb", day="2026-09-13", used=83, limit=500, remaining=417)
    assert store.source_usage("theintrodb", "2026-09-13") == {"used": 83, "limit": 500, "remaining": 417}
    assert store.source_usage("theintrodb", "2026-09-14") is None


def test_concurrent_writers(store):
    errors = []

    def work(n):
        try:
            for i in range(20):
                rec = store.upsert_file(
                    _ident(f"/m/{n}/{i}.mkv"), duration_ms=1_000_000, season_key=None, is_movie=True
                )
                store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)])
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store._count("files") == 160 and store._count("evidence") == 160


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "markers.db")
    s = MarkerStore(path)
    rec = s.upsert_file(_ident(), duration_ms=5, season_key=None, is_movie=True)
    s.close()
    s2 = MarkerStore(path)
    assert s2.get_file_by_id(rec.id).canonical_path == _ident().canonical_path
    s2.close()


def test_identity_change_leaves_other_files_alone(store):
    """HIGH 1: a mutant dropping a table from the invalidation loop, dropping its WHERE file_id=?,
    or scoping the unlocked-markers delete to all files rather than one, must fail this test."""
    rec = store.upsert_file(_ident("/m/a.mkv"), duration_ms=1_000_000, season_key=None, is_movie=True)
    other = store.upsert_file(_ident("/m/b.mkv"), duration_ms=1_000_000, season_key=None, is_movie=True)
    for r in (rec, other):
        store.replace_evidence(r.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)])
        store.save_decisions(r.id, {T.CREDITS: _decided(T.CREDITS, 900_000, 1_000_000)}, settings_fingerprint="f")
        store.lock_marker(r.id, Marker(T.INTRO, 1000, 30_000, ("user",), locked=True))
        # No public writer for fingerprints yet -- insert directly under the store's own lock.
        with store._lock:
            store._conn.execute("INSERT INTO fingerprints VALUES (?, 'intro', 0, 10, 1, x'00')", (r.id,))

    store.upsert_file(_ident("/m/a.mkv", size=101), duration_ms=1_000_000, season_key=None, is_movie=True)

    assert store._conn.execute("SELECT COUNT(*) FROM fingerprints WHERE file_id=?", (rec.id,)).fetchone()[0] == 0
    assert store.get_evidence(rec.id) == []
    assert store.get_decisions(rec.id) == {}
    assert store.get_markers(rec.id) == {T.INTRO: Marker(T.INTRO, 1000, 30_000, ("user",), locked=True)}

    assert len(store.get_evidence(other.id)) == 1
    assert T.CREDITS in store.get_decisions(other.id)
    assert T.CREDITS in store.get_markers(other.id)
    assert T.INTRO in store.get_markers(other.id)
    assert store._count("fingerprints") == 1

    # HIGH A: a mutant dropping WHERE id=? from upsert_file's changed-branch UPDATE would blast every
    # file's row to A's new size/mtime/duration.
    other_file = store.get_file_by_id(other.id)
    assert (other_file.size, other_file.mtime_ns, other_file.duration_ms) == (100, 1, 1_000_000)


def test_evidence_version_is_stored_per_lookup_and_goes_with_its_rows(store):
    # Chapter rules and online parsers change; the version a lookup's rows were made with says whether to derive
    # them again.
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    other = store.upsert_file(_ident("/m/other.mkv"), duration_ms=1_320_000, season_key=None, is_movie=False)
    chapter = Candidate(T.INTRO, 10_000, 30_000, Source.CHAPTERS, origin="Intro")
    plex = Candidate(T.INTRO, 1_000, 30_000, Source.SERVER_MARKERS, origin="plex-1")
    store.replace_evidence(rec.id, Source.CHAPTERS, [chapter], version=3)
    store.replace_evidence(other.id, Source.CHAPTERS, [chapter], version=3)
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [plex], origin="plex-1", version=1)
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1", version=2)
    store.replace_evidence(rec.id, Source.SKIPDB, [], detail="")
    assert store.evidence_version(rec.id, Source.CHAPTERS) == 3
    assert store.evidence_version(rec.id, Source.SERVER_MARKERS, "plex-1") == 1
    assert store.evidence_version(rec.id, Source.SERVER_MARKERS, "jf-1") == 2
    assert store.evidence_version(rec.id, Source.SERVER_MARKERS) is None
    assert store.evidence_version(rec.id, Source.SKIPDB) is None  # stored without a version

    store.replace_evidence(rec.id, Source.CHAPTERS, [chapter], version=4)
    assert store.evidence_version(rec.id, Source.CHAPTERS) == 4
    store.replace_evidence(rec.id, Source.CHAPTERS, [chapter])
    assert store.evidence_version(rec.id, Source.CHAPTERS) is None
    assert store.evidence_version(other.id, Source.CHAPTERS) == 3


@pytest.mark.parametrize(
    ("source", "plugins", "copied_from"),
    [
        (Source.SERVER_MARKERS_IMPORTED, "SkipDB", "skipdb"),
        (Source.SERVER_MARKERS_IMPORTED, "TheIntroDB", "introdb"),
        (Source.SERVER_MARKERS_IMPORTED, "Ani-Skip Segments", "aniskip"),
        (Source.SERVER_MARKERS_IMPORTED, "SkipDB, TheIntroDB", ""),
        # a server's own markers are never a copy, whatever their row says
        (Source.SERVER_MARKERS, "SkipDB", ""),
    ],
)
def test_an_imported_copy_reads_back_the_database_its_detail_names(store, source, plugins, copied_from):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    stored = Candidate(T.CREDITS, 1_241_000, None, Source.SERVER_MARKERS, origin="jf-1")
    store.replace_evidence(rec.id, source, [stored], origin="jf-1", detail=imported_detail(plugins), version=1)
    assert store.get_evidence(rec.id) == [
        Candidate(T.CREDITS, 1_241_000, None, source, origin="jf-1", copied_from=copied_from)
    ]


def test_an_answer_can_replace_another_sources_rows_under_the_same_origin(store):
    # A server read before an importer plugin was installed stored plain server markers; the next read of that server
    # stores them as imported copies instead, and nothing of the old answer may be left behind.
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    plex = Candidate(T.INTRO, 1_000, 30_000, Source.SERVER_MARKERS, origin="plex-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [plex], origin="plex-1", version=1)
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1", version=1)
    copy = Candidate(T.INTRO, 24_046, 114_105, Source.SERVER_MARKERS_IMPORTED, origin="jf-1")
    store.replace_evidence(
        rec.id,
        Source.SERVER_MARKERS_IMPORTED,
        [copy],
        origin="jf-1",
        detail="imported",
        version=2,
        also_replaces=(Source.SERVER_MARKERS,),
    )
    assert store.get_evidence(rec.id) == [plex, copy]
    assert [(r.source, r.origin, r.detail) for r in store.evidence_rows(rec.id)] == [
        (Source.SERVER_MARKERS, "plex-1", ""),
        (Source.SERVER_MARKERS_IMPORTED, "jf-1", "imported"),
    ]
    assert store.evidence_fetched_at(rec.id, Source.SERVER_MARKERS, "jf-1") is None
    assert store.evidence_version(rec.id, Source.SERVER_MARKERS, "jf-1") is None
    assert store.evidence_version(rec.id, Source.SERVER_MARKERS_IMPORTED, "jf-1") == 2
    assert store.evidence_version(rec.id, Source.SERVER_MARKERS, "plex-1") == 1


def test_evidence_scoping_by_source(store):
    """HIGH 2: a mutant dropping the source (or origin) filter from evidence_fetched_at's WHERE
    clause, or from replace_evidence's DELETE, must fail this test."""
    rec = store.upsert_file(_ident(), duration_ms=1_000_000, season_key=None, is_movie=True)
    store.replace_evidence(rec.id, Source.CHAPTERS, [], detail="no chapters")
    assert store.evidence_fetched_at(rec.id, Source.THEINTRODB) is None
    assert store.evidence_fetched_at(rec.id, Source.SERVER_MARKERS, "plex-1") is None
    store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)])
    assert {r.source for r in store.evidence_rows(rec.id)} == {Source.CHAPTERS, Source.SKIPDB}


def test_get_locked_excludes_decided(store):
    """HIGH 2: a mutant dropping the ``AND locked=1`` filter from _markers must fail this test."""
    rec = store.upsert_file(_ident(), duration_ms=1_000_000, season_key=None, is_movie=True)
    store.save_decisions(rec.id, {T.CREDITS: _decided(T.CREDITS, 900_000, 1_000_000)}, settings_fingerprint="f")
    assert store.get_locked(rec.id) == {}


def test_replace_evidence_chapters_share_one_key_regardless_of_titled_labels(store):
    """MED 1: replace_evidence's delete/insert must key off the `origin` argument (the lookup key),
    never off each candidate's own `c.origin` (its label) -- otherwise replacing CHAPTERS again with
    differently-titled candidates accumulates duplicates instead of replacing."""
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    first = Candidate(T.INTRO, 10_000, 30_000, Source.CHAPTERS, origin="Intro")
    store.replace_evidence(rec.id, Source.CHAPTERS, [first])
    assert store.get_evidence(rec.id) == [first]
    assert store.evidence_fetched_at(rec.id, Source.CHAPTERS) is not None

    second = Candidate(T.INTRO, 12_000, 32_000, Source.CHAPTERS, origin="Title Sequence")
    store.replace_evidence(rec.id, Source.CHAPTERS, [second])
    assert store.get_evidence(rec.id) == [second]  # only the second set remains -- labels round-trip
    assert store.evidence_fetched_at(rec.id, Source.CHAPTERS) is not None

    store.replace_evidence(rec.id, Source.CHAPTERS, [], detail="no chapters")
    assert store.get_evidence(rec.id) == []


def test_replace_evidence_server_markers_by_origin_replace_independently(store):
    """MED 1 / HIGH B: two different server origins under the same source must not clobber each
    other, and evidence_fetched_at must be scoped to its own origin (store.py ~386-393)."""
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    plex = Candidate(T.INTRO, 1_000, 2_000, Source.SERVER_MARKERS, origin="plex-1")
    jf = Candidate(T.INTRO, 3_000, 4_000, Source.SERVER_MARKERS, origin="jf-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [plex], origin="plex-1")
    assert store.evidence_fetched_at(rec.id, Source.SERVER_MARKERS, "plex-1") is not None
    assert store.evidence_fetched_at(rec.id, Source.SERVER_MARKERS, "jf-1") is None

    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [jf], origin="jf-1")
    assert store.evidence_fetched_at(rec.id, Source.SERVER_MARKERS, "jf-1") is not None
    assert {c.origin for c in store.get_evidence(rec.id)} == {"plex-1", "jf-1"}

    replacement = Candidate(T.INTRO, 9_000, 9_500, Source.SERVER_MARKERS, origin="plex-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [replacement], origin="plex-1")
    evidence = store.get_evidence(rec.id)
    assert len(evidence) == 2
    assert replacement in evidence and jf in evidence


@pytest.mark.parametrize("change", [{"size": 101}, {"mtime_ns": 2}])
def test_identity_change_with_unknown_duration_stores_null(store, change):
    """LOW: a changed identity with duration_ms=None must not COALESCE the stale old duration."""
    store.upsert_file(_ident(), duration_ms=1_000_000, season_key=None, is_movie=False)
    changed = store.upsert_file(_ident(**change), duration_ms=None, season_key=None, is_movie=False)
    assert changed.duration_ms is None


def test_unchanged_identity_with_no_duration_keeps_previous_value(store):
    """LOW: an unchanged identity must still COALESCE, unlike the changed-identity case above."""
    store.upsert_file(_ident(), duration_ms=1_000_000, season_key=None, is_movie=False)
    same = store.upsert_file(_ident(), duration_ms=None, season_key=None, is_movie=False)
    assert same.duration_ms == 1_000_000


def test_nested_tx_on_same_thread_raises_and_store_stays_usable(store):
    with store._tx():
        with pytest.raises(RuntimeError, match="nested MarkerStore transaction"):
            with store._tx():
                pass
    store.upsert_file(_ident("/m/after-nested.mkv"), duration_ms=1, season_key=None, is_movie=False)
    assert store.get_file("/m/after-nested.mkv") is not None


def test_failed_rollback_is_swallowed_and_recovered_by_the_next_tx(store):
    class Boom(Exception):
        pass

    store._conn = _FailOnce(store._conn, "ROLLBACK")
    with pytest.raises(Boom):
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO files (canonical_path, size, mtime_ns, updated_at) VALUES (?,?,?,?)",
                ("/m/rollback-fails.mkv", 1, 1, "now"),
            )
            raise Boom()
    # The real ROLLBACK failed once (swallowed): the underlying transaction is still technically open.
    # The next _tx() call's own in_transaction guard must notice and clear it before starting fresh.
    store.upsert_file(_ident("/m/after-failed-rollback.mkv"), duration_ms=1, season_key=None, is_movie=False)
    assert store.get_file("/m/after-failed-rollback.mkv") is not None
    assert store.get_file("/m/rollback-fails.mkv") is None


def test_tx_body_exception_rolls_back_earlier_statements_of_the_same_tx(store):
    with pytest.raises(sqlite3.IntegrityError):
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO files (canonical_path, size, mtime_ns, updated_at) VALUES (?,?,?,?)",
                ("/m/rollback-me.mkv", 1, 1, "now"),
            )
            # Same canonical_path again -- UNIQUE violation raised mid-transaction.
            conn.execute(
                "INSERT INTO files (canonical_path, size, mtime_ns, updated_at) VALUES (?,?,?,?)",
                ("/m/rollback-me.mkv", 1, 1, "now"),
            )
    assert store.get_file("/m/rollback-me.mkv") is None


def test_clock_is_used_for_every_written_timestamp(tmp_path):
    fixed = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    s = MarkerStore(str(tmp_path / "markers.db"), clock=lambda: fixed)
    try:
        rec = s.upsert_file(_ident(), duration_ms=1_000_000, season_key=None, is_movie=False)
        s.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)])
        s.save_decisions(rec.id, {T.CREDITS: _decided(T.CREDITS, 900_000, 1_000_000)}, settings_fingerprint="f")
        s.set_publish_state(
            rec.id,
            "plex-1",
            item_id="1",
            markers=[Marker(T.CREDITS, 900_000, 1_000_000, ("chapters",))],
            status="written",
        )
        s.record_source_usage("theintrodb", day="2026-09-13", used=1, limit=2, remaining=1)

        assert s.evidence_fetched_at(rec.id, Source.SKIPDB) == fixed
        assert s.get_decisions(rec.id)[T.CREDITS].decided_at == fixed.isoformat()
        pub = s.get_publish_state(rec.id, "plex-1")
        assert pub.updated_at == fixed.isoformat()
        usage_row = s._conn.execute(
            "SELECT updated_at FROM source_usage WHERE source=? AND day=?", ("theintrodb", "2026-09-13")
        ).fetchone()
        assert usage_row["updated_at"] == fixed.isoformat()
    finally:
        s.close()


def test_get_marker_store_is_a_singleton_ignoring_later_config_dir(tmp_path):
    reset_marker_store()
    try:
        a = get_marker_store(str(tmp_path / "first"))
        b = get_marker_store(str(tmp_path / "second"))
        assert a is b
        assert a.db_path == str(tmp_path / "first" / "markers.db")
    finally:
        reset_marker_store()


def test_get_marker_store_falls_back_to_config_dir_env(tmp_path, monkeypatch):
    reset_marker_store()
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    try:
        s = get_marker_store()
        assert s.db_path == str(tmp_path / "markers.db")
    finally:
        reset_marker_store()


def test_get_marker_store_created_once_under_concurrent_first_access(tmp_path):
    reset_marker_store()
    n = 32
    barrier = threading.Barrier(n)
    stores: list[MarkerStore] = []
    append_lock = threading.Lock()

    def worker():
        barrier.wait()
        s = get_marker_store(str(tmp_path))
        with append_lock:
            stores.append(s)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(stores) == n
        assert len({id(s) for s in stores}) == 1
    finally:
        reset_marker_store()


def test_publish_states_scoped_to_file(store):
    """LOW: a mutant dropping WHERE file_id=? from publish_states must fail this test."""
    a = store.upsert_file(_ident("/m/a.mkv"), duration_ms=1, season_key=None, is_movie=False)
    b = store.upsert_file(_ident("/m/b.mkv"), duration_ms=1, season_key=None, is_movie=False)
    store.set_publish_state(a.id, "plex-1", item_id="1", markers=[Marker(T.INTRO, 1, 2, ("x",))], status="written")
    store.set_publish_state(b.id, "plex-1", item_id="2", markers=[Marker(T.INTRO, 3, 4, ("y",))], status="written")
    assert [r.item_id for r in store.publish_states(a.id)] == ["1"]
    assert [r.item_id for r in store.publish_states(b.id)] == ["2"]


def test_get_file_by_id_scoped_to_id(store):
    """LOW: a mutant dropping WHERE id=? from get_file_by_id must fail this test."""
    a = store.upsert_file(_ident("/m/a.mkv"), duration_ms=1, season_key=None, is_movie=False)
    b = store.upsert_file(_ident("/m/b.mkv"), duration_ms=2, season_key="/m", is_movie=True)
    assert store.get_file_by_id(a.id).canonical_path == "/m/a.mkv"
    assert store.get_file_by_id(b.id).canonical_path == "/m/b.mkv"
    assert store.get_file_by_id(a.id).duration_ms == 1
    assert store.get_file_by_id(b.id).duration_ms == 2


def test_open_refuses_a_newer_schema_version_before_any_ddl(tmp_path, monkeypatch):
    """MED: the version check must run off sqlite_master before any DDL -- build a DB with only the
    `meta` table (never the full app schema) so a mutant that applies `_SCHEMA` before checking the
    version would leave behind tables this test can catch. Also: the connection must be closed on
    refusal (LOW), and the database must not have been switched to WAL before the check (LOW)."""
    path = str(tmp_path / "markers.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(store_mod.SCHEMA_VERSION + 1),))
    raw.commit()
    raw.close()

    captured: list[sqlite3.Connection] = []
    real_connect = store_mod.sqlite3.connect

    def spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        captured.append(conn)
        return conn

    monkeypatch.setattr(store_mod.sqlite3, "connect", spy_connect)

    with pytest.raises(RuntimeError, match="newer version"):
        MarkerStore(path)

    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")  # the connection was closed, not left open on the failure path

    raw = sqlite3.connect(path)
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    journal_mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
    raw.close()
    assert tables == {"meta"}
    assert journal_mode == "delete"  # never switched to WAL before the refusal
    assert not (tmp_path / "markers.db-wal").exists()


# The two tables schema 1 shipped, written out so the migration is tested against what an existing install really
# has -- not against today's `_SCHEMA` with its columns removed.
_V1_MARKERS = """CREATE TABLE markers (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    decided_by TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (file_id, type))"""
_V1_DECISIONS = """CREATE TABLE decisions (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    proposed_start_ms INTEGER,
    proposed_end_ms INTEGER,
    settings_fingerprint TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (file_id, type))"""


# `files` before schema 3 added `missing_since`.
_PRE_V3_FILES = """CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    duration_ms INTEGER,
    season_key TEXT,
    is_movie INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL)"""


def _schema_1_database(path, version="1"):
    """A markers.db as schema 1 left it: no `markers.locked_at`, no `decisions.decided_by`."""
    raw = sqlite3.connect(path)
    for stmt in store_mod._SCHEMA:
        if stmt.startswith("CREATE TABLE IF NOT EXISTS markers ") or stmt.startswith(
            "CREATE TABLE IF NOT EXISTS decisions "
        ):
            continue
        raw.execute(_PRE_V3_FILES if stmt.startswith("CREATE TABLE IF NOT EXISTS files ") else stmt)
    raw.execute(_V1_MARKERS)
    raw.execute(_V1_DECISIONS)
    raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (version,))
    raw.commit()
    raw.close()
    return raw


def test_open_upgrades_an_older_schema_version(tmp_path):
    path = str(tmp_path / "markers.db")
    _schema_1_database(path, version="0")  # every step of the chain runs, including the empty 0 -> 1
    s = MarkerStore(path)
    try:
        row = s._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row["value"] == str(store_mod.SCHEMA_VERSION)
    finally:
        s.close()


def test_a_schema_1_database_gains_the_lock_and_proposal_columns_and_keeps_its_rows(tmp_path):
    """The columns Task 3 adds must reach an existing install: `_SCHEMA` is CREATE TABLE IF NOT EXISTS, so without
    `_MIGRATIONS[1]` this database keeps the schema-1 tables and every read of the new columns raises."""
    path = str(tmp_path / "markers.db")
    _schema_1_database(path)
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO files (id, canonical_path, size, mtime_ns, duration_ms, season_key, is_movie, updated_at) "
        "VALUES (1, '/m/Show/S01E01.mkv', 100, 1, 600000, NULL, 0, '2026-09-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO markers (file_id, type, start_ms, end_ms, decided_by, locked, updated_at) "
        "VALUES (1, 'intro', 1000, 30000, '[\"user\"]', 1, '2026-09-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO decisions (file_id, type, status, reason, proposed_start_ms, proposed_end_ms, "
        "settings_fingerprint, decided_at) VALUES (1, 'credits', 'needs_review', 'sources disagree', 500000, 600000, "
        "'fp', '2026-09-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    s = MarkerStore(path)
    try:
        version = s._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        assert version == str(store_mod.SCHEMA_VERSION)
        assert s.get_locked(1) == {T.INTRO: Marker(T.INTRO, 1000, 30000, ("user",), locked=True)}
        assert s.locked_at(1) == {}  # a lock from before the column has no date, and that must not read as "today"
        decision = s.get_decisions(1)[T.CREDITS]
        assert (decision.proposed_start_ms, decision.proposed_end_ms) == (500000, 600000)
        assert decision.decided_by == ()  # the column's default, not a crash
        # The upgraded database still takes a new lock and a new proposal, with both new columns.
        s.save_user_markers(1, [Marker(T.CREDITS, 500_000, 600_000, ("user",))], settings_fingerprint="fp")
        assert set(s.locked_at(1)) == {T.CREDITS}
        s.save_decisions(
            1,
            {
                T.PREVIEW: TypeDecision(
                    T.PREVIEW, DecisionStatus.NEEDS_REVIEW, None, Marker(T.PREVIEW, 1, 2, ("skipdb",)), "r"
                )
            },
            settings_fingerprint="fp",
        )
        assert s.get_decisions(1)[T.PREVIEW].decided_by == ("skipdb",)
    finally:
        s.close()


def test_a_schema_2_database_gains_the_missing_mark_and_keeps_its_rows(tmp_path):
    """`files.missing_since` must reach an existing install: without `_MIGRATIONS[2]` every read of a file raises."""
    path = str(tmp_path / "markers.db")
    raw = sqlite3.connect(path)
    for stmt in store_mod._SCHEMA:
        raw.execute(_PRE_V3_FILES if stmt.startswith("CREATE TABLE IF NOT EXISTS files ") else stmt)
    raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '2')")
    raw.execute(
        "INSERT INTO files (id, canonical_path, size, mtime_ns, duration_ms, season_key, is_movie, updated_at) "
        "VALUES (1, '/m/Show/S01E01.mkv', 100, 1, 600000, '/m/Show', 0, '2026-09-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO decisions (file_id, type, status, reason, proposed_start_ms, proposed_end_ms, "
        "settings_fingerprint, decided_at) VALUES (1, 'credits', 'needs_review', 'sources disagree', 500000, 600000, "
        "'fp', '2026-09-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    s = MarkerStore(path)
    try:
        assert s._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "3"
        # The database as it was before, for a downgrade: copied once, next to it.
        backup = sqlite3.connect(path + store_mod.BACKUP_SUFFIX)
        try:
            assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
            assert backup.execute("SELECT canonical_path FROM files").fetchall() == [("/m/Show/S01E01.mkv",)]
            assert "missing_since" not in {r[1] for r in backup.execute("PRAGMA table_info(files)")}
        finally:
            backup.close()
        rec = s.get_file("/m/Show/S01E01.mkv")
        assert (rec.id, rec.size, rec.missing_since) == (1, 100, None)
        assert s.files_in_review() == ["/m/Show/S01E01.mkv"]
        assert s.mark_missing(rec) is True
        assert s.files_in_review() == [] and s.get_file(rec.canonical_path).missing_since is not None
    finally:
        s.close()


def test_the_pre_v3_copy_is_made_once_and_never_for_a_new_or_current_database(tmp_path):
    fresh = str(tmp_path / "fresh" / "markers.db")
    MarkerStore(fresh).close()
    MarkerStore(fresh).close()  # schema 3 already
    assert not os.path.exists(fresh + store_mod.BACKUP_SUFFIX)

    path = str(tmp_path / "old" / "markers.db")
    os.makedirs(os.path.dirname(path))
    _schema_1_database(path)
    MarkerStore(path).close()
    first = os.path.getmtime(path + store_mod.BACKUP_SUFFIX)
    MarkerStore(path).close()
    assert os.path.getmtime(path + store_mod.BACKUP_SUFFIX) == first
    assert not os.path.exists(path + store_mod.BACKUP_SUFFIX + ".partial")


def _losing_the_open_race(path, winner):
    """Patch ``_tx`` so ``winner()`` runs once in the window between reading the version and BEGIN IMMEDIATE."""
    entered: list[str] = []
    real_tx = MarkerStore._tx

    @contextmanager
    def other_opens_it_first(self):
        if not entered:
            entered.append("opened")
            winner()
        with real_tx(self) as conn:
            yield conn

    return patch.object(MarkerStore, "_tx", other_opens_it_first), entered


def test_a_second_process_opening_the_same_schema_1_store_migrates_nothing(tmp_path):
    """Two app processes on one CONFIG_DIR: the loser of the race must not re-run `_MIGRATIONS[1]`.

    Its bare `ALTER TABLE markers ADD COLUMN locked_at` would raise "duplicate column name" out of
    `__init__`, so which migrations to run is decided inside the transaction, not before it.
    """
    path = str(tmp_path / "markers.db")
    _schema_1_database(path)
    patched, entered = _losing_the_open_race(path, lambda: MarkerStore(path).close())

    with patched:
        loser = MarkerStore(path)
    try:
        assert entered == ["opened"], "the race window was never entered"
        assert loser._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == str(
            store_mod.SCHEMA_VERSION
        )
        columns = {r[1] for r in loser._conn.execute("PRAGMA table_info(markers)").fetchall()}
        assert "locked_at" in columns  # the winner's migration stands; the loser neither repeated nor undid it
    finally:
        loser.close()


class TestAnOlderBuildOpenedItMeanwhile:
    """A rollback to a build from before ``season_pair_runs`` (40311c3, 98bed80: schema 3 as well) opens markers.db
    and works, but never reads or clears the tables this build added. That build recreates ``season_pairs``, which
    this build drops on every open, so finding it means one ran since; what it can have left out of step is emptied."""

    @staticmethod
    def _touched_by_this_build(path):
        store = MarkerStore(path)
        a = store.upsert_file(FileIdentity("/m/a.mkv", 1, 1), duration_ms=1_300_000, season_key="/m", is_movie=False)
        b = store.upsert_file(FileIdentity("/m/b.mkv", 1, 1), duration_ms=1_300_000, season_key="/m", is_movie=False)
        intro = Marker(T.INTRO, 1_000, 30_000, ("chapters",))
        store.save_decisions(b.id, {T.INTRO: TypeDecision(T.INTRO, DecisionStatus.DECIDED, intro, None, "chapters")},
                             settings_fingerprint="f")  # fmt: skip
        b = store.upsert_file(FileIdentity("/m/b.mkv", 2, 2), duration_ms=1_300_000, season_key="/m", is_movie=False)
        for rec in (a, b):
            store.set_fingerprint(rec.id, points=b"\x01\x00\x00\x00", size=rec.size, mtime_ns=rec.mtime_ns,
                                  window="intro", start_s=0.0, length_s=455.0, algorithm=1)  # fmt: skip
        assert store.set_season_pair(a.id, b.id, 9, [(1.0, 2.0, 3.0, 4.0)], identity_a=(1, 1), identity_b=(2, 2))
        store.record_version_reruns([("/m/a.mkv", "decide_rules", 1)])
        credits = Marker(T.CREDITS, 1_200_000, 1_300_000, ("credits_text",))
        store.save_decisions(
            a.id,
            {
                T.INTRO: TypeDecision(T.INTRO, DecisionStatus.DECIDED, intro, None, "chapters"),
                T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.DECIDED, credits, None, "credits_text"),
            },
            settings_fingerprint="f",
        )
        store.lock_marker(a.id, credits)  # the user's own
        store.close()

    @staticmethod
    def _counts(path):
        with closing(sqlite3.connect(path)) as raw:
            tables = ("season_pair_runs", "replaced_decisions", "version_reruns", "files", "decisions", "fingerprints",
                      "markers", "evidence_versions")  # fmt: skip
            counts = {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
            counts["locked"] = raw.execute("SELECT COUNT(*) FROM markers WHERE locked=1").fetchone()[0]
            return counts

    def test_what_it_couldnt_keep_in_step_is_emptied_on_the_next_open(self, tmp_path):
        path = str(tmp_path / "markers.db")
        self._touched_by_this_build(path)
        with closing(sqlite3.connect(path)) as older_build:  # its _SCHEMA's CREATE TABLE IF NOT EXISTS
            older_build.execute(
                "CREATE TABLE season_pairs (file_a INTEGER NOT NULL, file_b INTEGER NOT NULL, "
                "matcher_version INTEGER NOT NULL, runs_json TEXT NOT NULL, PRIMARY KEY (file_a, file_b))"
            )
            older_build.commit()

        MarkerStore(path).close()

        assert self._counts(path) == {
            "season_pair_runs": 0,
            "replaced_decisions": 0,
            "version_reruns": 0,
            "files": 2,
            "decisions": 2,  # a's; b's went with its identity
            "fingerprints": 2,
            "markers": 2,
            "evidence_versions": 0,
            "locked": 1,
        }

    def test_without_one_in_between_everything_is_kept(self, tmp_path):
        path = str(tmp_path / "markers.db")
        self._touched_by_this_build(path)

        MarkerStore(path).close()

        counts = self._counts(path)
        assert (counts["season_pair_runs"], counts["replaced_decisions"], counts["version_reruns"]) == (1, 1, 1)


def test_losing_the_race_to_a_newer_build_is_still_refused(tmp_path):
    """The other cell of the same race: the winner was a NEWER build, not this one.

    The outer read saw a version this build supports, so only the read inside the transaction can catch
    it — without that second refusal this build would go on to operate a schema it doesn't know.
    """
    path = str(tmp_path / "markers.db")
    _schema_1_database(path)
    newer = str(store_mod.SCHEMA_VERSION + 1)

    def a_newer_build_wins():
        raw = sqlite3.connect(path)
        raw.execute("UPDATE meta SET value=? WHERE key='schema_version'", (newer,))
        raw.commit()
        raw.close()

    patched, entered = _losing_the_open_race(path, a_newer_build_wins)

    with patched, pytest.raises(RuntimeError, match=f"schema {newer}"):
        MarkerStore(path)

    assert entered == ["opened"], "the race window was never entered"
    raw = sqlite3.connect(path)
    try:
        # Refused, so nothing of this build's schema was written over the newer one.
        assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == newer
        columns = {r[1] for r in raw.execute("PRAGMA table_info(markers)").fetchall()}
        assert "locked_at" not in columns
    finally:
        raw.close()


def test_a_failed_migration_leaves_the_schema_version_where_it_was(tmp_path, monkeypatch):
    """The migration and the version write share one transaction: a half-migrated database must never record 2."""
    path = str(tmp_path / "markers.db")
    _schema_1_database(path)
    monkeypatch.setattr(
        store_mod,
        "_MIGRATIONS",
        {1: ("ALTER TABLE markers ADD COLUMN locked_at TEXT", "ALTER TABLE nope ADD COLUMN x TEXT")},
    )
    with pytest.raises(sqlite3.OperationalError):
        MarkerStore(path)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
        columns = {r[1] for r in raw.execute("PRAGMA table_info(markers)").fetchall()}
        assert "locked_at" not in columns  # the first ALTER rolled back with the second
    finally:
        raw.close()


def test_file_record_exposes_size_mtime_season_and_is_movie(store):
    rec = store.upsert_file(
        _ident(size=555, mtime_ns=777), duration_ms=1, season_key="/m/Show/Season 01", is_movie=True
    )
    assert (rec.size, rec.mtime_ns, rec.season_key, rec.is_movie) == (555, 777, "/m/Show/Season 01", True)


def test_publish_state_row_exposes_item_id_and_updated_at(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
    store.set_publish_state(
        rec.id, "plex-1", item_id="item-42", markers=[Marker(T.INTRO, 1, 2, ("x",))], status="written"
    )
    row = store.get_publish_state(rec.id, "plex-1")
    assert row.item_id == "item-42"
    assert row.updated_at != ""


def test_decision_row_exposes_reason_and_decided_at(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
    store.save_decisions(
        rec.id,
        {T.RECAP: TypeDecision(T.RECAP, DecisionStatus.NO_EVIDENCE, None, None, "no evidence")},
        settings_fingerprint="f",
    )
    d = store.get_decisions(rec.id)[T.RECAP]
    assert d.reason == "no evidence"
    assert d.decided_at != ""


def test_set_publish_state_leaves_the_retired_hash_columns_empty(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
    first = [Marker(T.INTRO, 1, 5000, ("chapters",))]
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=first, status="written")
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=None, status="failed")
    with store._lock:
        row = store._conn.execute("SELECT markers_hash, verified_at FROM publish_state").fetchone()
    assert (row["markers_hash"], row["verified_at"]) == (None, None)


def test_unchanged_upsert_on_one_file_does_not_touch_another_files_row(store):
    """HIGH A: a mutant dropping WHERE id=? from upsert_file's UNCHANGED-branch UPDATE would blast
    every file's duration_ms to the same COALESCE(?, duration_ms) value."""
    a = store.upsert_file(_ident("/m/a.mkv"), duration_ms=1_000_000, season_key=None, is_movie=False)
    b = store.upsert_file(_ident("/m/b.mkv"), duration_ms=2_000_000, season_key=None, is_movie=False)
    same = store.upsert_file(_ident("/m/a.mkv"), duration_ms=9_999_999, season_key=None, is_movie=False)
    assert same.id == a.id
    assert store.get_file_by_id(a.id).duration_ms == 9_999_999
    assert store.get_file_by_id(b.id).duration_ms == 2_000_000


def test_locked_marker_on_one_file_does_not_block_decisions_for_another(store):
    """HIGH A: a mutant dropping file_id from save_decisions' locked check (N40) would make A's lock
    look like it also covers B, silently dropping B's decided marker."""
    a = store.upsert_file(_ident("/m/a.mkv"), duration_ms=1_000_000, season_key=None, is_movie=False)
    b = store.upsert_file(_ident("/m/b.mkv"), duration_ms=1_000_000, season_key=None, is_movie=False)
    store.lock_marker(a.id, Marker(T.INTRO, 1000, 30_000, ("user",), locked=True))
    store.save_decisions(b.id, {T.INTRO: _decided(T.INTRO, 5_000, 25_000)}, settings_fingerprint="f")
    assert store.get_markers(b.id) == {T.INTRO: Marker(T.INTRO, 5_000, 25_000, ("chapters",))}


def test_needs_review_decision_on_one_file_does_not_delete_another_files_marker(store):
    """HIGH A: a mutant dropping file_id from save_decisions' unlocked-marker DELETE (N41) would
    delete B's marker when A's same-type decision goes to NEEDS_REVIEW."""
    a = store.upsert_file(_ident("/m/a.mkv"), duration_ms=1_000_000, season_key=None, is_movie=False)
    b = store.upsert_file(_ident("/m/b.mkv"), duration_ms=1_000_000, season_key=None, is_movie=False)
    store.save_decisions(b.id, {T.INTRO: _decided(T.INTRO, 5_000, 25_000)}, settings_fingerprint="f")
    store.save_decisions(
        a.id,
        {T.INTRO: TypeDecision(T.INTRO, DecisionStatus.NEEDS_REVIEW, None, None, "no agreement")},
        settings_fingerprint="f",
    )
    assert store.get_markers(b.id) == {T.INTRO: Marker(T.INTRO, 5_000, 25_000, ("chapters",))}


def test_migration_runs_and_bumps_version(tmp_path, monkeypatch):
    """MED: a real ALTER TABLE migration must actually run (not just an empty-migrations bump), and
    it must run BEFORE `_SCHEMA` -- a `_SCHEMA` index on the migration's new column would fail with
    "no such column" if a mutant applied `_SCHEMA` first, so its existence proves the ordering."""
    path = str(tmp_path / "markers.db")
    raw = sqlite3.connect(path)
    for stmt in store_mod._SCHEMA:
        raw.execute(stmt)
    raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '0')")
    raw.commit()
    raw.close()

    monkeypatch.setattr(store_mod, "_MIGRATIONS", {0: ("ALTER TABLE evidence ADD COLUMN probe TEXT",)})
    monkeypatch.setattr(
        store_mod, "_SCHEMA", (*store_mod._SCHEMA, "CREATE INDEX IF NOT EXISTS idx_probe ON evidence(probe)")
    )
    s = MarkerStore(path)
    try:
        version = s._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        assert version == str(store_mod.SCHEMA_VERSION)
        cols = {c[1] for c in s._conn.execute("PRAGMA table_info(evidence)").fetchall()}
        assert "probe" in cols
        index_exists = s._conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_probe'").fetchone()
        assert index_exists is not None
    finally:
        s.close()


def test_migration_failure_leaves_version_and_schema_unchanged(tmp_path, monkeypatch):
    """MED: a migration whose SECOND statement fails must roll back the first statement too, and
    leave schema_version unbumped; fixing the migration and reopening must then succeed cleanly."""
    path = str(tmp_path / "markers.db")
    raw = sqlite3.connect(path)
    for stmt in store_mod._SCHEMA:
        raw.execute(stmt)
    raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '0')")
    raw.commit()
    raw.close()

    monkeypatch.setattr(
        store_mod,
        "_MIGRATIONS",
        {0: ("ALTER TABLE evidence ADD COLUMN probe TEXT", "THIS IS NOT VALID SQL")},
    )
    with pytest.raises(sqlite3.OperationalError):
        MarkerStore(path)

    raw = sqlite3.connect(path)
    version = raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    cols = {c[1] for c in raw.execute("PRAGMA table_info(evidence)").fetchall()}
    raw.close()
    assert version == "0"
    assert "probe" not in cols

    monkeypatch.setattr(store_mod, "_MIGRATIONS", {0: ("ALTER TABLE evidence ADD COLUMN probe TEXT",)})
    s = MarkerStore(path)
    try:
        row = s._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row["value"] == str(store_mod.SCHEMA_VERSION)
        cols2 = {c[1] for c in s._conn.execute("PRAGMA table_info(evidence)").fetchall()}
        assert "probe" in cols2
    finally:
        s.close()


def test_publish_state_updated_at_uses_clock_when_markers_are_kept(tmp_path):
    fixed = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    s = MarkerStore(str(tmp_path / "markers.db"), clock=lambda: fixed)
    try:
        rec = s.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
        s.set_publish_state(rec.id, "plex-1", item_id="1", markers=None, status="failed")
        row = s.get_publish_state(rec.id, "plex-1")
        assert row.updated_at == fixed.isoformat()
    finally:
        s.close()


def test_get_evidence_falls_back_to_lookup_key_when_label_is_empty(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    c = Candidate(T.INTRO, 1_000, 2_000, Source.SERVER_MARKERS, origin="")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [c], origin="plex-1")
    assert store.get_evidence(rec.id)[0].origin == "plex-1"


def test_item_publish_state_matrix(store):
    # Plex serves one marker set per item across its versions, so what we last left there is kept per server item.
    intro = Marker(T.INTRO, 1, 5000, ("chapters",))
    credits = Marker(T.CREDITS, 9000, 12000, ("chapters",))
    assert store.get_item_publish_state("plex-1", "42") is None
    assert store.published_to_item("plex-1", "42") is False

    store.set_item_publish_state("plex-1", "42", None, "failed")  # the first write failed: nothing of ours known
    row = store.get_item_publish_state("plex-1", "42")
    assert (row.markers, row.status) == ((), "failed")
    assert store.published_to_item("plex-1", "42") is False

    store.set_item_publish_state("plex-1", "42", [credits, intro], "written")
    row = store.get_item_publish_state("plex-1", "42")
    assert (row.server_id, row.item_id, row.markers, row.status) == ("plex-1", "42", (intro, credits), "written")
    assert store.published_to_item("plex-1", "42") is True
    assert store.published_to_item("plex-2", "42") is False
    assert store.published_to_item("plex-1", "43") is False

    written_version = row.version
    store.set_item_publish_state("plex-1", "42", [intro, credits], "written")
    assert store.get_item_publish_state("plex-1", "42").version == written_version  # nothing changed

    store.set_item_publish_state("plex-1", "42", None, "failed")  # a later failure keeps what is still there
    row = store.get_item_publish_state("plex-1", "42")
    assert (row.markers, row.status) == ((intro, credits), "failed") and row.version > written_version
    assert store.published_to_item("plex-1", "42") is True

    failed_version = row.version
    store.set_item_publish_state("plex-1", "42", [], "written")
    row = store.get_item_publish_state("plex-1", "42")
    assert (row.markers, row.status) == ((), "written") and row.version > failed_version
    assert store.published_to_item("plex-1", "42") is False

    # Every type Plex keeps as its own (keepplex re-review LOW-5): still an item this app published to, so its markers
    # (possibly ours beside Plex's) are never read back as a second opinion.
    store.set_item_publish_state("plex-1", "42", [], "written", kept_types={T.INTRO})
    assert store.published_to_item("plex-1", "42") is True


def test_item_publish_state_version_follows_what_the_server_shows_not_who_decided(store):
    # Two versions that agree on the times but were decided by different sources (or one is locked) leave the item
    # showing the same markers; a version bump there would make each version's next run write the item again.
    intro = Marker(T.INTRO, 1000, 5000, ("chapters",))
    store.set_item_publish_state("plex-1", "42", [intro], "written")
    version = store.get_item_publish_state("plex-1", "42").version
    for same_times in (
        Marker(T.INTRO, 1000, 5000, ("theintrodb", "chapters")),
        Marker(T.INTRO, 1000, 5000, ("user",), locked=True),
    ):
        assert store.set_item_publish_state("plex-1", "42", [same_times], "written") == version
        assert store.get_item_publish_state("plex-1", "42").version == version
    for moved in (
        Marker(T.INTRO, 1001, 5000, ("chapters",)),
        Marker(T.INTRO, 1001, 5001, ("chapters",)),
        Marker(T.CREDITS, 1001, 5001, ("chapters",)),
    ):
        bumped = store.set_item_publish_state("plex-1", "42", [moved], "written")
        assert bumped == version + 1 and store.get_item_publish_state("plex-1", "42").markers == (moved,)
        version = bumped
    assert (
        store.set_item_publish_state("plex-1", "42", [Marker(T.CREDITS, 1001, 5001, ("x",))], "failed") == version + 1
    )


def test_item_publish_state_outlives_file_changes_and_file_rows(store):
    # The rows describe the server item, not a file version: a replaced or removed file doesn't take our markers off.
    rec = store.upsert_file(_ident("/m/Movie/A.mkv"), duration_ms=1, season_key=None, is_movie=True)
    intro = Marker(T.INTRO, 1, 5000, ("chapters",))
    store.set_item_publish_state("plex-1", "42", [intro], "written")
    before = store.get_item_publish_state("plex-1", "42")
    store.upsert_file(_ident("/m/Movie/A.mkv", size=999), duration_ms=None, season_key=None, is_movie=True)
    assert store.get_item_publish_state("plex-1", "42") == before
    with store._tx() as conn:
        conn.execute("DELETE FROM files WHERE id=?", (rec.id,))
    assert store.get_item_publish_state("plex-1", "42") == before
    assert store.published_to_item("plex-1", "42") is True


def test_publish_basis_is_per_file_and_server_and_goes_with_the_file_version(store):
    rec = store.upsert_file(_ident("/m/Movie/A.mkv"), duration_ms=1, season_key=None, is_movie=True)
    other = store.upsert_file(_ident("/m/Movie/B.mkv"), duration_ms=1, season_key=None, is_movie=True)
    assert store.get_publish_basis(rec.id, "plex-1") is None
    store.set_publish_basis(rec.id, "plex-1", decided_hash="abc", item_version=3)
    store.set_publish_basis(rec.id, "jellyfin-1", decided_hash="def", item_version=1)
    store.set_publish_basis(other.id, "plex-1", decided_hash="ghi", item_version=3)
    store.set_publish_basis(rec.id, "plex-1", decided_hash="abc2", item_version=4)
    assert store.get_publish_basis(rec.id, "plex-1") == ("abc2", 4)
    assert store.get_publish_basis(rec.id, "jellyfin-1") == ("def", 1)
    store.upsert_file(_ident("/m/Movie/A.mkv"), duration_ms=None, season_key=None, is_movie=True)
    assert store.get_publish_basis(rec.id, "plex-1") == ("abc2", 4)  # unchanged file
    store.upsert_file(_ident("/m/Movie/A.mkv", size=5), duration_ms=None, season_key=None, is_movie=True)
    assert store.get_publish_basis(rec.id, "plex-1") is None  # a replaced file is published again
    assert store.get_publish_basis(rec.id, "jellyfin-1") is None
    assert store.get_publish_basis(other.id, "plex-1") == ("ghi", 3)
    with store._tx() as conn:
        conn.execute("DELETE FROM files WHERE id=?", (other.id,))
    assert store.get_publish_basis(other.id, "plex-1") is None


def test_server_kind_is_kept_while_the_file_is_unchanged_and_cleared_when_it_changes(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=True)
    other = store.upsert_file(_ident("/m/Other.mkv"), duration_ms=1, season_key=None, is_movie=True)
    assert store.get_server_kind(rec.id) is None
    store.set_server_kind(rec.id, "episode")
    store.set_server_kind(other.id, "movie")
    store.set_server_kind(rec.id, "unknown")
    assert store.get_server_kind(rec.id) == "unknown"
    store.upsert_file(_ident(), duration_ms=None, season_key=None, is_movie=True)
    assert store.get_server_kind(rec.id) == "unknown"  # same size + mtime
    store.upsert_file(_ident(size=999), duration_ms=None, season_key=None, is_movie=True)
    assert store.get_server_kind(rec.id) is None  # a replaced file is confirmed again
    assert store.get_server_kind(other.id) == "movie"


def test_server_kind_table_is_created_on_an_existing_database(tmp_path):
    path = str(tmp_path / "markers.db")
    raw = sqlite3.connect(path)
    for stmt in store_mod._SCHEMA:
        if "server_kinds" not in stmt:
            raw.execute(stmt)
    raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(store_mod.SCHEMA_VERSION),))
    raw.commit()
    raw.close()
    s = MarkerStore(path)
    try:
        rec = s.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
        s.set_server_kind(rec.id, "movie")
        assert s.get_server_kind(rec.id) == "movie"
    finally:
        s.close()


def test_server_kind_goes_with_a_removed_file_row(store):
    # SQLite reuses the highest rowid after it is deleted: a new file must not inherit the removed file's kind.
    rec = store.upsert_file(_ident("/m/Gone.mkv"), duration_ms=1, season_key=None, is_movie=False)
    store.set_server_kind(rec.id, "episode")
    with store._tx() as conn:
        conn.execute("DELETE FROM files WHERE id=?", (rec.id,))
    assert store.get_server_kind(rec.id) is None
    new = store.upsert_file(_ident("/m/New.mkv"), duration_ms=1, season_key=None, is_movie=True)
    assert new.id == rec.id
    assert store.get_server_kind(new.id) is None


def test_item_publish_state_remembers_the_types_plex_keeps(tmp_path):
    # "Keep Plex's": once Plex's own detection replaced our marker of a type, that type stays Plex's on every path
    # until the server is switched back to restore or Plex's rows for it go away.
    path = str(tmp_path / "kept.db")
    store = MarkerStore(path)
    credits = Marker(T.CREDITS, 9000, 12000, ("chapters",))
    try:
        version = store.set_item_publish_state("plex-1", "42", [credits], "written", kept_types={T.INTRO})
        row = store.get_item_publish_state("plex-1", "42")
        assert (row.markers, row.kept_types) == ((credits,), frozenset({T.INTRO}))
        # Not given: what was kept stays (a failed write, or a caller that doesn't know).
        assert store.set_item_publish_state("plex-1", "42", [credits], "written") == version
        store.set_item_publish_state("plex-1", "42", None, "failed")
        assert store.get_item_publish_state("plex-1", "42").kept_types == frozenset({T.INTRO})
        failed = store.get_item_publish_state("plex-1", "42").version
        # A change in what is kept is a change on the item: siblings must look again.
        released = store.set_item_publish_state("plex-1", "42", [credits], "failed", kept_types=())
        assert released == failed + 1 and store.get_item_publish_state("plex-1", "42").kept_types == frozenset()
        store.set_item_publish_state("plex-1", "42", [credits], "written", kept_types={T.CREDITS, T.INTRO})
        assert store.get_item_publish_state("plex-1", "43") is None
        assert store.get_item_publish_state("plex-2", "42") is None
    finally:
        store.close()
    reopened = MarkerStore(path)
    try:
        assert reopened.get_item_publish_state("plex-1", "42").kept_types == frozenset({T.INTRO, T.CREDITS})
    finally:
        reopened.close()


def test_a_store_made_before_kept_types_opens_with_nothing_kept(tmp_path):
    path = str(tmp_path / "old.db")
    store = MarkerStore(path)
    store.set_item_publish_state("plex-1", "42", [Marker(T.INTRO, 1, 5000, ("chapters",))], "written")
    store._conn.execute("DROP TABLE item_kept_types")
    store.close()
    reopened = MarkerStore(path)
    try:
        row = reopened.get_item_publish_state("plex-1", "42")
        assert row.kept_types == frozenset() and len(row.markers) == 1
    finally:
        reopened.close()


class TestItemFiles:
    MARKER = Marker(MarkerType.INTRO, 1_000, 30_000, ("chapters",))

    def test_recorded_sorted_without_bumping_the_version_and_kept_after_a_failure(self, store):
        v1 = store.set_item_publish_state("plex-1", "7", [self.MARKER], "written", item_files=["/b.mkv", "/a.mkv"])
        row = store.get_item_publish_state("plex-1", "7")
        assert row.item_files == ("/a.mkv", "/b.mkv") and row.version == v1
        v2 = store.set_item_publish_state("plex-1", "7", [self.MARKER], "written", item_files=["/a.mkv"])
        assert v2 == v1 and store.get_item_publish_state("plex-1", "7").item_files == ("/a.mkv",)
        store.set_item_publish_state("plex-1", "7", None, "failed")
        assert store.get_item_publish_state("plex-1", "7").item_files == ("/a.mkv",)

    def test_rows_without_recorded_files_read_as_unknown(self, store):
        store.set_item_publish_state("jf-1", "abc", [self.MARKER], "written")
        assert store.get_item_publish_state("jf-1", "abc").item_files is None


class TestPublishedItems:
    MARKER = Marker(MarkerType.INTRO, 1_000, 30_000, ("chapters",))

    def test_written_items_with_markers_or_kept_types_and_their_files(self, store):
        m = self.MARKER
        a = store.upsert_file(FileIdentity("/m/b.mkv", 1, 1), duration_ms=1, season_key=None, is_movie=False)
        b = store.upsert_file(FileIdentity("/m/a.mkv", 1, 1), duration_ms=1, season_key=None, is_movie=False)
        for rec in (a, b):
            store.set_publish_state(rec.id, "plex-1", item_id="7", markers=[m], status="written")
        store.set_item_publish_state("plex-1", "7", [m], "written", item_files=["/m/b.mkv", "/m/a.mkv"])
        store.set_item_publish_state("plex-1", "8", [], "written", kept_types={MarkerType.CREDITS})
        store.set_item_publish_state("plex-1", "9", [], "written")
        store.set_item_publish_state("plex-1", "10", [m], "failed")
        store.set_item_publish_state("jf-1", "11", [m], "written")
        rows = store.published_items("plex-1")
        assert rows == [store.get_item_publish_state("plex-1", "7"), store.get_item_publish_state("plex-1", "8")]
        assert rows[0].item_files == ("/m/a.mkv", "/m/b.mkv") and rows[1].kept_types == frozenset({MarkerType.CREDITS})
        assert store.files_for_item("plex-1", "7") == ["/m/a.mkv", "/m/b.mkv"]
        assert store.files_for_item("plex-1", "8") == []
        assert store.files_for_item("jf-1", "7") == []

    def test_a_failed_write_after_a_success_is_not_listed(self, store):
        store.set_item_publish_state("plex-1", "7", [self.MARKER], "written")
        store.set_item_publish_state("plex-1", "7", None, "failed")
        assert store.published_items("plex-1") == []

    def test_files_for_an_item_are_looked_up_by_index(self, store):
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT f.canonical_path FROM publish_state p JOIN files f ON f.id = p.file_id "
            "WHERE p.server_id=? AND p.item_id=?",
            ("plex-1", "7"),
        ).fetchall()
        assert any("idx_publish_state_item" in str(tuple(row)) for row in plan)


class TestUndeliveredLocks:
    """Check servers must find a user's locked edit that a server never received (spec §6.2 step 6, ruling P-R1)."""

    LOCKED = Marker(MarkerType.INTRO, 1_000, 30_000, ("user",), locked=True)

    def _file(self, store, path="/m/a.mkv", *, locked=True):
        rec = store.upsert_file(FileIdentity(path, 1, 1), duration_ms=120_000, season_key=None, is_movie=False)
        if locked:
            store.save_user_markers(rec.id, [self.LOCKED], settings_fingerprint="fp")
        return rec

    @pytest.mark.parametrize(
        ("status", "listed"),
        [("failed", True), ("skipped", True), ("waiting", False), ("written", False)],
        ids=lambda v: str(v),
    )
    def test_a_locked_file_is_listed_for_the_servers_whose_last_publish_did_not_land(self, store, status, listed):
        rec = self._file(store)
        store.set_publish_state(rec.id, "jf-1", item_id="7", markers=None, status=status)
        assert store.files_with_undelivered_locks(["jf-1"]) == ([("/m/a.mkv", "jf-1")] if listed else [])

    def test_a_file_nobody_locked_is_never_listed(self, store):
        rec = self._file(store, locked=False)
        store.set_publish_state(rec.id, "jf-1", item_id="7", markers=None, status="failed")
        assert store.files_with_undelivered_locks(["jf-1"]) == []

    def test_unlocking_takes_the_file_off_the_list(self, store):
        rec = self._file(store)
        store.set_publish_state(rec.id, "jf-1", item_id="7", markers=None, status="failed")
        store.unlock_markers(rec.id, [MarkerType.INTRO])
        assert store.files_with_undelivered_locks(["jf-1"]) == []

    def test_only_the_servers_asked_for_count_and_each_pair_is_listed_once_in_path_order(self, store):
        b, a = self._file(store, "/m/b.mkv"), self._file(store, "/m/a.mkv")
        for rec in (a, b):
            store.set_publish_state(rec.id, "jf-1", item_id="7", markers=None, status="failed")
            store.set_publish_state(rec.id, "emby-1", item_id="7", markers=None, status="skipped")
            store.set_publish_state(rec.id, "plex-1", item_id="7", markers=None, status="failed")
        assert store.files_with_undelivered_locks(["jf-1", "emby-1"]) == [
            ("/m/a.mkv", "emby-1"),
            ("/m/a.mkv", "jf-1"),
            ("/m/b.mkv", "emby-1"),
            ("/m/b.mkv", "jf-1"),
        ]

    def test_no_servers_lists_nothing(self, store):
        rec = self._file(store)
        store.set_publish_state(rec.id, "jf-1", item_id="7", markers=None, status="failed")
        assert store.files_with_undelivered_locks([]) == []


class TestServerRechecks:
    """Decided files whose server answered "no markers", taken by Check servers on a backoff: 1, 2, 4, 8, 16 days."""

    NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    CREDITS = Marker(MarkerType.CREDITS, 900_000, 1_000_000, ("introdb", "skipdb"))
    AFTER = tuple(timedelta(days=d) for d in (1, 2, 4, 8, 16))

    @pytest.fixture
    def clock(self):
        return {"t": self.NOW - timedelta(days=2)}

    @pytest.fixture
    def cstore(self, tmp_path, clock):
        s = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        yield s
        s.close()

    def _file(self, store, path, *, decisions=None, answers=None):
        rec = store.upsert_file(FileIdentity(path, 1, 1), duration_ms=1_000_000, season_key=None, is_movie=True)
        credits = {MarkerType.CREDITS: _decided(MarkerType.CREDITS, 900_000, 1_000_000)}
        decisions = credits if decisions is None else decisions
        if decisions:
            store.save_decisions(rec.id, decisions, settings_fingerprint="f")
        for server_id, candidates in (answers if answers is not None else {"jf-1": []}).items():
            store.replace_evidence(rec.id, Source.SERVER_MARKERS, candidates, origin=server_id)
        return rec

    def _take(self, store, clock, servers=("jf-1",), limit=10):
        """What a Check servers run lists, marked taken as once each file ran: the files, sorted, each once."""
        due = store.server_rechecks_due(list(servers), now=clock["t"], after=self.AFTER, limit=limit)
        store.mark_server_rechecks_taken(due)
        return sorted({path for path, _server_id in due})

    def _rereads(self, store, rec, server_id="jf-1"):
        row = store._conn.execute(
            "SELECT rereads FROM server_marker_rereads WHERE file_id=? AND server_id=?", (rec.id, server_id)
        ).fetchone()
        return None if row is None else row["rereads"]

    def test_an_empty_answer_is_taken_again_after_1_2_4_8_and_16_days_then_never(self, cstore, clock):
        # Each time the job reads the server again and it still has nothing, the next check waits twice as long.
        start = self.NOW - timedelta(days=2)
        clock["t"] = start
        rec = self._file(cstore, "/m/a.mkv")
        taken_on = []
        for hour in range(0, 24 * 40, 6):
            clock["t"] = start + timedelta(hours=hour)
            if self._take(cstore, clock) == ["/m/a.mkv"]:
                taken_on.append(hour / 24)
                cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")  # read again: still empty
        # The first read is the stored answer (day 0); re-reads land 1, 2, 4, 8 and 16 days after the one before.
        assert [round(day - prev, 2) for prev, day in zip([0.0, *taken_on], taken_on, strict=False)] == [
            1.25, 2.25, 4.25, 8.25, 16.25,
        ]  # fmt: skip
        assert self._rereads(cstore, rec) == 5

    def test_listing_what_is_due_takes_nothing(self, cstore, clock):
        # A run cancelled before the file ran leaves it due at once, first in line.
        self._file(cstore, "/m/a.mkv")
        clock["t"] = self.NOW
        due = cstore.server_rechecks_due(["jf-1"], now=clock["t"], after=self.AFTER, limit=10)
        assert due == [("/m/a.mkv", "jf-1")]
        assert cstore.server_rechecks_due(["jf-1"], now=clock["t"], after=self.AFTER, limit=10) == due
        assert cstore._conn.execute("SELECT COUNT(*) FROM server_marker_rereads").fetchone()[0] == 0

    def test_marking_taken_keeps_nothing_for_an_answer_that_has_markers_by_then(self, cstore, clock):
        # The run's re-read found markers (the rows go with them): the mark after it mustn't bring a row back.
        rec = self._file(cstore, "/m/a.mkv")
        clock["t"] = self.NOW
        due = cstore.server_rechecks_due(["jf-1"], now=clock["t"], after=self.AFTER, limit=10)
        found = [Candidate(MarkerType.CREDITS, 950_000, None, Source.SERVER_MARKERS)]
        cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, found, origin="jf-1")
        cstore.mark_server_rechecks_taken([*due, ("/m/unknown.mkv", "jf-1")])
        assert cstore._conn.execute("SELECT COUNT(*) FROM server_marker_rereads").fetchone()[0] == 0

    def test_being_taken_doesnt_count_as_a_re_read(self, cstore, clock):
        # A file taken but whose server wasn't read (gone from disk, not in that server's library) is taken again a day
        # later on the same step.
        rec = self._file(cstore, "/m/a.mkv")
        clock["t"] = self.NOW
        assert self._take(cstore, clock) == ["/m/a.mkv"]
        assert self._take(cstore, clock) == []
        clock["t"] = self.NOW + timedelta(hours=23)
        assert self._take(cstore, clock) == []
        clock["t"] = self.NOW + timedelta(days=1, seconds=1)
        assert self._take(cstore, clock) == ["/m/a.mkv"]
        assert self._rereads(cstore, rec) == 0

    def test_the_first_read_isnt_a_re_read(self, cstore, clock):
        rec = self._file(cstore, "/m/a.mkv")
        assert self._rereads(cstore, rec) is None
        cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")
        assert self._rereads(cstore, rec) == 1

    @pytest.mark.parametrize("reset", ["server-answers-with-markers", "file-changed-on-disk"])
    def test_the_count_starts_again(self, cstore, clock, reset):
        rec = self._file(cstore, "/m/a.mkv")
        for _ in range(5):
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")
        clock["t"] = self.NOW + timedelta(days=40)
        assert self._take(cstore, clock) == []  # capped
        if reset == "server-answers-with-markers":
            found = [Candidate(MarkerType.CREDITS, 950_000, None, Source.SERVER_MARKERS)]
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, found, origin="jf-1")
            assert self._rereads(cstore, rec) is None
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")  # its detection went away
            assert self._rereads(cstore, rec) is None  # the answer before had markers: not a re-read of "nothing"
        else:
            cstore.upsert_file(FileIdentity("/m/a.mkv", 2, 2), duration_ms=1_000_000, season_key=None, is_movie=True)
            assert self._rereads(cstore, rec) is None
            cstore.save_decisions(rec.id, {MarkerType.CREDITS: _decided(MarkerType.CREDITS, 900_000, 1_000_000)},
                                  settings_fingerprint="f")  # fmt: skip
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")
        clock["t"] += timedelta(days=1, seconds=1)
        assert self._take(cstore, clock) == ["/m/a.mkv"]

    def test_a_re_read_that_fails_counts_and_moves_the_backoff_without_replacing_the_answer(self, cstore, clock):
        # An unusable answer (another cut on the item, a read that always fails) is taken on the same steps and cap.
        start = self.NOW - timedelta(days=2)
        clock["t"] = start
        rec = self._file(cstore, "/m/a.mkv", answers={})
        cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1", detail="unusable")
        stored = cstore.evidence_rows(rec.id)
        taken_on = []
        for hour in range(0, 24 * 60, 6):
            clock["t"] = start + timedelta(hours=hour)
            if self._take(cstore, clock) == ["/m/a.mkv"]:
                taken_on.append(hour / 24)
                cstore.count_failed_server_reread(rec.id, "jf-1")  # read again: failed again
                assert cstore.server_recheck_due(rec.id, "jf-1", now=clock["t"], after=self.AFTER) is False
        assert [round(day - prev, 2) for prev, day in zip([0.0, *taken_on], taken_on, strict=False)] == [
            1.25, 2.25, 4.25, 8.25, 16.25,
        ]  # fmt: skip
        assert self._rereads(cstore, rec) == 5
        assert cstore.evidence_rows(rec.id) == stored  # the answer itself is kept as it was

    def test_the_backoff_counts_from_a_failed_re_read_long_after_the_answer(self, cstore, clock):
        # The answer is 30 days old when Check servers first gets to it (no schedule until then) and the read fails.
        start = self.NOW - timedelta(days=2)
        rec = self._file(cstore, "/m/a.mkv", answers={})
        cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1", detail="unusable")
        clock["t"] = start + timedelta(days=30)
        cstore.count_failed_server_reread(rec.id, "jf-1")
        for at, due in ((timedelta(days=31), False), (timedelta(days=32, seconds=1), True)):
            clock["t"] = start + at
            assert cstore.server_recheck_due(rec.id, "jf-1", now=clock["t"], after=self.AFTER) is due
            assert (self._take(cstore, clock) == ["/m/a.mkv"]) is due

    @pytest.mark.parametrize(
        ("later", "rereads", "taken_after_a_day"),
        [("markers", None, True), ("empty", 3, False)],
        ids=["answer-with-markers-resets", "empty-answer-counts-on"],
    )
    def test_after_failed_re_reads_a_readable_answer(self, cstore, clock, later, rereads, taken_after_a_day):
        rec = self._file(cstore, "/m/a.mkv", answers={})
        cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1", detail="unusable")
        cstore.count_failed_server_reread(rec.id, "jf-1")
        cstore.count_failed_server_reread(rec.id, "jf-1")
        clock["t"] = self.NOW
        found = [Candidate(MarkerType.CREDITS, 950_000, None, Source.SERVER_MARKERS)] if later == "markers" else []
        cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, found, origin="jf-1")
        assert self._rereads(cstore, rec) == rereads
        if later == "markers":
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")  # its detection went away
            assert self._rereads(cstore, rec) is None  # the count starts again from the first step
        clock["t"] = self.NOW + timedelta(days=1, seconds=1)
        # A fresh count waits 1 day; 3 re-reads wait 8.
        assert (self._take(cstore, clock) == ["/m/a.mkv"]) is taken_after_a_day

    def test_due_for_the_pipeline_follows_the_same_steps(self, cstore, clock):
        rec = self._file(cstore, "/m/a.mkv")  # answered at NOW - 2 days
        due = lambda t: cstore.server_recheck_due(rec.id, "jf-1", now=t, after=self.AFTER)  # noqa: E731
        assert due(self.NOW) is True
        clock["t"] = self.NOW
        cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")  # re-read 1
        assert due(self.NOW + timedelta(days=2)) is False
        assert due(self.NOW + timedelta(days=2, seconds=1)) is True
        for _ in range(4):
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")
        assert due(self.NOW + timedelta(days=400)) is False
        assert cstore.server_recheck_due(rec.id, "emby-1", now=self.NOW, after=self.AFTER) is False  # never asked

    @pytest.mark.parametrize(
        ("change", "taken"),
        [
            ("none", True),
            ("answer-a-day-old-exactly", False),
            ("answer-12h-old", False),
            ("answer-has-a-marker", False),
            ("imported-answer-has-a-marker", False),
            ("unusable-answer", True),
            ("credits-needs-review", False),
            ("intro-decided-only", False),
            ("preview-decided", True),
            ("other-server", False),
            ("file-published-to-server", False),
            ("item-shows-ours-from-another-version", False),
            ("item-kept-plexs-own", False),
            ("file-published-elsewhere", True),
        ],
    )
    def test_which_files_are_taken(self, cstore, clock, change, taken):
        decisions, answers = None, None
        if change == "answer-has-a-marker":
            answers = {"jf-1": [Candidate(MarkerType.CREDITS, 950_000, None, Source.SERVER_MARKERS)]}
        elif change == "credits-needs-review":
            proposed = Marker(MarkerType.CREDITS, 900_000, 1_000_000, ("chapters",))
            review = TypeDecision(MarkerType.CREDITS, DecisionStatus.NEEDS_REVIEW, None, proposed, "x")
            decisions = {MarkerType.CREDITS: review}
        elif change == "intro-decided-only":
            decisions = {MarkerType.INTRO: _decided(MarkerType.INTRO, 10_000, 40_000)}
        elif change == "preview-decided":
            decisions = {MarkerType.PREVIEW: _decided(MarkerType.PREVIEW, 950_000, 1_000_000)}
        elif change == "other-server":
            answers = {"plex-1": []}
        elif change == "unusable-answer":
            answers = {}  # the server's first answer is the unusable one, stored below
        answered = {"answer-a-day-old-exactly": timedelta(days=1), "answer-12h-old": timedelta(hours=12)}
        clock["t"] = self.NOW - answered.get(change, timedelta(days=2))
        rec = self._file(cstore, "/m/a.mkv", decisions=decisions, answers=answers)
        if change == "unusable-answer":
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1", detail="Couldn't read")
        elif change == "imported-answer-has-a-marker":
            # Stored apart from the empty server answer: a marker under either server source makes the answer not empty.
            imported = [Candidate(MarkerType.CREDITS, 950_000, None, Source.SERVER_MARKERS_IMPORTED)]
            cstore.replace_evidence(rec.id, Source.SERVER_MARKERS_IMPORTED, imported, origin="jf-1")
        elif change == "file-published-to-server":
            cstore.set_publish_state(rec.id, "jf-1", item_id="x", markers=[self.CREDITS], status="written")
        elif change in ("item-shows-ours-from-another-version", "item-kept-plexs-own"):
            cstore.set_publish_state(rec.id, "jf-1", item_id="42", markers=[], status="written")
            mine = [self.CREDITS] if change == "item-shows-ours-from-another-version" else []
            kept = {MarkerType.CREDITS} if change == "item-kept-plexs-own" else None
            cstore.set_item_publish_state("jf-1", "42", mine, "written", kept_types=kept)
        elif change == "file-published-elsewhere":
            cstore.set_publish_state(rec.id, "plex-1", item_id="7", markers=[self.CREDITS], status="written")
        clock["t"] = self.NOW
        assert self._take(cstore, clock) == (["/m/a.mkv"] if taken else [])

    def test_each_limit_takes_the_least_recently_taken_then_the_oldest_answers(self, cstore, clock):
        self._file(cstore, "/m/new.mkv")
        clock["t"] -= timedelta(hours=1)
        self._file(cstore, "/m/old.mkv")
        clock["t"] = self.NOW
        assert self._take(cstore, clock, limit=1) == ["/m/old.mkv"]
        assert self._take(cstore, clock, limit=1) == ["/m/new.mkv"]
        assert self._take(cstore, clock, limit=1) == []
        # A day later both are due again (neither ran): the one taken first goes first.
        clock["t"] = self.NOW + timedelta(days=1, minutes=1)
        assert self._take(cstore, clock, limit=1) == ["/m/old.mkv"]
        self._file(cstore, "/m/never.mkv")  # answered just now: not due
        assert self._take(cstore, clock, limit=5) == ["/m/new.mkv"]

    def test_a_file_never_taken_goes_before_one_taken_before_whose_server_still_had_nothing(self, cstore, clock):
        # The one taken before may be gone from disk or from the server's library: it mustn't starve the others.
        clock["t"] = self.NOW - timedelta(days=3)
        self._file(cstore, "/m/unrefreshable.mkv")
        clock["t"] = self.NOW - timedelta(days=1, minutes=5)
        assert self._take(cstore, clock) == ["/m/unrefreshable.mkv"]
        clock["t"] = self.NOW - timedelta(days=2)
        self._file(cstore, "/m/waiting.mkv")
        clock["t"] = self.NOW
        assert self._take(cstore, clock, limit=1) == ["/m/waiting.mkv"]
        assert self._take(cstore, clock, limit=1) == ["/m/unrefreshable.mkv"]

    def test_a_file_asked_of_two_servers_is_listed_once_and_each_pair_counts(self, cstore, clock):
        self._file(cstore, "/m/a.mkv", answers={"jf-1": [], "emby-1": []})
        self._file(cstore, "/m/b.mkv", answers={"jf-1": []})
        clock["t"] = self.NOW
        assert self._take(cstore, clock, servers=("jf-1", "emby-1"), limit=2) == ["/m/a.mkv"]
        assert self._take(cstore, clock, servers=("jf-1", "emby-1"), limit=2) == ["/m/b.mkv"]

    def test_no_servers_takes_nothing(self, cstore, clock):
        self._file(cstore, "/m/a.mkv")
        clock["t"] = self.NOW
        assert self._take(cstore, clock, servers=()) == []

    def test_a_changed_file_forgets_when_it_was_taken(self, cstore, clock):
        self._file(cstore, "/m/a.mkv")
        clock["t"] = self.NOW
        assert self._take(cstore, clock) == ["/m/a.mkv"]
        assert cstore._conn.execute("SELECT COUNT(*) FROM server_marker_rereads").fetchone()[0] == 1
        cstore.upsert_file(FileIdentity("/m/a.mkv", 2, 2), duration_ms=1_000_000, season_key=None, is_movie=True)
        assert cstore._conn.execute("SELECT COUNT(*) FROM server_marker_rereads").fetchone()[0] == 0


class TestGoneItemsAndDriftTurns:
    MARKER = Marker(MarkerType.INTRO, 1_000, 30_000, ("chapters",))

    def test_a_gone_item_leaves_published_items_until_this_app_writes_it_again(self, store):
        v1 = store.set_item_publish_state("jf-1", "x", [self.MARKER], "written")
        store.mark_item_gone("jf-1", "x")
        row = store.get_item_publish_state("jf-1", "x")
        assert (row.status, row.markers, row.version > v1) == ("gone", (self.MARKER,), True)
        assert store.published_items("jf-1") == []
        store.mark_item_gone("jf-1", "missing")  # no row: nothing to do
        store.set_item_publish_state("jf-1", "x", [self.MARKER], "written")
        assert [r.item_id for r in store.published_items("jf-1")] == ["x"]

    @pytest.mark.parametrize("status", ["failed", "gone"])
    def test_only_a_written_item_is_marked_gone(self, store, status):
        store.set_item_publish_state("jf-1", "x", [self.MARKER], "written")
        if status == "failed":
            store.set_item_publish_state("jf-1", "x", None, "failed")
        else:
            store.mark_item_gone("jf-1", "x")
        before = store.get_item_publish_state("jf-1", "x")
        store.mark_item_gone("jf-1", "x")
        assert store.get_item_publish_state("jf-1", "x") == before

    def test_drift_listing_times_per_server(self, tmp_path):
        times = iter([datetime(2026, 9, 15, tzinfo=UTC), datetime(2026, 9, 16, tzinfo=UTC)])
        store = MarkerStore(str(tmp_path / "t.db"), clock=lambda: next(times))
        try:
            store.record_drift_listed([("jf-1", "a"), ("plex-1", "a")])
            store.record_drift_listed([("jf-1", "b")])
            assert store.drift_listed_at("jf-1", ["a", "b", "c"]) == {
                "a": "2026-09-15T00:00:00+00:00",
                "b": "2026-09-16T00:00:00+00:00",
            }
            assert store.drift_listed_at("plex-1", ["b"]) == {}
        finally:
            store.close()
