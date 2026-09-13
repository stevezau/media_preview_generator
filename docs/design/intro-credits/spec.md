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

**Status (2026-09-13).** Owner approved the build. All server write paths proven in the lab. Detection settings
measured on the owner's library. Decisions §14 closed. Build runs on PR #241, branch `feat/markers-detection`
(dev merged in); spec + slimmed evidence + plans live on the branch in `docs/design/intro-credits/`. Local-only,
gitignored files stay beside them: `evidence/lab/env` (tokens), `evidence/lab/synth/` (webm), `evidence/online/skipdb-dump.json`,
`evidence/plugins/emby-4.10/embylibs/`. Next step: the first unchecked task in the current phase plan.

**Working rules (owner's, non-negotiable).**
- Prove server behaviour on the **lab servers on storage** (§10.3), never on the prod Plex on `plex`. Prod Plex DB:
  read-only queries only (`sqlite3 "file:<db>?mode=ro"`).
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
| Where markers live | `taggings` rows (`text` = `intro`/`credits`/`commercial`, `time_offset`/`end_time_offset` ms, `extra_data` e.g. intro `{"pv:version":"5"}`, final credits `{"pv:final":"1","pv:version":"4"}`) pointing at the single `tags` row with `tag_type=12` and **`tag=''`**; plus a per-part copy in `media_parts.extra_data` (`pv:intros`, `pv:credits`: MediaPartMarkersArray JSON, sorted keys, `url` URL-encoded). `metadata_item_setting_markers` is per-user bookmarks. | prod DB (read-only) + (lab) |
| Direct DB write served? | **Yes, immediately, no restart**, XML identical to native markers. Stock Python `sqlite3` works (only `tags` has ICU triggers; we never touch `tags`). | (lab) `evidence/lab/py_write.py` |
| `taggings` alone enough? | Served, but **wiped** by the next forced Plex detection of any type (Plex rebuilds from `media_parts.extra_data`). Writing **both** survives. `extra_data` alone is not served. | (lab) |
| What wipes our markers | Forced detection of the **same** type (`PUT …/credits?force=1`, season `…/intro?force=1`). **Not** wiped by metadata refresh (force), section scan, analyze with detection off, or non-forced detection. PMS 1.43.1+ forces credits detection on manual Analyze. | (lab) + release notes |
| Tag row | Plex looks up `tag_type=12 AND tag=''`. A row created with `tag=NULL` is ignored (Plex makes its own); a new row is only served after a PMS restart. **Reuse Plex's row; never create one.** | (lab) |
| Serving shifts | Credits start served **+2 s** vs DB; non-final credits end **−2 s**. Intro unchanged. Prod native markers show the same. | (lab) + prod API |
| Plex Pass | **An unclaimed / no-Pass server serves no markers at all**, even ones in its DB, and hides marker settings. Viewers also need Pass or Plex Home. | (lab) + support.plex.tv |
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

### 3.3 Emby (4.10.0.40)

| Question | Answer | How |
|---|---|---|
| Core write API? | **No.** `POST /Items/{id}` with Chapters → 204 but ignored; no marker write route in OpenAPI, server DLL strings or web client. Staff (Jan 2026): not planned. | (lab) + research |
| Storage | `Chapters3` rows; `MarkerType` = Chapter, IntroStart, IntroEnd, **CreditsStart** (no credits end, no recap). | OpenAPI + (lab) |
| Plugin path | `IItemRepository.SaveChapters(internalId, list)` keeping existing `Chapter` rows. | (lab) `evidence/plugins/emby-4.10` |
| What wipes | `MetadataRefreshMode=FullRefresh` ("Search for missing metadata" / "Replace all") wipes all markers. Default/ValidationOnly/image refresh, recursive series refresh, library scan, restart do not. | (lab) |
| Self-heal | Plugin stores markers and re-applies on `ILibraryManager.ItemUpdated` (registered from an `IServerEntryPoint`) when they vanished — re-applied within the same refresh. | (lab) |
| Client | Emby web shows **Skip Intro** **without Premiere**. | (lab, Playwright) |
| Install | Catalog plugins install via `POST /Packages/Installed/{name}` + restart (proven with TimeMarkEdit). Separate builds for 4.9 and 4.10 (ABI change). Catalog entry needs a forum thread + developer id from Emby staff. | (lab) + dev.emby.media |

## 4. Online sources

**Accuracy** on 43 episodes with verified truth (studio chapters, frame check, or three-way agreement)
(`evidence/online/`):

| Source | Intros right / wrong / missing | Credits right / wrong / missing | Lookup |
|---|---|---|---|
| **TheIntroDB** v3 | **35 / 8 / 0** | 23 / 4 / 16 | `GET https://api.theintrodb.org/v3/media?tmdb_id&season&episode&duration_ms`; `credits.end_ms=null` = end of file; `null` start = 0; arrays may hold several segments |
| IntroDB.app | 26 / 13 / 4 | 17 / 4 / 22 | `GET https://api.introdb.app/segments?imdb_id&season&episode` (no duration); TV only |
| SkipDB | 16 / 17 / 10 | 8 / 25 / 10 | Measured on the daily ODbL dump matched by id + duration ±5% (R&M "outros" are the last 7 s). **Build uses the read API** `GET https://api.skipdb.tv/api/segments?imdb_id&season&episode&duration&adjust=conservative` (120 req/min), accepting only `match` exact/shifted (§14) |
| AniSkip | n/a | n/a | Anime only (MAL id + `episodeLength`); phase 4 |

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
Parse container chapters (and Matroska `ChapterSkipType` when present). Name → type by case-insensitive regex; a
chapter runs to the next chapter start. Must include names the published plugins miss: **"Title Sequence"**,
"Opening Credits", "Intro", "Recap", "Previously", "End Credits", "Credits", "Outro", "Preview". 165 of 1,500 sampled
seasons (11%) carry them. Exact when present. Chapter truth can still be off vs the rule in §5.4 (3 of 40 movies had
a late or wrong "End Credits" chapter by frame check).

### 5.2 Online lookups
External ids come from each server's metadata (Plex `guids`, Emby/Jellyfin `ProviderIds`) — new `MediaServer`
helper. TheIntroDB is queried with `duration_ms` of the actual file. Every online answer passes the sanity checks in
§5.5 before it counts.

### 5.3 TV intros — season audio matching
**Fingerprint** (per file, cached): `ffmpeg -ss 0 -t <W> -i <file> -vn -ac 2 -f chromaprint -algorithm 1
-fp_format raw -` → uint32 LE, **0.1238 s/point** (measured). `W = min(900 s, 35% of duration)`. The app image's
`/usr/lib/jellyfin-ffmpeg/ffmpeg` has chromaprint; `/usr/local/bin/ffmpeg` does not. CPU only (no GPU chromaprint),
~2 s per episode; at most 2 at a time.

**Matcher (v3)** for every episode pair: inverted index (±2 value shift); per shift, runs where
`popcount(a^b) ≤ 6`, gaps ≤ 3.5 s, length 8–120 s; keep all non-overlapping runs. Per episode: cluster candidates
(start, end) within ±4 s; rank by (length ≥ 15 s, number of supporting episodes, length); require support from
≥ 50% of the other episodes in the group (≥ 1 when only one other). Group = the season folder on disk
(server-agnostic); mixed releases in one season work (R&M NTb + Absinth).

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

Remaining failures: variable couch gag (The Simpsons), a repeated segment ahead of the real intro (Carême), title card
10–20 s longer than the chapter (Daredevil, Outlander). Credits via audio matching: 54% precision — **rejected**.

A new episode only fingerprints itself and compares with cached siblings; siblings still without an intro are
re-decided when it arrives.

### 5.4 Credits — on-screen text (movies and TV)
**Rule (owner):** Skip Credits lands on the **real credit roll** — the first credit card/crawl, including names over
footage — **not** on epilogue text cards ("Two months later…").

**Frames.** The job samples **keyframes of the tail itself** (no dependency on preview frames):
`ffmpeg -threads 2 [-hwaccel cuda -hwaccel_output_format cuda] -skip_frame nokey -ss <tail start> -copyts -i <file>
-an -sn -dn -fps_mode passthrough -vf "scale…320:180…,showinfo" -f rawvideo -` (pts from `showinfo`). Tail = last
**900 s** for movies (covers 205/205 movies' measured credits length: median 233 s, p95 529 s, max 852 s), last
**450 s** for TV. Then a short full decode at 1 fps over the 20 s before the coarse answer to refine it.
Measured cost on storage (P5000 NVDEC): 8–13 s per movie incl. text detection; CPU keyframe decode of a 15-min tail
26.5 s. Per-frame exact seeks (150–270 s) and full-rate decode of the tail are never used. Decode uses the app's existing per-GPU ffmpeg hwaccel selection (NVIDIA / Intel / AMD, same as previews) with CPU fallback.

**Text detector.** RapidOCR **detection model only** (no recognition) at the frame's own 320 px
(`det_limit_side_len=320, det_limit_type="max"`) — same hits as default upscaling (19/19, 0 false), far cheaper.
**Runs on any GPU vendor, CPU fallback** (owner 2026-09-13: "make sure all GPU types work";
`evidence/credits/gpu/RESULTS.md`). Same ONNX model on every path, identical boxes:
- **GPU:** ONNX Runtime **WebGPU plugin EP** (`onnxruntime-ep-webgpu`, +16 MB; Dawn → Vulkan → NVIDIA, Intel ANV, AMD
  RADV — ICDs and loader already in the image). Inside the app image on NVIDIA: 13.3 ms/frame vs CPU 18.7, 100% same
  boxes — **only** after applying the app's Vulkan probe env (`gpu/vulkan_probe.py` `get_vulkan_env_overrides()`,
  e.g. `__EGL_VENDOR_LIBRARY_FILENAMES`) before the session is created. Without it the NVIDIA ICD fails and Dawn
  silently uses llvmpipe at 430 ms/frame.
- **Guard:** use the GPU only when `get_vulkan_device_info()` reports a hardware device and a 20-frame self-test on
  that device beats CPU; pick the device with the EP's `powerPreference`/`deviceId` to match the GPU the user enabled
  for previews. Otherwise CPU.
- **CPU:** ONNX Runtime CPU, `intra_op_num_threads=2`, 18–23 ms/frame.
- Rejected: CUDA-only `onnxruntime-gpu` (+2.8 GB, NVIDIA only); ncnn Vulkan (fast, but the pnnx-converted model
  output was wrong); OpenVINO (Intel only, +180 MB); ROCm/MIGraphX (GB-scale, removed from ORT); OpenCV DNN (no
  Vulkan in pip wheels).
- Honest gain: decode dominates, so a movie takes ≈ 16.7 s with GPU text detection vs 18.8 s on CPU.
- **Proven:** storage P5000 13.3 vs 18.7 ms; plex TITAN RTX 4.8 vs 7.7 ms; plex Intel UHD 770 16.1 vs 8.0 ms (iGPU
  slower than that CPU → self-test picks CPU). All 100% identical boxes. **AMD not tested** (no hardware, owner
  confirmed): same Vulkan/RADV path, self-test decides.
- CPU runtime size ≈ +300 MB (onnxruntime 62 MB, rapidocr 16 MB, opencv-headless ≈ 150 MB, numpy 59 MB,
  shapely/pyclipper 15 MB). Note `rapidocr_onnxruntime` 1.4.4 is "gradually no longer maintained" and caps Python
  < 3.13: vendor its det pre/post-processing (small) or move to RapidOCR 3.x at build time.

**Rule "J"** per frame `[pts, boxes, luma_mean]`:
1. Credit frame = (`luma < 30` and `boxes ≥ 1`) or (`luma ≥ 30` and `boxes ≥ 3`). Bright frames need more text:
   signage in a lit scene gave 3+ boxes for minutes (Checkin' It Twice, −864 s under a looser rule).
2. Join credit frames into runs across gaps ≤ 24 s; dark empty frames never break a run (Summit of the Gods: 24 s of
   dark keyframes split the roll).
3. Keep runs ≥ 15 s; pick the **last** one (credits sit at the end).
4. **Anchor:** start only where two credit samples are adjacent (a lone scene-text frame 24 s before the roll glued
   itself on — Undisputed).
5. **Refine** with the 1 fps decode: walk back from the coarse start through contiguous credit frames (gaps ≤ 2.5 s),
   then back over the fade (luma < 12, steps ≤ 4 s).

**Measured** on 80 files with chapter truth (40 movies, 40 TV; 3 movie truths corrected by frame checks,
`evidence/credits/adjudicated.json`, sheets in `evidence/credits/framechecks/`):

| Frames | Within 10 s | Within 30 s | Early > 30 s | Late > 30 s | None |
|---|---|---|---|---|---|
| **Keyframes of the tail (chosen)** | **59 / 80** | 66 | **1** (Undisputed −34 s) | 9 | 4 |
| Preview frames every 6 s (owner's interval) | 58 / 80 | 61 | 0 | 9 | 10 |
| Preview frames every 10 s (app default) | 43 / 80 | 49 | 1 | 9 | 21 |

Late cases are credits styles the rule doesn't see as "credit frames": names over bright footage or a curtain call
(Taxi Driver, Come from Away, Revenge of the Nerds), textured or light backgrounds (The Mummy, LOTR: Return of the
King), tiny text on black at 320 px (WILL). Late skips are harmless (viewer sees more). Misses: credits shorter
than 15 s at end of file (Animal), credits over a scene (A Season to Remember), dark-grey textured background (Land of
the Dead), several sitcoms whose credits run squeezed over a scene. Why not preview frames: no better at 6 s, much
worse at the default 10 s, and it would couple the two job types.

Credits text alone is one source: under the default publish rule it needs a second source (§5.5).
Full rule tuning happens in phase 3 on a larger hand-checked set.

### 5.5 Combining evidence
Each source yields candidates `{type, start_ms, end_ms, source, confidence}`.
1. A **locked** user marker wins, always.
2. Chapters → accept.
3. Otherwise accept when two independent sources agree (intro end within 5 s; credits start within 10 s).
4. A single source is accepted only at the **"Medium"** publish setting, and only after sanity checks: inside the
   file; intro starts in the first 35%; credits start in the last 25% (movies ≤ 900 s from end); not past end.
5. Markers already on a server count as agreement evidence, never as a sole source.
6. No agreement → no marker; shown as **"Needs review"**.
7. Online sources are independent of each other only if they don't copy each other: IntroDB data looks partly seeded
   from others — IntroDB + TheIntroDB agreeing counts as one source unless a local source also agrees.

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
   gather evidence in §1 order, stop early when §5.5 is satisfied, decide, store.
4. **Season step.** Intros need siblings: fingerprint missing episodes in the season folder, re-decide episodes without
   an intro; if the season has only one episode, use the previous season's cached fingerprints (§5.3).
5. **Publish.** For each enabled owner, its `MarkerPublisher` writes the decided set; unchanged `markers_hash` → skip.
6. **Reconcile** (periodic + after jobs): read markers back from each server; if they differ (Plex forced
   detection, Emby FullRefresh without heal), re-publish. Locked markers always re-assert. Plex:
   `on_plex_redetect` = `restore` (default) or `keep_plex`.
7. **Outcomes** per server: markers written / reused / needs review / skipped + reason.
8. **Manual edit** in the Inspector: no job — save, lock, publish to every owner immediately.

### 6.3 Publishers
`MarkerPublisher` (parallel to `OutputAdapter`): `capability(server) -> Ready | NeedsPlugin | NeedsPass |
NeedsLocalDb | NeedsPlexDetectionOnce | Disabled`, `read(item_id) -> list[Marker]`, `write(item_id, markers)`.

**PlexMarkerPublisher** (opt-in per Plex server; Pass servers only)
- DB path from that server's `output.plex_config_folder` (`Plug-in Support/Databases/com.plexapp.plugins.library.db`).
  Check the directory's filesystem type; network mount (NFS/SMB/CIFS, Docker Desktop shares) → `NeedsLocalDb`, Plex
  stays read-only with a clear message.
- Resolve `metadata_item_id` + all `media_parts` for the item (existing bundle lookup).
- One short transaction, `busy_timeout=30000`: delete our types' `taggings` for the item; insert rows on Plex's
  `tag_type=12, tag=''` row; rewrite `pv:intros`/`pv:credits` in every part's `extra_data` (sorted keys, rebuilt
  `url`). Write credits start as `served − 2000 ms`.
- Tag row missing → `NeedsPlexDetectionOnce` (never create it).
- Multi-version items share one marker set: publish only when all parts' decisions agree within 2 s.
- Unknown schema (columns/JSON shape differ from 1.43) → stop writing, show message.
- Never write `tags`; never run integrity checks with stock SQLite (custom tokenizer).
- Warn when Plex's own detection is on (it can force-overwrite); reconcile re-applies.

**JellyfinMarkerPublisher**
- Extend **Media Preview Bridge** (`jellyfin-plugin/`, route prefix `MediaPreviewBridge`, today `Ping`,
  `ResolvePath`, `POST Trickplay/{itemId}`): add `POST /MediaPreviewBridge/Markers/{itemId}` (store JSON in plugin
  data folder, run providers with `forceOverwrite:false`) and `DELETE` same path; provider named
  "Media Preview Bridge". Prototype: `evidence/plugins/jellyfin-10.11/` and `-12.0/`.
- Two builds per release: 10.11 (net9, `Jellyfin.Controller` 10.11.0) and 12.0 (net10, `CleanupExtractedData`).
  Manifest carries both `targetAbi`s. App installs/updates via the existing `install_plugin()` flow
  (`PLUGIN_REPO_URL` manifest).
- Types: Intro, Outro (credits), Recap, Preview.

**EmbyMarkerPublisher**
- New **Media Preview Bridge for Emby** plugin: `POST /MediaPreviewBridge/Markers/{internalId}` stores markers,
  `SaveChapters` keeping `Chapter` rows, re-applies on `ItemUpdated`. Prototype: `evidence/plugins/emby-4.10/`
  (lab route `/markerslab/set`).
- Builds for 4.9 and 4.10. Submit to the Emby catalog; app installs via `POST /Packages/Installed/{name}`. Until
  accepted: manual DLL install instructions.
- Types: IntroStart, IntroEnd, CreditsStart.

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
5. **Season decision** (cheap numpy matching) runs when a season's last episode in the job finishes, in the
   dispatcher's completion callback, not in a worker slot.
6. **Priority and gate.** Webhook-triggered Intro & Credits jobs submit at NORMAL (preview jobs are HIGH), so previews
   drain first; backfill and schedules submit at LOW; users can change it live with the existing priority API.
   Intro & Credits jobs count toward `max_concurrent_jobs` like any job.
7. **Text detection on the worker's device.** One long-lived helper subprocess per GPU device
   (`python -m media_preview_generator.markers.textdet`), started lazily by the first marker item on that device;
   requests from that device's workers are serialized. Why a subprocess: the Vulkan loader reads its env once per
   process, and NVIDIA needs overrides (`VK_DRIVER_FILES`, `__EGL_VENDOR_LIBRARY_FILENAMES`) that hide other GPUs —
   the plex host has NVIDIA + Intel; a driver crash or hang can't take the web app down; the WebGPU plugin has a known
   Linux hang at shutdown without adapters (ORT PR #29591). On start the helper runs a 20-frame self-test against
   CPU and falls back to a CPU helper if slower or broken; result cached per device for the process lifetime.
   Measured: storage P5000 13.3 vs 18.7 ms; plex TITAN RTX 4.8 vs 7.7 ms (GPU kept); plex Intel UHD 770 16.1 vs
   8.0 ms (→ CPU). Device mapping: worker device (CUDA index / render node) → PCI bus id → EP device with the same
   `pci_bus_id`; phase 3 must prove the EP honours the chosen device (plugin README: it "selects the physical GPU
   independently").
8. **Per-job pause** for Intro & Credits jobs: change the job pause/resume routes to set the job-level flag for
   `kind=intro_credits` (global pause still pauses everything). This is what "pause a long backfill without touching
   previews" needs; today it is not possible.
9. **Webhooks:** `_execute_webhook_job` submits the preview job as today, then an Intro & Credits job for the same
   files when any owning server has markers enabled, items grouped by season folder. No extra debounce: the lower
   priority already runs it after the previews.
10. **Cancel:** online lookups and the textdet helper check `cancel_check` between requests; ffmpeg steps use the
    existing cancellation path.

## 7. UX

Mockups with real data (owner-approved direction): artifact link in §0. All non-obvious controls get ⓘ tooltips.
Show a mockup and confirm wording before building each screen.

1. **Servers page → server card → Edit → new tab "Intro & Credits"** (after "Webhook & Scanner"). Per server:
   - Switch "Send intro & credits markers to this server".
   - Libraries with checkboxes (sports-type unchecked by default).
   - Status block. Plex: write method (database), Plex Pass state, DB location + local-disk check, Plex's own
     detection on/off, "If Plex re-detects: put ours back / keep Plex's". Jellyfin/Emby: plugin name, installed vs
     required version, Install/Update button, which marker types the server can show.
   - Plex only: turning it on asks the database-write confirmation (once per Plex server; stores
     `db_write_confirmed_at`): no API exists; tested on Plex 1.43, stops if the DB looks different; must be same
     machine; Plex re-detection replaces ours and we put them back; viewers need Plex Pass or Plex Home.
   - Servers page cards themselves are unchanged.
2. **Settings → Intro & Credits** (shared detection only): detect Intros / Credits / Recaps; "Publish when"
   High / Medium; "Never overwrite my edits"; ordered sources (chapters, TheIntroDB + optional key + today's usage
   from its headers, IntroDB.app, SkipDB, season audio, credit text, markers already on servers) with measured numbers
   in ⓘ copy.
3. **Preview Inspector → "Intro & Credits" tab:** decision lane + evidence lanes (Chapters, Season audio, online
   sources, each server's current markers) in two zoom windows (first / last 3 min); per-server "will add / will
   replace"; Adjust, Lock, Re-detect, Publish.
4. **Inspector → Season view:** per-episode intro/credits, evidence chips, per-server dots, "Needs review", bulk
   publish.
5. **Dashboard → job queue:** Intro & Credits jobs linked under the preview job; per-server "Markers written × N /
   reused / needs review / skipped (reason)"; source counts.
6. **Setup Health:** plugin missing/outdated, Plex Pass missing, Plex marker tag row absent, Plex DB not local, Plex
   detection overwrite risk.

## 8. Settings and migration

`upgrade.py`: `_CURRENT_SCHEMA_VERSION` 14 → **15**. Adds, default **off**:
```json
"markers": {
  "detect": {"intro": true, "credits": true, "recap": false},
  "publish_when": "high",
  "respect_locks": true,
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
Per server (`media_servers[]`):
```json
"markers": {
  "enabled": false,
  "library_ids": null,
  "plex": {"db_write_confirmed_at": null, "on_plex_redetect": "restore"}
}
```
`library_ids: null` = all libraries except sports-type. `plex` block only on Plex servers. TheIntroDB key is a secret:
never logged, masked in UI and API responses.

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
- textdet helper: self-test picks GPU/CPU; crash → CPU; cancel between requests; device → PCI mapping.
- Online clients: header pacing, 429 backoff, key masking.

### 10.2 Accuracy harness (move into repo under `tests/eval/` or `tools/eval/`, not in CI)
- TV intros: 118 episodes (`evidence/eval/named_seasons.json`, truth from chapters) → report useful/wrong/missed.
- Credits: 80 files (`evidence/credits/movies40.json`, `tv40.json`, `adjudicated.json`) + the 205-movie chapter set.
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
- Every push: `ci.yml` tests. Label **`build-docker`** → `ghcr.io/stevezau/media_preview_generator:pr-<N>`
  (docs-only pushes don't build).
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
3. **Credits text.** Keyframe tail sampling, rule J, larger hand-checked set, tuning. *Done when:* harness ≥ §5.4
   numbers with 0–1 early per 80.
4. **Polish.** Adjust/Lock editor, AniSkip, Setup Health checks, helper container for Plex on another machine, docs.

## 13. Risks and open items

1. **TheIntroDB terms** — used without authorization; per-user keys; must degrade gracefully.
2. **Plex DB writes are unsupported by Plex** — opt-in with confirmation; unknown schema → stop.
3. **Plex client display** — proven at the API (served XML identical to native), not yet seen in a real Plex app
   (needs a claimed lab server). Verify in phase 1.
4. **Plex Pass for viewers** — non-Pass viewers never see skip buttons; say so in the UI.
5. **Emby catalog acceptance** — not guaranteed; manual install fallback.
6. **Multi-version items** — Plex shared markers + Jellyfin alternate versions need a lab test in phase 1.
7. **Credits truth is subjective** (epilogues, names over footage, post-credit scenes) — adjudications recorded.
8. **Jellyfin 12.0 catalog install** of a dual-ABI manifest is untested (manual load works).
9. **TheIntroDB keyed limit** (1,000/day) unverified.

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
  validates the item's own parts. A follow-up doesn't wait while its preview job counts down to a retry. Per-file outcomes gain `markers_waiting` and `markers_skipped`; any server failure makes the file
  count as failed unless another server was written.
