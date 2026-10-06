# Servers (/servers)

**Verdict**
- Clean and scannable, but it uses a different card language than the new dashboard (green text badge, big enabled switch, three bordered buttons per card).
- Status is spelled out ("Connected" x5) and the readiness glyph (yellow "!" + 10, red triangle + 14) is cryptic and sits next to the name.
- Fix: dashboard-style compact server rows/cards, status dot + icon, icon-only quiet actions, readiness as a labelled pill.

Shots: `shots/servers-desktop.png`, `shots/servers-mobile.png`. Markup is built in JS: `static/js/servers.js` ~L340-400 (card template). Page shell: `templates/servers.html` L10-30.

## P0
None. No functional or contrast failure seen.

## P1
1. **Text badge for status** (desktop: green "Connected" pill on every card).
   Change: replace `#${statusPillId}` pill with a 10px status dot (`--ok` / `--bad` / `--warn` / grey while checking) placed before the host line. Keep `role="img"`, `aria-label="Connected"` (or the error text), `title` same. Show the error text inline in red under the host only when not connected.
   Edit: `servers.js` ~L371 and the code that sets pill class/text at ~L210-220 (it sets `badge bg-secondary/bg-success/bg-warning`). Keep the element id; only change class and content.
2. **Readiness glyph is unclear** (yellow "!" + "10", red triangle + "14" beside the title).
   Change: move it to the right of the title row as a pill: icon + count + word, e.g. `[! 10 to fix]` warn tint, `[! 14 must fix]` bad tint; healthy = no pill (drop the green check, the Connected dot already says healthy). Tooltip "Open setup health" kept. It stays a button that opens the Health tab (`openEditModal(id,{openTab:'health'})`, ~L124, L313-320).
   Edit: `servers.js` ~L285-325 (glyph + `readiness-count-badge`), CSS in `processing_jobs.css` or `style.css` where `.readiness-count-badge` lives.
3. **Three bordered buttons per card, amber Edit competes with the page primary "Add Server".**
   Change: footer becomes quiet icon buttons right-aligned: Edit (pencil), Refresh libraries (arrow-clockwise), Delete (trash, red on hover only). Each `aria-label` + `title`. Remove the amber outline from Edit; amber stays only on Add Server. Keep the card itself clickable to Edit if it is not already (optional).
   Edit: `servers.js` ~L385-400; classes `edit-server-btn`, `refresh-libraries-btn`, `delete-server-btn` must stay.
4. **Libraries line is plain text** ("Libraries: 7 enabled / 7 total").
   Change: inset tile like the dashboard Libraries tile: big tabular-nums `7` + muted "of 7 libraries enabled". If enabled < total, show `--warn` colour on the number. No Manage link needed.
   Edit: `servers.js` card body.

## P2
1. Enabled switch has the text "Enabled" always. Keep the switch, drop the word when on; when off show a grey "Disabled" pill and dim the card (opacity .7). Keep its id/handler.
2. Vendor logo is inline with the title at three sizes of glyph. Put the logo in a 36px rounded tile (same as dashboard server rows) and use the title as 16px 600 weight (now looks like regular weight 20px).
3. Host URL is plain text; render in mono, muted, `text-truncate` with `title`.
4. Empty/loading: spinner only. Add an empty state ("No servers yet" + primary Add Server) if none exists.
5. Page head: subtitle differs between template ("prepares previews and media analysis for") and shot; fine, keep one sentence.
6. Grid is `row g-3` with 3 columns at 1600px, leaving the bottom half empty. Acceptable; optionally switch to `repeat(auto-fill,minmax(340px,1fr))`.

## Mobile 390px
- Card height is ~190px each, five cards need five screens of scroll. Make the footer one row: three 40x40 icon buttons (touch target >= 40px). Currently "Refresh libraries" text button wraps close to the Delete button.
- Add Server button sits under the subtitle at left; make it full width or keep in header right with the title wrapping.
- Status dot + readiness pill must wrap onto a second line, not overflow the title.

## Do NOT change
- `#serverList`, `.edit-server-btn`, `.refresh-libraries-btn`, `.delete-server-btn`, `data-id` attributes, `statusPillId` element id, `#editServerModal` and every `edit*` id inside it, `#addServerModal` include.
- Behaviour: health polling, readiness fetch, glyph opens Health tab, enable toggle PUT, delete confirm, refresh libraries.
- Edit modal contents (out of scope here).

## Test impact
- `tests/e2e/test_servers_page.py`, `test_journey_edit_existing_server.py`, `test_servers_jellyfin_trickplay.py`, `test_journey_jellyfin_off_media.py`, `test_intro_credits_server_tab.py`, `test_loudness_*`, `test_chapter_thumbnail_settings.py`, `test_ux_polish.py` reference `#serverList`, `.edit-server-btn` and friends. Keep those classes and they pass.
- Any test asserting the text "Connected" inside the card pill or `.badge` inside the card: grep `Connected` in `tests/e2e/test_servers_page.py` (L321 is `#connectResult`, unaffected). If a test reads pill text, keep the text visually hidden (`visually-hidden`) in the dot.
- `tests/test_routes.py` checks server page ids; re-run after.

## Card structure (fixed rows, all cards identical)
Header row (logo, name + status dot, enable switch) / host row / reserved issue row (readiness pill, error pill, or empty) / library tile / footer (last checked + edit, refresh, delete). Disabled shows as dimmed card + off switch (no extra pill). Measured: every row has the same height and offset in every card.

## Implementation parity
Checked against the pre-redesign `servers.js` card and `servers.html` page. All present after the change:
- Add Server button (opens `#addServerModal`), `#serverList`, loading spinner, load-error alert, empty state (now with an Add Server button).
- Per card: vendor logo, name, host URL (title tooltip), connection status (`#server-status-<id>`, now a dot with `role="img"`, title and aria-label; failure text also shown in the issue row `#server-error-<id>`), Setup Health glyph (`.server-readiness-glyph`, click/Enter/Space opens the Edit modal on the Health tab; shows `N must fix` / `N to fix`, hidden when healthy, disabled or unreachable), enable switch (`.server-enabled-toggle`, PATCH, toast, re-probe, reverts on error), enabled/total libraries, Edit (`.edit-server-btn`), Refresh libraries (`.refresh-libraries-btn`, spinner then reload, error toast), Delete (`.delete-server-btn`, confirm).
- Sequential connection then readiness probes; disabled servers skip probes.
- Edit modal and Add modal untouched.
Changed on purpose: Edit button text "Configure" became an icon button (aria-label `Edit <name>`); "Checking..." pill became a grey dot; the green check glyph for healthy servers is gone (data-state="ok", hidden); a disabled card is dimmed (`.off`) and the toggle label is visually hidden; disabling a server also clears its readiness pill.
