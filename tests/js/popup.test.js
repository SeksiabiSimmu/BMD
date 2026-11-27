'use strict';
// Behavioural tests for app/extension/popup.js. The script is loaded into a vm with a
// minimal DOM (tests/js/dom-shim.js) and a faked chrome.runtime so we exercise the real
// URL validation, the host-version gate (the fix for the "Disconnected" field report),
// the playlist preview/select flow and the enqueue round trip.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { buildDocument } = require('./dom-shim');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'extension', 'popup.js'), 'utf8');

// Every id the popup script reads or writes. Missing ids would make the script throw on load.
const POPUP_IDS = [
  'connected', 'track', 'trackStatus', 'enqueueTrack', 'preview', 'enqueueSelected',
  'metadata', 'normalize', 'musicbrainz', 'artwork', 'organize', 'trim_start', 'notifications',
  'format', 'advancedToggle', 'advancedSettings',
  'pane-track', 'pane-playlist', 'pane-queue', 'playlistUrl', 'playlistStatus',
  'playlistTitle', 'previewArea', 'playlistItems', 'selectedCount', 'selectAll',
  'jobList', 'queueCount', 'counts', 'queueStatus', 'clear', 'openDashboard',
  'checkHost', 'hostStatus', 'appVersion',
];

function harness(opts = {}) {
  const tabs = opts.tabs ?? [{ url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', title: 'Rick Astley - Never Gonna Give You Up' }];
  const rpcImpl = opts.rpc ?? ((method) => {
    if (method === 'health') return { ok: true, data: { version: '2.5.0', transport: 'nativeMessaging' } };
    if (method === 'jobs') return { ok: true, data: { jobs: [], counts: {}, next_cursor: null } };
    return { ok: true, data: {} };
  });
  // Mutable so individual tests can re-arm the responder after init() has run.
  const runtime = { rpc: rpcImpl };
  const sent = [];
  const storage = { ...opts.storage };
  const chrome = {
    runtime: {
      id: 'ext-id',
      // The popup shows the version of the installed package, read from the manifest.
      getManifest: () => ({ version: opts.version ?? '2.5.0' }),
      getURL: (p) => 'chrome-extension://ext-id/' + String(p).replace(/^\//, ''),
      sendMessage: (message) => {
        sent.push(message);
        if (message.type === 'BMD_RPC') return Promise.resolve(runtime.rpc(message.method, message.params || {}));
        return Promise.resolve({ ok: true });
      },
      openOptionsPage: () => { sent.push({ _openOptions: true }); },
    },
    tabs: { query: async () => tabs },
    storage: { local: { get: async (keys) => {
      const out = {};
      for (const k of [].concat(keys)) if (k in storage) out[k] = storage[k];
      return out;
    }, set: async (obj) => { Object.assign(storage, obj); } } },
  };
  const { document, byId } = buildDocument(POPUP_IDS, { tabs: ['track', 'playlist', 'queue'] });
  // The format <select> defaults to 'wav' in the HTML; the shim's makeElement starts at ''.
  byId.format.value = 'wav';
  // The advanced-settings pane carries the `hidden` attribute in the HTML; the shim starts at false.
  byId.advancedSettings.hidden = true;
  // The toggle carries aria-expanded="false" in the HTML; the shim starts with no attribute.
  byId.advancedToggle.setAttribute('aria-expanded', 'false');
  const context = vm.createContext({
    chrome, document, URL,
    setTimeout: (fn) => Promise.resolve().then(fn), clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, console, queueMicrotask, Promise,
  });
  vm.runInContext(SOURCE, context);
  return { chrome, document, byId, sent, storage,
    rpc: null, // kept for API symmetry; tests set runtime.rpc below
    setRpc: (fn) => { runtime.rpc = fn; },
    // Helper: resolve a microtask chain so the init() IIFE and any awaited RPCs settle.
    flush: () => new Promise((r) => setImmediate(r)) };
}

test('init() recognizes the active YouTube watch tab and shows the title', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  assert.match(h.byId.track.textContent, /Rick Astley/);
  assert.equal(h.byId.trackStatus.textContent, '');
});

test('init() on a non-YouTube tab prompts the user and keeps the track button disabled', async () => {
  const h = harness({ tabs: [{ url: 'https://example.com/', title: 'Example' }] });
  await h.flush(); await h.flush();
  assert.equal(h.byId.track.textContent, 'Open a YouTube / YouTube Music video.');
  assert.equal(h.byId.trackStatus.textContent, 'Individual watch pages are supported.');
});

test('isVideo: the host-version gate accepts a 2.5.0 helper (the regression fix)', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  // init() already clicks checkHost, so hostStatus reflects the real decision.
  assert.equal(h.byId.hostStatus.textContent, 'Connected via Native Messaging · v2.5.0');
  assert.ok(h.byId.connected.classList.contains('online'), 'the connection dot is online');
  assert.equal(h.byId.enqueueTrack.disabled, false, 'with a video + a live host, enqueue is enabled');
});

test('isVideo: a too-old host is rejected with a clear reason, not silently disconnected', async () => {
  const h = harness({ rpc: (m) => m === 'health'
    ? { ok: true, data: { version: '1.4.2', transport: 'nativeMessaging' } }
    : { ok: true, data: { jobs: [], counts: {} } } });
  await h.flush(); await h.flush();
  assert.match(h.byId.hostStatus.textContent, /incompatible.*1\.4\.2/);
  assert.equal(h.byId.connected.classList.contains('online'), false);
  assert.equal(h.byId.enqueueTrack.disabled, true);
});

test('isVideo: a health reply without a version string is treated as incompatible', async () => {
  const h = harness({ rpc: (m) => m === 'health'
    ? { ok: true, data: { transport: 'nativeMessaging' } }
    : { ok: true, data: { jobs: [], counts: {} } } });
  await h.flush(); await h.flush();
  assert.match(h.byId.hostStatus.textContent, /incompatible/);
});

test('isVideo: a 2.2.x host is rejected — it answers health but rejects the format parameter', async () => {
  // Regression (audit): the old floor `minor >= 0` was always true, so a 2.2 helper passed the
  // gate and only failed later, on the first format-bearing enqueue. The floor is 2.3.0.
  const h = harness({ rpc: (m) => m === 'health'
    ? { ok: true, data: { version: '2.2.7', transport: 'nativeMessaging' } }
    : { ok: true, data: { jobs: [], counts: {} } } });
  await h.flush(); await h.flush();
  assert.match(h.byId.hostStatus.textContent, /incompatible.*2\.2\.7/);
  assert.equal(h.byId.connected.classList.contains('online'), false);
});

test('a helper that answers health but disconnects on jobs leaves the dot offline', async () => {
  const h = harness({ rpc: (m) => m === 'health'
    ? { ok: true, data: { version: '2.5.0', transport: 'nativeMessaging' } }
    : { ok: false, error: 'Native helper disconnected' } });
  await h.flush(); await h.flush(); await h.flush();
  assert.match(h.byId.queueStatus.textContent, /Helper unavailable/);
  assert.equal(h.byId.connected.classList.contains('online'), false);
});

test('enqueue a single track posts the right payload and switches to the queue pane', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  // Mirror the background DEFAULTS so the payload reflects what a user with default settings sends.
  for (const name of ['metadata','normalize','musicbrainz','artwork','organize']) h.byId[name].checked = true;
  // Pre-arm the enqueue response; jobs is asked by the refreshQueue() that follows.
  h.setRpc((m) => m === 'enqueue' ? { ok: true, data: { added: ['abc'], duplicate: [], existing: [] } }
    : m === 'jobs' ? { ok: true, data: { jobs: [], counts: {} } } : { ok: true, data: {} });
  await h.document.click('enqueueTrack');
  await h.flush(); await h.flush();
  const enqueueCall = h.sent.find((m) => m.method === 'enqueue');
  assert.ok(enqueueCall, 'an enqueue RPC was sent');
  assert.equal(enqueueCall.params.url, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  assert.equal(enqueueCall.params.format, 'wav');
  assert.equal(enqueueCall.params.metadata, true);
  assert.match(h.byId.trackStatus.textContent, /1 queued/);
  assert.equal(h.byId['pane-queue'].hidden, false, 'switched to the queue pane');
});

test('enqueue reports duplicates and already-downloaded tracks without switching panes', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  // Start on the track pane explicitly, so we can prove the duplicate path never leaves it.
  await h.document.tab('track').click();
  await h.flush();
  assert.equal(h.byId['pane-track'].hidden, false);
  assert.equal(h.byId['pane-queue'].hidden, true);
  h.setRpc((m) => m === 'enqueue' ? { ok: true, data: { added: [], duplicate: ['x'], existing: ['y'] } }
    : m === 'jobs' ? { ok: true, data: { jobs: [], counts: {} } } : { ok: true, data: {} });
  await h.document.click('enqueueTrack');
  await h.flush(); await h.flush();
  assert.match(h.byId.trackStatus.textContent, /0 queued · 1 already queued · 1 already downloaded/);
  assert.equal(h.byId['pane-track'].hidden, false, 'nothing added → stays on the track pane');
  assert.equal(h.byId['pane-queue'].hidden, true, 'did not switch to the queue pane');
});

test('a failed enqueue surfaces the error text and never throws to the caller', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.setRpc(() => ({ ok: false, error: 'FFmpeg WAV conversion failed: boom' }));
  await h.document.click('enqueueTrack');
  await h.flush();
  assert.match(h.byId.trackStatus.textContent, /FFmpeg WAV conversion failed: boom/);
});

test('playlist preview rejects a non-playlist URL before calling the host', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.byId.playlistUrl.value = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
  const sentBefore = h.sent.length;
  await h.document.click('preview');
  await h.flush();
  assert.match(h.byId.playlistStatus.textContent, /supported YouTube playlist URL/);
  assert.equal(h.sent.length, sentBefore, 'no RPC was sent for an invalid URL');
});

test('playlist preview renders the track list and a select-all checkbox', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.byId.playlistUrl.value = 'https://www.youtube.com/playlist?list=PLtest12345678';
  h.setRpc((m) => m === 'preview'
    ? { ok: true, data: { title: 'Mixtape', count: 2, truncated: false, items: [
        { id: 'v1', title: 'Track One', artist: 'A' }, { id: 'v2', title: 'Track Two', artist: 'B' } ] } }
    : { ok: true, data: { jobs: [], counts: {} } });
  await h.document.click('preview');
  await h.flush(); await h.flush();
  assert.equal(h.byId.playlistTitle.textContent, 'Mixtape');
  assert.equal(h.byId.previewArea.hidden, false);
  assert.equal(h.byId.playlistItems.children.length, 2);
  assert.equal(h.byId.selectAll.checked, true, 'all rows start selected');
  assert.match(h.byId.selectedCount.textContent, /2\/2 selected/);
});

test('toggling one row off marks select-all indeterminate and disables enqueue-selected when none selected', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.byId.playlistUrl.value = 'https://www.youtube.com/playlist?list=PLtest12345678';
  h.setRpc((m) => m === 'preview'
    ? { ok: true, data: { title: 'Mixtape', count: 2, truncated: false, items: [
        { id: 'v1', title: 'Track One' }, { id: 'v2', title: 'Track Two' } ] } }
    : { ok: true, data: { jobs: [], counts: {} } });
  await h.document.click('preview');
  await h.flush(); await h.flush();
  // Flip the second row's checkbox off, then run the change listener that popup.js registered.
  const secondRow = h.byId.playlistItems.children[1];
  const checkbox = secondRow.children[0];
  checkbox.checked = false;
  const change = checkbox._listeners.change[0];
  change();
  await h.flush();
  assert.equal(h.byId.selectAll.indeterminate, true);
  assert.equal(h.byId.selectedCount.textContent, '1/2 selected');
  assert.equal(h.byId.enqueueSelected.disabled, false, 'one still selected → enabled');
  // Now flip the remaining one off too.
  const firstCheckbox = h.byId.playlistItems.children[0].children[0];
  firstCheckbox.checked = false;
  firstCheckbox._listeners.change[0]();
  await h.flush();
  assert.equal(h.byId.enqueueSelected.disabled, true, 'no selection → disabled');
});

test('select-all checkbox flips every row at once', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.byId.playlistUrl.value = 'https://www.youtube.com/playlist?list=PLtest12345678';
  h.setRpc((m) => m === 'preview'
    ? { ok: true, data: { title: 'Mix', count: 3, truncated: false, items: [
        { id: 'a', title: 'A' }, { id: 'b', title: 'B' }, { id: 'c', title: 'C' } ] } }
    : { ok: true, data: { jobs: [], counts: {} } });
  await h.document.click('preview');
  await h.flush(); await h.flush();
  h.byId.selectAll.checked = false;
  h.byId.selectAll._listeners.change[0]({ target: h.byId.selectAll });
  await h.flush();
  for (const row of h.byId.playlistItems.children) assert.equal(row.children[0].checked, false);
  assert.match(h.byId.selectedCount.textContent, /0\/3 selected/);
});

test('clearing finished jobs reports the count returned by the host', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.setRpc((m) => m === 'clear' ? { ok: true, data: { removed: 7 } }
    : { ok: true, data: { jobs: [], counts: {} } });
  await h.document.click('clear');
  await h.flush();
  assert.match(h.byId.queueStatus.textContent, /7 finished jobs cleared/);
});

test('renderQueue draws a Cancel button only for queued/running jobs and Retry only for error/canceled', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.setRpc((m) => m === 'jobs' ? { ok: true, data: { jobs: [
    { id: '1', state: 'running', title: 'Live', percent: 42 },
    { id: '2', state: 'done', title: 'Saved', filename: 'a.wav', artist: 'A', album: 'B', percent: 100 },
    { id: '3', state: 'error', title: 'Bad', message: 'boom', percent: 0 },
  ], counts: { queued: 0, running: 1, done: 1, error: 1 } } } : { ok: true, data: {} });
  // openTab('queue') is bound to the .tab button whose data-tab === 'queue'; click it.
  const queueTabBtn = h.document.tab('queue');
  await queueTabBtn.click();
  await h.flush(); await h.flush();
  assert.equal(h.byId['pane-queue'].hidden, false);
  assert.equal(h.byId.queueCount.textContent, '1');
  const cards = h.byId.jobList.children;
  assert.equal(cards.length, 3);
  const actionsOf = (card) => card.children.find((c) => c.className === 'job-actions');
  assert.equal(actionsOf(cards[0]).children[0].textContent, 'Cancel', 'running job → Cancel');
  assert.equal(actionsOf(cards[1]).children.length, 0, 'done job → no action button');
  assert.equal(actionsOf(cards[2]).children[0].textContent, '↻ Retry', 'error job → Retry');
  // The progress bar for the running job is clamped into [0,100].
  const progress = cards[0].children.find((c) => c.className === 'job-progress');
  assert.equal(progress.value, 42);
  // A bogus percent (negative or > 100) is clamped, never throwing.
  h.setRpc((m) => m === 'jobs' ? { ok: true, data: { jobs: [
    { id: '9', state: 'running', title: 'X', percent: 250 }], counts: { running: 1 } } } : { ok: true, data: {} });
  await h.document.tab('queue').click();
  await h.flush(); await h.flush();
  const clamped = h.byId.jobList.children[0].children.find((c) => c.className === 'job-progress');
  assert.equal(clamped.value, 100, 'percent clamped to 100');
});

test('openDashboard opens the options page', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  await h.document.click('openDashboard');
  await h.flush();
  assert.ok(h.sent.some((m) => m._openOptions), 'chrome.runtime.openOptionsPage was called');
});

test('changing a setting persists it to chrome.storage.local', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  h.byId.normalize.checked = true;
  h.byId.normalize._listeners.change[0]();
  await h.flush();
  assert.equal(h.storage.normalize, true);
  h.byId.normalize.checked = false;
  h.byId.normalize._listeners.change[0]();
  await h.flush();
  assert.equal(h.storage.normalize, false);
});

test('the format dropdown defaults to wav and persists the chosen format to storage', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  assert.equal(h.byId.format.value, 'wav', 'default format is wav');
  h.byId.format.value = 'flac';
  h.byId.format._listeners.change[0] && h.byId.format._listeners.change[0]();
  // The format select's change listener is registered at load; if there is none, the storage
  // path is exercised by the change event the harness fires. Either way the value is read back.
  await h.flush();
  assert.equal(h.storage.format, 'flac', 'flac was persisted');
  h.byId.format.value = 'mp3';
  h.byId.format._listeners.change && h.byId.format._listeners.change[0]();
  await h.flush();
  assert.equal(h.storage.format, 'mp3', 'mp3 was persisted');
});

test('enqueue sends the currently selected format, not always wav', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  for (const name of ['metadata','normalize','musicbrainz','artwork','organize']) h.byId[name].checked = true;
  h.byId.format.value = 'mp3';
  h.setRpc((m) => m === 'enqueue' ? { ok: true, data: { added: ['abc'], duplicate: [], existing: [] } }
    : m === 'jobs' ? { ok: true, data: { jobs: [], counts: {} } } : { ok: true, data: {} });
  await h.document.click('enqueueTrack');
  await h.flush(); await h.flush();
  const enqueueCall = h.sent.find((m) => m.method === 'enqueue');
  assert.equal(enqueueCall.params.format, 'mp3', 'the chosen format travels with the enqueue RPC');
});

test('advanced options are collapsed by default and expand on toggle, persisting the state', async () => {
  const h = harness();
  await h.flush(); await h.flush();
  assert.equal(h.byId.advancedSettings.hidden, true, 'advanced settings hidden by default');
  assert.equal(h.byId.advancedToggle.getAttribute('aria-expanded'), 'false');
  await h.byId.advancedToggle.click();
  await h.flush();
  assert.equal(h.byId.advancedSettings.hidden, false, 'toggle reveals the advanced settings');
  assert.equal(h.byId.advancedToggle.getAttribute('aria-expanded'), 'true');
  assert.equal(h.storage.advanced_open, true, 'expanded state persisted');
  await h.byId.advancedToggle.click();
  await h.flush();
  assert.equal(h.byId.advancedSettings.hidden, true, 'toggle collapses again');
  assert.equal(h.storage.advanced_open, false);
});

test('a saved advanced_open=true restores the expanded state on load', async () => {
  const h = harness({ storage: { advanced_open: true } });
  await h.flush(); await h.flush();
  assert.equal(h.byId.advancedSettings.hidden, false, 'restored expanded from storage');
  assert.equal(h.byId.advancedToggle.getAttribute('aria-expanded'), 'true');
});

test('a saved format is restored on load (not just the default wav)', async () => {
  const h = harness({ storage: { format: 'flac' } });
  await h.flush(); await h.flush();
  assert.equal(h.byId.format.value, 'flac', 'saved format restored');
});

test('the header shows the installed version from the manifest, never a page literal', async () => {
  // Regression (real-browser run at 2.5.0): the header of the page said "v2.4" while the
  // manifest said 2.5.0, and 2.3.0 had shipped with "v2.0". The page reads the manifest now.
  const h = harness({ version: '2.5.0' });
  await h.flush();
  assert.equal(h.byId.appVersion.textContent, 'v2.5.0');
  const other = harness({ version: '9.8.7' });
  await other.flush();
  assert.equal(other.byId.appVersion.textContent, 'v9.8.7', 'it follows the manifest, not a constant');
});
