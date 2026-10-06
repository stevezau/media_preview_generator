# Server modal (Add + Edit) - inventory, critique, parity

Banner: DEGRADED: single-context critique (design-spec run, no sub-agent split, `impeccable detect` not run on the live app). Findings come from the source plus lab screenshots.

Sources read: `templates/servers.html` (Edit modal L44-925), `_add_server_modal.html`, `_server_connection_form.html`, `static/js/servers.js`, `markers_server_tab.js`, `loudness_server_tab.js`, `plex_webhook_panel.js`, `plex-auth.js`. Not part of this modal: `_scan_filters.html` (Automation schedules + Start-job modal) and `_worker_quiet_hours.html` (Settings). `folder_picker.js` is the shared Browse dialog used by every folder field below.

Visual source: lab app at :18080 (Plex, 2 Jellyfin, 2 Emby). IMPORTANT drift: the lab build is older than the worktree. Lab shows tabs "General / Setup Health / Intro & Credits / Libraries / ..." (no Processing tab). The worktree source has "Connection / Setup Health / Processing / ..." with Chapter thumbnails, Intro & Credits and Loudness merged into Processing, and the Plex helper in Connection. The inventory follows the WORKTREE source; shots follow the lab where they agree. Raw lab shots: `app-review/shots/server-modal-lab/`.

Legend. Types: P = Plex, E = Emby, J = Jellyfin. Disposition: kept = same control, same id, same behaviour; restyled = same control and id, new look; moved = same id, new place; new = added by the redesign. "(i)" = a tooltip button exists today. All ids must survive (e2e tests and JS read them).

## 1. Edit modal - frame

| # | Control | id / selector | Type | P E J | Writes | States / validation | (i) | Disposition |
|---|---|---|---|---|---|---|---|---|
| 1 | Vendor logo | `#editServerVendorLogo` | img | P E J | - | hidden until vendor known | no | restyled (36px tile in header) |
| 2 | Title "Edit <name>" | `#editServerNameWrap`, `#editServerName` | text | P E J | - | truncates | no | restyled (name + mono host line) |
| 3 | Close | `.btn-close` | button | P E J | - | Esc, backdrop | no | kept |
| 4 | Server id / type | `#editServerId`, `#editServerType` | hidden | P E J | - | read by every handler | no | kept |
| 5 | Section select (mobile) | `#editServerSectionSelect` | select | P E J | - | options mirror the tab list; <= 767px only | no | moved (replaced by scrolling tab strip; the same `activateSection(id)` call must back both) |
| 6 | Section tabs (7) | `.nav-tabs .nav-link[data-bs-target]` | tabs | P E J | - | `#edit-tab-general/health/processing/libraries/paths/excludes/automation`; Automation tab un-hidden per vendor | no | restyled (left rail on desktop, scroll strip on mobile; ids kept) |
| 7 | Health tab marker | `#editHealthTabMarker` | span | P E J | - | warn/bad glyph after probe | no | restyled (count badge, see 3.5) |
| 8 | Cancel | footer btn | button | P E J | - | dismiss | no | kept |
| 9 | Save changes | `#editServerSave` | button | P E J | PUT `/api/servers/<id>` (name, url, verify_ssl, enabled, path_mappings, exclude_paths, libraries, output, markers, loudness, auth) | spinner "Saving..."; Plex markers confirm modal first | no | kept (primary; disabled until dirty = new behaviour) |
| 10 | Save error | `#editServerResult` | alert | P E J | - | `alert-danger` with API error | no | moved (to footer left, same id) |

## 2. Edit modal - Connection (`#edit-tab-general`)

| # | Control | id | Type | P E J | Writes | Validation / states | (i) | Depends on | Disposition |
|---|---|---|---|---|---|---|---|---|---|
| 11 | Display name | `#editServerDisplayName` | text | P E J | `name` | empty falls back to old name | yes | - | kept |
| 12 | Server URL | `#editServerUrl` | text | P E J | `url` | none inline; Test Connection checks | yes + "Docker & networking" explain panel | - | kept |
| 13 | Verify SSL certificate | `#editServerVerifySsl` | checkbox | P E J | `verify_ssl` | default on | yes | https URL (no-op on http) | kept |
| 14 | Server enabled | `#editServerEnabled` | checkbox | P E J | `enabled` | form-text "Uncheck to pause publishing" | yes | - | moved (header switch, always visible; text becomes the (i)) |
| 15 | Test Connection | `#editTestConnectionBtn` | button | P E J | POST `/api/servers/<id>/test-connection` | spinner "Testing..."; disabled while running | no | URL | restyled |
| 16 | Test result | `#editTestConnectionResult` | text | P E J | - | ok: "Connected - <version>"; fail: message (warning); error (danger) | no | 15 | restyled (pill) |
| 17 | Jellyfin plugin panel feed | `updateJellyfinPluginPanel(data.plugin)` | JS | J | - | plugin info from test result | n/a | 15 | kept (surfaces in Setup Health plugin row) |
| 18 | Plex config folder | `#editPlexConfigFolder` | text | P | `output.plex_config_folder` (+ `adapter: plex_bundle`) | debounced `/api/settings/validate-plex-config-folder`; valid "Looks like a valid Plex structure (n/n hash shards)", invalid red text | yes + template | - | kept |
| 19 | Browse (Plex folder) | `#editPlexConfigBrowseBtn` | button | P | fills 18 | opens folder picker | title only | - | kept |
| 20 | Store trickplay off the media drive | `#editJellyfinSaveOffMedia` | checkbox | J | `output.save_with_media = !checked` | reveals 21 | yes x2 + templates | - | kept |
| 21 | Jellyfin config folder | `#editJellyfinConfigFolder` | text | J | `output.jellyfin_config_folder` | placeholder `/jellyfin-config`; debounced `/api/settings/validate-jellyfin-config-folder`; must be `:rw` | yes | 20 on | kept |
| 22 | Browse (Jellyfin folder) | `#editJellyfinConfigBrowseBtn` | button | J | fills 21 | folder picker | title only | 20 on | kept |
| 23 | Off-media warning | `#editJellyfinOffMediaWarning` | alert | J | - | "writes to Jellyfin's config dir; SaveTrickplayWithMedia must be off; see Setup Health" | no | 20 on | restyled (one line + (i)) |
| 24 | Plex helper switch | `#markersAgentEnabled` | switch | P | `markers.plex.agent.enabled` | shared by chapter thumbnails + Intro & Credits; also gates loudness hint | yes x2 | - | kept |
| 25 | Helper address | `#markersAgentUrl` | url | P | `markers.plex.agent.url` | placeholder `http://plex-host.lan:9494` | no | 24 on | kept |
| 26 | Helper shared key | `#markersAgentToken` | password | P | `markers.plex.agent.token` | returns masked; unchanged mask = keep stored key; never logged | yes | 24 on | kept |
| 27 | Helper state line | `#markersAgentState` | text | P | - | live status of helper | no | 24 on | kept |
| 28 | Re-authenticate (details) | `<details>` + `#editReauthSection` | disclosure | P E J | `auth` (only when a new credential was confirmed) | closed by default; "Existing credentials are kept until you confirm a new one" | yes (explain template) | - | restyled (card "Credentials", closed) |
| 29 | Plex token | `#editReauthPlexToken` | password | P | `auth.token` | placeholder "paste a new X-Plex-Token to rotate" | yes + template | - | kept |
| 30 | JF method radios | `#editReauthJfQc/Pw/Key` (`editReauthJfMethod`) | radio group | J | picks 31-37 | default Quick Connect | no | - | kept |
| 31 | JF Start Quick Connect | `#editReauthJfQcStart` | button | J | POST `/api/servers/auth/jellyfin/quick-connect/{initiate,poll,exchange}` | status alert 32 | no | 30=QC | kept |
| 32 | JF QC status | `#editReauthJfQcStatus` | alert | J | - | shows code / progress | no | 31 | kept |
| 33 | JF username | `#editReauthJfUsername` | text | J | with 35 | autocomplete username | no | 30=PW | kept |
| 34 | JF password | `#editReauthJfPassword` | password | J | with 35 | autocomplete current-password | no | 30=PW | kept |
| 35 | JF Verify password | `#editReauthJfPwSubmit` | button | J | POST `/api/servers/auth/jellyfin/password` | status 36 | no | 33+34 | kept |
| 36 | JF password status | `#editReauthJfPwStatus` | alert | J | - | | no | 35 | kept |
| 37 | JF API key | `#editReauthJfApiKey` | password | J | `auth` (api_key) | | no | 30=Key | kept |
| 38 | Emby method radios | `#editReauthEmbyPw/Key` (`editReauthEmbyMethod`) | radio group | E | picks 39-43 | default Password; no Quick Connect on Emby | no | - | kept |
| 39 | Emby username | `#editReauthEmbyUsername` | text | E | with 41 | | no | 38=PW | kept |
| 40 | Emby password | `#editReauthEmbyPassword` | password | E | with 41 | | no | 38=PW | kept |
| 41 | Emby Verify password | `#editReauthEmbyPwSubmit` | button | E | POST `/api/servers/auth/emby/password` | status 42 | no | 39+40 | kept |
| 42 | Emby password status | `#editReauthEmbyPwStatus` | alert | E | - | | no | 41 | kept |
| 43 | Emby API key | `#editReauthEmbyApiKey` | password | E | `auth` (api_key) | | no | 38=Key | kept |
| 44 | Pending auth payload | `#editReauthPending` | hidden | P E J | `auth` | written by 29/35/41/37/43/31, read on save | no | - | kept |
| 45 | Delete server | `.delete-server-btn` equivalent | button | P E J | DELETE `/api/servers/<id>` | confirm dialog (existing card handler) | no | - | new (optional "Danger zone" at the bottom of Connection; reuses the card's handler) |

## 3. Edit modal - Setup Health (`#edit-tab-health`)

The body is rendered by JS from `/api/servers/<id>/previews-readiness`; rows differ by vendor and by what the probe finds.

| # | Control | id | Type | P E J | Writes | States / validation | (i) | Disposition |
|---|---|---|---|---|---|---|---|---|
| 46 | Card + status chip | `#editReadinessGroup`, `#editReadinessBadge` | card, badge | P E J | - | checking... / recommendations (warn) / action needed (bad) / all good / server disabled (no probe) | yes (overview + template) | restyled (summary header with counts) |
| 47 | Re-check | `#editReadinessRecheckBtn` | icon button | P E J | re-probe | tooltip explains | yes (title) | restyled (text button "Re-check") |
| 48 | Bucket headers | "Must fix n" / "Recommended n" | section | P E J | - | collapsible; bad / warn tint | no | restyled (+ "All good n" collapsed) |
| 49 | Category headings | LIBRARY SETTINGS / VENDOR-SIDE PREVIEW GENERATION / SERVER OPTIONS / STATUS | label | P E J | - | per probe | no | kept |
| 50 | Issue row | title, severity tag ("Recommended" / "Required - fix to enable"), (i), description | row | P E J | - | description currently always visible | yes per row | restyled (description -> (i) / expandable; one-line purpose stays) |
| 51 | Current -> Recommended pills | `CURRENTLY off -> RECOMMENDED on` | diff | P E J | - | read-only | no | restyled (inline diff) |
| 52 | Apply recommended | per-row btn | button | P E J | vendor settings via readiness-fix endpoints | spinner; row turns green | no | kept |
| 53 | Enable (override) | per-row btn | button | P (e.g. BIF generation) | vendor override | secondary style | no | kept |
| 54 | Dismiss | per-row link | button | P E J | dismissal record | hides row | no | kept |
| 55 | Plugin opt-in | `#editReadinessPluginOptIn` | checkbox | J | plugin install in the fix plan | checked by default; label "Install Media Preview Bridge plugin (recommended - instant previews, no races)" | yes | kept |
| 56 | Plugin opt-out warning | `#editReadinessPluginOptOutWarning` | alert | J | - | shows when 55 unticked | no | kept |
| 57 | Fix critical | `#editReadinessFixCriticalBtn` | button | P E J | opens fix-plan modal | danger style; only when must-fix exists | no | kept (sticky bar) |
| 58 | Fix all | `#editReadinessFixAllBtn` | button | P E J | opens fix-plan modal | warning style | no | kept (sticky bar) |
| 59 | Fix result | `#editReadinessFixResult` | text | P E J | - | | no | kept |
| 60 | Fix-plan modal | `#readinessFixPlanList`, `#readinessFixPlanCancelBtn`, `#readinessFixPlanApplyBtn` | modal | P E J | applies listed changes | lists exact changes first | no | kept (separate modal) |
| 61 | Type-to-confirm modal | `#readinessConfirmInput`, `#readinessConfirmSubmit` | modal | P E J | destructive vendor change | submit enabled when text matches | no | kept |
| 62 | Plex rows (probe) | library scan settings (auto scan, partial scan, periodic scan), per-library "Plex's BIF generation" (Apply / Enable override), Plex loudness (`set_plex_loudness_never`) | rows | P | Plex server prefs | | yes per row | kept |
| 63 | Jellyfin rows (probe) | per-library "Trickplay enabled in Jellyfin", "Look for trickplay next to the media file", Trickplay options sync | rows | J | Jellyfin library options | "Required - fix to enable" | yes per row | kept |
| 64 | Emby rows (probe) | none in the lab (healthy); vendor rows appear if the probe finds issues | rows | E | | | | kept |

## 4. Edit modal - Processing (`#edit-tab-processing`, worktree only)

| # | Control | id | Type | P E J | Writes | States / validation | (i) | Depends on | Disposition |
|---|---|---|---|---|---|---|---|---|---|
| 65 | Feature jump nav | `.processing-feature-nav a[data-feature-jump]` | links | P (all 4) E J (Previews, Intro & Credits) | - | Chapter + Loudness links Plex-only | no | - | restyled (section jump chips) |
| 66 | Previews output line | `#processingPreviewOutput` | text | P E J | - | where previews are written | no | - | kept |
| 67 | Links to Libraries / Settings | `.processing-section-link` | links | P E J | - | | no | - | kept |
| 68 | Chapter thumbnails | `#editPlexChapterThumbnails` | switch | P | `output.chapter_thumbnails` | off by default; "Plex 1.43.4.x" requirement | yes + template | - | kept |
| 69 | Chapter Setup Health link | `#editPlexChapterHealthLink` | link | P | - | jumps to Health | no | - | kept |
| 70 | Send intro & credits markers | `#markersEnabled` | switch | P E J | `markers.enabled` | Plex: confirm modal before first enable (`markers.plex.db_write_confirmed_at`) | yes | - | kept |
| 71 | Plex markers confirm | `#markersPlexConfirmOk`, `#markersPlexConfirmCancel` | modal | P | `markers.plex.db_write_confirmed_at` | text differs with helper on/off | no | 70 | kept |
| 72 | Markers status block | `#markersStatusBlock` | read-only rows | P E J | - | P: How markers get here, Plex Pass chip, Database location + local-disk chip, Plex's own detection chip; E: plugin version chip, Can show, Emby Premiere warning; J: plugin version chip, Can show | yes on some rows | 70 | restyled (definition list) |
| 73 | Libraries pointer | `#markersLibrariesPointer`, `.markers-libraries-link` | link | P E J | - | | no | 70 | kept |
| 74 | When Plex has its own markers | `#markersRedetectRestore` / `#markersRedetectKeep` (`markersPlexRedetect`) | segmented radio | P | `markers.plex.on_plex_redetect` (restore / keep_plex) | "Use ours" / "Keep Plex's" | yes + template | 70 | kept |
| 75 | When Emby has its own markers | `#markersEmbyRedetectRestore` / `#markersEmbyRedetectKeep` | segmented radio | E | `markers.emby.on_emby_redetect` (restore / keep_emby) | | yes + template | 70 | kept |
| 76 | Markers library ids | `.markers-lib-toggle` (Libraries tab column) | switches | P E J | `markers.library_ids` | | yes (column header) | 70 on | kept (column lives on the Libraries tab) |
| 77 | Analyse loudness | `#loudnessEnabled` | switch | P | `loudness.enabled` | needs Plex 1.43.4.x on same machine, CPU worker group | yes x2 + templates | - | kept |
| 78 | Loudness helper hint | `#loudnessHelperHint` | text | P | - | shown when Plex helper is on | no | 24 on | kept |
| 79 | Loudness libraries pointer | `#loudnessLibrariesPointer`, `.loudness-libraries-link` | link | P | - | music not supported | yes | 77 | kept |
| 80 | Loudness library ids | `.loudness-lib-toggle` (Libraries column) | switches | P | `loudness.library_ids` | | yes | 77 on | kept |

## 5. Edit modal - Libraries (`#edit-tab-libraries`)

| # | Control | id | Type | P E J | Writes | States | (i) | Disposition |
|---|---|---|---|---|---|---|---|---|
| 81 | Heading "Libraries to monitor" | text | text | P E J | - | | yes + template | restyled |
| 82 | Refresh libraries | `#editRefreshLibrariesBtn` | button | P E J | POST `/api/servers/<id>/refresh-libraries`; updates cache | spinner; error toast | no | kept |
| 83 | Library table | `#editLibraryTable`, `#editLibraryList` | table | P E J | - | empty: "No cached libraries - click Refresh libraries" | no | restyled |
| 84 | Row name + kind badge | `tr[data-lib-id/name/kind]` | cell | P E J | - | movie / episode / music / unknown | no | kept |
| 85 | Previews switch | `.edit-lib-toggle` | switch | P E J | `libraries[].enabled` | aria-label "Previews for <lib>" | column header none | kept |
| 86 | Intro & Credits column | `.markers-lib-col` / `.markers-lib-toggle` | switch | P E J | `markers.library_ids` | column hidden while 70 off; sports default off | yes (header) | kept |
| 87 | Loudness column | `.loudness-lib-col` / `.loudness-lib-toggle` | switch | P | `loudness.library_ids` | hidden while 77 off; movie and TV default on | yes (header) | kept |

## 6. Edit modal - Path mappings (`#edit-tab-paths`)

| # | Control | id / class | Type | P E J | Writes | Validation | (i) | Disposition |
|---|---|---|---|---|---|---|---|---|
| 88 | Intro text + worked example | `<details>` | text | P E J | - | collapsed example | yes (header (i)) | restyled |
| 89 | Path on Media Server | `.pm-remote` | text | P E J | `path_mappings[].remote_prefix` and `plex_prefix` (both written) | placeholder `/data_16tb/movies` | yes | kept |
| 90 | Local path on this app | `.pm-local` | text | P E J | `path_mappings[].local_prefix` | debounced `/api/settings/validate-local-path`; red/green feedback "Path exists" | yes | kept |
| 91 | Browse local path | `.pm-browse` | button | P E J | fills 90 | folder picker | title | kept |
| 92 | Path on Apps (Sonarr, Radarr, Webhooks) | `.pm-webhook` | text | P E J | `path_mappings[].webhook_prefixes` (";" separated) | optional | yes + explain | kept |
| 93 | Remove row | `.pm-remove` | icon button | P E J | removes row | | title | kept |
| 94 | Add row | `#editAddPathMapping` | button | P E J | adds row | | no | kept |
| 95 | Apply to all servers | `#editApplyPathMappingsAll` | button | P E J | PUT copy to every other server | confirm toast | title | kept |
| 96 | Rows with all three fields empty | - | rule | P E J | dropped on save | | no | kept |

## 7. Edit modal - Exclude paths (`#edit-tab-excludes`)

| # | Control | id / class | Type | P E J | Writes | Validation | (i) | Disposition |
|---|---|---|---|---|---|---|---|---|
| 97 | Intro | text | text | P E J | - | | yes | restyled |
| 98 | Value | `.ep-value` | text | P E J | `exclude_paths[].value` | placeholder `/data/Trailers/`; empty rows dropped | no | kept |
| 99 | Type | `.ep-type` | select | P E J | `exclude_paths[].type` (`path` = prefix, `regex`) | | yes (header) | kept |
| 100 | Remove row | `.ep-remove` | icon button | P E J | | | title | kept |
| 101 | Add row | `#editAddExcludePath` | button | P E J | | | no | kept |
| 102 | Apply to all servers | `#editApplyExcludePathsAll` | button | P E J | | | title | kept |

## 8. Edit modal - Webhook & Scanner (`#edit-tab-automation`)

| # | Control | id | Type | P E J | Writes | States | (i) | Disposition |
|---|---|---|---|---|---|---|---|---|
| 103 | Delay note | text | text | P E J | - | "Webhook jobs use the delay in Automation -> Triggers" | yes + template (`?delay=30`) | restyled |
| 104 | Plex caption | `#editPlexWebhookServerCaption` | text | P | - | "registered with <name> using its own Plex token" | no | kept |
| 105 | Vendor webhook card header | `#editVendorWebhookCard`, `#editVendorWebhookHeader`, `#editVendorWebhookPluginBadge` | card | E J | - | badge "Plugin required"; hidden for Plex | no | restyled |
| 106 | Webhook URL | `#editVendorWebhookUrl` | readonly text | E J | - | pinned to this server id | yes | kept |
| 107 | Copy URL | `#editVendorWebhookCopyBtn` | button | E J | clipboard | | title | kept |
| 108 | Auth header block | Name `X-Auth-Token`, Value `[THIS APP'S TOKEN]` | alert + code | E J | - | link to Settings -> Authentication; header keeps token out of logs | yes | restyled (one line + (i)) |
| 109 | Plugin hint | `#editVendorWebhookPluginHint` | alert | E J | - | link to plugin docs (Jellyfin Webhook plugin / Emby built-in 4.6+) | no | kept |
| 110 | Setup steps | `#editVendorWebhookSteps` | ol | E J | - | 7 steps J, 5 steps E | no | restyled (collapsed "Setup steps") |
| 111 | Plex Direct Webhook card | `#editPlexWebhookCard` | card | P | - | badges: Instant, Plex Pass required | no | restyled |
| 112 | Plex status badge | `#plexWebhookStatusBadge` | badge | P | - | Checking / Not registered / Registered / No Plex Pass / Error / Unknown | no | kept |
| 113 | Prerequisites list | Plex Pass; Mobile Push Notifications on | list | P | - | | yes (push quirk) | restyled (collapsed) |
| 114 | Alerts | `#plexWebhookNoPlexPass`, `#plexWebhookError`, `#plexWebhookWarning` | alerts | P | - | localhost URL warning; no Plex Pass; API error | no | kept |
| 115 | Webhook URL override | `#plexWebhookPublicUrl` | text | P | via `/api/settings/plex_webhook/register` | placeholder `http://your-host:8080/api/webhooks/server/<id>`; must be reachable from Plex | no | kept |
| 116 | Reset to detected URL | `#plexWebhookResetUrlBtn` | button | P | fills 115 | | title | kept |
| 117 | Register with Plex | `#plexWebhookRegisterBtn` | button | P | POST `/api/settings/plex_webhook/register` | spinner "Registering..."; label becomes "Re-register with Plex" | no | kept |
| 118 | Remove from Plex | `#plexWebhookUnregisterBtn` | button | P | POST `/api/settings/plex_webhook/unregister` | hidden until registered; danger outline | no | kept |
| 119 | Recently Added Scanner card | `#editRecentlyAddedCard`, `#editRecentlyAddedSafetyNetBadge` ("Safety net") | card | P E J | - | | no | restyled |
| 120 | Scanner status badge | `#recentlyAddedStatusBadge` | badge | P E J | - | Loading / No scanners / n scanners | no | kept |
| 121 | Scanner blurb + summary | `#editRecentlyAddedBlurb`, `#recentlyAddedScannerSummary` | text | P E J | - | GET `/api/schedules` | no | kept |
| 122 | Configure on Schedules page | `#recentlyAddedManageLink` | link-button | P E J | navigates | | no | kept |
| 123 | Scan all now | `#recentlyAddedScanNowBtn` | button | P E J | POST `/api/schedules/<id>/run` per scanner | hidden when none | no | kept |
| 124 | Scan result | `#recentlyAddedScanResult` | text | P E J | - | | no | kept |

## 9. Add Server flow (`#addServerModal`, shared with the Setup wizard)

| # | Control | id | Type | P E J | Writes | Validation / states | (i) | Disposition |
|---|---|---|---|---|---|---|---|---|
| 125 | Title | `#serverModalTitle` | text | P E J | - | "Add Server" | no | restyled (step title + stepper) |
| 126 | Type picker | `.server-type-btn[data-type=plex/emby/jellyfin]` | 3 buttons | P E J | selects vendor | | no | restyled (cards with one-line description) |
| 127 | Step label | `#step-connect-vendor` | text | P E J | - | "Connect to <vendor>" | no | kept |
| 128 | Server URL | `#serverUrl` | text | E J (P optional) | `url` | placeholder `http://hostname:port`; default-ports line | yes + explain | kept (default-ports line shows only the chosen vendor's port) |
| 129 | Display name | `#serverName` | text | P E J | `name` | placeholder "e.g. Home Server, Living Room" | yes | kept |
| 130 | Authentication radios | `#auth-quick`, `#auth-pw`, `#auth-key` (`authMethod`) | radio group | J: 3, E: 2 (no Quick Connect) | picks credential | | yes + explain | kept |
| 131 | Start Quick Connect | `#quickConnectStart` | button | J | POST `/api/servers/auth/jellyfin/quick-connect/*` | code shown in `#quickConnectCode` | yes | kept |
| 132 | Quick Connect code | `#quickConnectCode` | alert | J | - | | no | kept |
| 133 | Username | `#authUsername` | text | E J | `auth` via `/api/servers/auth/{emby,jellyfin}/password` | | yes | kept |
| 134 | Password | `#authPassword` | password | E J | same | "Sent once to the server; not stored here" | yes | kept |
| 135 | API key | `#authApiKey` | password | E J | `auth.api_key` | | yes | kept |
| 136 | Jellyfin trickplay notice | alert in step | alert | J | - | "Trickplay image extraction must be enabled per library; fix in Setup Health after saving" | no | restyled (one line) |
| 137 | Sign in with Plex | `#plexOAuthStart` | button | P | POST `/api/plex/auth/pin` + poll; lists servers | recommended path | yes | kept (primary; first on the Plex step) |
| 138 | Discovered Plex servers | `#plexDiscoveredList` | list | P | fills URL, name, token | pick one; can sign in again for another | yes + template | kept |
| 139 | Manual Plex token (details) | `#plexToken` | password | P | `auth.token` | closed by default; link to Plex docs | no | kept |
| 140 | Plex config folder | `#plexConfigFolder` | text | P | `output.plex_config_folder` | placeholder `/config/plex`; validate-plex-config-folder: "Looks like a valid Plex config folder" | yes + template | kept |
| 141 | Browse | `#plexConfigFolderBrowseBtn` | button | P | fills 140 | | title | kept |
| 142 | Form error | `#connectFormError` | alert | P E J | - | | no | kept |
| 143 | Back | `#step-connect-back` | button | P E J | - | | no | kept |
| 144 | Test connection -> | `#step-connect-test` | button | P E J | POST `/api/servers/test-connection` | | no | kept |
| 145 | Result panel | `#connectResult` | alert | P E J | - | success / failure with message | no | kept |
| 146 | Edit (back) / Save | `#step-result-back`, `#step-result-save` | buttons | P E J | POST `/api/servers` | | no | kept |

Total controls inventoried: 146 rows (about 175 individual ids and classes once the grouped rows are counted).

## 10. Critique (impeccable critique.md method)

Design-specificity verdict: the modal is generic Bootstrap: seven underlined tabs, labels with a trailing (i), text-heavy cards, green Save. It does not look like the redesigned dashboard (panel, pill, dot, ibtn language). It carries the most documentation in the product and the least hierarchy.

Heuristic read: visibility of status 2/4 (Health count is only a glyph; the tab height jumps per tab), match with real world 3/4, user control 3/4, consistency 2/4 (green Save vs amber primary everywhere else), error prevention 2/4 (no dirty state, no unsaved-changes guard), recognition 3/4, flexibility 3/4, minimalist design 1/4 (repeated helper text), error recovery 3/4, help 3/4 (good (i) coverage, but the same words are printed AND in the tooltip).

### P0 (correctness / data loss risk)
1. **No unsaved-changes guard.** Close, backdrop click or Esc discards edits silently, and all 7 tabs write on one Save. Change: track `dirty` (any input/change event in the modal); footer shows "Unsaved changes" and Save is enabled only while dirty; closing while dirty asks "Discard changes?" (inline confirm in the footer, not a native dialog). Keep `#editServerSave`.
2. **Modal height changes per tab** (lab shots: 410px on Path mappings, 940px on Webhook). The footer jumps under the cursor and Save moves. Change: fixed body height `min(70vh, 640px)` with the body scrolling and the footer pinned; on mobile the sheet is full-screen.
3. **Destructive/mutating actions look identical to navigation.** "Fix critical" / "Fix all" / "Remove from Plex" sit inline in long cards. Change: Fix buttons live in a sticky bar at the bottom of the Health tab (above the footer); "Remove from Plex" stays danger-outline and only appears when registered; the fix-plan and type-to-confirm modals stay (already good).

### P1
1. **Seven tabs wrap or truncate** ("Webhook & Scanner" forced `modal-xl`; mobile falls back to a select). Change: vertical left rail on desktop (200px, icon + label + badge), horizontal scroll-snap strip on mobile (sticky under the header, 44px targets). Keep the seven ids. Rename only the visible labels: General -> **Connection** (already in source), Webhook & Scanner -> **Webhooks** with sub-label "& scanner" is not needed; keep the current label because users search for "Scanner".
2. **Badges.** Setup Health shows a count badge (`14` red / `10` amber / none when healthy), not a glyph. Libraries shows `7/7` (amber when fewer enabled), Path mappings and Exclude paths show a row count when > 0, Webhook & Scanner shows a dot (grey not registered / green registered or scanner present). Set from the same data the probe already loads.
3. **Repeated helper text.** Rule: visible hint = purpose only, one line, <= 90 characters; everything else goes in the (i). Applied in the mock: Server URL, Verify SSL, Enabled (text removed), trickplay off-media, Plex helper, every Health row (description moves into the row's (i)), path-mapping intro, webhook prerequisites and steps, markers, chapter, loudness. Target: no paragraph longer than one line is visible on any tab except inside a collapsed "Setup steps" / "Worked example" disclosure.
4. **Test connection feedback is a grey line beside a small outline button.** Change: pill with state (idle / testing spinner / ok green dot "Connected - Plex 1.43.4" / warn / bad with message), `role="status"`, kept next to the button; the header status dot also updates. No latency is invented: show `version` only when the API returns it.
5. **Two sources of truth for "enabled".** Server enabled lives in a checkbox mid-form with its own caption. Change: header switch (same id `#editServerEnabled`), always visible; turning it off dims the rail and shows "Paused - no webhooks, scans or publishes" in the header (text is the existing (i)).
6. **Footer.** Cancel (ghost) + Save changes (amber primary, matches the dashboard; green is dropped). Save result and errors in the footer left (`#editServerResult`), not at the end of a long tab. Add: Ctrl/Cmd+Enter saves.
7. **Re-authenticate hidden in a text `<details>` summary**, the one place a broken token gets fixed. Change: a "Credentials" row on Connection showing "Signed in as <user> / token ending ...x4f2 (masked)" with a "Re-authenticate" button that expands the vendor form; when the last test failed with 401, auto-expand.
8. **Validation states.** Path/folder fields have red/green feedback only for the folder inputs. Change: one rule everywhere: red border + message below, green check icon at the right inside the field, message `role="alert"`; URL field gets an inline check for a missing scheme (client-side only).

### P2
1. Setup Health rows: severity as a left color bar + pill instead of a tinted icon + tiny tag; "Apply recommended" amber only on the first row, others secondary; buckets collapsible; "All good" collapsed by default.
2. Processing: the jump nav duplicates what a single scrolling column already gives; keep it only if the tab has 3+ sections (Plex), hide for Emby/Jellyfin (2 sections).
3. Markers status block: label/value definition list with chips, not mixed bold/muted lines.
4. Webhook tab: the Plex card and the vendor card are the same thing; show one "Webhook" card (state pill, URL + copy, steps collapsed once registered) followed by the "Recently Added Scanner" card. Plex keeps Register/Remove and the URL override.
5. Add flow: step indicator (1 Type, 2 Connect, 3 Verify), type cards with one-line description ("Sign in with Plex, servers auto-discovered" / "API key or login" / "Quick Connect, API key or login"), the "default ports" line shows only the chosen vendor, and for Plex the sign-in button comes BEFORE URL/name (URL is filled by discovery).
6. Light theme: the amber `--accent` on the dark rail needs `--accent` = `#b87a00` in light (already in shared.css); status pills use the tinted `-soft` variants, so contrast holds; checked in the mock.
7. Mobile 390: full-screen sheet (header 56px, tab strip 44px, sticky footer with two equal buttons, safe-area padding); tables (path mappings, exclude paths, libraries) become stacked rows with labels (no horizontal scroll).

### Do NOT change
Every id in sections 1-9, the payload shape sent by `saveEditedServer`, the three `server_id`-scoped webhook endpoints, `openEditModal(id, {openTab})` names (`health`, `markers`, `loudness`, `libraries`...), and the Setup wizard include of `_server_connection_form.html`.

## 11. Mockups

- `pages/servers.html` - servers page: spec-only state toggle (1 server / 5 servers) and theme.
- `pages/server-modal.html` - Edit modal for Plex / Emby / Jellyfin with all 7 tabs and the Add Server flow (type picker, connect per vendor, result), switcher is spec-only.
- Shots in `pages/shots/`: `servers-single`, `servers-two`, `servers-multi`, `servers-single-light`, `servers-single-mobile`, `servers-multi-mobile`; `modal-<plex|emby|jellyfin>-<general|health|processing|libraries|paths|excludes|automation>` (21 at 1440), `modal-add-type`, `modal-add-<vendor>-connect`, `modal-add-result`, `modal-plex-health-light`, `modal-mobile-{jellyfin-health,plex-libraries,plex-general,add-type}` (390, full-screen sheet).
- Servers page: rows below 3 servers, grid from 3 (fixed rows kept: header, host, issue, libraries, footer); Add-server tile / row at the end; summary strip; "Sends" chips use `server.markers.enabled`, `output.chapter_thumbnails`, `loudness.enabled` (already in the server object). Publishing counts are NOT in the card data, so none are shown.
- Mock URL params: `?type=plex|emby|jellyfin&tab=<section>&step=type|connect|result&probe=clean&sheet=1`.

## 12. Server modal parity

Checklist for the implementer. Tick after the redesign; every row must still be reachable per vendor.


Servers page (`/servers`)
- [ ] `#serverList`, `.edit-server-btn`, `.refresh-libraries-btn`, `.delete-server-btn`, `.server-enabled-toggle`, `#server-status-<id>`, `#server-error-<id>`, `.server-readiness-glyph` behaviour unchanged in both layouts (rows and grid).
- [ ] Layout picks rows for 1-2 servers and the grid for 3+; the Add-server tile/row opens `#addServerModal`.
- [ ] Empty state, load error and spinner still render; disabled servers skip probes and show the dimmed card.

Edit modal frame
- [ ] All seven `#edit-tab-*` panes exist and activate by id; `openEditModal(id, {openTab})` still opens `health`, `markers` (now Processing), `loudness` (Processing), `libraries`, `paths`, `excludes`, `automation`.
- [ ] `#editServerEnabled` now sits in the header; save still sends `enabled`.
- [ ] `#editServerSectionSelect` replaced by the scroll strip; one `activateSection()` backs both.
- [ ] Tab badges: Health count (bad if any must-fix, else warn, none when healthy), Libraries n/m, Path mappings and Exclude paths row counts, Webhook state dot.
- [ ] Dirty tracking: Save disabled until a change; close/Cancel while dirty asks to discard; Ctrl/Cmd+Enter saves.
- [ ] `#editServerResult` shows save errors in the footer.

Per type (P = Plex, E = Emby, J = Jellyfin)
- [ ] P: Plex config folder + browse + validation; Plex helper switch/address/key/state; token re-auth; chapter thumbnails; loudness; "When Plex has its own markers"; Direct Webhook register / re-register / remove, URL override + reset, status badge states; per-library BIF rows with "Enable (override)".
- [ ] E: Username+Password and API key re-auth (no Quick Connect); "When Emby has its own markers"; Emby webhook card (URL, copy, header, 5 steps); markers status with plugin version and Premiere warning; Intro & Credits only (no chapter, no loudness).
- [ ] J: off-media switch + Jellyfin config folder + browse + validation + warning; Quick Connect / password / API key re-auth; plugin opt-in + opt-out warning; Trickplay rows grouped by library; Jellyfin webhook card (URL, copy, header, 7 steps); markers status with plugin version.
- [ ] All: name, URL, Verify SSL, Test Connection (+ plugin panel feed for J), Libraries table with Previews + Intro & Credits (+ Loudness for P) columns and Refresh, Path mappings (3 columns, browse, validate, add, remove, apply to all), Exclude paths (value, path/regex, add, remove, apply to all), Recently Added Scanner card (badges, summary, Configure link, Scan all now, result).

Information and tooltips
- [ ] Every (i) that exists today still exists and keeps its full text; text removed from the visible hint is in the (i) (Server URL Docker note, Verify SSL, Enabled, off-media, Plex helper, Health row descriptions, push-notification quirk, auth header, delay override, markers, chapter, loudness, libraries columns).
- [ ] Visible hints are one line and <= 90 characters.
- [ ] Explain panels (`data-explain-template`) still open from the (i) buttons that had them (Server URL, off-media, Plex config folder, Setup Health overview, path mapping, webhook delay, webhook token, loudness requirements).

Add Server flow
- [ ] `.server-type-btn[data-type]` x3, step labels, `#serverUrl`, `#serverName`, `#auth-quick/#auth-pw/#auth-key` (Quick Connect Jellyfin only), username, password, API key, `#quickConnectStart` + code, Jellyfin trickplay notice, `#plexOAuthStart`, `#plexDiscoveredList`, manual `#plexToken`, `#plexConfigFolder` + browse, `#connectFormError`, Back, `#step-connect-test`, `#connectResult` (success and failure), `#step-result-back`, `#step-result-save`.
- [ ] The same `_server_connection_form.html` still renders inline in the Setup wizard.

Responsive and theme
- [ ] 390px: full-screen sheet, tab strip scrolls, 44px targets, tables stack with labels, footer buttons equal width, no horizontal page scroll.
- [ ] Light theme: pills, badges and diff chips keep contrast (uses the `-soft` tokens).

New (not in the current UI): Delete server in the modal (Connection, bottom), Unsaved-changes guard and footer message, step indicator in the Add flow, per-vendor port hint, credentials summary line. Remove any of these without affecting the rest if they are out of scope.

## Add flow top (revision)
- Header: 52px vendor-tinted tile (Plex amber, Emby green, Jellyfin blue-violet, neutral plus on step 1), 20px title "Add <Vendor> server", one-line purpose subtitle (replaces "Step n of 3" and the "Connect to <vendor>" H2).
- One full-width progress rail under the header: three equal steps with caption, done = green check, current = amber ring and bold label, connector fills 0 / 50 / 100%. At 390 only numbers plus the current step name show.
- Form left edge aligns with the title text (92px on desktop); modal height is auto (min-height only on step 1); footer unchanged.
- Shots: `modal-add-*`, `modal-mobile-add-plex-connect`, `modal-mobile-add-jellyfin-result`.

## Add flow: authentication methods (revision)
- Defaults follow `servers.js` (~L726): Jellyfin = Quick Connect, Emby = Username + Password; Emby has no Quick Connect button. Plex has no method switch: sign-in button first, manual token in a disclosure.
- The segmented control shows only the selected panel directly beneath it. Panels: Quick Connect card, Username + Password (note under Password), API key (show/hide button, where-to-find in the (i)).
- Quick Connect card states (spec toggle, also `?qc=idle|waiting|approved|expired`): idle (Start button + one-line hint); waiting (large code, Copy, hint "Enter this code in your Jellyfin profile menu", "Waiting for approval" pill, Cancel); approved (green pill); expired (struck-through code, red pill, "Get a new code"). Maps to `#quickConnectStart` / `#quickConnectCode` and the initiate, poll, exchange endpoints.
- Same segmented pattern applies to the Edit modal Re-authenticate forms (Jellyfin: 3 methods, Emby: 2).
- Shots: `modal-add-jellyfin-connect-{idle,waiting,approved,expired,password,apikey}`, `modal-add-emby-connect(-apikey)`, `modal-mobile-add-jellyfin-connect-waiting`.
