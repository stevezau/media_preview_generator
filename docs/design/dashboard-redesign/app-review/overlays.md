# Overlays review (modals, menus, dialogs, toasts)

Target mockup: `../pages/overlays.html` (spec-only switcher at the top opens every overlay; menus open on click or hover;
shots in `../pages/shots/overlay-*.png`). Real-app shots from the lab: `shots/overlay-real-*.png`.

Sources read: `_start_job_modal.html`, `_scan_filters.html`, `_manual_job_modal.html`, `_automation_schedules.html`
(schedule modal), `_job_details_modal.html`, `base.html` (navbar, offcanvas, confirm, info, toast, What's New),
`newmanual_jobs.js`, `scan_filters.js`, `folder_picker.js`, `schedule_modal.js`, `job_modal.js`, `notifications.js`,
`app.js` (`showNewJobModal`, `startNewJob`, `appConfirm`, `showToast`, `_openGlobalInfoModal`), `job_dialogs.css`.

Note on the lab app (127.0.0.1:18080): it runs an older build (no Plex loudness, no Workers / Global pause menu items, manual
modal still has a "Regenerate previews even if they already exist" checkbox). The templates in the repo are the truth;
the inventory below follows them. Job type is a **radio group** (one type per job), not checkboxes.

Legend: **kept** = same control/ID/behaviour, new look. **restyled** = same control, different widget (IDs and values stay).
**moved** = same control, new place. **new** = did not exist; every "new" row is optional and marked.

---------------------------------------------------------------------------------------------------------------------

## 1. Start New Job (`#newJobModal`, opens from Dashboard "Start new job")

| # | Control / state (real) | IDs / source | Mockup |
|---|---|---|---|
| 1 | Job type radio: Previews (default) | `#jobKindPreviews` `name=jobKind` | restyled: type card, blue |
| 2 | Job type radio: Intro & Credits + (i) | `#jobKindMarkers`, `#jobKindMarkersInfo` | restyled: card, violet, (i) kept |
| 3 | Job type radio: Plex loudness + (i) | `#jobKindLoudness` | restyled: card, teal, (i) kept |
| 4 | Intro & Credits mode radio: Find markers / Check servers + (i) | `#jobMarkersModeFind`, `#jobMarkersModeCheckServers`, `#jobMarkersCheckServersInfo`, group `#jobMarkersModeGroup` | restyled: segmented control, (i) beside it |
| 5 | Picking a type resets Priority (Previews Normal, others Low) | `onJobKindChange` | kept; the "default" tag moves with it |
| 6 | Libraries label + (i) | `#jobLibrariesGroup` | restyled: section "Libraries" + "What does this pick?" link opening the (i) dialog |
| 7 | "All Libraries" checkbox (default on, disables rows) | `#jobLibraryAll`, `toggleAllLibraries` | kept |
| 8 | Select all / None links | `setAllLibrariesChecked` | kept (top-right of the library box) |
| 9 | Caption "N libraries across M servers" | `#jobLibraryListCaption` | kept |
| 10 | Library list grouped by server: vendor logo + server name header, checkbox per library with type label, scroll at 240px | `#jobLibraryList`, `.job-library-checkbox`, `data-server-id`, `data-server-name`, `data-library-kind` | kept; header gets library count |
| 11 | Select a whole server | none | **new, optional**: "Select server" link per group (ticks that group's existing checkboxes) |
| 12 | Library states: Loading libraries..., "Can't load libraries right now. Check the Servers page", "No libraries available for this selection." | `_renderJobLibraryList` | kept; loading becomes skeleton rows, error stays amber with the Servers link |
| 13 | Scope badge: "Scanning every enabled library across all servers" / "Checking every library with Intro & Credits (Loudness) turned on" / "Scanning -> {server} only" / "Scanning -> N servers (cross-server fan-out)" | `#jobScopeBadge`, `_updateJobScopeBadge` | restyled: one-line scope bar, type colour (green when one server). Copy shortened, same four cases |
| 14 | Validation: no library ticked -> toast "Please select at least one library" | `startNewJob` | kept toast **and** new inline error under the list |
| 15 | Filter media `<details>`: summary line + "Edit filters/Hide filters" + chevron | `#jobScanFilters`, `data-filter-summary` | kept; summary text kept ("All selected media", "Added in last N days", "TV: latest N seasons per show", "Movies: 1990-any") |
| 16 | Filter hint "Filters combine. Only matching media is included in this run." + Clear button | `data-filter-clear` | kept |
| 17 | Scope note (hidden until needed): loading / no libraries / none selected / unknown library types | `data-filter-scope-note` | kept (hidden in mock, shown by the same rules) |
| 18 | Added to library select: Any time / Within the last... / Date range | `data-filter="added_filter"` | kept |
| 19 | Number of days (min 1, default 30) | `added_last_days` | kept |
| 20 | From / Through dates; error "Through date must be on or after From date." | `added_from`, `added_to` | kept + inline error |
| 21 | TV shows group: Seasons to include (All seasons / Most recent available seasons), Number of seasons per show (min 1), hint + (i) | `season_mode`, `latest_seasons`; fieldset `data-filter-type="tv"` hidden when no TV library in scope | kept |
| 22 | Movies group: Release year (All / Year range), From year, Through year, hint + (i); errors "Enter at least one release year.", "Through year must be on or after From year." | `movie_year_mode`, `movie_year_from/to`; `data-filter-type="movie"` | kept + inline errors |
| 23 | Footer note "Media missing a value an active filter needs is excluded." + (i) | `infoJob*ExcludedTpl` | kept |
| 24 | Whole-number validation ("Enter a whole number.") and filters block while libraries load (`data-loading`) | `MediaScanFilters.read/setLoading` | kept |
| 25 | Intro/Loudness note "Checks every eligible file in the chosen libraries." + (i); filters, mode and order hidden | `#jobOwnRunnerFiltersNote`, `_showJobKindControls` | restyled: info note; same hide rules |
| 26 | Check servers: libraries, filters, order, re-check all hidden | `_jobChecksServers` | kept; a note says "Checks every server. There are no libraries to pick." (**new** copy) |
| 27 | Re-check files already done (Intro only, not in Check servers) + (i) | `#jobMarkersForce` | restyled: switch |
| 28 | Processing Mode radio: Process only items missing previews / Regenerate all previews + (i) | `#jobMissingOnly`, `#jobRegenerateAll`, `name=jobProcessingMode` | restyled: segmented "Missing only / Regenerate all" (icons kept); hint changes to a warning line on Regenerate |
| 29 | Priority select High / Normal / Low + (i) (values 1/2/3) | `#jobPriority` | restyled: segmented control, same values; "default" tag |
| 30 | Processing Order select (Default, Newest, Oldest, Random) + (i) + hint + long (i) body | `#jobSortBy` | kept as select |
| 31 | Footer: Cancel, Start Job (play icon); closes via X / backdrop / Esc | footer | restyled: label follows the type ("Start previews job", "Start intro & credits job", "Start loudness job", "Check servers"); sticky |
| 32 | Footer summary | none | **new, optional**: "Previews, All libraries, Normal" echo of the choices |
| 33 | Error: submit retried once on network errors, failure toast "Failed to start job" | `_submitNewJob` | kept (toast) |

## 2. Process a file or folder (`#manualTriggerModal`, Dashboard "Manual trigger")

| # | Control / state (real) | IDs / source | Mockup |
|---|---|---|---|
| 1 | Job type radios Previews / Intro & Credits (+(i)) / Plex loudness (+(i)) | `name=manualJobKind`, `#manualKind*` | restyled: compact type cards |
| 2 | Per-kind help line (long text) | `#manualKindHelp`, `_MANUAL_KINDS[kind].help` | restyled: one line <= 90 chars; the long text goes in the (i) |
| 3 | "Search on" server select (non-Previews), note "Filters search results only. The job runs on enabled servers that own the selected files." | `#manualSearchServerGroup`, `#manualSearchServer` | moved: same slot as #4, label "Search on" |
| 4 | "Publish to which server?" select (Previews), note "Limits both the search above and where previews are published." | `#manualPublishServerGroup`, `#manualServerScope` | moved: label "Publish to", hint "Also limits the search below." |
| 5 | Server list load error "Could not load server choices..." | `manualSubmissionError` | kept (inline) |
| 6 | Find media label + (i) | `infoManualFindMediaTpl` | kept |
| 7 | Search input (debounce 250ms, Enter searches, Esc closes) with placeholder | `#manualSearchInput` | kept |
| 8 | Browse button -> folder picker | `#manualBrowseBtn`, `openFolderPicker({includeFiles:true})` | kept (inside the input) |
| 9 | Results dropdown: groups Shows/Movies/Episodes, row checkbox + clickable title (adds one), year, "N eps", "N folders", server badges (Plex/Emby/Jellyfin colours), "No matches.", "Searching...", error text | `#manualSearchResults`, `manualRenderSearchResults` | kept; group headers sticky |
| 10 | Tip row "Add an episode like S01E01 to pick just one episode." (only when a show is in results) | same | kept |
| 11 | Sticky "N selected / Add selected" bar once a row is ticked | `#manualSelectBar` | kept |
| 12 | Selected chips: kind icon, label, "Paths" disclosure, remove X; duplicate -> toast "Already added" | `#manualChips`, `manualAddSelection` | restyled: chip rows tinted with the job-type colour |
| 13 | Empty line "Nothing selected yet - search or browse above." | `#manualChipsEmpty` | restyled: dashed empty box |
| 14 | Clear all link | `#manualClearAll` | kept |
| 15 | "Or paste paths manually" details: textarea (mono, one path per line) + hint + (i) | `#manualAdvanced`, `#manualFilePaths` | kept (collapsed) |
| 16 | Path validation: "Pick at least one show, movie, file or folder."; "Use absolute container paths starting with /, without parent-directory (..) segments." | `#manualPathValidation`, `_manualPathError` | kept as inline errors |
| 17 | Processing Mode radios (Previews only) | `#manualMissingOnly`, `#manualForceRegenerate` | restyled: segmented |
| 18 | Re-check files already done (Intro only) + (i) | `#manualMarkersForce` | restyled: switch |
| 19 | Priority select + (i); defaults Previews Normal, others Low | `#manualPriority` | restyled: segmented |
| 20 | Submission error line (`role=alert`) "Failed to start job: ..." | `#manualSubmissionError` | restyled: red box above the footer |
| 21 | Start Job disabled until the path check passes; spinner "Starting..." while submitting | `#manualStartButton`, `_manualSubmitting` | kept; label follows the type, count in the footer ("N selected") |
| 22 | Success toast "Job Started" + kind label | `showToast` | kept |
| 23 | Form submit on Enter | `manualTriggerForm submit` | kept |

### Folder picker (`#folderPickerModal`, built by `folder_picker.js`)

| # | Control / state | Mockup |
|---|---|---|
| 1 | Title "Pick folders or video files" (or "Pick a folder") | kept |
| 2 | Up button (disabled at /), path input (Enter goes), Go button | kept |
| 3 | Breadcrumb | kept |
| 4 | Show hidden directories checkbox | kept |
| 5 | List: row checkbox + folder/file name (click opens folder), "No subfolders or videos.", Loading spinner row | kept |
| 6 | Error alert (listing failed) | kept (amber box) |
| 7 | Footer "Selected: path" or "N items selected", Cancel, "Pick this folder" / "Add N selected" | kept |

## 3. Add / Edit Schedule (`#newScheduleModal`)

| # | Control / state (real) | IDs / source | Mockup |
|---|---|---|---|
| 1 | Title Add Schedule / Edit Schedule; button Create Schedule / Save changes | `#scheduleModalTitle`, `#scheduleSubmitBtn`, `#scheduleEditId` | kept (both states in the switcher) |
| 2 | Group labels "What to run", "When", "Priority & order" | `.sched-group-label` | restyled: numbered sections 1 What to run, 2 Scope, 3 When, 4 Priority & order (numbers skip hidden sections) |
| 3 | Name (required) | `#scheduleName` | kept + inline "Name is required." |
| 4 | Scan mode radios: Full library scan (+(i), subline), Recently added only (+(i), subline), Intro & Credits (+(i)) | `name=scanMode`, `onScanModeChange` | restyled: three type cards (previews blue x2, intro violet). There is no loudness schedule mode; none added |
| 5 | Intro mode: Find markers / Check servers (+(i)) | `#scheduleMarkersFind`, `#scheduleMarkersCheckServers` | restyled: segmented |
| 6 | Lookback window select (15m ... 7d, default 1 hour) + hint + (i); Recently added only | `#scheduleLookback` | kept |
| 7 | Media Server select (All servers / each server) + hint + (i); hidden for Check servers | `#scheduleServer`, `onScheduleServerChange` | kept |
| 8 | Libraries: All checkbox, Select all / None, grouped list (220px scroll) | `#scheduleLibraryAll`, `#scheduleLibraryList`, `.schedule-library-checkbox` | kept; hidden for Check servers |
| 9 | Validation "Select at least one library or check All Libraries" (toast) | `saveSchedule` | kept toast + inline |
| 10 | Schedule Type radios: Specific Time / Interval / Cron Expression | `name=scheduleType` | restyled: segmented control, full width |
| 11 | Time (default 02:00) | `#scheduleTime` | kept + "Time is required." |
| 12 | Days checkboxes Sun..Sat (Mon-Fri default) | `.schedule-day` | restyled: seven toggle chips |
| 13 | Days validation "Select at least one day" | `saveSchedule` | kept + inline |
| 14 | Interval: Run every [n] + Minutes/Hours (default 2 hours), hint; min 1 | `#scheduleIntervalValue`, `#scheduleIntervalUnit` | kept + inline "Interval must be at least 1." |
| 15 | Cron expression (mono, placeholder `0 */2 * * *`) + hint + (i); must have 5 fields | `#scheduleCronInput` | kept + inline errors |
| 16 | Stop time (optional) + hint + (i); hidden for Interval | `#scheduleStopTime`, `#scheduleStopTimeGroup` | kept (shown for Time and Cron) |
| 17 | Plain-language summary of the trigger | none | **new, optional**: "Mon to Fri at 02:00, container time". Client-side text only |
| 18 | Job Priority select Default (from Settings) / High / Normal / Low + hint + (i) | `#schedulePriority` | restyled: segmented with Default first |
| 19 | Filter media (same component as Start New Job, prefix `schedule`); Full library scan only | `#scheduleScanFilters` | kept |
| 20 | Processing Order select; Full library scan only | `#scheduleSortBy` | kept |
| 21 | Enable schedule checkbox (default on) | `#scheduleEnabled` | restyled: switch, moved to the footer |
| 22 | Switching to Recently added on a new schedule pre-fills Interval 15 minutes | `onScanModeChange` | kept (try it in the mock) |
| 23 | Overlap-with-quiet-hours confirm ("Save anyway", warning) | `appConfirm` in `saveSchedule` | kept (see Confirm below) |
| 24 | Toasts: Schedule Created / Updated; errors "Failed to create/update schedule" | `showToast` | kept |
| 25 | Hidden inputs `#scheduleCron`, `#scheduleEditId`; edit preselects libraries after they load | `showEditScheduleModal` | kept |

## 4. Job details (`#logsModal`, Dashboard job row "Open logs and files", `/?job=` deep link)

| # | Control / state (real) | IDs / source | Mockup |
|---|---|---|---|
| 1 | Title = retry basename or library name; fallback "Job Details" | `_renderModalHeader` | kept |
| 2 | Status badge (+ tooltip) | `_statusMeta` | restyled: status pill (Completed ok, Running run, Failed bad, Pending warn) |
| 3 | Chips: job kind, source (manual/webhook/schedule icon), server name, duration, runs "N runs · 1 original + M retries / max" | `_jobKindBadgeHtml` etc. | restyled: type chip in type colour + tags |
| 4 | Job ID (mono, copy button with check feedback) in a "Job ID" disclosure | `#logsJobId`, `onCopyJobId` | moved: visible in the header on desktop; own line on mobile |
| 5 | Close X; Esc; backdrop; deep link `?job=&attempt=&tab=` and history state | `_pushModalState` | kept |
| 6 | Attempts bar (retry chains): pills per run (play = original, arrow = retry) coloured by status, duration, pending-server chips, "Run 1 (deleted)" disabled sentinel | `#attemptsDropdownWrap`, `_renderAttemptOption` | kept; mockup "Retry chain" variant |
| 7 | Chain-state chip: Chain completed / Completed with warnings / Chain failed / Cancelled / Attempt n/max running / Next attempt in m:ss (attempt n/max) / Pending, with (i) (not for loudness) | `#attemptsHint`, `_renderChainStateChip` | kept (live countdown) |
| 8 | Retry reason subtitle "Retried N times because {server} was still indexing" | `#retryReasonSubtitle` | kept |
| 9 | Results by server `<details>` + count, per server: logo, name, outcome chips (see dashboard expanded-row inventory) | `#jobServerResults`, `_renderPublishersBlock` | kept; open by default on desktop, collapsed < 768px (same rule) |
| 10 | Job information `<details>`: Files "N of M processed" / "N processed - total not reported", Priority, Requested paths, Created, Started, Finished, current item / wait reason, "Current file path" disclosure, job error | `#jobDetailsContext`, `_renderModalContext` | moved: always-visible facts strip (stops being a closed disclosure); wait reason and file path in a "Current activity / Waiting" box; error as a red "Job error" block at the top |
| 11 | Tabs Logs / Files | `#logsTab`, `#filesTab` | restyled: underline tabs, Files shows the count |
| 12 | Logs: filter input + clear (X), Auto-scroll switch | `#logsSearchInput`, `#logsAutoScroll` | kept |
| 13 | Attempt-scope subtitle "you're looking at Run N's log" | `#logsSubtitle` | kept |
| 14 | Load earlier logs / Load all | `#logsLoadEarlierBtn`, `#logsLoadAllBtn` | kept |
| 15 | Log viewer (`role=log`, aria-live polite, coloured lines, `No logs available` empty text), top/bottom scroll buttons | `#logsContent`, `colorizeLogLine` | kept |
| 16 | Footer line count "Showing last N of M log lines" | `#logsLineCount` | kept (under the log) |
| 17 | Files: view select Recorded outcomes / Requested paths (only webhook/file jobs) + note | `#fileResultsView`, `#requestedPathsNote` | kept |
| 18 | Outcome filter select (all outcomes, kind-specific: previews 7, intro 6, loudness 5, shared failed / file not found / gone from disk) | `#fileOutcomeFilter` | restyled: same options grouped by job type with `optgroup` |
| 19 | File search + clear, count "Showing a-b of N (M in list) - K items processed" | `#fileResultsSearch`, `#fileResultsCount` | kept |
| 20 | Table: File (basename, Inspector eye link, "Full path" disclosure), Outcome badge, Servers pills (vendor colour, dimmed when not published; "Server - Status" on own-runner jobs), Details (server notes: scrubber/chapters, markers message; reason), Worker (G0/C3 badge with full tooltip) | `#fileResultsTable` | kept; outcome chips use the shared chip language |
| 21 | Empty rows "No file results available" / "No matching files" | table | kept |
| 22 | Pagination: Show 50/100/250/500, info, page controls | `#filePerPageSelect`, `#filePaginationControls` | kept |
| 23 | Footer buttons: Retry now (pending retry), Cancel chain (active chain), Open BIF (when a preview exists), Copy, Download, Refresh (logs) / Refresh (files), Close | `#opAction*`, `copyLogs`, `downloadLogs` | moved: Copy / Download / Refresh become Logs-toolbar buttons (Files has its own Refresh); footer keeps only job actions: Retry now, Cancel chain, Open in Inspector, Close. "Open BIF" renamed "Open in Inspector" (it already opens the Inspector) |
| 24 | Polling while open (new log lines, attempts), Live state | `pollNewLogs` | kept; mock shows a "Live" hint on the Running variant |
| 25 | Operator actions confirm / error toasts (Retry now, Cancel chain) | `onOperatorRetryNow/CancelChain` | kept |

## 5. Navbar menus, notifications, help, mobile menu (`base.html`)

| # | Control / state (real) | Mockup |
|---|---|---|
| 1 | Brand + version link under it (tooltip "Release notes" / "Version X is available", update dot) | kept |
| 2 | Dashboard, Servers links (active state) | kept |
| 3 | Automation: word is a link to the page; hover (or ArrowDown / Space) opens: Triggers, Schedules; item active from the URL hash | restyled: icon tile + one-line purpose; **new**: click on the word's caret also opens (touch/laptop) |
| 4 | Settings menu: Workers, Global pause schedule, Processing Options, Intro & Credits, Logging, Authentication, Backups, About | restyled: two groups (Processing: first four, System: last four), one-line purpose each (from the existing `aria-label`s) |
| 5 | Tools menu: Inspector, Logs, Webhook Activity, divider, Run setup again (`?rerun=1`) | restyled: purpose line |
| 6 | Notification bell: badge count (9+), dropdown (stays open on outside-click mode), "Notifications" header, Reset ("Restore dismissed notifications") | kept; Reset renamed "Restore dismissed" |
| 7 | Notification entry: severity icon (info / warning / error), title, Show details / Hide details, sanitized HTML body, "Copy diagnostic bundle" (vulkan_probe only), Dismiss (until restart), Dismiss permanently (only if `permanent_dismissable !== false`; both only if `dismissable !== false`) | restyled: severity bar on the left; same buttons and rules |
| 8 | Empty state "You're all caught up." | kept |
| 9 | Toasts: "Dismissed", "Dismissed permanently", "Reset", "Error" | kept |
| 10 | Help menu: Report an issue, Ask a question, Documentation, Multi-server guide, divider, GitHub repository; external-link icons | kept |
| 11 | Buy me a coffee, Star, theme toggle, Logout (POST form with CSRF) | kept; mobile text labels kept |
| 12 | Mobile (< 1200px) offcanvas: header "Menu" + close; Dashboard, Servers, Automation / Settings / Tools expanding on tap; divider; notifications row ("N notification(s)" / "No new notifications" + badge); Help & feedback; coffee; Star on GitHub; Switch to light/dark theme; Logout; auto-closes on link tap but not on expanders | restyled: 48px rows, grouped "Account & help", Logout and version pinned to the sheet footer |

## 6. Shared dialogs and toasts

| # | Control / state (real) | Mockup |
|---|---|---|
| 1 | (i) info icon: hover tooltip (short text) + click opens `#globalInfoModal` from `data-explain-template`/`data-explain-html`; keyboard focusable; `aria-label` = tooltip | kept |
| 2 | Info dialog: title (default "About this setting"), HTML body (literal content only), optional "Open full documentation" link, Close | restyled: icon tile, readable body, docs link left, Close right |
| 3 | `appConfirm(message, {title, confirmText, cancelText, variant})`: icon by variant (danger/warning/primary), body, Cancel, confirm button in the variant colour; resolves false on dismiss | restyled: same options; mock shows danger (Clear job history), warning (Schedule overlaps quiet hours, "Save anyway"), cancel-with-custom-label ("Keep running") |
| 4 | Confirms in the app (13): Cancel job, Delete job, Clear job history, Clear jobs (by status), Delete schedule, Schedule overlaps quiet hours, Delete media server, Remove worker group, Clear webhook history, Skip setup wizard, Restore backup, Regenerate token, Set new token | kept: each keeps its message and button label (rule below) |
| 5 | `showToast(title, message, type)`: success / danger / warning / info icon, close X, one toast element (a new one replaces the old) | restyled: stack of up to 3, left colour bar, error toasts stay until closed |
| 6 | What's New (`#whatsNewModal`): version badge, name, date, notes, loading spinner, "Got it" | kept (shown in the switcher) |

---------------------------------------------------------------------------------------------------------------------

## Critique

Method note: inline design review (read the code, viewed the lab at 1440 and 390, built the mockup, re-viewed at both sizes
and in light theme). The impeccable detector pass was not run; a final design review should run it on the built pages.

### What works today (keep)
- Every control has an (i) and the dialog language is consistent (Bootstrap modal, Cancel left of an amber primary).
- Server-grouped library list, scope badge and scan filters are well thought out and must survive.
- Priority defaults follow the job type; validation exists for every filter field.

### P0 (ship with the redesign)
1. **Job type is invisible as a concept.** Three radios in a row, no colour, so "Previews / Intro & Credits / Loudness" does not match the dashboard (blue / violet / teal).
   Change: type cards (icon tile + name + one line + (i)) in all three creation dialogs (Start job, Process file, Schedule). The selected card takes the type colour and the header tile and summary chip follow it.
2. **The primary action says nothing.** "Start Job" is identical for four different jobs, and in the manual dialog it is disabled with no reason.
   Change: label follows the type ("Start previews job", "Check servers"), sticky footer with a one-line summary ("3 selected" / "All libraries, Normal"), the disabled reason is the inline error above ("Pick at least one ...").
3. **Long forms with no order.** Start job shows 7 blocks (type, mode, libraries, filters, mode, priority, order) in an order that is not the order of decisions; the Intro & Credits sub-mode hides half the form.
   Change: three numbered sections (1 Job type, 2 Libraries, 3 Options); numbers skip sections that are hidden for the type (CSS counters). Section "Options" order: Mode, (re-check), Priority, Order, Filter media.
4. **Job details footer mixes unrelated actions.** Eight buttons (Retry now, Cancel chain, Open BIF, Copy, Download, Refresh, Refresh files, Close) where half only matter on one tab.
   Change: Copy / Download / Refresh move into the Logs toolbar, Files keeps its own Refresh; the footer is only the job's actions plus Close.
5. **Job information is a closed disclosure.** Files processed, priority, created / started / finished are exactly what people open this dialog for.
   Change: always-visible facts strip under the header; error becomes a red block at the top; wait reason and current file in one "Current activity" box.
6. **Mobile is a shrunk desktop dialog.** 570px Bootstrap modals on a 390px phone, footer buttons off-screen on a short viewport (seen in the lab shot).
   Change: below 640px every creation / details dialog is a full-screen sheet with a sticky header and sticky footer (primary button full width); confirm becomes a bottom sheet.

### P1
1. **Repeated helper text.** Manual dialog: help line under the type (long), note under "Search on", note under "Publish to"; schedule has a hint under almost every field; Start job has hint + (i) + tooltip saying the same sentence.
   Rule applied: visible hint = purpose only, one line, <= 90 characters, otherwise nothing; every long sentence moves into the (i) dialog (the long manual-kind help texts, "Search scope does not restrict publishing", Loudness requirements).
2. **Radios for choices of 2 to 4 short options.** Processing Mode, Priority (a select with 3 values), Schedule Type, Intro mode.
   Change: segmented controls, same values and IDs. "Default" tag on the type's default priority.
3. **Priority/mode/regenerate parity.** Regenerate all rebuilds everything and looks identical to the safe option.
   Change: choosing it switches the hint to "Rebuilds every preview, including ones that already exist." in the warning colour (no extra confirm).
4. **Schedule modal.** Frequency radios plus four switching panels; stop time hidden for Interval; nothing says what will happen.
   Change: segmented frequency, day toggle chips, one summary line ("Mon to Fri at 02:00, container time", optional), Enabled switch in the footer, errors inline under the field (today only toasts, so the user must read a toast then find the field).
5. **Inline validation everywhere.** Real code reports through toasts or `reportValidity()` popups. Keep the toasts (existing flows and tests use them) and add an inline `.err` line (red, icon, one sentence) next to the field; the mock shows them with the "Spec: validation" chip in each dialog.
6. **Navbar menus are hover-only discoverable.** Hover opens them; the only other way is ArrowDown/Space.
   Change: caret click opens too; items get a purpose line; Settings is split into Processing and System groups (8 items is one long list); active item uses the amber bar.
7. **Notifications weight.** Two outlined buttons per card, one in destructive red, always visible.
   Change: severity bar, "Dismiss" primary-quiet, "Dismiss permanently" kept but not red until hover; "Restore dismissed" in the header.
8. **Toasts.** One shared element: a second toast replaces the first, errors vanish after 5s.
   Change: stack of up to 3, success/info auto-hide 5s, danger stays until closed, `role=alert` for errors; on mobile full-width above the safe area.
9. **Confirm copy.** The destructive button should name the object ("Delete schedule", "Clear history") and Cancel must be the default focus for danger variants. Existing `confirmText` values already do this for most; keep them.

### P2
1. Library list: per-server "Select server" link (optional, saves ~10 clicks on 5-server setups).
2. Filter media summary: keep the live summary line; add the active-filter count in the summary when collapsed.
3. Manual results: show the server badges in the job-type colour only on hover; today three colours compete with the type colour.
4. Job details: remember the last tab per session; show the Running variant "Live" hint.
5. Folder picker: show file size and a "Select all folders" link (not in the real dialog; skip if tests complicate).
6. Light theme: all overlays use tokens; verified in `shots/overlay-job-light-desktop.png` and `overlay-newjob-light-desktop.png`.

### Job-type colour language
Previews `--t-previews` blue, Intro & Credits (and Check servers) `--t-intro` violet, Loudness `--t-loud` teal. Used for: type card
when selected, dialog header tile, footer summary chip, chip rows in the manual dialog, job-details header chip. Never for status.
Status stays ok / warn / bad / run (green / amber / red / blue) in pills and chips. Amber is only the primary action and focus.

### States
- Empty: "Nothing selected yet" (manual), "No libraries available for this selection.", "No matching files", "No logs available", "You're all caught up."
- Loading: library skeleton rows, "Searching...", "Loading...", spinner on the primary button ("Starting...").
- Error: inline red line, red box above the footer for submit failures, amber note for scope problems; selection is kept after a failed submit.
- Keyboard: Esc closes the top dialog only; focus moves into the dialog on open and returns to the opener; the type cards are real radios (arrow keys move, visible focus ring); confirm focuses Cancel for danger.

## Do NOT change
- Element IDs, `name` attributes, `data-*` attributes, form values (1/2/3 priority, `missing`/`regenerate`, `jobKind` values) and the order of JS calls: e2e tests and `app.js` select them. Restyling means changing classes and wrappers, not IDs.
- API payloads (`/api/jobs`, `/api/markers/jobs`, `/api/loudness/jobs`, `/api/markers/reconcile`, `/api/jobs/manual`, schedules).
- The server-id collapsing rule (one ticked server -> `server_id`), the default-priority-per-type rule, hide rules of `_showJobKindControls` / `onScanModeChange`.
- (i) buttons keep `data-explain-title`, `data-explain-template` and `aria-label`.
- Every confirm message and confirm label; the quiet-hours overlap confirm.
- Modal deep links (`?job=`, `attempt`, `tab`) and polling.

## Tests likely affected
`tests/e2e/test_dashboard_modals.py`, `test_manual_job_kinds.py`, `test_intro_credits_jobs_ui.py`, `test_scan_filters.py`, `test_schedules.py`,
`test_info_icons.py`, `test_navbar_links.py`, `test_navbar_sticky.py`, `test_journey_notifications_lifecycle.py`, `test_dashboard_queue.py`,
`test_ux_polish.py`, `test_dashboard_polish.py`. Likely edits: selectors on radios that become cards (keep the inputs), Priority select
-> segmented (keep a hidden `<select id="jobPriority">` or update `select_option` calls), button labels "Start Job".

## Not mocked / known gaps
- Inline replacement states for the filter "loading" and the folder picker error are described, not drawn (one line in the picker).
- The "Check servers" sub-mode, Loudness variant and Recently-added schedule variant are switchable in the mock, but their notes are abbreviated.
- Real notification bodies are server-supplied HTML; the mock uses short samples.
- The live updates (log polling, countdown) are static text in the mock apart from the variant switch.

---------------------------------------------------------------------------------------------------------------------

## Overlays parity checklist (tick when built)

Start New Job
- [ ] 3 job-type radios keep IDs/values; Intro mode radios; priority reset on type change
- [ ] Libraries: All checkbox, Select all / None, caption, server groups, loading / error / empty states
- [ ] Scope badge: 4 cases (all, one server, many servers, per type wording)
- [ ] Filter media: summary, Clear, added / TV / movies fields, scope note, loading lock, 5 validation messages
- [ ] Own-runner note, Check-servers hides libraries/filters/order/re-check, re-check switch
- [ ] Processing Mode, Priority (1/2/3), Processing Order (4 options), (i) on each
- [ ] Footer label per type; Cancel; submit retry once; toast on success / failure

Process a file or folder
- [ ] 3 kinds, per-kind one-line help + (i) long text; priority default per kind
- [ ] Search on / Publish to swap; server list error
- [ ] Search (debounce, Enter, Esc), Browse, grouped results, checkboxes, tip row, Add selected bar, server badges, "No matches", "Searching..."
- [ ] Chips with Paths disclosure, remove, Clear all, empty line, duplicate toast
- [ ] Paste paths + 2 validation messages; Start disabled until valid; submission error; Enter submits
- [ ] Mode / re-check by kind
- [ ] Folder picker: up, path, go, breadcrumb, hidden dirs, list, error, selection count, confirm label

Add / Edit Schedule
- [ ] Add and Edit titles / button labels; hidden fields; library preselect on edit
- [ ] Name required; scan modes (full / recent / intro); intro Find / Check servers; lookback
- [ ] Server pin + libraries (hidden for Check servers); at-least-one-library validation
- [ ] Specific time + days, Interval (min 1), Cron (5 fields), Stop time (not for Interval); recent -> 15 min nudge
- [ ] Priority (Default/High/Normal/Low), filters + order (Full only), Enabled
- [ ] Quiet-hours overlap confirm; create / update / error toasts

Job details
- [ ] Header: title, status, kind, source, server, duration, runs, Job ID + copy
- [ ] Attempts bar, chain chip + (i), retry reason; Retry now / Cancel chain / Open in Inspector rules
- [ ] Results by server; facts (files, priority, requested, created, started, finished), current activity, file path, error
- [ ] Logs: filter, auto-scroll, attempt subtitle, load earlier / all, viewer, scroll buttons, line count, Copy / Download / Refresh
- [ ] Files: view select, outcome filter (all options), search, count, table 5 columns, eye link, Full path, pagination, empty states
- [ ] Deep link + history state, polling

Menus and shared
- [ ] Automation / Settings (8 items) / Tools (3 + Run setup again); hover, click, ArrowDown; active item
- [ ] Notifications: badge, severity, details toggle, copy bundle, Dismiss / permanently rules, Restore, empty
- [ ] Help (5 items), coffee, Star, theme, Logout, version link
- [ ] Mobile offcanvas: all of the above, expanders, notifications label, auto-close on link
- [ ] (i) dialog (title, body, docs link); confirm (3 variants, custom labels, all 13 messages); toasts (4 types, stack, error persists); What's New
- [ ] No horizontal page scroll at 390; dialogs full-screen below 640; light theme reviewed
