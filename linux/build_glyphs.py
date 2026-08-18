# -*- coding: utf-8 -*-
"""Dev tool  bake the controller button glyphs the keybinds picker uses out of
the local Steam install into data/images/glyphs/ at display size.

Steam ships its Steam Input glyph sets under
  <Steam>/controller_base/images/api/{knockout,dark,light}/<name>_{sm,md,lg}.png
We take the 128px "knockout" PNGs for exactly the basenames listed in
keybinds_picker._GLYPH_FILES (one source of truth) and high-quality-resize them
to GLYPH_PX, so the picker can load plain PNGs at runtime with tkinter alone
(no Pillow, no Steam dependency on the end user's machine  Windows or Linux).

Run once on a machine with Steam installed:  python build_glyphs.py
The resized PNGs are committed to the repo (mirror windows/ -> linux/ data).
Only the ~40 glyphs the picker references are copied, not Steam's full set."""

import os
import sys

from PIL import Image, ImageFilter

import keybinds_picker as kp
import pads

GLYPH_PX = 28          # on-screen glyph size (square)
HDR_PX = 57            # header L1/R1 bumper hint size (matches sc_l1_md.png)
THEME = "knockout"     # white monochrome set Steam uses in its dark UI


def _steam_path():
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for val in ("SteamPath", "InstallPath"):
                        try:
                            p = winreg.QueryValueEx(k, val)[0]
                            if p and os.path.isdir(p):
                                return p
                        except FileNotFoundError:
                            pass
            except OSError:
                pass
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam",
              os.path.expanduser("~/.steam/steam"),
              os.path.expanduser("~/.local/share/Steam")):
        if os.path.isdir(p):
            return p
    return None


# Controller photos: Steam steamui asset -> our data/images/<name>. ~1000px
# wide (matching controller_switch_pro.png); the picker fits them to its canvas
# at load. Kinds whose photo is missing fall back to line art.
_PHOTOS = {
    "controller_xbox.png": "controller_config_controller_xboxone.png",
    "controller_ps4.png": "controller_config_controller_ps4.png",
    "controller_ps5.png": "controller_config_controller_ps5.png",
    # The 2015 Steam Controller. Steam's file name predates the later models
    # having their own  this one is just "the controller".
    "controller_sc2015.png": "cropped_controller_config_controller.png",
}

# --- Controller-photo repalette ----------------------------------------------
# Steam ships these line-art PNGs at fill (13,19,27) / line (103,112,123);
# every controller image in this app is the ramochi5 monochrome look instead 
# fill (9,13,18), line (141,146,152), outlines ~1px bolder. The conversion is
# NOT a palette swap: it measures per-pixel line COVERAGE, widens it, and only
# then maps it onto the output ramp.
_FILL_IN, _LINE_IN = (13, 19, 27), (103, 112, 123)
_FILL_OUT, _LINE_OUT = (9, 13, 18), (141, 146, 152)
# Widen radius in display px, and the supersample factor the widen runs at.
# 0.5 reproduces the committed controller_xbox.png's stroke weight (median
# run 3px in the source -> 5px out), which is the calibration reference.
_WIDEN_RADIUS, _WIDEN_SS = 0.5, 4


def _coverage(im):
    """(coverage, alpha) planes as flat lists. Coverage is the MAX across
    channels of the fill->line ramp position, which is what greys Steam's
    coloured PlayStation face glyphs while still measuring their strokes."""
    px = im.convert("RGBA").getdata()
    cov, alpha = [], []
    d0, d1, d2 = (_LINE_IN[0] - _FILL_IN[0], _LINE_IN[1] - _FILL_IN[1],
                  _LINE_IN[2] - _FILL_IN[2])
    for r, g, b, a in px:
        if a < 8:
            cov.append(0)
            alpha.append(a)
            continue
        t = max((r - _FILL_IN[0]) / d0, (g - _FILL_IN[1]) / d1,
                (b - _FILL_IN[2]) / d2)
        cov.append(0 if t < 0 else (255 if t > 1 else int(t * 255 + 0.5)))
        alpha.append(a)
    return cov, alpha


def _widen(plane, size, radius=_WIDEN_RADIUS, ss=_WIDEN_SS):
    """Dilate an 8-bit plane by `radius` display px entirely in the CONTINUOUS
    domain: supersample -> MaxFilter -> box-downsample. Thresholding first and
    dilating the mask is what beads sub-pixel hairlines into a string of bright
    squares (it is what wrecked controller_ps4.png once)  don't."""
    w, h = size
    im = Image.new("L", size)
    im.putdata(plane)
    big = im.resize((w * ss, h * ss), Image.BILINEAR)
    big = big.filter(ImageFilter.MaxFilter(size=2 * int(radius * ss) + 1))
    return big.resize(size, Image.BOX)


def _repalette_photo(src_path):
    """Steam's controller line-art -> this app's palette (see above)."""
    im = Image.open(src_path).convert("RGBA")
    cov, alpha = _coverage(im)
    t = _widen(cov, im.size).getdata()
    # The alpha mask is dilated to match, so the widened OUTER contour stays
    # opaque instead of being clipped by the original silhouette.
    a = _widen(alpha, im.size).getdata()
    out = Image.new("RGBA", im.size)
    out.putdata([
        (_FILL_OUT[0] + (_LINE_OUT[0] - _FILL_OUT[0]) * v // 255,
         _FILL_OUT[1] + (_LINE_OUT[1] - _FILL_OUT[1]) * v // 255,
         _FILL_OUT[2] + (_LINE_OUT[2] - _FILL_OUT[2]) * v // 255,
         av)
        for v, av in zip(t, a)])
    return out


def main():
    steam = _steam_path()
    if not steam:
        print("Steam install not found"); return 1
    src_dir = os.path.join(steam, "controller_base", "images", "api", THEME)
    if not os.path.isdir(src_dir):
        print("glyph source not found:", src_dir); return 1
    img_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "images")
    out_dir = os.path.join(img_dir, "glyphs")
    os.makedirs(out_dir, exist_ok=True)

    bases = set()
    for mapping in kp._GLYPH_FILES.values():
        bases.update(mapping.values())
    # Every kind in the controller catalog: its picker glyphs + OSK trigger
    # hints (the latter are RAW md-size copies, like switchpro_l2_md.png).
    md_bases = set()
    for kind in pads.KINDS:
        p = pads.PADS[kind]
        bases.update(p["glyphs"].values())
        if p.get("osk_hints"):
            md_bases.update(p["osk_hints"])

    n_ok = n_miss = 0
    for base in sorted(bases):
        src = os.path.join(src_dir, base + "_md.png")
        if not os.path.exists(src):
            print("  MISSING:", base); n_miss += 1; continue
        im = Image.open(src).convert("RGBA").resize((GLYPH_PX, GLYPH_PX),
                                                    Image.LANCZOS)
        im.save(os.path.join(out_dir, base + ".png"))
        n_ok += 1
    for base in sorted(md_bases):
        src = os.path.join(src_dir, base + "_md.png")
        if not os.path.exists(src):
            print("  MISSING (md):", base); n_miss += 1; continue
        Image.open(src).convert("RGBA").save(
            os.path.join(out_dir, base + "_md.png"))
        n_ok += 1
    # Header L1/R1 bumper hints  every kind's own shoulder art at HDR_PX
    # (<base>_hdr.png), so the picker's tab-cycle glyphs can follow the
    # steering controller (see keybinds_picker._hdr_glyph_base).
    hdr_bases = set()
    for kind in pads.KINDS:
        for side in ("l1", "r1"):
            hdr_bases.add(kp._hdr_glyph_base(kind, side))
    for base in sorted(hdr_bases):
        src = os.path.join(src_dir, base + "_md.png")
        if not os.path.exists(src):
            print("  MISSING (hdr):", base); n_miss += 1; continue
        Image.open(src).convert("RGBA").resize((HDR_PX, HDR_PX),
                                               Image.LANCZOS).save(
            os.path.join(out_dir, base + "_hdr.png"))
        n_ok += 1
    print(f"wrote {n_ok} glyphs ({GLYPH_PX}px + raw md) to {out_dir}"
          + (f"  {n_miss} MISSING" if n_miss else ""))

    # Controller photos are baked ONCE and committed: some have since been
    # hand-repaired in place (controller_ps4.png's sub-pixel hairlines,
    # controller_triton.png, which isn't a Steam asset at all any more  see
    # sc_viewer._build_assets), so a rerun must not silently overwrite them.
    # Only missing files are written; pass --force-photos to redo the lot.
    force = "--force-photos" in sys.argv
    photo_src = os.path.join(steam, "steamui", "images", "controller")
    for out_name, src_name in sorted(_PHOTOS.items()):
        out = os.path.join(img_dir, out_name)
        if os.path.exists(out) and not force:
            print("kept photo", out_name, "(exists; --force-photos to redo)")
            continue
        src = os.path.join(photo_src, src_name)
        if not os.path.exists(src):
            print("  MISSING photo:", src_name); continue
        _repalette_photo(src).save(out)
        print("wrote photo", out_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
