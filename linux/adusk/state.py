import ctypes
import time
from collections import deque
from enum import IntEnum
from threading import Event, Lock

from adusk import diacritics

# Shutdown latch for the OSK's render loop. An Event rather than a bool behind a
# Lock: set/clear/is_set are already atomic, so the render loop can poll it every
# frame without taking a lock, and `reset_session()` re-arms it by clearing.
_exit_event = Event()

_visible = True
_visible_lock = Lock()

_shift_held = False
_shift_lock = Lock()

# Latched Shift from the mouse/click path (clicking the on-screen Shift key or
# right-click). Kept separate from _shift_held because a connected controller
# rewrites _shift_held every input frame  the toggle must read its own latch
# to decide on/off, or it would never turn back off.
_shift_latched = False
_shift_latch_lock = Lock()

# Set of keycode strings (e.g. "KEY_BACKSPACE") that should render in the
# CLICK (blue) state, e.g. while their corresponding controller button is held.
_highlighted = set()
_highlight_lock = Lock()

# Touchpad capacitive-touch state. Renderer uses it to hide the L2/R2 hint
# glyphs on Shift/Enter while LT/RT's alternate "click under pad" role is
# active.
_lpad_touched = False
_rpad_touched = False
_touch_lock = Lock()

# DPAD cursor over the virtual keyboard's grid. The cursor key is painted
# as HOVER; the A button presses it.
_cursor = (2, 5)
_cursor_lock = Lock()

# (row, col) of the key currently held down by the left mouse button, or None.
# Painted in the CLICK (blue) state so a mouse press flashes like a real key
# press. Kept separate from _highlighted (which a controller frame overwrites).
_mouse_press_cell = None
_mouse_press_lock = Lock()

# Number of keys in each row of the active keyboard layout. Published by
# the main thread after the layout is built so the controller thread can
# clamp DPAD navigation.
_grid_dims = []
_grid_lock = Lock()

# Queue of (row, col) cells whose KeyButton callback should fire on the
# main thread (the A button enqueues here on each rising edge).
_key_press_queue = deque()
_key_press_lock = Lock()

# DPAD direction events posted by the controller thread; consumed by the
# main loop, which has access to the keyboard layout for pixel-aware nav.
_dpad_queue = deque()
_dpad_lock = Lock()

# Set by the Move-key (shift held) callback to ask the main thread to
# advance the keyboard window through its 6-position rotation.
_position_cycle_requested = False
_position_cycle_lock = Lock()

# Lock-screen launcher: ask the main thread to JUMP to a SPECIFIC position
# index (0-5, see adusk._apply_window_position) instead of cycling  used to
# move the OSK out of the way of the LogonUI password box once its on-screen
# location is known. None = no pending request.
_window_position_request = None
_window_position_lock = Lock()

# Tracks whether the OS emoji picker (opened by the on-screen emoji key) is
# currently open, so pressing the emoji key again closes it (sends Escape)
# instead of re-opening. Reset per OSK open so a fresh session starts closed.
_emoji_open = False
_emoji_lock = Lock()

# HWND (int) of the window the user was typing in just before the OSK opened.
# adusk.main restores focus to it after showing the OSK: a controller-open
# fires the firmware lizard's mouse-click, which can land off the target field
# and steal focus. The OSK window is NOACTIVATE so it never takes focus, so
# re-activating this window puts the caret back. None = nothing to restore.
_focus_restore_target = None
_focus_restore_lock = Lock()

# Latest SDL-pad frame (SteamControllerInput) published by the tray's
# sdl_gamepad_thread while the OSK is open. adusk reads it via inputsrc
# .SharedSdlFrameSource instead of opening the pad a SECOND time  two
# Sdl3GamepadSource instances on two threads double-drove the same pad and
# delivered no input. None = no SDL pad frame this tick.
_sdl_frame = None
# Controller kind of the pad behind _sdl_frame ("switch"/"xbox"/"ps5"/...) so
# the OSK glyph swap can show the right family's art.
_sdl_frame_kind = "switch"
_sdl_frame_lock = Lock()

# Reference to the tray's Sdl3GamepadSource. While the OSK is open, adusk.main
# polls it on ITS OWN thread  the one pumping SDL events  because SDL only
# refreshes gamepad state on the event-pump thread, so the tray thread goes
# blind (reads all-zero buttons) once the OSK window's event loop is running.
_sdl_source = None

# Optional haptic-feedback hook. The controller thread registers a callable
# (bound to the live SteamController) here; the main thread calls haptic_tick()
# on each key press for a trackpad "tick". None when no controller is open.
_haptic_tick = None
# Dedicated hook for L2/R2 trigger-actuation feedback: routed per controller
# kind so only ANALOG-trigger controllers buzz (the rumble stands in for the
# click a digital trigger  Switch ZL/ZR  already has). Falls back to the
# pad-click hook when unregistered so nothing goes silent.
_trigger_haptic = None
# Separate, stronger hook for the simulated physical pad-click (press/release)
# so only that feedback is deeper/more intense than the light UI tick.
_pad_click_haptic = None
# Per-controller on/off for haptics (UI ticks AND gamepad rumble), keyed by
# controller kind ("sc", "switch", "xbox", "ps5", ...  the pads.py catalog;
# the legacy "sdl" alias maps to "switch"). Each controller's Options category
# has its own Haptics toggle (there is no global switch). OSK key-press ticks
# fan to the ACTIVE controller, so they read the active controller's entry.
_rumble_enabled = {"sc": True, "switch": True}
_haptic_lock = Lock()

# Which controller most recently drove the on-screen keyboard: "sc" (Steam
# Controller) or any SDL pad kind from the pads.py catalog ("switch", "xbox",
# "ps5", "steam_deck", ...). The renderer reads this to pick the Shift/Enter
# trigger glyphs (Steam Controller L2/R2 vs ZL/ZR vs LT/RT ...). Seeded at
# startup from the saved setting so the right glyphs show before any input,
# then updated live by InputMerger.poll() as each controller is used. A
# registered persist hook saves changes to disk so the last-used controller's
# glyphs stick across restarts. NOT cleared by reset_session  the choice must
# survive each OSK open/close.
_active_controller = "sc"
_active_controller_lock = Lock()
_active_controller_persist = None  # callable(kind) set by the tray to save it
# Glyph-switch debounce: after the active controller CHANGES, further changes
# are ignored for this long. During a physical hand-over both controllers emit
# input edges (setting one down slides its trackpad / jostles a stick while the
# other is first pressed), which used to flip the OSK glyphs back and forth
# ~3 times before settling. The first deliberate edge wins; the losing
# controller's transition noise inside the window can't steal the glyphs back.
# Same-kind refreshes are unaffected, and a real second switch just needs to
# land after the window (imperceptible for deliberate use).
_ACTIVE_SWITCH_COOLDOWN = 0.6
_active_switched_at = 0.0

# Legacy alias: settings written before the multi-controller catalog stored
# "sdl" for "any SDL pad" (in practice the Switch Pro).
_KIND_ALIASES = {"sdl": "switch"}


def _canon_kind(kind):
    """Canonical controller kind, or None when unusable. Accepts any non-empty
    string so new pads.py kinds never need this file to change; only the legacy
    "sdl" alias is rewritten."""
    if not kind or not isinstance(kind, str):
        return None
    return _KIND_ALIASES.get(kind, kind)

# Win32: ask the OS whether Caps Lock is currently toggled on. Lets the
# on-screen keyboard mirror the system caps state automatically  we don't
# need to track L3 ourselves because L3 just sends KEY_CAPSLOCK to the OS.
_VK_CAPITAL = 0x14
try:
    _user32 = ctypes.windll.user32
    _user32.GetKeyState.restype = ctypes.c_short
except Exception:
    _user32 = None


def close():
    """Ask the OSK's render loop to tear down at the end of this frame."""
    _exit_event.set()


def reset_session():
    """Wipe per-session state so adusk.main() can be invoked again from a
    long-lived launcher process (no subprocess startup cost)."""
    global _visible, _shift_held, _shift_latched, _highlighted
    global _lpad_touched, _rpad_touched, _cursor, _grid_dims
    global _position_cycle_requested, _mouse_press_cell, _focus_restore_target
    global _sdl_frame, _emoji_open, _window_position_request
    global _ctrl_latched, _alt_latched, _select_active, _diacritic
    global _virtual_kb
    _exit_event.clear()
    # The next session builds its own board; a stale one here would let the
    # input thread hit-test last session's layout on its first frames.
    _virtual_kb = None
    with _ctrl_latch_lock:
        _ctrl_latched = False
    with _alt_latch_lock:
        _alt_latched = False
    with _select_lock:
        _select_active = False
    with _diacritic_lock:
        # A variant row left open from the last session would swallow the
        # next one's first hold (every press would queue a repeat instead).
        _diacritic = None
    with _emoji_lock:
        _emoji_open = False
    with _focus_restore_lock:
        _focus_restore_target = None
    with _sdl_frame_lock:
        _sdl_frame = None
    with _visible_lock:
        _visible = True
    with _shift_lock:
        _shift_held = False
    with _shift_latch_lock:
        _shift_latched = False
    with _highlight_lock:
        _highlighted = set()
    with _touch_lock:
        _lpad_touched = False
        _rpad_touched = False
    with _cursor_lock:
        _cursor = (2, 5)
    with _mouse_press_lock:
        _mouse_press_cell = None
    with _grid_lock:
        _grid_dims = []
    with _key_press_lock:
        _key_press_queue.clear()
    with _dpad_lock:
        _dpad_queue.clear()
    with _position_cycle_lock:
        _position_cycle_requested = False
    with _window_position_lock:
        _window_position_request = None


def set_focus_restore_target(hwnd):
    """Record the window (HWND int) to re-focus after the OSK opens, or None."""
    global _focus_restore_target
    with _focus_restore_lock:
        _focus_restore_target = hwnd


def get_focus_restore_target():
    with _focus_restore_lock:
        return _focus_restore_target


def set_sdl_frame(frame, kind=None):
    """Publish the latest SDL-pad frame (SteamControllerInput) for the OSK.
    `kind` is the controller kind of the pad that produced it ("switch",
    "xbox", ...) so the OSK glyph swap can name the actual family; None keeps
    the previous kind (SharedSdlFrameSource falls back to "switch")."""
    global _sdl_frame, _sdl_frame_kind
    with _sdl_frame_lock:
        _sdl_frame = frame
        if kind:
            _sdl_frame_kind = kind


def get_sdl_frame_kind():
    with _sdl_frame_lock:
        return _sdl_frame_kind


def get_sdl_frame():
    with _sdl_frame_lock:
        return _sdl_frame


def set_sdl_source(src):
    """Register the tray's Sdl3GamepadSource so adusk.main can poll it on its
    own SDL event-pump thread while the OSK is open."""
    global _sdl_source
    _sdl_source = src


def get_sdl_source():
    return _sdl_source


# --- "Gyro To Mouse" shared runtime state -------------------------------------
# The single source of truth for which controller kinds currently have
# gyro-to-mouse toggled ON (session-only; always starts empty). Shared here so
# BOTH runtimes stay in sync: the tray paths (OS-cursor gyro + toggle chords
# while the OSK is closed) and the OSK paths (the gyro trackpad-circle pointer
# + toggle chords while it's open  the tray cedes the controllers then).
# _gyro_toggle_masks: kind -> tuple of button masks for that kind's "Gyro To
# Mouse" hotkey chords, published by the tray (startup + every chords save) so
# the OSK can evaluate the hotkey against its own merged frames.
# _kbd_gyro_always: the Options → Keyboard "Always Type With Gyro" switch. On =
# the gyro steers the OSK pointer from the moment the keyboard opens, with no
# prior toggle. Deliberately NOT folded into _gyro_mouse_kinds: that set is the
# DESKTOP gyro-mouse state, and typing with gyro must not leave the OS cursor
# gyro-driven once the keyboard closes. controller.py keeps the per-session
# on/off (the gyro hotkey can still flip it while typing).
_gyro_lock = Lock()
_gyro_mouse_kinds = set()
_gyro_toggle_masks = {}
_kbd_gyro_always = False


def set_gyro_mouse(kind, on):
    with _gyro_lock:
        if on:
            _gyro_mouse_kinds.add(kind)
        else:
            _gyro_mouse_kinds.discard(kind)


def toggle_gyro_mouse(kind):
    """Flip one kind's gyro-to-mouse state; returns the NEW state."""
    with _gyro_lock:
        if kind in _gyro_mouse_kinds:
            _gyro_mouse_kinds.discard(kind)
            return False
        _gyro_mouse_kinds.add(kind)
        return True


def is_gyro_mouse_active(kind):
    with _gyro_lock:
        return kind in _gyro_mouse_kinds


def get_gyro_mouse_kinds():
    with _gyro_lock:
        return frozenset(_gyro_mouse_kinds)


def set_gyro_toggle_masks(masks):
    """Publish every kind's gyro-toggle hotkey masks ({kind: [int, ...]})."""
    global _gyro_toggle_masks
    with _gyro_lock:
        _gyro_toggle_masks = {k: tuple(v) for k, v in (masks or {}).items()}


def get_gyro_toggle_masks_for(kind):
    with _gyro_lock:
        return _gyro_toggle_masks.get(kind, ())


def set_kbd_gyro_always(enabled):
    """Options → Keyboard "Always Type With Gyro"."""
    global _kbd_gyro_always
    with _gyro_lock:
        _kbd_gyro_always = bool(enabled)


def is_kbd_gyro_always():
    with _gyro_lock:
        return _kbd_gyro_always


def get_gyro_stream_kinds():
    """Kinds whose gyro hardware must be STREAMING: the toggled-on ones plus,
    while "Always Type With Gyro" is on, the active controller (which types
    with gyro without ever entering the desktop gyro-mouse set)."""
    kinds = get_gyro_mouse_kinds()
    if is_kbd_gyro_always():
        active = get_active_controller()
        if active:
            kinds = kinds | {active}
    return kinds


# Per-kind gyro tuning (the cog-wheel "Gyro To Mouse" modal on each gyro-
# capable controller's Options category). Published by the tray from settings
# (startup + live edits); read per frame by every gyro consumer (the tray's
# _GyroMouse OS-cursor paths and the OSK's gyro trackpad-circle pointer).
#   mode      "none" (hotkey + gyro disabled) / "hold_enable" (gyro only while
#             the hotkey is held) / "hold_suppress" (gyro always on, hotkey
#             held suppresses it) / "toggle" (hotkey flips it on/off).
#   dots      mouse pixels one full 360° turn generates at 1x sensitivity
#             (Steam's "Dots Per 360°"); px/deg = dots / 360.
#   sens      output multiplier on top of the calibrated dots value.
#   accel     "off" / "linear" / "relaxed" / "aggressive"  speed-dependent
#             sensitivity ramp (see gyro_shape).
#   deadzone  °/s below which rotation is ignored (soft: subtracted from the
#             magnitude so motion ramps from zero  hand-shake filter).
#   precision °/s under which sensitivity scales down with speed (small
#             motions become even smaller; an alternative to the deadzone).
#   output    "mouse" (OS cursor  the classic gyro mouse) / "rstick" (in
#             GAMEPAD mode the gyro deflects the virtual pad's RIGHT STICK
#             instead  Steam's "Gyro To Joystick"; desktop mode falls back
#             to the cursor since there's no pad to steer).
# The mode DEFAULT is "toggle", paired with the default both-thumbsticks hotkey
# (pads.GYRO_TOGGLE_DEFAULT_BUTTONS): out of the box L3 + R3 flips the gyro
# mouse on and off. The gyro still starts OFF  toggle only arms the gesture.
GYRO_DEFAULTS = {"mode": "toggle", "dots": 6545.0, "sens": 2.5,
                 "accel": "off", "deadzone": 0.36, "precision": 0.75,
                 "output": "mouse"}
_gyro_cfg = {}   # kind -> dict (partial; merged over GYRO_DEFAULTS on read)


def set_gyro_config(kind, **vals):
    """Merge per-kind gyro tuning values (any subset of GYRO_DEFAULTS' keys)."""
    with _gyro_lock:
        cfg = dict(_gyro_cfg.get(kind) or {})
        for k, v in vals.items():
            if k in GYRO_DEFAULTS and v is not None:
                cfg[k] = v
        _gyro_cfg[kind] = cfg


def get_gyro_config(kind):
    """This kind's full gyro tuning dict (defaults filled in)."""
    with _gyro_lock:
        cfg = dict(GYRO_DEFAULTS)
        cfg.update(_gyro_cfg.get(kind) or {})
        return cfg


def get_gyro_mode(kind):
    with _gyro_lock:
        return (_gyro_cfg.get(kind) or {}).get("mode", GYRO_DEFAULTS["mode"])


def get_gyro_output(kind):
    """"mouse" or "rstick"  where this kind's gyro motion lands (rstick only
    applies while a virtual pad is live; every caller falls back to mouse)."""
    with _gyro_lock:
        return (_gyro_cfg.get(kind) or {}).get("output",
                                               GYRO_DEFAULTS["output"])


def get_gyro_stick_gain(kind):
    """Right-stick units per °/s of rotation for the gyro→joystick output.
    Anchored so the stick saturates at (360 / sens) °/s  i.e. at the default
    2.5x sensitivity a ~144°/s flick is full deflection; higher sensitivity
    saturates on smaller motions."""
    cfg = get_gyro_config(kind)
    try:
        sens = max(0.1, float(cfg["sens"]))
    except (TypeError, ValueError):
        sens = GYRO_DEFAULTS["sens"]
    return 32767.0 * sens / 360.0


def get_gyro_gain(kind):
    """Mouse px per DEGREE of rotation: dots-per-360 / 360 x sensitivity."""
    cfg = get_gyro_config(kind)
    try:
        return max(0.0, float(cfg["dots"])) / 360.0 * max(0.0, float(cfg["sens"]))
    except (TypeError, ValueError):
        return GYRO_DEFAULTS["dots"] / 360.0 * GYRO_DEFAULTS["sens"]


def gyro_shape(kind, yaw_dps, pitch_dps):
    """Apply this kind's deadzone / precision / acceleration curves to one
    angular-velocity sample (°/s)  see _shape_gyro."""
    return _shape_gyro(get_gyro_config(kind), yaw_dps, pitch_dps)


def _shape_gyro(cfg, yaw_dps, pitch_dps):
    """Apply one gyro config's deadzone / precision / acceleration curves to
    an angular-velocity sample (°/s). Operates on the vector MAGNITUDE so
    diagonals shape identically to pure-axis motion; returns the shaped
    (yaw, pitch). Deadzone is SOFT (subtracted) so output ramps from zero,
    the precision filter scales sub-threshold speeds down proportionally,
    and acceleration multiplies output by a speed-dependent factor.

    Shared by the per-kind gyro mouse (gyro_shape) and the global gyro typing
    curves (kbd_gyro_shape)  the maths is identical, only the config differs."""
    mag = (yaw_dps * yaw_dps + pitch_dps * pitch_dps) ** 0.5
    if mag <= 0.0:
        return 0.0, 0.0
    try:
        dz = max(0.0, float(cfg["deadzone"]))
        prec = max(0.0, float(cfg["precision"]))
    except (TypeError, ValueError):
        dz, prec = GYRO_DEFAULTS["deadzone"], GYRO_DEFAULTS["precision"]
    out = mag - dz
    if out <= 0.0:
        return 0.0, 0.0
    if prec > 0.0 and out < prec:
        out *= out / prec
    accel = cfg.get("accel", "off")
    if accel == "linear":
        out *= min(3.0, 1.0 + mag / 300.0)
    elif accel == "relaxed":
        out *= min(2.0, 1.0 + (mag / 300.0) ** 0.5)
    elif accel == "aggressive":
        out *= min(4.0, 1.0 + (mag / 150.0) ** 1.5)
    scale = out / mag
    return yaw_dps * scale, pitch_dps * scale


# --- "Gyro To Type" tuning (the cog on Options → Keyboard) --------------------
# GLOBAL, deliberately NOT per-kind: gyro typing is one feel across every
# controller, so the keyboard owns its own curves instead of inheriting
# whichever pad happens to be active. Only the knobs that change how the OSK
# pointer FEELS are here  the per-controller Gyro To Mouse modal keeps the
# mouse-specific ones (Dots Per 360° calibration, Gyro Output) and the hotkey
# mode/bars, which stay per-controller because the buttons differ per pad.
#   sens       pointer speed multiplier; 2.5 (the gyro-mouse default) sweeps
#              the whole keyboard in a ~40° turn (see controller.py's
#              GYRO_OSK_FRAC_PER_GAIN, which is normalized to exactly that).
#   accel      "off" / "linear" / "relaxed" / "aggressive" (see _shape_gyro)
#   deadzone   °/s below which rotation is ignored (hand-shake filter)
#   precision  °/s under which sensitivity scales down with speed
KBD_GYRO_DEFAULTS = {"sens": 2.5, "accel": "off", "deadzone": 0.36,
                     "precision": 0.75}
_kbd_gyro_cfg = {}


def set_kbd_gyro_config(**vals):
    """Merge gyro-typing tuning values (any subset of KBD_GYRO_DEFAULTS)."""
    with _gyro_lock:
        for k, v in vals.items():
            if k in KBD_GYRO_DEFAULTS and v is not None:
                _kbd_gyro_cfg[k] = v


def get_kbd_gyro_config():
    """The full gyro-typing tuning dict (defaults filled in)."""
    with _gyro_lock:
        cfg = dict(KBD_GYRO_DEFAULTS)
        cfg.update(_kbd_gyro_cfg)
        return cfg


def kbd_gyro_gain():
    """Gyro-typing pointer gain, in the same px-per-DEGREE units as
    get_gyro_gain so controller.py's frac-per-gain constant applies unchanged.
    The keyboard has no Dots Per 360° calibration of its own (nothing here is
    calibrated against a game camera), so the gyro-mouse default anchors it and
    Gyro Sensitivity is the only multiplier."""
    cfg = get_kbd_gyro_config()
    try:
        return GYRO_DEFAULTS["dots"] / 360.0 * max(0.0, float(cfg["sens"]))
    except (TypeError, ValueError):
        return GYRO_DEFAULTS["dots"] / 360.0 * KBD_GYRO_DEFAULTS["sens"]


def kbd_gyro_shape(yaw_dps, pitch_dps):
    """gyro_shape's gyro-typing twin  the global curves, no kind."""
    return _shape_gyro(get_kbd_gyro_config(), yaw_dps, pitch_dps)


def should_close():
    """True once `close()` has been called and the loop should unwind."""
    return _exit_event.is_set()


def is_visible():
    with _visible_lock:
        return _visible


def show():
    global _visible
    with _visible_lock:
        _visible = True


def is_shift_held():
    with _shift_lock:
        return _shift_held


def set_shift_held(value):
    global _shift_held
    with _shift_lock:
        _shift_held = bool(value)


def is_shift_latched():
    with _shift_latch_lock:
        return _shift_latched


def set_shift_latched(value):
    global _shift_latched
    with _shift_latch_lock:
        _shift_latched = bool(value)


def set_active_controller(kind):
    """Record which controller is driving the OSK: "sc" (Steam Controller) or
    any SDL pad kind ("switch", "xbox", "ps5", ...). Callers pass this only on
    a fresh INTENTIONAL input edge (a button/click/stick deflection  see
    InputMerger.poll), so a hand merely resting on a controller can't flip the
    glyphs. A short cooldown after each ACTUAL switch absorbs the transition
    noise of physically swapping controllers (see _ACTIVE_SWITCH_COOLDOWN) 
    the glyphs change once, not back-and-forth. Persists via the registered
    hook only when the value actually changes, so disk writes stay rare."""
    global _active_controller, _active_switched_at
    kind = _canon_kind(kind)
    if kind is None:
        return
    with _active_controller_lock:
        if kind == _active_controller:
            return
        now = time.monotonic()
        if now - _active_switched_at < _ACTIVE_SWITCH_COOLDOWN:
            return
        _active_switched_at = now
        _active_controller = kind
        cb = _active_controller_persist
    if cb is not None:
        try:
            cb(kind)
        except Exception:
            pass


def get_active_controller():
    with _active_controller_lock:
        return _active_controller


def init_active_controller(kind):
    """Seed the active controller at startup from the saved setting, WITHOUT
    firing the persist hook (the value already matches what's on disk)."""
    global _active_controller
    kind = _canon_kind(kind)
    if kind is None:
        return
    with _active_controller_lock:
        _active_controller = kind


def set_active_controller_persist(fn):
    """Register a callback invoked (with the new kind) whenever the active
    controller changes, so the tray can save it to settings.json."""
    global _active_controller_persist
    with _active_controller_lock:
        _active_controller_persist = fn


def is_caps_on():
    """True if the OS has Caps Lock currently toggled on."""
    if _user32 is None:
        return False
    return bool(_user32.GetKeyState(_VK_CAPITAL) & 0x0001)


def set_highlighted(items):
    global _highlighted
    with _highlight_lock:
        _highlighted = set(items)


def get_highlighted():
    with _highlight_lock:
        return set(_highlighted)


def is_lpad_touched():
    with _touch_lock:
        return _lpad_touched


def is_rpad_touched():
    with _touch_lock:
        return _rpad_touched


def set_pad_touched(left, right):
    global _lpad_touched, _rpad_touched
    with _touch_lock:
        _lpad_touched = bool(left)
        _rpad_touched = bool(right)


def get_cursor():
    with _cursor_lock:
        return _cursor


def set_cursor(row, col):
    global _cursor
    with _cursor_lock:
        _cursor = (int(row), int(col))


def get_mouse_press_cell():
    with _mouse_press_lock:
        return _mouse_press_cell


def set_mouse_press_cell(cell):
    global _mouse_press_cell
    with _mouse_press_lock:
        _mouse_press_cell = tuple(cell) if cell is not None else None


def set_grid_dims(cols_per_row):
    global _grid_dims
    with _grid_lock:
        _grid_dims = list(cols_per_row)


def queue_key_press(row, col, repeat=False, silent=False):
    # repeat=True marks an auto-repeat hit (A held); the main thread only acts
    # on it over Backspace, so holding rubs out text without machine-gunning
    # ordinary keys.
    # silent=True is a DEFERRED base letter typed on release  its press edge
    # already clicked, so it must not click a second time.
    with _key_press_lock:
        _key_press_queue.append((int(row), int(col), bool(repeat),
                                 bool(silent)))


def drain_key_press_queue():
    with _key_press_lock:
        out = list(_key_press_queue)
        _key_press_queue.clear()
    return out


# --- typed-key echo (the first-run tour's keyboard slide) --------------------
# The OSK types into whatever window holds focus. During the tour that is the
# manager window  deliberately, so the keyboard can't raise the user's last
# app over the tour (see the tutorial_claimed short-circuit in the tray)  and
# the manager has no text field, so a user asked to "type hi" would press two
# keys and see absolutely nothing happen. So every key the keyboard fires is
# echoed here and the slide draws it back in a little box of its own.
#
# Nothing accumulates unless somebody has asked to watch: set_typed_watch(True)
# is what the slide turns on, and it turns it off again on the way out. The
# buffer is a small ring  this is a display echo, not a keylog, and it is
# gone the moment the slide is.
_typed_lock = Lock()
_typed_keys = []
_typed_watch = False
_TYPED_MAX = 40


def set_typed_watch(on):
    """Start/stop echoing typed keys. Clears whatever was there either way, so
    a watcher always starts from an empty box."""
    global _typed_watch
    with _typed_lock:
        _typed_watch = bool(on)
        _typed_keys.clear()


def note_typed_key(label):
    """One key fired by the keyboard, by its VISIBLE label ("h", "Space",
    "Backspace"). Called from vkb.dispatch_key  the single funnel every press
    goes through  so it costs a lock and a flag test when nobody is watching."""
    if not _typed_watch or not label:
        return
    with _typed_lock:
        if not _typed_watch:
            return
        _typed_keys.append(str(label))
        del _typed_keys[:-_TYPED_MAX]


def get_typed_keys():
    with _typed_lock:
        return list(_typed_keys)


def queue_dpad(direction, haptic=False):
    with _dpad_lock:
        _dpad_queue.append((direction, bool(haptic)))


def drain_dpad_queue():
    with _dpad_lock:
        out = list(_dpad_queue)
        _dpad_queue.clear()
    return out


def request_position_cycle():
    global _position_cycle_requested
    with _position_cycle_lock:
        _position_cycle_requested = True


def take_position_cycle_request():
    global _position_cycle_requested
    with _position_cycle_lock:
        v = _position_cycle_requested
        _position_cycle_requested = False
    return v


def request_window_position(index):
    """Ask the main loop to jump the OSK to position-rotation slot `index`
    (0-5). Used by the lock-screen launcher to dodge the password box."""
    global _window_position_request
    with _window_position_lock:
        _window_position_request = index


def take_window_position_request():
    global _window_position_request
    with _window_position_lock:
        v = _window_position_request
        _window_position_request = None
    return v


def is_emoji_open():
    with _emoji_lock:
        return _emoji_open


def set_emoji_open(value):
    global _emoji_open
    with _emoji_lock:
        _emoji_open = bool(value)


# Monotonic deadline guarding the ONE Escape that on_key_emoji sends to close the
# OS emoji picker: a global pynput Esc listener closes the OSK on any Escape, so
# without this our own emoji-close Escape would also close the keyboard.
_suppress_esc_until = 0.0


def suppress_esc_close():
    """Arm a brief window in which the next Escape must NOT close the OSK 
    called right before on_key_emoji emits its picker-closing Escape."""
    global _suppress_esc_until
    with _emoji_lock:
        _suppress_esc_until = time.monotonic() + 0.6


def take_esc_close_suppressed():
    """True (once) if an emoji-close Escape was just sent, so the global Esc
    listener swallows this Escape instead of closing the OSK."""
    global _suppress_esc_until
    with _emoji_lock:
        if time.monotonic() < _suppress_esc_until:
            _suppress_esc_until = 0.0
            return True
        return False


def set_haptic_tick(fn):
    global _haptic_tick
    with _haptic_lock:
        _haptic_tick = fn


def set_pad_click_haptic(fn):
    global _pad_click_haptic
    with _haptic_lock:
        _pad_click_haptic = fn


def set_trigger_haptic(fn):
    global _trigger_haptic
    with _haptic_lock:
        _trigger_haptic = fn


def set_rumble_enabled(kind, value):
    """Enable/disable haptics for controller kind ("sc", "switch", "xbox", ...)."""
    kind = _canon_kind(kind)
    if kind is None:
        return
    with _haptic_lock:
        _rumble_enabled[kind] = bool(value)


def is_rumble_enabled(kind):
    """Whether haptics are on for controller kind ("sc", "switch", "xbox", ...).
    Unknown / never-configured kinds default to on."""
    kind = _canon_kind(kind)
    with _haptic_lock:
        return _rumble_enabled.get(kind, True)


# Steam Controller-only OSK settings (tray "Steam Controller" submenu, shown only
# while an SC is connected). Set on the tray thread, read on the input thread.
# Apply ONLY to the Steam Controller  controller.py gates them on
# get_active_controller() == "sc".
_sc_lock = Lock()
# Left-stick OSK cursor navigation. Off = the SC's left stick doesn't move the
# OSK cursor, so its firmware-lizard behavior (e.g. scrolling a page) passes
# through while the OSK is open. Default on.
_kbd_stick_nav = True
# Mouse + right-stick OSK interaction. Off = mouse highlights only the close X,
# cannot hover/click keys. Default on.
_kbd_mouse_nav = True
# OSK L2/R2 (Shift/Enter) actuation: None = firmware full-pull digital bit only
# (default); an int 0..32767 also engages Shift/Enter at that lighter analog pull.
_sc_osk_trigger_threshold = None
# Desktop L2/R2 mouse-click actuation (Options "Mouse Trigger Actuation")  a
# SEPARATE threshold from the OSK one above: None = firmware full-pull digital
# bit only; an int 0..32767 also clicks at that lighter analog pull.
_sc_mouse_trigger_threshold = None
# GAMEPAD-mode L2/R2 actuation (Options "Gamepad Mode Trigger Actuation")  a
# SEPARATE threshold again: the analog pull at which L2/R2 register as PRESSED
# for the virtual Xbox pad, so a trigger REBOUND to a digital button (or a
# keyboard action) in the Gamepad tab fires at this lighter pull instead of only
# the firmware full-pull bit. None = firmware full-pull only; int 0..32767 = that
# analog pull. Does not affect a trigger left as the analog LT/RT axis.
_sc_gamepad_trigger_threshold = None
# Right-stick → mouse pointer speed multiplier (tray "Pointer Speed"). 1.0 =
# the tuned default; <1 slower, >1 faster. Scales the base px/sec in the OSK
# right-stick mouse (controller.py) and the SC desktop mouse (tray _Watcher).
_sc_mouse_speed = 1.0
# Switch Pro mouse speed, driven by the tray "Switch Pro Controller" submenu.
_switch_mouse_speed = 1.0
# SC desktop-takeover trackpad speeds (tray "Steam Controller" submenu). 1.0 =
# tuned default. Right trackpad → cursor (_sc_trackpad_speed) and left trackpad →
# scroll wheel (_sc_scroll_speed); both scale the base sensitivity in tray's
# _Watcher pad handlers, which drive the pads directly when firmware lizard is off.
_sc_trackpad_speed = 1.0
_sc_scroll_speed = 1.0
# Left-trackpad scroll style (Options → Touchpads): "normal" = direct 1:1 wheel
# notches only; "laptop" = a quick swipe also sets the page coasting with a
# smooth deceleration (kinetic scrolling), caught with a gentle tap; "wheel" =
# the left pad is a circular scroll dial (clockwise down, ccw up, clicky notches);
# "wheel_smooth" = the same dial but a continuous hi-res analog glide (no ticks).
_SC_SCROLL_MODES = ("normal", "laptop", "wheel", "wheel_smooth")
_sc_scroll_mode = "normal"


def set_kbd_stick_nav(enabled):
    global _kbd_stick_nav
    with _sc_lock:
        _kbd_stick_nav = bool(enabled)


def is_kbd_stick_nav_enabled():
    with _sc_lock:
        return _kbd_stick_nav


def set_kbd_mouse_nav(enabled):
    global _kbd_mouse_nav
    with _sc_lock:
        _kbd_mouse_nav = bool(enabled)


def is_kbd_mouse_nav_enabled():
    with _sc_lock:
        return _kbd_mouse_nav


def is_kbd_stick_nav_enabled_for(_kind=None):
    # While the OS emoji picker is open, force "LStick/Mouse controls OFF"
    # (desktop) behavior: the right stick + L2/R2 act as a system mouse so the
    # picker can be point-and-clicked  it's otherwise unreachable. Restored when
    # the emoji key toggles the picker shut (or the OSK closes / reset_session).
    if is_emoji_open():
        return False
    return is_kbd_stick_nav_enabled()


def set_sc_mouse_speed(factor):
    global _sc_mouse_speed
    with _sc_lock:
        _sc_mouse_speed = float(factor)


def get_sc_mouse_speed():
    with _sc_lock:
        return _sc_mouse_speed


def set_switch_mouse_speed(factor):
    global _switch_mouse_speed
    with _sc_lock:
        _switch_mouse_speed = float(factor)


def get_switch_mouse_speed():
    with _sc_lock:
        return _switch_mouse_speed


def set_sc_trackpad_speed(factor):
    global _sc_trackpad_speed
    with _sc_lock:
        _sc_trackpad_speed = float(factor)


def get_sc_trackpad_speed():
    with _sc_lock:
        return _sc_trackpad_speed


def set_sc_scroll_speed(factor):
    global _sc_scroll_speed
    with _sc_lock:
        _sc_scroll_speed = float(factor)


def get_sc_scroll_speed():
    with _sc_lock:
        return _sc_scroll_speed


# "Video Timeline Scrubbing" (Options → Touchpads): while a video site/player
# is focused (YouTube for now), the left trackpad becomes a circular timeline
# dial instead of a scroll wheel  clockwise scrubs forward, counter-clockwise
# back. "off" disables it; "hover" = the cursor rides the progress bar so the
# player's hover preview follows the dial while the video keeps playing, and
# lifting clicks that spot to seek; "frame" = frame-by-frame precision (pauses
# on each frame, resumes on lift); "seek" = fast 5s-per-detent seeking (no
# pause). The tray watcher polls this per input frame.
_video_scrub_mode = "off"

# Virtual Menus (Options → Virtual Menus): the sanitized menu list plus a
# version counter  the SC watcher polls the version each frame (cheap int
# compare) and rebuilds its pad lookup / hides the overlay ON ITS OWN THREAD
# when the picker publishes a change (the overlay window is owned by the
# input thread, so cross-thread hides are never needed).
_virtual_menus = []
_virtual_menus_ver = 0


def set_virtual_menus(menus):
    global _virtual_menus, _virtual_menus_ver
    with _sc_lock:
        _virtual_menus = list(menus or [])
        _virtual_menus_ver += 1


def get_virtual_menus():
    with _sc_lock:
        return list(_virtual_menus)


def get_virtual_menus_version():
    with _sc_lock:
        return _virtual_menus_ver


def set_video_scrub_mode(mode):
    global _video_scrub_mode
    with _sc_lock:
        _video_scrub_mode = (mode if mode in ("off", "hover", "frame", "seek")
                             else "off")


def get_video_scrub_mode():
    with _sc_lock:
        return _video_scrub_mode


def is_video_scrub_enabled():
    with _sc_lock:
        return _video_scrub_mode != "off"


def set_sc_scroll_mode(mode):
    global _sc_scroll_mode
    with _sc_lock:
        _sc_scroll_mode = mode if mode in _SC_SCROLL_MODES else "normal"


def get_sc_scroll_mode():
    with _sc_lock:
        return _sc_scroll_mode


# Invert left-pad scrolling (Options → Touchpads scroll-settings cog modal):
# flips the scroll DIRECTION for ALL scroll modes (normal/laptop/wheel/
# wheel_smooth). Polled per emit in tray's _emit_scroll / _handle_pad_wheel.
_sc_scroll_invert = False


def set_sc_scroll_invert(enabled):
    global _sc_scroll_invert
    with _sc_lock:
        _sc_scroll_invert = bool(enabled)


def is_sc_scroll_invert_enabled():
    with _sc_lock:
        return _sc_scroll_invert


# "Text Wheel Selection" (Options → Touchpads): while the LEFT mouse button is
# held over text, circling the left pad nudges the cursor per detent so the
# live drag-selection extends ~one character at a time. Polled per input frame
# by the desktop takeover watcher.
_text_wheel_selection = False


def set_text_wheel_selection(enabled):
    global _text_wheel_selection
    with _sc_lock:
        _text_wheel_selection = bool(enabled)


def is_text_wheel_selection_enabled():
    with _sc_lock:
        return _text_wheel_selection


# Pinch To Zoom (Touchpads page): one finger on each SC pad zooms/pans the
# desktop via the Magnification API. Polled per input frame by the watcher.
_pinch_zoom = False
# Zoomed 360°-pan camera sensitivity (Touchpads slider under the toggle):
# 0..1 float, mapped 1:1 to _Watcher.LPAN_SENS (0.7 = the shipped default =
# the slider's 70% mark). Polled per input frame by _handle_pad_pan.
_pinch_sensitivity = 0.7


def set_pinch_zoom(enabled):
    global _pinch_zoom
    with _sc_lock:
        _pinch_zoom = bool(enabled)


def is_pinch_zoom_enabled():
    with _sc_lock:
        return _pinch_zoom


def set_pinch_sensitivity(value):
    global _pinch_sensitivity
    with _sc_lock:
        try:
            _pinch_sensitivity = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            pass


def get_pinch_sensitivity():
    with _sc_lock:
        return _pinch_sensitivity


# "Swipe Between Pages" (Touchpads page): a fast horizontal flick on the left
# pad = Back/Forward (browser, File Explorer, Settings)  the macbook page
# swipe. Polled per input frame by the watcher.
_swipe_pages = False


def set_swipe_pages(enabled):
    global _swipe_pages
    with _sc_lock:
        _swipe_pages = bool(enabled)


def is_swipe_pages_enabled():
    with _sc_lock:
        return _swipe_pages


# Per-direction Swipe Between Pages outputs (Touchpads cog modal): a picker
# action-vocabulary VALUE id (same Gamepad-tab list as a Hotkeys "Button
# Combo" output), resolved via keybinds_runtime.resolve_action + dispatched
# with _fire_guide_action. Defaults match the original hardcoded behavior
# (flick right = Back, flick left = Forward).
_swipe_right_output = "page_prev"
_swipe_left_output = "page_next"


def set_swipe_right_output(value):
    global _swipe_right_output
    with _sc_lock:
        _swipe_right_output = str(value) if value else "page_prev"


def get_swipe_right_output():
    with _sc_lock:
        return _swipe_right_output


def set_swipe_left_output(value):
    global _swipe_left_output
    with _sc_lock:
        _swipe_left_output = str(value) if value else "page_next"


def get_swipe_left_output():
    with _sc_lock:
        return _swipe_left_output


# "Right Touchpad Tap to Click" (Touchpads page): a quick, still touch-and-
# lift on the RIGHT pad = a left click  the laptop touchpad tap. Polled per
# input frame by the watcher.
_tap_to_click = False


def set_tap_to_click(enabled):
    global _tap_to_click
    with _sc_lock:
        _tap_to_click = bool(enabled)


def is_tap_to_click_enabled():
    with _sc_lock:
        return _tap_to_click


# "Left Touchpad Tap to Click" (Touchpads page): the left-pad twin of the
# above  a quick, still touch-and-lift on the LEFT pad fires a right click.
# Polled per input frame by the watcher.
_tap_to_click_left = False


def set_tap_to_click_left(enabled):
    global _tap_to_click_left
    with _sc_lock:
        _tap_to_click_left = bool(enabled)


def is_tap_to_click_left_enabled():
    with _sc_lock:
        return _tap_to_click_left


# "Release Touch To Type" (Touchpads page): while the on-screen keyboard is
# open, LIFTING the finger off a trackpad enters the key that pad was
# hovering  no pad click, no L2/R2 pull. Read per input frame by
# ControllerManager.handle_pad_input (touchpad kinds only; the SDL pads have
# no pads to hover with).
_release_to_type = False


def set_release_to_type(enabled):
    global _release_to_type
    with _sc_lock:
        _release_to_type = bool(enabled)


def is_release_to_type_enabled():
    with _sc_lock:
        return _release_to_type


# Which key layout the OSK builds (Options -> Keyboard -> Keyboard Layout):
#   "classic" the Steam-style full QWERTY
#   "phone"   the Android-style board, with its ?123 symbol pages
#   "full75"  the 75% board: function row, Ctrl/Alt/Win, all four arrows and
#             the hold-and-drag Select key
# Read when the keyboard opens, so a change applies to the next open.
OSK_LAYOUTS = ("classic", "phone", "full75")
_osk_layout = "classic"


def set_osk_layout(name):
    global _osk_layout
    name = str(name)
    with _sc_lock:
        _osk_layout = name if name in OSK_LAYOUTS else "classic"


def get_osk_layout():
    with _sc_lock:
        return _osk_layout


# Which page of a multi-page layout the OSK is showing. Only the phone layout
# has more than one ("abc" / "sym1" / "sym2", switched by its ?123, =\< and ABC
# keys); every classic layout has the single page "main". Reset on each open so
# the keyboard never comes back up on a symbol page.
_osk_page = "main"


def set_osk_page(name):
    global _osk_page
    with _sc_lock:
        _osk_page = str(name)


def get_osk_page():
    with _sc_lock:
        return _osk_page


# "Touch Typing" (Touchpads page): make each trackpad behave like the glass on
# a phone  a fixed 1:1 map of its own region of the on-screen keyboard. A
# fresh touch puts the pointer EXACTLY where the thumb landed instead of
# gliding there from wherever it was last, and lifting types that key.
_touch_typing = False


def set_touch_typing(enabled):
    global _touch_typing
    with _sc_lock:
        _touch_typing = bool(enabled)


def is_touch_typing_enabled():
    with _sc_lock:
        return _touch_typing


# "Swipe Typing" (Touchpads page): while the on-screen keyboard is open, drag a
# thumb across a trackpad through a word's letters and lift to type the whole
# word (see adusk/swipe.py). Read per input frame  it also decides the pad ->
# keyboard mapping, because tracing a word needs each pad to reach the WHOLE
# layout rather than its own half.
_swipe_typing = False


def set_swipe_typing(enabled):
    global _swipe_typing
    with _sc_lock:
        _swipe_typing = bool(enabled)


def is_swipe_typing_enabled():
    with _sc_lock:
        return _swipe_typing


# Live swipe trails, keyed by the tracing pad's touch-button mask. The input
# thread publishes the point list BY REFERENCE and keeps appending to it; the
# render thread copies it each frame to draw the tail behind the finger.
# Per-pad rather than a single trail so a thumb merely resting on the other pad
# cannot blank the trail being drawn.
_swipe_trails = {}
_swipe_trail_lock = Lock()


def set_swipe_trail(key, points):
    """Publish (or, with points=None, retract) one pad's live trail."""
    with _swipe_trail_lock:
        if points is None:
            _swipe_trails.pop(key, None)
        else:
            _swipe_trails[key] = points


def get_swipe_trails():
    """Snapshot of every live trail, each as its own tuple of points. Copying
    under the lock keeps the renderer off a list the input thread is still
    appending to."""
    with _swipe_trail_lock:
        if not _swipe_trails:
            return ()
        return tuple(tuple(p) for p in _swipe_trails.values())


def swipe_trail_len():
    """Total points across every live trail  for the render dirty-flag. A
    growing trail changes what is drawn even on a frame where nothing else did,
    and this answers that without copying any points."""
    with _swipe_trail_lock:
        if not _swipe_trails:
            return 0
        return sum(len(p) for p in _swipe_trails.values())


# Per-kind pointer-speed multipliers ("Right Joystick Sensitivity" in each
# controller's Options category). The SC and Switch keep their dedicated slots
# above; other kinds land here. Missing kinds fall back to the Switch value
# (the historical behavior for every SDL pad).
_kind_mouse_speed = {}


def set_kind_mouse_speed(kind, factor):
    kind = _canon_kind(kind)
    if kind is None:
        return
    if kind == "sc":
        set_sc_mouse_speed(factor)
        return
    if kind == "switch":
        set_switch_mouse_speed(factor)
        return
    with _sc_lock:
        _kind_mouse_speed[kind] = float(factor)


def get_mouse_speed_for(kind):
    """Pointer-speed multiplier for the active controller kind."""
    kind = _canon_kind(kind)
    if kind == "sc":
        return get_sc_mouse_speed()
    if kind == "switch" or kind is None:
        return get_switch_mouse_speed()
    with _sc_lock:
        speed = _kind_mouse_speed.get(kind)
    return speed if speed is not None else get_switch_mouse_speed()


def set_sc_osk_trigger_threshold(threshold):
    global _sc_osk_trigger_threshold
    with _sc_lock:
        _sc_osk_trigger_threshold = threshold


def get_sc_osk_trigger_threshold():
    with _sc_lock:
        return _sc_osk_trigger_threshold


def set_sc_mouse_trigger_threshold(threshold):
    global _sc_mouse_trigger_threshold
    with _sc_lock:
        _sc_mouse_trigger_threshold = threshold


def get_sc_mouse_trigger_threshold():
    with _sc_lock:
        return _sc_mouse_trigger_threshold


def set_sc_gamepad_trigger_threshold(threshold):
    global _sc_gamepad_trigger_threshold
    with _sc_lock:
        _sc_gamepad_trigger_threshold = threshold


def get_sc_gamepad_trigger_threshold():
    with _sc_lock:
        return _sc_gamepad_trigger_threshold


# Per-kind SDL-pad trigger actuation thresholds (each analog-trigger
# controller's Options category: Keyboard / Mouse / Gamepad Mode Trigger
# Actuation). Keyed (kind, which) with which in ("osk", "mouse", "gamepad");
# value None = default full-ish pull (the source's built-in threshold), int
# 0..32767 = engage at that analog pull. Consumed by
# inputsrc.Sdl3GamepadSource._read_pad (osk/gamepad) and the tray's
# _SdlDesktopController trigger clicks (mouse).
_sdl_trigger_thresholds = {}


def set_sdl_trigger_threshold(kind, which, threshold):
    kind = _canon_kind(kind)
    if kind is None or which not in ("osk", "mouse", "gamepad"):
        return
    global _sdl_trigger_thresholds
    with _sc_lock:
        m = dict(_sdl_trigger_thresholds)
        m[(kind, which)] = threshold
        _sdl_trigger_thresholds = m


def get_sdl_trigger_threshold(kind, which):
    kind = _canon_kind(kind)
    with _sc_lock:
        return _sdl_trigger_thresholds.get((kind, which))


# OSK function → SC control-id button map (Options "On Screen Keyboard" page).
# Picks which physical Steam Controller button drives each OSK action; the
# defaults reproduce the built-in mapping exactly (controller.py resolves the
# control ids to SCButtons bits). Live-applied from the picker / tray.
_OSK_BUTTON_DEFAULTS = {
    "caps": "l3", "shift": "l2", "enter": "r2", "space": "y", "backspace": "x",
}
_osk_buttons = dict(_OSK_BUTTON_DEFAULTS)


def set_osk_button(func, control_id):
    """Remap one OSK function ('caps'/'shift'/'enter'/'space'/'backspace') to a
    SC control id (e.g. 'l3', 'y'). Unknown funcs are ignored."""
    global _osk_buttons
    with _sc_lock:
        if func in _OSK_BUTTON_DEFAULTS:
            m = dict(_osk_buttons)
            m[func] = control_id
            _osk_buttons = m


def set_osk_buttons(mapping):
    """Replace the whole OSK button map (any missing func keeps its default)."""
    global _osk_buttons
    with _sc_lock:
        m = dict(_OSK_BUTTON_DEFAULTS)
        if mapping:
            for k, v in mapping.items():
                if k in _OSK_BUTTON_DEFAULTS and v:
                    m[k] = v
        _osk_buttons = m


def get_osk_buttons():
    with _sc_lock:
        return dict(_osk_buttons)


# PER-CONTROLLER OSK button maps (each controller's Options category has its
# own Caps/Shift/Enter/Space/Backspace dropdowns). {kind: {func: control_id}};
# a kind with no entry uses the flat map above (which doubles as the Steam
# Controller's map for legacy settings files).
_osk_buttons_by_kind = {}


def set_osk_buttons_for(kind, mapping):
    """Set one controller kind's OSK function → control-id map (missing funcs
    keep their defaults)."""
    global _osk_buttons_by_kind
    kind = _canon_kind(kind)
    if kind is None:
        return
    m = dict(_OSK_BUTTON_DEFAULTS)
    if mapping:
        for k, v in mapping.items():
            if k in _OSK_BUTTON_DEFAULTS and v:
                m[k] = v
    with _sc_lock:
        by = dict(_osk_buttons_by_kind)
        by[kind] = m
        _osk_buttons_by_kind = by


def get_osk_buttons_for(kind):
    """The OSK function → control-id map for `kind`, falling back to the flat
    (legacy / SC) map when the kind has none of its own."""
    kind = _canon_kind(kind)
    with _sc_lock:
        m = _osk_buttons_by_kind.get(kind)
        return dict(m) if m is not None else dict(_osk_buttons)


# SC button bits that close the OSK  the controls bound to 'escape' in the SC
# desktop binds (B by default), published by the tray. Closing the OSK with a
# controller thus follows the Escape binding (configurable) instead of a
# hardcoded button. Empty until the tray publishes the real set.
_osk_close_buttons = frozenset()


def set_osk_close_buttons(bits):
    """Replace the set of SC button bits that close the OSK (ints)."""
    global _osk_close_buttons
    with _sc_lock:
        _osk_close_buttons = frozenset(int(b) for b in (bits or ()))


def get_osk_close_buttons():
    with _sc_lock:
        return _osk_close_buttons


# Which gamepad button, while the matching touchpad is touched, inputs the
# highlighted key under the finger (control-id strings like "l2"/"r2"/"a",
# resolved to SCButtons bits by controller.py). Defaults reproduce the built-in
# L2 (left) / R2 (right) trigger behaviour.
_lpad_click_button = "l2"
_rpad_click_button = "r2"


def set_lpad_click_button(control_id):
    global _lpad_click_button
    with _sc_lock:
        _lpad_click_button = control_id or "l2"


def set_rpad_click_button(control_id):
    global _rpad_click_button
    with _sc_lock:
        _rpad_click_button = control_id or "r2"


def get_lpad_click_button():
    with _sc_lock:
        return _lpad_click_button


def get_rpad_click_button():
    with _sc_lock:
        return _rpad_click_button


def haptic_tick():
    """Fire the registered haptic-feedback hook, if any. Safe to call from the
    main thread; swallows errors so feedback never breaks key dispatch. The tick
    fans out to the ACTIVE controller, so it's gated by that controller's
    Vibration toggle."""
    kind = get_active_controller()
    with _haptic_lock:
        if not _rumble_enabled.get(kind, True):
            return
        fn = _haptic_tick
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


def pad_click_haptic():
    """Fire the stronger physical-pad-click hook (press/release of the
    simulated trackpad click). Falls back to the normal tick if no dedicated
    hook is registered. Gated by the active controller's Vibration toggle like
    haptic_tick()."""
    kind = get_active_controller()
    with _haptic_lock:
        if not _rumble_enabled.get(kind, True):
            return
        fn = _pad_click_haptic or _haptic_tick
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


def trigger_haptic():
    """Fire the L2/R2 trigger-actuation hook: the "click" feedback for a
    trigger crossing its actuation point. The per-source routing buzzes ONLY
    controllers with analog triggers (SC/Deck/Xbox/PS/handhelds)  a digital
    trigger (Switch ZL/ZR) clicks mechanically and stays silent. Falls back to
    the pad-click hook when unregistered (its historical behavior). Gated by
    the active controller's Vibration toggle like haptic_tick()."""
    kind = get_active_controller()
    with _haptic_lock:
        if not _rumble_enabled.get(kind, True):
            return
        fn = _trigger_haptic or _pad_click_haptic or _haptic_tick
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


class InputState(IntEnum):
    """How hard a pad-driven pointer is engaging whatever it sits on.

    Ordered by escalation, and the renderer leans on that ordering (a key paints
    at the highest state any pointer reports against it), so keep the values
    monotonic if this ever grows a fourth level.
    """

    INACTIVE = 0   # pointer parked; nothing under it is highlighted
    HOVER = 1      # pointer is over a key, no click yet
    CLICK = 2      # key is being pressed


_close_x_rect = None
_close_x_active = False
_close_x_lock = Lock()


def set_close_x_rect(rect):
    global _close_x_rect
    with _close_x_lock:
        _close_x_rect = tuple(rect) if rect is not None else None


def get_close_x_rect():
    with _close_x_lock:
        return _close_x_rect


def set_close_x_active(active):
    global _close_x_active
    with _close_x_lock:
        _close_x_active = bool(active)


def is_close_x_active():
    with _close_x_lock:
        return _close_x_active


# =============================================================================
#  The live VirtualKeyboard
# =============================================================================
# Published by the renderer every frame so the INPUT thread can hit-test key
# rects itself  which the press-to-focus lock, the accent-row hold and the
# Select key all need before the click ever reaches the main thread. A plain
# slot: the object is swapped wholesale on a layout/page change and only ever
# read here.
_virtual_kb = None


def set_virtual_kb(kb):
    global _virtual_kb
    _virtual_kb = kb


def get_virtual_kb():
    return _virtual_kb


# =============================================================================
#  Latched Ctrl / Alt  (the 75% layout's on-screen modifier keys)
# =============================================================================
# Mirrors the Shift latch above: clicking the on-screen Ctrl/Alt key holds the
# real OS modifier until it is clicked again, so the next key produces its
# Ctrl+/Alt+ combination. Each keeps its own latch rather than folding into
# _highlighted, because the renderer paints the highlight from the union of all
# three  toggling one must never clear the others.
_ctrl_latched = False
_ctrl_latch_lock = Lock()
_alt_latched = False
_alt_latch_lock = Lock()


def is_ctrl_latched():
    with _ctrl_latch_lock:
        return _ctrl_latched


def set_ctrl_latched(value):
    global _ctrl_latched
    with _ctrl_latch_lock:
        _ctrl_latched = bool(value)


def is_alt_latched():
    with _alt_latch_lock:
        return _alt_latched


def set_alt_latched(value):
    global _alt_latched
    with _alt_latch_lock:
        _alt_latched = bool(value)


# Keycode names of the modifiers each latch holds. Plain strings because
# steamcontroller.uinput.Keys resolves KEY_* names lazily  keeping them here
# means the renderer doesn't have to import the injection layer.
_LATCH_KEYCODES = {
    "shift": ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"),
    "ctrl": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
    "alt": ("KEY_LEFTALT", "KEY_RIGHTALT"),
}


def get_latched_modifier_keys():
    """Keycodes of every currently LATCHED on-screen modifier.

    Distinct from get_highlighted(), which also carries physically-held
    buttons: a latch is a held STATE, so the renderer paints it pressed but
    must not run the press-pop animation on it (it would sit sunken for as
    long as the latch is on)."""
    keys = set()
    if is_shift_latched():
        keys.update(_LATCH_KEYCODES["shift"])
    if is_ctrl_latched():
        keys.update(_LATCH_KEYCODES["ctrl"])
    if is_alt_latched():
        keys.update(_LATCH_KEYCODES["alt"])
    return keys


# =============================================================================
#  Select mode  (the 75% layout's "Select" key: hold + drag to select text)
# =============================================================================
# True while a pad press / mouse drag is holding the on-screen Select key: OS
# Shift is held and horizontal travel fires Shift+Left/Right. The renderer
# paints the key pressed while this is on.
_select_active = False
_select_lock = Lock()


def is_select_active():
    with _select_lock:
        return _select_active


def set_select_active(value):
    global _select_active
    with _select_lock:
        _select_active = bool(value)


# =============================================================================
#  Split keyboard layout
# =============================================================================
# "Split Keyboard" (Options -> Keyboard): the board splits into left/right
# halves anchored to the screen edges with a transparent middle band, so each
# trackpad covers its own half and neither thumb has to reach across the body.
# Read by vkb's geometry (every row is laid out per half) and by screen's
# background pass (the band is cleared to alpha 0).
_split_layout = False
_split_layout_lock = Lock()


def set_split_layout(enabled):
    global _split_layout
    with _split_layout_lock:
        _split_layout = bool(enabled)


def is_split_layout_enabled():
    with _split_layout_lock:
        return _split_layout


# =============================================================================
#  Key hit assist  (expanded hit targets)
# =============================================================================
# Pixels of "grab radius" added around every key when a click/hover position is
# resolved (see vkb.find_key_expanded). Keys sit ~5-6 px apart, so a fast
# two-finger typist who misses by a hair otherwise lands on the neighbour or on
# nothing; expanding each rect and snapping to the nearest key edge fixes the
# near-miss without changing where a well-inside press lands. 0 disables it
# (exact rects  the pre-merge behaviour).
_hit_expand = 10
_hit_expand_lock = Lock()


def set_hit_expand(px):
    global _hit_expand
    try:
        px = int(px)
    except (TypeError, ValueError):
        return
    with _hit_expand_lock:
        _hit_expand = max(0, min(40, px))


def get_hit_expand():
    with _hit_expand_lock:
        return _hit_expand


# =============================================================================
#  Press-to-focus  (freeze the pointer on the key you are pressing)
# =============================================================================
# Pressing a trackpad down, or pulling L2/R2, drags the thumb a little  enough
# to slide off the key that was under the cursor. When enabled, crossing the
# press/pull threshold freezes the pointer on the selected key's CENTRE until
# the press is released, so the click always lands where it looked like it
# would.
#   _press_focus        master on/off
#   _pad_press_hold     raw pad force that engages the freeze
#   _pad_click_release  force the press must fall below to unfreeze
#   _pad_lock_glide     lowpass alpha for the glide onto the key centre
#   _trigger_focus_pull analog pull (0..32767) that engages it for L2/R2
_press_focus = True
_pad_press_hold = 2000
_pad_click_release = 1000
_pad_lock_glide = 0.35
_trigger_focus_pull = 16384
_press_focus_lock = Lock()


def set_press_focus(enabled):
    global _press_focus
    with _press_focus_lock:
        _press_focus = bool(enabled)


def is_press_focus_enabled():
    with _press_focus_lock:
        return _press_focus


def set_pad_press_focus(hold, release=None, glide_alpha=None):
    """Tune the pad-press freeze: `hold` is the raw force that engages it,
    `release` the force it unfreezes below, `glide_alpha` the lowpass used
    while gliding onto the key centre. None keeps the current value."""
    global _pad_press_hold, _pad_click_release, _pad_lock_glide
    with _press_focus_lock:
        if hold is not None:
            _pad_press_hold = max(0, int(hold))
        if release is not None:
            _pad_click_release = max(0, int(release))
        if glide_alpha is not None:
            _pad_lock_glide = max(0.01, min(1.0, float(glide_alpha)))


def get_pad_press_focus():
    """(hold, release, glide_alpha) for the pad-press freeze."""
    with _press_focus_lock:
        return _pad_press_hold, _pad_click_release, _pad_lock_glide


def set_trigger_focus_pull(pull):
    """Analog L2/R2 pull (0..32767) at which the pointer freezes on the key
    centre. The click itself still fires at the actuation threshold  this
    only decides when the aim locks."""
    global _trigger_focus_pull
    with _press_focus_lock:
        _trigger_focus_pull = max(0, min(32767, int(pull)))


def get_trigger_focus_pull():
    with _press_focus_lock:
        return _trigger_focus_pull


# =============================================================================
#  Diacritic variants  (hold a letter to pick its accented forms)
# =============================================================================
# Session tuple: (base_char, tuple(variants), index, rect, source).
#   index  -1 = "base" (nothing picked yet), >= 0 indexes `variants`
#   rect   (x, y, w, h) px of the candidate strip in window coords
#   source "pad" / "mouse" / "button"  which input opened it, so only that
#          input drives the highlight and the release commit
# None = no row open.
_diacritic = None
_diacritic_lock = Lock()

# Config, published by the tray and deliberately NOT cleared by reset_session:
# the merged per-locale variant map, the active locale, and the master switch.
_diacritic_variants = diacritics.merge_diacritic_maps(
    diacritics.DIACRITIC_VARIANTS)
_diacritic_locale = "en"
_diacritics_enabled = True
_diacritic_cfg_lock = Lock()


def open_diacritic(char, variants, rect, source):
    """Open the variant row for base letter `char`. Returns False (leaving no
    row open) when `rect` is None  a strip that cannot be clamped into the
    window (see diacritics.variant_row_rect)."""
    global _diacritic
    if rect is None:
        return False
    with _diacritic_lock:
        _diacritic = (str(char), tuple(str(v) for v in variants), -1,
                      tuple(int(v) for v in rect), str(source))
    return True


def close_diacritic():
    global _diacritic
    with _diacritic_lock:
        _diacritic = None


def is_diacritic_open():
    with _diacritic_lock:
        return _diacritic is not None


def get_diacritic():
    with _diacritic_lock:
        return _diacritic


def set_diacritic_index(index):
    global _diacritic
    with _diacritic_lock:
        if _diacritic is None:
            return
        _diacritic = (_diacritic[0], _diacritic[1], int(index),
                      _diacritic[3], _diacritic[4])


def get_diacritic_index():
    with _diacritic_lock:
        return _diacritic[2] if _diacritic is not None else -1


def get_diacritic_rect():
    with _diacritic_lock:
        return _diacritic[3] if _diacritic is not None else None


def get_diacritic_source():
    with _diacritic_lock:
        return _diacritic[4] if _diacritic is not None else None


def get_diacritic_variants_list():
    with _diacritic_lock:
        return _diacritic[1] if _diacritic is not None else ()


def get_diacritic_variant_count():
    with _diacritic_lock:
        return len(_diacritic[1]) if _diacritic is not None else 0


def get_diacritic_selected_char():
    """The highlighted variant, or None when the selection is still the base
    (index -1) or no row is open."""
    with _diacritic_lock:
        if _diacritic is None or _diacritic[2] < 0:
            return None
        return _diacritic[1][_diacritic[2]]


def set_diacritic_variants(mapping):
    """Publish the merged per-locale letter -> variants map (the tray merges
    the built-in fallback with any user override; the user wins per letter).
    Stored as a read-only snapshot: lookups run on the press edge, so
    get_diacritic_variants() must not deep-copy on every call."""
    global _diacritic_variants
    merged = diacritics.merge_diacritic_maps(mapping or {})
    with _diacritic_cfg_lock:
        _diacritic_variants = merged


def get_diacritic_variants():
    """The active variant map. Callers must treat it as READ-ONLY  it is the
    live snapshot, not a copy (set_diacritic_variants replaces it wholesale)."""
    with _diacritic_cfg_lock:
        return _diacritic_variants


def set_diacritic_locale(locale):
    """Publish the active locale for variant lookups. "auto" resolves against
    the system keyboard layout here, once, so the runtime only ever sees a
    concrete tag."""
    global _diacritic_locale
    tag = str(locale or "en").strip().lower()
    if tag in ("", "auto"):
        tag = diacritics.detect_system_locale() or "en"
    with _diacritic_cfg_lock:
        _diacritic_locale = tag


def get_diacritic_locale():
    with _diacritic_cfg_lock:
        return _diacritic_locale


def set_diacritics_enabled(enabled):
    global _diacritics_enabled
    with _diacritic_cfg_lock:
        _diacritics_enabled = bool(enabled)


def is_diacritics_enabled():
    with _diacritic_cfg_lock:
        return _diacritics_enabled


# =============================================================================
#  Key press / open / close sounds
# =============================================================================
# Steam's own on-screen-keyboard audio, played through hooks the tray registers
# (adusk/key_sound.py resolves the wavs from the Steam install). Kept as hooks
# rather than a direct import so the OSK core never depends on the audio
# backend  a build without it simply stays silent.
_key_sound = None
_key_sound_open = None
_key_sound_close = None
_key_sound_enabled = True
_key_sound_lock = Lock()


def set_key_sound(fn):
    global _key_sound
    with _key_sound_lock:
        _key_sound = fn


def set_key_sound_open(fn):
    global _key_sound_open
    with _key_sound_lock:
        _key_sound_open = fn


def set_key_sound_close(fn):
    global _key_sound_close
    with _key_sound_lock:
        _key_sound_close = fn


def set_key_sound_enabled(enabled):
    global _key_sound_enabled
    with _key_sound_lock:
        _key_sound_enabled = bool(enabled)


def is_key_sound_enabled():
    with _key_sound_lock:
        return _key_sound_enabled


def _fire_sound(which):
    with _key_sound_lock:
        if not _key_sound_enabled:
            return
        fn = {"key": _key_sound, "open": _key_sound_open,
              "close": _key_sound_close}[which]
    if fn is None:
        return
    try:
        fn()
    except Exception:
        pass


def key_sound_tick():
    """One keyboard click. Fired from dispatch_key  the single choke point
    every input path (pad, button, mouse) funnels through."""
    _fire_sound("key")


def key_sound_open():
    _fire_sound("open")


def key_sound_close():
    _fire_sound("close")


# =============================================================================
#  Per-app OSK memory  (position / size / skin, keyed by foreground exe)
# =============================================================================
# "Remember Per App" (Options -> Keyboard): each foreground app reopens the
# keyboard with the spot, size and skin it was last left at while that app was
# focused. Apps with no entry fall back to the global settings. The maps are
# owned by the tray (they live in settings.json); these slots are the live
# copies the OSK reads, plus a write queue the tray drains and persists.
_per_app_enabled = False
_position_per_app = {}
_size_per_app = {}
_skin_per_app = {}
_per_app_writes = deque()
_per_app_lock = Lock()


def set_per_app_memory(enabled):
    global _per_app_enabled
    with _per_app_lock:
        _per_app_enabled = bool(enabled)


def is_per_app_memory_enabled():
    with _per_app_lock:
        return _per_app_enabled


def set_per_app_maps(position=None, size=None, skin=None):
    """Publish the saved per-app maps (each {exe name lowercase: value})."""
    global _position_per_app, _size_per_app, _skin_per_app
    with _per_app_lock:
        if position is not None:
            _position_per_app = dict(position)
        if size is not None:
            _size_per_app = dict(size)
        if skin is not None:
            _skin_per_app = dict(skin)


def get_per_app_position(exe):
    """The remembered 0-5 window slot for `exe`, or None."""
    if exe is None:
        return None
    with _per_app_lock:
        if not _per_app_enabled:
            return None
        stored = _position_per_app.get(exe)
    if stored is None:
        return None
    try:
        return int(stored) % 6
    except (TypeError, ValueError, OverflowError):
        return None


def get_per_app_size(exe):
    """The remembered OSK size name for `exe`, or None."""
    if exe is None:
        return None
    with _per_app_lock:
        if not _per_app_enabled:
            return None
        stored = _size_per_app.get(exe)
    return stored if stored in ("small", "medium", "full") else None


def get_per_app_skin(exe):
    """The remembered skin name for `exe`, or None."""
    if exe is None:
        return None
    with _per_app_lock:
        if not _per_app_enabled:
            return None
        stored = _skin_per_app.get(exe)
    return stored if isinstance(stored, str) and stored.strip() else None


def note_per_app(exe, kind, value):
    """Record that `exe` should reopen with `value` for `kind` ("position" /
    "size" / "skin"). Updates the live map and queues the write for the tray
    to persist. No-op while the feature is off, or when the foreground is not
    a positionable app (exe None)."""
    if exe is None or kind not in ("position", "size", "skin"):
        return
    with _per_app_lock:
        if not _per_app_enabled:
            return
        {"position": _position_per_app, "size": _size_per_app,
         "skin": _skin_per_app}[kind][exe] = value
        _per_app_writes.append((exe, kind, value))


def drain_per_app_writes():
    """Pop every queued per-app write for the tray to persist."""
    out = []
    with _per_app_lock:
        while _per_app_writes:
            out.append(_per_app_writes.popleft())
    return out
