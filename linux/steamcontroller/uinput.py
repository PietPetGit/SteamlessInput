"""Key/mouse injection backend used by adusk.

Two implementations behind the same Keyboard/Mouse API:

1. Linux: open /dev/uinput via python-evdev and inject events at the
   kernel level. This is the only path that works on a Wayland session
    pynput's XTest path silently drops keys destined for native
   Wayland apps. Requires /dev/uinput to be writable by the user
   (CachyOS / most modern distros grant this via a uaccess ACL).

2. Fallback (Windows, or Linux without evdev / uinput access): pynput,
   which on Windows uses SendInput and on Linux uses XTest.

The uinput backend declares THREE virtual devices: a keyboard, a relative
mouse, and an absolute pointer. The third exists because Wayland has no
cursor-warp call at all  pynput's `mouse.position = (x, y)` is XTest, so it
silently does nothing for native Wayland clients, exactly like key injection.
A uinput device carrying ABS_X/ABS_Y plus mouse buttons is how VM guest
tablets (QEMU's usb-tablet and friends) warp a Wayland cursor, and it is the
same trick the extest XTEST shim uses to get Steam's own desktop mode working
under Wayland. See _init_uinput and Mouse.set_position.

Deliberately NOT mirrored from the Windows tree: the UIPI injection gate
(windows/steamcontroller/uinput.py's _suppressed, driven by tray._UipiGuard).
Windows discards injected input aimed at a window whose process outranks ours
in integrity  Task Manager, installers, the UAC desktop  which freezes the
trackpad cursor dead, so the Windows build falls back to the controller's
firmware mouse there and gates its own injection meanwhile. Linux has no
equivalent: path 1 injects through /dev/uinput at the KERNEL level, so its
events are indistinguishable from a physical device and no window can be
privileged against them; path 2's XTest reaches root-owned X clients just the
same. set_suppressed/suppressed exist purely to keep the module API identical
across the two trees. See also the lock-screen X guard (Windows-only by the
same reasoning).
"""

import sys
import time


# Adusk passes keycodes around as strings (e.g. "KEY_A"); this proxy lets
# code write `sui.Keys.KEY_A` or `sui.Keys["KEY_A"]` interchangeably.
class _KeysProxy:
    def __getitem__(self, name):
        return name

    def __getattr__(self, name):
        return name


Keys = _KeysProxy()


# API parity with the Windows tree's UIPI gate (see the module docstring).
# Nothing can block kernel-level uinput events, so these are inert here  they
# exist so shared callers don't have to branch on platform.
def set_suppressed(on):
    """No-op on Linux: there is no privilege tier that can refuse our input."""


def suppressed():
    return False


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_BACKEND = None  # "uinput" or "pynput"
_uinput_kb = None
_uinput_mouse = None
_uinput_abs = None
_uinput_keymap = None
_uinput_init_error = None

try:
    if sys.platform.startswith("linux"):
        import evdev
        from evdev import UInput, ecodes as _e
        _has_evdev = True
    else:
        _has_evdev = False
except Exception as _exc:
    _has_evdev = False
    _uinput_init_error = _exc


def _build_uinput_keymap():
    """Map adusk's KEY_* names to Linux input event codes. Most names
    already match the kernel input.h constants verbatim (they were
    copied from there), so evdev.ecodes resolves them directly. A few
    adusk-specific aliases are listed explicitly."""
    m = {}
    # Pass-through for any KEY_* / BTN_* name evdev recognizes.
    for name in dir(_e):
        if name.startswith("KEY_") or name.startswith("BTN_"):
            m[name] = getattr(_e, name)
    # Adusk aliases that aren't 1:1 kernel names.
    aliases = {
        "KEY_LEFTWIN": "KEY_LEFTMETA",
        "KEY_QUESTION": "KEY_SLASH",  # '?' is shift+'/'; same physical key
    }
    for alias, real in aliases.items():
        if real in m:
            m[alias] = m[real]
    return m


def _init_uinput():
    """Open /dev/uinput once at first use. Declares a virtual keyboard
    and virtual mouse. Returns True on success."""
    global _uinput_kb, _uinput_mouse, _uinput_abs
    global _uinput_keymap, _BACKEND, _uinput_init_error
    if _BACKEND == "uinput":
        return True
    if not _has_evdev:
        return False
    try:
        keymap = _build_uinput_keymap()

        # Declare every keyboard key code we might ever emit. Filter to
        # the valid 1..KEY_MAX range so we don't try to register sentinel
        # symbols like KEY_CNT (=KEY_MAX+1), which trip the uinput ioctl
        # with EINVAL.
        kb_codes = sorted({
            v for k, v in keymap.items()
            if k.startswith("KEY_") and 0 < v <= _e.KEY_MAX
        })
        kb_caps = {_e.EV_KEY: kb_codes}
        _uinput_kb = UInput(
            kb_caps, name="SteamlessInput-virtual-kb", version=1)

        # Mouse: relative X/Y plus the three standard buttons (so the
        # kernel actually treats this device as a pointer).
        mouse_caps = {
            _e.EV_KEY: [_e.BTN_LEFT, _e.BTN_RIGHT, _e.BTN_MIDDLE],
            _e.EV_REL: [_e.REL_X, _e.REL_Y, _e.REL_WHEEL, _e.REL_HWHEEL],
        }
        _uinput_mouse = UInput(
            mouse_caps, name="SteamlessInput-virtual-mouse", version=1)

        # Absolute pointer: a THIRD device, kept separate from the relative
        # mouse above on purpose. udev's input_id builtin classifies by
        # capability bits, and a device advertising BOTH REL_X/Y and ABS_X/Y
        # is ambiguous enough that compositors have been known to route only
        # one of the two; two single-purpose devices are unambiguous. The
        # buttons are declared but never emitted (button() always writes to
        # the relative device)  they're what make input_id tag this
        # ID_INPUT_MOUSE rather than a joystick, which is what gets libinput
        # to treat it as an absolute POINTER. Deliberately absent for the
        # same reason: BTN_TOUCH / BTN_TOOL_* and INPUT_PROP_DIRECT, any of
        # which would reclassify it as a touchscreen or a tablet.
        try:
            # AbsInfo is a namedtuple (value, min, max, fuzz, flat,
            # resolution); a bare tuple in that order is accepted by every
            # python-evdev that has it. resolution stays 0  a nonzero one
            # means "units per mm" and makes libinput size the device
            # physically instead of mapping it onto the screen.
            axis = (0, 0, _ABS_MAX, 0, 0, 0)
            abs_caps = {
                _e.EV_KEY: [_e.BTN_LEFT, _e.BTN_RIGHT, _e.BTN_MIDDLE],
                _e.EV_ABS: [(_e.ABS_X, axis), (_e.ABS_Y, axis)],
            }
            _uinput_abs = UInput(
                abs_caps, name="SteamlessInput-virtual-abs-pointer",
                version=1)
        except Exception as exc:
            # Non-fatal: keys and relative motion are the important half.
            # Only cursor warping (set_position) is lost.
            _uinput_abs = None
            print(f"uinput: absolute pointer unavailable: {exc}")

        _uinput_keymap = keymap
        _BACKEND = "uinput"
        # Compositors take a moment to notice a freshly created uinput
        # device. Without a brief settle, the very first key event after
        # opening the OSK can be dropped.
        time.sleep(0.1)
        return True
    except Exception as exc:
        _uinput_init_error = exc
        _uinput_kb = None
        _uinput_mouse = None
        _uinput_abs = None
        return False


# ---------------------------------------------------------------------------
# Screen geometry + cursor tracking (absolute pointer support)
# ---------------------------------------------------------------------------

# The absolute device's axes span a fixed normalized range rather than a pixel
# one, which is the same convention Windows uses for MOUSEEVENTF_ABSOLUTE. It
# costs nothing  libinput scales whatever range a device declares onto the
# output  and it means a resolution change or a hotplugged monitor only has to
# move the pixel->normalized conversion below, never tear down and recreate the
# uinput device (which would cost a fresh compositor settle each time).
_ABS_MAX = 65535

_abs_bounds = None       # (x, y, w, h) of the virtual desktop, or None
_abs_bounds_at = 0.0     # monotonic stamp of the last successful probe
_ABS_BOUNDS_TTL = 5.0    # re-probe this often, so a mode switch is picked up
_abs_warn_done = False   # "can't warp" is logged once, not once per frame


def _env_bounds():
    """Manual override via STEAMLESSINPUT_POINTER_BOUNDS, as "1920x1080" or
    "x,y,w,h". The escape hatch for a multi-head layout the probes below get
    wrong  see _desktop_bounds for why that's possible."""
    import os
    import re
    raw = (os.environ.get("STEAMLESSINPUT_POINTER_BOUNDS") or "").strip()
    if not raw:
        return None
    m = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", raw)
    if not m:
        m = re.fullmatch(r"(-?\d+),(-?\d+),(\d+),(\d+)", raw.replace(" ", ""))
    if m:
        g = [int(v) for v in m.groups()]
        b = (0, 0, g[0], g[1]) if len(g) == 2 else tuple(g)
        if b[2] > 0 and b[3] > 0:
            return b
    print(f"uinput: ignoring malformed STEAMLESSINPUT_POINTER_BOUNDS {raw!r}")
    return None


def _sdl_desktop_bounds():
    """Union of every SDL display's bounds, or None.

    Only answers when SDL's video subsystem is already up  the OSK inits it
    and never quits it, so in practice it is. We must not init it ourselves:
    it costs ~400 ms (it builds the XWayland connection and video driver) and
    would be paid from inside a scrub gesture, on whichever thread happened to
    ask. SDL_GetPrimaryDisplay returning 0 is the cheap, side-effect-free way
    to ask "is video up?" without binding SDL_WasInit."""
    try:
        import ctypes
        import sdl3w as S
    except Exception:
        return None
    try:
        if not S.SDL_GetPrimaryDisplay():
            return None            # video subsystem isn't inited
        rects = []
        count = ctypes.c_int(0)
        ids = S.SDL_GetDisplays(ctypes.byref(count))
        if ids:
            try:
                for i in range(count.value):
                    r = S.SDL_Rect()
                    if S.SDL_GetDisplayBounds(ids[i], ctypes.byref(r)):
                        rects.append((r.x, r.y, r.w, r.h))
            finally:
                S.SDL_free(ids)
        if not rects:
            return None
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1 - x0, y1 - y0)
    except Exception as exc:
        print(f"uinput: SDL display probe failed: {exc}")
        return None


def _drm_desktop_bounds():
    """Largest connected DRM connector's current mode, as (0, 0, w, h).

    The no-display-server fallback: /sys/class/drm/<card>-<conn>/modes lists a
    connector's modes with the current one first. It knows resolutions but not
    desktop LAYOUT, so a multi-head setup collapses to its biggest panel 
    good enough to keep warping roughly sane, and overridable via
    STEAMLESSINPUT_POINTER_BOUNDS when it isn't."""
    import glob
    import os
    import re
    best = None
    for status_path in glob.glob("/sys/class/drm/*/status"):
        try:
            with open(status_path) as fh:
                if fh.read().strip() != "connected":
                    continue
            modes = os.path.join(os.path.dirname(status_path), "modes")
            with open(modes) as fh:
                first = fh.readline().strip()
        except Exception:
            continue
        m = re.match(r"(\d+)x(\d+)", first)     # "1920x1080", "1920x1080i"
        if not m:
            continue
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 0 or h <= 0:
            continue
        if best is None or w * h > best[0] * best[1]:
            best = (w, h)
    return (0, 0, best[0], best[1]) if best else None


def _desktop_bounds(force=False):
    """The virtual desktop as (x, y, w, h), or None if nothing could read it.

    Cached for _ABS_BOUNDS_TTL so a scrub gesture isn't re-probing SDL every
    frame, but short enough that a resolution change or a hotplug is picked up
    without a restart. A FAILED probe deliberately doesn't refresh the stamp,
    so we keep retrying rather than caching the failure  and keeps the last
    known-good bounds meanwhile, since stale geometry beats no warping.

    Caveat worth knowing: this is the union of every display, which assumes
    the compositor maps an absolute pointer across the whole desktop. That's
    the default on KWin and wlroots; a compositor that instead binds the
    device to a single output will land the cursor on that output's share of
    the range. STEAMLESSINPUT_POINTER_BOUNDS pins it when that happens."""
    global _abs_bounds, _abs_bounds_at
    now = time.monotonic()
    if (not force and _abs_bounds is not None
            and (now - _abs_bounds_at) < _ABS_BOUNDS_TTL):
        return _abs_bounds
    found = _env_bounds() or _sdl_desktop_bounds() or _drm_desktop_bounds()
    if found is not None:
        if found != _abs_bounds:
            print("uinput: pointer bounds %dx%d at (%d,%d)"
                  % (found[2], found[3], found[0], found[1]))
        _abs_bounds = found
        _abs_bounds_at = now
    return _abs_bounds


# Where we believe the cursor is, as floats so that sub-pixel relative deltas
# accumulate instead of being truncated away frame by frame. None until seeded.
#
# Dead reckoning, because there is no alternative: Wayland exposes no global
# cursor query by design, and the uinput device is write-only. It holds up
# because SteamlessInput generates essentially all the pointer motion on these
# paths (the trackpad mouse) and set_position re-establishes ground truth on
# every warp. It drifts only when a REAL mouse is moved alongside the
# controller, which is the same limitation extest lives with.
_cursor_xy = None


def _seed_cursor():
    """Best-effort starting point for the tracked cursor.

    pynput can still be importable while we're on the uinput backend (we chose
    uinput because it's better, not because pynput was missing), and under
    X11/XWayland its read is a real XQueryPointer  so try it before falling
    back to the middle of the desktop."""
    try:
        from pynput.mouse import Controller as _MC
        px, py = _MC().position
        return float(px), float(py)
    except Exception:
        pass
    b = _desktop_bounds()
    if b is not None:
        return b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
    return 0.0, 0.0


def _cursor_track_move(dx, dy):
    """Fold a relative motion into the tracked position, clamped to the
    desktop so a long drag against an edge can't wind the tracked value off
    into the distance and desync it from the real (edge-stopped) cursor."""
    global _cursor_xy
    if _cursor_xy is None:
        _cursor_xy = list(_seed_cursor())
    x = _cursor_xy[0] + float(dx)
    y = _cursor_xy[1] + float(dy)
    b = _desktop_bounds()
    if b is not None:
        x = min(max(x, b[0]), b[0] + b[2] - 1)
        y = min(max(y, b[1]), b[1] + b[3] - 1)
    _cursor_xy = [x, y]


# ---------------------------------------------------------------------------
# pynput fallback
# ---------------------------------------------------------------------------

_pynput_kb = None
_pynput_mouse = None
_pynput_keymap = None


def _init_pynput():
    global _pynput_kb, _pynput_mouse, _pynput_keymap, _BACKEND
    if _BACKEND == "pynput":
        return True
    try:
        from pynput.keyboard import (
            Controller as _Controller,
            Key as _Key,
            KeyCode as _KeyCode,
        )
        from pynput.mouse import Controller as _MouseController
    except Exception as exc:
        print(f"uinput: pynput unavailable: {exc}")
        return False

    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m["KEY_" + c.upper()] = _KeyCode.from_char(c)
    for d in "0123456789":
        m["KEY_" + d] = _KeyCode.from_char(d)
    m.update({
        "KEY_SPACE":      _Key.space,
        "KEY_ENTER":      _Key.enter,
        "KEY_BACKSPACE":  _Key.backspace,
        "KEY_TAB":        _Key.tab,
        "KEY_ESC":        _Key.esc,
        "KEY_CAPSLOCK":   _Key.caps_lock,
        "KEY_LEFTSHIFT":  _Key.shift,
        "KEY_RIGHTSHIFT": _Key.shift_r,
        "KEY_LEFTCTRL":   _Key.ctrl,
        "KEY_RIGHTCTRL":  _Key.ctrl_r,
        "KEY_LEFTALT":    _Key.alt,
        "KEY_RIGHTALT":   _Key.alt_r,
        "KEY_LEFTMETA":   _Key.cmd,
        "KEY_LEFTWIN":    _Key.cmd,
        "KEY_MINUS":      _KeyCode.from_char("-"),
        "KEY_EQUAL":      _KeyCode.from_char("="),
        "KEY_DOT":        _KeyCode.from_char("."),
        "KEY_COMMA":      _KeyCode.from_char(","),
        "KEY_SLASH":      _KeyCode.from_char("/"),
        "KEY_BACKSLASH":  _KeyCode.from_char("\\"),
        "KEY_SEMICOLON":  _KeyCode.from_char(";"),
        "KEY_APOSTROPHE": _KeyCode.from_char("'"),
        "KEY_GRAVE":      _KeyCode.from_char("`"),
        "KEY_LEFTBRACE":  _KeyCode.from_char("["),
        "KEY_RIGHTBRACE": _KeyCode.from_char("]"),
        "KEY_QUESTION":   _KeyCode.from_char("?"),
        "KEY_INSERT":     _Key.insert,
        "KEY_LEFT":       _Key.left,
        "KEY_RIGHT":      _Key.right,
        "KEY_UP":         _Key.up,
        "KEY_DOWN":       _Key.down,
        "KEY_PAGEUP":     _Key.page_up,
        "KEY_PAGEDOWN":   _Key.page_down,
        "KEY_HOME":       _Key.home,
        "KEY_END":        _Key.end,
        "KEY_VOLUMEUP":    _Key.media_volume_up,
        "KEY_VOLUMEDOWN":  _Key.media_volume_down,
        "KEY_MUTE":        _Key.media_volume_mute,
        "KEY_PREVIOUSSONG": _Key.media_previous,
        "KEY_NEXTSONG":    _Key.media_next,
        "KEY_PLAYPAUSE":   _Key.media_play_pause,
        # Numpad  X11 keysyms (this fallback is XTest-backed): XK_KP_Add,
        # Subtract, Multiply, Divide, Decimal, Enter. The uinput backend
        # resolves KEY_KP* kernel names natively and never reads this map.
        "KEY_KPPLUS":      _KeyCode.from_vk(0xffab),
        "KEY_KPMINUS":     _KeyCode.from_vk(0xffad),
        "KEY_KPASTERISK":  _KeyCode.from_vk(0xffaa),
        "KEY_KPSLASH":     _KeyCode.from_vk(0xffaf),
        "KEY_KPDOT":       _KeyCode.from_vk(0xffae),
        "KEY_KPENTER":     _KeyCode.from_vk(0xff8d),
    })
    for _n in range(10):        # XK_KP_0 .. XK_KP_9
        m["KEY_KP%d" % _n] = _KeyCode.from_vk(0xffb0 + _n)
    # Function row (the 75% layout's top row). pynput exposes them as
    # Key.f1..Key.f12; the uinput backend resolves the kernel KEY_F* names
    # itself and never reads this map.
    for _n in range(1, 13):
        m["KEY_F%d" % _n] = getattr(_Key, "f%d" % _n)
    # NOTE: KEY_SELECT is deliberately ABSENT (the kernel HAS a KEY_SELECT, so
    # only the pynput map can leave it out). It is the sentinel keycode of the
    # on-screen "Select" key, whose whole behaviour lives in the hold-and-drag
    # handlers -- vkb.on_key_select is a no-op, so it is never dispatched, and
    # leaving it unmapped keeps a stray dispatch from sending a real key.

    _pynput_kb = _Controller()
    _pynput_mouse = _MouseController()
    _pynput_keymap = m
    _BACKEND = "pynput"
    return True


def _ensure_backend():
    """Pick uinput on Linux when possible, pynput otherwise. The first
    Keyboard()/Mouse() constructed triggers initialization; the chosen
    backend is then reused for the lifetime of the process."""
    if _BACKEND is not None:
        return
    if sys.platform.startswith("linux") and _init_uinput():
        print("uinput: using /dev/uinput backend (works under Wayland)")
        return
    if _init_pynput():
        why = ""
        if _uinput_init_error is not None:
            why = f" (uinput init failed: {_uinput_init_error!r})"
        print(f"uinput: using pynput backend{why}")
        return
    print("uinput: NO backend available  key/mouse injection disabled")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Keyboard:
    def __init__(self):
        _ensure_backend()

    def _resolve(self, code):
        if not isinstance(code, str):
            return None
        if _BACKEND == "uinput":
            return _uinput_keymap.get(code)
        if _BACKEND == "pynput":
            return _pynput_keymap.get(code)
        return None

    def pressEvent(self, keys):
        if _BACKEND is None:
            return
        if _BACKEND == "uinput":
            for code in keys:
                k = self._resolve(code)
                if k is None:
                    continue
                try:
                    _uinput_kb.write(_e.EV_KEY, k, 1)
                except Exception as exc:
                    print(f"uinput: press {code!r} failed: {exc}")
                # Track caps state ourselves on Linux: KWin under Wayland
                # doesn't propagate the latched-caps state through XWayland's
                # XKB, so adusk.state.is_caps_on() needs a manual signal to
                # know when to flip the OSK glyphs.
                if code == "KEY_CAPSLOCK":
                    try:
                        from adusk import state as _adusk_state
                        _adusk_state.notify_caps_key_sent()
                    except Exception:
                        pass
            try:
                _uinput_kb.syn()
            except Exception as exc:
                print(f"uinput: syn failed: {exc}")
            return
        # pynput
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                _pynput_kb.press(k)
            except Exception as exc:
                print(f"uinput: press {code!r} failed: {exc}")

    def releaseEvent(self, keys):
        if _BACKEND is None:
            return
        if _BACKEND == "uinput":
            for code in keys:
                k = self._resolve(code)
                if k is None:
                    continue
                try:
                    _uinput_kb.write(_e.EV_KEY, k, 0)
                except Exception as exc:
                    print(f"uinput: release {code!r} failed: {exc}")
            try:
                _uinput_kb.syn()
            except Exception as exc:
                print(f"uinput: syn failed: {exc}")
            return
        # pynput
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                _pynput_kb.release(k)
            except Exception as exc:
                print(f"uinput: release {code!r} failed: {exc}")

    # Characters the uinput backend can produce, as (keycode name, needs shift)
    # on a US layout. The kernel backend speaks key codes, not characters, so
    # unlike the Windows tree there is no Unicode escape hatch here  anything
    # outside this table (the phone symbol page's typographic glyphs: € π √ ™
    # and friends) simply can't be injected through /dev/uinput without
    # rewriting the client's XKB map, which is far too invasive to do for a
    # keypress. Those keys fall back to doing nothing on this backend; the
    # pynput backend below handles them properly.
    _TEXT_KEYS = {
        "!": ("KEY_1", True), "@": ("KEY_2", True), "#": ("KEY_3", True),
        "$": ("KEY_4", True), "%": ("KEY_5", True), "^": ("KEY_6", True),
        "&": ("KEY_7", True), "*": ("KEY_8", True), "(": ("KEY_9", True),
        ")": ("KEY_0", True), "_": ("KEY_MINUS", True),
        "+": ("KEY_EQUAL", True), "{": ("KEY_LEFTBRACE", True),
        "}": ("KEY_RIGHTBRACE", True), "|": ("KEY_BACKSLASH", True),
        ":": ("KEY_SEMICOLON", True), "\"": ("KEY_APOSTROPHE", True),
        "<": ("KEY_COMMA", True), ">": ("KEY_DOT", True),
        "?": ("KEY_SLASH", True), "~": ("KEY_GRAVE", True),
        "-": ("KEY_MINUS", False), "=": ("KEY_EQUAL", False),
        "[": ("KEY_LEFTBRACE", False), "]": ("KEY_RIGHTBRACE", False),
        "\\": ("KEY_BACKSLASH", False), ";": ("KEY_SEMICOLON", False),
        "'": ("KEY_APOSTROPHE", False), ",": ("KEY_COMMA", False),
        ".": ("KEY_DOT", False), "/": ("KEY_SLASH", False),
        "`": ("KEY_GRAVE", False), " ": ("KEY_SPACE", False),
    }

    def type_text(self, text):
        """Type a literal character (or short string) as itself.

        The phone layout's symbol pages need keys that produce '@', '{', '%'
        directly, with no Shift held  which a bare keycode cannot express,
        since KEY_2 only gives '@' when Shift happens to be down.

        On the pynput backend the character is handed straight to pynput, which
        resolves it (remapping a spare keysym for anything the layout can't
        reach), so the typographic symbols work. On the kernel uinput backend
        the character is looked up in _TEXT_KEYS and synthesised as an explicit
        Shift chord; characters outside that table are skipped."""
        if _BACKEND is None:
            return
        if _BACKEND == "pynput":
            # KeyCode is bound inside _init_pynput, not at module scope.
            try:
                from pynput.keyboard import KeyCode as _KC
            except Exception:
                return
            for ch in text:
                try:
                    k = _KC.from_char(ch)
                    _pynput_kb.press(k)
                    _pynput_kb.release(k)
                except Exception as exc:
                    print(f"uinput: type {ch!r} failed: {exc}")
            return
        for ch in text:
            entry = self._TEXT_KEYS.get(ch)
            if entry is None:
                if ch.isalnum():
                    entry = ("KEY_" + ch.upper(), ch.isupper())
                else:
                    print(f"uinput: no uinput key for {ch!r}; skipped")
                    continue
            code, needs_shift = entry
            if needs_shift:
                self.pressEvent(["KEY_LEFTSHIFT"])
            self.pressEvent([code])
            self.releaseEvent([code])
            if needs_shift:
                self.releaseEvent(["KEY_LEFTSHIFT"])

    # Clipboard writers tried, in order, for tap_char's paste fallback:
    # Wayland first, then X11. Resolved once  a session with neither simply
    # has no paste path and tap_char falls back to reporting failure.
    _CLIP_WRITERS = (("wl-copy", ()), ("xclip", ("-selection", "clipboard")),
                     ("xsel", ("--clipboard", "--input")))
    _clip_writer = None
    _clip_resolved = False

    def _clip_write(self, text):
        """Put `text` on the clipboard through whichever CLI writer exists.
        True if one accepted it."""
        cls = type(self)
        if not cls._clip_resolved:
            import shutil
            for exe, args in cls._CLIP_WRITERS:
                path = shutil.which(exe)
                if path:
                    cls._clip_writer = (path,) + args
                    break
            cls._clip_resolved = True
        if not cls._clip_writer:
            return False
        try:
            import subprocess
            subprocess.run(list(cls._clip_writer), input=text.encode("utf-8"),
                           timeout=1.0, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def reset_modifier_state(self):
        """Clear the BACKEND's internal modifier bookkeeping without sending
        any OS event.

        Only pynput keeps such state: its Controller uppercases character keys
        while its own `_modifiers` set believes Shift is held, and that set is
        instance-local, so a Shift pressed on one instance and released on
        another leaves it stuck  every later letter then arrives shifted. The
        kernel uinput backend holds no state of its own (each event is written
        straight to /dev/uinput), so there is nothing to clear there."""
        if _BACKEND != "pynput" or _pynput_kb is None:
            return
        try:
            kb = _pynput_kb
            with kb._modifiers_lock:  # pynput internals; absent from its stubs
                kb._modifiers.clear()
            kb._caps_lock = False
        except Exception:
            pass

    def _release_stuck_ctrl(self):
        """Force Ctrl and Alt up, in case a dropped key-up stranded one down.

        A stranded modifier turns every later keypress into a shortcut, so
        nothing types  and it survives an OSK close, because it is OS key
        state rather than app state. The 75% board's latching Ctrl/Alt keys
        hold the real modifiers, which makes this reachable rather than
        theoretical. A key-up on a key that is not held is a no-op, so this is
        safe to fire at every open.

        Shift is deliberately NOT released: the user may be holding it
        legitimately (a held L2, or the on-screen latch) while typing."""
        try:
            self.releaseEvent(["KEY_LEFTCTRL", "KEY_RIGHTCTRL",
                               "KEY_LEFTALT", "KEY_RIGHTALT"])
        except Exception:
            pass

    def force_caps_off(self):
        """Ensure Caps Lock is off, so the board types lowercase by default and
        Shift alone controls capitalisation.

        On X11 the LED state is readable through XKB, and a single KEY_CAPSLOCK
        tap clears it. Without an X display (a Wayland session, or no DISPLAY)
        the state can't be read, and blind-tapping Caps would TURN IT ON half
        the time  so that case deliberately does nothing. Unlike Windows there
        is no remapper here routinely eating the keystroke, so the on-screen
        Caps key can undo it either way."""
        try:
            import ctypes
            import ctypes.util
            libx11 = ctypes.util.find_library("X11")
            if not libx11:
                return
            x11 = ctypes.CDLL(libx11)
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            disp = x11.XOpenDisplay(None)
            if not disp:
                return
            try:
                x11.XkbGetIndicatorState.restype = ctypes.c_int
                x11.XkbGetIndicatorState.argtypes = [ctypes.c_void_p,
                                                     ctypes.c_uint,
                                                     ctypes.POINTER(ctypes.c_uint)]
                st = ctypes.c_uint(0)
                # XkbUseCoreKbd = 0x0100; bit 0 of the state is Caps Lock.
                if x11.XkbGetIndicatorState(disp, 0x0100, ctypes.byref(st)) != 0:
                    return
                if not (st.value & 1):
                    return          # already off
            finally:
                x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
                x11.XCloseDisplay(disp)
            self.pressEvent(["KEY_CAPSLOCK"])
            self.releaseEvent(["KEY_CAPSLOCK"])
        except Exception:
            pass

    def tap_char(self, char):
        """Type a single Unicode character  the accent row's commit path.

        Two injection routes, tried in order:
          1. pynput backend: hand it the char. pynput's XTest path remaps a
             spare keysym for anything the layout can't reach, so accented
             characters come out right on X11.
          2. uinput backend: the kernel has no notion of characters, only
             keycodes, so an accent that isn't on the active layout can't be
             synthesised directly. Put it on the clipboard and send Ctrl+V,
             which every text field accepts.
        Returns True if a route injected the character."""
        if not char or len(char) != 1 or _BACKEND is None:
            # Single character only, matching the Windows contract: the direct
            # key path resolves one keycode and the callers commit one glyph.
            return False
        if _BACKEND == "pynput":
            try:
                from pynput.keyboard import KeyCode as _KC
                k = _KC.from_char(char)
                _pynput_kb.press(k)
                _pynput_kb.release(k)
                return True
            except Exception as exc:
                print(f"uinput: tap_char {char!r} failed: {exc}")
                return False
        # Kernel uinput: a plain ASCII character that IS on the layout can be
        # tapped directly (same table type_text uses); anything else pastes.
        entry = self._TEXT_KEYS.get(char)
        if entry is None and char.isalnum() and char.isascii():
            entry = ("KEY_" + char.upper(), char.isupper())
        if entry is not None:
            code, needs_shift = entry
            if needs_shift:
                self.pressEvent(["KEY_LEFTSHIFT"])
            self.pressEvent([code])
            self.releaseEvent([code])
            if needs_shift:
                self.releaseEvent(["KEY_LEFTSHIFT"])
            return True
        if not self._clip_write(char):
            return False
        # Small settle: the clipboard owner has to be live before the paste,
        # and wl-copy/xclip fork a holder process to serve the selection.
        time.sleep(0.02)
        self.tap_with_modifier("KEY_LEFTCTRL", "KEY_V")
        return True

    def tap_with_modifier(self, modifier_code, key_code):
        """Press modifier+key as a chord, then release it (Ctrl+V, Win+. ...).

        On the uinput backend the raw key codes combine with the modifier at the
        kernel level, so a plain press/release sequence yields a true shortcut.
        (This mirrors the Windows tree's fix, where the pynput backend instead
        had to resolve the char to a raw VK because pynput injects printable
        chars as char/Unicode events that ignore a held modifier.)
        """
        self.pressEvent([modifier_code])
        self.pressEvent([key_code])
        self.releaseEvent([key_code])
        self.releaseEvent([modifier_code])


class Mouse:
    """Relative cursor movement; symmetric with Keyboard."""

    def __init__(self):
        _ensure_backend()

    def move(self, dx, dy):
        if not dx and not dy:
            return
        if _BACKEND == "uinput":
            try:
                _uinput_mouse.write(_e.EV_REL, _e.REL_X, int(dx))
                _uinput_mouse.write(_e.EV_REL, _e.REL_Y, int(dy))
                _uinput_mouse.syn()
            except Exception as exc:
                print(f"uinput: mouse move ({dx},{dy}) failed: {exc}")
                return
            # Keep the dead-reckoned position current. Tracked from the INTs
            # actually emitted, not the raw floats, so the tracker accumulates
            # exactly what the kernel saw.
            _cursor_track_move(int(dx), int(dy))
            return
        if _BACKEND == "pynput":
            try:
                _pynput_mouse.move(int(dx), int(dy))
            except Exception as exc:
                print(f"uinput: mouse move ({dx},{dy}) failed: {exc}")

    def button(self, button, pressed):
        """Press (pressed=True) or release (False) a mouse button.
        `button` is 'left', 'right', 'middle', or 'back'/'forward' (the
        side buttons  Page Previous/Next, honored as browser Back/Forward
        by most X11/Wayland apps)."""
        if _BACKEND == "uinput":
            code = {
                "left": _e.BTN_LEFT,
                "right": _e.BTN_RIGHT,
                "middle": _e.BTN_MIDDLE,
                "back": _e.BTN_SIDE,
                "forward": _e.BTN_EXTRA,
            }.get(button)
            if code is None:
                return
            try:
                _uinput_mouse.write(_e.EV_KEY, code, 1 if pressed else 0)
                _uinput_mouse.syn()
            except Exception as exc:
                print(f"uinput: mouse button {button} failed: {exc}")
            return
        if _BACKEND == "pynput":
            try:
                from pynput.mouse import Button as _B
                btn = {"left": _B.left, "right": _B.right,
                       "middle": _B.middle,
                       "back": getattr(_B, "button8", None),
                       "forward": getattr(_B, "button9", None)}.get(button)
                if btn is None:
                    return
                if pressed:
                    _pynput_mouse.press(btn)
                else:
                    _pynput_mouse.release(btn)
            except Exception as exc:
                print(f"uinput: mouse button {button} failed: {exc}")

    # press/release: Windows-API-symmetric wrappers over button() so the shared
    # adusk/controller.py (cp-mirrored from windows/) can drive mouse clicks the
    # same way on both platforms.
    def press(self, button="left"):
        self.button(button, True)

    def release(self, button="left"):
        self.button(button, False)

    def get_position(self):
        """Absolute cursor position (x, y) in screen px; (0, 0) on failure.

        The pynput backend reads the real pointer. The uinput backend returns
        the DEAD-RECKONED position instead  see the _cursor_xy comment for
        why there's nothing to read on Wayland, and what that costs. Callers
        (pinch-zoom's focal point, the scrub gesture's cursor save/restore)
        want an approximate live cursor, which this gives them; before this
        existed they got a hard (0, 0) and anchored to the desktop corner."""
        if _BACKEND == "pynput":
            try:
                px, py = _pynput_mouse.position
                return int(px), int(py)
            except Exception as exc:
                print(f"uinput: mouse get_position failed: {exc}")
                return 0, 0
        if _BACKEND == "uinput":
            global _cursor_xy
            if _cursor_xy is None:
                _cursor_xy = list(_seed_cursor())
            return int(round(_cursor_xy[0])), int(round(_cursor_xy[1]))
        return 0, 0

    def set_position(self, x, y):
        """Warp the cursor to an absolute screen position (used by the Video
        Timeline Scrubbing hover mode to ride the progress bar).

        On the uinput backend this drives the absolute pointer device, which
        is the only cursor warp that works under Wayland. Screen pixels are
        converted to the device's normalized axis range here rather than the
        device declaring a pixel range, so a resolution change costs a bounds
        re-probe instead of recreating the device. Windows-API-symmetric with
        the Windows wrapper."""
        global _cursor_xy, _abs_warn_done
        if _BACKEND == "pynput":
            try:
                _pynput_mouse.position = (int(x), int(y))
            except Exception as exc:
                print(f"uinput: mouse set_position ({x},{y}) failed: {exc}")
            return
        if _BACKEND != "uinput":
            return
        bounds = _desktop_bounds()
        if _uinput_abs is None or bounds is None:
            if not _abs_warn_done:
                _abs_warn_done = True
                why = ("no absolute pointer device" if _uinput_abs is None
                       else "screen bounds unreadable")
                print(f"uinput: cursor warping disabled ({why})")
            return
        bx, by, bw, bh = bounds
        bw = max(1, bw)
        bh = max(1, bh)
        # Clamp before scaling: an off-desktop request would otherwise scale
        # past the axis range and land somewhere arbitrary.
        px = min(max(int(x), bx), bx + bw - 1)
        py = min(max(int(y), by), by + bh - 1)
        # Sample at the pixel CENTRE (+0.5) and divide by the number of axis
        # buckets (_ABS_MAX + 1). That's the exact inverse of what libinput
        # does on the way back out  scale_axis() is
        #     screen = (val - min) * width / (max - min + 1)
        # i.e. it treats the axis as 65536 buckets spread over the width, not
        # as endpoints pinned to 0 and _ABS_MAX. Mapping the endpoints instead
        # round-trips every pixel about half a bucket low, which costs a whole
        # pixel across most of the screen; this way every px survives the
        # round trip exactly, ends included.
        nx = int(round((px - bx + 0.5) * (_ABS_MAX + 1) / bw))
        ny = int(round((py - by + 0.5) * (_ABS_MAX + 1) / bh))
        nx = min(max(nx, 0), _ABS_MAX)
        ny = min(max(ny, 0), _ABS_MAX)
        try:
            _uinput_abs.write(_e.EV_ABS, _e.ABS_X, nx)
            _uinput_abs.write(_e.EV_ABS, _e.ABS_Y, ny)
            _uinput_abs.syn()
        except Exception as exc:
            print(f"uinput: mouse set_position ({x},{y}) failed: {exc}")
            return
        # A warp is ground truth for the tracker  record the CLAMPED point,
        # which is where the cursor actually ended up.
        _cursor_xy = [float(px), float(py)]

    def scroll(self, dx, dy):
        """Scroll wheel; +dy = up (matches pynput / the Windows wrapper)."""
        if not dx and not dy:
            return
        if _BACKEND == "uinput":
            try:
                if dx:
                    _uinput_mouse.write(_e.EV_REL, _e.REL_HWHEEL, int(dx))
                if dy:
                    _uinput_mouse.write(_e.EV_REL, _e.REL_WHEEL, int(dy))
                _uinput_mouse.syn()
            except Exception as exc:
                print(f"uinput: mouse scroll ({dx},{dy}) failed: {exc}")
            return
        if _BACKEND == "pynput":
            try:
                _pynput_mouse.scroll(int(dx), int(dy))
            except Exception as exc:
                print(f"uinput: mouse scroll ({dx},{dy}) failed: {exc}")
