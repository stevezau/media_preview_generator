// Servers → Edit → "Intro & Credits" tab: this server's switch, library selection, capability status and the
// Plex database-write confirmation. servers.js calls the window globals exported at the bottom.

(function () {
    'use strict';

    // Jellyfin restarts after a plugin install, so the status is checked again once it's likely back.
    const INSTALL_RECHECK_MS = 20000;
    // Mirrors markers.settings.is_sports_library so the pills show before the status check returns; the status
    // response's default_selected replaces it when it arrives.
    const SPORTS_NAME_RE = /\bsports?\b/i;

    const $ = (sel, el) => (el || document).querySelector(sel);
    const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

    const tab = {
        server: null,
        status: null,
        loadSeq: 0,
        librariesTouched: false,
        pendingConfirmation: null,
    };

    function esc(value) {
        const shared = window.MPGShared && window.MPGShared.escapeHtml;
        const text = value == null ? '' : String(value);
        if (shared) return shared(text);
        return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function csrfHeaders() {
        return { 'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '' };
    }

    function vendorOf(server) {
        return String((server && server.type) || '').toLowerCase();
    }

    function isPlex(server) {
        return vendorOf(server) === 'plex';
    }

    function storedMarkers(server) {
        const markers = server && server.markers;
        return markers && typeof markers === 'object' ? markers : {};
    }

    function storedPlex(server) {
        const plex = storedMarkers(server).plex;
        return plex && typeof plex === 'object' ? plex : {};
    }

    // ---------- status block ---------------------------------------------------

    function infoIcon(title) {
        return `<button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="${esc(title)}"><i class="bi bi-info-circle"></i></button>`;
    }

    function badge(tone, text, extraClass) {
        const tones = {
            ok: 'bg-success-subtle text-success-emphasis border border-success-subtle',
            warn: 'bg-warning-subtle text-warning-emphasis border border-warning-subtle',
            bad: 'bg-danger-subtle text-danger-emphasis border border-danger-subtle',
            off: 'bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle',
        };
        return `<span class="badge ${tones[tone]}${extraClass ? ` ${extraClass}` : ''}">${esc(text)}</span>`;
    }

    function kvRow(label, valueHtml) {
        return `<div class="markers-kv-label">${esc(label)}</div><div>${valueHtml}</div>`;
    }

    function kvGrid(rows) {
        return `<div class="markers-kv small">${rows.join('')}</div>`;
    }

    // The same-host-path advice may already be in the message; the hint is only added when it isn't.
    const SAME_HOST_ADVICE_RE = /same[ -]host path|unraid/i;

    function warningLine(capability) {
        if (!capability.message) return '';
        const rawHint = (capability.details || {}).hint;
        const hint = rawHint && !SAME_HOST_ADVICE_RE.test(capability.message) ? rawHint : '';
        return `<div class="alert alert-warning small text-break py-2 mb-0 mt-3" role="status">
            <i class="bi bi-exclamation-triangle me-1"></i>${esc(capability.message)}
            ${hint ? `<div class="mt-1">${esc(hint)}</div>` : ''}
        </div>`;
    }

    function canShowText(types) {
        const words = (types || []).map(String);
        if (!words.length) return '—';
        words[0] = words[0].charAt(0).toUpperCase() + words[0].slice(1);
        return words.join(' · ');
    }

    // True/false when Plex reported its detection prefs; null when it couldn't be read (row left out).
    function plexDetectionOn(detection) {
        if (!detection || typeof detection !== 'object') return null;
        const values = [detection.intro, detection.credits];
        if (values.every((v) => v == null)) return null;
        return values.some((v) => v != null && v !== 'never');
    }

    // The status payload has no "local" flag. For needs_local_db: lock_holder is absent (network/unrecognised
    // filesystem) or anything but false → network share; lock_holder === false → local disk, wrong path.
    function plexDbOnNetworkShare(capability) {
        const details = capability.details || {};
        return capability.state === 'needs_local_db' && details.lock_holder !== false;
    }

    function dirname(path) {
        const text = String(path || '');
        const cut = text.lastIndexOf('/');
        return cut > 0 ? text.slice(0, cut) : text;
    }

    function renderPlexStatus(status) {
        const capability = status.capability || {};
        const details = capability.details || {};
        const rows = [
            kvRow(
                'How markers get here',
                `Written into this Plex server's database${infoIcon('Plex has no API or plugins for this. Only on the same machine as Plex.')}`,
            ),
        ];
        if (details.plex_pass === true) rows.push(kvRow('Plex Pass', badge('ok', '✓ Active')));
        if (details.plex_pass === false) rows.push(kvRow('Plex Pass', badge('bad', '✕ Not active')));
        if (details.db_path) {
            const disk = plexDbOnNetworkShare(capability)
                ? badge('bad', `✕ network share${details.fs_type ? ` (${details.fs_type})` : ''}`)
                : badge('ok', '✓ local disk');
            rows.push(kvRow(
                'Database location',
                `<span class="font-monospace text-break me-1">${esc(dirname(details.db_path))}</span>${disk}`,
            ));
        }
        const detectionOn = plexDetectionOn(details.detection);
        if (detectionOn === true) {
            rows.push(kvRow(
                "Plex's own detection",
                `${badge('warn', 'On', 'markers-detection')} <span class="text-muted">it can replace ours; we put them back</span>`,
            ));
        } else if (detectionOn === false) {
            rows.push(kvRow("Plex's own detection", badge('off', 'Off', 'markers-detection')));
        }
        return kvGrid(rows) + (capability.state === 'ready' ? '' : warningLine(capability));
    }

    function installButton(label) {
        return `<button type="button" class="btn btn-sm btn-primary ms-2 py-0" id="markersInstallPluginBtn">${esc(label)}</button>
            <span id="markersInstallResult" class="small ms-2"></span>`;
    }

    function renderJellyfinStatus(status) {
        const capability = status.capability || {};
        const details = capability.details || {};
        const rows = [kvRow('How markers get here', 'Media Preview Bridge plugin')];
        const pluginStates = ['ready', 'plugin_outdated', 'needs_plugin'];
        if (capability.state === 'ready') {
            rows.push(kvRow('Plugin', badge('ok', details.plugin_version ? `${details.plugin_version} ✓` : 'Installed ✓')));
        } else if (capability.state === 'plugin_outdated') {
            rows.push(kvRow('Plugin', badge('warn', 'Update needed') + installButton('Update')));
        } else if (capability.state === 'needs_plugin') {
            rows.push(kvRow('Plugin', badge('bad', 'Not installed') + installButton('Install')));
        }
        rows.push(kvRow('Can show', esc(canShowText(status.can_show))));
        return kvGrid(rows) + (pluginStates.includes(capability.state) ? '' : warningLine(capability));
    }

    function renderEmbyStatus(status) {
        const capability = status.capability || {};
        const rows = [kvRow('How markers get here', 'Media Preview Bridge for Emby plugin')];
        if (capability.state === 'needs_plugin') {
            rows.push(kvRow('Plugin', badge('off', 'Not available yet') + infoIcon(capability.message || '')));
        }
        rows.push(kvRow('Can show', 'Intro · credits start (no credits end)'));
        return kvGrid(rows) + (['ready', 'needs_plugin'].includes(capability.state) ? '' : warningLine(capability));
    }

    function renderStatus(server, status) {
        const block = $('#markersStatusBlock');
        if (!block) return;
        const vendor = vendorOf(server);
        if (vendor === 'plex') block.innerHTML = renderPlexStatus(status);
        else if (vendor === 'jellyfin') block.innerHTML = renderJellyfinStatus(status);
        else if (vendor === 'emby') block.innerHTML = renderEmbyStatus(status);
        else block.innerHTML = warningLine(status.capability || {});
        if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips($('#edit-tab-markers'));
    }

    function renderStatusError() {
        const block = $('#markersStatusBlock');
        if (!block) return;
        block.innerHTML = `<div class="small text-muted"><i class="bi bi-exclamation-circle me-1"></i>Couldn't check this server right now · <a href="#" class="markers-status-retry">Retry</a></div>`;
    }

    async function fetchStatus(server) {
        const seq = tab.loadSeq;
        const block = $('#markersStatusBlock');
        if (block) block.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Checking…</div>';
        let data = null;
        try {
            const r = await fetch(`/api/markers/servers/${encodeURIComponent(server.id)}/status`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            data = await r.json();
        } catch (_e) {
            if (seq === tab.loadSeq) renderStatusError();
            return;
        }
        // The user may have opened another server while this was loading.
        if (seq !== tab.loadSeq) return;
        tab.status = data;
        renderStatus(server, data);
        if (!tab.librariesTouched && Array.isArray(data.libraries)) renderLibraries(data.libraries, storedMarkers(server));
    }

    async function installPlugin(button) {
        const server = tab.server;
        if (!server) return;
        const seq = tab.loadSeq;
        const result = $('#markersInstallResult');
        const label = button.textContent;
        button.disabled = true;
        button.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${esc(label)}`;
        let data = null;
        let httpStatus = 0;
        try {
            const r = await fetch(`/api/servers/${encodeURIComponent(server.id)}/install-plugin`, {
                method: 'POST',
                headers: csrfHeaders(),
            });
            httpStatus = r.status;
            data = await r.json();
        } catch (_e) {
            data = null;
        }
        if (seq !== tab.loadSeq) return;
        if (!data || !data.ok) {
            button.disabled = false;
            button.textContent = label;
            if (result) {
                result.className = 'small ms-2 text-danger';
                result.textContent = (data && (data.error || data.message)) || `Install failed (HTTP ${httpStatus || '?'})`;
            }
            return;
        }
        if (result) {
            result.className = 'small ms-2 text-muted';
            result.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Jellyfin is restarting — checking again in 20 s…';
        }
        setTimeout(() => {
            if (seq === tab.loadSeq && tab.server) fetchStatus(tab.server);
        }, INSTALL_RECHECK_MS);
    }

    // ---------- libraries ------------------------------------------------------

    function libraryDomId(libraryId) {
        return `markersLib-${String(libraryId).replace(/[^A-Za-z0-9_-]/g, '_')}`;
    }

    function renderLibraries(libraries, markers) {
        const list = $('#markersLibraryList');
        if (!list) return;
        if (!libraries.length) {
            list.innerHTML = '<span class="small text-muted">No libraries yet — use Refresh libraries on the Libraries tab.</span>';
            return;
        }
        const chosen = Array.isArray(markers.library_ids) ? markers.library_ids.map(String) : null;
        list.innerHTML = libraries.map((lib) => {
            const id = String(lib.id);
            const byDefault = typeof lib.default_selected === 'boolean'
                ? lib.default_selected
                : !(SPORTS_NAME_RE.test(lib.name || '') || ['sport', 'sports'].includes(String(lib.kind || '').toLowerCase()));
            const checked = chosen === null ? byDefault : chosen.includes(id);
            const domId = libraryDomId(id);
            return `<input type="checkbox" class="btn-check markers-lib-toggle" id="${esc(domId)}" data-id="${esc(id)}" data-default="${byDefault ? '1' : '0'}" autocomplete="off"${checked ? ' checked' : ''}>`
                + `<label class="btn btn-sm markers-lib-pill" for="${esc(domId)}">${esc(lib.name || id)}</label>`;
        }).join('');
    }

    function readLibraryIds(server) {
        const stored = storedMarkers(server).library_ids;
        const storedIds = Array.isArray(stored) ? stored.map(String) : null;
        const toggles = $$('#markersLibraryList .markers-lib-toggle');
        // Untouched pills keep the stored choice exactly (an explicit list equal to the defaults stays a list).
        if (!tab.librariesTouched || !toggles.length) return storedIds;
        const ticked = toggles.filter((el) => el.checked).map((el) => el.dataset.id);
        const defaults = toggles.filter((el) => el.dataset.default === '1').map((el) => el.dataset.id);
        const sameAsDefault = ticked.length === defaults.length && ticked.every((id) => defaults.includes(id));
        return sameAsDefault ? null : ticked;
    }

    // ---------- public surface for servers.js ------------------------------------

    function loadMarkersTab(server) {
        tab.server = server;
        tab.status = null;
        tab.librariesTouched = false;
        tab.loadSeq += 1;
        const markers = storedMarkers(server);
        const toggle = $('#markersEnabled');
        if (toggle) toggle.checked = !!markers.enabled;

        const plex = isPlex(server);
        const redetectGroup = $('#markersPlexRedetectGroup');
        if (redetectGroup) redetectGroup.classList.toggle('d-none', !plex);
        const keepPlex = plex && storedPlex(server).on_plex_redetect === 'keep_plex';
        const restoreRadio = $('#markersRedetectRestore');
        const keepRadio = $('#markersRedetectKeep');
        if (restoreRadio) restoreRadio.checked = !keepPlex;
        if (keepRadio) keepRadio.checked = keepPlex;

        renderLibraries(server.libraries || [], markers);
        fetchStatus(server);
    }

    function readMarkersFromForm(server) {
        // The form only describes the server it was loaded for; anything else keeps its stored block untouched.
        if (!tab.server || !server || tab.server.id !== server.id) return { ...storedMarkers(server) };
        const out = {
            enabled: !!($('#markersEnabled') || {}).checked,
            library_ids: readLibraryIds(server),
        };
        if (isPlex(server)) {
            const picked = document.querySelector('input[name="markersPlexRedetect"]:checked');
            out.plex = {
                db_write_confirmed_at: storedPlex(server).db_write_confirmed_at || server._markersConfirmedAt || null,
                on_plex_redetect: picked ? picked.value : (storedPlex(server).on_plex_redetect || 'restore'),
            };
        }
        return out;
    }

    function markersNeedsPlexConfirmation(server) {
        return isPlex(server)
            && !!($('#markersEnabled') || {}).checked
            && !storedPlex(server).db_write_confirmed_at
            && !server._markersConfirmedAt;
    }

    function confirmLocalText() {
        const capability = (tab.status && tab.status.capability) || {};
        if (!(capability.details || {}).db_path) return '';
        return plexDbOnNetworkShare(capability)
            ? 'Checked: network share ✕ — writes will stay off'
            : 'Checked: local disk ✓';
    }

    function confirmPlexMarkers(server) {
        const target = server || tab.server;
        if (tab.pendingConfirmation) return tab.pendingConfirmation;
        const modalEl = $('#markersPlexConfirmModal');
        const toggle = $('#markersEnabled');
        if (!modalEl || !window.bootstrap || !window.bootstrap.Modal) {
            // No way to ask, so no database writes.
            if (toggle) toggle.checked = false;
            return Promise.resolve(false);
        }
        const local = $('#markersPlexConfirmLocal');
        if (local) local.textContent = confirmLocalText();

        tab.pendingConfirmation = new Promise((resolve) => {
            let accepted = false;
            const okBtn = $('#markersPlexConfirmOk');
            const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
            // Bootstrap has no public API for stacked modals: the Edit dialog's focus trap would pull focus (and
            // Escape) back behind this one, so it is paused while this modal is open. Private API (Bootstrap
            // 5.3.2, see base.html); test_escape_closes_only_the_confirmation catches a break on upgrade.
            const editDialog = window.bootstrap.Modal.getInstance($('#editServerModal'));
            const editFocusTrap = editDialog && editDialog._focustrap;
            if (editFocusTrap) editFocusTrap.deactivate();
            const onOk = () => {
                accepted = true;
                if (target) target._markersConfirmedAt = new Date().toISOString();
                modal.hide();
            };
            const onShown = () => {
                const backdrops = $$('.modal-backdrop');
                if (backdrops.length) backdrops[backdrops.length - 1].style.zIndex = '1060';
            };
            const onHidden = () => {
                okBtn.removeEventListener('click', onOk);
                modalEl.removeEventListener('shown.bs.modal', onShown);
                modalEl.removeEventListener('hidden.bs.modal', onHidden);
                modalEl.style.zIndex = '';
                if (toggle) toggle.checked = accepted;
                // Bootstrap drops body.modal-open when any modal closes; the Edit dialog is still open.
                if ($('#editServerModal.show')) {
                    document.body.classList.add('modal-open');
                    if (editFocusTrap) editFocusTrap.activate();
                }
                tab.pendingConfirmation = null;
                resolve(accepted);
            };
            okBtn.addEventListener('click', onOk);
            modalEl.addEventListener('shown.bs.modal', onShown);
            modalEl.addEventListener('hidden.bs.modal', onHidden);
            // Stacked over the Edit dialog: this modal and (once shown) its backdrop sit above it.
            modalEl.style.zIndex = '1065';
            modal.show();
        });
        return tab.pendingConfirmation;
    }

    function wire() {
        const toggle = $('#markersEnabled');
        if (toggle) {
            // Ask as soon as the switch is flipped on (Save asks again if that was bypassed).
            toggle.addEventListener('change', () => {
                if (tab.server && markersNeedsPlexConfirmation(tab.server)) confirmPlexMarkers(tab.server);
            });
        }
        const list = $('#markersLibraryList');
        if (list) {
            list.addEventListener('change', (event) => {
                if (event.target.classList.contains('markers-lib-toggle')) tab.librariesTouched = true;
            });
        }
        const block = $('#markersStatusBlock');
        if (block) {
            block.addEventListener('click', (event) => {
                const retry = event.target.closest('.markers-status-retry');
                if (retry) {
                    event.preventDefault();
                    if (tab.server) fetchStatus(tab.server);
                    return;
                }
                const install = event.target.closest('#markersInstallPluginBtn');
                if (install) installPlugin(install);
            });
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();

    window.loadMarkersTab = loadMarkersTab;
    window.readMarkersFromForm = readMarkersFromForm;
    window.markersNeedsPlexConfirmation = markersNeedsPlexConfirmation;
    window.confirmPlexMarkers = confirmPlexMarkers;
})();
