# QA report: Brave Music Downloader 2.5.0

Run on 2026-11-27 on the final tree and the final build. Machine: Linux container (Debian 13, kernel 5.10, 2 CPUs, 3 GB RAM); no Windows PC was available. Tools: Python 3.12.16, Node 24.22.3, Zig 0.14.1, PowerShell 7.7.0 (the Linux build), Wine 10.0, Playwright 1.57.0 with Chromium 143 for Testing.

**Verdict: release candidate.** Every suite that can run without Windows passes on the delivered files. The complete chain on Windows — the setup program, Windows PowerShell 5.1, install, upgrade, downgrade, repair, uninstall, SmartScreen and live downloads in Brave — is **not verified**. The list to work through on a Windows PC is `WINDOWS_QA.md`.

## 1. Build under test

Built with `python scripts/build_release.py --out dist --require-pinned` (Zig 0.14.1, release gate: pinned). A second build into another folder gave byte-identical files (`SHA256SUMS.txt` and `release.json` identical). Nothing is signed.

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `BraveMusicDownloader-Setup.exe` | 224,975 | `6ea041e60c7691b86f88e610a4b2cba1eca06ab5d69ed61bcbcc723c3f57fcf2` |
| `BraveMusicDownloader-Setup-offline.exe` | 197,371,643 | `60eddf01f7c4c429b3a1d657fe64b88aa3e6ed4cce5086c75714b41226a4f840` |
| `Application.zip` (28 files) | 118,951 | `70cc7f3222bc80bfdc42e45bbd459eef5e5ce7fbb50914177d4f39aa708896cf` |
| `Setup.ps1` | 51,434 | `79ed785ca525b32b5916b386fdcf9cf5cb9f01d9625e5e53825c150afe5ee906` |
| `BraveMusicDownloader-extension-2.5.0.zip` (14 files) | 36,443 | `655353cc5de2ee3e65b4a6229f34c4486a4d8b52e10fcea274211269ac27afe8` |

## 2. Automated test run (`test-run.log` in the release folder)

| Suite | Result |
| --- | --- |
| Python, by module name (8 modules, without the browser suite) | Ran 291 tests in 105.389 s: OK (expected failures=1); failures 0, errors 0, skipped 0 |
| Python, by discovery (includes the browser suite) | Ran 306 tests in 121.087 s: OK (expected failures=1); failures 0, errors 0, skipped 0 |
| Browser suite alone (`-p test_browser.py` with `BMD_TEST_BROWSER=1`) | Ran 15 tests in 14.484 s: OK |
| Native C core of the setup program (`tests/native/test_launcher_core.c`) | 670 checks passed, 0 failed (inside both Python runs) |
| PowerShell cases (`tests/ps/Test-Setup.ps1`, PowerShell 7.7.0) | 44 cases, no failure and no skip (one Python test each, inside the Python runs) |
| Setup program under Wine with synthetic installers (`tests/test_launcher_wine.py`) | no failure (inside the Python runs) |
| JavaScript (`node --test`, 5 files) | 72 tests: 72 pass, 0 fail, 0 skipped |
| Extension static gate (`scripts/check_extension.py`) | no findings |
| Launcher reproducible (`build_launcher.py --check-reproducible`) | a second build gave the same bytes |
| Release gates (`--require-pinned`) | passed (exit code 0) |

The one expected failure is `test_KNOWN_GAP_a_music_folder_inside_the_cache_is_not_protected_yet`: the uninstaller removes the whole cache folder, so a music folder placed inside it would be removed too. Closing the gap changes what the uninstaller deletes and needs the owner's decision; the test documents the gap and becomes a normal test when it is closed. No test was disabled or skipped to obtain this run.

## 3. The delivered files under Wine (`release-wine-check.json`)

The two setup programs of section 1 were started under Wine 10.0, where Windows PowerShell does not exist. 15 of 15 checks passed:

| No | Check | Result |
| --- | --- | --- |
| 1 | Self-test of the delivered online installer | exit 0 |
| 2 | Self-test of the delivered offline installer | exit 0 |
| 3 | Extract the offline installer into an empty folder | exit 0, 6 files |
| 4 | Every extracted file equals the hash recorded at build time | 6 files, 197,316,547 bytes |
| 5 | Help (`/?`) | exit 0 |
| 6 | Unknown option | exit 10 |
| 7 | Installer with 10 bytes missing | exit 11 |
| 8 | Installer with 1 byte missing | exit 11 |
| 9 | Self-test with a damaged entry the setup script needs to start | exit 3 |
| 10 | Installation attempt with that damaged entry: nothing is started | exit 3 |
| 11 | Self-test with a damaged tool entry that is unpacked later | exit 3 |
| 12 | Installation attempt with the damaged tool entry reaches Windows PowerShell | exit 12 (PowerShell is absent under Wine) |
| 13 | Intact offline installer where Windows PowerShell is missing | exit 12 |
| 14 | Online installer with a certificate table and the alignment padding a signing tool adds | exit 0 |
| 15 | No work folder is left behind in %TEMP% | none |

What this proves is the setup program's own half: it reads its container, checks every entry before it starts anything, passes options and exit codes on, and refuses damaged, truncated and foreign files. What it cannot prove is the installation itself, because the step after the setup program (Windows PowerShell 5.1 running `Setup.ps1`) does not exist under Wine.

## 4. The pinned Windows tools under Wine

Python 3.12.10, Deno 2.9.8 and FFmpeg 9.1 (the Windows builds pinned in `tools.lock.json`) start under Wine. `yt-dlp.exe` 2026.11.23 does not start there: the PyInstaller bootloader stops with `PYI-220`, `PYI-224` and `PYI-228`. This is a limit of this Wine build, not evidence about Windows; the first download on a Windows PC is therefore item one of the Windows acceptance list.

## 5. Verification matrix: 18 user scenarios

Four layers were executed. **E** is the engine (`app/installer/installer_core.py`, `app/Uninstall.ps1`) driven from the Python tests on Linux. **S** is `Setup.ps1` on PowerShell 7.7.0 with a stand-in engine (the real engine only in `engine-run-real`). **P** is the compiled setup program under Wine, with synthetic installers and a stub in place of Windows PowerShell. **D** is the delivered files of section 1 under Wine. The end-to-end chain on Windows — the exe, Windows PowerShell 5.1, the real engine, the registry, shortcuts and Brave — was executed for **none** of the 18 scenarios; the column says so for every row. 'Not verified' is not a pass.

| No | Scenario | Expected | Executed and observed | Windows end to end |
| --- | --- | --- | --- | --- |
| 1 | Fresh install | Python, yt-dlp, Deno and FFmpeg in place and verified against the pinned hashes; app under `%LOCALAPPDATA%\BraveMusicDownloader\app`; native host registered; Start menu entries; exit 0 | E: `test_fresh_install_records_ownership_and_registers_host`, `test_install_runs_the_helper_check_with_the_new_files_in_place`, `test_real_payload_installs_runs_its_host_and_uninstalls`, `test_failure_on_a_fresh_machine_leaves_nothing_behind` pass. S: `engine-run-ok`, `work-install`, `work-install-shortcuts` pass. D: checks 1 and 13 | Not verified |
| 2 | Silent install (`/S`) | No window and no question; options are passed on; the engine's exit code comes back | E: `test_the_script_is_started_with_the_expected_command_line`, `test_options_are_passed_on_and_the_exit_code_is_returned` (silent mode) pass. P: `test_options` in the C core. S: the silent cases of `entry-uninstall-nothing` and `uninstall-silent` | Not verified |
| 3 | Console (`/CONSOLE`) | Text progress in the console | S: `console-progress` passes. P: `test_console_repair_and_diagnose_reach_the_script` passes | Not verified |
| 4 | PC without Brave | Setup installs the program; the result window offers "Close" and no "Open Brave" | S: `brave-detection` (Find-Brave returns nothing), `window-result` (button "Close", no "Open Brave") and the `work-*` cases with an empty `Browser` pass | Not verified |
| 5 | `/REQUIREBRAVE` | Exit 7, "Brave is not installed", nothing installed | S: `entry-require-brave` passes. P: `-RequireBrave` is passed on to the script (`test_console_repair_and_diagnose_reach_the_script`) | Not verified |
| 6 | Upgrade from 2.3 or 2.4 | App replaced; music, settings and history kept; unchanged tools reused; a failed upgrade restores the previous installation | E: `test_upgrade_replaces_old_app_and_keeps_user_data`, `test_a_2_3_install_with_the_legacy_extension_name_still_upgrades`, `test_smart_update_reuses_unchanged_components_without_downloading`, `test_smart_update_repairs_a_component_whose_hash_changed`, `test_a_failed_upgrade_restores_the_previous_entry`, `test_any_failure_before_commit_restores_the_previous_install_exactly` pass. S: `work-install-update-and-failure` passes | Not verified |
| 7 | Downgrade | Refused with exit 5; the same version is a repair; allowed only with `/ALLOWDOWNGRADE`; an install without a version is not compared | E: `test_an_older_installer_refuses_to_downgrade_and_changes_nothing`, `test_the_same_version_is_a_repair_not_a_downgrade`, `test_downgrade_is_possible_only_when_explicitly_allowed`, `test_a_legacy_install_has_no_version_to_compare` pass. S: `engine-run-categories` (exit 5). P: `/ALLOWDOWNGRADE` is passed on | Not verified |
| 8 | Cancel | Cancel before the swap restores the previous installation; a cancel during the critical phase is ignored; the progress display cannot fail a setup that succeeded | E: `test_cancel_before_the_swap_restores_the_previous_installation`, `test_cancel_while_downloading_stops_at_once_and_leaves_nothing_on_a_fresh_machine`, `test_cancel_requested_during_the_critical_phase_is_ignored`, `test_progress_is_throttled_by_default`, `test_the_engine_reports_phases_components_and_download_bytes`, `test_without_a_progress_file_nothing_is_written` pass. S: `window-close-and-button`, `engine-run-cancel`, `engine-run-display`, `work-logs-outcome` pass | Not verified (closing the real window cannot be executed headlessly) |
| 9 | Offline installer | Needs no network; every bundled file is verified against the pinned hash; a damaged bundle never touches the previous installation | E: `test_an_offline_installer_needs_no_network_for_tools`, `test_each_bundled_file_is_verified_against_the_pinned_hash`, `test_a_damaged_bundle_never_touches_the_previous_installation`, `test_a_verified_copy_already_in_the_cache_is_reused`, `test_offline_installer_carries_the_tools_after_the_bootstraps`, `test_pinned_archives_install_through_the_engine_without_the_network`, `test_bundled_downloads_each_pinned_tool_once_and_keeps_it` pass. P: `test_the_bundle_switch_marks_an_installer_that_keeps_files_for_the_engine`, `test_an_online_installer_passes_no_python_archive`. D: checks 2 to 4 | Not verified |
| 10 | Online installer on a PC without network | Fails as a download (exit 4) after its retries and installs nothing; plain HTTP is refused; an interrupted download resumes | E: `test_an_online_install_without_a_network_fails_as_a_download_and_installs_nothing`, `test_refuses_plain_http_to_the_internet_and_404`, `test_interrupted_download_resumes_with_range` pass. S: `download-refused`, `engine-run-categories` (exit 4). P: `test_a_failed_download_exit_code_and_a_missing_powershell` | Not verified |
| 11 | Damaged or truncated installer | Refused before anything runs: exit 11 (not an installer), exit 3 (a damaged entry) | P: `test_a_damaged_entry_fails_the_self_test_and_nothing_is_started`, `test_a_truncated_installer_is_refused_before_anything_runs`, `test_a_file_without_a_container_is_not_an_installer`, `test_a_signed_installer_with_the_signing_tool_s_padding_passes_and_any_other_trailing_byte_fails` pass. Container: the byte-flip tests of `test_container.py`. E: `test_a_damaged_installer_file_is_category_damaged`, `test_incomplete_payload_is_rejected`. S: `entry-damaged-archive`. D: checks 7 to 12 and 14 | Not verified |
| 12 | Second setup while one is running | The second ends with exit 6 | E: `test_single_instance_lock`, `test_a_second_setup_is_reported_as_busy`, `test_helper_declines_while_a_live_setup_holds_the_lock`, `test_helper_starts_when_the_lock_is_stale_dead_garbage_or_explicitly_allowed`, `test_setup_own_helper_check_is_allowed_while_the_installer_holds_its_lock` pass. S: `exit-code-table`, `engine-run-categories` (exit 6) | Not verified |
| 13 | Uninstall (default) | Removes app, private tools, cache and the native-host registration; keeps music, settings and history; removes only what the install record lists and only registry entries that still point at the app | E: `test_default_uninstall_keeps_music_settings_and_history_and_removes_cache`, `test_registry_cleanup_is_conditional_on_ownership`, `test_files_the_app_did_not_create_survive`, `test_tampered_manifest_cannot_delete_outside_the_app_folder`, `test_uninstall_removes_the_entry` pass. S: `work-uninstall`, `uninstall-exit-codes`, `uninstall-arguments`, `uninstall-prerequisites`, `uninstall-stop-processes-and-leftovers`. P: `/UNINSTALL` is passed on | Not verified |
| 14 | Uninstall when nothing is installed | Exit 9; nothing is deleted | E: `test_nothing_installed_means_nothing_deleted` passes. S: `entry-uninstall-nothing` (exit 9, console and silent) | Not verified |
| 15 | Silent uninstall that also removes the history | `/S /UNINSTALL /REMOVEHISTORY` removes the history; the history alone needs the typed confirmation; music is never touched | E: `test_history_removal_needs_confirmation_and_never_touches_music`, `test_dry_run_changes_nothing` pass. S: `uninstall-silent`, `uninstall-silent-history-and-outcomes`, `uninstall-asks-before-removing-history`. P: `/S /UNINSTALL /REMOVEHISTORY` is passed on | Not verified |
| 16 | Repair (`/REPAIR`) | Installs the same version again over the installation and keeps settings and history | E: `test_the_same_version_is_a_repair_not_a_downgrade` passes. S: `work-install`. P: `test_console_repair_and_diagnose_reach_the_script` (`/REPAIR`) | Not verified |
| 17 | Diagnose (`/DIAGNOSE`) | Writes a diagnosis report and installs nothing; explains a broken setup and passes a working one | E: `test_hostcheck_explains_a_broken_setup_and_passes_a_working_one`, `test_reports_ownership_source_and_missing_tools` pass. S: `work-diagnose`, `engine-run-real`. P: `/DIAGNOSE` reaches the script | Not verified |
| 18 | Unsupported Windows | Windows 10 and 11, 64-bit: yes; Windows 8.1, 7 and 32-bit: exit 8 | S: `supported-windows` and `exit-code-table` (code 8) pass. The check inside the setup program was read, not run | Not verified |

## 6. The extension in a real browser (`tests/test_browser.py`)

Playwright 1.57.0 loads the unpacked extension into Chromium 143 for Testing (a Chromium, not Brave) and opens its pages as tabs. 15 tests pass in 14.5 s:

`test_the_service_worker_runs_under_the_pinned_id`, `test_the_browser_meets_the_minimum_version_of_the_manifest`, `test_the_browser_is_given_the_release_version_and_no_permission_beyond_the_manifest`, `test_the_queue_alarm_is_registered`, `test_the_pages_show_the_installed_version_read_from_the_manifest`, `test_the_popup_fits_its_own_width_in_every_state`, `test_the_popup_reports_the_helper_it_found`, `test_every_dashboard_page_opens_and_fits_desktop_half_screen_and_narrow_windows`, `test_the_overview_describes_the_format_that_was_saved`, `test_changing_the_format_on_the_settings_page_changes_the_overview`, `test_the_overview_says_why_it_is_empty_without_the_helper`, `test_diagnostics_names_the_connection_it_found`, `test_the_page_button_is_added_to_watch_pages_only`, `test_a_click_without_a_helper_is_reported_as_not_queued`, `test_zz_nothing_logged_an_error_a_warning_or_an_exception`.

The suite runs wherever Playwright and its Chromium are installed and is skipped elsewhere; `BMD_TEST_BROWSER=1` turns 'Playwright is not installed' into a failure. It exercises the extension, not the Python helper: the helper is not started by the suite, so a download from the page button to a saved file in a real browser is still open (section 8). `BMD_TEST_CHROMIUM=<executable>` runs the same tests in another Chromium-based browser such as Brave; that was not done here. Fourteen screenshots (popup in three states, five dashboard pages at 1280 px and 375 px, the page button) are in the `screenshots` folder of the release.

## 7. Found and fixed while testing

1. The first builds of this release showed the version label "v2.4" in the popup and the dashboard. The pages now read the version from the manifest, and the release gate refuses a version number written into an HTML page.
2. The dashboard did not fit a narrow window: its smallest width was 763 px. It now wraps below 900 px and fits 640 px and 375 px (checked at 1280 px and 375 px in the browser suite).
3. With no helper ever started, the dashboard said "Check the queue before trying again". It now says "Run the Brave Music Downloader setup again to repair it"; a connection that answered before it dropped still reports an unknown outcome.
4. The Overview described WAV whatever the format that had been saved; it now follows the Audio profile. The WAV cover was described as embedded; for WAV it is saved next to the track as `<track>.cover.jpg`.
5. A failing progress display could fail a setup that had succeeded: the last read of the progress file in `Invoke-EngineRun` is now guarded (PowerShell case `engine-run-display`).
6. Setup paused once more after its last download attempt. Both retry loops of the engine now skip the pause after the last try; on a PC without network the pauses are 1 s and 2 s between the tries, and the former 3 s at the end is gone (`test_an_online_install_without_a_network_fails_as_a_download_and_installs_nothing` asserts the sleep calls).
7. A signed installer was rejected by its own two readers, because a signing tool pads with up to seven zero bytes after the container's footer. Both readers now locate the footer through the PE header; a parity test runs both on the same inputs and the padding case is checked under Wine (check 14).
8. The test environment: a stale copy of the PowerShell stub in the Wine prefix made two install attempts return exit 0 instead of 12, so the delivered-file check passed for the wrong reason. `release_wine_check.py` now refuses to run while a file sits at that path, and `tests/test_launcher_wine.py` backs up and restores a real PowerShell instead of overwriting it.

## 8. Benchmarks re-run on the final tree (`performance-final.json`)

`bench/phase2.py library reconnect deephash` was run again on the final tree. The figures agree with the earlier results in `docs/PERF-PHASE2.md`: the first scan of 100,000 WAV files takes 1.94 s with 200 commits (earlier: 2.16 s, 200), the rescan 1.56 s (earlier 1.53 s), the extra memory 13.3 MB (earlier 14 MB); reconnect after a graceful exit p50 33 ms and p95 52 ms (earlier 33 and 46 ms), after a killed helper p50 34 ms and p95 48 ms (earlier 26 and 47 ms); hashing a 300 MB file takes 98 ms with a memory spike of 0.2 MB. Each reconnect figure rests on 10 runs, so the percentiles are coarse. The `progress` benchmark did not produce numbers this time: its stand-in downloader failed (the error is recorded in the file). Its figures in `docs/PERF-PHASE2.md` are therefore quoted, not re-measured.

## 9. Not executed

- Every scenario of section 5 end to end on Windows: the exe, Windows PowerShell 5.1, the real engine, the registry, shortcuts, the progress window, the uninstaller.
- Windows PowerShell 5.1 itself (the scripts were run on PowerShell 7.7.0 only; they are kept ASCII-only and were read against 5.1).
- SmartScreen and antivirus software (the installers are not code-signed), and signing with a real certificate (only the padding a signing tool inserts was simulated).
- A live YouTube download in Brave, the helper end to end in a browser, the context-menu command, the keyboard command, and the real toolbar popup window (the popup was opened as a tab).
- Brave itself: the browser suite ran in Chromium 143 for Testing.
- `yt-dlp.exe` under Wine (section 4).
