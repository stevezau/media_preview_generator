# Credits accuracy fixes after the 2026-09-27 sflix audit

The 2026-09-27 audit (`local/audit/report.md`, local-only) frame-checked a stratified sample of the markers
published to sflix's Plex and compared credits with Plex's own on the 86 files where both exist. This lane fixed the
failure shapes it found, measured before (`dev` at `4a34687`) and after (`fix/credits-accuracy`), and frame-checked
every marker the new rules moved that had no audit truth, one to three per show. Spec: §5.4 "Version 6", §5.5 rules
3, 4 and 16, §14 2026-09-27 "Credits accuracy".

Local-only (gitignored, library paths): every `*.json` here and `local/` (the audit's folder with its copy of
sflix's `markers.db`, run logs, frame sheets). They exist only on `storage`, in the main checkout's copy of this folder.

## Method

- **Items** (`build_items.py`, `items.json`): the audit's verdict set (135 markers: 88 credits, 51 intros, with the
  five movie chapters the audit counted) and the Plex comparison (85 credits). Truth is the audit's frame check: the
  first credit card, the title sequence's edges. A truth the audit only gave as "the marker was right" is flagged
  `approx`; a new decision moving more than 5 s from such a marker was frame-checked (`checks.json`; House S01E03 and
  S01E13 have a range: from the story's last frame to the first card, both edges right).
- **Split** (`split.py`, `split.json`): by file, `random.Random(20260927)`, 70 % tuning, 30 % held out. Every rule was
  shaped on tuning files and on files outside both sets; the held-out numbers were read once at the end.
- **Credit text** (`vtext.py`, `run_base.sh`, `run_work.sh`): the app's own detector (`find_credits` through the
  harness's decode cache, GPU on storage's P5000, the worker's CPU rerun) on every credits file of both sets and on
  every file of the audit's `markers.db` whose credits a chapter decided (`chapter_files.py`, `extra_files.json`), for
  the base tree and the work tree. The decode cache is keyed without `rule_j.py`, `detector.py` and `decide.py`, so
  the work run reuses the base run's decodes: same frames, only the rules differ.
- **Replay** (`replay.py`, `replay.sh`): every file of the audit's `markers.db` copy decided again by each tree's
  `decide()` the way `pipeline._decide` builds its context, with that tree's credit text, then the rule-change keep.
  Season audio answers for Spring of the Blade S01 (whose chapters now wait for it) were read with the app's matcher
  (`spring_inject.py`, `audio_inject.json`). `metrics.py` tallies the verdict and Plex sets per split; `moves.py`
  lists every marker that moved more than 5 s; `sheets.py` makes the 1 fps frame sheets for them.

## Results

Credits wrong = start more than 5 s from the first card (early or late); skips story = more than 5 s early.

| Set | Base credits wrong | Work credits wrong | Base skips story | Work skips story |
|---|---|---|---|---|
| Verdict, tuning (61) | 14 (23.0 %) | 6 (9.8 %) | 3 | 1 |
| Verdict, held out (27) | 8 (29.6 %) | 4 (14.8 %) | 1 | 1 |
| Verdict, all (88) | 22 (25.0 %) | 10 (11.4 %) | 4 | 2 |
| Plex comparison, all (81 judged) | 16 (19.8 %) | 12 (14.8 %) | 2 | 1 |
| Plex's own on the same files (80 judged) | 8 (10.0 %) | | 8 | |

Intros on the verdict set: 4 of 51 wrong before and after, skipping story 2 → 0 (Spring of the Blade S01E14's intro
now ends at the title card; its start is still 11 s early, on licence cards; Somebody Somewhere S03E07's lone SkipDB
intro goes to Needs review; Game of Thrones S03E09 right; S03E03's right lone SkipDB intro goes to Needs review too).

The ten credits still wrong are all but two late starts: rule J reading the roll after its first cards over footage
(17 Again 82 s, '71 120 s with SkipDB agreeing, 21 Jump Street 102 s, #SKYKING 24 s, several 6–12 s). The two early
ones are chapters credit text can't correct: 10 Things I Hate About You (the chapter 43 s early, rule J's own start
166 s late) and A Beautiful Imperfection (rule J starts on the same epilogue text as the chapter).

## What moved on sflix's copy

Every file of the audit's `markers.db` decided again (`moves.py replay_base_t2.json replay_work_t3.json`): 104 credits
starts moved more than 5 s, 7 intros changed. Frame-checked, one to three per show:

- **Credits chapters moved by credit text** (74): A Different World S02–S05 (53, credits over the closing footage after
  the executive producers' card; the chapter sat on the Netflix dub cards), Shakespeare and Hathaway S06 (10), La Brea
  S01E02–E05, #SKYKING, 10 Days of a Bad Man, 30 Coins S01E08, The Terrors of Jordan Mendoza S01E02, A Christmas Carol
  (1984; the chapter on the last shot). All right, or nearer (A Different World S03E14 8.5 s after the card over the
  last scene, #SKYKING 24 s late) and none earlier than the first card.
- **Refused by rule J's lit-keyframe checks** (before them these moved): The Fixers S01E10 (a dossier on a screen,
  19 s of story), A Different World S01E03 and S01E09 (T-shirt text, 17 and 10 s), S01E21 (a wall poster, 11 s),
  S05E16 (an epilogue caption, 13 s); The Half of It and Dark Matter S02E05 (chapters right on black cards 320×180
  reads nothing on; moved 30 and 22 s late).
- **Credit text's start over an online one** (24): Stargate Atlantis S01, House S01 (from SkipDB's time on the last
  shot to the black before the first card, or up to 4 s into the cards where rule J missed the two smallest), The
  Vampire Diaries, Beavis and Butt-Head S03, Get Shorty S02, Once Upon a Time S01E02 (SkipDB 7 s into the last scene),
  Peppa Pig S09E01. All right.
- **Rule J version 6's refine** (4): Accused S04E02 and S05E08, Beavis and Butt-Head S03E12 (were 5–9 s late), Homicide
  Hunter S06E13 (was 13 s early on a mugshot caption). All right by the audit's truth.
- **Credit text over a credits chapter** (1, rule 3 as before, reached now that credit text reads chapter files):
  Lioness S02E04, whose 5 s "Credits" chapter sat on the closing logos 50 s after the first card. Right.
- **Intros**: Spring of the Blade S01E02/E03/E14 end at the title card (the WEB chapters ran 5–37 s into the episode);
  Game of Thrones S03E04/E09 start at the title sequence (6 s, was 63 s); Somebody Somewhere S03E03/E07 to Needs review.

## Cost

A file whose credits a "Credits" chapter decided now gets one credit text read (then saved): on storage's P5000 a
median 6.5 s per episode (90th percentile 9.5 s, 246 files) and 10.9 s per film (3 files), decode and detection
together (`gpu_cost.py vtext_base.log`). On sflix's copy that is 342 files once. Version 6 also reads again, once, the
617 files whose credits rest on credit text.
