# Improvement backlog — Windows verification pass (2026-11-28)

Mission: bring BMD 2.5.0 closer to production-ready by closing the real-Windows verification gap
(`WINDOWS_QA.md`) and fixing whatever that pass uncovers. Baseline on this machine (Windows 10,
Brave installed, Python 3.12.16, ffmpeg 9.1 pinned build, Zig 0.14.1, Node 22, pwsh 7.7):

| Suite | Baseline result |
| --- | --- |
| Python (`unittest discover -s tests`) | 274 ran: **25 failures, 9 errors**, 6 skipped, 1 expected failure |
| JavaScript (`node --test`, 5 files) | **72/72 pass** |
| Extension static gate (`check_extension.py`) | no findings |
| Launcher build (`build_launcher.py --check-reproducible`) | reproducible on Windows, Zig 0.14.1 |
| Native C core (`test_launcher_native`) | skipped: no `cc`/`gcc`/`clang` on Windows |
| PS harness (`test_setup_ps` via pwsh 7.7) | 5 failures (URL routing + python-home), rest passed |

Every baseline failure was reproduced and root-caused before changing code. They are environment
portability defects in the test harness (the suite had never run on Windows), not product logic
bugs — except where noted.

| ID | Priority | Category | Problem | Proposed solution | Status | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | P1 | tests | `support.make_tools` writes the fake yt-dlp as an extensionless shebang script: invisible to `helper.tool()` on Windows (`yt-dlp.exe`/`shutil.which`), so ~20 pipeline/progress/log tests fail with `MissingTool` | Platform-aware fake: POSIX keeps the shebang script; Windows writes `yt-dlp.py` + a `yt-dlp.bat` shim (CreateProcess runs batch files) and puts the folder on PATH; add `fake_tool_path()` | pending | those tests pass on Windows |
| T2 | P1 | tests | `test_app.py` calls `self.tools / 'yt-dlp'` directly (`subprocess.run`) → WinError 193; `DeepHashTests` fake `ffmpeg` is extensionless so `diagnostics(deep=True)` never reaches the `app` branch | Use `support.fake_tool_path()` everywhere; DeepHash fake named per platform | pending | DeepHash + replay tests pass |
| T3 | P1 | tests | `test_installer.py`: (a) FsSafety uses a folder name with a trailing space — illegal on NTFS; (b) `BusyFolderTests` asserts `stop_processes_under` never runs — true only on POSIX; (c) fake system ffmpeg is extensionless so `shutil.which` misses it; (d) `make_launcher` writes a `/bin/sh` script as the launcher — WinError 216 | (a) drop the trailing space on Windows only; (b) assert platform-correct behavior; (c) write `ffmpeg.exe` on Windows; (d) compile the real `NativeLauncher.cs` with the product's `build_launcher_windows` (csc.exe) and point `host-paths.txt` at the live interpreter | pending | all 4 pass on Windows |
| T4 | P1 | tests | `test_setup_ps.py` builds the served URL as `http://127.0.0.1:P/C:\Users\...` (absolute path pasted into the URL — POSIX-only logic): the server sees an encoded path and 404s. `BMD_TEST_PYTHON=sys.executable` points at a venv overlay whose exe dir has no `pyvenv.cfg`, so child interpreters die with "No pyvenv.cfg" | Serve under a clean route (`/served/python.zip`), pass the server-side file as `BMD_TEST_FILE`; resolve the interpreter to `sys.base_prefix`'s exe when the exe dir is not a real home | pending | download-verified/-refused, uninstall-silent* pass |
| T5 | P1 | tests | `tests/ps/Test-Setup.ps1` `download-verified` derives the server-side file from the URL path (works only with the POSIX layout) | Use `$env:BMD_TEST_FILE` for that comparison | pending | case passes on Windows |
| T6 | P1 | tests | `test_launcher_native.compiler()` only knows `cc`/`gcc`/`clang` — always skipped on Windows, so the 670-check C core never runs there | Add Zig (`zig cc`, same gcc-style flags) to the compiler search; set `ZIG` for the run | pending | 670 checks pass on Windows |
| T7 | P2 | product | `build_release.collect()` reports payload paths with OS separators, so release-gate messages (and tests) differ across platforms | Report `as_posix()` in the gate messages | pending | release gate tests pass unchanged on Windows |
| T8 | P2 | product | `helper.worker_loop`: an exception from `STORE.next_queued()` (e.g. transient DB error) propagates out of the worker thread and kills it silently — the host stays up but never processes another job | Wrap the dequeue in try/except: log, brief sleep, continue | pending | new unit test; existing suite green |
| T9 | P3 | product | `audio_core.py` module docstring has garbled sentences left from an edit (astats/freeverb/loudnorm confusion) | Rewrite the paragraph accurately | pending | text review |
| T10 | P1 | validation | Full Python suite green on Windows | Re-run after T1-T9; record residual skips honestly | done (278 tests, 0 failures, 4 documented skips, 1 expected failure) | `python -m unittest discover -s tests` exit 0 |
| T11 | P1 | validation | JS suite, extension gate, launcher reproducibility (already green in baseline) | Re-run after changes | done (JS 72/72, gate clean, launcher byte-identical) | exit 0 |
| T12 | P1 | validation | Browser suite never ran in Brave itself (`BMD_TEST_CHROMIUM` unused) | Playwright + machine Brave, screenshots captured | done (15/15 in Brave 158, 14 screenshots) | 15/15 in Brave |
| T13 | P1 | release | No release artifacts exist for this tree; offline build needs pinned-lock downloads | `build_release.py --out dist --require-pinned`, second build byte-compare, `/SELFTEST` both installers, `/EXTRACT` parity, damage cases | done | SHA256SUMS match; self-test `ok` |
| T14 | P1 | QA | The WINDOWS_QA list never ran on a real Windows PC — this is the release's known gap | Run the doable-here subset: real install → A2/A3/A5/A6 checks → hostcheck → diagnose → uninstall → F1/F9; plus a helper end-to-end live download (CC-licensed content) exercising yt-dlp 2026.11.23 + Deno + ffmpeg; destructive/VM-only cases marked not-run | done (see BUILD_AND_TEST_REPORT.md; one product bug found: the setup work folder survived — fixed) | per-item PASS/BLOCKED record |
| T15 | P2 | docs | `BUILD_AND_TEST_REPORT.md` (this project's report) and backlog finalization | Write the report with real numbers and hashes only | done | report exists |
| T16 | P2 | UX | Pasting a YouTube URL that carries a `#fragment` (timestamp anchor) is rejected by both `canonicalVideo` (background.js) and `valid_parts` (helper.py) though the video id is unambiguous | Strip the fragment before validation instead of refusing; add tests on both sides | done | new tests; queue flow works |
| T17 | P1 | product | Found by real-Windows QA: the setup work folder `%TEMP%\BMD-Setup-<id>` survives a completed install (the script fills it with engine/python; the launcher only deletes the two bootstrap files, then gives up on the non-empty folder). Violates WINDOWS_QA A1 | `Remove-SetupWork` in `Setup.ps1` empties the script's own work folder at the end; PS harness case `work-cleanup` | done | fresh install leaves no `BMD-Setup-*` in %TEMP% (verified after rebuild) |
| T18 | P2 | UX | Walking the new-user flow found three remaining friction points: the release page offered 7 files with no single recommended download, the README led developers-first with a stale "not Windows-verified" status, and after installing, the user had to navigate to the extension folder by hand | Completion opens the extension folder and brave://extensions automatically (`Open-InstallHelp` + harness case `install-opens-help-windows`); README now leads with the one-file flow and the true status; the GitHub release page is restructured around one recommended download | done | harness case; suite green; rebuilt installers /SELFTEST; PS 5.1 run of the LF script |
| T19 | P0 | product | **Found by the browser E2E suite on a machine with a live 2.0.x-era database**: the migration kept a pre-2.3 3-column `library` table, so every download failed at commit ("table library has 3 columns but 4 values were supplied") and retried to exhaustion. Reproduced in the user's own Install.log (3/3 failed attempts at 03:38) | `Store._ensure_library_schema` replaces an incompatible leftover `library` table on every open; runs both for old databases migrating up and for databases a buggy older release already migrated. Regression test `test_a_pre_2_3_library_table_is_replaced_not_kept` covers both paths | done | test_app 51/51; full suite 294 OK; fix verified against a copy of the real database |
| T20 | P2 | reliability | The helper's worker-loop daemon thread had no stop flag: native-host shutdown and tests could only abandon it, and a surviving test thread claimed jobs from later test classes (sweep-cache flake) | `helper.WORKER_STOP` event checked by `worker_loop`; set on native-host shutdown and in the worker test | done | test_app 51/51 incl. `test_a_store_error_does_not_kill_the_worker` |
| T21 | P2 | diagnostics | Start-Engine discarded the engine's stdout/stderr: a silently failing engine run (exit 0 without writing its report) was undiagnosable | Start-Engine redirects the engine's streams; Invoke-EngineRun logs every engine line and includes the output in a missing-report failure message | done | harness case `manage-status-engine-repair` passes; failure messages now carry engine output |
| T22 | P3 | tests | The sweep-cache test assumed enqueue order == queue order; rapid enqueues sharing a `created` timestamp let the hex id decide the order, making the expectation order-dependent (flake under load) | Mark the running job directly instead of relying on `next_queued()`'s pick | done | test_app 51/51 green |

Explicitly out of scope (needs owner/other machine): code signing, store listing, SmartScreen
behavior with a signed binary, Windows 7/32-bit machines, power-cut (C3) and mid-encode power-off
(D5) cases, a PC without Brave (A9) and without network for the offline installer (A1b) —
recorded as BLOCKED in the report, not skipped silently.

Working rules honored from the master prompt: no destructive git commands, no commits or pushes
without authorization, existing user changes preserved (working tree was clean at start), no test
result invented — every claim traces to a command run in this session.
