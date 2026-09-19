# On-screen credit text on broadcast TV (final review, 2026-09-19)

The credit text detector (spec §5.4, rule J version 2) was measured on the 80 hand-checked files and the 205 movies
(`phase3-harness.md`), and none of them is a broadcast recording. This page measures it on 51 broadcast files, with a
channel logo, tickers or score graphics on screen through the story. It answers three questions. Does version 2's
"text on screen all through the tail" guard (`rule_j.text_all_through`) throw away real answers on logo'd TV? What does
the pipeline publish on these files? And do two candidate fixes help: suppressing static overlays, or not letting
credit text and a server's own marker decide High on their own?

**Outcome (controller's ruling, 2026-09-19): broadcast TV ships as measured, recorded here as a known limit.** The
guard costs one real answer (GPU), and removes two wrong or false answers on the GPU and three on the CPU. Credit text
on this population fails the Q4 gate's wrong-answer caps at Medium and High. The static-overlay prototype and the High
rule change were both rejected.

## The population

- **How the files were found.** The lab scale run's read-only dump of prod Plex's parts (125,956 parts) was searched by
  file name for broadcast captures (`HDTV`, `PDTV`, `DSR`, `SDTV`, `DVB`, `TVRip`, a `.ts` container), plus the Sports
  library. The library holds no Plex DVR recordings: there is no "Recordings" folder, and its 22 `.ts` files are
  downloaded releases in ordinary show, movie and sports folders. One mid-episode frame per candidate show was looked
  at, and the shows with a channel or show logo on screen were kept. From each show, 3 episodes spread over its seasons
  were picked.
- **39 episodes of 13 shows.** Channels were identified by their on-screen logo on at least one of the show's
  episodes:
  - A&E: Live Rescue, 60 Days In.
  - Investigation Discovery: Homicide Hunter.
  - Network 10: Bondi Rescue.
  - LifeStyle: Grand Designs Australia.
  - Mayday, one episode each on Discovery, National Geographic and a third HD channel.
  - BBC Three: Top Gear.
  - Channel 4 and SBS: 24 Hours in Police Custody.
  - FOX: MasterChef Junior.
  - Channel 5: The Railway Killers.
  - CTV: Grey's Anatomy.
  - ABC: You Can't Ask That.
  - Mean Mums' channel wasn't read.
- **12 sports feeds:**
  - NBA ×2.
  - English Premier League ×2.
  - Formula 1 ×2 (F1 TV).
  - FIFA World Cup ×2.
  - UFC ×2 (UFC Fight Pass).
  - Boxing ×1 (Main Event).
  - One file named as a PGA Tour event, which holds ESPN's college football championship broadcast.
- **Codecs and containers:** 37 H.264 and 14 HEVC; 45 .mkv, 4 .mp4, 2 .ts. None is VP9 (see "Code measured").
- **Plex's own credits markers** exist for 22 of the 51 files. None of the 51 has a credits chapter (ffprobe). No
  online (SkipDB / TheIntroDB) answers are recorded for them.

## Truth (frame checks)

Every file's tail was frame-checked, with no chapter or online truth to lean on:
- A contact sheet of the last 450 s, one frame every 15 s.
- 1 s strips around the start of the closing credits and around every answer any source gave.

Truth falls in three classes:

- **Roll (34 files):** the first frame of the closing credits (cards or crawl). Text cards that end the story ("…was
  sentenced to seven years"), promos and name captions aren't credits.
- **End card only (5 files):** Bondi Rescue ×3 and Mean Mums ×2 end on a website or production card and logos, with no
  roll. No answer is fine. An answer is judged against the card.
- **No credits (12 sports feeds):** they end in studio talk, interviews, stats graphics or a "stream has ended" slate.
  Any answer is **false**.

Verdicts use the harness's `judge_credits`:
- **Wrong:** more than 10 s early.
- **Late:** more than 30 s late.
- **Missed:** no answer on a roll.
- **Useful:** everything else.

## Code measured

- **Commit:** 391ca87, rule J version 2 with the guard (`rulej-r2`'s tip at the time).
- **Decode digest:** `d8c83e982a5c30a7`, the round-2 final runs'.
- **GPU path:** NVIDIA decode on the Quadro P5000, text detection on `webgpu cuda:0`.
- **CPU path:** CPU decode and CPU text detection.
- **Reproduction:** the app's own `detector.find_credits` through the harness's `DecodeCache`.

The VP9 fix (75b76e5, `feat/markers-detection` 01361da) changes nothing for these files:
- None of the 51 is VP9.
- The keyframe-pass argv that 01361da builds for each file, on both paths, is byte-identical to 391ca87's (checked by
  building both).
- `rule_j.py` and `decide.py` are the same in both commits.

## The guard: text share before the run

Rule J version 2 gives no answer when the tail holds less than 30 s before the credit run, or when 80 % or more of the
keyframes before the run carry any text box. Numbers are over the files where rule J found a run:

| Path | Files with a run | Share: min / median / p90 / max | TV episodes only: median / p90 / max | Tail before the run: min / median |
|---|---|---|---|---|
| GPU | 32 (23 TV) | 0.015 / 0.34 / 0.76 / 0.92 | 0.32 / 0.75 / 0.92 | 75 s / 404 s |
| CPU | 33 (24 TV) | 0.015 / 0.31 / 0.70 / 1.00 | 0.23 / 0.70 / 1.00 | 28 s / 407 s |

On the same stored decodes, the 80 reach 0.46 on both paths, the 205 0.54, and the 80's 40 episodes 0.17. On
broadcast TV the median is 0.3 and p90 is 0.7 to 0.76, so the 0.8 threshold leaves little room.

**Every file the guard changed** (answers with the guard patched off, frame-checked):

| File | Path | Share, tail before | Answer without the guard | Verdict |
|---|---|---|---|---|
| Live Rescue S02E01 | GPU | 0.92, 400 s | 5017 s | **Real answer lost.** The credits squeeze starts at 5011 s, so the answer was useful. |
| Live Rescue S02E01 | CPU | 1.00, 238 s | 4842–4872 s | Wrong one removed (a studio segment). |
| Bondi Rescue S14E12 | GPU | 0.83, 214 s | 2229 s to the end | Wrong one removed (234 s of story under a network promo, before the end card). |
| Mayday S12E10 | CPU | 0.24, **28 s** | 2276–2303 s | Wrong one removed by the 30 s floor (story). |
| Boxing, Tszyu vs Nurja | GPU / CPU | 0.85 / 0.84, 342 s | 4204–4227 / 4204–4235 s | False answer removed (studio talk with a ticker). |

Files at share ≥ 0.6 that the guard kept:
- **Homicide Hunter S06E02, GPU (0.70):** useful.
- **Bondi Rescue S14E12, CPU (0.70):** useful against the card.
- **Live Rescue S03E01 (0.77 / 0.73):** wrong.
- **Bondi Rescue S15E03-E04 (0.63 / 0.68):** wrong.
- **UFC Muhammad vs Bonfim (0.71 / 0.64):** false.

Other thresholds, scored on the stored rows (no answer on the 80 or the 205 moves under any of them):
- **0.9:** returns the Bondi S14E12 wrong answer (GPU) and the Boxing false answer (both paths), and still loses Live
  Rescue S02E01 on the GPU.
- **0.95:** gives Live Rescue S02E01 back on the GPU, but returns the Bondi S14E12 wrong answer and the Boxing false
  answer.
- **0.6:** removes three more wrong or false answers per path, but also one useful answer per path (Homicide Hunter
  S06E02 on the GPU, Bondi S14E12 on the CPU).
- **"≥ 2 boxes" instead of "any box":** stops catching 5 of the lab's 6 Synth Audio episodes (burnt-in timecode).

## What the pipeline publishes

Every source available offline, through the harness's `decide()` with the app's source order:
- Plex's credits markers as `server_markers`.
- Chapters (none here).
- Credit text, added only when the other sources leave credits undecided, as the pipeline does.

Counts over the 34 roll files, then the 5 end-card files, then the 12 sports feeds:

| Row | Useful | Wrong | Late | Missed | End-card files | Sports: false / none |
|---|---|---|---|---|---|---|
| Plex | 10 | 6 | 0 | 18 | 5 none | 6 / 6 |
| Credit text, GPU | 13 | 7 | 0 | 14 | 1 wrong, 4 none | 8 / 4 |
| Credit text, CPU | 12 | 8 | 0 | 14 | 1 useful, 1 wrong, 3 none | 8 / 4 |
| Medium, GPU | 10 | 5 | 0 | 19 | 1 wrong, 4 none | 4 / 8 |
| Medium, CPU | 10 | 4 | 0 | 20 | 1 useful, 1 wrong, 3 none | 5 / 7 |
| High, GPU | 4 | 2 | 0 | 28 | 5 none | 0 / 12 |
| High, CPU | 3 | 2 | 0 | 29 | 5 none | 1 / 11 |

The Q4 gate applied to these 51 files. "Wrong" counts roll and end-card answers more than 10 s early; false sports
answers are counted in brackets.

| Check | GPU | CPU |
|---|---|---|
| Medium useful ≥ Plex useful (10) | pass (10) | pass (10, 11 with the card) |
| Medium wrong ≤ 2 (2 % of 51) | **fail: 6 (+4 false)** | **fail: 5 (+5 false)** |
| Medium wrong ≤ Plex wrong (6, +6 false) | pass | pass |
| High wrong ≤ 1 (1 % of 51) | **fail: 2** | **fail: 2 (+1 false)** |
| High wrong ≤ Plex wrong | pass | pass |

Per file, in seconds:
- In the answer columns, "none" is no answer on an end-card file and "—" is no answer on a sports feed.
- In the Plex column, "—" means Plex has no credits marker.
- In the guard-share column, "—" means rule J found no run.

| # | File | Truth | Plex | Guard share GPU / CPU | Text GPU | Text CPU | Medium GPU / CPU | High GPU / CPU |
|---|---|---|---|---|---|---|---|---|
| 0 | Live Rescue S01E06 | roll 1160 | — | — / — | missed | missed | missed / missed | missed / missed |
| 1 | Live Rescue S02E01 | roll 5011 | — | 0.92 / 1.00 | missed | missed | missed / missed | missed / missed |
| 2 | Live Rescue S03E01 | roll 5056 | — | 0.77 / 0.73 | 4910–4936 wrong | 4910–4933 wrong | 4910 wrong / 4910 wrong | missed / missed |
| 3 | 60 Days In S02E11 | roll 3711 | — | — / — | missed | missed | missed / missed | missed / missed |
| 4 | 60 Days In S04E03 | roll 2484 | — | — / — | missed | missed | missed / missed | missed / missed |
| 5 | 60 Days In S05E11 | roll 4998 | — | — / — | missed | missed | missed / missed | missed / missed |
| 6 | Homicide Hunter S06E03 | roll 2523 | 2303 wrong | 0.57 / 0.31 | 2532 useful | 2356–2388 wrong | missed / missed | missed / missed |
| 7 | Homicide Hunter S06E01 | roll 2522 | — | 0.32 / 0.21 | 2529 useful | 2529 useful | 2529 useful / 2529 useful | missed / missed |
| 8 | Homicide Hunter S06E02 | roll 2521 | — | 0.70 / 0.38 | 2528 useful | 2528 useful | 2528 useful / 2528 useful | missed / missed |
| 9 | Bondi Rescue S14E12 | end card 2463 | — | 0.83 / 0.70 | none | 2455 useful | none / 2455 useful | none / none |
| 10 | Bondi Rescue S15E03-E04 | end card 2491 | — | 0.63 / 0.68 | 2352 wrong | 2471 wrong | 2352 wrong / 2471 wrong | none / none |
| 11 | Bondi Rescue S16E01 | end card 1304 | — | — / — | none | none | none / none | none / none |
| 12 | Grand Designs Australia S05E01 | roll 2854 | 2855 useful | — / — | missed | missed | missed / missed | missed / missed |
| 13 | Grand Designs Australia S06E01 | roll 2771 | 2770 useful | — / — | missed | missed | missed / missed | missed / missed |
| 14 | Grand Designs Australia S07E01 | roll 2806 | 2806 useful | — / — | missed | missed | missed / missed | missed / missed |
| 15 | Mayday S03E11 | roll 2728 | 2728 useful | 0.12 / 0.12 | 2724 useful | 2724 useful | 2724 useful / 2724 useful | 2724 useful / 2724 useful |
| 16 | Mayday S11E11 | roll 2685 | 2685 useful | 0.45 / 0.41 | 2550–2571 wrong | 2550–2571 wrong | missed / missed | missed / missed |
| 17 | Mayday S12E10 | roll 2685 | 2686 useful | 0.34 / 0.24 | 2684 useful | missed | 2684 useful / missed | 2684 useful / missed |
| 18 | Top Gear S01E03 | roll 3503 | 3338 wrong | 0.10 / 0.10 | 3347–3395 wrong | 3347–3383 wrong | 3347 wrong / 3347 wrong | 3347 wrong / 3347 wrong |
| 19 | Top Gear S02E01 | roll 2887 | 2875 wrong | — / — | missed | missed | missed / missed | missed / missed |
| 20 | Top Gear S05E07 | roll 3505 | 3359 wrong | — / 0.03 | missed | 3371–3398 wrong | missed / missed | missed / missed |
| 21 | 24 Hours in Police Custody S04E03 | roll 2823 | 2824 useful | 0.07 / 0.06 | 2814 useful | 2814 useful | missed / missed | missed / missed |
| 22 | 24 Hours in Police Custody S06E02 | roll 2768 | 2566 wrong | 0.41 / 0.33 | 2764 useful | 2764 useful | missed / missed | missed / missed |
| 23 | 24 Hours in Police Custody S07E04 | roll 2798 | 2775 wrong | 0.04 / 0.04 | 2775 wrong | 2775 wrong | 2775 wrong / 2775 wrong | 2775 wrong / 2775 wrong |
| 24 | MasterChef Junior S03E01 | roll 2527 | — | 0.44 / 0.35 | 2532 useful | 2532 useful | 2532 useful / 2532 useful | missed / missed |
| 25 | MasterChef Junior S05E15 | roll 2472 | — | 0.29 / 0.36 | 2481 useful | 2481 useful | 2481 useful / 2481 useful | missed / missed |
| 26 | MasterChef Junior S03E02 | roll 2533 | — | 0.48 / 0.45 | 2537 useful | 2534 useful | 2537 useful / 2534 useful | missed / missed |
| 27 | The Railway Killers S01E01 | roll 2668 | 2669 useful | 0.08 / 0.08 | 2669 useful | 2669 useful | 2669 useful / 2669 useful | 2669 useful / 2669 useful |
| 28 | The Railway Killers S01E02 | roll 2686 | 2686 useful | 0.04 / 0.06 | 2686 useful | 2686 useful | 2686 useful / 2686 useful | 2686 useful / 2686 useful |
| 29 | The Railway Killers S01E03 | roll 2672 | — | — / — | missed | missed | missed / missed | missed / missed |
| 30 | Grey's Anatomy S20E01 | roll 2490 | — | — / — | missed | missed | missed / missed | missed / missed |
| 31 | Grey's Anatomy S22E01 | roll 2464 | — | 0.12 / 0.03 | 2463 useful | 2463 useful | 2463 useful / 2463 useful | missed / missed |
| 32 | Grey's Anatomy S20E07 | roll 2519 | — | 0.22 / 0.06 | 2406–2449 wrong | 2519 useful | 2406 wrong / 2519 useful | missed / missed |
| 33 | Mean Mums S01E01 | end card 1335 | — | — / — | none | none | none / none | none / none |
| 34 | Mean Mums S02E02 | roll 1366 | — | — / — | missed | missed | missed / missed | missed / missed |
| 35 | Mean Mums S01E02 | end card 1236 | — | — / — | none | none | none / none | none / none |
| 36 | You Can't Ask That S02E01 | roll 1630 | — | 0.02 / 0.02 | 1581 wrong | 1581 wrong | 1581 wrong / 1581 wrong | missed / missed |
| 37 | You Can't Ask That S06E01 | roll 2063 | — | — / — | missed | missed | missed / missed | missed / missed |
| 38 | You Can't Ask That S02E02 | roll 1679 | 1678 useful | 0.01 / 0.01 | 1653 wrong | 1653 wrong | missed / missed | missed / missed |
| 39 | NBA, Lakers vs Trail Blazers | no credits | 6608 false | 0.15 / 0.16 | 6505–6549 false | 6653–6669 false | — / — | — / — |
| 40 | NBA, Hawks vs Nets | no credits | — | 0.40 / 0.35 | 8841–8847 false | 8841–8847 false | 8841 false / 8841 false | — / — |
| 41 | EPL, Everton vs Liverpool | no credits | — | — / — | — | — | — / — | — / — |
| 42 | EPL, Newcastle vs Brighton | no credits | 6287 false | — / — | — | — | — / — | — / — |
| 43 | F1, Japanese GP Practice 1 | no credits | — | 0.20 / 0.14 | 5781–5821 false | 5781–5821 false | 5781 false / 5781 false | — / — |
| 44 | F1, Australian GP Race | no credits | 6272 false | 0.46 / 0.37 | 6550–6866 false | 6732–6812 false | — / 6732 false | — / 6732 false |
| 45 | FIFA World Cup, Canada vs Bosnia-Herzegovina | no credits | — | — / — | — | — | — / — | — / — |
| 46 | FIFA World Cup, Bosnia-Herzegovina vs Qatar | no credits | 8908 false | 0.34 / 0.31 | 9300 false | 9301 false | — / — | — / — |
| 47 | UFC Fight Night 271, Adesanya vs Pyfer | no credits | — | 0.24 / 0.27 | 12641 false | 12641 false | 12641 false / 12641 false | — / — |
| 48 | UFC Fight Night 278, Muhammad vs Bonfim | no credits | 8848 false | 0.71 / 0.64 | 12519 false | 12560 false | 12519 false / 12560 false | — / — |
| 49 | Boxing, Tszyu vs Nurja | no credits | — | 0.85 / 0.84 | — | — | — / — | — / — |
| 50 | "PGA Tour" file (ESPN college football) | no credits | 9551 false | 0.39 / 0.38 | 9680 false | 9680 false | — / — | — / — |

## Failure shapes (frame-checked)

Rule J counts boxes, so broadcast graphics become credit frames: a dark frame needs one box, a lit one three.

- **Logo, show logo and location tag on dark story.** One box turns a dark frame into a credit frame:
  - Live Rescue S03E01: the A&E and show logos plus an "Earlier in Paterson, NJ" tag over a night fire call. The answer
    is 146 s early, and Medium publishes a 26 s (GPU) / 23 s (CPU) skip of story.
  - Homicide Hunter S06E03, CPU: an ID promo over a night scene.
  - Grey's Anatomy S20E07, GPU: the CTV logo over a dark scene.
  - Mayday S11E11: the National Geographic logo over a dark engine animation.
- **Network promo lower thirds on lit story** (logo plus two lines of promo text reach three boxes): Bondi Rescue
  S15E03-E04 and S14E12.
- **In-show overlays:**
  - Top Gear S01E03: the Stig lap's telemetry (timer, g-force, speed). The answer is 156 s early. Plex's marker agrees
    (3338 s), so High publishes it.
  - Top Gear S05E07, CPU: the studio's lap-time boards.
- **Name captions:** You Can't Ask That S02E01 (49 s early) and S02E02 (26 s early).
- **Epilogue text cards on black:** 24 Hours in Police Custody S07E04 (23 s early). Plex's marker agrees, so High
  publishes it. On S06E02 the answer lands on the last card, 4 s early (useful).
- **Missed rolls (14 per path):**
  - Credits over bright footage: Grand Designs Australia ×3 (Plex gets all three), Top Gear S02E01.
  - Rolls shorter than rule J's 15 s run: Top Gear S05E07 on the GPU (5 s; the CPU answers it wrong), You Can't Ask
    That S06E01 (about 8 s), and on the CPU rows Mayday S12E10, whose 17 s roll has credit frames over less than 15 s.
  - The guard: Live Rescue S02E01 (GPU: a useful answer lost; CPU: a wrong one removed).
  - No run found, cause not characterised: 60 Days In ×3, Mean Mums S02E02, The Railway Killers S01E03, Grey's Anatomy
    S20E01, Live Rescue S01E06.
- **Sports feeds** (8 of 12 answered on both paths): studio and interview segments carry score bugs, tickers and stats
  graphics. That covers NBA post-game and end slates, the F1 classification ticker and standings, UFC in-cage
  interviews and the "stream has ended" slate, FIFA's post-match graphics, and ESPN's post-game.

## Tried and not taken

### Static-overlay suppression (dropped)

Rows hold only box counts, so a scratch prototype kept each frame's box positions:
- Tail keyframes were decoded exactly as the app does. The app's `frames.decode_rows` and `find_credits` were served by
  the same detector build in-process.
- Box counts equal the helper's stored counts on every decode checked: 160/160 (80, GPU), 154/154 (80, CPU), 285/285
  (205, GPU), 91/91 (broadcast, both paths) and 12/12 (lab, CPU).
- With suppression off, every answer equals the shipped one.

The rule, chosen on the 80 / 205 and the lab files only:
- Boxes on the tail's keyframes are clustered by IoU ≥ 0.5.
- A cluster present on at least 50 % of keyframes counts as a static overlay.
- A box lying ≥ 60 % inside one is dropped before counting, in the keyframe rows and in every 1 fps window of that file.
- Rule J version 2 then runs unchanged, guard included.
- Plain IoU matching was rejected because it gave 3 of the 6 Synth Audio episodes an answer on the GPU (34, 130 and
  221 s). Containment keeps all six at no answer on both paths, and Synth Credits / Synth Credits Open stay 541–659 s
  and 541 s. A 70 % threshold changes nothing anywhere.

Results:
- **The 80 (GPU and CPU) and the 205 (GPU):** no answer moves. The 80 stay 64 / 1 (GPU) and 59 / 1 (CPU) within
  10 s / early. Medium stays 58 useful / 1 wrong (GPU) and 96 / 17 on the 205.
- **Broadcast, GPU, 4 answers move:**
  - Live Rescue S02E01: none → 5017 s, useful (the answer the guard lost).
  - Live Rescue S03E01: wrong → none.
  - UFC Adesanya vs Pyfer: moves earlier, to an 18 s skip of the in-cage interview (still false).
  - Boxing: none → 3926–3947 s, a **new false answer**. With the ticker removed the guard no longer fires, and backstage
    shots in front of the sponsor wall form a run.
- **Broadcast, CPU:** Live Rescue S03E01 wrong → none; UFC Adesanya vs Pyfer false → none; Boxing none → 4049–4078 s,
  false.
- **Coverage:** only 6 of the 51 broadcast files and none of the 80 have any static cluster. Most channel logos here
  aren't boxed on enough keyframes at 320 × 180 to form one.

It fails the owner's bar: it is never strictly better on the reference sets, and it adds a false answer on broadcast.
It also can't reach the bigger failure shapes above.

Cost, had it been kept:
- Positions come from the same detector call, so decoding costs nothing more.
- Clustering and rule J on stored rows took 0.06 s per file on the GPU path and 0.08 s on the CPU path (at most 1.9 s
  and 6.4 s, on the lab's 7,200-keyframe file).
- Every row would carry its boxes (about 4 small integers each, a few KB per file).
- The helper's protocol would have to return boxes instead of counts.
- Every stored decode and `credits_rule_j_80.json.gz` would have to be rebuilt.

### Credit text + a server's own marker not enough at High (rejected)

Both High wrong answers on broadcast are credit text and Plex's own marker agreeing on the same picture (Top Gear
S01E03's telemetry, 24 Hours in Police Custody S07E04's epilogue cards). Both detectors read the frames, so their
agreement isn't independent. A scratch patch of `decide._agreeing_cliques` made a credits clique of only credit text
and `server_markers` unable to decide at High (Medium untouched). Stored answers only:

| Population | High before | High after |
|---|---|---|
| The 80, GPU | 41 useful, 1 wrong, 38 missed | 80 missed |
| The 80, CPU | 41 useful, 1 wrong, 38 missed | 80 missed |
| The 205, GPU | 95 useful, 10 late, 15 wrong, 85 missed | 205 missed |
| Broadcast, GPU | 4 useful, 2 wrong, 28 missed | 34 missed |
| Broadcast, CPU | 3 useful, 2 wrong, 1 false, 29 missed | 34 missed, the false answer gone |

- **What changes on broadcast:** Top Gear S01E03 and 24 Hours in Police Custody S07E04 go from wrong to missed. On the
  CPU, the F1 Australian GP false answer is removed. Mayday S03E11, The Railway Killers S01E01 and S01E02, and (GPU)
  Mayday S12E10 go from useful to missed.
- **Medium:** no answer moves on any population.
- **Why it's rejected:** on these sets, credit text plus a server's own marker is the only way High ever publishes
  credits, so the rule empties High on a library without chapters or online answers.

## Scripts (kept out of git)

The scratch scripts ran from a session scratchpad and are not committed. They held library paths, and the decodes
stayed in the harness's cache. What they did:
- Measured each file through the app's `find_credits` and the `DecodeCache`, once as shipped and once with
  `rule_j.text_all_through` patched off, and recorded the share and the tail before the run.
- Scored text, Plex, chapters and `decide()` at High and Medium against the frame-checked truth.
- Ran the static-overlay prototype, replaying rule J on positions.
- Patched `decide._agreeing_cliques` for the High simulation.
- Built the keyframe-pass argv at 391ca87 and 01361da for the VP9 check.
- Made contact sheets and 1 s strips for the frame checks.

Media was only read, and every run was `nice -n 19`.
