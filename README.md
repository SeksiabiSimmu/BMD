# Brave Music Downloader (BMD)

A Brave browser extension plus a one-file Windows installer. It saves audio you own or have permission to save from YouTube as WAV (24-bit PCM), FLAC (24-bit lossless) or MP3 (320 kbps), with tags, BPM and cover artwork embedded in the file, and a simple dashboard with an "Advanced options" toggle.

**Status: release candidate.** Built and tested on Linux and under Wine 10.0; the complete installation on a real Windows PC (Windows PowerShell 5.1, SmartScreen, the uninstaller, live downloads in Brave) is not verified yet. Work through `WINDOWS_QA.md` before relying on it. `CHANGELOG.md` describes what 2.5.0 contains; `docs/QA-REPORT-2.5.0.md` holds the full verification matrix of 18 user scenarios.

## What is here

| Path | What it is |
| --- | --- |
| `app/extension` | The Brave extension (Manifest V3): page button, context menu, shortcut, popup, dashboard |
| `app/helper` | The helper: downloads, converts to WAV/FLAC/MP3, embeds tags, BPM and artwork; SQLite queue; native host |
| `app/installer` | The installer engine, the container format of the setup file, and the C setup program |
| `app/bridge` | The native-messaging bridge, compiled on the user's PC during setup |
| `scripts` | The release build with its gates, the tool lock, the icon and check scripts |
| `tests` | Python, JavaScript, native C and PowerShell suites, and a suite that loads the extension into a real Chromium |
| `docs` | Installation guide, release process, QA reports |
| `bench` | The benchmark harness and its raw results |

## One step after cloning

This repository was transferred through a text-only channel, so the four binary icon files travel base64-encoded inside `scripts/restore_binaries.py`. Run it once after cloning; it writes the files and verifies the SHA-256 of each:

```
python scripts/restore_binaries.py
```

(`app/installer/launcher/setup.ico` can also be produced from the PNG icons with `scripts/make_setup_icon.py`.)

## Build

```
python scripts/build_release.py --out dist --require-pinned
```

`BUILDING.md` lists the requirements (Python 3.12+, Zig 0.14.1, and optionally Node 20+, ffmpeg/ffprobe and PowerShell 7), the full test commands, and what the build checks before it builds anything. The same sources, Zig version and tool lock always give byte-identical installers.

## The 2.5.0 release package

This repository is the tested source; the release files are built from it with the command above (see `docs/RELEASE.md`) and are not stored here. A finished 2.5.0 package contains:

| File | What it is |
| --- | --- |
| `BraveMusicDownloader-Setup.exe` | The installer, one file of about 220 KB (Python, yt-dlp, Deno and FFmpeg are downloaded and verified during the installation) |
| `BraveMusicDownloader-Setup-offline.exe` | The offline installer, about 200 MB, with the four tools on board |
| `BraveMusicDownloader-extension-2.5.0.zip` | The extension folder alone, to load manually |
| `Application.zip`, `Setup.ps1` | The payload and the setup script, to install without the exe |
| `SHA256SUMS.txt`, `release.json` | The hash of every file, sizes, the Zig version; both say plainly that nothing is signed |
| `release-wine-check.json`, `test-run.log`, `performance-final.json` | The verification logs of the release check |
| `screenshots/` | Screenshots of the extension and the setup program |

## More

- Install and command line: `docs/INSTALLATION.md` and `app/README.md`
- The release process: `docs/RELEASE.md`
- What was tested and how: `docs/QA-REPORT-2.5.0.md`
- What still needs a Windows PC: `WINDOWS_QA.md`

Download only content you own or have permission to save.
