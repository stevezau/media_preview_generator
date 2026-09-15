// =========================================================================
// Preview Inspector → Intro & Credits tab (read-only + Re-detect).
//
// Renders GET /api/markers/item (markers.inspect.item_payload): what was decided for the file, the evidence behind
// it, and what each server shows now and what the next publish changes there. Loaded lazily when the tab is shown
// and cached per file. Every piece of text goes through textContent.
//
// Exposes window.loadMarkersInspector({server_id, item_id, media_file, type}); a null item (a pasted preview path)
// shows how to get a file instead. Depends on app.js globals: apiPost, showToast, getCsrfToken, _initBootstrapTooltips.
// Forwards every item and the resolved canonical path to window.markersSeason (markers_season.js), which owns the
// "This episode" / "Whole season" toggle.
// =========================================================================
(function () {
    'use strict';

    const WINDOW_MS = 180000;
    const MINUTE_MS = 60000;
    // A widened window keeps a little footage around the decision, so its edge isn't the window's edge.
    const LEAD_MS = 30000;
    // A segment ending this close to the end of the file runs "to the end" (same allowance as the backend).
    const END_OF_FILE_MS = 2000;
    // Spec §5.5: intro/recap agree on their end within 5 s, credits/preview on their start within 10 s.
    const START_SEGMENTS = ['intro', 'recap'];
    const TOLERANCE_MS = { intro: 5000, recap: 5000, credits: 10000, preview: 10000 };
    const TYPES = ['intro', 'credits', 'recap', 'preview'];
    const TYPE_LABELS = { intro: 'Intro', credits: 'Credits', recap: 'Recap', preview: 'Preview' };
    // Markers already on a server are shown from each server's live "now" lane, not from stored evidence.
    const SOURCES = [
        ['chapters', 'Chapters'],
        ['theintrodb', 'TheIntroDB'],
        ['introdb', 'IntroDB'],
        ['skipdb', 'SkipDB'],
        ['season_audio', 'Season audio'],
        ['season_audio_previous', 'Previous season audio'],
        ['credits_text', 'Credit text'],
        ['user', 'Your marker'],
    ];
    // Sources whose stored label is a match count ("10/10") worth showing on the bar.
    const COUNTED_SOURCES = ['season_audio', 'season_audio_previous'];
    const PLANS = {
        will_add: ['Will add', 'text-bg-secondary'],
        will_replace: ['Will replace', 'text-bg-warning'],
        will_remove: ['Will remove', 'text-bg-warning'],
        up_to_date: ['Up to date', 'text-bg-success'],
        waiting: ['Waiting', 'text-bg-info'],
        keeps_plex: ['Keeps Plex\'s', 'text-bg-secondary'],
        keeps_emby: ['Keeps Emby\'s', 'text-bg-secondary'],
        unknown: ['Unknown', 'text-bg-secondary'],
        not_enabled: ['Intro & Credits off', 'text-bg-secondary'],
        nothing_to_publish: ['Nothing to send yet', 'text-bg-secondary'],
    };
    const VENDOR_TILES = { plex: 'P', jellyfin: 'J', emby: 'E' };
    const CANT_READ = 'Couldn\'t read what the server shows';

    const cache = new Map();
    let requestSeq = 0;
    let shown = null; // {key, item, payload}

    const $ = function (id) { return document.getElementById(id); };

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function infoIcon(title) {
        const btn = el('button', 'info-icon ms-1');
        btn.type = 'button';
        btn.tabIndex = 0;
        btn.setAttribute('data-bs-toggle', 'tooltip');
        btn.setAttribute('data-bs-placement', 'top');
        btn.title = title;
        btn.appendChild(el('i', 'bi bi-info-circle'));
        return btn;
    }

    function clock(ms) {
        const total = Math.max(0, Math.floor(ms / 1000));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = String(total % 60).padStart(2, '0');
        return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    }

    function segmentEnd(seg, duration) {
        return seg.end_ms === null || seg.end_ms === undefined ? duration : seg.end_ms;
    }

    function toEnd(seg, duration) {
        return !!duration && segmentEnd(seg, duration) >= duration - END_OF_FILE_MS;
    }

    // Timeline text: "0:11–0:37", or "21:39 →" when the segment runs to the end of the file.
    function laneRange(seg, duration) {
        return toEnd(seg, duration) ? `${clock(seg.start_ms)} →` : `${clock(seg.start_ms)}–${clock(segmentEnd(seg, duration))}`;
    }

    // Server card text: "0:11–0:37", or "21:39–end".
    function cardRange(seg, duration) {
        return toEnd(seg, duration) ? `${clock(seg.start_ms)}–end` : `${clock(seg.start_ms)}–${clock(segmentEnd(seg, duration))}`;
    }

    function decidedMarker(payload, type) {
        const d = (payload.decisions || {})[type];
        return d && d.status === 'decided' && d.marker ? d.marker : null;
    }

    function agrees(seg, marker, duration) {
        if (START_SEGMENTS.indexOf(seg.type) !== -1) {
            return Math.abs(segmentEnd(seg, duration) - segmentEnd(marker, duration)) <= TOLERANCE_MS[seg.type];
        }
        return Math.abs(seg.start_ms - marker.start_ms) <= TOLERANCE_MS[seg.type];
    }

    // Two zoom windows: the first and last three minutes, widened to whole minutes when a decision reaches past them
    // (movie credits often start well before the last three minutes). Movies have no opening window.
    function windowsFor(payload) {
        const duration = payload.duration_ms;
        if (!duration) return [];
        const windows = [];
        const segmentFor = function (type) {
            const d = (payload.decisions || {})[type] || {};
            return d.marker || (d.status === 'needs_review' ? d.proposed : null);
        };
        if (!payload.is_movie) {
            let end = Math.min(duration, WINDOW_MS);
            START_SEGMENTS.forEach(function (type) {
                const seg = segmentFor(type);
                if (seg) {
                    const widened = Math.ceil((segmentEnd(seg, duration) + LEAD_MS) / MINUTE_MS) * MINUTE_MS;
                    end = Math.max(end, Math.min(duration, widened));
                }
            });
            windows.push({ id: 'opening', title: 'Opening', start: 0, end: end, types: START_SEGMENTS, endOfFile: false });
        }
        let start = Math.max(0, duration - WINDOW_MS);
        ['credits', 'preview'].forEach(function (type) {
            const seg = segmentFor(type);
            if (seg && seg.start_ms - LEAD_MS < start) {
                start = Math.max(0, Math.floor((seg.start_ms - LEAD_MS) / MINUTE_MS) * MINUTE_MS);
            }
        });
        windows.push({ id: 'ending', title: 'Ending', start: start, end: duration, types: ['credits', 'preview'], endOfFile: true });
        return windows;
    }

    function bar(seg, win, duration, className, label) {
        const len = win.end - win.start;
        const end = segmentEnd(seg, duration);
        if (end <= win.start || seg.start_ms >= win.end) return null;
        const left = Math.max(seg.start_ms, win.start);
        const right = Math.min(end, win.end);
        const node = el('span', 'mk-bar ' + className);
        node.style.left = ((left - win.start) / len * 100).toFixed(3) + '%';
        node.style.width = ((right - left) / len * 100).toFixed(3) + '%';
        const text = (seg.start_ms < win.start ? '← ' : '') + label + (end > win.end ? ' →' : '');
        node.title = text;
        node.appendChild(el('span', 'mk-bar-text', text));
        return node;
    }

    function lane(name, className) {
        const row = el('div', 'mk-lane' + (className ? ' ' + className : ''));
        row.dataset.lane = name;
        const label = el('div', 'mk-lane-label');
        label.appendChild(el('span', 'text-truncate', name));
        const track = el('div', 'mk-track');
        row.append(label, track);
        return { row: row, label: label, track: track };
    }

    // Spec §5.5 rule 7: a server's own marker moved a decided credits/preview start later.
    function shortenedNote(type, shortened) {
        const edge = `${TYPE_LABELS[type].toLowerCase()} start`;
        const servers = shortened.servers || [];
        if (servers.length === 1) return `Shortened to ${servers[0]}'s own ${edge}`;
        if (!servers.length) return `Shortened to a server's own ${edge}`;
        return `Shortened to the servers' own ${edge} (${servers.join(', ')})`;
    }

    function note(track, text) {
        track.appendChild(el('span', 'mk-lane-note', text));
    }

    // One evidence or "now" lane: bars inside the window, a note for segments outside it, ✕ when a segment of a
    // decided type isn't within tolerance of the decision.
    function segmentLane(name, segments, win, payload, emptyText) {
        const duration = payload.duration_ms;
        const l = lane(name);
        const outside = [];
        let disagree = false;
        segments.forEach(function (seg) {
            const marker = decidedMarker(payload, seg.type);
            if (marker && !agrees(seg, marker, duration)) disagree = true;
            const label = laneRange(seg, duration) + (COUNTED_SOURCES.indexOf(seg.source) !== -1 && seg.label ? ' · ' + seg.label : '');
            const node = bar(seg, win, duration, 'mk-bar-evidence', label);
            if (node) l.track.appendChild(node);
            else outside.push(label);
        });
        if (!segments.length) note(l.track, emptyText);
        if (outside.length) note(l.track, outside.join(', ') + ' · outside this view');
        if (disagree) {
            l.row.classList.add('lane-disagree');
            const cross = el('span', 'badge text-bg-danger mk-disagree', '✕');
            cross.title = 'Doesn\'t agree with the decision';
            l.label.appendChild(cross);
        }
        return l.row;
    }

    function decisionLane(win, payload) {
        const duration = payload.duration_ms;
        const l = lane('Decision', 'mk-lane-decision');
        const notes = [];
        win.types.forEach(function (type) {
            const d = (payload.decisions || {})[type] || {};
            const typeLabel = TYPE_LABELS[type];
            if (d.status === 'decided' && d.marker) {
                const node = bar(d.marker, win, duration, 'mk-bar-result', `${typeLabel} ${laneRange(d.marker, duration)}`);
                if (node) l.track.appendChild(node);
            } else if (d.status === 'needs_review' && d.proposed) {
                const seg = Object.assign({ type: type }, d.proposed);
                const node = bar(seg, win, duration, 'mk-bar-proposed', `${typeLabel} ${laneRange(seg, duration)}`);
                if (node) {
                    node.title = `Proposed, needs review: ${node.title}`;
                    l.track.appendChild(node);
                    l.row.classList.add('lane-proposed');
                }
            } else if (d.status === 'needs_review') {
                notes.push(`${typeLabel}: needs review`);
            } else if (d.status === 'no_evidence') {
                notes.push(`${typeLabel}: no markers found`);
            }
        });
        if (notes.length) note(l.track, notes.join(' · '));
        else if (!l.track.children.length) note(l.track, 'Nothing decided');
        return l.row;
    }

    function renderWindow(win, payload) {
        const card = el('div', 'card mk-window');
        card.dataset.window = win.id;
        const header = el('div', 'card-header py-2 d-flex align-items-center');
        const title = `${win.title} · ${clock(win.start)} – ${clock(win.end)}${win.endOfFile ? ' (end of file)' : ''}`;
        header.appendChild(el('span', 'mk-window-title', title));
        header.appendChild(infoIcon('Zoomed to the start or end of the file, so a difference of a second is visible.'));
        const scroll = el('div', 'card-body mk-scroll');
        const zoom = el('div', 'mk-zoom');
        const axis = el('div', 'mk-axis');
        for (let i = 0; i < 4; i++) {
            const tick = el('span', '', clock(win.start + (win.end - win.start) * i / 3));
            tick.style.left = (i * 100 / 3).toFixed(3) + '%';
            axis.appendChild(tick);
        }
        zoom.append(axis, decisionLane(win, payload));

        const evidence = (payload.evidence || []).filter(function (r) { return r.source !== 'server_markers'; });
        const known = SOURCES.map(function (s) { return s[0]; });
        const extra = evidence.map(function (r) { return r.source; })
            .filter(function (s, i, all) { return known.indexOf(s) === -1 && all.indexOf(s) === i; })
            .map(function (s) { return [s, s]; });
        SOURCES.concat(extra).forEach(function (source) {
            const rows = evidence.filter(function (r) { return r.source === source[0]; });
            const typed = rows.filter(function (r) { return r.type; });
            const inWindow = typed.filter(function (r) { return win.types.indexOf(r.type) !== -1; });
            // A lookup that found nothing shows in every window; a source with answers only for the other end
            // of the file stays out of this one.
            if (!inWindow.length && (typed.length || !rows.length)) return;
            const empty = rows.find(function (r) { return !r.type; });
            zoom.appendChild(segmentLane(source[1], inWindow, win, payload, (empty && empty.detail) || 'Nothing found'));
        });

        (payload.servers || []).forEach(function (server) {
            const name = `${server.server_name || server.server_type || 'Server'} now`;
            if (!Array.isArray(server.current)) {
                const l = lane(name);
                note(l.track, CANT_READ);
                zoom.appendChild(l.row);
                return;
            }
            const segments = server.current.filter(function (c) { return win.types.indexOf(c.type) !== -1; });
            zoom.appendChild(segmentLane(name, segments, win, payload, 'none yet'));
        });

        (win.types).forEach(function (type) {
            const d = (payload.decisions || {})[type] || {};
            if (d.status === 'needs_review' && d.reason) {
                zoom.appendChild(el('div', 'mk-window-note small text-muted', `${TYPE_LABELS[type]} needs review: ${d.reason}`));
            } else if (d.status === 'decided' && d.shortened_by) {
                zoom.appendChild(el('div', 'mk-window-note small text-muted', shortenedNote(type, d.shortened_by)));
            }
        });
        scroll.appendChild(zoom);
        card.append(header, scroll);
        return card;
    }

    function renderChips(payload) {
        const chips = el('div', 'mk-chips');
        TYPES.forEach(function (type) {
            const d = (payload.decisions || {})[type] || {};
            const label = TYPE_LABELS[type];
            if (d.status === 'decided' && d.marker) {
                chips.appendChild(el('span', 'badge text-bg-success', `${label} ${laneRange(d.marker, payload.duration_ms)}`));
            } else if (d.status === 'needs_review') {
                chips.appendChild(el('span', 'badge text-bg-warning', `${label}: Needs review`));
            } else if (d.status === 'no_evidence') {
                chips.appendChild(el('span', 'badge text-bg-secondary', `${label}: No markers found`));
            } else if (d.status === 'disabled') {
                // The backend records "detection off" either way; intros and recaps are never looked for in movies.
                const notForMovies = payload.is_movie && START_SEGMENTS.indexOf(type) !== -1;
                chips.appendChild(el('span', 'badge text-bg-secondary opacity-75',
                    `${label}: ${notForMovies ? 'not used for movies' : 'Detection off'}`));
            }
        });
        return chips;
    }

    function serverLines(server, payload) {
        const duration = payload.duration_ms;
        const canShow = server.can_show || [];
        const wanted = TYPES.map(function (type) { return decidedMarker(payload, type); })
            .filter(function (m) { return m && canShow.indexOf(m.type) !== -1; })
            .sort(function (a, b) { return a.start_ms - b.start_ms; });
        const current = Array.isArray(server.current) ? server.current : null;
        const lines = [];
        const listed = function (markers) {
            return markers.map(function (m) { return `${TYPE_LABELS[m.type] || m.type} ${cardRange(m, duration)}`; }).join(' · ');
        };
        if (server.plan === 'will_replace') {
            wanted.forEach(function (m) {
                const now = (current || []).filter(function (c) { return c.type === m.type; });
                const same = now.length === 1 && Math.abs(now[0].start_ms - m.start_ms) <= 1000
                    && Math.abs(segmentEnd(now[0], duration) - segmentEnd(m, duration)) <= 1000;
                if (same) return;
                let from = '(none)';
                if (now.length === 1) from = cardRange(now[0], duration);
                else if (now.length > 1) from = `(${now.length} markers)`;
                lines.push(`${TYPE_LABELS[m.type]} ${from} → ${cardRange(m, duration)}`);
            });
        } else if ((server.plan === 'will_add' || server.plan === 'up_to_date') && wanted.length) {
            lines.push(listed(wanted));
        } else if (server.plan === 'will_remove') {
            lines.push((server.published || []).length ? `Removes ${listed(server.published)}` : 'Removes the markers this app sent');
        }
        if (server.plan_reason) lines.push(server.plan_reason);
        if (current === null && !server.error) lines.push(CANT_READ);
        // Owner decision R1: Emby always gets the decided credits start, even one that ends before the file does —
        // never hidden here. Whether that credits marker runs to the true end (and what that means for viewers) is
        // the backend's call (markers.inspect._plan / publishers.emby.credits_note), carried in plan_reason above;
        // no copy of that wording lives here, so the two can't drift apart.
        if (server.server_type === 'emby' && wanted.some(function (m) { return m.type === 'credits'; })) {
            lines.push('Emby has no “credits end”');
        }
        // Only a Plex item with several versions: the note means nothing on a single file.
        if (server.server_type === 'plex' && server.version_count > 1) {
            lines.push('All versions of this item share one set of markers');
        }
        return lines;
    }

    function renderServers(payload) {
        const card = el('div', 'card mk-servers');
        const header = el('div', 'card-header py-2 d-flex align-items-center');
        header.appendChild(el('span', 'mk-window-title', 'On your servers'));
        header.appendChild(infoIcon('What each server shows viewers today, and what the next Intro & Credits job changes there.'));
        const bodyEl = el('div', 'card-body');
        const grid = el('div', 'mk-servers-grid');
        const servers = payload.servers || [];
        if (!servers.length) {
            bodyEl.appendChild(el('div', 'small text-muted', 'No server has this file in a library.'));
        }
        servers.forEach(function (server) {
            const stype = String(server.server_type || '').toLowerCase();
            const box = el('div', 'mk-server');
            box.dataset.serverId = server.server_id || '';
            const top = el('div', 'd-flex align-items-center gap-2 flex-wrap');
            top.appendChild(el('span', 'mk-tile mk-tile-' + stype, VENDOR_TILES[stype] || '?'));
            top.appendChild(el('strong', 'text-break', server.server_name || stype || 'Server'));
            const plan = PLANS[server.plan] || [server.plan || 'Unknown', 'text-bg-secondary'];
            top.appendChild(el('span', 'badge mk-plan ' + plan[1], plan[0]));
            box.appendChild(top);
            serverLines(server, payload).forEach(function (line) {
                box.appendChild(el('div', 'small text-muted mk-line', line));
            });
            const failed = server.publish_status === 'failed';
            if (['failed', 'waiting', 'skipped'].indexOf(server.publish_status) !== -1 && server.publish_message) {
                box.appendChild(el('div', 'small mk-line ' + (failed ? 'text-danger-emphasis' : 'text-muted'), server.publish_message));
            }
            if (server.error) box.appendChild(el('div', 'small text-warning-emphasis mk-error', server.error));
            grid.appendChild(box);
        });
        bodyEl.appendChild(grid);
        card.append(header, bodyEl);
        return card;
    }

    // Labels wider than their bar sit beside it in the page's text colour, so they stay readable on the track.
    function placeBarLabels(root) {
        root.querySelectorAll('.mk-bar').forEach(function (node) {
            const text = node.querySelector('.mk-bar-text');
            // clientWidth includes the bar's 5px side padding; the text doesn't get that room.
            if (!text || text.scrollWidth <= node.clientWidth - 10) return;
            const track = node.parentElement;
            const roomRight = track.clientWidth - (node.offsetLeft + node.offsetWidth);
            node.classList.add(roomRight >= text.scrollWidth + 6 || roomRight >= node.offsetLeft ? 'mk-bar-out-right' : 'mk-bar-out-left');
        });
    }

    function render(payload, item) {
        const body = $('markersInspectorBody');
        $('markersInspectorPath').textContent = payload.canonical_path || item.media_file || '';
        if (window.markersSeason) window.markersSeason.setPath(payload.canonical_path || item.media_file || '');
        $('markersRedetectBtn').disabled = !(payload.canonical_path || item.media_file);
        const parts = [];
        if (!payload.known) {
            const box = el('div', 'alert alert-secondary py-2 mk-not-checked');
            box.appendChild(el('strong', '', 'Not checked yet'));
            box.appendChild(el('div', 'small', 'No Intro & Credits job has looked at this file. Re-detect checks it now.'));
            parts.push(box);
        } else {
            parts.push(renderChips(payload));
            if (!payload.duration_ms) {
                parts.push(el('div', 'small text-muted mb-3', 'The file\'s length isn\'t known yet, so there is no timeline.'));
            }
            windowsFor(payload).forEach(function (win) { parts.push(renderWindow(win, payload)); });
        }
        parts.push(renderServers(payload));
        body.replaceChildren.apply(body, parts);
        placeBarLabels(body);
        // On a narrow screen the ending window scrolls; start it at the end of the file, where its markers are.
        body.querySelectorAll('.mk-window[data-window="ending"] .mk-scroll').forEach(function (scroll) {
            scroll.scrollLeft = scroll.scrollWidth;
        });
        if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips(body);
    }

    function keyFor(item) {
        return item ? [item.server_id || '', item.item_id || '', item.media_file || ''].join('|') : '';
    }

    // By path when the search gave one (a Plex item's versions are different files), else by server + item id;
    // a path this app can't place (400) is tried again by id.
    async function fetchItem(item) {
        const queries = [];
        if (item.media_file) queries.push('path=' + encodeURIComponent(item.media_file));
        if (item.server_id && item.item_id) {
            queries.push('server_id=' + encodeURIComponent(item.server_id) + '&item_id=' + encodeURIComponent(item.item_id));
        }
        let lastError = null;
        for (const query of queries) {
            const resp = await fetch('/api/markers/item?' + query);
            if (resp.status === 401) {
                window.location.href = '/login';
                throw new Error('Authentication required');
            }
            const data = await resp.json().catch(function () { return {}; });
            if (resp.ok) return data;
            lastError = new Error((data && data.error) || `HTTP ${resp.status}`);
            if (resp.status !== 400) break;
        }
        throw lastError || new Error('Nothing to look up');
    }

    async function loadMarkersInspector(item) {
        if (window.markersSeason) window.markersSeason.setItem(item);
        const body = $('markersInspectorBody');
        const button = $('markersRedetectBtn');
        if (!body || !button) return;
        const key = keyFor(item);
        const seq = ++requestSeq;
        if (!item || !(item.media_file || (item.server_id && item.item_id))) {
            shown = null;
            $('markersInspectorPath').textContent = '';
            button.disabled = true;
            body.replaceChildren(el('div', 'alert alert-secondary py-2', 'Search for the title above to see its Intro & Credits. A pasted preview path doesn\'t say which video file it belongs to.'));
            return;
        }
        if (cache.has(key)) {
            shown = { key: key, item: item, payload: cache.get(key) };
            render(shown.payload, item);
            return;
        }
        shown = { key: key, item: item, payload: null };
        $('markersInspectorPath').textContent = item.media_file || '';
        button.disabled = true;
        body.replaceChildren(el('div', 'text-muted small py-3', 'Loading Intro & Credits…'));
        try {
            const payload = await fetchItem(item);
            cache.set(key, payload);
            if (seq !== requestSeq) return;
            shown.payload = payload;
            render(payload, item);
        } catch (error) {
            if (seq !== requestSeq) return;
            body.replaceChildren(el('div', 'alert alert-warning py-2', `Couldn't load Intro & Credits for this file: ${error.message}`));
            button.disabled = !item.media_file;
        }
    }

    async function redetect() {
        if (!shown) return;
        const path = (shown.payload && shown.payload.canonical_path) || shown.item.media_file;
        if (!path) return;
        const button = $('markersRedetectBtn');
        button.disabled = true;
        try {
            const result = await apiPost('/api/markers/item/redetect', { path: path });
            // The job changes what this file shows once it runs: load it fresh next time.
            cache.delete(shown.key);
            showToast('Re-detect', 'Queued — see the Dashboard', 'success');
            const jobId = result && result.job_id;
            const toastBody = $('toastBody');
            if (jobId && toastBody) {
                const link = el('a', 'ms-1', String(jobId).substring(0, 8));
                link.href = '/?job=' + encodeURIComponent(jobId);
                toastBody.append(' ', link);
            }
        } catch (error) {
            showToast('Re-detect', `Couldn't queue it: ${error.message}`, 'danger');
        } finally {
            button.disabled = false;
        }
    }

    function wireRedetect() {
        const button = $('markersRedetectBtn');
        if (button) button.addEventListener('click', redetect);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wireRedetect);
    else wireRedetect();

    window.loadMarkersInspector = loadMarkersInspector;
})();
