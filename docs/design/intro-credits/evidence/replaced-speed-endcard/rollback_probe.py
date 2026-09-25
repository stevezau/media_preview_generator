"""A rollback's effect on markers.db: this build writes the tables it added (``season_pair_runs``,
``replaced_decisions``, ``version_reruns``, a carried marker), an older build opens it and works (a file replaced, a
pair cached, the carried marker read), then this build opens it again (spec §14 2026-09-25, "Rollback").

Usage (each older tree is an export, e.g. ``git archive 40311c3`` / ``git archive 98bed80`` untarred):
    python rollback_probe.py <this tree> <older tree> [<older tree> ...]

Each phase runs in its own process with that tree first on ``PYTHONPATH``; the markers.db lives in a temp folder.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile


def _tables(path):
    with sqlite3.connect(path) as conn:
        names = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        counts = {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
    wanted = ("season_pairs", "season_pair_runs", "replaced_decisions", "version_reruns")
    return {n: counts[n] for n in wanted if n in counts}


def _phase(mode, db):
    from media_preview_generator.markers import store as store_mod
    from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
    from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
    from media_preview_generator.markers.store import MarkerStore

    print("build:", os.path.dirname(os.path.dirname(os.path.dirname(store_mod.__file__))))

    def upsert(store, name, size, mtime):
        return store.upsert_file(
            FileIdentity(name, size, mtime), duration_ms=1_300_000, season_key="/m", is_movie=False
        )

    def fingerprint(store, rec):
        store.set_fingerprint(rec.id, points=b"\x01\x00\x00\x00\x02\x00\x00\x00", size=rec.size,
                              mtime_ns=rec.mtime_ns, window="intro", start_s=0.0, length_s=455.0, algorithm=1)  # fmt: skip

    def decided(mtype, marker, reason):
        return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, reason)

    intro = Marker(MarkerType.INTRO, 1_000, 30_000, ("chapters",))
    store = MarkerStore(db)
    if mode == "this-writes":
        a, b = upsert(store, "/m/S01E01.mkv", 100, 1), upsert(store, "/m/S01E02.mkv", 100, 1)
        store.save_decisions(
            b.id, {MarkerType.INTRO: decided(MarkerType.INTRO, intro, "chapters")}, settings_fingerprint="f"
        )
        b = upsert(store, "/m/S01E02.mkv", 101, 1)  # replaced: its decisions are kept aside for the carry-over
        fingerprint(store, a), fingerprint(store, b)
        assert store.set_season_pair(a.id, b.id, 9, [(1.0, 20.0, 3.0, 22.0)], identity_a=(100, 1), identity_b=(101, 1))
        store.record_version_reruns([("/m/S01E01.mkv", "decide_rules", 1), ("/m/S01E02.mkv", "credits_text", 4)])
        carried = Marker(MarkerType.CREDITS, 1_200_000, 1_300_000, ("carried_over",))
        store.save_decisions(
            a.id,
            {
                MarkerType.INTRO: decided(MarkerType.INTRO, intro, "chapters"),
                MarkerType.CREDITS: decided(MarkerType.CREDITS, carried, "carried over"),
            },
            settings_fingerprint="f",
        )
    elif mode == "older-uses":
        from media_preview_generator.markers import job_log, source_counts

        print("opened:", _tables(db))
        markers = store.get_markers(store.get_file("/m/S01E01.mkv").id)
        print("carried marker read as:", markers[MarkerType.CREDITS].decided_by,
              source_counts.stored_groups(store, "/m/S01E01.mkv")[MarkerType.CREDITS],
              job_log._labels(("carried_over",)))  # fmt: skip
        a = upsert(store, "/m/S01E01.mkv", 200, 2)  # replaced while the older build runs
        b = store.get_file("/m/S01E02.mkv")
        fingerprint(store, a), fingerprint(store, b)
        cached = store.set_season_pair(a.id, b.id, 9, [(5.0, 6.0, 7.0, 8.0)], identity_a=(200, 2), identity_b=(101, 1))
        print("pair cached in its own table:", cached, "; in review:", store.files_in_review())
    else:
        a, b = store.get_file("/m/S01E01.mkv"), store.get_file("/m/S01E02.mkv")
        print("pair runs for the file the older build replaced:", store.get_season_pair(a.id, b.id, 9))
        with store._lock:
            reruns = [tuple(r) for r in store._conn.execute("SELECT file_id, detector, version FROM version_reruns")]
            kept = [tuple(r) for r in store._conn.execute("SELECT file_id, type FROM replaced_decisions")]
        print("version_reruns:", reruns, "; replaced_decisions:", kept)
    store.close()
    print("after:", _tables(db))


def main():
    if sys.argv[1] == "--phase":
        _phase(sys.argv[2], sys.argv[3])
        return
    this, older = sys.argv[1], sys.argv[2:]
    for tree in older:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "markers.db")
            print(f"===== older build: {tree}")
            for mode, root in (("this-writes", this), ("older-uses", tree), ("this-reopens", this)):
                env = {**os.environ, "PYTHONPATH": root}
                out = subprocess.run(
                    [sys.executable, __file__, "--phase", mode, db], env=env, capture_output=True, text=True
                )
                lines = [ln for ln in (out.stdout + out.stderr).splitlines() if "| DEBUG" not in ln]
                print(f"--- {mode} (exit {out.returncode})")
                print("\n".join(lines[-12:]))
    print(json.dumps({"older_builds": len(older)}))


if __name__ == "__main__":
    main()
