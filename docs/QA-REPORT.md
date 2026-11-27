# QA report: Brave Music Downloader 2.4.0 (development build)

> This report describes the tree **before** the Phase 2 performance changes. The current tree's results and digests are in `PERF-PHASE2.md`.

Date: 2026-11-20 · Environment: Linux container, offline · Python 3.12.3 · Node v22.23.5 · ffmpeg version 6.1.1-3ubuntu5
Not available here: Windows, PowerShell, Brave/Chromium, internet, a Windows C toolchain.

## Build under test (2.4.0)
| Artifact | SHA-256 |
|---|---|
| dist/Application.zip (deterministic payload, tools not fully pinned) | `c7ab00fd16e8be2130a4616c1eb5b091c1579ea0edaf15e86c1bd797eece146e` |
| dist/Setup.ps1 | `180e4f0642a089ccd410a3aede65f2bbf0aaac21879eedbbf44320bbd7bc93ff` |
| dist/Install-Dev.cmd | `15c26f828fff8e8a0edd00e2c288d0f97c15466dedcd7fb1fb9ea84b5100b63f` |
| dist/Build-Exe.cmd | `88d17040d18c1c247f05c967f27f8e7e0b5e2c88e4b8ab4d7b73310a4d5dfdbf` |
| dist/SetupStub.cs | `c554941e4bffd8ba82d972dc179ac0ab005e0ba211bc5e6562b52da611764e71` |
| Extension-only ZIP (2.4.0) | `ffd4b7e81377c31842d389f9d85b6be588045266aec1e9b736c997e195d437b6` |
| BraveMusicDownloader-Setup.exe | **not built here**: no Windows compiler. `Build-Exe.cmd` builds it on Windows with the built-in `csc.exe`; neither it nor `SetupStub.cs` has been compiled or run |

(The digests above replaced the 2.3.0-era table; two independent builds of the 2.4.0 tree produce the identical `Application.zip`.)

Source note: the original 2.3.0 working tree was unavailable. This release is a re-implementation of the 2.3 feature set on top of the uploaded 2.0.1 installer source. The earlier test report's counts (39 + 38) are not comparable; the suites below are new.

## What ran (all passed)
Command: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_app test_installer test_release` in `tests/` → **133 tests, OK** (1 expected failure documenting a known gap); `node --test` over all five JS suites → **67 tests, OK**; `check_extension.py app/extension` → no findings (static heuristics only).

> Updated for 2.4.0 (formats, embedded metadata, smart installer, audit): the Python suite is now **133 tests** (46 `test_app`, 66 `test_installer`, 21 `test_release`) and the JS suite is now **67 tests** (12 `background`, 23 `popup`, 16 `dashboard`, 11 `content`, 5 `poller`). The 2.4.0 audit fixes (saved-format enqueue, host version floor, legacy extension-name upgrade compatibility) each carry a regression test.

| Suite | Tests | Covers |
|---|---|---|
| test_app | 46 | URL canonicalization; SQLite WAL + `synchronous=FULL`, index plans, migration of a 2.0-style database with backup, keyset pagination (complete, ordered, filtered, invalid cursor), crash requeue; real FFmpeg/ffprobe pipeline with a fake yt-dlp: 24-bit WAV + tags + sidecar, **FLAC and MP3 exports with embedded tags, embedded cover art (FLAC picture stream, WAV sidecar), BPM detection, unsupported-format rejection**, resume with `--continue`/`.part`, replay from cached source after a failed encode and after a helper restart, permanent vs transient errors, missing tool, low disk, cancel kills the process and cleans the cache, existing file never overwritten, truncated/16-bit WAV rejected; cache janitor boundaries; library reconcile (wav/flac/mp3), stale temp cleanup, symlinks not followed; native host framing (bad JSON, oversized frame, bad ids), singleton lock wait/give-up; diagnostics ownership/source |
| test_installer | 66 | safe deletion (boundary, symlinks, traversal), atomic no-overwrite commit incl. hard-link fallback, atomic JSON; downloads (hash, resume with Range, truncated connection, 404, non-HTTPS, checksum-file parsing, zip-slip/links); fresh install, upgrade, **a 2.3 install with the legacy "Brave Music Downloader (WAV)" extension name still upgrades**, **smart update: unchanged components are reused without re-downloading; changed/missing components are repaired**, failure at every stage restores the previous install and registry exactly, simulated power loss at every stage recovers on next run, roll-forward after the commit point, bad checksum, foreign folder, wrong extension identity, incomplete payload, single-instance lock; uninstall defaults (keeps music/history/settings, removes cache), conditional registry removal, the native round trip (a real host answers; a host that only reads input after stdin closes, a silent, crashing, noisy or wrong-version host are all rejected with reasons), rollback when the helper cannot answer, `hostcheck`, upgrade while a helper keeps respawning (stop-before-retry, abort naming the locker, helper declines during setup), user files survive, tampered manifest cannot delete outside, non-owned dependencies kept, history removal needs confirmation, dry run, rerun after interruption, legacy install, nothing-installed, registry key format regression |
| test_release | 21 | deterministic payload, no binaries/debris, checksums, generated launcher embeds the exact payload; the Windows exe builder files are consistent (resource names, BOM, no elevation); PowerShell scripts are ASCII; the **real payload installs, its native host runs as a subprocess answering framed JSON on a clean stdout, and uninstall leaves history**; build gates (debris, binaries, version drift, permission creep, extension-ID mismatch, stale text, unverifiable/insecure tool sources, `--require-pinned`, JS syntax); tool-lock refresh logic against stubbed servers |
| background.test.js | 12 | sender authorization, routing, read-only retry once, no replay of enqueue/cancel/retry/clear, back-off, reset on healthy connection and on `reconnect`, badge refresh, input validation, **the saved output format travels with every enqueue (with WAV fallback for unrecognized values)** |

Bugs found and fixed by these tests (and later by the field reports) during the work: duplicated backslash in the registry key paths; `URL.origin` is `"null"` for extension URLs (origin check compared null to null); HTTP client reports a dropped connection as a normal end (resume now detects short reads); malformed JSON used to terminate the host.

## Field reports from a real Windows 10 PC (user-reported logs, not reproduced by me)
Windows 10 build 19045, Windows PowerShell 5.1. Five setup runs on 2026-11-20/21:
1. Deno's Windows `.sha256sum` was not in `sha256sum` layout → setup stopped and rolled back. Fixed (parser accepts all common layouts, tested).
2. and 4. `CERTIFICATE_VERIFY_FAILED` (bundled Python could not validate a certificate chain) while fetching a checksum; runs 3 and 5 passed the same step, so it is intermittent. Fixed in this build: fall back to Windows' own TLS stack on certificate-trust errors (verification never disabled), retry checksum fetches, plain error text.
3. Runs 3–5 were started from `…\Temp\BMD-Setup-*\Setup.ps1`, i.e. the exe built by `Build-Exe.cmd`/`SetupStub.cs` worked.
5. **Completed:** Python PSF signature check passed; python, yt-dlp, deno, ffmpeg installed and verified; startup checks passed (SQLite 3.49.1, helper imports as 2.3.0, yt-dlp 2026.10.26, Deno 2.9.8, FFmpeg/ffprobe 9.1); native launcher built; native host registered; "Installation complete".
Each run rolled back cleanly on failure.
6. **After the completed setup, the dashboard showed "Disconnected".** Code review found the cause candidate: the launcher forwarded requests with `Stream.CopyTo`, which buffers small writes until the stream closes (the browser keeps it open). Present since 2.0.1. Fixed with explicit flushes, guarded by a static test, and setup now ends with a live round trip through the compiled launcher that rolls the install back if the helper does not answer. **Not yet confirmed on the user's PC** that this was the only cause; the C# launcher itself cannot be compiled here.
7. Run 7 (the build with the launcher fix) got through unpack, downloads, startup checks and the launcher compile, then stopped at the folder swap with "Files are in use" and **rolled back cleanly**; the browser kept restarting the helper while setup ran. Fixed in this build (see CHANGELOG); the Windows process-stop and Restart Manager calls are untested here.

## NOT verified (named, not assumed)
- **The fixed launcher on Windows** (C# not compiled here); the dashboard connecting to the helper in Brave.
- **`Uninstall.ps1` and the whole uninstall path on Windows** have never been run.
- The registry values were not inspected after setup (setup reported success).
- **Live YouTube extraction** with yt-dlp + Deno, real downloads, playlist import, cancel (`taskkill /T`), resume after killing the helper, hard power loss, FAT/exFAT output folders.
- The extension UI in a real browser (popup, dashboard pagination, Load more, diagnostics view, notifications). The service worker is simulated and `dashboard.js`/`popup.js`/`content.js` are now exercised by 67 behavioural Node tests (host-version gate incl. the 2.3.0 floor, URL validation, paging/merge, filters, diagnostics rendering, button injection, SPA navigation, the `BMD_ENQUEUE_CURRENT` contract, the format dropdown and advanced toggle); `check_extension.py` statically verifies the manifest and that every referenced file exists. A real Brave session is still the only way to see service-worker lifecycle, notifications and the rendered DOM.
- Upgrade from a 2.0.x install on Windows; SmartScreen/antivirus behavior; the Windows TLS fallback itself (tested with the PowerShell call mocked; the real call has not run, because the failing step has not recurred since it was added).
- Whether the tool URLs and asset names in the lock still resolve over time; `refresh_tool_lock.py` is tested only against stubs.
- The `tabs` permission removal in a real browser: `tabs.query`, the context menus and the keyboard shortcut are covered by the Node harnesses with the `tabs` permission absent, but only a live Brave session can confirm the active YouTube tab's url/title are still readable through the YouTube host permissions on every Brave build.

## Decisions to review
- Tools are verified by publisher checksum (and Python by its PSF signature) until you run `scripts/refresh_tool_lock.py` and build with `--require-pinned`.
- The `tabs` permission was **removed** in 2.4.0: the extension only reads the active tab's url/title, and the YouTube host permissions already expose those fields for YouTube tabs (all code paths were re-tested with the permission absent). If a Brave build ever hides them, the popup prefill and the shortcut's "open a YouTube track first" hint degrade gracefully — no functionality is lost.
- Python runtime changed from 3.13 to 3.12.10.
- Verified downloads are kept after a failed install so a retry is fast; they live in the removable cache.

All acceptance cases for the unverified list are in `WINDOWS_QA.md`.
