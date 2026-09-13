#!/bin/bash
# Throwaway Plex / Emby / Jellyfin 10.11 / Jellyfin 12.0 lab on `storage`, real library mounted READ-ONLY.
# State lives in docker volumes (mlab_*), so containers can be recreated without losing setup.
# Tokens/ids for these servers: ./env (chmod 600, never commit). Ports bind to 127.0.0.1 only.
#
#   ./up.sh            create any missing container
#   ./up.sh recreate   stop+remove and create all (volumes kept)
#
# Plex: unclaimed = no Plex Pass = markers are NOT served. To test Plex end to end, get a claim token from
# https://plex.tv/claim (valid 4 min) and run: PLEX_CLAIM=claim-xxxx ./up.sh recreate
set -euo pipefail

readonly HERE="$(cd "$(dirname "$0")" && pwd)"
MV=(-v "/data_16tb2/TV Shows/Rick and Morty (2013) {tvdb-275274}/Season 01:/media/tv/Rick and Morty (2013)/Season 01:ro"
    -v "/data_16tb/TV Shows/South Park (1997) {tvdb-75897}/Season 01:/media/tv/South Park (1997)/Season 01:ro"
    -v "/data_16tb/Movies/Toy Story (1995) {tmdb-862}:/media/movies/Toy Story (1995):ro"
    -v "/data_16tb/Movies/Up (2009) {tmdb-14160}:/media/movies/Up (2009):ro"
    -v "${HERE}/synth:/media/synth:ro")

if [[ "${1:-}" == "recreate" ]]; then
    for c in mlab-emby mlab-jellyfin mlab-jf12 mlab-plex; do
        docker rm -f "$c" >/dev/null 2>&1 || true
    done
fi

docker network create mlab >/dev/null 2>&1 || true
exists() { docker container inspect "$1" >/dev/null 2>&1; }

exists mlab-emby || docker run -d --name mlab-emby --network mlab -e UID=1000 -e GID=1000 \
    -p 127.0.0.1:18096:8096 -v mlab_emby_config:/config "${MV[@]}" emby/embyserver:latest
exists mlab-jellyfin || docker run -d --name mlab-jellyfin --network mlab --user 1000:1000 \
    -p 127.0.0.1:18097:8096 -v mlab_jf_config:/config -v mlab_jf_cache:/cache "${MV[@]}" jellyfin/jellyfin:10.11
exists mlab-jf12 || docker run -d --name mlab-jf12 --network mlab --user 1000:1000 \
    -p 127.0.0.1:18098:8096 -v mlab_jf12_config:/config -v mlab_jf12_cache:/cache "${MV[@]}" jellyfin/jellyfin:12.0
exists mlab-plex || docker run -d --name mlab-plex --network mlab -e TZ=UTC -e PLEX_UID=1000 -e PLEX_GID=1000 \
    -e "PLEX_CLAIM=${PLEX_CLAIM:-}" -p 127.0.0.1:32402:32400 -v mlab_plex_config:/config \
    -v mlab_plex_transcode:/transcode "${MV[@]}" plexinc/pms-docker:latest
docker ps --filter name=mlab- --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
