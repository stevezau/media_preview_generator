# Phase 3 accuracy harness results: on-screen credit text against Plex

Harness: `python -m tools.markers_eval credits-text` (see `tools/markers_eval/README.md`). Every row runs the app's own
`markers/credits/detector.find_credits` (rule J) and the app's own `decide()`. Details with file paths stay local
(`phase3_credits_gpu.json`, `phase3_credits_cpu.json`, git-ignored); this file holds counts and folder names only.

- Date: 2026-09-18, on `storage`.
- Code: `feat/markers-detection` at `d60ae65` plus this task's harness.
- Host: 20 CPU threads, one Quadro P5000 (driver 580.178.04), `ffmpeg version 8.0.1-3ubuntu2` (`/usr/bin/ffmpeg`),
  Python 3.14.4. Text detection ran on the GPU through the helper pool: its self-test measured 11.2–11.4 ms per frame
  on WebGPU against 17.4–18.8 ms on the CPU and pinned the helper to the card's PCI address.
- Sets: the 80 hand-checked credits files (40 movies + 40 episodes), the 205-movie set, and the 43 verified online
  cases. The 40 movies of the 80 are all in the 205, so 245 distinct files carry 285 set rows, plus the online cases'
  43 episodes. The truth is each file's last credits chapter, with 3 movies corrected by frame checks
  (`credits/adjudicated.json`). Chapters are therefore left out of every row: these files stand for files without
  usable chapters.

> **Superseded in part (2026-09-19): rule J version 2**, the last section of this file, moves the numbers below
> (rule J alone on the 80: GPU 64 / 1 / 7 / 3, CPU 59 / 1 / 8 / 8, which now meets §5.4 too; the 205's Medium useful
> 90 → 96 with no wrong answer added). The gate's outcome per set is unchanged. Everything above that section is
> version 1 as measured on 2026-09-18.

**Result: the 80 pass the gate on both decode paths; the 205 fail three of its five checks.** Rule J alone clears
spec §5.4 on the GPU decode (63 within 10 s, 1 early) and misses it by one file on the CPU decode (58 within 10 s),
which is a difference between the two paths' scalers, not between two runs of the same one. Nothing was tuned in
response — the numbers below are as measured, for the owner's ruling (Q4, Q5).

Two things the 205's failure is, in one place, because they point in different directions:

- **The usefulness floor (Medium 90 < Plex 124) fails on a detector gap.** Credits text answers *late* on the files
  where it and Plex disagree — later than Plex on 71 of those 78 files, median 80.7 s — because rule J takes the last
  credit run of a roll. Even publishing credits text on every one of those disagreements would reach 111, still short
  of 124. Fixing that was out of scope here and was not attempted.
- **The two wrong caps fail partly on the set's truth.** All 17 wrong answers were frame-checked: 12 are right and
  the file's last credits chapter is late by 13–370 s. After adjudication Medium's cap would clear (5 ≤ 5) and
  High's would still fail (4 > 3).

## Text detection bench (Task 4)

The vendored detector must find the same boxes as `rapidocr_onnxruntime` 1.4.4 on the 289-frame bench. Both runs, on
Python 3.12 and on 3.14:

```
289 of 289 frames identical; differing frames: []
289 of 289 frames with identical box corners; differing frames: []
```

Both exit 0. 289 frames, 99 with text, 453 boxes.

## Rule J alone on the 80 (spec §5.4)

The metric is spec §5.4's: how many answers land within 5 / 10 / 30 s of the truth, how many are more than 30 s early
or late, and how many files give no answer.

| Decode | files | within 5 s | within 10 s | within 30 s | early >30 s | late >30 s | none | meets spec |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Spec §5.4 bar | 80 | | ≥ 59 | | ≤ 1 | | | |
| Fixture (`credits_rule_j_80.json.gz`, 20 s refine) | 80 | | 63 | | 1 | 8 | 4 | |
| **GPU decode (reported run)** | 80 | 55 | **63** | 68 | **1** | 8 | **3** | pass |
| CPU decode | 80 | 49 | **58** | 62 | **1** | 9 | **8** | **fail (58 < 59)** |

The app's own run reproduces the fixture's 63 / 1 / 8 exactly and finds one more answer than the fixture's rows do
(3 files with no answer, not 4). The fixture's rows were decoded around the truth while the app decodes the tail and
the refine window itself, so a file near the tail's edge can gain an answer; nothing moved the other way.

Per HDR kind (Task 1 M5), GPU decode:

| HDR kind | files | within 5 s | within 10 s | within 30 s | early >30 s | late >30 s | none |
|---|---:|---:|---:|---:|---:|---:|---:|
| sdr | 58 | 45 | 50 | 52 | 1 | 2 | 3 |
| hdr10 (PQ or HLG) | 8 | 4 | 6 | 7 | 0 | 1 | 0 |
| dv_other (Dolby Vision, not profile 5) | 14 | 6 | 7 | 9 | 0 | 5 | 0 |
| dv5 | 0 | | | | | | |

The 80 hold no profile-5 file. Dolby Vision files are the weakest group (7 of 14 within 10 s, 5 late), which matches
what the late answers are: see the frame checks below.

## Rows per set (GPU decode)

`wrong` is a start more than 10 s early (it skips story); `late` is more than 30 s late; `missed` is no published
answer. High and Medium are the app's own `decide()` on credits text plus Plex's markers — no chapters, and Plex's
markers can never decide alone (rule 7).

### movies40 (40 files)

| Row | useful | late | wrong | missed |
| --- | ---: | ---: | ---: | ---: |
| Plex's first credits marker | 28 | 1 | 9 | 2 |
| Credits text alone | 29 | 8 | 1 | 2 |
| Pipeline, High | 21 | 0 | 0 | 19 |
| Pipeline, Medium | 22 | 0 | 0 | 18 |

`text_and_server_only` 21, `ends_found` 2, `ends_published` High 2 / Medium 2.

### tv40 (40 files)

| Row | useful | late | wrong | missed |
| --- | ---: | ---: | ---: | ---: |
| Plex's first credits marker | 19 | 0 | 4 | 17 |
| Credits text alone | 37 | 0 | 2 | 1 |
| Pipeline, High | 19 | 0 | 1 | 20 |
| Pipeline, Medium | 35 | 0 | 1 | 4 |

`text_and_server_only` 20, `ends_found` 1, `ends_published` Medium 1.

Episodes are where credit text earns its place: Plex has no credits marker at all on 17 of the 40, and credit text
answers 39 of them. At Medium a lone credits-text answer publishes (rule 6, owner's Q1), which is why Medium reaches
35 useful against High's 19.

### movie_credit_truth (205 files)

| Row | useful | late | wrong | missed |
| --- | ---: | ---: | ---: | ---: |
| Plex's first credits marker | 124 | 11 | 62 | 8 |
| Credits text alone | 111 | 46 | 37 | 11 |
| Pipeline, High | 89 | 9 | 15 | 92 |
| Pipeline, Medium | 90 | 9 | 17 | 89 |

`text_and_server_only` 113, `ends_found` 16, `ends_published` High 6 / Medium 7.

## The gate (owner's Q4, 2026-09-16: precision first, never looser than Plex)

### The 80 (movies40 + tv40 merged) — **pass, 5 of 5**

| Row | useful | late | wrong | missed |
| --- | ---: | ---: | ---: | ---: |
| Plex's first credits marker | 47 | 1 | 13 | 19 |
| Credits text alone | 66 | 8 | 3 | 3 |
| Pipeline, High | 40 | 0 | 1 | 39 |
| Pipeline, Medium | 57 | 0 | 1 | 22 |

| Check | Numbers | Result |
| --- | --- | --- |
| Medium useful ≥ Plex useful | 57 ≥ 47 | pass |
| Medium wrong ≤ 2 (2 % of 80) | 1 ≤ 2 | pass |
| Medium wrong ≤ Plex wrong | 1 ≤ 13 | pass |
| High wrong ≤ 1 (1 % of 80) | 1 ≤ 1 | pass |
| High wrong ≤ Plex wrong | 1 ≤ 13 | pass |

`text_and_server_only` 41, `ends_found` 3, `ends_published` High 2 / Medium 3.

High's one wrong answer is at the cap of 1, exactly as the plan expected: one episode of a 1987 sitcom whose credits
start on a "created by" card that both credits text and Plex place about 20 s before the chapter (frame-checked
below).

### The 205 movies — **fail, 3 of 5**

| Check | Numbers | Result |
| --- | --- | --- |
| Medium useful ≥ Plex useful | 90 < 124 | **fail** |
| Medium wrong ≤ 5 (2 % of 205) | 17 > 5 | **fail** |
| Medium wrong ≤ Plex wrong | 17 ≤ 62 | pass |
| High wrong ≤ 3 (1 % of 205) | 15 > 3 | **fail** |
| High wrong ≤ Plex wrong | 15 ≤ 62 | pass |

Both "never looser than Plex" checks pass by a wide margin — the pipeline publishes a quarter of Plex's wrong answers.
The two absolute caps and the usefulness floor fail. What is behind each:

**Why Medium useful (90) is far below Plex's useful (124): credits text answers late, and the disagreement is its
fault.** Credits text and Plex's marker disagree by more than 10 s on 90 of the 205 files, and a disagreement is not
published at either level: 92 files are undecided at High and 89 at Medium (78 of them are exactly this disagreement,
11 have no credits-text answer, and 3 have no Plex marker, so rule 7 leaves credits text alone at High). It would be
comfortable to call that precision-first working as written. The harness's own rows say otherwise. On those 78 files:

| On the 78 files whose disagreement blocks publication | useful | late | wrong |
| --- | ---: | ---: | ---: |
| Credits text | 21 | 37 | 20 |
| Plex's first credits marker | 34 | 6 | 38 |

Credits text is **later than Plex on 71 of the 78, by a median of 80.7 s**, and its own median distance from the
chapter is +21.4 s. So the source that is stopping publication here is mostly ours, and it is stopping it by being
late, not by being early. The ceiling makes the same point: publishing credits text unconditionally on every one of
those 78 disagreements — throwing the precision rule away entirely — would reach **111 useful against Plex's 124**,
still short of the floor.

The cause is rule J's own last-run selection, described under the late answers below: `coarse_start` takes the
**last** credit run, so a roll whose opening section is names over bright footage, or which is split by a gap longer
than 24 s, is answered from a later block. That is a detector gap, not a defect in the truth — the truth-reliability
finding below applies to the two "wrong" caps, not to this check. **Fixing it is out of scope for this task and was
not attempted**; it is the one measured reason this check cannot pass today.

**What the 15 High wrong answers are.** Every one was frame-checked (table below). On **15 of the 17** wrong answers
(High plus Medium's two extra) Plex's own marker is within 8 s of ours, so Plex is counted wrong on the same files.
Of the 17:

- **12 are right and the chapter truth is late** by 13 to 370 s: the answer sits on the roll's first credit card or
  on names over footage, and the file's last credits chapter marks a later block (typically the black roll after the
  names-over-footage section). This is the same defect the three `adjudicated.json` movies were corrected for; the
  205-movie set's truth is its unadjudicated chapters.
- **3 are epilogue text cards** taken for the roll (14 to 50 s early) — the shape spec §5.4 warns about.
- **2 are genuinely early**: one stops on the closing applause of a stand-up special 32 s before the title card, one
  lands on dark story footage 71 s before the chapter (both sources place it there).

So 5 of the 17 are answers a frame check calls wrong, against a cap of 3 at High and 5 at Medium. That does **not**
make both caps reachable. Four of the five — Gandhari, Trainwreck The Astroworld Tragedy, Breach and Lover Stalker
Killer — are published at High; the fifth, Gaurav Gupta Market Down Hai, is undecided at High because Plex has no
marker for it. **After adjudication, Medium's wrong cap would clear (5 ≤ 5); High's would not (4 > 3).**

What the adjudication does change is the meaning of the other 12: the measurement the caps were written for — "how
often does the app skip story" — is not what the unadjudicated 205-movie chapter truth measures.

**How reliable the 205's truth is.** The median distance from the chapter truth is 9.0 s for credits text and 4.3 s
for Plex's marker. On 15 files both sources agree with each other within 10 s and both are more than 10 s early
against the chapter; on 46 files credits text is more than 30 s later than the chapter (and Plex is too on 8 of them).

## Ends (the owner's Q3)

**These numbers predate a milestone-audit fix to `rule_j.coarse_end_s`** (it was reading the run's last emitted row
instead of its latest credit keyframe -- the same decode-order-for-presentation-order mistake already fixed once for
the start). The fix can only move a reported end later or make it disappear, never earlier, so this section's counts
and lines are a safe lower bound, not the current numbers; a fresh run (the local harness cache was cleared) would
move a small number of rows. The start numbers above are unaffected -- confirmed no caller passes anything but the
default `RuleParams` to the start path, and the anchor fix's own regression test is untouched by this one.

19 set rows — 17 distinct files (3 of the 80, 16 of the 205, two of them shared) — gave credits text an end: more than
30 s of the file follows the roll's last credit keyframe, so the skip stops there instead of running to the end of the
file.

A skip that stops before the end of the file is published for 6 files at High and 8 at Medium. One of those (a
stand-up special) takes its end from Plex's own non-final credits marker, not from credits text — `ends_published`
counts "the skip stops early", whatever supplied the end.

Frame-checked, the seven published credits-text ends are:

| File | end − duration | Adjudication |
| --- | ---: | --- |
| Avengers Infinity War (2018) | −111 s | scene kept (the post-credits scene is preserved) |
| Mushoku Tensei Jobless Reincarnation (2021) Season 01 | −59 s | scene kept (the post-ED scene is preserved) |
| Late Bloomers (2024) | −46 s | logo or black, not a scene |
| Demon Slayer Kimetsu no Yaiba Infinity Castle (2025) | −46 s | logo or black, not a scene |
| OMG 2 (2023) | −49 s | logo or black, not a scene (studio logos) |
| Dhoom Dhaam (2025) | −39 s | more credits, not a scene (dubbing cards) |
| Retro (2025) | −155 s | the roll is still running at the end; the skip stops 155 s early |

No end swallowed a scene. Five of the seven stop a little before the file ends where only logos, more credit cards or
black follow, which costs the viewer nothing; Retro's stops inside a roll whose later keyframes no longer count as
credit frames.

## Online cases (43 verified cases, all 43 files found)

Credit text is added the way the pipeline adds it: only to cases whose credits the online answers and Plex's markers
leave undecided (`credits_text_asked`).

| Setting | credits: useful / late / wrong / missed without credit text | with credit text | asked |
| --- | --- | --- | ---: |
| Default sources, High | 8 / 2 / 4 / 29 | **27 / 3 / 5 / 8** | 29 |
| TheIntroDB on, High | 14 / 1 / 4 / 24 | **33 / 3 / 5 / 2** | 24 |
| TheIntroDB on, Medium | 14 / 1 / 4 / 24 | **33 / 3 / 5 / 2** | 24 |

Credit text answers almost every case the other sources leave open: 21 more useful credits at the default setting and
19 more with TheIntroDB on, for one more wrong and one more late answer. Intros are unchanged (credit text decides
credits only): 10 useful / 3 wrong / 30 missed at the default setting, 11 / 1 / 31 with TheIntroDB on.

## Frame checks (the owner's Q5, preflight I7)

The harness wrote a 4×2 contact sheet of the 80 s around every answer worth a look: more than 10 s early, more than
30 s late, shaped like epilogue cards, or with an end (which gets a second sheet around the end). 185 of the 285 set
rows qualified (a movie in both movies40 and the 205 counts in each). A row can carry several reasons, so these
counts overlap:

| Reason | rows |
| --- | ---: |
| epilogue-like and nothing else | 85 |
| late >30 s | 54 |
| early >30 s | 31 |
| early 10–30 s | 9 |
| end kept | 19 |

`epilogue_like` is a deliberately wide trigger, and on these sets it is very wide: 73 of the 85 files it flags on its
own have an answer within 10 s of the truth. It is a "look at this" flag, not a verdict, so this costs precision
nothing — but a reviewer reading the list should know that most of those 85 rows are correct answers on rolls whose
first keyframes carry sparse text.

Names below are folder names: a movie's own folder, or an episode's show and season folder. The 40 episodes of tv40
come from 21 season folders, so a TV row's name is shared by up to five files of the set; the per-episode rows are in
the local JSON.

### Every wrong answer, frame-checked

The 17 files whose published High or Medium answer is more than 10 s early (all from the 205-movie set; the 80's
single wrong answer is the last row).

| File | answer − truth | Plex − truth | Rests on | Adjudication |
| --- | ---: | ---: | --- | --- |
| Late Bloomers (2024) | −370 s | −374 s | text + Plex | correct (credits on black; the chapter is late) |
| See How They Run (2022) | −306 s | −312 s | text + Plex | correct (credit cards; the chapter is late) |
| Paradise (2024) | −235 s | −234 s | text + Plex | correct (credits over a held frame; the chapter is late) |
| Ari Shaffir Americas Sweetheart (2025) | −184 s | −186 s | text + Plex | correct (full-screen credit cards; the chapter is late) |
| Retro (2025) | −173 s | no marker | text alone (Medium) | correct (the "a … padam" card starts the roll) |
| I Dont Feel at Home in This World Anymore (2017) | −165 s | −166 s | text + Plex | correct (credits on black; the chapter is late) |
| Crew (2024) | −158 s | −164 s | text + Plex | correct (names over footage) |
| Hendrie (2024) | −143 s | −142 s | text + Plex | correct (credits on black) |
| Boss Level (2021) | −133 s | −132 s | text + Plex | correct (stylised credit cards) |
| Gandhari (2026) | −71 s | −72 s | text + Plex | really early (dark story footage; both sources agree there) |
| Bill Burr Live at Red Rocks (2022) | −70 s | −65 s | text + Plex | correct (names over behind-the-scenes footage) |
| Trainwreck The Astroworld Tragedy (2025) | −50 s | −49 s | text + Plex | epilogue cards |
| Breach (2007) | −41 s | −40 s | text + Plex | epilogue cards |
| Gaurav Gupta Market Down Hai (2021) | −32 s | no marker | text alone (Medium) | really early (closing applause, 30 s before the roll) |
| Rachel Feinstein Big Guy (2024) | −16 s | −24 s | text + Plex | correct (the "written and performed by" card) |
| Lover Stalker Killer (2024) | −14 s | −13 s | text + Plex | epilogue cards (the roll follows immediately) |
| Famous Last Words Eric Dane (2026) | −13 s | −10 s | text + Plex | correct (the memorial card opens the roll) |
| Married. with Children (1987) Season 06 *(the 80)* | −21 s | −30 s | text + Plex | correct (the "created by" card; the chapter is late) |

### The 80's other answers worth a look

| File | answer − truth | Published | Adjudication |
| --- | ---: | --- | --- |
| Come from Away (2021) | +242 s | undecided | late, inside the roll (the polaroid-board credits) |
| Midas Man (2024) | +235 s | undecided | late, inside the roll |
| The Lord of the Rings The Return of the King (2003) | +175 s | undecided | late, inside the roll (the cast portraits) |
| No Other Choice (2025) | +146 s | undecided | late, inside the roll |
| Taxi Driver (1976) | +135 s | undecided | late, inside the roll |
| Revenge of the Nerds (1984) | +111 s | undecided | late, inside the roll |
| The Mummy (1999) | +82 s | undecided | late, inside the roll |
| WILL (2023) | +68 s | undecided | late, inside the roll |
| Undisputed (2024) | −34 s | undecided | really early (a lone text frame on story, 30 s before the roll) |
| Outlander (2014) Season 08 | −16 s | undecided | really early (the fade to black before the first card) |
| Animal (2023) | no answer | undecided | no credit run found in the tail |
| A Season to Remember (2024) | no answer | undecided | no credit run found in the tail (Plex has no marker either) |
| Revenant (2023) Season 01 | no answer | undecided | no credit run found in the tail (Plex has no marker either) |

On the 80, every late answer was left undecided because Plex disagreed with it, so none reached a published marker.

### The late answers on the 205

46 distinct files answer more than 30 s late (54 sheet rows: the 8 movies above are in both sets). 15 of the 46 were
frame-checked — the 8 in the 80, plus the four largest deltas and three at the low end of the range:
**all 15 land inside the credit roll, never on story.**

| File | answer − truth | Adjudication |
| --- | ---: | --- |
| Poolhall Junkies (2002) | +571 s | late, inside the roll (the cast block) |
| Superman (2025) | +471 s | late, inside the roll (the crew columns) |
| Kill Bill Vol. 2 (2004) | +365 s | late, inside the roll (the cast cards) |
| Black Panther (2018) | +281 s | late, inside the roll (the "CAST" block) |
| Saint Frances (2020) | +33 s | late, inside the roll |
| Entrapment (1999) | +31 s | late, inside the roll (names over footage started earlier) |
| The Aviator (2004) | +31 s | late, inside the roll |

The mechanism is rule J's own: `credit_runs` joins credit frames across gaps of at most 24 s and `coarse_start` takes
the **last** run, so a roll whose opening section is names over bright footage, or which is split by a longer gap,
starts at the later run. A late answer costs the viewer part of a skip; it never skips story. Dolby Vision files are
over-represented here (5 of 14), which fits: their tone-mapped keyframes are brighter, so the roll's opening frames
need 3+ boxes to count.

## Decode paths

`--decode gpu` is the reported run: NVIDIA decode through the app's own hwaccel arguments and text detection on the
GPU helper. `--decode cpu` reruns the 80 files with no GPU at all, to prove a CPU worker gives the same gate.

**The CPU run passes the Q4 gate on the 80 and misses spec §5.4's own bar for rule J alone.** Its gate is 5 of 5:

| Row | useful | late | wrong | missed |
| --- | ---: | ---: | ---: | ---: |
| Plex's first credits marker | 47 | 1 | 13 | 19 |
| Credits text alone | 61 | 9 | 2 | 8 |
| Pipeline, High | 40 | 0 | 1 | 39 |
| Pipeline, Medium | 54 | 0 | 1 | 25 |

| Check | Numbers | Result |
| --- | --- | --- |
| Medium useful ≥ Plex useful | 54 ≥ 47 | pass |
| Medium wrong ≤ 2 (2 % of 80) | 1 ≤ 2 | pass |
| Medium wrong ≤ Plex wrong | 1 ≤ 13 | pass |
| High wrong ≤ 1 (1 % of 80) | 1 ≤ 1 | pass |
| High wrong ≤ Plex wrong | 1 ≤ 13 | pass |

High's wrong answer is the same 1987 sitcom episode as on the GPU. But rule J alone reaches only 58 within 10 s
against the spec's 59, so the CPU run exits 1: **the two decode paths do not give the same answers.** 21 of the 80
files answer differently, 5 of them losing their answer altogether:

| What changed | files | Examples (answer − truth, GPU → CPU) |
| --- | ---: | --- |
| No answer on the CPU | 5 | Come from Away +242 → none; Nai Nai and Wai Po −3 → none; one episode each of Fresh Off the Boat, Iyanu and Revenant |
| Much later on the CPU | 3 | Dhoom Dhaam −3 → +252; Fighter +0 → +187; The Lord of the Rings The Return of the King +175 → +503 |
| Closer to the truth on the CPU | 3 | Outlander −16 → −0.4; WILL +68 → +41; Sexy Beast −2.4 → −0.4 |
| A few seconds apart, same band | 10 | Checkin It Twice +0.1 → +4.1; an Iyanu episode +0 → +9 |

The two paths scale the frame differently — `scale_cuda=320:180` on the GPU against swscale's `scale=320:180` on the
CPU — and the 320×180 luma is what text detection counts boxes on, so a frame near rule J's thresholds can fall on
either side. The candidate match for `scale_cuda` is swscale's bilinear filter; the last section of this document
records that measurement.

The two GPU-run files that fell back to a CPU decode (below) give the same answer in both runs, as they must.

**GPU decode fell back to the CPU on 2 of the 245 files** — one AV1 4K release and one HDR10+ HEVC remux, both of
which this card cannot decode. That is exactly what the app does with them (the detector raises
`CodecNotSupportedError` and the GPU worker reruns the item with no GPU), and the harness mirrors it so the file stays
in the set; both files' answers come from a CPU decode in the GPU run too.

## Run times

- GPU run (245 set files + 43 online cases = 288 distinct files, 19 of them already cached): 71 minutes, `nice -n 19`,
  one job at a time — about 15 s per file, including the 204 contact sheets it wrote.
- CPU run (the 80 files): 34 minutes, about 26 s per file.
- Answers are cached per file identity, detector version, decode path and kind, so a re-run of either costs seconds.
  The cache key does **not** include the scaler, so the bilinear measurement below was run against its own cache.

## Bilinear scaler measurement (CPU decode)

Because the CPU run misses spec §5.4's bar while the GPU run clears it, the plan's candidate match for `scale_cuda` —
swscale's bilinear filter — was measured on the same 80 files. It was measured by patching `frames._scale_filter`
inside one scratch process against its own cache; **the product filter was not changed**, and on these numbers it
should not be:

| CPU scaler | within 5 s | within 10 s | within 30 s | early >30 s | late >30 s | none | answers matching the GPU's |
|---|---:|---:|---:|---:|---:|---:|---:|
| `scale=320:180` (shipped, swscale default) | 49 | 58 | 62 | 1 | 9 | 8 | 59 of 80 |
| `scale=320:180:flags=bilinear` | 43 | 56 | 63 | 0 | 10 | 7 | 49 of 80 |

Bilinear is a **worse** match for `scale_cuda`, not a better one: it agrees with the GPU run on ten fewer files and
answers two fewer within 10 s. Its Q4 gate on the 80 still passes 5 of 5 (Plex 47 / 1 / 13 / 19; credits text alone
61 / 10 / 2 / 7; High 39 useful, 0 wrong, 41 missed; Medium 54 useful, 0 wrong, 26 missed), but nothing here argues
for changing the shipped filter. What the two rows do show is that rule J's answer is sensitive to how the frame is
scaled, which is worth a line in the spec: the §5.4 numbers belong to a decode path, not to the rule alone.

## Rule J version 2 (2026-09-19): the anchor's 24 s limit and scene text glued onto the end

The owner asked for Intro & Credits to be finished, the credit-text gaps included (spec §13 items 13 and 14). Rule J
version 2 (`CREDITS_TEXT_VERSION` 2) ships two changes, each the anchor's own purpose stated precisely. Everything
else tried is in the last table of this section, with why it wasn't taken.

1. **The start's anchor never steps over a gap longer than the 24 s join.** The anchor is there to step over one scene
   text frame the 24 s join glued onto the roll. A credit frame whose next credit frame is more than 24 s away can only
   have been joined by the dark bridge ("dark frames without text never break a run"), so every frame between is
   dark and the lone frame is the roll's own first card on black. Stepping off it put the start on whatever card text
   detection saw next: WILL's roll opens on "a film by", then shows 65 s of dark keyframes whose small text reads as
   no boxes, and the step put the start 72 s late.
2. **The end steps back over scene text glued on after the roll** (§13 item 13, the start's anchor mirrored). When
   the run's last credit keyframe is lit, more than 1.5 × the run's credit spacing after the credit keyframe before
   it, and separated from it by a lit keyframe with no text, it is scene text the 24 s join glued on. The end is then
   refined from the credit keyframe before it, and the 1 fps walk stops where the scene starts. It moves an end, never
   makes one: whether an end is kept is still Q3's test on the latest credit keyframe, so a lit logo card within 30 s
   of the end of the file still leaves the skip running to the end.

- Date: 2026-09-19, on `storage`; same host, ffmpeg, driver and Python as above. Text detection on the GPU helper,
  self-test 11.46 ms per frame against 18.35 ms on the CPU.
- Code: `feat/markers-detection` at `2256bca` (version 1, the "before" rows, re-measured today) and the same plus
  this change (version 2). Chapters are left out of every row, as above.
- Tooling: the harness now keeps every decode (`credits_decodes`, `tools/markers_eval/README.md`). Version 1's run
  decoded 498 windows on the GPU and 152 on the CPU; version 2 re-ran the app's own `find_credits` and `decide()` on
  them and decoded only the 1 fps windows its new starts ask for (11 on the GPU, 3 on the CPU). `--changed-since`
  listed every answer that moved by more than 10 s and wrote its sheets.

### Before and after

Rule J alone on the 80 (spec §5.4: at least 59 within 10 s, at most 1 early by more than 30 s):

| Decode | Version | within 5 s | within 10 s | within 30 s | early >30 s | late >30 s | none | meets spec |
|---|---|---:|---:|---:|---:|---:|---:|---|
| GPU | 1 | 55 | 63 | 68 | 1 | 8 | 3 | pass |
| GPU | **2** | 55 | **64** | 69 | 1 | **7** | 3 | pass |
| CPU | 1 | 49 | 58 | 62 | 1 | 9 | 8 | fail (58 < 59) |
| CPU | **2** | 49 | **59** | 63 | 1 | **8** | 8 | **pass** |
| Fixture (`credits_rule_j_80.json.gz`) | 1 → 2 | | 63 → 64 | | 1 → 1 | 8 → 7 | 4 → 4 | |

Rows per set, useful / late / wrong / missed (`wrong` is more than 10 s early against the chapter):

| Set, decode | Row | Version 1 | Version 2 |
|---|---|---|---|
| 80, GPU | Plex's first credits marker | 47 / 1 / 13 / 19 | 47 / 1 / 13 / 19 |
| | Credits text alone | 66 / 8 / 3 / 3 | **67 / 7 / 3 / 3** |
| | Pipeline, High | 40 / 0 / 1 / 39 | **41 / 0 / 1 / 38** |
| | Pipeline, Medium | 57 / 0 / 1 / 22 | **58 / 0 / 1 / 21** |
| 205, GPU | Plex's first credits marker | 124 / 11 / 62 / 8 | 124 / 11 / 62 / 8 |
| | Credits text alone | 111 / 46 / 37 / 11 | **116 / 41 / 37 / 11** |
| | Pipeline, High | 89 / 9 / 15 / 92 | **95 / 10 / 15 / 85** |
| | Pipeline, Medium | 90 / 9 / 17 / 89 | **96 / 10 / 17 / 82** |
| 80, CPU | Plex's first credits marker | 47 / 1 / 13 / 19 | 47 / 1 / 13 / 19 |
| | Credits text alone | 61 / 9 / 2 / 8 | **62 / 8 / 2 / 8** |
| | Pipeline, High | 40 / 0 / 1 / 39 | **41 / 0 / 1 / 38** |
| | Pipeline, Medium | 54 / 0 / 1 / 25 | **55 / 0 / 1 / 24** |

`text_and_server_only`: 80 41 → 42, 205 113 → 120. Ends found by credits text: unchanged (80: 3 GPU, 1 CPU; 205:
15, one fewer than the Ends section above, which predates the `coarse_end_s` fix). Ends published: 205 High 5 → 7,
Medium 6 → 8 (both below); the 80 unchanged (GPU High 2 / Medium 3, CPU High 1 / Medium 2). The 43 online cases: every row unchanged (default High 27 / 3 / 5 / 8; TheIntroDB on, High and Medium,
33 / 3 / 5 / 2); one answer moved (below).

The Q4 gate's outcome per set is unchanged: the 80 pass 5 of 5 on both paths, and the 205 still fail 3 of 5 (Medium
useful 96 < Plex 124; Medium wrong 17 > 5; High wrong 15 > 3), passing both "never looser than Plex" checks.

### The owner's bar (Q4/Q5), check by check

| Check | Numbers (version 1 → 2) | Result |
|---|---|---|
| No more early answers at any level, per set | 80 GPU: alone 3 → 3, High 1 → 1, Medium 1 → 1. 205: alone 37 → 37, High 15 → 15, Medium 17 → 17. 80 CPU: alone 2 → 2, High 1 → 1, Medium 1 → 1. Every moved answer moved later-to-earlier and none crossed into `wrong` | pass |
| Medium useful at least today's on each set, strictly more on one | 80: 57 → 58 (CPU 54 → 55); 205: 90 → 96 | pass |
| Rule J alone on the 80 meets §5.4 on the GPU; the CPU no worse than 58 / 1 | GPU 64 / 1; CPU 59 / 1 | pass |
| No published end moves into a scene | no credits-text end moved on either set or path; two decisions newly published with an end, frame-checked below: neither is in a scene | pass |
| Every answer that moves by more than 10 s is frame-checked | 9 files on the GPU (11 set rows), 3 on the CPU (2 of them the same files), 1 online case | done, below |

### Frame checks of every moved answer

| File | Decode | answer − truth, v1 → v2 | Published v2 (High, Medium) | Adjudication |
|---|---|---:|---|---|
| WILL (2023) *(both sets)* | GPU, CPU | +67.6 → +5.6 (CPU +40.6 → +5.6) | useful, useful | correct: the screenplay card; the roll opened on "a film by" at the chapter |
| The Second Mother (2015) | GPU | +21.0 → +1.0 | useful, useful | correct: the first cast card |
| Two for the Money (2005) | GPU | +88.0 → +7.0 | useful, useful | correct: the producer card; "directed by" over footage opened the roll at the chapter |
| RocknRolla (2008) | GPU | +32.9 → +6.9 | useful, useful | correct: the end-title card that opens the roll |
| Executive Decision (1996) | GPU | +32.8 → +13.8 | useful, useful | correct: the cast crawl over the last scene's footage |
| The Last Song (2010) | GPU | +52.7 → +26.7 | useful, useful | inside the roll (names over footage) |
| Revenge of the Nerds (1984) *(both sets)* | GPU, CPU | +110.8 → +75.8 | undecided | still late, inside the roll: the crawl on black after the cast cards over footage |
| Be Natural The Untold Story of Alice Guy-Blache (2018) | GPU | +121.0 → +89.0 | undecided | still late, inside the roll: the cast crawl after an archive clip |
| Black Panther (2018) | GPU | +281.4 → +242.4 | late, late | still late, inside the roll: the crawl right after the first mid-credits scene, so the published skip starts after that scene; its end (84 s before the end of the file, unchanged) keeps the post-credits scene |
| Fighter (2024) *(the 80)* | CPU | +187.0 → +151.0 | undecided | still late, inside the roll (the crew crawl) |
| Marvels Daredevil (2015) S03E12 *(online)* | GPU | +17.4 → −14.6 | not asked (other sources decide it) | correct: the "story editor" card on black opens the roll; the chapter is late |

The two newly published ends, both on decisions version 1 left undecided:

| File | end − duration | What supplies it | Adjudication |
|---|---:|---|---|
| Black Panther (2018) | −84 s | credits text (unchanged since version 1) | scene kept: the post-credits scene follows |
| Executive Decision (1996) | −26 s | Plex's own marker end (rule 7 shortens) | logo or black, not a scene: studio logos after the crawl |

No end swallowed a scene, and no credits-text end moved on either set. The end change's measured case is outside both
sets: Rick and Morty S01E04 on the CPU decode (in the online cases, which run on the GPU), measured with the app's own
`find_credits`. Version 1 ended the skip at 1198.0 s, 11.5 s into the scene after the roll; version 2 ends it at
1186.0 s, the roll's last frame (the side-by-side's frame check has the roll at 1162.2–1186.5 s), and its start is
1163.0 s. On the GPU decode the episode still has no answer (§13 item 14's third shape, below).

### Tried and not taken

Each was screened on the stored decodes, then measured with real decodes of every window it asks for. Numbers are
version 2 plus the candidate, GPU decode; `wrong` is against the chapter.

| Candidate | 80: Medium useful / wrong | 205: Medium useful / wrong | 205: High wrong | 205: alone wrong | Why not |
|---|---|---|---|---|---|
| Version 2 (shipped) | 58 / 1 | 96 / 17 | 15 | 37 | |
| Walk back to the previous kept run ≤ 30 s away, ≥ 60 % credit frames | 58 / 1 | 97 / 17 | 15 | 37 | one file (Instant Family) for two tuned numbers; it moves two early answers further early, Rescued by Ruby (2022) from inside the roll onto its epilogue cards |
| The same at ≥ 50 % | 58 / 1 | 98 / 18 | 16 | 38 | a new published wrong answer |
| The same at ≤ 60 s | 58 / 1 | 95 / 19 | 17 | 42 | new wrong answers; rule J alone 2 early on the 80 (fails §5.4) |
| Merge runs ≤ 40 s apart when half the lit keyframes between carry text | 58 / 1 | 97 / 17 | 15 | 37 | one file (Pizza Movie) for two tuned numbers; it moves an already-early documentary 61 s further early |
| The same at ≤ 90 s | 59 / 1 | 99 / 18 | 16 | 39 | new wrong answers |
| Lit keyframes next to a run need 2 boxes, not 3 | 59 / 2 | 102 / 22 | 20 | 38 | new wrong answers on both sets; High wrong 2 > the 80's cap of 1 |
| The anchor steps only across a lit keyframe (with the 24 s limit) | 57 / 2 | 100 / 19 | 17 | 38 | new wrong answers on both sets; High wrong 2 > the 80's cap of 1 |
| A frame at luma 30–33 needs 2 boxes, not 3 (§13 item 14's third shape) | 58 / 1 | 96 / 17 | 15 | 37 | see below |

The last row is the one near miss. The Rick and Morty S01E04 roll opens on a card at luma 30.5 with 2 boxes, just over
the dark line, and without it the rest of the roll's credit keyframes span 12.7 s, under the 15 s minimum; that is
why the GPU decode has no answer. Counting 2 boxes at luma 30–33 as a credit frame answers E04 (start 7.5 s after the
online truth, end on the roll's last frame) and moves six more episodes of the season to within 1 s of their
three-source truth (E03 +9.7 → +0.7, E05 +8.5 → +0.5, E06 +6.0 → −0.0, E08 +5.8 → −0.2, E10 +4.6 → −0.4, E09
+35.9 → +28.9), with every set count unchanged. But the harness's own run showed it moving three 205-set starts, and
one of them, Rescued by Ruby (2022), goes from inside the roll (the crew crawl, 48 s before a late chapter) to 128 s
before the chapter, on the film's epilogue cards: photos and a few lines of text on a dark ground. That count stays at
37 only because the chapter already called the old answer early; by frame check it adds a really early answer, so it
fails the bar's first check and is not shipped. It is the obvious next candidate if it can be told apart from
epilogue cards.

### What is still open

- **§13 item 13 (an end carried into a scene): fixed** for the measured shape; no end in either set moved.
- **§13 item 14 (late starts): narrowed, not closed.** The anchor's over-step was one cause of late answers (9 files
  of the 205 moved earlier, 6 of them into `useful` at both levels). The main cause stands: rule J takes the roll's
  last run, and a roll whose opening is names over bright footage, or is split by a gap longer than 24 s, is still
  answered from a later block (41 files of the 205 are more than 30 s late, down from 46). Every rule tried that
  reaches back to earlier blocks either adds early answers or gains a single file for tuned parameters (table above).
- The 205's gate still fails its usefulness floor (96 < 124) and both wrong caps (17 > 5, 15 > 3); the adjudication of
  its version 1 wrong answers above is unchanged, as no wrong answer moved.
