'use strict';
// Runs in the isolated content-script world. No local-helper tokens or API keys.
(() => {
  const ID = 'brave-music-download-wav';
  const valid = () => {
    try {
      const u = new URL(location.href);
      return (u.pathname === '/watch' && /^[a-zA-Z0-9_-]{11}$/.test(u.searchParams.get('v') || '')) ||
        /^\/shorts\/[a-zA-Z0-9_-]{11}\/?$/.test(u.pathname);
    } catch { return false; }
  };
  let previousUrl = '';
  let timer;
  function host() {
    if (location.hostname === 'music.youtube.com')
      return document.querySelector('ytmusic-player-bar .right-controls') || document.querySelector('ytmusic-player-bar');
    return document.querySelector('ytd-watch-metadata #top-level-buttons-computed') ||
      document.querySelector('ytd-watch-metadata #actions') ||
      document.querySelector('ytd-watch-flexy #top-level-buttons-computed');
  }
  function refresh() {
    const old = document.getElementById(ID);
    if (!valid()) { if (old) old.remove(); return; }
    const target = host();
    if (!target) { if (old) old.remove(); return; }
    if (old && old.parentElement !== target) old.remove();
    if (document.getElementById(ID)) return;
    const button = document.createElement('button');
    button.id = ID;
    button.type = 'button';
    button.textContent = '↓ Save';
    button.title = 'Add this track to your Brave Music download queue';
    button.setAttribute('aria-label','Queue track for download');
    button.addEventListener('click', async (event) => {
      event.preventDefault(); event.stopPropagation();
      if (!valid()) return;
      button.disabled = true;
      button.textContent = 'Queuing…';
      try {
        const response = await chrome.runtime.sendMessage({type:'BMD_ENQUEUE_CURRENT',title:document.title});
        if (response?.error) throw new Error(response.error);
        button.title = response?.message || 'Queued';
        button.textContent = response?.added ? '✓ Queued' : '✓ Exists';
      } catch (err) {
        button.title = err.message || 'Could not queue track';
        button.textContent = '! Error';
      }
      setTimeout(() => {
        if (button.isConnected) { button.disabled = false; button.textContent = '↓ Save'; }
      }, 2400);
    });
    target.appendChild(button);
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(refresh,280); }
  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement,{childList:true,subtree:true});
  window.addEventListener('yt-navigate-finish',schedule);
  window.addEventListener('yt-page-data-updated',schedule);
  window.addEventListener('popstate',schedule);
  // MutationObserver covers YouTube SPA navigation; a URL check also covers
  // player bar re-creation without assuming YouTube's internal components.
  setInterval(() => {
    if (previousUrl !== location.href || (valid() && !document.getElementById(ID))) {
      previousUrl = location.href; schedule();
    }
  },2500);
  schedule();
})();
