(function () {
    'use strict';

    // Navigation clicks scroll for a while; the scroll-spy must not overwrite the section they asked for.
    const NAVIGATION_LOCK_MS = 900;
    let navigationLockedUntil = 0;

    function sectionLinks() {
        return Array.from(document.querySelectorAll('#settings-sidebar a[href^="#section-"]'));
    }

    function markSection(id) {
        const select = document.getElementById('settingsMobileSection');
        const feedback = document.getElementById('settingsMobileSectionFeedback');
        const option = select && Array.from(select.options).find(function (item) { return item.value === id; });
        sectionLinks().forEach(function (link) {
            const current = link.getAttribute('href') === `#${id}`;
            link.classList.toggle('active', current);
            if (current) link.setAttribute('aria-current', 'location');
            else link.removeAttribute('aria-current');
        });
        if (!option) return;
        select.value = id;
        if (feedback) feedback.textContent = `Showing ${option.textContent}`;
    }

    function setupMobileSections() {
        const select = document.getElementById('settingsMobileSection');
        const feedback = document.getElementById('settingsMobileSectionFeedback');
        if (!select || !feedback) return;

        function showSection(id, scroll) {
            const section = document.getElementById(id);
            if (!section || !Array.from(select.options).some(function (item) { return item.value === id; })) return;
            navigationLockedUntil = Date.now() + NAVIGATION_LOCK_MS;
            markSection(id);
            if (scroll) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        select.addEventListener('change', function () {
            history.pushState(null, '', `#${select.value}`);
            showSection(select.value, true);
        });
        window.addEventListener('hashchange', function () {
            showSection(location.hash.slice(1), true);
        });
        sectionLinks().forEach(function (link) {
            link.addEventListener('click', function () {
                navigationLockedUntil = Date.now() + NAVIGATION_LOCK_MS;
                markSection(link.getAttribute('href').slice(1));
            });
        });
        if (location.hash) showSection(location.hash.slice(1), false);
        else markSection('section-workers');
    }

    // Highlights the section nearest the upper third of the screen in the sidebar and the mobile picker.
    function setupScrollSpy() {
        const sections = Array.from(document.querySelectorAll('.settings-content .section-card[id]'));
        if (!sections.length || !('IntersectionObserver' in window)) return;
        const observer = new IntersectionObserver(function (entries) {
            if (Date.now() < navigationLockedUntil) return;
            entries.forEach(function (entry) {
                if (entry.isIntersecting) markSection(entry.target.id);
            });
        }, { rootMargin: '-30% 0px -60% 0px' });
        sections.forEach(function (section) { observer.observe(section); });
    }

    function setupSecretToggles() {
        document.querySelectorAll('[data-toggle-secret]').forEach(function (button) {
            button.addEventListener('click', function () {
                const input = button.parentElement.querySelector('input');
                const reveal = input.type === 'password';
                input.type = reveal ? 'text' : 'password';
                button.querySelector('i').className = reveal ? 'bi bi-eye-slash' : 'bi bi-eye';
                button.setAttribute('aria-pressed', String(reveal));
            });
        });
    }

    const PRESET_DAYS = { all: [0, 1, 2, 3, 4, 5, 6], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6] };

    // The pause windows are edited by schedules.js; this only draws their week and keeps the nav dot in step.
    function setupPauseSchedule() {
        const windows = document.getElementById('quietHoursWindows');
        const graph = document.getElementById('pauseWeekGraph');
        const enabled = document.getElementById('quietHoursEnabled');
        if (!windows || !graph || !enabled || !window.WeekGraph) return;
        const hint = document.getElementById('quietHoursNextHint');
        const badge = document.getElementById('quietHoursStateBadge');
        const dot = document.getElementById('settingsNavPauseDot');
        const timeZone = function () { return window.WorkerGroups?.getSnapshot()?.timezone; };

        const weekCard = document.getElementById('pauseWeekCard');

        function paint() {
            // An empty chart while the schedule is off is noise; the windows below stay editable.
            if (weekCard) weekCard.hidden = !enabled.checked;
            graph.innerHTML = window.WeekGraph.render([], window.WeekGraph.pauseSegments(), { height: 14, timeZone: timeZone() });
            if (hint) {
                const next = window.WeekGraph.nextStart(timeZone());
                if (!enabled.checked) hint.textContent = 'Applied when on.';
                else if (window.WeekGraph.pausedNow(timeZone())) hint.textContent = 'Everything is paused, including current files.';
                else hint.textContent = next ? `Next pause ${next}.` : 'Add a window to pause on a schedule.';
            }
            window.dispatchEvent(new CustomEvent('settings-pause-changed'));
        }

        function syncDot() {
            if (dot && badge) dot.hidden = badge.textContent.trim() === 'off';
        }

        windows.addEventListener('input', paint);
        windows.addEventListener('change', paint);
        enabled.addEventListener('change', paint);
        // schedules.js rebuilds rows on load, add and remove, none of which fire an input event.
        new MutationObserver(paint).observe(windows, { childList: true });
        if (badge) new MutationObserver(syncDot).observe(badge, { childList: true, characterData: true, subtree: true, attributes: true });
        windows.addEventListener('click', function (event) {
            const preset = event.target.closest('[data-qh-preset]');
            if (!preset) return;
            const days = PRESET_DAYS[preset.dataset.qhPreset];
            preset.closest('.qh-window').querySelectorAll('.qh-day').forEach(function (box, index) {
                box.checked = days.includes(index);
            });
            paint();
        });
        paint();
        syncDot();
    }

    document.addEventListener('DOMContentLoaded', function () {
        setupMobileSections();
        setupScrollSpy();
        setupSecretToggles();
        setupPauseSchedule();
        const indicator = document.getElementById('saveStatusIndicator');
        if (indicator && !indicator.textContent.trim()) indicator.textContent = 'All changes saved';
    });
})();
