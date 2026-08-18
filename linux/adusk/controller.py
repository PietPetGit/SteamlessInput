import copy
import time
from collections import deque
from threading import Lock

import steamcontroller.uinput as sui
from steamcontroller import SCButtons, SCStatus, SCI_NULL, GYRO_DEG_PER_SEC
from steamcontroller.events import EventMapper

from adusk import inputsrc
from adusk import screen
from adusk.screen import CoordFraction
from adusk import diacritics
from adusk import state
from adusk import swipe as swipe_typing
from adusk import utils
from adusk import vkb
from adusk import vptr

# In-process bridge to the keybinds picker's controller-nav channel. Used ONLY
# during a live Options-tab preview: while the preview OSK holds the SC (the
# tray paused its own sc.run()), we forward frames here so the picker can keep
# navigating. Optional  absent in the standalone lockscreen OSK build.
try:
    import sc_viewer as _sc_viewer
except Exception:
    _sc_viewer = None


# OSK function-remap: control id (picker / state.get_osk_buttons) → SCButtons bit.
# Lets the Options "On Screen Keyboard" page rebind Caps/Shift/Enter/Space/
# Backspace to any SC button. Defaults: caps=L3, shift=L2(LT), enter=R2(RT),
# space=Y, backspace=X (see handle_input).
_OSK_CTRL_BITS = {
    "a": SCButtons.A, "b": SCButtons.B, "x": SCButtons.X, "y": SCButtons.Y,
    "l1": SCButtons.LB, "r1": SCButtons.RB,
    "l2": SCButtons.LT, "r2": SCButtons.RT,
    "l3": SCButtons.L3, "r3": SCButtons.R3,
    "l4": SCButtons.LGRIP1, "l5": SCButtons.LGRIP2,
    "r4": SCButtons.RGRIP1, "r5": SCButtons.RGRIP2,
    "start": SCButtons.START, "back": SCButtons.VIEW,
    "dpad_up": SCButtons.DPAD_UP, "dpad_down": SCButtons.DPAD_DOWN,
    "dpad_left": SCButtons.DPAD_LEFT, "dpad_right": SCButtons.DPAD_RIGHT,
}


def _ctrl_bit(ctrl_id, default=0):
    """Look up a control id in _OSK_CTRL_BITS. Returns 0 for "none"/None,
    `default` when the id is not in the table."""
    if not ctrl_id or ctrl_id == "none":
        return 0
    return _OSK_CTRL_BITS.get(ctrl_id, default)


class ControllerState:
    """Hand-off point between the input thread and the render loop.

    The input thread writes both pad pointers here every frame; the render loop
    reads them and drains `click_queue`. Everything is per-instance: the tray
    keeps one process alive across many OSK sessions, and class-level state
    would carry a previous session's queued clicks into the next one.
    """

    def __init__(self):
        self.click_queue = deque()
        self._pointers = None
        self._pointer_lock = Lock()

    def set_pointers(self, ptr_left, ptr_right):
        with self._pointer_lock:
            self._pointers = (ptr_left, ptr_right)

    def get_pointers(self):
        """A snapshot the render loop can read while input keeps mutating."""
        with self._pointer_lock:
            return copy.deepcopy(self._pointers)


# Right-stick-as-mouse tuning (shared by every controller while the OSK is
# open). Mirrors the tray's desktop-mode cursor feel.
_MOUSE_DEADZONE = 6000
_MOUSE_SPEED = 1400.0       # px/sec at full stick deflection
# Bigger exponent = longer ramp (more stick travel maps to slow speeds), so
# precise control needs less surgical thumb precision. Matches the tray _Watcher.
_MOUSE_EXPONENT = 5.0
# Minimum speed (fraction of full) the instant the stick passes the deadzone, so
# the first bit of travel moves a usable amount (>1px/frame) instead of the
# near-zero the steep exponent gives  fine control needs perceptible feedback.
_MOUSE_MIN = 0.05


def _mouse_vector(x, y, deadzone, exponent):
    """Radial stick → mouse velocity (vx, vy), each component scaled 0..1. Speed
    is a function of the stick's DISTANCE from center applied to the unit
    direction  NOT per-axis  so a diagonal full push moves at the same speed as
    a pure horizontal/vertical push. (Applying the exponent per-axis made
    diagonals ~radius·exp slower, very visible at high exponents.)"""
    mag = (x * x + y * y) ** 0.5
    if mag <= deadzone:
        return 0.0, 0.0
    m = min(1.0, (mag - deadzone) / (32767.0 - deadzone))
    # Floor + ramp: a small flat minimum the moment we pass the deadzone, then the
    # m**exponent curve on top (the floor only matters near center, where m**exp≈0).
    unit = _MOUSE_MIN + (1.0 - _MOUSE_MIN) * (m ** exponent)
    scaled = unit / mag  # `unit` speed along the unit vector (x/mag, y/mag)
    return x * scaled, y * scaled


# Horizontal reach of each trackpad across the keyboard, in design pixels at the
# base 1286px layout (converted to fractions below so every OSK size 
# "small"/"medium"/"full"  scales the reach with it). Both ends are measured
# from the pad's OWN side of the keyboard: the left pad from the left edge, the
# right pad from the right edge.
_PAD_DESIGN_W = 1286.0
# Inner end: the pad's inner edge puts the thumb circle's center this far in.
_PAD_REACH_PX = 704.0
# Outer end: the pad's outer edge parks the circle 2px PAST that side, so it sits
# a hair over halfway off the keyboard (negative = beyond the edge).
_PAD_EDGE_PX = -2.0
# raw_x/abs_max covers ±1/4, so the scalar is 2x the pad's total span; each pad
# is then centered at the midpoint of its own span.
_PAD_X_SCALAR = 2 * (_PAD_REACH_PX - _PAD_EDGE_PX) / _PAD_DESIGN_W
_LPAD_X_CENTER = (_PAD_EDGE_PX + _PAD_REACH_PX) / 2 / _PAD_DESIGN_W
_RPAD_X_CENTER = 1.0 - _LPAD_X_CENTER
# "Swipe Typing" reach: raw_x spans +-1/4 of abs_max, so a scalar of exactly 2
# centred at 1/2 maps each pad's FULL width onto the FULL keyboard width.
#
# The feature needs this. A word's letters are scattered across the whole
# layout  "hello" runs from 'e' on the left to 'l' on the right  while each
# pad normally reaches only its own ~55% (_PAD_REACH_PX of _PAD_DESIGN_W), so
# no single thumb could trace one. While the toggle is on, BOTH pads therefore
# address the whole keyboard: the pointer, the hover highlight, tapping and the
# swipe all move onto the same mapping at once, so what the user aims at is
# always what gets recorded and there is never a moment where the highlight
# jumps. The cost is that a key is ~1.8x smaller in thumb travel, which shape
# writing is explicitly built to tolerate  and precise tapping is what the
# toggle being OFF is for.
_SWIPE_X_SCALAR = 2.0
_SWIPE_X_CENTER = 0.5


def adjust_raw_x(raw_x, center_fraction, scalar=_PAD_X_SCALAR):
    """Raw pad X -> canvas pixel column.

    `raw_x` runs +/-0x20000 full scale. It is normalised to a signed fraction of
    that range, stretched by `scalar`, offset from `center_fraction` (where on
    the canvas this pad's centre sits), then taken up to pixels. Keep the
    (scalar * raw) / max grouping: the pads' 704px reach is calibrated against
    it and regrouping shifts the result by a rounding step at the extremes.
    """
    abs_max = 0x20000
    fraction = center_fraction + (scalar * raw_x) / abs_max
    return utils.round_to_int(screen.width * fraction)


def adjust_raw_x_span(raw_x, span_start, span_end):
    """Raw pad X -> canvas pixel column across the fixed interval
    [span_start, span_end].

    Split layout uses this so each pad covers exactly its OWN half's key span
     the left pad [0, band_left], the right pad [band_right, width]  rather
    than half of a display that is mostly transparent middle band. The raw X is
    normalised against the TRUE +/-0x8000 full scale here, not the +/-0x20000
    the overshooting whole-board mapping above uses: squeezing the pad's travel
    into a quarter of its span would leave the outer keys unreachable.
    """
    abs_max = 0x8000
    fraction = (raw_x + abs_max) / (2 * abs_max)
    x = span_start + fraction * (span_end - span_start)
    return utils.round_to_int(utils.clamp(x, 0, screen.width))


def adjust_raw_y(raw_y, center_fraction, scalar=6/5):
    """Raw pad Y -> canvas pixel row. Negated: the pads count up, screens down."""
    abs_max = 0x10000
    fraction = center_fraction + (scalar * -raw_y) / abs_max
    return utils.round_to_int(screen.height * fraction)


class ControllerManager:
    pad_smoothing = 0.15
    sc_input_previous = SCI_NULL
    # Grace window after the OSK opens during which a Steam(/Home) release is NOT
    # treated as a "close" gesture. Covers the Steam Controller HID handoff: the
    # OSK appears a beat before its SteamHidSource re-acquires the controller, so
    # the merged Steam reads released first (clearing the open seed below), then
    # the SC reconnects still carrying the open chord's lingering Steam  whose
    # release would otherwise instantly close the just-opened keyboard (~0.5 s).
    # The Switch Pro has no such gap (its SDL frames are already live, so its
    # seed holds), but the grace is harmless for it too.
    _OPEN_CLOSE_GRACE = 1.0

    def __init__(self, controller_state):
        self.controller_state = controller_state

        prev_ptrs = controller_state.get_pointers()
        self.prev_ptr_left = prev_ptrs[0]
        self.prev_ptr_right = prev_ptrs[1]

        # Steam+X / Steam-alone chord tracking. Seed both TRUE: the OSK was just
        # opened by a Steam(+X) chord that may still be held on the first frame 
        # most visibly on SDL pads (Switch Pro: Home+Y), where the OSK appears a
        # beat after the chord, by which point X (Y) is released but Steam (Home)
        # often isn't. Seeding _steam_was_pressed=True stops the first frame from
        # treating that lingering Steam as a fresh press, and _saw_x_during_steam=
        # True marks the opening chord as "used" so the tail of its Steam release
        # doesn't immediately close the keyboard. A later, deliberate Steam tap
        # still closes normally (its own rising edge re-clears the flag).
        self._steam_was_pressed = True
        self._saw_x_during_steam = True
        # When the OSK opened, for _OPEN_CLOSE_GRACE (the Steam-release auto-close
        # is suppressed until this elapses, so the SC reconnect blip can't close).
        self._open_t = time.monotonic()

        # --- Select key (hold + drag to select text) -------------------
        # Which pad (its repeat key) currently owns the held Select key, and
        # the raw pad-x the horizontal drag is measured from. Only the pad
        # that pressed Select drives the selection.
        self._select_pad = None
        self._select_anchor_x = 0.0
        self._select_base_dir = 0
        self._select_reverse = []
        self._select_touching = False

        # --- Accent row (hold a letter for its variants) ---------------
        # Which pad opened a variant row via a held press, and the per-pad
        # {repeat key: CoordFraction} of a variant-capable letter that was
        # pressed but deliberately NOT typed yet (see _should_defer_press).
        self._diacritic_pad = None
        self._deferred_base = {}
        # Earliest time each pad may fire another "enter the key" edge  the
        # click-bounce debounce (see PAD_CLICK_SETTLE).
        self._click_settle_at = {}

        # --- Press-to-focus ---------------------------------------------
        # Per-side frozen pointer target while a press/pull holds the aim on
        # one key, or absent while the finger tracks freely.
        self._focus_lock = {}

        self.evm = EventMapper()
        self._map_events()

    def _map_events(self):
        # Face buttons whose action is unconditional ride EventMapper. The
        # conditional bindings (LT/RT switch role while the same-side touchpad
        # is being touched) and the latching ones (L3, B, LGRIP, A, DPAD) are
        # handled manually in handle_input.
        # X → Backspace and Y → Space are handled manually below (X so it can
        # hold-to-repeat; Y so Space is remappable via the OSK button map). The
        # R4 back paddle → Space built-in is also handled manually (in the Space
        # block) so reassigning R4 via the OSK dropdowns can override it.

        # Rising-edge latches for manually-handled buttons.
        self._l3_was_pressed = False
        self._caps_was_pressed = False   # OSK Caps Lock button (default L3)
        self._space_was_pressed = False  # OSK Space button (default Y)
        self._shift_btn_prev = False     # OSK Shift button when not the LT trigger
        self._space_active = False       # Space held on the OS side (default Y)
        self._lgrip_was_pressed = False
        self._osk_close_was_pressed = False
        self._a_was_pressed = False
        # Defer model: the cell an A press landed on when that key has accented
        # variants, so nothing was typed at the press edge and the release
        # decides base vs variant. None when no press is deferred.
        self._a_deferred_cell = None
        # A-button (press key under cursor) hold-to-repeat clock, same cadence
        # as X; the main thread only repeats it over Backspace.
        self._a_repeat_at = 0.0
        self._view_was_pressed = False    # VIEW / "-" (Steam+VIEW Alt+Tab + position-cycle)
        self._alt_held_for_tab = False
        self._dpad_prev = 0
        # X (Backspace) hold-to-repeat: deletes once on press, then slow-repeats
        # while held. _x_repeat_at is the monotonic time of the next repeat.
        self._x_was_pressed = False
        self._x_repeat_at = 0.0
        # Same hold-to-repeat for the pad "enter the key" action (L2/R2 trigger
        # or a physical pad click): while held, re-enter the key on the same
        # BACKSPACE clock. Keyed per pad (by select-button mask) so the left and
        # right pads keep independent timers. The main thread only repeats a hit
        # that lands on Backspace, so holding rubs out text like the X button.
        self._click_repeat_at = {}
        # "Release Touch To Type" (Options → Touchpads): per pad (keyed by
        # touch-button mask) the LAST coordinate seen while the finger was
        # actually down, plus whether that same touch already entered a key by
        # clicking. The lift frame's pad coords are stale  the pad reports a
        # rest value the instant the touch bit clears  so the key has to come
        # from the last touched sample, and a touch that already typed must not
        # type again on the way up.
        self._release_coord = {}
        self._release_typed = {}
        # "Swipe Typing": the path currently being traced by each pad (keyed by
        # touch-button mask) in keyboard pixels, and whether that trace has
        # been poisoned  a pad click or click-trigger mid-gesture means the
        # user is deliberately entering ONE key, which is not a word.
        self._swipe_pts = {}
        self._swipe_bad = {}
        # Touchpad-click trigger (configurable per pad, default L2/R2). "Click
        # mode" is latched on the trigger's rising edge from whether the pad was
        # being touched then: touched → clicks the highlighted key (and the
        # button's normal OSK action is suppressed); untouched → the button keeps
        # its normal action. Latched until the trigger releases so sliding the
        # finger can't flip it.
        self._l_click_prev = False
        self._r_click_prev = False
        self._lpad_click_mode = False
        self._rpad_click_mode = False
        # Steam + left-stick media chords: track the stick's current direction
        # zone (edge-triggered) and the next allowed repeat time for volume.
        self._stick_zone_prev = "NEUTRAL"
        self._stick_repeat_at = 0.0
        # Left stick → keyboard cursor navigation (when Steam is NOT held).
        # Separate zone/repeat state from the media chord so the two stick
        # roles don't clobber each other's edge tracking.
        self._kbd_stick_zone_prev = "NEUTRAL"
        self._kbd_stick_repeat_at = 0.0
        self._kbd_scroll_at = 0.0  # next left-stick scroll tick (nav-off mode)
        self._kbd_scroll_zone_prev = "NEUTRAL"  # arrow-stick zone for the scroll
        # Fire a single haptic "open" tick on the first input frame.
        self._open_tick_pending = True
        # Steam-hold suppression of firmware lizard (kb/mouse)  see comment
        # in handle_input below.
        self._passive_lizard_suppressed = False
        self._last_lizard_suppress = 0.0
        # Tracks whether we are currently holding KEY_LEFTSHIFT / KEY_ENTER on
        # the OS side (driven by LT/RT but gated by touchpad contact).
        self._shift_active = False
        self._enter_active = False
        # In "control desktop" mode (Sticks Control Keyboard OFF) L2/R2 act as
        # the left/right MOUSE buttons instead of Shift/Enter  unless the pad is
        # being touched, where they keep the OSK key-press role. Track the held
        # state so the button mirrors the trigger (press/release, drag).
        self._mouse_l_active = False
        self._mouse_r_active = False
        self._kb = sui.Keyboard()
        # Right-stick-as-mouse while the OSK is open. Works for ANY controller in
        # the merged frame (Steam Controller, Switch Pro, Xbox, ...): the right
        # stick moves the system cursor so you can point-and-click the keys (or
        # anything else) without closing the keyboard. The right stick is unused
        # by the OSK otherwise (it navigates via the left stick / DPAD / pads).
        self._mouse = sui.Mouse()
        self._mouse_acc_x = 0.0
        self._mouse_acc_y = 0.0
        self._mouse_last_t = 0.0
        # "Gyro To Mouse" OSK pointer: while toggled on for the active
        # controller, the gyro steers a SYNTHESIZED right-trackpad touch, so
        # every kind gets the Steam Deck trackpad thumb-circle cursor over the
        # keys (see _handle_gyro_osk). _gyro_frac is the virtual position as a
        # fraction of the OSK surface, or None while the pointer is parked.
        self._gyro_frac = None
        self._gyro_last_t = 0.0
        self._gyro_chord_latch = False
        # "Always Type With Gyro" (Options → Keyboard) for THIS keyboard
        # session: None until the first frame, then True while the setting
        # holds gyro typing on and False once the gyro hotkey turns it off.
        # Session-local on purpose  see state._kbd_gyro_always.
        self._gyro_always_on = None
        # L3+R3 (both thumbsticks in) while the OSK is open always toggles
        # gyro typing, independent of the configured gyro hotkey/mode.
        self._l3r3_gyro_latch = False

    def _pointer_on_close_x(self):
        if state.is_close_x_active():
            return True
        cx = state.get_close_x_rect()
        if cx is None:
            return False
        x, y, w, h = cx
        for p in self.controller_state.get_pointers():
            if p.state != state.InputState.INACTIVE:
                px, py = p.coord_frac.to_absolute()
                if x <= px <= x + w and y <= py <= y + h:
                    return True
        return False

    @staticmethod
    def _click_select_haptic(bit):
        """Feedback for a button-driven "enter the key"/engage: L2/R2 route
        through the trigger haptic (per-kind  only analog-trigger controllers
        buzz, since the rumble stands in for the click they lack; the Switch's
        digital ZL/ZR click on their own and stay silent), any other button
        keeps the strong pad-click tick."""
        if bit & (SCButtons.LT | SCButtons.RT):
            state.trigger_haptic()
        else:
            state.pad_click_haptic()

    # Horizontal pad travel (raw units) that fires one Shift+Left/Right while
    # the Select key is held. The pad reads +/-0x8000 edge to edge, so this
    # step selects ~16 characters across a full sweep: a nudge picks out a few
    # characters precisely, a long drag reaches across a word or a line.
    SELECT_DRAG_STEP = 0x1000
    # Lift-off roll-back window. Every reverse arrow (one fired against the
    # drag's prevailing direction) fires IMMEDIATELY, 1:1, so back-and-forth
    # micro-adjustments never get swallowed. But a thumb rolls back ~50-120 ms
    # before it actually leaves the pad, and that roll-back would shrink the
    # selection the user just made. Reverse arrows are therefore buffered with
    # a timestamp: if the touch drops while one is still this fresh, it is
    # cancelled with a single compensating arrow. Older reverse arrows have
    # been on screen long enough to be deliberate and are left alone.
    SELECT_ROLLBACK_WINDOW = 0.12
    # Minimum gap between two "enter the key" edges on one pad. A physical pad
    # click can bounce  the force dips and re-crosses within a few ms of the
    # mechanical click  which would otherwise insert the key twice. The window
    # stays far shorter than a real retype (~100 ms even hammering), so a fast
    # deliberate double-tap still fires twice.
    PAD_CLICK_SETTLE = 0.05

    def _key_at(self, coord_frac):
        """The key under a pad coordinate, resolved on the INPUT thread with
        the same grab radius the click itself will use  so what the hold sees
        and what the click types can never disagree."""
        kb = state.get_virtual_kb()
        if kb is None:
            return None
        x, y = coord_frac.to_absolute()
        return kb.find_key_expanded(x, y)

    def _cell_has_variants(self, row, col):
        """True when the key at (row, col) has accented variants, so a press
        on it must defer its base letter until the release."""
        kb = state.get_virtual_kb()
        if kb is None:
            return False
        if not (0 <= row < len(kb.keys) and 0 <= col < len(kb.keys[row])):
            return False
        return vkb.diacritic_variants_for_key(kb.keys[row][col]) is not None

    def _is_select_key(self, coord_frac):
        key = self._key_at(coord_frac)
        return key is not None and key.is_select

    def _should_defer_press(self, coord_frac):
        """True when this press must NOT type at the press edge: it landed on a
        letter that has accented variants, so holding opens its variant row and
        the release decides base vs variant. A quick tap of such a key still
        types the base  just on release instead. Every other key keeps firing
        immediately."""
        if not state.is_diacritics_enabled():
            return False
        key = self._key_at(coord_frac)
        return key is not None and vkb.diacritic_variants_for_key(key) is not None

    def _try_open_diacritic(self, coord_frac, repeat_key):
        """The press on this pad has been held past the repeat delay over a
        letter with variants  open its row instead of letting the hold fire a
        meaningless key repeat. True if a row opened, after which the pad
        watches the press for the release commit and the finger for the
        highlight."""
        if not state.is_diacritics_enabled() or state.is_diacritic_open():
            return False
        kb = state.get_virtual_kb()
        if kb is None:
            return False
        x, y = coord_frac.to_absolute()
        if not vkb.open_diacritic_at(kb, x, y, "pad"):
            return False
        self._diacritic_pad = repeat_key
        return True

    def _end_diacritic_pad(self):
        """The press holding this pad's variant row released. Nothing was typed
        at the press edge, so a highlighted variant is committed as-is; a
        release with nothing highlighted commits the FIRST variant, because
        "while the row is open only accented letters are selectable" is the
        model the row presents  falling back to the base would retype the
        letter the user is already holding, which reads as nothing happening.

        The latch reset is unconditional: whichever branch or exception fires,
        this pad must never stay routed into row handling, or a stuck latch
        would silently swallow every later press on it."""
        repeat_key = self._diacritic_pad
        try:
            self._deferred_base.pop(repeat_key, None)
            char = state.get_diacritic_selected_char()
            if char is None:
                variants = state.get_diacritic_variants_list()
                char = variants[0] if variants else None
            if char is not None:
                self.controller_state.click_queue.append(("variant", char))
            else:
                state.close_diacritic()
        finally:
            self._diacritic_pad = None
            self._click_repeat_at.pop(repeat_key, None)

    def _fire_arrow(self, dirn):
        """Tap Shift+Right (dirn > 0) or Shift+Left once. Shift is already held
        for the whole select session."""
        key = sui.Keys.KEY_RIGHT if dirn > 0 else sui.Keys.KEY_LEFT
        self._kb.pressEvent([key])
        self._kb.releaseEvent([key])

    def _prune_select_reverse(self, now):
        cutoff = now - self.SELECT_ROLLBACK_WINDOW
        while self._select_reverse and self._select_reverse[0][0] < cutoff:
            self._select_reverse.pop(0)

    def _end_select(self, now):
        """Tear down a select session (press released, or finger lifted).
        Cancels any reverse arrow still inside the roll-back window  one
        compensating arrow each, which puts the selection back exactly where
        the drag stopped  then drops Shift and clears the session."""
        self._prune_select_reverse(now)
        for _t, dirn in self._select_reverse:
            self._fire_arrow(-dirn)
        self._select_reverse = []
        self._select_pad = None
        self._select_base_dir = 0
        self._select_touching = False
        self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_select_active(False)

    def _select_drag(self, raw_x, now):
        """Map horizontal pad travel to Shift+Left/Right taps: one arrow per
        SELECT_DRAG_STEP of travel from the anchor, moving the anchor along
        with the finger.

        Every arrow  forward or reverse  fires immediately, so rapid
        back-and-forth adjustments each land the instant their step of travel
        is seen (no withheld travel, no one-sided creep). Reverse arrows are
        additionally recorded, timestamped, so _end_select can cancel a
        lift-off roll-back. That is the ONLY place travel is ever undone."""
        self._prune_select_reverse(now)
        step = self.SELECT_DRAG_STEP
        while abs(raw_x - self._select_anchor_x) >= step:
            dirn = 1 if raw_x > self._select_anchor_x else -1
            if self._select_base_dir == 0:
                self._select_base_dir = dirn
            self._fire_arrow(dirn)
            self._select_anchor_x += step * dirn
            if dirn != self._select_base_dir:
                # Only a REVERSAL can be lift-off roll-back, so forward arrows
                # are never buffered and never cancelled.
                self._select_reverse.append((now, dirn))

    def handle_pad_input(self, coord_frac, buttons, touch_button_mask, select_button_mask,
                         click_button_mask=0, allow_click=True, now=0.0,
                         trigger_pressed=False, trigger_prev=False,
                         trigger_bit=0, raw_x=0):
        prev = self.sc_input_previous.buttons
        # Releasing the physical pad press rumbles too, so a click feels like a
        # full button: one tick pressing down, one coming back up. Checked
        # before the touch gate so it still fires if the finger lifts off at
        # the same instant the pad-click releases. Uses the stronger pad-click
        # haptic (deeper/more intense than the light UI tick).
        if (not (buttons & click_button_mask)) and (prev & click_button_mask):
            state.pad_click_haptic()
        touch_key = int(touch_button_mask)
        repeat_key = int(select_button_mask)
        touching = bool(buttons & touch_button_mask)
        holding = (allow_click and trigger_pressed) or bool(
            buttons & click_button_mask)

        # --- sessions that have to be serviced BEFORE the touch gate -------
        # All three end on a RELEASE, and on the release frame the finger is
        # often already off the pad  the gate below would return INACTIVE and
        # the session would never be torn down (a stuck latch then swallows
        # every later press on this pad).
        if self._select_pad == repeat_key:
            if not holding:
                self._end_select(now)
                return state.InputState.INACTIVE
            if not touching:
                # Still held but the finger lifted: freeze the selection. The
                # next fresh placement re-anchors, so nothing fires meanwhile.
                self._select_touching = False
                return state.InputState.INACTIVE
            if not self._select_touching:
                # A fresh placement must not fire arrows  the thumb may land
                # anywhere. Re-anchor and let only travel from here drag.
                self._select_anchor_x = raw_x
                self._select_base_dir = 0
                self._select_reverse = []
                self._select_touching = True
            else:
                self._select_drag(raw_x, now)
            return state.InputState.CLICK
        if self._diacritic_pad == repeat_key:
            if not holding:
                self._end_diacritic_pad()
                return state.InputState.INACTIVE
            if not state.is_diacritic_open():
                # Self-heal: the row was closed by another input or by
                # teardown, but the latch survived. Drop it rather than let it
                # swallow every later press on this pad.
                self._diacritic_pad = None
                self._click_repeat_at.pop(repeat_key, None)
            elif touching:
                rect = state.get_diacritic_rect()
                if rect is not None:
                    px, py = coord_frac.to_absolute()
                    state.set_diacritic_index(diacritics.variant_index_at_point(
                        rect, px, py, state.get_diacritic_variant_count()))
                return state.InputState.CLICK
            else:
                return state.InputState.CLICK
        elif repeat_key in self._deferred_base:
            # A deferred variant-capable press released without a row ever
            # opening  a quick tap. Type the base letter now.
            if not holding or (not touching and (prev & touch_button_mask)):
                coord = self._deferred_base.pop(repeat_key)
                self._click_repeat_at.pop(repeat_key, None)
                # The press edge already clicked, so this one stays silent.
                self.controller_state.click_queue.append(("deferred", coord))
                self._release_typed[touch_key] = True
                return state.InputState.INACTIVE

        if not (buttons & touch_button_mask):
            # "Release Touch To Type" (Options → Touchpads): lifting the finger
            # enters whatever key this pad was hovering  no pad click, no
            # L2/R2 pull. The coord is the last TOUCHED sample (see
            # _release_coord), and a touch that already entered a key by
            # clicking is skipped so one press can't type twice. The setting is
            # read at LIFT, not at touch-down, so toggling it mid-touch takes
            # effect immediately. Off a key, find_key returns None on the main
            # thread and the queued coord is simply dropped.
            last = self._release_coord.pop(touch_key, None)
            typed = self._release_typed.pop(touch_key, False)
            # "Touch Typing" implies this: on a phone you tap the glass and the
            # key goes in, so it shares the same lift path rather than growing
            # a second one that could double-fire alongside it.
            if (last is not None and not typed
                    and (state.is_release_to_type_enabled()
                         or state.is_touch_typing_enabled())):
                self.controller_state.click_queue.append(last)
                state.pad_click_haptic()
            return state.InputState.INACTIVE
        self._release_coord[touch_key] = coord_frac
        # Two ways to "enter" the key under the pointer while touching:
        #   • the trigger (L2/R2)  only when allowed by its current role 
        #     on the rising edge of the touch+trigger combo;
        #   • a physical pad press (pressing the trackpad down), which always
        #     selects regardless of the trigger role.
        # Each fires a rumble once on the rising edge  that rumble IS the
        # simulated click. Both the physical pad press and the L2/R2 trigger
        # select use the stronger pad-click haptic so all "enter the key"
        # feedback matches. Fired here on the controller thread for lowest
        # latency (fires even off a key).
        # trigger_pressed/_prev are the analog-aware actuation (see
        # _osk_trigger_pressed) so the touchpad-click honors the lowered
        # "Trigger Actuation" setting too, not just the firmware full-pull bit.
        trigger_held = allow_click and trigger_pressed
        pad_clicked = bool(buttons & click_button_mask)
        touch_was = bool(prev & touch_button_mask)
        trigger_edge = trigger_held and (not touch_was or not trigger_prev)
        pad_edge = pad_clicked and not (prev & click_button_mask)
        click_active = trigger_held or pad_clicked
        # Hold-to-repeat, keyed per pad. First hit enters the key, rumbles, and
        # arms the repeat clock; held past BACKSPACE_HOLD_DELAY it re-enters the
        # key every BACKSPACE_REPEAT. Repeat hits are tagged so the main thread
        # only acts on them over Backspace (no rumble on repeat  matches X).
        if trigger_edge or pad_edge:
            if self._select_pad is None and self._is_select_key(coord_frac):
                # A press on the Select key enters select mode instead of
                # typing: hold Shift now so the drag's arrow taps select text.
                self._select_pad = repeat_key
                self._select_anchor_x = raw_x
                self._select_base_dir = 0
                self._select_reverse = []
                self._select_touching = True
                self._release_typed[touch_key] = True
                self._kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                state.set_select_active(True)
                state.pad_click_haptic()
                return state.InputState.CLICK
            # This touch has entered its key already  don't let the lift
            # ("Release Touch To Type") enter a second one.
            self._release_typed[touch_key] = True
            if now > 0 and now < self._click_settle_at.get(repeat_key, 0.0):
                # A click bounce inside the settle window: swallow the phantom
                # second insert, but re-arm the hold clock (the pad IS still
                # held, so a long hold must keep rubbing out) and re-store the
                # defer  a real release inside the window must not lose the
                # base letter.
                if self._should_defer_press(coord_frac):
                    self._deferred_base[repeat_key] = coord_frac
                self._click_repeat_at[repeat_key] = now + self.BACKSPACE_HOLD_DELAY
            else:
                if self._should_defer_press(coord_frac):
                    # Tap-first, hold-to-extend: a press on a letter that has
                    # accented variants types NOTHING yet. The hold opens its
                    # row; the release types the picked variant, or the base if
                    # the row never opened. The click sound still fires at the
                    # press edge like any other key  the release commit must
                    # not re-tick, or a held pick would sound laggy.
                    self._deferred_base[repeat_key] = coord_frac
                    state.key_sound_tick()
                else:
                    self.controller_state.click_queue.append(coord_frac)
                self._click_repeat_at[repeat_key] = now + self.BACKSPACE_HOLD_DELAY
                self._click_settle_at[repeat_key] = now + self.PAD_CLICK_SETTLE
            # An L2/R2-driven select routes through the trigger haptic (only
            # analog-trigger kinds buzz  the rumble replaces the click those
            # triggers don't have); a physical pad press / rebound button
            # keeps the strong pad-click tick.
            if trigger_edge and not pad_edge:
                self._click_select_haptic(trigger_bit)
            else:
                state.pad_click_haptic()
        elif click_active and now >= self._click_repeat_at.get(repeat_key, float("inf")):
            # Held past the repeat delay. Letters can't repeat, so before
            # queueing a meaningless one, try turning the hold into a variant
            # row instead.
            if not self._try_open_diacritic(coord_frac, repeat_key):
                self.controller_state.click_queue.append(("repeat", coord_frac))
            self._click_repeat_at[repeat_key] = now + self.BACKSPACE_REPEAT
        if not click_active:
            self._click_repeat_at.pop(repeat_key, None)
        if click_active:
            return state.InputState.CLICK
        return state.InputState.HOVER

    # Cap on points kept for one traced word. At the OSK's poll rate even a
    # long word is a couple of hundred samples; the cap only bites on a thumb
    # left resting on the pad, and the decoder resamples anyway so extra
    # detail buys nothing.
    _SWIPE_MAX_PTS = 400
    # Samples closer together than this (keyboard px) are dropped, so a still
    # thumb can't fill the buffer with one repeated coordinate.
    _SWIPE_MIN_STEP = 3.0

    def _swipe_step(self, touch_mask, coords, buttons, pad_click_mask,
                    trigger_bit):
        """Trace one pad's Swipe Typing gesture, and on lift decode it into a
        word and type it.

        Runs before handle_pad_input for this frame, so a committed word can
        mark the pad as already-typed and stop "Release Touch To Type" from
        ALSO entering whatever key the finger happened to stop on  both
        features read the same lift."""
        key = int(touch_mask)
        if not state.is_swipe_typing_enabled():
            if self._swipe_pts.pop(key, None) is not None:
                self._swipe_bad.pop(key, None)
                state.set_swipe_trail(key, None)
            return

        if buttons & touch_mask:
            x, y = coords.to_absolute()
            pts = self._swipe_pts.get(key)
            if pts is None:
                pts = self._swipe_pts[key] = [(x, y)]
                self._swipe_bad[key] = False
                # Published by reference, once: the render thread re-reads the
                # live list every frame to draw the trail, and appending to a
                # list while another thread copies it is safe under the GIL.
                state.set_swipe_trail(key, pts)
            else:
                lx, ly = pts[-1]
                dx = x - lx
                dy = y - ly
                if (dx * dx + dy * dy >= self._SWIPE_MIN_STEP ** 2
                        and len(pts) < self._SWIPE_MAX_PTS):
                    pts.append((x, y))
            if (buttons & pad_click_mask) or (trigger_bit
                                              and (buttons & trigger_bit)):
                self._swipe_bad[key] = True
                state.set_swipe_trail(key, None)
            return

        # --- Lifted: judge and commit.
        pts = self._swipe_pts.pop(key, None)
        bad = self._swipe_bad.pop(key, False)
        if pts is None:
            return
        state.set_swipe_trail(key, None)
        if bad or not swipe_typing.is_swipe(pts):
            return
        # Decoding is deliberately SYNCHRONOUS on this input thread. It costs a
        # few tens of milliseconds at worst, and handing it to a worker would
        # let a fast follow-up tap land BEFORE the word it was typed after 
        # wrong text is far worse than one late input frame, and the stall
        # lands the instant the thumb leaves the pad, when nothing is moving.
        try:
            best = swipe_typing.decode(pts, max_results=1)
        except Exception as e:
            print(f"swipe: decode failed ({e!r})")
            return
        if not best:
            return
        self._type_word(best[0][0])
        self._release_typed[key] = True
        state.pad_click_haptic()

    def _type_word(self, word):
        """Type a decoded word, then a space  every swipe keyboard commits a
        word that way, and without it consecutive swipes would run together.

        A latched Shift capitalises the first letter and is then dropped, the
        same courtesy dispatch_key does for a single key. A Shift being HELD
        (the L2 trigger) is left alone: the user is holding it, so they get the
        whole word capitalised, which is what holding it means."""
        latched = state.is_shift_latched()
        for i, ch in enumerate(word):
            code = sui.Keys["KEY_" + ch.upper()]
            self._kb.pressEvent([code])
            self._kb.releaseEvent([code])
            if i == 0 and latched:
                vkb.clear_shift_latch(release_os=not self._shift_active)
        self._kb.pressEvent([sui.Keys.KEY_SPACE])
        self._kb.releaseEvent([sui.Keys.KEY_SPACE])

    # Left-stick deflection (int16) past this magnitude counts as a direction.
    STICK_DEADZONE = 14000
    # Volume feel: a tap = one step. Holding up/down past STICK_HOLD_DELAY
    # seconds then rapidly ramps, one step every STICK_VOL_REPEAT seconds.
    STICK_HOLD_DELAY = 0.5
    STICK_VOL_REPEAT = 0.021
    # Left-stick keyboard navigation: tap = one key; held past the delay it
    # repeats one key every KBD_STICK_REPEAT seconds (slow enough to land on
    # the intended key without overshooting).
    KBD_STICK_HOLD_DELAY = 0.35
    KBD_STICK_REPEAT = 0.15
    # Deflection before the left stick steps the OSK key cursor. 32% larger than
    # the base STICK_DEADZONE (20% then another 10%) so the cursor doesn't
    # actuate on a light push (the media chord keeps the smaller STICK_DEADZONE).
    # Mirror this in inputsrc's _GLYPH_LSTICK_THRESHOLD so the glyph swap still
    # tracks real key movement.
    KBD_STICK_DEADZONE = round(STICK_DEADZONE * 1.32)
    # The Switch Pro / SDL pads switch OSK keys at a SMALLER left-stick deflection
    # than the Steam Controller (user pref): 30% below KBD_STICK_DEADZONE. Applied
    # only while an SDL pad is the active controller  see _handle_kbd_stick.
    KBD_STICK_DEADZONE_SDL = round(KBD_STICK_DEADZONE * 0.7)
    # When "Sticks Control Keyboard" is OFF, the SC left stick scrolls the window
    # behind the OSK. It sends ARROW-KEY taps (not mouse-wheel notches) with the
    # SAME deadzone (STICK_DEADZONE) / hold / repeat as the tray _Watcher's
    # desktop arrow-stick, so the scroll speed is identical whether the OSK is
    # open or closed. (A wheel notch scrolls ~3 lines vs an arrow's ~1, which is
    # why the old wheel-based scroll felt faster than the closed-OSK scroll.)
    KBD_SCROLL_HOLD_DELAY = 0.35
    KBD_SCROLL_REPEAT = 0.05 / 0.7 * 1.1
    _SCROLL_ARROW_KEYS = {
        "UP":    sui.Keys.KEY_UP,
        "DOWN":  sui.Keys.KEY_DOWN,
        "LEFT":  sui.Keys.KEY_LEFT,
        "RIGHT": sui.Keys.KEY_RIGHT,
    }
    # Hold-to-repeat cadence for every controller "press a key" path (X, A,
    # L2/R2/pad-click): one hit on press, then (after holding past the delay) a
    # deliberately slow repeat. Single-sourced from vkb so the mouse path and
    # every key (Backspace + arrows) rub out / step at one matched speed.
    BACKSPACE_HOLD_DELAY = vkb.KEY_REPEAT_DELAY
    BACKSPACE_REPEAT = vkb.KEY_REPEAT_INTERVAL

    def _handle_media_stick(self, sc_input, steam_now, now):
        """Steam + left stick → media transport. Up/Down = volume (repeats
        while held); Left/Right = previous/next track (one per deflection).
        Edge-triggered: the stick must return toward center before the same
        direction fires again."""
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up (same hardware sign as the pads)

        zone = "NEUTRAL"
        if steam_now and (abs(x) > self.STICK_DEADZONE
                          or abs(y) > self.STICK_DEADZONE):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        key = {
            "UP":    sui.Keys.KEY_VOLUMEUP,
            "DOWN":  sui.Keys.KEY_VOLUMEDOWN,
            "LEFT":  sui.Keys.KEY_PREVIOUSSONG,
            "RIGHT": sui.Keys.KEY_NEXTSONG,
        }.get(zone)

        fire = False
        is_edge = False
        if zone != self._stick_zone_prev:
            # Entering a new non-neutral zone always fires once (the "tap").
            # Then wait STICK_HOLD_DELAY before any rapid repeat begins, so a
            # quick tap (or a sub-second hold) is exactly one step.
            fire = zone != "NEUTRAL"
            is_edge = fire
            self._stick_repeat_at = now + self.STICK_HOLD_DELAY
        elif zone in ("UP", "DOWN") and now >= self._stick_repeat_at:
            # Held past the delay: volume ramps fast. Track skip never repeats.
            fire = True
            self._stick_repeat_at = now + self.STICK_VOL_REPEAT
        self._stick_zone_prev = zone

        if fire and key is not None:
            self._kb.pressEvent([key])
            self._kb.releaseEvent([key])
            # Mark the Steam press as "used" so releasing it doesn't close the OSK.
            self._saw_x_during_steam = True
            # Haptic tick on a volume TAP only (one 2% step)  not the rapid
            # hold-ramp, and not track skip (left/right).
            if is_edge and zone in ("UP", "DOWN"):
                state.haptic_tick()

    def _handle_kbd_stick(self, sc_input, steam_now, now):
        """Left stick → move the on-screen-keyboard cursor (one key per
        deflection; auto-repeats while held). Only active when Steam is NOT
        held, since Steam + left stick is the media chord above. The actual
        cursor move  and its key-switch haptic  happens in the main loop
        via step_cursor, so this just posts DPAD direction events."""
        # With "Keyboard Sticks/Mouse controls" turned off for the ACTIVE
        # controller (its tray submenu), its left stick scrolls the window behind
        # the OSK instead of moving the key cursor  so you can scroll a page
        # while the OSK is open (firmware lizard is OFF while the OSK owns the
        # controller, so the app injects the scroll itself). Applies to the Steam
        # Controller AND the Switch Pro, each per its own toggle.
        active = state.get_active_controller()
        if not state.is_kbd_stick_nav_enabled_for(active):
            self._kbd_stick_zone_prev = "NEUTRAL"
            self._handle_kbd_stick_scroll(sc_input, steam_now, now)
            return
        # Not scrolling: clear the scroll zone so toggling the setting mid-hold
        # re-fires an initial tap instead of treating the deflection as ongoing.
        self._kbd_scroll_zone_prev = "NEUTRAL"
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up

        # SDL pads (Switch/Xbox/PS/...) switch keys at a smaller deflection
        # than the SC. The Steam Deck streams through the same HID backend at
        # the SC's stick scale, so it shares the SC deadzone.
        deadzone = (self.KBD_STICK_DEADZONE
                    if active in ("sc", "sc2015", "steam_deck")
                    else self.KBD_STICK_DEADZONE_SDL)
        zone = "NEUTRAL"
        if not steam_now and (abs(x) > deadzone or abs(y) > deadzone):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        fire = False
        if zone != self._kbd_stick_zone_prev:
            # Entering a new direction always steps once (the "tap"), then
            # waits KBD_STICK_HOLD_DELAY before the held auto-repeat begins.
            fire = zone != "NEUTRAL"
            self._kbd_stick_repeat_at = now + self.KBD_STICK_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._kbd_stick_repeat_at:
            fire = True
            self._kbd_stick_repeat_at = now + self.KBD_STICK_REPEAT
        self._kbd_stick_zone_prev = zone

        if fire:
            state.queue_dpad(zone, haptic=True)

    def _handle_kbd_stick_scroll(self, sc_input, steam_now, now):
        """Left stick → scroll the window behind the OSK (used when "Sticks
        Control Keyboard" is off). Sends ARROW-KEY taps on the dominant axis 
        one on entering a direction, then auto-repeating while held  with the
        exact deadzone / hold delay / repeat cadence the tray _Watcher uses for
        desktop arrow-stick scrolling, so the speed matches the OSK-closed scroll.
        The taps land on the focused window behind the no-focus OSK."""
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up
        dz = self.STICK_DEADZONE
        zone = "NEUTRAL"
        if not steam_now and (abs(x) > dz or abs(y) > dz):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"
        fire = False
        if zone != self._kbd_scroll_zone_prev:
            # New direction (or release): the press fires immediately, then we
            # wait KBD_SCROLL_HOLD_DELAY before the first repeat.
            fire = zone != "NEUTRAL"
            self._kbd_scroll_at = now + self.KBD_SCROLL_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._kbd_scroll_at:
            fire = True
            self._kbd_scroll_at = now + self.KBD_SCROLL_REPEAT
        self._kbd_scroll_zone_prev = zone
        key = self._SCROLL_ARROW_KEYS.get(zone)
        if fire and key is not None:
            self._kb.pressEvent([key])
            self._kb.releaseEvent([key])

    def _osk_trigger_pressed(self, buttons, bit, analog):
        """True if the OSK should treat this trigger (L2/R2) as pressed for
        Shift/Enter. Always true on the firmware full-pull digital bit; with a
        lowered actuation set (tray "Steam Controller" menu) it also engages at a
        lighter analog pull (0..32767). NOTE: no active=="sc" gate  the active
        controller only flips to "sc" on the FULL-pull digital edge, which would
        keep the lighter analog point from ever engaging (chicken-and-egg). The
        menu is SC-only and reads the merged trigger; an SC-only user is the SC."""
        if buttons & bit:
            return True
        thr = state.get_sc_osk_trigger_threshold()
        if thr is None:
            return False
        return analog >= thr

    def _pad_trigger_pressed(self, sc_input, bit):
        """True if the configured touchpad-click button is pressed. L2/R2 honour
        the analog actuation setting (lighter pull); any other button is a plain
        digital press."""
        if bit == SCButtons.LT:
            return self._osk_trigger_pressed(sc_input.buttons, SCButtons.LT, sc_input.ltrig)
        if bit == SCButtons.RT:
            return self._osk_trigger_pressed(sc_input.buttons, SCButtons.RT, sc_input.rtrig)
        return bool(sc_input.buttons & bit)

    # Gyro OSK pointer tuning. The circle's speed follows the GLOBAL gyro-
    # typing config (Options → Keyboard → "Gyro To Type", one setting for every
    # controller  NOT the per-kind Gyro To Mouse tuning, which is calibrated
    # for a game camera): GYRO_OSK_FRAC_PER_GAIN converts state.kbd_gyro_gain's
    # px/deg into OSK-surface fractions per degree, normalized so the DEFAULT
    # 2.5x sensitivity sweeps the full keyboard in a ~40° turn (0.025 frac/deg).
    # The deadzone / precision / acceleration shaping comes from kbd_gyro_shape.
    # MAX_DT clamps across gaps (toggle-on, reconnect) so no first-frame fling.
    GYRO_OSK_FRAC_PER_GAIN = 0.025 / (6545.0 / 360.0 * 2.5)
    GYRO_OSK_MAX_DT = 0.1

    def _handle_gyro_osk(self, sc_input, now):
        """Gyro control of the OSK, run before ANY other dispatch.

        Two jobs:
          1. The "Gyro To Mouse" hotkey (the cog modal's bars) also works
             while the OSK is open  the tray's chord paths cede the
             controllers then, so the ACTIVE kind's published masks are
             evaluated here against the merged frame, honoring the modal's
             Enable/Suppress/Toggle mode (held bits are masked out so they
             can't also fire OSK actions).
          2. While gyro is active, it steers a SYNTHESIZED right-trackpad
             touch: RPADTOUCH + rpad coords are written into the frame, so
             the whole existing trackpad pipeline  the Steam Deck thumb-
             circle pointer, key highlighting, the touchpad-click button
             (default R2) typing the highlighted key, hold-to-repeat  drives
             identically on EVERY controller kind, gyro-steered. A real
             trackpad finger (SC/Deck) always wins: the synthetic touch backs
             off while the physical RPADTOUCH bit is set and re-seeds at the
             keyboard's center on the next gyro engage.

          3. "Always Type With Gyro" (Options → Keyboard) seeds job 2 ON for
             the whole keyboard session, so the gyro need not have been
             toggled on BEFORE the keyboard opened. It's held session-local
             (never written into the shared desktop gyro-mouse state), and the
             hotkey above still turns it off/on while typing.

          4. L3+R3 (both thumbsticks clicked in together) is a FIXED chord,
             independent of the configured hotkey/mode, so gyro typing is
             always reachable even with no gyro hotkey bound: while gyro
             typing is OFF it turns it ON; while it's already ON it just
             RECENTERS the pointer back to the keyboard's middle (a lost
             pointer is far more likely mid-session than wanting it off, and
             the configured hotkey/"Always" setting still turn it off).
        """
        active = state.get_active_controller()
        mode = state.get_gyro_mode(active)
        always = state.is_kbd_gyro_always()
        if not always:
            self._gyro_always_on = False
        elif self._gyro_always_on is None:
            self._gyro_always_on = True
        # Both thumbsticks clicked in together is a fixed OSK chord,
        # independent of the gyro hotkey/mode above: turns gyro typing on if
        # it's off, or recenters the pointer if it's already on.
        l3r3_mask = SCButtons.L3 | SCButtons.R3
        l3r3_held = (sc_input.buttons & l3r3_mask) == l3r3_mask
        if l3r3_held and not self._l3r3_gyro_latch:
            if self._gyro_always_on or state.is_gyro_mouse_active(active):
                self._gyro_frac = [0.5, 0.5]
            else:
                state.toggle_gyro_mouse(active)
            state.pad_click_haptic()
        self._l3r3_gyro_latch = l3r3_held
        if l3r3_held:
            sc_input = sc_input._replace(buttons=sc_input.buttons & ~l3r3_mask)
        if mode == "none" and not always and not state.is_gyro_mouse_active(active):
            self._gyro_frac = None
            return sc_input
        masks = state.get_gyro_toggle_masks_for(active)
        held = False
        held_mask = 0
        for m in masks:
            if (sc_input.buttons & m) == m:
                held = True
                held_mask |= m
        if mode == "toggle":
            if held and not self._gyro_chord_latch:
                if self._gyro_always_on:
                    # "Always" had it on: the tap turns gyro typing OFF.
                    self._gyro_always_on = False
                    state.set_gyro_mouse(active, False)
                else:
                    state.toggle_gyro_mouse(active)
                state.pad_click_haptic()
            self._gyro_chord_latch = held
        elif mode == "hold_enable":
            state.set_gyro_mouse(active, held)
        elif mode == "hold_suppress":
            state.set_gyro_mouse(active, not held)
            # The suppress hold beats "Always" too, and releases back into it.
            self._gyro_always_on = always and not held
        if held_mask:
            sc_input = sc_input._replace(
                buttons=sc_input.buttons & ~held_mask)
        if not (self._gyro_always_on or state.is_gyro_mouse_active(active)):
            self._gyro_frac = None
            return sc_input
        if sc_input.buttons & SCButtons.RPADTOUCH:
            self._gyro_frac = None   # a real thumb owns the pad circle
            return sc_input
        if self._gyro_frac is None:
            # (Re)engage: park the circle at the keyboard's center.
            self._gyro_frac = [0.5, 0.5]
            dt = 0.0
        else:
            dt = now - self._gyro_last_t
        self._gyro_last_t = now
        if 0.0 < dt <= self.GYRO_OSK_MAX_DT:
            yaw_dps, pitch_dps = state.kbd_gyro_shape(
                sc_input.gyaw * GYRO_DEG_PER_SEC,
                sc_input.gpitch * GYRO_DEG_PER_SEC)
            k = self.GYRO_OSK_FRAC_PER_GAIN * state.kbd_gyro_gain() * dt
            # + yaw = turn left → circle left; + pitch = tilt up → circle up.
            self._gyro_frac[0] = min(1.0, max(0.0, self._gyro_frac[0] - yaw_dps * k))
            self._gyro_frac[1] = min(1.0, max(0.0, self._gyro_frac[1] - pitch_dps * k))
        # Inverse of adjust_raw_x/adjust_raw_y (right-pad anchors _RPAD_X_CENTER,
        # 1/2) so the virtual coords land the pointer exactly at _gyro_frac on
        # screen. The gyro reaches the WHOLE keyboard, so raw_x deliberately runs
        # past the pad's own ±0x8000 range for the left part of the layout.
        raw_x = utils.round_to_int((self._gyro_frac[0] - _RPAD_X_CENTER) * 0x20000 / _PAD_X_SCALAR)
        raw_y = utils.round_to_int((1 / 2 - self._gyro_frac[1]) * 0x10000 * 5 / 6)
        # This touch is SYNTHETIC: switching gyro typing off drops the
        # RPADTOUCH bit with no finger ever having lifted, which
        # "Release Touch To Type" would otherwise read as a lift and type a
        # phantom key wherever the gyro circle was parked. Flag the right pad
        # as already-typed so that lift is swallowed (the flag is consumed
        # there, so the next REAL finger types normally).
        self._release_typed[int(SCButtons.RPADTOUCH)] = True
        return sc_input._replace(
            buttons=sc_input.buttons | int(SCButtons.RPADTOUCH),
            rpad_x=raw_x, rpad_y=raw_y)

    def handle_input(self, sc, sc_input):
        # Gyro hotkey + gyro OSK pointer first  before EventMapper and every
        # handler below, so a toggle chord's buttons never also fire their OSK
        # actions and the synthesized right-pad touch drives the whole
        # trackpad pipeline downstream.
        sc_input = self._handle_gyro_osk(sc_input, time.monotonic())
        self.evm.process(sc, sc_input)

        # Haptic feedback: one tick when the keyboard first opens.
        if self._open_tick_pending:
            self._open_tick_pending = False
            state.haptic_tick()

        # Single monotonic timestamp for this frame, used by every hold-to-
        # repeat clock below (DPAD/A, X, the pad-click repeat, media stick).
        now = time.monotonic()
        # Which controller family is driving the OSK right now ("sc" / "sdl").
        # Per-controller settings (pointer speed, Sticks-Control-Keyboard) read
        # the entry for this family so the SC and the Switch Pro can differ.
        active = state.get_active_controller()

        # Right stick -> system mouse cursor so any pad can point-and-click the
        # OSK keys (hover highlights, A presses the hovered key) without closing
        # the keyboard. Sub-pixel motion is accumulated so slow nudges register.
        dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
        self._mouse_last_t = now
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0
        # "Pointer Speed" (tray, per active controller) scales the base px/sec.
        # Radial speed (see _mouse_vector) so diagonals aren't slower than axes.
        mouse_speed = _MOUSE_SPEED * state.get_mouse_speed_for(active)
        _mvecx, _mvecy = _mouse_vector(sc_input.rstick_x, sc_input.rstick_y,
                                       _MOUSE_DEADZONE, _MOUSE_EXPONENT)
        self._mouse_acc_x += _mvecx * mouse_speed * dt
        # Stick-up moves the cursor up; screen Y grows downward, so invert.
        self._mouse_acc_y += -_mvecy * mouse_speed * dt
        _mvx, _mvy = int(self._mouse_acc_x), int(self._mouse_acc_y)
        self._mouse_acc_x -= _mvx
        self._mouse_acc_y -= _mvy
        if _mvx or _mvy:
            self._mouse.move(_mvx, _mvy)

        # Steam held gates the media chords below (Steam + left stick / L3).
        steam_now = bool(sc_input.buttons & (SCButtons.STEAM | SCButtons.QAM))  # "..." (QAM) acts like Steam

        # Resolve the (live, remappable) OSK function → button map FOR THE
        # ACTIVE CONTROLLER KIND (each controller's Options category has its
        # own Caps/Shift/Enter/Space/Backspace dropdowns). Defaults reproduce
        # the built-in mapping: caps=L3, shift=L2(LT), enter=R2(RT), space=Y,
        # backspace=X. The shift/enter triggers keep their special role logic
        # (pad-click / mouse) ONLY while still bound to LT/RT.
        _osk_map = state.get_osk_buttons_for(active)
        caps_bit = _ctrl_bit(_osk_map.get("caps"), SCButtons.L3)
        shift_bit = _ctrl_bit(_osk_map.get("shift"), SCButtons.LT)
        enter_bit = _ctrl_bit(_osk_map.get("enter"), SCButtons.RT)
        space_bit = _ctrl_bit(_osk_map.get("space"), SCButtons.Y)
        backspace_bit = _ctrl_bit(_osk_map.get("backspace"), SCButtons.X)
        # Buttons the user has assigned to an OSK function via the Options "On
        # Screen Keyboard" dropdowns. Assigning a function to a button OVERRIDES
        # that button's built-in OSK behaviour: e.g. binding any function to L4
        # stops L4 closing the keyboard, and binding one to R4 stops R4 → Space.
        osk_func_bits = (caps_bit | shift_bit | enter_bit
                         | space_bit | backspace_bit)
        # R4 (RGRIP1) is a built-in Space trigger only while not reassigned.
        r4_space_bit = (0 if (SCButtons.RGRIP1 & osk_func_bits)
                        else SCButtons.RGRIP1)

        # Touchpad-click trigger button (Options → On Screen Keyboard). While
        # the matching pad is touched, THIS button clicks the highlighted key
        # under the finger; while the pad isn't touched it keeps its normal OSK
        # action. Defaults reproduce the built-in L2 (left) / R2 (right) feel.
        lpad_click_bit = _ctrl_bit(state.get_lpad_click_button(), SCButtons.LT)
        rpad_click_bit = _ctrl_bit(state.get_rpad_click_button(), SCButtons.RT)

        # Touch state first  it gates the click trigger and the masked buttons.
        lpad_touched = bool(sc_input.buttons & SCButtons.LPADTOUCH)
        rpad_touched = bool(sc_input.buttons & SCButtons.RPADTOUCH)
        state.set_pad_touched(lpad_touched, rpad_touched)

        # "Control the desktop" mode (Keyboard Sticks/Mouse controls OFF for the
        # ACTIVE controller): the OSK is click-through and L2/R2 act as the LEFT/
        # RIGHT mouse buttons  UNLESS the matching pad is touched (the click
        # button still clicks the OSK key under the finger).
        desktop_mode = not state.is_kbd_stick_nav_enabled_for(active)

        # Click-mode latch per pad. The click button enters "click mode" on its
        # rising edge IF the pad was being touched then; held until release so a
        # later touch/slide can't flip it, and pressing it without touching keeps
        # its normal action. L2/R2 honour the analog actuation setting.
        l_click_pressed = self._pad_trigger_pressed(sc_input, lpad_click_bit)
        r_click_pressed = self._pad_trigger_pressed(sc_input, rpad_click_bit)
        l_click_was = self._l_click_prev  # for handle_pad_input's click edge
        r_click_was = self._r_click_prev
        if l_click_pressed and not self._l_click_prev:
            self._lpad_click_mode = lpad_touched
        elif not l_click_pressed:
            self._lpad_click_mode = False
        self._l_click_prev = l_click_pressed
        if r_click_pressed and not self._r_click_prev:
            self._rpad_click_mode = rpad_touched
        elif not r_click_pressed:
            self._rpad_click_mode = False
        self._r_click_prev = r_click_pressed

        # Buttons for all the "normal action" handlers below: the click button is
        # masked out while it's actively clicking, so it inputs the highlighted
        # key INSTEAD of (not as well as) its normal OSK action. Pad touch state,
        # Steam chords and the click trigger itself read the raw buttons.
        eff_buttons = sc_input.buttons
        if self._lpad_click_mode:
            eff_buttons &= ~int(lpad_click_bit)
        if self._rpad_click_mode:
            eff_buttons &= ~int(rpad_click_bit)

        # Steam + physical L3 → Play/Pause (a media chord). Manual rising-edge.
        l3_pressed = bool(eff_buttons & SCButtons.L3)
        if l3_pressed and not self._l3_was_pressed and steam_now:
            self._kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
            self._kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
            # Mark the Steam press as "used" so releasing it doesn't close the OSK.
            self._saw_x_during_steam = True
        self._l3_was_pressed = l3_pressed

        # Caps Lock on the bound button (default L3), unless Steam is held.
        caps_pressed = bool(eff_buttons & caps_bit)
        if caps_pressed and not self._caps_was_pressed and not steam_now:
            self._kb.pressEvent([sui.Keys.KEY_CAPSLOCK])
            self._kb.releaseEvent([sui.Keys.KEY_CAPSLOCK])
        self._caps_was_pressed = caps_pressed

        # Shift on shift_bit (default L2). In desktop mode L2 is the RIGHT mouse
        # button, not Shift. A click-mode L2 is masked out of eff_buttons, so
        # touching+pulling clicks instead of shifting; pulling L2 untouched (or
        # then sliding onto the pad) keeps Shift held.
        shift_btn_now = bool(eff_buttons & shift_bit)
        if shift_bit == SCButtons.LT and desktop_mode:
            shift_btn_now = False
        if shift_btn_now and not self._shift_btn_prev:
            vkb.clear_shift_latch(release_os=not self._shift_active)
        self._shift_btn_prev = shift_btn_now
        if shift_btn_now and not self._shift_active:
            self._kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = True
            # Engage tick: trigger-routed for L2/R2 (analog-trigger kinds only)
            self._click_select_haptic(shift_bit)
        elif not shift_btn_now and self._shift_active:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = False
        # L2 → RIGHT mouse button in desktop mode (swapped per user pref), unless
        # L2 is clicking the pad (then it's masked out of eff_buttons).
        mouse_l_hold = desktop_mode and bool(eff_buttons & SCButtons.LT)
        if mouse_l_hold and not self._mouse_l_active:
            self._mouse.press("right")
            self._mouse_l_active = True
            state.trigger_haptic()  # L2 actuation = the click (analog kinds)
        elif not mouse_l_hold and self._mouse_l_active:
            self._mouse.release("right")
            self._mouse_l_active = False

        # Enter on enter_bit (default R2). In desktop mode R2 is the LEFT mouse
        # button, not Enter. Pulling Enter while the pointer is on the close X
        # closes the OSK.
        enter_btn_now = bool(eff_buttons & enter_bit)
        if enter_bit == SCButtons.RT and desktop_mode:
            enter_btn_now = False
        if enter_btn_now and not self._enter_active:
            if self._pointer_on_close_x():
                state.close()
                enter_btn_now = False
            else:
                self._kb.pressEvent([sui.Keys.KEY_ENTER])
                self._enter_active = True
                # Engage tick: trigger-routed for L2/R2 (analog kinds only)
                self._click_select_haptic(enter_bit)
        elif not enter_btn_now and self._enter_active:
            self._kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._enter_active = False
        # R2 → LEFT mouse button in desktop mode, unless R2 is clicking the pad.
        mouse_r_hold = desktop_mode and bool(eff_buttons & SCButtons.RT)
        if mouse_r_hold and not self._mouse_r_active:
            self._mouse.press("left")
            self._mouse_r_active = True
            state.trigger_haptic()  # R2 actuation = the click (analog kinds)
        elif not mouse_r_hold and self._mouse_r_active:
            self._mouse.release("left")
            self._mouse_r_active = False

        # Mirror Shift state to the renderer so it can show uppercase labels.
        # OR in the mouse/click latch so a controller frame doesn't stomp a
        # latched Shift (which would desync the display and break the toggle).
        state.set_shift_held(self._shift_active or state.is_shift_latched())

        # L4 (LGRIP1) closes the keyboard, on rising edge  UNLESS the user has
        # assigned an OSK function to L4 via the dropdowns, in which case that
        # function wins and L4 no longer closes.
        lgrip_pressed = bool(eff_buttons & SCButtons.LGRIP1)
        if (lgrip_pressed and not self._lgrip_was_pressed
                and not (SCButtons.LGRIP1 & osk_func_bits)):
            state.close()
        self._lgrip_was_pressed = lgrip_pressed

        # Any control bound to Escape in the SC desktop binds (B by default)
        # closes the keyboard on rising edge  mirrors the hardware/keyboard
        # Escape, so closing follows the binding instead of a hardcoded button.
        # A button assigned to an OSK function (dropdowns) overrides its close
        # role too, so it types instead of closing.
        close_bits = state.get_osk_close_buttons()
        close_now = any((eff_buttons & bit) and not (bit & osk_func_bits)
                        for bit in close_bits)
        if close_now and not self._osk_close_was_pressed:
            state.close()
        self._osk_close_was_pressed = close_now

        # DPAD navigates the cursor over the keyboard grid (one step per
        # press). Direction events are queued for the main loop, which knows
        # the layout's pixel widths and can pick the visually-aligned target.
        dpad_mask = (SCButtons.DPAD_UP | SCButtons.DPAD_DOWN
                     | SCButtons.DPAD_LEFT | SCButtons.DPAD_RIGHT)
        dpad_now = eff_buttons & dpad_mask
        dpad_newly = (self._dpad_prev ^ dpad_now) & dpad_now
        # DPAD navigation deliberately does NOT clear the Shift latch: a Shift
        # toggled on via DPAD+A must persist while you move the cursor to the
        # keys you want to capitalise, just like the mouse toggle. Only the L2
        # hold model resets the latch (handled in the LT block above).
        if dpad_newly & SCButtons.DPAD_UP:
            state.queue_dpad("UP")
        if dpad_newly & SCButtons.DPAD_DOWN:
            state.queue_dpad("DOWN")
        if dpad_newly & SCButtons.DPAD_LEFT:
            state.queue_dpad("LEFT")
        if dpad_newly & SCButtons.DPAD_RIGHT:
            state.queue_dpad("RIGHT")
        self._dpad_prev = dpad_now

        # A → press the key currently under the DPAD cursor. Press once on the
        # rising edge, then (held past BACKSPACE_HOLD_DELAY) repeat on the
        # BACKSPACE clock. The main thread only repeats a hit that lands on
        # Backspace, so holding A on the delete key rubs out text like X.
        a_pressed = bool(eff_buttons & SCButtons.A)
        a_row, a_col = state.get_cursor()
        if a_pressed and not self._a_was_pressed:
            if self._pointer_on_close_x():
                state.close()
            elif self._cell_has_variants(a_row, a_col):
                # Tap-first, hold-to-extend: a press on a letter that has
                # accented variants types NOTHING yet. Holding past the repeat
                # delay opens its row (the repeat path below); the release
                # types the picked variant, or the base if no row opened. The
                # click sound still fires at the press edge like any other
                # key  the release commit must not re-tick.
                self._a_deferred_cell = (a_row, a_col)
                state.key_sound_tick()
                self._a_repeat_at = now + self.BACKSPACE_HOLD_DELAY
            else:
                state.queue_key_press(a_row, a_col)
                self._a_repeat_at = now + self.BACKSPACE_HOLD_DELAY
        elif a_pressed and now >= self._a_repeat_at:
            state.queue_key_press(a_row, a_col, repeat=True)
            self._a_repeat_at = now + self.BACKSPACE_REPEAT
        elif self._a_was_pressed and not a_pressed:
            # A released. If the hold opened a variant row, commit the pick
            # the base was never typed, so there is nothing to rub out. A
            # release with nothing highlighted commits the FIRST variant (the
            # row's model is "only accented letters are selectable"), and a
            # quick tap that never opened a row types the base, silently.
            _cell = self._a_deferred_cell
            self._a_deferred_cell = None
            if (state.is_diacritic_open()
                    and state.get_diacritic_source() == "button"):
                _char = state.get_diacritic_selected_char()
                if _char is None:
                    _variants = state.get_diacritic_variants_list()
                    _char = _variants[0] if _variants else None
                if _char is not None:
                    self.controller_state.click_queue.append(("variant", _char))
                else:
                    state.close_diacritic()
            elif _cell is not None:
                state.queue_key_press(*_cell, silent=True)
            self._a_repeat_at = 0.0
        if a_pressed:
            state.set_mouse_press_cell((a_row, a_col))
        elif self._a_was_pressed:
            state.set_mouse_press_cell(None)
        self._a_was_pressed = a_pressed

        # Visual highlight: paint the on-screen key blue while its bound
        # controller button is held down.
        highlights = set()
        if self._shift_active or state.is_shift_latched():
            highlights.add(sui.Keys.KEY_LEFTSHIFT)
            highlights.add(sui.Keys.KEY_RIGHTSHIFT)
        if caps_pressed and not steam_now:
            highlights.add(sui.Keys.KEY_CAPSLOCK)
        if eff_buttons & backspace_bit:
            highlights.add(sui.Keys.KEY_BACKSPACE)
        if self._enter_active:
            highlights.add(sui.Keys.KEY_ENTER)
        if eff_buttons & (space_bit | r4_space_bit):
            highlights.add(sui.Keys.KEY_SPACE)
        state.set_highlighted(highlights)

        # Steam+X opens the keyboard; Steam pressed and released alone closes it.
        # (steam_now was computed at the top of this method.)
        x_now = bool(sc_input.buttons & SCButtons.X)
        if steam_now and not self._steam_was_pressed:
            self._saw_x_during_steam = False
        if steam_now and x_now and not self._saw_x_during_steam:
            self._saw_x_during_steam = True
            state.show()

        # The OSK owns the controller while it's open, so firmware lizard
        # (kb/mouse) must stay OFF the whole time  otherwise the firmware
        # ALSO emits its own keys/clicks (D-pad→arrows, A→click/Enter) into the
        # focused window on top of the OSK reading the same buttons. In gamepad
        # mode the OSK opens with Steam+X (Steam held); releasing Steam used to
        # restore lizard ON, which then navigated and launched items in the
        # focused Start menu while the user typed. The device is opened lizard-
        # off (with a watchdog); re-assert OFF during a Steam hold (so a
        # Steam+VIEW=Alt+Tab chord isn't fought by a firmware Tab) and force it
        # back OFF  never ON  on release.
        if steam_now:
            if (not self._passive_lizard_suppressed
                    or now - self._last_lizard_suppress > 2.0):
                sc.set_lizard(False)
                self._passive_lizard_suppressed = True
                self._last_lizard_suppress = now
        elif self._passive_lizard_suppressed:
            sc.set_lizard(False)
            self._passive_lizard_suppressed = False

        # Backspace on the bound button (default X), handled here (not via
        # EventMapper) so holding it slow-repeats the delete. One delete on
        # press, then after BACKSPACE_HOLD_DELAY a delete every BACKSPACE_REPEAT
        # seconds. Gated off while Steam is held, since Steam+X opens the keyboard.
        x_pressed = bool(eff_buttons & backspace_bit) and not steam_now
        if x_pressed and not self._x_was_pressed:
            self._kb.pressEvent([sui.Keys.KEY_BACKSPACE])
            self._kb.releaseEvent([sui.Keys.KEY_BACKSPACE])
            self._x_repeat_at = now + self.BACKSPACE_HOLD_DELAY
        elif x_pressed and now >= self._x_repeat_at:
            self._kb.pressEvent([sui.Keys.KEY_BACKSPACE])
            self._kb.releaseEvent([sui.Keys.KEY_BACKSPACE])
            self._x_repeat_at = now + self.BACKSPACE_REPEAT
        self._x_was_pressed = x_pressed

        # Space on the bound button (default Y)  held while the button is held
        # so OS key-repeat works. R4 (RGRIP1) is a built-in extra Space trigger
        # unless the user reassigned R4 to another OSK function (r4_space_bit
        # is then 0).
        space_pressed = bool(eff_buttons & (space_bit | r4_space_bit))
        if space_pressed and not self._space_was_pressed:
            self._kb.pressEvent([sui.Keys.KEY_SPACE])
            self._space_active = True
        elif not space_pressed and self._space_was_pressed:
            self._kb.releaseEvent([sui.Keys.KEY_SPACE])
            self._space_active = False
        self._space_was_pressed = space_pressed

        # Steam + left stick → media transport (volume / track skip).
        self._handle_media_stick(sc_input, steam_now, now)

        # Left stick (no Steam) → move the on-screen-keyboard cursor.
        self._handle_kbd_stick(sc_input, steam_now, now)

        # Steam + VIEW ("-" on the Switch Pro / small button upper-right of the
        # Steam logo) → Alt+Tab. Hold Alt for the duration of the Steam hold so
        # the switcher stays visible; each VIEW rising edge taps Tab once to
        # advance one slot. Releasing Steam drops Alt and commits the selection.
        # Marks the Steam press as "used" so releasing Steam doesn't close the OSK.
        view_now = bool(eff_buttons & SCButtons.VIEW)
        if steam_now and view_now and not self._view_was_pressed:
            if not self._alt_held_for_tab:
                self._kb.pressEvent([sui.Keys.KEY_LEFTALT])
                self._alt_held_for_tab = True
            self._kb.pressEvent([sui.Keys.KEY_TAB])
            self._kb.releaseEvent([sui.Keys.KEY_TAB])
            self._saw_x_during_steam = True
        elif view_now and not self._view_was_pressed:
            state.request_position_cycle()
        self._view_was_pressed = view_now
        if not steam_now and self._alt_held_for_tab:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held_for_tab = False

        if (self._steam_was_pressed and not steam_now and not self._saw_x_during_steam
                and now - self._open_t > self._OPEN_CLOSE_GRACE):
            state.close()
        self._steam_was_pressed = steam_now

        if self.sc_input_previous == SCI_NULL:
            self.sc_input_previous = sc_input
            return

        # While Swipe Typing is on both pads address the WHOLE keyboard rather
        # than their own half (see _SWIPE_X_SCALAR)  pointer, highlight, taps
        # and the traced path all at once, so they can never disagree.
        #
        # This deliberately wins over Touch Typing's fixed halves when both are
        # enabled: tracing a word needs the whole keyboard under one thumb, so
        # Swipe Typing is simply non-functional on half a board, whereas Touch
        # Typing's other half  the fresh-touch snap and tap-to-type  works
        # perfectly well on the wider mapping. (Said plainly in its tooltip.)
        # Split layout: each pad addresses only its own half. Read the band
        # from the LIVE board (the renderer publishes it), so a size change or
        # a layout swap can never leave the pads mapped to a stale span.
        split_band = None
        if state.is_split_layout_enabled():
            _kb = state.get_virtual_kb()
            if _kb is not None:
                split_band = _kb.split_gap_band()
        if split_band is not None:
            band_left, band_right = split_band
            ptr_left_coords = CoordFraction.from_absolute(
                adjust_raw_x_span(sc_input.lpad_x, 0, band_left),
                adjust_raw_y(sc_input.lpad_y, 1/2))
            ptr_right_coords = CoordFraction.from_absolute(
                adjust_raw_x_span(sc_input.rpad_x, band_right, screen.width),
                adjust_raw_y(sc_input.rpad_y, 1/2))
        elif state.is_swipe_typing_enabled():
            ptr_left_coords = CoordFraction.from_absolute(
                adjust_raw_x(sc_input.lpad_x, _SWIPE_X_CENTER, _SWIPE_X_SCALAR),
                adjust_raw_y(sc_input.lpad_y, 1/2))
            ptr_right_coords = CoordFraction.from_absolute(
                adjust_raw_x(sc_input.rpad_x, _SWIPE_X_CENTER, _SWIPE_X_SCALAR),
                adjust_raw_y(sc_input.rpad_y, 1/2))
        else:
            ptr_left_coords = CoordFraction.from_absolute(
                adjust_raw_x(sc_input.lpad_x, _LPAD_X_CENTER),
                adjust_raw_y(sc_input.lpad_y, 1/2))
            ptr_right_coords = CoordFraction.from_absolute(
                adjust_raw_x(sc_input.rpad_x, _RPAD_X_CENTER),
                adjust_raw_y(sc_input.rpad_y, 1/2))

        # Trace/commit BEFORE handle_pad_input so a committed word can suppress
        # that pad's release-to-type (they are the same lift).
        self._swipe_step(SCButtons.LPADTOUCH, ptr_left_coords,
                         sc_input.buttons, SCButtons.LPAD, lpad_click_bit)
        self._swipe_step(SCButtons.RPADTOUCH, ptr_right_coords,
                         sc_input.buttons, SCButtons.RPAD, rpad_click_bit)

        # Press-to-focus: pressing a pad down (or pulling its trigger past the
        # focus point) drags the thumb a little, which is exactly enough to
        # slide off the key that was under the cursor. Freeze the pointer on
        # that key's centre for the duration of the press so the click lands
        # where it looked like it would. Applied BEFORE handle_pad_input, so
        # the click it queues carries the frozen coordinate too.
        ptr_left_coords = self._focus_lock_coords(
            "l", ptr_left_coords, sc_input.lpad_force, lpad_touched,
            sc_input.ltrig, l_click_pressed and self._lpad_click_mode)
        ptr_right_coords = self._focus_lock_coords(
            "r", ptr_right_coords, sc_input.rpad_force, rpad_touched,
            sc_input.rtrig, r_click_pressed and self._rpad_click_mode)

        input_state_left = self.handle_pad_input(ptr_left_coords, sc_input.buttons,
                                                 SCButtons.LPADTOUCH, lpad_click_bit,
                                                 click_button_mask=SCButtons.LPAD,
                                                 allow_click=self._lpad_click_mode,
                                                 now=now,
                                                 trigger_pressed=l_click_pressed,
                                                 trigger_prev=l_click_was,
                                                 trigger_bit=lpad_click_bit,
                                                 raw_x=sc_input.lpad_x)
        input_state_right = self.handle_pad_input(ptr_right_coords, sc_input.buttons,
                                                  SCButtons.RPADTOUCH, rpad_click_bit,
                                                  click_button_mask=SCButtons.RPAD,
                                                  allow_click=self._rpad_click_mode,
                                                  now=now,
                                                  trigger_pressed=r_click_pressed,
                                                  trigger_prev=r_click_was,
                                                  trigger_bit=rpad_click_bit,
                                                  raw_x=sc_input.rpad_x)

        ptr_left = vptr.VirtualPointer(input_state_left, ptr_left_coords)
        ptr_right = vptr.VirtualPointer(input_state_right, ptr_right_coords)

        # "Touch Typing": a FRESH touch lands the pointer exactly where the
        # thumb did, like the glass on a phone. The smoothing below is a
        # low-pass toward the previous frame's position, so without this the
        # pointer glides in from wherever it was last left  which is the one
        # thing that stops the pads from feeling like fixed 1:1 maps of the
        # keyboard. Only the first frame of a touch skips it; tracking
        # afterwards stays smoothed, because the pad is far noisier than a
        # fingertip on glass and unfiltered tracking jitters.
        touch_typing = state.is_touch_typing_enabled()
        prev_buttons = self.sc_input_previous.buttons
        l_fresh = lpad_touched and not (prev_buttons & SCButtons.LPADTOUCH)
        r_fresh = rpad_touched and not (prev_buttons & SCButtons.RPADTOUCH)
        # A pointer under the press-to-focus lock uses the faster glide alpha
        # instead: its target is a fixed key centre, so it should settle onto
        # it briskly rather than crawl there at the tracking low-pass.
        _glide = state.get_pad_press_focus()[2]
        l_alpha = _glide if "l" in self._focus_lock else self.pad_smoothing
        r_alpha = _glide if "r" in self._focus_lock else self.pad_smoothing
        if not (touch_typing and l_fresh):
            ptr_left.smoothen(self.prev_ptr_left, l_alpha)
        if not (touch_typing and r_fresh):
            ptr_right.smoothen(self.prev_ptr_right, r_alpha)
        self.prev_ptr_left = copy.deepcopy(ptr_left)
        self.prev_ptr_right = copy.deepcopy(ptr_right)
        self.sc_input_previous = sc_input

        self.controller_state.set_pointers(ptr_left, ptr_right)

    def _focus_lock_coords(self, side, coords, press, touched, trig_analog,
                           click_held):
        """Freeze `coords` on the centre of the key being pressed while the
        press lasts, or hand them back untouched.

        Engages when the pad force crosses the hold threshold, or when the
        trigger's analog pull passes the focus point (the click itself still
        fires at its own actuation threshold  this only decides when the AIM
        locks). Stays engaged, on the same key, until the force falls back
        below the release threshold and the button is up, so the whole press
        lands on one key."""
        if not state.is_press_focus_enabled():
            self._focus_lock.pop(side, None)
            return coords
        hold, release, _glide = state.get_pad_press_focus()
        target = self._focus_lock.get(side)
        pressing = touched and press >= release
        if not (pressing or click_held):
            self._focus_lock.pop(side, None)
            return coords
        if target is not None:
            return target
        # Rising edge: only latch once the press is deliberate  past the hold
        # force, or past the trigger's focus pull.
        if not (click_held or press >= hold
                or trig_analog >= state.get_trigger_focus_pull()):
            return coords
        kb = state.get_virtual_kb()
        if kb is None:
            return coords
        x, y = coords.to_absolute()
        rc = kb.find_key_expanded_rc(x, y)
        if rc is None:
            return coords
        layout = kb.get_key_layout(*rc)
        if layout is None:
            return coords
        target = CoordFraction.from_absolute(
            int(layout.x + layout.w // 2), int(layout.y + layout.h // 2))
        self._focus_lock[side] = target
        return target

    def release_held(self):
        """Release anything we're holding on the OS side so closing the OSK
        (which tears down this manager) can never strand a key or  worse  a
        mouse button down. Called from input_thread's finally.

        Also tears down a live Select session (its Shift is held by us) and
        closes any open accent row, so neither survives into the next open."""
        if self._select_pad is not None:
            self._end_select(time.monotonic())
        self._diacritic_pad = None
        self._deferred_base.clear()
        state.close_diacritic()
        if self._mouse_l_active:  # L2 holds the RIGHT button (swapped)
            self._mouse.release("right")
            self._mouse_l_active = False
        if self._mouse_r_active:  # R2 holds the LEFT button (swapped)
            self._mouse.release("left")
            self._mouse_r_active = False
        if self._shift_active:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = False
        if self._enter_active:
            self._kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._enter_active = False
        if self._space_active:
            self._kb.releaseEvent([sui.Keys.KEY_SPACE])
            self._space_active = False


def update(sc, sc_input, manager):
    if state.should_close():
        # Adusk is shutting down  tell the controller thread to exit so it
        # can run its cleanup (re-enable lizard mode) before being killed.
        sc.addExit()
        return
    if sc_input.status != SCStatus.INPUT:
        return
    manager.handle_input(sc, sc_input)


# Delay between controller (re)connect attempts while the keyboard is open but
# no controller is responding. Only ticks in that transient state; once a
# controller is open sc.run() blocks (no polling), and a closed keyboard isn't
# running this thread at all.
_RECONNECT_DELAY = 0.5


# Poll/merge cadence. The Steam Controller still streams at its own HID rate
# (SteamHidSource stashes its latest frame on its own thread); this loop reads
# every source, OR-merges, and dispatches one combined frame. ~250 Hz keeps the
# touchpad-pointer and haptic latency low without busy-spinning  but only
# while the controller is actually in use: after _MERGE_IDLE_GRACE with no
# button/touch/stick/trigger input the loop settles to ~30 Hz (the render
# loop's idle pace) so an open-but-untouched OSK doesn't wake this thread
# 250x/sec. The next touch/press is caught within one ≤33 ms tick, which
# snaps the pace straight back up  a pad touch (button bit) always precedes
# the actual key press, so typing never sees the idle latency.
_MERGE_INTERVAL = 0.004
_MERGE_IDLE_INTERVAL = 1.0 / 30
_MERGE_IDLE_GRACE = 0.4
# Analog trigger pull (0..32767) counting as activity  well below the OSK's
# digital-on threshold, so a slow Shift/Enter pull re-arms the fast pace
# before it actuates.
_MERGE_TRIG_ACTIVE = 4000
# Gyro angular velocity (raw int16 units, ~16.4/°/s) counting as activity, so
# gyro-steering the OSK pointer holds the fast merge pace with no buttons held
# (~2 °/s  above rest noise, well below deliberate motion).
_MERGE_GYRO_ACTIVE = 33


def input_thread(controller_state, closing_haptic=False, preview=False):
    manager = ControllerManager(controller_state)
    # Input sources merged into one stream: the custom Steam Controller hidapi
    # driver (trackpads, tuned haptics, lizard) PLUS every SDL-recognized pad
    # (Xbox, DualSense, Switch Pro, ...). Both synthesize SteamControllerInput;
    # InputMerger OR-merges them and is the `sc` facade handle_input drives
    # (set_lizard + the two haptic ticks fan out to every source). With no SDL
    # pad attached the merged frame equals the Steam Controller's exactly, so
    # the proven SC-only path is unchanged. The merger's sources self-reconnect,
    # so a controller plugged in mid-session starts working without reopening.
    merger = inputsrc.InputMerger()
    merger.add(inputsrc.SteamHidSource())
    # SDL pads (Xbox/DualSense/Switch Pro) are read by the tray's one
    # sdl_gamepad_thread and published via state.set_sdl_frame(); adusk just
    # consumes those frames. Opening a second Sdl3GamepadSource here double-drove
    # the same pad across two threads and delivered no input.
    merger.add(inputsrc.SharedSdlFrameSource())
    # Expose haptic "ticks" to the main thread (dispatch_key buzzes on each key
    # press; the stronger pad-click tick for the simulated trackpad click).
    # Cleared on exit so a closed device's haptic methods are never called.
    state.set_haptic_tick(merger.haptic_click)
    state.set_pad_click_haptic(merger.haptic_pad_click)
    # L2/R2 actuation feedback, routed per controller kind (only analog-trigger
    # kinds buzz  see inputsrc haptic_trigger_click).
    state.set_trigger_haptic(merger.haptic_trigger_click)
    try:
        last_active = time.monotonic()
        while not state.should_close():
            merged = merger.poll()
            if merged is not None:
                if preview:
                    # A live Options-tab preview (Size/Transparency/Skin) is a
                    # passive render driven from the picker: the user is steering
                    # the PICKER with the controller, so the OSK must IGNORE all
                    # controller input (never call update()  no typing / nav).
                    # While this preview OSK is up the tray's own sc.run() is
                    # paused (it handed us the SC), so FORWARD each frame to the
                    # picker's nav channel (same slot the tray publishes) so it
                    # can keep navigating the slider/dropdown live.
                    if _sc_viewer is not None:
                        try:
                            _sc_viewer.publish(merged)
                        except Exception:
                            pass
                else:
                    update(merger, merged, manager)
                # Any button/touch bit, stick push, or trigger pull holds the
                # fast pace (touchpad touches set button bits, so pointer
                # moves count as activity too).
                if (inputsrc._frame_has_activity(merged)
                        or merged.ltrig > _MERGE_TRIG_ACTIVE
                        or merged.rtrig > _MERGE_TRIG_ACTIVE
                        or abs(merged.gyaw) > _MERGE_GYRO_ACTIVE
                        or abs(merged.gpitch) > _MERGE_GYRO_ACTIVE):
                    last_active = time.monotonic()
            time.sleep(_MERGE_INTERVAL
                       if (time.monotonic() - last_active) < _MERGE_IDLE_GRACE
                       else _MERGE_IDLE_INTERVAL)
    finally:
        # Drop any OS key / mouse button we were holding before tearing down,
        # so closing the OSK mid-pull can't strand (e.g.) the left mouse button.
        manager.release_held()
        # Haptic confirmation that the OSK closed  fired here, BEFORE the
        # hooks are cleared and the sources torn down, because by the time
        # tray.py's launcher_thread sees adusk.main() return, this thread has
        # already cleared state.set_haptic_tick(None) and closed the merger, so
        # a haptic_tick() call from the tray side is always a silent no-op.
        # Skipped for a live Size/Transparency preview open (closing_haptic is
        # only True for a real close).
        if closing_haptic:
            try:
                merger.haptic_click()
            except Exception:
                pass
        state.set_haptic_tick(None)
        state.set_pad_click_haptic(None)
        state.set_trigger_haptic(None)
        # Stops the SDL pads and signals the Steam Controller thread to exit so
        # its cleanup (re-enable lizard mode) runs before we return.
        merger.close()
