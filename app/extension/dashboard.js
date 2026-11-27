'use strict';
const E = id => document.getElementById(id);
const controls = ['metadata','normalize','musicbrainz','artwork','organize','trim_start','notifications'];
const pageNames = {overview:'Overview',downloads:'Download queue',import:'Playlist import',settings:'Audio settings',diagnostics:'Diagnostics'};
const PAGE_SIZE = 50;
let jobs = [], recent = [], counts = {}, selection = [], filter = 'all', queueInFlight = false;
let nextCursor = null, extraPages = [];
let connected = false;

async function rpc(method, params = {}) {
  const response = await chrome.runtime.sendMessage({type:'BMD_RPC',method,params});
  if (!response?.ok) {
    const error = new Error(response?.error || 'Native helper not connected');
    error.ambiguous = response?.ambiguous === true;
    throw error;
  }
  return response.data;
}
function setText(id, value) { E(id).textContent = String(value); }
function page(name) {
  if (!pageNames[name]) return;
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active',button.dataset.page === name));
  for (const item of Object.keys(pageNames)) E('page-' + item).hidden = item !== name;
  setText('heading',pageNames[name]);
  if (name === 'diagnostics') checkDiagnostics(false);
  if (name === 'downloads') renderJobs();
}
function displayStatus(job) {
  const card = document.createElement('div'); card.className = 'job';
  const header = document.createElement('div'); header.className = 'jobhead';
  const title = document.createElement('div'); title.className = 'jobtitle'; title.textContent = job.title || job.filename || job.video_id || 'Track';
  const state = document.createElement('span');state.className = 'state ' + job.state;state.textContent = job.state || '?';
  header.append(title,state);
  const info = document.createElement('div');info.className = 'jobmeta';
  info.textContent = (job.message || '') + (job.state === 'done' && job.filename ? ` · ${job.artist || ''} / ${job.album || ''} / ${job.filename}` : '');
  const progress = document.createElement('progress');progress.max=100;progress.value = Math.min(100,Math.max(0,Number(job.percent) || 0));
  const actions = document.createElement('div');actions.className = 'actions';
  if (['queued','running'].includes(job.state)) {
    const cancel = document.createElement('button');cancel.textContent='Cancel';
    cancel.addEventListener('click',() => jobAction('cancel',job.id));actions.append(cancel);
  } else if (['canceled','error'].includes(job.state)) {
    const retry = document.createElement('button');retry.textContent='↻ Retry';
    retry.addEventListener('click',() => jobAction('retry',job.id));actions.append(retry);
  }
  card.append(header,info,progress,actions);
  return card;
}
function hint(container, text) {
  container.replaceChildren();
  const p=document.createElement('p');p.textContent=text;container.append(p);
}
function fill(container, listed) {
  if (!listed.length) return hint(container,'No matching jobs.');
  container.replaceChildren();
  for (const job of listed) container.append(displayStatus(job));
}
function renderJobs() {
  const active = (counts.queued || 0) + (counts.running || 0);
  setText('statActive',active);setText('statDone',counts.done || 0);
  setText('statError',counts.error || 0);setText('statCanceled',counts.canceled || 0);
  fill(E('recent'),recent.slice(0,6));
  fill(E('downloadList'),jobs);
  E('loadMore').hidden = !nextCursor;
}
function serverFilter() { return filter === 'all' ? undefined : filter; }
function merge(first, extras) {
  const seen = new Set(first.map(job => job.id));
  return first.concat(extras.filter(job => !seen.has(job.id)));
}
async function refresh() {
  if (queueInFlight) return;
  queueInFlight = true;
  try {
    const data = await rpc('jobs',{limit:PAGE_SIZE,state:serverFilter()});
    connected = true;
    counts = data.counts || {};
    // Active jobs always sort first, so refreshing page one keeps progress live;
    // pages the person already loaded are kept underneath it.
    jobs = merge(data.jobs || [], extraPages);
    if (!extraPages.length) nextCursor = data.next_cursor || null;
    recent = filter === 'all' ? jobs : (await rpc('jobs',{limit:6})).jobs || [];
    E('signal').classList.add('online');
    setText('connection','Connected');
    renderJobs();
  } catch (error) {
    connected = false;
    E('signal').classList.remove('online');
    setText('connection','Disconnected');
    setText('queueStatus','Helper unavailable: ' + error.message);
    // The Overview is the first page a new user sees: say why "Recent activity" is empty.
    if (!recent.length) hint(E('recent'),'Nothing to show while the helper is disconnected.');
  } finally {queueInFlight=false;}
}
async function loadMore() {
  if (!nextCursor) return;
  E('loadMore').disabled = true;
  try {
    const data = await rpc('jobs',{limit:PAGE_SIZE,cursor:nextCursor,state:serverFilter()});
    extraPages = merge(extraPages, data.jobs || []);
    nextCursor = data.next_cursor || null;
    jobs = merge(jobs, extraPages);
    renderJobs();
  } catch (error) {setText('queueStatus',error.message);} finally {E('loadMore').disabled = false;}
}
function resetPaging() { extraPages = []; nextCursor = null; jobs = []; }
function describe(error) {
  return error.ambiguous ? error.message + ' The queue below shows what actually happened.' : error.message;
}
function settings() {return {format:E('format').value, ...Object.fromEntries(controls.filter(x => x !== 'notifications').map(x=>[x,E(x).checked]))};}
async function remember(ids) {
  if (ids?.length) chrome.runtime.sendMessage({type:'BMD_TRACK_ENQUEUED',ids}).catch(()=>{});
}
async function jobAction(method,id) {
  try {await rpc(method,{id});await refresh();setText('queueStatus',method === 'cancel' ? 'Canceled job.' : 'Job returned to queue.');}
  catch (error) {setText('queueStatus',describe(error));if (error.ambiguous) await refresh();}
}
async function quickDownload() {
  const url=E('singleUrl').value.trim();
  E('singleAdd').disabled=true;
  setText('singleStatus','Adding track…');
  try {
    const result=await rpc('enqueue',{...settings(),url,title:'YouTube track'});
    await remember(result.added);
    setText('singleStatus',`${result.added.length} queued · ${result.duplicate.length} already queued · ${result.existing.length} already downloaded`);
    await refresh();
  } catch (error) {setText('singleStatus',describe(error));if (error.ambiguous) await refresh();} finally {E('singleAdd').disabled=false;}
}
async function preview() {
  E('preview').disabled = true;
  E('playlistPreview').hidden = true;
  setText('importStatus','Reading playlist…');
  try {
    const data = await rpc('preview',{url:E('playlistUrl').value.trim()});
    selection = data.items.map(track=>({...track,selected:true}));
    setText('playlistName',data.title);
    const container = E('previewItems');container.replaceChildren();
    selection.forEach((track,i) => {
      const label = document.createElement('label');label.className='previewitem';
      const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=true;
      checkbox.addEventListener('change',()=>{track.selected=checkbox.checked;updateSelection();});
      const text=document.createElement('span');
      const strong=document.createElement('strong');strong.textContent=(i+1)+'. '+track.title;
      const small=document.createElement('small');small.textContent=track.artist || track.id;
      text.append(strong,small);label.append(checkbox,text);container.append(label);
    });
    E('playlistPreview').hidden=false;
    updateSelection();
    setText('importStatus',data.count+' tracks loaded' + (data.truncated ? ' · first 200 only' : ''));
  } catch (error) {setText('importStatus',error.message);} finally {E('preview').disabled=false;}
}
function updateSelection() {
  const selected = selection.filter(x=>x.selected).length;
  setText('selectedCount',selected+' / '+selection.length+' selected');
  E('selectAll').checked = selected === selection.length;
  E('selectAll').indeterminate = selected > 0 && selected < selection.length;
  E('importSelected').disabled = selected === 0;
}
async function importSelected() {
  const items=selection.filter(x=>x.selected).map(({id,title})=>({id,title}));
  if (!items.length) return;
  E('importSelected').disabled=true;
  setText('importStatus','Adding selected tracks…');
  try {
    const res=await rpc('enqueue',{items,...settings()});
    await remember(res.added);
    setText('importStatus',`${res.added.length} queued · ${res.duplicate.length} already queued · ${res.existing.length} already saved`);
    await refresh();page('downloads');
  } catch(error){setText('importStatus',describe(error));if (error.ambiguous) await refresh();}finally{E('importSelected').disabled=false;}
}
function formatBytes(value) {
  if (!Number.isFinite(value)) return 'unknown';
  const units = ['B','KB','MB','GB','TB'];
  let size = value, unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
  return size.toFixed(unit ? 1 : 0) + ' ' + units[unit];
}
function explainConnection(message) {
  const id = chrome.runtime.id;
  let help = message;
  if (/forbidden/i.test(message)) {
    // lliokng… is not a typo: it is this extension's pinned ID (derived from the manifest
    // "key", see extension_id.txt), the only origin the installed helper accepts.
    help = `Brave blocked this extension (its ID is ${id}). The helper only accepts lliokngbogdlnnjffdfgdjabpmjpobab; load the folder setup showed, not a copy without the manifest key.`;
  }
  else if (/not found/i.test(message)) help = 'Brave cannot find the helper registration. Run setup again, then reload the extension.';
  else if (/timed out/i.test(message)) help = 'The helper started but never answered. Run setup again (it now tests this) or run the hostcheck command from the README.';
  else if (/exited/i.test(message)) help = 'The helper stopped right after starting. See %LOCALAPPDATA%\\BraveMusicDownloader\\logs\\helper.log.';
  return help === message ? message : `${help} (${message})`;
}
async function checkDiagnostics(forceReconnect = false) {
  setText('diagnosticConnection','Checking native host…');
  try {
    let health;
    if (forceReconnect) { try { health = await rpc('reconnect'); } catch { health = await rpc('health'); } }
    else health = await rpc('health');
    setText('diagnosticConnection','Connected · v'+health.version+' · '+health.transport);
    setText('outputPath',health.output);
    const report=await rpc('diagnostics');
    const root=E('tools');root.replaceChildren();
    Object.entries(report.tools).forEach(([name,item])=>{
      const card=document.createElement('div');card.className='tool';
      const title=document.createElement('strong');title.textContent=(item.installed?'✓ ':'✕ ')+name;
      const version=document.createElement('small');version.textContent=item.version || 'Not found';
      const location=document.createElement('small');location.textContent=item.path || '';
      const owner=document.createElement('small');
      owner.textContent=item.source === 'missing' ? 'missing'
        : (item.source === 'app' ? 'private copy' : 'system copy') + (item.owned ? ' · installed by this app' : ' · not managed by this app');
      card.append(title,version,location,owner);root.append(card);
    });
    const store=document.createElement('div');store.className='tool';
    const heading=document.createElement('strong');heading.textContent='Library & cache';
    const line1=document.createElement('small');
    line1.textContent='Queue database: '+(report.database ? 'schema '+report.database.schema+' · '+report.database.integrity : 'unavailable');
    const line2=document.createElement('small');
    line2.textContent='Cache: '+formatBytes(report.cache_bytes)+' · Free space: '+formatBytes(report.free_bytes);
    const line3=document.createElement('small');
    line3.textContent='Helper log: '+(report.log_file || 'unavailable');
    store.append(heading,line1,line2,line3);root.append(store);
    setText('diagStatus',report.requirements_ok ? 'All required executables detected. Full YouTube support also needs yt-dlp\'s challenge solver and Deno.' : 'Problems: '+report.issues.join('; ')+'. Run the setup again to repair.');
  } catch (error) {
    setText('diagnosticConnection','Disconnected');
    setText('diagStatus',explainConnection(error.message));
  }
}
// What the Overview says about the output follows the chosen format, as the files do. The WAV cover
// is a "<track>.cover.jpg" file next to the track (helper.py), not a file named cover.jpg.
const PROFILES = {
  wav: ['24', '24-bit PCM WAV', 'Uncompressed and lossless: the largest files. The cover is saved next to the track as a .cover.jpg file.'],
  flac: ['24', '24-bit FLAC', 'Lossless at about half the size of WAV, with tags and cover embedded.'],
  mp3: ['320', 'MP3 · 320 kbps', 'Lossy, but plays everywhere; tags and cover are embedded.'],
};
function showProfile(format) {
  const [bits, name, note] = PROFILES[format] || PROFILES.wav;
  setText('profileBits',bits); setText('profileFormat',name); setText('profileNote',note);
}
async function init() {
  const saved=await chrome.storage.local.get([...controls,'format','advanced_open']);
  for (const name of controls) {
    if (typeof saved[name] === 'boolean') E(name).checked=saved[name];
    E(name).addEventListener('change',async () => {
      await chrome.storage.local.set({[name]:E(name).checked});
      setText('settingsStatus','Preferences saved.');
    });
  }
  // Output format: load the saved choice, persist on change.
  if (typeof saved.format === 'string' && ['wav','flac','mp3'].includes(saved.format)) E('format').value=saved.format;
  showProfile(E('format').value);
  E('format').addEventListener('change',async()=>{
    await chrome.storage.local.set({format:E('format').value});
    showProfile(E('format').value);
    setText('settingsStatus','Output format saved.');
  });
  // Advanced options collapse: hidden by default, expanded state remembered across sessions.
  const adv=E('advancedToggle'), advBox=E('advancedSettings');
  if (saved.advanced_open === true) { advBox.hidden=false; adv.setAttribute('aria-expanded','true'); }
  adv.addEventListener('click',async()=>{
    const open=advBox.hidden;
    advBox.hidden=!open;
    adv.setAttribute('aria-expanded',String(open));
    await chrome.storage.local.set({advanced_open:open});
  });
  document.querySelectorAll('.nav').forEach(button=>button.addEventListener('click',()=>page(button.dataset.page)));
  document.querySelectorAll('.filter').forEach(button=>button.addEventListener('click',()=>{
    filter=button.dataset.filter;
    document.querySelectorAll('.filter').forEach(x=>x.classList.toggle('active',x===button));
    resetPaging();
    renderJobs();
    refresh();
  }));
  E('refresh').addEventListener('click',refresh);
  E('goSettings').addEventListener('click',()=>page('settings'));
  E('goQueue').addEventListener('click',()=>page('downloads'));
  E('singleAdd').addEventListener('click',quickDownload);
  E('preview').addEventListener('click',preview);
  E('selectAll').addEventListener('change',event=>{
    selection.forEach(x=>x.selected=event.target.checked);
    E('previewItems').querySelectorAll('input[type=checkbox]').forEach(input=>input.checked=event.target.checked);
    updateSelection();
  });
  E('importSelected').addEventListener('click',importSelected);
  E('clearFinished').addEventListener('click',async()=>{
    try {const x=await rpc('clear');setText('queueStatus',x.removed+' jobs removed from history.');await refresh();}
    catch(error){setText('queueStatus',error.message);}
  });
  E('checkDependencies').addEventListener('click',() => checkDiagnostics(true));
  E('loadMore').addEventListener('click',loadMore);
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    // Prefill only from a real YouTube tab (host_permissions expose its url). A substring
    // match would prefill from any URL that merely contains "youtube.com/" anywhere.
    const host = tab?.url ? new URL(tab.url).hostname : '';
    const isYouTube = host === 'youtube.com' || host.endsWith('.youtube.com') || host === 'youtu.be';
    if (isYouTube) E('singleUrl').value=tab.url;
    if (host === 'youtube.com' || host.endsWith('.youtube.com')) {
      if (tab.url.includes('list=')) E('playlistUrl').value=tab.url;
    }
  } catch { /* No readable YouTube tab (the URL may be blank or not a YouTube host). */ }
  await refresh();
  createPoller(refresh,2200,document,{setInterval:(f,ms)=>setInterval(f,ms),clearInterval:(t)=>clearInterval(t)});
}
// The version shown is the installed package's own, read from the manifest. A literal in the
// page went stale in every release before this one (it still said v2.0 at 2.3).
setText('appVersion','v'+chrome.runtime.getManifest().version);
init().catch(error=>setText('connection',error.message));
