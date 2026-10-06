# Dashboard redesign — brief for implementation

**Start here:** open `mockup.html` in a browser (needs internet for Bootstrap Icons CDN; the app already ships
Bootstrap Icons 1.11.1). Screenshots beside it: `screenshot-dark-1920.png`, `screenshot-light-1440.png`,
`screenshot-mobile-390.png`, and `screenshot-before-live.png` (current page). The mockup is the visual
contract; this file is the rules and the mapping to existing code.

Scope: `templates/index.html`, `static/css/dashboard.css`, `worker_groups.css`, `queue.css`,
`static/js/worker_groups.js` (+ queue JS). Keep every existing element ID, data attribute, SocketIO hook
and behaviour — this is a visual/structure pass, not a logic change. Keep Bootstrap 5.3 + Bootstrap Icons.
Mode: Operate (scan fast, trust familiar patterns). Direction: refine the existing dark Plex-amber identity.

## Top navbar — KEEP EVERYTHING (the mockup bar is now complete)
Do not remove or reorder anything in `templates/base.html`'s navbar: brand + "local build" subtitle, Dashboard,
Servers, Automation ▾, Settings ▾, Tools ▾, notification bell with unread badge, Help, Buy-me-a-coffee, GitHub
Star, theme toggle, Logout (and the mobile offcanvas menu). Only visual polish: consistent 32px icon buttons,
amber tinted active tab, small badge, version/update indicator unchanged. The mockup draws this bar for
completeness; if base.html already matches, change nothing there.

## What Codex changes vs. leaves alone
CHANGE (dashboard page only): `templates/index.html`, `static/css/dashboard.css`, `worker_groups.css`, `queue.css`,
the render functions in `static/js/worker_groups.js` and the queue/job-stats/system-card render code in
`static/js/app.js` (these build the HTML as strings, so the *markup in the JS* must change, not just index.html),
plus new tokens in `static/css/style.css`.
LEAVE ALONE: all routes, API payloads, SocketIO events, settings.json schema, element IDs/data attributes that JS or
tests select (rename = update both sides + tests), modals (`_start_job_modal`, `_job_details_modal`, …), other pages.
The mockup is static HTML with hardcoded data: it fixes layout, spacing, colour, icons and copy; it does not define
behaviour. Where the mockup and live data disagree (e.g. job-expander contents), keep the live data and apply the
mockup's styling.
The page-by-page review of the rest of the app is in `app-review/` (one file per page + `shell.md`).

## Principles
1. State is shown by colour + icon, not by words in badges ("Connected" → green dot with tooltip).
2. One hue per task type, used everywhere (worker capability icons, job chips, queue row icons):
   Previews `--t-previews` blue, Intro & credits `--t-intro` violet, Plex loudness `--t-loud` teal.
3. Amber (`--accent`) is only for primary action, "next schedule", and job-ID links. Not decoration.
4. Text links ("Manage →", "Manage groups") become buttons / icon buttons.
5. No colored side-stripes on cards, no gradient text, no nested cards.
6. Numbers use `font-variant-numeric: tabular-nums`.

## Tokens (add to the existing `:root` / `[data-bs-theme]` blocks; values for both themes are in `mockup.html`)
`--t-previews/-soft`, `--t-intro/-soft`, `--t-loud/-soft`, `--ok/-soft`, `--bad/-soft`, `--run/-soft`, `--warn/-soft`,
`--inset` (existing `--surface-1`), `--line` (existing `--surface-border`). Radii 12 / 8 / 6. Faint text is
`#8686a0` dark / `#6b7186` light (passes 4.5:1; do not go lighter).

## Row 1 — three cards (System · Quick actions · Job statistics)
- Grid 3 equal columns, `align-items: stretch`, each card **`min-height: 340px`**, flex column; footer content uses
  `margin-top: auto` so the cards never look half empty. Collapse to 1 column < 1100px, min-height off.
- **System**: title "System" (not "System & Workers"), "Processing/Idle" status pill with pulsing dot on the right.
  Servers = compact rows (logo tile, name, mono host, **status dot only**; title/aria-label = "Connected" / error text).
  Worker groups = one row per group (icon, name, hardware subtitle, capability icons) with a −/+ stepper that changes **that group's saved worker count** via the existing `scale_saved_group` path (min 1; disabling a group is the way to reach zero). No "Manage groups" button here (it lives only in the Workers panel header). No per-GPU steppers. Footer: version (mono) +
  "Up to date" / "Update 4.4.2" pill. The separate "Status: Idle" block is removed (it's the header pill).
- **Quick actions**: primary "Start new job", secondary "Process a file or folder". No libraries tile (Servers covers it).
  Below: a 2x2 grid of tiles filling the card: "Run <next schedule> now" (one click POSTs `/api/schedules/<id>/run`;
  disabled "No schedules" state when none), Inspector, Webhook activity (pending-count pill, hidden at 0), Logs. Tiles keep >= 44px height.
- **Job statistics**: 3×2 tiles, each with a small icon + label + value (value coloured by state). Under them a
  6px segmented outcome bar (completed / failed / cancelled / active). Then the **schedule block**: amber-tinted inset
  with icon tile, "NEXT SCHEDULE", schedule name (bold), `in about 16 hours` bold + `Wed 01:00` muted, a
  settings icon-button (replaces the "Manage" text link). Below: "✓ 3 schedules, all enabled"
  (amber warning variant if any disabled; "No schedules — Create one" empty state keeps its link as a button).

## Workers panel
- Header: title + (i) tooltip · occupancy meter (one 14×6 pill per worker, filled = busy) + "5 / 5 busy" ·
  **Pause** button (moved here from the Active Jobs card; it controls workers) · **Manage groups** button with icon (the only Manage groups entry on the page; the System card has none).
- **No group editor on the dashboard**: groups are edited only in Settings; **Manage groups** (-> `/settings#section-workers`) is the single entry and appears once. The dashboard keeps the +/- steppers, capability icons and occupancy chip. Each worker card with a running job has a small **Logs** button next to the job chip (aria-label `View logs for job <id>`, tooltip "Job logs") that opens that job's log tab.
- Right of the header: **capability icons** (28px tiles): film = Previews, skip-forward = Intro & credits,
  soundwave = Plex loudness. Enabled = type colour on soft tint; disabled = dim grey. Each has a tooltip with its
  name. Replaces the "Previews · Intro & credits" text row. Then an occupancy chip `2 / 2` (no edit pencil).
  The old cpu-count / play-count icons are dropped (the chip carries it). Clock (schedule) icon shows only when the
  group has active hours; tooltip carries the schedule text.
- Worker card (one number only): top line = icon + "GPU worker" / "CPU worker" on the left and **one number chip on
  the right** (the worker's `#N` — the "Worker 1" label and the hardware name in parentheses are removed; hardware is
  in the group header). Title = media title, 2-line clamp, 15px semibold. Meta row = task chip (type colour + icon,
  replaces the grey "Previews" text) and the **job reference as an amber mono chip** with an arrow-out icon, right
  aligned, still linking to the job. Progress: 6px bar; below it speed (green) / ETA (bold) on the left, percent on
  the right. Step text ("Generating chapters 3/4") replaces speed/ETA when those aren't available. Indeterminate
  steps (audio analysis) use the sliding bar. Card border tints blue while busy.
- Idle worker: dashed border, muted "Waiting for work". Unavailable/outside-hours keeps its existing label as a
  small pill in the top line.
- The separate **Active jobs** card is removed from the layout while idle (it was an empty "Idle — no jobs in flight"
  box); the live worker cards already show active work. If any JS depends on `#activeJobs`, keep it rendered but
  `hidden` when empty rather than deleting.

## Job queue
- Status dropdown → **segmented tabs with counts** (All / Running / Pending / Completed / Failed). Search + server /
  library / type filters stay as compact outlined buttons (dropdowns). "Pending webhooks" chip moves to the card
  header beside Clear jobs. Pause is no longer in this header.
- Table: ID (mono, faint) · Job · Status · Priority · Progress · Created · Actions. Sticky header, 11px caps labels.
- Job cell: 30px **type icon tile** (type colour; failed jobs show a red warning tile), bold title, and a sub-line of
  small grey tags (library, server, trigger) + the type name. Failure reason (e.g. "2 files failed") in red in the
  sub-line. This replaces the heavy black "Intro & Credits" + grey "manual" pill pairs.
- Status = pill with icon (Running has the pulsing dot). Priority = icon + word: ↓ Low (grey), — Normal (amber),
  ↑ High (red) — replaces the filled grey/amber badges. Progress = thin inline bar + percent (green done, red failed,
  sliding when indeterminate).
- Row actions are **icon-only and always visible** (muted; delete turns red on hover). Today's three bordered buttons
  per row become three quiet icon buttons.
- **Priority** is a small dropdown button (icon + word + caret) on running/pending jobs, opening a menu
  High / Normal / Low with a check on the current one (existing `setJobPriority`). Editable for any job that has not finished: `pending` (not started yet) and `running` — the current code
  (`renderPriorityCell`, `set_job_priority`) already allows both, so keep that rule exactly. Completed / failed /
  cancelled jobs show it as plain read-only text (no caret, no hover). The editable control is **borderless**: icon + word + small
  caret; a subtle `--inset` background appears only on hover, keyboard focus or while the menu is open.
- **Row expander**: a chevron left of the type icon toggles a detail row under the job (existing `#job-detail-<id>` /
  `job-files-toggle`). Contents come from the live renderers (`_renderPublishersBlock`, `_markersFilesBody`, the activity
  and error sections in `app.js`); the mockup restyles them, it does not invent fields. The earlier per-file status /
  progress / worker / Retry rows are NOT in the live expander (per-file results live behind "View all files and results"),
  so they are gone. Two-column layout (stacks < 1100px): left = activity, error, results; right = files. Footer row: files
  note + "Open logs and files". See `screenshot-queue-expanded-priority-menu.png`.

### Expanded job row: field inventory
| Current app field (source) | Mockup location |
|---|---|
| Current/Last activity heading, current item or wait reason (`job-current-activity`) | left col, "Current activity" / "Last activity", mono path |
| Next eligible time (`resource_wait.next_eligible`) | under activity (pending row) |
| Started, Job elapsed (live), Finished | muted time line under activity |
| Job error (`job.error`) | red "Job error" block, top of left col |
| Results recorded so far + retry-chain note ("across all N runs") | "Results recorded so far" + muted note |
| Per-server block: logo + name | server row with logo + name |
| Frame-source chips Generated / Reused / Already Existed (extracted / cache_hit / output_existed), with tooltips | green / blue / grey chips x N |
| Scrubber/chapter chips, "Previews updated", status chips (pending registration, not indexed, failed ...) | same chip list; tone ok / warn / bad |
| Intro & Credits per-server statuses (written, up to date, waiting, skipped + reason, none, failed) | chips in server row |
| File issues (not found, source gone, no media parts, excluded, invalid hash, not indexed, no owners, failed) | "File issues" chips |
| CPU fallback line ("N files ran on the CPU because the GPU failed") | amber line under chips |
| "Decided by" marker sources per type, top 5 + "other", (i) info | "Decided by" block, one line per type |
| Files heading + total, first 5 rows | right col "Files N" |
| File basename, expands to full path (`details`), media title when different | row summary + disclosure + muted title |
| Inspector eye link (video paths only) | eye icon button at row end |
| "Requested paths N", 5 shown, "Showing 5 of N", "View all requested paths" (webhook/file jobs) | pending-row variant |
| "View all files and results" | button under file list |
| States: Loading..., read failed ("Could not read files ... try again"), empty ("No files listed yet" / "Files will be listed when the job runs") | `.state` boxes (cancelled / completed / pending rows) |
| "Open logs and files" (opens job details modal) | footer button |
| Open path disclosures and the open row survive re-render | behaviour, keep as today |

- **Clear jobs** menu: header "Clear finished jobs", checkbox + coloured status pill + count for Completed /
  Failed / Cancelled, then a red "Clear N jobs" button whose N follows the checked boxes. See
  `screenshot-queue-clear-menu.png`.
- Footer: per-page select, range text, pagination with amber active page. Wraps on mobile; table scrolls
  horizontally inside its card (`min-width: 900px`).

## States Codex must cover (mockup shows the active one)
- Idle (no jobs): worker cards in idle style, occupancy `0 / 5`, System pill "Idle", Pause hidden.
- Paused: Pause button becomes amber **Resume**, header gets a "Paused" warn pill.
- No servers: existing empty state, unchanged.
- Disabled schedules / no schedules; failed server connection (red dot + tooltip, row text turns red);
  update-available pill; queue empty state ("No jobs yet — Start new job").
- Light theme: tokens provided; verified in `screenshot-light-1440.png`.
- Mobile 390px: cards stack, group columns stack, table scrolls inside its card, header buttons wrap.

## Accessibility / polish checklist
- Every icon-only control has `aria-label` + `title`; status dots have `role="img"` + `aria-label`.
- Contrast ≥ 4.5:1 body, 3:1 large (use the faint values above).
- Focus ring: 2px `--accent`, offset 2px. Transitions 140–200ms, ease-out; progress width transition is OK.
- Respect `prefers-reduced-motion`: disable the pulse and sliding bar.
- Add the (i) tooltips pattern the project already uses next to any label that isn't obvious.

## Decisions to confirm (not blocking)
- Worker card shows the single **#N** on the right (matches "only the number on the right"). If the number
  should be a per-group index (1, 2) rather than the global worker ID, change the chip source only.
- Webhooks/Logs tiles in Quick actions are placeholders for "second-tier destinations".
- Mockup was drafted with Sonnet 5.5 + the impeccable skill, not Opus; the detector's only note is the
  progress-bar `transition: width`, left as is.

## Copy density, long names, queue columns (follow-up notes)
- Copy density: a visible helper under a setting label states purpose only, one line (<= ~90 chars); mechanics, exceptions, examples and "where else" move into the label's info icon (tooltip + `<template>` detail). Audit: `COPY-DENSITY.md`.
- Long hardware/device names, the version string and server host lines are one line with an ellipsis and the full text in `title`.
- Queue table: the ID column is sized to its content (88px) so the row expander sits next to it and the Job column gets the width.
- Progress cell shows the count once ("382 / 124,896"); a trailing "N/M" or "N/M completed" in the step message that repeats it is stripped for display only.
- Inspector: no "Try a recent job" chip and no Logs button in the page header (Logs is in the Tools menu).
