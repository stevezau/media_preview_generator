---
title: Configuration & API Reference
heading: Configuration & API Reference
description: Every Media Preview Generator setting, environment variable, path mapping and REST API endpoint, with defaults,
  request and response examples.
---

> [Back to Docs](README.md)

Complete reference for all configuration options and REST API endpoints.

> [!IMPORTANT]
> This page is the source of truth for configuration precedence, settings, and REST API behavior.
> For installation and setup flows, use [Getting Started](getting-started.md).
> For operations, webhooks, and troubleshooting, use [Guides & Troubleshooting](guides.md).

## Contents

- [Configuration Priority](#configuration-priority)
- [Media Servers](#media-servers)
- [Processing Options](#processing-options)
- [Environment Variables](#environment-variables)
- [Web Interface Settings](#web-interface-settings)
- [Webhook Settings](#webhook-settings)
- [Intro & Credits](#intro--credits)
- [Path Mappings](#path-mappings)
- [REST API](#rest-api)
- [WebSocket Events](#websocket-events)
- [Rate Limiting](#rate-limiting)

## Related Docs

- [Getting Started](getting-started.md)
- [Guides & Troubleshooting](guides.md)
- [FAQ](faq.md)
- [Main README](https://github.com/stevezau/media_preview_generator/blob/dev/README.md)

---

## Configuration Priority

**settings.json** (at `/config/settings.json`) is the sole source of truth for application configuration. On first start, environment variables are migrated into settings.json as seed values. After that, all configuration is managed via the **Web UI** (Setup Wizard and Settings page).

**Infrastructure environment variables** remain active and are not migrated (see [Infrastructure Variables](#infrastructure-variables)).

---

## Media Servers

Every server the app talks to — any number of Plex, Emby, and Jellyfin entries
— is stored as an array under `media_servers` in `settings.json`. Managed from
the **Servers** page (or via the REST API below). Each entry has the shape:

```json
{
  "id": "plex-household",
  "type": "plex",
  "name": "Household Plex",
  "url": "http://192.168.1.100:32400",
  "enabled": true,
  "auth": {
    "method": "token",
    "token": "..."
  },
  "libraries": [
    {"id": "1", "name": "Movies", "enabled": true},
    {"id": "2", "name": "TV Shows", "enabled": true}
  ],
  "path_mappings": [
    {"remote_prefix": "/data", "local_prefix": "/media", "webhook_prefixes": []}
  ],
  "plex_config_folder": "/plex"
}
```

Per-vendor notes:

| Vendor | `auth.method` values | Extra fields |
|---|---|---|
| Plex | `token` (OAuth is the *acquisition* flow — the result is stored as `auth.token`) | `plex_config_folder` (where BIF bundles are written), `server_identity` (Plex's `clientIdentifier`, used to disambiguate webhooks) |
| Emby | `password`, `api_key` | `auth.user_id`, `auth.access_token` |
| Jellyfin | `password`, `quick_connect`, `api_key` | `auth.user_id`, `auth.access_token` |

> **Runtime state, not persisted.** Jellyfin's Media Preview Bridge plugin
> presence is probed live via `JellyfinServer.check_plugin_installed()` and
> surfaced in the `/previews-readiness` payload — it isn't stored on the
> server entry.

**Legacy flat keys.** Older single-Plex installs had top-level `plex_url`,
`plex_token`, `plex_config_folder`, and `selected_libraries`. These are
migrated into the first enabled Plex entry of `media_servers[]` on first
boot. Reads still work via a compatibility shim, so existing scripts that
query `GET /api/settings` and look at `plex_url` keep working — but new
writes should use `media_servers[]` via `/api/servers`.

> [!TIP]
> Use the Setup Wizard to sign in. Plex OAuth, Jellyfin Quick Connect, and
> Emby username/password exchange all happen through the wizard without you
> pasting tokens by hand. Exactly one "first server" is configured via the
> wizard; add more from the Servers page.

---

## Processing Options

### Per-GPU Configuration (gpu_config)

GPU settings are configured per-GPU in **Settings** → **Processing Options**. Each entry in `gpu_config` has:

| Field | Type | Description |
|-------|------|-------------|
| `device` | string | GPU device identifier (e.g. `/dev/dri/renderD128`) |
| `name` | string | Display name (e.g. "Intel UHD Graphics 630") |
| `type` | string | `nvidia`, `intel`, `amd`, `apple` |
| `enabled` | boolean | Whether this GPU is used for processing |
| `workers` | int | Number of worker threads for this GPU (0–32) |
| `ffmpeg_threads` | int | CPU threads per FFmpeg job on this GPU (0–32, 0 = no limit). Recommended: 2 |

### Other Processing Settings

| Setting | Web UI | Default | Description |
|---------|--------|---------|-------------|
| `cpu_threads` | Yes | `1` | Number of CPU worker threads (0–32) |
| `scan_workers` | Yes | `0` (Auto) | Full-scan only: how many files are checked **in parallel** for an existing preview, independent of the FFmpeg-generation cap (GPU + CPU workers). Checking is light disk I/O and does NOT add FFmpeg/GPU load. `0` = Auto (`max(32, generators)`); an explicit value is bounded to 1–256. Raise it to speed the "skip already-done files" sweep on large libraries; lower it on a single spinning HDD. |
| `thumbnail_quality` | Yes | `4` | Preview quality 1-10, lower = better quality (2 = highest) |
| `thumbnail_interval` | Yes | `10` | Interval between preview images (1–60 s). Matches Plex/BIF community convention (see sidecar `-{width}-10.bif` files). |
| `selected_libraries` | Yes | All | Library IDs to process |
| `sort_by` (per-run) | Yes | `newest` | Order items are processed: `newest`, `oldest`, `random`, or empty for Plex's natural order. Set per manual run (New Job modal) or per schedule — not a global setting. |

### Frame Reuse Cache (frame_reuse)

When the same canonical file fires multiple webhooks within the cache TTL (e.g. Sonarr fires immediately, Plex's library.new follows 30 min later), this cache reuses the FFmpeg-extracted frames across siblings instead of re-running FFmpeg. Tuned per-server under **Settings → Performance**:

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Master toggle for cross-server frame reuse |
| `ttl_minutes` | `60` | How long to keep extracted frames in the cache |
| `max_cache_disk_mb` | `2048` | Disk cap for the cache (oldest entries evicted first) |

> [!TIP]
> **Multi-disk libraries (unraid shfs, mergerfs, JBOD):** pick **Random** as the Processing Order on the New Job modal or on a scheduled full-library scan. With alphabetical order, parallel workers tend to read sequential files from the same physical disk; shuffling spreads reads across disks so disk I/O stops being the bottleneck. Webhook jobs and Recently Added scans are unaffected — they touch too few files for ordering to matter.

> [!NOTE]
> When a GPU worker can't process a file (unsupported codec,
> hardware-accelerator error, driver crash), the same worker retries
> on CPU in-place and the UI shows a warning badge with the reason.
> No separate fallback pool is needed — increase `cpu_threads` if you
> want more dedicated CPU concurrency for files that never hit the GPU.

---

## Environment Variables

### Infrastructure Variables (always active) <a id="infrastructure-variables"></a>

These are not migrated to settings.json and remain in effect:

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_DIR` | `/config` | Directory for settings.json, auth, schedules |
| `WEB_PORT` | `8080` | Web server port |
| `PUID` | `1000` | User ID (Unraid: `99`) |
| `PGID` | `1000` | Group ID (Unraid: `100`) |
| `TZ` | Host | Timezone (e.g. `America/New_York`) |
| `CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |
| `HTTPS` | `false` | Enable HTTPS for cookies |
| `DEV_RELOAD` | `false` | Enable Flask auto-reload (development) |
| `WEB_AUTH_TOKEN` | Auto-generated | Fixed authentication token (overrides wizard-set token) |
| `AUTH_METHOD` | `internal` | Set to `external` to disable built-in auth when using a reverse proxy or VPN (see below) |
| `FLASK_SECRET_KEY` | Auto-generated | Override the Flask session signing key. Auto-generated and persisted to `/config/flask_secret.key` if not set. Set this only when you need a fixed key across rebuilds. |
| `LOG_FORMAT` | `pretty` | Log output format. Set to `json` to emit one JSON object per log line — useful when shipping logs to Loki / Datadog / similar aggregators. |
| `PLEX_DATA_ROOT` | `/` | Restricts where Plex data paths can be validated to. Defaults to the whole filesystem; tighten to e.g. `/plex` if you want the path validator to refuse anything outside that root. |
| `MEDIA_ROOT` | `/` | Same as `PLEX_DATA_ROOT` but for media paths. |
| `RATELIMIT_STORAGE_URL` | `memory://` | Backend for rate-limit counters. The default in-memory store is fine for a single-container deploy; set to `redis://host:port/0` if you run behind a load balancer with multiple replicas. |

### Developer / harness-only

Not for deployment — these override paths used during development and the accuracy harness, and are never migrated
into `settings.json`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_PREVIEW_TEXTDET_MODEL` | the Docker image's own copy (`/app/models/ch_PP-OCRv4_det_infer.onnx`) | Overrides the on-screen credit text model path, for development and the `tools/markers_eval credits-text` harness. |

### External Authentication (AUTH_METHOD)

If you secure access via a reverse proxy (Authelia, Authentik, Caddy Security, nginx basic auth, etc.) or a VPN (Tailscale, WireGuard), you can disable the built-in login screen:

```yaml
environment:
  - AUTH_METHOD=external
```

When set to `external`:

- The login page is bypassed; all browser and API requests are treated as authenticated.
- Webhook authentication (`webhook_secret` / Bearer token) is **not** affected — external services like Radarr and Sonarr still need their shared secret.
- A browser's `POST`/`PUT`/`PATCH`/`DELETE` still needs the page's CSRF token (the app's pages send it). Scripts
  should send the API token (`X-Auth-Token` or `Authorization: Bearer`); see [REST API → Authentication](#authentication).
- The setup wizard still runs on first boot.
- Removing the variable (or setting it back to `internal`) instantly re-enables built-in auth.

> [!CAUTION]
> Only use `AUTH_METHOD=external` when you are certain that network-level access control is in place. Without it, anyone who can reach the web UI has full access.

### Deprecated (no longer used)

These env vars are deprecated and silently ignored at startup with a warning logged. Configure via **Settings** instead:

| Variable | Replacement |
|----------|--------------|
| `GPU_SELECTION` | Per-GPU enable/disable in Settings → Processing Options |
| `GPU_THREADS` | Per-GPU workers in `gpu_config` |
| `FFMPEG_THREADS` | Per-GPU `ffmpeg_threads` in `gpu_config` |
| `PLEX_LIBRARIES` | Per-server library toggles (Settings → Media Servers → Libraries) |
| `REGENERATE_THUMBNAILS` | Tick "Regenerate" when starting a job from the UI |
| `SORT_BY` | Pick sort order when starting a job |
| `NICE_LEVEL` | Removed — process priority is no longer configurable |
| `FALLBACK_CPU_THREADS` | Removed in v3.x — CPU retry now happens in-place inside the GPU worker |

### One-time seed values (migrated on first start)

On first run, these env vars are migrated into settings.json. After that, settings.json is the source of truth:

- `PLEX_URL`, `PLEX_TOKEN`, `PLEX_CONFIG_FOLDER`, `PLEX_VERIFY_SSL`, `PLEX_TIMEOUT`
- `PLEX_BIF_FRAME_INTERVAL` / `THUMBNAIL_INTERVAL` (alias), `THUMBNAIL_QUALITY`, `TONEMAP_ALGORITHM`, `CPU_THREADS`, `SCAN_WORKERS`
- `MEDIA_PATH`, `TMP_FOLDER`, `LOG_LEVEL`

---

## Web Interface Settings

The web UI is served by [gunicorn](https://gunicorn.org/) (a Python web server) using thread workers — Docker handles launching it, you don't need to know about this unless you're running outside Docker. Listening port and related knobs live in [Infrastructure Variables](#infrastructure-variables) — `WEB_PORT`, `CORS_ORIGINS`, `HTTPS`, and `DEV_RELOAD`.

---

## Webhook Settings

Settings for automatic preview generation when media is imported via Radarr or Sonarr.

| Setting | Default | Web UI | Description |
|---------|---------|--------|-------------|
| `webhook_enabled` | `true` | Yes | Master enable/disable for webhook processing |
| `webhook_delay` | `60` | Yes | Delay before processing (10–300 s). Incoming webhooks are queued per source; a batch runs after this many seconds with no new imports, or 10 minutes after its first webhook, whichever comes first. |
| `webhook_secret` | *(empty)* | Yes | Dedicated secret for webhook auth (falls back to API token) |
| `plex_webhook_enabled` | `false` | Yes | Enable the Plex direct webhook (`/api/webhooks/plex`). Requires Plex Pass on the server-owner account. |
| `plex_webhook_public_url` | *(empty)* | Yes | URL Plex Media Server should POST to. Defaults to the URL you registered through. Override for reverse-proxy / split-network setups. |

Webhook processing respects `selected_libraries`; paths outside unchecked libraries are ignored.

The **Recently Added Scanner** is not configured via settings keys any more — it's a first-class schedule type (see [Schedules Endpoints](#schedules-endpoints) below). Create one through the Automation page (Triggers tab) "Create default scanner" shortcut, or through the Schedules tab modal with **Scan mode → Recently added only**.

> [!IMPORTANT]
> The Plex direct webhook and Recently Added schedules trigger only on **new** library items (new `ratingKey`s). They do **not** detect in-place file upgrades — Plex keeps the same item when Sonarr/Radarr replaces a file. Use the existing Sonarr/Radarr webhooks (which fire on `On Upgrade`) for that case.

> [!TIP]
> Configure webhooks on the **Automation** page (`/automation`, Triggers tab) in the web UI. See [Webhook Integration](guides.md#webhook-integration) for setup instructions. The legacy `/webhooks` and `/schedules` URLs still work — they 302-redirect to the relevant tab.

---

## Intro & Credits

Skip Intro / Skip Credits markers for Plex, Jellyfin and Emby. See the
[Intro & Credits guide](guides.md#intro--credits) for setup and troubleshooting; this section is the settings/API
reference. Schema version 15 (`upgrade.py`) added this feature, off everywhere by default.

### Global settings (`settings.json["markers"]`)

Shared detection settings — one file is detected once, whatever the publish rule decides. Managed from
**Settings → Intro & Credits**.

```json
{
  "detect": {"intro": true, "credits": true, "recap": false},
  "credits_window": {"tv_s": null, "movie_s": null},
  "sources": [
    {"id": "chapters", "enabled": true},
    {"id": "theintrodb", "enabled": false, "api_key": ""},
    {"id": "introdb", "enabled": true},
    {"id": "skipdb", "enabled": true},
    {"id": "season_audio", "enabled": true},
    {"id": "credits_text", "enabled": true},
    {"id": "server_markers", "enabled": true}
  ]
}
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `detect.intro` | bool | `true` | TV episodes only. |
| `detect.credits` | bool | `true` | TV episodes and movies. |
| `detect.recap` | bool | `false` | Jellyfin's player is the only one with a Skip Recap button. |
| `credits_window` | object | `{"tv_s": null, "movie_s": null}` | Settings → Intro & Credits → Advanced → "Where to look for credits". How far from the end of a file on-screen credit text is searched for; credits already rolling where it begins are followed back 120 s at a time, only as far as a start the last-quarter rule and the movie cap would still keep. `tv_s` applies to TV episodes, `movie_s` to movies and files of unknown kind. Each is `null` (Automatic: last 450 s of an episode, 900 s of a movie) or one of `300`, `600`, `900`, `1200`, `1800` seconds; anything else is refused with a 400 naming the key. A key left out means Automatic, and a partial post merges over the stored value. A longer window decodes longer for every file. A window you choose also lets credits start that far before the end even where the last-quarter-of-the-file rule would refuse them, and a movie window above 15 minutes raises the 900 s cap on how far before the end a movie's credits may start. Automatic keeps both rules exactly as before. Changing it decides files again, and files already read on another window are read again. A stored value that isn't valid is treated as Automatic (logged). |
| `sources` | array | see above | Evidence sources, in checking/precedence order. Reordering in the UI reorders this array. |
| `sources[].id` | one of `chapters`, `theintrodb`, `introdb`, `skipdb`, `season_audio`, `credits_text`, `server_markers` | — | `credits_text` runs where text detection is available (see `GET /api/markers/sources/local`); it decides credits alone. `season_audio` runs where ffmpeg has chromaprint (see `GET /api/markers/sources/local`); it decides an intro alone when nothing else answers, and confirms one another source found. Its previous-season hint is stored as `season_audio_previous` evidence (not a settings id). |
| `sources[].enabled` | bool | varies | `theintrodb` defaults to `false` (used without the vendor's written permission); the rest default to `true`. |
| `sources[].api_key` | string | `""` | `theintrodb` only. Optional. `GET`/`POST /api/settings` mask a set key as `****`; posting `****` back keeps the stored key unchanged. Never logged. |

There is no publish rule setting. Every file is decided the same way: chapters decide alone unless two other
independent sources agree on something different; any other source needs an independent source to agree, except that
on-screen credit text (credits), season audio (intros) and a SkipDB `exact`/`shifted` match (intros and recaps) may
decide alone. IntroDB, TheIntroDB, the previous-season hint (`season_audio_previous`) and markers already on servers
never decide alone, and season audio (or `season_audio_previous`) with markers already on servers isn't an agreeing
pair on its own. An agreeing server marker doesn't hold season audio back (it decides as if alone, credited to
`season_audio` only); the hint with only a server's marker stays in Needs review. The removed
`publish_when` key (`"high"` / `"medium"`) is ignored when an older `settings.json` or client sends it, and schema
version 16 deletes it and has the next start queue one job, **Intro & Credits: Needs review and waiting files, decided
again** (Low priority, source `decide_again`), an ordinary Intro & Credits job over every file in Needs review and
every file whose last row waits for its item's other versions, listed when it runs. The request (settings key
`_markers_decide_again`) is cleared when that job completes; until then every start queues it again (or finds it
queued), and with Intro & Credits off on every server it waits. An intro season audio decided alone keeps asking the
online sources on their schedule: one that later disagrees sends it to Needs review.

### Per-server settings (`media_servers[].markers`)

Whether — and where — a server actually receives markers. Managed from **Servers → (server) → Edit → Intro &
Credits tab**; `library_ids` from the **Libraries tab's Intro & Credits column** (shown while `enabled` is on), which
is separate from the Previews column's `libraries[].enabled`.

```json
{
  "enabled": false,
  "library_ids": null,
  "plex": {
    "db_write_confirmed_at": null,
    "on_plex_redetect": "restore",
    "agent": {"enabled": false, "url": "", "token": ""}
  }
}
```

An Emby server's block has `"emby": {"on_emby_redetect": "restore"}` in place of `plex`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Off until turned on for this server. On a Plex server, setting this `true` requires `plex.db_write_confirmed_at` to already be set (or included in the same request) — 400 otherwise. |
| `library_ids` | array of strings \| `null` | `null` | `null` = every library except sports-type ones (name matched, whole word "sport"/"sports" — no vendor exposes an actual sports library kind). An explicit list is taken literally, including a deliberate sports library. |
| `plex` | object | *(Plex servers only)* | Absent on Emby/Jellyfin entries. |
| `emby` | object | *(Emby servers only)* | Absent on Plex/Jellyfin entries. |
| `emby.on_emby_redetect` | `"restore"` \| `"keep_emby"` | `"restore"` | Edit → Intro & Credits "When Emby has its own markers": `restore` is "Use ours", `keep_emby` is "Keep Emby's". Markers from Emby's own intro detection (Emby Premiere) or another plugin: `restore` has the Media Preview Bridge for Emby plugin replace them with ours (`ReplaceOwn`); `keep_emby` leaves a type Emby has markers of and shows ours only for the other types (row message e.g. "1 marker(s); keeping Emby's intro"). The plugin still stores ours for a kept type and shows them once Emby's are gone; the next job that checks the file records them as ours again (or writes them, when it finds none of Emby's left). Remembered per server item in `markers.db`. **A marker you adjust or lock in the Inspector overrides this setting for its type**: it is sent with `ReplaceOwn` anyway and the row says "Replaced Emby's own marker…". |
| `plex.db_write_confirmed_at` | ISO-8601 timestamp \| `null` | `null` | Set once the one-time "Send intro & credits markers to Plex?" confirmation is accepted. Clearing it while `enabled` stays `true` in the same request is rejected (400) — send `enabled: false` in the same PUT to revoke. |
| `plex.on_plex_redetect` | `"restore"` \| `"keep_plex"` | `"restore"` | Edit → Intro & Credits "When Plex has its own markers": `restore` is "Use ours", `keep_plex` is "Keep Plex's". What a job does when Plex shows markers of a decided type that aren't ours: `restore` writes ours over them; `keep_plex` keeps Plex's markers of that type on every later run (forced ones included) until the setting is switched to `restore` or Plex has none of that type left (row message e.g. "Keeping Plex's credits", or "1 marker(s); keeping Plex's credits"). Under `keep_plex`, "not ours" means not what the job would write and not what the item record says this app left there, so markers on an item with no record of that type (a first publish, a reset `markers.db`, a re-added server) are kept too. Decided per type, and remembered per server item in `markers.db`. Markers that are gone are written again either way. **A marker you adjust or lock in the Inspector overrides this setting for its type**: its rows and `pv:` key are written over Plex's own and the row says "Replaced Plex's own marker…". |
| `plex.agent` | object | `{"enabled": false, "url": "", "token": ""}` | Edit → Intro & Credits → Plex marker agent: a [Plex marker agent](#plex-marker-agent) beside a Plex on another machine, which does the database write there. Plex servers only. |
| `plex.agent.enabled` | bool | `false` | `true` needs `url` and `token` set (400 otherwise: "Set the Plex marker agent's address and shared key before turning it on"). The database-write confirmation (`plex.db_write_confirmed_at`) still applies. |
| `plex.agent.url` | string | `""` | `http` or `https` address, e.g. `http://plex-host.lan:9494`. A trailing `/` is dropped. 400 for a query string, fragment, or a username/password in it. |
| `plex.agent.token` | string | `""` | The key both sides share (the agent's `AGENT_TOKEN`). Printable ASCII, no spaces. `GET`/`POST` return a set key as `****`; posting `****` (or leaving `token` out) keeps the stored key. Never logged. |

### Plex marker agent

The small container for a Plex on another machine (`plex-marker-agent/`, image `ghcr.io/stevezau/plex-marker-agent`). Its
setup, compose file, build steps, version rules and endpoint contract are in its
[README](https://github.com/stevezau/media_preview_generator/blob/dev/plex-marker-agent/README.md); the app side is `media_preview_generator/markers/publishers/plex_remote.py`.
The agent's own settings:

| Variable | Notes |
|---|---|
| `AGENT_TOKEN` | Required. The key both sides share (Bearer token); it is what the app's `plex.agent.token` must equal. Never logged. |
| `PLEX_CONFIG_DIR` | Plex's config folder as the agent's container sees it. The database path is derived from it; the app never sends a path. |
| `PLEX_CONFIG` | Compose only: Plex's config folder on the agent's host, mounted at `/plex`. Required. |
| `AGENT_PORT` | Port to listen on. Default `9494`. |
| `AGENT_BIND` | Compose only: the host address to publish on. Default `0.0.0.0`. |
| `PUID` / `PGID` | The user that owns Plex's database. Default `1000`. |

Every request carries `Authorization: Bearer <AGENT_TOKEN>` and `X-Marker-Agent-Protocol: 1`, and every answer carries
the agent's version and the protocols it speaks; a mismatch in either direction is refused before anything is written.

### Job kind `intro_credits`

Intro & Credits jobs are a distinct `kind` (alongside `previews`) on the same `Job`/`jobs.db` row shape as preview
jobs (see [Jobs Endpoints](#jobs-endpoints)) — same queue, priority levels, pause/cancel and dashboard, routed to
`markers`-specific check/process functions instead of the preview pipeline. `config` (the job's `config_json`
column) holds:

| Key | Type | Notes |
|---|---|---|
| `kind` | `"intro_credits"` | Always this value for a markers job. |
| `source` | string | What created it: `manual`, `schedule`, `inspector` (re-detect), `inspector_season` (Season view **Publish**), `season` (a Season follow-up job), `reconcile` (Check servers), or a webhook source name (`sonarr`, `radarr`, `plex`, `retry`, …). |
| `libraries` | `[{"server_id", "library_id"}]` | Libraries to enumerate. Empty with no `file_paths` = every library Intro & Credits goes to. |
| `file_paths` | array of strings | Explicit files/folders instead of libraries (webhook follow-ups, Inspector re-detect, retries). |
| `follows_job_id` | string \| `null` | The preview job this job waits for before taking a job-gate slot (webhook follow-ups only). Episodes that later joined the job (see below) don't wait for their own preview jobs. |
| `files_sealed` | bool | Present once a webhook follow-up or Season job has read its `file_paths`: no more files join it after that. |
| `force` | bool | Re-detect files already decided, asking every source again. |
| `webhook_item_id_hints` | `{path: {server_id: item_id}}` | Item ids a vendor webhook already supplied, so the job skips a lookup. |
| `retry_attempt` | int | Present only on a retry job: which retry this is (1-based). |
| `verify_chain` | bool | Present only on a retry queued by a verify job or by another retry in its chain: it queues no verify job. |
| `chain_attempt` | int | Present only on a verify job queued by a retry: the retries already used, so the verify job's own retry goes on counting. |
| `retry_delay` | int | Present only on a retry or verify job: seconds waited before it took a slot. |
| `retry_not_before` | ISO-8601 timestamp | Present only on a retry or verify job: the due time (survives a restart without waiting again in full). |
| `verify` | bool | Present only on a verify job: the delayed check of files published after they were replaced. It queues no further verify job, and doesn't retry a file gone from disk. |
| `reconcile` | bool | Present only on a Check servers job: it lists the files of drifted published items (and of items whose last publish failed, files with a locked marker a server never received, and decided files to ask servers again about) instead of libraries or paths. |
| `paused_by_schedule` | bool | Set when a schedule's stop time paused the job: that schedule's next start (or **Run now**) resumes it. Any resume, and a pause by hand, clears it (a job cancelled while so paused keeps it); a Re-run drops it. |

Retries (files not yet on disk, not yet in a server's library, or on a Plex whose Plex Pass check didn't answer)
reuse the webhook preview-retry backoff (`webhook_retry_count` / `webhook_retry_delay`) and cap at **500 files** per
retry job — a bigger backlog waits for the next run. A retry job's `file_paths` are the paths the job was given (a
webhook's own paths, not the first mapped disk's), with their `webhook_item_id_hints`. A job that publishes to
replaced files also queues one verify job (`verify: true`, named "Verify: …") for them, due after three times the
first retry delay (at least 600 s); none when `webhook_retry_count` is 0. Only jobs for sent files (webhooks and
retries, not `manual`/`inspector` or library runs) queue one. A job whose read-back of a server failed completes with
the warning `Couldn't check what N file(s) show on <server>`. A job where an online source's daily budget ran out
partway through completes with one warning per source that ran out (see `GET /api/markers/sources/usage` above for
the same state in Settings), and doesn't queue a retry for those files — nothing was stored for the source, so the
next scheduled or manual run for the same files asks it again on its own. For TheIntroDB only, the files it left with
a type undecided (`needs_review` / `no_evidence`) join one waiting LOW-priority job (`source: "theintrodb_recheck"`,
named "TheIntroDB recheck: N files", at most 500 files) due 5 minutes after the next 00:00 UTC (`retry_not_before`);
when it runs it drops files decided since, and lists nothing if TheIntroDB has been turned off. TheIntroDB also isn't
asked about a series (keyed by the tmdb/tvdb/imdb id it's sent) for 7 days once 3 of its episodes got "no entry"
while none has an answer: none recorded in `series_lookups`, and no file under the show's folder with a stored
TheIntroDB answer holding a marker. The 7 days run from the pause's start (`series_pauses` in `markers.db`); "no
entry" answers during it don't extend it, the next pause needs 3 new ones after it ends, an answer with a marker ends
it, and a forced run (`force`, Inspector re-detect) always asks.

When a job's season step finds that other episodes of the same season could now be decided differently (their season
intro-chapter check or season audio answer is out of date; an episode season audio never answered for, such as one no
server takes for Intro & Credits, has no answer to go out of date), the job queues a **Season job** for them once it
completes: `source: "season"`, named `Season: <show> · <season folder>` (or `Season: N seasons`), at Low priority —
Normal when the job was a webhook follow-up (not its retries or verify job, which queue theirs at Low), never ahead of
the job that asked — and capped at 500 files; more are left to their next run. Episodes already listed by a Season job
that hasn't read its files yet, or by a webhook follow-up that hasn't started, aren't queued again; new ones join such
a Season job at the same priority while it stays within 500 files. A Season job queues no further Season job, verify
job or retry for a file gone from disk. A cancelled job queues none. The requests live in memory: a restart before the
job completes drops them, and the season's next run asks again. Every job that completes, except Season, retry and verify
jobs, then starts a background cleanup once it has given back its slot (at most one running, and at most one start an
hour). It checks up to 2,000 fingerprinted files on disk for at most 60 s, those checked longest ago first, and clears
the cached fingerprints (and the matches made with them) of files gone from a folder that still exists. The app log
line, not the job's, is `Cleared the cached audio fingerprints of N file(s) no longer on disk`. Plex/Emby/Jellyfin webhooks arrive one episode at a
time: an episode whose season folder a webhook follow-up that hasn't read its files yet already covers joins that
follow-up (renamed "Intro & Credits · N files") while it stays within 500 files, instead of queuing another. A joined
episode doesn't wait for its own preview job: markers don't need previews.

**Check servers** (`reconcile: true`, `source: "reconcile"`, named "Intro & Credits · Check servers") is created by
`POST /api/markers/reconcile`, the Dashboard's Start New Job dialog, or a schedule with `config.reconcile` (see
[Schedules](#post-apischedules)); nothing schedules it by default. LOW priority unless the request or schedule sets one.
Only one is queued or running at a time: asking again (a **Re-run** of a finished one included) returns that job. It
reads back every item this app published (`item_publish_state` with status `written`) on each enabled server with
Intro & Credits on, 500 items per call (Plex one item per connection under the lock proof; Jellyfin and Emby one
request per item, stopping a server after 20 failed reads in a row; a read that got no HTTP answer isn't followed by an
"is the item gone?" request), and lists only the files of items that aren't `ours` any more (`missing`, `replaced`,
`versions_changed`, `gone`, or a kept type on a server now set to `restore`). For `missing`, `replaced` and
`versions_changed` the Plex item's current version files (optimized copies left out, mapped through the server's path
mappings) are listed too. Items whose last publish failed (status `failed`) aren't read back: their files are listed 1
day after the failure, then 2, 4, 8 and 16 days after each retry, at most 5 retries per failure (a success, or a new
failure after one, starts over); an item whose files don't fit the run's remaining files waits untaken. A pause during
the read-back gives the job's gate slot back until resume. It
also lists files with decided credits or preview whose stored answer from an enabled server (Intro & Credits on or not)
is empty or unusable and that show none of ours on that server's item, re-reading that server's own markers once the
answer is 1 day old, then 2, 4, 8 and 16 days after each re-read that stays empty or fails, and not after the fifth; a
file taken for a server isn't taken again within a day. At most 500 files per run (100 of them kept for those re-reads
while more drifted files wait); drifted items take turns across runs, with the warning "N more changed file(s) are
checked on a later run". An item the server no longer has is dropped quietly when no runnable file here belongs to it,
or when its file's row was "not in library" and the server confirmed the item missing: that file gets one retry job
(the normal retry above, `source: "reconcile"`, attempt 1; like `manual` jobs, it and its retries queue no not-on-disk
retry and no verify job) and the item is dropped only once that retry is queued. A run cancelled or failed before
then, or with `webhook_retry_count` 0, drops nothing, so the next run confirms the item again. Check servers queues no
other retry (a later run lists what still waits). Other warnings: `Skipped <server>: <reason>`,
`Couldn't check <server>`, `Couldn't check <server>: no connection to it`, `Couldn't read what N item(s) show on <server>`.
With nothing to list it completes at once (log "Every server checked still shows what this app
published").

### Outcome keys

Per-file outcomes (`markers.outcomes.FileOutcome`, shown in the job's Files panel and progress breakdown):

| Key | Label | Meaning |
|---|---|---|
| `markers_published` | Markers written | The job changed what at least one server shows (a forced restore included); another marker type may still need review, and the reason names it |
| `markers_up_to_date` | Up to date | Every enabled server already showed these markers (read back before saying so) |
| `markers_waiting` | Waiting | A server hasn't indexed the file yet, Plex didn't answer its Plex Pass check, or a Plex item's versions don't yet agree |
| `markers_needs_review` | Needs review | At least one marker wasn't sent (the sources disagree, or the only answer can't decide alone), and the job wrote nothing else for the file. The server row's message gives each such marker's reason |
| `markers_none` | No markers found | No source found an intro or credits for this file |
| `markers_no_owners` | No server with Intro & Credits on | No enabled server with Intro & Credits on holds this file |
| `skipped_file_not_found` | Not Found | File not found on disk |
| `markers_skipped` | Skipped | Every server that owns this file can't take markers right now (see the [capability states](guides.md#troubleshooting-intro--credits) — a plugin missing, Plex not ready, etc.), or the file is a trailer or other extra (reason "Extras aren't checked for markers") |
| `failed` | Failed | Processing failed |

Per-server row statuses (`markers.outcomes.ServerStatus`) use the same `markers_written` / `markers_up_to_date` /
`markers_needs_review` / `markers_skipped` / `markers_waiting` / `failed` keys, plus `markers_none` (nothing to
publish on that server). A file's overall outcome shows what still needs something, first match wins: any server
failed → failed; any server waiting with a retry queued (not indexed yet, Plex Pass unconfirmed) → waiting; any
written → published (or waiting while another server waits for the item's versions); any marker in review → needs
review; any server waiting → waiting; any up to date → up to date; any with nothing to publish → no markers; otherwise
skipped. So one server that is still
waiting (or failed) is never hidden behind another server that was written or is up to date. Retries and verify jobs
read the per-server rows, not this outcome.

Extras are never checked: a file with a Plex extra suffix (`-trailer`, `-featurette`, `-behindthescenes`,
`-deleted`, `-interview`, `-scene`, `-short`, `-other`, `-sample`) or directly inside an extras folder (`Trailers`,
`Featurettes`, `Extras`, `Behind The Scenes`, `Deleted Scenes`, `Interviews`, `Scenes`, `Shorts`, `Other`, `Samples`)
is `markers_skipped` before it is probed or looked up, whether it came from a folder, a webhook or a library listing.

### Decided-by counts

An Intro & Credits job's `progress.marker_sources` (in `GET /api/jobs/{id}` and the live `job_progress` event;
`null` on other jobs, and on an Intro & Credits job until its first file finishes) counts the files each source
decided, per marker type:

```json
{"intro": {"chapters": 40, "theintrodb+skipdb": 6}, "credits": {"chapters": 40, "credits_text": 9, "theintrodb+server_markers": 3}}
```

A file counts once per decided marker type, under one group: its marker's sources (source ids from Settings, joined
with `+`, in your source order). A marker a chapter set counts as `chapters` even when other sources agreed (a chapter
decides on its own; they only confirmed or trimmed it); markers already on servers (`server_markers`, importer copies
included) are named only when they were the one other opinion a single source needed; the user's own marker is
`user`. A file counts when its run ends with any outcome but `failed`, so a file whose every server was skipped still
counts what its sources decided, while a type in review, and a file not found, with no server, or an extra, never
count. A job revived after a restart counts the files it carries from what the markers store holds for them. The
Dashboard shows these as **Decided by** under the job's per-server breakdown, biggest group first, at most five groups
per type (the rest add up under "other").

### Intro & Credits Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/markers/jobs` | Start an Intro & Credits job |
| GET | `/api/markers/servers/{server_id}/status` | This server's Intro & Credits status (Edit tab) |
| GET | `/api/markers/sources/usage` | Today's online-lookup usage per source |
| GET | `/api/markers/item` | Inspector data for one file |
| POST | `/api/markers/item/redetect` | Re-run Intro & Credits for one file, asking every source again |
| POST | `/api/markers/item/markers` | Save your own markers for one file, lock them and publish to every owning server |
| DELETE | `/api/markers/item/markers` | Unlock one or more marker types of one file |
| GET | `/api/markers/season` | Inspector Season view data for one episode's season |
| POST | `/api/markers/season/publish` | Queue an Intro & Credits job for the episodes of one episode's season |
| GET | `/api/markers/sources/local` | Whether season audio matching and on-screen credit text can run in this container |
| POST | `/api/markers/reconcile` | Queue Intro & Credits · Check servers |

All of them require the same `X-Auth-Token` / `Authorization: Bearer` auth (or a logged-in session) as the rest of the
API. A script sending the API token can call any of the `POST` routes directly; from a signed-in browser they also
need the page's CSRF token, like every other state-changing route (see [Authentication](#authentication)).

#### POST /api/markers/jobs

**Request:** `{"libraries": [{"server_id", "library_id"}]}` or `{"file_paths": [...]}` — omit both for every library
Intro & Credits goes to. Optional: `priority` (`1`-`3` or `high`/`normal`/`low`, default `low`), `force` (re-detect
files already done), `library_name` (job title).

**Response:** `201` with the created job; `400` for an invalid body (both `libraries` and `file_paths` given, a bad
`priority`, …); `503` when the config directory isn't writable.

#### GET /api/markers/servers/{server_id}/status

**Response:** `200` with `server_id`, `server_type`, `enabled`, `settings` (as stored), `capability` (`{state,
message, details, warning}`, checked as if Intro & Credits were already on — one of `ready`, `disabled`,
`needs_confirmation`, `needs_plugin`, `plugin_outdated`, `needs_pass`, `needs_local_db`,
`agent_unavailable` (the Plex marker agent didn't answer, refused the key, is the wrong version or is beside another Plex),
`needs_plex_detection_once`, `unsupported_schema`, `unreachable`, `misconfigured`, or `unknown` when the check itself
failed; `warning` is `""` unless a `ready` Plex couldn't confirm Plex Pass, in which case jobs wait instead of
writing; on Emby, `needs_plugin` carries `details.catalog_listed`: `true` when Emby's plugin catalog lists the plugin,
`false` when it doesn't, `null` when the catalog couldn't be read; on a reachable Emby, `details.intro_skip_registered`
is Emby's `GET /Registrations/dvr` `IsRegistered` (whether its Emby Premiere key lets viewers skip intros), left out
when that couldn't be read or Emby rejected the credentials), `can_show` (marker types this server type can
display) and `libraries` (with each one's default selection).
`404` for an unknown server; `500` with a JSON error when the status can't be built. A server turned off on the
Servers page isn't contacted (`capability.state` is `disabled`).

#### GET /api/markers/sources/usage

**Response:** `200` with `{source_id: {day, used, limit, remaining, has_key, low_priority_exhausted, resets_at}}`
for `theintrodb`, `introdb` and `skipdb`. `has_key` is only meaningful for `theintrodb`; the key itself is never
returned. `low_priority_exhausted` is `true` once a full-library backfill (LOW priority) lookup against that source
would be refused right now — no budget left, or only the share reserved for higher-priority jobs remains.
`resets_at` is always the daily budget's day boundary in the user's words (`"00:00 UTC"`), never the source's own
reset header (TheIntroDB's can't be trusted — see `ratelimit.py`).

#### GET /api/markers/item

**Query:** either `path` (a file inside a server library), or `server_id` + `item_id`, optionally with
`version_file`: the version's file as the Preview Inspector's search row gave it (needed for Plex, where every version
of an item shares its id). By id, the version asked for is the one `version_file` names, else the one whose own id is
`item_id` (a Jellyfin version, a merged Jellyfin item's primary version, or an Emby item, whose media sources list
its other versions too), else the item's only version; another version is never opened in its place. A Plex
`version_file` that names none of the item's versions any more counts as a version that isn't here. Only a Plex item
with several versions asked for without `version_file` opens the first version whose file is on this disk.

**Response:** `200` with `known`, `canonical_path`, `duration_ms`, `is_movie`, `decisions` (by marker type),
`evidence` rows (each with its `label`: a chapter's title, season audio's `"10/10"`, or `""`), and `servers` (one row per owning server: `current` markers as read live, `published` markers that
are ours, `plan` — `will_add` / `will_replace` / `will_remove` / `up_to_date` / `waiting` / `keeps_plex` (Plex's own
detection replaced ours and `on_plex_redetect` is `keep_plex`) / `keeps_emby` (the same for Emby and `keep_emby`) /
`not_enabled` / `nothing_to_publish` / `unknown` — with `plan_reason` (names the types the server keeps, e.g. "Keeping
Plex's credits", on any plan; on Emby also "Emby skips to the end of the file" when decided credits end before the file
does), and `version_count`: a
Plex item's number of versions (`Media` entries other than optimized copies), which share one marker set (`null` for
other servers or when it can't be read); a server whose state can't be read gets a degraded row with `error` set
instead of failing the whole response). `400` when the path isn't a file inside a server library, the
query is incomplete, or `item_id` isn't shaped like an id that server's type uses (a Plex rating key is digits
only, e.g. `42`, not `/library/metadata/42`; a Jellyfin or Emby id is up to 36 ASCII hex digits and dashes starting
with a hex digit, or ASCII digits; checked before any server is contacted). `404` for an unknown server, or a
`server_id`+`item_id` with no file on this app's disk: `{"error": "This version's file isn't on this disk", "reason":
"version_not_here"}` when the server has the version asked for but its file isn't here. `409` when the
server is disabled. `500` with a JSON error when the file's data can't be built.

#### POST /api/markers/item/redetect

**Request:** `{"path"}`.

**Response:** `202` with `{"job_id"}` — a HIGH-priority, forced, single-file job. If this same file already has a
re-detect queued or running, that job's id is returned instead of starting a second one (a double-click, or clicking
again before the first finishes, doesn't spend the online sources' daily budget twice). `400` when the path isn't a
file inside a server library. `503` when the config directory isn't writable.

#### POST /api/markers/item/markers

Saves your own markers for one file, locks them, and publishes them to every owning server inside the request. Saving is
locking: there is no adjusted-but-unlocked marker. The save lands before any server is contacted, so a server that fails
can't lose it.

**Request:** the file, as `path` (inside a server library) or `server_id` + `item_id` (+ `version_file`, as in
`GET /api/markers/item`), plus `markers`: a non-empty list with one entry per type.

```json
{
  "path": "/media/tv/Show/Season 1/Show - S01E02.mkv",
  "markers": [
    {"type": "intro", "start_ms": 30000, "end_ms": 92000},
    {"type": "credits", "start_ms": 1320000, "end_ms": null}
  ]
}
```

`type` is `intro`, `credits`, `recap` or `preview`. `start_ms` and `end_ms` are whole milliseconds; `end_ms` `null` or
left out means "runs to the end of the file", and an end up to 2 s past the file's length is clamped to it. Only two
bounds apply to your own marker: it must be inside the file and must end after it starts. The 3 s minimum, the intro
length cap and the position windows that catch a wrong source are not applied.

**Response:** `200` with the file's stored markers and one row per owning server:

```json
{
  "canonical_path": "/media/tv/Show/Season 1/Show - S01E02.mkv",
  "duration_ms": 1440000,
  "markers": {
    "intro": {"type": "intro", "start_ms": 30000, "end_ms": 92000, "locked": true, "locked_at": "2026-09-21T10:15:02+00:00"}
  },
  "servers": [
    {
      "server_id": "plex-default",
      "server_name": "Plex",
      "server_type": "plex",
      "result": "written",
      "message": "…",
      "can_show": ["intro", "credits"],
      "cant_show": [],
      "notes": [],
      "replaced_own": []
    }
  ]
}
```

`markers` holds every stored marker of the file, keyed by type. Per server, `result` is `written`, `unchanged`,
`waiting`, `failed`, `not_enabled` (Intro & Credits is off there), `nothing_to_publish` or `needs_review`; `message` is
the same wording a job's row carries. `can_show` is what that server type can display and `cant_show` the saved types it
can't (Plex and Emby take no recap or preview). `notes` are per-field notes: on Emby an edited credits `end` is accepted
and published start-only, and the note says so. `replaced_own` lists the types whose own markers that server lost to
your lock although it is set to keep its own (`keep_plex` / `keep_emby`).

The publish is bounded: each call to a server waits at most 8 s, and a server the publish hasn't started on 25 s after
it began is not contacted: its row is `failed` with "Couldn't publish to this server in time; the next Intro & Credits
run publishes it". These are per-call limits and a start gate, not a cap on the whole request. A job already running on
the same file gives a `waiting` row, "Intro & Credits is running for this file; the next run publishes your marker".

**Errors:** `400` for a body it can't save from (a duplicate type, a marker outside the file or ending before it
starts, a non-integer time), a path outside every server library, or a type no enabled owner can show. `404` for an
unknown server or item. `409` when the server is off, no server with Intro & Credits on has the file
(`"reason": "no_marker_owner"`), the file was never analysed (`"reason": "not_analysed"`) or changed on disk since it was
analysed (`"reason": "file_changed"`). `503` when the config directory isn't writable.

#### DELETE /api/markers/item/markers

Drops your lock on one or more marker types. Nothing is published: the servers keep showing your times until the next
run decides those types again.

**Request:** the file as above, plus `types`: a non-empty list of `intro`, `credits`, `recap`, `preview`.

```json
{"path": "/media/tv/Show/Season 1/Show - S01E02.mkv", "types": ["intro"]}
```

**Response:** `200` with `canonical_path`, `unlocked` (the requested types that were locked; the rest were already
unlocked), `markers` (the file's remaining stored markers) and `decisions`, by type, for the requested types that have
one: `{"intro": {"status": "needs_review", "reason": "unlocked; the next run decides this type again"}}`. `400`, `404`
and `503` as above; `409` when the server is off or the file was never analysed.

#### GET /api/markers/season

**Query:** `path` (a TV episode file inside a server library; any episode of the season).

Read from this app's own marker database only — no server is contacted, so a whole season is one quick request (the
per-episode `GET /api/markers/item` stays the place for what a server shows right now).

**Response:** `200` with:

- `folder`, `show`, `season` — the season folder, and the show and season as the Publish job names them: the show
  folder's name (the folder itself when episodes sit straight in it) and `"Season N"` or `"Specials"` from the
  episode's name.
- `servers` — the enabled servers holding the episode, in server order: `server_id`, `server_name`, `server_type`,
  `markers_enabled` (Intro & Credits is on there and this library is selected).
- `episodes` — the episodes matched as one season (same folder and season number; at most the 40 nearest in a flat
  folder of hundreds; extras left out), each with `path`, `name`, `episode` (`"E01"`), `known` (the app has looked at
  it), `duration_ms`, `intro` and `credits` (`{status, reason, marker, proposed}` as in `GET /api/markers/item`),
  `needs_review` (any marker type in Needs review, recap and preview included) with `review_reason` (the first such
  type's reason, `""` when none),
  `evidence` chips (`[{source, label}]`: the sources with intro or credits evidence, markers already on servers left
  out; only season audio has a `label`, e.g. `"10/10"`) and `servers` dots (`{server_id: {state, message}}`, `state`
  one of `ok` — last publish wrote our markers, `none` — nothing of ours there or never published, `waiting`,
  `failed`, `skipped`, or `off` — Intro & Credits off there, or this episode's library isn't selected or is excluded).
- `counts` — `episodes` (the episodes listed), `total_episodes` (the season's size before the 40-nearest cap), `ready`
  (at least one decided marker of any type: what Publish sends, even when another type is in Needs review) and
  `needs_review` (any type in Needs review).

`400` `{"error": "Path is not a file inside any server library"}` (also for a missing `path`) or `{"error": "Not a TV
episode"}` (no `SxxEyy` in its name). `500` `{"error": "Couldn't build the Season view for this file"}`.

#### POST /api/markers/season/publish

**Request:** `{"path"}` — any episode of the season.

**Response:** `202` with `{"job_id"}` — a NORMAL-priority, not forced Intro & Credits job named
`Intro & Credits: <show> · Season N` (or `· Specials`) for exactly the episodes `GET /api/markers/season` lists for that path (same
folder and season number, at most the 40 nearest), so a folder holding several seasons only sends this one: decided
episodes are sent to every server that doesn't show them yet, undecided ones are checked again. While a Publish of the
same episodes is still queued or running, its id is returned instead of starting a second one. For example
`{"path": "/tv/Show/S01E02.mkv"}` in a flat `Show` folder that also holds `S02E01.mkv` queues "Intro & Credits: Show ·
Season 1" for `S01E01.mkv` and `S01E02.mkv` only, and answers `202` `{"job_id": "…"}`. `400` `{"error": "The request
body must be a JSON object with a path"}`, `{"error": "Path is not a file inside any server library"}` or `{"error":
"Not a TV episode"}`. `503` when the config directory isn't writable.

#### GET /api/markers/sources/local

**Response:** `200` with `{"season_audio": {"available", "ffmpeg", "message"}, "credits_text": {"available",
"message"}}` — season audio matching needs an ffmpeg with the chromaprint muxer (jellyfin-ffmpeg in the amd64
image; the arm64 image has none). `ffmpeg` is the binary found (`null` when none), and `message` says why it isn't
available (`""` when it is): no ffmpeg lists the muxer, or one didn't answer the check (then it is asked again
after 10 minutes), e.g.
`{"season_audio": {"available": true, "ffmpeg": "/usr/lib/jellyfin-ffmpeg/ffmpeg", "message": ""}}`.

`credits_text.available` is `false` when ONNX Runtime/OpenCV aren't installed, the text detection model isn't at
its expected path or isn't the expected file, or the check didn't answer (checked again after 10 minutes; arm64
has no GPU path but is still available on the CPU). `message` names the reason (`""` when available), e.g.
`{"credits_text": {"available": true, "message": ""}}` or
`{"credits_text": {"available": false, "message": "Needs the text detection model, which the Docker image includes; it isn't at /app/models/ch_PP-OCRv4_det_infer.onnx"}}`.

A check that fails outright answers `"available": null` for that source only (with `"ffmpeg": null` for season audio
and a `message` such as "Couldn't check whether credit text can run here"); the other source's answer is unaffected,
and Settings leaves a `null` row as it is.

#### POST /api/markers/reconcile

**Request:** optional JSON body `{"priority"}` (`1`-`3` or `high`/`normal`/`low`, default `low`); no body is fine.

**Response:** `202` `{"job_id": "…", "already_queued": false}` for a new Intro & Credits · Check servers job, or
`{"job_id": "…", "already_queued": true}` with the one already queued or running (whatever priority was asked for),
plus `"paused": true` when that job is paused.
`200` `{"job_id": null, "reason": "Intro & Credits is off on every server"}`. `400` `{"error": "The request body must
be JSON"}`, `{"error": "The request body must be a JSON object"}` or `{"error": "priority must be 1, 2, 3, high, normal
or low"}`. `503` when the config directory isn't writable (checked before the body).

> [!NOTE]
> There is no separate "install the plugin" route for Intro & Credits — the Edit tab's Install/Update button (Jellyfin
> and Emby) calls the existing `POST /api/servers/{id}/install-plugin` (see
> [Servers](#servers-beyond-the-basics-in-multi-media-server-endpoints)).

---

## Path Mappings

> [!IMPORTANT]
> Essential for Docker deployments where your media server sees files at
> different paths than this container does. Path mappings are stored
> **per-server** — each Plex / Emby / Jellyfin entry in `media_servers[]`
> carries its own list — because different servers can mount the same media
> at different paths.

### Why Path Mappings?

| Component | Sees files at |
|-----------|---------------|
| Media server (Plex / Emby / Jellyfin) | `/data/media/Movies/film.mkv` |
| This Container | `/media/Movies/film.mkv` |

Without mapping, you'll see "Skipping as file not found" errors.

### Configuration (Web UI)

Open **Servers → Edit** on the server that needs mapping, and add rows in the
Path Mappings section. Each row has:

- **Path on server** — The folder path the media server reports for the file
  (e.g. `/data`). Called `remote_prefix` in the API.
- **Path in this app** — The folder path this app uses for the same files
  (e.g. `/mnt/data`). Called `local_prefix` in the API.
- **Webhook path (if different)** — Only needed when Sonarr, Radarr, Tdarr,
  etc. use a different path than the media server (e.g. they use `/data`
  while Plex uses `/data_disk1`). Leave blank if they match. Called
  `webhook_prefixes` in the API.

Add as many rows as you need (e.g. one per disk when the server has multiple
roots). Each server manages its own list independently.

### Legacy env (semicolon pair)

| Variable | Description |
|----------|-------------|
| `PLEX_VIDEOS_PATH_MAPPING` | Path(s) as Plex sees them; semicolon-separated for multiple roots (seed value, first-boot only) |
| `PLEX_LOCAL_VIDEOS_PATH_MAPPING` | Path as this app sees them (seed value, first-boot only) |

The saved `path_mappings` on each `media_servers[]` entry take precedence.
Existing semicolon-based values are converted into mapping rows on the first
enabled Plex entry at migration time.

### When a server uses multiple roots (e.g. mergerfs)

If a server has several roots (e.g. `/data_disk1`, `/data_disk2`) but
Sonarr/Radarr see one path (`/data`):

- Add one row per server root, each with the same **Path in this app** (e.g. `/data`).
- In **Webhook path**, enter `/data` on one of the rows so imports from
  Sonarr/Radarr still match.

### Examples

| Situation | Path on server | Path in this app | Webhook path |
|-----------|----------------|------------------|--------------|
| Different paths in Docker | `/data` | `/mnt/data` | *(blank)* |
| Multiple disks, Sonarr sees one path | `/data_disk1` | `/data` | `/data` |
| Same (second disk) | `/data_disk2` | `/data` | *(blank)* |

### How to Find Your Paths

1. **Plex path**: Plex Web → Settings → Libraries → Edit → Folders.
2. **Emby path**: Emby Dashboard → Libraries → Edit → Folders.
3. **Jellyfin path**: Jellyfin Dashboard → Libraries → Edit → Folders.
4. **Container path**: Check your `-v` volume mount.

### No Mapping Needed

If both Plex and this container see files at the same path (e.g., both use `/media`), skip this configuration.

### Exclude Paths

Under the same **Media path mapping** settings you can add **Exclude paths**: paths or folders to skip for preview generation. These are applied to the **local** path (as this app sees the file after path mapping).

- **Path prefix** — Any file under this folder is skipped (e.g. `/mnt/media/archive` skips everything under that path).
- **Regex** — The full local path is matched against the pattern (e.g. `.*\.iso$` to skip ISO files).

Add one row per path or pattern. Excluded items are not queued for full-library runs and are skipped for webhook-triggered runs.

---

## REST API

All API endpoints (except `/api/health` and `/api/setup/status`) require authentication.

### Authentication

Include the authentication token in requests using one of these methods:

```bash
# X-Auth-Token header
curl -H "X-Auth-Token: YOUR_TOKEN" http://localhost:8080/api/jobs

# Authorization Bearer header
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8080/api/jobs
```

Get your token from [Authentication Token](getting-started.md#authentication-token), or set a fixed token with `WEB_AUTH_TOKEN`.

**Scripts: always send the API token in one of these headers.** A request with a valid API token needs nothing
else. A wrong one gets `401 {"error": "Authentication required"}`.

**CSRF token (browser sessions).** A request that relies on the signed-in browser session instead of the API token
must also send the page's CSRF token on every `POST`, `PUT`, `PATCH` and `DELETE`. This stops a page on another site
from using your signed-in browser to change settings or start jobs. The app's own pages do it for you: every page
carries the token in `<meta name="csrf-token">` and sends it as the `X-CSRFToken` header, and the login form sends it
as a hidden `csrf_token` field. Without it (or with a stale one) the request is refused with `400`:

```json
{"error": "This page's security token is missing or out of date. Reload the page and try again. Scripts: send the API token in an Authorization: Bearer or X-Auth-Token header instead."}
```

- `GET` requests never need it.
- The webhook receivers (`POST /api/webhooks/radarr`, `/sonarr`, `/sportarr`, `/custom`, `/plex`, `/incoming` and
  `/server/{server_id}`) never need it: they check their own webhook secret on every call.
- The setup wizard sends it too, including before setup is finished when its routes need no sign-in.
- With `AUTH_METHOD=external`, browser requests still need it; scripts behind the proxy should send the API token.
- A browser request that says it came from another origin (the browser-set `Sec-Fetch-Site` header is anything but
  `same-origin` or `none`) is refused even with a valid token: another app on the same host, on a different port,
  counts as the same site for cookies. Requests without the header (older browsers, scripts) rely on the token alone.
- The token lasts as long as the browser's signed-in session, up to 7 days after its last visit. Signing in starts a
  new session, so nothing from before it (a token another page may have read) stays valid. With
  `AUTH_METHOD=external` this app never sees a sign-in, so the `Sec-Fetch-Site` check above is what refuses such a
  token; its **Logout** only clears this app's session (sign out at your proxy or VPN).
- Signing out (`POST /logout`, the nav's **Logout**; opening `/logout` only asks), or changing the API token, ends
  **this browser's** session, and a tab still open from before has to be reloaded. Other browsers stay signed in:
  each session lives in a signed cookie in its browser, and the app keeps no list of sessions to end, even when the
  API token changes. To sign every browser out, delete `flask_secret.key` from the config folder (or change
  `FLASK_SECRET_KEY`, if you set it) and restart the container: every session cookie stops being valid, and each
  browser signs in again with the current token.

### Setup & Settings Endpoints

#### GET /api/setup/status

Check if setup is complete. **No authentication required.**

```json
{
  "configured": true,
  "setup_complete": true,
  "current_step": 0,
  "plex_authenticated": true
}
```

#### GET /api/setup/state

Get current setup wizard state.

```json
{
  "step": 2,
  "data": {
    "server_name": "My Plex Server"
  }
}
```

#### POST /api/setup/state

Save setup wizard progress.

**Request:**

```json
{
  "step": 2,
  "data": {
    "server_name": "My Plex Server"
  }
}
```

#### POST /api/setup/complete

Mark setup as complete. Returns `{"success": true, "redirect": "/"}`.

#### GET /api/setup/token-info

Get information about the current authentication token (used by Step 5 of the setup wizard).

```json
{
  "env_controlled": false,
  "token": "abc123xyz...",
  "token_length": 43,
  "source": "config"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `env_controlled` | boolean | Whether token is set via `WEB_AUTH_TOKEN` env var |
| `token` | string | The current authentication token |
| `token_length` | number | Length of the token |
| `source` | string | Either `"environment"` or `"config"` |

#### POST /api/setup/set-token

Set a custom authentication token during setup.

**Request:**

```json
{
  "token": "my-custom-password",
  "confirm_token": "my-custom-password"
}
```

Returns `{"success": true}` on success, or `{"success": false, "error": "..."}` with details:

- `"Tokens do not match."`
- `"Token must be at least 8 characters long."`
- `"Token is controlled by WEB_AUTH_TOKEN environment variable and cannot be changed."`

#### GET /api/settings

Get current settings.

```json
{
  "plex_url": "http://192.168.1.100:32400",
  "plex_token": "****",
  "plex_name": "My Server",
  "plex_config_folder": "/plex",
  "selected_libraries": ["1", "2"],
  "media_path": "/media",
  "plex_videos_path_mapping": "",
  "plex_local_videos_path_mapping": "",
  "path_mappings": [
    {"remote_prefix": "/data", "local_prefix": "/mnt/data", "webhook_prefixes": []}
  ],
  "gpu_config": [
    {"device": "/dev/dri/renderD128", "name": "Intel UHD 630", "type": "intel", "enabled": true, "workers": 4, "ffmpeg_threads": 2}
  ],
  "cpu_threads": 2,
  "thumbnail_interval": 10,
  "thumbnail_quality": 4
}
```

> **path_mappings keys**: `remote_prefix` is the canonical key as of the multi-server refactor (works for Plex, Emby, and Jellyfin). The legacy `plex_prefix` is still accepted as an alias on read; new writes should use `remote_prefix`.

#### POST /api/settings

Update settings. Send only the fields to change.

```json
{
  "gpu_config": [{"device": "/dev/dri/renderD128", "enabled": true, "workers": 4, "ffmpeg_threads": 2}],
  "cpu_threads": 2,
  "thumbnail_interval": 10,
  "plex_url": "http://192.168.1.100:32400"
}
```

`markers` (the Intro & Credits block) may be partial: posted keys merge over the stored block — `detect` key by key,
`sources` by `id` (a list naming every source sets their order; a shorter one updates those sources where they are;
naming a source twice is a `400`). `api_key: "****"` keeps the stored TheIntroDB key and `""` clears it. `GET
/api/settings` returns the block with the key masked.

### Plex OAuth Endpoints

#### POST /api/plex/auth/pin

Create a new Plex OAuth PIN.

```json
{
  "id": 12345,
  "code": "ABCD1234",
  "auth_url": "https://app.plex.tv/auth#?clientID=...&code=ABCD1234"
}
```

#### GET /api/plex/auth/pin/{id}

Check if PIN has been authenticated. Returns `{"authenticated": true, "auth_token": "..."}` or `{"authenticated": false, "auth_token": null}`.

#### GET /api/plex/servers

Get list of user's Plex servers.

```json
{
  "servers": [
    {
      "name": "My Server",
      "machine_id": "abc123",
      "host": "192.168.1.100",
      "port": 32400,
      "ssl": false,
      "owned": true,
      "local": true
    }
  ]
}
```

#### GET /api/plex/libraries

Get libraries from connected Plex server. Optional query parameters: `url`, `token`.

```json
{
  "libraries": [
    { "id": "1", "name": "Movies", "type": "movie" },
    { "id": "2", "name": "TV Shows", "type": "show" }
  ]
}
```

#### POST /api/plex/test

Test Plex connection. Request: `{"url": "...", "token": "..."}`. Returns `{"success": true, "server_name": "...", "version": "..."}`.

### Processing state (global pause)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/processing/state` | Get global processing pause state |
| POST | `/api/processing/pause` | Set global pause (no new jobs start; active job stops dispatch after current tasks) |
| POST | `/api/processing/resume` | Clear global pause |

**GET /api/processing/state** — Response: `{"paused": true}` or `{"paused": false}`. State is persisted and survives restarts.

**POST /api/processing/pause** — Response: `{"paused": true}`.

**POST /api/processing/resume** — Response: `{"paused": false}`.

### Jobs Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs` | List all jobs |
| POST | `/api/jobs` | Create new job |
| GET | `/api/jobs/{id}` | Get job details |
| POST | `/api/jobs/{id}/cancel` | Cancel job |
| POST | `/api/jobs/{id}/pause` | Intro & Credits job: pauses that job only (`200` with the job; `409` `{"error": "Only running jobs can be paused"}` when it isn't running). Preview job: global pause (delegates to `/api/processing/pause`). `404` for an unknown id. |
| POST | `/api/jobs/{id}/resume` | Intro & Credits job: resumes that job only (`200` with the job plus `processing_paused`, true while **Pause all** still holds it; `409` `{"error": "Only running jobs can be resumed"}` when it isn't running). Preview job: global resume (delegates to `/api/processing/resume`). `404` for an unknown id. |
| DELETE | `/api/jobs/{id}` | Delete job |

#### GET /api/jobs

```json
{
  "jobs": [
    {
      "id": "job-123",
      "status": "running",
      "library_id": "1",
      "library_name": "Movies",
      "progress": 45,
      "total_items": 100,
      "completed_items": 45,
      "created_at": "2024-01-15T10:30:00Z",
      "started_at": "2024-01-15T10:30:05Z"
    }
  ]
}
```

#### POST /api/jobs

**Request:** `{"library_id": "1", "library_name": "Movies"}`

**Response:** `{"id": "job-123", "status": "pending", "message": "Job created successfully"}`

#### GET /api/jobs/{id}

```json
{
  "id": "job-123",
  "status": "running",
  "library_id": "1",
  "library_name": "Movies",
  "progress": 45,
  "total_items": 100,
  "completed_items": 45,
  "failed_items": 0,
  "created_at": "2024-01-15T10:30:00Z",
  "started_at": "2024-01-15T10:30:05Z",
  "workers": [
    {
      "id": 0,
      "type": "gpu",
      "status": "working",
      "current_item": "Movie Title"
    }
  ]
}
```

### Schedules Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/schedules` | List schedules |
| POST | `/api/schedules` | Create schedule |
| PUT | `/api/schedules/{id}` | Update schedule |
| DELETE | `/api/schedules/{id}` | Delete schedule |
| POST | `/api/schedules/{id}/run` | Run now |

#### POST /api/schedules

**Cron request — full library scan (default):**

```json
{
  "name": "Nightly Movies",
  "library_id": "1",
  "cron_expression": "0 2 * * *"
}
```

**Interval request — full library scan:**

```json
{
  "name": "Every 4 Hours",
  "library_id": "1",
  "interval_minutes": 240
}
```

**Recently Added scanner schedule:**

```json
{
  "name": "Recently Added Scanner",
  "library_id": null,
  "interval_minutes": 15,
  "enabled": true,
  "config": {
    "job_type": "recently_added",
    "lookback_hours": 1
  }
}
```

`priority` (optional, `1`|`2`|`3`) pins the schedule. Omit it — or send
`null` on a `PUT` — to leave the schedule unpinned, which is the default: a
`recently_added` schedule then follows the global `incoming_job_priority`
setting (High out of the box), and a `full_library` schedule runs at Normal.
On `PUT`, an absent `priority` key leaves an existing pin untouched; an
explicit `null` clears it.

`config.job_type` accepts:

- `"full_library"` *(default — optional, omit to get the same behaviour)* — schedule runs a full library scan via the standard job pipeline, processing every item in `library_id` that's missing previews.
- `"recently_added"` — schedule runs a Recently Added scan instead. Requires `config.lookback_hours` (float, clamped to 0.25–720). Scans only items added within the lookback window (Plex `addedAt`, Emby/Jellyfin `DateCreated`), queuing each through the webhook job pipeline. When `library_id` is `null`, the scan falls back to the globally selected libraries in Settings (or every supported library when no global filter is set); when set, only that section is scanned. Works for Plex, Emby, and Jellyfin — each vendor's processor implements `scan_recently_added` against its native API.
- `"intro_credits"` — schedule creates an [Intro & Credits](#intro--credits) job (`kind=intro_credits`) instead of a preview job, for the schedule's libraries (every library Intro & Credits goes to when none are chosen). LOW priority unless the schedule sets one. Skipped while an earlier Find markers job from the same schedule is still pending or running. With `config.reconcile: true` it queues Intro & Credits · Check servers instead (every server; libraries and server don't apply; the UI shows it as "All servers"), skipped while any Check servers job is still pending or running. A start tick (or `POST /api/schedules/{id}/run`) first resumes every Intro & Credits job of the schedule that its stop time paused, whichever of the two it is (the schedule may have been switched since), and then queues nothing that tick; the check above applies only when it resumed nothing. A job paused by hand (`POST /api/jobs/{id}/pause`) is never resumed by a tick, and a stop tick doesn't take over a pause made by hand; the job's config carries `paused_by_schedule: true` from a stop-time pause until the next resume, pause by hand, or the job's end. Deleting a schedule leaves its paused jobs paused and logs a WARNING naming each; so does a `PUT` that switches `config.job_type` to a kind its start ticks don't resume (`intro_credits` ↔ `full_library`, or either to `recently_added`), for each job its stop time paused. While a Check servers job runs, its config also carries `check_servers_listing` (the files it listed), so a run revived after a restart checks those same files; the key is dropped when the job ends.

### System Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/health` | No | Health check |
| GET | `/api/system/status` | Yes | System status (GPUs, workers, job counts) |
| GET | `/api/system/config` | Yes | Current configuration |
| GET | `/api/libraries` | Yes | Aggregated library list across every configured server |

### Multi-Media-Server Endpoints

For full design and per-vendor details see [Multi-Media-Server](multi-server.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/servers` | List configured servers (auth redacted) |
| POST | `/api/servers` | Add a new server (auto-generates id) |
| GET | `/api/servers/<id>` | Fetch one server (auth redacted) |
| PUT/PATCH | `/api/servers/<id>` | Update; redacted auth values are kept. A body without `markers` keeps the stored Intro & Credits block; a partial `markers` merges over it, `plex`/`emby` sub-block included (a key sent as `null` wins; `"markers": null` resets the block to the defaults, Intro & Credits off) |
| DELETE | `/api/servers/<id>` | Remove a server |
| POST | `/api/servers/test-connection` | Test a candidate config without saving |
| POST | `/api/servers/<id>/refresh-libraries` | Re-fetch the server's library list |
| GET | `/api/servers/owners?path=...` | Diagnose which servers own a given path |
| GET | `/api/servers/<id>/output-status?path=...&item_id=...` | Whether publisher output files exist for a path on this server. `item_id` is required for **Plex** servers (the bundle hash is keyed by item id); optional for Emby and Jellyfin. Plex requests without `item_id` return `{"needs_item_id": true}`. |
| POST | `/api/servers/auth/emby/password` | Username+password → Emby token |
| POST | `/api/servers/auth/jellyfin/password` | Username+password → Jellyfin token |
| POST | `/api/servers/auth/jellyfin/quick-connect/initiate` | Begin Quick Connect ceremony |
| POST | `/api/servers/auth/jellyfin/quick-connect/poll` | Poll for approval |
| POST | `/api/servers/auth/jellyfin/quick-connect/exchange` | Exchange approved secret for token |
| GET | `/api/servers/<id>/health-check` | Per-server settings audit. Returns `{vendor, issues, issue_count, fixable_count}`; `issues[]` carries `{flag, label, severity, current, recommended, rationale, library_id, library_name, fixable}`. Works for Plex (server-wide prefs via `/:/prefs`), Emby and Jellyfin (per-library `LibraryOptions`). Replaces the older Jellyfin-only `/jellyfin/trickplay-status` route. |
| POST | `/api/servers/<id>/health-check/apply` | Apply settings to one or more flags. Three body shapes (all backwards-compatible): `{}` = fix every issue at recommended value; `{"flags": ["FlagName", ...]}` = fix only named flags toward recommended; `{"set": [{"flag": "X", "value": true\|false, "library_ids": ["id"]\|null}]}` = set each flag to the EXPLICIT value (enables disable-direction toggles on the Setup Health card). Returns `{ok, results}` keyed `<library_id>:<flag>` (or `:<flag>` for server-wide prefs). |
| GET | `/api/servers/<id>/previews-readiness` | Unified readiness payload for every vendor. Returns `{vendor, overall_ok, sections: [{id, title, docs_anchor, ok, severity, checks: [{id, label, docs_anchor, tooltip, ok, severity, current, recommended, actions: {enable?, disable?}, reason, meta}]}]}`. Drives the unified Setup Health card on the Edit Server modal. See the [Setup Health guide](guides/previews-readiness.md). |
| POST | `/api/servers/<id>/install-plugin` | Jellyfin and Emby (400 for Plex). Jellyfin: adds the Media Preview Bridge manifest URL to Jellyfin's plugin repos, queues the package install, and restarts Jellyfin. Returns `{ok, steps: [{step, ok, detail}], error}`. Emby: installs Media Preview Bridge for Emby from Emby's own plugin catalog and restarts Emby; when the catalog doesn't list it, answers `ok: false, manual: true` (install the DLL by hand). Returns `{ok, steps, error, manual}`. |
| POST | `/api/servers/<id>/plex-marker-detection` | Plex only. Setup Health's per-library **Turn off** for Plex's own intro/credits detection. Body `{"library_id": "2", "prefs": ["enableIntroMarkerGeneration", "enableCreditsMarkerGeneration"]}` (one or both); sets them off with `PUT /library/sections/{id}/prefs` for that library only, never Plex's server-wide prefs. 400 for any other pref, a library outside the server's Intro & Credits selection, or a non-Plex server. Returns `{ok, library_id, prefs}` or `{ok: false, error}`. |
| POST | `/api/servers/<id>/uninstall-plugin` | Jellyfin only. Removes the Media Preview Bridge plugin (`DELETE /Packages/{GUID}`; 404 treated as success — already gone) and restarts Jellyfin. Repo URL stays in place for possible re-install. Same response shape as `/install-plugin`. |
| GET | `/api/bif/servers/<id>/search?q=<query>` | Multi-server BIF Viewer search; returns `preview_kind` (`bif` or `trickplay`) per result so the viewer renders the right format |
| GET | `/api/bif/trickplay/info?server_id=...&path=...` | Parse a Jellyfin trickplay manifest + report sheet metadata |
| GET | `/api/bif/trickplay/frame?server_id=...&sheets_dir=...&index=N&tile_width=10&tile_height=10` | Slice and serve a single thumbnail JPEG from a trickplay tile sheet |

### Webhook Endpoints

Inbound webhook endpoints for Radarr/Sonarr/Custom integration. Webhook endpoints accept `X-Auth-Token`, `Authorization: Bearer`, or a configured `webhook_secret`.

> [!TIP]
> The new **universal webhook URL** at `POST /api/webhooks/incoming` auto-detects the vendor (Plex / Emby / Jellyfin / Sonarr / Radarr / templated path) so you only need one URL across every server. Falls back to per-server URLs at `POST /api/webhooks/server/<server_id>` for ambiguous setups (rare). See [Multi-Media-Server — Webhook configuration](multi-server.md#webhook-configuration-per-vendor) for details.

#### POST /api/webhooks/incoming

Universal webhook router. Inspect the request body, classify it as Plex / Emby / Jellyfin / Sonarr / Radarr / generic-`{path: ...}`, and dispatch to every server that owns the resolved canonical path. Works alongside the per-vendor URLs below — you can keep using those, or replace them all with this one.

Returns 200 with the dispatch result (`status`, `kind`, `canonical_path`, `publishers[]`, `frame_count`) on success, 202 with `status: "ignored"` for noise events the router intentionally drops (e.g. Jellyfin `PlaybackStart`), 400 for unrecognised payloads, 401 for bad auth, 413 for payloads above the 1 MiB cap.

#### POST /api/webhooks/server/{server_id}

Same as `/api/webhooks/incoming` but pins dispatch to one configured server. Useful when two configured servers (e.g. Plex + Jellyfin) own the same path and the source can't tell them apart — the URL itself carries the disambiguation. Returns 404 when the server id isn't configured.

#### POST /api/webhooks/radarr

Receive a Radarr webhook payload.

**Download event request:**

```json
{
  "eventType": "Download",
  "movie": {
    "title": "Inception",
    "folderPath": "/movies/Inception (2010)"
  }
}
```

**Response (202):** `{"success": true, "message": "Processing queued for 'Inception'"}`

**Test event:** `{"eventType": "Test"}` → **Response (200):** `{"success": true, "message": "Radarr webhook configured successfully"}`

#### POST /api/webhooks/sonarr

Same authentication and response patterns as Radarr.

**Download event request:**

```json
{
  "eventType": "Download",
  "series": { "title": "Breaking Bad" },
  "episodeFile": { "relativePath": "Season 01/S01E01.mkv" }
}
```

#### POST /api/webhooks/custom

Receive a custom webhook payload from any external tool (Tdarr, scripts, etc.). Accepts one or more file paths to process.

**Single file request:**

```json
{
  "file_path": "/media/movies/Movie (2024)/Movie.mkv"
}
```

**Multiple files request:**

```json
{
  "file_paths": [
    "/media/tv/Show/Season 01/S01E01.mkv",
    "/media/tv/Show/Season 01/S01E02.mkv"
  ],
  "title": "Optional display label"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | One of `file_path` / `file_paths` | Single absolute file path |
| `file_paths` | array of strings | One of `file_path` / `file_paths` | Multiple absolute file paths |
| `title` | string | No | Display label for history/jobs |
| `eventType` | string | No | Set to `"Test"` to verify connectivity |

**Response (202):** `{"success": true, "message": "Processing queued for 1 file"}`

**Test event:** `{"eventType": "Test"}` → **Response (200):** `{"success": true, "message": "Custom webhook configured successfully"}`

**Error (400):** `{"success": false, "error": "Payload must include 'file_path' (string) or 'file_paths' (array of strings)"}`

#### POST /api/webhooks/plex

Receive a native Plex webhook (Plex Pass feature). Plex POSTs `multipart/form-data` with a `payload` part containing the JSON event body. Only `library.new` events trigger work; other events (`media.play`, `media.rate`, `library.on.deck`, etc.) are acknowledged with 200 and ignored.

The endpoint also accepts a synthetic `test.ping` event used by the **Test reachability** button on the Automation page (Triggers tab).

**`library.new` payload (excerpt):**

```json
{
  "event": "library.new",
  "owner": true,
  "Metadata": {
    "ratingKey": "153037",
    "type": "movie",
    "title": "Some Movie",
    "Media": [{ "Part": [{ "file": "/data/movies/Some Movie/Some Movie.mkv" }] }]
  }
}
```

When `Media[].Part[].file` is missing from the payload (Plex doesn't always include it), the app fetches the item by `ratingKey` via the Plex API to recover the file paths.

**Authentication:** same as the other webhook endpoints — `X-Auth-Token` header, `Authorization: Bearer`, or HTTP Basic password.

> [!IMPORTANT]
> Plex's `library.new` webhook is wired through the same code path as mobile push notifications. If push notifications are disabled on your Plex server, library events are silently dropped — enable them under Plex Web → Settings → General (toggle *Enable mobile push notifications*). See the [Auto-trigger from Plex guide](guides.md#auto-trigger-from-plex-no-sonarrradarr) for full details.

#### POST /api/settings/plex_webhook/register

Register the Plex direct webhook (`/api/webhooks/plex`) with the user's plex.tv account, using the configured Plex token.

**Request body:**

```json
{ "public_url": "http://your-host:8080/api/webhooks/plex" }
```

`public_url` is optional — when omitted the server uses `<request scheme>://<host>/api/webhooks/plex`.

**Response (200):** `{"success": true, "registered_in_plex": true, "public_url": "..."}`

**Errors:**

- `400` — token missing
- `403` — Plex Pass required (`reason: "plex_pass_required"`)
- `502` — registration call to plex.tv failed

#### POST /api/settings/plex_webhook/unregister

Remove the Plex direct webhook from the user's plex.tv account and turn off the local toggle. Returns `{"success": true, "registered_in_plex": false}`.

#### GET /api/settings/plex_webhook/status

Probe live state. Returns the configured public URL, whether it is currently registered with Plex, and Plex Pass detection.

```json
{
  "enabled_in_settings": true,
  "registered_in_plex": true,
  "public_url": "http://your-host:8080/api/webhooks/plex",
  "default_url": "http://your-host:8080/api/webhooks/plex",
  "has_plex_pass": true,
  "error": null,
  "error_reason": null
}
```

#### POST /api/settings/plex_webhook/test

Self-POST a synthetic `test.ping` payload to the configured public URL to verify reachability. The receiving endpoint records a "test" history entry. Returns `{"success": true, "status_code": 200, ...}` on success.

To run a Recently Added scan immediately, call `POST /api/schedules/<id>/run` on the scanner schedule — it's a standard user schedule now, not a dedicated settings endpoint.

#### GET /api/webhooks/history

Get recent webhook events (newest first, max 100). For events with `status: "triggered"` (a debounced batch that was processed), the response may include `job_id`, `path_count`, and `files_preview` (up to 20 basenames) so the UI can show which files were in the batch. File lists are also available on the Dashboard job queue (expand with the chevron next to "Sonarr: N files" / "Radarr: N files" / "Custom: N files") and on the Automation page (Triggers tab) Activity Log (expand triggered rows).

```json
{
  "events": [
    {
      "timestamp": "2026-02-12T10:30:00+00:00",
      "source": "sonarr",
      "event_type": "Download",
      "title": "sonarr",
      "status": "triggered",
      "job_id": "abc-123",
      "path_count": 3,
      "files_preview": ["S01E01.mkv", "S01E02.mkv", "S01E03.mkv"]
    }
  ]
}
```

#### DELETE /api/webhooks/history

Clear all webhook history. Returns `{"success": true}`.

### Error Responses

All errors follow this format:

```json
{
  "error": "Error message",
  "code": "ERROR_CODE"
}
```

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `UNAUTHORIZED` | 401 | Missing or invalid authentication token |
| `NOT_FOUND` | 404 | Resource not found |
| `VALIDATION_ERROR` | 400 | Invalid request data |
| `SERVER_ERROR` | 500 | Internal server error |

---

## WebSocket Events

The dashboard uses Flask-SocketIO with WebSocket for real-time updates. The client connects to the `/jobs` namespace.

```javascript
const socket = io('/jobs', {
    transports: ['websocket', 'polling'],
    reconnection: true
});
```

| Event | Description |
|-------|-------------|
| `job_progress` | Job progress update |
| `job_complete` | Job finished |
| `job_error` | Job failed |
| `worker_update` | Worker status change |

Example payload:

```json
{
  "event": "job_progress",
  "data": {
    "job_id": "job-123",
    "progress": 50,
    "completed": 50,
    "total": 100,
    "current_item": "Movie Title"
  }
}
```

---

## Complete Endpoint Index

The sections above cover the endpoints most integrations need. This index
catalogues the remaining routes — mostly internal APIs the web UI calls, but
documented here so you can drive them from scripts if you want. All require
the same `X-Auth-Token` / `Authorization: Bearer` auth as the rest of the API
unless noted.

### Auth & token management

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/auth/status` | Session auth state — used by the UI on page load |
| POST | `/api/auth/login` · `/api/auth/logout` | Session login/logout (cookie-based, for the browser: needs the page's CSRF token; scripts send the API token instead) |
| POST | `/logout` | Sign this browser out (the nav's **Logout**, with the page's CSRF token). `GET /logout` only shows a "Sign out?" page |
| POST | `/api/token/regenerate` | Rotate the stored API token (disabled when `WEB_AUTH_TOKEN` is set) |
| POST | `/api/token/set` | Set a custom token — min 8 chars; returns `{success: false, error: ...}` on validation failure |

### Jobs (beyond the basics in [Jobs Endpoints](#jobs-endpoints))

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/jobs/manual` | Submit one or more absolute paths — `{"file_paths": ["/a.mkv", "/tv/Show"], "force_regenerate": false, "priority": 2, "server_id": "..."}`. Directories are expanded to the video files inside; bypasses library scan. |
| GET | `/api/media/search` | Backs the Manual Generation typeahead. `?q=` (min 2 chars), optional `?server_id=` to scope to one server. Fans across enabled servers and returns `{results: [{kind: "show"\|"movie"\|"episode", title, year, paths: [local container paths], child_count, servers: [{id, name, type}]}]}`. Shows resolve to their folder(s); the same item reported by several servers is merged into one row (union of paths + servers). |
| POST | `/api/jobs/{id}/priority` | Change a pending/running job's priority (`{"priority": 1\|2\|3}`; 1 = high) |
| POST | `/api/jobs/{id}/reprocess` | Re-run a finished job with the same config: `201` with the new job (an Intro & Credits job keeps its schedule), `409` while it's pending or running. A Check servers job is queued like `POST /api/markers/reconcile` and answers the same way (`202` `{"job_id", "already_queued"}`, reusing one already queued or running). A Re-run clears the global pause. |
| POST | `/api/jobs/{id}/retry-now` | Skip the retry back-off on a chain-head job whose next attempt is currently in the back-off countdown. Returns 200 + `{"fired": true, ...}` on success, 409 when no retry is pending, 400 if the job isn't a chain head. |
| POST | `/api/jobs/{id}/fire-webhook-now` | Skip the debounce window on a webhook-batch job that's still waiting to dispatch. Looks up the in-memory batch by `job_id` and cancels its threading timer, then dispatches the same callback synchronously. 202 on success, 404 when the job has no live pending batch (already fired, never had one, or container restart cleared the in-memory dict). |
| GET | `/api/jobs/{id}/logs` | Paginated log stream — `?offset=&limit=` (limit capped at 5000); or legacy `?last=N` for the tail |
| GET | `/api/jobs/{id}/files` | Per-file outcomes — paginated `?page=&per_page=` (per_page capped at 500), plus optional `?outcome=` and `?search=` filters. The underlying per-job JSONL is itself soft-capped at 5000 rows; past that, a `truncated` marker row appears and aggregate counts remain in `progress.outcome`. |
| POST | `/api/jobs/clear` | Delete completed/failed jobs from the queue |
| GET | `/api/jobs/stats` | Totals grouped by status |
| GET | `/api/jobs/workers` | Current worker-pool snapshot (type, state, current item) |
| POST | `/api/workers/add` · `/api/workers/remove` | Add or remove pool workers live (`{"worker_type": "CPU"\|"GPU", "count": N}`). A CPU change is also saved as the **CPU workers** setting, just like the dashboard's +/- buttons. A busy worker that's removed finishes its current file first. GPU changes aren't saved: the next Settings save or a restart puts back the per-GPU counts from Settings. |
| POST | `/api/jobs/{id}/workers/add` · `/api/jobs/{id}/workers/remove` | Per-job worker adjustment while the job runs. Not saved: the next Settings save or a restart puts back the saved counts. |

### Schedules

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/schedules/{id}/enable` · `/api/schedules/{id}/disable` | Toggle a schedule without deleting it |
| GET · POST | `/api/quiet-hours` | Read / write the multi-window quiet-hours policy (days of week + start/stop times) |

### Settings & setup

| Method | Endpoint | Description |
|---|---|---|
| PUT | `/api/settings/log-level` | Change runtime log verbosity (`{"level": "DEBUG"\|"INFO"\|...}`) |
| POST | `/api/settings/validate-local-path` | Pre-flight a mount/volume path before saving (exists + readable) |
| POST | `/api/settings/validate-plex-config-folder` | Pre-flight a Plex config folder (looks for `Cache/Media/Metadata`) |
| GET | `/api/settings/backups` | List rolling settings.json backup snapshots |
| POST | `/api/settings/backups/restore` | Restore a prior settings.json snapshot |
| POST | `/api/setup/skip` | Skip the setup wizard (advanced — saves `setup_complete=true` with minimal state) |
| POST | `/api/setup/validate-paths` | Pre-flight wizard path fields in bulk |

### Servers (beyond the basics in [Multi-Media-Server Endpoints](#multi-media-server-endpoints))

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/servers/{id}/test-connection` | Re-test a saved server's live connection |
| PATCH | `/api/servers/{id}/enabled` | Enable/disable a server entry without deleting (`{"enabled": true\|false}`) |
| POST | `/api/servers/{id}/vendor-extraction` | Toggle vendor-side preview generation (Plex `enableBIFGeneration`, Emby/Jellyfin trickplay extraction) |
| GET | `/api/servers/{id}/vendor-extraction/status` | Current aggregate state (e.g. "stopped on 3/5 libraries") |
| GET | `/api/servers/{id}/trickplay-readiness` | *(Jellyfin)* Legacy audit endpoint — kept for scripts. New integrations should use [`/previews-readiness`](#multi-media-server-endpoints). |
| POST | `/api/servers/{id}/trickplay-fix-all` | *(Jellyfin)* Apply all recommended trickplay flags |

### Webhooks (beyond the basics in [Webhook Endpoints](#webhook-endpoints))

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/webhooks/sportarr` | Sonarr-compatible feed for [Sportarr](https://github.com/Sportarr/Sportarr) (falls back to flat `filePath`) |
| GET | `/api/webhooks/pending` | Batches currently debouncing — per-source key, countdown, and queued paths |
| POST | `/api/webhooks/pending/{debounce_key}/fire-now` | Skip the debounce timer and dispatch the batch immediately |

### System & diagnostics

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/system/timezone` | Container TZ + source (env var vs `/etc/localtime`) |
| GET | `/api/system/media-servers` | Multi-server aggregate health (per-vendor connection + library counts) |
| GET | `/api/system/vulkan` | Vulkan ICD probe result (device, driver version, ICD files loaded) |
| GET | `/api/system/vulkan/debug` | Plain-text diagnostic bundle for attaching to GitHub issues (DV Profile 5 troubleshooting) |
| POST | `/api/system/rescan-gpus` | Re-probe all GPUs (refreshes `gpu_config` candidate list) |
| GET | `/api/system/version` | App version + commit SHA + build date |
| GET | `/api/system/browse` | Folder picker: lists sub-directories of `?path=` (default `/`). `?include_files=1` also returns video files (each entry has `is_dir`); `?show_hidden=1` includes dot-entries. System dirs (`/proc`, `/sys`, …) are denied. |
| GET | `/api/system/notifications` | In-app notification list (health checks, deprecations, warnings) |
| POST | `/api/system/notifications/{id}/dismiss` | Session-only dismiss |
| POST | `/api/system/notifications/{id}/dismiss-permanent` | Persistent dismiss (stored in settings) |
| POST | `/api/system/notifications/reset-dismissed` | Clear all permanent dismissals |
| GET | `/api/system/whats-new` | Release-notes viewer payload (version + changes since last-seen) |
| POST | `/api/system/whats-new/dismiss` | Mark the current version's notes as seen |
| GET | `/api/system/browse` | Safe filesystem browser, scoped to `MEDIA_ROOT` / `PLEX_DATA_ROOT` (used by path pickers) |
| GET | `/api/logs/history` | Persisted log history — `?limit=` (default 500, max 2000), `?level=` (minimum level filter), `?before=` (ISO-8601 timestamp cursor for older-than paging) |

---

## Rate Limiting

| Endpoint | Limit |
|----------|-------|
| `POST /login` | 5 per minute |
| `POST /api/auth/login` | 10 per minute |
| Default | 200 per day, 50 per hour |

Rate limit headers are included in responses:

- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`

---

## Next Steps

- Complete install and setup in [Getting Started](getting-started.md)
- Use operational workflows in [Guides & Troubleshooting](guides.md)

---

[Back to Docs](README.md) | [Main README](https://github.com/stevezau/media_preview_generator/blob/dev/README.md)
