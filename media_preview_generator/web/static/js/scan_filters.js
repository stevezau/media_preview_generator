// Shared full-library filters for the New Job and Schedule dialogs.
window.MediaScanFilters = (() => {
    function root(prefix) {
        const el = document.getElementById(`${prefix}ScanFilters`);
        if (el && !el.dataset.wired) {
            el.dataset.wired = 'true';
            el.addEventListener('input', () => refresh(prefix));
            el.addEventListener('change', () => refresh(prefix));
            el.querySelector('[data-filter-clear]').addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                const wasOpen = el.open;
                reset(prefix);
                el.open = wasOpen;
                refresh(prefix);
            });
        }
        return el;
    }

    function field(el, key) {
        return el.querySelector(`[data-filter="${key}"]`);
    }

    function reset(prefix, config = {}) {
        const el = root(prefix);
        if (!el) return;
        const defaults = {
            added_filter: 'all', added_last_days: 30, added_from: '', added_to: '',
            season_mode: config.latest_seasons ? 'latest' : 'all', latest_seasons: 1,
            movie_year_mode: config.movie_year_from || config.movie_year_to ? 'range' : 'all',
            movie_year_from: '', movie_year_to: '',
        };
        for (const [key, fallback] of Object.entries(defaults)) {
            const input = field(el, key);
            input.value = config[key] ?? fallback;
            input.setCustomValidity('');
        }
        el.open = config.added_filter && config.added_filter !== 'all'
            || !!config.latest_seasons || !!config.movie_year_from || !!config.movie_year_to;
    }

    // Inline copy of each failed check, beside its field; the native validation popup alone is easy to miss.
    function showErrors(el) {
        el.querySelectorAll('[data-filter-error]').forEach(box => {
            const input = field(el, box.dataset.filterError);
            const message = input && !input.disabled ? input.validationMessage : '';
            box.hidden = !message;
            box.querySelector('span').textContent = message;
        });
    }

    function refresh(prefix) {
        const el = root(prefix);
        if (!el) return;
        el.querySelectorAll('[data-filter-error]').forEach(box => { box.hidden = true; });
        const enabled = prefix === 'schedule'
            ? document.getElementById('scanModeFull')?.checked
            : !!document.getElementById('jobKindPreviews')?.checked;
        el.hidden = !enabled;
        const all = document.getElementById(`${prefix}LibraryAll`)?.checked;
        const rows = Array.from(document.querySelectorAll(`.${prefix}-library-checkbox`));
        const selected = all ? rows : rows.filter(cb => cb.checked);
        const loading = el.dataset.loading === 'true';
        const kinds = selected.map(cb => (cb.dataset.libraryKind || '').toLowerCase());
        const movieKinds = ['movie', 'movies'];
        const tvKinds = ['show', 'shows', 'tvshow', 'tvshows', 'episode', 'series'];
        const unknown = kinds.some(kind => !movieKinds.includes(kind) && !tvKinds.includes(kind));
        const hasTv = unknown || kinds.some(kind => tvKinds.includes(kind));
        const hasMovies = unknown || kinds.some(kind => movieKinds.includes(kind));
        const note = el.querySelector('[data-filter-scope-note]');
        note.textContent = loading ? 'Loading library scope…'
            : !rows.length ? 'Load libraries to choose media filters.'
            : !selected.length ? 'Select at least one library to choose media filters.'
            : unknown ? 'Some library types are unknown. TV and movie filters apply only to their matching media types.' : '';
        note.hidden = !note.textContent;
        el.querySelector('[data-filter-fields]').disabled = !enabled || !selected.length || loading;

        for (const [kind, visible, mode, values] of [
            ['tv', hasTv, 'season_mode', ['latest_seasons']],
            ['movie', hasMovies, 'movie_year_mode', ['movie_year_from', 'movie_year_to']],
        ]) {
            const group = el.querySelector(`[data-filter-type="${kind}"]`);
            group.hidden = !visible;
            group.disabled = !visible;
            // A different library selection must not carry invisible restrictions.
            if (!loading && selected.length && !visible) {
                field(el, mode).value = 'all';
                for (const key of values) field(el, key).value = key === 'latest_seasons' ? '1' : '';
            }
        }
        const dateMode = field(el, 'added_filter').value;
        const active = {
            days: dateMode === 'last_days', dates: dateMode === 'date_range',
            seasons: hasTv && field(el, 'season_mode').value === 'latest',
            years: hasMovies && field(el, 'movie_year_mode').value === 'range',
        };
        for (const [section, visible] of Object.entries(active)) {
            const area = el.querySelector(`[data-filter-section="${section}"]`);
            area.hidden = !visible;
            area.classList.toggle('d-none', !visible);
            area.querySelectorAll('input').forEach(input => {
                input.disabled = !visible;
                input.required = visible && section !== 'years';
                input.setCustomValidity('');
            });
        }
        const parts = [];
        if (active.days) parts.push(`Added in last ${field(el, 'added_last_days').value || '…'} days`);
        if (active.dates) parts.push(`Added ${field(el, 'added_from').value || '…'} through ${field(el, 'added_to').value || '…'}`);
        if (active.seasons) parts.push(`TV: latest ${field(el, 'latest_seasons').value || '…'} seasons per show`);
        if (active.years) {
            const from = field(el, 'movie_year_from').value;
            const to = field(el, 'movie_year_to').value;
            parts.push(from && to ? `Movies: ${from}–${to}`
                : from ? `Movies: ${from} onwards` : to ? `Movies: through ${to}` : 'Movies: choose release years');
        }
        el.querySelector('[data-filter-summary]').textContent = parts.join(' · ') || 'All selected media';
    }

    function read(prefix) {
        const el = root(prefix);
        if (!el) return {};
        refresh(prefix);
        if (el.dataset.loading === 'true') {
            el.open = true;
            return null;
        }
        if (el.hidden) return {};
        if (el.querySelector('[data-filter-fields]').disabled) {
            if (field(el, 'added_filter').value !== 'all' || field(el, 'season_mode').value !== 'all'
                || field(el, 'movie_year_mode').value !== 'all') {
                el.open = true;
                return null;
            }
            return {};
        }
        const value = key => field(el, key).value;
        const config = { added_filter: value('added_filter') };
        if (config.added_filter === 'last_days') config.added_last_days = Number(value('added_last_days'));
        if (config.added_filter === 'date_range') {
            config.added_from = value('added_from');
            config.added_to = value('added_to');
            if (config.added_from > config.added_to && config.added_to) {
                field(el, 'added_to').setCustomValidity('Through date must be on or after From date.');
            }
        }
        if (!el.querySelector('[data-filter-type="tv"]').hidden && value('season_mode') === 'latest') {
            config.latest_seasons = Number(value('latest_seasons'));
        }
        if (!el.querySelector('[data-filter-type="movie"]').hidden && value('movie_year_mode') === 'range') {
            const from = value('movie_year_from');
            const to = value('movie_year_to');
            if (!from && !to) field(el, 'movie_year_from').setCustomValidity('Enter at least one release year.');
            if (from) config.movie_year_from = Number(from);
            if (to) config.movie_year_to = Number(to);
            if (from && to && Number(from) > Number(to)) {
                field(el, 'movie_year_to').setCustomValidity('Through year must be on or after From year.');
            }
        }
        for (const input of el.querySelectorAll('input[type="number"]')) {
            if (!input.disabled && input.value && !Number.isSafeInteger(Number(input.value))) {
                input.setCustomValidity('Enter a whole number.');
            }
        }
        const invalid = Array.from(el.querySelectorAll('input, select')).find(input => !input.checkValidity());
        if (invalid) {
            el.open = true;
            showErrors(el);
            invalid.reportValidity();
            return null;
        }
        return config;
    }

    function setLoading(prefix, loading) {
        const el = root(prefix);
        if (!el) return;
        el.dataset.loading = String(loading);
        refresh(prefix);
    }

    return { reset, refresh, read, setLoading };
})();

// Segmented control drawn over a <select>. The select stays the source of truth (ids, tests and the JS that sets
// .value keep working); its value setter is wrapped so a programmatic change moves the highlight too.
window.OverlaySegments = (() => {
    function sync(select) {
        const group = select._ovSeg;
        if (!group) return;
        group.querySelectorAll('[role="radio"]').forEach(button => {
            const on = button.dataset.value === select.value;
            button.classList.toggle('on', on);
            button.setAttribute('aria-checked', String(on));
            button.tabIndex = on ? 0 : -1;
            button.querySelector('.ov-def')?.toggleAttribute('hidden', button.dataset.value !== select.dataset.segDefault);
        });
    }

    function choose(select, value) {
        select.value = value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function build(select) {
        if (select._ovSeg) return;
        const group = document.createElement('div');
        group.className = 'ov-seg';
        group.setAttribute('role', 'radiogroup');
        const label = select.id && document.querySelector(`label[for="${select.id}"]`);
        group.setAttribute('aria-label', (label?.firstChild?.textContent || 'Choice').trim());
        for (const option of select.options) {
            const button = document.createElement('button');
            button.type = 'button';
            button.setAttribute('role', 'radio');
            button.dataset.value = option.value;
            button.innerHTML = `${option.textContent.replace(/ \(from Settings\)$/, '')}<span class="ov-def" hidden>default</span>`;
            button.addEventListener('click', () => choose(select, option.value));
            group.appendChild(button);
        }
        group.addEventListener('keydown', event => {
            const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
            if (!step) return;
            const buttons = Array.from(group.querySelectorAll('button'));
            const next = buttons[(buttons.indexOf(document.activeElement) + step + buttons.length) % buttons.length];
            event.preventDefault();
            next.focus();
            choose(select, next.dataset.value);
        });
        const nativeValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
        Object.defineProperty(select, 'value', {
            get() { return nativeValue.get.call(this); },
            set(value) { nativeValue.set.call(this, value); sync(this); },
        });
        select._ovSeg = group;
        select.after(group);
        sync(select);
    }

    function init(scope = document) {
        scope.querySelectorAll('select.ov-seg-select').forEach(build);
    }
    init();
    return { init, sync, build };
})();
