# Phase 2: safe optimizations, measured before and after

Scope: only the items from the Phase 1 ranking that need no approval under your rules. Each is its own commit with its numbers in the message. **Nothing here touches the uninstaller, deletion paths, the database schema, the Native Messaging manifest or registration, or the installer's trust and verification steps.** Same machine as Phase 1: 1 CPU core, fsync about 0.13 ms (Windows disks are usually slower, so I/O gains below are likely understated for Windows; that is an expectation, not a measurement).

## 1. Results

| # | Change | Metric | Before | After |
|---|---|---|---|---|
| 1 | Coalesce progress updates (max 4/s) | store updates, 3-min track (real pipeline) | 67 | 9 (87% fewer) |
| | | time inside `update()`, same track | 23.6 ms | 4.4 ms |
| | | 300 progress lines at 100/s: updates / ms in update | 300 / 245 | 13 / 12 |
| | | 50 progress lines at 10/s: updates / ms in update | 50 / 53 | 18 / 17 |
| | | worst UI `page()` latency during the 100/s download | 2.26 ms | 0.53 ms |
| 2 | Batch the library scan | 100,000 WAVs, first scan | 18.5 s | 2.16 s (88% faster) |
| | | commits during the scan | 100,000 | 200 |
| | | 100,000 WAVs, rescan with nothing new | 2.73 s | 1.53 s |
| | | extra RAM during the scan | 88 MB | 14 MB |
| | | 10,000 WAVs, first scan | 1.76 s | 0.17 s |
| 3 | Stream the deep dependency hash | RSS growth, 300 MB tool | 300 MB | 0 MB (hash alone measured: 298 MB -> 0.9 MB, 0.58 s -> 0.23 s) |
| | | result when no per-file hash recorded | `False` (wrong) | `None` (unknown) |
| 4 | Faster reconnect | new helper answers after old exits (p50 / p95) | 109 / 200 ms | 33 / 46 ms |
| | | ... after the old one is killed (p50 / p95) | 84 / 127 ms | 26 / 47 ms |
| | | extension: disconnect -> retried read-only request sent (simulation, 15 runs, median) | 351 ms | 172 ms (109 to 200, jittered) |
| 5 | Pause polling while the dashboard is hidden | polls per minute, hidden tab | 27.3 | 0 |
| | | per hour hidden (derived from the measured 2.7 ms and 39 KB per poll) | 1,636 polls, about 4.5 s helper CPU, 62 MB of messages | 0 |

Rows marked simulation or derived are not direct measurements in Brave. The 351 ms and the polls-per-minute figures are deterministic consequences of the code; the hour totals multiply Phase 1's per-poll measurements.

## 2. No regression check
* Real pipeline, 3-min track, normalization on: wall 5.04 s -> 5.06 s; without normalization 0.40 s -> 0.46 s; final WAV size identical (45.4 MB); all jobs `done`.
* Host start, alternating A/B runs of the baseline commit and the current code (4 each, same session): warm start median **58.0 ms vs 57.9 ms** (no difference). Cold start (first launch after an install only, no `.pyc`): 66.5 ms vs 75.0 ms; the ranges overlap, so a few ms of extra compile time may be real. My earlier one-off run of 54 to 61 ms was machine drift: the baseline commit measured 58 ms in the same session.
* Native-messaging round trips unchanged within noise (`health` 0.03 ms, `jobs` page 2 to 3 ms).

## 3. Tests
Phase 1 handover: 103 Python tests + 9 JS tests. Now: **125 Python tests pass (plus 1 expected failure that records a known gap) and 57 JS tests pass**, with the extension UI (popup, dashboard, content script) now covered by behavioural tests in addition to the service-worker simulation. All were run again from the extracted source zip. New coverage by constraint:

| Hard constraint | Tests added or kept |
|---|---|
| Resume and interruption | kill mid-encode leaves no final WAV or temp file, keeps the cached source, retry replays it without re-downloading (new); `.part` resume, cached-source replay (kept) |
| Atomic WAV completion | existing-file-never-overwritten, validation of truncated or 16-bit output, fsync + hard-link fallback (kept) |
| Cancellation, no orphans | `/proc` scan finds no child process after a cancel (new); cancel under a noisy download is prompt (new) |
| Uninstall defaults, custom music folder | custom folder with spaces and non-ASCII outside the root, and one inside the root: untouched (new); defaults keep music/history/settings (kept) |
| Dependency ownership, Native Messaging cleanup | not-owned tools never removed, conditional registry removal (kept) |
| Deletion boundaries | spaces, non-ASCII, trailing dots, symlink loops, prefix-sharing sibling folder (new); symlink escapes, traversal, tampered manifest (kept). Windows junctions behave like the symlinks tested here but **were not tested on Windows** |
| Concurrency / races | 4 threads mutating the queue: invariants, counts, paging (new); library scan alongside queue traffic (new); lock hand-over (kept) |

Bugs found while testing: none new in product code beyond the deep-hash issue fixed above. **Known gap (not fixed, needs your approval):** the uninstaller removes the whole cache folder, so a music folder placed *inside* the cache would be deleted. 2.3 has no setting for a custom music folder (only the `BMD_OUTPUT` variable), so ordinary users cannot hit it; the expected-failure test `test_KNOWN_GAP_...` flips to passing when it is closed.

## 4. Risks and trade-offs
* Progress granularity: the bar and text now move at most every 0.25 s. The last tick is always stored, including when a download then fails.
* Library scan: the index is now written in batches by the background thread; a crash mid-scan loses at most one batch, and the index is rebuilt at the next start (it is a cache of what is on disk). The `ON CONFLICT ... DO UPDATE ... WHERE` statement needs SQLite 3.24 or newer; the Windows Python 3.12.10 in your install log reports SQLite 3.49.1.
* Reconnect: the shorter extension pause relies on the new helper waiting for the old helper's lock (up to 10 s, now polled every 50 ms). That path is covered by tests but **only a real Brave session shows service-worker behaviour**.
* The hidden-tab poller depends on `document.hidden`; the dashboard shows data up to 2.2 s stale for an instant when you return, then refreshes immediately.
* The deep diagnostics now say "unknown" until the installer records per-file hashes (below).

## 5. Intentionally not done, and why
| Candidate (Phase 1 rank) | Why not yet |
|---|---|
| Replace the `loudnorm` analysis (#1, 85% of conversion time) | changes the normalization method; needs your approval and a corpus check |
| Parallel conversion and download/convert pipeline (#1, #2) | cannot be measured on this 1-core machine; touches cancellation and atomic completion; needs your approval and your multi-core numbers |
| Fewer process spawns per track (#6) | only worth it if Windows spawn cost is high; needs `Measure-Windows.ps1` numbers |
| Skip rescanning an unchanged library (#4, second half) | needs a safe invalidation rule (directory times are not reliable on every Windows setup) and a place to keep it; rescan is now 1.5 s per 100,000 files |
| Auto-prune history (5,000-row cap) | product decision about what the database keeps |
| Record per-file hashes at install | touches the installer's record next to its verification steps; needs your approval |
| Uninstaller guard for a music folder inside the cache | deletion logic; needs your approval |
| Diff-based DOM updates in the dashboard | needs a Chrome performance trace to know whether it matters |

## 6. Manual verification checklist (Windows, Brave, YouTube)
1. Install the new build over your current one (close Brave first). Setup should end with "helper answered: version 2.3.0".
2. Download one real track and watch the progress bar: it should advance smoothly (about 4 steps a second at most) and finish at 100% with the WAV saved.
3. Import a 20-track playlist; cancel one running track and one queued track; confirm no `yt-dlp.exe` or `ffmpeg.exe` remain in Task Manager.
4. Kill the helper (`python.exe` under `app\python`) while the dashboard is open: it should reconnect on its own within about a second, with no duplicate jobs. Kill it mid-download: the job resumes.
5. Leave the dashboard in a background tab for 10 minutes; open the helper log: there should be no stream of requests while hidden; switching back refreshes at once.
6. With a large music folder (thousands of WAVs), restart Brave and open Diagnostics: the helper should answer immediately while the library indexes in the background; queue a track that already exists and confirm it is reported as already saved.
7. Run `bench/Measure-Windows.ps1` and send `bmd-windows-baseline.json`; it also measures the fsync speed that decides how much changes 1 and 2 help on your PC.

## 7. Commits
```
40b5413 Baseline: 2.3.0 source plus Phase 1 benchmark harness (no app changes)
4b13e8a perf: coalesce download progress updates (at most 4 stored per second)
3da9c19 perf: batch the library scan (one transaction per 1,000 files) and keep only ids in memory
b17ae4f fix: stream the deep dependency hash and stop reporting false mismatches
f25bb80 perf: faster reconnect (helper lock polling 200 -> 50 ms, extension retry pause 350 -> 100-200 ms jittered)
acd4aef perf: stop dashboard polling while the tab is hidden; refresh immediately when it is shown again
184e22d test: cover the hard constraints Phase 2 must not break (interruption, orphans, races, boundaries, custom music folders)
```
Build digests of this tree: `Application.zip` `5bdce59811db7fe4d46285dcc9018485ccbb2b726b44e616ef254ae29c0908bd`; extension zip `b9ab4dab54ebf36adb7f82c8f1248d647f45a352e025d97087091072461fb0db`.
