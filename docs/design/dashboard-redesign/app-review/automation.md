# Automation (/automation: Triggers and Schedules)

**Verdict**
- Useful but a wall of prose: the Triggers tab is ~3400px tall, 4 stacked cards of paragraphs, mixed link colours and inline code.
- The overview "how do you add media?" table is the right idea but is dense, uses text-link actions, and breaks on mobile.
- Fix: one scannable decision list with buttons, setup docs collapsed, settings grouped, all in the dashboard card language.

Shots: `shots/automation-desktop.png` (Triggers), `shots/automation-mobile.png`. Files: `templates/automation.html` (shell, sidebar, scrollspy JS), `_automation_triggers.html`, `_automation_schedules.html`, `static/css/pages/automation.css`, `automation_triggers.css`, `automation_schedules.css`.

## P0
1. **Mobile overview grid is unreadable** (mobile shot, "How do you add media?"): each row is a 2-column grid and the right column (action) squeezes to ~90px with text wrapping per word; the left column text wraps in a 140px column.
   Change: below 768px make each row a single column: title, one-line description, then a full-width button/link-button below. Selector: the row markup inside `#section-webhooks-overview` (`_automation_triggers.html` L4-85) and its grid rule in `automation_triggers.css`.

## P1
1. **Text-link actions** ("Use the *arr webhook", "Use the Custom webhook", "Servers -> Edit Plex -> Webhook & Scanner", "View webhook activity", "Settings section", "Publish to which server?").
   Change: make the primary navigations small outline buttons with an arrow-right icon (`btn btn-sm btn-outline-secondary`), still scrolling to the same anchors. "View webhook activity" is already a button; keep it. Inline "Settings section" links in help copy can stay links.
2. **Overview is five equal rows with long prose** (desktop).
   Change: shorten each row to: icon tile (vendor logo for Plex/Emby/Jellyfin, `bi-download` for *arr, `bi-plug` for custom), bold situation, one muted line, right-aligned button. Move the Emby and Jellyfin setup sentences (URL with token, "tick Library -> New Media Added") into a `<details>` or into the Servers edit modal copy. Keep the `/incoming` code but only in the expanded detail.
3. **Warning block "Note about file upgrades"** is a yellow left-stripe callout (side-stripe banned by brief).
   Change: neutral inset with a warn icon tile, no left border; title one line, body max two lines, rest in `<details>`.
4. **Setup guides are always open** (Sonarr numbered steps, Custom payload blocks).
   Change: collapse "Setup in Sonarr/Radarr/Sportarr" into a `<details>` closed by default under the URL field; the URL field with copy icon stays the hero. Custom: already uses `<details>` for Tdarr/FileFlows/curl; also collapse the two payload examples.
5. ***arr tabs are tiny coloured text chips** (Sonarr green, Radarr orange, Sportarr cyan).
   Change: use the segmented tab style from the dashboard queue (rounded group, active = raised surface). Do not use new hues; use neutral with a small icon. Colours are reserved for task types. Selector: `#tab-sonarr-tab`, `#tab-radarr-tab`, `#tab-sportarr-tab` (keep ids, `data-bs-target`).
6. **Section headers are tiny faint caps** ("OVERVIEW", "*ARR APPS") with 11px icons; they read as footnotes.
   Change: 15px 600 title case, same style as dashboard card titles. Keep `role="heading" aria-level="2"`.
7. **Settings card** (switch, 60s slider, secret field) is mixed with help text.
   Change: label left / control right rows, slider shows value at right, helper text 12px faint. Master switch gets the status pill idiom ("On" `--ok` / "Paused" `--warn`). The secret field: keep eye + Generate; make Generate an outline button with icon.

## P2
1. Sidebar: groups "TRIGGERS"/"SCHEDULES" are tiny caps; active group is amber. Use amber only for the active link; group labels neutral faint. Sidebar link text is 12px: raise to 13px.
2. Header: add a status chip next to the title: "Webhooks on" / "Paused" so state is visible without scrolling to Settings.
3. Code blocks: `<code>` in amber on dark everywhere. Use neutral inset mono and amber only for the copy-success flash.
4. Webhook URL hosts show `127.0.0.1:18080` - if possible show the host the browser used (JS: `location.origin`), otherwise add a muted hint "replace with your server's address".
5. Schedules tab (`_automation_schedules.html`, not screenshotted): ensure the table uses icon-only row actions with `aria-label`+`title`, status pill with icon, and "next run" in amber per the brief. Check that the schedule modal's two-column radios aren't a long undifferentiated form (group into Scope / When / Priority fieldsets).

## Mobile 390px
- Overview rows stacked (P0).
- Page is 5000px tall; with details collapsed it should drop by ~40%.
- Mobile nav (`#automationMobileNav`) is present; keep, and make the tabs sticky under the navbar.
- URL boxes: the `http://127.0.0.1:18080/api/...` truncates; fine, but the copy button must stay visible (it does).
- Payload `<pre>` blocks clip at the right edge: add `overflow-x:auto`.

## Do NOT change
- Ids: `#automation-sidebar`, `#sidebar-group-triggers/-schedules`, `#sidebar-triggers-links`, `#sidebar-schedules-links`, `#automationMobileNav`, `#automationMobileFeedback`, `#automationHeading*`, `#automationSubtitle`, `#pane-triggers`, `#pane-schedules`, all `#section-webhooks-*` and `#section-schedules-*` anchors, `#scheduleList`, `#newScheduleModal` and every `schedule*` form id, `#saveStatusIndicator`, tab ids above.
- Scrollspy and tab-switch script in `automation.html` (it keys on those ids and `href="#section-..."`).
- Webhook URLs, secret handling, autosave hooks, the slider/switch input ids and `onchange` handlers in the schedule modal.
- Copy text in help is documentation: shorten, but do not drop the auth instructions.

## Test impact
- `tests/e2e/test_webhooks_automation.py`, `test_schedules.py`, `test_navbar_menus.py`, `test_ux_polish.py`, `test_worker_groups.py` reference `#sidebar-*`, `#section-webhooks-*`, `#tab-sonarr-tab`, `#scheduleList`. Keep ids and they pass.
- If you collapse content in `<details>` that tests read (Sonarr step text, payload JSON), `to_contain_text` still works on hidden text only for `to_have_text`/`text_content`; visibility assertions (`to_be_visible`) will fail. Grep `Setup in Sonarr`, `file_path`, `Emby Dashboard` in `tests/e2e/test_webhooks_automation.py` before collapsing, and have tests open the `<details>` if needed.
- `tests/test_routes.py` asserts section ids render.

## Implementation parity

Every control of the pre-redesign page, and where it lives now. All ids, routes, payloads and handlers are unchanged.

Shell (`automation.html`)
- Sidebar groups (`#sidebar-group-triggers/-schedules`, tab switch), link lists, scrollspy, hash/`?tab=`/`editSchedule` handling: kept. Active link amber, group labels neutral, no side stripe.
- Mobile nav (`#automationMobileNav`, Triggers / Schedules / Global pause), `#automationMobileFeedback`, heading/subtitle swap, `#saveStatusIndicator`: kept. Added header chips `#automationWebhookPill` (mirrors master switch) and `#automationSchedulePill` (enabled count).

Triggers
- Overview: intro, five scenario rows with `#trigger-arr/-custom/-plex/-emby/-jellyfin`, buttons to `#section-webhooks-sonarr-radarr`, `#section-webhooks-custom`, `/servers` (Plex, Emby, Jellyfin), delay note, upgrade caveat (full text now in "Why this happens" details). Removed: the `.trigger-jump` button strip (duplicated the row buttons; Emby/Jellyfin rows previously had no action and now link to `/servers`, whose edit modal has their webhook card).
- *arr: server picker `#webhookServerScope` (still fills from `/api/servers`, appends `?server_id=`), delay note (now in details), tabs `#tab-sonarr/radarr/sportarr-tab` (Bootstrap tabs, segmented style), URL fields `#sonarrUrl/#radarrUrl/#sportarrUrl` with copy buttons (`copyUrl`, adds a copied flash), setup steps per app (collapsed, same text and Radarr "On Import/On Upgrade" events), auth info bar and link to Settings.
- Custom: `#customUrl` + copy, server-pin help, single/multiple payload (details, single open), Test event hint, Tdarr, FileFlows, curl details.
- Settings: `#webhookEnabled` (+ new On/Paused pill), `#webhookDelay` + `#delayValue`, delay explain modal template, `#webhookSecret` with eye (`toggleSecretVisibility`) and Generate (`generateSecret`), Processing link, autosave via `bindAutoSave`.
- Activity card + "View webhook activity" button.

Schedules
- Overview, global pause note (`#section-schedules-quiet-hours`, link to `/settings#section-worker-quiet-hours`), `Add Schedule`, `#scheduleList` rows with: name, kind badge (`.schedule-kind-badge`), quiet-hours overlap icon, library + server badge, schedule code + stop badge, priority badge (`.priority-badge`), next run (amber), status pill, Run now / Edit / Enable-Disable / Delete (icon buttons with aria-label + title), load-failure banner and recovery. Added: next-run hero with Run now, enabled-count pill.
- Modal `#newScheduleModal`: every field and id unchanged; added three group labels (What to run / When / Priority & order).

Shared tokens: none added. `.pill`, `.ibtn`, `.seg` are scoped to `.dashboard-layout` in the shared CSS, so `pages/automation.css` restates them under `.automation-page`.

Tests: no test edits needed (ids, `.decision-row`, `.schedule-kind-badge`, `.priority-badge`, `aria-label="Edit schedule"`, "Add Schedule" all kept).
