// Per-GPU configuration panel — shared between /settings and /setup wizard step 4.
//
// Each detected GPU renders as one card with an enable toggle, Workers count,
// and FFmpeg Threads count. Failed GPUs render disabled with the error inline.
//
// Callers must supply:
//   - <div id="gpuConfigList"></div>          (the panel mounts here)
//   - global function markDirty()              (called when fields change;
//                                               can be a no-op for non-autosave pages)
// Callers read per-GPU state back via collectGpuConfig() which returns the
// array shape expected by /api/settings (gpu_config[]).

// Vendor mark for the small caption under the GPU name. Uses the shared
// helper from app.js (window.MPGShared.gpuVendorLogo) so the dashboard and
// settings/setup panels stay visually in sync. Falls back to escaped text
// (e.g. "ARM", "UNKNOWN") when we don't ship an icon for the vendor.
function _gpuPanelVendorMark(type) {
    const fallback = `${escapeHtml(type || 'UNKNOWN')} &mdash; `;
    return window.MPGShared.gpuVendorLogo(type, 14) || fallback;
}

function renderGpuConfigPanel(detectedGpus, savedConfig) {
    if (document.getElementById('workerGroupSettings')) {
        renderGpuTuningPanel(detectedGpus, savedConfig);
        return;
    }
    const configByDevice = {};
    (savedConfig || []).forEach(c => { if (c.device) configByDevice[c.device] = c; });

    const container = document.getElementById('gpuConfigList');
    container.innerHTML = '';

    detectedGpus.forEach((gpu, idx) => {
        const deviceId = (gpu.device || 'gpu' + idx).replace(/[^a-zA-Z0-9]/g, '_');
        const isFailed = gpu.status === 'failed';

        if (isFailed) {
            const failedToggleId = `gpuEnable_${deviceId}`;
            const card = document.createElement('div');
            card.className = 'card mb-2 border-danger';
            card.style.opacity = '0.85';
            const errorHtml = gpu.error_detail
                ? `<br><span class="text-muted mt-1 d-inline-block">${escapeHtml(gpu.error_detail)}</span>`
                : '';
            card.innerHTML = `
            <div class="card-body py-2 px-3">
                <div class="d-flex flex-column">
                    <div class="d-flex align-items-center mb-1">
                        <div class="form-check form-switch mb-0">
                            <input class="form-check-input" id="${failedToggleId}" type="checkbox" disabled>
                            <label class="form-check-label fw-semibold text-muted" for="${failedToggleId}">
                                ${escapeHtml(gpu.name || 'Unknown GPU')}
                            </label>
                        </div>
                        <span class="badge bg-danger ms-2">failed</span>
                    </div>
                    <small class="text-muted mb-2">${_gpuPanelVendorMark(gpu.type)}${escapeHtml(gpu.device || 'N/A')}</small>
                    <div class="alert alert-danger mb-0 py-2 px-3" style="font-size: 0.85em;">
                        <i class="bi bi-exclamation-triangle-fill me-1"></i>
                        <strong>${escapeHtml(gpu.error || 'Acceleration test failed')}</strong>
                        ${errorHtml}
                        <br><small class="text-muted">Fix the issue and click <strong>Re-scan GPUs</strong> below.</small>
                    </div>
                </div>
            </div>`;
            container.appendChild(card);
            return;
        }

        const saved = configByDevice[gpu.device] || {};
        const enabled = saved.enabled !== undefined ? saved.enabled : true;
        const workers = saved.workers !== undefined ? saved.workers : 1;
        const ffmpegThreads = saved.ffmpeg_threads !== undefined ? saved.ffmpeg_threads : 2;

        // Escape every interpolation that crosses the HTML boundary. GPU
        // names + device paths come from nvidia-smi / lspci output, which is
        // system-controlled but can legitimately contain quotes ("MSI GTX
        // 1080 Ti 11 \"GAMING X\" 11G") that would otherwise break the
        // attributes. Mirrors the failed-GPU branch above.
        const safeName = escapeHtml(gpu.name || 'GPU');
        const safeType = escapeHtml(gpu.type || '');
        const safeDevice = escapeHtml(gpu.device || '');
        const safeDeviceOrNa = escapeHtml(gpu.device || 'N/A');
        const card = document.createElement('div');
        card.className = 'card mb-2';
        card.innerHTML = `
            <div class="card-body py-2 px-3">
                <div class="row align-items-start">
                    <div class="col-md-4 d-flex flex-column justify-content-center" style="min-height: 3.5rem;">
                        <div class="form-check form-switch mb-0">
                            <input class="form-check-input gpu-enable-toggle" type="checkbox"
                                   id="gpuEnable_${deviceId}" data-device="${safeDevice}"
                                   data-gpu-name="${safeName}" data-gpu-type="${safeType}"
                                   ${enabled ? 'checked' : ''}
                                   onchange="markDirty(); toggleGpuRow('${deviceId}')">
                            <label class="form-check-label fw-semibold" for="gpuEnable_${deviceId}">
                                ${safeName}
                            </label>
                        </div>
                        <small class="text-muted">${_gpuPanelVendorMark(gpu.type)}${safeDeviceOrNa}</small>
                    </div>
                    <div class="col-md-4 gpu-settings-${deviceId}" ${enabled ? '' : 'style="opacity:0.5;pointer-events:none"'}>
                        <label class="form-label form-label-sm mb-1" for="gpuWorkers_${deviceId}">Workers
                            <button type="button" class="info-icon ms-1" tabindex="0"
                                    data-bs-toggle="tooltip" data-bs-placement="top"
                                    title="How many files this GPU works on at once. Each one uses GPU memory, so start with 1."><i class="bi bi-info-circle"></i></button>
                        </label>
                        <input type="number" id="gpuWorkers_${deviceId}" class="form-control form-control-sm gpu-workers has-stepper"
                               data-device="${safeDevice}" min="1" max="16"
                               value="${workers}" onchange="onGpuWorkersChange(this, '${deviceId}')">
                    </div>
                    <div class="col-md-4 gpu-settings-${deviceId}" ${enabled ? '' : 'style="opacity:0.5;pointer-events:none"'}>
                        <label class="form-label form-label-sm mb-1" for="gpuFfmpegThreads_${deviceId}">FFmpeg Threads
                            <button type="button" class="info-icon ms-1" tabindex="0"
                                    data-bs-toggle="tooltip" data-bs-placement="top"
                                    title="CPU cores FFmpeg may use per worker for decoding and filtering. Lower values leave more for other workers."><i class="bi bi-info-circle"></i></button>
                        </label>
                        <input type="number" id="gpuFfmpegThreads_${deviceId}" class="form-control form-control-sm gpu-ffmpeg-threads has-stepper"
                               data-device="${safeDevice}" min="0" max="32"
                               value="${ffmpegThreads}" onchange="markDirty()">
                        <small class="text-muted">0 = use all CPU cores &middot; recommended: 2</small>
                    </div>
                </div>
            </div>
        `;
        container.appendChild(card);
    });

    if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips(container);
    // Apply −/+ stepper buttons to the per-GPU Workers + FFmpeg Threads
    // inputs. Safe no-op if the helper isn't loaded (older pages).
    window.MPGShared.attachSteppersTo(container);
}

function toggleGpuRow(deviceId) {
    const toggle = document.getElementById('gpuEnable_' + deviceId);
    const enabled = toggle.checked;
    const els = document.querySelectorAll('.gpu-settings-' + deviceId);
    els.forEach(el => {
        el.style.opacity = enabled ? '1' : '0.5';
        el.style.pointerEvents = enabled ? '' : 'none';
    });
    if (enabled) {
        const row = toggle.closest('.card');
        const workersInput = row.querySelector('.gpu-workers');
        if (workersInput && (parseInt(workersInput.value) || 0) < 1) {
            workersInput.value = 1;
        }
    }
}

function onGpuWorkersChange(input, deviceId) {
    markDirty();
    const val = parseInt(input.value) || 0;
    if (val <= 0) {
        input.value = 0;
        const toggle = document.getElementById('gpuEnable_' + deviceId);
        if (toggle && toggle.checked) {
            toggle.checked = false;
            toggleGpuRow(deviceId);
        }
    }
}

function collectGpuConfig() {
    if (document.getElementById('workerGroupSettings')) {
        return [...document.querySelectorAll('.gpu-tuning-threads')].map(input => ({
            device: input.dataset.device, name: input.dataset.name, type: input.dataset.type,
            ffmpeg_threads: Number(input.value),
        }));
    }
    const config = [];
    document.querySelectorAll('.gpu-enable-toggle').forEach(toggle => {
        const device = toggle.dataset.device;
        const row = toggle.closest('.card');
        const workersInput = row.querySelector('.gpu-workers');
        const ffmpegInput = row.querySelector('.gpu-ffmpeg-threads');
        let enabled = toggle.checked;
        let workers = parseInt(workersInput.value) || 0;
        if (enabled && workers <= 0) {
            enabled = false;
            workers = 0;
        }
        config.push({
            device: device,
            name: toggle.dataset.gpuName || 'GPU',
            type: toggle.dataset.gpuType || '',
            enabled: enabled,
            workers: workers,
            ffmpeg_threads: parseInt(ffmpegInput.value) || 0,
        });
    });
    return config;
}

// Settings page only: worker counts and jobs live in worker groups, so a GPU here has just its FFmpeg thread count.
// Failed GPUs keep their input so the saved value is not dropped from gpu_config.
function renderGpuTuningPanel(detectedGpus, savedConfig) {
    const container = document.getElementById('gpuConfigList');
    const saved = new Map((savedConfig || []).map(gpu => [gpu.device, gpu]));
    const rows = detectedGpus.map((gpu, index) => {
        const config = saved.get(gpu.device) || {};
        const failed = gpu.status === 'failed';
        const failure = failed ? `<div class="hint-line hint-bad"><i class="bi bi-exclamation-triangle-fill" aria-hidden="true"></i><span><strong>${escapeHtml(gpu.error || 'Acceleration test failed')}</strong>${gpu.error_detail ? ' ' + escapeHtml(gpu.error_detail) : ''} Fix the issue and click Re-scan GPUs.</span></div>` : '';
        return `<div class="gpu-row${failed ? ' is-failed' : ''}">
            <span class="gpu-ico" aria-hidden="true"><i class="bi bi-gpu-card"></i></span>
            <div class="gpu-main"><strong class="gpu-name">${escapeHtml(gpu.name || 'GPU')}</strong><div class="gpu-id">${_gpuPanelVendorMark(gpu.type)}${escapeHtml(gpu.device || '')}</div>${failure}</div>
            <div class="gpu-control"><label for="gpuTuning${index}">FFmpeg threads per worker</label>
            <input id="gpuTuning${index}" type="number" min="0" max="32" class="form-control gpu-tuning-threads has-stepper"
                data-device="${escapeHtml(gpu.device)}" data-name="${escapeHtml(gpu.name)}" data-type="${escapeHtml(gpu.type)}" value="${config.ffmpeg_threads ?? 2}"></div></div>`;
    }).join('');
    container.innerHTML = rows + `<div class="hint-line"><i class="bi bi-info-circle" aria-hidden="true"></i><span>CPU threads each GPU worker may use. 0 = no limit. Set worker counts in Worker groups.<button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="About FFmpeg threads" data-explain-title="FFmpeg threads per worker" data-explain-html="The GPU decodes the video, but FFmpeg still uses some CPU threads for filtering and scaling. This caps how many CPU threads each GPU worker uses, so several workers don&#39;t fight over your cores. 0 means no cap: FFmpeg picks its own. Default 2. Raise it only if the GPU sits idle while CPU cores are free. This does not change how many workers run; that is set per group in Worker groups. CPU workers always use as many threads as FFmpeg chooses." aria-label="About FFmpeg threads"><i class="bi bi-info-circle"></i></button></span></div>`;
    window._initBootstrapTooltips?.(container);
    container.querySelectorAll('input').forEach(input => input.addEventListener('change', () => {
        if (typeof markDirty === 'function') markDirty();
    }));
    window.MPGShared.attachSteppersTo(container);
}
