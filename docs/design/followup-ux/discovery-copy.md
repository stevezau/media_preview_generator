# Listing and demo copy

Prepared for owner review, 2026-10-05. These are drafts, not published community messages or a released-version announcement. Check each community's self-promotion rules before posting. The app's optional features and vendor requirements remain explicit.

## Docker Hub

Short description (95 characters; source of truth: `pyproject.toml`):

> GPU previews, intro/credits and shared results for Plex, Emby and Jellyfin. Self-hosted Docker.

Full description: the repository's `DOCKERHUB_README.md`. The existing release workflow publishes it on a release tag. The new wording explains remote processing and distinguishes CPU chapter/loudness work from GPU previews.

## GitHub About

Root task manages the live description, homepage and existing topics. Suggested fuller wording if needed:

> Self-hosted previews, intro/credits detection and cross-server reuse for Plex, Emby and Jellyfin, with GPU acceleration and remote processing. Includes Plex chapter thumbnails and CPU loudness analysis; Sonarr/Radarr automation.

Homepage: https://mediapreviewgenerator.dev/

Social preview upload: `docs/images/social-preview.jpg` (1280×640 baseline JPEG). This is separate from the website's Open Graph metadata. The existing card and README image show open films on test servers; do not substitute private media names or screenshots.

## Release announcement draft

Title: Clearer processing setup, job results and troubleshooting

This update makes it easier to see what Media Preview Generator is doing and what each server needs. Server configuration groups preview, chapter, intro/credits and loudness choices under Processing. Job details put saved results, active work and file issues into readable groups while preserving the unified queue and complete worker view.

The documentation now takes you from choosing a task to checking one file: GPU previews for Plex, Jellyfin trickplay, intro/credits and processing on another machine. It explains output mounts and the local-database exception for Plex loudness beside the decisions they affect.

Use the final release's tested change list for the additional setup, logs and webhook improvements. Do not publish this draft as a released feature list before the branch is verified and released.

## Plex demo draft

Title: A GPU preview workflow for Plex, with a one-file check

I maintain Media Preview Generator, a self-hosted Docker app that makes Plex's scrubber previews using NVIDIA, Intel or AMD hardware on supported hosts. It can run beside Plex or on a separate machine with access to the media and writable preview output location.

The short walkthrough is: pass the GPU through, check the Plex paths, process one file, inspect the per-file result, then scrub that file in Plex. The guide includes CPU fallback and platform limits rather than a universal speed claim:
https://mediapreviewgenerator.dev/plex-preview-thumbnails-gpu/

It also supports optional intro/credits and chapter thumbnails. Chapter images are CPU work and currently support Plex 1.43.4.x; markers need the documented Plex Pass and local/helper setup. Loudness is another optional CPU task with stricter same-machine requirements.

Demo attachment: `docs/images/player-plex.webp` followed by `docs/images/tour-publish.webp`. Caption the latter as a test setup. Tears of Steel: (CC) Blender Foundation, CC BY 3.0; link https://mediapreviewgenerator.dev/credits/.

## Jellyfin demo draft

Title: Moving trickplay generation to a separate container

I maintain Media Preview Generator. If Jellyfin's built-in trickplay generation already keeps up, it is the simpler option. If you want a separate GPU machine or share media with Plex/Emby, this Docker app can generate Jellyfin's native tiles and reuse the same extraction for the other servers.

The guide starts with Jellyfin's own tuning options, then explains the two output layouts and their permissions. The Media Preview Bridge plugin is optional for tiles beside the video and required for off-media output. Keep library trickplay extraction enabled: disabling it can remove existing tiles.
https://mediapreviewgenerator.dev/jellyfin-trickplay-gpu/

Demo attachment: `docs/images/player-jellyfin.webp`. Show one file being processed and the resulting seek preview. Include the test-server and film credits from https://mediapreviewgenerator.dev/credits/.

## Homelab demo draft

Title: One preview extraction for Plex, Emby and Jellyfin

I maintain a Docker app for media libraries shared across Plex, Emby and Jellyfin. Media Preview Generator decodes a file once, then writes each server's own preview format. New imports can arrive through Sonarr/Radarr webhooks; schedules and manual runs are also available.

The useful part of the demo is the same file in all three players, followed by each server's saved outcome. Processing can move to another machine, provided shared-media paths and output permissions line up. Intro/credits, chapter thumbnails and loudness have their own requirements; loudness specifically cannot use a remote Plex database or helper.
https://mediapreviewgenerator.dev/multi-server/

Demo attachment: `docs/images/players-3up.webp`, then the existing resolve/extract/publish tour captures. Caption player images as test servers and app captures as a fixture with open films; include https://mediapreviewgenerator.dev/credits/.

## Suggested 45-second recording sequence

1. Show one open film in Plex, Jellyfin and Emby and scrub its timeline.
2. Show the connected test servers and the required output mounts/settings without tokens or private paths.
3. Show the worker doing extraction, then expand the unified job row to show per-server results.
4. End on the matching task guide and its first-file verification steps.

Use a real isolated test run for recording. Existing still images support these draft posts; this task has not claimed or fabricated a new live processing video.
