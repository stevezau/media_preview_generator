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
