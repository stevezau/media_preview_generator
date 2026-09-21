#!/bin/bash
# Phase 4 lab row 12: the two containers the Plex marker agent needs.
#
#   mlab-plex-agent   the agent, beside the lab Plex, with Plex's config volume mounted
#   mlab-app-remote   the app, with the lab media and NO access to Plex's config volume (its /plexcfg is an
#                     empty volume of its own, so Plex's database file is not on any path it can see)
#
# That is the whole point of the row: the app container cannot open Plex's database, so every marker it writes has
# to go through the agent. It is a second app container on purpose — the phase 1-3 `mlab-app` keeps its /plexcfg
# mount and its results.
#
#   ./phase4_row12_up.sh              create whatever is missing
#   ./phase4_row12_up.sh recreate     remove both and create them again (volumes kept)
#   ./phase4_row12_up.sh down         remove both
#
# Images (override with MLAB_APP_IMAGE / MLAB_AGENT_IMAGE):
#   docker build -t plex-previews:phase4-lab .
#   docker build -f plex-marker-agent/Dockerfile -t plex-marker-agent:phase4-lab .
#
# MLAB_DIR sets the lab folder holding env and synth/ (default: this script's folder), as in up.sh.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LAB_DIR="${MLAB_DIR:-$SCRIPT_DIR}"
readonly ENV_FILE="${LAB_DIR}/env"
readonly APP_IMAGE="${MLAB_APP_IMAGE:-plex-previews:phase4-lab}"
readonly AGENT_IMAGE="${MLAB_AGENT_IMAGE:-plex-marker-agent:phase4-lab}"
readonly PLEX_CONFIG_IN_AGENT="/plexcfg/Library/Application Support/Plex Media Server"

export MLAB_MOUNTS_ONLY=1
# shellcheck source=up.sh
source "${SCRIPT_DIR}/up.sh"
unset MLAB_MOUNTS_ONLY

[[ -f "$ENV_FILE" ]] || { echo "no env file at ${ENV_FILE} (set MLAB_DIR to the lab folder)" >&2; exit 1; }

# One generated secret per key, kept in ./env (chmod 600, never committed) exactly like MLAB_APP_TOKEN.
ensure_secret() {
    local key="$1"
    if ! grep -q "^${key}=" "$ENV_FILE"; then
        printf '%s=%s\n' "$key" "$(openssl rand -hex 24)" >>"$ENV_FILE"
        chmod 600 "$ENV_FILE"
        # stderr: this function's stdout IS the secret, read by the caller through $(...).
        echo "Generated ${key} in ${ENV_FILE}" >&2
    fi
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -1
}

AGENT_TOKEN="$(ensure_secret MLAB_AGENT_TOKEN)"
APP_TOKEN="$(ensure_secret MLAB_APP_REMOTE_TOKEN)"

if [[ "${1:-}" == "recreate" || "${1:-}" == "down" ]]; then
    for c in mlab-app-remote mlab-plex-agent mlab-plex-agent-old; do
        docker rm -f "$c" >/dev/null 2>&1 || true
    done
    [[ "${1:-}" == "down" ]] && exit 0
fi

docker network create mlab >/dev/null 2>&1 || true
exists() { docker container inspect "$1" >/dev/null 2>&1; }

# The agent runs as the user that owns Plex's database (the lab Plex runs with PLEX_UID=1000).
exists mlab-plex-agent || docker run -d --name mlab-plex-agent --network mlab --user 1000:1000 \
    -e "AGENT_TOKEN=${AGENT_TOKEN}" -e "PLEX_CONFIG_DIR=${PLEX_CONFIG_IN_AGENT}" -e TZ=UTC \
    -p 127.0.0.1:19494:9494 -v mlab_plex_config:/plexcfg "$AGENT_IMAGE" >/dev/null

if ! exists mlab-app-remote; then
    docker volume create mlab_app_remote_config >/dev/null
    docker run --rm -v mlab_app_remote_config:/config alpine chown 1000:1000 /config
    # An EMPTY volume at /plexcfg, not Plex's. The app still needs a Plex config folder (previews write BIF files
    # into one, and the job config validation requires it), and a user whose Plex is on another machine has one —
    # a network mount, or as here a folder that simply doesn't hold Plex's live database. What it never has is a
    # readable, lockable com.plexapp.plugins.library.db: that is what the agent is for.
    docker volume create mlab_app_remote_plexcfg >/dev/null
    # Media/ so the app's own config validation recognises it as a Plex folder (previews write BIF files there);
    # no Plug-in Support/Databases, because the database is what this app must not be able to reach.
    docker run --rm -v mlab_app_remote_plexcfg:/plexcfg alpine sh -c \
        'mkdir -p "/plexcfg/Library/Application Support/Plex Media Server/Media/localhost" && chown -R 1000:1000 /plexcfg'
    docker run -d --name mlab-app-remote --network mlab -p 127.0.0.1:18081:8080 \
        -e PUID=1000 -e PGID=1000 -e TZ=UTC -e "WEB_AUTH_TOKEN=${APP_TOKEN}" \
        -v mlab_app_remote_config:/config:nocopy -v mlab_app_remote_plexcfg:/plexcfg \
        "${MV[@]}" "$APP_IMAGE" >/dev/null
fi

for _ in $(seq 1 60); do
    if curl -fs -o /dev/null http://127.0.0.1:18081/api/health && curl -fs -o /dev/null http://127.0.0.1:19494/v1/health; then
        docker ps --filter name=mlab-app-remote --filter name=mlab-plex-agent --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
        exit 0
    fi
    sleep 2
done
echo "mlab-app-remote or mlab-plex-agent did not answer within 120 s; see docker logs" >&2
exit 1
