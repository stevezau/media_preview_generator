# Chapter rules — anime titles, before and after (phase 4, Task 15), 2026-09-20

What Task 1 found (`aniskip-facts.md` §5) was measured properly here, against the owner's whole library,
before anything changed:

1. **`Ending` ships**, scoped to TV episodes. It gains **280 anime credits answers** and changes
   **nothing** on 11,919 non-anime TV episodes and 9,904 movies. The episode test is the file's **own
   path** (a season and episode in the name), not a kind a server resolved — §3 says why, and what the
   stricter input costs (nothing, measured).
2. **`End` alone does not ship.** No anime file in the library uses it; four movies and five non-anime
   TV episodes do, and taking it there would add three answers, all wrong or useless.
3. **Dropping a lone generic `Intro` does not ship.** It would remove 569 anime intros and 857
   non-anime TV intros to get rid of the 7 anime files Plex's own intro marker says are cold opens.

`CHAPTER_RULES_VERSION` went 1 → 2, which is what carries the rule to files the app has already probed.

---

## 1. The three populations

Files come from the phase-2 read-only dump of prod Plex's parts (no server was queried). Anime is
Sonarr's own `seriesType == "anime"` on the show's `{tvdb-…}` id — **used only to split the
measurement populations**; the shipped rule never reads Sonarr and could not (§3).

| Population | In the library | Probed | ffprobe read it | With chapters |
|---|---:|---:|---:|---:|
| anime episodes | 4,421 | all | 4,346 | 2,545 |
| non-anime TV episodes | 110,211 | 12,000 (seeded flat sample) | 11,919 | 4,169 |
| movies | 9,990 | all | 9,904 | 5,485 |

The TV sample is flat and seeded (`chapters_populations.py`, seed 20260920): a per-file error rate is
what the precision question asks for, and a flat sample is its unbiased estimator. Probing all 110,211
would have taken most of a day on a shared box. `ffprobe -show_chapters` reads the container header
only; nothing under `/data*` was written.

**The `Ending` chapter is an anime name, and only an anime name, in this library.**

| Bare whole-title chapter | anime | non-anime TV | movies |
|---|---:|---:|---:|
| `Ending` (any case) | **282** | 0 | 1 |
| `End` (any case) | 0 | 5 | 4 |

The anime `Ending` files sit in 30 shows. 264 of the 282 also carry an `Opening…` chapter, 157 a
preview chapter, and **none** carries an exact-uppercase `OP` or `ED` — the shows that use `ED` are a
different set from the shows that use `Ending`.

## 2. Baseline — today's classifier, before any change

Each row is what the app publishes from the file's chapters **alone**, through the real `decide()` at
High, so spec §5.5 rule 2's sanity checks are included. Plex's own markers (same read-only dump) are
the cross-check, **not truth** — the anime rows below show why.

| | anime (4,346) | non-anime TV (11,919) | movies (9,904) |
|---|---:|---:|---:|
| intro published from chapters | 1,145 | 1,201 | 229 |
| credits published from chapters | 827 | 2,147 | 1,550 |

How those existing answers already sit against Plex's own marker for the same file:

| credits, vs Plex | anime | non-anime TV | movies |
|---|---:|---:|---:|
| agrees (±10 s) | 270 | 1,043 | 832 |
| earlier than Plex by >10 s | **148** | 49 | 155 |
| later by 10–30 s | 9 | 114 | 129 |
| later by >30 s | 19 | 130 | 403 |
| Plex has no marker | 381 | 811 | 31 |

**Read this row before reading any "vs Plex" number below.** A third of the anime credits the app
already publishes from chapters are more than 10 s earlier than Plex's marker. Plex's credits
detection lands late on anime — it tends to mark the copyright card rather than the ED song. So
"earlier than Plex" on anime is a flag to frame-check, not a verdict.

## 3. What "anime" can mean inside `chapters.py`

`chapter_candidates()` sees one file's chapters and nothing else. The measurement ruled out every
in-file scope:

| Candidate scope | Catches the anime `Ending` files | Catches the movie `Ending` file |
|---|---:|---|
| the file also has a specific opening chapter | 264 of 282 | **yes** — the movie's first chapter is called `Opening` |
| the file also has any intro chapter | 265 of 282 | **yes** — same chapter |
| the file also has an exact `OP` or `ED` chapter | 0 of 282 | no |
| the file also has a preview chapter | 157 of 282 | no |

The one movie with a bare `Ending` chapter is a 1988 documentary whose chapters are
`Opening`, then ten scene names, then `Ending`. It is indistinguishable from an anime episode by
chapter shape alone. **Library kind is the only scope that separates them**, and it is also what the
rule's own comment always said: `End`/`Ending` are excluded because they are final-scene names *in
movies*.

So the rule reads `Ending` as credits only when the file **is a TV episode by its own path**:

- `classify_chapter_title(title, *, is_episode=False)` and `chapter_candidates(probe, *, is_episode=False)`.
- `pipeline.py` and `audio/season.py` both pass `ids_from_path(path).is_episode` — a season and episode
  number in the file's own name.

**Why the path and not the kind the pipeline resolves.** Chapter evidence is cached per file and
re-derived only when `CHAPTER_RULES_VERSION` changes. A resolved kind can change under that cache (a
server that answered nothing on the first run answers later), which would leave candidates derived
under a kind the file no longer has — in the wrong direction, a movie keeping a credits candidate from
its last scene. The path is part of the file's identity, so it cannot drift. The same reasoning applies
to the season step: it stores evidence for every member of a season group, and a season group can hold
a file whose name carries no `SxxEyy`, so each member is read with its own path's answer rather than
the asking episode's.

**The stricter input costs nothing here, measured:** all 282 anime files with a bare `Ending` chapter
name a season and episode in the path (282 of 282), as do 11,906 of the 11,919 non-anime TV episodes;
all 9,904 movies do not.

**What the scope catches:** every TV episode whose filename carries `SxxEyy`, anime or not. **What it
misses:** anime *films* in a Movies library, and an episode in an absolute-numbered folder whose
filename has no season and episode — even when a server calls it an episode. Those keep today's
behaviour — a missing marker, which spec §0 accepts.

## 4. The `Ending` rule, measured

Each variant was scored the same way as the baseline. "Gained" = a file with no credits answer before
and one after; "moved" = an answer that changed; "lost" = an answer that disappeared.

| Variant | anime | non-anime TV | movies |
|---|---|---|---|
| `Ending`, any file | +280 / 0 / 0 | no change | **+1 / 0 / 0 — wrong** |
| `Ending` + `End`, any file | +280 / 0 / 0 | +3 / 0 / 0 | **+3 / 0 / 0 — 1 wrong, 2 useless** |
| `Ending`, file has a specific opening chapter | +263 / 0 / 0 | no change | **+1 — the same wrong one** |
| `Ending` + `End`, **episodes only** | +280 / 0 / 0 | +3 / 0 / 0 | no change |
| **`Ending`, episodes only — shipped** | **+280 / 0 / 0** | **no change** | **no change** |

The shipped code was then run against all three populations and matched the winning variant file for
file (`chapters_score.py`, `shipped code matches the ending_only_episode variant exactly: True`).

### The cost, checked by hand

**Movies — the one file a global rule would break, frame-checked.** `Ending` 6746–7187 s of a 7187 s
documentary; Plex says credits at 6907 s. Frames at 10 s steps from 6740 s show the film still
running: interview footage, then an epilogue title card at ~6880 s, then more footage. The chapter is
the film's last *scene*. Publishing it would skip the last **161 s of the film**, including the
epilogue card. This is the single measured wrong answer, and the episode scope removes it.

**Movies — the four `End` files.** Two survive sanity: a 3 s `End` chapter at 6224 s of 6227 s
(Plex's credits: 6128 s) and at 7002 s of 7008 s (Plex: 6554 s). Both would put the skip prompt
96 s and 448 s after the credits actually start. Not story-skipping, but worse than saying nothing.

**Non-anime TV — the five `End` files.** Three survive sanity, all 3–9 s `End` chapters in two
cartoons. Same shape as the movies.

**Anime — what the 280 gained answers look like.** 244 are 60 s or longer (an ED song), 24 are
30–60 s, and **12 are under 30 s**. Against Plex: 65 agree, 146 land on a file Plex has no credits
marker for, 61 are more than 10 s earlier than Plex, 5 more than 30 s later.

Two of the "earlier than Plex" files were frame-checked, one from the middle of the bucket and one
from its extreme:

- Goblin Slayer S02E01, `Ending` 1323–1413 s, Plex 1368 s. The credit roll starts at 1323 s. **Plex is
  45 s late; the chapter is right.**
- Food Wars! S01E07, `Ending` 1298–1388 s, Plex 1433 s. The credit roll runs 1298–1388 s; Plex's
  marker is on the epilogue after it. **Plex is 135 s late; the chapter is right.**

That, with the baseline row in §2, is why "earlier than Plex" on anime is not counted as a cost.

The 12 short answers **are** a cost. All 12 are One Piece S21, and one was frame-checked: that HDTV
cut has no ED at all, and its `Ending` chapter is the 4-second "TO BE CONTINUED" card. Publishing it
means a skip prompt that flashes for four seconds on a card. It is a poor answer, not a
story-skipping one, and it is the same contract the chapter source already has for a short `Credits`
or `ED` chapter, so no length floor was added for `Ending` alone. **12 of 280, in one show.**

### The hand-checked truth sets say nothing changed

The phase-2 harness (`python -m tools.markers_eval report`) was re-run after the change:

- **80 hand-checked files (40 movies + 40 TV), last credits chapter vs the adjudicated truth: 78 useful
  / 1 late / 1 wrong / 0 missed — byte-identical to phase 2's recorded row.** Those sets hold no
  "Ending" titles, so this is the "the rule is inert where it should be" check on real truth.
- Ledger L165 on the lab scale run's 787 chapters — the only stored set with anime, and the only one
  holding movies and episodes together (603 episodes, 184 movies by path), so it is now scored row by
  row with each file's own kind rather than as one population. Files with a credits title rose to 491,
  and the "Ending titles not counted as credits" list went from 16 names (Chainsaw Man S01, Food Wars!
  S01, JUJUTSU KAISEN S02, SPY x FAMILY S01) to **empty** — including on the 184 movies, which is the
  side of that set the ledger exists to protect.
- The run's exit code is still 1 for the pre-existing reason phase 2 recorded ("with G3 on, the gate
  fails by construction"); nothing about that row moved.

## 5. Measured and not taken

### `End` as credits

| | anime | non-anime TV | movies |
|---|---:|---:|---:|
| files with a bare `End` chapter | **0** | 5 | 4 |
| answers it would add | 0 | 3 | 2 (a global rule) |

It buys nothing on the population it was meant for and costs answers everywhere else. `End` stays an
ordinary chapter name, on an episode as well as a movie.

### Dropping a lone generic `Intro`

581 anime files have exactly one intro chapter and it is a generic `Intro`, with no specific opening
chapter to fire §5.1's cold-open rule — Task 1's number, reproduced exactly. Is that chapter the theme
song or the cold open? Plex's own intro marker is found by matching audio across the season, so it
lands on the theme; a chapter that ends where Plex's intro ends is the theme, one that ends before
Plex's intro starts is the cold open.

| | anime (581) | non-anime TV (911) |
|---|---:|---:|
| theme — ends where Plex's intro ends | **233** | 273 |
| overlaps Plex's intro, edges differ | 31 | 67 |
| starts after Plex's intro | 3 | 5 |
| **cold open — ends before Plex's intro starts** | **7** | 4 |
| Plex has no intro marker | 307 | 562 |

Of the 274 anime files Plex can judge, **233 (85%) are the theme and 7 (2.6%) are the cold open**. Per
show (59 anime shows with such a file): 10 are uniformly theme, 1 uniformly cold open, 18 mixed, 29
unjudgeable. The convention is *not* uniform within a show here, so a per-show rule would not help.

Not shipping it, stated plainly: **not deciding an intro from a lone generic `Intro` would lose 569
anime intros and 857 non-anime TV intros to remove 7 known-wrong anime answers.** That is the honest
outcome the task asked for — fewer intros — so it is dropped. What Task 1 suspected ("on anime that
chapter is often the cold open") is not what the library says: it is usually the theme.

### Other uncounted anime titles

Seen while counting, not taken (no measurement supports them yet): `Ending Theme` (5 files),
`End Card` (2), `Ending: "<song>" by <artist>` (5). All would need the whole-title rule relaxed, which
is the rule that keeps "The Opening Night" from being an intro.

## 6. Reproducing

Scripts are committed beside Task 1's in `evidence/online/phase4/`; their data is git-ignored because
it holds real library paths. This page carries counts and titles only.

```bash
E=docs/design/intro-credits/evidence/online/phase4
PY=/home/data/.venv/bin/python
# Sonarr's series list (read-only GET) -> $E/sonarr_series_min.json; see evidence/README for credentials
nice -n 19 $PY $E/chapters_populations.py   # the three populations from the read-only parts dump
nice -n 19 $PY $E/chapters_probe.py         # ffprobe chapters, resumable, 8 threads, ~2 h
nice -n 19 $PY $E/chapters_census.py        # §1: the title census and the scope table
nice -n 19 $PY $E/chapters_score.py         # §2 and §4: baseline, every variant, and the shipped code
```

From a worktree, set `MARKERS_PARTS_DUMP` and `MARKERS_PLEX_DUMP` to the main checkout's phase-2 dumps
and `PYTHONPATH` to the worktree, or the scripts import the installed package instead of the one under
test.

## 7. What was not established

- **Whether the 146 anime answers Plex has no marker for are right.** They were not frame-checked. Four
  files were, all picked from the buckets where the rule looked worst: two confirmed the chapter is the
  credit roll (Plex was late), one confirmed the chapter is a "to be continued" card, and the movie
  confirmed the chapter is a scene.
- **Whether 110,211 non-anime TV episodes hold a bare `Ending` chapter anywhere.** The sample of
  11,919 found none. At that sample a rate above ~0.03% would have been likely to show.
- **Anime in the Movies library.** Anime films are out of the rule's scope by construction and were
  not counted separately.
- **Eleven of the 12 short anime answers.** All 12 are One Piece S21 and one was frame-checked; the
  other eleven were identified by length, not by frames.
