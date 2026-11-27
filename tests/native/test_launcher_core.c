/* Host tests for the portable launcher core. Compiled and run by tests/test_launcher_native.py:
 *
 *   cc -std=c11 -Wall -Wextra -Werror -include <generated vectors.h> launcher_core.c this_file.c
 *
 * SHA_VECTORS / PATTERN come from that generated header (digests computed by Python's hashlib).
 *
 * File modes, used by the same Python test to compare this parser with payload_container.py:
 *   test_launcher_core --emit <file>        write the standard container built here
 *   test_launcher_core --parse <file>...    print lc_parse's result code for each file
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "launcher_core.h"

static int checks = 0, failures = 0;

#define CHECK(cond) do { checks++; if (!(cond)) { failures++; printf("FAIL line %d: %s\n", __LINE__, #cond); } } while (0)
#define CHECK_MSG(cond, ...) do { checks++; if (!(cond)) { failures++; \
    printf("FAIL line %d: ", __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

/* ---------------------------------------------------------------- helpers ---- */
typedef struct { const char *name; size_t size; unsigned seed; int bootstrap; const unsigned char *data; } spec;

static void put16(unsigned char *p, uint16_t v) {
    p[0] = (unsigned char)v; p[1] = (unsigned char)(v >> 8);
}

static void put32(unsigned char *p, uint32_t v) {
    p[0] = (unsigned char)v; p[1] = (unsigned char)(v >> 8);
    p[2] = (unsigned char)(v >> 16); p[3] = (unsigned char)(v >> 24);
}

static void put64(unsigned char *p, uint64_t v) { put32(p, (uint32_t)v); put32(p + 4, (uint32_t)(v >> 32)); }

static uint64_t get64(const unsigned char *p) {
    return (uint64_t)p[0] | ((uint64_t)p[1] << 8) | ((uint64_t)p[2] << 16) | ((uint64_t)p[3] << 24)
         | ((uint64_t)p[4] << 32) | ((uint64_t)p[5] << 40) | ((uint64_t)p[6] << 48) | ((uint64_t)p[7] << 56);
}

/* The smallest header the reader accepts: MZ, PE signature, one optional header with 16 data
 * directories, of which only the Certificate Table entry (index 4) matters. */
#define PE_SIZE 512
#define PE_OPTIONAL 0x58
#define PE_DIRECTORIES(magic) ((magic) == 0x10b ? 96u : 112u)
#define PE_CERTIFICATE(magic) (PE_OPTIONAL + PE_DIRECTORIES(magic) + 4u * 8u)   /* data directory 4, 8 bytes each */

static const unsigned char MAGIC[8] = {'B', 'M', 'D', 'P', 'K', 'G', 0, 1};

static void make_pe(unsigned char *pe, uint16_t magic, uint32_t cert_offset, uint32_t cert_size) {
    uint32_t directories = PE_DIRECTORIES(magic);
    memset(pe, 0, PE_SIZE);
    pe[0] = 'M'; pe[1] = 'Z';
    put32(pe + 0x3c, 0x40);                                   /* e_lfanew */
    memcpy(pe + 0x40, "PE\0\0", 4);
    put16(pe + 0x44, 0x8664);                                 /* Machine */
    put16(pe + 0x46, 1);                                      /* NumberOfSections */
    put16(pe + 0x54, 240);                                    /* SizeOfOptionalHeader */
    put16(pe + 0x56, 0x22);                                   /* Characteristics */
    put16(pe + PE_OPTIONAL, magic);
    put32(pe + PE_OPTIONAL + directories - 4, 16);            /* NumberOfRvaAndSizes */
    put32(pe + PE_CERTIFICATE(magic), cert_offset);
    put32(pe + PE_CERTIFICATE(magic) + 4, cert_size);
}

static size_t footer_at(size_t end) { return end - LC_FOOTER_SIZE; }

static size_t index_at(const unsigned char *image, size_t end) {
    return (size_t)get64(image + footer_at(end) + 16);
}

static uint32_t entry_count(const unsigned char *image, size_t end) {
    return (uint32_t)image[footer_at(end) + 32] | ((uint32_t)image[footer_at(end) + 33] << 8)
         | ((uint32_t)image[footer_at(end) + 34] << 16) | ((uint32_t)image[footer_at(end) + 35] << 24);
}

static unsigned char *entry_at(unsigned char *image, size_t end, uint32_t index) {
    return image + index_at(image, end) + (size_t)index * LC_ENTRY_SIZE;
}

/* Recompute the footer's index hash after a test edited an entry, so that the entry-level
 * validation is exercised rather than being masked by the index hash check. */
static void refinish(unsigned char *image, size_t end) {
    unsigned char digest[32];
    size_t at = index_at(image, end);
    lc_sha256(image + at, (size_t)entry_count(image, end) * LC_ENTRY_SIZE, digest);
    memcpy(image + footer_at(end) + 40, digest, 32);
}

static void set_name(unsigned char *image, size_t end, uint32_t index, const char *name) {
    unsigned char *e = entry_at(image, end, index);
    memset(e, 0, LC_NAME_MAX);
    if (name) strcpy((char *)e, name);
}

/* [minimal PE header][entries on 16-byte boundaries][index][footer]; `out` must hold 2 MiB. */
static size_t build(unsigned char *out, const spec *specs, int count) {
    unsigned char index[LC_MAX_ENTRIES * LC_ENTRY_SIZE], digest[32];
    size_t pos = PE_SIZE, i, k;
    memset(index, 0, sizeof index);
    memset(out, 0, 2 * 1024 * 1024);
    make_pe(out, 0x20b, 0, 0);
    for (i = 0; i < (size_t)count; i++) {
        unsigned char *e = index + i * LC_ENTRY_SIZE;
        while (pos % 16) out[pos++] = 0;
        strcpy((char *)e, specs[i].name);
        put64(e + LC_NAME_MAX, (uint64_t)pos);
        put64(e + LC_NAME_MAX + 8, (uint64_t)specs[i].size);
        put32(e + LC_NAME_MAX + 16, specs[i].bootstrap ? LC_FLAG_BOOTSTRAP : 0);
        for (k = 0; k < specs[i].size; k++)
            out[pos + k] = specs[i].data ? specs[i].data[k] : (unsigned char)((k * 31 + specs[i].seed * 7) & 0xff);
        lc_sha256(out + pos, specs[i].size, digest);
        memcpy(e + LC_NAME_MAX + 24, digest, 32);
        pos += specs[i].size;
    }
    memcpy(out + pos, index, (size_t)count * LC_ENTRY_SIZE);
    lc_sha256(index, (size_t)count * LC_ENTRY_SIZE, digest);
    put64(out + pos + (size_t)count * LC_ENTRY_SIZE + 16, (uint64_t)pos);    /* index_offset */
    pos += (size_t)count * LC_ENTRY_SIZE;                 /* the footer follows the index */
    memcpy(out + pos, MAGIC, 8);
    put64(out + pos + 8, PE_SIZE);
    put64(out + pos + 24, (uint64_t)count * LC_ENTRY_SIZE);
    put32(out + pos + 32, (uint32_t)count);
    put32(out + pos + 36, 0);
    memcpy(out + pos + 40, digest, 32);
    return pos + LC_FOOTER_SIZE;
}

typedef struct { unsigned char *data; size_t size; int fail_at_call, calls; } mem;

static int mem_read(void *context, uint64_t at, void *buffer, size_t len) {
    mem *m = (mem *)context;
    if (at > m->size || len > m->size - (size_t)at) return 1;
    if (m->fail_at_call && ++m->calls == m->fail_at_call) return 1;
    memcpy(buffer, m->data + at, len);
    return 0;
}

static void source(lc_source *s, mem *m, unsigned char *data, size_t size) {
    m->data = data; m->size = size; m->fail_at_call = 0; m->calls = 0;
    s->read = mem_read; s->context = m; s->file_size = (uint64_t)size;
}

static void unhex(const char *hex, unsigned char out[32]) {
    int i;
    for (i = 0; i < 32; i++) {
        unsigned value = 0;
        if (sscanf(hex + i * 2, "%2x", &value) != 1) { out[i] = 0; continue; }
        out[i] = (unsigned char)value;
    }
}

static int hex_eq(const unsigned char *bytes, const char *hex) {
    unsigned char expected[32];
    unhex(hex, expected);
    return memcmp(bytes, expected, 32) == 0;
}

static unsigned char *image;                       /* allocated in main: the largest test needs 2 MiB */

/* the usual installer: three bootstrap files and one large tool archive */
static const spec SPECS[] = {{"Setup.ps1", 4000, 1, 1, NULL}, {"Application.zip", 90000, 2, 1, NULL},
                             {"python.zip", 3000, 3, 1, NULL}, {"ffmpeg", 1500000, 4, 0, NULL}};
#define SPEC_COUNT 4
/* the same with one byte more: its length is not a multiple of 8, so signing pads it */
static const spec ODD_SPECS[] = {{"Setup.ps1", 4000, 1, 1, NULL}, {"Application.zip", 90000, 2, 1, NULL},
                                 {"python.zip", 3000, 3, 1, NULL}, {"ffmpeg", 1500001, 4, 0, NULL}};

static size_t standard(void) { return build(image, SPECS, SPEC_COUNT); }

/* What signtool does to the file layout, without any cryptography: zeros up to a multiple of 8,
 * then a certificate table at the very end, announced in the header. The table deliberately
 * contains the container magic. */
static size_t sign(unsigned char *out, size_t size, size_t table_bytes) {
    size_t k;
    while (size % 8) out[size++] = 0;
    put32(out + PE_CERTIFICATE(0x20b), (uint32_t)size);
    put32(out + PE_CERTIFICATE(0x20b) + 4, (uint32_t)table_bytes);
    for (k = 0; k < table_bytes; k++) out[size + k] = (unsigned char)(k * 7 + 3);
    memcpy(out + size + table_bytes - 8, MAGIC, 8);
    return size + table_bytes;
}

struct sink_ctx { unsigned char *data; size_t len; int fail_at_chunk; int chunks; };

static int collect(void *context, const unsigned char *data, size_t len) {
    struct sink_ctx *sc = (struct sink_ctx *)context;
    sc->chunks++;
    if (sc->fail_at_chunk && sc->chunks == sc->fail_at_chunk) return 1;
    memcpy(sc->data + sc->len, data, len);
    sc->len += len;
    return 0;
}

/* ---------------------------------------------------------------- SHA-256 ---- */
static void test_sha256_vectors(void) {
    size_t i;
    unsigned char digest[32];
    for (i = 0; i < SHA_VECTOR_COUNT; i++) {
        lc_sha256(SHA_VECTORS[i].data, SHA_VECTORS[i].len, digest);
        CHECK_MSG(hex_eq(digest, SHA_VECTORS[i].hex), "SHA-256 of %lu bytes", (unsigned long)SHA_VECTORS[i].len);
    }
    lc_sha256("", 0, digest);
    CHECK(hex_eq(digest, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
    lc_sha256("abc", 3, digest);
    CHECK(hex_eq(digest, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
    {   /* FIPS 180-2 vector: one million 'a' */
        unsigned char *data = (unsigned char *)malloc(1000000);
        memset(data, 'a', 1000000);
        lc_sha256(data, 1000000, digest);
        free(data);
        CHECK(hex_eq(digest, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"));
    }
}

/* --------------------------------------------------------------- container ---- */
static void test_valid_container_parses(void) {
    lc_container c;
    lc_source s;
    mem m;
    uint32_t i;
    size_t size = standard();
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    CHECK(c.count == SPEC_COUNT && c.pe_size == PE_SIZE);
    CHECK(strcmp(c.entries[0].name, "Setup.ps1") == 0 && strcmp(c.entries[3].name, "ffmpeg") == 0);
    CHECK(c.entries[0].flags == LC_FLAG_BOOTSTRAP && c.entries[3].flags == 0);
    CHECK(c.entries[0].offset == PE_SIZE && c.entries[0].size == 4000);
    CHECK(c.entries[1].offset == 4512 && c.entries[2].offset == 94512 && c.entries[3].offset == 97520);
    CHECK(c.index_offset == 1597520 && size == 1598072);
    CHECK(c.index_offset + (size_t)c.count * LC_ENTRY_SIZE + LC_FOOTER_SIZE == size);
    for (i = 0; i < c.count; i++) {
        CHECK((c.entries[i].offset % 16) == 0 && c.entries[i].offset >= PE_SIZE);
        if (i) CHECK(c.entries[i].offset >= c.entries[i - 1].offset + c.entries[i - 1].size);
    }
    CHECK(lc_find(&c, "application.ZIP") == 1 && lc_find(&c, "FFMPEG") == 3);
    CHECK(lc_find(&c, "nope") < 0 && lc_find(&c, "") < 0);
}

static void test_signed_installer_is_recognised(void) {
    lc_container c;
    lc_source s;
    mem m;
    uint32_t i;
    size_t size = sign(image, standard(), 264);
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK && c.count == SPEC_COUNT);
    CHECK(c.entries[0].offset == PE_SIZE && c.entries[0].size == 4000 && c.index_offset == 1597520);
    for (i = 0; i < c.count; i++) CHECK(lc_verify_entry(&s, &c.entries[i], NULL, NULL) == LC_OK);
    image[size - 100] ^= 0xFF;                        /* inside the certificate table: not our data */
    CHECK(lc_parse(&s, &c) == LC_OK);
    image[c.entries[1].offset + 500] ^= 1;            /* inside a bootstrap file */
    CHECK(lc_verify_entry(&s, &c.entries[1], NULL, NULL) == LC_E_HASH);
    image[c.entries[1].offset + 500] ^= 1;

    /* A container whose length is not a multiple of 8: the signing tool pads it with zeros before
     * its table, and the footer is then seven bytes before that table. */
    {
        size_t plain = build(image, ODD_SPECS, SPEC_COUNT), k;
        CHECK(plain == 1598073 && plain % 8 == 1);
        size = sign(image, plain, 264);
        CHECK(size == plain + 7 + 264);
        source(&s, &m, image, size);
        CHECK(lc_parse(&s, &c) == LC_OK && c.count == SPEC_COUNT && c.index_offset == 1597521);
        CHECK(c.entries[3].offset == 97520 && c.entries[3].size == 1500001);
        for (i = 0; i < c.count; i++) CHECK(lc_verify_entry(&s, &c.entries[i], NULL, NULL) == LC_OK);
        image[plain + 6] = 1;                         /* something else than the padding zeros */
        CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
        image[plain + 6] = 0;
        put32(image + PE_CERTIFICATE(0x20b), 0);      /* not signed after all */
        put32(image + PE_CERTIFICATE(0x20b) + 4, 0);
        for (k = 1; k < 8; k++) {                     /* the padding zeros alone: harmless */
            source(&s, &m, image, plain + k);
            CHECK_MSG(lc_parse(&s, &c) == LC_OK && c.entries[3].size == 1500001, "%lu zeros appended", (unsigned long)k);
        }
        source(&s, &m, image, plain + 8);             /* too many for an 8-byte alignment */
        CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
        image[plain] = 0x41;
        source(&s, &m, image, plain + 1);             /* anything else after the footer */
        CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
        source(&s, &m, image, plain - 1);             /* truncated inside the footer */
        CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    }
    size = sign(image, standard(), 264);
    source(&s, &m, image, size);
    put32(image + PE_CERTIFICATE(0x20b), 300);        /* a table in the middle of the file: no footer before it */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    put32(image + PE_CERTIFICATE(0x20b), (uint32_t)size);            /* the table would start past the end */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put32(image + PE_CERTIFICATE(0x20b), (uint32_t)size + 8);
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put32(image + PE_CERTIFICATE(0x20b), 0);          /* the table is said to cover the whole file */
    put32(image + PE_CERTIFICATE(0x20b) + 4, (uint32_t)size);
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    put32(image + PE_CERTIFICATE(0x20b), (uint32_t)(size - 264));
    put32(image + PE_CERTIFICATE(0x20b) + 4, 0);      /* size 0: the file counts as unsigned */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    put32(image + PE_CERTIFICATE(0x20b) + 4, 264);
    put32(image + PE_OPTIONAL + 108, 4);              /* fewer than 5 data directories: no certificate entry */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    put32(image + PE_OPTIONAL + 108, 16);
    CHECK(lc_parse(&s, &c) == LC_OK);

    /* the same with a 32-bit optional header: its data directories start 16 bytes earlier */
    size = standard();
    put16(image + PE_OPTIONAL, 0x10b);
    put32(image + PE_OPTIONAL + 92, 16);
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    size = sign(image, size, 16);
    put16(image + PE_OPTIONAL, 0x10b);
    put32(image + PE_OPTIONAL + 92, 16);
    put32(image + PE_CERTIFICATE(0x10b), (uint32_t)(size - 16));
    put32(image + PE_CERTIFICATE(0x10b) + 4, 16);
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK && c.count == SPEC_COUNT);
    for (i = 0; i < c.count; i++) CHECK(lc_verify_entry(&s, &c.entries[i], NULL, NULL) == LC_OK);
}

static void test_the_header_must_be_a_windows_executable(void) {
    lc_container c;
    lc_source s;
    mem m;
    size_t size = standard();
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    image[1] = 'X';  CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);  image[1] = 'Z';
    image[0x43] = 'X';  CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);  image[0x43] = 0;
    put32(image + 0x3c, 0xFFFFFFF0u);  CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);  put32(image + 0x3c, 0x40);
    put32(image + 0x3c, 0);            CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);  put32(image + 0x3c, 0x40);
    put16(image + PE_OPTIONAL, 0x1234);  CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);  put16(image + PE_OPTIONAL, 0x20b);
    source(&s, &m, image, 100);                        /* the header itself is cut short */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    source(&s, &m, image, PE_OPTIONAL + PE_DIRECTORIES(0x20b) + 4);   /* the directory array is cut short */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
}

static void test_entries_verify_and_tampering_is_detected(void) {
    lc_container c;
    lc_source s;
    mem m;
    size_t size = standard(), i;
    static const size_t offsets[] = {0, 1, 65535, 65536, 65537, 131071, 131072, 999999, 1499998, 1499999};
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    for (i = 0; i < c.count; i++) CHECK(lc_verify_entry(&s, &c.entries[i], NULL, NULL) == LC_OK);
    for (i = 0; i < sizeof offsets / sizeof offsets[0]; i++) {
        image[c.entries[3].offset + offsets[i]] ^= 0x80;
        CHECK_MSG(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_E_HASH, "flip at %lu", (unsigned long)offsets[i]);
        image[c.entries[3].offset + offsets[i]] ^= 0x80;
    }
    m.fail_at_call = 1;
    CHECK(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_E_IO);
    m.calls = 0; m.fail_at_call = 3;                /* fail in the middle of the stream */
    CHECK(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_E_IO);
    m.calls = 0; m.fail_at_call = 23;               /* the last chunk */
    CHECK(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_E_IO);
    m.fail_at_call = 0;
    CHECK(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_OK);
}

static void test_entry_content_is_streamed_byte_for_byte(void) {
    lc_container c;
    lc_source s;
    mem m;
    struct sink_ctx sc;
    unsigned char digest[32];
    size_t size = standard();
    unsigned char *sink_buffer = (unsigned char *)malloc(PATTERN_SIZE + 1500000);   /* the archive, then the pattern */
    spec pattern = {"pattern", PATTERN_SIZE, 0, 0, PATTERN};
    if (!sink_buffer) { CHECK_MSG(0, "out of memory"); return; }
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    sc.data = sink_buffer; sc.len = 0; sc.fail_at_chunk = 0; sc.chunks = 0;
    CHECK(lc_verify_entry(&s, &c.entries[3], collect, &sc) == LC_OK);
    CHECK(sc.len == 1500000 && sc.chunks == 23);                  /* 1500000 / 65536 -> 23 chunks */
    CHECK(memcmp(sink_buffer, image + c.entries[3].offset, 1500000) == 0);
    sc.len = 0; sc.fail_at_chunk = 2; sc.chunks = 0;
    CHECK(lc_verify_entry(&s, &c.entries[3], collect, &sc) == LC_E_WRITE);   /* the destination gave up */
    sc.fail_at_chunk = 0;

    /* the streamed digest (16 full 64 KiB chunks) equals the one-shot digest */
    size = build(image, &pattern, 1);
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK && c.entries[0].size == PATTERN_SIZE);
    sc.len = 0; sc.chunks = 0;
    CHECK(lc_verify_entry(&s, &c.entries[0], collect, &sc) == LC_OK);
    CHECK(sc.len == PATTERN_SIZE && sc.chunks == 16 && memcmp(sink_buffer, PATTERN, PATTERN_SIZE) == 0);
    free(sink_buffer);
    lc_sha256(PATTERN, PATTERN_SIZE, digest);
    CHECK(hex_eq(digest, SHA_VECTORS[SHA_VECTOR_COUNT - 1].hex));
    image[c.entries[0].offset + PATTERN_SIZE - 1] ^= 1;
    CHECK(lc_verify_entry(&s, &c.entries[0], NULL, NULL) == LC_E_HASH);
}

static void test_damaged_files_are_rejected(void) {
    lc_container c;
    lc_source s;
    mem m;
    size_t size = standard(), k, at;
    static const size_t cuts[] = {PE_SIZE, 4600, 94511, 94512, 97520, 1597520, 1598000, 1598071};
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);

    memset(image, 0x41, 5000);                                  /* not an installer at all */
    source(&s, &m, image, 5000);
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    source(&s, &m, image, 10);
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    source(&s, &m, image, 71);
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    source(&s, &m, image, 0);
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);

    size = standard();
    source(&s, &m, image, size - 1);                            /* one byte missing */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    memset(image + size, 0, 16);                                /* zeros up to the 8-byte boundary: padding */
    source(&s, &m, image, size + 7);
    CHECK(lc_parse(&s, &c) == LC_OK && c.count == SPEC_COUNT);
    source(&s, &m, image, size + 8);                            /* eight bytes: not padding any more */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    image[size] = 0x41;                                         /* anything else after the footer */
    source(&s, &m, image, size + 1);
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    source(&s, &m, image, size + 4096);                         /* a lot appended */
    CHECK(lc_parse(&s, &c) == LC_E_NOT_INSTALLER);
    for (k = 0; k < sizeof cuts / sizeof cuts[0]; k++) {        /* cut short anywhere */
        source(&s, &m, image, cuts[k]);
        CHECK_MSG(lc_parse(&s, &c) == LC_E_NOT_INSTALLER, "cut at %lu", (unsigned long)cuts[k]);
    }

    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    at = c.index_offset;                                        /* keep it: a failed parse zeroes the container */
    for (k = 0; k < LC_FOOTER_SIZE; k++) {                      /* every footer byte matters */
        image[size - LC_FOOTER_SIZE + k] ^= 0x5A;
        CHECK_MSG(lc_parse(&s, &c) == (k < 8 ? LC_E_NOT_INSTALLER : LC_E_DAMAGED),
                  "footer byte %lu accepted", (unsigned long)k);
        image[size - LC_FOOTER_SIZE + k] ^= 0x5A;
    }
    for (k = at; k < size - LC_FOOTER_SIZE; k++) {              /* every index byte is protected by the hash */
        image[k] ^= 0x5A;
        if (lc_parse(&s, &c) != LC_E_DAMAGED) { CHECK_MSG(0, "index byte %lu accepted", (unsigned long)(k - at)); break; }
        image[k] ^= 0x5A;
    }
    CHECK(k == size - LC_FOOTER_SIZE);
    CHECK(lc_parse(&s, &c) == LC_OK);
}

static void test_footer_fields_are_validated(void) {
    lc_container c;
    lc_source s;
    mem m;
    size_t size = standard(), f, index_offset;
    unsigned char index_hash[32];
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    index_offset = c.index_offset;
    f = footer_at(size);
    memcpy(index_hash, image + f + 40, 32);

    put32(image + f + 32, 0);  refinish(image, size);           /* no entries at all */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put32(image + f + 32, LC_MAX_ENTRIES + 1);                  /* more than the format allows */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put32(image + f + 32, SPEC_COUNT);
    memcpy(image + f + 40, index_hash, 32);                     /* the real index hash again */
    put64(image + f + 24, (uint64_t)SPEC_COUNT * LC_ENTRY_SIZE + 1);   /* size does not match the count */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put64(image + f + 24, (uint64_t)SPEC_COUNT * LC_ENTRY_SIZE);
    put64(image + f + 16, size - LC_FOOTER_SIZE + 1);           /* the index would start after the data */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put64(image + f + 16, index_offset + 8);                    /* it would not end at the footer */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put64(image + f + 16, index_offset - 8);
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put64(image + f + 16, index_offset);
    put64(image + f + 8, index_offset + 1);                     /* an image larger than the data area */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put64(image + f + 8, 0);                                    /* no image at all */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put64(image + f + 8, PE_SIZE + 1);                          /* an entry would start inside the image */
    CHECK(lc_parse(&s, &c) == LC_E_DAMAGED);
    put64(image + f + 8, PE_SIZE - 1);                          /* the exact image size is the signer's business */
    CHECK(lc_parse(&s, &c) == LC_OK);
    put64(image + f + 8, PE_SIZE);
    CHECK(lc_parse(&s, &c) == LC_OK);
}

static void test_entry_fields_are_validated_with_a_consistent_hash(void) {
    lc_container c;
    lc_source s;
    mem m;
    size_t size = standard(), i;
    unsigned char *second;
    uint64_t offset, length, index_offset;
    char name[64];
    static const char *bad[] = {"../x", "a/b", "a\\b", "C:x", ".hidden", " leading", "-x", "_x", "x.", "x ",
                                "caf\xc3\xa9", "a\tb", "a*b"};
    source(&s, &m, image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    second = entry_at(image, size, 1);
    offset = get64(second + LC_NAME_MAX);
    length = get64(second + LC_NAME_MAX + 8);
    index_offset = c.index_offset;
#define MUST_BE_DAMAGED() do { refinish(image, size); CHECK(lc_parse(&s, &c) == LC_E_DAMAGED); } while (0)

    put64(second + LC_NAME_MAX, 1);                                 MUST_BE_DAMAGED();   /* inside the PE image */
    put64(second + LC_NAME_MAX, PE_SIZE - 1);                       MUST_BE_DAMAGED();
    put64(second + LC_NAME_MAX, offset);
    put64(second + LC_NAME_MAX + 8, index_offset);                  MUST_BE_DAMAGED();   /* past the data area */
    put64(second + LC_NAME_MAX + 8, UINT64_MAX);                    MUST_BE_DAMAGED();   /* offset + size would overflow */
    put64(second + LC_NAME_MAX + 8, index_offset - offset + 1);     MUST_BE_DAMAGED();
    put64(second + LC_NAME_MAX + 8, length);
    put32(second + LC_NAME_MAX + 16, LC_FLAG_BOOTSTRAP | 2u);       MUST_BE_DAMAGED();   /* an unknown flag */
    put32(second + LC_NAME_MAX + 16, LC_FLAG_BOOTSTRAP);
    refinish(image, size);                                           /* any entry may be a bootstrap file */
    CHECK(lc_parse(&s, &c) == LC_OK && c.entries[1].flags == LC_FLAG_BOOTSTRAP);
    put32(second + LC_NAME_MAX + 16, 0);
    put32(second + LC_NAME_MAX + 20, 7);                             MUST_BE_DAMAGED();   /* reserved */
    put32(second + LC_NAME_MAX + 20, 0);

    for (i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        set_name(image, size, 1, bad[i]);
        CHECK_MSG(lc_parse(&s, &c) == LC_E_DAMAGED, "an unrefreshed index hash for %s", bad[i]);
    }
    set_name(image, size, 1, "Application.zip");
    refinish(image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);
    for (i = 0; i < sizeof bad / sizeof bad[0]; i++) {              /* now with the hash made consistent */
        set_name(image, size, 1, bad[i]);
        MUST_BE_DAMAGED();
    }
    memset(name, 'a', 48); name[48] = 0;                            /* the longest accepted name */
    set_name(image, size, 1, name);
    refinish(image, size);
    CHECK(lc_parse(&s, &c) == LC_OK && strcmp(c.entries[1].name, name) == 0);
    name[48] = 'a'; name[49] = 0;                                   /* one character more */
    set_name(image, size, 1, name);
    MUST_BE_DAMAGED();
    set_name(image, size, 1, NULL);                                 /* empty */
    MUST_BE_DAMAGED();
    memset(second, 'a', LC_NAME_MAX);                               /* not terminated */
    MUST_BE_DAMAGED();
    set_name(image, size, 1, "ok.txt");
    second[20] = 'x';                                               /* bytes after the terminator */
    MUST_BE_DAMAGED();
    set_name(image, size, 1, "setup.PS1");                          /* a duplicate, ignoring case */
    MUST_BE_DAMAGED();
    set_name(image, size, 1, "Application.zip");
    refinish(image, size);
    CHECK(lc_parse(&s, &c) == LC_OK && lc_find(&c, "application.zip") == 1);
    second[LC_NAME_MAX + 24] ^= 1;                                  /* the entry's own digest */
    refinish(image, size);
    CHECK(lc_parse(&s, &c) == LC_OK);                               /* the structure is fine ... */
    CHECK(lc_verify_entry(&s, &c.entries[1], NULL, NULL) == LC_E_HASH);   /* ... the content is not */
#undef MUST_BE_DAMAGED

    {   /* 64 entries are fine */
        spec many[LC_MAX_ENTRIES];
        static char names[LC_MAX_ENTRIES][8];
        int n;
        for (n = 0; n < LC_MAX_ENTRIES; n++) {
            sprintf(names[n], "e%d", n);
            many[n].name = names[n]; many[n].size = (size_t)n * 16; many[n].seed = 1;
            many[n].bootstrap = 0; many[n].data = NULL;
        }
        size = build(image, many, LC_MAX_ENTRIES);
        source(&s, &m, image, size);
        CHECK(lc_parse(&s, &c) == LC_OK && c.count == LC_MAX_ENTRIES);
        CHECK(lc_find(&c, "e63") == 63 && lc_find(&c, "e7") == 7 && lc_find(&c, "e") < 0);
        for (n = 0; n < LC_MAX_ENTRIES; n++) CHECK(lc_verify_entry(&s, &c.entries[n], NULL, NULL) == LC_OK);
    }
}

static void test_reader_failures_are_reported(void) {
    lc_container c;
    lc_source s;
    mem m;
    size_t size = standard();
    int call;
    source(&s, &m, image, size);
    for (call = 1; call <= 3; call++) {                 /* header, footer, index */
        m.calls = 0; m.fail_at_call = call;
        CHECK_MSG(lc_parse(&s, &c) == LC_E_IO, "read %d", call);
    }
    m.fail_at_call = 0;
    CHECK(lc_parse(&s, &c) == LC_OK);
    for (call = 1; call <= 2; call++) {                 /* the tool archive takes 23 reads */
        m.calls = 0; m.fail_at_call = call;
        CHECK(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_E_IO);
    }
    m.calls = 0; m.fail_at_call = 23;
    CHECK(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_E_IO);
    m.fail_at_call = 0;
    CHECK(lc_verify_entry(&s, &c.entries[3], NULL, NULL) == LC_OK);
    for (call = LC_OK; call <= LC_E_ARG; call++) {
        CHECK(lc_error_text(call)[0] != 0);
        if (call) CHECK(strcmp(lc_error_text(call), lc_error_text(call - 1)) != 0 && strcmp(lc_error_text(call), "unknown error") != 0);
    }
    CHECK(strcmp(lc_error_text(99), "unknown error") == 0);
}

/* ---------------------------------------------------------------- command ---- */
static int parse(wchar_t **argv, int argc, lc_options *o) {
    wchar_t error[256];
    int r = lc_parse_args(argc, argv, o, error, 256);
    if (r != LC_OK) CHECK_MSG(error[0] != 0, "a usage error must leave a message");
    return r;
}

static int contains(const wchar_t *text, const wchar_t *part) {
    size_t n = lc_wlen(part), i;
    for (i = 0; text[i]; i++) {
        size_t k;
        int same = 1;
        for (k = 0; k < n; k++) if (!text[i + k] || text[i + k] != part[k]) { same = 0; break; }
        if (same) return 1;
    }
    return 0;
}

static void test_options(void) {
    lc_options o;
    wchar_t error[256];
    static const struct { int argc; int ui; int action; unsigned options; } cases[] = {
        {1, LC_UI_GUI, LC_ACTION_INSTALL, 0},
        {2, LC_UI_SILENT, LC_ACTION_INSTALL, 0},        /* /S */
        {2, LC_UI_SILENT, LC_ACTION_INSTALL, 0},        /* /Silent */
        {2, LC_UI_SILENT, LC_ACTION_INSTALL, 0},        /* -QUIET */
        {2, LC_UI_SILENT, LC_ACTION_INSTALL, 0},        /* /VERYSILENT */
        {2, LC_UI_CONSOLE, LC_ACTION_INSTALL, 0},       /* /CONSOLE */
        {3, LC_UI_GUI, LC_ACTION_INSTALL, 0},           /* /S /GUI: the last interface choice wins */
        {2, LC_UI_GUI, LC_ACTION_UNINSTALL, 0},         /* /UNINSTALL */
        {3, LC_UI_SILENT, LC_ACTION_UNINSTALL, 0},      /* /S /uninstall */
        {2, LC_UI_GUI, LC_ACTION_REPAIR, 0},            /* /REPAIR */
        {2, LC_UI_GUI, LC_ACTION_DIAGNOSE, 0},          /* /DIAGNOSE */
        {2, LC_UI_GUI, LC_ACTION_SELFTEST, 0},          /* /SELFTEST */
        {2, LC_UI_GUI, LC_ACTION_HELP, 0},              /* /? */
        {2, LC_UI_GUI, LC_ACTION_HELP, 0},              /* /HELP */
        {2, LC_UI_GUI, LC_ACTION_HELP, 0},              /* -help */
        {2, LC_UI_GUI, LC_ACTION_HELP, 0},              /* /H */
        {4, LC_UI_SILENT, LC_ACTION_UNINSTALL, LC_OPT_REMOVE_HISTORY},
        {6, LC_UI_GUI, LC_ACTION_INSTALL, LC_OPT_ALLOW_DOWNGRADE | LC_OPT_REQUIRE_BRAVE | LC_OPT_NO_SHORTCUTS
                                                | LC_OPT_REMOVE_HISTORY | LC_OPT_ALLOW_UNVERIFIED},
    };
    static wchar_t *lines[][8] = {
        {L"setup.exe", NULL},
        {L"setup.exe", L"/S", NULL},
        {L"setup.exe", L"/Silent", NULL},
        {L"setup.exe", L"-QUIET", NULL},
        {L"setup.exe", L"/VERYSILENT", NULL},
        {L"setup.exe", L"/CONSOLE", NULL},
        {L"setup.exe", L"/S", L"/GUI", NULL},
        {L"setup.exe", L"/UNINSTALL", NULL},
        {L"setup.exe", L"/S", L"/uninstall", NULL},
        {L"setup.exe", L"/REPAIR", NULL},
        {L"setup.exe", L"/DIAGNOSE", NULL},
        {L"setup.exe", L"/SELFTEST", NULL},
        {L"setup.exe", L"/?", NULL},
        {L"setup.exe", L"/HELP", NULL},
        {L"setup.exe", L"-help", NULL},
        {L"setup.exe", L"/H", NULL},
        {L"setup.exe", L"/S", L"/UNINSTALL", L"/REMOVEHISTORY", NULL},
        {L"setup.exe", L"/ALLOWDOWNGRADE", L"/REQUIREBRAVE", L"/NOSHORTCUTS", L"/REMOVEHISTORY", L"/ALLOWUNVERIFIED", NULL},
    };
    size_t i;
    CHECK(sizeof lines / sizeof lines[0] == sizeof cases / sizeof cases[0]);
    for (i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        memset(&o, 0xEE, sizeof o);
        CHECK(parse(lines[i], cases[i].argc, &o) == LC_OK);
        CHECK_MSG(o.ui == cases[i].ui && o.action == cases[i].action && o.options == cases[i].options, "line %lu", (unsigned long)i);
    }
    {
        wchar_t *argv[] = {L"setup.exe", L"/EXTRACT=C:\\Temp Folder", L"/LOG=D:\\a b\\log.txt", NULL};
        memset(&o, 0xEE, sizeof o);
        CHECK(lc_parse_args(3, argv, &o, error, 256) == LC_OK);
        CHECK(o.action == LC_ACTION_EXTRACT && o.extract_dir == argv[1] + 9 && o.log_path == argv[2] + 5);
        CHECK(lc_wlen(o.extract_dir) == 14 && lc_wlen(o.log_path) == 14);
        CHECK(wcscmp(o.extract_dir, L"C:\\Temp Folder") == 0 && wcscmp(o.log_path, L"D:\\a b\\log.txt") == 0);
    }
    {
        wchar_t *argv[] = {L"setup.exe", L"/S", L"setup.exe.bak", NULL};        /* a stray file name */
        CHECK(lc_parse_args(3, argv, &o, error, 256) == LC_E_USAGE);
        CHECK(contains(error, L"options only") && contains(error, L"setup.exe.bak"));
    }
    {
        static const wchar_t *bad[] = {L"/FOO", L"/INSTALL", L"/EXTRACT", L"/EXTRACT=", L"/LOG", L"/LOG=", L"/S=1", L"/HELP=x",
                                       L"/", L"-", L"/UNINSTALL=YES", L"/SILENT /S", L"//S", L"/S ", L"", L"/ALLOWDOWNGRADE=1"};
        size_t k;
        for (k = 0; k < sizeof bad / sizeof bad[0]; k++) {
            wchar_t *argv[] = {L"setup.exe", (wchar_t *)bad[k], NULL};
            CHECK_MSG(lc_parse_args(2, argv, &o, error, 256) == LC_E_USAGE, "option %ls accepted", bad[k]);
        }
        CHECK(contains(error, L"Unknown or malformed option: /ALLOWDOWNGRADE=1"));
        {
            wchar_t *argv[] = {L"setup.exe", L"/S", L"/LOG=C:\\x", L"/SILENT", L"/UNINSTALL", L"/REPAIR", L"/SELFTEST", L"/DIAGNOSE", L"/EXTRACT=C:\\x", NULL};
            CHECK(lc_parse_args(9, argv, &o, error, 256) == LC_OK);        /* the last action wins */
            CHECK(o.action == LC_ACTION_EXTRACT && o.ui == LC_UI_SILENT && o.log_path != NULL);
        }
    }
    {
        wchar_t *argv[] = {L"setup.exe", L"/S", NULL, L"/UNINSTALL", NULL};     /* a NULL entry is skipped */
        memset(&o, 0xEE, sizeof o);
        CHECK(lc_parse_args(4, argv, &o, error, 256) == LC_OK && o.ui == LC_UI_SILENT && o.action == LC_ACTION_UNINSTALL);
        CHECK(lc_parse_args(1, argv, &o, NULL, 0) == LC_OK);                   /* no error buffer: still fine */
        CHECK(lc_parse_args(2, argv, &o, NULL, 0) == LC_OK);
        CHECK(lc_wcstr(L"abc=d", L'=') == 3 && lc_wcstr(L"abc", L'=') < 0 && lc_wcstr(L"", L'=') < 0);
        CHECK(lc_wlen(L"") == 0 && lc_wlen(L"abc") == 3);
    }
    {
        /* an absurd option name cannot overflow the message buffer */
        static wchar_t huge[500];
        wchar_t *argv[] = {L"setup.exe", huge, NULL};
        size_t k, n = 0;
        huge[0] = L'/';
        for (k = 1; k < 499; k++) huge[k] = L'Z';
        huge[499] = 0;
        memset(error, 0x55, sizeof error);
        CHECK(lc_parse_args(2, argv, &o, error, 16) == LC_E_USAGE);
        CHECK(error[15] == 0 && lc_wlen(error) == 15);                 /* as much as fits, always terminated */
        CHECK(lc_wcstr(error, 0x5555) < 0);
        CHECK(lc_parse_args(2, argv, &o, error, 256) == LC_E_USAGE);
        CHECK(lc_wlen(error) == 29 + 64 + 3);                          /* the name is cut, with an ellipsis */
        CHECK(contains(error, L"Unknown or malformed option: /ZZZ") && contains(error, L"ZZZ..."));
        for (k = 0; error[k]; k++) n = k;
        CHECK(n + 1 == lc_wlen(error));
    }
    {
        static wchar_t garbage[100];
        wchar_t *argv[] = {L"setup.exe", garbage, NULL};
        size_t k;
        for (k = 0; k < 99; k++) garbage[k] = k % 2 ? L'\x01' : L'\n';
        garbage[99] = 0;
        CHECK(lc_parse_args(2, argv, &o, error, 256) == LC_E_USAGE);
        for (k = 0; error[k]; k++) CHECK_MSG((unsigned)error[k] >= 0x20, "control character at %lu", (unsigned long)k);
        CHECK(contains(error, L"Unexpected: ?????"));
    }
}

static void test_command_line_building(void) {
    wchar_t buffer[64];
    size_t length = 0, k;
    static wchar_t long_text[100];
    buffer[0] = 0;
    CHECK(lc_append_text(buffer, 64, &length, L"powershell.exe") == LC_OK && length == 14);
    CHECK(lc_append_quoted(buffer, 64, &length, L"C:\\Program Files\\x.ps1") == LC_OK);
    CHECK(lc_append_quoted(buffer, 64, &length, L"-NoProfile") == LC_OK);
    CHECK(wcscmp(buffer, L"powershell.exe \"C:\\Program Files\\x.ps1\" \"-NoProfile\"") == 0);
    length = 0; buffer[0] = 0;
    CHECK(lc_append_quoted(buffer, 64, &length, L"C:\\Windows\\powershell.exe") == LC_OK
          && length == 27 && buffer[0] == L'"');                            /* the program is the first word */
    length = 0; buffer[0] = 0;
    CHECK(lc_append_text(buffer, 64, &length, L"\"a\\\"b\"") == LC_OK);       /* raw text: backslashes are not special */
    CHECK(lc_wcstr(buffer, L'"') == 0 && lc_wlen(buffer) == 6);
    CHECK(lc_append_text(buffer, 64, &length, L"") == LC_OK && lc_wlen(buffer) == 6);

    length = 0; buffer[0] = 0;
    CHECK(lc_append_quoted(buffer, 64, &length, L"a\"b") == LC_E_ARG);        /* never try to escape a quote */
    CHECK(lc_append_quoted(buffer, 64, &length, L"a\nb") == LC_E_ARG);
    CHECK(lc_append_quoted(buffer, 64, &length, L"a\tb") == LC_E_ARG);
    CHECK(length == 0 && buffer[0] == 0);
    for (k = 0; k < 99; k++) long_text[k] = L'x';
    long_text[99] = 0;
    CHECK(lc_append_quoted(buffer, 64, &length, long_text) == LC_E_ARG);      /* does not fit: nothing is written */
    CHECK(length == 0 && buffer[0] == 0);
    CHECK(lc_append_text(buffer, 64, &length, long_text) == LC_E_ARG);
    CHECK(lc_append_quoted(buffer, 64, &length, L"") == LC_OK);               /* an empty argument is legal */
    CHECK(wcscmp(buffer, L"\"\"") == 0 && length == 2);
    CHECK(lc_append_quoted(buffer, 9, &length, L"abcd") == LC_E_ARG);         /* 2 + 1 + 6 characters + terminator */
    CHECK(length == 2);
    CHECK(lc_append_quoted(buffer, 10, &length, L"abcd") == LC_OK);
    CHECK(wcscmp(buffer, L"\"\" \"abcd\"") == 0 && length == 9);
    CHECK(lc_append_text(buffer, 10, &length, L"x") == LC_E_ARG);
    CHECK(lc_append_text(buffer, 11, &length, L"x") == LC_OK && lc_wlen(buffer) == 10);
}

static void test_names(void) {
    static const struct { const char *name; int valid; } names[] = {
        {"a", 1}, {"Setup.ps1", 1}, {"python-3.12.10-embed-amd64.zip", 1}, {"Application.zip", 1},
        {"a b", 1}, {"x_1-2.3", 1}, {"payload.json", 1}, {"9", 1},
        {"", 0}, {".a", 0}, {"-a", 0}, {"_a", 0}, {" a", 0}, {"a/b", 0}, {"a\\b", 0}, {"a:b", 0},
        {"a*b", 0}, {"a?b", 0}, {"a.", 0}, {"a ", 0}, {"a\x01", 0}, {"caf\xc3\xa9", 0}, {"ab.", 0}, {"a\x7f", 0},
        {"CON", 0}, {"nul", 0}, {"Aux.txt", 0}, {"com1", 0}, {"LPT9.log", 0}, {"CON .txt", 0}, {"conin$", 0}, {"conout$", 0},
        {"console", 1}, {"COM10", 1}, {"com1.txt.bak", 0}, {"nul.dat", 0}, {"auxiliary", 1}, {"prune", 1}};
    char limit[50];
    size_t i;
    memset(limit, 'a', 48); limit[48] = 0;
    for (i = 0; i < sizeof names / sizeof names[0]; i++)
        CHECK_MSG(lc_valid_name(names[i].name) == names[i].valid, "name %s", names[i].name);
    CHECK(lc_valid_name(limit) == 1);                       /* the longest accepted name */
    limit[48] = 'a'; limit[49] = 0;
    CHECK(lc_valid_name(limit) == 0);                       /* one character more */
}

/* ---------------------------------------------------------- file modes ---- */
static int file_read(void *context, uint64_t at, void *buffer, size_t len) {
    FILE *f = (FILE *)context;
    return fseek(f, (long)at, SEEK_SET) != 0 || fread(buffer, 1, len, f) != len;
}

static int parse_files(int argc, char **argv) {             /* --parse <file>...: one result code per line */
    int k;
    for (k = 2; k < argc; k++) {
        lc_source s;
        lc_container c;
        FILE *f = fopen(argv[k], "rb");
        if (!f || fseek(f, 0, SEEK_END) != 0) {
            printf("open-failed\n");
            if (f) fclose(f);
            continue;
        }
        s.file_size = (uint64_t)ftell(f);
        s.read = file_read;
        s.context = f;
        printf("%d\n", lc_parse(&s, &c));
        fclose(f);
    }
    return 0;
}

int main(int argc, char **argv) {
    size_t i, size;
    if (argc > 2 && !strcmp(argv[1], "--parse")) return parse_files(argc, argv);
    image = (unsigned char *)malloc(3 * 1024 * 1024);
    if (!image) return 2;
    if (argc == 3 && !strcmp(argv[1], "--emit")) {          /* write the standard container built here */
        FILE *f = fopen(argv[2], "wb");
        if (!f) return 2;
        size = standard();
        if (fwrite(image, 1, size, f) != size) { fclose(f); return 2; }
        fclose(f);
        free(image);
        return 0;
    }
    for (i = 0; i < PATTERN_SIZE; i++) PATTERN[i] = (unsigned char)(i % 251);
    test_sha256_vectors();
    test_valid_container_parses();
    test_signed_installer_is_recognised();
    test_the_header_must_be_a_windows_executable();
    test_entries_verify_and_tampering_is_detected();
    test_entry_content_is_streamed_byte_for_byte();
    test_damaged_files_are_rejected();
    test_footer_fields_are_validated();
    test_entry_fields_are_validated_with_a_consistent_hash();
    test_reader_failures_are_reported();
    test_options();
    test_command_line_building();
    test_names();
    printf("%d checks passed, %d failed\n", checks, failures);
    free(image);
    return failures ? 1 : 0;
}
