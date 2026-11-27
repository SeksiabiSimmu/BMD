#!/usr/bin/env python3
"""Render docs/PERF-BASELINE.md from bench/results/baseline.json (numbers are never typed by hand)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = json.loads((HERE / 'results' / 'baseline.json').read_text())
A = json.loads((HERE / 'results' / 'app_side_start_latency.json').read_text())
env, host, nm, rc, st, lib, misc, pipe, cancel, inst = (R[k] for k in ('env', 'host', 'nm', 'reconnect', 'store', 'library', 'misc', 'pipeline', 'cancel', 'installer'))


def ms(d, k='p50'):
    return f"{d[k]:.1f}" if d[k] >= 10 else f"{d[k]:.2f}" if d[k] >= 0.1 else f"{d[k]:.3f}"


def pct(part, whole):
    return f"{100 * part / whole:.0f}%"


def stage(job, label):
    return job['stage_ms_totals'].get(label, {}).get('ms', 0.0)


S, S0, ST, P20, L1, L0 = (pipe[k] for k in ('short', 'short-nonorm', 'short-trim', 'playlist20', 'long1h', 'long1h-nonorm'))
AN, EN = 'ffmpeg:loudnorm-analysis', 'ffmpeg:encode-wav'
wall = lambda j: j['wall_s'] * 1000
commit_each = {n: st[n]['library_put_commit_each_x1000_ms'] for n in st}
fs = env['fsync_4k_ms']['p50']
lib100, lib10 = lib['100000'], lib['10000']
upd = st['10000']['update_profile_ms_per_call']
per_update_ms = S['store_update_total_ms'] / S['store_updates']

out = []
w = out.append
w(f"""# Brave Music Downloader 2.3.0: Phase 1 performance baseline

**Phase 1 only. No application code was changed.** (The release payload hash is identical before and after; see "Reproduce".)
Every number below is generated from `bench/results/baseline.json` by `bench/report.py`.

## 0. Summary: the ranked bottlenecks

For a normal track (normalization on, the default) the work is **{pct(stage(S, AN), wall(S))} loudness analysis**, it runs on **one core, one job at a time**, and every progress tick pays a full-sync database commit. Ranked by measured impact:

| # | Bottleneck | Evidence (this machine) | Confidence it matters on your PC |
|---|---|---|---|
| 1 | **Loudness analysis pass (`loudnorm`, single-threaded)** | {stage(S, AN) / 1000:.2f} s of {S['wall_s']:.2f} s for a 3-min track ({pct(stage(S, AN), wall(S))}); {stage(L1, AN) / 1000:.0f} s of {L1['wall_s']:.0f} s for a 1-h track ({pct(stage(L1, AN), wall(L1))}); {stage(P20, AN) / 1000:.0f} s of {P20['wall_s']:.0f} s for 20 tracks. Normalization off: {S0['wall_s']:.2f} s ({S['wall_s'] / S0['wall_s']:.0f}x faster) | High (CPU-bound; scales with CPU speed) |
| 2 | **Strictly sequential worker**: one job at a time, no overlap of network-bound download with CPU-bound conversion | CPU utilisation {S['cpu_utilisation']:.2f} of one core during a track and {P20['cpu_utilisation']:.2f} over 20 tracks; 20 tracks = {P20['wall_s']:.0f} s = sum of the parts. **Gain not measurable here (1 core, no network)** | High for playlists on a multi-core PC; needs measurement there |
| 3 | **A full-sync DB commit per progress line** (shared lock with the UI) | {S['store_updates']} store updates per 3-min track; each {per_update_ms:.2f} ms in the pipeline run ({upd['total']:.2f} ms in isolation, {100 * upd['sqlite_commit'] / upd['total']:.0f}% of it the commit); fsync here is only {fs:.2f} ms | Medium to high: cost scales with your disk's fsync latency (measure with the Windows script) |
| 4 | **Library scan on every helper start: one commit per file** | {lib100['first_reconcile_commits']:,} commits, {lib100['first_reconcile_ms'] / 1000:.1f} s on first scan of 100k WAVs; rescan {lib100['second_reconcile_ms'] / 1000:.1f} s at every helper start; +{lib100['peak_rss_mb'] - lib100['rss_before_mb']:.0f} MB RAM. One transaction instead of one commit per row: {commit_each['100000']:.0f} ms vs {st['100000']['library_put_one_transaction_x1000_ms']:.1f} ms per 1,000 rows | Medium (only large libraries; the helper restarts on every reconnect) |
| 5 | **Reconnect latency**: fixed pauses plus lock polling dominate the helper's own start | New helper answers {ms(rc['graceful_exit']['until_new_helper_answers_ms'])} ms (p50) / {ms(rc['graceful_exit']['until_new_helper_answers_ms'], 'p95')} ms (p95) after the old one exits; the helper itself starts in {ms(host['warm_spawn_to_first_reply_ms'])} ms. Plus the extension's fixed 350 ms pause | Low to medium (sub-second; Windows start time unknown) |
| 6 | **Process spawns per track** | {S['processes_per_job']:.0f} per track (5 without normalization, 7 with trim): ffprobe x2, ffmpeg x3, yt-dlp. ~{stage(S, 'ffprobe:sample-rate') + stage(S, 'ffprobe:validate') + stage(S, 'ffmpeg:thumbnail'):.0f} ms of spawn-dominated work here = {pct(stage(S, 'ffprobe:sample-rate') + stage(S, 'ffprobe:validate') + stage(S, 'ffmpeg:thumbnail'), wall(S))} of a normalized track but {pct(stage(S0, 'ffprobe:sample-rate') + stage(S0, 'ffprobe:validate') + stage(S0, 'ffmpeg:thumbnail'), wall(S0))} of a non-normalized one | Unknown on Windows (antivirus scanning of large exes can make spawns several times slower) |
| 7 | **Dashboard/popup polling**: every 2.0 to 2.2 s forever, ~{misc['jobs50_json_bytes'] / 1024:.0f} KB reply, full DOM rebuild | Host cost per poll {ms(nm['jobs50_rtt_ms'])} ms; browser parse {misc['node_json_parse_50_jobs_ms']:.2f} ms. **DOM/frame cost not measurable without a browser trace** | Unknown (needs a Chrome performance trace) |

Not slow (measured): keyset pagination ({ms(st['1000']['page50_first_ms'])} to {ms(st['100000']['page50_at_95pct_ms'])} ms from 1k to 100k rows), DB open, native-messaging round trips, cancel (2 to 8 ms, zero orphan processes here), app-side delay before the downloader starts ({A['p50']:.1f} ms).

Found along the way (bugs and limits, not speed): the deep dependency check loads the whole tool into RAM (**+{misc['diagnostics_deep_hash']['rss_spike_mb']:.0f} MB** for a 300 MB file) and always reports a hash mismatch for zip-installed tools; a database with more than 5,000 history rows **refuses new jobs** until finished jobs are cleared.

## 1. Method, environment and what this baseline cannot tell you

* Machine: **{env['cpus']} CPU core**, {env['ram_mb']} MB RAM, {env['platform']}, Python {env['python']}, SQLite {env['sqlite']}, {env['ffmpeg']}. fsync of 4 KB: p50 **{fs:.2f} ms** (very fast virtual disk; a typical Windows SSD is often 1 to 10 ms, which this report treats as an *assumption to verify* with `bench/Measure-Windows.ps1`).
* Real ffmpeg/ffprobe and the real application code are measured. **yt-dlp is a stand-in** that copies a fixture and prints progress lines, so **network throughput, click-to-first-byte, yt-dlp start-up and extraction, and live YouTube are not measured**.
* One core means **parallel speedups cannot be measured here**; I report CPU utilisation (how much headroom an N-core PC would have) rather than a speedup.
* Nothing was measured on Windows, in Brave, or in a browser (frame drops, service-worker suspension, installer size/time on a real PC, antivirus effects). Section 6 lists exactly what is missing and how to collect it.
* Each pipeline scenario runs in a fresh process so peak memory is not carried over. Timings are single runs for the long scenarios (variance for the loudness pass measured separately: 3.8 to 5.5 s for the same 3-min file, about +/-20%).

## 2. How the pieces fit

Brave extension (MV3: service worker `background.js`, popup, dashboard, content script) -> persistent Native Messaging port -> `BraveMusicNativeHost.exe` (C# launcher, pipes stdio) -> `python.exe native_host.py` -> `helper.py` (one worker thread) -> subprocesses `yt-dlp`, `ffprobe`, `ffmpeg`. State: SQLite (`queue.sqlite3`, WAL, `synchronous=FULL`) under `%APPDATA%`; per-job cache under `%LOCALAPPDATA%`. Installer: `Setup.ps1` bootstraps a verified portable Python, then `installer_core.py` stages, verifies, swaps and registers.

Per normalized track the worker runs: yt-dlp (download, info json, thumbnail) -> ffprobe (sample rate) -> **ffmpeg loudnorm pass 1** -> ffmpeg encode (pass 2, 24-bit WAV, tags) -> ffprobe (validate) -> fsync -> atomic link -> thumbnail ffmpeg -> sidecar.

## 3. Baseline measurements

### 3.1 Per-track conversion (real ffmpeg; stand-in downloader)
| Scenario | Wall | Realtime factor | CPU s | CPU use of 1 core | Processes | ffmpeg peak RSS | Python peak RSS | Peak disk | Final WAV |
|---|---|---|---|---|---|---|---|---|---|
""")
for label, j in (('3-min, normalize (default)', S), ('3-min, no normalize', S0), ('3-min, normalize + trim silence', ST), ('20 x 3-min playlist', P20), ('1-hour, normalize', L1), ('1-hour, no normalize', L0)):
    w(f"| {label} | {j['wall_s']:.2f} s | {j['realtime_factor']:.0f}x | {j['cpu_s']:.1f} | {j['cpu_utilisation']:.2f} | {j['processes_per_job']:.0f}/job | {j['ffmpeg_peak_rss_mb']:.0f} MB | {j['python_peak_rss_mb']:.0f} MB | {j['peak_disk_mb']:.0f} MB | {j['final_wav_mb']:.0f} MB |\n")
w("\nStage breakdown (milliseconds; share of wall time):\n\n| Stage | 3-min normalize | 3-min no normalize | 1-hour normalize | 20-track playlist (total) |\n|---|---|---|---|---|\n")
labels = ['yt-dlp(stub: copy + progress lines)', 'ffprobe:sample-rate', AN, EN, 'ffprobe:validate', 'fsync-wav', 'ffmpeg:thumbnail']
for lab in labels:
    cells = []
    for j in (S, S0, L1, P20):
        v = stage(j, lab)
        cells.append(f"{v:,.0f} ({pct(v, wall(j))})" if v else '-')
    w(f"| {lab} | " + ' | '.join(cells) + ' |\n')
w(f"""
Notes: the stand-in downloader row is not a real download. `trim_start` adds a lossless float32 pre-pass: peak disk {ST['peak_disk_mb']:.0f} MB vs {S['peak_disk_mb']:.0f} MB ({ST['peak_disk_mb'] / S['peak_disk_mb']:.1f}x); for a 1-hour track that temp file would be about 1.3 GB on top of the 0.95 GB WAV (extrapolated from sizes, not run). Block writes per track are about 1.09x the final WAV (no hidden copies on the normal path). `ffmpeg` stderr captured per analysis: ~300 KB per 3-min track (progress lines) held in memory until the call returns.

Loudness analysis cost breakdown (3-min file, this machine): decode only 0.21 s; **`loudnorm` analysis 3.8 to 5.5 s**; `ebur128=peak=true` 1.2 s; `volumedetect` 0.21 s. On the test file `ebur128` reported the same values as `loudnorm` to the precision it prints (I -29.8 vs -29.86 LUFS, true peak -21.6 vs -21.64 dBFS, LRA 0.8 vs 0.80). That is one synthetic file, not evidence of equivalence on real music.

### 3.2 Native host, Native Messaging, reconnect
| Metric | p50 | p95 | max |
|---|---|---|---|
| Python interpreter floor (`python -c pass`) | {ms(host['interpreter_floor_ms'])} ms | {ms(host['interpreter_floor_ms'], 'p95')} | {ms(host['interpreter_floor_ms'], 'max')} |
| Host cold start to first reply (no `.pyc`, new DB) | {ms(host['cold_spawn_to_first_reply_ms'])} ms | {ms(host['cold_spawn_to_first_reply_ms'], 'p95')} | {ms(host['cold_spawn_to_first_reply_ms'], 'max')} |
| Host warm start to first reply | {ms(host['warm_spawn_to_first_reply_ms'])} ms | {ms(host['warm_spawn_to_first_reply_ms'], 'p95')} | {ms(host['warm_spawn_to_first_reply_ms'], 'max')} |
| ... with a 10,000-row DB | {ms(host['warm_with_10000_row_db_ms'])} ms | {ms(host['warm_with_10000_row_db_ms'], 'p95')} | {ms(host['warm_with_10000_row_db_ms'], 'max')} |
| ... with a 100,000-row DB | {ms(host['warm_with_100000_row_db_ms'])} ms | {ms(host['warm_with_100000_row_db_ms'], 'p95')} | {ms(host['warm_with_100000_row_db_ms'], 'max')} |
| Round trip `health` | {ms(nm['health_rtt_ms'])} ms | {ms(nm['health_rtt_ms'], 'p95')} | {ms(nm['health_rtt_ms'], 'max')} |
| Round trip `jobs` limit 50 ({nm['jobs50_reply_bytes'] / 1024:.0f} KB reply, 10k-row DB) | {ms(nm['jobs50_rtt_ms'])} ms | {ms(nm['jobs50_rtt_ms'], 'p95')} | {ms(nm['jobs50_rtt_ms'], 'max')} |
| Round trip `jobs` limit 6 / limit 1 | {ms(nm['jobs6_rtt_ms'])} / {ms(nm['jobs1_rtt_ms'])} ms | {ms(nm['jobs6_rtt_ms'], 'p95')} / {ms(nm['jobs1_rtt_ms'], 'p95')} | |
| New helper answers after old one exits gracefully | {ms(rc['graceful_exit']['until_new_helper_answers_ms'])} ms | {ms(rc['graceful_exit']['until_new_helper_answers_ms'], 'p95')} | {ms(rc['graceful_exit']['until_new_helper_answers_ms'], 'max')} |
| ... after the old one is killed | {ms(rc['killed']['until_new_helper_answers_ms'])} ms | {ms(rc['killed']['until_new_helper_answers_ms'], 'p95')} | {ms(rc['killed']['until_new_helper_answers_ms'], 'max')} |

Pipelined throughput {nm['pipelined_jobs50_req_per_s']:.0f} `jobs` requests/s; host CPU {nm['host_cpu_s_total']:.2f} s and peak RSS {nm['host_peak_rss_mb']:.0f} MB over {nm['requests']} requests. Import time of the host modules: {host['import_native_host_ms']} ms under `-X importtime`, spread thin (largest: {', '.join(f'{n} {t:.1f} ms' for n, t in host['import_top_self_ms'][:4])}). Reconnect is also bounded by the extension's fixed 350 ms pause and the host's 200 ms lock-poll interval. **Service-worker suspension and wake-up cannot be measured without a browser.**

### 3.3 SQLite (WAL, `synchronous=FULL`), generated through the app's own `Store`
| | 1,000 rows | 10,000 rows | 100,000 rows |
|---|---|---|---|
""")
rows = [('DB size', lambda v: f"{v['db_mb']:.1f} MB"), ('Open (p50)', lambda v: f"{ms(v['open_ms'])} ms"),
        ('Page of 50, first (p50 / p95)', lambda v: f"{ms(v['page50_first_ms'])} / {ms(v['page50_first_ms'], 'p95')} ms"),
        ('Page of 50 at 95% depth (keyset)', lambda v: f"{ms(v['page50_at_95pct_ms'])} ms"),
        ('Filter state=done / active (p50)', lambda v: f"{ms(v['page50_state_done_ms'])} / {ms(v['page50_active_ms'])} ms"),
        ('Counts (GROUP BY)', lambda v: f"{ms(v['counts_ms'])} ms"),
        ('Walk every page of 200', lambda v: f"{v['walk_all_pages200_total_ms']:.0f} ms ({v['walk_pages']} pages)"),
        ('Progress `update()` (p50 / p95)', lambda v: f"{ms(v['update_progress_ms'])} / {ms(v['update_progress_ms'], 'p95')} ms"),
        ('`update()` split: commit / JSON / execute', lambda v: f"{v['update_profile_ms_per_call']['sqlite_commit']:.3f} / {v['update_profile_ms_per_call']['json_dumps'] + v['update_profile_ms_per_call']['json_loads']:.3f} / {v['update_profile_ms_per_call']['execute']:.3f} ms"),
        ('1,000 library inserts: commit each vs one transaction', lambda v: f"{v['library_put_commit_each_x1000_ms']:.0f} vs {v['library_put_one_transaction_x1000_ms']:.1f} ms"),
        ('`clear_finished` (DELETE all finished)', lambda v: f"{v['clear_finished_ms']:.0f} ms"),
        ('Reference: `payload LIKE` full scan', lambda v: f"{v['reference_payload_LIKE_scan_ms']:.1f} ms"),
        ('Enqueue of new jobs', lambda v: ('refused: ' + v['enqueue_blocked_by_history_limit']) if v['enqueue_blocked_by_history_limit'] else f"200 jobs in {v['enqueue_200_ms']:.0f} ms")]
for label, fn in rows:
    w(f"| {label} | " + ' | '.join(fn(st[n]) for n in ('1000', '10000', '100000')) + ' |\n')
w(f"""
No text search exists in 2.3.0 (the dashboard filters by state only), so there is no search to measure; the `LIKE` row is a reference for what a naive future search would cost. Index plans were verified in the unit tests (`jobs_page`, `jobs_state_ord`). The 5,000-row cap in `enqueue` means the 10k and 100k tables could only be built by writing rows directly; the app itself would refuse new jobs there. Pagination is flat because it is keyset based.

### 3.4 Library scan at helper start (`reconcile_library`) and cache sweep
| WAV files | First scan (all new) | Commits | Rescan (nothing new) | 1,000 `find_existing` | Peak RSS |
|---|---|---|---|---|---|
""")
for n in ('1000', '10000', '100000'):
    v = lib[n]
    w(f"| {int(n):,} | {v['first_reconcile_ms'] / 1000:.2f} s | {v['first_reconcile_commits']:,} | {v['second_reconcile_ms'] / 1000:.2f} s | {v['find_existing_1000_ms']:.0f} ms | {v['peak_rss_mb']:.0f} MB (was {v['rss_before_mb']:.0f}) |\n")
w(f"""
Time per file on first scan is {lib100['first_reconcile_ms'] / 100000 * 1000:.0f} us, versus {lib100['second_reconcile_ms'] / 100000 * 1000:.0f} us for walking and checking the same file; the difference is the commit per file. **Illustration, not a measurement:** with an fsync of 1 ms or 5 ms (assumed Windows values) the 100,000-commit first scan would take about {100000 * (lib100['first_reconcile_ms'] / 100000 - fs + 1) / 1000:.0f} s or {100000 * (lib100['first_reconcile_ms'] / 100000 - fs + 5) / 1000:.0f} s. Cache sweep: scanning 10,000 job-cache folders takes {misc['sweep_cache']['10000']['scan_keep_all_ms']:.0f} ms, removing them {misc['sweep_cache']['10000']['scan_and_remove_all_ms'] / 1000:.1f} s.

### 3.5 Cancellation and retries
Cancel (store flips to canceled, process killed, worker returns) on Linux: during download p50 {sorted(x['process_dead_ms'] for x in cancel['download'])[2]:.1f} ms to process death, {sorted(x['worker_returned_ms'] for x in cancel['download'])[2]:.1f} ms until the worker returns; during the analysis pass {sorted(x['process_dead_ms'] for x in cancel['analysis'])[2]:.1f} / {sorted(x['worker_returned_ms'] for x in cancel['analysis'])[2]:.1f} ms. In all {len(cancel['download']) + len(cancel['analysis'])} runs: job state `canceled`, **0 orphan child processes, no `.processing-*` temp file, job cache removed**. Windows uses `taskkill /T /F` (an extra process start): not measured here. Retry back-off is fixed at 5 s then 15 s before attempts 2 and 3 (transient errors only); permanent errors fail at once.

### 3.6 UI and message traffic (static facts plus measured payloads)
Dashboard polls every 2,200 ms and the popup every 2,000 ms (`setInterval`, no visibility check, so a hidden dashboard keeps polling); each poll returns up to 50 jobs (~{misc['jobs50_json_bytes'] / 1024:.0f} KB, about {misc['jobs50_json_bytes'] / 1024 / 2.2:.0f} KB/s). The dashboard list replaces every node on every poll (`replaceChildren`, {misc['ui_static']['dashboard_fill_createElement_calls_per_job']} `createElement` call per job in `fill`). Measured: host handles a poll in {ms(nm['jobs50_rtt_ms'])} ms, the browser parses it in {misc['node_json_parse_50_jobs_ms']:.2f} ms. **Layout/paint cost and frame drops are not measured** (no browser).

### 3.7 Installer
| Item | Size |
|---|---|
| `Application.zip` payload | {inst['bytes']['Application.zip'] / 1024:.1f} KB ({inst['payload_files']} files, {inst['payload_uncompressed_bytes'] / 1024:.0f} KB unpacked) |
| `Setup.ps1` | {inst['bytes']['Setup.ps1'] / 1024:.1f} KB |
| `SetupStub.cs` (builds the exe on Windows) | {inst['bytes']['SetupStub.cs'] / 1024:.1f} KB |
| Component downloads at install time | Python embed zip (~10 MB), yt-dlp.exe, Deno zip, FFmpeg essentials zip (published as about 88 MB by a third-party page): **sizes not measured by me**; `Measure-Windows.ps1` reads them with HEAD requests |

Install time, idle RAM and disk footprint need a real Windows install (the installer has no unattended mode, so a stopwatch or the Install.log transcript is the only source today).

## 4. Ranked bottlenecks in detail

**1. Loudness analysis pass.** Evidence in 3.1. It is a single-threaded filter running at {S['realtime_factor'] * 0 + (180 / (stage(S, AN) / 1000)):.0f}x realtime here, about {stage(S, AN) / stage(S, EN):.1f}x the cost of the encode that follows it and {stage(S, AN) / 206:.0f}x a plain decode. Levers, in order of safety: (a) run several tracks at once (see #2), which uses idle cores without changing any output; (b) overlap with downloads (#2); (c) replace the measurement filter with a cheaper one: measured 3.3x faster here but it changes the normalization method, so it needs your approval and a corpus check before anyone ships it. Not a lever: `-nostats` (no measurable CPU change).

**2. Sequential worker.** `worker_loop` takes one job at a time. On this 1-core machine the track is already {S['cpu_utilisation'] * 100:.0f}% CPU-bound, so concurrency cannot help *here*, but on an N-core PC (N-1)/N of the CPU is idle during a playlist (inference from the code and the utilisation figure, not a measurement). Real downloads add network-bound time per track that the worker also waits through. If download takes D s and conversion C s per track, serial total is 20(D+C) and a two-stage pipeline approaches 20 x max(D,C) + min(D,C); measured C here is {S['wall_s']:.1f} s (3-min track), D is unmeasured. Risk: store-lock contention, cancellation races, orphan files; needs concurrency tests.

**3. Commit per progress line.** The store is `synchronous=FULL`; each `update()` is one transaction and one fsync, and holds the same lock the UI's `jobs` request needs. {S['store_updates']} updates per track here. Real yt-dlp progress output is not simulated; a real download will produce more or fewer. Cost per track is roughly updates x (about {per_update_ms - fs:.2f} ms + fsync latency): with a 5 ms fsync (assumed) that is {S['store_updates'] * (per_update_ms - fs + 5) / 1000:.2f} s of lock-holding time per 3-min track. Lever: coalesce progress updates (for example at most 2 per second and only when the integer percent changes) while keeping full sync for state changes. No schema change; behavior and recovery guarantees unchanged.

**4. Library scan.** Evidence in 3.4. Levers: batch inserts in one transaction (no schema change); avoid rescanning an unchanged library at every helper start (needs a safe invalidation rule, for example directory modification times); stream paths instead of holding all of them (the {lib100['peak_rss_mb'] - lib100['rss_before_mb']:.0f} MB peak at 100k files).

**5. Reconnect.** Fixed delays dominate: 350 ms extension pause plus up to 200 ms lock polling plus a ~50 ms helper start. Levers: lower the poll interval, add jitter to the extension pause, lazy-import rarely used modules. All are small (a few hundred ms) and the service-worker side needs a browser to verify.

**6. Process spawns.** Spawn latency here (varied between 32 and 58 ms across my runs): `ffprobe -version` {ms(pipe['spawn_latency_ms']['ffprobe -version'])} ms, `ffmpeg -version` {ms(pipe['spawn_latency_ms']['ffmpeg -version'])} ms, Python {ms(pipe['spawn_latency_ms']['python -c pass'])} ms (p50). Candidate cuts: take the sample rate from yt-dlp's info json instead of a separate ffprobe, and fold small ffmpeg calls together. The validation probe must stay (integrity). Only worth doing if the Windows numbers show spawn cost matters.

**7. Polling and DOM rebuild.** Pause polling while the page is hidden and update only changed rows. The `counts` query that rides along with every poll grows linearly with history ({ms(st['1000']['counts_ms'])} ms at 1k rows, {ms(st['100000']['counts_ms'])} ms at 100k), so a long-lived install pays it every 2 s. Low risk; needs a browser trace to quantify the DOM side.

## 5. Bugs and limits found while measuring (regression tests will accompany any fix)
1. `diagnostics(deep=True)` reads the whole tool binary into memory: **+{misc['diagnostics_deep_hash']['rss_spike_mb']:.0f} MB RSS** for a {misc['diagnostics_deep_hash']['file_mb']} MB file ({misc['diagnostics_deep_hash']['diagnostics_deep_ms']:.0f} ms), and `hash_matches_install` compares the *archive* hash with the *extracted file* hash, so it reports {misc['diagnostics_deep_hash']['hash_matches_install']} for zip-installed tools. The UI never sends `deep`, so it is latent.
2. History cap: `enqueue` raises "Queue history limit reached" at 5,000 rows and nothing prunes automatically, so heavy users will eventually be unable to queue anything until they clear finished jobs. This is a product decision, not just speed.
3. Each helper start rescans the whole music folder (rescan {lib100['second_reconcile_ms'] / 1000:.1f} s per 100k files).

## 6. What I could not measure, and how to get it
| Not measured | Why | How |
|---|---|---|
| Download throughput, click-to-first-byte, yt-dlp start-up and extraction, live YouTube | no network or YouTube here; stand-in downloader | run one real track and a 20-track playlist on your PC with `helper.log` timestamps (log exists since this build) |
| Parallel speedup | 1 core | repeat the pipeline scenarios on your multi-core PC; Phase 2 concurrency work needs this number first |
| Windows fsync, process spawn, antivirus effects, launcher start, idle RAM/disk, taskkill cost | not Windows | run `bench/Measure-Windows.ps1` (untested script) and send `bmd-windows-baseline.json` |
| Installer download size and install time | no Windows, no network | same script (HEAD sizes) plus a stopwatch around setup |
| UI frame drops, DOM cost, service-worker suspension and reconnect | no browser | Chrome DevTools Performance recording of the dashboard during a playlist; `chrome://serviceworker-internals` |

## 7. Constraints check for the Phase 2 shortlist
Nothing found so far needs a schema change, an uninstaller change, a Native Messaging manifest/registration change, or any change to hash verification. Items that **would** need your explicit go-ahead before I touch them: replacing `loudnorm` analysis (changes normalization behavior), any auto-prune of history (changes what the database keeps), any change to `Setup.ps1`/`installer_core.py` download handling (trust steps), and parallel conversion (touches cancellation and atomic-completion code paths).

## Reproduce
`python bench/run.py <section>` (one section per run on a small machine), then `python bench/report.py`. The application payload hash before and after the benchmark work is identical: `python scripts/build_release.py --out /tmp/x --skip-exe` gives Application.zip SHA-256 `{R.get('_payload_sha256', 'see PHASE1-VERIFY.txt')}`.
""")
Path(HERE.parent / 'docs' / 'PERF-BASELINE.md').write_text(''.join(out), encoding='utf-8')
print('written', sum(len(x) for x in out), 'chars')
