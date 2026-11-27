// Polls while the page is visible. A hidden tab stops polling (no needless messages to the helper) and
// refreshes immediately when it becomes visible again. Never runs two refreshes at once.
function createPoller(task, intervalMs, doc, timers) {
  let timer = null;
  let busy = false;
  const tick = async () => {
    if (busy) return;
    busy = true;
    try { await task(); } catch { /* the task reports its own errors */ } finally { busy = false; }
  };
  const start = () => { if (timer === null && !doc.hidden) timer = timers.setInterval(tick, intervalMs); };
  const stop = () => { if (timer !== null) { timers.clearInterval(timer); timer = null; } };
  doc.addEventListener('visibilitychange', () => {
    if (doc.hidden) { stop(); } else { tick(); start(); }
  });
  start();
  return { start, stop, tick, get running() { return timer !== null; } };
}
if (typeof module !== 'undefined') module.exports = { createPoller };
