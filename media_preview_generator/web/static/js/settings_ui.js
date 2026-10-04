(function () {
    'use strict';

    const summaries = {
        maxConcurrentJobs: 'Recommended: 3. Lower this to reduce load on your media servers.',
        incomingJobPriority: 'Recommended: High, so new imports can start ahead of long scans.',
        autoRequeueOnRestart: 'Recommended: On, so interrupted work can resume after a restart.',
    };

    function setupLayeredHelp() {
        document.querySelectorAll('.settings-content .form-text').forEach(function (help, index) {
            if (help.closest('template, .alert') || help.textContent.trim().length < 150) return;
            const group = help.closest('.mb-3, .mb-4, .form-group, .card-body') || help.parentElement;
            const control = group && group.querySelector('input[id], select[id], textarea[id]');
            if (!control || !summaries[control.id]) return;
            const label = control && group.querySelector(`label[for="${CSS.escape(control.id)}"]`);
            const richInfo = label && label.querySelector('.info-icon[data-explain-template]');
            const panelId = `settingsHelpDetail${index}`;
            const original = help.innerHTML;
            const summary = summaries[control.id];

            help.classList.add('settings-help-summary');
            help.textContent = summary;

            if (richInfo) {
                const template = document.getElementById(richInfo.dataset.explainTemplate);
                if (template && !template.content.querySelector('[data-settings-source-help]')) {
                    template.innerHTML += `<div data-settings-source-help>${original}</div>`;
                }
                return;
            }

            const toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.className = 'settings-help-toggle btn btn-link btn-sm p-0 mt-1';
            toggle.setAttribute('aria-expanded', 'false');
            toggle.setAttribute('aria-controls', panelId);
            toggle.textContent = 'More detail';
            const panel = document.createElement('div');
            panel.id = panelId;
            panel.className = 'settings-help-panel form-text mt-2';
            panel.hidden = true;
            panel.innerHTML = original;
            toggle.addEventListener('click', function () {
                const open = toggle.getAttribute('aria-expanded') === 'true';
                toggle.setAttribute('aria-expanded', String(!open));
                toggle.textContent = open ? 'More detail' : 'Hide detail';
                panel.hidden = open;
            });
            panel.addEventListener('keydown', function (event) {
                if (event.key !== 'Escape') return;
                panel.hidden = true;
                toggle.setAttribute('aria-expanded', 'false');
                toggle.textContent = 'More detail';
                toggle.focus();
            });
            help.after(toggle, panel);
        });
    }

    function setupMobileSections() {
        const select = document.getElementById('settingsMobileSection');
        const feedback = document.getElementById('settingsMobileSectionFeedback');
        if (!select || !feedback) return;

        function showSection(id, scroll) {
            const option = Array.from(select.options).find(function (item) { return item.value === id; });
            const section = document.getElementById(id);
            if (!option || !section) return;
            select.value = id;
            feedback.textContent = `Showing ${option.textContent}`;
            if (scroll) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        select.addEventListener('change', function () {
            history.pushState(null, '', `#${select.value}`);
            showSection(select.value, true);
        });
        window.addEventListener('hashchange', function () {
            showSection(location.hash.slice(1), true);
        });
        if (location.hash) showSection(location.hash.slice(1), false);
    }

    document.addEventListener('DOMContentLoaded', function () {
        setupLayeredHelp();
        setupMobileSections();
        const indicator = document.getElementById('saveStatusIndicator');
        if (indicator && !indicator.textContent.trim()) indicator.textContent = 'Saved';
    });
})();
