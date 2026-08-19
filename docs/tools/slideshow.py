# -*- coding: utf-8 -*-
"""Build the looping hero slideshow from the three composed slides.

Two builds come out of this:

  showcase-slideshow.webp  the one the README uses. Animated WebP cross-fades
                           between the slides and stays around a megabyte;
                           1280px wide still oversamples GitHub's ~1012px
                           README column.
  showcase-slideshow.png   an APNG fallback for anywhere animated WebP is not
                           wanted. Lossless, so it CUTS between slides — every
                           cross-fade tween would cost another full frame.
"""
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                   "docs", "assets")

NAMES = ["showcase-1-desktop.png",
         "showcase-2-virtual-menus.png",
         "showcase-3-keyboard.png"]

ANIM_W = 1280
HOLD_MS = 4200        # how long each slide sits still
FADE_MS = 90          # one cross-fade step
FADE_STEPS = 4


def main():
    full = [Image.open(os.path.join(OUT, n)).convert("RGB") for n in NAMES]
    size = (ANIM_W, round(ANIM_W * full[0].height / full[0].width))
    slides = [im.resize(size, Image.LANCZOS) for im in full]

    frames, durs = [], []
    for i, cur in enumerate(slides):
        frames.append(cur)
        durs.append(HOLD_MS)
        nxt = slides[(i + 1) % len(slides)]
        for s in range(1, FADE_STEPS + 1):
            frames.append(Image.blend(cur, nxt, s / (FADE_STEPS + 1)))
            durs.append(FADE_MS)

    # kmin/kmax = 1 makes EVERY frame a keyframe. Without it libwebp encodes
    # each frame as a delta against the last, and the cross-fade tweens leave
    # residue that survives into the next slide's hold frame — slide 2 showed
    # 4x the error of the other two, which read as slide 1 "bleeding" into it.
    # All-keyframes costs almost nothing here (a fade changes every pixel
    # anyway), which buys the headroom for quality 88.
    webp = os.path.join(OUT, "showcase-slideshow.webp")
    frames[0].save(webp, save_all=True, append_images=frames[1:],
                   duration=durs, loop=0, quality=88, method=4,
                   kmin=1, kmax=1)

    apng = os.path.join(OUT, "showcase-slideshow.png")
    slides[0].save(apng, save_all=True, append_images=slides[1:],
                   duration=HOLD_MS + FADE_MS * FADE_STEPS, loop=0)

    for p in (webp, apng):
        im = Image.open(p)
        print("%-28s %3d frames  %7.0f KB  %s" % (
            os.path.basename(p), getattr(im, "n_frames", 1),
            os.path.getsize(p) / 1024, im.size))


if __name__ == "__main__":
    main()
