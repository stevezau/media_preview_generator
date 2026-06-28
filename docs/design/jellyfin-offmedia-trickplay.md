# Design: Jellyfin trickplay off the media drive (data-folder support)

**Status:** Accepted — implementing. Approach below supersedes the original draft.
**Raised by:** Discussion [#264](https://github.com/stevezau/media_preview_generator/discussions/264) (RedRubble)
**Label:** enhancement

## Problem

Today this tool only writes Jellyfin trickplay **next to the media file**
(`<media_dir>/<basename>.trickplay/...`), which requires the per-library flag
`SaveTrickplayWithMedia = true`. Some users want the media drive kept clean and
prefer trickplay on a separate cache/config drive — exactly what Plex does, and
exactly what Jellyfin supports natively when `SaveTrickplayWithMedia = false`.

**Goal:** let a Jellyfin server be configured so this tool's output lands in
Jellyfin's data folder (`<config>/data/trickplay/...`) instead of beside the media.

## Chosen approach (verified against Jellyfin `release-10.11.z` source)

> **The app writes tiles straight into Jellyfin's data folder; the plugin only
> *registers* them (its job today, minus a hard-coded path). Nobody moves files.**

This is simpler and more robust than the draft's "plugin relocates from a staging
dir" idea, which would have required cross-volume copy+delete in C#
(`Directory.Move` throws across mounts — [MS docs](https://learn.microsoft.com/en-us/dotnet/api/system.io.directory.move)),
risked the same temp-leak bug Jellyfin itself has ([#15426](https://github.com/jellyfin/jellyfin/issues/15426)),
touched the media drive transiently, and needed the plugin to report paths back.

Writing directly: the plugin diff is ~15 lines, the media drive is never touched
(works with a read-only media mount), and the app already knows the path it wrote
so the freshness check and viewer "just work".

### Verified facts (release-10.11.z)

| Fact | Source |
|---|---|
| Off-media dir = `Path.Join(TrickplayPath, id[..2], id)`, `id` = GUID "D" (dashed, lowercase) | [PathManager.cs#L79](https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Emby.Server.Implementations/Library/PathManager.cs#L79) |
| `TrickplayPath = <ProgramDataPath>/data/trickplay` → `/config/data/trickplay`; `DataPath`/`ProgramDataPath` byte-stable across 10.10/10.11/master | [BaseApplicationPaths.cs#L79](https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Emby.Server.Implementations/AppBase/BaseApplicationPaths.cs#L79) |
| `ITrickplayManager.GetTrickplayDirectory(item, tileW, tileH, width, saveWithMedia)` returns the full leaf incl. the `"{0} - {1}x{2}"` subdir (already injected in the controller) | [TrickplayManager.cs#L669](https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Jellyfin.Server.Implementations/Trickplay/TrickplayManager.cs#L669) |
| Serve-time resolves the path via `SaveTrickplayWithMedia` **library option** → tiles must be at the `false` location, option must be `false`, `ThumbnailCount` must be the true frame count | [TrickplayController.cs#L95](https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Jellyfin.Api/Controllers/TrickplayController.cs#L95) |
| Jellyfin's scan-adoption sets `ThumbnailCount = sheetCount` (wrong) → broken HLS playlist; must register via `SaveTrickplayInfo` with the correct count | [#12887](https://github.com/jellyfin/jellyfin/issues/12887), TrickplayManager.cs#L299 |
| `IApplicationPaths` / `ITrickplayManager` are DI singletons, injectable into the plugin controller | [CoreAppHost.cs#L89](https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Jellyfin.Server/CoreAppHost.cs#L89), [ApplicationHost.cs#L520](https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Emby.Server.Implementations/ApplicationHost.cs#L520) |

### Version-proofing

The off-media path is `<config>/data/trickplay/<id[:2]>/<id>/<W> - 10x10/`. Only the
`data/trickplay` prefix is version-specific; it is stable for all supported versions
(plugin floor is 10.11+). The plugin's `Ping` response also reports the live
config-relative trickplay root, which the app caches and prefers over its built-in
default — so if Jellyfin ever moves the folder, the app follows automatically, and the
plugin's registration is *always* correct because it calls Jellyfin's own
`GetTrickplayDirectory(...)`.

## How it fits the existing pipeline (multi-server, frame cache, BIF reuse)

### Extract once, fan out
For one physical file the dispatcher finds every owning server, runs **FFmpeg once**
into a shared `frame_dir`, then hands that same dir to each server's adapter, which
writes its own format (Plex bundle BIF · Emby `.bif` sidecar · Jellyfin `.trickplay`
tile sheets). N owning servers = N adapters, **1 FFmpeg pass**.

### Two reuse layers
- **Frame cache** (`frame_cache.py`): caches the extracted JPGs keyed by
  `sha256(canonical_path)`, default 1 h TTL, persists across jobs. A later
  webhook/scan for the same path gets a `cache_hit` and skips FFmpeg.
- **Sibling-BIF reuse** (`_try_reuse_existing_bif`, only when a file has >1 publisher):
  if a Plex/Emby sibling already has a fresh `.bif` on disk, it's unpacked back to JPGs
  and reused. Jellyfin emits tile sheets, **not** a BIF, so a Jellyfin output can never
  be the *source* of this reuse — but Jellyfin happily **consumes** reused frames.

`frame_source` per publisher: `extracted` (FFmpeg ran) · `cache_hit` (frames reused) ·
`output_existed` (this publisher's output already on disk + fresh, used no frames).

### Where off-media changes things — and where it doesn't
Off-media touches **only the Jellyfin adapter's private decisions**; everything shared
is untouched:

| Concern | Media-adjacent (today) | Off-media (new) | Shared? |
|---|---|---|---|
| FFmpeg extraction | one pass | same | ✅ unchanged |
| Frame cache | path-keyed, 1 h | same | ✅ unchanged |
| Sibling-BIF reuse | Jellyfin consumes frames | same | ✅ unchanged |
| Tile destination | `<media>/<name>.trickplay/…` | `<jellyfin-config>/data/trickplay/<id[:2]>/<id>/…` | ❌ per-adapter |
| Needs item GUID? | no (`needs_server_metadata=False`) | **yes** (`True`) → reuses Plex's retry | ❌ per-adapter |
| Plugin call | `…?saveWithMedia=true` | `…?saveWithMedia=false` | ❌ per-adapter |

Off-media tiles are keyed by Jellyfin's **item GUID**, not the media basename, so the
adapter needs the GUID before it can compute its path → `needs_server_metadata()`
flips to `True` in off-media mode, reusing the **existing** Plex-style item-id
resolution + slow-backoff retry (60s→1h) for not-yet-indexed items. No new retry code.

### Walkthrough — one file in Plex + Jellyfin-A (media-adjacent) + Jellyfin-B (off-media)
```
1. find_owning_servers(Movie.mkv) → [Plex, Jelly-A, Jelly-B]   (3 publishers)
2. all_fresh probe → none fresh → generate
3. frame source: cache miss, no sibling BIF → FFmpeg ONCE → frame_dir (00001.jpg…); cache.put
4. per-publisher loop, all reading the SAME frame_dir:
   - Plex    → pack BIF → /plex-config/.../index-sd.bif ; refresh
   - Jelly-A → tiles → <media>/Movie.trickplay/320 - 10x10/*.jpg ; POST …?width=320          (saveWithMedia default true)
   - Jelly-B → tiles → /jelly-b-config/data/trickplay/a1/a1b2…/320 - 10x10/*.jpg ;
               POST …?width=320&saveWithMedia=false → plugin reads, computes correct ThumbnailCount, SaveTrickplayInfo
   - Jelly-B library option SaveTrickplayWithMedia=false → Jellyfin serves from the data folder
→ 1 FFmpeg pass, 3 outputs; Jelly-A and Jelly-B are identical tiles in different dirs, zero interference.
```
Second webhook 10 min later → all outputs fresh → `SKIPPED` (no FFmpeg). If only Jelly-B
was missing → `cache_hit` (no FFmpeg), only Jelly-B publishes. If Jelly-B hasn't indexed
the file yet → `SKIPPED_NOT_IN_LIBRARY`/`NOT_INDEXED` → slow-backoff retry; the other
servers publish immediately.

## Implementation

### Plugin (C#)
- `RegisterTrickplay`: add `[FromQuery] bool saveWithMedia = true` (default = today's
  behavior, fully back-compat). Replace the hard-coded media-adjacent path with
  `_trickplayManager.GetTrickplayDirectory(item, tileWidth, tileHeight, width, saveWithMedia)`.
- `Ping`: also return `trickplayRoot` (= `Path.GetRelativePath(ProgramDataPath, TrickplayPath)`),
  injecting `IApplicationPaths`.
- Bump `<Version>` 10.11.0.2 → .3; rebuild via Docker; update plugin README API table.
- **Not released here** — off-media needs a `plugin-v*` release + plugin update on the
  user's Jellyfin. Media-adjacent mode needs no plugin change.

### App (Python)
- `output.save_with_media: bool` (default `true`) and `output.jellyfin_config_folder: str`
  per Jellyfin server (free-form `output` dict, round-trips automatically).
- `JellyfinTrickplayAdapter`: thread both through; `needs_server_metadata()` → `True`
  off-media; compute/publish/staging root at the config-dir GUID path; normalise the item
  GUID to dashed-lowercase "D" form for the `<id[:2]>/<id>` shard.
- `JellyfinServer`: cache the plugin's `trickplayRoot` from the `Ping` probe; expose it for
  the adapter (default `data/trickplay`).
- Health-check: off-media flips recommended `SaveTrickplayWithMedia` → `false`, makes the
  plugin a hard requirement, and adds a config-dir **RW-mount** readiness check (mirrors
  Plex's `os.access(W_OK)` probe — read-only, no test-write).
- Viewer (`api_bif.py`): add `jellyfin_config_folder` to allowed roots; off-media branch
  computes the config-dir sheet dir.
- UI: Jellyfin-only "Store trickplay off the media drive" toggle + config-folder field +
  ⓘ tooltips + a warning when enabled.

### Scope
- Core feature fully built + tested. Off-media orphan cleanup via the **safe** path
  (explicit Radarr/Sonarr deletion webhooks); the full GUID-vs-live-server reconciliation
  sweep is a tracked follow-up (a failed server query must never delete valid tiles).
- Flipping a server to off-media leaves existing media-adjacent tiles in place (documented).

## Acceptance criteria
- A Jellyfin server can be configured for off-media trickplay.
- With the plugin installed and config dir mounted RW, generated tiles land in Jellyfin's
  data folder and scrubbing previews appear with a **correct** HLS playlist (no #12887).
- Health check guides the user to `SaveTrickplayWithMedia=false` and flags the plugin +
  RW-mount prerequisites.
- Media-adjacent mode (today's default) is unchanged.

## Sources
- `PathManager.GetTrickplayDirectory`, `BaseApplicationPaths`, `TrickplayManager`,
  `TrickplayController` (release-10.11.z, linked above).
- Path move 10.10→10.11 & migration bugs: [#11747](https://github.com/jellyfin/jellyfin/issues/11747),
  [#12703](https://github.com/jellyfin/jellyfin/issues/12703),
  [#15414](https://github.com/jellyfin/jellyfin/issues/15414),
  [#15426](https://github.com/jellyfin/jellyfin/issues/15426).
- Broken ThumbnailCount on import-existing: [#12887](https://github.com/jellyfin/jellyfin/issues/12887).
