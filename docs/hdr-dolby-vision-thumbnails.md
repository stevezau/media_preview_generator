---
description: "Washed-out or green previews come from HDR frames not tone-mapped. This app tone-maps HDR10, HLG and Dolby Vision; DV Profile 5 needs a Vulkan GPU."
---

# HDR and Dolby Vision preview thumbnails without washed-out or green frames

Washed-out, grey or green preview thumbnails come from frames taken from HDR video without tone mapping them to SDR. Tone mapping converts HDR's brightness and colour range into the smaller range a JPEG thumbnail can show. Media Preview Generator does this automatically:

- HDR10, HLG and HDR10+ are tone-mapped to SDR.
- Dolby Vision Profiles 7 and 8 are read through their HDR10 layer.
- Dolby Vision Profile 5, which has no HDR10 layer, needs a GPU with a hardware Vulkan driver in the container. On NVIDIA that means `NVIDIA_DRIVER_CAPABILITIES=all`.

## Why the built-ins get it wrong

- **Plex.** Plex staff have said preview thumbnails come from software frame grabbing and that tone mapping "doesn't work on all systems yet" ([forum](https://forums.plex.tv/t/hdr-tonemapping-for-video-thumbnails/755821)). Users report washed-out HDR and green Dolby Vision thumbnails. A 2023 report says it is still happening ([forum](https://forums.plex.tv/t/hdr-tone-mapping-for-video-thumbnails-on-synology-again/830582), user report).
- **Jellyfin.** Trickplay is only tone-mapped when tone mapping is on in Jellyfin's transcoding settings. Otherwise HDR trickplay looks grey. A core developer confirmed this in [#13142](https://github.com/jellyfin/jellyfin/issues/13142). A Dolby Vision colour fix landed via [#8049](https://github.com/jellyfin/jellyfin/issues/8049), but users [still report](https://www.reddit.com/r/jellyfin/comments/1spfb8q/trickplay_images_with_incorrect_colors_for_dolby/) green Dolby Vision trickplay in 2026. If you stay on Jellyfin's built-in, turn transcoding tone mapping on first.
- **Emby.** BIF thumbnails were once washed out. Per forum reports, Emby's core now tone-maps them ([forum](https://emby.media/community/topic/118095-suggestion-for-bif-file-generation/), user report).

Checked 23 September 2026. See the [comparison page](comparison.md) for sources.

## What this app does, per format

- **HDR10, HLG, HDR10+:** tone-mapped to SDR. The algorithm is set in **Settings → Thumbnail Settings → HDR Tone Mapping**. The default is `hable`; the others are `reinhard`, `mobius`, `clip`, `gamma` and `linear`. If thumbnails look too dark, try `reinhard`.
- **Dolby Vision Profile 7 and 8:** these carry an HDR10 fallback layer, which is tone-mapped the same way, with hardware decode.
- **Dolby Vision Profile 5:** there is no HDR10 layer, so the Dolby Vision data itself has to be applied. The app picks a path by GPU vendor:
  - Intel: VAAPI decode with an OpenCL tone map from Jellyfin's FFmpeg build.
  - NVIDIA: libplacebo on Vulkan.
  - AMD: VAAPI to Vulkan. This path is untested by the maintainer.

The app detects the format from the file's metadata, including the Dolby Vision profile, and picks the right path per file. Nothing needs to be set per library. Full detail is in [Guides — HDR & Dolby Vision](guides.md#hdr--dolby-vision).

## The one setup step: NVIDIA and Dolby Vision Profile 5

NVIDIA's container toolkit only loads its Vulkan driver when the container asks for the `graphics` capability. The common `compute,video,utility` setting leaves it out. Everything else still works, but Dolby Vision Profile 5 doesn't.

```bash
docker run -d \
  --gpus all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  ...
  stevezzau/media_preview_generator:latest
```

In Docker Compose, set `NVIDIA_DRIVER_CAPABILITIES=all` under `environment:`. `compute,video,utility,graphics` also works if you prefer the minimum. Restart the container after changing it.

Intel and AMD need no extra step beyond passing `/dev/dri`, which also brings the Mesa Vulkan driver.

**Without a hardware Vulkan driver,** the app can't run the Profile 5 tone-mapping path. It logs a warning and extracts those frames without tone mapping, so their thumbnails come out visibly dim. If only a software Vulkan driver is found (the usual case on NVIDIA without the `graphics` capability), the dashboard also shows a notice. With no Vulkan at all, the log is the only sign. For a diagnostic bundle to attach to a GitHub issue, use `GET /api/system/vulkan/debug`.

## Checking the result

- The **BIF Viewer** (`/bif-viewer`) shows the published thumbnails for Plex, Emby or Jellyfin, frame by frame ([BIF Viewer](multi-server.md#bif-viewer-multi-server)).
- To redo files after fixing a setting, start a job with **Regenerate** ticked. Otherwise files with a current preview are skipped.
- If a file failed on the GPU and was redone on the CPU, its worker card shows a yellow **CPU fallback** badge, and the job log gives the reason.

## Speed

Dolby Vision Profile 5 is the slowest format to process. The docs list rough, unbenchmarked expected speeds per GPU vendor. Treat them as indications, because they depend on the GPU, codec and storage ([FAQ](faq.md#why-is-cpu-usage-high-when-i-have-a-gpu-configured)). HDR files also use more CPU and memory per worker than SDR, because some of the colour conversion moves between GPU and CPU memory.

## Limits

- Dolby Vision Profile 5 on NVIDIA needs the `graphics` capability, as above.
- Where no GPU reaches the container, there is no hardware Vulkan driver, so Profile 5 files get the dim fallback. That covers CPU-only hosts, macOS, and AMD or Intel on Windows.
- The thumbnails are SDR by design. Preview thumbnails are small JPEGs, not HDR images.

## Related

- [Generate Plex preview thumbnails with a GPU](plex-preview-thumbnails-gpu.md)
- [Faster Jellyfin trickplay generation](jellyfin-trickplay-gpu.md)
- [Emby BIF thumbnails with a GPU](emby-bif-thumbnails-gpu.md)
