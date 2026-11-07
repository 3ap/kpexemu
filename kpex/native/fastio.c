/* Fast paths for the busiest device ports, called by Unicorn directly
 * (without going through Python) on MMIO accesses.
 *
 * Calling Python from Unicorn costs about 1.6 us per access, and the firmware
 * makes millions of them: every LCD pixel is a 16-bit write, every disk word
 * a 16-bit read. Here those accesses are handled in C:
 *
 * wlog_*     - writes are queued and handed to Python in bulk (flush), in
 *              their original order. Fits a device whose writes have no
 *              immediate effect the CPU could notice (the LCD). Reads go to
 *              Python after the queue has been flushed.
 * rstream_*  - reads of the data port come from a buffer prepared by Python
 *              (the ATA sector data). The last word of the buffer, and every
 *              other register, is left to Python so it can update its state.
 *
 * Build: cc -O2 -shared -fPIC -o libfastio.so fastio.c (kpex/native/__init__.py
 * does this automatically).
 */

#include <stdint.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

typedef uint64_t (*read_fn)(void *uc, uint64_t off, unsigned size, void *ud);
typedef void (*write_fn)(void *uc, uint64_t off, unsigned size, uint64_t val, void *ud);

/* --- queued writes ---------------------------------------------------- */

typedef struct {
    uint32_t *entries;      /* pairs: (size << 24) | offset, value */
    uint32_t len, cap;      /* in pairs */
    void (*flush)(void);    /* Python: replay the entries and set len = 0 */
    read_fn slow_read;      /* Python: device read */
} wlog_t;

EXPORT uint64_t wlog_read(void *uc, uint64_t off, unsigned size, void *ud)
{
    wlog_t *w = ud;
    if (w->len)
        w->flush();
    return w->slow_read(uc, off, size, 0);
}

EXPORT void wlog_write(void *uc, uint64_t off, unsigned size, uint64_t val, void *ud)
{
    wlog_t *w = ud;
    uint32_t *e = w->entries + 2 * w->len;
    e[0] = (size << 24) | (uint32_t)off;
    e[1] = (uint32_t)val;
    if (++w->len == w->cap)
        w->flush();
}

/* --- streamed reads --------------------------------------------------- */

typedef struct {
    const uint8_t *data;
    uint32_t pos, end;      /* C serves words while pos + 2 < end */
    uint32_t port_mask;     /* offset & port_mask == port: the data port */
    uint32_t port;
    read_fn slow_read;      /* Python: everything else */
    write_fn slow_write;
} rstream_t;

EXPORT uint64_t rstream_read(void *uc, uint64_t off, unsigned size, void *ud)
{
    rstream_t *s = ud;
    if (size == 2 && ((uint32_t)off & s->port_mask) == s->port && s->pos + 2 < s->end) {
        const uint8_t *p = s->data + s->pos;
        s->pos += 2;
        return p[0] | (p[1] << 8);
    }
    return s->slow_read(uc, off, size, 0);
}

EXPORT void rstream_write(void *uc, uint64_t off, unsigned size, uint64_t val, void *ud)
{
    rstream_t *s = ud;
    s->slow_write(uc, off, size, val, 0);
}
