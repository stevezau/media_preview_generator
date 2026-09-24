# 640×360 re-reads — investigation scripts

The write-up is `../small-text-retry.md` (what changed, the measurements, the open gaps and the **Before release**
checklist for the plex host); its evidence script `../small_text_retry.py` reads a file list at both sizes. These are
the investigation scripts it cites:

| File | What | Headline |
|---|---|---|
| `force640.py` | What the 640×360 reading answers on files the 320×180 reading answered (forced), with the app's own `_read_credits` at scale 2 | Accused's six live-check files that answer on epilogue and verdict cards at 320×180 all land on the roll at 640×360 (−1.5 … +7.5 s) |
| `stretch_probe.py` | After a 320×180 answer: the stretch from the run's last credit keyframe to the end of the file, and what 320×180 saw there | The coordinator's proposed trigger (no text after the run) fires on none of the six: every stretch holds captions or stray boxes |
| `accused_truth_all.json` (local-only) | Frame-checked credits truth on all 57 Accused files (3 s sampling) | With the "rest of the file at 640×360" rule: wrong 30 → 5, useful 24 → 49 |

**Open gap (Accused epilogue cards).** Epilogue cards glued onto the front of a roll that 320×180 does see are still
answered 11.5–40.5 s early on 4 Accused files (and S03E09 on its card 8.5 s early): that answer runs to the end of the
file, and no cheap trigger measured separates it from healthy rolls (spec §14 2026-09-25).

The scripts ran from the session scratchpad; `MEDIA_PREVIEW_TEXTDET_MODEL` points at `../bench/textdet-model/`
(local-only).
