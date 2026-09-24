---
title: Plex, Emby and Jellyfin previews when Sonarr or Radarr import a file
heading: Generate preview thumbnails as soon as Sonarr or Radarr imports a file
description: Add a Sonarr or Radarr webhook for import and upgrade. Previews are made after a 60 s quiet period, with retries
  while the server indexes the file.
---

In Sonarr or Radarr, add a **Webhook** connection that points at Media Preview Generator. Enable the import and upgrade events. When a file lands, the app waits for a quiet period (60 seconds by default), then makes previews for just those files. It publishes them to every Plex, Emby or Jellyfin server that has the file. If a server hasn't added the file to its library yet, the app retries automatically.

## Set it up

1. In Media Preview Generator, open **Automation → Triggers** and copy the **Radarr** or **Sonarr Webhook URL**.
2. In Radarr or Sonarr, go to **Settings → Connect → + → Webhook**.
3. Paste the URL and enable the events:
   - **Radarr:** On Import and On Upgrade.
   - **Sonarr:** On File Import and On File Upgrade.
4. **Authentication:** leave **Username** empty and put your API token or webhook secret in **Password**. If the form has a Headers section, you can use an `X-Auth-Token` header instead.
5. Click **Test**, then **Save**. The test shows up in the app's webhook activity log.

Field-by-field steps: [Configure Radarr](guides.md#configure-radarr) and [Configure Sonarr](guides.md#configure-sonarr). To use a separate secret for webhooks, see [Webhook Secret](guides.md#webhook-secret).

> [!TIP]
> Sonarr and Radarr often see paths differently from your media server and from this container, for example `/tv/...` against `/data/tv/...`. Fill in the webhook column of the server's path mapping so the paths line up. See the FAQ entry on the [webhook path column](faq.md#what-is-the-webhook--sonarr--radarr-path-column-for) and [Path Mappings](reference.md#path-mappings).

## What the app accepts

- **Radarr and Sonarr `Download` events.** These cover both imports and upgrades. The file path comes from:
  - Radarr: `movieFile.path`, or `movie.folderPath` plus `movieFile.relativePath`.
  - Sonarr: `episodeFile.path`, or `series.path` plus `episodeFile.relativePath`.
- **`Test` events.** Logged, and nothing is processed.
- **Any other event type.** Ignored and logged.
- **Sportarr.** Sonarr-compatible payloads are accepted at `/api/webhooks/sportarr`.
- **One URL for everything.** `POST /api/webhooks/incoming` also recognises Sonarr and Radarr payloads by their shape, alongside Plex, Emby, Jellyfin and plain `{"path": "..."}` bodies. See [Webhook Endpoints](reference.md#webhook-endpoints).
- **Tdarr, FileFlows and scripts.** Use the custom endpoint with `{"file_path": "..."}`. This matters when a tool changes a file after import, because Sonarr and Radarr send nothing then. See [Custom Webhook](guides.md#custom-webhook-tdarr-scripts-etc).

## What happens after the webhook

1. **Batching.** Files are queued per source. A batch runs once the delay (default 60 seconds, range 10–300) passes with no new files from that source. Each new file restarts the timer. A season pack becomes one job. See [Batching and the delay](guides.md#batching-and-the-delay).
2. **Finding the servers.** Each path is mapped to the container's view. Then every configured server whose enabled libraries contain the file is found. A path outside every enabled library is skipped, not retried.
3. **One decode.** FFmpeg runs once per file on a GPU worker, or on a CPU worker if you have no GPU.
4. **Publishing.** Each server that owns the file gets its own format:
   - Plex: a BIF in its data folder.
   - Emby: a BIF next to the video.
   - Jellyfin: trickplay tiles.
5. **Queue priority.** New imports run at **High** priority by default. They overtake a running full-library scan file by file, without cancelling it ([details](guides.md#letting-new-imports-jump-the-queue)).

## Retries for files the server hasn't indexed yet

Sonarr and Radarr often call the webhook before your media server has scanned the new file. Plex in particular can't take a preview until its scan creates the item.

- A file that isn't in a server's library yet, or whose preview hasn't been registered yet, is retried automatically.
- The retries run as follow-up jobs covering only the files still waiting.
- Work already done is not repeated. A companion file records the source file's size and modification time, so files with a current preview are skipped. Frames extracted in the last hour are reused if a second trigger arrives for the same file, such as Plex's own webhook after Sonarr's. See [Smart dedup](multi-server.md#smart-dedup-skipping-work-thats-already-done).

To shorten the wait, make sure the server notices new files quickly:

- Plex: turn on the FSEvent library-update settings.
- Emby and Jellyfin: turn on real-time monitoring.

[Setup Health](guides/previews-readiness.md) checks these.

## Upgrades

On an upgrade, Sonarr and Radarr send the new file along with the file it replaced. The app makes previews for the new file. It also removes the old file's sidecar output next to the media (the Emby BIF and the Jellyfin trickplay folder), so stale previews don't pile up.

The Plex direct webhook and the Recently Added poll only see new library items, not in-place upgrades. For upgrades, the Sonarr/Radarr webhook is the trigger that works ([why](guides.md#auto-trigger-from-plex-no-sonarrradarr)).

## Troubleshooting

- **401 from the webhook:** the token is wrong or missing. Leave Username empty and put the token in Password.
- **Radarr or Sonarr can't reach the URL:** use the host's IP or a Docker hostname they can resolve, not `localhost`.
- **The test works but imports do nothing:** check that the import events are ticked, and that webhooks are enabled on the Automation page.
- **The job sits in Pending:** check that **Incoming job priority** is **High**, and that **Pause Processing** is off.

More in the [troubleshooting table](guides.md#troubleshooting).

## Related

- [Generate Plex preview thumbnails with a GPU](plex-preview-thumbnails-gpu.md)
- [Faster Jellyfin trickplay generation](jellyfin-trickplay-gpu.md)
- [Emby BIF thumbnails with a GPU](emby-bif-thumbnails-gpu.md)
- [Comparison with the built-in generators](comparison.md)
