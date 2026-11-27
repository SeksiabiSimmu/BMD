'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createPoller } = require('../../app/extension/poller.js');

function fakes() {
  const listeners = [];
  const doc = { hidden: false, addEventListener: (type, fn) => type === 'visibilitychange' && listeners.push(fn) };
  const timers = { handlers: new Map(), next: 1, created: 0 };
  timers.setInterval = (fn) => { const id = timers.next++; timers.handlers.set(id, fn); timers.created++; return id; };
  timers.clearInterval = (id) => timers.handlers.delete(id);
  timers.fire = async () => { for (const fn of [...timers.handlers.values()]) await fn(); };
  const setHidden = (hidden) => { doc.hidden = hidden; listeners.forEach((fn) => fn()); };
  return { doc, timers, setHidden };
}

test('polls while visible, stops while hidden, refreshes at once when shown again', async () => {
  const { doc, timers, setHidden } = fakes();
  let calls = 0;
  const poller = createPoller(async () => { calls++; }, 2200, doc, timers);
  assert.equal(poller.running, true);
  await timers.fire(); await timers.fire();
  assert.equal(calls, 2);
  setHidden(true);
  assert.equal(poller.running, false);
  for (let i = 0; i < 30; i++) await timers.fire();          // 30 intervals pass while hidden
  assert.equal(calls, 2, 'no polls while hidden');
  setHidden(false);
  await new Promise((r) => setImmediate(r));
  assert.equal(calls, 3, 'one immediate refresh when it becomes visible');
  assert.equal(poller.running, true);
  await timers.fire();
  assert.equal(calls, 4);
});

test('repeated visibility events never create duplicate timers', async () => {
  const { doc, timers, setHidden } = fakes();
  const poller = createPoller(async () => {}, 2200, doc, timers);
  setHidden(false); setHidden(false); setHidden(true); setHidden(true); setHidden(false);
  assert.equal(timers.handlers.size, 1);
  poller.stop(); poller.stop();
  assert.equal(timers.handlers.size, 0);
});

test('a page that starts hidden does not poll until shown', async () => {
  const { doc, timers, setHidden } = fakes();
  doc.hidden = true;
  const poller = createPoller(async () => {}, 2200, doc, timers);
  assert.equal(poller.running, false);
  setHidden(false);
  assert.equal(poller.running, true);
});

test('slow refreshes never overlap and errors do not stop polling', async () => {
  const { doc, timers } = fakes();
  let active = 0, peak = 0, calls = 0, release;
  createPoller(() => new Promise((resolve, reject) => {
    calls++; active++; peak = Math.max(peak, active);
    release = () => { active--; calls === 1 ? reject(new Error('boom')) : resolve(); };
  }), 2200, doc, timers);
  const first = timers.fire();
  await timers.fire(); await timers.fire();                   // intervals fire while the first is still running
  assert.equal(calls, 1);
  release(); await first;
  const second = timers.fire();
  assert.equal(calls, 2, 'polling continued after the failure');
  release(); await second;
  assert.equal(peak, 1);
});

test('the dashboard actually uses the poller and loads it first', () => {
  const dir = path.join(__dirname, '..', '..', 'app', 'extension');
  const js = fs.readFileSync(path.join(dir, 'dashboard.js'), 'utf8');
  const html = fs.readFileSync(path.join(dir, 'dashboard.html'), 'utf8');
  assert.ok(js.includes('createPoller(refresh'));
  assert.ok(!/setInterval\(refresh/.test(js));
  assert.ok(html.indexOf('poller.js') > -1 && html.indexOf('poller.js') < html.indexOf('dashboard.js'));
});
