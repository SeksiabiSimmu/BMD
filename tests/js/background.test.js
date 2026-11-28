'use strict';
// Simulates the browser around the real service worker (app/extension/background.js).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'extension', 'background.js'), 'utf8');
const EXT = 'chrome-extension://ext-id/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function harness(options = {}) {
  const h = { ports: [], badge: [], listeners: {}, sent: [] };
  function makePort() {
    const port = {
      posted: [], connected: true, answered: undefined,
      onMessage: { addListener(fn) { port.deliver = fn; } },
      onDisconnect: { addListener(fn) { port.drop = (reason) => { port.connected = false; chrome.runtime.lastError = reason ? { message: reason } : undefined; fn(); chrome.runtime.lastError = undefined; }; } },
      postMessage(message) {
        if (!port.connected) throw new Error('Attempting to use a disconnected port object');
        port.posted.push(message);
        h.sent.push(message);
        h.behave(port, message);
      },
      disconnect() { port.connected = false; },
    };
    return port;
  }
  const chrome = {
    runtime: {
      id: 'ext-id', lastError: undefined,
      getURL: (p) => EXT + String(p).replace(/^\//, ''),
      connectNative() { const port = makePort(); h.ports.push(port); return port; },
      onInstalled: { addListener() {} }, onStartup: { addListener() {} },
      onMessage: { addListener(fn) { h.listeners.message = fn; } },
    },
    alarms: { get: async () => true, create: async () => {}, onAlarm: { addListener(fn) { h.listeners.alarm = fn; } } },
    storage: { local: { get: async () => (options.storage || {}), set: async () => {} } },
    contextMenus: { removeAll(cb) { cb && cb(); }, create() {}, onClicked: { addListener() {} } },
    commands: { onCommand: { addListener() {} } },
    notifications: { create: async () => {} },
    action: { setBadgeText: async (v) => { h.badge.push(v.text); }, setBadgeBackgroundColor: async () => {} },
    tabs: { query: async () => [] },
  };
  // Default behaviour: answer every request successfully.
  h.behave = options.behave || ((port, message) => {
    const data = message.method === 'jobs' ? { jobs: [], counts: { queued: 2, running: 1 }, next_cursor: null } : { ok: true, version: '2.5.0' };
    queueMicrotask(() => port.deliver({ id: message.id, ok: true, data }));
  });
  h.delays = [];
  const spyTimeout = (fn, ms, ...rest) => { h.delays.push(ms); return setTimeout(fn, ms, ...rest); };
  const context = vm.createContext({ chrome, URL, setTimeout: spyTimeout, clearTimeout, Date, console, queueMicrotask });
  vm.runInContext(SOURCE, context);
  h.rpc = (method, params = {}, sender) => new Promise((resolve) => {
    const keep = h.listeners.message({ type: 'BMD_RPC', method, params },
      sender || { id: 'ext-id', url: EXT + 'dashboard.html' }, resolve);
    if (keep !== true) resolve('rejected');
  });
  h.count = (method) => h.sent.filter((m) => m.method === method).length;
  h.chrome = chrome;
  return h;
}

test('only the popup and dashboard may use the native bridge', async () => {
  const h = harness();
  await sleep(20);
  const before = h.sent.length;
  const intruders = [
    { id: 'ext-id', url: 'https://www.youtube.com/watch?v=abcdefghijk', tab: {} },
    { id: 'other-ext', url: EXT + 'dashboard.html' },
    { id: 'ext-id', url: EXT + 'evil.html' },
    { id: 'ext-id', url: 'https://evil.example/dashboard.html' },
    { id: 'ext-id', url: 'chrome-extension://other/dashboard.html' },
    { id: 'ext-id', url: undefined },
  ];
  for (const sender of intruders) assert.equal(await h.rpc('jobs', {}, sender), 'rejected', JSON.stringify(sender));
  assert.equal(h.sent.length, before, 'nothing reached the helper');
  for (const url of [EXT + 'popup.html', EXT + 'dashboard.html#queue', EXT + 'dashboard.html?x=1']) {
    assert.equal((await h.rpc('health', {}, { id: 'ext-id', url })).ok, true, url);
  }
});

test('requests are routed with ids, params and results; unknown operations never reach the helper', async () => {
  const h = harness();
  await sleep(20);
  const reply = await h.rpc('jobs', { limit: 10, cursor: 'abc', state: 'done' });
  assert.equal(reply.ok, true);
  const posted = h.sent.filter((m) => m.method === 'jobs').pop();
  assert.deepEqual(posted.params, { limit: 10, cursor: 'abc', state: 'done' });
  assert.ok(Number.isInteger(posted.id));
  const bad = await h.rpc('format_disk');
  assert.equal(bad.ok, false);
  assert.equal(h.count('format_disk'), 0);
  h.behave = (port, m) => queueMicrotask(() => port.deliver({ id: m.id, ok: false, error: 'Unknown job' }));
  const failed = await h.rpc('status', { id: 'x' });
  assert.deepEqual([failed.ok, failed.error], [false, 'Unknown job']);
});

test('read-only requests are retried once on a fresh connection after a disconnect', async () => {
  let first = true;
  const h = harness({ behave: (port, m) => {
    if (m.method === 'health') return queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: {} }));
    if (first) { first = false; queueMicrotask(() => port.drop('Native host has exited.')); }
    else queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: { jobs: [{ id: 'j' }], counts: {} } }));
  } });
  await sleep(20);
  const result = await h.rpc('jobs');
  assert.equal(result.ok, true);
  assert.equal(result.data.jobs.length, 1);
  assert.equal(h.count('jobs'), 2, 'sent exactly twice: original + one retry');
  assert.ok(h.ports.length >= 2, 'a new port was opened');
});

test('read-only retry is limited to one attempt', async () => {
  const h = harness({ behave: (port, m) => {
    if (m.method === 'health') return queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: {} }));
    queueMicrotask(() => port.drop('Native host has exited.'));
  } });
  await sleep(20);
  const result = await h.rpc('jobs');
  assert.equal(result.ok, false);
  assert.match(result.error, /exited|repair/);
  assert.equal(h.count('jobs'), 2);
  await sleep(600);
  assert.equal(h.count('jobs'), 2, 'nothing keeps replaying in the background');
});

test('state-changing requests are never replayed and report an unknown outcome', async () => {
  for (const method of ['enqueue', 'cancel', 'retry', 'clear']) {
    const h = harness({ behave: (port, m) => {
      if (m.method === 'health') return queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: {} }));
      queueMicrotask(() => port.drop('Native host has exited.'));
    } });
    await sleep(20);
    const result = await h.rpc(method, { id: 'a'.repeat(32), url: 'https://youtu.be/abcdefghijk' });
    assert.equal(result.ok, false, method);
    assert.equal(result.ambiguous, true, method);
    assert.match(result.error, /before confirming/);
    await sleep(700);
    assert.equal(h.count(method), 1, `${method} was sent exactly once, even after reconnects`);
  }
});

test('a request to a host the browser never started is reported as not sent, not as unknown', async () => {
  // Regression (real-browser run at 2.5.0, no helper installed): the page button said "The helper
  // disconnected before confirming this action. Check the queue before trying again." for a
  // request that had provably never left the browser.
  for (const reason of ['Specified native messaging host not found.', 'Access to the specified native messaging host is forbidden.']) {
    const h = harness({ behave: (port) => queueMicrotask(() => port.drop(reason)) });
    await sleep(30);
    await sleep(1200);                                   // the warm-up's failed connection blocks dials for 1 s
    const result = await h.rpc('cancel', { id: 'c'.repeat(32) });
    assert.equal(result.ok, false, reason);
    assert.equal(result.ambiguous, false, `${reason}: nothing was sent, so the outcome is not unknown`);
    assert.ok(result.error.startsWith(reason), `${reason}: the browser's own reason is shown`);
    assert.match(result.error, /setup again to repair/);
    assert.equal(h.count('cancel'), 1, 'sent exactly once');
  }
  // The exception relies on the browser's reason: a host that had already answered on this
  // connection may have received the request, whatever the disconnect message says later.
  let answered = false;
  const h = harness({ behave: (port, m) => {
    if (m.method === 'health') { answered = true; return queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: {} })); }
    queueMicrotask(() => port.drop('Specified native messaging host not found.'));
  } });
  await sleep(30);
  assert.ok(answered, 'the helper answered the warm-up');
  const result = await h.rpc('cancel', { id: 'c'.repeat(32) });
  assert.equal(result.ambiguous, true, 'an answered connection keeps the unknown-outcome warning');
});

test('a helper that cannot start triggers back-off instead of a reconnect storm', async () => {
  const h = harness({ behave: (port) => queueMicrotask(() => port.drop('Specified native messaging host not found.')) });
  await sleep(30);                                     // warm-up health already failed once
  const portsBefore = h.ports.length;
  const results = [];
  for (let i = 0; i < 4; i++) results.push(await h.rpc('cancel', { id: 'b'.repeat(32) }));
  assert.ok(h.ports.length - portsBefore <= 1, 'connectNative is not hammered during the back-off window');
  const blocked = results.filter((r) => /retrying in/.test(r.error));
  assert.ok(blocked.length >= 3, 'later calls fail fast with a clear message');
  assert.ok(blocked.every((r) => r.ambiguous !== true), 'unsent requests are not reported as ambiguous');
  assert.match(blocked[0].error, /setup again to repair/);
});

test('a healthy connection resets the back-off, and the reconnect request clears it', async () => {
  let broken = true;
  const h = harness({ behave: (port, m) => {
    if (broken) return queueMicrotask(() => port.drop('Native host has exited.'));
    queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: { ok: true, version: '2.5.0' } }));
  } });
  await sleep(30);
  assert.equal((await h.rpc('health')).ok, false);      // blocked now
  broken = false;
  const reset = await h.rpc('reconnect');
  assert.equal(reset.ok, true);
  assert.equal(reset.data.version, '2.5.0');
  const after = await h.rpc('jobs');
  assert.equal(after.ok, true, 'back-off was cleared');
  // an answered connection that later drops must reconnect immediately (no penalty)
  h.ports[h.ports.length - 1].drop('Native host has exited.');
  assert.equal((await h.rpc('health')).ok, true);
});

test('the periodic alarm refreshes the toolbar badge from the helper', async () => {
  const h = harness();
  await sleep(20);
  h.listeners.alarm({ name: 'bmd-v2-queue-watch' });
  await sleep(50);
  assert.equal(h.badge.pop(), '3');
});

test('track-enqueued notes validate their input', async () => {
  const h = harness();
  const ask = (ids, sender) => new Promise((resolve) => {
    const keep = h.listeners.message({ type: 'BMD_TRACK_ENQUEUED', ids }, sender || { id: 'ext-id', url: EXT + 'popup.html' }, resolve);
    if (keep !== true) resolve('rejected');
  });
  assert.equal((await ask(['a'.repeat(32)])).ok, true);
  assert.equal(await ask(['../etc']), 'rejected');
  assert.equal(await ask(Array(201).fill('a'.repeat(32))), 'rejected');
  assert.equal(await ask(['a'.repeat(32)], { id: 'ext-id', url: 'https://www.youtube.com/' }), 'rejected');
});

test('the read-only retry pause is short and jittered', async () => {
  const pauses = [];
  for (let run = 0; run < 12; run++) {
    let first = true;
    const h = harness({ behave: (port, m) => {
      if (m.method === 'health') return queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: {} }));
      if (first) { first = false; queueMicrotask(() => port.drop('Native host has exited.')); }
      else queueMicrotask(() => port.deliver({ id: m.id, ok: true, data: { jobs: [], counts: {} } }));
    } });
    await sleep(10);
    const started = h.delays.length;
    assert.equal((await h.rpc('jobs')).ok, true);
    // the only timer between 50 and 1000 ms created by the retry is the pause (request timeouts are >= 20 s)
    pauses.push(...h.delays.slice(started).filter((ms) => ms >= 50 && ms <= 1000));
  }
  assert.equal(pauses.length, 12);
  assert.ok(pauses.every((ms) => ms >= 100 && ms < 200), pauses.join(','));
  assert.ok(new Set(pauses.map((ms) => Math.round(ms))).size > 3, 'jitter varies between runs');
});

test('enqueue from a YouTube page uses the saved output format, not a hard-coded WAV', async () => {
  // Regression (audit): the service worker hard-coded format:'wav' in enqueueTrack, so the
  // right-click menu and keyboard shortcut ignored the format chosen in the popup/dashboard.
  const h = harness({ storage: { format: 'flac' }, behave: (port, m) => {
    const data = m.method === 'enqueue'
      ? { added: ['a'.repeat(32)], duplicate: [], existing: [] }
      : { jobs: [], counts: {}, next_cursor: null };
    queueMicrotask(() => port.deliver({ id: m.id, ok: true, data }));
  } });
  await sleep(20);
  const reply = await new Promise((resolve) => {
    const keep = h.listeners.message(
      { type: 'BMD_ENQUEUE_CURRENT', title: 'A Song' },
      { id: 'ext-id', url: 'https://www.youtube.com/watch?v=abcdefghijk', tab: { title: 'Tab title' } },
      resolve);
    if (keep !== true) resolve('rejected');
  });
  assert.equal(reply.added, 1, 'one track was queued');
  const posted = h.sent.filter((m) => m.method === 'enqueue').pop();
  assert.equal(posted.params.format, 'flac', 'the saved format travels with the enqueue');
  assert.match(reply.message, /FLAC/, 'the confirmation names the real format');
});

test('an unrecognized stored format falls back to WAV instead of failing the enqueue', async () => {
  const h = harness({ storage: { format: 'ogg' }, behave: (port, m) => {
    const data = m.method === 'enqueue'
      ? { added: ['a'.repeat(32)], duplicate: [], existing: [] }
      : { jobs: [], counts: {}, next_cursor: null };
    queueMicrotask(() => port.deliver({ id: m.id, ok: true, data }));
  } });
  await sleep(20);
  const reply = await new Promise((resolve) => {
    const keep = h.listeners.message(
      { type: 'BMD_ENQUEUE_CURRENT', title: 'A Song' },
      { id: 'ext-id', url: 'https://www.youtube.com/watch?v=abcdefghijk', tab: { title: 'T' } },
      resolve);
    if (keep !== true) resolve('rejected');
  });
  assert.equal(reply.added, 1);
  const posted = h.sent.filter((m) => m.method === 'enqueue').pop();
  assert.equal(posted.params.format, 'wav', 'unknown stored values fall back to wav');
  assert.match(reply.message, /WAV/);
});

test('a URL with a timestamp fragment is queued by its video id instead of being refused', async () => {
  // Regression: canonicalVideo refused any URL with a #fragment, although the popup's own check
  // accepted it, so "watch?v=<id>&t=42s#start" (a link copied from the address bar) failed with
  // "Open a supported YouTube video". The canonical URL is built from the validated id alone.
  // (youtu.be is not a content-script host; its fragment handling is covered by the helper's
  // URL tests and by canonicalVideo's id extraction, tested here through the watch/shorts forms.)
  const enqueueFrom = async (pageUrl) => {
    const h = harness({ behave: (port, m) => {
      const data = m.method === 'enqueue'
        ? { added: ['a'.repeat(32)], duplicate: [], existing: [] }
        : { jobs: [], counts: {}, next_cursor: null };
      queueMicrotask(() => port.deliver({ id: m.id, ok: true, data }));
    } });
    await sleep(20);
    const reply = await new Promise((resolve) => {
      const keep = h.listeners.message(
        { type: 'BMD_ENQUEUE_CURRENT', title: 'A Song' },
        { id: 'ext-id', url: pageUrl, tab: { title: 'Tab title' } }, resolve);
      if (keep !== true) resolve('rejected');
    });
    return { reply, posted: h.sent.filter((m) => m.method === 'enqueue').pop() };
  };
  const canonical = 'https://www.youtube.com/watch?v=abcdefghijk';
  for (const pageUrl of [canonical + '&t=42s#start', canonical + '#start',
                        'https://www.youtube.com/shorts/abcdefghijk#']) {
    const { reply, posted } = await enqueueFrom(pageUrl);
    assert.equal(reply.added, 1, pageUrl + ' is queued: ' + JSON.stringify(reply));
    assert.equal(posted.params.url, canonical, 'the helper receives the id, never the fragment');
  }
  // A malformed video id on a real watch page fails with the honest message.
  const bad = await enqueueFrom('https://www.youtube.com/watch?v=not-a-video-id#t=1');
  assert.match(String(bad.reply.error), /Open a supported YouTube video/);
  assert.equal(bad.posted, undefined, 'nothing reached the helper');
  // Senders the extension must not act for are ignored (no response at all).
  for (const pageUrl of ['https://www.youtube.com.evil.example/watch?v=abcdefghijk',
                        'https://www.youtube.com:444/watch?v=abcdefghijk']) {
    const { reply, posted } = await enqueueFrom(pageUrl);
    assert.equal(reply, 'rejected', pageUrl + ' is not served');
    assert.equal(posted, undefined, 'and nothing reaches the helper');
  }
});
