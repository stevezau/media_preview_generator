// =========================================================================
// Schedule create/edit modal + lifecycle actions
//
// All the UI for the modal that opens from the Schedules page when a user
// clicks "Add Schedule" or the edit icon next to a schedule row, plus the
// per-schedule lifecycle endpoints (toggle enabled/disabled, run-now,
// delete). Previously lived inline in app.js (lines 2631-2980).
//
// Functions exported on window:
//   - onScheduleTypeChange / onScanModeChange — radio-group change handlers
//   - showNewScheduleModal / showEditScheduleModal — modal open
//   - saveSchedule — form submit (POST /api/schedules or PUT /<id>)
//   - toggleSchedule, runScheduleNow, deleteSchedule — row actions
//   plus three private helpers: _getSelectedScheduleType, _resetScheduleForm
//
// External dependencies (defined in app.js, available as window globals):
//   showToast, escapeHtml, _renderScheduleLibraryList, updateScheduleList,
//   plus bootstrap.Modal. Loaded AFTER app.js in base.html so those refs
//   resolve.
// =========================================================================

const _SCHEDULE_DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// "Mon to Fri", "Sat, Sun", "every day": consecutive days collapse into a range, in Monday-first order.
function _scheduleDaysLabel(days) {
    const order = [1, 2, 3, 4, 5, 6, 0].filter(day => days.includes(day));
    if (order.length === 7) return 'every day';
    const runs = [];
    for (const day of order) {
        const last = runs[runs.length - 1];
        if (last && (last[last.length - 1] + 1) % 7 === day && day !== 1) last.push(day);
        else runs.push([day]);
    }
    return runs.map(run => run.length > 2
        ? `${_SCHEDULE_DAY_NAMES[run[0]]} to ${_SCHEDULE_DAY_NAMES[run[run.length - 1]]}`
        : run.map(day => _SCHEDULE_DAY_NAMES[day]).join(', ')).join(', ');
}

// One plain-language line for the trigger, so the user can read back what they set. Client-side text only.
function _updateScheduleSummary() {
    const box = document.getElementById('scheduleWhenSummary');
    const type = document.querySelector('input[name="scheduleType"]:checked')?.value;
    if (!box || !type) return;
    let text = '';
    if (type === 'specific-time') {
        const time = document.getElementById('scheduleTime').value;
        const days = Array.from(document.querySelectorAll('.schedule-day:checked')).map(cb => parseInt(cb.value, 10));
        text = time && days.length ? `${_scheduleDaysLabel(days)} at ${time}, container time` : '';
    } else if (type === 'interval') {
        const amount = parseInt(document.getElementById('scheduleIntervalValue').value, 10);
        const unit = document.getElementById('scheduleIntervalUnit').value;
        text = amount >= 1 ? `Every ${amount === 1 ? unit.replace(/s$/, '') : `${amount} ${unit}`}` : '';
    } else {
        const cron = document.getElementById('scheduleCronInput').value.trim();
        text = cron ? `Cron ${cron}, container time` : '';
    }
    box.hidden = !text;
    const label = box.querySelector('span');
    if (label) label.textContent = text;
}

// Inline copy of a failed check beside its field; the toast stays for the flows that read it.
const _SCHEDULE_ERROR_IDS = ['scheduleNameError', 'scheduleLibrariesError', 'scheduleTimeError', 'scheduleDaysError', 'scheduleIntervalError', 'scheduleCronError'];

function _setScheduleFieldError(id, message) {
    const box = document.getElementById(id);
    if (!box) return;
    box.hidden = !message;
    const label = box.querySelector('span');
    if (label) label.textContent = message;
}

function _clearScheduleFieldErrors() {
    _SCHEDULE_ERROR_IDS.forEach(id => _setScheduleFieldError(id, ''));
}

document.getElementById('newScheduleForm')?.addEventListener('input', () => {
    _clearScheduleFieldErrors();
    _updateScheduleSummary();
});
document.getElementById('newScheduleForm')?.addEventListener('change', _updateScheduleSummary);

function onScheduleTypeChange() {
    const selected = document.querySelector('input[name="scheduleType"]:checked').value;
    document.getElementById('scheduleFieldsTime').classList.toggle('d-none', selected !== 'specific-time');
    document.getElementById('scheduleFieldsInterval').classList.toggle('d-none', selected !== 'interval');
    document.getElementById('scheduleFieldsCron').classList.toggle('d-none', selected !== 'cron');
    // D20 — stop_time only makes sense for time-of-day triggers.
    // Hide the field for interval triggers so users aren't confused
    // (and so the value gets cleared in saveSchedule).
    const stopGroup = document.getElementById('scheduleStopTimeGroup');
    if (stopGroup) {
        stopGroup.classList.toggle('d-none', selected === 'interval');
    }
    _updateScheduleSummary();
}

// Intro & Credits · Check servers reads back every server, so the server and library pickers don't apply.
function _scheduleChecksServers() {
    const markers = document.getElementById('scanModeMarkers');
    const checkServers = document.getElementById('scheduleMarkersCheckServers');
    return !!(markers && markers.checked && checkServers && checkServers.checked);
}

function onScanModeChange() {
    const selected = document.querySelector('input[name="scanMode"]:checked').value;
    const markersMode = document.getElementById('scheduleMarkersModeGroup');
    if (markersMode) markersMode.hidden = selected !== 'intro_credits';
    const checksServers = _scheduleChecksServers();
    ['scheduleServerGroup', 'scheduleLibrariesGroup', 'scheduleScopeSection'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.hidden = checksServers;
    });
    const checkNote = document.getElementById('scheduleCheckServersNote');
    if (checkNote) checkNote.hidden = !checksServers;
    const dialog = document.getElementById('newScheduleModal');
    if (dialog) dialog.dataset.kind = selected === 'intro_credits' ? (checksServers ? 'check' : 'intro_credits') : 'previews';
    const lookbackGroup = document.getElementById('scheduleLookbackGroup');
    if (lookbackGroup) {
        lookbackGroup.style.display = selected === 'recently_added' ? '' : 'none';
    }
    // Processing order only affects full-library scans — recently-added scans
    // touch a small, time-bounded set where shuffle is essentially a no-op.
    // Intro & Credits checks every file of the chosen libraries (files already done are skipped), so it has no
    // lookback and no order either.
    const sortByGroup = document.getElementById('scheduleSortByGroup');
    if (sortByGroup) {
        sortByGroup.style.display = selected === 'full_library' ? '' : 'none';
    }
    MediaScanFilters.refresh('schedule');
    // When flipping to recently-added in "Add" mode with untouched defaults,
    // nudge the trigger type to Interval and pre-fill 15 minutes — that's
    // the canonical shape of a Recently Added scanner.
    if (selected === 'recently_added') {
        const editId = document.getElementById('scheduleEditId').value;
        const intervalInput = document.getElementById('scheduleIntervalValue');
        if (!editId && intervalInput && intervalInput.value === '2') {
            document.getElementById('scheduleTypeInterval').checked = true;
            intervalInput.value = '15';
            document.getElementById('scheduleIntervalUnit').value = 'minutes';
            onScheduleTypeChange();
        }
    }
}

function _schedulePriorityValue() {
    // null = "no pin, use the default"; the API distinguishes an absent
    // priority from an explicit one, so this must not fall back to 2.
    const raw = document.getElementById('schedulePriority').value;
    return raw === '' ? null : (parseInt(raw, 10) || 2);
}

function _getSelectedScheduleType() {
    return document.querySelector('input[name="scheduleType"]:checked').value;
}

function _resetScheduleForm() {
    _clearScheduleFieldErrors();
    MediaScanFilters.reset('schedule');
    MediaScanFilters.setLoading('schedule', false);
    document.getElementById('scheduleLibraryAll').checked = true;
    document.getElementById('scheduleName').value = '';
    const srvSel = document.getElementById('scheduleServer');
    if (srvSel) srvSel.value = '';
    document.getElementById('scheduleCron').value = '';
    document.getElementById('scheduleEditId').value = '';
    document.getElementById('scheduleEnabled').checked = true;
    // '' = Default (inherit). Seeding '2' here was why no schedule ever
    // reached the inherit path — every save posted an explicit Normal.
    document.getElementById('schedulePriority').value = '';

    // Reset scan mode to Full library and hide lookback group
    document.getElementById('scanModeFull').checked = true;
    const findMarkers = document.getElementById('scheduleMarkersFind');
    if (findMarkers) findMarkers.checked = true;
    document.getElementById('scheduleLookback').value = '1';
    const sortByEl = document.getElementById('scheduleSortBy');
    if (sortByEl) {
        sortByEl.querySelector('option[value="inherit"]')?.remove();
        sortByEl.value = 'default';
    }
    onScanModeChange();

    // Reset schedule type to Specific Time
    document.getElementById('scheduleTypeTime').checked = true;
    onScheduleTypeChange();

    // Reset Specific Time fields
    document.getElementById('scheduleTime').value = '02:00';
    const defaultDays = new Set(['1', '2', '3', '4', '5']);
    document.querySelectorAll('.schedule-day').forEach(cb => {
        cb.checked = defaultDays.has(cb.value);
    });

    // Reset Interval fields
    document.getElementById('scheduleIntervalValue').value = '2';
    document.getElementById('scheduleIntervalUnit').value = 'hours';

    // Reset Cron Expression field
    document.getElementById('scheduleCronInput').value = '';

    // D20 — clear the optional stop time
    const stopInput = document.getElementById('scheduleStopTime');
    if (stopInput) stopInput.value = '';
}

function showNewScheduleModal() {
    _resetScheduleForm();
    document.getElementById('scheduleModalTitle').textContent = 'Add Schedule';
    const addIcon = document.getElementById('scheduleModalIcon');
    if (addIcon) addIcon.className = 'bi bi-calendar-plus';
    document.getElementById('scheduleSubmitBtn').innerHTML =
        '<i class="bi bi-check me-1"></i>Create Schedule';

    MediaScanFilters.setLoading('schedule', true);
    _populateScheduleServerPicker().then(onScheduleServerChange);
    const modal = new bootstrap.Modal(document.getElementById('newScheduleModal'));
    modal.show();
}

function showEditScheduleModal(scheduleId) {
    const schedule = schedules.find(s => s.id === scheduleId);
    if (!schedule) {
        showToast('Error', 'Schedule not found', 'danger');
        return;
    }

    _resetScheduleForm();

    document.getElementById('scheduleEditId').value = schedule.id;
    document.getElementById('scheduleName').value = schedule.name || '';
    document.getElementById('scheduleEnabled').checked = schedule.enabled !== false;
    // ``|| 2`` would rewrite a stored null to Normal on the first edit,
    // silently pinning a schedule the user never pinned.
    const storedPriority = schedule.priority;
    document.getElementById('schedulePriority').value =
        storedPriority === null || storedPriority === undefined ? '' : String(storedPriority);
    // D20 — pre-fill optional stop time
    const stopInput = document.getElementById('scheduleStopTime');
    if (stopInput) stopInput.value = schedule.stop_time || '';

    // Phase H7: pre-select the multi-select library checkboxes from
    // schedule.library_ids (with single-element fallback for legacy entries).
    const wantIds = Array.isArray(schedule.library_ids) && schedule.library_ids.length
        ? schedule.library_ids.map(String)
        : (schedule.library_id ? [String(schedule.library_id)] : []);

    function _applyLibraryPreselect() {
        if (document.getElementById('scheduleEditId').value !== scheduleId
            || document.getElementById('scheduleServer').value !== (schedule.server_id || '')) return;
        MediaScanFilters.setLoading('schedule', false);
        const allCb = document.getElementById('scheduleLibraryAll');
        if (!wantIds.length) {
            // "All Libraries" semantics — leave master checkbox checked.
            if (allCb) allCb.checked = true;
            onScheduleLibraryAllChange(allCb);
            MediaScanFilters.reset('schedule', schedule.config || {});
            MediaScanFilters.refresh('schedule');
            return;
        }
        if (allCb) allCb.checked = false;
        onScheduleLibraryAllChange(allCb);
        document.querySelectorAll('.schedule-library-checkbox').forEach(cb => {
            cb.disabled = false;
            cb.checked = wantIds.includes(String(cb.value));
        });
        MediaScanFilters.reset('schedule', schedule.config || {});
        MediaScanFilters.refresh('schedule');
    }

    // Populate server picker, then refresh libraries scoped to it, then
    // pre-select the saved library_ids.
    MediaScanFilters.setLoading('schedule', true);
    _populateScheduleServerPicker(schedule.server_id || '')
        .then(onScheduleServerChange)
        .then(_applyLibraryPreselect);

    // Pre-fill scan mode + lookback from the schedule's config
    const cfg = schedule.config || {};
    if (cfg.job_type === 'intro_credits') {
        document.getElementById('scanModeMarkers').checked = true;
        document.getElementById(cfg.reconcile ? 'scheduleMarkersCheckServers' : 'scheduleMarkersFind').checked = true;
    } else if (cfg.job_type === 'recently_added') {
        document.getElementById('scanModeRecent').checked = true;
        const lookbackSelect = document.getElementById('scheduleLookback');
        const lookbackVal = String(cfg.lookback_hours || 1);
        if (Array.from(lookbackSelect.options).some(o => o.value === lookbackVal)) {
            lookbackSelect.value = lookbackVal;
        }
    } else {
        document.getElementById('scanModeFull').checked = true;
    }
    const sortBySelect = document.getElementById('scheduleSortBy');
    if (sortBySelect) {
        const savedSortBy = Object.prototype.hasOwnProperty.call(cfg, 'sort_by')
            ? (cfg.sort_by || 'default') : 'inherit';
        if (savedSortBy === 'inherit') {
            sortBySelect.add(new Option('Use configured order', 'inherit'));
        }
        if (Array.from(sortBySelect.options).some(o => o.value === savedSortBy)) {
            sortBySelect.value = savedSortBy;
        } else {
            sortBySelect.value = 'default';
        }
    }
    onScanModeChange();

    if (schedule.trigger_type === 'interval' && schedule.trigger_value) {
        // Interval schedule: populate interval fields
        document.getElementById('scheduleTypeInterval').checked = true;
        const totalMinutes = parseInt(schedule.trigger_value, 10);
        if (totalMinutes >= 60 && totalMinutes % 60 === 0) {
            document.getElementById('scheduleIntervalValue').value = String(totalMinutes / 60);
            document.getElementById('scheduleIntervalUnit').value = 'hours';
        } else {
            document.getElementById('scheduleIntervalValue').value = String(totalMinutes);
            document.getElementById('scheduleIntervalUnit').value = 'minutes';
        }
    } else if (schedule.trigger_type === 'cron' && schedule.trigger_value) {
        const parts = schedule.trigger_value.split(/\s+/);
        const isSimpleTimeDays = parts.length === 5
            && /^\d+$/.test(parts[0])
            && /^\d+$/.test(parts[1])
            && parts[2] === '*'
            && parts[3] === '*'
            && /^[\d,]+$/.test(parts[4]);

        if (isSimpleTimeDays) {
            // Simple time+days pattern: use the Specific Time UI
            document.getElementById('scheduleTypeTime').checked = true;
            document.getElementById('scheduleTime').value =
                `${parts[1].padStart(2, '0')}:${parts[0].padStart(2, '0')}`;
            // Convert APScheduler day (0=Mon) back to Unix cron day (0=Sun)
            const cronDays = parts[4].split(',').map(d => String((parseInt(d.trim()) + 1) % 7));
            document.querySelectorAll('.schedule-day').forEach(cb => {
                cb.checked = cronDays.includes(cb.value);
            });
        } else {
            // Complex cron: show the raw cron input
            document.getElementById('scheduleTypeCron').checked = true;
            document.getElementById('scheduleCronInput').value = schedule.trigger_value;
        }
    }
    onScheduleTypeChange();

    document.getElementById('scheduleModalTitle').textContent = 'Edit Schedule';
    const editIcon = document.getElementById('scheduleModalIcon');
    if (editIcon) editIcon.className = 'bi bi-pencil';
    document.getElementById('scheduleSubmitBtn').innerHTML =
        '<i class="bi bi-check me-1"></i>Save Changes';

    const modal = new bootstrap.Modal(document.getElementById('newScheduleModal'));
    modal.show();
}

async function saveSchedule() {
    const opening = modalOpening(document.getElementById('newScheduleModal'));
    const editId = document.getElementById('scheduleEditId').value;
    const name = document.getElementById('scheduleName').value.trim();
    _clearScheduleFieldErrors();
    if (!name) {
        _setScheduleFieldError('scheduleNameError', 'Name is required.');
        showToast('Error', 'Name is required', 'danger');
        return;
    }

    const scheduleType = _getSelectedScheduleType();
    const checksServers = _scheduleChecksServers();
    // Phase H7: collect selected library_ids from the checkbox group.
    // Empty list → "All Libraries" master is checked → backend treats as None.
    const allLibsCb = document.getElementById('scheduleLibraryAll');
    let selectedLibraryIds = [];
    if (!checksServers && allLibsCb && !allLibsCb.checked) {
        selectedLibraryIds = Array.from(document.querySelectorAll('.schedule-library-checkbox:checked'))
            .map(cb => cb.value);
        if (selectedLibraryIds.length === 0) {
            _setScheduleFieldError('scheduleLibrariesError', 'Select at least one library or check All Libraries.');
            showToast('Error', 'Select at least one library or check "All Libraries"', 'warning');
            return;
        }
    }
    // Display name: a single library uses its name; multi shows count.
    let libraryDisplay = checksServers ? 'All servers' : 'All Libraries';
    if (selectedLibraryIds.length === 1) {
        const lib = libraries.find(l => String(l.id) === String(selectedLibraryIds[0]));
        libraryDisplay = lib ? lib.name : 'Selected Library';
    } else if (selectedLibraryIds.length > 1) {
        libraryDisplay = `${selectedLibraryIds.length} libraries`;
    }
    const scanMode = document.querySelector('input[name="scanMode"]:checked').value;

    // Build the config blob — recently-added schedules carry their
    // lookback_hours value through the same config dict that user
    // schedules already use.
    const scheduleConfig = { job_type: scanMode };
    if (checksServers) {
        scheduleConfig.reconcile = true;
    } else if (scanMode === 'recently_added') {
        scheduleConfig.lookback_hours = parseFloat(document.getElementById('scheduleLookback').value) || 1;
    } else if (scanMode === 'full_library') {
        // Processing order only applies to full-library scans
        const scanFilters = MediaScanFilters.read('schedule');
        if (scanFilters === null) return;
        Object.assign(scheduleConfig, scanFilters);
        const sortByEl = document.getElementById('scheduleSortBy');
        const sortBy = sortByEl ? sortByEl.value : '';
        if (sortBy && sortBy !== 'inherit') {
            scheduleConfig.sort_by = sortBy;
        }
    }

    const serverSelect = document.getElementById('scheduleServer');
    const serverId = serverSelect && !checksServers ? serverSelect.value : '';

    // D20 — stop_time only applies to time-of-day triggers; for
    // interval triggers we always send an empty string so the backend
    // clears any pre-existing stop cron.
    const stopInputEl = document.getElementById('scheduleStopTime');
    const stopTimeValue = (scheduleType === 'interval')
        ? ''
        : (stopInputEl ? (stopInputEl.value || '') : '');

    const payload = {
        name: name,
        library_id: selectedLibraryIds.length === 1 ? selectedLibraryIds[0] : null,
        library_ids: selectedLibraryIds,
        library_name: libraryDisplay,
        server_id: serverId || null,
        enabled: document.getElementById('scheduleEnabled').checked,
        priority: _schedulePriorityValue(),
        config: scheduleConfig,
        stop_time: stopTimeValue,
    };

    if (scheduleType === 'specific-time') {
        const timeValue = document.getElementById('scheduleTime').value;
        if (!timeValue) {
            _setScheduleFieldError('scheduleTimeError', 'Time is required.');
            showToast('Error', 'Time is required', 'danger');
            return;
        }
        const selectedDays = Array.from(document.querySelectorAll('.schedule-day:checked')).map(cb => cb.value);
        if (selectedDays.length === 0) {
            _setScheduleFieldError('scheduleDaysError', 'Select at least one day.');
            showToast('Error', 'Select at least one day', 'danger');
            return;
        }
        const [hours, minutes] = timeValue.split(':');
        // Convert Unix cron day (0=Sun) to APScheduler day (0=Mon)
        const apsDays = selectedDays.map(d => (parseInt(d) + 6) % 7);
        payload.cron_expression = `${parseInt(minutes)} ${parseInt(hours)} * * ${apsDays.join(',')}`;
    } else if (scheduleType === 'interval') {
        const intervalValue = parseInt(document.getElementById('scheduleIntervalValue').value, 10);
        if (!intervalValue || intervalValue < 1) {
            _setScheduleFieldError('scheduleIntervalError', 'Interval must be at least 1.');
            showToast('Error', 'Interval must be at least 1', 'danger');
            return;
        }
        const unit = document.getElementById('scheduleIntervalUnit').value;
        payload.interval_minutes = unit === 'hours' ? intervalValue * 60 : intervalValue;
    } else if (scheduleType === 'cron') {
        const cronInput = document.getElementById('scheduleCronInput').value.trim();
        if (!cronInput) {
            _setScheduleFieldError('scheduleCronError', 'Cron expression is required.');
            showToast('Error', 'Cron expression is required', 'danger');
            return;
        }
        const parts = cronInput.split(/\s+/);
        if (parts.length !== 5) {
            _setScheduleFieldError('scheduleCronError', 'Cron expression must have 5 fields.');
            showToast('Error', 'Cron expression must have 5 fields (minute hour day-of-month month day-of-week)', 'danger');
            return;
        }
        payload.cron_expression = cronInput;
    }

    // D21 — warn the user when this schedule's fire time falls inside
    // the active Quiet Hours window. The processing_paused gate in
    // execute_scheduled_job will silently skip every fire from this
    // schedule until quiet hours ends — better to flag it now than
    // have the user think the schedule is broken.
    if (typeof window._scheduleQuietHoursOverlap === 'function') {
        const synthetic = {
            trigger_type: payload.cron_expression ? 'cron' : 'interval',
            trigger_value: payload.cron_expression || String(payload.interval_minutes || ''),
        };
        const overlap = window._scheduleQuietHoursOverlap(synthetic);
        if (overlap && !await window.appConfirm(overlap + '\n\nSave anyway?', { title: 'Schedule overlaps quiet hours', confirmText: 'Save anyway', variant: 'warning' })) {
            return;
        }
    }

    try {
        if (editId) {
            await apiPut(`/api/schedules/${editId}`, payload);
            showToast('Schedule Updated', `Schedule "${name}" updated successfully`, 'success');
        } else {
            await apiPost('/api/schedules', payload);
            showToast('Schedule Created', `Schedule "${name}" created successfully`, 'success');
        }

        hideModalSafely(document.getElementById('newScheduleModal'), opening);
        loadSchedules();
    } catch (error) {
        const action = editId ? 'update' : 'create';
        showToast('Error', `Failed to ${action} schedule: ` + error.message, 'danger');
    }
}

async function toggleSchedule(scheduleId, enabled) {
    try {
        await apiPut(`/api/schedules/${scheduleId}`, { enabled: enabled });
        loadSchedules();
    } catch (error) {
        showToast('Error', 'Failed to update schedule: ' + error.message, 'danger');
    }
}

async function runScheduleNow(scheduleId) {
    try {
        await apiPost(`/api/schedules/${scheduleId}/run`);
        loadJobs();
        loadJobStats();
        showToast('Schedule Triggered', 'Schedule has been triggered', 'success');
    } catch (error) {
        showToast('Error', 'Failed to run schedule: ' + error.message, 'danger');
    }
}

async function deleteSchedule(scheduleId) {
    if (!await window.appConfirm('Delete this schedule? Future runs will stop firing.', { title: 'Delete schedule', confirmText: 'Delete' })) return;

    try {
        await apiDelete(`/api/schedules/${scheduleId}`);
        loadSchedules();
        showToast('Schedule Deleted', 'Schedule has been deleted', 'info');
    } catch (error) {
        showToast('Error', 'Failed to delete schedule: ' + error.message, 'danger');
    }
}
