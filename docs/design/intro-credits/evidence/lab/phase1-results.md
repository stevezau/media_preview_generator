# Phase 1 lab results

Lab servers on storage (read-only calls): Jellyfin 10.11 `mlab-jellyfin`, Jellyfin 12 `mlab-jf12`, Emby `mlab-emby`,
Plex `mlab-plex`. Real library mounted `:ro`.

## Final (final-2, bd9e561) (2026-09-19)

Image `media_preview_generator:final-2` (`sha256:7050dc5d1385…`, `GIT_SHA` bd9e561). Raw data in `results/final-2/`
and `results/scale/` (git-ignored); the 2026-09-14 scale run is kept in `results/scale-2026-09-14/`.

### Matrix: 16 of 16

The phase 1 rows (14, 1, 2, 3, 13, 4, 6, 5, 7, 8, 9, 10, 16, 18, 19, 17) pass as phase 2 row 19, on a fresh config
(see `phase2-results.md`, "Final (final-2, bd9e561)").
- Row 1: job `df0ad357`, 13 written and 1 Needs review, in 49.2 s.
- Row 10: at the pause 1 file was done and 2 were on workers; 3 once the workers were idle, and still 3 after the
  preview job ran.

### Scale run on the real library

**Robust. Accuracy held except where TheIntroDB's daily budget ran out. Most server-marker evidence was this app's own
earlier markers.** Four things differ from 2026-09-14:
- **Lab Emby 4.10 now takes part.** Its Intro & Credits is on (phase 2), but the scale folders aren't in its library.
  - It waits on 685 files: "Not in this server's library yet". That makes 432 file outcomes read Waiting.
  - Each scale job then queued three retry jobs (1, 2 and 5 min later, 09:58–10:07 UTC), which then stopped.
  - The Plex and Jellyfin rows are unaffected.
- **TheIntroDB ran out.** The lab's earlier runs today had used most of its daily budget, so 206 files were checked
  without it. The job now says so (2026-09-14 finding 4 is fixed): "TheIntroDB's daily lookup limit was reached: 206
  files were checked without it."
- **The app's own earlier markers counted as the servers' own.** On a fresh config, markers the 2026-09-14 run and
  today's rows wrote on the lab Plex and Emby read as "markers already on a server" (finding 2).
- **711 real files, not 715.** The host folder of Indian Matchmaking S03 is now empty (4 files).

**Setup:**
- Fresh `mlab_app_config` on final-2 with `MLAB_APP_GPU=nvidia`, then `./phase3_matrix.py configure`.
- One GPU worker (Quadro P5000) and one CPU worker; High; TheIntroDB without a key.
- Lab Plex quiet prefs as before; a Plex rescan of tv and movies first.
- Then `./phase1_matrix.py scale job backfill`, `scale collect`, `scale served`, `scale writes backfill`,
  `scale job second`, `scale writes second`.
- Then `./scale_score.py score`, `./scale_score.py compare backfill results/scale-2026-09-14/collected-backfill.json`
  and `./scale_score.py frames backfill`.

**Robustness:**
- **The backfill:** job `681b9db5` over all libraries, low priority, 726 files in 34.4 min (2.84 s per file).
  2026-09-14: 722 files in 5 min 47 s (0.48 s per file), with no season audio or credit text.
- **Outcomes:** 25 Markers written, 244 Needs review, 1 No markers found, 432 Waiting (Emby, above), 24 Failed
  (finding 1).
- **Server rows:**

  | Server | Written | Up to date | Needs review | None | Waiting | Failed |
  |---|---|---|---|---|---|---|
  | Lab Plex | 170 | 408 | 116 | 5 | 1 | 24 |
  | Lab Jellyfin 10.11 and 12.0 (each) | 603 | 0 | 117 | 6 | 0 | 0 |
  | Lab Emby 4.10 | 31 | 0 | 9 | 1 | 685 | 0 |
  | Lab Emby 4.9 (synth only) | 6 | 0 | 7 | 0 | 0 | 0 |

- **Warnings:** 2. For two Jujutsu Kaisen S02 episodes (10-bit x264): "GPU Worker 1 (Quadro P5000) couldn't process …
  on the GPU and is retrying on CPU. Reason: ffmpeg exited 218". Both finished on the CPU.
- **Peaks** (95 `docker stats` samples):
  - `mlab-app`: 593 % CPU and 962 MiB.
  - Jellyfin 10.11: 105 % and 295 MiB. Jellyfin 12.0: 27 % and 364 MiB. Plex: 23 % and 114 MiB.
  - Host load 4.3–18.3.
- **Lookups:** SkipDB 716 and IntroDB 603, all HTTP 200. TheIntroDB: 57 HTTP 200 and 138 HTTP 404, then 100 left and
  the low-priority reserve held.
- **Served = decided:**
  - Both Jellyfins serve exactly the decisions on 724 of 726 files.
  - Plex serves the decided intro and credits on every written or up-to-date row.
  - The mismatches are the 24 failed Plex parts, plus 2 Needs-review files that both Jellyfins still serve from before
    this run: Mr. Robot S04E01's 9:46–11:14 "Intro" chapter (decided on 2026-09-14) and Synth Show S01E01's markers.
- **Second backfill:** `d1b9c0e8`, low priority, 24.4 s.
  - 26 Up to date (the 25 written plus one that had been waiting), 431 Waiting, the rest as in the backfill.
  - No Plex rows or part `extra_data` changed, no plugin marker file changed, and no online request was sent.

**Decisions and sources, against 2026-09-14** (all files, synth included):

| | 2026-09-14 | final-2 |
|---|---|---|
| Intro decided / Needs review / none | 279 / 94 / 247 | 331 / 127 / 162 |
| Credits decided / Needs review / none | 500 / 92 / 130 | 523 / 162 / 41 |
| Intro answered by: chapters, IntroDB, TheIntroDB, SkipDB | 256, 230, 102, 78 | 254, 242, 37, 78 |
| Intro answered by: season audio, server markers | —, 23 | 162, 286 |
| Credits answered by: chapters, IntroDB, TheIntroDB, SkipDB | 480, 208, 80, 126 | 476, 220, 33, 126 |
| Credits answered by: credit text, server markers | —, 26 | 168, 501 |
| Intro decided with season audio / credits decided with credit text | — | 59 / 40 |
| Decided with chapters (intro / credits) | 249 / 472 | 245 / 468 |
| Decided by chapters alone (intro / credits) | 247 / 466 | 208 / 406 |

The drop in "chapters alone" is mostly the same chapter decisions, now also confirmed by server markers.

**Accuracy against the same truth** (711 real files; 3 s bar, as on 2026-09-14):

| | 2026-09-14 right / wrong / none | final-2 right / wrong / none |
|---|---|---|
| Intro (262 episodes with truth) | 255 / 0 / 7 | 246 / **7** / 9 |
| Credits (with truth) | 468 / 8 / 26 | 468 / 6 / 24 |

- **The 7 wrong intros are Rick and Morty S01** (E03, E04, E05, E07, E08, E09, E11). Each ends 3.2–4.3 s after the
  truth, so it skips story.
  - On 2026-09-14 TheIntroDB answered all of S01, and its end, first in source order, was right.
  - Today it wasn't asked (budget). The checked edge came from IntroDB (0:33 on E04), with SkipDB (0:29.8) and the lab
    Plex's markers agreeing inside the 10 s tolerance.
  - That is the rule as written (the checked edge comes from the best-ranked confirming source). The cause is the
    missing TheIntroDB answer, not a code change.
- **Credits:** Innerspace is still the one early start (2026-09-14 finding 2). Late starts went from 7 to 5.
- **Without truth:** credits markers 19 → 43, intro markers 19 → 75 (season audio and credit text).
- **Fixed since 2026-09-14 (finding 1 there):** the cold-open "Intro" chapters of Reservation Dogs S01E05 and E06, and
  Mr. Robot S04E01's, are no longer published: "Intro chapter is much longer than the rest of the season's".
  - The scorer's replay of `decide()` on the stored evidence has no season context and still publishes them.
  - Those are the 3 replay differences.

**Markers that skip more than on 2026-09-14, frame-checked** (`./scale_score.py compare`, then `frames`: frames at
−6, −2, +2 and +6 s around each start, and around each end more than 2 s before the file's end):
- **105 decided markers:** 89 new, 8 that start more than 3 s earlier, and 8 intros that end more than 3 s later.
  - 55 intros from season audio with an online source.
  - 35 credits from credit text with an online source or server markers.
  - 8 Rick and Morty intros whose end now comes from IntroDB (S01E01, E03, E05, E08, E11; S02E04, E05, E07).
  - 7 others: Severance S01E01, E06 and E07 credits, Ted Lasso S03E07 and Rick and Morty S02E10 intros, and 2 synth.
- **102 images**; the 3 synth files have no host path.
  - The Watcher S01E01's image has no end row. Its video stream stops at 47:06.6, 85 s before its audio, so the +6 s
    frame doesn't exist. `index.json` records the rows each image shows.
- **Right or safe on 93 images.** Starts fall on the change into the intro or credits, and ends on the change back.
  Where an end is off, it ends early, inside the intro or credits, which skips less.
  - Rick and Morty S02E04, E05, E07 and E10 show the title card at end − 2 s and story at + 2 s. A second pass at
    0.5 s steps puts each end within 0.5 s of the cut. 2026-09-14's ends there were 3.6–4.2 s early.
- **Skip story at the end, 5 Rick and Morty S01 intros** (E01, E03, E05, E08, E11): IntroDB's end, 2.9–4.3 s after
  the truth (E01's 2.9 s is inside the 3 s bar). Same cause as the 7 wrong intros above.
- **Skip story at the start, 4 files:**
  - **Severance S01E01, E06 and E07 credits** start 4.8–7.1 s earlier than on 2026-09-14.
    - At start + 2 s, the last shot is still on screen. E01 and E06 show "Directed by" at + 6 s; E07 is black by then.
    - On 2026-09-14 TheIntroDB's later start was used. Without it, IntroDB's start is. Same cause as the Rick and
      Morty intros.
  - **The Office S02E14 credits:** decided at 20:28.99 from SkipDB and credit text; SkipDB is first in source order.
    - The car's last shot is still on screen at 20:35. Credit text and IntroDB both start at 20:38.
    - IntroDB's candidate ends at 21:20, 9 s past the file's end, so it fails sanity ("ends past the end of the file")
      and doesn't take part.
    - This is 2026-09-14 finding 3 (SkipDB's The Office starts are 10–15 s early). It now publishes at High, because
      credit text agrees inside the 10 s tolerance.

### Findings

1. **24 Plex parts refused: "Plex media_parts.extra_data is not JSON".**
   - Scope: Avenue 5 S02E04–E06, Mr. Robot S04 (5), Mushoku Tensei (7), SPY x FAMILY (8) and The Intern.
   - What changed: their `extra_data` is now URL-encoded (`ma%3Acontainer=mkv&…&pv%3Acredits=…`), not JSON. On
     2026-09-14 the same parts were JSON, and the publisher wrote them.
   - The only content change is `pv:credits` gaining `"final":1`. This app writes `"final":true`, so the change
     isn't this app's.
   - The publisher refuses the part as unsupported, and that is safe: nothing is written, and the row reads Failed.
   - Who rewrote them: **not established.** Running Plex's own credits detection on another of our parts (Mr. Robot
     S04E02) left its `extra_data` JSON.
   - If Plex itself writes this form, real servers would refuse those parts too. Next steps: read a real server's
     `media_parts` for URL-form `extra_data` (read-only, owner's call), or catch the writer in the lab.
2. **After a fresh config, this app's own earlier Plex and Emby markers count as the server's own.**
   - Server markers answered on 501 credits and 286 intro files (2026-09-14: 26 and 23).
   - Most of it is our own earlier markers. For example, Rick and Morty S01E04's Plex and Emby intro, 0:01.5–0:31.64,
     is exactly what the 2026-09-14 run decided.
   - Jellyfin's are told apart through the Bridge's store. Plex's and Emby's are told apart only through
     `markers.db`'s publish records, which a fresh config doesn't have.
   - Replaying the stored evidence without server markers:
     - 6 credits are decided only because of them (credit text plus server markers);
     - they shorten 57 credits and 36 intros;
     - they never set the checked edge (the intro end or the credits start).
   - Spec §5.5 rule 7 says "never ours", but here "ours" can't be seen. This affects any reinstall or lost
     `markers.db` while servers keep markers the app wrote.
3. **The Office S02E14 credits start 9 s early at High** (above): credit text agreement lets SkipDB's early start
   through.
4. **TheIntroDB's budget shapes accuracy.** Without it, IntroDB's intro ends (Rick and Morty S01) and credits starts
   (Severance) are 3–7 s off, in the direction that skips story. They still agree with SkipDB inside the tolerances.
   Where TheIntroDB's own ends were early (Rick and Morty S02), IntroDB's are right.

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

**18 pass, 0 fail.** Row 11 is covered by unit tests only. Row 12 passed on a later re-check (see its row).

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
| 12 Plex app shows Skip Intro | pass | Re-checked 2026-09-14 in lab Plex's own web player (Playwright, signed in with the lab's claim account): Synth Chapters S01E02 (served intro 17–47 s, credits 100–120 s) shows **Skip Intro** at 0:18.4 and **Skip Credits** at 1:41.5. Screenshots: `../screenshots/phase1/plex-web-skip-intro.png`, `plex-web-skip-credits.png`. |
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

## Task 20 Step 4 — scale run on the real library (2026-09-14, image `pr-241` `sha256:b252be76…`)

Lab only (no side-by-side on the prod `plex` host). Prod Plex was read once, read-only
(`sqlite3 "file:<db>?mode=ro"` over ssh), for its own markers. Raw per-file data is in `lab/results/scale/`
(git-ignored).

### What was mounted

`./scale_score.py pick` chose the folders and wrote them to the git-ignored `scale_mounts.sh`, and `up.sh` `MV_SCALE` mounts them `:ro` into Plex and both Jellyfins (not
Emby). They were picked from `eval/named_seasons.json` and `credits/movie_credit_truth.json`, preferring items prod Plex
has its own markers for.

- **TV:** 613 episodes: 80 new season folders from 77 shows, plus the older Rick and Morty S01 and South Park S01.
  - 262 have intro truth and 402 credits truth.
  - 137 have no chapters, 107 are 4K (83 HDR/DV), 41 are mp4.
  - 11 are Specials (season 0) and 10 are date-named.
  - Anime with OP/ED chapters, and cold opens.
  - Long shows: The Simpsons S03+S09, How I Met Your Mother S04+S07+S08.
  - Shows without a tvdb id in the path: Rick and Morty and South Park, as up.sh mounts them. No real show folder
    lacks one.
- **Movies:** 102 movies, 77 of them 4K (71 HDR/DV) and one mp4.
  - 100 have credits truth, 3 of them corrected by frame checks (`credits/adjudicated.json`).
  - 102 extras: 72 `-trailer` files and 30 in `Trailers/` subfolders.
  - The library has no multi-version movies or episodes in one folder; row 8 covers those with synth files.

Lab settings for the run:
- **Lab Plex:** intro, credits, chapter-thumbnail and loudness analysis set to `never`, and the deep/upgrade media
  analysis butler tasks off. The earlier values are in `results/scale/lab_plex_prefs_before.json`; they are left off
  while the scale folders stay mounted.
- **Jellyfin 12.0:** got a `movies` library with 10.11's options (no trickplay or chapter images).
- **Scans:** Plex took 4 min, Jellyfin 10.11 25 min, Jellyfin 12.0 28 min. All three list the same 715 files.
- **App:** 1 CPU worker, High, TheIntroDB without a key, and 435 of 500 TheIntroDB lookups left.

Runner: `./phase1_matrix.py scale quiet | scan <server> | job <name> | collect <name> | served <name> | writes <name>`,
then `./scale_score.py truth | score`.

### 1. Robustness: pass

- **The backfill:** job `b6f7df77` over all libraries, low priority, 722 files. It completed in 5 min 47 s with no
  warning or error.
- **Outcomes:** 479 Markers written, 20 Up to date, 133 Needs review and 90 No markers found. No file was Failed,
  Waiting or Skipped, and no retry or verify job was queued.
- **Needs review, by marker:**
  - 136 had only one source ("don't agree yet").
  - 37 had sources that disagree.
  - 9 were chapters vetoed by two agreeing sources.
  - 4 were agreeing sources that conflict.
- **Server rows** (written / up to date / needs review / none):

  | Server | Written | Up to date | Needs review | None |
  |---|---|---|---|---|
  | Plex | 522 | 26 | 83 | 89 |
  | Jellyfin 10.11 | 521 | 27 | 84 | 90 |
  | Jellyfin 12.0 | 522 | 26 | 84 | 90 |

  Two synth files aren't in any Plex library.
- **Resources:** from 18 `docker stats` samples.
  - mlab-app peaked at 38% CPU, 239 MiB and 30 PIDs.
  - Jellyfin 10.11 peaked at 71% and 1.6 GiB; Jellyfin 12.0 at 29% and 1.7 GiB; Plex at 16% and 515 MiB.
  - Host load was 18.6–23.2, from other services and a ZFS scrub.
- **Online lookups:** TheIntroDB 335 (101 HTTP 200, 234 HTTP 404), IntroDB 577 and SkipDB 689, all HTTP 200. There were
  no 429s and no errors.
- **Folder job on `/media/movies`** (`1264c323`): 204 files. The 102 movies were Up to date. All 102 extras were Skipped
  ("Extras aren't checked for markers"), including those in `Trailers/`. No retries.

### 2. Served = decided: pass

- **Plex** (`includeMarkers=1`): all 279 decided intros and 500 decided credits are served exactly as decided. Credits
  are stored 2 s earlier, as in row 2.
  - Undecided types serve nothing, except Plex's own older rows on Rick and Morty and South Park (4 intro, 6 credits).
- **Both Jellyfins** (`/MediaSegments`): 721 of 722 files have ticks equal to the decisions on 10.11 and on 12.0.
  - The one left is Synth Show S01E01. It carries the plugin marker file an earlier lab test wrote (see the run 2
    notes); the app didn't write it.

### 3. Accuracy against truth (High)

Truth is studio chapters (the `run_eval_v3.py` name rules), `movie_credit_truth.json` (with `adjudicated.json`
applied) and `online/cases.json`.

- A marker is **wrong** when it is more than 3 s off at the boundary that matters (intro end, credits start), or when
  it skips story (an intro starting more than 3 s early, or credits running into a post-credits scene).
- At High, chapters publish alone, so on chapter files ours is the chapter. Frame checks caught what chapter truth
  can't.

| | Right | Wrong | None |
|---|---|---|---|
| Intro (262 episodes) | 255 | 0 | 7 |
| Credits (502 files) | 468 | 8 (1 skips story, 7 late) | 26 |

- **Where the markers came from:**
  - Chapters: 464 of the 468 right credits and 244 of the 255 right intros.
  - Online agreement: 4 right credits, 5 late credits, and 11 right intros.
- **After frame checks** (`results/scale/frames/`):
  - Intros: 253 right, 2 wrong.
  - Credits: 467 right and 2 wrong. Innerspace, which the adjudicated truth already counted, and Avatar, which chapter
    truth had counted right.
  - The other 7 credits are late only: they show some credits and skip no story.
- **Same stored evidence, re-decided with the app's own `decide()`:**
  - Replaying High reproduces every decision the job made.
  - **Medium** gives the same result on every truth file, but adds 30 markers on files without truth (25 credits, 5
    intros). All are from SkipDB alone.
  - **High without chapters**, as a view of files that have none: credits 10 right, 14 off (4 early); intros 17 right,
    8 off. Chapter truth itself is often late for credits (see 4), so read these as disagreements, not errors.
- **Coverage over all 715 real files:**

  | Marker | Ours | Prod Plex |
  |---|---|---|
  | Credits | 495 | 610 |
  | Intro (613 episodes) | 274 | 197 |

  On the 213 credits files without truth, ours has 19 and Plex has 181. This is the phase 1 gap until the detectors
  land. Shows with no online data and no chapters get nothing: Cowboy Bebop (2021), Planet Earth II, most Doctor Who
  specials.

### 4. Against prod Plex's own detection (same files, same truth)

- **Plex's markers are compared as served:** credits start at stored + 2 s, and non-final credits end at stored − 2 s.
- **Chapter truth for credits is often late:** it marks the credit roll, not credits over footage.
  - Frame-checked sample of 8 files where Plex starts more than 3 s before the chapter: in 5, Plex was right (Wild Wild
    Punjab, Ninja Kamui, Louis C.K., Fauda, Caught Stealing). In 3, Plex skipped 8–52 s of story (How I Met Your
    Mother, Legion, Moon Knight).
  - Sample of 9 files where our chapter credits start more than 8 s before Plex's: 7 were ours right and Plex late
    (Heeramandi 331 s, Den of Thieves 2, Oldboy, Kill Bill Vol. 2, South Park, Record of Ragnarok, The Simpsons). One
    is ours wrong (Avatar) and one is unclear (Attention Attention).

| Credits (497 truth files in prod Plex) | Count |
|---|---|
| Both right | 166 |
| Ours right, Plex not | 302 |
| Plex right, ours not | 9 |
| Neither | 25 |
| Both have a marker / ours only / Plex only / neither | 414 / 62 / 15 / 11 |
| Right within 5 s / 10 s: ours | 471 / 472 |
| Right within 5 s / 10 s: Plex | 236 / 301 |

| Intro (262 truth episodes) | Count |
|---|---|
| Both right | 39 |
| Ours right, Plex not | 216 |
| Plex right, ours not | 0 |
| Neither | 7 |
| Both have a marker / ours only / Plex only / neither | 106 / 149 / 6 / 1 |
| Right within 5 s / 10 s: ours | 255 / 255 |
| Right within 5 s / 10 s: Plex | 74 / 87 |

- **Plex's intro misses:** 150 have none. 40 start more than 3 s early (most by 3.0–3.5 s), 21 end early and 12 end
  late.
- **Where ours differs from Plex with no truth, frame-checked:**
  - South Park S01E04 and S01E05: ours (0:01–0:35) is the theme song. Prod Plex's (1:28–2:19) is story.
  - Ted Lasso S03E05 and South Park S01E04 credits: ours starts at the credits; Plex starts 15 s and 11 s late.
  - Severance S01E05 credits: ours (introdb + skipdb) starts up to 3.5 s into the last shot.

### 5. Second backfill: pass

Job `67a6af80`, normal (not forced), took 14.6 s:

- **Outcomes:** 499 Up to date, 133 Needs review, 90 No markers found, none written.
- **No writes:** no Plex marker rows added, removed or changed, and no part `extra_data` changed. Plugin marker files
  are unchanged on both Jellyfins.
- **No online requests:** the usage counters didn't move (TheIntroDB 362, IntroDB 603, SkipDB 716).
- **Caveat:** 43 TheIntroDB lookups were refused inside the app, because the low-priority budget was still used up (see
  findings). After the UTC day changes, the next normal job sends up to that many.

### Findings (wrong markers first)

1. **Studio "Intro" chapters that are really cold opens publish as intros.**
   - Reservation Dogs S01E05 publishes intro 0:09–2:15 and S01E06 0:09–1:36. The frames show story (road, car, woods,
     police car).
   - In S01E01, E04 and E08 of the same season, the "Intro" chapter is the 3–10 s title card, and that is right.
   - No other source had an intro for these files.
   - Cause: `sources/chapters.py` trusts a generic "Intro" chapter unless the file also has a specific opening chapter.
     At High, chapters publish alone (spec §5.5 rule 3). The sanity bounds (at most 300 s, starting before 35%) let
     86–126 s through.
2. **Movie "End Credits" chapters placed on the last story shot.**
   - Avatar's chapter starts 18 s early, and the skip covers the final shot of the film. Innerspace's starts 20 s
     early, on a story shot.
   - Both were published from chapters alone. Prod Plex is right on both (within 6 s).
   - Cause: the same rule as finding 1 (chapters publish alone); the chapter data itself is wrong.
3. **Medium would publish lone SkipDB answers that skip story** (offline replay; not published in the lab).
   - It adds 25 credits markers, and all 25 start more than 3 s before Plex's.
   - Frame-checked:
     - Battlestar Galactica S04E02 credits at 38:40 (4.4 min early).
     - S04E05 credits at 36:26 (6.7 min early).
     - The Office S02 credits 12 s early, and the other 19 The Office episodes show the same 10–15 s offset.
   - SkipDB answered these as exact or shifted matches (conf 0.82–0.9), so the app accepted them.
   - At High, all 20 The Office files correctly stayed Needs review.
4. **The TheIntroDB daily budget runs out silently during a big backfill.**
   - Low priority stopped at the 20% reserve (100 of 500 left). The last 39 files got `budget_exhausted` instead of an
     HTTP request.
   - Of those 39: 21 were written anyway from chapters, 14 were No markers found and 4 Needs review.
   - The job has no warning and the files' reasons don't say why; only a DEBUG log line records it.
   - They are asked again on a later run, by design.
5. **Credits that are only the end card after a post-credits scene** (harmless, but they miss the main credits).
   - Rick and Morty S01E04: 20:56–21:05 (introdb + skipdb). S02E05: 22:58–23:02 (theintrodb + skipdb).
   - Plex has the main credits before the scene.
6. **Coverage by design:**
   - "Ending" isn't a credits name, so Food Wars! has no credits on 7 files and Chainsaw Man goes to Needs review.
   - "Intro Start" / "Credit Start" and French names (Générique, Crédits) aren't matched.
   - Specials get no TheIntroDB or IntroDB lookup (season 0).

No crashes, stuck jobs, retry storms or failed files; served always equals decided; the second run writes nothing.
