# Setup wizard (/setup)

**Verdict**
- First step is clear (three vendor cards), but the page does not look like a first-run flow: full app nav is visible, step 1 is named "Sign In" while the content says "Choose your media server", and Skip is a small underlined link.
- Wizard is 5 steps over 1667 lines; steps 3-4 are long forms (not screenshotted, review them live).
- Fix: focused layout, honest step names, button hierarchy, grouped long forms.

Shots: `shots/setup-desktop.png`, `shots/setup-mobile.png` (step 1 only). Files: `templates/setup.html` (markup L1-490, inline JS below), `static/css/pages/setup.css`.

## P0
None. Skip is confirmed by modal, so no data loss risk.

## P1
1. **Step label mismatch**: stepper says "1 Sign In", panel says "Choose your media server". Rename label to "Server type" (and review 2 "Server" -> "Connect", 3 "Paths", 4 "Options", 5 "Security"). Edit labels at `setup.html` L18-40. Keep `data-step`.
2. **Skip is an underlined text link left-aligned outside the card** (desktop at y~568; the card is centred, the link is not).
   Change: place inside the card footer, right aligned, as a `btn btn-outline-secondary btn-sm` with icon. Copy: "Skip for now". Keep id `skipSetupBtn` and the appConfirm flow.
3. **Vendor cards use three different outline colours** (amber Plex, green Emby, cyan Jellyfin) via `btn-outline-primary/success/info`. These collide with the task-type hues (blue/violet/teal) and amber is reserved for primary.
   Change: neutral cards (`--inset` background, `--line` border), vendor logo in a 40px tile, vendor name in default text colour, hover = amber outline, focus ring 2px amber. Keep the buttons as `<button>` with `data-vendor` and `.wizard-vendor-btn`.
4. **Full app navbar shown during first-run** (including Dashboard, Settings, Logout, notifications). On first run this invites leaving mid-wizard.
   Change: in `setup.html` override the nav block to show only brand + theme toggle (+ Logout when auth is on). If base.html can't be overridden cleanly, add a `body.setup-mode .navbar .nav-link {display:none}` rule scoped to this page.
5. **Stepper has no completed/error affordance beyond colour.** Completed steps get a check icon in the circle (CSS class `.progress-step.completed` exists), and step labels remain visible on mobile as the current step name only.
6. **Long forms in steps 3 and 4** (paths: Plex config folder, path mappings, exclude paths, "Check file"; processing options). Group with fieldset headings and a short helper line each; move advanced items (exclude paths, path mappings) into a collapsed "Advanced" `<details>` when the default is fine. Make "Check file" a clear secondary button next to its input. Keep every id.

## P2
1. Title "Media Preview Generator" 32px duplicates the brand in the navbar; reduce to 24px or drop and use "Set up your server".
2. Footer buttons: Back = outline secondary, Next = primary amber, Finish = `btn-success` green (step 5). Make Finish the amber primary too (one primary colour); green is for status.
3. Disabled `Next` gives no reason. Add helper text next to it ("Connect a server to continue") or `aria-disabled` with tooltip.
4. Add (i) tooltips next to non-obvious path terms (path mapping, exclude path) per project pattern.
5. Light theme: check card border contrast; `--line` token.

## Mobile 390px
- Vendor cards stack at 130px each: switch to compact rows (logo left, name + hint right, 64px tall) so all three are visible without scroll.
- Stepper labels are already hidden; add "Step 1 of 5 - Server type" text under it for orientation.
- Skip link sits at left outside the card: move into card footer full-width secondary.
- Heading wraps to two lines; shrink to 22px.
- Back/Next footers: make them a sticky bottom bar (50/50 buttons) on steps 2-5.

## Do NOT change
- `.setup-step[data-step]`, `.progress-step[data-step]`, `.step-number`, `.step-connector`, `#step{1..5}Next/Back`, `#step1Next`, `#finishSetup`, `#skipSetupBtn`, `.wizard-vendor-btn[data-vendor]`, `#vendorPickerBackBtn`, `#manualPlexTestBtn`, `#serverUrlApply`, `#wizardPlexConfigFolder*`, `#setupAddPathMappingBtn`, `#setupCheckFile`, `#setupAddExcludePathBtn`, path-mapping and exclude row classes (`path-mapping-browse`, `path-mapping-remove`, `exclude-path-remove`).
- Step order, validation, the `/api/setup/skip` POST, the shared `_add_server_modal.html`, setup JS state machine.

## Test impact
- Many wizard e2e files: `tests/e2e/test_wizard_step1_vendor_picker.py`, `test_wizard_step2_libraries.py`, `test_wizard_step3_paths.py`, `test_wizard_step4_processing.py`, `test_wizard_step5_security.py`, `test_wizard_full_flows.py`, `test_wizard_emby_jellyfin_inline.py`, `test_journey_jellyfin_wizard_full.py`, `test_webapp.py`, `test_folder_picker.py`, `test_operational_followup.py`. They key on ids/classes above.
- Likely breakers: tests that match button text "Skip" (`test_wizard_step1_vendor_picker.py` TestSkipSetup ~L85-100; if copy changes to "Skip for now" a substring "Skip" match still works), tests expecting `.btn-success` on `#finishSetup`, tests expecting step label text "Sign In". Grep `Sign In`, `btn-success`, `Skip` in those files and update.
- Hiding navbar links on setup: check `test_navbar_menus.py` does not visit `/setup`.

## Implementation parity

Built in `templates/setup.html` + `static/css/pages/setup.css`. Shots: `impl/setup-step{1..5}-{dark-1440,light-1440,mobile-390}.png`, plus `setup-step1-plex-dark-1440.png` and `setup-step1-emby-dark-1440.png` for the inline vendor panels.

Every control and state below exists after the redesign; ids, payloads, validation and the JS state machine are unchanged.

- **Step 1**: vendor cards (`.wizard-vendor-btn[data-vendor]` plex/emby/jellyfin, now neutral buttons); Plex panel (`#plexSignInBtn` PIN flow, `#authStatus`, `#authError`, manual URL+token details with `#manualPlexUrl/#manualPlexToken/#manualPlexVerifySsl/#manualPlexTestBtn`, `#vendorPickerBackBtn`); Emby/Jellyfin inline panel (`#ejConnectPanel`, shared `_server_connection_form.html`, untouched); `#skipSetupBtn` (appConfirm then `POST /api/setup/skip`); `#step1Next`.
- **Step 2**: `#serverLoading`, `#serverSelect`, `#serverUrlOverride` (`#serverUrlInput`, `#serverUrlApply`, `#setupPlexVerifySsl`), `#librariesCard` (`#libraryLoading`, `#libraryGrid`, library cards), `#step2Back/#step2Next`.
- **Step 3**: `#step3PlexConfigSection` (`#wizardPlexConfigFolder`, browse button, validation feedback), "Check a real file" (`#setupSamplePath`, `#setupCheckFile`, `#setupPathPreviewResult`), path mapping table and exclude table with all row classes, `#setupAddPathMappingBtn`, `#setupAddExcludePathBtn`, `#step3Back/#step3Next`.
- **Step 4**: GPU panel (`#gpuConfigContainer`, `#gpuDetecting`, `#gpuConfigList`, `#gpuRescanBtn`), `#workerGroupSettings`, `#thumbnailInterval`, `#thumbnailQuality`/`#qualityValue`, `#step4Back/#step4Next`.
- **Step 5**: `#tokenEnvNotice`, `#customTokenSection` (`#newToken`, `#confirmToken`, `#tokenError`, `#tokenSuccess`), `#step5Back`, `#finishSetup` (validation messages unchanged).
- Resume notice (`#setupResumeNotice`), emby/jellyfin 4-step flow (step 2 dot hidden), `aria-valuenow/label` on the progress bar, `#setupProgressCurrent`.

Changes against the review:
- Labels: Server type / Connect / Paths / Options / Security (JS `stepLabels` updated). Completed steps show a check (CSS only; JS still writes the digit).
- Skip is an in-card `btn-outline-secondary` "Skip for now" (mobile: full width). Finish is amber `btn-primary` (copy kept: "Complete Setup").
- Neutral vendor cards; mobile 64px rows.
- Full navbar hidden with `body:has(.setup-container) > .navbar { display:none }` (base.html has no navbar block); replaced by `.setup-topbar` (brand, theme toggle via `#setupThemeBtn`, Logout POST form `#setupLogoutBtn`). The navbar's own `#themeToggleBtn`/`#navLogoutBtn` stay in the DOM, hidden.
- Steps 3-4 grouped with fieldset legends. Path mappings and exclude paths moved into a collapsed "Advanced" `<details id="setupAdvancedDetails">`; it opens itself when saved rows exist (`openSetupAdvanced()`), and "Check a real file" moved above it.
- Mobile: Back/Next is a sticky bottom bar inside the card; Next while disabled is neutral grey.
- Header h1 "Set up your server" at 24px (22px mobile).

Not done / differences from the mockup:
- Mockup steps 2 and 4 contents were invented; the real fields were kept (server dropdown + URL override, library grid; GPU/worker groups/thumbnails).
- "Why is Next disabled" helper text (P2.3): not added; needs JS state per step.
- Step 4 worker-group panel is rendered by `worker_groups.js` + `worker_groups.css` (dashboard builder's files); at the time of the shots it looked unstyled inside the wizard.
- Mockup's `--panel` token is `--bs-card-bg` here (no new token needed).
- Mockup subtitle "Five quick steps" omitted: Emby/Jellyfin flows have four.
