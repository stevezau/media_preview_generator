#!/bin/bash
# This branch's app as `mlab-site-app`: it makes the open films' previews for the site's captures
# and benchmark. Separate from the marker lab's mlab-app (own config volume, own port 18083). The
# films are mounted read-WRITE here, because Emby BIFs and Jellyfin trickplay are written next to
# each video; the media servers mount the same folder read-only.
#
#   ./site_app.sh            create mlab-site-app if missing
#   ./site_app.sh recreate   remove and create it again (config volume kept), e.g. after a rebuild
#
# Build the image first, from the lane worktree root:  nice -n 19 docker build -t plex-previews:site-lab .
set -euo pipefail

readonly ENV_FILE="${MLAB_ENV:-/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env}"
readonly MEDIA_ROOT="${OPENFILMS_DIR:-/home/data/mlab-openfilms}"
readonly IMAGE="${SITE_APP_IMAGE:-plex-previews:site-lab}"
readonly NAME=mlab-site-app

[[ -f "$ENV_FILE" ]] || { echo "no lab env file at ${ENV_FILE}" >&2; exit 1; }
# Docker would create a missing bind source as an empty root-owned folder.
[[ -d "${MEDIA_ROOT}/Movies" ]] || { echo "no films folder at ${MEDIA_ROOT}/Movies (run openfilms.sh)" >&2; exit 1; }
if ! grep -q '^MLAB_SITE_APP_TOKEN=' "$ENV_FILE"; then
    printf 'MLAB_SITE_APP_TOKEN=%s\n' "$(openssl rand -hex 24)" >>"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "generated MLAB_SITE_APP_TOKEN in ${ENV_FILE}"
fi

if [[ "${1:-}" == "recreate" ]]; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
fi
if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "${NAME} already exists (use: $0 recreate)"
    exit 0
fi

# The token goes in through an env file, never the argv that `ps` shows every user on the host.
envfile="$(mktemp)"
trap 'rm -f "$envfile"' EXIT
chmod 600 "$envfile"
printf 'WEB_AUTH_TOKEN=%s\n' "$(sed -n 's/^MLAB_SITE_APP_TOKEN=//p' "$ENV_FILE" | tail -1)" >"$envfile"

docker volume create mlab_site_app_config >/dev/null
docker run --rm -v mlab_site_app_config:/config alpine chown 1000:1000 /config
docker run -d --name "$NAME" --network mlab -p 127.0.0.1:18083:8080 \
    -e PUID=1000 -e PGID=1000 -e TZ=UTC --env-file "$envfile" \
    --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri:/dev/dri \
    -v mlab_site_app_config:/config:nocopy -v mlab_plex_config:/plexcfg \
    -v "${MEDIA_ROOT}/Movies:/media/openfilms/Movies" \
    "$IMAGE" >/dev/null
echo "${NAME} on http://127.0.0.1:18083"
