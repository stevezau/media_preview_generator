# Ruling: Plex publishing is per ITEM, not per file (replaces WaitingForVersionsError / removed / remaining)

Why: Plex serves one marker set per metadata item across all versions, but `previous` and publish hashes were per
file. Three review rounds each found a new way our markers stay on Plex untracked (versions disagree, partial removal,
a version added after publish, an undecided sibling). The fix is to track and compute the item's set directly.

## Contract changes

1. `MarkerPublisher.write(item_id, markers, *, previous, duration_ms, canonical_path) -> list[Marker]`
   - Returns the markers that are ours on that server item after the call (the exact set written, sorted by start).
   - `previous` = what this app last left on THAT SERVER ITEM (from any file), or None when unknown (a failed write).
   - Jellyfin (per-version item ids): returns `project(markers)` on success. No other change.
   - `WaitingForVersionsError` is deleted, with `removed`/`remaining`. No publisher raises for version disagreement.
   - `duration_ms: int | None` everywhere.

2. Plex `write` computes the item's desired set itself:
   - For each supported type (intro, credits): collect the decided marker of that type for EVERY live part of the item
     (`sibling_markers(local_path)` for other parts, `markers` for the calling part; None = never decided).
   - The type is desired only when every part is decided AND every part has that type AND all agree within 2 s
     (spec §6.3). The written times are the calling file's marker (as today).
   - Otherwise the type is not desired (versions disagree, a part is undecided, or some part has no such marker).
   - Removal: rows/keys of a non-desired or changed type are removed only when they serve exactly the times in
     `previous` (served-time comparison, as fixed in round 2). `previous=None` → nothing is provably ours, so nothing
     is removed. Plex's own rows are never removed.
   - Optimized copies (proxy_type non-zero AND a "Plex Versions" path component) don't take part in agreement but get
     extra_data rewritten.
   - No-op detection, the lock-domain probe, the deadline lock, index renumbering and error mapping all stay.
   - Returns the desired set actually written (empty list when nothing of ours remains).

3. Store (markers.db): new table `item_publish_state(server_id, item_id, markers_json, status, updated_at,
   PRIMARY KEY(server_id, item_id))`, created with CREATE TABLE IF NOT EXISTS (no version bump, like `server_kinds`).
   - `get_item_publish_state(server_id, item_id)`, `set_item_publish_state(server_id, item_id, markers|None, status)`.
   - `published_to_item(server_id, item_id)` reads this table (non-empty markers).

4. Pipeline, per owner:
   - Publishers gain a class attribute `atomic_writes: bool`. Plex = True (one SQLite transaction: a PublishError means
     nothing changed). Jellyfin = False (the plugin may have stored data before an error).
   - `previous` = the item row's markers; `[]` when there is no item row. Exception: when the item row status is
     "failed" and `publisher.atomic_writes` is False → `previous=None` (unknown; Jellyfin then DELETEs when nothing is
     wanted, which is safe because everything in the plugin store is ours).
   - After `write` returns `ours`: set item row = `ours` (status written); set the file's publish_state as today with
     published = `ours`.
   - File outcome per type: decided but not in `ours` on a Plex owner → WAITING ("versions don't agree yet").
   - On PublishError: item row status "failed", markers kept unchanged (for an atomic publisher they are still exactly
     what is on the server; for Jellyfin the None rule above applies on the next run).
   - Hash skip: skip only when the file's decided set AND the item row are unchanged since this file last published
     (store the item row's `updated_at` or hash in the file's publish_state). Any other file's publish to the same
     item changes the item row, so the next run of every file re-evaluates.

## Tests that must exist (both tasks)
- A publishes intro+credits; B added and disagrees on credits → B's run removes A's credits (previous = item row),
  returns [intro]; A's next run is not "up to date" and returns [intro] as well.
- Partial: versions agree on intro, disagree on credits → returns [intro]; later intro goes to review and credits agree
  → returns [credits] and our intro row is gone.
- Undecided sibling → its types are not desired; our rows for them removed (previous known).
- previous=None → Plex removes nothing and writes desired (only reachable from a caller bug for Plex, since it is atomic).
- A transient Plex PublishError after an earlier publish, then the type is no longer wanted → the next run passes the
  kept item row as previous and our old rows are removed.
- Single-version item behaves exactly as before (hash skip, no-op).

## Follow-ups from the combined review (2026-09-14)
- A per-(server, item) process lock covers item-row read → write → item-row/basis update, so two versions can't race.
- When versions agree within 2 s, the Plex publisher keeps what is already on the item (no rewrite ping-pong); item rows
  compare by (type, start, end).
- The up-to-date skip is bypassed on a forced re-detect and while the file is waiting.
- `write(..., own_previous=)` carries the file's last published set when its Plex item id changed (merge/split), so the
  moved part's old `pv:` keys are removed when they serve exactly that.
