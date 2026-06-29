# Media Preview Bridge — Jellyfin plugin

Tells Jellyfin about trickplay tiles that something else generated, so the scrubbing previews appear in the player **without Jellyfin running its own ffmpeg pass**. Built for the [Media Preview Generator](https://github.com/stevezau/media_preview_generator) tool, but the API is generic — anything that writes Jellyfin's trickplay tile format to disk can use it.

## What problem does this solve?

Jellyfin's built-in way to register new trickplay is gated by the per-library "Extract trickplay images during library scan" setting. If you turn that off (which you'd want to when an external tool is generating the tiles for you), Jellyfin never notices the tiles you wrote — they sit on disk and the player can't see them. This plugin gives external tools an HTTP endpoint they can call to register the tiles directly with Jellyfin, in one round trip.

## Install

In Jellyfin admin → Dashboard → Plugins → Repositories → **+** add:

```
https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json
```

Then go to Catalogue → install **Media Preview Bridge**. Restart Jellyfin.

## API

| Endpoint | Auth | What it does |
|---|---|---|
| `GET /MediaPreviewBridge/Ping` | anonymous | Returns `{plugin, version, ok:true, trickplayRoot}`. Use `ok` to detect whether the plugin is installed; `trickplayRoot` is Jellyfin's data-folder trickplay dir relative to the config root (e.g. `data/trickplay`) for off-media publishing. |
| `POST /MediaPreviewBridge/Trickplay/{itemId}?width=320&intervalMs=10000&saveWithMedia=true` | admin | Looks at the trickplay folder Jellyfin expects for this item (resolved via Jellyfin's own `GetTrickplayDirectory`, so it's version-proof), counts the tiles, and registers the resulting trickplay row with Jellyfin. Returns 204 on success, 404 if the item or tile folder isn't found. |

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

## Required Jellyfin library options

The publisher should set these on each library it owns trickplay for:

| Option | Value | Why |
|---|---|---|
| `EnableTrickplayImageExtraction` | `true` | Must be on. Off = Jellyfin **deletes** trickplay directories on the next refresh. |
| `ExtractTrickplayImagesDuringLibraryScan` | `false` | Off = no per-item ffmpeg burn during library scans. |
| `SaveTrickplayWithMedia` | `true` (media-adjacent) / `false` (off-media) | On = Jellyfin reads from `<media_dir>/<basename>.trickplay/`. Off = Jellyfin reads from `<config>/data/trickplay/` — use this with `saveWithMedia=false` to keep the media drive clean. Must match the layout the publisher writes. |

The Media Preview Generator's "Disable vendor extraction" toggle on each Jellyfin server flips all three for you.

## Build locally

```bash
cd jellyfin-plugin
docker run --rm -v "$PWD:/src" -w /src mcr.microsoft.com/dotnet/sdk:9.0 \
    dotnet build -c Release
```

The DLL lands at `bin/Release/net9.0/Jellyfin.Plugin.MediaPreviewBridge.dll`. Drop it into `<jellyfin-config>/plugins/MediaPreviewBridge_<version>/` and restart Jellyfin.

`<jellyfin-config>` is wherever Jellyfin stores its config:

- **Docker** — your mapped `/config` volume (e.g. `/mnt/user/appdata/jellyfin/config` on Unraid, `/var/lib/docker/volumes/jellyfin_config/_data` on a typical Linux Docker install).
- **Linux package install** — usually `/var/lib/jellyfin/`.
- **Windows** — `%ProgramData%\Jellyfin\Server\`.
- **macOS** — `~/.config/jellyfin/`.

## Compatibility

- Requires Jellyfin **10.11.x** or newer (uses internal trickplay-registration APIs that were added in Jellyfin 10.10).
- Targets **.NET 9** (matches Jellyfin 10.11's runtime).
- Single small DLL, zero runtime configuration, no UI page.

## Release process

Tag with `plugin-vX.Y.Z.W` (matching the Jellyfin version family). The CI workflow at `.github/workflows/jellyfin-plugin.yml` builds the DLL, attaches the zip to a GitHub release, and updates `manifest.json` on the `gh-pages` branch — Jellyfin's plugin catalogue auto-detects the new version.
