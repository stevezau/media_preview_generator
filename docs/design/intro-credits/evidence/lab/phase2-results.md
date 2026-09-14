# Phase 2 lab results

Lab servers on storage (`up.sh`), real library mounted `:ro`. Tokens in `env`, scrubbed from every result file.

## Task 4 — Emby plugin (2026-09-14, first build; superseded by fix round 1 below)

**Build.** `emby-plugin/`, `nice -n 19 docker run … mcr.microsoft.com/dotnet/sdk:9.0 dotnet build -c Release
-p:EmbyAbi=<abi> -o out/<abi>`: `Build succeeded.` with 0 warnings and 0 errors for `4.9` (`MediaBrowser.Server.Core
4.9.1.90`) and `4.10` (`4.10.0.24-beta2`). Each `out/<abi>/` holds only `MediaPreviewBridge.Emby.dll` (+ `.deps.json`,
`.pdb`); no `MediaBrowser.*.dll` copied.

**Servers.** `mlab-emby` = Emby 4.10.0.40 (the image `latest` pointed at; now pinned, same image id, not recreated), 4.10
build. `mlab-emby49` = Emby 4.9.1.90 (new, port 18099, wizard + Synth Chapters library via API, `EMBY49_TOKEN` /
`EMBY49_UID` in `env`), 4.9 build. Both logged `Loading MediaPreviewBridge.Emby, Version=1.0.0.0` and `Media Preview
Bridge: marker store /config/plugins/MediaPreviewBridge.Emby/markers`. The prototype `MarkersLabEmby.dll` was removed
from `mlab-emby` first (backup: `synth/_backup/MarkersLabEmby.dll`).

**Run.** `./emby_plugin_check.py mlab-emby49` then `./emby_plugin_check.py mlab-emby` (evidence:
`results/emby-plugin-<container>.json`). Item: Synth Chapters S01E01 (`mlab-emby` 53, `mlab-emby49` 11; 6,421,799
bytes; plain chapters `Chapter 1` 0, `Intro` 10 s, `Chapter 2` 40 s, `Credits` 100 s). Checks 9–11 use a copy of S01E01
in the container's own config volume (library "Plugin Check", `/config/plugcheck`), not in `synth/`: the Plex and
Jellyfin lab servers mount `synth/` and were running the phase 1 scale test. The script ends by deleting the S01E01
markers, the copy and its library, and the sessions it created (user `viewer` is kept for re-runs).

| # | Check | Emby 4.10.0.40 | Emby 4.9.1.90 |
|---|---|---|---|
| 1 | Ping without a token | PASS: 200 `{"Ok":true,"Version":"1.0.0.0","Features":["markers"]}` | PASS: same |
| 2 | `GET Markers/999999999`, admin key | PASS: 200 `Found` false, `Error` "item not found" | PASS: same |
| 3 | No token / user `viewer` (not admin), GET + POST + DELETE | PASS: no token 401 ×3, viewer **403** ×3 | PASS: same (401 / 403) |
| 4 | POST intro 10–40 s, credits 100 s, `FileSize` 6421799 | PASS: `Stored` 3; `IntroStart` 100000000, `IntroEnd` 400000000, `CreditsStart` 1000000000 + all 4 plain chapters with names | PASS: same |
| 5 | POST intro 12–42 s, no credits | PASS: `Stored` 2; new intro, no `CreditsStart`, plain chapters intact | PASS: same |
| 6 | Invalid: start without end / end ≤ start / credits −1 / `FileSize` 0 / all null | PASS: each 200, `Found` true, `Error` "intro start and end must be sent together" / "invalid intro ticks" / "invalid credits ticks" / "invalid fileSize" / "no markers; use DELETE to clear"; chapters and stored set unchanged | PASS: same |
| 7 | Refresh `FullRefresh` + `ReplaceAllMetadata=true` | PASS: rows back when the refresh completed (0.1 s later); log "markers written back for item 53 (None, MetadataImport, MetadataDownload)" | PASS: same, item 11 |
| 8 | Refresh `Default`, `ValidationOnly`, library scan, `docker restart` | PASS: markers kept after each; plugin wrote nothing back (no wipe) | PASS: same |
| 9 | POST with `FileSize` 6421800 (copy), then FullRefresh | PASS: 200 `Stale` true, `Stored` 0, no marker rows; still none after the refresh, no write-back log line | PASS: same |
| 10 | DELETE (copy, after a valid POST put 3 rows and `60.json`) | PASS: 200 `Stored` 0, no marker rows, plain chapters intact, `60.json` gone from the store dir | PASS: same (`21.json`) |
| 11 | Remove the copy's file, library scan | PASS: store file deleted; log "forgot markers of removed item 60"; GET answers `Found` false | PASS: same, item 21 |
| 12 | Emby web player (Playwright `emby_client.py 53`) after check 4 | PASS: `skip found: True \| t: 22.9 \| visible buttons: Skip Intro` (web device removed afterwards) | n/a (4.10 only) |

Findings for the app side (Task 10):
- Emby's serializer leaves null properties out: check 10's DELETE answered
  `{"Id":"60","Found":true,"Stale":false,"Stored":0}` (no `Error`, no ticks, no `FileSize`). Read a missing field as null.
- A non-admin user gets 403 (not 401) on both versions.
- The first 4.9 run passed all checks, but its cleanup could not remove the "Plugin Check" library: Emby 4.9's
  `DELETE /Library/VirtualFolders?name=…` answers 500 (NullReferenceException in `RemoveVirtualFolder(Int64 id)`); it
  needs `Id=<library ItemId>`. The script was fixed and both tables above are from the final script (4.9 re-run in full).
- Extra probe, both versions (not in the table): with markers stored for the copy, deleting its whole show folder
  (`/config/plugcheck/Plugin Check (2021)`) and scanning removes the episode (GET answers `Found` false), but Emby logs
  `Removing item from database, Type: Series` only and raises no `ItemRemoved` for the episode, so its store file
  (`26.json` on 4.9, `65.json` on 4.10) stayed; removed by hand afterwards. Same shape as Jellyfin 10.11 (spec §14,
  2026-09-14 plugin sweep task). `MediaItems.Id` is `INTEGER PRIMARY KEY AUTOINCREMENT` (4.9 `library.db`), so an
  orphaned file can't match a later item unless Emby's database is recreated while the plugin folder is kept.

## Task 4 — Emby plugin, fix round 1 (2026-09-14)

**Changes proven here.** Corrupt store files read as empty; the plugin only removes rows it wrote (`ReplaceOwn` per
POST); a replaced file's rows are removed, never written back; one lock across read-merge-save; rename-over saves and
start-up `.tmp` cleanup; store-then-chapters writes with rollback and a 500 JSON answer; the item path in the store
file; a daily/start-up sweep task; exact Skip Intro match in `emby_client.py`.

**Build.** Both ABIs rebuilt (`nice -n 19`, sdk:9.0): `Build succeeded.` 0 warnings, 0 errors each; only
`MediaPreviewBridge.Emby.dll` (+ `.deps.json`, `.pdb`) in `out/<abi>/`. Installed on both containers (sha256 matched);
both log the sweep task's daily trigger and a start-up run.

**Run.** Whole script on each container after the fix (`results/emby-plugin-<container>.json`). The first 4.9 run
failed only check 16 (see Findings); after the check was corrected both containers were re-run in full.
Items: `mlab-emby` S01E01 53 / S01E02 51, `mlab-emby49` 11 / 8.

| # | Check | Emby 4.10.0.40 | Emby 4.9.1.90 |
|---|---|---|---|
| 1 | Ping without a token | PASS: 200 `Ok` true, `Features` `["markers"]` | PASS |
| 2 | Unknown id, admin key | PASS: 200 `Found` false, "item not found" | PASS |
| 3 | No token / non-admin `viewer`, GET+POST+DELETE | PASS: 401 ×3 / 403 ×3 | PASS: same |
| 4 | POST 10–40 s + credits 100 s | PASS: `Stored` 3, 3 marker rows + 4 named plain chapters | PASS |
| 12 | Emby web after check 4 | PASS: `skip found: True \| t: 23.0 \| visible buttons: Skip Intro` (exact label) | n/a |
| 5 | POST 12–42 s, no credits | PASS: `Stored` 2, no `CreditsStart` | PASS |
| 6 | 5 invalid bodies | PASS: each 200 with `Error`, nothing changed | PASS |
| 7 | FullRefresh: "Replace all metadata" and "Search for missing metadata" | PASS: both wiped the rows and the plugin wrote them back (log line each) | PASS: same |
| 8 | Default, ValidationOnly, scan, restart; `999999999.json.tmp` planted before the restart | PASS: markers kept, no write-back; `.tmp` gone, "removed 1 unfinished marker file(s)" | PASS: same |
| 13 | DELETE, then Emby web at 0:22 | PASS: `Stored` 0, no rows; `skip found: False \| t: 42.1` (played through the old intro) | n/a |
| 14 | Corrupt store file: garbage, NUL bytes, `null`, `[]`, `{"IntroStartTicks":100000000,"IntroEndTicks":4`, `{"IntroSta`, empty | PASS: GET 200 `Found` true with no markers and no `Error` for each; FullRefresh wrote nothing back; exactly one warning per file ("SerializationException" ×4, "not a valid marker set" ×2, "ArgumentOutOfRangeException" for `{"IntroSta`), none quoting content; POST over it `Stored` 3 | PASS: same |
| 15 | Store file with another path (size right) | PASS: POST stores `Path`; GET `Stale` true; FullRefresh writes nothing; same file with the real path is written back | PASS: same |
| 9 | POST with `FileSize` + 1 (copy), FullRefresh | PASS: `Stale` true, `Stored` 0, no rows before or after | PASS |
| 10 | DELETE (copy, 3 rows stored) | PASS: `Stored` 0, rows and store file gone | PASS |
| 16 | A: copy overwritten by S01E02 (other size), scan. B: 1 byte appended with mtime kept, Default refresh, then a metadata edit | PASS: A same item id, no marker rows, `Stale` true, nothing written back (Emby's re-read removed the rows). B rows kept through the Default refresh (no item update), removed by the plugin on the metadata edit ("removed markers of a replaced file") | PASS: same |
| 11 | Copy removed, scan | PASS: store file deleted, "forgot markers of removed item" | PASS |
| 17 | S01E02 with marker rows the plugin didn't write (intro 20–50 s, added to `library.db` with Emby stopped), Emby marker detection ON | PASS: DELETE with nothing stored left them; POST 17–47 s + credits 100 s without `ReplaceOwn` → `Stored` 1, their intro + our credits; DELETE → their intro only, store file gone; POST with `ReplaceOwn` true → `Stored` 3, our intro + credits; DELETE → no marker rows, 4 plain chapters | PASS: same |
| 18 | Orphan: copy's show folder deleted + scan (control: S01E02 stored) | PASS: episode gone, its file stayed; sweep task run → orphan file deleted, S01E02's kept, "checked 2 stored item(s), removed 1" | PASS: same |
| 19 | Store folder made read-only (chmod 555), POST then DELETE on S01E02 | PASS: both HTTP 500 JSON `Found` true, `Error` "couldn't update the marker store (UnauthorizedAccessException)"; rows and stored set unchanged; DELETE after chmod 755 → 200, no rows | PASS: same |

Findings:
- Emby's own intro detection is an Emby Premiere feature: `GET /Registrations/intro-detection` →
  `IsRegistered: false`, and the "Detect Episode Intros" task (key `markers`) finishes in 0 s with detection on. Check
  17 therefore adds the other writer's rows straight into `Chapters3` (MarkerType 1/2) while the container is stopped;
  the plugin sees the same rows Emby's detection or TimeMarkEdit would leave.
- Default refresh, ValidationOnly refresh and a library scan of an unchanged item raise no `ItemUpdated` (a valid store
  file with missing rows was not written back after any of them); FullRefresh and a metadata edit (`POST /Items/{id}`)
  do. When a scan finds a changed file, Emby re-reads it and rewrites all its chapter rows itself, so marker rows are
  gone before the plugin looks. A file whose size changes without its mtime keeps its old rows until the item's next
  update (16B).
- `ReplaceOwn` binds case-insensitively (`replaceOwn` worked in a manual probe on 4.9 before the run).
- Lab left clean on both: no store files, no marker rows on S01E01/S01E02, "Plugin Check" library and
  `/config/plugcheck` removed, marker detection off, store folder 755, check sessions and web devices removed.

## Task 4 — Emby plugin, fix round 2 (2026-09-15)

**Changes.** `MarkerStore.Load` is best-effort: any exception reading or parsing counts as no markers (logged once,
type name only), and a file that doesn't end with `}` is refused before parsing ("incomplete file"). GET wraps the
whole read, POST/DELETE wrap the item lookup and the `Load` + `GetChapters` reads, so any read failure answers the 500
JSON `MarkersResponse`. The media file size is read before `MarkerStore.Gate` is taken (API and healer). The sweep
catches every exception per item and deletes nothing on an error. README: Emby Premiere's intro detection can replace
our rows.

**Build.** Both ABIs rebuilt: `Build succeeded.` 0 warnings / 0 errors each; installed DLL sha256 = build output.

**Run.** Whole script on both containers with the final build: Emby 4.10.0.40 **20/20**, Emby 4.9.1.90 **18/18**
(12 and 13 are 4.10 only). Every earlier check passes unchanged; changed and new rows:

| # | Check | Emby 4.10.0.40 | Emby 4.9.1.90 |
|---|---|---|---|
| 14 | 12 corrupt forms (added: truncated inside `Path`; cut inside `Path` then `}`; garbage ending in `}`; text where a number goes; valid JSON without markers) | PASS: every form GET 200 JSON with no markers, FullRefresh wrote nothing back, one warning each: "incomplete file" ×8, "SerializationException" (garbage ending in `}`), "IndexOutOfRangeException" (cut inside `Path` then `}`), "not a valid marker set" ×2; POST over a corrupt file `Stored` 3 | PASS: same |
| 20 | Every prefix (0–203 bytes) of the real 204-byte store file written by POST, then a file cut inside `Path` | PASS: all 204 prefixes GET 200 JSON with no markers; on the Path-cut file GET 200, DELETE 200 (file removed), POST 200 `Stored` 3 and the store file is valid again (`Path` back), GET shows the markers, 3 rows | PASS: same |

Findings:
- Before the round-2 build, 90 of 99 cuts in and around the `Path` string made GET fail with
  `IndexOutOfRangeException` (4.9, probe with the round-1 DLL).
- Emby's JSON reader also accepts an object cut after any complete token: with only the catch-all, 28 of the 204
  prefixes read as marker sets, 10 of them with a cut number (`CreditsStartTicks` 1, 10, … 100000000). The `}` test
  closes that; a cut inside a string still reaches the reader and fails there (covered by "cut inside Path, then }").
- Lab left clean on both (no store files, no marker rows, no copy library, detection off, store folder 755).
