# Settings page review (/settings) — handoff for Codex

Mode: Operate. Scope: visual/structure pass. No logic changes. Design language: `../DESIGN-BRIEF.md`, `../mockup.html`.

## 0. Read first: the screenshots are stale

`shots/settings-desktop.png` and `settings-mobile.png` come from an older build. They show NO "Workers" or "Global pause schedule" section, no mobile section picker, and still show a "CPU Workers" block and "GPU Configuration" inside Processing. The current `settings.html` has `#section-workers` (line 53), `#section-worker-quiet-hours` (via `_worker_quiet_hours.html`), `#settingsMobileSection`, and no CPU Workers block. Re-shoot before judging the top of the page. Findings about Workers below are from reading code (`worker_groups.js`, `worker_groups.css`), not pixels. Findings about Processing, Markers, Logging, Auth, Backups, About are from pixels and still hold.

## 1. Verdict

- The page is complete and honest, but it is one 6000px wall: same card weight everywhere, long prose under every control, and no live "you are here" cue besides the sidebar.
- It does not yet speak the dashboard language: amber on 15+ things (card titles, icons, code chips, buttons), no task-type colours, Bootstrap-default controls, wide stepper boxes that fill the column.
- Biggest win: shrink helper prose into (i) tooltips or one-line hints, use tight two-column rows, and make the Workers editor reuse the dashboard group row (capability icons, type colours).

## 2. Findings

### P0

**P0-1. Danger zone is mixed in with a routine form (Authentication, desktop crop 3, y 1200-1500 + crop 4 top).**
Problem: "Regenerate Token" is a small red outline button under a calm paragraph, with a one-line "Warning" below it. "Set" is a loud amber primary. Both log everyone out; the safe-looking amber button is the destructive one too. Backups "Restore selected" is amber outline, same weight as a harmless "Refresh".
Change:
- Wrap Regenerate Token and the whole token card in a `.danger-zone` block: 1px `--bad` border at 35% alpha, `--bad-soft` header strip, title "Sign-in token", copy "Changing the token signs out every browser." Put the warning BEFORE the buttons, not after.
- Make "Set" `btn-outline-secondary` (not amber). Amber stays for the one primary action only (brief principle 3).
- "Restore selected": `btn-outline-danger btn-sm`, and keep the confirm step if one exists. Restore overwrites live config.
Files: `templates/settings.html` ~819-882 (`#section-auth`), ~885-927 (`#section-backups`), `static/css/pages/settings.css` (add `.danger-zone`). Keep `regenerateToken()`, `#newTokenLogoutBtn`.

**P0-2. Helper prose beats the controls (Processing, desktop crop 1, y 340-400, 500-530, 830-880).**
Problem: Max concurrent jobs has a 4-line paragraph under the slider AND an (i) tooltip. Auto-requeue has a 4-line paragraph AND an (i). The project rule is (i) tooltips for non-obvious labels. Prose of this size means nobody reads either.
Change: for each control keep ONE line of hint max (<= 90 chars, `--muted`, 13px) and move the rest into the existing (i) `title`. Examples:
- Max concurrent jobs: hint "Keep at 3 for most setups." Tooltip carries the 1-2 / 5-10 guidance and the High-priority slot rule.
- Incoming job priority: hint "Webhook and Recently Added jobs. Leave on High so new imports jump the queue."
- Auto-requeue: hint "Re-queue interrupted jobs after a restart. Older ones are marked failed." Rest to tooltip.
- Backups intro (crop 4, 215-280): 3 sentences at body size 16px is the loudest text on the page. Cut to one 13px muted line + tooltip. Move `jobs.db` note into the (i).
- Logging "System Log File" and "Job History" paragraphs: 13px muted, one sentence each.
Files: `templates/settings.html` `.form-text` blocks; do not remove `.form-text` nodes whose IDs JS writes to (see Do NOT change).

**P0-3. Section wayfinding is one-dimensional on a 6000px page (sidebar + header).**
Problem: sidebar exists (`#settings-sidebar`, sticky) but: no group labels, 8 flat links; "Global pause schedule" sits between Workers and Processing with equal weight; page title block scrolls away; on mobile the picker is a `<select>` that scrolls but does not show where you are while scrolling. The page-level save indicator `#saveStatusIndicator` lives in `.page-actions` and scrolls out of view, so the user changes a slider at y=4000 and sees no save confirmation.
Change:
- Make `.page-header` sticky under the navbar (`position: sticky; top: var(--nav-h, 3.5rem); z-index: 5; background: var(--bs-body-bg); border-bottom: 1px solid var(--surface-border)`), keeping `#saveStatusIndicator` in it. Compact to one line when stuck (hide `.page-subtitle`).
- Group sidebar links under 3 small caps labels (`--faint`, 11px, letter-spacing .08em): "Run" (Workers, Pause schedule, Processing, Intro & Credits), "System" (Logging, Authentication, Backups), "Info" (About). Add a `.nav-count` or state dot only where useful: dot on Pause schedule when enabled (`#quietHoursStateBadge` state), dot on Authentication never.
- Scroll-spy: confirm `settings_ui.js` toggles `.active` while scrolling (lines ~66-82 handle click/hash only; check for an IntersectionObserver). If absent, add one on `.section-card` with `rootMargin: -30% 0 -60% 0`.
- Mobile: keep the select but make it sticky inside the sticky header, and update its value on scroll via the same observer.
Files: `templates/settings.html` 11-51, `static/css/pages/settings.css` 50-110, `static/js/settings_ui.js`.

### P1

**P1-1. Worker-group editor does not match the dashboard worker cards (`worker_groups.js` lines 118-125 settings variant; `worker_groups.css`).**
Problem: settings row shows `Name`, text "Previews · Intro & credits", a "GPU hardware" `<details>` disclosure, a count, an "Edit" outline button and an "Enabled" switch. The dashboard already replaced all of this (brief: hardware subtitle, capability icon tiles, occupancy chip, pencil icon).
Change (reuse the dashboard row markup, same classes, so one CSS source):
- Left: icon tile (cpu / gpu-card), name 17px bold, hardware as muted subtitle under it. Drop `<details class="worker-group-hardware">`.
- Right, in order: capability tiles 28px (`bi-film` `--t-previews`, `bi-skip-forward` `--t-intro`, `bi-soundwave` `--t-loud`; enabled = type colour on `-soft`, disabled = grey), count stepper chip, clock icon only if the group has hours (tooltip with schedule text), pencil icon button (`btn btn-sm btn-outline-secondary`, `aria-label="Edit {name}"`), switch with NO "Enabled" text (aria-label stays, tooltip "Enabled").
- Disabled group: row at 60% opacity, hardware subtitle stays.
- Empty state (no groups / loading): replace "Loading worker groups…" paragraph with 2 skeleton rows; empty = dashed inset "No worker groups yet" + primary "Add group".
- Editor panel `#workerGroupEditor`: same two-column field grid as P1-2; capability choices as three toggle tiles (icon + label + type colour) instead of plain checkboxes.
Files: `static/js/worker_groups.js` (template strings ~118-125, ~317-332, editor ~370-470), `static/css/worker_groups.css`. Reuse the dashboard classes `.worker-group-indicators`, `.worker-group-kind`.

**P1-2. Form density: full-width controls and single column (Processing crop 1, y 270-760 and 1290-1340).**
Problem: number steppers stretch to 450px+ (Files checked at once, CPU workers) while the label above is 120px. Sliders span 940px with value in the label. Everything is one column, so the section is ~2400px tall.
Change:
- Add `.setting-row` = CSS grid `grid-template-columns: minmax(0,1fr) minmax(180px, 320px); gap: 8px 24px; padding: 12px 0; border-top: 1px solid var(--surface-border)`. Label + (i) + 1-line hint left; control right. Collapse to one column < 768px.
- Steppers: `max-width: 160px`. Selects: `max-width: 320px`. Sliders: control column width, current value as a right-aligned `tabular-nums` chip, not inside the label ("Max concurrent jobs: 3" becomes label "Max concurrent jobs" + chip `3`).
- Unit suffix ("seconds", "MB", "files", "days") as `.input-group-text` that is flush and `--faint` (it is currently a heavy dark box about the size of the input).
- Group rows under `.settings-subheading` with `--faint` caps style at 12px, not 16px bold + icon (title, then rows). Keep the icon, drop the bold.
Files: `templates/settings.html` (rows are `.mb-3` divs), `static/css/pages/settings.css`, `static/js/stepper.js` only for width.

**P1-3. Amber is everywhere (all crops).**
Problem: every card title icon, every `<code>` path, "Apply Log Level Now", "Restore selected", numbered 1-4 circles in "How it decides", and nav links are amber. Amber no longer means "act here" or "next".
Change:
- Card header icon: `--faint`. Title stays `--text`. Active sidebar link stays amber (it is wayfinding, the one allowed use).
- `<code>` chips: `--inset` background, `--text` colour, `--mono`.
- Step circles in "How it decides": neutral `--inset` with `--text`, except step 1 of 4 no accent.
- "Apply Log Level Now": `btn-outline-secondary`.
- Markers section icon: `--t-intro` violet (it is the Intro & credits type colour everywhere else). Loudness toggles, if present on this page, use `--t-loud`.
Files: `static/css/pages/settings.css`, `static/css/style.css` (`.card-header` icon rule).

**P1-4. Save feedback is invisible and inconsistent (`autosave.js` 160-195; Backups; quiet hours).**
Problem: autosave states ("saving / saved / error") render in `#saveStatusIndicator` at page top, off-screen while editing. Quiet hours has its own explicit `#quietHoursSaveBtn` — a different pattern on the same page with no explanation. Error state has no field-level cue.
Change:
- Sticky header (P0-3) keeps the indicator visible. Style: dot + text; saving = `--run` pulsing dot, saved = `--ok` check + "Saved" fading to "All changes saved" after 2s, error = `--bad` + "Not saved — Retry" button.
- On the changed control add `.is-saving` / `.is-saved` (1s `--ok` ring) so the user sees WHICH field persisted. Reuse existing autosave hooks; add classes only.
- Quiet hours: add hint "This section has its own Save." next to `#quietHoursSaveBtn` and use the sticky indicator for its result too. Do not change its behaviour.
Files: `static/js/autosave.js` (class toggles only), `static/css/pages/settings.css`, `templates/_worker_quiet_hours.html`.

### P2

**P2-1. Intro & Credits: toggles and list are close to good but unbalanced (crop 2, y 700-1480).** "What to detect" column is 3 rows tall beside a 7-row sources list; left column is mostly empty. Make "What to detect" a compact horizontal strip of three toggle tiles (Intros / Credits / Recaps) above the sources list at full width. Source rows: type name 15px, 1-line hint 13px `--muted`; the "TheIntroDB" API-key input moves behind a small "Key" icon button that expands inline. Arrows up/down + drag handle are three reorder affordances in one row; keep drag + arrows but hide arrows until row hover/focus on desktop (keep visible on touch). "(optional)" field with no label: add visible label "API key (optional)".

**P2-2. "How it decides" 4 cards + "Rules that surprise people" are help content, not settings (crop 2-3).** Collapse the whole block into one `<details>` "How decisions are made" (closed by default) containing the 4 steps in a 4-col grid. Saves ~600px. Keep IDs `#markersHowItDecidesLabel`.

**P2-3. "Advanced" at the bottom of Markers (crop 3, y 400-470) gives no hint of contents.** Chevron + "Only change this if you know what you're doing." Make it a `<details>` styled like a row with a count ("Advanced · 4 options") and a `--warn` shield icon.

**P2-4. Backups list (crop 4).** Each file is a 3-line block. Make it one table-like row: `settings.json` mono name | live-saved time (`--muted`) | snapshot select | `Restore` danger-outline | `10 snapshots` chip. The "no backups yet" state: `--faint` chip inline, drop the second explanation line. "Refresh" becomes an icon button in the card header. "Advanced: pruning rules" `<details>` uses inline `style="cursor:pointer"`; move to CSS.

**P2-5. About (crop 4 bottom).** "Latest release: 4.4.2" teal alert is full-width and louder than the app name. Replace with the dashboard footer pattern: version mono + "Up to date" / "Update 4.4.2" pill (`--ok-soft` / `--warn-soft`). "View on GitHub" and "Release notes" as two buttons. Note: shot shows "Installed: local build" while Latest is 4.4.2 — an unreleased build must not show "update available" in warning colour; use `--faint` "dev build".

**P2-6. Card header style.** Uppercase letter-spaced 12px title is fine for a divider but weak as a landmark. Use 15px/600 sentence case, plus a one-line section description in `--muted` right under it. That replaces the page subtitle list.

**P2-7. Accessibility.** `.info-icon` chevron suffix ("ⓘ >") appears only for tooltips that overflow (`_applyInfoIconAffordance`). Fine, but make sure each (i) is a `<button>` with `aria-label="About {label}"`. Placeholder-only fields (token inputs, TheIntroDB key) need real labels. Disabled stepper "-" at min has contrast under 3:1 in the dark theme; use `--faint`.

## 3. Per-section notes

- `#section-workers`: P1-1. Intro line above rows: "Groups decide which hardware runs which task types." Add the "Add group" primary here, not hidden in the editor. Keep the `?worker_group=` deep link from the dashboard pencil: opening it must scroll to and open that group's editor.
- `#section-worker-quiet-hours` (Global pause schedule): status badge `#quietHoursStateBadge` ("off") is a Bootstrap grey pill. Use dot + text: `--ok` "Active window now" / `--warn` "Paused now" / `--faint` "Off". Day picker uses `.btn-check` pills; fine. Merge visually with Workers (same group "Run"). Its own Save button: see P1-4. Empty state (no windows): dashed inset "No pause windows. Add one."
- `#section-processing`: split into three sub-cards-less groups with `.settings-subheading` + `.setting-row`: Job execution, Library scanning & thumbnails, Smart caching. Keep `<hr>` only between groups. `#gpu-configuration` "GPU device tuning" + `#gpuConfigList`: per-GPU row should use the Worker-group row look (icon tile, name, mono `cuda:0`, vendor logo), with Workers and FFmpeg Threads as two compact steppers right. "Re-scan GPUs" becomes an icon button in the subheading row. Detecting state `#gpuDetecting` keep. The "GPU worker can't process… retries on CPU" note: one-line `--inset` hint with an info icon; currently a black box wider than the content. "Auto right now: 32 (…)" is cyan text: use `--faint` and drop the second sentence into the (i). HDR tone mapping help text changes with the select; keep, but 1 line.
- `#section-markers`: P2-1, P2-2, P2-3. The "Shared by every server… Servers → Edit → Intro & Credits" line is good; make "Servers" a small button-link. Section icon violet.
- `#section-logging`: Log level select and "Apply Log Level Now" belong on one row (select left, button right). Rotation size + Retention + Keep history: three `.setting-row`s in one two-column grid. "Rotation & retention changes take effect on next restart." becomes a `--warn` info chip next to those two fields only.
- `#section-auth`: P0-1. Token inputs get visible labels (they have: "New token", "Confirm"); add show/hide eye toggle. Put "Set" under the inputs on mobile. "New token generated" alert `#newTokenLogoutBtn` is `btn-danger`: ok, but add a "Copy" button first and make it the primary action; the user must copy before logging out.
- `#section-backups`: P0-2 (copy), P0-1 (restore weight), P2-4.
- `#section-about`: P2-5.

## 4. Mobile (390px) — from `settings-mobile.png` + CSS read

- Screenshot has no section picker; current template has one (`.settings-mobile-nav`, sticky at `top: 4.4rem`). It is a full-height card with label + select + feedback line (about 110px) glued to the top. Reduce to a single 44px select row, drop the label (use `aria-label`) and the "Showing Workers" feedback line (keep it `visually-hidden` with `aria-live`).
- Page title + subtitle eat 150px. In the sticky header (P0-3) show only title + save dot on mobile.
- Section cards use side padding 16px inside a 16px page gutter: the real form width is about 326px. Drop `.card-body` padding to 12px at <576px and remove the card side border radius mismatch.
- Prose paragraphs (P0-2) are 6-9 lines each at this width; the page is 8766px tall. P0-2 alone should cut ~1500px.
- Steppers stretch to full width (crop m0, 1660-1760). Cap at 160px (P1-2) so they do not look like text fields.
- `.settings-sidebar` is hidden < lg: no sidebar; scroll-spy for the select needed (P0-3).
- Touch targets: (i) icons are about 16px. Give `.info-icon` a 32px hit area via padding with negative margin. Reorder arrows in the source list are about 12px: min 32px on touch.
- Backups restore row: select + Restore + "10 snapshots" wrap awkwardly; stack select full width, then Restore full width with the chip above.
- Worker group rows: capability tiles + stepper + pencil + switch will not fit in a row at 390. Put tiles on a second line under the name; keep stepper, pencil, switch right.
- Code chips like `/config/logs/jobs/` overflow; add `overflow-wrap: anywhere` on `code`.

## 5. Do NOT change

- Any setting key or what it writes to `settings.json` (`gpu_config`, `webhook_*` legacy key names, `config_backup_*`, log, retry, requeue, thumbnail, markers-related keys, token fields). Labels may change; keys must not.
- Element IDs used by JS/tests: `#saveStatusIndicator`, `#settingsMobileSection`, `#settingsMobileSectionFeedback`, `#settings-sidebar`, `#workerGroupSettings`, `#workerGroupEditor`, `#workerGroupSearch`, `#workerGroupFilters`, `#workerGroupSummary`, `#workerGroupHold`, `#gpuConfigContainer`, `#gpuConfigList`, `#gpuDetecting`, `#gpuRescanBtn`, `#gpu-configuration`, `#quietHours*` (`quietHoursEnabled`, `quietHoursSaveBtn`, `quietHoursAddWindowBtn`, `quietHoursWindows`, `quietHoursStateBadge`, `quietHoursWindowTemplate`), `#configBackupKeep`, `#configBackupMaxAgeDays`, `#backupRestorePanel`, `#refreshBackupsBtn`, `#newTokenLogoutBtn`, `#markersSourcesLabel`, `#markersHowItDecidesLabel`, all `section-*` ids (also targeted by the navbar dropdown `data-anchor` and by `/settings?worker_group=ID#section-workers`).
- Inline handlers `regenerateToken()`, `rescanGPUs()`; data attributes `data-edit`, `data-enable`, `data-day`, `data-time`, `data-window`, `data-remove-window`, `data-group-id`, `data-group-indicator`, `data-bs-toggle="tooltip"`.
- Autosave wiring (`autosave.js` option `indicatorId`, the `.autosave-indicator` class and its `saving/saved/idle/error` states). Add classes; do not rename or remove.
- `/api/worker-groups` request shapes and the `worker-groups-updated` custom event.
- Behaviour: auto-save on change (except the explicit Save in quiet hours), the token-regeneration flow (copy then logout), restore confirm flow.
- The "Markers: source order" drag-and-drop result (order is persisted).
- `.info-icon` class and `_applyInfoIconAffordance` contract (`tests/test_info_icon_markup.py`).

## 6. Test impact

Run after changes: `pytest --no-cov -n 0 tests/test_routes.py tests/test_info_icon_markup.py`, then e2e `pytest -m e2e -n 8 --no-cov` on the files below.

- `tests/test_routes.py`: asserts `settings-sidebar` and `section-processing` markup. Sidebar regrouping (P0-3) must keep those strings and link hrefs.
- `tests/test_info_icon_markup.py`: scans templates for (i) markup. Converting prose to tooltips must use the `.info-icon` button pattern.
- `tests/e2e/test_settings_page.py`: `section-processing`, `gpuConfigList`, `info-icon`, Regenerate. Danger-zone restyle (P0-1) must keep the button text and the `regenerateToken()` path.
- `tests/e2e/test_worker_groups.py`: `worker-group-row`, `worker-group-hardware`, `quietHours*`. P1-1 removes `.worker-group-hardware` `<details>` in settings; update this test to look for the subtitle instead.
- `tests/e2e/test_ux_polish.py`: `settingsMobileSection`, `quietHours`, `gpuConfigList`. Mobile picker slimming (section 4) must keep the select, its `value` updates, and the feedback node.
- `tests/e2e/test_navbar_menus.py`: nav dropdown anchors and `settings-sidebar`.
- `tests/e2e/test_info_icons.py`, `test_wizard_step4_processing.py` (shares `gpuConfigList`): any change to GPU row markup affects the setup wizard too, so check `setup.html`.
- `tests/e2e/test_intro_credits_settings.py`, `test_intro_credits_server_tab.py`: Markers section selectors (P2-1, P2-2). If "How it decides" moves into `<details>`, tests reading its text need `open`.
- `tests/e2e/test_dashboard.py`, `tests/test_config.py`: mention settings; spot check only.
- Docs: if labels or sections move, update `docs/guides.md` (Settings screenshots/text) and regenerate `python scripts/generate_llms_full.py` (`tests/test_llms_full.py` checks staleness). `tests/e2e/snapshots/regen_readme.py` regenerates README shots.

## Settings: parity

Note: `worker_groups.js`, `_worker_quiet_hours.html` and `worker_groups.css` contain NO timeline graph (the lab app on :18080 is an older build with no Workers section, so it could not be captured). The week graph in the mockup is a new, derived view of the same windows data: no new data or API. Day presets (Every day / Weekdays / Weekends) are also new, UI-only shortcuts.

Workers (`#section-workers`, `worker_groups.js`):
- Add group, Add CPU group for loudness -> yes, panel header
- Capacity line (scheduled now / weekly peak, timezone) -> yes, top of panel
- Group row: name, hardware, job-type icons, hours text, state label (Available, Within group hours, Outside hours, Finishing current files, Workers busy, Globally paused, Disabled), available/running/finishing counts, next opening time, worker count, Enabled switch, Edit -> yes, each row
- Edit opens in place under that group, others collapse -> yes
- Editor: Name, Resource (CPU/GPU), Workers (1-32 CPU, 1-16 GPU), Jobs allowed (3 types, loudness disabled on GPU + note), Availability (Always / Weekly hours), windows (days, Start, End, remove), Add time window, timezone note, Apply group changes, Close, Duplicate, Remove -> yes
- Warnings, "Unsaved group changes" Apply / Discard bar, status message, validation text -> yes
- Footnote on current files finishing -> yes
- Removed from the old mockup (not in the app): "Who can start jobs", FFmpeg threads and free-text Active hours in the group editor.

Global pause schedule (`_worker_quiet_hours.html`): state badge, tooltip, description, Enable switch, Add window, Save, windows (Pause at, Resume at, Start days Mon-Sun, remove, title), timezone / Pause All footnote -> all yes. Week graph of pause windows -> new.

Processing: Max concurrent jobs, Incoming job priority, Retry count, Initial retry delay, Auto-requeue, Max requeue age, GPU tuning (per-GPU workers, FFmpeg threads, Re-scan), Files checked at once, Frame interval, Thumbnail quality, HDR tone mapping, Reuse frames / TTL / disk cap -> yes (Processing section).
Gap: per-GPU enable switch and failed-GPU card (red, error detail, disabled toggle) exist in `gpu_config_panel.js`; added to the mockup GPU rows.
Intro & Credits: detect intro / credits / recap, sources list, TheIntroDB key + Clear key, credits windows TV/Movies, How decisions are made -> yes.
Logging: Log level, rotation size, retention, job history days -> yes. Authentication: custom token + confirm, regenerate, copy, logout -> yes. Backups: refresh, per-file snapshot restore, keep N, max age -> yes. About: version, links -> yes.

## Implementation parity

Inventory of the page BEFORE the change, each confirmed after (templates `settings.html`, `_worker_quiet_hours.html`; JS `worker_groups.js`, `gpu_config_panel.js`, `autosave.js`, `settings_ui.js`, new `week_graph.js`). No settings key, autosave ID or persisted payload changed.

- Workers: Add group, Add CPU group for loudness, capacity line, per-group row (name, hardware, job-type icons, state, counts, next opening, hours, worker count, Enabled switch, Edit), inline editor under that group (Name, Resource, Workers, Jobs allowed, Availability, windows with days/Start/End/remove, Add time window, timezone note), Apply / Discard bar, warnings, message, Duplicate, Remove, Close, footnote, `?worker_group=` deep link, `worker-groups-updated` event untouched -> yes.
- Global pause: state badge, tooltip, description, Enable switch, Add window, Save, windows (Pause at, Resume at, Mon-Sun, remove), timezone footnote -> yes. New: week graph, "Next pause" hint, Every day / Weekdays / Weekends presets, empty state, nav dot.
- Processing: Max concurrent jobs, Incoming job priority, Retry count, Initial retry delay, Auto-requeue, Max requeue age, GPU tuning (FFmpeg threads per GPU, Re-scan, detecting, failed GPU), Files checked at once (+ Auto hint), Thumbnail interval (+ slow-path notice), Thumbnail quality, HDR tone mapping (+ live description), smart caching switch / window / size / multi-server hint -> yes.
- Intro & Credits: detect intro / credits / recap, source list (drag, up/down, per-source switch, unavailable badge + reason), TheIntroDB key / Clear key / usage, How it decides (now a closed `<details>`), See how decisions are made, Advanced credits windows -> yes (TheIntroDB key sits behind a key button; a stored key opens it).
- Logging: Log level, Apply Log Level Now, rotation size, retention, job history days -> yes.
- Authentication: custom token + confirm (+ show/hide), Set, Regenerate token, new-token panel (Copy, log me out) -> yes.
- Backups: Refresh, per-file snapshot select + Restore (confirm flow in app.js untouched), keep N, max age -> yes.
- About: installed / latest version, update message, GitHub link, release notes link -> yes.
- Header: `#saveStatusIndicator` states (saving / saved / idle / error + retry), mobile section picker + feedback node, sidebar with scroll-spy -> yes.

Gaps and calls:
- The pause state badge text still comes from `schedules.js` ("off", "paused now", "on (N windows)"); only its look changed (dot + text). "Active window now" needs a change there.
- Pause Save still reports through toasts (`schedules.js`); it does not use the sticky indicator.
- Settings-mode GPU rows show only FFmpeg threads: per-GPU enable and workers do not exist in this mode (worker groups own them), so the mockup's per-GPU switch was not added.
- The backups list is built by `app.js`; it is restyled with CSS only (danger zone, the "no backups yet" explanation line hidden).
- Header stays one line at all scroll positions (subtitle sits beside the title) so its height never shifts after an anchor jump.
- `markers_server_tab.js` and `loudness_server_tab.js` serve the Servers edit modal, not this page, and were not touched.
- Local tokens: `--settings-panel`, `--settings-nav-h`, `--settings-header-h` in `pages/settings.css`; `--panel` from the mockup is not in `style.css`, `--panel-2` is used instead.
- Tests changed: `tests/e2e/test_ux_polish.py` (the layered "More detail" toggle is now the auto-requeue (i) dialog), `tests/e2e/test_intro_credits_settings.py` (open the closed "How it decides" first).
- Docs (`docs/guides.md`, `llms-full.txt`) not updated; label case changed in places ("Thumbnail interval").
