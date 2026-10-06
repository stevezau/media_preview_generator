/* Week graph for Settings: Mon-Sun lanes drawn from the windows the editors already hold. No data of its own. */
(function () {
    'use strict';
    const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
    const DAY_MINUTES = 1440;
    const WEEK_MINUTES = DAY_MINUTES * 7;
    const LANE_COLORS = ['var(--run)', 'var(--accent)', 'var(--t-intro)', 'var(--t-loud)', 'var(--ok)'];
    const LANE_HEIGHT = 6;
    const escape = value => String(value).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const clock = minutes => String(Math.floor(minutes / 60)).padStart(2, '0') + ':' + String(minutes % 60).padStart(2, '0');

    function toMinutes(value) {
        const [hours, minutes] = String(value || '').split(':');
        return Number(hours) * 60 + (Number(minutes) || 0);
    }

    // A window belongs to its start day, so an overnight one spills into the next day as a continuation.
    function segments(windows) {
        const out = [];
        windows.forEach(window => {
            const start = toMinutes(window.start);
            const end = toMinutes(window.end);
            if (!window.start || !window.end || Number.isNaN(start) || Number.isNaN(end) || start === end) return;
            window.days.forEach(day => {
                if (end > start) out.push({ day, from: start, to: end });
                else {
                    out.push({ day, from: start, to: DAY_MINUTES });
                    if (end > 0) out.push({ day: (day + 1) % 7, from: 0, to: end, continued: true });
                }
            });
        });
        return out;
    }

    const allWeek = () => DAYS.map((_, day) => ({ day, from: 0, to: DAY_MINUTES }));

    function groupSegments(group) {
        return group.availability.mode === 'always' ? allWeek() : segments(group.availability.windows);
    }

    function pauseWindows() {
        const enabled = document.getElementById('quietHoursEnabled');
        if (!enabled || !enabled.checked) return [];
        return [...document.querySelectorAll('#quietHoursWindows .qh-window')].map(row => ({
            start: row.querySelector('.qh-window-start').value,
            end: row.querySelector('.qh-window-end').value,
            days: [...row.querySelectorAll('.qh-day')].filter(box => box.checked).map(box => DAY_KEYS.indexOf(box.value)),
        }));
    }

    const pauseSegments = () => segments(pauseWindows());

    function now(timeZone) {
        const options = { weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
        let parts;
        try {
            const zone = timeZone && timeZone !== 'Local time' ? { timeZone } : {};
            parts = new Intl.DateTimeFormat('en-US', { ...options, ...zone }).formatToParts(new Date());
        } catch (_) {
            // An unnamed server zone has no IANA id the browser knows; the browser's own clock is the best guess.
            parts = new Intl.DateTimeFormat('en-US', options).formatToParts(new Date());
        }
        const part = type => parts.find(item => item.type === type)?.value;
        return { day: Math.max(0, DAYS.indexOf(part('weekday'))), minute: (Number(part('hour')) % 24) * 60 + Number(part('minute')) };
    }

    // Minutes from now to the next window start, as "Fri 19:00"; null when there is none.
    function nextStart(timeZone) {
        const current = now(timeZone);
        let best = null;
        pauseSegments().filter(item => !item.continued).forEach(item => {
            let wait = ((item.day - current.day + 7) % 7) * DAY_MINUTES + item.from - current.minute;
            if (wait <= 0) wait += WEEK_MINUTES;
            if (!best || wait < best.wait) best = { wait, label: `${DAYS[item.day]} ${clock(item.from)}` };
        });
        return best && best.label;
    }

    function pausedNow(timeZone) {
        const current = now(timeZone);
        return pauseSegments().some(item => item.day === current.day && current.minute >= item.from && current.minute < item.to);
    }

    const percent = minutes => (minutes / DAY_MINUTES * 100).toFixed(3) + '%';

    /**
     * Draw seven day bars. lanes: [{color, label, segments}] stacked inside each bar, or one full-height lane when
     * `height` is given; pause: pause segments overlaid as hatching.
     */
    function render(lanes, pause, { height = null, timeZone = null, laneHeight = LANE_HEIGHT } = {}) {
        const current = now(timeZone);
        const barHeight = height || Math.max(lanes.length, 1) * (laneHeight + 1) - 1;
        const rows = DAYS.map((name, day) => {
            const lane = lanes.map((item, index) => item.segments.filter(segment => segment.day === day).map(segment =>
                `<i class="wkg-seg" style="--c:${item.color};left:${percent(segment.from)};width:${percent(segment.to - segment.from)};top:${height ? 0 : index * (laneHeight + 1)}px;height:${height || laneHeight}px" title="${escape(`${item.label} ${name} ${clock(segment.from)}–${clock(segment.to)}`)}"></i>`).join('')).join('');
            const paused = pause.filter(segment => segment.day === day).map(segment =>
                `<i class="wkg-seg wkg-pause" style="left:${percent(segment.from)};width:${percent(segment.to - segment.from)}" title="${escape(`Paused ${name} ${clock(segment.from)}–${clock(segment.to)}`)}"></i>`).join('');
            const marker = day === current.day ? `<i class="wkg-now" style="left:${percent(current.minute)}"></i>` : '';
            return `<div class="wkg-row${day === current.day ? ' is-today' : ''}"><span class="wkg-day">${name}</span><div class="wkg-bar" style="height:${barHeight}px">${lane}${paused}${marker}</div></div>`;
        }).join('');
        const axis = [[0, '00'], [25, '06'], [50, '12'], [75, '18'], [100, '24']].map(([at, text]) => `<span style="left:${at}%">${text}</span>`).join('');
        return `<div class="wkg" aria-hidden="true">${rows}<div class="wkg-axis"><i></i><div>${axis}</div></div></div>`;
    }

    window.WeekGraph = { DAYS, DAY_KEYS, LANE_COLORS, render, groupSegments, pauseSegments, nextStart, pausedNow, now, clock };
})();
