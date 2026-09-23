---
title: Skip Intro and Skip Credits detection for Plex, Emby and Jellyfin
heading: Skip Intro and Skip Credits
description: Detects intros and end credits once per file and adds Skip Intro and Skip Credits to Plex, and to Jellyfin and
  Emby through its own plugin.
---

> [!NOTE]
> Intro & Credits is in the `dev` image (`stevezzau/media_preview_generator:dev`) now, and in `latest` from the next release.

Media Preview Generator detects where each file's intro and end credits are and sends those points to Plex, Emby and Jellyfin, so viewers get a skip button. It detects each file once and sends the same answer to every server that has the file. On Jellyfin and Emby it works through its own plugin. It runs in the same Docker container that makes your preview thumbnails, and it stays off until you turn it on for a server.

## What viewers get, and what each server needs

- **Plex.** Skip Intro and Skip Credits buttons. Viewers need Plex Pass, or a Plex Home whose admin has it: without it, Plex hides skip buttons altogether. Plex has no way for another app to add markers, so the app writes them into Plex's own database and asks you to confirm once per server first. That only works on the Plex machine, so either run the app there or run the small [Plex marker agent](guides.md#plex-on-another-machine-the-plex-marker-agent) container next to Plex.
- **Jellyfin 10.11 or 12.0.** Jellyfin has no intro detection of its own: it shows skip segments that a plugin provides ([Jellyfin docs](https://jellyfin.org/docs/general/server/metadata/media-segments/)). This app's [Media Preview Bridge plugin](guides.md#jellyfin-the-media-preview-bridge-plugin), the same one trickplay uses, is that plugin. Viewers get Skip Intro and Skip Credits, plus Skip Recap and Skip Preview.
- **Emby 4.9 or 4.10.** Skip Intro, and Skip Credits through Emby's Up Next prompt. It works through this app's [Media Preview Bridge for Emby plugin](guides.md#emby-the-media-preview-bridge-for-emby-plugin). Skip Intro also needs Emby Premiere, which is Emby's rule; the credits prompt works without it. Emby has no credits end, so skipping credits goes to the end of the file, past any scene after the credits.

Plex and Emby can have markers of their own. With **Use ours**, the default, the app's markers replace them. **Keep Plex's** or **Keep Emby's** leaves them alone. For what each server offers by itself, see the [comparison](comparison.md).

## How it detects intros and credits

It looks in four places:

- **Chapters** already in the file.
- **Online skip databases.**
- **The theme song** a season's episodes share (TV only).
- **The credit roll itself**, read from the text on screen near the end of the file.

When it isn't sure, it sends nothing. The file waits in **Needs review** instead of getting a guess, because a missing skip button is better than one that skips into the story. From there you can check it, adjust the times, or add a marker yourself.

## When it runs

- **On import.** When Sonarr or Radarr imports a file, the app makes the previews first, then detects the intro and credits.
- **Across a library.** Start an Intro & Credits job for the libraries you choose, or put one on a schedule.
- **On the workers you already have.** It uses the same GPU and CPU workers, queue and priorities as previews, and adds none of its own.
- **Only where you [turn it on](guides.md#turning-it-on).** Each server has its own **Send intro & credits markers to this server** switch, off by default, and an **Intro & Credits** column on its Libraries tab.

## Limits

- **Sports libraries start switched off.** No online source covers sports, and a broadcast's on-screen text is easily taken for credits.
- **Movies have little online coverage**, so they mostly rely on the credit roll.
- **Broadcast TV with channel logos or scores on screen is a weak spot.** It sometimes skips into the story.
