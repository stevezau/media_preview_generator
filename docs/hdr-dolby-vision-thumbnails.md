---
title: HDR and Dolby Vision preview thumbnails for Plex, Jellyfin and Emby
heading: HDR and Dolby Vision preview thumbnails without washed-out or green frames
description: Washed-out or green previews come from HDR frames not tone-mapped. This app tone-maps HDR10, HLG and Dolby Vision;
  DV Profile 5 needs a Vulkan GPU.
---

Washed-out, grey or green preview thumbnails come from frames taken from HDR video without tone mapping them to SDR. Tone mapping converts HDR's brightness and colour range into the smaller range a JPEG thumbnail can show. Media Preview Generator does this automatically:

- HDR10, HLG and HDR10+ are tone-mapped to SDR.
- Dolby Vision Profiles 7 and 8 are read through their HDR10 layer.
- Dolby Vision Profile 5, which has no HDR10 layer, needs a GPU with a hardware Vulkan driver in the container. On NVIDIA that means `NVIDIA_DRIVER_CAPABILITIES=all`.

## Why the built-ins get it wrong

- **Plex.** Plex staff have said preview thumbnails come from software frame grabbing and that tone mapping "doesn't work on all systems yet" ([forum](https://forums.plex.tv/t/hdr-tonemapping-for-video-thumbnails/755821)). Users report washed-out HDR and green Dolby Vision thumbnails. A 2023 report says it is still happening ([forum](https://forums.plex.tv/t/hdr-tone-mapping-for-video-thumbnails-on-synology-again/830582), user report).
- **Jellyfin.** Trickplay is only tone-mapped when tone mapping is on in Jellyfin's transcoding settings. Otherwise HDR trickplay looks grey. A core developer confirmed this in [#13142](https://github.com/jellyfin/jellyfin/issues/13142). A Dolby Vision colour fix landed via [#8049](https://github.com/jellyfin/jellyfin/issues/8049), but users [still report](https://www.reddit.com/r/jellyfin/comments/1spfb8q/trickplay_images_with_incorrect_colors_for_dolby/) green Dolby Vision trickplay in 2026. If you stay on Jellyfin's built-in, turn transcoding tone mapping on first.
- **Emby.** Per forum reports, Emby's core tone-maps BIF thumbnails ([forum](https://emby.media/community/topic/118095-suggestion-for-bif-file-generation/), user report).

Checked 23 September 2026. See the [comparison page](comparison.md) for sources.

## What this app does, per format

- **HDR10, HLG, HDR10+:** tone-mapped to SDR. The algorithm is set in **Settings → Processing options → Thumbnails → HDR tone mapping**. The default is `hable`; the others are `reinhard`, `mobius`, `clip`, `gamma` and `linear`. If thumbnails look too dark, try `reinhard`.
- **Dolby Vision Profile 7 and 8:** these carry an HDR10 fallback layer, which is tone-mapped the same way, with hardware decode.
- **Dolby Vision Profile 5:** there is no HDR10 layer, so the Dolby Vision data itself has to be applied. The app picks a path by GPU vendor:
  - Intel: VAAPI decode with an OpenCL tone map from Jellyfin's FFmpeg build.
  - NVIDIA: libplacebo on Vulkan.
  - AMD: VAAPI decode, then Vulkan with libplacebo.

The app picks the path per file from its metadata. A Dolby Vision file whose video also declares a PQ (HDR10) or HLG transfer is tone-mapped through that layer; one that declares neither is treated as Profile 5. Nothing needs to be set per library. Full detail is in [Guides — HDR & Dolby Vision](guides.md#hdr--dolby-vision).

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

If NVIDIA's Vulkan driver arrives without the small config files that point to it, which happens with `--gpus all` on some Docker setups, the app writes those files itself. If those thumbnails are still tinted, the dashboard says what is missing under **Settings → Processing Options → GPU device tuning**.

### NVIDIA graphics files missing from the container

The dashboard says this when CUDA works in the container but none of NVIDIA's graphics driver files reached it, even with `NVIDIA_DRIVER_CAPABILITIES=all`. The app can't fill this gap itself, because the driver isn't there to use. Look for the files on the host (not in the container):

```bash
find / -xdev -name 'libGLX_nvidia.so*' 2>/dev/null
```

**It finds nothing:** the host's NVIDIA driver was installed without its graphics part. Install the full driver.

**It finds them:** the host's NVIDIA container setup isn't passing them in. It's worth telling whoever provides your NVIDIA driver package. Until that's fixed, mount the files in yourself. In the lines below, replace `/usr/lib` with the folder `find` printed, and `595.99.02` with your driver version (the number at the end of `libGLX_nvidia.so.<version>`). First check that every file exists on the host:

```bash
ls /usr/lib/libGLX_nvidia.so.0 /usr/lib/libEGL_nvidia.so.0 /usr/lib/libnvidia-{glcore,glvkspirv,tls,eglcore,glsi}.so.595.99.02
```

Then add them to the container's `volumes:` in compose (or as `-v` options) and recreate it:

```yaml
    volumes:
      - /usr/lib/libGLX_nvidia.so.0:/usr/lib/x86_64-linux-gnu/libGLX_nvidia.so.0:ro
      - /usr/lib/libEGL_nvidia.so.0:/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.0:ro
      - /usr/lib/libnvidia-glcore.so.595.99.02:/usr/lib/x86_64-linux-gnu/libnvidia-glcore.so.595.99.02:ro
      - /usr/lib/libnvidia-glvkspirv.so.595.99.02:/usr/lib/x86_64-linux-gnu/libnvidia-glvkspirv.so.595.99.02:ro
      - /usr/lib/libnvidia-tls.so.595.99.02:/usr/lib/x86_64-linux-gnu/libnvidia-tls.so.595.99.02:ro
      - /usr/lib/libnvidia-eglcore.so.595.99.02:/usr/lib/x86_64-linux-gnu/libnvidia-eglcore.so.595.99.02:ro
      - /usr/lib/libnvidia-glsi.so.595.99.02:/usr/lib/x86_64-linux-gnu/libnvidia-glsi.so.595.99.02:ro
```

The app writes the config files Vulkan needs, and the log then says `Vulkan ready for Dolby Vision Profile 5 tone-mapping`. After a host driver update, change the version numbers to match. A file that doesn't exist on the host is created there as an empty folder, and Vulkan stays unavailable.

Intel and AMD need no extra step beyond passing `/dev/dri`, which also brings the Mesa Vulkan driver.

**Without a hardware Vulkan driver,** the app can't run the Profile 5 tone-mapping path. It logs a warning and extracts those frames without tone mapping, so their thumbnails come out with a green and purple tint. If only a software Vulkan driver is found (the usual case on NVIDIA without the `graphics` capability), the bell menu also shows a notification. With no Vulkan at all, the log is the only sign. For a diagnostic bundle to attach to a GitHub issue, use `GET /api/system/vulkan/debug`.

## Checking the result

- The **Inspector** (`/inspector`) shows the published thumbnails for Plex, Emby or Jellyfin, frame by frame ([Inspector](multi-server.md#inspector-multi-server)).
- To redo files after fixing a setting, start a job with **Processing Mode → Regenerate all**. Otherwise files with a current preview are skipped.
- If a file failed on the GPU and was redone on the CPU, the worker's card in the dashboard **Workers** panel shows a yellow **CPU fallback** badge, and the job log gives the reason.

## Speed

Dolby Vision Profile 5 is the slowest format to process. How long it takes depends on the GPU, the codec and your storage, and each GPU vendor needs something different ([FAQ](faq.md#why-is-cpu-usage-high-when-i-have-a-gpu-configured)).

HDR is slow on a CPU worker, because it tone-maps every frame in software. A CPU-only node also has no hardware Vulkan, so its Dolby Vision Profile 5 thumbnails come out tinted. To send these files to a GPU worker, switch on the HDR [routing rule](guides.md#routing-rules).

## Limits

- Dolby Vision Profile 5 on NVIDIA needs the `graphics` capability, as above.
- Without a GPU that provides hardware Vulkan, Profile 5 thumbnails come out with a green and purple tint. That covers CPU-only hosts, macOS, AMD or Intel on Windows, and NVIDIA without the `graphics` capability.
- The thumbnails are SDR by design. Preview thumbnails are small JPEGs, not HDR images.

## Related

- [Generate Plex preview thumbnails with a GPU](plex-preview-thumbnails-gpu.md)
- [Faster Jellyfin trickplay generation](jellyfin-trickplay-gpu.md)
- [Emby BIF thumbnails with a GPU](emby-bif-thumbnails-gpu.md)
