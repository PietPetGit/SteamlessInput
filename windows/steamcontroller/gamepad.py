"""Virtual Xbox 360 gamepad bridge for the Steam Controller 2026.

Ported from the C++ SteamlessController project (src/app/VirtualController.cpp).
Takes parsed SteamControllerInput records and pushes the equivalent XInput
state through ViGEm via the `vgamepad` Python wrapper.

ViGEmBus must be installed on the host system; vgamepad ships ViGEmClient.dll
but the kernel driver is a separate install (https://github.com/ViGEm/ViGEmBus).
"""

try:
    import vgamepad as vg
    _VGAMEPAD_IMPORT_ERROR = None
except Exception as e:  # ImportError, OSError when ViGEmClient.dll fails
    vg = None
    _VGAMEPAD_IMPORT_ERROR = e


class ViGEmUnavailable(RuntimeError):
    """vgamepad / ViGEmBus is not usable on this machine."""


# ViGEmBus driver releases  shown/linked wherever we tell the user to install it.
VIGEM_DOWNLOAD_URL = "https://github.com/nefarius/ViGEmBus/releases"

_BUS_AVAILABLE = None   # cached vigem_bus_available() result (None = unprobed)


def vigem_bus_available(refresh=False):
    """True when the ViGEmBus KERNEL DRIVER is installed and reachable  i.e.
    a virtual pad could actually be created right now.

    ViGEmClient.dll ships with the app, so importing vgamepad succeeds even on a
    machine with no driver; the only honest test is asking the client to connect
    to the bus (VIGEM_ERROR_BUS_NOT_FOUND when it isn't there). The probe is
    cheap (no target is added) and the result is cached  pass refresh=True
    after the user has installed the driver mid-session.

    Used by the picker to hard-disable the "ViGEm Bus Driver" toggle instead of
    letting it be switched on into a mode that can never work.
    """
    global _BUS_AVAILABLE
    if _BUS_AVAILABLE is not None and not refresh:
        return _BUS_AVAILABLE
    ok = False
    if vg is not None:
        try:
            import vgamepad.win.vigem_client as vcli
            import vgamepad.win.vigem_commons as vcom
            client = vcli.vigem_alloc()
            if client:
                try:
                    err = vcli.vigem_connect(client)
                    ok = (err == vcom.VIGEM_ERRORS.VIGEM_ERROR_NONE)
                    if ok:
                        vcli.vigem_disconnect(client)
                finally:
                    vcli.vigem_free(client)
        except Exception:
            ok = False      # DLL missing / wrong arch / driver refused us
    _BUS_AVAILABLE = ok
    return ok


# -- Bit positions (taken from C++ SteamController.h; do not use the Python
# -- SCButtons enum here  its names don't match the C++ reference and the
# -- whole point of this module is to mirror the C++ Translate() exactly.)

# buf[2] (low byte of the uint32 button mask)
_BTN_A    = 0x01
_BTN_B    = 0x02
_BTN_X    = 0x04
_BTN_Y    = 0x08
_BTN_RS   = 0x20  # right stick click
_BTN_MENU = 0x40  # Menu / Start

# buf[3]
_BTN_RB      = 0x02
_BTN_DPAD_DN = 0x04
_BTN_DPAD_RT = 0x08
_BTN_DPAD_LT = 0x10
_BTN_DPAD_UP = 0x20
_BTN_VIEW    = 0x40  # View / Back
_BTN_LS      = 0x80  # left stick click

# buf[4]
_BTN_STEAM = 0x01  # Guide
_BTN_LB    = 0x08


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# Gamepad action id (from keybinds_runtime / the picker's gamepad layout) ->
# XUSB_BUTTON attribute name. Used to compile a picker button map into raw
# (sc_bit, xusb_flag) pairs for update(). Triggers (lt/rt) are analog axes, not
# entries here.
_ACTION_XUSB = {
    "btn_a": "XUSB_GAMEPAD_A", "btn_b": "XUSB_GAMEPAD_B",
    "btn_x": "XUSB_GAMEPAD_X", "btn_y": "XUSB_GAMEPAD_Y",
    "lb": "XUSB_GAMEPAD_LEFT_SHOULDER", "rb": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "ls": "XUSB_GAMEPAD_LEFT_THUMB", "rs": "XUSB_GAMEPAD_RIGHT_THUMB",
    "start": "XUSB_GAMEPAD_START", "back": "XUSB_GAMEPAD_BACK",
    "guide": "XUSB_GAMEPAD_GUIDE",
    "dpad_up": "XUSB_GAMEPAD_DPAD_UP", "dpad_down": "XUSB_GAMEPAD_DPAD_DOWN",
    "dpad_left": "XUSB_GAMEPAD_DPAD_LEFT", "dpad_right": "XUSB_GAMEPAD_DPAD_RIGHT",
}


class VirtualGamepad:
    """Wraps a vgamepad VX360Gamepad and exposes update(sci) which translates
    a Steam Controller input record into an XUSB report and sends it to ViGEm.
    """

    def __init__(self):
        if vg is None:
            raise ViGEmUnavailable(
                f"vgamepad not available: {_VGAMEPAD_IMPORT_ERROR!r}. "
                "Install the ViGEmBus driver (https://github.com/ViGEm/ViGEmBus/releases) "
                "and `pip install vgamepad`."
            )
        try:
            self._pad = vg.VX360Gamepad()
        except Exception as e:
            raise ViGEmUnavailable(
                f"Failed to create virtual Xbox 360 pad: {e!r}. "
                "Is the ViGEmBus driver installed?"
            ) from e

        # Cache the XUSB button enum so update() doesn't re-lookup each call.
        self._XB = vg.XUSB_BUTTON
        # Last XInput report we actually submitted, as a tuple. update() skips
        # the (native) ViGEm submit when the new report is identical  the game
        # polls XInput for current state, so re-sending an unchanged report just
        # burns CPU/USB. Saves the most while input is steady (idle sticks,
        # buttons held), which is a large fraction of frames.
        self._last_report = None
        # Strong ref to the force-feedback callback (vgamepad keeps its own too,
        # but holding it here makes the lifecycle explicit).
        self._rumble_cb = None

    def register_rumble(self, handler):
        """Forward game force-feedback to `handler(large, small)`  the XInput
        large/small motor intensities (0..255)  whenever the game updates
        rumble on the virtual pad. The callback runs on a ViGEm thread.

        Safe to call again to REPLACE the handler: a pad parked across a
        wireless dropout is handed back to the same controller under a new SDL
        instance id (see App._park_sdl_gamepad), and ViGEm refuses a second
        notification while one is attached  so drop the old one first."""
        pad = self._pad
        if pad is None:
            return
        if self._rumble_cb is not None:
            try:
                pad.unregister_notification()
            except Exception:
                pass
            self._rumble_cb = None

        # Signature must match vgamepad's expected callback exactly.
        def _cb(client, target, large_motor, small_motor, led_number, user_data):
            try:
                handler(large_motor, small_motor)
            except Exception as e:
                print(f"rumble callback error: {e!r}")

        self._rumble_cb = _cb
        try:
            pad.register_notification(callback_function=_cb)
        except Exception as e:
            print(f"register rumble notification failed: {e!r}")

    def close(self):
        pad = self._pad
        self._pad = None
        if pad is not None:
            try:
                pad.unregister_notification()
            except Exception:
                pass
            self._rumble_cb = None
            try:
                # Zero the report so games don't see ghost input after we leave.
                pad.reset()
                pad.update()
            except Exception:
                pass
            # vgamepad's __del__ unregisters the target with ViGEm.

    def reset(self):
        """Zero the XInput report and push it. Used to release any held
        buttons when we're about to stop pushing input (e.g. handing the
        controller back to firmware lizard mode mid-session)."""
        pad = self._pad
        if pad is None:
            return
        # Invalidate the dedup cache: we're zeroing the pad out-of-band, so the
        # next update() must always re-submit (even if its report happens to
        # match what we last sent before this reset).
        self._last_report = None
        try:
            pad.reset()
            pad.update()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def action_flag(self, action):
        """Return the XUSB button flag int for a gamepad action string, or 0."""
        XB = self._XB
        attr = _ACTION_XUSB.get(action)
        if not XB or not attr:
            return 0
        return int(getattr(XB, attr, 0) or 0)

    def compile_button_map(self, pairs):
        """Translate a picker button map [(sc_bit, action), ...] into the raw
        [(sc_bit, xusb_flag), ...] form update() consumes, resolving each action
        id to its XUSB button flag once (so update() just ORs flags). Unknown
        actions are dropped. Returns [] for a falsy/empty map."""
        XB = self._XB
        out = []
        for sc_bit, action in pairs or []:
            attr = _ACTION_XUSB.get(action)
            if attr is None:
                continue
            flag = getattr(XB, attr, None)
            if flag is None:
                continue
            out.append((int(sc_bit), int(flag)))
        return out

    def update(self, sci, button_map=None, lt_analog=True, rt_analog=True,
               extra_buttons=0, lstick_zero=False, rstick_zero=False,
               rstick_add=None):
        """Translate a SteamControllerInput into an XInput report and push it.

        With no button_map this mirrors VirtualController::Translate() from the
        C++ project (the fixed 1:1 mapping used by the SDL/Switch pads). When
        `button_map` is supplied (compiled via compile_button_map) the digital
        buttons come from it instead  that's the path the Steam Controller's
        configurable gamepad-mode binds take. `lt_analog`/`rt_analog` keep the
        analog trigger axes live (the watcher clears one when L2/R2 is rebound to
        a digital button). `rstick_add` = optional (dx, dy) int deflection ADDED
        to the right stick after zeroing/clamping  the gyro→joystick output
        rides on top of (or replaces, with rstick_zero) the physical stick.
        """
        pad = self._pad
        if pad is None:
            return

        buttons = sci.buttons
        XB = self._XB
        w = 0
        if button_map is not None:
            for sc_bit, flag in button_map:
                if buttons & sc_bit:
                    w |= flag
        else:
            b0 = (buttons >> 0) & 0xFF
            b1 = (buttons >> 8) & 0xFF
            b2 = (buttons >> 16) & 0xFF
            # Face buttons
            if b0 & _BTN_A: w |= XB.XUSB_GAMEPAD_A
            if b0 & _BTN_B: w |= XB.XUSB_GAMEPAD_B
            if b0 & _BTN_X: w |= XB.XUSB_GAMEPAD_X
            if b0 & _BTN_Y: w |= XB.XUSB_GAMEPAD_Y
            # Bumpers
            if b2 & _BTN_LB: w |= XB.XUSB_GAMEPAD_LEFT_SHOULDER
            if b1 & _BTN_RB: w |= XB.XUSB_GAMEPAD_RIGHT_SHOULDER
            # Menu / View
            if b0 & _BTN_MENU: w |= XB.XUSB_GAMEPAD_START
            if b1 & _BTN_VIEW: w |= XB.XUSB_GAMEPAD_BACK
            # Stick clicks
            if b1 & _BTN_LS: w |= XB.XUSB_GAMEPAD_LEFT_THUMB
            if b0 & _BTN_RS: w |= XB.XUSB_GAMEPAD_RIGHT_THUMB
            # Guide
            if b2 & _BTN_STEAM: w |= XB.XUSB_GAMEPAD_GUIDE
            # D-pad
            if b1 & _BTN_DPAD_UP: w |= XB.XUSB_GAMEPAD_DPAD_UP
            if b1 & _BTN_DPAD_DN: w |= XB.XUSB_GAMEPAD_DPAD_DOWN
            if b1 & _BTN_DPAD_LT: w |= XB.XUSB_GAMEPAD_DPAD_LEFT
            if b1 & _BTN_DPAD_RT: w |= XB.XUSB_GAMEPAD_DPAD_RIGHT

        w |= extra_buttons
        lt = _clamp(sci.ltrig >> 7, 0, 255) if lt_analog else 0
        rt = _clamp(sci.rtrig >> 7, 0, 255) if rt_analog else 0
        lx = 0 if lstick_zero else _clamp(sci.lstick_x, -32768, 32767)
        ly = 0 if lstick_zero else _clamp(sci.lstick_y, -32768, 32767)
        rx = 0 if rstick_zero else _clamp(sci.rstick_x, -32768, 32767)
        ry = 0 if rstick_zero else _clamp(sci.rstick_y, -32768, 32767)
        if rstick_add is not None:
            rx = _clamp(rx + int(rstick_add[0]), -32768, 32767)
            ry = _clamp(ry + int(rstick_add[1]), -32768, 32767)

        # Nothing changed since the last submit → don't re-send (the game keeps
        # reading the state we already pushed). Skips the native ViGEm call.
        snapshot = (w, lt, rt, lx, ly, rx, ry)
        if snapshot == self._last_report:
            return
        self._last_report = snapshot

        report = pad.report
        report.wButtons = w
        # Triggers: int16 0..0x7FFF → uint8 0..255 (same `>> 7` the C++ uses).
        report.bLeftTrigger  = lt
        report.bRightTrigger = rt
        # Sticks: int16 same range as XInput  pass straight through.
        report.sThumbLX = lx
        report.sThumbLY = ly
        report.sThumbRX = rx
        report.sThumbRY = ry

        pad.update()
