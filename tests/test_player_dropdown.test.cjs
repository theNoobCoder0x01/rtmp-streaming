const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = readFileSync(path.join(__dirname, '..', 'player.html'), 'utf8');
const script = Array.from(html.matchAll(/<script>([\s\S]*?)<\/script>/g)).at(-1)[1];

function createPlayer() {
    const mutations = { added: 0, removed: 0, labels: 0, selection: 0 };
    class Option {
        constructor(text, value) {
            this.label = text;
            this.value = value;
        }
        get text() { return this.label; }
        set text(value) { this.label = value; mutations.labels++; }
    }
    const select = {
        options: [new Option('Select a stream...', '')],
        selectedValue: '',
        get value() { return this.selectedValue; },
        set value(value) {
            this.selectedValue = this.options.some(option => option.value === value) ? value : '';
            mutations.selection++;
        },
        add(option) { this.options.push(option); mutations.added++; },
        remove(index) {
            if (this.options[index].value === this.selectedValue) this.selectedValue = '';
            this.options.splice(index, 1);
            mutations.removed++;
        },
    };
    const info = { innerHTML: '' };
    const sources = [];
    const player = {
        plays: 0, pauses: 0, resets: 0,
        src(source) { sources.push(source.src); },
        play() { this.plays++; return Promise.resolve(); },
        pause() { this.pauses++; },
        reset() { this.resets++; },
    };
    const sockets = [];
    class WebSocket {
        static OPEN = 1;
        constructor() { this.readyState = 1; this.messages = []; sockets.push(this); }
        send(message) { this.messages.push(JSON.parse(message)); }
    }
    const listeners = {};
    const timers = [];
    const context = vm.createContext({
        document: {
            getElementById(id) { return id === 'streamSelect' ? select : info; },
            addEventListener(event, callback) { listeners[event] = callback; },
        },
        location: { protocol: 'http:', host: 'localhost:8080' },
        videojs: () => player,
        Option, WebSocket,
        setTimeout: callback => timers.push(callback),
        console: { log() {} },
    });
    vm.runInContext(script, context, { filename: 'player.html' });
    listeners.DOMContentLoaded();
    return {
        select, player, sources, mutations, info, sockets,
        update(streams) {
            sockets.at(-1).onmessage({ data: JSON.stringify({ type: 'status_update', streams }) });
        },
        choose(id) { select.value = id; context.changeStream(); },
        refresh() { context.refreshStreams(); },
        reconnect() { sockets.at(-1).onclose(); timers.shift()(); },
    };
}

const stream = (id, changes = {}) => ({
    id, name: id, status: 'active', quality: '720p', bandwidth_in: 1, bandwidth_out: 2,
    ...changes,
});

test('selects and starts the first active stream when it becomes available', () => {
    const app = createPlayer();
    app.update([]);
    assert.equal(app.select.value, '');
    assert.equal(app.player.resets, 0);
    app.update([stream('offline', { status: 'inactive' }), stream('first'), stream('second')]);
    assert.equal(app.select.value, 'first');
    assert.deepEqual(app.sources, ['/hls/first.m3u8']);
    assert.equal(app.player.plays, 1);
});

test('refresh and bandwidth updates preserve dropdown nodes and video playback', () => {
    const app = createPlayer();
    app.update([stream('first'), stream('second')]);
    const options = app.select.options.slice();
    const before = { ...app.mutations };
    app.refresh();
    assert.deepEqual(app.sockets[0].messages, [{ type: 'request_status' }]);
    app.update([stream('first', { bandwidth_in: 9 }), stream('second')]);
    assert.deepEqual(app.mutations, before);
    app.select.options.forEach((option, index) => assert.equal(option, options[index]));
    assert.deepEqual(app.sources, ['/hls/first.m3u8']);
    assert.match(app.info.innerHTML, /9\.00 Mbps/);
});

test('a user selection survives refresh, list reordering and reconnection', () => {
    const app = createPlayer();
    app.update([stream('first'), stream('second')]);
    app.choose('second');
    const before = { ...app.mutations };
    app.update([stream('second'), stream('first')]);
    app.reconnect();
    app.update([stream('first'), stream('second')]);
    assert.equal(app.select.value, 'second');
    assert.deepEqual(app.mutations, before);
    assert.deepEqual(app.sources, ['/hls/first.m3u8', '/hls/second.m3u8']);
});

test('adding and removing other streams retains the selected option and playback', () => {
    const app = createPlayer();
    app.update([stream('first'), stream('second')]);
    app.choose('second');
    const selectedOption = app.select.options[2];
    app.update([stream('new'), stream('first'), stream('second')]);
    app.update([stream('new'), stream('second')]);
    assert.equal(app.select.value, 'second');
    assert.equal(app.select.options.find(option => option.value === 'second'), selectedOption);
    assert.deepEqual(app.sources, ['/hls/first.m3u8', '/hls/second.m3u8']);
    assert.equal(app.mutations.added, 3);
    assert.equal(app.mutations.removed, 1);
});

test('quality changes update a label without rebuilding options or restarting video', () => {
    const app = createPlayer();
    app.update([stream('first')]);
    const option = app.select.options[1];
    app.update([stream('first', { quality: '1080p' })]);
    assert.equal(app.select.options[1], option);
    assert.equal(option.text, 'Stream first (1080p)');
    assert.equal(app.mutations.added, 1);
    assert.equal(app.mutations.labels, 1);
    assert.equal(app.mutations.removed, 0);
    assert.equal(app.player.plays, 1);
});

test('an unavailable choice never falls back to another stream and resumes on return', () => {
    const app = createPlayer();
    app.update([stream('first'), stream('second')]);
    app.choose('second');
    app.update([stream('first'), stream('second', { status: 'inactive' })]);
    assert.equal(app.select.value, '');
    assert.equal(app.player.pauses, 1);
    assert.equal(app.player.resets, 1);
    assert.equal(app.info.innerHTML, '');
    app.update([stream('first')]);
    assert.equal(app.player.resets, 1);
    app.update([stream('first'), stream('second')]);
    assert.equal(app.select.value, 'second');
    assert.deepEqual(app.sources, ['/hls/first.m3u8', '/hls/second.m3u8', '/hls/second.m3u8']);
});

test('explicitly clearing the selection prevents another automatic default', () => {
    const app = createPlayer();
    app.update([stream('first'), stream('second')]);
    app.choose('');
    app.update([stream('first'), stream('second'), stream('new')]);
    assert.equal(app.select.value, '');
    assert.deepEqual(app.sources, ['/hls/first.m3u8']);
    assert.equal(app.player.resets, 1);
});

test('a changed source reloads only the selected stream', () => {
    const app = createPlayer();
    app.update([stream('first'), stream('second')]);
    app.update([stream('first', { name: 'renamed' }), stream('second', { name: 'other' })]);
    assert.equal(app.select.value, 'first');
    assert.deepEqual(app.sources, ['/hls/first.m3u8', '/hls/renamed.m3u8']);
});
