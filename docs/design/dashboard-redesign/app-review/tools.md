# Tools pages review: Logs, Webhook activity, Inspector, BIF viewer

Mode: Operate. Language reference: `DESIGN-BRIEF.md` + `mockup.html` (segmented tabs with counts, outlined filter buttons, pills with icon, mono faint IDs, always-visible icon-only row actions, tabular numbers, amber only for primary action / job IDs).
Shots: `shots/{logs,webhook-activity,inspector,bif-viewer}-{desktop,mobile}.png`. Caveats: (a) `bif-viewer-*` are the Inspector. `/bif-viewer` is a 302 to `/inspector` (`routes/pages.py` L135), so there is no BIF viewer page to restyle. (b) The logs and webhook shots look older than the current templates (no page title on Logs; no filter form, Refresh or search on Webhook activity), so findings cite templates. Re-shoot before sign-off. (c) No light-theme shots; light rules read from CSS only. One inline pass, no `impeccable detect` run.

## Cross-page findings (apply first)

**T-P0-1. Three different page headers.** Logs: full-bleed, 1.5rem title in `.log-heading`. Webhook: `.container-xxl` + `.page-header` + icon + 1.75rem title. Inspector: own crumb "TOOLS > INSPECTOR" + own title and left gutter 128px vs Webhook 152px (compare shots).
Change: one header for all three, Inspector's pattern: crumb (`Tools › <Page>`, 11px caps, `--faint`) above `h1.page-title` (no icon), subtitle `--muted` 14px, actions right-aligned in `.page-actions`. Put it in a shared include `templates/_tools_header.html` (args: title, subtitle) or a `{% macro %}`; style `.page-header` in `style.css` L308 (crumb: `font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--faint)`). Same container (`container-xxl`) for Webhook and Inspector. Logs keeps its full-height body but the header uses the same left padding.

**T-P0-2. Tokens not available.** See `shell.md` S-P0-1. Do that first or `var(--ok-soft)` etc. resolve to nothing on these pages.

**T-P1-1. Shared building blocks** (add to `style.css`, not per page): `.seg` (segmented tabs with `.count`), `.fbtn` (outlined filter button: `height:32px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--muted)`), `.pill.{ok,bad,run,warn}` with icon (24px high, 12px/600), `.ibtn` (32px, transparent, muted, hover `--inset`; `.danger` hover `--bad-soft`), `.mono-id` (`font-family:var(--bs-font-monospace); font-size:12px; color:var(--faint); font-variant-numeric:tabular-nums`). Copy values from `mockup.html` L85-100.

## A. Logs (`/logs`)

### 1. Verdict
- Capable viewer (levels, regex, source, raw, wrap, group, download) but the toolbar is a dense strip of 14 mixed controls.
- Level colour floods the whole row; mixed with teal text it is hard to scan for the one warning that matters.
- Mobile gives 40% of the screen to controls.

### 2. Findings
**L-P0-1. Info text is teal everywhere** (`logs-desktop.png`: every message and level is teal; the thing to find, warnings and errors, does not stand out). `style.css` L1566 `.log-level-info{color:#20c997}` reaches the message. Change in `pages/logs.css`: message `color: var(--bs-body-color)`; only the level word is coloured: info `var(--run)`, debug `var(--faint)`, warning `var(--warn)`, error/critical `var(--bad)`, and give warning/error rows a `var(--warn-soft)` / `var(--bad-soft)` background. Info rows get no tint. Keep the 3px left border colour already set (L52-57 of logs.css) but note brief rule: no side stripes on cards; a log row border is a status marker, keep it only on warning/error, remove for info/debug.
**L-P0-2. Not monospace.** Timestamps and messages use the sans font, so columns do not align. Set `#logContainer { font-family: var(--bs-font-monospace); font-size: 12.5px; line-height: 1.55; font-variant-numeric: tabular-nums }` for `.log-ts`, `.log-mod`, `.log-msg`. Level label fixed 5ch, upper case.
**L-P1-1. Toolbar to the queue pattern.** Today: `btn-group` of 4 level buttons coloured cyan/amber/red, search group, 3 outlined buttons, 2 selects, 2 checkboxes, then badges.
Change in `logs.html` L15-50 (keep every ID and `data-level`):
- Level buttons become a `.seg` with the same 4 buttons (Debug / Info / Warning / Error), selected = `--inset` fill + `--text`, no cyan/amber/red outlines. Counts per level optional (add `<span class="count">`).
- Search stays, 32px, `border-color: var(--line)`; regex and clear stay inside the input group.
- `View` select, `Source` select, `Wrap lines`, `Group by source` move into one outlined `.fbtn` "View" dropdown (popover with those four controls). IDs `logView logSource logWrap logGroupSource` stay in the DOM.
- Clear, Copy, Download become 32px `.ibtn` with `aria-label` + `title` (Clear is `.danger`).
- Right side: `serverLevelBadge` becomes a neutral `.pill`; `logCount` tabular-nums; `liveIndicator` + `logConnStatus` merge visually into one pill: `.pill.ok` with pulsing `.dot.live` when connected, `.pill.bad` with `x-circle` when not, `.pill.warn` while connecting. Keep both element IDs (JS writes text).
**L-P1-2. Empty/loading/error.** `#logPlaceholder` is plain muted text. Use a centred state: icon `terminal`, "Waiting for log lines", subtext "Messages appear here as the app writes them". Filter-no-match state: "No lines match. Reset filters" with a button. Error: `#logFeedback` becomes `.alert`-style row with `--bad-soft`, icon and Retry. Keep `logPlaceholder`, `logFeedback`.
**L-P1-3. First line clipped under the toolbar** (`logs-desktop.png` top row is cut). Add `padding-top: .5rem` to `#logContainer` and `scroll-padding-top`.
**L-P2-1. Light theme.** The panel is forced dark (`logs.css` L112+). Fine for a terminal; but the toolbar is also forced dark, which clashes with light header (dark bar under a white navbar). Make the toolbar follow the theme (`--panel`/`--line`) and keep only the log body dark.
**L-P2-2. Scroll buttons** (`.log-nav-btns`): fine; ensure 36px targets and `aria-label`s (currently only `title`).

### 3. Mobile (`logs-mobile.png`)
- Toolbar plus status takes ~210px. Collapse to: level `.seg` full width, search + a "Filters" icon `.fbtn` (opens the View popover) on one row; actions go in the popover or a kebab. Status pill on the same row as the count.
- Row layout: message wraps mid-bracket (`[Lab / Plex]`). At <576px make `.log-line` a 2-line grid: line 1 = `ts level module` (module truncated with ellipsis), line 2 = message full width. In `logs.css` change grid-template-columns to `auto auto 1fr` with `.log-msg { grid-column: 1 / -1 }`.
- Scroll buttons overlap the last line; add `padding-bottom: 4rem`.

### 4. Do NOT change
- All IDs: `logContainer logSearch logSearchRegex logSearchClear logClearBtn logCopyBtn logDownloadBtn logView logSource logWrap logGroupSource logCount logConnStatus liveIndicator serverLevelBadge followPill loadOlderBtn logScrollTop logScrollBottom logFeedback logPlaceholder`, `.log-level-btn[data-level]`, `.log-line` classes `log-level-*`, `expanded`, `log-raw-view`, `log-no-wrap`.
- Socket, history load, regex, grouping, raw view logic. localStorage key `logViewerLevel`.
- Full-height layout (`.log-page` calc height).

### 5. Test impact
`grep` hits: `tests/test_routes.py`, `tests/e2e/test_info_icons.py`, `tests/e2e/test_journey_settings_save_reload.py`, `tests/e2e/test_operational_followup.py`. Check for `.btn-outline-info` / level-button class assertions and for the three controls that move into the View popover (they must stay clickable in tests: open the popover first or keep them in the DOM, visible). Run `pytest --no-cov -n 0 tests/test_routes.py -k log` and the e2e files above with `-n 8`.

## B. Webhook activity (`/webhook-activity`)

### 1. Verdict
- Clear table but status and source are plain bold text on dark chips; no icon, no colour cue (`webhook-activity-desktop.png`).
- Each file shows twice (Queued row then Triggered row, same title); the pair reads as duplicates.
- Filters (in the template) use a labelled 5-column form, not the queue's segmented tabs.

### 2. Findings
**W-P0-1. Status = pill with icon.** `.status-badge` (`pages/automation_triggers.css` L16-22) uses Bootstrap rgba fills. Replace with tokens and icons, label kept (Operate mode wants scan speed; icon + colour carry state, the word stays because there are 7 states): triggered = `.pill.ok` + `check-circle-fill`; queued = `.pill.warn` + `hourglass-split`; ignored/disabled = neutral `.pill` + `dash-circle`; ignored_no_path(s)/error = `.pill.bad` + `exclamation-triangle-fill`; test = `.pill.run` + `beaker`. Change the JS string at `webhook_activity.html` L241 and the class map; keep `status-badge` class and `statusLabelMap` text.
**W-P0-2. Same-file duplicates.** Group consecutive events with identical title+source+server within 60s into one row with a "2 events" mono chip? That is a logic change. Visual-only fix: fade the Queued row (`opacity:.7`) and indent the Triggered row's chevron; plus a faint `title` tooltip. If product agrees, group later.
**W-P1-1. Filters to the queue pattern.** `#historyFilters` (L26-31): replace the four labelled fields with one row: `.seg` of Outcome tabs with counts (All / Triggered / Queued / Ignored / Failed) driven by `#historyStatus` values; `#historySearch` 32px with search icon; Source and Server as `.fbtn` dropdown buttons. Keep the four form control IDs in the DOM (hidden selects are fine if the buttons set them and dispatch `input`). Reset becomes a text button shown only when a filter is active. `#historyFilterCount` becomes tabular-nums right-aligned.
**W-P1-2. Table row.** Header: 11px caps `--faint`, sticky inside the card (currently 14px bold white; heavier than the content). Time column: relative text + `title` with absolute time, tabular-nums, `--muted`. Source: small tile/pill with vendor colour kept (radarr orange, sonarr green) via soft token bg. Server: `All` in `--faint`; named servers as a pill. Row hover `var(--inset)`. Chevron expander: 28px `.ibtn` instead of a bare blue caret; the blue is not a token, use `--muted`/`--text`.
**W-P1-3. Actions.** "Clear" stays a destructive outlined button; make it `.btn.danger` style (hover red). Refresh becomes a 32px `.ibtn` with `arrow-clockwise` and `aria-label`. Header actions go in `.page-actions`.
**W-P1-4. States.** `#historyEmpty`, `#historyNoMatches`, `#historyLoading`, `#historyError` are three different layouts. Unify into one `.empty-state`: 36px icon tile, bold line, muted line, primary/secondary button (Empty: "Open Automation → Triggers" as button; Error: Retry). Loading: 5 skeleton rows (`--inset` bars) instead of a spinner.
**W-P2-1. Pagination.** Bootstrap blue active page (`1` in the shot) violates the amber-only rule; set `.page-item.active .page-link { background: var(--accent); border-color: var(--accent); color: var(--accent-ink) }` (brief: "pagination with amber active page"). Per-page select 32px.
**W-P2-2. Light theme.** The grey rows and dark source chips (`rgba(...,0.2)`) lose contrast on white; the token swap in W-P0-1 fixes it. Check `.table` striping uses `--inset`.

### 3. Mobile (`webhook-activity-mobile.png`)
- Cards-per-row are good. Fix: Source/Server chips have a stray `·` and black rectangles; make the first line `time · Source pill · Server pill` wrapping, status pill right-aligned on line 1, title on line 2 (2-line clamp, 15px/600).
- Row tap target is the chevron only (22px). Make the whole title line the toggle, min 44px.
- Filters at 390px: segmented tabs scroll horizontally (`overflow-x:auto`, no wrap); search full width; the two `.fbtn` share a row.
- Pagination: info text above, controls below, centred.

### 4. Do NOT change
- Element IDs: `historyRefresh historyFilters historySearch historySource historyStatus historyServer historyFilterCount historyError historyNoMatches historyLoading historyEmpty historyTable historyBody historyPaginationFooter historyPerPageSelect historyPaginationInfo historyPaginationControls section-webhooks-activity`.
- Functions `loadHistory`, `clearHistory`, `changeHistoryPerPage`, `renderHistoryPage`; localStorage `historyPerPage`; status keys and labels; the `files_preview` expander behaviour; the `timeAgo` helper.
- Link back to Automation → Triggers (keep text, make it a button in the empty state; keep the subtitle link).
- Note: `automation_triggers.css` is also loaded by Automation. Edit status-badge/source-badge there carefully or move them to `webhook_activity.css` and leave Automation's copy.

### 5. Test impact
Hits: `tests/test_routes.py`, `tests/e2e/test_dashboard_polish.py`, `tests/e2e/test_worker_groups.py` (grep matched `status-badge`/`history-table`). Check for class-name assertions on `.status-badge.triggered` and for the filter selects being visible. Run those plus `grep -rn "webhook-activity" tests | head`. Add a test per status key (queued, triggered, ignored, ignored_no_path, error, test, disabled) asserting the pill class and icon.

## C. Inspector (`/inspector`)

### 1. Verdict
- Best-built page of the set: own scoped system, strong empty state and search; but it is a separate visual language (`--insp-*` tokens, IBM Plex from a CDN) that already drifts from the app.
- Header pattern is the right one; the others should copy it (T-P0-1).
- Empty state is a lot of empty space under one field (`inspector-desktop.png`).

### 2. Findings
**I-P1-1. Map `--insp-*` to the shared tokens.** `.insp` defines ~60 colours (`pages/inspector.css` L5-60). Do not rewrite 2,500 lines. Alias only: `--insp-ok: var(--ok)`, `--insp-bad: var(--bad)`, `--insp-blue: var(--run)`, `--insp-faint: var(--faint)`, `--insp-line: var(--line)`, `--insp-amber: var(--accent)`; and in the Intro lane use `--t-intro` (violet) not blue, Loudness lane `--t-loud` (teal), previews `--t-previews`. Brief principle 2: one hue per task type everywhere. Today intro is blue (`--insp-blue`) and loudness unset.
**I-P1-2. Font drift.** IBM Plex Sans + Mono load from jsDelivr (`inspector.html` L8-14), the rest of the app does not. Either (a) keep Plex Mono only for IDs/paths/timestamps and use the app sans for UI, which drops 4 of 7 font requests; or (b) leave as is. Recommend (a). Offline Docker installs fall back silently today; (a) makes that harmless.
**I-P1-3. Empty state.** After the hint line nothing helps. Add under the search: a "Recent files" row (last 5 inspected, from localStorage) or three example chips ("Paste a path", "Search a title", "Try a recent job"). Minimum change: `.insp-empty` card with icon `film`, "Search to inspect a file" and the hint text moved into it. Keep `#inspHint` id.
**I-P1-4. Search field.** Amber focus ring on the field is right (primary action). The scope select and info icon sit inside the input; at mobile the placeholder truncates ("e.g. The Matrix,"). Shorten placeholder to "Title or path" below 576px (use `data-short-placeholder` toggled in JS, or CSS `::placeholder` can't switch; do it at render).
**I-P2-1. Section nav** (`#inspSectionNav`: Timeline / Loudness / Evidence / Servers) should be the `.seg` style and sticky below the navbar (`top:57px`).
**I-P2-2. Result rows.** Status text in results (`Couldn't check`, `-`) uses `insp-state-muted`; render as `.pill` with icon (`bi-exclamation-circle`) for error, `bi-dash` for none. Server names as mono-faint chips.
**I-P2-3. Errors.** `messageCard('Couldn't open this file', ...)` is good. Add a Retry `.btn` and a "Copy path" `.ibtn`.

### 3. Mobile (`inspector-mobile.png`)
- Placeholder truncation (I-P1-4). Select + info icon eat 40% of the field width: move the scope select under the input on <576px (own row, full width).
- Crumb and title fine; lead text 2 lines OK.
- Verify Timeline at 390px (not in the shot): lanes need horizontal scroll inside the card, 44px tap targets for frame tiles.

### 4. Do NOT change
- `#inspector #inspSearch #inspQuery #inspScope #inspHint #inspResults #inspFolded #inspShowResults #inspSectionNav #inspFile` and anchors `#inspTimeline #inspLoudness #inspEvidence #inspServers`.
- Query params `?path= ?q= ?bif= ?file=`; the `/bif-viewer` 302 with `tab` dropped.
- Marker colour semantics inside the timeline (credits amber, intro) beyond the token alias in I-P1-1: check with owner before recolouring intro from blue to violet because markers docs and the lab references use the current colours.
- `inspector.js` render functions; do not touch the 3,369-line JS for styling.

### 5. Test impact
Hits: `tests/e2e/test_inspector_behaviours.py`, `test_inspector.py`, `test_inspector_menu_entry.py`. They use `.insp-*` classes and IDs; token aliasing and header changes do not break them. Moving the scope select on mobile and the empty-state card needs a check. Run `pytest -m e2e -n 8 --no-cov tests/e2e/test_inspector*.py`.

## D. BIF viewer (`/bif-viewer`)

### 1. Verdict
- There is no separate page: the route redirects to the Inspector. `bif-viewer-desktop.png` is byte-different but visually identical to the Inspector shot.
- Nothing to restyle. The Tools menu already lists only Inspector, Logs, Webhook Activity.
- Remaining work is wayfinding: a bare-BIF open (`?bif=`) should say so.

### 2. Findings
**B-P2-1.** In the Inspector's bare-BIF mode, show a "Preview file" pill in the file header (already added at `inspector.js` ~L841) using `.pill` (neutral) with `bi-file-earmark-image`. No other change.
**B-P2-2.** Breadcrumb for redirected links: still reads "Tools › Inspector". Correct. Do not add "BIF viewer".
**B-P2-3.** Docs: grep `docs/` and `README.md` for "BIF viewer" and update to "Inspector" if found (not checked here).

### 3. Mobile
Same as Inspector.

### 4. Do NOT change
- The `/bif-viewer` route and its query-param pass-through (old bookmarks).
- Do not re-create a `bif_viewer.html`. `.claude/CLAUDE.md` still lists it under `templates/`; that line is stale (not edited here).

### 5. Test impact
`grep -rn "bif-viewer" tests | head` before touching; the redirect has coverage in `tests/e2e/test_inspector_menu_entry.py` or routes tests. No expected breakage.

## Webhook activity: title alignment
The expander chevron sits in a fixed 24px box and the paired-row indent is gone, so Title text starts at the same x on every row (measured: one left offset across all rows, desktop and mobile).

## Inspector: function inventory

Source: `templates/inspector.html`, `static/js/inspector.js` (3,369 lines, read in full), `routes/api_inspector.py`, plus the live app at :18080 (empty, results, file with no preview and nothing checked). `folder_picker.js` is not used. Mockup: `pages/inspector.html`. Every row works in the mockup; the Spec button (bottom-left) switches page state and file variant, and `?path= ?file= ?view=season ?bif= ?q= ?state=&variant=` deep links load them.

| # | Current feature | Where it lives in the mockup |
|---|---|---|
| 1 | Title or path field, live search after 300 ms, 2+ chars | `#inspQuery` in the search card |
| 2 | Hint line under the field | `#inspHint` |
| 3 | Server scope select (All servers + each server), re-runs search | `#inspScope`; filters the results |
| 4 | Scope info tooltip | ⓘ beside the select |
| 5 | Path starting with `/` shows "Open this file"; Enter opens it | Type a path; chip "Paste a path" |
| 6 | Enter runs the search at once | Input keydown |
| 7 | "Searching…" state | Spec: Searching |
| 8 | "Nothing found for ..." | Spec: No match, or type `zzqx` |
| 9 | Search failed (red text with reason) | Spec: Search error |
| 10 | Results header TITLE / PREVIEW / INTRO & CREDITS | `.res-head` (hidden at 390, rows stack) |
| 11 | Row: title (year), meta line (kind, quality, servers) | `.res` rows |
| 12 | Row preview status: Ready N frames, Missing, Couldn't check, "…" while loading | Preview column; fills 0.7 s after the search |
| 13 | Row intro & credits status label | Intro & credits column |
| 14 | Show rows: chevron, "Pick an episode" | Dune: Prophecy, Severance rows |
| 15 | Show panel states (reading folders, error, none found) | `.show-panel` (loaded state shown; the other three are text rows in the app) |
| 16 | Season buttons with aria-pressed | `data-season` seg in the panel (Severance has 2) |
| 17 | Episode cards: code + marker state, open the file | `.ep` cards |
| 18 | Recent files (from the earlier mockup, kept) and empty-state chips | `#vEmpty` |
| 19 | Deep links `?path= ?file= ?view=season ?bif= ?q=` | Honoured on load; Spec shows the current link |
| 20 | URL updates when a file opens; browser back to results | `syncUrl` (pushState) |
| 21 | Folded back button "Results for “q”" / "New search" | `#inspShowResults` |
| 22 | Section nav Timeline / Loudness / Evidence / Servers, hides missing sections | `#inspSectionNav`, sticky |
| 23 | File title with year/episode sub-title | `#inspTitle`, `#inspTitleSub` |
| 24 | Chips: Film/Episode, length, quality, Locked by you, Preview file | `#inspChips` |
| 25 | File path with copy button and toast | `#inspPath`, `#copyPath` |
| 26 | Regenerate preview + ⓘ | `#inspRegenerate` |
| 27 | Check intro & credits now (unchecked) / Re-detect (checked) + ⓘ | `#inspRedetect` |
| 28 | Lock with confirm bar (Lock and send to ..., Leave them) | `#inspLock`, `#inspLockConfirmRow` |
| 29 | Back to automatic with confirm bar | `#inspUnlock`, `#inspUnlockConfirmRow` |
| 30 | Adjust (disabled while adjusting or locking) + ⓘ | `#inspAdjust` |
| 31 | Buttons disabled while a request is in flight | `S.busy` after a click |
| 32 | Live job banner: spinner/hourglass, %, "Open on the Dashboard"; file refreshes when the job ends | `#inspJobBanner`; Spec: Job running; start Regenerate to watch it finish |
| 33 | Versions bar: N files, switch, "this one", ⓘ | Spec: Two files |
| 34 | This episode / Whole season toggle + ⓘ | `#inspScopeEpisode`, `#inspScopeSeason` |
| 35 | Tile: On your servers (all variants, click to jump to Servers when attention) | `#tileServers` |
| 36 | Tile: We found (times, source or "set by you") | `[data-tile=found]` |
| 37 | Tile: Preview (frames, interval, date, size, covers; partial "Stops at"; none) | `[data-tile=preview]`; Spec: Partial / No preview |
| 38 | Tile: Intro & credits checked (date, found/none; Never) | `[data-tile=checked]` |
| 39 | Timeline title, range, ⓘ with how-to dialog | Timeline panel header |
| 40 | Legend: ours / a server's own | Panel header (hidden at 390) |
| 41 | Jump-to chips (ours, server's own, disabled "No intro") | `#inspJumps`; Film variant shows "No intro" |
| 42 | Overview bar of thumbnails | `#inspOverview` |
| 43 | Our intro/credits bands on the overview | `.ovbar .band` |
| 44 | Viewport box on the overview | `.ovbar .box` |
| 45 | "Now" time bubble | `#inspOvNow` |
| 46 | Click to jump, drag to scrub the overview | pointer handlers on the bar |
| 47 | Axis ticks, last tick dropped when crowded | `#ovAxis` |
| 48 | Readout: time, frame N of M, one every 10 s | `#inspNow`, `#inspFrameText` |
| 49 | Readout tag (Story, Intro on every server, server starts at ..., own marker here) | `#inspNowTag` |
| 50 | Step back/forward 10 frames | `#stepBack`, `#stepFwd` |
| 51 | "Click a frame to see it large" hint | Readout right side |
| 52 | Strip: tiles with times, drag, wheel to sideways, focusable, Left/Right one frame | `#inspStrip` |
| 53 | Centre line and highlighted current tile | `.centre`, `.tf.now` |
| 54 | Lanes: We found + one per server with status dot and notes (Not checked yet, Couldn't read, Nothing yet · next job adds ...) | `.lane`, `.lname` |
| 55 | Bands: ours tinted, own grey dashed, text, hover tip, click to jump | `.band` (global tooltip) |
| 56 | Edge lines with flags; flags that would overlap are hidden; own edge within 2 s of ours not drawn | `.edge` |
| 57 | "No preview" tiles | Spec: No preview / Partial |
| 58 | Frame-number mode when length is unknown | Spec: Length unknown |
| 59 | "Length isn't known" empty strip | Shown when no length and no preview (code path in `buildTimeline`) |
| 60 | Bare preview file mode (`?bif=`): no lanes, Preview file chip | Spec: Preview file only |
| 61 | Large frame dialog: time, N of M, tag, Previous/Next, Left/Right, Esc, backdrop click, strip follows | `#inspFrameDialog` |
| 62 | Adjust panel per type with live range | `#inspAdjustPanel`, `.atype` |
| 63 | Exact frames one second apart, click to set edge, in/out tint, ringed frame, edge flag lines, own dashed lines | `.frames` in Adjust |
| 64 | Start/End time inputs (m:ss, invalid state + message) | `[data-input]` |
| 65 | ±1 s nudge | `[data-nudge]` |
| 66 | "Runs to the end of the file" checkbox (credits) | `#inspToEnd-credits` |
| 67 | Add intro / Add credits when not found (no intro for films) | `#inspAdd-*` (appears after you clear a type; film variant has no intro row) |
| 68 | "Can't be adjusted" note when no server can show a type | Code path only: `cantRow` in the app; not reachable with these servers |
| 69 | Validation message, Save disabled | `.problem` (set End before Start) |
| 70 | Save and send to ..., Cancel, Sending…, result toast, locks | `#inspAdjustSave`, `#inspAdjustCancel` |
| 71 | Season card: title, summary, legend | `#inspSeason` |
| 72 | Publish N to M servers + ⓘ (disabled states) | `#inspPublishSeason` |
| 73 | Season rows: scaled lane + bars, intro, credits, FROM chips (+N, Locked), server dots with tooltips; click opens the episode; current highlighted | `.srow` |
| 74 | Season loading and error states | Same loading card and error card as a file |
| 75 | Loudness: per server status, per track header + metrics (5), notes, normalization, analysis version | `#inspLoudness` |
| 76 | Evidence rows: source, explanation, found, note, icon (used / warn / none / own) | `#inspEvidence` |
| 77 | "You" locked row in Evidence | `#inspLocked` (Spec: Locked) |
| 78 | Not checked card with Check now | `#inspNotChecked` |
| 79 | Item error card | `#inspItemError` (Spec: Intro & credits unreadable) |
| 80 | Servers card: pill, letter tile, shows line, plan line, reason, publish note, error, Plex versions note, preview line | `#inspServers` |
| 81 | File locations disclosure | `details.loc` |
| 82 | Loading card "Reading this file…" | Spec: Loading |
| 83 | Couldn't open this file (+ Retry, copy path, from review I-P2-3) | Spec: Error |
| 84 | Not in any library | Spec: Not in any library |
| 85 | Gone from disk + "Search for it" | Spec: Gone from disk |
| 86 | ⓘ tooltips and toasts everywhere | Global `#tip`, `#toasts` |

Count: 86 inventoried, 86 present (59 and 68 are visible only via their code path or a specific data case, noted above).

Kept from the earlier mockup: crumb + page header, recent files, empty-state chips, panel-style sections, Spec state control (now a popover so it also works at 390).

## Implementation parity (Logs and Webhook activity)
Logs, every control still present: level buttons (`.log-level-btn[data-level]`, disabled below the server level, `aria-pressed`), search + regex + clear, View/Source/Wrap/Group by source (now inside the View dropdown `#logViewBtn`; IDs unchanged), Clear/Copy/Download (icon buttons in the header), server level pill, line count, live/connection pill (`#liveIndicator` text + `#logConnStatus` kept, visually hidden), `#logFeedback` (now with icon and a Retry button for history failures), Load older, follow pill, scroll top/bottom, expandable lines, raw view, wrap toggle, grouping, `logViewerLevel` localStorage, socket and history logic. New: "No lines match" state with Reset filters, "Waiting for log lines" empty state. Fixed in passing: the follow pill never showed because JS cleared its inline display while CSS said `display:none`; it now sets `inline-flex`. Not done: per-level counts on the level tabs (loaded lines are already filtered by the server level, so the counts would mislead); the shared `_tools_header.html` include (crumb is inline per page).

Webhook activity, every control still present: Refresh (`#historyRefresh`), Clear, search, Source/Outcome/Server filters (hidden selects remain the state; Outcome is now tabs with counts, Source/Server are dropdowns), Reset filters (type=reset, shown only when a filter is active), filter count text, error/no-matches/loading/empty states, table columns, status label + `status-badge` class per status key, source badges, per-server cell (name pill / em-dash / All), file expander (button plus whole title line), files detail, pagination, per-page select with `historyPerPage`, link to Automation → Triggers. Outcome tabs list the exact status keys (Triggered, Queued, Ignored, Error always; No file path, Test, Disabled when present) instead of the mockup's grouped "Failed", because the filter matches exact status. Sticky table header not done (the table scrolls with the page).

### Implementation parity (Inspector)

Restyle only: `templates/inspector.html`, `static/js/inspector.js` (icons, empty state, error actions; no behaviour removed) and `static/css/pages/inspector.css`. Checked by the 153 existing Inspector e2e tests (all pass, none loosened) plus 3 new ones (`TestPageChrome`), and screenshots in `impl/inspector-*.png`. Shared tokens used as they are; the one local token is `--insp-panel` (card surface, `#212136` dark / `--bs-secondary-bg` light) because `style.css` has no card token.

| Rows | Still works | Where |
|---|---|---|
| 1-3, 5-6 | yes | `#inspQuery`, `#inspScope`, Enter / path row in `inspector.js` `runSearch`, `renderPathRow` (now with icon tile) |
| 4 | yes | `.insp-scope-info` tooltip, now beside the select (below it at 390) |
| 2 | yes | `#inspHint`, inside the search card |
| 7-9 | yes | `showMessage` text rows (`div.insp-row`) |
| 10-14 | yes | `renderResults`: head, icon tile, title/meta, preview and intro cells, show chevron. At 390 the head is hidden and the statuses stack under the title |
| 15-17 | yes | `renderShowPanel`: loading/error/none text rows, season buttons (`aria-pressed`, now a seg), episode cards |
| 18 | yes | `#inspEmpty` card with "Paste a path" and "Search a title" chips ("Try a recent job" dropped by owner decision; the header Logs button was removed too, Logs is in the Tools menu) and `#inspRecent` (last 5 opened files, localStorage) |
| 19-21 | yes | `openFromUrl`, `setUrl`, `#inspShowResults` unchanged |
| 22 | yes | `#inspSectionNav` now a sticky seg at every width (was mobile-only); hides missing sections via `syncSectionNav` |
| 23-25 | yes | `#inspTitle` (now an h2; the page h1 is `#inspSearchTitle`, always visible), `#inspTitleSub`, `#inspChips`, `#inspPath` + copy button |
| 26-31 | yes | `actions()` buttons and ⓘ, lock/unlock confirm bars, adjust, `state.busy` disabling: JS untouched |
| 32-33 | yes | job banner, versions bar |
| 34-38 | yes | scope toggle and the four tiles |
| 39-47 | yes | timeline head (icon, range, ⓘ + how-to dialog, legend hidden at 390), jump chips, overview bar, bands, box, bubble, scrub, axis |
| 48-53 | yes | readout, tag, step buttons, hint, strip drag / wheel / keys, centre line |
| 54-60 | yes | lanes, bands, edges, no-preview tiles, frame-number mode, empty strip, bare `?bif=` mode |
| 61 | yes | `#inspFrameDialog` unchanged |
| 62-70 | yes | adjust panel, frames, inputs, nudge, to-end, add, can't-adjust note (code path), validation, save / cancel |
| 71-74 | yes | season card, publish, rows, loading and error states |
| 75-81 | yes | loudness card (icon heading), evidence rows (restyled icons), locked row, not-checked card, item error card, servers card, file locations |
| 82, 84, 85 | yes | loading, not in any library, gone from disk (+ Search for it) |
| 83 | yes, added | `openErrorCard`: Retry (`#inspRetry`) and Copy path (`#inspErrorCopyPath`) |
| 86 | yes | `info-icon` tooltips and toasts unchanged |

Count: 86 of 86 fully. Also changed: IBM Plex fonts dropped (app sans + `--mono`); intro is now violet (`--t-intro`) per the mockup, which contradicts the "check with owner" note in section 4 above.
