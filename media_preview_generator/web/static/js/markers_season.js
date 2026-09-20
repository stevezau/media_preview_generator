// =========================================================================
// Preview Inspector → Intro & Credits → "Whole season".
//
// Renders GET /api/markers/season (markers.inspect.season_payload): every episode of the season folder with its
// decisions, evidence chips and one dot per server, and "Publish" (POST /api/markers/season/publish, a NORMAL
// job of exactly these episodes; an identical pending/running job is reused and its id is returned either way, so
// the toast always just says "Queued"). Loaded only when "Whole season" is picked, then kept per path; picking
// another episode while it's open reloads that episode's season instead of dropping back to "This episode".
// Every row has Edit, which shows that episode's own tab and opens markers_inspector.js's Adjust editor on it
// (window.markersEditor.openFile); the editor calls forgetFile() back when a save, lock or unlock lands, so the
// next look at the season re-reads it rather than rendering the times that edit has just replaced.
// markers_inspector.js calls setItem(item) for every item and setPath(canonical path) once the item's data
// arrives — setPath corrects a season already fetched under the item's media_file (used as a placeholder path
// until the canonical one is known) if the two differ, and remembers the canonical path for that media_file so the
// item shown again (the tab re-shown) asks for it straight away. Text goes through textContent. Depends on app.js
// globals: apiPost, showToast, _initBootstrapTooltips.
// =========================================================================
(function () {
    'use strict';

    // A segment ending this close to the end of the file runs "to the end" (same allowance as the backend).
    const END_OF_FILE_MS = 2000;
    // Matches markers.audio.season.MAX_GROUP_EPISODES: a folder with more episodes is capped to the nearest this many.
    const MAX_GROUP_EPISODES = 40;
    const CHIP_NAMES = {
        chapters: 'Chapters',
        theintrodb: 'TheIntroDB',
        introdb: 'IntroDB',
        skipdb: 'SkipDB',
        season_audio: 'Audio',
        season_audio_previous: 'Previous season',
        credits_text: 'Credit text',
        user: 'Your marker',
    };
    const DOT_WORDS = {
        ok: 'shows our markers',
        waiting: 'waiting',
        failed: 'failed',
        skipped: 'skipped',
        none: 'nothing sent yet',
        off: 'Intro & Credits is off',
    };
    // Every state without its own colour (off, none, skipped) is the plain grey .mk-dot.
    const LEGEND = 'Dots: green = server shows this marker, amber = waiting, red = failed, grey = server not enabled, skipped or nothing sent yet';
    const EDIT_TIP = 'Adjust this episode\'s intro and credits.';

    const cache = new Map();
    // media_file → the canonical path setPath reported for it (a file's canonical path doesn't change).
    const resolved = new Map();
    let item = null;
    let path = '';
    let seq = 0;

    const $ = function (id) { return document.getElementById(id); };

    // The season path a load/publish should use: the item's own canonical path once render() has reported it
    // (setPath, now or the last time this media_file was shown), else the search result's media_file as a
    // placeholder until that arrives.
    function askedPath() {
        const mediaFile = (item && item.media_file) || '';
        return path || resolved.get(mediaFile) || mediaFile;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function clock(ms) {
        const total = Math.max(0, Math.floor(ms / 1000));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = String(total % 60).padStart(2, '0');
        return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    }

    function range(marker, duration) {
        const end = marker.end_ms === null || marker.end_ms === undefined ? duration : marker.end_ms;
        if (duration && end >= duration - END_OF_FILE_MS) return `${clock(marker.start_ms)} →`;
        return `${clock(marker.start_ms)} – ${clock(end)}`;
    }

    function typeCell(decision, duration) {
        const td = el('td', 'mk-season-time');
        const d = decision || {};
        if (d.status === 'decided' && d.marker) td.textContent = range(d.marker, duration);
        else if (d.status === 'needs_review') td.appendChild(el('span', 'badge text-bg-warning', 'Needs review'));
        else if (d.status === 'disabled') td.appendChild(el('span', 'text-muted', 'Off'));
        else if (d.status === 'no_evidence') td.appendChild(el('span', 'text-muted', '—'));
        else td.appendChild(el('span', 'text-muted', 'Not checked yet'));
        return td;
    }

    function chipsCell(episode) {
        const td = el('td');
        (episode.evidence || []).forEach(function (chip) {
            const name = CHIP_NAMES[chip.source] || chip.source;
            td.appendChild(el('span', 'badge mk-chip', chip.label ? `${name} ${chip.label}` : name));
        });
        const locked = ['intro', 'credits'].some(function (t) { return episode[t] && episode[t].marker && episode[t].marker.locked; });
        if (locked) td.appendChild(el('span', 'badge mk-chip mk-chip-locked', '🔒 Locked by you'));
        return td;
    }

    function dotsCell(episode, servers) {
        const td = el('td');
        const dots = el('div', 'mk-dots');
        servers.forEach(function (server) {
            const state = (episode.servers || {})[server.server_id] || { state: 'none', message: '' };
            const dot = el('span', 'mk-dot mk-dot-' + state.state);
            dot.title = `${server.server_name}: ${state.message || DOT_WORDS[state.state] || state.state}`;
            // A title alone isn't read out on a span; the dot is otherwise empty.
            dot.setAttribute('role', 'img');
            dot.setAttribute('aria-label', dot.title);
            dots.appendChild(dot);
        });
        td.appendChild(dots);
        return td;
    }

    function rowButton(text, tip) {
        const button = el('button', 'btn btn-sm btn-outline-secondary py-0', text);
        button.type = 'button';
        if (tip) {
            button.setAttribute('data-bs-toggle', 'tooltip');
            button.setAttribute('data-bs-placement', 'top');
            button.title = tip;
        }
        return button;
    }

    // Edit is on every row, including one nothing was decided for: whether a file can be adjusted at all depends on
    // what each server can show, which the season payload doesn't carry — the episode's own tab answers that, and
    // says why when the answer is no. Nothing here says "published": the row's dots carry that, per server.
    function actionCell(episode) {
        const td = el('td', 'mk-season-action');
        const edit = rowButton('Edit', EDIT_TIP);
        edit.addEventListener('click', function () { openEpisode(episode.path, true); });
        // Any type in Needs review, recap and preview included (they have no column), as counts.needs_review counts.
        if (!episode.needs_review) {
            td.appendChild(edit);
            return td;
        }
        // e.g. ruling G3: season audio and a server's own marker only agree because both come from matching audio.
        // Shown verbatim, never paraphrased.
        const review = rowButton('Review', episode.review_reason);
        review.addEventListener('click', function () { openEpisode(episode.path); });
        const pair = el('div', 'd-flex gap-1');
        pair.append(review, edit);
        td.appendChild(pair);
        return td;
    }

    function render(payload) {
        const body = $('markersSeasonBody');
        const servers = payload.servers || [];
        const episodes = payload.episodes || [];
        const counts = payload.counts || {};
        const on = servers.filter(function (s) { return s.markers_enabled; }).length;

        const head = el('div', 'd-flex justify-content-between align-items-center flex-wrap gap-2 mb-2');
        const titles = el('div');
        const show = String(payload.show || '').replace(/\s*\{[a-z]+-[^}]*\}/gi, '').trim();
        titles.appendChild(el('div', 'mk-season-title', `${show} · ${payload.season || ''}`));
        // total_episodes is the season's size before the cap; only a bigger season says it's showing part of it.
        const total = counts.total_episodes || counts.episodes || 0;
        const subText = total > MAX_GROUP_EPISODES
            ? `${total} episodes (showing the ${MAX_GROUP_EPISODES} nearest)`
            : `${total} episodes`;
        titles.appendChild(el('div', 'mk-season-sub text-muted small', subText));
        const actions = el('div', 'd-flex align-items-center gap-2');
        actions.appendChild(el('span', 'badge text-bg-success mk-season-ready', `${counts.ready || 0} ready`));
        if (counts.needs_review) actions.appendChild(el('span', 'badge text-bg-warning mk-season-review', `${counts.needs_review} need review`));
        const publish = el('button', 'btn btn-sm btn-primary', `Publish ${counts.ready || 0} to ${on} server${on === 1 ? '' : 's'}`);
        publish.type = 'button';
        publish.id = 'markersSeasonPublishBtn';
        publish.disabled = !counts.ready || !on;
        publish.title = 'Runs Intro & Credits for this season as a job: decided episodes go to every server that doesn\'t show them yet, the rest are checked again.';
        publish.addEventListener('click', function () { publishSeason(publish); });
        actions.appendChild(publish);
        head.append(titles, actions);

        const card = el('div', 'card');
        const scroll = el('div', 'table-responsive');
        const table = el('table', 'table table-sm align-middle mb-0 mk-season-table');
        const headRow = el('tr');
        ['Ep', 'Intro', 'Credits', 'Evidence'].forEach(function (label) { headRow.appendChild(el('th', '', label)); });
        headRow.appendChild(el('th', 'mk-season-servers', servers.map(function (s) { return s.server_name; }).join(' · ')));
        headRow.appendChild(el('th'));
        const thead = el('thead');
        thead.appendChild(headRow);
        const tbody = el('tbody');
        episodes.forEach(function (episode) {
            const row = el('tr');
            row.dataset.episode = episode.episode || episode.name;
            row.appendChild(el('td', 'mk-season-time', episode.episode || episode.name));
            row.append(
                typeCell(episode.intro, episode.duration_ms),
                typeCell(episode.credits, episode.duration_ms),
                chipsCell(episode),
                dotsCell(episode, servers),
                actionCell(episode),
            );
            tbody.appendChild(row);
        });
        table.append(thead, tbody);
        scroll.appendChild(table);
        card.appendChild(scroll);
        body.replaceChildren(head, card, el('div', 'mk-season-legend text-muted small mt-1', LEGEND));
        if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips(body);
    }

    async function load() {
        const body = $('markersSeasonBody');
        const asked = askedPath();
        if (!asked) return;
        const mine = ++seq;
        if (cache.has(asked)) {
            render(cache.get(asked));
            return;
        }
        body.replaceChildren(el('div', 'text-muted small py-3', 'Loading the season…'));
        try {
            const resp = await fetch('/api/markers/season?path=' + encodeURIComponent(asked));
            if (resp.status === 401) {
                window.location.href = '/login';
                return;
            }
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
            cache.set(asked, data);
            if (mine === seq) render(data);
        } catch (error) {
            if (mine === seq) body.replaceChildren(el('div', 'alert alert-warning py-2', `Couldn't load this season: ${error.message}`));
        }
    }

    async function publishSeason(button) {
        const asked = askedPath();
        button.disabled = true;
        try {
            const result = await apiPost('/api/markers/season/publish', { path: asked });
            // The job changes what the season shows once it runs: load it fresh next time.
            cache.clear();
            showToast('Publish season', 'Queued — see the Dashboard', 'success');
            const jobId = result && result.job_id;
            const toastBody = $('toastBody');
            if (jobId && toastBody) {
                const link = el('a', 'ms-1', String(jobId).substring(0, 8));
                link.href = '/?job=' + encodeURIComponent(jobId);
                toastBody.append(' ', link);
            }
        } catch (error) {
            showToast('Publish season', `Couldn't queue it: ${error.message}`, 'danger');
        } finally {
            button.disabled = false;
        }
    }

    function showView(season) {
        $('markersViewEpisode').checked = !season;
        $('markersViewSeason').checked = season;
        $('markersEpisodeView').hidden = season;
        $('markersSeasonBody').hidden = !season;
        if (season) load();
    }

    function openEpisode(episodePath, edit) {
        showView(false);
        const next = { server_id: '', item_id: '', media_file: episodePath, type: 'episode' };
        if (edit) window.markersEditor.openFile(next);
        else window.loadMarkersInspector(next);
    }

    // One episode was saved, locked or unlocked: every season held in the cache that lists it now shows times,
    // chips or dots that edit has replaced. A season is cached under the path it was asked for — any one of its
    // episodes — so the whole cache is checked, not just that path's entry (which lists it too).
    function forgetFile(episodePath) {
        if (!episodePath) return;
        cache.forEach(function (payload, key) {
            const lists = (payload.episodes || []).some(function (episode) { return episode.path === episodePath; });
            if (lists) cache.delete(key);
        });
    }

    function setItem(next) {
        const toggle = $('markersViewToggle');
        const seasonRadio = $('markersViewSeason');
        const isEpisode = !!(next && next.type === 'episode' && next.media_file);
        // Another episode while "Whole season" is open stays there, showing its season; anything else (a movie,
        // or nothing) falls back to "This episode" — there is no season to show.
        const keepSeason = isEpisode && !!(toggle && !toggle.hidden && seasonRadio && seasonRadio.checked);
        item = next;
        path = '';
        seq++;
        if (!toggle) return;
        toggle.hidden = !isEpisode;
        if (keepSeason) load();
        else showView(false);
    }

    function setPath(next) {
        const value = next || '';
        const before = askedPath();
        path = value;
        if (value && item && item.media_file) resolved.set(item.media_file, value);
        const after = askedPath();
        if (after === before) return;
        // render() resolved the item's real path after "Whole season" already loaded (or started loading) the
        // placeholder media_file: bumping seq drops that response if it's still in flight, and the reload shows the
        // real path's season in place of whatever the placeholder rendered.
        seq++;
        const body = $('markersSeasonBody');
        if (body && !body.hidden) load();
    }

    function wire() {
        const season = $('markersViewSeason');
        const episode = $('markersViewEpisode');
        if (!season || !episode) return;
        season.addEventListener('change', function () { if (season.checked) showView(true); });
        episode.addEventListener('change', function () { if (episode.checked) showView(false); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();

    window.markersSeason = { setItem: setItem, setPath: setPath, forgetFile: forgetFile };
})();
