# -*- coding: utf-8 -*-
"""Render ONE OSK frame headless with a chosen skin and read the pixels back."""
import ctypes
import os
import sys

# docs/tools/<this file>  ->  the repo's Windows tree
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
WIN = os.path.join(REPO, "windows")
RAW = os.path.join(HERE, "_raw")

# Usage: osk_shot.py [SkinName] [out.png] [size]
# Resolve the paths BEFORE the chdir below, or a relative one lands in
# windows/ instead of where the caller meant it.
SKIN = sys.argv[1] if len(sys.argv) > 1 else "HatsuneMiku"
os.makedirs(RAW, exist_ok=True)
OUT = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 \
    else os.path.join(RAW, "raw_osk.png")
SIZE = sys.argv[3] if len(sys.argv) > 3 else None

# adusk resolves data/skins, data/fonts and the SDL3 DLLs relatively.
os.chdir(WIN)
sys.path.insert(0, WIN)

import sdl3w as S
from adusk import screen as SC
from adusk import skins, state, vptr
from adusk.screen import CoordFraction
from adusk.adusk import load_kb_config

skins.set_active_skin(SKIN)
if SIZE:
    SC.set_osk_size(SIZE)

if not S.SDL_InitSubSystem(S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS):
    raise SystemExit("SDL init failed: " + S.get_error())
if not S.TTF_Init():
    raise SystemExit("TTF_Init failed: " + S.get_error())

pages = load_kb_config().construct_pages()
first = next(iter(pages))
state.set_osk_page(first)
vkb = pages[first]

scr = SC.Screen()
ptrs = (vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(1 / 4, 1 / 2)),
        vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(3 / 4, 1 / 2)))
scr.render(vkb, ptrs)

read = S.SDL.SDL_RenderReadPixels
read.restype = ctypes.POINTER(S.SDL_Surface)
read.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
surf = read(scr.renderer, None)
if not surf:
    raise SystemExit("read failed: " + S.get_error())
s = surf.contents
print("surface", s.w, s.h, "pitch", s.pitch, "fmt", hex(s.format))

buf = ctypes.string_at(s.pixels, s.pitch * s.h)
from PIL import Image
img = Image.frombuffer("RGBA", (s.w, s.h), buf, "raw", "BGRA", s.pitch, 1)
img.save(OUT)
print("saved", OUT, img.size)
