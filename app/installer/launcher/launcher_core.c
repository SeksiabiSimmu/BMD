/* launcher_core: portable part of the setup launcher (no Windows, no C runtime). See header. */
#include "launcher_core.h"

/* ---------------------------------------------------------------- memory ----
 * No C runtime is linked into the Windows launcher, so nothing from <string.h> is used here.
 */
static void *xmemcpy(void *to, const void *from, size_t len) {
    unsigned char *t = (unsigned char *)to;
    const unsigned char *f = (const unsigned char *)from;
    while (len--) *t++ = *f++;
    return to;
}

static void *xmemset(void *to, int value, size_t len) {
    unsigned char *t = (unsigned char *)to;
    while (len--) *t++ = (unsigned char)value;
    return to;
}

static int xmemcmp(const void *a, const void *b, size_t len) {
    const unsigned char *x = (const unsigned char *)a, *y = (const unsigned char *)b;
    while (len--) {
        if (*x != *y) return *x < *y ? -1 : 1;
        x++; y++;
    }
    return 0;
}

static size_t xstrlen(const char *s) {
    size_t n = 0;
    while (s[n]) n++;
    return n;
}

size_t lc_wlen(const wchar_t *text) {
    size_t n = 0;
    while (text[n]) n++;
    return n;
}

int lc_wcstr(const wchar_t *text, wchar_t c) {
    size_t i;
    for (i = 0; text[i]; i++)
        if (text[i] == c) return (int)i;
    return -1;
}

static uint16_t rd16(const unsigned char *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t rd32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint64_t rd64(const unsigned char *p) {
    return (uint64_t)rd32(p) | ((uint64_t)rd32(p + 4) << 32);
}

static uint32_t rd32be(const unsigned char *p) {          /* SHA-256 reads its message words big-endian */
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static void wr32be(unsigned char *p, uint32_t v) {         /* SHA-256 is big-endian throughout */
    p[0] = (unsigned char)(v >> 24); p[1] = (unsigned char)(v >> 16);
    p[2] = (unsigned char)(v >> 8); p[3] = (unsigned char)v;
}

const char *lc_error_text(int code) {
    switch (code) {
        case LC_OK: return "ok";
        case LC_E_IO: return "the installer file could not be read";
        case LC_E_NOT_INSTALLER: return "this file is not a Brave Music Downloader installer";
        case LC_E_DAMAGED: return "the installer file is damaged or incomplete";
        case LC_E_HASH: return "a file inside the installer does not match its checksum";
        case LC_E_WRITE: return "the temporary folder could not be written";
        case LC_E_USAGE: return "unknown command-line option";
        case LC_E_ARG: return "an argument cannot be passed safely";
        default: return "unknown error";
    }
}

static int starts_with_ci(const char *name, const char *prefix, size_t n) {
    size_t i;
    for (i = 0; i < n; i++) {
        char a = name[i], b = prefix[i];
        if (a >= 'A' && a <= 'Z') a = (char)(a + 32);
        if (a != b) return 0;
    }
    return 1;
}

/* "CON", "NUL", "COM1", "LPT9", also with an extension ("aux.txt"): Windows would not create a
 * file with such a name but talk to a device instead. */
static int is_device_name(const char *name) {
    static const char *const devices[] = {"con", "prn", "aux", "nul", "conin$", "conout$"};
    size_t n = 0, i;
    while (name[n] && name[n] != '.') n++;
    while (n && name[n - 1] == ' ') n--;                /* Windows ignores trailing spaces here */
    if (n == 4 && name[3] >= '1' && name[3] <= '9'
        && (starts_with_ci(name, "com", 3) || starts_with_ci(name, "lpt", 3))) return 1;
    for (i = 0; i < sizeof devices / sizeof devices[0]; i++)
        if (xstrlen(devices[i]) == n && starts_with_ci(name, devices[i], n)) return 1;
    return 0;
}

int lc_valid_name(const char *name) {
    size_t n = xstrlen(name), i;
    if (n == 0 || n > LC_NAME_LIMIT) return 0;
    for (i = 0; i < n; i++) {
        unsigned char c = (unsigned char)name[i];
        int alnum = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
        /* the first and the last character must be alphanumeric: Windows silently drops trailing
         * dots and spaces from file names, so "x " and "x." could not be created as named */
        if (i == 0 || i == n - 1 ? !alnum : !(alnum || c == ' ' || c == '.' || c == '_' || c == '-')) return 0;
    }
    return !is_device_name(name);
}

/* ---------------------------------------------------------------- SHA-256 ---- */
typedef struct {
    uint32_t h[8];
    uint64_t bits;
    unsigned char buf[64];
    size_t used;
} sha256_ctx;

static const uint32_t SHA_K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u};

static uint32_t rotr(uint32_t x, unsigned n) { return (x >> n) | (x << (32u - n)); }

static void sha_block(sha256_ctx *c, const unsigned char *p) {
    uint32_t w[64], a, b, d, e, f, g, h, cc;
    int i;
    for (i = 0; i < 16; i++) w[i] = rd32be(p + (size_t)i * 4);
    for (i = 16; i < 64; i++) {
        uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = c->h[0]; b = c->h[1]; cc = c->h[2]; d = c->h[3];
    e = c->h[4]; f = c->h[5]; g = c->h[6]; h = c->h[7];
    for (i = 0; i < 64; i++) {
        uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + s1 + ch + SHA_K[i] + w[i];
        uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = s0 + maj;
        h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void sha_init(sha256_ctx *c) {
    static const uint32_t iv[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                                   0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    xmemcpy(c->h, iv, sizeof iv);
    c->bits = 0;
    c->used = 0;
}

static void sha_update(sha256_ctx *c, const void *data, size_t len) {
    const unsigned char *p = (const unsigned char *)data;
    c->bits += (uint64_t)len * 8;
    while (len) {
        size_t take = 64 - c->used;
        if (take > len) take = len;
        xmemcpy(c->buf + c->used, p, take);
        c->used += take; p += take; len -= take;
        if (c->used == 64) { sha_block(c, c->buf); c->used = 0; }
    }
}

static void sha_final(sha256_ctx *c, unsigned char out[32]) {
    unsigned char pad[72];
    uint64_t bits = c->bits;
    size_t pad_len = (c->used < 56) ? 56 - c->used : 120 - c->used;
    unsigned char tail[8];
    int i;
    xmemset(pad, 0, sizeof pad);
    pad[0] = 0x80;
    sha_update(c, pad, pad_len);                      /* 0x80, zeros, then the bit length */
    wr32be(tail, (uint32_t)(bits >> 32)); wr32be(tail + 4, (uint32_t)bits);
    sha_update(c, tail, 8);
    for (i = 0; i < 8; i++) wr32be(out + (size_t)i * 4, c->h[i]);
}

void lc_sha256(const void *data, size_t len, unsigned char out[32]) {
    sha256_ctx c;
    sha_init(&c);
    sha_update(&c, data, len);
    sha_final(&c, out);
}

/* -------------------------------------------------------------- container ---- */
static int same_name_ci(const char *a, const char *b) {
    size_t i;
    for (i = 0; a[i] || b[i]; i++) {
        char x = a[i], y = b[i];
        if (x >= 'A' && x <= 'Z') x = (char)(x + 32);
        if (y >= 'A' && y <= 'Z') y = (char)(y + 32);
        if (x != y) return 0;
    }
    return 1;
}

/* Where the installer's data ends. Authenticode signing happens after the container was appended
 * and puts the certificate table "at the very end of an image file", started on an 8-byte boundary,
 * so a signed installer is [PE image][container][0..7 zero bytes][certificate table] and the footer
 * sits right before the padding. Its directory entry is the one entry whose address is a file
 * offset rather than an RVA. */
#define LC_HEADER_PROBE (64u * 1024u)                  /* any real PE header fits in here */
#define LC_FOOTER_PAD 7u                               /* zero bytes the signing tool may have inserted */

static int image_end(const unsigned char *header, size_t have, uint64_t size, uint64_t *end) {
    uint64_t pe, optional;
    uint32_t table_offset, table_size;
    size_t directories;
    if (have < 0x40 || header[0] != 'M' || header[1] != 'Z') return LC_E_NOT_INSTALLER;
    pe = rd32(header + 0x3c);
    if (pe > have || have - pe < 24 + 2 || xmemcmp(header + pe, "PE\0\0", 4)) return LC_E_NOT_INSTALLER;
    optional = pe + 24;
    switch (rd16(header + optional)) {                 /* magic of the optional header */
        case 0x10b: directories = 96; break;           /* PE32  */
        case 0x20b: directories = 112; break;          /* PE32+ */
        default: return LC_E_NOT_INSTALLER;
    }
    if (have - optional < directories + 4) return LC_E_NOT_INSTALLER;
    if (rd32(header + optional + directories - 4) < 5) {   /* fewer than 5 data directories: no certificate entry */
        *end = size;
        return LC_OK;
    }
    if (have - optional < directories + 5 * 8) return LC_E_NOT_INSTALLER;
    table_offset = rd32(header + optional + directories + 4 * 8);
    table_size = rd32(header + optional + directories + 4 * 8 + 4);
    if (table_size == 0) {
        *end = size;                                   /* not signed */
    } else {
        if (table_offset > size || table_size > size - table_offset) return LC_E_DAMAGED;
        *end = table_offset;
    }
    return LC_OK;
}

int lc_parse(lc_source *source, lc_container *out) {
    static const unsigned char MAGIC[8] = {'B', 'M', 'D', 'P', 'K', 'G', 0, 1};
    unsigned char header[LC_HEADER_PROBE], tail[LC_FOOTER_SIZE + LC_FOOTER_PAD], footer[LC_FOOTER_SIZE],
                  index[LC_MAX_ENTRIES * LC_ENTRY_SIZE], digest[32];
    uint64_t size = source->file_size, end, footer_end, index_end, pe_size, index_offset, index_size;
    size_t have = size < LC_HEADER_PROBE ? (size_t)size : LC_HEADER_PROBE;
    uint32_t count, i;
    int r = LC_E_DAMAGED;
    xmemset(out, 0, sizeof *out);
    if (size < LC_FOOTER_SIZE) return LC_E_NOT_INSTALLER;
    if (source->read(source->context, 0, header, have)) return LC_E_IO;
    r = image_end(header, have, size, &end);
    if (r) return r;
    r = LC_E_DAMAGED;
    if (end < LC_FOOTER_SIZE) return LC_E_NOT_INSTALLER;
    footer_end = end;                                   /* reduced by the padding, once the footer is found */
    {
        size_t length = end < sizeof tail ? (size_t)end : sizeof tail, k;
        unsigned pad;
        int found = 0;
        if (source->read(source->context, end - length, tail, length)) return LC_E_IO;
        for (pad = 0; pad <= LC_FOOTER_PAD && (size_t)(LC_FOOTER_SIZE + pad) <= length; pad++) {
            const unsigned char *at = tail + (length - LC_FOOTER_SIZE - pad);
            if (xmemcmp(at, MAGIC, 8)) continue;
            for (k = length - pad; k < length; k++)
                if (tail[k]) return r;                  /* only the signing tool's zeros may follow the footer */
            xmemcpy(footer, at, LC_FOOTER_SIZE);
            footer_end = end - pad;
            found = 1;
            break;
        }
        if (!found) return LC_E_NOT_INSTALLER;
    }
    pe_size = rd64(footer + 8);
    index_offset = rd64(footer + 16);
    index_size = rd64(footer + 24);
    count = rd32(footer + 32);
    if (rd32(footer + 36) != 0) return LC_E_DAMAGED;                  /* reserved must be zero */
    if (count == 0 || count > LC_MAX_ENTRIES) return LC_E_DAMAGED;
    if (pe_size == 0 || pe_size > index_offset) return LC_E_DAMAGED;
    index_end = footer_end - LC_FOOTER_SIZE;           /* the index ends where the footer begins */
    if (index_offset > index_end || index_size != (uint64_t)count * LC_ENTRY_SIZE
            || index_end - index_offset != index_size) return LC_E_DAMAGED;
    if (source->read(source->context, index_offset, index, (size_t)index_size)) return LC_E_IO;
    lc_sha256(index, (size_t)index_size, digest);
    if (xmemcmp(digest, footer + 40, 32)) return LC_E_DAMAGED;

    for (i = 0; i < count; i++) {
        const unsigned char *e = index + (size_t)i * LC_ENTRY_SIZE;
        lc_entry *entry = &out->entries[i];
        size_t n = 0, k;
        while (n < LC_NAME_MAX && e[n]) n++;
        if (n == LC_NAME_MAX) return r;                                /* not NUL-terminated */
        for (k = n; k < LC_NAME_MAX; k++)
            if (e[k]) return r;                                        /* garbage after the name */
        xmemcpy(entry->name, e, n + 1);
        entry->offset = rd64(e + LC_NAME_MAX);
        entry->size = rd64(e + LC_NAME_MAX + 8);
        entry->flags = rd32(e + LC_NAME_MAX + 16);
        if (rd32(e + LC_NAME_MAX + 20) != 0) return r;
        xmemcpy(entry->sha256, e + LC_NAME_MAX + 24, 32);
        if (!lc_valid_name(entry->name)) return r;
        if (entry->flags & ~LC_FLAG_BOOTSTRAP) return r;
        if (entry->offset < pe_size || entry->size > index_offset - entry->offset) return r;
        {
            uint32_t j;
            for (j = 0; j < i; j++)
                if (same_name_ci(entry->name, out->entries[j].name)) return r;
        }
    }
    out->pe_size = pe_size;
    out->index_offset = index_offset;
    out->count = count;
    return LC_OK;
}

int lc_find(const lc_container *container, const char *name) {
    uint32_t i;
    for (i = 0; i < container->count; i++)
        if (same_name_ci(container->entries[i].name, name)) return (int)i;
    return -1;
}

int lc_verify_entry(lc_source *source, const lc_entry *entry, lc_sink sink, void *context) {
    unsigned char buf[64 * 1024], digest[32];
    sha256_ctx c;
    uint64_t done = 0;
    sha_init(&c);
    while (done < entry->size) {
        size_t chunk = (entry->size - done > sizeof buf) ? sizeof buf : (size_t)(entry->size - done);
        if (source->read(source->context, entry->offset + done, buf, chunk)) return LC_E_IO;
        sha_update(&c, buf, chunk);
        if (sink && sink(context, buf, chunk)) return LC_E_WRITE;
        done += chunk;
    }
    sha_final(&c, digest);
    return xmemcmp(digest, entry->sha256, 32) ? LC_E_HASH : LC_OK;
}

/* ---------------------------------------------------------------- command ---- */
static void wcopy(wchar_t *to, const wchar_t *from, size_t n) {
    while (n--) *to++ = *from++;
}

static int lower_eq(wchar_t a, wchar_t b) {
    if (a >= L'A' && a <= L'Z') a = (wchar_t)(a + 32);
    if (b >= L'A' && b <= L'Z') b = (wchar_t)(b + 32);
    return a == b;
}

static int ci_eq(const wchar_t *a, const wchar_t *b) {
    while (*a && *b) {
        if (!lower_eq(*a, *b)) return 0;
        a++; b++;
    }
    return *a == *b;
}

int lc_append_text(wchar_t *buffer, size_t cap, size_t *length, const wchar_t *text) {
    size_t n = lc_wlen(text);
    if (*length + n + 1 > cap) return LC_E_ARG;
    wcopy(buffer + *length, text, n);
    *length += n;
    buffer[*length] = 0;
    return LC_OK;
}

int lc_append_quoted(wchar_t *buffer, size_t cap, size_t *length, const wchar_t *text) {
    size_t n = lc_wlen(text), i, lead = *length ? 1 : 0;   /* a space before every argument but the first */
    for (i = 0; i < n; i++)
        if (text[i] == L'"' || (unsigned)text[i] < 0x20) return LC_E_ARG;
    if (*length + lead + n + 3 > cap) return LC_E_ARG;
    if (lead) buffer[(*length)++] = L' ';
    buffer[(*length)++] = L'"';
    wcopy(buffer + *length, text, n);
    *length += n;
    buffer[(*length)++] = L'"';
    buffer[*length] = 0;
    return LC_OK;
}

/* Compose the whole message first, then copy what fits: a small buffer must still get the start
 * of the message and a terminator, not an empty string. */
static void usage_error(wchar_t *error, size_t cap, const wchar_t *what, const wchar_t *arg) {
    wchar_t message[192], piece[2];
    size_t length = 0, i, n;
    message[0] = 0;
    piece[1] = 0;
    (void)lc_append_text(message, 192, &length, what);   /* the fixed texts are far shorter than this */
    for (i = 0; i < 64 && arg[i]; i++) {                 /* bound what a typed argument can add */
        piece[0] = arg[i] < 0x20 ? L'?' : arg[i];
        if (lc_append_text(message, 192, &length, piece)) break;
    }
    if (arg[i]) (void)lc_append_text(message, 192, &length, L"...");
    if (!cap) return;
    n = length < cap - 1 ? length : cap - 1;
    wcopy(error, message, n);
    error[n] = 0;
}

int lc_parse_args(int argc, wchar_t **argv, lc_options *options, wchar_t *error, size_t error_cap) {
    lc_options opt;
    const wchar_t *why = L"Unknown or malformed option: ";
    int i;
    xmemset(&opt, 0, sizeof opt);
    opt.ui = LC_UI_GUI;
    opt.action = LC_ACTION_INSTALL;
    if (error_cap) error[0] = 0;
    for (i = 1; i < argc; i++) {
        const wchar_t *arg = argv[i], *value = L"";
        wchar_t name[32];
        int eq, has_value = 0;
        size_t n = 0;
        if (!arg) continue;
        if (arg[0] != L'/' && arg[0] != L'-') {
            why = L"Setup takes options only (for example /S or /EXTRACT=C:\\Folder); it does not take file names. Unexpected: ";
            break;
        }
        arg++;
        eq = lc_wcstr(arg, L'=');
        has_value = eq >= 0;
        if (has_value) value = arg + eq + 1;
        n = has_value ? (size_t)eq : lc_wlen(arg);
        if (n == 0 || n >= sizeof name / sizeof name[0] || (has_value && !*value)) break;
        wcopy(name, arg, n);
        name[n] = 0;

        /* Every option below either accepts no value or requires one; anything else is an error. */
        if (ci_eq(name, L"S") || ci_eq(name, L"SILENT") || ci_eq(name, L"QUIET") || ci_eq(name, L"VERYSILENT")) {
            if (has_value) break;
            opt.ui = LC_UI_SILENT;
        } else if (ci_eq(name, L"CONSOLE")) {
            if (has_value) break;
            opt.ui = LC_UI_CONSOLE;
        } else if (ci_eq(name, L"GUI")) {
            if (has_value) break;
            opt.ui = LC_UI_GUI;
        } else if (ci_eq(name, L"UNINSTALL")) {
            if (has_value) break;
            opt.action = LC_ACTION_UNINSTALL;
        } else if (ci_eq(name, L"REPAIR")) {
            if (has_value) break;
            opt.action = LC_ACTION_REPAIR;
        } else if (ci_eq(name, L"DIAGNOSE")) {
            if (has_value) break;
            opt.action = LC_ACTION_DIAGNOSE;
        } else if (ci_eq(name, L"SELFTEST")) {
            if (has_value) break;
            opt.action = LC_ACTION_SELFTEST;
        } else if (ci_eq(name, L"?") || ci_eq(name, L"HELP") || ci_eq(name, L"H")) {
            if (has_value) break;
            opt.action = LC_ACTION_HELP;
        } else if (ci_eq(name, L"EXTRACT")) {
            if (!has_value) break;
            opt.action = LC_ACTION_EXTRACT;
            opt.extract_dir = value;
        } else if (ci_eq(name, L"LOG")) {
            if (!has_value) break;
            opt.log_path = value;
        } else if (ci_eq(name, L"ALLOWDOWNGRADE")) {
            if (has_value) break;
            opt.options |= LC_OPT_ALLOW_DOWNGRADE;
        } else if (ci_eq(name, L"REQUIREBRAVE")) {
            if (has_value) break;
            opt.options |= LC_OPT_REQUIRE_BRAVE;
        } else if (ci_eq(name, L"NOSHORTCUTS")) {
            if (has_value) break;
            opt.options |= LC_OPT_NO_SHORTCUTS;
        } else if (ci_eq(name, L"REMOVEHISTORY")) {
            if (has_value) break;
            opt.options |= LC_OPT_REMOVE_HISTORY;
        } else if (ci_eq(name, L"ALLOWUNVERIFIED")) {
            if (has_value) break;
            opt.options |= LC_OPT_ALLOW_UNVERIFIED;
        } else {
            break;
        }
        continue;                                       /* recognised */
    }
    if (i < argc) {                                     /* left the loop on `break`: argv[i] is the culprit */
        usage_error(error, error_cap, why, argv[i]);
        return LC_E_USAGE;
    }
    *options = opt;
    return LC_OK;
}
