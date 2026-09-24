---
title: FAQ
heading: FAQ
description: 'Answers to common Media Preview Generator questions: Plex, Emby and Jellyfin support, Windows and Docker, GPU
  choice, speed, RAM use and skipped files.'
faq_schema: true
---

> [Back to Docs](README.md)

Common questions about setup, usage, and behavior. For troubleshooting specific errors, see [Guides — Troubleshooting](guides.md#troubleshooting). For HDR and Dolby Vision behavior, see [Guides — HDR & Dolby Vision](guides.md#hdr--dolby-vision).

## Contents

- [Common questions](#common-questions)
- [General](#general)
- [Intro & Credits](#intro--credits)
- [GPUs](#gpus)
- [Performance](#performance)
- [Docker](#docker)
- [Processing](#processing)

## Related Docs

- [Getting Started](getting-started.md)
- [Guides & Troubleshooting](guides.md)
- [Configuration & API Reference](reference.md)

<!-- An <a id> at the end of a heading is the id the old MkDocs site gave that question, so links to it in issues and forums still land (tests/test_docs_site.py). No space before it: a space would change the heading's own id. -->

## Common questions

### Why are my Plex preview thumbnails taking so long?

Plex makes them inside the server, on the CPU, one frame every 2 seconds by default, during its scheduled maintenance window and optionally when media is added. Plex's own help page says a large library can take days. You can space the frames out in Plex, or hand the job to a GPU with this app, which starts on each file as it arrives. [More: Why Plex previews are slow](plex-preview-thumbnails-slow.md)

### Can I generate Plex preview thumbnails with a GPU?

Not with Plex itself: Plex documents no GPU option for this job. Media Preview Generator runs it with FFmpeg on an NVIDIA, Intel or AMD GPU and writes the result where Plex expects it, in Plex's data folder, which it needs read-write. [More: Plex previews with a GPU](plex-preview-thumbnails-gpu.md)

### How do I make Jellyfin trickplay generation faster?

Start with Jellyfin's own settings: hardware decoding, key-frame-only extraction, and more FFmpeg threads than the default of 1. If it still falls behind, this app can make the trickplay tiles on a GPU, on another machine if you like, and tell Jellyfin about them. [More: Faster Jellyfin trickplay](jellyfin-trickplay-gpu.md)

### Does Emby's thumbnail extraction use the GPU?

No. Emby's built-in BIF extraction has no GPU option. This app makes Emby BIF files on a GPU and saves them next to each video, where Emby looks for them, so the media folder must be writable. [More: Emby BIF previews with a GPU](emby-bif-thumbnails-gpu.md)

### Can previews be made as soon as Sonarr or Radarr imports a file?

Yes. Add a webhook in Sonarr or Radarr for import and upgrade. The app waits for a quiet period (60 seconds by default), makes the previews, and retries while the media server is still indexing the new file. [More: Previews on Sonarr or Radarr import](sonarr-radarr-preview-thumbnails.md)

### Why are my HDR or Dolby Vision preview thumbnails washed out or green?

The frames were grabbed without tone mapping, so HDR colours land in an ordinary picture. This app tone-maps HDR10, HLG and HDR10+, and uses the HDR10 layer of Dolby Vision profiles 7 and 8. Profile 5 needs a GPU with a hardware Vulkan driver in the container. [More: HDR and Dolby Vision previews](hdr-dolby-vision-thumbnails.md)

---

## General

### What does Media Preview Generator do?<a id="what-does-this-tool-do"></a>

Generates video preview thumbnails for **Plex, Emby, and Jellyfin** — alone or in any combination. These are the small images you see when scrubbing through videos. It runs preview generation off the media server, on a machine of your choosing, using every GPU it finds. When two or more of your servers contain the same file, FFmpeg runs only once and the output is written in each server's expected format (Plex stores it as a **BIF** bundle, Emby reads a **BIF** sidecar file next to the video, Jellyfin reads a folder of JPG tiles called **trickplay**). It can also add Skip Intro and Skip Credits markers: see [Intro & Credits](#intro--credits).

### Which Plex, Emby and Jellyfin settings should I change?<a id="what-plexembyjellyfin-settings-should-i-use"></a>

- **Plex**: In Settings → Library, set **"Generate video preview thumbnails"** to **Never**.
- **Emby**: Emby can make its own thumbnails during library scans. Turn off its scan-time thumbnail extraction and chapter-image extraction on each library (`ExtractTrickplayImagesDuringLibraryScan` and `ExtractChapterImagesDuringLibraryScan`). The Setup Health tab recommends this; it changes them only when you click **Disable**.
- **Jellyfin**: In each library's settings, **enable "Trickplay image extraction"** (Jellyfin reads this app's published tiles only when this is on). For **"Extract trickplay images during library scan"**: turn it off if the Media Preview Bridge plugin is installed. Without the plugin, keep it **on** — it's how Jellyfin picks up the tiles on its next scan.

The **Setup Health** tab on the Edit Server modal audits every required
flag across all three vendors and offers one-click toggles — see the
[Setup Health guide](guides/previews-readiness.md). Destructive flips
(like disabling Jellyfin's trickplay extraction) require typed confirmation.

Disabling each vendor's built-in generation avoids duplicate work and prevents the server from using CPU for thumbnails when you want this app to handle them. The one exception is Jellyfin without the plugin, as above.

### Does it run on Windows or Unraid?<a id="does-this-work-on-windows"></a>

Yes — run the Docker image on Docker Desktop with the WSL2 backend. If you have an **NVIDIA** GPU it is accelerated: the NVIDIA Windows driver exposes CUDA and NVDEC into WSL2, so `--gpus all` works much as it does on Linux (best-effort — WSL2 GPU detection is less reliable than native Linux). **AMD and Intel** GPUs are not accelerated under Docker (D3D11VA can't be reached from Docker's Linux VM), so those setups process on CPU — raise **CPU Workers** in Settings, or run the container on a Linux host. There's no separate Windows native build. See [Getting Started — Windows](getting-started.md#windows). There is an Unraid Community Applications template: see [Unraid](getting-started.md#unraid).

### Does it make chapter thumbnails too?<a id="does-this-generate-chapter-thumbnails"></a>

No. It makes **video preview thumbnails** (the timeline-scrubbing strip) and, once you turn it on for a server, Skip Intro and Skip Credits markers ([Intro & Credits guide](guides.md#intro--credits)). It doesn't make chapter thumbnails.

### Do I need a GPU to use Media Preview Generator?<a id="can-i-use-this-without-a-gpu"></a>

No. In **Settings** → **Processing Options**, disable all GPUs (or set workers to 0) and set **CPU Workers** to your desired value (e.g. `4` or `8`).

### Is there a Windows .exe, or do I need Docker?<a id="is-docker-required-is-there-a-standalone-exe"></a>

Docker is required. There is no standalone executable and no from-source install path — the container bundles the FFmpeg build and codec support the app depends on, so Docker is the only supported deployment. See [Getting Started](getting-started.md) for setup. It runs on Linux, Windows (Docker Desktop, WSL2 backend), macOS, Unraid, Synology, and anywhere else Docker runs.

### Does my media server need to run in Docker too?

No. Plex, Emby, and Jellyfin can all run bare-metal, in Docker, or any other
way. This tool just needs:

- **Network access** to each server's API (port 32400 for Plex, 8096 for
  Emby/Jellyfin by default).
- **For Plex specifically**: read/write access to the Plex application data
  directory (where BIF bundles are stored — mounted as `/plex`).
- **For Emby and Jellyfin**: **read-write** access to the media files,
  because Emby BIFs and Jellyfin trickplay tiles are written next to each
  video. A `:ro` media mount makes every write fail for those servers. No
  server-config mount needed. The exception is Jellyfin's
  [off-media mode](guides/previews-readiness.md#jellyfin-config-folder): it
  writes into Jellyfin's config folder instead, so that folder must be
  read-write and the media can stay read-only.
- **For Plex only**: read access to the media is enough.

### Can it run on a different machine from Plex, Emby or Jellyfin?<a id="can-i-run-this-on-a-different-machine-than-my-media-servers"></a>

Yes. The tool can run anywhere that can reach your servers' APIs over the
network. For Plex you also need access to the Plex config directory (NFS,
SMB, shared volume, etc.); for Emby/Jellyfin you just need the media files
visible. See [Networking](getting-started.md#networking) for setup details.

### Does it work with Jellyfin and Emby, not just Plex?<a id="does-this-work-with-jellyfin-or-emby"></a>

Yes. The app supports Plex, Emby, and Jellyfin — alone or in any combination. Each server is added under **Settings → Media Servers**. When two or more servers contain the same file, FFmpeg runs only once and the result is written in each server's expected format (Plex stores it as a BIF bundle, Emby reads a BIF sidecar file next to the video, Jellyfin reads a folder of JPG tiles called trickplay). See the [Multi-Server guide](multi-server.md) for setup, webhook routing, and per-server library/exclude rules.

---

## Intro & Credits

### Does it add Skip Intro and Skip Credits buttons?

Yes, on Plex, Emby and Jellyfin, once you turn it on for each server. What each server needs:

- **Plex**: needs Plex Pass on the server, and for viewers (or their Plex Home). The app runs on the same machine as Plex, or the Plex marker agent runs there for it.
- **Jellyfin** 10.11 or 12.0: the Media Preview Bridge plugin.
- **Emby** 4.9 or 4.10: the Media Preview Bridge for Emby plugin. Skip Intro also needs Emby Premiere, which is Emby's rule; Skip Credits doesn't.

[More: Skip Intro and Skip Credits](skip-intro-credits.md)

### How does it find intros and credits?

From the file's chapters, online skip databases, the theme song a season's episodes share, or the credit roll itself. It does this once per file and sends the result to every server that has the file. When it isn't sure, the file waits in **Needs review** instead of getting a guess.

### Will it overwrite Plex's or Emby's own markers?

Only with **Use ours**, which is the default. Choose **Keep Plex's** or **Keep Emby's** in the server's Intro & Credits settings and it leaves them alone.

---

## GPUs

### How do I check which GPUs it found?<a id="how-do-i-know-which-gpus-are-detected"></a>

Open **Settings** → **Processing Options**. The GPU panel lists all detected GPUs with their device IDs, names, and types.

### Can it use more than one GPU?<a id="can-i-use-multiple-gpus"></a>

Yes. In **Settings** → **Processing Options**, enable individual GPUs and set workers and FFmpeg threads per GPU. Each GPU can be enabled/disabled independently.

### Which GPU is best for preview thumbnails?<a id="which-gpu-should-i-use"></a>

Whichever one you already have. Any GPU the container can reach decodes the video; with none, the CPU does it, more slowly.

| GPU Type | Notes |
|----------|----------|
| NVIDIA | CUDA decoding; also works on Windows through WSL2 |
| Intel iGPU | Low power; common on Unraid |
| AMD | VAAPI decoding on Linux |
| CPU-only | Works everywhere; the CPU does all the decoding |

### Does it handle HDR and Dolby Vision?<a id="hdr--dolby-vision-support"></a>

Yes. It tone-maps HDR10, HLG and HDR10+ so thumbnails don't come out grey, and uses the HDR10 layer of Dolby Vision Profile 7 and 8; Profile 5 has its own path for each GPU vendor. See the [HDR & Dolby Vision](guides.md#hdr--dolby-vision) section in Guides for the per-vendor details.

---

## Performance

### How many workers and threads should I set?<a id="how-many-threads-should-i-use"></a>

Start with the defaults and increase gradually while monitoring system load. See the [Performance Tuning](getting-started.md#performance-tuning) table in Getting Started for concrete starting points across hardware tiers.

### Why is CPU usage high when I have a GPU configured?

GPU workers use both GPU and CPU — this is normal. The GPU handles video decoding and downscaling to thumbnail size; the CPU handles frame selection, JPEG encoding, and (for HDR content) part of the colour conversion. Standard SDR content barely uses the CPU at all; HDR content — especially Dolby Vision — uses noticeably more because frames have to move between CPU and GPU memory for the colour conversion step.

Dolby Vision Profile 5 (no HDR10 fallback layer) needs the most from your setup:

- **Intel** (iGPU, Arc): just pass the GPU to the container (`--device /dev/dri:/dev/dri`).
- **NVIDIA**: set `NVIDIA_DRIVER_CAPABILITIES=all` (or at minimum `compute,video,utility,graphics`) so the NVIDIA Vulkan driver is available inside the container. See [HDR & Dolby Vision](guides.md#hdr--dolby-vision) for the full explanation.
- **AMD** (Radeon): decoded on the GPU and tone-mapped through Vulkan, as for NVIDIA. Not yet tested on AMD hardware.
- **Apple / CPU-only**: no hardware Vulkan, so Profile 5 thumbnails come out with a green and purple tint ([Limits](hdr-dolby-vision-thumbnails.md#limits)).

The **FFmpeg Threads** setting per GPU controls how many CPU cores each worker can use. If you're running multiple GPU workers and seeing CPU contention, lower this value.

### How much RAM does each worker use?

Typical per-worker RSS with hardware decode:

| Content | Per-worker RSS |
|---|---|
| SDR 1080p | ~90–200 MB |
| 4K HDR10 / DV P7+8 | ~250–300 MB |
| 4K DV Profile 5 (libplacebo) | ~350–500 MB |

Earlier builds used ~1 GB per worker on 4K HDR content because frames were downloaded from the GPU at full source resolution. A recent fix moved the downscale onto the GPU itself, so only the small thumbnail-sized frame moves back to system RAM. An 8 GB container now comfortably supports 12+ GPU workers.

### What does thumbnail quality 1-10 change?<a id="whats-thumbnail-quality-1-10"></a>

Lower numbers = higher quality but larger file sizes.

- Quality 2 = highest quality
- Quality 4 = default (good balance)
- Quality 10 = lowest quality

The value is passed straight to FFmpeg's `-q:v`. FFmpeg's MJPEG encoder clamps
qscale to a minimum of 2, so setting 1 produces byte-identical output to 2 —
2 really is as sharp as it goes.

### Why is generation slow on my Unraid or mergerfs array?<a id="generation-feels-disk-bound-on-my-multi-disk-setup-unraidmergerfsjbod--how-do-i-speed-it-up"></a>

On setups where one share is backed by multiple physical disks (unraid's `shfs`, mergerfs, JBOD), parallel workers processing files in alphabetical order tend to pile onto one disk at a time. Open the **New Job** modal (or edit a full-library schedule) and set **Processing Order** to **Random**. Workers will pull items from different disks in parallel, so disk read throughput — not GPU — sets the ceiling. Webhook jobs and Recently Added scans don't expose this setting because they only touch a handful of files where ordering doesn't matter. See [Issue #219](https://github.com/stevezau/media_preview_generator/issues/219) for background.

---

## Docker

### Why does my container fail to start?

Most common cause: `init: true` in your docker-compose. Remove it — this container manages its own processes internally, and `init: true` conflicts with that.

### Why can't the container find my files?

Path mapping issue. See [Path Mappings](reference.md#path-mappings).

### Where do I find the login token?<a id="how-do-i-get-the-authentication-token"></a>

Use [Authentication Token](getting-started.md#authentication-token).

---

## Processing

### Can I make previews for some libraries only?<a id="can-i-process-specific-libraries-only"></a>

Yes. In **Settings** → **Libraries**, select which libraries to process.

### How do I redo previews that already exist?<a id="how-do-i-regenerate-existing-thumbnails"></a>

When starting a job, use the **Regenerate** option to force regeneration of existing thumbnails.

### Why does it skip some files?<a id="why-is-it-skipping-some-files"></a>

Possible causes:

- Thumbnails already exist (use the **Regenerate** option when starting a job to force)
- File not found (check [path mappings](reference.md#path-mappings))
- Gone from disk: a newer file replaced it before its job ran (the newer file gets its own preview)
- Invalid file format

### Why does a worker's ETA show "-"?<a id="why-does-eta-show-calculating-for-so-long"></a>

Each worker's ETA is the time left on the file it is working on, worked out from how far FFmpeg has got through the file and how fast it is going. It reads "-" until FFmpeg reports how far it has got. Before FFmpeg starts, for example while the worker is finding the file on a server or reusing frames it already made, the card shows that step instead of an ETA.

### What is the webhook / Sonarr / Radarr path column for?

Only relevant if you use [webhook integration](guides.md#webhook-integration).
When Sonarr, Radarr, or Tdarr fire a webhook, they include the file path as
*they* see it inside their container, which may differ from how your media
server sees it and how this app sees it. The webhook column in each server's
path-mapping row translates between them. For example:

| Container | Might see the file as |
|-----------|----------------------|
| Plex (or Emby, or Jellyfin) | `/data/tv/Show/episode.mkv` |
| Sonarr | `/tv/Show/episode.mkv` |
| This tool | `/mnt/media/tv/Show/episode.mkv` |

If you are not using webhooks, or every container uses the same media paths,
leave the webhook column blank.

---

[Back to Docs](README.md) | [Main README](https://github.com/stevezau/media_preview_generator/blob/dev/README.md)
