// Servers → Edit → "Loudness" tab (Plex only): this server's switch, and the Libraries tab's Loudness column
// (loudness.library_ids), shown while the switch is on. Same library-choice rules as the Intro & Credits column
// (markers_server_tab.js). servers.js calls the window globals exported at the bottom.

(function () {
    'use strict';

    // Mirrors loudness.settings.DEFAULT_KINDS: movie and TV libraries unless the user chose.
    const DEFAULT_KINDS = ['movie', 'episode'];

    const $ = (sel, el) => (el || document).querySelector(sel);
    const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

    const tab = {
        server: null,
        // Library id → the Loudness switch as the user last left it; empty until they flip one.
        libraryChoices: new Map(),
    };

    function esc(value) {
        const shared = window.MPGShared && window.MPGShared.escapeHtml;
        const text = value == null ? '' : String(value);
        if (shared) return shared(text);
        return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function isPlex(server) {
        return String((server && server.type) || '').toLowerCase() === 'plex';
    }

    function stored(server) {
        const block = server && server.loudness;
        return block && typeof block === 'object' ? block : {};
    }

    // The Plex database write is confirmed once, on the Intro & Credits tab; loudness writes the same database.
    function writeConfirmed(server) {
        const plex = server && server.markers && server.markers.plex;
        return !!((plex && plex.db_write_confirmed_at) || (server && server._markersConfirmedAt));
    }

    function libraryDomId(libraryId) {
        return `loudnessLib-${String(libraryId).replace(/[^A-Za-z0-9_-]/g, '_')}`;
    }

    function selectedByDefault(kind) {
        return DEFAULT_KINDS.includes(String(kind || '').toLowerCase());
    }

    function syncLibraryColumn() {
        const on = !!tab.server && isPlex(tab.server) && !!($('#loudnessEnabled') || {}).checked;
        $$('#editLibraryTable .loudness-lib-col').forEach((el) => el.classList.toggle('d-none', !on));
    }

    function syncSwitch() {
        const toggle = $('#loudnessEnabled');
        const confirmed = writeConfirmed(tab.server);
        if (toggle) toggle.disabled = !confirmed && !toggle.checked;
        const hint = $('#loudnessConfirmHint');
        if (hint) hint.classList.toggle('d-none', confirmed);
    }

    // Fills the Loudness cell of every row servers.js rendered. A switch the user flipped keeps its state across a
    // re-render (Refresh libraries); every other one shows the stored choice, or the default when none.
    function renderLibraryColumn() {
        const ids = stored(tab.server).library_ids;
        const chosen = Array.isArray(ids) ? ids.map(String) : null;
        $$('#editLibraryList tr[data-lib-id]').forEach((row) => {
            const cell = row.querySelector('.loudness-lib-cell');
            if (!cell) return;
            const id = String(row.dataset.libId);
            const byDefault = selectedByDefault(row.dataset.libKind);
            const checked = tab.libraryChoices.has(id)
                ? tab.libraryChoices.get(id)
                : (chosen === null ? byDefault : chosen.includes(id));
            const label = `Loudness for ${row.dataset.libName || id}`;
            cell.innerHTML = '<div class="form-check form-switch edit-lib-switch">'
                + `<input type="checkbox" role="switch" class="form-check-input loudness-lib-toggle" id="${esc(libraryDomId(id))}" data-id="${esc(id)}" data-default="${byDefault ? '1' : '0'}" aria-label="${esc(label)}"${checked ? ' checked' : ''}>`
                + '</div>';
        });
        syncLibraryColumn();
    }

    function readLibraryIds(server) {
        const ids = stored(server).library_ids;
        const storedIds = Array.isArray(ids) ? ids.map(String) : null;
        const toggles = $$('#editLibraryList .loudness-lib-toggle');
        // Untouched switches keep the stored choice exactly (an explicit list equal to the defaults stays a list).
        if (tab.libraryChoices.size === 0 || !toggles.length) return storedIds;
        const ticked = toggles.filter((el) => el.checked).map((el) => el.dataset.id);
        const defaults = toggles.filter((el) => el.dataset.default === '1').map((el) => el.dataset.id);
        const sameAsDefault = ticked.length === defaults.length && ticked.every((id) => defaults.includes(id));
        return sameAsDefault ? null : ticked;
    }

    // ---------- public surface for servers.js ------------------------------------

    function loadLoudnessTab(server) {
        tab.server = server;
        tab.libraryChoices = new Map();
        const plex = isPlex(server);
        const li = $('#editTabLoudnessLi');
        if (li) li.classList.toggle('d-none', !plex);
        const toggle = $('#loudnessEnabled');
        if (toggle) toggle.checked = plex && !!stored(server).enabled;
        syncSwitch();
        renderLibraryColumn();
    }

    function readLoudnessFromForm(server) {
        // The form only describes the Plex server it was loaded for; anything else keeps its stored block untouched.
        if (!tab.server || !server || tab.server.id !== server.id || !isPlex(server)) return { ...stored(server) };
        return {
            enabled: !!($('#loudnessEnabled') || {}).checked,
            library_ids: readLibraryIds(server),
        };
    }

    function wire() {
        const toggle = $('#loudnessEnabled');
        if (toggle) toggle.addEventListener('change', syncLibraryColumn);
        // Confirming the database write on the Intro & Credits tab unlocks the switch here.
        const markersToggle = $('#markersEnabled');
        if (markersToggle) markersToggle.addEventListener('change', () => setTimeout(syncSwitch, 0));
        const list = $('#editLibraryList');
        if (list) {
            list.addEventListener('change', (event) => {
                const el = event.target;
                if (el.classList.contains('loudness-lib-toggle')) tab.libraryChoices.set(el.dataset.id, el.checked);
            });
        }
        const pointer = $('#loudnessLibrariesPointer');
        if (pointer) {
            pointer.addEventListener('click', (event) => {
                if (!event.target.closest('.loudness-libraries-link')) return;
                event.preventDefault();
                const trigger = $('#editServerModal [data-bs-target="#edit-tab-libraries"]');
                if (trigger && window.bootstrap && window.bootstrap.Tab) window.bootstrap.Tab.getOrCreateInstance(trigger).show();
            });
        }
        const tabButton = $('#editServerModal [data-bs-target="#edit-tab-loudness"]');
        if (tabButton) tabButton.addEventListener('shown.bs.tab', syncSwitch);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();

    window.loadLoudnessTab = loadLoudnessTab;
    window.readLoudnessFromForm = readLoudnessFromForm;
    window.renderLoudnessLibraryColumn = renderLibraryColumn;
})();
