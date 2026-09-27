# The credits errors left after #320 (2026-09-27)

After #320 (credits accuracy, `dev` at `cab4ccf`, live on sflix) the audit's Plex comparison still had ours 12 of 81
credits more than 5 s off against Plex's 8 of 80, and two films whose chapter skipped story. This lane found why each
is wrong, fixed the shapes a simple general rule covers, and measured the rest. Spec: §5.4 version 7 (steps 6 and 9,
"A credits chapter off the roll" in §5.5 rule 3), §14 2026-09-27 "The credits errors left after #320".

Local-only (gitignored, library paths): `local/` (every tree's answers, replays, harness output, the two ablation
trees). The verdict and Plex sets, their split and the audit's `markers.db` copy are #320's
(`../credits-accuracy/`, its `local/audit/`).

## Method

- **Base** is `dev` at `cab4ccf` (`git archive` into `../credits-accuracy/local/base_cab4ccf`), **work** this branch.
  `run_all.sh <tree> <tag> <base|work> [text sets replay]` runs one tree: credit text on the 430 verdict, Plex and
  chapter files (`../credits-accuracy/vtext.py`), the harness sets (80, 205, Accused, I Survived, the online cases;
  `run_sets.sh`, then `allsets.py` for the rows), and the replay of the audit's `markers.db` with that credit text
  (`../credits-accuracy/replay.py`, which now takes the base tree from `$CREDFIX_BASE` and the tree's own chapter
  label, `detector.chapter_origin`). Decodes are cached by the code that turns a command into rows (`rule_j.py`,
  `detector.py` and `decide.py` left out), so the work run decodes only the new 1 fps windows.
- **Scoring**: `../credits-accuracy/metrics.py` (verdict set 70/30 by file, `random.Random(20260927)`, held-out read
  once at the end; Plex set; Plex's own), `wrong_list.py` (every wrong item, what decided it and credit text's own
  answer), `compare_vtext.py` and `compare_sets.py` (every answer that moved), `moves.py` (every replayed marker that
  moved more than 5 s).
- **Truths** a frame check corrected here (`../credits-accuracy/checks.json`, both trees judged against them):
  A Beautiful Imperfection (C03/A55: epilogue 6101–6121, a dedication card 6123–6127, first name card 6129; the Plex
  comparison's approximate truth was the chapter on the epilogue, so Plex's own 6102 is now counted early too),
  10 Things I Hate About You (S04/P06: the owner's range 1:32:37–1:32:52; Plex's own 5557 is now right), 78/52 (A46:
  dedications from 5318, first credit card 5329), The Vampire Diaries S05E09 (S25: the show's end logo 2494, first
  credit 2501; the spec counts a show's closing logo as the credits'). Plex's own tally stays 8 of 80 wrong, 8 skipping
  story.
- **Ablation**: each rule measured alone on the same decodes (`local/abl_*`).

## Why each was wrong

- **10 Things I Hate About You** (chapter 1:32:09 on the final kiss). sflix read it: GPU Worker 4 (Intel UHD 770)
  13:10:44–13:10:56, stored credit text 5738.0 s, "the frames keep the credits chapter at 5529232 ms" (storage's P5000
  reads the same). The roll is a yellow crawl over the rooftop band from 5573; at 320×180 text detection boxes it on
  only some keyframes (0 boxes on most of 5653–5733 with the crawl on screen), so rule J's runs are 5605–5649,
  5681–5709 and 5737–5845, split by 28–32 s gaps of boxless keyframes that `same_roll` won't bridge, and the last run
  is 166 s late. `moves_chapter`'s "on the story" shape needed every keyframe from the chapter to rule J's start to be
  lit and textless; the crawl's own text between them kept the chapter. `CHAPTER_TEXT_GAP_S` and the inside-the-roll
  checks were never reached (the start is after the chapter).
- **A Beautiful Imperfection** (chapter 1:41:40.9 on the epilogue text). sflix read it: GPU Worker 3 (TITAN RTX),
  credit text 6101.0 s, "the frames keep". Rule J's start *is* the epilogue: four sentence lines on black, 6102–6120,
  then a dedication 320×180 boxes nothing on, then name cards on black; the dark bridge joins them into one run, the
  anchor has adjacent credit frames to start on, and the start is within 10 s of the chapter (`CHAPTER_AGREES_S`), so
  nothing moves. Plex's own start (6102) is on the same text.
- **Late rule J starts** (the Plex set's other 10): three shapes.
  - *The anchor stepped over the roll's first card on black* (CIA S01E02 +6.4, Doc S02E14 +12, 5 to 7 +8.5, 3 Days to
    Kill +10, Ace Ventura +8.2): a card, then black or cards too small to box, then the next boxed card further than
    1.5 × the run's spacing. The anchor is for one story-text frame the 24 s join glued on over story.
  - *The 1 fps walk ran into its window's floor* (3 Women +12.2): decode order put a keyframe 21 s into the run first,
    and the window reaches 20 s back.
  - *The detector sees no roll to start from*: names over bright footage or a collage (17 Again +82, '71 +119,
    21 Jump Street +103, 14 Peaks +52 with its chapter), small cards 320×180 boxes nothing on (#SKYKING +24), and a
    late chapter the inside-the-roll check keeps (A Trip to Infinity: credit text right, lit keyframes without boxes
    between its cards). No box- or cadence-level rule reaches these without reading story text as a roll.

## Fixes (rule J version 7)

Three rules, no new constant; each one's own moves were measured with the other two in place (`local/abl_*`):

- **A chapter on the story moves to the first text after it** (`rule_j.chapter_moves_to`). The "on the story" shape
  now looks from the chapter to the first keyframe with text, or to rule J's start when none comes first: all lit and
  textless, and that text more than 10 s after the chapter. The start is never before the chapter and never after rule
  J's; the answer keeps rule J's start and its label names the chapter's new one (`chapter_hint(..., to_ms=)`), so an
  online source agreeing with rule J still outvotes the chapter from rule J's start. Moves 10 Things only (−43 s →
  +1.4 s against the first crawl line); nothing else in the replay.
- **The anchor keeps a card on black followed by nothing brighter** (`rule_j._anchored`): a dark first credit frame
  whose next credit frame (in decode order) follows at least one keyframe, every one no brighter than the card's own
  frame, is kept at any distance, as version 2 kept one joined over more than 24 s. Its own moves: CIA S01E02 +6.4 →
  −0.6, Doc S02E14 +12 → 0, 5 to 7 +8.5 → +0.5 (the card between was emitted late), the 205's Where Are You Christmas,
  Entrapment, Louis C.K. Ridiculous and A Little Prayer onto credit cards, CIA S01E05 onto its first card (5 s), and
  six Vampire Diaries episodes 4–7 s earlier (onto the show's end logo, frame-checked on S05E09 and S05E17). Two shapes made it narrower than "only dark keyframes between", each one a story skip in a measured
  run: an epilogue's photo card on black (Facing El Chapo, 17 s further onto its epilogue, then published at Medium and
  High because Plex's own marker agreed), hence "no brighter than the card"; and the 640×360 reading, whose rows leave
  out the text 320×180 boxed so the rest of an epilogue card reads as blank dark keyframes (Accused S05E01, 27.5 s
  early), hence off there (`black_reads`).
- **A walk that reaches its window's floor reads one join further back** (`rule_j.refine_reaches_floor`, the detector):
  the 1 fps window is 20 s; rule J's coarse start can sit up to one 24 s join after the roll's first frame (the anchor's
  step, or decode order putting a later keyframe first: 3 Women, 21 s). Once, 24 s: bounded, so text that never stops
  (subtitles on a dark scene) can't carry the walk far. Its own moves: 3 Women +12.2 → +4.2, and on the 205 Hellraiser,
  Croupier, Hard to Kill, Scream, Panic Room, Forrest Gump into 5 s, several others 15–24 s nearer, Animal onto its
  crawl's first lines, Marvel's Daredevil S03E09 onto its first card. 3 Days to Kill and Ace Ventura are fixed by
  either rule. It asks only when the walk over the roll's own frames ends less than one of its 2.5 s steps after the
  window's first row: a roll frame before that row would have carried the walk on (a black or lit sample on the floor
  between two cards included). A fade over black down to the floor, or a walk from a lone credit frame, reads nothing
  more. Both came from the architecture review; neither moved an answer on any set.

## Results

Credits wrong = start more than 5 s from the first card; skips story = more than 5 s early.

| Set | `dev` wrong | Work wrong | `dev` skips story | Work skips story |
|---|---|---|---|---|
| Verdict, tuning (61) | 6 | 4 | 1 | 0 |
| Verdict, held out (27) | 4 | 4 | 1 | 1 |
| Verdict, all (88) | 10 (11.4 %) | 8 (9.1 %) | 2 | 1 |
| Plex comparison (81) | 12 (14.8 %) | 7 (8.6 %) | 2 | 1 |
| Plex's own (80) | 8 (10.0 %) | | 8 | |

Intros: 4 of 51 wrong before and after; no intro row changes in the replay. Replay of sflix's copy: 10 credits rows
change, 7 by more than 5 s (10 Things, 3 Days to Kill, 3 Women, 5 to 7, Doc S02E14, The Vampire Diaries S05E09 onto
the end logo, CIA S01E02), every one right by the audit's truth or the frame checks above.

Still wrong on the Plex comparison: A Beautiful Imperfection (epilogue, below), #SKYKING +24 (cards 320×180 boxes
nothing on), '71 +119, 17 Again +82, 21 Jump Street +103, 14 Peaks +42 (names over bright footage, a collage, a
montage: 0–2 boxes a keyframe, story-like to rule J), A Trip to Infinity +211 (a late chapter; credit text right, lit
keyframes without boxes between its cards keep it).

Harness (`tools.markers_eval credits-text --decode gpu --sets 80,205,accused,isurvived --online`, `run_sets.sh`), `dev`
→ work on the same decodes:

| | `dev` | Work |
|---|---|---|
| Rule J alone on the 80 (78 on disk): within 10 s / early / late / none | 66 / 2 / 5 / 0 | 66 / 2 / 5 / 0 (67 with Daredevil frame-checked) |
| The 80's gate | 5 of 5 | 4 of 5 by the chapter truth, 5 of 5 frame-checked |
| The 205, Medium useful / wrong | 101 / 18 | 106 / 18 |
| The 205, High useful / wrong | 97 / 17 | 102 / 17 |
| Accused (57), credit text useful / wrong / late | 50 / 4 / 3 | 50 / 4 / 3 |
| I Survived (16) | 14 / 1 / 0, 1 none | the same |
| Online 43 cases (TheIntroDB on): credits useful / wrong; intros | 35 / 5; 11 / 1 | the same |

Daredevil S03E09 is the 80's one change: black after the last shot, "Co-Executive Producer" at 51:50, the "Credits"
chapter 13.5 s later on the cast list; work answers the card (`local/evidence_overlay`, the harness's evidence with that
one frame check added to `credits/adjudicated.json`). The 205's answers that moved earlier than the set's chapter truth
were frame-checked and are on credits: Louis C.K. Ridiculous ("Executive Producers" over the stage), A Little Prayer
("Unit Production Manager" on black), Animal (the crawl's first lines over the last scene).

## Epilogue cards (A Beautiful Imperfection, Accused)

A Beautiful Imperfection and Accused's four early episodes (S01E07 −14.5, S03E03 −40.5, S07E05 −14.5, S07E07 −11.5,
plus S02E04, S03E09 and S03E10 5.5–8.5 s early) start on sentence cards on black before the roll. The detection model
gives boxes, not words, so the signal would have to be a box's shape. `first_cards.py` / `first_cards_summary.py` read
the card every answer on a card on black starts on, over all sets (`dev`): 41 early, 192 right.

| Candidate signal | Early caught | Right ones it would move |
|---|---|---|
| Widest line ≥ 130 px of 320 | 22 | 28 |
| Same line on screen ≥ 8 s | 10 | 24 |
| Both ≥ 119 px and ≥ 4 s | 12 | 4 |
| Both ≥ 130 px and ≥ 6 s | 5 | 3 |

The pair that separates best is two new thresholds fitted to these files, misses Accused S03E03 (its cards stay 2 s at
a keyframe), and moves four right answers (The Lord of the Rings: The Fellowship of the Ring, Song Sung Blue, Nate
Bargatze Hello World, To Dye For) off their first card. Stargate Atlantis' first cards are four lines 183 px wide;
5 to 7's names run 226 px. Nothing kept: Plex's own start is on the same epilogue text in A Beautiful Imperfection.

## Lab regression

`lab_run.sh` on this branch's image (`11c7e8c`): `phase4_row13_reset.py` with the old app, a fresh app on a new empty
config volume (`mlab_app_config_credrem`; `mlab_app_config` is other lanes' and stays), `./phase2_matrix.py configure`,
then `lab_rows.sh` (phase4_row13_run.sh's rows and order, stopping at the first row worse than #320's run). Logs in
`local/lab_run.log` and `local/lab_rows.log`.

- Phase 1 (inside phase 2 row 19): **16 of 16 pass**.
- Phase 2: 16 of 24 pass. Phase 3: 3 of 12 pass (1, 4, 7).
- Every failure but one is on #320's list and fails the same check (`../credits-accuracy/README.md` "Lab regression":
  the `publish_when` rows, row 6's G3, the `-threads 2`/`scale_cuda` rows, phase 3 row 5, phase 3 row 11).
- **Phase 3 row 6** (cancel mid-decode; passed in #320's run) failed "the decode stopped within 10 s", the run stopped
  there, and the row was traced on a fresh app per trial with the decodes sampled every 0.5 s (`ps` in the app) and the
  app's log. The cancel lands as the worker picks the file up; when the item misses it, the whole file is read and
  stored as "unchanged", on both images: `dev` `cab4ccf` missed it in 4 of 6 traced trials, this branch in 5 of 6, with
  the same decode timeline (the reference decode check, the keyframe pass, the 1 fps windows, the 640×360 pass). The
  row passes only when its 1 s polls land in a 2–3 s gap between two decodes about 10 s after the cancel. Nothing on
  this branch touches the cancel path; why the item misses a cancel made at pickup is not traced (a separate issue).
  The run resumed at phase 3 row 7.

## Cost

A file whose 1 fps walk reaches its window's floor decodes one more 24 s window at 1 fps (9 of 428 answers on the
verdict, Plex and chapter files, 16 of 308 on the harness sets; about 1–2 s each on storage's P5000). The anchor and
the chapter move cost nothing. `CREDITS_TEXT_VERSION` 7 reads every stored credit text answer again once: 912 files on
sflix's disks, about 7 s each on its GPUs.
