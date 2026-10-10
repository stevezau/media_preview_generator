/* Dashboard strip for Library health: shown only when the last check found something to act on (previews Plex
 * isn't showing, or a check that failed). Reads the stored results from GET /api/library-health, so nothing is
 * re-counted here. Depends on app.js: escapeHtml. */
(function () {
    'use strict';

    const REFRESH_MS = 5 * 60 * 1000;

    function n(value) { return Number(value || 0).toLocaleString(); }

    function notShowingTotal(server) {
        return (server.libraries || []).reduce(function (sum, lib) {
            const cell = lib.cells && lib.cells.previews;
            return sum + (cell && cell.state === 'counted' ? cell.not_showing || 0 : 0);
        }, 0);
    }

    function line(kind, text, href, action) {
        return `<div class="lh-strip-line lh-strip-${kind}" role="status">
            <i class="bi ${kind === 'bad' ? 'bi-exclamation-circle' : 'bi-eye-slash'}" aria-hidden="true"></i>
            <span>${text}</span>
            <a class="btn btn-sm ${kind === 'bad' ? 'btn-outline-danger' : 'btn-outline-warning'}" href="${href}">${action}</a>
        </div>`;
    }

    function linesFor(data) {
        if (data.running && data.running.kind === 'reread') {
            const count = data.running.total ? ` · ${n(data.running.done)} of ${n(data.running.total)}` : '';
            return [line('warn', `Asking Plex to re-read videos so it shows their previews${count}`, '/library-health', 'View')];
        }
        const servers = data.servers || [];
        const plexCount = servers.filter(function (s) { return s.type === 'plex'; }).length;
        const lines = [];
        servers.forEach(function (server) {
            if (server.error) {
                lines.push(line('bad', `Library check failed for ${escapeHtml(server.name)}: ${escapeHtml(server.error)}`,
                    '/library-health', 'Open Library health'));
            }
            const total = server.type === 'plex' ? notShowingTotal(server) : 0;
            if (total > 0) {
                const owner = plexCount > 1 ? ` (${escapeHtml(server.name)})` : '';
                lines.push(line('warn', `<b>${n(total)} previews are made but Plex isn't showing them</b>${owner}`,
                    `/library-health?fix=${encodeURIComponent(server.server_id)}`, 'Review &amp; fix'));
            }
        });
        if (!lines.length && data.last_error) {
            lines.push(line('bad', `Library check failed: ${escapeHtml(data.last_error)}`, '/library-health', 'Open Library health'));
        }
        return lines;
    }

    async function refresh() {
        const strip = document.getElementById('libraryHealthStrip');
        if (!strip) return;
        try {
            const resp = await fetch('/api/library-health');
            if (!resp.ok) return;
            const lines = linesFor(await resp.json());
            strip.innerHTML = lines.join('');
            strip.hidden = !lines.length;
        } catch (err) {
            console.error('Library health strip refresh failed', err);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        refresh();
        setInterval(function () { if (!document.hidden) refresh(); }, REFRESH_MS);
    });
})();
