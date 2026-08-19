# -*- coding: utf-8 -*-
"""Compose the SteamlessInput showcase slides.

Takes the raw window grabs, trims the chrome that says nothing in a still,
lays each one on a dark stage as a card seen from a real camera angle, and
prints the headline over it. Slide 3 also carries the live Miku-skinned
keyboard in front of the settings page that skins it, under a drift of
sakura petals.
"""
import math
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SHOTS = os.path.join(HERE, "_raw")          # shoot.py / osk_shot.py
OUT = os.path.join(REPO, "docs", "assets")  # the published slides
FONTS = os.path.join(REPO, "windows", "data", "fonts")
os.makedirs(SHOTS, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

# 1600x1080 renders crisp on a HiDPI screen and still fills GitHub's ~1012px
# README column without the reader having to open it. Everything is drawn at
# 2x and LANCZOS'd down, so the card edges and type stay clean through the
# perspective warp.
W, H = 1600, 1120
SS = 2
CW, CH = W * SS, H * SS

BLACK_F = os.path.join(FONTS, "Montserrat-Black.ttf")
MED_F = os.path.join(FONTS, "Inter-Medium.ttf")

ACCENT = (26, 159, 255)          # the picker's own selection blue
MIKU = (64, 208, 198)            # the Miku skin's teal


# --------------------------------------------------------------------------
# camera
# --------------------------------------------------------------------------
def quad_from_3d(size, yaw, pitch, dist_w=1.9, focal_w=2.2,
                 fit=None, center=None):
    """Project a flat rectangle held at `yaw`/`pitch` (degrees) through a real
    pinhole camera, then fit the result to `width` around `center`.

    Doing the projection properly — rather than hand-placing four corners —
    is what keeps the screenshot from looking horizontally stretched: the
    foreshortening and the aspect come out of the same camera.
    """
    w, h = size
    yaw, pitch = math.radians(yaw), math.radians(pitch)
    dist, focal = w * dist_w, w * focal_w
    pts = []
    for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2),
                 (w / 2, h / 2), (-w / 2, h / 2)):
        z = 0.0
        x2, z2 = x * math.cos(yaw) + z * math.sin(yaw), \
            -x * math.sin(yaw) + z * math.cos(yaw)
        y3, z3 = y * math.cos(pitch) - z2 * math.sin(pitch), \
            y * math.sin(pitch) + z2 * math.cos(pitch)
        Z = z3 + dist
        pts.append((focal * x2 / Z, focal * y3 / Z))

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bw, bh = max(xs) - min(xs), max(ys) - min(ys)
    scale = min(fit[0] / bw, fit[1] / bh) if fit else 1.0
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    ox, oy = center or (W / 2, H / 2)
    quad = [(int((p[0] - cx) * scale * SS + ox * SS),
             int((p[1] - cy) * scale * SS + oy * SS)) for p in pts]
    print("   quad %-14s x %4d..%4d  y %4d..%4d" % (
        size, min(q[0] for q in quad) / SS, max(q[0] for q in quad) / SS,
        min(q[1] for q in quad) / SS, max(q[1] for q in quad) / SS))
    return quad


def find_coeffs(dst, src):
    """PIL PERSPECTIVE coefficients mapping the destination quad back to the
    source quad (PIL samples per output pixel, so the transform runs dst→src)."""
    m = []
    for (dx, dy), (sx, sy) in zip(dst, src):
        m.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        m.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
    A = np.array(m, dtype=float)
    B = np.array(src, dtype=float).reshape(8)
    return np.linalg.solve(A, B)


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                                        radius=radius, fill=255)
    return m


def make_card(img, radius=18, border=(255, 255, 255, 70), lift=1.07):
    """Screenshot -> a screen: rounded, hairline-bezelled, alpha-cut.

    `lift` is a small brightness gain that cancels the far-edge shading below,
    so this very dark UI still reads as a powered-on panel rather than a hole
    in the page. Nothing on it moves or changes."""
    img = ImageEnhance.Brightness(img.convert("RGB")).enhance(lift)
    img = img.convert("RGBA")
    m = rounded_mask(img.size, radius)
    card = Image.new("RGBA", img.size, (0, 0, 0, 0))
    card.paste(img, (0, 0), m)
    ImageDraw.Draw(card).rounded_rectangle(
        [0, 0, img.size[0] - 1, img.size[1] - 1],
        radius=radius, outline=border, width=3)
    return card


def lift_rgba(img, k=1.10):
    """make_card's brightness lift for a layer that must KEEP its alpha —
    the virtual-menu overlay is transparent between cells, and going through
    RGB to enhance it would fill those gaps in with black."""
    img = img.convert("RGBA")
    r, g, b, a = img.split()
    rgb = ImageEnhance.Brightness(Image.merge("RGB", (r, g, b))).enhance(k)
    return Image.merge("RGBA", (*rgb.split(), a))


def project(card, quad):
    w, h = card.size
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    return card.transform((CW, CH), Image.PERSPECTIVE,
                          find_coeffs(quad, src),
                          resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))


def _mul_alpha(mask, alpha):
    return Image.fromarray(
        (np.asarray(mask, dtype=np.uint16) *
         np.asarray(alpha, dtype=np.uint16) // 255).astype(np.uint8), "L")


def light_falloff(layer, quad, amount=0.13, sheen=28, warm=None):
    """Shade the card along its receding axis, and catch a highlight down the
    near edge, so the warp reads as a surface turned away from the light
    rather than a flat skew."""
    a = layer.split()[3]
    x0, x1 = min(p[0] for p in quad), max(p[0] for p in quad)
    ramp = np.clip((np.arange(CW, dtype=np.float32) - x0) /
                   max(1.0, x1 - x0), 0, 1)

    dark = Image.new("RGBA", layer.size, (0, 0, 0, 255))
    dark.putalpha(_mul_alpha(Image.fromarray(
        np.tile((ramp * amount * 255).astype(np.uint8), (CH, 1)), "L"), a))
    out = Image.alpha_composite(layer, dark)

    gloss = Image.new("RGBA", layer.size, warm or (255, 255, 255, 255))
    gloss.putalpha(_mul_alpha(Image.fromarray(
        np.tile((((1 - ramp) ** 3) * sheen).astype(np.uint8), (CH, 1)),
        "L"), a))
    return Image.alpha_composite(out, gloss)


def drop_shadow(layer, blur, dy, dx=0, opacity=0.72, spread=0):
    """Soft contact shadow cut from the card's own silhouette."""
    a = layer.split()[3]
    if spread:
        a = a.filter(ImageFilter.MaxFilter(spread * 2 + 1))
    a = a.filter(ImageFilter.GaussianBlur(blur)).point(
        lambda v: int(v * opacity))
    sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    sh.putalpha(a)
    return Image.fromarray(np.roll(np.roll(np.asarray(sh), dy, axis=0),
                                   dx, axis=1), "RGBA")


def screen_glow(canvas, layer, blur, strength=0.55):
    """Light the card throws back onto the wall behind it — the cheapest way
    to lift a dark UI off a dark stage without washing the UI out."""
    g = layer.filter(ImageFilter.GaussianBlur(blur))
    rgb = np.asarray(g.convert("RGB"), dtype=np.float32)
    a = np.asarray(g.split()[3], dtype=np.float32)[:, :, None] / 255.0
    base = np.asarray(canvas.convert("RGB"), dtype=np.float32)
    out = np.clip(base + rgb * a * strength, 0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGB").convert("RGBA")


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------
def background(glow, glow2=None):
    """Vertical wash + a broad bloom behind where the card will sit."""
    y = np.linspace(0, 1, CH, dtype=np.float32)[:, None]
    base = np.repeat((np.array([15, 21, 32], dtype=np.float32) * (1 - y) +
                      np.array([5, 7, 12], dtype=np.float32) * y)[:, None, :],
                     CW, axis=1)
    yy, xx = np.mgrid[0:CH, 0:CW].astype(np.float32)

    def bloom(cx, cy, rad, color, strength):
        d = np.sqrt(((xx - cx) / rad) ** 2 + ((yy - cy) / (rad * 0.66)) ** 2)
        return (np.clip(1 - d, 0, 1) ** 2 * strength)[:, :, None] * \
            np.array(color, dtype=np.float32)

    base += bloom(CW * 0.28, CH * 0.34, CW * 0.66, glow, 0.36)
    if glow2:
        base += bloom(CW * 0.84, CH * 0.80, CW * 0.55, glow2, 0.26)

    d = np.sqrt(((xx - CW / 2) / (CW * 0.74)) ** 2 +
                ((yy - CH / 2) / (CH * 0.84)) ** 2)
    base *= np.clip(1.14 - d * 0.58, 0.32, 1.0)[:, :, None]
    base += np.random.default_rng(7).normal(0, 1.8, (CH, CW, 1))
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8),
                           "RGB").convert("RGBA")


def tracked_text(draw, xy, text, font, fill, tracking=0):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def fit_font(path, text, max_w, start, floor, draw):
    """Largest point size at or below `start` whose `text` fits `max_w`.

    The slides' copy is edited by hand and its length varies a lot, so the
    headline sizes itself to the words rather than the words having to be
    trimmed to a size. Below `floor` it gives up and returns `floor` — that is
    the signal that the line genuinely needs rewriting, not shrinking."""
    for px in range(start, floor - 1, -2):
        f = ImageFont.truetype(path, px * SS)
        if draw.textlength(text, font=f) <= max_w:
            return f
    return ImageFont.truetype(path, floor * SS)


def draw_headline(canvas, eyebrow, headline, sub, accent):
    """Eyebrow / headline / deck, over a soft drop so the type holds up
    wherever the bloom lands behind it."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x = 116 * SS
    avail = (W - 2 * 112) * SS

    f_eye = ImageFont.truetype(MED_F, 24 * SS)
    f_head = fit_font(BLACK_F, headline.upper(), avail, 76, 46, d)
    f_sub = fit_font(MED_F, sub, avail, 26, 19, d)

    ey = 60 * SS
    d.rounded_rectangle([x, ey + 3 * SS, x + 7 * SS, ey + 26 * SS],
                        radius=4 * SS, fill=accent + (255,))
    tracked_text(d, (x + 21 * SS, ey), eyebrow.upper(), f_eye,
                 accent + (255,), tracking=3.2 * SS)

    # Baseline-anchored, not top-anchored: a headline that had to shrink to
    # fit still sits on the same line as the ones that did not, so the three
    # slides keep a common horizon.
    d.text((x - 5 * SS, 171 * SS), headline.upper(), font=f_head,
           fill=(255, 255, 255, 255), anchor="ls")
    d.text((x, 216 * SS), sub, font=f_sub, fill=(146, 165, 185, 255),
           anchor="ls")

    shade = Image.merge("RGBA", (
        *[Image.new("L", canvas.size, 0)] * 3,
        layer.filter(ImageFilter.GaussianBlur(13 * SS))
        .split()[3].point(lambda v: min(255, int(v * 1.5)))))
    canvas.alpha_composite(shade)
    canvas.alpha_composite(layer)
    return canvas


# --------------------------------------------------------------------------
# sakura
# --------------------------------------------------------------------------
def _bez(p0, p1, p2, p3, n=26):
    out = []
    for i in range(n + 1):
        t, u = i / n, 1 - i / n
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] +
                    3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] +
                    3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def petal(px, color, alpha):
    """One cherry-blossom petal: fat body, notched tip, a little lighter at
    the base so it catches light like a real one."""
    n = px * 4                              # drawn oversized, then downsampled
    img = Image.new("L", (n, n), 0)
    side = _bez((0.50, 1.00), (0.97, 0.82), (1.02, 0.30), (0.72, 0.03)) + \
        _bez((0.72, 0.03), (0.63, -0.02), (0.56, 0.06), (0.50, 0.21))
    pts = [(p[0] * n, p[1] * n) for p in side]
    pts += [((1 - p[0]) * n, p[1] * n) for p in reversed(side)]
    ImageDraw.Draw(img).polygon(pts, fill=255)
    img = img.resize((px, px), Image.LANCZOS)

    shade = np.linspace(1.0, 0.86, px, dtype=np.float32)[:, None]
    rgb = np.clip(np.array(color, dtype=np.float32)[None, None, :] *
                  shade[:, :, None], 0, 255).repeat(px, axis=1)
    return Image.fromarray(np.dstack(
        [rgb.astype(np.uint8),
         (np.asarray(img, dtype=np.float32) * alpha).astype(np.uint8)]),
        "RGBA")


# Petals drift over the cards, never over the words; and the big soft ones
# stay out in the side margins, where an out-of-focus foreground belongs.
TITLE_BOX = (60, 0, 1450, 258)
NEAR_MARGIN = 400          # px from either edge that a foreground petal may use


def sakura(canvas, seed=5, count=16):
    rng = random.Random(seed)
    # The near-white is only ever used on a small, sharp petal: blurred
    # across a dark stage it turns grey instead of pink.
    colors = [(255, 205, 224), (255, 174, 205), (250, 146, 188),
              (246, 160, 197)]
    pale = (255, 220, 233)   # still light, but never grey once blurred
    for i in range(count):
        # A few near-camera petals go soft, which is what makes the rest read
        # as distance rather than as stickers on the glass.
        big = i % 4 == 0
        px = int((rng.uniform(80, 118) if big else rng.uniform(24, 56)) * SS)
        p = petal(px, rng.choice(colors) if big or rng.random() < 0.7
                  else pale,
                  rng.uniform(0.46, 0.62) if big else rng.uniform(0.75, 1.0))
        p = p.rotate(rng.uniform(0, 360), resample=Image.BICUBIC, expand=True)
        if big:
            p = p.filter(ImageFilter.GaussianBlur(rng.uniform(4, 7) * SS))
        elif rng.random() < 0.25:
            p = p.filter(ImageFilter.GaussianBlur(1.1 * SS))
        for _try in range(60):
            x = rng.randint(-p.size[0] // 3, CW - p.size[0] * 2 // 3)
            y = rng.randint(-p.size[1] // 3, CH - p.size[1] * 2 // 3)
            cx, cy = (x + p.size[0] / 2) / SS, (y + p.size[1] / 2) / SS
            if TITLE_BOX[0] < cx < TITLE_BOX[2] and                     TITLE_BOX[1] < cy < TITLE_BOX[3]:
                continue
            if big and NEAR_MARGIN < cx < W - NEAR_MARGIN:
                continue
            break
        canvas.alpha_composite(p, (x, y))
    return canvas


# --------------------------------------------------------------------------
# slides
# --------------------------------------------------------------------------
# The window's 42px footer (Restore Defaults / Save / profile pips / the A-B
# hint bar) is pure chrome in a still, so every grab is cut above it.
FOOTER = 779


def load(name, footer=FOOTER):
    im = Image.open(os.path.join(SHOTS, name)).convert("RGB")
    return im.crop((0, 0, im.width, footer or im.height))


def place(canvas, card, quad, blur=40, dy=34, opacity=0.75, falloff=0.13,
          sheen=28, warm=None, spread=0, glow=0.5, glow_blur=70):
    layer = light_falloff(project(card, quad), quad, falloff, sheen, warm)
    canvas.alpha_composite(drop_shadow(layer, blur * SS, dy * SS,
                                       opacity=opacity, spread=spread))
    canvas = screen_glow(canvas, layer, glow_blur * SS, glow)
    canvas.alpha_composite(layer)
    return canvas


def slide_desktop():
    c = background((18, 96, 178))
    shot = load("raw_desktop.png")
    quad = quad_from_3d(shot.size, yaw=15, pitch=5, fit=(1420, 812),
                        center=(800, 672))
    c = place(c, make_card(shot), quad)
    return draw_headline(
        c, "01 · Desktop layout", "Remap every button",
        "Every controller and gaming handheld supported!", ACCENT)


def slide_vmenus():
    c = background((16, 88, 166), (86, 42, 154))

    shot = load("raw_vmenu.png")
    s_quad = quad_from_3d(shot.size, yaw=15, pitch=5, fit=(1205, 636),
                          center=(842, 574))
    c = place(c, make_card(shot), s_quad, blur=34, dy=26, opacity=0.6,
              glow=0.42)

    # The menu itself, at full size in front of the editor that built it.
    # No make_card(): the renderer already rounds the group's outer corners
    # and leaves the gaps transparent, so the cells float and cast their own
    # shadows instead of sitting on an invented panel.
    ov = Image.open(os.path.join(SHOTS, "raw_vmenu_overlay.png")).convert("RGBA")
    o_quad = quad_from_3d(ov.size, yaw=15, pitch=-4, fit=(726, 462),
                          center=(452, 806))
    c = place(c, lift_rgba(ov, 1.10), o_quad, blur=44, dy=30, opacity=0.82,
              falloff=0.11, sheen=30, spread=2, glow=0.5, glow_blur=60)

    return draw_headline(
        c, "02 · Virtual menus", "Overlay of buttons you can customise.",
        "Switch weapons in a first-person shooter, toggle hotkeys in a MMO, "
        "create shortcuts for your desktop.", ACCENT)


def slide_keyboard():
    c = background((16, 112, 120), (156, 62, 116))

    shot = load("raw_keyboard.png")
    s_quad = quad_from_3d(shot.size, yaw=15, pitch=5, fit=(1230, 648),
                          center=(792, 592))
    c = place(c, make_card(shot), s_quad, blur=34, dy=26, opacity=0.6,
              glow=0.42)

    # The keyboard itself, standing in front of the page that skins it.
    osk = Image.open(os.path.join(SHOTS, "raw_osk.png")).convert("RGBA")
    o_quad = quad_from_3d(osk.size, yaw=15, pitch=-4, fit=(1330, 436),
                          center=(820, 872))
    c = place(c, make_card(osk, radius=12, border=(255, 255, 255, 80)),
              o_quad, blur=46, dy=32, opacity=0.8, falloff=0.11, sheen=34,
              warm=(215, 255, 250, 255), spread=2, glow=0.72, glow_blur=58)

    c = sakura(c, count=27)
    return draw_headline(
        c, "03 · On-screen keyboard", "Custom keyboard skins!",
        "Use any gamepad as a mouse and keyboard", MIKU)


def finish(img, path):
    out = img.convert("RGB").resize((W, H), Image.LANCZOS)
    out.save(path, optimize=True)
    print("%-42s %8.0f KB" % (os.path.basename(path),
                              os.path.getsize(path) / 1024))
    return out


SLIDES = [(slide_desktop, "showcase-1-desktop.png"),
          (slide_vmenus, "showcase-2-virtual-menus.png"),
          (slide_keyboard, "showcase-3-keyboard.png")]

if __name__ == "__main__":
    for fn, name in SLIDES:
        finish(fn(), os.path.join(OUT, name))
