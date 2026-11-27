'use strict';
const el = (id) => document.getElementById(id);
let helperConnected = false;
let currentVideo = null;
let playlistPreview = [];
let busyPreview = false;
let busyAdd = false;
let requestBusy = false;
let activeTabName = 'track';
let queueTimer = null;

function isVideo(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'https:' || u.port || !['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be'].includes(u.hostname)) return false;
    if (u.hostname === 'youtu.be') return /^\/[\w-]{11}\/?$/.test(u.pathname);
    if (u.pathname === '/watch') return /^[\w-]{11}$/.test(u.searchParams.get('v') || '');
    return /^\/shorts\/[\w-]{11}\/?$/.test(u.pathname);
  } catch { return false; }
}
function isPlaylist(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' && !u.port &&
      ['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'].includes(u.hostname) &&
      ['/watch','/playlist'].includes(u.pathname) &&
      /^[\w-]{8,128}$/.test(u.searchParams.get('list') || '');
  } catch { return false; }
}
function setText(id, value) { el(id).textContent = value; }
function canRequest() { return helperConnected; }
function updateButtons() {
  el('enqueueTrack').disabled = !canRequest() || !currentVideo || busyAdd;
  el('preview').disabled = !canRequest() || busyPreview;
  el('enqueueSelected').disabled = !canRequest() || busyAdd || !playlistPreview.some((x) => x.selected);
}
function openTab(name) {
  activeTabName = name;
  document.querySelectorAll('.tab').forEach((button) => {
    const active = button.dataset.tab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  for (const key of ['track','playlist','queue']) el('pane-' + key).hidden = key !== name;
  if (name === 'queue') refreshQueue();
}
async function api(path, method = 'GET', payload = null) {
  const commands = {'/health':'health', '/jobs':'jobs', '/playlist/preview':'preview',
    '/enqueue':'enqueue','/cancel':'cancel','/retry':'retry','/clear':'clear', '/diagnostics':'diagnostics'};
  const operation = commands[path];
  if (!operation) throw new Error('Unsupported operation: ' + path);
  const answer = await chrome.runtime.sendMessage({type:'BMD_RPC',method:operation,params:payload || {}});
  if (!answer?.ok) throw new Error(answer?.error || 'Native host unavailable. Run the Brave Music Downloader setup again to repair it.');
  return answer.data;
}
function settings() { return {format:el('format').value, metadata:el('metadata').checked,normalize:el('normalize').checked,
  musicbrainz:el('musicbrainz').checked,artwork:el('artwork').checked,organize:el('organize').checked,trim_start:el('trim_start').checked}; }
async function enqueue(items, isSingle = false) {
  busyAdd = true;
  updateButtons();
  const status = isSingle ? 'trackStatus' : 'playlistStatus';
  setText(status, 'Adding to queue…');
  try {
    const payload = { ...settings(), ...(isSingle ? {url:currentVideo, title:el('track').textContent} : {items}) };
    const result = await api('/enqueue', 'POST', payload);
    const added=result.added.length;
    if (added) {
      chrome.runtime.sendMessage({type:'BMD_TRACK_ENQUEUED', ids:result.added}).catch(()=>{});
    }
    setText(status, `${added} queued · ${result.duplicate.length} already queued · ${result.existing.length} already downloaded`);
    await refreshQueue();
    if (added) openTab('queue');
  } catch (error) { setText(status, error.message); }
  finally { busyAdd = false; updateButtons(); }
}
el('enqueueTrack').addEventListener('click', () => enqueue([],true));

function updateSelected() {
  const selected = playlistPreview.filter((x) => x.selected).length;
  setText('selectedCount', `${selected}/${playlistPreview.length} selected`);
  el('selectAll').checked = selected === playlistPreview.length;
  el('selectAll').indeterminate = selected > 0 && selected < playlistPreview.length;
  updateButtons();
}
function renderPreview(data) {
  playlistPreview = data.items.map((x) => ({...x,selected:true}));
  setText('playlistTitle',data.title);
  el('previewArea').hidden = false;
  const container = el('playlistItems');
  container.replaceChildren();
  playlistPreview.forEach((entry,index) => {
    const label=document.createElement('label'); label.className='playlist-row';
    const checkbox=document.createElement('input'); checkbox.type='checkbox'; checkbox.checked=true;
    checkbox.addEventListener('change',()=>{entry.selected=checkbox.checked;updateSelected();});
    const info=document.createElement('span'); info.className='playlist-info';
    const title=document.createElement('strong'); title.textContent=`${index+1}. ${entry.title}`;
    const by=document.createElement('small'); by.textContent=entry.artist || entry.id;
    info.append(title,by); label.append(checkbox,info); container.append(label);
  });
  updateSelected();
}
el('selectAll').addEventListener('change', (event) => {
  const checked=event.target.checked;
  playlistPreview.forEach((x)=>{x.selected=checked;});
  el('playlistItems').querySelectorAll('input[type=checkbox]').forEach((cb)=>{cb.checked=checked;});
  updateSelected();
});
el('preview').addEventListener('click', async () => {
  const url=el('playlistUrl').value.trim();
  if (!isPlaylist(url)) { setText('playlistStatus','Paste a supported YouTube playlist URL.'); return; }
  busyPreview=true; updateButtons();
  setText('playlistStatus','Reading playlist track list…');
  el('previewArea').hidden=true;
  try {
    const data=await api('/playlist/preview','POST',{url});
    renderPreview(data);
    setText('playlistStatus',`${data.count} tracks listed${data.truncated?' (first 200 only)':''}. Choose tracks below.`);
  } catch (error) { setText('playlistStatus',error.message); }
  finally {busyPreview=false;updateButtons();}
});
el('enqueueSelected').addEventListener('click', () => {
  const selected=playlistPreview.filter((x)=>x.selected).map(({id,title})=>({id,title}));
  if (selected.length) enqueue(selected);
});

async function act(path,id) {
  if (requestBusy) return;
  requestBusy=true;
  try {
    await api(path,'POST',{id});
    await refreshQueue();
  } catch (error) {setText('queueStatus',error.message);}
  finally {requestBusy=false;}
}
function renderQueue(payload) {
  const counts=payload.counts || {};
  const active=(counts.queued||0)+(counts.running||0);
  setText('queueCount',String(active));
  setText('counts',`${active} active · ${counts.done||0} saved · ${counts.error||0} failed · ${counts.canceled||0} canceled`);
  const container=el('jobList');
  container.replaceChildren();
  if (!payload.jobs.length) {
    const empty=document.createElement('div'); empty.className='hint';
    empty.textContent='Your queue is empty. Add a track or import a playlist.';
    container.append(empty); return;
  }
  payload.jobs.forEach((job) => {
    const box=document.createElement('article');box.className='job';
    const top=document.createElement('div');top.className='job-top';
    const title=document.createElement('div');title.className='job-title';title.textContent=job.title || job.filename || job.video_id;
    const state=document.createElement('span');state.className='job-state '+job.state;
    state.textContent=job.state.toUpperCase();top.append(title,state);
    const desc=document.createElement('div');desc.className='job-desc';
    desc.textContent=(job.message||'')+(job.state==='done' && job.filename?` · ${job.artist||''} / ${job.album||'Singles'} / ${job.filename}`:'')+
      (job.state==='done' && job.artwork_source && job.artwork_source!=='none'?` · Art: ${job.artwork_source}`:'');
    const progress=document.createElement('progress');progress.className='job-progress';progress.max=100;
    progress.value=typeof job.percent==='number' ? Math.max(0,Math.min(100,job.percent)) : 0;
    const actions=document.createElement('div');actions.className='job-actions';
    if (['queued','running'].includes(job.state)) {
      const cancel=document.createElement('button');cancel.textContent='Cancel';
      cancel.addEventListener('click',()=>act('/cancel',job.id));actions.append(cancel);
    } else if (['error','canceled'].includes(job.state)) {
      const retry=document.createElement('button');retry.textContent='↻ Retry';
      retry.addEventListener('click',()=>act('/retry',job.id));actions.append(retry);
    }
    box.append(top,desc,progress,actions);container.append(box);
  });
}
async function refreshQueue() {
  if (!helperConnected) return;
  try {
    const payload=await api('/jobs');
    el('connected').classList.add('online');
    el('connected').title='Helper connected';
    renderQueue(payload);
  } catch (error) {
    el('connected').classList.remove('online');
    setText('queueStatus','Helper unavailable: '+error.message);
  }
}
el('clear').addEventListener('click',async()=>{
  try {const result=await api('/clear','POST',{});setText('queueStatus',`${result.removed} finished jobs cleared from history.`);await refreshQueue();}
  catch(error){setText('queueStatus',error.message);}
});
el('openDashboard').addEventListener('click', () => chrome.runtime.openOptionsPage());
// The helper reports its real VERSION (currently 2.5.0) over native messaging. The manifest pins the
// allowed extension id, so the host is trusted once it answers; the popup only rejects a host that
// is genuinely too old for the current RPC contract. The floor is 2.3.0: the first helper that
// accepts the `format` enqueue parameter (WAV/FLAC/MP3). An older helper would fail those requests.
function hostAcceptable(h) {
  if (!h || typeof h.version !== 'string' || !h.transport) return false;
  const [major, minor] = h.version.split('.').map((n) => Number(n));
  if (!Number.isInteger(major) || !Number.isInteger(minor)) return false;
  return major > 2 || (major === 2 && minor >= 3);
}
el('checkHost').addEventListener('click', async () => {
  setText('hostStatus', 'Checking local host…');
  try {
    const h = await api('/health');
    helperConnected = hostAcceptable(h);
    if (!helperConnected) throw new Error('The installed host is incompatible: ' + (h && h.version ? h.version : 'no version'));
    setText('hostStatus', 'Connected via Native Messaging · v' + h.version);
    el('connected').classList.add('online');
    updateButtons();
    await refreshQueue();
  } catch (error) {
    helperConnected = false;
    el('connected').classList.remove('online');
    setText('hostStatus', error.message);
    updateButtons();
  }
});
for (const name of ['metadata','normalize','musicbrainz','artwork','organize','trim_start','notifications']) {
  el(name).addEventListener('change',()=>chrome.storage.local.set({[name]:el(name).checked}));
}
el('format').addEventListener('change',()=>chrome.storage.local.set({format:el('format').value}));
// Advanced options collapse: hidden by default, expanded state is remembered across popups.
const advanced = el('advancedToggle'), advancedBox = el('advancedSettings');
advanced.addEventListener('click', () => {
  const open = advancedBox.hidden;
  advancedBox.hidden = !open;
  advanced.setAttribute('aria-expanded', String(open));
  chrome.storage.local.set({advanced_open: open});
});
document.querySelectorAll('.tab').forEach((button)=>button.addEventListener('click',()=>openTab(button.dataset.tab)));

(async function init() {
  // The version shown is the installed package's own, read from the manifest. A literal in the
  // page went stale in every release before this one (it still said v2.0 at 2.3).
  setText('appVersion', 'v' + chrome.runtime.getManifest().version);
  const saved = await chrome.storage.local.get(['metadata','normalize','musicbrainz','artwork','organize','trim_start','notifications','format','advanced_open']);
  for (const name of ['metadata','normalize','musicbrainz','artwork','organize','trim_start','notifications'])
    if (typeof saved[name] === 'boolean') el(name).checked = saved[name];
  if (typeof saved.format === 'string' && ['wav','flac','mp3'].includes(saved.format)) el('format').value = saved.format;
  if (saved.advanced_open === true) {
    advancedBox.hidden = false;
    advanced.setAttribute('aria-expanded', 'true');
  }
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  if (tab && isVideo(tab.url || '')) {
    currentVideo = tab.url;
    setText('track', tab.title || 'YouTube track');
  } else {
    setText('track', 'Open a YouTube / YouTube Music video.');
    setText('trackStatus', 'Individual watch pages are supported.');
  }
  if (tab && isPlaylist(tab.url || '')) {
    el('playlistUrl').value = tab.url;
    if (!currentVideo) openTab('playlist');
  }
  updateButtons();
  el('checkHost').click();
  queueTimer = setInterval(async () => {
    if (!helperConnected) return;
    await refreshQueue();
  }, 2000);
})().catch(error => setText('trackStatus', 'Startup failed: ' + error.message));
