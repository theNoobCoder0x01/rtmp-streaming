const assert = require('node:assert/strict');
const test = require('node:test');
const { duration, dateRange, localDate } = require('../public/monitor.js');

test('durations handle empty, brief, long and invalid values', () => {
    assert.equal(duration(null), '—');
    assert.equal(duration(NaN), '—');
    assert.equal(duration(-2), '0s');
    assert.equal(duration(0), '0s');
    assert.equal(duration(61), '1m 1s');
    assert.equal(duration(3661), '1h 1m');
    assert.equal(duration(90000), '1d 1h');
});

test('inclusive local dates convert to a half-open UTC interval', () => {
    const result = dateRange('2026-11-20', '2026-11-20');
    assert.equal(+new Date(result.start), +new Date(2026, 10, 20));
    assert.equal(+new Date(result.end), +new Date(2026, 10, 21));
    assert.equal(localDate(new Date(2026, 10, 20, 23, 59)), '2026-11-20');
});

test('invalid or reversed date ranges do not produce misleading reports', () => {
    assert.throws(() => dateRange('', '2026-11-20'));
    assert.throws(() => dateRange('2026-11-22', '2026-11-20'));
});
