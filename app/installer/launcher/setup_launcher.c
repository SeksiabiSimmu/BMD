/* setup_launcher.c - the Windows half of the Brave Music Downloader setup program.
 *
 * Zig builds this as a freestanding 64-bit program: no C runtime, only this file, launcher_core.c,
 * the icon and version resource and a manifest that never asks for administrator rights. The files
 * to install travel behind the executable image in the container that launcher_core.c reads, so
 * one .exe is the whole installer.
 *
 * What it does: read the command line, open its own file, check the container, unpack the setup
 * script, the application archive and (offline build) the Python archive into a private temporary
 * folder while verifying each one's SHA-256, start Windows PowerShell on the script, wait, remove
 * the folder and return the script's exit code (one outside the table below is reported as a
 * failed setup). A file that fails its checksum is deleted before
 * anything runs, so nothing unverified is ever executed.
 *
 * The launcher draws no window of its own. The script shows the progress window (default), prints
 * text (console mode) or stays quiet (silent mode); the launcher only speaks when it fails before
 * the script could start, and for /?, /SELFTEST and /EXTRACT.
 *
 * There is no heap here: every buffer is a static that the linker places in .bss, which Windows
 * clears before the program starts.
 */
#include <windows.h>
#include <shellapi.h>
#include "launcher_core.h"
#include "setup_version.h"

/* Exit codes of Setup.exe. Setup.ps1 maps its own outcome onto the same table (Get-ExitCode), so
 * a caller sees one code whichever half failed. Documented in app/README.md. */
enum {
    EXIT_OK = 0, EXIT_FAILED = 1, EXIT_CANCELLED = 2, EXIT_DAMAGED = 3, EXIT_DOWNLOAD = 4,
    EXIT_DOWNGRADE = 5, EXIT_BUSY = 6, EXIT_BRAVE_MISSING = 7, EXIT_UNSUPPORTED = 8,
    EXIT_NOT_INSTALLED = 9, EXIT_USAGE = 10, EXIT_NOT_INSTALLER = 11, EXIT_NO_POWERSHELL = 12
};

#define PATH_CAP 1024
#define COMMAND_CAP 8192
#define TEXT_CAP 8192
#define LINE_CAP (PATH_CAP + 128)

static lc_container container;
static lc_options options;
static STARTUPINFOW startup;
static PROCESS_INFORMATION process;
static wchar_t self_path[PATH_CAP], work_dir[PATH_CAP], command[COMMAND_CAP];
static wchar_t text[TEXT_CAP], line[LINE_CAP], title[96], powershell[PATH_CAP];
static int console_ui, redirected, no_box, overflow;
static HANDLE console_handle, output_handle;

static void zero(void *memory, size_t len) {
    unsigned char *p = (unsigned char *)memory;
    while (len--) *p++ = 0;
}

/* ---- building text, one piece at a time ---------------------------------------------- */

static void add(wchar_t *out, size_t cap, const wchar_t *part) {
    size_t length = lc_wlen(out);
    if (lc_append_text(out, cap, &length, part)) overflow = 1;
}

static void add_quoted(wchar_t *out, size_t cap, const wchar_t *part) {
    size_t length = lc_wlen(out);
    if (lc_append_quoted(out, cap, &length, part)) overflow = 1;
}

static void add_number(wchar_t *out, size_t cap, uint64_t value) {
    wchar_t digits[24], one[2];
    size_t n = 0;
    one[1] = 0;
    do {
        digits[n++] = (wchar_t)(L'0' + value % 10);
        value /= 10;
    } while (value);
    while (n) {
        one[0] = digits[--n];
        add(out, cap, one);
    }
}

/* Entry names are ASCII (launcher_core.c checks every character), so widening is a copy.
 * Returns the number of characters written, not counting the terminator. */
static size_t to_wide(const char *name, wchar_t *out) {
    size_t i;
    for (i = 0; name[i]; i++) out[i] = (wchar_t)(unsigned char)name[i];
    out[i] = 0;
    return i;
}

/* `folder`\`name` */
static void path_join(wchar_t *out, size_t cap, const wchar_t *folder, const wchar_t *name) {
    out[0] = 0;
    add(out, cap, folder);
    add(out, cap, L"\\");
    add(out, cap, name);
}

/* ---- where text goes ----------------------------------------------------------------- */

/* A window-subsystem program has no console of its own. Output redirected to a file or pipe
 * always wins (it is what /SILENT may write to); otherwise /?, /SELFTEST and /EXTRACT borrow the
 * console of the program that started them. Started from Explorer there is neither, and text
 * becomes a message box. */
static void open_output(void) {
    DWORD mode;
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD type = (out && out != INVALID_HANDLE_VALUE) ? GetFileType(out) : FILE_TYPE_UNKNOWN;
    if (type == FILE_TYPE_DISK || type == FILE_TYPE_PIPE || (type == FILE_TYPE_CHAR && !GetConsoleMode(out, &mode))) {
        redirected = 1;
        output_handle = out;
        return;
    }
    if (!AttachConsole(ATTACH_PARENT_PROCESS)) return;
    console_handle = CreateFileW(L"CONOUT$", GENERIC_WRITE | GENERIC_READ, FILE_SHARE_WRITE | FILE_SHARE_READ,
                                 NULL, OPEN_EXISTING, 0, NULL);
    console_ui = console_handle != INVALID_HANDLE_VALUE;
}

static void print_line(const wchar_t *message, size_t n) {
    static char utf8[TEXT_CAP * 3];
    DWORD written = 0;
    int size;
    if (console_ui) {
        WriteConsoleW(console_handle, message, (DWORD)n, &written, NULL);
        WriteConsoleW(console_handle, L"\r\n", 2, &written, NULL);
    } else if (redirected) {
        size = WideCharToMultiByte(CP_UTF8, 0, message, (int)n, utf8, (int)sizeof utf8 - 2, NULL, NULL);
        if (size > 0) {
            utf8[size] = '\r';
            utf8[size + 1] = '\n';
            WriteFile(output_handle, utf8, (DWORD)size + 2, &written, NULL);
        }
    }
}

/* Say `message` (lines separated by \n). `kind` is an MB_ICON_* value, or 0 to never show a box.
 * Silent mode never shows a box: its result is the exit code and the log. */
static void show(const wchar_t *message, UINT kind) {
    size_t start = 0, k, n = lc_wlen(message);
    if (console_ui || redirected) {
        for (k = 0; k <= n; k++) {
            if (k == n || message[k] == L'\n') {
                size_t end = k;                        /* the line ends here; a \r before the \n is dropped */
                if (end > start && message[end - 1] == L'\r') end--;
                if (end > start) print_line(message + start, end - start);
                start = k + 1;
            }
        }
    } else if (kind && !no_box) {
        MessageBoxW(NULL, message, title, kind | MB_SETFOREGROUND);
    }
}

/* The text prepared in `text` is reported and the program ends with `code`. */
static _Noreturn void fail(int code) {
    show(text, MB_ICONERROR);
    ExitProcess(code);
}

static _Noreturn void fail_with_error(int code, const wchar_t *what) {
    DWORD error = GetLastError();
    text[0] = 0;
    add(text, TEXT_CAP, what);
    add(text, TEXT_CAP, L" (Windows error ");
    add_number(text, TEXT_CAP, error);
    add(text, TEXT_CAP, L").");
    fail(code);
}

/* The command line help, with `error_line` (or NULL) in front of it. */
static void usage(const wchar_t *error_line) {
    size_t k, last = 0;
    text[0] = 0;
    if (error_line) {
        add(text, TEXT_CAP, error_line);
        add(text, TEXT_CAP, L"\n\n");
    }
    add(text, TEXT_CAP, title);
    add(text, TEXT_CAP, L"\n\nInstalls the Brave Music Downloader extension, its helper program and the tools it needs.\n\n");
    for (k = 0; self_path[k]; k++)
        if (self_path[k] == L'\\' || self_path[k] == L'/') last = k + 1;
    add(text, TEXT_CAP, self_path + last);
    add(text, TEXT_CAP, L" [options]\n\n"
        "  (no option)        open the installation manager: it checks what is installed,\n"
        "                     then offers Install, Repair and Uninstall\n"
        "  /MANAGE            open the installation manager\n"
        "  /S  /SILENT        install without any window or question (/VERYSILENT and /QUIET are the same)\n"
        "  /CONSOLE           install with text progress in this console\n"
        "  /EXTRACT=<folder>  unpack the setup script and the application archive; installs nothing\n"
        "  /UNINSTALL         remove the installed program\n"
        "  /REPAIR            install again over the existing installation (keeps settings and history)\n"
        "  /DIAGNOSE          write a diagnosis report; installs nothing\n"
        "  /SELFTEST          check every file packed into this installer; changes nothing\n"
        "  /?  /HELP          show this text\n\n"
        "  /LOG=<file>        write the installation log to this file\n"
        "  /ALLOWDOWNGRADE    install although a newer version is already installed\n"
        "  /REQUIREBRAVE      stop with exit code 7 when Brave is not installed\n"
        "  /NOSHORTCUTS       do not create the Start menu entries\n"
        "  /REMOVEHISTORY     with /UNINSTALL: also delete the download history\n"
        "  /ALLOWUNVERIFIED   accept a tool download whose checksum is unknown (not recommended)\n\n"
        "Exit codes: 0 done  1 failed  2 cancelled  3 installer damaged  4 a download could not be verified\n"
        "           5 a newer version is installed  6 setup is already running  7 Brave is not installed\n"
        "           8 this Windows is not supported  9 nothing is installed to remove  10 unknown option\n"
        "           11 not a Brave Music Downloader installer  12 Windows PowerShell is missing\n");
}

/* ---- reading the installer's own file ------------------------------------------------ */

typedef struct { HANDLE file; } self_file;

static DWORD io_error;            /* why the last read of the installer file failed */

/* lc_reader: return 0 when `len` bytes were read, nonzero otherwise. */
static int self_read(void *context, uint64_t at, void *buffer, size_t len) {
    LARGE_INTEGER position;
    DWORD got = 0;
    position.QuadPart = (LONGLONG)at;
    if (len > 0x7ffff000) {
        io_error = ERROR_INVALID_PARAMETER;
        return 1;
    }
    if (!SetFilePointerEx(((self_file *)context)->file, position, NULL, FILE_BEGIN)) {
        io_error = GetLastError();
        return 1;
    }
    if (!ReadFile(((self_file *)context)->file, buffer, (DWORD)len, &got, NULL)) {
        io_error = GetLastError();
        return 1;
    }
    if (got != (DWORD)len) {      /* fewer bytes than the file size announced: the file changed meanwhile */
        io_error = ERROR_HANDLE_EOF;
        return 1;
    }
    return 0;
}

/* The container's verdict, as an exit code. */
static int container_code(int code) {
    if (code == LC_E_NOT_INSTALLER) return EXIT_NOT_INSTALLER;
    if (code == LC_E_DAMAGED || code == LC_E_HASH || code == LC_E_IO) return EXIT_DAMAGED;
    return EXIT_FAILED;
}

typedef struct { HANDLE file; } file_sink;

static int sink_write(void *context, const unsigned char *data, size_t len) {
    DWORD written = 0;
    while (len) {
        DWORD chunk = len > 0x7ffff000 ? 0x7ffff000 : (DWORD)len;
        if (!WriteFile(((file_sink *)context)->file, data, chunk, &written, NULL) || written != chunk) return LC_E_WRITE;
        data += chunk;
        len -= chunk;
    }
    return LC_OK;
}

/* Stream one entry into `target`, verifying its SHA-256 on the way. The data is written to
 * `target.partial` and renamed only after the hash matched; on any failure both are deleted. */
static int extract_entry(lc_source *source, const lc_entry *entry, const wchar_t *target) {
    static file_sink sink;
    wchar_t partial[PATH_CAP];
    HANDLE file;
    int result;
    partial[0] = 0;
    add(partial, PATH_CAP, target);
    add(partial, PATH_CAP, L".partial");
    if (overflow) return LC_E_ARG;
    file = CreateFileW(partial, FILE_GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return LC_E_WRITE;
    sink.file = file;
    result = lc_verify_entry(source, entry, sink_write, &sink);
    CloseHandle(file);
    if (result) {
        DeleteFileW(partial);
        return result;
    }
    if (!MoveFileExW(partial, target, MOVEFILE_REPLACE_EXISTING)) {
        DeleteFileW(partial);
        return LC_E_WRITE;
    }
    return LC_OK;
}

/* Create `path` and the folders above it. The root (a drive or \\server\share) is never created.
 * Returns false when the folder does not exist afterwards. */
static int make_dirs(const wchar_t *path) {
    wchar_t part[PATH_CAP];
    size_t start = 0, k, j, n = lc_wlen(path);
    int root_parts = 0;
    if (n + 1 > PATH_CAP) return 0;
    if (n > 4 && path[0] == L'\\' && path[1] == L'\\' && path[2] == L'?' && path[3] == L'\\') {
        start = 4;                                                   /* \\?\ (long paths) */
        if (n > start + 4 && (path[start] | 32) == L'u' && (path[start + 1] | 32) == L'n'
            && (path[start + 2] | 32) == L'c' && path[start + 3] == L'\\') {
            start += 4;
            root_parts = 2;                                          /* \\?\UNC\server\share\ */
        }
    } else if (n > 2 && path[0] == L'\\' && path[1] == L'\\') {
        start = 2;
        root_parts = 2;                                              /* \\server\share\ */
    }
    if (root_parts == 0 && n > start + 1 && path[start + 1] == L':') start += 2;   /* C:\ */
    while (start < n && path[start] == L'\\') start++;                   /* the root itself exists */
    for (k = start; root_parts > 0 && k < n; k++) {
        if (path[k] == L'\\' && --root_parts == 0) {
            start = k + 1;
            break;
        }
    }
    for (k = start; k <= n; k++) {
        if (!(k == n || path[k] == L'\\') || k == 0 || path[k - 1] == L'\\') continue;
        for (j = 0; j < k; j++) part[j] = path[j];
        part[k] = 0;
        if (GetFileAttributesW(part) == INVALID_FILE_ATTRIBUTES && !CreateDirectoryW(part, NULL)
            && GetFileAttributesW(part) == INVALID_FILE_ATTRIBUTES) return 0;
    }
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

static int folder_is_empty(const wchar_t *dir) {
    WIN32_FIND_DATAW data;
    wchar_t pattern[PATH_CAP];
    HANDLE find;
    int empty = 1;
    path_join(pattern, PATH_CAP, dir, L"*");
    find = FindFirstFileW(pattern, &data);
    if (find == INVALID_HANDLE_VALUE) return 0;
    do {
        if (data.cFileName[0] == L'.' && (data.cFileName[1] == 0
            || (data.cFileName[1] == L'.' && data.cFileName[2] == 0))) continue;
        empty = 0;
    } while (FindNextFileW(find, &data));
    FindClose(find);
    return empty;
}

/* Delete the unpacked bootstrap files and the work folder itself. The script has ended, but a
 * process that has just terminated can keep the folder as its current directory for a moment
 * longer, so the removal is retried for a short time. An empty folder that stays behind anyway
 * (the computer was switched off meanwhile) holds nothing; Setup.ps1 removes such folders at
 * the next run. */
static void remove_work_dir(const lc_container *parsed) {
    uint32_t i;
    unsigned attempt;
    wchar_t name[LC_NAME_MAX], target[PATH_CAP];
    for (i = 0; i < parsed->count; i++) {
        if (!(parsed->entries[i].flags & LC_FLAG_BOOTSTRAP)) continue;
        to_wide(parsed->entries[i].name, name);
        path_join(target, PATH_CAP, work_dir, name);
        DeleteFileW(target);
        add(target, PATH_CAP, L".partial");
        DeleteFileW(target);
    }
    for (attempt = 0; attempt < 40 && !RemoveDirectoryW(work_dir); attempt++) Sleep(50);
}

/* The error text of the container code, as a line of text. */
static void reason_text(int code, wchar_t *out) {
    to_wide(lc_error_text(code), out);
}

#define REASON_CAP 96

/* ---- the actions --------------------------------------------------------------------- */

/* /SELFTEST: verify every entry without writing anything. */
static int self_test(lc_source *source, const lc_container *parsed) {
    uint32_t i;
    int bad = 0;
    wchar_t name[LC_NAME_MAX], field[50], reason[REASON_CAP];
    text[0] = 0;
    add(text, TEXT_CAP, title);
    add(text, TEXT_CAP, L" - self-test\n\n");
    for (i = 0; i < parsed->count; i++) {
        const lc_entry *entry = &parsed->entries[i];
        int result = lc_verify_entry(source, entry, NULL, NULL);
        size_t n = to_wide(entry->name, name), k;                /* the name, padded to a column of 49 */
        for (k = 0; k < 49; k++) field[k] = k < n ? name[k] : L' ';
        field[49] = 0;
        line[0] = 0;
        add(line, LINE_CAP, result ? L"FAILED  " : L"ok      ");
        add(line, LINE_CAP, field);
        add_number(line, LINE_CAP, entry->size);
        add(line, LINE_CAP, L" bytes");
        if (result) {
            add(line, LINE_CAP, L"  - ");
            reason_text(result, reason);
            add(line, LINE_CAP, reason);
            bad++;
        }
        add(text, TEXT_CAP, line);
        add(text, TEXT_CAP, L"\n");
    }
    add(text, TEXT_CAP, L"\n");
    if (bad) {
        add_number(text, TEXT_CAP, (uint64_t)bad);
        add(text, TEXT_CAP, L" of ");
        add_number(text, TEXT_CAP, parsed->count);
        add(text, TEXT_CAP, L" files do not match. The installer file is damaged: download it again.\n");
    } else {
        add(text, TEXT_CAP, L"Self-test passed: all ");
        add_number(text, TEXT_CAP, parsed->count);
        add(text, TEXT_CAP, L" files match their SHA-256.\n");
    }
    show(text, bad ? MB_ICONERROR : MB_ICONINFORMATION);
    return bad ? EXIT_DAMAGED : EXIT_OK;
}

/* /EXTRACT=<folder>: write every entry (bootstrap or not) into a new or empty folder. */
static int extract_all(lc_source *source, const lc_container *parsed, const wchar_t *dir) {
    uint32_t i, k;
    int result;
    wchar_t name[LC_NAME_MAX], target[PATH_CAP], reason[REASON_CAP];
    if (!make_dirs(dir)) fail_with_error(EXIT_FAILED, L"The folder could not be created");
    if (!folder_is_empty(dir)) fail_with_error(EXIT_FAILED, L"Nothing was extracted: the folder is not empty");
    for (i = 0; i < parsed->count; i++) {
        to_wide(parsed->entries[i].name, name);
        path_join(target, PATH_CAP, dir, name);
        result = extract_entry(source, &parsed->entries[i], target);
        if (result) {
            for (k = 0; k < i; k++) {                         /* leave the folder as it was */
                to_wide(parsed->entries[k].name, name);
                path_join(target, PATH_CAP, dir, name);
                DeleteFileW(target);
            }
            text[0] = 0;
            add(text, TEXT_CAP, L"The installer file could not be unpacked: ");
            add(text, TEXT_CAP, name);
            add(text, TEXT_CAP, L" (");
            reason_text(result, reason);
            add(text, TEXT_CAP, reason);
            add(text, TEXT_CAP, L").\nDownload the installer again and compare its SHA-256 with the published one.");
            fail(container_code(result));
        }
    }
    text[0] = 0;
    add(text, TEXT_CAP, L"Extracted ");
    add_number(text, TEXT_CAP, parsed->count);
    add(text, TEXT_CAP, L" files into\n");
    add(text, TEXT_CAP, dir);
    show(text, MB_ICONINFORMATION);
    return EXIT_OK;
}

/* Install, uninstall, repair or diagnose: unpack the bootstrap files and hand over to the script. */
static int run_script(lc_source *source, const lc_container *parsed, const lc_options *opt) {
    wchar_t name[LC_NAME_MAX], target[PATH_CAP], script[PATH_CAP], app_zip[PATH_CAP], python_zip[PATH_CAP];
    wchar_t reason[REASON_CAP];
    const wchar_t *action;
    DWORD flags = CREATE_NO_WINDOW, code = EXIT_FAILED, n;
    uint32_t i;
    int result, has_python = 0, has_bundle = 0;

    /* the work folder: %TEMP%\BMD-Setup-<process id>-<tick count> */
    work_dir[0] = 0;
    n = GetTempPathW(PATH_CAP - 80, work_dir);
    if (!n || n >= PATH_CAP - 80) fail_with_error(EXIT_FAILED, L"Windows did not provide a usable temporary folder");
    add(work_dir, PATH_CAP, L"BMD-Setup-");
    add_number(work_dir, PATH_CAP, GetCurrentProcessId());
    add(work_dir, PATH_CAP, L"-");
    add_number(work_dir, PATH_CAP, GetTickCount());
    if (!CreateDirectoryW(work_dir, NULL)) fail_with_error(EXIT_FAILED, L"The temporary folder could not be created");

    for (i = 0; i < parsed->count; i++) {
        const lc_entry *entry = &parsed->entries[i];
        if (!(entry->flags & LC_FLAG_BOOTSTRAP)) continue;
        to_wide(entry->name, name);
        path_join(target, PATH_CAP, work_dir, name);
        result = extract_entry(source, entry, target);
        if (result) {
            text[0] = 0;
            add(text, TEXT_CAP, L"The installer file could not be unpacked: ");
            add(text, TEXT_CAP, name);
            add(text, TEXT_CAP, L" (");
            reason_text(result, reason);
            add(text, TEXT_CAP, reason);
            add(text, TEXT_CAP, L").\nDownload the installer again and compare its SHA-256 with the published one.");
            remove_work_dir(parsed);
            fail(container_code(result));
        }
    }
    if (lc_find(parsed, "Setup.ps1") < 0 || lc_find(parsed, "Application.zip") < 0) {
        text[0] = 0;
        add(text, TEXT_CAP, L"This is not a complete Brave Music Downloader installer: "
                           L"the setup script or the application archive is missing.");
        remove_work_dir(parsed);
        fail(EXIT_NOT_INSTALLER);
    }
    path_join(script, PATH_CAP, work_dir, L"Setup.ps1");
    path_join(app_zip, PATH_CAP, work_dir, L"Application.zip");
    python_zip[0] = 0;
    i = (uint32_t)lc_find(parsed, "python");
    if (i < parsed->count && (parsed->entries[i].flags & LC_FLAG_BOOTSTRAP)) {
        path_join(python_zip, PATH_CAP, work_dir, L"python");
        has_python = 1;
    }

    /* Windows PowerShell, the one that ships with Windows */
    powershell[0] = 0;
    n = GetWindowsDirectoryW(powershell, PATH_CAP - 80);
    if (!n || n >= PATH_CAP - 80) powershell[0] = 0;
    add(powershell, PATH_CAP, L"\\System32\\WindowsPowerShell\\v1.0\\powershell.exe");

    /* The script's command line. Every path is passed quoted; lc_append_quoted refuses quotes and
     * control characters in a path instead of trying to escape them. */
    overflow = 0;
    command[0] = 0;
    add_quoted(command, COMMAND_CAP, powershell);
    add(command, COMMAND_CAP, L" -NoLogo -NoProfile -ExecutionPolicy Bypass -File");
    add_quoted(command, COMMAND_CAP, script);
    add(command, COMMAND_CAP, L" -PayloadZip");
    add_quoted(command, COMMAND_CAP, app_zip);
    add(command, COMMAND_CAP, L" -Installer");
    add_quoted(command, COMMAND_CAP, self_path);
    /* Files that are not unpacked here (the offline build's tool archives) are read from the
     * installer file by the engine; it is told so with -Bundle. */
    for (i = 0; i < parsed->count; i++)
        if (!(parsed->entries[i].flags & LC_FLAG_BOOTSTRAP)) has_bundle = 1;
    if (has_bundle) add(command, COMMAND_CAP, L" -Bundle");
    if (has_python) {
        add(command, COMMAND_CAP, L" -PythonZip");
        add_quoted(command, COMMAND_CAP, python_zip);
    }
    add(command, COMMAND_CAP, L" -Mode ");
    add(command, COMMAND_CAP, opt->ui == LC_UI_GUI ? L"Gui" : opt->ui == LC_UI_CONSOLE ? L"Console" : L"Silent");
    action = opt->action == LC_ACTION_UNINSTALL ? L"Uninstall" : opt->action == LC_ACTION_REPAIR ? L"Repair"
             : opt->action == LC_ACTION_DIAGNOSE ? L"Diagnose"
             : opt->action == LC_ACTION_MANAGE && opt->ui != LC_UI_SILENT ? L"Manage"
             : L"Install";           /* /S keeps its documented meaning: install silently */
    add(command, COMMAND_CAP, L" -Action ");
    add(command, COMMAND_CAP, action);
    if (opt->log_path) {
        add(command, COMMAND_CAP, L" -Log");
        add_quoted(command, COMMAND_CAP, opt->log_path);
    }
    if (opt->options & LC_OPT_ALLOW_DOWNGRADE) add(command, COMMAND_CAP, L" -AllowDowngrade");
    if (opt->options & LC_OPT_REQUIRE_BRAVE) add(command, COMMAND_CAP, L" -RequireBrave");
    if (opt->options & LC_OPT_NO_SHORTCUTS) add(command, COMMAND_CAP, L" -NoShortcuts");
    if (opt->options & LC_OPT_REMOVE_HISTORY) add(command, COMMAND_CAP, L" -RemoveHistory");
    if (opt->options & LC_OPT_ALLOW_UNVERIFIED) add(command, COMMAND_CAP, L" -AllowUnverified");
    if (overflow) {
        remove_work_dir(parsed);
        text[0] = 0;
        add(text, TEXT_CAP, L"An option value is too long or contains a character that cannot be passed on "
                           L"(a quote or a control character).");
        fail(EXIT_USAGE);
    }

    /* The script's window (or text) is the interface; no console window is added in window and
     * silent mode. */
    zero(&startup, sizeof startup);
    zero(&process, sizeof process);
    startup.cb = sizeof startup;
    if (opt->ui == LC_UI_CONSOLE) {
        flags = CREATE_NEW_CONSOLE;
        startup.dwFlags = STARTF_USESHOWWINDOW;
        startup.wShowWindow = SW_SHOWNORMAL;
    }
    if (!CreateProcessW(powershell, command, NULL, NULL, FALSE, flags, NULL, work_dir, &startup, &process)) {
        remove_work_dir(parsed);
        fail_with_error(EXIT_NO_POWERSHELL,
                        L"Windows PowerShell could not be started. Setup needs the PowerShell that ships with Windows");
    }
    CloseHandle(process.hThread);
    if (WaitForSingleObject(process.hProcess, INFINITE) != WAIT_OBJECT_0
        || !GetExitCodeProcess(process.hProcess, &code) || code == STILL_ACTIVE) code = EXIT_FAILED;
    CloseHandle(process.hProcess);
    remove_work_dir(parsed);
    /* The script ends with one of the codes above. Anything else means it stopped on the way (a
     * parser error, a crash, a stopped process): that is a failed setup, not a code of its own. */
    if (code > EXIT_NO_POWERSHELL) {
        text[0] = 0;
        add(text, TEXT_CAP, L"Setup did not complete: the setup script ended unexpectedly (exit code ");
        add_number(text, TEXT_CAP, code);
        add(text, TEXT_CAP, L").\nDetails, if any were written: ");
        add(text, TEXT_CAP, L"%LOCALAPPDATA%\\BraveMusicDownloader\\Install.log");
        fail(EXIT_FAILED);
    }
    return (int)code;
}

void mainCRTStartup(void) {
    static lc_source source;
    static self_file self;
    LARGE_INTEGER size;
    int argc = 0, result;
    DWORD got;
    LPWSTR *argv;
    wchar_t error[256], reason[REASON_CAP];

    add(title, sizeof title / sizeof title[0], L"Brave Music Downloader Setup " SETUP_VERSION_TEXT);
    argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) ExitProcess(EXIT_FAILED);
    overflow = 0;
    result = lc_parse_args(argc, argv, &options, error, sizeof error / sizeof error[0]);
    got = GetModuleFileNameW(NULL, self_path, PATH_CAP);
    if (!got || got >= PATH_CAP) ExitProcess(EXIT_FAILED);
    if (result) {                              /* unknown option: say what was wrong and what is allowed */
        open_output();
        no_box = 0;
        usage(error);
        show(text, MB_ICONERROR);
        ExitProcess(EXIT_USAGE);
    }
    no_box = options.ui == LC_UI_SILENT;
    open_output();
    /* argv is kept: options.extract_dir and options.log_path point into it, and the memory is
     * released when the process ends. */
    if (options.action == LC_ACTION_HELP) {
        usage(NULL);
        show(text, MB_ICONINFORMATION);
        ExitProcess(EXIT_OK);
    }
    self.file = CreateFileW(self_path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                            NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_RANDOM_ACCESS, NULL);
    if (self.file == INVALID_HANDLE_VALUE) fail_with_error(EXIT_FAILED, L"The installer file could not be read");
    if (!GetFileSizeEx(self.file, &size)) fail_with_error(EXIT_FAILED, L"The installer file could not be measured");
    source.read = self_read;
    source.context = &self;
    source.file_size = (uint64_t)size.QuadPart;
    result = lc_parse(&source, &container);
    if (result) {
        text[0] = 0;
        if (result == LC_E_NOT_INSTALLER) {
            add(text, TEXT_CAP, L"This file is not a Brave Music Downloader installer.");
        } else {
            add(text, TEXT_CAP, L"This file is not an intact Brave Music Downloader installer: ");
            reason_text(result, reason);
            add(text, TEXT_CAP, reason);
            if (result == LC_E_IO && io_error) {    /* a read failed: say why Windows refused */
                add(text, TEXT_CAP, L" (Windows error ");
                add_number(text, TEXT_CAP, io_error);
                add(text, TEXT_CAP, L")");
            }
        }
        add(text, TEXT_CAP, L"\nDownload the installer again and compare its SHA-256 with the published one.");
        fail(container_code(result));
    }
    switch (options.action) {
        case LC_ACTION_SELFTEST:
            ExitProcess(self_test(&source, &container));
        case LC_ACTION_EXTRACT:
            ExitProcess(extract_all(&source, &container, options.extract_dir));
        default:
            ExitProcess(run_script(&source, &container, &options));
    }
}
