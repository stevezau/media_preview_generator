# Media Preview Bridge — Jellyfin plugin

Tells Jellyfin about trickplay tiles that something else generated, so the scrubbing previews appear in the player **without Jellyfin running its own ffmpeg pass**, and lets that tool push **Skip Intro / Skip Credits** markers that Jellyfin serves as media segments. Built for the [Media Preview Generator](https://github.com/stevezau/media_preview_generator) tool, but the API is generic — anything that writes Jellyfin's trickplay tile format to disk, or knows where an intro starts and ends, can use it.

## What problem does this solve?

Jellyfin's built-in way to register new trickplay is gated by the per-library "Extract trickplay images during library scan" setting. If you turn that off (which you'd want to when an external tool is generating the tiles for you), Jellyfin never notices the tiles you wrote — they sit on disk and the player can't see them. This plugin gives external tools an HTTP endpoint they can call to register the tiles directly with Jellyfin, in one round trip.

Jellyfin has no API for writing media segments (intro, credits, recap, preview) either — its `/MediaSegments` endpoint is read-only, and it only serves segments that come from a registered segment provider. This plugin stores the markers you push and registers a segment provider named **Media Preview Bridge** that hands them to Jellyfin, so they show up immediately and survive library scans, metadata refreshes, restarts and the Media Segment Scan task.

## Install

In Jellyfin admin → Dashboard → Plugins → Repositories → **+** add:

```
https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json
```

Then go to Catalogue → install **Media Preview Bridge**. Restart Jellyfin.

## API

| Endpoint | Auth | What it does |
|---|---|---|
| `GET /MediaPreviewBridge/Ping` | anonymous | Returns `{plugin, version, ok:true, trickplayRoot, features}`. Use `ok` to detect whether the plugin is installed; `trickplayRoot` is Jellyfin's data-folder trickplay dir relative to the config root (e.g. `data/trickplay`) for off-media publishing; `features` lists what this build supports (`["trickplay", "markers"]` — older builds have no `features`). |
| `POST /MediaPreviewBridge/Trickplay/{itemId}?width=320&intervalMs=10000&saveWithMedia=true` | admin | Looks at the trickplay folder Jellyfin expects for this item (resolved via Jellyfin's own `GetTrickplayDirectory`, so it's version-proof), counts the tiles, and registers the resulting trickplay row with Jellyfin. Returns 204 on success, 404 if the item or tile folder isn't found. |
| `GET /MediaPreviewBridge/Markers/{itemId}` | admin | The markers stored for the item: `{itemId, fileSize, stale, segments: [{type, startTicks, endTicks}]}`. `fileSize` is left out when none was stored; `stale` is `true` when the stored `fileSize` differs from the file on disk (those markers are not served). 404 if the item doesn't exist. |
| `POST /MediaPreviewBridge/Markers/{itemId}` | admin | Replaces the item's markers and publishes them straight away. Body `{segments, fileSize?}`. Returns `{itemId, stored}`; 400 for a missing `segments`, more than 64 segments, an unsupported type, `endTicks <= startTicks`, a negative start, `fileSize <= 0` or a non-video item; 404 if the item doesn't exist. |
| `DELETE /MediaPreviewBridge/Markers/{itemId}` | admin | Removes the item's markers (only the ones this plugin published). Returns 204; 404 if the item doesn't exist. |

### `saveWithMedia` (default `true`)

Controls which layout the plugin reads — it must match where the caller wrote the tiles **and** the library's `SaveTrickplayWithMedia` option:

- `true` (default, back-compat) — tiles next to the media file:
  ```
  <media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>/<n>.jpg
  # e.g. /data/movies/Inception (2010)/Inception (2010).trickplay/320 - 10x10/0.jpg
  ```
- `false` — tiles in Jellyfin's data folder (keeps the media drive clean):
  ```
  <config>/data/trickplay/<id[..2]>/<id>/<width> - <tileW>x<tileH>/<n>.jpg
  # <id> = the item GUID in dashed lowercase form
  ```

Either way the plugin asks Jellyfin where the tiles live, so it always agrees with what the server reads at playback.

### Markers

```
POST /MediaPreviewBridge/Markers/{itemId}
Authorization: MediaBrowser Token="<admin API key>"
Content-Type: application/json

{"fileSize": 655778272,
 "segments": [
  {"type": "Intro", "startTicks": 1268000000, "endTicks": 1570000000},
  {"type": "Outro", "startTicks": 12953240000, "endTicks": 13214720000}
]}
```

- `type` is exactly `Intro`, `Outro` (credits), `Recap` or `Preview` (any letter case; numbers are rejected). Times are ticks (1 ms = 10,000 ticks). At most 64 segments.
- `segments` is required. Each POST replaces everything stored for the item; `{"segments": []}` clears it, same as `DELETE`. A body without `segments` is rejected rather than treated as "clear".
- `fileSize` (optional, bytes) is the size of the media file the markers were detected on. While the file on disk has a different size (it was replaced), the markers are not served and `GET` reports `stale: true`; push markers for the new file to fix that. Without `fileSize`, or when the file can't be read, the markers are always served.
- Errors are `{"error": "…"}` with status 400. A body that isn't valid JSON for this shape (for example ticks as text) gets ASP.NET's standard 400 validation response instead (415 when the `Content-Type` isn't JSON).
- Only the plugin's own segments change. Segments from other providers (chapter-based, Intro Skipper, …) are left alone.
- Markers are stored as one small JSON file per item in `<jellyfin-config>/plugins/Jellyfin.Plugin.MediaPreviewBridge/markers/`. A file that can't be read or doesn't hold valid markers is ignored (logged as a warning) and counts as no markers; the next POST replaces it.
- If the provider is switched off for a library (Library → Media Segment Providers), Jellyfin keeps the markers but doesn't serve them.
- On Jellyfin 12, a refresh that finds the media file changed deletes the item's stored markers along with its other extracted data; push them again for the new file.

## Required Jellyfin library options

The publisher should set these on each library it owns trickplay for:

| Option | Value | Why |
|---|---|---|
| `EnableTrickplayImageExtraction` | `true` | Must be on. Off = Jellyfin **deletes** trickplay directories on the next refresh. |
| `ExtractTrickplayImagesDuringLibraryScan` | `false` | Off = no per-item ffmpeg burn during library scans. |
| `SaveTrickplayWithMedia` | `true` (media-adjacent) / `false` (off-media) | On = Jellyfin reads from `<media_dir>/<basename>.trickplay/`. Off = Jellyfin reads from `<config>/data/trickplay/` — use this with `saveWithMedia=false` to keep the media drive clean. Must match the layout the publisher writes. |

The Media Preview Generator's "Disable vendor extraction" toggle on each Jellyfin server flips all three for you.

## Build locally

There is one build per Jellyfin release family, picked with `-p:JellyfinAbi` (default `10.11`):

```bash
cd jellyfin-plugin
# Jellyfin 10.11 (.NET 9)
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/src" -w /src mcr.microsoft.com/dotnet/sdk:9.0 \
    dotnet build -c Release -p:JellyfinAbi=10.11 -o out/10.11
# Jellyfin 12.0 (.NET 10) — clear obj/ first when switching ABI
rm -rf obj
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/src" -w /src mcr.microsoft.com/dotnet/sdk:10.0 \
    dotnet build -c Release -p:JellyfinAbi=12.0 -o out/12.0
```

Copy `out/<abi>/Jellyfin.Plugin.MediaPreviewBridge.dll` into `<jellyfin-config>/plugins/MediaPreviewBridge_<version>/` (`10.11.1.0` or `12.0.1.0`) and restart Jellyfin.

`<jellyfin-config>` is wherever Jellyfin stores its config:

- **Docker** — your mapped `/config` volume (e.g. `/mnt/user/appdata/jellyfin/config` on Unraid, `/var/lib/docker/volumes/jellyfin_config/_data` on a typical Linux Docker install).
- **Linux package install** — usually `/var/lib/jellyfin/`.
- **Windows** — `%ProgramData%\Jellyfin\Server\`.
- **macOS** — `~/.config/jellyfin/`.

## Compatibility

- **Jellyfin 10.11.x** — build `10.11.x.y`, targets .NET 9, compiled against `Jellyfin.Controller` 10.11.0.
- **Jellyfin 12.0.x** — build `12.0.x.y`, targets .NET 10, compiled against `Jellyfin.Controller` 12.0.0 (Jellyfin 12 added a required method to segment providers, so the 10.11 build can't serve markers there).
- The catalogue lists both with their `targetAbi`; Jellyfin installs the one that matches the server.
- Single small DLL, zero runtime configuration, no UI page.

## Release process

Tag with `plugin-v10.11.X.Y`. The CI workflow at `.github/workflows/jellyfin-plugin.yml` builds both DLLs (`10.11.X.Y` and `12.0.X.Y`), attaches both zips to one GitHub release, and publishes `manifest.json` (two `versions` entries, `targetAbi` `10.11.0.0` and `12.0.0.0`) to GitHub Pages — Jellyfin's plugin catalogue picks up the new version. Pull requests that touch the plugin build both ABIs in `.github/workflows/plugins-ci.yml`.
