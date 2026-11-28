# Build and test report — Windows verification pass for BMD 2.5.0

Date: 2026-11-28 · Machine: real Windows 10 x64 (10.0.19045), Brave 158.1.100.59, 16 cores.
Tools set up for this pass: GitHub CLI 2.106.0 + Git 2.56.1 (installed), Zig 0.14.1, Node 22.23.5,
PowerShell 7.7.0 (portable), FFmpeg/ffprobe 9.1 (the exact pinned build, SHA-256 verified against
`tools.lock.json`), Playwright 1.65.2. Python 3.12.16 was preinstalled. Branch:
`ai/windows-verification`. Nothing was committed, pushed, signed or published.

## 1. Executive summary

Before: the project was a well-engineered release candidate whose own docs said the real-Windows
half was unverified — and that showed immediately: on a real Windows PC the test suite itself
failed in 34 places (25 failures + 9 errors) because it had never been made Windows-compatible,
and the release's known gap (`WINDOWS_QA.md`: "Everything below is unverified until you run it")
was wide open.

After: every suite is green on Windows (278 Python tests, 670 native C checks, 72 JavaScript
tests, 44 PowerShell cases, 15 browser tests in real Brave), the release artifacts were built
twice byte-identically, and the installer was exercised end to end on this machine — real silent
install with pinned-hash tool downloads, hostcheck, diagnose, repair with component reuse, a real
YouTube download (yt-dlp 2026.11.23 + Deno) through native messaging into a sandboxed library,
and the full uninstall path with exit codes. Two real product bugs were found this way and fixed
(the setup work folder surviving in `%TEMP%`; a worker thread that could die silently), plus one
usability fix and one reliability hardening. The tree is now verified on Windows to the extent
this machine allows; what still needs other machines is listed in §6.

## 2. Changes implemented

### Release engineering (found while preparing the commit)
- **A Windows checkout silently broke release reproducibility.** `.gitattributes` only said
  `* text=auto`, so this Windows working tree held all 91 text files as CRLF, and the first
  Windows build shipped a `Setup.ps1` payload 973 bytes larger than the published hash —
  byte-different from the Linux-built release for the same sources. Fixed three ways:
  `.gitattributes` now pins `eol=lf` (plus explicit `*.png`/`*.ico` binary markers),
  `build_release.py` refuses any CRLF text file in the payload (its own check reads bytes —
  `read_text`'s universal newlines would have hidden exactly this), and the gate has a
  regression test. The shipped LF `Setup.ps1` was then executed under Windows PowerShell 5.1
  (extract → `-Mode Silent -Action Uninstall` on a machine without an installation): correct
  "nothing installed" handling and exit code 9, proving LF payloads are safe on the exact
  interpreter end users have. Final artifacts rebuild byte-identically on Windows.

### Product fixes
- **Setup work folder survived (found by real-Windows QA).** Every completed install left
  `%TEMP%\BMD-Setup-<id>` behind with the unpacked engine and downloaded Python (~15 MB per
  install): the C launcher deletes only the two bootstrap files and its comment assumed the
  folder was otherwise empty, but `Setup.ps1` fills it. Violates `WINDOWS_QA.md` A1. Fixed in
  `Setup.ps1` (`Remove-SetupWork` empties the script's own work folder; the next run also sweeps
  stragglers), harness case `work-cleanup` added; verified on a fresh real install (no leftover).
- **Worker thread could die silently.** In `helper.worker_loop`, an exception from
  `STORE.next_queued()` (e.g. a locked database) propagated out of the worker thread: the host
  stayed up but never processed another job. Now caught, logged, retried after 2 s.
  New test `WorkerLoopTests.test_a_store_error_does_not_kill_the_worker`.
- **Timestamp-fragment URLs were refused.** `https://youtu.be/<id>#t=30` and
  `watch?v=<id>&t=42s#start` failed validation in both `canonicalVideo` (background.js) and
  `valid_parts` (helper.py) although the video id is unambiguous. Fragments are now dropped, not
  refused (the canonical URL is built from the validated id alone). Tests added on the helper
  side; the JS suite (72) stays green.
- `audio_core.py`: replaced a garbled module docstring paragraph (leftover of an edit) with an
  accurate description of the BPM detection. No behavior change.
- `build_release.py`: release-gate messages now use forward slashes, so the gate text is
  identical on every platform (the release tests assert those texts).

### Test infrastructure (Windows portability — the 34 baseline failures)
- `tests/support.py`: fake tools are now platform-aware (`make_launchable_tool`): POSIX keeps the
  shebang script; Windows gets `yt-dlp.py` + a `yt-dlp.bat` shim that `helper.tool()` finds via
  `shutil.which`, and exit codes propagate through cmd.exe. Verified empirically (arg passing,
  quoted paths, `%(...)s` templates, exit codes).
- `tests/test_app.py`: direct `tools/yt-dlp` invocations and the DeepHash/Diagnostics fake tools
  use the platform helpers.
- `tests/test_installer.py`: the fake launcher on Windows now compiles the **real**
  `NativeLauncher.cs` the way setup does (csc.exe) and answers a live native roundtrip; the
  process-helper test asserts real Windows behavior instead of POSIX behavior; the fake system
  ffmpeg is named `ffmpeg.exe`; the fs-safety "weird path" drops only the trailing space (illegal
  on NTFS) and keeps non-ASCII, inner spaces and dots.
- `tests/test_setup_ps.py` + `tests/ps/Test-Setup.ps1`: the served download uses a clean URL
  route plus `BMD_TEST_FILE` (the old code pasted an absolute Windows path into the URL and 404'd
  against its own server); `BMD_TEST_PYTHON` resolves to an interpreter directory that is a real
  home (a venv overlay's is not — children died with "No pyvenv.cfg file").
- `tests/test_launcher_native.py`: the C core suite now also builds with `zig cc`, so the 670
  checks run on machines whose only C compiler is the one the release build uses anyway.
- `.gitignore`: `dist-check/` added (the folder name `docs/RELEASE.md` uses for reproducibility
  builds was not covered).

## 3. Files changed
Modified: `Setup.ps1`, `app/extension/background.js`, `app/helper/helper.py`,
`app/helper/audio_core.py`, `scripts/build_release.py`, `tests/support.py`, `tests/test_app.py`,
`tests/test_installer.py`, `tests/test_launcher_native.py`, `tests/test_setup_ps.py`,
`tests/ps/Test-Setup.ps1`, `.gitignore`. Added: `IMPROVEMENT_BACKLOG.md`,
`BUILD_AND_TEST_REPORT.md` (this file). Generated locally (untracked by design):
`app/extension/icons/*`, `app/installer/launcher/setup.ico` (via `scripts/restore_binaries.py`),
`dist/`, `dist-check/`. 12 tracked files changed, +221/−49 lines.

## 4. Testing results

| Test | Result | Evidence / Notes |
| --- | --- | --- |
| Baseline Python suite (before fixes) | 274 ran: 25 failures + 9 errors | all root-caused; environment portability, not product logic |
| Final Python suite | **Passed — 280 tests, 0 failures** | 105 s; skips: 2 Wine-suite (needs Wine), 2 by-design Windows skips (real window; real Brave present); 1 expected failure = the documented `KNOWN_GAP` uninstall/cache test |
| Native C core (compiled this machine) | **Passed — 670 checks** | first Windows run, via `zig cc`; plus C↔Python container parser parity |
| JavaScript (`node --test`) | **Passed — 73/73** | includes the new timestamp-fragment regression test |
| Extension static gate | **Passed** | no findings |
| Launcher reproducibility | **Passed** | byte-identical twice (Zig 0.14.1) |
| PowerShell harness (Setup.ps1 + Uninstall.ps1) | **Passed — 47 tests** | incl. new `work-cleanup`; 2 by-design skips |
| LF payload gate | **Passed** | CRLF payload refused with a named reason; LF copy passes; regression-tested |
| LF `Setup.ps1` under Windows PowerShell 5.1 | **Passed — exit 9, correct output** | extracted from the shipped installer (52,208 bytes, no CR bytes); full setup path incl. the new work-folder cleanup |
| Release build (`--require-pinned`) | **Passed** | online + offline + extension zip; all four tools fetched and pinned-verified |
| Reproducible release | **Passed** | two `dist`/`dist-check` builds → identical `SHA256SUMS.txt` |
| `/SELFTEST` both installers | **Passed — exit 0** | online 2/2 files, offline 6/6 files ok |
| `/?` and unknown option | **Passed — exit 0 / 10** | matches the documented table |
| Damaged/truncated/foreign files | **Passed** | truncated 10 B and 1 B → exit 11; corrupted entry → `/SELFTEST` FAILED + install refused exit 3; a foreign exe obviously runs itself and cannot report BMD codes (docs nuance, §6) |
| `/EXTRACT` + hash parity | **Passed** | extracted bytes equal `release.json` entries |
| Real install (`/S`, online, real downloads) | **Passed — exit 0** | A2 manifest (pinned, owned), A3 both registry keys, A4 extension + icons, A1 no staging/old/lock |
| Hostcheck (A4b) | **Passed — 8/8 ok** | incl. live native roundtrip through the compiled launcher |
| Diagnose (G1) | **Passed — 12/13 ok** | the one FAIL is the by-design "extension never contacted" (no extension loaded in a live browser during QA) |
| Repair (A6) | **Passed — exit 0** | "Reused unchanged components: python, yt-dlp, deno, ffmpeg" |
| Live download (D1/D3/D10, sandboxed) | **Passed — 11/11 checks** | Big Buck Bunny (CC-BY): yt-dlp 2026.11.23 + Deno vs live YouTube → `pcm_s24le` 48 kHz, 596.5 s, BPM 104, cover sidecar, library layout `Artist/Singles/Title [id].wav`; second enqueue correctly `existing` |
| Uninstall (F1/F2/F3/F9) | **Passed** | dry-run no-op; silent uninstall exit 0; keys/ARP/app/Start-menu removed; music + history kept; second uninstall → exit 9 |
| Work-folder fix on real install | **Passed** | no `%TEMP%\BMD-Setup-*` after install; the pre-fix leftover was swept by the designed stale-folder sweep |
| Extension in real Brave | **Passed — 15/15** | pinned ID, version, permissions, popup/dashboard rendering, page button; 14 screenshots |

Not run (needs other machines/conditions, per `WINDOWS_QA.md`): A1b offline install on a truly
offline PC, A7 SmartScreen/AV behavior with the unsigned file, A9 Brave-less PC, A10 Windows
7/32-bit, B1/B1b upgrade over a real 2.3/2.4 install (fresh-machine scenarios), C1–C4 destructive
mid-install cases (need a disposable VM), D4/D5 kill/power-off mid-download, D8 network loss
mid-download, D9 exFAT output drive, E1–E3 helper-restart with a live dashboard, signing.

## 5. Performance comparison
No new performance claims. Existing measurements (`docs/PERF-PHASE2.md`,
`performance-final.json`) were not re-measured; no code change in this pass touches a measured
path except the worker-loop guard, which only runs on a store error.

## 6. Remaining problems
- **Unsigned installers.** SmartScreen will warn; signing is documented in `docs/RELEASE.md` and
  needs the owner's certificate. The container stays valid after signing (tested: fake-sign
  parity in both readers).
- **`WINDOWS_QA.md` C6 wording.** "Replace the copy by any other .exe → exit 11" overpromises: a
  foreign exe runs as itself and cannot return the installer's exit codes. What holds (and was
  verified): truncated/damaged files containing this launcher are refused with 11/3 before
  anything runs. Suggest rephrasing that row next release.
- **Stale database checkpoints (support note from real-Windows QA).** One machine's data
  directory held a `queue.sqlite3` whose write-ahead log contained newer committed state than
  the main database file: the schema looked older than it was, and the first helper open
  recovered the true committed state (SQLite's normal WAL recovery — the installer's helper
  check is the first open). The specific user data stays in local QA notes; the general
  troubleshooting takeaway is that "history looks older or emptier than expected" can simply be
  a WAL that a killed process left behind, and the state after a clean open is the truth.
- `test_KNOWN_GAP_…` (music folder inside the cache folder is not protected at uninstall) remains
  the one expected failure, awaiting the owner's decision documented in `docs/QA-REPORT-2.5.0.md`.
- B1/B1b upgrade paths were exercised by tests and by design reviews, but not against real 2.3/2.4
  installs on this machine (none present).

## 7. Improvement backlog
`IMPROVEMENT_BACKLOG.md` is updated: T1–T14 and T16/T17 done with verification notes, T15 closed
by this report, and the explicitly out-of-scope list (signing, store listing, VM-only destructive
cases) is unchanged.

## 8. Release deliverables (final, LF-normalized, reproducible, unsigned)
In `dist/` (a second, identical copy exists in `dist-check/`):

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `BraveMusicDownloader-Setup.exe` | 225,860 | `1913597892494dded6ec7f84121f9c9659b7f7fb3bcf9ab5e7607799c7f9ca52` |
| `BraveMusicDownloader-Setup-offline.exe` | 197,372,523 | `c38dcf82028eedaa19148b24e52baec67ac48d85d793b5d9c894f814ab7149a7` |
| `BraveMusicDownloader-extension-2.5.0.zip` | 36,511 (14 files) | `fdb62abbc1163d3898f00082d91f6c319e6bae919b99d01728cc4cb30f4339be` |
| `Application.zip` | 119,068 | see `SHA256SUMS.txt` |
| `Setup.ps1` | 52,208 (LF) | see `SHA256SUMS.txt` |
| `SHA256SUMS.txt`, `release.json` | — | release gate: pinned; `signed: false` |

QA evidence (real-install logs, screenshots) and the machine's tool downloads are kept out of
the repository by policy; only the files listed above belong to the release.

Machine end state after QA: BMD uninstalled (registry keys, app folder, Start menu removed),
user data untouched, no `%TEMP%` leftovers. Nothing was committed or pushed by the QA pass: the
working tree on the dedicated branch holds all changes, ready for review — commit/push only on
the owner's say-so.

## 10. Addendum — one-download experience pass (second cycle, 2026-11-28)

Walking the release as a new user found three remaining friction points; all fixed on branch
`ai/single-exe-installer-release`:

- **The completion step needed folder navigation.** `Setup.ps1` now opens the extension folder
  in Explorer and `brave://extensions` itself (`Open-InstallHelp`; in the GUI the folder opens
  as soon as the completion screen appears, Brave via "Open Brave and close"; in console mode
  both open at the end). The Load-unpacked click remains — Brave's security rules do not allow
  silent extension installs — but the folder to pick is already open. Harness case:
  `install-opens-help-windows`.
- **The README led developers-first** and its status line still said "not verified on a real
  Windows PC". It now leads with the one-file flow (download → double-click → the one step in
  Brave) and the true Windows-verified status; `docs/INSTALLATION.md` step 3 matches the new
  behavior.
- **The release page offered seven files without a single recommended download.** The v2.5.0
  release page was restructured (description only — the seven assets are unchanged) to lead
  with `BraveMusicDownloader-Setup.exe` as the one recommended download and to group the rest
  as special cases (offline PC, fallback install, checksums).

Validation for this cycle: PowerShell harness **48 cases OK** (including the new one), full
Python suite **280 tests OK**, release rebuilt twice byte-identically, `/SELFTEST` clean, and
the extracted LF `Setup.ps1` executed under Windows PowerShell 5.1.
