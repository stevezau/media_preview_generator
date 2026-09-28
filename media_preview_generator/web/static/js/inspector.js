// =========================================================================
// Inspector: one page for a file's preview frames and its intro & credits (the approved design, boards 1-6).
//
// Search: GET /api/media/search (every enabled server, merged), then POST /api/inspector/status for each row's
// preview and Intro & Credits state, and POST /api/inspector/show for a show's seasons and episodes. A path starting
// with "/" opens that file. Choosing a file folds the results away and sets ?path= so the page can be linked.
//
// A file: GET /api/inspector/file (where each server keeps its preview, what it holds, a job working on the file,
// other versions) and GET /api/markers/item (what was decided, the evidence, what each server shows). The Timeline is
// one strip of every preview frame (GET /api/bif/frame, /api/bif/trickplay/frame), drawn only near the viewport, with
// a row per server underneath on the same scale. Adjust and "Needs your check" read exact frames one second apart
// from the video (GET /api/inspector/frames). Saving is POST /api/markers/item/markers (save = lock = publish to every
// owner); "Back to automatic" is DELETE on the same route. Regenerate preview is POST /api/jobs/manual, Re-detect
// POST /api/markers/item/redetect. A job on the /jobs socket that works on the open file shows as a live banner, and
// the file is read again when it ends.
//
// Every piece of text goes in through textContent. Depends on app.js globals: showToast, getCsrfToken,
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
    const ADJUST_COUNT = 8;
    const ADJUST_BEFORE = 3;
    const MARKERS_JOB = 'intro_credits';
    const ADD_HEAD_MS = 30000;
    const ADD_TAIL_MS = 60000;
    const ATTENTION_PLANS = ['unknown', 'will_add', 'will_replace', 'will_remove', 'waiting'];

    // Timeline geometry, in pixels: a tile every PITCH, lanes under the frames on the same scale.
    const PITCH = 152;
    const TILE_W = 144;
    const LANE_TOP = 118;
    const LANE_PITCH = 24;
    const LANE_H = 18;
    const STRIP_BARE_H = 106;
    const STEP_FRAMES = 10;
    // A file with no preview still gets a strip (its tiles say "No preview"), one place every this long.
    const NO_PREVIEW_STEP_MS = 10000;
    // Tiles are kept this many viewport widths past each side; images load once the strip stops for this long.
    const WINDOW_EXTRA = 0.75;
    const IMAGE_SETTLE_MS = 120;
    const GLIDE_SCREENS = 3;
    // Two edges closer than this are one line on the strip: a server's own marker that matches ours isn't drawn twice.
    const SAME_EDGE_MS = 2000;

    const TIPS = {
        regenerate: 'Makes this file\'s preview again for every server that has it, replacing the one there now. Runs as a job on the Dashboard.',
        redetect: 'Looks this file up again and asks every source afresh. Runs as a job on the Dashboard.',
        adjust: 'Move the intro and credits one second at a time, on frames read straight from the video. Saving sends your times to your servers and keeps them through later checks.',
        pick: 'Frames one second apart, read from the video. Step ten seconds either way to find the first frame.',
        lock: 'Keep these times exactly as they are. Later checks won\'t change them, and your servers get them now.',
        unlockHeader: 'Let later checks set these times again. What your servers show now stays until the next Intro & Credits job.',
        scope: 'Intros usually sit at the same spot in every episode of a season, so seeing them side by side makes an odd one stand out. Click an episode to open it.',
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
    const exactDone = new Map();
    let timeline = null;
    let big = null;
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

    function icon(name) {
        const i = el('i', 'bi bi-' + name);
        i.setAttribute('aria-hidden', 'true');
        return i;
    }

    function button(text, className, onClick, tip) {
        const b = el('button', className || 'btn insp-btn', text);
        b.type = 'button';
        if (onClick) b.addEventListener('click', onClick);
        if (tip) b.dataset.tip = tip;
        return b;
    }

    function iconButton(iconName, text, className, onClick) {
        const b = button('', className, onClick);
        b.append(icon(iconName), document.createTextNode(text));
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

    function dot(kind) {
        const d = el('span', 'insp-dot is-' + kind);
        d.setAttribute('aria-hidden', 'true');
        return d;
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

    function day(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
    }

    function when(iso) {
        const date = day(iso);
        if (!date) return '';
        const time = new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
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
        return toEnd(seg, duration) ? `${clock(seg.start_ms)} → end` : `${clock(seg.start_ms)} – ${clock(segmentEnd(seg, duration))}`;
    }

    function pct(ms, duration) {
        return Math.max(0, Math.min(100, (ms / duration) * 100));
    }

    function clamp(n, lo, hi) {
        return Math.max(lo, Math.min(hi, n));
    }

    // Intro and recap are drawn blue, credits and preview amber.
    function tone(type) {
        return START_TYPES.indexOf(type) !== -1 ? 'intro' : 'credits';
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
            let previewCell;
            let markers;
            if (r.kind === 'show') {
                previewCell = ['', ''];
                markers = [state.openShow === index ? 'Pick an episode below' : 'Pick an episode', 'insp-state-muted'];
            } else {
                previewCell = previewText(status);
                markers = markersText(status);
            }
            row.append(
                titleCell,
                el('div', 'insp-cell-preview ' + previewCell[1], previewCell[0]),
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
            const b = button(season.label, 'btn insp-btn' + (active ? ' active' : ''), function () {
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
            ? `Results for “${state.query}”` : 'New search';
    }

    function showSearch(push) {
        closeBig();
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
        closeBig();
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
        state.job = null;
        timeline = null;
        exactCache.clear();
        exactDone.clear();
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

    function stripStep() {
        return interval() || NO_PREVIEW_STEP_MS;
    }

    // A preview whose file length isn't known has no times (e.g. Jellyfin trickplay with no stated interval on a file
    // no server gave a length for): its frames are still shown, by number.
    function lengthUnknown() {
        const p = preview();
        return !duration() && !!(p && p.frame_count);
    }

    function frameLabel(index) {
        return lengthUnknown() ? `Frame ${(index + 1).toLocaleString()}` : clock(index * stripStep());
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

    function fileWord() {
        return state.file && state.file.kind === 'episode' ? 'episode' : 'film';
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

    function sortedCurrent(server) {
        return (Array.isArray(server.current) ? server.current : []).slice().sort(function (a, b) { return a.start_ms - b.start_ms; });
    }

    function serverMarkers() {
        const out = [];
        servers().forEach(function (s) {
            (Array.isArray(s.current) ? s.current : []).forEach(function (m) { out.push({ server: s, marker: m }); });
        });
        return out.sort(function (a, b) { return a.marker.start_ms - b.marker.start_ms; });
    }

    // Every marker a server made itself (not one this app sent), each with the key its band, line and chip share.
    function ownMarkers() {
        const out = [];
        servers().forEach(function (s) {
            sortedCurrent(s).forEach(function (m, i) {
                if (!m.ours) out.push({ server: s, marker: m, key: `own-${s.server_id}-${i}` });
            });
        });
        return out.sort(function (a, b) { return a.marker.start_ms - b.marker.start_ms; });
    }

    function attention() {
        if (!isChecked()) return [];
        return servers().filter(function (s) { return s.error || ATTENTION_PLANS.indexOf(s.plan) !== -1; });
    }

    function serverDot(s) {
        if (s.error || !Array.isArray(s.current) || s.plan === 'unknown') return 'bad';
        if (!isChecked()) return (s.current || []).some(function (m) { return !m.ours; }) ? 'own' : 'none';
        if (s.plan === 'up_to_date') return 'ok';
        if (s.plan && s.plan.indexOf('keeps_') === 0) return 'own';
        if (ATTENTION_PLANS.indexOf(s.plan) !== -1) return 'wait';
        return 'none';
    }

    // What a server that shows nothing gives viewers, in the words its row on the strip and its row on the card share.
    function emptyText(s) {
        if (!s.markers_enabled) return 'Intro & Credits is off';
        if (!isChecked()) return `Nothing here yet · check this ${fileWord()} first`;
        const dur = duration();
        const wanted = decidedTypes().filter(function (t) { return (s.can_show || []).indexOf(t) !== -1; });
        if (wanted.length && s.plan === 'will_add') {
            return `Nothing yet · the next job adds ${joinWith(wanted.map(function (t) { return `${TYPE_WORDS[t]} ${rangeText(decided(t), dur)}`; }), 'and')}`;
        }
        if (s.plan === 'waiting') return 'Nothing yet · waiting for its other versions to agree';
        return 'Nothing yet';
    }

    function splitTitle(title) {
        const match = /^(.*?)\s*\((\d{4})\)\s*(?:·\s*(.+))?$/.exec(title || '');
        if (!match || !match[1]) return { main: title || '', sub: '' };
        return { main: match[1], sub: [match[2], match[3]].filter(Boolean).join(' · ') };
    }

    // ------------------------------------------------------------------ render

    function render() {
        const root = $('inspFile');
        if (timeline) timeline.keepScroll();
        untooltip(root);
        const parts = [];
        if (state.bifOnly) {
            parts.push(header(state.file ? state.file.title : state.bifOnly.split('/').pop(), state.bifOnly, [], state.file && !state.file.error ? ['Preview file'] : []));
        } else {
            const f = state.file || {};
            const title = state.titleHint || f.title || state.path.split('/').pop();
            const ready = state.file && !state.file.error && f.exists !== false && f.in_library;
            if (ready) {
                const banner = el('div');
                banner.id = 'inspJobBanner';
                parts.push(banner);
                renderJobBanner(banner);
            }
            parts.push(header(title, state.path, ready ? actions() : [], ready ? headChips() : []));
        }
        if (!state.file) {
            parts.push(loadingCard());
        } else if (state.file.error) {
            parts.push(messageCard('Couldn\'t open this file', state.file.error, 'bad'));
        } else if (state.bifOnly) {
            parts.push(statTiles([previewStat()]));
            parts.push(timelineCard());
        } else if (state.file.exists === false) {
            parts.push(goneCard());
        } else if (!state.file.in_library || state.file.exists === null) {
            parts.push(messageCard('Not in any library',
                'No server has a library that holds this path, so the Inspector has nothing to compare and nothing here '
                + 'is sent anywhere. Check the path, or the libraries on the Servers page.', 'muted'));
        } else {
            if (state.confirmLock && lockableTypes().length) parts.push(lockConfirmRow());
            if (state.confirmUnlock && lockedTypes().length) parts.push(unlockConfirmRow());
            if (state.file.versions && state.file.versions.length > 1) parts.push(versionsBar());
            if (state.file.kind === 'episode') parts.push(scopeToggle());
            if (state.scope === 'season') {
                parts.push(seasonCard());
            } else {
                reviewTypes().forEach(function (type) { parts.push(reviewPanel(type)); });
                parts.push(statTiles([serversStat(), foundStat(), previewStat(), checkedStat()]));
                parts.push(timelineCard());
                const lower = el('div', 'insp-grid-2');
                let left;
                if (!state.item) left = itemErrorCard();
                else if (!isChecked()) left = notCheckedCard();
                else left = evidenceCard();
                lower.append(left, serversCard());
                parts.push(lower);
            }
        }
        root.replaceChildren.apply(root, parts);
        if (timeline && timeline.node.isConnected) timeline.mount();
        tidyAxes(root);
        tooltips(root);
    }

    function header(title, path, buttons, chips) {
        const head = el('div', 'insp-head');
        const text = el('div', 'insp-head-text');
        text.appendChild(el('div', 'insp-crumb', 'Tools › Inspector'));
        const parts = splitTitle(title);
        const titleRow = el('div', 'insp-titlerow');
        const h1 = el('h1', 'insp-title', parts.main);
        h1.id = 'inspTitle';
        titleRow.appendChild(h1);
        if (parts.sub) {
            const sub = el('div', 'insp-title-sub', parts.sub);
            sub.id = 'inspTitleSub';
            titleRow.appendChild(sub);
        }
        text.appendChild(titleRow);
        if (chips.length) {
            const row = el('div', 'insp-head-chips');
            row.id = 'inspChips';
            chips.forEach(function (c) { row.appendChild(typeof c === 'string' ? el('span', 'insp-chip', c) : c); });
            text.appendChild(row);
        }
        if (path) {
            const pathRow = el('div', 'insp-pathrow');
            const p = el('div', 'insp-path', path);
            p.id = 'inspPath';
            p.title = path;
            const copy = button('', 'insp-icon-btn', function () { copyPath(path); });
            copy.setAttribute('aria-label', 'Copy file path');
            copy.title = 'Copy file path';
            copy.appendChild(icon('copy'));
            pathRow.append(p, copy);
            text.appendChild(pathRow);
        }
        head.appendChild(text);
        if (buttons.length) {
            const bar = el('div', 'insp-actions');
            buttons.forEach(function (b) { bar.appendChild(b); });
            head.appendChild(bar);
        }
        return head;
    }

    function headChips() {
        const chips = [state.file.kind === 'episode' ? 'Episode' : 'Film'];
        const dur = duration();
        if (dur) {
            const c = el('span', 'insp-chip insp-mono', clock(dur));
            c.title = 'Length';
            chips.push(c);
        }
        if (state.file.quality) chips.push(state.file.quality);
        if (lockedTypes().length) {
            const lock = el('span', 'insp-chip is-locked');
            lock.id = 'inspLockedChip';
            lock.append(icon('lock-fill'), document.createTextNode('Locked by you'));
            chips.push(lock);
        }
        return chips;
    }

    async function copyPath(path) {
        let copied = false;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(path);
                copied = true;
            }
        } catch (e) {
            copied = false;
        }
        if (!copied) {
            // Plain http on a LAN has no clipboard API: a selected text area and the copy command still work there.
            const area = el('textarea');
            area.value = path;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            try { copied = document.execCommand('copy'); } catch (e) { copied = false; }
            area.remove();
        }
        toast('File path', copied ? 'Copied to the clipboard.' : 'Couldn\'t copy it: select the path and copy it by hand.', copied ? 'success' : 'warning');
    }

    function actionButton(iconName, text, className, handler, tip, id) {
        const wrap = el('span', 'd-inline-flex align-items-center');
        const b = iconName ? iconButton(iconName, text, className, handler) : button(text, className, handler);
        b.id = id;
        if (state.busy) b.disabled = true;
        wrap.append(b, infoIcon(tip));
        return wrap;
    }

    function actions() {
        const list = [];
        list.push(actionButton('arrow-clockwise', 'Regenerate preview', 'btn insp-btn', regenerate, TIPS.regenerate, 'inspRegenerate'));
        const analysed = !!(state.item && state.item.known && state.item.duration_ms);
        if (!isChecked()) {
            list.push(actionButton('', 'Check intro & credits now', 'btn insp-btn-primary', redetect, TIPS.redetect, 'inspRedetect'));
            return list;
        }
        list.push(actionButton('search', 'Re-detect intro & credits', 'btn insp-btn', redetect, TIPS.redetect, 'inspRedetect'));
        if (state.scope === 'season') return list;
        if (analysed && lockedTypes().length) {
            const b = actionButton('unlock', 'Back to automatic', 'btn insp-btn', askUnlock, TIPS.unlockHeader, 'inspUnlock');
            if (state.adjust || state.locking) b.querySelector('button').disabled = true;
            list.push(b);
        } else if (analysed && lockableTypes().length) {
            const b = actionButton('lock', state.locking ? 'Locking…' : 'Lock', 'btn insp-btn', askLock, TIPS.lock, 'inspLock');
            if (state.adjust || state.locking) b.querySelector('button').disabled = true;
            list.push(b);
        }
        if (analysed && !reviewTypes().length) {
            const label = state.adjust ? 'Adjusting…' : 'Adjust';
            const b = actionButton('', label, 'btn insp-btn-primary', startAdjust, TIPS.adjust, 'inspAdjust');
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
        const row = $('inspUnlockConfirmRow');
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

    function confirmBar(id, title, text, buttons) {
        const row = el('div', 'insp-confirm-bar');
        row.id = id;
        const words = el('div');
        words.append(el('div', 'insp-confirm-title', title), el('div', 'insp-small', text));
        const acts = el('div', 'insp-confirm-actions');
        buttons.forEach(function (b) { acts.appendChild(b); });
        row.append(words, acts);
        return row;
    }

    function lockConfirmRow() {
        const types = lockableTypes();
        const dur = duration();
        const names = receivers(types);
        const listed = types.map(function (t) { return `${TYPE_LABELS[t]} ${rangeText(decided(t), dur)}`; }).join(' · ');
        const to = names.length ? joinWith(names, 'and') : 'your servers';
        const yes = button(`Lock and send to ${to}`, 'btn insp-btn-primary', lockNow);
        yes.id = 'inspLockConfirm';
        const no = button('Leave them as they are', 'btn insp-btn', function () { state.confirmLock = false; render(); });
        return confirmBar('inspLockConfirmRow', `Lock these times? ${listed}`,
            `Later checks won't change them, and they go to ${to} now, the same way Save sends them.`, [no, yes]);
    }

    function unlockConfirmRow() {
        const yes = button('Go back to automatic', 'btn insp-btn-danger', unlock);
        yes.id = 'inspUnlockConfirm';
        const no = button('Keep them locked', 'btn insp-btn', function () { state.confirmUnlock = false; render(); });
        return confirmBar('inspUnlockConfirmRow', 'Back to automatic?',
            'Your times stay on your servers for now; the next check decides again and may move them.', [no, yes]);
    }

    function loadingCard() {
        const card = el('div', 'insp-card insp-loading');
        card.id = 'inspLoading';
        card.append(el('span', 'spinner-border spinner-border-sm'), el('span', '', 'Reading this file…'));
        return card;
    }

    function messageCard(title, text, tone) {
        const card = el('div', `insp-card insp-message is-${tone || 'muted'}`);
        card.dataset.state = title;
        card.append(el('div', 'insp-message-title', title), el('div', 'insp-message-text', text));
        return card;
    }

    function goneCard() {
        const known = state.file && state.file.known;
        const card = messageCard('Gone from disk',
            'This file isn\'t on disk any more: it was probably replaced or deleted. '
            + (known ? 'Intro & Credits still has what it found for it at this path. ' : '')
            + 'Search for the title to find the file it has now.', 'warn');
        card.id = 'inspGone';
        const back = button('Search for it', 'btn insp-btn mt-3', function () {
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
        bar.appendChild(el('span', 'insp-versions-text', `This ${fileWord()} has ${state.file.versions.length} files`));
        const seg = el('div', 'insp-seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Files of this item');
        state.file.versions.forEach(function (v) {
            const b = button(v.current ? `${v.label} · this one` : v.label, 'insp-seg-btn' + (v.current ? ' active' : ''), function () {
                if (!v.current) openFile(v.path, {});
            });
            b.title = v.path;
            b.setAttribute('aria-pressed', v.current ? 'true' : 'false');
            seg.appendChild(b);
        });
        bar.appendChild(withInfo(seg, TIPS.versions));
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
        const box = el('div', 'insp-banner');
        box.setAttribute('role', 'status');
        const running = job.status === 'running';
        box.appendChild(running ? el('span', 'spinner-border spinner-border-sm insp-banner-icon') : icon('hourglass-split'));
        const what = job.kind === MARKERS_JOB ? 'Intro & Credits' : 'Preview';
        const pctText = running && job.percent ? ` · ${Math.round(job.percent)}%` : '';
        box.appendChild(el('span', 'insp-banner-text', `${running ? 'Working on this file' : 'Queued for this file'}: ${what} job “${job.name || job.id}”${pctText}`));
        const link = el('a', 'insp-banner-link', 'Open on the Dashboard');
        link.href = '/?job=' + encodeURIComponent(job.id);
        box.appendChild(link);
        target.replaceChildren(box);
    }

    // ------------------------------------------------------------------ tiles

    function statTiles(tiles) {
        const grid = el('div', 'insp-stats');
        grid.id = 'inspTiles';
        tiles.forEach(function (t) { grid.appendChild(t); });
        return grid;
    }

    function stat(key, label, title, sub, tone) {
        const tile = el('div', 'insp-stat');
        tile.dataset.tile = key;
        const head = el('div', 'insp-stat-title' + (tone ? ' is-' + tone : ''));
        if (typeof title === 'string') head.textContent = title;
        else head.appendChild(title);
        tile.append(el('div', 'insp-stat-label', label), head, el('div', 'insp-stat-sub', sub));
        return tile;
    }

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

    function serversStat() {
        const label = 'ON YOUR SERVERS';
        const list = servers();
        if (!state.item) return stat('servers', label, 'Couldn\'t read', 'What your servers show couldn\'t be read just now', 'warn');
        if (!list.length) return stat('servers', label, 'No server', 'No server has this file in a library', 'muted');
        if (!isChecked()) {
            const own = list.filter(function (s) { return (s.current || []).some(function (m) { return !m.ours; }); });
            const rest = list.filter(function (s) { return own.indexOf(s) === -1; }).map(serverName);
            const later = rest.length ? `${joinWith(rest, 'and')} ${rest.length === 1 ? 'gets' : 'get'} ours once this ${fileWord()} is checked`
                : `Checking this ${fileWord()} decides ours`;
            if (own.length === 1) return stat('servers', label, `${serverName(own[0])}'s own only`, later);
            if (own.length > 1) return stat('servers', label, 'Their own only', later);
            return stat('servers', label, 'Nothing shown yet', `Your servers get ours once this ${fileWord()} is checked`, 'muted');
        }
        if (!decidedTypes().length) {
            return stat('servers', label, 'Nothing to send yet', reviewTypes().length ? 'Waiting for your check above' : 'Nothing was decided for this file', 'muted');
        }
        const sentences = serverSentences().join(' ');
        const need = attention();
        if (need.length) return stat('servers', label, `${need.length} need${need.length === 1 ? 's' : ''} attention`, sentences, 'warn');
        const ours = list.filter(function (s) { return s.plan === 'up_to_date'; }).length;
        return stat('servers', label, `${ours} of ${list.length} show${ours === 1 ? 's' : ''} ours`, sentences);
    }

    function timeSpan(type, ms) {
        return el('span', 'insp-mono insp-t-' + tone(type), clock(ms));
    }

    function foundStat() {
        const label = 'WE FOUND';
        if (!state.item) return stat('found', label, 'Couldn\'t read', state.itemError || 'Intro & Credits couldn\'t be read for this file', 'warn');
        if (!isChecked()) return stat('found', label, 'Not checked yet', `Check this ${fileWord()} to decide ours`, 'muted');
        const types = decidedTypes();
        const review = reviewTypes();
        const reviewNote = review.length ? `The ${typePhrase(review)} need${review.length === 1 && review[0] !== 'credits' ? 's' : ''} your check above` : '';
        if (!types.length) {
            if (review.length) return stat('found', label, 'Needs your check', reviewNote, 'warn');
            return stat('found', label, 'Nothing found', 'No intro or credits were found. Adjust adds them by hand.', 'muted');
        }
        const dur = duration();
        const lines = el('div', 'insp-stat-lines');
        types.forEach(function (t) {
            const m = decided(t);
            const line = el('div');
            line.dataset.type = t;
            line.append(`${TYPE_LABELS[t]} `, timeSpan(t, m.start_ms));
            if (toEnd(m, dur)) line.append(' → end');
            else line.append(' – ', timeSpan(t, segmentEnd(m, dur)));
            lines.appendChild(line);
        });
        const locked = lockedTypes();
        let sub;
        if (locked.length === types.length) {
            sub = 'Set by you · later checks keep it';
        } else {
            const names = [];
            types.filter(function (t) { return locked.indexOf(t) === -1; }).forEach(function (t) {
                (decided(t).decided_by || []).forEach(function (s) {
                    const name = (SOURCES[s] || [s])[0];
                    if (names.indexOf(name) === -1) names.push(name);
                });
            });
            sub = names.length ? `From ${joinWith(names, 'and')}` : '';
            if (locked.length) sub += `${sub ? ' · ' : ''}${capitalise(typePhrase(locked))} set by you`;
        }
        if (reviewNote) sub += `${sub ? ' · ' : ''}${reviewNote}`;
        return stat('found', label, lines, sub);
    }

    function timelineSlots(step) {
        const dur = duration();
        return dur ? Math.floor((dur - 1) / step) + 1 : 0;
    }

    function previewPartial() {
        const p = preview();
        const step = interval();
        return !!(p && step && duration() && p.frame_count < timelineSlots(step) - 2);
    }

    function previewStat() {
        const label = 'PREVIEW';
        const p = preview();
        const dur = duration();
        if (!p) return stat('preview', label, 'No preview yet', 'Regenerate preview to make one', 'warn');
        const step = interval();
        const covers = step ? p.frame_count * step : 0;
        if (previewPartial()) {
            return stat('preview', label, `Stops at ${clock(covers)}`, `Covers ${clock(covers)} of ${clock(dur)} · Regenerate preview to finish it`, 'warn');
        }
        const title = `${Number(p.frame_count).toLocaleString()} frames${step ? ` · every ${seconds(step)}` : ''}`;
        const sub = [p.created_at ? `Made ${day(p.created_at)}` : '', bytes(p.file_size), covers ? `covers ${clock(covers)}` : '']
            .filter(Boolean).join(' · ');
        return stat('preview', label, title, sub);
    }

    function checkedStat() {
        const label = 'INTRO & CREDITS CHECKED';
        if (!state.item) return stat('checked', label, 'Unknown', 'Intro & Credits couldn\'t be read', 'muted');
        if (!isChecked()) {
            const own = servers().filter(function (s) { return (s.current || []).some(function (m) { return !m.ours; }); }).map(serverName);
            return stat('checked', label, 'Never', own.length
                ? `${joinWith(own, 'and')} show${own.length === 1 ? 's its' : ' their'} own markers meanwhile` : 'Nothing is shown meanwhile', 'muted');
        }
        const checkedAt = ((state.item && state.item.evidence) || []).map(function (e) { return e.fetched_at; }).filter(Boolean).sort().pop();
        const parts = [];
        // Intro and credits are always named ("No intro · credits found"); a recap or preview only when there is one.
        TYPES.forEach(function (t) {
            const d = decision(t);
            if (d.status === 'decided') parts.push(`${TYPE_WORDS[t]} found`);
            else if (d.status === 'needs_review') parts.push(`${TYPE_WORDS[t]} need${t === 'credits' ? '' : 's'} your check`);
            else if (t === 'intro' || t === 'credits') parts.push(`no ${TYPE_WORDS[t]}`);
        });
        return stat('checked', label, checkedAt ? when(checkedAt) : 'Checked', capitalise(parts.join(' · ')));
    }

    // --------------------------------------------------------------- timeline

    function niceStep(total) {
        const steps = [60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3];
        return steps.find(function (s) { return total / s <= 6; }) || 3600e3;
    }

    function axis(end, className) {
        const node = el('div', className || 'insp-axis');
        const step = niceStep(end);
        const add = function (ms, alignRight) {
            const s = el('span', '', clock(ms));
            if (alignRight) s.style.right = '0';
            else s.style.left = `${pct(ms, end)}%`;
            node.appendChild(s);
        };
        add(0, false);
        for (let t = step; t < end - step * 0.6; t += step) add(t, false);
        add(end, true);
        return node;
    }

    // The last tick before the end label is left out when the two would touch (it depends on the width drawn at).
    function tidyAxes(root) {
        root.querySelectorAll('.insp-axis').forEach(function (node) {
            const spans = node.children;
            if (spans.length < 3) return;
            const end = spans[spans.length - 1];
            const before = spans[spans.length - 2];
            before.hidden = false;
            if (before.getBoundingClientRect().right + 8 > end.getBoundingClientRect().left) before.hidden = true;
        });
    }

    function timelineKey() {
        const p = preview();
        return JSON.stringify({
            path: state.path || state.bifOnly,
            dur: duration(),
            p: p ? [p.path, p.kind, p.frame_count, p.interval_ms, p.server_id] : null,
            item: !!state.item,
            checked: isChecked(),
            d: TYPES.map(function (t) { const m = decided(t); return m ? [m.start_ms, m.end_ms] : null; }),
            r: reviewTypes(),
            s: servers().map(function (s) { return [s.server_id, s.server_name, s.error, s.markers_enabled, s.plan, s.can_show, s.current]; }),
        });
    }

    function timelineCard() {
        const card = el('div', 'insp-card insp-timeline-card');
        card.id = 'inspTimeline';
        if (!duration() && !lengthUnknown()) {
            const head = el('div', 'insp-tl-head');
            head.appendChild(el('div', 'insp-card-title', 'Timeline'));
            card.append(head, el('div', 'insp-empty-strip', 'This file\'s length isn\'t known yet, so there is no timeline. Checking its intro & credits reads it.'));
            return card;
        }
        const key = timelineKey();
        if (!timeline || timeline.key !== key) {
            const same = timeline && timeline.path === (state.path || state.bifOnly) && timeline.numbered === lengthUnknown();
            const keepT = same ? timeline.nowT() : null;
            timeline = makeTimeline(key, keepT);
        }
        card.appendChild(timeline.node);
        if (state.adjust) card.appendChild(adjustPanel());
        return card;
    }

    function foundLane(dur) {
        const lane = { key: 'found', name: 'We found', dot: 'ours', strong: true, bands: [], note: null };
        if (!state.item) {
            lane.dot = 'bad';
            lane.note = { text: 'Couldn\'t read Intro & Credits for this file', cls: 'is-bad' };
        } else if (!isChecked()) {
            lane.dot = 'none';
            lane.note = { text: 'Not checked yet', cls: 'is-muted' };
        } else if (!decidedTypes().length) {
            lane.note = reviewTypes().length ? { text: 'Needs your check', cls: 'is-warn' } : { text: 'Nothing found', cls: 'is-muted' };
        } else {
            lane.bands = decidedTypes().map(function (t) {
                const m = decided(t);
                const text = `${TYPE_LABELS[t]} ${rangeText(m, dur)}`;
                return { start: m.start_ms, end: segmentEnd(m, dur), text: text, aria: `We found: ${text}`, cls: `is-ours is-${tone(t)}`, key: t };
            });
        }
        return lane;
    }

    function serverLane(s, dur) {
        const name = serverName(s);
        const lane = { key: s.server_id, serverId: s.server_id, name: name, dot: serverDot(s), strong: false, bands: [], note: null };
        const current = Array.isArray(s.current) ? s.current : null;
        if (s.error || current === null) {
            lane.note = { text: 'Couldn\'t read what it shows now', cls: 'is-bad' };
            return lane;
        }
        if (!current.length) {
            lane.note = { text: emptyText(s), cls: 'is-muted' };
            return lane;
        }
        lane.bands = sortedCurrent(s).map(function (m, i) {
            const text = `${TYPE_LABELS[m.type] || m.type} ${rangeText(m, dur)}${m.stale ? ' (made for an earlier file)' : ''}`;
            return {
                start: m.start_ms,
                end: segmentEnd(m, dur),
                text: text,
                aria: `${name}: ${m.ours ? 'our ' : ''}${text}`,
                cls: m.ours ? `is-tint is-${tone(m.type)}` : 'is-own',
                key: m.ours ? m.type : `own-${s.server_id}-${i}`,
            };
        });
        return lane;
    }

    function edgeLines(dur) {
        const lines = [];
        if (isChecked()) {
            decidedTypes().forEach(function (t) {
                const m = decided(t);
                const cls = `is-ours is-${tone(t)}`;
                lines.push({ t: m.start_ms, label: `${TYPE_LABELS[t]} start · ${clock(m.start_ms)}`, cls: cls, key: t });
                if (!toEnd(m, dur)) lines.push({ t: m.end_ms, label: `${TYPE_LABELS[t]} end · ${clock(m.end_ms)}`, cls: cls, key: t });
            });
        }
        const ours = lines.slice();
        const near = function (t) { return ours.some(function (l) { return Math.abs(l.t - t) < SAME_EDGE_MS; }); };
        ownMarkers().forEach(function (x) {
            const name = serverName(x.server);
            const word = TYPE_WORDS[x.marker.type] || x.marker.type;
            if (!near(x.marker.start_ms)) lines.push({ t: x.marker.start_ms, label: `${name} ${word} · ${clock(x.marker.start_ms)}`, cls: 'is-own', key: x.key });
            if (!toEnd(x.marker, dur) && !near(x.marker.end_ms)) {
                lines.push({ t: x.marker.end_ms, label: `${name} ${word} end · ${clock(x.marker.end_ms)}`, cls: 'is-own', key: x.key });
            }
        });
        return lines.sort(function (a, b) { return a.t - b.t; });
    }

    function jumpChips(dur) {
        const chips = [];
        if (isChecked()) {
            // One waiting for your check isn't "none": the panels above ask about it.
            ['intro', 'credits'].forEach(function (t) {
                if (!decided(t) && decision(t).status !== 'needs_review') {
                    chips.push({ disabled: true, label: `No ${TYPE_WORDS[t]}`, dot: tone(t), title: `No ${TYPE_WORDS[t]} in this ${fileWord()}` });
                }
            });
            decidedTypes().slice().sort(function (a, b) { return decided(a).start_ms - decided(b).start_ms; }).forEach(function (t) {
                chips.push({ key: t, label: TYPE_LABELS[t], time: decided(t).start_ms, dot: tone(t) });
            });
        }
        ownMarkers().forEach(function (x) {
            chips.push({ key: x.key, label: serverName(x.server), time: x.marker.start_ms, dot: 'own', title: `${serverName(x.server)}'s own ${TYPE_WORDS[x.marker.type] || x.marker.type} ${rangeText(x.marker, dur)}` });
        });
        return chips;
    }

    function ownTagAt(t, dur) {
        const own = ownMarkers().find(function (x) { return t >= x.marker.start_ms && t < segmentEnd(x.marker, dur); });
        return own ? `${serverName(own.server)}'s own ${TYPE_WORDS[own.marker.type] || own.marker.type} here` : '';
    }

    // The words under the strip for the time at its centre line: ours, a server's own, or the story in between.
    function tagAt(t) {
        if (state.bifOnly || !state.item) return ['', ''];
        const dur = duration();
        if (isChecked()) {
            const type = decidedTypes().find(function (ty) { const m = decided(ty); return t >= m.start_ms && t < segmentEnd(m, dur); });
            if (type) return [oursTag(type, t, dur), 'is-' + tone(type)];
        }
        const own = ownTagAt(t, dur);
        if (own) return [own, 'is-own'];
        return isChecked() ? ['Story', 'is-muted'] : ['Not checked yet', 'is-muted'];
    }

    function oursTag(type, t, dur) {
        const label = TYPE_LABELS[type];
        const able = enabledOwners(type);
        if (!able.length) return label;
        const covers = function (s) {
            return (s.current || []).some(function (m) { return m.type === type && t >= m.start_ms && t < segmentEnd(m, dur); });
        };
        const missing = able.filter(function (s) { return !covers(s); });
        if (!missing.length) return `${label} on every server`;
        for (const s of missing) {
            const own = sortedCurrent(s).find(function (m) { return m.type === type && !m.ours && m.start_ms > t; });
            if (own) return `${label} · ${serverName(s)} starts at ${clock(own.start_ms)}`;
        }
        return label;
    }

    function timelineTip(step, numbered) {
        if (numbered) return 'Every preview frame in order, by number. Scroll or drag the strip, or click the bar above it to jump. Click a frame to see it large.';
        const head = preview() ? `Every preview frame in order, one every ${seconds(step)}.` : 'This file has no preview yet, so each tile is a place a frame will go.';
        const rows = state.bifOnly ? '' : ' Each row below shows what that server gives viewers.';
        return `${head} Scroll or drag the strip, or click the bar above it to jump. Click a frame to see it large.${rows}`;
    }

    // One strip of every preview frame, windowed: only the tiles near the viewport exist, and their images load once
    // the strip stops. Lanes, bands and edge lines are few, so they are drawn once in strip coordinates.
    function makeTimeline(key, keepT) {
        const p = preview();
        const frames = p ? p.frame_count : 0;
        // With no length the strip counts in frames: one unit a frame, and the "duration" is the frame count.
        const numbered = lengthUnknown();
        const dur = numbered ? frames : duration();
        const step = numbered ? 1 : stripStep();
        let count = numbered ? frames : timelineSlots(step);
        // A preview a frame or two off the file's length is a whole preview, not a partial one.
        if (frames && (Math.abs(frames - count) <= 2 || frames > count)) count = frames;
        const width = count * PITCH - (PITCH - TILE_W);
        const xOf = function (t) { return (t / step) * PITCH + TILE_W / 2; };
        const plain = state.bifOnly || numbered;
        const lanes = plain ? [] : [foundLane(dur)].concat(servers().map(function (s) { return serverLane(s, dur); }));
        const height = lanes.length ? LANE_TOP + (lanes.length - 1) * LANE_PITCH + LANE_H : STRIP_BARE_H;
        const lines = plain ? [] : edgeLines(dur);
        const chips = plain ? [] : jumpChips(dur);

        const tl = {
            key: key,
            path: state.path || state.bifOnly,
            numbered: numbered,
            step: step,
            frames: frames,
            count: count,
            pad: 0,
            vw: 0,
            mounted: false,
            saved: null,
            active: '',
            nowIndex: -1,
            // Where a smooth scroll is heading, so steps pressed during it add up.
            target: null,
            tiles: new Map(),
        };

        const node = el('div', 'insp-tl');
        tl.node = node;

        // Head: title, range, legend, jump chips.
        const head = el('div', 'insp-tl-head');
        const titleRow = el('div', 'insp-tl-title');
        const range = numbered ? `${frames.toLocaleString()} frames` : `0:00 – ${clock(dur)}`;
        titleRow.append(el('div', 'insp-card-title', 'Timeline'), el('div', 'insp-tl-range insp-mono', range), infoIcon(timelineTip(step, numbered)));
        if (!plain) {
            const legend = el('div', 'insp-legend');
            const oursKey = el('span', 'insp-legend-item');
            const tones = lanes[0].bands.map(function (b) { return b.cls.indexOf('is-intro') !== -1 ? 'intro' : 'credits'; })
                .filter(function (v, i, all) { return all.indexOf(v) === i; });
            (tones.length ? tones : ['credits']).forEach(function (t) { oursKey.appendChild(el('span', 'insp-swatch is-' + t)); });
            oursKey.append('ours');
            const ownKey = el('span', 'insp-legend-item');
            ownKey.append(el('span', 'insp-swatch is-own'), 'a server\'s own');
            legend.append(oursKey, ownKey);
            titleRow.appendChild(legend);
        }
        head.appendChild(titleRow);
        const jumps = el('div', 'insp-jumps');
        jumps.id = 'inspJumps';
        if (chips.length) {
            jumps.appendChild(el('span', 'insp-jumps-label', 'Jump to'));
            chips.forEach(function (c) {
                const b = el('button', 'insp-jump' + (c.disabled ? ' is-off' : ''));
                b.type = 'button';
                b.appendChild(el('span', 'insp-jump-dot is-' + c.dot));
                b.append(c.label);
                if (c.title) b.title = c.title;
                if (c.disabled) {
                    b.disabled = true;
                } else {
                    b.dataset.jump = c.key;
                    b.appendChild(el('span', 'insp-mono insp-jump-time', clock(c.time)));
                    b.addEventListener('click', function () { tl.goTo(c.time, c.key, true); });
                }
                jumps.appendChild(b);
            });
        }
        head.appendChild(jumps);
        node.appendChild(head);
        if (numbered) {
            const note = el('div', 'insp-tl-note', 'This file\'s length isn\'t known yet, so frames are shown by number.');
            note.id = 'inspLengthNote';
            node.appendChild(note);
        }

        // Overview bar: thumbnails, our bands, the viewport box, the "now" bubble, and the axis.
        const ovRow = el('div', 'insp-tl-row');
        ovRow.appendChild(el('div', 'insp-tl-gutter'));
        const ovCol = el('div', 'insp-ov');
        const bubbleRow = el('div', 'insp-ov-bubble-row');
        const bubble = el('div', 'insp-ov-bubble insp-mono');
        bubble.id = 'inspOvNow';
        bubbleRow.appendChild(bubble);
        const bar = el('button', 'insp-ov-bar');
        bar.type = 'button';
        bar.id = 'inspOverview';
        bar.setAttribute('aria-label', `Jump to this point in the ${state.bifOnly ? 'preview' : fileWord()}`);
        const thumbs = el('span', 'insp-ov-thumbs');
        bar.appendChild(thumbs);
        if (!numbered && isChecked()) {
            decidedTypes().forEach(function (t) {
                const m = decided(t);
                const band = el('span', 'insp-ov-band is-' + tone(t));
                band.style.left = `${pct(m.start_ms, dur)}%`;
                band.style.width = `${Math.max(0.3, pct(segmentEnd(m, dur), dur) - pct(m.start_ms, dur))}%`;
                bar.appendChild(band);
            });
        }
        const box = el('span', 'insp-ov-box');
        bar.appendChild(box);
        bar.addEventListener('click', function (e) {
            const r = bar.getBoundingClientRect();
            tl.goTo(clamp((e.clientX - r.left) / r.width, 0, 1) * dur, null, true);
        });
        ovCol.append(bubbleRow, bar);
        if (!numbered) ovCol.appendChild(axis(dur, 'insp-axis insp-ov-axis'));
        ovRow.appendChild(ovCol);
        node.appendChild(ovRow);

        // Readout: the time at the centre line, which preview frame it is, and what is there.
        const readRow = el('div', 'insp-tl-row insp-readout-row');
        readRow.appendChild(el('div', 'insp-tl-gutter'));
        const readout = el('div', 'insp-readout');
        const now = el('div', 'insp-readout-left');
        const nowTime = el('div', 'insp-readout-time insp-mono');
        nowTime.id = 'inspNow';
        const frameText = el('div', 'insp-readout-frame');
        frameText.id = 'inspFrameText';
        const tag = el('div', 'insp-readout-tag');
        tag.id = 'inspNowTag';
        now.append(nowTime, frameText, tag);
        const nav = el('div', 'insp-readout-nav');
        const back = button('', 'insp-round-btn', function () { tl.stepBy(-STEP_FRAMES); });
        back.appendChild(icon('chevron-left'));
        back.setAttribute('aria-label', `Back ${STEP_FRAMES} frames`);
        back.title = `Back ${STEP_FRAMES} frames`;
        const fwd = button('', 'insp-round-btn', function () { tl.stepBy(STEP_FRAMES); });
        fwd.appendChild(icon('chevron-right'));
        fwd.setAttribute('aria-label', `Forward ${STEP_FRAMES} frames`);
        fwd.title = `Forward ${STEP_FRAMES} frames`;
        nav.append(el('span', 'insp-readout-hint', frames ? 'Click a frame to see it large' : ''), back, fwd);
        readout.append(now, nav);
        readRow.appendChild(readout);
        node.appendChild(readRow);

        // The strip, with the lane names beside it.
        const stripRow = el('div', 'insp-tl-row insp-strip-row');
        const names = el('div', 'insp-tl-gutter insp-lane-names');
        names.style.height = `${height + 12}px`;
        lanes.forEach(function (lane, k) {
            const n = el('div', 'insp-lane-name' + (lane.strong ? ' is-strong' : ''));
            n.style.top = `${LANE_TOP + k * LANE_PITCH}px`;
            n.dataset.lane = lane.key;
            n.append(dot(lane.dot), el('span', '', lane.name));
            n.title = lane.name;
            names.appendChild(n);
        });
        stripRow.appendChild(names);
        const wrap = el('div', 'insp-strip-wrap');
        wrap.style.height = `${height + 12}px`;
        const scroller = el('div', 'insp-strip');
        scroller.id = 'inspStrip';
        scroller.tabIndex = 0;
        scroller.setAttribute('aria-label', 'Preview frames, in order');
        const content = el('div', 'insp-strip-content');
        content.style.height = `${height}px`;
        const inner = el('div', 'insp-strip-inner');
        inner.style.width = `${width}px`;
        inner.style.height = `${height}px`;

        const bandNodes = [];
        lanes.forEach(function (lane, k) {
            const laneNode = el('div', 'insp-lane');
            laneNode.style.top = `${LANE_TOP + k * LANE_PITCH}px`;
            laneNode.dataset.lane = lane.key;
            if (lane.serverId) laneNode.dataset.serverId = lane.serverId;
            if (lane.note) {
                const note = el('div', 'insp-lane-note ' + lane.note.cls);
                note.appendChild(el('span', '', lane.note.text));
                laneNode.appendChild(note);
            }
            lane.bands.forEach(function (b) {
                const left = xOf(b.start);
                const right = Math.min(width, xOf(b.end));
                const band = el('button', 'insp-band ' + b.cls);
                band.type = 'button';
                band.style.left = `${left}px`;
                band.style.width = `${Math.max(6, right - left)}px`;
                band.title = b.aria;
                band.setAttribute('aria-label', b.aria);
                band.dataset.key = b.key;
                band.appendChild(el('span', 'insp-band-text', b.text));
                band.addEventListener('click', function () { tl.goTo(b.start, b.key, true); });
                bandNodes.push(band);
                laneNode.appendChild(band);
            });
            inner.appendChild(laneNode);
        });

        const tilesLayer = el('div', 'insp-tiles');
        inner.appendChild(tilesLayer);
        tilesLayer.addEventListener('click', function (e) {
            const hit = e.target.closest('.insp-tl-img');
            if (hit) openBig(Number(hit.parentNode.dataset.index));
        });

        const lineNodes = [];
        lines.forEach(function (line) {
            const n = el('div', 'insp-edge ' + line.cls);
            n.style.left = `${xOf(line.t)}px`;
            n.style.height = `${height}px`;
            n.dataset.key = line.key;
            n.appendChild(el('div', 'insp-edge-rule'));
            const flag = el('div', 'insp-edge-flag', line.label);
            n.appendChild(flag);
            n.title = line.label;
            lineNodes.push(n);
            inner.appendChild(n);
        });

        content.appendChild(inner);
        scroller.appendChild(content);
        const centre = el('div', 'insp-centre');
        centre.setAttribute('aria-hidden', 'true');
        wrap.append(scroller, centre);
        stripRow.appendChild(wrap);
        node.appendChild(stripRow);

        tl.nowT = function () {
            return tl.nowIndex >= 0 ? tl.nowIndex * step : (keepT !== null && keepT !== undefined ? keepT : 0);
        };

        tl.keepScroll = function () {
            if (node.isConnected) tl.saved = scroller.scrollLeft;
        };

        function centreIndex() {
            const x = scroller.scrollLeft + tl.vw / 2 - tl.pad - TILE_W / 2;
            return clamp(Math.round(x / PITCH), 0, count - 1);
        }

        function layout(t) {
            tl.vw = scroller.clientWidth || 1152;
            tl.pad = Math.max(0, Math.round(tl.vw / 2 - PITCH / 2));
            content.style.width = `${width + tl.pad * 2}px`;
            inner.style.left = `${tl.pad}px`;
            scroller.scrollLeft = Math.max(0, tl.pad + xOf(t) - tl.vw / 2);
        }

        // Flags on the strip never cover each other: one that would is left out (its line stays, named by its title).
        function layoutFlags() {
            let right = -Infinity;
            lineNodes.forEach(function (n) {
                const flag = n.querySelector('.insp-edge-flag');
                flag.hidden = false;
                const left = parseFloat(n.style.left) + 6;
                if (left < right + 6) flag.hidden = true;
                else right = left + flag.offsetWidth;
            });
        }

        // Each thumbnail is the frame at its own place on the bar; past the end of a short preview there is none.
        function buildThumbs() {
            if (!frames || thumbs.childElementCount) return;
            const k = clamp(Math.round(tl.vw / 56), 8, 24);
            for (let i = 0; i < k; i++) {
                const index = Math.floor((((i + 0.5) / k) * dur) / step);
                if (index >= frames) {
                    thumbs.appendChild(el('span', 'insp-ov-gap'));
                    continue;
                }
                const img = el('img');
                img.alt = '';
                img.draggable = false;
                img.src = frameUrl(index);
                thumbs.appendChild(img);
            }
        }

        function makeTile(i) {
            const t = i * step;
            const wrapTile = el('div', 'insp-tl-frame');
            wrapTile.style.left = `${i * PITCH}px`;
            wrapTile.dataset.index = String(i);
            if (i < frames) {
                const b = el('button', 'insp-tl-img');
                b.type = 'button';
                b.setAttribute('aria-label', `See frame ${numbered ? i + 1 : clock(t)} large`);
                const img = el('img');
                img.alt = '';
                img.draggable = false;
                img.dataset.src = frameUrl(i);
                b.appendChild(img);
                wrapTile.appendChild(b);
            } else {
                wrapTile.appendChild(el('div', 'insp-tl-none', 'No preview'));
            }
            wrapTile.appendChild(el('div', 'insp-tl-time insp-mono', frameLabel(i)));
            return wrapTile;
        }

        function windowTiles() {
            const left = scroller.scrollLeft - tl.pad;
            const extra = tl.vw * WINDOW_EXTRA;
            const first = Math.max(0, Math.floor((left - extra) / PITCH));
            const last = Math.min(count - 1, Math.ceil((left + tl.vw + extra) / PITCH));
            tl.tiles.forEach(function (n, i) {
                if (i < first || i > last) {
                    n.remove();
                    tl.tiles.delete(i);
                }
            });
            for (let i = first; i <= last; i++) {
                if (!tl.tiles.has(i)) {
                    const n = makeTile(i);
                    tilesLayer.appendChild(n);
                    tl.tiles.set(i, n);
                }
            }
        }

        function loadImages() {
            tl.tiles.forEach(function (n) {
                const img = n.querySelector('img');
                if (img && !img.getAttribute('src')) img.src = img.dataset.src;
            });
        }

        let settleTimer = null;
        let frameRequested = false;

        function update() {
            frameRequested = false;
            if (!node.isConnected) return;
            windowTiles();
            const idx = centreIndex();
            if (idx !== tl.nowIndex) {
                const old = tl.tiles.get(tl.nowIndex);
                if (old) old.classList.remove('is-now');
                tl.nowIndex = idx;
            }
            const cur = tl.tiles.get(idx);
            if (cur) cur.classList.add('is-now');
            const t = idx * step;
            nowTime.textContent = frameLabel(idx);
            bubble.textContent = frameLabel(idx);
            const every = numbered ? '' : ` · one every ${seconds(step)}`;
            frameText.textContent = idx < frames
                ? `preview frame ${(idx + 1).toLocaleString()} of ${frames.toLocaleString()}${every}`
                : 'no preview frame here';
            const tg = numbered ? ['', ''] : tagAt(t);
            tag.textContent = tg[0];
            tag.className = 'insp-readout-tag ' + tg[1];
            // The overview: the bubble over the centre time, the box over what the strip shows.
            const barW = bar.clientWidth || 1;
            bubble.style.left = `${clamp((t / dur) * barW, 32, Math.max(32, barW - 32))}px`;
            const from = Math.max(0, ((scroller.scrollLeft - tl.pad - TILE_W / 2) / PITCH) * step);
            const to = Math.min(dur, ((scroller.scrollLeft + tl.vw - tl.pad - TILE_W / 2) / PITCH) * step);
            box.style.left = `${pct(from, dur)}%`;
            box.style.width = `${Math.max(0, pct(to, dur) - pct(from, dur))}%`;
            clearTimeout(settleTimer);
            settleTimer = setTimeout(function () {
                tl.target = null;
                loadImages();
            }, IMAGE_SETTLE_MS);
        }

        function schedule() {
            if (frameRequested) return;
            frameRequested = true;
            requestAnimationFrame(update);
        }

        function setActive(k) {
            tl.active = k || '';
            node.querySelectorAll('[data-jump]').forEach(function (b) { b.classList.toggle('is-active', !!k && b.dataset.jump === k); });
            bandNodes.concat(lineNodes).forEach(function (n) { n.classList.toggle('is-active', !!k && n.dataset.key === k); });
        }

        // A move of a few screens glides; a far jump (the overview bar, a chip across the film) lands at once, since
        // gliding past thousands of frames shows nothing and takes seconds.
        tl.goTo = function (t, k, smooth) {
            const left = Math.max(0, tl.pad + xOf(t) - tl.vw / 2);
            const glide = smooth && Math.abs(left - scroller.scrollLeft) <= tl.vw * GLIDE_SCREENS;
            tl.target = glide ? clamp(Math.round(t / step), 0, count - 1) : null;
            if (glide) scroller.scrollTo({ left: left, behavior: 'smooth' });
            else scroller.scrollLeft = left;
            if (k !== null && k !== undefined) setActive(k);
            schedule();
        };

        tl.showFrame = function (i) {
            tl.goTo(i * step, null, true);
        };

        tl.stepBy = function (n) {
            const from = tl.target !== null ? tl.target : centreIndex();
            tl.goTo(clamp(from + n, 0, count - 1) * step, null, true);
        };

        tl.relayout = function () {
            if (!node.isConnected || !tl.mounted) return;
            const t = centreIndex() * step;
            layout(t);
            layoutFlags();
            update();
        };

        tl.mount = function () {
            if (!tl.mounted) {
                let t = keepT;
                let k = null;
                if (t === null || t === undefined) {
                    const first = lines.find(function (l) { return l.cls.indexOf('is-ours') !== -1; }) || lines[0];
                    t = first ? first.t : 0;
                    k = first ? first.key : null;
                }
                layout(t);
                buildThumbs();
                layoutFlags();
                if (k) setActive(k);
                tl.mounted = true;
                update();
                loadImages();
                return;
            }
            if (scroller.clientWidth && scroller.clientWidth !== tl.vw) {
                tl.relayout();
                return;
            }
            if (tl.saved !== null) scroller.scrollLeft = tl.saved;
            layoutFlags();
            update();
        };

        scroller.addEventListener('scroll', schedule, { passive: true });

        // A vertical wheel moves the strip sideways; at either end it scrolls the page as usual.
        scroller.addEventListener('wheel', function (e) {
            if (e.shiftKey || Math.abs(e.deltaX) >= Math.abs(e.deltaY)) return;
            const unit = e.deltaMode === 1 ? 40 : (e.deltaMode === 2 ? tl.vw : 1);
            const delta = e.deltaY * unit;
            const max = scroller.scrollWidth - scroller.clientWidth;
            if ((delta < 0 && scroller.scrollLeft <= 0) || (delta > 0 && scroller.scrollLeft >= max - 1)) return;
            scroller.scrollLeft += delta;
            e.preventDefault();
        }, { passive: false });

        // Click and drag moves the strip; a drag never counts as a click on the frame or band under the pointer.
        let drag = null;
        let swallowClick = false;
        const onMove = function (e) {
            if (!drag) return;
            const dx = e.clientX - drag.x;
            if (!drag.moved && Math.abs(dx) > 4) {
                drag.moved = true;
                scroller.classList.add('is-dragging');
            }
            if (drag.moved) scroller.scrollLeft = drag.left - dx;
        };
        const onUp = function () {
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            if (drag && drag.moved) {
                swallowClick = true;
                scroller.classList.remove('is-dragging');
                setTimeout(function () { swallowClick = false; }, 0);
            }
            drag = null;
        };
        scroller.addEventListener('pointerdown', function (e) {
            if (e.button !== 0 || e.pointerType !== 'mouse') return;
            drag = { x: e.clientX, left: scroller.scrollLeft, moved: false };
            window.addEventListener('pointermove', onMove);
            window.addEventListener('pointerup', onUp);
        });
        scroller.addEventListener('click', function (e) {
            if (!swallowClick) return;
            e.stopPropagation();
            e.preventDefault();
        }, true);
        scroller.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                tl.goTo((centreIndex() + (e.key === 'ArrowLeft' ? -1 : 1)) * step, null, false);
            }
        });

        return tl;
    }

    // ------------------------------------------------------------ frame dialog

    function frameDialog() {
        let d = $('inspFrameDialog');
        if (d) return d;
        d = el('dialog', 'insp-dialog');
        d.id = 'inspFrameDialog';
        d.setAttribute('aria-labelledby', 'inspBigTime');
        const head = el('div', 'insp-dialog-head');
        const title = el('div', 'insp-dialog-title');
        const time = el('div', 'insp-mono insp-dialog-time');
        time.id = 'inspBigTime';
        const text = el('div', 'insp-dialog-text');
        text.id = 'inspBigText';
        const tag = el('div', 'insp-readout-tag');
        tag.id = 'inspBigTag';
        title.append(time, text, tag);
        const close = button('', 'insp-round-btn is-plain', closeBig);
        close.setAttribute('aria-label', 'Close');
        close.appendChild(icon('x-lg'));
        head.append(title, close);
        const img = el('img', 'insp-dialog-img');
        img.id = 'inspBigImg';
        img.alt = '';
        const foot = el('div', 'insp-dialog-foot');
        const prev = iconButton('chevron-left', 'Previous', 'btn insp-btn', function () { stepBig(-1); });
        prev.id = 'inspBigPrev';
        const next = button('Next', 'btn insp-btn', function () { stepBig(1); });
        next.appendChild(icon('chevron-right'));
        next.id = 'inspBigNext';
        foot.append(prev, el('div', 'insp-dialog-hint', '← → step · Esc closes'), next);
        d.append(head, img, foot);
        d.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                stepBig(e.key === 'ArrowLeft' ? -1 : 1);
            }
        });
        d.addEventListener('click', function (e) { if (e.target === d) closeBig(); });
        d.addEventListener('close', function () { big = null; });
        $('inspector').appendChild(d);
        return d;
    }

    function openBig(index) {
        const p = preview();
        if (!p || !p.frame_count) return;
        big = clamp(index, 0, p.frame_count - 1);
        const d = frameDialog();
        updateBig();
        if (!d.open) d.showModal();
    }

    function stepBig(delta) {
        const p = preview();
        if (big === null || !p) return;
        const next = clamp(big + delta, 0, p.frame_count - 1);
        if (next === big) return;
        big = next;
        updateBig();
        if (timeline) timeline.showFrame(big);
    }

    function updateBig() {
        const p = preview();
        const step = stripStep();
        const t = big * step;
        const numbered = lengthUnknown();
        const img = $('inspBigImg');
        img.src = frameUrl(big);
        img.alt = numbered ? `Preview frame ${big + 1}` : `Preview frame at ${clock(t)}`;
        $('inspBigTime').textContent = frameLabel(big);
        $('inspBigText').textContent = `preview frame ${(big + 1).toLocaleString()} of ${Number(p.frame_count).toLocaleString()}`;
        const tg = numbered ? ['', ''] : tagAt(t);
        const tag = $('inspBigTag');
        tag.textContent = tg[0];
        tag.className = 'insp-readout-tag ' + tg[1];
        $('inspBigPrev').disabled = big <= 0;
        $('inspBigNext').disabled = big >= p.frame_count - 1;
    }

    function closeBig() {
        const d = $('inspFrameDialog');
        if (d && d.open) d.close();
        big = null;
    }

    // ------------------------------------------------------------------ season

    const CHIP_NAMES = {
        chapters: 'Chapters', theintrodb: 'TheIntroDB', introdb: 'IntroDB', skipdb: 'SkipDB', season_audio: 'Season audio',
        season_audio_previous: 'Previous season', credits_text: 'Credit text', user: 'Your marker',
    };
    const DOT_WORDS = {
        ok: 'shows our markers', waiting: 'waiting', failed: 'failed', skipped: 'skipped', none: 'nothing sent yet',
        off: 'Intro & Credits is off',
    };
    // Matches markers.audio.season.MAX_GROUP_EPISODES: a folder with more episodes is capped to the nearest this many.
    const MAX_GROUP_EPISODES = 40;

    function scopeToggle() {
        const row = el('div', 'insp-scope');
        const seg = el('div', 'insp-seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Show this episode or the whole season');
        [['episode', 'This episode', 'inspScopeEpisode'], ['season', 'Whole season', 'inspScopeSeason']].forEach(function (opt) {
            const on = state.scope === opt[0];
            const b = button(opt[1], 'insp-seg-btn' + (on ? ' active' : ''), function () { setScope(opt[0]); });
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
        closeBig();
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

    function seasonCell(ep, type) {
        const d = ep[type] || {};
        if (d.status === 'decided' && d.marker) return el('div', `insp-mono insp-season-time insp-t-${tone(type)}`, rangeText(d.marker, ep.duration_ms));
        if (d.status === 'needs_review') {
            const cell = el('div', 'insp-season-time insp-state-review', 'Needs review');
            if (ep.review_reason) cell.title = ep.review_reason;
            return cell;
        }
        return el('div', 'insp-season-time insp-state-muted', '—');
    }

    function seasonLane(ep, scale) {
        const lane = el('div', 'insp-season-lane');
        if (!ep.duration_ms || !scale) {
            lane.appendChild(el('div', 'insp-season-unknown', ep.known ? 'Length not known yet' : ''));
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

    function seasonFrom(ep) {
        const chips = el('div', 'insp-season-chips');
        const decidedAny = ['intro', 'credits'].some(function (t) { return ep[t] && ep[t].status === 'decided'; });
        if (!ep.known) {
            chips.appendChild(el('span', 'insp-mini-chip is-muted', 'Not checked yet'));
        } else if ((ep.evidence || []).length) {
            const names = ep.evidence.map(function (c) {
                const name = CHIP_NAMES[c.source] || c.source;
                return c.label ? `${name} ${c.label}` : name;
            });
            const chip = el('span', 'insp-mini-chip' + (/^season_audio/.test(ep.evidence[0].source) ? ' is-audio' : ''), names[0]);
            if (names.length > 1) {
                chip.appendChild(el('span', 'insp-mini-more', ` +${names.length - 1}`));
                chip.title = names.join(' · ');
            }
            chips.appendChild(chip);
        } else if (!decidedAny && !ep.needs_review) {
            chips.appendChild(el('span', 'insp-mini-chip is-muted', 'Nothing found'));
        }
        if (['intro', 'credits'].some(function (t) { return ep[t] && ep[t].marker && ep[t].marker.locked; })) {
            const lock = el('span', 'insp-mini-chip is-locked');
            lock.append(icon('lock-fill'), document.createTextNode('Locked by you'));
            chips.appendChild(lock);
        }
        return chips;
    }

    function seasonDots(ep, list) {
        const dots = el('div', 'insp-dots');
        list.forEach(function (server) {
            const st = (ep.servers || {})[server.server_id] || { state: 'none', message: '' };
            const item = el('span', 'insp-dot-item');
            const d = el('span', 'insp-dot insp-dot-' + st.state);
            d.title = `${server.server_name}: ${st.message || DOT_WORDS[st.state] || st.state}`;
            d.setAttribute('role', 'img');
            d.setAttribute('aria-label', d.title);
            item.append(d, el('span', 'insp-dot-letter', VENDOR_TILES[String(server.server_type || '').toLowerCase()] || server.server_name.charAt(0)));
            dots.appendChild(item);
        });
        return dots;
    }

    function seasonSummary(episodes, list, counts) {
        const node = el('div', 'insp-season-sub');
        node.id = 'inspSeasonSub';
        const has = function (t) { return episodes.filter(function (e) { return e[t] && e[t].status === 'decided'; }).length; };
        const intro = has('intro');
        const credits = has('credits');
        node.append(`${intro} ${intro === 1 ? 'has' : 'have'} an intro · ${credits} ${credits === 1 ? 'has' : 'have'} credits`);
        if (counts.needs_review) {
            node.append(' · ');
            const review = el('span', 'insp-state-review', `${counts.needs_review} need${counts.needs_review === 1 ? 's' : ''} review`);
            review.id = 'inspSeasonReview';
            node.appendChild(review);
        }
        const on = list.filter(function (s) { return s.markers_enabled; });
        let serversText;
        if (!on.length) {
            serversText = 'Intro & Credits is off on every server';
        } else {
            const behind = episodes.filter(function (e) {
                const any = ['intro', 'credits'].some(function (t) { return e[t] && e[t].status === 'decided'; });
                return any && on.some(function (s) { return ((e.servers || {})[s.server_id] || {}).state !== 'ok'; });
            }).length;
            serversText = behind ? `${behind} not on every server yet` : 'every server up to date';
        }
        node.append(` · ${serversText}`);
        return node;
    }

    function seasonCard() {
        const card = el('div', 'insp-card insp-season-card');
        card.id = 'inspSeason';
        if (state.seasonError) {
            card.append(el('div', 'insp-message-title', 'Couldn\'t load this season'), el('div', 'insp-small', state.seasonError));
            return card;
        }
        const payload = state.season;
        if (!payload) {
            card.classList.add('insp-loading');
            card.append(el('span', 'spinner-border spinner-border-sm'), el('span', '', 'Loading the season…'));
            return card;
        }
        const list = payload.servers || [];
        const episodes = payload.episodes || [];
        const counts = payload.counts || {};
        const on = list.filter(function (s) { return s.markers_enabled; }).length;
        const head = el('div', 'insp-season-headrow');
        const titles = el('div', 'insp-season-titles');
        const total = counts.total_episodes || counts.episodes || 0;
        const title = el('div', 'insp-season-title', `${payload.season || ''} · ${total > MAX_GROUP_EPISODES
            ? `${total} episodes (showing the ${MAX_GROUP_EPISODES} nearest)` : `${total} episode${total === 1 ? '' : 's'}`}`);
        titles.append(title, seasonSummary(episodes, list, counts));
        const acts = el('div', 'insp-season-acts');
        const legend = el('div', 'insp-legend');
        ['intro', 'credits'].forEach(function (t) {
            const item = el('span', 'insp-legend-item');
            item.append(el('span', 'insp-swatch is-' + t), TYPE_LABELS[t]);
            legend.appendChild(item);
        });
        acts.appendChild(legend);
        const publish = button(state.publishing ? 'Queueing…' : `Publish ${counts.ready || 0} to ${on} server${on === 1 ? '' : 's'}`,
            'btn insp-btn is-strong', publishSeason);
        publish.id = 'inspPublishSeason';
        publish.disabled = state.publishing || !counts.ready || !on;
        acts.appendChild(withInfo(publish, TIPS.publish));
        head.append(titles, acts);
        card.appendChild(head);

        const scale = Math.max.apply(null, [0].concat(episodes.map(function (e) { return e.duration_ms || 0; })));
        const grid = el('div', 'insp-season');
        const top = el('div', 'insp-season-row insp-season-head');
        top.append(el('div', '', 'EP'), scale ? axis(scale, 'insp-axis insp-season-axis') : el('div', '', 'INTRO & CREDITS'),
            el('div', '', 'INTRO'), el('div', '', 'CREDITS'), el('div', '', 'FROM'), el('div', '', 'SERVERS'));
        grid.appendChild(top);
        episodes.forEach(function (ep) {
            const row = el('button', 'insp-season-row' + (ep.path === state.path ? ' is-current' : ''));
            row.type = 'button';
            row.dataset.path = ep.path;
            row.dataset.episode = ep.episode || ep.name;
            row.setAttribute('aria-label', `Open ${ep.episode || ep.name}`);
            row.append(el('div', 'insp-season-ep insp-mono', ep.episode || ep.name), seasonLane(ep, scale),
                seasonCell(ep, 'intro'), seasonCell(ep, 'credits'), seasonFrom(ep), seasonDots(ep, list));
            row.addEventListener('click', function () { openFile(ep.path, { scope: 'episode' }); });
            grid.appendChild(row);
        });
        card.appendChild(grid);
        card.appendChild(el('div', 'insp-small mt-2', 'Each episode is drawn to one scale. Dots: green = the server shows our markers, amber = waiting, red = failed, grey = off, skipped or nothing sent yet. Choose an episode to open it.'));
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

    // ------------------------------------------------------------ exact frames

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
            promise.then(function (frames) { exactDone.set(key, frames); }, function () { exactCache.delete(key); });
            exactCache.set(key, promise);
        }
        return exactCache.get(key);
    }

    function edgeRounded(ms) {
        return Math.round(ms / 1000) * 1000;
    }

    function frameTile(src, t, opts) {
        const tile = el(opts.onPick ? 'button' : 'div', 'insp-frame' + (opts.ringed ? ' is-ringed' : '') + (opts.inside ? ' is-inside is-' + opts.inside : ''));
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
            tile.appendChild(img);
        } else {
            tile.appendChild(el('div', 'insp-frame-missing'));
        }
        tile.appendChild(el('span', 'insp-frame-time insp-mono', clock(t)));
        return tile;
    }

    // A line near the right end of the row puts its flag on its left, so the flag stays inside the row.
    function markLine(tile, line, after, flip) {
        const n = el('div', 'insp-adj-line ' + line.cls + (after ? ' is-after' : '') + (flip ? ' is-flip' : ''));
        n.title = line.label;
        if (line.label && !line.quiet) n.appendChild(el('div', 'insp-adj-flag', line.label));
        tile.appendChild(n);
    }

    // Exact frames one second apart from ``edge - before`` s. ``opts``: count, before, onPick (frames become buttons),
    // ring (the frame ringed), inside (whether a frame is inside the segment, for its colour), tone, and lines: edges
    // to draw ({t, label, cls}), each on the frame at its time, as a line before that frame.
    function exactFramesAround(edge, opts) {
        const count = opts.count || EXACT_COUNT;
        const before = opts.before === undefined ? ADJUST_BEFORE : opts.before;
        const start = Math.max(0, edgeRounded(edge) - before * 1000);
        const row = el('div', 'insp-frames' + (opts.lines ? ' has-lines' : ''));
        row.dataset.exact = String(start);
        row.style.setProperty('--insp-frames', String(count));
        const draw = function (frames, loading) {
            const nodes = frames.map(function (f) {
                return frameTile(loading ? '' : f.src, f.t_ms, {
                    onPick: loading ? null : opts.onPick,
                    ringed: opts.ring !== undefined && f.t_ms === edgeRounded(opts.ring),
                    pickLabel: opts.pickLabel,
                    inside: opts.inside && opts.inside(f.t_ms) ? opts.tone : '',
                });
            });
            (opts.lines || []).forEach(function (line) {
                const at = edgeRounded(line.t);
                const i = frames.findIndex(function (f) { return f.t_ms === at; });
                const last = frames[frames.length - 1];
                if (i !== -1) markLine(nodes[i], line, false, i >= nodes.length - 2);
                else if (last && at > last.t_ms && at <= last.t_ms + 1000) markLine(nodes[nodes.length - 1], line, true, true);
            });
            row.replaceChildren.apply(row, nodes);
        };
        const key = exactKey(start, count);
        if (exactDone.has(key)) {
            draw(exactDone.get(key), false);
            return row;
        }
        const placeholders = [];
        for (let i = 0; i < count; i++) placeholders.push({ t_ms: start + i * 1000, src: '' });
        draw(placeholders, true);
        exactFrames(start, count).then(function (frames) {
            if (!row.isConnected) return;
            draw(frames, false);
        }).catch(function (e) {
            row.replaceChildren(el('div', 'insp-frames-error', `Couldn't read frames here: ${e.message}`));
        });
        return row;
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
        state.confirmLock = false;
        state.confirmUnlock = false;
        render();
        const first = adjustTypes()[0];
        if (first && timeline) timeline.goTo(model[first].start, first, true);
        const panel = $('inspAdjustPanel');
        if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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

    function adjustRange(m) {
        return `${clock(m.start)} → ${m.toEnd ? 'end of file' : clock(m.end)}`;
    }

    // Where a server's own markers of this type put the same edge, drawn as dashed lines in the same frames.
    function ownEdges(type, edge, value) {
        const dur = duration();
        const out = [];
        ownMarkers().forEach(function (x) {
            if (x.marker.type !== type) return;
            if (edge === 'end' && toEnd(x.marker, dur)) return;
            const t = edge === 'start' ? x.marker.start_ms : x.marker.end_ms;
            if (Math.abs(edgeRounded(t) - value) < 1000) return;
            out.push({ t: t, label: `${serverName(x.server)} · ${clock(t)}`, cls: 'is-own', quiet: Math.abs(edgeRounded(t) - value) <= 2000 });
        });
        return out;
    }

    function edgeRow(type, edge) {
        const m = state.adjust.model[type];
        const value = edge === 'start' ? m.start : m.end;
        const word = TYPE_WORDS[type];
        const row = el('div', 'insp-adjust-edge');
        row.dataset.edge = `${type}-${edge}`;
        const lines = [{ t: value, label: `${TYPE_LABELS[type]} ${edge} · ${clock(value)}`, cls: 'is-ours is-' + tone(type) }]
            .concat(ownEdges(type, edge, value));
        row.appendChild(exactFramesAround(value, {
            count: ADJUST_COUNT,
            before: ADJUST_BEFORE,
            onPick: function (t) { setEdge(type, edge, t); },
            pickLabel: `set the ${word} ${edge} here`,
            inside: edge === 'start' ? function (t) { return t >= value; } : function (t) { return t < value; },
            tone: tone(type),
            lines: lines,
        }));
        const field = el('div', 'insp-adjust-field');
        const label = el('label', 'insp-adjust-input');
        label.append(edge === 'start' ? 'Start time' : 'End time');
        const input = el('input', 'insp-mono');
        input.type = 'text';
        input.value = clock(value);
        input.setAttribute('aria-label', `${TYPE_LABELS[type]} ${edge} time`);
        label.appendChild(input);
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
        const nudge = el('div', 'insp-nudge');
        const earlier = button('‹ 1s', 'insp-nudge-btn', function () { setEdge(type, edge, value - 1000); });
        earlier.setAttribute('aria-label', `Move the ${word} ${edge} one second earlier`);
        const later = button('1s ›', 'insp-nudge-btn', function () { setEdge(type, edge, value + 1000); });
        later.setAttribute('aria-label', `Move the ${word} ${edge} one second later`);
        nudge.append(earlier, later);
        field.append(label, nudge);
        row.appendChild(field);
        return row;
    }

    function adjustSection(type, dur) {
        const m = state.adjust.model[type];
        const sec = el('div', 'insp-adjust-type');
        sec.dataset.adjust = type;
        const head = el('div', 'insp-adjust-type-head');
        head.append(dot(tone(type)), el('span', '', TYPE_LABELS[type]), el('span', 'insp-mono insp-adjust-range insp-t-' + tone(type), adjustRange(m)));
        sec.appendChild(head);
        const starts = el('div', 'insp-adjust-line');
        starts.append(el('div', 'insp-adjust-label', 'Starts'), edgeRow(type, 'start'));
        sec.appendChild(starts);
        const ends = el('div', 'insp-adjust-line');
        const endBody = el('div', 'insp-adjust-ends');
        if (TO_END_TYPES.indexOf(type) !== -1) {
            const sw = el('label', 'insp-toend');
            const input = el('input');
            input.type = 'checkbox';
            input.id = `inspToEnd-${type}`;
            input.checked = m.toEnd;
            input.addEventListener('change', function () {
                m.toEnd = input.checked;
                if (!m.toEnd && m.end >= dur - END_OF_FILE_MS) m.end = Math.max(m.start + 1000, dur - 10000);
                m.changed = true;
                render();
            });
            sw.append(input, `Runs to the end of the file (${clock(dur)})`);
            endBody.appendChild(sw);
        }
        if (!m.toEnd) endBody.appendChild(edgeRow(type, 'end'));
        ends.append(el('div', 'insp-adjust-label', 'Ends'), endBody);
        sec.appendChild(ends);
        return sec;
    }

    function addRow(type) {
        const row = el('div', 'insp-adjust-note');
        row.dataset.add = type;
        const text = el('div');
        const title = el('div', 'insp-adjust-note-title');
        title.append(dot(tone(type)), `${TYPE_LABELS[type]} · not found`);
        text.append(title, el('div', 'insp-small', `Nothing was found for the ${TYPE_WORDS[type]}. Add one and move its edges to where it really is.`));
        const b = button(`Add ${TYPE_WORDS[type]}`, 'btn insp-btn', function () {
            const dur = duration();
            // Round starting times, never something a source would answer, so they can't be read as a finding
            // (tests/markers/test_api_markers_edit.py keeps a copy of them: change both together).
            if (type === 'intro') state.adjust.model.intro = { start: 0, end: ADD_HEAD_MS, toEnd: false, changed: true };
            else state.adjust.model.credits = { start: Math.max(0, dur - ADD_TAIL_MS), end: dur, toEnd: true, changed: true };
            render();
            if (timeline) timeline.goTo(state.adjust.model[type].start, null, true);
        });
        b.id = `inspAdd-${type}`;
        row.append(text, b);
        return row;
    }

    function cantRow(type) {
        const row = el('div', 'insp-adjust-note');
        row.dataset.cantAdjust = type;
        const text = el('div');
        const title = el('div', 'insp-adjust-note-title');
        title.append(dot(tone(type)), `${TYPE_LABELS[type]} · ${rangeText(decided(type), duration())}`);
        text.append(title, el('div', 'insp-small', `No server with Intro & Credits on for this file shows ${TYPE_PLURALS[type]}, so there is nothing to send it to and it can't be adjusted here.`));
        row.appendChild(text);
        return row;
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

    function adjustPanel() {
        const dur = duration();
        const types = adjustTypes();
        const panel = el('div', 'insp-adjust');
        panel.id = 'inspAdjustPanel';
        const head = el('div', 'insp-adjust-head');
        const title = el('div', 'insp-adjust-title');
        types.forEach(function (t) { title.appendChild(dot(tone(t))); });
        title.appendChild(el('span', '', types.length ? `Adjust ${typePhrase(types)}` : 'Adjust'));
        head.append(title, el('div', 'insp-small', 'Frames from the video, one per second. Click a frame to put the edge there.'));
        panel.appendChild(head);
        types.forEach(function (t) { panel.appendChild(adjustSection(t, dur)); });
        addableTypes().forEach(function (t) { panel.appendChild(addRow(t)); });
        unadjustableTypes().forEach(function (t) { panel.appendChild(cantRow(t)); });

        const foot = el('div', 'insp-adjust-foot');
        const text = el('div');
        const names = receivers(types);
        text.appendChild(el('div', 'insp-adjust-summary', types.length
            ? 'Saving sends your times now and locks them, so later checks keep them. You can go back to automatic any time.'
            : 'Nothing to save yet'));
        const problem = state.adjust.error || adjustProblem();
        if (problem) {
            const p = el('div', 'insp-adjust-problem', problem);
            p.setAttribute('role', 'alert');
            text.appendChild(p);
        }
        const buttons = el('div', 'insp-confirm-actions');
        const cancel = button('Cancel', 'btn insp-btn', cancelAdjust);
        cancel.id = 'inspAdjustCancel';
        const save = button(state.adjust.saving ? 'Sending…' : `Save and send to ${names.length ? joinWith(names, 'and') : 'your servers'}`, 'btn insp-btn-primary', saveAdjust);
        save.id = 'inspAdjustSave';
        save.disabled = state.adjust.saving || !types.length || !!adjustProblem();
        buttons.append(cancel, save);
        foot.append(text, buttons);
        panel.appendChild(foot);
        return panel;
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
            let g = groups.find(function (x) { return Math.abs(x.start - r.start_ms) <= SAME_ANSWER_MS; });
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
        const panel = el('div', 'insp-review');
        panel.dataset.review = type;
        const box = el('div', 'insp-review-box');
        const word = TYPE_WORDS[type];
        const where = `Where ${type === 'credits' ? 'do the credits' : `does the ${word}`} start?`;
        let heading;
        if (found.length >= 2) {
            const spread = found[found.length - 1].start - found[0].start;
            heading = `${where} ${found.length === 2 ? 'Two' : found.length} answers disagree by ${Math.round(spread / 1000)} seconds.`;
        } else if (found.length === 1) {
            heading = `${where} Only one answer came in, and it can't decide on its own.`;
        } else {
            heading = `${where} Nothing found an answer to check.`;
        }
        const names = receivers([type]);
        box.append(el('div', 'insp-review-title', heading),
            el('div', 'insp-review-text', `Pick the frame where the ${word} begin${type === 'credits' ? '' : 's'}. Your choice goes to ${names.length ? joinWith(names, 'and') : 'your servers'} and stays, even when the app checks this ${fileWord()} again.`));
        panel.appendChild(box);
        if (found.length) {
            const grid = el('div', 'insp-grid-2');
            found.forEach(function (c) {
                const card = el('div', 'insp-card insp-candidate');
                card.dataset.candidate = String(c.start);
                card.append(el('div', 'insp-candidate-time insp-mono', clock(c.start)), el('div', 'insp-candidate-text', c.text));
                card.appendChild(exactFramesAround(c.start, { count: EXACT_COUNT, before: 3, ring: c.start }));
                card.appendChild(el('div', 'insp-small', 'Frames one second apart, read from the video. The ringed frame is where this answer starts.'));
                const chosen = rs.selected !== null && edgeRounded(rs.selected) === edgeRounded(c.start);
                const b = button(startsWord(type, c.start), chosen ? 'btn insp-btn-primary' : 'btn insp-btn', function () {
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
        const pick = el('div', 'insp-card insp-pick');
        pick.dataset.pickYourself = type;
        const top = el('div', 'insp-pick-head');
        top.appendChild(withInfo(el('div', 'insp-pick-title', found.length ? 'Neither is right? Pick the frame yourself.' : 'Pick the frame yourself.'), TIPS.pick));
        const nav = el('div', 'd-flex gap-2');
        const back = button('◀︎ 10 s', 'btn insp-btn btn-sm', function () { rs.pickStart = Math.max(0, rs.pickStart - PICK_STEP_MS); render(); });
        back.setAttribute('aria-label', 'Show 10 seconds earlier');
        const fwd = button('10 s ▶︎', 'btn insp-btn btn-sm', function () { rs.pickStart = rs.pickStart + PICK_STEP_MS; render(); });
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
            const bar = el('div', 'insp-confirm-bar is-card');
            bar.dataset.confirm = type;
            const text = el('div');
            text.append(el('div', 'insp-confirm-title', `Selected: ${startsWord(type, rs.selected).toLowerCase()}`),
                el('div', 'insp-small', `Saving sends it to ${names.length ? joinWith(names, 'and') : 'your servers'} now and keeps it through future checks. You can go back to automatic any time.`));
            if (rs.error) {
                const p = el('div', 'insp-adjust-problem', rs.error);
                p.setAttribute('role', 'alert');
                text.appendChild(p);
            }
            const buttons = el('div', 'insp-confirm-actions');
            const notNow = button('Not now', 'btn insp-btn', function () { rs.selected = null; rs.error = ''; render(); });
            const save = button(rs.saving ? 'Sending…' : `Save and send to ${names.length ? joinWith(names, 'and') : 'your servers'}`, 'btn insp-btn-primary', function () { saveReview(type); });
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
        return `${label} ${clock(row.start_ms)} – ${clock(segmentEnd(row, dur))}`;
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
            // "the season audio", "the chapters", but "SkipDB" for a database and "your times" for the user's own.
            const who = (m.decided_by || []).map(function (s) {
                const name = (SOURCES[s] || [s])[0];
                if (s === 'user') return 'your times';
                return /DB$/.test(name) ? name : `the ${name.toLowerCase()}`;
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

    function evidenceRow(source, iconKind, iconName, name, explains, found, note) {
        const line = el('div', 'insp-ev');
        line.dataset.source = source;
        const badge = el('div', 'insp-ev-icon is-' + iconKind);
        badge.appendChild(el('i', 'bi ' + iconName));
        const who = el('div');
        who.append(el('div', 'insp-ev-name', name), el('div', 'insp-small', explains));
        const what = el('div');
        what.append(found, el('div', 'insp-small insp-ev-note', note));
        line.append(badge, who, what);
        return line;
    }

    function lockedRow() {
        const dur = duration();
        const locked = lockedTypes();
        const at = locked.map(function (t) { return decided(t).locked_at; }).filter(Boolean).sort().pop();
        const found = el('div', 'insp-ev-found');
        locked.forEach(function (t, i) {
            if (i) found.append(' · ');
            const m = decided(t);
            found.append(`${TYPE_LABELS[t]} `, el('span', 'insp-mono insp-t-' + tone(t), rangeText(m, dur)));
        });
        const row = evidenceRow('you', 'locked', 'bi-lock-fill', 'You', at ? `Set by you on ${day(at)}` : 'Set by you', found,
            `Locked · later checks keep ${locked.length === 1 && locked[0] !== 'credits' ? 'it' : 'them'}`);
        row.id = 'inspLocked';
        return row;
    }

    function evidenceCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspEvidence';
        card.appendChild(el('div', 'insp-card-heading', 'How it was decided'));
        if (lockedTypes().length) card.appendChild(lockedRow());
        const rows = (state.item && state.item.evidence) || [];
        if (!rows.length) {
            card.appendChild(el('div', 'insp-small insp-empty-note', 'No source answered for this file.'));
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
            let name = info[0];
            let explains = info[1];
            const own = row.source === 'server_markers' || row.source === 'server_markers_imported';
            if (row.source === 'server_markers') {
                name = `${originName(row)}'s own`;
                explains = `Markers ${originName(row)} made itself`;
            } else if (row.source === 'server_markers_imported') {
                name = `${originName(row)}'s imported`;
            }
            const iconName = note.icon === 'used' ? 'bi-check-lg' : note.icon === 'warn' ? 'bi-exclamation-triangle' : 'bi-dash-lg';
            const kind = own && note.icon === 'none' ? 'own' : note.icon;
            card.appendChild(evidenceRow(row.source, kind, iconName, name, explains, el('div', 'insp-ev-found', evidenceFound(row, dur)), note.text));
        });
        return card;
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
            const allOwn = shown.every(function (x) { return !x.marker.ours; });
            const said = Object.keys(byServer).map(function (n) {
                const ms = byServer[n];
                const types = ms.map(function (m) { return m.type; }).filter(function (t, i, all) { return all.indexOf(t) === i; });
                const own = ms.every(function (m) { return !m.ours; });
                return `${n} shows ${ms.length} ${typePhrase(types)} marker${ms.length === 1 ? '' : 's'}${own ? ' of its own' : ''} today`;
            });
            // With no length there are no rows under the strip to draw them in.
            const drawn = duration() ? `, drawn ${allOwn ? 'in grey ' : ''}on the timeline` : '';
            parts.push(`${joinWith(said, 'and')}${drawn}.`);
        } else if (servers().every(function (s) { return Array.isArray(s.current); })) {
            parts.push('No server shows intro or credits markers for this file yet.');
        }
        parts.push(`Checking the ${fileWord()} decides ours.`);
        keep.forEach(function (s) {
            const vendor = VENDOR_NAMES[s.server_type] || serverName(s);
            const others = servers().filter(function (o) { return o !== s && o.markers_enabled; }).map(serverName);
            parts.push(`With “Keep ${vendor}'s markers” on, ${serverName(s)} keeps its own either way${others.length ? `; ${joinWith(others, 'and')} ${others.length === 1 ? 'gets' : 'get'} ours` : ''}.`);
        });
        return parts.join(' ');
    }

    function notCheckedCard() {
        const card = el('div', 'insp-card insp-notchecked');
        card.id = 'inspNotChecked';
        const b = button('Check intro & credits now', 'btn insp-btn-primary align-self-start', redetect);
        b.id = 'inspCheckNow';
        if (state.busy) b.disabled = true;
        card.append(el('div', 'insp-card-heading', 'Not checked by Intro & Credits yet'), el('div', 'insp-notchecked-text', notCheckedText()), b);
        return card;
    }

    function itemErrorCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspItemError';
        card.append(el('div', 'insp-card-heading', 'Intro & Credits couldn\'t be read for this file'), el('div', 'insp-small insp-item-error', state.itemError));
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

    // What a server shows viewers now, one phrase per kind: "Ours · intro 2:45 – 2:59 and credits 24:59 → end".
    function markerPhrase(ms, dur) {
        const byType = [];
        ms.forEach(function (m) {
            let g = byType.find(function (x) { return x.type === m.type; });
            if (!g) { g = { type: m.type, ranges: [] }; byType.push(g); }
            g.ranges.push(rangeText(m, dur) + (m.stale ? ' (made for an earlier file)' : ''));
        });
        return joinWith(byType.map(function (g) { return `${TYPE_WORDS[g.type] || g.type} ${g.ranges.join(', and ')}`; }), 'and');
    }

    function showsLine(s) {
        const dur = duration();
        if (s.error || !Array.isArray(s.current)) return { dot: 'bad', text: 'Couldn\'t read what it shows now', cls: 'is-bad' };
        const sorted = sortedCurrent(s);
        if (!sorted.length) return { dot: s.plan === 'will_add' || s.plan === 'waiting' ? 'wait' : 'none', text: emptyText(s), cls: 'is-muted' };
        const ours = sorted.filter(function (m) { return m.ours; });
        const own = sorted.filter(function (m) { return !m.ours; });
        const parts = [];
        if (ours.length) parts.push(`Ours · ${markerPhrase(ours, dur)}`);
        if (own.length) parts.push(`${ours.length ? 'its' : 'Its'} own · ${markerPhrase(own, dur)}`);
        return { dot: own.length ? 'own' : 'ours', text: parts.join(' · '), cls: '' };
    }

    function serversCard() {
        const card = el('div', 'insp-card');
        card.id = 'inspServers';
        const head = el('div', 'insp-servers-head');
        head.appendChild(el('div', 'insp-card-heading', 'On your servers'));
        if (state.item && servers().length) {
            const need = attention();
            let pill;
            if (!isChecked()) pill = el('span', 'insp-pill is-muted', 'Not checked yet');
            else if (need.length) pill = el('span', 'insp-pill is-warn', `${need.length} need${need.length === 1 ? 's' : ''} attention`);
            else pill = el('span', 'insp-pill is-ok', 'All up to date');
            pill.id = 'inspServersPill';
            head.appendChild(pill);
        }
        card.appendChild(head);
        const previews = (state.file && state.file.previews) || [];
        const ids = [];
        servers().forEach(function (s) { ids.push(s.server_id); });
        previews.forEach(function (p) { if (ids.indexOf(p.server_id) === -1) ids.push(p.server_id); });
        if (!ids.length) {
            card.appendChild(el('div', 'insp-small insp-empty-note', 'No server has this file in a library.'));
            return card;
        }
        const locations = [];
        ids.forEach(function (id) {
            const s = servers().find(function (x) { return x.server_id === id; });
            const p = previews.find(function (x) { return x.server_id === id; });
            const type = String((s && s.server_type) || (p && p.server_type) || '').toLowerCase();
            const name = (s && serverName(s)) || (p && p.server_name) || id;
            const row = el('div', 'insp-server');
            row.dataset.serverId = id;
            row.append(el('div', 'insp-letter insp-letter-' + type, VENDOR_TILES[type] || '?'), el('div', 'insp-server-name', name));
            const lines = el('div', 'insp-server-body');
            if (s) {
                const shows = showsLine(s);
                const line = el('div', 'insp-server-shows ' + shows.cls);
                line.append(dot(shows.dot), el('span', '', shows.text));
                lines.appendChild(line);
                let words = PLAN_WORDS[s.plan] || PLAN_WORDS.unknown;
                if (s.plan === 'up_to_date' && s.publish_status === 'written') words += ' · sent by this app';
                lines.appendChild(el('div', 'insp-server-plan', words));
                if (s.plan_reason) lines.appendChild(el('div', 'insp-server-note', s.plan_reason));
                if (s.error) lines.appendChild(el('div', 'insp-server-note is-bad', s.error));
                if (['failed', 'waiting', 'skipped'].indexOf(s.publish_status) !== -1 && s.publish_message) {
                    lines.appendChild(el('div', 'insp-server-note', s.publish_message));
                }
                if (s.server_type === 'plex' && s.version_count > 1) lines.appendChild(el('div', 'insp-server-note', 'All versions of this item share one set of markers'));
            }
            lines.appendChild(el('div', 'insp-server-preview' + (p && p.error ? ' is-bad' : ''), previewLine(p)));
            if (p && p.path) {
                const what = p.kind === 'trickplay' ? 'Trickplay folder' : (type === 'emby' ? 'Preview (next to the video)' : 'Preview');
                locations.push({ id: id, text: `${name} · ${what}: ${p.path}` });
            }
            row.appendChild(lines);
            card.appendChild(row);
        });
        if (locations.length) {
            const det = el('details', 'insp-locations');
            det.appendChild(el('summary', '', 'File locations'));
            locations.forEach(function (loc) {
                const line = el('div', 'insp-mono', loc.text);
                line.dataset.serverId = loc.id;
                det.appendChild(line);
            });
            card.appendChild(det);
        }
        return card;
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
            resizeTimer = setTimeout(function () {
                if (timeline) timeline.relayout();
                tidyAxes($('inspFile'));
            }, 150);
        });
        tooltips(document.getElementById('inspSearch'));
        loadServers();
        openFromUrl(true);
    }

    window.inspector = { openFile: openFile, state: state };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
