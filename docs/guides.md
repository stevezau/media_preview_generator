---
description: Use the Media Preview Generator web UI, trigger jobs from Sonarr, Radarr or Plex webhooks, handle HDR and Dolby Vision, and fix common problems.
---

# Guides & Troubleshooting

> [Back to Docs](README.md)

Guides for the web interface, automation and webhooks, HDR handling, and troubleshooting.

> [!IMPORTANT]
> This page is the source of truth for web operations, webhook workflows, and troubleshooting.
> For installation and first-time setup, use [Getting Started](getting-started.md).
> For exact configuration values and API contracts, use [Configuration & API Reference](reference.md).

## Contents

- [Web Interface](#web-interface)
- [Previews readiness (per-check toggles & explanations)](guides/previews-readiness.md)
- [Webhook Integration](#webhook-integration)
- [Auto-trigger from Plex (no Sonarr/Radarr)](#auto-trigger-from-plex-no-sonarrradarr)
- [Intro & Credits](#intro--credits)
- [HDR & Dolby Vision](#hdr--dolby-vision)
- [Troubleshooting](#troubleshooting)
- [FAQ](faq.md)

## Related Docs

- [Getting Started](getting-started.md)
- [Configuration & API Reference](reference.md)
- [FAQ](faq.md)
- [Main README](https://github.com/stevezau/media_preview_generator/blob/dev/README.md)

---

## Web Interface

Dashboard for managing preview generation jobs, settings, and schedules.

### Setup Wizard

When you first access the web interface, you'll be guided through a **Setup Wizard** that supports **Plex, Emby, and Jellyfin**:

1. **Choose your media server** — pick **Plex**, **Emby**, or **Jellyfin**. The chosen card expands inline:
   - **Plex** — sign in via Plex OAuth (no manual token copying), or paste a URL + token if you prefer.
   - **Emby** — enter the server URL and an API key.
   - **Jellyfin** — enter the URL and run a **Quick Connect** ceremony (or paste an API key).
2. **Server & Libraries** *(Plex only)* — pick which Plex server (if you have several) and which libraries to enable. Emby/Jellyfin flows skip this step; libraries are managed later from **Settings → Media Servers**.
3. **Path Configuration** *(Plex only)* — confirm the Plex application data folder where BIF files are written, plus any media path mappings. Emby and Jellyfin write their previews next to each video (so the media must be mounted read-write for them), and only nudge the server over HTTP, so this step is skipped for those flows.
4. **Processing Options** — per-GPU enable/workers/FFmpeg threads, CPU workers, thumbnail interval, and quality.
5. **Security** — view or replace your access token (optional).

After setup completes, you'll land on the dashboard. You can add additional servers (any vendor, any number) at any time from **Settings → Media Servers** without re-running the wizard.

### Accessing the Dashboard

1. Start the container
2. Open `http://YOUR_SERVER_IP:8080`
3. Get your authentication token using [Authentication Token](getting-started.md#authentication-token)
4. Enter the token to log in

### Dashboard Features

**Connection Status** — shows every configured server (Plex, Emby, Jellyfin):

- **Connected** — server name, vendor, and available GPUs displayed
- **Not configured** — link to the setup wizard or **Servers** page to add one

**Job Management:**

- **Start new jobs** — process all libraries or specific ones
- **View progress** — real-time progress with WebSocket updates
- **Cancel jobs** — stop running jobs
- **Job history** — view completed/failed jobs

> [!NOTE]
> Jobs queue with priority (1 = high, 2 = normal, 3 = low). The dispatcher runs
> up to the configured concurrent-job cap; extra jobs sit in **Pending** and the
> gate releases them in priority order as slots free up. Manual, webhook, and
> scheduled jobs all share the same gate. To hard-stop everything, use
> **Pause Processing** — the global pause is persisted and survives restarts.
>
> Priority also decides who gets the next free worker **file by file**, so a
> High job overtakes a running full scan without cancelling or pausing it — the
> scan simply stops being fed new files until the High job drains. Whenever the
> concurrent-job cap is above 1, the last slot is reserved for High-priority
> work so an incoming webhook never has to wait out a multi-hour scan.

### Letting new imports jump the queue

A full-library regeneration can run for hours. **Settings → Processing Options →
Incoming job priority** controls the priority stamped on the jobs this app
creates for itself when new media lands — webhook deliveries from
Sonarr/Radarr/Plex/Emby/Jellyfin, and **Recently Added** sweeps. It defaults to
**High**, so a freshly imported episode is processed within one file of arriving
even while a full scan is running.

Manual and scheduled full scans keep whatever priority you gave them (Normal by
default), which is what leaves room for the High jobs to overtake.

A schedule only follows this setting while its own **Job Priority** is
**Default (from Settings)** — the value new schedules start on. Pick High,
Normal, or Low there to pin that schedule instead, and the global setting stops
applying to it.

Recently Added schedules created before this option existed are switched to
**Default (from Settings)** automatically on first start after upgrading. Those
carried an explicit *Normal* only because the old dialog had no way to say "no
pin", so there was no choice to preserve. Schedules you had deliberately set to
High or Low keep their setting, and so does any schedule you pin after
upgrading — the migration runs once.

Set the global setting to **Normal** to go back to strict
first-come-first-served ordering. The reserved slot is independent of this
setting — it is held back whenever the concurrent-job cap is above 1, so a job
you pin to High by hand can still overtake a running scan.

**Manual Generation:**

The **Manual Trigger** button generates previews for specific media on demand — no Sonarr/Radarr webhook or library scan needed. There are three ways to pick what to process, and they can be mixed:

- **Search** — start typing a show, movie, or episode name. The app searches your enabled servers and lists matches grouped by **Shows / Movies / Episodes**, each tagged with a badge showing which server(s) it came from. Pick a **show** to generate previews for every episode in it; pick a **movie** or **episode** for just that file. The path comes straight from the server, so you never have to know the in-container path (the common cause of "missing on disk" confusion).
- **Browse** — open the folder picker to navigate your mounted media and select either a **folder** (expanded to every video inside) or an individual **video file**.
- **Or paste paths manually** — the collapsible box still accepts one absolute container path per line, for power users or scripts.

Each pick becomes a removable chip; **Start Job** processes them all. The **Publish to which server?** dropdown scopes both the search and where previews are published — leave it on *All servers* to publish to whoever owns each file, or pick one server to limit both.

**Pause / Resume (global):**

- **Pause Processing** — Stops all processing system-wide: no new jobs will start (manual, scheduled, or webhook), and the current job stops dispatching new tasks. Files already mid-process finish first, then workers go idle (a "soft" pause — nothing is killed mid-frame). Use this to cap bandwidth or pause overnight.
- **Resume Processing** — Clears the global pause; new jobs can start and the current job resumes dispatching.
- Controls appear in the **Current Job** header and to the left of **Clear Jobs** in the Job Queue. State is persisted and survives restarts.
- An **Intro & Credits** job also has its own **Pause** button on its row: files already in progress finish, the job
  hands its slot back so other jobs can run, and it waits until you click its **Resume**. **Pause Processing** holds
  it too, but **Resume Processing** doesn't resume a job you paused on its own.

**Scheduling:**

The Dashboard shows a compact "Schedules" teaser with the next upcoming run and a total count. Full schedule management lives on the **Automation** page, under the **Schedules** tab (`/automation#schedules`, also linked from the top nav):

- **Cron schedules** — set up recurring processing
- **Interval-based** — run every X minutes
- **Per-library** — schedule specific libraries
- **Scan mode** — each schedule is either a *Full library scan* (default) or a *Recently added only* scan (see [Auto-trigger from Plex](#auto-trigger-from-plex-no-sonarrradarr))

> **Legacy URL note:** `/schedules` and `/webhooks` still work — they 302-redirect to `/automation#schedules` and `/automation#webhooks` respectively, so existing bookmarks and shared links keep working.

### Settings Page

Access settings at `/settings` to manage:

- **Plex Connection** — re-authenticate, test connection
- **Libraries** — select which libraries to process
- **Path Mappings** — media path, Plex videos path, local videos path
- **Processing Options** — per-GPU settings (enable/disable, workers, FFmpeg threads), CPU threads, thumbnail interval and quality

> For per-server settings audits (Plex FSEvent flags, Jellyfin trickplay flags,
> Media Preview Bridge plugin presence, Plex config folder writability, path
> mappings), open **Servers → Edit → Setup Health**. Full per-check reference:
> [Previews Readiness guide](guides/previews-readiness.md).

The Settings page and the Automation page's **Triggers** tab **save automatically as you edit** — there's no Save button. Toggles, sliders, and dropdowns commit immediately; text fields commit on blur (or ~1 s after you stop typing). A small status indicator in the page header shows `Saving…` / `Saved at HH:MM` so you can tell the change landed. If a save fails (e.g. the backend is down), the indicator shows an error and you can click it to retry.

### Automatic GPU → CPU Fallback

Every GPU worker includes automatic CPU fallback — no extra configuration
is needed. If FFmpeg fails on the GPU for any of the common reasons:

- Unsupported codec on the HW decoder
- Hardware-accelerator runtime error (CUDA sync/transfer failure, VAAPI surface exhaustion)
- Driver crash or FFmpeg signal kill (segfault, OOM)

…the same worker retries the file on CPU in-place.  The job log records
the specific reason ("Dolby Vision Profile 5 rejected by Intel VAAPI",
"signal kill (signal 11)", etc.) and the dashboard shows a yellow
"CPU fallback" badge on the affected worker card along with a toast.

The worker is busy on CPU while the retry runs.  If you have a lot of
content that never decodes on the GPU, set **CPU Workers > 0** so that
content routes directly to dedicated CPU workers from the main queue
instead of blocking a GPU worker each time.

#### Speeding up full-library scans on big libraries

A full scan first sweeps every file to check whether a fresh preview
already exists, then only generates the missing ones. Those two jobs have
independent concurrency:

- **GPU Workers / CPU Workers** cap how many previews *generate* at once
  (heavy FFmpeg/GPU work) — keep these matched to your hardware.
- **Library Scanning → Files checked at once** (`scan_workers`) caps how
  many files the *existence check* sweeps in parallel. This is light disk
  I/O and adds no FFmpeg/GPU load.

On a large, mostly-complete library the sweep can dominate. Leave
**Files checked at once** on **Auto** for most setups; raise it if the
"checking" phase feels slow on fast storage, or lower it if a single
spinning HDD thrashes under many parallel stats. Raising it never spawns
more simultaneous FFmpeg processes — that's still governed by your worker
counts.

Settings are saved to `/config/settings.json` and persist across restarts.

### Automation Page

The **Automation** page (`/automation`) hosts two tabs:

- **Triggers** — incoming webhooks from Radarr, Sonarr, Tdarr / custom scripts, and Plex Direct. Also houses the Recently Added Scanner shortcut. This is where you wire the app up to whatever puts media into Plex.
- **Schedules** — full CRUD for recurring scans (cron / interval / specific time). Both Full library and Recently-Added scanners live here.

The Triggers tab includes:

- **Enable/Disable** — master toggle for webhook processing
- **Webhook URLs** — copy-ready URLs for Radarr, Sonarr, and the generic Custom webhook
- **Delay** — seconds to wait after import (gives Plex time to index)
- **Webhook Secret** — optional dedicated authentication token
- **Setup instructions** — step-by-step guides for each source
- **Activity Log** — recent webhook events with status badges

The legacy `/webhooks` and `/schedules` URLs still work — they 302-redirect to the Triggers and Schedules tabs on the new page.

### Production Server

The Docker image runs the web interface for you — there's nothing to configure. The dashboard updates in real time over WebSocket; long-running jobs survive the default proxy timeouts. If you're running the app outside Docker (or just curious how the container is wired internally — gunicorn settings, single-worker rationale, WebSocket transport), see [CONTRIBUTING.md → Architecture](https://github.com/stevezau/media_preview_generator/blob/dev/CONTRIBUTING.md#architecture).

### Reverse Proxy

If you want to expose the web UI outside your local network — for example
with HTTPS, a custom domain, or alongside other services — you can place it
behind a reverse proxy such as Nginx, Apache, or Traefik.

The built-in server listens on port `8080` (HTTP) and the reverse proxy
forwards external requests to it. The web UI uses **WebSocket** (Socket.IO)
for real-time updates, so your reverse proxy **must** forward WebSocket
upgrade requests.

#### Nginx

```nginx
location / {
    proxy_pass http://localhost:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

#### Apache

Enable the required modules first:

```bash
sudo a2enmod proxy proxy_http proxy_wstunnel rewrite headers
sudo systemctl restart apache2
```

Example HTTPS virtual host:

```apache
<VirtualHost *:443>
    ServerName previews.example.com

    SSLEngine On
    SSLCertificateFile    /etc/letsencrypt/live/example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/example.com/privkey.pem
    SSLProtocol +TLSv1.2

    RequestHeader set X-Forwarded-Proto https
    RequestHeader set X-Forwarded-Ssl on

    RewriteEngine On
    RewriteCond %{HTTP:Upgrade} =websocket [NC]
    RewriteRule /(.*) ws://127.0.0.1:8080/$1 [P,L]

    ProxyPass / http://127.0.0.1:8080/
    ProxyPassReverse / http://127.0.0.1:8080/

    ProxyRequests Off
    ProxyPreserveHost On

    Header edit Location ^http://(.*)$ https://$1
</VirtualHost>
```

#### Traefik

Traefik v2+ forwards WebSocket upgrade headers automatically. No extra
configuration is required beyond a standard HTTP router and service.

### Authentication

The web interface uses token-based authentication:

1. **Auto-generated token** — created on first run, saved to `/config/auth.json`
2. **Custom token via wizard** — set your own token during the setup wizard (Step 5)
3. **Fixed token** — set `WEB_AUTH_TOKEN` environment variable (overrides wizard setting)
4. **Token masking** — tokens are always masked in logs (only last 4 chars shown)
5. **Server secrets** — a media server token, API key or password inside an error (a request's URL, say) shows as
   `****` in this app's own log lines (tracebacks included), a job's error and log, and the Files panel
6. **Protection from other sites** — while you're signed in, every change the browser makes (saving settings,
   starting or cancelling a job, editing a server) carries a security token only this app's pages know, so a page on
   another site can't make those changes for you. You never see it; if a button ever says the page's security token is
   out of date, reload the page. Webhooks aren't affected (they use the webhook secret). A script must send the API
   token in a header on every request, as below: signing in once with `POST /api/auth/login` and reusing the cookie
   no longer works for changes. See [Reference — Authentication](reference.md#authentication).
7. **Signing out** — **Logout** signs out this browser only; other browsers stay signed in, even after you change the
   token. To sign every browser out, delete `flask_secret.key` from the config folder (or change `FLASK_SECRET_KEY`,
   if you set it) and restart the container (see [Reference — Authentication](reference.md#authentication)).

API authentication:

```bash
# Bearer token
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8080/api/jobs

# X-Auth-Token header
curl -H "X-Auth-Token: YOUR_TOKEN" http://localhost:8080/api/jobs
```

### Rate Limiting

Login and API endpoints are rate-limited to protect against brute force. See [Reference — Rate Limiting](reference.md#rate-limiting) for the exact limits and the `RATELIMIT_STORAGE_URL` env var for multi-worker deployments.

### Real-Time Updates

The dashboard streams live job progress over WebSocket (Flask-SocketIO, `/jobs` namespace). See [Reference — WebSocket Events](reference.md#websocket-events) for the event table and payloads.

---

## Webhook Integration

Automatically generate preview thumbnails when Radarr or Sonarr imports new media, or when any external tool (Tdarr, scripts, etc.) modifies a file. Webhooks trigger processing of **only the imported file(s)** after a configurable delay, giving Plex time to detect and index the new files.

### How It Works

1. Radarr/Sonarr imports a file (or an external tool sends a custom webhook) and a POST is sent to this app.
2. The app **queues** the file and starts (or resets) a timer. Imports from the same source (Radarr, Sonarr, or Custom) are batched together.
3. A batch is processed only after the **delay** (e.g. 60s) has passed with **no new** imports from that source. So if another file arrives 1 second before the batch would run, it is added to the queue and the timer resets — the batch runs 60 seconds after that file. A batch never waits more than **10 minutes** from its first file, though: a steady stream of imports would otherwise hold it until the stream stopped. Files that arrive once that limit is reached start the next batch.
4. This delay is important because **your media servers need time to add the new file to their library**. If we process too soon, the file may not be indexed yet (regardless of vendor) and the job can fail or skip the item. Not-yet-indexed files are automatically retried on a backoff (1 m → 2 m → 5 m with the default retry count of 3 and initial delay of 30 s; the delay setting scales every wait, so 60 s gives 2 m → 4 m → 10 m, and more retries add 15 m and 60 m steps), so transient indexing lag doesn't drop work. Once the retries run out, the job says the file wasn't indexed after that many retries; the next scheduled scan picks it up. See [Slow-backoff retry queue](multi-server.md#slow-backoff-retry-queue).
5. When the timer fires, the app resolves each queued path against every configured server that owns it, processes it once, and publishes to each in its native format — Plex BIF bundle, Emby sidecar BIF, Jellyfin trickplay tiles. Items that already have a fresh preview are skipped automatically (source-aware dedup).

### Prerequisites

- Media Preview Generator running with the web UI accessible
- Radarr and/or Sonarr installed and managing your media (for Radarr/Sonarr webhooks)
- At least one media server configured in **Servers** (Plex, Emby, or Jellyfin — any combination). The app publishes webhook-triggered work to every server that owns the file.

### Configure Radarr

1. Open the web UI and navigate to **Automation** → **Triggers** tab (in the top nav)
2. Copy the **Radarr Webhook URL**
3. In Radarr, go to **Settings → Connect → + → Webhook**
4. Set **Name**: `Plex Previews`
5. Set **URL**: paste the Radarr Webhook URL
6. Under **Events**, enable:
   - On Import
   - On Upgrade
7. **Authentication** (use one):
   - **Username/Password** (works in all versions): Leave **Username** empty and set **Password** to your API token (see [Authentication Token](getting-started.md#authentication-token)) or webhook secret. The app treats the password as the token.
   - **Custom headers** (if your webhook form has a Headers section): Add **Key** = `X-Auth-Token`, **Value** = your API token or webhook secret.
8. Click **Test** to verify the connection
9. Click **Save**

### Configure Sonarr

1. Copy the **Sonarr Webhook URL** from the web UI Automation → Triggers tab
2. In Sonarr, go to **Settings → Connect → + → Webhook**
3. Set **Name**: `Plex Previews`
4. Set **URL**: paste the Sonarr Webhook URL
5. Under **Events**, enable **On File Import** and **On File Upgrade**. **On Import Complete** can stay on as well: it
   lists the files of an import again once they're all in, so a file its per-file event already sent is skipped (the
   two are matched by Sonarr's download id, so a season pack that takes an hour to import isn't queued twice), and a
   file it's the only one to list is processed.
6. **Authentication** (use one):
   - **Username/Password** (works in all versions): Leave **Username** empty and set **Password** to your API token or webhook secret. The app treats the password as the token.
   - **Custom headers** (if your webhook form has a Headers section): Add **Key** = `X-Auth-Token`, **Value** = your API token or webhook secret.
7. Click **Test** then **Save**

### Custom Webhook (Tdarr, scripts, etc.)

The custom webhook endpoint lets any tool trigger preview generation by POSTing a file path. This is useful when an external tool (like Tdarr) modifies a media file after Sonarr/Radarr has already imported it — Plex detects the change and removes the old thumbnails, but Sonarr/Radarr won't send a new webhook since no import occurred.

**Endpoint:** `POST /api/webhooks/custom`

**Expected payload — single file:**

```json
{
  "file_path": "/media/movies/Movie (2024)/Movie.mkv"
}
```

**Expected payload — multiple files:**

```json
{
  "file_paths": [
    "/media/tv/Show/Season 01/S01E01.mkv",
    "/media/tv/Show/Season 01/S01E02.mkv"
  ],
  "title": "Optional display label"
}
```

**Test connectivity (no processing):**

```json
{
  "eventType": "Test"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | One of `file_path` or `file_paths` required | Single absolute file path to process |
| `file_paths` | array of strings | One of `file_path` or `file_paths` required | Multiple absolute file paths to process |
| `title` | string | No | Display label shown in history/jobs (defaults to first file's basename) |
| `eventType` | string | No | Set to `"Test"` to verify the connection without triggering processing |

Authentication is the same as Radarr/Sonarr: use `X-Auth-Token` header, `Authorization: Bearer`, or Basic auth (password = token).

#### Configure Tdarr

Tdarr doesn't have built-in webhook support like Sonarr/Radarr. Instead, use the **Send Web Request** Flow plugin to POST to the custom endpoint after each transcode.

1. Open the web UI and navigate to **Automation** → **Triggers** tab — copy the **Custom Webhook URL**
2. In Tdarr, open the **Flow** you want to trigger previews from
3. Add a **Send Web Request** plugin after your transcode step
4. Configure the plugin:
   - **Method**: `POST`
   - **Request URL**: paste the Custom Webhook URL (e.g. `http://your-server:8080/api/webhooks/custom`)
   - **Request Headers**: `{"Content-Type": "application/json", "X-Auth-Token": "YOUR_TOKEN"}`
   - **Request Body**: `{"file_path": "{{{args.inputFileObj._id}}}"}`
5. Save the Flow

The `{{{args.inputFileObj._id}}}` template variable is replaced by Tdarr at runtime with the full path of the transcoded file.

> [!TIP]
> If the webhook request fails (e.g. the server is temporarily down), add a **Reset Flow Error** plugin after the Send Web Request step so Tdarr doesn't mark the entire transcode as failed.

#### Configure FileFlows

FileFlows uses two nodes at the end of your flow: a **Set Variable** node to construct the final output path, then a **Web Request** node to POST it to the custom endpoint.

1. In FileFlows, install the **Web** plugin (Plugins page → search "Web") so the **Web Request** node becomes available
2. Open the **Flow** you want to trigger previews from
3. Add a **Set Variable** node just before where the Web Request will go. FileFlows doesn't expose a built-in "final output path" variable, so build it from the folder + final filename + extension:
   - **Variable**: `output_file`
   - **Value**: `{folder.Orig.FullName}/{file.NameNoExtension}{ext}`
4. Add a **Web Request** node after the Set Variable node:
   - **Method**: `POST`
   - **URL**: paste the Custom Webhook URL (e.g. `http://your-server:8080/api/webhooks/custom`)
   - **Content Type**: `JSON`
   - **Headers**: key `X-Auth-Token`, value `YOUR_TOKEN`
   - **Body**: `{"file_path": "{output_file}"}`
5. Save the flow

The `{output_file}` placeholder is substituted by FileFlows at runtime with the absolute path of the file as it exists at the end of the flow (after any rename / re-extension steps).

#### curl Example

```bash
curl -X POST "http://your-server:8080/api/webhooks/custom" \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: YOUR_TOKEN" \
  -d '{"file_path": "/media/movies/Movie (2024)/Movie.mkv"}'
```

### Configuration
All settings are configurable from the **Automation** page → **Triggers** tab in the web UI.

| Setting | Default | Description |
|---------|---------|-------------|
| **Enable Webhooks** | On | Master toggle |
| **Delay before processing** | 60s | How long to wait with no new imports before running a batch (10–300 s). Incoming files are queued; a batch runs only after this many seconds of “quiet” from that source. Each new import resets the timer, up to 10 minutes from the batch's first file. |
| **Webhook Secret** | *(empty)* | Dedicated authentication token for webhooks |

Webhook processing uses your Settings library selection. If a webhook path belongs to an unchecked library, it is skipped.

### Webhook Secret

By default, webhooks authenticate using your main API token. You can optionally configure a **dedicated webhook secret** for better security isolation:

1. On the Automation page (Triggers tab), click **Generate** next to the secret field
2. Click **Save Changes**
3. Use the generated secret as the token: in Radarr/Sonarr, either put it in **Password** (leave Username empty) or in the **X-Auth-Token** header if your form has a Headers section.

### Batching and the delay

When multiple files are imported in quick succession (e.g., a season pack), the app **queues** them per source (Radarr, Sonarr, or Custom). Each new import **resets** the delay timer for that source. A batch runs when the timer finally fires — i.e. when that many seconds have passed with no new imports — or 10 minutes after its first file, whichever comes first.

**Example:** Sonarr imports 10 episodes over 30 seconds with a 60s delay. The timer keeps resetting as each episode arrives. One job runs 60 seconds after the *last* episode and processes all 10 files. A file that arrived at 59 seconds is not processed in an earlier batch — it goes in this batch, and the batch runs 60 seconds after it, so Plex has time to index it.

**Example (long import):** Sonarr imports 300 episodes one every 10 seconds (50 minutes in all). The batch runs 10 minutes after its first episode with the ~60 episodes it has by then, and the next episode opens a new batch. A file that joined a batch just before its 10 minutes were up gets less than the full delay; if the server hasn't indexed it yet, it's retried automatically.

**Viewing files in a batch:** On the **Dashboard**, jobs from webhooks show a label like "Sonarr: 3 files". Click the **+** (chevron) next to the label to expand and see the list of files. On the **Automation** page (Triggers tab), **Activity Log** rows for triggered batches include a chevron; click it to expand and see the files in that batch.

---

## Auto-trigger from Plex (no Sonarr/Radarr)

For media you add to Plex **manually** — copying files into a watched folder, importing through Plex itself, or using any tool other than Sonarr/Radarr/Tdarr — there are two built-in ways to auto-trigger preview generation. Both live on the **Automation** page's **Triggers** tab as dedicated sections (**Plex Direct** and **Recently Added Scanner** in the sidebar), and both feed into the same job pipeline as the existing webhooks.

> [!IMPORTANT]
> **Both options trigger only on _new_ library items.** When Sonarr or Radarr **upgrades** an existing file in place, Plex keeps the same library item, so neither option will see it. Use the existing Sonarr/Radarr webhooks (which fire on `On Upgrade`) for that case.

### Option A — Plex direct webhook (instant)

This uses Plex's built-in webhook feature. The app calls Plex's account API to register its own `/api/webhooks/plex` endpoint, so you don't have to copy/paste anything into Plex Web → Settings → Webhooks (though you still can if you'd rather).

**Requirements:**

- An active **Plex Pass** subscription on the server-owner account. Plex's webhook feature is Plex-Pass-only.
- **Mobile Push Notifications enabled** on your Plex server. This is the catch: Plex's `library.new` event is delivered through the same code path as mobile push notifications, and if push notifications are off, library events are silently dropped. Enable them under Plex Web → Settings → General (toggle *Enable mobile push notifications*). You don't have to actually use mobile push — they just need to be turned on.

**Setup:**

1. Open the web UI → **Automation** → **Triggers** tab and scroll to (or click) the **Plex Direct** sidebar link.
2. The URL field is pre-filled with the URL you're currently accessing the app at (typically correct for same-host setups). If your Plex Media Server is on a different host or behind a different network/proxy, override it with a URL Plex can reach.
3. Click **Test reachability** to verify the URL is routable. The app self-POSTs a synthetic ping; success means Plex should also be able to deliver.
4. Click **Register with Plex**. If you're missing Plex Pass, the UI will tell you and disable the button.
5. (Optional) Confirm by checking Plex Web → Settings → Webhooks — your URL should appear there.

**How it works at runtime:** Plex POSTs a `library.new` event to `/api/webhooks/plex` whenever a new item is added. The app filters out everything else (`media.play`, `media.rate`, etc.), pulls the file paths from `Metadata.Media[].Part[].file` if present, otherwise looks the item up by `ratingKey`, and feeds the paths into the same debounce → batch → process pipeline as Radarr/Sonarr.

**How auth works:** Plex's webhook UI doesn't allow custom headers or HTTP Basic credentials, so there's no way to put an `X-Auth-Token` header on the requests Plex sends. Instead, the **Register with Plex** button appends your webhook secret (or API token) to the URL Plex stores as a `?token=…` query parameter. When Plex POSTs to that URL, the endpoint validates the query token the same way it validates header tokens from Radarr/Sonarr. **If you rotate the webhook secret**, click **Re-register with Plex** (or just save settings — the app auto-re-registers on secret change) so Plex picks up the new value.

### Option B — Recently Added scanner (universal)

A scheduled poll for items where Plex's `addedAt` falls within a configured lookback window. Works without Plex Pass and without push notifications, at the cost of a polling interval of latency.

**The scanner is a first-class schedule type.**  You create, edit, enable, disable, and delete Recently Added scanners through the same Schedules UI as any other scheduled job — and you can create **multiple scanners** with different libraries, intervals, or lookback windows.  For example: scan Movies every 15 minutes with a 1-hour lookback, and your 4K library every 6 hours with a 24-hour lookback.

**Quick start (one click):**

1. Open the web UI → **Automation** → **Triggers** tab → **Recently Added Scanner** (sidebar link).
2. Click **Create default scanner**.  A schedule is created with sensible defaults: runs every **15 minutes**, lookback window **1 hour**, all libraries.
3. That's it.  You can stop here, or continue to customize it.

**Customize or add more scanners:**

1. Click **Manage in Schedules tab** on the scanner card, or switch to the **Schedules** tab directly.
2. Click **Add Schedule** (or **Edit** on an existing scanner).
3. In the modal, choose **Scan mode → Recently added only**.  The Schedule Type field defaults to Interval; pick your frequency.
4. Choose a **Lookback window** — 15 min / 30 min / 1 hour (default) / 2 hours / 6 hours / 24 hours / 3 days / 7 days.
5. Pick a **Library** (or leave as "All Libraries") and click **Create** / **Save**.

**Choosing a lookback window:** items that already have BIF previews are skipped automatically by the job runner, so a larger lookback is cheap — it just re-queries Plex for a wider window. Pick something a few times larger than your scan interval so transient outages (e.g. a 30-minute Plex hiccup) don't cause missed items. The default **1 hour** gives a 4× safety buffer over a 15-min interval, which is plenty for the happy path while staying light on Plex.

**Scheduled scanners are marked with a blue "Recently Added" badge** next to the schedule name in the Schedules table, so you can tell them apart from full-library scans at a glance.

**Why stateless?** The scanner doesn't track a "last seen" timestamp. Every tick it asks Plex for items added within the lookback window and submits them to the job pipeline; the job runner's existing BIF-existence check skips anything that's already done. This avoids cursor migrations, restart races, and clock-skew bugs.

### Which option to pick

| | Plex direct webhook | Recently Added scanner |
|---|---|---|
| **Latency** | Instant (event-driven) | Up to your scan interval |
| **Plex Pass required?** | Yes | No |
| **Other Plex requirements?** | Mobile Push Notifications must be enabled | None |
| **Detects new items?** | Yes | Yes |
| **Detects in-place file upgrades?** | No | No |
| **Setup complexity** | One click after entering URL | Toggle + pick interval |
| **Network requirements** | Plex must be able to reach this app | This app must be able to reach Plex |

You can enable **both** if you want belt-and-suspenders behavior — the recently-added scan acts as a safety net for any `library.new` event Plex's push-notification code path might drop.

---

## Intro & Credits

Skip Intro / Skip Credits markers for Plex, Jellyfin and Emby. Detected once per file — from chapters inside the file,
online databases, matching the theme tune across a season, and reading the on-screen credit roll — then published to
every server that has that file. **Precision over coverage:** a missing marker is fine, a
wrong one isn't, so a marker only ships when the evidence clears the bar below.

### Turning it on

Detection settings are shared by every server: **Settings → Intro & Credits**. What each server actually *receives*
is controlled per server: **Servers → (server) → Edit → Intro & Credits tab → "Send intro & credits markers to this
server"**. A server is off by default even after you turn the shared settings on.

Once the switch is on, the same server's **Libraries** tab gets an **Intro & Credits** column beside **Previews**:
switch on the libraries that get markers on that server. The two columns are independent — a library can get markers
with previews off, or previews without markers. **Sports libraries start off** — no online source covers sports and
local detection isn't reliable there either. You can still switch one on by hand if you want to try it.

### Sources and the publish rule

In **Settings → Intro & Credits**, "Where evidence comes from" lists every source in the order it's checked, with
drag-to-reorder:

| Source | Notes |
|---|---|
| Chapters inside the file | Free, exact when present (~1 in 9 seasons in a sampled library). On a TV episode (a file whose name has a season and an episode number) a chapter named `Ending` counts as credits, since anime names its ending that way; on a movie it doesn't, because there it is usually the last scene. `End` alone never counts. An intro chapter much longer than the rest of the season's (more than twice their median length and more than 30 s longer than it, with at least 2 other episodes carrying one) doesn't decide alone: another source that isn't a server's own marker has to agree. |
| TheIntroDB | Optional **API key** (masked once saved). Works without one (500 lookups/day); your own free key raises that. Off by default — see the note below. |
| IntroDB.app | No key needed, TV only. |
| SkipDB | Free, only counts an answer matched to your file's own length. |
| Matching audio across a season | TV intros. Finds the theme tune a season's episodes share. On its own it was 91 right, 13 wrong and 14 missed on 118 test episodes (Plex's own intro detection: 23 right, 15 wrong), so it publishes an intro by itself when nothing else answers — a show no online database has still gets intros. When another source disagrees, the episode goes to **Needs review**. A server's own marker doesn't count as a second source for it, since a server's intro detection matches audio too, but one that agrees doesn't hold it back either: season audio then decides as it would alone. One that disagrees sends the episode to **Needs review**, and the previous-season hint (below) plus only a server's own marker goes to **Needs review** too. Needs an ffmpeg with chromaprint, which the amd64 Docker image has; elsewhere Settings shows **Not available** and why. CPU, about 2 s per episode, at most 2 at once. See [Season audio and weekly releases](#season-audio-and-weekly-releases). |
| On-screen credit text | Finds where the credit roll starts from text on screen in the last 15 minutes of a movie (7.5 of an episode, or the window you set under Advanced), and stops the skip at the last credit when a scene follows the roll (Emby skips to the end of the file). Tested alone on 80 files: within 10 s on 66 (61 when decoded on the CPU), more than 30 s early on 1. Text that stays in one place through the story (a channel logo, a score bug, a ticker) is ignored, and a file with text on screen through most of its ending (a burnt-in timecode, say) still gets no answer. Credits already running when those last minutes begin are still found, by reading 2 more minutes back. Credits that start in the first 30 seconds of those minutes right after a scene, or more than 1½ minutes before them, get no answer. It publishes credits on its own. On TV recordings with a channel logo or other on-screen graphics it is about as accurate as Plex's own credits detection, not better: on its own it skipped into the story on 4–5 of the 39 episodes we checked and put credits on 4–5 of 12 sports broadcasts, which have none (sports libraries are left out of Intro & Credits unless you tick them). Uses your GPU when a quick self-test shows it is faster than the CPU and finds the same text; otherwise the CPU (about 10–30 s per file; 4K without a GPU up to about 2 min). A file where every frame is a keyframe (ProRes, DNxHD, MJPEG, all-intra H.264) is checked for text one frame every 2 seconds at the end; its whole ending is still read from disk, so a very high-bitrate one on a slow network share can still time out. It is then left alone for a day unless it changes or you Re-detect it. |
| Markers already on your servers | Second opinion only — see below. |

**How it decides** (the note under the list says the same):

- **Chapters** in the file decide on their own, unless two other independent sources agree on something different
  (then the file goes to **Needs review**), or an intro chapter is much longer than the rest of its season's (then one
  other source has to agree).
- **An online database's answer** is published once an independent source agrees with it: on-screen credit text,
  season audio, another database, or a server's own marker. IntroDB and TheIntroDB count as one, because IntroDB's data
  looks partly copied.
- **A single answer** decides alone only when it checks *your* file's own cut: on-screen credit text (credits), season
  audio (intros, so a show no online database has still gets them), or a SkipDB exact/shifted match (intros and
  recaps). A lone IntroDB or TheIntroDB answer never publishes, because neither knows which cut of the file you have;
  SkipDB's credits often start minutes before the real credit roll, so they need a second source too. The previous
  season's audio (a season's first episode) never publishes alone.
- **Anything else** — sources that disagree, or an only answer that can't decide alone — goes to **Needs review** with
  the reason, e.g. "Only IntroDB has the intro; an online answer needs a check against the file".

There is no stricter mode. An earlier "Publish when: High" setting (always two agreeing sources) left most of a library
in Needs review and was removed; upgrading decides the files it held there again once, from what was already found
(no lookups, nothing read from the files), along with any file still waiting for its item's other versions.

Markers already on a Plex/Jellyfin/Emby server only ever *confirm* another source — they never publish on their own,
and they can only **shorten** a skip (a later intro start, an earlier credits end), never lengthen one. That's
deliberate: a crowd-sourced answer that runs to the very end of the file must never swallow a scene after the
credits that the server's own marker correctly stops before. Once credits are decided, a server's own detected
markers can also move the credits start later — when none of them already covers the decided start and one starts
more than 10 seconds later — so an "End Credits" chapter placed on the last shot of the story doesn't skip that shot;
the Inspector then says "Shortened to Plex's own credits start". Intro ends are never moved this way. A Jellyfin or
Emby server's markers imported by its own intro-database plugin (e.g. an AniSkip importer) never do that and
don't count as an independent second opinion — they join the online-database group instead of adding a vote of their
own. AniSkip itself is not a source: it matched IntroDB to within 44 ms on 21 % of intros, against 10 % between the
two intro databases already counted as one, and nothing in the library carries the id it needs. Plex keeps one marker set per item, so when a Plex item has another version whose length differs from this
file's by more than 2 seconds (or the lengths can't be read), that server's markers aren't used for this file at all.
Emby keeps each version's markers apart (see [Emby](#emby-the-media-preview-bridge-for-emby-plugin)), so its
markers are read only from the file's own version and no such check applies. Season audio and a server's own marker never confirm each
other: another source has to agree. Markers this app wrote itself never count as a server's own on Jellyfin, or on
Emby while its Bridge plugin is still installed: it asks its own plugin there which markers are its. Plex records no
author, so if you lose or reset the app's config folder, or remove and add the Plex server again, markers it wrote to
Plex before that read back as Plex's own for those files. On the lab's scale run they never published a marker by
themselves, but 57 credits and 36 intros came out shorter than they should be, 6 credits were published only because
those old markers of ours counted as a second opinion, and 1 credits and 2 intros went to Needs review instead of
being written.

A marker you lock is never replaced by detection; to let detection decide a type again, **Unlock** it. See
[Adjusting, adding and locking markers](#adjusting-adding-and-locking-markers).

**Advanced → Where to look for credits** (collapsed by default) sets how far from the end of a file the on-screen
credit text is searched for, separately for **TV episodes** and **Movies**. Each is **Automatic** (the last 7½
minutes of an episode, the last 15 minutes of a movie or of a file that isn't recognised as an episode) or 5, 10, 15,
20 or 30 minutes. Automatic fits nearly every library: raise a window only if credits are being missed because they
start earlier than it reaches. When the credits are already rolling where the window begins, the search follows them
back two minutes at a time until it finds where they start, but only as far as a start could still be used: the last
quarter of the file, and for a movie 15 minutes from the end (or your movie window, if that is longer). The first two
minutes before the window are always read when the credits fill its start; on Automatic a movie is never read further
back than that. A file that isn't recognised as an episode or a movie gets the movie window but not the 15-minute
limit, so for it the last quarter is the limit. A window that covers your credits is still the surer choice. It never
spends longer on this than the ten minutes a single decode is allowed; a file that runs out of that time (a slow or
stalled network share) is tried again the next day. A longer window takes longer to decode for every file, and a window you choose also lets credits start that early: without it a start earlier than the last quarter of the file, or (for a movie window above 15 minutes) more than 15 minutes from the end, would be found and then refused. It never lets credits start before the middle of the file. Changing it reads files
again on the new window (files you leave on Automatic are not read again). Locked markers are unaffected.

**TheIntroDB** is used without the site's written permission (its terms restrict server-side use); it's off by
default, and pasting your own free key is optional and entirely up to you. The key is masked (`****`) everywhere it's
shown or returned by the API, and never logged.

TheIntroDB's usage line in Settings shows "N of today's lookups used · limit set by TheIntroDB" — once a library
job's daily lookups (or the smaller share left for full-library backfills) run out, it switches to "Daily limit
reached — lookups resume at 00:00 UTC" instead. See the troubleshooting table below for what a job or a file shows
when this happens mid-run. Files checked without TheIntroDB for that reason, and left with a marker undecided, are
checked again automatically just after 00:00 UTC by one low-priority **TheIntroDB recheck** job (up to 500 files; it
skips a file another job decided meanwhile, and runs nothing if you've turned TheIntroDB off by then).

To keep the daily lookups for shows TheIntroDB knows, a show it has no entry for (talk shows, for example) is left
alone for 7 days once 3 of its episodes came back with nothing, while no episode of the show has an answer from it
(one saved by an earlier run counts). The 7 days run from then: a new episode that comes back with nothing meanwhile
doesn't add to them. After them the show's episodes are asked again, older ones due their re-check included, and it
takes 3 more with nothing to leave it alone again. The log says so once per pause, at INFO: "TheIntroDB has no entry
for 3 or more episodes of <show> …; its episodes aren't looked up there until <date>". **Re-detect** in the Inspector
always asks.

### Needs review

When the sources don't clear the bar above for a marker — the only answer can't decide alone, or two credible answers
disagree — that marker isn't sent to any server and the file shows **Needs review**. When the job sent the file's
other markers, the file shows **Markers written** instead, and its reason in the Files panel names the marker still in
review, the times proposed and why (e.g. "intro needs review (only IntroDB has the intro; an online answer needs a
check against the file): 2:07–2:36 from introdb"). A server's row where nothing was sent says why the same way, or
e.g. "Sources disagree: credits_text, skipdb". Nothing is guessed. **Re-detect** in the Inspector asks every source
again, and **Adjust** lets you put the marker where it
really is by hand — including for a type nothing was found for at all, where **Add intro** / **Add credits** puts one
on the timeline at a starting time for you to drag. Saving it locks it, so later checks leave it alone.

Two reasons are specific to TV intros: "Season audio and a server's own marker agree, but both come from matching
audio; needs another source", and "Intro chapter is much longer than the rest of the season's".

### Season audio and weekly releases

Season audio compares an episode with the other episodes of its season on disk: the video files in the same folder
with the same season number in their names (in a folder holding more than 40 of them, the 40 nearest by episode
number). The first episode of a season a job checks fingerprints every member that has no fingerprint yet, on a
worker (once per file; a replaced file is fingerprinted again). The rest of the season then matches from those saved
fingerprints, normally without taking a worker. An episode ffmpeg can't fingerprint (damaged audio, a network read that
stalls) is left out of the other episodes' matching for a day while the file stays unchanged; its own check and a
re-detect still try it.

Saved fingerprints take up to about 28 KB per episode. When an Intro & Credits job completes (except Season, retry and
verify jobs), a cleanup starts in the background, at most once an hour. It looks for up to 2,000 fingerprinted files on
disk, for at most a minute, and clears the fingerprints of files that are gone (renamed by an upgrade, or deleted) while
their folder is still there; the next cleanup goes on where it stopped. A file whose folder is missing, as in an
unmounted library, keeps its fingerprint. The app log (not the job's log) says how many were cleared, and warns (at most
every 10 minutes) when a cleanup is still waiting on a stalled network share.

An episode with no other episode of its season on disk yet (a new season's first weekly release) is compared with the
previous season's first 4 episodes that are already fingerprinted, when its folder is named like a season (`Season 2`
next to `Season 1`). That answer never publishes on its own (alone it was 48 useful / 10 wrong / 24 missed on 82
episodes), and it isn't a second opinion for season audio (it's the same method on the same show). The Inspector shows it as **Previous season audio**.

When a new episode arrives, the season's earlier episodes may now be decided differently: their audio answer can
change, or the new episode's intro chapter changes what counts as normal for the season. Episodes outside the job
that brought the new one are checked again by a **Season: …** job (see
[Webhook follow-ups and retries](#webhook-follow-ups-and-retries)), so the season ends with the same markers whatever
order its episodes arrived in.

### Adjusting, adding and locking markers

Open it from **Tools → Intro & Credits** (the same page as Tools → Preview Inspector, opened on its Intro & Credits tab), search for a title and open a file. The Intro & Credits tab has three buttons for setting a marker yourself. Adjust stays greyed out until a job
has read the file's length.

- **Adjust** opens the editor on the timeline. Drag the handle at each edge of a marker, or type the times. **Save and
  publish to N servers** saves; **Cancel** throws the edit away. A time that looks odd (an intro shorter or longer than
  most, credits that don't run to the end) gets a warning and is still saved. Only two things are refused: a marker
  outside the file, and one that ends before it starts.
- **Add a marker where nothing was found.** On a file where no source found a marker, Adjust still opens. A type with
  no marker shows **Add intro**, **Add credits**, **Add recap** or **Add preview** where its bar would be. It puts a
  marker on the timeline at a starting time that is round on purpose, so it can't be mistaken for something the app
  found: an intro or recap from 0:00 to 0:30, credits the last 60 seconds, a preview the last 30. Drag it to where it
  really is. **Remove** takes back a marker you added before you save it.
- **Saving locks.** There is no adjusted-but-unlocked marker. A saved marker is locked and published straight away to
  every server that has the file and has Intro & Credits on. Detection and later jobs leave it alone.
- **Lock** on its own locks the times the app already decided, unchanged, and publishes them. Once anything on the file
  is locked the same button reads **Unlock**. It asks first, then drops the lock. Your times stay on the servers for
  now; the next check decides those types again and may move them, and **Re-detect** does that straight away. Unlock
  publishes nothing.
- **Your marker wins over "Keep Plex's" and "Keep Emby's".** See [Plex](#plex-writing-straight-into-plexs-database)
  and [Emby](#emby-the-media-preview-bridge-for-emby-plugin) for the row message.
- **Recap and preview** stay editable, with a note per server: only Jellyfin shows them. Where a server can't show a
  type the editor says so ("Only Jellyfin shows recaps. Plex has no recap marker, so this one won't reach it.") and
  leaves that type out of the save when no server with Intro & Credits on could show it. **Emby and credits:** an edited credits
  *end* is accepted, but Emby's Skip Credits always skips to the end of the file, and the editor says so.

A save is one web request, so it is bounded. Your times are saved and locked before any server is contacted, so a
server that fails can't lose your edit. Each call to a server waits at most 8 seconds, and Plex's database waits at most
8 seconds for its locks. A server the request hasn't started on within 25 seconds isn't started: its row says
"Couldn't publish to this server in time; the next Intro & Credits run publishes it". These are per-call limits and a
start gate, not a cap on the whole request, so a server already under way can take a few times 8 seconds. If an
Intro & Credits job is running on the same file, the row says "Intro & Credits is running for this file; the next run
publishes your marker".

### Season view in the Inspector

For a TV episode, the Inspector's Intro & Credits tab has a **This episode** / **Whole season** switch. **Whole
season** lists the season's episodes (the same group season audio uses) with each one's intro and credits, the
sources behind them, and one dot per server. A season of more than 40 episodes lists the 40 nearest, and the header
says so ("60 episodes (showing the 40 nearest)"). It reads only this app's own records, so a whole season loads at
once; **This episode** stays the place for what a server shows right now. An episode in **Needs review** has a
**Review** button (its tooltip gives the reason) that opens that episode. Every row also has an **Edit** button that
opens that episode in the marker editor; once you save there, that row shows your times and its 🔒 lock the next time
you open **Whole season**.

**Publish N to M servers** queues a Normal-priority Intro & Credits job named `Intro & Credits: <show> · Season N`
(or `· Specials`) for exactly the listed episodes: decided episodes go to every server that doesn't show them yet,
and the rest are checked again. Clicking it again while that job is still queued or running reuses it. N (and
**N ready**) counts the episodes with at least one decided marker, since the job sends those even when another type
of the same episode is in Needs review; **N need review** counts the episodes with any type in review, recaps and
previews included. The header names the show and season the way the job does.

### Plex: writing straight into Plex's database

Plex has no API or plugin system for markers, so this app writes them directly into Plex's own SQLite database — the
exact place Plex stores its own. Because that's unsupported by Plex, it's **opt-in with a one-time confirmation** the
first time you turn it on for a Plex server:

- **The app must run on the same machine as Plex, or you run the Plex marker agent next to Plex.** SQLite's write
  mode (WAL) doesn't work over a network filesystem, so a network-mounted database stays read-only. See
  [Plex on another machine](#plex-on-another-machine-the-plex-marker-agent).
- **Map Plex's config folder into both containers from the identical host path.** On unRAID specifically, don't mix
  `/mnt/user` and `/mnt/cache` between the two containers — even though both paths can reach the same files, Plex and
  this app must open the database through the exact same path for the app's same-host proof to succeed.
- **Viewers need Plex Pass** (or Plex Home) to see skip buttons at all — Plex hides markers entirely without it, even
  ones already in its database.
- If Plex's own detection re-analyzes an item, it can replace our markers with its own. The next Intro & Credits job
  that checks the file notices (see [Checking the servers still show them](#checking-the-servers-still-show-them)):
  with **"When Plex has its own markers" → "Use ours"** (the default) it writes ours again; with
  **"Keep Plex's"** it leaves Plex's markers of that type, and the file's row says so, e.g. **"Keeping Plex's
  credits"** or **"1 marker(s); keeping Plex's credits"**. This is decided per type: Plex's intro can be kept while
  our credits are still written. Once kept, no job touches Plex's markers of that type — not a forced **Re-detect**,
  not a run after a failed or skipped attempt, not another version of the item — until you switch the server to
  **Use ours** (the next job writes ours) or Plex no longer has markers of that type (then ours are written
  again). Markers that are simply gone are written again either way. A file Plex already has its own markers of a
  type for isn't read again for that type (no credit-text or season-audio pass), as long as every server the file's
  markers go to keeps its own and shows one: the row says **"Keeping Plex's credits"** and the Inspector **"Kept
  Plex's own marker"**. **Check servers** still watches that marker: once Plex no longer has it, or the server is
  switched to **Use ours**, it runs the file, which then reads it and writes ours.
- Plex detects in a library only when its server setting (Settings → Library → *Generate intro / credits video
  markers*) **and** the library's own setting (Edit library → Advanced → *Enable intro / credits detection*) are on.
  With **Use ours**, **Servers → Edit → Setup Health** lists the Intro & Credits libraries where both are on, each
  with a **Turn off** that switches off only that library's own setting. With **Keep Plex's** it doesn't warn. See
  [Previews Readiness](guides/previews-readiness.md#intro-credits).
- **A marker you adjust or lock in the Inspector is the exception**: it is written over Plex's own markers of that
  type even on a server set to **Keep Plex's**, and the server's row says so — *"Replaced Plex's own marker. This
  server is set to keep Plex's, but a marker you adjust always wins."* The Inspector says it before you save, too.
  Emby works the same way with **Keep Emby's**. Unlock the marker and that type goes back to the setting.
- With **"Keep Plex's"**, markers Plex already shows of a type this app has no record of writing on that item are
  kept too, unless they already match ours: Plex's own markers from before Intro & Credits was turned on, markers
  Plex filled in after this app removed its own (for example while an item's versions disagreed), and markers on an
  item after the app's Intro & Credits data was reset or the Plex server was removed and added again (the app can't
  tell those from Plex's own). What's kept is remembered per Plex item in the app's `markers.db`; after such a reset
  it is worked out again from what Plex shows.
- Tested against Plex 1.43. If a future Plex update changes the database's shape, the app stops writing and shows a
  message rather than guessing.

### Plex on another machine: the Plex marker agent

If this app and Plex run on different machines, run the **Plex marker agent** next to Plex. It is a small container of
its own (`plex-marker-agent/` in the repository, image `ghcr.io/stevezau/plex-marker-agent`) with Plex's config folder
mounted. The app asks it to do the database write on Plex's machine. It is the only supported way to write markers to a
Plex on another machine: without it that Plex stays read-only for markers. If the app and Plex are on the same machine,
don't run it.

1. Run the agent as its [README](https://github.com/stevezau/media_preview_generator/blob/dev/plex-marker-agent/README.md) says: the compose file, the settings (`AGENT_TOKEN`,
   `PLEX_CONFIG`, `PUID`/`PGID`) and the key.
2. In the app: **Servers → your Plex → Edit → Intro & Credits → Plex marker agent**. Switch it on, enter its **Address**
   (for example `http://plex-host.lan:9494`) and the **Shared key**, the same value as `AGENT_TOKEN`. The key is masked
   once saved. The one-time database-write confirmation still applies.
3. The line under the address shows the state: **Connected**, **Can't reach it**, **Key refused** or **Update needed**.
   **Check again** asks now. The agent has its own version, and a version the app doesn't accept is refused on the first
   call, in either direction, with a message saying which side to update.

The agent only ever writes the marker rows of one item into Plex's library database. It refuses a database that isn't
on a local disk of its own machine, a database schema it doesn't know, and a Plex that has no marker list yet, with the
same messages a same-machine setup gives. While it isn't answering, markers wait in this app and the next Intro &
Credits run sends them. Setup Health shows a row for it (see
[Previews Readiness](guides/previews-readiness.md#intro-credits)).

### Jellyfin: the Media Preview Bridge plugin

Jellyfin also has no core API for markers. This app's existing **Media Preview Bridge** plugin (the same one used for
trickplay tiles) gets a markers feature. The Edit tab's status block shows whether it's installed, its version, and
an **Install** / **Update** button that uses the same plugin-install flow as trickplay. Markers show as Skip Intro /
Skip Credits / Skip Recap / Skip Preview depending on what was decided.

### Emby: the Media Preview Bridge for Emby plugin

Emby has no API for markers, so a small Emby plugin, **Media Preview Bridge for Emby**, stores them and writes them
as Emby's own intro/credits chapter markers, next to the file's own chapters (which it never changes). It puts them
back when a "Replace all metadata" or "Search for missing metadata" refresh deletes them, stops showing them once the
file is replaced by a different file until this app sends markers for the new one, and forgets them when the item is
removed from Emby.

The Edit tab shows whether the plugin is installed and its version. When Emby's plugin catalog lists the plugin,
**Install** installs it and restarts Emby. Otherwise the tab offers **Install by hand**: download the DLL for your
Emby version (`MediaPreviewBridge.Emby-4.10.dll` for Emby 4.10, `MediaPreviewBridge.Emby-4.9.dll` for Emby 4.9)
from the plugin's GitHub release (tags `emby-plugin-v…`), rename it to `MediaPreviewBridge.Emby.dll`, copy it into
Emby's `plugins` folder (`/config/plugins` in the official Emby container) and restart Emby. The tab shows the
plugin's version with a ✓ once Emby is back. The API key or user this app uses for Emby needs administrator rights.

- **What Emby shows:** the tab's "Can show" reads **Intro · credits start**. Emby has no credits end: its Skip
  Credits button always skips to the end of the file, including any scene after the credits. The app still sends
  the decided credits start; when those credits end before the file does, the file's row and the Inspector's Emby
  card say **"Emby skips to the end of the file"**. Recaps and previews aren't sent to Emby (it has no button for
  them).
- **"When Emby has its own markers"** works like Plex's setting. Emby can have intro and credits markers of its own,
  from its intro detection (an Emby Premiere feature) or another plugin. **Use ours** (the default) replaces them
  with ours. **Keep Emby's** leaves a type Emby already has and sends ours only for the other types; the row says so,
  e.g. **"1 marker(s); keeping Emby's intro"**, and the Inspector shows **Keeps Emby's**. The plugin still keeps ours
  for a kept type and shows them once Emby's are gone.
- **Versions:** Emby keeps each version of a video (a movie in two cuts, an episode with two releases) as its own
  item with its own chapters, and its player shows the chapters of the version playing. So each version gets the
  markers decided for its own file, with no waiting for the other versions to agree. A file that Emby lists under
  another version's item isn't written: **"This file is Emby item 55, another version of item 53; markers not
  written"**.
- **Emby Premiere:** Emby only lets viewers skip intros when the Emby server has an active Emby Premiere key: without
  one, Emby shows "Skip Intro" a few times and then asks for Premiere instead of skipping. The credits "Up Next" prompt
  works either way. The key is the server's, so it covers every user and app on that server. When the server has no
  key, the tab's amber **Emby Premiere** row says so.

### Multi-version Plex items

When a Plex item has more than one version (a movie in two cuts, an episode with two releases), Plex serves **one**
marker set for the whole item, not one per file. A marker type only shows up once every version has a decided marker
of that type **and** they agree within 2 seconds. A newly added version that hasn't been decided yet temporarily
hides that item's markers on Plex until it catches up — precision first. Jellyfin and Emby versions each get their
own markers.

In the Preview Inspector, the Intro & Credits tab shows the version you clicked. When that version's file isn't on
this app's disk (the other version is, say, or a path mapping is missing), the tab says **"This version's file isn't on
this disk"** instead of showing another version's markers.

### Checking the servers still show them

A server can lose or change our markers without this app doing anything: Plex's own detection replaces them, and a
server that rescans a replaced file can drop them. So before a job reports a file **Up to date** on a server, it
reads back what that server shows — Plex's marker rows for the item in its database (read-only, with the same
same-machine checks as a write), Jellyfin's served segments (one request), Emby's chapter markers (one request) —
and writes ours again when they're gone or different. If that read fails, the file stays **Up to date** for this run,
and the job finishes with a warning such as **"Couldn't check what 3 file(s) show on Home Plex"**.

**Markers written** always means the job changed what the server shows (a forced **Re-detect** that restores lost
markers included); **Up to date** means the server already showed exactly this.

After a webhook job (or its retry) publishes to a file that was replaced (same path, new file), servers often rescan
it shortly after. The job therefore queues one **Verify: …** job for those files, which waits three times the first
retry delay (at least 10 minutes; its queue row reads **"Checking again in 10 min"**) and then checks them again the
same way. It follows **Settings → Retry policy** (none when the retry count is 0) and holds at most 500 files (the job
log says how many more wait for the next run). A verify job, and any retry it queues, never queues another verify;
its retries count on from the retries already used, and a file gone from disk by then isn't retried. Library runs
and files you picked yourself don't queue one: their replaced files may have changed long ago.

For Jellyfin and Emby, a replaced file whose markers are unchanged is always sent again when the plugin still holds
the old file's size: the plugin serves nothing for a file whose size changed (Jellyfin can still list the old
segments meanwhile).

In the Inspector, a server whose markers differ from ours shows **Will replace** or **Will add**, or **Keeps
Plex's** / **Keeps Emby's** when the server's own markers replaced ours and that server is set to keep them; the
reason names the kept types ("Keeping Plex's credits"), also next to **Will add** when another type is still
written. Another Jellyfin provider's segments next to ours don't count as a difference. The note "All versions of
this item share one set of markers" appears only on a Plex item with more than one version (a stacked file or a Plex
optimized copy isn't a version).

### Check servers

The checks above only run for files a job looks at. To check everything this app has published, run **Intro &
Credits · Check servers**: **Start New Job → Intro & Credits → Check servers** on the Dashboard,
`POST /api/markers/reconcile`, or a schedule in **Automation → Schedules** (type **Intro & Credits → Check servers**;
the server and library pickers don't apply). Nothing is scheduled by default. It runs at Low priority unless you pick
another. A marker you locked in the Inspector that a server didn't get (it was down or not ready when you saved)
is published again on every run until the server has it. Only one runs at a time: asking again while one is queued or
running reuses it ("A Check servers job is
already queued"), and a schedule tick then queues nothing. **Re-run** on a finished Check servers job asks the same
way. When the job already there is paused, the message is "A Check servers job is paused. Resume or cancel it on the
dashboard." A job paused by its schedule's stop time is resumed by that schedule's next start (or **Run now**), even
if you've switched the schedule between **Find markers** and **Check servers** since. That start queues nothing else;
the schedule's own mode queues on a later start, once the resumed job has finished. A job you paused yourself stays
paused until you resume it. Deleting the schedule, or switching it to a preview or Recently Added schedule, leaves a
paused job paused, and the log gets a warning naming the job: resume or cancel it on the dashboard. **Re-run** clears
**Pause all**, as it does for any job. With Intro & Credits off on every server there's nothing to check.

Its tooltip sums it up: "Checks that every server with Intro & Credits on still shows the markers this app sent, and
sends them again where they're missing or changed (unless that server is set to keep its own). Covers all servers and
libraries." In detail:

- It reads back every item whose last publish from this app succeeded, on each server with Intro & Credits on (Plex
  one item at a time, with the same same-machine checks as a write; Jellyfin and Emby one request per item), 500 items
  per step so its progress moves. Pausing the job during the read-back gives its job slot back until you resume it.
- Only the files of items whose markers are gone or different, whose Plex item gained or lost a version, or that the
  server replaced with a new item go through the normal rules again (**Keep Plex's** / **Keep Emby's**, versions, the
  server switch). A server set back to **Use ours** gets ours for the types it was keeping. The Plex item's current
  versions go along too: a file that replaced the one this app published there (an upgrade that deleted the old
  file) gets its own markers on that item, and markers decided for the old file come off.
- Items whose last publish failed (Plex busy writing its database, a server that stopped answering mid-write) aren't
  read back; their files run again 1 day after the failure, then 2, 4, 8 and 16 days after each retry that fails too.
  After 5 retries they wait for the next job that runs those files. A publish that works, or a new failure
  after one that did, starts over.
- It also re-reads a server's own markers for files with decided credits where that server had none (or its answer
  couldn't be used) and shows none of ours, since its own detection may have run since and can shorten our credits
  (see [Sources and the publish rule](#sources-and-the-publish-rule)). Every enabled server counts, with Intro &
  Credits on or not. The re-read waits until the answer is 1 day old, then 2, 4, 8 and 16 days after each re-read that
  stays empty or fails; after 5 such re-reads it stops.
- One run takes at most 500 files; the job warns "N more changed file(s) are checked on a later run", and waiting
  items take turns across runs. Whatever is still waiting is listed again next run, with one exception below.
- An item the server no longer has, with no file here that still belongs to it, is dropped from Check servers
  quietly. When a listed file's row is **Waiting** because it isn't in that server's library yet, and that server
  confirms the item is gone, the file gets one retry job, the same one any job queues for a file not in a server's
  library yet (same delay and retry count), since the server may not have added the file's new item yet. Once that
  retry is queued the item is dropped too, until this app publishes to it again. If the job is cancelled or fails
  first, or retries are off, nothing is dropped and the next run checks the file again. If the server doesn't confirm
  the item is gone, there is no retry and the file is listed again next run.
- A server that can't take markers is skipped with its reason ("Skipped Home Plex: …"). Items whose read fails get a
  warning ("Couldn't read what 3 item(s) show on Home Plex") and are read again next run. "Couldn't check Home Plex"
  means reading that server raised an error, or a Jellyfin or Emby server failed 20 reads in a row and the rest of it
  was left for the next run; "Couldn't check Home Plex: no connection to it" means there was no client for it.
- With nothing to fix it finishes at once; its log says "Every server checked still shows what this app published".
- A Check servers job that was running when the app restarted picks up where it was: it checks the files its first run
  listed, skipping those it had finished, rather than listing again. A file's recheck or retry is used up only once
  that file is actually checked, so a cancelled or interrupted run doesn't push any file's next check further out.
  The list is kept on the job only until it ends.

### Turning it off, or revoking the Plex confirmation

Turning the per-server switch off — or revoking the Plex database-write confirmation — takes effect immediately,
including for a job that's already running: its next per-file write to that server is skipped rather than going
ahead on a stale setting.

### Webhook follow-ups and retries

A Sonarr/Radarr/webhook import queues an Intro & Credits job right after the preview job for the same files. It
runs at Normal priority, or at Low when the preview job itself runs at Low, and it starts after that preview job's
first try (it doesn't wait out the preview job's retries; episodes that join it later, see below, don't wait for their
own preview jobs). If a file isn't on disk yet, a server hasn't indexed it into its library yet, or Plex didn't answer
its Plex Pass check, it's retried the way preview jobs are, with the same backoff — **Settings → Retry policy → Retry
count / Initial retry delay**. The job stays one row in the queue: **Pending**, with a **Retry 1/3** chip and
"Retry starting in …", then **Running** while the retry checks the files still waiting. Files that were already
done keep their results. The row turns **Completed** once every file is in, or **Failed** when the retries run out
with files still waiting (the Files panel lists them). **Retry now** starts the waiting retry at once. A job with no
preview job before it (markers on with previews off, a manual job, the Inspector) retries the same way.
A retry resolves the path Sonarr/Radarr sent again, so a file that lands on a
different disk than the first mapped one is still found. A retry batch holds at most 500 files at a time — a bigger backlog (e.g. a brand new library)
is picked up on the next run instead.

Plex, Emby and Jellyfin send one webhook per episode. Episodes of a season that arrive while an earlier episode's
follow-up is still waiting join that job (up to 500 files), so a season pack becomes one Intro & Credits job rather
than one per episode. An episode that joins doesn't wait for its own preview job: the job runs once the first
episode's preview job has finished, and markers don't need previews.

A new episode can change what the season's other episodes should get (for example, an intro chapter that looked
normal turns out far longer than the rest of the season). The job then queues a **Season: …** job that checks those
other episodes again. It runs at Low priority, or Normal right after a webhook import, unless those episodes are
already waiting in a Low Season job, which then checks them. Once it has run, the season has
the same markers whatever order the episodes arrived in, as if they had all been checked together. A Season job holds
at most 500 episodes; any beyond that, and any request lost to a restart before the job finished, are picked up the
next time an episode of that season is checked.

### Reading an Intro & Credits job's log

Each file gets two lines in the job's log: what was sent to each server and what was decided per marker type, then
what every source you turned on answered, in your source order. For example:

```
[10:14:03] INFO - Brave New World S01E05: nothing sent to Plex · intro not found · credits 47:36–48:38 found by credit text → needs review (sources don't agree yet; at Publish when: High one source isn't enough)
  sources: chapters none · TheIntroDB skipped (daily limit reached, resets 00:00 UTC) · credit text credits from 47:36 · Plex's own none
[10:14:09] INFO - The Fall (2013) S03E05: sent intro to Plex; Plex keeps its own credits ("Keep Plex's") · intro 1:02–1:33 (from chapters) · credits: kept Plex's own marker
  sources: chapters intro 1:02–1:33 · TheIntroDB no entry · credit text not read (every server keeps its own credits) · Plex's own credits from 58:23
```

- A decided marker names the sources it came from: "(from chapters)", or "(TheIntroDB + credit text agree)". A marker
  in review names who found it, the proposed times and why it wasn't sent. Times are `m:ss`, or `h:mm:ss` past an hour.
- Per server: "sent intro and credits to Plex", "Plex already has our intro", "nothing sent to Plex", "waiting for Plex
  to add the file to its library" (the job tries it again), and the types that server keeps as its own.
- Per source: its answer ("no entry", "none", "credits from 47:36"), or why it wasn't asked: "not needed (already
  decided)", "skipped (daily limit reached, …)", "skipped (no data for this show; asked again after <date>)", "not
  read (every server keeps its own credits)". **(saved earlier)**
  means the answer was stored before (by an earlier run, or while the job checked another episode of the season) and
  was used without asking again.

A **Season: …** job logs these lines only for episodes whose result changed, and one line per season for the rest:
"Season re-check, Brave New World S01 (3 episodes): no change, E01/E03/E04 still need review (credits from credit
text only)". A **TheIntroDB recheck** job does the same for its episodes ("TheIntroDB recheck, Brave New World S01
(3 episodes): no change"), and logs each movie on its own. Every job ends with a totals line, e.g. "Done: 12 files · 9 sent to Plex · 2 need review · 1 nothing found
· TheIntroDB skipped for 3 files".

Job log times are the container's local time (its `TZ`, or the `/etc/localtime` you mounted), the same clock as the
app's own log.

### Troubleshooting Intro & Credits

Each server's Edit → Intro & Credits tab checks whether that server can actually receive markers right now. This
table covers every state the check can report, using its exact wording:

| What you see | Why | What to do |
|---|---|---|
| Green "Installed ✓" / no warning banner, status block all green | Everything needed to write markers checks out | Nothing — turn the server switch on if you haven't |
| *(Plex)* No Plex Pass row, amber banner: "Can't reach Plex to confirm Plex Pass, so markers wait until Plex answers" | The database checks passed but Plex didn't answer the Plex Pass check (usually restarting or updating) | Wait for Plex to come back; affected files show **Waiting** and are retried |
| No status block; only the off switch | "Intro & Credits is off for this server" | Turn on "Send intro & credits markers to this server" |
| *(Plex)* Status block: "Confirm the Plex database write to turn this on" | Plex has no marker API, so writing needs a one-time confirmation per Plex server; flipping the switch opens that confirmation dialog | Read the dialog and click "Enable for Plex" |
| *(Jellyfin)* Red "Not installed" badge + **Install** button: "Install the Media Preview Bridge plugin" | Jellyfin has no core marker-write API; the plugin renders markers as media segments | Click **Install** |
| *(Emby)* Red "Not installed" badge + **Install** button: "Install the Media Preview Bridge for Emby plugin" | The plugin isn't on this Emby server, and Emby's plugin catalog lists it | Click **Install** (Emby restarts; the tab checks again after 20 s) |
| *(Emby)* Red "Not installed" · **Install by hand** | Emby's plugin catalog doesn't list the plugin yet (or couldn't be read) | Follow [the manual install](#emby-the-media-preview-bridge-for-emby-plugin) |
| *(Emby)* After **Install** or **Update**: "Media Preview Bridge for Emby isn't in the Emby plugin catalog yet; install it by hand (see the Intro & Credits guide)" · **Install by hand** / "Couldn't read Emby's plugin catalog" | The catalog doesn't list the plugin, or Emby didn't answer the catalog request | Follow [the manual install](#emby-the-media-preview-bridge-for-emby-plugin), or try again once Emby answers |
| *(Emby)* Amber "Update needed" badge, "— installed 1.0.0.0" (the version the plugin reports; "installed version unknown" when it doesn't say) + **Update** button; the job's rows say "Update Media Preview Bridge for Emby (installed …) to get markers support" | An older plugin without markers support | Click **Update**, or copy the newer DLL in by hand and restart Emby |
| *(Emby)* "Can't reach this Emby server" / "Can't reach the Media Preview Bridge markers endpoint on this Emby server" | A transient connection problem | Confirm the server is up and reachable; recheck |
| *(Emby)* "Emby rejected this server's credentials; reconnect it" | The stored API key or login no longer works | Reconnect the server from the Servers page |
| *(Emby)* "Emby refused the Media Preview Bridge markers endpoint; this server's API key or user needs administrator rights" | The connected account isn't an administrator | Reconnect with an admin account or API key |
| *(Emby)* Emby shows "Unlock Feature" when a viewer clicks **Skip Intro**, or stops showing Skip Intro; the tab's amber **Emby Premiere** row reads "Viewers can't skip intros: this Emby server has no Emby Premiere key. Skip Credits (Up Next) still works." | Emby only skips intros on a server with an active Emby Premiere key. Without one, its player shows Skip Intro for the first few episodes, asks for Premiere instead of skipping, then hides the button | Add an Emby Premiere key to this Emby server. Nothing to change in this app: the markers are written either way, and the tab notices the key within an hour |
| *(Settings)* **Matching audio across a season** shows "Not available" with a reason, e.g. "Needs an ffmpeg with the chromaprint muxer (jellyfin-ffmpeg in the amd64 image); none was found" | This container's ffmpeg has no chromaprint (the arm64 image, or a custom ffmpeg) | Use the amd64 Docker image; every other source keeps working. Saved season audio answers can't help decide an intro meanwhile, so one decided with them goes to **Needs review** on its next check unless other sources agree; they can still keep an intro in review that they contradict |
| *(Settings)* **Matching audio across a season** shows "Not available": "ffmpeg didn't answer the check for the chromaprint muxer; it is checked again in 10 minutes" | ffmpeg timed out, couldn't start, or exited with an error when asked for its muxers (a busy or slow container start) | Nothing: jobs started meanwhile match no episodes, but saved season audio answers still count, and the check runs again after 10 minutes |
| *(Settings)* **On-screen credit text** says "Not available" with one of: "Needs ONNX Runtime and OpenCV, which the Docker image includes; they aren't installed here" · "Needs the text detection model, which the Docker image includes; it isn't at …" · "The text detection model at … isn't the expected file" · "The text detection check didn't answer; it is checked again in 10 minutes" | Running outside the Docker image without the packages/model installed, a moved or corrupted model file, or the check timed out/crashed | Use the Docker image, which ships the packages and model; every other source keeps working meanwhile. The last reason clears itself — the check runs again after 10 minutes |
| Log line "Credit text detection on \<device\>: CPU (\<reason\>)" although this host has a GPU | The self-test or the device check picked the CPU: the reason is e.g. "the GPU wasn't at least 10% faster than the CPU (median 4.9 vs 5.2 ms per frame; GPU/CPU 0.94 per round)", "the GPU was finding different boxes than the CPU", "Vulkan reports no hardware GPU", "no WebGPU device is this GPU", "… GPUs have no WebGPU path here", "this GPU has no usable WebGPU adapter", or a WebGPU session or helper failure | Nothing to fix if the CPU really is faster or there's no hardware GPU for this device; otherwise check the GPU is passed into the container the way previews use it. arm64 has no GPU path for credit text either way |
| Log line "season_audio had no answer for \<file\> this time: Not fingerprinting \<episode\>: N earlier fingerprint ffmpegs are still stuck reading their files" | Earlier fingerprint reads stalled on the media mount and can't be stopped until it answers; no more are started meanwhile, so episodes that still need fingerprinting get no season audio answer (nothing is held against the files) | Check the media mount (NFS/SMB) is responding. It clears on its own once the stuck reads finish; the next run matches the season |
| A file fails with "Couldn't read the file: Not reading \<path\>: N earlier ffprobes are still stuck reading their files", or a log line "season_audio (or credits_text) had no answer for \<file\> this time: … Not reading \<path\>: N earlier ffprobes are still stuck reading their files" | Earlier ffprobe reads stalled on the media mount; until they finish no new ffprobe is started, so new files can't be checked | Check the media mount is responding; it clears on its own once the stuck reads finish, and the files are tried again on the next run |
| *(Plex)* Red "✕ Not active" next to Plex Pass: "This Plex server has no Plex Pass, so Plex won't show any markers." | Plex hides all markers — even ones already in its database — without Plex Pass | Add Plex Pass to this Plex server |
| *(Plex)* "Plex's database is on a network share (…). The app must run on the same machine as Plex to write markers; Plex stays read-only." | SQLite's write mode doesn't work over NFS/SMB/CIFS | Run this app on the same machine as Plex, or run the [Plex marker agent](#plex-on-another-machine-the-plex-marker-agent) next to Plex. With an agent, the same kind of message means the *agent's* mount is a network share |
| *(Plex)* "Plex's database is on a filesystem this app doesn't recognise as a local disk (…); Plex stays read-only." | The app couldn't prove the folder is a real local disk, so it refuses to risk Plex's database | Check the mount; open an issue if it's genuinely local |
| *(Plex)* "Plex is running, but not with the database file this app sees at …. Mount the exact folder Plex uses, on the same machine (on unRAID, the same /mnt/cache or /mnt/user path Plex uses)." | The app proves it shares Plex's live database lock before ever writing; this Plex has a different copy open | Map Plex's config folder into both containers from the identical host path |
| *(Plex)* "Plex doesn't have its database open through this folder right now (Plex is stopped, or this app sees a different path to the file). Markers are only written while Plex is running." | Same same-host proof, failing because nothing has the database open | Start Plex; recheck the mounted path |
| *(Plex)* "Plex hasn't created its marker tag yet. Run Plex's own intro or credits detection once on any item, then try again." | Markers reuse a database row Plex creates itself the first time it ever writes a marker | Run Plex's own intro or credits analysis once on any item, then recheck |
| *(Jellyfin)* Amber "Update needed" badge, "— installed 1.0.0.0" (the version the plugin reports; "installed version unknown" when it doesn't say) + **Update** button; the job's rows say "Update Media Preview Bridge (installed …) to get markers support" | An older plugin build predates the markers feature | Click **Update** |
| *(Plex)* "Plex […] data has an unknown shape; not writing markers." / "Plex's database has more than one marker tag row, so it's unclear which one Plex serves; not writing markers." | A future Plex version changed its database in a way the app doesn't recognise | Check for an app update; report your Plex version in an issue |
| *(Plex)* "Plex is busy writing its database; trying again on the next run (…)." | Another process (usually Plex itself) holds the database briefly | Nothing — it retries on the next job |
| *(Jellyfin)* "Can't reach this Jellyfin server" / "Can't reach the Media Preview Bridge markers endpoint on this Jellyfin server" | A transient connection problem | Confirm the server is up and reachable; recheck |
| *(Jellyfin)* "Jellyfin rejected this server's credentials; reconnect it" | The stored token/login no longer works | Reconnect the server from the Servers page |
| *(Jellyfin)* "Jellyfin refused the Media Preview Bridge markers endpoint; this server's API key or user needs administrator rights" | The connected account isn't an administrator | Reconnect with an admin account or API key |
| *(Plex)* "This app (user N) can't write …. Run it with the user that owns Plex's database (user M)." | A `PUID`/`PGID` mismatch between the two containers | Set this container's `PUID`/`PGID` to the user that owns Plex's files |
| *(Plex)* "Disk full: Plex's database can't take any writes (…)." | The disk holding Plex's database is full | Free up space on that disk |
| Status block fails to load: "Couldn't check this server's Intro & Credits status" (Edit tab) or "Couldn't read this server's Intro & Credits state (…)" (Inspector row) | An unexpected error while checking (e.g. `markers.db` unreadable) — one server's failure never blocks the rest of the page | Reload; check the logs for the exception type named in the message |

A file's row for one server (the job's Files panel, the Inspector) can also say:

| Message | Why | What to do |
|---|---|---|
| **Waiting**: "Can't reach Plex to confirm Plex Pass" | Plex answered its database checks but not the Plex Pass check, usually while restarting | Nothing; the file is retried (up to your retry count), and the next file checks Plex again |
| **Waiting**: "Not in this server's library yet" | The server hasn't scanned the file in yet | Nothing; the file is retried |
| **Up to date**: "Keeping Plex's intro" (or credits, or both; also added to other rows, e.g. "1 marker(s); keeping Plex's credits") | This Plex server is set to **Keep Plex's**, and Plex shows its own markers of that type (its detection replaced ours, or they were there before this app had a record of the item) | Switch it to **Use ours** if you want ours; the next job that checks the file writes them |
| **Skipped**: "This server was removed" | The server was deleted while the job ran | Nothing |
| **Skipped**: "This server is turned off on the Servers page" | The server was disabled while the job ran | Turn it back on, then run the library or Re-detect the file |
| **Skipped**: "Intro & Credits is off for this server" | The switch was turned off (or the Plex confirmation revoked) while the job ran | Turn it back on; the job's next file already checks again |
| **Skipped**: "This library isn't selected for Intro & Credits on this server" | The library was switched off for Intro & Credits, or removed from the server, while the job ran | Switch it back on in Edit → Libraries → Intro & Credits column |
| **Skipped**: "This file is excluded on this server" | The file matches one of that server's exclude paths | Remove the exclusion if it's wrong |
| **Up to date**: "Keeping Emby's intro" (or credits; also added to other rows, e.g. "1 marker(s); keeping Emby's intro") | This Emby server is set to **Keep Emby's**, and Emby shows its own markers of that type | Switch it to **Use ours** if you want ours |
| **Written**: "… Replaced Plex's own marker. This server is set to keep Plex's, but a marker you adjust always wins." (Emby says the same about Emby's) | You adjusted or locked that marker in the Inspector, so it was written over the server's own although the server is set to keep its own | Nothing; unlock the marker if you want the server's own back |
| Any row ending "…; Emby skips to the end of the file" | The credits end before the file does (a scene follows them), but Emby's Skip Credits always skips to the end of the file | Nothing; Emby has no credits end |
| **Waiting**: "Emby doesn't show which of this item's versions is this file yet; if the file is already in Emby's library, check this server's path mappings" | Emby groups several versions in this item, and none of them maps to this file with its own item id | Nothing while Emby is still scanning; otherwise fix the server's path mappings |
| **Failed**: "This file is Emby item 55, another version of item 53; markers not written" | The job found another version's item for this file | Check how Emby grouped this item's versions |
| **Failed**: "Couldn't read this server's saved settings (…)" | `settings.json` couldn't be read just before the write, so nothing was written | Check the config volume and the log; the next run tries again |
| Evidence detail: "Couldn't read this server's plugins, so its markers aren't used" | A Jellyfin/Emby server's plugin list couldn't be read, so its markers might be a crowd database's copy | Nothing; they're read again on a later run |
| Evidence detail: "Markers on this server look imported from …; not a second opinion for that database" | That server's markers came from a plugin that imports a skip database (IntroDB/TheIntroDB, SkipDB or AniSkip), so they don't confirm that database's own answer | Nothing; this is expected |
| Job warning: "TheIntroDB's daily lookup limit was reached: N files were checked without it. It resets at 00:00 UTC; the files it left undecided are checked again automatically after that (or add a TheIntroDB API key for a higher limit)." | The source's daily budget (or the smaller share full-library backfills may spend) ran out partway through the job | Nothing to fix; a **TheIntroDB recheck** job waits for 00:00 UTC and checks those files again. Add your own TheIntroDB API key for a higher limit. (IntroDB and SkipDB say "run the library again after that" instead: their files aren't rechecked automatically.) |
| File reason ends with "…; TheIntroDB not checked (daily limit reached)" | This file's result could still change once the source is available again — a file every other source already decided doesn't get this note | Nothing; the TheIntroDB recheck job asks for it after 00:00 UTC, and nothing was stored for this source, so any later run asks it too |
| Log: "TheIntroDB has no entry for 3 or more episodes of <show> …; its episodes aren't looked up there until <date>" | TheIntroDB had nothing for 3 episodes of the show and an answer for none, so the show's episodes stop using its daily lookups for 7 days | Nothing; after the date its episodes are asked again. **Re-detect** in the Inspector asks at once |

Per-file job outcomes use plainer labels in the job queue and Files panel: **Markers written** (the job changed
what a server shows), **Up to date** (every server already showed this),
**Needs review**, **Waiting** (a server hasn't indexed the file yet, Plex didn't answer its Plex Pass check, or the
item's versions don't agree yet), **Skipped** (the server can't take markers right now, or a setting changed while
the job ran — see the tables above for why — or the file is a trailer or other extra: "Extras aren't checked for
markers"), **No markers found**, and **No server with Intro & Credits on**. When a file's servers end differently,
the file shows the one that still needs something: **Failed**, then **Waiting** for a server the job tries again
(it hasn't indexed the file yet, or Plex didn't answer its Plex Pass check), then **Markers written**, then **Needs
review**, then **Waiting** for the item's other versions, then **Up to date**. So a file whose intro was written while
its credits need review counts as **Markers written** (its reason names the credits), and a file written to Plex but
still waiting on Jellyfin shows **Waiting**; each server's own result is on the file's row.

Under a job's per-server results, **Decided by** counts how many files each source decided, per marker type — for
example "Credits: chapters 40 · credit text 9 · TheIntroDB + server markers 3". It fills in while the job runs. A file
counts once per type, under the sources its marker came from; a marker a chapter set counts as **chapters** even when
other sources agreed, and **server markers** (markers already on your servers) only appear as the second opinion a
single source needed; your own edited markers count as **your edits**. Markers that need review, and files that failed
or weren't checked, aren't counted. See
[Reference — Decided-by counts](reference.md#decided-by-counts).

> [!NOTE]
> Rolling back to a version before Intro & Credits existed needs an extra step — see
> [Rolling back to a previous version](#rolling-back-to-a-previous-version).

---

## HDR & Dolby Vision

**The short version:** HDR thumbnails get **tone-mapped** to SDR (standard dynamic range) automatically, so you don't see washed-out or pitch-black previews. Most HDR formats just work; the trickiest case is **Dolby Vision Profile 5** (4K Dolby Vision rips with no HDR10 fallback layer), and the only setup step you may need is on NVIDIA — see the warning at the bottom of this section.

> [!TIP]
> **Quick glossary** (so the rest of this section is readable):
>
> - **Tone mapping** — converting HDR's wide brightness range down to the narrower SDR range a thumbnail can show. Without it, HDR content looks too dark or has a colour tint.
> - **HDR10 / HDR10+ / HLG** — the common HDR formats; all use a single video track and tone-map cleanly.
> - **Dolby Vision (DV)** — Dolby's HDR. **Profile 7/8** carries an HDR10 fallback layer (works the same as HDR10). **Profile 5** doesn't, and needs special handling.
> - **libplacebo / Vulkan** — the GPU library that handles DV Profile 5 tone mapping. Needs a working Vulkan driver.

| Format | What we do |
|--------|------------|
| HDR10 | Tone-map to SDR (algorithm configurable, default: Hable) |
| HLG | Tone-map to SDR (algorithm configurable, default: Hable) |
| HDR10+ (without Dolby Vision) | Tone-map to SDR (algorithm configurable, default: Hable) |
| Dolby Vision Profile 7/8 (with HDR10 fallback) | Tone-map the HDR10 fallback layer with hardware decode ([#178](https://github.com/stevezau/media_preview_generator/issues/178)) |
| Dolby Vision Profile 5 (no fallback layer) | Per-GPU specialist path (see below); software fallback uses libplacebo ([#172](https://github.com/stevezau/media_preview_generator/issues/172), [#178](https://github.com/stevezau/media_preview_generator/issues/178), [#212](https://github.com/stevezau/media_preview_generator/issues/212)) |

### Tone-map algorithm

**What:** the formula used to compress HDR's brightness range into SDR. **Default works for almost everyone.** Change only if your HDR thumbnails look too dark or oddly tinted.

Non-DV HDR content (HDR10, HLG, HDR10+) uses a configurable algorithm, set in **Settings → Thumbnail Settings → HDR Tone Mapping** or via the `TONEMAP_ALGORITHM` env var. Options: `hable` (default), `reinhard`, `mobius`, `clip`, `gamma`, `linear`. If HDR thumbnails look too dark, try `reinhard`.

### Dolby Vision Profile 5

**What:** the trickiest HDR format — has no HDR10 fallback layer, so the standard tone-mapping path can't read it. **What to do:** nothing — the tool picks the right path for whatever GPU you have. (NVIDIA users: see the warning below.)

The tool picks the fastest working path per GPU vendor:

| Vendor | Typical speed on 4K | Notes |
|---|---|---|
| Intel iGPU / Arc | **~17×** (UHD 770) | Uses Jellyfin's DV-aware patch — currently the fastest path |
| NVIDIA | ~10–16× (Turing); faster on Ada/Hopper | Needs Vulkan driver — see the NVIDIA warning below |
| AMD Radeon | (untested locally; same flags as NVIDIA) | |
| CPU-only fallback | ~5–10× (CPU-bound) | When no GPU is available |

The image ships **jellyfin-ffmpeg 8.1.2** as its preferred FFmpeg because Jellyfin's fork carries a Dolby-Vision-aware tone-mapping patch upstream FFmpeg still lacks. Non-amd64 builds fall back to the base image's FFmpeg 8.1.2 automatically.

Profile 7/8 (with HDR10 fallback) uses the standard tone-mapping chain — no Vulkan or special handling needed.

### Container edge cases handled automatically

You don't have to do anything for these — the container handles them on startup. Listed here so you know what the logs are telling you when they mention DRI symlinks or Vulkan probe retries.

- **Intel GPU under `--runtime=nvidia`.** When you're running both an Intel iGPU and an NVIDIA card under the NVIDIA container runtime, NVIDIA's tooling hides the Intel device from one specific OpenCL discovery path. The container quietly re-adds the missing symlinks on startup so the Intel iGPU stays usable for tone mapping.
- **NVIDIA Vulkan on dual-GPU hosts.** When both an Intel iGPU and an NVIDIA dGPU are present, Vulkan defaults to Intel — but Dolby Vision tone mapping is faster on the NVIDIA card. The container's Vulkan startup probe tries up to four configurations to force NVIDIA selection so frames don't bounce between the two cards.

> [!IMPORTANT]
> **NVIDIA users: set `NVIDIA_DRIVER_CAPABILITIES=all` (or include `graphics`).**
>
> Dolby Vision Profile 5 needs the NVIDIA Vulkan driver inside the container. NVIDIA's container toolkit only loads it when the `graphics` capability is declared. The common `compute,video,utility` setting is fine for everything else but **not** for Dolby Vision Profile 5 — without `graphics`, the app can't tone-map those files and their thumbnails come out visibly dim.
>
> **Fix:** add `-e NVIDIA_DRIVER_CAPABILITIES=all` to your `docker run` command (or set it in the `environment:` block of your compose file) and restart the container. `all` is what the official NVIDIA Vulkan images use. If you prefer minimum privilege, `compute,video,utility,graphics` works too.
>
> If the in-app warning banner persists after the restart, you may be hitting a less-common cause (a driver-version regression, a missing library in the container runtime config, or an ICD file in the wrong place). The banner will name the specific cause; `GET /api/system/vulkan/debug` returns a plain-text diagnostic bundle you can attach to a GitHub issue.

---

## FAQ

Common questions have moved to their own page — see [FAQ](faq.md).

---

## Troubleshooting

Use this table to diagnose common failures quickly.

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `Skipping as file not found` | Path mapping mismatch between a media server and this container | Verify the server's per-entry mappings in [Path Mappings](reference.md#path-mappings) (each Plex/Emby/Jellyfin entry has its own list). |
| `GPU permission denied` | Container user cannot access GPU device files | Set `PUID`/`PGID` to a user with GPU access; on Unraid use `PUID=99`, `PGID=100`. |
| `Plex config folder does not exist` / unwritable | Incorrect mount or wrong `plex_config_folder` | Confirm the mounted `/plex` path contains `Cache`, `Media`, and `Metadata`. Previews Readiness surfaces this per-Plex-server. |
| `Connection failed` on a server card | Bad URL, unreachable host, or invalid token | Use server IP (not `localhost` in Docker), verify the server is running, and test the URL + token with curl. |
| Webhook job sits in **Pending** for a long time | The concurrent-job gate is full — active jobs are running at capacity | Check **Settings → Processing Options → Incoming job priority** is **High** (the default) so webhook jobs take the reserved slot instead of queueing. Otherwise wait for a slot to free up (priority-ordered), raise the cap, or check the global **Pause Processing** toggle isn't on. Pausing ≠ cancelling — paused jobs stay in Pending. |
| Webhook returns `401` | Invalid or missing authentication | In Sonarr/Radarr webhook settings, leave **Username** empty and set **Password** to your API token or webhook secret. |
| Webhook test passes but imports do not trigger jobs | Wrong webhook events or webhooks disabled | Enable **On Import** in Radarr/Sonarr and verify `webhook_enabled=true`. |
| Log warns `Webhook from sonarr: ignored '…' — the payload didn't carry a file path` | The sender's payload has no file path anywhere: no `episodeFile`, no `episodeFiles[]`, no `filePath` (Sonarr's own "On Import Complete" event lists its files, so it never causes this) | Check the sender posts Sonarr's standard webhook body; a custom template or a proxy that rewrites the body must keep `episodeFile.path` (or `series.path` + `episodeFile.relativePath`). |
| New files are imported but previews are not generated | Plex indexing delay or wrong library mapping | Increase webhook delay and verify Radarr/Sonarr library mapping in Webhooks settings. |
| Job warning: "N file(s) still weren't indexed by the media server after N retries, so no more retries are queued. The next scheduled scan will pick them up." | The media server hadn't added the file to its library by the last retry | Nothing, if you have a scheduled scan: it picks the file up once the server has it. Otherwise raise **Retry count** or **Initial retry delay** (Settings → Retry policy), or check the file is under a library folder the server scans. |
| Radarr/Sonarr cannot reach webhook URL | Network routing or hostname issue | Use host IP or reachable Docker hostname (not `localhost`), then verify firewall and port `8080`. |
| New job starts after I paused | Global pause not set or UI not refreshed | Use **Pause Processing** (Current Job or Job Queue header). Pause is global and persisted; in-flight files finish before workers idle. |
| DV Profile 5 thumbnails are visibly dim, and the log warns that no working Vulkan device was found | The container can't reach a hardware Vulkan device, so the app skips Profile 5 tone mapping and extracts plain frames instead | Pass an iGPU to the container with `--device /dev/dri:/dev/dri` (Intel/AMD), or for NVIDIA set `NVIDIA_DRIVER_CAPABILITIES=all` so the NVIDIA Vulkan driver gets injected. Most users already pass `/dev/dri` for hardware video acceleration, which brings the Vulkan driver along for free. |

### Validate Plex Config Path

```bash
ls -la "/path/to/Library/Application Support/Plex Media Server"
```

Expected directories include `Cache`, `Media`, and `Metadata`.

### Debug Logging

Enable detailed logs when diagnosing persistent issues. In **Settings** → **Processing Options**, set **Log Level** to `DEBUG`. Alternatively, set `LOG_LEVEL=DEBUG` as an environment variable (one-time seed on first start).

---

## Rolling back to a previous version

Schema downgrades are **not automated**. If you need to revert from a release that ran a settings migration, do it manually:

1. **Stop the container.**
   ```bash
   docker stop media-preview-generator
   ```
2. **Restore the relevant `.bak` files** from your config volume. Each JSON file the app owns leaves a single rolling `.bak` next to it on every save:
   ```bash
   cd /your/config/dir
   mv settings.json.bak settings.json          # required
   mv schedules.json.bak schedules.json        # if you use Schedules
   mv webhook_history.json.bak webhook_history.json  # optional
   mv setup_state.json.bak setup_state.json    # optional
   ```
   `jobs.db` does **not** have a JSON `.bak` (jobs moved to SQLite as of this release). To recover an older job database, restore your full config-volume snapshot.
3. **Start the older app version** that wrote those files.
   ```bash
   docker run ... your/image:older-tag
   ```

> [!WARNING]
> **Rolling back from a version with Intro & Credits (settings schema v15 or newer).** The older binary doesn't know
> about the `intro_credits` job kind — it treats every job row it finds the same way, so a leftover Intro & Credits
> job comes back as a **full preview scan** of that job's libraries instead of quietly disappearing. Before you stop
> the container, **cancel any pending or running Intro & Credits jobs** from the Dashboard, or run this against your
> config volume's `jobs.db` (check `web/jobs.py`'s `jobs` table — columns `kind` and `status` — before running
> anything against a database you haven't looked at):
> ```bash
> sqlite3 /config/jobs.db "UPDATE jobs SET status='cancelled' WHERE kind='intro_credits' AND status IN ('pending','running')"
> ```
> This only stops the surprise scan; it doesn't restore markers already written to Plex or Jellyfin, and the older
> binary can't write or read them either way.
>
> **Intro & Credits schedules are worse: the older binary runs each one as a full preview scan on every tick.** While
> the new version is still running, open **Automation → Schedules** and **delete** (or **disable**) every schedule
> with the **Intro & Credits** badge. Both remove it from the scheduler's own database (`scheduler.db`) as well as
> from `schedules.json`. Editing `schedules.json` by hand isn't enough, because the older binary also starts the jobs
> already stored in `scheduler.db`, and there's no reliable command for those. Then keep the current
> `schedules.json`: in step 2, don't restore a `schedules.json` backup from before that change, or the schedules
> come back.

> **Multi-server caveat.** Multi-server installs cannot meaningfully downgrade to a single-server release without losing the second / third server's settings. The newer schema holds richer data than the older one can represent. The downgrade-refusal guard (introduced in this release) intentionally refuses to start the older binary against a newer `settings.json` — its log message names the `.bak` path so you have a one-line recovery hint.

> **Why it refuses to "just work".** Silent acceptance would drop unknown fields on the next save — exactly the failure mode that wiped a user's job history during a tag-drift incident on the multi-server branch. Refusing to boot is loud and recoverable; silent truncation is quiet and final.

---

## Support

Open a [GitHub Issue](https://github.com/stevezau/media_preview_generator/issues).

---

## Next Steps

- Validate installation and mounts in [Getting Started](getting-started.md)
- Confirm environment variables and API behavior in [Configuration & API Reference](reference.md)

---

[Back to Docs](README.md) | [Main README](https://github.com/stevezau/media_preview_generator/blob/dev/README.md)
