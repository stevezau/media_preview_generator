---
title: Plex, Jellyfin and Emby preview thumbnail generation compared
heading: Media Preview Generator vs Plex, Jellyfin and Emby built-in thumbnail generation
description: Built-in generators need no setup. Media Preview Generator adds GPU decoding, per-file triggers and one decode
  for several servers, but needs Docker.
facts_checked: 2026-09-24
---

This is my project, so weigh this page accordingly. I've tried to say plainly where the built-in generators are the better choice, and to link a source for how each one behaves. Spot something wrong or out of date? [Open an issue](https://github.com/stevezau/media_preview_generator/issues/new?labels=docs&title=Comparison%20correction) and I'll fix it.

If your server's built-in preview generator keeps up with your library, keep using it. It needs no setup and costs nothing extra. Media Preview Generator is for when it doesn't keep up. It runs FFmpeg with GPU decoding in its own Docker container, and starts on each new file when Sonarr, Radarr or the media server reports it. If you run more than one of Plex, Emby and Jellyfin, it decodes each file once and writes each server's own format. The cost is setup: a container, GPU passthrough, and write access to where each server keeps its previews.

Facts tagged with a reference such as P1 link to their source, and the Sources list below says what each one is. Forum posts are marked as user reports, not vendor statements.

## First, decide which problem you have

- **Previews take days to appear across a big library.** The built-in jobs run on the CPU by default ([P1][p1], [J1][j1], [E1][e1]), mostly on a schedule ([P2][p2], [J2][j2], [E2][e2]). Tune [Plex's own settings](plex-preview-thumbnails-slow.md#speed-it-up-with-plexs-own-settings) or [Jellyfin's](jellyfin-trickplay-gpu.md#start-with-jellyfins-own-settings) first; if that still can't keep up, move the work to a GPU.
- **A new episode has no previews for hours.** You want previews per file, when Sonarr or Radarr imports it. The built-ins work on a schedule or a library scan ([P2][p2], [J2][j2], [E2][e2]).
- **HDR previews look grey, or Dolby Vision looks green.** That's tone mapping, and the built-ins handle it differently (see the HDR / Dolby Vision row below).
- **You run more than one of Plex, Emby and Jellyfin.** Each built-in decodes the file again for its own server (see the Servers covered row below).
- **None of these.** Keep the built-in. It needs no setup.

## Side by side

| | Media Preview Generator | Plex built-in | Jellyfin trickplay (10.9+) | Emby built-in |
|---|---|---|---|---|
| **GPU decoding** | Yes: NVIDIA, Intel, AMD on Linux; NVIDIA on Windows via WSL2. Any GPU passed to the container is used, and the CPU takes over if GPU decode fails. | No GPU option documented. Plex calls it CPU-intensive. [P1][p1] | Optional hardware decode and MJPEG encode. Both off by default. [J1][j1] | No GPU option. [E1][e1] |
| **Where it runs** | Its own container, on the media server's host or another machine | Inside Plex Media Server | Inside Jellyfin. By default FFmpeg runs at below-normal priority with 1 thread. [J1][j1] | Inside Emby Server |
| **When it runs** | Per file on a webhook (Sonarr, Radarr, Plex, Emby, Jellyfin, Tdarr, custom). Also Recently Added polling, cron/interval schedules, or manual. | During scheduled maintenance, and optionally when media is added [P2][p2] | A scheduled task, and optionally during library scans [J1][j1] [J2][j2] | A scheduled task and library scans [E2][e2] |
| **Servers covered** | Plex, Emby and Jellyfin, any mix and number. One decode per file. | Plex | Jellyfin | Emby |
| **HDR / Dolby Vision** | Tone-maps HDR10, HLG, HDR10+. Dolby Vision 7/8 uses the HDR10 layer. Dolby Vision 5 needs a GPU with a hardware Vulkan driver; on NVIDIA that means the `graphics` capability. | Plex staff: not tone-mapped on all systems. Users report washed-out HDR and green Dolby Vision. [P3][p3] | Tone-mapped only if transcoding tone mapping is on. [J3][j3] Green Dolby Vision is still user-reported in 2026. [J4][j4] | Tone mapping is now done in Emby's core (user reports). [E3][e3] |
| **Output location** | Each server's own location: Plex data folder; Emby BIF next to the video; Jellyfin tiles next to the video, or in Jellyfin's data folder with the plugin | BIF files in Plex's data folder [P4][p4] [P2][p2] | Jellyfin's data folder, or next to the video when "save with media" is on [J1][j1] | Next to the video or in Emby's metadata folder [E4][e4] |
| **Skip Intro / Skip Credits** | Plex, Emby and Jellyfin, detected once per file. Plex viewers still need Plex Pass, and the app runs on the Plex machine or uses the Plex marker agent. Skip Intro on Emby still needs Premiere. Emby and Jellyfin need this app's plugin. [Details](skip-intro-credits.md) | TV intros, and credits on episodes and movies. Needs Plex Pass for the server's admin and for each viewer (or their Plex Home). [P7][p7] [P8][p8] | No detection of its own. Since 10.10 it stores skip segments that a plugin makes, and its web player offers to skip them. Jellyfin's own plugin makes them from chapter names. [J6][j6] | Detects TV intros, one season at a time. Skip Intro is an Emby Premiere feature. [E7][e7] [E5][e5] |
| **Setup** | Docker container, GPU passthrough, mounts, path mappings if paths differ, server sign-in | One setting | Per-library setting plus trickplay options | Per-library setting |
| **Price** | Free, MIT licence | Free. No Plex Pass needed. [P5][p5] | Free, open source | Free. No Emby Premiere needed. [E5][e5] |

The mounts depend on the server: Plex's data folder read-write for Plex, and the media folder read-write for Emby and for Jellyfin's default layout. Jellyfin output needs Jellyfin 10.10 or newer. See [Volume Mounts](getting-started.md#volume-mounts) and [Previews Readiness](guides/previews-readiness.md#version).

> [!NOTE]
> "One decode per file" does not mean one shared file. Each server still gets its own output, in its own format and place. The saving is the expensive step: the video is decoded and scaled once, not once per server. See [Per-vendor output formats](multi-server.md#per-vendor-output-formats).

## What the built-ins do better

- **Zero setup.** They are already in the server. There is no container to run, no GPU to pass through, and no path mapping to get right.
- **One system to maintain.** Previews follow the server's own settings, upgrades and file handling.
- **No write access to hand out.** This project needs write access to Plex's data folder, or to your media folders for Emby and Jellyfin. The built-ins already have it.
- **Jellyfin has hardware options of its own.** On a Jellyfin-only server with a spare Intel or AMD GPU, its own settings may be enough. See [Faster Jellyfin trickplay generation](jellyfin-trickplay-gpu.md).

## Which should you use?

- **One server, a small or slowly growing library, and waiting overnight is fine:** use the built-in.
- **Jellyfin only:** first try Jellyfin's hardware decoding, key-frame-only extraction and a higher thread count. Move to this project if a backlog still takes too long, or you want the work on another machine.
- **Plex with a large backlog, or new files that should have previews within minutes:** use this project. See [Plex preview thumbnails with a GPU](plex-preview-thumbnails-gpu.md).
- **Two or more servers holding the same files:** use this project. Each file is decoded once, not once per server.
- **Emby with a large library:** use this project if your storage is fast enough to keep a GPU busy. If disks are the bottleneck, neither option will be fast. See [Emby BIF thumbnails](emby-bif-thumbnails-gpu.md).
- **HDR or Dolby Vision thumbnails look wrong:** see [HDR and Dolby Vision thumbnails](hdr-dolby-vision-thumbnails.md).
- **Windows with an AMD or Intel GPU, or any Mac:** Docker can't reach those GPUs, so this project runs on CPU there. The built-in is simpler unless you run the container on a Linux host.
- **No Docker, or you can't give a container write access to where previews are stored:** use the built-in. For Jellyfin, [off-media mode](guides/previews-readiness.md#jellyfin-config-folder) needs write access to Jellyfin's config folder instead of the media.
- **You need chapter thumbnails:** this project doesn't make them.
- **You need a single BIF for a Roku app or one file:** a small tool such as [bifgen](https://github.com/entrez/bifgen) is enough.

> [!NOTE]
> Speed depends on the GPU, codec, storage and frame interval. This project defaults to one frame every 10 seconds, and Plex documents a default of one every 2 seconds [P6][p6], so compare at the same interval.

## Other tools you may see suggested

- **Emby community plugins.** The "MediaInfo for Emby" plugin family added HDR-to-SDR tone mapping for BIF thumbnails. It was later removed once Emby's core did this. [E3][e3] An older alpha Windows CLI, "Bif Generator", predates Emby's native BIF support. [E6][e6]
- **Small BIF CLIs.** [entrez/bifgen](https://github.com/entrez/bifgen), [amankumarsingh77/bif-generator](https://github.com/amankumarsingh77/bif-generator), shell scripts such as [this gist](https://gist.github.com/Voldrix/a66cb5c66dfae00e3f1e931e7a368adf), and [Roku's biftool](https://developer.roku.com/dev/docs/bif-file-creation). Each makes one BIF from one file. None has a GPU decode path, and none knows where a media server expects the file.
- **Jellyscrub.** The Jellyfin plugin that trickplay replaced. It is not maintained after Jellyfin 10.9.0. [J5][j5]

## Sources

All checked on the date at the top of this page unless a line says otherwise. "User report" means a forum post or issue from a user, not a vendor statement.

- **P1.** Plex Support, [Why is Plex using lots of CPU when nothing is playing?](https://support.plex.tv/articles/201697383-why-is-plex-using-my-cpu/): "essentially requires a transcode… CPU-intensive". Plex requests for GPU generation date back to at least 2021 ([forum](https://forums.plex.tv/t/use-gpu-for-generating-video-preview-thumbnails/680852), user report). No Plex changelog entry adding GPU decode for this feature was found.
- **P2.** Plex Support, [Video Preview Thumbnails](https://support.plex.tv/articles/202197528-video-preview-thumbnails/): options are disabled, during Scheduled Tasks maintenance, or maintenance plus when new content is added. It can run "for hours or even days", and the index files add to the space Plex Media Server's data uses.
- **P3.** Plex forum, [HDR tonemapping for video thumbnails](https://forums.plex.tv/t/hdr-tonemapping-for-video-thumbnails/755821) (Plex staff reply); [feature request](https://forums.plex.tv/t/proper-color-grading-in-dolby-vision-and-hdr-preview-thumbnails/778941); [2023 report](https://forums.plex.tv/t/hdr-tone-mapping-for-video-thumbnails-on-synology-again/830582) (user report).
- **P4.** Plex Support, [What are video preview thumbnails](https://support.plex.tv/articles/201725267-what-are-video-preview-thumbnails-in-plex/): "media index files" or "BIF" index files.
- **P5.** [Plex plans](https://www.plex.tv/plans/): preview thumbnails are not listed as a Plex Pass feature.
- **P6.** Plex Support, [Advanced, Hidden Server Settings](https://support.plex.tv/articles/201105343-advanced-hidden-server-settings/) (last modified 8 December 2022): `GenerateBIFFrameInterval`, default 2 seconds.
- **P7.** Plex Support, [Skip TV Show Intros](https://support.plex.tv/articles/skip-content/) (last modified 19 August 2024): detecting intros needs Plex Pass on the server admin's account, and skipping them needs it on the viewer's account or their Plex Home's admin. Intros are found by matching episodes' audio within a season.
- **P8.** Plex Support, [Credits Detection](https://support.plex.tv/articles/credits-detection/) (last modified 19 August 2024): credits on episodes and movies, with the same Plex Pass rule for personal media.
- **J1.** Jellyfin source, [TrickplayOptions.cs](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Model/Configuration/TrickplayOptions.cs): hardware acceleration, hardware encoding and key-frame-only extraction default to off; `ProcessThreads` 1; `ProcessPriority` BelowNormal. Hardware MJPEG encoding is limited to QSV, VA-API, VideoToolbox and RKMPP ([jellyfin-web string](https://github.com/jellyfin/jellyfin-web/blob/master/src/strings/en-us.json), `LabelTrickplayAccelEncodingHelp`). Each library's options ([LibraryOptions.cs](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Model/Configuration/LibraryOptions.cs)) include `ExtractTrickplayImagesDuringLibraryScan` and `SaveTrickplayWithMedia`.
- **J2.** [jellyfin/jellyfin#11549](https://github.com/jellyfin/jellyfin/issues/11549).
- **J3.** [jellyfin/jellyfin#13142](https://github.com/jellyfin/jellyfin/issues/13142) (confirmed by a core developer in the thread).
- **J4.** Fix: [jellyfin/jellyfin#8049](https://github.com/jellyfin/jellyfin/issues/8049) and PR #12571. Recurrence: [r/jellyfin, 2026](https://www.reddit.com/r/jellyfin/comments/1spfb8q/trickplay_images_with_incorrect_colors_for_dolby/) (user report; last checked 23 September 2026, because Reddit blocks automated re-checks).
- **J5.** Quoted in [HaveAGitGat/Tdarr_Plugins#362](https://github.com/HaveAGitGat/Tdarr_Plugins/issues/362).
- **J6.** Jellyfin docs, [Media segments](https://jellyfin.org/docs/general/server/metadata/media-segments/): "Media segments are provided by plugins", and an official Chapter Segments Provider plugin makes them from chapters. The [10.10.0 release post](https://jellyfin.org/posts/jellyfin-release-10.10.0/): "you will still require a plugin to create them", and the web interface supports skipping them.
- **E1.** Emby forum, [staff reply](https://emby.media/community/topic/96831-i-just-want-to-generate-the-thumbnail-previews-bif/): "We use ffmpeg to create the images, and our own code to write the bif files". A plugin developer's view that GPU decoding would not help, because I/O dominates: [forum](https://emby.media/community/topic/49481-fr-use-hw-acceleration-for-chapter-image-extraction/) (developer opinion, not an Emby statement).
- **E2.** Emby forum, [scheduled task discussion](https://emby.media/community/topic/136433-question-about-video-preview-thumbnail-extraction-scheduled-task/) (user report).
- **E3.** Emby forum, [BIF generation suggestion](https://emby.media/community/topic/118095-suggestion-for-bif-file-generation/) and [MediaInfo for Emby plugin thread](https://emby.media/community/topic/108984-mediainfo-for-emby-pluginhdr-vision-atmos-dtsx/page/54/) (user reports).
- **E4.** Emby forum, [rescan thumbnail images](https://emby.media/community/topic/126919-how-to-rescan-thumbnail-images-after-having-replaced-video-files/) and [where BIFs are saved](https://emby.media/community/topic/121672-how-to-stop-generating-bif-and-jpeg-file-without-turning-off-vpt/): unticking "Save video preview thumbnails into media folders" saves them to the server's metadata folder instead (user reports).
- **E5.** [Emby Premiere Feature Matrix](https://emby.media/support/articles/Premiere-Feature-Matrix.html): preview thumbnail generation is not a Premiere feature; Intro Skipping is Premiere-only.
- **E6.** Emby forum, [Bif Generator Windows tool](https://emby.media/community/topic/71614-bif-generator-windows-tool/).
- **E7.** Emby docs, [Intro Skip](https://emby.media/support/articles/Intro-Skip.html): "Requires Emby Server 4.7 or later and an Emby Premiere subscription". Detection runs per TV season and needs at least two episodes.

This project's own behaviour is documented in [Getting Started](getting-started.md), the [Multi-server guide](multi-server.md) and [HDR & Dolby Vision](guides.md#hdr--dolby-vision).

[p1]: https://support.plex.tv/articles/201697383-why-is-plex-using-my-cpu/
[p2]: https://support.plex.tv/articles/202197528-video-preview-thumbnails/
[p3]: https://forums.plex.tv/t/hdr-tonemapping-for-video-thumbnails/755821
[p4]: https://support.plex.tv/articles/201725267-what-are-video-preview-thumbnails-in-plex/
[p5]: https://www.plex.tv/plans/
[p6]: https://support.plex.tv/articles/201105343-advanced-hidden-server-settings/
[p7]: https://support.plex.tv/articles/skip-content/
[p8]: https://support.plex.tv/articles/credits-detection/
[j1]: https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Model/Configuration/TrickplayOptions.cs
[j2]: https://github.com/jellyfin/jellyfin/issues/11549
[j3]: https://github.com/jellyfin/jellyfin/issues/13142
[j4]: https://www.reddit.com/r/jellyfin/comments/1spfb8q/trickplay_images_with_incorrect_colors_for_dolby/
[j5]: https://github.com/HaveAGitGat/Tdarr_Plugins/issues/362
[j6]: https://jellyfin.org/docs/general/server/metadata/media-segments/
[e1]: https://emby.media/community/topic/96831-i-just-want-to-generate-the-thumbnail-previews-bif/
[e2]: https://emby.media/community/topic/136433-question-about-video-preview-thumbnail-extraction-scheduled-task/
[e3]: https://emby.media/community/topic/118095-suggestion-for-bif-file-generation/
[e4]: https://emby.media/community/topic/126919-how-to-rescan-thumbnail-images-after-having-replaced-video-files/
[e5]: https://emby.media/support/articles/Premiere-Feature-Matrix.html
[e6]: https://emby.media/community/topic/71614-bif-generator-windows-tool/
[e7]: https://emby.media/support/articles/Intro-Skip.html
