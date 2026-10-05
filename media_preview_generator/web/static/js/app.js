/**
 * Media Preview Generator - Dashboard JavaScript
 */

// Global state
let socket = null;
let libraries = [];
let jobs = [];
let schedules = [];
let _lastNotifiedJobId = null;
let processingPaused = false;
const expandedJobFileRows = new Set();
const expandedActiveJobFiles = new Set();
let cachedWorkerConfigCounts = null;
// Tracks previous fallback state per worker_id so we show a toast exactly
// once when a worker switches from GPU to CPU (not on every poll tick).
const _fallbackStateByWorker = new Map();
let cachedGpuConfig = null;
let cachedDetectedGpus = null;
let _elapsedTimerInterval = null;
let jobsLoadedOnce = false;
let jobPage = 1;
let jobPerPage = parseInt(localStorage.getItem('jobPerPage') || '50', 10);
let jobTotalPages = 1;
let jobTotal = 0;
let jobSearch = '';
let jobStatusFilter = '';
let jobServerFilter = '';
let jobLibraryFilter = '';
let jobKindFilter = '';
let jobFilterOptions = {servers: [], libraries: []};
let _jobFilterOptionsSignature = '';
let _jobSearchTimer = null;
let _jobsLoadSequence = 0;

function changeJobSearch(value) {
    jobSearch = value.trim();
    jobPage = 1;
    _jobsLoadSequence += 1;
    clearTimeout(_jobSearchTimer);
    updateJobFilterSummary();
    _jobSearchTimer = setTimeout(() => loadJobs({force: true}), 250);
}

function changeJobStatus(value) {
    jobStatusFilter = value;
    jobPage = 1;
    loadJobs({force: true});
}

function jobFiltersActive() {
    return !!(jobSearch || jobStatusFilter || jobServerFilter || jobLibraryFilter || jobKindFilter);
}

function updateJobFilterSummary() {
    const clear = document.getElementById('clearJobFilters');
    const count = document.getElementById('jobFilterCount');
    if (clear) clear.hidden = !jobFiltersActive();
    if (count) {
        count.hidden = !jobFiltersActive();
        count.textContent = `${jobTotal.toLocaleString()} ${jobTotal === 1 ? 'job matches' : 'jobs match'} these filters`;
    }
}

function updateJobLibraryChoices(resetSelection = false) {
    const select = document.getElementById('jobLibraryFilter');
    if (!select) return;
    const previousLabel = select.selectedOptions[0]?.textContent || 'Previously selected library';
    const choices = jobFilterOptions.libraries.filter(lib => !jobServerFilter || String(lib.server_id) === jobServerFilter);
    select.replaceChildren(new Option('All libraries', ''), ...choices.map(lib => new Option(`${lib.name} · ${lib.server_name || lib.server_id}`, JSON.stringify([String(lib.server_id), String(lib.id)]))));
    if (jobLibraryFilter && ![...select.options].some(option => option.value === jobLibraryFilter)) {
        if (resetSelection) jobLibraryFilter = '';
        else select.add(new Option(previousLabel, jobLibraryFilter));
    }
    select.value = jobLibraryFilter;
    select.title = select.selectedOptions[0]?.textContent || '';
}

function updateJobFilterOptions(options) {
    if (!options || !document.getElementById('jobServerFilter')) return;
    const signature = JSON.stringify(options);
    if (signature === _jobFilterOptionsSignature) return;
    _jobFilterOptionsSignature = signature;
    jobFilterOptions = {servers: options.servers || [], libraries: options.libraries || []};
    const select = document.getElementById('jobServerFilter');
    select.replaceChildren(new Option('All servers', ''), ...jobFilterOptions.servers.map(server => new Option(server.name || server.id, String(server.id))));
    if (jobServerFilter && ![...select.options].some(option => option.value === jobServerFilter)) select.add(new Option(jobServerFilter, jobServerFilter));
    select.value = jobServerFilter;
    updateJobLibraryChoices();
}

function _reloadFilteredJobs() {
    jobPage = 1;
    clearTimeout(_jobSearchTimer);
    updateJobFilterSummary();
    loadJobs({force: true});
}

function changeJobServer(value) {
    jobServerFilter = value;
    updateJobLibraryChoices(true);
    _reloadFilteredJobs();
}

function changeJobLibrary(value) {
    jobLibraryFilter = value;
    const select = document.getElementById('jobLibraryFilter');
    if (select) select.title = select.selectedOptions[0]?.textContent || '';
    _reloadFilteredJobs();
}

function changeJobKind(value) { jobKindFilter = value; _reloadFilteredJobs(); }

function clearJobFilters() {
    jobSearch = jobStatusFilter = jobServerFilter = jobLibraryFilter = jobKindFilter = '';
    for (const id of ['jobSearch', 'jobStatusFilter', 'jobServerFilter', 'jobKindFilter']) {
        const control = document.getElementById(id);
        if (control) control.value = '';
    }
    updateJobLibraryChoices();
    _reloadFilteredJobs();
}


const _HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/**
 * Escape HTML special characters to prevent XSS attacks.
 *
 * Quotes are escaped too, so the result is safe both as element text and inside a quoted attribute value
 * (title="…", data-…="…"). A file or server name can contain '"'; the old textContent/innerHTML trick left it as is.
 * @param {string} str - String to escape
 * @returns {string} - Escaped string safe for innerHTML and quoted attribute values
 */
function escapeHtml(str) {
    if (str === null || str === undefined) {
        return '';
    }
    return String(str).replace(/[&<>"']/g, (c) => _HTML_ESCAPES[c]);
}

/**
 * Sanitize a small fragment of server-rendered HTML before it hits innerHTML.
 * Allows the formatting tags the server uses in notifications (<br>, <strong>,
 * <em>, <code>, <span>, <div>, <ul>/<li>, <a>) and strips everything else —
 * including every event handler attribute (`onclick="..."`), inline-script
 * tags, <iframe>, and any `javascript:` URI. Defence in depth: the server
 * already escapes interpolated values via html.escape(), but a client-side
 * whitelist keeps the notification channel safe even if a future caller
 * forgets to escape something.
 * @param {string} html - HTML fragment
 * @returns {string} - Sanitized HTML fragment
 */
function sanitizeNotificationHtml(html) {
    if (!html) return '';
    var ALLOWED_TAGS = {
        'BR': [],
        'STRONG': [],
        'EM': [],
        'B': [],
        'I': [],
        'U': [],
        'CODE': [],
        'PRE': [],
        'UL': ['class'],
        'OL': ['class'],
        'LI': ['class'],
        'SPAN': ['class'],
        'DIV': ['class'],
        'P': ['class'],
        'SMALL': ['class'],
        // HTML5 disclosure widget — used by the migration card's "What
        // changed" expander. Without these, the unwrap path below would
        // collapse the entire <details> subtree (including allowed <ul>
        // /<li>) into one run-on text node.
        'DETAILS': ['class', 'open'],
        'SUMMARY': ['class'],
        'A': ['href', 'class', 'target', 'rel']
    };
    var template = document.createElement('template');
    template.innerHTML = html;
    var walker = document.createTreeWalker(template.content, NodeFilter.SHOW_ELEMENT);
    var toRemove = [];
    while (walker.nextNode()) {
        var el = walker.currentNode;
        var allowed = ALLOWED_TAGS[el.tagName];
        if (!allowed) {
            toRemove.push(el);
            continue;
        }
        // Strip attributes not on the whitelist; always strip anything that
        // looks like javascript:/data: URIs on href.
        for (var i = el.attributes.length - 1; i >= 0; i--) {
            var attr = el.attributes[i];
            if (allowed.indexOf(attr.name) === -1) {
                el.removeAttribute(attr.name);
                continue;
            }
            if (attr.name === 'href') {
                var v = attr.value.trim().toLowerCase();
                if (v.indexOf('javascript:') === 0 || v.indexOf('data:') === 0) {
                    el.removeAttribute('href');
                }
            }
        }
    }
    // Unwrap disallowed elements by promoting their children, not by
    // collapsing to textContent — otherwise an unknown wrapper element
    // would destroy the structure of every allowed descendant inside it.
    toRemove.forEach(function (el) {
        var parent = el.parentNode;
        if (!parent) return;
        while (el.firstChild) {
            parent.insertBefore(el.firstChild, el);
        }
        parent.removeChild(el);
    });
    return template.innerHTML;
}

var _libraryTypeLabels = {movie: 'Movies', show: 'TV Shows', sports: 'Sports', other_videos: 'Other Videos'};
var _libraryTypeIcons = {movie: 'bi-film', show: 'bi-tv', sports: 'bi-trophy', other_videos: 'bi-camera-video'};

function libraryTypeLabel(lib) {
    return _libraryTypeLabels[lib.display_type] || _libraryTypeLabels[lib.type] || lib.type;
}

function libraryTypeIcon(lib) {
    return _libraryTypeIcons[lib.display_type] || _libraryTypeIcons[lib.type] || 'bi-folder';
}

async function copyToClipboard(text, successMessage = 'Copied to clipboard', errorMessage = 'Failed to copy to clipboard') {
    const stringValue = String(text ?? '');

    try {
        if (window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            await navigator.clipboard.writeText(stringValue);
            showToast('Copied', successMessage, 'success');
            return true;
        }
    } catch (error) {
        console.debug('Clipboard API unavailable, trying fallback copy.', error);
    }

    // Legacy fallback for non-secure contexts and stricter browser policies.
    // We attach a copy listener so clipboard data is set even if selection-based
    // copying is unreliable in modals or restricted environments.
    const activeElement = document.activeElement;
    const selection = window.getSelection();
    const storedRanges = [];
    if (selection) {
        for (let i = 0; i < selection.rangeCount; i += 1) {
            storedRanges.push(selection.getRangeAt(i));
        }
    }

    const textarea = document.createElement('textarea');
    textarea.value = stringValue;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '0';
    textarea.style.opacity = '0';
    textarea.style.pointerEvents = 'none';
    document.body.appendChild(textarea);
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);

    let copied = false;
    const onCopy = (event) => {
        if (!event.clipboardData) {
            return;
        }
        event.clipboardData.setData('text/plain', stringValue);
        event.preventDefault();
        copied = true;
    };

    document.addEventListener('copy', onCopy, true);
    try {
        copied = document.execCommand('copy') || copied;
    } catch (error) {
        copied = false;
    } finally {
        document.removeEventListener('copy', onCopy, true);
        document.body.removeChild(textarea);
        if (selection) {
            selection.removeAllRanges();
            storedRanges.forEach((range) => selection.addRange(range));
        }
        if (activeElement && typeof activeElement.focus === 'function') {
            activeElement.focus({ preventScroll: true });
        }
    }

    if (copied) {
        showToast('Copied', successMessage, 'success');
    } else {
        showToast('Error', errorMessage, 'danger');
    }

    return copied;
}

// Initialize dashboard
function initDashboard() {
    connectSocket();

    const perPageSelect = document.getElementById('jobPerPageSelect');
    if (perPageSelect) {
        perPageSelect.value = String(jobPerPage);
    }

    loadJobs().then(() => loadWorkerStatuses());
    refreshStatus();
    loadLibraries();
    loadSchedules().then(() => maybeAutoOpenScheduleEdit());
    loadJobStats();
    loadWorkerConfigCounts();
    loadProcessingState();
    loadPendingWebhooks();
    requestNotificationPermission();

    // Set up auto-refresh
    // System status includes cached GPU detection — poll less frequently
    setInterval(refreshStatus, 120000);
    setInterval(loadJobStats, 10000);
    setInterval(loadJobs, 5000);
    setInterval(loadWorkerStatuses, 1000);
    setInterval(loadPendingWebhooks, 3000);
    // tickPendingWebhookCountdowns retired with the issue-237 chip:
    // the chip shows count only, no per-batch live countdown. Per-row
    // countdowns tick on _updateElapsedTimers (in lockstep with the
    // retry countdown) so we don't need a parallel 1Hz timer here.

    // Flush deferred job-queue updates when a priority dropdown closes
    document.addEventListener('hidden.bs.dropdown', function () {
        if (_jobQueueUpdatePending) {
            updateJobQueue();
        }
    });
}

// SocketIO Connection
function connectSocket() {
    // Polling-only — matches allow_upgrades=False on the server. WebSocket
    // pinned a gunicorn thread per browser tab and dead CLOSE_WAIT sockets
    // exhausted the pool. Skip the WS upgrade attempt entirely so we don't
    // pay the failed-handshake round-trip on every reconnect.
    socket = io('/jobs', {
        transports: ['polling'],
        reconnection: true,
        reconnectionAttempts: 10,
        reconnectionDelay: 1000
    });

    socket.on('connect', function() {
        console.log('Connected to SocketIO');
        // Reload data on reconnect to get current state
        loadJobs();
        loadWorkerStatuses();
        loadJobStats();
    });

    socket.on('disconnect', function() {
        console.log('Disconnected from SocketIO');
    });

    socket.on('connect_error', function(error) {
        console.error('SocketIO connection error:', error);
    });

    // Job events
    socket.on('job_created', function(job) {
        console.log('Job created:', job);
        loadJobs();
        loadJobStats();
        if (!_isUserVisibleJob(job)) return;
        showToast('Job Created', `Job ${job.id.substring(0, 8)} created`, 'info');
    });

    socket.on('job_updated', function(job) {
        loadJobs();
    });

    socket.on('job_started', function(job) {
        console.log('Job started:', job);
        loadJobs();
        loadJobStats();
    });

    socket.on('job_progress', function(data) {
        updateJobProgress(data.job_id, data.progress, data.publishers);
    });

    socket.on('worker_update', function(data) {
        const workers = data.workers || [];
        if (workers.length > 0) {
            updateWorkerStatuses(workers);
        } else {
            updateWorkerStatuses([], { keepBadgeCounts: true });
        }
    });

    socket.on('job_completed', function(job) {
        console.log('Job completed:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        removeActiveJob(job.id);
        if (!_isUserVisibleJob(job)) return;
        if (job.error) {
            showToast('Job completed with warnings', job.error, 'warning');
            showNotification('Job completed with warnings', job.error, 'warning');
        } else {
            showToast('Job Completed', `Job ${job.id.substring(0, 8)} completed successfully`, 'success');
            showNotification('Job Completed', `Processing finished for ${job.library_name || 'All Libraries'}`, 'success');
        }
    });

    socket.on('job_failed', function(job) {
        console.log('Job failed:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        removeActiveJob(job.id);
        if (!_isUserVisibleJob(job)) return;
        showToast('Job Failed', `Job ${job.id.substring(0, 8)} failed: ${job.error}`, 'danger');
        showNotification('Job Failed', job.error || 'Unknown error', 'error');
    });

    socket.on('job_cancelled', function(job) {
        console.log('Job cancelled:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        removeActiveJob(job.id);
        if (!_isUserVisibleJob(job)) return;
        showToast('Job Cancelled', `Job ${job.id.substring(0, 8)} cancelled`, 'warning');
    });

    socket.on('job_paused', function(data) {
        console.log('Job paused:', data);
        loadJobs();
        loadWorkerStatuses();
    });

    socket.on('job_resumed', function(data) {
        console.log('Job resumed:', data);
        loadJobs();
        loadWorkerStatuses();
    });

    socket.on('processing_paused_changed', function(data) {
        processingPaused = !!data.paused;
        renderGlobalPauseResume();
        updateWorkerStatuses(_latestDashboardWorkers);
        loadJobs();
        if (window.refreshQuietHoursState) window.refreshQuietHoursState();
        if (window.WorkerGroups) window.WorkerGroups.load();
    });

}

// API Helpers

/**
 * Read the CSRF token from the <meta name="csrf-token"> tag injected by Flask.
 * @returns {string} The CSRF token, or empty string if not found.
 */
function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

// Extract a user-friendly error message from a non-OK fetch Response.
// Prefers the backend's `{ error: "..." }` field when present, so callers
// surface "Could not connect to Plex at ..." instead of "HTTP 502: BAD GATEWAY".
async function _extractApiError(response) {
    const text = await response.text().catch(() => '');
    if (text) {
        try {
            const json = JSON.parse(text);
            if (json && typeof json.error === 'string' && json.error.trim()) {
                return json.error.trim();
            }
        } catch (_e) {
            // Non-JSON body — fall through to the generic status line.
        }
    }
    return `HTTP ${response.status}: ${response.statusText}`;
}

async function apiGet(url) {
    const response = await fetch(url);
    if (!response.ok) {
        if (response.status === 401) {
            console.error('Authentication failed, redirecting to login');
            window.location.href = '/login';
            throw new Error('Authentication required');
        }
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

async function apiPost(url, data = {}) {
    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken()
        },
        body: JSON.stringify(data)
    });
    if (!response.ok) {
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

async function apiDelete(url) {
    const response = await fetch(url, {
        method: 'DELETE',
        headers: { 'X-CSRFToken': getCsrfToken() }
    });
    if (!response.ok) {
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

async function apiPut(url, data = {}) {
    const response = await fetch(url, {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken()
        },
        body: JSON.stringify(data)
    });
    if (!response.ok) {
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

// Load Functions
async function refreshStatus() {
    // Each section is independently try-caught so a failure in one
    // (e.g. /api/system/config) does not cascade to others.

    // --- System status (GPU info + running job) ---
    try {
        const status = await apiGet('/api/system/status');
        updateSystemStatus(status);
    } catch (error) {
        console.error('Failed to refresh system status:', error);
        const statusEl = document.getElementById('systemStatus');
        if (statusEl && !error.message.includes('Authentication')) {
            statusEl.innerHTML = '<div class="text-danger small"><i class="bi bi-exclamation-triangle me-2"></i>Failed to load status</div>';
        }
    }

    // --- Media servers status (one row per configured server) ---
    await updateMediaServersStatus();

    // --- Worker thread counts ---
    // Keep config counts cached for idle-state badge rendering. Actual live
    // worker badges are updated by /api/jobs/workers to avoid race/flicker.
    try {
        await loadWorkerConfigCounts(true);
        renderDashboardGpuConfig();
    } catch (e) {
        console.warn('Failed to load worker config:', e);
    }
}

function normalizeWorkerConfigCounts(config) {
    // gpu_threads is the total across all enabled GPUs (computed from gpu_config)
    return {
        gpu_threads: Number(config?.gpu_threads ?? 0),
        cpu_threads: Number(config?.cpu_threads ?? 1),
        // The server rejects a saved CPU count above this; unknown until the config loads.
        cpu_threads_max: Number(config?.cpu_threads_max ?? Infinity)
    };
}

async function loadWorkerConfigCounts(forceRefresh = false) {
    if (cachedWorkerConfigCounts && !forceRefresh) {
        return cachedWorkerConfigCounts;
    }

    try {
        const config = await apiGet('/api/system/config');
        cachedWorkerConfigCounts = normalizeWorkerConfigCounts(config);
        cachedGpuConfig = config.gpu_config || [];
        renderNoWorkersWarning(config.config_warning || '');
        return cachedWorkerConfigCounts;
    } catch (e) {
        console.warn('Failed to cache worker config counts:', e);
    }

    return cachedWorkerConfigCounts;
}

// Tracks whether the last /api/libraries call failed so modal renderers
// (which read the cached `libraries` array) can tell "no libraries" apart
// from "couldn't load libraries" and show the right message.
let librariesLoadError = null;

// Map a backend status string from /api/system/media-servers into a
// (label, badge-class) pair so the dashboard renders consistently.
const _MEDIA_SERVER_STATUS_BADGES = {
    connected:     { label: 'Connected',     cls: 'bg-success' },
    unreachable:   { label: "Can't reach",   cls: 'bg-warning text-dark' },
    unauthorised:  { label: 'Auth failed',   cls: 'bg-warning text-dark' },
    misconfigured: { label: 'Misconfigured', cls: 'bg-danger' },
    disabled:      { label: 'Disabled',      cls: 'bg-secondary' },
};

const _MEDIA_SERVER_TYPE_ICONS = {
    plex:     'bi-play-btn',
    emby:     'bi-emoji-laughing',
    jellyfin: 'bi-cup-hot',
};

// Vendor SVG logos shipped under /static/images/vendors/. Returns an
// <img> tag for the given server type, or null when the type is unknown
// (callers fall back to the Bootstrap icon).
function _vendorLogo(type, size) {
    const stype = (type || '').toLowerCase();
    if (!['plex', 'emby', 'jellyfin'].includes(stype)) return null;
    const px = Number(size) || 18;
    // alt="" + aria-hidden so screen-readers + copy/paste don't double-announce
    // the vendor name (visible badge text already says "Plex"/"Emby"/etc.).
    // Without this, users reading the row see "plexPlex 1 not indexed yet"
    // instead of just "Plex 1 not indexed yet" because the alt-text bleeds
    // into rendered textContent in many AT and copy paths.
    return (
        `<img src="/static/images/vendors/${stype}.svg" alt="" aria-hidden="true" ` +
        `width="${px}" height="${px}" class="vendor-logo" ` +
        `style="vertical-align: -3px; margin-right: 4px;">`
    );
}

// GPU vendor SVG logos. Returns an <img> tag for known vendors, or null when
// we don't ship an icon (caller falls back to the previous text badge). Type
// values come from the backend as uppercase strings ("NVIDIA", "INTEL", "AMD",
// "APPLE", and the exotic "ARM"/"VIDEOCORE"/"UNKNOWN" which intentionally
// don't have icons).
const _GPU_VENDOR_LOGO_TYPES = ['nvidia', 'intel', 'amd', 'apple'];

function _gpuVendorLogo(type, size) {
    const stype = (type || '').toLowerCase();
    if (!_GPU_VENDOR_LOGO_TYPES.includes(stype)) return null;
    const px = Number(size) || 18;
    const label = stype.toUpperCase();
    return (
        `<img src="/static/images/vendors/${stype}.svg" alt="" aria-hidden="true" ` +
        `width="${px}" height="${px}" class="gpu-vendor-logo" title="${label}" ` +
        `style="vertical-align: -3px; margin-right: 4px;">`
    );
}

window.MPGShared = window.MPGShared || {};
window.MPGShared.gpuVendorLogo = _gpuVendorLogo;

function updateSystemDisclosure() {
    const card = document.querySelector('.dashboard-system-card');
    const toggle = card?.querySelector('.system-details-toggle');
    if (!toggle) return;
    const expanded = window.innerWidth >= 768 || !card.classList.contains('system-details-collapsed');
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-label', `${expanded ? 'Hide' : 'Show'} system details, ${document.getElementById('systemConnectionSummary').textContent}`);
    toggle.querySelector('i').className = `bi bi-chevron-${expanded ? 'up' : 'down'}`;
}

function toggleSystemDetails() {
    document.querySelector('.dashboard-system-card')?.classList.toggle('system-details-collapsed');
    updateSystemDisclosure();
}

function updateSystemConnections(servers, failed = false) {
    const card = document.querySelector('.dashboard-system-card');
    const summary = document.getElementById('systemConnectionSummary');
    if (!card || !summary) return;
    const connected = servers.filter(server => server.status === 'connected').length;
    const issue = failed || !servers.length || servers.some(server => !['connected', 'checking', 'connecting'].includes(server.status));
    summary.textContent = failed ? 'Connection details need attention' : servers.length ?
        `${connected} of ${servers.length} connected` : 'No servers configured';
    if (issue && card.dataset.connectionIssue !== 'true') card.classList.remove('system-details-collapsed');
    card.dataset.connectionIssue = String(issue);
    updateSystemDisclosure();
}

window.matchMedia('(max-width: 767.98px)').addEventListener('change', updateSystemDisclosure);

// Refresh the dashboard "Media Servers" rows from /api/system/media-servers.
// Renders one row per configured server with vendor icon + status badge.
// Empty state nudges the user to /servers.
async function updateMediaServersStatus() {
    const container = document.getElementById('mediaServersStatus');
    const emptyCard = document.getElementById('noServersEmptyState');
    if (!container) return;

    let payload;
    try {
        payload = await apiGet('/api/system/media-servers');
    } catch (e) {
        console.warn('Failed to load media-server status:', e);
        container.innerHTML =
            '<div class="text-danger small">' +
            '<i class="bi bi-exclamation-triangle me-2"></i>' +
            'Failed to load media-server status' +
            '</div>';
        updateSystemConnections([], true);
        return;
    }

    const servers = (payload && payload.servers) || [];
    updateSystemConnections(servers);
    if (servers.length === 0) {
        // Show the dashboard empty-state card (Phase H1) and a small inline note.
        if (emptyCard) emptyCard.classList.remove('d-none');
        container.innerHTML =
            '<div class="small text-muted">' +
            '<i class="bi bi-hdd-network me-2"></i>' +
            'No media servers configured. ' +
            '<a href="/servers">Add one</a>.' +
            '</div>';
        return;
    }
    // Hide the empty-state once at least one server is configured.
    if (emptyCard) emptyCard.classList.add('d-none');

    const rows = servers.map(s => {
        const badge = _MEDIA_SERVER_STATUS_BADGES[s.status]
            || { label: s.status || 'Unknown', cls: 'bg-secondary' };
        const typeLabel = (s.type || '').toUpperCase();
        const tooltip = s.error ? ` title="${escapeHtmlAttr(s.error)}"` : '';
        // URL is reference info, not the primary anchor — render smaller
        // and dimmer than the server name above it so the eye lands on
        // the name + status badge first.
        const url = s.url ? `<div class="text-muted text-truncate" style="max-width: 100%; font-size: 0.72rem; opacity: 0.7;" title="${escapeHtmlAttr(s.url)}">${escapeHtmlText(s.url)}</div>` : '';
        // Prefer the vendor SVG logo; fall back to the Bootstrap icon when
        // the server type is unknown (defensive — should never happen for
        // configured servers).
        const logo = _vendorLogo(s.type, 18) ||
            `<i class="bi ${_MEDIA_SERVER_TYPE_ICONS[s.type] || 'bi-hdd-network'} me-2"></i>`;
        return `
            <div class="d-flex justify-content-between align-items-start mb-2">
                <div class="d-flex flex-column" style="min-width: 0;">
                    <span>${logo}<strong>${escapeHtmlText(s.name || typeLabel || 'Server')}</strong></span>
                    ${url}
                </div>
                <span class="badge ${badge.cls}"${tooltip}>${escapeHtmlText(badge.label)}</span>
            </div>
        `;
    }).join('');

    container.innerHTML = rows;
}

// Kept for their callers: escapeHtml itself is safe in both places now.
function escapeHtmlText(str) {
    return escapeHtml(str);
}

function escapeHtmlAttr(str) {
    return escapeHtml(str);
}

async function loadLibraries() {
    try {
        const data = await apiGet('/api/libraries');
        libraries = data.libraries || [];
        librariesLoadError = null;
        await updateLibraryList();
        if (document.getElementById('newJobModal')?.classList.contains('show')) {
            _renderJobLibraryList(libraries);
            _updateJobScopeBadge();
        }
        updateMediaServersStatus();
    } catch (error) {
        console.error('Failed to load libraries:', error);
        librariesLoadError = error.message || 'Unknown error';
        updateMediaServersStatus();

        const listEl = document.getElementById('libraryList');
        if (!listEl) return;

        // Dashboard Quick Actions teaser stays short and actionable; the
        // Settings page gets the full backend-supplied detail since it's a
        // dedicated troubleshooting surface.
        const parentCardHeader = listEl.closest('.card')?.querySelector('.card-header')?.textContent || '';
        const isDashboardTeaser = parentCardHeader.includes('Quick Actions');

        if (isDashboardTeaser) {
            listEl.innerHTML =
                '<div class="text-warning small d-flex align-items-start gap-2">' +
                '<i class="bi bi-exclamation-triangle-fill mt-1"></i>' +
                '<span>Can\'t load libraries right now. ' +
                '<a href="/settings" class="text-decoration-none">Check your Plex connection</a>.</span>' +
                '</div>';
        } else {
            listEl.innerHTML =
                `<div class="text-danger small">Failed to load libraries. ${escapeHtml(librariesLoadError)}</div>`;
        }
    }
}

// ``{force: true}`` re-renders even while the pointer is over the queue: after the user's own Pause/Resume click has
// landed, the hover guard would otherwise keep the row stale until the pointer moves away.
async function loadJobs(options) {
    const force = !!(options && options.force === true);
    const sequence = ++_jobsLoadSequence;
    try {
        const params = new URLSearchParams({page: jobPage, per_page: jobPerPage});
        if (jobSearch) params.set('q', jobSearch);
        if (jobStatusFilter) params.set('status', jobStatusFilter);
        if (jobServerFilter) params.set('server_id', jobServerFilter);
        if (jobKindFilter) params.set('kind', jobKindFilter);
        if (jobLibraryFilter) {
            const [serverId, libraryId] = JSON.parse(jobLibraryFilter);
            params.set('library_server_id', serverId);
            params.set('library_id', libraryId);
        }
        const data = await apiGet('/api/jobs?' + params.toString());
        if (sequence !== _jobsLoadSequence) return;
        jobs = data.jobs || [];
        jobTotal = data.total || 0;
        jobTotalPages = data.pages || 1;
        updateJobFilterOptions(data.filter_options);
        updateJobFilterSummary();
        if (jobPage > jobTotalPages) {
            jobPage = jobTotalPages;
        }
        const wasFirstLoad = !jobsLoadedOnce;
        jobsLoadedOnce = true;
        updateJobQueue(force);
        renderJobPagination();

        // Deep-link auto-open (Tier 3.15 of job-modal rebuild): if the
        // page was loaded with ``?job=<id>`` in the URL, pop the modal
        // open now that jobs.find() can resolve. Run only on the
        // initial load — subsequent polls don't re-open the modal.
        if (wasFirstLoad && typeof _autoOpenModalFromUrl === 'function') {
            _autoOpenModalFromUrl();
        }

        // Active Jobs shows only jobs holding a JobGate slot
        // (status='running'). Pre-dispatch states (retry-backoff wait,
        // queued-at-gate) keep status='pending' and stay in the lower
        // Job Queue table — surfacing them here lets the panel count
        // exceed ``max_concurrent_jobs``, which contradicts the cap
        // the user just configured.
        // An Intro & Credits job paused on its own is running but has handed its slot back, so it isn't active.
        const activeJobs = jobs.filter(j => j.status === 'running' && _markersPauseState(j) !== 'own');
        updateActiveJobs(activeJobs, force);

        // Replay any progress events that arrived before the DOM was ready.
        for (const jid of Object.keys(_pendingProgress)) {
            updateJobProgress(jid, _pendingProgress[jid]);
        }
    } catch (error) {
        if (sequence !== _jobsLoadSequence) return;
        console.error('Failed to load jobs:', error);
        // Show empty state instead of error - jobs list may just be unavailable temporarily
        const tbody = document.getElementById('jobQueue');
        if (tbody && !error.message.includes('Authentication')) {
            // Show a less alarming message
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-4">
                        <i class="bi bi-hourglass-split me-2"></i>Loading job queue...
                    </td>
                </tr>
            `;
        }
    }
}

async function loadSchedules() {
    try {
        const data = await apiGet('/api/schedules');
        schedules = data.schedules || [];
        // Carry the backend's load_status onto a window global so the
        // schedules table renderer can surface a recovery banner when the
        // schedules.json file failed to load (the typical cause is wrong
        // file ownership in the user's container — pre-fix the user just
        // saw an empty list with no hint).
        window._schedulesLoadStatus = data.load_status || { status: 'ok' };
        updateScheduleList();
    } catch (error) {
        console.error('Failed to load schedules:', error);
    }
}

// Check for ?editSchedule=<id> in the URL on page load — when a user
// clicked "Edit" on a scanner from the Triggers tab we want to scroll
// to the schedules table and open the edit modal for that specific
// schedule.  Only does work when the full schedule table is on the
// page (the Automation page); on the Dashboard it redirects to /automation
// so stale links still land in the right place.
function maybeAutoOpenScheduleEdit() {
    try {
        const params = new URLSearchParams(window.location.search);
        const editId = params.get('editSchedule');
        if (!editId) return;

        const scheduleTable = document.getElementById('scheduleList');
        if (!scheduleTable) {
            // Dashboard or any page without the full table — redirect to
            // the Automation page (Schedules tab) and let it handle the edit.
            window.location.replace(
                '/automation?tab=schedules&editSchedule=' + encodeURIComponent(editId) + '#schedules'
            );
            return;
        }

        // Scroll the schedules card into view with a bit of breathing
        // room below the sticky navbar.
        const row = scheduleTable.closest('.card');
        const navbar = document.querySelector('.navbar.sticky-top');
        const navHeight = navbar ? navbar.offsetHeight : 56;
        const target = (row || scheduleTable).getBoundingClientRect().top + window.scrollY - navHeight - 16;
        window.scrollTo({ top: target < 0 ? 0 : target, behavior: 'smooth' });

        if (schedules.some(s => s.id === editId)) {
            // Tiny delay so the scroll animation starts before the modal
            // opens — feels smoother than a dead-snap.
            setTimeout(() => showEditScheduleModal(editId), 200);
        } else {
            console.warn('editSchedule=' + editId + ' not found in schedules list');
            showToast('Not found', 'Schedule not found — it may have been deleted.', 'warning');
        }

        // Clean the query param from the URL so a refresh doesn't re-trigger.
        if (window.history && window.history.replaceState) {
            const url = new URL(window.location.href);
            url.searchParams.delete('editSchedule');
            window.history.replaceState(null, '', url.pathname + url.search + url.hash);
        }
    } catch (e) {
        console.error('maybeAutoOpenScheduleEdit failed:', e);
    }
}


async function loadJobStats() {
    // Job Statistics card lives only on the dashboard; bail when its
    // elements are absent so SocketIO reconnect from any other page
    // doesn't throw on the first getElementById().textContent assignment.
    if (!document.getElementById('statPending')) {
        return;
    }
    try {
        const stats = await apiGet('/api/jobs/stats');
        document.getElementById('statPending').textContent = stats.pending || 0;
        document.getElementById('statRunning').textContent = stats.running || 0;
        document.getElementById('statCompleted').textContent = stats.completed || 0;
        document.getElementById('statFailed').textContent = stats.failed || 0;
        document.getElementById('statCancelled').textContent = stats.cancelled || 0;
        document.getElementById('statTotal').textContent = stats.total || 0;
    } catch (error) {
        console.error('Failed to load job stats:', error);
    }
}

async function loadProcessingState() {
    try {
        const data = await apiGet('/api/processing/state');
        const changed = processingPaused !== !!data.paused;
        processingPaused = !!data.paused;
        renderGlobalPauseResume();
        updateWorkerStatuses(_latestDashboardWorkers);
        // Running Intro & Credits jobs show as held while Pause all is on; the first jobs render may predate this.
        if (changed && jobsLoadedOnce) loadJobs();
    } catch (error) {
        console.error('Failed to load processing state:', error);
    }
}

// Maintain the "N waiting" chip in the Job Queue card header. Pre-issue-237
// UX this was a full-width yellow alert at the top of the page with an
// inline countdown + Fire-now button per batch. The per-row countdown +
// Fire-now lives in the queue table itself now (see ``renderJobQueue``);
// this chip is just the at-a-glance "you have stuff waiting" indicator.
// Click → scroll to the first row carrying ``data-webhook-fire-at``.
async function loadPendingWebhooks() {
    const chip = document.getElementById('pendingWebhooksChip');
    const chipText = document.getElementById('pendingWebhooksChipText');
    if (!chip || !chipText) return;

    try {
        const data = await apiGet('/api/webhooks/pending');
        const pending = data.pending || [];
        if (pending.length === 0) {
            chip.classList.add('d-none');
            chip.classList.remove('d-inline-flex');
            return;
        }
        // ``d-inline-flex`` (rather than the default block) keeps the
        // chip aligned with the other right-side header controls (which
        // sit in a flex row already).
        chip.classList.remove('d-none');
        chip.classList.add('d-inline-flex');
        const n = pending.length;
        chipText.textContent = `${n} waiting`;
        chip.title = n === 1
            ? 'One webhook is waiting — click to jump to its row in the queue'
            : `${n} webhooks waiting — click to jump to the next one in the queue`;
    } catch (error) {
        // Network blip: hide rather than show a stale count.
        chip.classList.add('d-none');
        chip.classList.remove('d-inline-flex');
    }
}

// Click handler for the chip — scroll to the first job row carrying a
// webhook countdown. If the row isn't on the page yet (e.g. the queue
// hasn't loaded), fall back to scrolling to the queue card itself.
document.addEventListener('click', function (e) {
    const chip = e.target.closest && e.target.closest('#pendingWebhooksChip');
    if (!chip) return;
    e.preventDefault();
    const target =
        document.querySelector('[data-webhook-fire-at]')
        || document.querySelector('#jobQueue')
        || document.querySelector('.jobs-table');
    if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        // Briefly highlight the row so the user's eye lands on it after
        // the scroll. The flash class is removed on transitionend so
        // re-clicks animate cleanly. CSS lives alongside other
        // job-row styles.
        const row = target.closest && target.closest('tr');
        if (row) {
            row.classList.add('queue-row-flash');
            setTimeout(function () { row.classList.remove('queue-row-flash'); }, 1500);
        }
    }
});

// Per-row "Fire now" (webhook debounce) — POSTs the job-scoped
// /api/jobs/<id>/fire-webhook-now and refreshes jobs + banner so the
// row transitions out of pending and the banner clears any stale
// _pending_batches reference.
async function fireWebhookNow(jobId) {
    if (!jobId) return;
    try {
        await apiPost('/api/jobs/' + encodeURIComponent(jobId) + '/fire-webhook-now', {});
        showToast('Webhook Fired', 'Skipped the initial wait — dispatching now.', 'success');
        await Promise.all([loadJobs(), loadPendingWebhooks()]);
    } catch (error) {
        showToast('Error', 'Could not fire webhook: ' + (error && error.message || error), 'danger');
    }
}

// Per-row "Retry now" (chain backoff) — POSTs to the existing
// /api/jobs/<id>/retry-now endpoint (already consumed by the modal
// footer button). Mirrors fireWebhookNow above so both row controls
// share the same success/error toast pattern.
async function retryNowFromRow(jobId) {
    if (!jobId) return;
    try {
        await apiPost('/api/jobs/' + encodeURIComponent(jobId) + '/retry-now', {});
        showToast('Retry Fired', 'Skipped the backoff — attempting now.', 'success');
        await loadJobs();
    } catch (error) {
        showToast('Error', 'Could not fire retry: ' + (error && error.message || error), 'danger');
    }
}

function renderGlobalPauseResume() {
    const pauseTitle = 'Pause all processing, including current files. Worker-group reductions instead let current files finish.';
    const resumeTitle = 'Clear the manual pause. Global pause schedules and job-specific pauses still apply.';
    // Hide the verbose "Pause Processing" / "Resume Processing" label on
    // mobile (xs <576px) — keep the icon. Title attr + aria-label keep
    // it accessible and tooltip-discoverable. On desktop the full label
    // returns via .d-sm-inline.
    const pauseBtn = `<button class="btn btn-sm btn-outline-warning text-nowrap" onclick="pauseProcessing()" title="${escapeHtml(pauseTitle)}" aria-label="Pause all processing, including current files">
        <i class="bi bi-pause-fill"></i><span class="d-none d-sm-inline ms-1">Pause Processing</span>
    </button>`;
    const resumeBtn = `<button class="btn btn-sm btn-outline-success text-nowrap" onclick="resumeProcessing()" title="${escapeHtml(resumeTitle)}" aria-label="Resume all processing">
        <i class="bi bi-play-fill"></i><span class="d-none d-sm-inline ms-1">Resume Processing</span>
    </button>`;
    const html = processingPaused ? resumeBtn : pauseBtn;
    const elQueue = document.getElementById('globalPauseResumeQueue');
    if (elQueue) elQueue.innerHTML = html;
}

async function pauseProcessing() {
    try {
        await apiPost('/api/processing/pause');
        processingPaused = true;
        renderGlobalPauseResume();
        await loadJobs();
        showToast('Processing Paused', 'Processing is paused, including current files.', 'warning');
        if (window.WorkerGroups) window.WorkerGroups.load();
    } catch (error) {
        showToast('Error', 'Failed to pause processing: ' + error.message, 'danger');
    }
}

async function resumeProcessing() {
    try {
        const result = await apiPost('/api/processing/resume');
        processingPaused = !!result.paused;
        renderGlobalPauseResume();
        await loadJobs();
        showToast(processingPaused ? 'Manual Pause Cleared' : 'Processing Resumed', processingPaused ? 'The global pause schedule is still active. Work continues when it ends.' : 'Eligible jobs can continue; job-specific pauses still apply.', processingPaused ? 'warning' : 'success');
        if (window.WorkerGroups) window.WorkerGroups.load();
    } catch (error) {
        showToast('Error', 'Failed to resume processing: ' + error.message, 'danger');
    }
}

// Update Functions
function updateSystemStatus(status) {
    let html = '';

    // Cache detected GPUs for the GPU Workers section
    if (status.gpus && status.gpus.length > 0) {
        cachedDetectedGpus = status.gpus;
    } else {
        cachedDetectedGpus = [];
    }

    html += '<div class="d-flex align-items-center">';
    if (status.running_job) {
        html += `<span class="badge bg-primary">Processing</span>`;
    } else if (status.pending_jobs > 0) {
        html += `<span class="badge bg-secondary">${status.pending_jobs} job(s) pending</span>`;
    } else {
        html += `<span class="badge bg-success">Idle</span>`;
    }
    html += '</div>';

    document.getElementById('systemStatus').innerHTML = html;

    // Timezone + Vulkan warnings live in the bell-icon notification
    // center (see loadNotifications()), not as dashboard banners.

    renderDashboardGpuConfig();
}

// Copy the plain-text Vulkan diagnostic bundle from /api/system/vulkan/debug
// to the clipboard. Bound to the "Copy diagnostic bundle" button inside
// the dashboard and settings-page vulkan warning banners.
function copyVulkanDiagnosticBundle(btn) {
    var originalHtml = btn ? btn.innerHTML : '';
    function restore(html, ms) {
        if (!btn) return;
        setTimeout(function () { btn.innerHTML = originalHtml; }, ms || 2000);
        btn.innerHTML = html;
    }
    fetch('/api/system/vulkan/debug')
        .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
        })
        .then(function (text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                return navigator.clipboard.writeText(text).then(function () { return text; });
            }
            // Fallback for older browsers / non-secure contexts.
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'absolute';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            return text;
        })
        .then(function () {
            restore('<i class="bi bi-check2 me-1"></i>Copied to clipboard');
        })
        .catch(function (err) {
            console.error('copyVulkanDiagnosticBundle failed:', err);
            restore('<i class="bi bi-x-circle me-1"></i>Copy failed — see console', 4000);
        });
}


function renderNoWorkersWarning(message) {
    let banner = document.getElementById('noWorkersWarningBanner');
    if (!message) {
        if (banner) banner.classList.add('d-none');
        return;
    }
    if (!banner) {
        const row = document.createElement('div');
        row.id = 'noWorkersWarningRow';
        row.className = 'row';
        row.innerHTML = `<div class="col-12 mb-3">
            <div class="alert alert-warning mb-0 d-flex align-items-center" id="noWorkersWarningBanner">
                <i class="bi bi-exclamation-triangle-fill me-2"></i>
                <span id="noWorkersWarningText"></span>
            </div>
        </div>`;
        const workerSection = document.getElementById('workerStatusContainer');
        if (workerSection) {
            workerSection.closest('.row').before(row);
        }
        banner = document.getElementById('noWorkersWarningBanner');
    }
    const textEl = document.getElementById('noWorkersWarningText');
    if (textEl) textEl.textContent = message;
    banner.classList.remove('d-none');
}

function renderDashboardGpuConfig() {
    const container = document.getElementById('gpuWorkerConfig');
    if (!container) return;

    const gpus = cachedDetectedGpus || [];
    const gpuConfig = cachedGpuConfig || [];

    if (gpus.length === 0) {
        container.innerHTML = '<span class="text-muted small">No GPUs detected</span>';
        return;
    }

    const configByDevice = {};
    gpuConfig.forEach(c => { if (c.device) configByDevice[c.device] = c; });

    let html = '<h6 class="mb-2"><i class="bi bi-gpu-card me-2"></i>GPU Workers</h6>';
    for (const gpu of gpus) {
        const device = gpu.device || '';
        const saved = configByDevice[device] || {};
        const safeDevice = escapeHtml(device);
        const isFailed = gpu.status === 'failed';

        const fullNameTitle = escapeHtml(`${gpu.name || 'GPU'}${device ? ' · ' + device : ''}`);
        if (isFailed) {
            const errorTitle = escapeHtml(gpu.error || 'GPU unusable');
            const errorDetail = escapeHtml(gpu.error_detail || '');
            const failedVendorMark = _gpuVendorLogo(gpu.type, 18)
                || `<span class="badge bg-primary me-1" style="font-size: 0.65em;">${escapeHtml(gpu.type).toUpperCase()}</span>`;
            html += `<div class="d-flex justify-content-between align-items-center mb-2">`;
            html += `<span class="text-truncate me-2" style="max-width: 70%;" title="${fullNameTitle}">`;
            html += failedVendorMark;
            html += `${escapeHtml(gpu.name)} <span class="badge bg-danger">failed</span>`;
            html += `</span>`;
            html += `<span><i class="bi bi-exclamation-triangle-fill text-danger" style="cursor:pointer;" `;
            html += `data-bs-toggle="popover" data-bs-trigger="click" data-bs-placement="left" `;
            html += `data-bs-title="${errorTitle}" `;
            html += `data-bs-content="${errorDetail}"></i></span>`;
            html += `</div>`;
            continue;
        }

        const enabled = saved.enabled !== undefined ? saved.enabled : true;
        const workers = saved.workers !== undefined ? saved.workers : 1;

        // Enabled/disabled was shown as a "enabled"/"disabled" badge
        // next to the GPU name, but on long names (e.g. "NVIDIA GeForce
        // RTX 4070 Ti SUPER") the surrounding text-truncate clipped the
        // badge — and the badge duplicated info already carried by the
        // right-side controls (workers ±  vs. an "Enable" button).
        // Drop the badge; signal disabled state by greying the whole
        // row instead. The Enable button on the right still surfaces
        // the action when the user does want to bring the GPU back.
        const rowStateCls = enabled ? '' : ' text-muted opacity-75';

        const vendorMark = _gpuVendorLogo(gpu.type, 18)
            || `<span class="badge bg-primary me-1" style="font-size: 0.65em;">${escapeHtml(gpu.type).toUpperCase()}</span>`;
        html += `<div class="d-flex justify-content-between align-items-center mb-2${rowStateCls}">`;
        // No status badge any more → give the name slot more room.
        // 70% leaves a comfortable margin for the right-side controls
        // even for the widest button (the "Enable" button on the
        // disabled-row path).
        html += `<span class="text-truncate me-2" style="max-width: 70%;" title="${fullNameTitle}${enabled ? '' : ' · disabled'}">`;
        html += vendorMark;
        html += `${escapeHtml(gpu.name)}`;
        html += `</span>`;
        if (enabled) {
            html += `<span class="d-flex align-items-center gap-1">`;
            html += `<button type="button" class="btn btn-sm btn-outline-secondary gpu-scale-btn" onclick="scaleGpuWorkers('${safeDevice}', -1)" title="Remove one worker"${workers <= 0 ? ' disabled' : ''}><i class="bi bi-dash-lg"></i></button>`;
            html += `<span class="badge bg-primary gpu-worker-badge" data-device="${safeDevice}" style="min-width: 1.5rem;">${workers}</span>`;
            html += `<button type="button" class="btn btn-sm btn-outline-success gpu-scale-btn" onclick="scaleGpuWorkers('${safeDevice}', 1)" title="Add one worker"><i class="bi bi-plus-lg"></i></button>`;
            html += `</span>`;
        } else {
            const enableTitle = (saved.workers || 0) > 0
                ? `Re-enable GPU (${saved.workers} worker${saved.workers === 1 ? '' : 's'})`
                : 'Enable with 1 worker';
            html += `<button type="button" class="btn btn-sm btn-outline-success" onclick="scaleGpuWorkers('${safeDevice}', 1)" title="${enableTitle}"><i class="bi bi-power me-1"></i>Enable</button>`;
        }
        html += `</div>`;
    }
    html += `<div class="mt-1"><a href="/settings#gpu-configuration" class="small text-decoration-none"><i class="bi bi-gear me-1"></i>Configure GPUs in Settings</a></div>`;
    container.innerHTML = html;

    container.querySelectorAll('[data-bs-toggle="popover"]').forEach(el => {
        new bootstrap.Popover(el, { html: false });
    });
}

async function scaleGpuWorkers(device, direction) {
    const detectedGpu = (cachedDetectedGpus || []).find(g => g.device === device);
    if (detectedGpu && detectedGpu.status === 'failed') return;

    const gpuConfig = cachedGpuConfig ? JSON.parse(JSON.stringify(cachedGpuConfig)) : [];
    let entry = gpuConfig.find(e => e.device === device);

    if (!entry) {
        if (!detectedGpu) return;
        entry = {
            device: device,
            name: detectedGpu.name || 'GPU',
            type: detectedGpu.type || '',
            enabled: true,
            workers: 1,
            ffmpeg_threads: 2
        };
        gpuConfig.push(entry);
    }

    const prevWorkers = entry.workers || 0;
    const prevEnabled = entry.enabled !== false;
    let newWorkers;
    let newEnabled;
    if (!prevEnabled && direction > 0) {
        // Re-enabling a disabled GPU: restore its previous worker count
        // rather than bumping it by +1. Disabling via the settings-page
        // toggle leaves the saved `workers` value untouched (only flips
        // `enabled` → false), so plain `prevWorkers + 1` turned "had 2
        // workers, toggled off in settings, hit Enable on dashboard"
        // into 3 workers.
        newWorkers = Math.max(1, prevWorkers);
        newEnabled = true;
    } else {
        newWorkers = Math.max(0, prevWorkers + direction);
        newEnabled = newWorkers > 0;
    }
    if (newWorkers === prevWorkers && newEnabled === prevEnabled) return;
    entry.workers = newWorkers;
    entry.enabled = newEnabled;

    try {
        const saveResult = await apiPost('/api/settings', { gpu_config: gpuConfig });
        cachedGpuConfig = gpuConfig;
        cachedWorkerConfigCounts = null;
        await loadWorkerConfigCounts(true);
        renderDashboardGpuConfig();
        await Promise.all([loadJobs(), loadWorkerStatuses(), refreshStatus()]);

        if (saveResult.warning) {
            showToast('Warning', saveResult.warning, 'warning');
        } else {
            showToast('Workers Updated', `GPU workers for ${device} set to ${newWorkers}`, 'success');
        }
    } catch (error) {
        entry.workers = prevWorkers;
        entry.enabled = prevEnabled;
        renderDashboardGpuConfig();
        showToast('Error', `Failed to update GPU workers: ${error.message}`, 'danger');
    }
}

async function updateLibraryList() {
    const listEl = document.getElementById('libraryList');
    if (!listEl) return;

    // Phase H6: replaced the flat per-library list with a compact summary.
    // The full per-server library detail lives on /servers; the new New Job
    // modal handles per-server picking. This card just orients the user.
    if (!libraries || libraries.length === 0) {
        listEl.innerHTML =
            '<div class="text-muted small">' +
            'No libraries enabled. <a href="/servers" class="text-decoration-none">Add or enable libraries on the Servers page</a>.' +
            '</div>';
        return;
    }

    const distinctServers = new Set();
    for (const l of libraries) {
        if (l && l.server_id) distinctServers.add(l.server_id);
    }
    const serverCount = distinctServers.size || 1;
    const libCount = libraries.length;

    listEl.innerHTML =
        `<div class="d-flex justify-content-between align-items-center">` +
        `<span class="small"><strong>${libCount}</strong> librar${libCount === 1 ? 'y' : 'ies'} ` +
        `across <strong>${serverCount}</strong> server${serverCount === 1 ? '' : 's'}</span>` +
        `<a href="/servers" class="small text-decoration-none">Manage <i class="bi bi-arrow-right-short"></i></a>` +
        `</div>` +
        `<div class="form-text mt-1">Use <strong>Start New Job</strong> above to scan a specific server / libraries.</div>`;
}

// Refresh the Schedules library checkbox group when the user picks a different
// server in the modal. Hits /api/libraries?server_id=<id> when a specific
// server is chosen so non-Plex servers (Emby/Jellyfin) are also covered.
async function onScheduleServerChange() {
    const sel = document.getElementById('scheduleServer');
    if (!sel) return;
    const serverId = sel.value;
    MediaScanFilters.setLoading('schedule', true);
    const listEl = document.getElementById('scheduleLibraryList');
    if (listEl) listEl.innerHTML = '<div class="text-muted small">Loading libraries…</div>';
    try {
        const url = serverId
            ? `/api/libraries?server_id=${encodeURIComponent(serverId)}`
            : '/api/libraries';
        const data = await apiGet(url);
        if (sel.value !== serverId) return;
        libraries = data.libraries || [];
        _renderScheduleLibraryList(libraries, serverId || null);
    } catch (e) {
        if (sel.value !== serverId) return;
        console.warn('Failed to refresh libraries for server change:', e);
        if (listEl) listEl.innerHTML = '<div class="text-warning small">Could not load libraries.</div>';
        showToast('Schedules', 'Could not load libraries for the selected server', 'warning');
    } finally {
        if (sel.value === serverId) MediaScanFilters.setLoading('schedule', false);
    }
}

// Phase H7: render the Schedules modal library checkbox group. Same group-by-
// server pattern as the New Job modal (H6). When pinned to one server, render
// flat. Each checkbox is disabled while "All Libraries" is checked.
function _renderScheduleLibraryList(libs, filterServerId) {
    const listEl = document.getElementById('scheduleLibraryList');
    if (!listEl) return;
    if (!libs || libs.length === 0) {
        listEl.innerHTML = '<div class="text-muted small">No libraries available for this selection.</div>';
        MediaScanFilters.refresh('schedule');
        return;
    }
    const allDisabled = document.getElementById('scheduleLibraryAll').checked;
    const renderRow = (lib, indent) => `
        <div class="form-check ${indent ? 'ms-2' : ''}">
            <input class="form-check-input schedule-library-checkbox" type="checkbox"
                   value="${lib.id}" id="schedLib_${lib.id}" ${allDisabled ? 'disabled' : ''}
                   data-library-kind="${escapeHtml(lib.type || lib.kind || '')}"
                   onchange="MediaScanFilters.refresh('schedule')">
            <label class="form-check-label" for="schedLib_${lib.id}">
                ${escapeHtml(lib.name)} <span class="text-muted small">(${libraryTypeLabel(lib)})</span>
            </label>
        </div>
    `;
    if (filterServerId) {
        listEl.innerHTML = libs.map(l => renderRow(l, false)).join('');
        MediaScanFilters.refresh('schedule');
        return;
    }
    const groups = new Map();
    for (const lib of libs) {
        const key = lib.server_id || '__legacy__';
        if (!groups.has(key)) {
            groups.set(key, { server_name: lib.server_name || '', server_type: lib.server_type || '', libs: [] });
        }
        groups.get(key).libs.push(lib);
    }
    const sections = [];
    for (const [_, grp] of groups) {
        const stype = (grp.server_type || '').toLowerCase();
        const logo = _vendorLogo(stype, 14) || '';
        const head = `<div class="text-muted small mt-2 mb-1">${logo}<strong>${escapeHtml(grp.server_name || stype.toUpperCase() || 'Server')}</strong></div>`;
        sections.push(head + grp.libs.map(l => renderRow(l, true)).join(''));
    }
    listEl.innerHTML = sections.join('');
    MediaScanFilters.refresh('schedule');
}

function onScheduleLibraryAllChange(checkbox) {
    document.querySelectorAll('.schedule-library-checkbox').forEach(cb => {
        cb.disabled = checkbox.checked;
        if (checkbox.checked) cb.checked = false;
    });
    MediaScanFilters.refresh('schedule');
}

function setScheduleLibrariesChecked(checked) {
    const allCb = document.getElementById('scheduleLibraryAll');
    if (allCb && allCb.checked) {
        allCb.checked = false;
        onScheduleLibraryAllChange(allCb);
    }
    document.querySelectorAll('.schedule-library-checkbox').forEach(cb => {
        cb.disabled = false;
        cb.checked = checked;
    });
    MediaScanFilters.refresh('schedule');
}

// Populate the Schedules modal's "Media Server" dropdown from /api/servers.
async function _populateScheduleServerPicker(currentServerId) {
    const sel = document.getElementById('scheduleServer');
    if (!sel) return;
    try {
        const data = await apiGet('/api/servers');
        const servers = (data.servers || []).filter(s => s.enabled !== false);
        sel.innerHTML = '<option value="">All servers</option>';
        for (const s of servers) {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = `${s.name} (${(s.type || '').toUpperCase()})`;
            if (currentServerId && currentServerId === s.id) opt.selected = true;
            sel.appendChild(opt);
        }
    } catch (e) {
        console.warn('Could not load servers for schedule picker:', e);
    }
}

function toggleJobFiles(jobId) {
    const detailRow = document.getElementById('job-detail-' + jobId);
    const btn = document.getElementById('job-files-toggle-' + jobId);
    if (!detailRow || !btn) return;
    const isExpanded = !detailRow.classList.contains('d-none');
    detailRow.classList.toggle('d-none');
    detailRow.setAttribute('aria-hidden', isExpanded ? 'true' : 'false');
    if (isExpanded) {
        expandedJobFileRows.delete(jobId);
    } else {
        expandedJobFileRows.add(jobId);
        const job = jobs.find(function (j) { return String(j.id) === String(jobId); });
        if (job) _loadJobFileList(job);
    }
    const icon = btn.querySelector('i');
    if (icon) {
        icon.classList.toggle('bi-chevron-down', isExpanded);
        icon.classList.toggle('bi-chevron-up', !isExpanded);
    }
    btn.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
}

function toggleActiveJobFiles(jobId) {
    const filesDiv = document.getElementById('active-job-files-' + jobId);
    const btn = filesDiv ? filesDiv.previousElementSibling : null;
    if (!filesDiv) return;
    const isExpanded = !filesDiv.classList.contains('d-none');
    filesDiv.classList.toggle('d-none');
    if (isExpanded) {
        expandedActiveJobFiles.delete(jobId);
    } else {
        expandedActiveJobFiles.add(jobId);
    }
    if (btn) {
        const icon = btn.querySelector('i');
        if (icon) {
            icon.classList.toggle('bi-chevron-down', isExpanded);
            icon.classList.toggle('bi-chevron-up', !isExpanded);
        }
    }
}

const PRIORITY_LABELS = {1: 'High', 2: 'Normal', 3: 'Low'};
const PRIORITY_BADGE_CLASS = {1: 'bg-danger', 2: 'bg-primary', 3: 'bg-secondary'};
const PRIORITY_DOT_CLASS = {1: 'priority-dot-high', 2: 'priority-dot-normal', 3: 'priority-dot-low'};

function renderPriorityCell(job) {
    const pri = job.priority || 2;
    const label = PRIORITY_LABELS[pri] || 'Normal';
    const badgeClass = PRIORITY_BADGE_CLASS[pri] || 'bg-primary';
    const isActive = job.status === 'running' || job.status === 'pending';
    if (!isActive) {
        return `<span class="priority-readonly priority-${pri}">${label}</span>`;
    }
    const items = [1, 2, 3].map(function (p) {
        const active = p === pri ? ' active' : '';
        const dot = `<span class="priority-dot ${PRIORITY_DOT_CLASS[p]}"></span>`;
        return `<li><a class="dropdown-item${active}" href="#" onclick="setJobPriority('${escapeHtml(job.id)}', ${p}); return false;">${dot}${PRIORITY_LABELS[p]}</a></li>`;
    }).join('');
    return `<div class="dropdown d-inline-block">
        <button class="btn dropdown-toggle priority-btn priority-${pri}" type="button" data-bs-toggle="dropdown" aria-expanded="false" style="cursor:pointer;">${label}</button>
        <ul class="dropdown-menu">${items}</ul>
    </div>`;
}

async function setJobPriority(jobId, priority) {
    // Update in-memory array so deferred table rebuilds stay consistent
    const jobObj = jobs.find(function (j) { return j.id === jobId; });
    const oldPriority = jobObj ? jobObj.priority : null;
    if (jobObj) jobObj.priority = priority;

    // Optimistic DOM update for immediate visual feedback
    const row = document.getElementById('job-row-' + jobId);
    if (row) {
        const btn = row.querySelector('.priority-btn');
        if (btn) {
            btn.classList.remove('priority-1', 'priority-2', 'priority-3');
            btn.classList.add('priority-' + priority);
            btn.textContent = PRIORITY_LABELS[priority] || 'Normal';
        }
    }

    try {
        await apiPost('/api/jobs/' + jobId + '/priority', {priority: priority});
    } catch (err) {
        if (jobObj && oldPriority !== null) jobObj.priority = oldPriority;
        loadJobs();
        showToast('Error', 'Failed to update priority', 'danger');
    }
}

// Render a small per-server badge for jobs/schedules tables. Returns an
// empty string when the row has no server attribution (back-compat with
// jobs created before the multi-server transition).
function _serverBadge(item) {
    const stype = (item && (item.server_type || (item.server && item.server.type) || '')).toLowerCase();
    const sname = item && (item.server_name || (item.server && item.server.name) || '');
    if (stype || sname) {
        const palette = { plex: 'bg-warning text-dark', emby: 'bg-success', jellyfin: 'bg-info text-dark' };
        const cls = palette[stype] || 'bg-secondary';
        const label = sname || stype.toUpperCase() || 'Server';
        const tooltip = stype ? `${stype.toUpperCase()}` : '';
        // Prepend a tiny vendor logo when we have a known type (12px, fits a badge);
        // colour palette stays as the colour-blind-friendly fallback.
        const logoTag = _vendorLogo(stype, 12);
        const logo = logoTag ? logoTag.replace('margin-right: 4px;', 'margin-right: 3px; vertical-align: -2px;') : '';
        return ` <span class="badge ${cls} ms-1" title="${escapeHtmlAttr(tooltip)}">${logo}${escapeHtmlText(label)}</span>`;
    }
    // Fallback for non-server-pinned jobs: surface the trigger source (Sonarr,
    // Radarr, manual scan, schedule etc.) so the user can tell "what server it
    // ran on" / "where the work came from" — D2. Without this, every webhook
    // job and every "All Servers" scan was an unlabelled row.
    const cfg = (item && item.config) || {};
    const src = String(cfg.source || '').trim().toLowerCase();
    const triggerPalette = {
        radarr:      { cls: 'bg-warning text-dark', label: 'Radarr' },
        sonarr:      { cls: 'bg-info text-dark',    label: 'Sonarr' },
        sportarr:    { cls: 'bg-info text-dark',    label: 'Sportarr' },
        tdarr:       { cls: 'bg-secondary',         label: 'Tdarr' },
        plex:        { cls: 'bg-warning text-dark', label: 'Plex Direct' },
        emby:        { cls: 'bg-success',           label: 'Emby Webhook' },
        jellyfin:    { cls: 'bg-info text-dark',    label: 'Jellyfin Webhook' },
        custom:      { cls: 'bg-secondary',         label: 'Custom Webhook' },
        scheduled:   { cls: 'bg-secondary',         label: 'Scheduled' },
        schedule:    { cls: 'bg-secondary',         label: 'Scheduled' },
        recently_added: { cls: 'bg-secondary',      label: 'Recently Added' },
        scheduled_recently_added: { cls: 'bg-secondary', label: 'Scheduled scan' },
        theintrodb_recheck: { cls: 'bg-secondary',  label: 'TheIntroDB recheck' },
        version_rerun: { cls: 'bg-secondary',       label: 'App update' },
    };
    if (src && triggerPalette[src]) {
        const t = triggerPalette[src];
        return ` <span class="badge ${t.cls} ms-1" title="Triggered by ${escapeHtmlAttr(t.label)}">${escapeHtmlText(t.label)}</span>`;
    }
    if (src) {
        // Unknown source — render as-is so the user still sees something
        return ` <span class="badge bg-secondary ms-1" title="Trigger: ${escapeHtmlAttr(src)}">${escapeHtmlText(src)}</span>`;
    }
    return '';
}

// D14 — single source of truth for status chip label + color, shared
// across every UI surface that renders an outcome:
//   * file-outcome cell badges per row       (job_modal.js → renderFileResultsTable)
//   * per-server aggregate badges            (this file → _renderPublishersBlock)
//   * per-server pills inside file rows      (job_modal.js → _renderFileServerPills)
//
// Both ProcessingResult AND PublisherStatus / MultiServerStatus enums
// resolve through this map so that semantically-equivalent statuses
// (e.g. file `skipped_bif_exists` ≡ publisher `skipped_output_exists`)
// always render the same label and color. Equivalences below.
const STATUS_META = {
    // Success — file generated this run OR successfully published to a server.
    generated:              { label: 'Generated',     cls: 'bg-success', tip: 'Preview was generated' },
    published:              { label: 'Generated',     cls: 'bg-success', tip: 'Preview was published to this server' },
    published_pending_chapters: { label: 'Chapters incomplete', cls: 'bg-warning text-dark', tip: 'Scrubber previews are ready. Chapter thumbnails are incomplete; open Files for each file’s status and reason.' },
    published_chapters_failed: { label: 'Chapters failed', cls: 'bg-danger', tip: 'Scrubber previews are ready, but chapter thumbnails failed. Open Files for the reason.' },
    // Tiles / sidecar are on disk, but the server hadn't indexed the file at publish
    // time so the per-item registration call (Jellyfin Media Preview Bridge plugin or
    // /Items/{id}/Refresh) was skipped. The retry queue picks this back up — once the
    // server indexes the file, the registration fires and the row promotes to "Generated".
    published_pending_registration: { label: 'Generated (auto-retrying)', cls: 'bg-success', tip: 'Tiles are on disk; the server has not indexed the file yet so trickplay registration is pending. The row shows a "Retry N/M" chip while retries back off (1m → 2m → 5m with the default retry settings) until the server catches up or the retries run out; after that the next scheduled scan picks the file up.' },

    // Output already on disk; source unchanged — nothing to redo.
    skipped_bif_exists:     { label: 'Already Existed', cls: 'bg-info text-dark', tip: 'Output already on disk and source unchanged' },
    skipped_output_exists:  { label: 'Already Existed', cls: 'bg-info text-dark', tip: 'Output already on disk on this server and source unchanged' },
    skipped:                { label: 'Already Existed', cls: 'bg-info text-dark', tip: 'Output already on disk' },

    // Media server knows the file exists but hasn't completed its analysis
    // pass yet (the bundle hash we need to write the BIF doesn't exist yet).
    // Retry queue will try again on slow backoff.
    skipped_not_indexed:    { label: 'Not Scanned Yet', cls: 'bg-warning text-dark', tip: 'Media server hasn\'t finished scanning / analysing this file yet — will retry' },
    not_indexed:            { label: 'Not Scanned Yet', cls: 'bg-warning text-dark', tip: 'Media server hasn\'t finished scanning / analysing this file yet — will retry' },
    // Media server doesn't know about this file at all (path isn't in any
    // of its libraries, OR it just hasn't been picked up by a scan yet).
    // We've nudged a scan and the retry queue will try again.
    skipped_not_in_library: { label: 'Not In Library', cls: 'bg-warning text-dark', tip: 'This server doesn\'t know about this file — nudged a scan and will retry. If it never appears, the file is outside every library root configured on this server.' },

    // Hard failures.
    failed:                 { label: 'Failed',        cls: 'bg-danger', tip: 'Processing failed' },
    no_frames:              { label: 'No Frames',     cls: 'bg-danger', tip: 'FFmpeg produced no frames (file may be unreadable)' },

    // No server owns this file path.
    no_media_parts:         { label: 'No Server Owner', cls: 'bg-light text-dark border', tip: 'No configured server claims this file path' },
    no_owners:              { label: 'No Server Owner', cls: 'bg-light text-dark border', tip: 'No configured server claims this file path' },

    // Legacy / pipeline-specific outcomes.
    skipped_file_not_found: { label: 'Not Found',     cls: 'bg-warning text-dark', tip: 'File not found on disk' },
    skipped_source_gone:    { label: 'Gone from disk', cls: 'bg-secondary', tip: 'Replaced by a newer file before this job reached it; the newer file is run on its own.' },
    skipped_excluded:       { label: 'Excluded',      cls: 'bg-secondary', tip: 'Path matched an exclusion rule' },
    skipped_invalid_hash:   { label: 'Invalid Hash',  cls: 'bg-warning text-dark', tip: 'Could not compute the path hash' },
    unresolved_plex:        { label: 'Not In Plex',   cls: 'bg-danger', tip: 'Could not find this item in Plex after lookup' },

    // Intro & Credits — file outcomes (markers.outcomes.FileOutcome) and per-server row statuses (ServerStatus).
    // markers_up_to_date / _none / _skipped / _waiting are both, with one label each.
    markers_published:      { label: 'Markers written', cls: 'bg-success', tip: 'The job changed what at least one server shows; the reason names any other marker that wasn\'t found' },
    markers_written:        { label: 'Markers written', cls: 'bg-success', tip: 'Markers were written to this server' },
    markers_up_to_date:     { label: 'Up to date', cls: 'bg-secondary', tip: 'The server already shows these markers' },
    markers_none:           { label: 'No markers found', cls: 'bg-secondary', tip: 'No source found an intro or credits for this file' },
    markers_no_owners:      { label: 'No server with Intro & Credits on', cls: 'bg-secondary', tip: 'No server with Intro & Credits turned on has this file' },
    markers_skipped:        { label: 'Skipped', cls: 'bg-secondary', tip: 'The server can\'t take markers right now (for example, a plugin is missing), or the file is a trailer or other extra' },
    markers_waiting:        { label: 'Waiting', cls: 'bg-info text-dark', tip: 'The server hasn\'t added the file yet (the job tries it again later)' },


    // Plex loudness: file outcomes and per-server row statuses (loudness/job.py).
    loudness_written:        { label: 'Loudness written', cls: 'bg-success', tip: 'Plex\'s loudness analysis was stored for at least one audio track, or its item marked analysed' },
    loudness_up_to_date:     { label: 'Up to date', cls: 'bg-secondary', tip: 'Plex already has loudness for every audio track, or the file has none' },
    loudness_no_owners:      { label: 'No server with Loudness on', cls: 'bg-secondary', tip: 'No Plex server with Loudness turned on has this file' },
    loudness_not_in_library: { label: 'Not in Plex yet', cls: 'bg-info text-dark', tip: 'Plex had not added this file when it was checked. The job status shows whether another attempt is scheduled.' },
    loudness_waiting:        { label: 'Waiting for Plex', cls: 'bg-info text-dark', tip: 'Plex or the source file was temporarily unavailable or changed. The job status shows whether another attempt is scheduled.' },
};

const JOB_KIND_INTRO_CREDITS = 'intro_credits';
const JOB_KIND_LOUDNESS = 'loudness';
// Match web.jobs.is_user_visible_job: retry attempts belong to their visible chain's lifecycle.
function _isUserVisibleJob(job) {
    const cfg = job && job.config || {};
    return !cfg.is_retry_attempt && !(cfg.is_retry && !cfg.is_retry_chain);
}
// Waiting rows whose server hasn't indexed the file yet (markers.outcomes.NOT_IN_LIBRARY); a "Retry: …" job follows.
const MARKERS_NOT_IN_LIBRARY = 'not_in_library';
const MARKERS_NOT_IN_LIBRARY_LABEL = 'Not in the server\'s library yet — will retry';
// Server messages that only repeat the pill or the file's reason (markers/pipeline.py); the Files panel lists the rest.
// "Sources don't agree yet" is what rows recorded before 2026-09-24 said when nothing was written.
const MARKERS_ROUTINE_MESSAGE = /^(Up to date|No markers found|Sources don't agree yet|\d+ marker\(s\))$/;

function _isMarkersJob(job) {
    return !!job && job.kind === JOB_KIND_INTRO_CREDITS;
}
window._isMarkersJob = _isMarkersJob;

// Kinds with their own runner (job_kinds.SELF_PAUSED_KINDS): they pause on their own and list their files.
function _hasOwnRunner(job) {
    return !!job && (job.kind === JOB_KIND_INTRO_CREDITS || job.kind === JOB_KIND_LOUDNESS);
}
window._hasOwnRunner = _hasOwnRunner;

// job_kinds.parse_job_kind defaults a missing/unknown kind to previews server-side, so this mirrors that default.
const JOB_KIND_PREVIEWS = 'previews';
const JOB_KIND_LABELS = {
    [JOB_KIND_PREVIEWS]: 'Previews',
    [JOB_KIND_INTRO_CREDITS]: 'Intro & Credits',
    [JOB_KIND_LOUDNESS]: 'Plex loudness',
};

// The one place every job's kind tag is built — the queue table row, the "waiting to retry" active-job
// card and the modal header (job_modal.js) all call this so the label and markup can't drift between them.
function _jobKindBadgeHtml(job) {
    const label = JOB_KIND_LABELS[job && job.kind] || JOB_KIND_LABELS[JOB_KIND_PREVIEWS];
    const icon = ({previews: 'bi-film', intro_credits: 'bi-skip-forward', loudness: 'bi-soundwave'})[job?.kind] || 'bi-film';
    return `<span class="badge text-bg-dark me-1 job-kind-badge"><i class="bi ${icon} me-1" aria-hidden="true"></i>${escapeHtml(label)}</span>`;
}
window._jobKindBadgeHtml = _jobKindBadgeHtml;

function _queueMetadataHtml(job, title) {
    const primaryId = String(job.server_id || '');
    const seen = new Set(primaryId ? [primaryId] : []);
    const associated = (job.library_scope || []).filter(pair => {
        const id = String(pair.server_id || '');
        if (!id || seen.has(id)) return false;
        seen.add(id); return true;
    });
    const hasOrigin = !!(job.server_name || job.server_type || job.server);
    const primary = hasOrigin ? `<span title="Originating server">${_serverBadge(job)}</span>` : '';
    const ownerBadge = pair => _serverBadge({server_name: pair.server_name || pair.server_id, server_type: pair.server_type});
    let owners = '';
    if (associated.length === 1) owners = `<span class="queue-additional-owner" title="Associated library server">${hasOrigin ? '+ ' : ''}${ownerBadge(associated[0])}</span>`;
    else if (associated.length > 1) owners = `<details class="queue-owner-details"><summary aria-label="Show associated library servers">${hasOrigin ? '+' : ''}${associated.length} servers</summary><div class="queue-owner-list">${associated.map(pair => `<div>${ownerBadge(pair)}</div>`).join('')}</div></details>`;
    const servers = primary || owners ? `<span class="queue-server-group">${primary}${owners}</span>` : '';
    const source = job.config?.source ? `<span class="queue-trigger-context">${_serverBadge({config: job.config})}</span>` : '';
    return _jobKindBadgeHtml(job) + _libraryTagHtml(job, title) + servers + source;
}

// 'own' = paused on its own (it has handed its job slot back), 'all' = held by Pause all (keeps its slot), '' = neither.
// Pause all doesn't set an Intro & Credits job's own flag, so the global flag is read here; a job paused before a
// restart comes back running + paused without a slot, which reads as 'own' too.
function _markersPauseState(job) {
    if (!['pending', 'running'].includes(job.status)) return '';
    if (job.paused) return 'own';
    return processingPaused ? 'all' : '';
}

// Queue titles from the backend start with "Intro & Credits: " or "Intro & Credits · " (after "Retry: " or "Verify: ");
// the kind badge says it already.
function _markersDisplayName(name) {
    const match = /^((?:Retry|Verify): )?Intro & Credits(?::| ·) (.+)$/.exec(name || '');
    return match ? (match[1] || '') + match[2] : (name || '');
}

// The libraries a job covers (``library_names`` from GET /api/jobs), left of its title; none when the title already
// names the one library.
function _libraryTagHtml(job, title) {
    const names = Array.isArray(job.library_names) ? job.library_names.filter(Boolean) : [];
    if (!names.length || (names.length === 1 && names[0] === title)) return '';
    const label = names.length <= 2 ? names.join(' · ') : `${names.length} libraries`;
    const tip = (names.length === 1 ? 'Library: ' : 'Libraries: ') + names.join(', ');
    return `<span class="badge border text-body-secondary fw-normal job-library-tag" title="${escapeHtmlAttr(tip)}">`
        + `<i class="bi bi-collection me-1" aria-hidden="true"></i>${escapeHtml(label)}</span>`;
}

// The re-check after an update (triggers.submit_version_reruns) says what it is; its counts are on its config.
function _versionRerunInfoHtml(job) {
    const cfg = (job && job.config) || {};
    if (!_isMarkersJob(job) || cfg.source !== 'version_rerun') return '';
    const size = Number((cfg.version_rerun_counts || {}).batch_size) || 0;
    const tip = 'After an update, files whose intro or credits were found by an older version are checked again, '
        + (size > 0 ? `${size} at a time` : 'a batch at a time') + ', at low priority.';
    return `<button type="button" class="info-icon align-baseline job-rerun-info" tabindex="0" data-bs-toggle="tooltip" `
        + `data-bs-placement="top" title="${escapeHtmlAttr(tip)}" aria-label="${escapeHtmlAttr(tip)}">`
        + '<i class="bi bi-info-circle"></i></button>';
}

// An Intro & Credits row's files (GET /api/jobs/<id>/file-list), read when the row opens. A finished job's list is
// kept; a running one's is read again at most every JOB_FILE_LIST_REFRESH_MS, since the queue re-renders every few
// seconds.
const _jobFileLists = new Map();
const JOB_FILE_LIST_REFRESH_MS = 15000;

function _markersFilesBlock(job) {
    return `<div id="job-file-list-${escapeHtml(job.id)}" class="job-file-list mb-2">${_markersFilesBody(job)}</div>`;
}

function _markersFilesBody(job) {
    const cached = _jobFileLists.get(String(job.id));
    const cfg = job.config || {};
    const paths = Array.isArray(cfg.webhook_paths) && cfg.webhook_paths.length ? cfg.webhook_paths : Array.isArray(cfg.file_paths) ? cfg.file_paths : [];
    const names = Array.isArray(cfg.webhook_basenames) ? cfg.webhook_basenames : [];
    if (paths.length || names.length) {
        // Match API-resolved media titles by actual path, preserving input order and duplicates.
        const titles = new Map((cached?.data?.files || []).map(file => [file.path, file.title]));
        const total = paths.length || names.length;
        const requested = paths.length ? paths.slice(0, 5).map((path, index) => ({path, name: names[index] || '', title: titles.get(path) || ''})) : names.slice(0, 5).map(name => ({name}));
        return `<h3 class="job-detail-heading">Requested paths <span class="text-body-secondary">${total.toLocaleString()}</span></h3>`
            + _queueFileRows(requested)
            + (total > 5 ? `<p class="small text-body-secondary mb-1">Showing 5 of ${total.toLocaleString()} requested paths.</p>` : '')
            + `<button type="button" class="btn btn-sm btn-outline-secondary queue-files-all" onclick="openJobDetails('${escapeHtml(job.id)}', 'files', 'requested')">View all requested paths<i class="bi bi-arrow-right ms-2" aria-hidden="true"></i></button>`;
    }
    if (!cached || !cached.data) return '<strong>Files:</strong> <span class="text-muted">Loading…</span>';
    if (cached.data.error) return '<strong>Files:</strong> <span class="text-muted">Could not read files. Open logs and files below, or try again.</span>';
    return _markersFilesHtml(cached.data, job);
}

function _queueFileRows(files) {
    return files.slice(0, 5).map(function (file, index) {
        const path = String(file.path || '');
        const basename = String(file.name || path.split(/[\\/]/).pop() || file.title || 'File');
        const title = String(file.title || '');
        const inspector = /\.(mkv|mp4|avi|m4v|ts|wmv|mov|flv|webm)$/i.test(path) && (/^\//.test(path) || /^[a-z]:[\\/]/i.test(path) || /^\\\\/.test(path))
            ? `<a class="btn btn-sm queue-inspector" href="/inspector?path=${escapeHtmlAttr(encodeURIComponent(path))}" target="_blank" rel="noopener" title="Open in Inspector" aria-label="Inspect ${escapeHtmlAttr(basename)}"><i class="bi bi-eye" aria-hidden="true"></i></a>` : '';
        return `<div class="queue-file-row"><div class="queue-file-text">${path
            ? `<details data-file-index="${index}"><summary>${escapeHtml(basename)}</summary><div class="queue-full-path">${escapeHtml(path)}</div></details>`
            : `<span>${escapeHtml(basename)}</span>`}${title && title !== basename ? `<span class="queue-file-title text-body-secondary">${escapeHtml(title)}</span>` : ''}</div>${inspector}</div>`;
    }).join('');
}

function _markersFilesHtml(data, job) {
    const files = Array.isArray(data.files) ? data.files : [];
    const total = Math.max(Number(data.total) || 0, files.length);
    if (!files.length) {
        return `<p class="text-body-secondary small mb-0">${job.status === 'pending' ? 'Files will be listed when the job runs.' : 'No files listed yet.'}</p>`;
    }
    return `<h3 class="job-detail-heading">Files <span class="text-body-secondary">${total.toLocaleString()}</span></h3>`
        + _queueFileRows(files)
        + `<button type="button" class="btn btn-sm btn-outline-secondary queue-files-all" onclick="openJobDetails('${escapeHtml(job.id)}', 'files')">View all files and results<i class="bi bi-arrow-right ms-2" aria-hidden="true"></i></button>`;
}

function _loadJobFileList(job) {
    const id = String(job.id);
    const cached = _jobFileLists.get(id);
    if (cached && cached.loading) return;
    if (cached && cached.status === job.status) {
        // A running job's list grows, and a failed read is tried again: both after the refresh interval.
        const changes = ['pending', 'running'].includes(job.status) || !!(cached.data && cached.data.error);
        if (!changes || Date.now() - cached.at < JOB_FILE_LIST_REFRESH_MS) return;
    }
    _jobFileLists.set(id, Object.assign({}, cached, { loading: true }));
    apiGet(`/api/jobs/${encodeURIComponent(id)}/file-list?limit=5`).then(function (data) {
        _jobFileLists.set(id, { data: data, status: job.status, at: Date.now() });
    }).catch(function (err) {
        // Kept until the refresh interval, so a failing read isn't asked again on every re-render.
        const failed = { files: [], total: 0, error: String((err && err.message) || err) };
        _jobFileLists.set(id, { data: (cached && cached.data) || failed, status: job.status, at: Date.now() });
    }).finally(function () {
        const box = document.getElementById('job-file-list-' + id);
        if (box) {
            const body = _markersFilesBody(job);
            if (box._body !== body) {
                const open = Array.from(box.querySelectorAll('details[open]')).map(el => el.dataset.fileIndex);
                box.innerHTML = body;
                box._body = body;
                open.forEach(index => box.querySelector(`details[data-file-index="${CSS.escape(index)}"]`)?.setAttribute('open', ''));
            }
        }
    });
}

// A webhook's Intro & Credits job is listed directly under the preview job it follows when that job is on this page.
function _orderFollowUps(list) {
    const ids = new Set(list.map(function (j) { return String(j.id); }));
    const leaderOf = function (job) {
        const lead = _hasOwnRunner(job) && job.config ? String(job.config.follows_job_id || '') : '';
        return lead && lead !== String(job.id) && ids.has(lead) ? lead : '';
    };
    const followers = new Map();
    for (const job of list) {
        const lead = leaderOf(job);
        if (lead) {
            if (!followers.has(lead)) followers.set(lead, []);
            followers.get(lead).push(job);
        }
    }
    const ordered = [];
    const placed = new Set();
    const emit = function (job) {
        const id = String(job.id);
        if (placed.has(id)) return;
        placed.add(id);
        ordered.push(job);
        (followers.get(id) || []).forEach(emit);
    };
    list.forEach(function (job) { if (!leaderOf(job)) emit(job); });
    // Followers whose leaders follow each other (never expected) still render.
    list.forEach(emit);
    return ordered;
}
window.STATUS_META = STATUS_META;

function _statusMeta(key) {
    return STATUS_META[key] || { label: key || '?', cls: 'bg-secondary', tip: '' };
}
window._statusMeta = _statusMeta;

// Back-compat alias retained while older call sites still reference the
// old name. New code should call _statusMeta() directly.
const _PUBLISHER_STATUS_BADGES = STATUS_META;

// Frame-provenance badges so users can see when one webhook's frames
// were reused across a sibling-server publish (no second FFmpeg) vs
// when this publisher's output was already on disk vs when FFmpeg
// just ran for this dispatch.
const _FRAME_SOURCE_BADGES = {
    cache_hit:      { label: 'Frames reused', cls: 'bg-info text-dark', tip: 'Frames came from the cache — FFmpeg did not run for this dispatch' },
    output_existed: { label: 'Already on disk', cls: 'bg-light text-dark border', tip: 'Output was already on disk and unchanged; nothing to re-publish' },
    extracted:      null, // no badge for "extracted" — it's the boring default
};

// File-level issue footnote, rendered LIVE during a run (it rides the
// job_progress event via progress.outcome, which the dispatcher pushes on the
// progress cadence). Shows only outcomes that aren't attributable to a single
// server — file missing on disk, no media parts, etc. The Generated / Reused /
// Already-Existed breakdown lives per-server in _renderPublishersBlock. Empty
// string when there's nothing to show yet.
function _renderJobFileIssues(outcome) {
    // File-level issues that aren't attributable to a single server — they
    // happen *before* per-server publishing (file missing on disk, no media
    // parts, excluded, etc.). Rendered as a small footnote under the
    // per-server breakdown. Generated / Reused / Already-Existed are
    // intentionally excluded here: they're shown per-server now.
    if (!outcome || typeof outcome !== 'object') return '';
    const keys = ['skipped_file_not_found', 'skipped_source_gone', 'no_media_parts', 'skipped_excluded',
                  'skipped_invalid_hash', 'skipped_not_indexed', 'markers_no_owners', 'failed'];
    return keys.map(function (k) {
        const n = outcome[k];
        if (!n || n <= 0) return '';
        const meta = _statusMeta(k);
        const tip = meta.tip ? ` title="${escapeHtmlAttr(meta.tip)}"` : '';
        const tone = meta.cls.includes('danger') ? 'job-result-error' : meta.cls.includes('warning') ? 'job-result-warning' : 'job-result-muted';
        return `<span class="job-result-item ${tone}"${tip}><span>${escapeHtmlText(meta.label)}</span> <strong>× ${n.toLocaleString()}</strong></span>`;
    }).filter(Boolean).join(' ');
}

// Intro & Credits "Decided by" counts (progress.marker_sources, markers/source_counts.py): one line per marker type,
// its source groups biggest first. A group names up to two sources ("TheIntroDB + IntroDB + 1 more"); past five
// groups the rest add up under "other", listed in its tooltip.
const MARKER_SOURCE_NAMES = {
    chapters: 'chapters',
    theintrodb: 'TheIntroDB',
    introdb: 'IntroDB',
    skipdb: 'SkipDB',
    season_audio: 'season audio',
    season_audio_previous: 'last season\'s audio',
    credits_text: 'credit text',
    server_markers: 'server markers',
    server_markers_imported: 'server markers',
    user: 'your edits',
    // A marker kept from the file this one replaced (markers/carry_over.py; job_log.CARRIED_OVER_LABEL).
    carried_over: 'the file it replaced',
};
const MARKER_SOURCE_TYPE_LABELS = { intro: 'Intro', credits: 'Credits', recap: 'Recap', preview: 'Preview' };
const MARKER_SOURCE_GROUPS_SHOWN = 5;
const MARKER_SOURCES_TIP = 'How many files each source decided in this job.';
const MARKER_SOURCES_DETAIL = '<p>A file counts once per marker type, under the sources its marker came from; a marker a '
    + 'chapter set counts as chapters.</p><p>Markers already on your servers are named only when they were the second '
    + 'opinion one source needed.</p><p>Markers that need review, and files that failed or weren\'t checked, aren\'t '
    + 'counted.</p>';

function _markerSourceGroupLabel(group) {
    const names = String(group).split('+').map(function (id) { return MARKER_SOURCE_NAMES[id] || id; });
    return names.length > 2 ? `${names[0]} + ${names[1]} + ${names.length - 2} more` : names.join(' + ');
}

function _renderMarkerSources(sources) {
    if (!sources || typeof sources !== 'object') return '';
    const lines = Object.keys(MARKER_SOURCE_TYPE_LABELS).map(function (type) {
        const counts = new Map();
        Object.entries(sources[type] || {}).forEach(function ([group, n]) {
            if (!(n > 0)) return;
            const label = _markerSourceGroupLabel(group);
            counts.set(label, (counts.get(label) || 0) + n);
        });
        if (!counts.size) return '';
        const ranked = Array.from(counts.entries())
            .sort(function (a, b) { return b[1] - a[1] || a[0].localeCompare(b[0]); });
        const shown = ranked.slice(0, MARKER_SOURCE_GROUPS_SHOWN).map(function ([label, n]) {
            return `${escapeHtmlText(label)} <span class="fw-semibold">${n.toLocaleString()}</span>`;
        });
        const rest = ranked.slice(MARKER_SOURCE_GROUPS_SHOWN);
        if (rest.length) {
            const total = rest.reduce(function (sum, entry) { return sum + entry[1]; }, 0);
            const tip = rest.map(function ([label, n]) { return `${label} ${n}`; }).join(' · ');
            shown.push(`<span title="${escapeHtmlAttr(tip)}">other <span class="fw-semibold">${total.toLocaleString()}</span></span>`);
        }
        return `<div class="marker-sources-line"><span class="text-body">${MARKER_SOURCE_TYPE_LABELS[type]}:</span> `
            + `${shown.join(' · ')}</div>`;
    }).filter(Boolean);
    if (!lines.length) return '';
    return `<div class="mt-2 marker-sources"><strong class="me-1">Decided by</strong>`
        + `<button type="button" class="info-icon align-baseline" tabindex="0" data-bs-toggle="tooltip" `
        + `data-bs-placement="top" title="${escapeHtmlAttr(MARKER_SOURCES_TIP)}" aria-label="What these counts mean" `
        + `data-explain-title="Decided by" data-explain-html="${escapeHtmlAttr(MARKER_SOURCES_DETAIL)}">`
        + `<i class="bi bi-info-circle"></i></button>`
        + `<div class="small text-muted mt-1">${lines.join('')}</div></div>`;
}

// "N files ran on the CPU because the GPU failed" (progress.cpu_fallback_files, JobTracker.cpu_fallback_files):
// files a GPU worker reran on the CPU. Nothing when none did.
function _renderCpuFallbackLine(count) {
    const n = Number(count) || 0;
    if (n <= 0) return '';
    const files = n === 1 ? '1 file ran' : `${n.toLocaleString()} files ran`;
    return `<div class="mt-1 small text-warning-emphasis cpu-fallback-line"><i class="bi bi-arrow-down-circle me-1"></i>`
        + `${files} on the CPU because the GPU failed</div>`;
}

function _renderPublishersBlock(job) {
    // D12 — per-server aggregate (one row per registered server with
    // status counts), NOT per-file. Per-file × per-server attribution
    // lives in the Files panel; rendering it here on jobs with hundreds
    // of files would stack hundreds of rows in the Active Jobs and
    // History sections. Each entry: {server_id, server_name, server_type,
    // counts: {published: N, failed: M, ...}}.
    const rows = (job && Array.isArray(job.publishers)) ? job.publishers : [];
    // File-level issues (not found on disk, no media parts, …) aren't
    // attributable to a single server, but they belong WITH the per-server
    // breakdown — rendered as a note inside this same block, directly under
    // the server rows, rather than dangling at the bottom of the card.
    const outcome = (job && job.progress && job.progress.outcome) || (job && job.outcome) || null;
    const fileIssues = _renderJobFileIssues(outcome);
    const isMarkers = _isMarkersJob(job);
    const sourcesBlock = isMarkers ? _renderMarkerSources(job.progress && job.progress.marker_sources) : '';
    const cpuLine = _renderCpuFallbackLine(job && job.progress && job.progress.cpu_fallback_files);
    if (!rows.length && !fileIssues && !sourcesBlock && !cpuLine) return '';
    const lines = rows.map(function (entry) {
        const stype = (entry.server_type || '').toLowerCase();
        const logo = _vendorLogo(stype, 18) || '';
        const sname = entry.server_name || stype.toUpperCase() || 'Server';
        const counts = (entry && typeof entry.counts === 'object' && entry.counts) ? entry.counts : {};
        const fs = (entry && typeof entry.frame_sources === 'object' && entry.frame_sources) ? entry.frame_sources : null;
        const chapters = !isMarkers && entry.chapter_counts && typeof entry.chapter_counts === 'object'
            ? entry.chapter_counts : null;
        const hasChapters = chapters && Object.values(chapters).some(function (n) { return Number.isInteger(n) && n > 0; });
        const legacyPreviewUpdates = !isMarkers && !chapters && fs && fs.output_existed > 0
            && Number.isInteger(counts.published) && counts.published > 0 ? counts.published : 0;
        const chapterStatuses = ['published_pending_chapters', 'published_chapters_failed'];
        const badgeSpecs = [];
        if (isMarkers) {
            // Intro & Credits: no frame provenance; per-server marker statuses in a fixed order.
            const order = ['markers_written', 'markers_up_to_date', 'markers_waiting',
                           'markers_skipped', 'markers_none', 'failed'];
            const present = Object.keys(counts).filter(function (k) { return counts[k] > 0; });
            order.concat(present.filter(function (k) { return order.indexOf(k) === -1; }))
                .forEach(function (status) {
                    if (!(counts[status] > 0)) return;
                    const m = _statusMeta(status);
                    badgeSpecs.push({label: m.label, cls: m.cls, count: counts[status], tip: m.tip});
                });
            // A server skipped for one shared reason (plugin missing…) says why on the same badge.
            const skipMessage = entry.messages && entry.messages.markers_skipped;
            if (present.length === 1 && present[0] === 'markers_skipped' && typeof skipMessage === 'string' && skipMessage) {
                badgeSpecs[0].suffix = skipMessage;
            }
        } else if (fs) {
            // Per-server breakdown by frame provenance: each server shows what
            // it actually did — freshly Generated (extracted), Reused (frames
            // cached from a sibling server, no second FFmpeg), or Already
            // Existed (output already on disk).
            [['extracted', 'Generated', 'bg-success', 'FFmpeg ran fresh for this server'],
             ['cache_hit', 'Reused', 'bg-info text-dark', 'Frames reused from another server — no second FFmpeg'],
             ['output_existed', 'Already Existed', 'bg-secondary', 'Output was already on disk and unchanged']]
                .forEach(function (s) {
                    if (fs[s[0]] > 0) badgeSpecs.push({
                        label: hasChapters || legacyPreviewUpdates ? 'Scrubber ' + s[1].toLowerCase() : s[1],
                        cls: s[2], count: fs[s[0]],
                        tip: (hasChapters || legacyPreviewUpdates) && s[0] === 'output_existed'
                            ? 'Scrubber images were already on disk and reused.' : s[3]
                    });
                });
            // Plus attention-worthy statuses NOT captured by frame provenance
            // (failures, pending registration, index issues).
            ['failed', 'published_pending_registration', 'published_pending_chapters', 'published_chapters_failed', 'skipped_not_indexed', 'not_indexed', 'skipped_not_in_library', 'no_owners', 'no_frames']
                .filter(function (k) { return !hasChapters || !chapterStatuses.includes(k); })
                .forEach(function (k) { if (counts[k] > 0) { const m = _statusMeta(k); badgeSpecs.push({label: m.label, cls: m.cls, count: counts[k], tip: m.tip}); } });
        } else {
            // Fallback for jobs recorded before per-server frame_sources existed.
            const statusOrder = ['published', 'published_pending_registration', 'published_pending_chapters', 'published_chapters_failed', 'skipped_output_exists', 'skipped_not_indexed', 'not_indexed', 'skipped_not_in_library', 'skipped', 'no_owners', 'no_frames', 'failed'];
            const seen = new Set();
            statusOrder.filter(function (k) { seen.add(k); return counts[k] > 0; })
                .concat(Object.keys(counts).filter(function (k) { return !seen.has(k) && counts[k] > 0; }))
                .filter(function (k) { return !hasChapters || !chapterStatuses.includes(k); })
                .forEach(function (status) { const m = _statusMeta(status); badgeSpecs.push({label: m.label, cls: m.cls, count: counts[status], tip: m.tip}); });
        }
        if (legacyPreviewUpdates) {
            badgeSpecs.push({
                label: 'Previews updated', cls: 'bg-success', count: legacyPreviewUpdates,
                tip: 'This run completed publication for these files. Older results do not record which preview output changed.'
            });
        }
        if (hasChapters) {
            // Scrubber provenance cannot say whether this run created chapter images or repaired their references.
            [
                ['updated', 'Chapters updated', 'bg-success', 'Chapter images were generated or their Plex references were repaired for these files.'],
                ['already_existed', 'Chapters already existed', 'bg-secondary', 'Chapter images and their Plex references were already current for these files.'],
                ['ready', 'Chapters ready', 'bg-success', 'Chapter images are ready; this older result does not record whether this run changed them.'],
                ['none', 'No chapters', 'bg-secondary', 'These files have no chapters to generate images for.'],
                ['waiting', 'Chapters waiting for Plex', 'bg-warning text-dark', 'Plex has not indexed the source or analyzed its chapters yet. Open Files for the reason.'],
                ['incomplete', 'Chapters incomplete', 'bg-warning text-dark', 'Older results do not distinguish waiting from failure. Open Files for each saved reason.'],
                ['failed', 'Chapters failed', 'bg-danger', 'Chapter extraction or registration failed. Open Files for the reason.']
            ].forEach(function (s) {
                if (Number.isInteger(chapters[s[0]]) && chapters[s[0]] > 0) {
                    badgeSpecs.push({label: s[1], cls: s[2], count: chapters[s[0]], tip: s[3]});
                }
            });
        }
        if (!badgeSpecs.length) return '';
        const badges = badgeSpecs.map(function (b) {
            const tip = b.tip ? ` title="${escapeHtmlAttr(b.tip)}"` : '';
            const suffix = b.suffix ? ` · ${escapeHtmlText(b.suffix)}` : '';
            const tone = b.cls.includes('danger') ? 'job-result-error' : b.cls.includes('warning') ? 'job-result-warning' : '';
            return `<div class="job-result-item ${tone}"${tip}><span>${escapeHtmlText(b.label)}${suffix}</span> <strong>× ${b.count}</strong></div>`;
        }).join(' ');
        return `<section class="job-server-results"><h4 class="job-detail-heading">${logo}${escapeHtmlText(sname)}</h4><div class="job-result-list">${badges}</div></section>`;
    }).filter(Boolean).join('');
    if (!lines && !fileIssues && !sourcesBlock && !cpuLine) return '';
    const cfg = (job && job.config) || {};
    const totalRuns = (cfg.retry_attempt || 0) + 1;
    const scope = cfg.is_retry_chain
        ? `<p class="small text-muted mb-2">Counts include results recorded across all ${totalRuns} ${totalRuns === 1 ? 'run' : 'runs'} of this retry chain.</p>` : '';
    const header = '<h3 class="job-detail-heading">Results recorded so far</h3>' + scope;
    const servers = lines ? `<div class="job-server-grid">${lines}</div>` : '';
    const issues = fileIssues
        ? `<section class="job-file-issues"><h4 class="job-detail-heading">File issues</h4><div class="job-result-list">${fileIssues}</div></section>` : '';
    return `<div class="job-results">${header}${servers}${issues}${cpuLine}${sourcesBlock}</div>`;
}

// Pick the retry-chain info-modal template matching the Job's server
// type. Plex's wait reason (library scan latency) and Jellyfin's wait
// reason (LibraryMonitor + Generate Trickplay Images task) are
// different enough that one tooltip can't honestly describe both —
// the previous unified template buried Plex users in Jellyfin-specific
// detail that didn't apply to their setup. Each chain Job carries a
// single server_type because the retry queue keys by
// (canonical_path, server_id), so per-chain branching is unambiguous.
function _pickRetryInfoTpl(job) {
    const t = (job && job.server_type || '').toLowerCase();
    return t === 'plex' ? 'infoRetryChainPlexTpl' : 'infoRetryChainJellyfinTpl';
}

// Retry chip rendered next to the title on retry-chain rows. Shows
// ONLY while the chain is actually in flight — any other status
// (completed / failed / cancelled / paused) falls through the
// ``activelyRetrying`` guard and renders nothing. Critical invariant:
// is_retry / max_retries / retry_attempt config flags survive on the
// Job after termination, so gating on those alone would leave the
// chip stuck on a green-Completed row.
//
// Word choice: "Retry N/M" reads as what's actually happening from
// the user's perspective. The internal vocabulary (trickplay
// registration / per-item Refresh API) is a Jellyfin/Emby
// implementation detail surfaced via the tooltip if they hover.
function _renderRetryChip(job) {
    const cfg = job.config || {};
    if (!cfg.is_retry_chain) return '';
    const max = typeof cfg.max_retries === 'number' ? cfg.max_retries : 0;
    if (max <= 0) return '';
    const status = (job.status || '').toLowerCase();
    const eta = job.progress && job.progress.retry_eta;
    const activelyRetrying = status === 'running'
        || (status === 'pending' && !!eta);
    if (!activelyRetrying) return '';
    const attempt = typeof cfg.retry_attempt === 'number' ? cfg.retry_attempt : 0;
    // An Intro & Credits chain retries files a server hadn't added to its library yet; the info templates below
    // explain preview tiles, so its chip goes without them.
    if (_hasOwnRunner(job)) {
        const retryTip = job.kind === JOB_KIND_LOUDNESS
            ? 'Automatically checks loudness again when Plex or the source file becomes available; no review is needed'
            : 'Checks these files again: a server hadn\'t added them to its library yet';
        return ' <span class="badge bg-warning text-dark ms-1 markers-chain-retry-chip" '
            + 'title="' + escapeHtmlAttr(retryTip) + '">'
            + '<i class="bi bi-arrow-clockwise me-1"></i>Retry ' + attempt + '/' + max + '</span>';
    }
    // Trailing info-icon opens the shared #globalInfoModal with the
    // full retry-chain explanation (what's happening, why the wait,
    // backoff schedule, when it gives up). Source of truth lives in
    // ``_shared_info_templates.html`` — Plex/Jellyfin variants picked
    // via ``_pickRetryInfoTpl`` above.
    return ' <span class="badge bg-warning text-dark ms-1 d-inline-flex align-items-center" '
        + 'title="Auto-retrying — click ⓘ for details">'
        + '<i class="bi bi-arrow-clockwise me-1"></i>Retry ' + attempt + '/' + max
        + ' <button type="button" class="info-icon btn btn-link p-0 ms-1 align-baseline text-dark"'
        + ' data-bs-toggle="tooltip" data-bs-placement="top"'
        + ' data-explain-template="' + _pickRetryInfoTpl(job) + '"'
        + ' data-explain-title="Why this file is auto-retrying"'
        + ' title="Why this file is being tried again."'
        + ' aria-label="About retry chain">'
        + '<i class="bi bi-info-circle"></i></button>'
        + '</span>';
}

// "Retry: …" Intro & Credits jobs from before retries ran in their job's own row re-check files a server hadn't added
// to its library yet. They are ordinary jobs, so the chip stays after they finish. A retry chain's row has the chain
// chip (_renderRetryChip) instead.
function _renderMarkersRetryChip(job) {
    if (job.config && job.config.is_retry_chain) return '';
    const attempt = job.config && Number(job.config.retry_attempt);
    if (!attempt || attempt < 1) return '';
    return ' <span class="badge bg-warning text-dark markers-retry-chip" '
        + 'title="Checks these files again: a server hadn\'t added them to its library yet">'
        + '<i class="bi bi-arrow-clockwise me-1"></i>Retry ' + attempt + '</span>';
}

function _markersPauseNote(state, job) {
    if (state === 'own' && job?.config?.pause_reasons?.includes('schedule')) return 'Paused by its job schedule. The next scheduled start clears that hold; other pause settings still apply.';
    if (state === 'own') return 'This job is paused. Resume clears its manual pause; other pause settings still apply.';
    if (state === 'all') return 'Held by Pause all processing. It continues when processing resumes.';
    return '';
}

// Each job owns its pause independently from the global controls.
function _markersPauseButton(job) {
    if (!['pending', 'running'].includes(job.status)) return '';
    if (job.config && job.config.is_retry_chain
        && ['scheduled', 'queued_for_slot', 'running'].includes(job.config.last_outcome)) return '';
    const jid = escapeHtml(job.id);
    if (job.paused) {
        return `<button class="btn btn-outline-success" onclick="resumeJob('${jid}')" title="Resume this job" aria-label="Resume job">
                    <i class="bi bi-play-fill"></i>
                </button>`;
    }
    return `<button class="btn btn-outline-warning" onclick="pauseJob('${jid}')" title="Pause this job" aria-label="Pause job">
                    <i class="bi bi-pause-fill"></i>
                </button>`;
}

let _jobQueueUpdatePending = false;

function updateJobQueue(force) {
    const tbody = document.getElementById('jobQueue');
    // The Job Queue table only exists on the dashboard. SocketIO connect/job
    // events fire on every page, so bail when the target DOM is absent —
    // otherwise reconnect from /settings or /servers crashes with
    // "Cannot set properties of null (setting 'innerHTML')".
    if (!tbody) {
        return;
    }

    // Defer rebuild while a priority dropdown is open to avoid destroying it
    if (tbody.querySelector('.dropdown-menu.show')) {
        _jobQueueUpdatePending = true;
        return;
    }
    // Defer while the user is hovering inside the table — a wholesale
    // tbody.innerHTML rebuild mid-hover destroys the button under the
    // cursor and the click never lands. Symptom from the field: the
    // red Cancel-job X "flashes red" on hover but never actually fires
    // the cancel because the row gets re-rendered between mousedown
    // and mouseup. The :hover check is a Bootstrap pattern (CSS
    // pseudo-class is queryable via :is(...:hover) on tbody.matches);
    // we use a `querySelector(':hover')` that walks any descendant
    // currently hovered. Safe because we'll rebuild on the next tick.
    if (force !== true && (tbody.matches(':hover') || tbody.querySelector(':hover'))) {
        _jobQueueUpdatePending = true;
        return;
    }
    _jobQueueUpdatePending = false;

    if (jobs.length === 0) {
        if (jobTotal === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-5">
                        <i class="bi bi-inbox fs-2 d-block mb-2 opacity-50"></i>
                        <div>${jobFiltersActive() ? 'No jobs match these filters.' : 'Nothing queued.'}</div>
                        <div class="small">${jobFiltersActive() ? 'Change a filter or use Clear filters to see other jobs.' : 'New jobs will show up here once you start one.'}</div>
                    </td>
                </tr>
            `;
        } else {
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-4">
                        No jobs on this page
                    </td>
                </tr>
            `;
        }
        return;
    }

    let html = '';

    const activeJobIds = new Set(jobs.map((job) => String(job.id)));
    for (const expandedId of Array.from(expandedJobFileRows)) {
        if (!activeJobIds.has(expandedId)) {
            expandedJobFileRows.delete(expandedId);
        }
    }

    for (const job of _orderFollowUps(jobs)) {
        const isMarkers = _isMarkersJob(job);
        const ownRunner = _hasOwnRunner(job);
        const markersPause = _markersPauseState(job);
        const statusBadge = getStatusBadge(
            job.status, job.paused || markersPause === 'all', job.error, job.progress && job.progress.outcome,
            _markersPauseNote(markersPause, job),
        );
        const progress = (Number(job.progress?.percent) || 0).toFixed(1);
        const created = formatRelativeTime(job.created_at);
        let actionButtons = '';

        // Per-row countdown flags. Computed once and reused for both
        // the action-button group below AND the progress cell further
        // down so the two stay consistent (button visible IFF
        // countdown is visible).
        const _scheduledAtPre = job.config && job.config.scheduled_at;
        const _retryEtaPre = job.progress && job.progress.retry_eta;
        const _inWorkerRetryWaitPre = ['pending', 'running'].includes(job.status)
            && !!_retryEtaPre && new Date(_retryEtaPre).getTime() > Date.now() - 1500;
        const _isRetryRowPre = !!(job.config && (job.config.is_retry || job.config.is_retry_chain));
        const isWaitingRetryRow =
            (job.status === 'pending' && _isRetryRowPre && _scheduledAtPre) || _inWorkerRetryWaitPre;
        const webhookFireAt = job.config && job.config.webhook_fire_at;
        const isWaitingWebhookRow =
            job.status === 'pending'
            && !isWaitingRetryRow
            && !!webhookFireAt
            && new Date(webhookFireAt).getTime() > Date.now() - 1500;

        // Use a btn-group so spacing is consistent between adjacent
        // icon buttons and the row's right edge — previously buttons
        // had ad-hoc me-1 margins that produced different gaps depending
        // on which set was rendered.
        if (job.status === 'running' || job.status === 'pending') {
            // Pending/running rows used to expose only a Cancel button —
            // users had no way to peek at a long-running job's logs or
            // open a retry-chain row's synthesized status. Adding View
            // Logs here mirrors the Active Jobs panel button group.
            //
            // The two "skip the wait" buttons live next to View Logs so
            // the per-row controls match the per-row countdown in the
            // progress cell:
            //   * Fire now (bi-lightning)  — webhook debounce window;
            //                                cancels the debounce timer
            //                                and dispatches immediately.
            //   * Retry now (bi-arrow-clockwise) — chain-head backoff;
            //                                short-circuits the backoff
            //                                wait so the next attempt
            //                                fires immediately.
            // Different icons + different tooltips on purpose: the verbs
            // map to different upstream actions, even though both feel
            // like "do it now" from the user's seat.
            const fireWebhookBtn = isWaitingWebhookRow
                ? `<button class="btn btn-outline-warning" onclick="fireWebhookNow('${escapeHtml(job.id)}')" title="Skip the webhook wait — dispatch now" aria-label="Fire webhook now">
                    <i class="bi bi-lightning-fill"></i>
                </button>`
                : '';
            // Retry now drives retry chains (preview and Intro & Credits); a "Retry: …" Intro & Credits or loudness
            // job just waits out its delay.
            const retryNowBtn = isWaitingRetryRow && (!ownRunner || !!(job.config && job.config.is_retry_chain))
                ? `<button class="btn btn-outline-warning" onclick="retryNowFromRow('${escapeHtml(job.id)}')" title="Skip the retry backoff — attempt now" aria-label="Retry now">
                    <i class="bi bi-arrow-clockwise"></i>
                </button>`
                : '';
            actionButtons = `<div class="btn-group btn-group-sm icon-btn-group" role="group">
                <button class="btn btn-outline-secondary" onclick="showLogsModal('${escapeHtml(job.id)}')" title="View logs" aria-label="View logs">
                    <i class="bi bi-file-text"></i>
                </button>
                ${fireWebhookBtn}${retryNowBtn}${_markersPauseButton(job)}
                <button class="btn btn-outline-danger" onclick="cancelJob('${escapeHtml(job.id)}')" title="Cancel" aria-label="Cancel job">
                    <i class="bi bi-x-lg"></i>
                </button>
            </div>`;
        } else {
            actionButtons = `<div class="btn-group btn-group-sm icon-btn-group" role="group">
                <button class="btn btn-outline-secondary" onclick="showLogsModal('${escapeHtml(job.id)}')" title="View logs" aria-label="View logs">
                    <i class="bi bi-file-text"></i>
                </button>
                <button class="btn btn-outline-secondary" onclick="reprocessJob('${escapeHtml(job.id)}')" title="Re-run" aria-label="Re-run job">
                    <i class="bi bi-arrow-repeat"></i>
                </button>
                <button class="btn btn-outline-danger" onclick="deleteJob('${escapeHtml(job.id)}')" title="Delete" aria-label="Delete job">
                    <i class="bi bi-trash"></i>
                </button>
            </div>`;
        }

        let webhookBasenames = job.config && Array.isArray(job.config.webhook_basenames) && job.config.webhook_basenames.length > 0
            ? job.config.webhook_basenames
            : [];
        if (webhookBasenames.length === 0 && job.config && Array.isArray(job.config.webhook_paths) && job.config.webhook_paths.length > 0) {
            webhookBasenames = job.config.webhook_paths.map(function (p) { return p.split('/').pop() || p; });
        }
        const isFilesExpanded = expandedJobFileRows.has(String(job.id));
        const filesToggleBtn = ` <button type="button" class="btn job-details-toggle" id="job-files-toggle-${escapeHtml(job.id)}"
                        onclick="toggleJobFiles('${escapeHtml(job.id)}')" aria-expanded="${isFilesExpanded ? 'true' : 'false'}" aria-controls="job-detail-${escapeHtml(job.id)}" title="Job details" aria-label="Job details">
                   <span class="visually-hidden">Job details</span><i class="bi ${isFilesExpanded ? 'bi-chevron-up' : 'bi-chevron-down'}"></i>
                 </button>`;
        const retryLabel = _renderRetryChip(job) || (isMarkers ? _renderMarkersRetryChip(job) : '');
        const priorityCell = renderPriorityCell(job);
        const scheduledAt = job.config && job.config.scheduled_at;
        // Two paths land in retry-wait: pending jobs awaiting their first
        // dispatch (scheduled_at on config) and running jobs whose worker
        // is sleeping out the backoff before the first item (retry_eta on
        // progress). Without the second branch, the queue row falls back
        // to the normal progress bar at 0% and the percent jumps as the
        // worker pool emits stale completed/total updates over the top.
        const retryEta = job.progress && job.progress.retry_eta;
        const inWorkerRetryWait = !!retryEta && new Date(retryEta).getTime() > Date.now() - 1500;
        const countdownTarget = inWorkerRetryWait ? retryEta : scheduledAt;
        // ``isWaitingRetryRow`` / ``isWaitingWebhookRow`` were computed
        // earlier (alongside the action-button group) so the same row
        // ALWAYS shows matching button + progress-cell state. Don't
        // recompute here — a divergent value would let the Fire-now /
        // Retry-now button appear without the corresponding countdown
        // (or vice versa).
        let progressCell;
        if (isWaitingWebhookRow) {
            const remaining = Math.max(0, Math.ceil((new Date(webhookFireAt).getTime() - Date.now()) / 1000));
            const label = remaining > 0 ? `Webhook firing in ${remaining}s` : 'Firing...';
            progressCell = `<span class="text-warning small" data-webhook-fire-at="${escapeHtml(webhookFireAt)}"><i class="bi bi-hourglass-split me-1"></i>${label}</span>`;
        } else if (isWaitingRetryRow) {
            const remaining = Math.max(0, Math.ceil((new Date(countdownTarget).getTime() - Date.now()) / 1000));
            const waitWord = _countdownWord(job, 'Retry starting');
            const label = remaining > 0 ? `${waitWord} ${_formatCountdown(remaining)}` : 'Starting...';
            progressCell = `<span class="text-warning small" data-scheduled-at="${escapeHtml(countdownTarget)}" data-countdown-label="${escapeHtmlAttr(waitWord)}"><i class="bi bi-hourglass-split me-1"></i>${label}</span>`;
        } else {
            // Color the bar by status — blue (primary) is reserved for
            // running. Completed/failed/cancelled get the matching outcome
            // colour so the bar reinforces the status pill rather than
            // contradicting it (a 100% blue bar next to a green
            // "Completed" pill was confusing the eye).
            // A paused Intro & Credits job does no work, so its bar doesn't animate.
            const barClass = markersPause ? 'bg-warning' : (({
                completed: 'bg-success',
                failed: 'bg-danger',
                cancelled: 'bg-secondary',
                running: 'progress-bar-striped progress-bar-animated',
                pending: 'bg-secondary',
            })[job.status] || '');
            const processed = Number(job.progress.processed_items) || 0;
            const total = Number(job.progress.total_items) || 0;
            const hasProgress = total > 0 || Number(progress) > 0;
            progressCell = job.status === 'pending' && !processed && Number(progress) === 0
                ? `<span class="small text-body-secondary">Queued${total ? ` · ${total.toLocaleString()} ${total === 1 ? 'item' : 'items'}` : ''}</span>`
                : hasProgress
                ? `<div class="queue-progress-label"><span data-queue-percent>${progress}%</span><span data-queue-items>${processed.toLocaleString()} / ${total ? total.toLocaleString() : '?'}</span></div>
                    <div class="progress queue-progress" data-status="${escapeHtml(job.status)}"><div class="progress-bar ${barClass}" role="progressbar" aria-label="Job progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}" style="width: ${Math.min(100, Math.max(0, Number(progress)))}%"></div></div>`
                : `<span class="small text-body-secondary">${job.status === 'pending' ? 'Queued' : job.status === 'running' ? 'Starting…' : 'No progress recorded'}</span>`;

        }
        const followsId = isMarkers && job.config && job.config.follows_job_id
            && jobs.some(function (j) { return String(j.id) === String(job.config.follows_job_id); })
            ? String(job.config.follows_job_id)
            : '';
        const followTip = `Runs after preview job ${followsId.substring(0, 8)} finishes its first try`;
        const rowTitle = isMarkers ? _markersDisplayName(job.library_name) : job.kind === JOB_KIND_LOUDNESS
            ? (job.library_name || '').replace(/^((?:Retry|Verify): )?Plex loudness(?::| ·) (.+)$/, '$1$2') : (job.library_name || '');
        const nameHtml = (followsId
            ? `<i class="bi bi-arrow-return-right job-follow-arrow" tabindex="0" data-bs-toggle="tooltip" title="${escapeHtmlAttr(followTip)}" aria-label="${escapeHtmlAttr(followTip)}"></i>` : '')
            + `<span class="queue-job-title">${escapeHtml(rowTitle) || 'All Libraries'}</span>` + _versionRerunInfoHtml(job);
        const wait = job.config?.resource_wait;
        const activityText = wait?.reason || (['running', 'pending'].includes(job.status) ? job.progress.current_item : '');
        const genericWait = job.status === 'pending' && !markersPause && !wait && !job.config?.is_retry_chain
            && !job.config?.retry_attempt && !isWaitingRetryRow && !isWaitingWebhookRow
            && !Number(job.progress.processed_items) && !Number(progress)
            && ['Waiting for an available worker', 'Waiting for available worker'].includes(activityText);
        if (genericWait) {
            const total = Number(job.progress.total_items) || 0;
            progressCell = `<span class="small text-body-secondary" tabindex="0" title="${escapeHtmlAttr(activityText)}">${total ? `${total.toLocaleString()} ${total === 1 ? 'item' : 'items'} · ` : ''}Awaiting worker</span>`;
        }
        const phase = activityText && !genericWait ? `<span class="queue-phase text-body-secondary">${escapeHtml(activityText)}</span>` : '';
        html += `
            <tr id="job-row-${escapeHtml(job.id)}" class="job-row${followsId ? ' job-row-follow-up' : ''}">
                <td class="queue-id" data-label="ID"><code title="${escapeHtmlAttr(job.id)}">${escapeHtml(job.id.substring(0, 8))}</code></td>
                <td class="queue-job" data-label="Job">
                    <div class="queue-job-main">${filesToggleBtn}<div class="queue-job-copy"><div class="queue-title-line">${nameHtml}</div>
                    <div class="queue-metadata">${_queueMetadataHtml(job, rowTitle)}</div></div></div>
                </td>
                <td class="queue-status" data-label="Status">${statusBadge}${retryLabel}</td>
                <td class="queue-priority" data-label="Priority">${priorityCell}</td>
                <td class="queue-progress-cell" data-label="Progress">${progressCell}${phase}</td>
                <td class="queue-created text-body-secondary" data-label="Created"><time datetime="${escapeHtmlAttr(job.created_at)}" title="${escapeHtmlAttr(formatDate(job.created_at))}">${created}</time></td>
                <td class="queue-actions" data-label="Actions">${actionButtons}</td>
            </tr>`;
        const filesBlock = `<section class="job-detail-files">${_markersFilesBlock(job)}</section>`;
        const publishersBlock = _renderPublishersBlock(job);
        const currentItem = job.progress?.current_item;
        const started = job.started_at ? `<span>Started ${escapeHtml(formatDate(job.started_at))}${job.status === 'running' ? ` · Job elapsed <span data-elapsed-since="${escapeHtmlAttr(job.started_at)}">${formatElapsed(job.started_at)}</span>` : ''}${job.completed_at ? ` · Finished ${escapeHtml(formatDate(job.completed_at))}` : ''}</span>` : '';
        const nextEligible = wait?.next_eligible ? `<p class="small mb-0">Next eligible: ${escapeHtml(formatDate(wait.next_eligible))}</p>` : '';
        const activity = currentItem || started || wait?.reason
            ? `<section class="job-current-activity"><h3 class="job-detail-heading">${['running', 'pending'].includes(job.status) ? 'Current activity' : 'Last activity'}</h3>${currentItem || wait?.reason ? `<p class="mb-1 queue-full-path">${escapeHtml(wait?.reason || currentItem)}</p>` : ''}${nextEligible}${started ? `<div class="small text-body-secondary">${started}</div>` : ''}</section>` : '';
        const error = job.error ? `<section class="job-detail-error"><h3 class="job-detail-heading text-danger-emphasis"><i class="bi bi-exclamation-triangle me-1"></i>Job error</h3><p class="mb-0 queue-full-path">${escapeHtml(job.error)}</p></section>` : '';
        html += `<tr id="job-detail-${escapeHtml(job.id)}" class="${isFilesExpanded ? '' : 'd-none'} job-files-detail" aria-hidden="${isFilesExpanded ? 'false' : 'true'}">
            <td colspan="7" class="bg-body-tertiary job-expanded-cell"><div class="job-expanded-content">
                <div class="queue-detail-summary">${activity}<div class="queue-results" id="queue-publishers-${escapeHtml(job.id)}">${publishersBlock}</div>${error}</div>
                ${filesBlock}
                <div class="job-detail-actions"><button class="btn btn-sm btn-outline-secondary" onclick="openJobDetails('${escapeHtml(job.id)}')"><i class="bi bi-file-text me-1"></i>Open logs and files</button></div>
            </div></td></tr>`;

    }

    const openPaths = Array.from(tbody.querySelectorAll('details[data-file-index][open]')).map(el => ({
        row: el.closest('tr').id, index: el.dataset.fileIndex,
    }));
    const openOwners = Array.from(tbody.querySelectorAll('.queue-owner-details[open]')).map(el => el.closest('tr').id);
    const focusedOwnerRow = tbody.contains(document.activeElement) && document.activeElement.matches('.queue-owner-details > summary')
        ? document.activeElement.closest('tr').id : null;
    _disposeBootstrapTooltips(tbody);
    tbody.innerHTML = html;
    openPaths.forEach(({row, index}) => document.getElementById(row)?.querySelector(`details[data-file-index="${CSS.escape(index)}"]`)?.setAttribute('open', ''));
    openOwners.forEach(row => document.getElementById(row)?.querySelector('.queue-owner-details')?.setAttribute('open', ''));
    if (focusedOwnerRow) document.getElementById(focusedOwnerRow)?.querySelector('.queue-owner-details > summary')?.focus({preventScroll: true});
    jobs.forEach(function (job) {
        if (expandedJobFileRows.has(String(job.id)) && document.getElementById('job-file-list-' + job.id)) _loadJobFileList(job);
    });

    // Status badges and ⓘs (the Retry chip's) get their tooltips; ⓘs also get the app-wide ⓘ rule.
    _initBootstrapTooltips(tbody);

    // Start the 1Hz ticker when ANY live countdown is on the page —
    // retry (`data-scheduled-at`) OR webhook debounce
    // (`data-webhook-fire-at`). Pre-fix the webhook branch was missing
    // here so a webhook-only queue (no retry rows) left the ticker
    // un-started — the row's "Webhook firing in Xs" text only
    // refreshed when ``loadJobs`` re-rendered the table (every 5s),
    // looking like the countdown was running slow.
    if (
        document.querySelector('[data-elapsed-since]')
        || document.querySelector('[data-scheduled-at]')
        || document.querySelector('[data-webhook-fire-at]')
    ) {
        _ensureElapsedTimer();
    }
}

function renderJobPagination() {
    const footer = document.getElementById('jobPaginationFooter');
    const info = document.getElementById('jobPaginationInfo');
    const controls = document.getElementById('jobPaginationControls');
    const perPageSelect = document.getElementById('jobPerPageSelect');

    if (!footer) return;

    if (jobTotal === 0) {
        footer.classList.add('d-none');
        return;
    }
    footer.classList.remove('d-none');

    perPageSelect.value = String(jobPerPage);

    const start = (jobPage - 1) * jobPerPage + 1;
    const end = Math.min(jobPage * jobPerPage, jobTotal);
    info.textContent = `Showing ${start}\u2013${end} of ${jobTotal}`;

    let pagesHtml = '';
    pagesHtml += `<li class="page-item ${jobPage <= 1 ? 'disabled' : ''}">
        <a class="page-link" href="#" onclick="goToJobPage(${jobPage - 1}); return false;" aria-label="Previous">&lsaquo;</a></li>`;

    const maxVisible = 5;
    let rangeStart = Math.max(1, jobPage - Math.floor(maxVisible / 2));
    let rangeEnd = Math.min(jobTotalPages, rangeStart + maxVisible - 1);
    if (rangeEnd - rangeStart + 1 < maxVisible) {
        rangeStart = Math.max(1, rangeEnd - maxVisible + 1);
    }

    if (rangeStart > 1) {
        pagesHtml += `<li class="page-item"><a class="page-link" href="#" onclick="goToJobPage(1); return false;">1</a></li>`;
        if (rangeStart > 2) {
            pagesHtml += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
        }
    }

    for (let p = rangeStart; p <= rangeEnd; p++) {
        pagesHtml += `<li class="page-item ${p === jobPage ? 'active' : ''}">
            <a class="page-link" href="#" onclick="goToJobPage(${p}); return false;">${p}</a></li>`;
    }

    if (rangeEnd < jobTotalPages) {
        if (rangeEnd < jobTotalPages - 1) {
            pagesHtml += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
        }
        pagesHtml += `<li class="page-item"><a class="page-link" href="#" onclick="goToJobPage(${jobTotalPages}); return false;">${jobTotalPages}</a></li>`;
    }

    pagesHtml += `<li class="page-item ${jobPage >= jobTotalPages ? 'disabled' : ''}">
        <a class="page-link" href="#" onclick="goToJobPage(${jobPage + 1}); return false;" aria-label="Next">&rsaquo;</a></li>`;

    controls.innerHTML = pagesHtml;
}

function goToJobPage(page) {
    if (page < 1 || page > jobTotalPages) return;
    jobPage = page;
    loadJobs();
}

function changeJobPerPage(value) {
    jobPerPage = parseInt(value, 10) || 50;
    jobPage = 1;
    localStorage.setItem('jobPerPage', String(jobPerPage));
    loadJobs();
}

function updateActiveJobs(runningJobs, force) {
    const container = document.getElementById('activeJobsContainer');
    const countBadge = document.getElementById('activeJobsCount');
    if (!container || !countBadge) return;

    // Same defer-on-hover guard as updateJobQueue: the wholesale
    // ``container.innerHTML = html`` rebuild every poll destroys the
    // Cancel-job button mid-hover, making the icon flicker (loses
    // hover state for ~1 frame) and — when the rebuild lands between
    // mousedown and mouseup — ate the click entirely. The hover
    // check defers the rebuild until the cursor moves out; pending
    // updates land on the next tick.
    if (force !== true && container && (container.matches(':hover') || container.querySelector(':hover'))) {
        return;
    }

    if (!runningJobs || runningJobs.length === 0) {
        countBadge.textContent = 'Idle';
        countBadge.className = 'badge bg-secondary';
        // Compact one-line empty state — the previous big-icon block was
        // ~200px of vertical space spent saying nothing. Match the
        // template's static empty-state markup so first paint and the
        // post-load JS render are visually identical.
        container.innerHTML = `
            <div class="text-muted small d-flex align-items-center justify-content-center gap-2 py-1">
                <i class="bi bi-inbox"></i>
                <span>Idle &mdash; no jobs in flight.</span>
            </div>
        `;
        _stopElapsedTimer();
        refreshWorkerScaleButtons();
        return;
    }

    countBadge.textContent = `${runningJobs.length} running`;
    countBadge.className = 'badge bg-primary pulse';

    let html = '';
    for (const job of runningJobs) {
        const jid = escapeHtml(job.id);
        const markersPause = _markersPauseState(job);
        const isPaused = !!job.paused || markersPause === 'all';
        const retryEta = job.progress && job.progress.retry_eta;
        const retryWaitTotal = job.progress && job.progress.retry_wait_total;
        // Worker has picked the job up but is sleeping out the retry
        // backoff — render this as its own state, not as "Running 0%."
        const isRetryWaiting = !isPaused && !!retryEta && new Date(retryEta).getTime() > Date.now() - 1500;
        const retryChip = _renderRetryChip(job);
        const retryAttempt = (job.config && job.config.retry_attempt) || 0;
        const maxRetries = job.config && job.config.max_retries;
        let statusBadge;
        if (isPaused) {
            const note = _markersPauseNote(markersPause, job);
            statusBadge = `<span class="badge bg-warning text-dark"${note ? ` title="${escapeHtmlAttr(note)}"` : ''}>Paused</span>`;
        } else if (isRetryWaiting) {
            const waitText = _isMarkersVerifyJob(job) ? 'Waiting to check again' : 'Waiting to retry';
            statusBadge = `<span class="badge bg-warning text-dark"><i class="bi bi-hourglass-split me-1"></i>${waitText}</span>`;
        } else {
            statusBadge = '<span class="badge bg-primary pulse">Running</span>';
        }
        const progress = (Number(job.progress?.percent) || 0).toFixed(1);

        // Build collapsible file list (matching Job Queue pattern)
        let webhookFilesHtml = '';
        let webhookFiles = job.config && Array.isArray(job.config.webhook_basenames) && job.config.webhook_basenames.length > 0
            ? job.config.webhook_basenames
            : null;
        if (!webhookFiles && job.config && Array.isArray(job.config.webhook_paths) && job.config.webhook_paths.length > 0) {
            webhookFiles = job.config.webhook_paths.map(function (p) { return p.split('/').pop() || p; });
        }
        if (webhookFiles && webhookFiles.length > 1) {
            const filesId = `active-job-files-${jid}`;
            const isExpanded = expandedActiveJobFiles.has(String(job.id));
            const pathCount = (job.config && typeof job.config.path_count === 'number') ? job.config.path_count : webhookFiles.length;
            const filesList = webhookFiles.map(function (b) { return `<div class="text-muted">${escapeHtml(b)}</div>`; }).join('');
            const overflow = pathCount > webhookFiles.length
                ? `<div class="text-muted mt-1">(+${pathCount - webhookFiles.length} more)</div>`
                : '';
            webhookFilesHtml = `
                <div class="mt-1 small">
                    <strong>Files:</strong> ${pathCount} file(s)
                    <button type="button" class="btn job-details-toggle" onclick="toggleActiveJobFiles('${jid}')"
                            aria-expanded="${isExpanded}" title="Show files"><span>Show files</span>
                        <i class="bi ${isExpanded ? 'bi-chevron-up' : 'bi-chevron-down'}"></i>
                    </button>
                    <div id="${filesId}" class="${isExpanded ? '' : 'd-none'} mt-1 ms-3">${filesList}${overflow}</div>
                </div>`;
        }
        // Single-file jobs intentionally omit the "File:" line here — the
        // library_name already contains the filename (e.g.
        // "Manual: Foo.mkv", "Sonarr: Show S01E01") so showing it twice
        // is redundant noise. Multi-file jobs keep the expandable list.

        // Start time and elapsed
        const startedLine = job.started_at
            ? `<span class="text-muted small"><i class="bi bi-clock me-1"></i>Started ${formatDate(job.started_at)} (<span data-elapsed-since="${escapeHtml(job.started_at)}">${formatElapsed(job.started_at)}</span>)</span>`
            : '';

        const activePri = job.priority || 2;
        const activePriBadge = `<span class="badge ${PRIORITY_BADGE_CLASS[activePri] || 'bg-primary'} priority-badge ms-1">${PRIORITY_LABELS[activePri] || 'Normal'}</span>`;

        // Strip the worker's "Retry: " library_name prefix while in the
        // waiting state — the chip already says "Retry N/M" and the
        // status pill says "Waiting to retry," so the prefix is noise.
        let libraryDisplay = escapeHtml(job.library_name) || 'All Libraries';
        if (isRetryWaiting && job.library_name && job.library_name.startsWith('Retry: ')) {
            libraryDisplay = escapeHtml(job.library_name.slice('Retry: '.length));
        }
        if (_isMarkersJob(job)) {
            libraryDisplay = (escapeHtml(_markersDisplayName(job.library_name)) || 'All Libraries')
                + _renderMarkersRetryChip(job);
        }
        libraryDisplay = _jobKindBadgeHtml(job) + libraryDisplay;

        let progressBlock;
        if (isRetryWaiting) {
            const totalSec = retryWaitTotal && retryWaitTotal > 0 ? retryWaitTotal : 30;
            const remaining = Math.max(0, Math.ceil((new Date(retryEta).getTime() - Date.now()) / 1000));
            const fillPct = Math.max(0, Math.min(100, ((totalSec - remaining) / totalSec) * 100)).toFixed(1);
            const waitWord = _countdownWord(job, 'Next attempt');
            // A verify job isn't recovering from a failure: it checks replaced files again once servers had time to rescan.
            const waitNote = _isMarkersVerifyJob(job)
                ? 'Servers often rescan a replaced file after its markers are sent.'
                : 'Backing off after a failure — will try again automatically.';
            progressBlock = `
            <div class="progress retry-countdown-bar" style="height: 24px;"
                 data-retry-eta="${escapeHtml(retryEta)}" data-retry-wait-total="${totalSec}" data-countdown-label="${escapeHtmlAttr(waitWord)}">
                <div class="progress-bar bg-warning text-dark progress-bar-striped progress-bar-animated"
                     role="progressbar" style="width: ${fillPct}%"
                     id="activeJobProgress-${jid}">
                    <span class="retry-countdown-label">${waitWord} ${_formatCountdown(remaining)}</span>
                </div>
            </div>
            <div class="d-flex justify-content-between mt-1 small">
                <span class="text-warning" id="activeJobItem-${jid}">
                    <i class="bi bi-hourglass-split me-1"></i>${waitNote}
                </span>
                <span class="text-muted" id="activeJobItems-${jid}">${retryAttempt > 0 ? `Attempt ${retryAttempt}${maxRetries ? ` of ${maxRetries}` : ''}` : ''}</span>
            </div>`;
        } else {
            progressBlock = `
            <div class="progress" style="height: 24px;">
                <div class="progress-bar progress-bar-striped progress-bar-animated"
                     role="progressbar" style="width: ${progress}%" id="activeJobProgress-${jid}">
                    ${progress}%
                </div>
            </div>
            <div class="d-flex justify-content-between mt-1 small text-muted">
                <span id="activeJobItem-${jid}">${escapeHtml(job.progress.current_item) || 'Starting...'}</span>
                <span id="activeJobItems-${jid}">Items: ${job.progress.processed_items || 0} / ${job.progress.total_items || '?'}</span>
            </div>`;
        }

        html += `
        <div class="active-job-card mb-3 p-3 border rounded${isRetryWaiting ? ' retry-waiting' : ''}" id="active-job-${jid}">
            <div class="d-flex justify-content-between align-items-start mb-2 gap-2 flex-wrap">
                <div class="d-flex align-items-center flex-wrap gap-2 min-w-0">
                    <span><strong>Job:</strong> <code>${jid.substring(0, 8)}</code></span>
                    ${statusBadge}${retryChip}${activePriBadge}
                </div>
                <div class="btn-group btn-group-sm icon-btn-group flex-shrink-0" role="group">
                    <button class="btn btn-outline-info" onclick="showLogsModal('${jid}')" title="View Logs" aria-label="View logs">
                        <i class="bi bi-file-text"></i>
                    </button>${_markersPauseButton(job)}
                    <button class="btn btn-outline-danger" onclick="cancelJob('${jid}')" title="Cancel job" aria-label="Cancel job">
                        <i class="bi bi-x-lg"></i>
                    </button>
                </div>
            </div>
            <div class="mb-2 small">
                <strong>Job:</strong> ${libraryDisplay}${_serverBadge(job)}${webhookFilesHtml}
            </div>
            ${startedLine ? `<div class="mb-2">${startedLine}</div>` : ''}
            <div class="job-current-activity"><h3 class="job-detail-heading">Current activity</h3>${progressBlock}</div>
            <div id="activeJobPublishers-${jid}">${_renderPublishersBlock(job)}</div>
        </div>`;
    }

    _disposeBootstrapTooltips(container);
    container.innerHTML = html;
    _initBootstrapTooltips(container);
    _ensureElapsedTimer();
    refreshWorkerScaleButtons();
}

function removeActiveJob(jobId) {
    const el = document.getElementById('active-job-' + jobId);
    if (el) el.remove();

    const container = document.getElementById('activeJobsContainer');
    if (container && container.querySelectorAll('.active-job-card').length === 0) {
        updateActiveJobs([]);
    }
}

// Cache latest progress per job so events arriving before the active-job
// DOM card is created can be replayed once loadJobs() renders it.
const _pendingProgress = {};

function updateJobProgress(jobId, progress, publishers) {
    const job = jobs.find(item => String(item.id) === String(jobId));
    if (!job) {
        _pendingProgress[jobId] = progress;
        return;
    }
    job.progress = Object.assign({}, job.progress, progress || {});
    if (publishers !== undefined) job.publishers = publishers;
    const row = document.getElementById('job-row-' + jobId);
    if (!row) {
        _pendingProgress[jobId] = progress;
        return;
    }
    delete _pendingProgress[jobId];
    // Countdown owners keep their own clock, rather than a progress event overwriting the wait label.
    const waiting = row.querySelector('[data-scheduled-at], [data-webhook-fire-at]');
    const pct = Number(job.progress.percent) || 0;
    if (!waiting) {
        const bar = row.querySelector('.queue-progress .progress-bar');
        if (bar) {
            bar.style.width = `${Math.min(100, Math.max(0, pct))}%`;
            bar.setAttribute('aria-valuenow', pct.toFixed(1));
        }
        const label = row.querySelector('[data-queue-percent]');
        if (label) label.textContent = pct.toFixed(1) + '%';
        const count = row.querySelector('[data-queue-items]');
        if (count) count.textContent = `${Number(job.progress.processed_items || 0).toLocaleString()} / ${job.progress.total_items ? Number(job.progress.total_items).toLocaleString() : '?'}`;
        const phase = row.querySelector('.queue-phase');
        if (phase) phase.textContent = job.config?.resource_wait?.reason || job.progress.current_item || '';
    }
    const detail = document.getElementById('job-detail-' + jobId);
    const activity = detail?.querySelector('.job-current-activity p');
    if (activity) activity.textContent = job.config?.resource_wait?.reason || job.progress.current_item || '';
    const results = document.getElementById('queue-publishers-' + jobId);
    if (results) {
        const html = _renderPublishersBlock(job);
        if (results._renderedHtml !== html) {
            _disposeBootstrapTooltips(results);
            results.innerHTML = html;
            results._renderedHtml = html;
            _initBootstrapTooltips(results);
        }
    }
}

// Worker Status Functions

// Keep worker nodes keyed while groups and task progress refresh independently.
// Native filename disclosures retain their open state across progress events.
let _latestDashboardWorkers = [];
window.addEventListener('worker-groups-updated', function () {
    if (_latestDashboardWorkers.length) updateWorkerStatuses(_latestDashboardWorkers);
});

function updateWorkerStatuses(workers, options = {}) {
    const {
        fallbackCounts = null,
        keepBadgeCounts = false
    } = options;
    const container = document.getElementById('workerStatusContainer');
    // Bail when the dashboard's worker container isn't on the current page
    // — otherwise SocketIO reconnect from /settings et al. crashes here.
    if (!container) {
        return;
    }
    _latestDashboardWorkers = workers || [];
    window.WorkerGroups?.setWorkerActivity(_latestDashboardWorkers);
    const cpuWorkersEl = document.getElementById('cpuWorkers');

    if (!workers || workers.length === 0) {
        container.querySelectorAll('[data-worker-key]').forEach(node => node.remove());
        const badge = document.getElementById('workersHeaderCount');
        if (badge) badge.textContent = 'No worker slots';
        if (keepBadgeCounts) {
            return;
        }
        const counts = fallbackCounts || cachedWorkerConfigCounts;
        if (counts) {
            if (cpuWorkersEl) cpuWorkersEl.textContent = String(counts.cpu_threads);
        }
        refreshWorkerScaleButtons();
        return;
    }

    const cpuCount = workers.filter(w => w.worker_type === 'CPU').length;
    if (cpuWorkersEl) cpuWorkersEl.textContent = String(cpuCount);

    // Workers panel header count badge — "N active / M slots". Gives the
    // user a glance-able signal of how busy the queue is without forcing
    // them to count rows. Hidden when there's nothing to count.
    const headerBadge = document.getElementById('workersHeaderCount');
    if (headerBadge) {
        const total = workers.length;
        const active = workers.filter(w => w.status === 'processing').length;
        const paused = workers.filter(w => w.status === 'processing' && (processingPaused || w.paused)).length;
        if (total === 0) {
            headerBadge.textContent = '—';
            headerBadge.className = 'badge bg-secondary';
        } else if (active === 0) {
            headerBadge.textContent = `${total} idle`;
            headerBadge.className = 'badge bg-secondary';
        } else {
            headerBadge.textContent = `${active} of ${total} occupied${paused ? ` · ${paused} paused` : ''}`;
            headerBadge.className = 'badge bg-primary';
        }
        // Match the all-caps card-header style suppression we set in HTML
        // so the badge text stays case-as-typed.
        headerBadge.style.textTransform = 'none';
        headerBadge.style.letterSpacing = '0';
    }

    // Surface GPU→CPU fallback transitions as a warning toast (once per switch).
    for (const w of workers) {
        const prev = _fallbackStateByWorker.get(w.worker_id) || false;
        const now = !!w.fallback_active;
        if (now && !prev) {
            const title = w.fallback_title || w.current_title || 'this file';
            const reason = w.fallback_reason || 'GPU processing failed';
            showToast(
                'Switched to CPU',
                `${w.worker_name} fell back to CPU for "${title}" — ${reason}`,
                'warning'
            );
        }
        _fallbackStateByWorker.set(w.worker_id, now);
    }

    let fallback = container.querySelector('[data-worker-fallback]');
    if (!fallback) {
        fallback = document.createElement('div');
        fallback.className = 'worker-group-workers';
        fallback.dataset.workerFallback = '';
        container.appendChild(fallback);
    }

    const seenKeys = new Set();
    for (const worker of workers) {
        const key = `${worker.worker_type}_${worker.worker_id}`;
        seenKeys.add(key);
        const host = window.WorkerGroups?.getDashboardWorkerHost(worker.group_id, worker.group_name) || fallback;
        let col = container.querySelector(`[data-worker-key="${CSS.escape(key)}"]`);
        if (!col) {
            // First sighting of this slot — stamp the static DOM shape
            // once. From here on we only mutate text/class on existing
            // nodes, so there's no flicker.
            col = document.createElement('div');
            col.className = 'worker-slot';
            col.dataset.workerKey = key;
            col.innerHTML = `
                <article class="card bg-body-tertiary workers-panel-card" data-card data-status="idle">
                    <div class="card-body">
                        <div class="worker-card-heading"><span data-name-wrap><i class="bi" data-icon></i><strong data-name></strong></span><span class="worker-id" data-worker-id></span><span class="badge" data-status-badge></span></div>
                        <span class="badge bg-warning text-dark d-none" data-fallback-badge><i class="bi bi-arrow-down-circle me-1"></i>CPU fallback</span>
                        <div class="small text-warning d-none" data-fallback-note><i class="bi bi-exclamation-triangle me-1"></i><span data-fallback-reason></span></div>
                        <div class="worker-current-job" data-current-job>
                            <details class="worker-file" data-file><summary data-title></summary><div class="queue-full-path" data-file-path></div></details>
                            <div class="worker-card-context"><span data-library></span><span class="worker-job-type"><i class="bi" data-kind-icon aria-hidden="true"></i><span data-worker-kind></span></span><button type="button" class="worker-job-link" data-worker-job></button></div>
                        </div>
                        <div class="worker-card-metrics" data-metrics><span data-percent></span><span class="d-none" data-chapter-stage></span><span class="worker-speed"><span class="text-body-secondary">Speed </span><strong data-speed></strong></span><span class="worker-eta"><span class="text-body-secondary">ETA </span><strong data-eta></strong></span></div>
                        <div class="progress" data-progress-wrap><div class="progress-bar" data-progress></div></div>
                    </div>
                </article>`;
            const jobLink = col.querySelector('[data-worker-job]');
            jobLink.addEventListener('click', function () {
                if (jobLink.dataset.jobId) openJobDetails(jobLink.dataset.jobId);
            });
        }
        if (col.parentElement !== host) host.appendChild(col);
        if (!col.querySelector('[data-icon]').classList.contains('me-2')) {
            // Cosmetic: slightly tighter icon spacing.
            col.querySelector('[data-icon]').classList.add('me-2');
        }
        _patchWorkerCard(col, worker);
    }

    // Drop any cards for workers that vanished (a job ending, a pool
    // resize). The legacy code did this implicitly via innerHTML
    // rebuild; here we do it explicitly.
    for (const col of container.querySelectorAll('[data-worker-key]')) {
        if (!seenKeys.has(col.dataset.workerKey)) {
            col.remove();
        }
    }
    refreshWorkerScaleButtons();
}

function _patchWorkerCard(col, worker) {
    const fallbackActive = !!worker.fallback_active;
    const isProcessing = worker.status === 'processing';
    const paused = isProcessing && (processingPaused || worker.paused);
    const card = col.querySelector('[data-card]');
    const icon = col.querySelector('[data-icon]');
    const nameEl = col.querySelector('[data-name]');
    const fallbackBadge = col.querySelector('[data-fallback-badge]');
    const fallbackNote = col.querySelector('[data-fallback-note]');
    const fallbackReason = col.querySelector('[data-fallback-reason]');
    const statusEl = col.querySelector('[data-status-badge]');
    const titleEl = col.querySelector('[data-title]');
    const progressWrap = col.querySelector('[data-progress-wrap]');
    const progress = col.querySelector('[data-progress]');
    const metrics = col.querySelector('[data-metrics]');
    const percent = col.querySelector('[data-percent]');
    const speed = col.querySelector('[data-speed]');
    const eta = col.querySelector('[data-eta]');
    const chapterStage = col.querySelector('[data-chapter-stage]');
    const chapterProgress = isProcessing ? worker.chapter_progress : null;
    // Older running workers only report a phase, so keep their activity indeterminate.
    const isChapterWork = isProcessing && (!!chapterProgress || /^Chapter thumbnails for /i.test(worker.current_phase || ''));
    const loudnessStream = isProcessing && /^Loudness\s+(\d+)\/(\d+)$/i.exec((worker.current_phase || '').trim());
    const isLoudnessWork = isProcessing && (worker.job_kind === 'loudness' || !!loudnessStream);

    // Attribute on the card itself so the .workers-panel-card[data-status]
    // CSS rule can flip the row's accent without re-rendering anything.
    if (card.getAttribute('data-status') !== worker.status) {
        card.setAttribute('data-status', worker.status);
    }
    progressWrap.hidden = !isProcessing;
    metrics.hidden = !isProcessing;
    card.dataset.paused = String(!!paused);
    const workerId = col.querySelector('[data-worker-id]');
    workerId.textContent = `#${worker.worker_id}`;
    workerId.setAttribute('aria-label', `Worker ID ${worker.worker_id}`);
    workerId.title = `Worker ID ${worker.worker_id}`;
    const library = col.querySelector('[data-library]');
    library.textContent = isProcessing ? worker.library_name || '' : '';
    const kind = col.querySelector('[data-worker-kind]');
    kind.textContent = isProcessing && worker.job_kind ? (JOB_KIND_LABELS[worker.job_kind] || worker.job_kind) : '';
    kind.parentElement.hidden = !kind.textContent;
    col.querySelector('[data-kind-icon]').className = 'bi ' +
        (worker.job_kind === 'loudness' ? 'bi-soundwave' : worker.job_kind === 'intro_credits' ? 'bi-skip-forward' : 'bi-film');
    col.querySelector('[data-current-job]').hidden = !isProcessing;
    const jobLink = col.querySelector('[data-worker-job]');
    jobLink.hidden = !isProcessing || !worker.job_id;
    jobLink.dataset.jobId = isProcessing ? worker.job_id || '' : '';
    jobLink.textContent = worker.job_id ? 'Job ' + String(worker.job_id).substring(0, 8) : '';
    jobLink.title = worker.job_id ? 'Open job ' + worker.job_id : '';
    jobLink.setAttribute('aria-label', worker.job_id ? 'Open job ' + worker.job_id : 'Job details');
    const file = col.querySelector('[data-file]');
    file.hidden = !isProcessing;
    const fullPath = worker.current_file || worker.current_title || '';
    const path = col.querySelector('[data-file-path]');
    if (path.textContent !== fullPath) path.textContent = fullPath;

    // Card border (warning ring on fallback)
    card.classList.toggle('border-warning', fallbackActive);

    // Icon (gpu-card vs cpu, fallback flips to cpu)
    const iconClass = (fallbackActive || isChapterWork || isLoudnessWork)
        ? 'bi-cpu'
        : (worker.worker_type === 'GPU' ? 'bi-gpu-card' : 'bi-cpu');
    if (!icon.classList.contains(iconClass)) {
        icon.className = `bi me-2 ${iconClass}`;
    }

    // Name (only update if changed — avoids tearing during text selection)
    if (nameEl.textContent !== worker.worker_name) {
        nameEl.textContent = worker.worker_name;
    }

    // Fallback badge + note
    fallbackBadge.classList.toggle('d-none', !fallbackActive);
    if (fallbackActive) {
        fallbackBadge.title = worker.fallback_reason || 'CPU fallback';
    }
    const showFallbackNote = fallbackActive && !!worker.fallback_reason;
    fallbackNote.classList.toggle('d-none', !showFallbackNote);
    if (showFallbackNote && fallbackReason.textContent !== worker.fallback_reason) {
        fallbackReason.textContent = worker.fallback_reason;
        fallbackNote.title = worker.fallback_reason;
    }

    // Status badge — colour AND text
    const statusColor = paused || worker.retiring ? 'bg-warning text-dark' : (isProcessing ? 'bg-primary' : 'bg-secondary');
    if (statusEl.className !== `badge ${statusColor}`) {
        statusEl.className = `badge ${statusColor}`;
    }
    const workerStatus = paused ? (worker.retiring ? 'Paused · finishing after resume' : 'Paused') : worker.retiring ? 'Finishing current file' : isProcessing ? 'Processing' : 'Idle';
    if (statusEl.textContent !== workerStatus) {
        statusEl.textContent = workerStatus;
    }
    card.classList.toggle('worker-long-status', !!worker.retiring);

    const title = isProcessing ? worker.current_title || worker.current_file || 'Processing…' : '';
    if (titleEl.textContent !== title) titleEl.textContent = title;
    titleEl.title = title;

    const ffmpegStarted = !!worker.ffmpeg_started;
    const chapterTotal = Math.max(0, Number(chapterProgress?.total) || 0);
    const chapterProcessed = Math.min(chapterTotal, Math.max(0, Number(chapterProgress?.processed) || 0));
    const chapterDeterminate = chapterTotal > 0 && !['preparing', 'waiting'].includes(chapterProgress?.stage);
    const progressPercent = isChapterWork
        ? (chapterTotal ? chapterProcessed / chapterTotal * 100 : 0)
        : (isProcessing ? (worker.progress_percent || 0) : 0);
    const indeterminate = isLoudnessWork || isChapterWork && !chapterDeterminate || isProcessing && !ffmpegStarted && !isChapterWork;
    const showProgress = isChapterWork ? chapterDeterminate : isProcessing && ffmpegStarted;
    const desiredWidth = indeterminate ? '35%' : (showProgress ? `${progressPercent.toFixed(1)}%` : '0%');
    if (progress.style.width !== desiredWidth) {
        progress.style.width = desiredWidth;
    }
    progress.classList.toggle('bg-warning', fallbackActive);
    progress.classList.toggle('progress-bar-striped', indeterminate);
    progress.classList.toggle('progress-bar-animated', indeterminate && !paused);
    progress.classList.toggle('worker-indeterminate', indeterminate);
    progress.style.animationPlayState = paused ? 'paused' : '';
    progressWrap.setAttribute('role', 'progressbar');
    progressWrap.setAttribute('aria-label', isLoudnessWork ? 'Loudness analysis' : isChapterWork ? 'Chapter thumbnails' : 'Video previews');
    progressWrap.setAttribute('aria-valuemin', '0');
    progressWrap.setAttribute('aria-valuemax', '100');
    if (indeterminate) progressWrap.removeAttribute('aria-valuenow');
    else progressWrap.setAttribute('aria-valuenow', progressPercent.toFixed(1));

    // Footer — when FFmpeg hasn't started yet, show the live sub-phase
    // string the worker emitted (e.g. "Resolving item id on EmbyTest…",
    // "Reusing cached frames", "Publishing to Plex…") in place of a
    // generic "Working…", so a 30s wait on a slow reverse-lookup
    // actually reads as that, not as a hung worker. Hide speed + ETA
    // chips during this phase — they're meaningless without FFmpeg
    // reporting and would otherwise crowd the phase text.
    //
    // Visual styling:
    // * Resolution / publishing phases — neutral text, default styling.
    //   "Working…" or e.g. "Resolving item id on EmbyTest…".
    // * Reuse phases ("Reusing sibling BIF" / "Reusing cached frames")
    //   — success-coloured + ✓ icon. These are the cases the worker
    //   short-circuits FFmpeg via cache; without distinct styling, a
    //   user watching the row sees a brief phase string flicker by
    //   and assumes "nothing happened" (user-flagged on job
    //   7a9d025b). The green check makes "fast cache hit" obvious.
    const etaWrap = eta.parentElement;
    const _PHASE_REUSE_RE = /(reusing|reused|already exists|skipped)/i;
    chapterStage.classList.toggle('d-none', !isChapterWork || !chapterProgress);
    if (isChapterWork) {
        const stages = {
            preparing: 'Checking chapters', waiting: 'Waiting for chapter access',
            extracting: 'Generating', registering: 'Registering with Plex',
            complete: 'Ready', failed: 'Failed',
        };
        const failures = Math.max(0, Number(chapterProgress?.failed) || 0);
        const stageLabel = stages[chapterProgress?.stage] || 'Processing chapters';
        chapterStage.textContent = `${stageLabel}${failures ? ` · ${failures} failed` : ''}`;
        percent.textContent = chapterDeterminate
            ? `Chapters ${chapterProcessed}/${chapterTotal} · ${progressPercent.toFixed(0)}%`
            : (chapterProgress ? 'Chapter thumbnails' : 'Generating chapters');
        percent.title = chapterDeterminate
            ? 'Chapter images checked or attempted; Plex registration follows.'
            : 'Chapter thumbnail work is active; no chapter count is available yet.';
        percent.classList.remove('text-truncate', 'text-success', 'fw-semibold');
        percent.style.flex = '';
        percent.style.minWidth = '';
        speed.parentElement.style.display = 'none';
        if (etaWrap) etaWrap.style.display = 'none';
    } else if (isLoudnessWork || isProcessing && !ffmpegStarted) {
        const phaseRaw = (worker.current_phase || '').trim();
        const isReusePhase = phaseRaw && _PHASE_REUSE_RE.test(phaseRaw);
        const phaseLabel = loudnessStream ? `Analyzing audio · stream ${loudnessStream[1]}/${loudnessStream[2]}` : phaseRaw || (isLoudnessWork ? 'Analyzing audio…' : 'Working…');
        const phaseDisplay = isReusePhase ? `✓ ${phaseLabel}` : phaseLabel;
        if (percent.textContent !== phaseDisplay) percent.textContent = phaseDisplay;
        if (percent.title !== phaseLabel) percent.title = phaseLabel;
        percent.classList.add('text-truncate');
        percent.style.flex = '1 1 auto';
        percent.style.minWidth = '0';
        // Toggle a success colour on the percent label when we're in a
        // reuse phase. Same Bootstrap utility class the Files panel's
        // "Frames reused" badge uses, so colour semantics stay
        // consistent across the UI.
        percent.classList.toggle('text-success', !!isReusePhase);
        percent.classList.toggle('fw-semibold', !!isReusePhase);
        speed.parentElement.style.display = 'none';
        if (etaWrap) etaWrap.style.display = 'none';
    } else {
        if (percent.title) percent.title = '';
        percent.classList.remove('text-truncate', 'text-success', 'fw-semibold');
        percent.style.flex = '';
        percent.style.minWidth = '';
        speed.parentElement.style.display = '';
        if (etaWrap) etaWrap.style.display = '';
        const percentText = `${progressPercent.toFixed(1)}%`;
        if (percent.textContent !== percentText) percent.textContent = percentText;
        const speedText = paused ? '—' : isProcessing ? (worker.speed || '0.0x') : '—';
        if (speed.textContent !== speedText) speed.textContent = speedText;
        const etaText = paused ? '—' : isProcessing ? (worker.eta || '-') : '-';
        if (eta.textContent !== etaText) eta.textContent = etaText;
    }
}

async function loadWorkerStatuses() {
    try {
        const data = await apiGet('/api/jobs/workers');
        const workers = data.workers || [];

        if (workers.length === 0) {
            if (!jobsLoadedOnce) {
                // First load — avoid flash of config defaults before jobs are fetched.
                updateWorkerStatuses([], { keepBadgeCounts: true });
                return;
            }
            // No pool exists yet (no job has ever run); show config defaults.
            const fallbackCounts = await loadWorkerConfigCounts(false);
            updateWorkerStatuses([], { fallbackCounts });
            return;
        }

        updateWorkerStatuses(workers);
    } catch (error) {
        console.error('Failed to load worker statuses:', error);
        const container = document.getElementById('workerStatusContainer');
        if (container && !error.message.includes('Authentication')) {
            const fallbackCounts = await loadWorkerConfigCounts(false);
            updateWorkerStatuses([], { fallbackCounts });
        }
    }
}


// Browser Notifications
let notificationsEnabled = false;

async function requestNotificationPermission() {
    if (!('Notification' in window)) return;

    if (Notification.permission === 'granted') {
        notificationsEnabled = true;
    } else if (Notification.permission !== 'denied') {
        const permission = await Notification.requestPermission();
        notificationsEnabled = (permission === 'granted');
    }
}

function showNotification(title, body, type = 'info') {
    if (!notificationsEnabled) return;

    const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : type === 'warning' ? '⚠️' : 'ℹ️';

    new Notification(`${icon} ${title}`, {
        body: body,
        icon: '/static/images/icon.png',
        tag: 'plex-preview-generator'
    });
}

// Action Functions
function showNewJobModal() {
    document.getElementById('jobLibraryAll').checked = true;
    MediaScanFilters.reset('job');
    const sortByEl = document.getElementById('jobSortBy');
    if (sortByEl) sortByEl.value = 'default';
    // Back to Previews; the priority is only reset when the last open left it on the Intro & Credits default.
    const wasOwnRunner = _jobKindIsMarkers() || _jobKindIsLoudness();
    const previewsKind = document.getElementById('jobKindPreviews');
    if (previewsKind) previewsKind.checked = true;
    const markersForce = document.getElementById('jobMarkersForce');
    if (markersForce) markersForce.checked = false;
    const findMarkers = document.getElementById('jobMarkersModeFind');
    if (findMarkers) findMarkers.checked = true;
    _showJobKindControls();
    if (wasOwnRunner) document.getElementById('jobPriority').value = '2';

    // Always show all libraries grouped by server. The per-server scope
    // is sent explicitly via the data-server-id attribute the renderer
    // stamps on each library checkbox (see startNewJob's collapsing of
    // ``selectedServerIds`` into a single ``server_id`` field). A
    // separate "Media Server" picker would be redundant — one click
    // instead of two for the common case of "tick a few libraries on
    // one server." Issue #244: the backend used to infer the server
    // from library_ids alone, which silently mis-routed on multi-Plex
    // because Plex assigns library ids per-server starting at "1".
    _renderJobLibraryList(libraries);
    _updateJobScopeBadge();

    const modal = new bootstrap.Modal(document.getElementById('newJobModal'));
    modal.show();
}

function _renderJobLibraryList(libs) {
    const listEl = document.getElementById('jobLibraryList');
    if (!listEl) return;

    const captionEl = document.getElementById('jobLibraryListCaption');
    if (captionEl) {
        const distinctServers = new Set();
        for (const l of libs || []) {
            if (l && l.server_id) distinctServers.add(l.server_id);
        }
        const serverCount = distinctServers.size;
        captionEl.innerHTML = serverCount > 0
            ? `<i class="bi bi-collection me-1"></i>${(libs || []).length} libraries across ${serverCount} server${serverCount === 1 ? '' : 's'}`
            : '';
    }
    if (!libs || libs.length === 0) {
        if (librariesLoadError) {
            listEl.innerHTML =
                '<div class="text-warning small d-flex align-items-start gap-2">' +
                '<i class="bi bi-exclamation-triangle-fill mt-1"></i>' +
                '<span>Can\'t load libraries right now. ' +
                '<a href="/servers" class="text-decoration-none">Check the Servers page</a>.</span>' +
                '</div>';
        } else {
            listEl.innerHTML = '<div class="text-muted small">No libraries available for this selection.</div>';
        }
        return;
    }

    // Group by server so same-named libraries on different servers
    // (e.g. "Movies" on Plex and "Movies" on Emby) show clearly
    // distinct under their vendor header.
    const groups = new Map();
    for (const lib of libs) {
        const key = lib.server_id || '__legacy__';
        if (!groups.has(key)) {
            groups.set(key, {
                server_id: lib.server_id || '',
                server_name: lib.server_name || '',
                server_type: lib.server_type || '',
                libs: [],
            });
        }
        groups.get(key).libs.push(lib);
    }
    const sections = [];
    for (const [_, grp] of groups) {
        const stype = (grp.server_type || '').toLowerCase();
        const logo = _vendorLogo(stype, 14) || '';
        const head = `<div class="text-muted small mt-2 mb-1">${logo}<strong>${escapeHtml(grp.server_name || stype.toUpperCase() || 'Server')}</strong></div>`;
        const rows = grp.libs.map(lib => `
            <div class="form-check ms-2">
                <input class="form-check-input job-library-checkbox" type="checkbox"
                       value="${lib.id}" id="jobLib_${lib.id}"
                       data-server-id="${escapeHtml(lib.server_id || '')}"
                       data-server-name="${escapeHtml(lib.server_name || '')}"
                       data-library-kind="${escapeHtml(lib.type || lib.kind || '')}" disabled>
                <label class="form-check-label" for="jobLib_${lib.id}">
                    ${escapeHtml(lib.name)} <span class="text-muted small">(${libraryTypeLabel(lib)})</span>
                </label>
            </div>
        `).join('');
        sections.push(head + rows);
    }
    listEl.innerHTML = sections.join('');

    // Refresh the scope badge whenever any library tick changes, so the
    // user always knows which server(s) the submit will target.
    document.querySelectorAll('.job-library-checkbox').forEach(cb => {
        cb.addEventListener('change', _updateJobScopeBadge);
    });
}

// Compute the computed-scope preview (one server vs. fan-out) from the
// current tick state — same per-checkbox data-server-id values that
// startNewJob collapses into the request's ``server_id`` field. This is
// the authoritative scope computation; the backend's
// ``_infer_server_from_library_ids`` is a refusing fallback for clients
// that don't send server_id (see issue #244).
function _updateJobScopeBadge() {
    MediaScanFilters.refresh('job');
    const badge = document.getElementById('jobScopeBadge');
    if (!badge) return;

    const allCb = document.getElementById('jobLibraryAll');
    if (allCb && allCb.checked) {
        badge.innerHTML =
            '<span class="badge bg-secondary-subtle text-secondary-emphasis border">'
            + (_jobKindIsMarkers()
                ? '<i class="bi bi-globe2 me-1"></i>Checking every library with Intro &amp; Credits turned on'
                : _jobKindIsLoudness()
                    ? '<i class="bi bi-globe2 me-1"></i>Checking every library with Loudness turned on'
                    : '<i class="bi bi-globe2 me-1"></i>Scanning every enabled library across all servers')
            + '</span>';
        return;
    }

    const ticked = Array.from(document.querySelectorAll('.job-library-checkbox:checked'));
    if (ticked.length === 0) {
        badge.innerHTML = '';
        return;
    }
    const serverNames = new Set();
    let singleServerName = '';
    for (const cb of ticked) {
        const sid = cb.dataset.serverId || '';
        if (!sid) continue;
        serverNames.add(sid);
        singleServerName = cb.dataset.serverName || sid;
    }
    const verb = _jobKindIsMarkers() || _jobKindIsLoudness() ? 'Checking' : 'Scanning';
    if (serverNames.size === 1) {
        badge.innerHTML =
            '<span class="badge bg-success-subtle text-success-emphasis border">'
            + `<i class="bi bi-bullseye me-1"></i>${verb} → <strong>${escapeHtml(singleServerName)}</strong> only`
            + '</span>';
    } else if (serverNames.size > 1) {
        badge.innerHTML =
            '<span class="badge bg-info-subtle text-info-emphasis border">'
            + `<i class="bi bi-diagram-3 me-1"></i>${verb} → <strong>${serverNames.size} servers</strong> (cross-server fan-out)`
            + '</span>';
    } else {
        badge.innerHTML = '';
    }
}

function _jobKindIsMarkers() {
    const radio = document.getElementById('jobKindMarkers');
    return !!(radio && radio.checked);
}

function _jobKindIsLoudness() {
    const radio = document.getElementById('jobKindLoudness');
    return !!(radio && radio.checked);
}

// Intro & Credits · Check servers reads back every server, so it has no libraries and no "re-check" switch.
function _jobChecksServers() {
    const radio = document.getElementById('jobMarkersModeCheckServers');
    return _jobKindIsMarkers() && !!(radio && radio.checked);
}

// Intro & Credits has no processing mode or order (every file is checked, season by season), a "re-check" switch
// instead.
function _showJobKindControls() {
    const markers = _jobKindIsMarkers();
    // Plex loudness, like Intro & Credits, checks every file of the chosen libraries (files already done are skipped).
    const ownRunner = markers || _jobKindIsLoudness();
    const checksServers = _jobChecksServers();
    const toggle = function (id, hidden) {
        const el = document.getElementById(id);
        if (el) el.hidden = hidden;
    };
    toggle('jobMarkersModeGroup', !markers);
    toggle('jobLibrariesGroup', checksServers);
    toggle('jobProcessingModeGroup', ownRunner);
    toggle('jobSortByGroup', ownRunner);
    toggle('jobMarkersForceGroup', !markers || checksServers);
    toggle('jobScanFiltersGroup', ownRunner);
    toggle('jobOwnRunnerFiltersNote', !ownRunner || checksServers);
    _updateJobScopeBadge();
}

// Picking a job type also picks its default priority: Intro & Credits runs at Low, previews at Normal.
function onJobKindChange() {
    _showJobKindControls();
    const priority = document.getElementById('jobPriority');
    if (priority) priority.value = _jobKindIsMarkers() || _jobKindIsLoudness() ? '3' : '2';
}

function toggleAllLibraries(checkbox) {
    const libraryCheckboxes = document.querySelectorAll('.job-library-checkbox');
    libraryCheckboxes.forEach(cb => {
        cb.disabled = checkbox.checked;
        if (checkbox.checked) {
            cb.checked = false;
        }
    });
    _updateJobScopeBadge();
}

// Phase H6: Select-all / None affordance for the multi-select picker.
function setAllLibrariesChecked(checked) {
    const allCb = document.getElementById('jobLibraryAll');
    if (allCb && allCb.checked) {
        allCb.checked = false;
        toggleAllLibraries(allCb);
    }
    document.querySelectorAll('.job-library-checkbox').forEach(cb => {
        cb.disabled = false;
        cb.checked = checked;
    });
    _updateJobScopeBadge();
}

// Job title from the picked library names: one name, a short list, or a count when the list gets long.
function _jobLibraryLabel(names, pickedCount) {
    if (pickedCount === 1) return names[0] || 'Selected Library';
    if (names.length !== pickedCount) return `${pickedCount} Libraries`;
    const joined = names.join(', ');
    return joined.length <= 60 ? joined : `${names.slice(0, 2).join(', ')} + ${names.length - 2} more`;
}

async function _submitNewJob(url, payload, successMessage) {
    const opening = modalOpening(document.getElementById('newJobModal'));
    // Retry once on transient network errors ("Failed to fetch" from
    // server congestion).
    let lastError;
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            await apiPost(url, payload);

            hideModalSafely(document.getElementById('newJobModal'), opening);
            loadJobs();
            loadJobStats();
            showToast('Job Started', successMessage, 'success');
            return;  // success — exit
        } catch (error) {
            lastError = error;
            if (attempt === 0 && error.message === 'Failed to fetch') {
                // Brief pause before retry
                await new Promise(r => setTimeout(r, 500));
                continue;
            }
            break;
        }
    }
    showToast('Error', 'Failed to start job: ' + lastError.message, 'danger');
}

// Intro & Credits: libraries go as server + library pairs, because library ids repeat across servers (Plex numbers
// its libraries from "1" on every server). An empty list means every library with Intro & Credits turned on.
async function _startMarkersJob() {
    const allTicked = document.getElementById('jobLibraryAll').checked;
    const ticked = allTicked ? [] : Array.from(document.querySelectorAll('.job-library-checkbox:checked'));
    if (!allTicked && ticked.length === 0) {
        showToast('Error', 'Please select at least one library', 'warning');
        return;
    }
    const picked = ticked.map(cb => ({ server_id: cb.dataset.serverId || '', library_id: cb.value }));
    const names = picked
        .map(p => (libraries.find(l => String(l.id) === p.library_id && (l.server_id || '') === p.server_id) || {}).name)
        .filter(Boolean);
    const label = allTicked ? 'All Libraries' : _jobLibraryLabel(names, picked.length);
    const payload = {
        libraries: picked,
        priority: parseInt(document.getElementById('jobPriority').value, 10) || 3,
        force: document.getElementById('jobMarkersForce').checked,
        library_name: `Intro & Credits: ${label}`,
    };
    await _submitNewJob('/api/markers/jobs', payload, 'Intro & Credits job has been started');
}

// Plex loudness: libraries go as server + library pairs like Intro & Credits; an empty list means every library with
// Loudness turned on. Libraries without it are skipped by the server (named in the job's warning).
async function _startLoudnessJob() {
    const allTicked = document.getElementById('jobLibraryAll').checked;
    const ticked = allTicked ? [] : Array.from(document.querySelectorAll('.job-library-checkbox:checked'));
    if (!allTicked && ticked.length === 0) {
        showToast('Error', 'Please select at least one library', 'warning');
        return;
    }
    const picked = ticked.map(cb => ({ server_id: cb.dataset.serverId || '', library_id: cb.value }));
    const names = picked
        .map(p => (libraries.find(l => String(l.id) === p.library_id && (l.server_id || '') === p.server_id) || {}).name)
        .filter(Boolean);
    const label = allTicked ? 'All Libraries' : _jobLibraryLabel(names, picked.length);
    const payload = {
        libraries: picked,
        priority: parseInt(document.getElementById('jobPriority').value, 10) || 3,
        library_name: `Plex loudness: ${label}`,
    };
    await _submitNewJob('/api/loudness/jobs', payload, 'Plex loudness job has been started');
}

// The answer to a Check servers request (POST /api/markers/reconcile, or Re-run on a Check servers job): job_id is
// null when Intro & Credits is off everywhere; only one Check servers job is queued or running at a time.
function _showCheckServersAnswer(result) {
    if (!result || !result.job_id) {
        showToast('Nothing to check', (result && result.reason) || 'Intro & Credits is off on every server', 'warning');
        return;
    }
    loadJobs();
    loadJobStats();
    if (result.paused) {
        showToast('Already queued', 'A Check servers job is paused. Resume or cancel it on the dashboard.', 'warning');
        return;
    }
    if (result.already_queued) {
        showToast('Already queued', 'A Check servers job is already queued', 'info');
        return;
    }
    showToast('Job Started', 'Intro & Credits · Check servers has been queued', 'success');
}

async function _startCheckServersJob() {
    const opening = modalOpening(document.getElementById('newJobModal'));
    const payload = { priority: parseInt(document.getElementById('jobPriority').value, 10) || 3 };
    let result;
    try {
        result = await apiPost('/api/markers/reconcile', payload);
    } catch (error) {
        showToast('Error', 'Failed to start job: ' + error.message, 'danger');
        return;
    }
    hideModalSafely(document.getElementById('newJobModal'), opening);
    _showCheckServersAnswer(result);
}

async function startNewJob() {
    if (_jobChecksServers()) {
        await _startCheckServersJob();
        return;
    }
    if (_jobKindIsMarkers()) {
        await _startMarkersJob();
        return;
    }
    if (_jobKindIsLoudness()) {
        await _startLoudnessJob();
        return;
    }
    const allLibrariesCheckbox = document.getElementById('jobLibraryAll');
    const forceRegenerate = document.getElementById('jobRegenerateAll').checked;

    let selectedLibraryIds = [];
    let libraryName = 'All Libraries';
    // Collect the ticked checkboxes' ``data-server-id`` so we can send
    // ``server_id`` explicitly when every tick belongs to one server.
    // Issue #244: relying on the backend's library-id-based inference
    // mis-routes when two Plex servers share a library id (Plex assigns
    // ids per-server starting at "1", so collisions are normal).
    let selectedServerIds = new Set();

    if (!allLibrariesCheckbox.checked) {
        // Get selected library checkboxes
        const selectedCheckboxes = document.querySelectorAll('.job-library-checkbox:checked');
        var selectedIdsLocal = Array.from(selectedCheckboxes).map(cb => cb.value);

        if (selectedIdsLocal.length === 0) {
            showToast('Error', 'Please select at least one library', 'warning');
            return;
        }

        selectedLibraryIds = selectedIdsLocal;
        for (const cb of selectedCheckboxes) {
            const sid = (cb.getAttribute('data-server-id') || '').trim();
            if (sid) selectedServerIds.add(sid);
        }
        // Build a display name from the looked-up library names so the
        // Jobs page shows which libraries were picked, not just a count.
        // Previously multi-library selections collapsed to "3 Libraries"
        // with no way to tell them apart — a user running two scans in
        // a row couldn't distinguish them.
        const pickedNames = selectedIdsLocal
            .map(id => (libraries.find(l => l.id === id) || {}).name)
            .filter(Boolean);
        // Some name lookups can miss (race with stale library cache): the label falls back to a count.
        libraryName = _jobLibraryLabel(pickedNames, selectedIdsLocal.length);
    }

    const priority = parseInt(document.getElementById('jobPriority').value, 10) || 2;
    const sortByEl = document.getElementById('jobSortBy');
    const sortBy = sortByEl ? sortByEl.value : '';

    const scanFilters = MediaScanFilters.read('job');
    if (scanFilters === null) return;
    const jobConfig = { force_generate: forceRegenerate, ...scanFilters };
    if (sortBy) {
        jobConfig.sort_by = sortBy;
    }

    // Explicit ``server_id`` when every ticked library belongs to one
    // server — the UI already knows this from the rendered library
    // groups (data-server-id attribute) and shouldn't make the backend
    // re-derive it from library_ids alone. Multi-server selections
    // stay unpinned so the cross-server fan-out path still works.
    const jobPayload = {
        library_ids: selectedLibraryIds,
        library_name: libraryName,
        priority: priority,
        config: jobConfig,
    };
    if (selectedServerIds.size === 1) {
        jobPayload.server_id = Array.from(selectedServerIds)[0];
    }

    await _submitNewJob('/api/jobs', jobPayload, 'Processing job has been started');
}

async function cancelJob(jobId) {
    if (!await appConfirm('Cancel this job? Any in-flight FFmpeg work will be killed.', { title: 'Cancel job', confirmText: 'Cancel job', cancelText: 'Keep running' })) return;

    try {
        await apiPost(`/api/jobs/${jobId}/cancel`);
        loadJobs();
        loadJobStats();
    } catch (error) {
        showToast('Error', 'Failed to cancel job: ' + error.message, 'danger');
    }
}

async function pauseJob(jobId) {
    try {
        await apiPost(`/api/jobs/${jobId}/pause`);
        await loadJobs({ force: true });
        showToast('Paused', 'Job pause requested. Other pause settings still apply.', 'warning');
    } catch (error) {
        showToast('Error', 'Failed to pause job: ' + error.message, 'danger');
    }
}

async function resumeJob(jobId) {
    try {
        const result = await apiPost(`/api/jobs/${jobId}/resume`);
        await loadJobs({ force: true });
        if (result && result.processing_paused === true) {
            showToast('Manual Pause Cleared', 'Global processing remains paused. Its other pause settings still apply.', 'warning');
        } else if (result && result.paused) {
            showToast('Manual Pause Cleared', 'This job is still paused by its schedule. It can continue at the next scheduled start.', 'warning');
        } else {
            showToast('Resumed', 'Job resumed', 'success');
        }
    } catch (error) {
        showToast('Error', 'Failed to resume job: ' + error.message, 'danger');
    }
}

async function scaleWorkers(jobId, workerType, delta) {
    const endpoint = delta > 0 ? 'add' : 'remove';
    const count = Math.abs(delta);

    try {
        const result = await apiPost(`/api/jobs/${jobId}/workers/${endpoint}`, {
            worker_type: workerType,
            count
        });
        await Promise.all([loadJobs(), loadWorkerStatuses(), refreshStatus()]);
        if (endpoint === 'add') {
            showToast('Workers Updated', `Added ${result.added} ${workerType} worker(s)`, 'success');
        } else {
            const scheduledRemoval = result.scheduled_removal || 0;
            const unavailable = result.unavailable || 0;
            if (scheduledRemoval > 0 || unavailable > 0) {
                showToast(
                    'Workers Updated',
                    `Removed ${result.removed} ${workerType}; ${scheduledRemoval} scheduled after current tasks; ${unavailable} unavailable`,
                    'warning'
                );
            } else {
                showToast('Workers Updated', `Removed ${result.removed} ${workerType} worker(s)`, 'info');
            }
        }
    } catch (error) {
        showToast('Error', `Failed to ${endpoint} ${workerType} worker(s): ${error.message}`, 'danger');
    }
}

function refreshWorkerScaleButtons() {
    const buttons = document.querySelectorAll('.worker-scale-btn');
    buttons.forEach((btn) => {
        const direction = parseInt(btn.getAttribute('data-direction'), 10);
        const workerType = btn.getAttribute('data-worker-type');
        if (direction === 1) {
            btn.disabled = getWorkerCountForType(workerType) >= getWorkerMaxForType(workerType);
            return;
        }
        btn.disabled = getWorkerCountForType(workerType) <= 0;
    });
}

function getWorkerCountForType(workerType) {
    if (workerType !== 'CPU') return 0;
    const el = document.getElementById('cpuWorkers');
    if (!el) return 0;
    const n = parseInt(el.textContent, 10);
    return Number.isNaN(n) ? 0 : n;
}

function getWorkerMaxForType(workerType) {
    if (workerType !== 'CPU') return Infinity;
    return cachedWorkerConfigCounts?.cpu_threads_max ?? Infinity;
}

function settingsKeyForWorkerType(workerType) {
    if (workerType === 'CPU') return 'cpu_threads';
    return null;
}

async function scaleWorkersGlobal(workerType, direction) {
    const currentCount = getWorkerCountForType(workerType);
    const maxCount = getWorkerMaxForType(workerType);
    let newCount = Math.max(0, currentCount + direction);
    // A count saved above the maximum (before the cap existed) steps down to the maximum first.
    if (direction < 0) newCount = Math.min(newCount, maxCount);
    if (newCount === currentCount || (direction > 0 && newCount > maxCount)) return;

    const settingsKey = settingsKeyForWorkerType(workerType);

    try {
        const saveResult = await apiPost('/api/settings', { [settingsKey]: newCount });
        cachedWorkerConfigCounts = null;
        await loadWorkerConfigCounts(true);

        const badgeEl = workerType === 'CPU' ? document.getElementById('cpuWorkers') : null;
        if (badgeEl) badgeEl.textContent = String(newCount);
        refreshWorkerScaleButtons();

        // The settings save resizes the live pool itself; a follow-up
        // /api/workers/add|remove call would change a second worker.
        await Promise.all([loadJobs(), loadWorkerStatuses(), refreshStatus()]);
        const retiring = saveResult.cpu_workers_retiring || 0;
        let busyNote = '';
        if (retiring === 1) {
            busyNote = '1 busy worker will stop after its current file.';
        } else if (retiring > 1) {
            busyNote = `${retiring} busy workers will stop after their current files.`;
        }
        if (saveResult.warning) {
            showToast('Warning', busyNote ? `${saveResult.warning} ${busyNote}` : saveResult.warning, 'warning');
        } else if (busyNote) {
            showToast('Setting Saved', `${workerType} workers set to ${newCount}. ${busyNote}`, 'success');
        } else {
            showToast('Setting Saved', `${workerType} workers set to ${newCount}`, 'success');
        }
    } catch (error) {
        const badgeEl = workerType === 'CPU' ? document.getElementById('cpuWorkers') : null;
        if (badgeEl) badgeEl.textContent = String(currentCount);
        refreshWorkerScaleButtons();
        showToast('Error', `Failed to update ${workerType} workers: ${error.message}`, 'danger');
    }
}

async function deleteJob(jobId) {
    if (!await appConfirm('Delete this job from the history? Logs and per-file results will also be removed.', { title: 'Delete job', confirmText: 'Delete' })) return;

    try {
        await apiDelete(`/api/jobs/${jobId}`);
        loadJobs();
        loadJobStats();
    } catch (error) {
        showToast('Error', 'Failed to delete job: ' + error.message, 'danger');
    }
}

async function reprocessJob(jobId) {
    try {
        const job = await apiPost(`/api/jobs/${jobId}/reprocess`);
        if (job && 'job_id' in job) {
            _showCheckServersAnswer(job);
            return;
        }
        loadJobs();
        loadJobStats();
        showToast('Reprocess Started', `Job ${job.id.substring(0, 8)} created`, 'success');
    } catch (error) {
        const msg = error.message || '';
        if (msg.includes('409') || msg.includes('running or pending')) {
            showToast('Cannot reprocess', 'Job is still running or pending', 'warning');
        } else {
            showToast('Error', 'Failed to reprocess job: ' + msg, 'danger');
        }
    }
}

async function clearCompletedJobs() {
    if (!await appConfirm('Clear all completed, failed, and cancelled jobs from the history?', { title: 'Clear job history', confirmText: 'Clear' })) return;

    try {
        const result = await apiPost('/api/jobs/clear');
        loadJobs();
        loadJobStats();
        showToast('Jobs Cleared', `Cleared ${result.cleared} jobs`, 'info');
    } catch (error) {
        showToast('Error', 'Failed to clear jobs: ' + error.message, 'danger');
    }
}

async function clearJobsByStatus() {
    const checkboxes = document.querySelectorAll('.clear-status-cb:checked');
    const statuses = Array.from(checkboxes).map(cb => cb.value);

    if (statuses.length === 0) {
        showToast('Warning', 'Select at least one status to clear', 'warning');
        return;
    }

    const labels = statuses.join(', ');
    if (!await appConfirm(`Clear all ${labels} jobs from the history?`, { title: 'Clear jobs', confirmText: 'Clear' })) return;

    try {
        const result = await apiPost('/api/jobs/clear', { statuses });
        loadJobs();
        loadJobStats();
        showToast('Jobs Cleared', `Cleared ${result.cleared} ${labels} jobs`, 'info');
    } catch (error) {
        showToast('Error', 'Failed to clear jobs: ' + error.message, 'danger');
    }
}

// Helper Functions
function _buildOutcomeTooltip(outcome) {
    if (!outcome || typeof outcome !== 'object') return '';
    // D14 — pull labels from the unified STATUS_META so the tooltip
    // matches the file-outcome chip and the per-server pill.
    var keys = ['generated', 'skipped_bif_exists', 'skipped_not_indexed',
                'markers_published', 'markers_up_to_date', 'markers_waiting',
                'markers_skipped', 'markers_none', 'markers_no_owners',
                'skipped_file_not_found', 'skipped_source_gone', 'skipped_excluded',
                'skipped_invalid_hash', 'failed', 'no_media_parts'];
    var lines = [];
    for (var i = 0; i < keys.length; i++) {
        var count = outcome[keys[i]];
        if (count && count > 0) {
            lines.push(_statusMeta(keys[i]).label + ': ' + count.toLocaleString());
        }
    }
    return lines.length > 0 ? lines.join('&#10;') : 'No items processed';
}

function getStatusBadge(status, paused, error, outcome, pauseNote) {
    if (paused === undefined) paused = false;
    if (error === undefined) error = null;
    if (outcome === undefined) outcome = null;

    var tooltipText = _buildOutcomeTooltip(outcome);
    if (error) {
        // The error can quote a file name or a server's words, so it's escaped for the title attribute.
        var errorText = escapeHtmlAttr(error);
        tooltipText = tooltipText
            ? tooltipText + '&#10;' + errorText
            : errorText;
    }
    if (pauseNote && status === 'running' && paused) {
        tooltipText = escapeHtmlAttr(pauseNote) + (tooltipText ? '&#10;' + tooltipText : '');
    }
    var tooltipAttrs = tooltipText
        ? ' data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="false" title="' + tooltipText + '"'
        : '';

    // Modern status indicator: a coloured dot + neutral label reads
    // cleaner than a stack of full-fill pills in a dense table. The
    // CSS lives under .status-dot in style.css.
    function dot(cls, label) {
        return '<span class="status-dot ' + cls + '"' + tooltipAttrs + '>' + label + '</span>';
    }

    if ((status === 'running' || status === 'pending') && paused) return dot('status-warning', 'Paused');
    if (status === 'completed' && error) return dot('status-warning', 'Completed with warnings');

    var clsMap = {
        'pending':   'status-pending',
        'running':   'status-running',
        'completed': 'status-completed',
        'failed':    'status-failed',
        'cancelled': 'status-cancelled'
    };
    var labelMap = {
        'pending':   'Pending',
        'running':   'Running',
        'completed': 'Completed',
        'failed':    'Failed',
        'cancelled': 'Cancelled'
    };
    return dot(clsMap[status] || 'status-pending', labelMap[status] || status);
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString();
}

// Relative time with absolute date as a tooltip. The dashboard's
// "Created" column used to show an awkward "5/2/2026, 6:01:38 PM"
// that was hard to scan at a glance — relative reads in 1 word
// ("2 min ago") and the absolute is one hover away. Falls back to
// a short locale string for anything older than a week.
function formatRelativeTime(dateStr) {
    if (!dateStr) return '<span class="text-muted">—</span>';
    const date = new Date(dateStr);
    const now = Date.now();
    const diffSec = Math.round((now - date.getTime()) / 1000);
    let label;
    if (diffSec < 5) label = 'just now';
    else if (diffSec < 60) label = `${diffSec}s ago`;
    else if (diffSec < 3600) label = `${Math.round(diffSec / 60)} min ago`;
    else if (diffSec < 86400) label = `${Math.round(diffSec / 3600)} h ago`;
    else if (diffSec < 86400 * 7) label = `${Math.round(diffSec / 86400)} d ago`;
    else label = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const abs = date.toLocaleString();
    return `<span class="text-nowrap" title="${escapeHtml(abs)}">${label}</span>`;
}

// A delayed Intro & Credits verify job checks replaced files again; it isn't a retry of a failure.
function _isMarkersVerifyJob(job) {
    return _isMarkersJob(job) && !!(job.config && job.config.verify);
}

// The words before a retry-wait countdown: "Checking again" for a verify job, the retry wording otherwise.
function _countdownWord(job, retryWord) {
    return _isMarkersVerifyJob(job) ? 'Checking again' : retryWord;
}

// Retry countdowns: "in 45 s" below 90 seconds, then rounded minutes ("in 5 min").
function _formatCountdown(seconds) {
    return seconds >= 90 ? `in ${Math.round(seconds / 60)} min` : `in ${seconds} s`;
}

function formatElapsed(startDateStr) {
    if (!startDateStr) return '';
    const elapsed = Math.max(0, Math.floor((Date.now() - new Date(startDateStr).getTime()) / 1000));
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60);
    const s = elapsed % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function _updateElapsedTimers() {
    document.querySelectorAll('[data-elapsed-since]').forEach(function (el) {
        el.textContent = formatElapsed(el.getAttribute('data-elapsed-since'));
    });
    document.querySelectorAll('[data-scheduled-at]').forEach(function (el) {
        var scheduled = new Date(el.getAttribute('data-scheduled-at')).getTime();
        var remaining = Math.max(0, Math.ceil((scheduled - Date.now()) / 1000));
        if (remaining > 0) {
            var word = el.getAttribute('data-countdown-label') || 'Retry starting';
            el.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>' + escapeHtml(word) + ' ' + _formatCountdown(remaining);
        } else {
            el.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Starting...';
        }
    });
    // Webhook debounce countdown — same shape as the retry countdown
    // above, different label ("Webhook firing in N s") and a separate
    // data attribute so the row knows which copy to show. Lives on the
    // same 1Hz tick interval so we don't multiply timers.
    document.querySelectorAll('[data-webhook-fire-at]').forEach(function (el) {
        var fireAt = new Date(el.getAttribute('data-webhook-fire-at')).getTime();
        var remaining = Math.max(0, Math.ceil((fireAt - Date.now()) / 1000));
        if (remaining > 0) {
            el.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Webhook firing in ' + remaining + 's';
        } else {
            el.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Firing...';
        }
    });
    // Active-jobs retry countdown bars (data-retry-eta on the .progress
    // wrapper). Tick the inner bar's width + label in lockstep so the
    // bar visibly fills as the wait elapses, rather than sitting at 0%
    // until the retry actually fires.
    document.querySelectorAll('.retry-countdown-bar[data-retry-eta]').forEach(function (wrap) {
        var eta = new Date(wrap.getAttribute('data-retry-eta')).getTime();
        var total = parseInt(wrap.getAttribute('data-retry-wait-total') || '30', 10);
        var remaining = Math.max(0, Math.ceil((eta - Date.now()) / 1000));
        var fill = Math.max(0, Math.min(100, ((total - remaining) / total) * 100));
        var bar = wrap.querySelector('.progress-bar');
        if (bar) {
            bar.style.width = fill.toFixed(1) + '%';
            var label = bar.querySelector('.retry-countdown-label');
            if (label) {
                var word = wrap.getAttribute('data-countdown-label') || 'Next attempt';
                label.textContent = remaining > 0 ? word + ' ' + _formatCountdown(remaining) : 'Starting…';
            }
        }
    });
}

function _ensureElapsedTimer() {
    if (_elapsedTimerInterval) return;
    _elapsedTimerInterval = setInterval(_updateElapsedTimers, 1000);
}

function _stopElapsedTimer() {
    if (!_elapsedTimerInterval) return;
    if (document.querySelector('[data-scheduled-at]')) return;
    if (document.querySelector('[data-webhook-fire-at]')) return;
    if (document.querySelector('.retry-countdown-bar[data-retry-eta]')) return;
    clearInterval(_elapsedTimerInterval);
    _elapsedTimerInterval = null;
}

/**
 * Bootstrap-modal replacement for the native browser ``confirm()``.
 *
 * Returns a Promise that resolves to ``true`` when the user clicks the
 * confirm button and ``false`` when they cancel/dismiss the dialog
 * (clicking outside, pressing Escape, or hitting the X). Same await-able
 * shape as the OS confirm so call sites stay simple:
 *
 *     if (!await appConfirm('Cancel this job?')) return;
 *
 * Options:
 *   - title (string, default "Confirm")
 *   - confirmText (string, default "Confirm") — primary button label
 *   - cancelText (string, default "Cancel") — secondary button label
 *   - variant ("danger" | "warning" | "primary", default "danger") —
 *     drives the primary button colour + icon. Most callers are
 *     destructive (cancel/delete/clear/restore) so danger is the
 *     default.
 *
 * The shared modal lives in base.html so every page gets it without
 * having to reimport partials.
 */
function appConfirm(message, opts = {}) {
    const modalEl = document.getElementById('appConfirmModal');
    if (!modalEl || !window.bootstrap) {
        // Defensive fallback — if Bootstrap isn't loaded yet (unlikely
        // outside the very first paint) we'd otherwise hang forever
        // waiting for a never-fired modal event. Reverting to native
        // confirm in that edge case is annoying but at least functional.
        return Promise.resolve(window.confirm(message));
    }
    const titleEl = document.getElementById('appConfirmModalTitleText');
    const iconEl = document.getElementById('appConfirmModalIcon');
    const bodyEl = document.getElementById('appConfirmModalBody');
    const okBtn = document.getElementById('appConfirmModalOkBtn');
    const cancelBtn = document.getElementById('appConfirmModalCancelBtn');

    titleEl.textContent = opts.title || 'Confirm';
    bodyEl.textContent = message;
    cancelBtn.textContent = opts.cancelText || 'Cancel';
    okBtn.textContent = opts.confirmText || 'Confirm';

    const variant = opts.variant || 'danger';
    okBtn.className = `btn btn-${variant}`;
    const iconMap = { danger: 'bi-exclamation-triangle text-danger', warning: 'bi-exclamation-triangle text-warning', primary: 'bi-question-circle text-primary' };
    iconEl.className = `bi ${iconMap[variant] || iconMap.danger}`;

    const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);

    return new Promise((resolve) => {
        let resolved = false;
        const onOk = () => {
            resolved = true;
            modal.hide();
            // Resolve AFTER hide so callers running follow-up DOM work
            // (e.g. removing a row) don't fight the modal's own teardown.
            resolve(true);
        };
        const onHidden = () => {
            okBtn.removeEventListener('click', onOk);
            modalEl.removeEventListener('hidden.bs.modal', onHidden);
            if (!resolved) resolve(false);
        };
        okBtn.addEventListener('click', onOk);
        modalEl.addEventListener('hidden.bs.modal', onHidden);
        modal.show();
    });
}

// Bootstrap ignores hide() while a dialog is still opening (the backdrop fades in, then the dialog), so hiding one on
// a quick answer — a save that returns within that ~0.5 s — left it on screen over finished work. Dialogs are tracked
// from their own events, which bubble to the document: still opening, open (from show until hide), and how many times
// each has opened, so a late answer can tell whether the dialog it was for is still the one on screen.
const _openingModals = new WeakSet();
const _openModals = new WeakSet();
const _modalOpenings = new WeakMap();
document.addEventListener('show.bs.modal', (event) => {
    if (event.defaultPrevented) return;
    _openingModals.add(event.target);
    _openModals.add(event.target);
    _modalOpenings.set(event.target, (_modalOpenings.get(event.target) || 0) + 1);
});
document.addEventListener('shown.bs.modal', (event) => _openingModals.delete(event.target));
document.addEventListener('hide.bs.modal', (event) => {
    if (!event.defaultPrevented) _openModals.delete(event.target);
});
document.addEventListener('hidden.bs.modal', (event) => {
    _openingModals.delete(event.target);
    _openModals.delete(event.target);
});

/**
 * Which opening of a dialog a request starts in, to pass to {@link hideModalSafely} with its answer.
 *
 * @param {HTMLElement|null} modalEl - The modal's element.
 * @returns {{count: number}|null} That opening; null when the dialog isn't open (or missing).
 */
function modalOpening(modalEl) {
    if (!modalEl || !_openModals.has(modalEl)) return null;
    return { count: _modalOpenings.get(modalEl) || 0 };
}

/**
 * Hide a Bootstrap modal, waiting for it to finish opening first when it hasn't yet.
 *
 * Use it wherever a dialog is hidden after an async answer rather than a click, passing the opening the request
 * started in ({@link modalOpening}): an answer that comes after the user closed that dialog (and maybe opened it
 * again) leaves it alone.
 *
 * @param {HTMLElement|null} modalEl - The modal's element; nothing happens when it's missing.
 * @param {{count: number}|null} [opening] - The opening the request started in; omitted: hide whatever is open.
 */
function hideModalSafely(modalEl, opening) {
    if (!modalEl || !window.bootstrap) return;
    if (opening !== undefined) {
        const sameOpening = opening && opening.count === (_modalOpenings.get(modalEl) || 0);
        if (!sameOpening || !_openModals.has(modalEl)) return;
    }
    const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
    if (_openingModals.has(modalEl)) {
        modalEl.addEventListener('shown.bs.modal', () => modal.hide(), { once: true });
    } else {
        modal.hide();
    }
}

function showToast(title, message, type = 'info') {
    const toast = document.getElementById('toastNotification');
    const toastTitle = document.getElementById('toastTitle');
    const toastBody = document.getElementById('toastBody');
    const toastIcon = document.getElementById('toastIcon');

    toastTitle.textContent = title;
    toastBody.textContent = message;

    // Update icon based on type
    const icons = {
        'success': 'bi-check-circle text-success',
        'danger': 'bi-exclamation-circle text-danger',
        'warning': 'bi-exclamation-triangle text-warning',
        'info': 'bi-info-circle text-info'
    };
    toastIcon.className = `bi ${icons[type] || icons.info} me-2`;

    const bsToast = new bootstrap.Toast(toast);
    bsToast.show();
}

// "What's New" changelog popup (runs on every page via base.html)
async function checkWhatsNew() {
    try {
        const resp = await fetch('/api/system/whats-new');
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data.has_new || !data.entries || data.entries.length === 0) return;

        const body = document.getElementById('whatsNewBody');
        if (!body) return;

        let html = '';
        for (const entry of data.entries) {
            const date = entry.date ? new Date(entry.date).toLocaleDateString() : '';
            html += `<div class="mb-4">`;
            html += `<h5 class="d-flex align-items-center gap-2">`;
            html += `<span class="badge bg-primary">${escapeHtml(entry.version)}</span>`;
            html += `<span>${escapeHtml(entry.name)}</span>`;
            if (date) html += `<small class="text-muted ms-auto">${escapeHtml(date)}</small>`;
            html += `</h5>`;
            if (entry.body) {
                html += `<div class="changelog-body text-break">${_renderMarkdownBasic(entry.body)}</div>`;
            }
            if (entry.url) {
                html += `<a href="${escapeHtml(entry.url)}" target="_blank" class="small text-decoration-none">`;
                html += `<i class="bi bi-box-arrow-up-right me-1"></i>View on GitHub</a>`;
            }
            html += `</div>`;
        }
        body.innerHTML = html;

        const modalEl = document.getElementById('whatsNewModal');
        const modal = new bootstrap.Modal(modalEl);
        modal.show();

        modalEl.addEventListener('hidden.bs.modal', async function () {
            try { await fetch('/api/system/whats-new/dismiss', { method: 'POST', headers: { 'X-CSRFToken': getCsrfToken() } }); }
            catch (e) { console.warn('Failed to dismiss what\'s new:', e); }
        }, { once: true });
    } catch (e) {
        console.debug('What\'s new check skipped:', e);
    }
}

function _renderMarkdownBasic(md) {
    // GitHub release bodies arrive with CRLF and frequently use leading
    // whitespace under top-level headings — handle both before any line-anchored
    // regex runs, otherwise headings/lists silently fail to match.
    let html = escapeHtml(md).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    // Headings: tolerate up to 4 leading spaces (CommonMark allows 0–3, GitHub
    // bodies sometimes go further when authors hand-indent under a section).
    html = html.replace(/^[ \t]{0,4}### (.+)$/gm, '<h6 class="mt-3 mb-1">$1</h6>');
    html = html.replace(/^[ \t]{0,4}## (.+)$/gm, '<h5 class="mt-3 mb-1">$1</h5>');
    // Inline code BEFORE bold so a `**foo**` inside `code` doesn't get bolded.
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Markdown links — extremely common in release notes (PRs, issues, docs).
    // Restrict the URL to safe schemes so a hand-crafted body can't drop a
    // javascript:/data: link into the modal.
    html = html.replace(/\[([^\]]+)\]\(((?:https?:|mailto:)[^)\s]+)\)/g, function (_m, text, url) {
        return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
    });
    html = html.replace(/^[ \t]*[*-] (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, function (m) { return '<ul class="mb-2">' + m + '</ul>'; });
    html = html.replace(/\n{2,}/g, '<br><br>');
    html = html.replace(/\n/g, '<br>');
    return html;
}

// checkWhatsNew() is called from the dashboard page (index.html) only,
// to avoid hitting the GitHub API on every page navigation.


// ============================================================================
// J6 — Settings > Backups panel
// ============================================================================
//
// Lists every (live, .bak) pair this app maintains and lets the user one-click
// swap them. The panel only shows on Settings (the trigger is the panel
// element itself; absent on every other page).

function _formatBackupTime(epoch) {
    if (!epoch) return '—';
    const d = new Date(epoch * 1000);
    return d.toLocaleString();
}

function _formatBackupLabel(b) {
    if (b.legacy) return 'Previous version';
    // timestamp is YYYYMMDD-HHMMSS (UTC). Render as local time.
    const ts = b.timestamp || '';
    if (ts.length === 15) {
        const iso = `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}T${ts.slice(9, 11)}:${ts.slice(11, 13)}:${ts.slice(13, 15)}Z`;
        const d = new Date(iso);
        if (!Number.isNaN(d.getTime())) return d.toLocaleString();
    }
    return _formatBackupTime(b.mtime);
}

async function refreshBackupsPanel() {
    const panel = document.getElementById('backupRestorePanel');
    if (!panel) return;
    panel.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Checking for backups…</div>';
    try {
        const data = await apiGet('/api/settings/backups');
        const files = data.files || [];
        if (!files.length) {
            panel.innerHTML = '<div class="text-muted small">No backup-tracked config files found.</div>';
            return;
        }
        // D17 — one row per managed file: header + dropdown + Restore button.
        // Replaces the old vertical-list-of-rows that grew with retention,
        // making the panel feel cluttered for users on installs that save
        // settings often (every webhook → webhook_history.json → +1 row).
        const blocks = files.map((f, idx) => {
            const liveAge = _formatBackupTime(f.live_mtime);
            const headerBadge = f.bak_newer
                ? '<span class="badge bg-warning text-dark ms-2" title="Most recent backup is newer than the live file">backup is newer</span>'
                : (f.has_bak ? '' : '<span class="badge bg-secondary ms-2">no backups yet</span>');

            const backups = f.backups || [];
            const selectId = 'backupSelect-' + idx;
            const selectHtml = backups.length
                ? `
                    <div class="d-flex align-items-center gap-2 mt-2">
                        <select id="${selectId}" class="form-select form-select-sm" style="max-width: 320px;"
                                aria-label="Backup snapshot for ${escapeHtmlAttr(f.name)}">
                            ${backups.map((b) => {
                                const label = escapeHtmlText(_formatBackupLabel(b))
                                    + (b.legacy ? ' (legacy)' : '');
                                return `<option value="${escapeHtmlAttr(b.filename)}">${label}</option>`;
                            }).join('')}
                        </select>
                        <button type="button" class="btn btn-sm btn-outline-warning flex-shrink-0"
                                data-restore-file="${escapeHtmlAttr(f.name)}"
                                data-restore-select="${selectId}">
                            <i class="bi bi-arrow-counterclockwise me-1"></i>Restore selected
                        </button>
                        <span class="text-muted small flex-shrink-0">${backups.length} snapshot${backups.length === 1 ? '' : 's'}</span>
                    </div>
                `
                : '<div class="small text-muted mt-2">No backups yet — they appear after the next save.</div>';

            return `
                <div class="border-bottom py-2">
                    <div>
                        <code>${escapeHtmlText(f.name)}</code>${headerBadge}
                        <div class="small text-muted">Live saved: ${escapeHtmlText(liveAge)}</div>
                    </div>
                    ${selectHtml}
                </div>
            `;
        }).join('');
        panel.innerHTML = blocks;
        panel.querySelectorAll('[data-restore-file]').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                const target = ev.currentTarget;
                const file = target.dataset.restoreFile;
                const select = document.getElementById(target.dataset.restoreSelect);
                const backup = select ? select.value : '';
                if (!file || !backup) return;
                if (!await appConfirm(`Restore ${file} from ${backup}? The current contents will be saved as a fresh backup first, so you can undo this restore.`, { title: 'Restore backup', confirmText: 'Restore', variant: 'warning' })) return;
                target.disabled = true;
                const origHtml = target.innerHTML;
                target.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restoring…';
                try {
                    await apiPost('/api/settings/backups/restore', { file, backup });
                    showToast('Backup restored', `${file} restored from ${backup}. Reload the page for caches to pick it up.`, 'success');
                    refreshBackupsPanel();
                } catch (e) {
                    showToast('Restore failed', (e && e.message) || 'Unknown error', 'danger');
                    target.disabled = false;
                    target.innerHTML = origHtml;
                }
            });
        });
    } catch (e) {
        panel.innerHTML = `<div class="text-warning small"><i class="bi bi-exclamation-triangle me-1"></i>Could not list backups: ${escapeHtmlText((e && e.message) || 'unknown error')}</div>`;
    }
}

// D17 — load + save retention controls. Hooks into the global Settings
// save flow (the SaveSettings button in the settings sidebar already
// POSTs every form input that has a known field name, so just keep the
// inputs in sync with /api/settings on load).
async function _initBackupRetentionControls() {
    const keepInput = document.getElementById('configBackupKeep');
    const ageInput = document.getElementById('configBackupMaxAgeDays');
    if (!keepInput || !ageInput) return;
    try {
        const data = await apiGet('/api/settings');
        const keep = parseInt(data.config_backup_keep, 10);
        const age = parseInt(data.config_backup_max_age_days, 10);
        keepInput.value = Number.isFinite(keep) ? keep : 10;
        ageInput.value = Number.isFinite(age) ? age : 0;
    } catch (e) {
        keepInput.value = 10;
        ageInput.value = 0;
    }
    const persist = async () => {
        const keep = Math.min(100, Math.max(1, parseInt(keepInput.value, 10) || 10));
        const age = Math.min(365, Math.max(0, parseInt(ageInput.value, 10) || 0));
        keepInput.value = keep;
        ageInput.value = age;
        try {
            await apiPost('/api/settings', {
                config_backup_keep: keep,
                config_backup_max_age_days: age,
            });
            showToast('Backups', 'Retention updated', 'success');
        } catch (e) {
            showToast('Save failed', (e && e.message) || 'Unknown error', 'danger');
        }
    };
    keepInput.addEventListener('change', persist);
    ageInput.addEventListener('change', persist);
}

document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('backupRestorePanel')) {
        refreshBackupsPanel();
        const btn = document.getElementById('refreshBackupsBtn');
        if (btn) btn.addEventListener('click', refreshBackupsPanel);
        _initBackupRetentionControls();
    }
    // Centralised Bootstrap tooltip init: every page that lands on the
    // base template gets `[data-bs-toggle="tooltip"]` initialised once
    // on load, so individual pages no longer need their own init blocks.
    // For elements added dynamically after page load, call
    // window._initBootstrapTooltips(scope) — see below.
    _initBootstrapTooltips(document);

    // Config-health banner (issue #278) — runs on every page so a
    // read-only /config or a missing media mount is visible even on the
    // Settings/Schedules pages, not just the dashboard. Polled so it
    // clears once the user fixes permissions (no restart needed).
    if (document.getElementById('configHealthBanner')) {
        checkConfigHealth();
        setInterval(checkConfigHealth, 60000);
    }
});

/**
 * Fetch config-dir + media-mount health and render the global banner.
 * Red (danger) when /config isn't writable — the app can't save anything;
 * yellow (warning) for non-fatal advisories (network fs, low space,
 * missing media mount). Clears when everything is healthy.
 */
async function checkConfigHealth() {
    const banner = document.getElementById('configHealthBanner');
    if (!banner) return;
    // Raw fetch, NOT apiGet: apiGet redirects to /login on 401, which would let
    // this passive background probe hijack navigation (e.g. bounce the setup
    // wizard to login when the config-health endpoint is auth-gated). A health
    // check must never move the user — silently bail on any non-OK response.
    let data;
    try {
        const resp = await fetch('/api/system/config-health');
        if (!resp.ok) return;
        data = await resp.json();
    } catch (e) {
        // Network hiccup — leave the last banner state, never blank the page.
        return;
    }
    const cfg = data.config || {};
    const cards = [];

    if (cfg.writable === false) {
        cards.push(`
            <div class="alert alert-danger d-flex align-items-start mb-2" role="alert">
                <i class="bi bi-x-octagon-fill me-2 mt-1"></i>
                <div>
                    <div class="fw-semibold">${escapeHtml(cfg.detail || "Config folder isn't writable")}</div>
                    <div class="small mt-1">${escapeHtml(cfg.hint || '')}</div>
                    <div class="small mt-1 text-muted">Settings, schedules, and job history can't be saved until this is fixed.</div>
                </div>
            </div>`);
    }
    // Advisories are opinions about the setup, not failures — the app works.
    // Each can be dismissed forever. (The writable=false card above cannot: it
    // is the reason nothing saves.)
    (cfg.warnings || []).forEach((w) => {
        const kind = escapeHtml(w.kind || '');
        cards.push(`
            <div class="alert alert-warning d-flex align-items-start mb-2" role="alert">
                <i class="bi bi-exclamation-triangle-fill me-2 mt-1"></i>
                <div class="small flex-grow-1">${escapeHtml(w.message || '')}</div>
                <button type="button" class="btn-close ms-2" aria-label="Dismiss this advisory"
                        title="Dismiss — don't show this again"
                        data-dismiss-config-warning="${kind}"></button>
            </div>`);
    });
    (data.media_mount_issues || []).forEach((m) => {
        const msg = m.issue === 'empty'
            ? `Media mount "${m.path}" is mounted but EMPTY inside the container (stale bind-mount / unmounted share). Every preview job for files on this disk will fail as "missing on disk".`
            : `Media mount "${m.path}" doesn't exist inside the container — check the volume mount and the path mapping under Settings → Media Servers.`;
        cards.push(`
            <div class="alert alert-warning d-flex align-items-start mb-2" role="alert">
                <i class="bi bi-hdd-network me-2 mt-1"></i>
                <div class="small">${escapeHtml(msg)}</div>
            </div>`);
    });

    if (cards.length === 0) {
        banner.classList.add('d-none');
        banner.innerHTML = '';
        return;
    }
    banner.innerHTML = cards.join('');
    banner.classList.remove('d-none');

    banner.querySelectorAll('[data-dismiss-config-warning]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const kind = btn.getAttribute('data-dismiss-config-warning');
            // Hide immediately — the advisory is cosmetic, so don't make the
            // user wait on the round-trip. The next poll (60s) reconciles.
            btn.closest('.alert')?.remove();
            try {
                await fetch('/api/system/config-health/dismiss', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
                    body: JSON.stringify({ kind }),
                });
            } catch (e) {
                // Persisting failed — it'll simply reappear on the next poll.
                console.warn('Failed to dismiss config-health advisory:', e);
            }
            if (!banner.querySelector('.alert')) {
                banner.classList.add('d-none');
                banner.innerHTML = '';
            }
        });
    });
}
window.checkConfigHealth = checkConfigHealth;

/**
 * App-wide ⓘ rule. Every info icon uses the `.info-icon` class:
 *   - hover shows one short sentence (its `title`);
 *   - when a detail is attached, the hover text ends with "Click for more.", the icon gets `.info-icon-more`
 *     (pointer cursor + chevron) and a click opens the shared #globalInfoModal;
 *   - with no detail, a click does nothing and the cursor stays default.
 *
 * A detail is attached by any of: a `_explanationHtml` DOM property (set by JS), `data-explain-template="<id>"`
 * naming a `<template>`, or a `data-explain-html` attribute. Authors write only the short sentence; the suffix
 * and the class come from `_applyInfoIconAffordance`, so an ⓘ can't say "click" when a click does nothing.
 */
const INFO_ICON_MORE_HINT = 'Click for more.';
// Hand-written variants of the hint from before the helper owned it; stripped so it is never doubled.
const INFO_ICON_HINT_SUFFIX_RE = /\s*(?:[—–-]\s*)?click for (?:more|details)\.?\s*$/i;

function _infoIconDetailHtml(el) {
    if (el._explanationHtml) return el._explanationHtml;
    const tplId = el.dataset.explainTemplate;
    if (tplId) {
        const tpl = document.getElementById(tplId);
        if (tpl && tpl.innerHTML.trim()) return tpl.innerHTML;
    }
    return el.dataset.explainHtml || '';
}

function _infoIconHint(el) {
    // Bootstrap moves `title` into `data-bs-original-title` once a tooltip exists.
    const text = el.getAttribute('data-bs-original-title') ?? el.getAttribute('title') ?? '';
    return text.replace(INFO_ICON_HINT_SUFFIX_RE, '').trim();
}

function _applyInfoIconAffordance(el) {
    const hasDetail = !!_infoIconDetailHtml(el);
    el.classList.toggle('info-icon-more', hasDetail);
    let hint = _infoIconHint(el);
    if (hasDetail) {
        if (hint && !/[.!?]$/.test(hint)) hint += '.';
        hint = hint ? `${hint} ${INFO_ICON_MORE_HINT}` : INFO_ICON_MORE_HINT;
    }
    const attr = el.hasAttribute('data-bs-original-title') ? 'data-bs-original-title' : 'title';
    if ((el.getAttribute(attr) || '') !== hint) el.setAttribute(attr, hint);
}
window._applyInfoIconAffordance = _applyInfoIconAffordance;

/**
 * Initialise Bootstrap tooltips on every `[data-bs-toggle="tooltip"]`
 * element under ``scope`` (defaults to the whole document), after
 * applying the ⓘ rule above to every `.info-icon` there. Safe to
 * call multiple times — Bootstrap's `Tooltip.getInstance(el)` short-
 * circuits if a tooltip already exists for the element.
 *
 * Pages with dynamic content (modals, library refreshes, etc.) should
 * call this after the new DOM lands so the tooltips render.
 */
function _initBootstrapTooltips(scope) {
    const root = scope || document;
    const icons = Array.from(root.querySelectorAll('.info-icon'));
    if (root.classList && root.classList.contains('info-icon')) icons.push(root);
    icons.forEach(_applyInfoIconAffordance);
    if (typeof bootstrap === 'undefined' || !bootstrap.Tooltip) return;
    root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
        if (!bootstrap.Tooltip.getInstance(el)) {
            new bootstrap.Tooltip(el);
        }
    });
}
window._initBootstrapTooltips = _initBootstrapTooltips;

// Call before replacing `scope`'s HTML: a tooltip open on a removed element otherwise stays on screen.
function _disposeBootstrapTooltips(scope) {
    if (!scope || typeof bootstrap === 'undefined' || !bootstrap.Tooltip) return;
    scope.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
        const tip = bootstrap.Tooltip.getInstance(el);
        if (tip) tip.dispose();
    });
}

function _openGlobalInfoModal({ title, html, docsHref }) {
    const modalEl = document.getElementById('globalInfoModal');
    if (!modalEl || typeof bootstrap === 'undefined' || !bootstrap.Modal) return;
    const titleEl = document.getElementById('globalInfoTitle');
    const bodyEl = document.getElementById('globalInfoBody');
    const docsLink = document.getElementById('globalInfoDocsLink');
    if (titleEl) titleEl.textContent = title || 'About this setting';
    // innerHTML is safe here UNDER A STRICT CONTRACT: explanation
    // strings must be built from constant literals only. Never
    // interpolate user / library / path / URL data into this field —
    // tooltip / label fields are text-escaped elsewhere, but
    // `explanation` is not. Sources today: backend _FLAG_METADATA
    // dicts (static prose), <template> elements (authored inline),
    // data-explain-html attrs (authored inline). A future contributor
    // adding `f"<p>Library {lib_name}…</p>"` to an explanation would
    // silently open XSS. Keep it literal.
    if (bodyEl) {
        bodyEl.innerHTML = html
            || '<p class="text-muted">No detailed explanation available.</p>';
    }
    if (docsLink) {
        if (docsHref) {
            docsLink.href = docsHref;
            docsLink.classList.remove('d-none');
        } else {
            docsLink.classList.add('d-none');
        }
    }
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

// `.info-modal-link` is a text link ("See how decisions are made") that opens a detail the same way an ⓘ does.
document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.info-icon, .info-modal-link');
    if (!btn) return;
    // XSS contract: see _openGlobalInfoModal — detail HTML is authored, never built from user data.
    const html = _infoIconDetailHtml(btn);
    if (!html) return;
    ev.preventDefault();
    _openGlobalInfoModal({
        title: btn.dataset.explainTitle || _infoIconHint(btn) || 'About this setting',
        html,
        docsHref: btn.dataset.explainDocs || '',
    });
});
window._openGlobalInfoModal = _openGlobalInfoModal;
// Explicit export so non-app.js scripts (servers.js, schedule_modal.js,
// inline templates) can call ``window.appConfirm(...)`` without relying
// on top-level-script-functions-become-globals semantics.
window.appConfirm = appConfirm;
