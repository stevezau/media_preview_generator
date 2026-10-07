/* Worker group settings editor and read-only dashboard activity. Runtime counts come from the server. */
(function () {
    'use strict';
    const JOBS = { previews: 'Video previews', intro_credits: 'Intro & Credits', loudness: 'Plex loudness' };
    const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const clone = value => JSON.parse(JSON.stringify(value));
    const escape = value => {
        const el = document.createElement('span');
        el.textContent = String(value ?? '');
        return el.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    };
    let snapshot = null;
    let draft = null;
    let dirty = false;
    let editing = null;
    let saving = false;
    let loading = null;
    let requestedEditor = new URLSearchParams(window.location.search).get('worker_group');
    const dashboardGroups = new Map();
    // One block per group member; workers, the row cap and idle line live here.
    const dashboardMembers = new Map();
    let pausedByGroup = {};
    let occupiedWorkerGroups = new Set();
    let groupSearch = '';
    let occupiedOnly = false;
    let showAllGroups = false;
    // Rows shown per member before the "Show N more" expander; problem rows are always shown.
    const DENSE_TABLE_ROW_CAP = 8;
    const EXPANDED_KEY = 'workerGroupsExpanded';
    let idleByMember = new Map();
    const memberNotes = new Map();
    let denseMode = false;
    let denseQueued = false;
    let groupSeq = 0;
    const settings = () => document.getElementById('workerGroupSettings');
    const dashboard = () => document.getElementById('workerGroupDashboard');

    const memberKey = (groupId, memberId) => `${groupId}:${memberId || 'unassigned'}`;
    const newId = prefix => window.crypto?.randomUUID?.().slice(0, 8) || prefix + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

    // Older servers and API-token tools may send the flat one-device shape; the UI only ever holds members.
    function normalize(data) {
        for (const group of data.groups || []) {
            if (!Array.isArray(group.members)) {
                group.members = [{ id: 'm1', resource: group.resource, device: group.device ?? null, count: group.count, job_types: group.job_types || [] }];
            }
            for (const key of ['resource', 'device', 'count', 'job_types']) delete group[key];
        }
        return data;
    }

    async function request(method, path = '', body) {
        const response = await fetch('/api/worker-groups' + path, {
            method, headers: { 'Content-Type': 'application/json', 'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '' },
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data.error || data.message || 'Worker settings could not be saved.');
            error.conflict = response.status === 409;
            throw error;
        }
        return data.groups ? normalize(data) : data;
    }

    function message(text, error = false) {
        for (const id of ['workerGroupMessage', 'workerGroupLiveMessage']) {
            const el = document.getElementById(id);
            if (!el) continue;
            el.textContent = text;
            el.className = 'small mt-2 ' + (error ? 'text-danger-emphasis' : 'text-body-secondary');
        }
    }

    const hardwareFor = member => (snapshot.hardware || []).find(item => item.device === member.device);
    const deviceMissing = member => member.resource === 'gpu' && !hardwareFor(member);
    const deviceIcon = member => (member.resource === 'cpu' ? 'cpu' : 'gpu-card');
    // The dashboard page does not load week_graph.js, so it keeps the same palette (CPU green, GPUs in detected order).
    const FALLBACK_GPU_COLORS = ['var(--run)', 'var(--t-intro)', 'var(--t-loud)', 'var(--accent)'];
    function deviceColor(member) {
        if (window.WeekGraph) return window.WeekGraph.deviceColor(member, snapshot.hardware);
        if (member.resource === 'cpu') return 'var(--ok)';
        const index = (snapshot.hardware || []).findIndex(item => item.device === member.device);
        return FALLBACK_GPU_COLORS[(index < 0 ? (snapshot.hardware || []).length : index) % FALLBACK_GPU_COLORS.length];
    }
    const totalWorkers = group => group.members.reduce((sum, member) => sum + member.count, 0);
    const groupJobTypes = group => [...new Set(group.members.flatMap(member => member.job_types))];

    function deviceName(member) {
        if (member.resource === 'cpu') return 'CPU';
        return hardwareFor(member)?.name || member.device || 'Unavailable GPU';
    }

    // "NVIDIA GeForce RTX 4090" -> "RTX 4090"; chips are narrow and the vendor adds nothing next to a model number.
    function shortDeviceName(member) {
        const name = deviceName(member);
        const model = name.replace(/^(NVIDIA( GeForce)?|Intel( Corporation)?|AMD( Radeon)?)\s+/i, '');
        return /\d/.test(model) ? model : name;
    }

    function memberStatus(group, member) {
        const groupRow = (snapshot.capacity?.groups || []).find(item => item.id === group.id) || {};
        const row = (groupRow.members || []).find(item => item.id === member.id) || {};
        return { ...row, busy: row.busy ?? 0, finishing: row.finishing ?? 0, available: row.available ?? 0 };
    }

    // "3 GPU + 10 CPU": the System card's one-line summary of a group.
    const memberSummary = group => group.members.map(member => `${member.count} ${member.resource === 'cpu' ? 'CPU' : 'GPU'}`).join(' + ');

    function hours(group) {
        if (group.availability.mode === 'always') return 'Always available';
        return group.availability.windows.map(window => {
            const days = window.days.length === 7 ? 'Daily' : window.days.map(day => DAYS[day]).join(', ');
            return `${days} ${window.start}–${window.end}${window.end < window.start ? ' next day' : ''}`;
        }).join('; ');
    }

    const timezoneLabel = () => snapshot.timezone_label || snapshot.timezone || 'App timezone';

    function nextTime(value) {
        if (!value) return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        try {
            if (!snapshot.timezone || snapshot.timezone === 'Local time') throw new RangeError('Local timezone');
            return new Intl.DateTimeFormat(undefined, {
                weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: snapshot.timezone,
            }).format(date) + ' · ' + timezoneLabel();
        } catch (_) {
            // The server's local zone may have no IANA name. Keep this opening's wall time and DST offset.
            const parts = String(value).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/);
            if (!parts) return String(value);
            const weekday = new Intl.DateTimeFormat(undefined, { weekday: 'short', timeZone: 'UTC' })
                .format(new Date(`${parts[1]}T${parts[2]}:00Z`));
            return `${weekday} ${parts[2]} · Local time (UTC${parts[3] === 'Z' ? '+00:00' : parts[3]})`;
        }
    }

    function status(group) {
        const row = (snapshot.capacity?.groups || []).find(item => item.id === group.id) || {};
        const labels = {
            disabled: 'Disabled', outside_hours: 'Outside hours', scheduled: 'Outside hours',
            hardware_unavailable: 'Hardware unavailable', unavailable: 'Hardware unavailable',
            active: 'Within group hours', off_hours: 'Outside hours', draining: 'Finishing current files',
            available: 'Available', open: 'Available', busy: 'Workers busy', paused: 'Globally paused',
            quiet_hours: 'Global pause schedule active',
        };
        const nextOpening = group.enabled && group.availability.mode === 'scheduled'
            && ['off_hours', 'outside_hours', 'scheduled', 'draining'].includes(row.state)
            && Date.parse(row.next_available_at) > Date.now() ? row.next_available_at : null;
        return {
            ...row, next_available_at: nextOpening, available: row.available ?? 0, busy: row.busy ?? 0, finishing: row.finishing ?? 0,
            label: !group.enabled ? 'Disabled' : row.state === 'draining' && nextOpening ? 'Outside hours' : labels[row.state] || 'Configured',
        };
    }

    function activity(state) {
        const parts = [];
        if (!snapshot.processing_paused && state.available) parts.push(`${state.available} available`);
        const paused = snapshot.processing_paused ? state.busy : Math.min(state.busy, pausedByGroup[state.id] || 0);
        if (state.busy > paused) parts.push(`${state.busy - paused} running`);
        if (paused) parts.push(`${paused} paused`);
        if (state.finishing) parts.push(`${state.finishing} finishing${snapshot.processing_paused ? ' after resume' : ''}`);
        return parts;
    }

    const STATE_TONES = {
        'Available': 'ok', 'Within group hours': 'ok', 'Workers busy': 'run', 'Finishing current files': 'run',
        'Globally paused': 'warn', 'Global pause schedule active': 'warn', 'Hardware unavailable': 'bad',
    };
    const PRESETS = { all: [0, 1, 2, 3, 4, 5, 6], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6] };
    // Same device, same colour in every group, strip and legend (week_graph.js owns the palette).
    const memberLabel = (group, member) => `${group.name} · ${shortDeviceName(member)} ×${member.count}`;
    const memberLanes = group => group.members.map(member => ({
        color: deviceColor(member), label: memberLabel(group, member), segments: window.WeekGraph.groupSegments(group),
    }));

    function memberChip(member) {
        const missing = deviceMissing(member);
        return `<span class="mchip${missing ? ' is-missing' : ''}" style="--c:${deviceColor(member)}" title="${escape(deviceName(member) + (missing ? ' (not detected)' : ''))}"><i class="dot" aria-hidden="true"></i><i class="bi bi-${deviceIcon(member)}" aria-hidden="true"></i><span class="mchip-name">${escape(shortDeviceName(member))}</span><span class="mchip-x">×${member.count}</span>${capabilityIcons(member.job_types)}</span>`;
    }

    function settingsGroupMeta(group) {
        const state = status(group);
        const notes = [...activity(state), state.next_available_at ? `Next ${nextTime(state.next_available_at)}` : ''].filter(Boolean);
        // "Within group hours" on an always-on group says nothing the hours line below does not.
        const redundant = group.availability.mode === 'always' && ['Within group hours', 'Available'].includes(state.label);
        const chip = redundant ? '' : `<span class="wg-state ${STATE_TONES[state.label] || ''}">${escape(state.label)}</span>`;
        return `${chip}${notes.map(note => `<span>${escape(note)}</span>`).join('')}<span class="wg-hours"><i class="bi bi-clock" aria-hidden="true"></i> ${escape(hours(group))}</span>`;
    }

    // The group's own week bar, only for groups with weekly hours; a global pause is overlaid when one is set.
    function settingsGroupStrip(group) {
        if (group.availability.mode !== 'scheduled' || !window.WeekGraph) return '';
        return `<div class="wg-strip">${window.WeekGraph.render(memberLanes(group), window.WeekGraph.pauseSegments(), { timeZone: snapshot.timezone })}</div>`;
    }

    function renderRows(container) {
        const groups = draft || snapshot.groups;
        const markup = groups.map(group => `<div class="wg-item${editing === group.id ? ' open' : ''}${group.enabled ? '' : ' is-off'}">
            <div class="worker-group-row" data-group-id="${escape(group.id)}">
            <span class="wg-ico" aria-hidden="true"><i class="bi bi-${group.members.length > 1 ? 'stack' : group.members[0] ? deviceIcon(group.members[0]) : 'collection'}"></i></span>
            <div class="worker-group-description"><strong class="wg-name">${escape(group.name)}</strong><div class="mchips">${group.members.map(memberChip).join('')}</div><div class="wg-meta">${settingsGroupMeta(group)}</div></div>
            <div class="worker-group-actions"><div class="worker-group-capacity"><span class="worker-group-control-label">Workers</span><span class="worker-group-count" aria-label="Configured workers">${totalWorkers(group)}</span></div><button type="button" class="btn btn-sm btn-outline-secondary worker-group-edit" data-edit="${escape(group.id)}" aria-label="Edit ${escape(group.name)}" aria-expanded="${editing === group.id}" aria-controls="workerGroupEditor" ${saving ? 'disabled' : ''}><i class="bi bi-pencil" aria-hidden="true"></i> Edit <i class="bi bi-chevron-down wg-chevron" aria-hidden="true"></i></button><label class="form-check form-switch mb-0 wg-switch" title="Enabled"><input class="form-check-input" type="checkbox" role="switch" data-enable="${escape(group.id)}" aria-label="Enable ${escape(group.name)}" ${group.enabled ? 'checked' : ''} ${saving ? 'disabled' : ''}></label></div>
            ${settingsGroupStrip(group)}
        </div></div>`).join('') || `<div class="wg-empty"><i class="bi bi-stack" aria-hidden="true"></i><strong>No worker groups yet</strong><span>Jobs wait until a group can run them.</span><span class="wg-empty-acts"><button type="button" class="btn btn-sm btn-primary" data-empty-add ${saving ? 'disabled' : ''}><i class="bi bi-plus-lg" aria-hidden="true"></i> Add group</button>${needsLoudnessGroup() ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-empty-add-cpu><i class="bi bi-soundwave" aria-hidden="true"></i> Add CPU group for loudness</button>' : ''}</span></div>`;
        if (container._groupMarkup === markup) return;
        container._groupMarkup = markup;
        const editor = document.getElementById('workerGroupEditor');
        const focused = editor?.contains(document.activeElement) ? document.activeElement : null;
        const selection = focused && focused.type === 'text' ? [focused.selectionStart, focused.selectionEnd] : null;
        // Row labels update while typing; retain the editor node, focus and text selection.
        if (editor && container.contains(editor)) container.after(editor);
        if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(container);
        container.innerHTML = markup;
        window._initBootstrapTooltips?.(container);
        container.querySelectorAll('[data-edit]').forEach(button => button.addEventListener('click', () => edit(button.dataset.edit)));
        container.querySelectorAll('[data-enable]').forEach(input => input.addEventListener('change', () => {
            ensureDraft(); dirty = true; draft.find(group => group.id === input.dataset.enable).enabled = input.checked; renderSettings();
        }));
        positionEditor();
        if (focused?.isConnected) {
            focused.focus({ preventScroll: true });
            if (selection) focused.setSelectionRange(...selection);
        }
    }

    // One lane per member of each enabled group, so overlapping hours and gaps are visible at a glance.
    function renderWeekOverview(groups) {
        const graph = document.getElementById('workerWeekGraph');
        const legend = document.getElementById('workerWeekLegend');
        if (!graph || !window.WeekGraph) return;
        const enabled = groups.filter(group => group.enabled);
        const lanes = enabled.flatMap(memberLanes);
        graph.innerHTML = lanes.length ? window.WeekGraph.render(lanes, window.WeekGraph.pauseSegments(), { timeZone: snapshot.timezone })
            : '<div class="wkg-empty">No enabled groups. Jobs wait until a compatible group is available.</div>';
        const devices = new Map();
        for (const member of enabled.flatMap(group => group.members)) devices.set(member.resource === 'cpu' ? 'cpu' : member.device, member);
        legend.innerHTML = [...devices.values()].map(member => `<span><i class="swatch" style="--c:${deviceColor(member)}"></i>${escape(deviceName(member))}</span>`).join('')
            + '<span><i class="swatch swatch-pause"></i>Global pause</span><span><i class="swatch swatch-now"></i>Now</span>';
        document.querySelectorAll('#workerWeekTz, [data-week-tz]').forEach(node => { node.innerHTML = `<i class="bi bi-globe2" aria-hidden="true"></i> ${escape(timezoneLabel())}`; });
    }

    function paintEditorPreview() {
        const preview = document.getElementById('workerGroupPreview');
        const group = (draft || snapshot.groups).find(item => item.id === editing);
        if (!preview || !group || !window.WeekGraph) return;
        preview.innerHTML = group.members.length
            ? window.WeekGraph.render(memberLanes(group), [], { timeZone: snapshot.timezone })
            : '<p class="wkg-empty">No devices yet.</p>';
    }

    function positionEditor() {
        const editor = document.getElementById('workerGroupEditor');
        const rows = document.getElementById('workerGroupRows');
        if (!editor || !rows) return;
        const selected = [...rows.querySelectorAll('[data-group-id]')].find(row => row.dataset.groupId === editing);
        if (selected) {
            if (selected.nextElementSibling !== editor) selected.after(editor);
        } else if (rows.contains(editor)) rows.after(editor);
    }

    function ensureDashboard() {
        const mount = dashboard();
        if (!mount) return null;
        if (!document.getElementById('workerGroupLiveRows')) {
            dashboardGroups.clear();
            mount.innerHTML = `<p id="workerGroupHold" class="worker-group-hold small text-warning-emphasis mb-2" role="status" hidden></p>
                <div id="workerGroupFilters" class="worker-group-filters" hidden>
                    <div class="worker-group-filter-row"><label class="worker-group-search"><span class="visually-hidden">Search worker groups</span><input type="search" class="form-control form-control-sm" id="workerGroupSearch" placeholder="Search groups" aria-label="Search worker groups"></label>
                    <div class="btn-group btn-group-sm" role="group" aria-label="Worker groups shown"><button type="button" class="btn btn-outline-secondary" data-group-filter="all">All groups</button><button type="button" class="btn btn-outline-secondary" data-group-filter="occupied">Occupied</button></div><button type="button" class="btn btn-sm btn-link" data-group-clear hidden>Clear filters</button></div>
                    <div class="worker-group-results small text-body-secondary"><span id="workerGroupSummary" role="status" aria-live="polite"></span><button type="button" class="btn btn-sm btn-link" data-group-outside hidden></button><button type="button" class="btn btn-sm btn-link" data-group-reveal hidden></button></div>
                </div><div id="workerGroupCols" class="wg-cols" aria-hidden="true" hidden><span>Worker</span><span>Media</span><span>Task</span><span>Progress</span><span>Job</span><span></span></div><div id="workerGroupLiveRows"></div><p id="workerGroupEmpty" class="text-body-secondary" hidden>No worker groups configured. Jobs wait until a compatible group is available.</p><div id="workerGroupLiveWarnings" class="small text-warning-emphasis mt-2"></div><div id="workerGroupLiveMessage" role="status" aria-live="polite"></div>`;
            mount.querySelector('#workerGroupSearch').addEventListener('input', event => {
                groupSearch = event.target.value.trim().toLocaleLowerCase(); showAllGroups = false; renderGroupVisibility();
            });
            mount.querySelector('#workerGroupFilters').addEventListener('click', event => {
                const filter = event.target.closest('[data-group-filter]');
                if (filter) { occupiedOnly = filter.dataset.groupFilter === 'occupied'; showAllGroups = false; }
                if (event.target.closest('[data-group-clear]')) { groupSearch = ''; occupiedOnly = false; showAllGroups = false; }
                if (event.target.closest('[data-group-reveal]')) showAllGroups = !showAllGroups;
                if (event.target.closest('[data-group-outside]')) { groupSearch = ''; occupiedOnly = true; showAllGroups = true; }
                if (!groupSearch) mount.querySelector('#workerGroupSearch').value = '';
                renderGroupVisibility();
            });
        }
        const live = document.getElementById('workerGroupLiveRows');
        if (!live._denseObserver) {
            // Cards are created and patched by app.js; the table re-applies its ordering and row cap after each change.
            live._denseObserver = new MutationObserver(queueDenseRows);
            live._denseObserver.observe(live, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'data-status'] });
        }
        return live;
    }

    // Held in memory too so expanding still works when storage is blocked.
    const expandedIds = (() => {
        try { return new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]')); } catch (_) { return new Set(); }
    })();

    function setExpanded(id, open) {
        if (open) expandedIds.add(id); else expandedIds.delete(id);
        try { localStorage.setItem(EXPANDED_KEY, JSON.stringify([...expandedIds])); } catch (_) { /* storage unavailable */ }
    }

    function queueDenseRows() {
        if (denseQueued) return;
        denseQueued = true;
        requestAnimationFrame(() => { denseQueued = false; applyDenseRows(); });
    }

    const isProblemSlot = slot => !!slot.querySelector('.wk.border-warning');
    const isRunningSlot = slot => !!slot.querySelector('.wk.busy');

    // Busy workers first, rows beyond the cap folded behind an in-place expander; fallback/odd-state rows stay visible.
    function applyDenseRows() {
        const expanded = expandedIds;
        for (const [id, entry] of dashboardMembers) {
            const slots = [...entry.host.querySelectorAll(':scope > .worker-slot')];
            const rows = slots.filter(slot => !slot.querySelector('.wk.idle') || isProblemSlot(slot));
            const sorted = denseMode ? rows.map((slot, index) => ({ slot, index }))
                .sort((a, b) => (isProblemSlot(b.slot) - isProblemSlot(a.slot)) || (isRunningSlot(b.slot) - isRunningSlot(a.slot)) || a.index - b.index)
                .map(item => item.slot) : [];
            const open = expanded.has(id);
            const overCap = denseMode && sorted.length > DENSE_TABLE_ROW_CAP;
            const shown = new Set(sorted.filter((slot, index) => !overCap || open || index < DENSE_TABLE_ROW_CAP || isProblemSlot(slot)));
            sorted.forEach((slot, index) => { if (slot.style.order !== String(index)) slot.style.order = String(index); });
            for (const slot of slots) {
                const capped = denseMode && rows.includes(slot) && !shown.has(slot);
                if (slot.hasAttribute('data-wg-capped') !== capped) slot.toggleAttribute('data-wg-capped', capped);
                if (!denseMode && slot.style.order) slot.style.order = '';
            }
            const hidden = sorted.filter(slot => !shown.has(slot));
            const running = hidden.filter(isRunningSlot).length;
            if (!entry.host.id) entry.host.id = `wgWorkers${++groupSeq}`;
            const button = entry.more;
            button.hidden = !overCap;
            if (!overCap) continue;
            const label = open ? 'Show less' : `Show ${hidden.length} more`;
            const suffix = !open && running ? ` · ${running} running` : '';
            const markup = `<i class="bi bi-chevron-${open ? 'up' : 'down'}" aria-hidden="true"></i><span>${label}</span>${suffix ? `<span class="wg-more-run">${suffix}</span>` : ''}`;
            if (button._markup !== markup) { button._markup = markup; button.innerHTML = markup; }
            button.setAttribute('aria-expanded', String(open));
            button.setAttribute('aria-controls', entry.host.id);
        }
    }

    // One muted line per member for workers with nothing to do; they are never listed as full rows.
    function renderIdleRows() {
        for (const [id, entry] of dashboardMembers) {
            const ids = idleByMember.get(id) || [];
            entry.idle.hidden = !ids.length;
            const markup = ids.length
                ? `<i class="bi bi-moon-stars" aria-hidden="true"></i><span class="wg-idle-label">${ids.length} ${ids.length === 1 ? 'worker' : 'workers'} idle</span><span class="wg-idle-chips">${ids.map(n => `<span class="wg-idle-n">#${escape(n)}</span>`).join('')}</span>` : '';
            if (entry.idle._markup !== markup) { entry.idle._markup = markup; entry.idle.innerHTML = markup; }
        }
    }

    function dashboardGroup(id, name) {
        const rows = ensureDashboard();
        if (!rows) return null;
        const key = String(id || 'unassigned');
        if (!dashboardGroups.has(key)) {
            const section = document.createElement('section');
            section.className = 'worker-group-section';
            section.dataset.workerGroupShell = key;
            const header = document.createElement('div');
            header.className = 'worker-group-dashboard-header';
            const members = document.createElement('div');
            members.className = 'wg-members';
            const footer = document.createElement('div');
            footer.className = 'wg-group-foot';
            section.append(header, members, footer);
            rows.append(section);
            dashboardGroups.set(key, { section, header, members, footer, name });
            header.innerHTML = `<div class="worker-group-description"><strong>${escape(name || 'Workers without group details')}</strong><span class="small text-body-secondary">Group details unavailable</span></div>`;
            renderGroupVisibility();
        }
        return dashboardGroups.get(key);
    }

    function dashboardMember(groupId, groupName, memberId) {
        const group = dashboardGroup(groupId, groupName);
        if (!group) return null;
        const key = memberKey(String(groupId || 'unassigned'), memberId);
        if (!dashboardMembers.has(key)) {
            const block = document.createElement('div');
            block.className = 'wg-mem';
            block.dataset.memberBlock = key;
            const head = document.createElement('div');
            head.className = 'wg-mem-head';
            const host = document.createElement('div');
            host.className = 'worker-group-workers';
            host.dataset.groupWorkers = key;
            const more = document.createElement('button');
            more.type = 'button';
            more.className = 'wg-more';
            more.hidden = true;
            more.addEventListener('click', () => {
                setExpanded(key, more.getAttribute('aria-expanded') !== 'true');
                applyDenseRows();
            });
            const idle = document.createElement('div');
            idle.className = 'wg-idle-row';
            idle.dataset.groupIdle = key;
            idle.hidden = true;
            block.append(head, host, more, idle);
            group.members.append(block);
            dashboardMembers.set(key, { groupKey: String(groupId || 'unassigned'), block, head, host, more, idle });
        }
        return dashboardMembers.get(key);
    }

    function setWorkerActivity(workers) {
        const next = {};
        occupiedWorkerGroups = new Set((workers || []).filter(worker => worker.status !== 'idle').map(worker => String(worker.group_id || 'unassigned')));
        idleByMember = new Map();
        for (const worker of workers || []) {
            if (worker.status === 'processing' || worker.fallback_active) continue;
            const key = memberKey(String(worker.group_id || 'unassigned'), worker.member_id);
            idleByMember.set(key, [...(idleByMember.get(key) || []), worker.worker_id]);
        }
        renderIdleRows();
        for (const worker of workers || []) {
            if (worker.group_id && worker.paused && !worker.retiring && worker.status !== 'idle') {
                next[worker.group_id] = (next[worker.group_id] || 0) + 1;
            }
        }
        if (Object.keys(next).length === Object.keys(pausedByGroup).length
            && Object.keys(next).every(id => next[id] === pausedByGroup[id])) { renderGroupVisibility(); return; }
        pausedByGroup = next;
        renderDashboard();
    }

    function getDashboardWorkerHost(groupId, groupName, memberId) {
        const entry = dashboardMember(groupId, groupName, memberId);
        if (entry) document.getElementById('workerGroupEmpty').hidden = true;
        return entry?.host || null;
    }

    function warnings() {
        return (snapshot.warnings || []).map(warning => typeof warning === 'string' ? warning : warning.message || '').filter(Boolean);
    }

    function replaceDashboardHeader(entry, markup) {
        if (entry.header._markup === markup) return;
        if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(entry.header);
        entry.header.innerHTML = markup;
        entry.header._markup = markup;
        window._initBootstrapTooltips?.(entry.header);
    }

    function renderGroupVisibility() {
        const controls = document.getElementById('workerGroupFilters');
        const rows = document.getElementById('workerGroupLiveRows');
        if (!controls || !rows) return;
        const configuredOrder = new Map((snapshot?.groups || []).map((group, index) => [String(group.id), index]));
        const entries = [...dashboardGroups.entries()].sort(([a], [b]) =>
            (configuredOrder.get(a) ?? Number.MAX_SAFE_INTEGER) - (configuredOrder.get(b) ?? Number.MAX_SAFE_INTEGER));
        entries.forEach(([, entry], index) => {
            if (rows.children[index] !== entry.section) rows.insertBefore(entry.section, rows.children[index] || null);
        });
        const occupied = ([id, entry]) => entry.occupied || occupiedWorkerGroups.has(id);
        const matches = entries.filter(pair => (!occupiedOnly || occupied(pair)) &&
            (!groupSearch || (pair[1].searchText || pair[1].name || '').toLocaleLowerCase().includes(groupSearch)));
        const visible = showAllGroups ? matches : matches.slice(0, 6);
        const visibleIds = new Set(visible.map(([id]) => id));
        entries.forEach(([id, entry]) => {
            entry.section.hidden = !visibleIds.has(id);
            const index = visible.findIndex(([visibleId]) => id === visibleId);
            entry.section.classList.toggle('worker-group-row-start-three', index >= 0 && index % 3 === 0);
            entry.section.classList.toggle('worker-group-row-start-two', index >= 0 && index % 2 === 0);
        });
        rows.dataset.visibleCount = visible.length;
        controls.hidden = entries.length <= 6 && !groupSearch && !occupiedOnly;
        const occupiedTotal = entries.filter(occupied).length;
        const outsideCount = entries.filter(pair => !visibleIds.has(pair[0]) && occupied(pair)).length;
        const filtered = groupSearch || occupiedOnly;
        const summary = `${entries.length} groups · ${occupiedTotal} occupied. ` + (matches.length ?
            `Showing ${visible.length} of ${matches.length}${filtered ? ' matching groups' : ''}.` : 'No matching groups.');
        const summaryNode = document.getElementById('workerGroupSummary');
        if (summaryNode.textContent !== summary) summaryNode.textContent = summary;
        const outside = controls.querySelector('[data-group-outside]');
        outside.hidden = !outsideCount;
        outside.textContent = `${outsideCount} occupied ${outsideCount === 1 ? 'group' : 'groups'} outside this view`;
        const reveal = controls.querySelector('[data-group-reveal]');
        reveal.hidden = matches.length <= 6;
        reveal.textContent = showAllGroups ? 'Show first 6' : `Show all ${matches.length}`;
        controls.querySelector('[data-group-clear]').hidden = !filtered;
        controls.querySelectorAll('[data-group-filter]').forEach(button => {
            const selected = (button.dataset.groupFilter === 'occupied') === occupiedOnly;
            button.classList.toggle('active', selected);
            button.setAttribute('aria-pressed', String(selected));
        });
    }

    const CAPABILITIES = [
        ['previews', 'film', 'Previews', 'previews'],
        ['intro_credits', 'skip-forward', 'Intro & credits', 'intro'],
        ['loudness', 'soundwave', 'Plex loudness', 'loud'],
    ];

    // One tile per job type: type colour when it may run, dim grey when not.
    function capabilityIcons(jobTypes) {
        return '<span class="caps">' + CAPABILITIES.map(([kind, icon, label, tone]) => {
            const allowed = jobTypes.includes(kind);
            const text = allowed ? label : `${label} (not allowed)`;
            return `<span class="cap${allowed ? ' ' + tone : ''}" role="img" aria-label="${escape(text)}" data-bs-toggle="tooltip" data-bs-title="${escape(text)}"><i class="bi bi-${icon}" aria-hidden="true"></i></span>`;
        }).join('') + '</span>';
    }

    // The System card is read-only: one line per group, with Enable for a disabled one. Steppers live on the Workers panel.
    function renderSystemGroups() {
        const mount = document.getElementById('systemWorkerGroups');
        if (!mount || !snapshot) return;
        const section = document.getElementById('systemWorkerGroupsSection');
        const markup = snapshot.groups.map(group => {
            const state = status(group);
            const action = group.enabled
                ? `<span class="pool-state ${STATE_TONES[state.label] || ''}">${escape(state.label === 'Configured' ? 'On' : state.label)}</span>`
                : `<button type="button" class="btn dash-btn-sm" data-group-enable ${saving ? 'disabled' : ''} aria-label="Enable ${escape(group.name)}" title="Enable with its saved worker counts"><i class="bi bi-power" aria-hidden="true"></i>Enable</button>`;
            return `<div class="pool-row${group.enabled ? '' : ' is-off'}" data-system-group="${escape(group.id)}">
                <i class="bi bi-${group.members.length > 1 ? 'stack' : group.members[0] ? deviceIcon(group.members[0]) : 'collection'} pool-ico" aria-hidden="true"></i>
                <span class="pool-lbl"><span class="pool-name" title="${escape(group.name)}">${escape(group.name)}</span><small title="${escape(memberSummary(group))}">${escape(memberSummary(group))}</small></span>
                ${action}</div>`;
        }).join('');
        if (section) section.hidden = !snapshot.groups.length;
        if (mount._markup === markup) return;
        if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(mount);
        mount._markup = markup;
        mount.innerHTML = markup;
        window._initBootstrapTooltips?.(mount);
    }

    async function postScale(path, changes) {
        if (saving) return;
        saving = true;
        renderDashboard();
        try {
            const data = await request('POST', path, changes);
            if (!snapshot || data.revision >= snapshot.revision) snapshot = data;
            message(data.warning || '', !!data.warning);
        } catch (error) {
            message(error.message, true);
            if (typeof showToast === 'function') showToast('Error', error.message, 'danger');
            await load(true);
        } finally {
            saving = false;
            renderDashboard();
        }
    }

    const scaleGroup = (id, changes) => postScale(`/${encodeURIComponent(id)}/scale`, changes);
    const scaleMember = (groupId, memberId, delta) =>
        postScale(`/${encodeURIComponent(groupId)}/members/${encodeURIComponent(memberId)}/scale`, { delta });

    function memberStepper(group, member, current) {
        const limit = snapshot.limits?.[member.resource] || 32;
        const name = deviceName(member);
        return `<span class="stepper sm" role="group" aria-label="Workers on ${escape(name)}">
            <button type="button" data-member-scale="-1" ${saving || current <= 1 ? 'disabled' : ''} aria-label="Remove one worker from ${escape(name)}" title="${current <= 1 ? 'Remove the device in Settings to use zero.' : 'Remove one worker'}"><i class="bi bi-dash" aria-hidden="true"></i></button>
            <output aria-live="polite" aria-label="Workers on ${escape(name)}">${current}</output>
            <button type="button" data-member-scale="1" ${saving || current >= limit ? 'disabled' : ''} aria-label="Add one worker to ${escape(name)}" title="${current >= limit ? 'At the worker limit' : 'Add one worker'}"><i class="bi bi-plus" aria-hidden="true"></i></button>
        </span>`;
    }

    function memberHead(group, member) {
        const state = memberStatus(group, member);
        const missing = deviceMissing(member) || ['hardware_unavailable', 'unavailable'].includes(state.state);
        const name = deviceName(member);
        const occupancy = `${state.busy} of ${member.count} configured ${member.count === 1 ? 'worker' : 'workers'} busy on ${name}.`;
        const chip = missing
            ? `<span class="chip warn" tabindex="0" role="img" aria-label="${escape(name)} not detected" data-bs-toggle="tooltip" data-bs-title="Its jobs wait; the other devices keep working."><i class="bi bi-exclamation-triangle" aria-hidden="true"></i>Not detected</span>`
            : `<span class="occ-chip" data-member-indicator role="img" aria-label="${escape(occupancy)}" data-bs-toggle="tooltip" data-bs-title="${escape(occupancy)}">${state.busy}<small>/ ${member.count}</small></span>`;
        const stepper = group.enabled ? memberStepper(group, member, member.count) : '';
        return `<div class="mem-h${missing ? ' miss' : ''}"><span class="devname"><i class="bi bi-${deviceIcon(member)}" aria-hidden="true"></i><span class="nm" title="${escape(name)}">${escape(name)}</span></span>${missing ? '' : capabilityIcons(member.job_types)}${chip}${stepper}</div>${missing ? `<p class="msg warn"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i><span>${escape(name)}: GPU not detected. Its jobs wait; other devices keep working.</span></p>` : ''}`;
    }

    // A member that is no longer configured keeps its row while its last files finish.
    function orphanHead(key) {
        const [groupId, memberId] = key.split(':');
        const groupRow = (snapshot.capacity?.groups || []).find(item => item.id === groupId) || {};
        const row = (groupRow.members || []).find(item => item.id === memberId);
        if (!row) return '<div class="mem-h"><span class="devname"><i class="bi bi-question-circle" aria-hidden="true"></i><span class="nm">Device details unavailable</span></span></div>';
        return `<div class="mem-h gone"><span class="devname"><i class="bi bi-${row.resource === 'cpu' ? 'cpu' : 'gpu-card'}" aria-hidden="true"></i><span class="nm">Removed device</span></span><span class="chip warn">${row.finishing} finishing${snapshot.processing_paused ? ' after resume' : ''}</span></div>`;
    }

    function replaceMarkup(node, markup) {
        if (node._markup === markup) return;
        const focused = node.contains(document.activeElement) ? document.activeElement : null;
        const focusKey = focused?.dataset?.memberScale;
        if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(node);
        node.innerHTML = markup;
        node._markup = markup;
        window._initBootstrapTooltips?.(node);
        // A stepper press re-renders its own button; keep keyboard focus on the same control.
        if (focusKey) node.querySelector(`[data-member-scale="${focusKey}"]:not(:disabled)`)?.focus({ preventScroll: true });
    }

    function renderDashboard() {
        if (!dashboard() || !snapshot) return;
        ensureDashboard();
        const hold = document.getElementById('workerGroupHold');
        hold.hidden = !snapshot.processing_paused;
        const owners = (snapshot.pause_reasons || []).map(reason => reason === 'quiet_hours' ? 'global pause schedule' : 'manual pause');
        const resumeHint = owners.includes('global pause schedule')
            ? owners.includes('manual pause') ? 'Resume processing and wait for the pause schedule to end.' : 'Processing resumes when the pause schedule ends.'
            : 'Resume processing to use available groups.';
        hold.textContent = 'Processing paused' + (owners.length ? ': ' + owners.join(' and ') : '') + '. ' + resumeHint;
        const present = new Set();
        const presentMembers = new Set();
        for (const group of snapshot.groups) {
            present.add(group.id);
            const entry = dashboardGroup(group.id, group.name);
            entry.header.dataset.groupId = group.id;
            delete entry.header.dataset.retiredGroup;
            const state = status(group);
            const counts = activity(state);
            const total = totalWorkers(group);
            entry.occupied = state.busy > 0 || state.finishing > 0;
            entry.searchText = [group.name, ...group.members.flatMap(member => [member.resource, deviceName(member)]), ...groupJobTypes(group).map(kind => JOBS[kind] || kind), hours(group)].join(' ');
            const exceptions = [];
            for (const count of counts) {
                if (!/^\d+ (running|available)$/.test(count)) exceptions.push(`<span>${escape(count)}</span>`);
            }
            const stateLabel = ['Within group hours', 'Available', 'Workers busy', 'Configured'].includes(state.label) ? '' :
                `<span class="worker-group-state">${escape(state.label)}${state.next_available_at ? ' · Next ' + escape(nextTime(state.next_available_at)) : ''}</span>`;
            const occupancy = `${state.busy} of ${total} configured ${total === 1 ? 'worker' : 'workers'} busy. Worker counts set simultaneous tasks, not CPU cores.`;
            const clock = group.availability.mode === 'always' ? '' :
                `<span class="cap-static" role="img" aria-label="${escape(hours(group))}" data-bs-toggle="tooltip" data-bs-title="${escape(hours(group) + ' · ' + timezoneLabel())}"><i class="bi bi-clock" aria-hidden="true"></i></span>`;
            const markup = `<div class="g-h"><div class="worker-group-description ttl"><strong class="g-name">${escape(group.name)}</strong></div>
                <div class="g-tools">${clock}${capabilityIcons(groupJobTypes(group))}<span class="occ-chip" data-group-indicator="configured" role="img" aria-label="${escape(occupancy)}" data-bs-toggle="tooltip" data-bs-title="${escape(occupancy)}">${state.busy}<small>/ ${total}</small></span></div></div>${exceptions.length ? `<div class="worker-group-counts small">${exceptions.join('')}</div>` : ''}${stateLabel ? `<div class="worker-group-availability small">${stateLabel}</div>` : ''}`;
            replaceDashboardHeader(entry, markup);
            for (const member of group.members) {
                const key = memberKey(group.id, member.id);
                presentMembers.add(key);
                const block = dashboardMember(group.id, group.name, member.id);
                block.block.dataset.memberGroup = group.id;
                block.block.dataset.memberId = member.id;
                block.block.style.setProperty('--c', deviceColor(member));
                block.block.classList.remove('gone');
                replaceMarkup(block.head, memberHead(group, member));
            }
            const capRow = (snapshot.capacity?.groups || []).find(item => item.id === group.id) || {};
            for (const row of capRow.members || []) {
                if (group.members.some(member => member.id === row.id) || !(row.finishing || row.busy)) continue;
                const key = memberKey(group.id, row.id);
                presentMembers.add(key);
                const block = dashboardMember(group.id, group.name, row.id);
                block.block.classList.add('gone');
                replaceMarkup(block.head, orphanHead(key));
            }
            replaceMarkup(entry.footer, group.enabled ? '' : `<button type="button" class="btn dash-btn-sm" data-group-enable="${escape(group.id)}" ${saving ? 'disabled' : ''} aria-label="Enable ${escape(group.name)}" title="Enable with its saved worker counts"><i class="bi bi-power" aria-hidden="true"></i>Enable</button>`);
        }
        for (const state of snapshot.capacity?.groups || []) {
            if (present.has(state.id) || !state.finishing) continue;
            present.add(state.id);
            const entry = dashboardGroup(state.id, state.name);
            delete entry.header.dataset.groupId;
            entry.header.dataset.retiredGroup = state.id;
            entry.occupied = true;
            entry.searchText = [state.name, ...(state.members || []).flatMap(member => [member.resource, member.device])].filter(Boolean).join(' ');
            const devices = (state.members || []).map(member => member.resource === 'cpu' ? 'CPU' : member.device || 'GPU').join(' + ');
            replaceDashboardHeader(entry, `<div class="worker-group-description"><strong>${escape(state.name || 'Removed group')}</strong><span class="small text-body-secondary">Removed · ${state.finishing} finishing${snapshot.processing_paused ? ' after resume' : ''}${devices ? ' · ' + escape(devices) : ''}</span></div>`);
            for (const row of state.members || []) {
                if (!(row.finishing || row.busy)) continue;
                const key = memberKey(state.id, row.id);
                presentMembers.add(key);
                const block = dashboardMember(state.id, state.name, row.id);
                block.block.classList.add('gone');
                replaceMarkup(block.head, orphanHead(key));
            }
        }
        for (const [key, entry] of dashboardMembers) {
            if (presentMembers.has(key)) continue;
            if (!entry.host.childElementCount) {
                if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(entry.block);
                entry.block.remove(); dashboardMembers.delete(key);
            } else {
                entry.block.classList.add('gone');
                replaceMarkup(entry.head, orphanHead(key));
            }
        }
        for (const [id, entry] of dashboardGroups) {
            if (present.has(id)) continue;
            const hasWorkers = [...dashboardMembers.values()].some(member => member.groupKey === id && member.host.childElementCount);
            if (!hasWorkers) {
                if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(entry.header);
                for (const [key, member] of dashboardMembers) if (member.groupKey === id) dashboardMembers.delete(key);
                entry.section.remove(); dashboardGroups.delete(id);
            } else {
                entry.occupied = occupiedWorkerGroups.has(id);
                delete entry.header.dataset.groupId;
                replaceDashboardHeader(entry, `<div class="worker-group-description"><strong>${escape(entry.name || 'Workers without group details')}</strong><span class="small text-body-secondary">Live worker activity remains visible while group details refresh.</span></div>`);
            }
        }
        denseMode = snapshot.groups.some(group => group.enabled);
        const liveRows = document.getElementById('workerGroupLiveRows');
        liveRows.classList.toggle('wg-dense', denseMode);
        document.getElementById('workerGroupCols').hidden = !denseMode;
        renderIdleRows();
        applyDenseRows();
        document.getElementById('workerGroupEmpty').hidden = dashboardGroups.size > 0;
        document.getElementById('workerGroupLiveWarnings').textContent = warnings().join(' ');
        renderSystemGroups();
        renderGroupVisibility();
        window.dispatchEvent(new CustomEvent('worker-groups-updated', { detail: snapshot }));
    }

    function renderSettings() {
        const mount = settings();
        if (!mount || !snapshot) return;
        if (!document.getElementById('workerGroupRows')) {
            mount.innerHTML = `
            <div class="wg-toolbar"><div id="workerGroupCapacity" class="wg-capacity"></div><div class="wg-toolbar-actions"><button type="button" class="btn btn-sm btn-outline-secondary" id="workerGroupAddCpu"><i class="bi bi-soundwave" aria-hidden="true"></i> <span class="lbl-long">Add CPU group for loudness</span><span class="lbl-short">CPU</span></button><button type="button" class="btn btn-sm btn-primary" id="workerGroupAdd"><i class="bi bi-plus-lg" aria-hidden="true"></i> Add group</button></div></div>
            <div class="week-card"><div class="week-head"><strong>Week at a glance</strong><button type="button" class="info-icon" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="One lane per device in each enabled group, coloured while it may start new files. Same device, same colour. Hatched red is a global pause. Updates as you edit." aria-label="About week at a glance"><i class="bi bi-info-circle"></i></button><span class="week-tz" id="workerWeekTz"></span></div><div id="workerWeekGraph"></div><div class="week-legend" id="workerWeekLegend"></div></div>
            <h3 class="settings-subheading"><i class="bi bi-collection" aria-hidden="true"></i>Worker groups<button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="A group is a name, an on/off switch and hours. It holds devices; each device has its own workers and jobs." aria-label="About worker groups"><i class="bi bi-info-circle"></i></button></h3>
            <div id="workerGroupRows" class="wg-list"></div><div id="workerGroupWarnings" class="alert alert-warning py-2 mt-3" hidden></div>
            <div id="workerGroupEditor" class="worker-group-editor" hidden></div>
            <div class="apply-bar" id="workerGroupApplyRow" hidden><button type="button" class="btn btn-primary btn-sm" id="workerGroupApply">Apply group changes</button><button type="button" class="btn btn-outline-secondary btn-sm" id="workerGroupCancel">Discard changes</button><span class="apply-note"><span class="dot"></span> Unsaved group changes</span></div>
            <div id="workerGroupMessage" role="status" aria-live="polite"></div>
            <div class="hint-line"><i class="bi bi-info-circle" aria-hidden="true"></i><span>Current files finish when a group closes or is reduced.<button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="How group changes apply" data-explain-title="How group changes apply" data-explain-html="Current files finish when a group closes or is reduced. GPU jobs may still use CPU stages or fallback. Chapter thumbnails are part of Video previews. The global job limit still applies. Devices shared by several groups add their worker counts. The weekly peak is limited to 32 CPU and 32 GPU workers." aria-label="About this note"><i class="bi bi-info-circle"></i></button></span></div>`;
            window._initBootstrapTooltips?.(mount);
        }
        const capacity = snapshot.capacity || {};
        document.getElementById('workerGroupCapacity').textContent = capacity.current && capacity.peak ? `${dirty ? 'Saved schedule' : 'Scheduled'} now: CPU ${capacity.current.cpu} · GPU ${capacity.current.gpu}. Weekly peak: CPU ${capacity.peak.cpu} · GPU ${capacity.peak.gpu}. ${timezoneLabel()}.` : '';
        renderRows(document.getElementById('workerGroupRows'));
        renderWeekOverview(draft || snapshot.groups);
        paintEditorPreview();
        positionEditor();
        const warning = document.getElementById('workerGroupWarnings');
        warning.textContent = warnings().join(' ');
        warning.hidden = !warning.textContent;
        document.getElementById('workerGroupApplyRow').hidden = !dirty;
        document.getElementById('workerGroupApply').disabled = saving;
        const editorApply = document.getElementById('workerGroupEditorApply');
        if (editorApply) editorApply.disabled = saving || !dirty;
        document.getElementById('workerGroupCancel').disabled = saving;
        document.getElementById('workerGroupAdd').disabled = saving;
        document.getElementById('workerGroupAddCpu').disabled = saving;
        document.getElementById('workerGroupAddCpu').hidden = !needsLoudnessGroup();
        if (saving) document.getElementById('workerGroupEditor').querySelectorAll('input,select,button').forEach(control => { control.disabled = true; });
        document.getElementById('workerGroupAdd').onclick = () => add(false);
        document.getElementById('workerGroupAddCpu').onclick = () => add(true);
        document.getElementById('workerGroupApply').onclick = () => save().catch(() => {});
        document.getElementById('workerGroupCancel').onclick = async () => { draft = null; dirty = false; editing = null; renderEditor(); await load(true); message('Unsaved group changes discarded. Latest groups loaded.'); };
    }

    function ensureDraft() { if (!draft) draft = clone(snapshot.groups); }
    function focusEditor(id) {
        if (editing !== id) return;
        const name = document.getElementById('workerGroupName');
        const editor = document.getElementById('workerGroupEditor');
        if (!name || !editor || editor.hidden) return;
        name.focus({ preventScroll: true });
        editor.previousElementSibling?.scrollIntoView({ block: 'start' });
    }
    function edit(id) {
        ensureDraft(); editing = id; renderSettings(); renderDashboard(); renderEditor(); focusEditor(id);
    }

    // Offered only when a server has loudness switched on and no enabled group has a CPU device taking loudness jobs.
    function needsLoudnessGroup() {
        const wanted = (snapshot.warnings || []).some(warning => warning && warning.job_type === 'loudness');
        const covered = (draft || snapshot.groups || []).some(group => group.enabled && group.members.some(member => member.resource === 'cpu' && member.job_types.includes('loudness')));
        return wanted && !covered;
    }

    function add(loudness) {
        ensureDraft();
        dirty = true;
        const id = window.crypto?.randomUUID?.() || 'group-' + Date.now().toString(36) + Math.random().toString(36).slice(2);
        draft.push({ id, name: loudness ? 'CPU loudness' : 'New worker group', enabled: true, availability: { mode: 'always', windows: [] },
            members: [{ id: newId('m'), resource: 'cpu', device: null, count: 1, job_types: loudness ? ['loudness'] : ['previews', 'intro_credits', 'loudness'] }] });
        edit(id);
    }

    function syncEditorState() {
        const apply = document.getElementById('workerGroupEditorApply');
        if (apply) apply.disabled = saving || !dirty;
        const editor = document.getElementById('workerGroupEditor');
        if (saving && editor) editor.querySelectorAll('input,select,button').forEach(control => { control.disabled = true; });
    }

    const infoIcon = (label, tip) => `<button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="${escape(tip)}" aria-label="About ${escape(label)}"><i class="bi bi-info-circle"></i></button>`;
    const deviceKeyOf = member => (member.resource === 'cpu' ? 'cpu' : member.device);
    const memberLimit = member => snapshot.limits?.[member.resource] || 32;
    const maxMembers = () => snapshot.limits?.members || 8;

    // The first unused GPU, else CPU; null when every detected device is already in the group.
    function nextDevice(group) {
        const used = new Set(group.members.map(deviceKeyOf));
        const gpu = (snapshot.hardware || []).find(item => item.device && item.status !== 'failed' && !used.has(item.device));
        if (gpu) return { resource: 'gpu', device: gpu.device, job_types: ['previews', 'intro_credits'] };
        return used.has('cpu') ? null : { resource: 'cpu', device: null, job_types: ['previews', 'intro_credits', 'loudness'] };
    }

    function deviceOptions(group, member) {
        const used = new Set(group.members.filter(other => other !== member).map(deviceKeyOf));
        const gpus = (snapshot.hardware || []).filter(item => item.device);
        const options = [{ value: 'cpu', label: 'CPU' }, ...gpus.map(gpu => ({ value: gpu.device, label: (gpu.name || gpu.device) + (gpu.status === 'failed' ? ' (unavailable)' : '') }))];
        if (member.resource === 'gpu' && !gpus.some(gpu => gpu.device === member.device)) options.push({ value: member.device, label: `${member.device} — not detected` });
        const selected = deviceKeyOf(member);
        return options.map(option => `<option value="${escape(option.value)}" ${option.value === selected ? 'selected' : ''} ${used.has(option.value) ? 'disabled' : ''}>${escape(option.label)}${used.has(option.value) ? ' (already in this group)' : ''}</option>`).join('');
    }

    function jobChips(member) {
        return CAPABILITIES.map(([kind, icon, , tone]) => {
            const blocked = kind === 'loudness' && member.resource !== 'cpu';
            const chip = `<button type="button" class="jchip ${tone}" data-kind="${kind}" aria-pressed="${member.job_types.includes(kind)}" ${blocked ? 'disabled aria-disabled="true"' : ''}><i class="bi bi-${icon}" aria-hidden="true"></i>${escape(JOBS[kind])}</button>`;
            return blocked ? `<span class="chipwrap">${chip}${infoIcon('why loudness is unavailable on a GPU', 'Plex loudness runs on CPU workers only')}</span>` : chip;
        }).join('');
    }

    // Row-level problems, shown live under the row; the same wording is used when Apply is refused.
    function memberIssues(group, member) {
        const name = shortDeviceName(member);
        const issues = [];
        if (!Number.isInteger(member.count) || member.count < 1 || member.count > memberLimit(member)) issues.push(['err', `${name}: enter 1–${memberLimit(member)} workers. Remove the device to use zero.`]);
        if (!member.job_types.length) issues.push(['err', `${name}: choose at least one job type.`]);
        if (group.members.findIndex(other => deviceKeyOf(other) === deviceKeyOf(member)) !== group.members.indexOf(member)) issues.push(['err', `${name} is already in this group.`]);
        if (member.resource === 'gpu' && member.job_types.includes('loudness')) issues.push(['err', `${name}: loudness runs on CPU only.`]);
        if (deviceMissing(member)) issues.push(['warn', 'Not detected — its jobs wait; the other devices keep working.']);
        if (memberNotes.has(member.id)) issues.push(['note', memberNotes.get(member.id)]);
        return issues;
    }

    const messageMarkup = issues => issues.map(([tone, text]) => `<p class="msg ${tone}"><i class="bi bi-${tone === 'err' ? 'exclamation-circle' : tone === 'warn' ? 'exclamation-triangle' : 'info-circle'}" aria-hidden="true"></i><span>${escape(text)}</span></p>`).join('');

    function memberRow(group, member) {
        const limit = memberLimit(member);
        const issues = memberIssues(group, member);
        const label = shortDeviceName(member);
        return `<div class="mrow${issues.some(i => i[0] === 'err') ? ' has-err' : issues.some(i => i[0] === 'warn') ? ' has-warn' : ''}" data-member="${escape(member.id)}" style="--c:${deviceColor(member)}">
            <div class="mrow-top">
                <div class="fld-dev"><label class="lbl" for="wgDevice-${escape(member.id)}">Device</label><select class="form-select" id="wgDevice-${escape(member.id)}" data-pick>${deviceOptions(group, member)}</select></div>
                <div><span class="lbl">Workers${infoIcon('workers', 'Simultaneous tasks on this device, not CPU cores.')}</span><div class="wg-stepper" role="group" aria-label="Workers on ${escape(label)}">
                    <button type="button" data-step="-1" aria-label="One fewer worker" ${member.count <= 1 ? 'disabled title="Remove the device to use zero"' : ''}><i class="bi bi-dash" aria-hidden="true"></i></button>
                    <input class="val" type="number" min="1" max="${limit}" value="${Number.isFinite(member.count) ? member.count : ''}" inputmode="numeric" data-count aria-label="Worker count for ${escape(label)}">
                    <button type="button" data-step="1" aria-label="One more worker" ${member.count >= limit ? 'disabled' : ''}><i class="bi bi-plus" aria-hidden="true"></i></button></div></div>
                <button type="button" class="rm" data-remove-member aria-label="Remove ${escape(label)} from this group" title="Remove device"><i class="bi bi-trash" aria-hidden="true"></i></button>
            </div>
            <div class="mrow-jobs"><span class="lbl">Jobs${infoIcon('jobs', 'Task types this device may run. A type that no device allows waits in the queue.')}</span>${jobChips(member)}</div>
            <div class="msgs" data-msgs>${messageMarkup(issues)}</div>
        </div>`;
    }

    function groupIssues(group) {
        return [
            ...(group.name.trim() ? [] : [['err', 'Every group needs a name.']]),
            ...(group.members.length ? [] : [['err', 'Add at least one device.']]),
        ];
    }

    function editorMarkupSettings(group) {
        const addLimit = group.members.length >= maxMembers();
        const free = !addLimit && nextDevice(group);
        return `
            <div class="ed-head"><i class="bi bi-pencil" aria-hidden="true"></i><h4>Edit group</h4><span class="ed-name" id="workerGroupEditorName">· ${escape(group.name)}</span><span class="flex-fill"></span><button type="button" class="btn btn-sm btn-icon" id="workerGroupClose" aria-label="Close editor" title="Close editor"><i class="bi bi-x-lg" aria-hidden="true"></i></button></div>
            <div class="err-foot" id="workerGroupEditorError" role="alert" hidden></div>
            <div class="setting-row"><div class="sr-label"><label class="sr-title" for="workerGroupName">Name</label></div><div class="sr-control"><input id="workerGroupName" class="form-control" maxlength="100" value="${escape(group.name)}"></div></div>
            <div class="ed-sec" id="workerGroupDevices"><h5 class="sr-title">Devices in this group${infoIcon('devices in this group', 'Each device has its own worker count and job types. Use one row per device.')}</h5>
                <p class="sr-hint">One row per device. Hours and the on/off switch apply to the whole group.</p>
                <div id="workerGroupMembers">${group.members.map(member => memberRow(group, member)).join('') || '<div class="wg-empty-devices">Add a device to give this group workers.</div>'}</div>
                <div id="workerGroupGroupMsgs">${messageMarkup(groupIssues(group).filter(issue => issue[1] !== 'Every group needs a name.'))}</div>
                <p class="msg err" id="workerGroupLastDevice" role="alert" hidden><i class="bi bi-exclamation-circle" aria-hidden="true"></i><span>A group needs at least one device. Remove the group instead.</span></p>
                <div class="add-device"><button type="button" class="btn btn-sm btn-outline-secondary" id="workerGroupAddDevice" ${free ? '' : 'disabled'}><i class="bi bi-plus-lg" aria-hidden="true"></i> Add device</button><span id="workerGroupAddDeviceInfo" ${free ? 'hidden' : ''}>${infoIcon('why Add device is unavailable', addLimit ? `A group holds up to ${maxMembers()} devices` : 'Every detected device is already in this group')}</span></div>
            </div>
            <div class="setting-row"><div class="sr-label"><label class="sr-title" for="workerGroupAvailability">Availability</label>${infoIcon('Availability', 'Group hours only stop new files from starting; current work finishes. A global pause stops everything.')}<div class="sr-hint">When this group may start new files.</div></div><div class="sr-control"><select class="form-select" id="workerGroupAvailability"><option value="always">Always available</option><option value="scheduled" ${group.availability.mode === 'scheduled' ? 'selected' : ''}>Weekly hours</option></select></div></div>
            <div id="workerGroupWindows" ${group.availability.mode === 'always' ? 'hidden' : ''}>${group.availability.windows.map((window, index) => windowEditorSettings(window, index)).join('')}<button type="button" id="workerGroupAddWindow" class="btn btn-sm btn-outline-secondary mt-2"><i class="bi bi-plus-lg" aria-hidden="true"></i> Add time window</button><p class="form-text tz-note"><i class="bi bi-globe2" aria-hidden="true"></i><span>${escape(timezoneLabel())}. Days select when the window starts.<button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="Overnight windows belong to the day they start." data-explain-title="Time windows" data-explain-html="Days select when the window starts: Mon 23:00–07:00 ends Tuesday. Overlapping windows in this group count once." aria-label="About time windows"><i class="bi bi-info-circle"></i></button></span></p><div class="week-card week-card-inline"><div class="week-head"><strong>This group’s week</strong></div><div id="workerGroupPreview"></div></div></div>
            <div class="ed-actions"><button type="button" class="btn btn-primary btn-sm" id="workerGroupEditorApply" ${saving || !dirty ? 'disabled' : ''}>Apply group changes</button><span class="ed-note">Saves all group edits. Closing keeps your draft.</span><span class="flex-fill"></span><button type="button" class="btn btn-sm btn-outline-secondary" id="workerGroupDuplicate"><i class="bi bi-copy" aria-hidden="true"></i> Duplicate group</button><button type="button" class="btn btn-sm btn-outline-danger" id="workerGroupRemove"><i class="bi bi-trash" aria-hidden="true"></i> Remove group</button></div>`;
    }

    function showEditorError(text) {
        const box = document.getElementById('workerGroupEditorError');
        if (!box) return;
        box.textContent = text || '';
        box.hidden = !text;
    }

    // Update row messages, stepper limits and the Add device state in place so typing is never interrupted.
    function refreshMembers(container, group) {
        for (const row of container.querySelectorAll('[data-member]')) {
            const member = group.members.find(item => item.id === row.dataset.member);
            if (!member) continue;
            const issues = memberIssues(group, member);
            row.classList.toggle('has-err', issues.some(issue => issue[0] === 'err'));
            row.classList.toggle('has-warn', !issues.some(issue => issue[0] === 'err') && issues.some(issue => issue[0] === 'warn'));
            const markup = messageMarkup(issues);
            const box = row.querySelector('[data-msgs]');
            if (box._markup !== markup) { box._markup = markup; box.innerHTML = markup; }
            const [minus, plus] = row.querySelectorAll('[data-step]');
            minus.disabled = !(member.count > 1);
            if (minus.disabled) minus.title = 'Remove the device to use zero'; else minus.removeAttribute('title');
            plus.disabled = member.count >= memberLimit(member);
        }
        const addLimit = group.members.length >= maxMembers();
        const free = !addLimit && nextDevice(group);
        container.querySelector('#workerGroupAddDevice').disabled = !free || saving;
        container.querySelector('#workerGroupAddDeviceInfo').hidden = !!free;
        container.querySelector('#workerGroupLastDevice').hidden = true;
        paintEditorPreview();
    }

    function renderEditor() {
        const container = document.getElementById('workerGroupEditor');
        if (!container) return;
        const group = draft?.find(item => item.id === editing);
        container.hidden = !group;
        if (!group) return;
        if (typeof _disposeBootstrapTooltips === 'function') _disposeBootstrapTooltips(container);
        container.innerHTML = editorMarkupSettings(group);
        window._initBootstrapTooltips?.(container);
        paintEditorPreview();
        const changed = () => {
            dirty = true;
            showEditorError('');
            renderSettings();
            message('Group changes are not saved until you apply them.');
        };
        const memberOf = node => group.members.find(item => item.id === node.closest('[data-member]').dataset.member);
        const refocus = id => { renderEditor(); document.getElementById(id)?.focus({ preventScroll: true }); };
        document.getElementById('workerGroupEditorApply').onclick = () => save().catch(() => {});
        document.getElementById('workerGroupName').oninput = event => {
            group.name = event.target.value;
            const label = document.getElementById('workerGroupEditorName');
            if (label) label.textContent = '· ' + group.name;
            changed();
        };
        container.querySelectorAll('[data-count]').forEach(input => input.oninput = event => {
            memberOf(input).count = event.target.value === '' ? NaN : Number(event.target.value);
            changed(); refreshMembers(container, group);
        });
        container.querySelectorAll('[data-step]').forEach(button => button.onclick = () => {
            const member = memberOf(button);
            member.count = Math.min(memberLimit(member), Math.max(1, (Number(member.count) || 1) + Number(button.dataset.step)));
            button.closest('.wg-stepper').querySelector('[data-count]').value = member.count;
            changed(); refreshMembers(container, group);
        });
        container.querySelectorAll('[data-pick]').forEach(select => select.onchange = () => {
            const member = memberOf(select);
            member.resource = select.value === 'cpu' ? 'cpu' : 'gpu';
            member.device = member.resource === 'cpu' ? null : select.value;
            memberNotes.delete(member.id);
            if (member.resource === 'gpu' && member.job_types.includes('loudness')) {
                member.job_types = member.job_types.filter(kind => kind !== 'loudness');
                memberNotes.set(member.id, 'Loudness removed — it runs on CPU only.');
            }
            changed(); refocus(select.id);
        });
        container.querySelectorAll('[data-member] [data-kind]').forEach(chip => chip.onclick = () => {
            const member = memberOf(chip);
            const on = !member.job_types.includes(chip.dataset.kind);
            member.job_types = CAPABILITIES.map(item => item[0]).filter(kind => kind === chip.dataset.kind ? on : member.job_types.includes(kind));
            chip.setAttribute('aria-pressed', String(on));
            memberNotes.delete(member.id);
            changed(); refreshMembers(container, group);
        });
        container.querySelectorAll('[data-remove-member]').forEach(button => button.onclick = () => {
            if (group.members.length <= 1) { document.getElementById('workerGroupLastDevice').hidden = false; return; }
            const member = memberOf(button);
            group.members.splice(group.members.indexOf(member), 1);
            memberNotes.delete(member.id);
            changed(); refocus('workerGroupAddDevice');
        });
        document.getElementById('workerGroupAddDevice').onclick = () => {
            const next = nextDevice(group);
            if (!next || group.members.length >= maxMembers()) return;
            const member = { id: newId('m'), count: 1, ...next };
            group.members.push(member);
            changed(); renderEditor();
            document.getElementById('wgDevice-' + member.id)?.focus({ preventScroll: true });
        };
        document.getElementById('workerGroupAvailability').onchange = event => {
            group.availability.mode = event.target.value;
            if (event.target.value === 'scheduled' && !group.availability.windows.length) group.availability.windows.push(defaultWindow());
            changed(); renderEditor();
        };
        document.getElementById('workerGroupAddWindow').onclick = () => { group.availability.windows.push(defaultWindow()); changed(); renderEditor(); };
        container.querySelectorAll('[data-preset]').forEach(button => button.onclick = () => {
            const row = button.closest('[data-window]');
            group.availability.windows[Number(row.dataset.window)].days = PRESETS[button.dataset.preset].slice();
            changed(); renderEditor();
            document.querySelector(`[data-window="${row.dataset.window}"] [data-preset="${button.dataset.preset}"]`)?.focus();
        });
        container.querySelectorAll('[data-window]').forEach(row => {
            const window = group.availability.windows[Number(row.dataset.window)];
            row.querySelectorAll('[data-day]').forEach(input => input.onchange = () => { window.days = [...row.querySelectorAll('[data-day]:checked')].map(input => Number(input.dataset.day)); changed(); });
            for (const key of ['start', 'end']) row.querySelector('[data-time="' + key + '"]').onchange = event => { window[key] = event.target.value; changed(); };
            row.querySelector('[data-remove-window]').onclick = () => { group.availability.windows.splice(Number(row.dataset.window), 1); changed(); renderEditor(); };
        });
        document.getElementById('workerGroupClose').onclick = () => { editing = null; if (!dirty) draft = null; renderSettings(); renderEditor(); document.getElementById(dirty ? 'workerGroupApply' : 'workerGroupAdd').focus(); };
        document.getElementById('workerGroupDuplicate').onclick = () => { const duplicate = clone(group); duplicate.id = 'group-' + Date.now().toString(36) + Math.random().toString(36).slice(2); duplicate.name += ' copy'; dirty = true; draft.push(duplicate); edit(duplicate.id); };
        document.getElementById('workerGroupRemove').onclick = () => { draft = draft.filter(item => item.id !== group.id); editing = null; changed(); renderEditor(); };
    }

    function defaultWindow() { return { days: [0, 1, 2, 3, 4, 5, 6], start: '23:00', end: '07:00' }; }
    function windowEditorSettings(window, index) {
        const days = DAYS.map((day, number) => `<label class="day-chip"><input type="checkbox" data-day="${number}" ${window.days.includes(number) ? 'checked' : ''}><span>${day}</span></label>`).join('');
        const presets = [['all', 'Every day'], ['weekdays', 'Weekdays'], ['weekends', 'Weekends']].map(([key, label]) => `<button type="button" data-preset="${key}">${label}</button>`).join('');
        return `<fieldset class="week-window worker-group-window" data-window="${index}"><div class="week-window-head"><legend>Window ${index + 1}</legend><button type="button" class="btn btn-sm btn-icon btn-icon-danger" data-remove-window aria-label="Remove window ${index + 1}" title="Remove window"><i class="bi bi-trash" aria-hidden="true"></i></button></div><div class="week-window-fields"><div class="week-field week-field-days"><span class="week-field-label">Days <span class="day-presets">${presets}</span></span><div class="day-chips" role="group" aria-label="Days">${days}</div></div><div class="week-field"><label class="week-field-label" for="wgStart${index}">Start</label><input type="time" class="form-control" id="wgStart${index}" data-time="start" value="${escape(window.start)}"></div><div class="week-field"><label class="week-field-label" for="wgEnd${index}">End</label><input type="time" class="form-control" id="wgEnd${index}" data-time="end" value="${escape(window.end)}"></div></div></fieldset>`;
    }

    function validate() {
        for (const group of draft) {
            const [problem] = [...groupIssues(group), ...group.members.flatMap(member => memberIssues(group, member).filter(issue => issue[0] === 'err'))];
            if (problem) return problem[1];
            if (group.availability.mode === 'scheduled') {
                if (!group.availability.windows.length) return (`${group.name}: add a time window or choose Always available.`);
                for (const window of group.availability.windows) {
                    if (!window.days.length) return (`${group.name}: select at least one start day for every window.`);
                    if (!/^\d{2}:\d{2}$/.test(window.start) || !/^\d{2}:\d{2}$/.test(window.end) || window.start === window.end) return (`${group.name}: each window needs different valid start and end times.`);
                }
            }
        }
        return '';
    }

    async function save() {
        if (!draft || !dirty) return;
        if (saving) throw new Error('Worker groups are still saving.');
        const error = validate();
        if (error) { message(error, true); showEditorError(error); throw new Error(error); }
        saving = true; renderSettings(); syncEditorState(); renderSystemGroups();
        let failure = '';
        try {
            snapshot = await request('PUT', '', { groups: draft, revision: snapshot.revision });
            draft = null; dirty = false; editing = null; renderSettings(); renderEditor(); renderDashboard();
            message(snapshot.warning || 'Worker groups saved. Current files finish; new assignments use these settings.', !!snapshot.warning);
        } catch (error) {
            const text = error.conflict ? 'Worker groups changed elsewhere. Your draft is preserved. Discard it and reload the latest groups before editing again.' : error.message;
            message(text, true);
            if (error.conflict) { await load(true); }
            failure = text;
            throw error;
        } finally {
            saving = false; renderSettings(); renderEditor(); renderDashboard();
            if (failure) showEditorError(failure);
        }
    }

    async function load(force = false) {
        if (loading) return loading;
        loading = (async () => {
            try {
                const data = await request('GET');
                if (snapshot && data.revision < snapshot.revision) return;
                // An open draft retains its original revision; a newer read must not authorize overwriting concurrent edits.
                if (draft && snapshot && !force) { snapshot.capacity = data.capacity; snapshot.warnings = data.warnings; snapshot.hardware = data.hardware; }
                else if (draft && snapshot) { snapshot.capacity = data.capacity; }
                else snapshot = data;
                renderSettings(); renderDashboard();
                if (requestedEditor && settings()) {
                    const id = requestedEditor; requestedEditor = null;
                    if (snapshot.groups.some(group => group.id === id)) {
                        edit(id);
                        // Let initial fragment navigation finish before positioning a directly linked editor.
                        const focusLinked = () => requestAnimationFrame(() => requestAnimationFrame(() => focusEditor(id)));
                        if (document.readyState === 'complete') focusLinked();
                        else window.addEventListener('load', focusLinked, { once: true });
                    } else message('That worker group no longer exists. Choose a group below or add one.', true);
                }
            } catch (error) {
                if (!snapshot) {
                    if (settings()) settings().innerHTML = '<p class="text-danger small">Could not load worker groups. Reload the page to try again.</p>';
                    if (dashboard()) ensureDashboard();
                }
                message(error.message, true);
            } finally { loading = null; }
        })();
        return loading;
    }
    window.WorkerGroups = { load, save, hasDraft: () => dirty, refreshHardware: () => load(true), getSnapshot: () => snapshot, getDashboardWorkerHost, setWorkerActivity };
    // Pause windows are edited outside this file; their hatching on the group lanes follows them.
    window.addEventListener('settings-pause-changed', () => { if (snapshot && settings()) renderSettings(); });
    window.addEventListener('beforeunload', event => { if (dirty && !saving) { event.preventDefault(); event.returnValue = ''; } });
    document.addEventListener('DOMContentLoaded', () => {
        if (!settings() && !dashboard()) return;
        load();
        setInterval(() => { if (!document.hidden && !saving) load(); }, dashboard() ? 5000 : 10000);
    });
    document.addEventListener('click', event => {
        const scale = event.target.closest('[data-member-scale]');
        if (scale) {
            const block = scale.closest('[data-member-id]');
            if (block) scaleMember(block.dataset.memberGroup, block.dataset.memberId, Number(scale.dataset.memberScale));
            return;
        }
        const enable = event.target.closest('[data-group-enable]');
        if (!enable) return;
        const id = enable.dataset.groupEnable || enable.closest('[data-system-group]')?.dataset.systemGroup;
        if (id) scaleGroup(id, { enabled: true });
    });
    // Empty-state buttons live inside the re-rendered list, so they are delegated.
    document.addEventListener('click', event => {
        if (event.target.closest('[data-empty-add]')) add(false);
        if (event.target.closest('[data-empty-add-cpu]')) add(true);
    });
})();
