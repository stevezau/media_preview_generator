#!/bin/bash
# The branch's app as `mlab-app`, wired to the lab servers from up.sh (spec §10.3). Its own config volume
# (mlab_app_config), the lab media at the servers' paths (read-only), and the lab Plex's config volume at /plexcfg so
# the Plex DB is on the same host. UI/API: http://127.0.0.1:18080, token MLAB_APP_TOKEN in ./env.
#
#   ./app.sh            create mlab-app if missing
#   ./app.sh recreate   remove and create it again (volume kept), e.g. after rebuilding the image
#
# Build the image first (from the repo root):
#   nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$VER" -t media_preview_generator:intro-credits .
# Then configure it with ./phase1_matrix.py configure (phase 2: ./phase2_matrix.py configure).
#
# MLAB_DIR sets the lab folder that holds env and synth/ (default: this script's folder); see up.sh.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LAB_DIR="${MLAB_DIR:-$SCRIPT_DIR}"
readonly IMAGE="${MLAB_APP_IMAGE:-media_preview_generator:intro-credits}"
readonly ENV_FILE="${LAB_DIR}/env"

export MLAB_MOUNTS_ONLY=1
# shellcheck source=up.sh
source "${SCRIPT_DIR}/up.sh"
unset MLAB_MOUNTS_ONLY

[[ -f "$ENV_FILE" ]] || { echo "no env file at ${ENV_FILE} (set MLAB_DIR to the lab folder)" >&2; exit 1; }
if ! grep -q '^MLAB_APP_TOKEN=' "$ENV_FILE"; then
    printf 'MLAB_APP_TOKEN=%s\n' "$(openssl rand -hex 24)" >>"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Generated MLAB_APP_TOKEN in ${ENV_FILE}"
fi
MLAB_APP_TOKEN="$(sed -n 's/^MLAB_APP_TOKEN=//p' "$ENV_FILE" | tail -1)"

if [[ "${1:-}" == "recreate" ]]; then
    docker rm -f mlab-app >/dev/null 2>&1 || true
fi

docker network create mlab >/dev/null 2>&1 || true
if ! docker container inspect mlab-app >/dev/null 2>&1; then
    # The app runs as PUID:PGID and the image doesn't chown /config. `nocopy`: an empty volume would otherwise take
    # the image's root-owned /config again on every new container.
    docker volume create mlab_app_config >/dev/null
    docker run --rm -v mlab_app_config:/config alpine chown 1000:1000 /config
    docker run -d --name mlab-app --network mlab -p 127.0.0.1:18080:8080 \
        -e PUID=1000 -e PGID=1000 -e TZ=UTC -e WEB_AUTH_TOKEN="$MLAB_APP_TOKEN" \
        -v mlab_app_config:/config:nocopy -v mlab_plex_config:/plexcfg \
        "${MV[@]}" "${MV_SCALE[@]}" "$IMAGE" >/dev/null
fi

for _ in $(seq 1 60); do
    if curl -fs -o /dev/null http://127.0.0.1:18080/api/health; then
        docker ps --filter name=mlab-app --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
        exit 0
    fi
    sleep 2
done
echo "mlab-app did not answer /api/health within 120 s; see: docker logs mlab-app" >&2
exit 1
