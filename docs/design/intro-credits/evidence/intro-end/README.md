# An intro's end is this file's — evidence

Spec §5.5 rule 13, §14 2026-09-25 (integration of the five lanes). South Park S01: IntroDB 0–30 s and season audio
0.12–33.6 s agree within 5 s; IntroDB ranked first, so 30.0 s won, and the theme ends at about 35.5 s. Now, with a
source that reads the file (chapters, season audio, credit text) among the agreeing candidates, IntroDB and
TheIntroDB (or an importer's copy) don't supply an intro's or recap's end. Credits keep the source order.

## Headline numbers (decided from season audio, IntroDB and Plex's own markers, real frame rates)

| Set | Plex's own | Before | After |
|---|---|---|---|
| held-out 175 | 69 / 4 / 102 | 123 / 5 / 47 | **125 / 3 / 47** |
| lab 118 | 23 / 15 / 80 | 86 / 5 / 27 | **87 / 4 / 27** |
| Accused | 0 / 0 / 56 | 3 / 0 / 53 | 3 / 0 / 53 |
| Bones S05–S08 (Plex's markers flagged stale) | 41 / 41 / 0 | 82 / 0 / 0 | 82 / 0 / 0 |
| library chapter set (311 files, intro chapter as truth; no Plex markers: the plex host was down) | — | 224 / 8 / 20 | **225 / 8 / 19** |

On the chapter set, "chapters contradicted" goes 9 → 8 (Warrior S03E04, now decided by its chapter), chapters with
IntroDB agreeing stay 13, and no answer turns wrong.

## Still wrong on held-out 175 (after)

- **Alias S02E09** (single source, season audio): 881.6–897.5 s against the truth 881.0–905.9 s; the answer ends
  8.4 s early (wrong, doesn't skip story). No IntroDB entry and no Plex marker, so nothing else answers. A frame
  sheet was made in the session (`frames/alias_E09.png`, 0.8 MB, not kept). Open.
- South Park S01E10: truth 18–43 s; season audio and IntroDB agree on 0.1–33.9 s.
- Succession S04E01: wrong only through bad chapter truth; its frames show the answer useful.

## Files

| File | What |
|---|---|
| `dump_evidence.py` → `evidence_{lab118,heldout175,accused}.json` | Per file: season audio (the app's step, one speed), IntroDB's raw intro, Plex's markers, frame rate, truth |
| `redecide.py` | Re-decide a dumped set with the tree's `decide()` and list every wrong file with its candidates |
| `variants.py` → `verdicts_{base,file_edge}.json`; `diffv.py`, `stats.py`, `compare_alone.py` | The rule as a monkeypatched variant, the files it changes, and end errors |
| `dump_libchap.py` (`dump_libchap.log`) → `evidence_libchap.json`, `introdb_libchap.json` | The #310 library chapter set: chapter candidates, the season's intro-chapter limit, season audio, IntroDB (imdb ids from Sonarr, read-only), frame rate |
| `decide_libchap.py` | That set decided before (a verbatim copy of the old composition) and after (the tree's `decide()`; `--variant` for the proposed rule) |

JSON results and the log are local-only (they name library files). The scripts ran from the session scratchpad
against the integration worktree; their paths still point there.
