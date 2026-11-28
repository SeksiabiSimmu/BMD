# Building the 2.5.0 release

Requirements: Python 3.12+ (build, gates and tests), [Zig 0.14.1](https://ziglang.org/download/) to compile the setup program (`ZIG=<path>`, `--zig <path>`, or `zig` on the PATH), and optionally Node 20+ (the JavaScript tests; the release gate also runs `node --check` on the extension's scripts when Node is installed), ffmpeg/ffprobe (the full audio pipeline tests) and PowerShell 7 (`pwsh`, which runs the tests of `Setup.ps1` and `Uninstall.ps1`). Two suites are skipped, never failed, when their tools are missing: `tests/test_launcher_wine.py` needs Zig and Wine (`WINE=<wine binary or wrapper script>`, `WINE_PREFIX=<prefix>`), and `tests/test_browser.py` needs Playwright with a Chromium (`pip install playwright && playwright install chromium`). `BMD_TEST_BROWSER=1` turns "Playwright is not installed" from a skip into a failure, for a machine that is supposed to have it: a requested run must not look like a pass. `BMD_TEST_CHROMIUM=<executable>` runs the browser suite in another Chromium-based browser such as Brave; `BMD_TEST_SHOTS=<folder>` keeps a screenshot of every page it opens.

```
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v   # Python suites: 306 tests (15 are the browser suite)
BMD_TEST_BROWSER=1 PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p test_browser.py -v   # browser suite alone
node --test tests/js/poller.test.js tests/js/background.test.js \
           tests/js/popup.test.js tests/js/dashboard.test.js tests/js/content.test.js   # 72 JS tests
python scripts/check_extension.py app/extension                     # extension static gate
python scripts/build_launcher.py --check-reproducible              # the launcher compiles to the same bytes twice
python scripts/refresh_tool_lock.py                                # online, once per release: pin tool versions + SHA-256
python scripts/build_release.py --out dist --require-pinned
```

The PowerShell cases (`tests/ps/Test-Setup.ps1`, 44 of them) run as part of the Python suite when `pwsh` is on the PATH or `BMD_TEST_PWSH=<executable>` is set: `tests/test_setup_ps.py` turns every case into one test and fails when a case fails or skips for a reason other than "needs another machine". The C core of the setup program has its own tests (`tests/native/test_launcher_core.c`, 670 checks); `tests/test_launcher_native.py` compiles and runs them and compares the C and the Python container parser file by file, so there is no separate command for them. Run the tests with `PYTHONDONTWRITEBYTECODE=1`: the release gate refuses `__pycache__` folders in the payload. The payload must also be LF-only — `.gitattributes` pins `eol=lf`, and the release gate refuses a CRLF file, because a payload checked out with CRLF would not be byte-identical to the published release (a Windows checkout once shipped a Setup.ps1 973 bytes larger than its published hash; Windows PowerShell 5.1 reads LF text correctly). One Wine build crashed at start when `TMPDIR` was set; if the Wine tests report a crashed loader, unset it.

## What the build does

`scripts/build_release.py` first checks the tree and stops with the file and the reason when something is wrong (exit code 2): one version in nine places (extension manifest, helper, installer engine, setup script, setup program, its manifest, changelog, README, `VERSION`), the extension ID in three places, the permission list, no version number written into an HTML page (the pages read it from the manifest), no binaries or build debris in the payload, ASCII-only PowerShell scripts (Windows PowerShell 5.1 reads a script without byte-order mark as ANSI text), Python and JavaScript syntax, no stale instructions, and — with `--require-pinned` — a SHA-256 for every tool in `tools.lock.json`.

It then compiles the setup program (`app/installer/launcher/`, C without a runtime, built by Zig) and appends the files to install in the layout of `app/installer/payload_container.py`:

| File | Content |
| --- | --- |
| `BraveMusicDownloader-Setup.exe` | `Setup.ps1` and `Application.zip`; Python, yt-dlp, Deno and FFmpeg are downloaded and verified during the installation (about 220 KB) |
| `BraveMusicDownloader-Setup-offline.exe` | the same plus the four tool archives on board (about 200 MB); needs a pinned lock and the downloads (kept in `.release-cache/`, fetched once) |
| `Application.zip`, `Setup.ps1` | the payload and a copy of the setup script, to install without the exe: `powershell -ExecutionPolicy Bypass -File Setup.ps1` next to the archive |
| `BraveMusicDownloader-extension-<version>.zip` | the extension folder alone, `manifest.json` at the root of the archive; it works only together with the helper that the setup program installs |
| `SHA256SUMS.txt`, `release.json` | the hash of every file, sizes, the Zig version, and the plain statement that nothing is signed |

The build reads every installer it writes back with the parser the setup program uses and refuses to hand out a file that does not pass. The same sources, Zig version and lock always give byte-identical files: build twice into two folders and compare `SHA256SUMS.txt` to check. Exit codes: 0 built, 1 the build failed, 2 a gate refused the tree.

`--online-only` skips the offline installer (no downloads). Nothing is signed here; signing is the last step and is described in `docs/RELEASE.md`.

## Not part of the build any more

The C# route of earlier versions (`SetupStub.cs`, `installer_launcher.c`, `Build-Exe.cmd`, `Install-Dev.cmd`, a C# compiler on a Windows PC) was removed in 2.5.0. `app/bridge/NativeLauncher.cs` is different: it is the native-messaging bridge, shipped as source and compiled on the user's PC during the installation.
