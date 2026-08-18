# -*- coding: utf-8 -*-
"""Live Steam Controller (Triton) viewer for the keybinds picker.

A Python/Tk port of "Steam Controller Gamepad Viewer by Ramonchi" (the web
overlay bundled in the repo): the same SVG line-art controller, but rendered
once with PIL into a static base image plus a set of small pre-rendered
"pressed" overlay images, all placed on the picker's existing controller
canvas. At runtime lighting a button up is a single canvas
itemconfigure(state=...) call and moving a stick/trackpad dot is a single
coords() call  no per-frame rasterizing, no SVG engine, no browser.

Data flow (all in-process):
  * tray.py calls publish(sci) at the top of its watcher's on_input with the
    RAW parsed HID frame. publish() is a single attribute store gated on a
    module flag, so it costs ~nothing while the picker is hidden.
  * keybinds_picker polls latest() at ~30 Hz ON ITS OWN Tk thread, but only
    while the picker window is visible AND an SC layout tab is showing; each
    tick diffs a compact state against the previous one and touches only the
    canvas items that actually changed. An idle controller = one tuple
    compare per tick; a hidden picker = nothing at all.

The SCI frames are immutable namedtuples, so the cross-thread handoff is a
plain atomic slot swap  no locks anywhere.

Geometry below is transcribed from the viewer's index.html / controller art
SVG (same coordinate space; the art SVG is served with a 24px overflow margin
so both files share coordinates). Button-bit constants match
steamcontroller.SCButtons but are inlined so this module stays importable
without the HID stack (standalone preview harness).
"""

import math
import re
import threading
import time

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
except Exception:                    # pragma: no cover - PIL ships with the app
    Image = None

# ---------------------------------------------------------------------------
# publish slot (tray -> picker). _latest is an immutable namedtuple; CPython
# attribute stores/reads are atomic, so no lock is needed. _active is flipped
# by the picker so the tray-side publish is a no-op while nothing is showing.
# ---------------------------------------------------------------------------
_active = False
_latest = None
# Controller-navigation claim (picker UI driven by the SC). Separate from
# _active: the viewer wants frames whenever an SC layout tab is on screen
# (even unfocused), navigation only while the picker window is FOREGROUND 
# and the tray additionally masks the nav buttons (dpad/A/B/LB/RB) out of its
# own dispatch while the claim is up, so steering the picker doesn't also
# fire desktop binds / the virtual pad.
_nav = False
# "Listen" claim (picker waiting for a raw button press to bind it): while up,
# the tray masks EVERY button bit (not just the nav set) out of its own
# dispatch so the press being captured can't also fire desktop binds / XInput.
_listen = False
# True while a Virtual Menu is showing (see set_vmenu_open): the picker's own
# navigation stands down so the dpad/stick/A steering the menu doesn't ALSO
# move the config GUI underneath it. _vmenu_until keeps the claim asserted for
# a moment AFTER the menu goes away  see set_vmenu_open for why.
_vmenu_open = False
_vmenu_until = 0.0
# The menu is steered off HID frames (~100 Hz) but the picker only polls them
# every ~33 ms, so a whole menu session  open, aim, fire, close  can fall
# between two of its polls. Long enough that the picker is guaranteed several
# polls to resynchronise on, short enough to be invisible.
_VMENU_RELEASE_GRACE = 0.25
# First-run tutorial claim (the guided tour's overlay is on screen), and the
# tray's mirror of "the on-screen keyboard is open". Both live here because
# this module is already the import-safe picker <-> tray channel; the picker
# must not pull in adusk, and the tray must not pull in tkinter.
_tutorial = False
_osk_open = False
# Button bits the nav mask must let THROUGH (see set_nav_keep)  the tour's
# keyboard slide, teaching the bare-X keyboard open, is the only user.
_nav_keep = 0
# Last virtual-menu entry fired (see note_vmenu_fire): a monotonic counter the
# reader edge-detects on, plus the icon id of the entry, so the tour can tell
# WHICH button of its demo menu was pressed.
_vmenu_fire_seq = 0
_vmenu_fire_icon = None
# Kind of the controller behind each publish channel, so the picker can show
# the right glyph set (footer button hints / header bumpers) for the pad that
# is ACTUALLY steering it: the HID channel carries the takeover watcher's kind
# ("sc" / "sc2015" / "steam_deck"); the SDL channel the active pad's kind.
_hid_kind = "sc"
_nav_kind = None


def publish(sci):
    """Called from the tray's input watcher for every raw SC frame. Must stay
    O(1): one flag test + one slot store when the viewer is on screen.

    _tutorial is in the gate alongside the nav claim because the tour is the
    one consumer that must keep seeing frames while the picker is NOT
    foreground: every chord it teaches is one that hands the foreground away
    the moment it fires (Alt-Tab most obviously), so a nav-only gate goes dark
    on exactly the press the tour just asked for."""
    global _latest
    if _active or _nav or _tutorial:
        _latest = sci


def set_active(on):
    """Picker-side gate: True while an SC layout tab is visibly showing."""
    global _active
    _active = bool(on)


def set_nav(on):
    """Picker-side gate: True while the picker is visible AND foreground and
    wants to consume the navigation buttons (see nav_claimed)."""
    global _nav
    _nav = bool(on)


def nav_claimed():
    """Tray-side test: mask the picker-navigation buttons out of dispatch."""
    return _nav


def set_vmenu_open(on):
    """Tray-side gate: True while a Virtual Menu is on screen and being
    steered. The menu takes the pad over completely (dpad/stick move its
    selection, A fires  see the tray's _vmenu_full_takeover), so the picker
    must stop navigating ITSELF off the same presses. It reads the published
    frames directly, independently of the tray's own dispatch, so the tray
    returning early is not enough on its own  this is what tells it.

    Releasing LINGERS (_VMENU_RELEASE_GRACE). The A that fires a menu entry
    also closes the menu, so without that the claim could be raised and
    dropped entirely between two of the picker's polls: it would never see
    the claim at all, and the button still physically held would read as a
    brand-new press against a prev_btn captured before the menu even opened.
    That is precisely how choosing a menu entry also pressed the tutorial's
    Next button. The lingering claim guarantees the picker gets several polls
    to resynchronise its edge state on before navigation resumes."""
    global _vmenu_open, _vmenu_until
    on = bool(on)
    if on:
        _vmenu_open, _vmenu_until = True, 0.0
    elif _vmenu_open:
        _vmenu_open = False
        _vmenu_until = time.monotonic() + _VMENU_RELEASE_GRACE


def vmenu_open():
    """Picker-side test: a Virtual Menu owns the controller right now (or did
    a moment ago  see set_vmenu_open)."""
    return _vmenu_open or time.monotonic() < _vmenu_until


def set_listen(on):
    """Picker-side gate: True while the picker is waiting for a raw button
    press to capture ("Listen" bind mode)."""
    global _listen
    _listen = bool(on)


def listen_claimed():
    """Tray-side test: mask EVERY button bit out of dispatch (a press is
    about to be captured as a binding, it must not also fire its action)."""
    return _listen


def set_tutorial(on):
    """Picker-side gate: True while the first-run tutorial overlay is up."""
    global _tutorial
    _tutorial = bool(on)


def tutorial_claimed():
    """Tray-side test: the guided tour is on screen, so an OSK opened right
    now is a DEMONSTRATION inside our own window  do not hand the foreground
    back to whatever app the user was in before (see the restore_hwnd branch
    in launcher_thread). Without this, pressing the tour's own "open the
    keyboard" chord buried the tutorial behind the user's last window."""
    return _tutorial


def set_nav_keep(mask):
    """Picker-side: button bits the picker's navigation mask must NOT swallow.

    While the config GUI is foreground the tray strips the nav buttons out of
    dispatch so highlighting a row can't also fire the action bound to it. The
    tour needs exactly one hole in that: its keyboard slide teaches the REAL
    way to open the on-screen keyboard  a bare X in Desktop Mode  and a
    masked X opens nothing, so the one press the slide asks for would do
    nothing at all while the slide asking for it is on screen.

    So the tour publishes the bit it is currently teaching, for as long as that
    step is outstanding, and takes it back the moment the step lands. Same
    shape as the Steam/QAM-held exemption next to it in the tray, just narrowed
    to one button and one slide."""
    global _nav_keep
    _nav_keep = int(mask or 0)


def nav_keep():
    """Tray-side: bits to spare from the picker navigation mask (see above)."""
    return _nav_keep


def note_vmenu_fire(icon):
    """Tray-side: a virtual-menu entry just fired, carrying its icon id.

    Only the guided tour listens (its Virtual Menus slide asks the user to
    press one specific button and has to know they hit THAT one), so this
    stays a two-slot store rather than a queue: the sequence number is what
    the reader edge-detects on, the icon is what it matches. Firing an entry
    is a human-speed event, so nothing can outrun a single slot."""
    global _vmenu_fire_seq, _vmenu_fire_icon
    _vmenu_fire_icon = icon
    _vmenu_fire_seq += 1


def vmenu_fire():
    """(sequence, icon id) of the last virtual-menu entry fired. The sequence
    starts at 0 and only ever increases, so a reader that remembers the last
    one it saw can tell a fresh press from a repeat of the same icon."""
    return _vmenu_fire_seq, _vmenu_fire_icon


def set_osk_open(on):
    """Tray-side: publish whether the on-screen keyboard is up. The keyboard
    is always-on-top, so anything drawing a full-window overlay (the tutorial)
    has to know when it is being covered."""
    global _osk_open
    _osk_open = bool(on)


def osk_open():
    return _osk_open


def set_hid_kind(kind):
    """Tray-side: the kind behind the HID takeover channel  "sc", "sc2015"
    or "steam_deck"."""
    global _hid_kind
    _hid_kind = kind or "sc"


def hid_kind():
    return _hid_kind


def nav_kind():
    """Catalog kind of the pad behind the last SDL nav frame (or None)."""
    return _nav_kind


def latest():
    return _latest


# Second, SDL-pad navigation slot: the tray's sdl_gamepad_thread publishes its
# merged pad frame here so EVERY controller (Switch/Xbox/PS/handhelds/...) can
# steer the picker, not just the Steam Controller. Kept separate from _latest
# because that slot ALSO feeds the live SC viewer art  an SDL frame there
# would light up the drawn Steam Controller. The picker's nav pump reads BOTH.
_latest_nav = None


def publish_nav(sci, kind=None):
    """Called from the tray's SDL pad thread for every merged pad frame. Must
    stay O(1): one flag test + one slot store while the nav claim is up.
    `kind` (optional) tags the frame with the active pad's catalog kind so the
    picker's hint glyphs can match the controller actually steering it.
    _tutorial widens the gate for the same reason it does in publish()."""
    global _latest_nav, _nav_kind
    if _nav or _tutorial:
        _latest_nav = sci
        if kind:
            _nav_kind = kind


def latest_nav():
    return _latest_nav


# --- Triton button bits (== steamcontroller.SCButtons, inlined) -------------
_B_A, _B_B, _B_X, _B_Y = 0x1, 0x2, 0x4, 0x8
_B_QAM, _B_R3, _B_VIEW = 0x10, 0x20, 0x40
_B_RG1, _B_RG2, _B_RB = 0x80, 0x100, 0x200
_B_DDOWN, _B_DRIGHT, _B_DLEFT, _B_DUP = 0x400, 0x800, 0x1000, 0x2000
_B_START, _B_L3, _B_STEAM = 0x4000, 0x8000, 0x10000
_B_LG1, _B_LG2, _B_LB = 0x20000, 0x40000, 0x80000
_B_RJOYT, _B_RPADT, _B_RPAD, _B_RT = 0x100000, 0x200000, 0x400000, 0x800000
_B_LJOYT, _B_LPADT, _B_LPAD, _B_LT = 0x1000000, 0x2000000, 0x4000000, 0x8000000
_B_RGRIPT, _B_LGRIPT = 0x10000000, 0x20000000

# --- colors (match the picker's Steam-dark theme; pressed = viewer default
# blue pulled toward the app accent) -----------------------------------------
_WHITE = (242, 248, 255)
_ACC = (37, 167, 255)            # viewer's --button-pressed (#25a7ff)
_GREY = (205, 209, 213)
_LINE_A = 145                    # idle line-art alpha (~55% white)
_LINE_W = 1.2                    # line-width fudge over the SVG's 0.9/1.3px

_SS = 3                          # supersample factor for all rasterizing

# SVG window rendered onto the canvas: x, y, w, h in viewer SVG units.
# (Body spans y 0..320; pulled triggers light up down to y=-42 + glow.)
_VB = (-2.0, -46.0, 460.0, 370.0)

# ---------------------------------------------------------------------------
# geometry (transcribed from the viewer's index.html; same coords as the art)
# ---------------------------------------------------------------------------
_P_BODY = "M384.95 316.346C387.534 317.113 389.109 317.041 391.801 317.178C406.386 317.919 421.837 312.708 432.331 302.458C457.84 277.541 454.505 233.68 450.551 201.518C445.672 161.824 434.279 122.749 423.101 84.5278C419.828 73.3342 417.674 61.6952 412.621 51.0978C408.414 42.2728 401.94 34.0273 394.341 27.8778C388.861 23.4426 382.132 20.8841 375.461 18.8478C356.926 13.1892 337.122 12.5528 317.901 11.6378C287.84 10.2066 257.723 10.0237 227.634 10.0078C197.545 10.0237 167.426 10.2066 137.364 11.6378C118.143 12.5528 98.3394 13.1892 79.804 18.8478C73.1335 20.8841 66.4044 23.4426 60.924 27.8778C53.3251 34.0273 46.8514 42.2728 42.644 51.0978C37.5915 61.6952 35.4374 73.3342 32.164 84.5278C20.9865 122.749 9.59354 161.824 4.71402 201.518C0.760376 233.68 -2.5752 277.541 22.934 302.458C33.4278 312.708 48.8787 317.919 63.464 317.178C66.1557 317.041 67.7314 317.113 70.315 316.346C76.0378 314.646 80.7996 310.482 84.454 305.908C88.6574 300.646 90.6228 294.089 93.854 288.258C98.4137 280.029 103.732 271.366 111.854 266.258C117.01 263.015 122.674 261.614 128.714 261.528C161.965 261.053 194.3 260.476 227.633 260.309C260.965 260.476 293.3 261.053 326.551 261.528C332.592 261.614 338.255 263.015 343.411 266.258C351.533 271.366 356.851 280.029 361.411 288.258C364.642 294.089 366.608 300.646 370.811 305.908C374.466 310.482 379.227 314.646 384.95 316.346Z"
_P_RB = "M394.341 27.8778C388.861 23.4426 382.132 20.8841 375.461 18.8478C359.516 13.9799 342.632 12.8287 325.988 12.0195C326.058 11.5995 326.196 10.8379 326.266 10.4179C326.606 8.77624 327.409 7.19377 328.556 5.96782C329.7 4.72122 331.19 3.80654 332.796 3.27782C335.04 2.49815 337.493 2.32237 339.846 2.16782C348.531 1.64 357.39 2.38451 365.917 4.0178C373.556 5.19303 381.667 8.00852 387.905 12.7579C391.361 15.4075 394.374 18.7736 396.305 22.6979C397.225 24.5479 397.905 26.5179 398.355 28.5379L398.985 32.0308C397.494 30.5628 395.943 29.1738 394.341 27.8778Z"
_P_LB = "M60.9238 27.8778C66.4042 23.4426 73.1334 20.8841 79.8038 18.8478C95.749 13.9799 112.633 12.8287 129.277 12.0195C129.207 11.5995 129.07 10.8379 129 10.4179C128.659 8.77624 127.856 7.19377 126.71 5.96782C125.565 4.72122 124.075 3.80654 122.47 3.27782C120.225 2.49815 117.772 2.32237 115.42 2.16782C106.735 1.64 97.8756 2.38451 89.3481 4.0178C81.7091 5.19303 73.5981 8.00852 67.3601 12.7579C63.9041 15.4075 60.8911 18.7736 58.9601 22.6979C58.0401 24.5479 57.3601 26.5179 56.9101 28.5379L56.2801 32.0308C57.7709 30.5628 59.3224 29.1738 60.9238 27.8778Z"
_P_TRIG_R = "M312.4 38C314.6 10 321.2 -21 336.6 -34C345.4 -41 358.6 -41 367.4 -34C382.8 -21 389.4 10 391.6 38C379.5 35 366.3 34 352 34C336.6 34 324.5 35 312.4 38Z"
_P_TRIG_L = "M143.6 38C141.4 10 134.8 -21 119.4 -34C110.6 -41 97.4 -41 88.6 -34C73.2 -21 66.6 10 64.4 38C76.5 35 89.7 34 104 34C119.4 34 131.5 35 143.6 38Z"
_P_DPAD = "M74.0048 64.0078C76.0048 64.0078 77.0049 63.0078 77.0048 61.0078L77.0049 47.0078C77.0049 45.2084 77.0049 44.0078 80.0048 43.0078C83.0048 42.0078 85.0048 42.0078 88.0048 42.0078L89.0037 42.0078C92.0037 42.0078 94.0037 42.0078 97.0037 43.0078C100.004 44.0078 100.004 45.2084 100.004 47.0078L100.004 61.0078C100.004 63.0078 101.004 64.0078 103.004 64.0078L117.004 64.0078C118.803 64.0078 120.004 64.0078 121.004 67.0078C122.004 70.0078 122.004 72.0078 122.004 75.0078L122.004 76.0078C122.004 79.0078 122.004 81.0078 121.004 84.0078C120.004 87.0078 118.803 87.0078 117.004 87.0078L103.004 87.0078C101.004 87.0078 100.004 88.0078 100.004 90.0078L100.004 104.008C100.004 105.807 100.004 107.008 97.0037 108.008C94.0037 109.008 92.0037 109.008 89.0037 109.008H88.0037C85.0037 109.008 83.0037 109.008 80.0037 108.008C77.0037 107.008 77.0037 105.807 77.0037 104.008L77.0037 90.0078C77.0037 88.0078 76.0037 87.0078 74.0037 87.0078L60.0037 87.0078C58.2042 87.0078 57.0037 87.0078 56.0037 84.0078C55.0037 81.0078 55.0037 79.0078 55.0037 76.0078L55.0037 75.0078C55.0037 72.0078 55.0037 70.0078 56.0037 67.0078C57.0037 64.0078 58.2042 64.0078 60.0037 64.0078L74.0037 64.0078C76.0037 64.0078 77.0037 63.0078 77.0037 61.0078"
_P_DPAD_UP = "M77.004 42.008H100.004V64.008L88.504 75.508L77.004 64.008V42.008Z"
_P_DPAD_RIGHT = "M122.004 64.008V87.008H100.004L88.504 75.508L100.004 64.008H122.004Z"
_P_DPAD_DOWN = "M77.004 109.008V87.008L88.504 75.508L100.004 87.008V109.008H77.004Z"
_P_DPAD_LEFT = "M55.004 64.008H77.004L88.504 75.508L77.004 87.008H55.004V64.008Z"
_P_VIEW = "M139.217 37.5878H155.525C158.835 37.6078 161.556 40.3077 161.556 43.6177C161.556 46.9377 158.886 49.6277 155.566 49.6477L139.217 49.6478C135.887 49.6478 133.187 46.9478 133.187 43.6178C133.187 40.2878 135.887 37.5878 139.217 37.5878Z"
_P_MENU = "M299.741 37.5878H316.049C319.359 37.6078 322.08 40.3077 322.08 43.6177C322.08 46.9377 319.41 49.6277 316.09 49.6477L299.741 49.6478C296.411 49.6478 293.711 46.9478 293.711 43.6178C293.711 40.2878 296.411 37.5878 299.741 37.5878Z"
_P_QAM = "M245.444 194.708C245.444 198.258 242.564 201.138 239.014 201.138H216.254C212.704 201.138 209.824 198.258 209.824 194.708C209.824 191.158 212.704 188.278 216.254 188.278H239.014C242.564 188.278 245.444 191.158 245.444 194.708Z"

# grip-touch sensor arcs (3 stroked curves per side along the body edge)
_P_GRIPT_L = (
    "M27.6 100.1C17.9 134.6 8.7 168.8 4.71402 201.518C0.760376 233.68 -2.5752 277.541 22.934 302.458",
    "M30.9 110.5C22.2 143.7 15.1 176.9 11.8 206.8C8.4 237.7 7.4 270.8 27.6 293.6",
    "M34.5 119.9C26.9 151.5 21.2 183.2 18.8 211.9C16.3 239.6 17.2 265.9 31.7 284.1",
)
_P_GRIPT_R = (
    "M427.7 100.1C437.4 134.6 446.6 168.8 450.551 201.518C454.505 233.68 457.84 277.541 432.331 302.458",
    "M424.4 110.5C433.1 143.7 440.2 176.9 443.5 206.8C446.9 237.7 447.9 270.8 427.7 293.6",
    "M420.8 119.9C428.4 151.5 434.1 183.2 436.5 211.9C439 239.6 438.1 265.9 423.6 284.1",
)

# pressed-state button glyphs (lit white on top of the blue fill)
_P_LBL_A = "M370.343 107.838L369.583 105.568H365.023L364.243 107.838H361.633L366.303 95.0078H368.263L372.953 107.838H370.343ZM367.353 98.7878L365.733 103.458H368.923L367.353 98.7878Z"
_P_LBL_B = "M394.503 82.8378H389.133V70.0078H394.283C396.783 70.0078 398.243 71.4178 398.243 73.6178C398.243 75.0378 397.303 75.9578 396.663 76.2678C397.433 76.6278 398.423 77.4378 398.423 79.1478C398.423 81.5478 396.783 82.8477 394.493 82.8477L394.503 82.8378ZM394.083 72.2377H391.633V75.1978H394.083C395.143 75.1978 395.743 74.5978 395.743 73.7178C395.743 72.8378 395.143 72.2377 394.083 72.2377ZM394.253 77.4478H391.643V80.6077H394.253C395.383 80.6077 395.923 79.8878 395.923 79.0178C395.923 78.1478 395.383 77.4478 394.253 77.4478Z"
_P_LBL_X = "M343.453 82.8378L340.963 78.3678L338.493 82.8378H335.633L339.613 76.2578L335.883 70.0078H338.733L340.973 74.1478L343.223 70.0078H346.053L342.323 76.2578L346.323 82.8378H343.463H343.453Z"
_P_LBL_Y = "M368.483 51.0778V56.3377H365.993V51.0778L362.133 43.5078H364.853L367.253 48.6777L369.613 43.5078H372.333L368.473 51.0778H368.483Z"
_P_LBL_VIEW = (
    "M152.068 42.8677H146.058C145.718 42.8677 145.578 43.0077 145.578 43.3477V45.8677C145.578 46.2077 145.718 46.3477 146.058 46.3477H152.078C152.418 46.3477 152.558 46.2077 152.558 45.8677V43.3477C152.558 43.0077 152.418 42.8677 152.068 42.8677Z",
    "M149.216 41.2977V41.7877C147.206 41.7877 145.216 41.7877 143.206 41.7877V43.3377H144.236V44.3077H142.726C142.386 44.3077 142.246 44.1677 142.246 43.8277V41.3077C142.246 40.9577 142.386 40.8177 142.726 40.8177H148.746C149.086 40.8177 149.226 40.9677 149.226 41.3077",
)
_P_LBL_MENU = (
    "M312.686 41.5978H303.286C302.936 41.5978 302.656 41.3178 302.656 40.9678C302.656 40.6178 302.936 40.3378 303.286 40.3378H312.686C313.036 40.3378 313.316 40.6178 313.316 40.9678C313.316 41.3178 313.036 41.5978 312.686 41.5978Z",
    "M312.686 44.1478H303.286C302.936 44.1478 302.656 43.8678 302.656 43.5278C302.656 43.1878 302.936 42.8978 303.286 42.8978H312.686C313.036 42.8978 313.316 43.1778 313.316 43.5278C313.316 43.8778 313.036 44.1478 312.686 44.1478Z",
    "M312.686 46.7078H303.286C302.936 46.7078 302.656 46.4278 302.656 46.0778C302.656 45.7278 302.936 45.4578 303.286 45.4578H312.686C313.036 45.4578 313.316 45.7378 313.316 46.0778C313.316 46.4178 313.036 46.7078 312.686 46.7078Z",
)
_P_LBL_QAM = (
    "M229.056 194.708C229.056 195.508 228.406 196.158 227.606 196.158C226.806 196.158 226.156 195.508 226.156 194.708C226.156 193.908 226.806 193.258 227.606 193.258C228.406 193.258 229.056 193.908 229.056 194.708Z",
    "M222.836 194.708C222.836 195.508 222.186 196.158 221.376 196.158C220.566 196.158 219.926 195.508 219.926 194.708C219.926 193.908 220.576 193.258 221.376 193.258C222.176 193.258 222.836 193.908 222.836 194.708Z",
    "M235.267 194.708C235.267 195.508 234.617 196.158 233.817 196.158C233.017 196.158 232.367 195.508 232.367 194.708C232.367 193.908 233.017 193.258 233.817 193.258C234.617 193.258 235.267 193.908 235.267 194.708Z",
)
_P_LBL_STEAM_DOT = "M231.917 70.1406C233.279 70.1406 234.383 71.245 234.383 72.6074C234.383 73.9699 233.279 75.0751 231.917 75.0751C230.554 75.0751 229.45 73.9699 229.45 72.6074C229.45 71.245 230.554 70.1406 231.917 70.1406Z"
_P_LBL_STEAM = "M231.914 67.6972C234.625 67.6972 236.824 69.8958 236.824 72.6074C236.824 75.3191 234.625 77.5175 231.914 77.5175C231.873 77.5175 231.833 77.5156 231.793 77.5146L231.796 77.5175L227.379 80.6679C227.383 80.7387 227.386 80.81 227.386 80.8818C227.386 82.9248 225.73 84.581 223.687 84.581C221.882 84.5809 220.38 83.2887 220.054 81.579L214.888 79.5195C214.583 78.4056 214.419 77.233 214.419 76.0224C214.419 75.6188 214.439 75.2195 214.474 74.8251L221.62 77.8134C222.21 77.415 222.921 77.1826 223.687 77.1826C223.762 77.1826 223.837 77.1849 223.912 77.1894L223.894 77.1718L227 72.6601L227.004 72.665C227.004 72.6459 227.003 72.6265 227.003 72.6074C227.004 69.8959 229.202 67.6975 231.914 67.6972ZM223.651 78.1513C223.345 78.1514 223.051 78.2038 222.776 78.2968L224.422 78.9853C225.449 79.4122 225.936 80.5912 225.509 81.6181C225.082 82.6449 223.903 83.1308 222.876 82.704L221.174 82.0253C221.607 82.9614 222.552 83.6121 223.651 83.6122C225.159 83.6122 226.381 82.3896 226.381 80.8818C226.381 79.3739 225.159 78.1513 223.651 78.1513ZM231.912 69.3359C230.105 69.336 228.64 70.8008 228.64 72.6074C228.64 74.4142 230.105 75.8788 231.912 75.8788C233.718 75.8788 235.183 74.4142 235.183 72.6074C235.183 70.8007 233.718 69.3359 231.912 69.3359Z"

# decorative inner body lines (art SVG only  part of the idle line art)
_P_DECOR = (
    "M260.328 121.388C249.068 121.368 205.408 121.388 194.148 121.458",
    "M129.908 122.978C126.218 123.138 123.508 123.318 119.828 123.508C117.828 123.618 115.837 123.768 113.847 123.968C111.377 124.228 108.897 124.558 106.447 125.018C104.247 125.428 102.057 125.928 99.8962 126.548C96.0362 127.658 92.2762 129.158 88.8362 131.208C85.7662 133.028 82.9562 135.288 80.5662 137.928C78.1162 140.658 76.1162 143.798 74.5662 147.118M51.4373 293.418C52.5273 277.648 53.9673 261.918 55.7773 246.218C57.1773 234.248 58.8071 222.328 60.6471 210.428M74.5663 147.108C73.6463 149.098 72.8662 151.128 72.2263 153.218C71.9963 153.948 71.7857 154.688 71.5857 155.428M60.6482 210.428C63.5382 191.928 66.7282 173.558 71.5882 155.428",
    "M380.707 147.108C381.627 149.098 382.397 151.128 383.047 153.218C383.277 153.948 383.487 154.688 383.687 155.428C384.167 157.238 384.637 159.048 385.077 160.858C385.557 162.818 386.017 164.778 386.467 166.748C387.467 171.148 388.397 175.558 389.277 179.988C391.287 190.098 393.037 200.248 394.627 210.428M335.445 123.508C337.445 123.618 339.435 123.768 341.425 123.968C343.895 124.228 346.376 124.558 348.826 125.018C351.026 125.428 353.216 125.928 355.376 126.548C359.236 127.658 362.996 129.158 366.446 131.208C369.506 133.028 372.326 135.288 374.706 137.928C377.166 140.658 379.156 143.798 380.706 147.118M399.496 246.218C401.306 261.918 402.746 277.648 403.826 293.418M394.617 210.428C396.467 222.328 398.087 234.258 399.497 246.218M324.398 122.968C328.078 123.138 331.768 123.308 335.448 123.498",
)

# circles: (cx, cy, r)
_C_A, _C_B = (367.133, 101.508, 13.5), (393.133, 76.008, 13.5)
_C_X, _C_Y = (341.133, 76.008, 13.5), (367.133, 49.508, 13.5)
_C_STEAM = (227.5, 76.0, 14.0)
_C_LRING = (162.133, 108.758, 34.5)
_C_RRING = (293.133, 108.758, 34.5)

# back grip paddles: (cx, cy, rx, ry, rot_deg)
_E_RG2 = (367.0, 180.5, 14.4, 20.8, -11.0)   # right upper  (RGRIP1 / R4)
_E_RG1 = (375.0, 234.5, 14.4, 20.8, -11.0)   # right lower  (RGRIP2 / R5)
_E_LG2 = (88.0, 180.5, 14.4, 20.8, 11.0)     # left upper   (LGRIP1 / L4)
_E_LG1 = (80.0, 234.5, 14.4, 20.8, 11.0)     # left lower   (LGRIP2 / L5)

# trackpads: 93x93 rounded rects; left rotated 9.87° about its origin, right
# mirrored via the matrix from the SVG. Local (0..93) -> global helpers below.
_PAD_SIZE = 93.0
_PAD_L_ORIGIN = (103.296, 139.723)
_PAD_L_ROT = 9.87378
_PAD_R_RECT = (-0.81371, 1.15667)
_PAD_R_MAT = (-0.985188, 0.171478, 0.171478, 0.985188, 350.965, 138.723)

_STICK_TRAVEL = 23.0
_STICK_DOT_R = 14.49

_TRIG_TOP, _TRIG_BOTTOM = -42.0, 38.0
_TRIG_LEVELS = 11                # analog quantization steps (plus level 0)


# ---------------------------------------------------------------------------
# tiny SVG-path toolkit (only what the transcribed data needs: M/L/H/V/C/Z)
# ---------------------------------------------------------------------------
_TOK = re.compile(r"[MmLlHhVvCcZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")
_BEZ_SEGS = 12


def _parse_path(d):
    """Flatten an SVG path into [(closed, [(x, y), ...]), ...] subpaths."""
    toks = _TOK.findall(d)
    subs, cur = [], []
    closed = False
    i = 0
    cx = cy = sx = sy = 0.0
    cmd = None

    def flush():
        nonlocal cur, closed
        if len(cur) > 1:
            subs.append((closed, cur))
        cur, closed = [], False

    def num():
        nonlocal i
        v = float(toks[i]); i += 1
        return v

    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                closed = True
                flush()
                cx, cy = sx, sy
                continue
        elif cmd in ("M", "m"):       # implicit lineto after moveto
            cmd = "L" if cmd == "M" else "l"
        if cmd in ("M", "m"):
            x, y = num(), num()
            if cmd == "m":
                x, y = cx + x, cy + y
            flush()
            cx, cy = sx, sy = x, y
            cur = [(x, y)]
        elif cmd in ("L", "l"):
            x, y = num(), num()
            if cmd == "l":
                x, y = cx + x, cy + y
            cur.append((x, y)); cx, cy = x, y
        elif cmd in ("H", "h"):
            x = num()
            if cmd == "h":
                x = cx + x
            cur.append((x, cy)); cx = x
        elif cmd in ("V", "v"):
            y = num()
            if cmd == "v":
                y = cy + y
            cur.append((cx, y)); cy = y
        elif cmd in ("C", "c"):
            x1, y1, x2, y2, x, y = (num(), num(), num(), num(), num(), num())
            if cmd == "c":
                x1, y1, x2, y2, x, y = (cx + x1, cy + y1, cx + x2, cy + y2,
                                        cx + x, cy + y)
            for k in range(1, _BEZ_SEGS + 1):
                t_ = k / _BEZ_SEGS
                mt = 1.0 - t_
                bx = (mt * mt * mt * cx + 3 * mt * mt * t_ * x1
                      + 3 * mt * t_ * t_ * x2 + t_ * t_ * t_ * x)
                by = (mt * mt * mt * cy + 3 * mt * mt * t_ * y1
                      + 3 * mt * t_ * t_ * y2 + t_ * t_ * t_ * y)
                cur.append((bx, by))
            cx, cy = x, y
        else:
            raise ValueError(f"unsupported path command {cmd!r}")
    flush()
    return subs


def _ellipse_pts(cx, cy, rx, ry, rot_deg=0.0, seg=48):
    a0 = math.radians(rot_deg)
    ca, sa = math.cos(a0), math.sin(a0)
    pts = []
    for k in range(seg + 1):
        a = 2.0 * math.pi * k / seg
        x, y = rx * math.cos(a), ry * math.sin(a)
        pts.append((cx + x * ca - y * sa, cy + x * sa + y * ca))
    return pts


def _round_rect_pts(x, y, w, h, r, seg=7):
    cs = ((x + w - r, y + r, -90, 0), (x + w - r, y + h - r, 0, 90),
          (x + r, y + h - r, 90, 180), (x + r, y + r, 180, 270))
    pts = []
    for cx, cy, a0, a1 in cs:
        for k in range(seg + 1):
            a = math.radians(a0 + (a1 - a0) * k / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(pts[0])
    return pts


def _rot_about(deg, cx, cy):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    def f(x, y):
        dx, dy = x - cx, y - cy
        return (cx + dx * ca - dy * sa, cy + dx * sa + dy * ca)
    return f


def _mat_fn(a, b, c, d, e, f_):
    def f(x, y):
        return (a * x + c * y + e, b * x + d * y + f_)
    return f


_PAD_L_XF = _rot_about(_PAD_L_ROT, *_PAD_L_ORIGIN)
_PAD_R_XF = _mat_fn(*_PAD_R_MAT)


def _pad_local_to_svg(side, nx, ny):
    """0..1 pad coords (screen orientation) -> SVG-space point."""
    if side == "left":
        ox, oy = _PAD_L_ORIGIN
        return _PAD_L_XF(ox + nx * _PAD_SIZE, oy + ny * _PAD_SIZE)
    rx, ry = _PAD_R_RECT
    return _PAD_R_XF(rx + (1.0 - nx) * _PAD_SIZE, ry + ny * _PAD_SIZE)


# ---------------------------------------------------------------------------
# rasterizer: build every image ONCE per canvas size (PIL level, no Tk)
# ---------------------------------------------------------------------------
def _apply_xf(subpaths, fn):
    return [(cl, [fn(x, y) for (x, y) in pts]) for (cl, pts) in subpaths]


def _paths(*ds):
    out = []
    for d in ds:
        out.extend(_parse_path(d))
    return out


def _pts_bbox(groups):
    xs = [x for (_c, pts) in groups for (x, _y) in pts]
    ys = [y for (_c, pts) in groups for (_x, y) in pts]
    return min(xs), min(ys), max(xs), max(ys)


class _Raster:
    """Draws shapes (given in SVG units) into a supersampled local image.

    Every op works inside the shape's own bounding box and composites
    IN PLACE (Image.alpha_composite(dest=...))  full-canvas temporaries per
    shape made the base-art build ~5x slower."""

    def __init__(self, sc, x0, y0, w_px, h_px):
        self.sc = sc
        self.x0, self.y0 = x0, y0
        self.size = (max(1, int(math.ceil(w_px * _SS))),
                     max(1, int(math.ceil(h_px * _SS))))
        self.img = Image.new("RGBA", self.size, (0, 0, 0, 0))

    def pt(self, x, y):
        return ((x - self.x0) * self.sc * _SS, (y - self.y0) * self.sc * _SS)

    def _bounds(self, groups, pad_px):
        xs, ys = [], []
        for (_cl, pts) in groups:
            for (x, y) in pts:
                px, py = self.pt(x, y)
                xs.append(px)
                ys.append(py)
        if not xs:
            return None
        x0 = max(0, int(min(xs) - pad_px))
        y0 = max(0, int(min(ys) - pad_px))
        x1 = min(self.size[0], int(max(xs) + pad_px) + 1)
        y1 = min(self.size[1], int(max(ys) + pad_px) + 1)
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1, y1)

    def _mask_fill(self, box, groups, xor=False):
        bw, bh = box[2] - box[0], box[3] - box[1]
        masks = []
        for (_cl, pts) in groups:
            m = Image.new("L", (bw, bh), 0)
            ImageDraw.Draw(m).polygon(
                [(self.pt(x, y)[0] - box[0], self.pt(x, y)[1] - box[1])
                 for (x, y) in pts], fill=255)
            masks.append(m)
        if not masks:
            return Image.new("L", (bw, bh), 0)
        out = masks[0]
        for m in masks[1:]:
            # 0/255 masks: difference == xor (glyph holes); lighter == union
            out = (ImageChops.difference(out, m) if xor
                   else ImageChops.lighter(out, m))
        return out

    def _mask_stroke(self, box, groups, w_su):
        # +_SS == +1 display pixel (canvas px are supersampled by _SS): all
        # controller-art outlines are drawn 1px bolder app-wide.
        w = max(1, int(round(w_su * _LINE_W * self.sc * _SS)) + _SS)
        m = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
        dr = ImageDraw.Draw(m)
        r = w / 2.0
        for (closed, pts) in groups:
            pp = [(self.pt(x, y)[0] - box[0], self.pt(x, y)[1] - box[1])
                  for (x, y) in pts]
            if closed and pp[0] != pp[-1]:
                pp.append(pp[0])
            dr.line(pp, fill=255, width=w, joint="curve")
            for (ex, ey) in (pp[0], pp[-1]):
                dr.ellipse((ex - r, ey - r, ex + r, ey + r), fill=255)
        return m

    def _blit(self, box, mask, rgb, alpha):
        if box is None or alpha <= 0:
            return
        layer = Image.new("RGBA", mask.size, rgb + (0,))
        layer.putalpha(mask.point(lambda v: v * alpha // 255))
        self.img.alpha_composite(layer, dest=(box[0], box[1]))

    # public ops -----------------------------------------------------------
    def fill(self, groups, rgb, alpha, xor=False, grad=None, clip=None,
             erase=None):
        box = self._bounds(groups, 3)
        if box is None:
            return None
        m = self._mask_fill(box, groups, xor)
        if clip is not None:
            m = ImageChops.multiply(m, self._mask_fill(box, clip))
        if grad is not None:
            m = ImageChops.multiply(m, grad.resize(m.size))
        if erase is not None:
            m = ImageChops.subtract(m, self._mask_fill(box, erase))
        self._blit(box, m, rgb, alpha)
        return (box, m)

    def stroke(self, groups, rgb, alpha, w_su, erase=None):
        # keep in sync with _mask_stroke's width (+1 display px = +_SS)
        w = max(1, int(round(w_su * _LINE_W * self.sc * _SS)) + _SS)
        box = self._bounds(groups, w // 2 + 3)
        if box is None:
            return None
        m = self._mask_stroke(box, groups, w_su)
        if erase is not None:
            m = ImageChops.subtract(m, self._mask_fill(box, erase))
        self._blit(box, m, rgb, alpha)
        return (box, m)

    def glow_under(self, handle, rgb, radius_su, strength):
        """Blur a fill/stroke's mask and slip it UNDER the already-drawn art
        as a colored halo (halo shows around the crisp shape's edges)."""
        if handle is None:
            return
        box, mask = handle
        r = max(1.0, radius_su * self.sc * _SS)
        p = int(r * 2.5) + 2
        gx0 = max(0, box[0] - p)
        gy0 = max(0, box[1] - p)
        gx1 = min(self.size[0], box[2] + p)
        gy1 = min(self.size[1], box[3] + p)
        pm = Image.new("L", (gx1 - gx0, gy1 - gy0), 0)
        pm.paste(mask, (box[0] - gx0, box[1] - gy0))
        blur = pm.filter(ImageFilter.GaussianBlur(r))
        halo = Image.new("RGBA", pm.size, rgb + (0,))
        halo.putalpha(blur.point(lambda v: int(v * strength)))
        region = self.img.crop((gx0, gy0, gx1, gy1))
        self.img.paste(Image.alpha_composite(halo, region), (gx0, gy0))

    def done(self):
        out_w = max(1, int(round(self.size[0] / _SS)))
        out_h = max(1, int(round(self.size[1] / _SS)))
        # BOX == plain area averaging  exact for an integer supersample
        # factor and ~4x faster than LANCZOS on the big base image.
        return self.img.resize((out_w, out_h), Image.BOX)


def _build_assets(cw, ch):
    """All PIL images + placement metadata for one canvas size. Pure PIL 
    PhotoImage conversion happens in _photo_assets (needs a Tk master)."""
    vx, vy, vw, vh = _VB
    sc = min(cw / vw, ch / vh)
    # Raised 35px so the controller's bottom edge isn't cropped by the
    # (width-driven, vertically-tight) canvas.
    ox, oy = (cw - vw * sc) / 2.0, (ch - vh * sc) / 2.0 - 35

    def to_px(x, y):
        return (ox + (x - vx) * sc, oy + (y - vy) * sc)

    pad_l_poly = [(False, [_PAD_L_XF(x, y) for (x, y) in _round_rect_pts(
        _PAD_L_ORIGIN[0], _PAD_L_ORIGIN[1], _PAD_SIZE, _PAD_SIZE, 16)])]
    pad_r_poly = [(False, [_PAD_R_XF(x, y) for (x, y) in _round_rect_pts(
        _PAD_R_RECT[0], _PAD_R_RECT[1], _PAD_SIZE, _PAD_SIZE, 16)])]
    pad_polys = pad_l_poly + pad_r_poly

    # -- base line art (whole controller, idle) ----------------------------
    base = _Raster(sc, vx, vy, vw * sc, vh * sc)
    body = _paths(_P_BODY)
    base.fill(body + _paths(_P_RB, _P_LB), (0, 0, 0), 78)          # 30% black
    base.stroke(body, _WHITE, _LINE_A, 1.3)
    base.stroke(_paths(_P_RB), _WHITE, _LINE_A, 1.3)
    base.stroke(_paths(_P_LB), _WHITE, _LINE_A, 1.3)
    for d in _P_DECOR:
        base.stroke(_parse_path(d), _WHITE, _LINE_A, 0.9)
    base.stroke(_paths(_P_DPAD), _WHITE, _LINE_A, 0.9)
    base.stroke(_paths(_P_VIEW), _WHITE, _LINE_A, 0.9)
    base.stroke(_paths(_P_MENU), _WHITE, _LINE_A, 0.9)
    base.stroke(_paths(_P_QAM), _WHITE, _LINE_A, 0.9)
    for (cx, cy, r) in (_C_A, _C_B, _C_X, _C_Y, _C_STEAM,
                        _C_LRING, _C_RRING):
        base.stroke([(True, _ellipse_pts(cx, cy, r, r))],
                    _WHITE, _LINE_A, 0.9)
    base.stroke(pad_l_poly, _WHITE, _LINE_A, 0.9)
    base.stroke(pad_r_poly, _WHITE, _LINE_A, 0.9)
    # idle glyphs (letters, view/menu/qam/steam marks)
    for d in (_P_LBL_A, _P_LBL_B, _P_LBL_X, _P_LBL_Y):
        base.fill(_parse_path(d), _WHITE, _LINE_A, xor=True)
    for d in _P_LBL_VIEW + _P_LBL_MENU + _P_LBL_QAM:
        base.fill(_parse_path(d), _WHITE, _LINE_A)
    base.fill(_parse_path(_P_LBL_STEAM_DOT), _WHITE, _LINE_A)
    base.fill(_parse_path(_P_LBL_STEAM), _WHITE, _LINE_A, xor=True)
    # idle back-grip paddles (faint grey, clipped out of the trackpads)
    for (cx, cy, rx, ry, rot) in (_E_RG2, _E_RG1, _E_LG2, _E_LG1):
        ell = [(True, _ellipse_pts(cx, cy, rx, ry, rot))]
        base.fill(ell, _GREY, 57, erase=pad_polys)
        base.stroke(ell, (240, 244, 248), 36, 0.8, erase=pad_polys)
    # stick centre dots (always-on markers)
    for (cx, cy, _r) in (_C_LRING, _C_RRING):
        base.fill([(True, _ellipse_pts(cx, cy, 4.5, 4.5))],
                  (255, 255, 255), 172)

    assets = {
        "sc": sc, "to_px": to_px,
        "base": (base.done(), to_px(vx, vy)),
        "over": {},          # key -> (pil, (px, py))  anchor nw
        "trig": {},          # side -> [level images]  anchor nw
        "stick": {},         # variant -> pil          anchor centre
        "pad": {},
    }

    # -- pressed overlay builder -------------------------------------------
    def overlay(key, draw_fn, bbox_groups, pad_su=7.0):
        bx0, by0, bx1, by1 = _pts_bbox(bbox_groups)
        bx0 -= pad_su; by0 -= pad_su; bx1 += pad_su; by1 += pad_su
        r = _Raster(sc, bx0, by0, (bx1 - bx0) * sc, (by1 - by0) * sc)
        draw_fn(r)
        assets["over"][key] = (r.done(), to_px(bx0, by0))

    def button_disc(key, circle, label_d, label_xor=True):
        cx, cy, rad = circle
        ring = [(True, _ellipse_pts(cx, cy, rad, rad))]
        def draw(r):
            fm = r.fill(ring, _ACC, 210)
            r.stroke(ring, _WHITE, 242, 0.85)
            if isinstance(label_d, tuple):
                for d in label_d:
                    r.fill(_parse_path(d), _WHITE, 250)
            elif label_d:
                r.fill(_parse_path(label_d), _WHITE, 250, xor=label_xor)
            r.glow_under(fm, _ACC, 4.2, 0.62)
        overlay(key, draw, ring)

    button_disc("a", _C_A, _P_LBL_A)
    button_disc("b", _C_B, _P_LBL_B)
    button_disc("x", _C_X, _P_LBL_X)
    button_disc("y", _C_Y, _P_LBL_Y)

    steam_ring = [(True, _ellipse_pts(_C_STEAM[0], _C_STEAM[1],
                                      _C_STEAM[2], _C_STEAM[2]))]

    def draw_steam(r):
        fm = r.fill(steam_ring, _ACC, 210)
        r.stroke(steam_ring, _WHITE, 242, 0.85)
        r.fill(_parse_path(_P_LBL_STEAM_DOT), _WHITE, 250)
        r.fill(_parse_path(_P_LBL_STEAM), _WHITE, 250, xor=True)
        r.glow_under(fm, _ACC, 4.2, 0.62)
    overlay("steam", draw_steam, steam_ring)

    def shape_button(key, shape_d, glyphs):
        groups = _paths(shape_d)
        def draw(r):
            fm = r.fill(groups, _ACC, 210)
            r.stroke(groups, _WHITE, 242, 0.85)
            for d in glyphs:
                r.fill(_parse_path(d), _WHITE, 250)
            r.glow_under(fm, _ACC, 4.2, 0.62)
        overlay(key, draw, groups)

    shape_button("view", _P_VIEW, _P_LBL_VIEW)
    shape_button("menu", _P_MENU, _P_LBL_MENU)
    shape_button("qam", _P_QAM, _P_LBL_QAM)

    for key, d in (("rb", _P_RB), ("lb", _P_LB)):
        groups = _paths(d)
        def draw(r, groups=groups):
            fm = r.fill(groups, _ACC, 235)
            r.stroke(groups, _WHITE, 248, 1.3)
            r.glow_under(fm, _ACC, 5.5, 0.72)
        overlay(key, draw, groups)

    # back grip paddles
    for key, ell in (("rg2", _E_RG2), ("rg1", _E_RG1),
                     ("lg2", _E_LG2), ("lg1", _E_LG1)):
        groups = [(True, _ellipse_pts(ell[0], ell[1], ell[2], ell[3], ell[4]))]
        def draw(r, groups=groups):
            fm = r.fill(groups, _ACC, 240, erase=pad_polys)
            r.stroke(groups, _WHITE, 240, 0.8, erase=pad_polys)
            r.glow_under(fm, _ACC, 4.0, 0.6)
        overlay(key, draw, groups)

    # grip capacitive-touch arcs
    for key, arcs in (("lgripT", _P_GRIPT_L), ("rgripT", _P_GRIPT_R)):
        groups = [sp for d in arcs for sp in _parse_path(d)]
        def draw(r, arcs=arcs):
            for i, d in enumerate(arcs):
                r.stroke(_parse_path(d), _GREY, 122, 1.9 if i == 0 else 1.25)
        overlay(key, draw, groups, pad_su=4.0)

    # stick click rings
    for key, (cx, cy, rad) in (("l3ring", _C_LRING), ("r3ring", _C_RRING)):
        ring = [(True, _ellipse_pts(cx, cy, rad, rad))]
        def draw(r, ring=ring):
            r.fill(ring, _ACC, 52)
            sm = r.stroke(ring, _WHITE, 248, 2.0)
            r.glow_under(sm, _ACC, 4.5, 0.6)
        overlay(key, draw, ring)

    # dpad wedges: blue fill fading toward the cross centre, clipped to the
    # cross plate; plus the shared outline glow shown while any is pressed.
    cross = _paths(_P_DPAD)
    wedges = (("dU", _P_DPAD_UP, "v0"), ("dR", _P_DPAD_RIGHT, "h1"),
              ("dD", _P_DPAD_DOWN, "v1"), ("dL", _P_DPAD_LEFT, "h0"))
    for key, d, axis in wedges:
        groups = _parse_path(d)
        def draw(r, groups=groups, axis=axis):
            g = Image.linear_gradient("L")     # 256x256, black top -> white
            if axis == "v0":                    # strongest at the TOP edge
                g = g.transpose(Image.FLIP_TOP_BOTTOM)
            elif axis == "h0":                  # strongest at the LEFT edge
                g = g.transpose(Image.ROTATE_90).transpose(
                    Image.FLIP_LEFT_RIGHT)
            elif axis == "h1":                  # strongest at the RIGHT edge
                g = g.transpose(Image.ROTATE_90)
            fm = r.fill(groups, _ACC, 245, grad=g, clip=cross)
            r.glow_under(fm, _ACC, 3.0, 0.4)
        overlay(key, draw, groups, pad_su=5.0)

    def draw_dout(r):
        sm = r.stroke(cross, _WHITE, 235, 1.15)
        r.glow_under(sm, _ACC, 4.5, 0.66)
    overlay("dOut", draw_dout, cross)

    # trackpad touch outlines (outer + inset ring, viewer style)
    for key, poly, inner in (
            ("padoutL", pad_l_poly,
             [(False, [_PAD_L_XF(x, y) for (x, y) in _round_rect_pts(
                 _PAD_L_ORIGIN[0] + 4, _PAD_L_ORIGIN[1] + 4,
                 _PAD_SIZE - 8, _PAD_SIZE - 8, 13)])]),
            ("padoutR", pad_r_poly,
             [(False, [_PAD_R_XF(x, y) for (x, y) in _round_rect_pts(
                 _PAD_R_RECT[0] + 4, _PAD_R_RECT[1] + 4,
                 _PAD_SIZE - 8, _PAD_SIZE - 8, 13)])])):
        def draw(r, poly=poly, inner=inner):
            sm = r.stroke(poly, _ACC, 230, 1.2)
            r.stroke(inner, _ACC, 166, 0.9)
            r.glow_under(sm, _ACC, 3.5, 0.5)
        overlay(key, draw, poly, pad_su=6.0)

    # -- analog trigger fill levels ----------------------------------------
    for side, d in (("lt", _P_TRIG_L), ("rt", _P_TRIG_R)):
        groups = _paths(d)
        bx0, by0, bx1, by1 = _pts_bbox(groups)
        pad_su = 8.0
        bx0 -= pad_su; by0 -= pad_su; bx1 += pad_su; by1 += pad_su
        levels = [None]
        for k in range(1, _TRIG_LEVELS + 1):
            depth = k / _TRIG_LEVELS
            boundary = _TRIG_BOTTOM - (_TRIG_BOTTOM - _TRIG_TOP) * depth
            r = _Raster(sc, bx0, by0, (bx1 - bx0) * sc, (by1 - by0) * sc)
            box = r._bounds(groups, 3)
            fm = r._mask_fill(box, groups)
            # keep only the filled band: erase everything above the boundary
            cut_y = int(round(r.pt(bx0, boundary)[1])) - box[1]
            if cut_y > 0:
                fm.paste(0, (0, 0, fm.size[0], min(cut_y, fm.size[1])))
            r._blit(box, fm, _ACC, 255)
            r.glow_under((box, fm), _ACC, 4.0, 0.6)
            if k == _TRIG_LEVELS:      # full pull: crisp white outline pops
                r.stroke(groups, _WHITE, 250, 1.3)
            levels.append((r.done(), to_px(bx0, by0)))
        assets["trig"][side] = levels

    # -- stick position dots (anchor CENTRE; moved at runtime) --------------
    def dot_img(build):
        rad_su = _STICK_DOT_R + 10.0
        r = _Raster(sc, -rad_su, -rad_su, rad_su * 2 * sc, rad_su * 2 * sc)
        build(r)
        return r.done()

    dr = _STICK_DOT_R
    assets["stick"]["idle"] = dot_img(lambda r: (
        r.fill([(True, _ellipse_pts(0, 0, dr, dr))], _GREY, 226),
        r.stroke([(True, _ellipse_pts(0, 0, dr, dr))],
                 (240, 244, 248), 130, 0.8)))
    def _stick_moved(r, glow):
        ring = [(True, _ellipse_pts(0, 0, dr, dr))]
        fm = r.fill(ring, _ACC, 240)
        r.stroke(ring, _WHITE, 242, 1.35)
        r.glow_under(fm, _ACC, glow, 0.66)
    assets["stick"]["moved"] = dot_img(lambda r: _stick_moved(r, 4.0))
    assets["stick"]["pressed"] = dot_img(lambda r: _stick_moved(r, 7.0))

    # -- trackpad finger dots (anchor CENTRE) --------------------------------
    def pad_dot(build):
        rad_su = 26.0
        r = _Raster(sc, -rad_su, -rad_su, rad_su * 2 * sc, rad_su * 2 * sc)
        build(r)
        return r.done()

    assets["pad"]["dot"] = pad_dot(lambda r: (
        r.glow_under(r.fill([(True, _ellipse_pts(0, 0, 7, 7))],
                            (255, 255, 255), 255),
                     (255, 255, 255), 5.5, 0.7)))
    def _pad_pressed(r):
        ring = [(True, _ellipse_pts(0, 0, 16, 16))]
        fm = r.fill(ring, _ACC, 208)
        r.stroke(ring, _WHITE, 248, 1.25)
        r.fill([(True, _ellipse_pts(0, 0, 7, 7))], _WHITE, 250)
        r.glow_under(fm, _ACC, 6.0, 0.8)
    assets["pad"]["pressed"] = pad_dot(_pad_pressed)

    # runtime placement metadata (canvas px)
    steam_cx, steam_cy = to_px(_C_STEAM[0], _C_STEAM[1])
    assets["meta"] = {
        "stickL": to_px(_C_LRING[0], _C_LRING[1]),
        "stickR": to_px(_C_RRING[0], _C_RRING[1]),
        "travel": _STICK_TRAVEL * sc,
        "pad_to_px": lambda side, nx, ny: to_px(*_pad_local_to_svg(side, nx, ny)),
        # (cx, cy, r) of the drawn Steam-button circle, in canvas px  used by
        # the picker to hang a hover/click hotspot exactly over the glyph
        # (viewer credit; see keybinds_picker._bind_viewer_credit_hotspot).
        "steamBtn": (steam_cx, steam_cy, _C_STEAM[2] * sc),
    }
    return assets


# PIL-level cache (per canvas size) and Tk PhotoImage cache (per size; the
# picker keeps ONE persistent Tk interpreter, so master never changes between
# builds  a real shutdown clears everything via neuter()). _build_lock keeps
# a prewarm thread and the picker's build thread from rasterizing twice.
_pil_cache = {}
_photo_cache = {}
_build_lock = threading.Lock()


def _pil_assets(cw, ch):
    key = (cw, ch)
    with _build_lock:
        pil = _pil_cache.get(key)
        if pil is None:
            pil = _build_assets(cw, ch)
            _pil_cache[key] = pil
        return pil


def prewarm(cw=532, ch=410):
    """Rasterize the PIL-level art on the CALLING thread (the tray spawns a
    daemon thread for this right after startup) so the picker's warm build
    only has to wrap cached images into PhotoImages. Pure PIL  thread-safe.
    Safe to skip: _photo_assets builds synchronously when not prewarmed."""
    if Image is None:
        return
    try:
        _pil_assets(cw, ch)
    except Exception:
        pass


def _photo_assets(cw, ch, master):
    key = (cw, ch)
    ph = _photo_cache.get(key)
    if ph is not None:
        return ph
    pil = _pil_assets(cw, ch)

    def mk(img):
        return ImageTk.PhotoImage(img, master=master)

    ph = {
        "base": (mk(pil["base"][0]), pil["base"][1]),
        "over": {k: (mk(im), pos) for k, (im, pos) in pil["over"].items()},
        "trig": {s: [None] + [(mk(im), pos) for (im, pos) in lv[1:]]
                 for s, lv in pil["trig"].items()},
        "stick": {k: mk(im) for k, im in pil["stick"].items()},
        "pad": {k: mk(im) for k, im in pil["pad"].items()},
        "meta": pil["meta"],
    }
    _photo_cache[key] = ph
    return ph


def neuter():
    """App-shutdown cleanup on the picker's Tk thread: make every cached
    PhotoImage's __del__ a no-op (same 'image delete off-thread' crash the
    picker guards its own caches against), then drop the caches."""
    for ph in _photo_cache.values():
        try:
            imgs = [ph["base"][0]]
            imgs += [p for (p, _pos) in ph["over"].values()]
            for lv in ph["trig"].values():
                imgs += [p for e in lv if e for (p, _pos) in (e,)]
            imgs += list(ph["stick"].values()) + list(ph["pad"].values())
            for im in imgs:
                # These are PIL ImageTk.PhotoImages: their __del__ deletes
                # through a PRIVATE wrapped tkinter.PhotoImage, so clearing
                # `.name` on the PIL object does nothing. Drop the wrapper so
                # __del__ bails on its own AttributeError  otherwise a later
                # GC pass calls Tcl on a dead thread and hangs the exit.
                photo = getattr(im, "_PhotoImage__photo", None)
                if photo is not None:
                    try:
                        photo.name = None
                    except Exception:
                        pass
                    try:
                        delattr(im, "_PhotoImage__photo")
                    except Exception:
                        pass
                try:
                    im.name = None
                except Exception:
                    pass
        except Exception:
            pass
    _photo_cache.clear()
    _pil_cache.clear()
    set_active(False)


# ---------------------------------------------------------------------------
# the live viewer bound to one canvas
# ---------------------------------------------------------------------------
_BOOL_BITS = (
    ("a", _B_A), ("b", _B_B), ("x", _B_X), ("y", _B_Y),
    ("view", _B_VIEW), ("menu", _B_START), ("steam", _B_STEAM),
    ("qam", _B_QAM), ("lb", _B_LB), ("rb", _B_RB),
    ("lg1", _B_LG2), ("lg2", _B_LG1),        # upper paddle = LGRIP1 (L4)
    ("rg1", _B_RG2), ("rg2", _B_RG1),
    ("l3ring", _B_L3), ("r3ring", _B_R3),
    ("lgripT", _B_LGRIPT), ("rgripT", _B_RGRIPT),
    ("dU", _B_DUP), ("dR", _B_DRIGHT), ("dD", _B_DDOWN), ("dL", _B_DLEFT),
)


class _ZeroFrame:
    buttons = 0
    ltrig = rtrig = 0
    lstick_x = lstick_y = rstick_x = rstick_y = 0
    lpad_x = lpad_y = rpad_x = rpad_y = 0


_ZERO = _ZeroFrame()


class ScViewer:
    def __init__(self, canvas, cw, ch, master):
        if Image is None:
            raise RuntimeError("PIL unavailable")
        self.canvas = canvas
        self._ph = _photo_assets(cw, ch, master)
        self.meta = self._ph["meta"]   # public: layout metadata (see _build_assets)
        self._items = {}
        self._on = {}                 # key -> currently-shown bool
        self._trig_lv = {"lt": 0, "rt": 0}
        self._stick = {"L": None, "R": None}   # (variant, x, y) or None
        self._pad = {"L": None, "R": None}     # (variant, x, y) or None
        self._pad_out = {"L": False, "R": False}
        self._build_items()

    def _build_items(self):
        cv = self.canvas
        ph = self._ph
        img, (bx, by) = ph["base"]
        cv.create_image(round(bx), round(by), image=img, anchor="nw")
        for key, (im, (px, py)) in ph["over"].items():
            self._items[key] = cv.create_image(
                round(px), round(py), image=im, anchor="nw", state="hidden")
        for side in ("lt", "rt"):
            im, (px, py) = ph["trig"][side][1]
            self._items[side] = cv.create_image(
                round(px), round(py), image=im, anchor="nw", state="hidden")
        meta = ph["meta"]
        for sk, mkey in (("L", "stickL"), ("R", "stickR")):
            x, y = meta[mkey]
            self._items["stick" + sk] = cv.create_image(
                round(x), round(y), image=ph["stick"]["idle"],
                anchor="center", state="hidden")
        for pk in ("L", "R"):
            self._items["paddot" + pk] = cv.create_image(
                0, 0, image=ph["pad"]["dot"], anchor="center", state="hidden")

    # -- runtime -------------------------------------------------------------
    def apply(self, sci):
        """Diff `sci` against the shown state and update only what changed.
        Runs on the picker's Tk thread."""
        if sci is None:
            sci = _ZERO
        cv = self.canvas
        items = self._items
        b = sci.buttons

        for key, bit in _BOOL_BITS:
            now = bool(b & bit)
            if self._on.get(key) != now:
                self._on[key] = now
                cv.itemconfigure(items[key],
                                 state="normal" if now else "hidden")
        # dpad shared outline glow
        dout = bool(b & (_B_DUP | _B_DRIGHT | _B_DDOWN | _B_DLEFT))
        if self._on.get("dOut") != dout:
            self._on["dOut"] = dout
            cv.itemconfigure(items["dOut"],
                             state="normal" if dout else "hidden")

        # triggers: quantized analog pull (digital full-pull bit forces max)
        for side, analog, bit in (("lt", sci.ltrig, _B_LT),
                                  ("rt", sci.rtrig, _B_RT)):
            t = min(1.0, max(0.0, analog / 32767.0))
            lv = _TRIG_LEVELS if (b & bit) else int(round(t * _TRIG_LEVELS))
            if lv != self._trig_lv[side]:
                self._trig_lv[side] = lv
                if lv == 0:
                    cv.itemconfigure(items[side], state="hidden")
                else:
                    cv.itemconfigure(items[side],
                                     image=self._ph["trig"][side][lv][0],
                                     state="normal")

        # sticks
        meta = self._ph["meta"]
        for sk, (sx, sy, tbit, pbit, ck) in (
                ("L", (sci.lstick_x, sci.lstick_y, _B_LJOYT, _B_L3, "stickL")),
                ("R", (sci.rstick_x, sci.rstick_y, _B_RJOYT, _B_R3, "stickR"))):
            nx = max(-1.0, min(1.0, sx / 32767.0))
            ny = max(-1.0, min(1.0, -sy / 32767.0))
            moved = (nx * nx + ny * ny) > 0.0064          # 0.08 dead ring
            pressed = bool(b & pbit)
            touched = bool(b & tbit) or moved or pressed
            if not touched:
                st = None
            else:
                cxp, cyp = meta[ck]
                st = ("pressed" if pressed else "moved" if moved else "idle",
                      round(cxp + nx * meta["travel"]),
                      round(cyp + ny * meta["travel"]))
            if st != self._stick[sk]:
                prev = self._stick[sk]
                self._stick[sk] = st
                iid = items["stick" + sk]
                if st is None:
                    cv.itemconfigure(iid, state="hidden")
                else:
                    if prev is None or prev[0] != st[0]:
                        cv.itemconfigure(iid, image=self._ph["stick"][st[0]],
                                         state="normal")
                    cv.coords(iid, st[1], st[2])

        # trackpads
        for pk, (px_, py_, tbit, cbit, side) in (
                ("L", (sci.lpad_x, sci.lpad_y, _B_LPADT, _B_LPAD, "left")),
                ("R", (sci.rpad_x, sci.rpad_y, _B_RPADT, _B_RPAD, "right"))):
            touched = bool(b & tbit)
            clicked = bool(b & cbit)
            if self._pad_out[pk] != touched:
                self._pad_out[pk] = touched
                cv.itemconfigure(items["padout" + pk],
                                 state="normal" if touched else "hidden")
            if not touched:
                st = None
            else:
                nx = min(1.0, max(0.0, (px_ + 32767.0) / 65534.0))
                ny = min(1.0, max(0.0, (-py_ + 32767.0) / 65534.0))
                gx, gy = meta["pad_to_px"](side, nx, ny)
                st = ("pressed" if clicked else "dot", round(gx), round(gy))
            if st != self._pad[pk]:
                prev = self._pad[pk]
                self._pad[pk] = st
                iid = items["paddot" + pk]
                if st is None:
                    cv.itemconfigure(iid, state="hidden")
                else:
                    if prev is None or prev[0] != st[0]:
                        cv.itemconfigure(iid, image=self._ph["pad"][
                            "pressed" if st[0] == "pressed" else "dot"],
                            state="normal")
                    cv.coords(iid, st[1], st[2])


def attach(canvas, cw, ch, master):
    """Build (or reuse cached) art for a cw x ch canvas and attach a live
    viewer to it. Raises if PIL is unavailable  the caller falls back to the
    static PNG."""
    return ScViewer(canvas, cw, ch, master)


# ---------------------------------------------------------------------------
# standalone demo: `python sc_viewer.py` opens a window and animates a
# synthetic input sweep (no controller / tray needed).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tkinter as tk
    from types import SimpleNamespace

    root = tk.Tk()
    root.title("sc_viewer demo")
    root.configure(bg="#0e141b")
    cw, ch = 532, 410
    cv = tk.Canvas(root, width=cw, height=ch, bg="#0e141b",
                   highlightthickness=0)
    cv.pack(padx=20, pady=20)
    t0 = time.monotonic()
    v = attach(cv, cw, ch, root)

    _bits = [bit for _k, bit in _BOOL_BITS]

    def tick():
        t = time.monotonic() - t0
        i = int(t * 2) % (len(_bits) + 4)
        btn = _bits[i] if i < len(_bits) else 0
        ang = t * 2.2
        pulse = (math.sin(t * 1.7) + 1) / 2
        sci = SimpleNamespace(
            buttons=btn | _B_LJOYT | _B_RJOYT | _B_LPADT | _B_RPADT
                    | (_B_LPAD if pulse > 0.8 else 0),
            ltrig=int(32767 * pulse), rtrig=int(32767 * (1 - pulse)),
            lstick_x=int(30000 * math.cos(ang)),
            lstick_y=int(30000 * math.sin(ang)),
            rstick_x=int(30000 * math.cos(-ang * 1.3)),
            rstick_y=int(30000 * math.sin(-ang * 1.3)),
            lpad_x=int(20000 * math.cos(ang * 0.7)),
            lpad_y=int(20000 * math.sin(ang * 0.7)),
            rpad_x=int(20000 * math.cos(-ang)),
            rpad_y=int(20000 * math.sin(-ang)),
        )
        v.apply(sci)
        root.after(33, tick)

    tick()
    root.mainloop()
