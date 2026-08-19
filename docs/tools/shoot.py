# -*- coding: utf-8 -*-
"""Drive keybinds_picker into a specific page, hide chrome, grab the window."""
import os
import sys
import time

# docs/tools/<this file>  ->  the repo's Windows tree
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
WIN = os.path.join(REPO, "windows")
RAW = os.path.join(HERE, "_raw")

# Usage: shoot.py <desktop|vmenu|keyboard> [out.png]
# Resolve the paths BEFORE the chdir below, or a relative one lands in
# windows/ instead of where the caller meant it.
WHAT = sys.argv[1] if len(sys.argv) > 1 else "desktop"
os.makedirs(RAW, exist_ok=True)
OUT = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 \
    else os.path.join(RAW, "raw_%s.png" % WHAT)

# The picker imports its siblings by bare name and reads data/ relatively.
os.chdir(WIN)
sys.path.insert(0, WIN)

import tkinter as tk
import keybinds_picker as KP
import keybinds_runtime as KR

general = {
    "seen_controllers": ["sc"],
    "virtual_menus": KR.default_virtual_menus(),
    "skin": "HatsuneMiku",
    "skins": ["DefaultTheme", "Cerulean", "Digital", "Grape", "Gruvbox",
              "HatsuneMiku", "NightShift", "Pumpkin", "Ruby", "Seafoam"],
}

p = KP._Picker({}, [], lambda b, c=None, pr=None: None, "sc", "pc",
               general=general)

# Corner links that add nothing to a still frame.
HIDE_TEXT = ("Community Configs", "Show Tutorial", "Advanced Presses")


def hide_chrome(w):
    """Un-place the floating corner links and the L1/R1 bumper glyphs."""
    for side in ("l1", "r1"):
        g = p._tab_glyphs.get(side)
        if g is not None:
            try:
                g.place_forget(); g.pack_forget()
            except tk.TclError:
                pass

    # The sidebar's 236px LIVE PREVIEW thumbnail. Slide 2 puts the menu itself
    # in front of this card at full size (see vmenu_shot.py), so the tiny copy
    # of it in the corner only competes with that.
    for pv in list(p._vmenu_preview.values()):
        try:
            pv.place_forget()
        except tk.TclError:
            pass

    def walk(node):
        for ch in node.winfo_children():
            if isinstance(ch, tk.Button):
                try:
                    txt = ch.cget("text")
                except tk.TclError:
                    txt = ""
                if any(t in txt for t in HIDE_TEXT):
                    try:
                        ch.place_forget(); ch.pack_forget()
                    except tk.TclError:
                        pass
            walk(ch)
    walk(w)


def grab(path):
    from PIL import ImageGrab
    p.root.update_idletasks(); p.root.update()
    x, y = p.root.winfo_rootx(), p.root.winfo_rooty()
    w, h = p.root.winfo_width(), p.root.winfo_height()
    ImageGrab.grab((x, y, x + w, y + h)).save(path)
    print("saved", path, w, h)


def step():
    try:
        if WHAT == "desktop":
            pass  # the picker already opens on the Desktop tab
        elif WHAT == "vmenu":
            p._show(kind="sc", view="controller")
            p.root.update()
            p._select_settings_cat("sc", "Virtual Menus")
            p.root.update()
            p._vmenu_open_editor(0)
        elif WHAT == "keyboard":
            p._show(kind="sc", view="settings")
            p.root.update()
            p._select_settings_cat("sc", "Keyboard")
        p.root.update()
        time.sleep(1.0)
        p.root.update()
        hide_chrome(p.root)
        p.root.update_idletasks(); p.root.update()
        time.sleep(0.4)
        p.root.update()
        grab(OUT)
    except Exception:
        import traceback; traceback.print_exc()
    p.root.destroy()


p.root.after(1400, step)
p.run()
