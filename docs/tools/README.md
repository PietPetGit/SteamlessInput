# Showcase slide tooling

Rebuilds the hero slideshow at the top of the README from **real** window
grabs — no mock-ups, no hand-retouching. Windows only (it drives the actual
tkinter picker and the actual SDL keyboard), and it needs `pillow` + `numpy`,
both already in `requirements.txt`.

The whole run takes about five minutes, most of it in `compose.py`.

```powershell
Set-Location <repo root>

# 1. Grab the three picker pages. Each opens the real window, navigates to the
#    page, hides the floating corner links / L1-R1 bumper glyphs, and grabs it.
#    Leave the machine alone while these run — they screen-grab the window.
python docs/tools/shoot.py desktop
python docs/tools/shoot.py vmenu
python docs/tools/shoot.py keyboard

# 2. Render one frame of the on-screen keyboard headless and read the pixels
#    straight back off the SDL renderer — no controller needed.
python docs/tools/osk_shot.py HatsuneMiku

# 2b. Render the virtual-menu overlay itself, big, through the same renderer
#     the live overlay uses. Slide 2 stands this in front of its editor.
python docs/tools/vmenu_shot.py

# 3. Compose the slides (stage, perspective card, headline, petals) ->
#    docs/assets/showcase-{1,2,3}-*.png
python docs/tools/compose.py

# 4. Cross-fade them into the looping hero ->
#    docs/assets/showcase-slideshow.{webp,png}
python docs/tools/slideshow.py
```

`docs/tools/_raw/` is scratch — the raw grabs are inputs, not published assets.

## Notes

- **Nothing in the UI is faked.** `shoot.py` seeds the picker with the virtual
  menus the app actually ships (`keybinds_runtime.default_virtual_menus()`) and
  with `skin = HatsuneMiku`, then lets the real code draw; `vmenu_shot.py` and
  `osk_shot.py` call the app's own renderers. The only edits are subtractive:
  the 42px footer bar is cropped, and the three top-right link buttons, the
  bumper glyphs and the sidebar's LIVE PREVIEW thumbnail are un-`place()`d,
  because none of them say anything in a still.
- **Slide copy sizes itself.** `fit_font` shrinks the headline until it fits
  the column, baseline-anchored so all three slides share a horizon. If a line
  bottoms out at 46px it is genuinely too long and wants rewriting.
- **The animation is all-keyframes** (`kmin=kmax=1` in `slideshow.py`). Without
  it libwebp deltas each frame against the last, and the cross-fade tweens
  leave residue in the next slide's hold frame.
- **The camera is a real one.** `compose.py:quad_from_3d` projects the card
  through a pinhole camera at a given yaw/pitch, so the foreshortening and the
  aspect come out of the same projection — a hand-placed quad stretches the
  screenshot sideways instead.
- **`make_card(lift=...)`** applies a small brightness gain that cancels the
  far-edge shading. It changes no content; it just stops a very dark UI from
  reading as a hole in the page.
- **Copy lives in the three `slide_*()` functions** at the bottom of
  `compose.py` — eyebrow, headline, deck. Edit there and re-run steps 3 and 4.
