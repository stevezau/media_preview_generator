# Copy density audit

Rule: a visible helper line under a setting label states PURPOSE ONLY, one line, <= ~90 characters.
Mechanics, exceptions, side effects, fallbacks, examples and where-else-to-configure move into the label's
info icon (short `title` tooltip + `<template>` detail dialog). Text is moved, never deleted. Errors,
validation text, empty states and pre-action warnings stay visible.

## Static scan (templates + JS-rendered markup, helper text > 90 chars)

| Location (before edit) | Chars | Action | Text |
|---|---|---|---|
| static/js/app.js:2263 | 112 | Keep: dynamic status/progress markup (scan false positive) | Counts include results recorded across all ${totalRuns} ${totalRuns === 1 ? 'run' : 'runs'... |
| static/js/app.js:2574 | 104 | Keep: dynamic status/progress markup (scan false positive) | ${job.status === 'pending' ? 'Queued' : job.status === 'running' ? 'Starting…' : 'No progr... |
| static/js/app.js:2595 | 95 | Keep: dynamic status/progress markup (scan false positive) | ${total ? `${total.toLocaleString()} ${total === 1 ? 'item' : 'items'} · ` : ''}Awaiting w... |
| static/js/app.js:2870 | 92 | Keep: dynamic status/progress markup (scan false positive) | ${retryAttempt > 0 ? `Attempt ${retryAttempt}${maxRetries ? ` of ${maxRetries}` : ''}` : '... |
| static/js/app.js:2880 | 138 | Keep: dynamic status/progress markup (scan false positive) | ${escapeHtml(job.progress.current_item) // 'Starting...'} Items: ${job.progress.processed_... |
| static/js/gpu_config_panel.js:217 | 139 | Keep: error text (must stay visible) | ${esc(gpu.error // 'Acceleration test failed')}${gpu.error_detail ? ' ' + esc(gpu.error_de... |
| static/js/gpu_config_panel.js:225 | 120 | Move to ⓘ (visible line -> purpose only) | 0 = automatic. Worker counts and jobs are configured in groups. If a GPU worker can't proc... |
| static/js/servers.js:778 | 165 | Move to ⓘ (visible line -> purpose only) | After you log in: Jellyfin needs Trickplay image extraction enabled per library — the Serv... |
| static/js/servers.js:1937 | 102 | Keep: state notice the user must see | This server is disabled — checks are paused. Re-enable it on the Servers page to run readi... |
| static/js/servers.js:3136 | 97 | Keep: dynamic value diff (scan false positive) | ` + `Currently ${formatHealthValue(current)} ` + `— recommended ${formatHealthValue(recomm... |
| static/js/worker_groups.js:481 | 160 | Keep: live status text | Removed · ${state.finishing} finishing${snapshot.processing_paused ? ' after resume' : ''}... |
| static/js/worker_groups.js:514 | 238 | Move to ⓘ (visible line -> purpose only) | Current files finish when a group closes or is reduced. GPU jobs may still use CPU stages ... |
| static/js/worker_groups.js:602 | 91 | Move to ⓘ (visible line -> purpose only) | Workers are simultaneous tasks, not CPU cores. Use Enabled to pause a group (zero workers)... |
| static/js/worker_groups.js:605 | 139 | Move to ⓘ (visible line -> purpose only) | ${escape(timezoneLabel())}. Days select when the window starts: Mon 23:00–07:00 ends Tuesd... |
| static/js/worker_groups.js:627 | 139 | Move to ⓘ (visible line -> purpose only) | ${escape(timezoneLabel())}. Days select when the window starts: Mon 23:00–07:00 ends Tuesd... |
| templates/_automation_schedules.html:21 | 160 | Move to ⓘ (visible line -> purpose only) | You can have as many schedules as you like — e.g. "Nightly full scan of Movies at 2 AM" an... |
| templates/_automation_schedules.html:101 | 103 | Move to ⓘ (visible line -> purpose only) | Fast poll — only processes items added within the lookback window (works for Plex, Emby, a... |
| templates/_automation_schedules.html:136 | 131 | Move to ⓘ (visible line -> purpose only) | How far back to look for newly added items. Items that already have previews are skipped a... |
| templates/_automation_schedules.html:143 | 134 | Move to ⓘ (visible line -> purpose only) | Pin this schedule to one configured server. Required if multiple servers share a library n... |
| templates/_automation_schedules.html:236 | 137 | Move to ⓘ (visible line -> purpose only) | Format: minute hour day-of-month month day-of-week Examples: 0 */2 * * * (every 2 hrs), 0,... |
| templates/_automation_schedules.html:245 | 193 | Move to ⓘ (visible line -> purpose only) | Pauses any running job from this schedule at this time of day. The next start tick resumes... |
| templates/_automation_schedules.html:261 | 170 | Move to ⓘ (visible line -> purpose only) | Priority assigned to jobs created by this schedule. Default lets Recently Added sweeps fol... |
| templates/_automation_schedules.html:272 | 137 | Move to ⓘ (visible line -> purpose only) | Newest and oldest use the date added to the library. Date ordering applies within each lib... |
| templates/_automation_schedules.html:273 | 170 | Move to ⓘ (visible line -> purpose only) | Pick Random if your library spans multiple physical disks (e.g. unraid shfs, mergerfs) — p... |
| templates/_automation_triggers.html:106 | 246 | Move to ⓘ (visible line -> purpose only) | Pick a server to scope these webhook URLs to it (adds ?server_id=<id>). Use this when the ... |
| templates/_automation_triggers.html:243 | 120 | Move to ⓘ (visible line -> purpose only) | The {{{args.inputFileObj._id}}} template is replaced by Tdarr at runtime with the full fil... |
| templates/_automation_triggers.html:271 | 180 | Move to ⓘ (visible line -> purpose only) | The {output_file} placeholder is substituted by FileFlows at runtime with the absolute pat... |
| templates/_job_details_modal.html:155 | 151 | Keep: read-only record note | Requested paths may be files or folders. They are separate from recorded outcomes; this li... |
| templates/_manual_job_modal.html:55 | 149 | Move to ⓘ (visible line -> purpose only) | One absolute container path per line. Directories expand to the media inside. Select at le... |
| templates/_scan_filters.html:40 | 117 | Move to ⓘ (visible line -> purpose only) | Uses the date recorded by your media server, not the release date. Date ranges include bot... |
| templates/_scan_filters.html:52 | 108 | Move to ⓘ (visible line -> purpose only) | Highest numbered seasons in the library; excludes specials. Applies only to TV shows, incl... |
| templates/_scan_filters.html:71 | 102 | Move to ⓘ (visible line -> purpose only) | Inclusive release years, for movies only. Enter at least one year; leave the other blank f... |
| templates/_scan_filters.html:75 | 123 | Move to ⓘ (visible line -> purpose only) | Media missing a date, season or year needed by an active filter is excluded. Existing prev... |
| templates/_server_connection_form.html:83 | 168 | Move to ⓘ (visible line -> purpose only) | Click Start Quick Connect to get a code, then enter it in your Jellyfin profile menu. Quic... |
| templates/_server_connection_form.html:102 | 115 | Move to ⓘ (visible line -> purpose only) | Recommended. Lists every Plex server your account can access so you can pick one (or add s... |
| templates/_server_connection_form.html:113 | 166 | Move to ⓘ (visible line -> purpose only) | Picking a server fills the fields below — set its config folder, then Test connection. To ... |
| templates/_server_connection_form.html:144 | 114 | Move to ⓘ (visible line -> purpose only) | Preview files land in {this folder}/Media/localhost/.... Common Docker bind: -v /path/to/p... |
| templates/_start_job_modal.html:69 | 108 | Move to ⓘ (visible line -> purpose only) | Checks every eligible file in the chosen libraries. Preview date, episode and ordering fil... |
| templates/_start_job_modal.html:114 | 137 | Move to ⓘ (visible line -> purpose only) | Newest and oldest use the date added to the library. Date ordering applies within each lib... |
| templates/_start_job_modal.html:115 | 109 | Move to ⓘ (visible line -> purpose only) | Pick Random if your media is split across multiple disks, so workers don't all hit one dis... |
| templates/_worker_quiet_hours.html:47 | 291 | Move to ⓘ (visible line -> purpose only) | Times use the app timezone. Days select when a window starts: Monday 23:00–07:00 ends Tues... |
| templates/logout.html:12 | 106 | Keep: page notice, not a setting helper | Sign-in is handled by your proxy or VPN: sign out there to leave. This only clears this ap... |
| templates/servers.html:180 | 98 | Move to ⓘ (visible line -> purpose only) | Where this Plex server's preview bundles will be written. Mirrors the Setup wizard → Step ... |
| templates/servers.html:192 | 142 | Move to ⓘ (visible line -> purpose only) | Off (default) = previews live next to the media (<media>.trickplay/). On = previews live i... |
| templates/servers.html:221 | 113 | Move to ⓘ (visible line -> purpose only) | Shared by chapter thumbnails and Intro & Credits when Plex runs on another machine. Intro ... |
| templates/servers.html:250 | 166 | Move to ⓘ (visible line -> purpose only) | Existing credentials are kept until you confirm a new one. Each vendor has its own auth op... |
| templates/servers.html:260 | 115 | Move to ⓘ (visible line -> purpose only) | Find it via Plex's docs. To switch Plex accounts entirely, delete this server and re-add v... |
| templates/servers.html:495 | 117 | Move to ⓘ (visible line -> purpose only) | Libraries to monitor — Previews covers every preview job for this server (webhooks, schedu... |
| templates/servers.html:532 | 204 | Move to ⓘ (visible line -> purpose only) | Use this when your media server and this app see the same files at different paths. Most i... |
| templates/servers.html:538 | 882 | Move to ⓘ (visible line -> purpose only) | Suppose your server stores a file at /data_16tb/movies/Inception.mkv but this app's contai... |
| templates/servers.html:585 | 112 | Move to ⓘ (visible line -> purpose only) | Skip preview generation for files matching these rules. Useful for things like trailer fol... |
| templates/servers.html:615 | 230 | Move to ⓘ (visible line -> purpose only) | Webhook jobs use Delay before processing in Automation → Triggers. To override it, append ... |
| templates/servers.html:658 | 197 | Move to ⓘ (visible line -> purpose only) | Use this app's web-auth token as the value. You can find or rotate it under Settings → Aut... |
| templates/servers.html:687 | 165 | Move to ⓘ (visible line -> purpose only) | Plex quirk: the library.new webhook only fires when push notifications are on. The toggle ... |
| templates/servers.html:775 | 110 | Move to ⓘ (visible line -> purpose only) | Selected libraries receive scrubber previews. Choose libraries or edit shared quality and ... |
| templates/servers.html:779 | 107 | Move to ⓘ (visible line -> purpose only) | Included in preview jobs for the selected preview libraries. Separate from scrubber images... |
| templates/servers.html:784 | 126 | Move to ⓘ (visible line -> purpose only) | Adds pictures to Plex's chapter menu and updates Plex's database. Plex may still show olde... |
| templates/servers.html:789 | 120 | Move to ⓘ (visible line -> purpose only) | Requires Plex 1.43.4.x and supported chapter data. For Plex on another machine, configure ... |
| templates/servers.html:861 | 377 | Move to ⓘ (visible line -> purpose only) | Requires Plex 1.43.4.x on the same machine, with Plex's config folder mounted locally. Net... |
| templates/servers.html:868 | 197 | Move to ⓘ (visible line -> purpose only) | Choose movie and TV libraries on the Libraries tab once this is on. Music libraries are no... |
| templates/servers.html:902 | 123 | Move to ⓘ (visible line -> purpose only) | Map Plex's config folder into both containers from the identical host path (on unRAID, don... |
| templates/settings.html:680 | 148 | Move to ⓘ (visible line -> purpose only) | Every file goes through the same four steps. Each marker type (intro, credits) is decided ... |
| templates/settings.html:720 | 139 | Move to ⓘ (visible line -> purpose only) | IntroDB and TheIntroDB count as one source. A plugin's imported copy of IntroDB or SkipDB ... |
| templates/settings.html:910 | 116 | Move to ⓘ (visible line -> purpose only) | Scripts and webhooks using the old token stop working until updated. Use this when you sus... |
| templates/setup.html:110 | 141 | Move to ⓘ (visible line -> purpose only) | Use this if your network blocks plex.tv, or if you’d rather paste a URL and token directly... |
| templates/setup.html:200 | 150 | Move to ⓘ (visible line -> purpose only) | Auto-detected from Plex. Override if the URL isn't reachable from this host (e.g. inside D... |
| templates/setup.html:279 | 208 | Move to ⓘ (visible line -> purpose only) | Enter the path inside this container. On your server Plex might use /config/plex/Library/A... |

## Playwright measurement at 1440px (before edit)

Script: wrapped (> 1 line) or > 90 char blocks matching `.sr-hint, .form-text, small.text-muted, .text-muted`
on /settings (all sections), /automation (schedule modal forced open), /servers (edit/add modals forced open),
and the job modals. Results (queue-created timestamp cells are layout noise and excluded):
 /settings 1 (token warning, 116 chars), /automation 10, /servers 7, start-job modal 4 (+ shared filter rows).
The Playwright hits are a subset of the static scan above; the remainder are 91-120 character lines that
fit on one line at 1440 but exceed the rule or wrap in narrower containers.

## Result (after edits)

Fixed: about 50 helper lines across automation (webhook secret, delay, server scope, schedule modal, trigger notes),
the start-job / manual-job / scan-filter modals, servers edit tabs and connection form, setup wizard, Settings
(token warning, marker steps and sources, pause schedule), plus the JS-rendered GPU and worker-group hints. Each now
shows a purpose-only line with an info icon whose `<template>` detail holds the original text unchanged.

Left on purpose:
- Intro & Credits source cost notes (`.markers-source-note`, 2 rows): `tests/e2e/test_intro_credits_settings.py` asserts that copy visibly.
- Settings `.sec-desc` section subtitles (92 and 96 chars, one line) and the empty-state "No schedules configured".
- Servers "Mapping Plex's config folder" bullet inside the Plex confirm dialog (a pre-action warning).
- Servers "Worked example" expander (already collapsed, progressive disclosure) and the Tdarr/FileFlows placeholder notes inside collapsed setup steps.
- servers.js post-login Jellyfin instruction and dynamic status/progress strings (not setting helpers).
