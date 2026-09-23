#!/bin/bash
# Phase 4 lab row 10: what Setup Health needs beyond the lab Plex.
#
#   mlab-plex-nopass    a second, throwaway Plex that was never claimed, so it has no Plex Pass (the lab Plex is
#                       claimed and four other rows depend on that). It has no library: Setup Health reads its
#                       Plex Pass and its database, not its media.
#   mlab-plex-nopass-proxy
#                       nginx inside that Plex's network namespace, listening on 32499 (config inline below): an
#                       unclaimed Plex answers only a tokenless request from its own loopback and refuses every token,
#                       while the app insists on sending one, so the proxy re-sends each request without it.
#   mlab-app-health     an app container that sees the unclaimed Plex's config folder at /plexnp (its database is
#                       on this machine) and a COPY of the lab Plex's database at /plexcopy (a database file that is
#                       not the one the running Plex holds: what Setup Health means by "not on this machine").
#
#   ./phase4_health_up.sh              create whatever is missing
#   ./phase4_health_up.sh recreate     remove the three containers and create them again (volumes kept)
#   ./phase4_health_up.sh down         remove the three containers
#   ./phase4_health_up.sh refresh-copy copy the lab Plex's database into /plexcopy again
#
# MLAB_APP_IMAGE is the app image (default plex-previews:phase4-lab). MLAB_DIR sets the lab folder holding env and
# synth/ (default: this script's folder), as in up.sh. Ports bind to 127.0.0.1 only: Plex 32403, the app 18082.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LAB_DIR="${MLAB_DIR:-$SCRIPT_DIR}"
readonly ENV_FILE="${LAB_DIR}/env"
readonly APP_IMAGE="${MLAB_APP_IMAGE:-plex-previews:phase4-lab}"
readonly PLEX_IMAGE="plexinc/pms-docker:latest"
readonly DB_DIR="Library/Application Support/Plex Media Server/Plug-in Support/Databases"

export MLAB_MOUNTS_ONLY=1
# shellcheck source=up.sh
source "${SCRIPT_DIR}/up.sh"
unset MLAB_MOUNTS_ONLY

[[ -f "$ENV_FILE" ]] || { echo "no env file at ${ENV_FILE} (set MLAB_DIR to the lab folder)" >&2; exit 1; }

if ! grep -q '^MLAB_APP_HEALTH_TOKEN=' "$ENV_FILE"; then
    printf 'MLAB_APP_HEALTH_TOKEN=%s\n' "$(openssl rand -hex 24)" >>"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Generated MLAB_APP_HEALTH_TOKEN in ${ENV_FILE}" >&2
fi
APP_TOKEN="$(sed -n 's/^MLAB_APP_HEALTH_TOKEN=//p' "$ENV_FILE" | tail -1)"

exists() { docker container inspect "$1" >/dev/null 2>&1; }

copy_lab_database() {
    docker volume create mlab_p4h_plexcopy >/dev/null
    docker run --rm -v mlab_plex_config:/src:ro -v mlab_p4h_plexcopy:/dst alpine sh -c "
        set -eu
        mkdir -p '/dst/${DB_DIR}' '/dst/Library/Application Support/Plex Media Server/Media/localhost'
        cp '/src/${DB_DIR}/com.plexapp.plugins.library.db' '/dst/${DB_DIR}/'
        for suffix in -wal -shm; do
            [ -f '/src/${DB_DIR}/com.plexapp.plugins.library.db'\"\$suffix\" ] &&
                cp '/src/${DB_DIR}/com.plexapp.plugins.library.db'\"\$suffix\" '/dst/${DB_DIR}/' || true
        done
        chown -R 1000:1000 /dst"
}

case "${1:-}" in
    down | recreate)
        for c in mlab-app-health mlab-plex-nopass-proxy mlab-plex-nopass; do
            docker rm -f "$c" >/dev/null 2>&1 || true
        done
        [[ "$1" == "down" ]] && exit 0
        ;;
    refresh-copy)
        copy_lab_database
        exit 0
        ;;
esac

docker network create mlab >/dev/null 2>&1 || true

exists mlab-plex-nopass || docker run -d --name mlab-plex-nopass --network mlab -e TZ=UTC -e PLEX_UID=1000 \
    -e PLEX_GID=1000 -p 127.0.0.1:32403:32400 -v mlab_plex_nopass_config:/config \
    -v mlab_plex_nopass_transcode:/transcode "$PLEX_IMAGE" >/dev/null

# The proxy is the only change between the app and the unclaimed Plex: it asks Plex from Plex's own loopback with no
# token. Plex itself is real and unmodified, so its Plex Pass state and its database are what Setup Health reads.
readonly PROXY_CONF='server {
    listen 32499;
    location / {
        proxy_pass http://127.0.0.1:32400;
        proxy_http_version 1.1;
        proxy_set_header X-Plex-Token "";
        proxy_set_header Host 127.0.0.1:32400;
    }
}'
exists mlab-plex-nopass-proxy || docker run -d --name mlab-plex-nopass-proxy --network container:mlab-plex-nopass \
    -e "PROXY_CONF=${PROXY_CONF}" nginx:alpine \
    sh -c 'printf "%s\n" "$PROXY_CONF" >/etc/nginx/conf.d/default.conf && exec nginx -g "daemon off;"' >/dev/null

if ! exists mlab-app-health; then
    docker volume create mlab_app_health_config >/dev/null
    docker run --rm -v mlab_app_health_config:/config alpine chown 1000:1000 /config
    copy_lab_database
    docker run -d --name mlab-app-health --network mlab -p 127.0.0.1:18082:8080 \
        -e PUID=1000 -e PGID=1000 -e TZ=UTC -e "WEB_AUTH_TOKEN=${APP_TOKEN}" \
        -v mlab_app_health_config:/config:nocopy -v mlab_plex_nopass_config:/plexnp \
        -v mlab_p4h_plexcopy:/plexcopy "${MV[@]}" "$APP_IMAGE" >/dev/null
fi

for _ in $(seq 1 90); do
    if curl -fs -o /dev/null http://127.0.0.1:18082/api/health && curl -fs -o /dev/null http://127.0.0.1:32403/identity; then
        docker ps --filter name=mlab-app-health --filter name=mlab-plex-nopass --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
        exit 0
    fi
    sleep 2
done
echo "mlab-app-health or mlab-plex-nopass did not answer within 180 s; see docker logs" >&2
exit 1
