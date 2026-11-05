"""Minimal SDL2 wrapper via cffi: a window, an RGB565 texture, the keyboard.

cffi is built into PyPy and is fast there; SDL2 itself comes from the system
(on Fedora, the sdl2-compat package). Nothing needs to be compiled.
"""

import os

import cffi

ffi = cffi.FFI()
ffi.cdef("""
int SDL_Init(uint32_t flags);
void SDL_Quit(void);
const char *SDL_GetError(void);
void *SDL_CreateWindow(const char *title, int x, int y, int w, int h, uint32_t flags);
void SDL_SetWindowTitle(void *window, const char *title);
void *SDL_CreateRenderer(void *window, int index, uint32_t flags);
void *SDL_CreateTexture(void *renderer, uint32_t format, int access, int w, int h);
int SDL_UpdateTexture(void *texture, const void *rect, const void *pixels, int pitch);
int SDL_RenderClear(void *renderer);
int SDL_RenderCopy(void *renderer, void *texture, const void *src, const void *dst);
void SDL_RenderPresent(void *renderer);
int SDL_PollEvent(uint8_t *event);
""")

INIT_VIDEO = 0x20
WINDOWPOS_CENTERED = 0x2FFF0000
PIXELFORMAT_RGB565 = 0x15151002
TEXTUREACCESS_STREAMING = 1

QUIT = 0x100
KEYDOWN = 0x300
KEYUP = 0x301

# SDL key codes (SDL_Keycode)
K_RETURN, K_ESCAPE, K_BACKSPACE, K_SPACE = 13, 27, 8, 32
K_RIGHT, K_LEFT, K_DOWN, K_UP = 0x4000004F, 0x40000050, 0x40000051, 0x40000052


# Library names to try: Windows, Linux, macOS. A copy next to the project
# (e.g. SDL2.dll on Windows) takes priority over the system one.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRARY_NAMES = ['SDL2.dll', 'libSDL2-2.0.so.0', 'libSDL2.so', 'libSDL2-2.0.0.dylib', 'libSDL2.dylib']


def load_sdl():
    candidates = [os.path.join(PROJECT_DIR, name) for name in LIBRARY_NAMES] + LIBRARY_NAMES
    for name in candidates:
        if os.sep in name and not os.path.exists(name):
            continue
        try:
            return ffi.dlopen(name)
        except OSError:
            pass
    raise RuntimeError('SDL2 library not found. Install SDL2, or on Windows put SDL2.dll '
                       f'next to gui.py ({PROJECT_DIR}).')


class Window:
    def __init__(self, title, width, height, scale=2):
        self.sdl = load_sdl()
        if self.sdl.SDL_Init(INIT_VIDEO) != 0:
            raise RuntimeError(ffi.string(self.sdl.SDL_GetError()).decode())
        self.width, self.height = width, height
        self.window = self.sdl.SDL_CreateWindow(title.encode(), WINDOWPOS_CENTERED, WINDOWPOS_CENTERED,
                                                width * scale, height * scale, 0)
        self.renderer = self.sdl.SDL_CreateRenderer(self.window, -1, 0)
        self.texture = self.sdl.SDL_CreateTexture(self.renderer, PIXELFORMAT_RGB565,
                                                  TEXTUREACCESS_STREAMING, width, height)
        if not self.texture:
            raise RuntimeError(ffi.string(self.sdl.SDL_GetError()).decode())
        self.event = ffi.new('uint8_t[64]')   # SDL_Event is 56 bytes

    def set_title(self, title):
        self.sdl.SDL_SetWindowTitle(self.window, title.encode())

    def show(self, pixels):
        """pixels: a buffer of width*height RGB565 pixels (2 bytes, little-endian)."""
        sdl = self.sdl
        sdl.SDL_UpdateTexture(self.texture, ffi.NULL, ffi.from_buffer(pixels), self.width * 2)
        sdl.SDL_RenderClear(self.renderer)
        sdl.SDL_RenderCopy(self.renderer, self.texture, ffi.NULL, ffi.NULL)
        sdl.SDL_RenderPresent(self.renderer)

    def events(self):
        """Return a list of events: ('quit',), ('down', key), ('up', key)."""
        out = []
        ev = self.event
        while self.sdl.SDL_PollEvent(ev):
            kind = ev[0] | (ev[1] << 8) | (ev[2] << 16) | (ev[3] << 24)
            if kind == QUIT:
                out.append(('quit',))
            elif kind in (KEYDOWN, KEYUP):
                repeat = ev[13]
                sym = ev[20] | (ev[21] << 8) | (ev[22] << 16) | (ev[23] << 24)
                if not repeat:
                    out.append(('down' if kind == KEYDOWN else 'up', sym))
        return out

    def close(self):
        self.sdl.SDL_Quit()
