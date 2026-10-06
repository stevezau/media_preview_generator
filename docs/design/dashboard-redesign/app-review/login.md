# Login (/login)

**Verdict**
- Good: small, focused, one primary button, helpful token hints, error states exist (wrong token, rate limit, expired).
- Weak: the floating-label token field looks broken (label sits top-left in a tall empty box), no show/hide for the token, help block is dense and left-aligned in a centred card.
- Fix: a normal labelled input with reveal toggle and a quieter help section.

Shot: `shots/login-desktop.png` (no mobile shot; reasoning from CSS). Files: `templates/login.html`, `static/css/pages/login.css`.

## P0
None. Autofocus, `autocomplete="current-password"`, `aria-describedby`, and `aria-invalid` on error are already right.

## P1
1. **Token field renders as a 58px box with the label pinned top-left and ~25px of empty space** (shot, y 383-440). It reads like a textarea.
   Change: replace `.form-floating` with a plain label above (`<label for="token">Authentication token</label>`) and an input group: key icon, input (`type=password`, 44px high), eye button to toggle visibility with `aria-label="Show token"`/`"Hide token"`, `aria-pressed`. Keep `id="token"`, `name="token"`, `required`, `autofocus`, `aria-describedby="loginTokenHelp"`.
   Edit: `login.html` L72-76, `login.css`; small inline script for the toggle.
2. **Help block is permanently expanded with 3 bullets + a second `<details>`.** First-time users need it; returning users never do.
   Change: collapse the whole "How do I find my token?" into one `<details>` (open when `error` or `rate_limited`, as the existing second one already does). Inside, merge the two levels: the three bullets, then the compose snippet. Monospace inline code in neutral inset; amber only for the Sign In button.
3. **Subtitle copy mismatch**: template text is "Sign in to manage your media processing." but the shot shows "Sign in to continue". Pick one (use "Enter your token to continue"), because the page asks for a token, not a user/password.
4. **Error states are alert boxes with icon + bold + small text**; fine. Move the alert above the field (already) and put focus on the input with the error styling (red border from `aria-invalid`). Verify the red border is visible on the dark gradient (>=3:1).

## P2
1. Brand: film icon (bootstrap `bi-film`) at 60px differs from the navbar logo image. Use the same logo asset as the navbar at 44px.
2. Page background has two radial colour washes (amber, blue). Per brief, amber is for actions; make it a flat `--bg` with one neutral radial. Light theme already neutral.
3. Add visible theme toggle? Page follows `data-bs-theme`; leave.
4. Add a link-button "Copy path" next to `/config/auth.json`? Skip, low value.
5. Card has no heading element: title is a `<p>`/div? Make the app name an `<h1>`.

## Mobile 390px
- Card has 16px side padding via body; card inner padding 24px leaves ~310px for the input: fine.
- The two `<code>` snippets (`docker logs <container>`, `WEB_AUTH_TOKEN`) wrap awkwardly: set `overflow-wrap:anywhere` on `.login-help code`.
- Keep Sign In full width, 48px tall; make sure the soft keyboard doesn't hide it (card `align-items:flex-start` on short viewports).
- `body { display:flex; align-items:center }` + `height:100%` can clip on landscape phones: use `min-height:100dvh`.

## Do NOT change
- Form method/action, hidden `csrf_token`, `name="token"`, `id="token"`, `id="loginTokenHelp"`, `.alert-danger` for the wrong-token message (tests key on it), the "didn't work" copy wording (test matches "didn"), rate-limited and expired-page alerts and their conditions, `autofocus`.
- `.login-card`, `.form-signin`, `.logo-container` class names if tests use them.

## Test impact
- `tests/e2e/test_login_page.py`: `#token` fill, `button[type="submit"]` click (keep exactly one submit button; the eye toggle must be `type="button"`), `.alert-danger` containing "didn". Focus assertion accepts `token` id.
- `tests/e2e/test_operational_layouts.py`, `test_csrf_fetch.py`, `test_wizard_step5_security.py` reference login selectors; re-run them.
- Collapsing the help `<details>` may break a test that expects `/config/auth.json` visible; grep `auth.json` in tests/e2e and open the details first if needed.

## Implementation parity

Built in `templates/login.html` + `static/css/pages/login.css`. Shots: `impl/login-{dark-1440,light-1440,mobile-390,wrongtoken-dark-1440,wrongtoken-mobile-390}.png`. The rate-limited state was checked in the browser earlier (alert + open help) and is covered by `tests/test_setup_login_template.py`; no separate shot saved.

Unchanged: form `POST` to `main.login`, hidden `csrf_token`, `name="token"`, `id="token"`, `required`, `autofocus`, `autocomplete="current-password"`, `aria-describedby="loginTokenHelp"`, `aria-invalid="true"` on error, exactly one submit button, `.alert-danger` with the "didn't work" wording, rate-limited and expired alerts with their conditions, the `loginTokenHelp` id, `.login-card`/`.form-signin`/`.logo-container` class names, and all server-side auth/rate-limit code.

Changed:
- Plain label above an input group (key icon, input, eye toggle). The toggle is `type="button"` `#tokenReveal`, with `aria-label` Show/Hide token and `aria-pressed`.
- Whole help is one `<details id="loginTokenHelp">`, open on `error` or `rate_limited` (bullets, then first-time text, then compose snippet).
- Subtitle "Enter your token to continue". Navbar logo asset at 44px. Flat neutral background (amber/blue washes removed). `<h1>` kept. `min-height:100dvh`, `overflow-wrap:anywhere` on code.
- Alerts sit above the field; red invalid border plus a soft ring on error.
- States (default / wrong token / rate limited) match the mockup. The expired-page alert (not in the mockup) keeps the amber warning style.
