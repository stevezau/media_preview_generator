#!/bin/bash
# The "older" Media Preview Bridge builds phase 4 row 11 installs to show Setup Health's "plugin too old" row.
#
# "Outdated" means the installed plugin answers Ping but doesn't list the markers feature (publishers/jellyfin.py and
# emby.py look for it). Only Jellyfin 10.11 has a released build that predates markers (plugin-v10.11.0.3, downloaded
# as it was published). Jellyfin 12.0 and both Embys never shipped one, so their "older" build is the current source
# with the markers feature left out of what Ping advertises: the same one-word difference, built the way CI builds.
#
#   ./phase4_old_plugins.sh        writes $MLAB_DIR/plugins-old/{jf10.11,jf12.0,emby4.10,emby4.9}/ (git-ignored)
#
# Needs docker (dotnet SDK images 9.0 and 10.0) and gh. MLAB_DIR sets the lab folder (default: this script's folder).
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO="$(cd "${SCRIPT_DIR}/../../../../.." && pwd)"
readonly LAB_DIR="${MLAB_DIR:-$SCRIPT_DIR}"
readonly OUT="${LAB_DIR}/plugins-old"
readonly RELEASE="plugin-v10.11.0.3"
readonly OLD_JF12_VERSION="12.0.0.3"
readonly OLD_EMBY_VERSION="0.9.0.0"

WORK="$(mktemp -d)"
readonly WORK
trap 'rm -rf "$WORK"' EXIT

rm -rf "$OUT"
mkdir -p "$OUT/jf10.11" "$OUT/jf12.0" "$OUT/emby4.10" "$OUT/emby4.9"
cp -r "${REPO}/jellyfin-plugin" "${REPO}/emby-plugin" "$WORK/"
rm -rf "$WORK"/*/bin "$WORK"/*/obj

sed -i 's/features = new\[\] { "trickplay", "markers" }/features = new[] { "trickplay" }/' \
    "$WORK/jellyfin-plugin/Api/TrickplayBridgeController.cs"
sed -i 's/Features = new\[\] { "markers" }/Features = new string[0]/' "$WORK/emby-plugin/Api/BridgeService.cs"
grep -q 'features = new\[\] { "trickplay" }' "$WORK/jellyfin-plugin/Api/TrickplayBridgeController.cs"
grep -q 'Features = new string\[0\]' "$WORK/emby-plugin/Api/BridgeService.cs"

dotnet_build() {
    local image="$1" dir="$2"
    shift 2
    nice -n 19 docker run --rm --user 1000:1000 -e HOME=/tmp -e DOTNET_CLI_HOME=/tmp -e DOTNET_NOLOGO=1 \
        -v "${WORK}:/src" -w "/src/${dir}" "$image" dotnet build -c Release "$@"
}

dotnet_build mcr.microsoft.com/dotnet/sdk:10.0 jellyfin-plugin -p:JellyfinAbi=12.0 -p:Version="$OLD_JF12_VERSION" \
    -p:FileVersion="$OLD_JF12_VERSION" -p:AssemblyVersion="$OLD_JF12_VERSION"
cp "$WORK/jellyfin-plugin/bin/Release/net10.0/Jellyfin.Plugin.MediaPreviewBridge.dll" "$OUT/jf12.0/"

for abi in 4.10 4.9; do
    dotnet_build mcr.microsoft.com/dotnet/sdk:9.0 emby-plugin -p:EmbyAbi="$abi" -p:Version="$OLD_EMBY_VERSION" \
        -o "/src/out/${abi}"
    cp "$WORK/out/${abi}/MediaPreviewBridge.Emby.dll" "$OUT/emby${abi}/"
done

(cd "$REPO" && gh release download "$RELEASE" -p '*.zip' -D "$OUT/jf10.11" --clobber)
ls -la "$OUT"/*/
