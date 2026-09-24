---
title: Setup Health
heading: Setup Health
description: What each Setup Health check means and how to fix it, so Plex, Emby and Jellyfin show the preview thumbnails
  this app generates.
---

> [Back to Guides](../guides.md) · [Configuration & API Reference](../reference.md) · [Multi-Media-Server Guide](../multi-server.md)

The **Setup Health** tab on the Edit Server modal is the single
place to verify — and adjust — every server-side setting that affects
whether this app's previews show up in Plex / Emby / Jellyfin.

Each row lives in one of three sections:

1. **Server status** — connection, version, plugin presence.
2. **Library settings** — per-library (or server-wide for Plex) flags.
3. **Advanced** — server trickplay geometry, vendor extraction, path
   mappings, Plex config folder writability.

A server with **Intro & Credits** switched on also gets an
[Intro & Credits section](#intro-credits) (Plex) or plugin rows (Jellyfin, Emby).

Every row carries an ⓘ tooltip (the one-liner), a direct link to
**this page** anchored at the relevant check, and — where applicable —
an **Enable** or **Disable** toggle that applies immediately.

> [!WARNING]
> A handful of toggles are **data-destructive**. The UI surfaces a
> typed-confirmation dialog for those cases. The danger is real: flipping
> `EnableTrickplayImageExtraction` off makes Jellyfin delete the
> `.trickplay/` directory this app published on its next library
> refresh. Read the section below before clicking.

---

## Connection  <a id="connection"></a>

**What it checks:** the configured URL + credentials reach the media
server and return an identity response (Plex `machineIdentifier`,
Emby/Jellyfin `/System/Info`).

**Why it matters:** every other check depends on this working. A red
row here almost always means the URL is wrong, the credential
expired, or the container can't see the server.

**Enable / disable:** read-only check — fix the URL or credential in
Server settings.

**Verify:** re-open the Edit Server modal and click the refresh icon
next to the badge.

---

## Server version  <a id="version"></a>

**What it checks:** Jellyfin must be 10.10 or newer; Plex and Emby
are informational (any recent release works).

**Why it matters:** pre-10.10 Jellyfin ignores the
`SaveTrickplayWithMedia` flag and looks for trickplay under
`<config>/data/trickplay/`, which this app never writes to. Upgrade is
the only fix.

**Enable / disable:** read-only — upgrade via your container / package
manager.

---

## Media Preview Bridge plugin  <a id="plugin"></a>
*Jellyfin only*

**What it checks:** the Media Preview Bridge plugin responds on
`GET /MediaPreviewBridge/Ping`.

**Why it matters:** with the plugin, new previews become visible to
Jellyfin **instantly** (Mode A). Without it, previews are adopted on
the next library scan or Jellyfin's daily 3 AM task (Mode B) — still
works, just slower to appear.

**Enable:** one-click **Install plugin**. The app adds its manifest
URL to Jellyfin's plugin repos, queues the install, and restarts
Jellyfin. Takes ~30 s; the Setup Health card polls until the plugin is
live.

**Disable:** **Uninstall plugin** (with confirm). Removes the package
and restarts Jellyfin. Published tiles stay on disk and are
re-discovered by the next library scan — no data loss.

---

## Library settings  <a id="library-settings"></a>

Per-library (or server-wide for Plex) flags governing preview
generation, scan behaviour, and trickplay adoption.

### Trickplay enabled (EnableTrickplayImageExtraction)  <a id="enable-trickplay"></a>
*Jellyfin only*

**What it checks:** Jellyfin's master trickplay gate on each library.

**Why it matters:** off makes Jellyfin **delete** the `.trickplay/`
directory this app published on the next library refresh
(`TrickplayManager.RefreshTrickplayDataInternal` prunes what it
considers orphaned data). This is the most destructive flag in the
system.

**Enable:** one-click **Enable**. Flips the flag to `true` for this
library.

**Disable:** **requires typing `disable trickplay`** to confirm. Do
not click through the dialog — Jellyfin will delete every preview
tile this app has generated for the library. You'll need to re-run
the generator to restore them.

---

### Save trickplay with media (SaveTrickplayWithMedia)  <a id="save-trickplay-with-media"></a>
*Jellyfin only*

**What it checks:** that Jellyfin looks for trickplay where this app
writes it. The recommended value depends on the server's **Store
trickplay off the media drive** setting:

- **Off (default):** recommend **on** — Jellyfin reads from
  `<media>.trickplay/`, where the app writes beside each video.
- **On (off-media):** recommend **off** — Jellyfin reads from
  `<config>/data/trickplay/`, the data folder the app writes into.

**Why it matters:** a mismatch means published tiles sit on disk but are
invisible to Jellyfin. Files aren't deleted — just unreachable until the
flag matches the layout the app is writing.

**Enable / disable:** toggle via the inline **Enable** / **Disable**
buttons. Disable shows a click-to-confirm dialog (non-destructive but
breaks visibility).

---

### Store trickplay off the media drive (off-media)  <a id="jellyfin-config-folder"></a>
*Jellyfin only — shown when the server's "Store trickplay off the media drive" toggle is on*

**What it is:** instead of writing trickplay next to each video, the app
writes it into Jellyfin's data folder
(`<config>/data/trickplay/<id[:2]>/<id>/<width> - 10x10/`), exactly like
Plex keeps its previews. This keeps the media drive clean and works even
when the media is mounted read-only.

**What it checks:** that the **Jellyfin config folder** you set is mounted
into *this* container **read-write**. It's a pure read-only probe —
`os.access(W_OK)` only, no test write. States: `writable` (good),
`missing` (path not mounted here), `read-only` (mounted `:ro` or wrong
PUID/PGID), `unset` (no folder configured).

**Requirements (all three):**

1. The **Media Preview Bridge plugin** installed in Jellyfin (it's the
   only thing that registers off-media tiles with the correct thumbnail
   count). The plugin section becomes a hard requirement when off-media is on.
2. Jellyfin's config dir bind-mounted read-write into this container, with
   the **Jellyfin config folder** field pointing at that mount.
3. `SaveTrickplayWithMedia` **off** for the libraries (Setup Health flips
   the recommendation for you).

**How to fix:** set the mount in your Docker config (e.g.
`-v /path/to/jellyfin/config:/jellyfin-config`, not `:ro`) and enter that
container path in the server's **Jellyfin config folder** field.

---

### Scan-time extraction (ExtractTrickplayImagesDuringLibraryScan)  <a id="scan-extraction"></a>
*Jellyfin + Emby*

**What it checks:** vendor's scan-time trickplay generation flag.

**Why it matters:**

- **With the Media Preview Bridge plugin installed (Mode A):**
  recommend **off**. The plugin registers previews directly; scan-time
  extraction is wasted CPU.
- **Plugin absent (Mode B):** recommend **on**. Jellyfin's
  `TrickplayProvider` only adopts existing tiles on scan when this
  flag is on. Off without the plugin means adoption stalls until the
  3 AM daily task.
- **Emby:** recommend **off**. Emby has no plugin mode; its scan-time
  extraction just repeats this app's work.

**Enable / disable:** toggle directly. Disable while in Mode B shows a
click-to-confirm dialog.

---

### Chapter-image extraction (ExtractChapterImagesDuringLibraryScan)  <a id="chapter-extraction"></a>
*Emby only*

**What it checks:** Emby's older preview pipeline that predates
trickplay.

**Why it matters:** when this app owns trickplay, chapter-image
extraction is wasted CPU. Disabling it doesn't affect anything this
app publishes.

**Enable / disable:** toggle directly. Non-destructive either way.

---

### Real-time monitor (EnableRealtimeMonitor)  <a id="realtime-monitor"></a>
*Emby + Jellyfin*

**What it checks:** vendor's filesystem watcher that auto-detects new
files without waiting for a manual scan.

**Why it matters:** off means Sonarr/Radarr imports only get noticed
on the next manual scan or webhook nudge — the "not in library yet"
state hangs around longer than necessary.

**Enable / disable:** toggle directly. Non-destructive either way.

---

### FSEvent library updates (FSEventLibraryUpdatesEnabled)  <a id="fsevent-updates"></a>
*Plex only — server-wide*

**What it checks:** Plex's filesystem event subscription in `Settings
→ Library`.

**Why it matters:** off = Plex never reacts to filesystem changes.
Your only signals for new files become this app's scan-nudges and
Plex's periodic timer. Most "why didn't Plex pick up the file?"
complaints trace back here.

**Enable / disable:** toggle directly. Server-wide setting (not
per-library).

---

### FSEvent partial scan (FSEventLibraryPartialScanEnabled)  <a id="fsevent-partial"></a>
*Plex only — server-wide*

**What it checks:** when on, Plex only re-scans the directory that
changed; off = full library scan per added file.

**Why it matters:** off can turn a single-episode import into a
multi-minute full scan.

**Enable / disable:** toggle directly.

---

### Scheduled library updates (ScheduledLibraryUpdatesEnabled)  <a id="scheduled-scan"></a>
*Plex only — server-wide*

**What it checks:** Plex's periodic-scan safety net.

**Why it matters:** belt-and-braces in case the real-time watcher
misses an event (network mounts, container restarts). Default 12 h
interval is fine.

**Enable / disable:** toggle directly.

---

## Server trickplay options  <a id="trickplay-options"></a>
*Jellyfin only*

**What it checks:** server-wide `TrickplayOptions` (tile width, tile
height, interval, resolution widths) match this app's adapter
geometry.

**Why it matters:** Jellyfin synthesises the client-facing
`TrickplayInfo` row from server-wide `TrickplayOptions` **verbatim**
— not measured from the tiles themselves. A mismatch (e.g. server
`TileWidth=8` vs adapter `10`) makes the scrubber pull the wrong
pixel range per tile. Previews appear to load but render wrong.

**Enable:** **Sync options** — fetches the server config,
rewrites only `TileWidth`, `TileHeight`, `Interval`, and ensures the
adapter's width is listed first in `WidthResolutions`, then POSTs
back.

**Disable:** no disable — syncing is idempotent.

---

## Vendor-side preview generation  <a id="vendor-extraction"></a>

**What it checks:** whether the vendor is generating its own previews
on top of this app's output.

**Why it matters:** with this app owning previews, vendor-side
generation is wasted CPU. Plex: `enableBIFGeneration` per library
section. Emby: `ExtractTrickplayImagesDuringLibraryScan` +
`ExtractChapterImagesDuringLibraryScan` per library. Jellyfin:
`ExtractTrickplayImagesDuringLibraryScan`, while
`EnableTrickplayImageExtraction` stays on (destructive when off — see
above). For Jellyfin this row follows the same plugin rule as
[scan-time extraction](#scan-extraction): with the plugin installed it
recommends "stopped" and offers Disable; without the plugin it recommends
"running" and offers only Enable, because Jellyfin then picks up this
app's tiles through that scan.

**Enable / disable:** toggles with the current aggregate state
reported (e.g. "stopped on 3/5 libraries"). Non-destructive.

---

## Plex config folder  <a id="plex-config-folder"></a>
*Plex only*

**What it checks:** the configured Plex data folder exists on this
container and is writable. The app probes `os.access(folder, W_OK)`
only — **no test write**, no tempfile, no chmod.

**Why it matters:** BIF bundles land under `Media/localhost/<hash>/`
inside this folder. A :ro Docker mount, a wrong path, or a PUID/PGID
mismatch silently blocks every publish.

**Enable / disable:** read-only status row. Fix the mount or the
path in Server settings.

---

## Path mappings  <a id="path-mappings"></a>

**What it checks:** every configured `local_prefix` exists on this
container.

**Why it matters:** if a `local_prefix` is missing, the mapping
effectively no-ops — scan-nudges go out with unmapped paths and
publishing fails silently.

**Enable / disable:** read-only status row. Fix under Server settings
→ Path mappings.

---

## Intro & Credits  <a id="intro-credits"></a>

These rows appear only for a server that has **Send intro & credits markers to this server** switched on (Servers →
Edit → Intro & Credits). Until then the server shows one row, **Intro & Credits is off for this server**, under "All
good", and nothing about markers is checked or contacted. The rows are read from the same check the Intro & Credits
tab runs, so the two never disagree. A fact the check never got to read shows no row: a Plex whose database is on
another machine says nothing about Plex Pass until that is fixed.

Only Plex's detection row has buttons, one **Turn off** per library. Every other row says what to change and where.

### Plex

- **Skip buttons need Plex Pass** (critical) / **Plex Pass is active**. Without Plex Pass Plex serves no intro or
  credits markers at all, not ours and not its own, so nobody sees a skip button, and this app writes nothing to it
  until the server has a Pass. Viewers need Plex Pass or to be in your Plex Home too.
- **Plex hasn't made its marker list yet** (critical) / **Plex's marker list is ready**. Plex only serves markers
  attached to a database row it made itself, and this app never creates that row. Turn on Plex's own intro detection
  for one library, play a file, then check again. You can turn Plex's detection back off afterwards.
- **Plex's library database isn't on this machine** (critical) / **…is on this machine**. Markers are written into
  Plex's database, which is only safe from the machine the file is on. Run this app on the Plex machine, or run the
  [Plex marker agent](../guides.md#plex-on-another-machine-the-plex-marker-agent) next to Plex. With an agent
  switched on the row reads **The Plex marker agent isn't on the machine with Plex's database**, means the agent's
  container, and its badge reads **Fix on the agent**: mount Plex's config folder into it from one of that machine's
  own disks.
- **The Plex marker agent** (critical), shown only when an agent is switched on for this server. **…is connected**,
  or one of: **isn't answering** (markers wait, nothing is lost), **refused this app's key** (set the same shared key
  on both sides), **and this app are different versions** (the Intro & Credits tab says which to update), **is
  beside a different Plex** (check the address: markers would have gone into the wrong database). While it is red,
  no marker reaches this server, and its badge reads **Fix on the agent** instead of **Change in Plex UI**.
- **Plex's own detection can replace your markers** (recommended). When Plex analyses a file again it replaces the
  markers on it with its own, ours included. The next Intro & Credits run puts ours back, but the file shows Plex's
  times until then. Plex detects in a library only when both of its settings are on: the server's *Generate intro
  video markers* / *Generate credits video markers* (Settings → Library) and the library's own *Enable intro
  detection* / *Enable credits detection* (Edit library → Advanced; only TV libraries have the intro one). The row
  lists each library Intro & Credits goes to where both are on, and what Plex detects there, for example
  **TV Shows · intro, credits** and **Movies · credits**. **Turn off** switches off that library's own setting for the
  types listed, after a confirmation. Plex's server setting and your other libraries stay as they are.
  - **Plex's own detection is off** / **…is off in your Intro & Credits libraries**: nothing to do.
  - **Keeping Plex's own markers: its detection can stay on**: the server is set to "Keep Plex's" under "When Plex
    has its own markers", so Plex's detection is what you asked for.
  - If a library's own setting can't be read (an older Plex, or Plex didn't answer), the row falls back to the server
    setting alone, shows **unknown**, and has no buttons: turn the settings off in Plex yourself, or choose "Keep
    Plex's".

### Jellyfin and Emby

- **Media Preview Bridge plugin** (Jellyfin) and **Media Preview Bridge for Emby plugin** (Emby), critical. Neither
  server has an API for markers, so the plugin is the only way they reach it. Previews are not affected. The row
  offers **Install** where the app can do it; on an Emby whose plugin catalogue doesn't list the plugin (or whose
  catalogue couldn't be read) it says to install it by hand and the Intro & Credits tab links the guide. Emby and
  Jellyfin restart once.
- **The plugin is too old for intro and credits markers** (recommended). The installed build works for previews but
  doesn't answer the markers feature, so markers wait. Update the plugin; nothing already published is lost.

---

## Troubleshooting

- **Card says "ready (next scan)" and I want "ready (instant)":**
  install the Media Preview Bridge plugin (Jellyfin only).
- **Card flickers between states after a fix:** normal during
  Jellyfin restart (up to ~30 s). The card polls until the server
  stabilises.
- **"action needed" badge persists after fixing a flag:** click the
  refresh icon next to the badge — the probe caches for the duration
  of the modal open.
- **A disable toggle is greyed out:** that flag is already disabled.
  The UI only shows the toggle that would actually change state.

## Related

- [Multi-Media-Server Guide](../multi-server.md) — webhook routing,
  per-server path mappings
- [FAQ](../faq.md)
- [Configuration & API Reference](../reference.md) — API endpoint
  schemas including `/previews-readiness`, `/health-check/apply`,
  `/install-plugin`, `/uninstall-plugin`
