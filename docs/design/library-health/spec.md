# Library health page (issue #400) — design

Status: approved by the owner 2026-10-10 ("looks good, let's do it"; decisions delegated). Mockup:
https://claude.ai/artifact/VV2QEpvmiCuGnUuTZb7q9x (the shape below supersedes it where they differ).

## 0. Start here

A new Tools page, **Library health**, shows per server and per library how many files still need previews,
loudness, intro markers and credits markers, lists those files, and starts the existing job for them. Counting is
read-only. The one write action is asking Plex to re-read videos whose previews exist but that Plex isn't showing.

Measured on sflix (129,199 files, Plex, NFS media, jobs running) on 2026-10-10:

- Plex database: listing every part with its hash 0.3 s; loudness rows 1.5 s; marker tags 0.1 s; markers.db 0.2 s.
- Stat of every `index-sd.bif` in Plex's own config folder (local ext4): 9.6 s cold, 1.2 s warm.
- Whole Plex check: 12 s cold, 2 s warm.
- Any per-file read of the **media share** (source `stat`, folder listing): ~7 files/s under load, threads don't help
  (5 hours for 129k). So no count may read the media share for Plex, and Emby sidecar checks are the only media-share
  reads, single-threaded.
- Plex's bulk section listing with `includeMarkers=1` drops markers (2 returned vs 36/40 per-item); never use it.
- Jellyfin `/Items?Fields=Trickplay` reports previews in bulk (32/32 matched disk) at ~70 items/s server-side.
- Jellyfin markers are per item (`/MediaSegments/{id}`), 43/s single-threaded.
- Emby markers come in bulk via `/Items?Fields=Chapters` (`MarkerType` IntroStart / CreditsStart).
- 7,014 sflix parts have a BIF on disk but no `"mi:indexes":"sd"` in `media_parts.extra_data`; every one has a BIF
  newer than Plex's last update of the part. `PUT /library/metadata/{id}/analyze` set the flag within 3 s on the lab
  Plex (item 1368). Root cause (separate follow-up, not in this change): `PlexServer.refresh_preview_metadata` gives up
  when the item isn't indexed yet ("has not indexed the file yet", 368 log lines since 2026-09-22) and nothing
  retries.

Probe scripts used for these numbers: `docs/design/library-health/probes/` (read-only).

## 1. Decisions

1. **Per server, per library.** Previews and markers live per server, so totals are never merged across servers.
2. **Four features:** Previews, Loudness, Intro, Credits. Each cell has a state:
   - `counted`: `total`, `done`, `nothing_found` (markers only), `todo` (= total − done − nothing_found), and for Plex
     previews `not_showing` (BIF exists, Plex flag missing).
   - `off`: the feature is disabled for this server or library (settings), with a link to the Servers page.
   - `not_applicable`: intro for movie libraries; loudness on Emby/Jellyfin ("Plex only").
   - `unavailable`: can't be counted with this server's access, with a one-line reason (e.g. no Plex database access).
   - `error`: the source failed; the reason is shown and the previous result is kept.
3. **Definitions** (what "done" means), per server type:
   - Plex with database access (`LocalPlexDb` resolvable, not agent mode):
     - Previews done = `index-sd.bif` exists with size > 0 at `PlexBundleAdapter.bundle_bif_path(folder, hash)`;
       `not_showing` = done but no `mi:indexes` = `sd` in the part's extra_data. Parts without a hash are todo.
     - Loudness done = every audio stream of the part passes `loudness.plex_db.has_analysis`; a `PublishError`
       (incomplete native data) counts as todo. Pre-filter rows with SQL `extra_data LIKE '%ln:%'` so `has_analysis`
       only runs on candidates. Parts with no audio stream are excluded from the total.
     - Intro/Credits done = the metadata item has a `taggings` row joined to a `tags` row with `tag_type = 12` and
       `taggings.text` = `intro` / `credits`.
   - Plex without database access (agent mode or no config folder): Previews from the section listing
     (`/library/sections/{id}/all?type=1|4`, one request per library), done = Part has `indexes="sd"`; loudness and
     markers `unavailable` ("Needs access to Plex's database").
   - Jellyfin: items from the existing paged `/Items` enumeration with `Fields=Path,Trickplay`; Previews done =
     a `Trickplay` entry keyed by that version's media source id. Markers done = `/MediaSegments/{id}` has a segment of type `Intro` / `Outro` (credits),
     4 worker threads. Loudness `not_applicable`.
   - Emby: items with `Fields=Path,Chapters`; Intro done = has `IntroStart`; Credits done = has `CreditsStart`.
     Previews done = the sidecar `<basename>-<width>-<interval>.bif` (the server's configured Emby width/interval, as
     `EmbyBifAdapter` names it) exists next to the mapped local path. Read once per folder (`os.scandir`), one thread.
     Loudness `not_applicable`.
   - Every server: markers `nothing_found` = markers.db decision for that canonical path and type with status
     `no_evidence` (legacy `needs_review` reads as `no_evidence`), only when the server shows no marker of that type.
     markers.db is opened read-only with `sqlite3 ... ?mode=ro` (never `MarkerStore`, which opens read-write and
     creates the file). A missing markers.db means zero nothing-found.
4. **Scope follows settings**, the same rules scans use:
   - Previews: library `enabled` flag.
   - Loudness: `load_server_loudness(cfg)` + `library_chosen(settings, library_id=, kind=)`; disabled → `off`.
   - Markers: `load_server(raw, type)` + `library_allowed(...)`; disabled → `off`. Intro only for episode libraries.
   - Files under the server's `exclude_paths` (`config.paths.is_path_excluded`) are left out of every count.
   - Plex paths map to local canonical paths with the server's path mappings (`plex_path_to_local` /
     `path_to_canonical_local`), used for exclusion, markers.db lookup, and the file list / job paths.
5. **One check, run three ways:** "Check now" on the page, nightly at 03:00 server-local time (an internal
   APScheduler cron job, `coalesce`, `max_instances=1`), and nothing else. No setting in v1. Single-flight: a second
   start while one runs returns the running state. Servers are checked one after another; each server's results
   replace its previous results in one transaction when that server finishes, so finished servers show new numbers
   while slower ones still run, and a failed or cancelled server keeps its last good numbers.
6. **Results persist** in `CONFIG_DIR/library_health.db` (sqlite, WAL): per-cell counts plus the todo file list per
   (server, library, feature), for the page's file list and for starting jobs on exactly those files.
7. **Actions start existing jobs** via the existing endpoints, never a new job kind:
   - Previews todo → `POST /api/jobs/manual` with `file_paths` + `server_id` when todo ≤ 1,000 files; otherwise
     `POST /api/jobs` with `libraries: [{server_id, library_id}]`. (A library job re-checks every file, which on a
     network share is slow; a path job touches only the listed files.)
   - Loudness todo → `POST /api/loudness/jobs` with `file_paths` (≤ 1,000) or `libraries`.
   - Intro/Credits todo → `POST /api/markers/jobs` the same way (one job covers both types).
   - A confirm step in the page states what will start ("Start a loudness job for 436 files in Sports?").
8. **"Not showing in Plex" fix:** a page button opens a dialog listing counts per library, then runs a background
   re-read task in the health runner: `PUT /library/metadata/{id}/analyze` per item from the last check's list,
   one request at a time, ≥ 0.5 s apart, cancellable, progress on the page. Item ids are de-duplicated (one analyze per
   metadata item). When it finishes it re-runs that server's check so the numbers update. Only runs for Plex servers
   with database access (the list comes from the database).
9. **UX** as in the mockup, with these changes: no "Change" link for the nightly time; the fix dialog says the task
   runs in the background on this page (not "on the Dashboard"); the file list is a flat, path-sorted list with a
   filter box, 100 rows per page, each row linking to the Inspector (`/inspector?path=<canonical path>` if the Inspector
   supports deep links; otherwise plain text). Info (i) tooltips on every column header and non-obvious state.
   The page polls `GET /api/library-health` every 2 s while a check or fix runs, every 60 s otherwise.

## 2. Architecture

```
media_preview_generator/library_health/
├── __init__.py
├── models.py      # Feature, CellState, Cell, LibraryResult, ServerResult, TodoFile, CheckProgress
├── scope.py       # per server+library: which features apply / off / n/a, exclusion predicate
├── markers_db.py  # read-only nothing-found lookup from markers.db
├── plex.py        # Plex DB counter + Plex API (no-DB) counter
├── embyish.py     # Emby and Jellyfin counters
├── store.py       # library_health.db: replace_server(), load(), list_todo(), last_run meta
└── runner.py      # single-flight check + Plex re-read task, progress, cancel, nightly entry
web/routes/api_library_health.py
web/templates/library_health.html, static/js/library_health.js, static/css/pages/library_health.css
```

Data flow: `runner.start_check()` → thread → `ServerRegistry.from_settings(settings_manager.get("media_servers"))` →
per server: `scope` + counter (`plex`/`embyish`) → `ServerResult` (cells + todo files) → `store.replace_server()`.
Routes read `store.load()` + `runner.progress()`.

Counters take a `cancel_check: Callable[[], bool]` and a `progress: Callable[[int, int], None]`, and must not raise
for one bad library: a library-level exception becomes an `error` cell for that library's features, and the server's
other libraries still count.

Plex DB reads use `LocalPlexDb(...)._database(read_only=True, deadline=...)` (the same lock and schema plumbing as
loudness and markers) with a 60 s deadline, then release the connection before the BIF stats begin.

## 3. API

All `@api_token_required`, JSON, errors as `{"error": "..."}`.

- `GET /api/library-health` → `{"checked_at", "duration_s", "next_nightly_at", "running": progress|null (a check, or a re-read with `kind: "reread"`),
  "servers": [ServerResult without todo lists]}`.
- `POST /api/library-health/check` → starts (or returns the running) check. 202 with progress.
- `POST /api/library-health/cancel` → cancels the running check or fix.
- `GET /api/library-health/files?server_id=&library_id=&feature=&q=&offset=&limit=` → `{"total", "files":
  [{"path", "title"}]}` (limit ≤ 500).
- `POST /api/library-health/plex-reread` `{"server_id"}` → starts the re-read task for that server. 409 only when a check or
  re-read is already running; 400/404 for other refusals (nothing to re-read, not a Plex server, unknown server).

## 4. Error handling

- Settings or registry failure → whole check fails with a logged warning; previous results stay; page shows the error.
- Server unreachable / auth failure → that server's cells keep previous numbers, server header shows the error chip.
- Plex DB locked past deadline or schema mismatch → `error` cells with the message; nothing is written to Plex.
- Emby folder unreadable → that file counts as todo (same as a scan, which can't see the output either).
- Nightly run when nothing is configured → no-op.
- Process restart mid-check → the run is simply gone; the last stored results remain; the next nightly runs again.

## 5. Testing

- Unit: each counter against fixtures — a small sqlite file with the Plex tables/columns used (metadata_items,
  media_items, media_parts, media_streams, taggings, tags, library_sections), a temp Plex config folder with BIF files,
  fake Emby/Jellyfin servers (stub `_request`/`list_items`), a temp markers.db built with the real schema.
- Scope: off / n/a / exclusion / intro-on-movies / sports default for markers.
- Store: replace is atomic per server, failed server keeps old rows, todo paging and filter.
- Runner: single-flight, cancel, per-server isolation, re-read pacing + de-dup + re-check after.
- Routes: auth required, shapes, 409 on concurrent fix, limit clamp.
- Nav: Tools menu lists Library health; aria-current covers `main.library_health`. Update the e2e navbar spec.
- Lab proof: run against the lab Plex/Emby/Jellyfin, then sflix read-only numbers must match the probe figures above
  (allowing for library changes since 2026-10-10).

## 6. Docs

`docs/guides.md` Tools list + a short "Library health" section (what the numbers mean, nightly check, the Plex fix);
regenerate `docs/llms-full.txt`.
