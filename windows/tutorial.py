"""First-run tutorial  a short, controller-driven tour of the features a new
user would otherwise never find.

Why this exists: every headline feature here is a CHORD (Guide held + another
control). None of them announce themselves, and a tray icon plus a settings
window teaches none of them. So the first launch runs this: a handful of short
slides that SHOW the input as real Steam Input glyph art, point an arrow at
what it produces, and  for the ones that can be pressed  ask the user to
actually do it, confirming the press live off the controller's own frames.
Pressing a chord once, with feedback, is what makes it stick; reading a list of
chords is not.

Shape of the thing. Two owned Toplevels float over the picker, exactly like the
cog-wheel settings modal (see _Picker._open_cog_modal): a dark SCRIM that dims
the window behind it, and a solid PANEL holding the slide. It is driven by the
picker's OWN navigation dispatch  _nav_dispatch hands every press here first
while a tutorial is up  so the controller and the keyboard steer it with no
extra input plumbing, and every glyph hint matches the pad actually in hand.

Detection reads the SAME published frames the picker navigates from
(sc_viewer.latest / latest_nav), so a press counts whether it arrived on the
Steam Controller's HID channel or from any SDL pad. The chords themselves are
resolved from the LIVE Chords-tab binds rather than hardcoded, so the tutorial
teaches what THIS user's controller actually does  after a rebind too, and on
the kinds whose defaults differ from the Steam Controller's (a Switch Pro has
Alt-Tab on "+" and no keyboard chord at all until one is bound). A step whose
action isn't bound on the current pad degrades to a "not bound yet" note
instead of asking for a press that could never land.

Two slides need something to exist before they can be tried, and the tour
supplies it for exactly as long as that slide is up: the keyboard slide opens
the real OSK, and the Virtual Menus slide installs a throwaway menu of its own
(see _demo_vmenu) because a new user has not built one yet. Both are put back
the way they were on the next slide change AND on close, by _settle_slide  no
tour exit path may leave the user's own configuration altered.

Nothing here is required: Next always advances, and "Skip Tutorial" is on every
slide. Finishing OR skipping persists `tutorial_done`, so it never reappears
uninvited  Options > General has a "Show Tutorial" button to replay it.
"""

import ctypes
import math
import os
import random
import sys
import time

import tkinter as tk

# The picker is always fully imported before this module is (it imports us
# lazily, from the method that opens the tutorial), so this can't be circular.
import keybinds_picker as kp
import keybinds_runtime
import pads

# The media slide's silent demo track  a real system media session, so
# the transport keys land on US and not on the user's own music (Windows:
# SMTC; Linux: MPRIS  the two media_demo.py files hard-fork). Optional on
# purpose: the slide is fully usable without it, so a missing projection
# package or session bus costs the tour nothing but the now-playing card.
try:
    import media_demo
except Exception as _e:                                  # pragma: no cover
    print(f"tutorial: media demo unavailable: {_e!r}")
    media_demo = None

# Palette / font aliases  one place to follow the picker's theme.
_BG = kp._BG
_FG = kp._FG
_MUTED = kp._MUTED
_ACCENT = kp._ACCENT
_GREEN = kp._GREEN
_ORANGE = kp._ORANGE
_PURPLE = kp._PURPLE
_PINK = kp._PINK
_TEAL = kp._TEAL
_PANEL_BOX = kp._PANEL_BOX
_FIELD = kp._DROPDOWN_FIELD
# The inset boxes drawn ON the stage card (the tray strip, the keyboard, the
# welcome slide's three columns) and their hairline edge. Darker than the card
# they sit on, so a picture-inside-the-picture reads as one.
_CARD = "#171b22"
_CARD_EDGE = "#2c333d"
_OUTLINE = kp._OUTLINE
_FONT = kp._FONT_FAMILY

# --- geometry ---------------------------------------------------------------
# The panel is a fixed card centred on the picker, clamped so it always leaves
# a margin of window visible around it (the scrim then reads as "the app,
# dimmed" rather than "a second window").
_PANEL_W_MAX = 940
_PANEL_H_MAX = 646
_PANEL_MARGIN = 36
_PANEL_W_MIN = 560
_PANEL_H_MIN = 430

_PAD_X = 30              # panel inner side padding
_STAGE_H = 318           # the illustration card
_TASK_ROW_H = 27         # one "try it" checklist row
_CHIP = 56               # button chip: a 28px glyph inside a rounded key cap
_GLYPH_PX = 28           # the baked glyph art's native size

# Scrim opacity. Dark enough that the slide is unambiguously the subject,
# light enough that the user still sees which window they're in.
_SCRIM_ALPHA = 0.72

# --- checklist animation ----------------------------------------------------
# The "try it" rows are the one part of the tour the user is meant to ACT on,
# so they get motion: pending rows breathe to pull the eye, and a step landing
# pops, rings and (on the last one) throws confetti. Purely canvas vector work
#  the PIL text is NOT re-rendered per frame (_text_photo is uncached and
# costs a full TrueType raster per call), so _paint_task draws the labels once
# into the static layer and _paint_task_anim redraws ONLY tagged primitives on
# top. See _anim_sync for how the loop starts and, more importantly, stops.
_ANIM_TAG = "anim"       # canvas tag for everything the loop redraws
_POP_S = 0.46            # tick-circle pop + its burst ring
_PILL_S = 0.26           # completed row's background tint fading in
_CONFETTI_S = 1.10       # all-steps-done particle burst
_BAR_S = 0.42            # progress bar easing to a new value
_ANIM_MS = 16            # ~60 Hz while something is actually moving
_IDLE_MS = 50            # ~20 Hz when only the pending-row breathing is live
# ~30 Hz for a slide whose ILLUSTRATION animates (the welcome slide's joystick
# and paddles  see _paint_stage_anim). Between the two rates above on purpose:
# the motion is continuous and never stops, so it must look smooth without
# costing a 60 Hz redraw for as long as the user sits on the slide.
_STAGE_MS = 33
_BREATHE_S = 1.9         # one full pending-row breath
# A step whose own action hides this overlay (Alt-Tab takes the foreground; the
# on-screen keyboard covers the band) marks itself "defer": its tick, its pop
# and the confetti are all held back until the tour is genuinely back on screen
#  otherwise the payoff plays to nobody and the user returns to a checklist
# that silently finished itself. Then this much settle time on top, so a slow
# machine finishes repainting before the animation starts rather than dropping
# the first half of it. See _anim_ready / _release_deferred.
_DEFER_S = 0.65
# Row tint behind a completed step, and the track the progress bar sits in.
_ROW_DONE_BG = kp._lerp_color(_BG, _GREEN, 0.16)
_BAR_TRACK = "#2c333d"
_CONFETTI_COLORS = (_GREEN, _ACCENT, _ORANGE, _PINK, _TEAL, "#ffffff")

# --- input ------------------------------------------------------------------
# Steam/Home (0x10000) and "..."/QAM (0x10)  the two buttons that open the
# chord layer. Same pair the tray and the picker's nav pump use.
_GUIDE_BITS = 0x10000 | 0x10
# Stick deflection that counts as a direction, matching the tray's own
# ARROW_DEADZONE so a press the tutorial accepts is a press the tray fires.
_STICK_DEADZONE = 14000

# Control ids that have no entry of their own in chord_buttons_for() because
# the chord vocabulary names them differently from the layout tabs.
_BIT_ALIAS = {"lstick_click": "l3", "rstick_click": "r3"}

# Guide/Home control id per kind  the button HELD to reach the chord layer.
# The HID-takeover families print it "Steam"; everything else calls it Home.
_GUIDE_CID = {"sc": "steam", "sc2015": "steam", "steam_deck": "steam"}

# Praise shown when a step's presses are all in. Deliberately short and varied
#  the point is a beat of "yes, that was it", not a paragraph.
_PRAISE = ("Nice!", "That's it!", "Perfect.", "You've got it.", "Exactly.")

# Every Guide/Steam chip drawn anywhere in the tour uses THIS glyph  the
# Switch Pro Controller's Home icon  regardless of which pad is actually
# connected. See _chip's override.
_GUIDE_ICON_KIND = "switch"
_GUIDE_ICON_CID = "home"


def _ease_out(t):
    """Cubic ease-out on [0,1]  fast off the mark, gently arriving. Every
    checklist animation uses it, so they all decelerate the same way."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _guide_cid(kind):
    return _GUIDE_CID.get(kind, "home")


def _bit_for(kind, cid):
    """Button bit for a control id on `kind`, or None when that pad has no
    such control (a lone Joy-Con has one stick click, the 2015 Steam
    Controller has no right stick at all)."""
    if not cid:
        return None
    cid = _BIT_ALIAS.get(cid, cid)
    try:
        attr = keybinds_runtime.chord_buttons_for(kind).get(cid)
    except Exception:
        return None
    return kp._SCB_NAME_TO_BIT.get(attr) if attr else None


def _cid_for_action(picker, kind, action):
    """Which control currently carries `action` on `kind`'s Chords tab, or None.

    Reads the LIVE dropdown vars, so a user who moved "Open Keyboard" onto a
    grip is taught the grip. The layout DEFAULT for the action is checked
    first, so the common (untouched) case can't be beaten to the answer by
    some other control that happens to hold the same value."""
    try:
        _labels, v2l, _l2v = kp._actions_for(kind, "guide")
        want = v2l.get(action)
        if not want:
            return None
        live = (picker._vars.get(kind) or {}).get("guide") or {}
        for cid, val in kp.default_binds(kind, "guide").items():
            if val == action and cid in live and live[cid].get() == want:
                return cid
        for cid, var in live.items():
            if var.get() == want:
                return cid
    except Exception:
        pass
    return None


def _stick_zone(x, y):
    """The tray's own stick-direction quantisation (_stick_zone_dispatch)."""
    if abs(x) <= _STICK_DEADZONE and abs(y) <= _STICK_DEADZONE:
        return "NEUTRAL"
    if abs(y) >= abs(x):
        return "UP" if y > 0 else "DOWN"
    return "RIGHT" if x > 0 else "LEFT"


def _haptic():
    """One feedback tick on the active controller. Entirely optional  the
    tutorial must work with no pad, no adusk, no rumble."""
    try:
        from adusk import state as adusk_state
        adusk_state.haptic_tick()
    except Exception:
        pass


# --- the on-screen keyboard, from the tutorial's point of view ---------------
# The OSK is an always-on-top window that parks over the bottom of the screen 
# i.e. squarely over this overlay's checklist and its Skip/Previous/Next row.
# So the tour owns it: it may be open ONLY while the keyboard slide is showing.
# The slide's second "try it" step has the user close it themselves (a bare B
# press  see feed()'s "close" check and nav_press's _close_task_pending
# guard), and _settle_slide is the backstop for every other way of leaving 
# a user who already had the keyboard up before starting the tour, one left
# open by an earlier slide, or Next pressed before the close step lands.
# How long the user gets on the demo window before the tour pulls itself back
# in front. Long enough to read the "you switched windows" graphic that just
# appeared there, short enough that nobody wonders whether the tour is over.
_RETURN_MS = 2600
# How long a fully-checked-off slide sits still before the tour pages itself
# onward. Starts counting from the celebration actually PLAYING (confetti
# seeded  see _arm_auto_advance's two call sites), not from the moment the
# step technically landed, so a deferred slide (Alt-Tab, the keyboard) gets
# the same read-it-and-see-it time as an immediate one. Long enough to read
# the praise line and watch the burst land; short enough that it doesn't feel
# like the tour is stuck. A manual Next/Previous/Skip cancels it outright 
# see _go's _cancel_afters, which this timer is armed through like any other.
_AUTO_ADVANCE_MS = 2400
# The switch-windows slide's own demo window (see _open_alt_window): the thing
# the user is asked to Alt-Tab TO, so the step has a target that definitely
# exists and a landing that can be told apart from a stray click. Big enough to
# read across the room, small enough to look like a window rather than a
# takeover. _ALT_ARM_MS is how long the focus watch stays deaf after building
# it  creating a window activates it, and that must not count as the switch.
_ALT_WIN_W = 520
_ALT_WIN_H = 340
_ALT_ARM_MS = 700
# ...and how long it gets on screen (behind the tour's own panel, so invisibly)
# before it is minimised, so DWM has a composited frame to use as its Alt-Tab
# thumbnail. Without it the switcher offers a generic placeholder.
_ALT_MINIMISE_MS = 260
# How many times the welcome slide re-tries taking the manager away when the
# picker is mid-transition and drops the request (see _hide_gui).
_GUI_HIDE_TRIES = 25
# How long after the manager is revealed the tour refuses to park itself.
# The reveal does not win the OS foreground immediately (focus-stealing
# prevention: _force_os_foreground + a re-assert 80ms later), and every tick
# in that gap looks exactly like "the user switched to another app".
_REVEAL_GRACE_S = 1.5


def _osk_is_open():
    """Whether the tray currently has the on-screen keyboard up. Published on
    the same picker <-> tray channel navigation rides (sc_viewer)."""
    scv = getattr(kp, "_scv", None)
    fn = getattr(scv, "osk_open", None) if scv is not None else None
    try:
        return bool(fn()) if fn is not None else False
    except Exception:
        return False


def _typed_text(labels):
    """Turn the keyboard's echoed key LABELS into the line to show back.

    Only the keys that would put something in a text box count: a single
    character is itself, Space is a space, Backspace rubs one out. Everything
    else (Shift, Caps, Enter, the arrows, Move) is a keyboard control, not a
    letter, and is skipped rather than spelled out  the box is meant to look
    like what the user typed, not like a log of what they pressed."""
    out = []
    for label in labels:
        if label == "Backspace":
            if out:
                out.pop()
        elif label == "Space":
            out.append(" ")
        elif len(label) == 1:
            out.append(label)
    return "".join(out)


def _close_osk():
    """Ask the keyboard to tear down at the end of its next frame  the same
    signal the tray's own close path sends (adusk_state.close). Safe to call
    when it isn't open, and from any thread: it only sets an Event."""
    try:
        from adusk import state as adusk_state
        adusk_state.close()
    except Exception:
        pass


# --- the tour's own virtual menu ---------------------------------------------
# The Virtual Menus slide is the one feature that can't be demonstrated with a
# chord alone: a menu only exists if somebody has BUILT one, and a new user by
# definition hasn't. So the tour ships its own  a single-button 3x3 touch grid
# on Guide + DPad Up  installs it for the length of that slide, and takes it
# straight back out again.
#
# It is deliberately NOT written to settings.json: _install_demo_vmenu swaps
# the live list held in adusk.state (which is what the tray's dispatcher reads,
# version-gated, so the swap takes effect on its next frame) and
# _restore_vmenus puts the user's own list back. Nothing the user has built is
# touched, and a tour that is killed mid-slide still restores on close().
#
# The icon is a bundled asset (data/images/vmenu_icons) rather than a
# `custom:` upload so it ships with the build and can't go missing; it is
# deliberately absent from VMENU_ICON_GROUPS, so it renders here but is never
# offered in the picker's icon browser.
_GABEN_ICON = "tutorial_gaben"
_DEMO_VMENU_NAME = "Tutorial Demo Menu"
# Centre cell of the 3x3 grid: the one a resting thumb already highlights, so
# the ask is "click the pad", not "hunt for a corner".
_DEMO_VMENU_SLOT = 4


def _demo_vmenu():
    """The tour's throwaway menu, in settings shape (see vmenus_sanitize)."""
    entries = [{"icon": "none", "action": "none", "actions": []}
               for _ in range(9)]
    entries[_DEMO_VMENU_SLOT] = {"icon": _GABEN_ICON, "action": "none",
                                 "actions": []}
    return {
        "name": _DEMO_VMENU_NAME, "type": "touch",
        "pad": "guide", "pad2": "dpad_up",       # hold Guide + DPad Up
        "key": "none", "key2": "none",
        "enabled": True, "activate": "toggle",
        "hpos": 90, "vpos": 76, "size": 100, "opacity": 90,
        "entries": entries,
    }


def _get_vmenus():
    try:
        from adusk import state as adusk_state
        return list(adusk_state.get_virtual_menus())
    except Exception:
        return None


def _set_vmenus(menus):
    try:
        from adusk import state as adusk_state
        adusk_state.set_virtual_menus(menus)
        return True
    except Exception:
        return False


def _vmenu_fire():
    """(sequence, icon) of the last virtual-menu entry fired, or (0, None)."""
    scv = getattr(kp, "_scv", None)
    fn = getattr(scv, "vmenu_fire", None) if scv is not None else None
    try:
        return fn() if fn is not None else (0, None)
    except Exception:
        return (0, None)


def _gyro_off():
    """Drop gyro-to-mouse on every controller.

    The gyro slide's checklist has the user switch it ON and then OFF again 
    it's a LATCHING toggle, and left on the pointer keeps drifting with the pad
    for the rest of the tour, which (since mouse hover moves this overlay's
    button focus  see Tutorial._hover) can silently walk the focus onto "Skip
    Tutorial" under the user's next A press. The second checklist step is the
    normal way that gets undone; this is the backstop for every path that
    skips it  Next pressed before the off step lands, or the slide replayed
    later  so the tour is never the reason gyro is left running."""
    try:
        from adusk import state as adusk_state
        for kind in list(adusk_state.get_gyro_mouse_kinds()):
            adusk_state.set_gyro_mouse(kind, False)
    except Exception:
        pass


def _identify_window(win, title):
    """Give an owned Toplevel a real title and the app's own icon.

    The scrim and panel are overrideredirect (no title bar), so Windows
    normally never surfaces them anywhere a title/icon would even be seen 
    but when it does (Alt-Tab rebuilding its list while another app is
    focused, say), they should read "Tutorial" with the real icon instead of
    a nameless "tk" window and the default feather.

    Sized exactly via LoadImageW rather than Tk's iconbitmap(), which scales
    the source .ico to whatever size it feels like and comes out blurry 
    mirrors keybinds_picker._set_window_icon, which solves the same problem
    for the main manager window. Returns the (small, big) HICON handles so
    the caller can keep them referenced (GDI resources, not Python-GC'd, but
    kept alive the same defensive way the picker does)."""
    try:
        win.title(title)
    except tk.TclError:
        pass
    if sys.platform != "win32":
        return None
    try:
        base = getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(__file__)))
        ico = os.path.join(base, "data", "images", "app_icon.ico")
        if not os.path.isfile(ico):
            return None
        u = ctypes.windll.user32
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = [ctypes.c_void_p]
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                 ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_uint]
        u.SendMessageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p]
        u.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        hwnd = u.GetParent(win.winfo_id())
        u.SetWindowTextW(hwnd, title)
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        # SM_CXSMICON/CYSMICON = 49/50 ; SM_CXICON/CYICON = 11/12  the exact
        # pixel sizes Windows wants for the title bar vs. taskbar/Alt-Tab.
        cxs, cys = u.GetSystemMetrics(49), u.GetSystemMetrics(50)
        cx, cy = u.GetSystemMetrics(11), u.GetSystemMetrics(12)
        small = u.LoadImageW(None, ico, IMAGE_ICON, cxs, cys, LR_LOADFROMFILE)
        big = u.LoadImageW(None, ico, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if small:
            u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        if big:
            u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        return (small, big)
    except Exception:
        return None


def _root_hwnd(win):
    """The OS window behind a Tk toplevel (winfo_id is its child frame)."""
    if sys.platform != "win32":
        return None
    try:
        u = ctypes.windll.user32
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        return u.GetAncestor(ctypes.c_void_p(win.winfo_id()), 2)  # GA_ROOT
    except Exception:
        return None


def _make_alt_tabbable(win):
    """Force a Toplevel into the Alt-Tab list.

    A Tk Toplevel created with a master is an OWNED window, and Windows hides
    owned windows from Alt-Tab and the taskbar unless they say otherwise. The
    switch-windows slide's demo window is a real target the user is asked to
    Alt-Tab TO, so it has to be there: WS_EX_APPWINDOW says "list me", and
    WS_EX_TOOLWINDOW (the opposite instruction) is cleared in case Tk set it.
    The style has to be applied while the window is HIDDEN  Windows only
    re-reads it when the window is next shown."""
    if sys.platform != "win32":
        return
    hwnd = _root_hwnd(win)
    if not hwnd:
        return
    try:
        u = ctypes.windll.user32
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.SetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_long]
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                         (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
    except Exception as e:
        print(f"tutorial: alt-tab styling failed: {e!r}")


def _dark_caption(win):
    """Give a Toplevel the app's dark title bar. The manager does this to
    itself at startup (_Picker._set_titlebar_color) and the demo window is the
    only other titled window the app ever shows  a stock white caption on it
    reads as a different program's window, which is the one thing this window
    must not look like."""
    if sys.platform != "win32":
        return
    hwnd = _root_hwnd(win)
    if not hwnd:
        return
    try:
        dwm = ctypes.windll.dwmapi

        def _set(attr, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr,
                                      ctypes.byref(v), ctypes.sizeof(v))

        _set(20, 1)            # immersive dark mode (Win10 20H1+)
        _set(19, 1)            # ...and its pre-20H1 attribute id
        _set(35, 0x00000000)   # DWMWA_CAPTION_COLOR (Win11)
        _set(36, 0x00FFFFFF)   # DWMWA_TEXT_COLOR
        _set(3, 1)             # no DWM open/close animation
    except Exception as e:
        print(f"tutorial: dark caption failed: {e!r}")


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint), ("ptMinPosition", _POINT),
                ("ptMaxPosition", _POINT), ("rcNormalPosition", _RECT)]


def _show_minimized(win, restore=None):
    """Map a window STRAIGHT INTO the taskbar, minimised and unfocused.

    The switch-windows slide's demo window is a thing to Alt-Tab TO, and a
    window already sitting on screen is a poor target: the user can see it, so
    "switch to it" is satisfied by looking. Minimised, it exists only as a
    taskbar button and an Alt-Tab entry, and restoring it IS the switch.

    SW_SHOWMINNOACTIVE (7), not iconify(): Tk's iconify maps-then-minimises,
    which flashes the window on screen and steals the foreground on the way
    (and taking it back flashed the manager over the tour). Returns False when
    it couldn't be done that way, so the caller can fall back.

    `restore`  an (x, y, w, h) the window should occupy when the user brings
    it back  is applied in the SAME call rather than by moving the window
    first. That is the whole point of going through SetWindowPlacement here:
    the demo window spends its pre-minimise moment parked under the tour's own
    panel (see _open_alt_window), and moving it to its real spot while it is
    still mapped would put it on screen for exactly the frame this is trying
    to avoid. rcNormalPosition is the restored rect, so setting it alongside
    showCmd hands Windows both facts at once and nothing is ever drawn in
    between."""
    if sys.platform != "win32":
        return False
    hwnd = _root_hwnd(win)
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        if restore is None:
            u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.ShowWindow(ctypes.c_void_p(hwnd), 7)  # SW_SHOWMINNOACTIVE
            return True
        u.GetWindowPlacement.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(_WINDOWPLACEMENT)]
        u.GetWindowPlacement.restype = ctypes.c_int
        u.SetWindowPlacement.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(_WINDOWPLACEMENT)]
        u.SetWindowPlacement.restype = ctypes.c_int
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if not u.GetWindowPlacement(ctypes.c_void_p(hwnd), ctypes.byref(wp)):
            u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.ShowWindow(ctypes.c_void_p(hwnd), 7)
            return True
        x, y, w, h = restore
        wp.showCmd = 7                              # SW_SHOWMINNOACTIVE
        wp.rcNormalPosition = _RECT(int(x), int(y), int(x + w), int(y + h))
        if not u.SetWindowPlacement(ctypes.c_void_p(hwnd), ctypes.byref(wp)):
            u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.ShowWindow(ctypes.c_void_p(hwnd), 7)
        return True
    except Exception as e:
        print(f"tutorial: minimised show failed: {e!r}")
        return False


def _show_no_activate(win):
    """Map a window WITHOUT giving it the foreground.

    Tk's deiconify() maps with SW_SHOWNORMAL, which activates  and an
    activation here costs a visible flicker: the tour would have to take the
    foreground back (_bring_front), which flashes the manager over the
    -topmost tour panel on its way. SW_SHOWNOACTIVATE shows the window,
    leaves focus exactly where it was, and Tk still reports it "normal"
    (verified, including that later geometry changes keep working).

    The window is still perfectly focusable afterwards  it just starts at the
    back of the Alt-Tab order rather than the front, which for the slide that
    asks the user to cycle to it is arguably the honest place to start.
    Returns False when it couldn't be done that way (non-Windows), so the
    caller can fall back to a plain deiconify."""
    if sys.platform != "win32":
        return False
    hwnd = _root_hwnd(win)
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.ShowWindow(ctypes.c_void_p(hwnd), 4)      # SW_SHOWNOACTIVATE
        return True
    except Exception as e:
        print(f"tutorial: no-activate show failed: {e!r}")
        return False


def _window_at(x, y):
    """The top-level window under a screen point, or None."""
    if sys.platform != "win32":
        return None
    try:
        u = ctypes.windll.user32
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        hit = u.WindowFromPoint(_PT(int(x), int(y)))
        return u.GetAncestor(ctypes.c_void_p(hit), 2) if hit else None
    except Exception:
        return None


def _foreground_hwnd():
    if sys.platform != "win32":
        return None
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        return u.GetForegroundWindow()
    except Exception:
        return None


def _claim(on):
    """Tell the tray a tour is (not) on screen. While claimed, an OSK open
    keeps the foreground on our own window instead of handing it back to the
    user's last app  see sc_viewer.tutorial_claimed."""
    scv = getattr(kp, "_scv", None)
    fn = getattr(scv, "set_tutorial", None) if scv is not None else None
    try:
        if fn is not None:
            fn(on)
    except Exception:
        pass
    if not on:
        _nav_keep(0)


def _nav_keep(mask):
    """Ask the tray to spare `mask` from the picker's navigation mask.

    The keyboard slide teaches the bare-X open, which is a NAV button while the
    manager is foreground  masked out of dispatch, so it would open nothing.
    This punches one bit through for as long as that step is outstanding; see
    sc_viewer.set_nav_keep for the whole story. Cleared on every slide change
    (_arm_slide re-publishes) and on close (above), so the hole can never
    outlive the slide that needs it."""
    scv = getattr(kp, "_scv", None)
    fn = getattr(scv, "set_nav_keep", None) if scv is not None else None
    try:
        if fn is not None:
            fn(mask)
    except Exception:
        pass


class Tutorial:
    """The overlay. One instance lives on the picker as `_tutorial` while it's
    up; `close()` drops it and the picker resumes normal navigation."""

    def __init__(self, picker):
        self.p = picker
        self._imgs = []          # PhotoImage refs (Tk GCs anything unreferenced)
        # Rendered text by (text, size, fg, bg, font)  see _text_img. Holds
        # its own strong refs, so anything drawn through it needs no _imgs
        # entry to stay alive.
        self._txt_cache = {}
        self._scrim = None
        self._panel = None
        self._idx = 0
        self._slides = []
        self._done = {}          # slide id -> set of satisfied check ids
        self._praise = {}        # slide id -> the praise line drawn for it
        # Index into self._btns  permanently empty now (see _build_chrome):
        # nothing in the footer is dpad-focusable any more, Skip and Next both
        # being mouse-only. Left in place rather than torn out, as the one
        # extension point a future focusable footer control would need.
        self._focus = -1
        self._btns = []
        self._nxt = self._prv = self._skp = None  # set by _build_chrome
        self._prev_btn = {"sc": 0, "sdl": 0}
        self._zone_prev = {"sc": "NEUTRAL", "sdl": "NEUTRAL"}
        # Latest left stick + buttons per channel, with the time it arrived 
        # what lets a slide DRAW the pad in the user's hands rather than a
        # diagram of one (the media slide's live thumbstick, see _stick_state).
        # Per channel because two pads can be publishing at once; the one being
        # moved wins.
        self._live_stick = {}
        self._poll_aid = None
        self._pulse_aid = None
        self._pulse_on = False
        self._hidden = False     # parked while another app is foreground
        # One tick of grace before parking, plus a longer window of it right
        # after the manager is revealed  see _poll_foreground.
        self._park_pending = False
        self._no_park_until = 0.0
        self._geom = None        # (w, h) the current slide was laid out at
        self._kind = None        # controller kind the slides were built for
        # One-shot timers armed by a completed step: the demo keyboard's
        # auto-close and the come-back-from-Alt-Tab raise. Cancelled on close
        # (and superseded rather than stacked).
        self._after_aids = []
        # A transient line under the checklist, set by a follow-up action so it
        # explains ITSELF ("the keyboard put itself away") rather than looking
        # like the tour misbehaving. Cleared on every slide change.
        self._note = None
        # The user's own virtual menus, held while the Virtual Menus slide has
        # its demo menu installed in their place (None = nothing borrowed).
        self._vmenus_saved = None
        # Last virtual-menu fire sequence we've seen, so a press is counted
        # once and a fire from BEFORE the slide opened is never counted.
        self._vmenu_seq = 0
        # --- checklist animation (see _anim_sync) ---
        self._anim_aid = None      # the loop's after id, None while stopped
        self._anim_t0 = time.monotonic()   # phase base for the idle breathing
        self._anim_pop = {}        # check id -> monotonic time it landed
        self._anim_done_t = None   # ...and when the LAST one did (confetti)
        self._confetti = []        # particles, seeded once per completion
        # Progress bar: eased from _bar_from to _bar_target starting at
        # _bar_t0, so its motion is time-based and frame-rate independent.
        self._bar_from = 0.0
        self._bar_target = 0.0
        self._bar_t0 = 0.0
        # Row geometry stashed by _paint_task for the animation layer to reuse
        #  recomputing it would re-measure the labels, and every measurement
        # is a PIL text raster (see the _ANIM_TAG comment).
        self._task_geom = None
        # The same trick for a slide whose ILLUSTRATION moves: its art function
        # publishes the coordinates its animated parts live at, and the loop
        # redraws only those (see _paint_stage_anim). None = this slide's
        # picture is entirely static.
        self._stage_geom = None
        # Every button chip currently drawn on the stage, as
        # (bits, cx, cy, size, colour, glyph)  so the animated layer can light
        # the ones the user is actually holding (_paint_chips_held). Rebuilt by
        # each stage paint, because the chips ARE the stage.
        self._chips = []
        # The stage height _render last laid the slide out at. Repaints that
        # happen BETWEEN renders (the media card arriving, typed letters)
        # must use the same number: measuring the canvas instead can catch
        # it a pixel or two off mid-resize, and every proportional
        # coordinate on the slide then shifts as the new element appears.
        self._stage_h = None
        # HICON handles from _identify_window, kept referenced defensively 
        # see that function's docstring.
        self._icon_handles = []
        # Steps that have LANDED but whose celebration is still waiting for
        # the tour to be back on screen (see _DEFER_S). They read as not-yet-
        # done everywhere the user can see, so nothing spoils the reveal.
        self._defer_pop = []
        self._defer_finish = False   # ...and the confetti waits with them
        self._defer_at = None        # monotonic deadline once we're visible
        # Last seen on-screen-keyboard state, for the falling edge that lands
        # the keyboard slide's "Close the keyboard" step (_poll_osk_close).
        self._osk_open_prev = _osk_is_open()
        # App-icon PhotoImages by (px, bg)  the welcome slide draws the real
        # tray icon on every repaint of that slide, and a resize repaints.
        self._icon_cache = {}
        # The media slide's silent demo track: whether it's ours right now, and
        # the last (track, playing) the stage was painted for  see _poll_media.
        self._media_on = False
        self._media_seen = None
        # The keyboard slide: whether the OSK is currently on our "small"
        # override, whether adusk is echoing typed keys, and what that echo has
        # spelled so far (drawn in the slide's own box  see _draw_typed).
        self._osk_small = False
        # The keyboard slide's third row goes green once the demo keyboard has
        # actually been put away (see _poll_osk_close); it has no checklist
        # step of its own to key off.
        self._osk_shut = False
        self._typing_watch = False
        self._typed = ""
        self._typed_seen = 0
        # The switch-windows slide's demo window: the Toplevel and its canvas,
        # whether the focus watch is armed yet (creating a window activates it
        #  see _open_alt_window), whether it currently HAS the foreground, and
        # its own PhotoImage refs (a separate canvas from the tour's).
        self._alt_win = None
        self._alt_canvas = None
        self._alt_armed = False
        self._alt_seen = False
        self._alt_focus = False
        self._alt_imgs = []
        # True while the welcome slide has the manager put away (_hide_gui):
        # the panel floats alone, the scrim is withdrawn, and the park logic
        # in _poll_foreground stands down until the window is back.
        self._gui_hidden = False

    # -- lifecycle -----------------------------------------------------------

    def open(self):
        """Build the scrim + panel and show the first slide. Returns False if
        the picker's geometry can't be trusted yet (never mapped), in which
        case the caller should try again once it is."""
        try:
            rx, ry, rw, rh = self.p._anchor_rect()
        except tk.TclError:
            return False
        self._kind = self._hint_kind()
        self._slides = self._build_slides(self._kind)
        _claim(True)
        # A keyboard that is ALREADY up (the user opened it before replaying
        # the tour, or the first-run fallback path opened it) is always-on-top
        # and would sit over the whole tutorial. Start from a clear screen 
        # slide 2 is where it gets opened, deliberately.
        if _osk_is_open():
            _close_osk()
        # Scrim: dims the whole picker client area and swallows stray clicks so
        # nothing behind the tutorial can be operated by accident. NOT -topmost
        # (see _open_cog_modal)  an owned Toplevel already sits above its
        # owner without floating over every other app on the desktop.
        scrim = tk.Toplevel(self.p.root)
        scrim.overrideredirect(True)
        scrim.configure(bg="#000000")
        scrim.geometry("%dx%d+%d+%d" % (rw, rh, rx, ry))
        try:
            scrim.attributes("-alpha", _SCRIM_ALPHA)
        except tk.TclError:
            pass
        # Swallow stray clicks on the dimmed area  but a click also RAISES a
        # topmost window on Win32, so put the panel back on top straight after
        # rather than waiting for the poll below to notice (see _fix_stack).
        scrim.bind("<Button-1>", lambda e: self._fix_stack())
        self._scrim = scrim
        self._pin_top(scrim)
        h = _identify_window(scrim, "Tutorial")
        if h:
            self._icon_handles.append(h)

        panel = tk.Toplevel(self.p.root)
        panel.overrideredirect(True)
        panel.configure(bg=_BG, highlightthickness=2,
                        highlightbackground=_FIELD, highlightcolor=_FIELD)
        panel.bind("<Escape>", lambda e: self._skip())
        # Transparent from the moment it exists. It is mapped here but not
        # rendered until _render below (which costs real time  every label on
        # the slide is a PIL raster), and a mapped-but-empty panel sitting on
        # screen for that stretch is a flash of dark nothing before the tour
        # appears. _fade_in takes it from 0 to 1 once there is something to
        # show.
        try:
            panel.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self._panel = panel
        self._pin_top(panel)
        h = _identify_window(panel, "Tutorial")
        if h:
            self._icon_handles.append(h)
        self._place_panel()
        self._build_chrome()
        # ...but not the welcome slide's disappearing act: hiding the manager
        # here would leave a bare desktop for as long as the first render takes
        # (~0.8s, measured). Render first, hide, then fade in  so the window
        # is gone before the tour shows up, with nothing dead in between.
        self._arm_slide(defer_gui_hide=True)
        self._anim_reset()
        self._render()
        try:
            scrim.lift()
            panel.lift()
            panel.focus_force()
        except tk.TclError:
            pass
        self._fix_stack()
        if self._slide.get("hide_gui"):
            self._hide_gui()
        self._fade_in(panel)
        self._poll_aid = self.p.root.after(150, self._poll_foreground)
        return True

    def close(self, completed=False):
        """Tear the overlay down. `completed` only affects the log line  both
        finishing and skipping mark the tutorial as seen (see _persist)."""
        _claim(False)
        self._cancel_afters()
        for aid in (self._poll_aid, self._pulse_aid, self._anim_aid):
            if aid is not None:
                try:
                    self.p.root.after_cancel(aid)
                except tk.TclError:
                    pass
        self._poll_aid = self._pulse_aid = self._anim_aid = None
        if self.p._tutorial is self:
            self.p._tutorial = None
        # Leave the desktop as we found it: a keyboard the TOUR opened as a
        # demo, a gyro pointer it asked the user to switch on, and the demo
        # virtual menu standing in for their own, are not things they chose to
        # keep. (Anything they turn on afterwards is their business  this only
        # runs while the overlay is going away.)
        self._settle_slide()
        for w in (self._panel, self._scrim):
            try:
                if w is not None:
                    w.destroy()
            except Exception:
                pass
        self._panel = self._scrim = None
        self._imgs = []
        self._persist()

    def _after(self, ms, fn):
        """Arm a one-shot timer that is cancelled if the tour goes away first."""
        def run():
            if self._panel is None:
                return
            try:
                fn()
            except Exception as e:
                print(f"tutorial timer failed: {e!r}")
        try:
            self._after_aids.append(self.p.root.after(ms, run))
        except tk.TclError:
            pass

    def _cancel_afters(self):
        for aid in self._after_aids:
            try:
                self.p.root.after_cancel(aid)
            except tk.TclError:
                pass
        self._after_aids = []

    def _persist(self):
        """Latch "this user has seen the tutorial" so first launch stops
        opening it. Applied through the normal Options channel, which both
        persists to settings.json and updates the picker's own mirror."""
        self.p._general["tutorial_done"] = True
        if self.p._on_general is None:
            return
        try:
            self.p._on_general("tutorial_done", True)
        except Exception as e:
            print(f"tutorial: could not persist completion: {e!r}")

    @staticmethod
    def _pin_top(win):
        """Float one of the overlay's windows above everything.

        The cog modal deliberately avoids -topmost (an owned window is already
        above its owner, and topmost would put it over other apps' windows
        too). That reasoning doesn't survive here: the picker keeps re-raising
        ITSELF for a while after a reveal (_bring_front / _reassert_foreground,
        which the first-run path runs immediately before the tour opens), and
        an owned-but-not-topmost overlay ends up buried under the very window
        it is supposed to be explaining. The usual objection doesn't apply
        either, because _poll_foreground withdraws the whole overlay the moment
        another application takes the foreground  so it is never topmost over
        somebody else's window."""
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass

    def _scrim_on_top(self):
        """Is the scrim actually covering the panel right now?

        Asked before every restack, because the restack is not free: Tk's
        lift(aboveThis) briefly raises the OTHER window on Windows, so calling
        it speculatively (it used to run on every 150ms tick as a safety net)
        made the tour flash black for a frame several times a minute  the
        cure was worse than the disease it was insuring against.

        One WindowFromPoint at the panel's centre. It answers the question the
        user actually cares about ("is there dimming over the tour"), and it
        stays quiet when something ELSE is legitimately on top there  the
        on-screen keyboard is allowed to cover this slide."""
        if sys.platform != "win32":
            return False
        scrim, panel = self._scrim, self._panel
        if scrim is None or panel is None or self._hidden or self._gui_hidden:
            return False
        try:
            x = panel.winfo_rootx() + panel.winfo_width() // 2
            y = panel.winfo_rooty() + panel.winfo_height() // 2
            here = _window_at(x, y)
            return bool(here) and int(here) == int(_root_hwnd(scrim) or -1)
        except tk.TclError:
            return False

    def _fix_stack_if_needed(self):
        """Restack ONLY when the scrim has actually got on top (see
        _scrim_on_top)  the check is cheap, the restack is not."""
        if self._scrim_on_top():
            self._fix_stack()

    def _fix_stack(self):
        """Keep the PANEL above the SCRIM.

        Both are -topmost, so their order is simply whichever was raised last
         and the scrim can win that race in ways we don't control. Clicking
        anywhere in the dimmed margin raises it (Win32 raises a topmost window
        on click, and the scrim deliberately swallows those clicks rather than
        letting them through), and a TclError partway through the un-hide loop
        below can leave it re-pinned while the panel isn't. Either way the
        result is the tour wearing the dimming that belongs to the GUI behind
        it, and nothing would have put it right again.

        Raises the panel just above the SCRIM specifically, not above
        everything: a blanket lift() would also drag it over the on-screen
        keyboard, which is supposed to cover the tour on the keyboard slide."""
        scrim, panel = self._scrim, self._panel
        if scrim is None or panel is None:
            return
        try:
            panel.lift(scrim)
        except tk.TclError:
            try:
                panel.lift()
            except tk.TclError:
                pass

    def _fade_in(self, panel, dur_ms=170):
        """Tween the PANEL's alpha 0->1 (the scrim dims instantly). Mirrors
        _fade_in_modal_panel; aborts if the tutorial closed mid-fade."""
        try:
            panel.attributes("-alpha", 0.0)
        except tk.TclError:
            return
        t0 = time.monotonic()

        def step():
            if self._panel is not panel:
                return
            t = min(1.0, (time.monotonic() - t0) * 1000.0 / dur_ms)
            try:
                panel.attributes("-alpha", t)
            except tk.TclError:
                return
            if t < 1.0:
                panel.after(16, step)
        step()

    # -- geometry ------------------------------------------------------------

    def _panel_size(self, rw, rh):
        w = max(_PANEL_W_MIN, min(_PANEL_W_MAX, rw - _PANEL_MARGIN * 2))
        h = max(_PANEL_H_MIN, min(_PANEL_H_MAX, rh - _PANEL_MARGIN * 2))
        return w, h

    def _place_panel(self):
        """(Re)position the scrim over the picker's client area and the panel
        centred inside it. Returns the panel's (w, h).

        While the manager is hidden (the welcome slide  see _hide_gui) this
        does NOTHING: the anchor it would read is a window that isn't there,
        and the panel staying put is the entire point  it must not jump when
        the window goes and jump back when it returns. The scrim is withdrawn
        for that whole stretch anyway, because dimming would either dim nothing
        or, at screen size, cover the very taskbar the slide is pointing at."""
        if self._gui_hidden:
            return self._geom or self._panel_size(*self.p._anchor_rect()[2:])
        rx, ry, rw, rh = self.p._anchor_rect()
        w, h = self._panel_size(rw, rh)
        try:
            self._scrim.geometry("%dx%d+%d+%d" % (rw, rh, rx, ry))
            self._panel.geometry("%dx%d+%d+%d" % (
                w, h, rx + max(0, (rw - w) // 2), ry + max(0, (rh - h) // 2)))
        except tk.TclError:
            pass
        return w, h

    def reposition(self):
        """The picker moved/resized (root <Configure>): follow it. A size
        change also re-lays the slide, since every stage drawing is sized to
        the panel."""
        if self._panel is None:
            return
        w, h = self._place_panel()
        if (w, h) != self._geom:
            self._render()

    def _root_on_screen(self):
        """Is the manager window actually showing? Mirrors the conditions the
        picker's own pumps park on (hidden / fake-minimized / ghost-hidden /
        iconified)."""
        p = self.p
        if not getattr(p, "_visible", True):
            return False
        if getattr(p, "_min_hidden", False) or getattr(p, "_ghost_hidden", False):
            return False
        try:
            return p.root.state() != "iconic"
        except tk.TclError:
            return False

    def _parts(self):
        """The overlay windows that park and unpark together. The scrim drops
        out of that set while the manager is hidden (_hide_gui): it is
        withdrawn for the duration, and an unpark that deiconified it would put
        a dimming sheet over a desktop with nothing behind it."""
        if self._gui_hidden:
            return (self._panel,)
        return (self._scrim, self._panel)

    def _poll_foreground(self):
        """Park the overlay while another APPLICATION is foreground, rather
        than closing it the way the cog modal does.

        Slide 2 asks the user to Alt-Tab, which by definition hands the
        foreground to something else  an owned, non-topmost Toplevel would
        keep hanging over that other window. So hide both parts and bring them
        straight back when the picker returns. The on-screen keyboard is this
        same process, so opening it (slide 1) does NOT park anything."""
        self._poll_aid = None
        if self._panel is None:
            return
        # The switch-windows slide's demo window IS this process, so
        # _app_is_active() stays true while the user is looking at it  park on
        # it explicitly. It is a window they were sent to read; the overlay
        # must get out of its way exactly as it would for another app's.
        alt_focused = self._alt_window_focused()
        # The manager coming back is what ends the welcome slide's "no window"
        # state  however it came back, not just via the taught tray click.
        if self._gui_hidden and self._root_on_screen():
            self._gui_restored()
        # ...and park it just the same whenever the picker ITSELF isn't on
        # screen. The overlay is -topmost, so a minimized (or ghost-hidden)
        # manager would otherwise leave the tour floating over a bare desktop
        # with nothing behind it.
        #
        # Except when the tour hid that manager ITSELF (_hide_gui): floating
        # over a bare desktop is then the entire point, and neither test can
        # be believed  the window is deliberately gone, and with it gone the
        # foreground belongs to some other app, so _app_is_active() is false
        # too. Stay up; the welcome slide is a handful of seconds long.
        want_hidden = (not self._gui_hidden
                       and (alt_focused or not kp._app_is_active()
                            or not self._root_on_screen()))
        # Parking is DEBOUNCED: one tick of "we're not the foreground" is
        # routinely just a transition. Revealing the manager from the tray
        # hands the foreground round the houses (shell -> us, with a
        # re-assert 80ms later), and a single unlucky sample in the middle of
        # it withdrew the entire tour for one frame  measured at ~0.85s after
        # the click, long after everything else had settled. Two consecutive
        # ticks (300ms) is still imperceptible for a real Alt-Tab away, and
        # UNparking stays instant: coming back must never wait.
        # ...and suppressed outright for a moment after the manager has just
        # been revealed. A tray-click reveal from a background process does
        # not win the OS foreground instantly  Windows' focus-stealing
        # prevention means it can take a few hundred ms and a re-assert to
        # land (measured: still not foreground 600ms in). The window is on
        # screen and was asked for; parking the tour off it in the meantime
        # is the one thing that must not happen.
        if (want_hidden and not alt_focused and self._root_on_screen()
                and time.monotonic() < self._no_park_until):
            want_hidden = False
        if want_hidden and not self._hidden:
            if not self._park_pending:
                self._park_pending = True
                want_hidden = False
        else:
            self._park_pending = False
        if want_hidden != self._hidden:
            self._hidden = want_hidden
            if want_hidden:
                for w in self._parts():
                    try:
                        w.withdraw()
                    except tk.TclError:
                        pass
            else:
                # Position and re-pin BEFORE showing anything. Deiconifying
                # first made both windows appear at wherever they were when
                # they went away and then jump into place  the visible lurch
                # when Alt-Tabbing back in. While withdrawn all of this is
                # free, so the first frame the user sees is already correct.
                try:
                    self._place_panel()
                except tk.TclError:
                    pass
                # Re-pinned one at a time: a TclError on the scrim used to
                # abort the whole block and leave IT re-pinned with the panel
                # never re-raised  i.e. the tour wearing the scrim's dimming.
                # Only the PANEL is lifted: lifting the scrim first put it
                # over the panel for the frame between the two calls, and the
                # scrim keeps its place in the z-order across a withdraw
                # anyway, so raising it buys nothing.
                for w in self._parts():
                    self._pin_top(w)   # a withdraw drops the flag on Win
                    try:
                        w.deiconify()
                    except tk.TclError:
                        pass
                try:
                    self._panel.lift()
                except tk.TclError:
                    pass
                self._fix_stack_if_needed()
        # Re-assert panel-above-scrim when it has slipped. Both are -topmost and
        # both are owned by the manager, so activating the manager restacks
        # them as a group and the order within that group is not ours to
        # choose  measured: a _bring_front leaves the scrim on top now and
        # then, and once it does NOTHING was putting it back. The tour then
        # sat behind its own 72%-black dimming until something happened to
        # call _fix_stack (a click on the scrim). One SetWindowPos per 150ms
        # is nothing, and it is idempotent when the order is already right.
        if not self._hidden and not self._gui_hidden:
            self._fix_stack_if_needed()
        # The keyboard slide's second step is landed here rather than off a
        # controller frame  none arrive while the OSK owns the pad.
        try:
            self._poll_alt_window(alt_focused)
        except Exception as e:
            print(f"tutorial demo-window watch failed: {e!r}")
        try:
            self._poll_osk_typing()
        except Exception as e:
            print(f"tutorial typing watch failed: {e!r}")
        try:
            self._poll_osk_close()
        except Exception as e:
            print(f"tutorial keyboard-close watch failed: {e!r}")
        try:
            self._poll_media()
        except Exception as e:
            print(f"tutorial media watch failed: {e!r}")
        # A step that hid the tour to do its job (Alt-Tab, the keyboard) has
        # its celebration waiting on exactly this: being visible again.
        try:
            self._defer_poll(time.monotonic())
        except Exception as e:
            print(f"tutorial deferred reveal failed: {e!r}")
        try:
            self._poll_aid = self.p.root.after(150, self._poll_foreground)
        except tk.TclError:
            pass

    # -- navigation ----------------------------------------------------------

    def nav_press(self, bit):
        """Every controller/keyboard press while the tutorial is up (see
        _Picker._nav_dispatch). Left/Right walk the footer buttons, A fires the
        focused one, B steps back a slide, and the bumpers page directly 
        which is also what the bumpers do in the picker itself, so the habit
        the tutorial builds is the right one."""
        if bit == kp._NB_B and self._close_task_pending():
            # The keyboard slide's own "close it" step IS a B press (the
            # Escape binding). Let it register as that instead of stepping
            # the tour back a slide  see feed()'s "close" check.
            return
        if bit == kp._NB_DLEFT:
            self._move_focus(-1)
        elif bit == kp._NB_DRIGHT:
            self._move_focus(+1)
        elif bit == kp._NB_A:
            # A reading slide (no checklist of its own) has no button for the
            # focus ring to land on  it names A directly in its own "continue"
            # / "finish" hint (_draw_onward), so A has to actually do that
            # here rather than through the _btns/_activate plumbing every
            # other slide's Skip/steps use.
            if self._reading_slide():
                self._go(+1)
            else:
                self._activate(self._focus)
        elif bit == kp._NB_B or bit == kp._NB_LB:
            self._go(-1)
        elif bit == kp._NB_RB:
            self._go(+1)

    def _reading_slide(self, s=None):
        """True on a slide with no checklist of its own and nothing else to
        point at (no "unbound" note, no "where" pointer)  the ones
        _paint_task hands to _draw_onward's "press A to continue/finish" hint,
        and so the only ones A pages through directly (see nav_press)."""
        s = s or self._slide
        return not (s.get("checks") or s.get("where") or s.get("unbound"))

    def _close_task_pending(self):
        """True on the keyboard slide, once it's been opened but not yet
        closed  the window in which a bare B press means "close it", not
        "go back"."""
        s = self._slide
        if s["id"] != "osk":
            return False
        ids = {c["id"] for c in (s.get("checks") or [])}
        if "close" not in ids:
            return False
        done = self._done.get(s["id"]) or set()
        return "osk" in done and "close" not in done

    def _move_focus(self, d):
        """Walk the gamepad-focusable footer buttons  just Skip now that
        Next/Previous are mouse-only (see _build_chrome), so this is mostly a
        no-op left in place for the day another focusable control joins it.
        CLAMPS at both ends rather than wrapping, on general principle: Skip
        is destructive (closes the tour with no undo), so it should never
        become reachable by an overshoot the user didn't mean."""
        n = len(self._btns)
        if not n:
            return
        self._focus = max(0, min(n - 1, self._focus + d))
        self._paint_btns()

    def _activate(self, i):
        if 0 <= i < len(self._btns):
            try:
                self._btns[i].invoke()
            except tk.TclError:
                pass

    def _go(self, d):
        i = self._idx + d
        if i < 0:
            return
        if i >= len(self._slides):
            self.close(completed=True)
            return
        self._idx = i
        # Every slide change starts clean: drop a pending auto-close / come-back
        # timer from the slide being left, and put the keyboard away if it is
        # still up (it belongs to the keyboard slide alone  it is always-on-top
        # and would cover everything after it).
        self._cancel_afters()
        self._note = None
        self._settle_slide()
        # Next/Previous are mouse-only (see _build_chrome)  the bumpers are
        # the gamepad's own way to page, wired directly in nav_press, and A
        # pages a reading slide the same way (see nav_press's reading-slide
        # branch). self._btns is never populated any more, so this is a no-op
        # left for whatever eventually reuses that plumbing.
        self._focus = -1
        self._arm_slide()
        self._anim_reset()
        self._render()

    def _settle_slide(self):
        """Undo whatever the slide being LEFT switched on. Everything the tour
        asks the user to turn on is latching, and would otherwise follow them
        through the rest of it  an always-on-top keyboard over the remaining
        slides, a gyro pointer drifting across the buttons, and a demo menu
        sitting on a chord the user never bound."""
        if _osk_is_open():
            _close_osk()
        _gyro_off()
        self._restore_vmenus()
        self._stop_media_demo()
        self._restore_osk_size()
        self._stop_typing_watch()
        self._close_alt_window()
        self._show_gui()

    def _arm_slide(self, defer_gui_hide=False):
        """Set up whatever the slide being ENTERED needs in place before the
        user can try it: a menu to actually press on the Virtual Menus slide
        (see _demo_vmenu), something for the transport keys to land on on the
        media slide (_start_media_demo), and on the keyboard slide a keyboard
        sized to leave the tour visible plus an echo of what gets typed on it.

        `defer_gui_hide` leaves the welcome slide's vanishing manager to the
        caller. open() uses it so the window goes away AFTER the first (slow)
        render and immediately before the fade-in  hiding it at arm time left
        a bare desktop for the whole of that render."""
        if self._slide.get("demo_vmenu"):
            self._install_demo_vmenu()
        if self._slide.get("media_demo"):
            self._start_media_demo()
        if self._slide.get("osk_small"):
            self._shrink_osk()
            self._start_typing_watch()
        if self._slide.get("alt_window"):
            self._open_alt_window()
        if self._slide.get("hide_gui") and not defer_gui_hide:
            self._hide_gui()
        self._sync_nav_keep()

    # Buttons that stay live on EVERY slide, because they are the mouse and the
    # tour is a window you have to be able to click things in (the very first
    # slide asks for a tray-icon click). Triggers = left/right click on every
    # kind, stick click = middle, pad clicks on the pads. Motion needs no bits.
    _MOUSE_KEEP = 0
    for _n in ("LT", "RT", "L3", "LPAD", "RPAD"):
        _MOUSE_KEEP |= int(kp._SCB_NAME_TO_BIT.get(_n) or 0)
    del _n

    def _sync_nav_keep(self):
        """Publish the ALLOW-LIST of buttons for the current slide.

        While a tour is up the tray masks every button bit that isn't in here
        (see tray.py's tutorial_claimed branches), so this is the whole
        vocabulary the controller has for as long as the slide is showing.
        Anything else a user mashes reaches the tutorial's own detection 
        published upstream of the mask  and reaches nothing else at all,
        which is the point: no stray alt-tab, no force-kill, no mode flip, no
        "Toggle Config GUI" closing the manager out from under the tour.

        What's in it:
          - the mouse: triggers click, stick/pads move the pointer. The very
            first slide asks for a tray-icon CLICK, so this can never go.
          - the Guide/"..." bits, but only on a slide that teaches a chord 
            they are the modifier those chords are held with. (The TAP action
            is separately suppressed tray-side; a mistimed tap must not end
            the tour.)
          - every bit this slide's own checks are waiting for, including the
            keyboard slide's bare-X open, which is a nav button and would
            otherwise be masked before it could open anything.

        Called on every slide change AND every time a step lands, so a hole
        closes the moment it stops being needed."""
        s = self._slide
        keep = self._MOUSE_KEEP
        checks = s.get("checks") or []
        done = self._done.get(s["id"]) or set()
        # The CHORD the slide teaches, first and unconditionally. Not every
        # step carries a bit to key off  Alt-Tab's is landed by a watcher on
        # the demo window (it is "poll", with no "bit" at all), so a keep-list
        # built from the checks alone masked the one chord that slide exists
        # to teach. And it stays allowed after the step lands: a user who
        # wants to try it twice must not find it dead the second time.
        for cid in (s.get("chord") or ()):
            if cid:
                keep |= _GUIDE_BITS | (_bit_for(self._kind, cid) or 0)
        for c in checks:
            if c["id"] in done:
                continue
            if c.get("guide"):
                keep |= _GUIDE_BITS
            for b in (c.get("bit"), *(c.get("pair") or ())):
                keep |= int(b or 0)
        # The bare-X keyboard open isn't a check "bit" (the slide resolves it
        # separately  see the osk slide's open_cid).
        if (s.get("id") == "osk" and "osk" not in done
                and any(c.get("id") == "osk" for c in checks)):
            keep |= _bit_for(self._kind, "x") or 0
        # Guide + D-Pad Up shows the demo menu, and the pads/sticks steer it.
        if s.get("id") == "vmenu" and s.get("vmenu_cids"):
            keep |= _GUIDE_BITS
            for cid in s.get("vmenu_cids") or ():
                keep |= _bit_for(self._kind, cid) or 0
        _nav_keep(keep)

    # -- the welcome slide's vanishing manager -------------------------------

    def _hide_gui(self, tries=0):
        """Take the manager away and leave the tour floating on its own.

        This is what makes the welcome slide's task mean anything: "click the
        tray icon to open the manager" is a sentence about a window that isn't
        there. With the window still up, the click had nothing to do and the
        lesson had to be taken on trust.

        Done from _arm_slide, which open() runs BEFORE the panel fades in 
        so the manager is already gone when the tour appears, rather than
        vanishing a moment later underneath it."""
        if self._gui_hidden or self._panel is None:
            return
        s = self._slide
        # The timer outlived its slide, or the user beat it to the tray icon:
        # either way there is nothing left to demonstrate by hiding.
        if (not s.get("hide_gui")
                or "tray" in (self._done.get(s["id"]) or set())):
            return
        self._gui_hidden = True
        self._park_scrim()
        try:
            self.p.hide_for_tutorial()
        except Exception as e:
            print(f"tutorial: could not hide the manager: {e!r}")
            self._gui_hidden = False
            self._place_panel()          # scrim back where it belongs
            return
        # Belt and braces: the picker drops a hide that lands mid-transition
        # (_hide's re-entrancy guard). Put ourselves back exactly as we were
        # and try again shortly rather than believing a hide that didn't
        # happen  a tour that thinks the window is gone while it is sitting
        # right there behind it gets every later decision wrong.
        if getattr(self.p, "_visible", False):
            self._gui_hidden = False
            self._place_panel()
            if tries < _GUI_HIDE_TRIES:
                self._after(80, lambda: self._hide_gui(tries + 1))
            else:
                print("tutorial: gave up hiding the manager")
            return
        # The panel deliberately does NOT move. It could re-centre on the
        # screen now that it has lost the window it was anchored to, but the
        # manager it was centred in is itself near the middle of the screen 
        # so re-centring buys a few pixels of tidiness and costs a jump on the
        # way out AND another on the way back. Standing still reads as the
        # window being taken out from under a tour that never flinched.
        self._pin_top(self._panel)      # a withdraw drops the flag on Windows
        try:
            self._panel.lift()
        except tk.TclError:
            pass
        # Nothing is said about it. The window going away used to draw a line
        # under the checklist ("that's where this window just went")  but the
        # slide's own picture already ends on the tray, and the checklist row
        # right above it is the sentence. Two explanations of one disappearing
        # window is one too many.

    def _park_scrim(self):
        """Move the scrim off the bottom of the screen instead of withdrawing
        it while the manager is away.

        It has nothing to dim then, but it must not be UNMAPPED either: a
        re-map lands a topmost window on top of the topmost panel, and the tour
        wears the dimming for the frame or two before _fix_stack undoes it 
        which is precisely the flicker the welcome slide's tray click used to
        end on. Parked off-screen it keeps both its map state and its place in
        the z-order, and coming back is a move."""
        try:
            self._scrim.geometry("%dx%d+0+%d" % (
                max(1, self._scrim.winfo_width()),
                max(1, self._scrim.winfo_height()),
                self._scrim.winfo_screenheight() + 4))
        except tk.TclError:
            pass

    def _show_gui(self):
        """Put the manager back  used when the slide is LEFT without the user
        ever clicking the tray icon (Next, Skip, or the tour closing). The tour
        must never be the reason somebody's window stayed hidden."""
        if not self._gui_hidden:
            return
        try:
            self.p._show_window()
        except Exception as e:
            print(f"tutorial: could not restore the manager: {e!r}")
        self._gui_restored()

    def _gui_restored(self):
        """The manager is on screen again  put the tour back the way it
        normally sits: scrim over the window, panel centred in it.

        Called from _poll_foreground rather than from the tray click alone, so
        it covers EVERY route back (the tray icon, the Toggle Config GUI chord,
        a second launch of the exe) instead of just the taught one."""
        if not self._gui_hidden:
            return
        self._gui_hidden = False
        # The scrim comes back from its off-screen park (see _park_scrim)  a
        # move, not a re-map, which is the whole point: re-mapping it raised it
        # ABOVE the panel for a frame or two (measured: ~22 ms of the tour
        # wearing the dimming) before _fix_stack could put things right. The
        # PANEL is left exactly where it is unless the manager came back
        # somewhere else, in which case _place_panel moves it once; it never
        # moved on the way out, so the common case moves nothing at all.
        before = self._geom
        size = self._place_panel()
        self._no_park_until = time.monotonic() + _REVEAL_GRACE_S
        # The reveal that brought the manager back also restacked its owned
        # windows  put the panel above the scrim, and again once the
        # foreground steal has settled.
        self._restack_after_raise()
        # Only repaint if the panel actually changed size: the slide art is
        # laid out to the panel, and a re-render blanks every canvas for a
        # frame, which is exactly the flicker this is avoiding.
        if size != before:
            self._render()

    # -- the switch-windows slide's demo window ------------------------------

    def _open_alt_window(self):
        """Put a REAL second window on screen for the Alt-Tab step to land on.

        The slide used to ask the user to switch windows and then hope they had
        one  on a fresh machine mid-first-run there may be nothing but the
        manager, and "it worked" was inferred from the tour losing the
        foreground, which a stray click also does. So the tour supplies the
        target: an ordinary titled window (see _make_alt_tabbable) carrying its
        own artwork, and the step lands when THAT window is the one in front.

        Deliberately NOT topmost. It is meant to behave like any other app the
        user might Alt-Tab to, and the overlay parks itself while it holds the
        foreground (see _poll_foreground) exactly as it would for Notepad 
        which also keeps the celebration held back until the tour is looked at
        again, instead of playing behind a window the user is reading."""
        if self._alt_win is not None:
            return
        try:
            win = tk.Toplevel(self.p.root)
            win.withdraw()                       # style it before it is shown
            win.configure(bg=_BG)
            win.resizable(False, False)
            win.protocol("WM_DELETE_WINDOW", lambda: None)  # the tour owns it
            win.bind("<FocusIn>", lambda e: self._set_alt_focus(True))
            win.bind("<FocusOut>", lambda e: self._set_alt_focus(False))
            # Realize it while still hidden: until Tk has actually created the
            # OS window there is no HWND to title, icon or restyle, and the
            # Win32 calls below would silently write to nothing (measured: the
            # ex-style came back unchanged without this).
            win.update_idletasks()
            h = _identify_window(win, "SteamlessInput  Tutorial Window")
            if h:
                self._icon_handles.append(h)
            _make_alt_tabbable(win)
            _dark_caption(win)
            cv = tk.Canvas(win, bg=_BG, highlightthickness=0, bd=0,
                           width=_ALT_WIN_W, height=_ALT_WIN_H)
            cv.pack(fill="both", expand=True)
            self._alt_win, self._alt_canvas = win, cv
            self._place_alt_window(win, self._alt_hidden_rect())
            self._paint_alt_window()
            # Shown first, minimised a moment later (_minimise_alt_window).
            # Not minimised outright: a window that has never been drawn has
            # no composited surface, so Alt-Tab shows it as a generic
            # placeholder instead of a preview of the artwork the whole slide
            # is pointing at. Showing it no-activate lets DWM capture the real
            # thing  and costs nothing visually, because it is parked under
            # the tour's own (larger, -topmost) panel for that whole moment
            # (_alt_hidden_rect) and only takes its real, screen-centred spot
            # as it minimises (_minimise_alt_window).
            if not _show_no_activate(win):
                win.deiconify()
            try:
                win.update()          # force the paint DWM will capture
            except tk.TclError:
                pass
        except tk.TclError as e:
            print(f"tutorial: demo window failed to open: {e!r}")
            self._alt_win = self._alt_canvas = None
            return
        # Still armed on a timer rather than immediately: on a platform where
        # the no-activate show didn't take, the window IS now focused, and the
        # step must not land on our own construction.
        self._alt_armed = False
        self._after(_ALT_MINIMISE_MS, self._minimise_alt_window)
        self._after(_ALT_ARM_MS, self._arm_alt_window)

    def _minimise_alt_window(self):
        """Put the demo window into the taskbar, once it has had a frame on
        screen for DWM to capture. The step is "switch to this window", and a
        window already sitting on screen is something you look at rather than
        switch to  minimised, restoring it IS the switch.

        This is also where it stops being parked under the panel and takes its
        real place: minimising and setting the restored position are ONE call
        (see _show_minimized), so the move never reaches the screen. The
        fallback (no SetWindowPlacement  i.e. not Windows) has to
        move-then-iconify, which can show a frame at the new spot; it is the
        same platform where the show couldn't be no-activate either, so the
        window is already visible there and this costs it nothing extra."""
        win = self._alt_win
        if win is None:
            return
        rect = self._alt_rect()
        if not _show_minimized(win, restore=rect):
            self._place_alt_window(win, rect)
            try:
                win.iconify()
            except tk.TclError:
                pass

    def _arm_alt_window(self):
        self._alt_armed = self._alt_win is not None

    def _alt_rect(self):
        """Where the demo window LIVES: centred on the work area  this is a
        window the user is being sent to LOOK at, so it gets the middle of the
        screen rather than a corner. The tour is parked while it's up, so
        nothing is covered."""
        win = self._alt_win or self.p.root
        try:
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
        except tk.TclError:
            sw, sh = 1920, 1080
        return (max(0, (sw - _ALT_WIN_W) // 2),
                max(0, (sh - _ALT_WIN_H) // 2), _ALT_WIN_W, _ALT_WIN_H)

    def _alt_hidden_rect(self):
        """Where it sits for the moment it is mapped but not yet minimised:
        dead centre of the tour's own panel.

        It has to be on screen and composited for that moment  that is the
        whole reason it is shown at all (DWM has no thumbnail of a window it
        has never drawn, so Alt-Tab would offer a blank placeholder instead of
        the artwork this slide points at). But "on screen" and "visible" are
        not the same thing: the panel is -topmost and always larger than this
        window, so parked under it the capture happens with nothing to see.

        Centring on the panel rather than the screen is the point  the two
        coincide only when the manager happens to be centred on the primary
        monitor. Anywhere else (a moved window, a second monitor) a
        screen-centred demo window pokes out from behind the panel, which is
        the one-frame flash this avoids. Falls back to the real rect if the
        panel isn't mapped yet, which can't happen from _arm_slide but keeps
        this honest if it is ever called earlier."""
        p = self._panel
        try:
            if p is None or not p.winfo_ismapped():
                return self._alt_rect()
            px, py = p.winfo_rootx(), p.winfo_rooty()
            pw, ph = p.winfo_width(), p.winfo_height()
        except tk.TclError:
            return self._alt_rect()
        # Measured against the FRAME, not the canvas: the caption and border
        # are part of what has to stay covered. If the panel can't swallow it
        # whole there is nothing to be gained by moving it, so it falls back
        # to its real place  a flash on a panel too small to hide it is not
        # avoidable, only relocatable.
        if pw < _ALT_WIN_W + 40 or ph < _ALT_WIN_H + 56:
            return self._alt_rect()
        return (px + (pw - _ALT_WIN_W) // 2, py + (ph - _ALT_WIN_H) // 2,
                _ALT_WIN_W, _ALT_WIN_H)

    def _place_alt_window(self, win, rect):
        """Move/size the demo window, and make sure it has actually MOVED.

        The flush is not optional. Tk queues a geometry request as idle work,
        and this window is shown by ShowWindow (see _show_no_activate) rather
        than by Tk's own deiconify  so nothing on the Tk side would ever run
        to apply it, and the window mapped wherever Tk had parked it when it
        was realised (measured: the default cascade spot, top-left, i.e. well
        outside the panel it is supposed to be hiding under). That is the
        one-frame flash on the way into this slide."""
        try:
            win.geometry("%dx%d+%d+%d" % (rect[2], rect[3], rect[0], rect[1]))
            win.update_idletasks()
        except tk.TclError:
            pass

    def _close_alt_window(self):
        """Take the demo window away again. From _settle_slide, so it runs on
        every exit  Next, Previous, Skip and close alike; a tour that is
        killed mid-slide must not leave a stray window on the desktop."""
        win, self._alt_win = self._alt_win, None
        self._alt_canvas = None
        self._alt_armed = False
        self._alt_seen = False
        self._alt_focus = False
        if win is None:
            return
        try:
            win.destroy()
        except tk.TclError:
            pass

    def _set_alt_focus(self, on):
        """Tk's own view of whether the demo window has focus  bound to its
        FocusIn/FocusOut. This is what answers the question on Linux, where
        there is no GetForegroundWindow to ask (and where the picker's
        _app_is_active is hardcoded True for the same reason)."""
        self._alt_focus = bool(on)

    def _alt_window_focused(self):
        """Is the demo window the one in front? Asked at 150 ms from
        _poll_foreground, which is also what decides whether the overlay
        parks  so both answers come from the same tick."""
        if self._alt_win is None or not self._alt_armed:
            return False
        if sys.platform != "win32":
            return self._alt_focus
        # Windows: compare the real foreground window rather than trusting Tk
        # focus events, which this overlay's own raising games can muddle.
        try:
            hwnd = _root_hwnd(self._alt_win)
        except tk.TclError:
            return False
        if not hwnd:
            return False
        fg = _foreground_hwnd()
        return bool(fg) and int(fg) == int(hwnd)

    def _poll_alt_window(self, focused):
        """Land the switch-windows step the moment the demo window is the one
        in front, and swap its artwork over to the "found it" state."""
        if self._alt_win is None:
            return
        rose = focused and not self._alt_seen
        if focused != self._alt_seen:
            self._alt_seen = focused
            self._paint_alt_window()
        if not focused:
            return
        s = self._slide
        if not any(c.get("id") == "tab" for c in (s.get("checks") or [])):
            return
        done = self._done.setdefault(s["id"], set())
        if "tab" in done:
            # Already ticked  a second visit to this slide, or a second
            # switch on the first. The tick is the only thing that is spent;
            # the come-back is not, and without it the user is left sitting in
            # the demo window with the tour stranded behind it. Rising edge
            # only, or every poll tick would stack another return timer.
            if rose:
                self._after(_RETURN_MS, self._return_to_tour)
            return
        was_done = set(done)
        done.add("tab")
        self._commit(s, was_done)

    def _paint_alt_window(self):
        """The demo window's own graphic: a target the user is aiming at, which
        becomes a tick once they hit it. Drawn with the tour's own primitives
        so it is unmistakably part of the same app.

        Those primitives park their PhotoImages in self._imgs, which _render
        CLEARS on every repaint of the slide  and Tk drops any image it holds
        the last reference to, so this window's text silently vanished the
        first time the tour repainted behind it. Swap the list out for the
        window's own for the duration: every helper keeps working unchanged and
        the refs it makes outlive the slide instead."""
        cv = self._alt_canvas
        if cv is None:
            return
        try:
            cv.delete("all")
        except tk.TclError:
            return
        keep, self._imgs = self._imgs, []
        try:
            self._paint_alt_window_inner(cv)
        finally:
            self._alt_imgs, self._imgs = self._imgs, keep

    def _paint_alt_window_inner(self, cv):
        w, h = _ALT_WIN_W, _ALT_WIN_H
        found = self._alt_seen
        col = _GREEN if found else _ACCENT
        kp._round_rect(cv, 8, 8, w - 8, h - 8, 14, fill=_PANEL_BOX,
                       outline=col, width=2)
        cx, cy = w * 0.5, h * 0.40
        for i, r in enumerate((74, 56, 38)):
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=col,
                           width=2 if i else 3, fill="")
        icon = self._app_icon_photo(52, bg=_PANEL_BOX)
        if icon is not None:
            self._imgs.append(icon)
            cv.create_image(cx, cy, image=icon)
        if found:
            # A tick struck through the rings  the same green the checklist
            # uses, so the payoff here and the payoff there read as one thing.
            cv.create_line(cx - 92, cy + 58, cx - 74, cy + 76, cx - 40,
                           cy + 34, fill=_GREEN, width=6, capstyle="round",
                           joinstyle="round")
        self._txt(cv, cx, h * 0.72,
                  "You switched windows!" if found else "Switch to this window",
                  size=17, fg=_FG, bg=_PANEL_BOX, anchor="center")
        self._txt(cv, cx, h * 0.84,
                  "Taking you back to the tour…" if found else
                  "Hold the Guide button and tap Select.",
                  size=11, fg=_MUTED, bg=_PANEL_BOX, anchor="center")

    # -- the keyboard slide's keyboard ---------------------------------------

    def _shrink_osk(self):
        """Put the keyboard on its "small" size for this slide.

        Goes down the picker's OWN transient preview channel  the one the
        Options Size slider uses while it's being dragged  so this is applied
        WITHOUT persisting and reverts with a single None. That matters twice
        over: the user's saved size is untouched whatever happens to the tour,
        and every window rule the keyboard already has (the cached-Screen
        rebuild, the topmost/no-focus handling, the tour's focus-restore
        short-circuit) keeps working exactly as before  this changes the size
        the next Screen is BUILT at, nothing else."""
        if self._osk_small or self.p._on_general is None:
            return
        try:
            self.p._on_general("osk_size_preview", "small")
            self._osk_small = True
        except Exception as e:
            print(f"tutorial: could not shrink the keyboard: {e!r}")

    def _restore_osk_size(self):
        """Back to the user's saved size. Called from _settle_slide, so it runs
        on every way out  Next, Previous, Skip and close alike."""
        if not self._osk_small:
            return
        self._osk_small = False
        try:
            if self.p._on_general is not None:
                self.p._on_general("osk_size_preview", None)
        except Exception as e:
            print(f"tutorial: could not restore the keyboard size: {e!r}")

    def _start_typing_watch(self):
        """Ask adusk to echo the keys the OSK fires, so the slide can show them
        back (_draw_typed) and land the "type hi" step. Off again on the way
        out  nothing is collected when no slide is asking."""
        self._typed = ""
        self._typed_seen = 0
        try:
            from adusk import state as adusk_state
            adusk_state.set_typed_watch(True)
            self._typing_watch = True
        except Exception as e:
            print(f"tutorial: typing echo unavailable: {e!r}")
            self._typing_watch = False

    def _stop_typing_watch(self):
        self._typed = ""
        self._typed_seen = 0
        if not self._typing_watch:
            return
        self._typing_watch = False
        try:
            from adusk import state as adusk_state
            adusk_state.set_typed_watch(False)
        except Exception as e:
            print(f"tutorial: typing echo failed to stop: {e!r}")

    # -- the silent demo track -----------------------------------------------

    def _start_media_demo(self):
        """Claim the media keys with the tour's own silent track, so Next Song
        and Play/Pause have something to act on that ISN'T the user's music.
        Everything about it is optional: if the session can't come up, the
        slide just draws its static artwork instead (see _art_media)."""
        if media_demo is None:
            return
        try:
            # Kicked off, NOT waited for. The session takes the better part of
            # a second to come up and is allowed six, and this runs on the Tk
            # thread in the middle of a slide change  waiting froze the tour
            # on this slide for as long as it took, which is most of what made
            # clicking through the tour feel slow.
            media_demo.start()
            # True means "this slide owns the media keys", not "the session is
            # live"  _art_media already draws a fallback for a session that
            # isn't up yet, in the same layout, and _poll_media repaints the
            # moment it is (that's a 150 ms tick, so the card lands well
            # inside a second without anything blocking).
            self._media_on = True
        except Exception as e:
            print(f"tutorial: media demo failed to start: {e!r}")
            self._media_on = False
        self._media_seen = None

    def _stop_media_demo(self):
        """Hand the media keys back. Called from _settle_slide, so it runs on
        every way out of the slide  Next, Previous, Skip and close alike."""
        if media_demo is None or not self._media_on:
            return
        self._media_on = False
        self._media_seen = None
        try:
            media_demo.stop()
        except Exception as e:
            print(f"tutorial: media demo failed to stop: {e!r}")

    def _poll_media(self):
        """Repaint the stage when the silent track changes under us  a Next
        that swapped the cover, a Play/Pause that stopped the clock. Driven
        from _poll_foreground's 150 ms tick and gated on an actual change, so
        a slide nobody is touching repaints exactly never."""
        if not self._media_on or media_demo is None:
            return
        try:
            now = media_demo.state()
        except Exception:
            return
        if now == self._media_seen:
            return
        self._media_seen = now
        try:
            self._paint_stage(self._cw(self._stage), self._slide,
                              self._stage_h)
        except tk.TclError:
            pass

    # -- the demo virtual menu -----------------------------------------------

    def _install_demo_vmenu(self):
        """Swap the live menu list for the tour's single demo menu, keeping
        the user's own list to put back. Idempotent: re-entering the slide
        (Previous, then Next again) must not overwrite the saved list with our
        own menu and strand the user's."""
        if self._vmenus_saved is not None:
            return
        saved = _get_vmenus()
        if saved is None:
            return                     # no adusk state  slide stays a poster
        if _set_vmenus([_demo_vmenu()]):
            self._vmenus_saved = saved
            # Start from the CURRENT fire sequence: anything the user pressed
            # on one of their own menus before this slide opened must not
            # arrive here looking like the demo button.
            self._vmenu_seq = _vmenu_fire()[0]

    def _restore_vmenus(self):
        """Put the user's own menus back. Safe to call when nothing was ever
        installed, and called from close() as well as on every slide change,
        so no exit path can leave the demo menu armed."""
        if self._vmenus_saved is None:
            return
        saved, self._vmenus_saved = self._vmenus_saved, None
        _set_vmenus(saved)

    def _skip(self):
        self.close(completed=False)

    # -- slide content -------------------------------------------------------

    def _hint_kind(self):
        """The pad the slides should speak the language of: whichever last
        steered the picker, else the selected controller tab."""
        try:
            return self.p._hint_kind()
        except Exception:
            return "sc"

    def _chord(self, kind, action):
        """(cid, bit) of the control carrying `action` on kind's Chords tab,
        or (None, None) when nothing does."""
        cid = _cid_for_action(self.p, kind, action)
        return cid, _bit_for(kind, cid)

    def _build_slides(self, kind):
        """Assemble the tour for one controller kind. Every "press it" step is
        resolved against the live binds here, so a slide can quietly become
        informational when its action isn't bound on this pad."""
        gcid = _guide_cid(kind)
        gname = pads.label_for(kind, gcid, "Guide")
        osk_cid, osk_bit = self._chord(kind, "show_keyboard")
        tab_cid, tab_bit = self._chord(kind, "alt_tab")
        vup_cid, _vup_bit = self._chord(kind, "volume_up")
        vdn_cid, _vdn_bit = self._chord(kind, "volume_down")
        nxt_cid, _nxt_bit = self._chord(kind, "media_next")
        pp_cid, pp_bit = self._chord(kind, "media_playpause")
        # The gyro toggle is not a Guide chord  it's a standalone two-button
        # combination, and the catalog decides which two (a pad without both
        # stick clicks gets none at all, and then the slide just explains).
        gyro_cids = tuple(pads.default_gyro_toggle_buttons(kind))
        gyro_bits = tuple(_bit_for(kind, c) for c in gyro_cids)
        gyro_ok = (len(gyro_bits) == 2 and all(gyro_bits)
                   and pads.has_gyro(kind))

        slides = []
        slides.append({
            "id": "welcome",
            "title": "Welcome to SteamlessInput",
            "caption": ("An open-source, easier-to-use Steam Input that "
                        "turns any gamepad into a Steam Controller with "
                        "improved PC Controls"),
            "art": self._art_welcome,
            # The stick, the pointer it drags around and the paddles clicking
            # under it are all live (see _welcome_anim)  a still picture of a
            # thumbstick says "there is a stick", a moving one says "this is
            # what it does".
            "stage_anim": self._welcome_anim,
            # Landed by the picker rather than by a controller frame: the tray
            # icon is a MOUSE target, and the click arrives as a toggle request
            # (see _Picker._toggle_visibility -> note_tray_click). "poll" keeps
            # feed() off it; "defer" holds the celebration until the window is
            # back in front, since the click goes via the shell.
            "checks": [{"id": "tray", "text": "Click the icon in the tray",
                        "poll": True, "defer": True}],
            # ...which only means anything with the manager out of the way,
            # so this slide puts it away (see _hide_gui) and the click the
            # checklist asks for is the one that brings it back.
            "hide_gui": True,
        })
        # The keyboard is NOT a Guide chord in Desktop Mode: a bare X opens it
        # (see the tray's `x_opens`, and resolve_sdl_open_bits for the SDL
        # pads, where it's the positional X). The Chords-tab "Open Keyboard"
        # bind still exists and still works, but teaching the chord for the
        # thing the user can do with one button was teaching the long way
        # round. `open_bit` is the bare X; `osk_bit` (the chord) is only what
        # decides whether the slide can be tried at all.
        open_bit = _bit_for(kind, "x")
        osk_checks = []
        if open_bit or osk_bit:
            osk_checks.append({"id": "osk", "text": "Open the keyboard",
                               "guide": False if open_bit else True,
                               "bit": open_bit or osk_bit})
            # Both landed by pollers, not by frames: while the OSK is up the
            # tray has handed the controller to adusk, so NOTHING reaches
            # feed()  not the keys being typed (echoed via
            # adusk state.note_typed_key, see _poll_osk_typing) and not the
            # press that closes it (caught as the window vanishing).
            # NOT deferred: the keyboard is on its "small" size for this
            # slide precisely so the checklist underneath stays visible, so
            # the payoff for typing "hi" plays right there, keyboard still up.
            osk_checks.append({"id": "type", "text": "Type “hi”",
                               "poll": True})
            # Closing the keyboard is deliberately NOT a checklist row. It is
            # what carries you onward (see _poll_osk_close / _advance_when_osk_
            # closed), and it also stands in for the typing step when the two
            # letters came out as "hj"  the slide must not be able to strand
            # anyone behind a keyboard they have already put away.
        slides.append({
            "id": "osk",
            "title": "On-Screen Keyboard",
            "caption": "",
            "art": self._art_osk,
            # The rows tick themselves off as their steps land, and that is
            # text  so the stage is repainted on commit (see _repaint_live_art).
            "live_art": True,
            "chord": (gcid, osk_cid),
            "open_cid": "x" if open_bit else osk_cid,
            "checks": osk_checks,
            # Shrink the keyboard to the app's own "small" size for this slide,
            # and only this slide: full-width it covers the checklist AND the
            # typed-letters box the slide is asking the user to watch.
            "osk_small": bool(osk_checks),
            "unbound": None if (open_bit or osk_bit) else "Open Keyboard",
        })
        slides.append({
            "id": "alttab",
            "title": "Switch Windows",
            "caption": "",
            "art": self._art_alttab,
            "chord": (gcid, tab_cid),
            # The step is landed by ARRIVING at the tour's own demo window
            # (_poll_alt_window), not by the button press: a press that opened
            # the switcher and then went nowhere isn't switching windows, and
            # on a fresh machine there may be no other window to switch TO.
            # So the tour supplies one  see _open_alt_window.
            # "poll": landed by that watcher rather than by a frame.
            # "defer": arriving there parks this whole overlay
            # (_poll_foreground), so the celebration would otherwise play to
            # nobody. Held until we're back on top.
            "checks": ([{"id": "tab", "text": "Switch to it",
                         "poll": True, "defer": True}] if tab_bit else []),
            "alt_window": bool(tab_bit),
            "unbound": None if tab_bit else "Alt-Tab",
        })
        media = []
        for cid, cid_id, text, zone in (
                (vup_cid, "vup", "Volume Up", "UP"),
                (vdn_cid, "vdn", "Volume Down", "DOWN"),
                (nxt_cid, "next", "Next Song", "RIGHT")):
            if cid:
                media.append({"id": cid_id, "text": text, "guide": True,
                              "stick": zone})
        if pp_bit:
            media.append({"id": "pp", "text": "Play / Pause",
                          "guide": True, "bit": pp_bit})
        slides.append({
            "id": "media",
            "title": "Media Controls",
            "caption": "",
            "art": self._art_media,
            # Both drawn sticks follow the real one (_media_anim), and the
            # artwork's own labels/arrows change as steps land  which is text,
            # so the stage is repainted on every commit ("live_art") rather
            # than trying to animate it.
            "stage_anim": self._media_anim,
            "live_art": True,
            "chord": (gcid, pp_cid),
            "checks": media,
            "unbound": None if media else "media controls",
            # Put a silent track of our own on the media keys while this slide
            # is up, so Next/Play-Pause visibly do something and do it to US
            # rather than to whatever the user had playing (see media_demo).
            "media_demo": bool(media),
        })
        slides.append({
            "id": "gyro",
            "title": "Gyro Mouse",
            "caption": "",
            "art": self._art_gyro,
            "gyro_cids": gyro_cids if gyro_ok else (),
            "checks": ([{"id": "gyro", "text": "Turn gyro aiming on",
                         "pair": gyro_bits},
                        {"id": "gyro_off", "text": "Turn gyro aiming off",
                         "pair": gyro_bits, "after": "gyro"}]
                       if gyro_ok else []),
            "unbound": None if gyro_ok else "Gyro To Mouse",
        })
        # The demo menu is triggered by Guide + DPad Up and steered with the
        # RIGHT pad (a BUTTON-triggered menu highlights from the right pad and
        # fires on its click  see the tray's _handle_virtual_menu). That code
        # lives in the HID takeover watcher, so controller-driven menus exist
        # only on the takeover families; an SDL pad gets the poster instead of
        # a press it could never land.
        vm_ok = bool(kind in pads.HID_KINDS and _bit_for(kind, "dpad_up")
                     and _bit_for(kind, "rpad"))
        slides.append({
            "id": "vmenu",
            "title": "Virtual Menus",
            "caption": "An on-screen overlay of buttons you can customise",
            "art": self._art_vmenu,
            "demo_vmenu": vm_ok,
            "vmenu_cids": (gcid, "dpad_up") if vm_ok else (),
            "checks": ([{"id": "gaben", "text": "Press the GABEN",
                         "vmenu_icon": _GABEN_ICON}] if vm_ok else []),
            "where": None if vm_ok else "Controller  ›  Virtual Menus",
        })
        slides.append({
            "id": "tabs",
            "title": "Three Ways To Bind",
            "caption": "The bumpers page between them, here and in the app.",
            "art": self._art_tabs,
            "checks": [],
        })
        slides.append({
            "id": "commands",
            "title": "The Defaults",
            "caption": "",
            "art": self._art_commands,
            "checks": [],
        })
        return slides

    @property
    def _slide(self):
        return self._slides[max(0, min(self._idx, len(self._slides) - 1))]

    # -- chrome (built once) -------------------------------------------------

    def _build_chrome(self):
        """The parts that never change between slides: the dot strip, the two
        canvases every slide paints into, and the footer."""
        p = self._panel
        self._dots = tk.Canvas(p, bg=_BG, highlightthickness=0, bd=0, height=26)
        self._dots.pack(fill="x", padx=_PAD_X, pady=(16, 0))
        self._head = tk.Canvas(p, bg=_BG, highlightthickness=0, bd=0, height=76)
        self._head.pack(fill="x", padx=_PAD_X)
        self._stage = tk.Canvas(p, bg=_BG, highlightthickness=0, bd=0,
                                height=_STAGE_H)
        self._stage.pack(fill="x", padx=_PAD_X)
        self._task = tk.Canvas(p, bg=_BG, highlightthickness=0, bd=0, height=96)
        self._task.pack(fill="both", expand=True, padx=_PAD_X, pady=(6, 0))
        self._foot = foot = tk.Frame(p, bg=_BG)
        foot.pack(fill="x", side="bottom", padx=_PAD_X, pady=(0, 18))
        self._hints = tk.Canvas(foot, bg=_BG, highlightthickness=0, bd=0,
                                height=30)
        self._hints.pack(side="left", fill="x", expand=True)
        # Next/Previous are small mouse-only glyph buttons, FLAT (no filled
        # background box  just a colored arrow on the panel)  NOT in
        # self._btns, so the dpad/A focus ring never lands on them either.
        # That's deliberate, not an oversight: the bumpers already page the
        # tour directly (see nav_press), so a controller user was never
        # routing through these widgets anyway. Right-to-left so Next ends up
        # rightmost.
        nxt = self._mk_btn(foot, "›", accent=True, flat=True, font_size=13,
                           command=lambda: self._go(+1), padx=6, pady=2)
        prv = self._mk_btn(foot, "‹", flat=True, font_size=13,
                           command=lambda: self._go(-1), padx=6, pady=2)
        for b in (nxt, prv):
            b.pack(side="right", padx=(4, 0))
        self._nxt, self._prv = nxt, prv
        # Skip lives in the top-right CORNER of the panel instead of the
        # footer row  the conventional window-close spot. A sibling of the
        # footer (parented on the panel itself, not `foot`), placed with
        # relx=1.0 so it tracks the panel's width with no resize handler of
        # its own to maintain. _paint_dots leaves it a lane on the right (see
        # _CORNER_RESERVE) so the "n / n" counter can't grow into it.
        self._skp = skp = self._mk_btn(p, "✕", muted=True, command=self._skip,
                                       padx=8, pady=4)
        skp.place(relx=1.0, x=-10, y=10, anchor="ne")
        # Mouse-only, deliberately: ✕ ENDS the tour, and a controller user
        # walking a focus ring across it and pressing A has quit something
        # they were halfway through. self._btns stays empty for the whole
        # tour now (Finish used to be the one thing that populated it  see
        # _style_next)  every reading slide's A press is wired directly in
        # nav_press instead, off the same "no checklist" test _draw_onward
        # uses to decide whether to show that hint at all.
        self._btns = []

    # Width _paint_dots reserves on the right of the dot strip so the slide
    # counter never sits under the corner Skip button (see _build_chrome).
    _CORNER_RESERVE = 36

    def _style_next(self):
        """Next stays the same bare arrow on every slide, last one included.

        It used to swap to a filled green "Finish ✓" pill there  its own
        button, with its own focus-ring plumbing to make A reach it. The last
        slide's actual call-to-action is the "A  finish" hint _draw_onward
        already puts in the task band (the same one every other reading slide
        carries, saying "continue"), so a second, louder button repeating it
        in the footer was two controls asking for the one press."""
        if self._nxt is None:
            return
        bg, fg = _BG, _ACCENT
        try:
            self._nxt.configure(text="›", bg=bg, fg=fg, activebackground=bg,
                                activeforeground=fg,
                                font=(_FONT, 13, "bold"), padx=6, pady=2)
        except tk.TclError:
            return
        # _rest is what the hover/focus painter restores to.
        self._nxt._rest = (bg, fg)

    def _mk_btn(self, parent, text, accent=False, muted=False, flat=False,
               command=None, padx=18, pady=6, font_size=10):
        # flat: no filled background pill, just colored text on the panel 
        # for glyphs that live directly on it (Next/Previous) rather than
        # sitting in the footer row alongside a real button like Skip.
        if flat:
            bg = _BG
            fg = _ACCENT if accent else (_MUTED if muted else _FG)
        else:
            bg = _ACCENT if accent else (_BG if muted else _FIELD)
            fg = _BG if accent else (_MUTED if muted else _FG)
        # highlightthickness stays 2 even for flat buttons  invisible at rest
        # (highlightbackground matches bg), but Next's completion pulse
        # (_pulse_step) needs a ring to turn green, and a flat button has no
        # filled background it could pulse instead.
        b = tk.Button(parent, text=text, relief="flat", bd=0, cursor="hand2",
                      bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                      highlightthickness=2, highlightbackground=_BG,
                      font=(_FONT, font_size, "bold"), padx=padx, pady=pady,
                      command=command)
        b._rest = (bg, fg)
        b.bind("<Enter>", lambda e, w=b: self._hover(w, True))
        b.bind("<Leave>", lambda e, w=b: self._hover(w, False))
        return b

    def _hover(self, w, on):
        """Mouse hover moves the SAME focus the controller moves, so the two
        input methods can never disagree about which button A would press."""
        if on and w in self._btns:
            self._focus = self._btns.index(w)
            self._paint_btns()

    def _paint_btns(self):
        """Focused button: pure white with inverted text  the picker's own
        nav-focus look (see _paint_nav_button)."""
        for i, b in enumerate(self._btns):
            bg, fg = b._rest
            focused = (i == self._focus)
            try:
                if focused and b is self._nxt:
                    # Finish keeps its green pill and takes the ring only 
                    # inverting the one button the last slide is asking for
                    # would hide the very call to action it just became.
                    b.configure(bg=bg, fg=fg, activebackground=bg,
                                activeforeground=fg,
                                highlightbackground="#ffffff")
                elif focused:
                    b.configure(bg="#ffffff", fg="#1b1f27",
                                activebackground="#ffffff",
                                activeforeground="#1b1f27",
                                highlightbackground=_ACCENT)
                else:
                    b.configure(bg=bg, fg=fg, activebackground=bg,
                                activeforeground=fg, highlightbackground=_BG)
            except tk.TclError:
                pass

    # -- drawing helpers -----------------------------------------------------

    def _text_img(self, text, size, fg, bg, font_path=None):
        """A rendered text image, CACHED for the life of the tour.

        kp._text_photo rasterises a TrueType face on every call, and the tour
        asks for the same strings over and over: _txt_w measures a label by
        rendering it and then _txt renders it again to draw it, wrapping
        measures every candidate line, and a slide repaints in full on every
        step that lands. That was the single biggest cost in a slide change.

        Keyed on everything that changes the pixels, and held on the Tutorial
        rather than the module: the images belong to this tour's master
        (self.p.root), so they die with it, and a later tour builds its own.
        The cap is a stop, not a policy  the whole tour is a few hundred
        distinct strings, so it should never be reached."""
        key = (text, size, fg, bg, font_path)
        img = self._txt_cache.get(key)
        if img is None:
            img = kp._text_photo(self.p.root, text, size, fg, bg,
                                 font_path=font_path)
            if img is None:
                return None
            if len(self._txt_cache) > 600:
                self._txt_cache.clear()
            self._txt_cache[key] = img
        return img

    def _txt(self, cv, x, y, text, size=11, fg=_FG, bg=_BG, anchor="w",
             font_path=None):
        """PIL-rendered text on a canvas (greyscale AA, the app's face). The
        surface colour is baked in, so pass the colour actually behind it."""
        img = self._text_img(text, size, fg, bg, font_path)
        if img is None:
            return cv.create_text(x, y, text=text, fill=fg, anchor=anchor,
                                  font=(_FONT, size))
        return cv.create_image(x, y, image=img, anchor=anchor)

    def _txt_w(self, text, size=11, fg=_FG, bg=_BG):
        img = self._text_img(text, size, fg, bg)
        if img is None:
            return len(text) * size * 0.55
        return img.width()

    def _glyph(self, cid, kind=None):
        """The baked Steam glyph PhotoImage for a control on this pad, or
        None when the set has no art for it (grips, squeeze pads)."""
        try:
            return self.p._glyph_image(kind or self._kind, cid)
        except Exception:
            return None

    def _chip(self, cv, cx, cy, cid, kind=None, size=_CHIP, color=_FIELD):
        """A key-cap chip: rounded square in `color` with the control's 28px
        glyph centred in it, or its PRINTED label when there's no glyph. Chips
        keep the small art crisp instead of upscaling it into mush.

        Every Guide/Steam chip in the tour draws the Switch Pro's Home glyph
        instead of the connected pad's own  a one-line override here covers
        every call site (welcome, the chord slides, the commands list) since
        they all render through this one method."""
        kind = kind or self._kind
        glyph_kind, glyph_cid = kind, cid
        if cid == _guide_cid(kind):
            glyph_kind, glyph_cid = _GUIDE_ICON_KIND, _GUIDE_ICON_CID
        h = size // 2
        kp._round_rect(cv, cx - h, cy - h, cx + h, cy + h, 12,
                       fill=color, outline="")
        img = self._glyph(glyph_cid, glyph_kind)
        if img is not None:
            self._imgs.append(img)
            cv.create_image(cx, cy, image=img)
        else:
            lbl = pads.label_for(kind, cid, cid)
            self._txt(cv, cx, cy, lbl[:4], size=11, fg=_FG, bg=color,
                      anchor="center")
        self._register_chip(cv, self._chip_bits(kind, cid), cx, cy, size,
                            color, img)
        return size

    # --- chips that light up while they are held --------------------------
    # Every chip the tour draws is a picture of a button the user has in their
    # hand, so it lights when that button goes down. It is drawn twice: the
    # static art paints the resting cap, and _paint_chips_held repaints the
    # held ones over the top at _STAGE_MS. Deliberately a small shift  the
    # tour asks for chords, and a chip flashing like a klaxon under a HELD
    # Guide button would fight the thing it is trying to teach.

    def _chip_bits(self, kind, cid):
        """Button bit(s) that count as "this chip is being held".

        The Guide/Steam chip resolves to both meta bits: the chord layer opens
        on Steam OR "..." (see _GUIDE_BITS), and the chip stands for whichever
        one this pad's user reaches for. Everything else is its own single bit,
        or None for a control with no bit at all (a printed-label chip for a
        grip sensor), which simply never lights."""
        if cid and cid == _guide_cid(kind):
            return _GUIDE_BITS
        return _bit_for(kind, cid)

    def _register_chip(self, cv, bits, cx, cy, size, color, img):
        """Remember a drawn chip for the animated layer. Stage canvas only:
        _chip also draws into the alt-tab demo window's own canvas, and those
        coordinates mean nothing here."""
        if bits and cv is self._stage:
            self._chips.append((bits, cx, cy, size, color, img))

    def _paint_chips_held(self, cv, held):
        """Repaint the chips whose buttons are down right now."""
        for bits, cx, cy, size, color, img in self._chips:
            if not (held & bits):
                continue
            h = size // 2
            if img == "dpad":
                self._dpad_chip(cv, cx, cy, size, color=self._chip_lit(color),
                                tags=_ANIM_TAG)
                continue
            if img is None:
                # No glyph art, so the cap can't be repainted: its label is PIL
                # text and a per-frame raster is exactly what the animated
                # layer may not do. A ring around it says the same thing and
                # covers nothing.
                kp._round_rect(cv, cx - h - 2, cy - h - 2, cx + h + 2,
                               cy + h + 2, 13, fill="",
                               outline=kp._lerp_color(color, _ACCENT, 0.75),
                               width=2, tags=_ANIM_TAG)
                continue
            kp._round_rect(cv, cx - h, cy - h, cx + h, cy + h, 12,
                           fill=self._chip_lit(color),
                           outline=kp._lerp_color(color, _ACCENT, 0.85),
                           width=2, tags=_ANIM_TAG)
            # The glyph goes back on top. Safe to re-place every frame: these
            # PhotoImages are owned by the picker's own glyph cache, so there
            # is nothing here for Tk to garbage-collect.
            cv.create_image(cx, cy, image=img, tags=_ANIM_TAG)

    @staticmethod
    def _chip_lit(color):
        """A pressed key cap: the same colour, lifted toward the accent. Small
        on purpose  this has to read at a glance without shouting."""
        return kp._lerp_color(color, _ACCENT, 0.34)

    def _live_fresh(self):
        """True while at least one pad is still publishing frames."""
        now = time.monotonic()
        return any(now - ts <= self._LIVE_S
                   for _x, _y, _b, ts in self._live_stick.values())

    def _held_bits(self):
        """Every button currently down, across whichever pads are publishing.
        Zero when nothing is (or nothing is connected), which is what stops the
        animation loop  see _anim_sync."""
        out, now = 0, time.monotonic()
        for _x, _y, b, ts in self._live_stick.values():
            if now - ts <= self._LIVE_S:
                out |= int(b)
        return out

    def _chord_chips(self, cv, cx, cy, cids, kind=None):
        """A row of chips joined by "+", centred on cx. Returns its width."""
        cids = [c for c in cids if c]
        if not cids:
            return 0
        gap = 26
        total = len(cids) * _CHIP + (len(cids) - 1) * gap
        x = cx - total / 2.0 + _CHIP / 2.0
        for i, cid in enumerate(cids):
            if i:
                # bg is the STAGE card, not the panel: PIL bakes the colour it
                # is given in behind the glyph, so _BG here painted a dark
                # square around every "+" on the gyro and Virtual Menu slides.
                self._txt(cv, x - gap / 2.0 - _CHIP / 2.0, cy, "+", size=15,
                          fg=_MUTED, bg=_PANEL_BOX, anchor="center")
            self._chip(cv, x, cy, cid, kind=kind)
            x += _CHIP + gap
        return total

    def _arrow(self, cv, x1, y1, x2, y2, color=_ACCENT, width=3, tags=()):
        cv.create_line(x1, y1, x2, y2, fill=color, width=width, tags=tags,
                       arrow="last", arrowshape=(15, 19, 6), capstyle="round")

    def _panelbox(self, cv, x1, y1, x2, y2, r=14, fill=_PANEL_BOX):
        kp._round_rect(cv, x1, y1, x2, y2, r, fill=fill, outline="")

    def _cw(self, cv):
        """A canvas's real drawable width (falls back to the panel's own while
        geometry is still settling on the very first paint)."""
        try:
            w = cv.winfo_width()
        except tk.TclError:
            w = 1
        if w > 1:
            return w
        return (self._geom[0] if self._geom else _PANEL_W_MAX) - _PAD_X * 2

    def _ch(self, cv, fallback):
        try:
            h = cv.winfo_height()
        except tk.TclError:
            h = 1
        return h if h > 1 else fallback

    # -- render --------------------------------------------------------------

    def _render(self):
        """Repaint every canvas for the current slide."""
        if self._panel is None:
            return
        try:
            self._panel.update_idletasks()
            w = self._panel.winfo_width()
            h = self._panel.winfo_height()
        except tk.TclError:
            return
        if w <= 1:
            w, h = self._panel_size(*self.p._anchor_rect()[2:])
        self._geom = (w, h)
        # Drop the previous slide's image refs in one go; every canvas is
        # about to be cleared, so nothing on screen still needs them.
        self._imgs = []
        s = self._slide
        # The illustration takes whatever the checklist doesn't: the panel is
        # one fixed size for the whole tour (nothing jumps between slides) and
        # a slide with no "try it" rows spends that space on the picture
        # instead of leaving a hole above the footer.
        stage_px, task_px = self._fit_stage(h, s)
        # Each canvas's WIDTH is measured rather than derived from the panel's
        #  the 2px panel border and Tk's own rounding make the two differ by
        # a few pixels, which is enough to clip a right-anchored label. The
        # heights come from _fit_stage instead of being measured, because
        # measuring them would mean flushing a half-changed frame first.
        self._paint_dots(self._cw(self._dots))
        self._paint_head(self._cw(self._head), s)
        self._stage_h = stage_px
        self._paint_stage(self._cw(self._stage), s, stage_px)
        self._paint_task(self._cw(self._task), s, task_px)
        self._paint_hints(s)
        self._style_next()
        self._paint_btns()
        self._sync_pulse()
        self._anim_sync()

    # Fixed vertical chrome above/below the stage+task pair: the dot strip and
    # its top pad, the title block, the task canvas's top pad, the footer's
    # bottom pad and the panel border.
    _CHROME_H = 16 + 26 + 76 + 6 + 18 + 4

    # Extra vertical room a multi-step slide reserves under its header for the
    # progress bar  enough that the bar clears the first row's breathing halo
    # (which reaches ~5px past the tick circle) instead of touching it.
    _BAR_ROOM = 8

    def _task_h(self, s):
        """Height the checklist band needs for one slide. Reserves the note
        line unconditionally on the slides that CAN grow one, so the stage
        doesn't resize under the user when it appears."""
        checks = s.get("checks") or []
        if checks:
            cols = 2 if len(checks) > 2 else 1
            rows = (len(checks) + cols - 1) // cols
            note = 22 if s["id"] in ("osk", "alttab", "vmenu") else 0
            bar = self._BAR_ROOM if len(checks) > 1 else 0
            return 34 + bar + rows * _TASK_ROW_H + note
        if s.get("where") or s.get("unbound"):
            return 62
        # Everything else with no checklist gets the same band: it is where the
        # "press this for the next slide" chip goes (see _draw_onward).
        return 62

    def _fit_stage(self, h, s):
        """Resize the stage/checklist pair for one slide and RETURN the two
        heights.

        Returning them is the point: this used to call update_idletasks() so
        the painters could measure the canvases afterwards, but that flushed a
        paint in between  one frame of the PREVIOUS slide's artwork already
        stretched to the NEW slide's canvas heights, which is what the
        slide-change flicker was. The painters take the numbers directly now,
        so the resize and the repaint land in the same frame."""
        foot = 48
        try:
            fh = self._foot.winfo_height()
            if fh > 1:
                foot = fh
        except tk.TclError:
            pass
        task_h = self._task_h(s)
        stage = int(max(210, h - self._CHROME_H - foot - task_h))
        try:
            self._stage.configure(height=stage)
            self._task.configure(height=task_h)
        except tk.TclError:
            pass
        return stage, task_h

    def _paint_dots(self, sw):
        cv = self._dots
        cv.delete("all")
        n = len(self._slides)
        gap, r = 16, 4
        x = sw / 2.0 - (n - 1) * gap / 2.0
        for i in range(n):
            done = i < self._idx
            cur = i == self._idx
            rr = r + 2 if cur else r
            col = "#ffffff" if cur else (_MUTED if done else "#3a3f47")
            cv.create_oval(x - rr, 13 - rr, x + rr, 13 + rr, fill=col,
                           outline="")
            x += gap
        # Right edge pulls in by _CORNER_RESERVE so the counter never sits
        # under the corner Skip (✕) button (see _build_chrome).
        self._txt(cv, sw - self._CORNER_RESERVE, 13,
                  "%d / %d" % (self._idx + 1, n), size=9,
                  fg=_MUTED, bg=_BG, anchor="e")

    def _paint_head(self, sw, s):
        """Title, plus the one-line caption under it when the slide has one.
        Several slides say everything they need to in the title alone, so a
        blank caption isn't a hole to fill  the title just centres itself in
        the band instead of sitting high with dead space beneath."""
        cv = self._head
        cv.delete("all")
        cap = s.get("caption") or ""
        self._txt(cv, sw / 2.0, 26 if cap else 40, s["title"], size=21,
                  fg=_FG, bg=_BG, anchor="center")
        if cap:
            self._txt(cv, sw / 2.0, 56, cap, size=11, fg=_MUTED, bg=_BG,
                      anchor="center")

    def _paint_stage(self, sw, s, h=None):
        # `h` is passed by _render straight from _fit_stage  the canvas has
        # only just been resized there and measuring it would need a flush
        # (see _fit_stage). Callers repainting a settled canvas omit it.
        cv = self._stage
        cv.delete("all")
        if h is None:
            h = self._ch(cv, _STAGE_H)
        self._panelbox(cv, 0, 0, sw, h - 8, r=16)
        # Dropped BEFORE the art runs: an animated slide republishes it, and a
        # static one must not leave the previous slide's coordinates behind for
        # the loop to keep painting into. Same for the chip register  the art
        # about to run is what fills it.
        self._stage_geom = None
        self._chips = []
        try:
            s["art"](cv, sw, h - 8, s)
        except Exception as e:            # a drawing bug must not trap the user
            print(f"tutorial art '{s['id']}' failed: {e!r}")
        # First frame of the moving parts, so the picture is complete the
        # instant it appears rather than one animation tick later.
        self._paint_stage_anim()

    def _paint_task(self, sw, s, band=None):
        """The "try it" checklist, or the note that stands in for it. Whatever
        it holds is centred in the band between the stage and the footer, so a
        one-line slide doesn't leave the block stranded at the top.

        `band` is passed by _render straight from _fit_stage for the same
        reason _paint_stage takes its height (no measuring flush); every other
        caller repaints an already-settled canvas and just measures it."""
        cv = self._task
        cv.delete("all")
        if band is None:
            band = self._ch(cv, 120)
        checks = s.get("checks") or []
        self._done.setdefault(s["id"], set())
        # What the user should SEE, not what has really landed  a step whose
        # celebration is deferred stays visually pending (see _visible_done).
        done = self._visible_done(s)
        cx = sw / 2.0
        if not checks:
            note = s.get("where")
            if s.get("unbound"):
                mid = band / 2.0
                self._txt(cv, cx, mid - 11,
                          "%s isn't bound on this controller yet."
                          % s["unbound"], size=11, fg=_FG, bg=_BG,
                          anchor="center")
                self._txt(cv, cx, mid + 13,
                          "The Chords tab is where you'd set it.", size=10,
                          fg=_MUTED, bg=_BG, anchor="center")
            elif note:
                mid = band / 2.0
                self._txt(cv, cx, mid - 13, "Find it in", size=10, fg=_MUTED,
                          bg=_BG, anchor="center")
                self._txt(cv, cx, mid + 12, note, size=13, fg=_ACCENT, bg=_BG,
                          anchor="center")
            else:
                # A slide with nothing to press and nothing to point at is one
                # the user is READING, and the only thing it owes them is how
                # to move on when they are done. The corner ✕ and a small grey
                # arrow are not that; this is.
                self._draw_onward(cv, cx, band / 2.0,
                                  self._idx == len(self._slides) - 1)
            # Nothing animated on a slide with no steps  drop the previous
            # slide's geometry so a stale row can never be drawn over this one.
            self._task_geom = None
            return
        all_done = len(done) >= len(checks)
        # The praise line is deliberately BIGGER than the prompt it replaces 
        # PIL text can't be scaled per frame (see the _ANIM_TAG note), so the
        # payoff moment gets its punch from a one-off size/colour jump instead.
        head = (self._praise.get(s["id"], _PRAISE[0]) if all_done
                else ("Try them now" if len(checks) > 1 else "Try it now"))
        # Checklist rows go in up to two columns so four media checks stay on
        # one screen without shrinking the illustration above them. Both
        # columns share one left edge per column (widest label wins), so the
        # tick circles line up instead of drifting with the text length.
        cols = 2 if len(checks) > 2 else 1
        rows = (len(checks) + cols - 1) // cols
        widest = max(self._txt_w(c["text"], size=11, fg=_FG, bg=_BG)
                     for c in checks)
        multi = len(checks) > 1
        bar = self._BAR_ROOM if multi else 0
        item_w = widest + 34
        block_w = item_w * cols + (28 if cols > 1 else 0)
        block_h = 22 + bar + rows * _TASK_ROW_H + (22 if self._note else 0)
        top = max(0, (band - block_h) / 2.0)
        # The prompt is a call to ACTION, so it carries the accent colour
        # rather than the muted grey the rest of the tour's small print uses 
        # and grows again, in green, when it turns into the payoff line.
        self._txt(cv, cx, top + 8, head, size=13 if all_done else 12,
                  fg=_GREEN if all_done else _ACCENT, bg=_BG, anchor="center")
        x0 = cx - block_w / 2.0
        geom_rows = []
        for i, c in enumerate(checks):
            col, row = i % cols, i // cols
            x = x0 + col * (item_w + 28)
            y = top + 30 + bar + row * _TASK_ROW_H
            ok = c["id"] in done
            # The tick circle itself is NOT drawn here  it lives in the
            # animated layer so it can pop, ring and breathe (_paint_task_anim).
            # A done row's label is rastered onto the PILL's colour, not the
            # panel's: PIL text bakes its background in, so a label drawn on
            # _BG kept a dark box around itself once the green tint slid in
            # behind it.
            self._txt(cv, x + 26, y, c["text"], size=11,
                      fg=_GREEN if ok else _FG,
                      bg=_ROW_DONE_BG if ok else _BG, anchor="w")
            geom_rows.append((x, y, item_w, c["id"]))
        if self._note:
            # Clamped into the band: the note is optional text appended under
            # a block that was centred without it in mind, and on a one-row
            # checklist it ran off the bottom edge of the canvas.
            ny = min(top + 34 + bar + rows * _TASK_ROW_H, band - 9)
            self._txt(cv, cx, ny, self._note, size=10, fg=_MUTED, bg=_BG,
                      anchor="center")
        self._task_geom = {"rows": geom_rows, "cx": cx, "top": top,
                           "band": band, "block_w": block_w, "sw": sw,
                           "n": len(checks), "multi": multi}
        self._paint_task_anim()

    def _draw_onward(self, cv, cx, cy, last):
        """"Take your time, then press this"  the A glyph for the button that
        moves the tour on, with the word beside it.

        Drawn for the READING slides (no checklist of their own)  the last
        slide (Finish) and the one before it (Continue) both point at the same
        button now, rather than the last slide alone naming A and the other
        pointing at the bumper: one glyph for "this is how you move on" reads
        as one idea instead of two. A is wired to fire it on every reading
        slide (see nav_press's own reading-slide branch), not just the last."""
        text = "finish" if last else "continue"
        # The bare glyph, no key cap around it, at small text weight: this is
        # a line of small print telling you the way out, not a button to look
        # at. A capped chip here read as loud as the checklist it replaced.
        icon = self._glyph("a")
        iw = float(icon.width()) if icon is not None else 0.0
        gap = 8
        tw = self._txt_w(text, size=10, fg=_MUTED, bg=_BG)
        x = cx - (iw + gap + tw) / 2.0
        if icon is not None:
            self._imgs.append(icon)
            cv.create_image(x + iw / 2.0, cy, image=icon)
        else:
            x -= gap
        self._txt(cv, x + iw + gap, cy, text, size=10, fg=_MUTED, bg=_BG,
                  anchor="w")

    # -- checklist animation -------------------------------------------------

    def _anim_reset(self, s=None):
        """Drop every animation this slide accumulated and re-seed the
        progress bar at whatever the NEW slide already has done (so stepping
        Back onto a finished slide shows a full bar, not one sliding in)."""
        s = s or self._slide
        checks = s.get("checks") or []
        done = self._done.get(s["id"]) or set()
        frac = (len(done) / float(len(checks))) if checks else 0.0
        self._anim_pop = {}
        self._anim_done_t = None
        self._confetti = []
        # A deferred celebration belongs to the slide being LEFT; leaving
        # cancels it outright rather than firing it over the next slide.
        self._defer_pop = []
        self._defer_finish = False
        self._defer_at = None
        self._bar_from = self._bar_target = frac
        self._bar_t0 = time.monotonic()
        self._anim_t0 = time.monotonic()

    def _visible_done(self, s):
        """The done-set as the user should currently SEE it. A step whose
        celebration is deferred has really landed  it's in self._done, and
        every rule keyed off that (the OSK slide's ordering, "finished") uses
        the real set  but it reads as still-to-do in the UI until the reveal,
        so the tick, the label colour, the praise line and the progress bar
        all arrive together with the animation instead of ahead of it."""
        done = self._done.get(s["id"]) or set()
        if not self._defer_pop:
            return done
        return done - set(self._defer_pop)

    def _anim_ready(self):
        """True when the checklist is genuinely on screen for the user to look
        at: the overlay isn't parked behind another app, the manager window is
        up, and the always-on-top keyboard isn't covering the band.

        Deliberately does NOT also require kp._app_is_active() (real Windows
        OS-foreground). The panel is -topmost, so it stays visually above
        every other window regardless of which one currently "has focus" 
        that's a z-order property, not a foreground one. Requiring real
        foreground on top of that held the keyboard slide's confetti hostage
        on real hardware: closing the OSK leaves no explicit focus restore
        while the tour claims it (see tray.py's tutorial_claimed() branch), so
        _app_is_active() could stay false for a while after the close was
        already visible and done  the payoff waited on something the user
        couldn't see or do anything about. self._hidden (which DOES factor in
        _app_is_active, debounced, in _poll_foreground) is what actually
        controls whether the panel is withdrawn; that's the real gate."""
        try:
            if self._hidden or not self._root_on_screen():
                return False
        except tk.TclError:
            return False
        return not _osk_is_open()

    def _poll_osk_close(self):
        """Land the keyboard slide's "Open the keyboard" and "Close the
        keyboard" steps, by WATCHING the keyboard rather than the buttons.

        The open used to be landed off the controller frame carrying the X
        press (feed()), and on real hardware that frame frequently never
        arrives: the press IS the open, and the tray hands the controller
        straight to adusk (the adusk_app.main() handoff in launcher_thread
        closes the HID watcher), so sc_viewer stops publishing before the
        tour has seen it. "osk" then never landed  and since both later
        steps require it, the whole slide silently did nothing: no typing
        tick, no close tick, no confetti. The keyboard APPEARING can't be
        missed, and it counts however the user opened it.

        The close is the same story in reverse: nothing reaches feed() while
        the OSK owns the pad, so the press that closes it is invisible. Its
        disappearance is not."""
        open_now = _osk_is_open()
        was, self._osk_open_prev = self._osk_open_prev, open_now
        s = self._slide
        if open_now and not was:
            # Rising edge: the keyboard is up, so the "open" step is done 
            # whichever button did it.
            if any(c.get("id") == "osk" for c in (s.get("checks") or [])):
                done = self._done.setdefault(s["id"], set())
                if "osk" not in done:
                    was_done = set(done)
                    done.add("osk")
                    self._commit(s, was_done)
            return
        if not was or open_now:
            return                      # no falling edge this tick
        if not any(c.get("id") == "type" for c in (s.get("checks") or [])):
            return                      # not the keyboard slide
        done = self._done.setdefault(s["id"], set())
        # Only after it was OPENED as this slide's first step  a keyboard the
        # user had up for their own reasons must not tick anything.
        if "osk" not in done:
            return
        # The third row ("Close the keyboard") has no checklist entry of its
        # own  closing is what carries the slide onward  but it is still a
        # thing the user just DID, so the row goes green like the other two.
        self._osk_shut = True
        self._repaint_live_art(s)
        if "type" not in done:
            # Closed without ever typing "hi": tick it anyway and celebrate
            # now. The keyboard is gone, there is no way left to satisfy that
            # box, and the point of the step (press some keys, watch them
            # appear) has been made either way.
            was_done = set(done)
            done.add("type")
            self._commit(s, was_done)
            return
        # Already finished (they typed "hi" while it was up, confetti and
        # all)  closing is simply the cue to carry on.
        self._advance_when_osk_closed()

    def _poll_osk_typing(self):
        """Echo what's being typed on the demo keyboard, and land the "type hi"
        step when it lands.

        Read from adusk rather than from the keys themselves: the OSK types
        into whatever window has focus, which during the tour is the manager
        (the keyboard must not raise the user's last app over the tour), and
        the manager has no text box  so this echo IS the feedback. Driven off
        _poll_foreground's 150 ms tick like the other pollers."""
        if not self._typing_watch:
            return
        try:
            from adusk import state as adusk_state
            keys = adusk_state.get_typed_keys()
        except Exception:
            return
        if len(keys) == self._typed_seen:
            return
        self._typed_seen = len(keys)
        self._typed = _typed_text(keys)
        s = self._slide
        done = self._done.setdefault(s["id"], set())
        # The box first  it's the whole point of the echo, and it has to
        # update on EVERY key, not only on the one that lands the step (that
        # one gets a second stage repaint from _commit's _repaint_live_art;
        # one extra paint on one frame is not worth a flag to dodge).
        try:
            self._paint_stage(self._cw(self._stage), s, self._stage_h)
        except tk.TclError:
            pass
        if ("hi" in self._typed.lower() and "type" not in done
                and "osk" in done
                and any(c.get("id") == "type"
                        for c in (s.get("checks") or []))):
            was_done = set(done)
            done.add("type")
            self._commit(s, was_done)

    def wants_tray_click(self):
        """True while the welcome slide's "click the tray icon" step is still
        outstanding. The picker asks BEFORE acting on a tray click: for as long
        as this is true that click must not hide the window, because hiding it
        closes the tour (see _Picker._hide_inner)  the tour would delete
        itself over the one thing it asked for."""
        if self._panel is None:
            return False
        s = self._slide
        if not any(c.get("id") == "tray" for c in (s.get("checks") or [])):
            return False
        return "tray" not in (self._done.get(s["id"]) or set())

    def note_tray_click(self):
        """The user clicked the tray icon  land the welcome slide's step.
        Called on the Tk thread from _Picker._toggle_visibility, which has just
        revealed/raised the window."""
        if not self.wants_tray_click():
            return
        s = self._slide
        done = self._done.setdefault(s["id"], set())
        was_done = set(done)
        done.add("tray")
        self._commit(s, was_done)

    def _defer_poll(self, now):
        """Drive any held-back celebration toward its reveal. Called from
        _poll_foreground (already ticking at 150 ms), so this costs nothing
        when nothing is waiting. The settle timer RESTARTS if we lose the
        screen again mid-wait  the animation only ever plays to a user who
        has been looking at it for _DEFER_S."""
        if not (self._defer_pop or self._defer_finish):
            return
        if not self._anim_ready():
            self._defer_at = None
            return
        if self._defer_at is None:
            self._defer_at = now + _DEFER_S
        elif now >= self._defer_at:
            self._release_deferred()

    def _release_deferred(self):
        """The reveal: tick the held steps, pop them, and throw the confetti
        if this was the last one."""
        now = time.monotonic()
        finish = self._defer_finish
        pops = self._defer_pop
        self._defer_pop = []
        self._defer_finish = False
        self._defer_at = None
        s = self._slide
        for cid in pops:
            self._anim_pop[cid] = now
        if finish:
            self._praise.setdefault(s["id"], random.choice(_PRAISE))
        # Repaint first  it publishes the geometry the confetti launches
        # from, and flips the labels/header into their done state. The
        # illustration flips with them on a slide that shows its own progress:
        # the reveal is the moment the step becomes visibly done, everywhere.
        self._repaint_live_art(s)
        checks = s.get("checks") or []
        if checks:
            self._bar_from = self._bar_frac(now)
            self._bar_target = (len(self._visible_done(s))
                                / float(len(checks)))
            self._bar_t0 = now
        try:
            self._paint_task(self._cw(self._task), s)
        except tk.TclError:
            return
        if finish:
            self._anim_done_t = now
            if self._task_geom is not None:
                self._seed_confetti(self._task_geom)
            self._arm_auto_advance(s)
        _haptic()
        self._sync_pulse()
        self._anim_sync()

    def _bar_frac(self, now):
        """Progress-bar fill, eased from where it was toward where it should
        be. Time-based rather than per-frame stepped, so the glide takes the
        same _BAR_S no matter what cadence the loop is running at."""
        if self._bar_target == self._bar_from:
            return self._bar_target
        t = min(1.0, max(0.0, (now - self._bar_t0) / _BAR_S))
        return self._bar_from + (self._bar_target - self._bar_from) * _ease_out(t)

    def _anim_motion(self, now):
        """True while something is genuinely MOVING (as opposed to the idle
        breathing)  the loop runs at full rate only for these."""
        for t0 in self._anim_pop.values():
            if now - t0 < _POP_S:
                return True
        if self._anim_done_t is not None and now - self._anim_done_t < _CONFETTI_S:
            return True
        return abs(self._bar_frac(now) - self._bar_target) > 0.001

    def _anim_sync(self):
        """(Re)arm the animation loop, or leave it stopped.

        It runs at ~60 Hz while something is moving, drops to ~20 Hz when only
        the pending-row breathing needs it, and STOPS COMPLETELY once every
        step is done and the last burst has finished  a tour left open on a
        finished slide costs nothing."""
        if self._anim_aid is not None:
            try:
                self.p.root.after_cancel(self._anim_aid)
            except tk.TclError:
                pass
            self._anim_aid = None
        if self._panel is None:
            return
        s = self._slide
        checks = s.get("checks") or []
        # A slide whose illustration animates keeps the loop alive on its own,
        # checklist or no checklist, and for as long as the slide is up  the
        # motion IS the picture, so it can't stop when the steps are done.
        # Chips that light while held do the same, but ONLY while some pad is
        # actually publishing: with nothing connected nothing can be held, so
        # an unattended tour still costs nothing. feed() restarts the loop when
        # a controller turns up (see its _anim_sync call).
        stage = s.get("stage_anim") is not None and self._stage_geom is not None
        if self._chips and self._live_fresh():
            stage = True
        if not checks and not stage:
            return
        now = time.monotonic()
        moving = self._anim_motion(now)
        # Visible-done, so a step waiting on its reveal keeps breathing rather
        # than freezing the loop on a row that still looks unfinished.
        pending = bool(checks) and len(self._visible_done(s)) < len(checks)
        if not (moving or pending or stage):
            return
        delay = _ANIM_MS if moving else (_STAGE_MS if stage else _IDLE_MS)
        try:
            self._anim_aid = self.p.root.after(delay, self._anim_tick)
        except tk.TclError:
            pass

    def _anim_tick(self):
        self._anim_aid = None
        if self._panel is None:
            return
        try:
            self._paint_task_anim()
        except tk.TclError:
            return
        self._anim_sync()

    def _paint_task_anim(self):
        """Redraw ONLY the animated primitives, tagged so they can be cleared
        without touching the (expensive) static text underneath. Lowered below
        the labels at the end so a row's tint sits behind its text."""
        g = self._task_geom
        cv = self._task
        # The stage carries the all-done confetti (see _seed_confetti), so its
        # animated layer is cleared here too  unconditionally, so a burst
        # still in flight is wiped even if the checklist geometry has gone.
        try:
            self._stage.delete(_ANIM_TAG)
        except tk.TclError:
            pass
        # ...and repainted here, before the early-out below: this is the one
        # loop that owns the tagged layer, so a slide whose ILLUSTRATION moves
        # rides on it rather than starting a second timer of its own.
        self._paint_stage_anim()
        if g is None:
            return
        cv.delete(_ANIM_TAG)
        s = self._slide
        done = self._visible_done(s)   # a deferred step still draws as pending
        now = time.monotonic()
        if g["multi"]:
            self._draw_progress_bar(cv, g, now)
        for i, (x, y, item_w, cid) in enumerate(g["rows"]):
            ok = cid in done
            pop_t0 = self._anim_pop.get(cid)
            age = (now - pop_t0) if pop_t0 is not None else None
            if ok:
                self._draw_row_done(cv, x, y, item_w, age)
            else:
                self._draw_row_pending(cv, x, y, now, i)
        try:
            cv.tag_lower(_ANIM_TAG)
        except tk.TclError:
            pass
        # Drawn last, and into its own canvas  the tag_lower above must not
        # push the confetti behind the slide art.
        if self._anim_done_t is not None:
            self._draw_confetti(now - self._anim_done_t)

    def _paint_stage_anim(self):
        """Repaint the moving parts of the slide's ILLUSTRATION.

        Same contract as the checklist's animated layer: everything drawn here
        carries _ANIM_TAG (already cleared by the caller) and is pure canvas
        vector work  no PIL text, which costs a full TrueType raster per call
        and cannot be redrawn at 30 Hz. The static art publishes the geometry
        it wants animated in self._stage_geom, so a frame never re-measures
        anything."""
        s = self._slide
        fn = s.get("stage_anim")
        g = self._stage_geom
        try:
            if fn is not None and g is not None:
                fn(self._stage, g, time.monotonic() - self._anim_t0)
            # Every slide gets this one, not just the animated ones: a chip is
            # a picture of a button, and it lights while that button is down.
            held = self._held_bits() if self._chips else 0
            if held:
                self._paint_chips_held(self._stage, held)
        except tk.TclError:
            pass
        except Exception as e:        # a drawing bug must not trap the user
            print(f"tutorial stage anim '{s['id']}' failed: {e!r}")

    def _draw_progress_bar(self, cv, g, now):
        """A slim fill under the header on multi-step slides: it answers "how
        many left?" at a glance and gives every landed step something visible
        to move."""
        w = g["block_w"]
        x0 = g["cx"] - w / 2.0
        y = g["top"] + 20
        kp._round_rect(cv, x0, y - 2, x0 + w, y + 2, 2, fill=_BAR_TRACK,
                       outline="", tags=_ANIM_TAG)
        frac = self._bar_frac(now)
        if frac <= 0.0:
            return
        fw = max(4.0, w * frac)
        # Amber while there's work left, green the moment it's full  the same
        # "you're done" colour the ticks and the praise line use.
        col = _GREEN if frac >= 0.999 else kp._lerp_color(_ACCENT, _GREEN, frac)
        kp._round_rect(cv, x0, y - 2, x0 + fw, y + 2, 2, fill=col,
                       outline="", tags=_ANIM_TAG)

    def _draw_row_pending(self, cv, x, y, now, i):
        """A step still to do: the empty circle breathes on an accent halo.
        Rows are phase-STAGGERED so a four-step slide reads as a row of
        separate invitations rather than one throbbing block."""
        cx = x + 8
        ph = (now - self._anim_t0) / _BREATHE_S * 2 * math.pi - i * 0.7
        puls = 0.5 + 0.5 * math.sin(ph)
        halo_r = 9.0 + 4.5 * puls
        cv.create_oval(cx - halo_r, y - halo_r, cx + halo_r, y + halo_r,
                       fill="", outline=kp._lerp_color(_BG, _ACCENT,
                                                       0.10 + 0.32 * puls),
                       width=2, tags=_ANIM_TAG)
        ring = kp._lerp_color("#4a505a", _ACCENT, 0.25 + 0.5 * puls)
        cv.create_oval(cx - 8, y - 8, cx + 8, y + 8, fill="", outline=ring,
                       width=2, tags=_ANIM_TAG)

    def _draw_row_done(self, cv, x, y, item_w, age):
        """A landed step: a green tint sweeps in behind the whole row, the
        circle pops past its size and settles, and a ring + sparks burst out
        of it. `age` is None once the animation has played out (a slide
        revisited later just shows the settled state)."""
        cx = x + 8
        t = None if age is None else min(1.0, age / _POP_S)
        # Row tint  fades in over its own shorter window so the background
        # has already arrived by the time the circle finishes settling.
        pill_t = 1.0 if age is None else min(1.0, age / _PILL_S)
        if pill_t > 0.01:
            # item_w is label width + 34, and the label starts 26 in, so the
            # pill runs from just left of the circle to just past the text.
            kp._round_rect(cv, x - 6, y - 12, x + item_w - 2, y + 12, 11,
                           fill=kp._lerp_color(_BG, _ROW_DONE_BG, pill_t),
                           outline="", tags=_ANIM_TAG)
        scale = 1.0
        if t is not None and t < 1.0:
            # Out past full size, then back  the overshoot is what makes it
            # read as a "pop" instead of a fade.
            if t < 0.34:
                scale = 1.34 * _ease_out(t / 0.34)
            else:
                scale = 1.34 - 0.34 * _ease_out((t - 0.34) / 0.66)
            # Burst ring flying outward as the circle lands. Kept SMALL on
            # purpose: rows are only _TASK_ROW_H apart, so a wide ring reads
            # as a mess crossing its neighbours rather than a clean pop.
            rr = 8.0 + 11.0 * _ease_out(t)
            cv.create_oval(cx - rr, y - rr, cx + rr, y + rr, fill="",
                           outline=kp._lerp_color(_GREEN, _BG, t),
                           width=2, tags=_ANIM_TAG)
            # ...and a few sparks with it, inside that same radius budget.
            for k in range(6):
                a = k * (math.pi / 3.0) + 0.4
                r0 = 9 + 8 * _ease_out(t)
                r1 = r0 + 4 * (1 - t)
                cv.create_line(cx + math.cos(a) * r0, y + math.sin(a) * r0,
                               cx + math.cos(a) * r1, y + math.sin(a) * r1,
                               fill=kp._lerp_color(_GREEN, _BG, t),
                               width=2, capstyle="round", tags=_ANIM_TAG)
        r = 8.0 * scale
        cv.create_oval(cx - r, y - r, cx + r, y + r, fill=_GREEN,
                       outline="", tags=_ANIM_TAG)
        m = scale
        cv.create_line(cx - 4 * m, y, cx - 1 * m, y + 4 * m,
                       cx + 4 * m, y - 4 * m, fill=_BG,
                       width=2, capstyle="round", joinstyle="round",
                       tags=_ANIM_TAG)

    def _seed_confetti(self, g):
        """Throw a burst of particles up over the ILLUSTRATION, not the
        checklist: the task band is only ~70px tall, so anything launched
        inside it leaves the canvas before the eye catches it. The stage is
        ~300px of mostly-empty card and reads as the celebration surface.

        Seeded ONCE (each frame integrates these, rather than re-randomising)
        so the burst is a real trajectory instead of static jitter."""
        rnd = random.Random(0xC0FFEE)
        try:
            w = max(200, self._stage.winfo_width())
            h = max(120, self._stage.winfo_height())
        except tk.TclError:
            return
        out = []
        for _ in range(34):
            a = -math.pi / 2 + rnd.uniform(-0.85, 0.85)
            spd = rnd.uniform(300.0, 620.0)
            out.append((w / 2.0 + rnd.uniform(-70, 70), h - 12.0,
                        math.cos(a) * spd, math.sin(a) * spd,
                        rnd.choice(_CONFETTI_COLORS), rnd.uniform(2.0, 4.0)))
        self._confetti = out

    def _draw_confetti(self, age):
        """Integrate + draw the burst onto the stage. Ballistic (gravity pulls
        them back down) and fading into the card colour, so it clears itself.
        Raised above the slide art  confetti in FRONT is the whole point."""
        cv = self._stage
        if age >= _CONFETTI_S or not self._confetti:
            return
        t = age
        fade = min(1.0, age / _CONFETTI_S)
        for x0, y0, vx, vy, col, size in self._confetti:
            x = x0 + vx * t
            y = y0 + vy * t + 0.5 * 900.0 * t * t
            sz = size * (1.0 - 0.35 * fade)
            cv.create_rectangle(x - sz, y - sz, x + sz, y + sz,
                                fill=kp._lerp_color(col, _PANEL_BOX, fade),
                                outline="", tags=_ANIM_TAG)
        try:
            cv.tag_raise(_ANIM_TAG)
        except tk.TclError:
            pass

    def _paint_hints(self, s):
        """Footer left side: deliberately blank now that the A/B glyph hints
        are gone (Select/Back no longer apply the way they used to  Skip is
        the only gamepad-focusable footer control, see _build_chrome). Left
        as a cleared canvas rather than removed outright: it's still the
        expand="x" spacer that keeps Skip/Previous/Next pinned to the right
        of the footer row."""
        self._hints.delete("all")

    # -- "press it" feedback -------------------------------------------------

    def _sync_pulse(self):
        """Pulse the Next button once a slide's presses are all in  the nudge
        that says "this one's done, carry on" without moving anything for the
        user. Visible-done, so it doesn't start nudging them onward while the
        step's own celebration is still waiting to be seen."""
        s = self._slide
        checks = s.get("checks") or []
        want = bool(checks) and len(self._visible_done(s)) >= len(checks)
        if self._pulse_aid is not None:
            try:
                self.p.root.after_cancel(self._pulse_aid)
            except tk.TclError:
                pass
            self._pulse_aid = None
        self._pulse_on = False
        if want:
            self._pulse_step()

    def _pulse_step(self):
        self._pulse_aid = None
        if self._panel is None or self._nxt is None:
            return
        self._pulse_on = not self._pulse_on
        # No focus check needed anymore: Next is mouse-only (not in
        # self._btns), so _paint_btns never overwrites its highlight color 
        # unlike the old 3-button row, where the pulse had to stand down
        # while Next itself held the white focus ring.
        try:
            self._nxt.configure(
                highlightbackground=_GREEN if self._pulse_on else _BG)
        except tk.TclError:
            return
        try:
            self._pulse_aid = self.p.root.after(520, self._pulse_step)
        except tk.TclError:
            pass

    # -- live press detection ------------------------------------------------

    def feed(self, ch, fr):
        """One published controller frame from channel `ch` ("sc" or "sdl").
        Called by the picker's nav pump at ~30Hz for BOTH channels, so a press
        counts from whichever pad the user actually picked up."""
        if fr is None:
            # Controller gone quiet: forget the edge state so the first frame
            # after it comes back isn't read as "already held", and drop the
            # live stick so a slide drawing it doesn't keep showing a pad that
            # has left the building.
            self._prev_btn[ch] = 0
            self._zone_prev[ch] = "NEUTRAL"
            self._live_stick.pop(ch, None)
            return
        # Gated on the panel existing, but deliberately NOT on self._hidden:
        # the overlay parks itself whenever another app is foreground
        # (_poll_foreground), and Alt-Tab's entire job is to make that happen 
        # so a hidden-means-deaf rule would drop the one press that slide
        # exists to detect. The checks are all Guide-held chords, which belong
        # to us wherever the user is. Painting a withdrawn canvas is fine; it
        # shows the ticked state when the tour comes back.
        if self._panel is None:
            return
        s = self._slide
        checks = s.get("checks") or []
        b = int(getattr(fr, "buttons", 0))
        prev = self._prev_btn.get(ch, 0)
        self._prev_btn[ch] = b
        newly = b & ~prev
        lx = int(getattr(fr, "lstick_x", 0))
        ly = int(getattr(fr, "lstick_y", 0))
        zone = _stick_zone(lx, ly)
        zprev = self._zone_prev.get(ch, "NEUTRAL")
        self._zone_prev[ch] = zone
        # Published for the artwork BEFORE the "has this slide anything to
        # detect" early-out below: the drawing is live whether or not there is
        # a step outstanding, which is what makes a finished media slide still
        # follow the stick while the user plays with it.
        self._live_stick[ch] = (lx, ly, b, time.monotonic())
        # A pad turning up mid-slide is what starts the chip-lighting loop 
        # _anim_sync stands it down when nothing is publishing, and nothing
        # else would ever wake it again on a slide with no other animation.
        if self._anim_aid is None and self._chips:
            self._anim_sync()
        if not checks:
            return
        done = self._done.setdefault(s["id"], set())
        was_done = set(done)      # diffed below to time each landed step's pop
        guide = bool(b & _GUIDE_BITS)
        hit = False
        for c in checks:
            if c["id"] in done:
                continue
            if c.get("poll"):
                continue      # landed by a poller, not by a frame (see "close")
            if c.get("guide") and not guide:
                continue
            if c.get("after") and c["after"] not in was_done:
                # Ordered pair (gyro on -> gyro off): both share one trigger,
                # so without this the FIRST press satisfies both. Gated on the
                # SNAPSHOT, not the live set  `done` is being mutated by this
                # very loop, so testing it would see the prerequisite land a
                # moment ago and wave the follow-up straight through on the
                # same frame.
                continue
            if "bit" in c:
                if c["bit"] and (newly & c["bit"]):
                    done.add(c["id"])
                    hit = True
            elif "stick" in c:
                if zone == c["stick"] and zone != zprev:
                    done.add(c["id"])
                    hit = True
            elif "pair" in c:
                # Rising edge of "both held", not level: the gyro toggle needs
                # a SECOND check on the same two buttons for "turn it off
                # again" (see the "gyro_off" check below), and a level test
                # would just re-satisfy instantly off the same held frame the
                # first check consumed instead of waiting for a fresh press.
                a, d = c["pair"]
                if a and d and (b & a) and (b & d) and not (prev & a and prev & d):
                    done.add(c["id"])
                    hit = True
            elif "vmenu_icon" in c:
                # Not a button at all: the tray tells us which menu entry
                # fired (see sc_viewer.note_vmenu_fire). Edge-detected on the
                # sequence so one press counts once, and matched on the icon
                # so pressing one of the demo menu's blank cells doesn't pass.
                seq, icon = _vmenu_fire()
                if seq != self._vmenu_seq:
                    self._vmenu_seq = seq
                    if icon == c["vmenu_icon"]:
                        done.add(c["id"])
                        hit = True
        if not hit:
            return
        self._commit(s, was_done)

    # How stale a published frame may be before the artwork stops believing it.
    # Comfortably longer than the nav pump's ~33 ms tick, short enough that
    # unplugging a pad drops the drawn stick back to centre while the user is
    # still looking at it.
    _LIVE_S = 0.5

    def _stick_state(self, press_bit=None):
        """The left stick RIGHT NOW as (dx, dy, pressed, guide): axes
        normalised to -1..1 with dy already in SCREEN direction (down
        positive), `pressed` true while `press_bit` is held, and `guide` true
        while the chord layer is open.

        Picks between channels rather than merging them  with a Steam
        Controller and an SDL pad both publishing, the one being pushed (or
        clicked) is the one in the user's hands, and a pad resting at centre
        must not drag the drawing back. With no pad at all everything is zero,
        which draws a stick sitting perfectly still: true, and the only honest
        thing to show."""
        best, score, now = (0.0, 0.0, False, False), -1.0, time.monotonic()
        for x, y, b, ts in self._live_stick.values():
            if now - ts > self._LIVE_S:
                continue
            dx = max(-1.0, min(1.0, x / 32767.0))
            # HID/SDL sticks are y-up (see _stick_zone); canvases are y-down.
            dy = max(-1.0, min(1.0, -y / 32767.0))
            pressed = bool(press_bit and (b & press_bit))
            s = math.hypot(dx, dy) + (2.0 if pressed else 0.0)
            if s > score:
                score, best = s, (dx, dy, pressed, bool(b & _GUIDE_BITS))
        return best

    def _repaint_live_art(self, s):
        """Repaint the ILLUSTRATION for a slide that shows its own progress.

        The media slide turns a landed direction's label green and gives it a
        tick, and every bit of that is PIL text  it cannot live in the 30 Hz
        animated layer (one TrueType raster per call). So the stage is redrawn
        once, here, on the same beat as the checklist. Only the stage canvas is
        touched and it is cleared and refilled inside one call, so there is no
        blank frame  the same repaint _poll_media has always done when the
        now-playing card changes."""
        if not s.get("live_art"):
            return
        try:
            self._paint_stage(self._cw(self._stage), s, self._stage_h)
        except tk.TclError:
            pass

    def _commit(self, s, was_done):
        """Everything that follows one or more steps landing: pop timing, the
        deferred-reveal queue, the progress bar, praise and the repaint.
        Shared by the frame path (feed) and the poll path (_poll_osk_close),
        which is why it re-derives the landed set from `was_done` rather than
        being told what changed."""
        checks = s.get("checks") or []
        done = self._done.setdefault(s["id"], set())
        if not checks:
            return
        now_t = time.monotonic()
        by_id = {c["id"]: c for c in checks}
        landed = done - was_done
        # A step whose own action hid the tour keeps its celebration back until
        # the user is looking again (_defer_poll); everything else pops now.
        # Several can land in one frame  the gyro pair, a fast double press.
        deferred = [cid for cid in landed if by_id.get(cid, {}).get("defer")]
        for cid in landed:
            if cid not in deferred:
                self._anim_pop[cid] = now_t
        self._defer_pop.extend(deferred)
        # The bar tracks what the user can SEE, so a deferred step doesn't
        # slide it forward early. Started from where it currently shows (not
        # from the old value) so a second step landing mid-glide continues
        # smoothly instead of snapping back.
        self._bar_from = self._bar_frac(now_t)
        self._bar_target = len(self._visible_done(s)) / float(len(checks))
        self._bar_t0 = now_t
        finished = len(done) >= len(checks)
        # ...but "finished" itself is the real state: the follow-up actions
        # below (Alt-Tab's come-back raise) have to run whether or not the
        # celebration is being held.
        if finished and not deferred:
            self._praise[s["id"]] = random.choice(_PRAISE)
            if s["id"] == "vmenu":
                # The "Find it in ..." pointer every non-interactive slide
                # carries  this one has checks instead, so it arrives here.
                self._note = "Build your own in Controller › Virtual Menus."
        if not deferred:
            _haptic()          # the reveal brings its own tick with it
        # A landed step can close the nav-mask hole it needed (the bare-X open).
        self._sync_nav_keep()
        self._repaint_live_art(s)
        try:
            self._paint_task(self._cw(self._task), s)
        except tk.TclError:
            return
        self._sync_pulse()
        if finished:
            if deferred:
                self._defer_finish = True
            else:
                # Confetti is seeded AFTER the repaint, which is what publishes
                # the geometry it launches from (_task_geom).
                self._anim_done_t = now_t
                if self._task_geom is not None:
                    self._seed_confetti(self._task_geom)
                self._arm_auto_advance(s)
        self._anim_sync()
        if finished:
            self._after_step(s)

    def _arm_auto_advance(self, s):
        """Once a slide's celebration has actually played, page onward by
        itself after _AUTO_ADVANCE_MS. No-ops past the last slide  a finished
        checklist should never be what silently CLOSES the tour; only Next/
        Skip do that. Armed through the same _after()/_cancel_afters() queue
        every other one-shot timer here uses, so a manual Previous/Next/Skip
        (which calls _cancel_afters in _go) drops it for free."""
        if self._idx >= len(self._slides) - 1:
            return
        if s.get("id") == "osk" and _osk_is_open():
            # The keyboard slide is finished the moment "hi" is typed, but the
            # keyboard is still up  paging out from under it would leave it
            # covering the next slide. Closing it is the cue; _poll_osk_close
            # comes back here once it has gone.
            self._note = "Close the keyboard to carry on."
            return
        self._after(_AUTO_ADVANCE_MS, lambda: self._go(+1))

    def _advance_when_osk_closed(self):
        """The keyboard slide's onward step: it finished while the keyboard was
        still up (see _arm_auto_advance), and the keyboard has just gone."""
        s = self._slide
        if s.get("id") != "osk":
            return
        self._note = None
        self._reclaim_front()
        try:
            self._paint_task(self._cw(self._task), s)
        except tk.TclError:
            pass
        self._after(_AUTO_ADVANCE_MS, lambda: self._go(+1))

    def _after_step(self, s):
        """What happens once a slide's presses are all in.

        Alt-Tab by definition hands the foreground to another window, which
        parks this overlay (see _poll_foreground). Nothing on screen would
        then say how to get back, so the tour brings itself forward again.
        (The keyboard slide needs no such recovery: the user closes the demo
        keyboard themselves as its last step  see feed()'s "close" check 
        so there's nothing left for the tour to clean up.)"""
        if s["id"] == "alttab":
            self._after(_RETURN_MS, self._return_to_tour)
        elif s["id"] == "osk" and not _osk_is_open():
            # The keyboard took the controller and the foreground with it, and
            # closing it hands them wherever Windows feels like  come back to
            # the front. Only once it is actually GONE: this slide finishes
            # while the keyboard is still up (typing "hi"), and raising the
            # manager over it then would bury the very thing being typed on.
            self._after(250, self._reclaim_front)

    def _reclaim_front(self):
        """Take the foreground back for the manager, without the park logic
        reading the hand-over as "the user went somewhere else"."""
        self._no_park_until = time.monotonic() + _REVEAL_GRACE_S
        try:
            self.p._bring_front()
        except Exception as e:
            print(f"tutorial: could not reclaim the foreground: {e!r}")
        self._restack_after_raise()

    def _restack_after_raise(self):
        """Put the panel back above the scrim after a manager raise  now, and
        again once the raise has finished flailing.

        _bring_front's foreground steal re-asserts itself at 80ms and 120ms
        (_reassert_foreground), and every one of those activations restacks
        the manager's owned windows  so fixing the order only at the moment
        we ask is not enough."""
        self._fix_stack_if_needed()
        for ms in (160, 320):
            self._after(ms, self._fix_stack_if_needed)

    def _return_to_tour(self):
        self._note = "Welcome back."
        self._no_park_until = time.monotonic() + _REVEAL_GRACE_S
        try:
            self.p._bring_front()
        except Exception as e:
            print(f"tutorial: could not return to the tour: {e!r}")
        self._restack_after_raise()
        try:
            self._paint_task(self._cw(self._task), self._slide)
        except tk.TclError:
            pass

    # ========================================================================
    # Slide artwork. Each takes the stage canvas plus its size and paints a
    # single idea: what you press on the left, an arrow, what happens on the
    # right. Nothing here is interactive  the checklist below the stage owns
    # all of the feedback.
    # ========================================================================

    def _io(self, w):
        """Standard input | arrow | output split for the stage."""
        return w * 0.24, w * 0.42, w * 0.53, w * 0.74

    def _pad_photo(self, cv, cx, cy, max_w, max_h):
        """The controller's own artwork, sized to fit a box. Falls back to the
        picker's line-art sketch if the PNG can't load."""
        photo = None
        try:
            photo = self.p._ctrl_static_photo(self._kind, int(max_w),
                                              int(max_h))
        except Exception:
            photo = None
        if photo is not None:
            self._imgs.append(photo)
            cv.create_image(cx, cy, image=photo)
            return True
        return False

    def _app_icon_photo(self, px, bg=None):
        """The app's OWN icon at `px`, optionally flattened onto `bg`.

        data/images/app_icon.ico is the same image the tray itself shows,
        which is the whole point of drawing it here: the picture on the slide
        and the thing the user is being asked to go and click are one and the
        same. Cached per (size, bg)  the welcome slide redraws it on every
        repaint of that slide, and a resize repaints."""
        key = ("app", int(px), bg)
        if key in self._icon_cache:
            return self._icon_cache[key]
        ph = None
        try:
            base = getattr(sys, "_MEIPASS",
                           os.path.dirname(os.path.abspath(__file__)))
            ph = self._file_photo(os.path.join(base, "data", "images",
                                               "app_icon.ico"), px, bg)
        except Exception as e:
            print(f"tutorial app icon failed to load: {e!r}")
        self._icon_cache[key] = ph
        return ph

    def _file_photo(self, path, px, bg=None):
        """Square PhotoImage from an image file, optionally flattened onto
        `bg`. Flattening matters for anything with transparency: Tk's own alpha
        compositing of a PhotoImage leaves a dark box on some builds (the same
        fix _Picker._vmenu_icon_photo makes for uploaded icons)."""
        from PIL import Image as PILImage, ImageTk as PILImageTk
        img = PILImage.open(path).convert("RGBA")
        img = img.resize((int(px), int(px)), PILImage.LANCZOS)
        if bg is not None:
            flat = PILImage.new("RGBA", img.size, self.p._hex_to_rgba(bg))
            flat.alpha_composite(img)
            img = flat
        return PILImageTk.PhotoImage(img, master=self.p.root)

    def _wrap(self, text, size, max_w, fg=_FG, bg=_PANEL_BOX):
        """Greedy word wrap measured with the SAME rasteriser that draws the
        text (_txt_w), so a line that fits here fits on the canvas."""
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word) if cur else word
            if cur and self._txt_w(trial, size=size, fg=fg, bg=bg) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    # The welcome slide's three columns, as (left, right) fractions of the
    # stage width. The first is the widest: the stick and the pointer it swings
    # around need the room, and it is also the first thing the user reads.
    _WELCOME_COLS = ((0.028, 0.392), (0.404, 0.652), (0.664, 0.972))

    def _art_welcome(self, cv, w, h, s):
        """How you drive the desktop with the pad in your hands  and where the
        app itself lives, in that order.

        No controller art and no app icon here on purpose. Before any of the
        chord teaching there are exactly three things a new user needs: the
        stick that moves the pointer, the paddles that click with it, and the
        tray icon that brings this window back. One column each, left to right,
        and the third is the one the checklist below actually asks for. (What
        the app IS is said once, in the slide's caption, not again here.)

        The moving parts  the stick, the pointer, the paddles pressing, the
        ring around the tray icon  are drawn by _welcome_anim into the
        animated layer; everything static is here. The coordinates they share
        go into self._stage_geom so a frame never re-measures anything."""
        heads = ("Move the pointer with the right stick",
                 "Click the mouse with the triggers",
                 "Open the manager from the tray")
        top, bot = h * 0.05, h * 0.96
        cols = [(w * f1, w * f2) for f1, f2 in self._WELCOME_COLS]
        # Wrap all three captions before drawing any of them: one column
        # spilling to a second line must push every column's artwork down
        # together, or the three pictures sit at three different heights.
        wraps = [self._wrap(t, 11, (x2 - x1) - 22, bg=_CARD)
                 for t, (x1, x2) in zip(heads, cols)]
        head_h = 20 + 17 * max(len(ln) for ln in wraps)
        geom = {}
        for i, (x1, x2) in enumerate(cols):
            kp._round_rect(cv, x1, top, x2, bot, 12, fill=_CARD,
                           outline=_CARD_EDGE, width=1)
            y = top + 20
            for ln in wraps[i]:
                self._txt(cv, (x1 + x2) / 2.0, y, ln, size=11, fg=_FG,
                          bg=_CARD, anchor="center")
                y += 17
            # What's left of the card, once the caption has had its share.
            box = (x1 + 12, top + head_h, x2 - 12, bot - 12)
            if i == 0:
                geom["js"] = self._draw_stick_column(cv, *box)
            elif i == 1:
                geom["pads"] = self._draw_grip_back(cv, *box)
            else:
                geom["tray"] = self._draw_tray_column(cv, *box)
        self._stage_geom = geom

    def _draw_stick_column(self, cv, x1, y1, x2, y2):
        """The welcome slide's thumbstick, sized to its column.

        Returns (cx, cy, r, orbit): the well's centre and radius, and the
        radius the pointer swings at."""
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        # Sized off BOTH axes, and the pointer's orbit (up to 1.95x, plus the
        # arrow itself) has to clear the card too  this column is the first
        # thing read on the first slide, so it gets as big as the box allows.
        r = max(24.0, min((x2 - x1) * 0.215, (y2 - y1) * 0.205))
        self._draw_stick_well(cv, cx, cy, r)
        return (cx, cy, r, r * 1.95)

    def _draw_stick_well(self, cv, cx, cy, r, edge=_CARD_EDGE):
        """The static half of a thumbstick: the dish it sits in and the ring
        marking how far it travels. The cap that moves in it is drawn by
        _draw_stick_cap into the animated layer  on the welcome slide from a
        clock, on the media slide from the user's own stick."""
        # Dish: a dark well with a lip, so the cap has something to sit IN.
        cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                       fill="#12161c", outline=edge, width=2)
        rr = r * 0.86
        cv.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                       fill="#1a1f28", outline="")
        # Travel ring  the same faint hairline the rest of the tour's boxes
        # use, so it reads as a guide rather than as part of the hardware.
        gr = r * 0.55
        cv.create_oval(cx - gr, cy - gr, cx + gr, cy + gr, fill="",
                       outline="#262d38", width=1)

    def _draw_stick_side_base(self, cv, cx, cy, s, label, edge=_OUTLINE):
        """The housing of a thumbstick seen FROM THE SIDE, with its printed
        name in it  the shape Steam's own L3 glyph uses (shared_l3.png), which
        is what this slide showed before and what a "push it in" instruction
        wants: a stick in profile, not a dial from above.

        Static on purpose. The cap that sinks and the arrow that comes down on
        it are the live half (_draw_stick_side_cap); the box they sit on never
        moves, exactly as the housing of a real stick doesn't."""
        top, bot = cy - 1.0, cy + s * 0.88
        self._rot_round_rect(cv, cx, (top + bot) / 2.0, s * 1.48, bot - top,
                             5.0, 0.0, taper=0.80, fill="#1b222c",
                             outline=edge, width=2)
        self._txt(cv, cx, (top + bot) / 2.0 + 1, label, size=9, fg=_FG,
                  bg="#1b222c", anchor="center")

    def _draw_stick_side_cap(self, cv, cx, cy, s, press=0.0, ring=_ACCENT,
                             bob=0.0):
        """The live half of the side-on stick: the cap, and the arrow pressing
        down on it.

        `press` (0..1) sinks both into the housing and lights them up  the
        cap is a flat lens from this angle, so travel and colour are all the
        depth the drawing has. `bob` nudges the arrow down without the cap,
        which is how the icon says "go on, push it" while nothing is
        happening."""
        drop = 5.0 * press
        cap_y = cy - s * 0.16 + drop
        # Wider than the housing and a real slab of a lens  in the glyph the
        # cap is the element you notice, and it is the part being pushed.
        rx, ry = s * 1.06, s * 0.30
        cv.create_oval(cx - rx, cap_y - ry, cx + rx, cap_y + ry, width=2,
                       fill=kp._lerp_color(_FIELD, _BG, 0.25 * press),
                       outline=kp._lerp_color(ring, "#ffffff", 0.55 * press),
                       tags=_ANIM_TAG)
        # Arrow: a solid wedge coming down onto the cap, the same one the glyph
        # has. It rides the press too, so at full travel it has met the cap.
        aw, ah = s * 0.36, s * 0.42
        tip = cap_y - ry - s * 0.22 + drop + bob
        cv.create_polygon(cx - aw, tip - ah, cx + aw, tip - ah, cx, tip,
                          fill=kp._lerp_color(ring, "#ffffff", 0.55 * press),
                          outline="", tags=_ANIM_TAG)

    def _draw_stick_cap(self, cv, cx, cy, r, dx, dy, press=0.0, ring=_ACCENT,
                        travel=0.48):
        """The moving half: the cap leaning out of its well by (dx, dy)  each
        -1..1  and sinking as `press` goes 0→1.

        The one drawing of a thumbstick in the tour, so the welcome slide's
        demo stick and the media slide's live one are visibly the same object.
        Always tagged for the animated layer; nothing here is text.

        `travel` is how far out of the well full deflection takes the cap. The
        default suits a well with no hard rim; a dish drawn with a bright lip
        wants less, or the cap visibly climbs out over its own edge."""
        tx, ty = cx + dx * r * travel, cy + dy * r * travel
        # Shaft: one fat round-capped line from the well to the cap, which at
        # this size looks exactly like a stick leaning over.
        cv.create_line(cx, cy, tx, ty, width=r * 0.8, fill="#2b3341",
                       capstyle="round", tags=_ANIM_TAG)
        # A pressed cap is smaller and paler  the same "it went away from you"
        # cue a key cap gives, which is all the depth a flat drawing can carry.
        cr = r * (0.52 - 0.06 * press)
        cv.create_oval(tx - cr, ty - cr, tx + cr, ty + cr, width=2,
                       fill=kp._lerp_color(_FIELD, _BG, 0.30 * press),
                       outline=kp._lerp_color(ring, "#ffffff", 0.45 * press),
                       tags=_ANIM_TAG)
        dr = cr * 0.62
        cv.create_oval(tx - dr, ty - dr, tx + dr, ty + dr,
                       fill=kp._lerp_color(_FIELD, _BG, 0.45 + 0.25 * press),
                       outline="", tags=_ANIM_TAG)
        # A short arc of light on the cap's upper-left, which is the one thing
        # that stops a flat circle reading as a hole.
        hr = cr * 0.74
        cv.create_arc(tx - hr, ty - hr, tx + hr, ty + hr, start=55, extent=95,
                      style="arc", width=2, tags=_ANIM_TAG,
                      outline=kp._lerp_color(_FIELD, "#ffffff", 0.32))
        return tx, ty

    # A real gamepad body, as fractions of (half width, half height).
    #
    # Traced rather than invented: hand-drawn attempts at this silhouette all
    # came out as an arch, a mask or a rectangle with legs. This is the body
    # outline of the Xbox One pad from the "8th Gen Console Vector Gamepad
    # Collection" by TheHoodieGuy02 on OpenGameArt, released CC0 (so: no
    # attribution required, no licence conflict with this project's GPL  the
    # credit here is manners, and a pointer to where to re-cut it from):
    #   https://opengameart.org/content/8th-gen-console-vector-gamepad-collection
    # Its single body path, flattened from Béziers to a polyline and simplified
    # to ~0.3% of the width, which is sub-pixel at the size we draw it. The
    # FRONT and BACK silhouettes of a pad are the same shape, so this is the
    # outline either way; what makes it a back is the battery door and the
    # paddles drawn on top of it.
    #
    # Points, so create_polygon takes it with smooth=False: the curve is
    # already in the data, and Tk's smoothing would round every corner off it.
    _PAD_BACK = (
        (0.195, -0.747), (0.382, -0.977), (0.414, -1.000), (0.730, -0.905),
        (0.772, -0.852), (0.794, -0.794), (0.917, -0.220), (0.959, 0.026),
        (0.991, 0.280), (1.000, 0.427), (0.990, 0.617), (0.966, 0.742),
        (0.930, 0.843), (0.880, 0.922), (0.800, 0.990), (0.763, 0.996),
        (0.709, 0.967), (0.644, 0.897), (0.580, 0.800), (0.403, 0.444),
        (0.378, 0.421), (-0.362, 0.418), (-0.401, 0.440), (-0.523, 0.696),
        (-0.614, 0.854), (-0.672, 0.931), (-0.732, 0.983), (-0.782, 1.000),
        (-0.850, 0.954), (-0.918, 0.865), (-0.958, 0.769), (-0.985, 0.650),
        (-0.999, 0.508), (-0.999, 0.385), (-0.980, 0.177), (-0.898, -0.317),
        (-0.794, -0.794), (-0.772, -0.852), (-0.730, -0.905), (-0.415, -1.000),
        (-0.384, -0.980), (-0.181, -0.738), (0.022, -0.719), (0.141, -0.728))
    # Its true width/height. Drawn to any other ratio it stops being a pad and
    # starts being a logo.
    _PAD_ASPECT = 1.432
    # Where the triggers sit, taken from the SAME drawing (its trigger paths,
    # measured in these body coordinates): x ±0.61, straddling the body's own
    # top edge, which runs through about y -0.94 at that x. Half the blade
    # stands proud of the shell, which is what you actually see of a trigger
    # from behind  and the whole point of this position is that L2/R2 are up
    # on the shoulders. A blade down on the grip is a rear paddle instead,
    # which is a different button entirely.
    #
    # The bumpers are deliberately NOT drawn: from directly behind they are
    # hidden by the triggers, and the wedges the front view shows read as
    # wings sticking out of the shoulders.
    _TRIGGER_X, _TRIGGER_Y = 0.61, -0.94
    # Blade angle, as the LEFT one's rotation; the right takes its negative.
    # Negative leans the left blade's TOP outward, which is the line the
    # shoulder runs on (see _PAD_BACK around x=-0.6).
    _TRIGGER_TILT = -14.0

    def _draw_grip_back(self, cv, x1, y1, x2, y2):
        """The static half of the trigger picture: a controller from behind,
        with its bumpers, for the two trigger blades to sit on. The blades
        themselves press on their own (see _welcome_anim).

        The triggers go at the TOP, on the shoulders, because that is where
        L2/R2 are  a blade down on the grip is a rear paddle, which is a
        different button. Their position comes from the same drawing the
        outline does (see _BUMPER / _TRIGGER_X), so the hardware lands where
        the artist put it rather than where it fitted.

        The labels sit ABOVE, one over each trigger, because that is where
        there is room once the hardware moved up the pad; under the grips they
        would be captioning the part of the controller nothing happens on.

        Returns (lx, ly, rx, ry, pw, ph, tilt) for the animated layer."""
        cx = (x1 + x2) / 2.0
        # Width-led, then clamped by height, so the pad keeps _PAD_ASPECT
        # whichever way the card is the tighter fit.
        sw = min((x2 - x1) * 0.98, 224.0)
        sh = sw / self._PAD_ASPECT
        if sh > (y2 - y1) * 0.52:
            sh = (y2 - y1) * 0.52
            sw = sh * self._PAD_ASPECT
        hw, hh = sw / 2.0, sh / 2.0
        pw, ph = sw * 0.085, sh * 0.30
        # The whole group  a caption line, then the pad with its triggers
        # standing proud of the top edge  centred in the card as one block,
        # with the labels sitting just above whatever reaches highest.
        top_out = max(ph / 2.0 - self._TRIGGER_Y * hh, hh)
        group_h = 16.0 + 10.0 + top_out + hh
        gtop = y1 + max(0.0, ((y2 - y1) - group_h) / 2.0)
        scy = gtop + 26.0 + top_out
        tx, ty = hw * self._TRIGGER_X, scy + hh * self._TRIGGER_Y
        for side in (-1, 1):
            # L2/ZL is right-click and R2/ZR is left-click on every kind (see
            # SC_DESKTOP_DEFAULTS / SDL_DESKTOP_DEFAULTS), so the labels are
            # crossed on purpose: your index finger's trigger is the primary
            # click, the way Steam Input ships it.
            self._txt(cv, cx + side * tx, gtop + 8,
                      "Right click" if side < 0 else "Left click", size=10,
                      fg=_FG, bg=_CARD, anchor="center")
        pts = []
        for fx, fy in self._PAD_BACK:
            pts.append(cx + fx * hw)
            pts.append(scy + fy * hh)
        cv.create_polygon(pts, smooth=False, fill="#1b222c",
                          outline="#39414d", width=2)
        # The battery door: one rounded panel in the middle of the shell,
        # LIGHTER than it. That is the whole difference between this being the
        # back of a pad and the front of one  but darker than the shell it
        # reads as a hole punched through the middle.
        kp._round_rect(cv, cx - hw * 0.32, scy - hh * 0.44,
                       cx + hw * 0.32, scy + hh * 0.10, 9,
                       fill="#232b37", outline="")
        return (cx - tx, ty, cx + tx, ty, pw, ph, self._TRIGGER_TILT)

    def _draw_tray_column(self, cv, x1, y1, x2, y2):
        """The manager, the arrow, and the tray it comes back from  read
        bottom to top, which is the direction the window arrives from.

        Returns (x, y, r) of OUR icon in the strip, so the animated layer can
        pulse a ring on the exact thing the checklist is asking for."""
        cx = (x1 + x2) / 2.0
        sw = (x2 - x1) - 6
        # Tray strip pinned to the bottom of the card; the manager mock and the
        # arrow between them are centred in whatever is left above it, so the
        # pair doesn't drift apart on a tall panel.
        strip_h = max(30.0, sw * 0.16)
        scy = y2 - strip_h / 2.0
        ix, iy, ir = self._draw_tray_strip(cv, cx, scy, sw)
        avail = (scy - strip_h / 2.0) - y1
        mw = min(sw * 0.90, 190.0)
        mh = min(mw * 0.64, avail - 46)
        if mh > 40:
            gap = 40.0
            mtop = y1 + max(0.0, (avail - mh - gap) / 2.0)
            self._draw_window_mock(cv, cx - mw / 2.0, mtop,
                                   cx + mw / 2.0, mtop + mh)
            self._arrow(cv, ix, scy - strip_h / 2.0 - 8, ix, mtop + mh + 10,
                        color=_ACCENT, width=3)
        return (ix, iy, ir)

    def _draw_window_mock(self, cv, x1, y1, x2, y2):
        """A little stand-in for THIS window  the thing the tray icon brings
        back. Deliberately abstract (a sidebar and a few rows): a faithful
        miniature at this size is a smudge, and the shape is what's
        recognisable."""
        kp._round_rect(cv, x1, y1, x2, y2, 7, fill="#0f1319",
                       outline=_CARD_EDGE, width=1)
        bar = min(15.0, (y2 - y1) * 0.2)
        cv.create_line(x1 + 1, y1 + bar, x2 - 1, y1 + bar, fill=_CARD_EDGE)
        for i in range(3):
            dx, d = x1 + 11 + i * 10, 2.2
            cv.create_oval(dx - d, y1 + bar / 2.0 - d, dx + d,
                           y1 + bar / 2.0 + d, fill="#39414d", outline="")
        sx = x1 + (x2 - x1) * 0.30
        cv.create_rectangle(x1 + 1, y1 + bar + 1, sx, y2 - 1, fill="#141a22",
                            outline="")
        ry = y1 + bar + 9
        i = 0
        while ry < y2 - 9:
            cv.create_rectangle(x1 + 8, ry, sx - 7, ry + 4,
                                fill=_ACCENT if i == 1 else "#2a323d",
                                outline="")
            cv.create_rectangle(sx + 8, ry, x2 - 10, ry + 4, fill="#232a34",
                                outline="")
            ry += 12
            i += 1

    def _draw_tray_strip(self, cv, cx, cy, sw):
        """The notification area as the user will see it: a few neighbouring
        icons, ours among them (the real icon, not a stand-in), a clock, and a
        mouse pointer sitting on ours.

        Returns (x, y, r) of our icon. The ring around it is NOT drawn here 
        it pulses in the animated layer, which is what makes this column the
        one the eye lands on."""
        bh = max(30.0, sw * 0.16)
        x1, x2 = cx - sw / 2.0, cx + sw / 2.0
        y1, y2 = cy - bh / 2.0, cy + bh / 2.0
        kp._round_rect(cv, x1, y1, x2, y2, 8, fill="#171b22",
                       outline=_CARD_EDGE, width=1)
        px = int(min(bh * 0.56, sw * 0.11))
        step = (sw - 20) / 5.0
        x = x1 + 10 + step / 2.0
        ours_x = None
        for i in range(4):
            if i == 3:                     # ours, last before the clock
                ours_x = x
                img = self._app_icon_photo(px, bg="#171b22")
                if img is not None:
                    self._imgs.append(img)
                    cv.create_image(x, cy, image=img)
            else:
                kp._round_rect(cv, x - px / 2.0, cy - px / 2.0, x + px / 2.0,
                               cy + px / 2.0, 4, fill="#39414d", outline="")
            x += step
        self._txt(cv, x, cy, "9:41", size=9, fg=_MUTED, bg="#171b22",
                  anchor="center")
        if ours_x is None:
            return (cx, cy, px * 0.78)
        self._draw_cursor(cv, ours_x + px * 0.30, cy + px * 0.20)
        return (ours_x, cy, px * 0.78)

    def _draw_cursor(self, cv, x, y, scale=1.0, tags=()):
        """A mouse pointer with its TIP at (x, y)  the same arrow the gyro
        slide steers, so the one the stick drags around on this slide is
        recognisably the same object."""
        pts = ((0, 0), (0, 22), (6, 16), (11, 25), (15, 22), (10, 14),
               (17, 13))
        flat = []
        for px, py in pts:
            flat.append(x + px * scale)
            flat.append(y + py * scale)
        cv.create_polygon(flat, fill="#ffffff", outline="#1b1f27", tags=tags)

    # -- the welcome slide's moving parts ------------------------------------
    # Everything below runs at _STAGE_MS for as long as the slide is up, so it
    # is all cheap canvas vector work: no PIL text (one raster per call), no
    # PhotoImages, nothing measured. See _paint_stage_anim.

    _STICK_SWEEP = 1.15      # rad/s the stick swings round the well
    _STICK_BREATHE = 0.83    # rad/s the deflection eases in and out at
    _PADDLE_CYCLE = 2.8      # seconds for one left-then-right press pair
    _PADDLE_HOLD = 0.22      # fraction of that cycle one press occupies

    def _welcome_anim(self, cv, g, t):
        """One frame of the welcome slide: the stick sweeping the pointer round
        with it, the paddles clicking under it, and the tray icon's ring.

        Everything is a pure function of `t` (seconds since the slide came up),
        so a dropped frame changes nothing and the three columns stay in step
        with each other."""
        a = t * self._STICK_SWEEP
        # Deflection eases in and out rather than sitting at full tilt: a stick
        # pinned to the rim reads as broken, a breathing one as being steered.
        mag = 0.62 + 0.34 * math.sin(t * self._STICK_BREATHE)
        if "js" in g:
            self._anim_stick(cv, g["js"], a, mag, t)
        if "pads" in g:
            self._anim_paddles(cv, g["pads"], t)
        if "tray" in g:
            self._anim_tray_ring(cv, g["tray"], t)

    def _anim_stick(self, cv, js, a, mag, t):
        """The stick cap tilted out of its dish, and the pointer it is dragging
        around the outside  same angle, and further out the further the stick
        leans, so the causal link is the geometry rather than a caption."""
        cx, cy, r, orbit = js
        dx, dy = math.cos(a) * mag, math.sin(a) * mag
        self._draw_stick_cap(cv, cx, cy, r, dx, dy)
        # The pointer, trailing a comet of where it has just been. The trail is
        # sampled BACKWARDS along the same path function, so it is the real
        # history of the motion and needs no state kept between frames. Long
        # enough (about half a second) to draw the ARC the pointer is on 
        # that arc is what says the stick is dragging it, in a still frame as
        # well as in motion.
        for k in range(11, 0, -1):
            ta = t - k * 0.045
            ang = ta * self._STICK_SWEEP
            m = 0.62 + 0.34 * math.sin(ta * self._STICK_BREATHE)
            rad = orbit * (0.80 + 0.20 * m)
            px, py = cx + math.cos(ang) * rad, cy + math.sin(ang) * rad
            dot = 3.6 - k * 0.22
            cv.create_oval(px - dot, py - dot, px + dot, py + dot,
                           fill=kp._lerp_color(_ACCENT, _CARD, k / 12.0),
                           outline="", tags=_ANIM_TAG)
        # The pointer sits in the direction the stick is leaning and further
        # out the harder it leans  but never nearer than 0.8 of the orbit, or
        # a gentle lean parks the arrow on top of the stick it belongs to.
        pr = orbit * (0.80 + 0.20 * mag)
        self._draw_cursor(cv, cx + math.cos(a) * pr, cy + math.sin(a) * pr,
                          scale=0.85, tags=_ANIM_TAG)

    def _anim_paddles(self, cv, pads_g, t):
        """The two blades pressing in turn, left then right, each throwing off
        a halo as it lands  the same "that did something" burst the checklist
        ticks use, in the same accent."""
        lx, ly, rx, ry, pw, ph, tilt = pads_g
        u = (t % self._PADDLE_CYCLE) / self._PADDLE_CYCLE
        # Mirrored angles: `tilt` is the LEFT blade's, the right takes its
        # negative, so both lean along their own shoulder (see _TRIGGER_TILT).
        # Rotated the same way instead, the pair reads as a shape that has
        # been sheared rather than as two sides of one controller.
        for cx, cy, deg, start in ((lx, ly, tilt, 0.06), (rx, ry, -tilt, 0.53)):
            # Progress through THIS blade's press window, or None outside it.
            p = (u - start) / self._PADDLE_HOLD
            p = p if 0.0 <= p <= 1.0 else None
            self._draw_paddle(cv, cx, cy, pw, ph, deg,
                              0.0 if p is None else math.sin(math.pi * p), p)

    def _draw_paddle(self, cv, cx, cy, pw, ph, deg, press, ripple):
        """One shoulder trigger: a blade in the SAME grey key-cap face and
        accent ring as the stick cap next door, so the two columns read as one
        set of hardware rather than two drawings.

        `press` (0..1) is how far it is pulled right now; `ripple` (0..1 or
        None) is how far through the pull the contact halo has flown."""
        if ripple is not None:
            # A halo in the blade's OWN shape rather than a circle round it: a
            # ring that size reads as a bubble, an outline flying off the edges
            # reads as the thing itself being pressed.
            grow = 5.0 + 13.0 * _ease_out(ripple)
            self._rot_round_rect(cv, cx, cy, pw + grow, ph + grow,
                                 (pw + grow) * 0.45, deg, taper=0.86, fill="",
                                 outline=kp._lerp_color(_ACCENT, _CARD, ripple),
                                 width=2, tags=_ANIM_TAG)
        # A pulled trigger drops along its OWN axis, into the shoulder it is
        # hinged on  down the blade, not down the page.
        a = math.radians(deg)
        px = cx - math.sin(a) * press * 3.5
        py = cy + math.cos(a) * press * 3.5
        self._rot_round_rect(cv, px, py, pw, ph, pw * 0.45, deg, taper=0.86,
                             fill=kp._lerp_color(_FIELD, _BG, 0.12),
                             outline=kp._lerp_color("#3d4652", _ACCENT,
                                                    0.35 + 0.65 * press),
                             width=2, tags=_ANIM_TAG)
        # The blade's own dish, matching the stick cap's.
        self._rot_round_rect(cv, px, py - ph * 0.04, pw * 0.44, ph * 0.70,
                             pw * 0.22, deg, taper=0.86,
                             fill=kp._lerp_color(_FIELD, _BG,
                                                 0.45 - 0.35 * press),
                             outline="", tags=_ANIM_TAG)

    def _rot_round_rect(self, cv, cx, cy, w, h, r, deg, taper=1.0, **kw):
        """A rounded rectangle rotated about its own centre, optionally
        narrowing toward the bottom (`taper` scales the lower edge).

        kp._round_rect is axis-aligned (it hands create_polygon a fixed point
        list), and a rear paddle standing square on the page reads as a
        battery. These are the same twelve control points, tapered into a blade
        and rotated  so the paddles keep exactly the corner rounding every
        other box in the tour has."""
        a = math.radians(deg)
        ca, sa = math.cos(a), math.sin(a)
        tw, bw, hh = w / 2.0, w * taper / 2.0, h / 2.0
        pts = ((-tw + r, -hh), (tw - r, -hh), (tw, -hh), (tw, -hh + r),
               (bw, hh - r), (bw, hh), (bw - r, hh), (-bw + r, hh),
               (-bw, hh), (-bw, hh - r), (-tw, -hh + r), (-tw, -hh))
        flat = []
        for px, py in pts:
            flat.append(cx + px * ca - py * sa)
            flat.append(cy + px * sa + py * ca)
        return cv.create_polygon(flat, smooth=True, **kw)

    def _anim_tray_ring(self, cv, tray, t):
        """A breathing ring around OUR icon in the strip  the same invitation
        the pending checklist row below is making, on the thing it is asking
        the user to click. Drawn here rather than in the static art so the two
        pulse together instead of one sitting still next to the other."""
        ix, iy, ir = tray
        puls = 0.5 + 0.5 * math.sin(t * (2 * math.pi / _BREATHE_S))
        rr = ir + 2.0 + 3.5 * puls
        kp._round_rect(cv, ix - rr, iy - rr, ix + rr, iy + rr, 6, fill="",
                       outline=kp._lerp_color("#3d4652", _ACCENT,
                                              0.25 + 0.65 * puls),
                       width=2, tags=_ANIM_TAG)

    def _art_osk(self, cv, w, h, s):
        """Left: the three buttons the keyboard needs, one per row, all three
        descriptions starting at the SAME x so they read as a table rather
        than as three loose captions. Right: the keyboard, and under it the
        box the typed letters actually land in.

        Each row carries the id of the step it teaches, so a row whose step has
        landed turns green and grows a tick  the same quiet "that one's done"
        the media slide's labels give (this slide is "live_art" for it). The
        last row has no step of its own: closing the keyboard is what carries
        the user onward rather than something to tick off."""
        done = self._visible_done(s)
        rows = [([s.get("open_cid") or "x"], "Open the keyboard", "osk"),
                (["a", "dpad"], "Move and type a letter", "type"),
                (["b"], "Close the keyboard", None)]
        # One column for the chips and one for the text, both measured off the
        # WIDEST chip run, so a pad whose glyphs are wider can't push its own
        # row's text out of line with the others.
        chip = 40
        gap = 16
        widest = max(len(cids) * chip + (len(cids) - 1) * gap
                     for cids, _t, _c in rows)
        cx0 = w * 0.06
        tx = cx0 + widest + 22
        for i, (cids, text, cid_done) in enumerate(rows):
            ry = h * (0.22 + i * 0.26)
            x = cx0 + chip / 2.0
            for j, cid in enumerate(cids):
                if j:
                    self._txt(cv, x - gap / 2.0 - chip / 2.0, ry, "+", size=13,
                              fg=_MUTED, bg=_PANEL_BOX, anchor="center")
                if cid == "dpad":
                    self._dpad_chip(cv, x, ry, size=chip)
                else:
                    self._chip(cv, x, ry, cid, size=chip)
                x += chip + gap
            ok = (cid_done in done) if cid_done else self._osk_shut
            # Colour only. The tick disc lives in the checklist at the bottom
            # of the panel and nowhere else: one row of the art turning green
            # says "done" perfectly well, and a second tick beside every label
            # made the slide look like it was marking its own homework twice.
            self._txt(cv, tx, ry, text, size=12,
                      fg=_GREEN if ok else _FG, bg=_PANEL_BOX, anchor="w")
        ox = w * 0.76
        self._draw_keyboard(cv, ox, h * 0.34, w * 0.40, h * 0.42)
        self._draw_typed(cv, ox, h * 0.76, w * 0.34, 46)
        self._txt(cv, ox, h * 0.95, "you can also type with the trackpads, "
                  "sticks and gyro", size=9, fg=_MUTED, bg=_PANEL_BOX,
                  anchor="center")

    def _draw_typed(self, cv, cx, cy, bw, bh):
        """The typed-letters box: what the demo keyboard has actually produced
        (see _poll_osk_typing), with a caret after it. Empty, it shows the word
        the checklist is asking for as a ghost  so the box reads as "type here"
        before anything is in it rather than as an unexplained empty slot."""
        x1, y1 = cx - bw / 2.0, cy - bh / 2.0
        x2, y2 = cx + bw / 2.0, cy + bh / 2.0
        text = self._typed[-18:]
        kp._round_rect(cv, x1, y1, x2, y2, 8, fill="#0f1319",
                       outline=_ACCENT if text else "#2c333d", width=2)
        if not text:
            self._txt(cv, cx, cy, "hi", size=15, fg="#39414d", bg="#0f1319",
                      anchor="center")
            return
        tw = self._txt_w(text, size=15, fg=_FG, bg="#0f1319")
        x = max(x1 + 12, cx - tw / 2.0)
        self._txt(cv, x, cy, text, size=15, fg=_FG, bg="#0f1319", anchor="w")
        cv.create_rectangle(x + tw + 3, cy - 10, x + tw + 5, cy + 10,
                            fill=_ACCENT, outline="")

    def _dpad_chip(self, cv, cx, cy, size=_CHIP, color=_FIELD, tags=()):
        """A key-cap chip holding the WHOLE D-pad rather than one direction 
        the row is "A + D-pad", and a single arrow glyph would read as "press
        left".

        Drawn rather than composited from the glyph set, because the set has no
        whole-D-pad art: shared_dpad_up/down/left/right are each the entire
        cross with a marker on one arm, so overlaying the four of them produces
        a blob, not a D-pad. This is the same cross those glyphs draw, in the
        same flat white, minus the marker."""
        h = size // 2
        kp._round_rect(cv, cx - h, cy - h, cx + h, cy + h, 12,
                       fill=color, outline="", tags=tags)
        arm = size * 0.32
        thick = size * 0.115
        r = thick * 0.55
        kp._round_rect(cv, cx - thick, cy - arm, cx + thick, cy + arm, r,
                       fill=_FG, outline="", tags=tags)
        kp._round_rect(cv, cx - arm, cy - thick, cx + arm, cy + thick, r,
                       fill=_FG, outline="", tags=tags)
        # Lights on ANY direction: the chip is the whole cross, so any arm
        # going down is this picture's button being pressed. ("dpad" marks it
        # for _paint_chips_held as vector art to redraw rather than a glyph.)
        if not tags:
            bits = 0
            for d in ("dpad_up", "dpad_down", "dpad_left", "dpad_right"):
                bits |= _bit_for(self._kind, d) or 0
            self._register_chip(cv, bits, cx, cy, size, color, "dpad")

    def _draw_keyboard(self, cv, cx, cy, kw, kh):
        x1, y1 = cx - kw / 2.0, cy - kh / 2.0
        x2, y2 = cx + kw / 2.0, cy + kh / 2.0
        self._panelbox(cv, x1, y1, x2, y2, r=10, fill="#171b22")
        rows, pad = 4, 8
        iw = (x2 - x1) - pad * 2
        ih = (y2 - y1) - pad * 2
        rh = ih / rows
        for r in range(rows):
            ry = y1 + pad + r * rh
            if r == rows - 1:
                kp._round_rect(cv, x1 + pad + iw * 0.22, ry + 3,
                               x1 + pad + iw * 0.78, ry + rh - 5, 4,
                               fill="#39414d", outline="")
                continue
            n = 10
            kw1 = iw / n
            for c in range(n):
                kx = x1 + pad + c * kw1
                lit = (r, c) in ((1, 3), (2, 6))
                kp._round_rect(cv, kx + 2, ry + 3, kx + kw1 - 2, ry + rh - 5, 4,
                               fill=_ACCENT if lit else "#39414d", outline="")

    def _art_alttab(self, cv, w, h, s):
        """The chord spelled out as what the hands do  HOLD one button, TAP
        the other  and, on the right, the window that tap is aiming at: a
        small copy of the demo window the slide has just put on screen, so the
        thing to Alt-Tab to is recognisable before the user goes looking."""
        gcid, cid = s.get("chord", (None, None))
        ix = w * 0.22
        self._hold_tap_chips(cv, ix, h * 0.40, gcid, cid)
        self._txt(cv, ix, h * 0.64, "keep holding to cycle further", size=10,
                  fg=_MUTED, bg=_PANEL_BOX, anchor="center")
        self._arrow(cv, w * 0.42, h * 0.40, w * 0.50, h * 0.40)
        # The target, drawn the way it actually looks (rings + our icon), with
        # a plain window cascaded behind it saying "this one comes to the
        # front". Both boxes are laid out to finish inside the card  an
        # earlier pass had the target running off the right edge.
        ww, wh = w * 0.24, h * 0.42
        cy = h * 0.42
        self._win_sketch(cv, w * 0.54, cy - wh * 0.55, ww, wh,
                         "#2b323c", "#454d59", None)
        self._draw_target_sketch(cv, w * 0.68, cy - wh * 0.15, ww, wh)
        self._txt(cv, w * 0.74, h * 0.94, "switch to the Tutorial Window",
                  size=10, fg=_ACCENT, bg=_PANEL_BOX, anchor="center")

    def _hold_tap_chips(self, cv, cx, cy, hold_cid, tap_cid):
        """Two chips side by side, each captioned with what to DO with it.
        The alt-tab chord is not two simultaneous presses: the Guide button is
        held down to keep the switcher open and the other one is tapped, once
        per window (see the tray's hold-cycle handling). Chips alone said none
        of that  they read as "press both"."""
        gap = 30
        n = 2 if tap_cid else 1
        total = n * _CHIP + (n - 1) * gap
        x = cx - total / 2.0 + _CHIP / 2.0
        for cid, verb in ((hold_cid, "hold"), (tap_cid, "tap"))[:n]:
            self._chip(cv, x, cy, cid)
            self._txt(cv, x, cy + _CHIP * 0.62, verb, size=11, fg=_FG,
                      bg=_PANEL_BOX, anchor="center")
            if cid is hold_cid and n > 1:
                self._txt(cv, x + _CHIP / 2.0 + gap / 2.0, cy, "+", size=15,
                          fg=_MUTED, bg=_PANEL_BOX, anchor="center")
            x += _CHIP + gap

    def _draw_target_sketch(self, cv, x0, y0, ww, wh):
        """A miniature of the demo window (see _paint_alt_window)  same rings,
        same icon, same accent  so the slide and the window it is talking
        about are visibly the same object."""
        self._win_sketch(cv, x0, y0, ww, wh, "#12171e", _ACCENT, _ACCENT,
                         lines=False)
        cx, cy = x0 + ww / 2.0, y0 + 18 + (wh - 18) / 2.0
        # Rings sized off the SHORTER side of the body, so a squeezed panel
        # shrinks them instead of drawing them through the window's edges.
        unit = min(ww, wh - 18)
        for r in (unit * 0.40, unit * 0.28, unit * 0.16):
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=_ACCENT,
                           width=2, fill="")
        icon = self._app_icon_photo(int(unit * 0.22), bg="#12171e")
        if icon is not None:
            self._imgs.append(icon)
            cv.create_image(cx, cy, image=icon)

    def _win_sketch(self, cv, x0, y0, ww, wh, body, bar, ring, lines=True):
        """A stand-in application window: title bar, body, optional ring.
        `lines=False` leaves the body empty for a caller that draws its own
        contents into it (the demo-window miniature)."""
        self._panelbox(cv, x0, y0, x0 + ww, y0 + wh, r=8, fill=body)
        kp._round_rect(cv, x0, y0, x0 + ww, y0 + 18, 8, fill=bar, outline="")
        cv.create_rectangle(x0, y0 + 10, x0 + ww, y0 + 18, fill=bar,
                            outline="")
        if ring:
            kp._round_rect(cv, x0, y0, x0 + ww, y0 + wh, 8, fill="",
                           outline=ring, width=2)
        if not lines:
            return
        for i in range(3):
            y = y0 + 34 + i * 14
            cv.create_rectangle(x0 + 12, y, x0 + ww * (0.75 - i * 0.16),
                                y + 4, fill="#39414d", outline="")

    # The media slide's three stick directions, as
    # (dx, dy, check id, label, zone)  the zone being what _stick_zone calls
    # that direction, so the drawing and the detection agree by construction.
    _MEDIA_DIRS = ((0, -1, "vup", "Volume Up", "UP"),
                   (0, 1, "vdn", "Volume Down", "DOWN"),
                   (1, 0, "next", "Next Song", "RIGHT"))

    # The media slide's two columns, as (left, right) fractions of the stage
    # width  the same card treatment as the welcome slide. The dial column is
    # much the wider: it carries three labels around a circle.
    _MEDIA_COLS = ((0.028, 0.606), (0.618, 0.972))

    def _art_media(self, cv, w, h, s):
        """Two cards, because these are two different gestures: PUSH the stick
        (volume and tracks) on the left, CLICK IT IN (play/pause) on the right.
        They used to share one open stage, which read as one four-way control
        with a stray label off to the side.

        Each card names its own chord with the Guide chip, and each shows the
        stick the way its own gesture wants it seen: top-down for direction,
        in profile for a press (see _draw_stick_side_base). Both are the REAL
        stick, following the user's own thumb  see _media_anim.

        Everything that changes as steps land is here rather than in the
        animated layer, because all of it is text: a landed direction turns its
        label green and grows a tick, its arrow lights up with it, and a card
        whose whole job is done ticks its own caption. The stage is repainted
        on every commit for exactly this ("live_art")."""
        gcid, pp_cid = s.get("chord", (None, None))
        pp_cid = pp_cid or "l3"
        done = self._visible_done(s)
        dirs_ok = all(c[2] in done for c in self._MEDIA_DIRS)
        pp_ok = "pp" in done
        heads = ("Push the stick for volume and tracks",
                 "Click the stick in to play or pause")
        top, bot = h * 0.05, h * 0.96
        cols = [(w * f1, w * f2) for f1, f2 in self._MEDIA_COLS]
        # Both captions wrapped before either is drawn, so one spilling onto a
        # second line pushes BOTH cards' artwork down together (same rule as
        # the welcome slide's three columns).
        wraps = [self._wrap(t, 11, (x2 - x1) - 26, bg=_CARD)
                 for t, (x1, x2) in zip(heads, cols)]
        head_h = 20 + 17 * max(len(ln) for ln in wraps)
        for i, (x1, x2) in enumerate(cols):
            kp._round_rect(cv, x1, top, x2, bot, 12, fill=_CARD,
                           outline=_CARD_EDGE, width=1)
            self._card_head(cv, x1, x2, top + 20, wraps[i],
                            (dirs_ok, pp_ok)[i])
        box_top, box_bot = top + head_h, bot - 12
        geom = {"pp_bit": self._pp_bit(s)}
        geom["dial"] = self._draw_media_dial(cv, cols[0][0] + 12, box_top,
                                             cols[0][1] - 12, box_bot,
                                             gcid, done)
        geom["click"] = self._draw_media_click(cv, cols[1][0] + 10, box_top,
                                               cols[1][1] - 10, box_bot,
                                               gcid, pp_cid)
        self._stage_geom = geom

    def _card_head(self, cv, x1, x2, y, lines, ok):
        """A column's caption, ticked once that column's steps are all in.
Colour only  see _art_osk for why the tick disc stays in the
        checklist."""
        cx, col = (x1 + x2) / 2.0, (_GREEN if ok else _FG)
        for ln in lines:
            self._txt(cv, cx, y, ln, size=11, fg=col, bg=_CARD,
                      anchor="center")
            y += 17

    def _draw_media_dial(self, cv, x1, y1, x2, y2, gcid, done):
        """The Guide chip, a plus, and the stick seen from above with its three
        directions labelled around it. Returns (cx, cy, r) for the live cap."""
        cy = (y1 + y2) / 2.0
        r = min((y2 - y1) * 0.21, (x2 - x1) * 0.15, 66.0)
        # Centre the WHOLE group, chip and labels included, rather than the
        # dial: the label sticks out ~90px to the right and the chip ~80px to
        # the left, so centring the circle alone leaves the card looking as if
        # everything slid left.
        tw = self._txt_w(self._MEDIA_DIRS[2][3], size=11, fg=_FG, bg=_CARD)
        cx = (x1 + x2) / 2.0 - ((r + 46 + tw) - (r + 108)) / 2.0
        self._chip(cv, cx - r - 80, cy, gcid)
        self._txt(cv, cx - r - 42, cy, "+", size=16, fg=_MUTED, bg=_CARD,
                  anchor="center")
        self._draw_stick_well(cv, cx, cy, r, edge=_OUTLINE)
        for dx, dy, cid, label, _zone in self._MEDIA_DIRS:
            ok = cid in done
            self._arrow(cv, cx + dx * (r + 8), cy + dy * (r + 8),
                        cx + dx * (r + 34), cy + dy * (r + 34),
                        color=_GREEN if ok else _ACCENT)
            lx, ly = cx + dx * (r + 46), cy + dy * (r + 46)
            self._txt(cv, lx, ly, label, size=11,
                      fg=_GREEN if ok else _FG, bg=_CARD,
                      anchor="w" if dx else "center")
        return (cx, cy, r)

    def _draw_media_click(self, cv, x1, y1, x2, y2, gcid, pp_cid):
        """The Guide chip, a plus, and the stick in profile with an arrow
        coming down on it  Steam's own L3 glyph, redrawn so the cap and the
        arrow can move (see _draw_stick_side_base / _draw_stick_side_cap).

        Under it goes what all four media controls are actually driving: with
        the demo track up, a live readout of it (see media_demo). Without one
        there is nothing true to say, so that half falls back to the play/pause
        glyph  in the same place, at the same size. ONE layout either way: the
        demo track comes up a moment after the slide does, and a card arriving
        into a different arrangement would shove the icon above it up the
        screen as the user is reading it.

        Returns (cx, cy, s) for the live cap."""
        # +50 centres the chip-plus-stick ROW, not the stick: the chip hangs
        # 128px off its left and the cap only 28 off its right.
        pr = 26.0
        cx = (x1 + x2) / 2.0 + 50
        cy = y1 + 34
        self._chip(cv, cx - pr - 74, cy, gcid, size=44)
        self._txt(cv, cx - pr - 40, cy, "+", size=16, fg=_MUTED, bg=_CARD,
                  anchor="center")
        self._draw_stick_side_base(cv, cx, cy, pr,
                                   pads.label_for(self._kind, pp_cid, "L3"))
        ncy = (cy + pr + 12 + y2) / 2.0
        st = media_demo.state() if (self._media_on and media_demo) else None
        if st is None:
            self._draw_note(cv, (x1 + x2) / 2.0, ncy, 15)
        else:
            self._draw_now_playing(cv, (x1 + x2) / 2.0, ncy, x2 - x1 - 8,
                                   min(118.0, y2 - (cy + pr + 20)), st)
        return (cx, cy, pr)

    def _media_anim(self, cv, g, t):
        """One frame of the media slide: both drawn sticks following the user's
        REAL one, the direction currently firing lit up, and a burst thrown off
        each control as its step lands.

        The live half is the point of the redesign  a diagram of a stick tells
        you where to push, a stick that leans when you lean tells you the tour
        can see you doing it."""
        now = time.monotonic()
        dx, dy, pressed, guide = self._stick_state(g.get("pp_bit"))
        done = self._visible_done(self._slide)
        cx, cy, r = g["dial"]
        # Holding Guide is what puts the stick on the media layer at all, so
        # the ring comes up with it: this is the difference between a stick
        # that moves the mouse and a stick that changes the volume.
        if guide:
            gr = r + 11
            cv.create_oval(cx - gr, cy - gr, cx + gr, cy + gr, fill="",
                           outline=kp._lerp_color(_CARD, _ACCENT, 0.60),
                           width=2, tags=_ANIM_TAG)
        # Quantised through the SAME function the detection uses, so what
        # lights up is exactly what is about to fire.
        zone = _stick_zone(int(dx * 32767), int(-dy * 32767))
        for ddx, ddy, cid, _label, dzone in self._MEDIA_DIRS:
            tipx, tipy = cx + ddx * (r + 34), cy + ddy * (r + 34)
            if guide and zone == dzone:
                self._arrow(cv, cx + ddx * (r + 8), cy + ddy * (r + 8),
                            tipx, tipy, color="#ffffff", width=4,
                            tags=_ANIM_TAG)
            # Burst from the ARROW, not from its tip: a ring centred on the tip
            # lands on the label just past it and reads as a halo round the
            # words rather than as the control firing.
            self._burst(cv, cx + ddx * (r + 21), cy + ddy * (r + 21),
                        self._age(cid, now), 11, bg=_CARD)
        self._draw_stick_cap(cv, cx, cy, r, dx, dy, travel=0.36,
                             press=1.0 if pressed else 0.0)
        px, pcy, pr = g["click"]
        pp_ok = "pp" in done
        # Still to do and not being pressed: the arrow bobs down at the stick,
        # on the same breath the pending checklist row below is taking. Once
        # it's done the icon simply sits there  a finished step has nothing
        # left to ask for.
        bob = 0.0
        if not (pressed or pp_ok):
            bob = 3.0 * (0.5 + 0.5 * math.sin(t * (2 * math.pi / _BREATHE_S)))
        self._draw_stick_side_cap(cv, px, pcy, pr,
                                  press=1.0 if pressed else 0.0, bob=bob,
                                  ring=_GREEN if pp_ok else _ACCENT)
        self._burst(cv, px, pcy, self._age("pp", now), pr * 0.9, bg=_CARD)

    def _age(self, cid, now):
        """Seconds since a step landed, or None if it hasn't (or its
        celebration is still being held back)."""
        t0 = self._anim_pop.get(cid)
        return None if t0 is None else now - t0

    def _burst(self, cv, cx, cy, age, r0, color=_GREEN, bg=_PANEL_BOX):
        """A ring and six sparks flying outward  the same shape the checklist
        ticks pop with, so a step landing looks the same wherever the slide
        echoes it. Fades into `bg`, so pass the surface it is drawn on. No-op
        once it has played out (or was never armed)."""
        dur = _POP_S * 1.6
        if age is None or age < 0.0 or age >= dur:
            return
        u = age / dur
        col = kp._lerp_color(color, bg, u)
        rr = r0 + r0 * 1.4 * _ease_out(u)
        cv.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, fill="",
                       outline=col, width=2, tags=_ANIM_TAG)
        for k in range(6):
            a = k * (math.pi / 3.0) + 0.4
            ra = rr + 4.0
            rb = ra + 9.0 * (1.0 - u)
            cv.create_line(cx + math.cos(a) * ra, cy + math.sin(a) * ra,
                           cx + math.cos(a) * rb, cy + math.sin(a) * rb,
                           fill=col, width=2, capstyle="round",
                           tags=_ANIM_TAG)

    def _pp_bit(self, s):
        """The button bit the Play/Pause check watches  what the drawn sticks
        show as PRESSED. None when this pad has no such bind, in which case
        they simply never depress."""
        for c in s.get("checks") or []:
            if c["id"] == "pp":
                return c.get("bit")
        return None

    def _draw_now_playing(self, cv, cx, cy, cw, ch, st):
        """The now-playing card: cover, title, and whether it's running.

        Deliberately the same cover art Windows is putting in its own media
        flyout  the flyout is a corner popup that fades after a couple of
        seconds, so the slide carries the same picture where the user is
        already looking. That is the whole point of the demo track: "Next
        Song" is only convincing when something visibly becomes another song.
        """
        track, playing = st
        x1, y1 = cx - cw / 2.0, cy - ch / 2.0
        x2, y2 = cx + cw / 2.0, cy + ch / 2.0
        # Darker than the column card it now sits inside (_CARD), or the two
        # boxes merge into one shape and the cover art floats in mid-air.
        # One border colour whether it's running or not: the card is the same
        # object either way, and a frame that changes colour under a
        # play/pause reads as the card itself moving.
        box = "#0f1319"
        kp._round_rect(cv, x1, y1, x2, y2, 10, fill=box,
                       outline=_CARD_EDGE, width=1)
        art = int(min(ch - 18, cw * 0.40))
        ax = x1 + 9 + art / 2.0
        img = self._cover_photo(track, art, bg=box)
        if img is not None:
            self._imgs.append(img)
            cv.create_image(ax, cy, image=img)
        else:
            kp._round_rect(cv, ax - art / 2.0, cy - art / 2.0, ax + art / 2.0,
                           cy + art / 2.0, 6, fill="#39414d", outline="")
        tx = ax + art / 2.0 + 12
        title = media_demo.track_title(track) if media_demo else "Track"
        self._txt(cv, tx, cy - 14, title, size=11, fg=_FG, bg="#171b22",
                  anchor="w")
        self._txt(cv, tx, cy + 5, "SteamlessInput Tutorial", size=9,
                  fg=_MUTED, bg="#171b22", anchor="w")
        # The transport state, spelled out as well as drawn: a paused glyph and
        # a playing glyph are two upright bars and a triangle, which is not a
        # difference worth squinting at.
        gy = cy + 26
        self._draw_transport(cv, tx + 6, gy, 6, playing)
        self._txt(cv, tx + 20, gy, "Playing" if playing else "Paused", size=9,
                  fg=_GREEN if playing else _ORANGE, bg="#171b22", anchor="w")

    def _draw_transport(self, cv, cx, cy, r, playing):
        col = _GREEN if playing else _ORANGE
        if playing:
            cv.create_polygon(cx - r * 0.7, cy - r, cx - r * 0.7, cy + r,
                              cx + r * 0.8, cy, fill=col, outline="")
            return
        cv.create_rectangle(cx - r * 0.8, cy - r, cx - r * 0.2, cy + r,
                            fill=col, outline="")
        cv.create_rectangle(cx + r * 0.2, cy - r, cx + r * 0.8, cy + r,
                            fill=col, outline="")

    def _cover_photo(self, track, px, bg=None):
        """The demo track's album art as a PhotoImage, cached per (track, size,
        bg)  the stage repaints on every state change and the PNG is 512px."""
        key = ("cover", int(track), int(px), bg)
        if key in self._icon_cache:
            return self._icon_cache[key]
        ph = None
        try:
            path = media_demo.cover_path(track) if media_demo else None
            if path:
                ph = self._file_photo(path, px, bg)
        except Exception as e:
            print(f"tutorial cover art failed: {e!r}")
        self._icon_cache[key] = ph
        return ph

    def _draw_note(self, cv, cx, cy, r):
        """A small play/pause pair  the payoff icon for the media slide."""
        cv.create_polygon(cx - r * 1.6, cy - r * 0.8, cx - r * 1.6, cy + r * 0.8,
                          cx - r * 0.4, cy, fill=_MUTED, outline="")
        cv.create_rectangle(cx + r * 0.3, cy - r * 0.8, cx + r * 0.7,
                            cy + r * 0.8, fill=_MUTED, outline="")
        cv.create_rectangle(cx + r * 1.1, cy - r * 0.8, cx + r * 1.5,
                            cy + r * 0.8, fill=_MUTED, outline="")

    def _art_gyro(self, cv, w, h, s):
        ix, ax, bx, ox = self._io(w)
        cids = s.get("gyro_cids") or ("l3", "r3")
        self._chord_chips(cv, ix, h * 0.42, list(cids))
        self._txt(cv, ix, h * 0.72, "click in BOTH sticks", size=10, fg=_MUTED,
                  bg=_PANEL_BOX, anchor="center")
        self._arrow(cv, ax, h * 0.42, bx, h * 0.42)
        # The pad itself, with a tilt arc sweeping over it and the pointer it
        # steers landing off to the side.
        pcx, pcy = ox - w * 0.01, h * 0.44
        pw, ph = w * 0.20, h * 0.42
        if not self._pad_photo(cv, pcx, pcy, pw, ph):
            kp._round_rect(cv, pcx - pw / 2, pcy - ph / 4, pcx + pw / 2,
                           pcy + ph / 4, 18, outline=_OUTLINE, width=2,
                           fill="")
        cv.create_arc(pcx - pw * 0.86, pcy - ph * 0.62, pcx + pw * 0.86,
                      pcy + ph * 0.62, start=36, extent=108, style="arc",
                      outline=_ACCENT, width=3)
        self._arrow(cv, pcx + pw * 0.62, pcy - ph * 0.30,
                    pcx + pw * 0.80, pcy - ph * 0.10, color=_ACCENT, width=3)
        curx, cury = ox + w * 0.14, h * 0.60
        cv.create_polygon(curx, cury, curx, cury + 22, curx + 6, cury + 16,
                          curx + 11, cury + 25, curx + 15, cury + 22,
                          curx + 10, cury + 14, curx + 17, cury + 13,
                          fill="#ffffff", outline="#1b1f27")
        self._txt(cv, ox, h * 0.88,
                  "tilt to move mouse  ·  works on the on-screen keyboard too",
                  size=10, fg=_MUTED, bg=_PANEL_BOX, anchor="center")

    def _art_vmenu(self, cv, w, h, s):
        """What the tour's own demo menu looks like when it opens: the chord
        that shows it on the left, the 3x3 touch grid it puts on screen  one
        filled cell, highlighted  on the right.

        When the demo can't run (an SDL pad: controller-triggered menus live in
        the HID takeover watcher) this falls back to a generic radial board, so
        the slide still shows what the FEATURE is rather than a menu the user
        is being told to press and can't."""
        cids = s.get("vmenu_cids") or ()
        ix = w * 0.24
        if cids:
            self._chord_chips(cv, ix, h * 0.40, list(cids))
        else:
            pad_cid = "lpad" if self._glyph("lpad") else "l3"
            self._chip(cv, ix, h * 0.40, pad_cid)
            self._txt(cv, ix, h * 0.62, "touch a pad or stick", size=10,
                      fg=_MUTED, bg=_PANEL_BOX, anchor="center")
        self._arrow(cv, w * 0.42, h * 0.40, w * 0.50, h * 0.40)
        if cids:
            self._draw_demo_grid(cv, w * 0.72, h * 0.42, min(h * 0.62, w * 0.30))
            self._txt(cv, w * 0.72, h * 0.90,
                      "Use the Sticks or Touchpads to navigate, press A to "
                      "select", size=10, fg=_MUTED, bg=_PANEL_BOX,
                      anchor="center")
            return
        cx, cy = w * 0.70, h * 0.44
        outer = min(h * 0.36, w * 0.17)
        r = outer * 0.66
        cv.create_oval(cx - outer, cy - outer, cx + outer, cy + outer,
                       fill="#171b22", outline="#2c333d", width=2)
        icons = ("action_01", "action_04", "action_07", "action_11",
                 "action_15", "action_19")
        for i, name in enumerate(icons):
            a = -math.pi / 2 + i * (2 * math.pi / len(icons))
            ex, ey = cx + math.cos(a) * r, cy + math.sin(a) * r
            cv.create_oval(ex - 20, ey - 20, ex + 20, ey + 20, fill="#2b323c",
                           outline="")
            img = None
            try:
                img = self.p._vmenu_icon_photo(name, 24, bg="#2b323c")
            except Exception:
                img = None
            if img is not None:
                self._imgs.append(img)
                cv.create_image(ex, ey, image=img)
        cv.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill=_ACCENT,
                       outline="")
        self._txt(cv, cx, h * 0.90, "your buttons, your icons", size=10,
                  fg=_MUTED, bg=_PANEL_BOX, anchor="center")

    def _draw_demo_grid(self, cv, cx, cy, side):
        """The demo menu's 3x3 touch grid, drawn the way the real overlay
        draws it: empty cells as plain tiles, the one filled cell carrying its
        icon and wearing the highlight ring the thumb would put there."""
        gap = side * 0.06
        cell = (side - gap * 2) / 3.0
        x0, y0 = cx - side / 2.0, cy - side / 2.0
        for i in range(9):
            col, row = i % 3, i // 3
            ex = x0 + col * (cell + gap)
            ey = y0 + row * (cell + gap)
            live = (i == _DEMO_VMENU_SLOT)
            kp._round_rect(cv, ex, ey, ex + cell, ey + cell, 10,
                           fill="#2b323c" if live else "#1d222a",
                           outline=_ACCENT if live else "",
                           width=3 if live else 0)
            if not live:
                continue
            img = None
            try:
                img = self.p._vmenu_icon_photo(_GABEN_ICON, int(cell * 0.72),
                                               bg="#2b323c")
            except Exception:
                img = None
            if img is not None:
                self._imgs.append(img)
                cv.create_image(ex + cell / 2.0, ey + cell / 2.0, image=img)

    def _art_tabs(self, cv, w, h, s):
        """The three binding tabs, drawn with the picker's OWN pill renderer so
        what the user sees here is pixel-identical to what they'll click.

        Each blurb is a RUN of parts rather than one string: the Chords one
        shows the guide button's own glyph where the word would be, matching
        how the picker's own Chords tooltip writes it (see
        _make_chords_tooltip_toplevel)."""
        defs = (("Desktop", [("t", "assign PC controls")]),
                ("Chords", [("t", "assign"), ("icon", None), ("t", "+"),
                            ("key", None), ("t", "combinations")]),
                ("Gamepad", [("t", "assign Xinput game controls")]))
        col = w / 3.0
        for i, (label, parts) in enumerate(defs):
            x = col * i + col / 2.0
            img = kp._pill_tab_photo(self.p.root, label, 13, selected=True)
            if img is not None:
                self._imgs.append(img)
                cv.create_image(x, h * 0.30, image=img)
            else:
                self._txt(cv, x, h * 0.30, label, size=14, fg=_FG,
                          bg=_PANEL_BOX, anchor="center")
            self._arrow(cv, x, h * 0.44, x, h * 0.58, color="#3a3f47", width=2)
            self._draw_blurb(cv, x, h * 0.68, parts)

    def _draw_blurb(self, cv, cx, cy, parts, size=11):
        """One tab's description, centred on cx  a run of ("t", text),
        ("icon", None) and ("key", None) parts. "icon" draws the guide
        button's glyph inline (the same one every Guide chip in the tour uses,
        _GUIDE_ICON_*); "key" draws a blank key cap with a "?" on it, standing
        for "whatever key you like"  the word "Keybind" was naming a thing the
        user has not met yet, where the shape of a key says it at a glance.
        Measured first so the whole run centres as a unit."""
        icon = self._glyph(_GUIDE_ICON_CID, _GUIDE_ICON_KIND)
        cap = float(icon.height()) if icon is not None else 26.0
        gap = 5
        total = 0.0
        widths = []
        for kind_, val in parts:
            if kind_ == "icon":
                wpx = float(icon.width()) if icon is not None else 0.0
            elif kind_ == "key":
                wpx = cap
            else:
                wpx = self._txt_w(val, size=size, fg=_FG, bg=_PANEL_BOX)
            widths.append(wpx)
            total += wpx
        total += gap * max(0, len(parts) - 1)
        x = cx - total / 2.0
        for (kind_, val), wpx in zip(parts, widths):
            if kind_ == "icon":
                if icon is not None:
                    self._imgs.append(icon)
                    cv.create_image(x + wpx / 2.0, cy, image=icon)
            elif kind_ == "key":
                # Round and white, the same button the guide glyph next to it
                # is: the pair reads as two buttons of one set, which is what
                # a chord IS. A grey key cap read as a different kind of thing
                # sitting beside a controller button.
                kx = x + wpx / 2.0
                # Drawn at the glyph's INK diameter, not the full cap: the
                # baked art has a 1px margin, so a disc filling the same box
                # came out visibly fatter than the Guide button beside it
                # (kp._GLYPH_INK_RATIO  same correction as the picker's own
                # _make_tooltip_key, which draws this identical button).
                dpx = kp._glyph_disc_px(cap)
                disc = kp._disc_photo(self.p.root, dpx, "#ffffff", _PANEL_BOX)
                if disc is not None:
                    self._imgs.append(disc)
                    cv.create_image(kx, cy, image=disc)
                else:
                    h = dpx / 2.0
                    cv.create_oval(kx - h, cy - h, kx + h, cy + h,
                                   fill="#ffffff", outline="")
                # Bold face: the plain proportional font _txt uses everywhere
                # else came out hairline-thin at this size, next to a Guide
                # glyph drawn with real stroke weight. Sized off the DISC (the
                # same 0.64 the picker uses), not the blurb's fixed text size 
                # otherwise the mark comes out a different size than the
                # identical "?" the Chords tooltip draws.
                self._txt(cv, kx, cy, "?", size=max(8, int(dpx * 0.64)),
                          fg="#1b1f27", bg="#ffffff", anchor="center",
                          font_path=kp._PILL_FONT_PATH)
            else:
                self._txt(cv, x, cy, val, size=size, fg=_FG, bg=_PANEL_BOX,
                          anchor="w")
            x += wpx + gap

    _COMMANDS_MSG = ("Try to learn the default keybinds, we are using the "
                     "same configuration as Steam Input!")

    def _art_commands(self, cv, w, h, s):
        """The last slide: the user's own Desktop tab, as a skeleton, with the
        one thing left to say written across it.

        The skeleton is MEASURED off the real tab (_Picker._skel_measure_layout
         the same recipe the app's own tab-switch placeholder screen draws
        from), not mocked up here, so what the user is about to walk into is
        what they see the shape of. Deliberately unreadable: the point is
        "there is a whole page of binds behind this", not to relitigate the
        list  the tour spent seven slides on what the defaults DO."""
        self._draw_desktop_skeleton(cv, w, h)
        # The message, on a plate so it stays legible over the bars behind it.
        lines = self._wrap(self._COMMANDS_MSG, 15, w * 0.74)
        line_h = 30
        bh = line_h * len(lines) + 34
        # Sat low rather than centred: the middle of the page is where the
        # controller is, and covering the one recognisable thing on the slide
        # to make room for a sentence about it would be a poor trade.
        by = min(h * 0.70 - bh / 2.0, h - bh - 8)
        kp._round_rect(cv, w * 0.09, by, w * 0.91, by + bh, 14,
                       fill="#0f1319", outline=_ACCENT, width=2)
        y = by + 17 + line_h / 2.0
        for ln in lines:
            self._txt(cv, w / 2.0, y, ln, size=15, fg=_FG, bg="#0f1319",
                      anchor="center")
            y += line_h

    def _draw_desktop_skeleton(self, cv, w, h):
        """The Desktop tab reduced to its placeholder rects, scaled to fit.

        Falls back to a generic arrangement (two stacks of bind rows around a
        controller) when there is no recipe to read  an unbuilt tab for this
        pad, or a window that has never laid one out. The slide has to say the
        same thing either way, and it is a backdrop, not a diagram."""
        rects = []
        try:
            rects = self.p._skel_measure_layout(self._kind, "pc") or []
        except Exception as e:
            print(f"tutorial: desktop skeleton unavailable: {e!r}")
        # The tab pills above the body, drawn with the picker's own renderer so
        # the tabs this is a skeleton OF are named in their real chrome. All
        # three of them: the previous slide just introduced Gamepad alongside
        # Desktop and Chords, and leaving it off here reads as it not being
        # there. Desktop is the selected one because it is the page below.
        top = 6.0
        pills = [kp._pill_tab_photo(self.p.root, name, 10, selected=sel)
                 for name, sel in (("Desktop", True), ("Chords", False),
                                   ("Gamepad", False))]
        pills = [p for p in pills if p is not None]
        if pills:
            gap = 14
            total = sum(p.width() for p in pills) + gap * (len(pills) - 1)
            x = w / 2.0 - total / 2.0
            tall = max(p.height() for p in pills)
            for p in pills:
                self._imgs.append(p)
                cv.create_image(x + p.width() / 2.0, top + tall / 2.0, image=p)
                x += p.width() + gap
            top += tall + 8
        # The page itself, in the app's own background colour: the skeleton's
        # bars are near-invisible on the stage card (they are designed to sit
        # on the darker page), and the plate is also what makes this read as a
        # window rather than as marks floating on the slide.
        kp._round_rect(cv, 0, top, w, h, 10, fill=_BG, outline="")
        if not rects:
            self._draw_skeleton_fallback(cv, w, h, top)
            return
        xs = [r[1] for r in rects] + [r[1] + r[3] for r in rects]
        ys = [r[2] for r in rects] + [r[2] + r[4] for r in rects]
        bw, bh = max(xs) - min(xs), max(ys) - min(ys)
        if bw <= 0 or bh <= 0:
            self._draw_skeleton_fallback(cv, w, h, top)
            return
        # Uniform scale (never up  a small window's tab shouldn't be blown up
        # into a blurry giant), centred in what is left under the pill.
        avail_w, avail_h = w * 0.94, (h - top) * 0.94
        k = min(avail_w / bw, avail_h / bh, 1.0)
        ox = (w - bw * k) / 2.0 - min(xs) * k
        oy = top + (h - top - bh * k) / 2.0 - min(ys) * k
        for role, rx, ry, rw, rh in rects:
            x0, y0 = ox + rx * k, oy + ry * k
            x1, y1 = x0 + rw * k, y0 + rh * k
            fill = self.p._SKEL_FILL.get(role, self.p._SKEL_BAR)
            if role == "blob":
                # The one rect that is NOT left as a placeholder: the real
                # controller art goes where the tab's own controller sits. The
                # bars around it can stay abstract  the pad is what tells the
                # user which page this is.
                if not self._pad_photo(cv, (x0 + x1) / 2.0, (y0 + y1) / 2.0,
                                       rw * k * 0.94, rh * k * 0.94):
                    kp._round_rect(cv, x0 + rw * k * 0.13, y0 + rh * k * 0.16,
                                   x0 + rw * k * 0.87, y0 + rh * k * 0.84,
                                   max(6, 60 * k), fill=fill, outline="")
            elif role == "link":
                mid = (y0 + y1) / 2.0
                cv.create_rectangle(x0, mid - 3, x1, mid + 3, fill=fill,
                                    outline="")
            else:
                cv.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")

    def _draw_skeleton_fallback(self, cv, w, h, top):
        """A stand-in Desktop tab: bind rows down both sides, pad chips beside
        them, the controller between."""
        body = h - top
        rows, rh = 8, body * 0.085
        for side in (0, 1):
            x0 = w * (0.06 if side == 0 else 0.60)
            for i in range(rows):
                y = top + body * 0.06 + i * (rh + body * 0.025)
                cv.create_rectangle(x0, y, x0 + w * 0.26, y + rh,
                                    fill=self.p._SKEL_BAR, outline="")
                cx = x0 + w * 0.275
                cv.create_rectangle(cx, y, cx + w * 0.045, y + rh,
                                    fill=self.p._SKEL_CHIP, outline="")
        cy = top + body * 0.5
        if not self._pad_photo(cv, w * 0.48, cy, w * 0.22, body * 0.6):
            kp._round_rect(cv, w * 0.38, top + body * 0.22, w * 0.58,
                           top + body * 0.78, 40, fill=self.p._SKEL_BLOB,
                           outline="")
