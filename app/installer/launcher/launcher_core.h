/* launcher_core: portable part of the setup launcher (no Windows, no C runtime).
 *
 * Everything here is compiled twice: into the Windows launcher (freestanding, -nostdlib) and
 * into tests/native/test_launcher_core.c on a normal host, so the parser that decides whether an
 * installer file is intact is really tested and not only cross-compiled.
 *
 * Container layout (see app/installer/payload_container.py):
 *   [PE image][entry data ...][index][footer 72 bytes][0..7 zero bytes][certificate table, once signed]
 *   footer:  magic 8s | pe_size Q | index_offset Q | index_size Q | count I | reserved I | index_sha256 32s
 *   entry:   name 64s | offset Q | size Q | flags I | reserved I | sha256 32s
 * All integers are little-endian. The footer is found through the PE header (certificate table
 * entry), so signing the finished installer with signtool does not hide it: the signing tool starts
 * that table on an 8-byte boundary and may insert up to seven zero bytes after the footer, which is
 * the only thing allowed there.
 */
#ifndef BMD_LAUNCHER_CORE_H
#define BMD_LAUNCHER_CORE_H

#include <stddef.h>
#include <stdint.h>
#include <wchar.h>

#define LC_MAX_ENTRIES 64
#define LC_NAME_MAX 64
#define LC_NAME_LIMIT 48          /* longest name the build accepts (characters) */
#define LC_FOOTER_SIZE 72
#define LC_ENTRY_SIZE 120
#define LC_FLAG_BOOTSTRAP 1u

enum {
    LC_OK = 0,
    LC_E_IO = 1,                  /* the installer file could not be read */
    LC_E_NOT_INSTALLER = 2,       /* no footer magic: not one of ours */
    LC_E_DAMAGED = 3,             /* truncated, edited, out of bounds, bad index hash */
    LC_E_HASH = 4,                /* an entry's content does not match its SHA-256 */
    LC_E_WRITE = 5,               /* the destination could not be written */
    LC_E_USAGE = 6,               /* unknown or malformed command-line option */
    LC_E_ARG = 7                  /* an argument cannot be put on a command line safely */
};

enum { LC_UI_GUI = 0, LC_UI_SILENT = 1, LC_UI_CONSOLE = 2 };
enum { LC_ACTION_INSTALL = 0, LC_ACTION_UNINSTALL, LC_ACTION_REPAIR, LC_ACTION_DIAGNOSE,
       LC_ACTION_EXTRACT, LC_ACTION_SELFTEST, LC_ACTION_HELP, LC_ACTION_MANAGE };
enum { LC_OPT_ALLOW_DOWNGRADE = 1, LC_OPT_REQUIRE_BRAVE = 2, LC_OPT_NO_SHORTCUTS = 4,
       LC_OPT_REMOVE_HISTORY = 8, LC_OPT_ALLOW_UNVERIFIED = 16 };

typedef struct {
    char name[LC_NAME_MAX];
    uint64_t offset;
    uint64_t size;
    uint32_t flags;
    unsigned char sha256[32];
} lc_entry;

typedef struct {
    uint64_t pe_size;
    uint64_t index_offset;
    uint32_t count;
    lc_entry entries[LC_MAX_ENTRIES];
} lc_container;

/* Read `len` bytes at absolute file offset `at`. Return 0 on success, nonzero on failure. */
typedef int (*lc_reader)(void *context, uint64_t at, void *buffer, size_t len);

typedef struct {
    lc_reader read;
    void *context;
    uint64_t file_size;
} lc_source;

typedef struct {
    int ui;
    int action;
    const wchar_t *extract_dir;   /* /EXTRACT=<dir> */
    const wchar_t *log_path;      /* /LOG=<file> */
    unsigned options;             /* LC_OPT_* bit set */
} lc_options;

const char *lc_error_text(int code);
int lc_valid_name(const char *name);

/* Parse and validate the container of the executable behind `source` (it must be a PE file).
 * On failure `*out` is zeroed. */
int lc_parse(lc_source *source, lc_container *out);
int lc_find(const lc_container *container, const char *name);

/* Stream one entry through `sink` (may be NULL) and verify its SHA-256. */
typedef int (*lc_sink)(void *context, const unsigned char *data, size_t len);
int lc_verify_entry(lc_source *source, const lc_entry *entry, lc_sink sink, void *context);

void lc_sha256(const void *data, size_t len, unsigned char out[32]);

/* Command line -> options. argv[0] is the program and is skipped. On LC_E_USAGE `error` holds a
 * message (at most error_cap-1 wide characters, always terminated). */
int lc_parse_args(int argc, wchar_t **argv, lc_options *options, wchar_t *error, size_t error_cap);

/* Append the argument `text` in quotes to the command line in `buffer`: ` "text"`, or only `"text"`
 * when the line is still empty (the program name is the first word). Refuses text containing a
 * quote or a control character: file names cannot contain them, so such input is a bug or an
 * attack, not a case to escape (Windows argument quoting is full of edge cases; never reaching
 * them is safer). */
int lc_append_quoted(wchar_t *buffer, size_t cap, size_t *length, const wchar_t *text);
int lc_append_text(wchar_t *buffer, size_t cap, size_t *length, const wchar_t *text);

size_t lc_wlen(const wchar_t *text);
int lc_wcstr(const wchar_t *text, wchar_t c);          /* first index of c, or -1 */

#endif
