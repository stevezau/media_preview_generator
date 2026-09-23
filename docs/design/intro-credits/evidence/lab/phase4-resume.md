# Phase 4 lab: where it stands, and how to resume after the reboot

Written 2026-09-21 when the owner paused the work for a reboot of `storage`. Nothing was running and no row was
half-applied: the last matrix run had finished and every row's cleanup had run. No token or password is in this file.

## Where the work is

- Worktree: `/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab5e1345f03df1f55`, branch
  `worktree-agent-ab5e1345f03df1f55`, on top of `513efd3`.
- `97fa74e` `fix(markers): a save a server missed is reported failed and Check servers finds it`. The one product bug
  the lab found (row 7), with tests written first, Architecture Review done, full suite green. The images below are
  built from it.
- A WIP commit on top of it: the lab matrix (`phase4_matrix.py`; rows added to `phase1_matrix.py`, `phase2_matrix.py`,
  `phase3_matrix.py`), the lab scripts, `phase4-results.md` and this file. **Architecture Review was run twice on the
  matrix diff (no HIGH; its MED items are fixed) but the WIP commit itself is unreviewed, and the very last edits below
  were never re-run on the lab. The final commit needs the review again.**
- Everything the runs need is in the repo or in the main checkout's `evidence/lab/` (`env`, `results/`, `synth/`,
  `plugins-old/`, all git-ignored, all on disk). Nothing needed lives in `/tmp`.

## Rows

All 17 rows passed on the final images in the last full pass (01:00–01:20 UTC, 2026-09-21), and the results file
`phase4-results.md` has their counts (they are the counts from that pass). Result JSON is in
`<MLAB_DIR>/results/`, where `MLAB_DIR=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab`:

| Rows | Result files |
|---|---|
| Phase 4 rows 1–12 | `p4-row-01.json` … `p4-row-12.json` (row 12 also `phase4-row-12.json`, its nine steps) |
| Phase 1 rows 20, 21 | `row-20.json`, `row-21.json` |
| Phase 2 rows 25, 26 | `p2-row-25.json`, `p2-row-26.json` |
| Phase 3 row 17 | `p3-row-17.json` |

`./phase4_summary.py` (with `MLAB_DIR` set) prints one line per row from those files.

**Not run:** row 13, the phase 1–3 matrices re-run on the final image. That is the step after this one, by design.

**Edited after the last run and not yet re-run on the lab** (all cleanup or guard changes from the second review, plus the
default image tags of the row 12 scripts): row 8 (`only_enabled` restores every server, then raises if one failed), row 10
(the detection settings are restored in its cleanup list), row 11 (`restore_current` restarts even when the plugin is
already back; a missing install row is a failed check, not a crash), phase 2 row 25 (one cleanup step per Emby), and
`phase4_row12_up.sh` / `phase4_row12_agent.py` (defaults now `plex-previews:phase4-lab` and
`plex-marker-agent:phase4-lab`). Syntax and `ruff` pass; run rows 8, 10, 11, P2 25 and 12 before the final commit, or
simply the whole pass below.

## State of the lab containers

Every lab container has restart policy `no`: **none of them comes back on its own after the reboot.** Their volumes and
the two images are on disk and survive it.

| Container | Image | Port | State when paused | Changed by this work |
|---|---|---|---|---|
| `mlab-plex` | `plexinc/pms-docker:latest` | 127.0.0.1:32402 | up, claimed; detection settings `never`; marker tag rows as found | rows write marker rows into its database; nothing structural |
| `mlab-jellyfin` (10.11), `mlab-jf12` (12.0) | `jellyfin/jellyfin` | 18097, 18098 | up | restarted by row 7 (stop/start) and row 11 (plugin swap); an empty `/config/plugins-held` folder is left in each |
| `mlab-emby` (4.10), `mlab-emby49` (4.9) | `emby/embyserver` | 18096, 18099 | up | restarted by rows 6, 11 and P2 25 (stopped to edit `library.db`, plugin swap); an empty `/config/plugins-held` in each; no foreign marker rows left |
| `mlab-app` | `plex-previews:phase4-lab` | 18080 | up, GPU runtime, recreated on the final image, config volume `mlab_app_config` kept | recreated |
| `mlab-app-remote` | `plex-previews:phase4-lab` | 18081 | up, no access to Plex's config volume | recreated (row 12) |
| `mlab-plex-agent` | `plex-marker-agent:phase4-lab` | 19494 | up, next to the lab Plex with its config volume | recreated (row 12) |

Throwaway containers this work created (none touches `plex`, the production Plex host):

| Container | Image | Port | What it is |
|---|---|---|---|
| `mlab-plex-nopass` | `plexinc/pms-docker:latest` | 127.0.0.1:32403 | a second, never-claimed Plex for row 10, its own volumes `mlab_plex_nopass_config` and `mlab_plex_nopass_transcode` |
| `mlab-plex-nopass-proxy` | `nginx:alpine` | none (shares `mlab-plex-nopass`'s network namespace, listens on 32499) | re-sends the app's requests to that Plex without a token |
| `mlab-app-health` | `plex-previews:phase4-lab` | 127.0.0.1:18082 | an app that sees the unclaimed Plex's config folder and a copy of the lab Plex's database; volumes `mlab_app_health_config`, `mlab_p4h_plexcopy` |

Other volumes this work created: `mlab_app_remote_config`, `mlab_app_remote_plexcfg` (row 12), `mlab_other_plexcfg` (row
12's wrong-Plex step). Row 12's `skew` and `wrong_plex` steps start and remove their own containers
(`mlab-plex-agent-old`, `mlab-plex-agent-elsewhere`); none exists now.

Lab data at rest: the synth episodes are back on their chapters' answer on every server; Keep Plex's and Keep Emby's are
off; `publish_when` is `high`; Intro & Credits is on for every server; the staged episodes (S01E04–S01E06) are out of
the show folder. One real-library file (the one phase 1 row 16 and row 20 use) ends on whatever the last run decided; it
now includes TheIntroDB in its sources. The lab Plex's synth episode 1 has no marker rows after row 12's last step
(`cleaned_up`), while the app still records it as published; the next row's `settle` puts it right.

The images:

| Tag | Id |
|---|---|
| `plex-previews:phase4-lab` | `sha256:9e388619861d4c52f1945d5735bf284cff5f929425217d698523304dd88381bf` |
| `plex-marker-agent:phase4-lab` | `sha256:357e917c049943b07291efdd5119dbabf0ddf916e0f2e06ac631e26604229417` |

If `docker images` no longer shows them, build both again from commit `97fa74e` in the worktree (the ids will differ):

```bash
cd /home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab5e1345f03df1f55
nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION=4.4.2.dev232 -t plex-previews:phase4-lab .
nice -n 19 docker build -f plex-marker-agent/Dockerfile -t plex-marker-agent:phase4-lab .
```

## Bring the lab back after the reboot

```bash
cd /home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab5e1345f03df1f55/docs/design/intro-credits/evidence/lab
export MLAB_DIR=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab
export MLAB_APP_IMAGE=plex-previews:phase4-lab MLAB_AGENT_IMAGE=plex-marker-agent:phase4-lab

# 1. the five lab servers (same containers, same volumes); ./up.sh creates any that are missing
docker start mlab-plex mlab-jellyfin mlab-jf12 mlab-emby mlab-emby49

# 2. the app (add "recreate" if `docker start mlab-app` complains); its config volume is kept
MLAB_APP_GPU=nvidia ./app.sh

# 3. the row 12 pair and the row 10 trio, recreated because the proxy has to join the Plex's new network namespace
./phase4_row12_up.sh recreate
./phase4_health_up.sh recreate
```

Wait until `curl -s http://127.0.0.1:18080/api/health` answers and Emby and both Jellyfins report healthy (a minute or
two). Then check the lab: `docker exec mlab-app python3 -c "print('ok')"` and that `./phase4_summary.py` still prints the
old results.

## Resume

```bash
MLAB_PYTHON=/home/data/.venv/bin/python ./phase4_run_all.sh     # about 25 minutes; log in $MLAB_DIR/results/phase4-run.log
MLAB_DIR=$MLAB_DIR ./phase4_summary.py                          # every row should say pass
```

Then: update the counts in `phase4-results.md` if they moved, run the full unit suite and `ruff check . && ruff format
--check .`, dispatch the Architecture Review on the staged diff, and make the final commit. Rows must not be run in
parallel, and nothing here may touch the production `plex` host or anything under `/data*`.
