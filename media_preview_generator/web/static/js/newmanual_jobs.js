// ---------------------------------------------------------------------------
// Manual Generation: server-search typeahead + folder/file picker + chip list.
// A selection supplies explicit paths to the chosen job API. A show
// carries its folder(s) (the dispatcher expands them into episodes); a movie/
// episode/file carries its file; a browsed folder carries that folder.
// ---------------------------------------------------------------------------
let _manualSelections = [];
let _manualWired = false;
let _manualLastBrowseRoot = '/';
let _manualSearchTimer = null;
let _manualSearchSeq = 0;
// Current search result set + the indices ticked for batch-add.
let _manualSearchResults = [];
let _manualChecked = new Set();

let _manualOpeningSequence = 0;
let _manualSubmitting = false;
const _MANUAL_KINDS = {
    previews: { label: 'Previews', endpoint: '/api/jobs/manual', help: 'Creates previews for Plex, Emby and Jellyfin servers that own these files. Chapter thumbnails follow each Plex server’s preview setting.' },
    intro_credits: { label: 'Intro & Credits', endpoint: '/api/markers/jobs', help: 'Runs on enabled owning servers and libraries with Intro & Credits on. Requires the server’s publisher setup and a compatible worker group. Search scope does not restrict publishing.' },
    loudness: { label: 'Plex loudness', endpoint: '/api/loudness/jobs', help: 'Runs on owning Plex movie and TV libraries with Loudness on. Requires a local Plex 1.43.4.x database and a CPU group allowing loudness. Existing complete measurements are kept; the helper is unsupported.' },
};

function _manualJobKind() {
    return document.querySelector('input[name="manualJobKind"]:checked')?.value || 'previews';
}

function _manualPaths() {
    const typed = document.getElementById('manualFilePaths').value.split('\n').map(path => path.trim()).filter(Boolean);
    return [...new Set([..._manualSelections.flatMap(selection => selection.paths), ...typed])];
}

function _manualPathError(paths) {
    if (!paths.length) return 'Pick at least one show, movie, file or folder.';
    if (paths.some(path => typeof path !== 'string' || !path.startsWith('/') || path.includes('\0') || path.split('/').includes('..'))) {
        return 'Use absolute container paths starting with /, without parent-directory (..) segments.';
    }
    return '';
}

function _manualUpdateStartState() {
    const paths = _manualPaths();
    const button = document.getElementById('manualStartButton');
    const pathError = _manualPathError(paths);
    if (button) button.disabled = _manualSubmitting || !!pathError || !_MANUAL_KINDS[_manualJobKind()];
    document.getElementById('manualPathValidation').textContent = paths.length ? pathError : '';
    const clearAll = document.getElementById('manualClearAll');
    if (clearAll) clearAll.classList.toggle('d-none', !paths.length);
}

function _showManualKindControls(resetDefaults = false) {
    const kind = _manualJobKind();
    const previews = kind === 'previews';
    document.getElementById('manualSearchServerGroup').hidden = previews;
    document.getElementById('manualPublishServerGroup').hidden = !previews;
    document.getElementById('manualProcessingModeGroup').hidden = !previews;
    document.getElementById('manualMarkersForceGroup').hidden = kind !== 'intro_credits';
    document.getElementById('manualKindHelp').textContent = _MANUAL_KINDS[kind]?.help || '';
    if (resetDefaults) {
        document.getElementById('manualPriority').value = previews ? '2' : '3';
        document.getElementById('manualMissingOnly').checked = true;
        document.getElementById('manualMarkersForce').checked = false;
        document.getElementById('manualSubmissionError').textContent = '';
    }
    _manualHideSearch();
    _manualUpdateStartState();
}


async function _populateManualServerScopePicker() {
    const opening = _manualOpeningSequence;
    const pickers = [document.getElementById('manualServerScope'), document.getElementById('manualSearchServer')];
    if (!pickers.every(Boolean)) return;
    try {
        const data = await apiGet('/api/servers');
        if (opening !== _manualOpeningSequence) return;
        const servers = (data.servers || []).filter(server => server.enabled !== false);
        pickers.forEach((select, index) => {
            const chosen = select.value;
            select.replaceChildren(new Option(index ? 'All enabled servers' : 'All servers (publish to whoever owns the file)', ''));
            servers.forEach(server => select.add(new Option(`${server.name} (${(server.type || '').toUpperCase()})`, server.id)));
            if ([...select.options].some(option => option.value === chosen)) select.value = chosen;
        });
    } catch (error) {
        if (opening === _manualOpeningSequence) document.getElementById('manualSubmissionError').textContent = 'Could not load server choices. Search and publishing remain set to all servers; reopen this dialog to try again.';
    }
}

function _manualKindIcon(kind) {
    if (kind === 'show') return 'bi-tv';
    if (kind === 'movie') return 'bi-film';
    if (kind === 'episode') return 'bi-collection-play';
    if (kind === 'file') return 'bi-film';
    return 'bi-folder2';
}

// Server-source pills shown on each search result so the user can see which
// server(s) a hit came from. A merged row (same item on several servers) shows
// one pill per server. Colour-coded by vendor for at-a-glance scanning.
function _manualServerBadges(servers) {
    const cls = { plex: 'text-warning border-warning', emby: 'text-success border-success', jellyfin: 'text-info border-info' };
    return (servers || []).map(s =>
        `<span class="badge bg-transparent border ${cls[s.type] || 'text-secondary border-secondary'} ms-1" style="font-weight:500;" title="${escapeHtmlAttr((s.type || '').toUpperCase())}">${escapeHtmlText(s.name)}</span>`
    ).join('');
}

function _manualSelectionKey(paths) {
    return Array.isArray(paths) && paths.length ? JSON.stringify(paths.slice().sort()) : '';
}

function manualAddSelection(sel) {
    const key = _manualSelectionKey(sel.paths);
    if (!key) return;
    if (_manualSelections.some(s => s.key === key)) {
        showToast('Already added', `${sel.label} is already selected`, 'info');
        return;
    }
    _manualSelections.push({ ...sel, key });
    manualRenderChips();
}

function manualRemoveSelection(key) {
    _manualSelections = _manualSelections.filter(s => s.key !== key);
    manualRenderChips();
}

function manualRenderChips() {
    const wrap = document.getElementById('manualChips');
    const empty = document.getElementById('manualChipsEmpty');
    if (!wrap) return;
    empty.classList.toggle('d-none', !!_manualSelections.length);
    wrap.innerHTML = _manualSelections.map(selection => {
        const tip = (selection.sublabel ? selection.sublabel + '\n' : '') + selection.paths.join('\n');
        return `<div class="manual-chip badge bg-secondary-subtle text-body border d-flex align-items-start gap-2 py-2 px-2" title="${escapeHtmlAttr(tip)}">
            <i class="bi ${_manualKindIcon(selection.kind)}" aria-hidden="true"></i>
            <div class="manual-chip-label"><span>${escapeHtmlText(selection.label)}</span><details class="manual-chip-paths mt-1"><summary>Paths</summary>${selection.paths.map(path => `<code>${escapeHtmlText(path)}</code>`).join('')}</details></div>
            <button type="button" class="btn btn-link text-reset p-0 ms-auto manual-chip-rm" data-key="${escapeHtmlAttr(selection.key)}" aria-label="Remove ${escapeHtmlAttr(selection.label)}"><i class="bi bi-x-lg" aria-hidden="true"></i></button>
        </div>`;
    }).join('');
    wrap.querySelectorAll('.manual-chip-rm').forEach(button => button.addEventListener('click', () => manualRemoveSelection(button.dataset.key)));
    _manualUpdateStartState();
}

function _manualHideSearch() {
    clearTimeout(_manualSearchTimer);
    _manualSearchSeq++;
    const box = document.getElementById('manualSearchResults');
    if (box) { box.classList.add('d-none'); box.innerHTML = ''; }
    _manualChecked = new Set();
}

function manualRenderSearchResults(results) {
    _manualSearchResults = results || [];
    _manualChecked = new Set();
    const box = document.getElementById('manualSearchResults');
    if (!box) return;
    if (!results || !results.length) {
        box.innerHTML = '<div class="list-group-item text-muted small">No matches.</div>';
        box.classList.remove('d-none');
        return;
    }
    _manualSearchResults = results;
    _manualChecked = new Set();
    const kindLabel = { show: 'Shows', movie: 'Movies', episode: 'Episodes' };
    let lastKind = null;
    const rows = [];
    results.forEach((r, idx) => {
        if (r.kind !== lastKind) {
            lastKind = r.kind;
            rows.push(`<div class="list-group-item bg-body-tertiary py-1 small fw-semibold text-muted">${kindLabel[r.kind] || 'Results'}</div>`);
        }
        const year = r.year ? ` <span class="text-muted">(${r.year})</span>` : '';
        const bits = [];
        if (r.kind === 'show') {
            if (r.child_count) bits.push(`${r.child_count} eps`);
            if ((r.paths || []).length > 1) bits.push(`${r.paths.length} folders`);
        }
        const meta = bits.length ? `<span class="small text-muted me-2">${escapeHtmlText(bits.join(' · '))}</span>` : '';
        // A row is a checkbox (batch-pick) + a clickable title (add-one-now) +
        // metadata/badges. The checkbox toggles selection without closing the
        // dropdown; clicking the title adds just that item immediately.
        rows.push(`<div class="list-group-item d-flex align-items-center gap-2" data-idx="${idx}">
            <input type="checkbox" class="form-check-input mt-0 flex-shrink-0 manual-row-check" data-idx="${idx}" aria-label="Select ${escapeHtmlAttr(r.title)}">
            <button type="button" class="flex-grow-1 text-truncate manual-row-add" data-idx="${idx}"><i class="bi ${_manualKindIcon(r.kind)} me-2" aria-hidden="true"></i>${escapeHtmlText(r.title)}${year}</button>
            <span class="ms-1 flex-shrink-0 d-flex align-items-center">${meta}${_manualServerBadges(r.servers)}</span>
        </div>`);
    });
    // Discoverability: when a show is in the results and the user hasn't typed
    // a season/episode yet, hint that they can append one to target a single
    // episode (the search box accepts "Show S01E01" or "Show 1x01").
    const q = (document.getElementById('manualSearchInput') || {}).value || '';
    const typedEpisode = /\bS\d{1,2}E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b/i.test(q);
    if (results.some(r => r.kind === 'show') && !typedEpisode) {
        rows.unshift('<div class="list-group-item small text-body-secondary bg-body-tertiary py-1 border-bottom"><i class="bi bi-lightbulb me-1 text-warning" aria-hidden="true"></i>Tip: add an episode like <code>S01E01</code> to pick just one episode.</div>');
    }
    // Sticky batch-add bar — only shown once something is ticked.
    rows.push(`<div class="list-group-item d-flex justify-content-between align-items-center position-sticky bottom-0 bg-body border-top" id="manualSelectBar" hidden>
        <span class="small text-muted"><span id="manualSelectCount">0</span> selected</span>
        <button type="button" class="btn btn-sm btn-primary" id="manualAddSelectedBtn"><i class="bi bi-plus-lg me-1" aria-hidden="true"></i>Add selected</button>
    </div>`);
    box.innerHTML = rows.join('');
    box.classList.remove('d-none');
    box.querySelectorAll('.manual-row-add').forEach(el => {
        el.addEventListener('click', () => {
            _manualAddResult(_manualSearchResults[parseInt(el.dataset.idx, 10)]);
            const input = document.getElementById('manualSearchInput');
            if (input) { input.value = ''; input.focus(); }
            _manualHideSearch();
        });
    });
    box.querySelectorAll('.manual-row-check').forEach(cb => {
        cb.addEventListener('change', () => {
            const idx = parseInt(cb.dataset.idx, 10);
            if (cb.checked) _manualChecked.add(idx); else _manualChecked.delete(idx);
            _manualUpdateSelectBar();
        });
    });
    const addBtn = document.getElementById('manualAddSelectedBtn');
    if (addBtn) addBtn.addEventListener('click', () => {
        [..._manualChecked].sort((a, b) => a - b).forEach(i => _manualAddResult(_manualSearchResults[i]));
        const input = document.getElementById('manualSearchInput');
        if (input) { input.value = ''; input.focus(); }
        _manualHideSearch();
    });
}

// Turn one search result into a chip selection (shared by single-click add
// and the batch "Add selected" button).
function _manualAddResult(r) {
    if (!r) return;
    const labelYear = r.year ? ` (${r.year})` : '';
    const sub = r.kind === 'show'
        ? [r.child_count ? `${r.child_count} eps` : '', `${r.paths.length} folder(s)`].filter(Boolean).join(' · ')
        : '';
    manualAddSelection({ kind: r.kind, label: `${r.title}${labelYear}`, sublabel: sub, paths: r.paths });
}

function _manualUpdateSelectBar() {
    const bar = document.getElementById('manualSelectBar');
    const cnt = document.getElementById('manualSelectCount');
    if (!bar || !cnt) return;
    cnt.textContent = _manualChecked.size;
    bar.hidden = !_manualChecked.size;
}

async function manualRunSearch() {
    const input = document.getElementById('manualSearchInput');
    if (!input) return;
    const q = input.value.trim();
    if (q.length < 2) { _manualHideSearch(); return; }
    const scopeId = _manualJobKind() === 'previews' ? 'manualServerScope' : 'manualSearchServer';
    const scope = document.getElementById(scopeId).value;
    const box = document.getElementById('manualSearchResults');
    if (box) {
        box.innerHTML = '<div class="list-group-item text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Searching…</div>';
        box.classList.remove('d-none');
    }
    const seq = ++_manualSearchSeq;
    try {
        const qs = new URLSearchParams({ q });
        if (scope) qs.set('server_id', scope);
        const data = await apiGet('/api/media/search?' + qs.toString());
        if (seq !== _manualSearchSeq) return;  // a newer keystroke superseded this
        manualRenderSearchResults(data.results || []);
    } catch (e) {
        if (seq !== _manualSearchSeq) return;
        if (box) box.innerHTML = `<div class="list-group-item text-danger small">${escapeHtmlText((e && e.message) || 'Search failed')}</div>`;
    }
}

function manualOpenBrowse() {
    const opening = _manualOpeningSequence;
    openFolderPicker(_manualLastBrowseRoot, (path, meta) => {
        if (!path || opening !== _manualOpeningSequence) return;
        _manualLastBrowseRoot = (meta && meta.isDir) ? path : path.replace(/\/+[^/]+\/?$/, '') || '/';
        const base = path.replace(/\/+$/, '').split('/').pop() || path;
        manualAddSelection({
            kind: (meta && meta.isDir) ? 'folder' : 'file',
            label: base,
            sublabel: path,
            paths: [path],
        });
    }, { includeFiles: true });
}

function _wireManualModal() {
    if (_manualWired) return;
    _manualWired = true;
    const input = document.getElementById('manualSearchInput');
    if (input) {
        input.addEventListener('input', () => {
            clearTimeout(_manualSearchTimer);
            _manualSearchTimer = setTimeout(manualRunSearch, 250);
        });
        input.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') _manualHideSearch();
            if (ev.key === 'Enter') { ev.preventDefault(); clearTimeout(_manualSearchTimer); manualRunSearch(); }
        });
    }
    const browseBtn = document.getElementById('manualBrowseBtn');
    if (browseBtn) browseBtn.addEventListener('click', manualOpenBrowse);
    const clearAll = document.getElementById('manualClearAll');
    if (clearAll) clearAll.addEventListener('click', (ev) => {
        ev.preventDefault();
        _manualSelections = [];
        document.getElementById('manualFilePaths').value = '';
        manualRenderChips();
    });
    ['manualServerScope', 'manualSearchServer'].forEach(id => document.getElementById(id).addEventListener('change', () => {
        _manualHideSearch();
        if ((input.value || '').trim().length >= 2) manualRunSearch();
    }));
    document.querySelectorAll('input[name="manualJobKind"]').forEach(radio => radio.addEventListener('change', () => {
        _showManualKindControls(true);
        if ((input.value || '').trim().length >= 2) manualRunSearch();
    }));
    document.getElementById('manualFilePaths').addEventListener('input', () => {
        document.getElementById('manualSubmissionError').textContent = '';
        _manualUpdateStartState();
    });
    document.getElementById('manualTriggerForm').addEventListener('submit', event => { event.preventDefault(); startManualJob(); });
    document.getElementById('manualTriggerModal').addEventListener('hidden.bs.modal', () => {
        _manualOpeningSequence++;
        clearTimeout(_manualSearchTimer);
        _manualHideSearch();
    });
    // Clicking outside the results dropdown dismisses it.
    document.addEventListener('click', (ev) => {
        if (!ev.target.closest('#manualSearchResults, #manualSearchInput, #manualServerScope, #manualSearchServer, [name="manualJobKind"], label[for^="manualKind"]') && ev.target !== input) {
            _manualHideSearch();
        }
    });
}

function showManualTriggerModal() {
    _manualSelections = [];
    _manualOpeningSequence++;
    _manualSearchSeq++;
    clearTimeout(_manualSearchTimer);
    document.getElementById('manualKindPreviews').checked = true;
    document.getElementById('manualMissingOnly').checked = true;
    document.getElementById('manualMarkersForce').checked = false;
    document.getElementById('manualSubmissionError').textContent = '';
    document.getElementById('manualSearchServer').value = '';
    document.getElementById('manualFilePaths').value = '';
    document.getElementById('manualForceRegenerate').checked = false;
    document.getElementById('manualPriority').value = '2';
    const searchInput = document.getElementById('manualSearchInput');
    if (searchInput) searchInput.value = '';
    _manualHideSearch();
    const sel = document.getElementById('manualServerScope');
    if (sel) sel.value = '';
    _populateManualServerScopePicker();
    manualRenderChips();
    _showManualKindControls();
    _wireManualModal();
    new bootstrap.Modal(document.getElementById('manualTriggerModal')).show();
}

async function startManualJob() {
    if (_manualSubmitting) return;
    const modal = document.getElementById('manualTriggerModal');
    const opening = modalOpening(modal);
    const openingSequence = _manualOpeningSequence;
    const paths = _manualPaths();
    const kind = _manualJobKind();
    const spec = _MANUAL_KINDS[kind];
    const errorText = document.getElementById('manualSubmissionError');
    const invalid = _manualPathError(paths) || (!spec ? 'Choose a supported job type.' : '');
    if (invalid) {
        errorText.textContent = invalid;
        showToast('Check selection', invalid, 'warning');
        return;
    }
    const priority = Number(document.getElementById('manualPriority').value);
    if (![1, 2, 3].includes(priority)) {
        errorText.textContent = 'Choose High, Normal or Low priority.';
        return;
    }
    const payload = { file_paths: paths, priority };
    if (kind === 'previews') {
        payload.force_regenerate = document.getElementById('manualForceRegenerate').checked;
        const serverId = document.getElementById('manualServerScope').value;
        if (serverId) payload.server_id = serverId;
    } else if (kind === 'intro_credits') {
        payload.force = document.getElementById('manualMarkersForce').checked;
    }
    const label = _manualSelections.length === 1 && !document.getElementById('manualFilePaths').value.trim()
        ? _manualSelections[0].label : `${paths.length} path(s)`;
    _manualSubmitting = true;
    const startButton = document.getElementById('manualStartButton');
    startButton.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Starting…';
    errorText.textContent = '';
    _manualUpdateStartState();
    try {
        await apiPost(spec.endpoint, payload);
        hideModalSafely(modal, opening);
        loadJobs();
        loadJobStats();
        showToast('Job Started', `${spec.label}: ${label}`, 'success');
    } catch (error) {
        if (openingSequence === _manualOpeningSequence) errorText.textContent = 'Failed to start job: ' + error.message;
        showToast('Error', 'Failed to start manual job: ' + error.message, 'danger');
    } finally {
        _manualSubmitting = false;
        startButton.innerHTML = '<i class="bi bi-play-fill me-1" aria-hidden="true"></i>Start Job';
        _manualUpdateStartState();
    }
}
