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

## Task 4 — Emby plugin, milestone audit fix LOW-3: a write Emby stops half way (2026-09-15)

**Changes.** POST and DELETE save the store first with the rows they replace (`Replacing`: what the item's rows showed
of ours before), then the chapter rows, then the store without `Replacing`. `Apply`, DELETE and the healer take rows of
either set for ours; the healer finishes a stopped write at the item's next update ("finished an interrupted marker
write"). `MarkerStore.Save` flushes the temp file to disk (`Flush(true)`) before the rename. The app's contract is
unchanged (GET never shows `Replacing`).

**Build.** Both ABIs (`nice -n 19`, sdk:9.0, `dotnet build -c Release -p:EmbyAbi=<abi> -p:Version=1.0.0.0`): `Build
succeeded.` 0 warnings, 0 errors each. Installed for the run (sha256 = build output), then the builds installed before
were put back (`mlab-emby` 4f7e48b5…, `mlab-emby49` 06141003…) and both containers restarted.

**Script.** `emby_plugin_check.py` gained check 21 and `--checks N,…` (runs only those checks and merges them into the
container's results file); `store_dir` reads rotated Emby logs too. Check 21 plants the store file a stopped write
leaves (the new set with `Replacing` = the rows still on S01E01, or only `Replacing` for a DELETE) and then:

| Step | Before the fix (installed build, `mlab-emby49`) | After: Emby 4.10.0.40 and 4.9.1.90 |
|---|---|---|
| POST 12–42 s over rows 10–40 s + credits 100 s | `Stored` 0, the old rows stay (taken for another writer's) | `Stored` 2, rows 12–42 s only, store without `Replacing` |
| DELETE after the same stop | the old rows stay, store gone: nothing tracks them | no marker rows, store gone |
| Item update (metadata edit) after a stopped POST | old rows stay, `Replacing` never cleared | rows 12–42 s, store without `Replacing`, log line |
| Item update after a stopped DELETE | rows stay, store file left | no marker rows, store file deleted, log line |

**Run.** Whole table with the fix: Emby 4.10.0.40 **21/21** (one run), Emby 4.9.1.90 **19/19** (checks 1–8, then the
rest with `--checks`). Every earlier check passes unchanged. Before the fix, check 21 FAIL on 4.9 (table above); its
leftover rows were removed afterwards (store planted, DELETE, plain chapters intact).

Lab left clean on both: no store files, no marker rows on S01E01/S01E02, no copy library, the earlier builds loaded.

## Task 10 — Emby publisher through Media Preview Bridge for Emby (2026-09-15)

**Setup.** `mlab-app` rebuilt from lane `p2-task-10` (`media_preview_generator:p2-t10`), then put back on `pr-241`.
Intro & Credits turned on for `mlab-emby` (Emby 4.10.0.40, plugin 1.0.0.0) with `PUT /api/servers/mlab-emby
{"markers": {"enabled": true, "library_ids": null}}` (saved block gained `"emby": {"on_emby_redetect": "restore"}`), and
turned off again at the end. `mlab-emby49` has no app server, so Emby 4.9.1.90 was driven through the lane's
`EmbyMarkerPublisher` directly. Scripts and raw results: scratchpad `t10/lab_app_t10.py`, `t10/lab_publisher_t10.py`
(`lab-app-run.json`, `lab-app-cleanup.json`, `lab-publisher.json`, scrubbed).

**Emby groups S01E01 with its "- Extended" copy on both versions.** `GET /Users/{uid}/Items/{id}?Fields=MediaSources`
answers two sources for S01E01 and S01E01 - Extended (120 008 ms and 130 008 ms; the `/Items` list shows them apart),
and two equal sources (120 008 ms) for S01E03 and S01E03 - Copy. One chapter set can't fit two cuts, so those two
files are refused; the equal-length pair publishes (superseded: spec §14 2026-09-15).

| # | Check | Result |
|---|---|---|
| 1 | Status tab payload | PASS: `ready`, "Media Preview Bridge for Emby plugin", `plugin_version` 1.0.0.0, `can_show` intro + credits |
| 2 | Job 1 over the synth show, `mlab-emby` rows | PASS: S01E02, S01E03, S01E03 - Copy `markers_written` "2 marker(s)"; S01E01 and S01E01 - Extended `failed` "This Emby item groups versions of different lengths; one marker set can't fit them all" |
| 3 | Chapters after job 1 (`Fields=Chapters`) | PASS: S01E02 IntroStart 17 000 / IntroEnd 47 000 / CreditsStart 100 000; S01E03 and the Copy 25 000 / 55 000 / 100 000 (= `SYNTH_TRUTH`), each with its 4 original `Chapter` rows; nothing on the refused pair |
| 4 | Job 2, same files | PASS: the three `markers_up_to_date` "Up to date"; store files `51.json`, `52.json`, `54.json` mtimes unchanged; Emby log "stored markers for item" count unchanged (11): no POST |
| 5 | Inspector, S01E02 / S01E01 - Extended | PASS: `up_to_date`, reason ""; Extended `will_add` with reason "Emby skips to the end of the file" (credits 100–120 s on a 130 s file) |
| 6 | Emby 4.9, S01E02 Use ours | PASS: POST `ReplaceOwn` true then one chapter read; rows 17 000 / 47 000 / 100 000 + 4 plain; unchanged write = state read + chapter read, no POST; `shows` ours; DELETE leaves the 4 plain chapters |
| 7 | Emby 4.9, S01E03 with credits ending 10 s early | PASS: credits start 100 000 sent; `projection_note` "Emby skips to the end of the file"; unchanged write sends nothing; DELETE clean |
| 8 | Emby 4.9, S01E01 (grouped) | PASS: refused before any plugin call |
| 9 | Emby 4.10, Keep Emby's on Synth Show (2020) S01E01 (Emby rows IntroStart 20 s, IntroEnd 50 s, CreditsStart 200 s, not the plugin's) | PASS: POST `ReplaceOwn` false; ours `[]`, kept intro + credits; plugin stores ours (300000000 / 600000000 / 2100000000, size 12 695 354); Emby's 3 rows unchanged; `shows` with kept types ours; second write: no POST; DELETE clears the store, Emby's rows unchanged |

Other job rows: Plex, Jellyfin 10.11 and 12.0 were up to date on both jobs except one Plex write on job 1 (S01E01, "2
marker(s)", up to date on job 2), from phase-2 Plex code on a lab `markers.db` written by `pr-241`.

Lab left clean: `DELETE /MediaPreviewBridge/Markers/{51,52,54}` (200, `Stored` 0), no store files on either Emby, no
plugin rows on the synth episodes (plain chapters intact), Emby Intro & Credits off, `mlab-app` on `pr-241` and healthy.

## Task 10 — round 1: Emby versions share one marker set (2026-09-15; superseded by round 3 below)

**What Emby does.** Every version is its own item with its own chapter rows (S01E01 = item 53 with 4 chapters,
S01E01 - Extended = item 55 with 5), and the per-user item answer lists all versions as MediaSources, the item's own
file first (53: S01E01, Extended; 55: Extended, S01E01; the same on 4.9 with items 11/10). `GET /Items?Ids=` without a
user id lists only the item's own file. The plugin compares its stored size and path with the POSTed item's own
`item.Path` and that file's size (`StoredMarkers.IsStale`), so each version item takes its own file's size: no plugin
change.

**Rule.** The Plex rule (plex-item-publishing.md): a type goes onto a version item only when every version decided it
within 2 s; otherwise the row waits ("Waiting for this item's other versions to agree on: …"). The versions are
recorded (`item_files`); a version added or re-decided apart since shows as `VERSIONS_CHANGED` in the read-back.

**Run.** `mlab-app` on `media_preview_generator:p2-t10` (round 1 build), Intro & Credits on for `mlab-emby` only (Plex
and both Jellyfins switched off for the run, so nothing was written there, and switched back on). Jobs over Synth
Chapters (2021). POSTs counted from the Emby log ("stored markers for item") and the store files' mtimes. Script:
scratchpad `t10/lab_app_r1.py`, results `lab-r1-run.json`, `lab-r1-cleanup.json`.

| Job | Change before it | S01E01 (53) | S01E01 - Extended (55) | S01E02 / S01E03 / Copy | POSTs |
|---|---|---|---|---|---|
| 1 | — | written "2 marker(s)"; IntroStart 10 000 / IntroEnd 40 000 / CreditsStart 100 000 + 4 chapters | written "2 marker(s); Emby skips to the end of the file"; same rows + 5 chapters | written at `SYNTH_TRUTH` | 5 |
| 2 | — | up to date | up to date "Up to date; Emby skips to the end of the file" | up to date | 0 |
| 3 | Extended's credits locked 5 s later (105 000) in `markers.db` | waiting "…agree on: credits" (the read-back saw Extended re-decided apart); intro only | waiting "…agree on: credits"; intro only | up to date | 2 |
| 4 | — | waiting, unchanged | waiting, unchanged | up to date | 0 |
| 5 | credits row restored (100 000, unlocked) | written "2 marker(s)" | written "2 marker(s); Emby skips…" | up to date | 2 |
| 6 | — | up to date | up to date | up to date | 0 |

Inspector after job 6: S01E01 `up_to_date`; Extended `up_to_date`, reason "Emby skips to the end of the file".

Lab left clean: `DELETE /MediaPreviewBridge/Markers/{51..55}` (200, `Stored` 0), no store files on either Emby, no
plugin rows on the synth episodes (plain chapters intact), Extended's credits row as before, Plex/Jellyfin switches
back on, Emby off, `mlab-app` on `pr-241`.

## Task 10 — round 3: each Emby version plays its own chapters, so each is published on its own (2026-09-15)

**Emby web, grouped item.** On `mlab-emby` (4.10) the plugin stored intro 10–40 s on S01E01 (item 53) and intro
20–50 s on S01E01 - Extended (item 55). Logged in as `lab`, item 53's page, "Version" picked in the selector, Play,
then seeks; "Skip Intro" read from the visible buttons 3 s after each seek. Script: scratchpad
`t10/emby_versions_play.py`. Screenshots: `docs/design/intro-credits/evidence/screenshots/phase2/task10-r3-*.png`.

| Page / version picked | Stream | 7.9 s | 14.9 s (only 53's intro) | 47.9 s (only 55's intro) | 57.9 s |
|---|---|---|---|---|---|
| 53 / S01E01 | `videos/55/…?MediaSourceId=mediasource_53` | no | **Skip Intro** (`page53-v53-at12`) | no (`page53-v53-at45`) | no |
| 53 / Extended | `videos/55/…?MediaSourceId=mediasource_55` | no | no (`page53-v55-at12`) | **Skip Intro** (`page53-v55-at45`) | no |
| 55 / Extended | `videos/55/…?MediaSourceId=mediasource_55` | no | no | **Skip Intro** | no |
| 55 / S01E01 | the player moved on to S01E02 (item 51) before the first seek, twice (`page55-v53-at5`) | — | — | — | — |

The version picker's screenshot is `page53-v55-item`. The player shows the chapters of the version it plays, not
the chapters of the page's item. Clicking "Skip Intro" opens Emby Premiere's "Unlock Feature" dialog on this unlicensed
server, so where a skip lands wasn't measured. Markers deleted afterwards, the played state reset and the web devices
removed.

**API.** With an API key and no user id, `GET /Items?Ids=53&Fields=MediaSources` lists only item 53's own source.
Adding `AlternateMediaSources` lists both. Each source has `ItemId` (53, 55) and `Id` `mediasource_<item id>`, on 4.9.1.90
(items 11/10) and 4.10. The per-user route lists both either way. `/Items?UserId=` as a query parameter lists only the
own source. `Chapters` in the same read gives that item's own rows (53: 4, 55: 5).

**Rule now.** Each Emby version is its own item and is published on its own, the way Jellyfin versions are. There's no
agreement across versions, no waiting and no recorded version files. The write reads the item once
(`Fields=Chapters,MediaSources,AlternateMediaSources`) and checks that this file is that item's own version.
Emby listing the file under another version's item fails the row ("This file is Emby item 55, another version of
item 53"). An item with several versions and none of them this file waits as not in the library. The POST carries
this file's size.

**Publisher on both Embys.** Lane `EmbyMarkerPublisher`, scratchpad `t10/lab_publisher_r3.py`
(`lab-publisher-r3.json`). The 4.9 client had an API key only, the 4.10 client a user id.

| Check | Emby 4.9 (API key) | Emby 4.10 (user id) |
|---|---|---|
| Versions read | 11: S01E01→11, Extended→10; 10: Extended→10, S01E01→11 | 53: →53, →55; 55: →55, →53 |
| First write, per item | item read, POST `ReplaceOwn` true, chapter read; 11 IntroStart 10 000 / IntroEnd 40 000, 10 20 000 / 50 000 | same on 53 / 55 |
| Unchanged second write | item read + store read, no POST; `shows` ours | same |
| Extended file offered on S01E01's item | `PublishError` "This file is Emby item 10, another version of item 11", one item read, no plugin call | same with 55 / 53 |
| DELETE both | no marker rows, 4 and 5 plain chapters left, stores empty | same |

**Run.** `mlab-app` on `media_preview_generator:p2-t10` (round 3 build), Intro & Credits on for `mlab-emby` only, same
sequence and counting as round 1. Script: scratchpad `t10/lab_app_r3.py` (`lab-r3-run.json`, `lab-r3-cleanup.json`).

| Job | Change before it | S01E01 (53) | S01E01 - Extended (55) | S01E02 / S01E03 / Copy | POSTs |
|---|---|---|---|---|---|
| 1 | — | written "2 marker(s)"; IntroStart 10 000 / IntroEnd 40 000 / CreditsStart 100 000 + 4 chapters | written "2 marker(s); Emby skips to the end of the file"; same rows + 5 chapters | written | 5 |
| 2 | — | up to date | up to date "Up to date; Emby skips to the end of the file" | up to date | 0 |
| 3 | Extended's credits locked 5 s later (105 000) | up to date, CreditsStart 100 000 | written "2 marker(s); Emby skips…", CreditsStart 105 000 (only `55.json` changed) | up to date | 1 |
| 4 | — | up to date | up to date | up to date | 0 |
| 5 | credits row restored (100 000, unlocked) | up to date | written, CreditsStart 100 000 | up to date | 1 |
| 6 | — | up to date | up to date | up to date | 0 |

Inspector after job 6: S01E01 `up_to_date`; Extended `up_to_date`, reason "Emby skips to the end of the file".

Lab left clean: `DELETE /MediaPreviewBridge/Markers/{51..55}` (200, `Stored` 0), no store files on either Emby, no
plugin rows on the synth episodes (plain chapters intact), Extended's credits row as before, Plex/Jellyfin switches
back on, Emby off, `mlab-app` on `pr-241`.

## Task 12 — Season view API and local source status (2026-09-15)

**Setup.** Lane `p2-task-12` built as `media_preview_generator:p2-t12`. `mlab-app` was left alone on `pr-241` (Task 11
ran in parallel): a throwaway `mlab-app-t12` ran the lane image on port 18082 with the lab mounts and a copy of
`mlab_app_config` without `jobs.db` or `scheduler.db` (nothing revived or scheduled) and off the `mlab` network (no lab
server reachable). Removed afterwards with its volume. Script: scratchpad `t12/app_t12.sh`, `t12/smoke_t12.py`
(`smoke_t12.out`).

The lab `markers.db` was written by `pr-241`: Task 7's season audio lab run hasn't happened on it, so no episode
carries season audio evidence yet and no chip has a `"10/10"` label.

```
{'episodes': 11, 'needs_review': 2, 'ready': 9} ['E01', 'E02', 'E03', 'E04', 'E05', 'E06', 'E07', 'E08', 'E09', 'E10', 'E11']
{'mlab-plex': True, 'mlab-jellyfin': True, 'mlab-jf12': True, 'mlab-emby': False}
{'season_audio': {'available': True, 'ffmpeg': '/usr/lib/jellyfin-ffmpeg/ffmpeg', 'message': ''}}
```

| Check | Result |
|---|---|
| Episodes, counts | PASS: E01–E11; 9 ready, 2 Needs review (E02 and E09 credits: "sources disagree: introdb/theintrodb, server_markers, skipdb", "agreeing sources conflict: introdb/theintrodb") |
| Servers | PASS: every lab server listed; Emby `markers_enabled` false (Intro & Credits off there since Task 10) |
| Chips | PASS: `theintrodb`, `introdb`, `skipdb` on every episode, no `server_markers` chip |
| Dots | PASS: Plex, Jellyfin 10.11 and 12.0 `ok` on all 11; Emby `off` |
| Local sources | PASS: available, jellyfin-ffmpeg |
| Refusals | PASS: a real movie file `400` "Not a TV episode" (GET and POST publish); `../` out of the season folder `400`; no token `401` |

`POST /api/markers/season/publish` for an episode wasn't run on the lab: its job would look up online sources for the
two undecided episodes. Its route, trigger and job reuse are covered by `tests/markers/test_api_markers.py` and
`tests/markers/test_triggers.py` (real `JobManager`).

## Task 11 — Intro & Credits · Check servers (2026-09-15)

**Setup.** `mlab-app` rebuilt from lane `p2-task-11` (`media_preview_generator:p2-t11`), then put back on `pr-241`.
Intro & Credits on for `mlab-plex`, `mlab-jellyfin` and `mlab-jf12` (as found), `mlab-emby` off except in check 5.
Published items in `markers.db`: Plex 546, Jellyfin 10.11 548, Jellyfin 12.0 548, Emby 5. Script and raw results:
scratchpad `t11/lab/lab_t11.py` (`fresh`, `dry`, `baseline`, `recheck`, `drift`, `emby`, `schedule`, `cleanup` `.json`,
scrubbed).

| # | Check | Result |
|---|---|---|
| 1 | Schedules after the app started on the lane image | PASS: `[]`, no built-in job |
| 2 | Dry run inside the app on a copy of `markers.db` | PASS: no drift, no warnings, 0 files to ask servers again; read-back of all 1 642 items on the three servers with Intro & Credits on took 28.9 s |
| 3 | `POST /api/markers/reconcile`, nothing changed | PASS: 202, LOW job "Intro & Credits · Check servers" completed in 31 s with no files; log "Every server checked still shows what this app published" |
| 4 | Emby's empty answer for Rick and Morty S01E03 (credits decided) aged to two days, then Check servers | PASS: the file alone listed ("1 decided file(s) to ask servers again"), Emby read again (answer's `fetched_at` Sep 14 07:43 → Sep 15 01:00), taken time recorded; Plex/Jellyfin rows up to date; the next run lists nothing |
| 5 | Our taggings rows deleted from Plex item 64 (Synth S01E02) and the Jellyfin 10.11 plugin's markers deleted (204), then Check servers | PASS: "2 published item(s) changed on servers (1 file(s))"; S01E02 `markers_written` on Plex and Jellyfin 10.11, up to date on Jellyfin 12.0; Plex rows (text, offsets, `extra_data`), Plex-served markers and Jellyfin segments equal to before; the next run finds nothing |
| 6 | Emby on: its 5 version items were published before and the plugin store emptied since | PASS: all 5 `markers_written` on Emby (per version: items 51–55), Plex and both Jellyfins up to date; chapters show IntroStart/IntroEnd/CreditsStart |
| 7 | Plugin markers of S01E01 - Extended (item 55) deleted, then Check servers | PASS: only item 55 listed and written; S01E01 (53, the other version) untouched; the next run finds nothing |
| 8 | Saved schedule `{"job_type": "intro_credits", "reconcile": true}` every 720 min, "Run now" twice 1 s apart | PASS: one LOW job with `parent_schedule_id` and `reconcile`; log "Check servers job dc10fe0e hasn't finished; not queueing another"; `last_run` set |

Plex read-back lock hold, measured on a copy of the lab Plex database (Plex's lock proof stubbed): a single-item
read-back holds this process's lock 2.0–2.4 ms median, 2.4–2.8 ms max (mostly SQLite loading Plex's schema on the new
connection); a Check servers slice 2.1 ms median, 3.6–5.4 ms max, about one item per connection on this database, 1.8 s
for 516 items (three runs each, `t11/hold-measure-2ms-predictive.txt`). Holding one connection for a 500-item chunk
would hold it about 90 ms.

Lab left clean: schedule deleted, `DELETE /MediaPreviewBridge/Markers/{51..55}` on Emby (200, `Stored` 0, 0 store
files, no marker chapters), Emby Intro & Credits off, S01E02's Plex rows and Jellyfin segments as before, `mlab-app` on
`pr-241` with the same switches. Left in the lab `markers.db`: the new `server_marker_rechecks` table (one row) and
`idx_publish_state_item` index (the `pr-241` build ignores both), and Rick and Morty S01E03's Plex item now has its
version files recorded (the pipeline's one recording write).

## Task 11 — fix round 1: rotation, backoff, deleted items (2026-09-15)

**Setup.** `mlab-app` rebuilt from lane `p2-task-11` after fix round 1 (`media_preview_generator:p2-t11`), then put
back on `pr-241`. Switches as found (Intro & Credits on for `mlab-plex`, `mlab-jellyfin`, `mlab-jf12`; `mlab-emby` off;
no schedules). Script and raw results: scratchpad `t11/lab-r1/lab_r1.py` (`dry`, `unchanged`, `already-queued`,
`drift`, `gone` `.json`, scrubbed).

| # | Check | Result |
|---|---|---|
| 1 | Listing inside the app on a copy of `markers.db` | PASS: published Plex 546, Jellyfin 10.11 548, Jellyfin 12.0 548, Emby 5; nothing listed, no warnings; 25 s |
| 2 | `POST /api/markers/reconcile` twice, nothing changed | PASS: both 202 `already_queued: false`, LOW, completed in 25 s and 26 s with no files and no warning; log "0 published item(s) changed on servers (0 file(s) this run); 0 decided file(s) to ask servers again" |
| 3 | Two POSTs back to back (low, then high) | PASS: second answer `{"already_queued": true}` with the first job's id; one job, LOW |
| 4 | Our taggings rows deleted from Plex item 64 (Synth S01E02) and the Jellyfin 10.11 plugin's markers deleted (204), then Check servers | PASS: "2 published item(s) changed on servers (1 file(s) this run)"; `markers_written` on Plex and Jellyfin 10.11, up to date on Jellyfin 12.0; Plex rows, Plex-served markers and Jellyfin segments equal to before; the next run lists nothing |
| 5 | S01E02's Jellyfin 10.11 publish rows pointed at item `0badc0de…` (Jellyfin has no such item; the real item's row kept), then Check servers three times | PASS: run 1 lists the file with no warning ("1 published item(s) changed"), Jellyfin 10.11 up to date on the real item, publish row back on the real item id; run 2 marks the missing item `gone` quietly, no files, no warning; run 3 lists nothing; segments on the real item unchanged. The fake row was deleted afterwards |

Screenshots retaken with the new tooltip copy: `task11-schedule-modal-check-servers-tooltip.png` and
`task11-start-job-check-servers-tooltip.png`.

The Plex read-back no longer has the 2 ms time budget measured above: it reads one item per connection, the single
read-back's lock footprint, with the settings, database files and lock proof checked once per call and the schema on
its first connection.

Lab left clean: `mlab-app` on `pr-241` with the same switches, no schedules, S01E02's Plex rows and Jellyfin segments
as before. The Task 11 tables were dropped from the lab `markers.db` (`server_marker_rechecks` from the first build,
`server_marker_rereads`, `drift_listings`); `idx_publish_state_item` stays (ignored by `pr-241`).

## Task 11 — fix round 2: a deleted Emby item (2026-09-15)

**Setup.** `mlab-app` rebuilt from lane `p2-task-11` after fix round 2, then put back on `pr-241`. Script and raw
results: scratchpad `t11/lab-r2/lab_r2.py` (`emby-gone`, `cleanup` `.json`, scrubbed).

| # | Check | Result |
|---|---|---|
| 1 | Emby Intro & Credits on (its plugin store was emptied by round 0's cleanup), Check servers twice | PASS: first run "5 published item(s) changed", all 5 version items `markers_written`; second run lists nothing |
| 2 | S01E02's Emby publish rows pointed at item `999999999997` (Emby has no such item; item 51's row kept), then Check servers three times | PASS: run 1 lists the file with no warning and the publish row goes back to item 51 (up to date); run 2 marks `999999999997` `gone` with no files and no warning; run 3 lists nothing; the marker chapters on all 5 items unchanged |

Cassettes for the same lookups (`tests/test_servers_emby_markers_vcr.py::TestEmbyItemMissingContract`, per user: 404;
API key: an empty `Items` list; and `tests/test_servers_jellyfin_vcr.py::TestJellyfinItemMissingContract`, Jellyfin
10.11: `/MediaSegments` 404 then an empty `Items` list) were recorded from `mlab-emby` and `mlab-jellyfin`.

Lab left clean: the fake row deleted; `DELETE /MediaPreviewBridge/Markers/{51..55}` on Emby (200, `Stored` 0, 0 store
files, no marker chapters); Emby Intro & Credits off as found; `mlab-app` on `pr-241` with the same switches and no
schedules; the Task 11 tables dropped from the lab `markers.db` again.

## Task 17 — phase-2 lab matrix (2026-09-15, `feat/markers-detection` 93e5c60, image `ca49022ec1fd`)

**22 pass, 0 fail, 1 needs owner (row 20).** Product fixes: none. Phase-1 regression: 16 of 16 rows pass. The phase 2
close-out (below) records row 20 as pass in Plex Web and re-ran row 10 with the `final` flag compared.

Runner: `./phase2_matrix.py configure`, then `./phase2_matrix.py run <rows>`. The lab scripts take the lab folder from
`MLAB_DIR`, so a worktree runs against the long-lived lab. Raw evidence is in `results/p2-row-NN.json`,
`results/p2-lab-setup.json` and `results/p2-note6-playbackinfo.json` (all git-ignored). Rows ran in this order: 23, 1,
21, 17, 19, 22, 2, 3, 4, 18, 5, 6, 8, 7, 9, 10, 11, 12, 13, 14, 16, 15, 20. Rows 7, 10, 14 and 16 were re-run after
their harness was corrected (see "Expectations changed"). Fix round 1 re-ran rows 1, 2, 3, 13, 14, 15 and 23, and
phase-1 row 7 under row 19's conditions, with the checks below; all pass.

### Lab setup

- **App:** `mlab-app` on `media_preview_generator:intro-credits`, built from 93e5c60, on a fresh `mlab_app_config`.
- **Servers:** all five are `ready`.
  - Lab Plex (Plex Pass, ext4, lock holder).
  - Jellyfin 10.11 (plugin 10.11.1.0) and Jellyfin 12.0 (plugin 12.0.1.0).
  - Emby 4.10.0.40 and 4.9.1.90, plugin 1.0.0.0 built from the same tree: 4.10 sha256 `06d7a38e…`, 4.9 `4133a56f…` (manual).
  - Intro & Credits is on for all five, with every library.
- **Local sources:** `GET /api/markers/sources/local` answered `/usr/lib/jellyfin-ffmpeg/ffmpeg`, available.
- **New mounts in `up.sh`:**
  - `/media/synth-audio` and `/media/synth-movies` on every server and the app.
  - `/media/plexonly` on `mlab-plex` only.
- **Libraries created by `configure`:**

  | Server | Synth Chapters | Synth Audio | Synth Movies |
  |---|---|---|---|
  | Plex (section keys) | 3, plus `/media/plexonly` as its second location | 4 | 5 |
  | Jellyfin 10.11 and 12.0 | `962eee9a…` | `a347c26d…` | `fec5ae97…` |
  | Emby 4.10 | 47 | 102 | 104 |
  | Emby 4.9 | 3 | 80 | 82 |

- **Synth media:**
  - `synth_audio.sh`: Synth Audio (2022) S01E01–E04, S02E01 and a staged S02E02, each 300 s with a 30 s theme.
  - `synth_chapters.sh` adds the two-version Synth Movie (2023) (1080p and 720p, End Credits 1:40–2:00) and a staged
    116 s Plexonly cut of S01E03.
  - Matcher check on Season 01 (manual): all four themes found, within 1 s of the start and 2 s of the end.

### Lab reset used

1. **On the old app (`pr-241`, old `markers.db`):** turn intro and credits detection off, then run one normal job over
   Synth Chapters, Synth Show, Rick and Morty S01, South Park S01, Toy Story and Up. It removed only our markers: 28
   files written. Plex's own rows stay (11 rows on those items).
2. **Synth folder:** delete phase 1's `S01E01 - Extended` and `S01E03 - Copy` from the Synth Chapters season.
3. **Containers:** remove `mlab-app` and `mlab_app_config`, then recreate the five servers with the new mounts (their
   volumes are kept).
   - The phase 1 scale mounts stay mounted.
   - Lab Plex's detection prefs stay `never`. Only rows 3, 12, 13 and the phase-1 regression turned intro and credits
     detection to `asap`, and each put it back.
4. **Plugins:** install both Emby plugin builds; the earlier DLLs are backed up outside the repo.
5. **App:** `./app.sh`, then `./phase2_matrix.py configure`.

### Rows

| Row | Result | Evidence |
|---|---|---|
| 1 Capability | pass | 5 of 5 `ready`. Both Embys report `plugin_version` 1.0.0.0, the csproj version; the evidence records no `MLAB_EMBY_PLUGIN_VERSION` override. That these are the DLLs built from this tree rests on the sha256 above (manual). Season audio is available with jellyfin-ffmpeg. |
| 2 Season audio backfill, High then Medium | pass | Both runs, forced, on 5 servers: every S01 episode Needs review with season audio 3/3 near its theme (E01 19.4–48.3 s against 20–50 s). Nothing of ours is published: Plex serves the same intro rows as before each run (its own, from row 3), and Jellyfin and Emby serve none. High ran after the show's 5 fingerprints were deleted, so it fingerprinted: chromaprint ffmpeg peaked at 1 at once, with `-threads 2`. Medium ran no chromaprint ffmpeg (peak 0, no argv): a forced run reuses cached fingerprints, and High had cached them all. S02E01 gets no same-season answer. Its previous-season hint was empty in the first run and 4/4 at 14.1–43.7 s (theme 15–45 s) in the second. |
| 3 High alone (G3) | pass | Plex's own forced season intro detection found all four themes: E01 17.7–47.0 s, E02 42.7–72.0 s, E03 2.6–31.9 s, E04 67.7–97.0 s. S01E02 has Plex's marker (42.7–72.0 s) and season audio (44.5–73.3 s) agreeing within 5 s (a pass condition) and stays Needs review: "Season audio and a server's own marker agree, but both come from matching audio; needs another source". |
| 4 Weekly release | pass | Sonarr webhook for S02E02, then its follow-up job, then exactly one Season job at NORMAL: "Season: Synth Audio (2022) · Season 02", holding only S02E01. Afterwards S02E01 has season audio 1/1 at 14.5–43.6 s and S02E02 1/1 at 59.4–88.5 s, both Needs review, and nothing is served. |
| 5 Rick and Morty S01 at High | pass | Normal job after row 18's forced run. Intro `decided_by` includes season_audio on 11 of 11. On the 11 online truth cases: 11 useful, 0 wrong, 0 missed (phase 1: 11 intros from online agreement). |
| 6 Emby write and serve (4.10) | pass | Synth Chapters E01–E03 show IntroStart, IntroEnd and CreditsStart at the decisions, and keep their 4 plain chapters. Synth Audio S01 has no marker chapters. The Extended copy (130 s) is its own item (120) with its own CreditsStart 100 s; its Files row reads "2 marker(s); Emby skips to the end of the file". The old "can't show credits" text doesn't appear. |
| 7 Emby wipe matrix and a replaced file | pass | S01E02 keeps its markers through FullRefresh with Replace all (healed), Default, ValidationOnly, a library scan and a restart. S01E03 replaced by a new encode (6,467,367 bytes; the file it replaced was 5,038,532, the first run's re-encode): plugin GET `Stale` true and no marker chapters. The next normal job writes it (`markers_written`), and `Stale` is false. |
| 8 Emby web Skip Intro | pass | `skip found: True`, t 22.9 s, visible button "Skip Intro". The lab Embys have no Emby Premiere key, so a click opens "Unlock Feature" and doesn't skip (close-out, "Emby needs Emby Premiere to skip intros"). Native Emby apps: not testable here. |
| 9 Emby 4.9 | pass | Row 6's first part: marker chapters equal the decisions on 7 files, with 4 plain chapters kept. FullRefresh: markers back. |
| 10 Check servers restores | pass | Re-run in the close-out with the `final` flag compared. A forced job on S01E01 first rewrote its stale non-final credits as final (they run to the file's end). Our rows dropped on all three: Plex taggings deleted, Jellyfin 10.11 plugin DELETE, Emby plugin DELETE. One Check servers job listed S01E01 and E03 (2 published), wrote Plex, Jellyfin and Emby back, and all serve the markers from before the drop, Plex's `final` flag included. The second run listed 0 files: "0 published item(s) changed on servers". |
| 11 Plex version drift | pass | The Plexonly cut was added as a second version of S01E03, which the app can't read. Check servers first: it listed E03, Plex then served none of ours, and the row read "Waiting for this item's other versions to agree on: intro, credits". A normal job kept it waiting. With the copy removed, a normal job wrote both markers again. |
| 12 Plex P3 and P4 | pass | P3 answer below. P4 answer below. At the end Rick and Morty S01E01 and Synth S01E02 are written back. |
| 13 L274 final flag | pass | With Keep Plex's, Plex's forced intro detection on the season changed the intro (129.0–156.8 s → 126.8–157.1 s; the row requires the times to differ) and left the credits rows alone (1298000–1320000, `final` true before and after). The next job reads "Keeping Plex's intro" and doesn't mention credits. A normal job over the season under "Use ours" put ours back. |
| 14 L263 movie versions | pass | Jellyfin 10.11 and 12.0 list both versions (alternate MediaSources) with Outro 100–120 s each (start and end ticks checked). Each Emby version is its own item with CreditsStart 100 s, on 4.10 (115/116) and 4.9 (93/94). Plex: the row splits an earlier merge, so Plex lists two local items (1369, 1375). A job publishes both: each serves credits 100–120 s, and the app's record of each lists its one version file. The row then merges them in Plex, and a second job leaves one item with 2 versions serving credits 100–120 s. The app's record of the merged item lists both version files, and both files' Plex rows are written or up to date. |
| 15 Cassettes with lab servers stopped | pass | `test_servers_markers_vcr.py`, `test_servers_emby_markers_vcr.py` and `test_servers_jellyfin_vcr.py`: 28 passed with `mlab-plex`, `mlab-jellyfin` and `mlab-emby` stopped; they were started again afterwards. |
| 16 Season view in the real app | pass | Synth Chapters S01 (note 3): "Synth Chapters (2021) · Season 1", "3 episodes", "3 ready", "Publish 3 to 5 servers", 15 of 15 dots green. Publish queued "Intro & Credits: Synth Chapters (2021) · Season 1" at NORMAL, which completed. Screenshot: `../screenshots/phase2/task17-season-view-synth-chapters.png`. |
| 17 Security | pass | Without the token, `GET /api/markers/season`, `POST /api/markers/season/publish`, `GET /api/markers/sources/local` and `POST /api/markers/reconcile` all answer 401. With it, `season?path=/etc/passwd` and `…/synth-audio/../../etc/passwd` answer 400. |
| 18 Resources | pass | Forced job on Rick and Morty S01 with its 11 fingerprints deleted, 24 s. Chromaprint: at most 1 ffmpeg at once (sampled every 0.5 s), with `-threads 2`. `mlab-app` peaked at 303.6 % CPU and 242 MiB, against phase 1 row 15's 86 % and 99 MiB (no fingerprinting then). |
| 19 Phase-1 regression | pass | Rows 14, 1, 2, 3, 13, 4, 6, 5, 7, 8, 9, 10, 16, 18, 19 and 17 all pass. Row 1: 5 Markers written and 9 Needs review; Rick and Morty credits sources disagree today, see the notes. Two expectations changed, see below. |
| 20 Plex app | pass (Plex Web) | Plex Web 4.160.0 on the lab Plex, Synth Chapters S01E02 (our intro 17–47 s, credits 100–120 s): "Skip Intro" shown at 18.0 s and a click landed at 47.00 s; "Skip Credits" shown at 101.0 s and a click landed at 120.00 s, then S01E03 started. Viewers need Plex Pass on their own account (or a Plex Pass admin's Home). Plex Web ignores the credits `final` flag. Native Plex apps: not testable here. Details in the close-out. |
| 21 Check servers schedule | pass | Fresh config: `GET /api/schedules` answers `[]`. A saved `{"job_type": "intro_credits", "reconcile": true}` schedule with Run now twice gave exactly one LOW "Intro & Credits · Check servers" job carrying the schedule's id. The schedule was then deleted. |
| 22 Deleted Jellyfin item | pass | Phase 1's published `S01E01 - Extended` and `S01E03 - Copy` were removed and scanned. Check servers (E01, E02 and E03 listed) raised no "Couldn't read" warning, and the run after listed nothing. |
| 23 Emby `Replacing` round trip (note 9) | pass | Check 22 on both Embys (`emby_plugin_check.py --checks 22`). S01E01 starts with markers at 10 s, 40 s and credits 100 s. A slow SQLite trigger on `Chapters3` held a POST of 12–42 s, no credits, inside Emby's chapter write. The store file, read while that POST still waited, held IntroStart 12 s, IntroEnd 42 s, no credits, and `Replacing` IntroStart 10 s, IntroEnd 40 s, CreditsStart 100 s. The container was then killed: `library.db` still held the three old marker rows (MarkerType 1, 2 and 3 at 10, 40 and 100 s). After the restart, Emby served the old markers and the plugin GET answered IntroStart 12 s. A POST without `ReplaceOwn` answered 200 with `Stored` 2. Emby then served only 12 s and 42 s, the store file had no `Replacing`, and the plain chapters were unchanged. The trigger and its table were dropped in a `finally` block; `hold_objects_left` is 0 on both. |

**P3 (row 12).** Setup: our markers removed from Rick and Morty S01E01, then Plex's forced credits detection added its
own credits (1297324–1321472 served, `final`). A job with credits detection off then published only our intro (129.0–156.8
s).

- **Served:** Plex serves both.
- **`[index]`:** credits 0, intro 1. That is text order, then `time_offset`, the order the publisher writes.
- **After Plex's forced credits detection again:** both rows are unchanged (same served times and `[index]`).

**P4 (row 12).** Setup: our intro removed from Synth S01E02 (credits kept). The `pv:intros` key was deleted, so Plex
served credits only. Then Plex's non-forced intro detection ran on the season.

- **Part:** Plex analysed the part again; `pv:intros` came back on it.
- **Rows:** Plex added no intro row of its own, and served nothing new.

**Note 6, Emby `PlaybackInfo` of a grouped item** (`results/p2-note6-playbackinfo.json`).

- **Both versions listed:** on 4.10 and 4.9, PlaybackInfo for S01E01's item and for the Extended item lists both
  versions as MediaSources. The asked item's own file comes first.
- **Chapters:** each MediaSource carries its own item's `Chapters`. Plain chapters are 4 on `mediasource_53`/`_11` and
  5 on `mediasource_122`/`_100`, each with our marker chapters.

### Expectations changed

- **Synth audio unique parts.** The brief's pink and brown noise made the matcher check fail (BAD ×4).
  - expectation changed: chromaprint hashes any stationary noise alike, so noise with different seeds matched across
    episodes at every shift.
  - The unique parts are now seeded pseudo-random melodies, and the theme is a fixed-seed melody (`synth_audio.sh`
    header).
- **Row 2 (note 1).**
  - expectation changed: S02E01's previous-season hint uses the previous season's cached fingerprints only (spec §6.2
    step 4; `test_no_cached_previous_season_gives_no_hint_and_fingerprints_nothing_else`).
  - In a fresh show's first backfill, S02E01 ran before S01 was fingerprinted, so its hint was empty.
  - The row checks "no same-season answer" in both runs, and "hint near the theme" on the second run.
- **Row 5.**
  - expectation changed: a normal job stops asking once the online sources agree, so season audio reaches `decided_by`
    only after a run that asks every source.
  - The row runs after row 18's forced job.
- **Row 7.**
  - expectation changed: a re-encode of S01E03 (`-b:v 250k`, audio copied) moved every chapter 7 ms later (Opus codec
    delay), which changed the truth for later rows.
  - The replacement is now a fresh `synth_chapters.sh` encode of S01E03: same chapters, another size.
- **Row 10.**
  - Plex's markers are dropped by deleting our taggings rows. Plex's forced credits detection fails on the synth files
    (phase 1 row 5), so it would drop nothing.
  - In the first run, the dropped credits row carried `final` false, left from when S01E01 had its 130 s second version,
    and the restored row carried `final` true. The close-out fixed the publisher and re-ran the row with the flag
    compared (see "Phase 2 close-out").
- **Row 11.**
  - expectation changed: the row predates Plex version drift (Task 9). A version the app can't read now takes our
    markers off the item ("Waiting for this item's other versions to agree").
  - Checked with Check servers first, then a normal job, then the copy removed and a normal job.
- **Row 14.**
  - Plex's movie agent matches nothing for the synth movie, so Plex lists each version as its own local item.
  - expectation changed: the row splits an earlier merge (`PUT /library/metadata/{id}/split`), publishes the two
    separate items, merges them (`PUT /library/metadata/{id}/merge`) and checks the merged item with 2 versions and
    the app's records of the item's version files before and after.
- **Row 16 (note 3).** It uses Synth Chapters S01. The job name follows Task 12's code: "Season 1", not "Season 01".
- **Row 19.**
  - expectation changed: phase 1 row 7 B2 required every server row to be `markers_written`. A server whose rescan
    left our markers in place is correctly Up to date: the Embys, which phase 1 didn't have (they don't re-read a file
    whose mtime alone changed), and Plex when its own detection is off. B2 now requires Markers written where B1's
    rescan dropped ours and written or Up to date elsewhere, as A3 and C2 do, and every server, the Embys included,
    must serve the chapters.
  - Fix round 1 re-ran phase 1 row 7 with Plex's detection on, as row 19 runs it: B1's rescan dropped ours on Plex and
    both Jellyfins, and B2 wrote those three; the Embys were Up to date and served the chapters. Run with the lab's
    detection `never`, Plex kept its markers and reported Up to date.
  - expectation changed: phase 1 row 17 runs on S01E03 (`ROW17_EPISODE=3`). Row 9 sent S01E02's webhook minutes
    earlier, and the app drops a repeat of the same file for 600 s.
- **Rows 2, 3, 4 and 6** use controller notes 1 and 2 as written (R2, G3, R1, Emby per version).

### Notes

- **Rick and Morty credits changed since phase 1.** 8 of 11 are Needs review, against 2 of 11 in phase 1 run 2.
  - SkipDB now answers some episodes with the short end card after the post-credits scene, e.g. S01E03 1308–1315 s
    against IntroDB/TheIntroDB 1228 s. The sources disagree, so the files wait for review.
  - The intros are unaffected.
- **Preview jobs in row 4 failed in the lab.** This is lab configuration, not Intro & Credits.
  - Jellyfin and Emby write their previews next to the media, which is mounted read-only.
  - The two Plex libraries `configure` adds are off for previews.
  - So S02E02's webhook preview job and its 3 retries failed. Its Intro & Credits follow-up ran and completed.

### Open items for the owner

- **Row 20 (ledger L276):** done in Plex Web by the close-out; native Plex apps aren't testable here.
- **Native Emby apps (note 6):** Skip Intro on a TV or mobile client; not testable here. Emby needs Emby Premiere on
  the server to skip intros (close-out).
- **G3 and the harness gate (`evidence/eval/phase2-harness.md`, Task 15).**
  - Eval lists, 118 intro episodes: Plex's own markers 23 useful / 15 wrong. Season audio alone 91 / 13 / 14.
  - Shipped (G3 on), High and Medium: 0 useful / 0 wrong. The "Medium beats Plex" gate fails by construction; High
    passes.
  - With G3 off: 23 useful / 7 wrong (eval lists), 23 / 4 (full folder). The gate passes.
  - The owner kept G3 on (§14, 2026-09-15). The lab agrees: row 3 is Needs review where Plex and season audio agree.
- **Plex `final` flag (row 10).** After a longer version is deleted, a credits row could keep `final` false: the
  publisher left a row that already serves the wanted times alone, so the flag stored while S01E01 had its 130 s
  version stayed on the 120 s file. Fixed in the close-out.
- **Spec note: a new season's lone opener gets its previous-season hint one run late** when the previous season is
  fingerprinted in the same backfill (spec §14, 2026-09-15).
  - The hint uses cached fingerprints only, and nothing asks the opener again in that job. Its next run is due,
    because the signature includes whether the previous season has fingerprints.
  - Precision is unaffected; the effect is one run of delay.

## Phase 2 close-out (2026-09-15, lane `lane/p2-closeout` on 07eff93, image `b526c3fb8e0a`)

The owner checks (row 20 and Emby Premiere) and two Task 17 findings, turned into product, docs and lab changes. Lab:
`mlab-app` recreated on the close-out image; nothing else changed on the servers. Raw owner-check evidence is in
`results/owner-checks/` (git-ignored).

### Row 20: Plex Web shows and runs Skip Intro and Skip Credits — pass (Plex Web)

- **Setup.** Plex Web 4.160.0 served by the lab Plex, headless Chromium. Every request went through an allowlist (the
  lab server and plex.tv); no request reached the production server. The server has Plex Pass; the synth VP9/Opus files
  direct-played.
- **Synth Chapters S01E02 (our intro 17–47 s, credits 100–120 s, `final` true).**

  | Step | Observed |
  |---|---|
  | Played from 0 s | "Skip Intro" shown at 18.0 s |
  | Clicked "Skip Intro" at 19.75 s | `currentTime` 47.00 s (the marker's end), label "0:47 / 2:00" |
  | Seek to 96 s, played | "Skip Credits" shown at 101.0 s |
  | Clicked "Skip Credits" at 102.73 s | `currentTime` 120.00 s (the marker's end), then S01E03 started |

  Both buttons show about 1 s after the marker starts, and Plex Web hides each by itself about 10 s later. Plex Web's
  code seeks to `endTimeOffset` rounded to whole seconds.
- **Plex Pass per viewer.** Plex Web's intro skip checks the signed-in account's `intro-markers` feature, and credits
  skip on a library item checks `credits-markers`. Plex's docs say the same: the account playing needs Plex Pass (or
  must be in the Home of a Plex Pass admin). A viewer without it gets no skip buttons, even with our markers in place.
  Not tested with a second account.
- **`final` and non-final credits.** S01E01 (credits 100–120 s, non-final at the time) and S01E02 (`final` true) behave
  the same in Plex Web: the same "Skip Credits" times, the same 120.00 s target, no post-play screen, and the same
  watched time. Plex Web 4.160.0 never reads a marker's `final`. Plex's credits article says some apps minimise the
  player into the post-play screen at the final credits; that couldn't be checked here.
- **Native Plex apps (TV, mobile, HTPC):** not testable here.
- Screenshots: `../screenshots/phase2/row20-plex-web-skip-intro-shown.png`,
  `../screenshots/phase2/row20-plex-web-after-skip-intro.png` (burnt-in "Chapter 2 47s-100s", player "0:47 / 2:00"),
  `../screenshots/phase2/row20-plex-web-skip-credits-shown.png`.

### Emby needs Emby Premiere to skip intros (rows 6–9, note 6)

- **Web client code**, `videoosd.js`, the same on 4.10.0.40 and 4.9.1.90: Skip Intro validates the Premiere feature
  `dvr`, which Emby's licence server checks by server id. Without it the button shows only while a per-browser counter
  is under 5 (each episode adds 2), a click opens "Unlock Feature" and doesn't seek, and then the button stops showing.
  `CreditsStart` drives the "Up Next" overlay with no check; Emby's web player has no Skip Credits button.
- **API** (both lab Embys): `GET /Registrations/dvr` → `IsRegistered` false, `IsTrial` false.
- **Emby 4.10 web, Synth Chapters S01E03 (our intro 25–55 s):** with fresh browser storage, "Skip Intro" was visible at
  26.9 s; a click at 27.5 s opened "Unlock Feature" and the video was still at 30.1 s. With the counter preset to 5, no
  "Skip Intro" appeared inside the intro. Screenshot: `../screenshots/phase2/ownercheck-b-emby410-web-fresh-after-click.png`.
- **Docs:** Emby's Intro Skip article ("Requires … an Emby Premiere subscription") and Premiere Feature Matrix (Intro
  Skipping under "Server / All Apps").
- **Native Emby apps:** not testable here; Emby's docs put Intro Skip under Premiere for all apps.
- **Product:** the Emby Intro & Credits tab reads `GET /Registrations/dvr` (kept an hour per server URL) and, when
  `IsRegistered` is false, shows the amber **Emby Premiere** row "Viewers can't skip intros: this Emby server has no Emby
  Premiere key. Skip Credits (Up Next) still works." with an ⓘ. A failed read shows nothing and is kept 5 minutes.
  Lab: both Embys' status answers `intro_skip_registered: false`, Jellyfin's has no such field. Screenshot:
  `../screenshots/phase2/closeout-emby-tab-no-premiere.png`.

### Row 10 re-run: the Plex `final` flag

- **Product.** Rows and the `pv:credits` key that serve the wanted times with a stale `final` flag are now rewritten
  on a one-version item under "Use ours", never under "Keep Plex's" (spec §14, 2026-09-15 "Plex credits `final`
  flag").
- **Lab** (`./phase2_matrix.py run 10`, `MLAB_DIR` at the main checkout's lab folder): pass, 8 of 8 checks.
  - Before the row, Plex served S01E01's credits 100–120 s with `final` false (and no intro row). A forced job on
    S01E01 wrote Plex (`markers_written`, "2 marker(s)"): credits 100–120 s with `final` true, intro 10–40 s. Plex's
    database then held the credits row `98000–120000` with `pv:final` 1 and the part's `pv:credits` entry with
    `"final":true`.
  - Our rows dropped on Plex, Jellyfin 10.11 and Emby; one Check servers job listed S01E03 and S01E01 and wrote them
    back. Plex, Jellyfin and Emby serve exactly the markers from before the drop, Plex's `final` flag included. The
    second Check servers run listed 0 files.
  - The earlier run's result is kept in scratch; `results/p2-row-10.json` is this run's.

### Task 17 findings

- **A sibling changed on disk mid-job.** An episode whose season audio answer left out a sibling changed on disk now
  goes into the job's Season follow-up once the job has read that sibling again (spec §14, 2026-09-15). Unit-tested
  (`tests/markers/test_season_followups.py::TestAnEpisodeRunBeforeItsChangedSibling`); not re-run in the lab.
- **A lone opener's previous-season hint one run late:** recorded in spec §14 as a known limitation.
