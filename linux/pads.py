# -*- coding: utf-8 -*-
"""Controller catalog + identification.

One place that knows every controller family the app supports: display name,
whether it has analog triggers, its Steam glyph set, the physical labels its
buttons carry, and how to recognize it (USB VID/PID, SDL name, or  for PC
handhelds whose built-in pads present as plain XInput/Xbox devices  the
machine's own BIOS product name).

Used by:
  * tray.py             detection: each newly seen pad kind is persisted to
                         settings["seen_controllers"], which permanently unlocks
                         that controller's picker tab + Options category.
  * keybinds_picker.py  per-kind layout tabs, Options categories, and the
                         scheme-aware (per-controller) button dropdowns.
  * adusk (state/screen/controller)  the OSK's per-controller glyph swap and
                         per-kind settings (haptics, pointer speed, actuation).
  * build_glyphs.py     bakes the Steam glyph sets each kind references.

The Steam Controller ("sc") keeps its dedicated hidapi driver; every other kind
arrives through the SDL3 backend (frames synthesized into SCButtons bits), so
all non-SC kinds share the Switch Pro's control-id space: the picker/runtime
cids stay identical across kinds and only LABELS + GLYPHS differ. That is what
lets one proven runtime path drive every controller family.
"""

import os
import sys

# Ordered: the picker's top bar and the Options categories list kinds in this
# order (sc first  always unlocked).
KINDS = (
    "sc", "sc2015", "steam_deck", "xbox", "ps5", "ps4",
    # --- Nintendo family (see NINTENDO_KINDS) --------------------------------
    # Switch 1: the Pro Controller ("switch", the hand-verified reference every
    # other Nintendo entry is modelled on), the Joy-Cons (each half is its own
    # SDL gamepad; a connected L+R pair is ALSO offered as one combined pad),
    # and the Nintendo Switch Online retro pads.
    "switch", "joycon_pair", "joycon_l", "joycon_r",
    "nso_snes", "nso_nes", "nso_n64", "nso_genesis", "gamecube",
    # Switch 2. Bluetooth on these is a proprietary BLE/GATT protocol no PC
    # Bluetooth stack speaks, so on Windows they are USB-C only (and need an
    # SDL new enough to carry its Switch 2 HIDAPI driver); on Linux they can
    # also arrive through a third-party BLE bridge. Catalogued regardless so
    # that however one reaches SDL, it lands on its own page instead of being
    # mislabelled a Switch 1 Pro Controller.
    "switch2", "joycon2_pair", "joycon2_l", "joycon2_r",
    "rog_ally",
    "legion_go", "msi_claw", "gpd_win", "onexplayer", "ayaneo",
    "8bitdo", "generic",
)

# Every kind made by Nintendo. Two things key off this rather than a bare
# `== "switch"` test: the OSK's Nintendo lettering swap (see nintendo_xy) and
# the Bluetooth dropout guard (nintendo_bt.py)  the ~20-minute firmware
# disconnect is a property of Nintendo's Bluetooth stack, so it applies to
# every wireless pad in this list, Joy-Cons most of all.
NINTENDO_KINDS = (
    "switch", "joycon_pair", "joycon_l", "joycon_r",
    "nso_snes", "nso_nes", "nso_n64", "nso_genesis", "gamecube",
    "switch2", "joycon2_pair", "joycon2_l", "joycon2_r",
)

# The Joy-Con kinds specifically  the pads whose behaviour is governed by the
# SDL combine/orientation hints (see tray's _apply_nintendo_sdl_hints).
JOYCON_KINDS = ("joycon_pair", "joycon_l", "joycon_r",
                "joycon2_pair", "joycon2_l", "joycon2_r")

# A SINGLE Joy-Con  the half-controller played sideways. Its one stick is the
# only analog input it has, and which way "up" points on that stick depends on
# the quarter-turn the hold implies, so these are the kinds the stick-rotation
# option applies to (see single_joycon_side / the joycon_stick_rotate setting).
# A COMBINED pair is held normally and is deliberately excluded.
SINGLE_JOYCON_KINDS = ("joycon_l", "joycon_r", "joycon2_l", "joycon2_r")

# Kinds driven by the dedicated hidapi takeover runtime (tray _Watcher: full
# trackpad desktop control, chords, gamepad remap) rather than the SDL path.
# The Steam Deck and the ORIGINAL 2015 Steam Controller both share the 2026
# Steam Controller's trackpad hardware and control-id space, so they get the
# SC's picker tabs and runtime wholesale  on WINDOWS. The Linux runtime
# (tray_linux.py) has no takeover watcher yet (its desktop mode rides the
# firmware lizard mouse), so there they stay SDL pads with the SDL-template
# tabs until that port lands; the picker and Options pages key off this tuple
# so one shared source does the right thing per platform.
HID_KINDS = (("sc", "sc2015", "steam_deck") if sys.platform == "win32"
             else ("sc",))

# HID-takeover kinds with rear grip paddles (so their OSK-function dropdowns
# offer l4/l5/r4/r5). Deliberately NOT keyed off HID_KINDS: that tuple is
# platform-gated, and which buttons a controller physically has is not.
_GRIP_KINDS = ("sc", "sc2015", "steam_deck")

# Legacy kind aliases found in old settings files ("sdl" was "any SDL pad",
# which in practice meant the Switch Pro).
LEGACY_ALIASES = {"sdl": "switch"}


def canon(kind):
    """Canonical kind for a possibly-legacy value; None if unknown."""
    kind = LEGACY_ALIASES.get(kind, kind)
    return kind if kind in KINDS else None


def is_nintendo(kind):
    """True for any Nintendo-made controller (see NINTENDO_KINDS)."""
    return (canon(kind) or "") in NINTENDO_KINDS


def is_joycon(kind):
    """True for the Joy-Con kinds (single halves and combined pairs)."""
    return (canon(kind) or "") in JOYCON_KINDS


def single_joycon_side(kind):
    """"l" / "r" for a lone Joy-Con, else None.

    The side decides which WAY its stick has to turn: held sideways, a left
    Joy-Con is rotated a quarter turn counter-clockwise (its rail edge  the
    side that clips to the console  becomes the top) and a right Joy-Con
    clockwise. So one option can correct both, in opposite directions."""
    kind = canon(kind) or ""
    if kind not in SINGLE_JOYCON_KINDS:
        return None
    return "l" if kind.endswith("_l") else "r"


# --- Per-kind metadata -------------------------------------------------------
# labels: control id -> the PHYSICAL label printed on that controller. Two id
#   spaces meet here (they never collide):
#     * the OSK-function/settings ids (l1/l2/.../start/back  SC id space,
#       mapped to SCButtons bits in adusk.controller._OSK_CTRL_BITS), and
#     * the SDL layout-template cids (l/r/zl/zr/minus/plus/home/capture 
#       the Switch Pro template every SDL kind reuses).
#   Face buttons are POSITIONAL cids (a=south, b=east, x=west, y=north, the
#   SDL3 convention), so Nintendo-lettered pads label them swapped  the label
#   tells the truth about what's printed on the button the user must press.
# glyphs: control id -> Steam knockout-glyph basename (build_glyphs bakes them).
# osk_hints: ("l2 glyph", "r2 glyph") md-size basenames for the OSK's
#   Shift/Enter trigger hints (baked as <base>_md.png at Steam's md size).
# analog_triggers: gates the three Trigger Actuation sliders (a kind with
#   digital triggers  Switch  never shows them).
# gyro: the hardware has a gyroscope WE can actually read  gates the "Gyro To
#   Mouse" card on that controller's Options category. True for the two HID
#   kinds (the Triton/Deck IMU streams in their state reports) and the SDL
#   kinds whose pads expose SDL_SENSOR_GYRO (Switch Pro, DualShock 4,
#   DualSense). The XInput-presenting kinds (Xbox pads, handheld built-ins,
#   8BitDo in its default X-mode) carry no gyro on the wire even when the
#   shell has one, so they stay False.
# photo: static controller image (data/images/<photo>) for the layout tabs.

_XBOX_LABELS = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "l1": "LB", "r1": "RB", "l2": "LT", "r2": "RT",
    "l3": "L3", "r3": "R3",
    "start": "Menu", "back": "View",
    "dpad_up": "↑", "dpad_down": "↓", "dpad_left": "←", "dpad_right": "→",
    # SDL layout-template cids
    "l": "LB", "r": "RB", "zl": "LT", "zr": "RT",
    "minus": "View", "plus": "Menu", "home": "Guide", "capture": "Share",
    # Back paddles (Xbox Elite lettering). SDL numbers them RIGHT_PADDLE1=P1,
    # LEFT_PADDLE1=P2, RIGHT_PADDLE2=P3, LEFT_PADDLE2=P4, and inputsrc maps
    # those onto the SC's grip bits  so l4=P2, l5=P4, r4=P1, r5=P3.
    "l4": "P2", "l5": "P4", "r4": "P1", "r5": "P3",
    "touchpad": "Touchpad Click",
}

_XBOX_GLYPHS = {
    "a": "shared_color_button_a", "b": "shared_color_button_b",
    "x": "shared_color_button_x", "y": "shared_color_button_y",
    "l": "xbox_lb", "r": "xbox_rb", "zl": "xbox_lt", "zr": "xbox_rt",
    "l1": "xbox_lb", "r1": "xbox_rb", "l2": "xbox_lt", "r2": "xbox_rt",
    "l3": "shared_l3", "r3": "shared_r3",
    "minus": "xbox_button_select", "plus": "xbox_button_start",
    "home": "xbox_button_logo", "capture": "xbox_button_share",
    "start": "xbox_button_start", "back": "xbox_button_select",
    "dpad_up": "shared_dpad_up", "dpad_down": "shared_dpad_down",
    "dpad_left": "shared_dpad_left", "dpad_right": "shared_dpad_right",
}

_PS_LABELS = {
    "a": "Cross", "b": "Circle", "x": "Square", "y": "Triangle",
    "l1": "L1", "r1": "R1", "l2": "L2", "r2": "R2",
    "l3": "L3", "r3": "R3",
    "dpad_up": "↑", "dpad_down": "↓", "dpad_left": "←", "dpad_right": "→",
    "l": "L1", "r": "R1", "zl": "L2", "zr": "R2",
    "home": "PS",
    # The centre touchpad clicks in as its own SDL button on both DualShock 4
    # and DualSense (SDL_GAMEPAD_BUTTON_TOUCHPAD  NOT the MISC1 "capture" bit).
    "touchpad": "Touchpad Click",
}

_PS_GLYPHS_BASE = {
    "a": "ps_color_button_x", "b": "ps_color_button_circle",
    "x": "ps_color_button_square", "y": "ps_color_button_triangle",
    "l3": "shared_l3", "r3": "shared_r3",
    "dpad_up": "ps_dpad_up", "dpad_down": "ps_dpad_down",
    "dpad_left": "ps_dpad_left", "dpad_right": "ps_dpad_right",
}

# --- Nintendo shared tables --------------------------------------------------
# The Switch Pro Controller entry below is the hand-verified reference for the
# whole Nintendo family; these two tables are its labels/glyphs lifted out so
# every other Nintendo kind is a deliberate DELTA from something known-good
# rather than a fresh guess.
#
# Face buttons are POSITIONAL cids (a=south, b=east, x=west, y=north  the SDL3
# convention), and Nintendo letters them the other way round, so the label
# tables say what is actually PRINTED on the button the user has to press.
_NIN_FACE_LABELS = {"a": "B", "b": "A", "x": "Y", "y": "X"}

_NIN_GLYPHS = {
    "a": "shared_button_b", "b": "shared_button_a",
    "x": "shared_button_y", "y": "shared_button_x",
    "l": "switchpro_l", "r": "switchpro_r",
    "zl": "switchpro_l2", "zr": "switchpro_r2",
    "l1": "switchpro_l", "r1": "switchpro_r",
    "l2": "switchpro_l2", "r2": "switchpro_r2",
    "l3": "switchpro_lstick_click", "r3": "switchpro_rstick_click",
    "minus": "switchpro_button_minus", "plus": "switchpro_button_plus",
    "home": "switchpro_button_home", "capture": "switchpro_button_capture",
    "dpad_up": "switchpro_dpad_up", "dpad_down": "switchpro_dpad_down",
    "dpad_left": "switchpro_dpad_left", "dpad_right": "switchpro_dpad_right",
}

# Full Pro-Controller-shaped label set (Pro 1/2, Joy-Con pairs). Joy-Con rails
# put SL/SR on the paddle cids; a Pro Controller simply never sets them.
_NIN_PRO_LABELS = dict(
    _NIN_FACE_LABELS,
    l1="L", r1="R", l2="ZL", r2="ZR", l3="L3", r3="R3",
    start="+", back="–",
    dpad_up="↑", dpad_down="↓", dpad_left="←", dpad_right="→",
    l="L", r="R", zl="ZL", zr="ZR",
    minus="–", plus="+", home="⌂", capture="◉",
    l4="Left SL", l5="Left SR", r4="Right SL", r5="Right SR",
)

# A LONE Joy-Con is played sideways (the default  see the "Hold Single
# Joy-Con Upright" option), so SDL rotates its buttons a quarter turn: the rail
# edge becomes the top, which is what puts SL/SR on the shoulder cids. These
# labels follow that rotation. They are the one part of the Nintendo catalog
# NOT verified against hardware here  if a label reads wrong on your Joy-Con,
# the picker's Listen capture will show which row a button really is, and the
# labels shift again if you switch that Joy-Con to upright.
_JOYCON_L_LABELS = {
    # Left Joy-Con rotated counter-clockwise (its rail is on the right edge):
    # ◁ ends up south, ▽ east, △ west, ▷ north.
    "a": "◁", "b": "▽", "x": "△", "y": "▷",
    "l1": "SL", "r1": "SR", "l": "SL", "r": "SR",
    "l2": "L", "r2": "ZL", "zl": "L", "zr": "ZL",
    "l3": "Stick Click", "r3": "Stick Click",
    "start": "–", "back": "–", "minus": "–", "plus": "–",
    "home": "◉", "capture": "◉",
    "dpad_up": "↑", "dpad_down": "↓", "dpad_left": "←", "dpad_right": "→",
}
_JOYCON_R_LABELS = {
    # Right Joy-Con rotated clockwise (rail on the left edge): A south, X east,
    # B west, Y north.
    "a": "A", "b": "X", "x": "B", "y": "Y",
    "l1": "SL", "r1": "SR", "l": "SL", "r": "SR",
    "l2": "R", "r2": "ZR", "zl": "R", "zr": "ZR",
    "l3": "Stick Click", "r3": "Stick Click",
    "start": "+", "back": "+", "minus": "+", "plus": "+",
    "home": "⌂", "capture": "⌂",
    "dpad_up": "↑", "dpad_down": "↓", "dpad_left": "←", "dpad_right": "→",
}

PADS = {
    "sc": {
        "name": "Steam Controller",
        "analog_triggers": True,
        "photo": "controller_triton.png",
        "osk_hints": None,   # keeps its custom glyph_l2.png / sc_r2_md.png art
        "labels": {
            "a": "A", "b": "B", "x": "X", "y": "Y",
            "l1": "L1", "r1": "R1", "l2": "L2", "r2": "R2",
            "l3": "L3", "r3": "R3",
            "l4": "L4", "l5": "L5", "r4": "R4", "r5": "R5",
            "start": "≡", "back": "⧉",
            "dpad_up": "↑", "dpad_down": "↓",
            "dpad_left": "←", "dpad_right": "→",
        },
        "glyphs": {},        # keybinds_picker._GLYPH_FILES["sc"] stays canonical
    },
    "sc2015": {
        # The ORIGINAL Steam Controller. Same trackpad-first design the whole
        # app is built around, so it runs the full HID takeover runtime (see
        # HID_KINDS)  it just has less hardware than the 2026 unit: ONE rear
        # paddle per side instead of two, no right stick (that side is all
        # trackpad), no D-pad (the left pad's quadrants were it), no "..."
        # button, and no capacitive stick/grip sensors. Everything it lacks is
        # listed in ABSENT_CONTROLS, which is what strips those rows from its
        # layout tabs, its chord vocabulary and its OSK dropdowns.
        #
        # The two small buttons flanking the Steam button are printed as
        # ARROWS on this hardware (◀ back / ▶ start), not the 2026 unit's
        # ≡/⧉  Steam's own glyph set names them sc_button_l_arrow /
        # sc_button_r_arrow. Their bits are the usual VIEW/START pair, and
        # they sit on the same physical sides as the SC spec's columns, so no
        # row swap is needed (unlike the Deck's Menu/View).
        # Labels/glyphs cover BOTH id spaces (like the Steam Deck's entry):
        # the SC space on the platforms that run the takeover runtime, and the
        # SDL layout-template space where it arrives as an ordinary SDL pad
        # (Linux, until the takeover port lands  see HID_KINDS).
        "name": "Steam Controller (2015)",
        "analog_triggers": True,
        "photo": "controller_sc2015.png",
        "osk_hints": None,   # same trigger art as the 2026 unit (sc_l2/sc_r2)
        "labels": {
            "a": "A", "b": "B", "x": "X", "y": "Y",
            "l1": "L1", "r1": "R1", "l2": "L2", "r2": "R2",
            "l3": "L3",
            # The single rear paddle per side. Steam Input calls them the
            # left/right GRIP, and prints LG/RG on its glyphs.
            "l4": "LG", "r4": "RG",
            "start": "▶", "back": "◀",
            "l": "L1", "r": "R1", "zl": "L2", "zr": "R2",
            "minus": "◀", "plus": "▶", "home": "Steam",
        },
        # Steam's sc_* set IS this controller's own art  the 2026 entry
        # borrows the Deck's sd_* pieces where the newer hardware differs, so
        # here the sc_* originals go back in.
        "glyphs": {
            "a": "shared_button_a", "b": "shared_button_b",
            "x": "shared_button_x", "y": "shared_button_y",
            "l": "sc_l1", "r": "sc_r1",
            "zl": "sc_l2_half", "zr": "sc_r2_half",
            "l1": "sc_l1", "r1": "sc_r1",
            "l2": "sc_l2_half", "r2": "sc_r2_half",
            "l3": "shared_l3",
            "l4": "sc_lg", "r4": "sc_rg",
            "minus": "sc_button_l_arrow", "plus": "sc_button_r_arrow",
            "back": "sc_button_l_arrow", "start": "sc_button_r_arrow",
            "home": "sc_button_steam", "steam": "sc_button_steam",
            # The left pad doubled as this controller's D-pad, which is how
            # Steam draws it; the right one is a plain touchpad.
            "lpad": "sc_dpad_click", "rpad": "sc_touchpad_click",
        },
    },
    # --- Nintendo -----------------------------------------------------------
    # The Switch Pro Controller is the verified reference; everything below it
    # in this block states only how it DIFFERS. All of them share the Switch
    # glyph set (the only Nintendo art Steam ships that we bake), and a control
    # with no glyph simply renders as a text chip carrying its label  which is
    # why the retro pads need no art of their own to read correctly.
    "switch": {
        "name": "Switch Pro Controller",
        "analog_triggers": False,   # ZL/ZR are digital switches
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_NIN_PRO_LABELS),
        "glyphs": dict(_NIN_GLYPHS),
    },
    "switch2": {
        # Switch 2 Pro Controller. Same shape as the Switch 1 Pro plus two rear
        # buttons (GL/GR) and the C/GameChat button. The rear pair is labelled
        # on the paddle cids, which is where a driver would surface them; C has
        # no cid because nothing we can read reports it yet. Steam's own glyph
        # set (checked directly against a local install) has no dedicated
        # PHOTO for this controller  still just the four Switch 1 photos  but
        # it DOES carry real GL/GR icons (switchpro_gl/gr, added alongside the
        # C-button glyph, both absent from the older Switch 1 set), so those
        # get real Steam art instead of a text-only chip.
        "name": "Switch 2 Pro Controller",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_NIN_PRO_LABELS, l4="GL", r4="GR",
                       l5="GL", r5="GR"),
        "glyphs": dict(_NIN_GLYPHS, l4="switchpro_gl", r4="switchpro_gr",
                       l5="switchpro_gl", r5="switchpro_gr"),
    },
    "joycon_pair": {
        # Both Joy-Cons held as one controller (SDL's combined pad  see the
        # "Combine Joy-Cons" option). Pro Controller layout, plus the four rail
        # buttons on the paddle cids.
        "name": "Joy-Con Pair",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_NIN_PRO_LABELS),
        "glyphs": dict(_NIN_GLYPHS),
    },
    "joycon_l": {
        "name": "Joy-Con (L)",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_JOYCON_L_LABELS),
        "glyphs": dict(_NIN_GLYPHS),
    },
    "joycon_r": {
        "name": "Joy-Con (R)",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_JOYCON_R_LABELS),
        "glyphs": dict(_NIN_GLYPHS),
    },
    "joycon2_pair": {
        "name": "Joy-Con 2 Pair",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_NIN_PRO_LABELS),
        "glyphs": dict(_NIN_GLYPHS),
    },
    "joycon2_l": {
        "name": "Joy-Con 2 (L)",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_JOYCON_L_LABELS),
        "glyphs": dict(_NIN_GLYPHS),
    },
    "joycon2_r": {
        "name": "Joy-Con 2 (R)",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_JOYCON_R_LABELS),
        "glyphs": dict(_NIN_GLYPHS),
    },
    # Nintendo Switch Online retro pads. They pair exactly like a Pro
    # Controller and SDL drives them through the same Switch HIDAPI driver,
    # but each has far fewer controls: the labels below name only what the
    # hardware actually has, and the controls it LACKS are listed in
    # CHORD_SKIP so they never show up as bindable.
    "nso_snes": {
        "name": "SNES Controller",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": dict(_NIN_FACE_LABELS,
                       l1="L", r1="R", l="L", r="R",
                       start="Start", back="Select",
                       minus="Select", plus="Start", home="⌂",
                       dpad_up="↑", dpad_down="↓",
                       dpad_left="←", dpad_right="→"),
        "glyphs": dict(_NIN_GLYPHS),
    },
    "nso_nes": {
        # Covers the NES pads and the Famicom controllers, which SDL surfaces
        # as a left/right pair of mini-gamepads.
        "name": "NES Controller",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": {"a": "B", "b": "A",
                   "start": "Start", "back": "Select",
                   "minus": "Select", "plus": "Start", "home": "⌂",
                   "dpad_up": "↑", "dpad_down": "↓",
                   "dpad_left": "←", "dpad_right": "→"},
        "glyphs": dict(_NIN_GLYPHS),
    },
    "nso_n64": {
        # The N64 pad's four C buttons arrive as the RIGHT STICK, which is why
        # the stick-direction cids carry the C labels here.
        "name": "N64 Controller",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": {"a": "A", "b": "B",
                   "l1": "L", "r1": "R", "l": "L", "r": "R",
                   "l2": "Z", "zl": "Z", "r2": "ZR", "zr": "ZR",
                   "start": "Start", "plus": "Start", "home": "⌂",
                   "dpad_up": "↑", "dpad_down": "↓",
                   "dpad_left": "←", "dpad_right": "→",
                   "rstick_up": "C ↑", "rstick_down": "C ↓",
                   "rstick_left": "C ←", "rstick_right": "C →"},
        "glyphs": dict(_NIN_GLYPHS),
    },
    "nso_genesis": {
        # 6-button Mega Drive/Genesis pad: A B C on the front row, X Y Z above.
        # Six buttons don't fit four face slots, so two of them land on the
        # shoulder cids and the exact arrangement isn't verified here  the
        # face labels below are the positional convention (the same letters the
        # pad prints, possibly not in the same places). The picker's Listen
        # capture shows which row a button really is.
        "name": "Genesis Controller",
        "analog_triggers": False,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        "labels": {"a": "A", "b": "B", "x": "X", "y": "Y",
                   "l1": "Z", "r1": "C", "l": "Z", "r": "C",
                   "start": "Start", "plus": "Start",
                   "back": "Mode", "minus": "Mode", "home": "⌂",
                   "dpad_up": "↑", "dpad_down": "↓",
                   "dpad_left": "←", "dpad_right": "→"},
        "glyphs": dict(_NIN_GLYPHS),
    },
    "gamecube": {
        # GameCube pads  through the WUP-028 adapter, and the NSO GameCube
        # controller for Switch 2. Its L/R ARE analog (the only Nintendo pad
        # here where the trigger actuation sliders mean anything), and its face
        # buttons sit in GameCube's own arrangement rather than Nintendo's
        # usual lettering, so nintendo_xy() deliberately excludes it.
        "name": "GameCube Controller",
        "analog_triggers": True,
        "photo": "controller_switch_pro.png",
        "osk_hints": ("switchpro_l2", "switchpro_r2"),
        # Z is a digital shoulder (SDL puts it on the right bumper); L and R
        # are the analog triggers.
        "labels": {"a": "A", "b": "X", "x": "B", "y": "Y",
                   "r1": "Z", "r": "Z",
                   "l2": "L", "r2": "R", "zl": "L", "zr": "R",
                   "start": "Start", "plus": "Start", "home": "⌂",
                   "dpad_up": "↑", "dpad_down": "↓",
                   "dpad_left": "←", "dpad_right": "→",
                   "rstick_up": "C ↑", "rstick_down": "C ↓",
                   "rstick_left": "C ←", "rstick_right": "C →"},
        "glyphs": dict(_NIN_GLYPHS),
    },
    "xbox": {
        "name": "Xbox Controller",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        "labels": _XBOX_LABELS,
        "glyphs": _XBOX_GLYPHS,
    },
    "ps5": {
        "name": "DualSense Controller",
        "analog_triggers": True,
        "photo": "controller_ps5.png",
        "osk_hints": ("ps5_l2", "ps5_r2"),
        # DualSense Edge adds two rear buttons (SDL's LEFT/RIGHT_PADDLE1).
        "labels": dict(_PS_LABELS, start="Options", back="Create",
                       minus="Create", plus="Options", capture="Mute",
                       l4="Left Back", r4="Right Back"),
        "glyphs": dict(_PS_GLYPHS_BASE,
                       l="ps5_l1", r="ps5_r1", zl="ps5_l2", zr="ps5_r2",
                       l1="ps5_l1", r1="ps5_r1", l2="ps5_l2", r2="ps5_r2",
                       minus="ps5_button_create", plus="ps5_button_options",
                       start="ps5_button_options", back="ps5_button_create",
                       home="ps4_button_logo", capture="ps_button_mute"),
    },
    "ps4": {
        "name": "DualShock 4 Controller",
        "analog_triggers": True,
        "photo": "controller_ps4.png",
        "osk_hints": ("ps4_l2", "ps4_r2"),
        # "capture" is the SDL MISC1 bit, which a DualShock 4 never reports 
        # it used to be labelled "Pad Click", but the pad click is its own SDL
        # button and now has its own "touchpad" cid (see _PS_LABELS). Left in
        # place (bit-compatible with the shared SDL template) but named for
        # what it is, and glyph-less so it renders as a plain text chip.
        "labels": dict(_PS_LABELS, start="Options", back="Share",
                       minus="Share", plus="Options", capture="Extra Button"),
        "glyphs": dict(_PS_GLYPHS_BASE,
                       l="ps4_l1", r="ps4_r1", zl="ps4_l2", zr="ps4_r2",
                       l1="ps4_l1", r1="ps4_r1", l2="ps4_l2", r2="ps4_r2",
                       minus="ps4_button_share", plus="ps4_button_options",
                       start="ps4_button_options", back="ps4_button_share",
                       home="ps4_button_logo", touchpad="ps4_trackpad_click"),
    },
    "steam_deck": {
        "name": "Steam Deck",
        "analog_triggers": True,
        "photo": "controller_steam_deck.png",
        "osk_hints": ("sd_l2", "sd_r2"),
        "labels": {
            "a": "A", "b": "B", "x": "X", "y": "Y",
            "l1": "L1", "r1": "R1", "l2": "L2", "r2": "R2",
            "l3": "L3", "r3": "R3",
            "l4": "L4", "l5": "L5", "r4": "R4", "r5": "R5",
            "start": "Menu", "back": "View",
            "dpad_up": "↑", "dpad_down": "↓",
            "dpad_left": "←", "dpad_right": "→",
            "l": "L1", "r": "R1", "zl": "L2", "zr": "R2",
            "minus": "View", "plus": "Menu", "home": "Steam", "capture": "…",
        },
        "glyphs": {
            "a": "shared_button_a", "b": "shared_button_b",
            "x": "shared_button_x", "y": "shared_button_y",
            "l": "sd_l1", "r": "sd_r1", "zl": "sd_l2", "zr": "sd_r2",
            "l1": "sd_l1", "r1": "sd_r1", "l2": "sd_l2", "r2": "sd_r2",
            "l3": "shared_l3", "r3": "shared_r3",
            "l4": "sd_l4", "l5": "sd_l5", "r4": "sd_r4", "r5": "sd_r5",
            "minus": "sd_button_view", "plus": "sd_button_menu",
            "start": "sd_button_menu", "back": "sd_button_view",
            "home": "sd_button_steam", "capture": "sd_button_aux",
            "dpad_up": "shared_dpad_up", "dpad_down": "shared_dpad_down",
            "dpad_left": "shared_dpad_left", "dpad_right": "shared_dpad_right",
        },
    },
    # PC handhelds: their built-in pads present as XInput/Xbox devices, so the
    # Xbox labels/glyphs are the truth of what the firmware exposes. Detected
    # by the MACHINE (BIOS product name) as much as by the pad itself.
    "rog_ally": {
        "name": "ROG Ally",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        # Two rear paddles, printed M1 (right) / M2 (left).
        "labels": dict(_XBOX_LABELS, l4="M2", r4="M1"),
        "glyphs": _XBOX_GLYPHS,
    },
    "legion_go": {
        "name": "Legion Go",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        # Y1 on the right controller face; M2/M3 on its back; plus a touchpad.
        "labels": dict(_XBOX_LABELS, capture="Y1", l4="M2", r4="M3"),
        "glyphs": dict(_XBOX_GLYPHS, capture="lgs_y1"),
    },
    "msi_claw": {
        "name": "MSI Claw",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        "labels": dict(_XBOX_LABELS, l4="M2", r4="M1"),
        "glyphs": _XBOX_GLYPHS,
    },
    "gpd_win": {
        "name": "GPD Win",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        "labels": dict(_XBOX_LABELS, l4="L4", r4="R4"),
        "glyphs": _XBOX_GLYPHS,
    },
    "onexplayer": {
        "name": "OneXPlayer",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        "labels": dict(_XBOX_LABELS, l4="L4", r4="R4"),
        "glyphs": _XBOX_GLYPHS,
    },
    "ayaneo": {
        "name": "AYANEO",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        "labels": dict(_XBOX_LABELS, l4="L4", r4="R4"),
        "glyphs": _XBOX_GLYPHS,
    },
    "8bitdo": {
        "name": "8BitDo Controller",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        # Ultimate-series rear buttons are printed L4 / R4.
        "labels": dict(_XBOX_LABELS, home="Home", l4="L4", r4="R4"),
        "glyphs": dict(_XBOX_GLYPHS, home="8bitdo_button_home"),
    },
    "generic": {
        "name": "Generic Controller",
        "analog_triggers": True,
        "photo": "controller_xbox.png",
        "osk_hints": ("xbox_lt", "xbox_rt"),
        "labels": _XBOX_LABELS,
        "glyphs": _XBOX_GLYPHS,
    },
}


# Stick-direction (+ click) glyphs are the shared Steam set on every kind 
# merged into each non-SC catalog entry so the full-parity layout tabs (stick
# direction rows) show art instead of text chips.
_STICK_GLYPHS = {
    "lstick_up": "shared_lstick_up", "lstick_down": "shared_lstick_down",
    "lstick_left": "shared_lstick_left", "lstick_right": "shared_lstick_right",
    "rstick_up": "shared_rstick_up", "rstick_down": "shared_rstick_down",
    "rstick_left": "shared_rstick_left", "rstick_right": "shared_rstick_right",
}
for _k, _p in PADS.items():
    if _k != "sc":
        for _cid, _g in _STICK_GLYPHS.items():
            _p["glyphs"].setdefault(_cid, _g)

# --- Optional physical controls, per kind ------------------------------------
# The SDL layout template (a/b/x/y, l/r/zl/zr, l3/r3, minus/plus, home/capture,
# dpad) is the set EVERY pad has. Real hardware carries more: rear paddles and a
# clickable touchpad both arrive as their own SDL buttons and are synthesized
# into the SC bit space by adusk.inputsrc._SDL_TO_SC (paddles -> the grip bits,
# touchpad -> LPAD), but they only make sense to OFFER on kinds that have them.
# The Hotkeys button dropdown builds each kind's vocabulary from these two
# tables (see keybinds_picker._sdl_chord_labels); the HID kinds ("sc",
# "steam_deck") don't consult them  they use SC_CHORD_BUTTON_LABELS whole.
#
# CHORD_EXTRA: template-extra cids this kind physically has.
# CHORD_SKIP:  template cids this kind physically LACKS.
_PADDLES_2 = ("l4", "r4")
_PADDLES_4 = ("l4", "l5", "r4", "r5")

CHORD_EXTRA = {
    # 2015 Steam Controller on the SDL path (Linux): its two grip levers.
    "sc2015":     _PADDLES_2,
    "switch":     _PADDLES_4,                     # Joy-Con SL/SR rails
    "switch2":    _PADDLES_2,                     # GL / GR rear buttons
    # A COMBINED Joy-Con pair reports all four rail buttons; a lone Joy-Con
    # spends its SL/SR on the shoulder cids instead (it is held sideways), so
    # it has no extras at all.
    "joycon_pair":  _PADDLES_4,
    "joycon2_pair": _PADDLES_4,
    "xbox":       _PADDLES_4,                     # Elite P1-P4
    "ps5":        _PADDLES_2 + ("touchpad",),     # Edge rear buttons + pad click
    "ps4":        ("touchpad",),
    "rog_ally":   _PADDLES_2,                     # M1 / M2
    "legion_go":  _PADDLES_2 + ("touchpad",),     # M2 / M3 + touchpad
    "msi_claw":   _PADDLES_2,                     # M1 / M2
    "gpd_win":    _PADDLES_2,
    "onexplayer": _PADDLES_2,
    "ayaneo":     _PADDLES_2,
    "8bitdo":     _PADDLES_2,                     # Ultimate L4 / R4
    "generic":    _PADDLES_4 + ("touchpad",),     # unknown pad: offer it all
}

# --- Controls a kind physically does NOT have --------------------------------
# The SDL layout template assumes a modern twin-stick pad. Nintendo's back
# catalog does not: an NSO SNES pad has no sticks at all, a lone Joy-Con has
# one, the GameCube pad's sticks don't click. Listing the missing controls here
# keeps them out of BOTH the layout tabs and the Hotkeys button vocabulary, so
# a page never offers a binding that could not possibly fire.
#
# Stick DIRECTIONS are listed via the _NO_STICKS/_NO_RSTICK shorthands below.
_LSTICK_CIDS = ("lstick_up", "lstick_down", "lstick_left", "lstick_right", "l3")
_RSTICK_CIDS = ("rstick_up", "rstick_down", "rstick_left", "rstick_right", "r3")
_NO_STICKS = _LSTICK_CIDS + _RSTICK_CIDS
_DPAD_CIDS = ("dpad_up", "dpad_down", "dpad_left", "dpad_right")

# 2015 Steam Controller  controls it simply hasn't got, on either path: the
# second rear paddle per side (it has ONE, on l4/r4), the LG/RG grip-squeeze
# pads, the capacitive grip-rest and thumbstick sensors, and the "..." (QAM /
# SDL "capture") button that arrived with the 2026 unit.
_SC2015_ABSENT = ("l5", "r5", "lg", "rg", "lgrip_touch", "rgrip_touch",
                  "qam", "capture", "lstick_touch", "rstick_touch")
# ...and two more, but ONLY where the HID takeover drives it. Physically this
# controller has no right stick (that whole side is trackpad) and no D-pad
# (the left TRACKPAD was the D-pad, and its quadrants ride the same click
# switch as `lpad`, so the driver deliberately doesn't surface them as
# separate bits  see _SC2015_BTN_MAP_1 in steamcontroller/__init__.py). On
# the SDL path those rows ARE real: SDL re-presents the right pad as a right
# stick and the left pad's quadrants as a D-pad, which is the only way to
# reach them there.
_SC2015_ABSENT_HID = ("r3", "rstick_click",
                      "rstick_up", "rstick_down",
                      "rstick_left", "rstick_right",
                      "dpad_up", "dpad_down", "dpad_left", "dpad_right")

ABSENT_CONTROLS = {
    "sc2015": _SC2015_ABSENT + (_SC2015_ABSENT_HID
                                if "sc2015" in HID_KINDS else ()),
    # NSO retro pads: D-pad + face buttons + Start/Select, nothing more.
    "nso_snes": _NO_STICKS + ("zl", "zr", "capture"),
    "nso_nes": _NO_STICKS + ("l", "r", "zl", "zr", "x", "y", "capture"),
    # N64: one stick, the four C buttons arriving as the right stick, L/R/Z,
    # A/B and Start. No X/Y, no Select, no stick clicks.
    "nso_n64": ("l3", "r3", "x", "y", "zr", "minus", "capture"),
    # Mega Drive/Genesis 6-button: D-pad, six buttons, Start and Mode.
    "nso_genesis": _NO_STICKS + ("zl", "zr", "capture"),
    # GameCube: analog L/R on the trigger cids, Z on the right shoulder, no
    # left shoulder, no clickable sticks, no Select/Capture.
    "gamecube": ("l", "l3", "r3", "minus", "capture"),
    # A lone Joy-Con is one stick and four buttons held sideways: no D-pad (on
    # the left half those buttons ARE the faces), no second stick, and the
    # Home/+ pair lives on the right half while –/Capture live on the left.
    "joycon_l": _RSTICK_CIDS + _DPAD_CIDS + ("home", "plus"),
    "joycon_r": _RSTICK_CIDS + _DPAD_CIDS + ("capture", "minus"),
}
ABSENT_CONTROLS["joycon2_l"] = ABSENT_CONTROLS["joycon_l"]
ABSENT_CONTROLS["joycon2_r"] = ABSENT_CONTROLS["joycon_r"]

# TWO id spaces name the same physical controls (see the catalog docstring):
# the OSK/settings space (l1/l2/start/back) and the SDL layout-template space
# (l/zl/plus/minus)  e.g. "l2" and "zl" are both SCButtons.LT. The table above
# is written in whichever space reads naturally per entry, so expand each one
# to cover BOTH. Without this a control the hardware lacks vanished from the
# layout tabs (which use the template space) while the OSK button dropdowns
# (which use the OSK space) still offered it  an NSO SNES pad was offering ZL
# as its Shift key, and the OSK's default Shift/Enter silently could not fire.
_CID_ALIASES = (("l1", "l"), ("r1", "r"), ("l2", "zl"), ("r2", "zr"),
                ("start", "plus"), ("back", "minus"))


def _expand_absent(cids):
    out = set(cids)
    for a, b in _CID_ALIASES:
        if a in out:
            out.add(b)
        if b in out:
            out.add(a)
    return tuple(sorted(out))


ABSENT_CONTROLS = {_k: _expand_absent(_v)
                   for _k, _v in ABSENT_CONTROLS.items()}

CHORD_SKIP = dict(ABSENT_CONTROLS)
# A DualShock 4 has no MISC1 button at all  its pad click is the dedicated
# "touchpad" cid above, not "capture". (Kept out of ABSENT_CONTROLS: the row
# has always been on its layout tab, and removing a control someone may have
# already bound is not this table's job.)
CHORD_SKIP["ps4"] = ("capture",)


def absent_controls(kind):
    """Control ids this kind's hardware doesn't have  dropped from its layout
    tabs (see keybinds_picker._sdl_layout)."""
    return ABSENT_CONTROLS.get(canon(kind) or "", ())


def chord_extra_buttons(kind):
    """Extra (non-template) chord-button cids this kind physically has."""
    return CHORD_EXTRA.get(canon(kind) or "", ())


def chord_skip_buttons(kind):
    """Template chord-button cids this kind physically LACKS."""
    return CHORD_SKIP.get(canon(kind) or "", ())


# Kinds with a readable gyroscope (see the catalog comment)  stamped onto the
# catalog here so a future kind defaults to no-gyro until proven otherwise.
_GYRO_KINDS = ("sc", "sc2015", "steam_deck", "switch", "ps4", "ps5",
               # Every Joy-Con and Switch 2 pad carries an IMU too (a single
               # Joy-Con is Nintendo's own motion controller). The NSO retro
               # pads and the GameCube pad have none.
               "joycon_pair", "joycon_l", "joycon_r",
               "switch2", "joycon2_pair", "joycon2_l", "joycon2_r")
for _k, _p in PADS.items():
    _p["gyro"] = _k in _GYRO_KINDS


def display_name(kind):
    p = PADS.get(canon(kind) or "")
    return p["name"] if p else str(kind)


def has_analog_triggers(kind):
    p = PADS.get(canon(kind) or "")
    return bool(p and p["analog_triggers"])


def has_gyro(kind):
    """True when this kind's hardware streams gyro data we can read  gates
    the per-controller "Gyro To Mouse" Options card and its runtime."""
    p = PADS.get(canon(kind) or "")
    return bool(p and p.get("gyro"))


# The program-wide DEFAULT "Gyro To Mouse" hotkey: clicking BOTH thumbsticks at
# once. It ships configured on every gyro-capable controller (mode "toggle",
# output "mouse") so the gyro is one gesture away without anyone having to
# discover the cog modal first. Both ids exist in BOTH chord-button spaces (see
# keybinds_runtime.SC_CHORD_BUTTONS / SDL_CHORD_BUTTONS), so one pair covers
# every kind.
GYRO_TOGGLE_DEFAULT_BUTTONS = ("l3", "r3")


def default_gyro_toggle_buttons(kind):
    """This kind's default gyro-toggle chord  both stick clicks, or () when
    the hardware hasn't got both of them (a lone Joy-Con has one stick, the
    2015 Steam Controller's right side is a trackpad on the HID path). No
    default is invented for those: their modal opens with an empty bar."""
    if not has_gyro(kind):
        return ()
    absent = set(absent_controls(kind)) | set(chord_skip_buttons(kind))
    if any(b in absent for b in GYRO_TOGGLE_DEFAULT_BUTTONS):
        return ()
    return GYRO_TOGGLE_DEFAULT_BUTTONS


def default_gyro_toggle_chord(kind):
    """The saved-chord form of the above ({"buttons": [...], "type":
    "gyro_toggle"}), or None when this kind has no default."""
    btns = default_gyro_toggle_buttons(kind)
    return {"buttons": list(btns), "type": "gyro_toggle"} if btns else None


def label_for(kind, cid, default=None):
    """The physical label printed on `cid` for this controller kind."""
    p = PADS.get(canon(kind) or "")
    if not p:
        return default if default is not None else cid
    return p["labels"].get(cid, default if default is not None else cid)


def glyph_for(kind, cid):
    """Steam knockout-glyph basename for a control, or None (text chip)."""
    p = PADS.get(canon(kind) or "")
    return p["glyphs"].get(cid) if p else None


def osk_hint_glyphs(kind):
    """(l2_hint, r2_hint) md-size glyph FILENAMES for the OSK's Shift/Enter
    trigger hints, or None for the Steam Controller (custom art)."""
    p = PADS.get(canon(kind) or "")
    if not p or not p.get("osk_hints"):
        return None
    l2, r2 = p["osk_hints"]
    return (l2 + "_md.png", r2 + "_md.png")


# Nintendo kinds whose face buttons carry the usual Nintendo lettering, i.e.
# X/Y (and A/B) are swapped relative to the positional cids. Excluded: the
# GameCube pad (its own arrangement, not a straight swap), the N64/Genesis pads
# (no X/Y pair to swap) and the lone Joy-Cons (held sideways  their face
# buttons are a rotation, not a swap).
_NINTENDO_XY_KINDS = ("switch", "switch2", "joycon_pair", "joycon2_pair",
                      "nso_snes", "nso_nes")


def nintendo_xy(kind):
    """True when the kind's physical X/Y (and A/B) lettering is swapped vs the
    positional cids (Nintendo layouts)  drives the OSK's X/Y hint-glyph swap."""
    return canon(kind) in _NINTENDO_XY_KINDS


# --- Per-kind settings keys --------------------------------------------------
# The SC and Switch Pro keep their historical settings names; every other kind
# uses "<base>_<kind>". `base` is one of: osk_trigger_actuation,
# mouse_trigger_actuation, gamepad_trigger_actuation, pointer_speed,
# rumble_enabled, rumble_gamepad.
_LEGACY_SETTING_KEYS = {
    ("sc", "osk_trigger_actuation"): "sc_osk_trigger_actuation",
    ("sc", "mouse_trigger_actuation"): "sc_mouse_trigger_actuation",
    ("sc", "gamepad_trigger_actuation"): "sc_gamepad_trigger_actuation",
    ("sc", "pointer_speed"): "sc_pointer_speed",
    ("sc", "rumble_enabled"): "rumble_enabled_sc",
    ("sc", "rumble_gamepad"): "rumble_gamepad_sc",
    # HID-takeover trackpad settings (the Steam Deck gets per-kind copies:
    # "trackpad_speed_steam_deck", "lpad_click_button_steam_deck", ...).
    ("sc", "trackpad_speed"): "sc_trackpad_speed",
    ("sc", "lpad_click_button"): "lpad_click_button",
    ("sc", "rpad_click_button"): "rpad_click_button",
    ("switch", "pointer_speed"): "switch_pointer_speed",
    ("switch", "rumble_enabled"): "rumble_enabled_switch",
    ("switch", "rumble_gamepad"): "rumble_gamepad_switch",
}


def setting_key(kind, base):
    kind = canon(kind) or kind
    return _LEGACY_SETTING_KEYS.get((kind, base), "%s_%s" % (base, kind))


def parse_setting_key(key):
    """Inverse of setting_key: (kind, base) or None if not a per-kind key."""
    for (kind, base), legacy in _LEGACY_SETTING_KEYS.items():
        if key == legacy:
            return kind, base
    for base in ("osk_trigger_actuation", "mouse_trigger_actuation",
                 "gamepad_trigger_actuation", "pointer_speed",
                 "rumble_enabled", "rumble_gamepad", "trackpad_speed",
                 "lpad_click_button", "rpad_click_button",
                 # "Gyro To Mouse" cog-modal tuning (per gyro-capable kind).
                 "gyro_mode", "gyro_dots", "gyro_sens", "gyro_accel",
                 "gyro_deadzone", "gyro_precision", "gyro_output",
                 # Advanced Presses timing (per kind)  see
                 # keybinds_runtime.adv_timing, which builds the same keys.
                 "adv_long_ms", "adv_double_ms", "adv_soft_pct"):
        prefix = base + "_"
        if key.startswith(prefix):
            kind = canon(key[len(prefix):])
            if kind:
                return kind, base
    return None


# --- OSK function rebind: which controls each kind offers ---------------------
# Ids from the SC space (adusk.controller._OSK_CTRL_BITS). The Steam Controller
# and Steam Deck have back grips (l4/l5/r4/r5); plain pads don't.
_OSK_IDS_BASE = ("none", "a", "b", "x", "y", "l1", "r1", "l2", "r2",
                 "l3", "r3", "start", "back",
                 "dpad_up", "dpad_down", "dpad_left", "dpad_right")
_OSK_IDS_GRIPS = ("none", "a", "b", "x", "y", "l1", "r1", "l2", "r2",
                  "l3", "r3", "l4", "l5", "r4", "r5", "start", "back",
                  "dpad_up", "dpad_down", "dpad_left", "dpad_right")


def osk_button_ids(kind):
    """Control ids offered by the per-controller OSK-function dropdowns 
    minus the ones this pad hasn't physically got, so an NSO SNES pad can't be
    told to put Shift on a ZL it doesn't have."""
    ids = _OSK_IDS_GRIPS if canon(kind) in _GRIP_KINDS else _OSK_IDS_BASE
    absent = set(absent_controls(kind))
    return tuple(c for c in ids if c not in absent) if absent else ids


# Default OSK function → button per controller. The base row is the historical
# Steam Controller / Switch Pro mapping; a kind only appears in the override
# table when the base default names a button it hasn't got. Nintendo's back
# catalog is where this bites: the NSO pads have no ZL/ZR and no stick clicks,
# so Shift/Enter/Caps all defaulted onto controls that could never fire, and
# the keyboard came up with no usable Enter key at all.
_OSK_DEFAULT_BUTTONS = {"shift": "l2", "enter": "r2", "space": "y",
                        "backspace": "x", "caps": "l3"}
_OSK_DEFAULT_OVERRIDES = {
    # Shoulder buttons stand in for the missing triggers.
    "nso_snes": {"shift": "l1", "enter": "r1"},
    "nso_genesis": {"shift": "l1", "enter": "r1"},
    # N64: Z is on the left trigger cid, so only Enter needs re-homing; it has
    # no X/Y face buttons for Space/Backspace.
    "nso_n64": {"enter": "r1"},
    # A NES pad is two buttons and Start/Select  A and B are already the OSK's
    # own select/back, so there is genuinely nothing left to assign. Left
    # explicitly unbound (visible in the dropdowns) rather than pointing at
    # controls that silently do nothing.
    "nso_nes": {"shift": "none", "enter": "none", "space": "none",
                "backspace": "none"},
}


def osk_default_button(kind, func):
    """Default control id for one OSK function on this controller kind.

    Guaranteed never to name a control the pad lacks: an override wins, then
    the base default, and anything still absent degrades to "none" so a future
    kind can't inherit a dead binding by accident."""
    kind = canon(kind) or kind
    cid = _OSK_DEFAULT_OVERRIDES.get(kind, {}).get(
        func, _OSK_DEFAULT_BUTTONS.get(func, "none"))
    if cid != "none" and cid in absent_controls(kind):
        return "none"
    return cid


def osk_default_buttons(kind):
    """The whole default OSK function → button map for a kind."""
    return {f: osk_default_button(kind, f) for f in _OSK_DEFAULT_BUTTONS}


# --- Desktop-tab default overrides -------------------------------------------
# The shared SDL desktop defaults assume a two-stick pad: left stick = arrow
# keys, right stick = mouse cursor. A lone Joy-Con has exactly ONE stick, and
# SDL reports it as the LEFT one  so the stock defaults put arrows on the only
# stick it has and the cursor on a stick that does not exist, leaving the
# controller unable to move the mouse at all, which is the app's whole job on
# the desktop. Point its single stick at the cursor instead.
#
# Nothing is displaced by this: a sideways Joy-Con has no D-pad either (its
# direction buttons ARE the face buttons), and the on-screen keyboard navigates
# by stick through adusk's own handler, independent of these binds.
_JOYCON_SINGLE_STICK = {
    "lstick_up": "joystick_mouse", "lstick_down": "joystick_mouse",
    "lstick_left": "joystick_mouse", "lstick_right": "joystick_mouse",
}
DESKTOP_DEFAULT_OVERRIDES = {
    "joycon_l": _JOYCON_SINGLE_STICK,
    "joycon_r": _JOYCON_SINGLE_STICK,
    "joycon2_l": _JOYCON_SINGLE_STICK,
    "joycon2_r": _JOYCON_SINGLE_STICK,
}


def desktop_defaults(kind, base):
    """`base` (keybinds_runtime.SDL_DESKTOP_DEFAULTS) with this kind's
    overrides applied, or `base` itself when it has none. The catalog owns the
    override so the picker's layout tabs and the live runtime agree on what
    "default" means for a controller."""
    over = DESKTOP_DEFAULT_OVERRIDES.get(canon(kind) or "")
    if not over:
        return base
    merged = dict(base)
    merged.update(over)
    return merged


def osk_button_label(kind, cid):
    """Dropdown label for an OSK-function control id, per controller scheme."""
    if cid == "none":
        return "None"
    base = {"l3": "Left Stick Click", "r3": "Right Stick Click",
            "dpad_up": "DPad Up", "dpad_down": "DPad Down",
            "dpad_left": "DPad Left", "dpad_right": "DPad Right"}
    if cid in base:
        return base[cid]
    lbl = label_for(kind, cid, cid.upper())
    if cid == "start":
        return "Start (%s)" % lbl if lbl not in ("Menu", "Options", "+") else lbl
    if cid == "back":
        return "Back (%s)" % lbl if lbl not in ("View", "Share", "Create", "–") else lbl
    return lbl


# --- Identification ----------------------------------------------------------

_VID_NINTENDO = 0x057E
_VID_SONY = 0x054C
_VID_MICROSOFT = 0x045E
_VID_VALVE = 0x28DE
_VID_ASUS = 0x0B05
_VID_LENOVO = 0x17EF
_VID_MSI = 0x0DB0
_VID_GPD = 0x2F24
_VID_8BITDO = 0x2DC8

# Valve's controller product ids (mirrors steamcontroller.HID_KIND_PIDS, kept
# here so identify() can classify an SDL-visible Valve pad without importing
# the hidapi driver).
_VALVE_PIDS = {
    0x1102: "sc2015",      # Steam Controller (2015), wired USB
    0x1142: "sc2015",      # Steam Controller (2015), wireless receiver
    0x1205: "steam_deck",  # Steam Deck built-in pad
    0x1302: "sc",          # Steam Controller (2026), wired
    0x1304: "sc",          # Steam Controller (2026), wireless dongle
}

_SONY_PS3_PIDS = {0x0268}
_SONY_PS4_PIDS = {0x05C4, 0x09CC, 0x0BA0}
_SONY_PS5_PIDS = {0x0CE6, 0x0DF2}

# SDL_GamepadType values  a coarse fallback when VID/name are opaque.
#
# These are the ACTUAL SDL3 enum values, read back out of the shipped SDL3.dll
# with SDL_GetGamepadStringForType(0..15) rather than assumed. The previous
# table was shifted by one against this (it had PS5 where SWITCH_PRO is, so a
# Switch Pro whose VID/name a backend failed to report was identified as a
# DualSense); it never surfaced because Nintendo pads always carry VID 0x057E.
_SDL_TYPE_TO_KIND = {
    2: "xbox",         # SDL_GAMEPAD_TYPE_XBOX360
    3: "xbox",         # SDL_GAMEPAD_TYPE_XBOXONE
    4: "ps4",          # SDL_GAMEPAD_TYPE_PS3 (no PS3 kind  closest is ps4)
    5: "ps4",          # SDL_GAMEPAD_TYPE_PS4
    6: "ps5",          # SDL_GAMEPAD_TYPE_PS5
    7: "switch",       # SDL_GAMEPAD_TYPE_NINTENDO_SWITCH_PRO
    8: "joycon_l",     # ..._NINTENDO_SWITCH_JOYCON_LEFT
    9: "joycon_r",     # ..._NINTENDO_SWITCH_JOYCON_RIGHT
    10: "joycon_pair",  # ..._NINTENDO_SWITCH_JOYCON_PAIR
    11: "gamecube",    # SDL_GAMEPAD_TYPE_GAMECUBE
}

# Nintendo USB product ids. Switch 1 pads and the NSO retro pads share the
# Joy-Con/Pro ids (the retro pads ARE Joy-Con-class devices), so the PID alone
# can't tell an NSO SNES pad from a Joy-Con  the NAME does that, and is
# consulted first. Switch 2's ids are its own block.
_NINTENDO_PIDS = {
    0x2006: "joycon_l",     # Joy-Con (L)
    0x2007: "joycon_r",     # Joy-Con (R)
    0x2009: "switch",       # Switch Pro Controller
    0x200E: "joycon_pair",  # Joy-Con Charging Grip (both halves over USB)
    0x2017: "nso_snes",     # NSO SNES controller
    0x2019: "nso_n64",      # NSO N64 controller
    0x201E: "nso_genesis",  # NSO Mega Drive / Genesis controller
    0x0337: "gamecube",     # WUP-028 GameCube adapter
    # Switch 2 (Bluetooth is BLE/GATT  see the KINDS comment; these ids are
    # what they enumerate as over USB-C).
    0x2066: "joycon2_l",
    0x2067: "joycon2_r",
    0x2069: "switch2",
    0x2073: "gamecube",     # NSO GameCube controller for Switch 2
}

# Matched against the SDL device name, most specific first. SDL names Nintendo
# devices exactly like this ("Nintendo Switch Joy-Con (L)", "Nintendo SNES
# Controller", ...), which is how the retro pads are told apart from the
# Joy-Cons they share product ids with.
_NINTENDO_NAME_HINTS = (
    ("snes", "nso_snes"),
    # SDL spells the Famicom pads "Nintendo Family Computer Controller (1)/(2)"
    # and the NES ones "Nintendo NES Controller (L)/(R)". Both share the
    # Joy-Con product ids, so this name match is the ONLY thing that keeps them
    # off the Joy-Con pages.
    ("family computer", "nso_nes"), ("famicom", "nso_nes"),
    ("nes controller", "nso_nes"),
    ("n64", "nso_n64"),
    ("genesis", "nso_genesis"), ("mega drive", "nso_genesis"),
    ("gamecube", "gamecube"), ("game cube", "gamecube"),
    # Switch 2 names, as reported by the drivers/bridges that can reach them.
    ("joy-con 2 (l", "joycon2_l"), ("joy-con 2 (r", "joycon2_r"),
    ("joy-con 2", "joycon2_pair"),
    ("pro controller 2", "switch2"), ("switch 2 pro", "switch2"),
    ("joy-con (l", "joycon_l"), ("joy-con (r", "joycon_r"),
    ("joy-con pair", "joycon_pair"), ("joy-cons", "joycon_pair"),
    ("pro controller", "switch"),
)


# Substrings that mark a device as Nintendo's when the VID doesn't (a BLE
# bridge, an adapter, a virtual pad). Deliberately narrow  "genesis" or "snes"
# alone would also match third-party retro pads, which are not Nintendo's and
# must not inherit the Nintendo Bluetooth guard.
_NINTENDO_NAME_MARKERS = ("nintendo", "joy-con", "joycon", "switch pro",
                          "pro controller 2", "switch 2")


def _identify_nintendo(name, pid, sdl_type):
    """Which Nintendo controller this is. Name first (it is the only thing that
    separates the NSO retro pads from the Joy-Con ids they reuse), then the
    product id, then SDL's own gamepad type, and finally the Switch Pro  the
    historical default for an unrecognised Nintendo pad."""
    n = (name or "").lower()
    for sub, kind in _NINTENDO_NAME_HINTS:
        if sub in n:
            return kind
    kind = _NINTENDO_PIDS.get(int(pid or 0))
    if kind:
        return kind
    kind = _SDL_TYPE_TO_KIND.get(int(sdl_type or 0))
    if kind in NINTENDO_KINDS:
        return kind
    return "switch"

_NAME_HINTS = (
    ("steam deck", "steam_deck"),
    ("dualsense", "ps5"), ("ps5", "ps5"),
    ("dualshock", "ps4"), ("ps4", "ps4"), ("ps3", "ps4"),
    # Nintendo names are resolved by _identify_nintendo (which kind exactly),
    # not by this coarse table  see the marker check in identify().
    ("rog ally", "rog_ally"), ("ally", "rog_ally"),
    ("legion", "legion_go"),
    ("claw", "msi_claw"),
    ("gpd", "gpd_win"),
    ("onexplayer", "onexplayer"), ("one xplayer", "onexplayer"),
    ("ayaneo", "ayaneo"), ("aya neo", "ayaneo"),
    ("8bitdo", "8bitdo"),
    ("xbox", "xbox"), ("xinput", "xbox"),
)


def identify(name=None, vid=0, pid=0, sdl_type=0, machine=None):
    """Best-effort kind for a detected SDL pad. `machine` (see machine_kind())
    reclassifies an anonymous XInput/Xbox pad as the handheld's own built-in
    controller when the app is running ON that handheld."""
    n = (name or "").lower()
    vid = int(vid or 0)
    pid = int(pid or 0)

    kind = None
    if vid == _VID_NINTENDO:
        kind = _identify_nintendo(name, pid, sdl_type)
    elif vid == _VID_SONY:
        if pid in _SONY_PS5_PIDS:
            kind = "ps5"
        elif pid in _SONY_PS4_PIDS or pid in _SONY_PS3_PIDS:
            kind = "ps4"
        else:
            kind = "ps5" if "dualsense" in n else "ps4"
    elif vid == _VID_VALVE:
        # Valve's own product ids decide which Steam Controller this is; the
        # name is only consulted when a PID we don't know shows up (a virtual
        # pad, an adapter). 0x1102 = the 2015 controller over USB, 0x1142 =
        # its wireless receiver.
        kind = _VALVE_PIDS.get(pid)
        if kind is None:
            if "deck" in n:
                kind = "steam_deck"
            elif "2015" in n or "steam controller" == n.strip():
                # SDL names the original exactly "Steam Controller"; the 2026
                # unit reports its own model name.
                kind = "sc2015"
            else:
                kind = "sc"
    elif vid == _VID_ASUS:
        kind = "rog_ally"
    elif vid == _VID_LENOVO:
        kind = "legion_go"
    elif vid == _VID_MSI:
        kind = "msi_claw"
    elif vid == _VID_GPD:
        kind = "gpd_win"
    elif vid == _VID_8BITDO:
        kind = "8bitdo"
    elif vid == _VID_MICROSOFT:
        kind = "xbox"

    # A Nintendo pad that doesn't carry Nintendo's VID: a Linux BLE bridge for
    # the Switch 2 pads, a wireless adapter that re-presents the controller, a
    # uinput/virtual device. The name still says what it is.
    if kind is None and any(m in n for m in _NINTENDO_NAME_MARKERS):
        kind = _identify_nintendo(name, pid, sdl_type)
    if kind is None:
        for sub, k in _NAME_HINTS:
            if sub in n:
                kind = k
                break
    if kind is None:
        kind = _SDL_TYPE_TO_KIND.get(int(sdl_type or 0), "generic")

    # On a handheld PC, the built-in pad presents as a plain Xbox/XInput
    # device  attribute it to the machine so ITS category unlocks. NOT on a
    # Steam Deck: its built-in pad never presents as XInput on its own (that
    # was Steam Input's doing) and is driven by the dedicated steamcontroller
    # HID backend  an Xbox pad plugged into a Deck must stay "xbox".
    if machine and machine != "steam_deck" and kind in ("xbox", "generic"):
        kind = machine
    return kind


_HANDHELD_KINDS = ("rog_ally", "legion_go", "msi_claw", "gpd_win",
                   "onexplayer", "ayaneo", "steam_deck")

_machine_kind_cache = ("unset",)


def machine_kind():
    """The handheld kind this machine IS (its built-in controller), or None on
    a regular PC. Windows: BIOS SystemProductName/Manufacturer from the
    registry (no WMI). Linux: DMI sysfs. Cached after the first call."""
    global _machine_kind_cache
    if _machine_kind_cache != ("unset",):
        return _machine_kind_cache[0]
    manuf = product = family = ""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\BIOS") as k:
                def _val(name):
                    try:
                        return str(winreg.QueryValueEx(k, name)[0])
                    except OSError:
                        return ""
                manuf = _val("SystemManufacturer")
                product = _val("SystemProductName")
                family = _val("SystemFamily")
        else:
            def _dmi(name):
                try:
                    with open("/sys/class/dmi/id/" + name, encoding="utf-8",
                              errors="replace") as f:
                        return f.read().strip()
                except OSError:
                    return ""
            manuf = _dmi("sys_vendor")
            product = _dmi("product_name")
            family = _dmi("product_family")
    except Exception:
        pass
    blob = " ".join((manuf, product, family)).lower()
    kind = None
    if "rog ally" in blob or "rc71" in blob or "rc72" in blob:
        kind = "rog_ally"
    elif "legion go" in blob or ("lenovo" in blob and any(
            m in blob for m in ("83e1", "83l3", "83n6", "83q2", "83q3", "8apu1"))):
        kind = "legion_go"
    elif "claw" in blob and ("micro-star" in blob or "msi" in blob):
        kind = "msi_claw"
    elif "gpd" in blob and ("win" in blob or product.lower().startswith("g16")):
        kind = "gpd_win"
    elif "onexplayer" in blob or "one xplayer" in blob or "one-netbook" in blob:
        kind = "onexplayer"
    elif "ayaneo" in blob or "aya neo" in blob or "ayadevice" in blob:
        kind = "ayaneo"
    elif "valve" in blob or "jupiter" in blob or "galileo" in blob:
        kind = "steam_deck"
    _machine_kind_cache = (kind,)
    return kind


_ALWAYS_UNLOCKED = ("sc", "steam_deck", "xbox", "ps5", "switch2")


def unlocked_kinds(seen):
    """Ordered kinds whose picker tab / Options category are unlocked: the
    (2026) Steam Controller, the Steam Deck, Xbox, DualSense and the Switch 2
    Pro Controller always, plus every kind in `seen`
    (settings["seen_controllers"])  which is where the 2015 Steam Controller,
    a DualShock 4, or anything else shows up once it's actually been detected."""
    seen_set = {canon(k) for k in (seen or ())}
    return tuple(k for k in KINDS
                 if k in _ALWAYS_UNLOCKED or k in seen_set)
