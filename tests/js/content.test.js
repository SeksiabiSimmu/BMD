'use strict';
// Behavioural tests for app/extension/content.js. The script is an IIFE that injects a "↓ Save"
// button into YouTube's watch/shorts pages and drives it from a MutationObserver plus SPA
// navigation events. We load it in a vm with a faked location, a stub MutationObserver and a
// scripted chrome.runtime so we exercise the real validity gating, button lifecycle and the
// BMD_ENQUEUE_CURRENT message contract the background service worker relies on.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { makeElement } = require('./dom-shim');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'extension', 'content.js'), 'utf8');

// A minimal MutationObserver: records the callback and the observed root, and exposes a fire()
// so tests can drive the observer the way YouTube's DOM mutations would.
class FakeObserver {
  constructor(fn) { this.fn = fn; this.root = null; }
  observe(root, _opts) { this.root = root; }
  disconnect() { this.fn = null; this.root = null; }
  fire() { if (this.fn) this.fn(); }
}

function makeLocation(href) {
  const u = new URL(href);
  // content.js reads location.href, .hostname and .pathname directly off window.location.
  return { href, hostname: u.hostname, pathname: u.pathname, searchParams: u.searchParams,
    toString: () => href };
}

function harness(opts = {}) {
  const href = opts.href ?? 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
  const documentEl = makeElement('html');
  documentEl.children = [];
  let titleVal = '';
  // The "host" is the YouTube actions container the script appends the button into. It is a
  // detached element we hand back through document.querySelector(); tests inspect it via h.host.
  const host = makeElement('div');
  const target = opts.target ?? (() => host);
  const sent = [];
  const messageResponder = opts.onMessage ?? (() => ({ added: true, message: 'Queued' }));
  const chrome = {
    runtime: {
      id: 'ext-id',
      sendMessage: (message) => { sent.push(message); return Promise.resolve(messageResponder(message)); },
    },
  };
  const location = makeLocation(href);
  // The script reads location.href / hostname / pathname. We expose `location` both as a global
  // and on window; the IIFE references bare `location`.
  let observer = null;
  const MutationObserver = class extends FakeObserver { constructor(fn) { super(fn); observer = this; } };
  const window = {
    location,
    addEventListener: (type, fn) => { (window._listeners[type] = window._listeners[type] || []).push(fn); },
    _listeners: {},
  };
  const document = {
    documentElement: documentEl,
    // getElementById must find the injected button so refresh() can detect an existing button and
    // remove it when the page becomes invalid. The button lives in the host target.
    getElementById: (id) => {
      const found = findById(host, id);
      return found || null;
    },
    createElement: makeElement,
    querySelector: (sel) => target(),
    get title() { return titleVal; }, set title(v) { titleVal = v; },
  };
  // setInterval: store the callback so we can fire it to simulate the periodic URL re-check.
  let intervalFn = null;
  const setInterval = (fn) => { intervalFn = fn; return 1; };
  const clearInterval = () => { intervalFn = null; };
  // setTimeout: the IIFE's debounced refresh() uses 280ms and must run so the button gets injected.
  // The click handler's 2400ms text-reset timer must NOT fire during a test (it would clobber the
  // ✓ Queued/✓ Exists/! Error state the test asserts). So auto-run only short timers (<= 350ms) and
  // record long ones so a test can inspect/flush them explicitly if needed.
  const longTimers = [];
  let nextTimer = 2;
  const setTimeout = (fn, ms = 0) => {
    const id = nextTimer++;
    if (Number(ms) <= 350) Promise.resolve().then(() => { try { fn(); } catch (e) { /* swallow */ } });
    else longTimers.push({ id, fn });
    return id;
  };
  const clearTimeout = (id) => {
    const i = longTimers.findIndex((t) => t.id === id);
    if (i !== -1) longTimers.splice(i, 1);
  };
  const context = vm.createContext({
    chrome, document, window, location, MutationObserver, URL,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, console, queueMicrotask, Promise,
  });
  vm.runInContext(SOURCE, context);
  return {
    chrome, document, sent, observer, host,
    // Find the injected button wherever it currently lives (host target).
    button: () => host.children.find((c) => c.id === 'brave-music-download-wav') || null,
    fireObserver: () => observer && observer.fire(),
    fireInterval: () => intervalFn && intervalFn(),
    fireWindowEvent: (type) => (window._listeners[type] || []).forEach((fn) => fn()),
    setHref: (newHref) => {
      const u = new URL(newHref);
      location.href = newHref; location.hostname = u.hostname;
      location.pathname = u.pathname; location.searchParams = u.searchParams;
    },
    // Resolve the pending setTimeout queued by a button click (the script uses a plain setTimeout
    // to reset the button text after 2.4s). We run it on the next microtask.
    flush: () => new Promise((r) => setImmediate(r)),
  };
}

function findById(root, id) {
  for (const child of root.children || []) {
    if (child.id === id) return child;
    const deeper = findById(child, id);
    if (deeper) return deeper;
  }
  return null;
}

test('on a valid watch page with a host target, the script injects exactly one ↓ Save button', async () => {
  const h = harness();
  await h.flush();
  const btn = h.button();
  assert.ok(btn, 'button was injected into the host target');
  assert.equal(btn.textContent, '↓ Save');
  assert.equal(h.host.children.length, 1, 'exactly one button appended');
});

test('on a non-YouTube URL with no video id, no button is ever injected', async () => {
  // content.js's valid() only checks pathname+v or /shorts/<11>; it does NOT check the hostname
  // (the background's canonicalVideo re-validates the host before downloading). So a URL must
  // lack a /watch?v= or /shorts/<id> path entirely to be invalid here.
  const h = harness({ href: 'https://example.com/feed/trending' });
  await h.flush();
  h.fireObserver(); await h.flush();
  assert.equal(h.host.children.length, 0);
  assert.equal(h.button(), null);
});

test('a watch page is valid only with an 11-char v= id; bad ids never inject', async () => {
  const h = harness({ href: 'https://www.youtube.com/watch?v=short' });
  await h.flush();
  assert.equal(h.host.children.length, 0, 'too-short v= id → no button');
  h.setHref('https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  h.fireObserver(); await h.flush();
  assert.equal(h.host.children.length, 1, 'once the URL becomes valid, the button appears');
});

test('shorts URLs (/shorts/<11>) are valid; other paths are not', async () => {
  const h = harness({ href: 'https://www.youtube.com/shorts/abcdefghijk' });
  await h.flush();
  assert.equal(h.host.children.length, 1, 'shorts URL injects');
  // Navigating to a non-watch, non-shorts path makes valid() false, so refresh() removes the button.
  h.setHref('https://www.youtube.com/feed/trending');
  h.fireObserver(); await h.flush();
  assert.equal(h.host.children.length, 0, 'non-watch, non-shorts path removes the button');
});

test('clicking the button sends BMD_ENQUEUE_CURRENT with the page title and shows ✓ Queued', async () => {
  const h = harness({ onMessage: () => ({ added: true, message: 'Queued for FLAC download.' }) });
  await h.flush();
  const btn = h.button();
  // content.js reads document.title when it builds the message.
  h.document.title = 'Rick Astley - Never Gonna Give You Up';
  await btn.click();
  await h.flush();
  const sent = h.sent[h.sent.length - 1];
  assert.equal(sent.type, 'BMD_ENQUEUE_CURRENT');
  assert.equal(sent.title, 'Rick Astley - Never Gonna Give You Up');
  assert.match(btn.textContent, /Queued/);
});

test('an already-queued track (added:false, existing) shows ✓ Exists', async () => {
  const h = harness({ onMessage: () => ({ added: false, message: 'Already saved in your library.' }) });
  await h.flush();
  const btn = h.button();
  await btn.click();
  await h.flush();
  assert.match(btn.textContent, /Exists/);
});

test('a rejected enqueue (host error) shows ! Error and keeps the title helpful', async () => {
  const h = harness({ onMessage: () => ({ error: 'Open a supported YouTube video before downloading.' }) });
  await h.flush();
  const btn = h.button();
  await btn.click();
  await h.flush();
  assert.match(btn.textContent, /Error/);
  assert.match(btn.title, /supported YouTube video/);
});

test('if sendMessage throws, the button shows ! Error without crashing the page', async () => {
  const h = harness({ onMessage: () => { throw new Error('Extension context invalidated'); } });
  await h.flush();
  const btn = h.button();
  await btn.click();
  await h.flush();
  assert.match(btn.textContent, /Error/);
});

test('SPA navigation (yt-navigate-finish) re-runs refresh and re-injects after the host changes', async () => {
  let host = makeElement('div');
  const h = harness({ target: () => host });
  await h.flush();
  assert.equal(host.children.length, 1);
  // Simulate YouTube replacing the actions container: a new host element, old one detached.
  host = makeElement('div');
  h.fireWindowEvent('yt-navigate-finish');
  await h.flush();
  assert.equal(host.children.length, 1, 'button was injected into the new host');
});

test('the periodic interval re-injects if YouTube recreates the actions bar without firing events', async () => {
  let host = makeElement('div');
  const h = harness({ target: () => host });
  await h.flush();
  assert.equal(host.children.length, 1);
  host = makeElement('div');   // YouTube rebuilt the DOM; the old button is gone with the old host.
  h.fireInterval();
  await h.flush();
  assert.equal(host.children.length, 1, 'the interval re-injected the button');
});

test('refresh is debounced: many rapid mutations fire refresh only once per tick', async () => {
  const h = harness();
  await h.flush();
  const before = h.host.children.length;
  for (let i = 0; i < 10; i++) h.fireObserver();
  await h.flush();
  assert.equal(h.host.children.length, before, 'coalesced mutations did not duplicate the button');
});
