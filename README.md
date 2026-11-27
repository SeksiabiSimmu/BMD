[README.md](https://github.com/user-attachments/files/35513266/README.md)
# Brave Music Downloader 2.5.0 — what is in this folder

Start with `BraveMusicDownloader-2.5.0-STATUS.md` (one screen) or with the report (everything, ten sections).

| File | What it is |
| --- | --- |
| `BraveMusicDownloader-2.5.0-STATUS.md` | Short status: rating, files with hashes, what is verified, what is not, decisions for the owner |
| `BraveMusicDownloader-2.5.0-Report.docx`, `BraveMusicDownloader-2.5.0-Report.pdf` | Final report: executive summary, architecture audit, code cleanup, performance, security, test report with the 18-scenario matrix, compatibility, release information, remaining issues, release-readiness rating |
| `BraveMusicDownloader-2.5.0/` | The release files: `BraveMusicDownloader-Setup.exe` (online installer), `BraveMusicDownloader-Setup-offline.exe` (about 200 MB, with all tools), `BraveMusicDownloader-extension-2.5.0.zip`, `Application.zip` and `Setup.ps1` (fallback without the exe), `SHA256SUMS.txt`, `release.json`, `release-wine-check.json`, `test-run.log`, `performance-final.json`, `screenshots/` |
| `BraveMusicDownloader-2.5.0-source.zip` | The complete source tree as tested, top folder `BraveMusicDownloader-2.5.0` |

**State: release candidate, not yet run on Windows.** Everything that can run without Windows was run and passed; the complete installation on a Windows PC (Windows PowerShell 5.1, SmartScreen, uninstaller, registry, live downloads in Brave) is unverified. The acceptance list is `WINDOWS_QA.md` in the source zip; the 18-scenario matrix is `docs/QA-REPORT-2.5.0.md`.
