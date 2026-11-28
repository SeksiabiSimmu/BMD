# Changelog

## 2.6.0

The setup program becomes an installation manager: it checks what is on the machine before it offers anything.

### Added
- **Double-clicking the setup file opens an installation manager**, not an immediate installation. It assesses the machine first and shows a component table - the Brave installation, the program files, the installation record, Python, yt-dlp, Deno and FFmpeg (each executed for its version), Native Messaging (registration, manifest, launcher and a live helper round trip), the extension's link to the helper (via the helper's own contact marker), the Start menu entries and the Installed-apps entry - each marked green (working), yellow (needs attention), red (missing or broken) or grey (not applicable). Install, Repair and Uninstall sit under the table; every operation is followed by a fresh assessment, and the manager stays open for repeated operations. Uninstall asks once and keeps music, settings and history; Install is incremental - healthy components are reused and only missing or changed ones are fetched (the engine always did this; the manager now shows it). An older installed version is offered an update; a newer installed version is never downgraded. A legacy pre-2.3 installation is recognized conservatively, and an interrupted previous setup is reported so Repair can finish or roll it back.
- The engine gained a read-only `manage-status` report behind the manager: component states, ownership from the installation record (or conservative recognition when the record is missing), the version comparison against this setup, and the recommendation. 9 new engine tests; 3 new PowerShell-harness cases; 1 new JavaScript test.
- `/MANAGE` opens the manager explicitly. `/S` keeps its documented meaning: install silently. `/UNINSTALL`, `/REPAIR`, `/DIAGNOSE`, `/SELFTEST` and `/EXTRACT` work exactly as before.
- A timestamp-fragment YouTube URL (`watch?v=<id>&t=42s#start`) is accepted and canonicalized instead of refused, on both the extension and the helper side.
- After a successful install, the extension folder in Explorer and `brave://extensions` open automatically, so the one browser step needs no folder navigation.

### Changed
- `(no option)` now opens the manager instead of installing right away. Scripts are unaffected: `/S` installs silently as before.

## 2.5.0

The installer rebuilt as one program, and a release build that refuses to hand out a tree that is not ready.

### Windows verification pass (included in the released 2.5.0 files)
The first verification on a real Windows 10 PC (Brave 158), after the suite itself was made Windows-compatible (it had never run there): the installer work folder now survives nothing - the setup script empties its own `%TEMP%` work folder; the helper's worker thread survives a transient store error instead of dying silently; payload files are pinned to LF line endings (`.gitattributes`) and the release gate refuses CRLF files, so builds are byte-identical across platforms again; after an install the extension folder and brave://extensions open for the user. Evidence and remaining Windows gaps: `BUILD_AND_TEST_REPORT.md`.

### Added
- **One setup file.** `BraveMusicDownloader-Setup.exe` is a single Windows program of under 1 MB: a native launcher (written in C in `app/installer/launcher/`, built with Zig 0.14, importing only `kernel32`, `user32` and `shell32`) followed by the files to install. Double-clicking installs. In 2.4.0 the exe first had to be assembled on a PC with a C# compiler (`Build-Exe.cmd`), or one ran `Install-Dev.cmd`.
- **Offline installer.** `BraveMusicDownloader-Setup-offline.exe` carries Python, yt-dlp, Deno and FFmpeg (about 200 MB) for PCs without internet access. Nothing from it runs before its SHA-256 has been checked.
- **Command line for both files:** `/S` (also `/SILENT`, `/VERYSILENT`, `/QUIET`), `/CONSOLE`, `/UNINSTALL`, `/REPAIR`, `/DIAGNOSE`, `/SELFTEST`, `/EXTRACT=<folder>`, `/?`, `/LOG=<file>`, `/ALLOWDOWNGRADE`, `/REQUIREBRAVE`, `/NOSHORTCUTS`, `/REMOVEHISTORY`, `/ALLOWUNVERIFIED`. Exit codes 0–12 tell scripts and users exactly what happened (3 installer damaged, 7 Brave is not installed, 9 nothing is installed to remove, …). `/SELFTEST` checks a downloaded installer without installing anything.
- **Smart update with the offline file.** On an upgrade the offline installer copies an unchanged tool straight from the old installation instead of unpacking it again; a tool whose pinned hash changed comes from the installer file and is still checked against the pin.
- **Release gates with reasons.** `scripts/build_release.py` checks the tree before it builds: one version in nine places (extension manifest, helper, installer engine, setup script, setup program, its manifest, changelog, README, `VERSION`), the extension ID in three, the permission list, no version number written into an HTML page (the pages read it from the manifest; a literal went stale with every release before this one), no binaries or build debris in the payload, ASCII-only PowerShell for Windows PowerShell 5.1, JavaScript and Python syntax, no stale instructions. It stops with the file and the reason. `--require-pinned` refuses a lock whose tools are not pinned.
- **Reproducible builds.** The same sources, Zig version and lock always give byte-identical installers. `SHA256SUMS.txt` and `release.json` list every file with its hash and say plainly that nothing is signed.
- **The extension as its own archive.** `BraveMusicDownloader-extension-<version>.zip` holds the extension folder alone (`manifest.json` at the root of the archive), built as reproducibly as the installers. It is not a second way to install the program: without the helper that the setup file installs, the extension shows "Disconnected" and queues nothing.
- **The extension in a real browser, in the test run.** `tests/test_browser.py` loads the unpacked extension into Chromium through Playwright where that is installed (skipped elsewhere): the pinned ID, the version and the permissions the browser really grants, popup and dashboard rendered by the browser at 1280, 640 and 375 px, the page button on YouTube-shaped pages, and nothing in the console.

### Changed
- **Tools pinned and cross-checked.** yt-dlp 2026.11.23, Deno 2.9.8, FFmpeg 9.1, Python 3.12.10. Each hash was computed from the real download and compared with something the publisher published separately: the release's `SHA2-256SUMS` (yt-dlp), its `.sha256sum` (Deno), the `.sha256` on gyan.dev (FFmpeg), and python.org's Sigstore bundle, verified against the release manager python.org names for 3.12 (Rekor log index 194017046). Setup now refuses any tool file that does not match the pin, on every PC.
- `scripts/refresh_tool_lock.py` reads GitHub's latest release from the redirect of its `latest/download` link instead of the GitHub API (which allows 60 anonymous requests per hour), and refuses to pin a download it could not cross-check. `scripts/sigstore_verify.py` is new.
- Removed the old C# route: `SetupStub.cs`, `installer_launcher.c`, `kernel32.def`, `user32.def`, and with them `Build-Exe.cmd` and `Install-Dev.cmd`.
- `BUILDING.md` describes the new build (Zig 0.14, one command) and the optional browser suite; `docs/INSTALLATION.md` and `docs/RELEASE.md` are new.

### Fixed before release
- **A signed installer was rejected by its own readers.** A signing tool starts the certificate table on an 8-byte boundary and pads with up to seven zero bytes after the container's footer; both readers took anything after the footer as damage, so signing the finished installer made it unusable. The setup program's C core and `payload_container.py` now find the footer through the PE header's certificate-table entry and accept exactly that zero padding. Any other byte after the footer is still refused with exit code 3. Covered on both readers, in the compiled C core and with the compiled setup program.
- **The dashboard did not fit a narrow window.** Its narrowest layout was 763 px wide whatever the window: in a half-screen options tab on a 1280 px monitor, part of the page lay outside the window. Below 900 px the sidebar now wraps its entries and the pages fit at 640 and 375 px as well, measured in a real Chromium.
- **A stale version label.** The popup and the dashboard showed a version written into the page when it was built (it said v2.4 in this release's first builds, and v2.0 at 2.3). Both now show the version of the installed package, read from the manifest.
- **"Check the queue before trying again" when nothing was installed.** When the browser could not start the helper at all (no host registered, or a registration that names another extension), a click on the page button said the helper "disconnected before confirming this action", so the user was sent to look for a job that could never have been queued. Nothing posted on such a connection can have reached a helper, so the button now says what happened and "Run the Brave Music Downloader setup again to repair it". A connection that answered before it dropped still reports an unknown outcome.
- **The Overview described WAV whatever the format chosen.** Its "Audio profile" panel now follows the saved format (24-bit PCM WAV, 24-bit FLAC, MP3 at 320 kbps) and changes with the dropdown on the settings page.
- **The WAV cover was described wrongly.** The pages and the README said the cover is embedded or saved as `cover.jpg`; the helper writes `<track>.cover.jpg` next to the track. Text corrected in the popup, the dashboard and the README; the helper is unchanged.
- **A failing progress display could fail a setup that had succeeded.** `Invoke-EngineRun` in `Setup.ps1` reads the progress file once more after the engine has exited (everything, when the engine ended before the first poll). A display that failed during that last read threw, and setup reported a failure although the engine's result was a success. That read is now guarded like the ones during the run: the display is switched off, the reason goes to the log, and the result is the engine's. PowerShell case `engine-run-display`.
- **Setup paused once more after its last attempt.** Both download retry loops of the installer engine waited after the final try as well, before saying that the download failed. On a PC without a network the first download is tried three times: pauses of 1 and 2 seconds between the tries, and until now a third, useless one of 3 seconds at the end. A pause now only follows a try that another one comes after.
- The Overview says why "Recent activity" is empty while the helper is disconnected, instead of showing nothing.
- `bmdfs.read_json` is covered by tests for files written by other editors: a byte-order mark, CRLF line ends and non-ASCII text are accepted, and a torn, empty or non-UTF-8 file gives the default instead of an error.

### Tests
- Python: 133 → 306 tests. New since 2.4.0: the 2.5.0 setup flow (downgrade guard, offline bundle, "Programs and Features" entry, cancel channel, result file, diagnosis), the installer container, the compiled launcher core (a native test compiled from the same C file: 670 checks, plus a C-versus-Python parity test), the real setup program under Wine (14 tests: self-test, extraction, damaged, truncated and signed files, the hand-over to PowerShell, every exit code), the 44 PowerShell cases of `tests/ps/Test-Setup.ps1` run through PowerShell 7, the release build (gates with reasons, byte-identical rebuild, the extension archive, read-back of the built installer with the parser the setup program uses), and the extension in a real Chromium (15 tests, Playwright).
- JavaScript: 67 → 72 (the version from the manifest, the audio profile that follows the format, the honest empty state, the "not sent" report of a request to a host the browser never started).
- Not verified here, because no Windows PC was available: the complete installation on Windows with Windows PowerShell 5.1, SmartScreen, the uninstaller, live YouTube downloads in Brave. The list is in `WINDOWS_QA.md`; what each installation case was tested with is in `docs/QA-REPORT-2.5.0.md`.

## 2.4.0

Formats, embedded metadata, a smart installer and a simpler UI — plus a full code, security, performance and permission audit.

### Added
- **Output formats: WAV, FLAC and MP3.** Choose per download from the popup or dashboard; the format travels with the enqueue RPC and is stored in the job. WAV stays 24-bit PCM (lossless, the original product behaviour); FLAC is 24-bit lossless compressed (~50% smaller); MP3 is 320 kbps CBR for compatibility. The file extension and library index follow the chosen format. An unknown format is rejected at the native-host boundary.
- **Embedded metadata.** Title, artist, album, date, genre, source URL and **BPM** are written into the file itself (FLAC VORBIS_COMMENT / MP3 ID3v2 `TBPM`), plus a JSON sidecar. **Cover artwork is embedded inside the file** for FLAC and MP3 (a real attached-picture stream); WAV keeps a `cover.jpg` sidecar because RIFF `LIST/INFO` cannot hold a picture portably.
- **BPM detection.** A pure-Python autocorrelation over the decoded mono envelope (no numpy, no extra tool) runs once per track before the encode and is folded into the 60-200 BPM musical envelope. Best-effort: it never blocks a download.
- **Smart installer.** On update, the installer reads the previous `install-manifest.json` and reuses any component whose pinned SHA-256 still matches and whose files are present — unchanged Python/yt-dlp/FFmpeg/Deno are copied from the old app instead of re-downloaded (~200 MB saved). A changed or missing component is repaired automatically. The manifest records `update.reused` / `update.repaired`, and Setup.ps1 prints "Reused unchanged components: …" / "Repaired/updated components: …".
- **Simpler dashboard and popup.** The default UI shows only the format dropdown and the queue; metadata, normalize, MusicBrainz, artwork, organize, trim and notifications are behind an "Advanced options" toggle that remembers its open/closed state across sessions.

### Fixed (audit findings)
- The right-click menu and keyboard shortcut always enqueued **WAV** regardless of the saved output format (a hard-coded `format:'wav'` in the service worker); they now use the saved format (WAV/FLAC/MP3) and fall back to WAV for an unrecognized stored value.
- The popup's host-version gate used `minor >= 0`, which is always true — it accepted helpers older than the current RPC contract (a 2.2 helper answers `health` but rejects the `format` enqueue parameter). The floor is now 2.3.0, the first helper that accepts `format`.
- The dashboard prefilled the quick-download field from any tab whose URL merely *contained* "youtube.com/" (e.g. `https://example.com/youtube.com/x`); it now parses the hostname and prefills only from real YouTube tabs.
- Dead code: the `wav_command()` back-compat wrapper (its comment claimed callers that no longer existed) and its stale import were removed after reference verification.

### Changed
- **Removed the `tabs` permission.** The extension only reads the active tab's URL/title, and the YouTube host permissions already expose those fields for YouTube tabs; the sensitive `tabs` permission (URL/title access to every site) was unnecessary and is gone. `tabs.query` and the context menu/shortcut behavior are unchanged.
- Format-neutral user-facing copy: the extension name dropped the "(WAV)" suffix; the page button reads "↓ Save"; menus, notifications and the quick-download panel no longer say WAV when the chosen format may be FLAC or MP3.
- Version floors: the popup rejects helpers older than 2.3.0 with a clear "run setup again" message instead of failing later on the first format request.

### Tests
- Python: 125 → 133 (format pipeline for FLAC/MP3, embedded artwork, BPM, unsupported-format rejection, smart-installer reuse/repair, legacy-name upgrade compatibility).
- JS: 57 → 67 (format dropdown persistence and enqueue flow, advanced toggle, saved format restore, the audit fixes above).

### Performance (Phase 2 work, each measured before and after — see `docs/PERF-PHASE2.md`)
- Download progress is stored at most 4 times per second instead of on every yt-dlp output line (the first and last tick and all state changes are always stored): 67 -> 9 database updates per 3-minute track.
- The library scan at helper start writes in batches of 1,000 files in one transaction and no longer rewrites unchanged rows or holds every path in memory: 100,000 files, first scan 18.5 s -> 2.2 s, rescan 2.7 s -> 1.5 s, extra RAM 88 MB -> 14 MB.
- Reconnect is quicker: a starting helper polls the single-instance lock every 50 ms (was 200 ms) and the extension's one read-only retry waits 100-200 ms with jitter (was a fixed 350 ms).
- The dashboard stops polling while its tab is hidden and refreshes at once when shown again; refreshes can no longer overlap.
- The deep dependency check streams the hash instead of loading the whole tool into memory (+300 MB for a 300 MB file before) and reports "unknown" instead of a false mismatch when no per-file hash was recorded.

### Fixed (Phase 2)
- The popup's "Check connection" step compared the helper's version to a hard-coded `'2.0'`, so a freshly installed 2.3.0 helper reported "incompatible" and left the popup permanently "Disconnected" — the same symptom reported from a real Windows 10 PC. The popup now checks the helper's version against a real floor and reports its version; the manifest already pins the allowed extension id, so the host is trusted once it answers.
- A dead fourth argument to the preview `api()` call (a timeout the function signature never accepted) has been removed; the background already applies the 140 s preview timeout.

## 2.3.0

Rebuilt from the 2.0.1 installer source to the 2.3 feature set. The original 2.3 working tree was not available, so this is a re-implementation checked by new automated tests.

### Added
- Setup now ends with a real native-messaging round trip through the compiled launcher (`--check-host`); if the helper does not answer, the install is rolled back with the helper's own error output.
- `installer_core.py hostcheck` explains a "Disconnected" dashboard: registry keys, manifest, allowed extension ID, launcher, host-paths and a live round trip (command in the README).
- The dashboard translates common connection errors (blocked ID, missing registration, timeout, helper exited) into what to do.
- Helper log `%LOCALAPPDATA%\BraveMusicDownloader\logs\helper.log` (rotating): job attempts, retries, errors, startup maintenance; its path is shown in Diagnostics.
- Resumable downloads and replay of the WAV step from a cached source (per-job cache, `yt-dlp --continue`).
- Atomic, validated WAV commit that never overwrites an existing file; stale `.processing-*` temp files are cleaned up.
- Persistent native port with back-off, one read-only retry after a disconnect, and no automatic replay of state-changing requests.
- Host singleton lock waits briefly so a quick reconnect does not collide with the previous host.
- Paged queue history (keyset pagination), server-side state filters, "Load more" in the dashboard.
- SQLite: WAL + `synchronous=FULL`, composite indexes, schema versioning, pre-migration backup, library index.
- Diagnostics: tool source (private/system), ownership, database health, cache size, free space; "Run checks" forces a reconnect.
- Retry policy with permanent vs transient failures; free-space check.
- Transactional installer with journal, rollback, crash recovery, verified/resumable downloads, ownership manifest.
- Uninstaller with ownership checks, dry run, safe deletion boundaries, optional history removal.
- Release builder with consistency/permission/binary gates, deterministic payload and checksums; tool-lock pinning script.

### Changed
- Python runtime is 3.12.10 (last 3.12 release with Windows binaries), verified by its PSF code signature or a pinned hash.
- Cache moved to `%LOCALAPPDATA%\BraveMusicDownloader\cache` (removable); queue database stays in `%APPDATA%\BraveMusicDownloader`.
- Sender check for the native bridge compares scheme and host and accepts the popup and dashboard with a hash or query.

### Fixed
- **Upgrade failed with "Files are in use"** while Brave and the dashboard were open (seen on a real Windows PC): the browser restarts a dropped helper within seconds, and each new helper held the app folder open during the swap. Setup now stops helpers before every rename attempt (up to 40 tries), the helper declines to start while a live setup holds its lock (stale or dead locks are ignored), and the error names the program holding files when Windows can tell us. The failed run had rolled back cleanly.
- **"Disconnected" after a successful setup:** the native launcher copied the browser's requests to the helper with `Stream.CopyTo`, which leaves small writes in the pipe's 4 KB buffer; the browser keeps its end open, so the helper never received the first request. The launcher now forwards and flushes every chunk (both directions). The bug dates from 2.0.1 and could not show up without running the launcher on Windows.
- Setup could stop with `CERTIFICATE_VERIFY_FAILED` while fetching a checksum or download (seen on a real Windows PC, intermittently). The bundled Python cannot fetch an intermediate certificate a server forgot to send, so on a certificate-trust error setup now retries through Windows' own TLS stack (still verified; verification is never disabled). Checksum fetches also retry on transient errors.
- Unexpected setup errors now give a plain message (traceback goes to the log) and the rollback message no longer says "interrupted" for an ordinary failure.
- Checksum files in PowerShell `Get-FileHash` layouts (Deno's Windows `.sha256sum`) are now accepted; unreadable checksum files are reported with their first characters. Found on the first real Windows run.
- A malformed (but complete) native message no longer terminates the helper.
- Removed the unused localhost HTTP server and token file from the helper.
- Removed instructions that referred to scripts that no longer exist.

### Not verified
Windows installer/uninstaller execution, registry behavior, live YouTube extraction, Windows process-tree cancellation, power-loss recovery, SmartScreen/antivirus. See `WINDOWS_QA.md`.
