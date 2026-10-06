# Dashboard redesign: technical audit

Scope: 35 changed templates/CSS/JS under `media_preview_generator/web`, plus `impl/*.png` screenshots. Source was read only where needed. Contrast numbers are computed from the tokens in `static/css/style.css`. Touch and overflow findings come from the 390px screenshots and the CSS, not from a synthesized-touch run. Gesture handling (drag surfaces) was not exercised.

Health score (Impeccable rubric): A11y 2, Performance 3, Responsive 2, Theming 2, Integrity 3 = 12/20 (Acceptable). Light theme and mobile are the weak spots. Dark desktop is clean.

## Detector triage (4 findings)

| Finding | Verdict |
|---|---|
| `static/css/queue.css:114` layout-transition (`.mini .progress-bar { transition: width .6s }`) | Hidden finding. Real but low impact (P2). It animates `width` on a 6px bar and is already disabled under reduced motion (queue.css:215). Fix by `transform: scaleX()` if it ever jank-profiles. |
| `static/css/style.css:1200` layout-transition (`.progress-bar { transition: width .3s }`) | Same pattern, global Bootstrap bar. Not covered by a reduced-motion rule. See P2-6. |
| `static/css/worker_groups.css:157` layout-transition (`.workers-panel-card .progress-bar { transition: width .6s }`) | Same pattern. Reduced-motion override exists at worker_groups.css:180. The indeterminate bar (`.worker-indeterminate`) is correctly set to `animation:none` there. That is intentional: state stays visible as a static bar. |
| `templates/servers.html:51` broken-image `<img id="editServerVendorLogo" src="">` | False positive. The `src` is filled by JS and the element starts as `d-none`, with `alt=""` and explicit width and height. Optional hardening: drop the empty `src` attribute so the browser does not request the page URL. |

Also, the detector could not resolve `<link href="{{ url_for(...) }}">` in `_automation_schedules.html:1`. That is a tool limit, not a bug. A `<link>` in the body does work, but it is render-blocking inside the page and not in `<head>`. See P2-8.

Not flagged, and intentional: short uppercase tracking labels (`.sec-label`, `.dash-stat-label`, `.dash-sched-label`) are 10-11px, 2-3 words. The breadcrumb above the Inspector `<h1>` is intentional. The Inspector `.insp-*` `outline:none` rules that sit with a box-shadow or border ring are fine (see P1-3 for the ones that are not).

## P0 (blocks task completion)

None found.

## P1 (WCAG AA failure or significant difficulty, fix before release)

**P1-1. Light theme: amber `--accent` fails contrast as text and as a button fill.** Page: Dashboard, Inspector, Settings.
- File: `static/css/style.css:159` (`--accent:#b87a00`, `--accent-ink:#fff`).
- Measured: `#b87a00` on white is 3.61:1. On the page bg `#e4e7eb` it is 2.91:1. White on `#b87a00` is 3.61:1.
- Affected selectors: `.dashboard-layout .dash-btn-primary` (15px/600 "Start new job"), `.jobref` (worker_groups.css:154, 12px mono on `--accent-soft`, about 3.2:1), `.priority-2` (queue.css:101, 12px "Normal"), `.seg button.on .c` (queue.css:27), and `.dash-libs-ico` and `.dash-sched-label` (dashboard.css:87, 120).
- Fix: add `--accent-text: #7a4f00` (already exists as `--plex-orange-text`, 7.2:1) in the light block and use it for every `color: var(--accent)` text use. For fills, keep `#b87a00` but set `--accent-ink: #1a1203` (about 5.3:1 on `#b87a00`) instead of white.

**P1-2. Light theme: `--faint` and `--ok` fail on the page background.** Page: Dashboard, Queue, Workers.
- File: `static/css/style.css:158` and `:162`.
- Measured: `#6b7186` on `#e4e7eb` is 3.91:1 and on `--inset` about 4.06:1. `#138a58` on `#e4e7eb` is 3.52:1 and on white 4.37:1. The CSS comment says "passes 4.5:1, do not go lighter", which is only true on white. There are 32 `color:var(--faint)` uses, including `.queue-items`, `.wk-library` and "Queued".
- Fix: `--faint: #5f6579` (about 5.0:1 on `#e4e7eb`) and `--ok: #0f7a4d` (about 4.8:1 on `#e4e7eb`). Check `--warn` (4.0:1 on page bg) with `#8a5a00`.

**P1-3. Missing visible focus on several Inspector controls.** Page: Inspector (a keyboard-only user can lose position).
- File: `static/css/pages/inspector.css`.
  - `button.insp-season-row:focus-visible` (about line 2271): `outline:none` with only a background change shared with hover, which is about 1.1:1.
  - `.insp-episode:focus-visible` (about line 655): `outline:none`, with a border color shared with hover.
  - `.insp-adjust-input input:focus` (about line 1876): only a 1px border change, no ring.
  - `.insp-row.is-open` (about line 587): `outline:none`, only OK if the row is not focusable.
- Fix: replace each `outline:none` with `outline: 2px solid var(--insp-amber); outline-offset: 2px`, or use `box-shadow: var(--focus-ring)`. The pattern at `.insp-band:focus-visible` is the right model.

**P1-4. `.info-icon` focus ring is nearly invisible.** Page: all pages with an (i) icon.
- File: `static/css/style.css:2212-2218`. The ring is `rgba(13,110,253,.25)` blue, and `outline:none` is set. Against the Plex dark and slate bg this is under 1.5:1, and it is a different color than the branded `--focus-ring` used elsewhere (style.css:39).
- Fix: replace with `box-shadow: var(--focus-ring)`. Also delete the stray `outline:none` in the shared `:hover, :focus-visible` rule.

**P1-5. Dashboard queue table on phone hides Status, Progress and Actions with no scroll cue.** Page: Dashboard (390px).
- File: `static/css/queue.css:198-205`. The table keeps its column layout and scrolls inside the card. `impl/dashboard-mobile-390.png` shows only ID and Job. Pause, cancel and retry are off-screen, and nothing hints that the card scrolls.
- Fix: below 576px, keep the card-row layout from style.css (do not undo it), or add an edge fade plus a sticky Actions column. Minimum: make the job-name cell show a compact status dot and progress percent on phones.

**P1-6. Icon-only buttons are below 44px on touch, and some have no accessible name.** Pages: Dashboard, Servers, Settings.
- File: `static/css/dashboard.css:24` (`.ibtn` 32px), `queue.css:126` (`.rowact .ibtn` 32px), `dashboard.css:78-80` (stepper 28px, `.cap` 24px), `queue.css:189` (pagination 32px), `queue.css:22` (`.seg button` 30px), `queue.css:96` (`.priority-btn` 28px), `worker_groups.css:90` (`.cap` 28px). Only the `.exp` chevron (queue.css:60) and `#newJobModal` use 44px.
- Fix: add one rule, `@media (pointer: coarse) { .dashboard-layout .ibtn, .seg button, .priority-btn, .stepper button, .pagination .page-link { min-width: 44px; min-height: 44px } }`. For the visual size to stay compact, use a `::after { inset: -6px }` hit-area expander as `.exp` does.
- Missing accessible names:
  - `static/js/servers.js:1482` and `:1612` (the `.pm-remove` and `.ep-remove` buttons contain only `bi-x-lg`): add `aria-label="Remove mapping"` and `aria-label="Remove endpoint"`.
  - `static/js/app.js:1356` and `:1358` (the GPU scale buttons have `title` only): add `aria-label` to match.
  - `static/js/markers_server_tab.js:78`: the `info-icon` button has `title` but no `aria-label`. The static templates use the same pattern, so this is systemic: `title` is not reliably announced on a focusable button.

**P1-7. Dashboard has no `<h1>`, and headings skip levels.** Page: Dashboard.
- File: `templates/index.html`. The headings are `<h6 class="sec-label">` (lines 50, 60) and an `<h5>` empty state (line 29), with no page heading. Servers, Logs, Webhook Activity, Automation, Login and Inspector all have an `<h1>`.
- Fix: add `<h1 class="visually-hidden">Dashboard</h1>` at the top of the main container, and change each card header to `<h2 class="card-title">` (styling is class-based, so the look does not change).

## P2 (annoyance, workaround exists)

- **P2-1. Servers (390px): the connection error is truncated** ("Could not connect to Plex at http://mlab-..."), so the host (which is what a user needs) is cut off. File: `static/css/pages/servers.css`, the error-pill selector. Fix: `white-space: normal; overflow-wrap: anywhere`, or put the full text in `title` and `aria-describedby`. The server row action buttons (edit, refresh, delete) are about 32px. Use the same coarse-pointer 44px fix as P1-6.
- **P2-2. Dashboard queue filter tabs overflow at 390px.** The `.seg` control scrolls horizontally (`queue.css:21`), and "Cancelled" is clipped with no cue. Fix: add `mask-image` edge fade, or wrap with `flex-wrap`.
- **P2-3. Global reduced-motion rule is a blanket kill.** `dashboard.css:145` sets `transition-duration: .01ms !important` on everything in `.dashboard-layout`. It is acceptable (state changes still apply instantly), but the 32 hover and filter transitions lose all feedback. Scope it to transforms and width only.
- **P2-4. Reduced-motion gaps.** `style.css:1200` (`.progress-bar` width transition) and the `.priority-*` and chevron transitions have no reduced-motion override outside `.dashboard-layout`. Pages without that class (Servers, Settings, Setup) rely only on settings.css:552 for the save indicator and skeleton.
- **P2-5. Info-icon (i) tooltip coverage is uneven.** Dashboard (`index.html`) has 2 icons against 45 in Settings and 39 in Servers. Not-obvious controls on Dashboard with no (i): Pause, the priority menu, "Clear jobs" and the worker occupancy chip (`2 / 2`). The mockups showed (i) on Workers. Add them per the project preference.
- **P2-6. Hard-coded colors in the redesigned files.** `queue.css:24` and `:25` use `rgba(0,0,0,.25)` shadows, and `style.css:2217` uses blue rgba. Move to tokens (`--shadow-1`) for theme consistency.
- **P2-7. `!important` chains on queue progress** (`queue.css:111-114`, five rules). It works, but it blocks theme overrides. Use `[data-status]` attribute selectors with higher specificity instead.
- **P2-8. `_automation_schedules.html:1` puts `<link rel=stylesheet>` in the body.** It blocks rendering and flashes unstyled content. Move it to the `{% block head %}` of the including template.
- **P2-9. Inspector light: the amber/violet legend dots and "ours" swatches are 2 hue-only encodings.** They are paired with text labels, so this is not a failure, but the `Jump to` pills use `#7a4f00` and violet at a small size. Verify at 4.5:1.

## Patterns and systemic issues

- **The light theme was tuned against white cards, not the `#e4e7eb` page bg.** Every muted or state token fails at 2.9-4.1:1 where text sits directly on the page (P1-1, P1-2). The dark theme passes everywhere I measured (faint 4.6:1, muted 6.2:1).
- **44px touch targets exist only on the Setup, Login and job modals.** The dashboard components (the biggest surface) were built at 24-32px for desktop density.
- **`outline:none` with a weak or shared replacement** appears 9 times in `inspector.css` and `style.css`.
- **Icon-only controls generated in JS** skip `aria-label` consistently.

## Positive findings

- Dark theme contrast and tokens are solid (`--muted` 6.19, `--faint` 4.6, `--run` 6.26 on panel).
- Branded `--focus-ring` on links, buttons and the `.insp-band` pattern is good, and the Dashboard queue controls have visible-hidden labels (`index.html:237-258`).
- No horizontal page overflow at 390px on any screenshot (Dashboard, Servers, Settings, Automation, Inspector, Setup, Login). Wide content scrolls inside its own card.
- Reduced-motion handling is intentional: the indeterminate worker bar becomes a static bar, and the live dot stops pulsing, so state is preserved.
- One `<h1>` per page on six of the seven main pages, `<main>` skip target (`#main-content`), and a `viewport` meta without zoom lock.
- Only 3 detector hits in 35 files, and none is a real defect.

## Safe to ship?

| Page | Verdict |
|---|---|
| Dashboard | No. Fix P1-1, P1-2, P1-5, P1-6, P1-7 first. Phone users cannot reach job actions. |
| Servers | Yes after P1-6 (the two unlabeled remove buttons) and the P2-1 truncation. |
| Automation | Yes, with the P1-1 and P1-2 token fix (it shares the tokens). |
| Settings | Yes after the P1-1 and P1-2 token fix. Light theme text is borderline. |
| Inspector | No. Fix P1-3 (keyboard focus) and the P1-1 and P1-2 token fix. |
| Logs, Webhook Activity | Yes. Only the shared token fix applies. |
| Login, Setup | Yes. They already have 44px targets. The light-theme token fix applies to the amber CTA. |

## Recommended order

1. P1 `/impeccable colorize`: the light-theme tokens (P1-1, P1-2). One edit in `style.css` fixes most pages.
2. P1 `/impeccable adapt`: the phone queue layout and the 44px coarse-pointer targets (P1-5, P1-6, P2-1, P2-2).
3. P1 `/impeccable harden`: focus rings and accessible names (P1-3, P1-4, P1-6 labels, P1-7 heading).
4. P2 `/impeccable clarify`: (i) tooltips on the dashboard (P2-5).
5. P2 `/impeccable optimize`: the width transitions and reduced-motion scope (P2-3, P2-4, P2-7, P2-8).
6. `/impeccable polish` last.

## Resolution

Measured on the live app (light 1440, dark and touch 390): on the `#e4e7eb` page, `--accent-text` 5.75, `--faint` 4.67, `--ok` 4.94, `--warn` 4.78; dark `--accent-ink` on amber 5.14. No horizontal overflow at 390 on Dashboard, Settings, Inspector, Servers. Screenshots: `impl/audit-fix-*.png`.

| Finding | Status | Note |
|---|---|---|
| P1-1 accent contrast | fixed | New `--accent-text` (#7a4f00 light, unchanged amber dark) used for every `color: var(--accent)`; light `--accent-ink` is #1a1203. |
| P1-2 faint/ok/warn | fixed | `--faint` #5f6579, `--ok` #0d7048 (the audit's #0f7a4d measured 4.33, so darker), `--warn` #8a5a00. |
| P1-3 Inspector focus | fixed | Season row, episode, row, adjust input get a 2px ring. `.insp-row.is-open` is not focusable, left as is. |
| P1-4 info-icon ring | fixed | Uses `--focus-ring`; stray `outline:none` removed from the shared rule. Light theme overrides `--focus-ring` to a dark amber. |
| P1-5 phone queue | fixed | At 700px and below each job is a stacked card with every field (status, priority, progress, id, created, actions). |
| P1-6 touch and names | fixed | 44px `pointer: coarse` targets for icon buttons, seg, priority, stepper, pagination, `.dash-btn`. Inline `.info-icon` and the 28px-wide `.exp` chevron stay under 44px. All icon-only buttons now have aria-label (JS-generated ones explicitly, static ones via `_labelIconOnlyButtons`, info icons from their tooltip). |
| P1-7 headings | fixed | Hidden h1; card titles `role=heading aria-level=2`; section labels h3; empty state h2. Look unchanged. |
| P2-1 servers error | fixed | Error pill wraps; 44px touch via the shared rule. |
| P2-2 seg overflow | fixed | Wraps at 700px and below. |
| P2-3 reduced motion scope | fixed | Only transform/width motion is stopped; colour feedback stays. |
| P2-4 progress-bar reduced motion | fixed | Global `.progress-bar` rule added. |
| P2-5 dashboard (i) | fixed | Added to Pause, Priority column, Clear jobs. Occupancy chip already had a tooltip. |
| P2-6 hard-coded colours | fixed | Shadow uses `--shadow-1`; blue ring replaced. |
| P2-7 `!important` | fixed | JS no longer emits `bg-*` on queue bars; `[data-status]` rules need no `!important`. |
| P2-8 body `<link>` | fixed | Moved into `automation.html` head. |
| P2-9 Inspector legend | not-a-bug | Dots are paired with text; not changed. |
| Width transitions | partly fixed | Queue mini bar uses `scaleX`. Worker panel and active-job bars still animate width because JS and e2e tests read `style.width`. |
| Empty `<img src="">` | not changed | False positive, as stated. |

### Final pass (2026-10-06)

| Item | Status | Note |
|---|---|---|
| Inspector row 18 "Try a recent job" | dropped by owner decision | The `#inspChipJob` chip, its `/api/jobs` fetch, CSS and its two e2e tests were removed; "Recent files" covers it. The header Logs button was removed as well (Logs is in the Tools menu). |
| Touch targets | fixed | `pointer: coarse` only. Transparent 44px `::before` hit areas for `.info-icon` and checkbox/switch inputs; `.exp` widened to 44px with a -8px margin; 44px min for `.btn`, `.nav-link`, log buttons and search, `summary`, navbar links, form controls, day chips, preset buttons, marker up/down, range sliders. Re-measured at 390 on the seven pages. Left under 44px by design: inline text links in prose (WCAG exempts), visually-hidden status select, `btn-check` inputs (their label is the target). Switch hit area proven on /servers; Chromium-only trick (Firefox ignores `::before` on inputs, so there the switch keeps its own size). |
| Light contrast | fixed | `--ok` #0a6340, `--bad` #b01f2c, `--run` #1c55bd, `--warn` #805300; pills on tinted backgrounds now >= 4.5 (was ok 4.31, bad 4.42, run 3.88 worst case). Dark untouched. |
| Dark pills still under 4.5 | fixed in the polish pass below | white on `--bad` count badge 3.16; muted `bg-secondary` / `text-bg-dark` badges 3.7-4.2; `--bad` on tinted pill 3.92. Dark was to stay untouched. |
| Progress bars to `scaleX` | not changed | `test_ui_workers_panel.py` and `app.js` (3230, 4169) read and write `style.width`; switching would mean rewriting those assertions. |

Screenshots: `impl/final-{inspector-empty,dashboard,settings}-{390,1440}.png`. Full e2e: 880 passed.

### Polish pass (2026-10-06)

| Item | Status | Note |
|---|---|---|
| Dark contrast | fixed | Scripted every text node on the seven pages at 1440 and 390 (computed colour over the composited ancestor background). Dark `--muted` #aeaec4, `--faint` #a4a4bb, `--bad` #f77d88, `--run` #74adff; bell count badge uses dark ink. Worst case went from 1.95 (muted on tinted pill 3.74, white on `--bad` 3.16) to none under 4.5. Left: disabled controls (Debug level button, disabled pagination chevron), which WCAG exempts. Light tokens are in a separate block and were not touched; the light sweep still lists the amber gradient brand text and a few Bootstrap link/muted spots (4.1-4.4) that predate this pass. |
| Switch hit areas at 390 | verified, one fix | Real `elementFromPoint` at the 44px box edges with `has_touch` + `is_mobile`, Chromium and Firefox: all 21 Settings, 25 Servers and 18 Automation checkboxes/switches resolve to their own input, so the `::before` area works in Firefox too. The quiet-hours day chips (38x30 label, hidden `btn-check` input) did not: now 44x44 under `pointer: coarse` in `settings.css`. |
| System card on phones | fixed | Details start open (toggle kept; still opens itself on a connection issue). Screenshot checked at 390. |
| Phone offcanvas menu | checked, no change | Dashboard, Servers, Automation, Settings, Tools, bell, help, coffee, Star, theme, Logout all present and at least 43px tall. |
| Docs | done | `guides.md`: worker steppers, status tabs, Settings side menu/phone picker, inline editor and week graph, Inspector (chip since dropped). Pause/Resume in Workers was already described. `llms-full.txt` regenerated. |
