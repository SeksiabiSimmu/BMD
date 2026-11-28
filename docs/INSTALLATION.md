# Installation guide (2.6.0)

Brave Music Downloader saves audio you own or have permission to save, as WAV (24-bit PCM), FLAC (24-bit lossless) or MP3 (320 kbps), with tags and BPM. The cover artwork is embedded in FLAC and MP3 files; for WAV it is saved next to the track as `<track>.cover.jpg`. It needs 64-bit Windows 10 or 11 and Brave. It installs for the current Windows user only: no administrator rights, no PATH changes, no browser policies.

## 1. Check the download

The release contains `SHA256SUMS.txt`. In PowerShell, in the folder with the files:

```
Get-FileHash .\BraveMusicDownloader-Setup.exe -Algorithm SHA256
```

The hash must equal the line for that file in `SHA256SUMS.txt`. A file that differs was not downloaded completely or was changed: download it again. `BraveMusicDownloader-Setup.exe /SELFTEST` checks the file's own content without installing anything.

## 2. Which file

| File | Use it when |
| --- | --- |
| `BraveMusicDownloader-Setup.exe` (about 220 KB) | the PC has internet access; Python, yt-dlp, Deno and FFmpeg are downloaded during the installation and checked against the SHA-256 pinned in `tools.lock.json` |
| `BraveMusicDownloader-Setup-offline.exe` (about 200 MB) | the PC is offline or blocks the downloads; the four tools travel inside the file |
| `BraveMusicDownloader-extension-<version>.zip` (about 36 KB) | only the extension is wanted, for example to load it on a second PC that already has the program: unzip it and use **Load unpacked** on the unzipped folder |

Both setup files behave identically otherwise. The extension archive is **not** a second way to install the program: the extension alone shows "Disconnected" and queues nothing, because the helper, Python and the audio tools come from the setup file.

## 3. Install

1. Double-click the setup file. Windows may warn because the file is not code-signed. The **installation manager** opens: it checks the machine and shows every component - Brave, the program files, Python, yt-dlp, Deno, FFmpeg, Native Messaging, the extension's link to the helper - green, yellow, red or grey.
2. Click **Install** and wait for "Installation complete". Everything is built in a staging folder and swapped in at the end; if the installation fails or the PC loses power, the previous version and the registry entries are restored on the next run. Healthy components are reused, so only what is missing is downloaded.
3. The installer opens the extension folder in Explorer and `brave://extensions`. Turn on **Developer mode**, click **Load unpacked** and pick the folder (its path is also on your clipboard: `%LOCALAPPDATA%\BraveMusicDownloader\app\extension`). When upgrading, click the extension's reload arrow instead.
4. Open the extension → **Full dashboard → Diagnostics → Run checks**.

**Repair** diagnoses first, then fixes exactly what is marked (missing or corrupted tools, broken registrations, an interrupted previous setup) - your music, settings and history are kept. **Uninstall** asks once, removes only what the installation record owns, and leaves the manager open. Files are saved in `Music\Brave Music Downloader`; the installation log is `%LOCALAPPDATA%\BraveMusicDownloader\Install.log`.

## 4. Upgrading

Run the newer setup file and click **Install** in the manager (it says an update is available). The installer compares each component with the previous installation (`install-manifest.json`): anything whose pinned hash is unchanged is kept or copied from the old installation, so most updates only replace the application itself (a few MB). Music, settings and download history are never touched. An older installer than the installed version stops with exit code 5 unless you add `/ALLOWDOWNGRADE`.

## 5. Command line

```
BraveMusicDownloader-Setup.exe [options]
```

| Option | Meaning |
| --- | --- |
| *(none)* | open the installation manager: assess the machine, then choose Install, Repair or Uninstall |
| `/MANAGE` | the same, explicitly |
| `/S` (also `/SILENT`, `/VERYSILENT`, `/QUIET`) | install without any window or question |
| `/CONSOLE` | text progress in the console |
| `/UNINSTALL` | remove the program (keeps music, settings and history); `/REMOVEHISTORY` also deletes the history |
| `/REPAIR` | install again over the existing installation |
| `/DIAGNOSE` | write a diagnosis report; installs nothing |
| `/SELFTEST` | check every file packed into the installer; changes nothing |
| `/EXTRACT=<folder>` | unpack the setup script and the application archive; installs nothing |
| `/?` | the help |
| `/LOG=<file>` | write the installation log to this file |
| `/ALLOWDOWNGRADE`, `/REQUIREBRAVE`, `/NOSHORTCUTS`, `/ALLOWUNVERIFIED` | allow an older version than the installed one; stop with exit code 7 when Brave is missing; create no Start menu entries; accept a tool download whose checksum is unknown (not recommended) |

Exit codes: 0 done · 1 failed · 2 cancelled · 3 installer damaged · 4 a download could not be verified · 5 a newer version is installed · 6 setup is already running · 7 Brave is not installed · 8 this Windows is not supported · 9 nothing is installed to remove · 10 unknown option · 11 not a Brave Music Downloader installer · 12 Windows PowerShell is missing.

Examples: `BraveMusicDownloader-Setup.exe /S /LOG=%TEMP%\bmd-setup.log` installs without any window; `BraveMusicDownloader-Setup.exe /UNINSTALL /S` removes the program silently; `BraveMusicDownloader-Setup.exe` opens the manager.

## 6. Uninstall

Any of these removes the program: Settings → Apps → Installed apps → Brave Music Downloader → Uninstall, the Start menu folder *Brave Music Downloader* → **Uninstall Brave Music Downloader**, `BraveMusicDownloader-Setup.exe /UNINSTALL`, or `Uninstall.ps1` from `%LOCALAPPDATA%\BraveMusicDownloader\app`. Then remove the extension in `brave://extensions`.

The uninstaller keeps your music, settings and download history. It removes only what the install record says this app owns, only inside its own folders, and only registry entries that still point at this app. `Uninstall.ps1 -DryRun` lists what would be removed; `-RemoveHistory` (or `/UNINSTALL /REMOVEHISTORY`) also deletes the history database after you type `DELETE HISTORY`. Your music is never deleted by the uninstaller.

## 7. Where things are

| Folder | Content |
| --- | --- |
| `%LOCALAPPDATA%\BraveMusicDownloader\app` | the program: `extension`, `helper`, `installer`, `bridge`, `python`, `tools` |
| `%LOCALAPPDATA%\BraveMusicDownloader\cache` | downloads in progress and cached sources (removable) |
| `%LOCALAPPDATA%\BraveMusicDownloader\Install.log`, `logs\helper.log` | the installation log and the helper's log (also shown in Diagnostics) |
| `%APPDATA%\BraveMusicDownloader` | settings and the download history (`queue.sqlite3`) |
| `Music\Brave Music Downloader` | the saved files |

## 8. If something goes wrong

- **Exit code 3 (installer damaged):** download the file again and compare its hash with `SHA256SUMS.txt`.
- **Exit code 4 (a download could not be verified):** check proxy or antivirus HTTPS scanning, then run setup again.
- **Exit code 6:** another setup is running; wait for it or restart the PC.
- **"Files are in use":** close Brave completely (check the system tray) and run setup again.
- **Dashboard says Disconnected:** run this in PowerShell; every FAIL line says what is wrong:
  `& "$env:LOCALAPPDATA\BraveMusicDownloader\app\python\python.exe" "$env:LOCALAPPDATA\BraveMusicDownloader\app\installer\installer_core.py" hostcheck --root "$env:LOCALAPPDATA\BraveMusicDownloader"`
- **`/DIAGNOSE`** writes a report of every check without changing anything.
