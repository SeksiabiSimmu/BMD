# Release process (2.5.0)

Nothing in the release process publishes anything. Uploading the files, creating a GitHub release or submitting the extension to a store are separate decisions that need the owner's explicit go-ahead; the steps below end with a folder that is ready for them.

## 1. Prepare the tree

1. Set the version in `VERSION`. It must be the same in nine places (the build checks): `app/extension/manifest.json`, `app/helper/helper.py`, `app/installer/installer_core.py`, `Setup.ps1`, `app/installer/launcher/setup_version.h`, `app/installer/launcher/setup.manifest`, `CHANGELOG.md`, `app/README.md`, `VERSION`.
2. Describe the release in `CHANGELOG.md` (top entry), with the real test numbers.
3. Refresh the tool lock once per release, online: `python scripts/refresh_tool_lock.py`. It pins exact tool versions with a SHA-256 that it cross-checks against something the publisher published separately (checksum files, python.org's Sigstore bundle). Review the diff of `tools.lock.json`.
4. Run everything: the Python suite, the JavaScript tests, the extension gate (`BUILDING.md`). With Playwright installed, also run the browser suite (`BMD_TEST_BROWSER=1`), which loads the extension into a real Chromium. All must pass; a test is never disabled to get a green run.
5. Record the real numbers in `docs/QA-REPORT-2.5.0.md`: test counts, the hash of every file, what was not executed. A number that was not read from a run does not go in.

## 2. Build

```
python scripts/build_release.py --out dist --require-pinned
```

Exit code 2 means a gate refused the tree; the message names the file and the reason. The folder then holds `BraveMusicDownloader-Setup.exe`, `BraveMusicDownloader-Setup-offline.exe`, `Application.zip`, `Setup.ps1`, `BraveMusicDownloader-extension-<version>.zip`, `SHA256SUMS.txt` and `release.json`.

## 3. Verify the build

- **Reproducible:** build a second time into another folder (`--out dist-check`) and compare the two `SHA256SUMS.txt` files; they must be identical. `release.json` records the Zig version; a different Zig version can give different bytes.
- **Self-test:** `BraveMusicDownloader-Setup.exe /SELFTEST` on Windows reports `ok` for every file.
- **Install:** work through `WINDOWS_QA.md` on a Windows VM for the build that will be shipped.

## 4. Sign (optional, needs a code-signing certificate)

Signing is the last step because the signature must cover the whole file, container included. The readers locate the container through the PE header's certificate-table entry, so the file stays valid after signing:

```
signtool sign /fd SHA256 /tr <timestamp server URL> /td SHA256 /a BraveMusicDownloader-Setup.exe
signtool verify /pa /all BraveMusicDownloader-Setup.exe
```

Repeat for the offline file, then run `/SELFTEST` on both signed files. `SHA256SUMS.txt` and `release.json` describe the **unsigned** files: regenerate them for the signed ones (or record both) and say in the release notes which is which. The `"signed": false` entry in `release.json` must be updated by hand after signing.

## 5. Hand out

Publish the two setup files and `SHA256SUMS.txt` together. `Application.zip` with `Setup.ps1` is a fallback for people who cannot or do not want to run the exe. `BraveMusicDownloader-extension-<version>.zip` is the extension alone: say in the release notes that it needs the helper, so it is useful only next to a setup file (or unzipped for **Load unpacked**) and is not a second way to install the program. State plainly in the release notes: which Windows builds were tested, what was not tested (see `docs/QA-REPORT-2.5.0.md`), and that the extension is loaded unpacked (no store listing) until the owner decides otherwise.

## 6. After the release

Field reports go into `docs/QA-REPORT.md` (historical record), fixes into the next changelog entry. Tool downloads can disappear or move (GitHub releases, python.org, gyan.dev): when setup reports exit code 4 for a tool, refresh the lock and build a new release.
