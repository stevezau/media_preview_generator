# Season audio v8: the end-picture check on one scaler — evidence

Spec §5.3 "End picture", §5.4 Frames, §14 2026-09-25 "Season audio v8".

Season audio's end-picture check (#310) read its 320×180 frames with each vendor's own scaler (`scale_cuda`,
`scale_vaapi`, swscale's bicubic on the CPU), kept as `frames.decode_command(vendor_scaler=True)` when #312 moved credit
text onto one scaler, so #310's measurements held. Partners of one season can be decoded by workers of different
vendors, so it was the last place a worker's GPU vendor could change an answer. It now reads frames the way credit text
does: the whole decoded frame downloaded in the stream's own format (NV12 or P010, from the pixel format
`probe.stream_starts` now reads with the start times), then scaled by the nearest pixel. `vendor_scaler` is gone.

Stored results: `end_picture.CHECK_VERSION` 1 → 2 (cached shares aren't read back) and `SEASON_AUDIO_VERSION` 7 → 8
(answers are due again). Measured and written before the version re-run (§14 2026-09-25 "Production audit after #312")
made season audio's answers carry the check's version too (`SEASON_AUDIO_ANSWER_VERSION`): since then the check
version alone makes the answers due. Merged with the speed by ear and the end card (`evidence/replaced-speed-endcard/`),
the values are `SEASON_AUDIO_VERSION` 9 and `CHECK_VERSION` 3, answers stored under 2009.

## Headline numbers (season audio alone, useful / wrong / missed)

| Set | Before, NVIDIA | Before, CPU | After, NVIDIA | After, CPU |
|---|---|---|---|---|
| lab 118 | 91 / 12 / 15 | 91 / 12 / 15 | 91 / 12 / 15 | 91 / 12 / 15 |
| held-out 175 | 124 / 4 / 47 | 124 / 4 / 47 | 124 / 4 / 47 | 124 / 4 / 47 |
| Accused (+1 file without a title card, no answer either way) | 3 / 0 / 53 | 3 / 0 / 53 | 3 / 0 / 53 | 3 / 0 / 53 |
| library chapter set | 111 / 59 / 54 | 111 / 59 / 54 | 111 / 59 / 54 | 111 / 59 / 54 |
| Bones S05–S08 | 82 / 0 / 0 | 82 / 0 / 0 | 82 / 0 / 0 | 82 / 0 / 0 |

- Every run measured the same 303 end-picture shares from scratch (406 decodes; no read failure, no GPU decode fell
  back to the CPU). Bones needs none: every intro there starts after 30 s.
- **After: NVIDIA and the CPU give identical answers and identical shares (303 of 303) on every set.**
- **Before → after on NVIDIA: no answer moved.** One share moved: Bob's Burgers S10E07 against S10E08, the 0–18.7 s
  title sequence, 0.83 → 1.0 (a pass either way). Its instant at 18.2 s correlated 0.599 with `scale_cuda` (just
  under the 0.6 limit), 0.609 with the CPU's bicubic and 0.604 with the one scaler (`share_detail.py`). Frame check
  (`frame_pair.py`, 17.7 s and 18.2 s): both episodes show the same storefront title card, only the gag sign next door
  differs, so "alike" is the right reading. Before on the CPU already matched after on every share.
- Frames (`frames_vendor_check.py` on the integration test's 12 s 1280×720 mandelbrot clips): before, CUDA and CPU
  planes differ (8-bit mean |d| 1.29, max 83; 10-bit 1.44, max 85); after, byte-identical (7 of 7 frames each).

## Production re-run cost

From the production markers.db snapshot of 2026-09-24 18:26 (read-only; `ep_prod_cost.py`): 466 present TV files
have a fingerprint; 103 cached shares under check version 1, for 47 files. The season audio bump makes all 466 answers
due; they match again from their fingerprints (3,017 pairs within today's groups; the pair cache is keyed by the season
audio version too). Only files whose walk meets a cluster starting in the first 30 s decode: **63 files in 9 seasons**
(46 of the 47 with a cached share among them), **124–240 shares, 163–284 decode windows** (178 with production's own
cached verdicts), each a 3.5–4 s stretch at 2 fps, well under a second of worker time apiece on the harness runs. The
snapshot's season audio answers were still at v4 (1,124 files) and v5 (219), so the release that carries v7 already
re-runs every one of them: v8 adds no re-run on top, only the re-decode of those windows.

## Files

| File | What |
|---|---|
| `ep_sets.py` | Season audio alone on one set with one tree's `end_picture.Reader` on NVIDIA or the CPU: the four #310 sets through the `intro-pick` harness (fingerprints, truth, `lab.Season`, scored as `../intro-guards/t3_sets.py`), Bones through the tree's `tools.markers_eval.SeasonStep` with the speed clock (as `../speed/bones_eval.py`). Every share decoded fresh; every decode counted by where it ran and its filter |
| `run_all.sh` | The four runs (before/after × NVIDIA/CPU), one at a time at nice 19, then the comparison |
| `compare_runs.py` | Tallies per run, answers that moved, shares that differ between two runs |
| `frames_vendor_check.py` | One tree's end-picture decode on the CPU and CUDA: are the 320×180 planes byte-identical? |
| `share_detail.py`, `frame_pair.py` | One share instant by instant (correlation, flat statistics) on one tree and device; the two files' frames side by side at given instants |
| `ep_prod_cost.py` | The production season step replayed from the markers.db snapshot's fingerprints (read-only), counting the end-picture checks the walk asks for |

The runs used copies of `media_preview_generator/` and `tools/` at 40311c3 (before) and with this change (after) in
the session scratchpad; the four sets' data (`intro-pick/`) and the Bones truth (`../speed/truth.json`) are
local-only. Result JSONs, share pickles and logs name library files: they go to `local/` (gitignored).
