# Media Preview Bridge for Emby

Lets [Media Preview Generator](https://github.com/stevezau/media_preview_generator) show **Skip Intro** and **Skip
Credits** in Emby, and keeps those markers when Emby refreshes the item's metadata.

## What problem does this solve?

Emby has no API for writing intro or credits markers: it keeps them as chapter rows with a marker type (`IntroStart`,
`IntroEnd`, `CreditsStart`), and `POST /Items/{id}` ignores chapters. This plugin adds an endpoint that writes those
rows next to the file's own chapters, which it never changes. It only ever removes rows it wrote itself: markers from
Emby's own intro detection or from other plugins stay unless you ask for ours to replace them.

Emby's "Replace all metadata" and "Search for missing metadata" refreshes delete every marker row. The plugin keeps a
copy of what it was sent and writes the markers back during that same refresh, as long as the file on disk is still
the one the markers were detected on. Normal refreshes, library scans and restarts don't touch the markers.

## Install

When Emby's plugin catalog lists the plugin, the **Install** button on Media Preview Generator's **Intro & Credits**
tab installs it and restarts Emby. Otherwise install it by hand. Pick the build for your Emby version: 4.9 and 4.10
plugins aren't interchangeable.

1. Download `MediaPreviewBridge.Emby-4.10.dll` (Emby 4.10) or `MediaPreviewBridge.Emby-4.9.dll` (Emby 4.9) from the
   plugin's GitHub release (tags `emby-plugin-v…`) and rename it to `MediaPreviewBridge.Emby.dll`.
2. Copy it into Emby's `plugins` folder (`/config/plugins` in the official container).
3. Restart Emby.

After the restart, Media Preview Generator's **Intro & Credits** tab shows the plugin as ready for that Emby server.

## Uninstall

Remove the markers first: call `DELETE /MediaPreviewBridge/Markers/{Id}` for each item. Media Preview Generator has
no way to clear a whole server yet (turning Intro & Credits off leaves the markers in place). Once the plugin is gone,
the rows it wrote stay in Emby with nothing to remove them or put them back, until a "Replace all metadata" refresh of
the item (which also removes every other marker). Then delete
`MediaPreviewBridge.Emby.dll` and the `MediaPreviewBridge.Emby` folder from Emby's `plugins` folder and restart Emby.

## API

Emby keeps C# property names as they are, so every JSON field is PascalCase, and it leaves out fields that are null
(treat a missing field as null). Times are ticks (1 ms = 10,000 ticks). `{Id}` is Emby's numeric item id.

| Endpoint | Auth | What it does |
|---|---|---|
| `GET /MediaPreviewBridge/Ping` | anonymous | `{"Ok": true, "Version": "1.0.0.0", "Features": ["markers"]}`. Use `Ok` to detect that the plugin is installed. |
| `GET /MediaPreviewBridge/Markers/{Id}` | admin | The item's stored markers as a `MarkersResponse` (`Stored` is 0). |
| `POST /MediaPreviewBridge/Markers/{Id}` | admin | Replaces the item's stored markers and writes them into Emby's chapters straight away. Returns a `MarkersResponse`; `Stored` is the number of our marker rows the item now shows. |
| `DELETE /MediaPreviewBridge/Markers/{Id}` | admin | Removes the rows the plugin wrote for the item and its stored markers. Returns a `MarkersResponse` with `Stored` 0. With nothing stored, no row changes. |

POST body:

```json
{"IntroStartTicks": 100000000, "IntroEndTicks": 400000000, "CreditsStartTicks": 1000000000, "FileSize": 6421799,
 "ReplaceOwn": false}
```

`MarkersResponse`:

```json
{"Id": "1234", "Found": true, "IntroStartTicks": 100000000, "IntroEndTicks": 400000000,
 "CreditsStartTicks": 1000000000, "FileSize": 6421799, "Stale": false, "Stored": 3}
```

`Replacing*Ticks` (`ReplacingIntroStartTicks`, `ReplacingIntroEndTicks`, `ReplacingCreditsStartTicks`) appear only
while a write is under way, or after Emby stopped in the middle of one: the markers of ours the item's rows showed
before it, which can still be the rows on the item. A caller that asks which rows are the plugin's own has to count
them as well. They go at the next POST, DELETE or item update.

- Answers are HTTP 200 JSON, so both Emby versions answer the same way. An unknown or non-numeric id gives
  `Found: false, Error: "item not found"`. A body the plugin refuses gives `Found: true` with `Error` saying why, and
  nothing is stored. When the store file or the item's chapters can't be written, the answer is HTTP 500 with the same
  JSON shape and `Error` set; the store and the chapter rows are left as they were. If Emby stops in the middle of a
  write (a restart, a crash), the store file still names the rows that write was replacing, so the next POST, DELETE or
  update of the item finishes it. Emby itself answers 401 without a
  token and 403 for a user who isn't an administrator.
- Intro (start + end) and credits are handled as two types. `ReplaceOwn` (default `false`) decides what happens when
  the item already has rows of a type that the plugin didn't write (Emby's own intro detection, another plugin):
  `false` keeps those rows and adds none of ours for that type; `true` replaces them with ours. The plugin's own
  earlier rows are always replaced. A skipped type is added at the item's next update if it has no rows by then.
- With Emby Premiere's intro detection on for the library, Emby may detect an episode again after the plugin wrote its
  markers, and Emby's save replaces all of the item's marker rows: our rows (including `CreditsStart`) can disappear
  until the item's next update. Media Preview Generator's read-back check sends them again.
- Intro start and end come together or not at all, with `0 <= IntroStartTicks < IntroEndTicks`. `CreditsStartTicks`
  is `>= 0` (Emby has no credits end). At least one of intro or credits is required: use `DELETE` to clear.
- `FileSize` (optional, bytes) is the size of the file the markers were detected on. The plugin also stores the
  item's path. While the file on disk has a different size or the item has a different path (the file was replaced),
  the markers are stored but not shown, `Stale` is `true`, nothing is written back, and the plugin's rows are removed
  at the item's next update (when Emby re-reads a replaced file it drops every marker row itself). Push markers for
  the new file to fix that. Without `FileSize`, or when the file can't be read, only the path is compared.
- The file's own chapters (`MarkerType: Chapter`) are kept on every write.
- "Replace all metadata" and "Search for missing metadata" refreshes delete every marker row; the plugin writes its
  markers back in the same refresh. Default refreshes, library scans and restarts leave the rows alone.
- Markers are stored as one JSON file per item in `<emby-config>/plugins/MediaPreviewBridge.Emby/markers/` (the
  folder is logged at start-up: "Media Preview Bridge: marker store …"). A file that can't be read or doesn't hold
  valid markers counts as no markers, is logged once, and is never written back; the next POST replaces it.
- When Emby removes a video from the library, its stored markers are deleted. When a whole show folder is removed,
  Emby reports only the show, so the scheduled task **Media Preview Bridge: clean up Intro & Credits markers**
  (Scheduled Tasks → Maintenance; after every start and daily at 03:00) deletes the stored markers of items Emby no
  longer has. It waits while Emby is starting or scanning (up to 10 minutes) and skips the run if that doesn't end.
  Until it runs, a leftover file is only shown on an item with the same id and the same path.

## Build

```bash
dotnet build -c Release -p:EmbyAbi=4.10   # MediaBrowser.Server.Core 4.10.0.24-beta2
dotnet build -c Release -p:EmbyAbi=4.9    # MediaBrowser.Server.Core 4.9.1.90
```

Emby's own assemblies are compile-only references, so `MediaPreviewBridge.Emby.dll` is the only DLL in the build output.

## Catalog submission

Text for the Emby plugin catalog entry. Not submitted yet: the entry needs a forum thread and a developer id from Emby
staff.

**Name:** Media Preview Bridge for Emby

**Short description:** Adds Skip Intro and Skip Credits markers from Media Preview Generator and keeps them in place.

**Description:** Media Preview Generator detects intros and credits once per file (chapters, online intro databases,
season audio matching) and sends them to every server that has the file. This plugin gives Emby a small API
(`/MediaPreviewBridge/Markers/{Id}`, administrators only) that stores those markers per item and writes them as Emby's
own IntroStart / IntroEnd / CreditsStart chapter markers. The file's own chapters are never changed, and markers from
Emby's own intro detection or other plugins are replaced only when the app asks. When Emby rebuilds an item's chapters
it writes them again; when the file is replaced by a different file it stops until the app sends new ones; when the item
is removed it forgets them. Each version of a video is its own Emby item and gets its own markers. Viewers need Emby
Premiere on the server to skip intros, as for Emby's own intro detection. No data leaves the server.

**Targets:** Emby Server 4.9 and 4.10 (separate DLLs).

**Source / issues:** https://github.com/stevezau/media_preview_generator (folder `emby-plugin/`).

**Developer:** <owner's Emby forum name — to be filled>
