# -*- coding: utf-8 -*-
"""Render the virtual-menu overlay itself, at showcase resolution.

The picker's sidebar preview is a 236px thumbnail — far too small to carry a
slide. This calls the SAME renderer the live overlay uses
(`keybinds_runtime.render_vmenu_image`) at several times its natural size, so
slide 2 can put the actual menu in front of the editor that built it.
"""
import os
import sys

# docs/tools/<this file>  ->  the repo's Windows tree
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
WIN = os.path.join(REPO, "windows")
RAW = os.path.join(HERE, "_raw")

# Usage: vmenu_shot.py [out.png] [scale]
os.makedirs(RAW, exist_ok=True)
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.join(RAW, "raw_vmenu_overlay.png")
SCALE = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

# The icon loader resolves data/images/vmenu_icons relatively.
os.chdir(WIN)
sys.path.insert(0, WIN)

import keybinds_runtime as KR

# The menu SteamlessInput ships with (tray.DEFAULT_SETTINGS), so the slide
# shows what a fresh install actually has on it.
menu = KR.default_virtual_menus()[0]
entries = menu["entries"]
nat_w, nat_h = KR.vmenu_natural_size(menu["type"], len(entries))
w, h = int(nat_w * SCALE), int(nat_h * SCALE)

# Spotify (cell 2) lit, so the menu reads as one being used rather than an
# idle grid. No `thumb=` cursor: its art is a 128px sprite, and blown up to
# this size it just smears over the icon it is supposed to be pointing at.
HL = 2

img = KR.render_vmenu_image(entries, w, h, highlight=HL,
                            gap=int(KR.VMENU_CELL_GAP * SCALE))
img.save(OUT)
print("saved", OUT, img.size)
