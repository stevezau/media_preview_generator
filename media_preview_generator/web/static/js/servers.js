// Servers page — fetches /api/servers and drives the Add Server wizard.
//
// Stays vanilla JS / Bootstrap 5; no framework so the page mounts the same
// way as the rest of the app. CSRF token comes from the <meta> tag the base
// template renders.

(function () {
    'use strict';

    const $ = (sel, el) => (el || document).querySelector(sel);
    const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    async function api(method, url, body) {
        const opts = {
            method,
            headers: { 'X-CSRFToken': csrfToken() },
        };
        if (body !== undefined) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const r = await fetch(url, opts);
        let data = null;
        try { data = await r.json(); } catch (_) { /* non-JSON */ }
        return { ok: r.ok, status: r.status, data };
    }

    // ---------- inline form-validation helpers --------------------------------
    // Centralised so popups never appear for form errors. Marking a field
    // invalid relies on Bootstrap's `.is-invalid` + adjacent
    // `.invalid-feedback` div pattern. Helpers also self-clear on input.
    function markFieldInvalid(input, msg) {
        if (!input) return;
        input.classList.add('is-invalid');
        // Override the static feedback message if one was provided.
        const fb = input.parentElement && input.parentElement.querySelector('.invalid-feedback');
        if (fb && msg) fb.textContent = msg;
        // Re-clear once the user edits the field, so the red doesn't stick.
        if (!input.dataset.invalidWired) {
            input.addEventListener('input', () => input.classList.remove('is-invalid'), { once: true });
            input.dataset.invalidWired = '1';
        }
        input.focus();
    }

    function clearFieldErrors(rootSelector) {
        $$(`${rootSelector || '#step-connect'} .is-invalid`).forEach(el => el.classList.remove('is-invalid'));
    }

    function showFormError(msg, region) {
        const el = $(region || '#connectFormError');
        if (!el) return;
        el.textContent = msg;
        el.classList.remove('d-none');
    }

    function clearFormError(region) {
        const el = $(region || '#connectFormError');
        if (!el) return;
        el.classList.add('d-none');
        el.textContent = '';
    }

    // ---------- list rendering -------------------------------------------------
    async function loadServers() {
        const list = $('#serverList');
        list.className = 'srv-grid';
        list.innerHTML = '<div class="srv-state text-muted"><div class="spinner-border" role="status"></div></div>';
        const r = await api('GET', '/api/servers');
        if (!r.ok) {
            list.innerHTML = `<div class="srv-state"><div class="alert alert-danger mb-0">Failed to load servers (HTTP ${r.status}).</div></div>`;
            return;
        }
        const servers = (r.data && r.data.servers) || [];
        if (servers.length === 0) {
            list.className = 'srv-grid';
            renderServerSummary([]);
            list.innerHTML = `
                <div class="srv-state">
                    <div class="srv-empty">
                        <span class="srv-vlogo"><i class="bi bi-hdd-network"></i></span>
                        <strong>No servers yet</strong>
                        <span>Connect Plex, Emby or Jellyfin to start generating previews.</span>
                        <button type="button" class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#addServerModal">
                            <i class="bi bi-plus-lg me-1"></i>Add Server
                        </button>
                    </div>
                </div>`;
            return;
        }
        // One or two servers get roomy full-width rows; from three the cards form a grid. Both carry the same ids,
        // classes and data attributes, so every handler below (and the probes) work on either.
        const asRows = servers.length <= ROW_LAYOUT_MAX;
        list.className = asRows ? 'srv-rows' : 'srv-grid';
        list.innerHTML = servers.map(asRows ? serverRow : serverCard).join('') + (asRows ? addServerRow() : addServerTile());
        renderServerSummary(servers);
        $$('.delete-server-btn').forEach((btn) => {
            btn.addEventListener('click', (ev) => {
                deleteServerWithConfirm(ev.currentTarget.dataset.id, ev.currentTarget.dataset.name);
            });
        });
        $$('.edit-server-btn').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                const id = ev.currentTarget.dataset.id;
                openEditModal(id);
            });
        });
        $$('.refresh-libraries-btn').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                // Capture the button before await — `ev.currentTarget` is
                // nulled once the event handler returns, which happens at
                // the first await suspension.
                const target = ev.currentTarget;
                const id = target.dataset.id;
                target.disabled = true;
                target.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
                const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/refresh-libraries`);
                if (r.ok) loadServers();
                else {
                    showToast('Refresh failed', `${(r.data && r.data.error) || r.status}`, 'danger');
                    target.disabled = false;
                    target.innerHTML = '<i class="bi bi-arrow-clockwise"></i>';
                }
            });
        });
        // Wire readiness glyph click + keyboard activation → open Edit
        // modal DIRECTLY on the Setup Health tab so a user clicking a
        // ⚠/❗ lands on the fix-it UI without another navigation click.
        // role="button" sets the screen-reader expectation that
        // Enter/Space work too.
        $$('.server-readiness-glyph').forEach((glyph) => {
            const open = () => openEditModal(glyph.dataset.id, { openTab: 'health' });
            glyph.addEventListener('click', open);
            glyph.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    open();
                }
            });
        });
        // Quick enable/disable toggle on each card.
        $$('.server-enabled-toggle').forEach((cb) => {
            cb.addEventListener('change', async (ev) => {
                const target = ev.currentTarget;
                const id = target.dataset.id;
                const enabled = target.checked;
                target.disabled = true;
                const r = await api('PATCH', `/api/servers/${encodeURIComponent(id)}/enabled`, { enabled });
                target.disabled = false;
                if (r.ok) {
                    const label = target.parentElement.querySelector('label');
                    if (label) label.textContent = enabled ? 'Enabled' : 'Disabled';
                    const card = target.closest('.srv-card');
                    if (card) {
                        card.classList.toggle('off', !enabled);
                        delete card.dataset.conn;
                    }
                    showToast('Server updated', `${enabled ? 'Enabled' : 'Disabled'} successfully`, 'success');
                    // Re-probe connection status — disabled servers shouldn't
                    // probe (we'd hit a server the user just paused).
                    if (enabled) probeServerConnection(id);
                    else {
                        updateServerStatusPill(id, { ok: null, message: 'Disabled' });
                        updateServerReadinessGlyph(id, null);
                    }
                } else {
                    target.checked = !enabled;  // revert on error
                    showToast('Update failed', `${(r.data && r.data.error) || r.status}`, 'danger');
                }
            });
        });
        // Per-card connection + readiness probe — sequential per server
        // to avoid hammering 3+ servers in parallel from the same
        // browser tab. Each probe is ~200-1500ms; on a Plex with Intro &
        // Credits switched on the readiness also runs the marker
        // capability check, which waits on Plex's SQLite lock for
        // markers.inspect.UI_DB_WAIT_S (5 s) per check rather than a
        // job's 30 s budget, and is cached 60 s — 5 s while it isn't
        // healthy. A load that finds a check already running for the
        // same server is served the last answer instead of queueing
        // behind it. Connection runs
        // first and returns its status; when the server is unreachable
        // we SKIP the readiness probe (the endpoint would error out and
        // paint a misleading "unknown" glyph when the real problem is
        // the connection pill below).
        (async () => {
            for (const s of servers) {
                if (!s.enabled) {
                    updateServerStatusPill(s.id, { ok: null, message: 'Disabled' });
                    updateServerReadinessGlyph(s.id, null);
                    continue;
                }
                const connOk = await probeServerConnection(s.id);
                if (connOk) {
                    await probeServerReadiness(s.id);
                } else {
                    // Connection failed — hide the readiness glyph; the
                    // connection pill already signals the underlying
                    // problem, no need to double-warn.
                    updateServerReadinessGlyph(s.id, { unknown: true });
                }
            }
        })();
    }

    async function probeServerConnection(serverId) {
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/test-connection`);
            const data = r.data || {};
            const ok = data.ok === true;
            updateServerStatusPill(serverId, {
                ok,
                message: data.message || (ok ? 'Connected' : 'Connection failed'),
            });
            return ok;
        } catch (e) {
            updateServerStatusPill(serverId, { ok: false, message: String(e) });
            return false;
        }
    }

    // The status element is a dot; its title/aria-label carry the words. A failure message also lands in the
    // reserved issue row so it is readable without hovering.
    function updateServerStatusPill(serverId, { ok, message }) {
        const dot = document.getElementById(`server-status-${serverId}`);
        if (!dot) return;
        let tone = 'bad';
        let label = message || 'Connection failed';
        if (ok === null) {
            tone = '';
            label = message || 'Disabled';
        } else if (ok) {
            tone = 'ok';
            label = message || 'Connected';
        }
        dot.className = `srv-dot ${tone}`.trim();
        dot.title = label;
        dot.setAttribute('aria-label', label);
        const statusText = document.getElementById(`server-status-text-${serverId}`);
        if (statusText) {
            statusText.textContent = ok === false ? 'Unreachable' : ok === null ? 'Disabled' : 'Connected';
            statusText.classList.toggle('bad', ok === false);
        }
        const card = dot.closest('.srv-card');
        if (card) card.dataset.conn = ok === false ? 'bad' : ok === null ? 'off' : 'ok';
        refreshServerSummary();

        const error = document.getElementById(`server-error-${serverId}`);
        if (error) {
            error.classList.toggle('d-none', ok !== false);
            error.title = label;
            error.querySelector('span').textContent = ok === false ? label : '';
        }
        const hint = document.getElementById(`server-checked-${serverId}`);
        if (hint) {
            hint.textContent = ok === null
                ? 'Not checked while disabled'
                : `Last checked ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
        }
    }

    async function probeServerReadiness(serverId) {
        // Unified Setup Health probe — drives the inline glyph next to
        // each server name on the card. Same endpoint the Edit modal's
        // Setup Health section consumes, so the glyph and modal agree
        // without a second probe. Single source of truth.
        try {
            const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/previews-readiness`);
            if (!r.ok || !r.data) {
                updateServerReadinessGlyph(serverId, { unknown: true });
                return;
            }
            // Roll up sections[] to the worst severity present. Mirrors
            // the modal's _deriveBadgeState walker — BOTH section-level
            // (section.ok/severity) AND per-check (check.ok/severity)
            // are checked so the glyph and the modal badge can never
            // disagree. Per-check counts drive the tooltip copy; a
            // section-level-only failure still registers in anyCritical
            // / anyRecommended ONLY when its checks[] is empty — when
            // checks[] is non-empty, the per-check walk is
            // authoritative AND lets dismissed flags silence the
            // recommended tier (issue #237).
            const sections = r.data.sections || [];
            let anyCritical = false;
            let anyRecommended = false;
            let criticalCount = 0;
            let recommendedCount = 0;
            for (const section of sections) {
                if (!(section.checks && section.checks.length)) {
                    if (section.ok === false) {
                        if (section.severity === 'critical') anyCritical = true;
                        else if (section.severity === 'recommended') anyRecommended = true;
                    }
                }
                for (const check of (section.checks || [])) {
                    if (check.ok === false && check.severity === 'critical') {
                        // Critical: dismiss flag is a no-op, mirrors
                        // partition-layer safety enforcement.
                        anyCritical = true;
                        criticalCount += 1;
                    } else if (check.ok === false && check.severity === 'recommended') {
                        if (check.dismissed === true) continue;
                        anyRecommended = true;
                        recommendedCount += 1;
                    }
                }
            }
            if (anyCritical) {
                const tooltip = criticalCount > 0
                    ? `${criticalCount} critical setup issue${criticalCount === 1 ? '' : 's'} — click to fix`
                    : 'Critical setup issue — click to fix';
                updateServerReadinessGlyph(serverId, { state: 'critical', tooltip, count: criticalCount });
            } else if (anyRecommended) {
                const tooltip = recommendedCount > 0
                    ? `${recommendedCount} recommended improvement${recommendedCount === 1 ? '' : 's'} — click to review`
                    : 'Recommended improvement available — click to review';
                updateServerReadinessGlyph(serverId, { state: 'recommended', tooltip, count: recommendedCount });
            } else {
                updateServerReadinessGlyph(serverId, {
                    state: 'ok',
                    tooltip: 'Setup healthy — click for details',
                    count: 0,
                });
            }
        } catch (e) {
            updateServerReadinessGlyph(serverId, { unknown: true });
        }
    }

    function updateServerReadinessGlyph(serverId, info) {
        const glyph = document.getElementById(`server-readiness-${serverId}`);
        if (!glyph) return;
        // Disabled cards and failed probes hide the pill: the dot and error row already say what is wrong. The
        // marker class stays so a re-render that looks for .server-readiness-glyph still finds it.
        const note = document.getElementById(`server-health-note-${serverId}`);
        const setNote = (text, tone) => {
            if (!note) return;
            note.textContent = text;
            note.className = `srv-health-note${tone ? ` ${tone}` : ''}`;
        };
        if (info === null || info.unknown) {
            glyph.className = 'server-readiness-glyph d-none';
            glyph.removeAttribute('data-state');
            setNote(info === null ? 'Not checked while disabled' : '', '');
            refreshServerSummary();
            return;
        }
        // A healthy server shows no pill; the green dot already says so.
        glyph.dataset.state = info.state;
        const count = typeof info.count === 'number' ? info.count : 0;
        if (info.state === 'critical') {
            glyph.className = 'server-readiness-glyph pill bad';
            glyph.innerHTML = `<i class="bi bi-exclamation-triangle-fill"></i>${count > 0 ? `${count} must fix` : 'Must fix'}`;
        } else if (info.state === 'recommended') {
            glyph.className = 'server-readiness-glyph pill warn';
            glyph.innerHTML = `<i class="bi bi-exclamation-circle-fill"></i>${count > 0 ? `${count} to fix` : 'To review'}`;
        } else {
            glyph.className = 'server-readiness-glyph d-none';
        }
        if (info.state === 'ok') setNote('Setup is healthy', 'ok');
        else setNote('Open to review and fix', '');
        glyph.title = info.tooltip || '';
        glyph.setAttribute('aria-label', info.tooltip || '');
        refreshServerSummary();
    }

    function serverCard(server) {
        const libCount = (server.libraries || []).length;
        const enabledLibs = (server.libraries || []).filter((l) => l.enabled).length;
        const vendorLogo = vendorLogoHtml(server);
        const id = escapeHtml(server.id);
        const name = escapeHtml(server.name);
        const statusPillId = `server-status-${id}`;
        const enabledToggleId = `server-enabled-${id}`;
        const readinessGlyphId = `server-readiness-${id}`;
        // Every card renders the same rows (header, host, issue slot, libraries, footer) so a row sits at the same
        // height in every card; the issue slot is reserved even when empty.
        return `
            <article class="card srv-card${server.enabled ? '' : ' off'}" data-id="${id}">
                <div class="srv-top">
                    <span class="srv-vlogo">${vendorLogo}</span>
                    <div class="srv-title">
                        <span class="srv-dot" id="${statusPillId}" role="img" aria-label="Checking connection" title="Checking connection"></span>
                        <h2 class="srv-name" title="${name}">${name}</h2>
                    </div>
                    <div class="form-check form-switch mb-0" title="Quick enable/disable — when off, this server is ignored by all jobs and webhooks">
                        <input class="form-check-input server-enabled-toggle" type="checkbox"
                               id="${enabledToggleId}" data-id="${id}"
                               ${server.enabled ? 'checked' : ''}>
                        <label class="form-check-label visually-hidden" for="${enabledToggleId}">${server.enabled ? 'Enabled' : 'Disabled'}</label>
                    </div>
                </div>
                <div class="srv-host" title="${escapeHtml(server.url)}">${escapeHtml(server.url)}</div>
                <div class="srv-issue">
                    <span class="server-readiness-glyph d-none"
                          id="${readinessGlyphId}"
                          data-id="${id}"
                          role="button"
                          tabindex="0"></span>
                    <span class="pill bad d-none" id="server-error-${id}"><i class="bi bi-x-circle-fill"></i><span></span></span>
                </div>
                <div class="srv-lib">
                    <span class="srv-lib-num${enabledLibs < libCount ? ' warn' : ''}">${enabledLibs}</span>
                    <span class="srv-lib-sub">of ${libCount} ${libCount === 1 ? 'library' : 'libraries'} enabled</span>
                </div>
                <div class="srv-foot">
                    <span class="srv-hint" id="server-checked-${id}"></span>
                    <button type="button" class="ibtn edit-server-btn" data-id="${id}"
                            aria-label="Edit ${name}" title="Edit">
                        <i class="bi bi-pencil"></i>
                    </button>
                    <button type="button" class="ibtn refresh-libraries-btn" data-id="${id}"
                            aria-label="Refresh libraries for ${name}" title="Refresh libraries">
                        <i class="bi bi-arrow-clockwise"></i>
                    </button>
                    <button type="button" class="ibtn danger delete-server-btn" data-id="${id}"
                            data-name="${name}" aria-label="Delete ${name}" title="Delete">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
            </article>
        `;
    }

    // Below this many servers the list shows roomy rows; from here up it is a grid of cards.
    const ROW_LAYOUT_MAX = 2;
    const VENDOR_NAMES = { plex: 'Plex', emby: 'Emby', jellyfin: 'Jellyfin' };

    function vendorLogoHtml(server) {
        const vendor = (server.type || '').toLowerCase();
        return VENDOR_NAMES[vendor]
            ? `<img src="/static/images/vendors/${escapeHtml(vendor)}.svg" alt="${escapeHtml(server.type)}" width="22" height="22">`
            : '<i class="bi bi-hdd-network"></i>';
    }

    // What this server receives, from the settings the list already carries (no counts: those are not in the data).
    function sendsChipsHtml(server) {
        const vendor = (server.type || '').toLowerCase();
        const enabledLibs = (server.libraries || []).filter((l) => l.enabled).length;
        const chip = (cls, icon, label, on) => `<span class="srv-fchip ${cls}${on ? '' : ' off'}"><i class="bi ${icon}"></i>${label} <span class="srv-fchip-st">${on ? 'on' : 'off'}</span></span>`;
        let html = chip('pv', 'bi-images', 'Previews', enabledLibs > 0)
            + chip('mk', 'bi-skip-forward-fill', 'Intro &amp; Credits', !!(server.markers && server.markers.enabled));
        if (vendor === 'plex') {
            html += chip('ld', 'bi-volume-up', 'Loudness', !!(server.loudness && server.loudness.enabled))
                + chip('ch', 'bi-card-image', 'Chapter thumbnails', !!(server.output && server.output.chapter_thumbnails));
        }
        return `<span class="srv-k">Sends</span>${html}`;
    }

    function libraryChipsHtml(libraries) {
        const shown = libraries.slice(0, 6).map((l) => `<span class="srv-chip${l.enabled ? '' : ' x'}" title="${escapeHtml(l.name || l.id || '')}">${escapeHtml(l.name || l.id || 'unnamed')}</span>`).join('');
        return libraries.length > 6 ? `${shown}<span class="srv-chip more">+${libraries.length - 6} more</span>` : shown;
    }

    function libraryBarHtml(libraries, enabledLibs) {
        const segs = libraries.map((l) => `<i class="${l.enabled ? '' : 'x'}"></i>`).join('');
        return `<div class="srv-segbar${enabledLibs < libraries.length ? ' bad' : ''}" aria-hidden="true">${segs}</div>`;
    }

    // Roomy layout for one or two servers. Same ids, classes and data attributes as serverCard (it is still a
    // .srv-card), plus a column of library chips, a health column and the "Sends" strip.
    function serverRow(server) {
        const libraries = server.libraries || [];
        const libCount = libraries.length;
        const enabledLibs = libraries.filter((l) => l.enabled).length;
        const vendor = (server.type || '').toLowerCase();
        const id = escapeHtml(server.id);
        const name = escapeHtml(server.name);
        return `
            <article class="card srv-card${server.enabled ? '' : ' off'}" data-id="${id}" data-layout="row">
                <div class="srv-r-id">
                    <span class="srv-vlogo srv-vlogo-lg">${vendorLogoHtml(server)}</span>
                    <div class="srv-r-meta">
                        <h2 class="srv-name" title="${name}">${name}</h2>
                        <div class="srv-r-status">
                            <span class="srv-dot" id="server-status-${id}" role="img" aria-label="Checking connection" title="Checking connection"></span>
                            <span id="server-status-text-${id}">Checking…</span>
                            <span class="srv-faint">· ${escapeHtml(VENDOR_NAMES[vendor] || server.type || '')} · <span id="server-checked-${id}"></span></span>
                        </div>
                        <div class="srv-host" title="${escapeHtml(server.url)}">${escapeHtml(server.url)}</div>
                    </div>
                </div>
                <div class="srv-r-libs">
                    <span class="srv-k">Libraries</span>
                    <div class="srv-r-libhead">
                        <span class="srv-lib-num${enabledLibs < libCount ? ' warn' : ''}">${enabledLibs}</span>
                        <span class="srv-lib-sub">of ${libCount} ${libCount === 1 ? 'library' : 'libraries'} enabled</span>
                    </div>
                    ${libraryBarHtml(libraries, enabledLibs)}
                    <div class="srv-chips">${libraryChipsHtml(libraries)}</div>
                </div>
                <div class="srv-r-health">
                    <span class="srv-k">Setup health</span>
                    <div class="srv-issue">
                        <span class="server-readiness-glyph d-none"
                              id="server-readiness-${id}"
                              data-id="${id}"
                              role="button"
                              tabindex="0"></span>
                        <span class="pill bad d-none" id="server-error-${id}"><i class="bi bi-x-circle-fill"></i><span></span></span>
                    </div>
                    <span class="srv-health-note" id="server-health-note-${id}"></span>
                </div>
                <div class="srv-r-acts">
                    <div class="form-check form-switch mb-0" title="Quick enable/disable — when off, this server is ignored by all jobs and webhooks">
                        <input class="form-check-input server-enabled-toggle" type="checkbox"
                               id="server-enabled-${id}" data-id="${id}"
                               ${server.enabled ? 'checked' : ''}>
                        <label class="form-check-label visually-hidden" for="server-enabled-${id}">${server.enabled ? 'Enabled' : 'Disabled'}</label>
                    </div>
                    <div class="srv-r-btns">
                        <button type="button" class="ibtn edit-server-btn" data-id="${id}"
                                aria-label="Edit ${name}" title="Edit">
                            <i class="bi bi-pencil"></i>
                        </button>
                        <button type="button" class="ibtn refresh-libraries-btn" data-id="${id}"
                                aria-label="Refresh libraries for ${name}" title="Refresh libraries">
                            <i class="bi bi-arrow-clockwise"></i>
                        </button>
                        <button type="button" class="ibtn danger delete-server-btn" data-id="${id}"
                                data-name="${name}" aria-label="Delete ${name}" title="Delete">
                            <i class="bi bi-trash"></i>
                        </button>
                    </div>
                </div>
                <div class="srv-r-sends">${sendsChipsHtml(server)}</div>
            </article>
        `;
    }

    function addServerVendorLogos() {
        return ['plex', 'emby', 'jellyfin'].map((v) => `<span class="srv-vlogo srv-vlogo-sm"><img src="/static/images/vendors/${v}.svg" alt="" width="16" height="16"></span>`).join('');
    }

    function addServerTile() {
        return `
            <button type="button" class="srv-addtile" data-bs-toggle="modal" data-bs-target="#addServerModal">
                <span class="srv-plus"><i class="bi bi-plus-lg"></i></span>
                <strong>Add server</strong>
                <span>Connect another Plex, Emby or Jellyfin.</span>
                <span class="srv-vs">${addServerVendorLogos()}</span>
            </button>`;
    }

    function addServerRow() {
        return `
            <button type="button" class="srv-addrow" data-bs-toggle="modal" data-bs-target="#addServerModal">
                <span class="srv-plus"><i class="bi bi-plus-lg"></i></span>
                <span class="srv-addrow-text"><strong>Add another server</strong>Plex, Emby or Jellyfin. Frames are extracted once and reused across servers.</span>
                <span class="srv-vs">${addServerVendorLogos()}</span>
            </button>`;
    }

    // Summary strip above the list: counts come from the server list; the attention count follows the probes.
    function renderServerSummary(servers) {
        const el = document.getElementById('serverSummary');
        if (!el) return;
        if (!servers.length) {
            el.innerHTML = '';
            return;
        }
        const libs = servers.reduce((n, sv) => n + (sv.libraries || []).filter((l) => l.enabled).length, 0);
        el.innerHTML = `<span><b>${servers.length}</b> ${servers.length === 1 ? 'server' : 'servers'}</span>`
            + '<span class="srv-sep"></span>'
            + `<span><b>${libs}</b> ${libs === 1 ? 'library' : 'libraries'} enabled</span>`
            + '<span class="srv-sep"></span>'
            + '<span id="serverSummaryStatus" role="status">Checking servers…</span>';
    }

    function refreshServerSummary() {
        const el = document.getElementById('serverSummaryStatus');
        if (!el) return;
        const cards = $$('#serverList .srv-card').filter((c) => !c.classList.contains('off'));
        const pending = cards.filter((c) => !c.dataset.conn).length;
        const attention = cards.filter((c) => {
            if (c.dataset.conn === 'bad') return true;
            const g = c.querySelector('.server-readiness-glyph');
            return !!g && (g.dataset.state === 'critical' || g.dataset.state === 'recommended');
        }).length;
        if (attention > 0) el.innerHTML = `<b class="srv-warn">${attention}</b> need${attention === 1 ? 's' : ''} attention`;
        else if (pending > 0) el.textContent = 'Checking servers…';
        else el.innerHTML = '<b class="srv-ok">All</b> healthy';
    }

    // Shared by the card's trash button and the Delete button in the Edit modal. Resolves true when deleted.
    async function deleteServerWithConfirm(id, name) {
        if (!await appConfirm(`Delete media server "${name}"? Previews already published to this server stay on disk; this only removes the configuration entry.`, { title: 'Delete media server', confirmText: 'Delete' })) return false;
        const r = await api('DELETE', `/api/servers/${encodeURIComponent(id)}`);
        if (r.ok) {
            if (document.getElementById('serverList')) loadServers();
            return true;
        }
        showToast('Delete failed', `${(r.data && r.data.error) || r.status}`, 'danger');
        return false;
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ---------- Add Server wizard ---------------------------------------------
    const wizard = {
        type: null,
        url: '',
        name: '',
        authMethod: null,
        accessToken: null,
        userId: null,
        apiKey: null,
        plexToken: null,
        plexConfigFolder: null,
        quickConnectSecret: null,
        quickConnectPoll: null,
    };

    function showStep(stepId) {
        $$('.server-step').forEach((s) => s.classList.add('d-none'));
        $('#' + stepId).classList.remove('d-none');
        updateAddHeader(stepId);
    }

    // The modal's header tile, title, subtitle and three-step rail follow the step. Absent on /setup, which inlines
    // only the form.
    const ADD_STEP_INDEX = { 'step-type': 0, 'step-connect': 1, 'step-result': 2 };
    const ADD_CONNECT_SUBTITLES = {
        plex: 'Sign in with Plex, or enter the URL and token',
        emby: 'Enter the URL and a login or API key',
        jellyfin: 'Enter the URL, then use Quick Connect, a login or an API key',
    };

    function updateAddHeader(stepId, { failed = false } = {}) {
        const tile = document.getElementById('addServerLogoTile');
        if (!tile) return;
        const index = ADD_STEP_INDEX[stepId] || 0;
        const vendor = index === 0 ? '' : (wizard.type || '');
        tile.dataset.vendor = vendor;
        tile.innerHTML = vendor
            ? `<img src="/static/images/vendors/${escapeHtml(vendor)}.svg" alt="" width="28" height="28">`
            : '<i class="bi bi-plus-lg"></i>';
        const vendorName = VENDOR_NAMES[vendor] || '';
        const title = document.getElementById('serverModalTitle');
        if (title) title.textContent = vendorName ? `Add ${vendorName} server` : 'Add Server';
        const sub = document.getElementById('serverModalSubtitle');
        if (sub) {
            sub.textContent = index === 0
                ? 'Choose which media server to connect'
                : index === 1
                    ? (ADD_CONNECT_SUBTITLES[vendor] || '')
                    : (failed ? 'Fix the connection and try again' : 'Review the details, then save');
        }
        const rail = document.getElementById('addServerProgress');
        if (rail) {
            rail.style.setProperty('--p', String(index / 2));
            $$('.sm-prog-st', rail).forEach((li, i) => {
                li.classList.toggle('done', i < index);
                li.classList.toggle('on', i === index);
                if (i === index) li.setAttribute('aria-current', 'step');
                else li.removeAttribute('aria-current');
            });
        }
    }

    function resetQuickConnectCard() {
        if (wizard.quickConnectPoll) {
            clearInterval(wizard.quickConnectPoll);
            wizard.quickConnectPoll = null;
        }
        wizard.quickConnectSecret = null;
        wizard.accessToken = null;
        wizard.userId = null;
        const code = document.getElementById('quickConnectCode');
        if (code) {
            code.className = 'sm-qc d-none';
            code.innerHTML = '';
            delete code.dataset.qc;
        }
        const idle = document.getElementById('quickConnectIdle');
        if (idle) idle.classList.remove('d-none');
    }

    function resetWizard() {
        Object.keys(wizard).forEach((k) => { wizard[k] = null; });
        wizard.url = '';
        wizard.name = '';
        // step-type only exists when the modal is on the page (i.e. /servers).
        // The setup wizard inlines just the connection form and supplies its
        // own vendor picker, so guard the call here.
        if (document.getElementById('step-type')) {
            showStep('step-type');
        }
        const titleEl = document.getElementById('serverModalTitle');
        if (titleEl) titleEl.textContent = 'Add Server';
        $('#serverUrl').value = '';
        $('#serverName').value = '';
        $('#authUsername').value = '';
        $('#authPassword').value = '';
        $('#authApiKey').value = '';
        $('#plexToken').value = '';
        $('#plexConfigFolder').value = '';
        resetQuickConnectCard();
        clearFormError();
        $('#authApiKey').type = 'password';
    }

    // Switch the connection form into "connect to <vendor>" mode and reveal
    // step-connect. Used by both the modal's vendor buttons (/servers) and
    // the setup wizard's vendor picker (/setup, via window.MPGShared.pickVendor).
    function pickVendorAndAdvance(vendor) {
        wizard.type = vendor;
        const vendorLabel = document.getElementById('step-connect-vendor');
        if (vendorLabel) {
            vendorLabel.textContent = vendor[0].toUpperCase() + vendor.slice(1);
        }
        showStep('step-connect');
        configureAuthForType(vendor);
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-feature-jump]').forEach((link) => {
            link.addEventListener('click', (event) => {
                event.preventDefault();
                const heading = document.querySelector(link.getAttribute('href'));
                if (heading) { heading.focus({ preventScroll: true }); heading.scrollIntoView({ block: 'start' }); }
            });
        });
        document.querySelectorAll('.processing-section-link').forEach((link) => {
            link.addEventListener('click', (event) => {
                event.preventDefault();
                activateSection(`edit-tab-${link.dataset.section}`);
            });
        });
        const editSectionSelect = document.getElementById('editServerSectionSelect');
        if (editSectionSelect) {
            editSectionSelect.addEventListener('change', () => activateSection(editSectionSelect.value));
            document.querySelectorAll('#editServerModal [data-bs-toggle="tab"]').forEach((tab) => {
                tab.addEventListener('shown.bs.tab', (event) => {
                    editSectionSelect.value = (event.target.dataset.bsTarget || '').replace(/^#/, '');
                    // A section opens at its top, whatever the one before it was scrolled to.
                    const panes = document.querySelector('#editServerModal .sm-panes');
                    if (panes) panes.scrollTop = 0;
                });
            });
        }
        // The Add Server modal is a shared partial included from both
        // /servers and /setup. The /servers-only setup (server list,
        // webhook URL, edit modal) only runs when those elements exist
        // — on /setup we just need the modal wiring below.
        const isServersPage = !!document.getElementById('serverList');

        if (isServersPage) {
            loadServers();
        }

        // Modal-only wiring. /setup inlines the connection form (no modal),
        // so guard these so a missing #addServerModal doesn't throw and
        // break the connection-form button listeners further down.
        const modalEl = document.getElementById('addServerModal');
        if (modalEl) {
            modalEl.addEventListener('show.bs.modal', resetWizard);
            modalEl.addEventListener('hidden.bs.modal', resetWizard);
        }

        // Quick Connect card buttons (Add flow and Edit → Re-authenticate render the same card).
        document.addEventListener('click', (ev) => {
            const copy = ev.target.closest('.sm-qc-copy');
            if (copy) {
                const value = copy.dataset.code || '';
                if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(value).catch(() => {});
                copy.innerHTML = '<i class="bi bi-check2 me-1"></i>Copied';
                return;
            }
            const cancel = ev.target.closest('.sm-qc-cancel');
            const retry = ev.target.closest('.sm-qc-retry');
            const btn = cancel || retry;
            if (!btn) return;
            if (btn.dataset.qcTarget === 'edit') {
                if (_editReauthQcPoll) { clearInterval(_editReauthQcPoll); _editReauthQcPoll = null; }
                const status = document.getElementById('editReauthJfQcStatus');
                if (cancel && status) { status.className = 'sm-status d-none'; status.innerHTML = ''; }
                if (retry) _editReauthStartQuickConnect();
            } else {
                resetQuickConnectCard();
                if (retry) startQuickConnect();
            }
        });

        $$('.server-type-btn').forEach((btn) => {
            btn.addEventListener('click', () => pickVendorAndAdvance(btn.dataset.type));
        });

        // Legacy /servers?add=<vendor> entry point — pre-opens the modal at
        // the connection step. Setup wizard no longer uses this path (it
        // calls window.MPGShared.pickVendor directly via the inline panel),
        // but kept for any deep links / bookmarks that survived the refactor.
        const _addParam = new URLSearchParams(window.location.search).get('add');
        if (_addParam && ['plex', 'emby', 'jellyfin'].includes(_addParam) && modalEl) {
            const _modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            _modal.show();
            setTimeout(() => {
                document.querySelector('.server-type-btn[data-type="' + _addParam + '"]')?.click();
            }, 50);
            window.history.replaceState({}, '', window.location.pathname);
        }

        // step-connect-back returns to step-type — that only exists in the
        // modal. /setup overrides this in its own DOMContentLoaded handler
        // to return to the wizard's vendor picker instead.
        $('#step-connect-back').addEventListener('click', () => {
            if (document.getElementById('step-type')) showStep('step-type');
        });

        $$('input[name="authMethod"]').forEach((radio) => {
            radio.addEventListener('change', () => {
                wizard.authMethod = radio.value;
                renderAuthFields();
            });
        });

        const apiKeyShow = document.getElementById('authApiKeyShow');
        if (apiKeyShow) {
            apiKeyShow.addEventListener('click', () => {
                const input = $('#authApiKey');
                input.type = input.type === 'password' ? 'text' : 'password';
                apiKeyShow.innerHTML = `<i class="bi ${input.type === 'password' ? 'bi-eye' : 'bi-eye-slash'}"></i>`;
            });
        }
        $('#step-connect-test').addEventListener('click', testConnection);
        $('#step-result-back').addEventListener('click', () => showStep('step-connect'));
        $('#step-result-save').addEventListener('click', saveServer);
        $('#quickConnectStart').addEventListener('click', startQuickConnect);
        $('#plexOAuthStart').addEventListener('click', startPlexOAuth);
        // Batch-add was removed (silently mis-pinned config folder on the
        // second server, and forced multi-Plex installs to re-edit every
        // server anyway). One sign-in adds one server; the user signs in
        // again from the next "Add Server" click.

        // Browse button for the Add Server modal's Plex config folder
        // field. The Edit modal already has its own wiring at the bottom
        // of this file (editPlexConfigBrowseBtn → editPlexConfigFolder);
        // this is the matching pair for the partial used in #step-connect
        // and the setup wizard. The button lives inside the shared
        // _server_connection_form partial so both surfaces inherit it,
        // but only the modal needs the click handler bound — the setup
        // wizard hides this partial under #auth-fields-token-plex on
        // /setup (the wizard uses its own wizardPlexConfigFolder block
        // at step 3 with its own browse wiring).
        const plexCfgBrowseBtn = $('#plexConfigFolderBrowseBtn');
        if (plexCfgBrowseBtn) {
            plexCfgBrowseBtn.addEventListener('click', () => {
                const cfgInput = $('#plexConfigFolder');
                if (!cfgInput) return;
                const start = (cfgInput.value || '').trim() || '/';
                window.openFolderPicker(start, (picked) => {
                    cfgInput.value = picked;
                    _validateLocalPathInput(cfgInput);
                });
            });
        }
    });

    // ---------- Plex OAuth + auto-discovery ------------------------------------
    async function startPlexOAuth() {
        const btn = $('#plexOAuthStart');
        btn.disabled = true;
        const origLabel = btn.innerHTML;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Waiting for plex.tv…';

        const auth = new PlexAuth({
            onSuccess: async (token) => {
                btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Discovering servers…';
                wizard.plexToken = token;
                const r = await fetch('/api/plex/servers', {
                    headers: {
                        'X-Plex-Token': token,
                        'X-CSRFToken': csrfToken(),
                    },
                });
                let data = null;
                try { data = await r.json(); } catch (_) { /* */ }
                if (!r.ok || !data || !data.servers) {
                    showFormError('Could not list Plex servers from plex.tv. Try the manual token option below.');
                    btn.disabled = false;
                    btn.innerHTML = origLabel;
                    return;
                }
                renderPlexDiscovered(data.servers);
                btn.disabled = false;
                btn.innerHTML = origLabel;
            },
            onError: (err) => {
                console.error('Plex OAuth failed', err);
                showFormError('Plex OAuth failed: ' + (err.message || err));
                btn.disabled = false;
                btn.innerHTML = origLabel;
            },
            onCancel: () => {
                btn.disabled = false;
                btn.innerHTML = origLabel;
            },
        });

        try {
            const pin = await auth.requestPin();
            auth.openAuthWindow(pin.auth_url);
            // pollForToken resolves with the actual auth_token (not just a
            // boolean) so we can hand it to the wizard's per-server entry
            // builder. The onSuccess callback wired into the constructor
            // above expects the token as its parameter, so invoke it
            // explicitly — pollForToken intentionally does NOT call
            // onSuccess itself (PlexAuth.login() is the wrapper that does;
            // we don't use login() here because we want the in-flight
            // button-state changes around the discovery fetch).
            const token = await auth.pollForToken(pin.id);
            if (token) {
                await auth.onSuccess(token);
            }
        } catch (err) {
            console.error('Plex OAuth flow error', err);
            showFormError('Plex OAuth flow error: ' + (err.message || err));
            btn.disabled = false;
            btn.innerHTML = origLabel;
        }
    }

    // Stash the discovered set so the batch-add path can look up
    // each server's machine_id + uri without re-fetching.
    let plexDiscoveredCache = [];

    function renderPlexDiscovered(servers) {
        const list = $('#plexDiscoveredServers');
        plexDiscoveredCache = servers || [];
        if (servers.length === 0) {
            list.innerHTML = '<div class="text-muted small">No Plex servers found on your account.</div>';
            $('#plexDiscoveredList').classList.remove('d-none');
            return;
        }
        // Radio (not checkbox) — adding more than one Plex server in a
        // single sign-in always required post-add per-server config
        // (config folder, path mappings) anyway, and the old multi-pick
        // path silently cleared the URL field on the second tick which
        // confused every user who saw it. One pick at a time matches
        // the linear flow: pick → Test connection → Save → (sign in
        // again to add another).
        list.innerHTML = servers.map((s, idx) => {
            const ownedBadge = s.owned ? '<span class="pill ok">owned</span>' : '<span class="pill">shared</span>';
            const localBadge = s.local ? '<span class="pill run ms-1">local</span>' : '';
            const sslBadge = s.ssl ? '<span class="pill ms-1">https</span>' : '';
            return `
                <label class="list-group-item d-flex align-items-start gap-2">
                    <input type="radio" name="plexDiscoveredPick" class="form-check-input mt-1 plex-server-pick"
                           data-idx="${idx}"
                           data-uri="${escapeHtml(s.uri || '')}"
                           data-name="${escapeHtml(s.name || '')}"
                           data-machine-id="${escapeHtml(s.machine_id || '')}">
                    <div class="flex-grow-1">
                        <strong>${escapeHtml(s.name || 'Unnamed Plex')}</strong>
                        ${ownedBadge}${localBadge}${sslBadge}
                        <br>
                        <small class="text-muted">${escapeHtml(s.uri || s.host || '')}</small>
                    </div>
                </label>
            `;
        }).join('');
        $('#plexDiscoveredList').classList.remove('d-none');

        $$('.plex-server-pick').forEach((el) => {
            el.addEventListener('change', () => {
                if (!el.checked) return;  // radio "change" fires for the newly-selected one only
                // Always auto-fill: the user picked this server, the
                // form below is now the per-server config page (URL,
                // name pre-filled; config folder + path mappings stay
                // user-controlled). Force-overwrite serverUrl so a
                // stale value from a previous pick doesn't linger.
                $('#serverUrl').value = el.dataset.uri || '';
                if (!$('#serverName').value) $('#serverName').value = el.dataset.name || '';
                // Surface the test-connection CTA visually: scroll it
                // into view so a user on a tall list doesn't have to
                // hunt for "what's next?" after picking.
                const testBtn = document.getElementById('step-connect-test');
                if (testBtn && testBtn.scrollIntoView) {
                    testBtn.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
            });
        });
    }

    const PORT_HINTS = { plex: ':32400', emby: ':8096', jellyfin: ':8096' };

    function configureAuthForType(type) {
        const methodSection = $('#auth-method-section');
        const plexConfig = document.getElementById('auth-fields-plex-config');
        const trickplayNote = document.getElementById('auth-jellyfin-trickplay-note');
        const portHint = document.getElementById('serverUrlPortHint');
        if (portHint && PORT_HINTS[type]) portHint.innerHTML = `Default port: <code>${PORT_HINTS[type]}</code>`;
        if (trickplayNote) trickplayNote.classList.toggle('d-none', type !== 'jellyfin');
        if (type === 'plex') {
            methodSection.classList.add('d-none');
            wizard.authMethod = 'token';
            $('#auth-fields-token-plex').classList.remove('d-none');
            if (plexConfig) plexConfig.classList.remove('d-none');
            $('#auth-fields-password').classList.add('d-none');
            $('#auth-fields-api-key').classList.add('d-none');
            $('#auth-fields-quick-connect').classList.add('d-none');
        } else {
            methodSection.classList.remove('d-none');
            if (plexConfig) plexConfig.classList.add('d-none');
            // Pick a sensible default per vendor.
            const defaultMethod = type === 'jellyfin' ? 'quick_connect' : 'password';
            wizard.authMethod = defaultMethod;
            $$('input[name="authMethod"]').forEach((r) => {
                r.checked = r.value === defaultMethod;
            });
            // Quick Connect only makes sense on Jellyfin — hide just
            // its radio + its label, NOT the parent .btn-group.
            // Issue #247: the previous ``.parentElement`` toggle hid
            // the WHOLE auth-method picker (Quick Connect + Password +
            // API Key) for Emby, leaving the user stuck on the default
            // Password method with no way to reach API Key.
            const hideQuick = type !== 'jellyfin';
            $('#auth-quick').classList.toggle('d-none', hideQuick);
            $$('label[for=auth-quick]').forEach((l) => l.classList.toggle('d-none', hideQuick));
            renderAuthFields();
        }
    }

    function renderAuthFields() {
        $('#auth-fields-password').classList.toggle('d-none', wizard.authMethod !== 'password');
        $('#auth-fields-api-key').classList.toggle('d-none', wizard.authMethod !== 'api_key');
        $('#auth-fields-quick-connect').classList.toggle('d-none', wizard.authMethod !== 'quick_connect');
        $('#auth-fields-token-plex').classList.toggle('d-none', wizard.authMethod !== 'token');
        const plexConfig = document.getElementById('auth-fields-plex-config');
        if (plexConfig) plexConfig.classList.toggle('d-none', wizard.authMethod !== 'token');
    }

    async function startQuickConnect() {
        const url = $('#serverUrl').value.trim();
        if (!url) { markFieldInvalid($('#serverUrl'), 'Enter the Jellyfin URL first.'); return; }
        const card = $('#quickConnectCode');
        const r = await api('POST', '/api/servers/auth/jellyfin/quick-connect/initiate', { url });
        if (!r.ok || !r.data || !r.data.ok) {
            card.className = 'sm-status bad';
            card.textContent = (r.data && r.data.message) || 'Quick Connect failed';
            return;
        }
        wizard.quickConnectSecret = r.data.secret;
        // D22 — auto-open the Jellyfin Quick Connect entry page in a
        // new tab so the user doesn't have to navigate manually. Best-
        // effort: popup blockers may refuse, in which case the inline
        // instruction below still tells them where to go. Strip any
        // trailing slash on the base URL so we don't end up with
        // /web//#/quickconnect.
        const baseUrl = url.replace(/\/+$/, '');
        const qcUrl = baseUrl + '/web/#/quickconnect';
        try { window.open(qcUrl, '_blank', 'noopener,noreferrer'); } catch (_) { /* blocked */ }
        const idle = document.getElementById('quickConnectIdle');
        if (idle) idle.classList.add('d-none');
        card.className = 'sm-qc';
        renderQuickConnect(card, 'waiting', { code: r.data.code, url: qcUrl, target: 'add' });

        // Poll every 2 seconds.
        if (wizard.quickConnectPoll) clearInterval(wizard.quickConnectPoll);
        const startedAt = Date.now();
        wizard.quickConnectPoll = setInterval(async () => {
            const p = await api('POST', '/api/servers/auth/jellyfin/quick-connect/poll',
                { url, secret: wizard.quickConnectSecret });
            if (p.ok && p.data && p.data.authenticated) {
                clearInterval(wizard.quickConnectPoll);
                wizard.quickConnectPoll = null;
                const e = await api('POST', '/api/servers/auth/jellyfin/quick-connect/exchange',
                    { url, secret: wizard.quickConnectSecret });
                if (e.ok && e.data && e.data.ok) {
                    wizard.accessToken = e.data.access_token;
                    wizard.userId = e.data.user_id;
                    renderQuickConnect(card, 'approved', {
                        code: r.data.code, url: qcUrl, target: 'add',
                        who: e.data.server_name || 'Jellyfin user', after: 'Continue with Test connection.',
                    });
                } else {
                    card.className = 'sm-status bad';
                    card.textContent = (e.data && e.data.message) || 'Token exchange failed';
                }
            } else if (quickConnectExpired(p, startedAt)) {
                clearInterval(wizard.quickConnectPoll);
                wizard.quickConnectPoll = null;
                renderQuickConnect(card, 'expired', { code: r.data.code, url: qcUrl, target: 'add' });
            }
        }, 2000);
    }

    async function testConnection() {
        clearFieldErrors('#step-connect');
        clearFormError();
        wizard.url = $('#serverUrl').value.trim();
        wizard.name = $('#serverName').value.trim();
        let firstBad = null;
        if (!wizard.url) { markFieldInvalid($('#serverUrl')); firstBad = $('#serverUrl'); }
        if (!wizard.name) {
            markFieldInvalid($('#serverName'));
            if (!firstBad) firstBad = $('#serverName');
        }
        if (firstBad) { firstBad.focus(); return; }

        // Build auth based on method.
        const auth = await buildAuth();
        if (!auth) return;  // helper already surfaced an inline error

        const payload = {
            type: wizard.type,
            name: wizard.name,
            url: wizard.url,
            auth,
        };
        if (wizard.type === 'plex') {
            payload.output = {
                adapter: 'plex_bundle',
                plex_config_folder: $('#plexConfigFolder').value.trim(),
                frame_interval: 10,
            };
        }

        const r = await api('POST', '/api/servers/test-connection', payload);
        const result = $('#connectResult');
        const connected = !!(r.ok && r.data && r.data.ok);
        if (connected) {
            result.className = 'sm-res';
            result.innerHTML = connectResultSuccessHtml(payload, r.data);
            // The wizard used to render an inline "Jellyfin trickplay
            // disabled" warning + "Fix it for me" button here. The
            // button's only side-effect was setting a `_pendingTrickplayFix`
            // attribute that nothing read, so the user got an empty
            // promise that the fix would land "after Save". The
            // unified Server health-check panel on the Edit-Server
            // modal (post-save) covers this case for every vendor and
            // every flag — so we drop the misleading wizard surfacing.
        } else {
            result.className = 'sm-res';
            result.innerHTML = connectResultFailureHtml(payload, (r.data && r.data.message) || 'Connection failed');
        }
        wizard._lastTestPayload = payload;
        showStep('step-result');
        updateAddHeader('step-result', { failed: !connected });
    }

    function connectResultSuccessHtml(payload, data) {
        const signIn = {
            token: 'Plex sign-in (token stored)',
            api_key: 'API key (stored)',
            password: 'Login (access token stored)',
            quick_connect: 'Quick Connect (access token stored)',
        }[(payload.auth || {}).method] || 'Credentials stored';
        const folder = (payload.output || {}).plex_config_folder;
        return `
            <div class="sm-res-banner ok" id="connectResultMsg"><i class="bi bi-check-circle-fill"></i>
                <div class="sm-res-msg"><b>Connected to <strong>${escapeHtml(data.server_name || wizard.name)}</strong>${data.version ? ' (v' + escapeHtml(data.version) + ')' : ''}.</b>
                    <div class="sm-hint">Saving enables the server and opens its Setup Health check.</div></div></div>
            <dl class="sm-res-dl">
                <dt>Display name</dt><dd>${escapeHtml(payload.name)}</dd>
                <dt>Server URL</dt><dd><code>${escapeHtml(payload.url)}</code></dd>
                <dt>Sign-in</dt><dd>${escapeHtml(signIn)}</dd>
                ${folder ? `<dt>Config folder</dt><dd><code>${escapeHtml(folder)}</code></dd>` : ''}
            </dl>`;
    }

    function connectResultFailureHtml(payload, message) {
        const port = PORT_HINTS[payload.type] || '';
        return `
            <div class="sm-res-banner bad" id="connectResultMsg"><i class="bi bi-x-octagon-fill"></i>
                <div class="sm-res-msg"><b>Could not connect</b><div class="sm-hint">${escapeHtml(message)}</div></div></div>
            <div class="sm-card"><div class="sm-card-b sm-res-tips">
                <b>Check these first</b>
                <span class="sm-hint">Is the URL reachable from inside this container? <code>localhost</code> is the container itself.</span>
                <span class="sm-hint">Is the port right (${escapeHtml(port)} by default)?</span>
                <span class="sm-hint">Self-signed HTTPS? Turn off certificate verification after saving.</span>
            </div></div>`;
    }

    async function buildAuth() {
        if (wizard.type === 'plex') {
            // Prefer the OAuth-derived token when present; fall back to manual.
            const tok = wizard.plexToken || $('#plexToken').value.trim();
            if (!tok) {
                markFieldInvalid($('#plexToken'), 'Sign in with Plex or paste a token.');
                return null;
            }
            return { method: 'token', token: tok };
        }
        if (wizard.authMethod === 'api_key') {
            const k = $('#authApiKey').value.trim();
            if (!k) { markFieldInvalid($('#authApiKey')); return null; }
            return { method: 'api_key', api_key: k };
        }
        if (wizard.authMethod === 'password') {
            const u = $('#authUsername').value.trim();
            const p = $('#authPassword').value;
            if (!u) { markFieldInvalid($('#authUsername')); return null; }
            const endpoint = wizard.type === 'jellyfin'
                ? '/api/servers/auth/jellyfin/password'
                : '/api/servers/auth/emby/password';
            const r = await api('POST', endpoint, { url: wizard.url, username: u, password: p });
            if (!r.ok || !r.data || !r.data.ok) {
                // Common cause: wrong URL (or URL unreachable from this
                // container — e.g. user typed `http://localhost:8096` but
                // Jellyfin is on a docker bridge). The backend message is
                // usually specific enough; pass it through verbatim.
                const msg = (r.data && r.data.message)
                    || `Authentication failed (HTTP ${r.status}). Check the username, password, and that the URL is reachable from this container.`;
                showFormError(msg);
                return null;
            }
            return {
                method: 'password',
                access_token: r.data.access_token,
                user_id: r.data.user_id,
            };
        }
        if (wizard.authMethod === 'quick_connect') {
            if (!wizard.accessToken) {
                showFormError('Complete Quick Connect first — open Jellyfin and approve the code shown above.');
                return null;
            }
            return {
                method: 'quick_connect',
                access_token: wizard.accessToken,
                user_id: wizard.userId,
            };
        }
        return null;
    }

    async function saveServer() {
        const payload = wizard._lastTestPayload;
        if (!payload) {
            showToast('Run the connection test first', 'Use the Test connection button before saving.', 'warning');
            return;
        }
        // Modal only exists on /servers; /setup inlines the form.
        const addModalEl = document.getElementById('addServerModal');
        const opening = modalOpening(addModalEl);
        const r = await api('POST', '/api/servers', payload);
        if (r.ok) {
            hideModalSafely(addModalEl, opening);
            // Notify any listening page (the setup wizard subscribes to this
            // so it can advance from step 1 → GPU/security after an
            // Emby/Jellyfin add). Always fires; /servers ignores it.
            document.dispatchEvent(new CustomEvent('mediaServerAdded', {
                detail: { server: r.data, type: payload.type },
            }));
            if (typeof loadServers === 'function' && document.getElementById('serverList')) {
                loadServers();
            }
        } else {
            const msg = (r.data && r.data.error) || `HTTP ${r.status}`;
            showFormError(`Failed to save server: ${msg}`);
        }
    }

    // ---------- Edit Server modal --------------------------------------------
    // Opens a separate modal pre-populated from GET /api/servers/<id> and
    // submits via PUT /api/servers/<id>. Path mappings + exclude paths get
    // an "Apply to all servers" button that PUTs the same list to every
    // other configured server (one click instead of N).

    let _editState = null;  // { server, allServers }
    // D24 — Quick Connect poll handle for the Edit-modal flow. Distinct
    // from the wizard's `wizard.quickConnectPoll` so opening Edit while
    // the Add wizard is mid-flight doesn't stomp the wizard's poll.
    let _editReauthQcPoll = null;
    let _editReauthQcSecret = null;

    // Render the Emby/Jellyfin webhook info card on the per-server Edit
    // modal's "Webhook & Scanner" tab. Pre-fix the tab was Plex-only;
    // Emby and Jellyfin users had no place to find the webhook URL their
    // plugin should POST to. The backend at
    // /api/settings/<vendor>_webhook/info returns the URL + plugin
    // install instructions; this function paints them.
    async function _renderVendorWebhookSection(server) {
        const card = document.getElementById('editVendorWebhookCard');
        if (!card) return;
        const t = String(server.type || '').toLowerCase();
        if (t !== 'emby' && t !== 'jellyfin') {
            card.classList.add('d-none');
            return;
        }
        card.classList.remove('d-none');

        const headerEl = document.getElementById('editVendorWebhookHeader');
        const urlInput = document.getElementById('editVendorWebhookUrl');
        const headerNameEl = document.getElementById('editVendorWebhookHeaderName');
        const hintEl = document.getElementById('editVendorWebhookPluginHint');
        const stepsEl = document.getElementById('editVendorWebhookSteps');
        const copyBtn = document.getElementById('editVendorWebhookCopyBtn');

        if (headerEl) headerEl.textContent = (t === 'emby' ? 'Emby' : 'Jellyfin') + ' Webhook';
        if (urlInput) urlInput.value = 'Loading…';
        if (hintEl) hintEl.textContent = '';
        if (stepsEl) stepsEl.innerHTML = '';

        let info;
        try {
            const url = '/api/settings/' + t + '_webhook/info?server_id=' + encodeURIComponent(server.id);
            info = await apiGet(url);
        } catch (err) {
            if (urlInput) urlInput.value = '';
            if (hintEl) {
                hintEl.className = 'sm-warnline bad';
                hintEl.textContent = 'Could not load webhook info: ' + (err && err.message || err);
            }
            return;
        }

        if (urlInput) urlInput.value = info.webhook_url_per_server || info.webhook_url || '';
        // Header name is static prose (X-Auth-Token) — the API confirms
        // it but never returns the value. We deliberately don't expose
        // the actual token in this UI: the user looks it up under
        // Settings → Authentication and pastes it into their plugin.
        if (headerNameEl) headerNameEl.textContent = info.auth_header_name || 'X-Auth-Token';
        if (hintEl) {
            hintEl.className = 'sm-hint';
            const plugin = info.plugin || {};
            const installLink = plugin.install_url
                ? ` <a href="${plugin.install_url}" target="_blank" rel="noopener">${escapeHtml(plugin.plugin_name || 'plugin')} install instructions ↗</a>`
                : '';
            hintEl.innerHTML = '<i class="bi bi-info-circle me-1"></i>' +
                'You need the ' + escapeHtml(plugin.plugin_name || 'webhook plugin') +
                ' configured on your ' + escapeHtml(t === 'emby' ? 'Emby' : 'Jellyfin') +
                ' server.' + installLink;
        }
        if (stepsEl) {
            const steps = (info.plugin || {}).config_steps || [];
            stepsEl.innerHTML = steps.map(s => '<li>' + escapeHtml(s) + '</li>').join('');
            const stepsSummary = document.getElementById('editVendorWebhookStepsSummary');
            if (stepsSummary) stepsSummary.textContent = steps.length ? `Setup steps (${steps.length})` : 'Setup steps';
        }

        if (copyBtn && urlInput) {
            copyBtn.onclick = () => {
                const value = urlInput.value;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(value).catch(() => {});
                } else {
                    urlInput.select();
                    try { document.execCommand('copy'); } catch (_) {}
                }
                copyBtn.innerHTML = '<i class="bi bi-check2"></i>';
                setTimeout(() => { copyBtn.innerHTML = '<i class="bi bi-clipboard"></i>'; }, 1500);
            };
        }
    }

    function _resetEditReauthSection(serverType) {
        const t = String(serverType || '').toLowerCase();
        const plex = document.getElementById('editReauthPlex');
        const jf = document.getElementById('editReauthJellyfin');
        const emby = document.getElementById('editReauthEmby');
        if (!plex || !jf || !emby) return;
        plex.classList.toggle('d-none', t !== 'plex');
        jf.classList.toggle('d-none', t !== 'jellyfin');
        emby.classList.toggle('d-none', t !== 'emby');

        // Reset all inputs / pending state so a previous Edit's
        // values don't leak across.
        const resetIds = [
            'editReauthPlexToken', 'editReauthJfUsername', 'editReauthJfPassword',
            'editReauthJfApiKey', 'editReauthEmbyUsername', 'editReauthEmbyPassword',
            'editReauthEmbyApiKey', 'editReauthPending',
        ];
        resetIds.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.value = '';
        });
        ['editReauthJfQcStatus', 'editReauthJfPwStatus', 'editReauthEmbyPwStatus'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) {
                el.innerHTML = '';
                el.className = 'sm-status d-none mt-2';
            }
        });
        if (_editReauthQcPoll) {
            clearInterval(_editReauthQcPoll);
            _editReauthQcPoll = null;
        }
        _editReauthQcSecret = null;
        // Reset method radios to defaults.
        const jfDefault = document.getElementById('editReauthJfQc');
        if (jfDefault) jfDefault.checked = true;
        const embyDefault = document.getElementById('editReauthEmbyPw');
        if (embyDefault) embyDefault.checked = true;
        _onEditReauthMethodChange();
    }

    function _onEditReauthMethodChange() {
        const jfMethod = (document.querySelector('input[name="editReauthJfMethod"]:checked') || {}).value;
        const showJf = (m) => (jfMethod === m ? '' : 'd-none');
        const jfQc = document.getElementById('editReauthJfFieldsQc');
        const jfPw = document.getElementById('editReauthJfFieldsPw');
        const jfKey = document.getElementById('editReauthJfFieldsKey');
        if (jfQc) jfQc.className = showJf('quick_connect');
        if (jfPw) jfPw.className = showJf('password');
        if (jfKey) jfKey.className = showJf('api_key');

        const embyMethod = (document.querySelector('input[name="editReauthEmbyMethod"]:checked') || {}).value;
        const showEmby = (m) => (embyMethod === m ? '' : 'd-none');
        const embyPw = document.getElementById('editReauthEmbyFieldsPw');
        const embyKey = document.getElementById('editReauthEmbyFieldsKey');
        if (embyPw) embyPw.className = showEmby('password');
        if (embyKey) embyKey.className = showEmby('api_key');
    }

    function _readEditReauthPayload(server) {
        const t = String(server && server.type || '').toLowerCase();
        // Prefer the JS-stashed verified payload (for Quick Connect /
        // password flows that already round-tripped through the auth
        // endpoint). Falls through to direct field reads for token /
        // api_key paste paths that don't need server-side verification.
        const pendingRaw = (document.getElementById('editReauthPending') || {}).value || '';
        if (pendingRaw) {
            try { return JSON.parse(pendingRaw); } catch (_) { /* fall through */ }
        }
        if (t === 'plex') {
            const tok = (document.getElementById('editReauthPlexToken') || {}).value || '';
            if (!tok.trim()) return null;
            return { method: 'token', token: tok.trim() };
        }
        if (t === 'jellyfin' || t === 'emby') {
            const radioName = t === 'jellyfin' ? 'editReauthJfMethod' : 'editReauthEmbyMethod';
            const method = (document.querySelector('input[name="' + radioName + '"]:checked') || {}).value;
            if (method === 'api_key') {
                const idPrefix = t === 'jellyfin' ? 'editReauthJf' : 'editReauthEmby';
                const k = (document.getElementById(idPrefix + 'ApiKey') || {}).value || '';
                if (!k.trim()) return null;
                return { method: 'api_key', api_key: k.trim() };
            }
            // password and quick_connect flows write the validated
            // {method, access_token, user_id} payload into
            // #editReauthPending when their respective Verify / Approve
            // step succeeds. If pending is empty, nothing to send.
            return null;
        }
        return null;
    }

    async function _editReauthVerifyPassword(vendor) {
        const url = ($('#editServerUrl').value || '').trim();
        if (!url) { showToast('URL required', 'Enter the server URL first.', 'warning'); return; }
        const idPrefix = vendor === 'jellyfin' ? 'editReauthJf' : 'editReauthEmby';
        const u = (document.getElementById(idPrefix + 'Username') || {}).value.trim();
        const p = (document.getElementById(idPrefix + 'Password') || {}).value;
        const status = document.getElementById(idPrefix + 'PwStatus');
        if (!u) { showToast('Username required', '', 'warning'); return; }
        const endpoint = vendor === 'jellyfin'
            ? '/api/servers/auth/jellyfin/password'
            : '/api/servers/auth/emby/password';
        if (status) {
            status.className = 'sm-status mt-2';
            status.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Verifying…';
        }
        const r = await api('POST', endpoint, { url, username: u, password: p });
        if (!r.ok || !r.data || !r.data.ok) {
            if (status) {
                status.className = 'sm-status bad mt-2';
                status.textContent = (r.data && r.data.message) || `Auth failed (HTTP ${r.status})`;
            }
            return;
        }
        const payload = {
            method: 'password',
            access_token: r.data.access_token,
            user_id: r.data.user_id,
        };
        document.getElementById('editReauthPending').value = JSON.stringify(payload);
        setEditDirty(true);
        if (status) {
            status.className = 'sm-status ok mt-2';
            status.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Verified — click <strong>Save changes</strong> to apply.';
        }
    }

    async function _editReauthStartQuickConnect() {
        const url = ($('#editServerUrl').value || '').trim();
        if (!url) { showToast('URL required', 'Enter the Jellyfin URL first.', 'warning'); return; }
        const status = document.getElementById('editReauthJfQcStatus');
        const r = await api('POST', '/api/servers/auth/jellyfin/quick-connect/initiate', { url });
        if (!r.ok || !r.data || !r.data.ok) {
            status.className = 'sm-status bad';
            status.textContent = (r.data && r.data.message) || 'Quick Connect failed';
            return;
        }
        _editReauthQcSecret = r.data.secret;
        const baseUrl = url.replace(/\/+$/, '');
        const qcUrl = baseUrl + '/web/#/quickconnect';
        try { window.open(qcUrl, '_blank', 'noopener,noreferrer'); } catch (_) { /* blocked */ }
        status.className = 'sm-qc sm-qc-edit';
        renderQuickConnect(status, 'waiting', { code: r.data.code, url: qcUrl, target: 'edit' });

        if (_editReauthQcPoll) clearInterval(_editReauthQcPoll);
        const startedAt = Date.now();
        _editReauthQcPoll = setInterval(async () => {
            const p = await api('POST', '/api/servers/auth/jellyfin/quick-connect/poll',
                { url, secret: _editReauthQcSecret });
            if (p.ok && p.data && p.data.authenticated) {
                clearInterval(_editReauthQcPoll);
                _editReauthQcPoll = null;
                const e = await api('POST', '/api/servers/auth/jellyfin/quick-connect/exchange',
                    { url, secret: _editReauthQcSecret });
                if (e.ok && e.data && e.data.ok) {
                    document.getElementById('editReauthPending').value = JSON.stringify({
                        method: 'quick_connect',
                        access_token: e.data.access_token,
                        user_id: e.data.user_id,
                    });
                    setEditDirty(true);
                    renderQuickConnect(status, 'approved', {
                        code: r.data.code, url: qcUrl, target: 'edit',
                        who: e.data.server_name || 'Jellyfin user', after: 'Click Save changes to apply.',
                    });
                } else {
                    status.className = 'sm-status bad';
                    status.textContent = (e.data && e.data.message) || 'Token exchange failed';
                }
            } else if (quickConnectExpired(p, startedAt)) {
                clearInterval(_editReauthQcPoll);
                _editReauthQcPoll = null;
                renderQuickConnect(status, 'expired', { code: r.data.code, url: qcUrl, target: 'edit' });
            }
        }, 2000);
    }

    // ---------- Quick Connect card (Add flow and Edit → Re-authenticate share it) ------------------------------
    const QC_LIFETIME_MS = 5 * 60 * 1000;

    function quickConnectExpired(pollResponse, startedAt) {
        const msg = String((pollResponse && pollResponse.data && pollResponse.data.message) || '');
        return /expired|not found/i.test(msg) || Date.now() - startedAt > QC_LIFETIME_MS;
    }

    // Fills `el` (which already carries .sm-qc) for one of the three live states: waiting, approved, expired.
    function renderQuickConnect(el, state, { code = '', url = '', target = 'add', who = '', after = '' } = {}) {
        el.classList.remove('d-none');
        el.dataset.qc = state;
        const link = url
            ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">Jellyfin Quick Connect</a>`
            : 'Jellyfin Quick Connect';
        const pills = {
            waiting: '<span class="pill run"><span class="sm-spin"></span> Waiting for approval</span>',
            approved: '<span class="pill ok"><i class="bi bi-check-circle-fill"></i> Approved</span>',
            expired: '<span class="pill bad"><i class="bi bi-clock-history"></i> Code expired</span>',
        };
        const messages = {
            waiting: `Opened ${link} in a new tab — log in if needed, then enter this code in your Jellyfin profile menu.`,
            approved: `<i class="bi bi-check2-circle me-1"></i>Approved as ${escapeHtml(who || 'Jellyfin user')}. ${escapeHtml(after)}`,
            expired: 'The code timed out after 5 minutes. Get a new one.',
        };
        const copy = state === 'expired' ? '' : `<button type="button" class="btn btn-sm btn-outline-secondary sm-qc-copy" data-code="${escapeHtml(code)}"><i class="bi bi-clipboard me-1"></i>Copy</button>`;
        const actions = state === 'waiting'
            ? `<button type="button" class="btn btn-sm btn-outline-secondary sm-qc-cancel" data-qc-target="${target}">Cancel</button>`
            : state === 'expired'
                ? `<button type="button" class="btn btn-sm btn-primary sm-qc-retry" data-qc-target="${target}"><i class="bi bi-arrow-repeat me-1"></i>Get a new code</button>`
                  + `<button type="button" class="btn btn-sm btn-outline-secondary sm-qc-cancel" data-qc-target="${target}">Cancel</button>`
                : '';
        el.innerHTML = `
            <div class="sm-qc-row"><div class="sm-qc-code" aria-label="Quick Connect code">${escapeHtml(code)}</div>${copy}</div>
            <div class="sm-hint">${messages[state]}</div>
            <div class="sm-qc-foot">${pills[state]}<span>${actions}</span></div>`;
    }

    // ---------- Edit modal: dirty state, results, header, badges ----------------------------------------------
    // Everything the Save button sends is edited in this one dialog, so one flag covers all seven sections.
    let _editDirty = false;
    let _editDiscardOk = false;

    function setEditDirty(dirty) {
        _editDirty = !!dirty;
        const save = document.getElementById('editServerSave');
        if (save && !save.dataset.busy) save.disabled = !_editDirty;
        const flag = document.getElementById('editServerDirty');
        if (flag) flag.classList.toggle('d-none', !_editDirty);
        if (!_editDirty) {
            const bar = document.getElementById('editServerDiscardBar');
            if (bar) bar.classList.add('d-none');
        }
    }

    // `kind` is bad | ok | warn; empty text hides the line.
    function showEditResult(kind, html) {
        const el = document.getElementById('editServerResult');
        if (!el) return;
        el.className = html ? kind : 'd-none';
        el.innerHTML = html || '';
    }

    // The same call backs the rail, the phone strip and the (hidden) section select.
    function activateSection(paneId) {
        const trigger = document.querySelector(`#editServerModal [data-bs-target="#${paneId}"]`);
        if (trigger && window.bootstrap) window.bootstrap.Tab.getOrCreateInstance(trigger).show();
    }

    function setEditStatusPill(tone, text) {
        const pill = document.getElementById('editServerStatusPill');
        if (!pill) return;
        if (!text) {
            pill.classList.add('d-none');
            return;
        }
        pill.className = `pill ${tone}`.trim();
        pill.querySelector('.sm-pill-text').textContent = text;
    }

    function setNavBadge(id, text, tone) {
        const el = document.getElementById(id);
        if (!el) return;
        if (text === '' || text == null) {
            el.classList.add('d-none');
            el.textContent = '';
            return;
        }
        el.className = `sm-nb ${tone || ''}`.trim();
        el.textContent = text;
    }

    function updateEditBadges() {
        const toggles = $$('#editLibraryList .edit-lib-toggle');
        if (toggles.length) {
            const on = toggles.filter((t) => t.checked).length;
            setNavBadge('editLibrariesTabBadge', `${on}/${toggles.length}`, on < toggles.length ? 'warn' : '');
        } else {
            setNavBadge('editLibrariesTabBadge', '');
        }
        const maps = readPathMappingsFromForm().length;
        setNavBadge('editPathsTabBadge', maps > 0 ? String(maps) : '');
        const rules = readExcludePathsFromForm().length;
        setNavBadge('editExcludesTabBadge', rules > 0 ? String(rules) : '');
    }

    // Grey until something is set up, green when a Plex webhook is registered or a scanner exists.
    function updateAutomationDot() {
        const dot = document.getElementById('editAutomationTabDot');
        if (!dot) return;
        const registered = ((document.getElementById('plexWebhookStatusBadge') || {}).textContent || '').trim() === 'Registered';
        const scanner = /active/i.test(((document.getElementById('recentlyAddedStatusBadge') || {}).textContent || ''));
        dot.className = `sm-nb sm-nb-dot${registered || scanner ? ' ok' : ''}`;
        dot.title = registered ? 'Webhook registered' : scanner ? 'Scanner active' : 'Nothing set up yet';
    }

    function renderEditCredentials(server) {
        const method = String((server.auth || {}).method || '').toLowerCase();
        const who = {
            token: 'Signed in with a Plex token',
            api_key: 'Using an API key',
            password: 'Signed in with a login',
            quick_connect: 'Signed in with Quick Connect',
        }[method] || 'Credentials stored';
        const hint = method === 'password' || method === 'quick_connect'
            ? 'Access token stored; the password is never saved.'
            : 'Stored; never shown in full.';
        $('#editCredWho').textContent = who;
        $('#editCredHint').textContent = hint;
        setCredFormsOpen(false);
    }

    function setCredFormsOpen(open) {
        const forms = document.getElementById('editReauthSection');
        const btn = document.getElementById('editCredToggle');
        if (forms) forms.classList.toggle('d-none', !open);
        if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    // The URL is only checked here for a missing scheme; Test Connection does the real check.
    function validateEditUrl() {
        const input = document.getElementById('editServerUrl');
        const msg = document.getElementById('editServerUrlMsg');
        if (!input || !msg) return;
        const value = input.value.trim();
        const bad = value !== '' && !/^https?:\/\//i.test(value);
        input.classList.toggle('is-invalid', bad);
        msg.textContent = bad ? 'Start the address with http:// or https://' : '';
        msg.classList.toggle('d-none', !bad);
    }

    function renderEditHeader(server) {
        $('#editServerName').textContent = server.name || '';
        // Long server names used to wrap the title onto two lines; the CSS truncates, and the full name stays on hover.
        const nameWrap = $('#editServerNameWrap');
        if (nameWrap) nameWrap.title = `Edit ${server.name || ''}`.trim();
        const host = $('#editServerHostLine');
        if (host) {
            host.textContent = server.url || '';
            host.title = server.url || '';
        }
        const vendorLogo = $('#editServerVendorLogo');
        if (vendorLogo) {
            const t = (server.type || '').toLowerCase();
            if (VENDOR_NAMES[t]) {
                vendorLogo.src = `/static/images/vendors/${t}.svg`;
                vendorLogo.alt = t;
                vendorLogo.classList.remove('d-none');
            } else {
                vendorLogo.classList.add('d-none');
            }
        }
        setEditStatusPill('', server.enabled === false ? 'Disabled' : '');
        $('#editServerModal').classList.toggle('sm-paused', server.enabled === false);
    }

    async function openEditModal(serverId, { openTab = 'general' } = {}) {
        // Fetch the target server + the full server list (needed for the
        // "Apply to all" buttons so we know who to copy to).
        //
        // openTab — which tab to land on. Defaults to 'general' (the
        // historical behaviour). The /servers card glyph passes 'health'
        // so a user clicking a ⚠/❗ lands exactly on the fix-it UI.
        const [singleR, listR] = await Promise.all([
            api('GET', `/api/servers/${encodeURIComponent(serverId)}`),
            api('GET', '/api/servers'),
        ]);
        if (!singleR.ok || !singleR.data) {
            showToast('Failed to load server', `HTTP ${singleR.status}`, 'danger');
            return;
        }
        const server = singleR.data;
        const allServers = (listR.ok && listR.data && listR.data.servers) || [];
        _editState = { server, allServers };

        renderEditHeader(server);
        $('#editServerId').value = server.id || '';
        $('#editServerType').value = server.type || '';
        $('#editServerDisplayName').value = server.name || '';
        $('#editServerUrl').value = server.url || '';
        $('#editServerVerifySsl').checked = server.verify_ssl !== false;
        $('#editServerEnabled').checked = server.enabled !== false;
        // Reset the test-connection result so a stale "Connected" from
        // the previous Edit doesn't carry over.
        const tcResult = document.getElementById('editTestConnectionResult');
        if (tcResult) {
            tcResult.className = 'sm-tres';
            tcResult.textContent = '';
        }
        validateEditUrl();
        renderEditCredentials(server);
        setEditDirty(false);
        _editDiscardOk = false;
        // Unified "Previews readiness" card — one probe per modal open.
        // Fire-and-forget so the modal opens instantly; the card renders
        // itself when the probe returns. Disabled servers MUST NOT be
        // probed (we'd wake a server the user explicitly paused); show a
        // static "Disabled" state instead and let the user re-enable
        // before running checks.
        if (server.enabled === false) {
            _renderReadinessDisabled(server.id);
        } else {
            runReadinessProbe(server.id, server.type || '');
        }

        // D24 — vendor-aware re-auth UI: show ONE block matching the
        // server's type, hide the others, and reset all input state so
        // an old value from a previous Edit doesn't leak across.
        _resetEditReauthSection(server.type || '');

        // Plex-only: show the config folder field + wire its inline validator.
        const isPlex = (server.type || '').toLowerCase() === 'plex';
        $('#editPlexConfigGroup').classList.toggle('d-none', !isPlex);
        $('#processingChapters').classList.toggle('d-none', !isPlex);
        document.querySelectorAll('.processing-plex-link').forEach((link) => link.classList.toggle('d-none', !isPlex));
        const featureNav = document.querySelector('#edit-tab-processing .processing-feature-nav');
        if (featureNav) featureNav.classList.toggle('d-none', !isPlex);
        const previewOutput = $('#processingPreviewOutput');
        const previewDescriptions = {
            plex: ['Plex config folder (set in Connection)', 'Plex BIF preview bundles are written to the Plex config folder in Connection.'],
            emby: ['Beside each video (the media mount must be writable)', 'Emby BIF previews are written beside each video. The media mount must be writable.'],
            jellyfin: ['Beside each video, or Jellyfin\'s config folder', 'Jellyfin trickplay tiles are written beside each video, or to the config folder selected in Connection. Setup Health checks the plugin and storage requirements.'],
        };
        const previewText = previewDescriptions[(server.type || '').toLowerCase()]
            || ['Follows this server’s configuration', 'Preview output follows this server’s configuration.'];
        previewOutput.textContent = previewText[0];
        previewOutput.title = previewText[1];

        // Jellyfin-only: "store trickplay off the media drive" toggle + the
        // config-folder field it reveals. Mirrors the Plex config-folder block.
        const isJellyfin = (server.type || '').toLowerCase() === 'jellyfin';
        const offMediaGroup = document.getElementById('editJellyfinOffMediaGroup');
        if (offMediaGroup) offMediaGroup.classList.toggle('d-none', !isJellyfin);
        const jfOutput = server.output || {};
        const saveOffMedia = jfOutput.save_with_media === false;
        const offMediaToggle = document.getElementById('editJellyfinSaveOffMedia');
        const configFolderGroup = document.getElementById('editJellyfinConfigFolderGroup');
        const configFolderInput = document.getElementById('editJellyfinConfigFolder');
        if (offMediaToggle) offMediaToggle.checked = saveOffMedia;
        if (configFolderInput) {
            configFolderInput.value = jfOutput.jellyfin_config_folder || '';
            configFolderInput.classList.remove('is-valid', 'is-invalid');
            // Bind the inline structural validator once (mirrors the Plex
            // config-folder field) so the path is checked live as you type.
            if (!configFolderInput.dataset.validatorBound) {
                configFolderInput.addEventListener('input', _debouncedValidatePath(configFolderInput));
                configFolderInput.dataset.validatorBound = '1';
            }
            // Only validate up-front when off-media is actually on (the field is
            // visible) — avoids a fetch against a hidden field for a server that
            // has a stale stored config folder but off-media disabled.
            if (configFolderInput.value && saveOffMedia) _validateLocalPathInput(configFolderInput);
        }
        if (configFolderGroup) configFolderGroup.classList.toggle('d-none', !(isJellyfin && saveOffMedia));
        // The "Webhook & Scanner" tab now shows for ALL server types.
        // Pre-fix it was hidden for non-Plex servers — closing the user's
        // bug report "Plex has webhook register section but emby and jelly
        // does not? Why?". Per-vendor content is rendered by
        // _renderVendorWebhookSection() below.
        const automationTabLi = document.getElementById('editTabAutomationLi');
        if (automationTabLi) automationTabLi.classList.remove('d-none');
        // Plex Direct Webhook card stays Plex-only (the registration
        // API is Plex-specific). The Recently Added Scanner card
        // applies to every vendor — it just polls the server's
        // recently-added API and dispatches per-item jobs.
        const plexWebhookCard = document.getElementById('editPlexWebhookCard');
        const recentlyAddedCard = document.getElementById('editRecentlyAddedCard');
        if (plexWebhookCard) plexWebhookCard.classList.toggle('d-none', !isPlex);
        // The Recently Added Scanner card is universal — show it
        // for every vendor.
        if (recentlyAddedCard) recentlyAddedCard.classList.remove('d-none');
        _renderVendorWebhookSection(server);
        const editSectionSelect = document.getElementById('editServerSectionSelect');
        if (editSectionSelect) {
            Array.from(editSectionSelect.options).forEach((option) => {
                const tab = document.querySelector(`#editServerModal [data-bs-target="#${option.value}"]`);
                const unavailable = !tab || tab.closest('.nav-item').classList.contains('d-none');
                option.disabled = unavailable;
                option.hidden = unavailable;
            });
        }
        // Always force the General tab active on open. Without this, opening a
        // Plex server, clicking "Webhook & Scanner", closing, then opening a
        // non-Plex server leaves the now-hidden Plex pane visible because
        // Bootstrap doesn't auto-reset on modal hide. (Fix-2 from H code review.)
        try {
            document.querySelectorAll('#editServerModal .nav-link').forEach((el) => el.classList.remove('active'));
            document.querySelectorAll('#editServerModal .tab-pane').forEach((el) => el.classList.remove('show', 'active'));
            // Map the openTab key to the DOM ids. Unknown values fall
            // back to general rather than silently leaving every tab
            // hidden (the pre-fix symptom would be a blank modal body).
            const tabMap = {
                general: 'edit-tab-general',
                health: 'edit-tab-health',
                processing: 'edit-tab-processing',
                markers: 'edit-tab-processing',
                loudness: 'edit-tab-processing',
            };
            const paneId = tabMap[openTab] || 'edit-tab-general';
            const activeTab = document.querySelector(`#editServerModal [data-bs-target="#${paneId}"]`);
            const activePane = document.getElementById(paneId);
            if (activeTab) activeTab.classList.add('active');
            if (activePane) activePane.classList.add('show', 'active');
            if (editSectionSelect) editSectionSelect.value = paneId;
        } catch (_e) {
            // Best-effort — Bootstrap not available shouldn't break Edit.
        }
        if (isPlex) {
            const out = server.output || {};
            $('#editPlexChapterThumbnails').checked = out.chapter_thumbnails === true;
            $('#editPlexChapterHealthLink').onclick = (event) => {
                event.preventDefault();
                activateSection('edit-tab-health');
            };
            const cfgInput = $('#editPlexConfigFolder');
            cfgInput.value = out.plex_config_folder || '';
            cfgInput.classList.remove('is-valid', 'is-invalid');
            // Bind once — _editPlexConfigBound is set after the first wire-up.
            if (!cfgInput.dataset.validatorBound) {
                cfgInput.addEventListener('input', _debouncedValidatePath(cfgInput));
                cfgInput.dataset.validatorBound = '1';
            }
            if (cfgInput.value) _validateLocalPathInput(cfgInput);

            // Plex-only: load the Plex Direct webhook registration status.
            try {
                if (typeof loadPlexWebhookStatus === 'function') loadPlexWebhookStatus();
                const caption = document.getElementById('editPlexWebhookServerCaption');
                if (caption) caption.innerHTML = `This webhook will be registered with <strong>${escapeHtml(server.name || 'this Plex server')}</strong> using its own Plex token.`;
            } catch (_e) { }
        }

        // Recently Added Scanner panel applies to every vendor. Scope
        // the panel's server-id state so list-filter + create-default
        // target THIS server, then wire buttons + load the list. Pre-
        // fix this lived inside the ``if (isPlex)`` block which is why
        // Emby/Jellyfin users saw an empty "Loading scanners…" spinner.
        try {
            if (typeof setPlexWebhookPanelServerId === 'function') setPlexWebhookPanelServerId(server.id);
            if (typeof _wirePlexWebhookPanel === 'function') _wirePlexWebhookPanel();
            if (typeof loadRecentlyAddedScanners === 'function') loadRecentlyAddedScanners();
        } catch (_e) { }

        renderEditLibraries(server.libraries || []);
        renderEditPathMappings(server.path_mappings || []);
        renderEditExcludePaths(server.exclude_paths || []);
        if (window.loadMarkersTab) window.loadMarkersTab(server);
        if (window.loadLoudnessTab) window.loadLoudnessTab(server);
        showEditResult('', '');

        const modalEl = document.getElementById('editServerModal');
        const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
        updateEditBadges();
        updateAutomationDot();
    }

    // Each row's Intro & Credits cell is left empty and hidden: markers_server_tab.js fills it
    // (window.renderMarkersLibraryColumn) and shows the column while that tab's switch is on.
    function renderEditLibraries(libraries) {
        const list = $('#editLibraryList');
        if (!libraries.length) {
            list.innerHTML = '<tr><td colspan="4" class="text-muted">No cached libraries — click "Refresh libraries" on the server card to fetch them from the server.</td></tr>';
            updateEditBadges();
            return;
        }
        list.innerHTML = libraries.map((lib, idx) => {
            const label = lib.name || lib.id || 'unnamed';
            return `
            <tr data-lib-id="${escapeHtml(lib.id || '')}"
                data-lib-name="${escapeHtml(lib.name || '')}"
                data-lib-kind="${escapeHtml(lib.kind || '')}">
                <td class="text-break" data-l="Library">
                    ${escapeHtml(label)}
                    <span class="badge bg-secondary ms-1">${escapeHtml(lib.kind || 'unknown')}</span>
                </td>
                <td class="text-center sm-lib-sw" data-l="Previews">
                    <div class="form-check form-switch edit-lib-switch">
                        <input type="checkbox" role="switch" class="form-check-input edit-lib-toggle"
                               data-idx="${idx}"
                               data-id="${escapeHtml(lib.id || '')}"
                               data-name="${escapeHtml(lib.name || lib.id || '')}"
                               aria-label="Previews for ${escapeHtml(label)}"
                               ${lib.enabled ? 'checked' : ''}>
                    </div>
                </td>
                <td class="text-center markers-lib-col markers-lib-cell d-none" data-l="Intro &amp; Credits"></td>
                <td class="text-center loudness-lib-col loudness-lib-cell d-none" data-l="Loudness"></td>
            </tr>`;
        }).join('');
        updateEditBadges();
    }

    // The stacked (phone) layout hides the table header, so each cell carries its own label with the same ⓘ the
    // header has. Hidden on desktop, where the header shows.
    function cellLabel(text, tip, explain) {
        const more = explain
            ? ` data-explain-title="${escapeHtml(explain.title)}" data-explain-template="${escapeHtml(explain.template)}" aria-label="Explain ${escapeHtml(explain.title)}"`
            : '';
        return `<span class="sm-cell-lab">${escapeHtml(text)}<button type="button" class="info-icon" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="${escapeHtml(tip)}"${more}><i class="bi bi-info-circle"></i></button></span>`;
    }

    function renderEditPathMappings(mappings) {
        const tbody = $('#editPathMappingsTable tbody');
        tbody.innerHTML = '';
        mappings.forEach((row) => addPathMappingRow(row));
        if (!mappings.length) addPathMappingRow();
    }

    function addPathMappingRow(row) {
        row = row || {};
        const tbody = $('#editPathMappingsTable tbody');
        const tr = document.createElement('tr');
        const remoteVal = row.remote_prefix || row.plex_prefix || '';
        const localVal = row.local_prefix || '';
        const webhookAliases = Array.isArray(row.webhook_prefixes)
            ? row.webhook_prefixes.join('; ')
            : (row.webhook_prefixes || '');
        tr.innerHTML = `
            <td data-l="Path on Media Server">${cellLabel('Path on Media Server', 'The path your media server reports (what\'s stored in its database / shown in its admin UI).')}<input type="text" class="form-control form-control-sm pm-remote" value="${escapeHtml(remoteVal)}" placeholder="/data_16tb/movies"></td>
            <td data-l="Local path on this app">
                ${cellLabel('Local path on this app', 'The path the same file appears at INSIDE this app\'s container — i.e. the volume mount target.')}
                <div class="input-group input-group-sm has-validation sm-vf">
                    <input type="text" class="form-control form-control-sm pm-local" value="${escapeHtml(localVal)}" placeholder="/mnt/plex/movies">
                    <button type="button" class="btn btn-outline-secondary pm-browse" title="Browse folders" aria-label="Browse folders">
                        <i class="bi bi-folder2-open"></i>
                    </button>
                    <div class="invalid-feedback small"></div>
                    <div class="valid-feedback small">Path exists</div>
                </div>
            </td>
            <td data-l="Path on Apps (Sonarr, Radarr, Webhooks)">${cellLabel('Path on Apps (Sonarr, Radarr, Webhooks)', 'For Sonarr/Radarr/Tdarr webhooks that send a different path than your server reports.', { title: 'Webhook path mapping', template: 'infoWebhookPathTpl' })}<input type="text" class="form-control form-control-sm pm-webhook" value="${escapeHtml(webhookAliases)}" placeholder="/data" title="Optional. Webhook source prefix that resolves to this disk. Add another row for additional sources."></td>
            <td class="sm-cell-x"><button type="button" class="btn btn-sm btn-outline-danger pm-remove" aria-label="Remove mapping" title="Remove mapping"><i class="bi bi-x-lg"></i></button></td>
        `;
        tr.querySelector('.pm-remove').addEventListener('click', () => { tr.remove(); setEditDirty(true); updateEditBadges(); });
        const localInput = tr.querySelector('.pm-local');
        localInput.addEventListener('input', _debouncedValidatePath(localInput));
        if (localVal) _validateLocalPathInput(localInput);
        tr.querySelector('.pm-browse').addEventListener('click', () => {
            const start = (localInput.value || '').trim() || '/';
            window.openFolderPicker(start, (picked) => {
                localInput.value = picked;
                _validateLocalPathInput(localInput);
            });
        });
        tbody.appendChild(tr);
        if (typeof _initBootstrapTooltips === 'function') _initBootstrapTooltips(tr);
    }

    // Debounced inline validation of local-path inputs (path mappings + Plex
    // config folder). Mirrors the Setup Wizard's UX so users get red-border
    // feedback the moment they type a path that doesn't exist.
    const _validateTimers = new WeakMap();
    function _debouncedValidatePath(input) {
        return function () {
            clearTimeout(_validateTimers.get(input));
            _validateTimers.set(input, setTimeout(() => _validateLocalPathInput(input), 400));
        };
    }

    async function _validateLocalPathInput(input) {
        const path = (input.value || '').trim();
        const feedback = input.parentElement.querySelector('.invalid-feedback');
        const success = input.parentElement.querySelector('.valid-feedback');
        if (!path) {
            input.classList.remove('is-invalid', 'is-valid');
            return;
        }
        // The Plex config folder field gets the deeper structural check so
        // the success message can confidently say "this is a real Plex config
        // folder". Other path-mapping inputs only need existence + readable.
        // Three IDs cover the same widget on different surfaces:
        //   * editPlexConfigFolder       — /servers Edit Server modal
        //   * plexConfigFolder           — _server_connection_form partial
        //   * wizardPlexConfigFolder     — /setup wizard step 3 (renamed to
        //                                   avoid colliding with the partial's
        //                                   hidden input on the same page)
        const isPlexCfg =
            input.id === 'editPlexConfigFolder' ||
            input.id === 'plexConfigFolder' ||
            input.id === 'wizardPlexConfigFolder';
        // Jellyfin off-media config folder gets the same deeper structural
        // check (mirrors Plex) so the success message can confidently say
        // "valid Jellyfin config folder" rather than just "path exists".
        const isJellyfinCfg = input.id === 'editJellyfinConfigFolder';
        const useStructuralCheck = isPlexCfg || isJellyfinCfg;
        const endpoint = isPlexCfg
            ? '/api/settings/validate-plex-config-folder'
            : isJellyfinCfg
              ? '/api/settings/validate-jellyfin-config-folder'
              : '/api/settings/validate-local-path';
        try {
            const resp = await fetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '',
                },
                body: JSON.stringify({ path }),
            });
            const data = await resp.json();
            if (input.value.trim() !== path) return;
            if (data.error) {
                input.classList.remove('is-valid');
                input.classList.add('is-invalid');
                if (feedback) feedback.textContent = data.error;
            } else if (!data.exists) {
                input.classList.remove('is-valid');
                input.classList.add('is-invalid');
                if (feedback) feedback.textContent = 'Directory not found on this container';
            } else {
                input.classList.remove('is-invalid');
                input.classList.add('is-valid');
                if (success && useStructuralCheck && data.detail) {
                    success.textContent = `Looks like a ${data.detail}`;
                } else if (success && isPlexCfg) {
                    success.textContent = 'Valid Plex config folder';
                } else if (success && isJellyfinCfg) {
                    success.textContent = 'Valid Jellyfin config folder';
                } else if (success) {
                    success.textContent = 'Path exists';
                }
            }
        } catch {
            input.classList.remove('is-valid', 'is-invalid');
        }
    }

    function readPathMappingsFromForm() {
        return $$('#editPathMappingsTable tbody tr').map((tr) => {
            const remote = tr.querySelector('.pm-remote').value.trim();
            const local = tr.querySelector('.pm-local').value.trim();
            const webhookRaw = (tr.querySelector('.pm-webhook')?.value || '').trim();
            const webhook_prefixes = webhookRaw
                ? webhookRaw.split(/[;,]/).map((s) => s.trim()).filter(Boolean)
                : [];
            if (!remote && !local && !webhook_prefixes.length) return null;
            // Both keys: this build reads remote_prefix first; older builds' path resolvers read only plex_prefix.
            return { remote_prefix: remote, plex_prefix: remote, local_prefix: local, webhook_prefixes };
        }).filter(Boolean);
    }

    function renderEditExcludePaths(rules) {
        const tbody = $('#editExcludePathsTable tbody');
        tbody.innerHTML = '';
        rules.forEach((row) => addExcludePathRow(row));
        if (!rules.length) addExcludePathRow();
    }

    function addExcludePathRow(row) {
        row = row || {};
        const tbody = $('#editExcludePathsTable tbody');
        const tr = document.createElement('tr');
        const value = row.value || '';
        const type = row.type || 'path';
        tr.innerHTML = `
            <td data-l="Value"><input type="text" class="form-control form-control-sm ep-value" value="${escapeHtml(value)}" placeholder="/data/Trailers/"></td>
            <td data-l="Type">
                ${cellLabel('Type', 'Path prefix skips everything under a folder; Regex matches the full file path.', { title: 'Exclude paths', template: 'infoExcludeTypeTpl' })}
                <select class="form-select form-select-sm ep-type">
                    <option value="path" ${type === 'path' ? 'selected' : ''}>path (prefix)</option>
                    <option value="regex" ${type === 'regex' ? 'selected' : ''}>regex</option>
                </select>
            </td>
            <td class="sm-cell-x"><button type="button" class="btn btn-sm btn-outline-danger ep-remove" aria-label="Remove endpoint" title="Remove endpoint"><i class="bi bi-x-lg"></i></button></td>
        `;
        tr.querySelector('.ep-remove').addEventListener('click', () => { tr.remove(); setEditDirty(true); updateEditBadges(); });
        tbody.appendChild(tr);
    }

    function readExcludePathsFromForm() {
        return $$('#editExcludePathsTable tbody tr').map((tr) => {
            const value = tr.querySelector('.ep-value').value.trim();
            const type = tr.querySelector('.ep-type').value;
            if (!value) return null;
            return { value, type };
        }).filter(Boolean);
    }

    function readEnabledLibraryIds() {
        return $$('.edit-lib-toggle').map((el) => ({
            id: el.dataset.id,
            name: el.dataset.name,
            enabled: el.checked,
        }));
    }

    async function saveEditedServer() {
        if (!_editState) return;
        const { server } = _editState;
        const opening = modalOpening(document.getElementById('editServerModal'));
        const saveBtn = $('#editServerSave');
        saveBtn.dataset.busy = '1';
        saveBtn.disabled = true;
        showEditResult('', '');
        const orig = saveBtn.innerHTML;
        saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';

        // Build PUT payload — only include fields the user actually changed
        // shape-wise (match server_config_to_dict's expected keys).
        const payload = {
            name: $('#editServerDisplayName').value.trim() || server.name,
            url: $('#editServerUrl').value.trim(),
            verify_ssl: $('#editServerVerifySsl').checked,
            enabled: $('#editServerEnabled').checked,
            path_mappings: readPathMappingsFromForm(),
            exclude_paths: readExcludePathsFromForm(),
        };

        // D24 — vendor-aware re-auth: build payload.auth from whichever
        // section is visible AND has new content. Empty section means
        // "leave existing auth alone" (matches api_servers.py PUT
        // redaction rules — omitting auth preserves the on-disk value).
        const newAuth = _readEditReauthPayload(server);
        if (newAuth) {
            payload.auth = newAuth;
        }

        // Per-library enabled toggles (preserve other library fields).
        // D23 — build the payload from the DOM toggle inputs as the
        // source of truth (they're what the user actually clicked),
        // then merge any non-toggle fields (paths, kind, etc.) from
        // the cached server.libraries by id. The previous design
        // built ONLY from cached server.libraries, which silently
        // wrote libraries=[] when the user clicked Refresh-libraries
        // mid-modal: the DOM had the freshly-fetched checkboxes but
        // the cache still held the empty list captured at modal open.
        const toggles = readEnabledLibraryIds();
        const cachedById = new Map((server.libraries || []).map((lib) => [String(lib.id), lib]));
        payload.libraries = toggles.map((t) => {
            const cached = cachedById.get(String(t.id)) || {};
            return {
                ...cached,
                id: t.id,
                name: t.name || cached.name || t.id,
                enabled: !!t.enabled,
            };
        });

        // Plex config folder lives under output.
        if ((server.type || '').toLowerCase() === 'plex') {
            payload.output = {
                ...(server.output || {}),
                adapter: 'plex_bundle',
                plex_config_folder: $('#editPlexConfigFolder').value.trim(),
                chapter_thumbnails: $('#editPlexChapterThumbnails').checked,
            };
        }

        // Jellyfin off-media trickplay settings live under output too. Spread
        // the existing output so width / frame_interval / webhook keys survive.
        if ((server.type || '').toLowerCase() === 'jellyfin') {
            const offMedia = !!($('#editJellyfinSaveOffMedia') || {}).checked;
            payload.output = {
                ...(server.output || {}),
                save_with_media: !offMedia,
                jellyfin_config_folder: ($('#editJellyfinConfigFolder').value || '').trim(),
            };
        }

        // Plex: turning Intro & Credits on needs the database-write confirmation (asked when the switch was
        // flipped; asked again here in case that was dismissed).
        if (window.markersNeedsPlexConfirmation && window.markersNeedsPlexConfirmation(server)) {
            const confirmed = await window.confirmPlexMarkers(server);
            if (!confirmed) {
                delete saveBtn.dataset.busy;
                saveBtn.disabled = false;
                saveBtn.innerHTML = orig;
                return;
            }
        }
        if (window.readMarkersFromForm) payload.markers = window.readMarkersFromForm(server);
        if (window.readLoudnessFromForm) payload.loudness = window.readLoudnessFromForm(server);

        const r = await api('PUT', `/api/servers/${encodeURIComponent(server.id)}`, payload);
        delete saveBtn.dataset.busy;
        saveBtn.innerHTML = orig;
        // An answer that arrives after the user closed this dialog (and maybe opened another) must not touch the
        // new one's state: only the opening the request started in gets the result.
        const nowOpen = modalOpening(document.getElementById('editServerModal'));
        const sameOpening = !!opening && !!nowOpen && nowOpen.count === opening.count;
        saveBtn.disabled = sameOpening ? false : !_editDirty;
        if (r.ok) {
            if (sameOpening) {
                // Saved: nothing is left to discard, so the unsaved-changes guard must not stop the close.
                _editDiscardOk = true;
                setEditDirty(false);
            }
            hideModalSafely(document.getElementById('editServerModal'), opening);
            loadServers();
        } else if (sameOpening) {
            showEditResult('bad', `<i class="bi bi-x-circle-fill me-1"></i>${escapeHtml((r.data && r.data.error) || `Save failed (HTTP ${r.status})`)}`);
        }
    }

    /**
     * Copy the modal's current path_mappings / exclude_paths into every
     * OTHER configured server via PUT. Lets users with shared mounts /
     * shared exclusion rules avoid typing the same list N times.
     */
    async function applyListToAllServers(field, valueProducer, btn) {
        if (!_editState) return;
        const { server, allServers } = _editState;
        const others = allServers.filter((s) => s.id !== server.id);
        if (!others.length) {
            showToast('Nothing to copy', 'No other servers are configured.', 'info');
            return;
        }
        const value = valueProducer();
        const okList = [];
        const failedList = [];
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
        for (const other of others) {
            const r = await api('PUT', `/api/servers/${encodeURIComponent(other.id)}`, { [field]: value });
            if (r.ok) okList.push(other.name || other.id);
            else failedList.push(`${other.name || other.id}: ${(r.data && r.data.error) || r.status}`);
        }
        btn.disabled = false;
        btn.innerHTML = orig;
        if (failedList.length === 0) {
            showEditResult('ok', `<i class="bi bi-check2-circle me-1"></i>Copied ${escapeHtml(field)} to ${okList.length} other server${okList.length === 1 ? '' : 's'}.`);
        } else {
            showEditResult('warn', `<i class="bi bi-exclamation-triangle me-1"></i>Copied to ${okList.length}/${others.length} server${others.length === 1 ? '' : 's'}; failures: ${failedList.map(escapeHtml).join('; ')}`);
        }
    }

    async function setVendorExtraction(scanExtraction) {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const result = document.getElementById('editVendorExtractionResult');
        const disableBtn = document.getElementById('editDisableVendorExtractionBtn');
        const enableBtn = document.getElementById('editEnableVendorExtractionBtn');
        const both = [disableBtn, enableBtn].filter(Boolean);
        const labels = both.map(b => b.innerHTML);
        both.forEach(b => { b.disabled = true; });
        const targetBtn = scanExtraction ? enableBtn : disableBtn;
        if (targetBtn) targetBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Working…';
        result.className = 'small text-muted';
        result.textContent = '';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/vendor-extraction`, { scan_extraction: scanExtraction });
            const data = r.data || {};
            const okCount = data.ok_count || 0;
            const skippedCount = data.skipped_count || 0;
            const errorCount = data.error_count || 0;
            const total = data.total || 0;
            const verb = scanExtraction ? 'Re-enabled' : 'Disabled';
            const parts = [`${okCount}/${total} libraries`];
            if (skippedCount > 0) parts.push(`${skippedCount} skipped (custom agent — toggle in Plex UI)`);
            if (errorCount > 0) parts.push(`${errorCount} failed`);
            if (errorCount === 0) {
                result.className = skippedCount > 0 ? 'small text-warning' : 'small text-success';
                result.innerHTML = `<i class="bi bi-${skippedCount > 0 ? 'info-circle' : 'check-circle'} me-1"></i>${verb}: ${parts.join(' · ')}`;
            } else {
                result.className = 'small text-danger';
                result.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>${verb}: ${parts.join(' · ')} — see Logs page`;
            }
        } catch (e) {
            result.className = 'small text-danger';
            result.textContent = String(e);
        } finally {
            both.forEach((b, i) => { b.disabled = false; b.innerHTML = labels[i]; });
            // Re-probe so the panel snaps to the new state's CTA.
            renderVendorExtractionState(id);
        }
    }

    async function renderVendorExtractionState(serverId) {
        // Probe per-library state and pick the right CTA. Avoids
        // showing both Disable and Re-enable when one of them would
        // be a no-op. Critical → red, mixed → yellow,
        // already-recommended → success message + small Re-enable link.
        const disableBtn = document.getElementById('editDisableVendorExtractionBtn');
        const enableBtn = document.getElementById('editEnableVendorExtractionBtn');
        const stateMsg = document.getElementById('editVendorExtractionState');
        if (!disableBtn || !enableBtn) return;
        // Default to hiding both until the probe answers.
        disableBtn.classList.add('d-none');
        enableBtn.classList.add('d-none');
        if (stateMsg) { stateMsg.className = 'small text-muted'; stateMsg.textContent = 'Checking…'; }

        const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/vendor-extraction/status`);
        if (!r.ok || !r.data) {
            // Probe failed — show both buttons so the user can still act manually.
            disableBtn.classList.remove('d-none');
            enableBtn.className = 'btn btn-sm btn-outline-secondary';
            if (stateMsg) { stateMsg.className = 'small text-warning'; stateMsg.textContent = 'Could not check current state — try Test Connection above.'; }
            return;
        }

        const { extracting_count = 0, stopped_count = 0, skipped_count = 0, total = 0 } = r.data;
        if (stateMsg) {
            const fragments = [];
            if (stopped_count > 0) fragments.push(`${stopped_count}/${total} disabled`);
            if (skipped_count > 0) fragments.push(`${skipped_count} skipped (custom agent — toggle in Plex UI)`);
            stateMsg.textContent = fragments.length > 0 ? fragments.join(' · ') : '';
            stateMsg.className = 'small text-muted';
        }

        if (extracting_count > 0) {
            // At least one library is still doing its own extraction —
            // primary action is "disable on this server" (idempotent for
            // libraries already disabled).
            disableBtn.classList.remove('d-none');
            disableBtn.disabled = false;
            disableBtn.innerHTML = '<i class="bi bi-stop-circle me-1"></i>Disable on this server';
        } else {
            // All libraries at recommended state — hide Disable, keep Re-enable available for revert.
            disableBtn.classList.add('d-none');
            enableBtn.className = 'btn btn-sm btn-outline-secondary';
            enableBtn.disabled = false;
            enableBtn.innerHTML = 'Re-enable';
            if (stateMsg) {
                stateMsg.className = 'small text-success';
                stateMsg.innerHTML = `<i class="bi bi-check-circle me-1"></i>Server isn't generating its own previews. ${skipped_count > 0 ? `(${skipped_count} library could not be checked — toggle in Plex UI.)` : ''}`;
            }
        }
    }

    async function testEditConnection() {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const btn = document.getElementById('editTestConnectionBtn');
        const result = document.getElementById('editTestConnectionResult');
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing…';
        result.className = 'sm-tres';
        result.textContent = '';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/test-connection`);
            const data = r.data || {};
            if (data.ok) {
                result.className = 'sm-tres ok';
                result.innerHTML = `<i class="bi bi-check-circle-fill"></i>Connected${data.version ? ` &mdash; ${escapeHtml(data.version)}` : ''}`;
                setEditStatusPill('ok', 'Connected');
            } else {
                result.className = 'sm-tres warn';
                result.innerHTML = `<i class="bi bi-exclamation-triangle-fill"></i>${escapeHtml(data.message || 'Connection failed')}`;
                setEditStatusPill('bad', 'Unreachable');
                // A refused credential is the one case the credentials card is for: open it.
                if (/\b401\b|unauthori[sz]ed|forbidden|invalid (token|api key|credential)|credentials?\b/i.test(String(data.message || ''))) {
                    setCredFormsOpen(true);
                }
            }
            // Plugin badge — only present in the response for Jellyfin
            // servers that connected successfully. updateJellyfinPluginPanel
            // hides the panel for non-Jellyfin and missing-plugin cases.
            updateJellyfinPluginPanel(data.plugin);
        } catch (e) {
            result.className = 'sm-tres bad';
            result.textContent = String(e);
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    // ─── Unified "Previews readiness" card ─────────────────────────────
    // Section-based renderer — replaces the flat row list with three
    // labelled sections and inline per-issue CTAs:
    //
    //   STATUS           ─ version + activation mode row + inline
    //                      [Install plugin] button when plugin absent.
    //   LIBRARY SETTINGS ─ per-issue [Fix this] buttons + a global
    //                      [Fix all library settings] shortcut.
    //   SERVER OPTIONS   ─ Jellyfin TrickplayOptions row + [Sync
    //                      options] button when geometry mismatches.
    //
    // The "Fix and enable everything" button at the bottom is still
    // there for users who want one-click triage.

    // Cache of the most recent successful readiness probe payload, keyed
    // by serverId so concurrent edit-modal opens for different servers
    // can't bleed into each other. Used by the bulk Fix buttons to
    // enumerate the change plan. Cleared on probe failure for the same
    // serverId so we don't operate on stale data after that server goes
    // offline. Declared above the function that mutates it so the
    // binding is initialised before any caller can reach it (avoids the
    // latent TDZ footgun a future top-level call would expose).
    const _readinessDataByServer = new Map();

    // Render the readiness card's "Server is disabled" state WITHOUT
    // probing the network. Mirrors the backend's _disabled_response
    // contract on /api/servers/<id>/previews-readiness — the two layers
    // agree so a disabled server is never woken by this modal whether
    // the user opened Edit from the card glyph or via an external link.
    function _renderReadinessDisabled(serverId) {
        const group = document.getElementById('editReadinessGroup');
        const badge = document.getElementById('editReadinessBadge');
        const body = document.getElementById('editReadinessBody');
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixResult = document.getElementById('editReadinessFixResult');
        const pluginCtl = document.getElementById('editReadinessPluginControls');
        if (!group || !badge || !body || !fixCtl) return;
        group.classList.remove('d-none');
        fixCtl.classList.add('d-none');
        if (pluginCtl) pluginCtl.classList.add('d-none');
        if (fixResult) { fixResult.className = 'sm-hint'; fixResult.textContent = ''; }
        badge.className = 'pill';
        badge.textContent = 'disabled';
        setNavBadge('editHealthTabMarker', '');
        body.innerHTML = '<div class="sm-hint"><i class="bi bi-pause-circle me-1"></i>This server is disabled — checks are paused. Re-enable it with the switch above to run readiness checks.</div>';
        _readinessDataByServer.delete(serverId);
    }

    async function runReadinessProbe(serverId, serverType) {
        const group = document.getElementById('editReadinessGroup');
        const badge = document.getElementById('editReadinessBadge');
        const body = document.getElementById('editReadinessBody');
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixResult = document.getElementById('editReadinessFixResult');
        const pluginCtl = document.getElementById('editReadinessPluginControls');
        if (!group || !badge || !body || !fixCtl) return;

        // Reset state from any prior modal open.
        group.classList.remove('d-none');
        body.innerHTML = '';
        fixCtl.classList.add('d-none');
        if (pluginCtl) pluginCtl.classList.add('d-none');
        if (fixResult) { fixResult.className = 'sm-hint'; fixResult.textContent = ''; }
        badge.className = 'pill';
        badge.textContent = 'checking…';

        const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/previews-readiness`);
        if (!r.ok || !r.data) {
            badge.className = 'pill warn';
            badge.textContent = 'unavailable';
            body.innerHTML = '<div class="sm-hint">Could not reach the server. Check connection and try again.</div>';
            setNavBadge('editHealthTabMarker', '');
            _readinessDataByServer.delete(serverId);
            return;
        }

        // Stash the latest probe payload so the Fix-all / Fix-critical
        // buttons can build a preview plan from it without re-probing
        // (the preview modal needs the data BEFORE making any change).
        _readinessDataByServer.set(serverId, r.data);
        renderReadiness(serverId, serverType, r.data);
    }

    // Badge derivation — 5 possible labels driven by the unified envelope:
    //   red   "action needed"     — any critical section failing
    //   amber "recommendations"   — non-critical issues only
    //   green "ready (instant)"   — Jellyfin + plugin installed
    //   green "ready (next scan)" — Jellyfin without plugin (Mode B is valid)
    //   green "ready"             — everything ok, no previews plugin (Plex/Emby;
    //                               an Emby markers plugin doesn't change this)
    //
    // Walks sections[] and rolls up severity. Drives the sub-label
    // off the plugin section's current state — no vendor branching
    // needed in the call site.
    function _deriveBadgeState(data) {
        const sections = data.sections || [];
        let anyCritical = false;
        let anyRecommended = false;
        let pluginInstalled = null;
        for (const section of sections) {
            // Section-level summary is the FALLBACK for sections that
            // emit no checks[] — when checks[] is non-empty, the
            // per-check walk below is authoritative. The section-level
            // line is dismissed-blind on purpose: dismissed flags live
            // on individual checks, not sections; a section with no
            // checks at all has nothing to silence.
            if (!(section.checks && section.checks.length)) {
                if (section.ok === false && section.severity === 'critical') {
                    anyCritical = true;
                }
                if (section.ok === false && section.severity === 'recommended') {
                    anyRecommended = true;
                }
            }
            if (section.id === 'plugin' && section.checks && section.checks.length
                && section.checks[0].id === 'plugin_installed') {
                // Plugin section carries the installed bit in the first row's
                // ``current`` value ("installed"/"not installed"/version).
                //
                // Keyed on the row id, not just the section id: the sub-label
                // below says how PREVIEWS activate, which only Jellyfin's
                // Bridge plugin decides. Emby's Intro & Credits plugin shares
                // the section id (the install controls key on it) but has
                // nothing to do with previews — reading it here would label a
                // healthy Emby "ready (instant)".
                pluginInstalled = section.checks[0].current !== 'not installed';
            }
            for (const check of section.checks || []) {
                if (check.ok === false && check.severity === 'critical') {
                    // Issue #237: critical failures ignore the
                    // dismissed flag — the safety enforcement at the
                    // partition layer forces them into mustFix
                    // regardless, so the badge must escalate too. A
                    // user can't silence a true blocker by POSTing its
                    // id to the dismiss endpoint.
                    anyCritical = true;
                }
                if (check.ok === false && check.severity === 'recommended') {
                    // Issue #237: recommended dismissals DO silence
                    // the yellow tier. If every recommended check is
                    // dismissed, the badge falls through to "ready" —
                    // the user has acknowledged each row and doesn't
                    // need the modal header nagging anymore. They can
                    // still see what's dismissed under
                    // "All good → Dismissed (N)".
                    if (check.dismissed === true) continue;
                    anyRecommended = true;
                }
            }
        }
        // `tier` is the stable string consumers key off (badge, tab marker, glyph) —
        // using it avoids substring-matching the Bootstrap class list, which
        // would break silently if someone ever added `bg-warning-subtle` etc.
        if (anyCritical) return { tier: 'critical', cls: 'bad', text: 'action needed' };
        if (anyRecommended) return { tier: 'recommended', cls: 'warn', text: 'recommendations' };
        if (pluginInstalled === true) return { tier: 'ok', cls: 'ok', text: 'ready (instant)' };
        if (pluginInstalled === false) return { tier: 'ok', cls: 'ok', text: 'ready (next scan)' };
        return { tier: 'ok', cls: 'ok', text: 'ready' };
    }

    // Renders the unified previews-readiness card. Walks data.sections[]
    // and groups the checks into three buckets (Must fix / Recommended /
    // All good) so users immediately see what needs their attention vs.
    // what's just informational. Within each bucket the source section
    // appears as a small subheading so context like "Library settings"
    // isn't lost.
    //
    // No vendor branching in this function — everything is driven by
    // what the server emitted. Vendors control section set + copy.
    function renderReadiness(serverId, serverType, data) {
        const badge = document.getElementById('editReadinessBadge');
        const body = document.getElementById('editReadinessBody');
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixCritBtn = document.getElementById('editReadinessFixCriticalBtn');
        const pluginCtl = document.getElementById('editReadinessPluginControls');
        if (!badge || !body || !fixCtl) return;

        const sections = data.sections || [];

        // Badge.
        const badgeState = _deriveBadgeState(data);
        badge.className = `pill ${badgeState.cls}`;
        badge.textContent = badgeState.text;
        setEditStatusPill('ok', 'Connected');

        // Bucket the checks once so each bucket can render its own
        // collapsible group with the right icon/colour/expanded state.
        // Bucketing rules:
        //   Must fix      = critical + !ok
        //   Recommended   = !ok (anything not critical, including info)
        //   All good      = ok (everything passing or info-pass)
        const partition = _partitionChecks(sections);

        // Count badge on the rail: red while anything must be fixed, amber for recommendations only, none when healthy.
        if (partition.mustFix.length > 0) setNavBadge('editHealthTabMarker', String(partition.mustFix.length), 'bad');
        else if (partition.recommended.length > 0) setNavBadge('editHealthTabMarker', String(partition.recommended.length), 'warn');
        else setNavBadge('editHealthTabMarker', '');
        const tabMarker = document.getElementById('editHealthTabMarker');
        if (tabMarker) tabMarker.title = partition.mustFix.length ? 'Action needed' : partition.recommended.length ? 'Recommendations' : '';

        body.innerHTML = '';
        if (partition.mustFix.length > 0) {
            body.appendChild(_renderBucket({
                serverId,
                serverType,
                tier: 'critical',
                title: 'Must fix',
                items: partition.mustFix,
                expanded: true,
                badgeCls: 'bg-danger',
                iconHtml: '<i class="bi bi-x-octagon-fill text-danger me-2"></i>',
                emptyHint: '',
            }));
        }
        if (partition.recommended.length > 0) {
            body.appendChild(_renderBucket({
                serverId,
                serverType,
                tier: 'recommended',
                title: 'Recommended',
                items: partition.recommended,
                expanded: partition.mustFix.length === 0,
                badgeCls: 'bg-warning text-dark',
                iconHtml: '<i class="bi bi-exclamation-triangle-fill text-warning me-2"></i>',
                emptyHint: '',
            }));
        }
        if (partition.allGood.length > 0) {
            body.appendChild(_renderBucket({
                serverId,
                serverType,
                tier: 'ok',
                title: 'All good',
                items: partition.allGood,
                // Only auto-expand when there's nothing actionable, so a
                // server that's fully healthy still shows its checks
                // up-front instead of an empty card.
                expanded: partition.mustFix.length === 0 && partition.recommended.length === 0,
                badgeCls: 'bg-success',
                iconHtml: '<i class="bi bi-check-circle-fill text-success me-2"></i>',
                emptyHint: '',
            }));
        }

        // One amber "Apply recommended" per bucket (the first); the rest read as secondary.
        body.querySelectorAll('.readiness-bucket').forEach((bucket) => {
            const first = bucket.querySelector('.rd-acts .btn-warning');
            if (first) first.classList.add('rd-primary');
        });

        // Show the fix controls when there are fixable rows (any check
        // with an enable/disable action whose recommended-state flip is
        // actionable). The critical-only button only surfaces when at
        // least one critical row is fixable — otherwise it'd be a
        // dead button alongside "Fix all".
        const fixablePlan = _buildFixPlan(sections, 'all');
        const criticalPlan = _buildFixPlan(sections, 'critical');
        if (fixablePlan.length > 0) {
            fixCtl.classList.remove('d-none');
        } else {
            fixCtl.classList.add('d-none');
        }
        if (fixCritBtn) {
            if (criticalPlan.length > 0) {
                fixCritBtn.classList.remove('d-none');
            } else {
                fixCritBtn.classList.add('d-none');
            }
        }

        // Hide the legacy plugin opt-in checkbox — install happens
        // inline via the per-check toggle now.
        if (pluginCtl) pluginCtl.classList.add('d-none');

        // Re-init Bootstrap tooltips on any new ⓘ icons.
        if (typeof _initBootstrapTooltips === 'function') {
            _initBootstrapTooltips(body);
        } else if (window.bootstrap && window.bootstrap.Tooltip) {
            body.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new window.bootstrap.Tooltip(el));
        }
    }

    // Walk every section's checks and assign each to one of three
    // buckets. The original section is preserved on each item so the
    // bucket renderer can group rows by their source section without
    // losing context (e.g. "Library settings → Movies — Trickplay
    // enabled").
    //
    // Bucketing rules:
    //   critical + !ok      → Must fix    (red, expanded)
    //   recommended + !ok   → Recommended (amber, expanded if no critical)
    //   info        + !ok   → All good    (probe-failure rows are not
    //                         actionable improvements; surfacing them
    //                         under "Recommended" would cry-wolf about
    //                         a transient connectivity blip).
    //   ok          + any   → All good
    // Vendor display-name lookup for badge copy that references the
    // user's media server admin UI (e.g. "Change in Plex UI"). Falls
    // back to "server admin" when the type is missing/unknown so the
    // badge still reads sensibly.
    function _vendorDisplayName(serverType) {
        switch ((serverType || '').toLowerCase()) {
            case 'plex': return 'Plex';
            case 'jellyfin': return 'Jellyfin';
            case 'emby': return 'Emby';
            default: return 'server admin';
        }
    }

    function _partitionChecks(sections) {
        const mustFix = [];
        const recommended = [];
        const allGood = [];
        for (const section of sections || []) {
            for (const check of (section.checks || [])) {
                // Optional server controls are explicitly visible without implying a
                // recommendation. Other informational diagnostics stay hidden.
                if ((check.severity || 'info') === 'info' && check.informational !== true) continue;
                const item = {
                    check,
                    sectionTitle: section.title || section.id || '',
                    sectionAnchor: section.docs_anchor || '',
                    sectionId: section.id || '',
                };
                // Issue #237: critical checks ALWAYS go to mustFix
                // even if dismissed — the user can't silence a true
                // blocker by POSTing its id to the dismiss endpoint.
                // The backend doesn't validate the dismissed id
                // against current severity; this partition step is
                // the safety enforcement instead.
                if (check.ok === false && check.severity === 'critical') {
                    mustFix.push(item);
                } else if (check.dismissed === true) {
                    // Dismissed non-critical row → tucked under
                    // "All good → Dismissed" so the user can see what
                    // they've silenced and undismiss if they change
                    // their mind. Raw audit state (ok / severity) is
                    // preserved on the check; only the placement
                    // changes.
                    allGood.push(item);
                } else if (check.ok === false && check.severity === 'recommended') {
                    recommended.push(item);
                } else {
                    allGood.push(item);
                }
            }
        }
        return { mustFix, recommended, allGood };
    }

    // Render one bucket as a `<details>` block. Items are grouped by
    // their source section — the section title appears as a small
    // grey subheading so users can still tell whether a row is about
    // the library scan settings or the plugin or path mappings.
    //
    // Issue #237: the "All good" bucket has a second sub-group for
    // dismissed checks — rendered AFTER the passing rows, under a
    // "Dismissed" subhead, so users can see what they've silenced
    // without confusing them with passing-row checkmarks.
    function _renderBucket({ serverId, serverType, tier, title, items, expanded, badgeCls, iconHtml }) {
        const det = document.createElement('details');
        det.className = 'readiness-bucket';
        det.dataset.tier = tier;
        if (expanded) det.open = true;

        const sum = document.createElement('summary');
        sum.className = 'd-flex align-items-center gap-2 user-select-none';
        sum.innerHTML = `${iconHtml}<span class="fw-semibold">${escapeHtml(title)}</span>`
            + `<span class="badge ${badgeCls} ms-1">${items.length}</span>`;
        det.appendChild(sum);

        const inner = document.createElement('div');
        inner.className = 'readiness-bucket-body';

        // Split dismissed items out so they render under a separate
        // "Dismissed" subhead at the bottom of the bucket. Done only
        // for the allGood tier (where dismissed items live); other
        // tiers carry no dismissed items by partition.
        const passing = [];
        const dismissed = [];
        for (const item of items) {
            if (tier === 'allGood' && item.check && item.check.dismissed === true) {
                dismissed.push(item);
            } else {
                passing.push(item);
            }
        }

        // Group passing items by section — same stable order as before.
        let lastSectionId = null;
        for (const item of passing) {
            if (item.sectionId !== lastSectionId) {
                lastSectionId = item.sectionId;
                inner.appendChild(_renderSectionSubhead(item.sectionTitle));
            }
            inner.appendChild(_renderCheckRow(serverId, serverType, item.check));
        }

        if (dismissed.length > 0) {
            // Use a distinct subhead so the dismissed rows don't read
            // as "passing under the same section as everything above".
            inner.appendChild(_renderSectionSubhead(`Dismissed (${dismissed.length})`));
            for (const item of dismissed) {
                inner.appendChild(_renderCheckRow(serverId, serverType, item.check));
            }
        }

        det.appendChild(inner);
        return det;
    }

    function _renderSectionSubhead(title) {
        // A plain label. Every row already carries its OWN ⓘ that opens the inline explanation modal; section-level
        // docs links 404'd on private forks / unpublished branches, so they stay dropped.
        const wrap = document.createElement('div');
        wrap.className = 'rd-cat';
        wrap.textContent = title;
        return wrap;
    }

    // Render one check row. Emits: status icon + label + ⓘ tooltip
    // (anchored to docs page) + severity badge + side-by-side
    // current/recommended diff + Manual chip when read-only +
    // inline [Enable] / [Disable] toggles built from check.actions.
    function _renderCheckRow(serverId, serverType, check) {
        const row = document.createElement('div');

        const ok = check.ok !== false;
        const sev = check.severity || 'info';
        const informational = sev === 'info' && check.informational === true;
        // ONE badge per row. When there's no action button the badge says where the user has to go instead
        // ("Change in <vendor> UI"): this app can't act on that row at all, so calling it a "fix" would mislead.
        const actionsObj = check.actions || {};
        const hasFixAction = !!(actionsObj.enable || actionsObj.disable);
        const vendorLabel = _vendorDisplayName(serverType);
        // check.fix_where is an explicit hint from the backend (readiness.py) for rows whose fix happens
        // somewhere other than the vendor's own admin UI — today only the Plex marker agent's rows. Reading
        // this off the row id would drift the moment a new agent-fixed row shipped without updating this list.
        const manualBadgeText = check.fix_where === 'agent' ? 'Fix on the agent' : `Change in ${vendorLabel} UI`;
        const manualBadgeTitle = check.fix_where === 'agent'
            ? "This app can't toggle this for you — fix it on the machine running the Plex marker agent."
            : `This app can't toggle this for you — open ${vendorLabel}'s admin UI and follow the instructions below.`;
        let tone = '';
        let tierBadge;
        if (informational) {
            tierBadge = '';
        } else if (ok && sev === 'critical') {
            tone = 'ok';
            tierBadge = '<span class="badge bg-success-subtle text-success-emphasis border border-success-subtle" title="Required check — currently passing.">Required</span>';
        } else if (ok) {
            tone = 'ok';
            tierBadge = '<span class="badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle" title="Recommended optimisation — currently applied.">Recommended</span>';
        } else if (sev === 'critical' && !hasFixAction) {
            tone = 'bad';
            tierBadge = `<span class="badge bg-danger-subtle text-danger-emphasis border border-danger-subtle" title="${escapeAttr(manualBadgeTitle)}">${escapeHtml(manualBadgeText)}</span>`;
        } else if (sev === 'critical') {
            tone = 'bad';
            tierBadge = '<span class="badge bg-danger-subtle text-danger-emphasis border border-danger-subtle" title="Required for the server to work — apply the fix.">Required — fix to enable</span>';
        } else if (!hasFixAction) {
            tone = 'warn';
            tierBadge = `<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle" title="${escapeAttr(manualBadgeTitle)}">${escapeHtml(manualBadgeText)}</span>`;
        } else {
            tone = 'warn';
            tierBadge = '<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle" title="Recommended improvement — server still works without it.">Recommended</span>';
        }
        row.className = `rd-row${tone ? ` ${tone}` : ''}`;

        // No data-explain-docs — the row's rich explanation is shown
        // inline via the global explain modal (set up by app.js's
        // .info-icon click delegator), and we deliberately don't link
        // out to an external docs page. Pre-fix every row carried a
        // GitHub URL that 404'd on private forks / unpushed branches.
        const tooltip = check.tooltip || '';
        const reason = check.reason || '';
        // The description is one line in the row; its full text also sits in the ⓘ so nothing is lost to the clamp.
        const explanationHtml = (reason ? `<p>${escapeHtml(reason)}</p>` : '') + (check.explanation || '');
        const infoIcon = (tooltip || explanationHtml)
            ? `<button type="button" class="info-icon" `
                + `data-bs-toggle="tooltip" data-bs-placement="top" `
                + `title="${escapeAttr(tooltip || reason)}" `
                + `data-explain-title="${escapeAttr(check.label || tooltip || 'About this check')}" `
                + `aria-label="Explain ${escapeAttr(check.label || '')}">`
                + `<i class="bi bi-info-circle"></i></button>`
            : '';

        const valuesHtml = _renderValueDiff(check.current, check.recommended, ok, check.label || '');

        const reasonStr = reason
            ? `<div class="rd-reason" title="${escapeAttr(reason)}">${escapeHtml(reason)}</div>`
            : '';

        const labelHtml = escapeHtml(check.label || check.id || '');
        row.innerHTML = `<span class="rd-sev" aria-hidden="true"></span>`
            + `<div class="rd-main"><div class="rd-title"><span>${labelHtml}</span>${tierBadge}${infoIcon}</div>${reasonStr}${valuesHtml}</div>`
            + `<div class="rd-acts"></div>`;
        const main = row.querySelector('.rd-main');
        const acts = row.querySelector('.rd-acts');
        if (check.help_url === '/settings#section-workers') {
            const help = document.createElement('a');
            help.href = check.help_url;
            help.className = 'd-inline-block mt-1';
            help.textContent = check.help_label || 'Configure CPU workers';
            main.appendChild(help);
        }

        // Attach the rich explanation HTML to the info-icon button as
        // a DOM property — can't round-trip multi-paragraph HTML through
        // a data-attr string (quote/entity soup). The document-level
        // .info-icon click delegator in app.js reads this back on open.
        //
        // Contract: renderReadiness() rebuilds every row on each probe
        // (full innerHTML swap, not diff-based), so the fresh DOM node
        // always carries the latest explanation — no stale-reference
        // window after a re-probe. See renderReadiness above.
        if (explanationHtml) {
            const infoBtn = row.querySelector('.info-icon');
            if (infoBtn) infoBtn._explanationHtml = explanationHtml;
        }

        // Per-check toggle buttons. Two semantic roles:
        //   FIX direction   — matches check.recommended (the "right" answer).
        //   OPPOSITE direction — moves AWAY from recommended (destructive/opt-out).
        // The fix is the filled button; the opposite is a low-emphasis outline; passing rows hide the fix (no-op)
        // and keep only the opt-out as a quiet outline.
        const actions = check.actions || {};
        // ``check.fix_action`` is an explicit hint from the backend
        // (string: "enable" or "disable") that names which action key
        // is the recommended fix. Required for rows whose ``recommended``
        // is a descriptive string (e.g. the scheduled-trickplay row's
        // "disabled (Bridge plugin handles registration)") because the
        // boolean fallback below treats any truthy ``recommended`` as
        // "enable" and would pick the wrong action — silently doing the
        // OPPOSITE of what the row recommends.
        const fixDir = (typeof check.fix_action === 'string' && (check.fix_action === 'enable' || check.fix_action === 'disable'))
            ? check.fix_action
            : (check.recommended ? 'enable' : 'disable');
        const breakDir = fixDir === 'enable' ? 'disable' : 'enable';
        const fixAction = actions[fixDir];
        const breakAction = actions[breakDir];

        // Button labels are decoupled from the on/off DIRECTION the
        // action runs in — the same word "Enable" meant "apply
        // the recommendation" on one row (recommended=On) and "override
        // the recommendation" on another (recommended=Off). Now:
        //   * Fix button   — always reads "Apply recommended" (intent-
        //                    labelled), unless the backend names the fix.
        //   * Break button — reads "Enable (override)" or "Disable
        //                    (override)" — outcome verb + a "(override)"
        //                    tag so the user reads it as "do the
        //                    opposite of the recommendation, on
        //                    purpose". Tooltip restates the target
        //                    state in full.
        if (!informational && !ok && fixAction) {
            const targetOn = fixDir === 'enable';
            const icon = targetOn ? 'bi-toggle-on' : 'bi-toggle-off';
            // ``check.fix_label`` names the fix where "Apply recommended"
            // wouldn't say what it does (Plex's "Turn on" for a library's
            // own marker settings, "Set server-wide to Never").
            const fixLabel = typeof check.fix_label === 'string' && check.fix_label ? check.fix_label : '';
            const btn = _makeActionButton('btn-warning', icon, escapeHtml(fixLabel || 'Apply recommended'), check, fixDir);
            btn.title = fixLabel
                ? `${fixLabel} — ${check.label || check.id || 'this'}`
                : `Apply the recommendation — set ${check.label || check.id || 'this'} to ${targetOn ? 'On' : 'Off'}`;
            btn.addEventListener('click', () => _runCheckAction(serverId, serverType, check, fixDir, btn));
            acts.appendChild(btn);
        }
        if (!informational && breakAction) {
            const targetOn = breakDir === 'enable';
            const verb = targetOn ? 'Enable' : 'Disable';
            const icon = targetOn ? 'bi-toggle-on' : 'bi-toggle-off';
            const btn = _makeActionButton(
                'btn-outline-secondary',
                icon,
                `${verb} <span class="text-body-tertiary fw-normal">(override)</span>`,
                check,
                breakDir,
            );
            btn.title = `Override the recommendation — set ${check.label || check.id || 'this'} to ${targetOn ? 'On' : 'Off'}`;
            btn.addEventListener('click', () => _runCheckAction(serverId, serverType, check, breakDir, btn));
            acts.appendChild(btn);
        }
        const optionalDir = check.optional_action;
        if (informational && (optionalDir === 'enable' || optionalDir === 'disable')
            && actions[optionalDir] && typeof check.optional_label === 'string' && check.optional_label) {
            const icon = optionalDir === 'enable' ? 'bi-toggle-on' : 'bi-toggle-off';
            const btn = _makeActionButton(
                'btn-outline-secondary text-body', icon, escapeHtml(check.optional_label), check, optionalDir,
            );
            btn.querySelector('i').setAttribute('aria-hidden', 'true');
            btn.title = check.optional_label;
            btn.addEventListener('click', () => _runCheckAction(serverId, serverType, check, optionalDir, btn));
            acts.appendChild(btn);
        }
        if (check.note && typeof check.note === 'object' && check.note.text) {
            main.appendChild(_renderCheckNote(serverId, serverType, check));
        }

        // Issue #237: per-check dismiss control. Only on recommended-
        // severity rows — critical rows always belong in mustFix,
        // forced by _partitionChecks regardless of dismissed state.
        // Dismissed rows render under "All good → Dismissed" and get
        // an Undismiss link instead.
        if (sev === 'recommended') {
            if (check.dismissed === true) {
                const undismissBtn = document.createElement('button');
                undismissBtn.type = 'button';
                undismissBtn.className = 'btn btn-sm rd-dismiss';
                undismissBtn.innerHTML = '<i class="bi bi-arrow-counterclockwise"></i>Undismiss';
                undismissBtn.title = `Restore this check to the Recommended bucket — ${check.label || check.id || 'this row'}.`;
                undismissBtn.addEventListener('click', () => _toggleCheckDismissal(serverId, serverType, check, false, undismissBtn));
                acts.appendChild(undismissBtn);
            } else {
                const dismissBtn = document.createElement('button');
                dismissBtn.type = 'button';
                dismissBtn.className = 'btn btn-sm rd-dismiss';
                dismissBtn.innerHTML = '<i class="bi bi-x-circle"></i>Dismiss';
                dismissBtn.title = `Hide this recommendation — ${check.label || check.id || 'this row'} — from the Recommended bucket. Reversible from the All good section.`;
                dismissBtn.addEventListener('click', () => _toggleCheckDismissal(serverId, serverType, check, true, dismissBtn));
                acts.appendChild(dismissBtn);
            }
        }
        if (!acts.children.length) acts.remove();

        return row;
    }

    // Issue #237: POST to /previews-readiness/(dis|un)dismiss and
    // re-probe to redraw the card. The dismiss endpoint stores the
    // check_id on the per-server health_dismissals list; the GET
    // handler tags the check with ``dismissed: true`` on the next
    // probe, which _partitionChecks moves to the "Dismissed"
    // subsection.
    async function _toggleCheckDismissal(serverId, serverType, check, dismiss, btn) {
        const checkId = check && check.id;
        if (!checkId) return;
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Working…';
        try {
            const encoded = encodeURIComponent(serverId);
            const suffix = dismiss ? 'dismiss' : 'undismiss';
            const r = await api('POST', `/api/servers/${encoded}/previews-readiness/${suffix}`, { check_id: checkId });
            if (!r.ok) {
                showToast('Dismiss failed', (r.data && r.data.error) || `HTTP ${r.status}`, 'danger');
                btn.disabled = false;
                btn.innerHTML = original;
                return;
            }
            await runReadinessProbe(serverId, serverType);
        } catch (e) {
            showToast('Dismiss error', String(e), 'danger');
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    function _showPlexHelperConfiguration(event) {
        if (event) event.preventDefault();
        const group = document.getElementById('markersPlexAgentGroup');
        activateSection('edit-tab-general');
        if (group) requestAnimationFrame(() => group.scrollIntoView({ block: 'center' }));
    }

    // A note under a row offering a different fix than the row's own
    // (Plex: "Set server-wide to Never" beneath a library whose own marker
    // setting is off). ``check.note``: {text, tooltip, button, action}.
    // The action goes through the same confirm modal as every other one.
    function _renderCheckNote(serverId, serverType, check) {
        const note = check.note;
        const wrap = document.createElement('div');
        wrap.className = 'readiness-note d-flex align-items-start gap-2 mt-2 p-2 rounded';
        const icon = document.createElement('i');
        icon.className = 'bi bi-info-circle text-info mt-1';
        icon.setAttribute('aria-hidden', 'true');
        const body = document.createElement('div');
        body.className = 'flex-grow-1';
        const text = document.createElement('div');
        text.className = 'text-body-secondary';
        text.textContent = note.text;
        body.appendChild(text);
        if (note.configure_plex_helper) {
            const link = document.createElement('a');
            link.href = '#edit-tab-general';
            link.className = 'd-inline-block mt-1 chapter-helper-link';
            link.textContent = 'Configure Plex helper';
            link.addEventListener('click', _showPlexHelperConfiguration);
            body.appendChild(link);
        }
        if (note.action && note.button) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-sm btn-outline-info mt-1 readiness-note-action';
            btn.innerHTML = `<i class="bi bi-toggle-off me-1"></i>${escapeHtml(note.button)}`;
            btn.title = note.tooltip || note.button;
            btn.setAttribute('data-bs-toggle', 'tooltip');
            btn.addEventListener('click', () => _runAction(
                serverId, serverType, check, note.action, btn, `${note.button}: done.`,
            ));
            body.appendChild(btn);
        }
        wrap.append(icon, body);
        return wrap;
    }

    function _makeActionButton(colorCls, iconCls, text, check, direction) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `btn btn-sm ${colorCls} mt-1`;
        btn.innerHTML = `<i class="bi ${iconCls} me-1"></i>${text}`;
        btn.title = `${text} ${check.label || check.id || ''}`;
        btn.dataset.direction = direction;
        return btn;
    }

    // Action dispatcher — driven by check.actions[direction].action.
    // Opens the confirm modal first if action.confirm is non-null;
    // otherwise fires the request immediately. Re-probes after every
    // action completes.
    async function _runCheckAction(serverId, serverType, check, direction, btn) {
        const action = (check.actions || {})[direction];
        if (!action) return;
        await _runAction(serverId, serverType, check, action, btn, `${check.label || 'Setting'} updated.`);
    }

    async function _runAction(serverId, serverType, check, action, btn, successText) {
        const confirm = action.confirm;

        const proceed = async () => {
            const original = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
            try {
                const response = await _dispatchCheckAction(serverId, action);
                if (!response.ok) {
                    showToast('Action failed', response.error || `HTTP ${response.status}`, 'danger');
                    btn.disabled = false;
                    btn.innerHTML = original;
                    return;
                }
                // Re-probe — use convergence polling if the action
                // triggered a Jellyfin restart (install/uninstall).
                if (action.action === 'install_plugin' || action.action === 'uninstall_plugin') {
                    const expected = action.action === 'install_plugin';
                    await reprobeUntilConverged(
                        serverId,
                        serverType,
                        (d) => _pluginInstalledFromEnvelope(d) === expected,
                        { deadlineMs: 90_000, intervalMs: 3_000 },
                    );
                } else if (action.action === 'update_plugin') {
                    // An update can't wait on "the plugin appeared" — it was
                    // already there, so that predicate is true on the first
                    // poll and we'd report success while the server is still
                    // restarting.
                    //
                    // Both halves are needed. "The row is gone" alone is also
                    // true mid-restart (the server can't be read, so the row
                    // can't be built) — and the probe returns a degraded 200,
                    // not an error, so nothing else would keep us polling.
                    // Requiring the plugin to read as installed as well means
                    // we wait for the server to answer again before believing
                    // the row cleared.
                    await reprobeUntilConverged(
                        serverId,
                        serverType,
                        (d) => _pluginInstalledFromEnvelope(d) === true && !_hasFailingCheck(d, check.id),
                        { deadlineMs: 90_000, intervalMs: 3_000 },
                    );
                } else {
                    await runReadinessProbe(serverId, serverType);
                }
                showToast('Applied', successText, 'success');
            } catch (e) {
                showToast('Action error', String(e), 'danger');
                btn.disabled = false;
                btn.innerHTML = original;
            }
        };

        if (confirm) {
            _openConfirmModal(confirm, proceed);
        } else {
            proceed();
        }
    }

    // Map an action envelope to the endpoint it drives. Pure data →
    // URL translation; every check's behaviour is determined by what
    // the server emitted in check.actions.
    async function _dispatchCheckAction(serverId, action) {
        const encoded = encodeURIComponent(serverId);
        const args = action.args || {};
        switch (action.action) {
            case 'apply_flag': {
                // New schema: {"set": [{flag, value, library_ids}]}.
                const row = {
                    flag: args.flag,
                    value: args.value,
                    library_ids: args.library_ids || null,
                };
                // Server-side destructive guardrail: if this flip is
                // destructive (confirm.kind='type'), include the phrase
                // in the body so the route's authoriser passes. Without
                // this the route 400s with "requires typed confirmation".
                const body = { set: [row] };
                if (action.confirm && action.confirm.kind === 'type' && action.confirm.phrase) {
                    body.confirm = { [args.flag]: action.confirm.phrase };
                }
                const r = await api('POST', `/api/servers/${encoded}/health-check/apply`, body);
                return { ok: !!(r.data && r.data.ok !== false) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'install_plugin': {
                const r = await api('POST', `/api/servers/${encoded}/install-plugin`, {});
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'uninstall_plugin': {
                const r = await api('POST', `/api/servers/${encoded}/uninstall-plugin`, {});
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'update_plugin': {
                // Same endpoint: it installs the newest build over the old
                // one. Named apart only so the caller waits for the right
                // thing (see _runCheckAction).
                const r = await api('POST', `/api/servers/${encoded}/install-plugin`, {});
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'sync_trickplay_options': {
                const r = await api('POST', `/api/servers/${encoded}/trickplay-fix-all`, { install_plugin: false });
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'set_vendor_extraction': {
                // Per-library actions carry an optional library_ids
                // array; legacy aggregate actions omit it and apply
                // server-wide. Forward both shapes faithfully so the
                // backend doesn't have to guess.
                const body = { scan_extraction: !!args.scan_extraction };
                if (Array.isArray(args.library_ids) && args.library_ids.length > 0) {
                    body.library_ids = args.library_ids;
                }
                const r = await api('POST', `/api/servers/${encoded}/vendor-extraction`, body);
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'turn_on_plex_library_markers': {
                // One library's own "Intro markers" / "Credits markers"
                // settings back on: while off, Plex hides every skip
                // marker in that library, ours included.
                const r = await api('POST', `/api/servers/${encoded}/plex-library-markers`, {
                    library_id: String(args.library_id || ''),
                    prefs: Array.isArray(args.prefs) ? args.prefs : [],
                });
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'set_plex_detection_never': {
                // Plex's server-wide intro/credits detection to Never:
                // stops its own detection and hides nothing.
                const r = await api('POST', `/api/servers/${encoded}/plex-marker-detection`, {
                    types: Array.isArray(args.types) ? args.types : [],
                });
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'set_plex_loudness_never': {
                // Plex's server-wide loudness analysis to Never.
                const r = await api('POST', `/api/servers/${encoded}/plex-loudness-analysis`, {});
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'set_scheduled_trickplay': {
                // Toggle the Emby/Jellyfin daily Generate-Trickplay-Images
                // scheduled task. Body shape mirrors the backend route:
                // ``{enabled: bool}``. The readiness row builds the
                // recommendation conditionally on plugin state — the
                // dispatcher here just forwards the click.
                const r = await api('POST', `/api/servers/${encoded}/scheduled-trickplay`, {
                    enabled: !!args.enabled,
                });
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            default:
                return { ok: false, error: `Unknown action: ${action.action}`, status: 0 };
        }
    }

    // Open the destructive-toggle confirm modal. `confirm` is the
    // server-supplied blob: {kind: 'button'|'type', phrase, body}.
    // For kind='type', the submit button stays disabled until the
    // user types the exact phrase — defence in depth alongside the
    // backend guardrails.
    function _openConfirmModal(confirm, onConfirm) {
        const modalEl = document.getElementById('readinessConfirmModal');
        if (!modalEl || !window.bootstrap || !window.bootstrap.Modal) {
            // No modal wiring — fall back to native confirm dialog so
            // destructive actions still require explicit acknowledgement.
            if (window.confirm(confirm.body || 'Are you sure?')) onConfirm();
            return;
        }
        const titleEl = document.getElementById('readinessConfirmTitle');
        const bodyEl = document.getElementById('readinessConfirmBody');
        const typeWrap = document.getElementById('readinessConfirmTypeWrap');
        const phraseEl = document.getElementById('readinessConfirmPhrase');
        const inputEl = document.getElementById('readinessConfirmInput');
        const submitBtn = document.getElementById('readinessConfirmSubmit');

        if (titleEl) titleEl.textContent = 'Confirm action';
        // ``confirm.body`` is server-emitted HTML (Python code in the
        // readiness probes). Render it as HTML so ``<code>``,
        // ``<strong>``, ``<br>`` etc. format correctly — pre-fix this
        // used ``textContent`` and users saw literal tag markup in
        // the confirmation modal.
        if (bodyEl) bodyEl.innerHTML = confirm.body || '';
        const kind = confirm.kind || 'button';
        const phrase = confirm.phrase || '';

        // Dispose any prior submit handler by cloning the button
        // BEFORE binding any references — otherwise the input-handler
        // captures the node that's about to be detached and can't
        // toggle the live button in the DOM.
        const newSubmit = submitBtn.cloneNode(true);
        submitBtn.parentNode.replaceChild(newSubmit, submitBtn);

        if (kind === 'type' && phrase) {
            if (typeWrap) typeWrap.classList.remove('d-none');
            if (phraseEl) phraseEl.textContent = phrase;
            if (inputEl) inputEl.value = '';
            newSubmit.disabled = true;
            if (inputEl) {
                inputEl.oninput = () => {
                    newSubmit.disabled = inputEl.value !== phrase;
                };
            }
        } else {
            if (typeWrap) typeWrap.classList.add('d-none');
            newSubmit.disabled = false;
        }

        newSubmit.addEventListener('click', () => {
            window.bootstrap.Modal.getInstance(modalEl).hide();
            onConfirm();
        });

        const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
    }

    // Whether an envelope still carries this check, failing. Used by
    // reprobeUntilConverged to wait out an action whose effect is "this row
    // goes away" rather than a state flip (plugin update).
    function _hasFailingCheck(data, checkId) {
        if (!checkId) return false;
        return ((data && data.sections) || []).some(
            (s) => (s.checks || []).some((c) => c.id === checkId && c.ok === false),
        );
    }

    // Read plugin-installed bit from a unified envelope. Used by
    // reprobeUntilConverged for install/uninstall actions.
    function _pluginInstalledFromEnvelope(data) {
        const sections = (data && data.sections) || [];
        const plugin = sections.find((s) => s.id === 'plugin');
        if (!plugin || !plugin.checks || !plugin.checks.length) return null;
        return plugin.checks[0].current !== 'not installed';
    }

    function _makeSection(title) {
        // No external docs link — see _renderSectionSubhead. Row-level
        // ⓘ icons open the inline explain modal which is the only
        // place rich help text should live.
        const sec = document.createElement('div');
        sec.className = 'mb-3';
        const heading = document.createElement('div');
        heading.className = 'text-muted small text-uppercase fw-bold mb-1 d-flex align-items-center gap-1';
        heading.style.letterSpacing = '0.5px';
        const label = document.createElement('span');
        label.textContent = title;
        heading.appendChild(label);
        sec.appendChild(heading);
        return sec;
    }

    function _makeRow({ ok, severity, label, reason, htmlLabel }) {
        const row = document.createElement('div');
        row.className = 'd-flex align-items-start gap-2 mb-1';
        const icon = ok
            ? '<i class="bi bi-check-circle-fill text-success mt-1"></i>'
            : (severity === 'critical'
                ? '<i class="bi bi-x-circle-fill text-danger mt-1"></i>'
                : '<i class="bi bi-exclamation-triangle-fill text-warning mt-1"></i>');
        const renderedLabel = htmlLabel ? label : escapeHtml(label);
        const detail = reason
            ? `<div class="small text-muted">${escapeHtml(reason)}</div>`
            : '';
        row.innerHTML = `${icon}<div class="flex-grow-1">${renderedLabel}${detail}</div>`;
        return row;
    }

    // Poll /previews-readiness until ``predicate(data)`` holds, or we
    // hit ``deadlineMs``. Used after install/fix actions to reflect the
    // post-action state without racing Jellyfin's ~15-30s restart.
    async function reprobeUntilConverged(serverId, serverType, predicate, { deadlineMs = 60_000, intervalMs = 3_000 } = {}) {
        const start = Date.now();
        while (Date.now() - start < deadlineMs) {
            try {
                const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/previews-readiness`);
                if (r.ok && r.data && predicate(r.data)) {
                    renderReadiness(serverId, serverType, r.data);
                    return r.data;
                }
            } catch (_) {
                // Jellyfin restarting — keep polling.
            }
            await new Promise((res) => setTimeout(res, intervalMs));
        }
        // Deadline hit — render the best-effort current state so the
        // user sees WHY we stopped waiting (e.g. badge flips to
        // "action needed" if install failed).
        await runReadinessProbe(serverId, serverType);
        return null;
    }

    async function runReadinessFixAll(serverId, serverType) {
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixBtn = document.getElementById('editReadinessFixAllBtn');
        const fixResult = document.getElementById('editReadinessFixResult');
        if (!fixCtl || !fixBtn) return;

        const vendor = (serverType || '').toLowerCase();
        const isJellyfin = vendor === 'jellyfin';

        // Figure out whether we actually need to install the plugin.
        // Default: install if Jellyfin AND plugin currently absent per
        // the last-rendered readiness. Legacy checkbox from the old
        // collapse-block is honoured if someone has it ticked.
        const pluginOptIn = document.getElementById('editReadinessPluginOptIn');
        // If the checkbox exists and user explicitly unchecked it,
        // respect that. Otherwise default based on current state.
        let installPlugin = true;
        if (pluginOptIn && pluginOptIn.dataset.userTouched === 'true') {
            installPlugin = !!pluginOptIn.checked;
        }

        const original = fixBtn.innerHTML;
        fixBtn.disabled = true;
        fixBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Fixing…';
        if (fixResult) { fixResult.className = 'small text-muted'; fixResult.textContent = ''; }

        try {
            let r;
            if (isJellyfin) {
                r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/trickplay-fix-all`, {
                    install_plugin: installPlugin,
                });
            } else {
                // Emby + Plex: the only fixable things are the
                // vendor-extraction toggles. Drive those via the
                // existing endpoints.
                r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/health-check/apply`, {});
            }
            if (!r.ok || !r.data) {
                if (fixResult) {
                    fixResult.className = 'small text-danger';
                    fixResult.textContent = `Failed: HTTP ${r.status}`;
                }
                return;
            }
            const allOk = !!r.data.ok;
            if (fixResult) {
                if (allOk) {
                    fixResult.className = 'small text-success';
                    fixResult.textContent = installPlugin && isJellyfin
                        ? '✓ Fix applied. Waiting for Jellyfin restart…'
                        : '✓ Fix applied. Re-probing…';
                } else {
                    fixResult.className = 'small text-warning';
                    fixResult.textContent = `Some steps failed: ${escapeHtml(r.data.error || 'see logs')}`;
                }
            }
            // Re-probe with convergence polling. If we requested a plugin
            // install, wait until plugin.installed=true (Jellyfin takes
            // 15-30s to restart). Otherwise just reflect the current
            // state immediately.
            if (isJellyfin && installPlugin) {
                await reprobeUntilConverged(
                    serverId,
                    serverType,
                    (d) => _pluginInstalledFromEnvelope(d) === true,
                    { deadlineMs: 90_000, intervalMs: 3_000 },
                );
            } else {
                await runReadinessProbe(serverId, serverType);
            }
        } finally {
            fixBtn.disabled = false;
            fixBtn.innerHTML = original;
        }
    }

    // Legacy health-check probe kept for back-compat; new Edit modal
    // flow uses runReadinessProbe instead.
    async function runHealthCheckProbe(serverId) {
        // Deprecated — no-op. Left in place so external callers (if any)
        // don't raise ReferenceError.
        void serverId;
    }

    function formatHealthValue(v) {
        if (v === true) return 'On';
        if (v === false) return 'Off';
        if (v === null || v === undefined) return '—';
        // Objects (e.g. Jellyfin's TrickplayOptions geometry dict) don't
        // fit a one-cell value; render a compact JSON-ish summary so the
        // diff still surfaces *something* informative without breaking
        // the layout. Falls through to escapeHtml(String(v)) for
        // strings/numbers.
        if (typeof v === 'object') {
            try {
                return escapeHtml(JSON.stringify(v));
            } catch (_e) {
                return escapeHtml(String(v));
            }
        }
        return escapeHtml(String(v));
    }

    // Render the side-by-side current/recommended diff for a check row.
    // Empty string when there's no value to show (info-only checks with
    // current=null and recommended=null).
    //
    // - When recommended is null/undefined → only render the current value.
    // - When current === recommended (passing) → single neutral pill.
    // - When current !== recommended (failing) → two-column grid with the
    //   current value tinted red and the recommended tinted green so the
    //   mismatch jumps out at a glance. Replaces the prior `Currently <X>`
    //   one-liner that left users guessing whether <X> was good or bad.
    function _renderValueDiff(current, recommended, ok, label) {
        const hasCurrent = !(current === null || current === undefined);
        const hasRecommended = !(recommended === null || recommended === undefined);
        if (!hasCurrent && !hasRecommended) return '';

        // Avoid the "Plex 1.43.2.10687 / Currently 1.43.2.10687" double-up.
        // For info-only rows where the label already names the current
        // value, skip the Currently line — the label is the current value.
        if (
            !hasRecommended
            && hasCurrent
            && typeof current === 'string'
            && typeof label === 'string'
            && label.includes(current)
        ) {
            return '';
        }

        // Object-typed values (Jellyfin's TrickplayOptions geometry dict
        // and similar) don't fit a one-line pill. Pre-redesign rendered
        // them as a giant single-line JSON code-block that overflowed
        // the row. Now: when passing, hide the dump entirely (the user
        // already sees ✓ + "matches recommended"); when failing, show
        // a compact "see details" hint pointing at the ⓘ explanation.
        const currentIsObject = hasCurrent && typeof current === 'object';
        const recommendedIsObject = hasRecommended && typeof recommended === 'object';
        const isObjectRow = currentIsObject || recommendedIsObject;

        if (!hasRecommended) {
            if (isObjectRow) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<span class="text-body-tertiary">Current state — open ⓘ for details</span></div>`;
            }
            return `<div class="readiness-currently text-muted mt-1">`
                + `Currently <code>${formatHealthValue(current)}</code></div>`;
        }
        if (!hasCurrent) {
            if (isObjectRow) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<span class="text-body-tertiary">Recommended state — open ⓘ for details</span></div>`;
            }
            return `<div class="readiness-currently text-muted mt-1">`
                + `Recommended <code>${formatHealthValue(recommended)}</code></div>`;
        }

        // Both present.
        const matched = ok || _valuesEqual(current, recommended);
        if (matched) {
            if (isObjectRow) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<i class="bi bi-check2 me-1"></i>Matches recommended — open ⓘ for details</div>`;
            }
            // Tautology guard: on a passing row where current and
            // recommended display IDENTICALLY as non-toggle values
            // (e.g. "Currently reachable — recommended reachable",
            // "Currently installed — recommended installed"), drop
            // the redundant "— recommended X" tail. The recommended
            // value is information only when there's a meaningful
            // OTHER state — true for bool toggles ("we recommend On"
            // tells you the direction), useless for unary status
            // strings where the only valid value IS the recommended
            // one. Bools keep the tail so the user can still see
            // which way the recommendation points.
            const sameDisplay = formatHealthValue(current) === formatHealthValue(recommended);
            const isBoolToggle = typeof current === 'boolean' && typeof recommended === 'boolean';
            if (sameDisplay && !isBoolToggle) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<i class="bi bi-check2 me-1"></i>Currently <code>${formatHealthValue(current)}</code></div>`;
            }
            return `<div class="readiness-currently text-muted mt-1">`
                + `<i class="bi bi-check2 me-1"></i>Currently <code>${formatHealthValue(current)}</code> `
                + `<span class="text-body-tertiary">— recommended <code>${formatHealthValue(recommended)}</code></span></div>`;
        }

        if (isObjectRow) {
            // Mismatch on a structured value — pointing at ⓘ keeps the row
            // scannable, and the rich explanation modal already carries
            // the field-by-field breakdown.
            return `<div class="readiness-currently text-muted mt-1">`
                + `<span class="text-danger-emphasis">Doesn't match recommended</span> `
                + `— open ⓘ for details</div>`;
        }

        return `<div class="readiness-values d-flex flex-wrap gap-2 mt-1">`
            + `<div class="readiness-current px-2 py-1 rounded">`
            +   `<span class="readiness-label">Currently</span> `
            +   `<code>${formatHealthValue(current)}</code></div>`
            + `<div class="readiness-arrow text-body-tertiary align-self-center">→</div>`
            + `<div class="readiness-recommended px-2 py-1 rounded">`
            +   `<span class="readiness-label">Recommended</span> `
            +   `<code>${formatHealthValue(recommended)}</code></div></div>`;
    }

    // Loose equality for the value-diff renderer — treats true/false
    // distinctly from "true"/"false" strings, but treats `1 == "1"` as
    // equal because the underlying probes sometimes return numeric
    // strings while the recommended value is a number.
    function _valuesEqual(a, b) {
        if (a === b) return true;
        if (a === null || b === null) return false;
        if (typeof a === 'object' || typeof b === 'object') {
            try { return JSON.stringify(a) === JSON.stringify(b); } catch (_e) { return false; }
        }
        // eslint-disable-next-line eqeqeq
        return a == b;
    }

    // Pick which of (enable | disable) is the "fix" for this check —
    // i.e. the action whose post-condition matches check.recommended.
    // Falls back to whichever single action exists when there's no
    // boolean target (install_plugin / sync_trickplay_options).
    //
    // The fix-direction matters: getting it backwards on Plex's BIF
    // generation rows would TURN ON Plex's own preview generation
    // (the opposite of what the user clicked "Fix" for) and silently
    // burn duplicate CPU on every library scan.
    function _pickFixAction(check) {
        const a = check.actions || {};
        // Explicit backend hint wins. Required for rows where
        // ``recommended`` is a descriptive string (e.g. the
        // scheduled-trickplay row) and the boolean fallback would
        // pick the wrong direction. Same hint that ``_renderCheckRow``
        // reads so the per-row "Apply recommended" button and the
        // bulk "Apply all" button always agree.
        if (typeof check.fix_action === 'string' && a[check.fix_action]) {
            return a[check.fix_action];
        }
        // Boolean-recommended fallback for legacy rows (apply_flag,
        // set_vendor_extraction). args.value covers apply_flag;
        // args.scan_extraction covers set_vendor_extraction.
        const direction = check.recommended ? 'enable' : 'disable';
        const matched = a[direction];
        if (matched) {
            const args = matched.args || {};
            if (args.value !== undefined && args.value === check.recommended) return matched;
            if (args.scan_extraction !== undefined && args.scan_extraction === check.recommended) return matched;
            // No discriminating arg — assume convention holds (enable
            // moves toward "more on / installed", disable the opposite).
            return matched;
        }
        // Single-direction actions (install_plugin etc.) have no opposite.
        return a.enable || a.disable || null;
    }

    // Walk every section's checks and assemble a fix plan ordered for
    // execution. Items are scoped to:
    //   'critical' — only severity=critical failing checks
    //   'all'      — every failing check that has an automatic fix
    //
    // Manual rows (no enable/disable action — version upgrades,
    // skipped custom-agent libraries) are excluded — they need user
    // intervention outside this app.
    function _buildFixPlan(sections, scope) {
        const plan = [];
        for (const section of sections || []) {
            for (const check of (section.checks || [])) {
                if (check.ok !== false) continue;
                if (scope === 'critical' && check.severity !== 'critical') continue;
                // ``bulk: false``: a fix whose reach goes beyond its row
                // (Plex's server-wide Never) is only applied from its own
                // button, after its own confirmation.
                if (check.bulk === false) continue;
                const fixAction = _pickFixAction(check);
                if (!fixAction) continue;
                plan.push({
                    check,
                    fixAction,
                    sectionTitle: section.title || section.id || '',
                });
            }
        }
        return plan;
    }

    // Open the bulk fix-plan modal. Preview lists every change about to
    // happen (label + current → recommended). On Apply: dispatches each
    // item through _dispatchCheckAction sequentially, then re-probes.
    //
    // Sequential not parallel — installing a Jellyfin plugin restarts
    // the server, so subsequent calls have to wait. Parallel would
    // race the restart and half the calls would 502.
    async function _runFixPlanWithPreview(serverId, serverType, plan, label) {
        if (!plan.length) return;
        const modalEl = document.getElementById('readinessFixPlanModal');
        const titleEl = document.getElementById('readinessFixPlanTitle');
        const introEl = document.getElementById('readinessFixPlanIntro');
        const listEl = document.getElementById('readinessFixPlanList');
        const applyBtn = document.getElementById('readinessFixPlanApplyBtn');
        const cancelBtn = document.getElementById('readinessFixPlanCancelBtn');
        const resultsEl = document.getElementById('readinessFixPlanResults');
        if (!modalEl || !titleEl || !listEl || !applyBtn) return;

        titleEl.textContent = label;
        introEl.textContent = `This will change ${plan.length} setting${plan.length === 1 ? '' : 's'} on the media server. Review the list and click Apply to proceed.`;
        listEl.innerHTML = '';
        for (const item of plan) {
            const li = document.createElement('li');
            li.className = 'list-group-item d-flex justify-content-between align-items-start gap-2';
            const cur = formatHealthValue(item.check.current);
            const rec = formatHealthValue(item.check.recommended);
            li.innerHTML = `<div class="flex-grow-1">`
                + `<div class="fw-semibold">${escapeHtml(item.check.label || item.check.id || '')}</div>`
                + `<div class="text-body-secondary small">${escapeHtml(item.sectionTitle)}</div>`
                + `</div>`
                + `<div class="text-end small">`
                +   `<code class="text-danger-emphasis">${cur}</code> `
                +   `<span class="text-body-tertiary">→</span> `
                +   `<code class="text-success-emphasis">${rec}</code>`
                + `</div>`;
            listEl.appendChild(li);
        }
        if (resultsEl) {
            resultsEl.classList.add('d-none');
            resultsEl.innerHTML = '';
        }
        applyBtn.disabled = false;
        applyBtn.innerHTML = `<i class="bi bi-magic me-1"></i>Apply ${plan.length}`;

        const modal = window.bootstrap && window.bootstrap.Modal
            ? window.bootstrap.Modal.getOrCreateInstance(modalEl)
            : null;
        if (!modal) return;

        // Wire one-shot Apply handler. Replace the button element to
        // detach any previous listeners — calling .show() with stale
        // listeners would trigger every prior click handler (the modal
        // is reused across Critical/All/Per-server invocations).
        const newApplyBtn = applyBtn.cloneNode(true);
        applyBtn.parentNode.replaceChild(newApplyBtn, applyBtn);
        newApplyBtn.addEventListener('click', async () => {
            const opening = modalOpening(modalEl);
            newApplyBtn.disabled = true;
            if (cancelBtn) cancelBtn.disabled = true;
            newApplyBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
            const outcomes = [];
            for (let i = 0; i < plan.length; i++) {
                const item = plan[i];
                try {
                    const res = await _dispatchCheckAction(serverId, item.fixAction);
                    outcomes.push({ item, ok: !!res.ok, error: res.error });
                } catch (exc) {
                    outcomes.push({ item, ok: false, error: String(exc) });
                }
            }
            const okCount = outcomes.filter((o) => o.ok).length;
            const failCount = outcomes.length - okCount;
            if (resultsEl) {
                resultsEl.classList.remove('d-none');
                const cls = failCount === 0 ? 'text-success' : (okCount === 0 ? 'text-danger' : 'text-warning');
                let html = `<div class="${cls} mb-2"><strong>${okCount}/${outcomes.length} applied</strong>`;
                if (failCount > 0) html += ` (${failCount} failed)`;
                html += `</div>`;
                if (failCount > 0) {
                    html += '<ul class="mb-0 ps-3">';
                    for (const o of outcomes) {
                        if (o.ok) continue;
                        html += `<li>${escapeHtml(o.item.check.label || o.item.check.id || '')}: ${escapeHtml(o.error || 'failed')}</li>`;
                    }
                    html += '</ul>';
                }
                resultsEl.innerHTML = html;
            }
            newApplyBtn.innerHTML = '<i class="bi bi-check2 me-1"></i>Done';
            // Re-probe so the readiness card reflects post-apply truth.
            try { await runReadinessProbe(serverId, serverType); } catch (_e) { /* swallow */ }
            // Auto-close after a short pause so the user sees the result
            // confirmation. Keep the modal open if there were failures so
            // the user can read the per-failure detail.
            if (failCount === 0) {
                setTimeout(() => hideModalSafely(modalEl, opening), 1200);
            }
            if (cancelBtn) {
                cancelBtn.disabled = false;
                cancelBtn.textContent = 'Close';
            }
        });

        modal.show();
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function escapeAttr(s) {
        return escapeHtml(s);
    }

    async function applyHealthFixes(serverId) {
        const fixCtl = document.getElementById('editHealthFixControls');
        const fixBtn = document.getElementById('editHealthFixAllBtn');
        const fixResult = document.getElementById('editHealthFixResult');
        if (!fixCtl || !fixBtn) return;

        const original = fixBtn.innerHTML;
        fixBtn.disabled = true;
        fixBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
        if (fixResult) { fixResult.className = 'small text-muted'; fixResult.textContent = ''; }

        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/health-check/apply`, {});
            if (!r.ok || !r.data) {
                if (fixResult) {
                    fixResult.className = 'small text-danger';
                    fixResult.textContent = `Failed: HTTP ${r.status}`;
                }
                return;
            }
            const allOk = !!r.data.ok;
            const okCount = Object.values(r.data.results || {}).filter((v) => v === 'ok').length;
            const errCount = Object.values(r.data.results || {}).filter((v) => v !== 'ok').length;
            if (fixResult) {
                if (allOk) {
                    fixResult.className = 'small text-success';
                    fixResult.textContent = `✓ Applied ${okCount} setting${okCount === 1 ? '' : 's'}`;
                } else if (okCount > 0) {
                    fixResult.className = 'small text-warning';
                    fixResult.textContent = `Applied ${okCount}, ${errCount} failed — see logs`;
                } else {
                    fixResult.className = 'small text-danger';
                    fixResult.textContent = `Failed: ${errCount} error${errCount === 1 ? '' : 's'}`;
                }
            }
            // Re-probe so the panel reflects the new state.
            runHealthCheckProbe(serverId);
        } finally {
            fixBtn.disabled = false;
            fixBtn.innerHTML = original;
        }
    }

    // ─── Media Preview Bridge plugin status / install ──────────────────
    // Drives the Jellyfin-only "Media Preview Bridge plugin" card in the
    // Edit Server modal. Visible only when the connection succeeded AND
    // the server is Jellyfin.
    function updateJellyfinPluginPanel(plugin) {
        const group = document.getElementById('editJellyfinPluginGroup');
        const badge = document.getElementById('editJellyfinPluginBadge');
        const installBtn = document.getElementById('editInstallPluginBtn');
        if (!group || !badge) return;

        if (!plugin) {
            // Non-Jellyfin or connection failed — hide the panel entirely.
            group.classList.add('d-none');
            return;
        }
        group.classList.remove('d-none');
        if (plugin.installed) {
            badge.className = 'badge bg-success ms-1';
            badge.textContent = `installed${plugin.version ? ` · v${plugin.version}` : ''}`;
            if (installBtn) installBtn.classList.add('d-none');
        } else {
            badge.className = 'badge bg-warning text-dark ms-1';
            badge.textContent = 'not installed';
            if (installBtn) installBtn.classList.remove('d-none');
        }
    }

    async function installJellyfinPlugin() {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const btn = document.getElementById('editInstallPluginBtn');
        const result = document.getElementById('editInstallPluginResult');
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Installing…';
        result.className = 'small text-muted';
        result.textContent = 'Adding repo, queuing install, requesting Jellyfin restart…';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/install-plugin`);
            const data = r.data || {};
            if (!data.ok) {
                result.className = 'small text-danger';
                result.innerHTML = `<i class="bi bi-x-circle me-1"></i>${escapeHtml(data.error || 'Install failed')}`;
                return;
            }
            result.className = 'small text-info';
            result.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Jellyfin restarting — polling for plugin (up to 60s)…';

            // Poll the test-connection endpoint every 3s; flip the badge
            // when it reports plugin.installed=true. 60s deadline matches
            // a typical Jellyfin restart on a small install.
            const deadline = Date.now() + 60_000;
            while (Date.now() < deadline) {
                await new Promise((res) => setTimeout(res, 3000));
                try {
                    const probe = await api('POST', `/api/servers/${encodeURIComponent(id)}/test-connection`);
                    const probePlugin = probe.data && probe.data.plugin;
                    if (probePlugin && probePlugin.installed) {
                        updateJellyfinPluginPanel(probePlugin);
                        result.className = 'small text-success';
                        result.innerHTML = `<i class="bi bi-check-circle me-1"></i>Plugin installed (v${escapeHtml(probePlugin.version || '?')}) — Jellyfin will now register published trickplay instantly.`;
                        return;
                    }
                } catch (_) {
                    // Keep polling — Jellyfin may still be down mid-restart.
                }
            }
            result.className = 'small text-warning';
            result.innerHTML = '<i class="bi bi-clock-history me-1"></i>Restart taking longer than expected. Click Test Connection in a minute to re-check the plugin status.';
        } catch (e) {
            result.className = 'small text-danger';
            result.textContent = String(e);
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    function copyPluginRepoUrl() {
        const input = document.getElementById('editJellyfinPluginRepoUrl');
        if (!input) return;
        navigator.clipboard.writeText(input.value).then(
            () => showToast('Copied', 'Plugin repo URL copied to clipboard.', 'success'),
            () => {
                input.select();
                document.execCommand('copy');
                showToast('Copied', 'Plugin repo URL copied (fallback).', 'success');
            }
        );
    }

    async function refreshLibrariesFromModal(btn) {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Refreshing…';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/refresh-libraries`);
            if (!r.ok) {
                showToast('Refresh failed', `${(r.data && r.data.error) || r.status}`, 'danger');
                return;
            }
            // Re-fetch the server payload so the Libraries tab repaints with
            // fresh names without forcing the user to close and re-open.
            // /api/servers/<id> returns the server dict directly (matches
            // openEditModal's `const server = singleR.data;` earlier in this
            // file) — not wrapped under `.server`.
            const fresh = await api('GET', `/api/servers/${encodeURIComponent(id)}`);
            if (fresh.ok && fresh.data) {
                renderEditLibraries(fresh.data.libraries || []);
                if (window.renderMarkersLibraryColumn) window.renderMarkersLibraryColumn();
                if (window.renderLoudnessLibraryColumn) window.renderLoudnessLibraryColumn();
                // D23 — sync the cached server payload so saveEditedServer
                // sees the freshly-fetched libraries, not the stale [] it
                // captured at modal open. Without this, ticking checkboxes
                // and clicking Save would write libraries=[] to the
                // server (because (server.libraries || []).map(...) is []).
                if (_editState && _editState.server) {
                    _editState.server.libraries = fresh.data.libraries || [];
                }
            }
            // Also refresh the cards on the page (counts changed).
            loadServers();
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    // Dirty tracking, the discard guard, the footer, Ctrl/Cmd+Enter, the header switch and the credentials toggle.
    function wireEditModalChrome() {
        const modalEl = document.getElementById('editServerModal');
        if (!modalEl) return;
        // Controls whose changes are never part of the PUT: the section select, the readiness card (it acts on the
        // vendor directly), the webhook URL (used only by Register) and the re-auth method pickers.
        const NOT_SAVED = '#editServerSectionSelect, #editReadinessGroup, #plexWebhookPublicUrl, '
            + 'input[name="editReauthJfMethod"], input[name="editReauthEmbyMethod"]';
        const onEdit = (event) => {
            const t = event.target;
            if (!t || !t.matches || !t.matches('input, select, textarea') || t.closest(NOT_SAVED)) return;
            setEditDirty(true);
            if (t.closest('#edit-tab-paths, #edit-tab-excludes, #edit-tab-libraries')) updateEditBadges();
            if (t.id === 'editServerUrl') validateEditUrl();
            if (t.id === 'editServerEnabled') modalEl.classList.toggle('sm-paused', !t.checked);
        };
        modalEl.addEventListener('input', onEdit);
        modalEl.addEventListener('change', onEdit);

        // Close, Esc, backdrop and Cancel all end up in hide(); while there are edits, ask first (inline, in the footer).
        modalEl.addEventListener('hide.bs.modal', (event) => {
            // A save in flight has already taken the edits, so closing then has nothing to discard.
            const saving = !!(document.getElementById('editServerSave') || {}).dataset.busy;
            if (event.target !== modalEl || !_editDirty || _editDiscardOk || saving) return;
            event.preventDefault();
            const bar = document.getElementById('editServerDiscardBar');
            if (bar) {
                bar.classList.remove('d-none');
                const keep = document.getElementById('editServerKeepEditing');
                if (keep) keep.focus();
            }
        });
        modalEl.addEventListener('hidden.bs.modal', (event) => {
            if (event.target !== modalEl) return;
            _editDirty = false;
            _editDiscardOk = false;
            if (_editReauthQcPoll) { clearInterval(_editReauthQcPoll); _editReauthQcPoll = null; }
        });
        const keepBtn = document.getElementById('editServerKeepEditing');
        if (keepBtn) keepBtn.addEventListener('click', () => document.getElementById('editServerDiscardBar').classList.add('d-none'));
        const discardBtn = document.getElementById('editServerDiscardConfirm');
        if (discardBtn) {
            discardBtn.addEventListener('click', () => {
                _editDiscardOk = true;
                window.bootstrap.Modal.getOrCreateInstance(modalEl).hide();
            });
        }

        modalEl.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey)) return;
            const save = document.getElementById('editServerSave');
            if (_editDirty && save && !save.disabled) {
                event.preventDefault();
                save.click();
            }
        });

        const credToggle = document.getElementById('editCredToggle');
        if (credToggle) {
            credToggle.addEventListener('click', () => setCredFormsOpen(credToggle.getAttribute('aria-expanded') !== 'true'));
        }
        const deleteBtn = document.getElementById('editDeleteServerBtn');
        if (deleteBtn) {
            deleteBtn.addEventListener('click', async () => {
                if (!_editState) return;
                const { server } = _editState;
                if (await deleteServerWithConfirm(server.id, server.name || server.id)) {
                    _editDiscardOk = true;
                    setEditDirty(false);
                    window.bootstrap.Modal.getOrCreateInstance(modalEl).hide();
                }
            });
        }

        // The rail's webhook dot follows the two status badges the Plex webhook panel keeps up to date.
        if (window.MutationObserver) {
            const observer = new MutationObserver(updateAutomationDot);
            ['plexWebhookStatusBadge', 'recentlyAddedStatusBadge'].forEach((id) => {
                const el = document.getElementById(id);
                if (el) observer.observe(el, { childList: true, characterData: true, subtree: true });
            });
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        // The Edit Server modal lives only on /servers. servers.js is also
        // loaded on /setup (for MPGShared exports the wizard depends on),
        // so bail when the modal-specific elements aren't on this page —
        // otherwise the very first .addEventListener throws TypeError on
        // null and halts every JS handler that runs after this script,
        // including the wizard's vendor-button click bindings.
        const editServerSaveBtn = document.getElementById('editServerSave');
        if (!editServerSaveBtn) return;
        $('#editAddPathMapping').addEventListener('click', () => addPathMappingRow());
        $('#editAddExcludePath').addEventListener('click', () => addExcludePathRow());
        editServerSaveBtn.addEventListener('click', saveEditedServer);
        $('#editApplyPathMappingsAll').addEventListener('click', (ev) =>
            applyListToAllServers('path_mappings', readPathMappingsFromForm, ev.currentTarget)
        );
        $('#editApplyExcludePathsAll').addEventListener('click', (ev) =>
            applyListToAllServers('exclude_paths', readExcludePathsFromForm, ev.currentTarget)
        );
        const refreshBtn = document.getElementById('editRefreshLibrariesBtn');
        if (refreshBtn) refreshBtn.addEventListener('click', (ev) => refreshLibrariesFromModal(ev.currentTarget));
        const testConnBtn = document.getElementById('editTestConnectionBtn');
        if (testConnBtn) testConnBtn.addEventListener('click', testEditConnection);
        // Unified "Previews readiness" card (v3). Both Fix buttons go
        // through the preview modal so the user sees exactly what's
        // about to change before committing — no more silent flips.
        const readinessFixBtn = document.getElementById('editReadinessFixAllBtn');
        if (readinessFixBtn) readinessFixBtn.addEventListener('click', async () => {
            const id = (_editState && _editState.server && _editState.server.id) || '';
            const type = (_editState && _editState.server && _editState.server.type) || '';
            if (!id) return;
            // Look up by the in-flight serverId so a stale probe from a
            // previously-open modal can't leak into this one.
            const data = _readinessDataByServer.get(id);
            if (!data) return;
            const plan = _buildFixPlan(data.sections || [], 'all');
            if (!plan.length) return;
            await _runFixPlanWithPreview(id, type, plan, 'Apply all recommended fixes');
        });
        const readinessFixCriticalBtn = document.getElementById('editReadinessFixCriticalBtn');
        if (readinessFixCriticalBtn) readinessFixCriticalBtn.addEventListener('click', async () => {
            const id = (_editState && _editState.server && _editState.server.id) || '';
            const type = (_editState && _editState.server && _editState.server.type) || '';
            if (!id) return;
            const data = _readinessDataByServer.get(id);
            if (!data) return;
            const plan = _buildFixPlan(data.sections || [], 'critical');
            if (!plan.length) return;
            await _runFixPlanWithPreview(id, type, plan, 'Fix critical issues only');
        });
        const readinessRecheckBtn = document.getElementById('editReadinessRecheckBtn');
        if (readinessRecheckBtn) readinessRecheckBtn.addEventListener('click', () => {
            const id = (_editState && _editState.server && _editState.server.id) || '';
            const type = (_editState && _editState.server && _editState.server.type) || '';
            if (id) runReadinessProbe(id, type);
        });
        // Plugin opt-out warning — show when the user unticks the checkbox.
        const pluginOptIn = document.getElementById('editReadinessPluginOptIn');
        const pluginOptOutWarning = document.getElementById('editReadinessPluginOptOutWarning');
        if (pluginOptIn && pluginOptOutWarning) {
            pluginOptIn.addEventListener('change', () => {
                pluginOptOutWarning.classList.toggle('d-none', pluginOptIn.checked);
            });
        }

        // D24 — vendor-aware re-auth wiring inside the Edit modal.
        document.querySelectorAll('input[name="editReauthJfMethod"]').forEach((r) =>
            r.addEventListener('change', _onEditReauthMethodChange));
        document.querySelectorAll('input[name="editReauthEmbyMethod"]').forEach((r) =>
            r.addEventListener('change', _onEditReauthMethodChange));
        const jfQcBtn = document.getElementById('editReauthJfQcStart');
        if (jfQcBtn) jfQcBtn.addEventListener('click', _editReauthStartQuickConnect);
        const jfPwBtn = document.getElementById('editReauthJfPwSubmit');
        if (jfPwBtn) jfPwBtn.addEventListener('click', () => _editReauthVerifyPassword('jellyfin'));
        const embyPwBtn = document.getElementById('editReauthEmbyPwSubmit');
        if (embyPwBtn) embyPwBtn.addEventListener('click', () => _editReauthVerifyPassword('emby'));

        const plexBrowseBtn = document.getElementById('editPlexConfigBrowseBtn');
        if (plexBrowseBtn) {
            plexBrowseBtn.addEventListener('click', () => {
                const cfgInput = document.getElementById('editPlexConfigFolder');
                const start = ((cfgInput && cfgInput.value) || '').trim() || '/';
                window.openFolderPicker(start, (picked) => {
                    cfgInput.value = picked;
                    _validateLocalPathInput(cfgInput);
                });
            });
        }

        // Jellyfin off-media: reveal the config-folder field when the toggle
        // is on, and wire its folder-browse button (mirrors the Plex one).
        const offMediaToggle = document.getElementById('editJellyfinSaveOffMedia');
        if (offMediaToggle) {
            offMediaToggle.addEventListener('change', () => {
                const grp = document.getElementById('editJellyfinConfigFolderGroup');
                if (grp) grp.classList.toggle('d-none', !offMediaToggle.checked);
            });
        }
        const jfBrowseBtn = document.getElementById('editJellyfinConfigBrowseBtn');
        if (jfBrowseBtn) {
            jfBrowseBtn.addEventListener('click', () => {
                const cfgInput = document.getElementById('editJellyfinConfigFolder');
                const start = ((cfgInput && cfgInput.value) || '').trim() || '/';
                window.openFolderPicker(start, (picked) => {
                    cfgInput.value = picked;
                    _validateLocalPathInput(cfgInput);
                });
            });
        }

        wireEditModalChrome();
    });

    // Public surface for /setup wizard (and any other page) that needs the
    // same path-mapping row + path-validation behaviour without duplicating
    // the IIFE-private helpers.
    window.MPGShared = window.MPGShared || {};
    window.MPGShared.validateLocalPathInput = _validateLocalPathInput;
    window.MPGShared.debouncedValidatePath = _debouncedValidatePath;
    window.MPGShared.addPathMappingRow = addPathMappingRow;
    // Quote-safe (attribute values too); markers_server_tab.js renders with it.
    window.MPGShared.escapeHtml = escapeHtml;
    // Used by the /setup wizard's vendor picker to enter the inlined
    // connection form at "step-connect" without going through #step-type
    // (which only exists in the modal).
    window.MPGShared.pickVendor = pickVendorAndAdvance;
    window.MPGShared.resetServerWizard = resetWizard;
    window.MPGShared.activateSection = activateSection;
})();
