# Brave Music Downloader 2.3.0: Phase 1 performance baseline

**Phase 1 only. No application code was changed.** (The release payload hash is identical before and after; see "Reproduce".)
Every number below is generated from `bench/results/baseline.json` by `bench/report.py`.

## 0. Summary: the ranked bottlenecks

For a normal track (normalization on, the default) the work is **85% loudness analysis**, it runs on **one core, one job at a time**, and every progress tick pays a full-sync database commit. Ranked by measured impact:

| # | Bottleneck | Evidence (this machine) | Confidence it matters on your PC |
|---|---|---|---|
| 1 | **Loudness analysis pass (`loudnorm`, single-threaded)** | 4.28 s of 5.04 s for a 3-min track (85%); 81 s of 92 s for a 1-h track (88%); 98 s of 115 s for 20 tracks. Normalization off: 0.40 s (13x faster) | High (CPU-bound; scales with CPU speed) |
| 2 | **Strictly sequential worker**: one job at a time, no overlap of network-bound download with CPU-bound conversion | CPU utilisation 0.98 of one core during a track and 0.98 over 20 tracks; 20 tracks = 115 s = sum of the parts. **Gain not measurable here (1 core, no network)** | High for playlists on a multi-core PC; needs measurement there |
| 3 | **A full-sync DB commit per progress line** (shared lock with the UI) | 67 store updates per 3-min track; each 0.35 ms in the pipeline run (0.22 ms in isolation, 68% of it the commit); fsync here is only 0.13 ms | Medium to high: cost scales with your disk's fsync latency (measure with the Windows script) |
| 4 | **Library scan on every helper start: one commit per file** | 100,000 commits, 16.1 s on first scan of 100k WAVs; rescan 2.0 s at every helper start; +88 MB RAM. One transaction instead of one commit per row: 124 ms vs 1.6 ms per 1,000 rows | Medium (only large libraries; the helper restarts on every reconnect) |
| 5 | **Reconnect latency**: fixed pauses plus lock polling dominate the helper's own start | New helper answers 78.1 ms (p50) / 191.0 ms (p95) after the old one exits; the helper itself starts in 51.4 ms. Plus the extension's fixed 350 ms pause | Low to medium (sub-second; Windows start time unknown) |
| 6 | **Process spawns per track** | 6 per track (5 without normalization, 7 with trim): ffprobe x2, ffmpeg x3, yt-dlp. ~103 ms of spawn-dominated work here = 2% of a normalized track but 25% of a non-normalized one | Unknown on Windows (antivirus scanning of large exes can make spawns several times slower) |
| 7 | **Dashboard/popup polling**: every 2.0 to 2.2 s forever, ~39 KB reply, full DOM rebuild | Host cost per poll 2.74 ms; browser parse 0.06 ms. **DOM/frame cost not measurable without a browser trace** | Unknown (needs a Chrome performance trace) |

Not slow (measured): keyset pagination (0.38 to 0.25 ms from 1k to 100k rows), DB open, native-messaging round trips, cancel (2 to 8 ms, zero orphan processes here), app-side delay before the downloader starts (2.4 ms).

Found along the way (bugs and limits, not speed): the deep dependency check loads the whole tool into RAM (**+300 MB** for a 300 MB file) and always reports a hash mismatch for zip-installed tools; a database with more than 5,000 history rows **refuses new jobs** until finished jobs are cleared.

## 1. Method, environment and what this baseline cannot tell you

* Machine: **1 CPU core**, 3992 MB RAM, Linux-6.18.44-fc-v726-x86_64-with-glibc2.39, Python 3.12.3, SQLite 3.45.1, ffmpeg version 6.1.1-3ubuntu5. fsync of 4 KB: p50 **0.13 ms** (very fast virtual disk; a typical Windows SSD is often 1 to 10 ms, which this report treats as an *assumption to verify* with `bench/Measure-Windows.ps1`).
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
| 3-min, normalize (default) | 5.04 s | 36x | 5.0 | 0.98 | 6/job | 105 MB | 27 MB | 48 MB | 45 MB |
| 3-min, no normalize | 0.40 s | 452x | 0.3 | 0.87 | 5/job | 51 MB | 27 MB | 48 MB | 45 MB |
| 3-min, normalize + trim silence | 4.73 s | 38x | 4.7 | 0.99 | 7/job | 104 MB | 27 MB | 109 MB | 45 MB |
| 20 x 3-min playlist | 114.65 s | 31x | 112.9 | 0.98 | 6/job | 105 MB | 28 MB | 912 MB | 908 MB |
| 1-hour, normalize | 92.06 s | 39x | 91.4 | 0.99 | 6/job | 108 MB | 27 MB | 964 MB | 908 MB |
| 1-hour, no normalize | 4.83 s | 746x | 4.3 | 0.89 | 5/job | 54 MB | 27 MB | 964 MB | 908 MB |

Stage breakdown (milliseconds; share of wall time):

| Stage | 3-min normalize | 3-min no normalize | 1-hour normalize | 20-track playlist (total) |
|---|---|---|---|---|
| yt-dlp(stub: copy + progress lines) | 44 (1%) | 37 (9%) | 67 (0%) | 793 (1%) |
| ffprobe:sample-rate | 33 (1%) | 33 (8%) | 40 (0%) | 787 (1%) |
| ffmpeg:loudnorm-analysis | 4,282 (85%) | - | 81,134 (88%) | 97,852 (85%) |
| ffmpeg:encode-wav | 564 (11%) | 214 (53%) | 10,362 (11%) | 12,493 (11%) |
| ffprobe:validate | 32 (1%) | 33 (8%) | 34 (0%) | 777 (1%) |
| fsync-wav | 45 (1%) | 40 (10%) | 316 (0%) | 928 (1%) |
| ffmpeg:thumbnail | 37 (1%) | 35 (9%) | 68 (0%) | 887 (1%) |

Notes: the stand-in downloader row is not a real download. `trim_start` adds a lossless float32 pre-pass: peak disk 109 MB vs 48 MB (2.3x); for a 1-hour track that temp file would be about 1.3 GB on top of the 0.95 GB WAV (extrapolated from sizes, not run). Block writes per track are about 1.09x the final WAV (no hidden copies on the normal path). `ffmpeg` stderr captured per analysis: ~300 KB per 3-min track (progress lines) held in memory until the call returns.

Loudness analysis cost breakdown (3-min file, this machine): decode only 0.21 s; **`loudnorm` analysis 3.8 to 5.5 s**; `ebur128=peak=true` 1.2 s; `volumedetect` 0.21 s. On the test file `ebur128` reported the same values as `loudnorm` to the precision it prints (I -29.8 vs -29.86 LUFS, true peak -21.6 vs -21.64 dBFS, LRA 0.8 vs 0.80). That is one synthetic file, not evidence of equivalence on real music.

### 3.2 Native host, Native Messaging, reconnect
| Metric | p50 | p95 | max |
|---|---|---|---|
| Python interpreter floor (`python -c pass`) | 8.70 ms | 9.13 | 9.55 |
| Host cold start to first reply (no `.pyc`, new DB) | 67.1 ms | 67.9 | 67.9 |
| Host warm start to first reply | 51.4 ms | 54.4 | 54.7 |
| ... with a 10,000-row DB | 53.2 ms | 56.4 | 56.4 |
| ... with a 100,000-row DB | 54.4 ms | 96.8 | 96.8 |
| Round trip `health` | 0.030 ms | 0.24 | 0.39 |
| Round trip `jobs` limit 50 (36 KB reply, 10k-row DB) | 2.74 ms | 3.46 | 38.2 |
| Round trip `jobs` limit 6 / limit 1 | 1.74 / 0.43 ms | 2.29 / 0.67 | |
| New helper answers after old one exits gracefully | 78.1 ms | 191.0 | 191.0 |
| ... after the old one is killed | 64.3 ms | 150.6 | 150.6 |

Pipelined throughput 863 `jobs` requests/s; host CPU 1.56 s and peak RSS 26 MB over 1501 requests. Import time of the host modules: 64.6 ms under `-X importtime`, spread thin (largest: helper 8.0 ms, native_host 4.2 ms, store 2.7 ms, music_metadata 2.0 ms). Reconnect is also bounded by the extension's fixed 350 ms pause and the host's 200 ms lock-poll interval. **Service-worker suspension and wake-up cannot be measured without a browser.**

### 3.3 SQLite (WAL, `synchronous=FULL`), generated through the app's own `Store`
| | 1,000 rows | 10,000 rows | 100,000 rows |
|---|---|---|---|
| DB size | 1.2 MB | 12.3 MB | 124.1 MB |
| Open (p50) | 0.19 ms | 0.17 ms | 0.21 ms |
| Page of 50, first (p50 / p95) | 0.38 / 0.46 ms | 0.23 / 0.26 ms | 0.24 / 0.29 ms |
| Page of 50 at 95% depth (keyset) | 0.24 ms | 0.28 ms | 0.25 ms |
| Filter state=done / active (p50) | 0.24 / 0.15 ms | 0.27 / 0.36 ms | 0.24 / 1.48 ms |
| Counts (GROUP BY) | 0.038 ms | 0.35 ms | 5.38 ms |
| Walk every page of 200 | 6 ms (5 pages) | 57 ms (50 pages) | 578 ms (500 pages) |
| Progress `update()` (p50 / p95) | 0.19 / 0.23 ms | 0.20 / 0.26 ms | 0.20 / 0.28 ms |
| `update()` split: commit / JSON / execute | 0.092 / 0.023 / 0.011 ms | 0.149 / 0.034 / 0.019 ms | 0.123 / 0.025 / 0.013 ms |
| 1,000 library inserts: commit each vs one transaction | 113 vs 2.4 ms | 114 vs 2.1 ms | 124 vs 1.6 ms |
| `clear_finished` (DELETE all finished) | 5 ms | 72 ms | 880 ms |
| Reference: `payload LIKE` full scan | 0.6 ms | 4.7 ms | 35.5 ms |
| Enqueue of new jobs | 200 jobs in 4 ms | refused: Queue history limit reached; clear finished jobs first | refused: Queue history limit reached; clear finished jobs first |

No text search exists in 2.3.0 (the dashboard filters by state only), so there is no search to measure; the `LIKE` row is a reference for what a naive future search would cost. Index plans were verified in the unit tests (`jobs_page`, `jobs_state_ord`). The 5,000-row cap in `enqueue` means the 10k and 100k tables could only be built by writing rows directly; the app itself would refuse new jobs there. Pagination is flat because it is keyset based.

### 3.4 Library scan at helper start (`reconcile_library`) and cache sweep
| WAV files | First scan (all new) | Commits | Rescan (nothing new) | 1,000 `find_existing` | Peak RSS |
|---|---|---|---|---|---|
| 1,000 | 0.17 s | 1,000 | 0.02 s | 9 ms | 28 MB (was 27) |
| 10,000 | 1.62 s | 10,000 | 0.23 s | 11 ms | 38 MB (was 28) |
| 100,000 | 16.07 s | 100,000 | 2.02 s | 14 ms | 126 MB (was 38) |

Time per file on first scan is 161 us, versus 20 us for walking and checking the same file; the difference is the commit per file. **Illustration, not a measurement:** with an fsync of 1 ms or 5 ms (assumed Windows values) the 100,000-commit first scan would take about 103 s or 503 s. Cache sweep: scanning 10,000 job-cache folders takes 65 ms, removing them 1.3 s.

### 3.5 Cancellation and retries
Cancel (store flips to canceled, process killed, worker returns) on Linux: during download p50 2.4 ms to process death, 2.8 ms until the worker returns; during the analysis pass 6.0 / 6.9 ms. In all 10 runs: job state `canceled`, **0 orphan child processes, no `.processing-*` temp file, job cache removed**. Windows uses `taskkill /T /F` (an extra process start): not measured here. Retry back-off is fixed at 5 s then 15 s before attempts 2 and 3 (transient errors only); permanent errors fail at once.

### 3.6 UI and message traffic (static facts plus measured payloads)
Dashboard polls every 2,200 ms and the popup every 2,000 ms (`setInterval`, no visibility check, so a hidden dashboard keeps polling); each poll returns up to 50 jobs (~39 KB, about 18 KB/s). The dashboard list replaces every node on every poll (`replaceChildren`, 1 `createElement` call per job in `fill`). Measured: host handles a poll in 2.74 ms, the browser parses it in 0.06 ms. **Layout/paint cost and frame drops are not measured** (no browser).

### 3.7 Installer
| Item | Size |
|---|---|
| `Application.zip` payload | 78.7 KB (25 files, 213 KB unpacked) |
| `Setup.ps1` | 8.7 KB |
| `SetupStub.cs` (builds the exe on Windows) | 2.6 KB |
| Component downloads at install time | Python embed zip (~10 MB), yt-dlp.exe, Deno zip, FFmpeg essentials zip (published as about 88 MB by a third-party page): **sizes not measured by me**; `Measure-Windows.ps1` reads them with HEAD requests |

Install time, idle RAM and disk footprint need a real Windows install (the installer has no unattended mode, so a stopwatch or the Install.log transcript is the only source today).

## 4. Ranked bottlenecks in detail

**1. Loudness analysis pass.** Evidence in 3.1. It is a single-threaded filter running at 42x realtime here, about 7.6x the cost of the encode that follows it and 21x a plain decode. Levers, in order of safety: (a) run several tracks at once (see #2), which uses idle cores without changing any output; (b) overlap with downloads (#2); (c) replace the measurement filter with a cheaper one: measured 3.3x faster here but it changes the normalization method, so it needs your approval and a corpus check before anyone ships it. Not a lever: `-nostats` (no measurable CPU change).

**2. Sequential worker.** `worker_loop` takes one job at a time. On this 1-core machine the track is already 98% CPU-bound, so concurrency cannot help *here*, but on an N-core PC (N-1)/N of the CPU is idle during a playlist (inference from the code and the utilisation figure, not a measurement). Real downloads add network-bound time per track that the worker also waits through. If download takes D s and conversion C s per track, serial total is 20(D+C) and a two-stage pipeline approaches 20 x max(D,C) + min(D,C); measured C here is 5.0 s (3-min track), D is unmeasured. Risk: store-lock contention, cancellation races, orphan files; needs concurrency tests.

**3. Commit per progress line.** The store is `synchronous=FULL`; each `update()` is one transaction and one fsync, and holds the same lock the UI's `jobs` request needs. 67 updates per track here. Real yt-dlp progress output is not simulated; a real download will produce more or fewer. Cost per track is roughly updates x (about 0.23 ms + fsync latency): with a 5 ms fsync (assumed) that is 0.35 s of lock-holding time per 3-min track. Lever: coalesce progress updates (for example at most 2 per second and only when the integer percent changes) while keeping full sync for state changes. No schema change; behavior and recovery guarantees unchanged.

**4. Library scan.** Evidence in 3.4. Levers: batch inserts in one transaction (no schema change); avoid rescanning an unchanged library at every helper start (needs a safe invalidation rule, for example directory modification times); stream paths instead of holding all of them (the 88 MB peak at 100k files).

**5. Reconnect.** Fixed delays dominate: 350 ms extension pause plus up to 200 ms lock polling plus a ~50 ms helper start. Levers: lower the poll interval, add jitter to the extension pause, lazy-import rarely used modules. All are small (a few hundred ms) and the service-worker side needs a browser to verify.

**6. Process spawns.** Spawn latency here (varied between 32 and 58 ms across my runs): `ffprobe -version` 36.0 ms, `ffmpeg -version` 31.7 ms, Python 9.60 ms (p50). Candidate cuts: take the sample rate from yt-dlp's info json instead of a separate ffprobe, and fold small ffmpeg calls together. The validation probe must stay (integrity). Only worth doing if the Windows numbers show spawn cost matters.

**7. Polling and DOM rebuild.** Pause polling while the page is hidden and update only changed rows. The `counts` query that rides along with every poll grows linearly with history (0.038 ms at 1k rows, 5.38 ms at 100k), so a long-lived install pays it every 2 s. Low risk; needs a browser trace to quantify the DOM side.

## 5. Bugs and limits found while measuring (regression tests will accompany any fix)
1. `diagnostics(deep=True)` reads the whole tool binary into memory: **+300 MB RSS** for a 300 MB file (427 ms), and `hash_matches_install` compares the *archive* hash with the *extracted file* hash, so it reports False for zip-installed tools. The UI never sends `deep`, so it is latent.
2. History cap: `enqueue` raises "Queue history limit reached" at 5,000 rows and nothing prunes automatically, so heavy users will eventually be unable to queue anything until they clear finished jobs. This is a product decision, not just speed.
3. Each helper start rescans the whole music folder (rescan 2.0 s per 100k files).

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
`python bench/run.py <section>` (one section per run on a small machine), then `python bench/report.py`. The application payload hash before and after the benchmark work is identical: `python scripts/build_release.py --out /tmp/x --skip-exe` gives Application.zip SHA-256 `55ff44338dd1c7c1606f0f0445ed9c5aa94f95e5e1a44cb2b185503317d7ce89`.
