# -*- coding: utf-8 -*-
"""Steam Input controller-config (VDF) import + SteamInputDB.com browsing.

Two independent halves, both stdlib-only and import-light (no tkinter, no
SDL, no steamcontroller  safe to import from the picker OR the tray):

  1. A tolerant text-VDF parser + `convert()`, which maps a Steam Input
     `controller_mappings` file onto ONE of our controller kinds' GAMEPAD-tab
     binds (control id -> action value from the merged Xbox+desktop
     vocabulary) plus the kind's Gyro-To-Mouse settings. Everything the
     config asks for that our model can't express is returned in a `skipped`
     report instead of being silently dropped.

  2. A tiny SteamInputDB.com API client (search games + community configs,
     download a config's VDF from the Steam CDN) used by the picker's
     "Community Configs" browser. SteamInputDB is Steam-Web-API-backed, so
     the VDFs are the exact files Steam itself would apply.

The import targets the GAMEPAD tab deliberately: community configs are
game configs, and the Gamepad tab's merged vocabulary (XInput outputs AND
keyboard / mouse / system actions per control) is the one place every
binding type a typical config uses has a native home.
"""

import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# VDF text parser
# ---------------------------------------------------------------------------

# Ceiling for parse_vdf's recursive descent  see the guard inside `_obj`.
# Genuine controller configs nest about six levels; 100 is far above anything
# legitimate and far below the interpreter's stack limit.
_MAX_VDF_DEPTH = 100

_TOKEN_RE = re.compile(
    r'"((?:\\.|[^"\\])*)"'      # quoted string (with escapes)
    r"|(\{|\})"                 # braces
    r"|(//[^\n]*)"              # line comment
    r"|([^\s\{\}\"]+)",         # bare token (rare in exports, but legal)
    re.S)


def parse_vdf(text):
    """Parse Valve KeyValues text into nested dicts. A key that repeats at
    the same level (``group``, ``preset``, ``binding`` ...) accumulates into a
    LIST under that key, so nothing is lost. Raises ValueError on a
    structurally broken file."""
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        if m.group(3) is not None:      # comment
            continue
        if m.group(1) is not None:
            tokens.append(("s", m.group(1).replace('\\"', '"')
                           .replace("\\\\", "\\")))
        elif m.group(2) is not None:
            tokens.append((m.group(2), None))
        else:
            tokens.append(("s", m.group(4)))
    pos = [0]

    def _obj(depth=0):
        # Depth guard. This descent is recursive, and a corrupt or hostile file
        # of nothing but "{" exhausts the interpreter stack at ~5k levels 
        # raising RecursionError, which is NOT a ValueError, so it would sail
        # past convert()'s handler and land on the Tk main thread. In the
        # --windowed build that has no console: the user clicks Import and
        # simply nothing happens. Real controller configs nest ~6 deep.
        if depth > _MAX_VDF_DEPTH:
            raise ValueError("nesting deeper than %d levels" % _MAX_VDF_DEPTH)
        out = {}
        while pos[0] < len(tokens):
            t, val = tokens[pos[0]]
            if t == "}":
                pos[0] += 1
                return out
            if t != "s":
                raise ValueError("unexpected '%s'" % t)
            key = val
            pos[0] += 1
            if pos[0] >= len(tokens):
                raise ValueError("dangling key %r" % key)
            t2, val2 = tokens[pos[0]]
            if t2 == "{":
                pos[0] += 1
                v = _obj(depth + 1)
            elif t2 == "s":
                pos[0] += 1
                v = val2
            else:
                raise ValueError("key %r followed by '%s'" % (key, t2))
            if key in out:
                if not isinstance(out[key], list):
                    out[key] = [out[key]]
                out[key].append(v)
            else:
                out[key] = v
        return out

    root = _obj()
    if pos[0] < len(tokens):
        raise ValueError("trailing tokens")
    return root


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# ---------------------------------------------------------------------------
# controller_type <-> our kind
# ---------------------------------------------------------------------------

TYPE_TO_KIND = {
    # "gordon" is Valve's internal name for the 2015 Steam Controller.
    "controller_steamcontroller_gordon": "sc2015",
    "controller_triton": "sc",
    "controller_neptune": "steam_deck",
    "controller_switch_pro": "switch",
    "controller_switch2_pro": "switch",
    "controller_8bitdo": "switch",
    "controller_ps4": "ps4",
    "controller_ps5": "ps5",
    "controller_ps5_edge": "ps5",
    "controller_xboxone": "xbox",
    "controller_xboxelite": "xbox",
    "controller_xbox360": "xbox",
}

# The HID-takeover kinds share the SC's control-id space (trackpads, grips);
# every other kind is an SDL-template clone (see keybinds_picker._sdl_layout).
_HID_KINDS = ("sc", "sc2015", "steam_deck")

# controller_types whose configs map onto a kind with NO layout mismatch 
# used by the browser to pre-filter results to the current tab's controller.
# Anything else still imports (the converter degrades + reports).
KIND_MATCH_TYPES = {
    "sc": ("controller_triton", "controller_steamcontroller_gordon",
           "controller_neptune"),
    # The 2015 controller takes its OWN configs first; the two later trackpad
    # pads' configs still import (they simply carry controls it hasn't got,
    # which the converter drops and reports).
    "sc2015": ("controller_steamcontroller_gordon", "controller_triton",
               "controller_neptune"),
    "steam_deck": ("controller_neptune", "controller_triton",
                   "controller_steamcontroller_gordon"),
    "switch": ("controller_switch_pro", "controller_switch2_pro",
               "controller_8bitdo"),
    "ps4": ("controller_ps4",),
    "ps5": ("controller_ps5", "controller_ps5_edge"),
}
_XBOX_TYPES = ("controller_xboxone", "controller_xboxelite",
               "controller_xbox360", "controller_generic")


def match_types_for(kind):
    """controller_type tags whose configs fit `kind` natively. Xbox-shaped
    kinds (xbox itself + every XInput handheld) all take the Xbox types."""
    return KIND_MATCH_TYPES.get(kind, _XBOX_TYPES)


# ---------------------------------------------------------------------------
# binding string -> our action id
# ---------------------------------------------------------------------------

_XINPUT_MAP = {
    "a": "btn_a", "b": "btn_b", "x": "btn_x", "y": "btn_y",
    "shoulder_left": "lb", "shoulder_right": "rb",
    "trigger_left": "lt", "trigger_right": "rt",
    "joystick_left": "ls", "joystick_right": "rs",
    "select": "back", "start": "start", "guide": "guide",
    "dpad_up": "dpad_up", "dpad_down": "dpad_down",
    "dpad_left": "dpad_left", "dpad_right": "dpad_right",
    # Stick-direction outputs: on a stick direction they're the identity
    # passthrough (our "analog" value); _bind rejects them anywhere else.
    "lstick_up": "analog", "lstick_down": "analog",
    "lstick_left": "analog", "lstick_right": "analog",
    "rstick_up": "analog", "rstick_down": "analog",
    "rstick_left": "analog", "rstick_right": "analog",
}

# Steam key_press names -> our pc-vocabulary action ids. Bare modifiers map
# to the HOLD actions (Steam holds the key while the button is held; our
# hold_* actions are that exact behavior).
_KEY_MAP = {
    "return": "enter", "escape": "escape", "tab": "tab", "space": "space",
    "backspace": "backspace", "delete": "delete",
    "up_arrow": "up", "down_arrow": "down",
    "left_arrow": "left", "right_arrow": "right",
    "page_up": "pageup", "page_down": "pagedown",
    "home": "home", "end": "end",
    "left_control": "hold_ctrl", "right_control": "hold_ctrl",
    "left_shift": "hold_shift", "right_shift": "hold_shift",
    "left_alt": "hold_alt", "right_alt": "hold_alt",
    "left_win": "hold_win", "right_win": "hold_win",
    "left_windows": "hold_win", "right_windows": "hold_win",
    "left_meta": "hold_win", "right_meta": "hold_win",
    "print_screen": "print_screen", "printscreen": "print_screen",
    "enter": "enter",               # some exports say ENTER, not RETURN
    # Media / volume keys (seen in community desktop & chord configs).
    "volume_up": "volume_up", "volume_down": "volume_down",
    "mute": "volume_mute", "volume_mute": "volume_mute",
    "play_pause": "media_playpause", "play": "media_playpause",
    "next_track": "media_next", "prev_track": "media_prev",
    "previous_track": "media_prev",
    # Punctuation / special keys (corpus: brackets, backtick, dash, ...).
    "comma": "comma", "period": "period",
    "slash": "slash", "forward_slash": "slash",
    "backslash": "backslash", "back_slash": "backslash",
    "semicolon": "semicolon",
    "apostrophe": "quote", "single_quote": "quote",
    "left_bracket": "lbracket", "right_bracket": "rbracket",
    "back_tick": "backtick", "grave": "backtick", "tilde": "backtick",
    "dash": "minus", "minus": "minus",
    "equals": "equals", "equal": "equals",
    "capslock": "capslock", "caps_lock": "capslock",
    "insert": "insert",
    # Numpad (Steam calls it KEYPAD_*; a few alias spellings seen in the wild).
    "keypad_dash": "kp_minus", "keypad_minus": "kp_minus",
    "keypad_plus": "kp_plus",
    "keypad_asterisk": "kp_multiply", "keypad_multiply": "kp_multiply",
    "keypad_forward_slash": "kp_divide", "keypad_slash": "kp_divide",
    "keypad_divide": "kp_divide",
    "keypad_period": "kp_period", "keypad_dot": "kp_period",
    "keypad_enter": "kp_enter",
}
# Modifier tokens for multi-key activator combos ("LEFT_CONTROL" + "C").
_KEY_MODS = {
    "left_control": "ctrl", "right_control": "ctrl",
    "left_shift": "shift", "right_shift": "shift",
    "left_alt": "alt", "right_alt": "alt",
    "left_win": "win", "right_win": "win",
    "left_windows": "win", "right_windows": "win",
    "left_meta": "win", "right_meta": "win",
}

_MOUSE_MAP = {"left": "mouse_left", "right": "mouse_right",
              "middle": "mouse_middle",
              "back": "page_prev", "forward": "page_next"}

_WHEEL_MAP = {"scroll_up": "scroll_up", "scroll_down": "scroll_down"}


def _map_key_name(name):
    """One Steam key_press key -> our action id, or None if unsupported."""
    n = name.lower()
    if n in _KEY_MAP:
        return _KEY_MAP[n]
    if len(n) == 1 and n.isalnum():
        return n
    if re.fullmatch(r"f([1-9]|1[0-2])", n):
        return n
    if re.fullmatch(r"key_[a-z0-9]", n):        # some exports use KEY_A style
        return n[-1]
    m = re.fullmatch(r"keypad_([0-9])", n)      # numpad digits
    if m:
        return "kp_" + m.group(1)
    return None


def _map_single(binding):
    """One binding string -> (action_id | None, skip_reason | None).
    (None, None) means 'nothing to bind' (empty_binding)."""
    body = binding.split(",", 1)[0].strip()     # drop the ", label" suffix
    if not body:
        return None, None
    parts = body.split()
    btype = parts[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if btype == "empty_binding":
        return None, None
    if btype == "xinput_button":
        out = _XINPUT_MAP.get(arg)
        return (out, None) if out else (None, "gamepad output '%s'" % arg)
    if btype == "key_press":
        out = _map_key_name(arg)
        return (out, None) if out else (None, "key '%s'" % arg)
    if btype == "mouse_button":
        out = _MOUSE_MAP.get(arg)
        return (out, None) if out else (None, "mouse button '%s'" % arg)
    if btype == "mouse_wheel":
        out = _WHEEL_MAP.get(arg)
        return (out, None) if out else (None, "wheel '%s'" % arg)
    if btype == "controller_action":
        if arg == "change_preset":
            return "profile_cycle", None
        if arg == "show_keyboard":
            return "show_keyboard", None
        if arg == "screenshot":
            return "print_screen", None
        if arg == "quit_application":
            return "force_kill", None
        if arg == "controller_poweroff":
            return "power_off", None
        if arg in ("empty_binding", "empty_sub_command", "none"):
            return None, None
        if arg == "set_led":
            # LED color decoration  cosmetic and Steam-client-only; the app
            # manages controller LEDs itself. Nothing lost, nothing reported.
            return None, None
        if arg.startswith("gr_"):
            return None, "Steam Game Recording (Steam-client only)"
        if arg.startswith("bigpicture"):
            return None, "Steam Big Picture (Steam-client only)"
        if arg.startswith("system_key"):
            return None, "Steam-overlay system key (Steam-client only)"
        if arg in ("hold_layer", "add_layer", "set_layer", "remove_layer"):
            # Handled structurally: the action-layer pass converts hold_layer
            # presets into Advanced Presses shift rows and reports the
            # toggle-style layers itself (with the layer's title).
            return None, None
        # Steam Music transport -> the system media/volume equivalents.
        sm = {"steammusic_playpause": "media_playpause",
              "steammusic_next": "media_next",
              "steammusic_prev": "media_prev",
              "steammusic_volup": "volume_up",
              "steammusic_voldown": "volume_down",
              "steammusic_togglemute": "volume_mute"}.get(arg)
        if sm:
            return sm, None
        return None, "controller action '%s'" % arg
    if btype == "mode_shift":
        # Handled structurally (the shift-layer pass turns the layer into
        # Advanced Presses rows); the holder's own binding is just "be the
        # shift", so there's nothing to report per binding.
        return None, None
    if btype in ("hold_layer", "set_layer", "apply_layer", "remove_layer"):
        return None, "action layer"
    if btype == "game_action":
        return None, "in-game action (Steamworks-only)"
    return None, "binding '%s'" % btype


# Base action ids a modifier combo may collapse onto  mirrors the picker's
# _MOD_ELIGIBLE_IDS (keybinds_picker) so every emitted "ctrl+x" value keeps
# its modifier chip when applied. Keep the two in sync.
_COMBO_BASES = frozenset(
    {"enter", "escape", "tab", "backspace", "delete", "space",
     "up", "down", "left", "right", "pageup", "pagedown", "home", "end",
     "comma", "period", "slash", "backslash", "semicolon", "quote",
     "lbracket", "rbracket", "minus", "equals", "backtick", "insert",
     "kp_plus", "kp_minus", "kp_multiply", "kp_divide", "kp_period",
     "kp_enter"}
    | set("abcdefghijklmnopqrstuvwxyz")
    | set("0123456789")
    | {"f%d" % n for n in range(1, 13)}
    | {"kp_%d" % n for n in range(10)}
)


def _map_bindings(bindings):
    """A Full_Press activator's binding list ->
    (action_id | None, [extra action_ids], [skips]).
    Multiple key_press bindings that form modifiers+one-key collapse into our
    'ctrl+alt+x' combo value; otherwise the first mappable binding is the
    control's action and the REST come back as extras (Steam fires them all
    simultaneously  the gamepad tab plays them as "Extra Action" rows)."""
    skips = []
    singles = []
    for b in bindings:
        if not isinstance(b, str):
            continue
        body = b.split(",", 1)[0].strip().split()
        if len(body) >= 2 and body[0].lower() == "key_press":
            singles.append(body[1].lower())
    if len(singles) >= 2 and len(singles) == len(bindings):
        mods = [_KEY_MODS[k] for k in singles if k in _KEY_MODS]
        bases = [k for k in singles if k not in _KEY_MODS]
        if mods and len(bases) == 1:
            base = _map_key_name(bases[0])
            if base in _COMBO_BASES:
                ordered = [m for m in ("ctrl", "shift", "alt", "win")
                           if m in mods]
                return "+".join(ordered + [base]), [], skips
    action = None
    extras = []
    for b in bindings:
        if not isinstance(b, str):
            continue
        mapped, reason = _map_single(b)
        if mapped and action is None:
            action = mapped
        elif mapped and action is not None:
            extras.append(mapped)
        elif reason:
            skips.append(reason)
    return action, extras, skips


# "chord" is NOT here: a chord binding fires only while ANOTHER button is
# held, so playing it as the control's plain action would fire it without
# the guard  chords convert to Mode-Shift rows (or report) instead.
_PREFERRED_ACTIVATORS = ("full_press", "start_press", "release",
                         "long_press", "double_press")

# Secondary activators our Advanced Presses rows can express natively.
_ADV_ACTIVATORS = {"long_press": "long", "double_press": "double"}


def _activator_bindings(by_name, name):
    """ALL binding strings of one activator type, merged across duplicate
    blocks  Steam stores each extra binding added in its UI as ANOTHER
    activator entry of the same type, so taking only the first silently
    dropped every "additional binding"."""
    bindings = []
    for entry in _as_list(by_name.get(name)):
        if not isinstance(entry, dict):
            continue
        for bb in _as_list(entry.get("bindings")):
            if isinstance(bb, dict):
                bindings.extend(_as_list(bb.get("binding")))
    return bindings


_TOUCH_MENU_BTN_RE = re.compile(r"^touch_menu_button_(\d+)$")


def _touch_menu_entries(group):
    """A touch_menu group's inputs -> (Virtual-Menu entries, [skips]).
    Buttons come back in Steam's slot order; entries whose bindings are
    controller outputs (meaningless in a menu that fires desktop actions)
    or unmappable turn into empty cells with a report."""
    numbered = []
    for iname, blk in (group.get("inputs") or {}).items():
        m = _TOUCH_MENU_BTN_RE.match(iname.lower())
        if not m or not isinstance(blk, dict):
            continue
        acts = blk.get("activators")
        by_name = {k.lower(): v for k, v in acts.items()} \
            if isinstance(acts, dict) else {}
        a, _extras, sk = _map_bindings(_activator_bindings(by_name,
                                                          "full_press"))
        numbered.append((int(m.group(1)), a, sk))
    numbered.sort()
    entries, skips = [], []
    for _n, a, sk in numbered[:16]:
        skips.extend(sk)
        if a is None:
            a = "none"
        elif a in _XINPUT_IDS or a == "analog":
            skips.append("button %d is a controller output" % (_n + 1))
            a = "none"
        entries.append({"icon": "none", "action": a})
    return entries, skips


def _chord_rows(entries, adv_sink, adv_cid, cids):
    """Steam "chord" activator entries = hold ANOTHER button + press this
    input. Each entry names its holder (settings.chord_button, an index in
    the _GYRO_FLAG_SLOTS numbering)  exactly an Advanced Presses Mode-Shift
    row: hold `holder`, press `adv_cid` -> action. Entries that can't
    convert (no adv context, holder absent on this kind, holder == target)
    come back as skip strings."""
    skips = []
    for entry in _as_list(entries):
        if not isinstance(entry, dict):
            continue
        bindings = []
        for bb in _as_list(entry.get("bindings")):
            if isinstance(bb, dict):
                bindings.extend(_as_list(bb.get("binding")))
        a, _extras, sk = _map_bindings(bindings)
        skips.extend(sk)
        if a is None or a == "analog":
            continue
        settings = entry.get("settings") or {}
        try:
            idx = int(str(settings.get("chord_button", "")), 0)
        except (TypeError, ValueError):
            idx = -1
        holder = None
        spec = _GYRO_FLAG_SLOTS.get(idx)
        if spec is not None and cids is not None:
            grp, name = spec
            if grp in ("face", "dpad"):
                holder = name
            elif grp == "slot":
                holder = cids.get(name)
            # "touch" slots stay None: pad-touch isn't an adv-row holder.
        if adv_sink is None or adv_cid is None or holder is None \
                or holder == adv_cid:
            skips.append("Chord activator")
            continue
        adv_sink.append({"control": holder, "press": "shift",
                         "target": adv_cid, "action": a})
    return skips


def _input_action(input_block, adv_sink=None, adv_cid=None, cids=None):
    """One group input's activator map -> (action | None, [skips]). Prefers
    Full_Press for the control's regular action. Long_Press / Double_Press
    activators become Advanced Presses rows when `adv_sink` (the convert()
    adv list) and `adv_cid` (our control id) are given; Chord activators
    become Mode-Shift rows (needs `cids` too); anything else secondary is
    reported."""
    acts = input_block.get("activators")
    if not isinstance(acts, dict):
        return None, []
    by_name = {k.lower(): v for k, v in acts.items()}
    chosen = None
    for name in _PREFERRED_ACTIVATORS:
        if name in by_name:
            chosen = name
            break
    if chosen is None and by_name:
        chosen = next((n for n in by_name if n != "chord"), None)
    skips = []
    if "chord" in by_name:
        skips.extend(_chord_rows(by_name["chord"], adv_sink, adv_cid, cids))
    for name in by_name:
        if name == chosen or name == "full_press" or name == "chord":
            continue
        adv_press = _ADV_ACTIVATORS.get(name)
        if adv_press and adv_sink is not None and adv_cid is not None:
            a, _extras, sk = _map_bindings(_activator_bindings(by_name, name))
            skips.extend(sk)
            if a is not None and a != "analog":
                adv_sink.append({"control": adv_cid, "press": adv_press,
                                 "action": a})
                continue
            if a is None and not sk:
                # Nothing mappable AND nothing reportable in this activator:
                # empty blocks, or layer actions the layer pass handles.
                continue
        skips.append("%s activator" % name.replace("_", " ").title())
    action, extras, more = _map_bindings(_activator_bindings(by_name, chosen))
    # An input whose ONLY press is a Long/Double Press (no full press at
    # all) keeps that shape when adv rows are available: the plain press
    # stays unbound, exactly like Steam. Without adv rows (Chords tab,
    # stick directions) the action downgrades to the plain press instead.
    chosen_adv = _ADV_ACTIVATORS.get(chosen or "")
    if chosen_adv and adv_sink is not None and adv_cid is not None \
            and action is not None and action != "analog":
        adv_sink.append({"control": adv_cid, "press": chosen_adv,
                         "action": action})
        more = more + ["extra binding '%s'" % e
                       for e in extras if e != "analog"]
        return None, skips + more
    # Steam fires every binding of one activator TOGETHER  extras become
    # "Extra Action (held)" rows on the gamepad tab, reported elsewhere.
    for e in extras:
        if e == "analog":
            continue
        if adv_sink is not None and adv_cid is not None:
            adv_sink.append({"control": adv_cid, "press": "plus",
                             "action": e})
        else:
            more = more + ["extra binding '%s'" % e]
    return action, skips + more


# ---------------------------------------------------------------------------
# config model helpers
# ---------------------------------------------------------------------------

def _groups_by_id(cm):
    out = {}
    for g in _as_list(cm.get("group")):
        if isinstance(g, dict) and "id" in g:
            out[str(g["id"])] = g
    return out


def _first_preset(cm, preset_id=None):
    presets = [p for p in _as_list(cm.get("preset")) if isinstance(p, dict)]
    if not presets:
        return None, 0
    presets.sort(key=lambda p: str(p.get("id", "")))
    if preset_id is not None:
        for p in presets:
            if str(p.get("id")) == str(preset_id):
                return p, len(presets)
    # "Default" (id 0) first when present; extra action sets are reported.
    return presets[0], len(presets)


def _preset_title(cm, preset):
    """Human title for a preset/action set (falls back to its name)."""
    name = str(preset.get("name", "") or "")
    for src in ("actions", "action_layers"):
        blk = cm.get(src)
        if isinstance(blk, dict) and isinstance(blk.get(name), dict):
            t = str(blk[name].get("title", "") or "")
            if t and not t.startswith("#"):
                return t
    return name or "Set %s" % preset.get("id", "?")


def list_action_sets(text):
    """[(preset_id, title), ...] for a config's action sets  the browser's
    import step offers a choice when there's more than one. [] on any parse
    problem (the import path will surface the real error)."""
    try:
        root = parse_vdf(text)
        cm = root.get("controller_mappings")
        if isinstance(cm, list):
            cm = cm[0]
        if not isinstance(cm, dict):
            return []
        presets = [p for p in _as_list(cm.get("preset"))
                   if isinstance(p, dict)]
        presets.sort(key=lambda p: str(p.get("id", "")))
        return [(str(p.get("id", "")), _preset_title(cm, p))
                for p in presets]
    except Exception:
        return []


def _active_groups(preset, groups, source):
    """The non-modeshift ACTIVE group(s) bound to `source` in this preset."""
    gsb = preset.get("group_source_bindings")
    out = []
    if not isinstance(gsb, dict):
        return out
    for gid, desc in gsb.items():
        for d in _as_list(desc):
            if not isinstance(d, str):
                continue
            parts = d.split()
            if not parts or parts[0] != source:
                continue
            if "active" not in parts[1:] or "modeshift" in parts[1:]:
                continue
            g = groups.get(str(gid))
            if g is not None:
                out.append(g)
    return out


def _modeshift_groups(preset, groups):
    """{source: modeshift group} for every source carrying a LIVE shift
    layer. Inactive modeshift groups are leftovers from editing  Steam
    ignores them too, so they're not worth a skip line."""
    gsb = preset.get("group_source_bindings")
    out = {}
    if isinstance(gsb, dict):
        for gid, desc in gsb.items():
            for d in _as_list(desc):
                if not isinstance(d, str):
                    continue
                parts = d.split()
                if parts and "modeshift" in parts[1:] \
                        and "active" in parts[1:]:
                    g = groups.get(str(gid))
                    if g is not None:
                        out[parts[0]] = g
    return out


_MODE_SHIFT_RE = re.compile(r"^mode_shift\s+(\S+)\s+(\S+)", re.I)
_LAYER_RE = re.compile(
    r"^controller_action\s+(hold_layer|add_layer|set_layer)\s+(\d+)", re.I)


def _digital_inputs(preset, groups, cids):
    """Every (our cid, input block) pair across the preset's active digital
    sources  the shared walk the mode-shift machinery uses both to find the
    SHIFT BUTTON (the control bound to `mode_shift <source> <gid>`) and to
    convert a shift layer's own button groups."""
    out = []

    def _grp_inputs(source, table):
        for g in _active_groups(preset, groups, source):
            for iname, blk in (g.get("inputs") or {}).items():
                if not isinstance(blk, dict):
                    continue
                cid = table(iname.lower())
                if cid is not None:
                    out.append((cid, blk))

    _grp_inputs("switch", lambda n: cids.get(_SWITCH_INPUTS.get(n, "")))
    _grp_inputs("button_diamond", lambda n: _FACE_INPUTS.get(n))
    _grp_inputs("dpad", lambda n: ("dpad_" + _DPAD_INPUTS[n])
                if n in _DPAD_INPUTS else None)
    _grp_inputs("joystick",
                lambda n: cids["ls_click"] if n == "click" else None)
    _grp_inputs("right_joystick",
                lambda n: cids["rs_click"] if n == "click" else None)
    _grp_inputs("left_trigger",
                lambda n: cids["lt"] if n == "click" else None)
    _grp_inputs("right_trigger",
                lambda n: cids["rt"] if n == "click" else None)
    _grp_inputs("left_trackpad",
                lambda n: cids.get("lpad") if n == "click" else None)
    _grp_inputs("right_trackpad",
                lambda n: cids.get("rpad") if n == "click" else None)
    return out


def _group_button_actions(source, group, cids, skipped, label):
    """A (shift-layer) group's digital inputs -> {our cid: action value}.
    Only button-shaped groups convert (four_buttons / dpad / switches-style
    inputs); analog modes in a shift layer are reported."""
    if source == "gyro":
        # A gyro layer's "dpad" inputs are TILT directions, not buttons 
        # mapping them onto the real dpad would be wrong. Callers report.
        return {}
    mode = str(group.get("mode", "")).lower()
    inputs = group.get("inputs") or {}
    table = {}
    if source == "button_diamond":
        table = _FACE_INPUTS
    elif source == "dpad" or mode == "dpad":
        table = {k: "dpad_" + v for k, v in _DPAD_INPUTS.items()}
    elif source == "switch":
        table = {k: cids.get(v) for k, v in _SWITCH_INPUTS.items()}
    out = {}
    for iname, blk in inputs.items():
        if not isinstance(blk, dict):
            continue
        low = iname.lower()
        cid = table.get(low)
        if cid is None and source in ("joystick", "right_joystick") \
                and low in _DPAD_INPUTS:
            # A shifted stick in dpad mode: directions become the stick's
            # digital zone binds.
            pre = "lstick_" if source == "joystick" else "rstick_"
            cid = pre + _DPAD_INPUTS[low]
        if cid is None and low == "click":
            cid = {"joystick": cids["ls_click"],
                   "right_joystick": cids["rs_click"],
                   "left_trigger": cids["lt"],
                   "right_trigger": cids["rt"],
                   "left_trackpad": cids.get("lpad"),
                   "right_trackpad": cids.get("rpad")}.get(source)
        if cid is None:
            continue
        action, sk = _input_action(blk)
        skipped.extend("%s (shifted): %s" % (label, s) for s in sk)
        if action is not None:
            out[cid] = action
    return out


# ---------------------------------------------------------------------------
# per-kind control ids
# ---------------------------------------------------------------------------

def _cids(kind):
    """Semantic slot -> this kind's control id (None = kind lacks it)."""
    hid = kind in _HID_KINDS
    return {
        "lb": "l1" if hid else "l",
        "rb": "r1" if hid else "r",
        "lt": "l2" if hid else "zl",
        "rt": "r2" if hid else "zr",
        "start": "start" if hid else "plus",      # Start/Menu (≡)
        "back": "back" if hid else "minus",       # Select/View (⧉)
        "ls_click": "lstick_click" if hid else "l3",
        "rs_click": "rstick_click" if hid else "r3",
        "l4": "l4" if hid else None, "l5": "l5" if hid else None,
        "r4": "r4" if hid else None, "r5": "r5" if hid else None,
        "lpad": "lpad" if hid else None,
        "rpad": "rpad" if hid else None,
        "capture": "qam" if hid else "capture",
        # Face buttons under their own names, so switch-group inputs like
        # BUTTON_Y (a favourite mode-shift holder in older exports) resolve
        # through the same slot table as everything else.
        "a": "a", "b": "b", "x": "x", "y": "y",
    }


# Steam "switch"-group input name -> semantic slot above. The two
# *_trigger_threshold entries are the triggers' SOFT PULL exposed as a
# switch input  a favourite mode-shift holder ("hold L2 → D-pad = items");
# a real ACTION on one becomes a Soft Pull row instead of a plain bind.
# Configs also put trigger FULL pulls, stick/pad clicks and even face
# buttons into the switch group (mostly as mode-shift / layer holders) 
# all mapped so those holders are found instead of skipped.
_SWITCH_INPUTS = {
    "left_bumper": "lb", "right_bumper": "rb",
    "button_escape": "start", "button_menu": "back",
    "button_back_left": "l5", "button_back_right": "r5",
    "button_back_left_upper": "l4", "button_back_right_upper": "r4",
    "left_grip": "l5", "right_grip": "r5",
    "button_capture": "capture",
    "left_trigger_threshold": "lt", "right_trigger_threshold": "rt",
    "left_trigger": "lt", "right_trigger": "rt",
    "left_click": "lpad", "right_click": "rpad",
    "left_stick_click": "ls_click", "right_stick_click": "rs_click",
    "joystick_click": "ls_click",
    "button_a": "a", "button_b": "b", "button_x": "x", "button_y": "y",
}

_DPAD_INPUTS = {"dpad_north": "up", "dpad_south": "down",
                "dpad_west": "left", "dpad_east": "right"}

_FACE_INPUTS = {"button_a": "a", "button_b": "b",
                "button_x": "x", "button_y": "y"}

# Gyro-button bitmask -> semantic slot (best-effort community decode; the
# same table SteamInputDB's own preview uses). Slots resolve per kind via
# _cids; unmappable flags are reported.
_GYRO_FLAG_SLOTS = {
    4: ("face", "a"), 5: ("face", "b"), 6: ("face", "x"), 7: ("face", "y"),
    8: ("dpad", "dpad_up"), 9: ("dpad", "dpad_down"),
    10: ("dpad", "dpad_left"), 11: ("dpad", "dpad_right"),
    12: ("slot", "rpad"), 13: ("slot", "lpad"),
    14: ("slot", "lb"), 15: ("slot", "ls_click"),
    16: ("slot", "lt"), 17: ("slot", "lt"),
    18: ("slot", "rt"), 19: ("slot", "rt"),
    21: ("slot", "rb"), 22: ("slot", "start"), 25: ("slot", "back"),
    # Touch sensors  "touch" resolves only on the HID kinds (SC / Deck),
    # whose SCButtons expose the touch bits as chordable buttons. Pad touch
    # (0/1, plus 20/26 aliases) is THE classic "gyro while thumb rests on
    # the pad" setup.
    0: ("touch", "rpad_touch"), 1: ("touch", "lpad_touch"),
    20: ("touch", "rpad_touch"), 26: ("touch", "rpad_touch"),
    2: ("touch", "rstick_touch"), 3: ("touch", "lstick_touch"),
    41: ("slot", "l4"), 42: ("slot", "r4"),
    43: ("slot", "l5"), 47: ("slot", "r5"),
}
_GYRO_FLAG_NAMES = {
    0: "right pad touch", 1: "left pad touch", 20: "right pad touch",
    26: "right pad touch", 23: "right stick deflection",
    24: "stick deflection", 30: "left stick deflection",
    # 2/3 are the capacitive stick-touch slots  named here for the kinds
    # whose slot lookup fails (non-HID pads have no touch controls).
    2: "right stick touch", 3: "left stick touch",
    44: "left gripsense", 45: "right gripsense",
}


def _gyro_hold_buttons(mask_str, kind, skipped):
    """Decode a gyro_button mask into up to 2 of our chord button ids."""
    try:
        mask = int(str(mask_str), 0)
    except (TypeError, ValueError):
        return []
    if mask <= 0:
        return []
    cids = _cids(kind)
    hid = kind in _HID_KINDS
    out = []
    for bit in range(64):
        if not mask & (1 << bit):
            continue
        spec = _GYRO_FLAG_SLOTS.get(bit)
        cid = None
        if spec is not None:
            grp, name = spec
            if grp in ("face", "dpad"):
                cid = name
            elif grp == "slot":
                cid = cids.get(name)
            elif grp == "touch" and hid:
                cid = name
        if cid is not None:
            if cid not in out:
                out.append(cid)
        else:
            nm = _GYRO_FLAG_NAMES.get(bit, "button bit %d" % bit)
            skipped.append("Gyro button '%s' isn't selectable here" % nm)
    if len(out) > 2:
        skipped.append("Gyro uses %d buttons  kept the first 2" % len(out))
        out = out[:2]
    return out


# ---------------------------------------------------------------------------
# convert()
# ---------------------------------------------------------------------------

_MOUSE_MODES = ("absolute_mouse", "joystick_mouse", "mouse_region")

# Steam's special config catalogs (configs FOR these app ids are the ones
# Steam itself applies outside games): the Desktop layout and the Steam-
# button chord layout. The picker's Desktop / Chords tabs browse these.
DESKTOP_APP_ID = "413080"
CHORDS_APP_ID = "443510"

# Our xinput output ids  meaningless on the keyboard/mouse-only tabs.
_XINPUT_IDS = frozenset(_XINPUT_MAP.values())


def convert(text, kind, mode="gamepad", preset_id=None):
    """Map a Steam Input controller-config VDF onto `kind`'s binds for one
    picker tab. `mode` = "gamepad" (default  the merged Xbox+desktop
    vocabulary), "pc" (Desktop tab) or "guide" (Chords tab  bindings that
    fire while Steam/Home is held); the pc/guide tabs are keyboard/mouse
    only, so controller (XInput) outputs are reported instead of bound and
    analog stick/trigger passthrough has no meaning there. Returns a dict:

      ok       False + 'error' when the file isn't a controller config.
      title / description / creator / controller_type   config metadata.
      binds    {cid: action value}   SPARSE: only controls the config sets.
               Callers reset the rest of the tab to defaults (importing a
               config replaces the layout, exactly like applying it in Steam).
      adv      Advanced Presses rows (gamepad mode only; [] otherwise).
      gyro     None, or {"mode": ..., "output": "mouse"|"rstick", "sens":
               float (optional), "buttons": [chord ids]}  gamepad/pc only.
      vmenus   [{"pad": "lpad"|"rpad", "type": "touch"|"radial"|"hotbar",
               "entries": [{"icon", "action"}]}]  Steam touch/radial/
               hotbar menus, importable as Virtual Menus (HID kinds,
               gamepad/pc modes; [] otherwise).
      skipped  [str]  everything the config asked for that we can't express.
      notes    [str]  informational (extra action sets, source of a value).
    """
    try:
        root = parse_vdf(text)
    except ValueError as e:
        return {"ok": False, "error": "Not a valid VDF file (%s)" % e}
    cm = root.get("controller_mappings")
    if isinstance(cm, list):
        cm = cm[0]
    if not isinstance(cm, dict):
        return {"ok": False, "error": "No controller_mappings in this file"}

    skipped, notes = [], []
    binds = {}
    convert_mode = mode      # group handlers shadow `mode` locally
    kb_only = mode in ("pc", "guide")
    # Advanced Presses rows exist on the Gamepad AND Desktop tabs (the
    # engine runs in both modes); only the Chords tab reports them skipped.
    adv = []
    adv_sink = adv if mode in ("gamepad", "pc") else None
    cids = _cids(kind)
    groups = _groups_by_id(cm)
    preset, n_presets = _first_preset(cm, preset_id)
    if preset is None:
        return {"ok": False, "error": "Config has no preset/action set"}
    if n_presets > 1 and preset_id is None:
        notes.append("Config has %d action sets  imported the default one"
                     % n_presets)

    # --- mode-shift layers → Advanced Presses "shift" rows -------------------
    # A mode shift = hold ONE button → a source's controls swap to an
    # alternate group. Find each shifted source's TRIGGER (the control whose
    # binding is `mode_shift <source> <gid>`) and flatten the shift group's
    # buttons into {"press": "shift", "control": trigger, "target": cid,
    # "action": ...} rows the AdvPressEngine plays back. Gamepad tab only;
    # non-button shift layers (a shifted gyro/pad MODE) still report.
    ms_groups = _modeshift_groups(preset, groups)
    if ms_groups:
        triggers = {}        # source -> trigger cid (from mode_shift binds)
        for cid, blk in _digital_inputs(preset, groups, cids):
            acts = blk.get("activators")
            if not isinstance(acts, dict):
                continue
            by_name = {k.lower(): v for k, v in acts.items()}
            for aname in by_name:
                for b in _activator_bindings(by_name, aname):
                    if not isinstance(b, str):
                        continue
                    m = _MODE_SHIFT_RE.match(b.split(",", 1)[0].strip())
                    if m:
                        triggers.setdefault(m.group(1).lower(), cid)
        # Sources whose shift holder exists SOMEWHERE in this preset 
        # including inputs we can't map (pad touch, inside another layer).
        # An active modeshift group with NO holder anywhere is dead residue
        # from editing: Steam can't reach it either, so no report.
        reachable = set()
        gsb_all = preset.get("group_source_bindings")
        for gid in (gsb_all if isinstance(gsb_all, dict) else {}):
            g = groups.get(str(gid))
            if not isinstance(g, dict):
                continue
            for blk in (g.get("inputs") or {}).values():
                if not isinstance(blk, dict):
                    continue
                acts = blk.get("activators")
                if not isinstance(acts, dict):
                    continue
                by_name = {k.lower(): v for k, v in acts.items()}
                for aname in by_name:
                    for b in _activator_bindings(by_name, aname):
                        if isinstance(b, str):
                            m = _MODE_SHIFT_RE.match(
                                b.split(",", 1)[0].strip())
                            if m:
                                reachable.add(m.group(1).lower())
        for src, g in sorted(ms_groups.items()):
            label = "%s shift layer" % src.replace("_", " ")
            trig_cid = triggers.get(src)
            if trig_cid is None and src not in reachable:
                continue        # dead layer  no holder binding at all
            if mode == "guide" or trig_cid is None:
                skipped.append("Mode-shift layer on %s"
                               % src.replace("_", " "))
                continue
            actions = _group_button_actions(src, g, cids, skipped, label)
            if not actions:
                skipped.append("Mode-shift layer on %s (nothing mappable)"
                               % src.replace("_", " "))
                continue
            for tcid, action in actions.items():
                adv.append({"control": trig_cid, "press": "shift",
                            "target": tcid, "action": action})

    # --- hold-type action layers → shift rows too ---------------------------
    # `controller_action hold_layer <preset id>` = hold this control → the
    # whole layer preset applies until release: same shape as a mode shift,
    # spanning every source the layer redefines. Its button-shaped groups
    # flatten into shift rows on the holder; toggle layers (add/set_layer)
    # can't be a held shift and are reported with the layer's title.
    presets_by_id = {str(p.get("id", "")): p
                     for p in _as_list(cm.get("preset")) if isinstance(p, dict)}
    layer_seen = set()
    for cid, blk in _digital_inputs(preset, groups, cids):
        acts = blk.get("activators")
        if not isinstance(acts, dict):
            continue
        by_name = {k.lower(): v for k, v in acts.items()}
        for aname in by_name:
            if aname not in ("full_press", "start_press", "long_press"):
                continue
            if aname == "long_press":
                # A long-press layer converts to a plain held shift only
                # when the control has no regular action to lose  else the
                # generic "Long Press activator" report stands.
                base, _x, _sk = _map_bindings(
                    _activator_bindings(by_name, "full_press"))
                if base is not None:
                    continue
            for b in _activator_bindings(by_name, aname):
                if not isinstance(b, str):
                    continue
                m = _LAYER_RE.match(b.split(",", 1)[0].strip())
                if not m:
                    continue
                verb, lid = m.group(1).lower(), m.group(2)
                if (cid, lid) in layer_seen:
                    continue
                layer_seen.add((cid, lid))
                lp = presets_by_id.get(lid)
                title = _preset_title(cm, lp) if lp else "layer %s" % lid
                nice = cid.replace("_", " ")
                if verb != "hold_layer" or convert_mode == "guide" \
                        or lp is None:
                    skipped.append("%s: toggles action layer '%s' (only "
                                   "hold-layers import)" % (nice, title))
                    continue
                lrows = {}
                lgsb = lp.get("group_source_bindings")
                if isinstance(lgsb, dict):
                    for lgid, ldesc in lgsb.items():
                        for d in _as_list(ldesc):
                            parts = d.split() if isinstance(d, str) else []
                            if not parts or "active" not in parts[1:] \
                                    or "modeshift" in parts[1:]:
                                continue
                            lg = groups.get(str(lgid))
                            if lg is None:
                                continue
                            lrows.update(_group_button_actions(
                                parts[0], lg, cids, skipped,
                                "'%s' layer" % title))
                lrows.pop(cid, None)     # the holder can't be its own target
                if not lrows:
                    skipped.append("Hold layer '%s' on %s (nothing mappable)"
                                   % (title, nice))
                    continue
                for tcid, action in lrows.items():
                    adv.append({"control": cid, "press": "shift",
                                "target": tcid, "action": action})

    missing = []        # [(label, action)]  coalesced report lines at the end
    kb_xi = []          # kb-tab controller outputs  likewise coalesced

    def _bind(cid, action, what):
        if action is None:
            return
        if cid is None:
            missing.append((what, action))
            return
        if kb_only and (action in _XINPUT_IDS or action == "analog"):
            kb_xi.append(what)
            return
        if action == "analog" and not (cid.startswith("lstick_")
                                       or cid.startswith("rstick_")):
            # A stick-direction xinput output somewhere that isn't that
            # stick direction (e.g. a paddle emitting rstick_left).
            skipped.append("%s: stick-direction output" % what)
            return
        binds[cid] = action

    def _one(source):
        gs = _active_groups(preset, groups, source)
        return gs[0] if gs else None

    # --- switches (bumpers, Start/Select, grips, capture) --------------------
    g = _one("switch")
    if g is not None:
        for iname, blk in (g.get("inputs") or {}).items():
            if not isinstance(blk, dict):
                continue
            low = iname.lower()
            slot = _SWITCH_INPUTS.get(low)
            label = iname.replace("button_", "").replace("_", " ")
            cid = cids.get(slot) if slot else None
            action, sk = _input_action(blk, adv_sink, cid, cids)
            skipped.extend("%s: %s" % (label, s) for s in sk)
            if slot is None:
                if action is not None:
                    if label.lower().startswith("macro"):
                        # Extra macro paddles this kind doesn't have (raw
                        # input name "button_macro0")  fold into the one
                        # "Not on this controller" line.
                        missing.append((label, action))
                    elif low.startswith("always_on"):
                        skipped.append("Always-on action isn't supported")
                    else:
                        skipped.append("Unsupported button '%s'" % label)
                continue
            if slot in ("lt", "rt") and low.endswith("_threshold"):
                # Trigger soft-pull as a switch input: an action here fires
                # at the light pull  exactly our Soft Pull row. (Its far
                # more common use, being a mode-shift holder, has action
                # None and is handled by the shift-layer pass.)
                if action is not None:
                    if adv_sink is not None:
                        adv_sink.append({"control": cid, "press": "soft",
                                         "action": action})
                    else:
                        skipped.append("%s: soft-pull binding" % label)
                continue
            # A trigger FULL pull / stick click / pad click / face button in
            # the switch group binds like the control itself would (the
            # dedicated source group, when present, refines it afterwards).
            _bind(cid, action, label)

    # --- face buttons ---------------------------------------------------------
    g = _one("button_diamond")
    if g is not None:
        for iname, blk in (g.get("inputs") or {}).items():
            cid = _FACE_INPUTS.get(iname.lower())
            if cid is None or not isinstance(blk, dict):
                continue
            action, sk = _input_action(blk, adv_sink, cid, cids)
            skipped.extend("%s: %s" % (cid.upper(), s) for s in sk)
            _bind(cid, action, cid.upper())

    # --- d-pad ------------------------------------------------------------------
    g = _one("dpad")
    if g is not None:
        for iname, blk in (g.get("inputs") or {}).items():
            d = _DPAD_INPUTS.get(iname.lower())
            if d is None or not isinstance(blk, dict):
                continue
            action, sk = _input_action(blk, adv_sink, "dpad_" + d, cids)
            skipped.extend("D-pad %s: %s" % (d, s) for s in sk)
            _bind("dpad_" + d, action, "D-pad " + d)

    # --- sticks -------------------------------------------------------------
    def _stick(source, prefix, click_cid, label):
        g = _one(source)
        if g is None:
            return
        mode = str(g.get("mode", "")).lower()
        inputs = g.get("inputs") or {}
        if mode in ("dpad", "four_buttons"):
            # Fully digital stick: each direction drives its bound action.
            table = _DPAD_INPUTS if mode == "dpad" else None
            for iname, blk in inputs.items():
                if not isinstance(blk, dict):
                    continue
                low = iname.lower()
                d = (table.get(low) if table
                     else {"button_y": "up", "button_a": "down",
                           "button_x": "left", "button_b": "right"}.get(low))
                if d is None:
                    if low != "click":
                        continue
                    action, sk = _input_action(blk, adv_sink, click_cid,
                                               cids)
                    skipped.extend("%s click: %s" % (label, s) for s in sk)
                    _bind(click_cid, action, label + " click")
                    continue
                action, sk = _input_action(blk)
                skipped.extend("%s %s: %s" % (label, d, s) for s in sk)
                _bind(prefix + d, action, "%s %s" % (label, d))
        else:
            if kb_only:
                # No analog passthrough on the Desktop/Chords tabs: a mouse-
                # like stick becomes the Desktop tab's joystick_mouse mode;
                # anything else analog is reported.
                if mode in _MOUSE_MODES or mode == "mouse_joystick":
                    if convert_mode == "pc":
                        for d in ("up", "down", "left", "right"):
                            binds[prefix + d] = "joystick_mouse"
                    elif prefix == "rstick_":
                        # The app ALREADY moves the cursor with the right
                        # stick while Steam/Home is held (desktop and
                        # gamepad mode both)  nothing to import, nothing
                        # lost.
                        notes.append("Right stick as mouse  the right "
                                     "stick already moves the cursor while "
                                     "Steam is held")
                    else:
                        skipped.append("%s as mouse (Chords tab has no "
                                       "stick-mouse)" % label)
                elif mode:
                    skipped.append("%s as '%s'"
                                   % (label, mode.replace("_", " ")))
            else:
                if mode not in ("joystick_move", "joystick_camera", ""):
                    # Informational, not a loss: the stick still works, just
                    # without the config's fancy mode (flick stick, ...).
                    notes.append("%s as '%s'  kept as the analog stick"
                                 % (label, mode.replace("_", " ")))
                for d in ("up", "down", "left", "right"):
                    binds[prefix + d] = "analog"
            blk = inputs.get("click")
            if isinstance(blk, dict):
                action, sk = _input_action(blk, adv_sink, click_cid, cids)
                skipped.extend("%s click: %s" % (label, s) for s in sk)
                _bind(click_cid, action, label + " click")

    _stick("joystick", "lstick_", cids["ls_click"], "Left stick")
    _stick("right_joystick", "rstick_", cids["rs_click"], "Right stick")

    # --- triggers -------------------------------------------------------------
    def _trigger(source, cid, analog_id, label):
        g = _one(source)
        if g is None:
            return
        inputs = g.get("inputs") or {}
        click = inputs.get("click")
        edge = inputs.get("edge")
        action = None
        if isinstance(click, dict):
            action, sk = _input_action(click, adv_sink, cid, cids)
            skipped.extend("%s: %s" % (label, s) for s in sk)
        if action is None and isinstance(edge, dict):
            action, sk = _input_action(edge)
            skipped.extend("%s soft pull: %s" % (label, s) for s in sk)
            if action is not None:
                notes.append("%s soft-pull binding imported as full pull"
                             % label)
        elif isinstance(edge, dict):
            # Full pull AND a separate soft pull: the light pull becomes an
            # Advanced Presses "soft" row (gamepad tab only; the runtime
            # asserts it past the light-pull threshold, independent of the
            # full-pull action).
            e_action, _ = _input_action(edge)
            if e_action is not None and e_action != action:
                if adv_sink is not None:
                    adv_sink.append({"control": cid, "press": "soft",
                                     "action": e_action})
                else:
                    skipped.append("%s separate soft-pull binding" % label)
        settings = g.get("settings") or {}
        analog_out = str(settings.get("output_trigger", "")).strip()
        if kb_only:
            # Desktop/Chords triggers are plain actions  no analog concept.
            if action is not None and action not in ("lt", "rt"):
                _bind(cid, action, label)
        elif action is None or action == analog_id:
            # Passthrough  but don't clobber a bind the switch group
            # already set (a trigger full-pull switch input).
            binds.setdefault(cid, analog_id)
        else:
            binds[cid] = action
            if analog_out:
                skipped.append("%s analog output (trigger is now a button)"
                               % label)

    _trigger("left_trigger", cids["lt"], "lt", "Left trigger")
    _trigger("right_trigger", cids["rt"], "rt", "Right trigger")

    # --- trackpads (HID kinds only) -------------------------------------------
    no_pad = []
    vmenus = []
    for source, slot, label in (("left_trackpad", "lpad", "Left trackpad"),
                                ("right_trackpad", "rpad", "Right trackpad")):
        g = _one(source)
        if g is None:
            continue
        mode = str(g.get("mode", "")).lower()
        cid = cids.get(slot)
        if cid is None:
            if g.get("inputs"):
                no_pad.append(label.split()[0].lower())
            continue
        # Steam touch / radial / hotbar pad menus ALL import as a TOUCH-style
        # Virtual Menu (only the Touch type is supported for now; radial/hotbar
        # are future work, so their entries just land on a touch menu). The
        # entry mapping is identical across the three, so this is lossless
        # apart from the on-screen shape.
        _VMENU_MODES = ("touch_menu", "radial_menu", "hotbar")
        if mode in _VMENU_MODES and convert_mode != "guide":
            # Shown while the pad is touched, a pad click fires the highlighted
            # cell. The pad click belongs to the menu, so no click binding.
            tm_entries, tm_sk = _touch_menu_entries(g)
            skipped.extend("%s menu: %s" % (label, s) for s in tm_sk)
            nice = mode.replace("_", " ")
            if tm_entries and any(e["action"] != "none"
                                  for e in tm_entries):
                vmenus.append({"pad": slot, "type": "touch",
                               "entries": tm_entries})
                extra = ("" if mode == "touch_menu"
                         else " (%s imports as a Touch Menu for now)" % nice)
                notes.append("%s %s imported as a Virtual Menu "
                             "(Options → Virtual Menus)%s"
                             % (label, nice, extra))
            else:
                skipped.append("%s as '%s' (nothing mappable in it)"
                               % (label, nice))
            continue
        if slot == "lpad" and mode in ("dpad", "four_buttons") \
                and _one("dpad") is None:
            # SC/Deck: the left pad IS the d-pad  a dpad-mode pad group's
            # directions map onto the real dpad controls.
            table = (_DPAD_INPUTS if mode == "dpad"
                     else {"button_y": "up", "button_a": "down",
                           "button_x": "left", "button_b": "right"})
            for iname, blk in (g.get("inputs") or {}).items():
                if not isinstance(blk, dict):
                    continue
                d = table.get(iname.lower())
                if d is not None:
                    action, sk = _input_action(blk, adv_sink, "dpad_" + d,
                                               cids)
                    skipped.extend("%s %s: %s" % (label, d, s) for s in sk)
                    _bind("dpad_" + d, action, "%s %s" % (label, d))
                elif iname.lower() == "click":
                    action, sk = _input_action(blk, adv_sink, cid, cids)
                    skipped.extend("%s click: %s" % (label, s) for s in sk)
                    _bind(cid, action, label + " click")
            continue
        single = mode == "single_button"
        if slot == "lpad" and mode == "scrollwheel":
            # Scroll wheel IS what the left pad does here  the style knob
            # lives in Options → Touchpads. Nothing is lost, so just note it.
            notes.append("Left trackpad scroll wheel  the left pad already "
                         "scrolls (style in Options → Touchpads)")
        elif not single and mode not in _MOUSE_MODES and mode \
                and mode != "reference":
            skipped.append("%s as '%s' (pad modes are set in Options)"
                           % (label, mode.replace("_", " ")))
        blk = (g.get("inputs") or {}).get("click")
        action = None
        if isinstance(blk, dict):
            action, sk = _input_action(blk, adv_sink, cid, cids)
            skipped.extend("%s click: %s" % (label, s) for s in sk)
            _bind(cid, action, label + " click")
        if single:
            # A single-button pad IS its one binding. Click imported above;
            # a touch-fire binding ("touch to press") downgrades to the pad
            # click, with a note. Only a pad with neither still reports.
            if action is None:
                tblk = (g.get("inputs") or {}).get("touch")
                if isinstance(tblk, dict):
                    action, sk = _input_action(tblk, adv_sink, cid, cids)
                    skipped.extend("%s touch: %s" % (label, s) for s in sk)
                    _bind(cid, action, label + " touch")
                    if action is not None:
                        notes.append("%s single-button touch binding "
                                     "imported as the pad click" % label)
            elif action is not None:
                notes.append("%s single-button: its click binding imported"
                             % label)
            if action is None:
                skipped.append("%s as 'single button' (pad modes are set "
                               "in Options)" % label)
    if no_pad:
        skipped.append("%s trackpad bindings (no trackpads on this "
                       "controller)" % " & ".join(no_pad).capitalize())

    # --- gyro -------------------------------------------------------------------
    # Gamepad + Desktop imports carry the gyro setup (it's per-controller,
    # not per-tab); a Chords import leaves gyro untouched.
    gyro = None
    gs = _active_groups(preset, groups, "gyro") if convert_mode != "guide" \
        else []
    if gs:
        g = gs[0]
        mode = str(g.get("mode", "")).lower()
        settings = g.get("settings") or {}
        if mode in _MOUSE_MODES or mode in ("mouse_joystick",
                                            "joystick_move",
                                            "joystick_camera",
                                            "gyro_to_mouse",
                                            "gyro_to_joystick"):
            # mouse_joystick / joystick output (or an explicit right-stick
            # output_joystick) → our "Right Joystick" gyro output; plain
            # mouse modes  including the newer "Gyro To Mouse [BETA]"
            # (gyro_to_mouse)  → the classic gyro mouse.
            out_js = str(settings.get("output_joystick", "")).strip()
            as_stick = (mode in ("mouse_joystick", "joystick_move",
                                 "joystick_camera", "gyro_to_joystick")
                        or out_js in ("1", "2", "4"))
            mask = settings.get("gyro_ratchet_button_mask",
                                settings.get("gyro_button"))
            buttons = _gyro_hold_buttons(mask, kind, skipped)
            invert = str(settings.get("gyro_button_invert", "")).strip()
            gmode = "hold_suppress" if invert == "1" else "hold_enable"
            if not buttons and invert != "1":
                # Always-on gyro (or a hold button we can't map): suppress
                # mode with no hotkey seeds ON  the closest match.
                gmode = "hold_suppress"
            if as_stick and convert_mode == "pc":
                # Desktop tab: there's no virtual pad to steer  fall back
                # to the gyro mouse (the closest desktop behavior).
                as_stick = False
                notes.append("Gyro-to-joystick imported as gyro mouse "
                             "(Desktop layout)")
            gyro = {"mode": gmode, "buttons": buttons,
                    "output": "rstick" if as_stick else "mouse"}
            sens = settings.get("sensitivity")
            s_fmt = "%g%%"
            if sens is None and "gyro_natural_sensitivity" in settings:
                # The newer gyro_to_mouse groups store a "natural
                # sensitivity" (x100) instead of a percent.
                sens = settings["gyro_natural_sensitivity"]
                s_fmt = "natural sensitivity %g"
            try:
                s = float(sens)
                if s > 0:
                    gyro["sens"] = max(0.1, min(30.0, 2.5 * s / 100.0))
                    notes.append("Gyro sensitivity approximated from the "
                                 "config's " + s_fmt % s)
            except (TypeError, ValueError):
                pass
        elif mode and mode != "reference":
            skipped.append("Gyro as '%s'" % mode.replace("_", " "))

    if missing:
        # One line for all of them ("back left, back right, ..."), instead of
        # a report row per absent paddle. Paddles whose action is ALREADY
        # bound on some other control (the classic A/B/X/Y comfort mirror)
        # lose nothing  those go to notes, not the skip report.
        bound_actions = set(binds.values())
        dup = [w for w, a in missing if a in bound_actions]
        lost = [w for w, a in missing if a not in bound_actions]
        if dup:
            notes.append("Not on this controller, but already on other "
                         "buttons (nothing lost): %s" % ", ".join(dup))
        if lost:
            skipped.append("Not on this controller: %s" % ", ".join(lost))

    ctype = str(cm.get("controller_type", "") or "")
    if ctype and TYPE_TO_KIND.get(ctype) not in (None, kind) and \
            kind not in _HID_KINDS:
        notes.append("Config was made for %s" % ctype.replace(
            "controller_", "").replace("_", " ").title())

    # De-duplicate adv rows (a control keeps its FIRST long/double/soft row;
    # shift rows are per (holder, target); plus rows are per (holder, action)
    # since one control may carry several extra actions). On the Desktop tab
    # rows must stay keyboard/mouse  Xbox outputs are reported instead.
    seen = set()
    adv_out = []
    stick_dir = re.compile(r"stick_(up|down|left|right)$")
    for r in adv:
        if kb_only and r["action"] in _XINPUT_IDS:
            kb_xi.append("%s %s" % (r["control"], r["press"]))
            continue
        # Rows the Advanced Presses GUI (and engine) can't express: the
        # Steam/QAM buttons own the chord system, and stick DIRECTIONS have
        # no button bit to time or shift.
        cells = [r["control"], r.get("target") or ""]
        if any(c in ("steam", "qam") for c in cells):
            skipped.append("%s %s (the Steam/Quick-Menu buttons drive the "
                           "chord system)" % (r["control"], r["press"]))
            continue
        if any(stick_dir.search(c) for c in cells):
            skipped.append("Stick-direction %s binding (needs a button)"
                           % r["press"])
            continue
        key = (r["control"], r["press"], r.get("target"),
               r["action"] if r["press"] == "plus" else None)
        if key not in seen:
            seen.add(key)
            adv_out.append(r)

    if kb_xi:
        skipped.append("Controller outputs (this tab is keyboard & mouse): "
                       + ", ".join(kb_xi))

    return {
        "ok": True,
        "title": str(cm.get("title", "") or ""),
        "description": str(cm.get("description", "") or ""),
        "creator": str(cm.get("creator", "") or ""),
        "controller_type": ctype,
        "binds": binds,
        "adv": adv_out,
        "gyro": gyro,
        "vmenus": vmenus,
        "skipped": skipped,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# SteamInputDB.com API client
# ---------------------------------------------------------------------------

API_BASE = "https://api.steaminputdb.com"

# SteamInputDB is a one-person project (Peter Repukat / Alia5) and this is an
# UNOFFICIAL consumer of the API its website runs on  there is no published
# third-party contract and no key to identify us. So the User-Agent carries the
# app, its version and its repo: he can allowlist us, throttle us specifically,
# or come find us if we misbehave, rather than seeing anonymous load he has to
# treat as abuse. Bump CLIENT_VERSION on release.
CLIENT_VERSION = "1.0"
PROJECT_URL = "https://github.com/PietPetGit/SteamlessInput"
_UA = "SteamlessInput/%s (+%s) community-config-import" % (
    CLIENT_VERSION, PROJECT_URL)

_TIMEOUT = 12
_MAX_VDF = 4 * 1024 * 1024


class ApiUnavailable(Exception):
    """The service couldn't be reached: a 5xx from its gateway, a rate
    limit, or a dead connection. Carries a message meant for the user, and
    is deliberately DISTINCT from 'the request worked, there's no such
    thing'  an outage must never be reported as 'config not found'."""


# nginx fronts the API, so a restarting/overloaded backend answers 502-504
# for a stretch; 429 is its rate limiter. All are worth one quiet retry
# before the user sees anything  a single blip shouldn't fail a search.
_RETRY_CODES = frozenset((429, 500, 502, 503, 504))
_BACKOFF = (0.7, 1.8)           # len() == number of retries after the first try
# Ceiling on a server-supplied Retry-After. We always honour the header when it
# is shorter than this; beyond it we give up and report an outage rather than
# parking a worker thread for minutes on end.
_RETRY_AFTER_CAP = 30.0

# --- politeness controls -----------------------------------------------------
# Everything below is keyed by SERVICE, because this module talks to two
# unrelated hosts: SteamInputDB's API (one maintainer, unofficial consumer,
# treat gently) and Steam's own config CDN (Valve, fine). They fail
# independently  a CDN outage must not stop us reaching the API, and a
# throttle sized for one has no business slowing the other.
SVC_API = "SteamInputDB"
SVC_CDN = "Steam's config CDN"

# Nothing on the API is latency-critical: the user has pressed Return and is
# watching a spinner. So its requests are serialised with a floor between them.
# Without it the picker's typo fallback (up to 3 extra searches for one miss)
# and any burst of appinfo lookups would all leave at once. The CDN gets a much
# smaller floor  it is a static file host built for this.
_MIN_INTERVAL = {SVC_API: 0.34, SVC_CDN: 0.05}
_DEFAULT_INTERVAL = 0.34
_pace_lock = threading.Lock()
_last_request_at = {}           # service -> monotonic timestamp

# If a service is genuinely down, every client retrying in lockstep is the worst
# thing we could do to it. After this many consecutive transport failures we
# stop trying for a cooldown and fail fast locally instead.
_BREAKER_TRIP = 4
_BREAKER_COOLDOWN = 60.0
_breaker_lock = threading.Lock()
_breaker_fails = {}             # service -> consecutive failures
_breaker_open_until = {}        # service -> monotonic deadline

# Short-lived response cache. The same appid/file id gets looked up repeatedly
# while browsing, and the typo fallback re-issues overlapping prefix searches;
# none of that data changes minute to minute.
_CACHE_TTL = 300.0
_CACHE_MAX = 256
_cache_lock = threading.Lock()
_cache = {}                     # key -> (expires_at, value)


def _cache_get(key):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if hit[0] <= now:
            _cache.pop(key, None)
            return None
        return hit[1]


def _cache_put(key, value):
    now = time.monotonic()
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Cheap eviction: drop everything already expired, and if that
            # freed nothing, the oldest-expiring entry. No LRU bookkeeping for
            # a cache this small.
            dead = [k for k, v in _cache.items() if v[0] <= now]
            for k in dead:
                _cache.pop(k, None)
            if len(_cache) >= _CACHE_MAX:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        _cache[key] = (now + _CACHE_TTL, value)


def clear_cache():
    """Drop cached API responses (used by the picker's manual refresh)."""
    with _cache_lock:
        _cache.clear()


def _breaker_check(service):
    """Raise ApiUnavailable immediately while `service`'s breaker is open."""
    with _breaker_lock:
        remain = _breaker_open_until.get(service, 0.0) - time.monotonic()
    if remain > 0:
        raise ApiUnavailable(
            "%s looked down a moment ago, so we've paused requests for %d more "
            "seconds to avoid piling on. Try again shortly."
            % (service, int(remain + 0.5)))


def _breaker_record(service, ok):
    with _breaker_lock:
        if ok:
            _breaker_fails.pop(service, None)
            _breaker_open_until.pop(service, None)
            return
        fails = _breaker_fails.get(service, 0) + 1
        if fails >= _BREAKER_TRIP:
            _breaker_open_until[service] = time.monotonic() + _BREAKER_COOLDOWN
            _breaker_fails.pop(service, None)
        else:
            _breaker_fails[service] = fails


def _pace(service):
    """Block until this service's minimum inter-request interval has elapsed."""
    gap = _MIN_INTERVAL.get(service, _DEFAULT_INTERVAL)
    with _pace_lock:
        wait = gap - (time.monotonic() - _last_request_at.get(service, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_request_at[service] = time.monotonic()


def _retry_after(exc, fallback):
    """Seconds to wait, preferring the server's own Retry-After header.

    It is the service telling us exactly how long to back off; ignoring it in
    favour of our own guess is how a rate limit turns into a ban.
    """
    hdrs = getattr(exc, "headers", None)
    raw = hdrs.get("Retry-After") if hdrs else None
    if raw:
        try:
            secs = float(str(raw).strip())
        except ValueError:
            try:                # HTTP-date form
                from email.utils import parsedate_to_datetime
                import datetime as _dt
                when = parsedate_to_datetime(str(raw).strip())
                now = _dt.datetime.now(_dt.timezone.utc)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_dt.timezone.utc)
                secs = (when - now).total_seconds()
            except Exception:
                return fallback
        if secs >= 0:
            return min(secs, _RETRY_AFTER_CAP)
    return fallback


def _describe(exc, what="SteamInputDB"):
    """Human-readable one-liner for a failed request."""
    code = getattr(exc, "code", None)
    if code in (502, 503, 504):
        return ("%s is down right now (server error %d)  nothing wrong on "
                "your end. Try again in a few minutes." % (what, code))
    if code == 500:
        return ("%s hit a server error (500). Try again in a few minutes."
                % what)
    if code == 429:
        return "%s is rate-limiting us  wait a moment and try again." % what
    if code == 404:
        return "%s has no such entry (404)." % what
    if code:
        return "%s returned HTTP %s." % (what, code)
    reason = getattr(exc, "reason", None) or exc
    return ("Can't reach %s  check your internet connection. (%s)"
            % (what, reason))


def friendly_error(exc, what="SteamInputDB"):
    """Turn any exception from this module's network calls into a message
    worth showing in the UI. ApiUnavailable already carries one."""
    if isinstance(exc, ApiUnavailable):
        return str(exc)
    if isinstance(exc, ValueError):
        # A rejected URL (see _check_cdn_url), not a transport failure  say so
        # rather than blaming the user's connection.
        return "Blocked for safety: %s." % exc
    return _describe(exc, what)


def _urlopen(req, what=SVC_API, opener=None):
    """urlopen + short backoff retry on transient server/network failures.

    Anything still failing after the retries is raised as ApiUnavailable
    with a message the UI can show verbatim; non-transient HTTP errors
    (404, 4xx) propagate straight away so callers can tell them apart.

    Every call is paced (see `_pace`) and guarded by the circuit breaker, so a
    service that is actually down stops receiving traffic from us instead of
    being retried on a loop by every client that has the app open.
    """
    _breaker_check(what)
    last = None
    for attempt in range(len(_BACKOFF) + 1):
        _pace(what)
        try:
            _open = opener.open if opener is not None else \
                urllib.request.urlopen
            resp = _open(req, timeout=_TIMEOUT)
            _breaker_record(what, True)
            return resp
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in _RETRY_CODES:
                # A real answer, just not a happy one  the service is up.
                _breaker_record(what, True)
                raise
            # 429 means we are the problem: obey the server's own timing.
            delay = _retry_after(
                e, _BACKOFF[attempt] if attempt < len(_BACKOFF) else 0.0)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            delay = _BACKOFF[attempt] if attempt < len(_BACKOFF) else 0.0
        if attempt < len(_BACKOFF):
            if delay > 0:
                time.sleep(delay)
        else:
            break
    _breaker_record(what, False)
    raise ApiUnavailable(_describe(last, what)) from last


def _post_json(path, body, cache_key=None):
    """POST JSON, optionally serving/filling the short-lived response cache."""
    if cache_key is not None:
        hit = _cache_get(cache_key)
        if hit is not None:
            return hit
    req = urllib.request.Request(
        API_BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": _UA,
                 "Accept": "application/json",
                 "Accept-Encoding": "identity"})
    with _urlopen(req) as resp:
        out = json.loads(resp.read().decode("utf-8", "replace"))
    if cache_key is not None:
        _cache_put(cache_key, out)
    return out


def search(term, limit_games=4, limit_configs=12):
    """Combined search: (games, configs). Games are {app_id, name, ...};
    configs are ConfigItem dicts (title, controller_type_nice, file_url,
    votes, subscriptions, app_id_string, ...)."""
    out = _post_json("/v1/search/", {
        "search_term": term,
        "limit_games": limit_games,
        "limit_configs": limit_configs,
    }, cache_key=("search", term, limit_games, limit_configs))
    return (out.get("games") or []), (out.get("configs") or [])


def search_configs(app_id=None, query="", limit=25, page=None, creator=None):
    """Config search: most-downloaded first, ties broken by highest upvotes.
    `app_id` narrows to one game (Steam appid as a string; non-Steam games
    use their name); `creator` narrows to one uploader (SteamID64 string)."""
    body = {"query_text": query or "", "limit": limit,
            "include": {"votes": True, "tags": True},
            "rank": {"by": "subscriptions"}}
    flt = {}
    if app_id:
        flt["app_id"] = str(app_id)
    if creator:
        flt["creator"] = str(creator)
    if flt:
        body["filter"] = flt
    if page:
        body["page"] = int(page)
    out = _post_json("/v1/search/configs", body,
                     cache_key=("configs", str(app_id or ""), query or "",
                                limit, page or 0, str(creator or "")))
    items = out.get("items") or []
    # The API sorts by ONE key server-side; re-sort (stable) so equal
    # download counts fall back to upvotes instead of arrival order.
    items.sort(key=lambda c: (-(c.get("subscriptions") or 0),
                              -((c.get("votes") or {}).get("up") or 0)))
    return items


def config_details(file_id):
    """One config's full details (ConfigItem shape  title, controller_type,
    file_url, votes, ...) straight from its Steam file id, or None if the id
    isn't a controller config. Powers pasted links / bare share codes."""
    key = ("filedetails", int(file_id))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    url = "%s/v1/steam/filedetails?file_id=%s&playtime_stats=30" % (
        API_BASE, int(file_id))
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json",
                                               "Accept-Encoding": "identity"})
    try:
        with _urlopen(req) as resp:
            out = json.loads(resp.read().decode("utf-8", "replace"))
        _cache_put(key, out)
        return out
    except ApiUnavailable:
        raise           # an outage is NOT "no such config"  let it show
    except Exception:
        return None


def app_name(app_id):
    """A Steam appid's display name, or None."""
    key = ("appinfo", int(app_id))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    url = "%s/v1/steam/appinfo?app_id=%s" % (API_BASE, int(app_id))
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json",
                                               "Accept-Encoding": "identity"})
    # Display name only, with a caller-side fallback  swallow everything
    # (including an outage): the config fetch right after reports the error.
    try:
        with _urlopen(req) as resp:
            name = json.loads(
                resp.read().decode("utf-8", "replace")).get("name")
        if name:
            _cache_put(key, name)
        return name
    except Exception:
        return None


def resolve_vanity(name):
    """steamcommunity.com/id/<name> vanity -> SteamID64, or None. Uses the
    profile's keyless ?xml=1 view (no Web API key needed)."""
    try:
        url = "https://steamcommunity.com/id/%s?xml=1" % \
            urllib.parse.quote(str(name).strip("/"))
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept-Encoding":
                                                   "identity"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            text = resp.read(65536).decode("utf-8", "replace")
        m = re.search(r"<steamID64>(\d{17})</steamID64>", text)
        return m.group(1) if m else None
    except Exception:
        return None


# --- smart search-bar query parsing ------------------------------------------
# The Community Configs search box accepts more than game titles: pasted
# SteamInputDB / Steam links, bare share codes, creator profiles. parse_query
# classifies the input; the picker dispatches on the type.

_Q_PATTERNS = (
    # SteamInputDB config page: steaminputdb.com/config/<fileid>
    ("file", re.compile(r"steaminputdb\.com/config/(\d{6,})", re.I)),
    # Steam workshop/share pages: sharedfiles or workshop filedetails ?id=
    ("file", re.compile(
        r"steamcommunity\.com/(?:sharedfiles|workshop)/filedetails/"
        r"[^\s]*?[?&]id=(\d{6,})", re.I)),
    # Steam client share link: steam://controllerconfig/<app>/<fileid>
    ("file", re.compile(r"steam://controllerconfig/[^/\s]+/(\d{6,})", re.I)),
    # Creator profile by id64: steamcommunity.com/profiles/<id64>
    ("creator", re.compile(r"steamcommunity\.com/profiles/(\d{17})", re.I)),
    # Creator profile by vanity name: steamcommunity.com/id/<name>
    ("vanity", re.compile(r"steamcommunity\.com/id/([A-Za-z0-9_\-]+)", re.I)),
    # A game page: store.steampowered.com/app/<id> or steaminputdb.com/app/<id>
    ("app", re.compile(
        r"(?:store\.steampowered\.com|steaminputdb\.com)/(?:app|game)/(\d+)",
        re.I)),
)

# Whole-input shapes (no URL): SteamID64, long file id, short appid,
# creator:<name> prefix.
_Q_ID64_RE = re.compile(r"^7656119\d{10}$")
_Q_DIGITS_RE = re.compile(r"^\d+$")
_Q_CREATOR_RE = re.compile(r"^(?:creator|by|from)\s*[:=]\s*(.+)$", re.I)


def clean_text(term):
    """Free-text cleanup so decorated / mistyped queries still hit: strip
    emoji + decoration symbols and control/format characters, unify fancy
    quotes/dashes, collapse whitespace. (Actual TYPO tolerance comes from the
    Steam store search behind /v1/search/  it spell-corrects game names 
    so our job is just not feeding it junk characters.)"""
    out = []
    # NFKC first: folds decorated Unicode ("𝕊𝔲𝔭𝔢𝔯𝔦𝔬𝔯", fullwidth, ligatures)
    # to plain ASCII letters, so fancy-titled searches match normal text.
    for ch in unicodedata.normalize("NFKC", str(term)):
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cf", "Co", "Cs", "Cn") \
                or 0xFE00 <= ord(ch) <= 0xFE0F:     # variation selectors
            out.append(" ")             # emoji / decorations / zero-widths
        elif ch in "“”„‟":
            out.append('"')
        elif ch in "‘’‚‛":
            out.append("'")
        elif ch in "–−":
            out.append("-")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def parse_query(term):
    """Classify one search-box input:
      ("file", file_id)      pasted config link or bare share code
      ("creator", id64)      profile link / bare SteamID64 / creator:<id64>
      ("vanity", name)       /id/<name> profile link or creator:<name>
                             (resolve with resolve_vanity, then creator)
      ("app", appid)         store/steaminputdb game link or bare short number
      ("text", cleaned)      everything else  normal text search
    URLs win over plain shapes; the first recognized pattern applies."""
    raw = str(term or "").strip().strip("<>\"' ")
    for qtype, rx in _Q_PATTERNS:
        m = rx.search(raw)
        if m:
            return (qtype, m.group(1))
    m = _Q_CREATOR_RE.match(raw)
    if m:
        who = m.group(1).strip().strip("/")
        if _Q_ID64_RE.match(who):
            return ("creator", who)
        mm = re.search(r"steamcommunity\.com/profiles/(\d{17})", who)
        if mm:
            return ("creator", mm.group(1))
        mm = re.search(r"steamcommunity\.com/id/([A-Za-z0-9_\-]+)", who)
        return ("vanity", mm.group(1) if mm else who)
    bare = raw.replace(" ", "")
    if _Q_ID64_RE.match(bare):
        return ("creator", bare)
    if _Q_DIGITS_RE.match(bare):
        # Steam appids are ≤7 digits today; config file ids are 9+ digits
        # (oldest known ~5.7e8). 8 digits is ambiguous  treat as file id
        # (appids won't reach 8 digits for years).
        return ("app", bare) if len(bare) <= 7 else ("file", bare)
    return ("text", clean_text(raw))


# Hosts a config file may legitimately come from. `file_url` arrives inside an
# API response, so it is attacker-controlled the moment SteamInputDB is
# compromised, spoofed, or simply wrong  and urllib will cheerfully open
# file:// (reading a local file off the user's disk) or plain http:// (a
# downgrade anyone on the network can tamper with). Neither is ever correct
# here: Steam serves UGC over HTTPS from its own CDNs. Matched on the
# registrable suffix so per-region/shard names (steamusercontent-a, cdn-,
# ...) keep working. If a legitimate host is ever missed the user sees exactly
# which one was refused, so it is a one-line fix rather than a silent failure.
_CDN_HOST_SUFFIXES = (
    ".steamusercontent.com",
    ".steamstatic.com",
    ".steampowered.com",
    ".steamcontent.com",
    ".akamaihd.net",
)


def _check_cdn_url(url):
    """Return `url` if it is an HTTPS Steam-CDN address; else raise ValueError."""
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        raise ValueError("unreadable download URL")
    if parts.scheme.lower() != "https":
        raise ValueError(
            "refusing a non-HTTPS config download (%s)"
            % (parts.scheme or "no scheme"))
    host = (parts.hostname or "").lower()
    if not any(host == s.lstrip(".") or host.endswith(s)
               for s in _CDN_HOST_SUFFIXES):
        raise ValueError("refusing a config download from %s  not a Steam CDN"
                         % (host or "an unnamed host"))
    return url


class _CdnRedirectGuard(urllib.request.HTTPRedirectHandler):
    """Re-check the target of every redirect.

    Validating only the initial URL would be theatre: a redirect could hop the
    download to http:// or to any host it liked, and urllib follows redirects
    by default.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_cdn_url(newurl)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


_cdn_opener = urllib.request.build_opener(_CdnRedirectGuard)


def download_vdf(file_url):
    """Fetch a config's VDF text from the Steam CDN (a different host from
    the API  it can be up while SteamInputDB is down, and vice versa).

    The URL is validated before AND across redirects: it must be HTTPS to a
    Steam CDN. The read is capped at `_MAX_VDF`, so an oversized or endless
    response can't exhaust memory.
    """
    _check_cdn_url(file_url)
    req = urllib.request.Request(
        file_url, headers={"User-Agent": _UA,
                           "Accept-Encoding": "identity"})
    with _urlopen(req, what=SVC_CDN, opener=_cdn_opener) as resp:
        data = resp.read(_MAX_VDF)
    return data.decode("utf-8", "replace")
