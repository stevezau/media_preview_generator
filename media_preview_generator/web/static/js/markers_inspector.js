// =========================================================================
// Preview Inspector → Intro & Credits tab (read-only, Re-detect, and the Adjust / Lock editor).
//
// Renders GET /api/markers/item (markers.inspect.item_payload): what was decided for the file, the evidence behind
// it, and what each server shows now and what the next publish changes there. Loaded lazily when the tab is shown
// and cached per file. Every piece of text goes through textContent.
//
// Adjust opens the editor over the same two zoom windows: the Decision bar grows a drag handle at each edge, each
// edited type gets a strip with typed times, and a type nothing was found for carries an Add button where its bar
// would be, which seeds a deliberately round starting time (addSeed) you then drag. One action bar saves the lot
// through POST /api/markers/item/markers (save = lock = publish to every owner, plan ruling P-R3) and shows what
// each server did. Lock sends the decided times through that same endpoint and Unlock is
// DELETE /api/markers/item/markers; both say what they will do and ask first. Copy is lifted verbatim from the
// owner-approved pack,
// docs/design/intro-credits/evidence/design/phase4/ui-copy.md.
//
// A job publishes markers behind the tab's back, so the file on screen is read again whenever an Intro & Credits job
// finishes on the /jobs socket, and Re-detect drops what it holds straight away.
//
// Exposes window.loadMarkersInspector({server_id, item_id, media_file, type}); a null item (a pasted preview path)
// shows how to get a file instead. Depends on app.js globals: apiPost, showToast, getCsrfToken,
// _initBootstrapTooltips, _disposeBootstrapTooltips, on window.bootstrap (Modal for the Lock and Unlock
// confirmations, Tooltip for the header buttons) and on window.io (the /jobs socket).
// Forwards every item and the resolved canonical path to window.markersSeason (markers_season.js), which owns the
// "This episode" / "Whole season" toggle, and tells it to forget a file whose markers a save, lock or unlock has
// just changed. window.markersEditor.openFile is that view's Edit: load a file's tab and open the editor on it.
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
    const VENDOR_NAMES = { plex: 'Plex', jellyfin: 'Jellyfin', emby: 'Emby' };
    const CANT_READ = 'Couldn\'t read what the server shows';

    // --- editor constants -------------------------------------------------
    // Only these two types can be told to run to the end of the file; an intro or recap that does is a mis-detection.
    const TO_END_TYPES = ['credits', 'preview'];
    const NUDGE_MS = 1000;
    const NUDGE_SHIFT_MS = 10000;
    // A drag can't push an edge past the other one; a typed time can, and then the editor refuses to save.
    const MIN_LENGTH_MS = 1000;
    // The bounds markers.decide applies to a *source*. A user's own marker keeps only "inside the file" and "ends
    // after it starts" (plan ruling P-R2), so these only ever produce a warning here.
    const USUAL_MIN_INTRO_MS = 3000;
    const USUAL_MAX_INTRO_MS = 300000;
    const USUAL_START_PERCENT = 35; // intro/recap start within the first 35% of the file
    const USUAL_END_PERCENT = 75; // credits/preview start in the last 25%
    const TYPE_WORDS = { intro: 'intro', credits: 'credits', recap: 'recap', preview: 'preview' };
    const TYPE_PLURALS = { intro: 'intros', credits: 'credits', recap: 'recaps', preview: 'previews' };
    // What POST /api/markers/item/markers calls each per-server outcome (api_markers._EDITOR_RESULTS).
    const RESULTS = {
        written: ['Updated', 'text-bg-success'],
        unchanged: ['Up to date', 'text-bg-success'],
        waiting: ['Waiting', 'text-bg-info'],
        failed: ['Couldn\'t reach it', 'text-bg-danger'],
        not_enabled: ['Intro & Credits off', 'text-bg-secondary'],
        nothing_to_publish: ['Nothing sent', 'text-bg-secondary'],
        needs_review: ['Needs review', 'text-bg-warning'],
    };
    const RESULT_LINES = {
        failed: ['Your times are saved. This server gets them at the next Check servers run.', 'text-danger-emphasis'],
        not_enabled: ['Turn on Intro & Credits for this server to send markers here.', 'text-muted'],
    };
    const LOCK_TIP = 'Keep these times exactly as they are. Later checks won\'t change them.';
    const UNLOCK_TIP = 'Let later checks set these times again.';
    // --- adding a marker by hand (built 2026-09-21 to the answers relayed with the go-ahead) ---
    const ADD_TIP = 'Puts a marker on the timeline at a starting time. Drag it to where it really is, then save.';
    const REMOVE_TIP = 'Take this one back out. Nothing has been saved yet.';
    const STARTING_TIMES = 'Starting times, not something we found — drag them to where they really are.';
    const NOTHING_TO_SAVE = 'Nothing to save yet';
    // The times a hand-added marker starts from. Round on purpose: the audio and text detectors practically never
    // answer a whole 30 seconds from 0:00 or exactly the last minute, so a starting time can't be read as something
    // this app found. Near enough the truth that the drag is short, and inside the bounds warningsFor() checks, so
    // the editor opens with no warning already showing.
    const ADD_HEAD_MS = 30000;
    const ADD_TAIL_MS = { credits: 60000, preview: 30000 };
    // The Season view's Edit reaches a file the editor can't open at all: one whose length isn't known, so there is
    // no timeline to put a marker on. That is a file no job has looked at, or one a job looked at but couldn't read
    // a duration for. A file merely missing an intro or credits opens on its Add buttons.
    const NOTHING_TO_ADJUST = 'Nothing to adjust on this episode yet — Re-detect checks the file now.';
    const NOTHING_TO_LOCK = 'Nothing to lock on this episode yet — Re-detect checks the file now.';
    const NO_FILE_YET = 'No file is open in this tab yet.';
    const STILL_LOADING = 'This file is still loading.';
    const COULDNT_READ = 'This file\'s Intro & Credits couldn\'t be read.';
    const REDETECT_QUEUEING = 'This file is on its way to the queue.';
    const NO_OWNER_TO_LOCK = 'No server with Intro & Credits on has this file, so there is nothing to lock.';
    const EDIT_IN_HAND = 'Save or cancel the times you are adjusting first.';
    const LOCK_IN_FLIGHT = 'Your last change is still on its way to your servers.';
    const NOT_A_TIME = 'That isn\'t a time. Try 1:23 or 0:14.';
    // Only an Intro & Credits job changes what this tab shows (media_preview_generator/job_kinds.py).
    const JOB_KIND_MARKERS = 'intro_credits';

    const cache = new Map();
    // The /jobs socket, opened the first time a file is shown in the tab: a page that never opens it never connects.
    let jobsSocket = null;
    let requestSeq = 0;
    let shown = null; // {key, item, payload}
    // The open edit: {types, model: {type: {start, end, toEnd, editable, badStart, badEnd}}, saving, ui, actions}.
    let editing = null;
    // What the open Lock dialog listed: {key, path, types, model}. Confirm sends this, not whatever has arrived since.
    let pendingLock = null;
    // The answer to the last save: {servers, markers, sent}. Cleared whenever another file or a fresh payload loads.
    let results = null;
    // A Lock is in flight: it publishes to every server, so the header's other actions wait for its answer.
    let busy = false;
    // A Re-detect is on its way to the queue. Held here rather than on the button, which every render rewrites.
    let queueing = false;

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
        if (!editing && win.types.some(function (type) { return isLocked(payload, type); })) {
            const lock = el('i', 'bi bi-lock-fill mk-lock');
            lock.setAttribute('data-bs-toggle', 'tooltip');
            lock.setAttribute('data-bs-placement', 'top');
            lock.title = 'You set these times. Later checks won\'t change them.';
            l.label.appendChild(lock);
        }
        win.types.forEach(function (type) {
            const d = (payload.decisions || {})[type] || {};
            const typeLabel = TYPE_LABELS[type];
            // While editing, a type with no bar of its own says nothing here: its Add sits on the row below.
            if (editing && !editing.model[type]) {
                return;
            } else if (editing && editing.model[type]) {
                l.track.appendChild(editBar(type, win, payload));
            } else if (d.status === 'decided' && d.marker) {
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
        // While editing, an empty track is the point — the Add row under it says what to do about it.
        else if (!editing && !l.track.children.length) note(l.track, 'Nothing decided');
        return l.row;
    }

    // The offer to make a marker by hand, on its own row directly under the Decision lane and aligned with its
    // track. It can't live *in* the track: a bar that reaches the left edge puts its grab handle (z-index 3) over
    // the button and takes the click.
    // A type no server with Intro & Credits on can show stays on screen, disabled, saying why — the same answer
    // Task 5 gives an adjustable one.
    function addButtons(types, payload) {
        const wrap = el('div', 'mk-adds');
        types.forEach(function (type) {
            const reachable = vendorsFor(payload, type).shown.length > 0;
            const button = el('button', 'mk-add');
            button.type = 'button';
            button.dataset.addType = type;
            button.disabled = editing.saving || !reachable;
            button.setAttribute('data-bs-toggle', 'tooltip');
            button.setAttribute('data-bs-placement', 'top');
            button.title = reachable ? ADD_TIP : reachNote(type, payload);
            button.appendChild(el('i', 'bi bi-plus-lg me-1'));
            button.appendChild(el('span', '', `Add ${TYPE_WORDS[type]}`));
            button.addEventListener('click', function () { addType(type); });
            wrap.appendChild(button);
        });
        return wrap;
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
        if (editing) {
            const adds = win.types.filter(function (type) { return !editing.model[type]; });
            if (adds.length) zoom.appendChild(addButtons(adds, payload));
            win.types.forEach(function (type) {
                if (editing.model[type]) zoom.appendChild(editStrip(type, payload));
            });
        }

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
        // A type that could be added but has nowhere to go: its Add button is disabled, so the reason lives here,
        // where it has room to wrap. A type already in the edit says it in its own strip instead.
        if (editing) {
            win.types.forEach(function (type) {
                if (editing.model[type] || vendorsFor(payload, type).shown.length) return;
                const line = el('div', 'mk-window-note small text-muted');
                line.appendChild(el('i', 'bi bi-slash-circle me-1'));
                line.appendChild(el('span', '', reachNote(type, payload)));
                zoom.appendChild(line);
            });
        }
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
                // The rules record "detection off", also for intros and recaps, which are never looked for in movies.
                // A job that didn't read the file for a type every server keeps its own of says so instead ("kept
                // Plex's own marker", markers.outcomes.kept_own_reason).
                const notForMovies = payload.is_movie && START_SEGMENTS.indexOf(type) !== -1;
                const keptOwn = d.reason && d.reason !== 'detection off' ? capitalise(d.reason) : '';
                chips.appendChild(el('span', 'badge text-bg-secondary opacity-75',
                    `${label}: ${keptOwn || (notForMovies ? 'not used for movies' : 'Detection off')}`));
            }
        });
        // The same chip the Season view shows for a locked row (markers_season.js), so the two read alike.
        if (TYPES.some(function (type) { return isLocked(payload, type); })) {
            const chip = el('span', 'badge mk-chip mk-chip-locked');
            chip.appendChild(el('i', 'bi bi-lock-fill me-1'));
            chip.appendChild(el('span', '', 'Locked by you'));
            chips.appendChild(chip);
        }
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
            box.appendChild(top);
            const row = resultFor(server.server_id);
            if (row) {
                const badge = resultBadge(row);
                const chip = el('span', 'badge mk-plan ' + badge[1], badge[0]);
                // The publisher's own words stay reachable without putting job wording on the card.
                if (row.message) chip.title = row.message;
                top.appendChild(chip);
                resultLines(row, payload).forEach(function (line) {
                    box.appendChild(el('div', 'small mk-line ' + line[1], line[0]));
                });
                grid.appendChild(box);
                return;
            }
            if (editing && editing.saving && willReceive(server)) {
                const chip = el('span', 'badge mk-plan text-bg-secondary');
                chip.appendChild(sendingSpinner());
                chip.appendChild(el('span', '', 'Sending…'));
                top.appendChild(chip);
                const listed = pendingFor(server, payload);
                if (listed) box.appendChild(el('div', 'small text-muted mk-line', listed));
                grid.appendChild(box);
                return;
            }
            const plan = PLANS[server.plan] || [server.plan || 'Unknown', 'text-bg-secondary'];
            top.appendChild(el('span', 'badge mk-plan ' + plan[1], plan[0]));
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
        if (results) {
            const done = el('div', 'alert alert-success py-2 small mt-3 mb-0 d-flex align-items-center gap-2 mk-saved');
            // Announced when it appears, and reachable: the save replaced the editor the focus was in.
            done.setAttribute('role', 'status');
            done.tabIndex = -1;
            done.appendChild(el('i', 'bi bi-lock-fill'));
            done.appendChild(el('div', '', 'Saved and locked. Your times stay until you unlock them.'));
            bodyEl.appendChild(done);
        }
        card.append(header, bodyEl);
        return card;
    }

    // =====================================================================
    // Adjust / Lock editor
    // =====================================================================

    function plural(count, word) {
        return count === 1 ? `1 ${word}` : `${count} ${word}s`;
    }

    // "0:14", "40:55" and "1:55:12" all read as a time; "90" is 90 seconds. Anything else is not a time at all.
    function parseClock(text) {
        const match = /^(\d{1,3})(?::(\d{1,2}))?(?::(\d{1,2}))?$/.exec(String(text === undefined || text === null ? '' : text).trim());
        if (!match) return null;
        const parts = [match[1], match[2], match[3]].filter(function (p) { return p !== undefined; }).map(Number);
        if (parts.slice(1).some(function (n) { return n > 59; })) return null;
        return parts.reduce(function (total, n) { return total * 60 + n; }, 0) * 1000;
    }

    // A time a screen reader can read out: "0 minutes 14 seconds", "1 hour 55 minutes 12 seconds".
    function spokenTime(ms) {
        const total = Math.max(0, Math.round(ms / 1000));
        const hours = Math.floor(total / 3600);
        const words = hours ? [plural(hours, 'hour')] : [];
        words.push(plural(Math.floor((total % 3600) / 60), 'minute'), plural(total % 60, 'second'));
        return words.join(' ');
    }

    function lengthText(ms) {
        const total = Math.max(0, Math.round(ms / 1000));
        if (total < 120) return `${plural(total, 'second')} long`;
        const seconds = total % 60;
        const minutes = plural(Math.floor(total / 60), 'minute');
        return seconds ? `${minutes} ${plural(seconds, 'second')} long` : `${minutes} long`;
    }

    function joinWith(names, word) {
        if (names.length < 2) return names[0] || '';
        return `${names.slice(0, -1).join(', ')} ${word} ${names[names.length - 1]}`;
    }

    function capitalise(text) {
        return text ? text[0].toUpperCase() + text.slice(1) : text;
    }

    function isLocked(payload, type) {
        const d = (payload.decisions || {})[type];
        return !!(d && d.marker && d.marker.locked);
    }

    function lockedTypes(payload) {
        return TYPES.filter(function (type) { return isLocked(payload, type); });
    }

    function enabledOwners(payload) {
        return (payload.servers || []).filter(function (s) { return s.markers_enabled; });
    }

    // Which brands with Intro & Credits on can show this type, and which can't. Brands, not server names: the
    // approved copy names Plex, Emby and Jellyfin, and two Plex servers say nothing more than one.
    function vendorsFor(payload, type) {
        const shown = [];
        const missing = [];
        enabledOwners(payload).forEach(function (server) {
            const vendor = VENDOR_NAMES[String(server.server_type || '').toLowerCase()] || server.server_type;
            const bucket = (server.can_show || []).indexOf(type) !== -1 ? shown : missing;
            if (shown.indexOf(vendor) === -1 && missing.indexOf(vendor) === -1) bucket.push(vendor);
        });
        return { shown: shown, missing: missing };
    }

    // The times the editor starts from: what was decided, or the proposal a "needs review" type is waiting on.
    function decisionSeed(payload, type) {
        const d = (payload.decisions || {})[type] || {};
        if (d.status === 'decided' && d.marker) {
            return { start: d.marker.start_ms, end: segmentEnd(d.marker, payload.duration_ms) };
        }
        if (d.proposed) return { start: d.proposed.start_ms, end: segmentEnd(d.proposed, payload.duration_ms) };
        return null;
    }

    // Every type the two zoom windows show, in the order they appear on screen. A movie has no opening window, so
    // it never lists intro or recap — which is why neither can be added to one.
    function windowTypes(payload) {
        const out = [];
        windowsFor(payload).forEach(function (win) {
            win.types.forEach(function (type) {
                if (out.indexOf(type) === -1) out.push(type);
            });
        });
        return out;
    }

    // A type is adjustable when one of the two zoom windows shows it and something put a bar there to drag.
    function editableTypes(payload) {
        return windowTypes(payload).filter(function (type) { return !!decisionSeed(payload, type); });
    }

    // A type a window shows that nothing was found for: there is no bar to drag, so the editor offers to make one.
    // The complement of editableTypes over the same list, so between them they cover every type on screen exactly
    // once. A type whose detection is off is included on purpose — spec §5.5 rule 1 lets a locked user marker win
    // "even for a type whose detection is off".
    function addableTypes(payload) {
        return windowTypes(payload).filter(function (type) { return !decisionSeed(payload, type); });
    }

    // Where a hand-added marker starts. The tail types are clamped at 0 because refusalFor() checks
    // `start >= duration`, not `start < 0`: an unclamped seed on a file shorter than the seed would sail past the
    // editor and come back 400 from the API. An intro or recap needs no clamp — 0:00–0:30 past the end of a shorter
    // file is refused by the same "inside the file" bound any other marker gets.
    function addSeed(payload, type) {
        const duration = payload.duration_ms;
        const tail = ADD_TAIL_MS[type];
        if (!tail) return { start: 0, end: ADD_HEAD_MS, toEnd: false };
        return { start: Math.max(0, duration - tail), end: duration, toEnd: true };
    }

    function resolved(type) {
        const model = editing.model[type];
        const duration = shown.payload.duration_ms;
        return { type: type, start_ms: model.start, end_ms: model.toEnd ? duration : model.end };
    }

    // The types a save sends. A type no server with Intro & Credits on can show is left out (and its strip says so).
    function sentTypes() {
        return editing.types.filter(function (type) { return editing.model[type].editable; });
    }

    // Spec §5.5 rule 2's two bounds a user's own marker keeps (P-R2). Everything else only warns.
    function refusalFor(type, payload) {
        const model = editing.model[type];
        // A box holding something that isn't a time at all: the model still carries the last time that *was* one, so
        // the bounds below would check a time the user can no longer see, and the red box would say nothing.
        if (model.badStart || model.badEnd) return NOT_A_TIME;
        const duration = payload.duration_ms;
        const end = model.toEnd ? duration : model.end;
        if (model.start >= duration || end > duration) return `That's past the end of the file (${clock(duration)}).`;
        if (end <= model.start) return 'The end has to come after the start.';
        return '';
    }

    function warningsFor(type, payload) {
        const model = editing.model[type];
        const duration = payload.duration_ms;
        const end = model.toEnd ? duration : model.end;
        const out = [];
        const atStart = START_SEGMENTS.indexOf(type) !== -1;
        if (type === 'intro' && end - model.start < USUAL_MIN_INTRO_MS) {
            out.push('That\'s shorter than most intros. This will still be saved.');
        }
        if (type === 'intro' && end - model.start > USUAL_MAX_INTRO_MS) {
            out.push('That\'s longer than most intros. This will still be saved.');
        }
        // Cross-multiplied like markers.decide, so the exact boundary can't land on the wrong side of a float.
        if (atStart && model.start * 100 > USUAL_START_PERCENT * duration) {
            out.push(`That's later in the file than ${TYPE_PLURALS[type]} usually are. This will still be saved.`);
        }
        if (!atStart && model.start * 100 < USUAL_END_PERCENT * duration) {
            out.push(`That's earlier than ${TYPE_PLURALS[type]} usually start. This will still be saved.`);
        }
        if (type === 'credits' && end < duration - END_OF_FILE_MS) {
            out.push('Credits usually run to the end of the file. This will still be saved.');
        }
        return out;
    }

    // The per-type note when some (or no) server with Intro & Credits on can show this type. Owner ruling: a type
    // one server can show stays editable and the note names who misses out; only "nobody can show it" refuses.
    function reachNote(type, payload) {
        const vendors = vendorsFor(payload, type);
        const word = TYPE_WORDS[type];
        if (!vendors.shown.length) {
            const missing = vendors.missing;
            const plurals = capitalise(TYPE_PLURALS[type]);
            // Nothing has been put there yet, so the refusal says "added"; a type detection found says "adjusted".
            const verb = decisionSeed(payload, type) ? 'adjusted' : 'added';
            // Nothing to name: no server with Intro & Credits on has this file at all, so the save would be refused
            // whatever the times were (the API answers 409 for it).
            if (!missing.length) return `${plurals} can't be ${verb} here: no server with Intro & Credits on has this file.`;
            let who = `${missing[0]} has no ${word} marker`;
            if (missing.length === 2) who = `neither ${missing[0]} nor ${missing[1]} has a ${word} marker`;
            else if (missing.length > 2) who = `none of ${joinWith(missing, 'and')} has a ${word} marker`;
            return `${plurals} can't be ${verb} here: ${who}, and no other server has this file.`;
        }
        if (!vendors.missing.length) return '';
        const shows = vendors.shown.length === 1 ? 'shows' : 'show';
        return `Only ${joinWith(vendors.shown, 'and')} ${shows} ${TYPE_PLURALS[type]}. `
            + `${joinWith(vendors.missing, 'and')} ${vendors.missing.length === 1 ? 'has' : 'have'} no ${TYPE_WORDS[type]} marker, `
            + 'so this one won\'t reach them.';
    }

    function editBanner(payload) {
        const box = el('div', 'alert alert-warning py-2 small d-flex align-items-center gap-2 mb-3 mk-edit-banner');
        box.appendChild(el('i', 'bi bi-pencil'));
        const what = payload.is_movie ? 'movie' : 'episode';
        box.appendChild(el('div', '', `Adjusting this ${what}. Nothing changes on your servers until you save.`));
        return box;
    }

    // The decision bar while it is being edited: the same amber bar, with a grab handle at each edge.
    function editBar(type, win, payload) {
        const node = el('span', 'mk-bar mk-bar-result mk-bar-editing');
        const text = el('span', 'mk-bar-text');
        const start = handle(type, 'start', win, payload);
        const end = handle(type, 'end', win, payload);
        node.append(start, text, end);
        editing.ui[type] = Object.assign(editing.ui[type] || {}, {
            win: win, bar: node, text: text, startHandle: start, endHandle: end,
        });
        return node;
    }

    function handle(type, edge, win, payload) {
        const node = el('button', 'mk-handle mk-handle-' + edge);
        node.type = 'button';
        // A slider, not a button whose name keeps changing: a screen reader announces a moved value on its own, while
        // a renamed button is only read out again the next time it is focused.
        node.setAttribute('role', 'slider');
        node.setAttribute('aria-label', `${TYPE_LABELS[type]} ${edge}`);
        node.setAttribute('aria-valuemin', '0');
        node.setAttribute('aria-valuemax', String(Math.round((payload.duration_ms || 0) / 1000)));
        node.disabled = !editing.model[type].editable || editing.saving;
        node.addEventListener('keydown', function (event) { onHandleKey(event, type, edge, payload); });
        node.addEventListener('pointerdown', function (event) { onHandleGrab(event, node, type, edge, win, payload); });
        return node;
    }

    function onHandleKey(event, type, edge, payload) {
        // Guarded here rather than in edgeMs, so every path that reads the model from a listener checks the same way.
        if (!editing || !editing.model[type] || !shown || !shown.payload) return;
        const step = event.shiftKey ? NUDGE_SHIFT_MS : NUDGE_MS;
        let delta = 0;
        if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') delta = -step;
        else if (event.key === 'ArrowRight' || event.key === 'ArrowUp') delta = step;
        if (!delta) return;
        event.preventDefault();
        // The page's own arrow-key handler steps the preview frame; it must not also fire for a marker nudge.
        event.stopPropagation();
        moveEdge(type, edge, edgeMs(type, edge) + delta, payload);
    }

    function onHandleGrab(event, node, type, edge, win, payload) {
        if (editing.model[type].editable === false || editing.saving) return;
        const track = node.closest('.mk-track');
        if (!track) return;
        event.preventDefault();
        node.focus();
        node.setPointerCapture(event.pointerId);
        const move = function (ev) {
            const rect = track.getBoundingClientRect();
            if (!rect.width) return;
            const fraction = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
            moveEdge(type, edge, win.start + fraction * (win.end - win.start), payload);
        };
        const stop = function () {
            node.removeEventListener('pointermove', move);
            node.removeEventListener('pointerup', stop);
            node.removeEventListener('pointercancel', stop);
        };
        node.addEventListener('pointermove', move);
        node.addEventListener('pointerup', stop);
        node.addEventListener('pointercancel', stop);
    }

    function edgeMs(type, edge) {
        const model = editing.model[type];
        return edge === 'start' ? model.start : (model.toEnd ? shown.payload.duration_ms : model.end);
    }

    // A drag or a nudge stays inside the file and can't cross the other edge; a typed time may, and is refused.
    function moveEdge(type, edge, ms, payload) {
        if (!editing || !editing.model[type] || !shown || !shown.payload) return;
        const model = editing.model[type];
        const duration = payload.duration_ms;
        const snapped = Math.round(ms / 1000) * 1000;
        // Compared after the clamp, so an arrow key that a bound swallows leaves "Starting times" saying the truth.
        const was = { start: model.start, end: model.end, toEnd: model.toEnd };
        if (edge === 'start') {
            const ceiling = (model.toEnd ? duration : model.end) - MIN_LENGTH_MS;
            model.start = Math.min(Math.max(0, snapped), Math.max(0, ceiling));
            model.badStart = false;
        } else {
            model.end = Math.min(Math.max(model.start + MIN_LENGTH_MS, snapped), duration);
            // Dragging the end onto the end of the file is the same statement the switch makes, so they agree.
            model.toEnd = TO_END_TYPES.indexOf(type) !== -1 && model.end >= duration - END_OF_FILE_MS;
            model.badEnd = false;
        }
        if (model.start !== was.start || model.end !== was.end || model.toEnd !== was.toEnd) model.untouched = false;
        refreshType(type);
        refreshActions(payload);
    }

    function timeField(type, edge, payload) {
        const label = el('label', 'd-flex align-items-center gap-1 small text-muted mb-0', edge === 'start' ? 'Start' : 'End');
        const input = el('input', 'mk-time');
        input.type = 'text';
        // Not "numeric": iOS shows a digits-only pad, and every time here has a colon in it.
        input.inputMode = 'text';
        input.autocomplete = 'off';
        input.setAttribute('aria-label', `${TYPE_LABELS[type]} ${edge}`);
        input.setAttribute('aria-describedby', messagesId(type));
        input.setAttribute('aria-invalid', 'false');
        input.addEventListener('input', function () { onFieldInput(type, edge, input, payload); });
        input.addEventListener('blur', function () { refreshType(type); refreshActions(payload); });
        label.appendChild(input);
        editing.ui[type] = editing.ui[type] || {};
        editing.ui[type][edge + 'Input'] = input;
        return label;
    }

    function onFieldInput(type, edge, input, payload) {
        const model = editing.model[type];
        const ms = parseClock(input.value);
        if (ms === null) {
            // Not a time at all: the model keeps its last good value, the field goes red and Save waits.
            if (edge === 'start') model.badStart = true; else model.badEnd = true;
        } else if (edge === 'start') {
            model.badStart = false;
            model.start = ms;
            model.untouched = false;
        } else {
            model.badEnd = false;
            model.toEnd = false;
            model.end = ms;
            model.untouched = false;
        }
        refreshType(type);
        refreshActions(payload);
    }

    function toEndSwitch(type, payload) {
        const wrap = el('div', 'form-check form-switch mb-0 d-flex align-items-center gap-2');
        const input = el('input', 'form-check-input mt-0');
        input.type = 'checkbox';
        input.setAttribute('role', 'switch');
        input.id = 'mkRunsToEnd-' + type;
        input.addEventListener('change', function () {
            const model = editing.model[type];
            const was = { end: model.end, toEnd: model.toEnd };
            model.toEnd = input.checked;
            model.badEnd = false;
            if (!model.toEnd) model.end = Math.min(payload.duration_ms, Math.max(model.start + MIN_LENGTH_MS, model.end));
            // Toggling the switch changes what the save sends (`end_ms: null` versus a time), so it counts as a
            // move even when the end lands on the same millisecond.
            if (model.end !== was.end || model.toEnd !== was.toEnd) model.untouched = false;
            refreshType(type);
            refreshActions(payload);
        });
        const label = el('label', 'form-check-label small text-muted', 'Runs to the end of the file');
        label.setAttribute('for', input.id);
        wrap.append(input, label);
        editing.ui[type] = Object.assign(editing.ui[type] || {}, { toEndInput: input });
        return wrap;
    }

    function keyboardHint() {
        const hint = el('span', 'mk-edit-hint');
        const wide = el('span', 'mk-edit-hint-wide');
        wide.append('Arrow keys move it 1 second · hold ', el('kbd', '', 'Shift'), ' for 10 seconds');
        const narrow = el('span', 'mk-edit-hint-narrow');
        narrow.append('Arrow keys 1 second · ', el('kbd', '', 'Shift'), ' 10 seconds');
        hint.append(wide, narrow);
        return hint;
    }

    function messagesId(type) {
        return 'mk-edit-messages-' + type;
    }

    function editStrip(type, payload) {
        const strip = el('div', 'mk-edit-strip');
        strip.dataset.editType = type;
        strip.appendChild(el('span', 'mk-edit-type', TYPE_LABELS[type]));
        strip.appendChild(timeField(type, 'start', payload));
        strip.appendChild(timeField(type, 'end', payload));
        const length = el('span', 'small text-muted mk-edit-length');
        // The times are announced as text, not only as a position on the track.
        length.setAttribute('role', 'status');
        strip.appendChild(length);
        if (TO_END_TYPES.indexOf(type) !== -1) {
            strip.appendChild(toEndSwitch(type, payload));
            strip.appendChild(infoIcon('Turn this off when something plays after the credits — a last scene, or a preview of the next episode.'));
        }
        strip.appendChild(keyboardHint());
        strip.appendChild(infoIcon('Tab to a handle, then use the arrow keys. You can also type a time straight into the boxes.'));
        if (editing.model[type].added) strip.appendChild(removeButton(type));
        const messages = el('div', 'mk-edit-messages');
        messages.id = messagesId(type);
        // What the boxes point at with aria-describedby, and a live region so a refusal is read out as it appears.
        messages.setAttribute('role', 'status');
        strip.appendChild(messages);
        editing.ui[type] = Object.assign(editing.ui[type] || {}, { length: length, messages: messages });
        return strip;
    }

    // Its tooltip rides on the button, the way Adjust, Lock and Add carry theirs, rather than a separate ⓘ.
    function removeButton(type) {
        const button = el('button', 'btn btn-link mk-edit-remove text-danger-emphasis', 'Remove');
        button.type = 'button';
        button.disabled = editing.saving;
        button.setAttribute('data-bs-toggle', 'tooltip');
        button.setAttribute('data-bs-placement', 'top');
        button.title = REMOVE_TIP;
        button.addEventListener('click', function () { removeType(type); });
        return button;
    }

    function message(icon, className, text) {
        const line = el('div', 'mk-edit-warn ' + className);
        line.appendChild(el('i', 'bi ' + icon + ' me-1'));
        line.appendChild(el('span', '', text));
        return line;
    }

    function setSliderValue(node, ms) {
        node.setAttribute('aria-valuenow', String(Math.round(ms / 1000)));
        node.setAttribute('aria-valuetext', spokenTime(ms));
    }

    // Everything one type's strip and bar show, recomputed from the model. Called on every nudge, drag and keystroke,
    // so it never rebuilds a node: a rebuild would drop the focus the keyboard edit lives on.
    function refreshType(type) {
        if (!editing || !editing.model[type] || !shown || !shown.payload) return;
        const payload = shown.payload;
        const duration = payload.duration_ms;
        const model = editing.model[type];
        const ui = editing.ui[type] || {};
        const seg = resolved(type);
        const end = seg.end_ms;
        const frozen = !model.editable || editing.saving;
        const refusal = model.editable ? refusalFor(type, payload) : '';
        // The offending box is the one that holds an impossible time: the start when it is at or past the end (of the
        // marker or of the file), the end when it reaches past the file.
        const badStart = !!model.badStart || model.start >= duration || (!!refusal && end <= model.start);
        const badEnd = !!model.badEnd || end > duration;
        // A box being typed into is never rewritten: "0" on the way to "0:14" is a valid time, and normalising it
        // mid-word would move the caret out from under the next keystroke. Its blur tidies it up instead.
        if (ui.startInput) {
            if (document.activeElement !== ui.startInput) ui.startInput.value = clock(model.start);
            ui.startInput.disabled = frozen;
            ui.startInput.classList.toggle('is-invalid', badStart);
            ui.startInput.setAttribute('aria-invalid', badStart ? 'true' : 'false');
        }
        if (ui.endInput) {
            if (document.activeElement !== ui.endInput) ui.endInput.value = clock(end);
            ui.endInput.disabled = frozen || model.toEnd;
            ui.endInput.classList.toggle('is-invalid', badEnd);
            ui.endInput.setAttribute('aria-invalid', badEnd ? 'true' : 'false');
        }
        if (ui.toEndInput) {
            ui.toEndInput.checked = model.toEnd;
            ui.toEndInput.disabled = frozen;
        }
        if (ui.length) ui.length.textContent = lengthText(Math.max(0, end - model.start));
        if (ui.bar) {
            const win = ui.win;
            const span = win.end - win.start;
            const left = Math.min(Math.max(model.start, win.start), win.end);
            const right = Math.min(Math.max(end, left), win.end);
            ui.bar.style.left = ((left - win.start) / span * 100).toFixed(3) + '%';
            ui.bar.style.width = ((right - left) / span * 100).toFixed(3) + '%';
            const label = `${TYPE_LABELS[type]} ${laneRange(seg, duration)}`;
            ui.text.textContent = label;
            ui.bar.title = label;
            setSliderValue(ui.startHandle, model.start);
            setSliderValue(ui.endHandle, end);
            ui.startHandle.disabled = frozen;
            ui.endHandle.disabled = frozen;
            placeBarLabel(ui.bar);
        }
        if (ui.messages) {
            const lines = [];
            const note = reachNote(type, payload);
            if (!model.editable) {
                lines.push(message('bi-slash-circle', 'text-danger-emphasis', note));
            } else {
                if (note) lines.push(message('bi-info-circle', 'text-muted', note));
                // Only while the seed is still untouched: once an edge moves these are the user's times, not ours.
                if (model.untouched) lines.push(message('bi-info-circle', 'text-muted', STARTING_TIMES));
                if (refusal) lines.push(message('bi-x-circle', 'text-danger-emphasis', refusal));
                else warningsFor(type, payload).forEach(function (text) {
                    lines.push(message('bi-exclamation-triangle', 'text-warning-emphasis', text));
                });
            }
            // A live region announces a mutation, not a change, and this runs on every keystroke and every frame
            // of a drag: putting the same standing warning back would read it out again each time.
            const spoken = lines.map(function (line) { return line.textContent; }).join('\n');
            if (spoken !== ui.messagesText) {
                ui.messagesText = spoken;
                ui.messages.replaceChildren.apply(ui.messages, lines);
            }
        }
    }

    function pendingText(payload) {
        return sentTypes().map(function (type) {
            return `${TYPE_LABELS[type]} ${laneRange(resolved(type), payload.duration_ms)}`;
        }).join(' · ');
    }

    // The pack's rule for the count: servers with Intro & Credits on that can show at least one type being saved.
    function publishCount(payload, types) {
        return enabledOwners(payload).filter(function (server) {
            return (server.can_show || []).some(function (type) { return types.indexOf(type) !== -1; });
        }).length;
    }

    function willReceive(server) {
        const types = sentTypes();
        return !!server.markers_enabled && (server.can_show || []).some(function (type) { return types.indexOf(type) !== -1; });
    }

    function saveLabel(payload) {
        const count = publishCount(payload, sentTypes());
        return count ? `Save and publish to ${count} server${count === 1 ? '' : 's'}` : 'Save';
    }

    function sendingSpinner() {
        const spinner = el('span', 'spinner-border spinner-border-sm me-1');
        spinner.style.width = '.7rem';
        spinner.style.height = '.7rem';
        spinner.setAttribute('aria-hidden', 'true');
        return spinner;
    }

    function pendingFor(server, payload) {
        const canShow = server.can_show || [];
        return sentTypes().filter(function (type) { return canShow.indexOf(type) !== -1; })
            .map(function (type) { return `${TYPE_LABELS[type]} ${cardRange(resolved(type), payload.duration_ms)}`; })
            .join(' · ');
    }

    function editActions(payload) {
        const bar = el('div', 'mk-edit-actions');
        if (editing.saving) {
            bar.appendChild(el('span', 'small text-muted', 'Your times are saved. Sending them to your servers…'));
            bar.appendChild(el('span', 'flex-grow-1'));
            const saving = el('button', 'btn btn-sm btn-primary mk-edit-save');
            saving.type = 'button';
            saving.disabled = true;
            saving.appendChild(sendingSpinner());
            saving.appendChild(el('span', '', 'Saving…'));
            bar.appendChild(saving);
            editing.actions = {};
            return bar;
        }
        bar.appendChild(el('span', 'small text-muted', 'Adjusting'));
        const pending = el('span', 'mk-edit-pending');
        bar.appendChild(pending);
        bar.appendChild(el('span', 'flex-grow-1'));
        bar.appendChild(el('span', 'small text-muted', 'Saving keeps your times — later checks won\'t change them.'));
        const cancel = el('button', 'btn btn-sm btn-outline-secondary mk-edit-cancel', 'Cancel');
        cancel.type = 'button';
        cancel.addEventListener('click', cancelEdit);
        const save = el('button', 'btn btn-sm btn-primary mk-edit-save');
        save.type = 'button';
        save.addEventListener('click', function () { saveEdit(sentTypes()); });
        bar.append(cancel, save);
        editing.actions = { pending: pending, save: save };
        return bar;
    }

    function refreshActions(payload) {
        if (!editing || !shown || !shown.payload) return;
        const actions = editing.actions || {};
        if (!actions.save) return;
        const blocked = sentTypes().some(function (type) {
            const model = editing.model[type];
            return model.badStart || model.badEnd || !!refusalFor(type, payload);
        });
        const listed = pendingText(payload);
        // Nothing on the timeline yet (or every added marker taken back out): the slot says so instead of sitting
        // empty, and drops the monospace it uses for times.
        actions.pending.textContent = listed || NOTHING_TO_SAVE;
        actions.pending.classList.toggle('mk-edit-empty', !listed);
        actions.save.textContent = saveLabel(payload);
        actions.save.disabled = blocked || !sentTypes().length;
    }

    function workingReason() {
        if (editing) return EDIT_IN_HAND;
        if (busy) return LOCK_IN_FLIGHT;
        return queueing ? REDETECT_QUEUEING : '';
    }

    function adjustReason(payload) {
        // A file where nothing was found still has somewhere to go: the editor opens on its Add buttons. What it
        // still needs is a length -- without one there is no timeline to put a marker on.
        const nothing = !payload.known || !payload.duration_ms
            || !(editableTypes(payload).length || addableTypes(payload).length);
        return nothing ? NOTHING_TO_ADJUST : '';
    }

    // A decided type no enabled server can show is no more lockable than it is adjustable (the API refuses the whole
    // request over one), so a file with times but nowhere to send them says that rather than "nothing to lock".
    function lockReason(payload) {
        if (!payload.duration_ms) return NOTHING_TO_LOCK;
        if (lockedTypes(payload).length || lockableTypes(payload).length) return '';
        const decided = TYPES.some(function (type) { return decidedMarker(payload, type); });
        return decided ? NO_OWNER_TO_LOCK : NOTHING_TO_LOCK;
    }

    // Bootstrap 5.3 gives .btn:disabled pointer-events: none, so a disabled button's tooltip never fires and the
    // user is left with a grey button and no reason at all. These two stay clickable and say why instead — the same
    // sentence the Season view's Edit gives — while aria-disabled tells assistive tech they do nothing.
    function setBlocked(button, reason) {
        // The button's own words when it works, captured before the first refusal overwrites the title.
        if (button.dataset.tip === undefined) button.dataset.tip = button.title;
        button.dataset.blocked = reason || '';
        // The markup ships both buttons disabled for the moment before anything is loaded. From here on the state is
        // aria-disabled, which keeps the pointer events the tooltip and the "why not" need.
        button.disabled = false;
        button.classList.toggle('mk-blocked', !!reason);
        if (reason) button.setAttribute('aria-disabled', 'true');
        else button.removeAttribute('aria-disabled');
        retitle(button, reason || button.dataset.tip);
    }

    // Returns whether the press was refused, so the caller does nothing else with it.
    function sayWhyBlocked(button) {
        const reason = button.dataset.blocked || '';
        if (reason) showToast('Intro & Credits', reason, 'info');
        return !!reason;
    }

    function syncHeaderButtons(payload, item) {
        const path = payload.canonical_path || item.media_file || '';
        const redetect = $('markersRedetectBtn');
        const adjust = $('markersAdjustBtn');
        const lock = $('markersLockBtn');
        const working = workingReason();
        if (redetect) setBlocked(redetect, working || (path ? '' : NO_FILE_YET));
        if (!adjust || !lock) return;
        setBlocked(adjust, working || adjustReason(payload));
        const locked = lockedTypes(payload);
        setLockButton(lock, locked.length > 0);
        setBlocked(lock, working || lockReason(payload));
    }

    // Every caller follows this with setBlocked, which is what puts the new words on the tooltip.
    function setLockButton(button, locked) {
        button.replaceChildren(el('i', 'bi ' + (locked ? 'bi-unlock' : 'bi-lock') + ' me-1'), document.createTextNode(locked ? 'Unlock' : 'Lock'));
        button.dataset.mode = locked ? 'unlock' : 'lock';
        button.dataset.tip = locked ? UNLOCK_TIP : LOCK_TIP;
    }

    // The button's own words change with its job, so its tooltip is rebuilt rather than left saying the old thing.
    function retitle(button, text) {
        button.title = text;
        const bs = window.bootstrap;
        if (!bs || !bs.Tooltip) return;
        const tip = bs.Tooltip.getInstance(button);
        if (tip) tip.dispose();
        if (button.getAttribute('data-bs-toggle') === 'tooltip') new bs.Tooltip(button);
    }

    // Returns whether the editor opened, so the Season view's Edit can say why when it didn't.
    function startEditing() {
        const payload = shown && shown.payload;
        if (!payload || !payload.duration_ms || editing) return false;
        const types = editableTypes(payload);
        // A file with nothing found still opens: every type a window shows carries an Add button instead of a bar.
        if (!types.length && !addableTypes(payload).length) return false;
        const model = {};
        types.forEach(function (type) {
            const seed = decisionSeed(payload, type);
            model[type] = {
                start: seed.start,
                end: seed.end,
                toEnd: TO_END_TYPES.indexOf(type) !== -1 && seed.end >= payload.duration_ms - END_OF_FILE_MS,
                // Owner ruling: only a type no enabled server can show at all is refused.
                editable: vendorsFor(payload, type).shown.length > 0,
                added: false,
                untouched: false,
                badStart: false,
                badEnd: false,
            };
        });
        editing = { types: types, model: model, saving: false, ui: {}, actions: null };
        results = null;
        render(payload, shown.item);
        // Focus follows the button that was pressed into the editor, so a keyboard user is already on a handle — or,
        // when there is nothing to drag yet, on the first Add.
        const first = $('markersInspectorBody').querySelector('.mk-handle:not([disabled]), .mk-add:not([disabled])');
        if (first) first.focus({ preventScroll: true });
        return true;
    }

    // The types on screen, in the order the two windows show them.
    function modelTypes(payload) {
        return windowTypes(payload).filter(function (type) { return !!editing.model[type]; });
    }

    // Put a marker on the timeline where detection found nothing. It is an ordinary edited marker from here on:
    // the same drag, the same bounds, the same save, the same lock.
    function addType(type) {
        const payload = shown && shown.payload;
        if (!editing || editing.saving || !payload || editing.model[type]) return;
        if (!vendorsFor(payload, type).shown.length) return;
        const seed = addSeed(payload, type);
        editing.model[type] = {
            start: seed.start,
            end: seed.end,
            toEnd: seed.toEnd,
            editable: true,
            added: true,
            untouched: true,
            badStart: false,
            badEnd: false,
        };
        editing.types = modelTypes(payload);
        render(payload, shown.item);
        const ui = editing.ui[type] || {};
        if (ui.startHandle && !ui.startHandle.disabled) ui.startHandle.focus({ preventScroll: true });
    }

    // Only a marker added in this edit can be taken back out. One detection found is dropped with Unlock or
    // Re-detect, which is a different thing: it has already reached the servers.
    function removeType(type) {
        const payload = shown && shown.payload;
        if (!editing || editing.saving || !payload) return;
        const model = editing.model[type];
        if (!model || !model.added) return;
        delete editing.model[type];
        delete editing.ui[type];
        editing.types = modelTypes(payload);
        render(payload, shown.item);
        const back = $('markersInspectorBody').querySelector(`.mk-add[data-add-type="${type}"]`);
        if (back && !back.disabled) back.focus({ preventScroll: true });
    }

    function cancelEdit() {
        if (!editing || editing.saving) return;
        editing = null;
        render(shown.payload, shown.item);
        // The re-render threw away the node the focus was on; it goes back to the button the edit started from.
        const adjust = $('markersAdjustBtn');
        if (adjust) adjust.focus();
    }

    // null end_ms is how the API reads "runs to the end of the file" — the same thing the switch says.
    function markersBody(types, model) {
        return types.map(function (type) {
            return { type: type, start_ms: model[type].start, end_ms: model[type].toEnd ? null : model[type].end };
        });
    }

    async function saveEdit(types) {
        if (!editing || editing.saving || !types.length) return;
        const payload = shown.payload;
        const path = payload.canonical_path || shown.item.media_file;
        const body = markersBody(types, editing.model);
        const started = { key: shown.key, seq: requestSeq, path: path };
        editing.saving = true;
        render(payload, shown.item);
        try {
            applySaved(await apiPost('/api/markers/item/markers', { path: path, markers: body }), types, started);
        } catch (error) {
            // api_markers.marker_item_save stores and locks the times *before* it publishes, so a 500, a dropped
            // connection and the post-save FileChangedError all report a failure over a save that landed. Both
            // caches would go on serving the pre-save, unlocked times, so they go whatever the error was.
            cache.delete(started.key);
            if (window.markersSeason) window.markersSeason.forgetFile(started.path);
            showToast(
                'Intro & Credits',
                `Couldn't finish saving: ${error.message} Your times may already be saved — reload this file to see`
                + ' where they stand.',
                'danger',
            );
            if (!editing || !shown || shown.key !== started.key) return;
            editing.saving = false;
            render(shown.payload, shown.item);
        }
    }

    // The save answers with the stored markers and one row per server, not a whole item payload: fold what it says
    // into the payload on screen, so the next visit re-reads the file from the API.
    // ``started`` is where the request set off; an answer for a file the Inspector has since left is dropped.
    // Returns whether it was applied, so a caller doesn't announce a save the user can no longer see.
    function applySaved(answer, types, started) {
        // The write landed on the server whichever file is on screen now, so both caches forget it first: an answer
        // for a file the user has already left would otherwise leave its pre-save times to come back on the next visit.
        cache.delete(started.key);
        if (window.markersSeason) window.markersSeason.forgetFile(started.path);
        if (!shown || !shown.payload || shown.key !== started.key || started.seq !== requestSeq) return false;
        const payload = shown.payload;
        const saved = answer.markers || {};
        // Every stored marker comes back, not only the ones just sent, so a type the user didn't touch keeps the
        // decision it already had and only its stored times are refreshed.
        Object.keys(saved).forEach(function (type) {
            const decisions = payload.decisions || (payload.decisions = {});
            const d = decisions[type] || (decisions[type] = {});
            const locked = !!saved[type].locked;
            const before = d.marker || {};
            d.status = 'decided';
            if (locked) {
                d.reason = 'locked by user';
                d.proposed = null;
            }
            d.marker = {
                type: type,
                start_ms: saved[type].start_ms,
                end_ms: saved[type].end_ms,
                decided_by: locked ? ['user'] : (before.decided_by || []),
                locked: locked,
                locked_at: saved[type].locked_at || null,
            };
        });
        applyToServers(payload, answer, types);
        results = { servers: answer.servers || [], markers: saved, sent: types.slice() };
        editing = null;
        render(payload, shown.item);
        // The re-render threw away the editor the focus was in: it goes to what the save has just said.
        const done = $('markersInspectorBody').querySelector('.mk-saved');
        if (done) done.focus();
        return true;
    }

    // A server that took the save shows the saved times from this moment on. Without this its "now" lane would keep
    // drawing the marker it had before, and the Decision lane would mark that server as disagreeing with the very
    // times it just took (§5.5: 5 s apart for intro/recap, 10 s for credits/preview).
    function applyToServers(payload, answer, types) {
        const saved = answer.markers || {};
        (answer.servers || []).forEach(function (row) {
            if (row.result !== 'written' && row.result !== 'unchanged') return;
            const server = (payload.servers || []).find(function (s) { return s.server_id === row.server_id; });
            // A server whose markers couldn't be read stays unread: this answer says what it took, not what it has.
            if (!server || !Array.isArray(server.current)) return;
            const canShow = row.can_show || [];
            const took = types.filter(function (type) { return canShow.indexOf(type) !== -1 && saved[type]; });
            if (!took.length) return;
            // Emby's credits are published start-only when they end before the file does, and run to the end there.
            const startOnly = (row.notes || []).some(function (n) { return n.type === 'credits' && n.field === 'end'; });
            const rest = server.current.filter(function (c) { return took.indexOf(c.type) === -1; });
            took.forEach(function (type) {
                const marker = saved[type];
                rest.push({
                    type: type,
                    start_ms: marker.start_ms,
                    end_ms: type === 'credits' && startOnly ? null : marker.end_ms,
                });
            });
            server.current = rest.sort(function (a, b) { return a.start_ms - b.start_ms; });
        });
    }

    function resultFor(serverId) {
        if (!results) return null;
        return results.servers.find(function (row) { return row.server_id === serverId; }) || null;
    }

    function typeWords(types) {
        return types.map(function (type) { return TYPE_WORDS[type] || type; });
    }

    function resultBadge(row) {
        const cant = row.cant_show || [];
        if (cant.length && row.result === 'written') {
            return [`${capitalise(joinWith(typeWords(cant), 'or'))} not sent`, 'text-bg-secondary'];
        }
        return RESULTS[row.result] || [row.result || 'Unknown', 'text-bg-secondary'];
    }

    // What a server took. Emby's credits are published start-only when they end before the file does, and its row
    // says so in `notes`, so the line shows the start alone rather than an end Emby never got.
    function listedForResult(row, payload) {
        const canShow = row.can_show || [];
        const startOnly = (row.notes || []).some(function (n) { return n.type === 'credits' && n.field === 'end'; });
        return results.sent.filter(function (type) { return canShow.indexOf(type) !== -1; })
            .map(function (type) {
                const marker = results.markers[type];
                if (!marker) return '';
                if (type === 'credits' && startOnly) return `${TYPE_LABELS[type]} ${clock(marker.start_ms)}`;
                return `${TYPE_LABELS[type]} ${cardRange(marker, payload.duration_ms)}`;
            })
            .filter(Boolean).join(' · ');
    }

    function resultLines(row, payload) {
        const lines = [];
        const cant = row.cant_show || [];
        const vendor = VENDOR_NAMES[String(row.server_type || '').toLowerCase()] || row.server_type;
        if (row.result === 'written' || row.result === 'unchanged') {
            const listed = listedForResult(row, payload);
            if (listed) lines.push([listed, 'text-muted']);
        }
        if (RESULT_LINES[row.result]) {
            lines.push(RESULT_LINES[row.result]);
        } else if (row.result === 'written' && cant.length) {
            const sent = results.sent.filter(function (type) { return cant.indexOf(type) === -1; });
            const were = sent.length === 1 ? 'was' : 'were';
            lines.push([`${vendor} has no ${joinWith(typeWords(cant), 'or')} marker. `
                + `Its ${joinWith(typeWords(sent), 'and')} ${were} updated.`, 'text-muted']);
        } else if (row.result === 'nothing_to_publish' && cant.length) {
            lines.push([`${vendor} has no ${joinWith(typeWords(cant), 'or')} marker, and nothing else changed.`, 'text-muted']);
        } else if (row.message && row.result !== 'written' && row.result !== 'unchanged') {
            // A written row's message is the job's own wording ("2 marker(s)"); the times above say it better, and
            // the badge still carries the publisher's words for anyone who wants them.
            lines.push([row.message, 'text-muted']);
        }
        (row.notes || []).forEach(function (note) {
            if (note.type !== 'credits' || note.field !== 'end' || !note.note) return;
            // The phrase itself stays the publisher's (markers.publishers.emby.CREDITS_BEFORE_END_NOTE); only the
            // sentence around it is the editor's, so the two can't drift into saying different things.
            lines.push([`Your credits end wasn't sent. ${note.note}, past any scene after the credits.`, 'text-warning-emphasis']);
        });
        if ((row.replaced_own || []).length) {
            // The same sentence markers.outcomes.replaced_own_note writes for a job row; the editor row carries only
            // the types, so the words live here too — change both together.
            lines.push([`Replaced ${vendor}'s own marker. This server is set to keep ${vendor}'s, `
                + 'but a marker you adjust always wins.', 'text-muted']);
        }
        const server = (payload.servers || []).find(function (s) { return s.server_id === row.server_id; });
        if (server && String(row.server_type).toLowerCase() === 'plex' && server.version_count > 1) {
            lines.push(['All versions of this item share one set of markers', 'text-muted']);
        }
        return lines;
    }

    // What Lock can actually send. A type no server with Intro & Credits on can show is left out for the same reason
    // the editor leaves it out: the API refuses the whole request over one unshowable type
    // (``api_markers._unshowable_type``), and a recap is decided whatever vendors the user happens to run.
    function lockableTypes(payload) {
        return TYPES.filter(function (type) {
            return decidedMarker(payload, type) && vendorsFor(payload, type).shown.length > 0;
        });
    }

    // Lock publishes to every owning server, exactly as Save does, so it says so first — Save's own words, with the
    // times it is about to keep. What it lists is held until Confirm, so what gets published is what was read.
    function askToLock() {
        const payload = shown && shown.payload;
        const modalEl = $('markersLockModal');
        const list = $('markersLockList');
        if (!payload || !payload.duration_ms || !modalEl || !list || !window.bootstrap) return;
        const types = lockableTypes(payload);
        if (!types.length) return;
        const model = {};
        types.forEach(function (type) {
            const marker = decidedMarker(payload, type);
            const end = segmentEnd(marker, payload.duration_ms);
            model[type] = {
                start: marker.start_ms,
                end: end,
                toEnd: TO_END_TYPES.indexOf(type) !== -1 && end >= payload.duration_ms - END_OF_FILE_MS,
            };
        });
        pendingLock = {
            key: shown.key,
            path: payload.canonical_path || shown.item.media_file,
            types: types,
            model: model,
        };
        list.replaceChildren.apply(list, types.map(function (type) {
            const marker = decidedMarker(payload, type);
            return el('li', 'font-monospace small', `${TYPE_LABELS[type]} ${laneRange(marker, payload.duration_ms)}`);
        }));
        const count = publishCount(payload, types);
        const confirm = $('markersLockConfirm');
        if (confirm) confirm.textContent = `Lock and publish to ${count} server${count === 1 ? '' : 's'}`;
        showModal(modalEl);
    }

    function forgetPendingLock() {
        pendingLock = null;
    }

    // Which of this tab's dialogs are open or on their way up. Bootstrap's own `modal-open` sits on the body and
    // would answer for the other one, and a dialog's `show` class isn't set yet while its backdrop fades in.
    const openDialogs = new Set();

    function showModal(modalEl) {
        openDialogs.add(modalEl);
        modalEl.addEventListener('hidden.bs.modal', function () { openDialogs.delete(modalEl); }, { once: true });
        window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }

    // Bootstrap ignores hide() while a dialog is still opening — its backdrop fades first, so that window is real —
    // and the dialog would then sit on screen over the work the button just started, its backdrop swallowing every
    // click on the page beneath. So the hide is chased until it takes: once the dialog is up if that one was
    // ignored, and dropped again the moment a hide completes. A dialog the user has already closed has nothing to
    // chase — arming then would slam it shut the next time it opened.
    function hideModal(modalEl) {
        if (!modalEl || !window.bootstrap) return;
        const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.hide();
        if (!openDialogs.has(modalEl)) return;
        const again = function () { modal.hide(); };
        modalEl.addEventListener('shown.bs.modal', again, { once: true });
        modalEl.addEventListener('hidden.bs.modal', function () {
            modalEl.removeEventListener('shown.bs.modal', again);
        }, { once: true });
    }

    // Lock alone changes no time: the times the dialog listed go through the same save (so the same lock, and the
    // same publish to every owner) exactly as they are — plan ruling P-R3.
    async function lockNow() {
        const pending = pendingLock;
        pendingLock = null;
        hideModal($('markersLockModal'));
        if (!pending || editing || busy) return;
        const started = { key: pending.key, seq: requestSeq, path: pending.path };
        // The publish runs against every server: Adjust and Re-detect stay out of reach until it answers, so an edit
        // can't be started and then thrown away by the answer.
        busy = true;
        if (shown && shown.payload) syncHeaderButtons(shown.payload, shown.item);
        let answer = null;
        let failure = '';
        try {
            answer = await apiPost(
                '/api/markers/item/markers',
                { path: pending.path, markers: markersBody(pending.types, pending.model) },
            );
        } catch (error) {
            failure = error.message;
        }
        busy = false;
        // An answer for a file the Inspector has since left applies nothing, but the buttons still have to come back.
        if (!answer || !applySaved(answer, pending.types, started)) {
            if (shown && shown.payload) syncHeaderButtons(shown.payload, shown.item);
            if (failure) showToast('Intro & Credits', `Couldn't lock these markers: ${failure}`, 'danger');
            return;
        }
        showToast('Intro & Credits', 'Locked — these times stay until you unlock them.', 'success');
    }

    async function apiSend(url, method, data) {
        const response = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
            body: JSON.stringify(data),
        });
        if (response.status === 401) {
            window.location.href = '/login';
            throw new Error('Authentication required');
        }
        const body = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error((body && body.error) || `HTTP ${response.status}`);
        return body;
    }

    function askToUnlock() {
        const payload = shown && shown.payload;
        const modalEl = $('markersUnlockModal');
        const list = $('markersUnlockList');
        if (!payload || !modalEl || !list || !window.bootstrap) return;
        const types = lockedTypes(payload);
        if (!types.length) return;
        list.replaceChildren.apply(list, types.map(function (type) {
            const marker = decidedMarker(payload, type);
            return el('li', 'font-monospace small', `${TYPE_LABELS[type]} ${laneRange(marker, payload.duration_ms)}`);
        }));
        modalEl.dataset.types = types.join(',');
        showModal(modalEl);
    }

    async function unlock() {
        const payload = shown && shown.payload;
        const modalEl = $('markersUnlockModal');
        if (!payload || !modalEl) return;
        const types = String(modalEl.dataset.types || '').split(',').filter(Boolean);
        const path = payload.canonical_path || shown.item.media_file;
        // The file being unlocked, held from before the await: an answer arriving after the user opened something
        // else has to drop this file's cached payload, not the one they moved to.
        const key = shown.key;
        const button = $('markersUnlockConfirm');
        if (button) button.disabled = true;
        try {
            await apiSend('/api/markers/item/markers', 'DELETE', { path: path, types: types });
            hideModal(modalEl);
            showToast('Intro & Credits', 'Unlocked — the next check decides these times again.', 'success');
            // What the sources make of these types now is the API's answer, not something this page can work out.
            results = null;
            cache.delete(key);
            if (window.markersSeason) window.markersSeason.forgetFile(path);
            await loadMarkersInspector(shown.item);
        } catch (error) {
            showToast('Intro & Credits', `Couldn't unlock: ${error.message}`, 'danger');
        } finally {
            if (button) button.disabled = false;
        }
    }

    // Labels wider than their bar sit beside it in the page's text colour, so they stay readable on the track.
    function placeBarLabel(node) {
        const text = node.querySelector('.mk-bar-text');
        // The bar being dragged is measured again on every move, so a label that fitted a moment ago is put back.
        node.classList.remove('mk-bar-out-right', 'mk-bar-out-left');
        // clientWidth includes the bar's 5px side padding; the text doesn't get that room.
        if (!text || text.scrollWidth <= node.clientWidth - 10) return;
        const track = node.parentElement;
        const roomRight = track.clientWidth - (node.offsetLeft + node.offsetWidth);
        node.classList.add(roomRight >= text.scrollWidth + 6 || roomRight >= node.offsetLeft ? 'mk-bar-out-right' : 'mk-bar-out-left');
    }

    function placeBarLabels(root) {
        root.querySelectorAll('.mk-bar').forEach(placeBarLabel);
    }

    function render(payload, item) {
        const body = $('markersInspectorBody');
        $('markersInspectorPath').textContent = payload.canonical_path || item.media_file || '';
        if (window.markersSeason) window.markersSeason.setPath(payload.canonical_path || item.media_file || '');
        syncHeaderButtons(payload, item);
        const parts = [];
        if (editing) {
            // Every node the live edit updates is built below; nothing may be left pointing at the last render's DOM.
            editing.ui = {};
            parts.push(editBanner(payload));
        }
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
        // One action bar for the whole edit, under both zoom windows (the approved pack, surfaces 2 and 3).
        if (editing) parts.push(editActions(payload));
        parts.push(renderServers(payload));
        if (typeof window._disposeBootstrapTooltips === 'function') window._disposeBootstrapTooltips(body);
        body.replaceChildren.apply(body, parts);
        if (editing) {
            editing.types.forEach(function (type) { refreshType(type); });
            refreshActions(payload);
        }
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
    // a path this app can't place (400) is tried again by id, naming the version so no other version opens instead.
    async function fetchItem(item) {
        const queries = [];
        if (item.media_file) queries.push('path=' + encodeURIComponent(item.media_file));
        if (item.server_id && item.item_id) {
            let byId = 'server_id=' + encodeURIComponent(item.server_id) + '&item_id=' + encodeURIComponent(item.item_id);
            if (item.media_file) byId += '&version_file=' + encodeURIComponent(item.media_file);
            queries.push(byId);
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
            lastError.reason = (data && data.reason) || '';
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
        // A different file (or a fresh read of this one) is not the file the open edit or the last save was about.
        editing = null;
        results = null;
        if (!item || !(item.media_file || (item.server_id && item.item_id))) {
            shown = null;
            $('markersInspectorPath').textContent = '';
            headerForNoPayload();
            body.replaceChildren(el('div', 'alert alert-secondary py-2', 'Search for the title above to see its Intro & Credits. A pasted preview path doesn\'t say which video file it belongs to.'));
            return;
        }
        watchJobs();
        if (cache.has(key)) {
            shown = { key: key, item: item, payload: cache.get(key) };
            render(shown.payload, item);
            return;
        }
        shown = { key: key, item: item, payload: null };
        $('markersInspectorPath').textContent = item.media_file || '';
        headerForNoPayload();
        body.replaceChildren(el('div', 'text-muted small py-3', 'Loading Intro & Credits…'));
        try {
            const payload = await fetchItem(item);
            cache.set(key, payload);
            if (seq !== requestSeq) return;
            shown.payload = payload;
            render(payload, item);
        } catch (error) {
            if (seq !== requestSeq) return;
            shown.failed = error.reason === 'version_not_here' ? 'version' : 'read';
            headerForNoPayload();
            if (error.reason === 'version_not_here') {
                // Nothing to re-detect: the job would find no file either.
                const box = el('div', 'alert alert-secondary py-2 mk-version-not-here');
                box.appendChild(el('strong', '', error.message));
                box.appendChild(el('div', 'small', 'Intro & Credits reads the file itself. Pick another version, or check this server\'s path mappings.'));
                body.replaceChildren(box);
                return;
            }
            body.replaceChildren(el('div', 'alert alert-warning py-2', `Couldn't load Intro & Credits for this file: ${error.message}`));
        }
    }

    // The Season view's Edit: show that episode's tab, then open the editor on it. Whatever the user did while this
    // file's payload was in flight wins — another file, or "Whole season" — so the editor never opens over someone
    // else's file or inside the hidden episode view. A file the editor can't open at all says so rather than doing
    // nothing — which now means one with no length, not one with no markers.
    async function editFile(item) {
        await loadMarkersInspector(item);
        const episodeView = $('markersEpisodeView');
        if (!shown || shown.item !== item || (episodeView && episodeView.hidden)) return;
        // A file that wouldn't load already says so where the editor would have been.
        if (shown.payload && !startEditing()) showToast('Intro & Credits', NOTHING_TO_ADJUST, 'info');
    }

    // All three header buttons refused for one reason, whatever they said a moment ago. Callers that want one of
    // them back say so afterwards.
    function headerButtonsBlocked(reason) {
        const redetect = $('markersRedetectBtn');
        const adjust = $('markersAdjustBtn');
        const lock = $('markersLockBtn');
        if (redetect) setBlocked(redetect, reason);
        if (adjust) setBlocked(adjust, reason);
        if (lock) {
            setLockButton(lock, false);
            setBlocked(lock, reason);
        }
    }

    // Both caches forget one file: whatever is about to change it, what they hold describes the file before that
    // happened. Takes the file rather than reading it off ``shown``, so a request that answers after the user has
    // moved on still drops the file it was about.
    function forget(key, path) {
        cache.delete(key);
        if (path && window.markersSeason) window.markersSeason.forgetFile(path);
    }

    function forgetShown() {
        if (!shown) return;
        forget(shown.key, (shown.payload && shown.payload.canonical_path) || shown.item.media_file);
    }

    // A job publishing markers changes what every server shows, and a Re-detect changes the decisions themselves.
    function jobFinished(job) {
        // A previews job leaves every marker exactly where it was; a job with no kind at all is read as one that may
        // have moved them.
        if (job && job.kind && job.kind !== JOB_KIND_MARKERS) return;
        forgetShown();
        if (shown) readAgain(shown.item);
    }

    // Read one file's tab again. An open edit, a lock in flight or the open Lock dialog owns the screen: dropping
    // what the caches hold is enough then, and neither the times the user is typing nor the ones the dialog is
    // showing change underneath them.
    function readAgain(item) {
        if (editing || busy || pendingLock) return undefined;
        if (!shown || shown.item !== item) return undefined;
        // A read that failed leaves no payload, and it is exactly the state a job can put right: Re-detect is
        // offered there, so the file it re-reads has to reach the screen when that job lands.
        if (!shown.payload && shown.failed !== 'read') return undefined;
        return loadMarkersInspector(item);
    }

    function watchJobs() {
        if (jobsSocket || typeof window.io !== 'function') return;
        // Polling only, as app.js connects: a websocket pins a gunicorn thread for every open tab.
        jobsSocket = window.io('/jobs', { transports: ['polling'], reconnection: true });
        jobsSocket.on('job_completed', jobFinished);
        jobsSocket.on('job_failed', jobFinished);
    }

    // The header row as the state it is in now describes it, wherever that state changed.
    function refreshHeader() {
        if (shown && shown.payload) syncHeaderButtons(shown.payload, shown.item);
        else headerForNoPayload();
    }

    // The header row while nothing is on screen to act on: no file at all, one still being read, or one whose read
    // failed. ``shown.failed`` says which failure, because only one of them leaves anything worth pressing.
    function headerForNoPayload() {
        const working = workingReason();
        if (!shown) {
            headerButtonsBlocked(working || NO_FILE_YET);
            return;
        }
        headerButtonsBlocked(working || (shown.failed ? COULDNT_READ : STILL_LOADING));
        // A read that failed can be tried again as a job: it opens the file itself. A version that isn't on this
        // disk can't — the job would find no file either.
        const redetect = $('markersRedetectBtn');
        if (redetect && !working && shown.failed === 'read' && shown.item.media_file) setBlocked(redetect, '');
    }

    async function redetectItem() {
        if (!shown) return;
        const started = { key: shown.key, item: shown.item };
        const path = (shown.payload && shown.payload.canonical_path) || shown.item.media_file;
        if (!path) return;
        queueing = true;
        refreshHeader();
        let result = null;
        let failure = '';
        // Only the request is guarded: everything below describes a job that is already queued, and a throw there
        // would otherwise be reported as a job that never was.
        try {
            result = await apiPost('/api/markers/item/redetect', { path: path });
        } catch (error) {
            failure = error.message;
        }
        queueing = false;
        refreshHeader();
        if (failure) {
            showToast('Re-detect', `Couldn't queue it: ${failure}`, 'danger');
            return;
        }
        forget(started.key, path);
        showToast('Re-detect', 'Queued — see the Dashboard', 'success');
        const jobId = result && result.job_id;
        const toastBody = $('toastBody');
        if (jobId && toastBody) {
            const link = el('a', 'ms-1', String(jobId).substring(0, 8));
            link.href = '/?job=' + encodeURIComponent(jobId);
            toastBody.append(' ', link);
        }
        // The queued job hasn't run yet, so this brings back what any *other* job has changed since the tab last read
        // the file; jobFinished reads it again when this one lands, and the confirmation above doesn't wait on it.
        await readAgain(started.item);
    }

    function wireButtons() {
        const redetect = $('markersRedetectBtn');
        if (redetect) redetect.addEventListener('click', function () {
            if (sayWhyBlocked(redetect)) return;
            redetectItem();
        });
        const adjust = $('markersAdjustBtn');
        if (adjust) adjust.addEventListener('click', function () {
            if (sayWhyBlocked(adjust)) return;
            startEditing();
        });
        const lock = $('markersLockBtn');
        if (lock) lock.addEventListener('click', function () {
            if (sayWhyBlocked(lock)) return;
            if (lock.dataset.mode === 'unlock') askToUnlock();
            else askToLock();
        });
        const confirmUnlock = $('markersUnlockConfirm');
        if (confirmUnlock) confirmUnlock.addEventListener('click', unlock);
        const confirmLock = $('markersLockConfirm');
        if (confirmLock) confirmLock.addEventListener('click', lockNow);
        const lockModal = $('markersLockModal');
        // Dismissed with the ✕, the backdrop or Escape as well as the button: nothing is held on to either way.
        if (lockModal) lockModal.addEventListener('hidden.bs.modal', forgetPendingLock);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wireButtons);
    else wireButtons();

    window.loadMarkersInspector = loadMarkersInspector;
    // The Season view's Edit action (phase 4, Task 6) opens this same editor; the pure time helpers are exported
    // beside it so they can be pinned without a JS test runner.
    window.markersEditor = {
        openFile: editFile,
        // The open /jobs socket, so a test can call the handlers this file registered on it without a real job.
        jobsSocket: function () { return jobsSocket; },
        parseClock: parseClock,
        spokenTime: spokenTime,
        lengthText: lengthText,
        // Pure, and the one place the starting times are decided: exported so the clamp can be pinned on a file
        // shorter than the seed, which no rendered payload is.
        addSeed: addSeed,
    };
})();
