# Credit text deciding alone against never-alone sources — a measured proposal (not in the code)

`patch_rule.py` monkeypatches `decide._decide_from_single_source`: at Medium, credits proposed by on-screen credit
text decide alone when the only sources that disagree are ones that never decide credits alone (a server's own
marker, IntroDB/TheIntroDB), instead of going to Needs review. `decide.py` has no such rule: it was measured during
the credits text version 4 work and not taken.

| File | What |
|---|---|
| `patch_rule.py` | The proposed rule (`enable()` / `disable()`) |
| `eval_v2.py` | The 80 hand-checked files (movies40 + tv40) and the 205 movies: Plex, credit text alone, Medium today, the proposal, and the proposal with Plex's marker used where text found nothing (`judge_credits`). Input: the harness's per-file credits-text rows |
| `eval_online.py` | The 43 verified online cases at the app's three source settings, credit text added the way the pipeline adds it, with and without the proposal |

The run outputs weren't kept; the scripts ran from the session scratchpad against the harness caches
(`~/.cache/markers_eval`) and re-run in minutes once those are warm.
