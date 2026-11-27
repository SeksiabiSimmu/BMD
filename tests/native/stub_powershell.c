/* A stand-in for Windows PowerShell, used by tests/test_launcher_wine.py.
 *
 * The real launcher starts "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe". Wine has
 * no Windows PowerShell (its powershell.exe does nothing), so the test puts this program there for
 * the duration of the run. It records what it was asked to do, so the test can check the hand-over:
 *
 *   BMD_STUB_OUT=<file>    where to write the record (UTF-8, one `kind|value...` line per fact)
 *   BMD_STUB_EXIT=<n>      the exit code to return (default 0)
 *   BMD_STUB_SLEEP=<ms>    wait before exiting (default 0)
 *
 * The record lists every command-line argument, the current folder, the files in it with size and
 * SHA-256 (launcher_core.c supplies the hash), and whether the installer file it was told about
 * still exists. Built like the launcher: freestanding, no C runtime.
 */
#include <windows.h>
#include <shellapi.h>
#include "launcher_core.h"

#define PATH_CAP 1024
#define REPORT_CAP (256 * 1024)
#define CONTENT_CAP (4 * 1024 * 1024)

static char report[REPORT_CAP], content[CONTENT_CAP];
static size_t used;
static DWORD written;
static HANDLE output;

static void emit_char(char c) {
    if (used + 1 < REPORT_CAP) report[used++] = c;
}

static void emit(const char *text) {
    while (*text) emit_char(*text++);
}

static void emit_wide(const wchar_t *text) {
    int size = WideCharToMultiByte(CP_UTF8, 0, text, -1, report + used, (int)(REPORT_CAP - used), NULL, NULL);
    if (size > 1) used += (size_t)size - 1;                      /* without the terminator */
}

static void emit_number(uint64_t value) {
    char digits[24];
    size_t n = 0;
    do {
        digits[n++] = (char)('0' + value % 10);
        value /= 10;
    } while (value);
    while (n) emit_char(digits[--n]);
}

static void emit_hex(const unsigned char *data, size_t n) {
    static const char digits[] = "0123456789abcdef";
    size_t i;
    for (i = 0; i < n; i++) {
        emit_char(digits[data[i] >> 4]);
        emit_char(digits[data[i] & 15]);
    }
}

static void environment(const wchar_t *name, wchar_t *buffer, DWORD cap) {
    DWORD n = GetEnvironmentVariableW(name, buffer, cap);
    if (!n || n >= cap) buffer[0] = 0;
}

static DWORD number_in(const wchar_t *text) {
    DWORD value = 0;
    while (*text >= L'0' && *text <= L'9') value = value * 10 + (DWORD)(*text++ - L'0');
    return value;
}

/* Is `argument` the option `name` (given in lower case)? */
static int is_option(const wchar_t *argument, const wchar_t *name) {
    if (argument[0] != L'-') return 0;
    argument++;
    for (; *name; argument++, name++) {
        wchar_t a = *argument >= L'A' && *argument <= L'Z' ? (wchar_t)(*argument + 32) : *argument;
        if (a != *name) return 0;
    }
    return *argument == 0;
}

static void copy_wide(wchar_t *to, const wchar_t *from, size_t cap) {
    size_t k = 0;
    for (; from[k] && k + 1 < cap; k++) to[k] = from[k];
    to[k] = 0;
}

/* One file of the current folder: name, size and SHA-256. */
static void record_file(const wchar_t *name) {
    static unsigned char digest[32];
    wchar_t path[PATH_CAP];
    HANDLE file;
    LARGE_INTEGER size;
    DWORD got = 0;
    copy_wide(path, name, PATH_CAP);
    file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
                       OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    emit("file|");
    emit_wide(name);
    emit("|");
    if (file == INVALID_HANDLE_VALUE) {
        emit("unreadable\r\n");
        return;
    }
    GetFileSizeEx(file, &size);
    emit_number((uint64_t)size.QuadPart);
    emit("|");
    if (size.QuadPart > 0 && (uint64_t)size.QuadPart <= CONTENT_CAP
        && ReadFile(file, content, (DWORD)size.QuadPart, &got, NULL) && got == (DWORD)size.QuadPart) {
        lc_sha256(content, (size_t)got, digest);
        emit_hex(digest, 32);
    } else {
        emit("not-hashed");
    }
    emit("\r\n");
    CloseHandle(file);
}

void mainCRTStartup(void) {
    int argc = 0, i;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    wchar_t target[PATH_CAP], value[PATH_CAP], current[PATH_CAP];
    WIN32_FIND_DATAW data;
    HANDLE find;
    DWORD exit_code = 0;

    environment(L"BMD_STUB_SLEEP", value, PATH_CAP);
    if (number_in(value)) Sleep(number_in(value));
    environment(L"BMD_STUB_EXIT", value, PATH_CAP);
    exit_code = number_in(value);

    used = 0;
    for (i = 0; i < argc; i++) {
        emit("argument|");
        emit_wide(argv[i]);
        emit("\r\n");
    }
    current[0] = 0;
    GetCurrentDirectoryW(PATH_CAP, current);
    emit("cwd|");
    emit_wide(current);
    emit("\r\n");
    for (i = 1; i + 1 < argc; i++) {
        if (is_option(argv[i], L"installer")) {                  /* the installer the launcher was started from */
            emit("installer|");
            emit_number(GetFileAttributesW(argv[i + 1]) != INVALID_FILE_ATTRIBUTES);
            emit("|");
            emit_wide(argv[i + 1]);
            emit("\r\n");
        }
    }
    find = FindFirstFileW(L"*", &data);
    if (find != INVALID_HANDLE_VALUE) {
        do {
            if (data.cFileName[0] == L'.' && (data.cFileName[1] == 0
                || (data.cFileName[1] == L'.' && data.cFileName[2] == 0))) continue;
            if (data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                emit("folder|");
                emit_wide(data.cFileName);
                emit("\r\n");
            } else {
                record_file(data.cFileName);
            }
        } while (FindNextFileW(find, &data));
        FindClose(find);
    }
    environment(L"BMD_STUB_OUT", target, PATH_CAP);
    if (target[0]) {
        output = CreateFileW(target, FILE_GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (output != INVALID_HANDLE_VALUE) {
            WriteFile(output, report, (DWORD)used, &written, NULL);
            CloseHandle(output);
        }
    }
    ExitProcess(exit_code);
}
