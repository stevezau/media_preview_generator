import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from media_preview_generator.markers import store as store_mod
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
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
    fixed = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
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


def test_open_upgrades_an_older_schema_version(tmp_path):
    path = str(tmp_path / "markers.db")
    raw = sqlite3.connect(path)
    for stmt in store_mod._SCHEMA:
        raw.execute(stmt)
    raw.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '0')")
    raw.commit()
    raw.close()
    s = MarkerStore(path)
    try:
        row = s._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row["value"] == str(store_mod.SCHEMA_VERSION)
    finally:
        s.close()


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
    fixed = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
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
