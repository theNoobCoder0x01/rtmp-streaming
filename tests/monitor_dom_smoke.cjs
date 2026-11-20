// Optional: NODE_PATH=<temporary jsdom install>/node_modules node tests/monitor_dom_smoke.cjs
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');

const root = path.join(__dirname, '..');
const fixtures = JSON.parse(execFileSync('python3', [path.join(__dirname, 'monitor_dom_fixtures.py')], { encoding: 'utf8' }));
const errors = [], requests = [], downloads = [];
let failDashboard = false;
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => errors.push(error.message));
const dom = new JSDOM(readFileSync(path.join(root, 'm.html'), 'utf8'), { url: 'http://monitor.test/m.html', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole });
const { window } = dom;
const $ = id => window.document.getElementById(id);
window.setInterval = () => 1;
window.URL.createObjectURL = () => 'blob:test';
window.URL.revokeObjectURL = () => {};
const originalClick = window.HTMLAnchorElement.prototype.click;
window.HTMLAnchorElement.prototype.click = function () { if (this.download) downloads.push(this.download); else originalClick.call(this); };
window.fetch = async input => {
    const url = new URL(input, 'http://monitor.test'); requests.push(url);
    if (url.pathname.endsWith('/dashboard') && failDashboard) return new Response(JSON.stringify({ detail: 'Monitoring storage is unavailable.' }), { status: 503 });
    if (url.pathname.endsWith('.csv')) return new Response('id,stream_name\n1,test\n', { headers: { 'Content-Type': 'text/csv' } });
    const endpoint = url.pathname.split('/').at(-1);
    const prefix = url.searchParams.get('ip') === '192.168.1.250' ? 'empty_' : url.searchParams.get('role') === 'publisher' ? 'publisher_' : '';
    assert.ok(fixtures[prefix + endpoint], `Unexpected API endpoint ${url}`);
    return new Response(JSON.stringify(fixtures[prefix + endpoint]), { headers: { 'Content-Type': 'application/json' } });
};
async function waitFor(predicate, description) {
    for (let attempt = 0; attempt < 100; attempt++) {
        if (predicate()) return;
        await new Promise(resolve => setTimeout(resolve, 10));
    }
    throw new Error(`Timed out: ${description}; ${$('pageError').textContent}`);
}
async function run() {
    window.eval(readFileSync(path.join(root, 'public/monitor.js'), 'utf8'));
    await waitFor(() => $('publishers').textContent === '1', 'live data');
    assert.equal($('viewers').textContent, '2');
    assert.equal($('uniqueIps').textContent, '3');
    assert.ok($('trafficChart').querySelector('svg'));
    assert.match($('connectionsBody').textContent, /192\.168\.1\.20/);
    assert.equal(window.document.querySelector('img'), null, 'Stream names must render as text, never HTML');
    assert.equal(window.injected, undefined);

    window.document.querySelector('[data-view="logs"]').click();
    await waitFor(() => $('pageTitle').textContent === 'Connection log' && $('logBody').textContent.includes('192.168.1.23'), 'session log');
    window.document.querySelector('[data-mode="events"]').click();
    await waitFor(() => $('logHead').textContent.includes('Event'), 'event log');
    assert.match($('logBody').textContent, /Disconnected/);
    $('role').value = 'publisher';
    $('filters').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => requests.at(-1).searchParams.get('role') === 'publisher' && !$('refresh').disabled, 'role filter');
    assert.ok(![...$('logBody').querySelectorAll('.badge')].some(element => element.textContent.includes('viewer')));

    window.document.querySelector('[data-view="reports"]').click();
    await waitFor(() => $('pageTitle').textContent === 'Reports' && $('reportSessions').textContent === '2', 'filtered report');
    assert.ok($('roleBreakdown').textContent.includes('Broadcaster'));
    $('ip').value = '192.168.1.250';
    $('filters').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => $('reportSessions').textContent === '0', 'empty report');
    assert.match($('streamReportBody').textContent, /No activity/);
    $('resetFilters').click();
    await waitFor(() => $('reportSessions').textContent === '4', 'reset report filters');
    window.document.querySelector('[data-section="reports"] .export').click();
    await waitFor(() => downloads.length === 1, 'CSV download');

    failDashboard = true;
    window.document.querySelector('[data-view="live"]').click();
    await waitFor(() => !$('pageError').hidden, 'error state');
    assert.match($('pageError').textContent, /storage is unavailable/);
    assert.equal($('publishers').textContent, '1', 'An error must not fabricate zero connections');
    failDashboard = false;
    $('refresh').click();
    await waitFor(() => $('pageError').hidden && !$('refresh').disabled, 'recovery');
    assert.deepEqual(errors, []);
    dom.window.close();
    console.log('DOM smoke passed: live data, safe text rendering, history/events, filters, reports, empty/error/recovery states and CSV.');
}
run().catch(error => { console.error(error); dom.window.close(); process.exitCode = 1; });
