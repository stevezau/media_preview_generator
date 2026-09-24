---
title: Why Plex video preview thumbnails take so long
heading: Why Plex video preview thumbnails take so long, and how to speed them up
description: Plex makes preview thumbnails on the CPU inside the server, at a frame every 2 seconds by default. How to tune
  Plex, or move the work to a GPU.
---

Plex makes video preview thumbnails inside Plex Media Server, on the CPU. Plex's support articles say the job "essentially requires a transcode" of each file, and call it CPU-intensive. By default Plex grabs a frame every 2 seconds, and it runs the job during its scheduled maintenance. So a large library can take "hours or even days". To speed it up:

- Generate only for the libraries that need it.
- Leave key-frame-only extraction on.
- Raise the frame interval.
- Or move the work to a GPU with an external tool such as Media Preview Generator.

## Why it is slow

- **It decodes the video on the CPU.** Plex describes the job as CPU-intensive, taking "anywhere from less than a minute to several minutes for a single item". The time depends on the processor and on the file's length and resolution ([Plex Support](https://support.plex.tv/articles/201697383-why-is-plex-using-my-cpu/)). Plex documents no GPU option for this job.
- **It samples often.** Plex's documented default is one thumbnail every 2 seconds (`GenerateBIFFrameInterval`, [Plex Support](https://support.plex.tv/articles/201105343-advanced-hidden-server-settings/)).
- **It runs on a schedule.** You choose "never", "as a scheduled task", or "as a scheduled task and when media is added" ([Plex Support](https://support.plex.tv/articles/202197528-video-preview-thumbnails/)). Plex warns that turning it on before a large import can keep the server busy "for hours or even days". Users report it working through one file at a time during the maintenance window (forum reports, not a Plex statement).
- **It shares the machine with playback.** The same CPU is serving your streams.

## Speed it up with Plex's own settings

Try these first. They cost nothing.

1. **Limit it to the libraries that need it.** Preview generation can be turned off per library. It is an advanced setting when you create or edit a library ([Plex Support](https://support.plex.tv/articles/202197528-video-preview-thumbnails/)). Home videos or rarely watched libraries may not need previews at all.
2. **Keep key-frame-only extraction on.** `GenerateBIFKeyframesOnly` "greatly speeds up processing and reduces CPU usage". It defaults to on. Check that nobody turned it off.
3. **Raise the frame interval.** `GenerateBIFFrameInterval` sets the seconds between thumbnails. Going from 2 to 5 or 10 means far fewer frames, with coarser scrubbing. Both are hidden settings: they live in `Preferences.xml` on Linux, the registry on Windows, and a `.plist` on macOS. Restart Plex after changing them. Plex documents both in [Advanced, Hidden Server Settings](https://support.plex.tv/articles/201105343-advanced-hidden-server-settings/), last modified 8 December 2022. We have not confirmed they behave the same on every current Plex release.
4. **Don't turn on "when media is added" right before a big import.** Plex's own warning: the server may spend hours or days on it.

If that is fast enough, stop here. The built-in needs no extra software.

## Move the work to a GPU

[Media Preview Generator](comparison.md) is a free, MIT-licensed Docker app. It makes the same BIF files Plex would, using FFmpeg with GPU decoding (NVIDIA, Intel or AMD). It then writes them into Plex's data folder, where Plex serves them to its apps. It differs from the built-in in several ways:

- **It uses the GPU.** Decoding and downscaling happen on the GPU. If a file won't decode there, the same worker retries it on the CPU ([CPU fallback](guides.md#automatic-gpu--cpu-fallback)).
- **It works on several files at once.** You set the number of workers per GPU, plus optional CPU workers.
- **It works per file, when the file arrives.** Triggers include Sonarr and Radarr webhooks, Plex's own webhook (Plex Pass), a "Recently Added" poll (no Plex Pass), schedules, or a manual pick. See [Previews as soon as Sonarr or Radarr imports a file](sonarr-radarr-preview-thumbnails.md).
- **It can run on another machine.** It needs network access to Plex, and the Plex data folder mounted read-write.

Once it is running, set Plex's **Settings → Library → Generate video preview thumbnails** to **Never**, so Plex doesn't do the same work again. Setup steps are in [Generate Plex preview thumbnails with a GPU](plex-preview-thumbnails-gpu.md).

> [!NOTE]
> Speed depends on your GPU, the codec, your storage and the frame interval. This app defaults to one frame every 10 seconds (adjustable from 1 to 60). If you compare, use the same interval on both. If your disks are the bottleneck, a GPU won't fix that. On multi-disk shares, see the FAQ entry on [disk-bound setups](faq.md#why-is-generation-slow-on-my-unraid-or-mergerfs-array).

## Limits to know first

- It runs only in Docker. It has a web UI and no CLI.
- Plex must scan a new file before its preview can be written, because the file's location in Plex's data folder only exists after the scan. The app retries automatically after 1, 2 and 5 minutes by default. See [Slow-backoff retry queue](multi-server.md#slow-backoff-retry-queue).
- It makes video preview thumbnails only, not chapter thumbnails.
- On Windows only NVIDIA GPUs are accelerated, and on macOS none are. Those setups run on CPU.

## Related

- [Comparison with the built-in generators](comparison.md)
- [Getting Started](getting-started.md)
- [HDR and Dolby Vision thumbnails](hdr-dolby-vision-thumbnails.md): if Plex's HDR previews look washed out or green
