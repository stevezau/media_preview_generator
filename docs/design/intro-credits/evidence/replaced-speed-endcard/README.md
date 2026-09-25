# Speed by ear, carry-over, end card — evidence

Spec §5.3 ("Two playback speeds", "End picture"), §5.5 rule 15, §14 2026-09-25 ("Three intro fixes after #312").
Three production losses found after #312, each against Plex's own markers.

## 1. Speed by ear (season audio v8 in this lane, v9 merged)

RuPaul's Drag Race UK S08E04, AMZN release (23.976 fps) in a season of 25 fps episodes. The show is native 25 fps; the
release only changed the frame rate. The season step stretched its audio to 25 fps because of the frame rate alone.

| Pair (prod fingerprints, `e04` = files 1940-1942 E01-E03 25 fps, 1987 the AMZN E04, 1995 the SPAMnEGGS E04) | Runs |
|---|---|
| E02 / E03 against 1987 stretched (what prod ran) | none |
| E02 / E03 against 1987 as it plays | 307.7–330.0 ↔ 282.4–304.6; 297.8–314.4 ↔ 291.4–308.0 |
| 1995 (25 fps copy of E04) against 1987 as it plays | 897 of 900 s at offset 0 |
| 1995 against 1987 stretched | none |

Rule (`season.clock_by_audio`): a file at the other frame rate is matched both ways against the files at the group's
speed; it is retimed only when its retimed audio matches more of them (runs not mostly silence) than its own does;
on a tie it is matched as it plays. Scores (`speed_scores.py`, `speed_scores_rpdr.py`; references matched, own /
retimed): every Bones minority file 0–1 / all (S05 5 files 0/17, S06 2 files 0/21, S07 1 file 0/12, S08 2 files
1/22 and 0/22) → retimed; RPDR 1987 2/0 (3/0 with the 25 fps copy) → as it plays.

Even as it plays, 1987 has no season audio answer in its group of four (two clusters of one supporting episode each,
the quorum is two), so its intro comes from the carry-over (2). The file is gone from disk now: the STAN release (1996)
decided its own intro from its chapter.

Sets: no season group of lab 118, held-out 175, Accused or the #310 library chapter set mixes speeds
(`mixed_in_sets.py`, `mixed_chap.py`), so no answer there can move. Bones S05–S08, season audio alone, by frame
rate → by ear: **82 / 0 / 0 → 82 / 0 / 0**, 0 answers changed (`bones_speed.py`). Cost: 3.6 ms per pair
(`pair_cost.py`); both speeds of a pair are cached (`season_pair_runs`, one row per version).

## 2. Carry-over (§5.5 rule 15)

Tomb Raider King S01E12 (item 693668): the old WEBDL (file 1570) had an Intro chapter 0–92 s, decided and published;
the WEBRip that replaced it (file 1899) is identical in length (1,417,088 ms), has no chapters, and every source
answered nothing; the intro was removed from Plex. The frame sheet of 1899 (`e04/trk_e12_0-96.jpg`, local) shows the
full opening from 0 to about 89 s. RuPaul's Drag Race UK S08E04 (item 693934): 1939 (intro chapter 285–306 s) replaced
by 1987 (408 ms shorter), intro removed the same way.

Replay on production's markers.db (post-#312 snapshot, a copy; `carry_prod.py`): of the 174 files on disk with a type
in "no evidence", 2 have a replaced file of the same item; the rule carries **one** marker: Tomb Raider King S01E12's
intro 0–92 s. RPDR's 1987 is gone from disk now (its unit test reproduces it).

## 3. End card (end-picture check v2 in this lane, v3 merged)

Tomb Raider King S01E12's season step: all 4 other episodes of its folder match 0.3–89.2 s, but the end-picture check
gave 0.5 against both partners (E03, E05). Per instant (`trk_frames.py`, `trk_sync_detail.py`): the last 1.5 s (the
sword card) match with correlation 1.00; the 1.5 s before (a blond character, a crow, a dark creature, black) don't,
at any shift within ±1 s. The frame sheets (`trk_sheet.sh`) show why: E12's opening is re-cut before its end card
(a creature shot E02-E05 don't have, the crow and black half a second earlier), and its pictures run about 0.4 s ahead
of the audio against E02, E03 and E05 (`trk_sync.py`: best picture shift 0.33–0.5 s, mean correlation 0.75–0.85 against
0.15–0.19 at the audio alignment). The split of the season over two disks isn't the cause: its folder still had 4
partners. A half-second sync search alone doesn't pass it (`ep_sync_sets.py`, `trk_sync_share.py`: 0.5 either way).

Rule: a partner's share is 1 when the last 1.5 s (3 instants) all match on pictures whose inside (a border of 4 rows
and 7 columns, about 11 %, cropped) isn't flat — the same end card after shots that differ; a shared fade to black,
or black with a channel's logo in its corner, is no card. With it, `trk_after.py`: shares 1.0 and 1.0, season audio
0.31–89.23 s from 4 of 4 episodes.

| Measure | Before | After |
|---|---|---|
| lab 118, season audio alone (`ep_variants_sets.py`, "card") | 91 / 12 / 15 | 91 / 12 / 15 |
| held-out 175 | 124 / 4 / 47 | 124 / 4 / 47 |
| Accused | 3 / 0 / 53 | 3 / 0 / 53 |
| library chapter set | 111 / 59 / 54 | 111 / 59 / 54 |
| Bones S05–S08 (`bones_speed.py`, with fix 1) | 82 / 0 / 0 | 82 / 0 / 0 |
| the four sets' 195 failing checks (`card_all_failing.py`) | 0 pass | 23 pass: 22 the real intro, 1 without truth, 0 wrong |
| production's 16 failing early clusters (`prod_card.py`) | 0 pass | 1 passes: Tomb Raider King S01E12 |

Decided with IntroDB and Plex's own markers (`../intro-end/redecide.py`, `decide_libchap.py` on this tree): held-out
125 / 3 / 47, lab 118 87 / 4 / 27, Accused 3 / 0 / 53, chapter set 225 / 8 / 19, unchanged (no season audio answer
moved). Rejected: a half-second picture sync search (`ep_sync_sets.py`, `trk_sync_share.py`: 0.5 either way) and a
per-instant ±0.5 s search ("near": 0.5 on TRK; +1 useful on the chapter set, South Park S12E03).

## Rollback (integration)

`rollback_probe.py <this tree> <older tree>...`: this build writes `season_pair_runs`, `replaced_decisions`,
`version_reruns` and a carried marker; each older build (exports of 40311c3 and 98bed80) opens the database with its
own store code, replaces a file, caches a pair and reads the carried marker; this build opens it again. Both older
builds open it and work (schema 3, `season_pairs` recreated); on the way back this build empties the three tables
(spec §14 "Rollback"). It makes its own markers.db in a temp folder.

The other scripts ran from the session scratchpad (`laneL/`) with this worktree on `sys.path`; their paths still point there.
Outputs are local-only.
