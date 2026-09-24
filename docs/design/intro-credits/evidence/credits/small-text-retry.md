# Credits text version 4: one scaler, and a 640×360 reading of tails with no answer

2026-09-24, storage (P5000 NVDEC; text detection on the CPU helper: WebGPU gives identical boxes at both sizes, and
the helper's self-test now checks 640×360 too). Spec §5.4 and §14 2026-09-24.

## What changed

1. **One scaler on every decode path.** `scale=W:H:flags=neighbor,format=nv12` after `hwdownload` in the stream's
   own format (NV12 / P010; `-extra_hw_frames 8` on VAAPI) on CUDA and VAAPI, after ffmpeg's own download elsewhere.
   `scale_cuda`, `scale_vaapi` and swscale's bicubic each blurred 4–11 px text differently, so answers depended on the
   worker's vendor (80-file rule J: NVIDIA 66 within 10 s, CPU 61, Intel 58). Now the frames are bit-identical: the CPU
   runs below equal the GPU runs file for file, and `test_frames_integration.py` asserts GPU rows == CPU rows (8- and
   10-bit, 320×180 and 640×360, CUDA; VAAPI in the lab image).
2. **A tail with no answer at 320×180 is read again at 640×360** (`detector.RETRY_SCALE`). Accused (2020)'s credit
   cards are 4–11 px tall at 640×360 and box nothing at 320×180. The larger reading finds its runs and reads a frame's
   own text without what the 320×180 reading boxed (an epilogue card read again glued onto the roll 13–34 s early on
   4 of 14 Accused files; a lower third's words boxed apart made a 36 s run 347 s early on A Season to Remember,
   CPU decode), and drops boxes taller than 15 px it alone found (dark footage boxed as blobs: Frankenstein: The
   Anatomy Lesson answered 400 s early at 19 px; 11 px lost Lisa Ann Walter's roll).
3. `CREDITS_TEXT_VERSION` 3 → 4: every stored answer is read again once (prod: 1,244 files, 912 found + 332 "nothing
   found", read-only copy of markers.db).

## The files that asked for it (prod "credits not found", frame-checked truth)

`small_text_retry.py notfound.tsv truth.json out.jsonl --decode gpu|cpu`; GPU and CPU give the same answers.

| | prod (dev) | version 4 |
|---|---|---|
| Accused, 14 files | 14 none | 12 useful (−4.0 … +9.5 s), 1 late (S03E06 +32.5), 1 wrong (S06E07 −353, a 320×180 answer, as dev's NVIDIA) |
| Habeas Corpus S01E04 | none | +2.5 s (640×360) |
| Taskmaster NZ S07E02 | none | none (dev's scaler + 640×360 found it at +1.5 s; the neighbor scaler doesn't) |
| Lioness S02E08 | none | none (no truth) |

Plex has no credits marker of its own on any of them. Nine Accused answers come from the 640×360 reading, all within
−4 … +8.5 s of the first card; the other five are the 320×180 reading's.

## Harness (`tools.markers_eval credits-text`), useful / wrong / late / missed

Chapter truth; "adj." uses the two frame-checked truths (Animal (2023) 11635 s, Lisa Ann Walter: It Was an Accident
(2026) 3331 s, where the chapters mark the last seconds of the roll).

| run | 80 text | 80 Medium | 80 High | 205 text | 205 Medium | 205 High | rule J 80 (≤10 s / early) |
|---|---|---|---|---|---|---|---|
| Plex | 47/13/1/19 | | | 124/61/11/8 | | | |
| dev, NVIDIA | 69/3/5/3 | 60/1/0/19 | 43/1/0/36 | 123/35/35/11 | 101/15/9/79 | 99/14/9/82 | 66 / 1 |
| dev, CPU | 63/2/7/8 | 57/1/0/22 | 43/1/0/36 | — | — | — | 61 / 1 |
| neighbor only (any vendor) | 68/4/5/3 | 60/1/0/19 | 43/1/0/36 | 122/34/37/11 | 99/15/9/81 | 97/14/9/84 | 65 / 1 |
| **version 4 (any vendor)** | 70/5/5/0 | **62/1/0/17** | 43/1/0/36 | 126/35/38/5 | **101/15/10/78** | 98/14/9/83 | 67 / 2 |
| version 4, adj. | 70/4/6/0 | 62/1/0/17 | 43/1/0/36 | 126/34/39/5 | 101/15/10/78 | 98/14/9/83 | 67 / 1 |

Gate: the 80 passes 5/5 on every run; the 205 fails the same 3 checks as dev (Medium useful < Plex, Medium wrong > 5,
High wrong > 3). Rule J on the 80 meets the spec (≥ 59 within 10 s, ≤ 1 early) with the adjudicated truths; on chapter
truth Animal counts early (the chapter is 437 s after its first card).

Where the changes come from (NVIDIA decode):

- **The 640×360 reading only adds answers**: 9 files, 7 useful (A Season to Remember, Once Upon a Time in the West,
  Once Upon a Studio, Oldboy, Revenant S01 ×1, …) and 2 late (Alex Borstein +44 s, Animal +39 s adj.), none wrong.
  Medium +3, High +1.
- **The neighbor scaler on NVIDIA** (the vendor dev scaled best on) moves answers both ways: Medium loses Two for the
  Money (useful → late) and A Child of My Own (useful, now outside Medium's agreement), text-only answers Tau and one
  Revenant episode become wrong, Pamela and Clerks become useful. On the CPU (and Intel) it is a clear gain: 80 Medium
  57 → 60 before the retry, 62 after.
- Medium wrong is 1 (80) and 15 (205) on every run: no Medium or High wrong was added by either change.

## Epilogue and verdict cards on Accused (the live check)

The six Accused files the prod live check found credit-text answers on all start before the roll, frame-checked:

| file | truth | prod = dev NVIDIA | dev CPU | version 4 (any vendor) | 640×360 forced |
|---|---|---|---|---|---|
| S03E09 | 2534.5 | 2526 (epilogue card) | 2162 | 2526 | 2539 |
| S07E06 | 2546.5 | 2328 (agrees with Plex) | 2328 | 2328 | 2546 |
| S05E02 | 2546.5 | 2487 (3 s verdict card) | 2487 | 2487 | 2554 |
| S04E01 | 2543.5 | 2463 (agrees with Plex) | 2463 | 2463 | 2548 |
| S07E05 | 2537.5 | 2523 (sentencing card) | 2453 | 2523 | 2536 |
| S07E09 | 2548.5 | 2338 (mid-episode card) | 2338 | 2338 | 2548 |

All six were 320×180 answers, so the 640×360 reading of tails without an answer never ran on them. The cause is the
same small text: at 320×180 the real roll boxes nothing, so the last text run is the cards before it. Read at
640×360 (`small-text-retry/force640.py`, the app's own `_read_credits` at scale 2), all six land on the roll (−1.5 … +7.5 s).

### Reading the rest of the file after an answer that ends in a scene (2026-09-25)

The proposed trigger -- the stretch after the run is long enough for a roll and 320×180 saw no text in it -- fires on
none of the six (`small-text-retry/stretch_probe.py`): S03E09 and S07E05's runs reach the end of the file (the cards are glued onto the
front of the roll, which 320×180 half sees), and the other four stretches hold captions and stray boxes (8–28 text
keyframes, 2–11 isolated credit frames). What the other four, S06E07 and 26 of Accused's 47 answers share is an end:
more than 30 s of the file follows the answer (Q3). So an answer with an end has the rest of the file, from its end,
read at 640×360 (the keyframes before it are the 320×180 ones, their text seen), and the roll found there is the
answer.

Frame-checked truth on all 57 Accused files (3 s sampling; 37 checked for this, `small-text-retry/accused_truth_all.json`, local-only), GPU decode:

| | useful | wrong | late | missed |
|---|---|---|---|---|
| version 4 | 24 | 30 | 3 | 0 |
| + read after an answer's end | **49** | **5** | 3 | 0 |

25 of the 26 answers with an end land on the roll (−1.5 … +12.5 s); S05E01 moves from −120.5 s to −27.5 s. Of the
live check's six, S07E06, S05E02, S04E01 and S07E09 are fixed, and S06E07 (−353 s → +1.5 s). Still wrong: S03E03
(−40.5), S07E05 (−14.5), S01E07 (−14.5), S07E07 (−11.5), S05E01 (−27.5), and S03E09 lands on its epilogue card
(−8.5 s, inside the useful window): cards glued onto the front of a roll 320×180 half sees, an answer without an end.
No cheap trigger measured separates those: a run that opens dark and turns lit is 3 of 8 right answers in the
harness sets.

The harness sets don't move: the 80 (text 70/4/6/0, Medium 62/1/0/17, no new early answer; CPU decode identical),
the 205 (text 126/34/39/5, Medium 101/15/10/78, High 98/14/9/83) and the online set (28/5/3/7, 33/5/3/2); only Come from
Away moves on, late either way (+241.6 → +324.6 s). 3 of the 80's answers and 17 of the 205's have an end and pay
for the read (26 decodes). On Accused the 26 triggering files each decode a median 81 more frames at 640×360: +7.1 s
median, +15.0 s at most. Taskmaster NZ S07E02 stays without an answer (it has none to read after).

## Cost

Per file, GPU decode, CPU text detection: 320×180 reading alone median 6.6 s (Accused, 225 keyframes); with the
640×360 reading 21.6 s. Text detection 17.2 ms/frame at 320×180, 60.9 ms/frame at 640×360 (CPU helper; WebGPU on the
P5000 19–21 and 36–39 ms). The CPU decode: 10.8 s and 26.8 s. Two kinds of file pay for a second reading: a tail
without a 320×180 answer, read again whole (26 % of prod's stored answers were "nothing found"), and an answer with an
end, whose rest of the file is read again (above: a median +7.1 s on Accused, and an intra-only file its whole tail
again, thinned). An answer that runs to the end of the file pays nothing.

## Review 4 (2026-09-25)

- **An intra-only file's rest of the file is read from the tail's own seek.** Its keyframe pass keeps one packet in
  48 counted from the seek (`noise=drop=mod(n\,K)`), so read from the answer's end it kept other frames than the
  320×180 reading (a generated all-I H.264: 539, 541, … against 0, 2, …), and none of the text that reading boxed was
  matched to them. It now seeks where the tail's pass did and drops the rows before the end
  (`test_the_rest_of_an_intra_only_file_is_read_on_the_keyframes_the_320x180_reading_read`, real ffmpeg). The cost:
  such a file with an end reads its whole tail again at 640×360, thinned (a movie's 900 s tail is 450 frames, ~27 s
  of CPU text detection at 61 ms a frame). A keyframe or VP9 pass still starts at the end: it decodes the same
  keyframes from any seek.
- **A 640×360 timeout of a tail without an answer is a timeout** (the decode's, or the look-back's shared deadline):
  no answer, and the file waits a day, as a 320×180 timeout does; before, it stored "nothing found" for good. After an
  answer a timeout keeps that answer, and any other failure (a decode that can't be read, a helper that died) still
  stores "nothing found" or keeps the answer, with a warning that says which.
- **A 640×360 text detection request carries 16 frames**, the pixels of 64 at 320×180, so the helper's 60 s
  per-request timeout and the frame queue's bytes hold at either size. `frames.decode_rows` at scale 2 with 16 and
  with 64 frames per request gives identical rows (240 s of keyframes, NVIDIA decode, CPU helper: Accused S06E07 89
  rows, Avengers: Infinity War 20, Black Panther 53).
- No answer moves on the measured sets: `frames.keyframe_thinning` thins none of the 301 files of the 80, the 205 and
  Accused (none is intra-only or VP9), and no 640×360 reading failed or timed out on them.
- **Films with a scene after the credits** (the 205, frame-checked): Avengers: Infinity War (8252 s, truth 8251.3)
  keeps its answer; Black Panther (7606 s, +242 s) and Superman (2025) (7610 s, +470 s) are late on the crawl after
  their mid-credits scene with or without the read after the end. All three have an end and are read again, and none
  moves. Of the answers that trigger the read in the 80 (3) and the 205 (17), only Come from Away moves (late either
  way).

## Before release

The plex host was down for this round; these wait for it:

- [ ] The helper's self-test on the plex host (`same_boxes_large`, 640×360) for the TITAN RTX and the Intel iGPU:
      the GPU keeps text detection only if its boxes match the CPU's at both sizes.
- [ ] A 4K tail on Intel VAAPI (a 10-bit HEVC movie): the 320×180 and 640×360 readings' decode times, with
      `-extra_hw_frames 8` and the full-frame `hwdownload`, well inside the decode timeout.
- [ ] The Intel/VAAPI answers with this code: the 80 (and Accused) with GPU rows equal to the CPU's, as on NVIDIA.
- AMD VAAPI stays untested (no hardware): `-extra_hw_frames 8` and the full-frame download have never run on an AMD
  GPU. A decode that fails there is a GPU failure, read again on the CPU with the same frames.
