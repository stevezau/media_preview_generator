# Intro & Credits (skip markers) — Design Spec

**Revision 3 (consolidated) · 2026-09-13 · Status: approved, build in progress on PR #241 (`feat/markers-detection`)**
Supersedes PR #241's spec (`feat/markers-detection`, `docs/design/2026-05-17-intro-credits-detection-design.md`).
Revision 2 is archived at `evidence/history/spec-rev2-2026-09-13.md`.

---

## 0. Start here (read this first after a `/clear`)

**What this is.** Skip Intro / Skip Credits markers for Plex, Emby and Jellyfin, detected once per media file and
published to every server that has the file. Feature name in the UI: **"Intro & Credits"**.

**Read in this order.**
1. This file, top to bottom. It is the source of truth. If code and spec disagree, stop and ask the owner.
2. The implementation plans next to this file (`plan-*.md`) — which task is next is the first unchecked box.
3. `evidence/README.md` — map of every script, truth set, prototype plugin, and the lab.
4. Design report with UX mockups (owner-approved look): artifact
   https://claude.ai/code/artifact/65394c1a-e878-4fc2-985b-63bc4c307c5d (source: `evidence/design/index.html`).
5. Memory notes: `intro-credits-markers-design`, `lab-servers-on-storage`, `design-doc-survives-clear`.

**Status (2026-09-20).** Phase 1 (§12: store, chapters/online detection, Intro & Credits job type, Plex + Jellyfin
publishers, per-server Edit tab, Settings section, Inspector tab, config migration, docs) is built, audited and
lab-proven: lab matrix 18/19 (row 11 unit-tested), `pr-241` image checked on the lab, and a scale run on 715 real files
with 0 failures (`evidence/lab/phase1-results.md`). Its findings are fixed in §5.5 rules 6–7 and a warning when an
online source's daily budget runs out (§14, 2026-09-14/15), or moved into phase 2 (the season chapter-intro check).
**Phase 2 is built, audited and lab-proven** (`plan-phase2.md`; ledger `.superpowers/sdd/plan-phase2/progress.md`):
season audio intros (fingerprint store, v3 matcher, season step with the silence guard and the season chapter-intro
check, Season follow-up jobs), the Emby Bridge plugin and Emby publisher, Plex version drift, the Check servers job
(§6.2 step 6), the Season view and its API, the Settings / Edit tab / Inspector UI, and the accuracy harness
(`evidence/eval/phase2-harness.md`: reproduces §5.3 exactly; the full report against Plex's own markers). Milestone
audit: 0 critical/high, 7 medium fixed. Lab matrix 23/23 (row 20 in Plex Web; native Plex and Emby apps not
testable), owner checks done (Plex Web skip buttons, Emby Premiere), and the `pr-241` image
(`sha256:3afed8e7124b6a2c465bcdf6c70f261c044b78ee18c29cbf642444d628908341`, from `8a8b92e`) re-ran rows 1, 2 and 8
on the lab (`evidence/lab/phase2-results.md` "PR image check"). Rulings R1–R5 and G3 are in §14. Still open with the
owner: the Emby catalog submission (roadmap checkpoint 4).
**Phase 3 (credits text) is built, audited and lab-proven** (`plan-phase3.md`; ledger
`.superpowers/sdd/plan-phase3/progress.md`):
keyframe-tail sampling, rule J's start and end, the ONNX Runtime WebGPU/CPU text detector with a per-device
self-test, the Settings row, the Inspector "Credit text" lane, and the accuracy harness
(`evidence/eval/phase3-harness.md`). Milestone audit found and fixed two real bugs (the end anchor, a stale
tooltip; §14 2026-09-18). The harness gate passes 5 of 5 on the 80 hand-checked files (movies40 + tv40) on both
decode paths and fails 3 of 5 on the harder 205-movie set — a disclosed detector-gap limitation, not fixed here
(§5.4, §13 item 14; owner, 2026-09-18): ships at "Medium" now, "High" needs a second source until that gap closes.
Rule J version 2 (final review, §14 2026-09-19/20) narrowed that gap (205 Medium useful 90 → 96, no early answer
added), fixed §13 item 13, stopped text that never leaves the screen reading as a roll, and reads before the tail for
a roll the tail cuts into; rule J alone now meets §5.4 on **both** decode paths (GPU 64 within 10 s, CPU 59, 1 early
each). The 205 still fails 3 of 5. Rulings T-R1–T-R9 and contradictions C1–C7 resolved while planning phase 3 are in
§14. **The whole PR was then reviewed in eight lanes** (security and web, jobs and workers, servers and publishers,
detection, harness, CI/Docker/docs, UI, cleanup); it also fixed the VP9 keyframe
pass (§5.4 Frames), the two bugs the final scale run exposed (§14 2026-09-19: Plex's own migration leaving parts the
publisher refused, fixed; and this app's earlier markers counting as a server's second opinion, fixed for Emby
through the Bridge store and still open for Plex, §13 item 17) and added the broadcast-TV limit (§13 item 15). Lab
matrix (`evidence/lab/phase3-results.md`): **16 of 16** rows pass on `final-2` (`bd9e561`) — row 14 now outright,
from the Intel GPU's own per-process render counters (NVIDIA publishes none; `nvidia-smi pmon` shows the absence) —
with phase 2 24 of 24 and phase 1's 16 re-run rows 16 of 16 on the same image, beside a final-2 scale run of 711 real
files (`evidence/lab/phase1-results.md` "Final (final-2, bd9e561)"; the 715-file run above is 2026-09-14's, and
final-2's 24 Failed Plex rows are finding 1, fixed and re-proven). The published `pr-241` image
(`sha256:813f67cb5550d1ce0abd564c95b88b42379dbdb02ae48fbc4e589f5c677a9042`, from `003a8d1`) re-ran rows 1, 2, 3, 16
and phase 2's row 24 on the lab, and an earlier build ran the detector on one real season on `plex` beside production
(§14, 2026-09-19): 10 of 11 starts within 10 s (E04 no answer), every answered episode's after-credits scene kept.
Tests: 11,429 unit/integration (88.84 %), 363 e2e, the CI integration selection; CI green on `003a8d1` including the
new arm64 image check. Next: owner review, then phase 4. Build runs on PR #241, branch
`feat/markers-detection`; spec, slimmed evidence and plans live in `docs/design/intro-credits/`. Local-only, gitignored
files stay beside them: `evidence/lab/env` (tokens), `evidence/lab/synth/` (webm), `evidence/lab/scale_mounts.sh` and
`evidence/lab/results/` (real library paths), `evidence/online/skipdb-dump.json`,
`evidence/plugins/emby-4.10/embylibs/`.

**Working rules (owner's, non-negotiable).**
- Prove server behaviour on the **lab servers on storage** (§10.3), never on the prod Plex on `plex`. Prod Plex DB:
  read-only queries only (`sqlite3 "file:<db>?mode=ro"`). The owner's two one-off exceptions (§14 2026-09-16 Q7,
  2026-09-19) don't extend to anything else: ask again, and remind the owner of this rule when asking.
- Never delete or write files under `/data*` (the owner's library, both hosts). Lab mounts are `:ro`.
- Commits on the feature branch need no per-commit ask (owner, 2026-09-13); run the `Architecture Review` agent on
  the staged diff first and block on HIGH; commit with `PATH="/home/data/.venv/bin:$PATH" git commit`. Never commit
  to `dev`/`main`. Never commit `evidence/lab/env` (tokens) or other secrets.
- Nothing merges into `dev` until the owner says it is fully tested. Releases only on the explicit word "release".
- **Resource rule:** storage is shared. One heavy job at a time, `nice -n 19`, capped threads, GPU where it helps.
- Update this spec (not just code) whenever a decision changes; add a dated line to §14.
- Visible UI changes: show a mockup and get the look + wording confirmed before building across files.
- Talk to the owner in short, plain answers; bugs in Cause / Fix / Proof form.

---

## 1. Summary

- **Detect once per file, publish to every owner.** Same path the app uses for previews: resolve which servers own
  a canonical file path, do the work once, fan out per server. Several Plex, Emby and Jellyfin servers are normal.
- **No server has a marker write API.** Plex → direct write into Plex's database (Plex Pass servers, same host only).
  Jellyfin → our existing Media Preview Bridge plugin gains a markers provider. Emby → a new small plugin that also
  re-applies markers when Emby wipes them. All three proven end to end, including skip buttons in the Jellyfin and
  Emby web players.
- **Nobody's markers are trusted blindly** — not Plex's, not online databases'. Every candidate passes checks, and by
  default a marker is published only when chapters say so or two independent sources agree.
- **Sources, in order:** chapters in the file → TheIntroDB → IntroDB.app → SkipDB → season audio matching (TV
  intros) → on-screen credit text (credits) → markers already on servers (second opinion only).
- **Its own job type** ("Intro & Credits") next to previews, sharing triggers, path/owner resolution and job UI.
- **Off until turned on**, per server.
- **Precision over coverage:** a missing marker is acceptable; a wrong one is not.

## 2. Goals and non-goals

**Goals**
1. One detection per file, reused by every server that has the file, including later webhooks from other servers.
2. Correct markers on all three servers that survive each server's scans, refreshes and restarts (or self-heal).
3. User control: review, adjust, lock; locked markers are never overwritten.
4. Measurable accuracy: an eval harness against ground truth gates every detector change.
5. Light on the host: capped threads, lowest priority, one worker by default.

**Non-goals (v1)**
- Commercial/ad detection. Recap/preview only when chapters or an online source supply them (Jellyfin shows these;
  Plex and Emby have no recap button).
- Per-user skip preferences (client-side on every server).
- Writing chapters into media files (media paths are read-only).
- Dropped from PR #241: PaddleOCR `:with-ocr` image, anime multi-OP clustering, subtitle recap tier, EDL export, bulk
  grid editor, submitting back to community DBs, AWS/ML tiers. Revisit only with evidence.

## 3. Proven facts per server

*(lab)* = proven on the lab servers on storage (Plex 1.43.4, Emby 4.10.0.40, Jellyfin 10.11.11 and 12.0.0) with the
real library mounted read-only.

### 3.1 Plex (PMS 1.43.4)

| Question | Answer | How it was proven |
|---|---|---|
| HTTP API to write intro/credits? | **No.** `POST /library/metadata/{id}/marker` → 400 for `type` 1–6 and every name; `attributes` override → 400; `PUT/DELETE …/marker/{id}` on intro/credits → 404; PUT converting a bookmark → 200 but ignored. Only bookmarks work. | (lab, claimed Pass server) + official spec + Plex Web 4.160 bundles + PMS binary route strings (`/marker`, `/marker/:markerID` only) |
| Other routes | Custom Metadata Providers (Dec 2025) spec has no marker/media/part fields. Plex's cloud credits lookup (`tv.plex.provider.metadata` `/markers?hash=<media_parts.hash>&type=credits`) can be pointed elsewhere with hidden pref `MetadataProviderUrl`, but **zero requests reached the lab proxy** even on forced credits detection → not honoured. Chapter names are never converted to markers. Plex has no plugin system. | (lab) `evidence/plex-provider-redirect/` + developer.plex.tv + staff posts |
| Where markers live | `taggings` rows (`text` = `intro`/`credits`/`commercial`, `time_offset`/`end_time_offset` ms, `extra_data` e.g. intro `{"pv:version":"5"}`, final credits `{"pv:final":"1","pv:version":"4"}`) pointing at the single `tags` row with `tag_type=12` and **`tag=''`**; plus a per-part copy in `media_parts.extra_data` (`pv:intros`, `pv:credits`: MediaPartMarkersArray JSON, sorted keys, `url` URL-encoded; or, on parts Plex's one-time credits `final` migration rewrote, that `url` form alone, §14 2026-09-19). `metadata_item_setting_markers` is per-user bookmarks. | prod DB (read-only) + (lab) |
| Direct DB write served? | **Yes, immediately, no restart**, XML identical to native markers. Stock Python `sqlite3` works (ICU triggers exist only on `tags` and `metadata_items` — PMS 1.43.4 — and we never write either; none on `taggings` or `media_parts`). | (lab) `evidence/lab/py_write.py` |
| `taggings` alone enough? | Served, but **wiped** by the next forced Plex detection of any type (Plex rebuilds from `media_parts.extra_data`). Writing **both** survives. `extra_data` alone is not served. | (lab) |
| What wipes our markers | Forced detection of the **same** type (`PUT …/credits?force=1`, season `…/intro?force=1`). **Not** wiped by metadata refresh (force), section scan, analyze with detection off, or non-forced detection. PMS 1.43.1+ forces credits detection on manual Analyze. | (lab) + release notes |
| Tag row | Plex looks up `tag_type=12 AND tag=''`. A row created with `tag=NULL` is ignored (Plex makes its own); a new row is only served after a PMS restart. **Reuse Plex's row; never create one.** | (lab) |
| Serving shifts | Credits start served **+2 s** vs DB; non-final credits end **−2 s**. Intro unchanged. Prod native markers show the same. | (lab) + prod API |
| Plex Pass | **An unclaimed / no-Pass server serves no markers at all**, even ones in its DB, and hides marker settings. Viewers also need Pass or Plex Home. | (lab) + support.plex.tv |
| When Plex's own detection runs | In a library only when **both** hold: the server pref (`GET /:/prefs`) `GenerateIntroMarkerBehavior` / `GenerateCreditsMarkerBehavior` ∈ `never`\|`scheduled`\|`asap` is not `never`, **and** the library pref (`GET /library/sections/<id>/prefs`) `enableIntroMarkerGeneration` (TV libraries only) / `enableCreditsMarkerGeneration`, a bool defaulting to `true`, is on. Plex's own summary of the library pref: "Detect … for items in this library when enabled in server settings." Written per library with `PUT /library/sections/<id>/prefs?<pref>=0`, the same write as `enableBIFGeneration`. | (lab, 2026-09-23) |
| DB location | WAL mode (checked on prod). SQLite: "All processes using a database must be on the same host computer; WAL does not work over a network filesystem." The app must run on the same host as Plex to write. Prod `plex` host: local ext4 → OK. | sqlite.org/wal.html + prod |
| Are Plex's markers right? | **Not always.** Prod South Park S01 intros ~80 s late (frames + studio chapters put the theme at 0:09–0:37); several credits markers past end of file. | prod DB + frame check |

### 3.2 Jellyfin (10.11.11 and 12.0.0)

| Question | Answer | How |
|---|---|---|
| Core write API? | **No.** `POST /MediaSegments/{id}` → 405; controller GET-only in 10.10, 10.11, 12.0. | (lab) + source |
| Plugin path | Plugin registers an `IMediaSegmentProvider` and a push endpoint that stores markers in the plugin data folder, then calls `IMediaSegmentManager.RunSegmentPluginProviders(item, libraryManager.GetLibraryOptions(item), forceOverwrite:false, ct)`. Segments appear instantly and **survive** Media Segment Scan, FullRefresh + replace-all, restart. | (lab, both versions) `evidence/plugins/jellyfin-*` |
| Why a provider | GET filters out rows whose provider id isn't a registered provider; a provider returning 0 segments deletes its rows. | source |
| Client | jellyfin-web shows **Skip Intro** for our segment. | (lab, Playwright, synthetic VP9 episode) |
| Versions | 12.0 adds required `IMediaSegmentProvider.CleanupExtractedData` and net10 → **two builds** (10.11/net9, 12.0/net10). Bridge 10.11.0.3 still loads on 12.0. 12.0 rejects `X-Emby-Token`/`api_key` (app already sends `Authorization`, #282). | (lab) |
| Types | Intro, Outro (credits), Recap, Preview, Commercial. | API |
| Wipes | All segments deleted when the file's mtime changes on refresh (correct: file changed). | source |

### 3.3 Emby (4.10.0.40, 4.9.1.90)

| Question | Answer | How |
|---|---|---|
| Core write API? | **No.** `POST /Items/{id}` with Chapters → 204 but ignored; no marker write route in OpenAPI, server DLL strings or web client. Staff (Jan 2026): not planned. | (lab) + research |
| Storage | `Chapters3` rows; `MarkerType` = Chapter, IntroStart, IntroEnd, **CreditsStart** (no credits end, no recap). | OpenAPI + (lab) |
| Plugin path | `IItemRepository.SaveChapters(internalId, list)` keeping existing `Chapter` rows. | (lab) `evidence/plugins/emby-4.10` |
| What wipes | `MetadataRefreshMode=FullRefresh` ("Search for missing metadata" / "Replace all") wipes all markers. Default/ValidationOnly/image refresh, recursive series refresh, library scan, restart do not. | (lab) |
| Self-heal | Plugin stores markers and re-applies on `ILibraryManager.ItemUpdated` (registered from an `IServerEntryPoint`) when they vanished — re-applied within the same refresh. | (lab) |
| Client | Emby web shows **Skip Intro** for our markers, but **skipping an intro needs an Emby Premiere key on the server**. The player checks the Premiere feature `dvr` against Emby's licence server by server id (same code in 4.9.1.90 and 4.10.0.40; Emby's Premiere Feature Matrix lists Intro Skipping under "Server / All Apps"). Without a key the button shows only while a per-browser counter is under 5 (each episode adds 2: 2 at the first, 4 at the second, so the first 2 episodes), a click opens "Unlock Feature" and doesn't seek, and then the button stops showing. `CreditsStart` drives the "Up Next" overlay with no check; Emby web has no Skip Credits button. `GET /Registrations/dvr` answers `IsRegistered`. Native apps not tested. | (lab, Playwright + the web client's `videoosd.js`) + emby.media Intro Skip and Premiere Feature Matrix + `evidence/lab/phase2-results.md` "Phase 2 close-out" |
| Versions | Each version is its own item with its own `Chapters3` rows; an item lists every version as a MediaSource with its `ItemId` (with an API key only when `AlternateMediaSources` is asked for). The web player shows the chapters of the version it plays. | (lab, 4.10 + 4.9) `evidence/lab/phase2-results.md` Task 10 round 3 |
| 4.9.1.90 | Same chapter behaviour; the plugin's 4.9 build passes the same checks as 4.10 on `mlab-emby49` (18/18; the 2 web-player checks are 4.10 only). 4.9's `DELETE /Library/VirtualFolders?name=` answers 500 (needs `Id=`). | (lab) `evidence/lab/phase2-results.md` Task 4 (first build, fix rounds 1–2) |
| Install | Catalog plugins install via `POST /Packages/Installed/{name}` + restart (proven with TimeMarkEdit). Separate builds for 4.9 and 4.10 (ABI change). Catalog entry needs a forum thread + developer id from Emby staff. | (lab) + dev.emby.media |

## 4. Online sources

**Accuracy** on 43 episodes with verified truth (studio chapters, frame check, or three-way agreement)
(`evidence/online/`):

| Source | Intros right / wrong / missing | Credits right / wrong / missing | Lookup |
|---|---|---|---|
| **TheIntroDB** v3 | **35 / 8 / 0** | 23 / 4 / 16 | `GET https://api.theintrodb.org/v3/media?tmdb_id&season&episode&duration_ms`; `credits.end_ms=null` = end of file; `null` start = 0; arrays may hold several segments |
| IntroDB.app | 26 / 13 / 4 | 17 / 4 / 22 | `GET https://api.introdb.app/segments?imdb_id&season&episode` (no duration); TV only |
| SkipDB | 16 / 17 / 10 | 8 / 25 / 10 | Measured on the daily ODbL dump matched by id + duration ±5% (R&M "outros" are the last 7 s). **Build uses the read API** `GET https://api.skipdb.tv/api/segments?imdb_id&season&episode&duration&adjust=conservative` (120 req/min), accepting only `match` exact/shifted (§14) |
| AniSkip | — | — | Anime only (MAL id + `episodeLength`). **Measured 2026-09-20, not taken** (`evidence/eval/aniskip-facts.md`): nothing in the library carries a MAL id, its numbering is per MAL entry and not the library's, and where it answers on intros it matches IntroDB to ≤ 44 ms on 21 % of them, so it is not independent (§5.5 rule 8) |

**Coverage** on a random prod sample of 200 TV episodes + 120 movies (`evidence/coverage/`):

| Source | TV | Movies |
|---|---|---|
| TheIntroDB | 26% (intro 22%, credits 10%) | 1% |
| IntroDB.app | intro 20%, credits 18% | n/a |
| SkipDB | intro 12%, credits 19% | credits 12% |
| Chapters in file | intro 10%, credits 14% | credits 21% |
| All online | intro 32%, credits 34% | intro 0%, credits 12% |
| Online + chapters | intro 34%, credits 41% | credits 29% |

Wider search found only Crunchyroll skip-events, IntroHater (key, Stremio-tied), Open Anime Timestamps (scraped),
SkipMe.db (client-restricted). Nothing covers sports, talk, reality or most Asian dramas → sports-type libraries off by
default. **Local detection carries most coverage.**

**Limits (measured from response headers 2026-09-13).** TheIntroDB without key: `x-ratelimit-limit: 30` per
`reset: 10` s, `x-usagelimit-limit: 500`/day, `x-usagelimit-specificmedia-limit: 2000`. With a key: 1,000/day per the
TheIntroDB Emby plugin author (**unverified** — check headers with the owner's key). IntroDB.app: anonymous, no key,
no rate-limit headers. SkipDB: read API, 120 requests/min, anonymous. The app paces from headers (never hard-coded), backs off on 429,
circuit-breaks on repeated 5xx. Webhook-triggered files get first call on the daily budget; backfill uses what's
left and local detection for the rest.

**Risk accepted by owner (2026-09-13).** TheIntroDB's terms (2026-06-16) license the API "solely for client-side,
non-commercial, end-user applications", prohibit server calls and caching beyond a session "unless expressly
authorized in writing". Used without that authorization. So: each user may paste **their own** free key (reads work
without one); no shared key ships; everything must work with the source disabled (keys can be revoked).

## 5. Detection

### 5.1 Chapters
Parse container chapters. Name → type by case-insensitive regex (`OP`/`ED` only in capitals, a leading BOM ignored); a
chapter runs to the next chapter start (its own end is clamped to the first later chapter start). A generic "Intro" /
"Introduction" chapter is ignored when the same file has a specific opening chapter ("OP", "Opening", "Title Sequence",
"Theme Song"…): anime files put the cold open in "Intro" (Mushoku Tensei S01E06: Intro 0–275 s, OP 275–364 s).
A **lone** generic "Intro" still decides: measured 2026-09-20 on 581 anime files, it is the theme song in 233 of the
274 Plex's own intro marker can judge and the cold open in 7, so dropping it would cost 569 anime intros to remove 7
wrong ones (`evidence/eval/phase4-chapters.md`).
**"Ending" is credits on a TV episode only** — on anime it names the ED (282 files), in a film it names the last
scene. Measured 2026-09-20: it gains 280 anime credits and changes nothing on 11,919 non-anime TV episodes or 9,904
movies; the one movie with an "Ending" chapter was frame-checked and taking it would skip the last 161 s of the film.
The kind is read from the file's **own path** (`ids_from_path`: a season and episode in the name), never from a kind a
server resolved — the path is part of the file's identity, so chapter evidence cached under `CHAPTER_RULES_VERSION`
can never disagree with the input that derived it. All 282 of those anime files name a season and episode, so the
stricter input costs nothing measured; a file whose path doesn't keeps the movie answer even when a server calls it an
episode. **"End" alone is still nothing anywhere**: no anime file uses it, four movies and five non-anime TV episodes
do.
Matroska `ChapterSkipType` is deferred: ffprobe 8 can't read it. Must include names the published plugins miss: **"Title Sequence"**,
"Opening Credits", "Intro", "Recap", "Previously", "End Credits", "Credits", "Outro", "Preview". 165 of 1,500 sampled
seasons (11%) carry them. Exact when present. Chapter truth can still be off vs the rule in §5.4 (3 of 40 movies had
a late or wrong "End Credits" chapter by frame check).

### 5.2 Online lookups
External ids come from the path first (`{tvdb-…}`/`{tmdb-…}`/`{imdb-tt…}` on the show or movie folder, `SxxEyy`), then
from each server's metadata (Plex `guids`, Emby/Jellyfin `ProviderIds`) — new `MediaServer` helper. An episode only
ever uses its **show's** ids (if the show lookup fails, no ids); a path counts as a movie only with a tmdb/imdb id and
no tvdb id; movies drop tvdb ids (different id space); items that are neither movie nor episode, and extras (trailers,
featurettes, `Extras/` folders…), get no ids. TheIntroDB is queried with `duration_ms` of the actual file. Every online answer passes the sanity checks in
§5.5 before it counts.

### 5.3 TV intros — season audio matching
**Fingerprint** (per file, cached): `ffmpeg -ss 0 -t <W> -i <file> -vn -ac 2 -f chromaprint -algorithm 1
-fp_format raw -` → uint32 LE, **0.1238 s/point** (measured). `W = min(900 s, 35% of duration)`. The app image's
`/usr/lib/jellyfin-ffmpeg/ffmpeg` has chromaprint; `/usr/local/bin/ffmpeg` does not, and the arm64 image has no
jellyfin-ffmpeg, so there season audio is unavailable with a message (Settings "Not available",
`GET /api/markers/sources/local`). CPU only (no GPU chromaprint), ~2 s per episode; at most 2 at a time.

**Matcher (v3)** for every episode pair: inverted index (±2 value shift); per shift, runs where
`popcount(a^b) ≤ 6`, gaps ≤ 3.5 s, length 8–120 s; keep all non-overlapping runs. Per episode: cluster candidates
(start, end) within ±4 s; rank by (length ≥ 15 s, number of supporting episodes, length); require support from
≥ 50% of the other episodes in the group (≥ 1 when only one other). Group = the video files in the episode's folder
with the same parsed season number, at most the 40 nearest by episode number (a flat folder can hold hundreds;
server-agnostic); mixed releases in one season work (Rick and Morty S01). An intro whose points are more than half
chromaprint's silence value (±2, or ≤ 6 bits apart) is dropped, and a pair that provably can't hold a run of 120 s or
less is skipped (two silent openings); neither changes the 118-episode numbers (`evidence/eval/phase2-harness.md`,
Task 7).

**Measured** on 118 episodes with studio-chapter truth ("useful" = end within 5 s and start within 15 s)
(`evidence/eval/`):

| Setting | Useful | Wrong | Missed |
|---|---|---|---|
| v1 (longest run, 15 s min, 600 s window) | 74 (63%) | 21 | 23 |
| v2 (+ all runs per pair, 8 s min, 50% quorum) | 84 (71%) | 17 | 17 |
| **v3** (+ window min(900 s, 35%), prefer ≥ 15 s) — alg1 stereo | **91 (77%)** | **13** | **14** |
| alg4 stereo | 87 | 11 | 20 |
| alg0 stereo | 86 | 15 | 17 |
| front-channel mono | 80 | 20 | 18 |
| alg2 stereo (0.3715 s/point, measured) | 48 | 10 | 60 |
| alg3 | unusable: chromaprint Test4 `set_remove_silence(true)` shifts every timestamp | | |
| v3 + snap end to silence | worse → not used | | |
| Parameter sweep (tune half / check half) | v3 already best on the held-out half | | |

**Few episodes (weekly releases)** (`evidence/eval/few_siblings.py`):
- Only **one other episode** of the season: 93 useful / 15 wrong / 10 missed — as good as the full season. A season
  is decidable from its second episode.
- **No same-season episode**, up to 4 episodes of the **previous season** (82 episodes had one): 48 useful / 10 wrong /
  24 missed (59%, precision 83%). Used for a season's first episode, as a candidate that still needs a second source.
- **In the app** (`tools/markers_eval`, `evidence/eval/phase2-harness.md`): the harness's eval-lists mode (at most 8
  files per season, only files with chapters) reproduces the table above exactly (91 / 13 / 14); app mode, whole season
  folder (158 files matched): **91 useful / 10 wrong / 17 missed** (The Simpsons S03: 3 wrong become missed).

Season audio decides an intro alone at Medium when nothing else answers (owner, 2026-09-24, overriding R2 "never
alone at High or Medium": alone 91 useful / 13 wrong / 14 missed on the 118, against Plex's own 23 right / 15 wrong).
The previous-season hint still never decides alone (48 / 10 / 24 above; owner 2026-09-13: it needs a second source).
Neither season audio nor the hint makes an agreeing pair with markers already on a server (G3, §5.5 rule 4): both come
from matching audio, so an episode where only those two answer stays in Needs review.

Remaining failures: variable couch gag (The Simpsons), a repeated segment ahead of the real intro (Carême), title card
10–20 s longer than the chapter (Daredevil, Outlander). Credits via audio matching: 54% precision — **rejected**.

A job fingerprints the season folder's missing episodes on its workers, matches cached fingerprints inline, and
queues a Season job for same-season episodes outside the job whose inputs changed (R3; §6.4 item 5).

### 5.4 Credits — on-screen text (movies and TV)
**Rule (owner):** Skip Credits lands on the **real credit roll** — the first credit card/crawl, including names over
footage — **not** on epilogue text cards ("Two months later…").

**Frames.** The job samples **keyframes of the tail itself** (no dependency on preview frames):
`ffmpeg -threads 2 [-hwaccel cuda -hwaccel_output_format cuda] -skip_frame nokey -ss <tail start> -copyts -i <file>
-an -sn -dn -fps_mode passthrough -vf "scale…320:180…,showinfo" -f rawvideo -` (pts from `showinfo`). Tail = last
**900 s** for a movie or a file of unknown kind, **450 s** for a TV episode (a `season_key` on the file's record;
T-R4 — a longer tail on an unknown-kind file only costs extra decode) **by default**, user-adjustable in Advanced
(`markers.credits_window`, §8): 5, 10, 15, 20 or 30 min, separately for TV episodes and for movies (a file of unknown
kind follows the movie value). Measured tails (lab scale run chapter
truth): TV credits (400 episodes) median 72 s, p95 267 s, 390 within 450 s (the 10 beyond are 462–463 s and
chapter mislabels at 1,365–2,578 s); movies (102) p95 563 s, max 852 s, all within 900 s — covers 205/205 measured
movie credits lengths too (median 233 s, p95 529 s). Keyframe rows are read in ffmpeg's own output order, **never
sorted** (T-R5): they are not always increasing (8 of 80 files in the 80-file set), and dropping the non-increasing
rows changes rule J's answer on one file. Mean luma is rounded to 0.1 and pts to 0.001 before rule J, matching the
prototype (T-R6, rule J compares luma against 30 and 12); because `-copyts` keeps the file's own start time in
`pts_time` while `-ss` seeks from the start of the file, every row's pts has the container's own `format.start_time`
subtracted before rule J sees it — skipping that on a recording with a non-zero start (an MPEG-TS PCR base can put
`pts_time` tens of thousands of seconds into a short file) would put the whole answer out of range. Then a short
full decode at 1 fps over the 20 s before the coarse answer to refine it, and, when more than 30 s of the file
follows the chosen run's last credit keyframe, a second 1 fps decode over the 21 s from 1 s before that keyframe to
refine the end (Q3); otherwise the skip runs to the end of the file with no end. Measured cost on storage (P5000
NVDEC): 8–13 s per movie incl. text detection; CPU keyframe decode of a 15-min tail 26.5 s. Per-frame exact seeks
(150–270 s) and full-rate decode of the tail are never used. Decode uses the app's existing per-GPU ffmpeg hwaccel
selection (NVIDIA / Intel / AMD, same as previews) with CPU fallback; a decode that exits non-zero or yields no
frames is a GPU failure (worker CPU rerun), while a decode that times out (600 s) is "no answer" and isn't decoded
again for a day unless the file changes or the run is forced (T-R7). **Intra-only streams** (every frame a keyframe:
ProRes, DNxHD, MJPEG, all-I H.264/HEVC) make `-skip_frame nokey` skip nothing, so the keyframe pass would decode and
text-check every frame of the tail and time out. `frames.keyframe_thinning` reads the first 24 video packet flags
with ffprobe; when all 24 are keyframes and their times give a stride above 1, the keyframe pass adds the input
bitstream filter `-bsf:V:0 noise=drop=mod(n\,N)` (ffmpeg 7.1+; the image ships 8.x) with N = 2.0 s over the median
frame interval (48 at 24 fps) — the median file's median keyframe gap in the 80-file set (1.46 s p10, 8.1 s p90),
the spacing rule J was measured at. Packets are dropped before the decoder, so the decode and text detection are
thinned but the whole tail is still read from disk. **VP9** (the same ffprobe's `codec_name`) skips nothing either:
FFmpeg's VP9 decoder never reads `-skip_frame`, and every hwaccel decodes inside it, so its keyframe pass drops the
packets not flagged as keyframes instead (`noise=drop=not(key)`, or `not(key)+mod(n\,N)` for an all-key VP9); a VP9
tail with no flagged keyframe gives no frames, a GPU failure whose CPU rerun finds no roll. The two 1 fps refine
decodes are unchanged. Any other stream (every other decoder honors `-skip_frame`, measured per codec in
`evidence/eval/phase3-harness.md`), or a probe that errors, gets the command above unchanged. A probe that times out
(30 s) is "no answer" for a day, like a decode timeout (T-R7); one that isn't started because earlier ffprobes are
stuck on the mount is "no answer" this run only, with nothing recorded against the file. The start-time probe
(`frames.container_start_s`) is handled the same way.

**Text detector.** RapidOCR **detection model only** (no recognition) at the frame's own 320 px
(`det_limit_side_len=320, det_limit_type="max"`) — same hits as default upscaling (19/19, 0 false), far cheaper.
`det_limit_side_len=320` is actually ignored by rapidocr 1.4.4 under `limit_type="max"` (the limit is raised to 960
for any frame under it): the frame keeps its own size because it's already under that raised limit, not because the
320 setting took effect (C6) — don't "fix" the limit to make it apply.
**Runs on any GPU vendor, CPU fallback** (owner 2026-09-13: "make sure all GPU types work";
`evidence/credits/gpu/RESULTS.md`). Same ONNX model on every path, identical boxes: post-processing is vendored in
`markers/credits/textdet.py` (pyclipper kept for the unclip step, T-R2); identical box counts to
rapidocr_onnxruntime 1.4.4 on the 289-frame bench (`evidence/eval/phase3-harness.md`). Model
`ch_PP-OCRv4_det_infer.onnx` (4,745,517 bytes, sha256 `d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9`,
taken from the rapidocr_onnxruntime 1.4.4 wheel), pinned at `/app/models/ch_PP-OCRv4_det_infer.onnx`;
`MEDIA_PREVIEW_TEXTDET_MODEL` overrides the path for development and the harness (not a setting; §8).
**arm64: no WebGPU EP wheel, CPU only.**
- **GPU:** ONNX Runtime **WebGPU plugin EP** (`onnxruntime-ep-webgpu`, +16 MB; Dawn → Vulkan → NVIDIA, Intel ANV, AMD
  RADV — ICDs and loader already in the image). Inside the app image on NVIDIA: 13.3 ms/frame vs CPU 18.7, 100% same
  boxes — **only** after applying the app's Vulkan probe env (`gpu/vulkan_probe.py` `get_vulkan_env_overrides()`,
  e.g. `__EGL_VENDOR_LIBRARY_FILENAMES`) before the session is created. Without it the NVIDIA ICD fails and Dawn
  silently uses llvmpipe at 430 ms/frame.
- **Guard:** use the GPU only when `get_vulkan_device_info()` reports a hardware device and a 20-frame self-test on
  that device finds exactly the CPU's boxes, in the same places, faster — a GPU that only matches the CPU's speed, or
  finds the same *number* of boxes somewhere else, doesn't pass (it compared counts alone until 2026-09-21, which
  was a complete test only while rule J read a row's count; version 3 reads where the boxes are). Otherwise CPU.
  A GPU helper runs on the WebGPU EP device whose `pci_bus_id` matches the worker GPU's; with no PCI match it falls back to the CPU, except on a host with a single WebGPU device when the
  worker's PCI address is unknown (T-R3) — the EP lists every display PCI device from sysfs, not only
  Vulkan-capable ones (storage's own ASPEED BMC VGA is listed beside the P5000), so refusing the GPU whenever
  several devices are listed would disable it on ordinary servers.
- **CPU:** ONNX Runtime CPU, `intra_op_num_threads=2`, 18–23 ms/frame.
- Rejected: CUDA-only `onnxruntime-gpu` (+2.8 GB, NVIDIA only); ncnn Vulkan (fast, but the pnnx-converted model
  output was wrong); OpenVINO (Intel only, +180 MB); ROCm/MIGraphX (GB-scale, removed from ORT); OpenCV DNN (no
  Vulkan in pip wheels).
- Honest gain: decode dominates, so a movie takes ≈ 16.7 s with GPU text detection vs 18.8 s on CPU.
- **Proven:** storage P5000 13.3 vs 18.7 ms (planning bench); the shipped helper's own self-test on storage measured
  11.2–11.4 ms/frame on WebGPU against 17.4–18.8 ms on the CPU (GPU kept, pinned to the card's PCI address,
  `evidence/eval/phase3-harness.md`); plex TITAN RTX 4.8 vs 7.7 ms; plex Intel UHD 770 16.1 vs 8.0 ms (iGPU slower
  than that CPU → self-test picks CPU). All 100% identical boxes. **AMD not tested** (no hardware, owner
  confirmed): same Vulkan/RADV path, self-test decides (Q6).
- CPU runtime size ≈ +300 MB (onnxruntime 62 MB, rapidocr 16 MB, opencv-headless ≈ 150 MB, numpy 59 MB,
  pyclipper ≈ 3.5 MB — all in the image; shapely ≈ 11 MB is a test-only dependency, used only to check the
  vendored post-processing against rapidocr's own, and stays out of the image). `rapidocr_onnxruntime` 1.4.4 is
  "gradually no longer maintained" and caps Python < 3.13, which is why its det pre/post-processing is vendored
  (above) rather than depended on at build time.

**What a row holds** (2026-09-20). Every decoded frame gives one row, `[pts, boxes, luma_mean, positions]`: its time
in seconds from the start of the file, how many text boxes the detector found, its mean luma, and where each of those
boxes is — `(left, top, right, bottom)` as inclusive pixel indices of the frame's own 320×180 (0–319 across, 0–179
down, so a box is `right − left + 1` wide), the bounds of the detector's own quadrilateral
(`markers/credits/rule_j.Box`). The positions come out of the same detection call as the count, so no
frame is decoded or read twice for them: measured over 910 decodes (the sets, the online cases and the 51 broadcast
files on both decode paths), they add a median 8.0 KB and at most 373.3 KB of rows per decode, and 15 µs per frame of
text detection against its 11–17 ms. The helper protocol carries
them (`{"id": n, "boxes": [[[left, top, right, bottom], …], …]}`, one list per frame) and the harness stores them with
every cached decode. **Rule J version 3 reads the positions in three places** (steps 4, 5 and 7 below); everything else — whether a frame
is a credit frame, which runs there are, where the run ends and whether text fills the tail — reads the count and the
luma, as it always did. A row that doesn't carry positions (the 80-file fixture's, built from the prototype's own
measurements) is read as a frame whose text could be anywhere, so it has no overlay, is never taken for a roll's own,
and its answer is version 2's (`rule_j.boxes_of`).

**Rule "J"** per frame `[pts, boxes, luma_mean, positions]`:
1. Credit frame = (`luma < 30` and `boxes ≥ 1`) or (`luma ≥ 30` and `boxes ≥ 3`). Bright frames need more text:
   signage in a lit scene gave 3+ boxes for minutes (Checkin' It Twice, −864 s under a looser rule).
2. Join credit frames into runs across gaps ≤ 24 s; dark empty frames never break a run (Summit of the Gods: 24 s of
   dark keyframes split the roll).
3. Keep runs ≥ 15 s; pick the **last** one (credits sit at the end). Steps 1–3 read the rows exactly as they were
   decoded, so which runs there are, which one is last, and how long it has to be, never depend on step 4.
4. **Text that never moves** (rule J version 3, §13 item 15): a box position the detector keeps finding right across
   the **story** — the rows before the run step 3 picked — is a channel or score bug, a ticker or a burnt-in timecode,
   not credits, and inside the run it doesn't count as text at all. The story's boxes are gathered into groups by
   overlap (IoU ≥ 0.5 against the group's first box); a group is an overlay when its first and last sighting are
   ≥ 80 % of the story apart and it was seen ≥ 4 times and ≥ 5 % of the story's row count (sightings are boxes, so two
   boxes of one frame on the same spot count twice); a box lying ≥ 60 % inside one is dropped and the frame recounted.
   **Steps 5–7 and the end all read the rows this step leaves**, and so does the 1 fps refine at step 9. A run left
   with fewer than two credit frames is not a roll and there is no answer: a bug over a night scene is all of it, and
   one credit frame would make a start that is also its own end. Gathering only the story's boxes, and picking the run
   before dropping any of them, is what keeps a roll long enough to fill the tail (the lab's Heeramandi episodes) from
   being read as its own overlay, and stops the step unmasking an earlier, worse run. Step 8 still counts the rows as
   they were decoded, so an overlay this step doesn't find still costs the file its answer; but its tests are measured
   from a start this step may have moved later, so a run it pushes past the 30 s floor can gain an answer version 2
   refused (no set, lab or broadcast file does). **It is not monotone the other way either:** thinning the run grows
   the spacing step 6 measures, so it can stop the anchor stepping over a glued-on frame and put a start up to the
   24 s join *earlier* than version 2, on story
   (`test_rule_j.TestOverlayBoxes.test_thinning_a_run_can_stop_the_anchor_stepping`; no set, lab or broadcast file
   does that either, and that measurement is the whole of the evidence).
   The thresholds were chosen on the 80, the 205 and the lab files,
   where the only answer they move is one 205 movie's, 22 s closer to its truth; on 51 broadcast recordings they take
   Medium's wrong answers from 6 to 4 on the GPU decode. The costs: a bug that leaves the screen before the credits
   (its span is measured against the story, so a wrong answer only moves later), and in-show graphics, name captions
   and epilogue cards, which move (`evidence/eval/broadcast-tv.md`).
5. **Same roll** (rule J version 3, §13 item 14): an earlier kept run is the same roll as the last one when its text
   sits in the last run's own horizontal band — the median middle of its boxes within 32 px of the last run's, a tenth
   of the frame's width (`rule_j.band_of`) — and at least half the keyframes between the two runs carry a text box.
   A roll keeps its layout from card to card and its text never stops; a scene's signs, captions and lower thirds do
   neither. Neither test is a distance, so a roll the 24 s join split anywhere in the tail is put back together. The
   start then comes from the earliest run of the roll, and the end still from the last (`rule_j.same_roll`).
6. **Anchor:** start only where two credit samples are adjacent (a lone scene-text frame 24 s before the roll glued
   itself on — Undisputed). One step at most, and never over a gap longer than the 24 s join: a frame further ahead
   was joined by dark frames alone, so every keyframe between is dark, and it is kept as the start, lit or dark (WILL's
   first card on black; on the sets 3 of the 9 such first frames are lit, all on the roll by frame check; rule J
   version 2). The cost is lit scene text followed by more than 24 s of dark keyframes before the roll (§13 item 14).
7. **Reach back** (rule J version 3, §13 item 14): step the start back over earlier keyframes whose text is in the
   roll's band and which keep the roll's own cadence — no further from the frame the walk is on than 1.5 × the spacing
   of the run's credit frames (the anchor's yardstick), and never more than the 24 s join. That spacing is the
   smaller of the run's on the rows this step reads and on the rows **as they were decoded**, so step 4's thinning
   can never widen it: thinning takes credit frames out of the run, and this walk has no step limit, so a wider
   limit is a longer walk (2026-09-21, `evidence/eval/phase3-harness.md`). This is what catches a roll
   whose opening is names over bright footage: those frames read 1–2 boxes, under the 3 a lit frame needs, so the run
   began after them. The cadence is what keeps the walk out of story, whose text is sporadic. The dark bridge is not
   honoured here — step 2 has already joined everything it reaches, so a stretch the run stopped at holds a lit frame,
   and crossing it as well puts two more of the 205 over 10 s before their chapter. Nor does it step onto a row of the
   run itself: the anchor has already ruled on those, and undoing its step took one published answer on the 80 more
   than 10 s early (Marvel's Daredevil S03, whose decode order put the anchor's next credit row two frames along).
   The band is the only thing it steps onto — a credit frame out of the band is not the roll's — which is what keeps
   step 5's refusal meaning anything end to end.
   **Steps 5 and 7 read the rows step 4 left, and that ordering is load-bearing**: read as decoded, a keyframe whose
   only box is a channel bug is in the roll's band whenever the bug is, and the walk crosses the whole story on it —
   worth two of the 51 broadcast recordings' right answers on the CPU decode when the band steps were measured
   without step 4 (`evidence/eval/broadcast-tv.md`). It costs something the other way, and that is not fixed: a roll
   that fills most of its own tail and that the join split has its opening block and its names over footage inside
   step 4's story, so its own text can be gathered as an overlay and step 5 then refuses the merge — the answer
   usually stays where version 2 had it, the gain forfeited. No file of either set, the lab or the 51 has that shape,
   and the three ways round it that were measured each cost a real broadcast answer. Step 4 is **not** monotone
   either way, on that shape or any other: it thins the chosen run's credit frames, which grows the spacing step 6
   measures, so it can also stop the anchor stepping over a glued-on frame and put a start up to the 24 s join
   *earlier* than version 2, on story. Nothing in
   the sets, the lab or the 51 does, and that measurement is the whole of the evidence
   (`evidence/eval/phase3-harness.md`, "The order of the two, and why").
8. **Text all through** (rule J version 2): no answer unless the tail holds at least 30 s of keyframes before the run
   and fewer than 80 % of those carry any text box. Text that never leaves the screen (a burnt-in timecode, a channel
   bug, subtitles from the first frame) makes runs anywhere: the lab's Synth Audio test pattern was answered on every
   episode. Every file of the sets has at least 84 s of the tail before its run and a share of at most 0.54. When the
   run is under 30 s into the tail and nothing lit comes before it in the tail (luma under 30), the roll may have
   begun before the tail. The keyframes of the 120 s before the tail are then read through the same keyframe pass.
   They are kept only when the run carries on into them, and the run is judged on both (final review, round 3). The
   run has then crossed the tail's edge, so while it still starts under 30 s after the first row read, the 120 s
   before the rows read so far are read and put in front in turn (2026-09-23) whatever they hold: more of the roll
   moves the start back, and story is what the start needs before it. The later steps read no further back than 30 s
   before the earliest credits start rule 2 of §5.5 keeps for the file (`decide.earliest_credits_start_ms`, one
   function both sides call: the last 25 %, or a chosen window never before the middle, and for a movie
   max(900 s, the movie window) from the end; a file of unknown kind — no season, not a movie — reads the movie's tail
   but has no such cap) — any earlier start would be refused, and those 30 s are the story a start on that line
   needs. No later step is shorter than 30 s: a remainder under that is read with the step before it (up to 150 s),
   and one left right after the first step isn't read. That saves a decode of its own for a sliver holding a keyframe
   or two at most, and often none. A window without a keyframe isn't empty: the keyframe pass gives the first keyframe
   after it and exits 0, on the GPU and the CPU alike (`evidence/credits/empty-window-decode.md`) — the first row
   already read, which the join drops — so such a step adds nothing and is no GPU failure. The first step reads what
   it always has, even where it lies wholly before that line (a movie on Automatic, whose tail starts at the 900 s
   cap): narrowed, it would change answers that were found. All the steps share one decode's 600 s: the first keeps that limit as
   before, each later one gets what is left of it, and a later step that would start past it or runs past it is a
   timeout like any decode's (T-R7) — a stalled mount leaves the file for a day rather than storing "nothing found".
   A run still too close to the first row when the steps stop has no answer. This answers the lab's five Heeramandi
   episodes, whose 462 s credits start before the 450 s tail, on the roll's first card, in one step. The costs: a
   roll that starts 0–30 s into the tail after a scene gets no answer, and so does one whose first card is the tail's own first keyframe with story before the tail
   (the join at the tail's edge is refused; a caption run on a night scene at the start of the tail has the same
   rows). So does one with only its first card before the tail when the anchor steps over it (the join is judged on
   the anchored start). The lab scale run's 400
   episodes have none of them: its 10 credits chapters starting 420 s or more before the end are those five and five
   mislabels. A file whose channel logo text detection boxes on 80 % of the story
   loses its answer too: on 51 broadcast recordings with channel logos that was one right answer, against five wrong
   ones the step removed (`evidence/eval/phase3-harness.md`). It counts the rows **as they were decoded**, step 4's
   overlays included — it is the step that catches a file whose overlay step 4 doesn't find, and counting them out
   would take those answers away.
9. **Refine** with the 1 fps decode: walk back from the coarse start through contiguous credit frames (gaps ≤ 2.5 s),
   then back over the fade (luma < 12, steps ≤ 4 s). The 1 fps rows are read without step 4's overlays too, so the
   walk can't step back over a bug the keyframes already dropped.

**Measured** on 80 files with chapter truth (40 movies, 40 TV; 3 movie truths corrected by frame checks,
`evidence/credits/adjudicated.json`, sheets in `evidence/credits/framechecks/`):

| Frames | Within 10 s | Within 30 s | Early > 30 s | Late > 30 s | None |
|---|---|---|---|---|---|
| **Keyframes of the tail (chosen)** | **59 / 80** | 66 | **1** (Undisputed −34 s) | 9 | 4 |
| Preview frames every 6 s (owner's interval) | 58 / 80 | 61 | 0 | 9 | 10 |
| Preview frames every 10 s (app default) | 43 / 80 | 49 | 1 | 9 | 21 |

The table's keyframe row was measured with the prototype's 10 s refine span; the app refines over 20 s, which gives
63 / 1 early / 8 late / 4 none on the same rows (`tests/fixtures/markers/credits_rule_j_80.json.gz`), and rule J
version 2 (the anchor's 24 s limit, step 6) gives 64 / 1 / 7 / 4. Version 3 gives the same on that fixture: its rows
carry no box positions, so none of its three steps fires there (its own numbers come from the harness's decode
cache).

Late cases are credits styles the rule doesn't see as "credit frames": names over bright footage or a curtain call
(Taxi Driver, Come from Away, Revenge of the Nerds), textured or light backgrounds, tiny text on black at 320 px
(WILL). Version 3's band reaches the ones whose opening still boxes some text (The Mummy and LOTR: Return of the King
move from +82 and +175 s to +10 s); what stays late is the opening the detector finds no text on at all. Late skips
are harmless (viewer sees more). Misses: credits shorter
than 15 s at end of file (Animal), credits over a scene (A Season to Remember), dark-grey textured background (Land of
the Dead), several sitcoms whose credits run squeezed over a scene. Why not preview frames: no better at 6 s, much
worse at the default 10 s, and it would couple the two job types.

Credits text alone is one source: at "High" it needs a second source; at "Medium" it may publish alone, since
it reads this file's own frames (§5.5 rule 6; owner, 2026-09-16). It also agrees with a server's own credits marker
as an independent source (rule 7 still shortens). The skip ends at the roll's last credit frame, refined at 1 fps to
the roll's last contiguous credit frame (no fade step), when more than 30 s of the file follows the roll (a scene
after the credits); otherwise it runs to the end of the file. The end mirrors the start's anchor (rule J version 2,
§13 item 13): when the run's last credit keyframe is a lit frame further from the one before it than 1.5 × the
spacing of the run's other credit keyframes, with a lit keyframe without text between them, it is scene text the 24 s
join glued on, so the end is refined from the credit keyframe before it and the 1 fps walk stops where the scene
starts. Whether there is an end is decided exactly as version 1 did, from the latest credit keyframe and the 1 fps
walk from it; the step back then moves that end earlier and never makes one. Emby still gets the start only (§6.3 R1).
Rule J version 1 shipped as measured; version 2 (`CREDITS_TEXT_VERSION` 2: the anchor's 24 s limit, the end's step
back, text all through) and version 3 (`CREDITS_TEXT_VERSION` 3: text that never moves, same roll, reach back — the
first rule to read where a frame's text is) are the two tunings so far, and each cleared the owner's bar: no more
early answers at any level on either set or either decode path — version 3 has one *fewer* on the 205 — no end moved,
Medium more useful. Every answer more than 10 s early or 30 s late,
shaped like epilogue cards, or with an end, and every answer a rule change moves by more than 10 s, is frame-checked
and adjudicated in `evidence/eval/phase3-harness.md`, and every answer whose verdict changes is compared run against
run whatever the size of the move; further tuning must beat version 3 the same way. Epilogue text cards touching the
roll, or joined to it over black, become the start (pinned in `test_rule_j.TestEpilogueCards`); the harness
frame-checks every answer shaped like that.

**Harness** (`evidence/eval/phase3-harness.md`; the app's own `find_credits` and `decide()`, not the prototype):
- Rule J alone on the 80 (this spec's own bar: ≥ 59 within 10 s, ≤ 1 early): version 3 on the GPU decode 66 within
  10 s / 1 early / 5 late / 3 none, on the CPU decode 61 / 1 / 7 / 8 — both meet the bar (version 2: 64 / 1 / 7 / 3 and
  59 / 1 / 8 / 8; version 1: 63 / 1 / 8 / 3 and 58 / 1 / 9 / 8, the CPU one file short), every version measured on the
  same tree after the intra-only thinning. The two decode paths scale the frame differently (`scale_cuda` vs swscale)
  and don't always agree.
- Q4 gate, check by check: the 80 (movies40 + tv40) passes 5 of 5 on both decode paths. The 205 movies fails 3 of 5
  (Medium useful 101 < Plex's 124; Medium wrong 16 > cap 5; High wrong 15 > cap 3; version 2: 96 and 17; version 1:
  90) but passes both "never looser than Plex" checks by a wide margin. What is left of the gap is the roll the
  detector never sees at all: on the 41 files version 2 answered more than 30 s late, only 34 had a text box within
  30 s of the chapter to reach for, and 35 are still late — a disclosed, tracked limitation (§13 item 14).
- Ends (Q3): 19 set rows (17 distinct files) got an end in version 1's first run; published for 6 files at High and 8
  at Medium. Neither version 2 nor version 3 moved a credits-text end on either set, and the two decisions version 2
  newly publishes with an end keep the scene or stop on logos. No end swallowed a scene.
- Every set answer version 2 moved is the anchor's; every one version 3 moves is the roll's own band (`same_roll` or
  `reach_back`) bar one, a stand-up special whose stage signage step 4 takes for the overlay it is. The end's rules
  and the text-all-through step move no answer on either set or decode path, and no online decision moves under
  either version.

### 5.5 Combining evidence
Each source yields candidates `{type, start_ms, end_ms, source, confidence}`.
1. A **locked** user marker wins, always (even for a type whose detection is off); it is never demoted by rules 9–10,
   and it is published over a server's own markers even where that server is set to keep them (`keep_plex`,
   `keep_emby`). This is the only rule for a lock against a kept type; the publishers report the types whose own
   markers they replaced and the UI says so (§6.3, §14 2026-09-20).
2. Sanity checks apply to every candidate, chapters included: inside the file (end ≤ duration + 2 s, clamped to the
   duration; unknown duration fails; a segment can't end before it starts); length ≥ 3 s for every type and ≤ 300 s for
   intros and recaps; intro/recap starts in the first 35% and must not run to the end of the file (end missing or
   ≥ duration − 2 s); credits/preview start in the last 25%; movie credits start ≤ 900 s from the end. (A chosen
   credits window moves both, §8; the earliest credits start they keep is `decide.earliest_credits_start_ms`, which
   the credit text detector's reads before its tail are bounded by, §5.4 step 8.)
3. Chapters → accept (first intro/recap chapter, last credits/preview chapter; on a tie the one with the earlier end),
   unless two agreeing independent non-chapter sources contradict the chapter → **"Needs review"**. One contradicting
   source never overrides chapters. When two or more independent sources agree with the chapter's checked edge, the
   other edge takes their safer value if it is safer (later intro/recap start, earlier credits/preview end). **Season
   chapter-intro check (F1):** when at least 2 other episodes of the season group have an intro chapter, an intro
   chapter longer than max(2 × their median, median + 30 s) doesn't decide alone: it needs one agreeing independent
   source that isn't markers already on a server (else "Needs review", reason "Intro chapter is much longer than the
   rest of the season's"); the agreeing candidates may then shorten it the same way.
4. Otherwise accept when two independent sources agree: intro/recap **end** within 5 s; credits/preview **start**
   within 10 s. An agreeing set needs a candidate that is neither markers already on a server nor season audio (or its
   previous-season hint): season audio and a server's own detection never decide together (G3; "Needs review", reason
   "Season audio and a server's own marker agree, but both come from matching audio; needs another source").
   Every maximal set of mutually agreeing candidates is considered (a sliding window over the compared
   times). Only candidates that agree with a different independent source may supply times: the agreed edge comes
   from the first of them in source order; the other edge takes the safer value across them (latest intro/recap
   start, earliest credits/preview end). If the composed marker fails sanity → "Needs review". (Taking the safest
   agreed edge instead was tried in the phase-1 audit and rejected: it hid a contradiction and published a wrong
   Daredevil S03E02 intro.)
5. If two groups of agreeing sources would publish times that don't agree with each other → "Needs review". Before
   anything is published (chapters, agreement or "Medium"), any two agreeing candidates from different independent
   sources that are both outside the tolerance of the published time send it to "Needs review" — a third source that
   agrees with both sides can't hide a contradiction. Another chapter of the same type counts as one side of such a
   pair; a chapter within tolerance of two groups that disagree with each other is still accepted.
6. A single source is accepted only at the **"Medium"** rules (the app's only rules since 2026-09-24, §14:
   `decide.APP_PUBLISH_WHEN`; "High" stays in `DecisionContext` for the evaluation harness), only when that source checks the file's
   cut itself — chapters, credits text (it reads this file's own frames), season audio for an intro (since
   2026-09-24, §14; not the previous-season hint), or SkipDB `exact`/`shifted` matches for an intro or recap (IntroDB
   and TheIntroDB return an answer whatever the file's length, so alone they never decide; SkipDB alone never decides
   credits or a preview, which need an agreeing independent source as at High) — and only
   when no sane candidate from another independent source (markers already on a server included) contradicts it and
   every pair of the source's own candidates agrees; its other edge takes the safer value across those candidates.
7. Markers already on a server count as agreement evidence, never as a sole source, and never supply the published
   times on their own. When a server marker agrees, it may **shorten** the composed skip (a later intro/recap start,
   an earlier credits/preview end) but never lengthen it — so a crowd answer running to the end of the file can't
   swallow a post-credits scene that the server's own marker stops before. Markers from several servers count as one
   source. A Plex item's markers are not used for a file whose item has another version with a duration more
   than 2 s different (one set per item describes one cut). Emby's are read only from the file's own version item,
   which carries only that version's markers (§3.3, §6.3; a path that resolves to another version's item gives no
   evidence), so no such check applies. Markers on a Jellyfin/Emby server
   that has an importer plugin count as the database it imports (rule 8). Once credits or a preview are decided
   (any path but a lock), a server's own detection markers of that type (never an importer plugin's, never ours or
   another cut's) may also move the **start** later. If any of them covers the decided start or starts within 10 s of
   it, the server says the credits are already running there and nothing moves. Otherwise each server offers its first
   start more than 10 s after the decided start and more than 10 s before the decided end, and the latest offer wins —
   so a server that splits its credits into pieces can't pull the start to its last piece. `decided_by` adds
   `server_markers`; the reason (and the Inspector) names the server(s). A shortened marker failing sanity sends the
   type to "Needs review" with the unshortened marker proposed. This runs last, after rule 5's contradiction check and
   rules 9–10 have judged the unshortened markers, and only on types still decided, so it can shorten a marker but
   never turn "Needs review" into a published one. Intro and recap ends are never moved this way. A chapter decision
   shortened or confirmed only by server markers still counts as chapters alone for the evidence search.
8. Online sources are independent of each other only if they don't copy each other: IntroDB data looks partly seeded
   from others — IntroDB + TheIntroDB always count as one source. Server markers written by an importer plugin count
   as the database it imports (by the plugin's name): an IntroDB or TheIntroDB importer's as IntroDB + TheIntroDB, a
   SkipDB importer's as SkipDB (so SkipDB and its copy never agree). An AniSkip importer's also count as IntroDB +
   TheIntroDB, permanently: phase 4 measured it (2026-09-20) and AniSkip matches IntroDB to ≤ 44 ms on 21 % of intros (14 of 68), against 10 % (7 of 72) between TheIntroDB and IntroDB, which are already one source (`evidence/eval/aniskip-facts.md` "Is it independent?"). When the database can't be told (no name
   matches, or the server has importers of more than one database) they count as IntroDB + TheIntroDB, as before.
   Season audio and its previous-season hint count as one source (the same method on the same show).
   SkipDB intro starts also match TheIntroDB's to ≤ 44 ms on the Daredevil S03 episodes both cover
   (agreement is on intro ends, so they stay separate for now; re-measure before enabling TheIntroDB by default).
9. A decided intro and recap overlapping by more than 5 s → both "Needs review".
10. A decided preview overlapping decided credits by more than 10 s → the preview goes to "Needs review".
11. No agreement → no marker; shown as **"Needs review"**. `decided_by`: agreement → the sources that agree with the
    winner plus any that supplied an edge; chapters → `chapters`, plus the agreeing sources when they replaced the
    chapter's other edge; "Medium" → the sources that supplied an edge. Results never depend on the order candidates
    arrive in.

### 5.6 Resource rules
Intro & Credits jobs: 1 worker by default, lowest priority, ffmpeg `-threads 2`, ONNX Runtime `intra_op_num_threads=2`,
fingerprints ≤ 2 in parallel, online lookups paced by headers. Never parallel per-frame seeks.

## 6. Architecture

### 6.1 Data — new `markers.db` (SQLite) in `CONFIG_DIR`

```
files(id, canonical_path UNIQUE, size, mtime, duration_ms, season_key, updated_at)
fingerprints(file_id, window TEXT, start_s, points BLOB)                   -- cached chromaprint
evidence(file_id, source, type, start_ms, end_ms, confidence, meta_json, fetched_at)
markers(file_id, type, start_ms, end_ms, decided_by, locked, updated_at)   -- desired state
publish_state(file_id, server_id, item_id, markers_hash, status, message, verified_at)
```
- File identity = path + size + mtime. A change invalidates fingerprints, evidence and unlocked markers.
- `markers` is the single source of truth; servers are projections of it. `publish_state` is per `server_id`, so two
  Plex servers are tracked independently.

### 6.2 Job type and flow
**Intro & Credits is its own job type**: own queue, schedules, priority, pause/cancel, retries. It reuses triggers
(scans, schedules, Sonarr/Radarr/server webhooks), owner/path resolution, job storage and the dashboard/job UI.

1. **Trigger.** Webhook: the debounced batch submits the preview job (HIGH) as today, then an Intro & Credits job for
   the same files at NORMAL, grouped by season folder — it runs after the previews (§6.4). Backfill: "Start job →
   Intro & Credits" for chosen libraries, or a schedule, at LOW. Schedules are independent from preview schedules;
   each skips files already done.
2. **Owners.** `find_owning_servers(canonical_path)` → keep owners with `markers.enabled` and the item's library in
   `library_ids`. No enabled owner → nothing is detected.
3. **Ensure markers for the file.** Fresh `markers` for (size, mtime) → reuse ("detected once, reused"). Otherwise
   gather evidence in §1 order, stop early when §5.5 is satisfied by more than chapters alone. **A decided type
   doesn't ask a local detector again on a normal run** (only a forced run, an answer of another version, or an
   answer the decision rests on does); a chapter decision is final for credits text (C7) — a server never asked for
   the file, and not yet published to, is still read once, since rule 7 lets its own markers shorten decided
   credits; an empty or unusable answer isn't asked again while everything stays decided. **No local detector reads
   the file for a type no answer of ours would be shown for**, on any run, forced included: every server the file's
   markers go to keeps its own (`keep_plex`, `keep_emby`) and shows its own of that type now — rule 7's own markers
   (never ours, an importer plugin's or another cut's), read from each server on that run in one read the evidence
   read shares — and the type isn't locked. A type that ends undecided while that holds, whether it was skipped or
   an answer stored earlier left it in review, is stored `disabled` with the reason "kept Plex's own marker" instead
   of Needs review, and the rows say "Keeping Plex's credits"; a decided type stays decided (the publisher keeps
   Plex's rows and says so, as before), and what is sent to a server is exactly what an undecided type sends. Worked
   out again on every run and never stored as an answer, so
   "Use ours", a server losing its marker or a new destination without one reads the file on the next run (§14
   2026-09-23). Decide, store. Stored chapter and online evidence carries its rules or parser version; a file whose
   stored version is older is probed or asked again on the next run.
4. **Season step.** Intros need siblings. A job fingerprints the season folder's missing episodes on its workers,
   matches cached fingerprints inline, and queues a Season job for same-season episodes outside the job whose inputs
   changed (R3). An episode alone in its season group uses up to 4 cached fingerprints of the previous season (§5.3).
   Season jobs (`Season: <show> · <season>`) run at LOW, or NORMAL when queued by a webhook follow-up (its retries and
   verify job queue LOW); at most 500 files; new requests join a running Season job of the same priority holding an
   episode of that folder, or else a waiting one; a Season job never queues another of its own, only one follow-up for
   the requests it took while running that its own run of the file came before (§14, 2026-09-24). A retry whose runs
   changed nothing stored queues none. Requests live in memory: a restart only delays them until the season's next
   run.
5. **Publish.** For each enabled owner, its `MarkerPublisher` writes the decided set; unchanged `markers_hash` → skip.
6. **Reconcile.** After jobs = the read-back before "Up to date" and the delayed verify job (phase 1). On demand =
   **Intro & Credits · Check servers**, a LOW job the user starts from Start New Job or `POST /api/markers/reconcile`,
   or schedules in Automation → Schedules like any other job; nothing is scheduled by default (R5). It bulk-reads every
   published item (and every item a file left a type to the server's own marker on, step 3: read back like a kept
   type) and re-runs drifted files (Plex forced detection, Emby FullRefresh without heal, a Plex version
   change, an item the server replaced; a drifted Plex item's current version files too), plus files with decided
   credits or preview whose server's stored answer is empty or unusable, on a 1/2/4/8/16-day backoff (at most 5
   re-reads, failed ones included), and the files of items whose last publish failed, on the same backoff (at most 5
   retries per failure). A file with a marker the user locked that an owning server never received (its last publish
   there `failed` or `skipped`: the server was down or not ready when the editor saved) is listed on every run with no
   backoff, until a server has it. Locked markers re-assert (§5.5 rule 1). Plex
   `on_plex_redetect` = `restore` (default) or `keep_plex`; Emby `on_emby_redetect` = `restore` or `keep_emby`.
   `keep_plex` keeps Plex's markers (§14 2026-09-14), not stored as evidence.
7. **Outcomes** per server: markers written / reused / needs review / skipped + reason.
8. **Manual edit** in the Inspector: no job — save, lock, publish to every owner immediately (`POST /api/markers/item/markers`; `DELETE` on the same route unlocks and publishes nothing). It is one web request, so it is bounded: the save and the lock land before any server is contacted, each call to a server is capped at 8 s (`PUBLISH_NOW_SERVER_TIMEOUT_S`; Plex's database waits the same 8 s for its locks), and a server the fan-out hasn't started 25 s in isn't started (`PUBLISH_NOW_DEADLINE_S`, a start gate, not a cancellation, so a server already under way can run to a small multiple of 8 s). A server not reached says so in its row and is published by the next run; a job already running on the same file makes the request give up on the whole publish after 2 s. No retries, and no thread that outlives the request.

### 6.3 Publishers
`MarkerPublisher` (parallel to `OutputAdapter`): `capability() -> Ready | Disabled | NeedsConfirmation |
NeedsPlugin | PluginOutdated | NeedsPass | NeedsLocalDb | AgentUnavailable | NeedsPlexDetectionOnce | UnsupportedSchema | Unreachable |
Misconfigured` (`publishers/base.Capability`), `write(item_id, markers, *, previous, duration_ms, canonical_path,
own_previous, kept_types) -> list[Marker]` (the markers ours on the item after the call; `last_write_changed` says
whether that call changed the server, `last_kept_types` which types stay the server's own,
`last_replaced_own_types` which types a lock took off the server although it keeps its own (§5.5 rule 1),
`last_item_files` which version files the set was computed for), `shows(item_id, ours, *, kept_types, item_files) -> Ours | Missing |
Replaced | VersionsChanged | Gone | None` (a cheap read-back of what the server shows of what we last left there: Plex's
`taggings` rows and the item's live version files under the same lock proof, Jellyfin's core `/MediaSegments`;
`Shown.VERSIONS_CHANGED` when the item's versions, optimized copies left out, differ from the `item_files` recorded
at the last write), `atomic_writes`.

**PlexMarkerPublisher** (opt-in per Plex server; Pass servers only)
- Two halves (`publishers/plex_db.py`): the publisher decides what to write (settings, path mappings, multi-version
  agreement), and a `PlexDatabase` runs it against the file. `LocalPlexDb` opens the file this process sees;
  `plex_remote.RemotePlexDb` hands the same arguments to the **Plex marker agent** on Plex's own machine, which runs
  `LocalPlexDb` there. One implementation of the rules either way.
- DB path from that server's `output.plex_config_folder` (`Plug-in Support/Databases/com.plexapp.plugins.library.db`).
  Check the directory's filesystem type; network mount (NFS/SMB/CIFS, Docker Desktop shares) → `NeedsLocalDb`, Plex
  stays read-only with a clear message. With an agent the path, the filesystem check and the lock proof are the
  **agent's**, not this app's.
- **Plex marker agent** (`plex-marker-agent/`, its own small image; contract and distribution in its README):
  per-server `markers.plex.agent` = `{enabled, url, token}`, the key masked as `****` everywhere and round-tripping
  unchanged, `Authorization: Bearer` with `secrets.compare_digest` — the app's own token model. Its database path is
  its own `PLEX_CONFIG_DIR`, never the app's to choose; it exposes no SQL and nothing but one item's markers. Version
  skew (an agent that doesn't implement the app's `X-Marker-Agent-Protocol`, or is older than `MIN_AGENT_VERSION`) is
  refused on the first call, in either direction, naming both versions and which side to update → `AgentUnavailable`,
  nothing written. The same state covers an unreachable agent, a refused key, and an agent whose Plex isn't this
  server (both sides name Plex's machine identifier — the agent from `Preferences.xml`, the app from the server it is
  connected to — and a proven mismatch refuses the write), with `details["agent"].state` telling the Edit tab which.
  A remote publisher is **not** `atomic_writes`: an answer can be lost after the agent's transaction committed, so
  after a failed write what is ours on the item is unknown and nothing of ours is removed until it is read back.
- Resolve `metadata_item_id` + all `media_parts` for the item (existing bundle lookup).
- One short transaction, `busy_timeout=30000`: delete our types' `taggings` for the item; insert rows on Plex's
  `tag_type=12, tag=''` row; rewrite `pv:intros`/`pv:credits` in every part's `extra_data` (sorted keys, rebuilt
  `url`). Write credits start as `served − 2000 ms`.
- Tag row missing → `NeedsPlexDetectionOnce` (never create it).
- Multi-version items share one marker set: publish only when all parts' decisions agree within 2 s. A part never
  decided whose file is on none of its path-mapped disks (Plex lists a deleted file until it scans) takes no part; one
  on disk and never decided is waited for, and the job retries the waiting file (§14, 2026-09-24).
- Unknown schema (columns/JSON shape differ from 1.43, `extra_data` in neither of Plex's two forms) → stop writing,
  show message. A part is written back in the form it has (JSON with `url`, or the URL-encoded form alone). A
  URL-encoded part we empty is left as `""` — what Plex's own rollback makes of the `{"url":""}` an emptied JSON part
  gets, and no fields in either form — and a later write that puts a key back writes JSON, Plex's usual form, exactly
  as it does over the NULL Plex leaves on an empty part.
- Never write `tags`; never run integrity checks with stock SQLite (custom tokenizer).
- Rows and `pv:` keys that already serve the desired times stay byte for byte, except a stale credits `final` flag on
  a one-version item under "Use ours" (see §14, 2026-09-15 "Plex credits `final` flag").
- Warn when Plex's own detection is on (it can force-overwrite). Before a file is reported up to date, the job reads
  the item's rows back: gone → written again; replaced by Plex's own → written again (`on_plex_redetect=restore`) or
  kept per type (`keep_plex`): the publisher leaves that type's rows and `pv:` key alone on every write path until
  the setting is `restore`, Plex has no rows of the type, or the user locked the type (`item_publish_state` kept
  types). Under `keep_plex` a
  decided type's rows become kept when they are neither what the write would show nor what the item record (or the
  moved file's own record) says is ours, so rows on an item with no record of the type are kept rather than replaced.
  A **locked** type is never kept (§5.5 rule 1): its rows and `pv:` key are rewritten — the stale credits `final` flag
  refresh `keep_plex` otherwise turns off included — and the types whose own rows it replaced are reported in
  `last_replaced_own_types`.

**JellyfinMarkerPublisher**
- Extend **Media Preview Bridge** (`jellyfin-plugin/`, route prefix `MediaPreviewBridge`, today `Ping`,
  `ResolvePath`, `POST Trickplay/{itemId}`): add `POST /MediaPreviewBridge/Markers/{itemId}` (store JSON in plugin
  data folder, run providers with `forceOverwrite:false`) and `DELETE` same path; provider named
  "Media Preview Bridge". Prototype: `evidence/plugins/jellyfin-10.11/` and `-12.0/`.
- Two builds per release: 10.11 (net9, `Jellyfin.Controller` 10.11.0) and 12.0 (net10, `CleanupExtractedData`).
  Manifest carries both `targetAbi`s. App installs/updates via the existing `install_plugin()` flow
  (`PLUGIN_REPO_URL` manifest).
- Types: Intro, Outro (credits), Recap, Preview.

**EmbyMarkerPublisher** (`markers/publishers/emby.py`; plugin `emby-plugin/`, contract in its README)
- **Media Preview Bridge for Emby** plugin, builds for 4.9 and 4.10. JSON keeps C# PascalCase and leaves out nulls;
  times are ticks (ms × 10,000). `GET /MediaPreviewBridge/Ping` (anonymous) → `{"Ok", "Version", "Features":
  ["markers"]}`. `GET` / `POST` / `DELETE /MediaPreviewBridge/Markers/{Id}` (administrators; 401 without a token, 403
  for a non-admin): POST `{IntroStartTicks, IntroEndTicks, CreditsStartTicks, FileSize, ReplaceOwn}`; every answer is a
  `MarkersResponse` `{Id, Found, IntroStartTicks, IntroEndTicks, CreditsStartTicks, FileSize, Stale, Stored, Error}`
  (a refused body is 200 with `Error`; a store or chapter write failure is 500 JSON with nothing changed).
- The plugin writes `SaveChapters` keeping `Chapter` rows and removes only rows equal to what it stored; `ReplaceOwn`
  replaces other writers' rows of a type (the app sends it for `restore`). **Heal:** re-applies on `ItemUpdated` when a
  FullRefresh deleted the rows. **Stale guard:** the store records the item's path and `FileSize`; while the file on
  disk differs, markers are stored but not shown (`Stale: true`) and our rows are removed at the next item update.
  **Remove:** a removed item's store file is deleted (`ItemRemoved`), and a daily/start-up sweep deletes store files of
  items Emby no longer has.
- The publisher checks the Ping `markers` feature and admin access (`needs_plugin` with `catalog_listed`,
  `plugin_outdated`, `misconfigured`, `unreachable`). **Per version:** each Emby version is its own item and the
  player plays the chosen version's chapters, so each version is published on its own like Jellyfin (no cross-version
  agreement, no recorded `item_files`). A write reads `Fields=Chapters,MediaSources,AlternateMediaSources` once and
  confirms this file is that item's own version (listed under another version's item → failed; several versions and
  none this file → waiting, not in library). POST carries this file's size; every POST is confirmed by reading the
  chapters back (not shown → DELETE, failed). An unchanged set whose stored state matches (same ticks and size, not
  stale) sends nothing. `shows()` reads the chapter rows (credits compared by start). `keep_emby` posts without
  `ReplaceOwn` and records the types Emby shows its own rows of as kept; the plugin still stores ours for them (a
  decided type only: one the file wasn't read for, §6.2 step 3, has none to store). A
  **locked** type is never kept (§5.5 rule 1): since `ReplaceOwn` is per POST and not per type, a locked type Emby
  still shows its own rows of is re-posted alone with `ReplaceOwn`, then the whole set is posted again without it, and
  that type is reported in `last_replaced_own_types`.
- **Credits (R1):** Emby always gets the decided credits start, even when the credits end before the file does
  (end < duration − 2 s); its Skip Credits button then skips to the end of the file, including any scene after the
  credits. The row and the Inspector note say "Emby skips to the end of the file".
- Types: IntroStart, IntroEnd, CreditsStart (no recap or preview).
- Install: `POST /api/servers/{id}/install-plugin` installs from Emby's catalog (`POST /Packages/Installed/{name}` +
  restart) when `GET /Packages` lists the plugin; `manual: true` when `GET /Packages` doesn't list it (`false` with an
  error when the catalog can't be read), and the Edit tab links the guide's manual DLL install. Catalog status: §13
  item 5. Prototype: `evidence/plugins/emby-4.10/`.

### 6.4 Workers, priority and pause — plugging into the existing engine
Owner (2026-09-13): marker work must respect the GPU and CPU workers exactly like previews.

**How the engine works today (verified in code on `dev` @ d47e376):**
- One process-wide `WorkerPool` (`jobs/worker.py`): GPU workers per enabled `gpu_config` device (`workers` each) plus
  `cpu_threads` CPU workers (default 1). Workers are **not** routed by type: `_find_available_worker` takes the first
  idle worker (GPU workers are listed first); a GPU worker passes its `gpu`/`gpu_device` down, a CPU worker `None`.
  `cpu_only` routing exists but is unused.
- One `JobDispatcher` (`jobs/dispatcher.py`) with a tracker per job (`check_queue`, `item_queue`). `_get_next_item`
  serves the highest priority (HIGH 1 / NORMAL 2 / LOW 3), then the oldest job, draining each job first.
- The check stage (`_run_check`) runs on `scan_workers` threads, not on GPU/CPU workers.
- `JobGate` (`web/job_gate.py`): `max_concurrent_jobs` default 3, one slot kept for HIGH.
- GPU failure: the same worker reruns the item on CPU (`worker.py` `_process_item`); nothing is re-queued.
- Pause from the UI is **global** (`/jobs/<id>/pause` delegates to global pause); per-job pause flags exist
  (`job_manager.is_pause_requested(job_id)`, used by schedule stop times) and running ffmpeg is SIGSTOPped.
- There is no job kind: `Worker._process_item` calls `process_canonical_path` directly; `_run_check` likewise.
- Webhooks are debounced into batches (`webhook_delay`, default 60 s; `web/webhooks.py` `_execute_webhook_job`).
- `Worker.ffmpeg_threads` is stored but never passed to ffmpeg; CPU preview ffmpeg runs uncapped.

**Design:**
1. **Job kind.** Add `kind` (`previews` | `intro_credits`) to `Job` and `JobTracker`; the tracker carries `check_fn`
   and `process_fn`. `Worker._process_item` calls `tracker.process_fn(item, gpu, gpu_device, …)`; `_run_check` calls
   `tracker.check_fn`. Previews keep today's functions; tests assert the previews path still receives identical
   kwargs (D34 lesson, `.claude/rules/testing.md`).
2. **Same pool, same limits.** Marker items run on the same GPU and CPU workers as previews, so the user's worker
   counts cap total load. No extra workers, no parallel pool. Dashboard worker rows show
   "Intro & Credits · <title> · <step>" through the existing worker status updates.
3. **Check stage (no worker slot):** file identity, chapters (ffprobe), online lookups through one shared rate
   limiter per source (all jobs), decision. Only files still needing local detection enter `item_queue`.
4. **Worker stage, per movie/episode:**
   - Audio fingerprint when its season needs one: CPU ffmpeg `-threads 2` on whichever worker picked the item.
   - Credits: keyframe tail decode with **the worker's GPU** using the same hwaccel argument builder as previews
     (`processing/ffmpeg_runner.py`: CUDA / VAAPI / QSV…); a CPU worker decodes in software. Then text detection on
     the worker's GPU (item 7) or CPU.
   - GPU error → rerun that step on CPU in the same worker, mirroring previews.
5. **Season decision** (cheap numpy matching): a job fingerprints the season folder's missing episodes on its
   workers, matches cached fingerprints inline, and queues a Season job for same-season episodes outside the job whose
   inputs changed (R3; the engine has no completion hook; phase 2 Task 3). A pair with more than 2,000,000 value
   matches is matched on a worker instead of a checking thread.
6. **Priority and gate.** Webhook-triggered Intro & Credits jobs submit at NORMAL (preview jobs are HIGH), so previews
   drain first; backfill and schedules submit at LOW; users can change it live with the existing priority API.
   Intro & Credits jobs count toward `max_concurrent_jobs` like any job.
7. **Text detection on the worker's device.** One long-lived helper subprocess per GPU device plus one shared CPU
   helper (`python -m media_preview_generator.markers.credits.textdet_helper`, T-R1 — the roadmap's module name, not
   `markers.textdet`), started lazily by the first marker item on that device; requests from that device's workers
   are serialized. Why a subprocess: the Vulkan loader reads its env once per process, and NVIDIA needs overrides
   (`VK_DRIVER_FILES`, `__EGL_VENDOR_LIBRARY_FILENAMES`) that hide other GPUs — the plex host has NVIDIA + Intel; a
   driver crash or hang can't take the web app down; the WebGPU plugin has a known Linux hang at shutdown without
   adapters (ORT PR #29591). On start the helper runs a 20-frame self-test against CPU and falls back to a CPU
   helper unless it finds exactly the same boxes, corner for corner, faster; result cached per device for the
   process lifetime. A GPU helper that crashes or fails during a request, or that fails to answer between requests, moves its device to the
   CPU helper for the rest of that run of the app, with one WARNING. A helper with no request for 10 minutes exits
   (code 75) and is started again on demand without a new self-test; one within 5 s of that idle exit is replaced
   before the next request instead of racing its own timer. On a timeout, cancel or failure the helper's whole
   process group is killed with a bounded wait, and any pipe a stuck process still holds is handed to a daemon
   reaper instead of being closed on the worker thread. The availability check (`GET /api/markers/sources/local` or
   the first job) runs its `--check` subprocess once per process under a lock; the first caller waits up to 30 s,
   later callers reuse the cached answer (M17). Measured: storage P5000 13.3 vs 18.7 ms; plex TITAN RTX 4.8 vs
   7.7 ms (GPU kept); plex Intel UHD 770 16.1 vs 8.0 ms (→ CPU). Device mapping: worker device (CUDA index / render
   node) → PCI bus id → EP device with the same `pci_bus_id`, with no PCI match falling back to the CPU except on a
   single-WebGPU-device host with an unknown worker PCI address (T-R3); phase 3 must prove the EP honours the chosen
   device on a two-GPU host (plugin README: it "selects the physical GPU independently") — open until Task 13 row
   14 (§13).
8. **Per-job pause** for Intro & Credits jobs: change the job pause/resume routes to set the job-level flag for
   `kind=intro_credits` (global pause still pauses everything). This is what "pause a long backfill without touching
   previews" needs; today it is not possible.
9. **Webhooks:** `_execute_webhook_job` submits the preview job as today, then an Intro & Credits job for the same
   files when any owning server has markers enabled, items grouped by season folder. No extra debounce: the lower
   priority already runs it after the previews.
10. **Cancel:** online lookups check `cancel_check` between requests; text detection is asked chunk by chunk
    (64 frames) and the decode checks for cancel between chunks; ffmpeg steps use the existing cancellation path.

## 7. UX

Mockups with real data (owner-approved direction): artifact link in §0. All non-obvious controls get ⓘ tooltips.
Show a mockup and confirm wording before building each screen.

1. **Servers page → server card → Edit → new tab "Intro & Credits"** (after "Webhook & Scanner"). Per server:
   - Switch "Send intro & credits markers to this server".
   - Libraries with checkboxes (sports-type unchecked by default).
   - Status block. Plex: write method (database), Plex Pass state, DB location + local-disk check, Plex's own
     detection on/off, "When Plex has its own markers: Use ours / Keep Plex's". Jellyfin/Emby: plugin name, installed vs
     required version, Install/Update button, which marker types the server can show. Emby: "Install by hand" (guide
     link) when the catalog doesn't list the plugin, "Can show: Intro · credits start" with the no-credits-end tooltip,
     and "When Emby has its own markers: Use ours / Keep Emby's".
   - Plex only: turning it on asks the database-write confirmation (once per Plex server; stores
     `db_write_confirmed_at`): no API exists; tested on Plex 1.43, stops if the DB looks different; must be same
     machine; Plex re-detection replaces ours and we put them back; viewers need Plex Pass or Plex Home.
   - Servers page cards themselves are unchanged.
2. **Settings → Intro & Credits** (shared detection only): detect Intros / Credits / Recaps; ordered sources
   (chapters, TheIntroDB + optional key + today's usage
   from its headers, IntroDB.app, SkipDB, season audio, credit text, markers already on servers) with measured numbers
   in ⓘ copy, and under them a "How it decides" note restating §5.5 at the Medium rules (no publish setting since
   2026-09-24, §14). The credit text row is live and its switch works (no longer a disabled placeholder): it shows the
   same "Not available" badge and reason as season audio's row when this container can't run it (Task 10's copy).
3. **Preview Inspector → "Intro & Credits" tab:** decision lane + evidence lanes (Chapters, Season audio, Credit
   text, online sources, each server's current markers) in two zoom windows (first / last 3 min); per-server "will
   add / will replace"; Adjust, Lock/Unlock, Re-detect, Publish. Adjust also opens on a file where nothing
   was found: a type with no marker carries `+ Add <type>`, which seeds a round starting time (intro/recap 0:00–0:30,
   credits the last 60 s, preview the last 30 s) to drag. Save = lock = publish. Recap and preview stay editable,
   with a per-server note (only Jellyfin shows them); an edited Emby credits end carries the Emby note.
4. **Inspector → Season view:** per-episode intro/credits, evidence chips, per-server dots, "Needs review". "Publish N
   to M servers" = a normal-priority Intro & Credits job for exactly that season's episodes, named `Intro & Credits:
   <show> · Season N` (or `· Specials`); an identical pending or running job is reused (R4). Review opens that
   episode, and every row's **Edit** opens that episode in the marker editor.
5. **Dashboard → job queue:** Intro & Credits jobs linked under the preview job; per-server "Markers written × N /
   reused / needs review / skipped (reason)"; source counts. A job with files a server hasn't added yet stays one row,
   retried like a preview job: pending with the "Retry N/M" chip and "Retry starting in …" while its hidden retries
   run (§14 2026-09-23).
6. **Setup Health:** plugin missing/outdated, Plex Pass missing, Plex marker tag row absent, Plex DB not local, Plex
   detection overwrite risk, and — when a Plex marker agent is set up — the agent's connection (unreachable, key
   refused, version mismatch, beside a different Plex). Built (phase 4): a `markers` section of the previews-readiness
   envelope, only for a server with Intro & Credits on (one "off" row otherwise, emitted `recommended` + `ok: true`
   because `servers.js _partitionChecks` drops `info` rows); documented in `docs/guides/previews-readiness.md`.

## 8. Settings and migration

`upgrade.py`: `_CURRENT_SCHEMA_VERSION` 14 → **15**. Adds, default **off**:
```json
"markers": {
  "detect": {"intro": true, "credits": true, "recap": false},
  "credits_window": {"tv_s": null, "movie_s": null},
  "sources": [
    {"id": "chapters", "enabled": true},
    {"id": "theintrodb", "enabled": false, "api_key": ""},
    {"id": "introdb", "enabled": true},
    {"id": "skipdb", "enabled": true},
    {"id": "season_audio", "enabled": true},
    {"id": "credits_text", "enabled": true},
    {"id": "server_markers", "enabled": true}
  ]
}
```
v15 also seeded `"publish_when": "high"`. Schema **16** (2026-09-24, §14) removes it: `validate_global` drops the key
whatever it says, `_migrate_to_v16` deletes it (a user-facing note only when it was `"high"`) and sets
`_markers_decide_again`, and the next start (`web.app._decide_again_after_upgrade`, after revived jobs are
started) queues one NORMAL job, **Intro & Credits: Needs review and waiting files, decided again**
(`triggers.submit_decide_again`, source `decide_again`), then clears the key. The job lists, when it runs, the files
with a type in Needs review (`MarkerStore.files_in_review`) and the files whose last row on a server is "Waiting for
this item's other versions to agree on: …" (`MarkerStore.files_waiting_for_other_versions`, `outcomes.VERSIONS_WAITING`;
12 were stuck on the owner's server), and decides them from stored answers only (`PipelineContext.stored_answers_only`:
no online lookup, no detector, no read of the markers on servers, no Season job; a file changed on disk is skipped);
publishing runs as in any job, and its retries keep stored-answers-only. With Intro & Credits off everywhere, or no
such file, only the key is cleared. The detection fingerprint still hashes `publish_when` as `"medium"`, so a Medium
install keeps its hash.

`credits_window` (added after v15, no schema bump: a block without it reads as Automatic) is `null` (Automatic: 450 s
TV, 900 s movie and unknown kind) or one of 300, 600, 900, 1200, 1800 seconds per kind. It is part of the detection
fingerprint only when not Automatic, and a chosen window stores its credit text answers under a version of its own
(`CREDITS_TEXT_VERSION` + window seconds × 1000), so an answer read from another window is asked again while
Automatic keeps the version it always had. A longer window costs a longer decode for every file. The movie credits sanity bound (`decide.MOVIE_CREDITS_MAX_FROM_END_MS`, 900 s) follows a movie window above it, so a longer window's findings are not discarded. The removed
`respect_locks` key is ignored when found in an older `settings.json`.
Per server (`media_servers[]`):
```json
"markers": {
  "enabled": false,
  "library_ids": null,
  "plex": {"db_write_confirmed_at": null, "on_plex_redetect": "restore"}
}
```
`library_ids: null` = all libraries except sports-type. `plex` block only on Plex servers; Emby servers carry
`"emby": {"on_emby_redetect": "restore"}` instead (`keep_emby` = "Keep Emby's"). TheIntroDB key is a secret:
never logged, masked in UI and API responses.

`MEDIA_PREVIEW_TEXTDET_MODEL` overrides the text detection model path for development and the harness — it is not a
setting, and `settings.json` never stores it (§5.4).

## 9. Codebase touchpoints (verified 2026-09-13 on `dev` @ d47e376)

| Area | Where |
|---|---|
| Owners of a path | `servers/ownership.py` `find_owning_servers()`; `servers/registry.py` `ServerRegistry.find_owning_servers()` |
| Preview pipeline (pattern to mirror, don't modify) | `processing/multi_server.py` `process_canonical_path()` |
| Job engine | `jobs/dispatcher.py` `JobDispatcher` (`_run_check`, `_assign_tasks`, `_get_next_item`), `jobs/worker.py` `WorkerPool`, `Worker._process_item` (`_run_once` → `process_canonical_path`), `_find_available_worker(cpu_only=…)`, `jobs/orchestrator.py`, `web/routes/job_runner.py` (`_build_selected_gpus`, `pause_check`), `web/job_gate.py` |
| Jobs / priority / pause API | `web/jobs.py` (`Job`, PRIORITY_HIGH/NORMAL/LOW, `incoming_job_priority`), `web/routes/api_jobs.py` (`pause_job` → global today, priority change) |
| Vulkan device + env | `gpu/vulkan_probe.py` `get_vulkan_device_info()`, `get_vulkan_env_overrides()` |
| Frame cache (not used by markers) | `processing/frame_cache.py` `FrameCache`, `get_frame_cache()` |
| Preview ffmpeg (keyframe-only `-skip_frame:v nokey`, `fps=…:round=up`, 320×240 fit, JPEG `-q:v`) | `processing/generator.py`, `processing/ffmpeg_runner.py` |
| Server clients | `servers/plex.py`, `servers/jellyfin.py` (`install_plugin`, `PLUGIN_REPO_URL`), `servers/emby.py` |
| Plex config folder / bundle paths (DB path is derived from the same `plex_config_folder`) | `output/plex_bundle.py` |
| Settings / migration | `config/__init__.py`, `web/settings_manager.py`, `upgrade.py` |
| Server Edit dialog tabs | `web/templates/servers.html` (`#edit-tab-general`, `-health`, `-libraries`, `-paths`, `-excludes`, …) |
| Inspector | `web/templates/bif_viewer.html` ("Preview Inspector", page route `web/routes/pages.py` `bif_viewer()`), data API `web/routes/api_bif.py` |
| Setup Health (Intro & Credits rows) | `markers/readiness.py` (rows), `servers/plex.py`, `servers/jellyfin.py`, `servers/emby.py` `previews_readiness()` (`servers/base.py` documents the envelope), `web/routes/api_servers.py`, `web/static/js/servers.js` `renderReadiness` |
| Webhooks | `web/routes/api_plex_webhook.py`, `api_vendor_webhook.py`, `web/webhooks.py` (`_execute_webhook_job`, `webhook_delay` debounce) |
| Jellyfin plugin | `jellyfin-plugin/` (net9, v10.11.0.3), `.github/workflows/jellyfin-plugin.yml` (tag push / dispatch) |
| PR image | `.github/workflows/docker-pr.yml` (`pull_request_target` to main/dev, label `build-docker`, ignores `docs/**`) |

## 10. Testing

### 10.1 Unit (pytest, mocks per `.claude/rules/testing.md`)
- Plex `extra_data` merge matrix: NULL / empty / existing intro / both × add intro / credits final / non-final.
- Tag-row lookup (`''` vs NULL vs missing); +2 s shift; multi-part agreement; network-filesystem detection.
- Decision rules §5.5 as a full matrix (locks, chapters, 1 vs 2 sources, dependent sources, sanity bounds).
- Publishers assert request kwargs / SQL parameters, not call counts.
- Config migration 14 → 15; per-server filtering (enabled × library_ids × owner type).
- Job kinds: previews path kwargs unchanged; intro_credits items routed to `process_fn`/`check_fn`; GPU worker vs CPU
  worker × decode ok / GPU error → CPU rerun; per-job pause only affects `kind=intro_credits`; priority ordering
  previews HIGH before markers NORMAL/LOW.
- `frames.py`: tail length by kind (T-R4), row order and non-increasing rows (T-R5), luma/pts rounding and the
  `-copyts` start-time subtraction (T-R6), chunked decode and cancel between chunks, decode/timeout error mapping
  (T-R7).
- `textdet_helper.py`: self-test picks GPU/CPU by the exact boxes both sides find, not their count and not speed
  alone; crash or a between-request failure → CPU for the process lifetime; idle exit and the 5 s replace margin (T-R8); process-group kill and pipe
  reaper; device → PCI mapping (T-R3); the availability check's once-per-process lock (M17).
- `detector.py` and rule J: the anonymised 80-file fixture (`test_reproduces_the_spec_table`) and the decision
  matrix through `decide()` — chapters/credits-text-alone/agreement cells exist and pass.
- Online clients: header pacing, 429 backoff, key masking.

### 10.2 Accuracy harness (move into repo under `tests/eval/` or `tools/eval/`, not in CI)
- TV intros: 118 episodes (`evidence/eval/named_seasons.json`, truth from chapters) → report useful/wrong/missed.
- Credits: 80 files (`evidence/credits/movies40.json`, `tv40.json`, `adjudicated.json`) + the 205-movie chapter set,
  run via `python -m tools.markers_eval credits-text` (`tools/markers_eval/README.md`); results in
  `evidence/eval/phase3-harness.md`.
- The text detection bench (`289`-frame set) checks the vendored detector against `rapidocr_onnxruntime` 1.4.4
  before use.
- Online: 43 verified cases.
- Required before merging any detector change; results pasted in the PR.

### 10.3 Lab (storage) — `evidence/lab/up.sh`
Emby 18096, Jellyfin 10.11 18097, Jellyfin 12.0 18098, Plex 32402 (127.0.0.1 only; tokens in `evidence/lab/env`).
Re-run the §3 behaviour matrix for every publisher change: write, serve, refresh/scan/forced detection/restart,
client skip button via Playwright on the synthetic VP9 episodes (`evidence/lab/synth/`). Plex end-to-end needs a
claim token from the owner (lab Plex is unclaimed → no Pass → nothing served).

**Live testing with the app (owner: keep the lab for future dev testing too).** Run the branch's app image on storage
as its own container (own config dir, e.g. `mlab-app`, port on 127.0.0.1) wired to the lab servers: the lab
servers' media paths mounted read-only at the same paths, and the lab Plex's `mlab_plex_config` volume mounted so the
Plex DB is on the same host (the §3.1 rule). Test end to end: webhooks/scans → Intro & Credits jobs → markers on all
four servers → skip buttons (Playwright). Grow the lab library as tests need it (read-only mounts): a show with 2+
seasons and weekly-style single episodes, a multi-version item, movies from `evidence/credits/framechecks/`. Plex:
claim the lab server with the owner's token at the start of phase 1 (Pass needed to serve markers). Before the
evidence folder is trimmed, move `lab/up.sh` + `lab/env` to a stable dev-tools location.

### 10.4 Plugins
C# builds for each target ABI in CI; smoke test on lab containers before any release.

## 11. Build and release plan

- Branch **`feat/markers-detection`** = draft PR #241 → `dev` (reused; GitHub can't change a PR's head branch).
  Merge `dev` in whenever the PR shows "out-of-date".
- Every push: `ci.yml` tests, and on a PR also builds the arm64 image natively and checks credit text detection in
  it (no push, no secrets; the published image's arm64 build only runs on `dev` pushes, tags and manual
  dispatch). Label **`build-docker`** → `ghcr.io/stevezau/media_preview_generator:pr-<N>` (amd64; a PR whose
  changes are all docs doesn't build).
- Spec + evidence are on the branch in `docs/design/intro-credits/` (tokens, dumps, synth media, Emby DLLs
  gitignored). Trim or remove before release.
- Plugins built locally for the lab during the build; public releases (Jellyfin manifest, Emby catalog) only at merge.
- Real servers: `pr-<N>` image as a **second container on `plex`** with its own config dir, one show first, only
  after the owner's OK. The prod Plex DB is never written until the owner enables it for that server.
- Merge to `dev` only when the owner says it's fully tested.

## 12. Phases (each ends usable, tested in the lab, owner-reviewed)

1. **Store + online/chapters + Plex & Jellyfin.** `markers.db`, file identity, chapter source, TheIntroDB/IntroDB/SkipDB
   clients, decision rules, Intro & Credits job type + triggers + dashboard rows, Plex publisher, Jellyfin Bridge
   extension (two builds), per-server Edit tab, Settings section, read-only Inspector tab, config migration.
   *Done when:* chapter- and online-covered files get correct markers on lab Plex (claimed) and both Jellyfins,
   survive the §3 wipe matrix, and the owner has seen it on one real show via the side-by-side container.
2. **Season audio intros + Emby.** Fingerprint cache, season step, v3 matcher, weekly-release handling, reconcile loop,
   Emby plugin + catalog submission, Season view, eval harness in repo. *Done when:* harness ≥ the §5.3 numbers,
   Emby skip button in the lab.
3. **Credits text.** Keyframe tail sampling, rule J, the larger hand-checked set. **Built:** the harness gate
   (owner's Q4) passes 5 of 5 on the 80 (movies40 + tv40) on both decode paths and fails 3 of 5 on the 205-movie set
   — a disclosed detector-gap limitation, not tuned around (§5.4, §13). Ships at "Medium"; at "High" it needs a
   second source until that gap closes. *Done when* (Task 14): lab-proven, with the shipped image's digest recorded.
   **Done (2026-09-19), then final-reviewed across the whole PR (2026-09-20):** rule J version 3 meets §5.4 on both
   decode paths (GPU 66 within 10 s, CPU 61, 1 early each; version 2 was 64 and 59); the lab matrix is **16 of 16** on `final-2` (`bd9e561`,
   `evidence/lab/phase3-results.md`), with phase 2 24 of 24 and phase 1 16 of 16 on the same image, a scale run over
   711 real files, and the published `pr-241` image's re-run of rows 1, 2, 3, 16 and phase 2's row 24 (an earlier
   `pr-241` build ran one real season on `plex`). The
   review also closed one of the two bugs the scale run exposed (Plex's own migration leaving parts the publisher
   refused) and narrowed the other (the app's earlier markers read back as a server's second opinion: fixed for Emby,
   still open for Plex, §13 item 17), and recorded one accuracy risk on broadcast TV (§13 item 15). Tests: 11,429
   unit/integration (88.84 %), 363 e2e, the CI integration selection, all green.
   Waiting on owner review.
4. **Polish.** Adjust/Lock editor, AniSkip, Setup Health checks, helper container for Plex on another machine, docs.

## 13. Risks and open items

1. **TheIntroDB terms** — used without authorization; per-user keys; must degrade gracefully.
2. **Plex DB writes are unsupported by Plex** — opt-in with confirmation; unknown schema → stop.
3. **Plex client display** — proven at the API (served XML identical to native) and in Plex Web 4.160.0 (Skip Intro
   and Skip Credits shown and landing at the marker ends, phase 2 close-out row 20). Native Plex apps not tested.
4. **Plex Pass for viewers** — non-Pass viewers never see skip buttons; say so in the UI.
5. **Emby catalog acceptance** — not guaranteed; manual install fallback (built: the Edit tab's "Install by hand"
   and the guide's manual install). Catalog text is written (`emby-plugin/README.md` "Catalog submission"); not
   submitted — waiting for the owner's Emby forum thread / developer id (roadmap owner checkpoint 4).
6. **Multi-version items** — Plex shared markers + Jellyfin alternate versions need a lab test in phase 1.
7. **Credits truth is subjective** (epilogues, names over footage, post-credit scenes) — adjudications recorded.
8. **Jellyfin 12.0 catalog install** of a dual-ABI manifest is untested (manual load works).
9. **TheIntroDB keyed limit** (1,000/day) unverified.
10. **AMD credit text detection is untested** (no hardware); the self-test decides GPU vs CPU per device (Q6), the
    same Vulkan/RADV path already proven on NVIDIA and Intel.
11. **Dolby Vision profile 5 credits text is unmeasured** — 0 of the 80 hand-checked files and 0 of the 205-movie
    set are profile 5 (`evidence/credits/phase3-measurements.md` M5); the detector reads the base layer's luma like
    any file, but this hasn't been checked against a real profile-5 credit roll.
12. ~~**Dawn's device choice on a two-GPU host is half proven.**~~ — **closed on the final image** (`final-2`,
    `bd9e561`; `evidence/lab/phase3-results.md` "Final (final-2, bd9e561) — plex rows"). The WebGPU EP's own device
    selection doesn't choose the physical adapter Dawn runs on (T-R3), so row 14 now checks the hardware itself: all
    8 WebGPU helpers carried `--pci-bus-id 0000:00:02.0` and 2.08–2.81 s of `drm-engine-render` time in their own
    Intel DRM fdinfo, and none had a DRM file open on another device. The NVIDIA driver publishes no DRM fdinfo
    counters, so its half stays an absence check: `nvidia-smi pmon` listed no text detection helper on the TITAN in
    36 samples. Either way the Intel self-test chose the CPU on that iGPU, so nothing beyond the self-test's own
    frames ran on it.
13. ~~A lone credit-text keyframe inside a scene, within 24 s of the roll, joins rule J's run and extends it over
    that scene~~ — **fixed in rule J version 2** (§5.4): the end steps back over a lit credit keyframe glued on after
    a scene frame, and the 1 fps walk stops where the scene starts. Whether there is an end is still decided from the
    latest credit keyframe, so the step back never makes one. Measured on Rick and Morty S01E04's CPU decode (the
    skip ended 11.5 s into the scene; it now ends on the roll's last frame); no end in either set moved.
14. **Credit text answers late when a roll's opening section is names over bright footage, or is split by a gap
    longer than 24 s** (rule J's `coarse_start` takes the roll's last run) — the one measured reason on-screen
    credit text alone misses the usefulness and precision gate on the harder 205-movie set (§5.4 "Harness"; owner,
    2026-09-18). It ships at "Medium" now; at "High" it still needs a second source until this is fixed. **Narrowed
    by rule J version 2:** the anchor no longer steps off a roll's first card across more than 24 s of dark frames
    (205: Medium useful 90 → 96, no early answer added). **Narrowed again by rule J version 3**, which reads where a
    frame's text is: an earlier run in the last run's own band whose text never stops is the same roll, and a keyframe
    before the start in that band, at the roll's own cadence, is more of it (§5.4 steps 5 and 7). 205: Medium useful
    96 → 101 with one wrong answer fewer (17 → 16), credits text alone 116 → 123 useful with 41 → 35 late; the 80:
    Medium 58 → 60 on the GPU decode and 55 → 57 on the CPU, rule J alone 64 → 66 and 59 → 61 within 10 s. No early
    answer was added at any level on either set or either decode path and no end moved. The candidates that reach
    back **without** reading positions all added one (`evidence/eval/phase3-harness.md` "Tried and not taken" and
    "The band's two numbers, swept").
    **What is left of it** is the roll the detector finds no text on at all: of the 41 files version 2 answered more
    than 30 s late, only 34 have a text box within 30 s of the chapter for any rule to reach for (Kill Bill Vol. 2's
    cast cards start 88 s after its chapter; Black Panther's mid-credits animation boxes nothing for 16 s), and 35 are
    still late. **The anchor's limit carries a risk the other way:**
    the frame it keeps may be lit (3 of the 9 on the sets, all on the roll), so scene text on a lit frame followed by
    more than 24 s of dark keyframes before the roll becomes the start, early by that stretch; no set file has that
    shape, and nothing in the rows tells it from a lit title card. Likewise subtitles on a dark scene after text-free
    story, joined to the roll, start early — and version 3's reach back carries the start to the first subtitled
    frame, so where the tail then holds under 30 s of rows before it the answer is refused altogether rather than made
    early (`test_rule_j.TestTextAllThrough`); version 2's text-all-through step only catches text on screen through
    most of the tail before the run (§5.4 step 8). **Version 3's own cost is the same shape at the head of a roll:**
    where a scene's own text sits in the roll's band and recurs at its cadence, the walk takes it. No file of the 80
    or the 205 has that shape — no verdict changed at any level on either decode path — but broadcast TV is full of it
    (item 15). **And version 3's two halves cancel on one shape:** a roll that fills most of its own tail and that
    the join split has its opening block and its names over footage in front of the last run, which is where step 4
    looks for its story, so the roll's own text is gathered as an overlay and step 5 refuses the merge, usually
    leaving the answer where version 2 had it. No file of either set, the lab or the 51 broadcast recordings has the shape, and
    it is pinned (`test_rule_j.TestOverlayBoxes.test_a_split_roll_can_be_gathered_as_its_own_overlay`) rather than
    fixed, because the three fixes measured each cost a real broadcast answer. Step 4's own bound (item 15) applies
    here as everywhere: it can also put a start up to the 24 s join *earlier* than version 2, likewise unobserved on
    anything measured.
    Its third shape — a roll whose first card sits just
    above the luma-30 dark line and whose other credit keyframes span under 15 s (Rick and Morty S01E04 on the GPU
    decode, no answer) — has a measured candidate, 2 boxes at luma 30–33, that answers it and six more of the season
    within 1 s but put one 205-set movie's start on its epilogue cards; not shipped. **Rolls the tail cuts into:**
    version 2's 30 s floor (§5.4 step 8) left a roll starting before the tail, or in its first 30 s, without an
    answer. Round 3 reads the 120 s before the tail when nothing lit comes before the run in the tail, and keeps it
    when the run carries on into it. That answers the lab's five Heeramandi episodes on the roll's first card. A
    roll that starts 0–30 s into the tail after a scene still gets none, nor does one whose first card is the tail's
    first keyframe. So did one that began more than 90 s before the tail; since 2026-09-23 the steps go on while the
    run starts under 30 s after the first row read, back to 30 s before the earliest start the decision keeps. So does
    one whose only card before the tail the anchor steps over. The lab's chapter truth has none of them.
15. **Credit text on broadcast TV with on-screen graphics is still less accurate than on anything else** — 51
    frame-checked broadcast files (39 episodes of 13 shows with a channel or show logo, 12 sports feeds;
    `evidence/eval/broadcast-tv.md`). On a dark story frame one box is enough (a channel logo alone); on a lit one, a
    logo plus a two-line promo lower-third; name captions, in-show graphics and epilogue cards do the same.
    **Narrowed by rule J version 3** (§5.4 step 4: text in one place right across the story doesn't count as text
    inside the run, and a run left with fewer than two credit frames is not a roll), and the same version's band
    steps (5 and 7) cost it something back. Step 4 is not monotone: thinning a run grows the spacing the anchor
    measures, so it can put a start up to the 24 s join *earlier* than version 2, on story — pinned
    (`test_rule_j.TestOverlayBoxes.test_thinning_a_run_can_stop_the_anchor_stepping`), unobserved on the 51, the two
    sets and the lab, and true of the overlay step shipped alone as much as of the pair. Measured after the rule was fixed, never tuned on:
    - **GPU decode, strictly better.** At Medium it skips story on about 1 episode in 10 (4 of 39 against 6,
      counting the one episode with only an end card; Plex 6; the Q4 gate's cap is 2 here, as on the 80), with the
      same useful answers as before and as many as Plex (10 of 34 rolls), and still puts credits on 4 of the 12
      sports feeds (Plex: 6). High doesn't move.
    - **CPU decode, one right answer worse.** Medium useful 11 → 10 and Medium wrong stays 5; High is unchanged at 2
      wrong plus 1 false. The overlay step cancels most of what the band steps cost here but not all: MasterChef
      Junior S03E02's contestant name caption isn't an overlay at all, and Bondi Rescue S14E12's network promo
      strapline breathes enough to split into groups that each span under 80 % of the story. Both are in the roll's
      band and at its cadence, and the reach back takes them (frame-checked). Against them the pair gives back Bondi
      Rescue S15E03-E04, wrong → useful on its end card.
    - The Q4 caps fail here under every version. Both High wrong answers are credit text and Plex agreeing on an
      in-show graphic or an epilogue card, and the CPU's High false answer is the same pair.
    - What is left is what the overlay step can't see because the text moves, comes and goes, or redraws itself:
      in-show graphics (Top Gear's Stig-lap telemetry), name captions, epilogue cards, and the changing text of
      sports studio segments.
    - Tried and not taken: ignoring boxes that sit in one place on at least half of the **tail's** keyframes (the
      final review's prototype: no answer moved on the 80 or 205; broadcast +2/−1 on each path — version 3 differs in
      measuring the story rather than the tail, in picking the run before dropping anything, and in refusing a run
      the overlay empties); suppressing the overlays before the runs are found (it moved one broadcast answer 209 s
      earlier onto story); a "boxes in a screen corner don't count" test (it took two real answers off the sets
      outright); and not counting text + a server's own marker as agreement at High (every broadcast High mistake
      gone, but every High credits answer on the 80 and 205 too).
16. **The Jellyfin plugin flushes its marker store file but not the folder** (fa3772e): after a power cut the rename
    itself can be lost, leaving the previous store file (or none on a first write), never an empty one; the app writes
    the markers again when a later job that includes the file, or a Check servers run, reads the server back and finds
    them changed. Lab-verified order on both ABIs: write → fsync → rename (`evidence/lab/phase2-results.md` "Jellyfin
    plugin fa3772e"). The Emby plugin's store has the same shape (`Flush(true)`, then replace or move; not traced).
17. **Markers this app wrote to Plex read as Plex's own after markers.db is lost** (a reset or reinstall, a server
    re-added under a new id; scale run 2026-09-19 finding 2). Plex records nobody's ownership of a marker, and the
    app's record is only in markers.db. The first run then reads them as a second opinion, and that stored answer stays
    for the file until the file changes (a server we have published to is never read again). On the scale run they
    never set a checked edge, decided 6 credits together with Emby's (neither alone was needed), shortened 57 credits
    and 36 intros, and put 1 credits and 2 intros into Needs review; removing only Emby's changes nothing. Jellyfin's
    and Emby's are told apart through their plugin's store (§14 2026-09-19). Owner decision pending: a key of ours in
    each part's `extra_data` recording what we wrote (a lab key of ours survived Plex's analyze and its credits
    `final` migration, which each rewrote the part in the other form, and a forced refresh; Plex's forced credits
    detection is untested;
    `evidence/lab/phase1-results.md` "Findings 1 and 2"), or accept this as a known limit. **A lock makes the same loss worse**: a lock lives only in `markers.db` (Plex has no
    place for it), so losing that file loses the user's own edit, not just its provenance. A marker of the user's that Plex
    still shows then reads as Plex's own, and under "Keep Plex's" it is kept as such, but nothing marks it locked.
    Known limit until the owner decides the `extra_data` key above.
18. **The Emby plugin's in-flight `Replacing` set needs a plugin release to be told apart.** The plugin saves its
    store before it writes the chapter rows, so after an Emby crash in between the item can still show the set the
    write is replacing. The app reads those rows as ours from `Replacing*Ticks`, which `emby-plugin/` now answers but
    no released build does; until the next plugin release, an Emby that crashed mid-write plus a lost markers.db can
    still show markers of ours as its own (§14 2026-09-19). **Release order:** no `emby-plugin-v*` tag or Emby plugin
    release exists yet, so cutting the first one at or after `fb32a88` closes this before anyone can install a build
    without it.

## 14. Decisions log

- 2026-09-13 · Emby = our own self-healing plugin, submitted to the Emby catalog.
- 2026-09-13 · Credits land on the real credit roll, not epilogue cards.
- 2026-09-13 · TheIntroDB used without written permission, user-supplied key optional; ToS risk accepted.
- 2026-09-13 · Plex = DB write on Plex Pass servers, only when the app shares a host with Plex; otherwise read-only
  with a message; helper container on the Plex host later.
- 2026-09-13 · Name "Intro & Credits". Online sources TheIntroDB + IntroDB + SkipDB, all validated.
- 2026-09-13 · Separate Intro & Credits job type (owner chose it for control — don't re-propose merging into preview
  jobs). Publish rule is a setting, default "chapters or two sources agree". Off until turned on; sports excluded.
- 2026-09-13 · Several servers per type → per-server switch/libraries/status in each server's Edit dialog; only
  shared detection in Settings.
- 2026-09-13 · Build on `feat/intro-credits` with a PR image; nothing into `dev` until fully tested. Spec + evidence
  stay current until release. Spec must be self-contained for a `/clear`.
- 2026-09-13 · Research/test jobs must not overload storage; use the GPU where it helps.
- 2026-09-13 · Credits job samples **keyframes of the tail itself**, not preview frames (owner asked for the most
  reliable option: best score 59/80, fewest misses 4 vs 10, no dependency on previews existing, the preview interval,
  or the 60-min frame cache). Replaces the earlier "reuse preview frames".
- 2026-09-13 · A season's first episode may use the previous season's fingerprints as a hint that needs a second
  source (owner: yes).
- 2026-09-13 · Marker work uses the existing GPU/CPU worker pool, priorities, gate and cancel (owner); per-job pause
  added for Intro & Credits jobs; webhooks submit markers at NORMAL after HIGH previews (§6.4).
- 2026-09-13 · Text detection must work on every GPU type (owner). Proposed and proven on NVIDIA in the app image:
  ONNX Runtime WebGPU plugin over Vulkan (+16 MB), app Vulkan probe env, self-test guard, CPU fallback (§5.4).
  Intel tested on the plex host (iGPU slower than CPU there → CPU); AMD untested, no hardware.
- 2026-09-13 · Build approved. Reuse PR #241 (branch `feat/markers-detection`) instead of a new `feat/intro-credits`
  PR; dev merged in; commits/pushes on that branch without per-commit asks; spec + evidence tracked on the branch.
- 2026-09-13 · Implementation plan written (`plan-roadmap.md`, `plan-phase1.md`). Decisions taken while planning:
  SkipDB via its read API instead of the daily dump (no 29 MB daily download, fresher data, duration matching; the
  ODbL reciprocity term exempts read-only API use) — only `exact`/`shifted` matches count. Markers already on servers
  are read from **every** enabled server that owns the file (even with Intro & Credits off there, so Plex's markers can
  confirm a source for Jellyfin), and only before we have published to that server. Plex writes replace only the types
  we decided; Plex's own marker for an undecided type stays. Intros and recaps are detected for TV episodes only;
  previews are not detected (no setting). Intro & Credits jobs are created through `POST /api/markers/jobs` (the
  preview job route is untouched). Server external ids come from the path first (`{tvdb-…}`, `{tmdb-…}`, SxxEyy), the
  server only when the path can't answer.

- 2026-09-13 · Plan review (Architecture Review) changes: a webhook's Intro & Credits job waits for its preview job to
  finish before taking a slot (priority alone can't order them when previews are set to Normal/Low). A paused Intro &
  Credits job hands its active slot back until resume. Intro & Credits holds at most a quarter of the checking threads
  (online lookups can sleep on rate limits). Plex writes re-read the item inside the write transaction so Plex's own
  fresh `extra_data` keys survive; the library-wide marker-version sample runs once per job (refreshed every 5 min), each write
  validates the item's own parts. A follow-up doesn't wait while its preview job counts down to a retry: it starts after the preview job's first try. Per-file outcomes gain `markers_waiting` and `markers_skipped`; any server failure makes the file
  count as failed unless another server was written.
- 2026-09-13 · Decision rules tightened during Task 3 review (§5.5): sanity checks apply to chapters too; an intro or
  recap may not run to the end of the file; at "Medium" a single source is refused when another independent source
  (server markers included) contradicts it; published times come only from candidates that agree with a different
  independent source.
- 2026-09-13 · Task 3 review round 2 (§5.5): conflicting groups of agreeing sources → review; the edge the tolerance
  doesn't check takes the safer value; chapters yield to two agreeing contradicting sources; server markers never
  supply times; intro/recap and preview/credits overlaps → review; IntroDB + TheIntroDB always one source.
- 2026-09-13 · Task 3 review round 3 (§5.5): agreement search considers every maximal agreeing set (the greedy search
  could miss a conflicting group and flip with input order); at "Medium" a source whose own candidates disagree doesn't
  publish; `decided_by` includes the sources that supplied either edge.
- 2026-09-13 · Task 3 review round 4 (§5.5): chapter ties pick the earlier end; an agreeing pair from different sources
  outside the tolerance of a result blocks it even when a third source bridges them; "Medium" needs every pair of the
  source's own candidates to agree.
- 2026-09-13 · Task 3 review round 5 (§5.5): the edge the tolerance doesn't check takes the safer value for "Medium"
  single sources and for chapters confirmed by two sources; remaining ties prefer the shorter skip.
- 2026-09-14 · Task 3 final review (§5.5): a second chapter of the same type that another source agrees with blocks the
  first chapter (review); `decided_by` wording per path.
- 2026-09-14 · Chapters (§5.1), from real-library review: a generic "Intro" chapter yields to a specific opening chapter
  in the same file (cold open); chapter ends clamp to the next chapter start; `OP`/`ED` match only in capitals;
  Matroska `ChapterSkipType` deferred (ffprobe can't read it).
- 2026-09-14 · External ids (§5.2), from Task 6 review + lab check: episodes use only show ids (no fallback to episode
  ids), path "movie" needs tmdb/imdb and no tvdb, movies drop tvdb, extras and non-movie/episode items get no ids.
- 2026-09-14 · Online sources (§5.3), from Task 7 live check + review: TheIntroDB's daily budget ends at the UTC day
  change (it sends `x-usagelimit-reset: 0` mid-day); TheIntroDB is only asked with the file duration; empty or
  error-shaped 200 answers and non-JSON 404s count as unavailable, not "no data"; keys with non-printable characters are
  refused before any request.
- 2026-09-14 · Jellyfin plugin (§6.3), from Task 9 review: POST carries optional `fileSize` of the analysed file and the
  provider serves nothing when the item's on-disk length differs (10.11 keeps our stored copy after a file replacement);
  a corrupt store file reads as empty; `segments` is required and types must be exact names; max 64 segments.
- 2026-09-14 · Plex DB publisher (§6.3), from Task 8 deep review: before any write, prove a shared SQLite lock domain
  with Plex (another process's lock on the `-shm` dead-man byte), in addition to the filesystem type; no proof →
  `NeedsLocalDb` (or unreachable when Plex is down). Unknown filesystem types are not ready. Removing a type we
  published deletes its `pv:<type>` key (not `""`) and only rows/keys still equal to what we wrote, so Plex's own
  re-detected markers stay and Plex can analyse the part again (verify in the lab matrix).
- 2026-09-14 · Jellyfin publisher and server-marker evidence (§5.5, §6.3), from Task 10 deep review: a Jellyfin publish
  counts as written only when core `/MediaSegments` then shows our segments (otherwise a clear "provider switched off" or
  "file size differs" problem); `read()` returns only served segments. Plex/Emby markers are not read as evidence for
  an item any file has been published to (Plex serves one set per item across versions). Known limit: after a
  `markers.db` reset or a server re-added under a new id, markers we wrote to Plex earlier can count as Plex's own
  opinion once.
- 2026-09-14 · Plex multi-version publishing (§6.3), from Task 8/11 reviews: publishing is tracked per server item, not
  per file. The Plex publisher computes the item's set from every live version (a type is shown only when every version
  is decided and agrees within 2 s), removes only rows that serve exactly what we last left on that item, and returns
  what is ours afterwards; markers.db keeps that per item. A newly added, not-yet-decided version hides that item's
  markers until it is decided (precision first).
- 2026-09-14 · Job triggers (§6.4), from Task 12 deep review: a webhook follow-up takes the preview job's priority when
  that is lower than Normal (Low incoming jobs still let previews drain first); files the server hasn't indexed yet get
  delayed retry follow-ups on the webhook retry schedule (this also covers servers with markers on and previews off;
  since 2026-09-23 they retry in the job's own row, as preview jobs do);
  Intro & Credits jobs interrupted by a restart and not resumed are marked failed so schedules and follow-ups aren't
  blocked.
- 2026-09-14 · Milestone audit B (§5.5 rules 3–8, §6.2), reproduced on the owner's files: rule 7 lets an agreeing server
  marker shorten a skip (Rick and Morty S01: crowd credits to end of file skipped the post-credits scene on 6 of 10
  episodes; Plex's own markers were right); a chapter-only decision no longer ends the evidence search, so rule 3's
  veto runs in normal jobs (TheIntroDB skipped at Low priority when only confirming chapters); at Medium only chapters
  or SkipDB exact/shifted may decide alone (Demon Slayer S03E05 Blu-ray: a single IntroDB answer skipped 81 s of cold
  open); server markers from importer plugins or another cut of the same item aren't independent evidence; chapter
  rules and online parsers carry versions so stored evidence is re-derived after a rules change. Cost measured on the
  43 verified online cases: with TheIntroDB on, Medium's correct credits drop from 22 to 1 (precision chosen over
  coverage). Evidence scripts: `evidence/audit-phase1/`.
- 2026-09-14 · Milestone audit fix lanes (§6.2, §6.4, §7, §8), from audits A and C: turning a server's Intro & Credits
  switch off, or revoking a Plex server's database-write confirmation, now stops an already-running job's next
  per-file write to that server (it re-reads the setting per file rather than caching it at job start) instead of
  finishing the write on a stale setting. A webhook-triggered file not yet on disk, not only one a server hasn't
  indexed yet, now gets the same delayed retry follow-up (`webhook_retry_count`/`webhook_retry_delay`); a retry job
  is capped at 500 files, the rest wait for the next run. `POST /api/markers/jobs` is CSRF-exempt like `POST
  /api/jobs` (a token script's endpoint, not browser-only). `GET /api/markers/item?server_id&item_id` rejects an
  `item_id` that isn't shaped like that server type's ids (400) before any server is contacted. Inspector re-detect
  reuses an already-queued or already-running forced job for the same file instead of starting a second one. There
  is still no separate "install the Jellyfin plugin" route for markers — the Edit tab uses the existing `POST
  /api/servers/{id}/install-plugin` (per the Task 13 ruling above), whose result now also drops the per-server
  capability cache so the tab re-checks instead of showing the pre-install plugin state.
- 2026-09-14 · Jellyfin plugin (§6.3), from the docs/lab pass: the Bridge's segment provider does no per-item work
  until markers have been pushed to that server at least once (`Supports()` gates on the marker store folder
  existing), so a server that never receives markers pays no per-video provider cost on scans. A new background
  service follows Jellyfin's `ILibraryManager.ItemRemoved` on both ABI builds and deletes a removed item's stored
  markers — broader than the 12.0-only `CleanupExtractedData` hook (file-changed only), which stays as an additional
  cleanup path on 12.0. Jellyfin 10.11 reports only the top item when a whole folder is removed, so a
  scheduled task (after start and daily) also deletes stored markers for items Jellyfin no longer has, once startup
  and library scans are done.
- 2026-09-14 · Read-back verify (§6.3), from the pre-lab lab matrix (Plex forced credits detection and rescans of a
  replaced file wiped or replaced our markers while jobs and the Inspector said up to date): a file whose decision and
  item record are unchanged is up to date on a server only when `shows()` still finds ours there; otherwise it is
  written again (Plex `keep_plex`: Plex's own replacement is left, row "Plex's own markers are kept"). A read failure
  keeps "Up to date" (debug log). Outcome labels follow `last_write_changed`: a write that changed the server is
  "Markers written" (forced restores included), a true no-op is "Up to date". After publishing a replaced file the job
  queues one delayed verify job (first webhook retry delay × 3, at least 10 min, retry cap and switch; verify jobs
  queue no further verify). The Inspector compares credits ends properly (Plex's final flag, Emby's missing end) and
  plans `keeps_plex` for a kept replacement.
- 2026-09-14 · Job outcomes (§6.2 item 7), from lab finding 6 (a file said "Up to date" while Plex waited for its
  versions or Jellyfin 12.0 hadn't indexed it; movie trailers in folder jobs waited and queued retries): a file's
  outcome shows what still needs something, first match wins — failed, needs review, waiting, written, up to date,
  no markers, skipped. A failed or waiting server is never hidden behind a written or up-to-date one (this replaces
  the 2026-09-13 "failed unless another server was written"), and a file with any marker type in review counts as
  Needs review even when its agreed types were published. Retries and verify jobs still read the per-server rows.
  Extras (`external_ids.is_extra`: a Plex extra suffix, or an extras folder as the parent) are skipped before any
  probe, owner or item lookup ("Extras aren't checked for markers"), from folders, webhooks and library listings.
- 2026-09-14 · Read-back review fixes (§6.3, §6.4), from the read-back deep review: "Keep Plex's" is per type and
  persistent, enforced in the Plex publisher's write (`kept_types` recorded with the item record): once Plex's rows
  replace ours of a type, no path (skipped/failed/waiting rows, multi-version waits, forced runs, Re-detect, sibling
  versions) deletes them until the server is set to restore or Plex has no rows of that type; other types are still
  written; a first publish still replaces Plex's rows. Rows and the Inspector name the kept types ("Keeping Plex's
  credits"). Jellyfin skips the POST for an unchanged set only when the plugin's stored file size matches the file on
  disk. A Plex Pass unknown answer is reused for 5 s; read-back failures raise one job warning per server; verify jobs
  come only from sent-file jobs, keep the retry count across the chain, never queue another verify, and don't retry
  a missing file. The Inspector ignores another Jellyfin provider's segments beside ours and counts Plex versions as
  `Media` entries without `proxyType`.
- 2026-09-14 · Keep Plex's re-review (§6.3), from the scoped re-review of 2073f3d: under `keep_plex`, a decided type's
  Plex rows are kept whenever they aren't provably ours (not what the write would show, not the item record, not the
  moved file's record), which replaces "a first publish replaces Plex's rows" on keep_plex servers and covers a type
  we removed ourselves before Plex filled it and a lost kept state (markers.db reset, re-added server, new item id);
  rows equal to what the write would show are ours (a record lost after Plex's COMMIT). `write` no longer returns
  before reading the rows while a type is kept, so a kept type Plex dropped is released even with nothing to write.
  `published_to_item` counts an item whose types are all kept.
- 2026-09-14 · Owner approved the keep_plex semantics (Plex's own markers are kept whenever Plex has them). The Plex
  setting is renamed "When Plex has its own markers": "Use ours" (`restore`) / "Keep Plex's" (`keep_plex`); stored
  values unchanged. Its tooltip covers both before and after we publish.
- 2026-09-14 · Lab scale run F3 (§5.5 rule 6): at "Medium" SkipDB `exact`/`shifted` alone decides intros and recaps
  only, never credits or previews. Offline replay of the scale run: all 25 lone SkipDB credits that Medium added
  started more than 3 s before prod Plex's (frame-checked: Battlestar Galactica S04E02 4.4 min early, S04E05 6.7 min,
  The Office S02 12 s); after the change all 25 are Needs review. Evidence: `evidence/audit-phase1/scale_replay_s1.py`.
- 2026-09-14 · Lab scale run F2 (§5.5 rule 7, §6.2 step 3): after credits or a preview are decided, a server's own
  detection markers may move the start later. Avatar (2009) and Innerspace (1987) "End Credits" chapters start on the
  last story shots; prod Plex's credits are served 18.5 s and 25.6 s later and are right. First cut ("latest start
  across all server markers") pulled Avengers Infinity War's credits to Plex's last 12 s piece, so the rule is per
  server: any own marker covering the decided start or starting within 10 s of it blocks it; otherwise each server
  offers its first start inside the skip and the latest offer wins; importer-plugin markers never shorten. Frame
  check (lab scale run, 16 shortened credits, 5 shortened intros): 4 of 16 old credits starts skipped story (Avatar,
  Innerspace, Congo; Rocky Aur Rani Kii Prem Kahaani by a 5 min epilogue) and no shortened start did (0 unsafe; 12
  were later than needed, since Plex's credits start at the plain text crawl); all 5 old intro ends were right and
  Plex's intro ends partway through the opening, so intros and recaps are not shortened. Replay with prod Plex's
  markers: 40 credits shortened (39 chapter decisions), 0 intros, nothing else, nothing without server markers. The
  shortening runs after the overlap checks (rules 9–10) and only on types still decided, so a preview they held back
  stays in review. With everything decided, the pipeline still reads a server never asked for the file (not one
  already answered, empty, unreadable or another cut), and a chapter decision backed only by server markers keeps the
  evidence search open. The Inspector says e.g. "Shortened to Plex's own credits start" under the ending.
- 2026-09-14 · Emby credits (R1, owner; §6.3): Emby always gets the decided credits start, even when the credits end
  before the file does; Emby's Skip Credits button then skips to the end of the file, including any scene after the
  credits, and the file's row and the Inspector say "Emby skips to the end of the file". Overrides the recommended
  "send credits only when they run to the end of the file".
- 2026-09-14 · Season matching (R3, technical ruling; §5.3, §6.2 step 4, §6.4 item 5): the job engine has no completion
  hook, so a job fingerprints the season folder's missing episodes on its workers, matches cached fingerprints inline,
  and queues a Season job for same-season episodes outside the job whose inputs changed. Replaces "a new episode only
  fingerprints itself" and "runs when a season's last episode in the job finishes, in the dispatcher's completion
  callback".
- 2026-09-14 · Check servers (R5, owner: "the user can set a scheduled job just like we do for previews"; §6.2 step 6):
  reconcile is an Intro & Credits job the user starts (Start New Job, `POST /api/markers/reconcile`) or schedules in
  Automation → Schedules; nothing is scheduled by default. Replaces the roadmap's "APScheduler job every 12 h + after
  each Intro & Credits job" (after-job checks stay the read-back and the verify job) and "`keep_plex` stores Plex's set
  as evidence" (`keep_plex` keeps Plex's markers, owner line above).
- 2026-09-14 · Local detectors (§6.2 step 3; phase 2 Task 3, ledger L165/L204): a forced run tracks its refresh per
  (file, source), so a local detector that has to wait for a worker no longer stops later sources being asked again;
  a detector's answer is stored with its version, and an answer from another version is asked again even for decided
  types.
- 2026-09-14 · Season audio needs an ffmpeg with chromaprint (§5.3): jellyfin-ffmpeg in the amd64 image. The arm64
  image has none, so the source isn't run and Settings shows "Not available" with the reason
  (`GET /api/markers/sources/local`).
- 2026-09-15 · Plex version drift in read-back (§6.3), from the parked multi-version limit (a version added to a Plex
  item after publishing, never decided here, left our markers showing for a cut nobody checked): every Plex write
  records the item's version files (optimized copies left out) with the item record, without bumping its version.
  The read-back compares them with the item's live parts and reports `VERSIONS_CHANGED`, which is written again like
  any other drift, so the types the new version hasn't agreed on come off on the next normal run. A write that changes
  nothing still records the files. An item record with markers but no recorded files (a markers.db from before this)
  is written once to record them; that write takes no write lock while the item is as recorded. Jellyfin records no
  files (item ids are per version). An item whose parts are all deleted or in Plex's trash waits as "not in library"
  with "Plex has no live files for this item".
- 2026-09-15 · Emby Bridge plugin (§3.3, §6.3; phase 2 Task 4). The plugin only ever removes marker rows equal to what
  it stored for the item: Emby's own intro detection and other plugins' rows are left, mirroring the owner's Plex rule.
  POST takes `ReplaceOwn` (default false): true replaces rows of a type we write that aren't ours (the app sends it for
  "Use ours"), false keeps them and adds ours only for types with no rows. DELETE with nothing stored changes nothing.
  A corrupt or cut-off store file reads as empty (a file not ending in `}` is refused before parsing: Emby's JSON
  reader otherwise accepted 28 of 204 cut-off lengths of a real file, 10 with a broken credits start), and every read
  failure answers 200/500 JSON. The store records the item's path and file size; a mismatch at item update removes our
  rows (a replaced file keeps its item id). A daily sweep deletes store files of removed items (Emby reports only the
  series when a show folder is deleted). Owner decision R1: Emby always gets a decided credits start, even when the
  credits end before the file does (Emby's player then skips to the end of the file).
- 2026-09-15 · Lab scale run F4: TheIntroDB's daily budget ran out mid-backfill (low priority stops at the reserve)
  and 39 files were checked without it, logged only at DEBUG. Jobs now complete with a warning naming each source that
  ran out, how many files were checked without it and when lookups resume (00:00 UTC, the limiter's day boundary); an
  undecided file's reason notes the skipped source; Settings shows "Daily limit reached — lookups resume at …"; the log
  line is a WARNING once per source per job. No automatic retry: nothing is stored for a skipped lookup, so the next
  run asks again.
- 2026-09-15 · Season view Publish (R4, owner: "Start a job", 2026-09-14; §7.4): "Publish N to M servers" queues a
  normal-priority Intro & Credits job of exactly the season's listed episodes (the season group, not the whole folder),
  named `Intro & Credits: <show> · Season N` (or `· Specials`); an identical pending or running job is reused. Not
  HIGH.
- 2026-09-15 · Season audio rulings (§5.3, §5.5 rules 4 and 6; `evidence/eval/phase2-harness.md` Task 15). R2
  (ruling 2026-09-14 while the owner was unsure): season audio never decides alone at High or Medium — alone it would
  publish 91 useful / 13 wrong / 14 missed on the 118 episodes (10 skipping story). G3 (owner, 2026-09-15: kept on):
  season audio or its previous-season hint plus markers already on a server never decide together, since a server's
  intro detection matches audio too. G3 off would publish 23 right / 7 wrong against Plex's own 23 / 15 on the 118
  episodes (whole-folder mode 23 / 4); it adds no useful answer Plex doesn't have. §5.5 rule 6 is unchanged.
- 2026-09-15 · Harness gate (phase 2 Task 15, `evidence/eval/phase2-harness.md`): on the 118 intro episodes, "Medium
  beats Plex" = as useful or more and no more wrong than Plex's own markers; High = no more wrong than Plex. With G3 on
  (shipped) the gate fails by construction: Medium 0 useful against Plex's 23 (High passes, 0 wrong against 15). With
  G3 off it passes in both modes.
- 2026-09-15 · Season step (§5.3, §5.5 rule 3; phase 2 Tasks 7 and 7b): the season group is the folder's episodes with
  the same parsed season number, at most the 40 nearest; an intro that is more than half silence is dropped and a pair
  that provably holds no run of 120 s or less is skipped; the season chapter-intro check from lab scale finding F1
  (Reservation Dogs S01E05/E06 "Intro" chapters that are story, `evidence/lab/phase1-results.md`) makes a much longer
  intro chapter need an agreeing source that isn't server markers; a season ends with the same decisions whatever order
  its episodes arrive in. Harness unchanged: 91 / 13 / 14 (eval lists), 91 / 10 / 17 (whole folder).
- 2026-09-15 · Phase 2 audit, detection (§5.3, §6.1; `.superpowers/sdd/plan-phase2/audit-detection.md`): a file ffmpeg
  can't fingerprint is skipped by other episodes' season steps for a day while its identity is unchanged (its own run and
  a forced re-detect still try); a fingerprint made with another algorithm or window length is computed again (and its
  cached pairs dropped). Siblings that waited on a file's lock while ffmpeg failed on it skip it too. Every job that
  completes, except Season, retry and verify jobs, starts a background sweep after giving back its slot (one at a time,
  at most one start an hour, a warning when a start is skipped for one running over 10 minutes, at most every 10
  minutes): up to 2,000 fingerprinted files within 60 s,
  oldest-checked first, dropping the fingerprints, pairs and member failures of files gone from a folder that still
  exists; `files` rows stay, no auto_vacuum. The season-audio answer and its signature are stored in one transaction. A
  Season job never queues a member season audio never answered for. In a flat folder, a change also reaches files whose
  own group holds the changed episode for season audio, as for F1. The sweep deletes only when the file's row still has
  the identity it was listed with. A fingerprint failure is recorded while the file's lock is held. Chromaprint has
  three states: available; absent (every ffmpeg listed its muxers without it, kept for the process), where stored
  season audio (and hint) answers can keep a type in review but never help decide it; unknown (a check timed out,
  couldn't start or exited with an error, asked again after 10 minutes), where no season audio runs but stored answers
  count as before.
- 2026-09-15 · Season follow-up jobs (§6.2 step 4; phase 2 Task 8): `Season: <show> · <season>` jobs run at LOW, or
  NORMAL when a webhook follow-up queued them (its retries and verify job queue LOW); at most 500 files; new requests
  join a waiting Season job of the same priority; a Season job never queues another. Webhook follow-ups for episodes of
  one season join a waiting follow-up (up to 500 files), and joined episodes don't wait for their own preview jobs.
  Requests are kept in memory, so a restart only delays them until the season's next run.
- 2026-09-15 · Emby publishes per version (§3.3, §5.5 rule 7, §6.3; phase 2 Task 10 round 3,
  `evidence/lab/phase2-results.md`): Emby's web player plays each version's own chapters, so each version item gets
  its own markers, like Jellyfin; no cross-version agreement or recorded version files. Replaces round 1's "Emby
  versions share one marker set" (the Plex item rule). (The server-marker evidence reader's one-cut check on Emby
  items, parked here as stricter than needed, was dropped on 2026-09-19.) "When Emby has its own markers: Use ours /
  Keep Emby's"
  (`on_emby_redetect`) mirrors Plex's setting through the plugin's `ReplaceOwn`.
- 2026-09-15 · Check servers details (§6.2 step 6; phase 2 Task 11): "Intro & Credits · Check servers", LOW unless
  asked otherwise; one queued or running at a time (`already_queued`). It reads every published item in steps of 500
  items (Plex one item per database connection under the lock proof; Jellyfin and Emby one request per item, a
  server stopped after 20 failed reads in a row, "Couldn't check <name>") and lists at most 500 files per run, drifted
  items taking turns. A pause during the read-back gives the job slot back. Files with decided credits or preview whose
  server's stored answer is empty or unusable are re-read once it is 1 day old, then 2, 4, 8 and 16 days after each
  re-read that stays empty or fails, at most 5 times. An item the server no longer has is dropped when no runnable file
  belongs to it, or when the server confirms it missing after a "not in library" row; that file gets the one normal
  retry, and the item is dropped only once the retry is queued (a cancelled or failed run, or retries off, drops
  nothing). It queues no other retries.
- 2026-09-15 · Phase 2 milestone audit, jobs and Season view (§6.2 step 6, §6.4 item 8, §7.4): a Re-run of a finished
  Check servers job is queued like `POST /api/markers/reconcile` (one at a time, `already_queued`); other Intro &
  Credits Re-runs keep their schedule. Both answers add `"paused": true` when the Check servers job already there is
  paused; a Check servers Re-run clears Pause all like any Re-run. A schedule's start tick (or Run now) resumes the
  Intro & Credits jobs its stop time paused (`paused_by_schedule`; never a pause by hand) whichever mode they are, and
  then queues nothing that tick; otherwise it applies its own mode's "unfinished" check (Find markers ignores Check
  servers jobs). Deleting a schedule leaves its paused jobs paused with a WARNING. Season view: "Publish N" counts episodes with at least one decided marker of any type,
  `needs_review` any type in review; the header's show and season are the Publish job's.
- 2026-09-15 · Emby needs Emby Premiere to skip intros (§3.3, §7; phase 2 close-out, owner checks,
  `evidence/lab/phase2-results.md` "Phase 2 close-out"): replaces §3.3's "Emby web shows Skip Intro without Premiere".
  The key is the server's, not the viewer's. Without it Emby shows Skip Intro for the first 2 episodes, opens "Unlock
  Feature" instead of skipping, then hides the button; credits' "Up Next" works. The Emby Edit tab reads `GET
  /Registrations/dvr` (the answer kept an hour per server URL, a failed or timed-out read 5 minutes; not asked of an
  Emby that is unreachable or rejects the credentials) and, only when `IsRegistered` is false, shows the amber row
  "Viewers can't skip intros: this Emby server has no Emby Premiere key. Skip Credits (Up Next) still works." with an
  ⓘ; a failed read, a 404 or an older Emby shows nothing and never blocks the tab. The guide and the plugin's catalog
  text say so.
- 2026-09-15 · Plex credits `final` flag (§3.1, §6.3; phase 2 close-out, lab row 10): under Use ours, rows and the
  `pv:` key that serve the desired times but carry another `final` flag than this file's duration gives are rewritten
  through the normal write (e.g. credits stored non-final while a longer version existed, after that version is
  deleted); on a first publish, Plex's own rows with the same times get our flag too. Only on an item with one version
  (optimized copies aside) and a known duration: on a multi-version item each version's runtime can give another flag
  for the same times, so the stored flag stays and nothing flips back and forth. Never under Keep Plex's: rows serving
  the wanted times can be Plex's own even when the item record lists those times (a write that changed nothing
  records them too), and Plex's own rows are never touched. The write runs when a job has a reason to write: a normal
  job on an item whose versions changed, Check servers, or a forced run; a normal job doesn't rewrite an item that
  already reads back as ours. Plex Web 4.160.0 ignores `final`; Plex's docs say some apps show post-play at the final
  credits.
- 2026-09-15 · A new season's lone opener gets its previous-season hint one run late (§6.2 step 4; Task 17 row 2)
  when the previous season is fingerprinted in the same backfill: the hint reads cached fingerprints only, and nothing
  asks the opener again in that job. Its next run is due, because the signature includes whether the previous season's
  files have fingerprints. Precision is unaffected. Known limitation, not changed.
- 2026-09-15 · A sibling changed on disk mid-job (§6.2 step 4; Task 17 fix round 1 finding): an episode whose season
  audio answer left out a sibling changed on disk (its record no longer matches the file) is noted by the job. When the
  job ends, a noted episode goes into the job's Season follow-up if its answer is now out of date (the job read that
  sibling again) and its intro is undecided or rests on season audio. The cap, "no chaining" and "a Season job never
  queues another" are unchanged. Known limitation: when the changed sibling isn't one of the job's files, the job's
  Season job reads it, and that Season job can't queue the earlier episode again, so the episode is one run late, as
  before.
- 2026-09-16 · Phase 3 owner answers: Q1 credits text alone at Medium (yes; measured on 80: alone 65 useful / 3 wrong,
  Medium with Plex 56 / 1 vs Plex 47 / 13; cited from `plan-phase3.md` "Measured while planning" until Task 1 copies
  them into `evidence/credits/phase3-measurements.md`) · Q2 credits text and a server's own marker are independent
  (yes) · Q3 credits text ends at the last credit frame when the roll ends more than 30 s before the end of the file
  (4 of 76 runs), else no end; Emby unchanged (R1)
- 2026-09-16 · Phase 3 owner answers: Q4 gate — per set, Medium wrong ≤ 2 % and ≤ Plex, High wrong ≤ 1 % and ≤ Plex
  (caps rounded up: 80 → 2 and 1, 205 → 5 and 3), Medium useful ≥ Plex, rule J ≥ 59 / ≤ 1 early of 80 · Q5 rule J
  ships as measured, frame-checked · Q6 AMD: self-test decides · Q8 ≤ +250 MB image
- 2026-09-16 · Phase 3 owner answer Q7: a throwaway container on `plex` for the lab's GPU rows 12–15 only (synthetic
  movie, no `/data*` or Plex config, no prod Plex access, removed after)
- 2026-09-16 · Importer copies join their database's group (§5.5 rules 7 and 8; Task 9 fix round 1). An importer
  plugin's markers were always grouped with IntroDB + TheIntroDB, but the plugin check also matches SkipDB and AniSkip
  importers, so SkipDB and a SkipDB importer's copy of it agreed and decided (credits at High: SkipDB 5 730 s and the
  copy 5 731 s published over credits text at 5 700 s). Now the database comes from the plugin names the stored row
  names (`copied_from`). Rows now name every importer plugin, and the reader version is 2, so servers are read once
  again (a row stored earlier named only the first plugin and could file a two-database server's copy as SkipDB).
  AniSkip copies count as IntroDB + TheIntroDB until phase 4 measures them (Task 9 fix round 2). Unknown or more than
  one database: as before. Known limit: on a server with importers of two databases the copy's database can't be
  told, so SkipDB plus its copy still decide there (pinned by test_pipeline's `two-databases` row).
- 2026-09-16 · Phase 3 plan rulings, taken while planning (T-R1–T-R9; `plan-phase3.md`), each with its cost if
  wrong: T-R1 the helper module is `markers.credits.textdet_helper`, not §6.4's `markers.textdet` (cost: a rename)
  · T-R2 post-processing keeps pyclipper for the unclip step; `shapely` stays a test-only dependency instead of the
  roadmap's "unclip computed analytically" (cost: 1–10 MB of image if a fuzz test ever finds a differing box) · T-R3
  a GPU helper matches the worker GPU's PCI bus id, falling back to the CPU on no match except a single-device host
  with an unknown worker address (cost: an Intel/AMD worker's text detection may land on the host's other GPU —
  resource use only) · T-R4 the tail is 450 s for an episode (`season_key` present), 900 s for everything else
  (cost: extra decode on an unknown-kind TV file) · T-R5 keyframe rows are read in ffmpeg's own order, never sorted
  (cost: none measured) · T-R6 luma is rounded to 0.1 and pts to 0.001 as the prototype recorded them, and every
  row has the container's own `format.start_time` subtracted before rule J sees it (cost: nonsense credits answers
  on a recording with a non-zero start if that subtraction is skipped) · T-R7 a GPU decode error reruns on CPU, a
  600 s decode timeout is "no answer" for a day, a CPU decode failure is asked again next run (cost: a file that
  always fails CPU decode, not by timing out, takes a worker slot every run) · T-R8 helpers idle-exit after 10
  minutes and one within 5 s of that exit is replaced before the next request instead of racing its timer (cost: a
  1–2 s helper start after an idle gap) · T-R9 the detector ignores `pause_check` like season audio (cost: a paused
  job finishes the file already in progress, ≈10–30 s).
- 2026-09-16 · Contradictions resolved while planning phase 3 (C1–C7; `plan-phase3.md`): C1 §5.5 rule 6's
  lone-decider list vs §5.4's "needs a second source under the default publish rule" — resolved by Q1 (credits text
  may decide alone at Medium) · C2 §5.4's "AMD: self-test decides" vs the evidence README's "default to CPU until
  proven" — resolved by Q6 (self-test decides) · C3 §12's "larger hand-checked set, tuning" vs the roadmap's
  phase-3 bullets, which name no tuning — resolved by Q5 (ships as measured, no tuning attempted) · C4 §6.4 item 7's
  helper module name vs the roadmap's file map — resolved by T-R1 · C5 §5.4's table (measured at a 10 s refine
  span) vs its own text (20 s refine) — the table stays as measured, with a note now beside it (§5.4) · C6 §5.4's
  "at the frame's own 320 px" vs `det_limit_side_len=320` being ignored by rapidocr 1.4.4 under `limit_type="max"`
  — the effect is as written; the reason is now in the spec so the limit isn't "fixed" later (§5.4) · C7 §6.2 step
  3's "a chapter decision keeps asking" the later sources vs the landed `_detector_pending`, which asks a local
  detector only for undecided types — the spec now follows the code (§6.2 step 3).
- 2026-09-18 · Owner ruling on Task 11's failed 205-movie gate check (Q4; the milestone audit): on-screen credit
  text ships at "Medium" now. At "High" it is not yet good enough on the harder, unadjudicated 205-movie set — a
  real, disclosed limitation (rule J's `coarse_start` anchors on the roll's **last** credit run, which answers late
  when a gap splits the roll), not a measurement artifact; fixing it is a tracked follow-up (§13 item 14), not
  attempted here. Available and recommended at "Medium"; available at "High" only when a second source agrees —
  unchanged default behaviour for anyone who hasn't touched "Publish when".
- 2026-09-19 · Owner, phase 3 close-out (roadmap checkpoint 2): a one-off side-by-side of the `pr-241` image on `plex`
  against one real season (Rick and Morty S01), read-only, its own config, no Plex config folder. Reminded of the
  lab-only rule first, the owner approved it for this run only. The database-write confirmation the app needs to
  turn Intro & Credits on for a Plex server was not given, so the shipped detector ran directly in the container and
  nothing was published; everything was removed afterwards. Result on untitled chapters that decide nothing: 10 of 11
  starts within 10 s (one 0.5 s early, none later than 9.3 s); the nine answered episodes with a scene after the
  roll all got an end within 4 s of the roll's end, so the scene is kept; one episode (E04) had no answer (`evidence/lab/phase3-results.md` "Side-by-side").
- 2026-09-19 · Final review (controller ruling, a technical park per the 2026-09-15 line, not an owner ruling): the
  server-marker evidence reader no longer applies the one-cut check to Emby (§5.5 rule 7). Each Emby version is its
  own item and its markers are read from that item, so the check only turned valid evidence into "unusable" and, under
  user-id auth, re-read that Emby server on every run still missing evidence. The reader now checks the resolved
  item is the file's own version (as the publisher already did), since Emby's fallback search can return another
  version's item. Plex's one-cut check is unchanged.
- 2026-09-19 · Final review, cleanup (§6.2 step 6, technical): a Check servers run uses a file's server recheck, and a
  failed item's retry, once that file has its result, not when the run lists it. A run cancelled (or ended by a
  restart) before a file leaves that file's rechecks and retries due for the next run; the backoff and the 5-read and
  5-retry caps count only checks that happened. An item's retry counts once per run however many of its files run,
  and a revived run doesn't count a file again. Files no run can check still take their turn when listed.
- 2026-09-19 · Final review, rule J lane (the owner asked for the credit-text gaps to be finished; tuning ships only
  under the owner's own bar, Q4/Q5): rule J version 2 (`CREDITS_TEXT_VERSION` 2, so stored answers are asked again).
  The anchor never steps over a gap longer than the 24 s join; the end steps back over scene text glued on after the
  roll, with whether there is an end still decided as in version 1; and text on screen through most of the tail
  before the run gives no answer (the lab's Synth Audio timecode; §5.4 steps 6 and 8, §13 items 13–14). Both sets and both
  decode paths, both versions measured on the same tree after the intra-only thinning: no early answer added at any
  level; Medium useful 80 57 → 58 (CPU 54 → 55), 205 90 → 96; rule J alone on the 80 64 / 1 (CPU 59 / 1, now meeting
  §5.4 too); every moved answer frame-checked, all of them the anchor's (`evidence/eval/phase3-harness.md` "Rule J
  version 2"). Known and pinned, no set file has either: a lit scene-text frame the anchor keeps across dark
  keyframes, and subtitles on a dark scene after text-free story. Measured and not shipped: walking
  back to earlier runs, merging runs over text-carrying gaps, a lower box count next to a run, a lit-only anchor, and
  2 boxes at luma 30–33 (the last would answer E04 on the GPU but put one movie's start on its epilogue cards). The
  205's gate still fails 3 of 5; credit text alone stays at "Medium" as ruled on 2026-09-18.
- 2026-09-19 · Final review, rule J lane round 3 (`CREDITS_TEXT_VERSION` stays 2; no stored answer holds round 2's):
  a run under 30 s into the tail with nothing lit before it is read on into the 120 s before the tail, and judged on
  both when it carries on into them (§5.4 step 8). It gives back what version 2's 30 s floor took: the lab's five
  Heeramandi episodes, whose 462 s credits start before the 450 s tail, are answered on the roll's first card on both
  decode paths (version 1's answers; the chapters start one card later). It doesn't bring back the broadcast wrong
  answer the floor removed (Mayday S12E10 on the CPU: story came first, so nothing before the tail is read). Every
  other answer is unchanged: every set row on round 2's stored decodes (0 new windows), the 51 broadcast recordings
  and the lab's synthetic files. The harness now keys its stored answers on the text detection backend actually used
  and the GPU device, and its stored decodes on the ffmpeg build.
- 2026-09-19 · Final review (controller ruling): credit text on broadcast TV ships as measured, a known limit (§13
  item 15, `evidence/eval/broadcast-tv.md`); static-overlay suppression and "credit text + a server's own marker
  can't decide High" were measured and rejected. The VP9 keyframe pass drops non-key packets before the decoder
  (FFmpeg's VP9 decoder ignores `-skip_frame`; §5.4 Frames, `evidence/eval/phase3-harness.md` "VP9's keyframe pass").
- 2026-09-19 · Final review, scale-run finding 1 (§3.1, §6.3): the 24 lab-Plex parts refused as "not JSON" were
  rewritten by Plex itself. PMS 1.43.4's weekly Butler "Optimize database" runs a new database's deferred one-time
  migrations the first time it runs (lab: 2026-09-17 02:01 UTC). One of them, `202302020000`
  (`CreditsFinalAttributeMigration`), flags credits `final` when the stored end is within 10 s of the part's end,
  writes those parts' `extra_data` in Plex's pre-2023-09 URL-encoded form (`k=v&…`, sorted keys, no `url` field: the
  JSON form's `url` field holds exactly that form) with the entry's `final` as `1`, and rebuilds the item's `taggings`
  rows with `pv:final`. It flagged our non-final credits ending 1.1–9.7 s before the file's end (10.1 s was left).
  The owner's production DB (read-only, same PMS) has no URL-form row: the migration ran there before the JSON
  conversion (`schema_migrations` rows 339 and 354), and 791 credits entries still carry `"final":1` inside JSON. The
  publisher now reads both forms, writes a part back in the form it has byte for byte as Plex would (checked on every
  lab row and 517,476 production rows), reads `final` `1` as final, and still refuses anything else. Under "Use ours"
  such an item gets our `final` flag back once; the migration is recorded and doesn't run again on that database.
- 2026-09-19 · Final review, scale-run finding 2 (§5.5 rule 7, §13 item 17): after a fresh config, markers this app
  had written to Plex and Emby read as "markers already on a server" (credits on 500 files from Plex and 36 from
  Emby, intros on 285 and 26). Emby's reader now leaves out the chapter rows the Bridge plugin wrote, as Jellyfin's
  reader does with its segments: the plugin shows exactly what it stores, and its store lives on the server, so it
  outlasts a lost markers.db. A type's rows are ours only when every row the plugin writes for it is there (an intro
  counts as a pair), the set a write still under way is replacing included (`Replacing*Ticks`, from the next plugin
  build; an older one answers none and the gap stays). An Emby with no markers route stores nothing of ours: a 404
  says so, and for anything else it answers (a proxy's 502 or HTML page, a 401 on an unrouted path) the Ping settles
  whether the plugin is there. A store that can't be read on an Emby that has the plugin (an error, or credentials
  that aren't an administrator's, which can't publish there either) gives no evidence, as with Jellyfin, and is asked
  again like any unusable answer. Server reader version 4: every server a file isn't yet
  published to is read once more (one request per file and server). A file already published to a server keeps the
  answer it stored before (a published server isn't read again), so a markers.db that read our own Emby markers
  before this keeps them for those files. Plex keeps no such record (§13 item 17).
- 2026-09-20 · Final review, scale-bugs round 2 (§6.3, §13 items 17–18): an Emby that answers the markers route with
  anything but the plugin's JSON (a proxy's 502 or HTML page, a 401 on an unrouted path) is settled by the Ping, so
  only an Emby that has the Bridge takes its markers out of the evidence; pinned against the lab's Emby 4.9 with the
  plugin's DLL moved aside (`tests/cassettes/test_servers_emby_markers_vcr/TestEmbyWithoutTheBridgeContract*`). The
  Plex library sample gives each `extra_data` form its own 50 newest rows, since the credits `final` migration rewrites
  parts where they are and a URL-encoded part keeps its old id. The Emby plugin answers `Replacing*Ticks` (needs a
  plugin build; §13 item 18).
- 2026-09-20 · Final review closed: the branch is pushed to `003a8d1`, where CI is green including the new arm64
  image job (the lab-evidence commits after it are docs only),
  and the published `pr-241` image (`sha256:813f67cb…`) passed its own checks (detector exit 0, model sha256 = the
  pin, ffmpeg 8.1.2) and re-ran lab rows 1, 2, 3, 16 and phase 2's row 24, with credit text giving no answer on the
  lab's burnt-in-timecode episodes. Still the owner's to do: the Emby catalog submission (roadmap checkpoint 4).
- 2026-09-20 · Credit-text rows carry their text boxes' **positions** (§5.4 "What a row holds"): `(left, top, right,
  bottom)` in the frame's own 320×180 pixels, appended to the row, so rule J reads exactly the three fields it read
  before and no answer of its own can move. The helper protocol answers a frame's boxes instead of a count (a reply
  that isn't four numbers per box is refused), the harness's decode cache and answer cache store them, and
  `tools/markers_eval/credits_synth_fixture.py` rebuilds the lab fixture with them; the 80-file rule J fixture keeps
  the prototype's measured rows and carries none, which its own `about` says. Nothing is decoded or detected twice for
  them: a file's rows weigh 8.0 KB more at the median (373.3 KB at the heaviest decode measured) and the bounds cost
  15 µs a frame against 11–17 ms of text detection. Proven by re-decoding everything, since the decode key changed:
  the GPU run over the 80, the 205 and the 43 online cases (568 windows decoded, 543 rows compared) and the CPU run
  over the 80 (154, 80) are identical to the round-3 final runs file by
  file, gate check by gate check, online decision by online decision; the 51 broadcast files on both paths and the
  lab's synthetic files answer the same too (`evidence/eval/phase3-harness.md` "Rows carry their boxes' positions").
  Wanted by §13 items 14 and 15, which are built on top of it.
- 2026-09-20 · §5.1 "Ending" is credits on a **TV episode** only (`CHAPTER_RULES_VERSION` 1 → 2), measured on the
  owner's whole library before and after — 4,346 anime episodes, 11,919 non-anime TV episodes (a seeded sample of
  110,211) and 9,904 movies: +280 anime credits, no change on the other two. The scope is library kind because no
  in-file scope separates an anime `Opening`/`Ending` pair from the one movie that has the same pair, and that movie's
  "Ending" is its last scene (frame-checked: taking it skips 161 s of the film). The kind is the file's own path
  (`ids_from_path`), not a resolved kind, so cached chapter evidence can never disagree with what derived it; all 282
  anime files with that chapter name a season and episode, so the stricter input costs nothing. Measured and **not**
  taken in the same run: `End` as credits (0 anime files use it; 9 non-anime files do) and not deciding an intro from a lone
  generic `Intro` (would lose 569 anime + 857 TV intros to remove 7 wrong ones — the chapter is the theme song in 85%
  of the files Plex can judge, not the cold open). `evidence/eval/phase4-chapters.md`.
- 2026-09-20 · **Q1 · A marker the user adjusted or locked beats a server's "keep its own" setting** (option a). A
  locked type is published to every owning server even where `on_plex_redetect` is `keep_plex` or `on_emby_redetect`
  is `keep_emby`: Plex's own rows and `pv:` key of that type are rewritten, and Emby gets that type re-posted with
  `ReplaceOwn` (which is per POST, so a locked type goes on its own and the whole set follows without it). The reason:
  the setting answers "who wins when the *server* detects something of its own", and the user isn't the server — a
  lock is the one answer nobody else can produce, and a setting that silently swallowed it would leave the Inspector
  showing times no player ever plays. The override is never silent: the publishers report the types whose own markers
  they replaced, the server row says "Replaced Plex's own marker. This server is set to keep Plex's, but a marker you
  adjust always wins.", and the Inspector says it before the save ("This server is set to keep Plex's own markers.
  Your locked marker replaces them anyway."). This closes spec contradiction D1 — §5.5 rule 1 stands and §6.2 step 6's
  exception is gone, so the rule is stated once. An **unlocked** decision still loses to a kept type exactly as before.
- 2026-09-20 · **Q3 · The Plex marker agent's shape** (plan phase 4 Task 10). A Plex on another machine gets a
  small container of its own (`plex-marker-agent/`, built from this repo, published beside the app image), run next
  to Plex with Plex's config folder mounted. It is not the app image in another mode: it carries no ffmpeg, no GPU
  drivers and none of the media libraries, and it has one job. The app talks to it over HTTP with **one shared key
  the user pastes into both sides** — `Authorization: Bearer`, `secrets.compare_digest`, the app's own token model,
  masked as `****` in every response and never logged. It is the **only** supported way to write markers to a Plex on
  another host, and it never becomes a general remote-control API: the database path is its own `PLEX_CONFIG_DIR`
  (never the app's to send), it runs no caller-supplied SQL, and its eight endpoints are all about one item's
  markers. It refuses what the in-process writer refuses — a database that isn't on a local disk **on its side**, a
  schema it doesn't know, a missing or duplicated marker tag row (it never creates one) — because it runs that same
  code (`LocalPlexDb`), not a second implementation. A refusal comes back as the very same exception the local path
  raises, so a failure reads exactly as it does today. Version skew is caught on the first call in either direction
  (the app sends `X-Marker-Agent-Protocol`, the agent answers its version and the protocols it implements) and
  refuses with both versions named, writing nothing. Cost if wrong: a rebuild of the container and its docs.
- 2026-09-20 · Rule J **version 3** (`CREDITS_TEXT_VERSION` 3, so stored answers are asked again; §5.4 steps 4, 5 and
  7; §13 items 14 and 15): the first rule to read **where** a frame's text is, in two mechanisms built and measured
  apart and then combined and re-measured together.
  - **Text that never moves is not credits** (step 4). Text the detector keeps finding in one place right across the
    story — a channel or score bug, a ticker, a burnt-in timecode — doesn't count as text inside the run, and a run
    that leaves fewer than two credit frames without it is not a roll. The thresholds (IoU 0.5, 80 % of the story's
    span, 5 % of its rows, 4 sightings, 60 % containment) sit in a flat safe band: no set answer moves at any
    keyframe share from 0.02 to 0.15. Re-swept 2026-09-21 with **both** halves live (the original sweep was the
    overlay half alone) through the committed `credits-text --sweep`: the band holds (GPU over the 80 and the 204,
    CPU over the 80), and the one number never swept before, containment, is flat from 0.5 to 0.7 and loses the
    overlay half's own gain at 0.8.
  - **A roll the 24 s join split, put back together from the band its text keeps to** (steps 5 and 7). An earlier
    kept run whose text sits in the last run's band (median box middle within 32 px, a tenth of the frame) and whose
    gap to it never stops carrying text (half the keyframes between) is the same roll; and a keyframe before the
    start in that band, no further than 1.5 × the run's own credit cadence and never more than the 24 s join, is more
    of it. Both only move a start earlier; the end is still measured from the run alone, and no end moved.
  - **The order is load-bearing.** The runs are still picked, and `text_all_through` still counts its share, on the
    rows as they were decoded, so step 4 can never unmask an earlier run, and step 8 never sees a keyframe it
    emptied as blank. It can still turn a refusal into an answer: both of step 8's tests are measured from a start
    step 4 may have moved, so it can carry a run past the 30 s floor and move the share step 8 counts. Everything
    that reads a frame's own text — the anchor, both band steps, the ends and the 1 fps refine — reads the rows step 4
    left. Read as decoded, the walk crosses a whole broadcast story on a channel bug's own boxes; that is worth two
    right answers on the CPU decode of the 51 recordings, and it is pinned by
    `test_rule_j.TestOverlayBoxes.test_the_band_steps_never_walk_the_start_back_over_the_bug`. **The two halves do
    cancel on one shape** — a roll that fills most of its own tail and that the join split has its own text inside
    step 4's story, so the merge is refused and the answer usually stays where version 2 had it. Unreachable on anything
    measured, pinned, and not fixed: the three fixes measured each cost a real broadcast answer. Step 4's own
    non-monotonicity applies to this shape too, so "stays where version 2 had it" is the usual case and not a
    guarantee: thinning a run grows the spacing the anchor measures, so step 4 can stop the anchor stepping over a
    glued-on frame and put a start up to the 24 s join earlier than version 2, on story. That reproduces with the
    band steps stubbed out, so it is the overlay step's own doing, and it is pinned
    (`test_rule_j.TestOverlayBoxes.test_thinning_a_run_can_stop_the_anchor_stepping`) rather than argued.
  - **How early step 4 can put a start is not bounded** (corrected 2026-09-21; it read "up to the 24 s join"). The
    join bounds the *anchor*, which takes one step; step 7's walk has no step limit, and both of the things it reads
    move under step 4. Its **cadence** did: thinning grows the run's spacing and so the walk's own limit, which on a
    synthetic tail put a start 297.5 s before the band steps alone. That is closed — the walk's cadence is capped by
    the run as it was decoded (`rule_j.reach_back`), which moves no answer of either set on either decode path, none
    of the 51 broadcast recordings and none of the lab's 8. What remains open is **which frames are in the band**:
    `in_band` reads the median middle of a frame's *remaining* boxes, so dropping a corner bug can put a story
    keyframe in the roll's band, and a synthetic tail of them answers the file's first row. Both are pinned
    (`test_rule_j.TestReachBack`), nothing measured does either, and the real worst case is a start anywhere in the
    tail.
  - **Sets, both decode paths, against the positions commit:** no early answer added at any level and no verdict
    became `wrong` — the 205 has one *fewer* (Medium wrong 17 → 16, credits text alone 37 → 36). Medium useful 80
    58 → 60 (CPU 55 → 57), 205 96 → 101; credits text alone on the 205 116 → 123 useful with 41 → 35 late; rule J
    alone on the 80 64 → 66 within 10 s (CPU 59 → 61), 1 early on each path; the 80's gate 5 of 5 on both paths, the
    205's still failing the same 3 of 5. The 43 online cases decide identically, no end moved, and the lab fixture
    rebuilds byte-identical. The two mechanisms take nothing from each other here: every answer the band steps move,
    the pair moves to the same second, and step 4's one gain is on top of them.
  - **Every answer that moved more than 10 s is frame-checked** (12 GPU rows over 9 files, 2 on the CPU), and every
    file's verdict was compared run against run at all three levels whatever the size of the move — which is what
    caught the one variant that added an early answer (letting the walk step back inside the run undoes the anchor).
  - **Measured and not shipped:** the walk honouring the dark bridge, the walk inside the run, a scale-free band, no
    band at all, a distance limit on the merge, suppressing the overlays before the runs are found, the overlay step
    without its two-credit-frame floor, the same idea against the whole tail rather than the story, and a
    "boxes in a screen corner don't count" test.
  - **Measured afterwards, never tuned on — the 51 broadcast recordings** (§13 item 15,
    `evidence/eval/broadcast-tv.md`): the GPU decode is strictly better (Medium wrong 6 → 4, nothing lost), the CPU
    decode loses one right answer (Medium useful 11 → 10, wrong unchanged at 5, High unchanged). Step 4 cancels most
    of what the band steps cost there but not all: MasterChef Junior S03E02's name caption is not an overlay, and
    Bondi Rescue S14E12's promo strapline splits into groups spanning under 80 % of the story. Shipped anyway,
    because the owner's bar is the two sets and broadcast is item 15's disclosed population; the cost is recorded
    here and frame-checked there.
  - The 205's gate still fails 3 of 5; credit text alone stays at "Medium" as ruled on 2026-09-18.
    `evidence/eval/phase3-harness.md` "Rule J version 3", `evidence/eval/broadcast-tv.md` "Rule J version 3".
- 2026-09-21 · **AniSkip: measured, not taken** (spec §4, §5.5 rule 8; plan phase 4 Task 1, D3, D4). No source is added
  in phase 4, so `SOURCE_IDS`, the settings block and the schema version don't change. Reasons, from
  `evidence/eval/aniskip-facts.md`: nothing in the library carries a MAL id and the two maps that would supply one have no
  licence; AniSkip numbers episodes per MAL entry, which the library's season and episode numbers don't match for a
  third of the anime on disk, and a wrong pick returns another episode's times; it is not a coverage win; and it is
  probably not independent. The independence test is rule 8's own: AniSkip and IntroDB give the same intro to ≤ 44 ms on
  14 of 68 episodes both answer (21 %), against 7 of 72 (10 %) between TheIntroDB and IntroDB. Rule 8's interim ruling
  is therefore permanent: an AniSkip importer's copies stay in the IntroDB + TheIntroDB group.
- 2026-09-21 · **A save from the Inspector is bounded** (§6.2 step 8; plan ruling P-R1). It saves and locks first, then
  publishes inline to each owner: each call to a server waits at most 8 s (`PUBLISH_NOW_SERVER_TIMEOUT_S`, Plex's database
  locks included), and a server not started within 25 s (`PUBLISH_NOW_DEADLINE_S`) isn't started, its row says so and the
  next run publishes it. A job running the same file makes the request leave the publish to the next run (2 s wait).
  Cost accepted: the 25 s is a start gate, so one server already under way can outlast it by a small multiple of 8 s.
- 2026-09-21 · **A locked edit a server never received is always due on Check servers** (§6.2 step 6; found by phase 4
  lab row 7, Jellyfin stopped while the editor saved). The server shows what this app last left there, so it hasn't
  drifted, and a `skipped` publish isn't a failed item, so no run listed the file until another job happened to; and the
  editor labelled that server "Intro & Credits off" with "Turn on Intro & Credits". Now a skipped row of a server with
  Intro & Credits on is `failed` in the editor ("Your times are saved. This server gets them at the next Check servers
  run."), and Check servers lists every file with a locked marker whose last publish to an owning server was `failed`
  or `skipped`, on every run, with no backoff (at most 100 files a run, so a server that stays not ready can't crowd
  out the other lists): the user's own edit isn't left waiting a day.
- 2026-09-21 · **Saving is locking, and the editor clamps rather than judges** (§5.5 rule 1, rule 2; plan P-R2, P-R3).
  There is no adjusted-but-unlocked marker. A marker the user saves keeps two of rule 2's bounds (inside the file, ends
  after it starts); the 3 s minimum, the intro cap and the position windows exist to catch a wrong source and are not
  applied, so the editor warns and still saves. Adding a marker where nothing was found starts from a round time
  (intro/recap 0:00–0:30, credits the last 60 s, preview the last 30 s) so it can't be read as something detected.
  `respect_locks` does not gate a lock: it stays in the settings block, but a lock wins whatever it is set to. (Since removed: see the entry below.)
- 2026-09-21 · **Setup Health gets an Intro & Credits section** (§7 item 6, §9; plan D6, P-R5, P-R6). The method stays
  `previews_readiness()` and the envelope gains a `markers` section (or plugin rows for Jellyfin and Emby) built from the
  facts the Edit tab already computed, so the two can't disagree. A server with the feature off gets one `recommended`,
  `ok: true` row and is never contacted for the rest. Its copy calls the Plex marker agent "the Plex marker helper", the
  Edit tab calls it "Plex marker agent"; the docs say both.
- 2026-09-21 · **§13 item 17 gains a lock note** (D7). A lock lives only in `markers.db`, so losing it loses the user's
  edit, not just its provenance. The `extra_data` ownership key stays the owner's pending decision; nothing new is built.
- 2026-09-21 · **Rule J step 7's cadence is capped by the run as it was decoded** (`CREDITS_TEXT_VERSION` stays 3;
  no stored answer moves). Step 4's thinning grew the run's credit-frame spacing and so the walk's own step limit,
  and the walk has no limit on how many steps it takes, so a wider limit was a longer walk: on a synthetic tail it
  put a start 297.5 s before the band steps alone. The walk now takes the smaller of that spacing and the same run's spacing on the rows
  as decoded. Measured HEAD against it on **both decode paths** over the 80, the 204 (`Paradise (2024)`'s file was
  replaced on disk) and the 43 online cases, and over the 51 broadcast recordings: **no answer, end or decision
  moves anywhere**, and the Q4 gate reads the same. This is the first CPU run over the 205 — every published CPU run
  covered the 80 — so it decoded 398 windows; the capped arm then reused all 553.
  `evidence/eval/phase3-harness.md` "The walk's cadence, capped by the run as decoded".
- 2026-09-21 · **The GPU text-detection self-test compares the boxes both backends find, not how many.** It gated on
  `count()`, which was a complete test only while rule J read a row's box count; version 3 reads where the boxes
  are, so a backend finding the same *number* of boxes in different *places* passed and then answered differently
  from the CPU path — and this self-test is the only runtime check there is. It now compares `detect()`, which costs
  the same (`count` is `len(detect(...))`). §5.4 Guard; pinned in `test_textdet_helper`.
- 2026-09-22 · **Where to look for credits is a setting; "Never overwrite my edits" is gone** (§5.4 Frames, §7 item 2,
  §8). Advanced (collapsed) in Settings → Intro & Credits holds two selects, TV episodes and Movies, each Automatic or
  5/10/15/20/30 min (`markers.credits_window`). Automatic is byte-for-byte the old 450 s / 900 s. The window is in the
  detection fingerprint only when set, and a chosen window changes the stored answer's version, so nothing is decoded
  again on upgrade and a changed window never serves an answer read from another. `markers.respect_locks` was removed:
  it gated nothing since a lock always wins (§5.5 rule 1; verified: no reader of it was left in the pipeline), so the
  switch only suggested a choice that did not exist. An old `settings.json` that has it still loads.
- 2026-09-23 · **Credit text keeps reading back until the credits' start has story before it** (owner; §5.4 step 8,
  §5.5 rule 2, §13 item 14). The tail's own step is unchanged: it is read when the tail opens on the run, and kept
  only when the run carries on into it (`rule_j.opens_on_the_run`, `rule_j.joined_before`). The run has then crossed
  the tail's edge, so while it still starts under 30 s after the first row read (`rule_j.too_little_story`), the
  120 s before the rows read so far are read and put in front whatever they hold (`rule_j.rows_before`): more of the
  roll moves the start back, and story is what the start needs. That closes both gaps a first cut left — a roll
  whose first card is a step's first keyframe (a 300 s TV window with 420 s of credits) and one starting 0–30 s into
  a step after story (the old 90–120 s before the tail). **Never read what the decision refuses**: the later steps
  stop 30 s before the earliest credits start rule 2 keeps, one function both sides call
  (`decide.earliest_credits_start_ms`, with `decide.credits_limits_ms` mapping the user's windows for the pipeline
  and the detector alike): the last 25 % (a stepped-back start is outside the chosen window, so the window doesn't
  exempt it), and for a movie max(900 s, the movie window) from the end. The 30 s are the story a start on that line
  needs. On Automatic a movie takes no step after the first. A file of unknown kind (no season, not a movie) reads
  the movie's tail but has no 900 s cap, so the last 25 % bounds it: on Automatic it can step back from 68 min on.
  **No later step is under 30 s** (`detector._next_step_start`): a remainder under that is read with the step before
  it, up to 150 s, and one left right after the first step isn't read (a 36 min 8 s episode with 5 s keyframes would
  have read a 2 s step). It saves a decode of its own — an ffmpeg start, a seek, a hardware decoder brought up — for a
  sliver that holds a keyframe or two at most. A review first took such a sliver for a false GPU failure, read as no
  frames; measured in the app image (ffmpeg 8.1.2, a P5000; H.264 in MP4 and Matroska, VP9 in WebM), a keyframe pass
  over a window with no keyframe gives the first keyframe after it and exits 0 on the GPU and the CPU alike
  (`evidence/credits/empty-window-decode.md`). That keyframe is the first row already read and the join drops it, so
  there is no GPU failure to guard against and the code has none. **The first step is unchanged** even where it lies
  wholly before that line: narrowed to the line, or to 30 s before it, it turned 1,028 found answers into none on
  the synthetic sweep below — every one a start the decision refuses, but found answers all the same. All the steps share one decode's 600 s
  (`detector.LOOK_BACK_TIMEOUT_S`): the first keeps its own limit, the later ones get what is left, and a later step
  that would start past it or runs past it is a **timeout** like any decode's (T-R7). A stalled NFS read on the first
  step must leave the file for a day, not store a "nothing found" that is never asked again. **No found answer
  moves**: a found start had its story within the tail or the first step, so no later step is read. Checked against
  the previous build on 32,384 synthetic files (story lit or dark, 2 s or 5 s keyframes, a channel bug or none, a
  scene after or none, episodes and movies on Automatic and on 300, 600 and 1800 s windows;
  `evidence/credits/lookback_sweep.py` reproduces every number here): all 15,237 it answered come out the same,
  decodes included. Of its 17,147 misses 2,804 now have an answer; of the 14,343 left,
  7,518 began before the earliest start the decision keeps, 5,841 start 30 s or more inside the tail (rule J's own
  misses on the sweep's channel-bug shapes, not the look-back's), 496 have their first card 0–30 s into the tail
  (unchanged, see above), and 488 are a dark story under a channel bug in front of the roll: the bug's frames are
  credit frames there, so the run reaches back through the story — the tail's overlays are the only ones read, and
  that tail holds no story to find the bug in. So `CREDITS_TEXT_VERSION` stays 3. A "nothing found" stored before
  this is asked again once, only while credits are undecided and a step after the first can be read (an episode on
  Automatic from 38 min, a file of unknown kind from 68 min, never a movie on Automatic; `detector.credits_text_due`);
  every answer now carries the basis
  `detector.LOOK_BACK_BASIS` (`detector_runs`) that tells the two apart. Still costs: a roll that starts 0–30 s into
  the tail after a scene, or on the tail's own first keyframe with story before the tail (the join at the tail's
  edge is refused, as a caption run on a night scene there needs).
- 2026-09-23 · **Under "Keep Plex's" / "Keep Emby's", a file isn't read for a type the server already shows its own
  of** (§6.2 step 3). Found on the owner's server: a Plex-only, Keep Plex's library queued 10,011 movies and 77 of 83
  checked had Plex's own credits, yet credit text decoded every tail (~10 s GPU, 20–130 s CPU), because server markers
  only agree (rule 7) and an undecided type always asks its detector, while the publisher then left Plex's rows alone
  (§6.3) — every answer was thrown away. Now, per type, when every server the markers go to keeps its own and shows
  its own now, no local detector runs for it; one server that would show ours (Use ours, no marker of its own, a
  Jellyfin, another cut, an importer plugin's copy, our own rows on the item, an unknown result after a failed write)
  reads the file as before, and a lock always does (rule 1). "Shows its own now" is a read of each server on that run:
  a stored answer with markers is never read again, and the server's markers come after the detectors in the default
  order, so a stored answer would miss a server that lost its marker and a first run would have none. That read is
  shared with the evidence read (a first run still asks each server once) and costs one read per later run of such a
  file, as a decided and kept type's read-back did. The stored evidence and its read schedule are unchanged, so no
  other type's decision moves. A type that ends undecided while that holds is stored `disabled`, reason "kept Plex's
  own marker" (the chip and the Season view show it), the job row "Keeping Plex's credits" with Up to date, not Needs
  review — also when no detector was skipped on that run, so the owner's ~31 movies an earlier run left in review
  with Plex's own credits leave the review list on their next run (an undecided type, nothing found included, now
  asks the servers on every run under a keep setting, but only a type every destination's publisher can show:
  `publishers.factory.supported_types_for`, so a recap under Plex asks none). A decided type stays decided, with the
  publisher's kept note. Plex's marker gone, or the setting back to Use ours, returns such a file to Needs review, or
  reads it when its answer is due. What a server shows is unchanged: Plex's publisher already leaves Plex's rows of a
  type it isn't sent (`test_a_type_left_undecided_for_plexs_own_leaves_the_item_as_deciding_it_would`), and the
  pipeline sends Plex's database byte for byte what Needs review sent
  (`test_an_answer_left_in_review_writes_the_item_byte_for_byte_as_before`); only the record differs (below). On Emby
  the plugin no longer holds a hidden copy of ours for that type, so a refresh that deletes Emby's own rows leaves the
  type empty until Check servers or the next run reads the file, where the plugin used to put ours back at once.
  Architecture review (no HIGH; three MEDs fixed):
  - **Check servers still heals it (MED 1).** A decided and kept type was recorded (`item_kept_types`), read back,
    and run again once the server lost its marker or was set to Use ours; a type left undecided was recorded nowhere.
    Now such a file goes through the write even with nothing to send (both publishers return before touching the
    server: no markers, nothing of ours before), which records it on its item (`publish_state`, an
    `item_publish_state` row with nothing of ours). `published_items` adds the item's `own_types` from its files'
    stored kept status (`decisions`, `outcomes.is_kept_own`), and Check servers reads them back as it reads kept types
    (gone → `Shown.MISSING`; Use ours → released). Existing storage, no schema change; `item_kept_types` and so
    `published_to_item`'s evidence gating are unchanged.
  - **A Plex version that decided a type isn't left waiting (MED 2; superseded below).** Plex writes a type only when
    every version decided it alike, so a version left to Plex's marker kept another version that decided the type in
    "Waiting for this item's other versions" forever.
  - **A kept episode doesn't re-queue its season (MED 3).** Season audio never runs for it, so its stored answer's
    signature never catches up; the three sibling predicates (`season_audio_followups`, `_request_redecide`,
    `season_audio_answer_outdated`) treat its intro as settled, and the season chapter step doesn't ask again for a
    kept sibling whose re-decided intro is still undecided.
  - LOWs: `publish_now` and the Inspector plan name the stored kept-own types as a job's rows do ("Keeping Plex's
    credits", plan "Keeps Plex's").
  Focused review (two MEDs, two LOWs, reproduced on the real-database harness):
  - **An item with nothing of ours isn't listed for its versions forever (MED A).** Its recorded version files can be
    from an earlier write (a write with nothing to send doesn't read the item), so a version added since read as
    "versions changed" on every Check servers run. Such a row has nothing to agree on across versions, so its read-back
    doesn't compare them.
  - **A Plex item with several versions is read exactly as before this feature (MED B; replaces MED 2's fix).** The
    per-version fixes (read a version when another decided the type; ask the job to re-run a version left to Plex) each
    left an order they didn't cover — the last, one scan listing both versions, where the Season follow-up drops the
    re-run — and three multi-version bugs in a row said the cell doesn't fit the skip: Plex's one marker set per item
    ties every version's decision together. So a Plex file is never skipped, and never given the kept status, when its
    item has more than one part (the markers read's own parts request, shared; unknown counts as several), and that
    machinery is gone. A kept status an earlier build stored on such an item is read again on the file's next run.
  - LOWs: a kept status from a file gone from disk or replaced says nothing about its item; `publish_now` names kept
    types only on a server that still keeps its own and got the last run's rows, and a write with nothing to send
    leaves a failed item row failed, for its retry.
- 2026-09-23 · **Setup Health checks Plex's own detection per library, and not under "Keep Plex's"** (owner-approved
  mockup; §3.1 "When Plex's own detection runs", §7 item 6). The `markers_plex_detection` row read only the
  server-wide prefs and always recommended Off, so it warned about libraries whose own switch was already off, and
  warned a server set to "Keep Plex's", which wants Plex's markers. Now, per Plex server with Intro & Credits on and
  "Use ours": the row lists each library in the server's Intro & Credits selection (`marker_libraries`) where Plex
  effectively detects (server pref not `never` AND library pref on), with the types (intro only in TV libraries), and
  a per-library **Turn off** (`POST /api/servers/<id>/plex-marker-detection`) that writes only that library's listed
  `enable…MarkerGeneration` prefs, after the usual confirm; the server-wide prefs are never written. Nothing listed →
  All good; "Keep Plex's" → an All good "Keeping Plex's own markers: its detection can stay on". A library switch that
  can't be read (older Plex, a failed request) falls back to the server-wide wording with `current: unknown` and no
  buttons. The library prefs are the one fact the row reads itself (`readiness.marker_facts`), only when the
  server-wide detection is on and "Use ours" is chosen, and fresh, so the row refreshes after Turn off. The row keeps
  its id, so an earlier Dismiss still applies. Lab-proven against the throwaway lab Plex: Turn off flips exactly the
  listed library prefs (confirmed by reading `/prefs` back), restore lands them at their prior values, and an
  unrelated pref (`enableAdMarkerGeneration`) is untouched throughout (`evidence/lab/plex_detection_turnoff_proof.py`,
  `.md`). The per-library reads also no longer retry a hung Plex (`retry_plex_call(..., max_retries=0)`): a Setup
  Health probe used to wait ~4x per library on a stalled connection before this.
- 2026-09-23 · **An Intro & Credits job waiting for a server retries in its own row, like a preview job** (owner; §7
  item 5, §6.2). On the owner's server a Sonarr follow-up went green "Completed" while a file wasn't in Plex's library
  yet (a waiting file counted as done), then queued an unlinked top-level "Retry: Intro & Credits · …" job on its own
  timer, beside the preview job's one-row retry chain, so the queue filled with loose retry rows. Now the job reuses
  the preview retries' chain as it is (owner: "don't complicate it; the preview jobs already do this"): a job whose
  run leaves files waiting (not in a server's library, not on disk yet, Plex Pass unanswered) becomes the chain head
  (`upsert_retry_chain_job`: pending, `retry_eta`, "Retry N/M", "Retry starting in …"), and its retry is a hidden
  `is_retry` job with `parent_job_id` (`is_user_visible_job`), on the same schedule and count. The retry records its
  files on the head's Files panel, shows its run on the head, and queues the next retry of the same chain; the chain
  ends as a preview chain does — completed once nothing waits, failed ("exhausted", with how many files still wait)
  when the retries run out. Files that settled keep their results. A follow-up still starts after its preview job's
  first try (the "follows" tooltip now says so), and the two chains don't wait for each other. The head's "Retry now"
  works (the retry reads `force_fire_now`), and a live head isn't run again when the queue resumes; a Check servers
  run or a schedule's job whose only work left is its retry chain doesn't hold back the next run. An old top-level
  "Retry:" job still runs; if its files still wait, it heads a chain of its own.
  Review fixes (two MEDs, LOWs): Inspector Re-detect and Season Publish don't reuse a job that is only counting down
  to its retry (its retry is not forced and lists only the waiting files); after each retry the head's outcome and
  "Decided by" are counted again from its Files-panel rows (`stored_groups`, as a revived job counts); a revived retry
  reads the head's rows to skip files it settled and ends the chain when none is left; deleting a head cancels its
  retry still counting down; a live head refuses a pause, and the chain's end clears any pause on it.
- 2026-09-24 · **A Plex version deleted from disk no longer holds its item back; one not checked yet is retried** (§6.3).
  On the owner's server 12 items sat on "Waiting for this item's other versions to agree" with no retry queued, e.g.
  Big Bang Theory S12E05: intro and credits were decided at 18:01 UTC on the WEBDL copy, but Plex still listed the
  Bluray copy Sonarr had deleted at 17:25 (Plex drops a part only when it scans). That copy was never checked, so
  `markers_for_path` had no decision for it, and `agreed_across_versions` kept every type off the item. Now the Plex
  publisher leaves out of the agreement a version with no decision whose file is on none of the disks the server's path
  mappings give for it (`fs.gone_from_disk`, the fingerprint sweep's check moved there: at least one of those folders
  must still be there, so an unmounted disk never makes a version look gone, and a stale file handle doesn't either).
  A version on disk and never checked is still waited for, but the row now carries `reason_code`
  `versions_unchecked`, a retry reason (`RETRY_REASON_CODES`): the job retries the file on the usual retry schedule
  ("with another version not checked yet"), so a copy deleted after the decision stops blocking at the next retry.
  Versions that were checked and disagree carry no code, as before: retrying changes nothing until one of them does.
  Being a retry reason, it ranks the file "waiting" in `file_outcome` even with a type in review (so does a version
  replaced on disk and not checked again yet); versions that disagree leave a type in review ranked first.
  Every other case writes the item as before (the real-database tests in `test_publisher_contract.py` and
  `test_plex_db_publisher.py` are unchanged). The 12 items already waiting are published by their file's next run.
- 2026-09-24 · **Season re-checks: one Season job per season at a time, and none for nothing** (§6.2 step 4). On the
  owner's server, in 12 h, 63 Season jobs (a third of all Intro & Credits rows) re-decided 242 files 836 times and
  published nothing: CSI S01 was re-checked 14 times and Daily Show S31 11 times, two pairs of Season jobs were created
  in the same second for the same files, and every retry of a file waiting for a server queued the same 12 episodes
  again. Three causes, three fixes:
  - **A request only joined a Season job that hadn't started, and Season jobs start about 10 ms after they are
    queued**, so every job that finished while one ran queued another. Now a running Season job at the request's
    priority that lists an episode of the file's folder takes it (`LATE_REQUESTS`, each file with the
    `pipeline.sequence_number` of its latest request). When it finishes it stops taking requests (`LATE_SEALED`, under
    `FOLLOW_UP_LOCK`) and queues one follow-up, at its own priority, for the files whose run in it started before
    their request, or that it didn't run (`PipelineContext.ran_since`: a worker stage reads everything again, so it
    counts); a file it ran after the request already read that job's results. A Season job that is cancelled drops
    what it took, as it drops its own files.
  - **Past 40 episodes a season's files each have their own group** (the 40 nearest), and the season step compared
    every matched sibling's answer with the new episode's own signature, which never equals it: a new Daily Show
    episode asked again for all 39 siblings it matched, of which only the 19 whose group holds it could change
    (`test_a_new_episode_of_a_long_season_asks_again_only_for_siblings_it_changed`). `_request_redecide` now compares
    a sibling whose group differs with the signature its own group has with the files this run matched, as
    `season_audio_followups` and `season_audio_answer_outdated` already did. A season of 40 episodes or fewer is
    compared exactly as before.
  - **A retry re-ran its file and its season steps asked again** for whatever was out of date, which its first run had
    already queued. A retry now queues a Season job only when one of its runs changed something stored
    (`PipelineContext.answers_changed`: a new or replaced file or re-read chapters, a local detector's answer, changed
    decisions); otherwise it logs that it queued none. A first run is not held to this.
  The fuzz of the queue (`test_queued_season_jobs_end_with_the_all_at_once_decisions`) still ends every season with the
  all-at-once decisions, now with running Season jobs taking requests.
- 2026-09-24 · **The strict mode was removed: every file is decided at the Medium rules** (owner; §5.5 rule 6, §7
  item 2, §8). On the owner's server the online databases cover only 1–3 % of the library, so at the default "High"
  1,006 of 1,250 files sat in Needs review and 58 were published; Plex's own detection has no such option. A first
  draft moved High behind an Advanced switch ("Only publish when two sources agree"); the owner dropped the switch
  too. "Publish when" is gone from Settings and the API (a posted or stored `publish_when` is ignored; schema 16
  deletes it), and the pipeline always passes `decide.APP_PUBLISH_WHEN` ("medium"). The rules engine is unchanged:
  `DecisionContext` still takes "high" for the evaluation harness, and on-screen credit text alone stays within the
  phase-3 gate at Medium (80 files: 65 useful / 3 wrong alone). Settings shows a "How it decides" note under the
  sources, each sentence checked against `decide.py`; the proposed copy said sources are tried "cheapest first" (the
  order is the user's, and the default puts the cheap server markers last), that an online answer is confirmed only
  by a check of the file (another database or a server's own marker counts too), and that "the file check alone
  decides" (season audio never does; SkipDB matched to the file's length may, for intros and recaps), so the note
  says what the code does instead. The files High left in Needs review are decided again once, on the first start of
  this build, by one job that reads nothing again (§8); the same job takes the files whose last row waits for their
  item's other versions (the 12 above). Its job log names only the files whose decisions changed and ends with one
  "Decided again from saved answers (N files): …" line, like a Season job's. Needs review now says why: a lone answer
  that can't decide alone reads "only IntroDB has the intro; an online answer needs a check against the file" (or "only season audio
  found the intro; matching audio needs another source to agree", "only a server's own marker has …"), a
  disagreement keeps "sources disagree: …", and a server row's message is those reasons (it said "Sources don't
  agree yet" for both). No reason code changed; the Files panel still hides the old row text as routine.
- 2026-09-24 · **Season audio may publish an intro alone** (owner, overriding R2 of 2026-09-14/15; §5.3, §5.5 rule 6):
  "if it doesn't exist online then use the GPU/CPU check". On the 118 intro episodes season audio alone is 91 useful /
  13 wrong / 14 missed, against Plex's own detection at 23 right / 15 wrong. `decide._may_decide_alone` now lets a
  `season_audio` intro decide at Medium when it is the only independent group (not credits, recaps or previews: only
  intros were measured, and audio credits were rejected at 54 % precision). The previous-season hint
  (`season_audio_previous`) stays agreement-only: the spec's data doesn't support it alone (48 useful / 10 wrong / 24
  missed, precision 83 % against 88 % for this season's audio) and the owner ruled on 2026-09-13 that it needs a second
  source. Unchanged: G3 (season audio or the hint with only a server's own marker is not an agreeing pair and stays in
  review with the G3 reason, even when they agree), a disagreeing source sends the intro to review, and the hint
  disagreeing with this season's audio is one source contradicting itself. A season-audio decision rests on its
  answer, so it is decided again when the season grows (`intro_rests_on_season_audio`). The harness gate row
  (`tests/markers_eval/test_decisions.py`) now passes with G3 on: an episode Plex didn't answer is decided by audio.
