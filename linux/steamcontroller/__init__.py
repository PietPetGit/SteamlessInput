"""Windows Steam Controller driver  one class, three Valve HID families.

Primary target is the "Triton" 2026 Steam Controller (wireless adapter PID
0x1304  Valve internal codename Proteus  or wired 0x1302), whose state
report ID is 0x45 (newer firmware; 46 bytes) or the legacy 0x42 (USB/full
state, 54 bytes)  same field layout, see TRITON_INPUT_REPORT_IDS.

Two older families speak the classic 64-byte ValveInReport_t framing
(`unReportVersion=1 | ucType | ucLength` then a per-type payload) and are
translated onto the SAME SCI record + Triton SCButtons bit space, so the whole
desktop/chords/gamepad takeover runtime drives them unchanged:
  * "steam_deck"   the Deck's built-in pad (PID 0x1205), ucType 9.
  * "sc2015"       the ORIGINAL 2015 Steam Controller (wired PID 0x1102 /
                    wireless dongle 0x1142), ucType 1. Same trackpad-first
                    hardware the whole app is built around, minus a right
                    stick, a D-pad and the second pair of rear paddles.

All three layouts come from Valve's own open-source headers in libsdl-org/SDL
(src/joystick/hidapi/steam/controller_structs.h plus the steam / steamdeck /
steam_triton drivers). This file maps them onto the small adusk-facing API
surface (SteamController, SCButtons, SCStatus, SteamControllerInput, SCI_NULL,
EventMapper.process inputs).
"""

import sys
import threading
import time
from collections import namedtuple
from enum import IntEnum
from struct import Struct, unpack

# Precompiled parser for the Triton input report body (bytes 1..29). Built once
# and used via unpack_from so the per-frame hot path does a single C-level
# unpack with no intermediate slice allocations.
#   B seq | I buttons | h h triggers | h h h h sticks | h h H h h H pads+pressure
_TRITON_STRUCT = Struct('<BIhhhhhhhhHhhH')

# IMU gyro X/Y/Z (3x int16 LE) at report offset 0x28  present in BOTH state
# variants (the 46-byte 0x45 "no-quaternion" frame ends exactly after it, the
# 54-byte 0x42 frame appends the fusion quaternion). Frozen zeros/garbage until
# the IMU is switched on via SETTING_IMU_MODE (see set_imu); only parsed into
# the SCI when the frame is long enough.
_TRITON_GYRO_OFFSET = 0x28
_TRITON_GYRO_MIN_LEN = 0x2E      # 46  offset + 3 * int16
_TRITON_GYRO_STRUCT = Struct('<hhh')

import hid


# Linux-only: send feature reports via /dev/hidrawN + HIDIOCSFEATURE ioctl.
# The pip `hidapi` wheel uses the libusb backend, and on Linux libusb can't
# write feature reports to interfaces whose kernel HID driver wasn't detached
# (the dongle's keyboard/mouse lizard-mode interfaces are always claimed by
# usbhid). The kernel hidraw ioctl path routes the feature report correctly
# regardless of driver claims, so we use it on Linux instead of the hidapi
# send_feature_report call.
_IS_LINUX = sys.platform.startswith("linux")


def _send_lizard_via_libusb(report):
    """Send the given feature-report bytes (with the report ID at index 0)
    to interface 2 of the Steam Controller dongle/wired device via a libusb
    SET_REPORT control transfer. The hidapi libusb backend always includes
    the report-ID byte in the SET_REPORT payload, which the Triton firmware
    STALLs (it expects the ID only in wValue, payload to be 64 bytes); doing
    the transfer ourselves with the correct wire format is the only way to
    actually flip lizard mode on Linux. Returns 0 on success, libusb errno
    otherwise. Quietly returns -1 if libusb isn't available."""
    if not _IS_LINUX:
        return -1
    try:
        import ctypes
        import ctypes.util
        lib_name = ctypes.util.find_library('usb-1.0')
        if not lib_name:
            return -1
        libusb = ctypes.CDLL(lib_name)
    except Exception:
        return -1

    class _H(ctypes.Structure):
        pass

    libusb.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    libusb.libusb_open_device_with_vid_pid.restype = ctypes.POINTER(_H)
    libusb.libusb_open_device_with_vid_pid.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16
    ]
    libusb.libusb_kernel_driver_active.argtypes = [ctypes.POINTER(_H), ctypes.c_int]
    libusb.libusb_detach_kernel_driver.argtypes = [ctypes.POINTER(_H), ctypes.c_int]
    libusb.libusb_attach_kernel_driver.argtypes = [ctypes.POINTER(_H), ctypes.c_int]
    libusb.libusb_claim_interface.argtypes = [ctypes.POINTER(_H), ctypes.c_int]
    libusb.libusb_release_interface.argtypes = [ctypes.POINTER(_H), ctypes.c_int]
    libusb.libusb_control_transfer.argtypes = [
        ctypes.POINTER(_H), ctypes.c_uint8, ctypes.c_uint8,
        ctypes.c_uint16, ctypes.c_uint16,
        ctypes.c_char_p, ctypes.c_uint16, ctypes.c_uint,
    ]
    libusb.libusb_control_transfer.restype = ctypes.c_int
    libusb.libusb_close.argtypes = [ctypes.POINTER(_H)]
    libusb.libusb_exit.argtypes = [ctypes.c_void_p]

    ctx = ctypes.c_void_p()
    if libusb.libusb_init(ctypes.byref(ctx)) != 0:
        return -1

    handle = None
    try:
        for pid in (PRODUCT_ID_PROTEUS, PRODUCT_ID_WIRED):
            handle = libusb.libusb_open_device_with_vid_pid(ctx, VENDOR_ID, pid)
            if handle:
                break
        if not handle:
            return -1

        # The Triton firmware accepts SET_REPORT only on interface 2 (the
        # first vendor-specific data interface). wValue encodes the report
        # type (Feature=3) in the high byte and the report ID in the low.
        # The payload must NOT include the report ID byte  the hidapi
        # libusb backend gets this wrong, which is why we're here.
        iface = 2
        report_id = bytes(report)[0]
        payload = bytes(report)[1:]  # 64 bytes

        was_attached = libusb.libusb_kernel_driver_active(handle, iface)
        if was_attached == 1:
            libusb.libusb_detach_kernel_driver(handle, iface)
        claim_rc = libusb.libusb_claim_interface(handle, iface)
        if claim_rc != 0:
            if was_attached == 1:
                libusb.libusb_attach_kernel_driver(handle, iface)
            return claim_rc

        buf = ctypes.create_string_buffer(payload, len(payload))
        rc = libusb.libusb_control_transfer(
            handle,
            0x21,  # class | interface | host-to-device
            0x09,  # SET_REPORT
            (0x03 << 8) | report_id,  # Feature report
            iface,
            buf, len(payload), 1000,
        )

        libusb.libusb_release_interface(handle, iface)
        # Intentionally do NOT re-attach the kernel driver. Re-attach puts the
        # interface in a transient state where hidapi's subsequent open fails
        # with a read error. Leaving the kernel driver detached on iface 2
        # is fine: hidapi will claim it cleanly via its libusb backend, and
        # the kernel driver re-binds automatically when our process exits.

        # rc is bytes transferred (positive) on success, negative on error.
        return 0 if rc == len(payload) else rc
    finally:
        if handle:
            libusb.libusb_close(handle)
        libusb.libusb_exit(ctx)


def _find_steam_controller_hidraw_paths():
    """Return a list of /dev/hidrawN paths whose underlying USB device is a
    Valve Steam Controller (VID 0x28DE). The Triton dongle exposes several
    hidraw nodes (one per HID interface); the data interfaces all sit on
    interface index >= 3 on the user's hardware. We probe each in turn 
    the firmware only accepts the SET_REPORT feature command on a subset.

    Used as the Windows-style sibling of `_send_lizard_via_libusb`: the
    kernel hidraw driver's HIDIOCSFEATURE ioctl puts the report ID in
    wValue.lo and sends the body as the wire payload (same shape Windows'
    HidD_SetFeature uses), so the firmware actually honors the command.
    libusb backend gets this wrong and duplicates the report ID byte."""
    paths = []
    base = "/sys/class/hidraw"
    if not os.path.isdir(base):
        return paths
    for name in sorted(os.listdir(base)):
        if not name.startswith("hidraw"):
            continue
        try:
            real = os.path.realpath(os.path.join(base, name, "device"))
        except OSError:
            continue
        # real looks like:
        #   /sys/.../usb1/1-2/1-2:1.3/0003:28DE:1304.0130
        # the basename encodes "BUSCLASS:VID:PID.HIDID"; cheaper to check
        # than walking up the sysfs tree.
        bn = os.path.basename(real)
        if "28DE:" in bn.upper():
            paths.append(os.path.join("/dev", name))
    return paths


def _send_feature_via_hidraw(report):
    """Send a 65-byte feature report (report ID at index 0) via the kernel
    hidraw HIDIOCSFEATURE ioctl. Tries each Valve hidraw node until one
    accepts it. Returns the path that worked, or None on failure.

    The kernel handles the SET_REPORT wire format correctly  the report
    ID goes in wValue.low, body in the payload, no duplication. Doesn't
    fight with hidapi's libusb backend because hidapi opens the device
    via /dev/bus/usb, not via /dev/hidraw."""
    import fcntl

    paths = _find_steam_controller_hidraw_paths()
    if not paths:
        return None

    # HIDIOCSFEATURE = _IOC(_IOC_WRITE | _IOC_READ, 'H', 0x06, len)
    _IOC_NRSHIFT = 0
    _IOC_TYPESHIFT = 8
    _IOC_SIZESHIFT = 16
    _IOC_DIRSHIFT = 30
    _IOC_WRITE = 1
    _IOC_READ = 2
    payload = bytes(report)
    cmd = (((_IOC_WRITE | _IOC_READ) << _IOC_DIRSHIFT)
           | (len(payload) << _IOC_SIZESHIFT)
           | (ord('H') << _IOC_TYPESHIFT)
           | (0x06 << _IOC_NRSHIFT))

    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            rc = fcntl.ioctl(fd, cmd, payload, False)
            # ioctl returns >=0 on success; firmware-level rejection
            # comes back as OSError(EPIPE) etc.
            if rc >= 0:
                return path
        except OSError:
            pass
        finally:
            os.close(fd)
    return None


VENDOR_ID = 0x28DE
PRODUCT_ID_PROTEUS = 0x1304  # Steam Controller Puck / Triton (wireless dongle)
PRODUCT_ID_WIRED   = 0x1302  # Steam Controller 2026 (wired USB)
PRODUCT_ID_DECK    = 0x1205  # Steam Deck built-in controller (LCD and OLED)
PRODUCT_ID_SC2015        = 0x1102  # Steam Controller 2015 (wired USB)
PRODUCT_ID_SC2015_DONGLE = 0x1142  # Steam Controller 2015 wireless receiver

# Product-ID sets per HID controller family this driver speaks. "sc" is the
# 2026 Steam Controller (Triton); "steam_deck" is the Deck's built-in pad and
# "sc2015" the original 2015 Steam Controller  both of those use the older
# 64-byte ValveInReport_t wire protocol (from SDL's SDL_hidapi_steamdeck.c /
# SDL_hidapi_steam.c / controller_structs.h). One SteamController instance
# opens ANY of the three and translates them all onto the same SCI record +
# Triton SCButtons bit space, so the whole desktop/chords/gamepad runtime
# drives every family unchanged.
HID_KIND_PIDS = {
    "sc": (PRODUCT_ID_PROTEUS, PRODUCT_ID_WIRED),
    "steam_deck": (PRODUCT_ID_DECK,),
    "sc2015": (PRODUCT_ID_SC2015, PRODUCT_ID_SC2015_DONGLE),
}
HID_KINDS = tuple(HID_KIND_PIDS)

# The families on the classic ValveInReport_t framing: feature reports are
# prefixed with HID report id 0x00 (the Triton uses 0x01) and share Valve's
# ID_* command set. Kept as one tuple so every kind test below reads as
# "which protocol", not "which product".
VALVE_LEGACY_KINDS = ("steam_deck", "sc2015")

# Remembered across SteamController instances: the interface path that last
# returned input reports. Tried first on the next open so a rebuild (e.g. the
# gamepad<->lizard switch on alt-tab) skips the dongle's silent slots and comes
# live in milliseconds instead of probing each slot for up to 1.5s  which is
# what made the mode chime lag ~1s behind the actual switch.
_LAST_GOOD_PATH = None

# Triton input ("state") report IDs. A firmware update bumped the primary
# REPORT_STATE id from 0x42 to 0x45 (the BLE / "no-quaternion" variant); wired
# units can still emit the legacy 0x42 (USB / full-state). The byte layout after
# the id is IDENTICAL for both, so accept EITHER  mirroring the reference
# project's IsStateReportId(), which treats 0x45 and 0x42 the same.
TRITON_INPUT_REPORT_IDS = (0x45, 0x42)
TRITON_INPUT_REPORT_LEN = 54   # full USB (0x42) report length  reference only
# Minimum payload size for a usable Triton input report. The parser reads the
# first 30 bytes (report ID + buttons/triggers/sticks/pads), so anything below
# that is unparseable. Recent firmware emits 46-byte 0x45 reports  older notes
# describe a 54-byte format. Gating on the parser's actual minimum (rather than
# the observed full length) keeps this from rejecting future firmware that trims
# a couple more trailing bytes.
TRITON_INPUT_MIN_LEN = 30

# Power/battery status report (HID input report id 0x43, 17 bytes). It streams
# interleaved with the game-input reports on the same vendor interface. Layout
# after the report id byte: [1]=charge state, [2]=battery percent (0..100),
# [3:5]=battery voltage mV (uint16 LE), then system/input voltage, current and
# temperature (unused here). Charge-state values and the percent/voltage offsets
# were taken from Valve's SDL steam_triton driver and the Bloss battery-indicator
# reference (GamepadBatteryParser.TryParseSteamTritonBatteryStatus).
TRITON_BATTERY_REPORT_ID = 0x43
TRITON_BATTERY_REPORT_LEN = 17

# report[1] charge-state byte values.
CHARGE_STATE_RESET = 0
CHARGE_STATE_DISCHARGING = 1
CHARGE_STATE_CHARGING = 2
CHARGE_STATE_SOURCE_VALIDATE = 3
CHARGE_STATE_CHARGING_DONE = 4

# Treat the battery as unknown if no input/battery frame has arrived for this
# long. The controller streams input continuously while connected, so a gap this
# big means it powered off (Steam+Y) or dropped its wireless link  even though
# the dongle is still plugged in. Keeps the tray from showing a stale %.
BATTERY_FRESH_SECONDS = 4.0

# Wireless link-status reports (byte[1]: 1=disconnected, 2=connected). We skip
# them in the read loop so they don't get mis-parsed as input frames.
TRITON_WIRELESS_STATUS_IDS = (0x46, 0x79)

# --- Steam Deck wire format ---------------------------------------------------
# The Deck's controller interface streams 64-byte ValveInReport_t frames:
#   [0:2] unReportVersion (uint16 LE, always 1)
#   [2]   ucType   (9 = ID_CONTROLLER_DECK_STATE)
#   [3]   ucLength (64)
# then the SteamDeckStatePacket_t payload (SDL controller_structs.h):
#   I  unPacketNum | I ulButtonsL | I ulButtonsH
#   h  sLeftPadX/Y, sRightPadX/Y
#   h  accel x3 | gyro x3 | quaternion x4      (ignored here)
#   H  sTriggerRawL/R (0..32767, same scale as the Triton's)
#   h  sLeftStickX/Y, sRightStickX/Y           (positive Y = up, like Triton)
#   H  sPressurePadLeft/Right
DECK_REPORT_VERSION = 0x01
DECK_REPORT_TYPE_STATE = 0x09    # ID_CONTROLLER_DECK_STATE
DECK_INPUT_REPORT_LEN = 64
_DECK_STRUCT = Struct('<IIIhhhhhhhhhhhhhhHHhhhhHH')

# Deck button bits (SDL_hidapi_steamdeck.c SteamDeckButtons) → Triton SCButtons
# bits, so every consumer (watcher, virtual-pad bridge, OSK, picker nav) reads
# Deck frames exactly like Steam Controller frames. Values are plain ints
# (enum lookups are too slow for the 250 Hz hot path). The Deck has no
# grip-rest capacitive sensors, so LGRIP_REST/RGRIP_REST never set.
_DECK_BTN_MAP_L = tuple((m, int(b)) for m, b in (
    (0x00000001, 0x00800000),   # R2 full pull        -> RT
    (0x00000002, 0x08000000),   # L2 full pull        -> LT
    (0x00000004, 0x00000200),   # R1                  -> RB
    (0x00000008, 0x00080000),   # L1                  -> LB
    (0x00000010, 0x00000008),   # Y                   -> Y
    (0x00000020, 0x00000002),   # B                   -> B
    (0x00000040, 0x00000004),   # X                   -> X
    (0x00000080, 0x00000001),   # A                   -> A
    (0x00000100, 0x00002000),   # DPAD up             -> DPAD_UP
    (0x00000200, 0x00000800),   # DPAD right          -> DPAD_RIGHT
    (0x00000400, 0x00001000),   # DPAD left           -> DPAD_LEFT
    (0x00000800, 0x00000400),   # DPAD down           -> DPAD_DOWN
    (0x00001000, 0x00000040),   # View                -> VIEW
    (0x00002000, 0x00010000),   # Steam               -> STEAM
    (0x00004000, 0x00004000),   # Menu                -> START
    (0x00008000, 0x00040000),   # L5                  -> LGRIP2
    (0x00010000, 0x00000100),   # R5                  -> RGRIP2
    (0x00020000, 0x04000000),   # left pad click      -> LPAD
    (0x00040000, 0x00400000),   # right pad click     -> RPAD
    (0x00080000, 0x02000000),   # left pad touch      -> LPADTOUCH
    (0x00100000, 0x00200000),   # right pad touch     -> RPADTOUCH
    (0x00400000, 0x00008000),   # L3                  -> L3
    (0x04000000, 0x00000020),   # R3                  -> R3
))
_DECK_BTN_MAP_H = tuple((m, int(b)) for m, b in (
    (0x00000200, 0x00020000),   # L4                  -> LGRIP1
    (0x00000400, 0x00000080),   # R4                  -> RGRIP1
    (0x00004000, 0x01000000),   # left stick touch    -> LPADJOY_TOUCH
    (0x00008000, 0x00100000),   # right stick touch   -> RPADJOY_TOUCH
    (0x00040000, 0x00000010),   # "..." (QAM)         -> QAM
))

# Deck feature-report command set (shared Valve constants, controller_constants.h
#  the kernel hid-steam.c deck path uses exactly these). Deck feature reports
# are prefixed with HID report id 0x00, not the Triton's 0x01. Unlike the
# Triton-on-Linux caveats above, the Deck's controller interface is a plain
# built-in USB HID with a proper hidraw node, so feature reports via hidapi
# behave (Steam and the kernel hid-steam driver use this exact path).
VALVE_FEATURE_REPORT_ID = 0x00
ID_CLEAR_DIGITAL_MAPPINGS = 0x81
ID_SET_DEFAULT_DIGITAL_MAPPINGS = 0x85
ID_LOAD_DEFAULT_SETTINGS = 0x8E
ID_TRIGGER_HAPTIC_PULSE = 0x8F   # 2015 SC: pulse train on one trackpad LRA
ID_TRIGGER_HAPTIC_CMD = 0xEA     # MsgTriggerHaptic (pads: tick/click/tone/...)
ID_TRIGGER_RUMBLE_CMD = 0xEB     # MsgSimpleRumbleCmd (the two rumble motors)

# Settings register numbers (write via ID_SET_SETTINGS_VALUES, 0x87).
SETTING_LEFT_TRACKPAD_MODE = 7
SETTING_RIGHT_TRACKPAD_MODE = 8
SETTING_SMOOTH_ABSOLUTE_MOUSE = 24
SETTING_LEFT_TRACKPAD_CLICK_PRESSURE = 52
SETTING_RIGHT_TRACKPAD_CLICK_PRESSURE = 53
SETTING_STEAM_WATCHDOG_ENABLE = 71
TRACKPAD_NONE = 7                # TrackpadDPadMode: firmware pad mouse off

# MsgTriggerHaptic side ids / command ids (controller_structs.h).
DECK_HAPTIC_SIDE_L = 0x01
DECK_HAPTIC_SIDE_R = 0x02
DECK_HAPTIC_SIDE_BOTH = 0x03
DECK_HAPTIC_CMD_OFF = 0
DECK_HAPTIC_CMD_TONE = 3

# Feature-report commands (sent via send_feature_report with report ID 1)
FEATURE_REPORT_ID = 0x01
FEATURE_REPORT_LEN = 64

ID_SET_SETTINGS_VALUES = 0x87
SETTING_LIZARD_MODE = 9
LIZARD_MODE_OFF = 0
# Lizard mode is a bitfield in the firmware: bit 0 = keyboard emulation,
# bit 1 = mouse emulation. 3 = both on, which matches Triton's default
# boot state and is what users mean by "lizard mode" (trackpads drive the
# system mouse, face buttons emit keys). Value 1 only enables kb  sending
# that explicitly kills the trackpad mouse, which earlier confused the
# "enable-lizard" cold-start.
LIZARD_MODE_ON = 3

# IMU streaming (register 48 on the Triton, the Deck and the original SC 
# SDL3's drivers write the same number on all three). Bit-flags: OFF=0,
# STEERING=1, TILT=2, SEND_ORIENTATION=4, SEND_RAW_ACCEL=8, SEND_RAW_GYRO=16.
# 0x18 (raw accel + raw gyro) is exactly what SDL sends when a game enables
# the gamepad sensors; with it on, the gyro fields in the state report go
# live (they're frozen otherwise  verified across 9k+ idle captures).
SETTING_IMU_MODE = 48
IMU_MODE_OFF = 0x00
IMU_MODE_RAW = 0x18              # SEND_RAW_ACCEL | SEND_RAW_GYRO

# Power-off command. On Valve's controllers this feature report tells the
# controller to turn itself off (the "hold Steam+Y to turn off" behavior in
# Steam Input). Payload is the ASCII string "off!". Confirmed on the original
# Steam Controller / SDL's hidapi driver; experimental on Triton hardware.
ID_TURN_OFF_CONTROLLER = 0x9F

# Haptics. Unlike lizard/turn-off (feature reports), haptics are HID OUTPUT
# reports sent with a plain write (byte 0 = report ID, 65-byte buffer). Format
# and actuator mapping confirmed on real 2026 hardware by the SteamHapticsSinger
# project: 0x83 plays an LFO tone on one actuator, 0x82 stops it.
HID_OUTPUT_REPORT_LEN = 65
ID_OUT_HAPTIC_LFO_TONE = 0x83   # play a tone: [id, actuator, gain, freqLo, freqHi, 0xFF, 0x7F]
ID_OUT_HAPTIC_STOP     = 0x82   # stop an actuator: [id, actuator]

# Actuator indices (no-swap mapping from SteamHapticsSinger):
HAPTIC_PAD_LEFT     = 0   # left trackpad
HAPTIC_PAD_RIGHT    = 1   # right trackpad
HAPTIC_RUMBLE_LEFT  = 3   # left back rumble motor
HAPTIC_RUMBLE_RIGHT = 4   # right back rumble motor

# Tone gain is a signed int8: nearer +127 is loudest, more-negative is quieter
# (the changelog warns the loud end can damage the motors). SteamHapticsSinger
# ships -2 (0xFE) for audible music; UI ticks want much less, so HAPTIC_CLICK_GAIN
# is well down the scale for a light tap.
HAPTIC_DEFAULT_GAIN = 0xFE
# Gain is ~dB-like and steep: -2 is near full blast, -80 is inaudible. A light
# but feelable click sits near the top; the SHORT burst count keeps it clicky.
HAPTIC_CLICK_GAIN = -6
# The simulated trackpad-click (a physical pad press) gets its own tick: a
# short, crisp, slightly firmer pop than the light key tap. Kept high-frequency
# and short so it reads as a real button click, not a deep buzz.
HAPTIC_PAD_CLICK_GAIN = -5
HAPTIC_PAD_CLICK_FREQ = 500
# Mode-change "chime": a short, deliberately subtle two-tone played on both
# trackpads, with the two pads detuned a couple Hz so they beat gently
# ("chorus") and a barely-there low-D pedal on a rumble motor for warmth. Kept
# quiet and low because it fires on every gamepad mode change. This voicing was
# chosen by ear (a low rising fifth) over louder/melodic alternatives.
HAPTIC_CHIME_GAIN = 3        # just above the -2 "music" level: clear, not loud
# "Ding-dong": a two-tone major third (F#4, A4). ON rises F#4->A4, OFF falls
# A4->F#4 (play_chime reverses for off). Equal-tempered.
CHIME_NOTES = (370, 440)     # F#4, A4
CHIME_DURATIONS = (0.10, 0.15)  # quick two-tone blip, second rings a touch
# Each chime tone is bounded to this long so a crash mid-chime can't leave a
# pad/motor buzzing (the body pedal is a rumble motor). Far longer than any
# single note, so in normal play the next note's stop cuts it first  the sound
# is unchanged; only the crash tail self-terminates. See play_chime.
CHIME_TONE_SECONDS = 1.0
CHIME_DETUNE_HZ = 2          # left pad offset from right -> faint chorus beat
CHIME_BODY_FREQ = 147        # D3 pedal under the tones for warmth (Hz)
CHIME_BODY_GAIN = -12        # gentle warmth, well inside the safe motor band
CHIME_BODY_ACTUATOR = HAPTIC_RUMBLE_LEFT

# Game force-feedback → back rumble motors. The XInput large/small motor
# intensities (0..255) each play a continuous tone on one motor; intensity
# scales the (signed) gain, capped below the level the changelog warns can
# damage the motors. Low/high frequencies give the large (heavy) / small
# (buzzy) feel of a normal pad.
RUMBLE_FREQ_LOW = 90     # large motor (left, actuator 3)  heavy
RUMBLE_FREQ_HIGH = 180   # small motor (right, actuator 4)  buzzy
RUMBLE_GAIN_MIN = -40    # lightest audible rumble (intensity 1)
RUMBLE_GAIN_MAX = -4     # strongest (intensity 255), still below the damage zone

# Self-terminating rumble (anti-"infinite buzz"): each motor tone is sent as a
# SHORT bounded burst lasting ~RUMBLE_TONE_SECONDS, and a keepalive thread
# re-arms it every RUMBLE_REFRESH_SECONDS while the game still wants rumble. The
# burst outlasts the refresh so sustained rumble feels continuous  but if this
# process dies (crash / hard kill / a game that quits without zeroing FFB)
# before the usual set_rumble(0,0) stop is delivered, the last burst simply
# lapses on its own within ~RUMBLE_TONE_SECONDS instead of the motor buzzing
# until the controller is power-cycled. (A tone's length in cycles = freq_hz *
# seconds.) This is the same self-expiring contract SDL_RumbleGamepad(ms)
# already gives the SDL pads, so neither controller path can get stuck on.
RUMBLE_TONE_SECONDS = 1.5
RUMBLE_REFRESH_SECONDS = 1.0

# Watchdog: the controller re-enables lizard mode if we don't keep disabling
# it. SDL re-sends every 3s; we use a slightly tighter interval to be safe.
LIZARD_REFRESH_SECONDS = 2.0


class SCStatus(IntEnum):
    INPUT = 0x42       # Triton input-state report type


# Button bit assignments  Triton-specific. Names map to what adusk's
# controller.py expects (LGRIP, LB, RB, A, B, LPADTOUCH, RPADTOUCH, LT, RT).
# Source: TritonButtons enum in SDL_hidapi_steam_triton.c
class SCButtons(IntEnum):
    # Face buttons
    A      = 0x00000001
    B      = 0x00000002
    X      = 0x00000004
    Y      = 0x00000008
    # Right cluster
    QAM    = 0x00000010
    R3     = 0x00000020   # right stick click
    VIEW   = 0x00000040   # select/view/back
    RGRIP1 = 0x00000080   # right back paddle (Triton R4)
    RGRIP2 = 0x00000100   # right back paddle (Triton R5)
    RB     = 0x00000200   # right bumper
    DPAD_DOWN  = 0x00000400
    DPAD_RIGHT = 0x00000800
    DPAD_LEFT  = 0x00001000
    DPAD_UP    = 0x00002000
    START      = 0x00004000   # menu
    L3         = 0x00008000   # left stick click
    STEAM      = 0x00010000
    LGRIP1     = 0x00020000   # left back paddle (Triton L4)  bound to KEY_LEFTSHIFT in adusk
    LGRIP2     = 0x00040000   # left back paddle (Triton L5)
    LB         = 0x00080000   # left bumper
    RPADJOY_TOUCH = 0x00100000   # right joystick touch
    RPADTOUCH     = 0x00200000   # right trackpad touch
    RPAD          = 0x00400000   # right trackpad click
    RT            = 0x00800000   # right trigger digital click (full pull)
    LPADJOY_TOUCH = 0x01000000   # left joystick touch
    LPADTOUCH     = 0x02000000   # left trackpad touch
    LPAD          = 0x04000000   # left trackpad click
    LT            = 0x08000000   # left trigger digital click
    RGRIP_REST    = 0x10000000   # right grip touch (always-on resting)
    LGRIP_REST    = 0x20000000   # left grip touch
    # adusk expects an "LGRIP" alias  combined mask for either left paddle.
    LGRIP = 0x00060000           # LGRIP1 (L4) | LGRIP2 (L5)
    RGRIP = 0x00000180           # RGRIP1 (R4) | RGRIP2 (R5)


# adusk's controller.py expects an SCI tuple with these exact field names.
# Stick fields are appended on the end so existing positional uses keep working.
# gpitch/gyaw/groll: raw IMU angular velocity (int16, ±32768 = ±2000 °/s) in
# SDL's axis convention  pitch = raw gyro X, yaw = raw Z, roll = -raw Y (the
# swizzle SDL's Triton/Deck drivers apply). Zero until set_imu(True) turns the
# IMU stream on; defaulted so every existing constructor keeps working.
SteamControllerInput = namedtuple(
    'SteamControllerInput',
    'status seq buttons ltrig rtrig lpad_x lpad_y rpad_x rpad_y '
    'lstick_x lstick_y rstick_x rstick_y lpad_force rpad_force '
    'gpitch gyaw groll',
    defaults=(0, 0, 0, 0, 0),
)

# Raw gyro int16 → degrees per second (±32768 = ±2000 °/s full scale).
GYRO_DEG_PER_SEC = 2000.0 / 32768.0

SCI_NULL = SteamControllerInput(
    status=0, seq=0, buttons=0,
    ltrig=0, rtrig=0,
    lpad_x=0, lpad_y=0, rpad_x=0, rpad_y=0,
    lstick_x=0, lstick_y=0, rstick_x=0, rstick_y=0,
)


def _build_setting_report(setting, value):
    """Build the 65-byte Triton feature report that writes ONE settings
    register (ID_SET_SETTINGS_VALUES with a single ControllerSetting)."""
    buf = bytearray(FEATURE_REPORT_LEN + 1)  # +1 for report ID prefix
    buf[0] = FEATURE_REPORT_ID
    buf[1] = ID_SET_SETTINGS_VALUES
    buf[2] = 3                        # length: 1 ControllerSetting = 1+2 bytes
    buf[3] = setting & 0xFF           # settingNum
    buf[4] = value & 0xFF             # settingValue low byte
    buf[5] = (value >> 8) & 0xFF
    return list(buf)


DISABLE_LIZARD_REPORT = _build_setting_report(SETTING_LIZARD_MODE, LIZARD_MODE_OFF)
ENABLE_LIZARD_REPORT = _build_setting_report(SETTING_LIZARD_MODE, LIZARD_MODE_ON)
# IMU streaming on/off (gyro-to-mouse). Sent on demand by set_imu() and
# re-asserted by the lizard watchdog while wanted  the same firmware watchdog
# that restores lizard mode also reverts settings when Steam "dies".
IMU_ON_REPORT = _build_setting_report(SETTING_IMU_MODE, IMU_MODE_RAW)
IMU_OFF_REPORT = _build_setting_report(SETTING_IMU_MODE, IMU_MODE_OFF)


def _build_turn_off_report():
    """Build the feature report that asks the controller to power off.
    Command 0x9F with the 4-byte payload "off!" (same as SDL's driver)."""
    buf = bytearray(FEATURE_REPORT_LEN + 1)  # +1 for report ID prefix
    buf[0] = FEATURE_REPORT_ID
    buf[1] = ID_TURN_OFF_CONTROLLER
    buf[2] = 0x04                     # payload length
    buf[3:7] = b"off!"                # 0x6F 0x66 0x66 0x21
    return list(buf)


TURN_OFF_REPORT = _build_turn_off_report()


# --- Steam Deck feature-report builders ---------------------------------------

def _valve_report(cmd, payload=b""):
    """65-byte feature report for the Deck: [0x00 report id][cmd][len][payload]."""
    buf = bytearray(FEATURE_REPORT_LEN + 1)
    buf[0] = VALVE_FEATURE_REPORT_ID
    buf[1] = cmd & 0xFF
    buf[2] = len(payload) & 0xFF
    buf[3:3 + len(payload)] = payload
    return list(buf)


def _valve_settings_report(*pairs):
    """ID_SET_SETTINGS_VALUES report writing (settingNum, value) registers 
    each a packed 3-byte ControllerSetting (uint8 num + uint16 LE value)."""
    payload = bytearray()
    for num, val in pairs:
        payload.append(num & 0xFF)
        payload.append(val & 0xFF)
        payload.append((val >> 8) & 0xFF)
    return _valve_report(ID_SET_SETTINGS_VALUES, bytes(payload))


# Lizard OFF (we own the pads): clear the firmware's digital keyboard/mouse
# mappings, then kill the pad-mouse modes, the firmware haptic pad click (we
# play our own tick) and the "is Steam alive" watchdog that would silently
# restore lizard mode a few seconds later. Mirrors the kernel hid-steam.c
# deck path + SDL's DisableDeckLizardMode.
DECK_LIZARD_OFF_REPORTS = (
    _valve_report(ID_CLEAR_DIGITAL_MAPPINGS),
    _valve_settings_report(
        (SETTING_SMOOTH_ABSOLUTE_MOUSE, 0),
        (SETTING_LEFT_TRACKPAD_MODE, TRACKPAD_NONE),
        (SETTING_RIGHT_TRACKPAD_MODE, TRACKPAD_NONE),
        (SETTING_LEFT_TRACKPAD_CLICK_PRESSURE, 0xFFFF),
        (SETTING_RIGHT_TRACKPAD_CLICK_PRESSURE, 0xFFFF),
        (SETTING_STEAM_WATCHDOG_ENABLE, 0),
    ),
)

# Lizard ON (hand the pads back to firmware  desktop mouse/keyboard emulation
# just like SteamOS with Steam closed): restore the default digital mappings
# and reload the default settings (hid-steam.c's deck "enable" sequence).
DECK_LIZARD_ON_REPORTS = (
    _valve_report(ID_SET_DEFAULT_DIGITAL_MAPPINGS),
    _valve_report(ID_LOAD_DEFAULT_SETTINGS),
)


def _build_deck_haptic_report(side, cmd, gain=0, freq_hz=0, dur_ms=0):
    """ID_TRIGGER_HAPTIC_CMD (0xEA) MsgTriggerHaptic for the Deck's pad LRAs:
    side 1=left / 2=right / 3=both, cmd 0=off / 3=tone (freq + bounded dur_ms,
    self-terminating), dBgain like the Triton's (0 loud, negative quieter)."""
    payload = bytearray(19)
    payload[0] = side & 0xFF
    payload[1] = cmd & 0xFF
    payload[2] = 0                      # ui_intensity (0 = default)
    payload[3] = gain & 0xFF            # int8 dBgain
    payload[4] = freq_hz & 0xFF
    payload[5] = (freq_hz >> 8) & 0xFF
    d = max(-32768, min(32767, int(dur_ms))) & 0xFFFF
    payload[6] = d & 0xFF
    payload[7] = (d >> 8) & 0xFF
    payload[10] = 0                     # lfo_freq (unused)
    payload[12] = 100                   # lfo_depth % (SDL/Steam default)
    return _valve_report(ID_TRIGGER_HAPTIC_CMD, bytes(payload))


def _build_deck_rumble_report(left, right):
    """ID_TRIGGER_RUMBLE_CMD (0xEB) MsgSimpleRumbleCmd: XInput-style 0..255
    motor intensities scaled to the uint16 speeds SDL's deck driver sends
    (gains 2/0 match SDL_hidapi_steamdeck.c's RumbleJoystick)."""
    l = max(0, min(255, int(left))) << 8
    r = max(0, min(255, int(right))) << 8
    payload = bytearray(9)
    payload[0] = 0                      # unRumbleType
    payload[1] = 0                      # unIntensity: HAPTIC_INTENSITY_SYSTEM
    payload[2] = 0
    payload[3] = l & 0xFF
    payload[4] = (l >> 8) & 0xFF
    payload[5] = r & 0xFF
    payload[6] = (r >> 8) & 0xFF
    payload[7] = 2 & 0xFF               # nLeftGain
    payload[8] = 0                      # nRightGain
    return _valve_report(ID_TRIGGER_RUMBLE_CMD, bytes(payload))


# Triton actuator index -> Deck MsgTriggerHaptic side. The Deck's only haptic
# actuators ARE the two pad LRAs, so the Triton's back rumble motors (3/4)
# fall back to the matching pad  chime/rumble callers keep working.
_DECK_ACTUATOR_SIDE = {
    0: DECK_HAPTIC_SIDE_L,   # HAPTIC_PAD_LEFT
    1: DECK_HAPTIC_SIDE_R,   # HAPTIC_PAD_RIGHT
    3: DECK_HAPTIC_SIDE_L,   # HAPTIC_RUMBLE_LEFT
    4: DECK_HAPTIC_SIDE_R,   # HAPTIC_RUMBLE_RIGHT
}


def _is_deck_state(data):
    """True if `data` is a Deck 64-byte state report (version 1, type 9)."""
    return (len(data) >= DECK_INPUT_REPORT_LEN
            and data[0] == DECK_REPORT_VERSION and data[1] == 0x00
            and data[2] == DECK_REPORT_TYPE_STATE)


def _parse_deck(data):
    """Parse a Deck state report into the shared SCI tuple (Triton bit space)."""
    if not _is_deck_state(data):
        return None
    (pkt, bl, bh,
     lpad_x, lpad_y, rpad_x, rpad_y,
     _ax, _ay, _az, _gx, _gy, _gz, _qw, _qx, _qy, _qz,
     ltrig, rtrig,
     lstick_x, lstick_y, rstick_x, rstick_y,
     press_l, press_r) = _DECK_STRUCT.unpack_from(data, 4)
    buttons = 0
    for m, b in _DECK_BTN_MAP_L:
        if bl & m:
            buttons |= b
    for m, b in _DECK_BTN_MAP_H:
        if bh & m:
            buttons |= b
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=pkt & 0xFF, buttons=buttons,
        ltrig=ltrig, rtrig=rtrig,
        lpad_x=lpad_x, lpad_y=lpad_y,
        rpad_x=rpad_x, rpad_y=rpad_y,
        lstick_x=lstick_x, lstick_y=lstick_y,
        rstick_x=rstick_x, rstick_y=rstick_y,
        lpad_force=press_l, rpad_force=press_r,
        # Same swizzle SDL's deck driver applies: pitch = X, yaw = Z, roll = -Y.
        gpitch=_gx, gyaw=_gz, groll=-_gy,
    )


# --- Steam Controller 2015 wire format ----------------------------------------
# The original Steam Controller streams the same 64-byte ValveInReport_t the
# Deck does, with ucType 1 (ID_CONTROLLER_STATE) and a 60-byte
# ValveControllerStatePacket_t payload (SDL controller_structs.h):
#   [0:2] unReportVersion (uint16 LE, always 1)
#   [2]   ucType    (1 state / 3 wireless event / 4 status event)
#   [3]   ucLength  (0x3C for a state report)
# then, from offset 4:
#   I  unPacketNum
#   3 button bytes | B nLeft | B nRight | 3 pad bytes   (the ulButtons uint64)
#   h  sLeftPadX/Y, sRightPadX/Y        (the left pair doubles as the STICK)
#   H  sTriggerL/R | h accel x3 | h gyro x3 | h quaternion x4
#
# What the 2015 hardware does NOT have, and why it matters downstream: no right
# stick (the right side is all trackpad), no D-pad (the LEFT PAD's quadrants
# are it), one rear paddle per side instead of two, no "..." QAM button, no
# capacitive stick/grip sensors, and no rumble motors  the two trackpad LRAs
# are the only actuators it owns.
SC2015_REPORT_VERSION = 0x01
SC2015_REPORT_TYPE_STATE = 0x01     # ID_CONTROLLER_STATE
SC2015_REPORT_TYPE_WIRELESS = 0x03  # ID_CONTROLLER_WIRELESS (dongle link)
SC2015_REPORT_TYPE_STATUS = 0x04    # ID_CONTROLLER_STATUS (battery)
SC2015_INPUT_REPORT_LEN = 64
# Bytes needed to unpack the buttons/triggers/pads below (through offset 23).
SC2015_INPUT_MIN_LEN = 24
#   I pkt | B b0 | B b1 | B b2 | B nLeft | B nRight | 3x | h lpad/stick XY,
#   h rpad XY
_SC2015_STRUCT = Struct('<IBBBBB3xhhhh')
# Raw gyro X/Y/Z (3x int16 LE) at report offset 34  live only once the IMU
# stream is switched on via SETTING_IMU_MODE (see set_imu).
_SC2015_GYRO_OFFSET = 34
_SC2015_GYRO_MIN_LEN = 40
_SC2015_GYRO_STRUCT = Struct('<hhh')

# 2015 SC button bits -> Triton SCButtons bits, per button byte. Masks are
# SDL_hidapi_steam.c's STEAM_*_MASK values shifted into their own byte (that
# file numbers them across the low 24 bits of ulButtons); plain ints because
# enum lookups are too slow for the hot path.
#
# Deliberately NOT mapped:
#   * b1 bits 0-3, the firmware's left-pad QUADRANT "D-pad" bits. The pad has
#     one click switch, so a quadrant press already sets LEFTPAD_CLICKED 
#     mapping the quadrants too would fire a D-pad bind AND the pad-click bind
#     from one press. The pad's position is available as lpad_x/lpad_y, which
#     is what every trackpad feature in this app actually reads.
#   * b2 bit 7 (LEFTPAD_AND_JOYSTICK), a routing flag rather than a button 
#     see the stick/pad demux in _parse_sc2015.
_SC2015_BTN_MAP_0 = tuple((m, int(b)) for m, b in (
    (0x01, 0x00800000),   # right trigger full pull -> RT
    (0x02, 0x08000000),   # left trigger full pull  -> LT
    (0x04, 0x00000200),   # right bumper            -> RB
    (0x08, 0x00080000),   # left bumper             -> LB
    (0x10, 0x00000008),   # Y (north)               -> Y
    (0x20, 0x00000002),   # B (east)                -> B
    (0x40, 0x00000004),   # X (west)                -> X
    (0x80, 0x00000001),   # A (south)               -> A
))
_SC2015_BTN_MAP_1 = tuple((m, int(b)) for m, b in (
    (0x10, 0x00000040),   # left "back" arrow (◀)   -> VIEW
    (0x20, 0x00010000),   # Steam                   -> STEAM
    (0x40, 0x00004000),   # right "start" arrow (▶) -> START
    (0x80, 0x00020000),   # left grip paddle        -> LGRIP1
))
_SC2015_BTN_MAP_2 = tuple((m, int(b)) for m, b in (
    (0x01, 0x00000080),   # right grip paddle       -> RGRIP1
    (0x02, 0x04000000),   # left pad click          -> LPAD
    (0x04, 0x00400000),   # right pad click         -> RPAD
    (0x08, 0x02000000),   # left pad touch          -> LPADTOUCH
    (0x10, 0x00200000),   # right pad touch         -> RPADTOUCH
    (0x40, 0x00008000),   # analog stick click      -> L3
))
# b2 flags read directly by the axis demux rather than mapped to a button.
_SC2015_LPAD_TOUCH_BIT = 0x08
_SC2015_LPAD_AND_JOY_BIT = 0x80

# Analog triggers arrive as one byte each. SDL widens them with (n << 7) | n
# and then remaps 0..SC2015_TRIGGER_FULL onto the full int16 range, because the
# physical trigger saturates well before the byte does (the last of the travel
# is the digital click). Reproduced here so the actuation sliders  which are
# calibrated against the Triton's 0..32767  mean the same pull on both.
SC2015_TRIGGER_FULL = 26000
_SC2015_TRIGGER_LUT = tuple(
    min(32767, (((n << 7) | n) * 32767) // SC2015_TRIGGER_FULL)
    for n in range(256))

# Battery. The 2015 controller runs on two AA cells, so there is no charging
# state to report and ucBatteryLevel is 0 on most firmware  the usable signal
# is the pack voltage. These endpoints bracket a 2-cell pack from fresh to
# flat; the percentage is an ESTIMATE, flagged as such in the tray tooltip.
SC2015_BATTERY_MIN_MV = 2100
SC2015_BATTERY_MAX_MV = 3100

# Wireless-event codes (SDL's D0G_WIRELESS_*), byte 4 of a ucType 3 report.
SC2015_WIRELESS_DISCONNECTED = 1
SC2015_WIRELESS_ESTABLISHED = 2
SC2015_WIRELESS_NEWLYPAIRED = 3


def _is_valve_legacy_report(data, ucType):
    """True if `data` is a 64-byte ValveInReport_t of type `ucType`."""
    return (len(data) >= 4
            and data[0] == SC2015_REPORT_VERSION and data[1] == 0x00
            and data[2] == ucType)


def _is_sc2015_state(data):
    """True if `data` is a 2015 Steam Controller state report."""
    return (len(data) >= SC2015_INPUT_MIN_LEN
            and _is_valve_legacy_report(data, SC2015_REPORT_TYPE_STATE))


# Last seen [lpad_x, lpad_y, lstick_x, lstick_y] for the shared-axis demux in
# _parse_sc2015. A plain list (not per-instance state) because only ONE 2015
# controller drives the takeover runtime at a time and the parser is a module
# function; a stale value can at worst hold a released stick/pad one frame
# longer.
_SC2015_LAST = [0, 0, 0, 0]


def _parse_sc2015(data):
    """Parse a 2015 Steam Controller state report into the shared SCI tuple
    (Triton bit space), or None if it isn't one."""
    if not _is_sc2015_state(data):
        return None
    (pkt, b0, b1, b2, ntrig_l, ntrig_r,
     ax, ay, rpad_x, rpad_y) = _SC2015_STRUCT.unpack_from(data, 4)
    buttons = 0
    for m, b in _SC2015_BTN_MAP_0:
        if b0 & m:
            buttons |= b
    for m, b in _SC2015_BTN_MAP_1:
        if b1 & m:
            buttons |= b
    for m, b in _SC2015_BTN_MAP_2:
        if b2 & m:
            buttons |= b
    # Stick/pad demux: the left pad and the analog stick SHARE one pair of
    # axes. The finger-down bit says which one the current frame carries; the
    # "and joystick" bit says the firmware is interleaving both across frames,
    # in which case the other one holds its last value instead of snapping to
    # centre (that snap would fight every stick and trackpad feature we have).
    # Mirrors SDL's FormatStatePacketUntilGyro.
    lpad_x = lpad_y = lstick_x = lstick_y = 0
    both = b2 & _SC2015_LPAD_AND_JOY_BIT
    if b2 & _SC2015_LPAD_TOUCH_BIT:
        lpad_x, lpad_y = ax, ay
        _SC2015_LAST[0], _SC2015_LAST[1] = ax, ay
        if both:
            lstick_x, lstick_y = _SC2015_LAST[2], _SC2015_LAST[3]
    else:
        lstick_x, lstick_y = ax, ay
        _SC2015_LAST[2], _SC2015_LAST[3] = ax, ay
        if both:
            lpad_x, lpad_y = _SC2015_LAST[0], _SC2015_LAST[1]
    if len(data) >= _SC2015_GYRO_MIN_LEN:
        _gx, _gy, _gz = _SC2015_GYRO_STRUCT.unpack_from(data,
                                                        _SC2015_GYRO_OFFSET)
    else:
        _gx = _gy = _gz = 0
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=pkt & 0xFF, buttons=buttons,
        ltrig=_SC2015_TRIGGER_LUT[ntrig_l], rtrig=_SC2015_TRIGGER_LUT[ntrig_r],
        lpad_x=lpad_x, lpad_y=lpad_y,
        rpad_x=rpad_x, rpad_y=rpad_y,
        lstick_x=lstick_x, lstick_y=lstick_y,
        # No right stick on this hardware; the SCI field stays centred.
        rstick_x=0, rstick_y=0,
        # No pad pressure sensors either (the pads click mechanically).
        lpad_force=0, rpad_force=0,
        # Same swizzle SDL applies: pitch = X, yaw = Z, roll = -Y.
        gpitch=_gx, gyaw=_gz, groll=-_gy,
    )


def _parse_sc2015_battery(data):
    """Parse a 2015 Steam Controller status report (ucType 4) into a
    SteamControllerBattery, or None if it isn't one / carries no reading.

    Payload after the 4-byte header: unPacketNum(4) sEventCode(2)
    unStateFlags(2) sBatteryVoltage(2) ucBatteryLevel(1)."""
    if not _is_valve_legacy_report(data, SC2015_REPORT_TYPE_STATUS):
        return None
    if len(data) < 15:
        return None
    voltage = data[12] | (data[13] << 8)
    level = data[14]
    if 0 < level <= 100:
        percent = level
    elif SC2015_BATTERY_MIN_MV <= voltage <= 6000:
        # Firmware left the level byte empty  estimate from pack voltage.
        span = SC2015_BATTERY_MAX_MV - SC2015_BATTERY_MIN_MV
        percent = int(round(
            100.0 * (min(voltage, SC2015_BATTERY_MAX_MV)
                     - SC2015_BATTERY_MIN_MV) / span))
        percent = max(1, min(100, percent))
    else:
        return None
    # Two AA cells: never charging, never "charge complete".
    return SteamControllerBattery(
        percent=percent, charge_state=CHARGE_STATE_DISCHARGING,
        charging=False, charge_complete=False, voltage_mv=voltage)


# Lizard OFF for the 2015 controller: drop the firmware's digital keyboard /
# mouse mappings and put both trackpads in TRACKPAD_NONE so the firmware stops
# driving the cursor while WE own the pads. The Deck's extra registers (click
# pressure, the Steam watchdog) don't exist on this firmware  the kernel's
# hid-steam.c writes exactly these two for a non-Deck unit.
SC2015_LIZARD_OFF_REPORTS = (
    _valve_report(ID_CLEAR_DIGITAL_MAPPINGS),
    _valve_settings_report(
        (SETTING_SMOOTH_ABSOLUTE_MOUSE, 0),
        (SETTING_LEFT_TRACKPAD_MODE, TRACKPAD_NONE),
        (SETTING_RIGHT_TRACKPAD_MODE, TRACKPAD_NONE),
    ),
)

# Lizard ON: hand the pads back to firmware mouse/keyboard emulation.
SC2015_LIZARD_ON_REPORTS = (
    _valve_report(ID_SET_DEFAULT_DIGITAL_MAPPINGS),
    _valve_report(ID_LOAD_DEFAULT_SETTINGS),
)

# Power-off, Valve legacy framing: command 0x9F with the payload "off!".
SC2015_TURN_OFF_REPORT = _valve_report(ID_TURN_OFF_CONTROLLER, b"off!")

# The 2015 pads' haptic report indexes its two LRAs the other way round from
# every other actuator id in this file (0 = RIGHT), a quirk the kernel driver
# also has to undo. Map our actuator ids through this rather than flipping the
# constants everyone else shares. The rumble "motors" fold onto the matching
# pad, exactly as they do on the Deck  this hardware has no motors at all.
_SC2015_HAPTIC_PAD = {
    HAPTIC_PAD_LEFT: 1,
    HAPTIC_PAD_RIGHT: 0,
    HAPTIC_RUMBLE_LEFT: 1,
    HAPTIC_RUMBLE_RIGHT: 0,
}

# ID_TRIGGER_HAPTIC_PULSE gain is a signed dB trim like the Triton's, but its
# useful band is much narrower (the kernel documents -24..+6). Our shared gain
# constants live on the Triton's far wider scale, so they're squeezed into this
# range instead of being sent raw  a Triton "-80 = inaudible" would otherwise
# clip to something loud here.
SC2015_GAIN_MIN = -24
SC2015_GAIN_MAX = 6
# Widest gain the shared constants use (HAPTIC_CHIME_GAIN=3 at the top,
# RUMBLE_GAIN_MIN=-40 at the bottom), used to rescale into the band above.
_SC2015_SRC_GAIN_MIN = -40.0
_SC2015_SRC_GAIN_MAX = 6.0


def _sc2015_gain(gain):
    """Rescale one of the shared Triton gain constants into the 2015 pad's
    narrower dB band, then wrap to the report's unsigned byte.

    The shared constants are signed dB trims, but a couple are written as the
    unsigned byte they end up as on the Triton (HAPTIC_DEFAULT_GAIN = 0xFE is
    -2), so re-sign anything above 127 before rescaling  otherwise "quiet"
    would read as +254 and clamp to the loudest setting there is."""
    g = float(gain)
    if g > 127:
        g -= 256.0
    g = max(_SC2015_SRC_GAIN_MIN, min(_SC2015_SRC_GAIN_MAX, g))
    frac = ((g - _SC2015_SRC_GAIN_MIN)
            / (_SC2015_SRC_GAIN_MAX - _SC2015_SRC_GAIN_MIN))
    return int(round(SC2015_GAIN_MIN
                     + frac * (SC2015_GAIN_MAX - SC2015_GAIN_MIN))) & 0xFF


def _build_sc2015_haptic_report(pad, freq_hz, gain, count):
    """ID_TRIGGER_HAPTIC_PULSE (0x8F): pulse one trackpad LRA `count` times
    with `duration` µs on / `interval` µs off. Frequency is expressed as the
    two half-period times, so a tone of `freq_hz` for N cycles is
    duration = interval = 500000/freq_hz, count = N. Payload:
    [pad][duration LE16][interval LE16][count LE16][gain]."""
    half = max(1, min(0xFFFF, int(500000.0 / max(1, int(freq_hz)))))
    c = max(1, min(0xFFFF, int(count)))
    payload = bytearray(8)
    payload[0] = pad & 0xFF
    payload[1] = half & 0xFF
    payload[2] = (half >> 8) & 0xFF
    payload[3] = half & 0xFF
    payload[4] = (half >> 8) & 0xFF
    payload[5] = c & 0xFF
    payload[6] = (c >> 8) & 0xFF
    payload[7] = _sc2015_gain(gain)
    return _valve_report(ID_TRIGGER_HAPTIC_PULSE, bytes(payload))


def _build_haptic_tone_report(actuator, freq_hz, gain, count=0x7FFF):
    """Build the 0x83 LFO-tone OUTPUT report: play `freq_hz` on `actuator`.
    `count` (bytes 5-6) is the burst length; 0x7FFF ~= continuous (until a
    stop), while a small value plays just a few cycles for a crisp click."""
    f = int(freq_hz) & 0xFFFF
    c = int(count) & 0xFFFF
    buf = bytearray(HID_OUTPUT_REPORT_LEN)  # 65 bytes, id included
    buf[0] = ID_OUT_HAPTIC_LFO_TONE
    buf[1] = actuator & 0xFF
    buf[2] = gain & 0xFF
    buf[3] = f & 0xFF
    buf[4] = (f >> 8) & 0xFF
    buf[5] = c & 0xFF
    buf[6] = (c >> 8) & 0xFF
    return bytes(buf)


def _build_haptic_stop_report(actuator):
    """Build the 0x82 stop OUTPUT report for `actuator`."""
    buf = bytearray(HID_OUTPUT_REPORT_LEN)  # 65 bytes, id included
    buf[0] = ID_OUT_HAPTIC_STOP
    buf[1] = actuator & 0xFF
    return bytes(buf)


def _tone_count(freq_hz, seconds):
    """Burst length (cycles) for a tone of `freq_hz` that lasts ~`seconds`,
    clamped to the report's 16-bit count field. Used to make every sustained
    tone self-expiring so a crash/hard-kill can't leave an actuator buzzing
    (the controller stops the actuator itself once the burst finishes)."""
    return min(0xFFFF, max(1, int(freq_hz * seconds)))


def _rumble_gain(intensity):
    """Map an XInput motor intensity (1..255) to a signed tone gain within the
    safe [RUMBLE_GAIN_MIN, RUMBLE_GAIN_MAX] range (higher = louder)."""
    i = max(1, min(255, int(intensity)))
    return int(round(RUMBLE_GAIN_MIN
                     + (i / 255.0) * (RUMBLE_GAIN_MAX - RUMBLE_GAIN_MIN)))


def _enumerate_data_interfaces(kinds=HID_KINDS):
    """Vendor-specific HID data interfaces for the requested controller
    families, Steam Controller candidates first. SC: usage page 0xFF00 usage 1
    on the dongle (PID 0x1304, one per paired controller) or the wired unit
    (0x1302). Steam Deck: the vendor interface of PID 0x1205 (usage page
    0xFFFF usage 1  the same VID/PID also exposes the lizard boot
    mouse/keyboard interfaces, which the usage filter and ultimately the
    input-report probe in _open_first_responsive keep us off).
    2015 SC: usage page 0xFF00 usage 1 on the wired unit (PID 0x1102, a single
    vendor interface) or the wireless receiver (0x1142, FOUR of them  one per
    pairing slot, and only the slots with a controller actually paired stream
    anything, which the probe sorts out).

    Linux's hidapi hidraw backend reports usage_page=0/usage=0 for every
    interface (it can't read those without the libusb backend), so on that
    backend we keep everything and let the input-probe in _open_first_responsive
    filter to the actual data interfaces. Each entry carries '_kind' so the
    opener knows which family it got."""
    out = []
    if "sc" in kinds:
        sc = []
        for pid in (PRODUCT_ID_PROTEUS, PRODUCT_ID_WIRED):
            for d in hid.enumerate(VENDOR_ID, pid):
                up = d.get('usage_page', 0)
                us = d.get('usage', 0)
                if (up == 0xFF00 and us == 1) or (up == 0 and us == 0):
                    d = dict(d)
                    d['_kind'] = "sc"
                    sc.append(d)
        sc.sort(key=lambda d: (d.get('product_id', 0),
                               d.get('interface_number', 0)))
        out.extend(sc)
    if "steam_deck" in kinds:
        deck = []
        for d in hid.enumerate(VENDOR_ID, PRODUCT_ID_DECK):
            up = d.get('usage_page', 0)
            us = d.get('usage', 0)
            if (up in (0xFF00, 0xFFFF) and us == 1) or (up == 0 and us == 0):
                d = dict(d)
                d['_kind'] = "steam_deck"
                deck.append(d)
        deck.sort(key=lambda d: d.get('interface_number', 0))
        out.extend(deck)
    if "sc2015" in kinds:
        old = []
        for pid in (PRODUCT_ID_SC2015, PRODUCT_ID_SC2015_DONGLE):
            for d in hid.enumerate(VENDOR_ID, pid):
                # Interface 0 of both products is the firmware's boot
                # mouse/keyboard (generic-desktop usage page)  the vendor
                # filter keeps us on the data interfaces only, except on the
                # hidraw backend, which reports no usage at all (see above).
                up = d.get('usage_page', 0)
                us = d.get('usage', 0)
                if (up == 0xFF00 and us == 1) or (up == 0 and us == 0):
                    d = dict(d)
                    d['_kind'] = "sc2015"
                    old.append(d)
        old.sort(key=lambda d: (d.get('product_id', 0),
                                d.get('interface_number', 0)))
        out.extend(old)
    return out


def enumerate_data_interfaces(kinds=HID_KINDS):
    """Public view of _enumerate_data_interfaces  every vendor data interface
    for the requested families, each dict carrying 'path' and '_kind'.

    The tray's multi-controller supervisor uses this to see how many physical
    Steam Controllers / Decks are attached (one interface per paired pad on the
    dongle) so it can hand each unclaimed one its own reader + virtual pad.
    Listing only; no handle is opened, so it works while we (or Steam) hold a
    device. Silent/unpaired interfaces are still listed  only the opener's
    input-report probe can tell those apart."""
    return _enumerate_data_interfaces(kinds)


def present_product_ids():
    """Set of Steam Controller product IDs (e.g. PRODUCT_ID_PROTEUS for the
    wireless receiver/puck, PRODUCT_ID_WIRED for a USB-C-tethered controller)
    currently enumerable on USB. Cheap presence probe  lists HID, never opens
    a handle  so it works whether or not we (or Steam) hold the device, and
    even when nothing is paired/connected. Used by the tray's device watcher."""
    pids = set()
    try:
        for d in hid.enumerate(VENDOR_ID, 0):
            pid = d.get('product_id')
            if pid:
                pids.add(pid)
    except Exception:
        pass
    return pids


def present_hid_kinds(pids=None):
    """Which HID controller families ("sc" / "steam_deck") have hardware
    enumerable right now, in driver preference order. Presence of the SC
    dongle does NOT mean a controller is awake  the opener's probe decides
    that  this is just the cheap 'worth trying' signal for the launcher."""
    if pids is None:
        pids = present_product_ids()
    out = []
    for kind in HID_KINDS:
        if any(p in pids for p in HID_KIND_PIDS[kind]):
            out.append(kind)
    return tuple(out)


# Battery snapshot handed to callers via SteamController.get_battery().
#   percent         0..100
#   charge_state    raw CHARGE_STATE_* byte
#   charging        True while a charger is supplying power (charging, source-
#                   validate, or charge-complete)  mirrors the reference's
#                   IsCharging (anything but Discharging/Reset).
#   charge_complete True once the pack is full and the charger has stopped.
#   voltage_mv      battery voltage in millivolts (diagnostic).
SteamControllerBattery = namedtuple(
    'SteamControllerBattery',
    'percent charge_state charging charge_complete voltage_mv'
)


def _parse_battery(data):
    """Parse a 0x43 power-status report into a SteamControllerBattery, or None
    if the frame isn't a (valid) battery report.

    Windows hidapi delivers 17 bytes; Linux's hidraw backend trims it to 15
    (firmware-side descriptor differs). All fields we actually use sit in the
    first 5 bytes (report id + state + percent + voltage LE), so we only
    require that much."""
    if len(data) < 5 or data[0] != TRITON_BATTERY_REPORT_ID:
        return None
    percent = data[2]
    if percent > 100:
        return None  # firmware sends 0xFF-ish placeholders before it has a reading
    cs = data[1]
    charging = cs in (CHARGE_STATE_CHARGING,
                      CHARGE_STATE_SOURCE_VALIDATE,
                      CHARGE_STATE_CHARGING_DONE)
    # A 0% reading while not charging isn't a real level  the firmware emits it
    # as the controller powers off (e.g. the Steam+Y turn-off) and before it has
    # taken a reading. Treat it as "no reading" so it can't fire a bogus 0%
    # critical-battery warning. (Mirrors the reference's IsDisplayableController-
    # Battery: a battery is real only if it's charging or percent > 0.)
    if percent == 0 and not charging:
        return None
    return SteamControllerBattery(
        percent=percent,
        charge_state=cs,
        charging=charging,
        charge_complete=cs == CHARGE_STATE_CHARGING_DONE,
        voltage_mv=data[3] | (data[4] << 8),
    )


def _parse_triton(data: bytes) -> SteamControllerInput:
    """Parse a 54-byte Triton input report into the SCI tuple."""
    if len(data) < TRITON_INPUT_MIN_LEN or data[0] not in TRITON_INPUT_REPORT_IDS:
        return None
    # Skip byte 0 (report ID 0x45 / legacy 0x42). Layout after that:
    #   B  seq            (1 byte)
    #   I  buttons        (4 bytes, uint32 LE)
    #   h  sTriggerLeft   (2 bytes, int16)
    #   h  sTriggerRight  (2 bytes, int16)
    #   h  sLeftStickX
    #   h  sLeftStickY
    #   h  sRightStickX
    #   h  sRightStickY
    #   h  sLeftPadX
    #   h  sLeftPadY
    #   H  sPressureLeft  (ignored)
    #   h  sRightPadX
    #   h  sRightPadY
    #   H  sPressureRight (ignored)
    (seq, buttons, ltrig, rtrig,
     lstick_x, lstick_y, rstick_x, rstick_y,
     lpad_x, lpad_y, _pL, rpad_x, rpad_y, _pR) = _TRITON_STRUCT.unpack_from(data, 1)
    # Triton firmware wire-swap: the physical Menu (≡ hamburger) button reports
    # on bit 0x40 and the View (⧉ two-boxes) button on bit 0x4000  the reverse
    # of the names our SCButtons enum (VIEW=0x40, START=0x4000) and the bundled
    # sc2-research capture table assigned; both mislabel the pair. Confirmed on
    # real hardware (see _view_menu_probe): pressing ≡ set 0x40, pressing ⧉ set
    # 0x4000. Swap the two bits HERE, at the single input source, so every
    # downstream consumer (live viewer, picker glyphs + binds, chords, OSK)
    # sees the app-wide convention ≡=START(0x4000) / ⧉=VIEW(0x40) as true.
    if buttons & 0x4040:                     # 0x40 VIEW | 0x4000 START
        _v = buttons & 0x40
        _s = buttons & 0x4000
        buttons = (buttons & ~0x4040) | (0x4000 if _v else 0) | (0x40 if _s else 0)
    # IMU gyro  present in both state variants (the short battery/status
    # frames never reach here). Frozen zeros until set_imu(True).
    if len(data) >= _TRITON_GYRO_MIN_LEN:
        _gx, _gy, _gz = _TRITON_GYRO_STRUCT.unpack_from(data, _TRITON_GYRO_OFFSET)
    else:
        _gx = _gy = _gz = 0
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=seq, buttons=buttons,
        ltrig=ltrig, rtrig=rtrig,
        lpad_x=lpad_x, lpad_y=lpad_y,
        rpad_x=rpad_x, rpad_y=rpad_y,
        lstick_x=lstick_x, lstick_y=lstick_y,
        rstick_x=rstick_x, rstick_y=rstick_y,
        lpad_force=_pL, rpad_force=_pR,
        # Same swizzle SDL's Triton driver applies: pitch = X, yaw = Z, roll = -Y.
        gpitch=_gx, gyaw=_gz, groll=-_gy,
    )


# HID-open retry: toggling a block setting ("Block SteamInput Steam Controller grab"
# or "Block SteamInput Xbox Controller grab") closes the current handle and immediately
# reopens in the other mode (shared<->exclusive). The OS can take a moment to
# release the just-closed handle, so the first reopen can hit a transient
# sharing violation in EITHER direction. Retry a few times so the toggle applies
# live (turning the block both on AND off) instead of only after a restart.
# Retries run only on a *failed* open, so a cold/first open  the normal case,
# and every interface probe in _open_first_responsive  pays nothing.
OPEN_RETRY_ATTEMPTS = 5
OPEN_RETRY_DELAY = 0.1


class SteamController:
    """API-compatible with adusk's expectations:
        SteamController(callback, callback_args=None)
        sc.run()
        sc.addExit()
    """

    def __init__(self, callback, callback_args=None, passive=False, exclusive=False,
                 kinds=("sc",), paths=None, exclude_paths=None):
        self._cb = callback
        self._cb_args = callback_args if callback_args is not None else ()
        self._passive = passive
        # Which controller families to try ("sc" and/or "steam_deck"); the
        # first responsive interface wins, SC candidates first. After a
        # successful open, `kind` names the family actually driving.
        # LINUX DEFAULT IS SC-ONLY: the Deck is still an SDL pad on this
        # platform (tray_linux.py has no takeover watcher yet  see
        # pads.HID_KINDS), so the passive chord watcher and the OSK's
        # SteamHidSource must not grab the Deck's vendor interface and fight
        # SDL over lizard mode. Full Deck support is in this driver, ready
        # for the takeover port: pass kinds=("steam_deck",) or both.
        self._kinds = tuple(k for k in HID_KINDS if k in (kinds or HID_KINDS))
        self.kind = None
        # Multi-controller support (2+ Steam Controllers on one PC  the dongle
        # exposes one data interface per paired pad). Both are HID *path*
        # filters applied to the enumeration in _open_first_responsive:
        #   paths          restrict the search to these paths, in this order.
        #                  Player 2+ readers pass the set of paths nobody has
        #                  claimed yet and still probe for the first RESPONSIVE
        #                  one, so silent/unpaired interfaces are skipped just
        #                  like in the unrestricted case.
        #   exclude_paths  never open these. A callable is re-evaluated at open
        #                  time, which is what player 1 passes: its reader is
        #                  torn down and rebuilt constantly (mode changes, OSK
        #                  toggles) and the set of paths owned by other players
        #                  moves underneath it. Without this, a rebuild could
        #                  grab a device another reader already holds  hidapi
        #                  happily opens the same path twice in shared mode, and
        #                  both readers would then drive off the same pad.
        self._paths = tuple(paths) if paths is not None else None
        self._exclude_paths = exclude_paths
        # HID path this instance actually opened (None until a successful open).
        self.path = None
        # When True, open the controller with no sharing so other apps (Steam)
        # can't grab it. Falls back to shared if exclusive open is denied.
        self._exclusive = exclusive
        # Other dongle/controller HID interfaces held open (claimed but never
        # read) purely to deny Steam access to them too -- see
        # _claim_remaining_interfaces. Only populated when self._exclusive.
        self._blocked_devs = []
        self._dev = None
        self._dev_lock = threading.Lock()
        self._exit = threading.Event()
        self._lizard_thread = None
        # True once this instance has successfully opened a controller. Lets the
        # launcher tell "device absent" (open failed) from "ran then was kicked"
        # so it can back off reconnect attempts only when nothing is there.
        self.opened = False
        # Lizard state the watchdog keeps re-asserting:
        # - Non-passive (OSK / gamepad mode): we want firmware kb/mouse OFF
        #   (Triton inputs only). Watchdog re-sends DISABLE_LIZARD.
        # - Passive (chord watcher / desktop mode): we want firmware kb/mouse
        #   ON so the trackpads emulate the system mouse  that's the
        #   desktop-mode UX users expect. Watchdog re-sends ENABLE_LIZARD.
        #   If we left this at False here, the watchdog's libusb fallback
        #   would occasionally win a race against hidapi's iface-2 claim,
        #   land a DISABLE_LIZARD report, and the trackpad mouse would die
        #   until the firmware's own re-enable timer fires.
        # set_lizard() flips this on the fly  tray.py uses that to let
        # "hold Steam" briefly re-enable firmware mouse/kb in gamepad mode.
        self._lizard_enabled = bool(passive)
        # Whether the IMU stream should be on (gyro-to-mouse active). Sent by
        # set_imu() and re-asserted by the deck watchdog tick. Linux Triton
        # caveat: runtime feature reports via the hidapi libusb backend are
        # malformed (see _lizard_watchdog)  the write is attempted but may
        # not land until the next cold open, same as runtime lizard changes.
        self._imu_enabled = False
        # Last battery status seen on the wire (a SteamControllerBattery, or
        # None until the controller streams its first 0x43 power report). Set on
        # the read thread, read via get_battery(); a single attribute
        # read/write of an immutable tuple is atomic under the GIL, so no lock.
        self._battery = None
        # Monotonic count of accepted 0x43 battery frames. The battery report
        # streams at only ~0.4 Hz, so callers use this to tell a genuinely fresh
        # reading from the same one re-read  enough to confirm a charge-state
        # change across two DISTINCT frames (see battery_frame_id).
        self._battery_seq = 0
        # time.monotonic() of the last input/battery frame, for get_battery()'s
        # freshness check (see BATTERY_FRESH_SECONDS).
        self._last_frame_t = 0.0
        # Game-rumble keepalive (see set_rumble / _rumble_keepalive): the last
        # requested (large, small) motor intensities, and the daemon thread that
        # re-arms the self-expiring bursts while they're non-zero. A single tuple
        # read/write is atomic under the GIL; the thread is started lazily on the
        # first non-zero rumble and dies with this instance.
        self._rumble_state = (0, 0)
        self._rumble_thread = None

    def _open_device(self, path):
        """Open `path`. In exclusive mode, try a no-sharing open (blocks Steam)
        and fall back to normal shared hidapi if that's denied  e.g. because
        Steam already holds the device  so the controller still works.

        Both opens are retried (see OPEN_RETRY_ATTEMPTS): a block toggle reopens
        in the other mode right after closing our own handle, which can briefly
        race the OS releasing it in either direction. Retries fire only on a
        failed open, so normal opens and the probe loop are unaffected."""
        last_err = None
        try:
            from . import winhid
        except ImportError:
            # Linux builds exclude the Windows-only winhid module (no
            # CreateFileW exclusive-open equivalent for hidraw exists), so
            # "Block SteamInput Steam Controller grab" can't do anything here
            #  skip straight to the shared open below without the noise of
            # a doomed retry loop.
            winhid = None
        if self._exclusive and winhid is not None:
            for attempt in range(OPEN_RETRY_ATTEMPTS):
                try:
                    dev = winhid.ExclusiveHidDevice()
                    dev.open_path(path)
                    print("steamcontroller: opened EXCLUSIVE (Steam blocked)")
                    return dev
                except Exception as e:
                    # A sharing violation right after our own close clears once
                    # the OS releases the device; a genuine conflict (Steam holds
                    # it) won't, so cap the retries and then fall back to shared.
                    last_err = e
                    if attempt + 1 < OPEN_RETRY_ATTEMPTS:
                        time.sleep(OPEN_RETRY_DELAY)
            print(f"steamcontroller: exclusive open denied ({last_err}); "
                  "falling back to shared")
        # Shared hidapi open  retried for the same race when a block is toggled
        # OFF and we reopen shared right after releasing the exclusive handle.
        for attempt in range(OPEN_RETRY_ATTEMPTS):
            try:
                dev = hid.device()
                dev.open_path(path)
                return dev
            except Exception as e:
                last_err = e
                if attempt + 1 < OPEN_RETRY_ATTEMPTS:
                    time.sleep(OPEN_RETRY_DELAY)
        raise last_err if last_err is not None else OSError(f"could not open {path}")

    def _open_first_responsive(self):
        global _LAST_GOOD_PATH
        candidates = _enumerate_data_interfaces(self._kinds)

        # Multi-controller path filters (see __init__): keep only the paths this
        # reader is allowed to claim, so two readers never open the same device.
        if self._paths is not None:
            order = {p: i for i, p in enumerate(self._paths)}
            candidates = [c for c in candidates if c['path'] in order]
            candidates.sort(key=lambda c: order[c['path']])
        if self._exclude_paths is not None:
            blocked = self._exclude_paths
            if callable(blocked):
                try:
                    blocked = blocked()
                except Exception:
                    blocked = ()
            blocked = set(blocked or ())
            if blocked:
                candidates = [c for c in candidates if c['path'] not in blocked]

        if not candidates:
            raise RuntimeError(
                "No Steam Controller / Steam Deck interface found "
                f"(VID 0x{VENDOR_ID:04X}, kinds {self._kinds})."
            )

        # On Linux we do NOT touch firmware lizard mode at all on Triton
        # hardware. Testing shows the SET_SETTINGS write to setting #9
        # apparently persists in firmware non-volatile state  once we
        # disable lizard, no value we tried (1, 3) re-enables trackpad
        # mouse emulation, and only physically power-cycling the puck
        # restores defaults. So instead we trust that Triton input
        # reports flow alongside firmware kb/mouse emulation (the
        # original codebase comment claiming otherwise was based on a
        # behaviour we couldn't reproduce on this hardware) and let the
        # firmware-default trackpad-mouse stay alive in both passive
        # and non-passive sessions.

        # Try the last-known-good interface first. Stable sort: the matching
        # path (key False/0) moves to the front, everything else keeps order.
        # Skipped for a path-restricted reader  that caller supplied its own
        # priority order, and player 1's stickiness must not drag a player-2
        # reader onto the device player 1 is about to rebuild onto.
        if _LAST_GOOD_PATH is not None and self._paths is None:
            candidates.sort(key=lambda c: c['path'] != _LAST_GOOD_PATH)

        last_err = None
        for cand in candidates:
            path = cand['path']
            kind = cand.get('_kind', "sc")
            try:
                dev = self._open_device(path)
            except Exception as e:
                last_err = e
                continue

            # Tell the controller to stop pretending to be a keyboard/mouse,
            # unless we're in passive mode (just listening for hotkeys).
            if not self._passive:
                try:
                    for report in self._lizard_reports(False, kind):
                        rc = dev.send_feature_report(report)
                    print(f"steamcontroller: disable-lizard ({kind}) on iface "
                          f"{cand['interface_number']} returned {rc}")
                except Exception as e:
                    last_err = e
                    dev.close()
                    continue

            # Probe: wait briefly for input reports. Unpaired wireless ports
            # (and the Deck's lizard mouse/kb interfaces) stay silent so we
            # keep moving in that case.
            dev.set_nonblocking(0)
            deadline = time.time() + 1.5
            got_input = False
            while time.time() < deadline:
                try:
                    data = dev.read(64, 200)
                except Exception as e:
                    last_err = e
                    break
                if not data:
                    continue
                if kind == "steam_deck":
                    if _is_deck_state(data):
                        got_input = True
                        break
                elif kind == "sc2015":
                    if _is_sc2015_state(data):
                        got_input = True
                        break
                elif (len(data) >= TRITON_INPUT_MIN_LEN
                        and data[0] in TRITON_INPUT_REPORT_IDS):
                    got_input = True
                    break

            if got_input:
                self._dev = dev
                self.kind = kind
                self.path = path
                # Only the unrestricted (player 1) opener updates the sticky
                # last-good path; a player-2 reader remembering ITS path here
                # would send player 1 chasing the wrong device on every rebuild.
                if self._paths is None:
                    _LAST_GOOD_PATH = path
                print(f"steamcontroller: opened {kind} iface "
                      f"{cand['interface_number']}")
                if self._exclusive:
                    self._claim_remaining_interfaces(candidates, path)
                return

            if self._exclusive:
                # Keep this idle interface claimed too (see _blocked_devs):
                # libusb's open already detached its kernel usbhid driver and
                # removed its /dev/hidrawN (see linux-hidapi-libusb-exclusivity
                # memory) -- holding it denies Steam access to every dongle
                # HID interface, not just the one carrying live input, in case
                # SteamInput's Steam-button detection reads a different one.
                self._blocked_devs.append(dev)
            else:
                dev.close()

        raise RuntimeError(
            "Found Steam Controller / Steam Deck interfaces but none returned "
            "input reports. Is the controller paired/powered? "
            f"Last error: {last_err!r}"
        )

    def _lizard_reports(self, enabled, kind=None):
        """The feature report(s) that set lizard (firmware mouse/kb) mode for
        `kind` (defaults to the opened device's family). The Triton takes one
        SETTING_LIZARD_MODE write; the Deck and the 2015 controller take the
        kernel hid-steam.c sequences (clear/restore digital mappings + settings
        registers)  the 2015 firmware has fewer registers to write, so it gets
        its own pair (see SC2015_LIZARD_OFF_REPORTS)."""
        kind = kind or self.kind
        if kind == "steam_deck":
            return DECK_LIZARD_ON_REPORTS if enabled else DECK_LIZARD_OFF_REPORTS
        if kind == "sc2015":
            return (SC2015_LIZARD_ON_REPORTS if enabled
                    else SC2015_LIZARD_OFF_REPORTS)
        return (ENABLE_LIZARD_REPORT,) if enabled else (DISABLE_LIZARD_REPORT,)

    def _imu_reports(self, enabled, kind=None):
        """The feature report(s) that switch the IMU (gyro/accel) stream on or
        off for `kind`  one SETTING_IMU_MODE register write on all three
        families (SDL writes the same register number to each); only the report
        framing differs between the Triton and the two legacy families."""
        val = IMU_MODE_RAW if enabled else IMU_MODE_OFF
        if (kind or self.kind) in VALVE_LEGACY_KINDS:
            return (_valve_settings_report((SETTING_IMU_MODE, val)),)
        return ((IMU_ON_REPORT,) if enabled else (IMU_OFF_REPORT,))

    def set_imu(self, enabled):
        """Toggle the IMU (raw gyro + accel) stream at runtime  drives the
        gyro fields in the state report (gpitch/gyaw/groll). Safe before the
        device opens: the flag is remembered and the deck watchdog re-asserts
        it once reports are flowing. Sent immediately when the device is live
        (on the Linux Triton the runtime write may be ignored by the firmware
         see the hidapi libusb caveat on _lizard_watchdog)."""
        with self._dev_lock:
            self._imu_enabled = bool(enabled)
            if self._dev is None:
                return
            try:
                for report in self._imu_reports(self._imu_enabled):
                    self._dev.send_feature_report(report)
            except Exception:
                pass

    def _claim_remaining_interfaces(self, candidates, active_path):
        """block_sc_hid only: open and hold every other SC HID interface
        (dongle slots / wired controller) that _open_first_responsive didn't
        already try, purely so libusb's open detaches their kernel usbhid
        driver and removes their /dev/hidrawN too. Failures (e.g. Steam
        already has one claimed) are expected and silently skipped -- we
        can't dislodge a handle Steam opened first."""
        for cand in candidates:
            if cand['path'] == active_path:
                continue
            try:
                dev = self._open_device(cand['path'])
            except Exception:
                continue
            self._blocked_devs.append(dev)

    def _lizard_watchdog(self):
        """Re-assert whichever lizard state we currently want every
        LIZARD_REFRESH_SECONDS, so the controller's own watchdog doesn't
        revert it. _lizard_enabled is read under _dev_lock so set_lizard()
        can never lose a race with a watchdog tick.

        Linux note: skip the hidapi send entirely. The hidapi libusb
        backend duplicates the report ID byte in the wire payload, so
        what reaches the firmware is a malformed 65-byte feature report.
        At best the firmware silently ignores it; at worst, repeatedly
        spamming malformed feature reports puts the controller into a
        stuck state where trackpad mouse emulation stops responding
        (observed during long sessions). The libusb fallback can't claim
        iface 2 while hidapi holds it either, so the only effective
        Linux re-assert path is the cold-start libusb call in
        _open_first_responsive  we rely on the firmware's own lizard
        state holding between SC instance lifetimes."""
        if _IS_LINUX and self.kind != "steam_deck":
            # Nothing useful we can do at runtime on Linux for the Triton (see
            # above); just keep the thread alive so the existing teardown
            # semantics still apply. The DECK is different: its controller
            # interface is a plain built-in hidraw device whose feature
            # reports behave, and its lizard-off sequence already disables the
            # firmware's own Steam watchdog  the periodic re-assert below is
            # belt-and-braces against firmware quirks/reconnects.
            self._exit.wait()
            return
        while not self._exit.is_set():
            if self._exit.wait(LIZARD_REFRESH_SECONDS):
                return
            with self._dev_lock:
                if self._dev is None:
                    return
                try:
                    for report in self._lizard_reports(self._lizard_enabled):
                        self._dev.send_feature_report(report)
                    # The IMU stream rides the same tick while wanted  the
                    # deck's default-settings reload also clears register 48.
                    if self._imu_enabled:
                        for report in self._imu_reports(True):
                            self._dev.send_feature_report(report)
                except Exception:
                    pass

    def set_lizard(self, enabled):
        """Toggle lizard (firmware mouse/kb) mode at runtime. Works in both
        passive and non-passive modes  passive callers use this to briefly
        suppress firmware kb/mouse during chord injections (e.g. so the
        Steam+VIEW → Alt+Tab chord isn't fighting a firmware-emitted Tab
        from the same VIEW button). The hardware watchdog re-asserts lizard
        in 3-5s if we don't keep re-sending, so callers needing longer
        suppression must re-send periodically."""
        with self._dev_lock:
            self._lizard_enabled = bool(enabled)
            if self._dev is None:
                return
            try:
                for report in self._lizard_reports(self._lizard_enabled):
                    self._dev.send_feature_report(report)
            except Exception:
                pass

    def turn_off(self):
        """Ask the controller to power itself off (Steam Input's hold-Steam+Y
        behavior). Sends the ID_TURN_OFF_CONTROLLER feature report.

        Linux caveat (known not to work on this Triton/puck hardware):
        the 0x9F SET_REPORT delivered via libusb on iface 2 returns
        LIBUSB_ERROR_IO once hidapi has started streaming inputs  only
        the cold-start SET_REPORT used to disable lizard-mode at open
        time goes through. Other transports we tried all fail:
        hidapi-libusb sends a wrong-format 65-byte payload (duplicates
        the report ID), and hidraw HIDIOCSFEATURE STALLs (EPIPE) on
        iface 3-6 where the kernel binds usbhid  iface 2 has no hidraw
        node because libusb-via-hidapi claims it. The libusb close+
        detach+claim+release cycle that's needed to free iface 2 also
        causes a brief wireless-pair blip on the puck which Plasma
        surfaces as a connect/disconnect notification.

        Windows uses a kernel HID stack code path that libusb doesn't
        expose to userspace, which is why HidD_SetFeature works there.
        On this hardware on Linux, hold the Steam button for ~10 s to
        power off  that's a firmware feature independent of the host.

        No-op on a Steam Deck: its controller is the machine itself."""
        if self.kind == "steam_deck":
            return False
        with self._dev_lock:
            if self._dev is None:
                return False
            try:
                self._dev.send_feature_report(
                    SC2015_TURN_OFF_REPORT if self.kind == "sc2015"
                    else TURN_OFF_REPORT)
                print("steamcontroller: sent turn-off command "
                      "(firmware likely ignores on Triton; hold Steam ~10s)")
                return True
            except Exception as e:
                print(f"steamcontroller: turn_off failed: {e}")
                return False

    def _tone_unlocked(self, actuator, freq_hz, gain, count):
        """Write one tone to `actuator` (caller holds _dev_lock). Triton: the
        0x83 LFO-tone OUTPUT report. Deck: the 0xEA MsgTriggerHaptic feature
        report (cmd=tone) on the matching pad LRA, with the burst length
        converted from cycles to the deck's bounded dur_ms. 2015 SC: the 0x8F
        pulse train, which IS a cycle count already  every tone it plays is
        inherently self-terminating, so a runaway buzz isn't possible there."""
        if self.kind == "sc2015":
            self._dev.send_feature_report(_build_sc2015_haptic_report(
                _SC2015_HAPTIC_PAD.get(actuator, 0), freq_hz, gain,
                # 0x7FFF means "until stopped" to the other two families; the
                # 2015 pads have no such mode, so cap a sustained tone at the
                # longest burst the count field can express.
                0xFFFF if count >= 0x7FFF else count))
            return
        if self.kind == "steam_deck":
            side = _DECK_ACTUATOR_SIDE.get(actuator, DECK_HAPTIC_SIDE_BOTH)
            if count >= 0x7FFF:
                dur_ms = -1                       # negative = until stopped
            else:
                dur_ms = max(4, int(count * 1000.0 / max(1, int(freq_hz))))
            self._dev.send_feature_report(_build_deck_haptic_report(
                side, DECK_HAPTIC_CMD_TONE, gain=gain, freq_hz=int(freq_hz),
                dur_ms=dur_ms))
        else:
            self._dev.write(_build_haptic_tone_report(
                actuator, freq_hz, gain, count))

    def _stop_unlocked(self, actuator):
        """Stop `actuator` (caller holds _dev_lock)  0x82 on the Triton,
        0xEA cmd=off on the Deck. The 2015 pads have no stop command at all
        (0x8F queues a finite pulse train), so the shortest possible pulse
        stands in: it supersedes whatever is playing and ends immediately."""
        if self.kind == "sc2015":
            self._dev.send_feature_report(_build_sc2015_haptic_report(
                _SC2015_HAPTIC_PAD.get(actuator, 0), 1000,
                _SC2015_SRC_GAIN_MIN, 1))
            return
        if self.kind == "steam_deck":
            side = _DECK_ACTUATOR_SIDE.get(actuator, DECK_HAPTIC_SIDE_BOTH)
            self._dev.send_feature_report(_build_deck_haptic_report(
                side, DECK_HAPTIC_CMD_OFF))
        else:
            self._dev.write(_build_haptic_stop_report(actuator))

    def haptic_tone(self, actuator, freq_hz, gain=HAPTIC_DEFAULT_GAIN, count=0x7FFF):
        """Play an LFO tone on one actuator. Default `count` plays until
        stopped; a small `count` plays a short burst (a click)."""
        with self._dev_lock:
            if self._dev is None:
                return False
            try:
                self._tone_unlocked(actuator, freq_hz, gain, count)
                return True
            except Exception as e:
                print(f"steamcontroller: haptic_tone failed: {e}")
                return False

    def haptic_stop(self, actuator):
        """Stop the tone on one actuator."""
        with self._dev_lock:
            if self._dev is None:
                return False
            try:
                self._stop_unlocked(actuator)
                return True
            except Exception as e:
                print(f"steamcontroller: haptic_stop failed: {e}")
                return False

    def haptic_pad_click(self):
        """'Physical pad click' tick for the simulated trackpad click (press
        AND release) and the L2/R2 selects. A short, crisp, slightly firmer pop
        than the light key tap  high-frequency and brief so it feels like a
        real button click rather than a deep buzz."""
        self.haptic_click(freq_hz=HAPTIC_PAD_CLICK_FREQ, gain=HAPTIC_PAD_CLICK_GAIN,
                          count=4, duration=0.014)

    def haptic_click(self, freq_hz=550, gain=HAPTIC_CLICK_GAIN, count=5, duration=0.018):
        """Crisp trackpad 'click' for UI feedback: play a very short burst
        (`count` cycles) on both trackpad actuators so it snaps rather than
        buzzes. A higher frequency gives a faster attack (snappier onset) and
        the short safety-stop keeps the tail tight, so rapid press/release
        ticks read as distinct clicks instead of smearing into a buzz. Both
        pad writes go out under a single lock for minimal onset latency; the
        timed stop after `duration` is a safety net in case the hardware
        ignores the burst count and plays continuously."""
        pads = (HAPTIC_PAD_LEFT, HAPTIC_PAD_RIGHT)
        with self._dev_lock:
            if self._dev is None:
                return
            try:
                for act in pads:
                    self._tone_unlocked(act, freq_hz, gain, count)
            except Exception as e:
                print(f"steamcontroller: haptic_click failed: {e}")
                return
        if self.kind in VALVE_LEGACY_KINDS:
            # Deck tones carry their own bounded dur_ms and 2015 pulse trains
            # their own cycle count  no timed stop needed (and skipping it
            # avoids cutting a click short with an off cmd).
            return

        def _stop():
            with self._dev_lock:
                if self._dev is None:
                    return
                for act in pads:
                    try:
                        self._stop_unlocked(act)
                    except Exception:
                        pass

        threading.Timer(duration, _stop).start()

    def play_chime(self, on=True):
        """Play a short rising (on) / falling (off) arpeggio on both trackpads
        to confirm a mode change, echoing the controller's power on/off jingle.
        Tones go to the pad actuators (0/1), not the motors, so it's audible
        with no damage risk. Blocks for the chime's duration (~0.35s)  call
        from a worker thread if you don't want to wait, and call it BEFORE the
        device is torn down or the trailing stops will cut the chime short.

        Voicing (chosen by ear): the melody plays on the right pad with the
        left pad a few Hz higher (CHIME_DETUNE_HZ) so they beat together for a
        fuller chorus, over a steady soft low-D pedal on a rumble motor for
        body. A trailing stop silences all three actuators."""
        notes = CHIME_NOTES if on else tuple(reversed(CHIME_NOTES))
        acts = (HAPTIC_PAD_RIGHT, HAPTIC_PAD_LEFT, CHIME_BODY_ACTUATOR)
        # The Deck and the 2015 controller have only the two pad LRAs  the
        # low-D body pedal would land on the left pad and replace its melody
        # note, so skip it there.
        if self.kind in VALVE_LEGACY_KINDS:
            acts = (HAPTIC_PAD_RIGHT, HAPTIC_PAD_LEFT)
        for freq, dur in zip(notes, CHIME_DURATIONS):
            # (actuator, frequency, gain) for this note: detuned pad pair + body
            voicing = (
                (HAPTIC_PAD_RIGHT, freq, HAPTIC_CHIME_GAIN),
                (HAPTIC_PAD_LEFT, freq + CHIME_DETUNE_HZ, HAPTIC_CHIME_GAIN),
                (CHIME_BODY_ACTUATOR, CHIME_BODY_FREQ, CHIME_BODY_GAIN),
            )
            if self.kind in VALVE_LEGACY_KINDS:
                voicing = voicing[:2]
            with self._dev_lock:
                if self._dev is None:
                    return
                try:
                    for act, f, gain in voicing:
                        # Stop before each tone for a clean onset (also required
                        # on the motor  omitting it there can reboot the unit).
                        # Bounded count so a crash mid-chime self-terminates.
                        self._stop_unlocked(act)
                        self._tone_unlocked(act, f, gain,
                                            _tone_count(f, CHIME_TONE_SECONDS))
                except Exception as e:
                    print(f"steamcontroller: play_chime failed: {e}")
                    return
            time.sleep(dur)
        with self._dev_lock:
            if self._dev is None:
                return
            for act in acts:
                try:
                    self._stop_unlocked(act)
                except Exception:
                    pass

    def _emit_rumble(self, large, small):
        """Write the two back-motor tones for the given intensities as SHORT,
        self-expiring bursts (RUMBLE_TONE_SECONDS). A stop precedes each tone 
        per SteamHapticsSinger this avoids the controller rebooting when
        re-driving the motors. Re-validates against the latest requested state
        under the lock so a concurrent set_rumble(0,0) can't be clobbered by a
        stale keepalive re-arm (which would briefly turn a just-stopped motor
        back on). Returns True if written."""
        with self._dev_lock:
            if self._dev is None:
                return False
            if (large, small) != self._rumble_state:
                # A newer request landed; let its own emit win.
                return False
            try:
                if self.kind == "steam_deck":
                    # Real rumble motors via the deck's simple-rumble command
                    # (SDL's RumbleJoystick path). Zero speeds stop the motors;
                    # the keepalive re-sends while non-zero, and close() zeros.
                    self._dev.send_feature_report(
                        _build_deck_rumble_report(large, small))
                    return True
                if self.kind == "sc2015":
                    # No motors on this hardware: game FFB plays as pulse
                    # trains on the two pad LRAs, which is what Steam Input
                    # itself does. Each burst is finite (the 0x8F count field),
                    # and the keepalive re-arms it while the game wants rumble.
                    for act, intensity, freq in (
                        (HAPTIC_PAD_LEFT, large, RUMBLE_FREQ_LOW),
                        (HAPTIC_PAD_RIGHT, small, RUMBLE_FREQ_HIGH),
                    ):
                        if intensity and intensity > 0:
                            self._tone_unlocked(
                                act, freq, _rumble_gain(intensity),
                                _tone_count(freq, RUMBLE_TONE_SECONDS))
                        else:
                            self._stop_unlocked(act)
                    return True
                for act, intensity, freq in (
                    (HAPTIC_RUMBLE_LEFT, large, RUMBLE_FREQ_LOW),
                    (HAPTIC_RUMBLE_RIGHT, small, RUMBLE_FREQ_HIGH),
                ):
                    self._dev.write(_build_haptic_stop_report(act))
                    if intensity and intensity > 0:
                        self._dev.write(_build_haptic_tone_report(
                            act, freq, _rumble_gain(intensity),
                            _tone_count(freq, RUMBLE_TONE_SECONDS)))
                return True
            except Exception as e:
                print(f"steamcontroller: set_rumble failed: {e}")
                return False

    def set_rumble(self, large, small):
        """Drive the two back rumble motors from XInput large/small motor
        intensities (0..255); 0 stops a motor. The tones are SELF-TERMINATING
        bursts re-armed by a keepalive thread (see RUMBLE_TONE_SECONDS), so if
        this process dies before the usual set_rumble(0,0) stop is sent, the
        motors fall silent on their own within ~RUMBLE_TONE_SECONDS rather than
        buzzing indefinitely. Returns True if the immediate write succeeded."""
        large = max(0, min(255, int(large)))
        small = max(0, min(255, int(small)))
        self._rumble_state = (large, small)
        ok = self._emit_rumble(large, small)
        if large or small:
            self._ensure_rumble_keepalive()
        return ok

    def _ensure_rumble_keepalive(self):
        """Start the rumble keepalive thread if it isn't already running."""
        t = self._rumble_thread
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self._rumble_keepalive, daemon=True)
        self._rumble_thread = t
        t.start()

    def _rumble_keepalive(self):
        """Re-arm the bounded rumble bursts every RUMBLE_REFRESH_SECONDS while a
        motor should be on, so sustained game rumble feels continuous. Exits the
        moment the rumble is zero or the device goes away  and then the last
        burst lapses by itself, which is the whole point: a dropped stop (crash,
        hard kill) can never leave a motor stuck on."""
        while not self._exit.is_set():
            large, small = self._rumble_state
            if not (large or small):
                return
            with self._dev_lock:
                gone = self._dev is None
            if gone:
                return
            self._emit_rumble(large, small)
            self._exit.wait(RUMBLE_REFRESH_SECONDS)

    def get_battery(self):
        """Most recent SteamControllerBattery seen on the wire, or None if the
        controller hasn't streamed one yet this session OR has gone silent
        (powered off via Steam+Y / dropped its wireless link)  detected as no
        input/battery frame for BATTERY_FRESH_SECONDS, so the tray doesn't keep
        showing a stale % while the dongle stays plugged in."""
        b = self._battery
        if b is None:
            return None
        if time.monotonic() - self._last_frame_t > BATTERY_FRESH_SECONDS:
            return None
        return b

    def battery_frame_id(self):
        """Monotonic counter of accepted 0x43 battery frames, so callers can
        tell a genuinely fresh reading from the same one re-read (the report
        streams at only ~0.4 Hz). Used to confirm a charge-state change across
        two DISTINCT frames instead of waiting a fixed wall-clock window."""
        return self._battery_seq

    def is_live(self):
        """True once the device is open and usable (run() has opened it and it
        hasn't been closed). `opened` alone isn't enough  it stays True after
        close  so we also require a live handle."""
        with self._dev_lock:
            return self.opened and self._dev is not None

    def addExit(self):
        self._exit.set()

    def run(self):
        try:
            self._open_first_responsive()
        except Exception as e:
            print(f"steamcontroller: open failed: {e}")
            return
        self.opened = True

        if not self._passive:
            self._lizard_thread = threading.Thread(
                target=self._lizard_watchdog, daemon=True
            )
            self._lizard_thread.start()

        try:
            while not self._exit.is_set():
                with self._dev_lock:
                    dev = self._dev
                if dev is None:
                    break
                try:
                    data = dev.read(64, 200)
                except Exception as e:
                    print(f"steamcontroller: read error: {e}")
                    break
                if not data:
                    continue
                if self.kind == "steam_deck":
                    # The Deck streams only 64-byte state frames on this
                    # interface (its battery is the machine's own  no 0x43s).
                    sci = _parse_deck(bytes(data))
                    if sci is None:
                        continue
                    self._last_frame_t = time.monotonic()
                    try:
                        self._cb(self, sci, *self._cb_args)
                    except Exception as e:
                        print(f"steamcontroller: callback raised: {e}")
                    continue
                if self.kind == "sc2015":
                    # State frames interleave with the periodic status report
                    # (battery) and, on the wireless receiver, link events.
                    # Everything else falls through and is ignored.
                    raw = bytes(data)
                    sci = _parse_sc2015(raw)
                    if sci is None:
                        batt = _parse_sc2015_battery(raw)
                        if batt is not None:
                            self._last_frame_t = time.monotonic()
                            self._battery = batt
                            self._battery_seq += 1
                        continue
                    self._last_frame_t = time.monotonic()
                    try:
                        self._cb(self, sci, *self._cb_args)
                    except Exception as e:
                        print(f"steamcontroller: callback raised: {e}")
                    continue
                # Power-status and link-status reports stream interleaved with
                # the game-input reports. Pull battery out here (cheap: one byte
                # compare on the read thread, off the watcher hot path) and drop
                # the link-status frames so they aren't mis-parsed as input.
                head = data[0]
                if head == TRITON_BATTERY_REPORT_ID:
                    self._last_frame_t = time.monotonic()
                    batt = _parse_battery(bytes(data))
                    if batt is not None:
                        self._battery = batt
                        self._battery_seq += 1
                    continue
                if head in TRITON_WIRELESS_STATUS_IDS:
                    continue
                sci = _parse_triton(bytes(data))
                if sci is None:
                    continue
                # A real input frame  the controller is alive and streaming.
                self._last_frame_t = time.monotonic()
                try:
                    self._cb(self, sci, *self._cb_args)
                except Exception as e:
                    print(f"steamcontroller: callback raised: {e}")
        finally:
            self._exit.set()
            with self._dev_lock:
                try:
                    if self._dev is not None:
                        # Stop any haptics still playing so the controller
                        # doesn't keep buzzing after we release the device
                        # (e.g. a haptic_click whose timed stop hasn't fired).
                        if self.kind == "steam_deck":
                            for report in (
                                    _build_deck_rumble_report(0, 0),
                                    _build_deck_haptic_report(
                                        DECK_HAPTIC_SIDE_BOTH,
                                        DECK_HAPTIC_CMD_OFF)):
                                try:
                                    self._dev.send_feature_report(report)
                                except Exception:
                                    pass
                        elif self.kind == "sc2015":
                            for act in (HAPTIC_PAD_LEFT, HAPTIC_PAD_RIGHT):
                                try:
                                    self._stop_unlocked(act)
                                except Exception:
                                    pass
                        else:
                            for act in (HAPTIC_PAD_LEFT, HAPTIC_PAD_RIGHT,
                                        HAPTIC_RUMBLE_LEFT, HAPTIC_RUMBLE_RIGHT):
                                try:
                                    self._dev.write(_build_haptic_stop_report(act))
                                except Exception:
                                    pass
                        # Stop the IMU stream if we turned it on (battery
                        # hygiene  nothing reads it once we release the device).
                        if self._imu_enabled:
                            try:
                                for report in self._imu_reports(False):
                                    self._dev.send_feature_report(report)
                            except Exception:
                                pass
                        # Restore lizard mode immediately so the controller
                        # works as a normal mouse/keyboard right away instead
                        # of waiting for the hardware watchdog (~3-5 sec; the
                        # Deck's watchdog is disabled outright while we own it).
                        if not self._passive:
                            try:
                                for report in self._lizard_reports(True):
                                    self._dev.send_feature_report(report)
                            except Exception:
                                pass
                        self._dev.close()
                except Exception:
                    pass
                self._dev = None
                for d in self._blocked_devs:
                    try:
                        d.close()
                    except Exception:
                        pass
                self._blocked_devs = []
