# Brave Music Downloader — v2.6.0

Download only content you own or have permission to save. Output: **WAV (24-bit PCM), FLAC (24-bit lossless) or MP3 (320 kbps)**, chosen per download. Tags, BPM and cover artwork are embedded in the file (for WAV the cover is saved next to the track as `<track>.cover.jpg`). Optional loudness normalization, conservative MusicBrainz matching, playlist queue and browser actions. A lossless export cannot restore the quality of lossy YouTube audio.

> **Status: Windows-verified 2.6.0.** The helper, the installer engine (now also the installation manager's assessment), the setup program's file container, the extension and the setup script are covered by 290 Python tests (49 of them drive the 49 PowerShell cases; 15 more load the extension into a real Chromium where Playwright is installed), 73 JavaScript tests and 670 checks of the setup program's C core — all green on a real Windows 10 PC (Brave 158), where install, the installation manager, repair, diagnose, uninstall and a live download were verified end to end (`BUILD_AND_TEST_REPORT.md`). What still needs other machines is in `WINDOWS_QA.md`: the power-loss cases, a PC without Brave, Windows 7 / 32-bit, the offline installer on a truly offline PC, and SmartScreen behavior of the unsigned file.

## Install (Windows 10/11, 64-bit)

1. Run **`BraveMusicDownloader-Setup.exe`**. It is a single file: nothing has to be built or unpacked first. Windows may warn because the installer is not code-signed (see *Security and limitations*).
   - On a PC without internet access, or where tool downloads are blocked, use **`BraveMusicDownloader-Setup-offline.exe`** instead: the same setup with Python, yt-dlp, Deno and FFmpeg on board (about 200 MB instead of under 1 MB).
2. The **installation manager** opens: it checks the machine and shows every component - Brave, the program files, Python, yt-dlp, Deno, FFmpeg, Native Messaging (with a live helper round trip), the extension's link to the helper - green, yellow, red or grey. Click **Install**.
3. Setup provides portable Python, yt-dlp, Deno and FFmpeg (downloaded, or copied from the offline file), checks every one against the SHA-256 pinned for this release, and installs for your Windows user only (`%LOCALAPPDATA%\BraveMusicDownloader\app`). No administrator rights, PATH edits or browser policies.
4. The install is **transactional**: it is built in a staging folder and swapped in only when everything checked out. If anything fails (or the PC loses power), the previous version and registry entries are restored automatically on the next run.
5. **Upgrading?** The manager says an update is available. The installer compares each component against the previous install and only replaces what changed — unchanged Python, yt-dlp, FFmpeg and Deno are reused, so most updates download only the app itself (a few MB). Your music, settings and download history are always kept.
6. When setup completes, it opens the extension folder and `brave://extensions`. Turn on **Developer mode**, click **Load unpacked** and pick the folder (its path is also on your clipboard: `…\app\extension`). Upgrading? Click the extension's reload arrow instead.
7. Open the extension → **Full dashboard → Diagnostics → Run checks**. Files are saved in `Music\Brave Music Downloader` (Artist / Album or Singles folders by default).

**Repair** in the same manager diagnoses first and fixes exactly what is marked (missing or corrupted tools, broken registrations, an interrupted previous setup) — your music, settings and history are kept. **Uninstall** asks once, removes only what the installation record owns, and leaves the manager open. The manager re-checks the installation after every operation.

## Command line

`BraveMusicDownloader-Setup.exe [options]` — both setup files understand the same options:

| Option | Meaning |
| --- | --- |
| *(none)* | open the installation manager: assess the machine, then choose Install, Repair or Uninstall |
| `/MANAGE` | the same, explicitly |
| `/S` (also `/SILENT`, `/VERYSILENT`, `/QUIET`) | install without any window or question |
| `/CONSOLE` | text progress in the console |
| `/UNINSTALL` | remove the installed program (keeps music, settings and history) |
| `/REPAIR` | install again over the existing installation (keeps settings and history) |
| `/DIAGNOSE` | write a diagnosis report; installs nothing |
| `/SELFTEST` | check every file packed into the installer; changes nothing |
| `/EXTRACT=<folder>` | unpack the setup script and the application archive; installs nothing |
| `/?` (also `/HELP`) | show the help |
| `/LOG=<file>` | write the installation log to this file |
| `/ALLOWDOWNGRADE` | install although a newer version is already installed |
| `/REQUIREBRAVE` | stop with exit code 7 when Brave is not installed |
| `/NOSHORTCUTS` | do not create the Start menu entries |
| `/REMOVEHISTORY` | with `/UNINSTALL`: also delete the download history |
| `/ALLOWUNVERIFIED` | accept a tool download whose checksum is unknown (not recommended) |

Exit codes: 0 done · 1 failed · 2 cancelled · 3 installer damaged · 4 a download could not be verified · 5 a newer version is installed · 6 setup is already running · 7 Brave is not installed · 8 this Windows is not supported · 9 nothing is installed to remove · 10 unknown option · 11 not a Brave Music Downloader installer · 12 Windows PowerShell is missing.

## Choosing the output format

The popup and the dashboard's **Audio settings** page both have a format dropdown (WAV · 24-bit PCM, FLAC · 24-bit lossless, MP3 · 320 kbps). The choice is remembered and used by every entry point — the page button, the right-click menu, the keyboard shortcut, the popup and the dashboard. Metadata (title, artist, album, BPM, source URL) is embedded in the file itself for all three formats; cover artwork is embedded inside FLAC and MP3 files, and saved for WAV as a `<track>.cover.jpg` file next to the track. The remaining options (metadata embedding, loudness normalization, MusicBrainz matching, artwork, library organization, leading-silence trimming, notifications) are under the **Advanced options** toggle.

## Uninstall

Any of these removes the program: **Settings → Apps → Installed apps → Brave Music Downloader → Uninstall**, the Start menu folder *Brave Music Downloader* → **Uninstall Brave Music Downloader**, `BraveMusicDownloader-Setup.exe /UNINSTALL`, or `Uninstall.ps1` from the installed app folder (`%LOCALAPPDATA%\BraveMusicDownloader\app`). Then remove the extension in `brave://extensions`.

By default it **keeps** your music, settings and download history and **removes** the app, its private tools, the temporary cache and the native-host registration. It only removes what the install record says this app owns, only inside its own folders, and only registry entries that still point at this app. Tools it did not install are never touched.

- `Uninstall.ps1 -DryRun` shows what would be removed and changes nothing.
- `Uninstall.ps1 -RemoveHistory` (or `/UNINSTALL /REMOVEHISTORY`) also deletes the history database after you type `DELETE HISTORY`. Your music is never deleted by the uninstaller.

## Troubleshooting

- Setup log: `%LOCALAPPDATA%\BraveMusicDownloader\Install.log`. Helper log (jobs, retries, errors): `%LOCALAPPDATA%\BraveMusicDownloader\logs\helper.log` (also shown in Diagnostics).
- Setup says **Files are in use**? Close Brave completely (check the system tray), then run setup again. Setup stops the helper right before swapping folders and names the program holding files when it can.
- Dashboard says **Disconnected**? Run this in PowerShell; every FAIL line says what is wrong:
  `& "$env:LOCALAPPDATA\BraveMusicDownloader\app\python\python.exe" "$env:LOCALAPPDATA\BraveMusicDownloader\app\installer\installer_core.py" hostcheck --root "$env:LOCALAPPDATA\BraveMusicDownloader"`
- Setup stops with **exit code 3 (installer damaged)**? The file did not download completely. Download it again and compare its SHA-256 with the value in `SHA256SUMS.txt` published with the release; `BraveMusicDownloader-Setup.exe /SELFTEST` checks a file without installing anything.
- If setup says it could not validate a certificate, it retries through Windows automatically; if downloads still fail, check proxy/antivirus HTTPS scanning and run setup again.

## Security and limitations

- The installer is **not code-signed**; SmartScreen and antivirus behavior are untested.
- Downloads use HTTPS and every tool must match the SHA-256 pinned in `tools.lock.json` for this release (Python 3.12.10, yt-dlp 2026.11.23, Deno 2.9.8, FFmpeg 9.1). Each pinned value was cross-checked against something the publisher published separately: the checksum files of the yt-dlp, Deno and FFmpeg releases, and python.org's Sigstore signature. A pinned hash protects against corruption and against a manipulated download, not against a compromised publisher.
- Only installs for the current Windows user. It does not change policies, disable antivirus, or change PATH.
- YouTube extraction depends on yt-dlp and its challenge solver (Deno); it can break when YouTube changes. Run a newer setup to refresh tools.
- Browser permissions are minimal: no `tabs` permission (the extension reads the active tab's URL/title only for YouTube tabs via the YouTube host permissions), no all-sites access, and the native host only accepts this extension's pinned ID.

## Files

`extension/` Brave extension · `helper/` audio processing (WAV/FLAC/MP3), SQLite queue, native host · `bridge/` native launcher source · `installer/` setup and uninstall engine and the format of the setup file's payload · `tools/` and `python/` are put in place during setup.
