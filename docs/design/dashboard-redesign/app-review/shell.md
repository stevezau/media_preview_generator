# Global shell review (navbar, offcanvas, theme, toasts)

Mode: Operate. Source: `templates/base.html`, `static/css/style.css` (navbar ~L360-1100, mobile ~L1364+). Shots: `shots/dashboard-desktop.png`, `dashboard-mobile.png`, plus the navbar strip in every Tools shot. No light-theme shot exists; light rules were read, not seen.
Method note: critique.md asks for two isolated sub-agents plus the detector. This was one inline pass, no `impeccable detect` run. Treat as design review only.

## 1. Verdict
- The bar works and is legible, but the right cluster mixes four shapes: 38px circles, an outlined Star pill, an un-circled Help icon, and a text Logout link.
- The bell badge is an inline-styled stock Bootstrap red pill. It is not the mockup badge.
- The phone menu is a long single column of six utility rows. It needs grouping, not removal.

## 2. Findings

### P0
**S-P0-1. Design tokens exist only on the dashboard page.** `--t-*`, `--ok/-soft`, `--bad/-soft`, `--run`, `--warn`, `--inset`, `--line` will be added to `dashboard.css`, which only `index.html` loads (`index.html:7`). Tools pages and the navbar load `style.css` only.
Change: add the full token block from `mockup.html` (dark L12-30, light L37-44) to the `:root` and `html[data-bs-theme="light"]` blocks in `style.css` (L11-130), alias `--inset: var(--surface-1)`, `--line: var(--surface-border)`. Do NOT define them in `dashboard.css` only. Every Tools change below depends on this.

**S-P0-2. Help button is not in the icon-button rule.** `style.css` L843-867 gives bell, theme and coffee a 38px circle. `#helpMenuBtn` is missing, so Help is the only bare glyph with a different hit area and hover (dashboard-desktop navbar: ? sits flush, others are circles).
Change: one shared class. Replace the three-ID selector lists with `.navbar .nav-item > .nav-ibtn` and add `class="nav-ibtn"` to `#notificationBellBtn`, `#helpMenuBtn`, `#sponsorLinkBtn`, `#themeToggleBtn`. Keep all IDs. Spec at >=1200px: 34x34, `border-radius: 8px` (mockup `--radius-xs`), colour `var(--muted)`, hover `background: var(--inset); color: var(--text)`. Not 50% circles, not amber hover (amber is reserved for primary action, next schedule, job IDs; `style.css` L861-863 forces amber on hover, remove that). Coffee keeps `#db61a2`.

### P1
**S-P1-1. Unify the right cluster.** Order stays: bell, help, coffee, Star, theme, Logout.
- Star: keep outlined pill, set height 34px to match the icon buttons (now 30px, `style.css` L915). Border `var(--line)`, hover border `var(--faint)` not amber. Star glyph stays gold.
- Logout: keep the text and icon. Make it a ghost button: height 34px, `padding: 0 .7rem`, colour `var(--muted)`, hover `background: var(--bad-soft); color: var(--bad)` (it ends a session, mirrors the delete-icon hover in the mockup).
- Add a 1px x 18px `var(--line)` divider between the primary nav and the utility cluster, and between {bell, help} and {coffee, Star, theme}. Do it with `.navbar-nav-utility > li:nth-child(n)` pseudo-elements or `margin-left` on `#sponsorLinkBtn`'s li. Desktop only.
Files: `style.css` L832-950, `base.html` L270-338.

**S-P1-2. Bell badge.** Today: inline `style="top:35%;left:70%;font-size:.65rem"` + `bg-danger` + `translate-middle` (`base.html` ~L246).
Change: remove the inline style. CSS: `#notificationBellBadge { position:absolute; top:2px; right:2px; left:auto; transform:none; min-width:16px; height:16px; padding:0 4px; font-size:10px; font-weight:700; line-height:16px; background:var(--bad); color:#fff; box-shadow:0 0 0 2px var(--plex-dark); }`. The ring colour must match the navbar background (`var(--plex-dark)` dark; `#fff` in the light theme, set in its own rule). In `notifications.js` L65 cap the text: `n > 9 ? '9+' : String(n)` and keep the full count in the `visually-hidden` span and in `title`. Keep `d-none` toggling and IDs.

**S-P1-3. Active state.** Active top-level link already uses amber text on `--plex-orange-soft` (`style.css` L412). That matches `.tab.on` in the mockup. Two gaps:
- Inactive links use the body colour; mockup uses `--muted` with hover `--text`. Set `.navbar .nav-link { color: var(--muted) }` and hover `var(--text)`. Keeps amber as the one signal.
- Add `aria-current="page"` to the active `.nav-link` and the active `.dropdown-item` in the Tools menu (`base.html` L193-207). Dashboard and Servers links currently carry only a CSS class.
- Keep the `nav-hover-menu` behaviour (`nav_menus.js`) as is.

**S-P1-4. Offcanvas (phone) utility block.** `dashboard-mobile.png` is a full-page capture so the open menu is not shown; this is from CSS/markup. After the divider there are six full-width rows (Notifications, Help & feedback, Buy me a coffee, Star on GitHub, Switch to light theme, Logout).
Change, at `max-width: 1199.98px` in `.navbar-nav-utility`: lay the first five items out as a 2-column grid of 44px-tall tiles (`display:grid; grid-template-columns:1fr 1fr; gap:8px`), each tile `background: var(--inset); border:1px solid var(--line); border-radius:8px; padding:0 .75rem`, icon + label. Bell tile shows the count inline (`Notifications 1`) instead of a floating dot. Pin Logout full-width at the bottom: make `.offcanvas-body` a flex column, `.navbar-nav-utility { margin-top:auto }`, and Logout last with `border-top:1px solid var(--line)`. The Notifications dropdown opens inline; keep `data-bs-auto-close="outside"`. All six items stay.
Also: rotate the `dropdown-toggle` caret when a section is expanded; indent expanded children 1.75rem; active child gets the soft-amber fill, no side stripe.

### P2
**S-P2-1. Brand.** "local build" is 11px; confirm it uses `--faint` (`#8686a0` dark / `#6b7186` light, 4.5:1). Check `.navbar-version` colour (`style.css` L987). Keep the update dot.
**S-P2-2. Toast.** One shared `#toastNotification` (`base.html` ~L457); a second toast replaces the first. Change: icon in a 28px soft tile using `--ok-soft/--bad-soft/--warn-soft/--run-soft` with matching glyph colour (map in `app.js` L4237-4243 from `text-success` etc. to the tokens); title 14px/600; no coloured border or left stripe. Mobile: `.toast-container` full-width with `padding: 12px 16px calc(12px + env(safe-area-inset-bottom))`. Error toasts: `autohide: false` (default 5s is too short to read a failure). Keep IDs `toastNotification/toastTitle/toastBody/toastIcon` and `showToast(title,message,type)` signature.
**S-P2-3. Footer.** There is none. Do not add one. The version already lives under the wordmark, GitHub/Docs links live in Help. Ensure `main` has `padding-bottom: 2rem` so the last card does not touch the viewport edge (Tools shots end hard).
**S-P2-4. Theme toggle.** Icon swaps moon/sun and the mobile label reads "Switch to light theme". Fine. Add `aria-pressed` reflecting dark, and keep `localStorage.theme`. Verify the toggle updates tooltip text too.
**S-P2-5. Dropdown menus.** Tools/Help/Notifications menus: radius 8px, `border: 1px solid var(--line)`, item hover `var(--inset)`, active item soft amber. One shared rule so the three match.

## 3. Mobile issues (390px)
- Bar is fine: brand ellipsizes, hamburger is 44px. Keep.
- Open drawer: utility stack is long (S-P1-4). Logout is mid-list instead of last-and-anchored.
- Bell badge on mobile: the badge sits on the icon, but the drawer row also shows the label; the dot then floats oddly. Use the inline count there.
- Toast covers the bottom of the page including pagination controls; add the safe-area bottom padding.

## 4. Do NOT change
- Items and order: Dashboard, Servers, Automation, Settings, Tools menus, bell, help, coffee, Star, theme, Logout.
- All IDs: `navbarNav`, `notificationBellBtn/Badge/Icon/Label/List/Empty/ResetBtn`, `helpMenuBtn`, `sponsorLinkBtn`, `navStarBtn`, `themeToggleBtn/Icon/Label`, `navLogoutBtn`, `navVersion/Text/Dot`, `navAutomationDropdown` etc., `data-nav-menu-toggle`, `data-anchor`.
- Breakpoint `xl` (1200px) for the offcanvas switch. The `backdrop-filter: none` on mobile (it breaks `position:fixed` children).
- Logout stays a POST form with CSRF token.
- Pink coffee and gold star colours. Skip link. `.navbar-star-word` hide rule between 1200-1399px.

## 5. Test impact
- `tests/test_sponsor_links.py`, `tests/test_navbar_version.py`: assert IDs/hrefs/labels. Safe if IDs and aria-labels stay.
- `tests/e2e/test_navbar_links.py`, `test_navbar_menus.py`, `test_theme_toggle.py`, `test_journey_notifications_lifecycle.py`: check these for selectors that expect a circle size or the badge's inline style, and for text `0` -> `9+` changes. Run: `pytest -m e2e -n 8 --no-cov tests/e2e/test_navbar_links.py tests/e2e/test_navbar_menus.py tests/e2e/test_theme_toggle.py tests/e2e/test_journey_notifications_lifecycle.py`.
- `tests/test_csrf.py` references the logout form; do not move the token input.
- Add: a unit test that `base.html` renders `aria-current="page"` on the active nav link (matrix: Dashboard, Servers, Automation, Settings, Tools children).
