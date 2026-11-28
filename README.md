# Brave Music Downloader (BMD)

**One download. One double-click. One step in Brave.** Brave Music Downloader saves audio you own or have permission to save from YouTube as WAV (24-bit PCM), FLAC (24-bit lossless) or MP3 (320 kbps), with tags, BPM and cover artwork embedded in the file, and a simple dashboard with an "Advanced options" toggle.

## Install (Windows 10/11, 64-bit)

1. **Download one file:** [`BraveMusicDownloader-Setup.exe`](https://github.com/SeksiabiSimmu/BMD/releases/latest) from the latest release (~226 KB). The installer downloads Python, yt-dlp, Deno and FFmpeg by itself and checks every one against a pinned SHA-256. No internet on that PC? Download `BraveMusicDownloader-Setup-offline.exe` from the same release — the four tools are inside it.
2. **Double-click it.** Everything installs automatically for your Windows user: no administrator rights, no PATH changes, no browser policies. The installer verifies that the helper answers before it reports success.
3. **One step in Brave** (its security rules do not allow silent extension installs): the installer opens the extension folder and `brave://extensions` for you — turn on **Developer mode**, click **Load unpacked**, pick the folder, done.
4. Open the extension → **Full dashboard → Diagnostics → Run checks.** Files are saved in `Music\Brave Music Downloader`.

Updating is the same one file — settings and history are kept and unchanged tools are reused. Repair, diagnostics and uninstall live in the same installer, the Start menu folder and Windows' Installed apps. Details: `docs/INSTALLATION.md`.

## Status: Windows-verified 2.5.0

Install, repair, diagnostics, uninstall and live downloads in Brave were verified on a real Windows 10 PC — the evidence is in `BUILD_AND_TEST_REPORT.md` and `docs/QA-REPORT-2.5.0.md`. What still needs other environments is listed in `WINDOWS_QA.md`: the power-loss cases, a PC without Brave, Windows 7 / 32-bit, the offline installer on a truly offline PC, and SmartScreen behavior of the unsigned file. `CHANGELOG.md` describes what 2.5.0 contains.

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
