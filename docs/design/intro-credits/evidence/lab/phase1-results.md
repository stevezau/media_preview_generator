# Phase 1 lab results

Lab servers on storage (read-only calls): Jellyfin 10.11 `mlab-jellyfin`, Jellyfin 12 `mlab-jf12`, Emby `mlab-emby`,
Plex `mlab-plex`. Real library mounted `:ro`.

## Task 6 — `get_external_ids` (2026-09-14, staged code before fix round 1)

| Server | Item | Returned | Result |
|---|---|---|---|
| Jellyfin 10.11 (API key and user login) | Rick and Morty S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` | pass |
| Jellyfin 10.11 (both) | Toy Story | `{kind: movie, tmdb: 862, imdb: tt0114709, tvdb: None}` | pass |
| Jellyfin 12 (both) | Rick and Morty S01E01 | same as 10.11 | pass |
| Emby (both) | Rick and Morty S01E01 | `{kind: episode, ids: None, season: 1, episode: 1}` | ids not confirmable: lab Emby has empty ProviderIds |
| Emby (both) | Toy Story | `{kind: movie, imdb: tt0114709}` | pass (Emby only has the imdb from the file name) |
| Plex | Rick and Morty S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` via `/library/metadata/3?includeGuids=1` then the show | pass |
| Plex | Toy Story / Up | tmdb 862 / 14160, imdb ok, tvdb 318 / 315 | pass; tvdb movie ids dropped in fix round 1 |

Found by the same check: when the show lookup failed, episodes fell back to their own episode ids (Jellyfin:
tvdb 4711142; Plex: tmdb 973400) — fixed in Task 6 fix round 1 (episodes use show ids only).

## Task 6 — after fix round 1 (2026-09-14)

| Server | Item | Returned | Result |
|---|---|---|---|
| Jellyfin 10.11 and 12 (API key and user login) | Rick and Morty S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` | pass |
| Jellyfin 10.11 (both) | Toy Story | `{kind: movie, tmdb: 862, imdb: tt0114709, tvdb: None}` | pass |
| Jellyfin, Plex | the show/series item itself | `{kind: unknown, all ids None}` | pass |
| Emby (both) | S01E01 / Toy Story | episode with no ids (lab Emby has no ProviderIds) / movie imdb tt0114709 | pass |
| Plex | S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` | pass |
| Plex | Toy Story / Up | tmdb 862 / 14160, imdb ok, tvdb None | pass |
| Jellyfin 10.11 series fetch timing out; Plex empty show | S01E01 | `{kind: episode, ids None, season: 1, episode: 1}` (no fallback to episode ids) | pass |

Real library, read-only, 271,394 video files: TV episodes with ids 221,972 (4-digit seasons like `S2023Exx` now parse);
unknown 9,866 (9,638 extras + 228 date-named episodes left to the server). Movies with ids 19,102; the 16,627 that
became unknown are all `-trailer` files. No movie returns a tvdb id. All 4,911 TV show folders carry `{tvdb-…}` only.

## Task 7 — online sources live smoke (2026-09-14, storage egress, one request per source)

`pytest --no-cov -n 0 -s -m integration tests/markers/test_online_sources_live.py` — 3 passed, exactly 3 requests.
Item: Rick and Morty S01E01 (tmdb 60625, imdb tt2861424, tvdb 275274), file duration given to TheIntroDB.

| Source | HTTP | Candidates (ms) | Rate/usage headers |
|---|---|---|---|
| TheIntroDB (no key) | 200 | intro 127 894–156 824, credits 1 298 000–end | `x-ratelimit-limit 30`, `-remaining 29`, `-reset 10`; `x-usagelimit-limit 500`, `-remaining 173`, `-reset 0`; `x-usagelimit-specificmedia-limit 2000`, `-remaining 1997`, `-reset 0` |
| IntroDB.app | 200 | intro 128 000–160 000, credits 1 295 000–1 321 000 | none |
| SkipDB | 200 | intro 129 000–157 800 (conf 0.91), credits 1 296 000–1 320 000 (conf 0.9) | none |

Findings:
- TheIntroDB sends `x-usagelimit-reset: 0` mid-day, so trusting it as "seconds until reset" would refill the budget
  on every response. The limiter ends the daily budget at the UTC day change only
  (`test_live_theintrodb_headers_keep_the_budget_until_the_day_rolls` pins the captured header set).
- The keyless daily budget was already at 173/500 at 05:54 UTC on storage's IP; the pipeline shares that allowance.
- IntroDB.app and SkipDB send no rate headers; they are paced by the 0.5 s floor plus 429 handling.
- The three sources agree within 1.2 s on the intro start and 3 s on the credits start for this item.
- The TheIntroDB keyed limit (spec §13 item 9) is still unverified: no key available.

## Task 19 — end-to-end lab matrix, run 2 (2026-09-14, branch `2073f3d`)

**17 pass, 0 fail.** Row 11 is covered by unit tests only, and row 12 is waiting for the owner.

Setup:
- **App:** `mlab-app`, image `671f3dc50d71` (`4.4.3.dev55`), with a fresh config volume. It is wired to lab Plex
  (claimed, Plex Pass), Jellyfin 10.11 and Jellyfin 12.0. Emby stays `needs_plugin`, so Intro & Credits is off there.
  TheIntroDB is enabled without a key.
- **Plugins:** both built from `2073f3d` (10.11 md5 `eef55cc1…`, 12.0 `6da427fd…`) and installed. The old plugin
  folders were backed up first.
- **Capability:**
  - Plex is `ready`: `plex_pass: true`, ext4, lock holder, detection `asap`.
  - Both Jellyfins are `ready`.

Raw evidence is in `lab/results/row-NN.json` (git-ignored; run 1's copies are in `results/run1/`). The runner is
`./phase1_matrix.py run <rows>`. Rows ran in this order: 14, 1, 15, 2, 3, 13, 4, 6, 5, 7, 8, 9, 10, 11, 12, 16, 18,
19, 17.

| Row | Result | Evidence |
|---|---|---|
| 1 Backfill: Synth Chapters + Rick and Morty S01 | pass | Job `d7a7025e`, 6.8 s. 12 Markers written and 2 Needs review (S01E02 and S01E09 credits). All 14 files written on each of Plex, Jellyfin 10.11 and Jellyfin 12.0. 11 lookups per online source. |
| 2 Plex `includeMarkers=1` | pass | Synth intros served exactly as the chapters. Credits served at 100000–120000 and stored at 98000 (the −2 s shift). Rick and Morty served = decisions. The two credits in review keep Plex's own rows. |
| 3 Jellyfin `/MediaSegments` | pass | 14 of 14 files: 10.11 and 12.0 ticks equal the decisions × 10,000. |
| 4 Second backfill | pass | Job `495b584f`: 12 Up to date, 2 Needs review. Every server row Up to date. Plex taggings ids (29 rows) and parts `extra_data` unchanged. Plugin marker files unchanged. 0 online lookups. |
| 5 Plex wipe matrix | pass | Synth S01E03 keeps our markers through forced metadata refresh, section scan, analyze with detection set to never, non-forced detection, and forced credits detection (Plex's detector fails on the synth files). Rick and Morty S01E01: Plex's forced credits detection replaced ours (1298000–1320000 → 1297324–1321472). The next normal job wrote ours back: Plex row `markers_written`. |
| 6 Jellyfin wipe matrix | pass | Both servers kept every segment through Scan Media Library, ReplaceAllMetadata refresh, the Media Segment Scan task, and a container restart. |
| 7 File change | pass | A: touch E02, job re-probes; servers already show ours, so Up to date. Rescan drops them (Jellyfins: all segments; Plex: intro only). The next normal job writes them back on all 3 (`markers_written`). B: touch, rescan, then job: written on all 3. C: touch, rescan, forced job: the restore reports `markers_written` on all 3. |
| 8 Multi-version | pass | A: E01 + Extended (+10 s). B: E03 + an identical copy, starting with a forced job. In both, the original alone gives Plex `markers_waiting` (file outcome Waiting) and Plex serves nothing. Deciding the second version writes the same `pv:intros`/`pv:credits` to both parts, and Plex serves the chapters (credits included). Jellyfin 12.0 wrote both second versions. |
| 9 Webhook | pass | Sonarr E02 → preview job `d51b44fa` at HIGH (1) → Intro & Credits job `d6e92129` at NORMAL (2), `follows_job_id` = `d51b44fa`, started after the preview job finished. The dashboard lists it directly under the preview row ("follows d51b44fa"). |
| 10 Per-job pause | pass | Forced South Park S01 job `0c1da817`: pause at 1/13. The 8 files already in flight finished (soft pause), then it held at 9/13. Meanwhile preview job `de3ffeaf` (HIGH) and its follow-up `6586c230` completed. Resume → 13/13. |
| 11 Plex DB on a network share | not lab-tested | Unit tests (`test_fs.py`, `test_plex_db_publisher.py -k network/mount/local_db/lock_holder`): 42 passed. |
| 12 Plex app shows Skip Intro | pending owner | Play Synth Chapters S01E02 or S01E03 in a Plex app. Skip Intro should appear at 0:17 or 0:25, Skip Credits at 1:40. |
| 13 Jellyfin web Skip Intro | pass | Synth S01E01 on 10.11 and 12.0: "Skip Intro" at 0:22.9. |
| 14 Security | pass | All 5 `/api/markers/*` routes return 401 without auth. `item?path=` traversal (3 forms) and `redetect` of `/etc/passwd` return 400. |
| 15 Resources | pass | During row 1: peak CPU 86%, 99 MiB. TheIntroDB: 11 lookups, 11 at most in any 10 s (limit 30). |
| 16 Plex re-detection: restore vs Keep Plex's | pass | Rick and Morty S01E01. Restore: forced credits detection, then a normal job → `markers_written`, ours back. `keep_plex`: forced detection, then a normal job → Plex's credits stay. Inspector plan is `keeps_plex`, "Keeping Plex's credits". Job log: "item 3 shows Plex's own credits instead of ours; keeping them (Keep Plex's)". A second normal job and a forced job leave them alone (credits taggings row ids unchanged). Back to restore → `markers_written`. |
| 17 Delayed verify job | pass | Touch E02, then a Sonarr webhook. The follow-up job logs "1 replaced file(s) are checked again in 600s". Verify job `2426a7af` ("Verify: Intro & Credits · Synth Chapters S01E02") shows "Checking again in 9 min" on the dashboard. Both Jellyfins rescanned and dropped the segments. At 600 s the verify job wrote them back: Jellyfin 10.11 and 12.0 `markers_written`, ticks = chapters. |
| 18 Extras | pass | Folder job on `Toy Story (1995)`: `Toy Story (1995)-trailer.mkv` → Skipped, "Extras aren't checked for markers". The movie itself → Markers written. No retry job. |
| 19 Jellyfin 12.0 alternate versions | pass | `… S01E01 - Extended` and `… S01E03 - Copy` are alternate versions of their episodes on 12.0, and both carry our Intro/Outro ticks. jellyfin-web 12.0 plays the Extended version (`mediaSourceId` = its id, duration 130.008 s) and shows "Skip Intro" at 0:22.9. |

### What changed since run 1

- **"Up to date" is now read back.** A normal job after Plex's forced detection (row 5), or after a server rescan of a
  changed file (row 7 A), writes our markers back. In run 1 it answered Up to date while they were gone.
- **Restores report "Markers written".** A forced run that restores markers now says so (row 7 C). In run 1 it said
  Up to date.
- **Replaced files get a delayed verify job** (new row 17).
- **"Keep Plex's" works per type** (new row 16).
- **Jellyfin 12.0 alternate versions get markers** (rows 8 and 19). In run 1 they waited `not_in_library` and started
  retry chains.
- **The file outcome shows the step still in progress:**
  - Needs review files stay Needs review (rows 1 and 4).
  - A Plex that is waiting makes the file Waiting (row 8).
- **Extras are skipped without a retry** (new row 18).
- **The Extended test file was re-encoded with a 10 s tail** (was 20 s), so both versions keep their credits (row 8).
- **Runner criteria updated for the new semantics:**
  - Row 4 accepts Needs review when a marker is still in review.
  - Row 7 A1 accepts Up to date when the servers already show ours.

  The row 4 job that failed the old criterion also made zero writes.

### Open item from run 2 — fixed

- **"Keeping Plex's credits" wasn't shown in the job's Files panel (row 16).** Only the Inspector and the job log
  showed it.
  - Cause: `JobManager.record_file_result` kept only `id/name/type/status/reason_code` for each server, so the
    server row's `message` (which carries the kept note) never reached the job's file results. For job `d64c6204` the
    Plex entry read just `markers_up_to_date`.
  - Fix (`5418883`): Intro & Credits jobs keep each server's message on its saved row, and the Files panel lists the
    ones that add something ("Lab Plex: Keeping Plex's credits"). Preview rows are unchanged.
  - Proof: row 16 re-run on the PR image `ghcr.io/stevezau/media_preview_generator:pr-241`
    (`sha256:b252be76…`, built from `f3113e8`) passes, with the new check "job Files panel says Keeping Plex's
    credits". Job `2ee04be0`: Plex row `markers_up_to_date`, message "Keeping Plex's credits".

### PR image check (2026-09-14)

- `pr-241` pulled; `import media_preview_generator.markers.pipeline` works; `mlab-app` recreated on it (volume kept).
- Capability: Lab Plex `ready` (written into the database), both Jellyfins `ready` (plugin), Emby `needs_plugin`.
- Screenshots for the PR are in `../screenshots/phase1/` (synth files only).

### Lab reset used before run 2

1. On the run 1 app, turn off intro and credits detection, then run a normal job over the synth show, Rick and Morty
   S01, South Park S01 and the movies. That removes only our markers: 29 files written. Plex's own rows stay, and so does the plugin
   marker file for Synth Show (2020) S01E01 (an earlier lab test in `/media/synth`).
2. Delete the extra versions from the synth season folder.
3. Rescan Plex, both Jellyfins and Emby.
4. Run Plex's non-forced credits detection on each episode and movie, and intro detection on both seasons. Plex's own
   markers come back (54 rows).
5. Remove `mlab-app` and `mlab_app_config`, then run `./app.sh` and `./phase1_matrix.py configure`.
6. Install plugins from the branch build (old folders backed up), and restart both Jellyfins.

Other notes:
- Lab Plex still serves a leftover per-user marker on Rick and Morty S01E07 (Sep 12 marker-API research). Row 2 records
  it separately.
- The Playwright sessions for the lab user were deleted from both Jellyfins after the web-player rows.
