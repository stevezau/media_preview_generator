# Lab matrices brought up to `dev` `cab4ccf` (2026-09-27)

Every row below failed on `dev` because its expectation predated an intended change, not because the app was wrong.
Each keeps its row's intent; none was deleted. Rows not listed were already passing and are unchanged. The cites are
spec sections (`../../spec.md`), §14 decision entries, commits and code.

Clean full run on `plex-previews:dev-cab4ccf` (`phase4_row13_reset.py`, then a new empty config volume
`mlab_app_config_lab3`, `./phase2_matrix.py configure`, `./phase4_row13_run.sh`; log `results/row13-run.log`):
**phase 1 16 of 16, phase 2 24 of 24, phase 3 12 of 12.** The full run before it (volume `mlab_app_config_lab2`, log in
`results/before-row13-run-20260927T110804/`) had phase 3 row 6 fail (the known cancel bug) and row 16's premise fail
(the sampler, fixed below); everything else passed.

## Phase 2 (`phase2_matrix.py`)

| Row | Old expectation | New expectation | Why (cite) |
|---|---|---|---|
| 2 | Forced runs at High, then Medium: every Synth Audio S01 intro in Needs review (R2, season audio never alone), no server serves an intro; the High run fingerprints with at most two chromaprint ffmpeg at once, each `-threads 2` | Two forced runs: every S01 intro decided by season audio alone (`decided_by` `["season_audio"]`) near the theme, and all five servers serve it; S02E01's previous-season hint never decides and nothing of ours is served on it (unchanged); the first run fingerprints with at most one chromaprint ffmpeg per worker (count read from `/api/jobs/workers`) and no `-threads`; the second reuses the cache (unchanged) | "Publish when" removed: §14 2026-09-24 "The strict mode was removed", `upgrade._migrate_to_v16`. Season audio decides an intro alone: §5.3, §5.5 rule 6, §14 2026-09-24 "Season audio may publish an intro alone" (`decide._may_decide_alone`). Fingerprints capped by the worker pool, at ffmpeg's own thread count: §5.6 "Threads" and "No app-wide fingerprint limit", #314 (`1f19472`, `markers/audio/fingerprint.py`) |
| 3 | At High, S01E02's intro is Needs review although Plex's own intro agrees with season audio (G3); nothing of ours on Plex for S01 | Our intro is taken off S01E02 first; after Plex's forced detection its intro agrees within 5 s (premise, unchanged); season audio then decides alone at its own edges, reason "single source (season_audio)", never as a pair with Plex's marker (G3); under "Use ours" the job writes ours back and Plex serves it; a normal job over S01 writes ours back where Plex's detection replaced them | §14 2026-09-24 "An agreeing server marker no longer holds season audio back" (`decide._decide_from_single_source`). The clearing step: the app never reads a Plex item it published to as evidence (`pipeline._read_server_markers`, `MarkerStore.published_to_item`, §13 item 17), and row 2 now publishes every S01 intro, so without it the G3 premise can't form |
| 4 | S02E01 and the new S02E02 still Needs review; no server serves an intro on S02 | Both decided by season audio alone near the theme; every server serves both. The Season job checks are unchanged | §14 2026-09-24 "Season audio may publish an intro alone" |
| 5 | Rick and Morty S01 at High | The same job and checks (season audio in `decided_by` somewhere, no decided intro wrong against the 11 online truth cases) at the app's only rules | "Publish when" removed (row 2's cite) |
| 6 | "Synth Audio S01 has no intro on Emby (G3: needs review)" | "Synth Audio S01 intros on Emby near the theme": one IntroStart and one IntroEnd, start ±15 s, end ±5 s (the row's check that Emby's chapters equal the decisions is unchanged) | §14 2026-09-24 "Season audio may publish an intro alone" |
| 10, 12 | Set "Publish when" to High first | Nothing to set; checks unchanged | "Publish when" removed (row 2's cite) |
| 18 | At most two chromaprint ffmpeg at once, `-threads 2` | At most one per worker, every one with no `-threads`; fingerprints deleted must be a positive count | §5.6, #314 (row 2's cite) |

## Phase 3 (`phase3_matrix.py`)

| Row | Old expectation | New expectation | Why (cite) |
|---|---|---|---|
| 2 | At High, credit text alone is Needs review and nothing is published; then at Medium it publishes | The High half's feature is gone, so it is replaced by the same risk under the new rules: a `publish_when: high` posted as an older client or settings.json has it is taken and not kept, and the forced job's lone credit text answer is still decided (`decided_by` `["credits_text"]`, the text's end) and published. The Medium half's checks (five servers, Emby's note, the Inspector lane) are unchanged | §8 schema 16, §14 2026-09-24 "The strict mode was removed" (`markers.settings.validate_global` drops the key) |
| 3 | A decode with `scale_cuda=320:180`; `-threads 2` on every decode seen | The file's decodes on CUDA with `hwdownload` and `scale=320:180:flags=neighbor`; every decode on both legs scales by the nearest pixel, none with a vendor scaler; every file decode on the GPU with `-threads 2 -filter_threads 2` (the row's GPU worker has `ffmpeg_threads` 2). The reference decode check's clips are recorded, not thread-checked | One scaler: §5.4 "One scaler on every path", §14 2026-09-24 "Credits text version 4" (#312, `40311c3`). The reference decode check (§5.4, 2026-09-25) runs once per GPU on its own daemon thread, not as a worker's decode. Worker threads: §5.6 "Threads" |
| 5 | Exactly one "this request runs on the CPU because its GPU helper failed" line; no "times in a row" or "for the rest of this run" warning | Exactly one "this request is read on the CPU and the GPU is tried again in" line; no "for the rest of this run of the app" line; one "back on the GPU after 1 failed request" line (new check). The job and answer checks are unchanged | The helper's wording and back-off since #314 (`textdet_helper._gpu_failed`, `_gpu_worked`, `GPU_RETRY_BASE_S` 5 s doubling); "times in a row" no longer exists in the code |
| 8, 10, 16, 17 | Set "Publish when" to Medium, then back to High | Nothing to set; checks unchanged (row 17's cleanup no longer says "a High run") | "Publish when" removed (phase 2 row 2's cite) |
| 9 | `-threads 2` on every decode seen | `-threads 2 -filter_threads 2` on every decode of the file, all on the GPU; the reference decode check's decodes are recorded, not checked | Row 3's cites |
| 11 | Re-runs phase 2 rows 1, 3, 8 and phase 1 rows 2, 7 | Unchanged; it failed only through phase 2 row 3 | |
| 16 | Premise: at least two decodes seen, one a refine window | The same, counting only the open movie's own decodes (the reference decode check's clips would otherwise count toward "two"), seen by a faster sampler (below) | §5.4 reference decode check (2026-09-25). The sampler: in the first full run this premise failed on a working app (the answer's 9:01 start comes from a 1 fps refine window, yet only the keyframe pass was seen): a refine window decodes in 0.7–0.9 s on the P5000, and the old poll (a new `docker exec` every 0.5 s) missed it |

Phase 3 row 6 (a cancel landing at pickup is ignored) is a known product bug being fixed on `fix/cancel-at-pickup`;
its check is unchanged. It is intermittent: it failed the first full run ("the decode stopped within 10 s: False") and
passed the clean one. Row 17 is a phase 4 row that lives in this file; only its "Publish when" calls were dropped, and
it wasn't run here.

## Scripts

- `app.sh` takes `MLAB_APP_CONFIG_VOLUME` (default `mlab_app_config`), the same change as `fix/credits-remaining`'s
  `f573911`: each run gets a new empty config volume, and old ones are left in place.
- `phase2_matrix.delete_fingerprints` now runs as the app's user and opens markers.db `mode=rw`. As root on a fresh
  config it created an empty root-owned markers.db that the app couldn't write, so every Intro & Credits job failed
  ("attempt to write a readonly database"). On a config with no markers.db it deletes nothing and says "0".
- `phase4_row13_run.sh` moves the previous run's log in with its archived results, and says to pass the run's
  `MLAB_APP_IMAGE` and `MLAB_APP_CONFIG_VOLUME`. `phase4_row13_reset.py`'s notes describe the new-volume flow.
- `phase4_matrix.set_settings` no longer sets the removed "Publish when".
- `phase3_matrix.ProcessSampler` (rows 3, 4, 5, 7, 9, 16) runs one `ps` loop inside the app container every 0.05 s for
  the whole block, instead of a new `docker exec` every 0.5 s, and stops it through a stop file when the block ends.
