"""Big Picture automation engine (Options → Big Picture).

Watches for Steam's Big Picture window and, while it is open, applies a set of
user-chosen system changes  switching the active display and audio output,
enabling HDR, disabling Night Light, pausing media and hiding the mouse cursor
 then reverts every one of them when Big Picture closes. It can also OPEN Big
Picture when a controller connects and close it again when the last one
disconnects (skipped while a game is running).

Implementation notes. Three MIT-licensed projects contributed code here 
BigPictureManager (magrega), Auto-Big-Picture (Goatvisuals) and Big Picture
Portal; see THIRD_PARTY_NOTICES.md. Everything else is written directly
against the documented Win32 / WinRT APIs.

* Detection: the Big Picture window is found by a "big picture" title match
  plus a steam*-process ownership guard (BigPictureManager's rule, so an
  unrelated window with that name can never trigger), backed up by Steam's own
  BigPictureInForeground registry flag for localizations that translate the
  name outright. The matching window HANDLE is then latched, so the session
  survives alt-tabbing and a Steam retitle. No table of localized titles is
  used  see is_big_picture_running. Polled at 1 Hz on a worker thread that
  blocks entirely while no feature is enabled.
* Display: DisplayConfig topology save/switch/restore (QueryDisplayConfig /
  SetDisplayConfig  the same API this codebase already drives in tray.py's
  display-scaling helpers) rather than DisplaySwitch.exe, so the EXACT
  multi-monitor topology comes back on exit, on any monitor count. The switch
  supplies a single active path with its mode indexes invalidated and no mode
  array, letting Windows derive the target's best mode itself; the saved
  topology is persisted (settings["bp_recovery"]) so a crash mid-session is
  repaired on the next launch.
* Audio: the tray's IPolicyConfig default-endpoint switch, retried against a
  short deadline because an HDMI audio endpoint only finishes enumerating a
  moment after the display switch wakes the TV. The device list deliberately
  includes attached-but-inactive endpoints, so an HDMI TV that is currently
  off can still be pre-selected.
* Night Light: BigPictureManager's byte-level CloudStore codec  understands
  manual vs schedule state shapes on both registry key variants and restores
  the exact prior mode (not a blind toggle).
* HDR: DisplayConfig advanced-color API  the Windows 11 24H2 typed calls
  (GET_ADVANCED_COLOR_INFO_2 / SET_HDR_STATE) with the legacy
  GET_ADVANCED_COLOR_INFO / SET_ADVANCED_COLOR_STATE fallback for
  Windows 10 / older 11 builds.
* Media pause: SMTC (GlobalSystemMediaTransportControlsSessionManager) pauses
  only sessions that are actually PLAYING (BigPictureManager's approach);
  falls back to a VK_MEDIA_STOP keystroke when the WinRT projection is
  unavailable (stop being idempotent, unlike a play/pause toggle).
* Cursor hiding: blank system cursors + SPI_SETCURSORS restore, with a
  move-to-reveal poll (Big Picture Portal's cursor manager).
* Auto open/close: controller connect/disconnect edges (Auto-Big-Picture,
  ported from /dev/input polling to the tray's live controller state), with
  the steam://open/bigpicture and steam://close/bigpicture URLs and a
  RunningAppID game-in-progress guard.

LICENCE NOTE for contributors: SteamlessInput is GPL-3.0. Port code into this
module only from GPL-3.0-compatible sources (MIT / BSD / Apache-2.0 / LGPL any
version / GPL-2.0-or-later / GPL-3.0)  proprietary code cannot be carried
here, and AGPL-3.0 code drags its network-use obligation along with it.
"""

import base64
import ctypes
import os
import threading
import time
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

# --- Steam helpers -----------------------------------------------------------

STEAM_KEY = r"Software\Valve\Steam"

def big_picture_foreground_flag():
    """Steam's own `BigPictureInForeground` value: True/False while Steam is
    running, None when the value is absent (Steam closed, or a build that
    doesn't publish it). This is FOREGROUND state, not existence  Steam
    clears it when you alt-tab away  so it can only ever be a positive
    signal, never proof that Big Picture closed."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STEAM_KEY) as k:
            return int(winreg.QueryValueEx(k, "BigPictureInForeground")[0]) != 0
    except (OSError, ValueError):
        return None


def steam_game_running():
    """True while Steam reports a game in progress (RunningAppID != 0)  the
    Auto-Big-Picture "don't close Big Picture mid-game" guard, via Steam's own
    registry value instead of a process scan."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STEAM_KEY) as k:
            return int(winreg.QueryValueEx(k, "RunningAppID")[0]) != 0
    except OSError:
        return False


def open_big_picture():
    """Ask Steam to open Big Picture (starts Steam first when needed)."""
    try:
        os.startfile("steam://open/bigpicture")
        return True
    except OSError as e:
        print(f"big_picture: open failed: {e!r}")
        return False


def close_big_picture():
    """Ask a RUNNING Steam to leave Big Picture."""
    try:
        os.startfile("steam://close/bigpicture")
        return True
    except OSError as e:
        print(f"big_picture: close failed: {e!r}")
        return False

def toggle_big_picture():
    """Open Big Picture, or leave it if it is already up  the bindable
    "Toggle Big Picture" action. Reads the real window state
    (is_big_picture_running) rather than tracking what we last asked for, so
    the button stays in step with Big Picture opened or closed by any other
    means."""
    if is_big_picture_running():
        return close_big_picture()
    return open_big_picture()


# --- Big Picture window detection -------------------------------------------

_ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if IS_WINDOWS else None


def _visible_windows():
    """[(hwnd, title)] for every visible top-level window with a title.
    Minimized windows are INCLUDED  minimizing Big Picture must not read as
    "closed" and yank the display/audio out from under it."""
    out = []

    def _cb(hwnd, _l):
        if _user32.IsWindowVisible(hwnd):
            n = _user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value:
                    out.append((hwnd, buf.value))
        return True

    _user32.EnumWindows(_ENUM_PROC(_cb), 0)
    return out


def _window_pid(hwnd):
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _is_steam_pid(pid, cache):
    """True when `pid` is a steam* process (steam.exe / steamwebhelper.exe 
    prefix-matched so a Steam window-ownership change survives updates)."""
    if pid in cache:
        return cache[pid]
    ok = False
    try:
        import psutil
        ok = psutil.Process(pid).name().lower().startswith("steam")
    except Exception:
        ok = False
    cache[pid] = ok
    return ok


def _norm_title(s):
    """Lowercased title with non-breaking spaces folded to real ones (Steam
    pads some localized titles with U+00A0) and whitespace runs collapsed."""
    return " ".join(s.replace(" ", " ").lower().split())


# Set to the Big Picture window handle once identified, so the session stays
# "open" while that window lives even if the cheap title test stops matching
# (see is_big_picture_running). Cleared when the window is gone.
_bp_window = None


def is_big_picture_running():
    """True while Steam's Big Picture window exists.

    Two independent signals, both requiring the window to belong to a `steam*`
    process so an unrelated window named "Big Picture" can never trigger:

    1. The window title contains "big picture". Steam keeps those two ASCII
       words in most of its localizations (German "Big-Picture-Modus", French
       "mode Big Picture", Russian "Режим Big Picture", Japanese
       "Big Pictureモード", ...), so this covers the large majority of
       languages on its own.
    2. Steam's own `BigPictureInForeground` registry flag, as a best-effort
       extra. NOTE: current Steam builds do not appear to publish this value
       at all (verified 2026-07-28  the value was absent both in and out of
       Big Picture), so treat signal 1 as the one that actually fires. It is
       kept because it costs a single registry read and older/other builds do
       set it.

    Either way the matching window HANDLE is latched: once identified, the
    session stays open until that window is destroyed, so alt-tabbing out of
    Big Picture never reads as "closed" (which would revert the display
    mid-session). Verified against live Steam: opens detect in ~2 s, the latch
    holds while Big Picture is backgrounded, and a close detects in ~2 s.

    KNOWN GAP: a few localizations translate the name outright rather than
    keeping the ASCII words (Simplified Chinese, Bulgarian, Finnish,
    Hungarian). With signal 2 absent on current builds, Big Picture will not
    be detected in those languages. The window class is NOT usable to close
    this gap  Big Picture and the ordinary Steam client window are both
    `SDL_app` owned by steamwebhelper.exe (also verified). A hardcoded table
    of localized titles is deliberately avoided: it breaks the moment Valve
    retranslates or renames the mode, exactly the churn the handle latch is
    here to survive. If the gap ever matters, prefer a live signal (Steam's
    own registry/IPC state) over hardcoded strings.
    """
    global _bp_window
    if not IS_WINDOWS:
        return False
    if _bp_window is not None:
        if _user32.IsWindow(_bp_window):
            return True
        _bp_window = None
    pid_cache = {}
    steam_hwnds = []
    for hwnd, title in _visible_windows():
        if not _is_steam_pid(_window_pid(hwnd), pid_cache):
            continue
        steam_hwnds.append(hwnd)
        if "big picture" in _norm_title(title):
            _bp_window = hwnd
            return True
    if steam_hwnds and big_picture_foreground_flag():
        fg = _user32.GetForegroundWindow()
        _bp_window = fg if fg in steam_hwnds else steam_hwnds[0]
        return True
    return False


# --- DisplayConfig: enumerate / switch / save / restore ----------------------

_QDC_ALL_PATHS = 0x1
_QDC_ONLY_ACTIVE_PATHS = 0x2
_PATH_ACTIVE = 0x1
_MODE_IDX_INVALID = 0xFFFFFFFF
_SDC_APPLY_SUPPLIED = 0x80 | 0x20 | 0x400 | 0x200   # APPLY | USE_SUPPLIED |
#                                                     ALLOW_CHANGES | SAVE_TO_DATABASE
_GET_TARGET_NAME = 2


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _DC_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int32), ("size", ctypes.c_uint32),
                ("adapterId", _LUID), ("id", ctypes.c_uint32)]


class _DC_PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class _DC_PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshNumerator", ctypes.c_uint32),
                ("refreshDenominator", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", ctypes.c_int32),
                ("statusFlags", ctypes.c_uint32)]


class _DC_PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _DC_PATH_SOURCE),
                ("targetInfo", _DC_PATH_TARGET), ("flags", ctypes.c_uint32)]


class _DC_MODE_INFO(ctypes.Structure):
    """DISPLAYCONFIG_MODE_INFO kept opaque (64 raw bytes)  modes are only
    ever copied whole between QueryDisplayConfig and SetDisplayConfig."""
    _fields_ = [("blob", ctypes.c_ubyte * 64)]


class _DC_TARGET_NAME(ctypes.Structure):
    _fields_ = [("header", _DC_HEADER), ("flags", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16),
                ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32),
                ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
                ("monitorDevicePath", ctypes.c_wchar * 128)]


def _query_paths(flags):
    """(paths_array, modes_array, n_path, n_mode) or None on failure."""
    if not IS_WINDOWS:
        return None
    n_path, n_mode = ctypes.c_uint32(), ctypes.c_uint32()
    if _user32.GetDisplayConfigBufferSizes(
            flags, ctypes.byref(n_path), ctypes.byref(n_mode)) != 0:
        return None
    paths = (_DC_PATH_INFO * max(1, n_path.value))()
    modes = (_DC_MODE_INFO * max(1, n_mode.value))()
    if _user32.QueryDisplayConfig(flags, ctypes.byref(n_path), paths,
                                  ctypes.byref(n_mode), modes, None) != 0:
        return None
    return paths, modes, n_path.value, n_mode.value


def _target_name(path):
    """DISPLAYCONFIG_TARGET_DEVICE_NAME for a path's target, or None."""
    tn = _DC_TARGET_NAME()
    tn.header.type = _GET_TARGET_NAME
    tn.header.size = ctypes.sizeof(_DC_TARGET_NAME)
    tn.header.adapterId = path.targetInfo.adapterId
    tn.header.id = path.targetInfo.id
    if _user32.DisplayConfigGetDeviceInfo(ctypes.byref(tn)) != 0:
        return None
    return tn


def _paths_by_device():
    """{monitorDevicePath: [path, ...]} over ALL paths (a monitor usually
    appears on several candidate paths  one per source it could be driven
    from), plus the parallel {device_path: friendly_name} map. The picker and
    the settings key displays by this device path because it is the one
    identifier that survives reboots and re-plugs."""
    out, names = {}, {}
    q = _query_paths(_QDC_ALL_PATHS)
    if q is not None:
        paths, _modes, n_path, _n_mode = q
        for p in paths[:n_path]:
            tn = _target_name(p)
            if tn is None or not tn.monitorDevicePath:
                continue
            out.setdefault(tn.monitorDevicePath, []).append(p)
            names.setdefault(tn.monitorDevicePath,
                             tn.monitorFriendlyDeviceName or "Generic Monitor")
    return out, names


def list_displays():
    """[(device_path, friendly_name, is_active)] for every attached monitor 
    a monitor counts as active when ANY of its candidate paths is; actives
    sort first, then alphabetical."""
    by_dev, names = _paths_by_device()
    out = []
    for dp, plist in by_dev.items():
        active = any(p.flags & _PATH_ACTIVE for p in plist)
        out.append((dp, names[dp], active))
    out.sort(key=lambda t: (not t[2], t[1].lower()))
    return out


def save_display_topology():
    """The active topology as a JSON-safe dict (raw path/mode bytes, base64)
    for the in-memory snapshot AND the persisted crash-recovery record."""
    q = _query_paths(_QDC_ONLY_ACTIVE_PATHS)
    if q is None:
        return None
    paths, modes, n_path, n_mode = q
    if n_path == 0:
        return None
    pb = ctypes.string_at(ctypes.byref(paths),
                          ctypes.sizeof(_DC_PATH_INFO) * n_path)
    mb = ctypes.string_at(ctypes.byref(modes),
                          ctypes.sizeof(_DC_MODE_INFO) * n_mode)
    return {"n_path": n_path, "n_mode": n_mode,
            "paths": base64.b64encode(pb).decode("ascii"),
            "modes": base64.b64encode(mb).decode("ascii")}


def restore_display_topology(snap):
    """SetDisplayConfig the exact saved topology back. True on success."""
    if not IS_WINDOWS or not snap:
        return False
    try:
        n_path, n_mode = int(snap["n_path"]), int(snap["n_mode"])
        pb = base64.b64decode(snap["paths"])
        mb = base64.b64decode(snap["modes"])
        if (len(pb) != ctypes.sizeof(_DC_PATH_INFO) * n_path
                or len(mb) != ctypes.sizeof(_DC_MODE_INFO) * n_mode):
            return False
        paths = (_DC_PATH_INFO * n_path).from_buffer_copy(pb)
        modes = (_DC_MODE_INFO * max(1, n_mode)).from_buffer_copy(
            mb + b"\x00" * (ctypes.sizeof(_DC_MODE_INFO) * max(1, n_mode)
                            - len(mb)))
        res = _user32.SetDisplayConfig(n_path, paths, n_mode, modes,
                                       _SDC_APPLY_SUPPLIED)
        if res != 0:
            print(f"big_picture: restore topology failed ({res})")
        return res == 0
    except Exception as e:
        print(f"big_picture: restore topology error: {e!r}")
        return False


def set_only_display(device_path):
    """Make `device_path` the ONLY active display.

    No mode array is supplied at all: the chosen path's mode indexes are
    invalidated (DISPLAYCONFIG_PATH_MODE_IDX_INVALID) and SetDisplayConfig is
    handed zero modes, which per its contract makes Windows derive the best
    source/target mode for the topology itself (SDC_USE_SUPPLIED_DISPLAY_CONFIG
    + SDC_ALLOW_CHANGES). That both sidesteps hand-copying mode structs and
    means a TV that renegotiates its preferred mode on wake still comes up
    right. Among the monitor's candidate paths, a currently-active one is
    preferred (no source re-route when we're already driving it); otherwise
    the first path whose target reports available."""
    by_dev, _names = _paths_by_device()
    plist = by_dev.get(device_path) or []
    chosen = next((p for p in plist if p.flags & _PATH_ACTIVE),
                  next((p for p in plist if p.targetInfo.targetAvailable),
                       plist[0] if plist else None))
    if chosen is None:
        print(f"big_picture: display not found: {device_path}")
        return False
    path = _DC_PATH_INFO.from_buffer_copy(chosen)
    path.flags = _PATH_ACTIVE
    path.sourceInfo.modeInfoIdx = _MODE_IDX_INVALID
    path.sourceInfo.statusFlags = 0
    path.targetInfo.modeInfoIdx = _MODE_IDX_INVALID
    path.targetInfo.statusFlags = 0
    res = _user32.SetDisplayConfig(1, ctypes.byref(path), 0, None,
                                   _SDC_APPLY_SUPPLIED)
    if res != 0:
        print(f"big_picture: display switch failed ({res})")
    return res == 0


# --- HDR (DisplayConfig advanced color) --------------------------------------

# Windows 11 24H2 added typed HDR calls; older builds use the advanced-color
# state (which also flips wide-color-only displays  the 24H2 API fixes that,
# hence the dual path).
_GET_ADV_COLOR = 9         # DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
_SET_ADV_COLOR = 10        # ..._SET_ADVANCED_COLOR_STATE
_GET_ADV_COLOR_2 = 15      # ..._GET_ADVANCED_COLOR_INFO_2      (24H2+)
_SET_HDR_STATE = 16        # ..._SET_HDR_STATE                  (24H2+)
_WIN_24H2_BUILD = 26100


class _DC_GET_ADV(ctypes.Structure):
    _fields_ = [("header", _DC_HEADER), ("value", ctypes.c_uint32),
                ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32)]


class _DC_GET_ADV2(ctypes.Structure):
    _fields_ = [("header", _DC_HEADER), ("value", ctypes.c_uint32),
                ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32),
                ("activeColorMode", ctypes.c_uint32)]


class _DC_SET_U32(ctypes.Structure):
    _fields_ = [("header", _DC_HEADER), ("value", ctypes.c_uint32)]


def _win_build():
    try:
        import sys
        return sys.getwindowsversion().build
    except Exception:
        return 0


def _hdr_query(path):
    """(supported, enabled) for one path's target, or None. Prefers the 24H2
    API (true HDR bits), falls back to legacy advanced-color."""
    if _win_build() >= _WIN_24H2_BUILD:
        pkt = _DC_GET_ADV2()
        pkt.header.type = _GET_ADV_COLOR_2
        pkt.header.size = ctypes.sizeof(_DC_GET_ADV2)
        pkt.header.adapterId = path.targetInfo.adapterId
        pkt.header.id = path.targetInfo.id
        if _user32.DisplayConfigGetDeviceInfo(ctypes.byref(pkt)) == 0:
            v = pkt.value
            # bit4 highDynamicRangeSupported, bit5 highDynamicRangeUserEnabled
            return bool(v & 0x10), bool(v & 0x20)
    pkt = _DC_GET_ADV()
    pkt.header.type = _GET_ADV_COLOR
    pkt.header.size = ctypes.sizeof(_DC_GET_ADV)
    pkt.header.adapterId = path.targetInfo.adapterId
    pkt.header.id = path.targetInfo.id
    if _user32.DisplayConfigGetDeviceInfo(ctypes.byref(pkt)) != 0:
        return None
    v = pkt.value
    # bit0 advancedColorSupported, bit1 advancedColorEnabled,
    # bit3 advancedColorForceDisabled
    if v & 0x8:
        return False, False
    return bool(v & 0x1), bool(v & 0x2)


def _hdr_set(path, enable):
    """Enable/disable HDR on one path's target. 24H2 typed call first, legacy
    advanced-color fallback. True on success."""
    if _win_build() >= _WIN_24H2_BUILD:
        pkt = _DC_SET_U32()
        pkt.header.type = _SET_HDR_STATE
        pkt.header.size = ctypes.sizeof(_DC_SET_U32)
        pkt.header.adapterId = path.targetInfo.adapterId
        pkt.header.id = path.targetInfo.id
        pkt.value = 1 if enable else 0
        if _user32.DisplayConfigSetDeviceInfo(ctypes.byref(pkt)) == 0:
            return True
    pkt = _DC_SET_U32()
    pkt.header.type = _SET_ADV_COLOR
    pkt.header.size = ctypes.sizeof(_DC_SET_U32)
    pkt.header.adapterId = path.targetInfo.adapterId
    pkt.header.id = path.targetInfo.id
    pkt.value = 1 if enable else 0
    return _user32.DisplayConfigSetDeviceInfo(ctypes.byref(pkt)) == 0


def _hdr_active_paths():
    q = _query_paths(_QDC_ONLY_ACTIVE_PATHS)
    if q is None:
        return []
    paths, _m, n_path, _n = q
    return list(paths[:n_path])


def hdr_supported():
    """True when ANY active display supports HDR."""
    for p in _hdr_active_paths():
        st = _hdr_query(p)
        if st is not None and st[0]:
            return True
    return False


def hdr_snapshot():
    """{"lo,hi,id": enabled} for every HDR-capable ACTIVE target  the
    restore record taken before forcing HDR on."""
    out = {}
    for p in _hdr_active_paths():
        st = _hdr_query(p)
        if st is not None and st[0]:
            t = p.targetInfo
            key = "%d,%d,%d" % (t.adapterId.LowPart, t.adapterId.HighPart,
                                t.id)
            out[key] = bool(st[1])
    return out


def hdr_set_all(enable):
    """Force HDR on/off on every capable active display. True when at least
    one display accepted the change."""
    ok = False
    for p in _hdr_active_paths():
        st = _hdr_query(p)
        if st is not None and st[0] and st[1] != bool(enable):
            ok = _hdr_set(p, enable) or ok
        elif st is not None and st[0]:
            ok = True                     # already in the requested state
    return ok


def hdr_restore(snap):
    """Put every snapshotted target back to its recorded HDR state."""
    if not snap:
        return
    for p in _hdr_active_paths():
        t = p.targetInfo
        key = "%d,%d,%d" % (t.adapterId.LowPart, t.adapterId.HighPart, t.id)
        if key in snap:
            st = _hdr_query(p)
            if st is not None and st[0] and st[1] != bool(snap[key]):
                _hdr_set(p, bool(snap[key]))


# --- Night Light (CloudStore registry codec) ---------------------------------
# Port of BigPictureManager's NightLightCodec: the state/settings blobs under
# the CloudStore keys have four known shapes (manual/schedule x on/off) told
# apart by a marker byte + length; conversions splice the byte runs the shapes
# share and bump the 5-byte version counter so Windows notices the change.

_NL_STATE_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"
    r"\DefaultAccount\Current"
    r"\default$windows.data.bluelightreduction.bluelightreductionstate"
    r"\windows.data.bluelightreduction.bluelightreductionstate",
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"
    r"\DefaultAccount\Cloud"
    r"\default$windows.data.bluelightreduction.bluelightreductionstate",
)
_NL_SETTINGS_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"
    r"\DefaultAccount\Current"
    r"\default$windows.data.bluelightreduction.settings"
    r"\windows.data.bluelightreduction.settings",
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"
    r"\DefaultAccount\Cloud"
    r"\default$windows.data.bluelightreduction.settings",
)

_NL_MARKER_IDX = 18
_NL_MANUAL_ON, _NL_SCHED_ON = 0x15, 0x12
_NL_MANUAL_OFF, _NL_SCHED_OFF = 0x13, 0x10
_NL_LEN = {  # (marker, exact blob length) → canonical mode
    (_NL_MANUAL_ON, 43): "manual_on", (_NL_SCHED_ON, 40): "sched_on",
    (_NL_MANUAL_OFF, 41): "manual_off", (_NL_SCHED_OFF, 38): "sched_off",
}


def _nl_read(key_variants):
    """(blob bytes, resolved key path) from the first variant holding Data."""
    if not IS_WINDOWS:
        return None, None
    import winreg
    for kp in key_variants:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, kp) as k:
                data = winreg.QueryValueEx(k, "Data")[0]
                if data:
                    return bytes(data), kp
        except OSError:
            continue
    return None, None


def _nl_write(key_variants, preferred, data):
    # Never write an empty/None blob: the shape builders return None for a
    # state they can't convert, and writing that through would DESTROY the
    # user's night light state (an empty Data value reads back as "absent").
    if not data:
        print("big_picture: refusing to write an empty night light blob")
        return False
    import winreg
    order = ([preferred] if preferred else []) + [
        k for k in key_variants if k != preferred]
    for kp in order:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, kp, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "Data", 0, winreg.REG_BINARY, data)
                return True
        except OSError:
            continue
    return False


def _nl_mode(state):
    """Canonical mode of a state blob ("manual_on", "sched_on", "manual_off",
    "sched_off", suffixed "_odd" on a marker match with an unexpected length,
    or "unknown")."""
    if not state or len(state) <= _NL_MARKER_IDX:
        return "unknown"
    m = state[_NL_MARKER_IDX]
    exact = _NL_LEN.get((m, len(state)))
    if exact:
        return exact
    return {_NL_MANUAL_ON: "manual_on_odd", _NL_SCHED_ON: "sched_on_odd",
            _NL_MANUAL_OFF: "manual_off", _NL_SCHED_OFF: "sched_off",
            }.get(m, "unknown")


def _nl_is_on(mode):
    return mode in ("manual_on", "manual_on_odd", "sched_on", "sched_on_odd")


def _nl_bump_version(b):
    out = bytearray(b)
    for i in range(10, min(15, len(out))):
        if out[i] != 0xFF:
            out[i] = (out[i] + 1) & 0xFF
            break
    return bytes(out)


def _nl_manual_off_state(src):
    """Any ON shape → the matching OFF shape (BuildGenericOffState)."""
    mode = _nl_mode(src)
    if mode in ("manual_off", "sched_off"):
        return bytes(src)
    if mode.startswith("manual_on") and len(src) >= 43:
        out = bytearray(41)
        out[0:22] = src[0:22]
        out[23:41] = src[25:43]
        out[_NL_MARKER_IDX] = _NL_MANUAL_OFF
        return _nl_bump_version(out)
    if mode.startswith("sched_on") and len(src) >= 40:
        out = bytearray(38)
        out[0:22] = src[0:22]
        out[22:38] = src[24:40]
        out[_NL_MARKER_IDX] = _NL_SCHED_OFF
        return _nl_bump_version(out)
    return None


def _nl_manual_on_state(src):
    """Any shape → manual ON (BuildManualEnabledState)."""
    if not src or len(src) < 23:
        return None
    out = bytearray(43)
    out[0:23] = src[0:23]
    out[23] = 0x10
    out[24] = 0x00
    tail = src[23:23 + 18]
    out[25:25 + len(tail)] = tail
    out[_NL_MARKER_IDX] = _NL_MANUAL_ON
    return _nl_bump_version(out)


def _nl_sched_on_state(src):
    """Any shape → schedule ON (BuildScheduleEnabledState)."""
    mode = _nl_mode(src)
    if mode == "sched_on":
        return _nl_bump_version(src)
    out = bytearray(40)
    out[0:22] = src[0:22]
    out[22] = 0x10
    out[23] = 0x00
    if mode == "sched_off" and len(src) >= 38:
        out[24:40] = src[22:38]
    elif mode == "manual_off" and len(src) >= 41:
        out[24:40] = src[23:39]
    elif mode.startswith("manual_on") and len(src) >= 43:
        out[24:40] = src[27:43]
    elif len(src) > _NL_MARKER_IDX and src[_NL_MARKER_IDX] == _NL_SCHED_ON:
        return _nl_bump_version(src)
    else:
        return None
    out[_NL_MARKER_IDX] = _NL_SCHED_ON
    return _nl_bump_version(out)


def _nl_sched_off_state(src):
    """Any shape → schedule OFF (BuildScheduleDisabledState)."""
    mode = _nl_mode(src)
    if mode == "sched_off":
        return bytes(src)
    if mode.startswith("sched_on") and len(src) >= 40:
        out = bytearray(38)
        out[0:22] = src[0:22]
        out[22:38] = src[24:40]
        out[_NL_MARKER_IDX] = _NL_SCHED_OFF
        return _nl_bump_version(out)
    if mode.startswith("manual_on") and len(src) >= 43:
        out = bytearray(38)
        out[0:23] = src[0:23]
        out[23:38] = src[28:43]
        out[_NL_MARKER_IDX] = _NL_SCHED_OFF
        return _nl_bump_version(out)
    if mode == "manual_off":
        return bytes(src)
    return _nl_manual_off_state(src)


def nightlight_supported():
    """True only when the night light state blob is present AND has a shape
    this codec recognizes.

    The mere existence of the key is NOT enough: on a machine where night
    light has never been configured the blob is a short stub (13 bytes here,
    no valid marker) that none of the shape builders can convert. Reporting
    that as "supported" made the Options toggle look usable while the feature
    silently did nothing at runtime  so the shape is checked up front and the
    picker greys the row instead."""
    state, _ = _nl_read(_NL_STATE_KEYS)
    if not state or _nl_mode(state) == "unknown":
        return False
    settings, _ = _nl_read(_NL_SETTINGS_KEYS)
    return settings is not None


def nightlight_enabled():
    state, _ = _nl_read(_NL_STATE_KEYS)
    return _nl_is_on(_nl_mode(state)) if state else False


def nightlight_snapshot():
    """JSON-safe snapshot of the CURRENT night light state + settings blobs
    (taken before disabling; drives the semantic restore)."""
    state, sp = _nl_read(_NL_STATE_KEYS)
    settings, gp = _nl_read(_NL_SETTINGS_KEYS)
    if state is None:
        return None
    return {"state": base64.b64encode(state).decode("ascii"),
            "state_path": sp,
            "settings": (base64.b64encode(settings).decode("ascii")
                         if settings else None),
            "settings_path": gp}


def nightlight_disable():
    """Turn night light OFF (and its schedule off, so a schedule window
    starting mid-session can't re-tint the TV). True when it ended up off 
    including when it already was. False when the blob has a shape this codec
    can't convert, so the caller doesn't record a session step that its
    restore could never undo."""
    state, sp = _nl_read(_NL_STATE_KEYS)
    if state is None:
        return False
    mode = _nl_mode(state)
    if mode == "unknown":
        print("big_picture: unrecognized night light state; leaving it alone")
        return False
    if not _nl_is_on(mode):
        return True                      # already off  nothing to write
    new_state = (_nl_sched_off_state(state) if mode.startswith("sched_on")
                 else _nl_manual_off_state(state))
    if new_state is None:
        print("big_picture: cannot build an OFF night light state; skipping")
        return False
    if new_state == state:
        return True
    return _nl_write(_NL_STATE_KEYS, sp, new_state)


def nightlight_restore(snap):
    """Put night light back to the snapshotted semantic mode (manual on,
    schedule on, or off)  BigPictureManager's ExecuteSemanticRestore."""
    if not snap:
        return False
    try:
        old_state = base64.b64decode(snap["state"])
    except Exception:
        return False
    old_mode = _nl_mode(old_state)
    cur_state, sp = _nl_read(_NL_STATE_KEYS)
    if cur_state is None:
        return False
    if old_mode.startswith("sched_on"):
        new_state = _nl_sched_on_state(cur_state)
    elif old_mode.startswith("manual_on"):
        new_state = _nl_manual_on_state(cur_state)
    elif old_mode == "sched_off":
        new_state = _nl_sched_off_state(cur_state)
    else:
        new_state = _nl_manual_off_state(cur_state)
    if new_state is None:
        # Couldn't rebuild the snapshotted mode from the CURRENT blob  leave
        # night light as-is rather than writing something unconvertible.
        print("big_picture: cannot rebuild the saved night light state")
        return False
    if new_state == cur_state:
        return True
    return _nl_write(_NL_STATE_KEYS, sp, new_state)


# --- Media pause (SMTC with keystroke fallback) ------------------------------

def pause_media():
    """Pause everything that is actually PLAYING. SMTC pauses each playing
    session individually (Spotify, browsers, media players); when the WinRT
    projection isn't available, fall back to one VK_MEDIA_STOP keystroke
    (stop is idempotent, unlike a play/pause toggle)."""
    try:
        import asyncio
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as _Mgr,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as _St,
        )

        async def _pause_all():
            n = 0
            mgr = await _Mgr.request_async()
            for s in mgr.get_sessions():
                try:
                    if (s.get_playback_info().playback_status
                            == _St.PLAYING):
                        if await s.try_pause_async():
                            n += 1
                except Exception:
                    continue
            return n

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_pause_all())
        finally:
            loop.close()
    except Exception as e:
        print(f"big_picture: SMTC unavailable ({e!r}); sending media stop")
        _send_media_key(0xB2)   # VK_MEDIA_STOP
        return -1


def _send_media_key(vk):
    if not IS_WINDOWS:
        return
    _user32.keybd_event(vk, 0, 0, 0)
    _user32.keybd_event(vk, 0, 2, 0)    # KEYEVENTF_KEYUP


# --- Cursor hiding (big-picture-portal port) ---------------------------------

_SYSTEM_CURSOR_IDS = (32512, 32513, 32514, 32642, 32643, 32644, 32645,
                      32646, 32648, 32649, 32650)
_SPI_SETCURSORS = 0x0057
_CURSOR_REVEAL_S = 3.0    # idle seconds before the moved-and-shown cursor hides again


def _blank_cursor():
    and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
    xor_mask = (ctypes.c_ubyte * 128)()
    return _user32.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)


def _hide_system_cursors():
    for cid in _SYSTEM_CURSOR_IDS:
        h = _blank_cursor()
        if h:
            _user32.SetSystemCursor(h, cid)


def restore_system_cursors():
    """Reload every system cursor from the registry (undoes the blanking)."""
    if IS_WINDOWS:
        _user32.SystemParametersInfoW(_SPI_SETCURSORS, 0, None, 0)


class _CursorManager:
    """Hides the mouse cursor while Big Picture runs; a mouse move reveals it
    and it re-hides after a few idle seconds (portal's cursor.go)."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bp-cursor", daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2)
        self._thread = None
        restore_system_cursors()

    def _run(self):
        _hide_system_cursors()
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        last = (pt.x, pt.y)
        visible = False
        idle_since = time.monotonic()
        while not self._stop.wait(0.1):
            _user32.GetCursorPos(ctypes.byref(pt))
            cur = (pt.x, pt.y)
            if cur != last:
                last = cur
                idle_since = time.monotonic()
                if not visible:
                    restore_system_cursors()
                    visible = True
            elif visible and time.monotonic() - idle_since > _CURSOR_REVEAL_S:
                _hide_system_cursors()
                visible = False


# --- Audio device enumeration (all states, for the picker dropdown) ----------

_MM_RENDER_BASE = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                   r"\MMDevices\Audio\Render")
_RENDER_ID_PREFIX = "{0.0.0.00000000}."
# PKEY_Device_FriendlyName / PKEY_Device_DeviceDesc as MMDevices property names
_PKEY_FRIENDLY = "{a45c254e-df1c-4efd-8020-67d146a850e0},14"
_PKEY_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_DEVICE_STATE_ACTIVE = 1


def list_audio_devices():
    """[(endpoint_id, friendly_name, is_active)] for every playback endpoint,
    INCLUDING disabled/unplugged ones  an HDMI TV that is currently off must
    still be selectable as the Big Picture device, and the switch retries
    while it wakes up (see _set_audio_retry)."""
    if not IS_WINDOWS:
        return []
    import winreg
    out = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MM_RENDER_BASE)
    except OSError:
        return []
    with base:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(base, i)
            except OSError:
                break
            i += 1
            state, name = 0, None
            try:
                with winreg.OpenKey(base, guid) as k:
                    state = int(winreg.QueryValueEx(k, "DeviceState")[0])
            except OSError:
                pass
            try:
                with winreg.OpenKey(base, guid + r"\Properties") as k:
                    for pkey in (_PKEY_FRIENDLY, _PKEY_DESC):
                        try:
                            val = winreg.QueryValueEx(k, pkey)[0]
                        except OSError:
                            continue
                        if val:
                            name = str(val)
                            break
            except OSError:
                pass
            # Skip NOTPRESENT (4) endpoints  stale registrations of hardware
            # that is gone for good; keep active/disabled/unplugged.
            if state == 4:
                continue
            out.append((_RENDER_ID_PREFIX + guid, name or guid,
                        state == _DEVICE_STATE_ACTIVE))
    return sorted(out, key=lambda t: (not t[2], t[1].lower()))


# --- The engine --------------------------------------------------------------

# How long to keep retrying an audio-device switch. An HDMI endpoint only
# finishes enumerating a moment after the display switch wakes the TV, so a
# single set attempt right after the topology change routinely misses it.
_AUDIO_SWITCH_WINDOW_S = 6.0
_AUDIO_RETRY_STEP_S = 0.75
_DISCONNECT_GRACE_S = 3.0    # controller gone this long before auto-close
# How long the controller signal is treated as untrustworthy after the tray
# un-pauses (see _auto_launch_tick). While SteamlessInput is paused for
# Steam it cedes the controllers entirely  the Steam Controller's HID handle
# is closed and _current_sc is None  so "no controller" during a pause says
# nothing about what is physically plugged in. When Steam exits, the stack
# rebuilds and the pad reappears; without this settle window that reads as a
# fresh connect and re-opens Big Picture, which restarts Steam, which pauses
# us again: an infinite reopen loop. The window must comfortably outlast the
# controller rebuild (HID reprobe + reconnect backoff).
_PAUSE_SETTLE_S = 10.0


class BigPictureEngine:
    """Owns the monitor thread + the enter/exit session state.

    Constructor hooks keep this module tray-agnostic:
      settings            the tray's live settings dict (read + recovery writes)
      save_settings       persists that dict
      get_default_audio() → current default endpoint id (tray's COM helper)
      set_default_audio(id) → bool
      controller_connected() → bool (any live pad right now)
      controller_paused()   → bool (the tray has ceded the controllers, so
                              controller_connected() is meaningless  see
                              _PAUSE_SETTLE_S). Optional; defaults to never.
      notify(title, msg)  tray toast (failures only)
    """

    def __init__(self, settings, save_settings, get_default_audio,
                 set_default_audio, controller_connected, notify,
                 controller_paused=None):
        self.settings = settings
        self._save = save_settings
        self._get_audio = get_default_audio
        self._set_audio = set_default_audio
        self._pads_live = controller_connected
        self._pads_paused = controller_paused or (lambda: False)
        self._notify = notify
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._cursor = _CursorManager()
        self._session = None          # in-memory copy of the recovery record
        self._bp_seen = False
        self._pads_seen = None        # None until the first sample (no edge)
        self._pads_gone_at = None
        # Pause-boundary guard: _was_paused latches a pause so the RESUME can
        # be detected, which arms _pads_settle_until (edges ignored until then).
        self._was_paused = False
        self._pads_settle_until = 0.0

    # -- lifecycle --

    def start(self):
        if self._thread is not None:
            return
        self._recover_after_crash()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="big-picture", daemon=True)
        self._thread.start()

    def stop(self):
        """App shutdown. The session's system changes are left in place (Big
        Picture may still be open; the persisted recovery record repairs
        things on the next launch once BP is gone)  EXCEPT the blanked
        cursors, which nothing else would ever restore."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._cursor.stop()

    def refresh(self):
        """A bp_* setting changed  re-evaluate the enabled state promptly."""
        self._wake.set()

    # -- settings --

    def _s(self, key, default=None):
        return self.settings.get(key, default)

    def _session_features_on(self):
        return any(self._s(k) for k in (
            "bp_display", "bp_audio", "bp_nightlight", "bp_pause_media",
            "bp_hdr", "bp_hide_cursor"))

    def _watching(self):
        return (self._session_features_on()
                or self._s("bp_auto_launch", "off") != "off")

    # -- crash recovery --

    def _recover_after_crash(self):
        """A previous run died (or exited) mid-session: once Big Picture is
        no longer running, put everything the record lists back."""
        rec = self._s("bp_recovery") or {}
        if not rec.get("active"):
            return
        if is_big_picture_running():
            # Still in Big Picture  adopt the session instead of reverting
            # under it; the monitor loop will revert on its normal exit edge.
            self._session = dict(rec)
            self._bp_seen = True
            if rec.get("cursor_hidden"):
                self._cursor.start()
            print("big_picture: adopted live session from recovery record")
            return
        print("big_picture: recovering interrupted session")
        self._revert_session(dict(rec))
        self._clear_recovery()

    def _persist_recovery(self):
        self.settings["bp_recovery"] = dict(self._session or {})
        try:
            self._save(self.settings)
        except Exception as e:
            print(f"big_picture: recovery persist failed: {e!r}")

    def _clear_recovery(self):
        self._session = None
        if self._s("bp_recovery"):
            self.settings["bp_recovery"] = {}
            try:
                self._save(self.settings)
            except Exception as e:
                print(f"big_picture: recovery clear failed: {e!r}")

    # -- monitor loop --

    def _run(self):
        while not self._stop.is_set():
            if not self._watching() and self._session is None:
                # Nothing enabled  block until a setting change wakes us.
                self._wake.wait()
                self._wake.clear()
                continue
            try:
                self._tick()
            except Exception as e:
                print(f"big_picture: tick error: {e!r}")
            self._wake.wait(1.0)
            self._wake.clear()

    def _tick(self):
        bp = is_big_picture_running() if (self._session_features_on()
                                          or self._s("bp_auto_close")
                                          or self._session is not None
                                          or self._bp_seen) else False
        if bp and not self._bp_seen:
            self._bp_seen = True
            if self._session is None and self._session_features_on():
                self._enter_session()
        elif not bp and self._bp_seen:
            self._bp_seen = False
            if self._session is not None:
                self._exit_session()
        self._auto_launch_tick(bp)

    # -- auto open/close --

    def _auto_launch_tick(self, bp_running):
        mode = self._s("bp_auto_launch", "off")
        if mode == "off":
            self._pads_seen = None
            self._pads_gone_at = None
            return
        # Edges ARE sampled while the tray is paused for Steam  both the
        # "When Steam Is Running" auto-open and the auto-close can only ever
        # fire while Steam is up, i.e. while paused  but that relies on the
        # caller's controller_connected() staying truthful across the pause
        # (the tray falls back to a passive presence probe; see
        # _bp_controller_present). What must NOT be trusted is the boundary
        # itself: the presence signal swaps source there, and the controller
        # stack takes seconds to rebuild after Steam exits, so a pad
        # "appearing" then is an artefact. Left unguarded that re-opens Big
        # Picture, which restarts Steam, which pauses us again  forever.
        paused = self._pads_paused()
        if paused:
            self._was_paused = True
        elif self._was_paused:
            self._was_paused = False
            self._pads_settle_until = time.monotonic() + _PAUSE_SETTLE_S
        if time.monotonic() < self._pads_settle_until:
            # Re-baseline every tick while settling: whatever the controller
            # state is when the window expires becomes the "before" for the
            # next real edge, so a mid-rebuild appearance never fires.
            self._pads_seen = None
            self._pads_gone_at = None
            return
        pads = bool(self._pads_live())
        prev = self._pads_seen
        self._pads_seen = pads
        if prev is None:
            return                     # first sample after enable  no edge
        if pads and not prev:
            self._pads_gone_at = None
            if not bp_running:
                if mode == "always" or _steam_proc_running():
                    print("big_picture: controller connected  opening BP")
                    open_big_picture()
        elif not pads and prev:
            self._pads_gone_at = time.monotonic()
        if (not pads and self._pads_gone_at is not None
                and time.monotonic() - self._pads_gone_at
                >= _DISCONNECT_GRACE_S):
            self._pads_gone_at = None
            if (self._s("bp_auto_close") and bp_running
                    and not steam_game_running()):
                print("big_picture: last controller gone  closing BP")
                close_big_picture()

    # -- session enter/exit --

    def _enter_session(self):
        print("big_picture: Big Picture detected  applying session")
        ses = {"active": True}

        if self._s("bp_pause_media"):
            try:
                pause_media()
            except Exception as e:
                print(f"big_picture: media pause failed: {e!r}")

        if self._s("bp_nightlight") and nightlight_supported():
            snap = nightlight_snapshot()
            # Only record the revert step if the disable actually landed 
            # otherwise the exit would "restore" a state we never changed.
            if snap and nightlight_disable():
                ses["night"] = snap

        dev = self._s("bp_display_device")
        if self._s("bp_display") and dev:
            topo = save_display_topology()
            if topo and set_only_display(dev):
                ses["display"] = topo
            elif topo:
                self._notify("Big Picture",
                             "Could not switch to the Big Picture display.")

        if self._s("bp_audio") and self._s("bp_audio_device"):
            prev = self._get_audio()
            target = self._s("bp_audio_device")
            if prev != target and self._set_audio_retry(target):
                ses["audio_prev"] = prev
            elif prev != target:
                self._notify("Big Picture",
                             "Could not switch the audio output.")

        if self._s("bp_hdr"):
            snap = hdr_snapshot()
            if snap:
                ses["hdr_prev"] = snap
                hdr_set_all(True)

        if self._s("bp_hide_cursor"):
            ses["cursor_hidden"] = True
            self._cursor.start()

        self._session = ses
        self._persist_recovery()

    def _exit_session(self):
        print("big_picture: Big Picture closed  reverting session")
        self._revert_session(self._session or {})
        self._clear_recovery()

    def _revert_session(self, ses):
        """Undo everything a session record lists, most-visible first."""
        if ses.get("cursor_hidden"):
            self._cursor.stop()
            restore_system_cursors()

        if ses.get("hdr_prev"):
            try:
                hdr_restore(ses["hdr_prev"])
            except Exception as e:
                print(f"big_picture: HDR restore failed: {e!r}")

        if ses.get("display"):
            try:
                if not restore_display_topology(ses["display"]):
                    self._notify("Big Picture",
                                 "Could not restore the display setup  use "
                                 "Win+P if a monitor is still off.")
            except Exception as e:
                print(f"big_picture: display restore failed: {e!r}")

        if ses.get("audio_prev"):
            try:
                self._set_audio_retry(ses["audio_prev"])
            except Exception as e:
                print(f"big_picture: audio restore failed: {e!r}")

        if ses.get("night"):
            try:
                nightlight_restore(ses["night"])
            except Exception as e:
                print(f"big_picture: night light restore failed: {e!r}")

    def _set_audio_retry(self, endpoint_id):
        """Set the default audio device, retrying (up to the
        _AUDIO_SWITCH_WINDOW_S deadline) while a just-activated HDMI endpoint
        finishes enumerating. Bails early on engine shutdown."""
        deadline = time.monotonic() + _AUDIO_SWITCH_WINDOW_S
        while True:
            if self._set_audio(endpoint_id):
                return True
            if (time.monotonic() >= deadline
                    or self._stop.wait(_AUDIO_RETRY_STEP_S)):
                return False


def _steam_proc_running():
    try:
        import psutil
        for p in psutil.process_iter(attrs=["name"]):
            if (p.info.get("name") or "").lower() == "steam.exe":
                return True
    except Exception:
        pass
    return False
