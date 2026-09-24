#!/bin/bash
# Prod Plex's intro/credits marker settings, server-wide and per library (read; `apply`/`movies` write them).
# Usage: plexprefs.sh show | keys | apply <section-key>... | movies | markers <ratingKey>...
# The token is read at run time from the Plex container's Preferences.xml over ssh, held in a shell variable and sent
# only as a request header: it is never printed, logged or written. Don't add `set -x` or `curl -v` here.
# Writing Plex's settings is a prod change: only with the owner's go-ahead (spec §0 working rules).
set -euo pipefail
readonly B="${PLEX_URL:-http://172.16.10.240:32400}"
T=$(ssh plex 'p=$(docker inspect plex --format "{{range .Mounts}}{{if eq .Destination \"/config\"}}{{.Source}}{{end}}{{end}}"); grep -oE "PlexOnlineToken=\"[^\"]+\"" "$p/Library/Application Support/Plex Media Server/Preferences.xml" | cut -d\" -f2')
api() { curl -sS -m 20 -X "$1" -H "X-Plex-Token: $T" -H "Accept: application/json" "$B$2"; }
show() {
  api GET "/:/prefs" | jq -r '.MediaContainer.Setting[] | select(.id|test("Generate(Intro|Credits)MarkerBehavior")) | "server \(.id)=\(.value)"'
  api GET "/library/sections" | jq -r '.MediaContainer.Directory[] | "\(.key)\t\(.type)\t\(.title)"' | while IFS=$'\t' read -r key type title; do
    api GET "/library/sections/$key/prefs" | jq -r --arg t "$title" '.MediaContainer.Setting[] | select(.id|test("enable(Intro|Credits)MarkerGeneration")) | "\($t): \(.id)=\(.value)"'
  done
}
case "${1:-show}" in
  markers) ;;
  show) show ;;
  movies) api PUT "/library/sections/1/prefs?enableCreditsMarkerGeneration=1" && show ;;
  keys) api GET "/library/sections" | jq -r ".MediaContainer.Directory[] | \"\(.key) \(.title)\"" ;;
  apply)
    api PUT "/:/prefs?GenerateIntroMarkerBehavior=never&GenerateCreditsMarkerBehavior=never" >/dev/null
    for key in "${@:2}"; do api PUT "/library/sections/$key/prefs?enableIntroMarkerGeneration=1&enableCreditsMarkerGeneration=1" >/dev/null; done
    show ;;
esac
# markers <ratingKey>...: print served markers per item
if [[ "${1:-}" == "markers" ]]; then
  for k in "${@:2}"; do
    api GET "/library/metadata/$k?includeMarkers=1" | jq -r --arg k "$k" '.MediaContainer.Metadata[0] | "\($k) \(.title): " + ([(.Marker // [])[] | "\(.type) \(.startTimeOffset/1000|floor)-\(.endTimeOffset/1000|floor)s"] | join(", "))'
  done
fi
