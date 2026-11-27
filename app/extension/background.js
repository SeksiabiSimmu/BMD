'use strict';
// v2.3: all computer access goes through browser-enforced Native Messaging.
// No localhost API or shared bearer token. Content scripts only send safe intents.
// The native port is persistent. After a disconnect, read-only requests are retried once;
// state-changing requests are NEVER replayed automatically (their outcome may be unknown).
const NATIVE_HOST = 'com.bravemusic.downloader';
const ALARM = 'bmd-v2-queue-watch';
const MENU_PAGE = 'bmd-current-track';
const MENU_LINK = 'bmd-linked-track';
const WATCH_HOSTS = new Set(['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be']);
const PAGE_HOSTS = new Set(['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com']);
const DEFAULTS = {metadata:true,normalize:true,musicbrainz:true,artwork:true,organize:true,trim_start:false,notifications:true};
const READ_ONLY = new Set(['health','diagnostics','jobs','status','preview']);
const MUTATING = new Set(['enqueue','cancel','retry','clear']);
const BACKOFF_MS = [0, 1000, 2000, 5000, 15000, 30000];
// Pause before the one read-only retry. The new helper waits for the old one's lock by itself, so this only
// has to cover the browser noticing the disconnect; jitter keeps several windows from reconnecting in lockstep.
const RETRY_PAUSE_MS = 100;
const RETRY_JITTER_MS = 100;
const REPAIR_HINT = 'Run the Brave Music Downloader setup again to repair it.';
let port = null;
let seq = 0;
let failures = 0;        // consecutive connections that died before answering anything
let blockedUntil = 0;    // while set, do not dial again (host missing or crashing at start)
const waiting = new Map();

class HostError extends Error {
  constructor(message, flags = {}) { super(message); Object.assign(this, flags); }
}
function canonicalVideo(raw) {
  try {
    const u = new URL(raw);
    if (u.protocol !== 'https:' || u.port || u.username || u.password || u.hash || !WATCH_HOSTS.has(u.hostname)) return null;
    let vid = null;
    if (u.hostname === 'youtu.be') vid = u.pathname.match(/^\/([A-Za-z0-9_-]{11})\/?$/)?.[1];
    else if (u.pathname === '/watch') vid = u.searchParams.get('v');
    else vid = u.pathname.match(/^\/shorts\/([A-Za-z0-9_-]{11})\/?$/)?.[1];
    return /^[A-Za-z0-9_-]{11}$/.test(vid || '') ? `https://www.youtube.com/watch?v=${vid}` : null;
  } catch { return null; }
}
function isOwnPage(raw) {
  try {
    const u = new URL(raw);
    const own = new URL(chrome.runtime.getURL('/'));
    // Compare scheme + host: URL.origin is the opaque string "null" for extension URLs in some engines.
    return u.protocol === own.protocol && u.host === own.host && ['/popup.html', '/dashboard.html'].includes(u.pathname);
  } catch { return false; }
}
function isYoutubePage(raw) {
  try { const u = new URL(raw); return u.protocol === 'https:' && !u.port && PAGE_HOSTS.has(u.hostname); }
  catch { return false; }
}
function failPending(error, notSent = false) {
  for (const item of waiting.values()) {
    clearTimeout(item.timer);
    // What was posted on a connection whose helper was never started cannot have reached it:
    // that request is known not to have happened, not of unknown outcome.
    item.reject(new HostError(error.message, {disconnected: true, sent: item.sent && !notSent}));
  }
  waiting.clear();
}
function connect() {
  if (port) return port;
  const wait = blockedUntil - Date.now();
  if (wait > 0) throw new HostError(`Native helper unavailable; retrying in ${Math.ceil(wait / 1000)}s. ${REPAIR_HINT}`, {disconnected: true, sent: false});
  const next = chrome.runtime.connectNative(NATIVE_HOST);
  next.answered = false;
  port = next;
  next.onMessage.addListener((packet) => {
    next.answered = true;
    failures = 0;
    blockedUntil = 0;
    if (!packet || !Number.isInteger(packet.id)) return;
    const item = waiting.get(packet.id);
    if (!item) return;
    waiting.delete(packet.id);
    clearTimeout(item.timer);
    if (packet.ok) item.resolve(packet.data);
    else item.reject(new Error(packet.error || 'Native helper request failed'));
  });
  next.onDisconnect.addListener(() => {
    const reason = chrome.runtime.lastError?.message || 'Native helper disconnected.';
    if (port === next) port = null;
    if (!next.answered) {
      failures += 1;
      blockedUntil = Date.now() + BACKOFF_MS[Math.min(failures, BACKOFF_MS.length - 1)];
    }
    // "not found" (nothing is installed) and "forbidden" (the registration does not name this
    // extension): the browser never started the helper, so nothing posted on this port arrived.
    const neverStarted = !next.answered && /not found|forbidden/i.test(reason);
    failPending(new Error(`${reason} ${REPAIR_HINT}`), neverStarted);
  });
  return next;
}
function send(method, params) {
  return new Promise((resolve, reject) => {
    const number = ++seq;
    const timeout = method === 'preview' ? 140000 : method === 'diagnostics' ? 45000 : 20000;
    const item = {resolve, reject, sent: false, timer: null};
    item.timer = setTimeout(() => {
      waiting.delete(number);
      reject(new HostError('Native helper timed out', {timedOut: true, sent: item.sent}));
    }, timeout);
    waiting.set(number, item);
    try {
      connect().postMessage({id: number, method, params});
      item.sent = true;
    } catch (error) {
      clearTimeout(item.timer);
      waiting.delete(number);
      reject(error instanceof HostError ? error : new HostError(error.message, {disconnected: true, sent: false}));
    }
  });
}
const pause = (ms) => new Promise((done) => setTimeout(done, ms));
async function native(method, params = {}) {
  const readOnly = READ_ONLY.has(method);
  if (!readOnly && !MUTATING.has(method)) throw new Error('Unknown native operation');
  for (let attempt = 0; ; attempt++) {
    try {
      return await send(method, params);
    } catch (error) {
      if (!(error instanceof HostError) || !error.disconnected) throw error;
      if (readOnly && attempt === 0) { await pause(RETRY_PAUSE_MS + Math.random() * RETRY_JITTER_MS); continue; }
      if (!readOnly && error.sent) {
        throw new HostError('The helper disconnected before confirming this action. Check the queue before trying again.', {ambiguous: true});
      }
      throw error;
    }
  }
}
function reconnectNative() {
  failures = 0;
  blockedUntil = 0;
  if (port) { try { port.disconnect(); } catch { /* already closed */ } port = null; }
  failPending(new Error('Native helper connection was reset.'));
  return native('health');
}
async function showNotice(title, message, id) {
  const {notifications} = await chrome.storage.local.get('notifications');
  if (notifications === false) return;
  try { await chrome.notifications.create(id || '', {
    type:'basic', iconUrl:'icons/icon128.png',
    title:String(title).slice(0,120), message:String(message).slice(0,240), priority:0
  }); } catch { /* Notification permission may be unavailable. */ }
}
async function enqueueTrack(raw, title) {
  const url = canonicalVideo(raw);
  if (!url) throw new Error('Open a supported YouTube video before downloading.');
  const values = await chrome.storage.local.get([...Object.keys(DEFAULTS), 'format']);
  const settings = Object.fromEntries(Object.entries(DEFAULTS).map(([k,def]) => [k,typeof values[k] === 'boolean' ? values[k] : def]));
  // The saved output format travels with every enqueue (WAV, FLAC or MP3); an
  // unrecognised stored value falls back to WAV instead of failing the request.
  const format = ['wav','flac','mp3'].includes(values.format) ? values.format : 'wav';
  const {notifications, ...options} = settings;
  const result = await native('enqueue', {url,title:String(title || 'YouTube track').slice(0,240),...options,format});
  const added = result.added?.length || 0;
  const duplicate = result.duplicate?.length || 0;
  const existing = result.existing?.length || 0;
  const message = added ? `Queued for ${format.toUpperCase()} download.` : existing ? 'Already saved in your library.' : duplicate ? 'Already in the queue.' : 'No new tracks added.';
  if (added) {
    const {bmdWatchedStates} = await chrome.storage.local.get('bmdWatchedStates');
    const seen = bmdWatchedStates || {};
    for (const job of result.added) seen[job] = 'queued';
    await chrome.storage.local.set({bmdWatchedStates:trimSeen(seen)});
  }
  showNotice('Brave Music Downloader', message);
  return {added,duplicate,existing,message};
}
function trimSeen(seen) { return Object.fromEntries(Object.entries(seen).slice(-500)); }
async function watchJobs() {
  let payload;
  try { payload = await native('jobs'); } catch { return; }
  const {bmdWatchedStates} = await chrome.storage.local.get('bmdWatchedStates');
  const seen = bmdWatchedStates || {};
  const fresh = bmdWatchedStates === undefined;
  for (const job of payload.jobs || []) {
    if (!job?.id) continue;
    if (!fresh && ['done','error'].includes(job.state) && seen[job.id] && seen[job.id] !== job.state)
      await showNotice(job.state === 'done' ? 'Download complete' : 'Download failed',
        `${(job.title || job.filename || 'Track').slice(0,100)} — ${job.message || job.state}`,
        `bmd-${job.state}-${job.id}`);
    seen[job.id] = job.state;
  }
  await chrome.storage.local.set({bmdWatchedStates:trimSeen(seen)});
  const active = (payload.counts?.queued || 0) + (payload.counts?.running || 0);
  await chrome.action.setBadgeText({text:active ? String(active > 99 ? '99+' : active) : ''});
  await chrome.action.setBadgeBackgroundColor({color:'#356bea'});
}
function installMenus() {
  chrome.contextMenus.removeAll(() => {
    const pages = ['https://youtube.com/*','https://www.youtube.com/*','https://music.youtube.com/*','https://m.youtube.com/*'];
    chrome.contextMenus.create({id:MENU_PAGE,title:'Queue current track',contexts:['page'],documentUrlPatterns:pages});
    chrome.contextMenus.create({id:MENU_LINK,title:'Queue linked YouTube track',contexts:['link'],documentUrlPatterns:pages});
  });
}
async function ensureAlarm() {
  if (!await chrome.alarms.get(ALARM)) await chrome.alarms.create(ALARM,{periodInMinutes:1});
}
chrome.runtime.onInstalled.addListener(() => { installMenus(); ensureAlarm(); });
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); });
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === ALARM) watchJobs(); });
chrome.contextMenus.onClicked.addListener((info,tab) => {
  if (![MENU_PAGE,MENU_LINK].includes(info.menuItemId) || !isYoutubePage(tab?.url || info.pageUrl || '')) return;
  const target = info.menuItemId === MENU_LINK ? info.linkUrl : tab?.url;
  enqueueTrack(target,tab?.title).catch(error => showNotice('Track not queued', error.message));
});
chrome.commands.onCommand.addListener(async (key) => {
  if (key !== 'queue-current-wav') return;
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  if (!isYoutubePage(tab?.url || '')) return showNotice('Brave Music Downloader','Open a YouTube track first.');
  enqueueTrack(tab.url,tab.title).catch(error => showNotice('Track not queued',error.message));
});
chrome.runtime.onMessage.addListener((message,sender,respond) => {
  if (message?.type === 'BMD_RPC') {
    // Native requests are accepted only from the extension's popup/dashboard.
    if (sender.id !== chrome.runtime.id || !isOwnPage(sender.url)) return;
    const call = message.method === 'reconnect' ? reconnectNative() : native(message.method, message.params || {});
    call.then(data => respond({ok:true,data}))
      .catch(error => respond({ok:false,error:error.message,ambiguous:error.ambiguous === true}));
    return true;
  }
  if (message?.type === 'BMD_TRACK_ENQUEUED') {
    if (sender.id !== chrome.runtime.id || !isOwnPage(sender.url)) return;
    const ids = message.ids;
    if (!Array.isArray(ids) || ids.length > 200 || !ids.every(x => typeof x === 'string' && /^[0-9a-f]{32}$/.test(x))) return;
    chrome.storage.local.get('bmdWatchedStates').then(({bmdWatchedStates}) => {
      const seen = bmdWatchedStates || {};
      for (const id of ids) seen[id] = 'queued';
      return chrome.storage.local.set({bmdWatchedStates:trimSeen(seen)});
    }).then(() => respond({ok:true})).catch(() => respond({ok:false}));
    return true;
  }
  if (message?.type !== 'BMD_ENQUEUE_CURRENT' || !sender.tab || !isYoutubePage(sender.url || '')) return;
  enqueueTrack(sender.url,typeof message.title === 'string' ? message.title : sender.tab.title)
    .then(respond).catch(error => respond({error:error.message}));
  return true;
});
ensureAlarm();
native('health').catch(() => { /* the next request or alarm reconnects */ });
