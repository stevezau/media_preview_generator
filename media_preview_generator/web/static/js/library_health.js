/* Library health: what's done and what's left in each library, per server.
 * Draws GET /api/library-health, polls it (2 s while a check runs, 60 s otherwise, paused while the tab is hidden)
 * and starts the existing job endpoints from the to-do file list. Depends on app.js: escapeHtml, showToast,
 * _initBootstrapTooltips, _disposeBootstrapTooltips. */
(function () {
    'use strict';

    const POLL_RUNNING_MS = 2000;
    const POLL_IDLE_MS = 60000;
    const PAGE_SIZE = 100;
    const PATH_PAGE_SIZE = 500;
    const FILTER_DEBOUNCE_MS = 300;

    const FEATURES = ['previews', 'loudness', 'intro', 'credits'];
    const FEATURE_LABEL = { previews: 'Previews', loudness: 'Loudness', intro: 'Intro', credits: 'Credits' };
    const JOB_LABEL = { previews: 'preview', loudness: 'loudness', intro: 'intro & credits', credits: 'intro & credits' };
    const HEADER_TIP = {
        previews: 'A preview counts as made when its file is in the server\'s preview folder for that video.',
        loudness: 'Counts loudness measured by this app or by Plex itself. Files where Plex\'s own loudness data is incomplete need Plex\'s own analysis; a loudness job here leaves them alone, so they stay on the to-do list.',
        intro: 'Counts intros from the server\'s own detection and from this app. Some shows have no intro, so "to do" means nobody has looked yet, not that it is missing.',
        credits: 'Counts credits from the server\'s own detection and from this app. "To do" means nobody has looked yet.',
    };
    const DONE_TEXT = { previews: 'All made', loudness: 'All measured', intro: 'All checked', credits: 'All checked' };
    const HAVE_TEXT = { intro: 'have an intro', credits: 'have credits' };

    const state = {
        data: null,
        timer: null,
        inFlight: false,
        again: false,
        files: null,
        filesSeq: 0,
        filterTimer: null,
        // The dashboard's "Review & fix" links here with ?fix=<server_id>; open that dialog once the data arrives.
        pendingFix: new URLSearchParams(window.location.search).get('fix'),
    };
    const rendered = {};

    function $(id) { return document.getElementById(id); }
    function n(value) { return Number(value || 0).toLocaleString(); }

    function tip(text) {
        return `<button type="button" class="info-icon" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" aria-label="More info" title="${escapeHtml(text)}"><i class="bi bi-info-circle"></i></button>`;
    }

    function toast(title, message, kind) {
        if (typeof showToast === 'function') showToast(title, message, kind || 'info');
    }

    async function request(method, url, body) {
        const options = { method: method, headers: {} };
        if (body !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        const resp = await fetch(url, options);
        if (resp.status === 401) {
            window.location.href = '/login';
            throw new Error('Authentication required');
        }
        const data = await resp.json().catch(function () { return {}; });
        if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
        return data;
    }

    // Replace an element's HTML only when it changed, so a 2 s poll doesn't close open tooltips or drop focus.
    function setHtml(id, html) {
        if (rendered[id] === html) return;
        rendered[id] = html;
        const el = $(id);
        if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(el);
        el.innerHTML = html;
        if (typeof _initBootstrapTooltips === 'function') _initBootstrapTooltips(el);
    }

    // ---------------------------------------------------------------- time

    function clock(date) {
        return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    function whenText(epochSeconds) {
        const date = new Date(epochSeconds * 1000);
        const now = new Date();
        const ageSeconds = (now - date) / 1000;
        if (ageSeconds < 60) return 'just now';
        const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        if (date >= midnight) return `today ${clock(date)}`;
        const yesterday = new Date(midnight.getTime() - 86400000);
        if (date >= yesterday) return `yesterday ${clock(date)}`;
        return `${date.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${clock(date)}`;
    }

    // ---------------------------------------------------------------- render

    function servers() { return (state.data && state.data.servers) || []; }

    function renderSide() {
        const data = state.data;
        const running = !!(data && data.running);
        let html = `<button type="button" class="btn btn-primary" data-action="check"${running ? ' disabled' : ''}>`
            + (running
                ? '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Checking…'
                : '<i class="bi bi-arrow-repeat me-1" aria-hidden="true"></i>Check now')
            + '</button>';
        const checked = servers().filter(function (s) { return s.checked_at; });
        if (checked.length) {
            const newest = checked.reduce(function (a, b) { return b.checked_at > a.checked_at ? b : a; });
            html += `<span class="lh-when">Last checked <b>${escapeHtml(whenText(newest.checked_at))}</b> · took ${newest.duration_s < 1 ? 'under 1' : n(Math.round(newest.duration_s))} s</span>`;
        }
        if (data && data.next_nightly_at) {
            const next = new Date(data.next_nightly_at);
            if (!isNaN(next)) {
                html += `<span class="lh-when">Checks every night at ${escapeHtml(clock(next))}`
                    + tip('Runs automatically every night.') + '</span>';
            }
        }
        setHtml('lhSide', html);
    }

    function renderProgress() {
        const running = state.data && state.data.running;
        if (!running) { setHtml('lhProgress', ''); return; }
        const reread = running.kind === 'reread';
        const name = running.server_name ? `${running.server_name}: ` : '';
        const step = running.step || (reread ? 'Asking Plex to re-read' : 'Checking');
        const count = running.total ? ` · ${n(running.done)} of ${n(running.total)}` : '';
        const text = running.cancelled ? 'Cancelling…' : `${name}${step}${count}`;
        const pct = running.total ? Math.min(100, Math.round((running.done / running.total) * 100)) : 0;
        setHtml('lhProgress', `<div class="lh-progress">
            <div class="lh-progress-text"><span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>${escapeHtml(text)}</span>
                <button type="button" class="btn btn-link btn-sm p-0 ms-auto" data-action="cancel"${running.cancelled ? ' disabled' : ''}>Cancel</button></div>
            <div class="lh-bar lh-bar-run" role="progressbar" aria-label="Check progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i class="lh-fill" style="width:${pct}%"></i></div>
        </div>`);
    }

    function renderError() {
        const message = state.data && state.data.last_error;
        setHtml('lhError', message
            ? `<div class="alert alert-danger" role="alert"><i class="bi bi-exclamation-triangle me-2" aria-hidden="true"></i>${escapeHtml(message)}</div>`
            : '');
    }

    function notShowingByLibrary(server) {
        const rows = [];
        server.libraries.forEach(function (lib) {
            const cell = lib.cells && lib.cells.previews;
            if (cell && cell.state === 'counted' && cell.not_showing > 0) rows.push({ name: lib.name, count: cell.not_showing });
        });
        return rows;
    }

    function plexServersWithProblems() {
        return servers()
            .filter(function (s) { return s.type === 'plex'; })
            .map(function (s) { return { server: s, rows: notShowingByLibrary(s) }; })
            .filter(function (entry) { return entry.rows.length > 0; });
    }

    function renderAttention() {
        const entries = plexServersWithProblems();
        const busy = !!(state.data && state.data.running);
        const several = servers().filter(function (s) { return s.type === 'plex'; }).length > 1;
        const html = entries.map(function (entry) {
            const total = entry.rows.reduce(function (sum, row) { return sum + row.count; }, 0);
            const owner = several ? ` <span class="lh-attn-server">(${escapeHtml(entry.server.name)})</span>` : '';
            const split = entry.rows.map(function (row) { return `${escapeHtml(row.name)} ${n(row.count)}`; }).join(' · ');
            return `<div class="lh-attn" role="status">
                <div><b>${n(total)} previews are made but Plex isn't showing them</b>${owner}
                <span class="lh-attn-detail">The preview files are on disk, but Plex hasn't re-read those videos since, so it doesn't know about them. ${split}.</span></div>
                <button type="button" class="btn btn-sm btn-outline-warning" data-action="fix" data-server="${escapeHtml(entry.server.server_id)}"${busy ? ' disabled' : ''}>Review &amp; fix</button>
            </div>`;
        }).join('');
        setHtml('lhAttention', html);
    }

    function renderLegend() {
        setHtml('lhLegend', servers().length
            ? '<div class="lh-legend"><span>Numbers in bold are files still to do; click one to list them.</span><span><i class="lh-sw lh-sw-none" aria-hidden="true"></i>Striped: checked, nothing found</span></div>'
            : '');
    }

    function cellButton(server, lib, feature, label, notShowing, extraClass) {
        return `<button type="button" class="${extraClass}" data-action="files" data-server="${escapeHtml(server.server_id)}" data-library="${escapeHtml(lib.library_id)}" data-feature="${feature}"${notShowing ? ' data-not-showing="1"' : ''}>${label}</button>`;
    }

    function renderCell(server, lib, feature) {
        const cell = lib.cells && lib.cells[feature];
        if (!cell) return '<span class="lh-na">–</span>';
        const reason = escapeHtml(cell.reason || '');
        if (cell.state === 'off') {
            return `<span class="lh-chip lh-chip-muted">${reason || 'Off for this library'}</span> <a class="lh-link" href="/servers">Change in Servers</a>`;
        }
        if (cell.state === 'not_applicable') return `<span class="lh-na">${reason || 'Not used here'}</span>`;
        if (cell.state === 'unavailable') return `<span class="lh-na">${reason || 'Not available'}</span>`;
        if (cell.state === 'error') {
            const failure = `<span class="lh-err"><i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>${reason || "Couldn't check"}</span>`;
            if (!cell.total) return failure;
            // The runner keeps the last good numbers on a failed cell, so the to-do count stays usable.
            const lastKnown = cell.todo > 0
                ? `<span class="lh-big">${cellButton(server, lib, feature, `<span class="lh-num">${n(cell.todo)}</span> <small>to do</small>`, false, 'lh-todo')}</span>`
                : `<span class="lh-na">${n(cell.done)} done</span>`;
            return `${lastKnown}${failure}<span class="lh-note">Numbers from the last check that worked</span>`;
        }

        const total = cell.total || 0;
        if (!total) return '<span class="lh-na">–</span>';
        const donePct = total ? (cell.done / total) * 100 : 0;
        const nonePct = total ? (cell.nothing_found / total) * 100 : 0;
        const head = cell.todo > 0
            ? `<span class="lh-big">${cellButton(server, lib, feature, `<span class="lh-num">${n(cell.todo)}</span> <small>to do</small>`, false, 'lh-todo')}</span>`
            : `<span class="lh-done"><i class="bi bi-check2" aria-hidden="true"></i> ${DONE_TEXT[feature]}</span>`;
        const bar = `<div class="lh-bar lh-bar-${feature}" aria-hidden="true"><i class="lh-fill" style="width:${donePct.toFixed(2)}%"></i><i class="lh-hatch" style="width:${nonePct.toFixed(2)}%"></i></div>`;
        let note = '';
        if (HAVE_TEXT[feature]) {
            note = `${n(cell.done)} ${HAVE_TEXT[feature]}` + (cell.nothing_found ? ` · ${n(cell.nothing_found)} nothing found` : '');
        } else if (cell.todo > 0) {
            const pct = Math.min(99, Math.round(donePct));
            note = `${n(cell.done)} of ${n(total)} done · ${pct}%`;
        } else {
            // Keeps a done cell as tall as a to-do one, so "not showing in Plex" sits on the same line in every row.
            note = `${n(cell.done)} done`;
        }
        const noteHtml = note ? `<span class="lh-note">${note}</span>` : '';
        const warn = feature === 'previews' && server.type === 'plex' && cell.not_showing > 0
            ? cellButton(server, lib, feature, `${n(cell.not_showing)} not showing in Plex`, true, 'lh-warn-note')
            : '';
        return `${head}${bar}${noteHtml}${warn}`;
    }

    function bothMarkersOff(lib) {
        const intro = lib.cells && lib.cells.intro;
        const credits = lib.cells && lib.cells.credits;
        return !!(intro && credits && intro.state === 'off' && credits.state === 'off' && intro.reason === credits.reason);
    }

    // Intro and credits are switched off together, so one notice spans both columns instead of repeating.
    function rowCells(server, lib) {
        const merge = bothMarkersOff(lib);
        return FEATURES.map(function (f) {
            if (merge && f === 'credits') return '';
            const span = merge && f === 'intro' ? ' colspan="2"' : '';
            const label = merge && f === 'intro' ? 'Intro &amp; credits' : FEATURE_LABEL[f];
            return `<td data-label="${label}"${span}><div class="lh-cell">${renderCell(server, lib, f)}</div></td>`;
        }).join('');
    }

    function renderServer(server) {
        const libs = server.libraries || [];
        const files = libs.reduce(function (sum, lib) { return sum + (lib.total || 0); }, 0);
        const letter = escapeHtml((server.type || '?').charAt(0).toUpperCase());
        const chips = (server.access ? `<span class="lh-chip lh-chip-ok">${escapeHtml(server.access)}</span>` : '')
            + (server.error ? `<span class="lh-chip lh-chip-bad" role="status"><i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>${escapeHtml(server.error)}</span>` : '');
        const head = FEATURES.map(function (f) {
            return `<th scope="col"><span class="lh-sw lh-sw-${f}"></span>${FEATURE_LABEL[f]}${tip(HEADER_TIP[f])}</th>`;
        }).join('');
        const rows = libs.map(function (lib) {
            const counted = FEATURES.some(function (f) { return lib.cells && lib.cells[f] && lib.cells[f].state === 'counted'; });
            const fileLine = lib.total > 0
                ? `<div class="lh-sub">${n(lib.total)} files</div>`
                : (counted ? '<div class="lh-sub">No files</div>' : '');
            const cells = rowCells(server, lib);
            return `<tr><th scope="row" class="lh-lib"><b>${escapeHtml(lib.name)}</b>${fileLine}</th>${cells}</tr>`;
        }).join('');
        return `<section class="lh-server" aria-label="${escapeHtml(server.name)}">
            <div class="lh-server-h">
                <span class="lh-logo lh-logo-${escapeHtml(server.type)}" aria-hidden="true">${letter}</span>
                <h2>${escapeHtml(server.name)}</h2>
                ${libs.length ? `<span class="lh-meta">${n(files)} files · ${n(libs.length)} ${libs.length === 1 ? 'library' : 'libraries'}</span>` : ''}
                <div class="lh-server-chips">${chips}</div>
            </div>
            ${libs.length
                ? `<div class="table-responsive"><table class="lh-table"><thead><tr><th scope="col">Library</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>`
                : '<p class="lh-sub p-3 mb-0">Nothing counted for this server yet.</p>'}
        </section>`;
    }

    function renderServers() {
        const list = servers();
        if (!state.data) { setHtml('lhServers', ''); return; }
        if (!list.length && state.data.servers_configured === 0) {
            setHtml('lhServers', `<div class="lh-empty"><h2>No media servers set up yet</h2>
                <p>Add a Plex, Emby or Jellyfin server to see what's done and what's left in each library.</p>
                <a class="btn btn-primary" href="/servers">Set up a server</a></div>`);
        } else if (!list.length) {
            const running = !!state.data.running;
            setHtml('lhServers', `<div class="lh-empty"><h2>Not checked yet</h2>
                <p>Run a check to count what's done and what's left in each library.</p>
                <button type="button" class="btn btn-primary" data-action="check"${running ? ' disabled' : ''}>Check now</button></div>`);
        } else {
            setHtml('lhServers', list.map(renderServer).join(''));
        }
        $('lhFoot').hidden = !list.length;
    }

    function render() {
        renderSide();
        renderProgress();
        renderError();
        renderAttention();
        renderLegend();
        renderServers();
    }

    // ---------------------------------------------------------------- polling

    async function refresh() {
        if (state.inFlight) { state.again = true; return; }
        state.inFlight = true;
        clearTimeout(state.timer);
        try {
            state.data = await request('GET', '/api/library-health');
            render();
            openPendingFix();
            setHtml('lhPollNote', '');
        } catch (err) {
            console.error('Library health refresh failed', err);
            setHtml('lhPollNote', '<p class="lh-note mb-0" role="status">Couldn\'t refresh — retrying.</p>');
        } finally {
            state.inFlight = false;
        }
        if (state.again) { state.again = false; refresh(); return; }
        schedule();
    }

    function schedule() {
        clearTimeout(state.timer);
        if (document.hidden) return;
        const running = !!(state.data && state.data.running);
        state.timer = setTimeout(refresh, running ? POLL_RUNNING_MS : POLL_IDLE_MS);
    }

    // ---------------------------------------------------------------- actions

    async function startCheck() {
        try {
            await request('POST', '/api/library-health/check');
        } catch (err) {
            toast('Error', err.message, 'danger');
        }
        refresh();
    }

    async function cancelCheck() {
        try {
            await request('POST', '/api/library-health/cancel');
        } catch (err) {
            toast('Error', err.message, 'danger');
        }
        refresh();
    }

    function findServer(id) { return servers().find(function (s) { return s.server_id === id; }); }
    function findLibrary(server, id) { return (server.libraries || []).find(function (l) { return l.library_id === id; }); }

    function modalFor(id) { return bootstrap.Modal.getOrCreateInstance($(id)); }

    function openFix(serverId) {
        const server = findServer(serverId);
        if (!server) return;
        const rows = notShowingByLibrary(server);
        const total = rows.reduce(function (sum, row) { return sum + row.count; }, 0);
        $('lhFixTitle').textContent = `Ask Plex to show ${n(total)} previews`;
        $('lhFixBody').innerHTML = `<p>These previews are already made. Plex only starts showing a preview after it re-reads the video, and it hasn't re-read these. This asks Plex to re-read each one. Nothing new is made.</p>
            <dl class="lh-kv">${rows.map(function (row) { return `<dt>${escapeHtml(row.name)}</dt><dd>${n(row.count)}</dd>`; }).join('')}</dl>
            <p class="mb-0">Plex re-reads one video at a time and can take a few seconds each, so thousands take hours. It runs in the background; you can cancel it on this page.</p>`;
        const start = $('lhFixStart');
        start.dataset.server = serverId;
        start.disabled = false;
        modalFor('lhFixModal').show();
    }

    function openPendingFix() {
        const serverId = state.pendingFix;
        if (!serverId) return;
        state.pendingFix = null;
        const url = new URL(window.location.href);
        url.searchParams.delete('fix');
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
        const server = findServer(serverId);
        if (server && !state.data.running && notShowingByLibrary(server).length) openFix(serverId);
    }

    async function startReread() {
        const start = $('lhFixStart');
        start.disabled = true;
        try {
            await request('POST', '/api/library-health/plex-reread', { server_id: start.dataset.server });
            modalFor('lhFixModal').hide();
            toast('Started', 'Asking Plex to re-read those videos. Progress shows on this page.', 'success');
            refresh();
        } catch (err) {
            start.disabled = false;
            toast('Error', err.message, 'danger');
        }
    }

    // ---------------------------------------------------------------- file list

    function splitPath(path) {
        const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
        return cut < 0 ? { name: path, folder: '' } : { name: path.slice(cut + 1), folder: path.slice(0, cut) };
    }

    function fileRow(file) {
        const parts = splitPath(file.path || '');
        return `<div class="lh-file"><div class="lh-file-text"><span class="lh-file-name" title="${escapeHtml(file.title || parts.name)}">${escapeHtml(parts.name)}</span>
            <span class="lh-file-folder">${escapeHtml(parts.folder)}</span></div>
            <a class="lh-link" href="/inspector?path=${encodeURIComponent(file.path)}">Open in Inspector →</a></div>`;
    }

    function filesQuery(f, offset, limit, q) {
        const params = new URLSearchParams({
            server_id: f.serverId, library_id: f.libraryId, feature: f.feature, q: q,
            offset: String(offset), limit: String(limit), not_showing: f.notShowing ? '1' : '0',
        });
        return `/api/library-health/files?${params.toString()}`;
    }

    async function loadFiles(append) {
        const f = state.files;
        const seq = ++state.filesSeq;
        const offset = append ? f.shown : 0;
        try {
            const res = await request('GET', filesQuery(f, offset, PAGE_SIZE, f.q));
            if (seq !== state.filesSeq) return;
            f.total = res.total;
            f.shown = offset + res.files.length;
            const html = res.files.map(fileRow).join('');
            if (append) $('lhFilesList').insertAdjacentHTML('beforeend', html);
            else $('lhFilesList').innerHTML = html || '<p class="lh-note mb-0">No files match.</p>';
            $('lhFilesCount').textContent = `${n(res.total)} ${res.total === 1 ? 'file' : 'files'}`;
            $('lhFilesMore').hidden = f.shown >= res.total;
        } catch (err) {
            if (seq === state.filesSeq) toast('Error', err.message, 'danger');
        }
    }

    function jobTarget(f) {
        return f.todo > state.data.path_job_limit ? 'library' : 'paths';
    }

    function resetFilesFooter() {
        const f = state.files;
        $('lhFilesConfirmText').hidden = true;
        $('lhFilesBack').hidden = true;
        const start = $('lhFilesStart');
        start.disabled = false;
        start.hidden = f.notShowing;
        const target = f.todo > state.data.path_job_limit ? f.libraryName : `these ${n(f.todo)} files`;
        start.textContent = `Start ${JOB_LABEL[f.feature]} job for ${target}`;
        f.confirming = false;
    }

    function openFiles(button) {
        const server = findServer(button.dataset.server);
        const lib = server && findLibrary(server, button.dataset.library);
        if (!lib) return;
        const feature = button.dataset.feature;
        const notShowing = button.dataset.notShowing === '1';
        clearTimeout(state.filterTimer);
        const cell = lib.cells[feature];
        state.files = {
            serverId: server.server_id, libraryId: lib.library_id, libraryName: lib.name, feature: feature,
            notShowing: notShowing, todo: cell.todo, q: '', shown: 0, total: 0, confirming: false,
        };
        $('lhFilesTitle').textContent = `${lib.name} · ${FEATURE_LABEL[feature]}`;
        $('lhFilesCount').textContent = `${n(notShowing ? cell.not_showing : cell.todo)} files`;
        $('lhFilesQuery').value = '';
        $('lhFilesList').innerHTML = '';
        $('lhFilesMore').hidden = true;
        $('lhFilesNote').textContent = notShowing
            ? 'These previews are already made, but Plex is not showing them yet. Use "Review & fix" on the page to ask Plex to re-read them.'
            : 'The list comes from the last check. The job skips files that are already done.';
        resetFilesFooter();
        modalFor('lhFilesModal').show();
        loadFiles(false);
    }

    function askConfirm() {
        const f = state.files;
        const what = jobTarget(f) === 'library' ? f.libraryName : `${n(f.todo)} files in ${f.libraryName}`;
        const markers = f.feature === 'intro' || f.feature === 'credits';
        $('lhFilesConfirmText').textContent = markers
            ? `Find intros & credits for ${what}?`
            : `Start a ${JOB_LABEL[f.feature]} job for ${what}?`;
        $('lhFilesConfirmText').hidden = false;
        $('lhFilesBack').hidden = false;
        $('lhFilesStart').textContent = 'Start';
        f.confirming = true;
    }

    async function allPaths(f) {
        const paths = [];
        let total = Infinity;
        while (paths.length < total) {
            const res = await request('GET', filesQuery(f, paths.length, PATH_PAGE_SIZE, ''));
            total = res.total;
            if (!res.files.length) break;
            res.files.forEach(function (file) { paths.push(file.path); });
        }
        return paths;
    }

    async function startJob() {
        const f = state.files;
        const start = $('lhFilesStart');
        start.disabled = true;
        try {
            const libraries = [{ server_id: f.serverId, library_id: f.libraryId }];
            const byLibrary = jobTarget(f) === 'library';
            const paths = byLibrary ? [] : await allPaths(f);
            // An empty file_paths list means "every enabled library" to the job endpoints, so never send one.
            if (!byLibrary && !paths.length) {
                start.disabled = false;
                resetFilesFooter();
                toast('Nothing to start', 'Nothing left to do here — run Check now.', 'warning');
                refresh();
                return;
            }
            let url;
            let body;
            if (f.feature === 'previews') {
                url = byLibrary ? '/api/jobs' : '/api/jobs/manual';
                body = byLibrary ? { libraries: libraries } : { file_paths: paths, server_id: f.serverId };
            } else {
                url = f.feature === 'loudness' ? '/api/loudness/jobs' : '/api/markers/jobs';
                body = byLibrary ? { libraries: libraries } : { file_paths: paths };
            }
            await request('POST', url, body);
            modalFor('lhFilesModal').hide();
            toast('Job started', 'See the Dashboard for progress.', 'success');
        } catch (err) {
            start.disabled = false;
            toast('Error', err.message, 'danger');
        }
    }

    // ---------------------------------------------------------------- wiring

    function onClick(event) {
        const button = event.target.closest('[data-action]');
        if (!button || button.disabled) return;
        const action = button.dataset.action;
        if (action === 'check') startCheck();
        else if (action === 'cancel') cancelCheck();
        else if (action === 'fix') openFix(button.dataset.server);
        else if (action === 'files') openFiles(button);
    }

    function init() {
        $('lhApp').addEventListener('click', onClick);
        $('lhFixStart').addEventListener('click', startReread);
        $('lhFilesMore').addEventListener('click', function () { loadFiles(true); });
        $('lhFilesBack').addEventListener('click', resetFilesFooter);
        $('lhFilesStart').addEventListener('click', function () {
            if (state.files.confirming) startJob();
            else askConfirm();
        });
        $('lhFilesModal').addEventListener('hidden.bs.modal', function () { clearTimeout(state.filterTimer); });
        $('lhFilesQuery').addEventListener('input', function (event) {
            clearTimeout(state.filterTimer);
            const value = event.target.value.trim();
            state.filterTimer = setTimeout(function () {
                if (!state.files) return;
                state.files.q = value;
                loadFiles(false);
            }, FILTER_DEBOUNCE_MS);
        });
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) clearTimeout(state.timer);
            else refresh();
        });
        refresh();
    }

    init();
})();
