// =========================================================================
// Inspector: one page for a file's preview frames and its intro & credits (the approved design, boards 1-4).
//
// Search: GET /api/media/search (every enabled server, merged), then POST /api/inspector/status for each row's
// preview and Intro & Credits state, and POST /api/inspector/show for a show's seasons and episodes. A path starting
// with "/" opens that file. Choosing a file folds the results away and sets ?path= so the page can be linked.
//
// A file: GET /api/inspector/file (where each server keeps its preview, what it holds, a job working on the file,
// other versions) and GET /api/markers/item (what was decided, the evidence, what each server shows). Frames come
// from the preview (GET /api/bif/frame, /api/bif/trickplay/frame); Adjust and "Needs your check" read exact frames one
// second apart from the video (GET /api/inspector/frames). Saving is POST /api/markers/item/markers (save = lock =
// publish to every owner); "Back to automatic" is DELETE on the same route. Regenerate preview is POST
// /api/jobs/manual, Re-detect POST /api/markers/item/redetect. A job on the /jobs socket that works on the open file
// shows as a live banner, and the file is read again when it ends.
//
// Every piece of text goes in through textContent. Depends on app.js globals: apiPost, showToast, getCsrfToken,
// _initBootstrapTooltips, _disposeBootstrapTooltips; window.bootstrap; window.io.
// =========================================================================
(function () {
    'use strict';

    const END_OF_FILE_MS = 2000;
    const TYPES = ['intro', 'credits', 'recap', 'preview'];
    const START_TYPES = ['intro', 'recap'];
    const TO_END_TYPES = ['credits', 'preview'];
    const TYPE_LABELS = { intro: 'Intro', credits: 'Credits', recap: 'Recap', preview: 'Preview' };
    const TYPE_WORDS = { intro: 'intro', credits: 'credits', recap: 'recap', preview: 'preview' };
    const TYPE_PLURALS = { intro: 'intros', credits: 'credits', recap: 'recaps', preview: 'previews' };
    const VENDOR_TILES = { plex: 'P', jellyfin: 'J', emby: 'E' };
    const VENDOR_NAMES = { plex: 'Plex', jellyfin: 'Jellyfin', emby: 'Emby' };
    const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨'];
    // Each source's name and what it is, in the words of the "How it was decided" card.
    const SOURCES = {
        season_audio: ['Season audio', 'The theme music heard across the season'],
        season_audio_previous: ['Previous season audio', 'The theme music of the season before'],
        chapters: ['Chapters', 'Chapter names inside the file'],
        credits_text: ['Credit text', 'On-screen credits read from the video'],
        theintrodb: ['TheIntroDB', 'Online database of intros and credits'],
        introdb: ['IntroDB', 'Online database of intros'],
        skipdb: ['SkipDB', 'Online database of intros and credits'],
        server_markers: ['Server markers', 'Markers a server made itself'],
        server_markers_imported: ['Imported markers', 'Markers a server plugin copied from an online database'],
        user: ['Your times', 'Times you set by hand'],
    };
    const SOURCE_ORDER = ['season_audio', 'season_audio_previous', 'chapters', 'credits_text', 'theintrodb', 'introdb',
        'skipdb', 'server_markers', 'server_markers_imported', 'user'];
    // Candidates within this of each other are one answer ("Two answers disagree by 28 seconds").
    const SAME_ANSWER_MS = 2000;
    const SEARCH_DEBOUNCE_MS = 300;
    const STATUS_BATCH = 5;
    const EXACT_COUNT = 7;
    const PICK_COUNT = 14;
    const PICK_STEP_MS = 10000;
    const CLOSEUP_SIDE = 3;
    const ENDING_SHARE = 0.12;
    const MARKERS_JOB = 'intro_credits';
    const ADD_HEAD_MS = 30000;
    const ADD_TAIL_MS = 60000;
    const TIPS = {
        regenerate: 'Makes this file\'s preview again for every server that has it, replacing the one there now. Runs as a job on the Dashboard.',
        redetect: 'Looks this file up again and asks every source afresh. Runs as a job on the Dashboard.',
        adjust: 'Move the intro and credits one second at a time, on frames read straight from the video. Saving sends your times to your servers and keeps them through later checks.',
        allFrames: 'Every frame of the preview, with intro frames edged blue and credits frames orange. Click one to see it larger.',
        unlock: 'Lets later checks decide these times again. What your servers show now stays until the next Intro & Credits job.',
        pick: 'Frames one second apart, read from the video. Step ten seconds either way to find the first frame.',
        lock: 'Keep these times exactly as they are. Later checks won\'t change them, and your servers get them now.',
        unlockHeader: 'Let later checks set these times again. What your servers show now stays until the next Intro & Credits job.',
        scope: 'Intros are found by comparing a season\'s episodes, so the whole season is the natural place to check and publish them.',
        publish: 'Runs Intro & Credits for this season as a job: decided episodes go to every server that doesn\'t show them yet, the rest are checked again.',
        versions: 'The server keeps these files under one item. Each has its own preview; Plex shows one set of markers for all of them.',
    };

    const state = {
        servers: [],
        query: '',
        searchSeq: 0,
        results: [],
        statuses: {},
        openShow: -1,
        showSeasons: {},
        showSeason: {},
        path: '',
        titleHint: '',
        bifOnly: '',
        file: null,
        item: null,
        itemError: '',
        loadSeq: 0,
        view: 'timeline',
        allStep: 1,
        allIndex: 0,
        adjust: null,
        review: {},
        confirmUnlock: false,
        confirmLock: false,
        locking: false,
        scope: 'episode',
        season: null,
        seasonError: '',
        seasonSeq: 0,
        publishing: false,
        busy: '',
        job: null,
    };
    const exactCache = new Map();
    let jobsSocket = null;
    let searchTimer = null;
    let reloadTimer = null;
    let resizeTimer = null;

    const $ = function (id) { return document.getElementById(id); };

    // ---------------------------------------------------------------- helpers

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(text, className, onClick, tip) {
        const b = el('button', className || 'btn btn-outline-secondary', text);
        b.type = 'button';
        if (onClick) b.addEventListener('click', onClick);
        if (tip) b.dataset.tip = tip;
        return b;
    }

    function infoIcon(title) {
        const b = el('button', 'info-icon ms-1');
        b.type = 'button';
        b.tabIndex = 0;
        b.setAttribute('data-bs-toggle', 'tooltip');
        b.setAttribute('data-bs-placement', 'top');
        b.title = title;
        b.setAttribute('aria-label', title);
        b.appendChild(el('i', 'bi bi-info-circle'));
        return b;
    }

    function withInfo(node, tip) {
        const wrap = el('span', 'd-inline-flex align-items-center');
        wrap.append(node, infoIcon(tip));
        return wrap;
    }

    function clock(ms) {
        const total = Math.max(0, Math.round(ms / 1000));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = String(total % 60).padStart(2, '0');
        return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    }

    function seconds(ms) {
        const s = ms / 1000;
        return Number.isInteger(s) ? `${s} s` : `${s.toFixed(1)} s`;
    }

    function parseClock(text) {
        const match = /^(\d{1,3})(?::(\d{1,2}))?(?::(\d{1,2}))?$/.exec(String(text || '').trim());
        if (!match) return null;
        const parts = [match[1], match[2], match[3]].filter(function (p) { return p !== undefined; }).map(Number);
        if (parts.slice(1).some(function (n) { return n > 59; })) return null;
        return parts.reduce(function (total, n) { return total * 60 + n; }, 0) * 1000;
    }

    function bytes(n) {
        if (!n && n !== 0) return '';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    }

    function when(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        const date = d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
        const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        return `${date}, ${time}`;
    }

    function joinWith(items, word) {
        if (items.length <= 1) return items.join('');
        return `${items.slice(0, -1).join(', ')} ${word} ${items[items.length - 1]}`;
    }

    function capitalise(text) {
        return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
    }

    function serverName(server) {
        return server.server_name || VENDOR_NAMES[String(server.server_type || '').toLowerCase()] || 'Server';
    }

    function segmentEnd(seg, duration) {
        return seg.end_ms === null || seg.end_ms === undefined ? duration : seg.end_ms;
    }

    function toEnd(seg, duration) {
        if (seg.end_ms === null || seg.end_ms === undefined) return true;
        return !!duration && seg.end_ms >= duration - END_OF_FILE_MS;
    }

    function rangeText(seg, duration) {
        return toEnd(seg, duration) ? `${clock(seg.start_ms)} → end` : `${clock(seg.start_ms)}–${clock(segmentEnd(seg, duration))}`;
    }

    function pct(ms, duration) {
        return Math.max(0, Math.min(100, (ms / duration) * 100));
    }

    async function getJson(url) {
        const resp = await fetch(url);
        if (resp.status === 401) {
            window.location.href = '/login';
            throw new Error('Authentication required');
        }
        const data = await resp.json().catch(function () { return {}; });
        return { ok: resp.ok, status: resp.status, data: data };
    }

    async function sendJson(method, url, body) {
        const resp = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '' },
            body: JSON.stringify(body || {}),
        });
        if (resp.status === 401) {
            window.location.href = '/login';
            throw new Error('Authentication required');
        }
        const data = await resp.json().catch(function () { return {}; });
        if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
        return data;
    }

    function toast(title, message, kind) {
        if (typeof showToast === 'function') showToast(title, message, kind || 'info');
    }

    function tooltips(root) {
        if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips(root);
    }

    function untooltip(root) {
        if (typeof window._disposeBootstrapTooltips === 'function') window._disposeBootstrapTooltips(root);
    }

    // ----------------------------------------------------------------- search

    async function loadServers() {
        try {
            const res = await getJson('/api/servers');
            state.servers = ((res.data && res.data.servers) || []).filter(function (s) { return s.enabled !== false; });
        } catch (e) {
            state.servers = [];
        }
        const scope = $('inspScope');
        state.servers.forEach(function (s) {
            const opt = el('option', '', s.name || VENDOR_NAMES[s.type] || s.id);
            opt.value = s.id;
            scope.appendChild(opt);
        });
    }

    function queryChanged() {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
    }

    function resultsBox() {
        return $('inspResults');
    }

    function showMessage(text, className) {
        const box = resultsBox();
        box.hidden = false;
        box.replaceChildren(el('div', 'insp-row ' + (className || 'insp-state-muted'), text));
    }

    async function runSearch() {
        const query = $('inspQuery').value.trim();
        state.query = query;
        const seq = ++state.searchSeq;
        if (query.startsWith('/')) {
            renderPathRow(query);
            return;
        }
        if (query.length < 2) {
            resultsBox().hidden = true;
            resultsBox().replaceChildren();
            return;
        }
        showMessage('Searching…');
        const params = new URLSearchParams({ q: query });
        if ($('inspScope').value) params.set('server_id', $('inspScope').value);
        let res;
        try {
            res = await getJson('/api/media/search?' + params.toString());
        } catch (e) {
            if (seq === state.searchSeq) showMessage(`Search failed: ${e.message}`, 'text-danger');
            return;
        }
        if (seq !== state.searchSeq) return;
        if (!res.ok) {
            showMessage((res.data && res.data.error) || `Search failed (HTTP ${res.status})`, 'text-danger');
            return;
        }
        state.results = (res.data.results || []).filter(function (r) { return (r.paths || []).length; });
        state.statuses = {};
        state.openShow = -1;
        renderResults();
        fetchStatuses(seq);
    }

    function renderPathRow(path) {
        const box = resultsBox();
        box.hidden = false;
        const row = el('button', 'insp-row');
        row.type = 'button';
        row.dataset.path = path;
        const cell = el('div');
        cell.append(el('div', 'insp-row-title', 'Open this file'), el('div', 'insp-row-meta insp-mono text-break', path));
        row.append(cell, el('div'), el('div'), el('i', 'bi bi-chevron-right'));
        row.addEventListener('click', function () { openFile(path, {}); });
        box.replaceChildren(row);
    }

    function titleWithYear(r) {
        const title = r.title || '';
        if (r.year && title.indexOf(`(${r.year})`) === -1 && r.kind !== 'episode') return `${title} (${r.year})`;
        return title;
    }

    function serverNames(r) {
        return (r.servers || []).map(function (s) { return s.name || VENDOR_NAMES[s.type] || s.id; });
    }

    function metaLine(r, status) {
        const parts = [];
        if (r.kind === 'show') {
            parts.push('TV show');
            if (r.child_count) parts.push(`${r.child_count.toLocaleString()} episodes`);
        } else {
            parts.push(r.kind === 'episode' ? 'Episode' : 'Film');
            if (status && status.quality) parts.push(status.quality);
        }
        const names = status && status.servers && status.servers.length ? status.servers : serverNames(r);
        if (names.length) parts.push(names.join(', '));
        return parts.join(' · ');
    }

    function previewText(status) {
        if (!status) return ['…', 'insp-state-muted'];
        if (status.error) return ['Couldn\'t check', 'insp-state-muted'];
        if (!status.in_library) return ['Not in a library', 'insp-state-muted'];
        const p = status.preview || {};
        if (p.state === 'ready') return [p.frames ? `Ready · ${Number(p.frames).toLocaleString()} frames` : 'Ready', ''];
        if (p.state === 'missing') return ['Missing', 'insp-state-muted'];
        return ['Couldn\'t check', 'insp-state-muted'];
    }

    function markersText(status) {
        if (!status) return ['…', 'insp-state-muted'];
        if (status.error || !status.in_library || !status.markers) return ['—', 'insp-state-muted'];
        const m = status.markers;
        if (m.state === 'needs_review') return [m.label, 'insp-state-review'];
        if (m.state === 'not_checked' || m.state === 'none') return [m.label, 'insp-state-muted'];
        return [m.label, ''];
    }

    function renderResults() {
        const box = resultsBox();
        untooltip(box);
        box.hidden = false;
        if (!state.results.length) {
            box.replaceChildren(el('div', 'insp-row insp-state-muted', `Nothing found for “${state.query}”.`));
            return;
        }
        const head = el('div', 'insp-row insp-row-head');
        head.append(el('div', '', 'TITLE'), el('div', 'insp-cell-preview', 'PREVIEW'), el('div', 'insp-cell-markers', 'INTRO & CREDITS'), el('div'));
        const nodes = [head];
        state.results.forEach(function (r, index) {
            const row = el('button', 'insp-row' + (state.openShow === index ? ' is-open' : ''));
            row.type = 'button';
            row.dataset.index = String(index);
            row.dataset.kind = r.kind;
            const status = r.kind === 'show' ? null : state.statuses[r.paths[0]];
            const titleCell = el('div');
            titleCell.style.minWidth = '0';
            titleCell.append(el('div', 'insp-row-title', titleWithYear(r)), el('div', 'insp-row-meta', metaLine(r, status)));
            let preview;
            let markers;
            if (r.kind === 'show') {
                preview = ['', ''];
                markers = [state.openShow === index ? 'Pick an episode below' : 'Pick an episode', 'insp-state-muted'];
            } else {
                preview = previewText(status);
                markers = markersText(status);
            }
            row.append(
                titleCell,
                el('div', 'insp-cell-preview ' + preview[1], preview[0]),
                el('div', 'insp-cell-markers ' + markers[1], markers[0]),
                el('i', 'bi ' + (r.kind === 'show' && state.openShow === index ? 'bi-chevron-down' : 'bi-chevron-right'))
            );
            row.addEventListener('click', function () { chooseResult(index); });
            nodes.push(row);
            if (r.kind === 'show' && state.openShow === index) nodes.push(renderShowPanel(r, index));
        });
        box.replaceChildren.apply(box, nodes);
        tooltips(box);
    }

    async function fetchStatuses(seq) {
        const paths = state.results.filter(function (r) { return r.kind !== 'show'; }).map(function (r) { return r.paths[0]; });
        const batches = [];
        for (let i = 0; i < paths.length; i += STATUS_BATCH) batches.push(paths.slice(i, i + STATUS_BATCH));
        await Promise.all(batches.map(async function (batch) {
            try {
                const data = await sendJson('POST', '/api/inspector/status', { paths: batch });
                if (seq !== state.searchSeq) return;
                Object.assign(state.statuses, data.items || {});
            } catch (e) {
                if (seq !== state.searchSeq) return;
                batch.forEach(function (p) { state.statuses[p] = { in_library: true, error: e.message }; });
            }
            renderResults();
        }));
    }

    function chooseResult(index) {
        const r = state.results[index];
        if (!r) return;
        if (r.kind === 'show') {
            state.openShow = state.openShow === index ? -1 : index;
            renderResults();
            if (state.openShow === index && !state.showSeasons[index]) loadShow(index);
            return;
        }
        openFile(r.paths[0], { title: titleWithYear(r) });
    }

    async function loadShow(index) {
        const r = state.results[index];
        state.showSeasons[index] = { loading: true };
        try {
            const data = await sendJson('POST', '/api/inspector/show', { paths: r.paths.slice(0, 10) });
            state.showSeasons[index] = { seasons: data.seasons || [] };
            const first = (data.seasons || [])[0];
            state.showSeason[index] = first ? first.season : null;
        } catch (e) {
            state.showSeasons[index] = { error: e.message };
        }
        if (state.openShow === index) renderResults();
    }

    function renderShowPanel(r, index) {
        const panel = el('div', 'insp-show');
        panel.dataset.show = String(index);
        const data = state.showSeasons[index];
        if (!data || data.loading) {
            panel.appendChild(el('div', 'insp-state-muted small', 'Reading the show\'s folders…'));
            return panel;
        }
        if (data.error) {
            panel.appendChild(el('div', 'text-danger small', `Couldn't read this show's episodes: ${data.error}`));
            return panel;
        }
        if (!data.seasons.length) {
            panel.appendChild(el('div', 'insp-state-muted small', 'No episodes found in this show\'s folders.'));
            return panel;
        }
        const seasons = el('div', 'insp-seasons');
        seasons.setAttribute('role', 'group');
        seasons.setAttribute('aria-label', 'Seasons');
        data.seasons.forEach(function (season) {
            const active = season.season === state.showSeason[index];
            const b = button(season.label, 'btn btn-sm btn-outline-secondary' + (active ? ' active' : ''), function () {
                state.showSeason[index] = season.season;
                renderResults();
            });
            b.setAttribute('aria-pressed', active ? 'true' : 'false');
            seasons.appendChild(b);
        });
        panel.appendChild(seasons);
        const season = data.seasons.find(function (s) { return s.season === state.showSeason[index]; }) || data.seasons[0];
        const grid = el('div', 'insp-episodes');
        season.episodes.forEach(function (ep) {
            const card = el('button', 'insp-episode');
            card.type = 'button';
            card.dataset.path = ep.path;
            const stateText = (ep.markers && ep.markers.label) || '';
            card.append(el('div', 'insp-episode-code', ep.code), el('div', 'insp-episode-state' + (ep.markers && ep.markers.state === 'needs_review' ? ' insp-state-review' : ''), stateText));
            card.addEventListener('click', function () {
                const code = ep.episode !== null && ep.episode !== undefined && season.season !== null
                    ? `S${String(season.season).padStart(2, '0')}E${String(ep.episode).padStart(2, '0')}` : ep.code;
                openFile(ep.path, { title: `${titleWithYear(r)} · ${code}` });
            });
            grid.appendChild(card);
        });
        panel.appendChild(grid);
        return panel;
    }

    // ------------------------------------------------------------- navigation

    function setUrl(params, replace) {
        const url = window.location.pathname + (params ? '?' + params.toString() : '');
        const data = { path: state.path, bif: state.bifOnly, title: state.titleHint };
        if (replace) window.history.replaceState(data, '', url);
        else window.history.pushState(data, '', url);
    }

    function fold(folded) {
        $('inspSearch').hidden = folded;
        $('inspFolded').hidden = !folded;
        $('inspFile').hidden = !folded;
        $('inspShowResultsText').textContent = state.query && !state.query.startsWith('/')
            ? `Back to the results for “${state.query}”` : 'New search';
    }

    function showSearch(push) {
        state.path = '';
        state.bifOnly = '';
        state.file = null;
        state.item = null;
        state.adjust = null;
        fold(false);
        if (push) setUrl(null, false);
        $('inspQuery').focus();
    }

    function resetFileState() {
        state.file = null;
        state.item = null;
        state.itemError = '';
        state.adjust = null;
        state.review = {};
        state.confirmUnlock = false;
        state.confirmLock = false;
        state.locking = false;
        state.season = null;
        state.seasonError = '';
        state.view = 'timeline';
        state.allIndex = 0;
        state.job = null;
        exactCache.clear();
    }

    function openFile(path, options) {
        const opts = options || {};
        resetFileState();
        state.path = path;
        state.bifOnly = '';
        state.titleHint = opts.title || '';
        state.scope = opts.scope === 'season' ? 'season' : 'episode';
        fold(true);
        if (!opts.fromHistory) setUrl(fileParams(), !!opts.replace);
        watchJobs();
        loadFile();
        if (state.scope === 'season') loadSeason();
    }

    function fileParams() {
        const params = new URLSearchParams({ path: state.path });
        if (state.scope === 'season') params.set('view', 'season');
        return params;
    }

    async function loadFile() {
        const seq = ++state.loadSeq;
        const path = state.path;
        render();
        const q = 'path=' + encodeURIComponent(path);
        const [fileRes, itemRes] = await Promise.all([
            getJson('/api/inspector/file?' + q).catch(function (e) { return { ok: false, status: 0, data: { error: e.message } }; }),
            getJson('/api/markers/item?' + q).catch(function (e) { return { ok: false, status: 0, data: { error: e.message } }; }),
        ]);
        if (seq !== state.loadSeq) return;
        if (fileRes.ok) {
            state.file = fileRes.data;
            state.job = fileRes.data.job || null;
        } else {
            state.file = { error: (fileRes.data && fileRes.data.error) || `HTTP ${fileRes.status}`, canonical_path: path };
        }
        if (itemRes.ok) {
            state.item = itemRes.data;
            state.itemError = '';
        } else {
            state.item = null;
            state.itemError = (itemRes.data && itemRes.data.error) || `HTTP ${itemRes.status}`;
        }
        render();
    }

    async function openBif(path, options) {
        resetFileState();
        state.path = '';
        state.bifOnly = path;
        fold(true);
        if (!(options && options.fromHistory)) setUrl(new URLSearchParams({ bif: path }), !!(options && options.replace));
        const seq = ++state.loadSeq;
        render();
        const res = await getJson('/api/bif/info?path=' + encodeURIComponent(path)).catch(function (e) {
            return { ok: false, data: { error: e.message } };
        });
        if (seq !== state.loadSeq) return;
        if (!res.ok) {
            state.file = { error: (res.data && res.data.error) || 'Couldn\'t read this preview file', canonical_path: path };
        } else {
            const d = res.data;
            state.file = {
                bif_only: true,
                canonical_path: path,
                exists: true,
                in_library: true,
                title: path.split('/').pop(),
                kind: 'movie',
                duration_ms: d.frame_count * d.frame_interval_ms || null,
                previews: [],
                versions: [],
                job: null,
                preview: {
                    kind: 'bif', path: d.path, exists: true, frame_count: d.frame_count,
                    interval_ms: d.frame_interval_ms, file_size: d.file_size, created_at: d.created_at,
                },
            };
            state.view = 'all';
        }
        render();
    }

    // ------------------------------------------------------------------ model

    function duration() {
        const f = state.file || {};
        if (f.duration_ms) return f.duration_ms;
        if (state.item && state.item.duration_ms) return state.item.duration_ms;
        const fromServer = ((state.item && state.item.servers) || []).map(function (s) { return s.duration_ms; }).filter(Boolean);
        return fromServer.length ? fromServer[0] : 0;
    }

    function preview() {
        return (state.file && state.file.preview) || null;
    }

    function interval() {
        const p = preview();
        return p ? (p.interval_ms || (duration() && p.frame_count ? Math.round(duration() / p.frame_count) : 0)) : 0;
    }

    function frameUrl(index) {
        const p = preview();
        if (!p) return '';
        const i = Math.max(0, Math.min(index, p.frame_count - 1));
        if (p.kind === 'trickplay') {
            const params = new URLSearchParams({
                server_id: p.server_id || '', sheets_dir: p.sheets_dir || p.path || '', index: String(i),
                tile_width: String(p.tile_width || 10), tile_height: String(p.tile_height || 10),
            });
            return '/api/bif/trickplay/frame?' + params.toString();
        }
        return `/api/bif/frame?path=${encodeURIComponent(p.path)}&index=${i}`;
    }

    function frameIndexAt(ms) {
        const p = preview();
        const step = interval();
        if (!p || !step) return 0;
        return Math.max(0, Math.min(p.frame_count - 1, Math.round(ms / step)));
    }

    function decision(type) {
        return ((state.item && state.item.decisions) || {})[type] || {};
    }

    function decided(type) {
        const d = decision(type);
        return d.status === 'decided' && d.marker ? d.marker : null;
    }

    function decidedTypes() {
        return TYPES.filter(function (t) { return decided(t); });
    }

    function reviewTypes() {
        return TYPES.filter(function (t) { return decision(t).status === 'needs_review'; });
    }

    function lockedTypes() {
        return TYPES.filter(function (t) { const m = decided(t); return m && m.locked; });
    }

    function servers() {
        return (state.item && state.item.servers) || [];
    }

    function enabledOwners(type) {
        return servers().filter(function (s) {
            return s.markers_enabled && (!type || (s.can_show || []).indexOf(type) !== -1);
        });
    }

    function isChecked() {
        return !!(state.item && state.item.known && Object.values(state.item.decisions || {}).some(function (d) { return d && d.status; }));
    }

    function serverMarkers() {
        const out = [];
        servers().forEach(function (s) {
            (Array.isArray(s.current) ? s.current : []).forEach(function (m) { out.push({ server: s, marker: m }); });
        });
        return out.sort(function (a, b) { return a.marker.start_ms - b.marker.start_ms; });
    }

    // ------------------------------------------------------------------ render

    function render() {
        const root = $('inspFile');
        untooltip(root);
        const parts = [];
        if (state.bifOnly) {
            parts.push(header(state.file ? state.file.title : state.bifOnly.split('/').pop(), state.bifOnly, []));
        } else {
            const f = state.file || {};
            const title = state.titleHint || f.title || state.path.split('/').pop();
            parts.push(header(title, state.path, state.file && !state.file.error && f.exists !== false && f.in_library ? actions() : []));
        }
        const body = el('div', 'd-flex flex-column gap-4');
        body.id = 'inspBody';
        if (!state.file) {
            body.appendChild(loadingCard());
        } else if (state.file.error) {
            body.appendChild(messageCard('Couldn\'t open this file', state.file.error, 'danger'));
        } else if (state.bifOnly) {
            body.appendChild(factsCard());
            body.appendChild(wholeFileCard());
        } else if (state.file.exists === false) {
            body.appendChild(goneCard());
        } else if (!state.file.in_library || state.file.exists === null) {
            body.appendChild(messageCard('Not in any library',
                'No server has a library that holds this path, so the Inspector has nothing to compare and nothing here '
                + 'is sent anywhere. Check the path, or the libraries on the Servers page.', 'secondary'));
        } else {
            if (state.file.versions && state.file.versions.length > 1) body.appendChild(versionsBar());
            if (state.file.kind === 'episode') body.appendChild(scopeToggle());
            const banner = el('div');
            banner.id = 'inspJobBanner';
            body.appendChild(banner);
            renderJobBanner(banner);
            if (state.scope === 'season') {
                body.appendChild(seasonCard());
                parts.push(body);
                root.replaceChildren.apply(root, parts);
                tooltips(root);
                return;
            }
            reviewTypes().forEach(function (type) { body.appendChild(reviewPanel(type)); });
            body.appendChild(summaryCard());
            body.appendChild(wholeFileCard());
            const ending = endingCard();
            if (ending) body.appendChild(ending);
            const closeups = closeupCards();
            if (closeups) body.appendChild(closeups);
            if (state.adjust) body.appendChild(adjustBar());
            const lower = el('div', 'insp-grid-2');
            lower.append(evidenceCard(), serversCard());
            body.appendChild(lower);
        }
        parts.push(body);
        root.replaceChildren.apply(root, parts);
        tooltips(root);
        requestAnimationFrame(layoutAllLabels);
    }

    function header(title, path, buttons) {
        const head = el('div', 'insp-head');
        const text = el('div', 'insp-head-text');
        text.append(el('div', 'insp-crumb', 'Tools › Inspector'));
        const h1 = el('h1', 'insp-title', title);
        h1.id = 'inspTitle';
        text.appendChild(h1);
        if (path) {
            const p = el('div', 'insp-path', path);
            p.id = 'inspPath';
            p.title = path;
            text.appendChild(p);
        }
        head.appendChild(text);
        if (buttons.length) {
            const bar = el('div', 'insp-actions');
            buttons.forEach(function (b) { bar.appendChild(b); });
            head.appendChild(bar);
        }
        return head;
    }

    function actionButton(text, className, handler, tip, id) {
        const wrap = el('span', 'd-inline-flex align-items-center');
        const b = button(text, className, handler);
        b.id = id;
        if (state.busy) b.disabled = true;
        wrap.append(b, infoIcon(tip));
        return wrap;
    }

    function actions() {
        const list = [];
        list.push(actionButton('Regenerate preview', 'btn btn-outline-secondary', regenerate, TIPS.regenerate, 'inspRegenerate'));
        const analysed = !!(state.item && state.item.known && state.item.duration_ms);
        if (!isChecked()) {
            list.push(actionButton('Check intro & credits now', 'btn btn-insp-primary', redetect, TIPS.redetect, 'inspRedetect'));
            return list;
        }
        list.push(actionButton('Re-detect intro & credits', 'btn btn-outline-secondary', redetect, TIPS.redetect, 'inspRedetect'));
        if (state.scope === 'season') return list;
        if (analysed && lockedTypes().length) {
            const b = actionButton('Back to automatic', 'btn btn-outline-secondary', askUnlock, TIPS.unlockHeader, 'inspUnlock');
            if (state.adjust || state.locking) b.querySelector('button').disabled = true;
            list.push(b);
        } else if (analysed && lockableTypes().length) {
            const b = actionButton(state.locking ? 'Locking…' : 'Lock', 'btn btn-outline-secondary', askLock, TIPS.lock, 'inspLock');
            if (state.adjust || state.locking) b.querySelector('button').disabled = true;
            list.push(b);
        }
        if (analysed && !reviewTypes().length) {
            const label = state.adjust ? 'Adjusting…' : 'Adjust';
            const b = actionButton(label, 'btn btn-insp-primary', startAdjust, TIPS.adjust, 'inspAdjust');
            if (state.adjust || state.locking) b.querySelector('button').disabled = true;
            list.push(b);
        }
        return list;
    }

    // What Lock can send: the decided types a server with Intro & Credits on can show (the save route refuses the whole
    // request over one no owner can show).
    function lockableTypes() {
        return decidedTypes().filter(function (t) { return enabledOwners(t).length > 0; });
    }

    function askLock() {
        state.confirmLock = true;
        state.confirmUnlock = false;
        render();
        const row = $('inspLockConfirmRow');
        if (row) row.scrollIntoView({ block: 'nearest' });
    }

    function askUnlock() {
        state.confirmUnlock = true;
        state.confirmLock = false;
        render();
        const row = $('inspLocked');
        if (row) row.scrollIntoView({ block: 'nearest' });
    }

    // Lock changes no time: the decided times go through the same save as Adjust (the same lock, the same publish to
    // every owner) exactly as they are.
    async function lockNow() {
        const dur = duration();
        const types = lockableTypes();
        const markers = types.map(function (t) {
            const m = decided(t);
            const runsToEnd = TO_END_TYPES.indexOf(t) !== -1 && toEnd(m, dur);
            return { type: t, start_ms: m.start_ms, end_ms: runsToEnd ? null : segmentEnd(m, dur) };
        });
        const path = state.path;
        state.locking = true;
        state.confirmLock = false;
        render();
        try {
            const answer = await saveMarkers(markers, path);
            toast('Locked', `These times stay until you go back to automatic. ${savedMessage(answer)}`, 'success');
            if (state.path !== path) return;
            state.locking = false;
            await loadFile();
        } catch (e) {
            toast('Lock', `Couldn't lock these times: ${e.message}`, 'danger');
            if (state.path !== path) return;
            state.locking = false;
            render();
        }
    }

    function lockConfirmRow() {
        const types = lockableTypes();
        const dur = duration();
        const row = el('div', 'insp-confirm-inline mt-3');
        row.id = 'inspLockConfirmRow';
        const names = receivers(types);
        const listed = types.map(function (t) { return `${TYPE_LABELS[t]} ${rangeText(decided(t), dur)}`; }).join(' · ');
        const text = el('div');
        text.append(el('div', 'fw-semibold', `Lock these times? ${listed}`),
            el('div', 'insp-small', `Later checks won't change them, and they go to ${names.length ? joinWith(names, 'and') : 'your servers'} now, the same way Save sends them.`));
        const buttons = el('div', 'd-flex gap-2 flex-wrap');
        const yes = button(`Lock and send to ${names.length ? joinWith(names, 'and') : 'your servers'}`, 'btn btn-sm btn-insp-primary', lockNow);
        yes.id = 'inspLockConfirm';
        const no = button('Leave them as they are', 'btn btn-sm btn-outline-secondary', function () { state.confirmLock = false; render(); });
        buttons.append(yes, no);
        row.append(text, buttons);
        return row;
    }

    function loadingCard() {
        const card = el('div', 'insp-card d-flex align-items-center gap-2');
        card.id = 'inspLoading';
        card.append(el('span', 'spinner-border spinner-border-sm'), el('span', '', 'Reading this file…'));
        return card;
    }

    function messageCard(title, text, tone) {
        const card = el('div', `insp-card border-${tone || 'secondary'}`);
        card.dataset.state = title;
        card.append(el('div', 'fw-semibold fs-5 mb-1', title), el('div', 'insp-small fs-6', text));
        return card;
    }

    function goneCard() {
        const known = state.file && state.file.known;
        const card = messageCard('Gone from disk',
            'This file isn\'t on disk any more: it was probably replaced or deleted. '
            + (known ? 'Intro & Credits still has what it found for it at this path. ' : '')
            + 'Search for the title to find the file it has now.', 'warning');
        card.id = 'inspGone';
        const back = button('Search for it', 'btn btn-outline-secondary mt-3', function () {
            const t = (state.file && state.file.title) || '';
            $('inspQuery').value = t.split(' · ')[0].replace(/\s*\(\d{4}\)\s*$/, '');
            showSearch(true);
            runSearch();
        });
        card.appendChild(back);
        return card;
    }

    function versionsBar() {
        const bar = el('div', 'insp-versions');
        bar.id = 'inspVersions';
        bar.appendChild(withInfo(el('span', 'fw-medium', `This item has ${state.file.versions.length} versions:`), TIPS.versions));
        state.file.versions.forEach(function (v) {
            const b = button(v.label, 'btn btn-sm btn-outline-secondary' + (v.current ? ' active' : ''), function () {
                if (!v.current) openFile(v.path, {});
            });
            b.title = v.path;
            b.setAttribute('aria-pressed', v.current ? 'true' : 'false');
            bar.appendChild(b);
        });
        return bar;
    }

    function renderJobBanner(node) {
        const target = node || $('inspJobBanner');
        if (!target) return;
        const job = state.job;
        if (!job) {
            target.replaceChildren();
            target.hidden = true;
            return;
        }
        target.hidden = false;
        const box = el('div', 'alert alert-info d-flex align-items-center gap-2 mb-0 py-2');
        box.setAttribute('role', 'status');
        const running = job.status === 'running';
        if (running) box.appendChild(el('span', 'spinner-border spinner-border-sm'));
        else box.appendChild(el('i', 'bi bi-hourglass-split'));
        const what = job.kind === MARKERS_JOB ? 'Intro & Credits' : 'Preview';
        const pctText = running && job.percent ? ` · ${Math.round(job.percent)}%` : '';
        box.appendChild(el('span', '', `${running ? 'Working on this file' : 'Queued for this file'}: ${what} job “${job.name || job.id}”${pctText}`));
        const link = el('a', 'ms-auto', 'Open on the Dashboard');
        link.href = '/?job=' + encodeURIComponent(job.id);
        box.appendChild(link);
        target.replaceChildren(box);
    }

    // ------------------------------------------------------------------ season

    const CHIP_NAMES = {
        chapters: 'Chapters', theintrodb: 'TheIntroDB', introdb: 'IntroDB', skipdb: 'SkipDB', season_audio: 'Audio',
        season_audio_previous: 'Previous season', credits_text: 'Credit text', user: 'Your marker',
    };
    const DOT_WORDS = {
        ok: 'shows our markers', waiting: 'waiting', failed: 'failed', skipped: 'skipped', none: 'nothing sent yet',
        off: 'Intro & Credits is off',
    };
    // Matches markers.audio.season.MAX_GROUP_EPISODES: a folder with more episodes is capped to the nearest this many.
    const MAX_GROUP_EPISODES = 40;

    function scopeToggle() {
        const row = el('div', 'd-flex align-items-center gap-2');
        const seg = el('div', 'insp-seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Show this episode or the whole season');
        [['episode', 'This episode', 'inspScopeEpisode'], ['season', 'Whole season', 'inspScopeSeason']].forEach(function (opt) {
            const on = state.scope === opt[0];
            const b = button(opt[1], 'btn' + (on ? ' active' : ''), function () { setScope(opt[0]); });
            b.id = opt[2];
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            seg.appendChild(b);
        });
        row.appendChild(withInfo(seg, TIPS.scope));
        return row;
    }

    function setScope(scope) {
        if (state.scope === scope) return;
        state.scope = scope;
        state.adjust = null;
        state.confirmLock = false;
        state.confirmUnlock = false;
        setUrl(fileParams(), false);
        if (scope === 'season') loadSeason();
        render();
    }

    async function loadSeason() {
        const seq = ++state.seasonSeq;
        const path = state.path;
        state.season = null;
        state.seasonError = '';
        render();
        const res = await getJson('/api/markers/season?path=' + encodeURIComponent(path)).catch(function (e) {
            return { ok: false, status: 0, data: { error: e.message } };
        });
        if (seq !== state.seasonSeq || state.path !== path) return;
        if (res.ok) state.season = res.data;
        else state.seasonError = (res.data && res.data.error) || `HTTP ${res.status}`;
        if (state.scope === 'season') render();
    }

    function seasonTimes(ep) {
        const dur = ep.duration_ms;
        const parts = [];
        ['intro', 'credits'].forEach(function (t) {
            const d = ep[t] || {};
            if (d.status === 'decided' && d.marker) parts.push(`${TYPE_LABELS[t]} ${rangeText(d.marker, dur)}`);
        });
        if (ep.needs_review) return ['Needs review', 'insp-state-review'];
        if (!ep.known) return ['Not checked yet', 'insp-state-muted'];
        if (!parts.length) return ['Nothing found', 'insp-state-muted'];
        return [parts.join(' · '), ''];
    }

    function seasonLane(ep, scale) {
        const lane = el('div', 'insp-season-lane');
        if (!ep.duration_ms || !scale) {
            lane.appendChild(el('div', 'insp-small ps-2', ep.known ? 'Length not known yet' : ''));
            return lane;
        }
        const track = el('div', 'insp-season-track');
        track.style.width = `${pct(ep.duration_ms, scale)}%`;
        lane.appendChild(track);
        ['intro', 'credits'].forEach(function (t) {
            const d = ep[t] || {};
            if (d.status !== 'decided' || !d.marker) return;
            const m = d.marker;
            const bar = el('div', 'insp-bar insp-bar-' + t);
            bar.style.left = `${pct(m.start_ms, scale)}%`;
            bar.style.width = `${Math.max(0.4, pct(segmentEnd(m, ep.duration_ms) - m.start_ms, scale))}%`;
            bar.title = `${TYPE_LABELS[t]} ${rangeText(m, ep.duration_ms)}`;
            lane.appendChild(bar);
        });
        return lane;
    }

    function seasonDots(ep, servers) {
        const dots = el('div', 'insp-dots');
        servers.forEach(function (server) {
            const st = (ep.servers || {})[server.server_id] || { state: 'none', message: '' };
            const dot = el('span', 'insp-dot insp-dot-' + st.state);
            dot.title = `${server.server_name}: ${st.message || DOT_WORDS[st.state] || st.state}`;
            dot.setAttribute('role', 'img');
            dot.setAttribute('aria-label', dot.title);
            dots.appendChild(dot);
        });
        return dots;
    }

    function seasonCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspSeason';
        if (state.seasonError) {
            card.append(el('div', 'fw-semibold', 'Couldn\'t load this season'), el('div', 'insp-small', state.seasonError));
            return card;
        }
        const payload = state.season;
        if (!payload) {
            card.append(el('span', 'spinner-border spinner-border-sm me-2'), el('span', '', 'Loading the season…'));
            return card;
        }
        const servers = payload.servers || [];
        const episodes = payload.episodes || [];
        const counts = payload.counts || {};
        const on = servers.filter(function (s) { return s.markers_enabled; }).length;
        const head = el('div', 'd-flex justify-content-between align-items-start flex-wrap gap-2 mb-3');
        const titles = el('div');
        const show = String(payload.show || '').replace(/\s*\{[a-z]+-[^}]*\}/gi, '').trim();
        titles.appendChild(el('div', 'insp-card-title', `${show} · ${payload.season || ''}`));
        const total = counts.total_episodes || counts.episodes || 0;
        titles.appendChild(el('div', 'insp-small mt-1', total > MAX_GROUP_EPISODES
            ? `${total} episodes (showing the ${MAX_GROUP_EPISODES} nearest)` : `${total} episodes`));
        const acts = el('div', 'd-flex align-items-center gap-2 flex-wrap');
        const ready = el('span', 'insp-chip', `${counts.ready || 0} ready`);
        ready.id = 'inspSeasonReady';
        acts.appendChild(ready);
        if (counts.needs_review) {
            const review = el('span', 'insp-chip insp-state-review', `${counts.needs_review} need review`);
            review.id = 'inspSeasonReview';
            acts.appendChild(review);
        }
        const publish = button(state.publishing ? 'Queueing…' : `Publish ${counts.ready || 0} to ${on} server${on === 1 ? '' : 's'}`,
            'btn btn-insp-primary', publishSeason);
        publish.id = 'inspPublishSeason';
        publish.disabled = state.publishing || !counts.ready || !on;
        acts.appendChild(withInfo(publish, TIPS.publish));
        head.append(titles, acts);
        card.appendChild(head);

        const scale = Math.max.apply(null, [0].concat(episodes.map(function (e) { return e.duration_ms || 0; })));
        const grid = el('div', 'insp-season');
        const top = el('div', 'insp-season-row insp-season-head');
        top.append(el('div', '', 'EP'), el('div', '', scale ? `INTRO & CREDITS · 0:00 – ${clock(scale)}, ONE SCALE` : 'INTRO & CREDITS'),
            el('div', '', 'TIMES'), el('div', '', 'SOURCES'), el('div', '', servers.map(function (s) { return s.server_name; }).join(' · ')));
        grid.appendChild(top);
        episodes.forEach(function (ep) {
            const row = el('button', 'insp-season-row' + (ep.path === state.path ? ' is-current' : ''));
            row.type = 'button';
            row.dataset.path = ep.path;
            row.dataset.episode = ep.episode || ep.name;
            row.setAttribute('aria-label', `Open ${ep.episode || ep.name}`);
            const times = seasonTimes(ep);
            const timesCell = el('div', times[1], times[0]);
            if (ep.needs_review && ep.review_reason) timesCell.title = ep.review_reason;
            const chips = el('div', 'insp-season-chips');
            (ep.evidence || []).forEach(function (chip) {
                const name = CHIP_NAMES[chip.source] || chip.source;
                chips.appendChild(el('span', 'insp-mini-chip', chip.label ? `${name} ${chip.label}` : name));
            });
            if (['intro', 'credits'].some(function (t) { return ep[t] && ep[t].marker && ep[t].marker.locked; })) {
                chips.appendChild(el('span', 'insp-mini-chip is-locked', '🔒 Locked by you'));
            }
            row.append(el('div', 'fw-semibold', ep.episode || ep.name), seasonLane(ep, scale), timesCell, chips, seasonDots(ep, servers));
            row.addEventListener('click', function () { openFile(ep.path, { scope: 'episode' }); });
            grid.appendChild(row);
        });
        card.appendChild(grid);
        card.appendChild(el('div', 'insp-small mt-2', 'Intro in blue, credits in orange, each episode drawn to one scale. Dots: green = server shows our markers, amber = waiting, red = failed, grey = off, skipped or nothing sent yet. Choose an episode to open it.'));
        return card;
    }

    async function publishSeason() {
        const path = state.path;
        state.publishing = true;
        render();
        try {
            const data = await sendJson('POST', '/api/markers/season/publish', { path: path });
            toast('Publish season', 'Queued — see the Dashboard', 'success');
            if (state.path === path) {
                state.job = { id: data.job_id, kind: MARKERS_JOB, status: 'pending', name: 'Publish season', percent: 0 };
            }
        } catch (e) {
            toast('Publish season', `Couldn't queue it: ${e.message}`, 'danger');
        }
        state.publishing = false;
        if (state.path === path) render();
    }

    // ---------------------------------------------------------------- summary

    function typePhrase(types) {
        const words = types.map(function (t) { return TYPE_WORDS[t]; });
        if (words.length === 2 && words.indexOf('intro') !== -1 && words.indexOf('credits') !== -1) return 'intro and credits';
        return joinWith(words, 'and');
    }

    function serverSentences() {
        // Servers in the same state share one sentence: each phrase is [said of one server, said of several].
        const groups = {};
        const order = [];
        const add = function (one, many, name) {
            if (!groups[one]) { groups[one] = { many: many, names: [] }; order.push(one); }
            groups[one].names.push(name);
        };
        const shown = decidedTypes();
        servers().forEach(function (s) {
            const name = serverName(s);
            const mine = shown.filter(function (t) { return (s.can_show || []).indexOf(t) !== -1; });
            const them = mine.length === 2 ? 'both' : (mine.length === 1 ? 'it' : 'them');
            const onNext = `${mine.length === 1 ? 'it' : 'them'} on the next job`;
            if (s.error || s.plan === 'unknown') add('couldn\'t be read just now', 'couldn\'t be read just now', name);
            else if (s.plan === 'up_to_date') add(`has ${them}`, `have ${them}`, name);
            else if (s.plan === 'will_add') add(`gets ${onNext}`, `get ${onNext}`, name);
            else if (s.plan === 'will_replace') add('shows other times; the next job replaces them', 'show other times; the next job replaces them', name);
            else if (s.plan === 'will_remove') add('loses the markers this app sent, on the next job', 'lose the markers this app sent, on the next job', name);
            else if (s.plan === 'waiting') add('is waiting for its other versions to agree', 'are waiting for their other versions to agree', name);
            else if (s.plan === 'keeps_plex' || s.plan === 'keeps_emby') add('keeps its own markers', 'keep their own markers', name);
            else if (s.plan === 'not_enabled') add('has Intro & Credits turned off', 'have Intro & Credits turned off', name);
        });
        return order.map(function (one) {
            const group = groups[one];
            return `${joinWith(group.names, 'and')} ${group.names.length > 1 ? group.many : one}.`;
        });
    }

    function summarySentence() {
        const node = el('div', 'insp-sentence');
        node.id = 'inspSummary';
        const dur = duration();
        const bits = [];
        const intro = decided('intro');
        const credits = decided('credits');
        if (intro) bits.push(['Skip Intro runs ', `${clock(intro.start_ms)} – ${clock(segmentEnd(intro, dur))}`, 'insp-t-intro']);
        if (credits) {
            const t = toEnd(credits, dur) ? clock(credits.start_ms) : `${clock(credits.start_ms)} – ${clock(credits.end_ms)}`;
            bits.push([toEnd(credits, dur) ? 'Skip Credits starts at ' : 'Skip Credits runs ', t, 'insp-t-credits']);
        }
        ['recap', 'preview'].forEach(function (t) {
            const m = decided(t);
            if (m) bits.push([`${TYPE_LABELS[t]} `, rangeText(m, dur), 'insp-t-intro']);
        });
        if (!bits.length) {
            const review = reviewTypes();
            if (review.length) node.textContent = `The ${typePhrase(review)} need${review.length === 1 && review[0] !== 'credits' ? 's' : ''} your check above.`;
            else node.textContent = 'No intro or credits were found for this file. Adjust adds them by hand.';
            return node;
        }
        bits.forEach(function (bit, i) {
            if (i > 0) node.append(i === bits.length - 1 ? ' and ' : ', ');
            node.append(bit[0]);
            node.appendChild(el('span', bit[2], bit[1]));
        });
        node.append('.');
        serverSentences().forEach(function (s) { node.append(' ' + s); });
        return node;
    }

    function notCheckedText() {
        const shown = serverMarkers();
        const keep = servers().filter(function (s) { return s.keeps_server_markers && s.markers_enabled && (s.current || []).length; });
        const parts = [];
        if (shown.length) {
            const byServer = {};
            shown.forEach(function (x) {
                const n = serverName(x.server);
                byServer[n] = byServer[n] || [];
                byServer[n].push(x.marker);
            });
            const said = Object.keys(byServer).map(function (n) {
                const ms = byServer[n];
                const types = ms.map(function (m) { return m.type; }).filter(function (t, i, all) { return all.indexOf(t) === i; });
                return `${n} shows ${ms.length} ${typePhrase(types)} marker${ms.length === 1 ? '' : 's'} today`;
            });
            parts.push(`${joinWith(said, 'and')}, drawn in grey on the frames below so you can see where they land.`);
        } else if (servers().every(function (s) { return Array.isArray(s.current); })) {
            parts.push('No server shows intro or credits markers for this file yet.');
        }
        parts.push(`Checking the ${state.file && state.file.kind === 'episode' ? 'episode' : 'film'} decides its own.`);
        keep.forEach(function (s) {
            const vendor = VENDOR_NAMES[s.server_type] || serverName(s);
            parts.push(`With “Keep ${vendor}'s” on, ${serverName(s)}'s markers stay as they are.`);
        });
        return parts.join(' ');
    }

    function factsChips() {
        const chips = el('div', 'insp-chips');
        chips.id = 'inspFacts';
        const p = preview();
        const dur = duration();
        if (p) {
            if (p.created_at) chips.appendChild(el('span', 'insp-chip', `Preview made ${when(p.created_at)}`));
            const step = interval();
            const parts = [`${Number(p.frame_count).toLocaleString()} frames`];
            if (step) parts.push(`one every ${seconds(step)}`);
            const covers = step ? p.frame_count * step : 0;
            if (covers) parts.push(`covers ${clock(covers)}`);
            chips.appendChild(el('span', 'insp-chip', parts.join(' · ')));
            if (p.file_size) chips.appendChild(el('span', 'insp-chip', bytes(p.file_size)));
        } else if (!state.bifOnly) {
            const chip = el('span', 'insp-chip', 'No preview yet');
            chip.id = 'inspNoPreview';
            chips.appendChild(chip);
        }
        if (!p && dur) chips.appendChild(el('span', 'insp-chip', `Runs ${clock(dur)}`));
        const checkedAt = ((state.item && state.item.evidence) || []).map(function (e) { return e.fetched_at; }).filter(Boolean).sort().pop();
        if (checkedAt) chips.appendChild(el('span', 'insp-chip', `Intro & Credits checked ${when(checkedAt)}`));
        return chips;
    }

    function factsCard() {
        const card = el('div', 'insp-card');
        card.appendChild(factsChips());
        return card;
    }

    function unlockControls() {
        const locked = lockedTypes();
        if (!locked.length) return null;
        const row = el('div', 'd-flex align-items-center gap-2 flex-wrap mt-3');
        row.id = 'inspLocked';
        row.appendChild(el('i', 'bi bi-lock-fill'));
        row.appendChild(el('span', '', `You set the ${typePhrase(locked)}, so later checks keep ${locked.length === 1 && locked[0] !== 'credits' ? 'it' : 'them'}.`));
        if (state.confirmUnlock) {
            row.appendChild(el('span', 'insp-small', 'Back to automatic? Your times stay on your servers for now; the next check decides again and may move them.'));
            const yes = button('Go back to automatic', 'btn btn-sm btn-outline-danger', unlock);
            yes.id = 'inspUnlockConfirm';
            const no = button('Keep them locked', 'btn btn-sm btn-outline-secondary', function () { state.confirmUnlock = false; render(); });
            row.append(yes, no);
        }
        return row;
    }

    function summaryCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspSummaryCard';
        if (!state.item) {
            card.appendChild(el('div', 'fw-semibold', 'Intro & Credits couldn\'t be read for this file'));
            if (state.itemError) card.appendChild(el('div', 'insp-small', state.itemError));
        } else if (!isChecked()) {
            card.dataset.mode = 'unchecked';
            card.append(el('div', 'fw-semibold fs-5', 'Not checked by Intro & Credits yet'), el('div', 'mt-1', notCheckedText()));
        } else {
            card.dataset.mode = 'checked';
            card.appendChild(summarySentence());
        }
        card.appendChild(factsChips());
        const unlockRow = unlockControls();
        if (unlockRow) card.appendChild(unlockRow);
        if (state.confirmLock && lockableTypes().length) card.appendChild(lockConfirmRow());
        return card;
    }

    // --------------------------------------------------------------- timeline

    function niceStep(total) {
        const steps = [60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3];
        return steps.find(function (s) { return total / s <= 6; }) || 3600e3;
    }

    function axis(start, end) {
        const node = el('div', 'insp-axis');
        const span = end - start;
        const step = niceStep(span);
        const first = Math.ceil(start / step) * step;
        const add = function (ms, alignRight) {
            const s = el('span', '', clock(ms));
            if (alignRight) s.style.right = '0';
            else s.style.left = `${pct(ms - start, span)}%`;
            node.appendChild(s);
        };
        add(start, false);
        for (let t = first === start ? first + step : first; t < end - step * 0.6; t += step) add(t, false);
        add(end, true);
        return node;
    }

    function strip(start, end, count, tall) {
        const node = el('div', 'insp-strip' + (tall ? ' is-tall' : ''));
        const p = preview();
        const span = end - start;
        for (let i = 0; i < count; i++) {
            const t = start + (span * (i + 0.5)) / count;
            const img = el('img');
            img.loading = 'lazy';
            img.alt = `Frame at ${clock(t)}`;
            img.src = frameUrl(frameIndexAt(t));
            img.title = clock(t);
            node.appendChild(img);
        }
        if (!p) node.hidden = true;
        return node;
    }

    function band(seg, start, end, className, dur) {
        const b = el('div', 'insp-band ' + className);
        const span = end - start;
        const from = Math.max(seg.start_ms, start);
        const to = Math.min(segmentEnd(seg, dur), end);
        b.style.left = `${pct(from - start, span)}%`;
        b.style.width = `${Math.max(0, pct(to - start, span) - pct(from - start, span))}%`;
        return b;
    }

    function laneRow(grid, name, strong, node) {
        const label = el('div', 'insp-lane-name' + (strong ? ' is-strong' : ''), name);
        grid.append(label, node);
    }

    function lane(items, start, end, dur, note) {
        const node = el('div', 'insp-lane');
        const span = end - start;
        if (note) {
            node.appendChild(el('div', 'insp-lane-note', note));
            return node;
        }
        items.forEach(function (item) {
            const seg = item.seg;
            const from = Math.max(seg.start_ms, start);
            const to = Math.min(segmentEnd(seg, dur), end);
            if (to < start || from > end) return;
            const bar = el('div', 'insp-bar ' + item.className);
            bar.style.left = `${pct(from - start, span)}%`;
            bar.style.width = `${Math.max(0, pct(to - start, span) - pct(from - start, span))}%`;
            node.appendChild(bar);
            if (item.label) {
                const label = el('div', 'insp-lane-label', item.label);
                label.dataset.at = String(pct(from - start, span));
                label.dataset.until = String(pct(to - start, span));
                if (item.inside) label.dataset.inside = '1';
                node.appendChild(label);
            }
        });
        node.dataset.layout = '1';
        return node;
    }

    // Labels in a lane never overlap. Each label tries, in order: inside its bar (a bar that runs to the end, when the
    // label fits), just after the bar, just before it, then a little further right of the label before it; the first
    // place clear of the labels already on the row wins. When nothing on the first row is clear it drops to a second.
    const MAX_SHIFT_PX = 160;
    const GAP_PX = 8;

    function layoutLabels(laneNode) {
        const width = laneNode.clientWidth;
        if (!width) return;
        const labels = Array.prototype.slice.call(laneNode.querySelectorAll(':scope > .insp-lane-label'));
        labels.sort(function (a, b) { return Number(a.dataset.at) - Number(b.dataset.at); });
        const rowRight = [-Infinity, -Infinity];
        let usedSecond = false;
        labels.forEach(function (label) {
            label.classList.remove('is-inside', 'is-row2');
            const w = label.offsetWidth;
            const at = (Number(label.dataset.at) / 100) * width;
            const until = (Number(label.dataset.until) / 100) * width;
            const options = [];
            if (label.dataset.inside && until - at >= w + GAP_PX) options.push({ left: at + 4, inside: true });
            options.push({ left: until + 6 }, { left: at - 6 - w });
            let placed = null;
            for (let row = 0; row < 2 && !placed; row++) {
                const clear = function (left) { return left >= 0 && left + w <= width && left >= rowRight[row] + GAP_PX; };
                const hit = options.find(function (o) { return clear(o.left); });
                if (hit) placed = { row: row, left: hit.left, inside: !!hit.inside };
                else {
                    const shifted = rowRight[row] + GAP_PX;
                    if (clear(shifted) && shifted - until <= MAX_SHIFT_PX) placed = { row: row, left: shifted };
                }
            }
            if (!placed) placed = { row: 1, left: Math.max(0, Math.min(at, width - w)) };
            rowRight[placed.row] = placed.left + w;
            if (placed.row === 1) {
                usedSecond = true;
                label.classList.add('is-row2');
            }
            if (placed.inside) label.classList.add('is-inside');
            label.style.left = `${placed.left}px`;
        });
        laneNode.classList.toggle('has-row2', usedSecond);
    }

    function layoutAllLabels() {
        document.querySelectorAll('#inspFile [data-layout="1"]').forEach(layoutLabels);
    }

    function decidedLaneItems(dur) {
        return decidedTypes().map(function (t) {
            const m = decided(t);
            const label = toEnd(m, dur) ? `${TYPE_LABELS[t]} ${clock(m.start_ms)} → end` : `${TYPE_LABELS[t]} ${clock(m.start_ms)}–${clock(m.end_ms)}`;
            return { seg: m, className: 'insp-bar-' + t, label: label, inside: TO_END_TYPES.indexOf(t) !== -1 };
        });
    }

    function sameAsDecided(server) {
        return server.plan === 'up_to_date' || (server.plan && server.plan.indexOf('keeps_') === 0 && !(server.current || []).length);
    }

    function serverLane(server, start, end, dur, numbered) {
        const current = Array.isArray(server.current) ? server.current : null;
        if (server.error || current === null) return lane([], start, end, dur, 'Couldn\'t read what it shows now');
        if (!current.length) {
            if (!server.markers_enabled) return lane([], start, end, dur, 'Nothing here · Intro & Credits is off for it');
            const wanted = decidedTypes().filter(function (t) { return (server.can_show || []).indexOf(t) !== -1; });
            if (wanted.length) return lane([], start, end, dur, `Nothing yet · the next job adds ${typePhrase(wanted)}`);
            return lane([], start, end, dur, 'Nothing yet');
        }
        const same = isChecked() && sameAsDecided(server);
        const sorted = current.slice().sort(function (a, b) { return a.start_ms - b.start_ms; });
        const items = sorted.map(function (m, i) {
            let label = null;
            if (same) label = i === 0 ? 'Same as decided' : null;
            else {
                const n = numbered ? `${CIRCLED[i] || i + 1} ` : '';
                label = `${n}${TYPE_LABELS[m.type] || m.type} ${rangeText(m, dur)}${m.stale ? ' (made for an earlier file)' : ''}`;
            }
            return { seg: m, className: 'insp-bar-server', label: label };
        });
        return lane(items, start, end, dur, null);
    }

    function nearEndMarkers(dur) {
        return serverMarkers().filter(function (x) { return x.marker.start_ms >= dur * (1 - ENDING_SHARE); });
    }

    function wholeFileCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspWholeFile';
        const dur = duration();
        const top = el('div', 'd-flex justify-content-between align-items-center gap-2 flex-wrap mb-3');
        const kind = state.file && state.file.kind === 'episode' ? 'EPISODE' : 'FILM';
        top.appendChild(el('div', 'insp-card-title', dur ? `Whole ${kind.toLowerCase()} · 0:00 – ${clock(dur)}` : `Whole ${kind.toLowerCase()}`));
        if (preview()) {
            const seg = el('div', 'insp-seg');
            seg.setAttribute('role', 'group');
            seg.setAttribute('aria-label', 'Show the timeline or every frame');
            const tl = button('Timeline', 'btn' + (state.view === 'timeline' ? ' active' : ''), function () { state.view = 'timeline'; render(); });
            tl.id = 'inspViewTimeline';
            tl.setAttribute('aria-pressed', state.view === 'timeline' ? 'true' : 'false');
            const all = button('All frames', 'btn' + (state.view === 'all' ? ' active' : ''), function () { state.view = 'all'; render(); });
            all.id = 'inspViewAll';
            all.setAttribute('aria-pressed', state.view === 'all' ? 'true' : 'false');
            seg.append(tl, all);
            top.appendChild(withInfo(seg, TIPS.allFrames));
        }
        card.appendChild(top);
        if (state.view === 'all' && preview()) {
            card.appendChild(allFrames());
            return card;
        }
        if (!dur) {
            card.appendChild(el('div', 'insp-empty-strip', 'This file\'s length isn\'t known yet, so there is no timeline. Checking its intro & credits reads it.'));
            return card;
        }
        const grid = el('div', 'insp-timeline');
        const checked = isChecked();
        if (checked && decidedTypes().length) {
            const labels = lane(decidedLaneItems(dur).map(function (x) {
                return { seg: { start_ms: x.seg.start_ms, end_ms: x.seg.start_ms + 1 }, className: 'd-none', label: x.label.replace(' → end', '') };
            }), 0, dur, dur, null);
            labels.classList.add('bg-transparent');
            laneRow(grid, '', false, labels);
        }
        const wrap = el('div', 'insp-strip-wrap');
        wrap.id = 'inspFilmstrip';
        if (preview()) {
            wrap.appendChild(strip(0, dur, Math.max(8, Math.min(24, Math.round(((wrap.clientWidth || 1100) / 70))) || 16), false));
            if (checked) {
                decidedTypes().forEach(function (t) {
                    wrap.appendChild(band(decided(t), 0, dur, START_TYPES.indexOf(t) !== -1 ? 'insp-band-intro' : 'insp-band-credits', dur));
                });
            } else {
                serverMarkers().forEach(function (x) { wrap.appendChild(band(x.marker, 0, dur, 'insp-band-server', dur)); });
            }
        } else {
            const empty = el('div', 'insp-empty-strip');
            empty.id = 'inspNoPreviewStrip';
            empty.append(el('i', 'bi bi-image'), el('span', '', 'No preview yet, so there are no frames to draw here. Regenerate preview makes one; the close-ups below read frames straight from the video.'));
            wrap.appendChild(empty);
        }
        laneRow(grid, 'Preview frames', false, wrap);
        laneRow(grid, '', false, axis(0, dur));
        if (checked) laneRow(grid, 'Decided', true, lane(decidedLaneItems(dur), 0, dur, dur, decidedTypes().length ? null : 'Nothing decided'));
        servers().forEach(function (s) {
            let node = serverLane(s, 0, dur, dur, false);
            if (!checked) {
                const near = nearEndMarkers(dur).filter(function (x) { return x.server === s; });
                if (near.length > 1) {
                    node = lane(near.map(function (x) { return { seg: x.marker, className: 'insp-bar-server', label: null }; }), 0, dur, dur, null);
                    const n = el('div', 'insp-lane-label', `${near.length} ${typePhrase([near[0].marker.type])} markers near the end · zoomed below`);
                    n.dataset.at = String(pct(near[0].marker.start_ms, dur));
                    n.dataset.until = n.dataset.at;
                    node.appendChild(n);
                }
            }
            node.dataset.serverId = s.server_id || '';
            laneRow(grid, `${serverName(s)} now`, false, node);
        });
        card.appendChild(grid);
        return card;
    }

    function endingCard() {
        const dur = duration();
        if (!dur || isChecked() || !preview()) return null;
        const near = nearEndMarkers(dur);
        if (!near.length) return null;
        const first = Math.min.apply(null, near.map(function (x) { return x.marker.start_ms; }));
        const start = Math.max(0, Math.floor((first - 60000) / 60000) * 60000);
        const card = el('div', 'insp-card');
        card.id = 'inspEnding';
        card.appendChild(el('div', 'insp-card-title mb-3', `Ending, zoomed · ${clock(start)} – ${clock(dur)} (end of file)`));
        const grid = el('div', 'insp-timeline');
        const wrap = el('div', 'insp-strip-wrap');
        wrap.appendChild(strip(start, dur, 12, true));
        near.forEach(function (x) { wrap.appendChild(band(x.marker, start, dur, 'insp-band-server', dur)); });
        laneRow(grid, 'Preview frames', false, wrap);
        laneRow(grid, '', false, axis(start, dur));
        const counts = {};
        near.forEach(function (x) {
            const name = serverName(x.server);
            counts[name] = (counts[name] || 0) + 1;
            const n = counts[name];
            const len = toEnd(x.marker, dur) ? '' : ` (${seconds(segmentEnd(x.marker, dur) - x.marker.start_ms)})`;
            const label = `${TYPE_LABELS[x.marker.type] || x.marker.type} ${rangeText(x.marker, dur)}${len}`;
            laneRow(grid, `${name} ${CIRCLED[n - 1] || n}`, false, lane([{ seg: x.marker, className: 'insp-bar-server', label: label, inside: toEnd(x.marker, dur) }], start, dur, dur, null));
        });
        card.appendChild(grid);
        return card;
    }

    // --------------------------------------------------------------- close-ups

    function frameTile(src, t, options) {
        const opts = options || {};
        const tile = el(opts.onPick ? 'button' : 'div', 'insp-frame' + (opts.ringed ? ' is-ringed' : ''));
        if (opts.onPick) {
            tile.type = 'button';
            tile.setAttribute('aria-label', `Frame at ${clock(t)}${opts.pickLabel ? ': ' + opts.pickLabel : ''}`);
            tile.addEventListener('click', function () { opts.onPick(t); });
        }
        tile.dataset.t = String(t);
        if (src) {
            const img = el('img');
            img.alt = `Frame at ${clock(t)}`;
            img.src = src;
            img.loading = 'lazy';
            tile.appendChild(img);
        } else {
            tile.appendChild(el('span', 'insp-frame-missing'));
        }
        tile.appendChild(el('span', '', clock(t)));
        return tile;
    }

    // Preview frames on both sides of an edge: the frame at or after the edge is the first one of the segment.
    function previewFramesAround(edge, cutClass) {
        const row = el('div', 'insp-frames');
        const step = interval();
        const first = Math.ceil(edge / step);
        const count = preview().frame_count;
        for (let i = first - CLOSEUP_SIDE; i < first; i++) {
            if (i >= 0) row.appendChild(frameTile(frameUrl(i), i * step));
        }
        row.appendChild(el('div', 'insp-cut ' + (cutClass || '')));
        for (let i = first; i < first + CLOSEUP_SIDE && i < count; i++) row.appendChild(frameTile(frameUrl(i), i * step));
        return row;
    }

    function exactKey(start, count) {
        return `${state.path}|${start}|${count}`;
    }

    // At most two frame reads in flight from this page (the server reads two at a time and turns the rest away), and a
    // read the server was too busy for is asked again a little later.
    const FRAME_FETCHES = 2;
    const FRAME_RETRIES = 6;
    let framesRunning = 0;
    const framesWaiting = [];

    function limited(task) {
        return new Promise(function (resolve, reject) {
            const run = function () {
                framesRunning++;
                task().then(resolve, reject).finally(function () {
                    framesRunning--;
                    const next = framesWaiting.shift();
                    if (next) next();
                });
            };
            if (framesRunning < FRAME_FETCHES) run();
            else framesWaiting.push(run);
        });
    }

    async function fetchFrames(url) {
        for (let attempt = 1; ; attempt++) {
            const res = await getJson(url);
            if (res.ok) return res.data.frames || [];
            if (res.status !== 503 || attempt >= FRAME_RETRIES) throw new Error((res.data && res.data.error) || `HTTP ${res.status}`);
            await new Promise(function (r) { setTimeout(r, 700 * attempt); });
        }
    }

    function exactFrames(start, count) {
        const key = exactKey(start, count);
        if (!exactCache.has(key)) {
            const params = new URLSearchParams({ path: state.path, start_ms: String(start), count: String(count), width: '320' });
            const promise = limited(function () { return fetchFrames('/api/inspector/frames?' + params.toString()); });
            promise.catch(function () { exactCache.delete(key); });
            exactCache.set(key, promise);
        }
        return exactCache.get(key);
    }

    // Exact frames one second apart; the frame at ``edge`` is the first after the cut. ``pick`` makes them buttons.
    function exactFramesAround(edge, options) {
        const opts = options || {};
        const count = opts.count || EXACT_COUNT;
        const before = opts.before === undefined ? CLOSEUP_SIDE : opts.before;
        const start = Math.max(0, Math.round(edge / 1000) * 1000 - before * 1000);
        const row = el('div', 'insp-frames');
        row.dataset.exact = String(start);
        const placeholders = function () {
            const nodes = [];
            for (let i = 0; i < count; i++) {
                const t = start + i * 1000;
                if (opts.cut && t === edgeRounded(edge)) nodes.push(el('div', 'insp-cut ' + (opts.cutClass || '')));
                nodes.push(frameTile('', t));
            }
            return nodes;
        };
        row.replaceChildren.apply(row, placeholders());
        exactFrames(start, count).then(function (frames) {
            if (!row.isConnected && !document.body.contains(row)) return;
            const nodes = [];
            frames.forEach(function (f) {
                if (opts.cut && f.t_ms === edgeRounded(edge)) nodes.push(el('div', 'insp-cut ' + (opts.cutClass || '')));
                nodes.push(frameTile(f.src, f.t_ms, {
                    onPick: opts.onPick,
                    ringed: opts.ring !== undefined && f.t_ms === edgeRounded(opts.ring),
                    pickLabel: opts.pickLabel,
                }));
            });
            row.replaceChildren.apply(row, nodes);
        }).catch(function (e) {
            row.replaceChildren(el('div', 'insp-small text-warning-emphasis', `Couldn't read frames here: ${e.message}`));
        });
        return row;
    }

    function edgeRounded(ms) {
        return Math.round(ms / 1000) * 1000;
    }

    function stepNote() {
        const step = interval();
        return preview() && step ? `frames every ${seconds(step)}` : 'frames one second apart';
    }

    function edgeBlock(labelText, edge, cutClass) {
        const frag = el('div');
        frag.appendChild(el('div', 'insp-edge-label', labelText));
        frag.appendChild(preview() && interval() ? previewFramesAround(edge, cutClass) : exactFramesAround(edge, { cut: true, cutClass: cutClass }));
        return frag;
    }

    function closeupCard(type, m, dur) {
        const card = el('div', 'insp-card');
        card.dataset.closeup = type;
        const top = el('div', 'd-flex justify-content-between align-items-center gap-2');
        const isStart = START_TYPES.indexOf(type) !== -1;
        const range = toEnd(m, dur) ? `${clock(m.start_ms)} → end (${clock(dur)})` : `${clock(m.start_ms)} – ${clock(segmentEnd(m, dur))}`;
        top.append(el('div', 'insp-closeup-title ' + (isStart ? 'is-intro' : 'is-credits'), `${TYPE_LABELS[type]} · ${range}`), el('div', 'insp-small', stepNote()));
        card.appendChild(top);
        const cutClass = isStart ? 'is-intro' : '';
        card.appendChild(edgeBlock(`Starts at ${clock(m.start_ms)}`, m.start_ms, cutClass));
        if (!toEnd(m, dur)) card.appendChild(edgeBlock(`Ends at ${clock(m.end_ms)}`, m.end_ms, cutClass));
        else {
            const note = el('div', 'insp-card mt-3 py-2');
            note.style.background = 'var(--insp-raised)';
            const kind = state.file && state.file.kind === 'episode';
            note.appendChild(el('div', '', kind ? 'Runs to the end of the file. Skipping credits jumps to the next episode.' : 'Runs to the end of the file.'));
            const step = interval();
            const lastBefore = preview() && step ? (Math.ceil(m.start_ms / step) - 1) * step : -1;
            if (lastBefore >= 0) {
                note.appendChild(el('div', 'insp-small', `The last story frame is ${clock(lastBefore)}; the ${TYPE_WORDS[type]} start${type === 'credits' ? '' : 's'} on the next frame.`));
            }
            card.appendChild(note);
        }
        const hint = m.locked
            ? 'Set by you: later checks keep these times.'
            : (state.item && state.item.duration_ms ? 'Adjust moves these edges one second at a time, on frames from the video.' : '');
        if (hint) card.appendChild(el('div', 'insp-frames-note', hint));
        return card;
    }

    function serverCloseup(x, n, dur) {
        const card = el('div', 'insp-card');
        card.dataset.closeup = 'server';
        const top = el('div', 'd-flex justify-content-between align-items-center gap-2');
        top.append(el('div', 'insp-closeup-title', `${serverName(x.server)} ${CIRCLED[n - 1] || n} starts ${clock(x.marker.start_ms)}`), el('div', 'insp-small', stepNote()));
        card.appendChild(top);
        const block = edgeBlock(`${TYPE_LABELS[x.marker.type] || x.marker.type} ${rangeText(x.marker, dur)}`, x.marker.start_ms, 'is-server');
        card.appendChild(block);
        const own = x.marker.ours ? 'Sent there by this app earlier.' : `${serverName(x.server)}'s own marker${x.marker.stale ? ', made for an earlier file at this path' : ''}.`;
        card.appendChild(el('div', 'insp-frames-note', own));
        return card;
    }

    function closeupCards() {
        const dur = duration();
        if (!dur || !state.item) return null;
        const grid = el('div', 'insp-grid-2');
        grid.id = 'inspCloseups';
        if (state.adjust) {
            adjustTypes().forEach(function (type) { grid.appendChild(adjustCard(type, dur)); });
            addableTypes().forEach(function (type) { grid.appendChild(addCard(type)); });
            unadjustableTypes().forEach(function (type) {
                const card = el('div', 'insp-card');
                card.dataset.cantAdjust = type;
                card.appendChild(el('div', 'insp-closeup-title', `${TYPE_LABELS[type]} · ${rangeText(decided(type), dur)}`));
                card.appendChild(el('div', 'insp-small mt-2', `No server with Intro & Credits on for this file shows ${TYPE_PLURALS[type]}, so there is nothing to send it to and it can't be adjusted here.`));
                grid.appendChild(card);
            });
            return grid;
        }
        if (isChecked()) {
            const types = decidedTypes();
            if (!types.length) return null;
            types.forEach(function (t) { grid.appendChild(closeupCard(t, decided(t), dur)); });
            return grid;
        }
        const counts = {};
        const shown = serverMarkers();
        if (!shown.length) return null;
        shown.forEach(function (x) {
            const name = serverName(x.server);
            counts[name] = (counts[name] || 0) + 1;
            grid.appendChild(serverCloseup(x, counts[name], dur));
        });
        return grid;
    }

    // ------------------------------------------------------------------ adjust

    function adjustTypes() {
        return TYPES.filter(function (t) { return !!state.adjust.model[t]; });
    }

    function addableTypes() {
        return ['intro', 'credits'].filter(function (t) {
            if (state.adjust.model[t]) return false;
            if (t === 'intro' && state.item && state.item.is_movie) return false;
            return enabledOwners(t).length > 0;
        });
    }

    // A decided type no server with Intro & Credits on (for this file) can show: saving it would refuse the whole save,
    // so Adjust leaves it out and says why.
    function unadjustableTypes() {
        return decidedTypes().filter(function (t) { return !enabledOwners(t).length; });
    }

    function startAdjust() {
        const dur = duration();
        const model = {};
        decidedTypes().filter(function (t) { return enabledOwners(t).length > 0; }).forEach(function (t) {
            const m = decided(t);
            model[t] = { start: m.start_ms, end: segmentEnd(m, dur), toEnd: TO_END_TYPES.indexOf(t) !== -1 && toEnd(m, dur), changed: false };
        });
        state.adjust = { model: model, saving: false, error: '' };
        render();
        const first = document.querySelector('#inspCloseups [data-closeup]');
        if (first) first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function cancelAdjust() {
        state.adjust = null;
        render();
    }

    function setEdge(type, edge, ms) {
        const dur = duration();
        const m = state.adjust.model[type];
        const value = Math.max(0, Math.min(dur, edgeRounded(ms)));
        if (edge === 'start') m.start = value;
        else m.end = value;
        m.changed = true;
        state.adjust.error = '';
        render();
    }

    function edgeEditor(type, edge, dur) {
        const m = state.adjust.model[type];
        const value = edge === 'start' ? m.start : m.end;
        const isStart = START_TYPES.indexOf(type) !== -1;
        const block = el('div');
        block.dataset.edge = `${type}-${edge}`;
        block.appendChild(el('div', 'insp-edge-label', `${edge === 'start' ? 'Starts' : 'Ends'} at ${clock(value)}`));
        block.appendChild(exactFramesAround(value, {
            cut: true,
            cutClass: isStart ? 'is-intro' : '',
            onPick: function (t) { setEdge(type, edge, t); },
            pickLabel: `set the ${TYPE_WORDS[type]} ${edge} here`,
        }));
        const nudge = el('div', 'insp-nudge');
        const earlier = button('◀\uFE0E 1 s', 'btn btn-sm btn-outline-secondary', function () { setEdge(type, edge, value - 1000); });
        earlier.setAttribute('aria-label', `Move the ${TYPE_WORDS[type]} ${edge} one second earlier`);
        const later = button('1 s ▶\uFE0E', 'btn btn-sm btn-outline-secondary', function () { setEdge(type, edge, value + 1000); });
        later.setAttribute('aria-label', `Move the ${TYPE_WORDS[type]} ${edge} one second later`);
        const input = el('input', 'form-control form-control-sm insp-mono');
        input.type = 'text';
        input.value = clock(value);
        input.setAttribute('aria-label', `${TYPE_LABELS[type]} ${edge} time`);
        const apply = function () {
            const ms = parseClock(input.value);
            if (ms === null) {
                input.classList.add('is-invalid');
                input.title = 'That isn\'t a time. Try 1:23 or 0:14.';
                return;
            }
            if (ms !== value) setEdge(type, edge, ms);
        };
        input.addEventListener('change', apply);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                apply();
            }
        });
        nudge.append(earlier, later, input, el('span', 'insp-small', 'Click a frame to put the edge there.'));
        block.appendChild(nudge);
        return block;
    }

    function adjustCard(type, dur) {
        const m = state.adjust.model[type];
        const card = el('div', 'insp-card');
        card.dataset.closeup = type;
        const isStart = START_TYPES.indexOf(type) !== -1;
        const range = m.toEnd ? `${clock(m.start)} → end` : `${clock(m.start)} – ${clock(m.end)}`;
        const top = el('div', 'd-flex justify-content-between align-items-center gap-2');
        top.append(el('div', 'insp-closeup-title ' + (isStart ? 'is-intro' : 'is-credits'), `${TYPE_LABELS[type]} · ${range}`), el('div', 'insp-small', 'frames one second apart, from the video'));
        card.appendChild(top);
        card.appendChild(edgeEditor(type, 'start', dur));
        if (TO_END_TYPES.indexOf(type) !== -1) {
            const sw = el('div', 'form-check form-switch mt-3');
            const input = el('input', 'form-check-input');
            input.type = 'checkbox';
            input.id = `inspToEnd-${type}`;
            input.checked = m.toEnd;
            input.addEventListener('change', function () {
                m.toEnd = input.checked;
                if (!m.toEnd && m.end >= dur - END_OF_FILE_MS) m.end = Math.max(m.start + 1000, dur - 10000);
                m.changed = true;
                render();
            });
            const label = el('label', 'form-check-label', 'Runs to the end of the file');
            label.htmlFor = input.id;
            sw.append(input, label);
            card.appendChild(sw);
        }
        if (!m.toEnd) card.appendChild(edgeEditor(type, 'end', dur));
        return card;
    }

    function addCard(type) {
        const card = el('div', 'insp-card d-flex flex-column gap-2');
        card.dataset.add = type;
        card.appendChild(el('div', 'insp-closeup-title ' + (type === 'intro' ? 'is-intro' : 'is-credits'), `${TYPE_LABELS[type]} · not found`));
        card.appendChild(el('div', 'insp-small', `Nothing was found for the ${TYPE_WORDS[type]}. Add one and move its edges to where it really is.`));
        const b = button(`Add ${TYPE_WORDS[type]}`, 'btn btn-outline-secondary align-self-start', function () {
            const dur = duration();
            // Round starting times, never something a source would answer, so they can't be read as a finding
            // (tests/markers/test_api_markers_edit.py keeps a copy of them: change both together).
            if (type === 'intro') state.adjust.model.intro = { start: 0, end: ADD_HEAD_MS, toEnd: false, changed: true };
            else state.adjust.model.credits = { start: Math.max(0, dur - ADD_TAIL_MS), end: dur, toEnd: true, changed: true };
            render();
        });
        b.id = `inspAdd-${type}`;
        card.appendChild(b);
        return card;
    }

    function receivers(types) {
        const names = [];
        enabledOwners().forEach(function (s) {
            if (types.some(function (t) { return (s.can_show || []).indexOf(t) !== -1; }) && names.indexOf(serverName(s)) === -1) names.push(serverName(s));
        });
        return names;
    }

    function adjustProblem() {
        const dur = duration();
        for (const type of adjustTypes()) {
            const m = state.adjust.model[type];
            const end = m.toEnd ? dur : m.end;
            const plural = type === 'credits';
            if (end <= m.start) return `The ${TYPE_WORDS[type]} ${plural ? 'have' : 'has'} to end after ${plural ? 'they start' : 'it starts'}.`;
            if (m.start >= dur) return `The ${TYPE_WORDS[type]} ${plural ? 'have' : 'has'} to start inside the file.`;
        }
        return '';
    }

    function adjustBar() {
        const types = adjustTypes();
        const bar = el('div', 'insp-card insp-confirm');
        bar.id = 'inspAdjustBar';
        const text = el('div');
        const names = receivers(types);
        text.append(el('div', 'fw-semibold', types.length ? `Your times: ${types.map(function (t) {
            const m = state.adjust.model[t];
            return `${TYPE_WORDS[t]} ${m.toEnd ? clock(m.start) + ' → end' : clock(m.start) + '–' + clock(m.end)}`;
        }).join(' · ')}` : 'Nothing to save yet'));
        text.appendChild(el('div', 'insp-small', 'Saving sends them now and keeps them through later checks. You can go back to automatic any time.'));
        const problem = state.adjust.error || adjustProblem();
        if (problem) text.appendChild(el('div', 'small text-danger mt-1', problem));
        const buttons = el('div', 'd-flex gap-2 flex-wrap');
        const cancel = button('Cancel', 'btn btn-outline-secondary', cancelAdjust);
        cancel.id = 'inspAdjustCancel';
        const save = button(state.adjust.saving ? 'Sending…' : `Save and send to ${names.length ? joinWith(names, 'and') : 'your servers'}`, 'btn btn-insp-primary', saveAdjust);
        save.id = 'inspAdjustSave';
        save.disabled = state.adjust.saving || !types.length || !!adjustProblem();
        buttons.append(cancel, save);
        bar.append(text, buttons);
        return bar;
    }

    async function saveMarkers(markers, path) {
        return sendJson('POST', '/api/markers/item/markers', { path: path, markers: markers });
    }

    // What the save did on each server, in one toast. The "Replaced …'s own marker" sentence is
    // markers.outcomes.replaced_own_note's (the save route hands over only the types) — change both together.
    function savedMessage(answer) {
        const rows = (answer && answer.servers) || [];
        const took = rows.filter(function (r) { return r.result === 'written' || r.result === 'unchanged'; }).map(function (r) { return r.server_name; });
        const later = rows.filter(function (r) { return r.result === 'waiting' || r.result === 'failed'; }).map(function (r) { return r.server_name; });
        const parts = [];
        if (took.length) parts.push(`${joinWith(took, 'and')} ${took.length === 1 ? 'has' : 'have'} them now.`);
        if (later.length) parts.push(`Your times are saved; ${joinWith(later, 'and')} ${later.length === 1 ? 'gets' : 'get'} them on the next job.`);
        rows.forEach(function (r) {
            const vendor = VENDOR_NAMES[String(r.server_type || '').toLowerCase()] || r.server_type;
            if ((r.replaced_own || []).length) {
                parts.push(`Replaced ${vendor}'s own marker. This server is set to keep ${vendor}'s, but a marker you adjust always wins.`);
            }
            (r.notes || []).forEach(function (note) {
                if (note.type === 'credits' && note.field === 'end' && note.note) parts.push(`Your credits end wasn't sent to ${r.server_name}. ${note.note}, past any scene after the credits.`);
            });
        });
        return parts.join(' ') || 'Saved.';
    }

    async function saveAdjust() {
        const dur = duration();
        const markers = adjustTypes().map(function (t) {
            const m = state.adjust.model[t];
            return { type: t, start_ms: m.start, end_ms: m.toEnd ? null : Math.min(m.end, dur) };
        });
        // Another file may be open by the time the answer arrives: only this edit's own state is touched then.
        const adj = state.adjust;
        const path = state.path;
        adj.saving = true;
        render();
        try {
            const answer = await saveMarkers(markers, path);
            toast('Saved', savedMessage(answer), 'success');
            if (state.adjust !== adj) return;
            state.adjust = null;
            await loadFile();
        } catch (e) {
            if (state.adjust !== adj) {
                toast('Adjust', `Couldn't save: ${e.message}`, 'danger');
                return;
            }
            adj.saving = false;
            adj.error = `Couldn't save: ${e.message}`;
            render();
        }
    }

    async function unlock() {
        const path = state.path;
        try {
            await sendJson('DELETE', '/api/markers/item/markers', { path: path, types: lockedTypes() });
            toast('Back to automatic', 'The next check decides these times again.', 'success');
            if (state.path !== path) return;
            state.confirmUnlock = false;
            await loadFile();
        } catch (e) {
            toast('Back to automatic', `Couldn't do it: ${e.message}`, 'danger');
        }
    }

    // ------------------------------------------------------------------ review

    function sourcePhrase(row) {
        const src = row.source;
        if (src === 'chapters') return row.label ? `From the file's “${row.label}” chapter` : 'From the file\'s chapters';
        if (src === 'credits_text') return 'From the credits read on screen';
        if (src === 'season_audio') return 'From the theme music heard across the season';
        if (src === 'season_audio_previous') return 'From the previous season\'s theme music';
        if (src === 'server_markers' || src === 'server_markers_imported') return `${originName(row)}'s own marker`;
        if (src === 'user') return 'Your earlier times';
        return `From ${(SOURCES[src] || [src])[0]}`;
    }

    function originName(row) {
        const s = servers().find(function (x) { return x.server_id === row.origin; });
        return s ? serverName(s) : (row.origin || 'A server');
    }

    function candidates(type) {
        const rows = ((state.item && state.item.evidence) || []).filter(function (r) {
            return r.type === type && r.start_ms !== null && r.start_ms !== undefined && !/earlier file/i.test(r.detail || '');
        });
        const groups = [];
        rows.sort(function (a, b) { return a.start_ms - b.start_ms; }).forEach(function (r) {
            const key = START_TYPES.indexOf(type) !== -1 ? r.start_ms : r.start_ms;
            let g = groups.find(function (x) { return Math.abs(x.start - key) <= SAME_ANSWER_MS; });
            if (!g) {
                g = { start: r.start_ms, end: r.end_ms, rows: [] };
                groups.push(g);
            }
            g.rows.push(r);
        });
        const own = groups.filter(function (g) { return g.rows.some(function (r) { return r.source !== 'server_markers' && r.source !== 'server_markers_imported'; }); });
        const proposed = decision(type).proposed;
        if (proposed && proposed.start_ms !== null && !own.some(function (g) { return Math.abs(g.start - proposed.start_ms) <= SAME_ANSWER_MS; })) {
            own.push({ start: proposed.start_ms, end: proposed.end_ms, rows: [{ source: (proposed.decided_by || [])[0] || 'user', label: '' }] });
        }
        return (own.length ? own : groups).map(function (g) {
            const phrases = [];
            const agreeing = [];
            g.rows.forEach(function (r) {
                if (r.source === 'server_markers' || r.source === 'server_markers_imported') agreeing.push(`${originName(r)}'s own marker agrees`);
                else if (phrases.indexOf(sourcePhrase(r)) === -1) phrases.push(sourcePhrase(r));
            });
            return { start: g.start, end: g.end, text: phrases.concat(agreeing).join(' · ') || agreeing.join(' · ') };
        }).sort(function (a, b) { return a.start - b.start; });
    }

    function reviewState(type) {
        if (!state.review[type]) {
            const found = candidates(type);
            const first = found.length ? found[0].start : (TO_END_TYPES.indexOf(type) !== -1 ? Math.max(0, duration() - 120000) : 0);
            state.review[type] = { selected: null, end: null, pickStart: Math.max(0, edgeRounded(first) - 5000) };
        }
        return state.review[type];
    }

    function startsWord(type, ms) {
        return type === 'credits' ? `Credits start at ${clock(ms)}` : `${TYPE_LABELS[type]} starts at ${clock(ms)}`;
    }

    function reviewPanel(type) {
        const found = candidates(type);
        const rs = reviewState(type);
        const panel = el('div', 'd-flex flex-column gap-4');
        panel.dataset.review = type;
        const box = el('div', 'insp-review-box');
        const word = TYPE_WORDS[type];
        let heading;
        if (found.length >= 2) {
            const spread = found[found.length - 1].start - found[0].start;
            heading = `Where ${type === 'credits' ? 'do the credits' : `does the ${word}`} start? ${found.length === 2 ? 'Two' : found.length} answers disagree by ${Math.round(spread / 1000)} seconds.`;
        } else if (found.length === 1) {
            heading = `Where ${type === 'credits' ? 'do the credits' : `does the ${word}`} start? Only one answer came in, and it can't decide on its own.`;
        } else {
            heading = `Where ${type === 'credits' ? 'do the credits' : `does the ${word}`} start? Nothing found an answer to check.`;
        }
        const names = receivers([type]);
        box.append(el('div', 'insp-review-title', heading),
            el('div', 'mt-1', `Pick the frame where the ${word} begin${type === 'credits' ? '' : 's'}. Your choice goes to ${names.length ? joinWith(names, 'and') : 'your servers'} and stays, even when the app checks this ${state.file && state.file.kind === 'episode' ? 'episode' : 'film'} again.`));
        panel.appendChild(box);
        if (found.length) {
            const grid = el('div', 'insp-grid-2');
            found.forEach(function (c) {
                const card = el('div', 'insp-card d-flex flex-column gap-2');
                card.dataset.candidate = String(c.start);
                card.append(el('div', 'fs-5 fw-semibold insp-mono', clock(c.start)), el('div', 'insp-small fs-6', c.text));
                card.appendChild(exactFramesAround(c.start, { ring: c.start }));
                card.appendChild(el('div', 'insp-small', 'Frames one second apart, read from the video. The ringed frame is where this answer starts.'));
                const chosen = rs.selected !== null && edgeRounded(rs.selected) === edgeRounded(c.start);
                const b = button(startsWord(type, c.start), chosen ? 'btn btn-insp-primary' : 'btn btn-outline-secondary', function () {
                    rs.selected = c.start;
                    rs.end = c.end;
                    render();
                });
                b.dataset.pick = String(c.start);
                card.appendChild(b);
                grid.appendChild(card);
            });
            panel.appendChild(grid);
        }
        const pick = el('div', 'insp-card');
        pick.dataset.pickYourself = type;
        const top = el('div', 'd-flex justify-content-between align-items-center gap-2 flex-wrap');
        top.appendChild(withInfo(el('div', 'fw-semibold', found.length ? 'Neither is right? Pick the frame yourself.' : 'Pick the frame yourself.'), TIPS.pick));
        const nav = el('div', 'd-flex gap-2');
        const back = button('◀\uFE0E 10 s', 'btn btn-sm btn-outline-secondary', function () { rs.pickStart = Math.max(0, rs.pickStart - PICK_STEP_MS); render(); });
        back.setAttribute('aria-label', 'Show 10 seconds earlier');
        const fwd = button('10 s ▶\uFE0E', 'btn btn-sm btn-outline-secondary', function () { rs.pickStart = rs.pickStart + PICK_STEP_MS; render(); });
        fwd.setAttribute('aria-label', 'Show 10 seconds later');
        nav.append(back, fwd);
        top.appendChild(nav);
        pick.appendChild(top);
        pick.appendChild(exactFramesAround(rs.pickStart, {
            before: 0,
            count: PICK_COUNT,
            ring: rs.selected === null ? undefined : rs.selected,
            onPick: function (t) { rs.selected = t; rs.end = null; render(); },
            pickLabel: `the ${word} start${type === 'credits' ? '' : 's'} here`,
        }));
        pick.appendChild(el('div', 'insp-small mt-2', `Click the first frame of the ${word}. You'll see it here before anything is saved.`));
        panel.appendChild(pick);
        if (rs.selected !== null) {
            const bar = el('div', 'insp-card insp-confirm');
            bar.dataset.confirm = type;
            const text = el('div');
            text.append(el('div', 'fw-semibold', `Selected: ${startsWord(type, rs.selected).toLowerCase()}`),
                el('div', 'insp-small', `Saving sends it to ${names.length ? joinWith(names, 'and') : 'your servers'} now and keeps it through future checks. You can go back to automatic any time.`));
            if (rs.error) text.appendChild(el('div', 'small text-danger mt-1', rs.error));
            const buttons = el('div', 'd-flex gap-2');
            const notNow = button('Not now', 'btn btn-outline-secondary', function () { rs.selected = null; rs.error = ''; render(); });
            const save = button(rs.saving ? 'Sending…' : `Save and send to ${names.length ? joinWith(names, 'and') : 'your servers'}`, 'btn btn-insp-primary', function () { saveReview(type); });
            save.disabled = !!rs.saving;
            save.dataset.saveReview = type;
            buttons.append(notNow, save);
            bar.append(text, buttons);
            panel.appendChild(bar);
        }
        return panel;
    }

    async function saveReview(type) {
        const rs = state.review[type];
        const dur = duration();
        let end = null;
        if (START_TYPES.indexOf(type) !== -1) {
            // An intro keeps the chosen answer's length; one picked by hand starts at 30 s long for Adjust to refine.
            const length = rs.end && rs.selected !== null && rs.end > rs.selected ? rs.end - rs.selected : 30000;
            end = Math.min(dur, rs.selected + length);
        } else if (rs.end && rs.end < dur - END_OF_FILE_MS && rs.end > rs.selected) {
            end = rs.end;
        }
        rs.saving = true;
        rs.error = '';
        const path = state.path;
        render();
        try {
            const answer = await saveMarkers([{ type: type, start_ms: edgeRounded(rs.selected), end_ms: end }], path);
            toast('Saved', savedMessage(answer), 'success');
            if (state.review[type] !== rs) return;
            delete state.review[type];
            await loadFile();
        } catch (e) {
            if (state.review[type] !== rs) {
                toast('Needs your check', `Couldn't save: ${e.message}`, 'danger');
                return;
            }
            rs.saving = false;
            rs.error = `Couldn't save: ${e.message}`;
            render();
        }
    }

    // ---------------------------------------------------------------- evidence

    function evidenceFound(row, dur) {
        if (row.type === null || row.type === undefined || row.start_ms === null || row.start_ms === undefined) return 'No entry';
        const label = TYPE_LABELS[row.type] || row.type;
        if (row.source === 'chapters' && row.label) return `“${row.label}” chapter at ${clock(row.start_ms)}`;
        if (TO_END_TYPES.indexOf(row.type) !== -1) return `${label} at ${clock(row.start_ms)}`;
        return `${label} ${clock(row.start_ms)}–${clock(segmentEnd(row, dur))}`;
    }

    function evidenceNote(row, dur) {
        if (row.type === null || row.type === undefined) {
            const asked = row.fetched_at ? `Asked ${when(row.fetched_at)}` : '';
            return { icon: 'none', text: [row.detail, asked].filter(Boolean).join(' · ') };
        }
        if (/earlier file/i.test(row.detail || '')) return { icon: 'warn', text: 'Made for an earlier version of this file, so not used' };
        const d = decision(row.type);
        const m = decided(row.type);
        const extra = [];
        if (row.source === 'season_audio' || row.source === 'season_audio_previous') {
            const match = /^(\d+)\s*\/\s*(\d+)$/.exec(row.label || '');
            if (match) extra.push(`Same theme in ${match[1]} of ${match[2]} episodes`);
        }
        if (row.detail) extra.push(row.detail);
        if (m && (m.decided_by || []).indexOf(row.source) !== -1) {
            return { icon: 'used', text: extra.concat([`Used for the ${TYPE_WORDS[row.type]}`]).join(' · ') };
        }
        if (d.status === 'needs_review') return { icon: 'warn', text: extra.concat(['One of the answers that needs your check']).join(' · ') };
        if (m) {
            const gap = Math.abs(row.start_ms - m.start_ms);
            const agrees = gap <= (START_TYPES.indexOf(row.type) !== -1 ? 5000 : 10000);
            // "the season audio", "the chapters", but "your times" for the user's own.
            const who = (m.decided_by || []).map(function (s) {
                return s === 'user' ? 'your times' : `the ${(SOURCES[s] || [s])[0].toLowerCase()}`;
            });
            return {
                icon: agrees ? 'used' : 'warn',
                text: extra.concat([agrees
                    ? `Agrees with ${joinWith(who, 'and') || 'the decision'}, so ${who.length === 1 ? 'that answer is' : 'those answers are'} kept`
                    : `${Math.round(gap / 1000)} s from the decision, so not used`]).join(' · '),
            };
        }
        return { icon: 'none', text: extra.concat(['Not used']).join(' · ') };
    }

    function evidenceCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspEvidence';
        card.appendChild(el('div', 'insp-card-title mb-2', 'How it was decided'));
        const rows = (state.item && state.item.evidence) || [];
        if (!rows.length) {
            card.appendChild(el('div', 'insp-small fs-6', isChecked() ? 'No source answered for this file.' : 'Nothing has been asked yet. Check intro & credits asks every source.'));
            return card;
        }
        const dur = duration();
        const sorted = rows.slice().sort(function (a, b) {
            const ia = SOURCE_ORDER.indexOf(a.source);
            const ib = SOURCE_ORDER.indexOf(b.source);
            return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || (a.start_ms || 0) - (b.start_ms || 0);
        });
        sorted.forEach(function (row) {
            const info = SOURCES[row.source] || [row.source, ''];
            const note = evidenceNote(row, dur);
            const line = el('div', 'insp-ev');
            line.dataset.source = row.source;
            const icon = el('div', 'insp-ev-icon');
            icon.appendChild(el('i', 'bi ' + (note.icon === 'used' ? 'bi-check-lg' : note.icon === 'warn' ? 'bi-exclamation-triangle' : 'bi-dash-lg')));
            const who = el('div');
            let name = info[0];
            let explains = info[1];
            if (row.source === 'server_markers') {
                name = `${originName(row)}'s own`;
                explains = `Markers ${originName(row)} made itself`;
            } else if (row.source === 'server_markers_imported') {
                name = `${originName(row)}'s imported`;
            }
            who.append(el('div', 'fw-medium', name), el('div', 'insp-small', explains));
            const found = el('div');
            found.append(el('div', '', evidenceFound(row, dur)), el('div', 'insp-small', note.text));
            line.append(icon, who, found);
            card.appendChild(line);
        });
        return card;
    }

    // ----------------------------------------------------------------- servers

    const PLAN_WORDS = {
        up_to_date: 'Intro & credits up to date',
        will_add: 'Intro & credits: adds them on the next job',
        will_replace: 'Intro & credits: the next job replaces what it shows',
        will_remove: 'Intro & credits: the next job removes the ones this app sent',
        waiting: 'Intro & credits: waiting for its other versions to agree',
        keeps_plex: 'Intro & credits: keeps Plex\'s own',
        keeps_emby: 'Intro & credits: keeps Emby\'s own',
        not_enabled: 'Intro & Credits is off here',
        nothing_to_publish: 'Intro & credits: nothing to send yet',
        unknown: 'Intro & credits: couldn\'t read what it shows',
    };

    function previewLine(p) {
        if (!p) return 'Preview: not looked up';
        if (p.error) return `Preview: ${p.error}`;
        if (!p.path) return `No preview yet${p.note ? ' · ' + p.note : ''}`;
        if (!p.exists) return 'No preview yet';
        if (p.kind === 'trickplay') return `Preview in place · trickplay tiles${p.frame_count ? ' · ' + Number(p.frame_count).toLocaleString() + ' frames' : ''}`;
        return `Preview in place${p.frame_count ? ' · ' + Number(p.frame_count).toLocaleString() + ' frames' : ''}`;
    }

    function serversCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspServers';
        card.appendChild(el('div', 'insp-card-title mb-2', 'On your servers'));
        const previews = (state.file && state.file.previews) || [];
        const ids = [];
        servers().forEach(function (s) { ids.push(s.server_id); });
        previews.forEach(function (p) { if (ids.indexOf(p.server_id) === -1) ids.push(p.server_id); });
        if (!ids.length) {
            card.appendChild(el('div', 'insp-small fs-6', 'No server has this file in a library.'));
            return card;
        }
        ids.forEach(function (id) {
            const s = servers().find(function (x) { return x.server_id === id; });
            const p = previews.find(function (x) { return x.server_id === id; });
            const type = String((s && s.server_type) || (p && p.server_type) || '').toLowerCase();
            const row = el('div', 'insp-server');
            row.dataset.serverId = id;
            row.append(el('div', 'insp-tile insp-tile-' + type, VENDOR_TILES[type] || '?'), el('div', 'fw-semibold text-break', (s && serverName(s)) || (p && p.server_name) || id));
            const lines = el('div', 'd-flex flex-column gap-1');
            if (s) {
                let words = PLAN_WORDS[s.plan] || PLAN_WORDS.unknown;
                if (s.plan === 'up_to_date' && s.publish_status === 'written') words += ' · sent by this app';
                lines.appendChild(el('div', '', words));
                if (s.plan_reason) lines.appendChild(el('div', 'insp-small', s.plan_reason));
                if (s.error) lines.appendChild(el('div', 'small text-warning-emphasis', s.error));
                if (['failed', 'waiting', 'skipped'].indexOf(s.publish_status) !== -1 && s.publish_message) {
                    lines.appendChild(el('div', 'insp-small', s.publish_message));
                }
                if (s.server_type === 'plex' && s.version_count > 1) lines.appendChild(el('div', 'insp-small', 'All versions of this item share one set of markers'));
            }
            lines.appendChild(el('div', p && p.error ? 'text-warning-emphasis' : '', previewLine(p)));
            if (p && p.path) {
                const det = el('details', 'insp-locations');
                det.appendChild(el('summary', '', 'File locations'));
                const what = p.kind === 'trickplay' ? 'Trickplay folder' : (type === 'emby' ? 'Preview (next to the video)' : 'Preview');
                det.appendChild(el('div', '', `${what}: ${p.path}`));
                lines.appendChild(det);
            }
            row.appendChild(lines);
            card.appendChild(row);
        });
        return card;
    }

    // -------------------------------------------------------------- all frames

    function allFrames() {
        const p = preview();
        const wrap = el('div');
        wrap.id = 'inspAllFrames';
        const step = interval();
        const count = p.frame_count;
        const dur = duration();
        const view = el('div', 'insp-allframes-view');
        const big = el('img');
        big.id = 'inspAllFramesBig';
        const caption = el('div', 'd-flex align-items-center gap-2');
        const prev = button('', 'btn btn-sm btn-outline-secondary', function () { selectFrame(state.allIndex - 1); });
        prev.appendChild(el('i', 'bi bi-chevron-left'));
        prev.setAttribute('aria-label', 'Previous frame');
        const next = button('', 'btn btn-sm btn-outline-secondary', function () { selectFrame(state.allIndex + 1); });
        next.appendChild(el('i', 'bi bi-chevron-right'));
        next.setAttribute('aria-label', 'Next frame');
        const label = el('span', 'insp-mono');
        label.id = 'inspAllFramesLabel';
        caption.append(prev, label, next);
        view.append(big, caption);
        wrap.appendChild(view);

        const controls = el('div', 'd-flex align-items-center gap-2 mb-2 flex-wrap');
        const select = el('select', 'form-select form-select-sm w-auto');
        select.id = 'inspAllStep';
        select.setAttribute('aria-label', 'Show every frame, or every few');
        [1, 2, 5, 10, 25].forEach(function (n) {
            const opt = el('option', '', n === 1 ? 'Every frame' : `Every ${n}th`);
            opt.value = String(n);
            if (n === state.allStep) opt.selected = true;
            select.appendChild(opt);
        });
        select.addEventListener('change', function () { state.allStep = Number(select.value) || 1; render(); });
        controls.append(select, el('span', 'insp-small', `${Number(count).toLocaleString()} frames${step ? `, one every ${seconds(step)}` : ''}. Intro frames are edged blue, credits orange.`));
        wrap.appendChild(controls);

        const grid = el('div', 'insp-allframes');
        const segs = decidedTypes().map(function (t) { return { type: t, m: decided(t) }; });
        for (let i = 0; i < count; i += state.allStep) {
            const t = i * step;
            const b = el('button');
            b.type = 'button';
            b.dataset.index = String(i);
            const inSeg = segs.find(function (s) { return t >= s.m.start_ms && t < segmentEnd(s.m, dur); });
            if (inSeg) b.classList.add(START_TYPES.indexOf(inSeg.type) !== -1 ? 'is-intro' : 'is-credits');
            const img = el('img');
            img.loading = 'lazy';
            img.alt = `Frame ${i}`;
            img.src = frameUrl(i);
            b.append(img, el('span', '', step ? clock(t) : `#${i}`));
            b.addEventListener('click', function () { selectFrame(i); });
            grid.appendChild(b);
        }
        wrap.appendChild(grid);
        requestAnimationFrame(function () { selectFrame(state.allIndex); });
        return wrap;
    }

    function selectFrame(index) {
        const p = preview();
        if (!p) return;
        const i = Math.max(0, Math.min(index, p.frame_count - 1));
        state.allIndex = i;
        const big = $('inspAllFramesBig');
        if (!big) return;
        big.src = frameUrl(i);
        big.alt = `Frame ${i}`;
        const step = interval();
        $('inspAllFramesLabel').textContent = `Frame ${i.toLocaleString()} of ${(p.frame_count - 1).toLocaleString()}${step ? ' · ' + clock(i * step) : ''}`;
        document.querySelectorAll('#inspAllFrames .insp-allframes button.is-active').forEach(function (b) { b.classList.remove('is-active'); });
        const active = document.querySelector(`#inspAllFrames .insp-allframes button[data-index="${i}"]`);
        if (active) active.classList.add('is-active');
    }

    // ---------------------------------------------------------------- actions

    // Both answer after an await: the job banner is only set when the file it was asked for is still the one open.
    async function regenerate() {
        const path = state.path;
        state.busy = 'regenerate';
        render();
        try {
            const job = await sendJson('POST', '/api/jobs/manual', { file_paths: [path], force_regenerate: true, priority: 1 });
            if (state.path === path) {
                state.job = { id: job.id, kind: job.kind || 'previews', status: job.status || 'pending', name: job.library_name || 'Regenerate preview', percent: 0 };
            }
            toast('Regenerate preview', 'Queued. The preview is rebuilt as a job on the Dashboard.', 'success');
        } catch (e) {
            toast('Regenerate preview', `Couldn't queue it: ${e.message}`, 'danger');
        }
        state.busy = '';
        render();
    }

    async function redetect() {
        const path = state.path;
        state.busy = 'redetect';
        render();
        try {
            const data = await sendJson('POST', '/api/markers/item/redetect', { path: path });
            if (state.path === path) {
                state.job = { id: data.job_id, kind: MARKERS_JOB, status: 'pending', name: 'Intro & Credits', percent: 0 };
            }
            toast('Intro & credits', 'Queued. This page updates when the job finishes.', 'success');
        } catch (e) {
            toast('Intro & credits', `Couldn't queue it: ${e.message}`, 'danger');
        }
        state.busy = '';
        render();
    }

    // -------------------------------------------------------------------- jobs

    function jobTouches(job) {
        if (!job || !state.path) return false;
        const cfg = job.config || {};
        if ((cfg.file_paths || []).indexOf(state.path) !== -1 || (cfg.webhook_paths || []).indexOf(state.path) !== -1) return true;
        const progress = job.progress || {};
        if (progress.current_file === state.path) return true;
        return (progress.workers || []).some(function (w) { return w && w.current_file === state.path; });
    }

    function onJobEvent(job, ended) {
        if (!job || !state.path) return;
        const tracked = state.job && state.job.id === job.id;
        if (!tracked && !jobTouches(job)) return;
        if (ended) {
            const kind = job.kind || (state.job && state.job.kind);
            state.job = null;
            renderJobBanner();
            clearTimeout(reloadTimer);
            // A finished Intro & Credits job changed what was decided; a preview job the frames. Either way: read again.
            reloadTimer = setTimeout(function () {
                if (state.adjust) return;
                loadFile();
                if (state.scope === 'season') loadSeason();
            }, kind === MARKERS_JOB ? 500 : 800);
            return;
        }
        state.job = {
            id: job.id,
            kind: job.kind || (state.job && state.job.kind),
            status: job.status || (state.job && state.job.status) || 'running',
            name: job.library_name || (state.job && state.job.name) || '',
            percent: job.progress ? job.progress.percent : (state.job && state.job.percent) || 0,
        };
        renderJobBanner();
    }

    function onJobProgress(data) {
        if (!data || !state.path) return;
        const tracked = state.job && state.job.id === data.job_id;
        const progress = data.progress || {};
        const touches = progress.current_file === state.path || (progress.workers || []).some(function (w) { return w && w.current_file === state.path; });
        if (!tracked && !touches) return;
        state.job = Object.assign({}, state.job || { id: data.job_id, kind: '', name: '' }, { status: 'running', percent: progress.percent || 0 });
        renderJobBanner();
    }

    function watchJobs() {
        if (jobsSocket || typeof window.io !== 'function') return;
        // Polling only, as app.js connects: a websocket pins a gunicorn thread for every open tab.
        jobsSocket = window.io('/jobs', { transports: ['polling'], reconnection: true });
        ['job_created', 'job_started', 'job_updated'].forEach(function (name) {
            jobsSocket.on(name, function (job) { onJobEvent(job, false); });
        });
        ['job_completed', 'job_failed', 'job_cancelled'].forEach(function (name) {
            jobsSocket.on(name, function (job) { onJobEvent(job, true); });
        });
        jobsSocket.on('job_progress', onJobProgress);
    }

    // -------------------------------------------------------------------- init

    function openFromUrl(replace) {
        const params = new URLSearchParams(window.location.search);
        const path = (params.get('path') || params.get('file') || '').trim();
        const bif = (params.get('bif') || '').trim();
        if (path.startsWith('/')) {
            openFile(path, { replace: replace, fromHistory: !replace, scope: params.get('view') === 'season' ? 'season' : 'episode' });
            if (replace && !params.get('path')) setUrl(new URLSearchParams({ path: path }), true);
            return true;
        }
        if (bif) {
            openBif(bif, { replace: replace, fromHistory: !replace });
            return true;
        }
        const q = (params.get('q') || '').trim();
        if (q) {
            $('inspQuery').value = q;
            runSearch();
        }
        return false;
    }

    function init() {
        $('inspQuery').addEventListener('input', queryChanged);
        $('inspQuery').addEventListener('keydown', function (e) {
            if (e.key !== 'Enter') return;
            const q = $('inspQuery').value.trim();
            if (q.startsWith('/')) {
                e.preventDefault();
                openFile(q, {});
            } else {
                clearTimeout(searchTimer);
                runSearch();
            }
        });
        $('inspScope').addEventListener('change', function () { if (state.query.length >= 2) runSearch(); });
        $('inspShowResults').addEventListener('click', function () { showSearch(true); });
        window.addEventListener('popstate', function () {
            if (!openFromUrl(false)) showSearch(false);
        });
        window.addEventListener('resize', function () {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(layoutAllLabels, 150);
        });
        document.addEventListener('keydown', function (e) {
            if (state.view !== 'all' || !$('inspAllFrames')) return;
            if (e.target && ['INPUT', 'SELECT', 'TEXTAREA'].indexOf(e.target.tagName) !== -1) return;
            if (e.key === 'ArrowLeft') { e.preventDefault(); selectFrame(state.allIndex - 1); }
            if (e.key === 'ArrowRight') { e.preventDefault(); selectFrame(state.allIndex + 1); }
        });
        tooltips(document.getElementById('inspSearch'));
        loadServers();
        openFromUrl(true);
    }

    window.inspector = { openFile: openFile, state: state };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
