# -*- coding: utf-8 -*-
"""Resolve keybind action values (from the keybinds picker) into concrete
desktop emits for _SdlDesktopController.

Single source of truth shared by the picker and the runtime so the option ids
never drift. Import-safe: no tkinter, no SDL  and sui.Keys / SCButtons are
passed IN rather than imported, so this module stays free of the steamcontroller
package and is cheap to import from the tray at startup.

Scope: only the Switch Pro DESKTOP controller's cleanly-bindable controls
(triggers, stick clicks, D-pad, A/B/Y). The Steam Controller's desktop is its
firmware lizard mode (we only toggle it), and the on-screen-keyboard typing path
is deliberately NOT remappable (its buttons ARE the keyboard). See the
keybinds-picker / linux-tray notes."""

# Mouse-button actions -> sui.Mouse button name. sui.Mouse supports only
# left / right / middle, so mouse4 / mouse5 have no desktop emit here (a control
# bound to them simply does nothing).
_CLICK = {
    "mouse_left": "left",
    "mouse_right": "right",
    "mouse_middle": "middle",
}

# Actions that aren't a plain key or click (no desktop emit in this layer).
_NON_KEY = {"none", "as_mouse", "joystick_mouse", "show_keyboard",
            "scroll_up", "scroll_down", "toggle_magnifier", "trackpad_mouse"}

# Named (non-alphanumeric) keyboard actions -> sui.Keys attribute name.
_NAMED_KEYS = {
    "enter": "KEY_ENTER", "space": "KEY_SPACE", "escape": "KEY_ESC",
    "tab": "KEY_TAB", "backspace": "KEY_BACKSPACE", "delete": "KEY_DELETE",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "ctrl": "KEY_LEFTCTRL", "shift": "KEY_LEFTSHIFT", "alt": "KEY_LEFTALT",
    "win": "KEY_LEFTMETA",
    "pageup": "KEY_PAGEUP", "pagedown": "KEY_PAGEDOWN",
    "home": "KEY_HOME", "end": "KEY_END",
    "print_screen": "KEY_SYSRQ",
    # Media / volume transport  resolve straight to media keycodes so they work
    # in BOTH the Desktop and Chords tabs via the normal tap path.
    "media_playpause": "KEY_PLAYPAUSE", "media_next": "KEY_NEXTSONG",
    "media_prev": "KEY_PREVIOUSSONG", "volume_up": "KEY_VOLUMEUP",
    "volume_down": "KEY_VOLUMEDOWN", "volume_mute": "KEY_MUTE",
    # Punctuation / special keys (also importable from community configs).
    "comma": "KEY_COMMA", "period": "KEY_DOT", "slash": "KEY_SLASH",
    "backslash": "KEY_BACKSLASH", "semicolon": "KEY_SEMICOLON",
    "quote": "KEY_APOSTROPHE", "lbracket": "KEY_LEFTBRACE",
    "rbracket": "KEY_RIGHTBRACE", "minus": "KEY_MINUS",
    "equals": "KEY_EQUAL", "backtick": "KEY_GRAVE",
    "capslock": "KEY_CAPSLOCK", "insert": "KEY_INSERT",
    # Numpad block (kernel-style KEY_KP* names; both uinput shims carry them).
    "kp_plus": "KEY_KPPLUS", "kp_minus": "KEY_KPMINUS",
    "kp_multiply": "KEY_KPASTERISK", "kp_divide": "KEY_KPSLASH",
    "kp_period": "KEY_KPDOT", "kp_enter": "KEY_KPENTER",
    "kp_0": "KEY_KP0", "kp_1": "KEY_KP1", "kp_2": "KEY_KP2",
    "kp_3": "KEY_KP3", "kp_4": "KEY_KP4", "kp_5": "KEY_KP5",
    "kp_6": "KEY_KP6", "kp_7": "KEY_KP7", "kp_8": "KEY_KP8",
    "kp_9": "KEY_KP9",
    # Legacy guide-mode key aliases (kept so old saved configs still resolve).
    "key_tab": "KEY_TAB", "key_escape": "KEY_ESC", "key_enter": "KEY_ENTER",
}

# Switch Pro DESKTOP defaults  the built-in behavior of _SdlDesktopController's
# cleanly-bindable controls. MUST match keybinds_picker's "switch" layout
# defaults. An empty/unset bind for a control falls back to these, so the
# default map reproduces the original hardcoded tables exactly.
SWITCH_DEFAULTS = {
    "zl": "mouse_right", "zr": "mouse_left",   # ZL/ZR triggers (LT/RT)
    "l3": "mouse_middle", "r3": "none",        # stick clicks
    "dpad_up": "up", "dpad_down": "down",
    "dpad_left": "left", "dpad_right": "right",
    "a": "enter", "b": "escape", "y": "space",  # face buttons (SDL positions)
}


def pc_submap(kind_binds):
    """Extract the PC-mode control->action map from one controller's saved
    binds. Accepts the new nested {"pc":{...},"gamepad":{...}} shape and the
    legacy flat {control: action} shape (which predated modes and was PC-only).
    Gamepad-mode binds are deliberately ignored here  only PC mode applies to
    the desktop controller. Always returns a dict."""
    if not isinstance(kind_binds, dict):
        return {}
    if "pc" in kind_binds or "gamepad" in kind_binds:
        pc = kind_binds.get("pc")
        return pc if isinstance(pc, dict) else {}
    return kind_binds  # legacy flat = PC mode


def click_for(action):
    """Mouse button name for a click action, or None if it isn't a (supported)
    click."""
    return _CLICK.get(action)


def key_for(action, Keys):
    """sui.Keys.* constant for a keyboard action, or None. `Keys` is sui.Keys,
    passed in so this module needn't import steamcontroller. Built defensively
    with getattr, so a key the platform lacks just yields None (the control does
    nothing) instead of raising."""
    if not action or action in _CLICK or action in _NON_KEY:
        return None
    attr = _NAMED_KEYS.get(action)
    if attr is None:
        if len(action) == 1 and action.isalnum():       # a-z, 0-9
            attr = "KEY_" + action.upper()
        elif action.startswith("f") and action[1:].isdigit():  # f1-f12
            attr = "KEY_" + action.upper()
    if attr is None:
        return None
    key = getattr(Keys, attr, None)
    if key is None and attr == "KEY_LEFTMETA":  # platform naming varies
        key = getattr(Keys, "KEY_LEFTWIN", None) or getattr(Keys, "KEY_LWIN", None)
    return key


# --- Two-button desktop chords (Steam Controller) ---------------------------
# Control id -> SCButtons attribute name, for the chord editor + runtime. These
# are the buttons selectable as a chord component (digital buttons only; the
# Steam/QAM button is reserved for the built-in Steam chords). Shared so the
# picker's dropdowns and tray's _Watcher agree on ids.
SC_CHORD_BUTTONS = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "l1": "LB", "r1": "RB", "l2": "LT", "r2": "RT",
    "l4": "LGRIP1", "l5": "LGRIP2", "r4": "RGRIP1", "r5": "RGRIP2",
    "l3": "L3", "r3": "R3", "start": "START", "back": "VIEW",
    # "..." (Quick Access)  its own bit, distinct from STEAM, so it can be a
    # plain chord button in the desktop (Hotkeys) path. In GAMEPAD mode the tray
    # still treats it as the Guide button; in desktop mode it's independent.
    "qam": "QAM",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
    "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
    "lpad": "LPAD", "rpad": "RPAD",
    # Touch sensors (also on the Desktop page)  real button bits, so usable in
    # chords. lg/rg are NOT here: no distinct hardware bit on this firmware.
    "lstick_touch": "LPADJOY_TOUCH", "rstick_touch": "RPADJOY_TOUCH",
    "lgrip_touch": "LGRIP_REST", "rgrip_touch": "RGRIP_REST",
    # Trackpad TOUCH (thumb resting, no click)  the classic Steam Input
    # "gyro while touching the pad" gate; imported configs land on these.
    "lpad_touch": "LPADTOUCH", "rpad_touch": "RPADTOUCH",
    # Gamepad-Layout ("XInput output") aliases  same SCButtons bit as the
    # physical control that produces this XInput output BY DEFAULT (see
    # SC_GAMEPAD_DEFAULTS), so a Hotkeys chord can be built by Xbox-style
    # identity instead of physical name. These always track the DEFAULT
    # gamepad-mode wiring, not a live per-control remap in the Gamepad
    # Layout tab.
    "xi_a": "A", "xi_b": "B", "xi_x": "X", "xi_y": "Y",
    "xi_lb": "LB", "xi_rb": "RB", "xi_lt": "LT", "xi_rt": "RT",
    "xi_ls": "L3", "xi_rs": "R3",
    "xi_back": "START", "xi_start": "VIEW",
    "xi_dpad_up": "DPAD_UP", "xi_dpad_down": "DPAD_DOWN",
    "xi_dpad_left": "DPAD_LEFT", "xi_dpad_right": "DPAD_RIGHT",
}

# Id used in the chord dropdowns for the Guide button (Steam / "..." on the SC,
# Home on a Switch Pro). It is NOT in SC_CHORD_BUTTONS: a Guide+button chord is
# the same gesture as the Chords tab (Steam HELD + button), so it's fired by the
# Steam-held path (build_guide_chords)  never by build_chords (which is gated to
# "Steam not held"). Kept out of SC_CHORD_BUTTONS so build_chords skips it.
CHORD_GUIDE_ID = "guide"

# --- Chord-button dropdown vocabulary ----------------------------------------
# Both label lists below are ordered by PHYSICAL REGION, and the picker paints
# one rail color per region (keybinds_picker._CHORD_BTN_GROUPS / _chord_color_fn),
# so a dropdown reads as coloured blocks rather than one long list:
#
#   meta -> face -> bumpers+triggers -> paddles/grips -> sticks -> dpad ->
#   trackpads -> XInput outputs
#
# The "xi_" output aliases go LAST: they're the specialist block (they only fire
# in Gamepad Mode), and keeping them off the top means the physical face buttons
# are near the start of the list where people look for them.
CHORD_GROUP_ORDER = ("meta", "face", "shoulder", "paddle", "stick", "dpad",
                     "pad", "output")

# Chord-button id -> region key, for every id either label list can hold. Used
# by the picker for the rail colors; kept here so ids/labels/colors share one
# source and can't drift.
CHORD_BUTTON_GROUP = {
    CHORD_GUIDE_ID: "meta",
    "back": "meta", "start": "meta", "qam": "meta",
    "minus": "meta", "plus": "meta", "home": "meta", "capture": "meta",
    "a": "face", "b": "face", "x": "face", "y": "face",
    "l1": "shoulder", "r1": "shoulder", "l2": "shoulder", "r2": "shoulder",
    "l": "shoulder", "r": "shoulder", "zl": "shoulder", "zr": "shoulder",
    "l4": "paddle", "l5": "paddle", "r4": "paddle", "r5": "paddle",
    "lgrip_touch": "paddle", "rgrip_touch": "paddle",
    "l3": "stick", "r3": "stick",
    "lstick_touch": "stick", "rstick_touch": "stick",
    "dpad_up": "dpad", "dpad_down": "dpad",
    "dpad_left": "dpad", "dpad_right": "dpad",
    "lpad": "pad", "rpad": "pad",
    "lpad_touch": "pad", "rpad_touch": "pad", "touchpad": "pad",
}


def chord_button_group(cid):
    """Region key for a chord-button id ("meta"/"face"/.../"output")."""
    if cid and cid.startswith("xi_"):
        return "output"
    return CHORD_BUTTON_GROUP.get(cid, "face")


# Human labels for the chord button dropdowns (picker). Order is the dropdown
# order. Kept here next to the id map so the two never drift. "Guide" is first so
# it's easy to find; it pairs with another button to make a Steam-held chord.
SC_CHORD_BUTTON_LABELS = [
    # meta
    (CHORD_GUIDE_ID, "Guide"),
    ("back", "View"), ("start", "Menu"), ("qam", "QAM"),
    # face
    ("a", "A"), ("b", "B"), ("x", "X"), ("y", "Y"),
    # bumpers + triggers
    ("l1", "L1"), ("r1", "R1"), ("l2", "L2"), ("r2", "R2"),
    # rear paddles + grip-rest sensors
    ("l4", "L4"), ("l5", "L5"), ("r4", "R4"), ("r5", "R5"),
    ("lgrip_touch", "Gripsense Left"),
    ("rgrip_touch", "Gripsense Right"),
    # sticks
    ("l3", "Left Stick Click"), ("r3", "Right Stick Click"),
    ("lstick_touch", "Left Capacitive Touch Thumbstick"),
    ("rstick_touch", "Right Capacitive Touch Thumbstick"),
    # dpad
    ("dpad_up", "DPad Up"), ("dpad_down", "DPad Down"),
    ("dpad_left", "DPad Left"), ("dpad_right", "DPad Right"),
    # trackpads
    ("lpad", "Left Touchpad Click"), ("rpad", "Right Touchpad Click"),
    ("lpad_touch", "Left Touchpad Touch"),
    ("rpad_touch", "Right Touchpad Touch"),
    # Gamepad-Layout ("XInput output") aliases  default-wiring names, not a
    # live remap tracker (see SC_CHORD_BUTTONS above). The 10 that share a
    # label with a physical control above are suffixed " (Output)" so the
    # dropdown never shows two rows with the same text.
    ("xi_a", "A (Output)"), ("xi_b", "B (Output)"),
    ("xi_x", "X (Output)"), ("xi_y", "Y (Output)"),
    ("xi_lb", "Left Bumper"), ("xi_rb", "Right Bumper"),
    ("xi_lt", "Left Trigger"), ("xi_rt", "Right Trigger"),
    ("xi_ls", "Left Stick Click (Output)"), ("xi_rs", "Right Stick Click (Output)"),
    ("xi_back", "Select"), ("xi_start", "Start"),
    ("xi_dpad_up", "DPad Up (Output)"), ("xi_dpad_down", "DPad Down (Output)"),
    ("xi_dpad_left", "DPad Left (Output)"), ("xi_dpad_right", "DPad Right (Output)"),
]

# SDL-template chord buttons  the vocabulary every non-SC kind draws from. The
# core rows are on EVERY pad; the paddle/touchpad rows are filtered per kind by
# pads.CHORD_EXTRA (and pads.CHORD_SKIP drops what a kind lacks), so a plain
# Xbox pad never offers Elite paddles and only the PlayStation/Legion pads offer
# a touchpad click. Labels here are the neutral defaults 
# keybinds_picker._sdl_chord_labels swaps in each kind's printed names (ZL -> LT
# -> L2, P1 -> M1 -> L4, ...).
SDL_CHORD_BUTTON_LABELS = [
    # meta
    (CHORD_GUIDE_ID, "Guide"),
    ("minus", "Minus (-)"), ("plus", "Plus (+)"),
    ("home", "Home"), ("capture", "Capture"),
    # face
    ("a", "A"), ("b", "B"), ("x", "X"), ("y", "Y"),
    # bumpers + triggers
    ("l", "L"), ("r", "R"), ("zl", "ZL"), ("zr", "ZR"),
    # rear paddles (Elite P1-P4 / Ally M1-M2 / Edge back buttons / Joy-Con SL-SR)
    ("l4", "Left Paddle 1"), ("l5", "Left Paddle 2"),
    ("r4", "Right Paddle 1"), ("r5", "Right Paddle 2"),
    # sticks
    ("l3", "Left Stick Click"), ("r3", "Right Stick Click"),
    # dpad
    ("dpad_up", "DPad Up"), ("dpad_down", "DPad Down"),
    ("dpad_left", "DPad Left"), ("dpad_right", "DPad Right"),
    # touchpad click (DualShock 4 / DualSense / Legion Go)
    ("touchpad", "Touchpad Click"),
    # XInput output aliases  same idea as the SC's (see above): they track this
    # kind's DEFAULT gamepad wiring (SDL_GAMEPAD_DEFAULTS) so a Hotkey can be
    # built by Xbox identity and fire in GAMEPAD mode instead of desktop mode.
    ("xi_a", "A (Output)"), ("xi_b", "B (Output)"),
    ("xi_x", "X (Output)"), ("xi_y", "Y (Output)"),
    ("xi_lb", "Left Bumper"), ("xi_rb", "Right Bumper"),
    ("xi_lt", "Left Trigger"), ("xi_rt", "Right Trigger"),
    ("xi_ls", "Left Stick Click (Output)"), ("xi_rs", "Right Stick Click (Output)"),
    ("xi_back", "Select"), ("xi_start", "Start"),
    ("xi_dpad_up", "DPad Up (Output)"), ("xi_dpad_down", "DPad Down (Output)"),
    ("xi_dpad_left", "DPad Left (Output)"), ("xi_dpad_right", "DPad Right (Output)"),
]

# Historical name (this list was Switch-Pro-only before every SDL kind shared it).
SWITCH_CHORD_BUTTON_LABELS = SDL_CHORD_BUTTON_LABELS


# SDL-template controllers (every non-SC kind  Switch/Xbox/PS/handhelds share
# the Switch's control-id space): chord-button id -> SCButtons attribute name.
# Mirrors SC_CHORD_BUTTONS for the SDL cids so the Hotkeys editor + the SDL
# desktop runtime agree. "home" is the Guide/Steam bit itself (a Home+X chord is
# also expressible as guide+X; both are honored); "capture" is the spare QAM bit
# (SDL MISC1  the Switch Capture / DualSense mute / handheld extra button).
SDL_CHORD_BUTTONS = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "l": "LB", "r": "RB", "zl": "LT", "zr": "RT",
    "l3": "L3", "r3": "R3",
    "minus": "VIEW", "plus": "START",
    "home": "STEAM", "capture": "QAM",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
    "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
    # Rear paddles: adusk.inputsrc._SDL_TO_SC folds SDL's four paddle buttons
    # onto the SC's grip bits, so they're real frame bits on any pad that has
    # them (Xbox Elite, DualSense Edge, handheld M-buttons, Joy-Con SL/SR).
    "l4": "LGRIP1", "l5": "LGRIP2", "r4": "RGRIP1", "r5": "RGRIP2",
    # Touchpad CLICK (SDL_GAMEPAD_BUTTON_TOUCHPAD) -> the otherwise-unused-on-SDL
    # left trackpad bit. Distinct from "capture"/MISC1, which is the mute/share
    # button on the pads that have one.
    "touchpad": "LPAD",
    # Gamepad-Layout ("XInput output") aliases  the SDL twin of the SC's xi_
    # block. Each maps to the SCButtons bit of the control that produces that
    # Xbox output BY DEFAULT (see SDL_GAMEPAD_DEFAULTS), so a chord built from
    # them is Gamepad-Mode-scoped (build_chords sets is_gamepad when EVERY
    # button is an xi_ alias).
    "xi_a": "A", "xi_b": "B", "xi_x": "X", "xi_y": "Y",
    "xi_lb": "LB", "xi_rb": "RB", "xi_lt": "LT", "xi_rt": "RT",
    "xi_ls": "L3", "xi_rs": "R3",
    "xi_back": "VIEW", "xi_start": "START",
    "xi_dpad_up": "DPAD_UP", "xi_dpad_down": "DPAD_DOWN",
    "xi_dpad_left": "DPAD_LEFT", "xi_dpad_right": "DPAD_RIGHT",
}


# Controller kinds that speak the Steam Controller's control-id space (they
# run the HID takeover runtime, so their frames arrive as SCButtons bits).
# Spelled out rather than imported from pads.py: this module is deliberately
# import-free so the tray can pull it in cheaply at startup.
SC_ID_SPACE_KINDS = ("sc", "sc2015", "steam_deck")


def chord_buttons_for(kind):
    """The chord-button id map for a controller kind. The HID-takeover kinds
    (both Steam Controllers and the trackpad-parity Steam Deck) share the SC id
    space; every SDL-backed kind shares the SDL template's."""
    return (SC_CHORD_BUTTONS if kind in SC_ID_SPACE_KINDS
            else SDL_CHORD_BUTTONS)


def chords_for(chords, kind="sc"):
    """Return the chord list for a controller. Accepts the per-controller dict
    {"sc":[...], "switch":[...]} OR the legacy FLAT list (treated as the SC list).
    Unknown kinds get []."""
    if isinstance(chords, dict):
        return chords.get(kind) or []
    return list(chords or []) if kind == "sc" else []

# Aliases accepted when parsing a typed key combo ("ctrl+alt+i").
_CHORD_KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "cmd": "win", "super": "win",
    "meta": "win", "windows": "win", "return": "enter", "esc": "escape",
    "del": "delete", "pgup": "pageup", "pgdn": "pagedown",
    # Numpad display names (see chord_keys_label)  no '+' allowed in a
    # display token, so NumPlus stands in for the plus key.
    "numplus": "kp_plus", "numminus": "kp_minus", "nummul": "kp_multiply",
    "numdiv": "kp_divide", "numdot": "kp_period", "numenter": "kp_enter",
}
_CHORD_KEY_ALIASES.update({"num%d" % _n: "kp_%d" % _n for _n in range(10)})

# Token -> display name where .capitalize() would be ugly (numpad keys).
_CHORD_KEY_PRETTY = {
    "kp_plus": "NumPlus", "kp_minus": "NumMinus", "kp_multiply": "NumMul",
    "kp_divide": "NumDiv", "kp_period": "NumDot", "kp_enter": "NumEnter",
}
_CHORD_KEY_PRETTY.update({"kp_%d" % _n: "Num%d" % _n for _n in range(10)})
# Modifier tokens, emitted first (so the combo presses modifiers before the key).
_CHORD_MODS = ("ctrl", "shift", "alt", "win")


def normalize_chord_keys(text):
    """Parse a typed key combo like 'Ctrl + Alt + I' into a normalized token
    list ['ctrl','alt','i'] (lowercased, aliased, de-duplicated, modifiers
    first). Used by the picker to store the chord and is round-trip stable."""
    toks = []
    for raw in str(text).replace(" ", "").split("+"):
        if not raw:
            continue
        t = raw.lower()
        t = _CHORD_KEY_ALIASES.get(t, t)
        if t not in toks:
            toks.append(t)
    return [k for k in _CHORD_MODS if k in toks] + \
           [k for k in toks if k not in _CHORD_MODS]


def chord_keys_label(keys):
    """Render a stored token list back to a display string, e.g.
    ['ctrl','alt','i'] -> 'Ctrl + Alt + I'."""
    pretty = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win"}
    pretty.update(_CHORD_KEY_PRETTY)
    out = []
    for k in keys or []:
        out.append(pretty.get(k, k.upper() if len(k) == 1 else k.capitalize()))
    return " + ".join(out)


# --- Modifier-prefixed action values ("alt+f4", "ctrl+shift+a") --------------
# A per-control action value MAY carry one or more Ctrl/Shift/Alt/Win modifiers
# in front of a single base keyboard action, joined with "+", modifiers first
# (e.g. "ctrl+alt+f4"). No plain action id contains "+", so "+" unambiguously
# marks a modifier combo. Only keyboard-key bases take modifiers (the picker
# enforces this); resolve_action wraps such a value into a ("combo", ...).
def split_modifiers(value):
    """Split a stored action value into (mods, base).
    'alt+f4' -> (['alt'], 'f4');  a plain value -> ([], value)."""
    if not value or "+" not in value:
        return [], value
    parts = [p for p in value.split("+") if p]
    if not parts:
        return [], value
    mods = [p for p in parts[:-1] if p in _CHORD_MODS]
    return mods, parts[-1]


def join_modifiers(mods, base):
    """Compose (['alt','ctrl'], 'f4') -> 'ctrl+alt+f4' with modifiers ordered
    Ctrl, Shift, Alt, Win. Empty/none mods -> `base` unchanged."""
    ordered = [m for m in _CHORD_MODS if m in set(mods or ())]
    return "+".join(ordered + [base]) if ordered else base


def _parse_chord_action(ch, Keys):
    """Resolve a saved chord dict's ACTION into a runtime action, or None if
    invalid/empty:
      {"type":"keys", "keys":[sui.Key, ...]}  (modifiers first)
      {"type":"launch", "path": str, "args": str}"""
    typ = ch.get("type")
    if typ == "keys":
        keys = []
        for tok in normalize_chord_keys("+".join(ch.get("keys") or [])):
            k = key_for(tok, Keys)
            if k is not None:
                keys.append(k)
        if keys:
            return {"type": "keys", "keys": keys}
    elif typ == "launch":
        path = (ch.get("path") or "").strip()
        if path:
            return {"type": "launch", "path": path, "args": ch.get("args", "")}
    return None


def build_chords(chords, SCButtons, Keys, button_ids=None):
    """Resolve the saved `chords` list into a runtime list of
    (mask, action, is_gamepad):

      mask       = SCButtons bit1 | bit2 (both must be held to fire)
      action     = {"type":"keys", "keys":[sui.Key, ...]}  (modifiers first)
                   {"type":"launch", "path": str, "args": str}
      is_gamepad = True when EVERY button in the chord is an "xi_"-prefixed
                   Gamepad-Layout alias (see SC_CHORD_BUTTONS)  such a chord
                   only fires in Gamepad Mode. A chord built from physical
                   button ids (the common case) is desktop-only, matching the
                   pre-existing behavior. A chord mixing a physical id with an
                   "xi_" alias is treated as desktop-only (conservative:
                   "is_gamepad" requires ALL buttons to be aliases, not ANY).

    Chords with the Guide button (CHORD_GUIDE_ID) are SKIPPED here  they're the
    Steam-held gesture and are built by build_guide_chords instead. Invalid /
    incomplete entries are skipped. `SCButtons` and `Keys` are passed in to keep
    this module import-light (no steamcontroller dependency)."""
    ids = button_ids if button_ids is not None else SC_CHORD_BUTTONS
    out = []
    for ch in chords or []:
        if not isinstance(ch, dict):
            continue
        btns = ch.get("buttons") or []
        # A chord may bind ONE button ("L1") or TWO ("L1 + R1"). For a single
        # button the mask is just that bit, so it fires as a plain hotkey. Two
        # identical buttons is invalid. Guide-component chords have no SC bit, so
        # they're skipped here and handled by build_guide_chords instead.
        if len(btns) == 1:
            bit = getattr(SCButtons, ids.get(btns[0], ""), None)
            if not bit:
                continue
            mask = int(bit)
        elif len(btns) == 2 and btns[0] != btns[1]:
            bit1 = getattr(SCButtons, ids.get(btns[0], ""), None)
            bit2 = getattr(SCButtons, ids.get(btns[1], ""), None)
            if not bit1 or not bit2:
                continue
            mask = int(bit1) | int(bit2)
        else:
            continue
        action = _parse_chord_action(ch, Keys)
        if action:
            is_gamepad = all(b.startswith("xi_") for b in btns)
            out.append((mask, action, is_gamepad))
    return out


def build_guide_chords(chords, SCButtons, Keys, button_ids=None):
    """Resolve the saved key-combo / launch chords whose trigger includes the
    GUIDE (Steam / "...") button into a runtime list of (other_button_bit, action).
    Guide + one other button → (that button's bit, action), fired while Steam is
    HELD and the button is pressed (same gesture as the Chords tab). Guide ALONE →
    (0, action), fired once per Steam/"..." hold. The non-guide button must be a
    real SC digital bit. (button_combo chords are handled by build_button_combos.)"""
    ids = button_ids if button_ids is not None else SC_CHORD_BUTTONS
    out = []
    for ch in chords or []:
        if not isinstance(ch, dict):
            continue
        btns = ch.get("buttons") or []
        if CHORD_GUIDE_ID not in btns:
            continue
        if len(btns) == 1:
            bit = 0                       # Guide alone
        elif len(btns) == 2 and btns[0] != btns[1]:
            other = btns[1] if btns[0] == CHORD_GUIDE_ID else btns[0]
            b = getattr(SCButtons, ids.get(other, ""), None)
            if not b:
                continue
            bit = int(b)
        else:
            continue
        action = _parse_chord_action(ch, Keys)
        if action:
            out.append((bit, action))
    return out


def build_gamepad_toggle_masks(chords, SCButtons, button_ids=None):
    """Button masks for chords whose action is the GAMEPAD-MODE TOGGLE
    (saved type "gamepad_toggle"). Returned SEPARATELY from build_chords because
    this action must fire in BOTH desktop and gamepad mode  the normal chord
    path (build_chords / _handle_chords) is desktop-only, so it could only ever
    switch gamepad mode ON. The tray evaluates these masks every frame in both
    modes and the App latches the fire across the mode-switch watcher rebuild so
    holding the chord can't ping-pong. Each mask is one or two SC button bits
    (all required to fire). Guide-component chords use SCButtons.STEAM as the
    Guide bit (on_input reads steam_now AFTER the toggle block so clearing the
    STEAM bit also suppresses all Steam-held paths); invalid / incomplete entries
    are skipped."""
    return _build_toggle_masks(chords, "gamepad_toggle", SCButtons, button_ids)


def build_gyro_toggle_masks(chords, SCButtons, button_ids=None):
    """Button masks for chords that TOGGLE GYRO-TO-MOUSE (saved type
    "gyro_toggle"  the per-controller Options "Gyro To Mouse" hotkey bars).
    Same evaluate-every-frame / App-latched contract as the gamepad-mode
    toggle masks above, so the hotkey works in both desktop and gamepad mode."""
    return _build_toggle_masks(chords, "gyro_toggle", SCButtons, button_ids)


def _build_toggle_masks(chords, chord_type, SCButtons, button_ids=None):
    ids = button_ids if button_ids is not None else SC_CHORD_BUTTONS
    out = []
    guide_bit = int(SCButtons.STEAM)
    for ch in chords or []:
        if not isinstance(ch, dict) or ch.get("type") != chord_type:
            continue
        btns = ch.get("buttons") or []
        if len(btns) == 1:
            if btns[0] == CHORD_GUIDE_ID:
                out.append(guide_bit)
            else:
                bit = getattr(SCButtons, ids.get(btns[0], ""), None)
                if bit:
                    out.append(int(bit))
        elif len(btns) == 2 and btns[0] != btns[1]:
            if CHORD_GUIDE_ID in btns:
                other = btns[1] if btns[0] == CHORD_GUIDE_ID else btns[0]
                bit = getattr(SCButtons, ids.get(other, ""), None)
                if bit:
                    out.append(guide_bit | int(bit))
            else:
                bit1 = getattr(SCButtons, ids.get(btns[0], ""), None)
                bit2 = getattr(SCButtons, ids.get(btns[1], ""), None)
                if bit1 and bit2:
                    out.append(int(bit1) | int(bit2))
    return out


# ---- Built-in "hold ≡ to switch Desktop <-> Gamepad" gesture ---------------
# Seconds the Start / Menu / ≡ / + / Options button must be held BY ITSELF
# before the live control scheme flips. Long enough that a normal Start press
# (open the pause menu) never trips it, short enough to feel deliberate.
MODE_HOLD_SEC = 0.75

# A frame gap longer than this restarts the hold timer. The frame stream stops
# whenever the watcher is torn down, the OSK takes the pad or the app pauses
# for Steam; without this a stale press time from minutes ago would fire the
# gesture on the very first frame after the stream resumes.
_MODE_HOLD_GAP = 0.5

# Bits that do NOT count as "another button is held" for the gesture: the
# passive touch/rest sensors. A thumb parked on a trackpad or a hand simply
# gripping the controller must not cancel the hold.
_MODE_HOLD_PASSIVE = ("RPADTOUCH", "LPADTOUCH", "RPADJOY_TOUCH",
                      "LPADJOY_TOUCH", "RGRIP_REST", "LGRIP_REST")


def mode_hold_passive_mask(SCButtons):
    """Mask of the passive touch/rest bits the hold-≡ gesture ignores when
    deciding whether the button is held ALONE (see _MODE_HOLD_PASSIVE).
    SCButtons is passed in to keep this module import-light; kinds whose enum
    lacks a bit just contribute nothing."""
    mask = 0
    for name in _MODE_HOLD_PASSIVE:
        bit = getattr(SCButtons, name, None)
        if bit:
            mask |= int(bit)
    return mask


class ModeHoldGesture:
    """The built-in "hold ≡ (Start / Menu / + / Options) to switch between
    Desktop and Gamepad controls" gesture.

    This is a FIXED gesture, not a binding: there is nothing to set up, which
    is the whole point  a controller-only user has to be able to get out of
    gamepad mode without finding a keyboard or the config GUI first. The
    Hotkeys-style "Gamepad Mode Toggle" chord bars stay available as a
    user-chosen alternative.

    Pure logic  no I/O, `now` injected  so the HID watcher and the SDL pad
    loop share it verbatim (one instance each, both living on the App so the
    state survives the watcher rebuild the mode switch kicks off; a
    per-watcher instance would restart its timer and ping-pong while the
    button is still down).

    step(buttons, start_bit, now, enabled=True, passive_mask=0) ->
      (fired, mask)
        fired = True on the ONE frame the hold completes (the caller flips the
                mode); never again until the button is released.
        mask  = bits to strip from the frame. Zero until the gesture fires,
                then the Start bit for the rest of the press, so the press that
                switched modes doesn't also fire Start's own binding / leak to
                the virtual pad on the way out."""

    def __init__(self, hold_sec=MODE_HOLD_SEC):
        self._hold = hold_sec
        self._t0 = None        # when the current qualifying hold began
        self._fired = False    # this press already switched the mode
        self._last = None      # previous step() time, for the gap restart

    def reset(self):
        self._t0 = None
        self._fired = False
        self._last = None

    def step(self, buttons, start_bit, now, enabled=True, passive_mask=0):
        gap = self._last is None or (now - self._last) > _MODE_HOLD_GAP
        self._last = now
        buttons = int(buttons)
        start_bit = int(start_bit)
        down = bool(buttons & start_bit)
        if not enabled or not down:
            self._t0 = None
            self._fired = False
            return False, 0
        # Already fired: keep swallowing the button until it comes back up.
        if self._fired:
            return False, start_bit
        # Held alongside anything else (a chord, a face button)  this press
        # means something else. Passive touch/rest contacts don't count.
        if buttons & ~(start_bit | int(passive_mask)):
            self._t0 = None
            return False, 0
        if self._t0 is None or gap:
            self._t0 = now
            return False, 0
        if (now - self._t0) < self._hold:
            return False, 0
        self._fired = True
        return True, start_bit


def _chord_trigger_mask(btns, SCButtons, button_ids=None):
    """Trigger (mask, is_gamepad) for a Hotkeys chord's button list, or None if
    invalid. One button = that bit; two DISTINCT buttons = both required.
    Guide-component triggers return None (the Guide-held path owns those).
    is_gamepad = every button is an 'xi_' Gamepad-Layout alias."""
    ids = button_ids if button_ids is not None else SC_CHORD_BUTTONS
    if not btns or CHORD_GUIDE_ID in btns:
        return None
    if len(btns) == 1:
        bit = getattr(SCButtons, ids.get(btns[0], ""), None)
        if not bit:
            return None
        mask = int(bit)
    elif len(btns) == 2 and btns[0] != btns[1]:
        bit1 = getattr(SCButtons, ids.get(btns[0], ""), None)
        bit2 = getattr(SCButtons, ids.get(btns[1], ""), None)
        if not bit1 or not bit2:
            return None
        mask = int(bit1) | int(bit2)
    else:
        return None
    return mask, all(b.startswith("xi_") for b in btns)


def build_button_combos(chords, SCButtons, Keys, button_ids=None):
    """Resolve saved Hotkeys "button_combo" chords into runtime entries:
      (mask, is_gamepad, xbox_output_ids, key_actions, guide)
      mask            = trigger bit(s), all required to fire. For a GUIDE trigger
                        (Guide + one other button) this is just the OTHER button's
                        bit; the tray also requires Steam/"..." to be held.
      is_gamepad      = True when every trigger button is an "xi_" alias (fires in
                        Gamepad Mode only); physical triggers are desktop-only.
                        Ignored for guide triggers (they fire in BOTH modes while
                        Steam/"..." is held).
      xbox_output_ids = selected outputs that are Xbox (ViGEm) DIGITAL buttons 
                        the tray ORs their XUSB flags into the virtual pad while
                        the trigger is HELD.
      key_actions     = resolve_action() tuples for outputs that are keyboard /
                        mouse / system actions  held / edge-fired while held.
      guide           = True when the trigger includes the Guide button (Steam /
                        "..."), so the tray gates it on guide_now instead of the
                        two-button chord mask.
    The "Button Combo" effect (Hotkeys tab) holds ALL its outputs while the
    trigger is held. Analog outputs (lt/rt/analog) contribute nothing (they have
    no digital XUSB flag); an entry with no usable output is dropped."""
    ids = button_ids if button_ids is not None else SC_CHORD_BUTTONS
    out = []
    for ch in chords or []:
        if not isinstance(ch, dict) or ch.get("type") != "button_combo":
            continue
        btns = ch.get("buttons") or []
        xbox_ids, key_actions = [], []
        for out_id in ch.get("outputs") or []:
            if out_id in _GAMEPAD_OUTPUT_IDS:
                if out_id not in ("none", "analog") and out_id not in _GAMEPAD_ANALOG:
                    xbox_ids.append(out_id)   # digital Xbox button
            else:
                act = resolve_action(out_id, Keys)
                if act[0] != "none":
                    key_actions.append(act)
        if not xbox_ids and not key_actions:
            continue
        if CHORD_GUIDE_ID in btns:
            # Guide gesture (Steam / "..." held). Guide ALONE → mask 0 (fires
            # whenever Steam/"..." is held); Guide + one other button → that
            # button's bit is also required.
            if len(btns) == 1:
                mask = 0
            elif len(btns) == 2 and btns[0] != btns[1]:
                other = btns[1] if btns[0] == CHORD_GUIDE_ID else btns[0]
                bit = getattr(SCButtons, ids.get(other, ""), None)
                if not bit:
                    continue
                mask = int(bit)
            else:
                continue
            out.append((mask, False, xbox_ids, key_actions, True))
        else:
            trig = _chord_trigger_mask(btns, SCButtons, ids)
            if trig is None:
                continue
            mask, is_gamepad = trig
            out.append((mask, is_gamepad, xbox_ids, key_actions, False))
    return out


# --- Steam Controller desktop per-control rebind (takeover) ------------------
# Default action for every control shown in the picker's SC "pc" layout. MUST
# match the picker's SC pc layout defaults exactly (the picker syncs its layout
# to this dict on import) so an UNCHANGED control resolves to no override and
# keeps the tray _Watcher's proven hardcoded behavior. Only a control whose saved
# value DIFFERS from its default here becomes a live override.
SC_DESKTOP_DEFAULTS = {
    # left cluster
    "l1": "ctrl", "l2": "mouse_right", "l4": "hold_shift", "l5": "hold_win",
    "lg": "none", "lg2": "none", "lpad": "mouse_middle",
    # right cluster
    "r1": "alt", "r2": "mouse_left", "r4": "pageup", "r5": "pagedown",
    "rg": "none", "start": "escape", "back": "tab", "rpad": "mouse_left",
    # sticks
    "lstick_up": "up", "lstick_down": "down",
    "lstick_left": "left", "lstick_right": "right",
    "rstick_up": "joystick_mouse", "rstick_down": "joystick_mouse",
    "rstick_left": "joystick_mouse", "rstick_right": "joystick_mouse",
    "rstick": "joystick_mouse", "rstick_click": "none",
    "lstick_click": "mouse_middle",   # L3 (left stick click)
    # touch / grip sensors (capacitive  fire on contact, not press)
    "lstick_touch": "none", "rstick_touch": "none",
    "lgrip_touch": "none", "rgrip_touch": "none",
    # d-pad
    "dpad_up": "up", "dpad_down": "down", "dpad_left": "left", "dpad_right": "right",
    # face
    "a": "enter", "b": "escape", "x": "show_keyboard", "y": "space",
    # guide cluster  TAP actions (a short press with no chord). Steam TAP opens/
    # closes the config GUI by default (hold + button still runs the CHORDS);
    # "..." stays free.
    "steam": "toggle_gui", "qam": "none",
}

# Control id -> SCButtons attr for the DIGITAL controls we can live-override.
# Includes X (default "show_keyboard"; the dispatcher opens the OSK so X is
# rebindable and any button can be bound to open it). The analog sticks
# (lstick_*, rstick) are handled separately (resolve_sc_sticks); lg/lg2/rg have
# no distinct hardware bit on this firmware.
SC_DESKTOP_BUTTONS = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "l1": "LB", "r1": "RB", "l2": "LT", "r2": "RT",
    "l4": "LGRIP1", "l5": "LGRIP2", "r4": "RGRIP1", "r5": "RGRIP2",
    "start": "START", "back": "VIEW", "rstick_click": "R3", "lstick_click": "L3",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
    "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
    "lpad": "LPAD", "rpad": "RPAD",
    "lstick_touch": "LPADJOY_TOUCH", "rstick_touch": "RPADJOY_TOUCH",
    "lgrip_touch": "LGRIP_REST", "rgrip_touch": "RGRIP_REST",
}

# Left-stick directional zone -> control id (resolve_sc_sticks). The right stick
# (rstick) is a mouse on/off mode, not directional.
_LSTICK_DIRS = {"UP": "lstick_up", "DOWN": "lstick_down",
                "LEFT": "lstick_left", "RIGHT": "lstick_right"}
_RSTICK_DIRS = {"UP": "rstick_up", "DOWN": "rstick_down",
                "LEFT": "rstick_left", "RIGHT": "rstick_right"}

# Multi-key combos exposed as single actions.
_DESKTOP_COMBOS = {"prev_tab": ["ctrl", "shift", "tab"], "next_tab": ["ctrl", "tab"]}
# Held-modifier actions (press on button-down, release on button-up).
_DESKTOP_HOLDS = {"hold_ctrl": "ctrl", "hold_shift": "shift",
                  "hold_alt": "alt", "hold_win": "win"}
# System actions dispatched specially (not a key/click). Pass through as their
# own tuple type; the tray dispatcher (_fire_guide_action) executes them. Shared
# by the Desktop and Chords tabs.
_SYSTEM_ACTIONS = ("alt_tab", "force_kill", "power_off", "toggle_magnifier",
                   "brightness_up", "brightness_down", "lock_pc", "screen_off",
                   "sleep_pc", "shutdown_pc",
                   "profile_cycle", "toggle_gui", "mic_mute_toggle",
                   "gamepad_mode_toggle", "big_picture")

# Sentinel "key" for the Push-to-Talk action. resolve_action returns PTT as
# ("hold", MIC_PTT_KEY) so it rides the SAME ref-counted hold plumbing every
# hold-capable dispatch site already has (press on button-down, release on
# button-up, released in bulk when a mode switch drops the desktop layer) 
# the tray's key-hold helpers recognize this sentinel and open/close the
# system microphone instead of pressing a keyboard key. The "@" prefix keeps
# it out of every real sui.Keys value's namespace (those are the kernel-style
# "KEY_*"/"BTN_*" names on Windows and their int codes on Linux).
MIC_PTT_KEY = "@mic_ptt"

# Sentinel "key" for the bindable "Gyro To Mouse" action (Desktop / Chords /
# Gamepad tabs). Like MIC_PTT_KEY it rides the ("hold", ...) contract so every
# hold-capable dispatch site gets press-on-button-down / release-on-button-up
# for free  but the hold is not applied HERE: what a press actually does is
# read from the controller's own Options → Gyro To Mouse mode
# (none / hold_enable / hold_suppress / toggle), exactly as the modal's hotkey
# bars are, so a button bound on a layout tab and a chord picked in the modal
# behave identically. The tray's key-hold helpers recognize this sentinel and
# route it to that mode-aware handler instead of pressing a keyboard key.
GYRO_MOUSE_KEY = "@gyro_mouse"

# Page navigation actions -> Windows XBUTTON id (1=Back/"Page Previous",
# 2=Forward/"Page Next"). Dispatched via raw mouse_event (not sui.Mouse, which
# only supports left/right/middle)  same injection "Swipe Between Pages" uses,
# so it's honored by browsers, File Explorer and Windows Settings/Control Panel.
_XBUTTON_ACTIONS = {"page_prev": 1, "page_next": 2}


def resolve_action(value, Keys):
    """Map a picker action VALUE to a normalized runtime action tuple for the
    SC dispatcher (used by both Desktop and Chords/guide tabs):
      ("none",)                nothing
      ("click", "left"|"right"|"middle")
      ("scroll", +1|-1)        one wheel notch per press
      ("hold", key)            hold a modifier while the button is held
                                (key may be MIC_PTT_KEY: hold the mic open, or
                                GYRO_MOUSE_KEY: drive the controller's gyro
                                per its own Options gyro mode)
      ("combo", (k1, k2, ...)) press all + release reverse, once per press
      ("tap", key)             press+release once per press (keys, media, F-keys)
      ("show_keyboard",)       open the OSK
      ("xbutton", 1|2)         Page Previous/Next (mouse Back/Forward)
      ("alt_tab",|"force_kill",|"power_off",|"toggle_magnifier",)  system action
    Actions that don't apply to a button (as_mouse / joystick_mouse / trackpad_mouse
    / mouse4 / mouse5, which sui.Mouse can't emit) resolve to ("none",)."""
    if not value or value == "none":
        return ("none",)
    # Modifier-prefixed keyboard combo ("alt+f4", "ctrl+shift+a"): press the
    # modifier(s) + the base key together, released in reverse  reusing the
    # existing ("combo", ...) dispatch. Only a base that resolves to a plain key
    # ("tap") takes modifiers; anything else ignores them and resolves normally.
    if "+" in value:
        mods, base = split_modifiers(value)
        base_act = resolve_action(base, Keys)
        if mods and base_act[0] == "tap" and base_act[1] is not None:
            mkeys = [key_for(m, Keys) for m in mods]
            mkeys = [k for k in mkeys if k is not None]
            if mkeys:
                return ("combo", tuple(mkeys) + (base_act[1],))
        return base_act
    if value in _CLICK:
        return ("click", _CLICK[value])
    if value == "scroll_up":
        return ("scroll", 1)
    if value == "scroll_down":
        return ("scroll", -1)
    if value in _DESKTOP_HOLDS:
        k = key_for(_DESKTOP_HOLDS[value], Keys)
        return ("hold", k) if k is not None else ("none",)
    if value in _DESKTOP_COMBOS:
        keys = [key_for(t, Keys) for t in _DESKTOP_COMBOS[value]]
        keys = [k for k in keys if k is not None]
        return ("combo", tuple(keys)) if keys else ("none",)
    if value == "mic_ptt":
        # Push to Talk is a HOLD, not an edge action  reuse the ("hold", ...)
        # contract with the mic sentinel so every hold-capable dispatch site
        # opens the mic on button-down and closes it on button-up for free.
        return ("hold", MIC_PTT_KEY)
    if value == "gyro_mouse":
        # "Gyro To Mouse" is a HOLD too  the controller's gyro MODE decides
        # what a hold means (enable while held / suppress while held / flip on
        # each press), so the action only has to report press and release and
        # the dispatcher hands both to the mode-aware handler.
        return ("hold", GYRO_MOUSE_KEY)
    if value == "show_keyboard":
        return ("show_keyboard",)
    if value in _XBUTTON_ACTIONS:
        return ("xbutton", _XBUTTON_ACTIONS[value])
    if value in _SYSTEM_ACTIONS:
        return (value,)
    if value in ("as_mouse", "joystick_mouse", "trackpad_mouse",
                 "mouse4", "mouse5"):
        return ("none",)  # not meaningful as a button action (sui.Mouse lacks 4/5)
    k = key_for(value, Keys)
    return ("tap", k) if k is not None else ("none",)


def resolve_guide_taps(binds, Keys, defaults=None):
    """Resolve Steam / "..." (QAM) TAP actions from an SC submap. Returns
    {"steam": action, "qam": action} (resolve_action tuples). The tray fires the
    action only on a clean TAP (short press, no other button during the hold) so
    the held Steam/"..." chords are untouched. `defaults` selects the fallback
    map  SC_DESKTOP_DEFAULTS for the pc submap (default), SC_GAMEPAD_DEFAULTS
    for the gamepad submap (so the Guide button also toggles the GUI while a
    virtual pad is being driven). Default 'none' → tap does nothing."""
    binds = binds or {}
    defaults = SC_DESKTOP_DEFAULTS if defaults is None else defaults
    out = {}
    for cid in ("steam", "qam"):
        val = binds.get(cid) or defaults.get(cid, "none")
        out[cid] = resolve_action(val, Keys)
    return out


def resolve_sc_sticks(binds, Keys):
    """Resolve the analog sticks from the SC pc binds. Returns a 4-tuple:
      (lstick_mouse, lstick, rstick_mouse, rstick)
      lstick_mouse = True when all left directions are 'joystick_mouse'
                     (left stick drives the cursor instead of firing keys).
      lstick  = {"UP"/"DOWN"/"LEFT"/"RIGHT": action}  used when lstick_mouse=False.
      rstick_mouse = True when all right directions are 'joystick_mouse' (or
                     legacy: all "none" + rstick key = joystick_mouse).
      rstick  = {"UP"/"DOWN"/"LEFT"/"RIGHT": action}  used when rstick_mouse=False.
    Defaults: left=arrows, right=mouse."""
    binds = binds or {}
    lstick = {}
    all_lstick_mouse = True
    for zone, cid in _LSTICK_DIRS.items():
        val = binds.get(cid) or SC_DESKTOP_DEFAULTS.get(cid, "none")
        lstick[zone] = resolve_action(val, Keys)
        if val not in ("joystick_mouse", "as_mouse"):
            all_lstick_mouse = False
    lstick_mouse = all_lstick_mouse
    rstick = {}
    all_rstick_mouse = True
    all_rstick_none = True
    for zone, cid in _RSTICK_DIRS.items():
        val = binds.get(cid) or SC_DESKTOP_DEFAULTS.get(cid, "none")
        rstick[zone] = resolve_action(val, Keys)
        if val not in ("joystick_mouse", "as_mouse"):
            all_rstick_mouse = False
        if val != "none":
            all_rstick_none = False
    rsv = binds.get("rstick") or SC_DESKTOP_DEFAULTS.get("rstick")
    # Legacy: if directions were all "none" (old saves) and the rstick setting
    # says joystick_mouse, honour that so old configs don't break.
    rstick_mouse = all_rstick_mouse or (
        all_rstick_none and rsv in ("joystick_mouse", "as_mouse"))
    return lstick_mouse, lstick, rstick_mouse, rstick


def resolve_sc_overrides(binds, SCButtons, Keys):
    """From the SC PC-mode binds, return [(cid, bit, action), ...] for every
    DIGITAL control whose saved action differs from its default. The tray
    _Watcher masks these bits out of its hardcoded desktop path and dispatches
    `action` instead  so unchanged controls keep their built-in behavior and
    only the user's edits diverge. `binds` is the SC pc submap (see pc_submap)."""
    binds = binds or {}
    out = []
    for cid, attr in SC_DESKTOP_BUTTONS.items():
        value = binds.get(cid)
        if value is None or value == SC_DESKTOP_DEFAULTS.get(cid):
            continue  # unset or unchanged → hardcoded default path handles it
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        out.append((cid, int(bit), resolve_action(value, Keys)))
    return out


def resolve_sc_close_buttons(binds, SCButtons):
    """Set of int SCButtons bits whose SC desktop (pc) action resolves to
    'escape'. These close the OSK when pressed while it's open, mirroring the
    keyboard Escape  so the close follows the binding (B is 'escape' by default)
    instead of being hardcoded. `binds` is the SC pc submap (see pc_submap)."""
    binds = binds or {}
    out = set()
    for cid, attr in SC_DESKTOP_BUTTONS.items():
        val = binds.get(cid) or SC_DESKTOP_DEFAULTS.get(cid)
        if val == "escape":
            bit = getattr(SCButtons, attr, None)
            if bit:
                out.add(int(bit))
    return out


# --- Steam Controller GUIDE-hold binds (Chords tab in the picker) ------------
# Default action for each control when the Guide/Steam/"..." button is HELD.
# MUST match the picker's SC "guide" layout defaults exactly. Controls whose
# saved value differs from their default here will override the default at
# runtime; controls with a "none" default do nothing by default.
# The Watcher gates its hardcoded Steam+B/Y/X/VIEW handlers on the button
# NOT being present in this map (i.e. user set it to "none"), so the guide
# system owns those buttons when they have a non-none action.
SC_GUIDE_DEFAULTS = {
    "l1": "toggle_magnifier",
    "r1": "print_screen",
    "dpad_down": "tab",
    "dpad_left": "escape",
    "dpad_right": "enter",
    "b": "force_kill",
    "y": "power_off",
    "x": "show_keyboard",
    "back": "alt_tab",
    "lstick_click": "media_playpause",
}

# Control id -> SCButtons attr for the guide-overlay digital controls we dispatch.
# Includes x/y/b/back(VIEW)  the Watcher gates its hardcoded handlers for those
# so the guide bind wins when set; set to "none" in picker to restore the
# hardcoded default. lstick_click (L3) and lstick directional zones still have
# their own hardcoded handlers and are not included here.
SC_GUIDE_BUTTONS = {
    "a": "A",
    "b": "B", "x": "X", "y": "Y",
    "l1": "LB", "r1": "RB",
    "l2": "LT", "r2": "RT",
    "l4": "LGRIP1", "l5": "LGRIP2",
    "r4": "RGRIP1", "r5": "RGRIP2",
    "start": "START", "back": "VIEW",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
    "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
    "lpad": "LPAD", "rpad": "RPAD",
    "rstick_click": "R3",
    "lstick_click": "L3",
    "rstick_touch": "RPADJOY_TOUCH",
    "lstick_touch": "LPADJOY_TOUCH",
    "lgrip_touch": "LGRIP_REST",
    "rgrip_touch": "RGRIP_REST",
}

# Analog zone maps for guide-hold dispatch (separate from digital SC_GUIDE_BUTTONS).
_SC_GUIDE_RSTICK_ZONES = {
    "UP": "rstick_up", "DOWN": "rstick_down",
    "LEFT": "rstick_left", "RIGHT": "rstick_right",
}
_SC_GUIDE_LSTICK_ZONES = {
    "UP": "lstick_up", "DOWN": "lstick_down",
    "LEFT": "lstick_left", "RIGHT": "lstick_right",
}

def resolve_sc_guide(guide_binds, SCButtons, Keys):
    """From the SC guide-mode binds (picker "guide" submap), return
    [(bit, action), ...] for every digital button that has a non-none guide
    action. Called at launcher time; the Watcher fires these on rising edge of
    Steam+button. Uses the shared resolve_action so the Chords tab supports
    the full Desktop vocabulary. The Watcher builds _guide_bind_bits from this
    list to gate its hardcoded Steam+B/Y/X/VIEW handlers."""
    binds = guide_binds or {}
    out = []
    for cid, attr in SC_GUIDE_BUTTONS.items():
        val = binds.get(cid) or SC_GUIDE_DEFAULTS.get(cid, "none")
        action = resolve_action(val, Keys)
        if action[0] == "none":
            continue
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        out.append((int(bit), action))
    return out


def resolve_sc_guide_rstick(guide_binds, Keys):
    """From the SC guide-mode binds return {zone: action} for right-stick
    directional zones. Zone keys are 'UP','DOWN','LEFT','RIGHT'; only zones
    with a non-none action are included. Passed to the Watcher as
    guide_rstick_zones; the Watcher fires actions on zone entry while Steam is
    held and suppresses cursor movement while any zone is bound."""
    binds = guide_binds or {}
    out = {}
    for zone, cid in _SC_GUIDE_RSTICK_ZONES.items():
        val = binds.get(cid, "none")
        action = resolve_action(val, Keys)
        if action[0] != "none":
            out[zone] = action
    return out


def resolve_sc_guide_lstick(guide_binds, Keys):
    """From the SC guide-mode binds return {zone: action} for left-stick
    directional zones. Zone keys are 'UP','DOWN','LEFT','RIGHT'; only zones
    with a non-none action are included. Passed to the Watcher as
    guide_lstick_zones; the Watcher fires actions on zone entry while Steam is
    held and suppresses the hardcoded media-chord zone handling."""
    binds = guide_binds or {}
    out = {}
    for zone, cid in _SC_GUIDE_LSTICK_ZONES.items():
        val = binds.get(cid, "none")
        action = resolve_action(val, Keys)
        if action[0] != "none":
            out[zone] = action
    return out


# --- Steam Controller GAMEPAD mode (virtual Xbox) per-control rebind ---------
# Default virtual-Xbox output for each control in the picker's SC "gamepad"
# layout. MUST match keybinds_picker's SC gamepad layout defaults (the picker
# syncs its layout defaults to this dict on import, same as SC_DESKTOP_DEFAULTS
# for the pc layout). Values are gamepad action ids (see keybinds_picker's
# _build_gamepad_actions); "none" = no output.
SC_GAMEPAD_DEFAULTS = {
    # left cluster
    "l1": "lb", "l2": "lt", "l4": "none", "l5": "none", "lg": "none",
    "start": "back", "lpad": "none",
    # right cluster
    "r1": "rb", "r2": "rt", "r4": "none", "r5": "none", "rg": "none",
    "back": "start", "rpad": "none",
    # sticks (analog passthrough  directions aren't wired to buttons)
    "lstick_up": "analog", "lstick_down": "analog",
    "lstick_left": "analog", "lstick_right": "analog",
    "rstick_up": "analog", "rstick_down": "analog",
    "rstick_left": "analog", "rstick_right": "analog",
    "lstick_click": "ls", "rstick_click": "rs",
    # touch / grip sensors
    "lstick_touch": "none", "rstick_touch": "none",
    "lgrip_touch": "none", "rgrip_touch": "none",
    # d-pad
    "dpad_up": "dpad_up", "dpad_down": "dpad_down",
    "dpad_left": "dpad_left", "dpad_right": "dpad_right",
    # face
    "a": "btn_a", "b": "btn_b", "x": "btn_x", "y": "btn_y",
    # guide cluster  Steam TAP opens/closes the config GUI (hold still runs the
    # chords). This replaces the old Xbox-Guide passthrough: rebind "steam" back
    # to "guide" on the Gamepad tab if you want the button to reach the game.
    "steam": "toggle_gui", "qam": "none",
}

# Control id -> SCButtons attr for the DIGITAL controls whose Xbox output we can
# remap in gamepad mode. Analog sticks aren't direction-mapped. l2/r2 are the
# trigger CLICK bits  left at "lt"/"rt" they pass through as analog axes;
# bound to a button they fire that button on full pull.
SC_GAMEPAD_BUTTONS = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "l1": "LB", "r1": "RB", "l2": "LT", "r2": "RT",
    "l4": "LGRIP1", "l5": "LGRIP2", "r4": "RGRIP1", "r5": "RGRIP2",
    "start": "START", "back": "VIEW",
    "lstick_click": "L3", "rstick_click": "R3",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
    "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
    "lpad": "LPAD", "rpad": "RPAD",
    "steam": "STEAM", "qam": "QAM",
    "lstick_touch": "LPADJOY_TOUCH", "rstick_touch": "RPADJOY_TOUCH",
    "lgrip_touch": "LGRIP_REST", "rgrip_touch": "RGRIP_REST",
}

# Gamepad action ids that are the analog triggers (not digital buttons).
_GAMEPAD_ANALOG = {"lt", "rt"}

# Every action id the gamepad ("Xbox output") vocabulary produces (see
# keybinds_picker._build_gamepad_actions). A SC gamepad-mode control whose value
# is NOT in here is a DESKTOP/keyboard action (the Gamepad tab now also offers
# the pc vocabulary on the SC): it emits no XInput bit and is instead injected
# as a key/click via resolve_sc_gamepad_keys + the watcher's key-override path.
_GAMEPAD_OUTPUT_IDS = frozenset({
    "none", "analog",
    "btn_a", "btn_b", "btn_x", "btn_y",
    "lb", "rb", "lt", "rt", "ls", "rs",
    "back", "start", "guide",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
})


def gamepad_submap(kind_binds):
    """Extract the GAMEPAD-mode control->action map from one controller's saved
    binds (mirror of pc_submap). Legacy flat binds were PC-only, so they yield an
    empty gamepad map. Always returns a dict."""
    if not isinstance(kind_binds, dict):
        return {}
    if "pc" in kind_binds or "gamepad" in kind_binds:
        gp = kind_binds.get("gamepad")
        return gp if isinstance(gp, dict) else {}
    return {}  # legacy flat = PC mode only


def resolve_sc_gamepad(binds, SCButtons):
    """Resolve the SC gamepad-mode binds into the inputs VirtualGamepad.update
    needs to honor them:

      button_map = [(sc_bit, action), ...]  for every digital control, OR its
                   `action`'s Xbox button into wButtons when sc_bit is held.
                   Reproduces the 1:1 SC->Xbox default unless the picker overrode
                   a control. "none"/analog-trigger actions are omitted.
      lt_analog / rt_analog = keep the analog trigger axis live (True unless L2/R2
                   was rebound to a non-trigger output, in which case the trigger
                   becomes that digital button instead of an axis).

    `SCButtons` is passed in to keep this module free of the steamcontroller
    package; the action->XUSB translation lives in steamcontroller.gamepad."""
    binds = binds or {}
    button_map = []
    lt_analog = rt_analog = True
    for cid, attr in SC_GAMEPAD_BUTTONS.items():
        val = binds.get(cid) or SC_GAMEPAD_DEFAULTS.get(cid, "none")
        if cid == "l2":
            lt_analog = (val == "lt")
        elif cid == "r2":
            rt_analog = (val == "rt")
        # "none", analog trigger, OR a keyboard action (not an Xbox output) →
        # no XInput bit. Keyboard actions are dispatched by the watcher via
        # resolve_sc_gamepad_keys instead; excluding them here also stops the
        # control double-acting (XInput + key). ("none" is in _GAMEPAD_OUTPUT_IDS
        # semantically but still emits nothing.)
        if val == "none" or val not in _GAMEPAD_OUTPUT_IDS or val in _GAMEPAD_ANALOG:
            continue
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        button_map.append((int(bit), val))
    return button_map, lt_analog, rt_analog


def resolve_sc_gamepad_keys(binds, SCButtons, Keys):
    """From the SC gamepad-mode binds, return [(cid, bit, action), ...] for every
    digital control bound to a DESKTOP/keyboard action (not an Xbox output). The
    tray _Watcher dispatches these while driving the virtual pad  so a gamepad-
    mode control can type a key / click / run a system action instead of (these
    are excluded from resolve_sc_gamepad's XInput button_map). `action` is a
    resolve_action tuple; mirror of resolve_sc_overrides but over the gamepad
    binds + SC_GAMEPAD_BUTTONS. `binds` is the SC gamepad submap."""
    binds = binds or {}
    out = []
    for cid, attr in SC_GAMEPAD_BUTTONS.items():
        if cid in ("steam", "qam"):
            # Meta (Guide) buttons: their TAP is dispatched by the guide-tap path
            # (resolve_guide_taps) in BOTH modes, and their HOLD is the chord
            # modifier  never a plain key-override (which is gated off while
            # Guide is held anyway, so this entry would be dead here).
            continue
        val = binds.get(cid) or SC_GAMEPAD_DEFAULTS.get(cid, "none")
        if val == "none" or val in _GAMEPAD_OUTPUT_IDS:
            continue  # unset / analog / an Xbox output → not a key action
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        out.append((cid, int(bit), resolve_action(val, Keys)))
    return out


def resolve_sc_gamepad_sticks(binds):
    """Resolve SC gamepad-mode stick direction binds.
    Returns (lstick_map, rstick_map). Each is None when all 4 directions are
    'analog'/'none' (full analog passthrough), or {"UP":action,...} when any
    direction is bound to a real button (stick goes fully digital  axis zeroed,
    bound button held while stick is in that zone)."""
    binds = binds or {}

    def _resolve(dirs):
        m = {}
        all_pass = True
        for zone, cid in dirs.items():
            val = binds.get(cid) or SC_GAMEPAD_DEFAULTS.get(cid, "none")
            if val not in ("none", "analog"):
                all_pass = False
                m[zone] = val
        if all_pass:
            return None
        for zone in dirs:
            m.setdefault(zone, "none")
        return m

    return _resolve(_LSTICK_DIRS), _resolve(_RSTICK_DIRS)


def build_desktop_tables(binds, SCButtons, Keys):
    """Build (clicks, key_taps) for _SdlDesktopController from a Switch keybind
    map (control_id -> action value).

      clicks   = ((scbutton_bit, mouse_name), ...)
      key_taps = ((scbutton_bit, sui_key), ...)

    Controls not present in `binds` fall back to SWITCH_DEFAULTS, so an empty map
    reproduces the built-in behavior exactly. `SCButtons` and `Keys` are passed
    in to keep this module import-light. A control whose action is "none" /
    unsupported is simply omitted (it does nothing)."""
    binds = binds or {}
    button = {
        "zl": SCButtons.LT, "zr": SCButtons.RT,
        "l3": SCButtons.L3, "r3": SCButtons.R3,
        "dpad_up": SCButtons.DPAD_UP, "dpad_down": SCButtons.DPAD_DOWN,
        "dpad_left": SCButtons.DPAD_LEFT, "dpad_right": SCButtons.DPAD_RIGHT,
        "a": SCButtons.A, "b": SCButtons.B, "y": SCButtons.Y,
    }
    clicks, taps = [], []
    for cid, bit in button.items():
        action = binds.get(cid) or SWITCH_DEFAULTS.get(cid, "none")
        name = click_for(action)
        if name is not None:
            clicks.append((bit, name))
            continue
        key = key_for(action, Keys)
        if key is not None:
            taps.append((bit, key))
    return tuple(clicks), tuple(taps)


# =============================================================================
# SDL-template controllers  FULL Steam-Controller-parity binds
# =============================================================================
# Every non-SC kind (Switch / Xbox / PS / Steam Deck / handhelds) shares the
# SDL template's control-id space, so ONE set of defaults + resolvers serves
# them all; only labels/glyphs differ per kind (pads.py). These mirror the SC's
# SC_DESKTOP_* / SC_GUIDE_* / SC_GAMEPAD_* trio: the picker syncs its generated
# SDL layouts to these dicts on import, and _SdlDesktopController dispatches
# from the resolvers, so defaults reproduce the historical built-in behavior
# exactly and only user edits diverge.

# Desktop (pc) defaults. "x" is the positional WEST face button  the OSK
# opener (physical Y on a Switch, X on Xbox/PS)  rebindable like the SC's X.
# "home" is the Guide TAP (hold = the guide chords below).
SDL_DESKTOP_DEFAULTS = {
    "l": "prev_tab", "r": "next_tab",           # bumpers: browser tab switch
    "zl": "mouse_right", "zr": "mouse_left",    # triggers
    "minus": "none", "plus": "none", "capture": "none",
    "l3": "mouse_middle", "r3": "none",         # stick clicks
    "lstick_up": "up", "lstick_down": "down",
    "lstick_left": "left", "lstick_right": "right",
    "rstick_up": "joystick_mouse", "rstick_down": "joystick_mouse",
    "rstick_left": "joystick_mouse", "rstick_right": "joystick_mouse",
    "dpad_up": "up", "dpad_down": "down",
    "dpad_left": "left", "dpad_right": "right",
    "a": "enter", "b": "escape", "x": "show_keyboard", "y": "space",
    "home": "toggle_gui",                       # guide TAP: open/close config GUI
}

# Digital controls the SDL desktop runtime dispatches (cid -> SCButtons attr).
# "home" is NOT here  it's the guide-chord modifier; its TAP is special-cased.
SDL_DESKTOP_BUTTONS = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "l": "LB", "r": "RB", "zl": "LT", "zr": "RT",
    "l3": "L3", "r3": "R3",
    "minus": "VIEW", "plus": "START", "capture": "QAM",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
    "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
}

# Guide-hold (Chords tab) defaults  reproduce the historical hardcoded Home
# chords: Home+L3 = Play/Pause, Home+left stick = volume/track, Home+"+" =
# Alt+Tab, Home+B(positional) = force-kill. "x" stays none here because the
# OSK-open path (Guide+X in gamepad mode) lives in the tray thread.
SDL_GUIDE_DEFAULTS = {
    "l3": "media_playpause",
    "plus": "alt_tab",
    "b": "force_kill",
    "lstick_up": "volume_up", "lstick_down": "volume_down",
    "lstick_left": "media_prev", "lstick_right": "media_next",
}

SDL_GUIDE_BUTTONS = dict(SDL_DESKTOP_BUTTONS)

# Gamepad (virtual Xbox) defaults  the 1:1 positional map the ViGEm bridge
# already produces. Stick directions are analog passthrough.
SDL_GAMEPAD_DEFAULTS = {
    "l": "lb", "r": "rb", "zl": "lt", "zr": "rt",
    # home TAP opens/closes the config GUI (hold still runs the chords); rebind
    # it back to "guide" on the Gamepad tab to pass Xbox Guide to the game.
    "minus": "back", "plus": "start", "home": "toggle_gui", "capture": "none",
    "l3": "ls", "r3": "rs",
    "lstick_up": "analog", "lstick_down": "analog",
    "lstick_left": "analog", "lstick_right": "analog",
    "rstick_up": "analog", "rstick_down": "analog",
    "rstick_left": "analog", "rstick_right": "analog",
    "dpad_up": "dpad_up", "dpad_down": "dpad_down",
    "dpad_left": "dpad_left", "dpad_right": "dpad_right",
    "a": "btn_a", "b": "btn_b", "x": "btn_x", "y": "btn_y",
}

SDL_GAMEPAD_BUTTONS = dict(SDL_DESKTOP_BUTTONS, home="STEAM")


def resolve_sdl_desktop(binds, SCButtons, Keys, defaults=None):
    """From an SDL kind's PC-mode binds, return [(cid, bit, action), ...] for
    every digital control with a non-none resolved action. show_keyboard
    controls are EXCLUDED (the tray thread owns the OSK-open path  see
    resolve_sdl_open_bits). Unset controls fall back to SDL_DESKTOP_DEFAULTS,
    so an empty map reproduces the built-in behavior.

    `defaults` overrides that fallback table for controllers whose hardware
    makes the shared defaults wrong (pads.desktop_defaults  a lone Joy-Con
    has one stick where the table assumes two). Passed in rather than looked
    up so this module stays free of catalog imports."""
    binds = binds or {}
    defaults = defaults or SDL_DESKTOP_DEFAULTS
    out = []
    for cid, attr in SDL_DESKTOP_BUTTONS.items():
        val = binds.get(cid) or defaults.get(cid, "none")
        if val == "show_keyboard":
            continue
        action = resolve_action(val, Keys)
        if action[0] == "none":
            continue
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        out.append((cid, int(bit), action))
    return out


def resolve_sdl_open_bits(binds, SCButtons):
    """Bitmask of SDL desktop controls bound to 'show_keyboard' (default: the
    positional X / WEST face button). The tray's SDL thread opens the OSK on
    these bits (bare press in desktop mode, Guide-held in gamepad mode)."""
    binds = binds or {}
    mask = 0
    for cid, attr in SDL_DESKTOP_BUTTONS.items():
        val = binds.get(cid) or SDL_DESKTOP_DEFAULTS.get(cid, "none")
        if val == "show_keyboard":
            bit = getattr(SCButtons, attr, None)
            if bit:
                mask |= int(bit)
    return mask


def resolve_sdl_close_buttons(binds, SCButtons):
    """Set of int SCButtons bits whose SDL desktop action resolves to 'escape'
    (B by default)  mirror of resolve_sc_close_buttons, so closing the OSK
    with any pad follows that pad's Escape binding."""
    binds = binds or {}
    out = set()
    for cid, attr in SDL_DESKTOP_BUTTONS.items():
        val = binds.get(cid) or SDL_DESKTOP_DEFAULTS.get(cid)
        if val == "escape":
            bit = getattr(SCButtons, attr, None)
            if bit:
                out.add(int(bit))
    return out


def resolve_sdl_home_tap(binds, Keys, defaults=None):
    """Resolve the Guide/Home TAP action from an SDL kind's submap (mirror of
    resolve_guide_taps): fired on a clean short press with no chord. `defaults`
    picks the fallback  SDL_DESKTOP_DEFAULTS for the pc submap (default),
    SDL_GAMEPAD_DEFAULTS for the gamepad submap (so Home also toggles the GUI
    while a virtual pad is driven)."""
    binds = binds or {}
    defaults = SDL_DESKTOP_DEFAULTS if defaults is None else defaults
    val = binds.get("home") or defaults.get("home", "none")
    return resolve_action(val, Keys)


def _resolve_stick_zones(binds, defaults, dirs, Keys):
    """{zone: action} + all-joystick-mouse flag for one stick's direction cids."""
    zones = {}
    all_mouse = True
    for zone, cid in dirs.items():
        val = binds.get(cid) or defaults.get(cid, "none")
        zones[zone] = resolve_action(val, Keys)
        if val not in ("joystick_mouse", "as_mouse"):
            all_mouse = False
    return zones, all_mouse


def resolve_sdl_sticks(binds, Keys, defaults=None):
    """Resolve an SDL kind's analog sticks from its pc binds (mirror of
    resolve_sc_sticks, over SDL_DESKTOP_DEFAULTS). Returns
    (lstick_mouse, lstick_zones, rstick_mouse, rstick_zones): a *_mouse=True
    stick drives the cursor; otherwise its zone actions fire on deflection
    (tap-then-repeat). Defaults: left=arrows, right=mouse  unless `defaults`
    overrides them for a pad the shared table doesn't fit (see
    resolve_sdl_desktop; a single-stick Joy-Con puts the cursor on its left)."""
    binds = binds or {}
    defaults = defaults or SDL_DESKTOP_DEFAULTS
    lstick, l_mouse = _resolve_stick_zones(binds, defaults,
                                           _LSTICK_DIRS, Keys)
    rstick, r_mouse = _resolve_stick_zones(binds, defaults,
                                           _RSTICK_DIRS, Keys)
    return l_mouse, lstick, r_mouse, rstick


def resolve_sdl_guide(guide_binds, SCButtons, Keys):
    """From an SDL kind's guide-mode binds, return [(bit, action), ...] for
    every digital control with a non-none guide action (defaults reproduce the
    historical hardcoded Home chords). Fired on rising edge while Guide/Home is
    held  mirror of resolve_sc_guide."""
    binds = guide_binds or {}
    out = []
    for cid, attr in SDL_GUIDE_BUTTONS.items():
        val = binds.get(cid) or SDL_GUIDE_DEFAULTS.get(cid, "none")
        action = resolve_action(val, Keys)
        if action[0] == "none":
            continue
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        out.append((int(bit), action))
    return out


def resolve_sdl_guide_lstick(guide_binds, Keys):
    """{zone: action} for an SDL kind's guide-held LEFT stick (defaults =
    volume up/down + track prev/next, the historical Home+stick chords).
    UP/DOWN zones auto-repeat at the media-ramp cadence in the runtime."""
    binds = guide_binds or {}
    out = {}
    for zone, cid in _LSTICK_DIRS.items():
        val = binds.get(cid) or SDL_GUIDE_DEFAULTS.get(cid, "none")
        action = resolve_action(val, Keys)
        if action[0] != "none":
            out[zone] = action
    return out


def resolve_sdl_guide_rstick(guide_binds, Keys):
    """{zone: action} for an SDL kind's guide-held RIGHT stick (default none 
    the right stick stays the Guide-held mouse)."""
    binds = guide_binds or {}
    out = {}
    for zone, cid in _RSTICK_DIRS.items():
        val = binds.get(cid) or SDL_GUIDE_DEFAULTS.get(cid, "none")
        action = resolve_action(val, Keys)
        if action[0] != "none":
            out[zone] = action
    return out


def resolve_sdl_gamepad(binds, SCButtons):
    """Resolve an SDL kind's gamepad-mode binds for VirtualGamepad.update
    (mirror of resolve_sc_gamepad): (button_map, lt_analog, rt_analog).
    zl/zr left at "lt"/"rt" keep their analog axes; rebound they become that
    digital output. Keyboard actions are excluded (resolve_sdl_gamepad_keys)."""
    binds = binds or {}
    button_map = []
    lt_analog = rt_analog = True
    for cid, attr in SDL_GAMEPAD_BUTTONS.items():
        val = binds.get(cid) or SDL_GAMEPAD_DEFAULTS.get(cid, "none")
        if cid == "zl":
            lt_analog = (val == "lt")
        elif cid == "zr":
            rt_analog = (val == "rt")
        if val == "none" or val not in _GAMEPAD_OUTPUT_IDS or val in _GAMEPAD_ANALOG:
            continue
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        button_map.append((int(bit), val))
    return button_map, lt_analog, rt_analog


def resolve_sdl_gamepad_keys(binds, SCButtons, Keys):
    """[(cid, bit, action)] for SDL gamepad-mode controls bound to a DESKTOP/
    keyboard action instead of an Xbox output (mirror of
    resolve_sc_gamepad_keys)  the Gamepad tab offers the merged vocabulary on
    every kind now."""
    binds = binds or {}
    out = []
    for cid, attr in SDL_GAMEPAD_BUTTONS.items():
        val = binds.get(cid) or SDL_GAMEPAD_DEFAULTS.get(cid, "none")
        if val == "none" or val in _GAMEPAD_OUTPUT_IDS:
            continue
        bit = getattr(SCButtons, attr, None)
        if not bit:
            continue
        out.append((cid, int(bit), resolve_action(val, Keys)))
    return out


# =============================================================================
# Advanced press actions (Gamepad tab): Long Press / Double Press / Soft Pull
# =============================================================================
# Stored INSIDE a kind's gamepad submap under the reserved "__adv" key as a
# list of {"control": cid, "press": "long"|"double"|"soft", "action": value}
# rows, so they ride the existing profile slots / save / import plumbing
# untouched (every other consumer looks up real cids and ignores "__adv").
#
#   long    action asserts after the control is HELD ≥ LONG_PRESS_S; a shorter
#           press still fires the control's REGULAR action as a deferred pulse
#           on release (exactly Steam Input's "Long Press" activator).
#   double  action asserts on the second press of a quick double-tap; a single
#           press fires the regular action after the double-tap window closes.
#   soft    (triggers only) action asserts while the analog pull is past a
#           light threshold  Steam's "Soft Pull" edge binding  independent
#           of the full-pull action.
#
# The engine below decides WHICH action a press means; the tray applies the
# asserted specs each frame (XUSB flags OR'd into the virtual pad, keyboard/
# mouse/system actions held or edge-fired like gamepad key overrides).

#   shift   (Steam's MODE SHIFT)  while the row's `control` is HELD, the
#           row's `target` control emits `action` instead of its normal
#           binding. Any number of rows may share one shift holder, forming
#           a full alternate layer; the holder's own binding is disabled
#           (it's a modifier now, exactly like Steam's mode-shift button).
#   plus    an EXTRA action asserted while the control is held, on top of
#           its normal binding (Steam's "additional bindings" on one press).
ADV_PRESS_TYPES = ("long", "double", "soft", "shift", "plus")

LONG_PRESS_S = 0.35      # hold this long → long-press action
DOUBLE_GAP_S = 0.25      # re-press within this window → double-press action
PULSE_S = 0.09           # deferred regular-action pulse length
SOFT_PULL_ON = 8192      # analog trigger soft-pull engage (0..32767)
SOFT_PULL_OFF = 6200     # release below this (hysteresis)
TRIGGER_FULL = 32767     # analog trigger full-scale (the soft-pull % is of this)

# Those three defaults are the SHIPPED feel; each controller can retune them on
# its Advanced Presses page. adv_timing below is the ONE reader  shared by the
# tray (which builds the engines) and the picker (which edits and explains
# them)  so the number on screen and the number the engine uses cannot drift
# apart. Ranges are what the picker's sliders span AND what a stored value is
# clamped to, so a hand-edited settings.json can't produce an unusable pad.
ADV_LONG_MS_RANGE = (150, 1200)
ADV_DOUBLE_MS_RANGE = (120, 600)
ADV_SOFT_PCT_RANGE = (5, 60)
# Release hysteresis, kept proportional to whatever engage point is chosen.
ADV_SOFT_RELEASE_RATIO = SOFT_PULL_OFF / float(SOFT_PULL_ON)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def adv_setting_key(kind, base):
    """The per-controller settings key for one advanced-press timing. Same
    "<base>_<kind>" shape pads.setting_key produces  spelled out here because
    this module deliberately imports nothing (see the file header), and these
    bases are new, so none of pads' legacy-key exceptions apply."""
    return "%s_%s" % (base, kind)


ADV_TIMING_RANGES = {"adv_long_ms": ADV_LONG_MS_RANGE,
                     "adv_double_ms": ADV_DOUBLE_MS_RANGE,
                     "adv_soft_pct": ADV_SOFT_PCT_RANGE}
ADV_TIMING_DEFAULTS = {"adv_long_ms": LONG_PRESS_S * 1000.0,
                       "adv_double_ms": DOUBLE_GAP_S * 1000.0,
                       "adv_soft_pct": SOFT_PULL_ON * 100.0 / TRIGGER_FULL}


def adv_timing_value(general, kind, base, default=None):
    """ONE advanced-press timing setting for `kind`, clamped to its range  the
    raw slider-facing number (ms, or % of trigger travel). Bad or out-of-range
    stored values fall back to the default rather than being trusted."""
    if default is None:
        default = ADV_TIMING_DEFAULTS[base]
    lo, hi = ADV_TIMING_RANGES[base]
    try:
        return _clamp(
            float((general or {}).get(adv_setting_key(kind, base), default)),
            lo, hi)
    except (TypeError, ValueError):
        return float(default)


def adv_timing(general, kind):
    """(long_s, double_s, soft_on, soft_off) for one controller: its own
    Advanced Presses timing settings, in the units AdvPressEngine wants."""
    long_ms = adv_timing_value(general, kind, "adv_long_ms")
    dbl_ms = adv_timing_value(general, kind, "adv_double_ms")
    soft_pct = adv_timing_value(general, kind, "adv_soft_pct")
    soft_on = int(round(TRIGGER_FULL * soft_pct / 100.0))
    return (long_ms / 1000.0, dbl_ms / 1000.0,
            soft_on, int(round(soft_on * ADV_SOFT_RELEASE_RATIO)))


def adv_rows(gp_binds):
    """Validated advanced-press rows from a kind's gamepad submap. Unknown
    press types / non-dict rows are dropped; controls and actions are kept
    as-is (resolve_adv_config validates them against the kind's tables).
    Shift rows additionally need a `target` control."""
    rows = (gp_binds or {}).get("__adv")
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        cid = r.get("control")
        press = r.get("press")
        action = r.get("action")
        if not (cid and action and action != "none"
                and press in ADV_PRESS_TYPES):
            continue
        row = {"control": cid, "press": press, "action": action}
        if press == "shift":
            target = r.get("target")
            if not target or target == cid:
                continue
            row["target"] = target
        out.append(row)
    return out


def _adv_spec(action, Keys):
    """An adv action value -> engine spec: ("xusb", output_id) for a virtual
    Xbox output, ("key", resolve_action tuple) for the desktop vocabulary,
    or None for none/analog (not assertable)."""
    if not action or action in ("none", "analog") or action in _GAMEPAD_ANALOG:
        return None
    if action in _GAMEPAD_OUTPUT_IDS:
        return ("xusb", action)
    act = resolve_action(action, Keys)
    return ("key", act) if act[0] != "none" else None


def _adv_spec_pc(action, Keys):
    """Desktop-mode adv spec: keyboard/mouse/system actions only  an Xbox
    output id (which the pc vocabulary doesn't offer, but an imported row
    might carry) resolves to None instead of leaking into desktop mode."""
    if not action or action in _GAMEPAD_OUTPUT_IDS \
            or action in _GAMEPAD_ANALOG:
        return None
    act = resolve_action(action, Keys)
    return ("key", act) if act[0] != "none" else None


# --- The Guide button in the Advanced Presses rows ---------------------------
# Long / Double / Mode-Shift / Extra rows may sit on the GUIDE button (Steam and
# "..." on a HID pad, Home on an SDL one). The Desktop tab's button maps leave
# it out on purpose  its TAP is dispatched by the guide-tap path and its HOLD
# is the chord modifier  so the adv resolver uses these EXTENDED maps instead.
# (The gamepad maps already carry it.)
#
# Two rules keep the chord layer intact, which is the whole reason the Guide
# button used to be excluded here:
#   * the Guide bits NEVER join `owned` / AdvPressEngine.frame_mask, so they are
#     never stripped from the frame  Guide + X, the Chords tab, the guide-hold
#     stick zones and the Guide TAP all keep working exactly as before.
#   * a Guide row has NO base action to pulse (base = None below): the short
#     press already belongs to the guide-tap path (resolve_guide_taps /
#     resolve_sdl_home_tap, which fire only under the tray's _GUIDE_TAP_S 
#     shorter than any long-press threshold), so a tap can't double-fire.
SC_ADV_DESKTOP_BUTTONS = dict(SC_DESKTOP_BUTTONS, steam="STEAM", qam="QAM")
SDL_ADV_DESKTOP_BUTTONS = dict(SDL_DESKTOP_BUTTONS, home="STEAM")


def adv_guide_cids(kind):
    """The control ids that ARE the Guide button on this kind  Steam + "..."
    on the HID pads (both are `guide_now` to the watcher), Home on an SDL one.
    The SDL "capture" button shares the QAM bit but is a plain button there, so
    it is deliberately NOT in this list."""
    return ("steam", "qam") if kind in SC_ID_SPACE_KINDS else ("home",)


def adv_guide_mask(kind, SCButtons):
    """SCButtons mask of `kind`'s Guide bits (see adv_guide_cids). Handed to
    AdvPressEngine so its Guide rows keep running while the chord layer is up
    and so those bits are never masked out of the frame. SCButtons is passed in
    to keep this module import-free."""
    buttons = (SC_ADV_DESKTOP_BUTTONS if kind in SC_ID_SPACE_KINDS
               else SDL_ADV_DESKTOP_BUTTONS)
    mask = 0
    for cid in adv_guide_cids(kind):
        bit = getattr(SCButtons, buttons.get(cid, ""), None)
        if bit:
            mask |= int(bit)
    return mask


def resolve_adv_config(gp_binds, kind, SCButtons, Keys, mode="gamepad"):
    """Build the AdvPressEngine inputs for one kind's gamepad binds:
      controls = {cid: {"bit", "base", "long", "double"}} for every control
                 with a long/double row (base = the control's regular bind's
                 spec, pulsed on a short/single press; None when the control
                 is analog/none so nothing pulses).
      softs    = [(cid, "lt"|"rt", spec), ...] soft-pull rows (trigger cids).
      shifts   = {shift_bit: {target_bit: spec}} mode-shift layers  while
                 the shift button is held, each held target emits its spec
                 instead of its normal binding.
      plus     = {bit: [spec, ...]} extra actions asserted while the control
                 is held ON TOP of its normal binding (never masked).
      owned    = OR-mask of the long/double controls' bits + every shift
                 HOLDER's bit  the tray masks these out of the normal
                 button_map / key-override paths permanently. (Shift TARGET
                 bits join the engine's per-frame mask only while their
                 shift is held  see AdvPressEngine.frame_mask.) The GUIDE
                 bits are never in here: a Guide row rides ALONGSIDE the
                 chord layer instead of taking the button over (see
                 SC_ADV_DESKTOP_BUTTONS).
    `mode` picks the tab: "gamepad" (default  specs may be Xbox outputs or
    key actions) or "pc" (the Desktop tab  key actions only; the control
    tables and base-action defaults come from the desktop layout).
    Returns (controls, softs, shifts, plus, owned)."""
    binds = gp_binds or {}
    hid = kind in SC_ID_SPACE_KINDS
    if mode == "pc":
        buttons = SC_ADV_DESKTOP_BUTTONS if hid else SDL_ADV_DESKTOP_BUTTONS
        defaults = SC_DESKTOP_DEFAULTS if hid else SDL_DESKTOP_DEFAULTS
        spec_of = _adv_spec_pc
    else:
        buttons = SC_GAMEPAD_BUTTONS if hid else SDL_GAMEPAD_BUTTONS
        defaults = SC_GAMEPAD_DEFAULTS if hid else SDL_GAMEPAD_DEFAULTS
        spec_of = _adv_spec
    guide_cids = adv_guide_cids(kind)
    trig = {("l2" if hid else "zl"): "lt", ("r2" if hid else "zr"): "rt"}
    controls = {}
    softs = []
    shifts = {}
    plus = {}
    owned = 0
    for r in adv_rows(binds):
        cid, press = r["control"], r["press"]
        attr = buttons.get(cid)
        bit = getattr(SCButtons, attr, None) if attr else None
        if not bit:
            continue
        spec = spec_of(r["action"], Keys)
        if spec is None:
            continue
        if press == "soft":
            side = trig.get(cid)
            if side:
                softs.append((cid, side, spec))
            continue
        if press == "shift":
            tattr = buttons.get(r.get("target", ""))
            tbit = getattr(SCButtons, tattr, None) if tattr else None
            if tbit:
                shifts.setdefault(int(bit), {})[int(tbit)] = spec
                if cid not in guide_cids:
                    owned |= int(bit)
            continue
        if press == "plus":
            plus.setdefault(int(bit), []).append(spec)
            continue
        ent = controls.get(cid)
        if ent is None:
            # A Guide row has no base to pulse  the guide-tap path owns the
            # short press (see SC_ADV_DESKTOP_BUTTONS).
            base_val = binds.get(cid) or defaults.get(cid, "none")
            ent = {"bit": int(bit),
                   "base": (None if cid in guide_cids
                            else spec_of(base_val, Keys)),
                   "long": None, "double": None}
            controls[cid] = ent
        ent[press] = spec
    for cid, ent in controls.items():
        if cid not in guide_cids:
            owned |= ent["bit"]
    return controls, softs, shifts, plus, owned


class AdvPressEngine:
    """Per-frame press-decision state machine for the long/double rows plus
    the soft-pull comparators. Feed every input frame; read back the specs
    currently asserted. Pure logic  no I/O, `now` injected  so it's unit-
    testable and shared verbatim by the SC watcher and the SDL pad loop.

    step(buttons, ltrig, rtrig, now, enabled=True) returns
      {slot: spec}  the specs asserted THIS frame, keyed by a stable slot id
      ("cid:long" / "cid:double" / "cid:base" / "cid:soft"). The caller diffs
      against the previous frame's dict: xusb specs OR their flag into the pad
      while present; key specs press on appearance, release on disappearance
      (tap/combo/system key actions fire once on appearance).
    `enabled=False` (e.g. while Steam/Guide is held) releases everything and
    freezes the timers so a chord can't leak a stray pulse  EXCEPT the rows
    that live on the Guide button itself (`guide_bit`), which by definition
    only ever act during such a hold. Those keep stepping, and their bits are
    kept out of owned_mask/frame_mask, so a Long Press on Guide and the whole
    chord layer coexist (see adv_guide_mask).

    Press thresholds come from `timing` (see adv_timing)  per controller, so
    two pads plugged in at once can each have their own long-press feel."""

    def __init__(self, controls, softs, shifts=None, plus=None, timing=None,
                 guide_bit=0):
        self._controls = controls
        self._softs = softs
        self._shifts = shifts or {}
        self._plus = plus or {}
        # `timing` is one controller's tuned thresholds (see adv_timing);
        # omitting it keeps the shipped defaults, so every call site that
        # doesn't care stays a plain constructor call. Named _thr because
        # self._soft_on below is the per-control hysteresis STATE.
        (self._long_s, self._double_s,
         self._soft_on_thr, self._soft_off_thr) = timing or (
            LONG_PRESS_S, DOUBLE_GAP_S, SOFT_PULL_ON, SOFT_PULL_OFF)
        self._st = {cid: {"state": "idle", "t": 0.0}
                    for cid in controls}
        self._soft_on = {}
        self._guide_bit = int(guide_bit or 0)
        self.owned_mask = 0
        for ent in controls.values():
            self.owned_mask |= ent["bit"]
        for sbit in self._shifts:
            self.owned_mask |= sbit
        # The Guide bits are never taken over: the chord layer, the guide-hold
        # binds and the guide TAP all still need to see the button.
        self.owned_mask &= ~self._guide_bit
        # owned_mask + this frame's held-shift TARGET bits (recomputed by
        # step)  the mask the tray applies to the outgoing frame.
        self.frame_mask = self.owned_mask

    def step(self, buttons, ltrig, rtrig, now, enabled=True):
        out = {}
        raw = int(buttons)
        if not enabled:
            # "Disabled" means the Guide button is down (the chord layer has
            # priority). Rows ON the Guide button are the exception  that hold
            # IS their gesture  so instead of releasing everything, the frame
            # is narrowed to the Guide bits and every other row is reset.
            buttons = raw & self._guide_bit
            ltrig = rtrig = 0
            for cid, st in self._st.items():
                if not (self._controls[cid]["bit"] & self._guide_bit):
                    st["state"] = "idle"
            self._soft_on.clear()
            if not self._guide_bit:
                self.frame_mask = self.owned_mask
                return out
        # Mode-shift layers: while a shift holder is held, its targets are
        # masked from the normal paths (frame_mask) and emit their shifted
        # spec while pressed. A layer HELD BY the Guide button reads the raw
        # frame  its targets are ordinary buttons, which the narrowing above
        # has just cleared.
        dyn = 0
        for sbit, targets in self._shifts.items():
            src = raw if (sbit & self._guide_bit) else buttons
            if src & sbit:
                for tbit, spec in targets.items():
                    dyn |= tbit
                    if src & tbit:
                        out["shift:%x:%x" % (sbit, tbit)] = spec
        self.frame_mask = self.owned_mask | dyn
        # "plus" rows: extra actions ride ON TOP of the control's normal
        # binding while it's held. A control currently REPLACED by a held
        # shift layer (dyn) pauses its extras too  the layer swaps the
        # whole binding, exactly like Steam.
        for pbit, specs in self._plus.items():
            if buttons & pbit and not (dyn & pbit):
                for i, spec in enumerate(specs):
                    out["plus:%x:%d" % (pbit, i)] = spec
        for cid, ent in self._controls.items():
            st = self._st[cid]
            down = bool(buttons & ent["bit"])
            state = st["state"]
            if state == "idle":
                if down:
                    st["state"], st["t"] = "pressed", now
            elif state == "pressed":
                if down:
                    if ent["long"] and now - st["t"] >= self._long_s:
                        st["state"] = "long"
                else:
                    if ent["double"]:
                        st["state"], st["t"] = "wait2", now
                    elif ent["base"] is not None:
                        st["state"], st["t"] = "pulse", now
                    else:
                        st["state"] = "idle"
            elif state == "long":
                if not down:
                    st["state"] = "idle"
            elif state == "wait2":
                if down:
                    st["state"] = "double"
                elif now - st["t"] >= self._double_s:
                    if ent["base"] is not None:
                        st["state"], st["t"] = "pulse", now
                    else:
                        st["state"] = "idle"
            elif state == "double":
                if not down:
                    st["state"] = "idle"
            elif state == "pulse":
                if down:
                    st["state"], st["t"] = "pressed", now
                elif now - st["t"] >= PULSE_S:
                    st["state"] = "idle"
            state = st["state"]
            if state == "long":
                out[cid + ":long"] = ent["long"]
            elif state == "double":
                out[cid + ":double"] = ent["double"]
            elif state == "pulse":
                out[cid + ":base"] = ent["base"]
        for cid, side, spec in self._softs:
            analog = ltrig if side == "lt" else rtrig
            on = self._soft_on.get(cid, False)
            thr = self._soft_off_thr if on else self._soft_on_thr
            on = analog >= thr
            self._soft_on[cid] = on
            if on:
                out[cid + ":soft"] = spec
        return out


def resolve_sdl_gamepad_sticks(binds):
    """(lstick_map, rstick_map) for SDL gamepad-mode stick direction binds 
    mirror of resolve_sc_gamepad_sticks over SDL_GAMEPAD_DEFAULTS. None = full
    analog passthrough; a dict = the stick goes digital (axis zeroed, the
    bound output held while deflected into that zone)."""
    binds = binds or {}

    def _resolve(dirs):
        m = {}
        all_pass = True
        for zone, cid in dirs.items():
            val = binds.get(cid) or SDL_GAMEPAD_DEFAULTS.get(cid, "none")
            if val not in ("none", "analog"):
                all_pass = False
                m[zone] = val
        if all_pass:
            return None
        for zone in dirs:
            m.setdefault(zone, "none")
        return m

    return _resolve(_LSTICK_DIRS), _resolve(_RSTICK_DIRS)


# ---------------------------------------------------------------------------
# Virtual Menus (Steam-style touch menus on the SC / Steam Deck trackpads)
# ---------------------------------------------------------------------------
# A virtual menu = {"type": "touch" | "radial" | "hotbar", "name": str,
#                   "pad": "none" | "lpad" | "rpad",
#                   "entries": [{"icon": icon id, "action": pc action id}]}
# stored as settings["virtual_menus"] (a global list  the SC and the Deck
# share the HID runtime, so one menu set serves both). Touch = grid, thumb
# highlights + pad click fires; radial = donut sectors picked by thumb
# ANGLE; hotbar = linear strip CLICKED THROUGH (each pad click advances to
# the next slot and fires it  thumb position is ignored).
#
# Everything visual is single-sourced HERE: the picker's live preview, the
# tray's on-screen overlay and the pad hit-testing all use the same layout
# table and renderer, so what you see in Options is exactly what the
# trackpad drives.

VMENU_MAX_ENTRIES = 16

# Entry count -> row layout (cells per row, top to bottom). Mirrors Steam's
# balanced touch-menu grids (wider middle row; 4x4 at the 16 cap).
VMENU_LAYOUTS = {
    1: [1], 2: [2], 3: [3], 4: [2, 2], 5: [2, 1, 2], 6: [3, 3],
    7: [2, 3, 2], 8: [3, 2, 3], 9: [3, 3, 3], 10: [3, 4, 3],
    11: [4, 3, 4], 12: [4, 4, 4], 13: [4, 5, 4], 14: [4, 3, 3, 4],
    15: [4, 4, 4, 3], 16: [4, 4, 4, 4],
}

# On-screen cell base sizes (px, before the Size% / resolution scaling). The
# overlay window (vmenu.py) and the picker's aspect-matched sidebar preview
# both use these so the preview's SHAPE tracks the real menu as entries are
# added. `vmenu_natural_size(type, n)` returns the menu's base (w, h)  its
# true aspect ratio  for a given type + entry count.
VMENU_CELL_W = 132
VMENU_CELL_H = 100
VMENU_GRID_PAD = 8
VMENU_HOTBAR_CELL_W = 88
VMENU_HOTBAR_H = 96
VMENU_RADIAL_BASE = 420
# The DEFAULT (Size=100%) on-screen footprint of a menu  a square box the
# grid is FIT INTO (preserving its aspect), so adding entries subdivides the
# box into smaller cells instead of growing the whole menu. 220px at the 900px
# reference height (the 2.png design box on 1600x900); the overlay res-scales
# and size%-scales it (see vmenu.show).
VMENU_BOX_BASE = 220


def vmenu_natural_size(style, n):
    """Base (w, h) of a menu overlay for `style` ("touch"/"radial"/"hotbar")
    and `n` entries  the true aspect ratio, before Size%/resolution scaling.
    Touch grids reshape with the entry count (1=near-square, 2=wide, 4=2x2,
    6=3x2, ...); radial is always square; hotbar is a 1-row strip."""
    n = max(1, int(n))
    if style == "radial":
        return (VMENU_RADIAL_BASE, VMENU_RADIAL_BASE)
    if style == "hotbar":
        return (VMENU_HOTBAR_CELL_W * n + VMENU_GRID_PAD, VMENU_HOTBAR_H)
    rows = vmenu_layout(n)
    cols = max(rows)
    # Only the FULL 4x4 grid (the 16-entry cap) uses SQUARE cells so it fills
    # the square box as 51x51 buttons. Every other count keeps the original
    # 132x100 cell aspect (its grid reshapes / letterboxes inside the box).
    if n == VMENU_MAX_ENTRIES and cols == len(rows):
        cw = ch = VMENU_CELL_W
    else:
        cw, ch = VMENU_CELL_W, VMENU_CELL_H
    return (cw * cols + VMENU_GRID_PAD, ch * len(rows) + VMENU_GRID_PAD)

# Icon set: real Steam Input icon art. Two sources, both bundled as PNGs in
# data/images/vmenu_icons/<id>.png (both trees; "none" shows the entry's
# action label as text instead; any id with no bundled asset  a stale or
# hand-edited settings file  falls back to a plain circle outline, never
# a crash):
#   - the "Controls" group: control-glyph SVGs ripped from the Steam
#     client's own bundled art (controller_base/images/api/knockout, the
#     white-on-transparent theme) via scripts/rip_vmenu_icons.py.
#   - everything else: the SAME generic action-icon library Steam's own
#     touch-menu "Select an Icon" browser offers (weapons, ammo,
#     inventory, magic, movement, ...)  captured by driving that real
#     Steam UI and cropping each grid cell (scratchpad
#     extract_steam_icons.py; that library is served from Valve's CDN on
#     demand, not bundled with the client, so it can't be grabbed from
#     local files  Steam's own window has to render it first).
# VMENU_ICON_GROUPS = [(label, [icon ids]), ...]  the picker renders one
# section per group; VMENU_ICONS is the flat id list (validation, "none"
# first) derived from it.
VMENU_ICON_GROUPS = (
    ("Weapons", tuple("weapon_%02d" % i for i in range(1, 79))),
    ("Ammo", tuple("ammo_%02d" % i for i in range(1, 9))),
    ("Inventory", tuple("inventory_%02d" % i for i in range(1, 46))),
    ("Magic", tuple("magic_%02d" % i for i in range(1, 63))),
    ("Actions", tuple("action_%02d" % i for i in range(1, 57))),
    ("Movement", tuple("movement_%02d" % i for i in range(1, 52))),
    ("Menu", tuple("menu_%02d" % i for i in range(1, 28))),
    ("Vehicle", tuple("vehicle_%02d" % i for i in range(1, 18))),
    ("Utility", tuple("utility_%02d" % i for i in range(1, 20))),
    ("Input", tuple("input_%02d" % i for i in range(1, 24))),
    ("Media", tuple("media_%02d" % i for i in range(1, 26))),
    ("Targets", tuple("target_%02d" % i for i in range(1, 18))),
    ("Social", tuple("social_%02d" % i for i in range(1, 26))),
    # Steam's real picker calls this last tab "Other" (its own content is
    # just A/B/C/X/Y/Z letter glyphs); ours is a richer set of gamepad
    # control/mouse/keyboard glyphs useful for labeling menu buttons that
    # trigger OTHER virtual-menu buttons or system actions, kept under the
    # same name+position as Steam's tab order.
    ("Other", (
        "a", "b", "c", "x", "y", "z", "face_n", "face_e", "face_s", "face_w",
        "circle",
        "dpad", "dpad_up", "dpad_down", "dpad_left", "dpad_right",
        "gyro", "gyro_pitch", "gyro_roll", "gyro_yaw",
        "l3", "r3",
        "lstick", "lstick_click", "lstick_up", "lstick_down", "lstick_left",
        "lstick_right", "lstick_touch",
        "rstick", "rstick_click", "rstick_up", "rstick_down", "rstick_left",
        "rstick_right", "rstick_touch",
        "paddle_l", "paddle_r", "lm", "rm",
        "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8",
        "mouse_left", "mouse_right", "mouse_middle", "mouse_4", "mouse_5",
        "scroll_up", "scroll_down",
        "touch", "touch_tap", "touch_doubletap",
        "keyboard", "mouse", "magnifier", "screenshot", "action_set",
        "set_led", "camera_reset", "dpi_calibration",
    )),
)
VMENU_ICONS = ("none",) + tuple(
    icon for _label, ids in VMENU_ICON_GROUPS for icon in ids)

# --- Bundled-but-unlisted icons ("preset_*", "tutorial_*") ------------------
# A second bundled namespace that lives in the SAME data/images/vmenu_icons
# folder and renders through the same loader, but is deliberately absent from
# VMENU_ICON_GROUPS  so it works everywhere art is drawn and appears nowhere
# in the picker's "Select Icon" browser (which is built from the groups; see
# _open_vmenu_icon_modal). That is what the namespace is FOR: these are app
# art, not a library the user picks from.
#
#   preset_*    the icons on the shipped default menu (default_virtual_menus)
#   tutorial_*  the first-run tour's own demo menu (tutorial._GABEN_ICON)
#
# The consequence worth knowing: an id with no bundled PNG falls back to a
# plain circle outline rather than failing loudly, so a renamed/removed file
# here degrades silently. Keep the names and the settings that reference them
# in step  they are a fixed contract, not free-form filenames.
VMENU_PRESET_ICON_PREFIX = "preset_"

# --- User-uploaded ("custom") icons -----------------------------------------
# On top of the bundled library above, a user can supply their OWN image for a
# menu button (picker: the "Custom" tab, first in the icon browser). Such an
# icon id is the bundled-id namespace's one exception: it carries the
# VMENU_CUSTOM_PREFIX and its PNG lives in a WRITABLE dir next to settings.json
# (import_custom_vmenu_icon), NOT in the read-only bundle. Everything else
# (settings storage, sanitize, the overlay renderer, the picker cache) treats a
# custom id like any other id  _load_vmenu_icon_asset just resolves the prefix
# to the writable dir. A dangling custom id (file deleted out from under a saved
# config) falls back to the same plain-circle outline as any unknown id.
VMENU_CUSTOM_PREFIX = "custom:"
# Uploaded art is normalized to a fixed square canvas so it matches the bundled
# 128x128 icons and downscales just as crisply in every menu size.
VMENU_CUSTOM_ICON_PX = 128
# Fraction of the canvas the (trimmed) content fills  leaves a small margin so
# a custom icon doesn't touch the tile edges, roughly matching the breathing
# room the bundled Steam glyphs have (their content spans ~91% of 128).
VMENU_CUSTOM_CONTENT_FRAC = 0.90
# Sanity gates on the SOURCE file the user picks (friendly rejection, never a
# crash or a decompression bomb): cap the on-disk size and the decoded pixel
# dimensions before we touch the pixels.
VMENU_CUSTOM_MAX_BYTES = 12 * 1024 * 1024      # 12 MB
VMENU_CUSTOM_MAX_DIM = 8192                     # px per side (pre-normalize)
# Formats we accept  the common lossy/lossless raster set Pillow decodes, with
# alpha preserved where the format carries it (PNG / WEBP / GIF / TIFF / ICO).
VMENU_CUSTOM_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
                     ".tiff", ".tif", ".ico")
# --- Program icons ----------------------------------------------------------
# The Custom tab can also take a PROGRAM (a .desktop launcher, or an executable)
# and lift the icon the desktop shows for it, so binding "launch Firefox" to a
# menu button doesn't mean hunting down a PNG of the Firefox logo first. The
# extracted image then goes through the very same normalize/store path an
# uploaded image does.
#
# Linux has no icon-in-the-binary convention: the icon lives in the freedesktop
# .desktop entry's `Icon=` key, resolved against the icon theme dirs below. A
# plain executable therefore only works if a .desktop file names it.
VMENU_PROGRAM_EXTS = (".desktop", ".png", ".svg", ".svgz", ".xpm")
# The size we prefer when an icon theme offers several. Larger than the canvas
# so the LANCZOS downscale has pixels to work with.
VMENU_PROGRAM_ICON_PX = 256
# Where installed apps put their launchers and artwork (XDG_DATA_DIRS order,
# with the per-user dir first). Expanded at call time, not import time.
VMENU_DESKTOP_DIRS = ("~/.local/share/applications",
                      "/usr/local/share/applications",
                      "/usr/share/applications",
                      "/var/lib/flatpak/exports/share/applications",
                      "~/.local/share/flatpak/exports/share/applications",
                      "/var/lib/snapd/desktop/applications")
VMENU_ICON_THEME_DIRS = ("~/.local/share/icons", "~/.icons",
                         "/usr/local/share/icons", "/usr/share/icons",
                         "/var/lib/flatpak/exports/share/icons",
                         "~/.local/share/flatpak/exports/share/icons",
                         "/usr/share/pixmaps")

# --- Custom TEXT "icons" ----------------------------------------------------
# A menu button can carry a short WORD instead of a glyph ("RELOAD", "MAP", …).
# The picker offers it on the Custom tab, right beside the "+" upload tile. Like
# an upload it is just another id in the entry's `icon` field  here the
# VMENU_TEXT_PREFIX namespace carrying the literal label (`text:RELOAD`)  so it
# round-trips through settings, sanitize, the overlay renderer and the picker
# with no extra plumbing. Unlike an upload NOTHING is written to disk: the
# string IS the icon.
VMENU_TEXT_PREFIX = "text:"
VMENU_TEXT_MAX_LEN = 16      # keeps a label readable at real cell sizes
VMENU_TEXT_MAX_LINES = 2     # word-wrapped over at most this many lines
VMENU_TEXT_MIN_PX = 6        # smallest font tried before ellipsising instead
# Fraction of a cell the label may span, so text keeps the same breathing room
# the glyphs have rather than running into the box edges.
VMENU_TEXT_PAD_FRAC = 0.84


def is_text_vmenu_icon(icon):
    return isinstance(icon, str) and icon.startswith(VMENU_TEXT_PREFIX)


def vmenu_icon_text(icon):
    """The literal label carried by a `text:` id ("" for every other id)."""
    return icon[len(VMENU_TEXT_PREFIX):] if is_text_vmenu_icon(icon) else ""


def make_text_vmenu_icon(text):
    """Build a `text:<label>` id from raw user input: unprintable characters
    (newlines, tabs, control codes) become spaces, runs of whitespace collapse
    to one, and the result is trimmed to VMENU_TEXT_MAX_LEN. Empty input gives
    "none", so clearing the box is the same as picking no icon at all."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    s = " ".join(s.split())[:VMENU_TEXT_MAX_LEN].strip()
    return (VMENU_TEXT_PREFIX + s) if s else "none"


# Overlay / preview palette (matches the mock screenshots the layouts were
# designed against).
# NB: there is NO backdrop behind a touch menu  the boxes float on a
# transparent image; each box is a rounded rectangle (see render_vmenu_image).
VMENU_CELL = (35, 36, 40, 255)        # idle box fill
VMENU_CELL_EDGE = (51, 53, 59, 255)   # idle box 1px edge
VMENU_HL = (16, 55, 83, 255)          # highlighted (hover) box fill
VMENU_HL_EDGE = (26, 159, 255, 255)   # accent (radial edge / legacy)
VMENU_HL_RIDGE = (26, 159, 255, 255)  # 2px accent ridge across a hover box top
VMENU_FG = (207, 211, 218, 255)       # icon / label color
VMENU_CELL_GAP = 5                    # transparent px between boxes
VMENU_HL_RIDGE_PX = 2                 # ridge height (px)


def vmenu_layout(n):
    """Row layout for `n` entries (clamped into 1..VMENU_MAX_ENTRIES)."""
    n = max(1, min(int(n or 1), VMENU_MAX_ENTRIES))
    return VMENU_LAYOUTS[n]


def vmenu_cell_rects(n, w, h, gap=VMENU_CELL_GAP, rows=None):
    """Pixel rects [(x0, y0, x1, y1), ...] for `n` entries inside a w x h
    canvas  one rect per entry, reading order. Rows split the height
    evenly; each row's cells split the full width evenly (matching Steam's
    look where a 1-cell row spans the whole menu). `rows` overrides the
    balanced layout (the Hot Bar passes [n] for its single strip)."""
    if rows is None:
        rows = vmenu_layout(n)
    rects = []
    rh = (h - gap) / max(1, len(rows))
    y = float(gap)
    for cols in rows:
        cw = (w - gap) / max(1, cols)
        x = float(gap)
        for _ in range(cols):
            rects.append((int(x), int(y), int(x + cw - gap),
                          int(y + rh - gap)))
            x += cw
        y += rh
    return rects


def vmenu_corner_flags(rows):
    """[(top_left, top_right, bottom_right, bottom_left), ...] per cell, in
    the same reading order as vmenu_cell_rects  ONLY the 4 outermost cells of
    the whole menu (the actual corners of its bounding box) get a rounded
    corner, and each gets exactly the ONE corner that's on the outside. Every
    row's leftmost/rightmost cell touches the menu's left/right edge (rows
    split the full width), so "first in row 0" is the menu's true top-left
    regardless of how many columns that row has, etc. A 1-row menu (or a
    single-entry menu) is simultaneously top+bottom on that side, so it gets
    BOTH corners on that side (a fully-rounded pill end)."""
    total = len(rows)
    out = []
    for r, cols in enumerate(rows):
        is_top = (r == 0)
        is_bottom = (r == total - 1)
        for c in range(cols):
            is_left = (c == 0)
            is_right = (c == cols - 1)
            out.append((is_top and is_left, is_top and is_right,
                       is_bottom and is_right, is_bottom and is_left))
    return out


def vmenu_cell_at(n, nx, ny):
    """Entry index under a normalized (0..1, y DOWN) touch position, or None
    when outside. Same layout math as vmenu_cell_rects."""
    if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
        return None
    rows = vmenu_layout(n)
    r = min(int(ny * len(rows)), len(rows) - 1)
    idx = sum(rows[:r])
    cols = rows[r]
    c = min(int(nx * cols), cols - 1)
    return idx + c


# How far (in normalized 0..1 pad units) the thumb must travel PAST the
# currently-highlighted cell's edge before the highlight switches. This
# hysteresis band straddles every boundary, so a finger resting on one can't
# rapid-fire flip A<->B (and spam the cell-change haptic) on pad jitter.
VMENU_HYST_MARGIN = 0.05


def vmenu_cell_at_hyst(n, nx, ny, cur, margin=VMENU_HYST_MARGIN):
    """Like vmenu_cell_at, but sticky: while `cur` (the currently-held index,
    or None) is highlighted, the thumb has to move `margin` beyond that cell's
    boundary before the highlight moves  the current cell's rect is grown by
    `margin` on every side into a "hold zone", and any position inside it keeps
    `cur`. Outside it, the raw cell wins (so a genuine move, even a big jump,
    still tracks instantly). Returns the new index (or None off-menu)."""
    raw = vmenu_cell_at(n, nx, ny)
    if cur is None or raw is None or raw == cur:
        return raw
    rows = vmenu_layout(n)
    R = len(rows)
    acc = 0
    for r_cur, cols_cur in enumerate(rows):
        if cur < acc + cols_cur:
            c_cur = cur - acc
            break
        acc += cols_cur
    else:
        return raw               # cur is stale (out of range)  take the raw
    # The current cell's normalized rect (see vmenu_cell_at's band math),
    # grown by `margin`; still inside it => keep the current highlight.
    if (c_cur / cols_cur - margin <= nx <= (c_cur + 1) / cols_cur + margin
            and r_cur / R - margin <= ny <= (r_cur + 1) / R + margin):
        return cur
    return raw


def vmenu_neighbor(style, n, cur, direction):
    """Index reached by stepping `direction` ("up"/"down"/"left"/"right") from
    the currently highlighted `cur`  the keyboard-navigation twin of the
    thumb's vmenu_cell_at (see the tray's _KeyVMenuRunner, where the arrow keys
    move between boxes while a key-triggered menu is open).

    `cur` None (or stale) lands on the first box. Movement WRAPS, so holding a
    direction cycles rather than dead-ends. A radial menu has no rows, so
    right/down step clockwise and left/up counter-clockwise; a hot bar is one
    row. In a touch grid, left/right wrap inside the current row and up/down
    change row while keeping the box's horizontal CENTER  rows can hold
    different numbers of boxes (see VMENU_LAYOUTS), so the nearest column of
    the target row wins rather than a raw column index."""
    n = max(1, min(int(n or 1), VMENU_MAX_ENTRIES))
    if cur is None or not (0 <= int(cur) < n):
        return 0
    cur = int(cur)
    if style == "radial":
        return (cur + (1 if direction in ("right", "down") else -1)) % n
    rows = [n] if style == "hotbar" else vmenu_layout(n)
    acc = 0
    for r, cols in enumerate(rows):
        if cur < acc + cols:
            break
        acc += cols
    else:
        return 0
    c = cur - acc
    if direction in ("left", "right"):
        return acc + (c + (1 if direction == "right" else -1)) % cols
    r2 = (r + (1 if direction == "down" else -1)) % len(rows)
    cols2 = rows[r2]
    # Horizontal center of the current box, remapped onto the target row.
    c2 = min(cols2 - 1, max(0, int((c + 0.5) / cols * cols2)))
    return sum(rows[:r2]) + c2


_VMENU_FONT_CACHE = {}         # px -> ImageFont


def _vmenu_font(px):
    """Best-effort truetype for icon letters / labels (falls back to PIL's
    default bitmap font). Cached by size  a label is measured at several
    candidate sizes per fit (see _vmenu_text_layout) and re-rendered every
    overlay frame, so opening the font file each time would be pure waste."""
    hit = _VMENU_FONT_CACHE.get(px)
    if hit is not None:
        return hit
    from PIL import ImageFont
    font = None
    for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf",
                 "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, px)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    _VMENU_FONT_CACHE[px] = font
    return font


_VMENU_TEXT_SCRATCH = [None]   # 1x1 ImageDraw used purely to measure text
_VMENU_TEXT_LAYOUT_CACHE = {}  # (text, box_w, box_h, max_lines) -> (px, lines)


def _vmenu_measure_ctx():
    if _VMENU_TEXT_SCRATCH[0] is None:
        from PIL import Image, ImageDraw
        _VMENU_TEXT_SCRATCH[0] = ImageDraw.Draw(
            Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    return _VMENU_TEXT_SCRATCH[0]


def _vmenu_text_w(d, s, font):
    bb = d.textbbox((0, 0), s, font=font)
    return bb[2] - bb[0]


def _vmenu_split_balanced(d, word, font, max_w, k):
    """Split `word` into exactly `k` contiguous chunks, minimizing the WIDEST
    chunk  i.e. the most balanced break, not the greedy one. None when even
    the best split leaves a chunk wider than max_w.

    Greedy breaking ("take everything that fits, leftovers next line") is what
    made a hard-broken word render as "yoooooo / oo": the fit search then
    settles on the largest font where that lopsided split still fits. Balancing
    gives "yoooo / oooo" AND a bigger font, since the widest line is shorter.

    Exhaustive DP over cut positions  a label is capped at
    VMENU_TEXT_MAX_LEN characters and k is at most VMENU_TEXT_MAX_LINES, so the
    search is tiny (and the whole layout is cached by the caller anyway)."""
    n = len(word)
    if k > n:
        return None
    memo = {}

    def W(i, j):
        hit = memo.get((i, j))
        if hit is None:
            hit = memo[(i, j)] = _vmenu_text_w(d, word[i:j], font)
        return hit

    best = {}

    def solve(i, r):
        """(widest chunk, [chunk, ...]) for word[i:] split into r chunks."""
        hit = best.get((i, r))
        if hit is not None:
            return hit
        if r == 1:
            hit = (W(i, n), [word[i:]])
        else:
            hit = None
            # Longest-first so that among equally-balanced splits the EARLIER
            # line is the fuller one (the usual typographic preference).
            for j in range(n - r + 1, i, -1):
                rest_w, rest = solve(j, r - 1)
                cand_w = max(W(i, j), rest_w)
                if hit is None or cand_w < hit[0]:
                    hit = (cand_w, [word[i:j]] + rest)
        best[(i, r)] = hit
        return hit

    widest, chunks = solve(0, k)
    return chunks if widest <= max_w else None


def _vmenu_text_wrap(d, text, font, max_w, max_lines):
    """Greedy word-wrap `text` into at most `max_lines` lines no wider than
    `max_w` px, or None when it doesn't fit at this font size.

    A word too wide for a whole line is HARD-BROKEN across the remaining
    lines (balanced, via _vmenu_split_balanced) rather than failing the fit 
    "RELO / AD" at a legible size beats the same word shrunk to an unreadable
    single line, which is what the fit search would otherwise settle for."""
    lines = []
    cur = ""
    for word in text.split():
        cand = (cur + " " + word) if cur else word
        if _vmenu_text_w(d, cand, font) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            if len(lines) >= max_lines:
                return None
            cur = ""
        if _vmenu_text_w(d, word, font) <= max_w:
            cur = word
            continue
        # Fewest lines first, so a word only ever takes the space it needs.
        for k in range(2, max_lines - len(lines) + 1):
            chunks = _vmenu_split_balanced(d, word, font, max_w, k)
            if chunks is not None:
                lines.extend(chunks)
                break
        else:
            return None              # can't be broken small enough here
    if cur:
        lines.append(cur)
    return lines if lines and len(lines) <= max_lines else None


def _vmenu_text_ellipsise(d, text, font, max_w, max_lines):
    """Last-resort fit for a box too small for the whole label at even the
    minimum size: keep as much of the text as fits (wrapping across the
    allowed lines) and mark the cut with an ellipsis."""
    lines = []
    rest = text
    while rest and len(lines) < max_lines:
        take = rest
        while take and _vmenu_text_w(d, take, font) > max_w:
            take = take[:-1]
        if not take:
            break
        lines.append(take)
        rest = rest[len(take):].lstrip()
    if not lines:
        return [text[:1]]
    if rest:
        last = lines[-1]
        while last and _vmenu_text_w(d, last + "…", font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _vmenu_text_layout(text, box_w, box_h, max_lines):
    """(font_px, [line, ...]) fitting `text` into a box_w x box_h area  the
    largest font size (binary-searched) whose word-wrap fits both the line
    budget and the height, falling back to VMENU_TEXT_MIN_PX with an
    ellipsis when even that can't hold it. Cached: the overlay re-renders
    every frame while a menu is open, and cells are a stable size."""
    key = (text, box_w, box_h, max_lines)
    hit = _VMENU_TEXT_LAYOUT_CACHE.get(key)
    if hit is not None:
        return hit
    d = _vmenu_measure_ctx()

    def fits(px):
        font = _vmenu_font(px)
        lines = _vmenu_text_wrap(d, text, font, box_w, max_lines)
        if lines is None:
            return None
        asc, desc = font.getmetrics()
        return lines if (asc + desc) * len(lines) <= box_h else None

    best = None
    lo, hi = VMENU_TEXT_MIN_PX, max(VMENU_TEXT_MIN_PX, min(box_h, 64))
    while lo <= hi:
        mid = (lo + hi) // 2
        lines = fits(mid)
        if lines is None:
            hi = mid - 1
        else:
            best = (mid, lines)
            lo = mid + 1
    if best is None:
        px = VMENU_TEXT_MIN_PX
        best = (px, _vmenu_text_ellipsise(d, text, _vmenu_font(px), box_w,
                                          max_lines))
    if len(_VMENU_TEXT_LAYOUT_CACHE) > 256:   # bound growth across configs
        _VMENU_TEXT_LAYOUT_CACHE.clear()
    _VMENU_TEXT_LAYOUT_CACHE[key] = best
    return best


def draw_vmenu_text(img, rect, text, color=VMENU_FG,
                    max_lines=VMENU_TEXT_MAX_LINES):
    """Paint a custom label (a `text:` icon) centered inside `rect` on the RGBA
    image `img`, auto-sized to the largest font that fits. This is the twin of
    draw_vmenu_icon: same job, letters instead of a glyph."""
    from PIL import ImageDraw
    text = " ".join(str(text or "").split())
    if not text:
        return
    x0, y0, x1, y1 = rect
    box_w = max(1, int((x1 - x0) * VMENU_TEXT_PAD_FRAC))
    box_h = max(1, int((y1 - y0) * VMENU_TEXT_PAD_FRAC))
    px, lines = _vmenu_text_layout(text, box_w, box_h, max_lines)
    font = _vmenu_font(px)
    asc, desc = font.getmetrics()
    line_h = asc + desc
    d = ImageDraw.Draw(img)
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    top = cy - (line_h * len(lines)) / 2.0
    for i, line in enumerate(lines):
        bb = d.textbbox((0, 0), line, font=font)
        d.text((cx - (bb[0] + bb[2]) / 2.0,
                top + i * line_h + (line_h - (bb[1] + bb[3])) / 2.0),
               line, font=font, fill=color)


_VMENU_ICON_DIR = None
_VMENU_ICON_CACHE = {}         # icon id -> source RGBA asset (or None)
_VMENU_ICON_SCALED_CACHE = {}  # (icon id, px size) -> LANCZOS-scaled RGBA


def vmenu_icon_asset_dir():
    """data/images/vmenu_icons  resolved relative to this file (source
    tree) or sys._MEIPASS (frozen build), exactly like every other bundled
    image path in this app."""
    global _VMENU_ICON_DIR
    if _VMENU_ICON_DIR is None:
        import os
        import sys
        base = getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(__file__)))
        _VMENU_ICON_DIR = os.path.join(base, "data", "images", "vmenu_icons")
    return _VMENU_ICON_DIR


def vmenu_custom_icon_dir(create=False):
    """The WRITABLE folder holding user-uploaded icons, alongside settings.json
    (i.e. next to the EXE when frozen, next to this module in the source tree).
    NEVER the read-only _MEIPASS bundle, which can't be written and is wiped on
    the next launch. `create=True` makes the folder if it's missing (used
    before a write).

    Mirrors tray.py's _settings_paths fallback: on a read-only install dir
    (Program Files, an admin-owned folder) the exe-dir location can't be
    created, and uploads would fail silently for the whole session. Fall back
    to the same per-user config dir settings.json uses, so the two stay
    together wherever they land. An EXISTING exe-dir folder always wins, so a
    portable install that already has icons keeps using them."""
    import os
    import sys
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "custom_icons")
    if os.path.isdir(d):
        return d                      # portable install already established
    if create:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            pass                      # read-only install -> per-user fallback
    if sys.platform == "win32":
        cfg = os.environ.get("APPDATA")
    else:
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    if not cfg:
        return d                      # no fallback available; keep old behavior
    alt = os.path.join(cfg, "SteamlessInput", "custom_icons")
    if create:
        try:
            os.makedirs(alt, exist_ok=True)
        except OSError:
            return d
    elif not os.path.isdir(alt):
        return d                      # nothing anywhere yet -> report exe-dir
    return alt


def is_custom_vmenu_icon(icon):
    return isinstance(icon, str) and icon.startswith(VMENU_CUSTOM_PREFIX)


def _custom_icon_path(icon):
    """Absolute PNG path for a `custom:<name>` id (the name is the on-disk
    filename; the ':' never touches the filesystem)."""
    import os
    name = icon[len(VMENU_CUSTOM_PREFIX):]
    return os.path.join(vmenu_custom_icon_dir(), name + ".png")


def list_custom_vmenu_icons():
    """Every uploaded icon id (`custom:<name>`) currently on disk, NEWEST
    first  so a just-added icon shows at the front of the picker's Custom
    tab. Missing dir / unreadable entries simply yield an empty list."""
    import os
    d = vmenu_custom_icon_dir()
    out = []
    try:
        for fn in os.listdir(d):
            root, ext = os.path.splitext(fn)
            if ext.lower() == ".png" and root:
                try:
                    mt = os.path.getmtime(os.path.join(d, fn))
                except OSError:
                    mt = 0
                out.append((mt, VMENU_CUSTOM_PREFIX + root))
    except OSError:
        return []
    out.sort(key=lambda t: t[0], reverse=True)
    return [icon for _mt, icon in out]


def _purge_vmenu_icon_caches(icon):
    """Drop every cached asset/scaled copy of one icon id (after a delete or a
    re-import) so the next render/upload reloads it fresh from disk."""
    _VMENU_ICON_CACHE.pop(icon, None)
    for key in [k for k in _VMENU_ICON_SCALED_CACHE if k[0] == icon]:
        _VMENU_ICON_SCALED_CACHE.pop(key, None)


def import_custom_vmenu_icon(src_path):
    """Validate + normalize a user-picked image file into the custom-icon dir
    and return its new `custom:<name>` id.

    Normalization mirrors the bundled icons so uploads scale identically:
    honor any EXIF orientation, convert to RGBA (alpha preserved for formats
    that carry it), fit-inside a VMENU_CUSTOM_ICON_PX square with LANCZOS
    (aspect ratio kept, never stretched), and center on a transparent canvas.
    The filename is a content hash, so re-uploading the same image is a no-op
    that reuses the existing id (natural dedup).

    Raises ValueError with a short, user-facing message on any rejected input
    (missing file, too large, unreadable/unsupported, decode bomb)."""
    import os
    import hashlib
    try:
        from PIL import Image, ImageOps
    except Exception:
        raise ValueError("Image support is unavailable.")
    if not src_path or not os.path.isfile(src_path):
        raise ValueError("That file could not be found.")
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in VMENU_CUSTOM_EXTS:
        raise ValueError("Unsupported file type. Use PNG, JPG, GIF, BMP, "
                         "WEBP, TIFF or ICO.")
    try:
        size = os.path.getsize(src_path)
    except OSError:
        raise ValueError("That file could not be read.")
    if size > VMENU_CUSTOM_MAX_BYTES:
        raise ValueError("That image is too large (max %d MB)."
                         % (VMENU_CUSTOM_MAX_BYTES // (1024 * 1024)))
    try:
        with open(src_path, "rb") as f:
            raw = f.read()
    except OSError:
        raise ValueError("That file could not be read.")
    try:
        img = Image.open(src_path)
        img.load()
    except Image.DecompressionBombError:
        raise ValueError("That image is too large to process.")
    except Exception:
        raise ValueError("That file isn't a readable image.")
    if max(img.size) > VMENU_CUSTOM_MAX_DIM:
        raise ValueError("That image is too large (max %d px per side)."
                         % VMENU_CUSTOM_MAX_DIM)
    try:
        # Phone photos store rotation in EXIF rather than the pixels  apply it
        # so a portrait shot doesn't come in sideways.
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGBA")
    except Exception:
        raise ValueError("That image couldn't be processed.")
    return _store_custom_vmenu_icon(img, raw)


def _store_custom_vmenu_icon(img, hash_src):
    """Trim / fit / center an already-decoded RGBA image onto the standard
    custom-icon canvas, write it into the custom-icon dir, and return its new
    `custom:<name>` id.

    Shared by the image upload and the program-icon extraction so both land
    identically sized and centered. `hash_src` is the bytes the filename hash
    comes from  the original file for an upload, the extracted pixels for a
    program icon  so re-importing the same thing is a no-op that reuses the
    existing id."""
    import os
    import hashlib
    from PIL import Image
    px = VMENU_CUSTOM_ICON_PX
    # Trim the transparent border FIRST so the icon is centered on its actual
    # CONTENT (not on any lopsided padding the source carried) and fills the
    # tile  without this, a logo cropped to one corner imports tiny and skewed.
    # Use the ALPHA channel's bbox (getbbox() alone also counts colored-but-
    # transparent pixels some formats carry). A fully-opaque image has no
    # transparent border, so this is a harmless no-op there.
    bb = img.getchannel("A").getbbox()
    if bb:
        img = img.crop(bb)
    # Fit-inside a slightly-inset square (never distort), then center on the
    # transparent canvas  content ends up mathematically centered every time.
    w, h = img.size
    if w <= 0 or h <= 0:
        raise ValueError("That image couldn't be processed.")
    inner = max(1, int(round(px * VMENU_CUSTOM_CONTENT_FRAC)))
    scale = min(inner / w, inner / h)
    nw = max(1, min(px, int(round(w * scale))))
    nh = max(1, min(px, int(round(h * scale))))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    canvas.paste(img, ((px - nw) // 2, (px - nh) // 2), img)
    # Content hash of the ORIGINAL bytes → stable, collision-safe id that
    # dedups identical re-uploads.
    name = hashlib.sha1(hash_src).hexdigest()[:16]
    icon = VMENU_CUSTOM_PREFIX + name
    d = vmenu_custom_icon_dir(create=True)
    dest = os.path.join(d, name + ".png")
    try:
        canvas.save(dest, "PNG")
    except OSError:
        raise ValueError("Couldn't save the icon (is the folder writable?).")
    _purge_vmenu_icon_caches(icon)
    return icon


def _parse_desktop_entry(path):
    """The [Desktop Entry] group of a .desktop launcher as a flat dict.
    Deliberately a tiny hand-parse  no GLib dependency, and the format is one
    flat INI group we only need a couple of keys out of. Localized variants
    (Icon[de]=, Exec[fr]=, ...) are skipped in favor of the unlocalized key."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            in_entry = False
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_entry = (line == "[Desktop Entry]")
                    continue
                if not in_entry or "=" not in line or line.startswith("#"):
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in out:
                    out[key] = val.strip()
    except OSError:
        return {}
    return out


def _desktop_entry_icon_name(path):
    """The `Icon=` value from a .desktop launcher's [Desktop Entry] group, or
    None."""
    return _parse_desktop_entry(path).get("Icon") or None


def _desktop_entry_exec_argv(path):
    """The `Exec=` command line from a .desktop launcher, split into
    (argv0, extra_args_string) with freedesktop field codes (%f, %u, %c, ...)
    stripped out  or None if the entry has no Exec= line (or it's empty once
    the field codes are gone)."""
    import shlex
    val = _parse_desktop_entry(path).get("Exec")
    if not val:
        return None
    try:
        tokens = shlex.split(val)
    except ValueError:
        return None
    codes = {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c",
             "%k", "%v", "%m"}
    tokens = [t.replace("%%", "%") for t in tokens if t not in codes]
    if not tokens:
        return None
    return tokens[0], shlex.join(tokens[1:])


def _find_theme_icon_file(name):
    """Resolve a freedesktop icon NAME ("firefox") to a file on disk, or None.

    Walks the icon dirs preferring the largest raster size available (and any
    SVG last, since we can only use it if a renderer is installed). Not a full
    icon-theme-spec implementation with inheritance  a name-and-size sweep hits
    essentially every real launcher and needs no extra dependency."""
    import os
    exts = (".png", ".webp", ".xpm", ".svg", ".svgz")
    best = None            # (rank, size, path)
    for d in VMENU_ICON_THEME_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                stem, ext = os.path.splitext(fn)
                if stem != name or ext.lower() not in exts:
                    continue
                path = os.path.join(root, fn)
                vector = ext.lower() in (".svg", ".svgz")
                # Pull the pixel size out of the ".../256x256/apps/..." segment
                # the spec's directory layout uses; unknown => 0.
                size = 0
                for part in root.split(os.sep):
                    a, _x, b = part.partition("x")
                    if a.isdigit() and a == b:
                        size = int(a)
                        break
                # Prefer a raster at/above our target, then the biggest raster,
                # then anything vector.
                rank = (0 if vector else 1,
                        1 if size >= VMENU_PROGRAM_ICON_PX else 0, size)
                if best is None or rank > best[0]:
                    best = (rank, path)
    return best[1] if best else None


def _load_icon_file_image(path):
    """Any icon FILE (raster or SVG) as an RGBA PIL image, or None. SVGs need a
    renderer  cairosvg if it's installed, else rsvg-convert on PATH; without
    either we simply report no icon rather than failing the whole import."""
    import os
    import subprocess
    from PIL import Image
    ext = os.path.splitext(path)[1].lower()
    if ext in (".svg", ".svgz"):
        px = VMENU_PROGRAM_ICON_PX
        try:
            import cairosvg
            import io
            png = cairosvg.svg2png(url=path, output_width=px,
                                   output_height=px)
            return Image.open(io.BytesIO(png)).convert("RGBA")
        except Exception:
            pass
        try:
            import io
            png = subprocess.run(
                ["rsvg-convert", "-w", str(px), "-h", str(px), path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, check=True).stdout
            return Image.open(io.BytesIO(png)).convert("RGBA")
        except Exception:
            return None
    try:
        img = Image.open(path)
        img.load()
        return img.convert("RGBA")
    except Exception:
        return None


def extract_program_icon_image(path):
    """The icon the desktop shows for a program, as an RGBA PIL image. Raises
    ValueError with a user-facing message if there is none.

    Accepts a .desktop launcher (its `Icon=` is resolved against the icon
    themes), an icon file directly, or a plain executable  for which we look
    for a .desktop entry whose name matches, since a Linux binary carries no
    icon of its own."""
    import os
    if not path or not os.path.exists(path):
        raise ValueError("That file could not be found.")
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    icon_path = None
    if ext == ".desktop":
        name = _desktop_entry_icon_name(path)
        if not name:
            raise ValueError("That launcher doesn't name an icon.")
        # Icon= may be an absolute path OR a theme name.
        icon_path = name if os.path.isabs(name) and os.path.isfile(name) \
            else _find_theme_icon_file(name)
        if not icon_path:
            raise ValueError("That launcher's icon couldn't be found.")
    elif ext in (".png", ".svg", ".svgz", ".xpm", ".webp", ".jpg", ".jpeg"):
        icon_path = path
    else:
        # A bare executable: find the .desktop entry that launches it, then
        # recurse into the branch above.
        stem = os.path.basename(path)
        for d in VMENU_DESKTOP_DIRS:
            d = os.path.expanduser(d)
            if not os.path.isdir(d):
                continue
            cand = os.path.join(d, stem + ".desktop")
            if os.path.isfile(cand):
                return extract_program_icon_image(cand)
        name = _find_theme_icon_file(stem)
        if not name:
            raise ValueError("No icon is installed for that program.")
        icon_path = name
    img = _load_icon_file_image(icon_path)
    if img is None:
        raise ValueError("That program's icon couldn't be read.")
    if not img.getchannel("A").getbbox():
        img.putalpha(255)      # no alpha at all -> treat as opaque
    return img


def _tokenize_home_path(path):
    """An absolute path rewritten to start with $HOME in place of the current
    user's expanded home directory, if it lives under it. Left unchanged
    otherwise (e.g. a distro-packaged app under /usr or /opt, which is
    already the same path on every account on the machine).

    Both Spotify and Discord ship an official Linux build only as a per-user
    tarball/AppImage on some distros (Arch's AUR packages, snap-less installs,
    ...), landing under $HOME rather than /usr  storing that literally bakes
    in this one account's username, breaking the saved config for any other
    user or PC. _launch_program() (tray_linux.py) expands $HOME back at the
    moment it actually runs, on whichever account that turns out to be."""
    import os
    home = os.path.expanduser("~")
    norm = os.path.normpath(path)
    if norm == home or norm.startswith(home + os.sep):
        return "$HOME" + norm[len(home):]
    return path


# Sentinel "path" value a "Launch Program" hotkey can carry INSTEAD OF a real
# file path: "whatever the OS's default browser actually is"  resolved fresh
# every time the button fires (see resolve_default_browser_exe /
# tray_linux.py's _launch_program), not baked in at config time. Launched
# with no arguments, so it opens the browser's own start/new-tab page rather
# than any fixed site. Shared verbatim with the Windows tree so a config
# using it means the same thing on either platform.
VMENU_LAUNCH_DEFAULT_BROWSER = "$DEFAULT_BROWSER$"

# The same idea for Steam itself: a sentinel "path" resolved at fire time to
# however Steam is ACTUALLY installed here. The shipped default menu
# (default_virtual_menus) has two Steam buttons, and Linux has no single
# answer to "where is Steam"  a distro package, a Flatpak, or a Steam Deck's
# own copy are all normal  so the lookup has to happen on the machine.
# Unlike the browser sentinel this one KEEPS its stored args, because
# "-bigpicture" is the only thing separating the two buttons.
VMENU_LAUNCH_STEAM = "$STEAM$"


def resolve_steam_exe():
    """How to start Steam on this machine, re-resolved on every call (see
    VMENU_LAUNCH_STEAM): the `steam` wrapper on PATH if there is one  which
    is what a distro package, SteamOS and the Deck all provide  else the
    Flatpak's exported launcher, which flatpak installs as a real executable
    and which forwards its arguments to Steam (so "-bigpicture" still lands).
    None when Steam isn't installed at all, in which case the caller simply
    does nothing."""
    import os
    import shutil
    exe = shutil.which("steam")
    if exe:
        return exe
    for d in ("~/.local/share/flatpak/exports/bin",
              "/var/lib/flatpak/exports/bin"):
        cand = os.path.join(os.path.expanduser(d),
                            "com.valvesoftware.Steam")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def resolve_default_browser_exe():
    """The current default browser's executable, re-resolved on every call 
    so VMENU_LAUNCH_DEFAULT_BROWSER always reflects whatever's ACTUALLY
    default on THIS machine right now, not whichever browser happened to be
    default wherever the button was originally configured.

    Uses `xdg-settings get default-web-browser` (the freedesktop-standard way
    every major DE tracks this), resolves the .desktop file it names to its
    Exec= line, and returns just argv0  no args  so launching it opens the
    browser's own start page instead of navigating anywhere. None if it can't
    be resolved (xdg-settings missing, or the named .desktop file isn't found
    in any of the usual application dirs)."""
    import os
    import subprocess
    try:
        out = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=True).stdout
    except Exception:
        return None
    name = out.decode("utf-8", "replace").strip()
    if not name:
        return None
    for d in VMENU_DESKTOP_DIRS:
        d = os.path.expanduser(d)
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            got = _desktop_entry_exec_argv(cand)
            if got:
                return got[0]
            break
    return None


def resolve_program_launch_target(path):
    """The (target, args) a picked PROGRAM source should have the touch-menu
    button's cog-modal "Launch Program" hotkey run, or None if there's
    nothing launchable to extract (a plain icon file, or a .desktop entry
    lacking Exec=).

    A .desktop launcher resolves through its `Exec=` line (field codes like
    %u stripped). A bare executable first looks for a .desktop entry naming
    it  same lookup extract_program_icon_image uses for its icon  so
    picking the binary directly still gets the launcher's real argv (icon
    themes carry no argument info of their own, but many launchers set flags
    their app expects); failing that, the executable itself is the target.

    The resolved target is run through _tokenize_home_path() so a per-user
    install (Spotify/Discord on some distros ship this way) is stored
    PORTABLY  as a $HOME token expanded at launch time  instead of
    hardcoding this one account's username into the saved config."""
    import os
    if not path or not os.path.exists(path):
        return None
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".desktop":
        got = _desktop_entry_exec_argv(path)
        if got is None:
            return None
        return (_tokenize_home_path(got[0]), got[1])
    if ext in (".png", ".svg", ".svgz", ".xpm", ".webp", ".jpg", ".jpeg"):
        return None
    stem = os.path.basename(path)
    for d in VMENU_DESKTOP_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        cand = os.path.join(d, stem + ".desktop")
        if os.path.isfile(cand):
            got = _desktop_entry_exec_argv(cand)
            if got:
                return (_tokenize_home_path(got[0]), got[1])
            break
    if os.access(path, os.X_OK):
        return (_tokenize_home_path(path), "")
    return None


def _custom_icon_launch_path(icon):
    """Sidecar JSON path storing a program-sourced custom icon's resolved
    launch target, alongside its PNG."""
    import os
    name = icon[len(VMENU_CUSTOM_PREFIX):]
    return os.path.join(vmenu_custom_icon_dir(), name + ".launch.json")


def set_custom_vmenu_icon_launch_info(icon, path, args):
    """Record the (path, args) a program/launcher-sourced custom icon should
    launch, in a small JSON sidecar next to its PNG  later read by
    custom_vmenu_icon_launch_info() when the icon is actually picked for an
    entry, so the picker can auto-fill that entry's "Launch Program" hotkey.
    A no-op for non-custom ids or an empty path (nothing to remember)."""
    import json
    if not is_custom_vmenu_icon(icon) or not (path or "").strip():
        return
    try:
        with open(_custom_icon_launch_path(icon), "w", encoding="utf-8") as f:
            json.dump({"path": path, "args": args or ""}, f)
    except OSError:
        pass


def custom_vmenu_icon_launch_info(icon):
    """The (path, args) set_custom_vmenu_icon_launch_info() recorded for a
    program-sourced custom icon, or None if this icon has none (a plain
    upload, bundled art, custom text, or a sidecar that failed to write)."""
    import os
    import json
    if not is_custom_vmenu_icon(icon):
        return None
    src = _custom_icon_launch_path(icon)
    if not os.path.isfile(src):
        return None
    try:
        with open(src, "r", encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("path", ""), d.get("args", ""))
    except (OSError, ValueError):
        return None


def import_program_vmenu_icon(src_path):
    """Lift a program's icon into the custom-icon dir and return its
    `custom:<name>` id  the picker's "use a program's icon" tile.

    The extracted image goes through the same normalize/store path as an
    uploaded PNG, so the two are indistinguishable once imported. The id hashes
    the extracted PIXELS (not the source file, which may be large and changes on
    every update), so re-picking the same program  or picking both the binary
    and its launcher  dedups onto one icon.

    Also resolves the source's launch target (a .desktop entry's Exec=, or the
    source path itself) and stashes it in a sidecar keyed by the new icon id,
    so the picker can auto-fill "Launch Program" once this icon is actually
    assigned to a button."""
    img = extract_program_icon_image(src_path)
    try:
        img = img.convert("RGBA")
    except Exception:
        raise ValueError("That icon couldn't be processed.")
    stamp = ("%dx%d:" % img.size).encode("ascii") + img.tobytes()
    icon = _store_custom_vmenu_icon(img, stamp)
    target = resolve_program_launch_target(src_path)
    if target is not None:
        set_custom_vmenu_icon_launch_info(icon, target[0], target[1])
    return icon


def delete_custom_vmenu_icon(icon):
    """Remove an uploaded icon's file and purge its caches. Returns True if a
    file was actually deleted. A no-op for non-custom ids."""
    import os
    if not is_custom_vmenu_icon(icon):
        return False
    ok = False
    try:
        path = _custom_icon_path(icon)
        if os.path.isfile(path):
            os.remove(path)
            ok = True
    except OSError:
        pass
    try:
        lp = _custom_icon_launch_path(icon)
        if os.path.isfile(lp):
            os.remove(lp)
    except OSError:
        pass
    _purge_vmenu_icon_caches(icon)
    return ok


_VMENU_THUMB_CURSOR = [None]   # cached touchcircle.png (RGBA) or False
# Dimmed + resized cursor sprites, keyed by pixel size  a moving thumb hits
# this every frame, so we resize/dim ONCE per size instead of per redraw.
_VMENU_THUMB_DIM_CACHE = {}


def _load_vmenu_thumb_cursor():
    """The OSK thumb cursor art (data/images/glyphs/touchcircle.png), cached.
    None if it can't be loaded."""
    if _VMENU_THUMB_CURSOR[0] is None:
        import os
        import sys
        from PIL import Image
        base = getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "data", "images", "glyphs",
                            "touchcircle.png")
        try:
            _VMENU_THUMB_CURSOR[0] = Image.open(path).convert("RGBA")
        except Exception:
            _VMENU_THUMB_CURSOR[0] = False
    c = _VMENU_THUMB_CURSOR[0]
    return c if c else None


def _vmenu_dim_cursor(sz):
    """The thumb cursor resized to `sz` px and pre-dimmed to ~65% alpha,
    cached by size (the LANCZOS resize + per-pixel alpha scale is otherwise
    redone every single thumb frame). Returns an RGBA image or None."""
    hit = _VMENU_THUMB_DIM_CACHE.get(sz)
    if hit is not None:
        return hit
    cur = _load_vmenu_thumb_cursor()
    if cur is None:
        return None
    from PIL import Image
    c = cur.resize((sz, sz), Image.LANCZOS)
    # dim to ~65% so the button under the thumb stays readable
    a = c.getchannel("A").point(lambda v: int(v * 166 / 255))
    c.putalpha(a)
    if len(_VMENU_THUMB_DIM_CACHE) > 24:      # a handful of sizes at most
        _VMENU_THUMB_DIM_CACHE.clear()
    _VMENU_THUMB_DIM_CACHE[sz] = c
    return c


def vmenu_thumb_scale(style, w, h):
    """The thumb-cursor scale factor for a rendered menu of size (w, h) 
    the SINGLE source of truth shared by the renderers and the overlay's
    fast-path thumb compositing (so a cached body + separately-drawn thumb is
    pixel-identical to a full render)."""
    if style == "radial":
        return max(0.6, w / 420.0)
    return max(0.6, min(w, h) / 220.0)


def draw_vmenu_thumb_on(img, style, nx, ny):
    """Composite the OSK thumb cursor onto `img` at normalized (nx, ny),
    using the style's canonical scale. Overlay fast path calls this on a
    copied cached body; the renderers call it as their final step."""
    w, h = img.size
    _draw_vmenu_thumb(img, nx, ny, scale=vmenu_thumb_scale(style, w, h))


def _draw_vmenu_thumb(img, nx, ny, scale=1.0):
    """Paste the OSK thumb cursor onto `img` centered at the normalized
    (nx, ny) position (0..1, y DOWN), at ~65% opacity (matching the OSK)."""
    if nx is None or ny is None:
        return
    cur = _load_vmenu_thumb_cursor()
    if cur is None:
        return
    w, h = img.size
    sz = max(8, int(cur.width * scale))
    c = _vmenu_dim_cursor(sz)
    if c is None:
        return
    cx = int(nx * w) - sz // 2
    cy = int(ny * h) - sz // 2
    img.alpha_composite(c, (cx, cy))


def _load_vmenu_icon_asset(icon):
    """The bundled white-glyph PNG for one icon id (RGBA, cached), or None
    when there isn't one (unknown/legacy id  caller falls back)."""
    if icon in _VMENU_ICON_CACHE:
        return _VMENU_ICON_CACHE[icon]
    import os
    from PIL import Image
    # User uploads resolve to the writable custom-icon dir; everything else to
    # the read-only bundle. A deleted/missing custom file just returns None,
    # so the caller falls back to the plain-circle outline like any unknown id.
    if is_custom_vmenu_icon(icon):
        path = _custom_icon_path(icon)
    else:
        path = os.path.join(vmenu_icon_asset_dir(), icon + ".png")
    asset = None
    if os.path.isfile(path):
        try:
            asset = Image.open(path).convert("RGBA")
            asset = _recenter_rgba(asset)
        except Exception:
            asset = None
    _VMENU_ICON_CACHE[icon] = asset
    return asset


def _recenter_rgba(img):
    """Return `img` shifted so its opaque CONTENT is centered on the canvas.
    A handful of the bundled Steam glyphs (and any lopsided source) sit a few
    px off-center; this makes every icon render dead-centered in its tile.
    Uses the ALPHA bbox (getbbox() alone also counts colored-but-transparent
    pixels some formats carry). An already-centered icon shifts by 0 (a
    sub-pixel offset rounds to 0), so this never disturbs the well-placed ones."""
    try:
        bb = img.getchannel("A").getbbox()
    except Exception:
        return img
    if not bb:
        return img
    l, t, r, b = bb
    w, h = img.size
    dx = int(round((w - 1) / 2.0 - (l + r - 1) / 2.0))
    dy = int(round((h - 1) / 2.0 - (t + b - 1) / 2.0))
    if dx == 0 and dy == 0:
        return img
    from PIL import Image as _Image
    out = _Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, (dx, dy), img)
    return out


def _vmenu_scaled_icon(icon, size):
    """The icon glyph LANCZOS-scaled to `size` px, cached by (icon, size).
    A menu's cells are a stable size, so this turns a per-cell-per-frame
    resize into a one-time cost per (icon, size)."""
    key = (icon, size)
    hit = _VMENU_ICON_SCALED_CACHE.get(key)
    if hit is not None:
        return hit
    asset = _load_vmenu_icon_asset(icon)
    if asset is None:
        return None
    from PIL import Image
    scaled = asset.resize((size, size), Image.LANCZOS)
    if len(_VMENU_ICON_SCALED_CACHE) > 256:   # bound growth across configs
        _VMENU_ICON_SCALED_CACHE.clear()
    _VMENU_ICON_SCALED_CACHE[key] = scaled
    return scaled


def draw_vmenu_icon(img, cx, cy, r, icon, color=VMENU_FG):
    """Paste one Virtual Menu icon (a real Steam Input control glyph)
    centered at (cx, cy) with radius `r` onto `img` (an RGBA PIL Image 
    NOT an ImageDraw context, since a bundled asset needs the image to
    paste onto). `color` only affects the circle-outline fallback used for
    an id with no bundled asset  the glyphs themselves are pre-colored
    to match the app's theme."""
    size = max(1, int(r * 2))
    if is_text_vmenu_icon(icon):
        # A custom-text id has no art: draw its letters into the same square
        # the glyph would have filled, so every icon-drawing caller (the
        # picker's tiles + entry-row button) gets text for free.
        draw_vmenu_text(img, (cx - r, cy - r, cx + r, cy + r),
                        vmenu_icon_text(icon), color=color)
        return
    scaled = _vmenu_scaled_icon(icon, size)
    if scaled is not None:
        img.paste(scaled, (int(cx - size / 2), int(cy - size / 2)), scaled)
        return
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    lw = max(2, r // 5)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=lw)


def _vmenu_cell_glyph(img, d, rect, e, labels):
    """One cell's icon-or-label content, centered in `rect`. `img` is the
    target RGBA Image (icons paste onto it); `d` is an ImageDraw context
    on the same image (used for the text fallback)."""
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    icon = str(e.get("icon") or "none")
    if is_text_vmenu_icon(icon):
        # Custom text gets the WHOLE cell, not the square an icon would take
        #  a word wants the box's full width to stay readable.
        draw_vmenu_text(img, rect, vmenu_icon_text(icon))
        return
    # Icons are bundled at 128x128 and LANCZOS-downscaled, so a bigger
    # target here is strictly higher quality (more source detail kept) 
    # //3 instead of //4 renders noticeably crisper than the old ratio.
    r = max(9, min(rect[2] - rect[0], rect[3] - rect[1]) // 3)
    if icon != "none":
        draw_vmenu_icon(img, cx, cy, r, icon)
        return
    act = str(e.get("action") or "")
    t = (labels or {}).get(act) or (act if act and act != "none" else "")
    if t:
        f = _vmenu_font(max(10, min(16, r)))
        t = t if len(t) <= 12 else t[:11] + "..."
        bb = d.textbbox((0, 0), t, font=f)
        d.text((cx - (bb[0] + bb[2]) / 2, cy - (bb[1] + bb[3]) / 2), t,
               font=f, fill=VMENU_FG)


def vmenu_radial_at(n, nx, ny, dead=0.16):
    """Entry index for a RADIAL menu from a normalized (0..1, y DOWN) touch
    position: sector 0 is centered at the TOP, running clockwise (Steam's
    radial convention). None inside the center dead-zone."""
    import math
    n = max(1, min(int(n or 1), VMENU_MAX_ENTRIES))
    dx, dy = nx - 0.5, ny - 0.5
    if math.hypot(dx, dy) < dead:
        return None
    # y-down atan2 = clockwise-positive, 0° at 3 o'clock  same convention
    # the renderer draws with.
    ang = math.degrees(math.atan2(dy, dx))
    step = 360.0 / n
    return int(((ang + 90.0 + step / 2.0) % 360.0) / step) % n


def render_vmenu_radial(entries, size, highlight=None, labels=None,
                        thumb=None):
    """Render a RADIAL menu (options around a circle) to a PIL RGBA image 
    a donut of n sectors, sector 0 centered at the top, clockwise. `thumb` =
    (nx, ny) 0..1 pad position to draw the OSK thumb cursor at, or None."""
    import math
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    n = max(1, min(len(entries), VMENU_MAX_ENTRIES))
    c = size / 2.0
    R = c - 4
    hole = R * 0.34
    step = 360.0 / n
    gap_deg = min(2.5, step * 0.06) if n > 1 else 0.0
    box = (c - R, c - R, c + R, c + R)
    for i in range(n):
        hl = (i == highlight)
        center = -90.0 + i * step
        a0 = center - step / 2.0 + gap_deg
        a1 = center + step / 2.0 - gap_deg
        if n == 1:
            d.ellipse(box, fill=VMENU_HL if hl else VMENU_CELL,
                      outline=VMENU_HL_EDGE if hl else VMENU_CELL_EDGE,
                      width=2 if hl else 1)
        else:
            d.pieslice(box, a0, a1, fill=VMENU_HL if hl else VMENU_CELL,
                       outline=VMENU_HL_EDGE if hl else VMENU_CELL_EDGE,
                       width=2 if hl else 1)
    # Punch the center hole (donut)  transparent, with a soft rim.
    d.ellipse((c - hole, c - hole, c + hole, c + hole), fill=(0, 0, 0, 0))
    d.ellipse((c - hole, c - hole, c + hole, c + hole),
              outline=VMENU_CELL_EDGE, width=1)
    mid = (R + hole) / 2.0
    for i in range(n):
        e = entries[i] if i < len(entries) else {}
        ang = math.radians(-90.0 + i * step)
        cx = c + mid * math.cos(ang)
        cy = c + mid * math.sin(ang)
        r = max(8, int(min(28, R * 3.2 / (n + 2))))
        rect = (int(cx - r * 1.6), int(cy - r * 1.6),
                int(cx + r * 1.6), int(cy + r * 1.6))
        _vmenu_cell_glyph(img, d, rect, e, labels)
    if thumb is not None:
        draw_vmenu_thumb_on(img, "radial", thumb[0], thumb[1])
    return img


def render_vmenu_image(entries, w, h, highlight=None, labels=None,
                       gap=VMENU_CELL_GAP, rows=None, thumb=None):
    """Render a touch menu (or, with rows=[n], a Hot Bar strip) to a PIL
    RGBA image  the ONE renderer behind the Options live preview and the
    on-screen overlay. `entries` = [{"icon", "action"}, ...]; `highlight` =
    entry index or None; `labels` maps action ids to short display text
    for icon-less entries; `thumb` = (nx, ny) 0..1 pad position to draw the
    OSK thumb cursor at (overlay only), or None. NO backdrop  the gaps are
    transparent (floating boxes); each box is a rounded rectangle, and the
    highlighted box gets a 2px accent ridge across its (rounded) top."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))   # transparent  no backdrop
    d = ImageDraw.Draw(img)
    n = max(1, min(len(entries), VMENU_MAX_ENTRIES))
    rows_l = rows if rows is not None else vmenu_layout(n)
    rects = vmenu_cell_rects(n, w, h, gap, rows=rows_l)
    corner_flags = vmenu_corner_flags(rows_l)
    for i, rect in enumerate(rects):
        e = entries[i] if i < len(entries) else {}
        hl = (i == highlight)
        cw, ch = rect[2] - rect[0], rect[3] - rect[1]
        # Only the 4 outer corners of the WHOLE menu are rounded (one per
        # corner box, on its outside corner)  everywhere else is square, so
        # the group reads as a single rounded-rect shape, not 16 separate
        # rounded tiles. A subtle radius (~18% of the short side)  just a
        # little more than a barely-there nub, well short of a pill shape.
        corners = corner_flags[i] if i < len(corner_flags) else (
            False, False, False, False)
        br = max(4, int(min(cw, ch) * 0.18)) if any(corners) else 0
        if hl:
            # Paint the box's TRUE rounded shape once in the ridge color,
            # then paste the main fill over everything EXCEPT the top
            # ridge_px band  both use the exact same rounded_rectangle
            # geometry, so the ridge naturally follows/tapers with a rounded
            # TOP corner instead of being squared off underneath it (a plain
            # inset rectangle there would square off most of the curve, since
            # the radius is normally much taller than the 2px ridge).
            d.rounded_rectangle(rect, radius=br, fill=VMENU_HL_RIDGE,
                                corners=corners)
            fill_mask = Image.new("L", img.size, 0)
            ImageDraw.Draw(fill_mask).rounded_rectangle(
                rect, radius=br, fill=255, corners=corners)
            # Image.paste's box is (left, upper, right, lower) with the
            # right/lower edge EXCLUSIVE  unlike ImageDraw, where rect[2] is
            # an INCLUSIVE pixel column. Without the +1 the rightmost column
            # of the box was never zeroed here, so it kept showing the fill
            # color even in the ridge rows (the ridge looked "1px short").
            fill_mask.paste(0, (rect[0], rect[1], rect[2] + 1,
                               rect[1] + VMENU_HL_RIDGE_PX))
            fill_layer = Image.new("RGBA", img.size, VMENU_HL)
            img.paste(fill_layer, (0, 0), fill_mask)
            d = ImageDraw.Draw(img)   # re-bind: paste() invalidates the draw
        else:
            # Flat fill, no separate-color 1px edge  a solid box.
            d.rounded_rectangle(rect, radius=br, fill=VMENU_CELL,
                                corners=corners)
        _vmenu_cell_glyph(img, d, rect, e, labels)
    if thumb is not None:
        draw_vmenu_thumb_on(img, "touch", thumb[0], thumb[1])
    return img


# Per-menu presentation / activation settings (mirrors Steam's touch-menu
# options, plus "toggle"  SteamlessInput's own addition). Ranges + defaults
# match Steam's own sliders.
#   toggle: the TRIGGER press/touch flips the menu open/closed instead of
#           having to be held the whole time  press once to show it, aim and
#           click, press the trigger again to put it away. The highlighted
#           entry still fires on a plain click while it's open (see the tray
#           _handle_virtual_menu / _KeyVMenuRunner activation branches).
VMENU_ACTIVATE_STYLES = ("toggle", "click", "release", "touch_release",
                         "continuous")
# A menu button can carry a LIST of Hotkey-style actions (the cog modal is a
# mini Hotkeys page  one horizontal panel per action, minus the two trigger
# dropdowns). Each action is a chord-shaped dict:
#   {"type": "keys", "keys": [tok, ...]}
#   {"type": "launch", "path": str, "args": str}
#   {"type": "button_combo", "outputs": [gamepad output id, ...]}
#   {"type": "powershell", "script": str}   (PowerShell OR batch/CMD  the
#       type name predates CMD support; vmenu_script_is_batch picks which)
# Pressing the button fires the row's simple `action` AND every action here.
VMENU_ACTION_TYPES = ("keys", "button_combo", "launch", "powershell")
VMENU_ENTRY_COMBO_SLOTS = 3   # Button Combo output slots (matches Hotkeys)
VMENU_ENTRY_MAX_ACTIONS = 6   # cap per button (keeps the cog modal sized)
# Cap on a pasted script. Generous enough for any real automation
# snippet somebody would bind to a menu button, while keeping settings.json a
# sane size (the script is stored inline in it, not as a separate file).
VMENU_SCRIPT_MAX_LEN = 20000
VMENU_DEFAULTS = {
    "activate": "toggle",  # when the highlighted entry fires / how the menu
    #                        opens (see the tray _handle_virtual_menu
    #                        activation branches)  "toggle" is the default
    # Default placement = the lower-right spot marked in the design reference
    # (a 220x220 box at x=1241,y=516 on a 1600x900 screen). Positions are a
    # fraction of the FREE space (see vmenu.show), so hpos=90 keeps the menu's
    # right edge ~10% in from the right and vpos=76 keeps its bottom ~24% up
    # from the bottom on ANY resolution (the overlay itself is res-scaled).
    "hpos": 90,           # horizontal screen position 0(left)..100(right)
    "vpos": 76,           # vertical screen position 0(top)..100(bottom)
    "size": 100,          # overlay scale 50..150 (% of the base size)
    "opacity": 90,        # overlay opacity 40..100 (%)
}
_VMENU_RANGES = {"hpos": (0, 100), "vpos": (0, 100),
                 "size": (50, 150), "opacity": (40, 100)}

# The menu's TRIGGER input (the "Show While Touching" dropdown). A PAD trigger
# shows the menu while that pad is TOUCHED and is navigated by that pad's thumb
# (pad click fires). A BUTTON trigger shows the menu while the button is HELD
# and is navigated by the RIGHT pad's thumb (RIGHT-pad click fires; releasing
# the button closes it). Menus are global (SC + Deck share the HID runtime),
# so the list covers both controllers. Ordered for the dropdown.
VMENU_TRIGGERS = (
    ("none", "Not activated"),
    ("lpad", "Left Trackpad"),
    ("rpad", "Right Trackpad"),
    # The GUIDE button (Steam / "..." on a HID pad, Home on an SDL one). A TAP
    # still runs its own binding (the guide-tap detector only fires under
    # _GUIDE_TAP_S), so tap = that binding, hold = the menu. The Chords tab
    # keeps working underneath: only the trigger's own bit is masked while the
    # menu is up (_vmenu_suppress_bits), and that mask lands after the frame's
    # guide state has been read.
    ("guide", "Guide (Steam / Home)"),
    ("qam", "QAM (…) / Capture"),
    ("a", "A"), ("b", "B"), ("x", "X"), ("y", "Y"),
    ("l1", "L1 / Left Bumper"), ("r1", "R1 / Right Bumper"),
    ("l2", "L2 / Left Trigger"), ("r2", "R2 / Right Trigger"),
    ("l3", "L3 / Left Stick Click"), ("r3", "R3 / Right Stick Click"),
    ("l4", "L4 Paddle"), ("r4", "R4 Paddle"),
    ("l5", "L5 Paddle"), ("r5", "R5 Paddle"),
    ("back", "View"), ("start", "Menu"),
    ("dpad_up", "D-Pad Up"), ("dpad_down", "D-Pad Down"),
    ("dpad_left", "D-Pad Left"), ("dpad_right", "D-Pad Right"),
)
VMENU_TRIGGER_LABELS = dict(VMENU_TRIGGERS)
VMENU_TRIGGER_IDS = frozenset(t[0] for t in VMENU_TRIGGERS)
VMENU_PAD_TRIGGERS = ("lpad", "rpad")
# Non-pad (button) trigger ids -> the SCButtons attribute for the HELD bit
# (reuses the canonical SC_CHORD_BUTTONS naming). Pads use their own TOUCH bit.
VMENU_TRIGGER_HOLD_BIT = {
    tid: SC_CHORD_BUTTONS[tid] for tid, _lbl in VMENU_TRIGGERS
    if tid in SC_CHORD_BUTTONS}
# CHORD_GUIDE_ID is deliberately absent from SC_CHORD_BUTTONS (a Guide chord is
# the Steam-held gesture, not a plain two-bit mask), but as a menu trigger it is
# just "this button is held"  the STEAM bit, which an SDL pad's Home folds onto
# too (SDL_CHORD_BUTTONS["home"]).
VMENU_TRIGGER_HOLD_BIT[CHORD_GUIDE_ID] = "STEAM"

# Optional SECOND trigger button (the "pad2" field)  held together with the
# primary trigger to form a CHORD, the same "[Button A] + [Button B]" idea as
# the Gamepad Mode Toggle hotkey and the Gyro-toggle bar. Digital buttons
# only: a chord of two pad TOUCHES isn't offered (Steam Input doesn't have
# one either, and "touching both pads" is an awkward physical ask), so this
# is VMENU_TRIGGERS minus the two pads. "none" (kept from VMENU_TRIGGERS)
# means "no second button"  the trigger behaves exactly as it did before
# this existed.
VMENU_TRIGGER2 = tuple(t for t in VMENU_TRIGGERS if t[0] not in VMENU_PAD_TRIGGERS)
VMENU_TRIGGER2_LABELS = dict(VMENU_TRIGGER2)
VMENU_TRIGGER2_IDS = frozenset(t[0] for t in VMENU_TRIGGER2)

# The menu's KEYBOARD / MOUSE trigger (the "Show Virtual Menu while Holding
# Key" dropdown), stored as `key` alongside the controller `pad` trigger. The
# two are independent ways into the SAME menu  a menu can have both, either,
# or neither. While the key is HELD the overlay shows and is steered WITHOUT a
# controller:
#   * move the MOUSE over a box to highlight it, LEFT-CLICK to fire it
#   * arrow keys move between boxes, Enter fires, Escape closes
# (see the tray's _KeyVMenuRunner). Enter/Escape/the arrows are therefore NOT
# offered as triggers  they already have a job while a menu is open.
#
# Rows are (id, label, Win32 VK). The VK is what the tray's WH_KEYBOARD_LL
# hook matches on; mouse rows carry None and are matched from the
# WH_MOUSE_LL hook instead (see VMENU_KEY_MOUSE). The Linux tree mirrors this
# table verbatim for source parity  its runtime (tray_linux.py) has no
# overlay, so nothing reads the VKs there.
_VMENU_KEY_ROWS = (
    ("mouse_middle", "Middle Mouse Button", None),
    ("mouse_x1", "Mouse Button 4 (Back)", None),
    ("mouse_x2", "Mouse Button 5 (Forward)", None),
    ("lctrl", "Left Ctrl", 0xA2), ("rctrl", "Right Ctrl", 0xA3),
    ("lalt", "Left Alt", 0xA4), ("ralt", "Right Alt", 0xA5),
    ("lshift", "Left Shift", 0xA0), ("rshift", "Right Shift", 0xA1),
    ("lwin", "Left Windows", 0x5B), ("rwin", "Right Windows", 0x5C),
    ("apps", "Menu / Context Key", 0x5D),
    ("tab", "Tab", 0x09), ("capslock", "Caps Lock", 0x14),
    ("space", "Space", 0x20), ("backspace", "Backspace", 0x08),
) + tuple(("f%d" % i, "F%d" % i, 0x6F + i) for i in range(1, 13)) + (
    ("insert", "Insert", 0x2D), ("home", "Home", 0x24),
    ("pageup", "Page Up", 0x21), ("delete", "Delete", 0x2E),
    ("end", "End", 0x23), ("pagedown", "Page Down", 0x22),
    ("printscreen", "Print Screen", 0x2C), ("scrolllock", "Scroll Lock", 0x91),
    ("pause", "Pause / Break", 0x13),
) + tuple(("d%d" % i, str(i), 0x30 + i) for i in range(10)) + tuple(
    (c.lower(), c, 0x41 + i) for i, c in enumerate(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ")) + (
    ("grave", "` (Grave)", 0xC0), ("minus", "- (Minus)", 0xBD),
    ("equals", "= (Equals)", 0xBB), ("lbracket", "[ (Left Bracket)", 0xDB),
    ("rbracket", "] (Right Bracket)", 0xDD), ("backslash", "\\ (Backslash)",
                                              0xDC),
    ("semicolon", "; (Semicolon)", 0xBA), ("quote", "' (Quote)", 0xDE),
    ("comma", ", (Comma)", 0xBC), ("period", ". (Period)", 0xBE),
    ("slash", "/ (Slash)", 0xBF),
) + tuple(("num%d" % i, "Numpad %d" % i, 0x60 + i) for i in range(10)) + (
    ("numlock", "Num Lock", 0x90), ("nummultiply", "Numpad *", 0x6A),
    ("numadd", "Numpad +", 0x6B), ("numsubtract", "Numpad -", 0x6D),
    ("numdecimal", "Numpad .", 0x6E), ("numdivide", "Numpad /", 0x6F),
)
VMENU_KEY_TRIGGERS = (("none", "Not activated"),) + tuple(
    (kid, lbl) for kid, lbl, _vk in _VMENU_KEY_ROWS)
VMENU_KEY_LABELS = dict(VMENU_KEY_TRIGGERS)
VMENU_KEY_IDS = frozenset(kid for kid, _lbl in VMENU_KEY_TRIGGERS)
# Optional SECOND key/mouse trigger (the "key2" field)  held together with
# `key` to form a CHORD, same idea as the controller trigger's `pad2` (see
# above). Unlike pad2 there's no "pad" concept to exclude here, so this is
# literally the same vocabulary  "none" still means "no second button".
# id -> Win32 virtual-key code (keyboard triggers only)
VMENU_KEY_VK = {kid: vk for kid, _lbl, vk in _VMENU_KEY_ROWS if vk is not None}
# id -> mouse button name, for the rows the LL MOUSE hook owns instead
VMENU_KEY_MOUSE = {"mouse_middle": "middle", "mouse_x1": "x1",
                   "mouse_x2": "x2"}


def _vmenu_clamp_int(v, key):
    lo, hi = _VMENU_RANGES[key]
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return VMENU_DEFAULTS[key]
    return max(lo, min(hi, n))


# Batch/CMD-only syntax  none of it is valid PowerShell, so a hit here is
# strong evidence a pasted script is a .bat rather than a .ps1.
_VMENU_BATCH_PATTERNS = (
    r"^@(echo|rem|set|if|for|call|goto|cls|pause|exit|title)\b",
    r"^echo\s+(on|off)\b",
    r"^rem\b",
    r"^goto\s+\w",
    r"^:[A-Za-z_]\w*\s*$",          # a :label line
    r"^(setlocal|endlocal)\b",
    r"^pause\s*$",
    r"\bexit\s*/b\b",
    r"%[A-Za-z_]\w*%",              # %VAR% expansion
    r"%~[dpnxsfa0-9]",              # %~dp0 and friends
    r"^if\s+(not\s+)?errorlevel\b",
    r"^set\s+/?\w*\s*\w+=",
    r"^call\s+\S",
    r">\s*nul\b",
)
# PowerShell-only syntax. Weighed against the above so a script using BOTH
# vocabularies (or a plain command that runs in either shell) resolves the
# way the majority of its lines suggest.
_VMENU_PS_PATTERNS = (
    r"\$\w+",                       # $variable
    r"\b(?:Get|Set|New|Remove|Start|Stop|Write|Add|Invoke|Select|Where|"
    r"ForEach|Out|Test|Restart|Enable|Disable|Clear|Copy|Move|Import|"
    r"Export|Convert|Join|Split|Measure|Sort)-[A-Za-z]\w+",
    r"\[\s*[A-Za-z_][\w.]*\s*\]::",  # [System.Foo]::Bar()
    r"^param\s*\(",
    r"^function\s+\w",
    r"\s-(?:eq|ne|lt|gt|le|ge|like|match|contains|notlike|join|split)\b",
    r"\|\s*(?:Where|ForEach|Select|Sort|Measure|Out)-Object",
    r"@\{",                         # hashtable literal
    r"^\s*#(?!!)",                  # a # comment line
)


def vmenu_script_is_batch(script):
    """True if a pasted script should run through cmd.exe rather than
    PowerShell.

    The cog modal's script field takes EITHER  people paste old .bat
    snippets ("@echo off / shutdown /h / exit") as readily as PowerShell 
    and there's no interpreter dropdown, so the interpreter is inferred here
    from the syntax actually used. Scores batch-only markers against
    PowerShell-only ones; PowerShell wins ties and wins when neither appears,
    since a bare command line (`shutdown /h`, `ipconfig`) runs fine in it.

    A first line of `::cmd` / `::batch` (or `#ps` / `#powershell`) forces the
    choice outright, for the rare script whose syntax reads either way."""
    import re
    text = str(script or "")
    if not text.strip():
        return False
    body = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not body:
        return False
    first = body[0].lower().rstrip()
    if first in ("::cmd", "::batch", "@cmd", "rem cmd"):
        return True
    if first in ("#ps", "#powershell", "#pwsh"):
        return False
    def _score(patterns):
        n = 0
        for pat in patterns:
            for ln in body:
                if re.search(pat, ln, re.IGNORECASE):
                    n += 1
                    break
        return n
    return _score(_VMENU_BATCH_PATTERNS) > _score(_VMENU_PS_PATTERNS)


def vmenu_script_summary(script):
    """A one-line description of a pasted PowerShell script, for the cog
    modal's read-only preview field (a tk.Entry can't render newlines, and
    the row is a single 52px strip). Shows the first non-blank line, trimmed,
    plus a line count once there's more than one."""
    lines = [ln.strip() for ln in str(script or "").splitlines()]
    body = [ln for ln in lines if ln]
    if not body:
        return ""
    head = body[0]
    if len(head) > 34:
        head = head[:33] + "…"
    n = len(lines)
    return head if n <= 1 else "%s   (%d lines)" % (head, n)


def _sanitize_vmenu_action(a):
    """Normalize ONE menu-button Hotkey action, or None if empty/invalid
    (a blank Key Combo, a Launch with no path, a Button Combo with no
    outputs)  such rows exist only in the live editor, never persisted."""
    if not isinstance(a, dict):
        return None
    typ = a.get("type")
    if typ not in VMENU_ACTION_TYPES:
        return None
    if typ == "keys":
        keys = [str(k) for k in (a.get("keys") or []) if k]
        return {"type": "keys", "keys": keys} if keys else None
    if typ == "launch":
        path = str(a.get("path") or "").strip()
        return {"type": "launch", "path": path,
                "args": str(a.get("args") or "")} if path else None
    if typ == "powershell":
        # Keep the script VERBATIM apart from the length cap  indentation and
        # blank lines are meaningful in a pasted script, so nothing here
        # strips or collapses them. An all-whitespace script is "empty".
        script = str(a.get("script") or "")[:VMENU_SCRIPT_MAX_LEN]
        return {"type": "powershell", "script": script} \
            if script.strip() else None
    outs = [str(o) for o in (a.get("outputs") or [])
            if o and o != "none"][:VMENU_ENTRY_COMBO_SLOTS]
    return {"type": "button_combo", "outputs": outs} if outs else None


def vmenus_sanitize(menus):
    """Validate a settings-shaped virtual-menu list: keeps only well-formed
    menus, clamps entries to the cap, drops unknown pads/types, and normalizes
    the per-menu presentation/activation settings to their valid ranges.
    Returns a NEW deep-copied list (safe to store)."""
    out = []
    for m in menus if isinstance(menus, list) else []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        typ = m.get("type") if m.get("type") in ("touch", "radial",
                                                 "hotbar") else "touch"
        # `pad` is really the TRIGGER id now (a pad OR any button); kept named
        # "pad" for settings back-compat.
        pad = m.get("pad") if m.get("pad") in VMENU_TRIGGER_IDS else "none"
        # Optional second trigger button  held together with `pad` to form a
        # chord. Invalid, a pad id, or equal to `pad` itself all collapse to
        # "none" (no combo): a chord against itself isn't meaningful, and the
        # editor already prevents picking it, but a hand-edited settings.json
        # shouldn't be able to smuggle in a no-op "combo".
        pad2 = m.get("pad2") if m.get("pad2") in VMENU_TRIGGER2_IDS else "none"
        if pad2 == pad:
            pad2 = "none"
        # The keyboard/mouse trigger  independent of `pad` (a menu may be
        # opened by a controller input, by a key, by both or by neither).
        # Missing (menus saved before the dropdown existed) → "none".
        key = m.get("key") if m.get("key") in VMENU_KEY_IDS else "none"
        # Optional second key/mouse trigger  same "chord" idea as `pad2`.
        key2 = m.get("key2") if m.get("key2") in VMENU_KEY_IDS else "none"
        if key2 == key:
            key2 = "none"
        # An explicit on/off toggle (Options list page). Missing → enabled, so
        # menus saved before the toggle existed keep working.
        enabled = bool(m.get("enabled", True))
        act = m.get("activate") if m.get("activate") in VMENU_ACTIVATE_STYLES \
            else VMENU_DEFAULTS["activate"]
        entries = []
        for e in (m.get("entries") or [])[:VMENU_MAX_ENTRIES]:
            if not isinstance(e, dict):
                continue
            # A menu button's output = the row's inline simple `action` PLUS a
            # list of extra Hotkey-style `actions` (the cog modal). Blank /
            # incomplete actions are dropped here (kept in the live editor).
            actions = []
            # migrate a legacy single-effect entry (pre-list settings)
            leg = e.get("effect")
            if leg in VMENU_ACTION_TYPES:
                mig = _sanitize_vmenu_action({
                    "type": leg, "keys": e.get("keys"),
                    "path": e.get("path"), "args": e.get("args"),
                    "outputs": e.get("outputs"), "script": e.get("script")})
                if mig is not None:
                    actions.append(mig)
            for a in (e.get("actions") or []):
                sa = _sanitize_vmenu_action(a)
                if sa is not None:
                    actions.append(sa)
            icon = str(e.get("icon") or "none")
            if is_text_vmenu_icon(icon):
                # Re-run the same normalization the picker applies (collapse
                # whitespace, cap the length, empty -> "none") so a
                # hand-edited settings.json can't smuggle in a multi-line or
                # unbounded label the renderer would have to cope with.
                icon = make_text_vmenu_icon(vmenu_icon_text(icon))
            entries.append({"icon": icon,
                            "action": str(e.get("action") or "none"),
                            "actions": actions[:VMENU_ENTRY_MAX_ACTIONS]})
        out.append({
            "name": name, "type": typ, "pad": pad, "pad2": pad2,
            "key": key, "key2": key2,
            "enabled": enabled, "activate": act,
            "hpos": _vmenu_clamp_int(m.get("hpos", VMENU_DEFAULTS["hpos"]),
                                     "hpos"),
            "vpos": _vmenu_clamp_int(m.get("vpos", VMENU_DEFAULTS["vpos"]),
                                     "vpos"),
            "size": _vmenu_clamp_int(m.get("size", VMENU_DEFAULTS["size"]),
                                     "size"),
            "opacity": _vmenu_clamp_int(
                m.get("opacity", VMENU_DEFAULTS["opacity"]), "opacity"),
            "entries": entries})
    return out


# --- The menu every new install starts with ---------------------------------
# Virtual Menus are the app's least discoverable feature: the page opens on an
# empty list, and building a useful menu means knowing the trigger vocabulary,
# the icon browser AND the cog modal's action types before anything appears on
# screen at all. So a fresh install ships with ONE menu already built and
# armed, on Guide + DPad Up  the same chord the first-run tour teaches (see
# tutorial._demo_vmenu, which borrows the trigger for the length of its slide)
#  so the first press a new user makes shows them the feature working, and
# the menu they then edit is a worked example rather than a blank grid.
#
# Every launch target here is resolved at FIRE time, never at build time:
#   - Steam / Big Picture     -> VMENU_LAUNCH_STEAM (PATH, else the Flatpak)
#   - the browser button      -> VMENU_LAUNCH_DEFAULT_BROWSER
#   - Spotify / Discord       -> the bare command name, handed to _launch_program
#                                which falls through to xdg-open, so a distro
#                                package and a Flatpak alias both work
#  because this list is shipped to every machine, and a path baked from one
# developer's box would be dead on most of them. A button whose program isn't
# installed simply does nothing when pressed; it is not an error, and the user
# can retarget or delete it in the editor.
#
# Two of the twelve cells are deliberately EMPTY. A grid with obvious gaps in
# it reads as "yours to fill in" far better than a full one does, and gives
# somebody a place to put their first button without deleting one of ours.
#
# Icons are the unlisted preset_* bundle (see VMENU_PRESET_ICON_PREFIX): they
# render here but never show up in the icon browser, so nobody has to scroll
# past a Spotify logo while picking art for their own button.
#
# The Windows tree's twin of this list carries the same twelve buttons, with
# Windows launch targets and its power row written as PowerShell/CMD scripts.
# Here EVERY button is a plain "launch" instead, deliberately: a "powershell"
# action runs under pwsh on Linux (see tray._run_user_script), which almost
# nobody has installed, so a shipped default written as a script would be dead
# on arrival. systemctl/loginctl invoked directly is what every systemd
# desktop  and SteamOS  actually uses, and needs no shell at all.
_DEFAULT_VMENU_ENTRIES = (
    ("preset_steam", "launch", VMENU_LAUNCH_STEAM, ""),
    ("preset_steam_bigpicture", "launch", VMENU_LAUNCH_STEAM, "-bigpicture"),
    ("preset_spotify", "launch", "spotify", ""),
    ("preset_discord", "launch", "discord", ""),
    # The Windows twin has Notepad here. Linux has no equivalent every install
    # is guaranteed to have, and picking one distro's editor would leave the
    # button dead on the others  so this cell ships free instead.
    (None, None, None, None),          # free cell
    ("preset_browser", "launch", VMENU_LAUNCH_DEFAULT_BROWSER, ""),
    (None, None, None, None),          # free cell
    (None, None, None, None),          # free cell
    ("preset_shutdown", "launch", "systemctl", "poweroff"),
    ("preset_restart", "launch", "systemctl", "reboot"),
    ("preset_lock", "launch", "loginctl", "lock-session"),
    # Plain suspend rather than the Windows side's hibernate-if-possible: a
    # hibernate needs a swap area big enough for the image, which a Deck (and
    # most zram-only installs) doesn't have, and `systemctl hibernate` just
    # fails there.
    ("preset_sleep", "launch", "systemctl", "suspend"),
)


def default_virtual_menus():
    """The virtual-menu list a settings file with none of its own starts from
    (tray.DEFAULT_SETTINGS). Returns a FRESH list every call so the default
    can never be aliased into a live config and mutated from under the next
    reader."""
    entries = []
    for icon, kind, payload, args in _DEFAULT_VMENU_ENTRIES:
        if kind == "launch":
            actions = [{"type": "launch", "path": payload, "args": args}]
        elif kind == "script":
            actions = [{"type": "powershell", "script": payload}]
        else:
            actions = []
        entries.append({"icon": icon or "none", "action": "none",
                        "actions": actions})
    return [{
        "name": "Virtual Menu 1", "type": "touch",
        "pad": "guide", "pad2": "dpad_up",
        "key": "none", "key2": "none",
        "enabled": True, "activate": "toggle",
        "hpos": VMENU_DEFAULTS["hpos"], "vpos": VMENU_DEFAULTS["vpos"],
        "size": VMENU_DEFAULTS["size"], "opacity": VMENU_DEFAULTS["opacity"],
        "entries": entries,
    }]
