#!/bin/bash
# Throwaway Plex / Emby 4.10 + 4.9 / Jellyfin 10.11 / Jellyfin 12.0 lab on `storage`, real library mounted READ-ONLY.
# State lives in docker volumes (mlab_*), so containers can be recreated without losing setup.
# Tokens/ids for these servers: ./env (chmod 600, never commit). Ports bind to 127.0.0.1 only.
#
#   ./up.sh            create any missing container
#   ./up.sh recreate   stop+remove and create all (volumes kept)
#   docker rm -f mlab-jellyfin && ./up.sh   recreate one server (volumes kept)
#
# Plex: unclaimed = no Plex Pass = markers are NOT served. To test Plex end to end, get a claim token from
# https://plex.tv/claim (valid 4 min) and run: PLEX_CLAIM=claim-xxxx ./up.sh recreate
#
# MLAB_DIR sets the lab folder that holds env, synth/ and scale_mounts.sh (default: this script's folder). Containers
# outlive the checkout that created them, so a checkout elsewhere points MLAB_DIR at the long-lived lab folder.
set -euo pipefail

readonly HERE="${MLAB_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# Bind-mount sources must be absolute, and a lab folder without synth/ or env is the wrong folder: Docker would create
# the missing mount sources as empty root-owned folders.
if [[ "$HERE" != /* || ! -d "${HERE}/synth" || ! -f "${HERE}/env" ]]; then
    echo "not a lab folder: ${HERE} (needs an absolute path with synth/ and env; set MLAB_DIR)" >&2
    exit 1
fi
MV=(-v "/data_16tb2/TV Shows/Rick and Morty (2013) {tvdb-275274}/Season 01:/media/tv/Rick and Morty (2013)/Season 01:ro"
    -v "/data_16tb/TV Shows/South Park (1997) {tvdb-75897}/Season 01:/media/tv/South Park (1997)/Season 01:ro"
    -v "/data_16tb/Movies/Toy Story (1995) {tmdb-862}:/media/movies/Toy Story (1995):ro"
    -v "/data_16tb/Movies/Up (2009) {tmdb-14160}:/media/movies/Up (2009):ro"
    -v "${HERE}/synth/Synth Show (2020):/media/synth/Synth Show (2020):ro"
    -v "${HERE}/synth/Synth Chapters (2021):/media/synth-chapters/Synth Chapters (2021):ro"
    -v "${HERE}/synth/Synth Audio (2022):/media/synth-audio/Synth Audio (2022):ro"
    -v "${HERE}/synth/Synth Movie (2023):/media/synth-movies/Synth Movie (2023):ro"
    -v "${HERE}/synth/Synth Credits (2024):/media/synth-credits/Synth Credits (2024):ro"
    -v "${HERE}/synth/Synth Credits Open (2025):/media/synth-credits/Synth Credits Open (2025):ro")
# A second location of the Synth Chapters library, for Plex only: a version of an episode the app can't read (phase 2
# version-drift row). The app must never see it, so app.sh doesn't get it.
PLEXONLY=(-v "${HERE}/synth/_plexonly:/media/plexonly:ro")
# Phase 1 scale run (Task 20 Step 4): real seasons and movies with truth, picked by `./scale_score.py pick`, which writes
# the mounts to scale_mounts.sh (git-ignored: it lists real library folders; results/scale/pick.json has each folder's
# reason). Plex and both Jellyfins get them; Emby doesn't (its Intro & Credits is off until the phase 2 plugin, and
# scanning them there would only burn CPU on the shared host). Without the file the scale folders aren't mounted.
MV_SCALE=()
if [[ -f "${HERE}/scale_mounts.sh" ]]; then
    # shellcheck source=/dev/null
    source "${HERE}/scale_mounts.sh"
fi
# Synth folders are mounted one show at a time: synth/_staging (files a test adds later) must stay invisible, and each
# show belongs to one library only (synth = Synth Show, synth-chapters = synth_chapters.sh output, synth-audio =
# synth_audio.sh output, synth-movies = the two-version movie from synth_chapters.sh, synth-credits =
# synth_credits.sh output (two movies, one library)). Run synth_chapters.sh, synth_audio.sh and synth_credits.sh
# before this script: Docker creates a missing bind source as an empty root-owned folder.
# app.sh sources this list (MLAB_MOUNTS_ONLY=1) so the app sees every file at the servers' paths.
# MLAB_MOUNTS_ONLY=1 works only when sourced (`return`); run directly, it is not set.
[[ "${MLAB_MOUNTS_ONLY:-}" == "1" ]] && return 0

if [[ "${1:-}" == "recreate" ]]; then
    for c in mlab-emby mlab-emby49 mlab-jellyfin mlab-jf12 mlab-plex; do
        docker rm -f "$c" >/dev/null 2>&1 || true
    done
fi

docker network create mlab >/dev/null 2>&1 || true
exists() { docker container inspect "$1" >/dev/null 2>&1; }

exists mlab-emby || docker run -d --name mlab-emby --network mlab -e UID=1000 -e GID=1000 \
    -p 127.0.0.1:18096:8096 -v mlab_emby_config:/config "${MV[@]}" emby/embyserver:4.10.0.40
exists mlab-emby49 || docker run -d --name mlab-emby49 --network mlab -e UID=1000 -e GID=1000 \
    -p 127.0.0.1:18099:8096 -v mlab_emby49_config:/config "${MV[@]}" emby/embyserver:4.9.1.90
exists mlab-jellyfin || docker run -d --name mlab-jellyfin --network mlab --user 1000:1000 \
    -p 127.0.0.1:18097:8096 -v mlab_jf_config:/config -v mlab_jf_cache:/cache "${MV[@]}" "${MV_SCALE[@]}" jellyfin/jellyfin:10.11
exists mlab-jf12 || docker run -d --name mlab-jf12 --network mlab --user 1000:1000 \
    -p 127.0.0.1:18098:8096 -v mlab_jf12_config:/config -v mlab_jf12_cache:/cache "${MV[@]}" "${MV_SCALE[@]}" jellyfin/jellyfin:12.0
exists mlab-plex || docker run -d --name mlab-plex --network mlab -e TZ=UTC -e PLEX_UID=1000 -e PLEX_GID=1000 \
    -e "PLEX_CLAIM=${PLEX_CLAIM:-}" -p 127.0.0.1:32402:32400 -v mlab_plex_config:/config \
    -v mlab_plex_transcode:/transcode "${MV[@]}" "${PLEXONLY[@]}" "${MV_SCALE[@]}" plexinc/pms-docker:latest
docker ps --filter name=mlab- --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
