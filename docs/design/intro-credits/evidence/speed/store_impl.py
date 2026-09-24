"""Apply the review's store.py changes: frame rates bound to the identity they were read with, and a changed rate
dropping the file's matched pairs."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "media_preview_generator/markers/store.py"
)
s = p.read_text()
reps = [
    (
        """    # A file's video frame rate as ffprobe read it (NULL: probed, no rate: no video stream, or none it reports). Season
    # audio matches 25 fps and film-rate releases of one show at one speed, and decide reads online times on the file's
    # clock by it (``markers.speed``). A separate table, so a markers.db from before it only gains it.
    \"\"\"CREATE TABLE IF NOT EXISTS frame_rates (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        frame_rate REAL)\"\"\",
""",
        """    # A file's video frame rate as ffprobe read it (NULL: probed, no rate: no video stream, or none it reports), with
    # the identity it was read from: a row whose identity isn't the file's counts as unread. Season audio matches 25 fps
    # and film-rate releases at one speed, and decide reads online times on the file's clock by it (``markers.speed``).
    # A separate table, so a markers.db from before it only gains it.
    \"\"\"CREATE TABLE IF NOT EXISTS frame_rates (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        frame_rate REAL)\"\"\",
""",
    ),
    (
        """            self._write_evidence(conn, file_id, Source.CHAPTERS, chapters, "", "", chapter_version, (), now)
            conn.execute("INSERT OR REPLACE INTO frame_rates (file_id, frame_rate) VALUES (?,?)", (file_id, frame_rate))
""",
        """            self._write_evidence(conn, file_id, Source.CHAPTERS, chapters, "", "", chapter_version, (), now)
            self._write_frame_rate(conn, file_id, (identity.size, identity.mtime_ns), frame_rate)
""",
    ),
    (
        """    def get_frame_rate(self, file_id: int) -> tuple[bool, float | None]:
        \"\"\"A file's video frame rate as ffprobe read it.

        Returns:
            ``(stored, frame_rate)``: whether the file was probed for it, and the rate (None: it has none).
        \"\"\"
        with self._lock:
            r = self._conn.execute("SELECT frame_rate FROM frame_rates WHERE file_id=?", (file_id,)).fetchone()
        return (False, None) if r is None else (True, r["frame_rate"])

    def set_frame_rate(self, file_id: int, frame_rate: float | None, *, identity: tuple[int, int]) -> bool:
        \"\"\"Store a file's video frame rate as ffprobe read it.

        Args:
            file_id: The file.
            frame_rate: The rate, None when it has none.
            identity: ``(size, mtime_ns)`` of the file as it was probed.

        Returns:
            False (nothing stored) when the file's row has another identity now: it was replaced while it was probed.
        \"\"\"
        with self._tx() as conn:
            row = conn.execute("SELECT size, mtime_ns FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None or (row["size"], row["mtime_ns"]) != tuple(identity):
                return False
            conn.execute("INSERT OR REPLACE INTO frame_rates (file_id, frame_rate) VALUES (?,?)", (file_id, frame_rate))
        return True
""",
        """    def get_frame_rate(self, file_id: int) -> tuple[bool, float | None]:
        \"\"\"A file's video frame rate as ffprobe read it from the file as its row is now.

        Returns:
            ``(stored, frame_rate)``: whether the file was probed for it with its current identity, and the rate (None:
            it has none).
        \"\"\"
        with self._lock:
            r = self._conn.execute(
                "SELECT r.frame_rate FROM frame_rates r JOIN files f ON f.id = r.file_id "
                "AND f.size = r.size AND f.mtime_ns = r.mtime_ns WHERE r.file_id=?",
                (file_id,),
            ).fetchone()
        return (False, None) if r is None else (True, r["frame_rate"])

    def set_frame_rate(self, file_id: int, frame_rate: float | None, *, identity: tuple[int, int]) -> bool:
        \"\"\"Store a file's video frame rate as ffprobe read it. A rate other than the stored one (a first one
        included) drops the file's cached season pairs: they may have been matched at another speed.

        Args:
            file_id: The file.
            frame_rate: The rate, None when it has none.
            identity: ``(size, mtime_ns)`` of the file as it was probed.

        Returns:
            False (nothing stored) when the file's row has another identity now: it was replaced while it was probed.
        \"\"\"
        with self._tx() as conn:
            row = conn.execute("SELECT size, mtime_ns FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None or (row["size"], row["mtime_ns"]) != tuple(identity):
                return False
            self._write_frame_rate(conn, file_id, tuple(identity), frame_rate)
        return True

    @staticmethod
    def _write_frame_rate(
        conn: sqlite3.Connection, file_id: int, identity: tuple[int, int], frame_rate: float | None
    ) -> None:
        old = conn.execute(
            "SELECT size, mtime_ns, frame_rate FROM frame_rates WHERE file_id=?", (file_id,)
        ).fetchone()
        if old is None or (old["size"], old["mtime_ns"], old["frame_rate"]) != (*identity, frame_rate):
            conn.execute("DELETE FROM season_pairs WHERE file_a=? OR file_b=?", (file_id, file_id))
        conn.execute(
            "INSERT OR REPLACE INTO frame_rates (file_id, size, mtime_ns, frame_rate) VALUES (?,?,?,?)",
            (file_id, *identity, frame_rate),
        )
""",
    ),
]
for old, new in reps:
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new, 1)
p.write_text(s)
print("ok")
