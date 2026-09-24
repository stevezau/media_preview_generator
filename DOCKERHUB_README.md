<!-- The Docker Hub "Full Description", synced by CI (ci.yml update-dockerhub-description) on each
     release tag. It is README.md with every link and image made absolute (Docker Hub has no repository
     to resolve a relative path against) and without the badge block or centred HTML. Three differences
     are deliberate: the logo is a sized <img> of the PNG, the captions are _italics_ rather than
     HTML sub tags, and the Image Tags section is Docker-only. tests/test_readmes.py fails when the
     sections the two files share drift apart, so edit both. -->

<img src="https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/icon.png" alt="" width="110" height="110">

# Media Preview Generator

**Your media server's slowest jobs, done on your GPU.** Media Preview Generator makes the preview
thumbnails and the Skip Intro and Skip Credits markers for Plex, Emby and Jellyfin. It starts the moment
Sonarr or Radarr imports a file and sends the results to every server at once. Self-hosted, one Docker
container.

**[Explore the docs »](https://mediapreviewgenerator.dev/)** ·
[Quick start](#quick-start) ·
[How it compares](https://mediapreviewgenerator.dev/comparison/) ·
[Ask a question](https://github.com/stevezau/media_preview_generator/discussions) ·
[Report a bug](https://github.com/stevezau/media_preview_generator/issues/new?labels=bug)

![Tears of Steel in the Plex, Jellyfin and Emby web players, each showing a preview thumbnail over the seek bar](https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/players-3up.webp)

_Plex, Jellyfin and Emby web players on test servers, each paused mid-scrub on the same film. The
thumbnails were made by this app. Tears of Steel, (CC) Blender Foundation | mango.blender.org,
[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)._

## What it does

Plex, Emby and Jellyfin can make preview thumbnails themselves, but they do it inside the server, mostly
on the CPU, and mostly on a schedule. On a big library that can take days, and the episode that arrived
tonight can sit with a blank timeline until the next maintenance window.

**Media Preview Generator makes them on your GPU, for each file as it arrives.** When more than one
server holds the same file, it decodes it once and writes each server's own format.

**It also adds Skip Intro and Skip Credits.** It finds each file's intro and end credits once and sends
the markers to every server that has the file. When it isn't sure, it sends nothing and the file waits
for your review.

## What it looks like

| It finds every server with that file |
| --- |
| ![Servers page with one card each for Plex, Jellyfin and Emby](https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/tour-resolve.webp) |

| One GPU pass per file |
| --- |
| ![Dashboard with GPU workers making previews for three films](https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/tour-extract.webp) |

| Each server gets its own format |
| --- |
| ![One film's previews published to Plex, Jellyfin and Emby](https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/tour-publish.webp) |

| It waits for slow servers |
| --- |
| ![A Jellyfin job waiting to retry while the server indexes a new film](https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/tour-retry.webp) |

_App screenshots come from a test setup with made-up servers; the job titles are open films._

## Features

**GPU first, CPU when needed**
- **Every GPU you pass in.** NVIDIA, Intel and AMD on Linux; NVIDIA on Windows through WSL2. Each GPU passed to the container gets its own workers.
- **CPU fallback built in.** A file the GPU can't decode is retried on the CPU by the same worker.
- **Key-frame skipping.** Jumps between key frames when a file's key-frame spacing allows it.

**Starts on its own**
- **Webhooks.** Sonarr, Radarr, Sportarr, Tdarr, FileFlows, Plex (Plex Pass), Emby (Premiere) and Jellyfin, or any JSON with a path. Sonarr, Radarr and the three servers can share one URL.
- **Schedules.** Recently Added polling, plus cron and interval schedules.
- **Manual Generation.** Search your servers by title (a whole show, a movie or one episode), or browse the media folders, and make previews for just those.
- **Retries and skips.** Retries after 1, 2 and 5 minutes by default while a server indexes a new file, and skips files whose previews are current.

**Every server from one decode**
- **Each server's own format.** A BIF in Plex's data folder, a BIF next to the video for Emby, and Jellyfin trickplay tiles (next to the video, or in Jellyfin's data folder with the companion plugin).
- **Setup Health.** Checks each server's settings and offers fixes one library at a time.

**Right colours**
- **HDR tone mapping.** Tone-maps HDR10, HLG and HDR10+, and uses the HDR10 layer of Dolby Vision profiles 7 and 8. Profile 5 needs a GPU with a hardware Vulkan driver.

**Skip Intro and Skip Credits**
- Intro & Credits is in the `dev` image (`stevezzau/media_preview_generator:dev`) now, and in `latest` from the next release.
- **Found once, sent to every server.** Finds intros and end credits from the file's chapters, online skip databases (TheIntroDB, IntroDB.app, SkipDB), the season's theme song or the credit roll itself (read on the GPU when that's faster), then sends the markers to every server. When it isn't sure, the file waits for your review instead of getting a guess. Off until you [turn it on](https://mediapreviewgenerator.dev/guides/#turning-it-on) for a server.
- **Yours to correct.** Tools → Intro & Credits adjusts, adds and locks a file's markers by hand, and Setup Health checks each server's Intro & Credits setup.
- **Plex.** Needs Plex Pass on the server, and for viewers (or their Plex Home). The app runs on the Plex machine, or next to it through the [Plex marker agent](https://mediapreviewgenerator.dev/guides/#plex-on-another-machine-the-plex-marker-agent).
- **Jellyfin.** Jellyfin 10.11 or 12.0, with the Media Preview Bridge plugin (the one trickplay uses).
- **Emby.** Emby 4.9 or 4.10, with the Media Preview Bridge for Emby plugin. Skip Intro needs Emby Premiere; Skip Credits doesn't.

## Where it fits

It sits next to Sonarr, Radarr and Tdarr and takes over the media server's own preview job. Once it
runs, the server's own generation is the same work done twice. The app's
[Setup Health](https://mediapreviewgenerator.dev/guides/previews-readiness/) tab says which
setting to turn off on each server, and which to leave on: switching Jellyfin's trickplay off deletes
the tiles this app wrote. If your server's own generator keeps up, you don't need this: see
[how it compares](https://mediapreviewgenerator.dev/comparison/).

## Quick start

**You'll need:** Docker · Plex, Emby or Jellyfin (Jellyfin 10.10 or newer), reachable from the
container · write access where previews go: Plex's data folder, or the media folder for Emby and
Jellyfin. A GPU is optional: its driver on the host, plus the NVIDIA Container Toolkit for NVIDIA on
Linux.

```bash
docker run -d \
  --name media-preview-generator \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 -e PGID=1000 \
  -v /path/to/media:/media \
  -v /path/to/plex/config:/plex \
  -v /path/to/app/config:/config \
  stevezzau/media_preview_generator:latest
```

- Intel or AMD: `--device /dev/dri` as above. NVIDIA: `--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all` instead. No GPU, or on Windows or macOS? Delete the `--device /dev/dri` line.
- Plex writes into `/plex`, so the media can be `:ro`. Emby, and Jellyfin in its default layout, write next to each video, so for them the media mount must be read-write. No Plex? Drop the `/plex` mount.
- Open `http://YOUR_IP:8080` and sign in with the token saved in `auth.json` in your app config folder (or pin your own with `-e WEB_AUTH_TOKEN=...`), then follow the setup wizard.
- The image is `stevezzau/media_preview_generator`. The double z is the Docker Hub account's name, not a typo.

Docker Compose, Unraid and GPU details: [Getting started](https://mediapreviewgenerator.dev/getting-started/).

## Image Tags

| Tag | Source | Use for |
|---|---|---|
| `:latest` | Latest GitHub release | **Recommended.** Stable. |
| `:X.Y.Z` (version) | A specific release (e.g. `:3.7.5`) | Pinning to a known-good version |
| `:dev` | Every push to `dev` | Bleeding edge — may break |

See the [releases page](https://github.com/stevezau/media_preview_generator/releases) for version history and per-release notes.

## Documentation

The docs are also a website: **[mediapreviewgenerator.dev](https://mediapreviewgenerator.dev/)**.

| Page | What's in it |
| --- | --- |
| [Getting started](https://mediapreviewgenerator.dev/getting-started/) | Docker, GPUs, mounts, Unraid, networking |
| [How it compares](https://mediapreviewgenerator.dev/comparison/) | Plex, Jellyfin and Emby built-in generation, side by side |
| [Guides](https://mediapreviewgenerator.dev/guides/) | The web UI, webhooks, schedules, troubleshooting |
| [Multi-server](https://mediapreviewgenerator.dev/multi-server/) | Plex, Emby and Jellyfin from one instance |
| [Reference](https://mediapreviewgenerator.dev/reference/) | Every setting, environment variable and API endpoint |
| [FAQ](https://mediapreviewgenerator.dev/faq/) | Windows, GPUs, RAM, skipped files |
| [Why Plex previews are slow](https://mediapreviewgenerator.dev/plex-preview-thumbnails-slow/) | What Plex does, and what to change |
| [Plex previews with a GPU](https://mediapreviewgenerator.dev/plex-preview-thumbnails-gpu/) | Moving Plex's preview job to a GPU |
| [Faster Jellyfin trickplay](https://mediapreviewgenerator.dev/jellyfin-trickplay-gpu/) | Jellyfin's own settings first, then a GPU |
| [Emby BIF previews with a GPU](https://mediapreviewgenerator.dev/emby-bif-thumbnails-gpu/) | Emby BIFs made on a GPU, next to each video |
| [Previews on Sonarr or Radarr import](https://mediapreviewgenerator.dev/sonarr-radarr-preview-thumbnails/) | Webhooks, quiet period, retries |
| [HDR and Dolby Vision previews](https://mediapreviewgenerator.dev/hdr-dolby-vision-thumbnails/) | Tone mapping, and the Dolby Vision 5 caveat |
| [Skip Intro and Skip Credits](https://mediapreviewgenerator.dev/skip-intro-credits/) | What each server needs, how they're found, limits |

## Support the project

It's free and MIT licensed. Helping is optional.

- **[Star it on GitHub](https://github.com/stevezau/media_preview_generator)**: free, and it's how other server owners find it.
- **[Buy me a coffee on Ko-fi](https://ko-fi.com/stevezau)**: no account needed.

## Get help

- **Previews don't show up?** Open the server's Setup Health tab in the app. It names the setting that's in the way.
- **Not sure it's a bug?** Ask in [Discussions](https://github.com/stevezau/media_preview_generator/discussions).
- **Found a bug?** [Open an issue](https://github.com/stevezau/media_preview_generator/issues/new?labels=bug) with the app version and what you tried.

## License

MIT, see [LICENSE](https://github.com/stevezau/media_preview_generator/blob/main/LICENSE). Recent development is AI-assisted (Claude); every change is reviewed and tested.
