(function (global) {
    'use strict';
    const roleNames = { publisher: 'Broadcaster', rtmp_viewer: 'RTMP viewer', hls_viewer: 'Browser viewer' };
    const titles = { live: ['Live overview', 'A current view of broadcasters, viewers and connection activity.'], logs: ['Connection log', 'Persistent session history and connect/disconnect events.'], reports: ['Reports', 'Explore connection activity across a selected date range.'] };
    function duration(value) {
        if (value === null || !Number.isFinite(Number(value))) return '—';
        const seconds = Math.max(0, Math.floor(Number(value)));
        if (seconds < 60) return `${seconds}s`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
        return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
    }
    function stamp(value) {
        return value === null || value === undefined ? '—' : new Date(value * 1000).toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
    function localDate(date) {
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    }
    function dateRange(from, through) {
        const start = new Date(`${from}T00:00:00`), end = new Date(`${through}T00:00:00`);
        end.setDate(end.getDate() + 1);
        if (!from || !through || !Number.isFinite(+start) || !Number.isFinite(+end) || start >= end) throw new Error('Choose a valid date range.');
        return { start: start.toISOString(), end: end.toISOString() };
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = { duration, dateRange, localDate };
    if (typeof document === 'undefined') return;

    const $ = id => document.getElementById(id);
    const state = { view: 'live', mode: 'sessions', offset: 0, total: 0, busy: false, request: 0, controller: null };
    const text = (id, value) => { $(id).textContent = value; };
    function badge(label, kind) {
        const element = document.createElement('span');
        element.className = `badge ${kind}`;
        element.textContent = label;
        return element;
    }
    const role = value => badge(roleNames[value] || value, roleNames[value] ? value : 'neutral');
    function table(id, columns, rows, empty = 'No activity recorded for this selection.') {
        const body = $(id), fragment = document.createDocumentFragment();
        for (const item of rows) {
            const row = document.createElement('tr');
            for (const column of columns) {
                const cell = document.createElement('td');
                const value = column.value(item);
                if (value instanceof Node) cell.append(value); else cell.textContent = value ?? '—';
                if (column.mono) cell.className = 'mono';
                row.append(cell);
            }
            fragment.append(row);
        }
        if (!rows.length) {
            const row = document.createElement('tr'), cell = document.createElement('td');
            cell.colSpan = columns.length; cell.className = 'empty'; cell.textContent = empty; row.append(cell); fragment.append(row);
        }
        body.replaceChildren(fragment);
    }
    const eventColumns = [
        { label: 'Event', value: row => badge(row.kind === 'connect' ? 'Connected' : 'Disconnected', row.kind) },
        { label: 'Role', value: row => role(row.role) },
        { label: 'LAN IP', value: row => row.ip_address, mono: true },
        { label: 'Stream', value: row => row.stream_name },
        { label: 'Time', value: row => stamp(row.occurred_at) },
        { label: 'Reason', value: row => `${row.reason.replaceAll('_', ' ')}${row.estimated ? ' · estimated' : ''}` },
    ];
    const sessionColumns = [
        { label: 'Role', value: row => role(row.role) },
        { label: 'LAN IP', value: row => row.ip_address, mono: true },
        { label: 'Stream', value: row => row.stream_name },
        { label: 'Account', value: row => row.username || '—' },
        { label: 'Connected', value: row => stamp(row.started_at) },
        { label: 'Disconnected', value: row => row.ended_at === null ? badge('Active', 'healthy') : stamp(row.ended_at) },
        { label: 'Duration', value: row => `${row.estimated ? '≈ ' : ''}${duration(row.duration_seconds)}` },
        { label: 'Source / reason', value: row => (row.end_reason || row.start_source).replaceAll('_', ' ') },
    ];
    async function api(path, options = {}) {
        const response = await fetch(path, { cache: 'no-store', ...options });
        if (response.status === 401) { location.reload(); throw new Error('Sign-in expired.'); }
        if (!response.ok) {
            let message = 'Unable to fetch monitoring data. Try refreshing.';
            try { const data = await response.json(); message = typeof data.detail === 'string' ? data.detail : 'Check your filters and try again.'; } catch (_) {}
            throw new Error(message);
        }
        return response;
    }
    function selectedQuery() {
        const fields = new FormData($('filters'));
        const params = new URLSearchParams(dateRange(fields.get('from'), fields.get('to')));
        for (const key of ['stream', 'ip', 'role']) { const value = fields.get(key).trim(); if (value) params.set(key, value); }
        if (state.view === 'logs' && state.mode === 'sessions' && fields.get('status')) params.set('status', fields.get('status'));
        return params;
    }
    function chart(samples) {
        const target = $('trafficChart'); target.replaceChildren();
        if (!samples.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'No traffic measurements yet. History will appear as streams are observed.'; target.append(empty); return; }
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 840 190'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Observed RTMP bandwidth, in megabits per second.');
        const first = samples[0].at, last = Math.max(first + 300, samples.at(-1).at);
        const maximum = Math.max(1, ...samples.map(row => Math.max(row.bandwidth_in, row.bandwidth_out)));
        for (let index = 0; index < 4; index++) {
            const y = 20 + index * 43;
            const line = document.createElementNS(ns, 'line'); Object.entries({ x1: 48, x2: 826, y1: y, y2: y, stroke: '#edf1f7' }).forEach(([key, value]) => line.setAttribute(key, value)); svg.append(line);
            const label = document.createElementNS(ns, 'text'); label.setAttribute('x', '0'); label.setAttribute('y', y + 4); label.setAttribute('fill', '#91a0b4'); label.setAttribute('font-size', '10'); label.textContent = (maximum * (3 - index) / 3).toFixed(1); svg.append(label);
        }
        for (const [key, color] of [['bandwidth_in', '#8da9ec'], ['bandwidth_out', '#245de5']]) {
            let points = [], previous = null;
            const draw = () => { if (!points.length) return; const line = document.createElementNS(ns, 'polyline'); line.setAttribute('points', points.join(' ')); line.setAttribute('fill', 'none'); line.setAttribute('stroke', color); line.setAttribute('stroke-width', '2.5'); svg.append(line); points = []; };
            for (const sample of samples) {
                if (previous !== null && sample.at - previous > 600) draw();
                const x = 48 + (sample.at - first) / (last - first) * 778, y = 149 - sample[key] / maximum * 129;
                points.push(`${x},${y}`); previous = sample.at;
                if (samples.length === 1) { const dot = document.createElementNS(ns, 'circle'); dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.setAttribute('r', '3'); dot.setAttribute('fill', color); svg.append(dot); }
            }
            draw();
        }
        for (const [at, x, anchor] of [[first, 48, 'start'], [samples.at(-1).at, 826, 'end']]) {
            const label = document.createElementNS(ns, 'text'); label.setAttribute('x', x); label.setAttribute('y', '178'); label.setAttribute('text-anchor', anchor); label.setAttribute('fill', '#91a0b4'); label.setAttribute('font-size', '10'); label.textContent = new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); svg.append(label);
        }
        target.append(svg);
    }
    function live(data) {
        text('publishers', data.counts.publisher); text('viewers', data.counts.rtmp_viewer + data.counts.hls_viewer);
        text('viewerBreakdown', `${data.counts.hls_viewer} browser · ${data.counts.rtmp_viewer} direct RTMP`); text('uniqueIps', data.unique_ips);
        text('outbound', data.streams.reduce((sum, row) => sum + row.bandwidth_out, 0).toFixed(2));
        text('streamCount', `${data.streams.length} observed`); text('connectionCount', `${data.total_connections} active sessions${data.total_connections > 200 ? ' · showing latest 200' : ''}`);
        $('healthBadge').className = `badge ${data.health.status}`; text('healthBadge', data.health.status === 'healthy' ? 'Monitor healthy' : data.health.status);
        $('healthNotice').hidden = data.health.status === 'healthy' && !data.health.dropped_events;
        text('healthNotice', [data.health.message || (data.health.status === 'starting' ? 'Waiting for the first Nginx observation.' : ''), data.health.last_success ? `Last successful observation ${duration(data.health.age_seconds)} ago.` : '', data.health.dropped_events ? `${data.health.dropped_events} observation(s) could not be recorded.` : ''].filter(Boolean).join(' '));
        chart(data.history);
        table('streamsBody', [{ value: row => row.name }, { value: row => row.quality }, { value: row => row.publishers }, { value: row => row.rtmp_viewers }, { value: row => `${row.bandwidth_in.toFixed(2)} Mbps` }, { value: row => `${row.bandwidth_out.toFixed(2)} Mbps` }], data.streams, 'No RTMP streams are currently observed.');
        table('connectionsBody', sessionColumns.filter(column => !['Disconnected', 'Source / reason'].includes(column.label)), data.connections, 'No devices are currently connected.');
        table('recentBody', eventColumns, data.recent_events, 'No connection events recorded in the last 24 hours.');
    }
    function logs(data) {
        const columns = state.mode === 'sessions' ? sessionColumns : eventColumns;
        const row = document.createElement('tr'); for (const column of columns) { const cell = document.createElement('th'); cell.textContent = column.label; row.append(cell); } $('logHead').replaceChildren(row);
        table('logBody', columns, data.items); state.total = data.total;
        text('pageCount', data.total ? `${data.offset + 1}–${Math.min(data.offset + data.limit, data.total)} of ${data.total}` : 'No records');
        $('previous').disabled = state.offset === 0; $('next').disabled = state.offset + data.limit >= data.total;
    }
    function report(data) {
        text('reportSessions', data.summary.sessions); text('reportIps', data.summary.unique_ips); text('reportDuration', duration(data.summary.duration_seconds)); text('reportPeak', data.summary.peak_connections);
        text('reportRange', `${stamp(data.start)} – ${stamp(data.end)}`); text('reportEvents', `${data.summary.connects} connects · ${data.summary.disconnects} disconnects in this range`);
        const area = $('roleBreakdown'); area.replaceChildren();
        const maximum = Math.max(1, ...data.roles.map(row => row.sessions));
        for (const item of data.roles) {
            const row = document.createElement('div'); row.className = 'role-row';
            const label = document.createElement('span'); label.textContent = roleNames[item.label] || item.label;
            const track = document.createElement('div'); track.className = 'bar-track'; const bar = document.createElement('div'); bar.className = 'bar'; bar.style.width = `${item.sessions / maximum * 100}%`; track.append(bar);
            const count = document.createElement('strong'); count.textContent = item.sessions; row.append(label, track, count); area.append(row);
        }
        if (!data.roles.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'No connections overlap this date range.'; area.append(empty); }
        table('streamReportBody', [{ value: row => row.label }, { value: row => row.sessions }, { value: row => row.unique_ips }, { value: row => duration(row.duration_seconds) }], data.streams);
        table('ipReportBody', [{ value: row => row.label, mono: true }, { value: row => row.sessions }, { value: row => duration(row.duration_seconds) }], data.ips);
    }
    async function load() {
        const version = ++state.request;
        if (state.controller) state.controller.abort(); state.controller = new AbortController(); state.busy = true;
        $('pageError').hidden = true; $('refresh').disabled = true;
        try {
            let path = '/api/monitoring/dashboard';
            if (state.view !== 'live') { const params = selectedQuery(); if (state.view === 'logs') { params.set('limit', '50'); params.set('offset', state.offset); } path = `/api/monitoring/${state.view === 'reports' ? 'reports' : state.mode}?${params}`; }
            const response = await api(path, { signal: state.controller.signal }); const data = await response.json();
            if (version !== state.request) return;
            ({ live, logs, reports: report })[state.view](data);
            text('freshness', `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`);
        } catch (error) { if (error.name !== 'AbortError' && version === state.request) { text('pageError', error.message); $('pageError').hidden = false; } }
        finally { if (version === state.request) { state.busy = false; $('refresh').disabled = false; } }
    }
    function view() {
        const requested = location.hash.slice(1); state.view = titles[requested] ? requested : 'live';
        document.querySelectorAll('[data-section]').forEach(element => { element.hidden = element.dataset.section !== state.view; });
        document.querySelectorAll('[data-view]').forEach(element => { if (element.dataset.view === state.view) element.setAttribute('aria-current', 'page'); else element.removeAttribute('aria-current'); });
        text('pageTitle', titles[state.view][0]); text('pageDescription', titles[state.view][1]); $('filterArea').hidden = state.view === 'live'; $('healthNotice').hidden = true;
        $('statusField').hidden = state.view !== 'logs' || state.mode === 'events'; state.offset = 0; load();
    }
    function resetFilters() { $('filters').reset(); $('from').value = $('to').value = localDate(new Date()); state.offset = 0; }
    $('refresh').addEventListener('click', load);
    $('filters').addEventListener('submit', event => { event.preventDefault(); state.offset = 0; load(); });
    $('resetFilters').addEventListener('click', () => { resetFilters(); load(); });
    $('previous').addEventListener('click', () => { state.offset = Math.max(0, state.offset - 50); load(); });
    $('next').addEventListener('click', () => { state.offset += 50; load(); });
    document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => { state.mode = button.dataset.mode; state.offset = 0; document.querySelectorAll('[data-mode]').forEach(item => item.setAttribute('aria-pressed', String(item === button))); $('statusField').hidden = state.mode === 'events'; load(); }));
    document.querySelectorAll('.export').forEach(button => button.addEventListener('click', async () => {
        button.disabled = true;
        try { const response = await api(`/api/monitoring/sessions.csv?${selectedQuery()}`); const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a'); link.href = url; link.download = 'stream-sessions.csv'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
        catch (error) { text('pageError', error.message); $('pageError').hidden = false; }
        finally { button.disabled = false; }
    }));
    window.addEventListener('hashchange', view);
    document.addEventListener('visibilitychange', () => { if (!document.hidden && state.view === 'live') load(); });
    setInterval(() => { if (state.view === 'live' && !document.hidden && !state.busy) load(); }, 5000);
    text('timeZone', `Times are shown in ${Intl.DateTimeFormat().resolvedOptions().timeZone}. End date is inclusive. Live overview updates every five seconds; history and reports refresh on demand.`);
    resetFilters(); view();
})(globalThis);
