# I Survived a Serial Killer S01 — the credits text regression set (2026-09-25)

The production audit after #312 found two episodes of this season answered wrong by credits text: S01E04 92 s early
(story captions), S01E14 68 s early (court footage). The write-up is `../small-text-retry.md` ("The audit's two
regressions") and spec §14 2026-09-25.

| File | What |
|---|---|
| `isurvived_truth.json` (local-only) | Frame-checked truth for all 16 episodes: the first credit card, `{"<file>": seconds}`, 0.5 s sampling. The harness reads it as `--sets isurvived` |
| `local/` (local-only) | `E01.jpg` … `E16.jpg`: the last 160 s of each episode 2 s apart; `E??-zoom-*.jpg`: 0.5 s apart around the first card (E11 twice: its roll starts later than the others'). The trace logs below, and `accused_s05e01.jpg` (the one Accused answer that moves more than 10 s) |
| `trace.py` | The app's own `find_credits` on one file, printing each reading's rows and boxes; `--vendor` decodes the 320×180 pass with version 3's per-vendor scaler |

Every episode's roll is ~27–29 s of small cards over footage, starting 27–29 s before the end (E12 is a double
episode). Plex has no credits marker on any of them.

## The findings, from the traces (`local/*.log`)

- **S01E04, why version 3 got it right** (`e04_dev_gpu.log` against `e04_vendor_gpu.log`, the same keyframes): the
  roll's 320×180 credit keyframes are 1231–1245 s with the nearest-pixel scaler, 14.0 s, under rule J's 15 s run; the
  1249.25 s keyframe boxes 2 there and 4 with `scale_cuda`, so version 3's run was 1231–1249 s (18 s) and answered
  1231 s. swscale's bicubic (`e04_vendor_cpu.log`) finds no 320×180 run at all.
- **S01E04, why the rest of the file found nothing** (`e04_dev_gpu.log`): at 640×360 the roll boxes 8, 12, 13, 18, 4,
  6, 6, 12, 9, 6 boxes on 1231–1249 s, but 320×180 boxed its cards as blocks and every line inside them was left out:
  0, 1, 0, 5, 2, 2, 0, 5, 7, 4 boxes and no run.
- **S01E14, why the overlays don't take the court footage** (`e14_dev_gpu.log`, `e14_dev_gpu_all.log`): its date line
  (30,19)–(86,29), logo (247,145)–(266,154) and small print in the frame's bottom corners are boxed only from 1171 s on
  (none of the tail's keyframes before), where the 640×360 run starts, so `rule_j.overlay_boxes` -- which gathers the
  story before the run and needs a box across 80 % of it -- never sees them.
- **Accused S04E05 and S07E02** (`s04e05_A.log`, `s07e02_A.log`): why "count all text after the end" isn't enough --
  a card on black 20 and 12 s before the roll, boxed whole at 320×180, joins the roll at 640×360.
- **Still open**: E13 (`e13_final.log`, story graphics glued onto the front of a roll 320×180 sees; an answer to the
  end of the file is never read again) and E11 (`e11_final.log`, the roll's text keyframes span 14 s at either size).

## Commands (storage, nice 19)

```bash
cd /home/data/workspace/plex_generate_vid_previews
MARKERS_EVAL_EVIDENCE=<this checkout's evidence folder> nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval \
  credits-text --decode gpu --sets 80,205,accused,isurvived --online --json <local-only>.json
TRACE_BACKEND="webgpu cuda:0" TRACE_FFMPEG=/usr/bin/ffmpeg nice -n 19 /home/data/.venv/bin/python \
  docs/design/intro-credits/evidence/credits/isurvived/trace.py "$PWD" "<episode path>" --from 1150 --to 1266
```
