# Redesign handoff (start here)

View everything: serve this folder (`python -m http.server 8099` here) and open `index.html`.
Mockups are static HTML: they fix layout, spacing, colour, icons and copy, not behaviour. Keep live data,
IDs, routes, API payloads and settings.json untouched; update tests when markup they select changes.

Shared design language: `shared.css` (tokens + components, copied from the approved dashboard mockup) and
`DESIGN-BRIEF.md` (rules). Tokens must be added to `static/css/style.css` so every page gets them.

## Order of work (one PR per step; run `ruff`, the unit suite, and the relevant e2e after each)
1. **Tokens + shell** — `app-review/shell.md`; navbar keeps every item (bell, help, coffee, Star, theme, Logout).
2. **Dashboard** — `mockup.html` + `DESIGN-BRIEF.md` (markup lives in `index.html`, `app.js`, `worker_groups.js`).
3. **Servers** — `pages/servers.html` + `app-review/servers.md`
4. **Automation** — `pages/automation.html` + `app-review/automation.md`
5. **Settings** — `pages/settings.html` + `app-review/settings.md` (worker-group editor reuses dashboard group rows)
6. **Tools** — `pages/logs.html`, `pages/webhook-activity.html`, `pages/inspector.html` + `app-review/tools.md`
   (`/bif-viewer` only redirects to the inspector; no page).
7. **Overlays** — `pages/overlays.html` + `app-review/overlays.md` (do after the page it opens from: Start job / Process file /
   Job details with the Dashboard, Schedule modal with Automation, navbar menus + offcanvas with the shell, confirm / toast / (i) with step 1)
8. **Setup + Login** — `pages/setup.html`, `pages/login.html` + `app-review/{setup,login}.md`

## Each review file lists
Findings P0/P1/P2 with files/selectors, mobile issues, a "Do NOT change" list, and the tests likely affected.
Treat the mockup as the target and the review file as the implementation checklist.

## Known gaps (mockup invented content; use the real app's fields)
Setup steps 2 and 4; Servers Add/Edit modals; the Inspector's non-File states are shown via a spec-only toggle;
Settings and Logs screenshots in `app-review/shots/` were from an older build, so trust the templates.

## Overlays
All modals, menus and dialogs are in `pages/overlays.html` (spec-only switcher at the top opens each one; each creation dialog has a
"Spec: validation" chip to show inline errors). `app-review/overlays.md` holds the per-control inventory (kept / restyled / moved),
the critique and an "Overlays parity" checklist. Rules: job type = radio cards in the type colour, hint = one line <= 90 chars with the
rest inside the (i), sticky footer with a primary that names the job, full-screen sheets below 640px. Server Add/Edit modals live with
the Servers page, not here.
