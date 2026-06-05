# Design: Jellyfin trickplay off the media drive (data-folder support)

**Status:** Draft / design-only — not started. Opened as a draft PR to action later.
**Raised by:** Discussion [#264](https://github.com/stevezau/media_preview_generator/discussions/264) (RedRubble)
**Label:** enhancement

## Problem

Today this tool only writes Jellyfin trickplay **next to the media file**
(`<media_dir>/<basename>.trickplay/...`), which requires the per-library flag
`SaveTrickplayWithMedia = true`. Some users want the media drive kept clean and
prefer trickplay on a separate cache/config drive — exactly what Plex does, and
exactly what Jellyfin supports natively when `SaveTrickplayWithMedia = false`.

This is a real Jellyfin capability we simply don't write to. **Goal:** let a
Jellyfin server be configured so this tool's output lands in Jellyfin's data
folder instead of beside the media.

## Current behaviour (code references)

- `media_preview_generator/output/jellyfin_trickplay.py` — output path is
  derived **purely from `canonical_path`**; `compute_output_paths` does
  `del server, item_id`, and `needs_server_metadata()` returns `False`.
  Layout: `<media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>/<n>.jpg`.
- `media_preview_generator/processing/multi_server.py:188` builds the adapter
  with `JellyfinTrickplayAdapter(width=..., frame_interval=...)` — no location
  option.
- Registration: `jellyfin-plugin/Api/TrickplayBridgeController.cs:159-166`
  **hardcodes** the media-adjacent path and registers via
  `ITrickplayManager.SaveTrickplayInfo`.
- Health-check already names the location it never writes to:
  `<config>/data/trickplay/` (`servers/jellyfin.py:646`,
  `docs/guides/previews-readiness.md:113-115`).

## How Jellyfin actually stores & registers trickplay (verified against source)

### The off-media path formula (10.11)

From `PathManager.GetTrickplayDirectory` (release-10.11.z,
[Emby.Server.Implementations/Library/PathManager.cs](https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Emby.Server.Implementations/Library/PathManager.cs)):

```csharp
public string GetTrickplayDirectory(BaseItem item, bool saveWithMedia = false)
{
    var id = item.Id.ToString("D", CultureInfo.InvariantCulture).AsSpan();
    return saveWithMedia
        ? Path.Combine(item.ContainingFolderPath, Path.ChangeExtension(Path.GetFileName(item.Path), ".trickplay"))
        : Path.Join(_config.ApplicationPaths.TrickplayPath, id[..2], id);
}
```

So the **off-media** directory is:

```
<ApplicationPaths.TrickplayPath>/<guid[0:2]>/<guid>/<width> - <tileW>x<tileH>/<n>.jpg
```

where `guid` is the item GUID in **"D" (dashed, lowercase) form**, `guid[0:2]`
is a 2-char shard, and the `<width> - <tileW>x<tileH>` resolution sub-dir is
appended by the caller (same format string this app already uses,
`jellyfin_trickplay.py:154`).

`ApplicationPaths.TrickplayPath` resolves to **`<config>/data/trickplay/`** in
10.11. ⚠️ **This path moved between versions** — 10.10 stored it under
`<config>/metadata/library/<hash>/trickplay/<resolution>`; 10.11 moved it to
`<config>/data/trickplay/<hash>/<resolution>`
([jellyfin#11747](https://github.com/jellyfin/jellyfin/issues/11747), and the
migration itself has been buggy:
[#15414](https://github.com/jellyfin/jellyfin/issues/15414),
[#12703](https://github.com/jellyfin/jellyfin/issues/12703),
[#15426](https://github.com/jellyfin/jellyfin/issues/15426)). **This version
coupling is the core reason to keep path computation inside the plugin** (see
Approach B) rather than replicate it in this app.

### Registration — and why we can't lean on Jellyfin's scan

`TrickplayManager.RefreshTrickplayDataAsync` reads
`saveWithMedia = libraryOptions.SaveTrickplayWithMedia` and **will adopt
pre-existing tiles on disk** without re-running ffmpeg: when tiles exist but no
DB row does, it creates a `TrickplayInfo` and saves it.

**But that native adoption path is buggy:** it sets
`ThumbnailCount = existingFiles.Length` (the *sheet* count) instead of the
individual-thumbnail count, producing a broken HLS playlist — Jellyfin issue
[#12887](https://github.com/jellyfin/jellyfin/issues/12887). The Media Preview
Bridge plugin exists precisely to register with the **correct** thumbnail count
(`TrickplayBridgeController.cs:195-203`). **Conclusion: off-media support must
go through the plugin too** — relying on Jellyfin's own scan adoption would
reintroduce the broken-playlist bug.

## Approach B — let the plugin place + register (recommended)

The plugin runs *inside* Jellyfin, so it can call
`IPathManager.GetTrickplayDirectory(item, saveWithMedia)` and get the correct
path for **any** version, immune to the 10.10→10.11 move. It already computes
`ThumbnailCount` correctly. We extend it rather than teach this app Jellyfin's
internal layout.

### Plugin changes (C#)
- Inject `IPathManager` (currently it injects `ILibraryManager` +
  `ITrickplayManager` only and **hardcodes** the media path,
  `TrickplayBridgeController.cs:159-166`).
- Replace the hardcoded `containingFolder / basename.trickplay / ...` with
  `_pathManager.GetTrickplayDirectory(item, saveWithMedia)` + the
  `<width> - <tileW>x<tileH>` sub-dir. This **also makes the existing
  media-adjacent path version-proof** — a free robustness win.
- Add a `saveWithMedia` query param (default `true` for back-comat) to
  `POST /MediaPreviewBridge/Trickplay/{itemId}`. When `false`, the plugin reads
  tiles from the app-written staging location and writes/relocates them into the
  data-folder path before registering. (Decide: does the app write straight into
  the data folder and the plugin only registers, or does the plugin do the
  move? The plugin-does-the-move variant means the app never needs to know the
  version-specific path — preferred.)
- Requires Jellyfin **10.11+** (already the plugin's floor).

### App changes (Python)
- Add a per-Jellyfin-server output option, e.g. `output.save_with_media: bool`
  (default `true`). Persisted in `media_servers[].output`.
- `JellyfinTrickplayAdapter`: thread the flag through. In off-media mode the
  publish flow hands tiles to the plugin's `saveWithMedia=false` endpoint after
  writing. If the plugin-does-the-move variant is chosen, the adapter still
  writes to a temp/staging dir and the path stays app-agnostic.
- **Plugin becomes required** for off-media mode. Gate the toggle on
  plugin-installed; surface a clear error otherwise.

### Server config / health-check (`servers/jellyfin.py`)
- When a server is set to off-media, the recommended `SaveTrickplayWithMedia`
  value flips to **`false`** (currently hardcoded to `True` at
  `servers/jellyfin.py:2308`, `:2432`), and **plugin-installed** becomes a hard
  requirement rather than a recommendation.
- Add a readiness check that Jellyfin's config dir is mounted **read-write**
  into this container (analogous to the Plex config-mount check).

### UI
- Toggle on the Jellyfin server card / Setup Health tab with an ⓘ tooltip
  explaining the trade-off: off-media keeps the media drive clean but requires
  the plugin **and** a RW config-dir mount.

### Viewer / orphan sweep (audit)
- BIF Viewer trickplay endpoints (`web/routes/api_bif.py`) and
  `JellyfinTrickplayAdapter.list_orphans_in_folder` currently assume the
  media-adjacent layout — both need an off-media branch (orphan detection in the
  data folder is keyed by GUID, not basename, so the sweep strategy differs).

## Rejected — Approach A (app writes the data folder directly)

App replicates Jellyfin's GUID→path scheme itself (now fully known, see formula
above). Works plugin-less, **but**:
- Version-coupled (the path moved in 10.11 and may move again).
- Still needs the item GUID (so `needs_server_metadata()` → `True`, slow-backoff
  retry like Plex) **and** a RW config mount.
- Would still need correct `ThumbnailCount` registration to avoid #12887, i.e.
  the plugin anyway — so it buys nothing over B except fragility.

## Estimated effort

~2 days: plugin (C#) path/param change + app-side flag + Jellyfin
server-config/health-check/UI updates, plus tests:
- adapter matrix: `save_with_media` true vs false
- health-check recommended-value flip + plugin-required gate when off-media
- RW-mount readiness check
- viewer + orphan sweep off-media branches

## Open questions
1. **Who moves the tiles** — app writes data-folder path directly (needs version
   awareness) vs plugin relocates from a staging dir (app stays version-agnostic,
   **preferred**)?
2. **Migration** of existing media-adjacent tiles when a user flips a server to
   off-media: leave them (orphans) or offer a one-click move?
3. Is `ApplicationPaths.TrickplayPath` ever relocatable by the admin? If so the
   plugin (which asks Jellyfin) is the only safe source — another point for B.

## Acceptance criteria
- A Jellyfin server can be configured for off-media trickplay.
- With the plugin installed and config dir mounted RW, generated tiles land in
  Jellyfin's data folder and scrubbing previews appear with a **correct** HLS
  playlist (no #12887 regression).
- Health check guides the user to the right `SaveTrickplayWithMedia` value and
  flags the plugin + RW-mount prerequisites.
- Media-adjacent mode (today's default) is unchanged.

## Sources
- Jellyfin `PathManager.GetTrickplayDirectory` (release-10.11.z) — https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Emby.Server.Implementations/Library/PathManager.cs
- Jellyfin `TrickplayManager` (release-10.11.z) — https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Jellyfin.Server.Implementations/Trickplay/TrickplayManager.cs
- Trickplay path moved 10.10→10.11 — https://github.com/jellyfin/jellyfin/issues/11747
- Broken ThumbnailCount on import-existing (#12887) — https://github.com/jellyfin/jellyfin/issues/12887
- Save-with-media migration bugs — https://github.com/jellyfin/jellyfin/issues/15414 , https://github.com/jellyfin/jellyfin/issues/12703 , https://github.com/jellyfin/jellyfin/issues/15426
