"""Multi-controller input sources for the on-screen keyboard.

The custom Steam Controller hidapi driver (steamcontroller/) stays the primary
backend. This adds an SDL3 backend so ANY SDL-recognized pad (Xbox, DualSense,
Switch Pro, 8BitDo, ...) drives the same OSK. Every active source is polled each
frame and OR-merged into ONE SteamControllerInput, which the existing
ControllerManager.handle_input consumes unchanged.

INVARIANT: when no SDL pad is connected, the merged frame equals the Steam
Controller's frame exactly (OR with 0 / max with 0 / untouched-pad), so the
proven Steam-Controller-only path never changes behavior.

Each source exposes:
    poll()            -> latest SteamControllerInput, or None when it has no
                         live device (so the merger can skip it and, when ALL
                         sources are idle, the input loop does no work)
    set_lizard(bool)  -> firmware kb/mouse toggle (no-op for generic pads)
    haptic_click()    -> light "key tap" feedback
    haptic_pad_click()-> firmer "select" feedback
    haptic_trigger_click() -> L2/R2 actuation feedback (analog-trigger kinds
                         only  a digital trigger already clicks mechanically)
    addExit()/close() -> teardown
An InputMerger fans set_lizard/haptics out to every source and presents the same
interface handle_input expects from a SteamController (the `sc` argument).
"""

import math
import time
from threading import Lock, Thread

import sdl3w as S
from steamcontroller import (SteamController, SCButtons, SCStatus, SCI_NULL,
                             SteamControllerInput, GYRO_DEG_PER_SEC)

from adusk import state

# Controller catalog (kind identification + per-kind metadata). Defensive so a
# stripped-down bundle without pads.py still runs with the generic behavior.
try:
    import pads as _pads
except Exception:
    _pads = None

# Nintendo Bluetooth guard  pacing policy for the Switch Pro / Joy-Con
# ~20-minute firmware dropout (see nintendo_bt.py for the full story). Optional
# for the same reason pads is.
try:
    import nintendo_bt as _nbt
except Exception:
    _nbt = None


# Reconnect cadence for a dropped/absent Steam Controller (mirrors the old
# input_thread loop).
_RECONNECT_DELAY = 0.5


def _clamp16(v):
    return -32767 if v < -32767 else 32767 if v > 32767 else v


# Stick magnitude (of 32767) above which a frame counts as "actively in use".
# Comfortably above resting drift (~3000) but below an intentional push.
_ACTIVITY_STICK = 8000
# Analog trigger pull (0..32767) counting as activity  below every digital
# actuation threshold (the lightest, the SC's "low", engages at 3000), so a
# lowered-actuation Shift/Enter pull (which can engage without ever setting
# the firmware LT/RT bit) marks its controller as the haptic owner BEFORE the
# engage tick fires.
_ACTIVITY_TRIG = 2500
# Gyro angular velocity (raw int16 units, ~16.4/°/s) counting as activity
# (~2 °/s  above rest noise, well below deliberate motion). Without this, a
# gyro-steered OSK pointer (no buttons/sticks held) never marked its source
# active, so the key-switch haptic fell back to fanning out and buzzed the
# idle Steam Controller instead of the pad actually being waved around.
_ACTIVITY_GYRO = 33


def _frame_has_activity(f):
    """True if this input frame shows the controller is actively being used
    (any button, trigger pull, stick pushed past the deadzone, or gyro motion
    while its stream is on). Used to decide which controller 'owns' the
    current interaction so haptics go only to it."""
    if f.buttons:
        return True
    if f.ltrig > _ACTIVITY_TRIG or f.rtrig > _ACTIVITY_TRIG:
        return True
    if abs(f.gyaw) > _ACTIVITY_GYRO or abs(f.gpitch) > _ACTIVITY_GYRO:
        return True
    return (abs(f.lstick_x) > _ACTIVITY_STICK or abs(f.lstick_y) > _ACTIVITY_STICK
            or abs(f.rstick_x) > _ACTIVITY_STICK or abs(f.rstick_y) > _ACTIVITY_STICK)


def merge_inputs(a, b):
    """OR-merge two SteamControllerInput frames into one. Buttons OR together,
    triggers take the max, each stick takes the larger-magnitude source, and a
    trackpad's coordinates come from whichever source is actively touching it
    (`a` wins ties  pass the Steam Controller as `a` so its tuned pads lead)."""
    buttons = a.buttons | b.buttons
    ltrig = a.ltrig if a.ltrig >= b.ltrig else b.ltrig
    rtrig = a.rtrig if a.rtrig >= b.rtrig else b.rtrig
    lstick_x = a.lstick_x if abs(a.lstick_x) >= abs(b.lstick_x) else b.lstick_x
    lstick_y = a.lstick_y if abs(a.lstick_y) >= abs(b.lstick_y) else b.lstick_y
    rstick_x = a.rstick_x if abs(a.rstick_x) >= abs(b.rstick_x) else b.rstick_x
    rstick_y = a.rstick_y if abs(a.rstick_y) >= abs(b.rstick_y) else b.rstick_y
    if (b.buttons & SCButtons.LPADTOUCH) and not (a.buttons & SCButtons.LPADTOUCH):
        lpad_x, lpad_y, lpad_force = b.lpad_x, b.lpad_y, b.lpad_force
    else:
        lpad_x, lpad_y, lpad_force = a.lpad_x, a.lpad_y, a.lpad_force
    if (b.buttons & SCButtons.RPADTOUCH) and not (a.buttons & SCButtons.RPADTOUCH):
        rpad_x, rpad_y, rpad_force = b.rpad_x, b.rpad_y, b.rpad_force
    else:
        rpad_x, rpad_y, rpad_force = a.rpad_x, a.rpad_y, a.rpad_force
    # Gyro rides the larger-magnitude source per axis (like the sticks)  only
    # controllers with gyro-to-mouse toggled on stream nonzero values, so this
    # just carries the active gyro through the merge.
    gpitch = a.gpitch if abs(a.gpitch) >= abs(b.gpitch) else b.gpitch
    gyaw = a.gyaw if abs(a.gyaw) >= abs(b.gyaw) else b.gyaw
    groll = a.groll if abs(a.groll) >= abs(b.groll) else b.groll
    return SteamControllerInput(
        status=SCStatus.INPUT, seq=a.seq, buttons=buttons,
        ltrig=ltrig, rtrig=rtrig,
        lpad_x=lpad_x, lpad_y=lpad_y, rpad_x=rpad_x, rpad_y=rpad_y,
        lstick_x=lstick_x, lstick_y=lstick_y,
        rstick_x=rstick_x, rstick_y=rstick_y,
        lpad_force=lpad_force, rpad_force=rpad_force,
        gpitch=gpitch, gyaw=gyaw, groll=groll)


class SteamHidSource:
    """The custom Steam Controller hidapi driver, made pollable. Runs
    SteamController.run() on its own thread (with reconnect), stashing the
    latest input frame for the merge loop to read. Haptics/lizard forward to
    the live device; teardown lets run()'s cleanup restore lizard mode."""

    # No input frame for this long => treat the device as released/gone.
    STALE_AFTER = 1.0

    @property
    def controller_kind(self):
        """This source's controller family for the OSK glyph swap (see
        InputMerger.poll()): the live device's kind  "sc", "sc2015" or
        "steam_deck", all driven by the same hidapi backend  defaulting to
        "sc" before a device opens."""
        sc = self._live()
        return getattr(sc, "kind", None) or "sc"

    def __init__(self):
        self._lock = Lock()
        self._sc = None
        self._latest = SCI_NULL
        self._latest_t = 0.0
        self._exit = False
        # Last IMU (gyro stream) state pushed to the device  the OSK's gyro
        # trackpad-circle pointer needs the stream on while "Gyro To Mouse" is
        # toggled for this HID family; poll() follows the shared state so a
        # toggle flips it live. Reset per device (a fresh open starts IMU-off).
        self._imu_want = False
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _on_frame(self, sc, sci):
        # Runs on the SteamController read thread, once per HID input frame.
        with self._lock:
            self._latest = sci
            self._latest_t = time.monotonic()

    def _run_loop(self):
        while not self._exit and not state.should_close():
            sc = SteamController(callback=self._on_frame)
            with self._lock:
                self._sc = sc
                self._imu_want = False  # fresh device opens with IMU off
            try:
                sc.run()  # blocks until the device drops or addExit() fires
            except Exception as e:
                print(f"SteamHidSource: run error: {e!r}")
            finally:
                with self._lock:
                    self._sc = None
                    self._latest = SCI_NULL
            if self._exit or state.should_close():
                break
            time.sleep(_RECONNECT_DELAY)

    def poll(self):
        with self._lock:
            sc = self._sc
            if sc is None:
                return None
            if time.monotonic() - self._latest_t > self.STALE_AFTER:
                return None
            latest = self._latest
        # Follow the shared gyro-to-mouse state: switch the device's IMU
        # stream on/off on change (one feature report per flip  cheap check
        # otherwise), so the OSK's gyro pointer has live gyro fields.
        # "Always Type With Gyro" counts too: it steers the OSK pointer without
        # ever touching the shared desktop gyro-mouse state, and the IMU has to
        # be streaming for it. Left on for the session even if the gyro hotkey
        # turns typing back off  re-arming costs a feature report, the stream
        # costs a few HID bytes.
        want = (state.is_gyro_mouse_active(getattr(sc, "kind", None) or "sc")
                or state.is_kbd_gyro_always())
        if want != self._imu_want:
            self._imu_want = want
            try:
                sc.set_imu(want)
            except Exception:
                pass
        return latest

    def _live(self):
        with self._lock:
            return self._sc

    def set_lizard(self, enabled):
        sc = self._live()
        if sc is not None:
            sc.set_lizard(enabled)

    def haptic_click(self):
        sc = self._live()
        if sc is not None:
            sc.haptic_click()

    def haptic_pad_click(self):
        sc = self._live()
        if sc is not None:
            sc.haptic_pad_click()

    def haptic_trigger_click(self):
        # L2/R2 actuation feedback. The SC's / Deck's triggers are analog, so
        # the tick IS the click  same strong tick as the simulated pad click.
        sc = self._live()
        if sc is not None:
            sc.haptic_pad_click()

    def addExit(self):
        sc = self._live()
        if sc is not None:
            sc.addExit()

    def close(self):
        self._exit = True
        self.addExit()
        self._thread.join(timeout=1.0)


# SDL gamepad button -> Steam Controller button bit. Uses the SCButtons VALUES
# so a synthesized frame is bit-identical to a real Triton frame: both the OSK
# (which reads SCButtons names) and the ViGEm bridge (which reads the matching
# C++ byte positions) then treat an SDL pad exactly like the Steam Controller.
_SDL_TO_SC = [
    (S.SDL_GAMEPAD_BUTTON_SOUTH, SCButtons.A),
    (S.SDL_GAMEPAD_BUTTON_EAST,  SCButtons.B),
    (S.SDL_GAMEPAD_BUTTON_WEST,  SCButtons.X),
    (S.SDL_GAMEPAD_BUTTON_NORTH, SCButtons.Y),
    (S.SDL_GAMEPAD_BUTTON_BACK,  SCButtons.VIEW),
    (S.SDL_GAMEPAD_BUTTON_START, SCButtons.START),
    (S.SDL_GAMEPAD_BUTTON_GUIDE, SCButtons.STEAM),
    (S.SDL_GAMEPAD_BUTTON_LEFT_STICK,  SCButtons.L3),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_STICK, SCButtons.R3),
    (S.SDL_GAMEPAD_BUTTON_LEFT_SHOULDER,  SCButtons.LB),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER, SCButtons.RB),
    (S.SDL_GAMEPAD_BUTTON_DPAD_UP,    SCButtons.DPAD_UP),
    (S.SDL_GAMEPAD_BUTTON_DPAD_DOWN,  SCButtons.DPAD_DOWN),
    (S.SDL_GAMEPAD_BUTTON_DPAD_LEFT,  SCButtons.DPAD_LEFT),
    (S.SDL_GAMEPAD_BUTTON_DPAD_RIGHT, SCButtons.DPAD_RIGHT),
    # Back paddles -> grips (close / space), matching the SC paddle bindings.
    (S.SDL_GAMEPAD_BUTTON_LEFT_PADDLE1,  SCButtons.LGRIP1),
    (S.SDL_GAMEPAD_BUTTON_LEFT_PADDLE2,  SCButtons.LGRIP2),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_PADDLE1, SCButtons.RGRIP1),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_PADDLE2, SCButtons.RGRIP2),
    # MISC1 (Switch Capture / DualSense mute / handheld extra button) -> the
    # spare QAM bit, making the "capture" cid a bindable desktop/chord button.
    (S.SDL_GAMEPAD_BUTTON_MISC1, SCButtons.QAM),
    # Touchpad CLICK (DualShock 4 / DualSense / Legion Go) -> the left trackpad
    # bit, which no SDL pad otherwise uses. This is a SEPARATE SDL button from
    # MISC1 above: a DS4 reports its pad click here and never sets MISC1, while
    # a DualSense sets MISC1 for Mute and this for the pad click. Backs the
    # "touchpad" chord cid (keybinds_runtime.SDL_CHORD_BUTTONS).
    (S.SDL_GAMEPAD_BUTTON_TOUCHPAD, SCButtons.LPAD),
]

# Trigger pull (0..32767) at/above which the LT/RT *digital* bit engages  the
# OSK uses it for Shift (L2) / Enter (R2) / select. The analog ltrig/rtrig is
# always carried too (for the ViGEm Xbox triggers).
_TRIGGER_DIGITAL_ON = 12000


class Sdl3GamepadSource:
    """Polls every SDL-recognized gamepad and synthesizes a SteamControllerInput
    (OR-merged across multiple pads). No trackpad-pointer synthesis  SDL pads
    drive the OSK via DPAD/stick navigation + A to press, triggers for
    Shift/Enter, X=Backspace, Y=Space (the trackpad pointer stays a Steam
    Controller exclusive)."""

    def __init__(self):
        self._pads = {}          # instance_id -> SDL_Gamepad*
        # instance_id -> controller kind from the pads.py catalog ("switch",
        # "xbox", "ps5", "steam_deck", ...)  identified at rescan from the
        # pad's name/VID/PID (+ the machine itself for handheld built-ins).
        self._pad_kinds = {}
        # Which trigger-actuation threshold family _read_pad applies: "osk"
        # while the on-screen keyboard consumes the LT/RT digital bits,
        # "mouse" for the desktop trigger clicks. Set by the tray on mode
        # changes; per-kind values live in state.get_sdl_trigger_threshold.
        self.trigger_mode = "osk"
        self._next_scan = 0.0
        self._available = True
        # instance_id -> monotonic time that pad last had a button/trigger/stick
        # active. Used to target key-press haptics at the pad actually being
        # used, so a second connected controller doesn't buzz when the first one
        # types. See _active_gamepad / haptic_click.
        self._pad_active_t = {}
        # Gyro-to-mouse: controller kinds whose pads should stream gyro data
        # (set from the tray thread via set_gyro_kinds; a frozenset swap is
        # atomic). Enable/disable is applied on the SDL thread in _pump 
        # sensor streaming costs battery/CPU, so it's on only while that
        # kind's "Gyro To Mouse" is toggled on. _pad_gyro_on tracks what's
        # currently enabled per pad; _pad_has_gyro caches HasSensor per pad.
        self._gyro_want = frozenset()
        self._gyro_applied = None      # last want-set applied (None = force)
        self._pad_gyro_on = {}
        self._pad_has_gyro = {}
        # --- Nintendo Bluetooth guard (see nintendo_bt.py) -------------------
        # Switch Pro / Joy-Cons drop off Bluetooth after ~20 min because rumble
        # traffic + the IMU reports the rumble itself provokes saturate the
        # sniff-mode link they put every non-Nintendo host into. For those pads
        # (and ONLY those, on Bluetooth) we pace our rumble output reports
        # through a per-pad governor and trickle a no-op packet during idle to
        # hold the link up. On USB-C none of this applies and the guard is off.
        self._bt_safe = True           # setting, tray-driven
        # "Rotate Single Joy-Con Stick" (Options → Joy-Cons). Applies to lone
        # Joy-Cons only, in _read_pad. Tray-driven; a plain bool swap is atomic
        # so the SDL thread can read it without a lock.
        self._joycon_stick_rotate = False
        self._pad_governors = {}       # jid -> nintendo_bt.RumbleGovernor
        self._pad_guarded = {}         # jid -> guard applies to this pad
        # jid -> stable identity (serial / name) so the tray can match a pad
        # that reconnects to the one that dropped, and (jid, uid, kind) drop
        # events for the tray's dropout notice.
        self._pad_uids = {}
        self._drop_events = []
        self._drop_lock = Lock()
        # adusk.main initializes SDL with SDL_INIT_GAMEPAD before the input
        # thread starts; if that ever fails, every SDL call below no-ops.

    def _rescan(self):
        try:
            pads = S.list_gamepads_ex()  # [(instance_id, name, vid, pid, type)]
        except Exception:
            return
        # Exclude the Steam Controller: the custom hidapi driver owns it, and
        # letting SDL open it too would double-drive the same device and fight
        # over the HID handle. Generic pads  Xbox/DualSense/etc.  are kept,
        # and so is the STEAM DECK's built-in pad (a normal SDL gamepad to us).
        names = {jid: (name or "") for jid, name, _v, _p, _t in pads
                 if not ("steam" in (name or "").lower()
                         and "deck" not in (name or "").lower())}
        # Identify each pad's controller kind (drives the OSK glyph family,
        # per-kind settings, and the seen-controllers unlock).
        if _pads is not None:
            machine = _pads.machine_kind()
            for jid, name, vid, pid, typ in pads:
                if jid in names and jid not in self._pad_kinds:
                    try:
                        self._pad_kinds[jid] = _pads.identify(
                            name, vid, pid, typ, machine=machine)
                    except Exception:
                        self._pad_kinds[jid] = "generic"
        ids = set(names)
        for jid in list(self._pads):
            if jid not in ids:
                g = self._pads[jid]
                # Best-effort: clear the Home LED as the pad leaves so a
                # controller that lit blue for gamepad mode doesn't keep the LED
                # stuck on. NOTE: once a controller is physically detached SDL
                # refuses LED writes to it, so this only lands when the pad is
                # still reachable (a soft/transient drop); a hard yank can't be
                # cleared remotely  the controller holds it until it sleeps or
                # reconnects, at which point _home_led_applied below re-applies.
                try:
                    S.SDL_SetGamepadLED(g, 0, 0, 0)
                except Exception:
                    pass
                try:
                    S.SDL_CloseGamepad(g)
                except Exception:
                    pass
                del self._pads[jid]
                self._pad_active_t.pop(jid, None)
                kind = self._pad_kinds.pop(jid, None)
                self._pad_gyro_on.pop(jid, None)
                self._pad_has_gyro.pop(jid, None)
                self._pad_governors.pop(jid, None)
                guarded = self._pad_guarded.pop(jid, False)
                # The uid mapping deliberately OUTLIVES the disconnect: the
                # tray doesn't see the drop until its next poll, and by then it
                # still has to ask uid_of(jid) who left in order to match the
                # reconnecting controller to the one that dropped. Pruned
                # instead of popped.
                uid = self._pad_uids.get(jid)
                self._prune_pad_uids()
                # Publish the drop so the tray can react (a guarded Nintendo
                # pad gets the one-per-session "this is firmware" notice).
                with self._drop_lock:
                    self._drop_events.append((jid, uid, kind, guarded))
                    del self._drop_events[:-8]
                print(f"Sdl3GamepadSource: closed gamepad {jid}")
        for jid in ids:
            if jid not in self._pads:
                try:
                    g = S.SDL_OpenGamepad(jid)
                except Exception:
                    g = None
                if g:
                    self._pads[jid] = g
                    print(f"Sdl3GamepadSource: opened gamepad {jid} ({names[jid]})")
                    # A freshly-(re)connected pad starts with sensors off;
                    # force _pump to re-apply the wanted gyro streaming.
                    self._gyro_applied = None
                    try:
                        self._pad_has_gyro[jid] = bool(
                            S.SDL_GamepadHasSensor(g, S.SDL_SENSOR_GYRO))
                    except Exception:
                        self._pad_has_gyro[jid] = False
                    self._pad_uids[jid] = self._pad_uid(g, jid, names[jid])
                    self._classify_guard(jid, g)

    def _pad_uid(self, g, jid, name):
        """A pad's identity ACROSS reconnects. SDL instance ids are fresh every
        connect, so the serial (the Bluetooth MAC on a Switch Pro) is what lets
        the tray recognise a returning controller. Falls back to the name when
        a backend exposes no serial."""
        serial = None
        try:
            serial = S.gamepad_serial(g)
        except Exception:
            serial = None
        if serial:
            return "sn:" + serial
        return "nm:%s" % (name or jid)

    def _classify_guard(self, jid, g):
        """Does the Nintendo Bluetooth guard apply to this pad? Only Nintendo
        kinds, only on a wireless link, only while the setting is on."""
        guarded = False
        if self._bt_safe and _nbt is not None:
            kind = self._pad_kinds.get(jid, "")
            if _nbt.is_nintendo(kind):
                try:
                    guarded = bool(S.gamepad_is_wireless(g))
                except Exception:
                    guarded = True
        self._pad_guarded[jid] = guarded
        if guarded:
            self._pad_governors[jid] = _nbt.RumbleGovernor()
            print(f"Sdl3GamepadSource: Nintendo Bluetooth guard on for {jid}")
        else:
            self._pad_governors.pop(jid, None)

    def set_bt_safe(self, on):
        """Options → Switch Pro "Bluetooth Safe Mode". Re-classifies every open
        pad so the toggle takes effect without a reconnect."""
        on = bool(on)
        if on == self._bt_safe:
            return
        self._bt_safe = on
        for jid, g in list(self._pads.items()):
            self._classify_guard(jid, g)

    def _prune_pad_uids(self, keep=8):
        """Forget all but the `keep` most recent uids of pads that are no
        longer connected (live pads are never dropped)."""
        stale = [j for j in self._pad_uids if j not in self._pads]
        for j in stale[:-keep]:
            del self._pad_uids[j]

    def set_joycon_stick_rotate(self, on):
        """Options → Joy-Cons "Rotate Single Joy-Con Stick". Applies LIVE  it
        is a judgement the user makes with the controller in hand, so it must
        take effect while they hold it, not on the next launch."""
        self._joycon_stick_rotate = bool(on)

    def uid_of(self, jid):
        """Stable cross-reconnect identity for one SDL instance id (or None).
        Still answers for a RECENTLY DISCONNECTED pad  that is the whole
        point: it is what a dropped controller is matched back by."""
        return self._pad_uids.get(jid)

    def take_drop_events(self):
        """Pop the (jid, uid, kind, was_guarded) tuples for pads that have
        disconnected since the last call. Drained by the tray thread."""
        with self._drop_lock:
            out = self._drop_events
            self._drop_events = []
        return out

    def _read_pad(self, g, kind=None):
        buttons = 0
        for sdl_btn, sc_bit in _SDL_TO_SC:
            try:
                if S.SDL_GetGamepadButton(g, sdl_btn):
                    buttons |= sc_bit
            except Exception:
                pass
        lt = S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_LEFT_TRIGGER)
        rt = S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_RIGHT_TRIGGER)
        lt = lt if lt > 0 else 0
        rt = rt if rt > 0 else 0
        # Per-kind trigger actuation (each analog-trigger controller's Options
        # category): the pull at which LT/RT's DIGITAL bit engages. None =
        # the built-in default. Which family applies (OSK typing vs desktop
        # mouse) is the tray-set trigger_mode.
        thr = state.get_sdl_trigger_threshold(kind, self.trigger_mode)
        if thr is None:
            thr = _TRIGGER_DIGITAL_ON
        if lt >= thr:
            buttons |= SCButtons.LT
        if rt >= thr:
            buttons |= SCButtons.RT
        lx = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_LEFTX))
        ly = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_LEFTY))
        rx = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_RIGHTX))
        ry = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_RIGHTY))
        # Hardware sticks are +up; SDL reports +down  invert Y so the OSK and
        # the ViGEm/XInput bridge both see the right vertical sign.
        lsx, lsy = lx, -ly
        # "Rotate Single Joy-Con Stick": a lone Joy-Con is played sideways, so
        # whether its stick's "up" matches the user's "up" depends on a quarter
        # turn that the driver may or may not already have applied. Corrected
        # HERE, at the frame source, so every consumer downstream  desktop
        # cursor, on-screen-keyboard navigation, the virtual gamepad, gyro 
        # sees one consistently oriented stick.
        if self._joycon_stick_rotate and _pads is not None:
            side = _pads.single_joycon_side(kind)
            if side == "l":          # rail edge up = a quarter turn CCW
                lsx, lsy = -lsy, lsx
            elif side == "r":        # ...and the other half turns the other way
                lsx, lsy = lsy, -lsx
        return SteamControllerInput(
            status=SCStatus.INPUT, seq=0, buttons=buttons,
            ltrig=lt, rtrig=rt,
            lpad_x=0, lpad_y=0, rpad_x=0, rpad_y=0,
            lstick_x=lsx, lstick_y=lsy, rstick_x=rx, rstick_y=-ry)

    def _pump(self):
        """Shared per-frame SDL housekeeping for poll()/poll_all(): refresh
        gamepad state and periodically rescan for (dis)connects. Returns the
        monotonic 'now', or None when SDL is unavailable / the update failed."""
        if not self._available:
            return None
        try:
            # SDL_UpdateGamepads only refreshes already-open pads; it does not
            # run udev hotplug detection. Without SDL_PumpEvents, a controller
            # plugged in after this source was created never appears in
            # SDL_GetGamepads() (list_gamepads() in _rescan), so it's invisible
            # until the program is restarted. Pumping here makes hotplugs show
            # up live.
            S.SDL_PumpEvents()
            S.SDL_UpdateGamepads()
        except Exception:
            return None
        now = time.monotonic()
        if now >= self._next_scan:
            self._next_scan = now + 0.5
            self._rescan()
        # Apply a pending gyro-streaming change (cheap no-op when the wanted
        # kind-set hasn't moved; also re-runs after a (re)connect reset).
        if self._gyro_want != self._gyro_applied:
            self._apply_gyro_enable()
        # Hold guarded (Nintendo/Bluetooth) links up during idle.
        if self._pad_governors:
            self._bt_keepalive(now)
        return now

    def set_gyro_kinds(self, kinds):
        """Controller kinds whose pads should stream gyro data (Gyro To Mouse
        toggled on). Records intent; _pump applies it on the SDL thread."""
        self._gyro_want = frozenset(kinds or ())

    def _apply_gyro_enable(self):
        for jid, g in list(self._pads.items()):
            want = (self._pad_has_gyro.get(jid, False)
                    and self._pad_kinds.get(jid, "switch") in self._gyro_want)
            if want == self._pad_gyro_on.get(jid, False):
                continue
            try:
                S.SDL_SetGamepadSensorEnabled(g, S.SDL_SENSOR_GYRO, want)
                self._pad_gyro_on[jid] = want
            except Exception:
                pass
        self._gyro_applied = self._gyro_want

    # Gyro angular velocity (rad/s) that counts as deliberate use  the rad/s
    # twin of _ACTIVITY_GYRO's raw-unit threshold (~2 °/s).
    _GYRO_ACTIVE_RAD = _ACTIVITY_GYRO * GYRO_DEG_PER_SEC * math.pi / 180.0

    def read_gyro(self):
        """[(jid, kind, x, y, z rad/s), ...] for every pad currently streaming
        gyro data (values refresh with the poll's SDL_UpdateGamepads). Empty
        when Gyro To Mouse is off everywhere. A pad showing real gyro motion is
        marked active (like a button press) so key-press haptics reach the pad
        being WAVED, not just the one last pressing buttons  gyro-steering the
        OSK pointer is button-less, and without this the haptic target went
        stale and the ticks landed on the wrong (idle) controller."""
        out = []
        now = time.monotonic()
        for jid, on in self._pad_gyro_on.items():
            if not on:
                continue
            g = self._pads.get(jid)
            if g is None:
                continue
            try:
                v = S.gamepad_gyro(g)
            except Exception:
                v = None
            if v is not None:
                if (abs(v[0]) > self._GYRO_ACTIVE_RAD
                        or abs(v[1]) > self._GYRO_ACTIVE_RAD):
                    self._pad_active_t[jid] = now
                out.append((jid, self._pad_kinds.get(jid, "switch"),
                            v[0], v[1], v[2]))
        return out

    def poll(self):
        return self.poll_all()[0]

    def poll_all(self):
        """Both views the tray's gamepad path needs, from ONE pump: the
        OR-merged frame (drives the single desktop user's OSK-open / mouse /
        chords) AND a per-pad dict {instance_id: SteamControllerInput}, one
        entry per open SDL pad, NOT merged  so each physical controller can
        drive its OWN dedicated virtual XInput device (automatic multiplayer).
        Returns (merged_or_None, frames_dict); (None, {}) when no pad is live.
        Scales to any number of pads / any mix of controller types."""
        now = self._pump()
        if now is None or not self._pads:
            return None, {}
        merged = None
        frames = {}
        for jid, g in list(self._pads.items()):
            try:
                f = self._read_pad(g, self._pad_kinds.get(jid))
            except Exception:
                continue
            # Remember which pad is actively being used, so the key-press haptic
            # can buzz only that controller. Counts BOTH buttons/triggers AND
            # stick deflection  the OSK navigates its grid with the left stick
            # (an axis, not a button), so a button-only check missed every
            # stick-driven key change and the haptic went silent.
            if _frame_has_activity(f):
                self._pad_active_t[jid] = now
            frames[jid] = f
            merged = f if merged is None else merge_inputs(merged, f)
        return merged, frames

    # A pad counts as the "active" one for haptics if it was used this recently
    # (covers the brief gap between the input and the key dispatch that fires
    # the tick).
    _ACTIVE_WINDOW = 1.0

    def _active_jid(self):
        """Instance id of the pad most recently providing input, or None if
        nothing has been pressed lately."""
        now = time.monotonic()
        best_jid, best_t = None, 0.0
        for jid, t in self._pad_active_t.items():
            if t > best_t:
                best_jid, best_t = jid, t
        if best_jid is not None and (now - best_t) <= self._ACTIVE_WINDOW:
            return best_jid
        return None

    def _active_gamepad(self):
        """The SDL_Gamepad* of the pad most recently providing input, or None
        if nothing has been pressed lately. Used so key-press haptics go to the
        controller actually typing, not every connected pad."""
        jid = self._active_jid()
        return self._pads.get(jid) if jid is not None else None

    def active_kind(self):
        """Controller kind ("switch"/"xbox"/"ps5"/...) of the pad most recently
        in use  of the LAST-used pad if everything has gone quiet, and
        "switch" (the historical default for any SDL pad) with no pads at all."""
        jid = self._active_jid()
        if jid is None:
            best_jid, best_t = None, 0.0
            for j, t in self._pad_active_t.items():
                if t > best_t:
                    best_jid, best_t = j, t
            jid = best_jid
        if jid is None and self._pads:
            jid = next(iter(self._pads))
        return self._pad_kinds.get(jid, "switch")

    def kind_of(self, jid):
        """Controller kind for one SDL instance id ("switch" fallback)."""
        return self._pad_kinds.get(jid, "switch")

    @property
    def controller_kind(self):
        # The OSK glyph swap (InputMerger.poll) reads this per intentional
        # edge  name the actual pad family instead of a generic "sdl".
        return self.active_kind()

    def _rumble(self, low, high, ms):
        for jid, g in list(self._pads.items()):
            self._rumble_one(g, low, high, ms, jid=jid)

    def _jid_of(self, g):
        for jid, pad in self._pads.items():
            if pad == g:
                return jid
        return None

    def _rumble_one(self, g, low, high, ms, jid=None, droppable=True):
        """Send one rumble packet to one pad, through the Nintendo Bluetooth
        guard when that pad is a Switch pad on Bluetooth (unguarded pads take
        the direct path they always did). `droppable` False forces the packet
        past the governor's pacing  for deliberate one-shot confirmations that
        would be meaningless if silently dropped."""
        if g is None:
            return
        gov = None
        if self._pad_governors:
            if jid is None:
                jid = self._jid_of(g)
            gov = self._pad_governors.get(jid)
        if gov is not None:
            pkt = gov.filter(time.monotonic(), low, high, ms,
                             imu=bool(self._pad_gyro_on.get(jid)),
                             droppable=droppable)
            if pkt is None:
                return
            low, high, ms = pkt
        try:
            S.SDL_RumbleGamepad(g, low, high, ms)
        except Exception:
            pass

    def _bt_keepalive(self, now):
        """Trickle a no-op output report to each guarded (Nintendo, Bluetooth)
        pad that has had no traffic for a while. Sniff mode  which is what
        these controllers put every non-Nintendo host into  sends no keepalives
        of its own and relies on the host to keep the link busy."""
        for jid, gov in list(self._pad_governors.items()):
            if not gov.keepalive_due(now):
                continue
            g = self._pads.get(jid)
            if g is None:
                continue
            low, high, ms = gov.keepalive_packet(now)
            try:
                S.SDL_RumbleGamepad(g, low, high, ms)
            except Exception:
                pass

    def haptic_click(self):
        # Only the pad currently being used buzzes on a key press. Biased hard to
        # the HIGH-frequency motor with a short pulse: the high-freq actuator has
        # a much faster attack than the low-freq one, so the tick is FELT sooner
        # (sharper, less "delayed") instead of ramping up softly. Kept low
        # amplitude so it's a light tick, not a jolt. Tunable.
        jid = self._active_jid()
        self._rumble_one(self._pads.get(jid), 0x0200, 0x0C00, 10, jid=jid)

    def haptic_pad_click(self):
        jid = self._active_jid()
        self._rumble_one(self._pads.get(jid), 0x1A00, 0x2800, 24, jid=jid)

    def haptic_trigger_click(self, strong=True):
        """L2/R2 actuation feedback: ONLY controller kinds with ANALOG triggers
        buzz  their trigger has no mechanical click, so the rumble stands in
        for it. A digital trigger (Switch ZL/ZR) already clicks physically and
        stays silent. Targets the pad actually being used, gated by that pad
        kind's own Haptics toggle. `strong` picks the OSK select weight; False
        is the lighter desktop mouse-click tick (matching the SC's)."""
        jid = self._active_jid()
        if jid is None:
            return
        kind = self._pad_kinds.get(jid, "switch")
        if _pads is None or not _pads.has_analog_triggers(kind):
            return
        if not state.is_rumble_enabled(kind):
            return
        lo, hi, ms = (0x1A00, 0x2800, 24) if strong else (0x0200, 0x0C00, 10)
        self._rumble_one(self._pads.get(jid), lo, hi, ms, jid=jid)

    def set_rumble(self, large, small):
        """Game force-feedback: large/small motor (0..255) -> a sustained
        rumble, refreshed on each change (1s window so it persists between
        updates; the game sends 0,0 to stop). Targets the active pad if one is
        clearly in use, else all pads (so a single idle pad still rumbles)."""
        low = max(0, min(255, int(large))) * 257
        high = max(0, min(255, int(small))) * 257
        jid = self._active_jid()
        g = self._pads.get(jid) if jid is not None else None
        if g is not None:
            self._rumble_one(g, low, high, 1000, jid=jid)
        else:
            self._rumble(low, high, 1000)

    def set_rumble_pad(self, jid, large, small):
        """Game force-feedback for ONE specific pad (by SDL instance id)  used
        in separate-XInput mode so each player's virtual pad rumbles only its
        own physical controller, never the others."""
        g = self._pads.get(jid)
        if g is None:
            return
        low = max(0, min(255, int(large))) * 257
        high = max(0, min(255, int(small))) * 257
        self._rumble_one(g, low, high, 1000, jid=jid)

    def has_pads(self):
        return bool(self._pads)

    def set_lizard(self, enabled):
        pass  # generic pads have no firmware lizard mode

    def addExit(self):
        pass

    def close(self):
        for g in list(self._pads.values()):
            # Clear the Home LED on the way out so quitting the app doesn't leave
            # a controller's gamepad-mode LED stuck blue. The pad is still live
            # here (unlike a yanked disconnect), so this actually lands.
            try:
                S.SDL_SetGamepadLED(g, 0, 0, 0)
            except Exception:
                pass
            try:
                S.SDL_CloseGamepad(g)
            except Exception:
                pass
        self._pads.clear()
        # The SDL_INIT_GAMEPAD subsystem is torn down by adusk.main's SDL_Quit.


class SharedSdlFrameSource:
    """Reads SDL-pad frames published by the tray's sdl_gamepad_thread rather
    than polling SDL itself.

    The tray already owns one working Sdl3GamepadSource (it detects Guide+X to
    open the OSK). Having adusk open a SECOND Sdl3GamepadSource on its input
    thread double-drove the same pad across two threads and delivered no input,
    so a non-Steam pad (Xbox/DualSense/Switch Pro) couldn't drive the OSK once
    open. This source keeps ALL SDL access on the tray's thread: it just returns
    the latest frame the tray published via state.set_sdl_frame().

    Haptics forward to the tray's live Sdl3GamepadSource (registered via
    state.set_sdl_source). The tray's sdl_gamepad_thread owns that source's SDL
    access; SDL_RumbleGamepad is safe to call cross-thread, so the Switch Pro /
    Xbox / DualSense buzzes on each OSK key press (matching the SC)."""

    # The tray/adusk publish generic-SDL-pad frames here tagged with the pad's
    # actual kind ("switch"/"xbox"/"ps5"/...)  drives the OSK to that
    # family's glyphs. See InputMerger.poll().
    @property
    def controller_kind(self):
        return state.get_sdl_frame_kind()

    def poll(self):
        return state.get_sdl_frame()

    def set_lizard(self, enabled):
        pass

    @staticmethod
    def _tray_src():
        try:
            return state.get_sdl_source()
        except Exception:
            return None

    def haptic_click(self):
        src = self._tray_src()
        if src is not None:
            try:
                src.haptic_click()
            except Exception:
                pass

    def haptic_pad_click(self):
        # Non-trigger "select"/confirm feedback (a gyro-hotkey toggle confirm,
        # an OSK function rebound to a face button, ...). L2/R2 actuation does
        # NOT come through here anymore  it fires haptic_trigger_click below,
        # which is what enforces "digital triggers (Switch ZL/ZR) don't buzz".
        src = self._tray_src()
        if src is not None:
            try:
                src.haptic_pad_click()
            except Exception:
                pass

    def haptic_trigger_click(self):
        # L2/R2 actuation feedback. The tray source gates it per pad kind:
        # analog-trigger controllers (Xbox/PS/Deck/handhelds) buzz  the
        # rumble IS the missing click  while digital-trigger kinds (Switch
        # ZL/ZR) stay silent because the trigger already clicks mechanically.
        src = self._tray_src()
        if src is not None:
            try:
                src.haptic_trigger_click()
            except Exception:
                pass

    def set_rumble(self, large, small):
        src = self._tray_src()
        if src is not None:
            try:
                src.set_rumble(large, small)
            except Exception:
                pass

    def addExit(self):
        pass

    def close(self):
        pass


class InputMerger:
    """Holds the active input sources, OR-merges their frames, and presents the
    `sc`-facade (set_lizard / haptic_click / haptic_pad_click / addExit) that
    ControllerManager.handle_input expects, fanning each call out to every
    source."""

    # A source stays the haptic target this long after it last showed activity
    # (covers the gap between an input and the key dispatch that fires the tick).
    _ACTIVE_WINDOW = 1.0

    def __init__(self):
        self._sources = []
        # The source whose controller is actively driving the OSK, and when it
        # last showed activity. Haptics route ONLY here so a key press buzzes
        # just the controller being used  not every connected controller.
        self._active_src = None
        self._active_t = 0.0
        # Per-source state for the OSK glyph swap's edge detection  see poll()
        # and _intentional_edge(). Keyed by source identity; each value is a dict
        # holding the previous intentional-button mask, stick-deflected flag, and
        # the per-trackpad slide anchors.
        self._glyph_edge_prev = {}

    def add(self, src):
        self._sources.append(src)

    def poll(self):
        merged = None
        now = time.monotonic()
        # Controller family that made a fresh INTENTIONAL input this frame, for
        # the OSK glyph swap. Edge-detected (not activity level) on purpose: with
        # both controllers connected, a hand resting on the Steam Controller
        # keeps a trackpad-touch bit set EVERY frame, which  as a level signal 
        # fought the Switch's stick taps and made the glyphs flicker / lag. An
        # edge (a newly-pressed button/click or a stick entering deflection)
        # only fires on a deliberate action, so a resting hand is ignored and a
        # real input switches the glyphs immediately. Last source in poll order
        # wins if two act on the same frame (SDL added after the SC).
        frame_kind = None
        for src in self._sources:
            try:
                f = src.poll()
            except Exception as e:
                print(f"InputMerger: source poll error: {e!r}")
                f = None
            if f is None:
                continue
            if _frame_has_activity(f):
                self._active_src = src
                self._active_t = now
            if self._intentional_edge(src, f):
                frame_kind = getattr(src, "controller_kind", frame_kind)
            merged = f if merged is None else merge_inputs(merged, f)
        # Tell the renderer which controller family is in use so the Shift/Enter
        # (and X/Y) glyphs match it. Persisted on a real change in set_active_controller.
        if frame_kind is not None:
            state.set_active_controller(frame_kind)
        return merged

    # Trackpad-touch bits are EXCLUDED from the button edge: a hand resting on a
    # Steam Controller pad holds these set without any deliberate action, so
    # counting them would let an idle hand steal the glyphs. A pad *click*
    # (LPAD/RPAD) is a separate bit and still counts. Deliberate pad SLIDING is
    # picked up separately below (a resting finger doesn't move).
    _GLYPH_EDGE_BTN_MASK = ~(SCButtons.LPADTOUCH | SCButtons.RPADTOUCH)
    # Pad coords are int16 (±32767 full-scale). A finger that has moved this far
    # from its anchor since the last check counts as a deliberate slide; the
    # anchor then resets to the new spot, so the test measures RECENT movement
    # (a finger that slides then rests stops firing) and a resting finger's small
    # capacitive jitter never reaches it.
    _PAD_MOVE_THRESHOLD = 2500
    # Stick deflection needed to count as a glyph-switch edge, matched to when
    # each stick actually DOES something in the OSK so a tiny nudge that moves
    # nothing can't flip the glyphs. The LEFT stick steps the key cursor at
    # ControllerManager.KBD_STICK_DEADZONE (18480 = base deadzone +32%); the
    # RIGHT stick drives the mouse at _MOUSE_DEADZONE (6000). Keep in sync with
    # controller.py.
    _GLYPH_LSTICK_THRESHOLD = 18480
    _GLYPH_RSTICK_THRESHOLD = 6000

    def _intentional_edge(self, src, f):
        """True when this source's frame shows a NEW deliberate input vs its
        previous frame  a button/trigger/click newly pressed, a stick newly
        pushed past the deadzone, or a trackpad finger sliding (Steam Controller).
        Used to decide which controller owns the OSK glyphs (see poll). Steady
        holds  including a hand merely RESTING on a trackpad  produce no edge,
        so they never flip the glyphs; actively using the SC pad switches back to
        the SC glyphs."""
        intent_btns = f.buttons & self._GLYPH_EDGE_BTN_MASK
        stick_defl = (abs(f.lstick_x) > self._GLYPH_LSTICK_THRESHOLD
                      or abs(f.lstick_y) > self._GLYPH_LSTICK_THRESHOLD
                      or abs(f.rstick_x) > self._GLYPH_RSTICK_THRESHOLD
                      or abs(f.rstick_y) > self._GLYPH_RSTICK_THRESHOLD)

        st = self._glyph_edge_prev.get(src)
        if st is None:
            # First frame for this source: there's no prior state to diff, so
            # record a SILENT baseline and fire no edge. The merger is rebuilt on
            # every OSK open, so without this whatever a connected controller
            # happens to report on that first frame (an idle Steam Controller's
            # stick drift / a wake frame, a still-held opening chord) would look
            # like a fresh press and flip the glyphs  making them jump to the
            # Steam Controller every time the OSK is reopened with the Switch.
            # The persisted last-used controller shows until a genuine NEW input.
            self._glyph_edge_prev[src] = {
                "btns": intent_btns, "defl": stick_defl,
                "la": (f.lpad_x, f.lpad_y) if (f.buttons & SCButtons.LPADTOUCH) else None,
                "ra": (f.rpad_x, f.rpad_y) if (f.buttons & SCButtons.RPADTOUCH) else None,
            }
            return False

        edge = bool(intent_btns & ~st["btns"]) or (stick_defl and not st["defl"])
        st["btns"] = intent_btns
        st["defl"] = stick_defl

        # Trackpad slide → deliberate Steam Controller use. Anchor on touch-down
        # (no edge), then fire (and re-anchor) once the finger has moved past the
        # jitter threshold. SDL pads report no pad touch/coords, so this is a
        # no-op for them.
        t = self._PAD_MOVE_THRESHOLD
        for touch_bit, key, px, py in (
                (SCButtons.LPADTOUCH, "la", f.lpad_x, f.lpad_y),
                (SCButtons.RPADTOUCH, "ra", f.rpad_x, f.rpad_y)):
            if f.buttons & touch_bit:
                anchor = st[key]
                if anchor is None:
                    st[key] = (px, py)
                elif abs(px - anchor[0]) > t or abs(py - anchor[1]) > t:
                    edge = True
                    st[key] = (px, py)
            else:
                st[key] = None
        return edge

    def _active_source(self):
        """The source actively in use, or None if nothing has been touched
        recently (callers then fall back to fanning out to all sources)."""
        if (self._active_src is not None
                and (time.monotonic() - self._active_t) <= self._ACTIVE_WINDOW):
            return self._active_src
        return None

    def set_lizard(self, enabled):
        for src in self._sources:
            try:
                src.set_lizard(enabled)
            except Exception:
                pass

    def _fan_haptic(self, method):
        # Route the tick to the controller actually being used; if none is
        # clearly active (rare  e.g. the one-shot open tick), fall back to all.
        active = self._active_source()
        targets = [active] if active is not None else self._sources
        for src in targets:
            try:
                getattr(src, method)()
            except Exception:
                pass

    def haptic_click(self):
        self._fan_haptic("haptic_click")

    def haptic_pad_click(self):
        self._fan_haptic("haptic_pad_click")

    def haptic_trigger_click(self):
        # L2/R2 actuation feedback  each source decides whether its controller
        # kind buzzes (analog triggers only; see Sdl3GamepadSource).
        self._fan_haptic("haptic_trigger_click")

    def addExit(self):
        for src in self._sources:
            try:
                src.addExit()
            except Exception:
                pass

    def close(self):
        for src in self._sources:
            try:
                src.close()
            except Exception:
                pass
