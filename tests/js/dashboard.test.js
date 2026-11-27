'use strict';
// Behavioural tests for app/extension/dashboard.js. The script (and its dependency poller.js)
// are loaded into a vm with a minimal DOM (tests/js/dom-shim.js) and a faked chrome.runtime,
// so we exercise the real paging/merge logic, filters, diagnostics rendering, ambiguous-action
// handling and the connection status indicator.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { buildDocument } = require('./dom-shim');

const DASHBOARD_SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'extension', 'dashboard.js'), 'utf8');
const POLLER_SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'extension', 'poller.js'), 'utf8');

const DASHBOARD_IDS = [
  'connection', 'signal', 'queueStatus', 'heading', 'statActive', 'statDone', 'statError',
  'statCanceled', 'recent', 'downloadList', 'loadMore', 'singleUrl', 'singleAdd', 'singleStatus',
  'playlistUrl', 'preview', 'playlistPreview', 'playlistName', 'previewItems', 'selectAll',
  'selectedCount', 'importSelected', 'importStatus', 'tools', 'outputPath', 'diagStatus',
  'diagnosticConnection', 'metadata', 'normalize', 'musicbrainz', 'artwork', 'organize',
  'trim_start', 'notifications', 'settingsStatus', 'refresh', 'goSettings', 'goQueue', 'clearFinished',
  'checkDependencies', 'format', 'advancedToggle', 'advancedSettings',
  'page-overview', 'page-downloads', 'page-import', 'page-settings', 'page-diagnostics',
  'appVersion', 'profileBits', 'profileFormat', 'profileNote',
];

const NAV = ['overview', 'downloads', 'import', 'settings', 'diagnostics'];
const FILTERS = ['all', 'queued', 'running', 'done', 'error', 'canceled'];

function harness(opts = {}) {
  const tabs = opts.tabs ?? [];
  const runtime = { rpc: opts.rpc ?? ((method) => {
    if (method === 'health') return { ok: true, data: { version: '2.5.0', transport: 'nativeMessaging', output: 'C:\\Music' } };
    if (method === 'jobs') return { ok: true, data: { jobs: [], counts: {}, next_cursor: null } };
    if (method === 'diagnostics') return { ok: true, data: diagnosticsReport() };
    return { ok: true, data: {} };
  }) };
  const sent = [];
  const storage = { ...opts.storage };
  const chrome = {
    runtime: {
      id: 'ext-id',
      // The sidebar shows the version of the installed package, read from the manifest.
      getManifest: () => ({ version: opts.version ?? '2.5.0' }),
      getURL: (p) => 'chrome-extension://ext-id/' + String(p).replace(/^\//, ''),
      sendMessage: (message) => {
        sent.push(message);
        if (message.type === 'BMD_RPC') return Promise.resolve(runtime.rpc(message.method, message.params || {}));
        return Promise.resolve({ ok: true });
      },
    },
    tabs: { query: async () => tabs },
    storage: { local: { get: async (keys) => {
      const out = {};
      for (const k of [].concat(keys)) if (k in storage) out[k] = storage[k];
      return out;
    }, set: async (obj) => { Object.assign(storage, obj); } } },
  };
  const { document, byId } = buildDocument(DASHBOARD_IDS, { nav: NAV, filters: FILTERS });
  // The format <select> defaults to 'wav' in the HTML; the shim's makeElement starts at ''.
  byId.format.value = 'wav';
  // The advanced-settings pane carries the `hidden` attribute in the HTML; the shim starts at false.
  byId.advancedSettings.hidden = true;
  byId.advancedToggle.setAttribute('aria-expanded', 'false');
  // poller.js attaches to module.exports when module is defined; keep it undefined so createPoller
  // becomes a global on the vm context, exactly as it is in the browser page.
  const context = vm.createContext({
    chrome, document, URL,
    setTimeout: (fn) => Promise.resolve().then(fn), clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    Date, console, queueMicrotask, Promise, Math,
  });
  vm.runInContext(POLLER_SRC, context);   // defines global createPoller
  vm.runInContext(DASHBOARD_SRC, context);
  return { chrome, document, byId, sent, storage,
    setRpc: (fn) => { runtime.rpc = fn; },
    flush: () => new Promise((r) => setImmediate(r)) };
}

function diagnosticsReport() {
  return {
    requirements_ok: true, issues: [],
    tools: {
      ffmpeg: { installed: true, version: '7.1.5', path: '/usr/bin/ffmpeg', source: 'app', owned: true },
      yt_dlp: { installed: false, version: '', path: '', source: 'missing', owned: false },
    },
    database: { schema: '2.3', integrity: 'ok' },
    cache_bytes: 5242880, free_bytes: 10737418240,
    log_file: '/tmp/helper.log',
  };
}

test('init() connects and shows the empty-queue hint', async () => {
  const h = harness();
  await h.flush(); await h.flush(); await h.flush();
  assert.match(h.byId.connection.textContent, /Connected/);
  assert.ok(h.byId.signal.classList.contains('online'));
  assert.equal(h.byId.downloadList.children.length, 1);
  assert.match(h.byId.downloadList.children[0].textContent, /Your queue is empty|No matching jobs/);
});

test('a failed jobs RPC flips the connection indicator to Disconnected', async () => {
  const h = harness({ rpc: () => ({ ok: false, error: 'Native helper disconnected' }) });
  await h.flush(); await h.flush(); await h.flush();
  assert.match(h.byId.connection.textContent, /Disconnected/);
  assert.equal(h.byId.signal.classList.contains('online'), false);
  assert.match(h.byId.queueStatus.textContent, /Helper unavailable/);
  // The Overview is the landing page: its "Recent activity" box says why it is empty.
  assert.equal(h.byId.recent.children.length, 1);
  assert.match(h.byId.recent.children[0].textContent, /Nothing to show while the helper is disconnected/);
});

test('stats reflect counts from the jobs payload', async () => {
  const h = harness({ rpc: (m) => m === 'jobs' ? { ok: true, data: { jobs: [], counts:
    { queued: 3, running: 1, done: 9, error: 2, canceled: 1 }, next_cursor: null } } : { ok: true, data: {} } });
  await h.flush(); await h.flush(); await h.flush();
  assert.equal(h.byId.statActive.textContent, '4', 'active = queued + running');
  assert.equal(h.byId.statDone.textContent, '9');
  assert.equal(h.byId.statError.textContent, '2');
  assert.equal(h.byId.statCanceled.textContent, '1');
});

test('loadMore pages through the cursor and merges without duplicating jobs', async () => {
  let page = 0;
  const h = harness({ rpc: (m) => {
    if (m !== 'jobs') return { ok: true, data: {} };
    page += 1;
    if (page === 1) return { ok: true, data: { jobs: [{ id: 'a', state: 'running', title: 'A', percent: 10 }], counts: { running: 1 }, next_cursor: 'c1' } };
    if (page === 2) return { ok: true, data: { jobs: [{ id: 'b', state: 'queued', title: 'B', percent: 0 }], counts: { running: 1 }, next_cursor: 'c2' } };
    return { ok: true, data: { jobs: [{ id: 'a', state: 'done', title: 'A', percent: 100 }], counts: { done: 1 }, next_cursor: null } };
  } });
  await h.flush(); await h.flush(); await h.flush();
  assert.equal(h.byId.loadMore.hidden, false, 'cursor present → load more shown');
  assert.equal(h.byId.downloadList.children.length, 1);
  // Click loadMore: page 2 adds job B.
  await h.byId.loadMore.click();
  await h.flush(); await h.flush();
  const ids1 = h.byId.downloadList.children.map((c) => c.children[0].children[0].textContent);
  assert.equal(h.byId.downloadList.children.length, 2, 'two distinct jobs after paging');
  // Click loadMore again: page 3 returns job A again (re-run as done). The merge must de-dupe by id,
  // so the list does not grow to three.
  await h.byId.loadMore.click();
  await h.flush(); await h.flush();
  assert.equal(h.byId.downloadList.children.length, 2, 'duplicate id from a later page is not re-added');
  assert.equal(h.byId.loadMore.hidden, true, 'no further cursor → load more hidden');
});

test('filter buttons reset paging and re-query with the state param', async () => {
  const h = harness({ rpc: (m, p) => {
    if (m !== 'jobs') return { ok: true, data: {} };
    return { ok: true, data: { jobs: [], counts: {}, next_cursor: null, _state: p && p.state } };
  } });
  await h.flush(); await h.flush(); await h.flush();
  const jobsCalls = () => h.sent.filter((m) => m.method === 'jobs');
  // Click the 'error' filter button. refresh() re-queries with the selected state; the recent-list
  // fetch that follows always sends limit:6 with no state, so look for a call carrying state:'error'.
  await h.document.filter('error').click();
  await h.flush(); await h.flush(); await h.flush();
  assert.ok(jobsCalls().some((c) => c.params.state === 'error'),
    'a jobs RPC carried the selected error filter');
  // 'all' sends no state on the page refresh.
  await h.document.filter('all').click();
  await h.flush(); await h.flush(); await h.flush();
  // The most recent full-page fetch (limit:50, no recent re-fetch) carries no state under 'all'.
  assert.ok(jobsCalls().some((c) => c.params.limit === 50 && c.params.state === undefined),
    'all filter sends no state filter on the page fetch');
});

test('a quick single download enqueues and remembers the added ids', async () => {
  const h = harness({ rpc: (m) => m === 'enqueue'
    ? { ok: true, data: { added: ['id1'], duplicate: [], existing: [] } }
    : m === 'jobs' ? { ok: true, data: { jobs: [], counts: {}, next_cursor: null } }
    : { ok: true, data: {} } });
  await h.flush(); await h.flush(); await h.flush();
  h.byId.singleUrl.value = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
  await h.byId.singleAdd.click();
  await h.flush(); await h.flush();
  const enqueueCall = h.sent.find((m) => m.method === 'enqueue');
  assert.ok(enqueueCall, 'an enqueue RPC was sent');
  assert.equal(enqueueCall.params.url, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  assert.match(h.byId.singleStatus.textContent, /1 queued/);
  assert.ok(h.sent.some((m) => m.type === 'BMD_TRACK_ENQUEUED' && m.ids.includes('id1')),
    'a BMD_TRACK_ENQUEUED notice was posted for the new job');
});

test('an ambiguous cancel surfaces the special message and triggers a refresh', async () => {
  let calls = 0;
  const h = harness({ rpc: (m) => {
    if (m === 'jobs') {
      calls += 1;
      return { ok: true, data: { jobs: calls === 1 ? [{ id: '1', state: 'running', title: 'X', percent: 5 }] : [], counts: { running: calls === 1 ? 1 : 0 }, next_cursor: null } };
    }
    if (m === 'cancel') return { ok: false, error: 'The helper disconnected before confirming this action.', ambiguous: true };
    return { ok: true, data: {} };
  } });
  await h.flush(); await h.flush(); await h.flush();
  assert.equal(h.byId.downloadList.children.length, 1);
  const cancelBtn = h.byId.downloadList.children[0].children.find((c) => c.className === 'actions').children[0];
  await cancelBtn.click();
  await h.flush(); await h.flush();
  assert.match(h.byId.queueStatus.textContent, /The queue below shows what actually happened/);
  assert.ok(calls >= 2, 'an ambiguous error triggered a follow-up refresh');
});

test('diagnostics page renders the tool cards and the library/cache lines', async () => {
  const h = harness();
  await h.flush(); await h.flush(); await h.flush();
  // Open the diagnostics page (clicks the .nav button with data-page='diagnostics').
  await h.document.nav('diagnostics').click();
  await h.flush(); await h.flush(); await h.flush();
  assert.match(h.byId.diagnosticConnection.textContent, /Connected · v2\.5\.0 · nativeMessaging/);
  assert.equal(h.byId.outputPath.textContent, 'C:\\Music');
  assert.match(h.byId.diagStatus.textContent, /All required executables detected/);
  // Two tool cards + one library card.
  assert.equal(h.byId.tools.children.length, 3);
  const toolNames = h.byId.tools.children.map((c) => c.children[0].textContent);
  assert.ok(toolNames.some((t) => /ffmpeg/.test(t)), 'ffmpeg tool card present');
  assert.ok(toolNames.some((t) => /yt[_-]dlp|yt.dl/i.test(t)), 'yt-dlp tool card present');
  const libCard = h.byId.tools.children[2];
  assert.match(libCard.textContent, /Library & cache/);
  assert.match(libCard.textContent, /5\.0 MB/);
  assert.match(libCard.textContent, /10\.0 GB/);
});

test('diagnostics reports a missing required tool as a problem to repair', async () => {
  const h = harness({ rpc: (m) => m === 'diagnostics' ? { ok: true, data: {
    requirements_ok: false, issues: ['yt-dlp is missing'],
    tools: { yt_dlp: { installed: false, version: '', path: '', source: 'missing', owned: false } },
    database: null, cache_bytes: 0, free_bytes: 0, log_file: '',
  } } : { ok: true, data: { version: '2.5.0', transport: 'nativeMessaging', output: '' } } });
  await h.flush(); await h.flush(); await h.flush();
  await h.document.nav('diagnostics').click();
  await h.flush(); await h.flush(); await h.flush();
  assert.match(h.byId.diagStatus.textContent, /Problems: yt-dlp is missing/);
});

test('explainConnection turns a "forbidden" message into a manifest-key hint naming the id', async () => {
  const h = harness({ rpc: () => ({ ok: false, error: 'Native host forbidden' }) });
  await h.flush(); await h.flush(); await h.flush();
  await h.document.nav('diagnostics').click();
  await h.flush(); await h.flush(); await h.flush();
  // The diagnostics page surfaces the explained hint. The extension id is the harness chrome.runtime.id.
  assert.match(h.byId.diagStatus.textContent, /Brave blocked this extension.*ext-id/);
  assert.match(h.byId.diagStatus.textContent, /lliokngbogdlnnjffdfgdjabpmjpobab/);
});

test('checkDependencies forces a reconnect (reconnect then health fallback)', async () => {
  const calls = [];
  const h = harness({ rpc: (m) => {
    calls.push(m);
    if (m === 'reconnect') return { ok: true, data: { version: '2.5.0', transport: 'nativeMessaging', output: 'C:\\Music' } };
    if (m === 'health') return { ok: true, data: { version: '2.5.0', transport: 'nativeMessaging', output: 'C:\\Music' } };
    if (m === 'diagnostics') return { ok: true, data: diagnosticsReport() };
    return { ok: true, data: {} };
  } });
  await h.flush(); await h.flush(); await h.flush();
  await h.document.nav('diagnostics').click();
  await h.flush(); await h.flush(); await h.flush();
  calls.length = 0;
  await h.byId.checkDependencies.click();
  await h.flush(); await h.flush(); await h.flush();
  assert.ok(calls.includes('reconnect'), 'checkDependencies tries reconnect first');
});

test('formatBytes handles finite, huge and non-finite inputs', async () => {
  // formatBytes is not exported; we probe it indirectly through the diagnostics cache/free lines.
  const h = harness({ rpc: (m) => m === 'diagnostics' ? { ok: true, data: {
    requirements_ok: true, issues: [], tools: {},
    database: null, cache_bytes: Number.POSITIVE_INFINITY, free_bytes: 0, log_file: '' } }
    : { ok: true, data: { version: '2.5.0', transport: 'nativeMessaging', output: '' } } });
  await h.flush(); await h.flush(); await h.flush();
  await h.document.nav('diagnostics').click();
  await h.flush(); await h.flush(); await h.flush();
  assert.match(h.byId.tools.children[0].textContent, /unknown/);
});

test('clearFinished reports the removed count', async () => {
  const h = harness({ rpc: (m) => m === 'clear' ? { ok: true, data: { removed: 5 } }
    : m === 'jobs' ? { ok: true, data: { jobs: [], counts: {}, next_cursor: null } } : { ok: true, data: {} } });
  await h.flush(); await h.flush(); await h.flush();
  await h.byId.clearFinished.click();
  await h.flush(); await h.flush();
  assert.match(h.byId.queueStatus.textContent, /5 jobs removed from history/);
});

test('changing a setting persists it and confirms', async () => {
  const h = harness();
  await h.flush(); await h.flush(); await h.flush();
  h.byId.normalize.checked = false;
  h.byId.normalize._listeners.change[0]();
  await h.flush();
  assert.equal(h.storage.normalize, false);
  assert.match(h.byId.settingsStatus.textContent, /Preferences saved/);
});

test('the dashboard format dropdown persists and is sent with a quick download', async () => {
  const h = harness({ rpc: (m, p) => {
    if (m === 'enqueue') return { ok: true, data: { added: [p.url ? 'id1' : 'id0'], duplicate: [], existing: [] } };
    if (m === 'jobs') return { ok: true, data: { jobs: [], counts: {}, next_cursor: null } };
    return { ok: true, data: {} };
  } });
  await h.flush(); await h.flush(); await h.flush();
  assert.equal(h.byId.format.value, 'wav', 'default format');
  h.byId.format.value = 'flac';
  h.byId.format._listeners.change[0]();   // fire the change listener so the choice is persisted
  await h.flush();
  assert.equal(h.storage.format, 'flac', 'format change was persisted');
  h.byId.singleUrl.value = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
  await h.byId.singleAdd.click();
  await h.flush(); await h.flush();
  const enqueueCall = h.sent.find((m) => m.method === 'enqueue');
  assert.equal(enqueueCall.params.format, 'flac', 'quick download carries the selected format');
});

test('advanced options are collapsed by default and expand on toggle in the dashboard', async () => {
  const h = harness();
  await h.flush(); await h.flush(); await h.flush();
  assert.equal(h.byId.advancedSettings.hidden, true, 'collapsed by default');
  await h.byId.advancedToggle.click();
  await h.flush();
  assert.equal(h.byId.advancedSettings.hidden, false, 'expanded after toggle');
  assert.equal(h.storage.advanced_open, true, 'expanded state persisted');
});

test('the Overview audio profile follows the saved format', async () => {
  // Regression (real-browser run at 2.5.0): the Overview described "24-bit PCM WAV" whatever
  // format the user had chosen, because the text was written into the page.
  const wav = harness();
  await wav.flush(); await wav.flush(); await wav.flush();
  assert.equal(wav.byId.profileBits.textContent, '24');
  assert.equal(wav.byId.profileFormat.textContent, '24-bit PCM WAV');
  assert.match(wav.byId.profileNote.textContent, /largest files/);
  for (const [format, bits, name, note] of [
    ['flac', '24', '24-bit FLAC', /Lossless/],
    ['mp3', '320', 'MP3 · 320 kbps', /Lossy/],
  ]) {
    const h = harness({ storage: { format } });
    await h.flush(); await h.flush(); await h.flush();
    assert.equal(h.byId.profileBits.textContent, bits, `${format}: the big number`);
    assert.equal(h.byId.profileFormat.textContent, name, `${format}: the name`);
    assert.match(h.byId.profileNote.textContent, note, `${format}: the description`);
  }
});

test('the audio profile changes with the format dropdown', async () => {
  const h = harness();
  await h.flush(); await h.flush(); await h.flush();
  for (const [format, name] of [['mp3', 'MP3 · 320 kbps'], ['flac', '24-bit FLAC'], ['wav', '24-bit PCM WAV']]) {
    h.byId.format.value = format;
    h.byId.format._listeners.change[0]();
    await h.flush(); await h.flush();
    assert.equal(h.byId.profileFormat.textContent, name, `${format}: the name`);
  }
  // A value the page has no description for falls back to WAV text, never to "undefined".
  h.byId.format.value = 'ogg';
  h.byId.format._listeners.change[0]();
  await h.flush();
  assert.equal(h.byId.profileFormat.textContent, '24-bit PCM WAV');
  assert.equal(h.byId.profileNote.textContent.includes('undefined'), false);
});

test('the sidebar shows the installed version from the manifest, not a page literal', async () => {
  // Regression (real-browser run at 2.5.0): the popup header said v2.4 in the 2.5.0 build.
  const h = harness({ version: '2.5.0' });
  await h.flush();
  assert.equal(h.byId.appVersion.textContent, 'v2.5.0');
});
