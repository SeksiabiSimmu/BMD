# Windows acceptance tests for 2.5.0

Everything below is **unverified** until you run it. This build was made and tested on Linux without a Windows PC; what did run is listed in `docs/QA-REPORT-2.5.0.md`. Use a disposable Windows 10/11 x64 VM with Brave installed and a snapshot you can revert. Do the destructive cases (marked ⚠) only in the VM. Record the build under test: `Get-FileHash .\BraveMusicDownloader-Setup.exe` and `.\BraveMusicDownloader-Setup-offline.exe` must equal the values in `SHA256SUMS.txt`.

Result key: PASS / FAIL / BLOCKED. Note the Windows build, Brave version, and (for downloads) the `yt-dlp --version` shown in Diagnostics.

## A. Install
| # | Steps | Expected |
|---|---|---|
| A1 | Double-click `BraveMusicDownloader-Setup.exe` on a clean VM with internet. The **installation manager** opens (a component table, all red/grey); click **Install**. | A window shows the progress and ends with "Installation complete"; no administrator prompt; log at `%LOCALAPPDATA%\BraveMusicDownloader\Install.log`; no `app.staging-*`, `app.old-*`, `.install.lock` or `%TEMP%\BMD-Setup-*` left behind. Re-launching the setup file shows every component green and offers Repair and Uninstall. |
| A1b | Disable the network adapter, double-click `BraveMusicDownloader-Setup-offline.exe`. | Same result; the log shows each tool coming from the installer file and no download. |
| A1c | `BraveMusicDownloader-Setup.exe /S` in a command prompt, then `echo %ERRORLEVEL%`. Repeat with `/CONSOLE`. | `/S`: no window at all, exit code 0. `/CONSOLE`: text progress, exit code 0. |
| A1d | `BraveMusicDownloader-Setup.exe /SELFTEST`, then `/?`. | Self-test lists every file as `ok` and ends with "Self-test passed"; `/?` prints the options; exit code 0 for both; nothing was installed. |
| A2 | Check `%LOCALAPPDATA%\BraveMusicDownloader\install-manifest.json`. | Lists python, yt-dlp, deno, ffmpeg with `owned: true` and `verified_by: pinned`. |
| A3 | `reg query "HKCU\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.bravemusic.downloader"` (and the Chrome key). | Default value = `…\app\bridge\com.bravemusic.downloader.json`; the file lists `chrome-extension://lliokngbogdlnnjffdfgdjabpmjpobab/`. |
| A4 | In Brave: Developer mode → Load unpacked → `…\app\extension`. Check the extension ID. | ID is `lliokngbogdlnnjffdfgdjabpmjpobab`; the permission list shows **no** "Read your browsing history". |
| A4b | Run the hostcheck command from the README (PowerShell). | Every line is `ok` and "All checks passed." Any `FAIL` line names the problem. |
| A5 | Dashboard → Diagnostics → Run checks. | Connected · v2.5.0; yt-dlp, ffmpeg, ffprobe, deno ✓ and "private copy · installed by this app"; database schema 1. |
| A6 | Run the installer again (`/REPAIR`). | Succeeds; manifest updated; music/history untouched; the log says which components were reused (nothing re-downloaded). |
| A7 | SmartScreen / antivirus: note every warning or quarantine for the unsigned installer. | Record only (not a pass/fail). |
| A8 | While A1 is still installing, start `BraveMusicDownloader-Setup.exe /S` a second time. | The second run ends at once with exit code 6; the first run completes normally. |
| A9 | On a VM **without** Brave: run the installer once as it is, once with `/REQUIREBRAVE`. | `/REQUIREBRAVE`: exit code 7 and nothing installed. Plain run: record what setup says and its exit code (it must not crash). |
| A10 | Windows 7 / 32-bit Windows, if available. | Exit code 8 ("This version of Windows is not supported"); nothing installed. |

## B. Upgrade and downgrade
| # | Steps | Expected |
|---|---|---|
| B1 | Install 2.4.0 (or 2.3.0), queue and finish a few jobs, then install 2.5.0. | Old app replaced; history intact; the log says "Reused unchanged components" for the tools; extension reload shows v2.5.0. |
| B1b | Install 2.3.0 (the previous "Brave Music Downloader (WAV)" build), queue a job, then install 2.5.0. | The legacy-named install is **recognized** (setup must not say "not a Brave Music Downloader folder"); history intact. |
| B2 | After B1, install the **older** 2.4.0 package again. | Exit code 5, nothing changed. With `/ALLOWDOWNGRADE` it installs. |
| B3 | Settings → Apps → Installed apps. | "Brave Music Downloader 2.5.0" is listed with its uninstall entry; the Start menu folder has the dashboard and uninstall entries. |

## C. Installer failure and recovery ⚠
| # | Steps | Expected |
|---|---|---|
| C1 | Disconnect the network after a tool download starts, then rerun after reconnecting. | Setup reports failure (exit code 4 or 1); previous install (if any) untouched; rerun reuses verified downloads and succeeds. |
| C2 | During "Install (staged…)", forcibly kill `python.exe` (setup's), then run setup again. | Output says the previous setup was interrupted and rolled back / finished; final state is a clean install. |
| C3 | ⚠ Power-off the VM (not shutdown) during the swap/registration phase; boot and run setup. | Same as C2. |
| C4 | Keep Brave open with the dashboard open and the helper running, run setup (upgrade). | Setup stops helpers right before swapping and completes; if something still holds files it names the program and rolls back with nothing half-installed. |
| C5 | Copy the installer, change one byte in the copy (or cut the file short), run it. | Exit code 3, "The installer file is damaged or incomplete"; nothing is started. `/SELFTEST` names the file that failed. |
| C6 | Replace the copy by any other `.exe`. | Exit code 11, "This file is not a Brave Music Downloader installer." |

## D. Downloads (live YouTube; use content you are allowed to save)
| # | Steps | Expected |
|---|---|---|
| D1 | Queue one track with the format set to **WAV**. | Job completes; WAV at `Music\Brave Music Downloader\Artist\Album\Title [id].wav`; ffprobe shows `pcm_s24le`; no `.processing-*` files. |
| D1b | Queue a track with the format set to **FLAC**. | `.flac` file; ffprobe shows `flac` + embedded tags (`title`, `artist`, `TBPM`) and an **attached picture stream**. |
| D1c | Queue a track with the format set to **MP3** (page button or Alt+Shift+M — not the popup). | `.mp3` file; ffprobe shows `mp3`, 320 kbps, embedded tags + picture stream. |
| D1d | Check the JSON sidecar next to any finished file. | Contains `output_format`, `bpm` (int in 60-200 or null), `musicbrainz_match`, `cover_embedded`. |
| D2 | Queue a playlist (preview, select 5, import). | All queue; progress updates; completed jobs listed; "Load more" appears only past 50 jobs. |
| D3 | Queue the same track again. | "Already saved in your library"; the existing file is not modified. |
| D4 | ⚠ Start a long track; kill `python.exe` (host) mid-download; reopen Brave. | Job returns to queue "Resumed after helper restart" and completes. |
| D5 | ⚠ Power-off the VM mid-encode; boot, open Brave. | Job resumes; the file completes from the cached source; no truncated output exists. |
| D6 | Cancel a running job. | State `canceled`; no `yt-dlp.exe` / `ffmpeg.exe` children left; cache folder for the job removed. |
| D7 | Queue a private/unavailable video. | Fails immediately with a clear message, no retry loop. |
| D8 | Block the network mid-download, restore it. | Job retries (up to 3 attempts) and completes or fails with the network error. |
| D9 | Output folder on a FAT32/exFAT USB drive. | Output still commits without overwriting. |
| D10 | Confirm yt-dlp 2026.11.23 works against current YouTube (with Deno 2.9.8). | Records the yt-dlp version that passed; if it fails, run a newer setup to refresh tools. |

## E. Native Messaging reconnection
| # | Steps | Expected |
|---|---|---|
| E1 | With the dashboard open, end the helper (`python.exe` under `app\python`) in Task Manager. | Dashboard shows brief "Disconnected" then reconnects on its own; no duplicate jobs. |
| E2 | Press "Cancel" at the instant the helper is killed. | Message says the helper disconnected before confirming; the action is not repeated automatically. |
| E3 | Rename the native manifest temporarily; open the dashboard. | Clear "unavailable… repair" message; no reconnect storm; after restoring it, Diagnostics → Run checks reconnects immediately. |

## F. Uninstall ⚠
| # | Steps | Expected |
|---|---|---|
| F1 | `Uninstall.ps1 -DryRun` | Lists actions; nothing changes. |
| F2 | `BraveMusicDownloader-Setup.exe /UNINSTALL` (and once through Settings → Apps). | App folder, tools, cache, both registry keys and the Start menu folder removed; **music, `queue.sqlite3` and settings remain**; Brave shows the extension can no longer reach the helper. |
| F3 | `BraveMusicDownloader-Setup.exe /UNINSTALL /S` | Same, without any window; exit code 0. |
| F4 | `BraveMusicDownloader-Setup.exe /UNINSTALL /REMOVEHISTORY`: answer wrongly, then correctly. | Wrong answer cancels; correct answer deletes only the history database; music remains. |
| F5 | Put your own file in `app\tools\` before F2. | That file and the folder remain; the report says so. |
| F6 | Point the Chrome registry key at another path before F2. | Key left alone and reported. |
| F7 | ⚠ Kill the uninstaller mid-way, run it again. | Completes; ends clean. |
| F8 | Reinstall after F2. | Works and finds the preserved history. |
| F9 | `/UNINSTALL` on a PC where nothing is installed. | Exit code 9; nothing deleted. |

## G. Diagnosis
| # | Steps | Expected |
|---|---|---|
| G1 | `BraveMusicDownloader-Setup.exe /DIAGNOSE` on a healthy installation. | A report is written and shown; every check is `ok`; exit code 0; nothing was changed. |
| G2 | `/DIAGNOSE` after renaming the native manifest (E3). | The failing check names the missing registration. |

## H. Not covered by anything automated
Windows PowerShell 5.1 itself (the tests use PowerShell 7), SmartScreen and antivirus behaviour, the code-signature prompt once the installer is signed, real registry writes as Windows performs them, Start menu shortcuts and the "Installed apps" entry, `taskkill /T`, `os.rename` atomicity on your volume, file locking while Brave runs, the look of the progress window, an offline installer on a PC that is really cut off from the network.
