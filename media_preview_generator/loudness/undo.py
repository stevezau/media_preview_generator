"""Put back the audio tracks the Plex loudness job wrote, from its write log.

Each line of ``loudness-writes.jsonl`` holds a track's (``stream_id``) or an item's mark (``metadata_item_id``)
``extra_data`` before and after the write. A row that still holds exactly what the job wrote gets its old value back;
one Plex changed since (a new poster's blurhash, say) loses only the fields the job added, while they still hold the
job's values. A row whose added fields Plex changed is left alone.

Stop Plex first: it refuses while another process has the database open. Inside the app's container:

    python -m media_preview_generator.loudness.undo --db ".../com.plexapp.plugins.library.db" \
        --log /config/loudness-writes.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse

from ..markers.publishers.base import PublishError
from ..markers.publishers.plex_db import decode_extra_data, encode_extra_data, shm_lock_held_elsewhere


def restored(before: str | None, after: str, current: str | None) -> str | None | bool:
    """What a row's ``extra_data`` goes back to, or False when it's left alone.

    Args:
        before: The row before the job's write.
        after: What the job wrote.
        current: The row now.
    """
    if current == after:
        return before
    try:
        old = decode_extra_data(before)[0]
        # ``url`` restates the other fields (the encoder rebuilds it), so it's no field of its own here.
        added = {k: v for k, v in decode_extra_data(after)[0].items() if k != "url" and old.get(k) != v}
        now, url_form = decode_extra_data(current)
    except PublishError:
        return False
    if not added or any(now.get(k) != v for k, v in added.items()):
        return False
    return encode_extra_data({k: v for k, v in now.items() if k not in added}, url_form=url_form)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="Plex's com.plexapp.plugins.library.db")
    parser.add_argument("--log", required=True, help="loudness-writes.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="count what would be restored, change nothing")
    args = parser.parse_args(argv)

    if shm_lock_held_elsewhere(args.db):
        print("Plex has the database open; stop Plex first.", file=sys.stderr)
        return 2
    records, unreadable = [], 0
    with open(args.log, encoding="utf-8") as fh:
        for line in fh:
            try:
                records.append(json.loads(line))
            except ValueError:
                # A line cut short (the app stopped mid-write) or blank: the rest still undoes.
                unreadable += line.strip() != ""
    # mode=rw: a mistyped path fails instead of creating an empty database next to Plex's.
    uri = f"file:{urllib.parse.quote(args.db)}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    count = skipped = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for record in reversed(records):
            # Only extra_data is set, so Plex's title-search triggers on metadata_items don't fire.
            table, row_id = (
                ("metadata_items", record["metadata_item_id"])
                if "metadata_item_id" in record
                else ("media_streams", record["stream_id"])
            )
            row = conn.execute(f"SELECT extra_data FROM {table} WHERE id = ?", (row_id,)).fetchone()  # noqa: S608
            back = restored(record["before"], record["after"], row[0]) if row is not None else False
            if back is False:
                skipped += 1
                continue
            conn.execute(f"UPDATE {table} SET extra_data = ? WHERE id = ?", (back, row_id))  # noqa: S608 - fixed names
            count += 1
        conn.execute("ROLLBACK" if args.dry_run else "COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    verb = "Would restore" if args.dry_run else "Restored"
    print(f"{verb} {count} track(s) and item mark(s); left {skipped} that changed since or were already restored.")
    if unreadable:
        print(f"Skipped {unreadable} unreadable line(s) in {args.log}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
