# Lab servers on `storage`

Throwaway Plex, Emby and Jellyfin servers (plus a few app instances) used for proofs during
development, kept between sessions because Plex's claim and each server's library setup take
time to rebuild. `docs/design/` is excluded from the site build, so this page never ships.

All state lives in named Docker volumes (`mlab_*`), so a container can be removed and recreated
without losing its config — but see the warning below before doing that.

## Containers (2026-10-10)

Every container's name starts `mlab-`. Ports bind to `127.0.0.1` only.

| Container | Image | Host -> container port | Volumes | Notes |
|---|---|---|---|---|
| `mlab-plex` | `plexinc/pms-docker:latest` | 32402 -> 32400 | `mlab_plex_config`, `mlab_plex_transcode` | Claimed lab Plex |
| `mlab-emby` | `emby/embyserver:4.10.0.40` | 18096 -> 8096 | `mlab_emby_config` | Lab Emby, with the Media Preview Bridge plugin and an "Open Films" library |
| `mlab-emby49` | `emby/embyserver:4.9.1.90` | 18099 -> 8096 | `mlab_emby49_config` | Older Emby, for version checks |
| `mlab-jellyfin` | `jellyfin/jellyfin:10.11` | 18097 -> 8096 | `mlab_jf_config`, `mlab_jf_cache` | Lab Jellyfin 10.11, with the plugin and "Open Films" |
| `mlab-jf12` | `jellyfin/jellyfin:12.0` | 18098 -> 8096 | `mlab_jf12_config`, `mlab_jf12_cache` | Jellyfin 12 with the 12.x plugin; its libraries are the real library `:ro`, no "Open Films" |
| `mlab-app` | `plex-previews:captions` | 18080 -> 8080 | `mlab_app_config_captions`, `mlab_plex_config` (at `/plexcfg`) | The app, wired to the servers above |
| `mlab-app-remote` | `plex-previews:final-dd911c0` | 18081 -> 8080 | `mlab_app_remote_config`, `mlab_app_remote_plexcfg` | App with no access to `mlab-plex`'s config, so it reaches Plex through `mlab-plex-agent` |
| `mlab-plex-agent` | `plex-marker-agent:release-check` | 19494 -> 9494 | `mlab_plex_config` (at `/plexcfg`) | The Plex marker agent, next to `mlab-plex`'s config |
| `mlab-plex-nopass` | `plexinc/pms-docker:latest` | 32403 -> 32400 | `mlab_plex_nopass_config`, `mlab_plex_nopass_transcode` | Unclaimed Plex (no Plex Pass), for Setup Health's Plex Pass checks. Stopped |
| `mlab-plex-nopass-proxy` | `nginx:alpine` | shares `mlab-plex-nopass`'s network, listens on 32499 | none | Strips `X-Plex-Token` so tokened requests reach the unclaimed Plex. Stopped |
| `mlab-app-health` | `plex-previews:phase4-lab` | 18082 -> 8080 | `mlab_app_health_config`, `mlab_plex_nopass_config` (at `/plexnp`), `mlab_p4h_plexcopy` | Setup Health's "database not on this machine" case. Stopped |
| `mlab-site-app` | `plex-previews:site-lab` | 18083 -> 8080 | `mlab_site_app_config`, `mlab_plex_config` (at `/plexcfg`) | Generated the docs-site screenshots and benchmark. Stopped |

For a one-off proof, start a new app container (name it `mlab-<purpose>-<date>`) with its own new
config volume, and remove it and its volume when the proof is done. Real-library mounts are `:ro`.

## Media

- **Open Films** (`/home/data/lab-media/open-films`, also at `/home/data/mlab-openfilms/Movies`):
  sample films that `mlab-plex`, `mlab-emby` and `mlab-jellyfin` have as a library. It is the only
  writable media in the lab, so Emby BIFs and Jellyfin trickplay can be written next to the videos.
- **Real library** (`/data_16tb*`): always mounted `:ro`, used for Intro & Credits on real episodes.
- **Docs-site screenshots:** `docs/design/site-redesign-lab/lab_setup.py` and `tests/e2e/snapshots/` drive
  `mlab-site-app` against the Open Films library.
- The synthetic clips (`docs/design/intro-credits/evidence/lab/synth/`) and the lab scripts
  (`up.sh`, `app.sh`, `phase*`) were removed in #378. `mlab-app`, `mlab-app-remote` and
  `mlab-app-health` still bind-mount the old synth paths, so starting them makes Docker create empty
  root-owned folders there; delete those afterwards. To read an old script:
  `git show b758a022^:docs/design/intro-credits/evidence/lab/<file>`.

## Tokens

Each app's server keys and tokens live in its own `settings.json` (for example in the
`mlab_app_config_captions` and `mlab_app_config` volumes). Read them from a read-only mount, e.g.
`docker run --rm -v mlab_app_config:/c:ro alpine cat /c/settings.json`, inside a script. Never
print a token or copy one into a commit, a doc, or chat output.

## Starting and stopping

```bash
# Stop, keep volumes.
docker stop mlab-plex mlab-emby mlab-emby49 mlab-jellyfin mlab-jf12 mlab-app mlab-app-remote mlab-plex-agent

# Start again. The Setup Health extras: mlab-plex-nopass before mlab-plex-nopass-proxy (shared network).
docker start mlab-plex mlab-emby mlab-emby49 mlab-jellyfin mlab-jf12 mlab-app mlab-app-remote mlab-plex-agent
```

**Never `docker rm -v` these containers or remove their volumes.** The named volumes hold a
claimed Plex server and each server's library and plugin setup. Several containers share
`mlab_plex_config`, so check what shares a volume before replacing a container.
