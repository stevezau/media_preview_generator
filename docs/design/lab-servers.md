# Lab servers on `storage`

Throwaway Plex, Emby and Jellyfin servers (plus a few app instances) used for proofs during
development, kept between sessions because Plex's claim and each server's library setup take
time to rebuild. `docs/design/` is excluded from the site build, so this page never ships.

All state lives in named Docker volumes (`mlab_*`), so a container can be removed and recreated
without losing its config — but see the warning below before doing that.

## Containers (2026-09-24)

Every container's name starts `mlab-`. Ports bind to `127.0.0.1` only.

| Container | Image | Host -> container port | Notes |
|---|---|---|---|
| `mlab-plex` | `plexinc/pms-docker:latest` | 32402 -> 32400 | Claimed lab Plex; the intro/credits matrix and the docs-site screenshots both use it |
| `mlab-emby` | `emby/embyserver:4.10.0.40` | 18096 -> 8096 | |
| `mlab-emby49` | `emby/embyserver:4.9.1.90` | 18099 -> 8096 | Older Emby, for the version-matrix rows |
| `mlab-jellyfin` | `jellyfin/jellyfin:10.11` | 18097 -> 8096 | |
| `mlab-jf12` | `jellyfin/jellyfin:12.0` | 18098 -> 8096 | |
| `mlab-app` | `plex-previews:phase4-lab` | 18080 -> 8080 | The app, wired to the servers above (`up.sh`, `app.sh`) |
| `mlab-plex-nopass` | `plexinc/pms-docker:latest` | 32403 -> 32400 | Unclaimed Plex (no Plex Pass), no library, for Setup Health's Plex Pass checks |
| `mlab-plex-nopass-proxy` | `nginx:alpine` | shares `mlab-plex-nopass`'s network namespace, listens on 32499 | Strips `X-Plex-Token` so the app's tokened requests still reach the unclaimed Plex |
| `mlab-app-health` | `plex-previews:phase4-lab` | 18082 -> 8080 | Sees `mlab-plex-nopass`'s config at `/plexnp` and a copy of `mlab-plex`'s database at `/plexcopy`, for Setup Health's "database not on this machine" case |
| `mlab-plex-agent` | `plex-marker-agent:phase4-lab` | 19494 -> 9494 | The Plex marker agent, next to `mlab-plex`'s config |
| `mlab-app-remote` | `plex-previews:phase4-lab` | 18081 -> 8080 | App with no access to `mlab-plex`'s config volume, so it must reach Plex through `mlab-plex-agent` |
| `mlab-site-app` | `plex-previews:site-lab` | (none published; used via its own config volume) | Generates the open-films previews for the docs-site screenshots and the benchmark; separate config volume and image from `mlab-app` |
| `mlab-plex-pre-openfilms` | `plexinc/pms-docker:latest` | exited | Stopped snapshot, kept for rollback. **Shares `mlab-plex`'s `mlab_plex_config`/`mlab_plex_transcode` volumes** — never start it while `mlab-plex` is running |
| `mlab-jellyfin-pre-openfilms` | `jellyfin/jellyfin:10.11` | exited | Same pattern, shares `mlab-jellyfin`'s `mlab_jf_config`/`mlab_jf_cache` volumes |
| `mlab-emby-pre-openfilms` | `emby/embyserver:4.10.0.40` | exited | Same pattern, shares `mlab-emby`'s `mlab_emby_config` volume |

## Which lab uses which containers

- **Intro & Credits lab** (`docs/design/intro-credits/evidence/lab/`, see `up.sh` and the
  `phase*.sh`/`phase*.py` scripts there): `mlab-plex`, `mlab-emby`, `mlab-emby49`,
  `mlab-jellyfin`, `mlab-jf12`, `mlab-app`, plus the Setup Health and marker-agent extras
  (`mlab-plex-nopass`, `mlab-plex-nopass-proxy`, `mlab-app-health`, `mlab-plex-agent`,
  `mlab-app-remote`) from `phase4_health_up.sh` and `phase4_row12_up.sh`.
- **Docs-site screenshots** (`docs/design/site-redesign-lab/`, see `lab_setup.py` and
  `site_app.sh`): reuses `mlab-plex`, `mlab-jellyfin` and `mlab-emby` with an added "Open Films"
  library, plus its own `mlab-site-app`. The films live at `/home/data/mlab-openfilms/Movies`, a
  symlink to `/home/data/lab-media/open-films`. Capture and comparison scripts are in
  `tests/e2e/snapshots/` (e.g. `lab_players.py`) and `scripts/` (e.g. `benchmark_previews.py`).

## Tokens

Every generated token (app tokens, the marker agent token, Plex claim state) lives in
`docs/design/intro-credits/evidence/lab/env`, which is gitignored and `chmod 600`. Both labs read
from that same file. Never copy a token out of it into a commit, a doc, or chat output.

## Starting and stopping

```bash
docker stop mlab-plex mlab-emby mlab-emby49 mlab-jellyfin mlab-jf12 mlab-app ...   # stop, keep volumes
docker start mlab-plex mlab-emby mlab-emby49 mlab-jellyfin mlab-jf12 mlab-app ...  # start again
```

**Never `docker rm` these containers.** Their named volumes hold a claimed Plex server, each
server's library setup, and (for the `-pre-openfilms` set) state shared with a live container.
Removing a container is fine if its replacement reuses the same volume names deliberately (as
`up.sh` and the `phase4_*` scripts do); removing one without checking what shares its volumes is
not.

## The real library

Every bind mount of the real library (paths under `/data_16tb*`) is `:ro` in every lab container.
The only read-write media mount in the lab is `mlab-site-app`'s open-films folder, which is
sample content, not the user's library — Emby and Jellyfin write their BIFs/trickplay next to the
video there, exactly as they would in production.
