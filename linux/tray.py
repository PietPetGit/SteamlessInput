"""Steam Controller Keyboard  system-tray launcher.

This is the bundled entry point for the portable EXE. It:
  * Runs a tray icon (right-click menu: Launch at PC start, Close when Steam
    starts, Exit). Settings persist in `settings.json` next to the EXE.
  * Watches the Steam Controller for the Steam+X chord and brings up the
    on-screen keyboard in-process (no subprocess startup cost).
  * Optionally pauses the listener while Steam is running and resumes after
    Steam exits (the controller is released so Steam can grab it).
"""

import atexit
import ctypes
import json
import math
import re
from collections import deque
import os
import sys
import shutil
import threading
import time
import traceback
from ctypes import wintypes


# --- Resource / path helpers ------------------------------------------------

def _is_frozen():
    return getattr(sys, "frozen", False)


def _bundle_dir():
    """Directory containing read-only bundled resources (data/, glyphs)."""
    if _is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _exe_dir():
    """Directory we treat as the install location (for portable settings)."""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _exe_path():
    return os.path.abspath(sys.executable) if _is_frozen() else os.path.abspath(__file__)


# --- Crash capture ------------------------------------------------------------
# The portable EXE runs windowed: when something aborts the process at the C
# level (a Tcl panic, a "Fatal Python error", a CRT abort inside an extension),
# its one diagnostic line goes to an invisible stderr and the app just
# vanishes. Route the C-level stderr (fd 2) into crash.log next to the EXE and
# arm faulthandler, so a hard crash leaves the fatal message plus every
# thread's Python stack behind instead of nothing.
import faulthandler  # noqa: E402


def _arm_crash_log():
    try:
        path = os.path.join(_exe_dir(), "crash.log")
        try:
            if os.path.getsize(path) > 1_000_000:   # keep the log bounded
                os.replace(path, path + ".old")
        except OSError:
            pass
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        f.write(f"\n=== launch {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"pid={os.getpid()} frozen={_is_frozen()} ===\n")
        if _is_frozen():
            # Frozen build only  from source the console keeps its stderr.
            os.dup2(f.fileno(), 2)      # C-level stderr (panic/abort messages)
            sys.stderr = f              # Python tracebacks from every thread
        faulthandler.enable(file=f, all_threads=True)
        return f
    except Exception:
        return None


_crash_log_file = _arm_crash_log()   # module-lifetime ref: faulthandler + fd 2


# --- Single instance ----------------------------------------------------------
# Nothing stopped a second copy of the EXE from starting: both then fight over
# the controller's HID handles, the virtual pad and the tray icon  input
# doubles, device grabs fail, and the app reads as crash-prone. Hold a named
# mutex for the process lifetime. A SECOND launch used to just show an
# "already running" MessageBox and exit  which read as "the program does
# nothing / the GUI refuses to open" (the box is easy to miss or dismiss, and
# double-clicking the exe is the natural way to try to get the GUI back). Now
# a second launch SIGNALS the running instance to open the Keybinds manager
# (via a named auto-reset event the tray watches) and exits silently  so
# re-launching the exe always produces the GUI, matching what the user meant.

_OPEN_GUI_EVENT_NAME = "SteamlessInput-open-gui"


def _ensure_single_instance():
    """Returns (mutex_handle, open_gui_event_handle)  both None off-Windows /
    on failure. Exits the process (after signaling the first instance) when
    another copy already holds the mutex."""
    if sys.platform != "win32":
        return None, None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_wchar_p]
        k32.CreateEventW.restype = ctypes.c_void_p
        k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_wchar_p]
        k32.SetEvent.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        # A self-relaunch (Reset Settings writes fresh defaults and spawns a
        # new copy of the exe) can start the new process a beat before the
        # outgoing one has fully released the mutex  Windows only frees a
        # named mutex once every handle to it is closed, which happens at
        # process teardown, not the instant exit_app() is called. Retry for
        # up to ~3s before concluding it's a REAL second launch; a genuine
        # double-launch still resolves on the very first try below.
        h = None
        already = True
        for _attempt in range(15):
            h = k32.CreateMutexW(None, False, "SteamlessInput-single-instance")
            already = ctypes.get_last_error() == 183      # ERROR_ALREADY_EXISTS
            if not already:
                break
            k32.CloseHandle(h)
            time.sleep(0.2)
        # Auto-reset (bManualReset=0) named event, shared by both processes:
        # the second launch sets it; the first instance's watcher thread waits
        # on it and opens the Keybinds GUI once per signal.
        ev = k32.CreateEventW(None, 0, 0, _OPEN_GUI_EVENT_NAME)
        if already:
            if ev:
                k32.SetEvent(ev)      # ask the running instance to open the GUI
            sys.exit(0)
        return h, ev
    except SystemExit:
        raise
    except Exception:
        return None, None


# Held until process exit; the event handle feeds the open-GUI watcher thread.
_single_instance_mutex, _open_gui_event = _ensure_single_instance()


# --- Orphaned onefile-extraction cleanup --------------------------------------
# A --onefile EXE unpacks its whole payload (~44 MB / ~1600 files) into a fresh
# %TEMP%\_MEIxxxxx on every launch and deletes it again on a NORMAL exit. An
# ABNORMAL end  Task Manager "End task", a crash, a power loss, the OS killing
# us at shutdown  skips that cleanup and strands the directory forever.
#
# They accumulate silently and are not harmless: measured here, startup degraded
# from ~2.3 s to ~19 s as the count climbed (244 strays were sitting in %TEMP%),
# because every launch re-extracts into an ever-more-crowded directory that the
# AV then rescans. That is exactly the "it gets slower on slow PCs over time"
# failure mode, and a user would never connect it to this app.
#
# So sweep our OWN strays at startup, on a background thread (never delaying the
# tray icon). Safety rules, in order:
#   * skip the directory we are currently running from (sys._MEIPASS);
#   * only touch dirs carrying OUR marker file, so another PyInstaller app's
#     _MEI dir is never deleted  the folder name alone is not proof of owner;
#   * a dir still in use by a live instance keeps its files locked, so the
#     delete simply fails and we move on. Best-effort throughout: cleanup must
#     never be able to stop the app from starting.
_MEI_MARKER = os.path.join("data", "images", "app_icon.ico")
# Deleting one stray is ~1600 file unlinks. Someone who has been force-killing
# the app for months can have hundreds, and clearing them all at once would be
# a minutes-long disk-I/O storm competing with the launch we are in the middle
# of. Cap the work per launch and let successive runs catch up  the count only
# ever grows by one per abnormal exit, so a small cap still wins the race.
_MEI_SWEEP_LIMIT = 20


def _sweep_orphan_meipass():
    # Not win32-gated: PyInstaller's onefile extract-and-strand behaviour is
    # identical on Linux (/tmp/_MEIxxxxx), and this is pure os/shutil.
    if not _is_frozen():
        return
    try:
        tmp = os.path.dirname(os.path.abspath(sys._MEIPASS))
        keep = os.path.normcase(os.path.abspath(sys._MEIPASS))
    except Exception:
        return
    removed = 0
    try:
        names = [d for d in os.listdir(tmp) if d.startswith("_MEI")]
    except OSError:
        return
    for name in names:
        path = os.path.join(tmp, name)
        try:
            if os.path.normcase(os.path.abspath(path)) == keep:
                continue
            if not os.path.isdir(path):
                continue
            # Ours? (Never delete a different frozen app's extraction dir.)
            if not os.path.isfile(os.path.join(path, _MEI_MARKER)):
                continue
            shutil.rmtree(path, ignore_errors=True)
            # rmtree(ignore_errors) reports nothing; a still-locked dir simply
            # survives (partially emptied  harmless, retried next launch), so
            # confirm before counting it.
            if not os.path.exists(path):
                removed += 1
            if removed >= _MEI_SWEEP_LIMIT:
                break
        except Exception:
            continue
    if removed:
        print(f"cleaned {removed} orphaned onefile temp dir(s)")


# IMPORTANT: ADUSK_DATA must be set before importing adusk.*  adusk.resources
# captures its env-var search path at import time.
os.environ["ADUSK_DATA"] = os.path.join(_bundle_dir(), "data")
# (SDL3 DLLs are located by sdl3w/_loader.py via sys._MEIPASS  no env var needed.)


import pystray  # noqa: E402

# pystray's Win32 backend opens the tray menu, and every nested submenu,
# anchored/cascading toward the right of the cursor. TrackPopupMenuEx
# always clamps its requested position to keep the menu fully on-screen,
# so requesting an anchor point far past the right edge (with
# TPM_RIGHTALIGN, which pystray already passes below) lands the menu
# flush against that edge instead. With zero room to its right, Windows'
# normal submenu placement then auto-flips every nested flyout to open
# leftward too  using ordinary left-to-right item rendering (text +
# arrow), unlike TPM_LAYOUTRTL which also mirrors that layout.
from pystray._util import win32 as _pystray_win32  # noqa: E402
_pystray_track_popup_menu_ex = _pystray_win32.TrackPopupMenuEx


def _track_popup_menu_ex_left(hmenu, flags, x, y, hwnd, params):
    # Default anchor: the cursor itself. pystray passes TPM_RIGHTALIGN, so
    # anchoring the menu's right edge at the cursor already opens it leftward,
    # next to the tray icon, on whichever monitor was clicked. This is the safe
    # fallback for any layout we don't special-case below.
    anchor_x = x
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        hmon = user32.MonitorFromPoint(
            wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
        if hmon:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
                # rcWork excludes the taskbar, matching how TrackPopupMenuEx
                # clamps the menu within the monitor's usable area.
                work_w = mi.rcWork.right - mi.rcWork.left
                if work_w > 0 and (mi.rcWork.right - x) <= work_w // 2:
                    # Icon is in the RIGHT half of its monitor  the usual
                    # bottom-right system tray. Slam the anchor to *this*
                    # monitor's right edge: with no room to the right, Windows
                    # auto-flips the menu and every nested submenu leftward
                    # (the behavior we want). Clamping to the clicked monitor's
                    # edge (instead of a blind `x + 10000`) is what keeps a
                    # multi-monitor setup from flinging the menu onto the screen
                    # to the right.
                    anchor_x = mi.rcWork.right
                # Otherwise the icon is in the LEFT half (taskbar moved to a
                # secondary/left monitor, a vertical/left taskbar, or a click
                # that landed on the neighbor screen at the seam). Leave the
                # anchor at the cursor so the menu opens where it was clicked
                # and lets Windows choose a sane submenu flip direction, rather
                # than slamming it to a far edge / the wrong monitor.
    except Exception:
        pass
    return _pystray_track_popup_menu_ex(
        hmenu, flags, anchor_x, y, hwnd, params)


_pystray_win32.TrackPopupMenuEx = _track_popup_menu_ex_left

from PIL import Image  # noqa: E402

import sdl3w as S  # noqa: E402
from steamcontroller import SteamController, SCButtons, SCStatus  # noqa: E402
from steamcontroller import present_hid_kinds  # noqa: E402  (sc/sc2015/deck)
from steamcontroller import enumerate_data_interfaces  # noqa: E402  (multiplayer)
from steamcontroller import GYRO_DEG_PER_SEC  # noqa: E402  (raw gyro → °/s)
from steamcontroller import uinput as sui  # noqa: E402
from steamcontroller.gamepad import VirtualGamepad, ViGEmUnavailable  # noqa: E402
from adusk import adusk as adusk_app  # noqa: E402
from adusk import inputsrc as adusk_inputsrc  # noqa: E402
from adusk import key_sound as adusk_key_sound  # noqa: E402
from adusk import power as adusk_power  # noqa: E402
from adusk import screen as adusk_screen  # noqa: E402
from adusk import skins as adusk_skins  # noqa: E402
from adusk import state as adusk_state  # noqa: E402
import keybinds_runtime  # noqa: E402  (import-safe: no tkinter/SDL)
import pads  # noqa: E402  (import-safe: controller catalog + identification)
import vmenu  # noqa: E402  (import-safe: ctypes-only touch-menu overlay)
import sc_viewer  # noqa: E402  (import-safe: PIL only  publish slot for the
                  # keybinds picker's live controller preview)
import autostart  # noqa: E402
import big_picture  # noqa: E402  (import-safe: subprocess/glob only  the
#                     Options → Big Picture controller-connect automation)


SETTINGS_FILENAME = "settings.json"
STEAM_PROC_NAME = "steam.exe"

# Buttons the keybinds picker consumes for controller UI navigation while its
# window is visible + foreground (sc_viewer.nav_claimed()): dpad moves the
# highlight, A activates, B cancels, X removes / resets, Y toggles fine slider
# adjust, Menu/≡ (START) opens value entry / Listen bind mode, LB/RB cycle the
# tab pills. Masked out of on_input's dispatch so they don't double-fire
# desktop binds / XInput.
_PICKER_NAV_MASK = (SCButtons.A | SCButtons.B
                    | SCButtons.X | SCButtons.Y
                    | SCButtons.START
                    | SCButtons.DPAD_UP | SCButtons.DPAD_DOWN
                    | SCButtons.DPAD_LEFT | SCButtons.DPAD_RIGHT
                    | SCButtons.LB | SCButtons.RB)

# While the picker's "Listen" bind-capture is armed (sc_viewer.listen_claimed())
# EVERY button bit is masked out of dispatch  the press about to be captured
# as a binding must not also fire its desktop/XInput action. Only the touch
# SENSORS survive (they gate trackpad-mouse motion, and a rest/touch is not a
# capturable "press").
_PICKER_LISTEN_KEEP = (SCButtons.LPADTOUCH | SCButtons.RPADTOUCH
                       | SCButtons.LPADJOY_TOUCH | SCButtons.RPADJOY_TOUCH
                       | SCButtons.LGRIP_REST | SCButtons.RGRIP_REST)

# The two "guide" buttons (Steam logo / "..." QAM). The picker's controller
# navigation is a BARE-button vocabulary  it never uses a Steam-held chord 
# so a frame with either of these down is not navigation and must NOT go
# through _PICKER_NAV_MASK. Masking it there broke two things at once while the
# config GUI was foreground: Steam+X could no longer open the OSK (X was gone
# by the time the open check ran), and every Steam+button chord looked like a
# CLEAN Steam tap to the tap detector, which fired the Guide tap action
# ("Toggle Config GUI" by default) instead.
_GUIDE_BITS = SCButtons.STEAM | SCButtons.QAM

# Touch/rest sensor bits the built-in "hold ≡ to switch Desktop <-> Gamepad"
# gesture ignores when deciding whether Start/Menu is held ALONE  a thumb
# resting on a trackpad or a hand gripping the controller must not cancel it.
# (Any REAL button still does, so Start-based chords keep working.)
_MODE_HOLD_PASSIVE_MASK = keybinds_runtime.mode_hold_passive_mask(SCButtons)

# Ceiling on the keybind profile slots ONE (controller, layout tab) may hold 
# the picker's footer "+" stops here and every slot number read out of
# settings.json is clamped to it. MUST match keybinds_picker._MAX_PROFILES:
# a count the picker would clamp lower than the tray stores would leave a slot
# unreachable from the footer.
_MAX_KEYBIND_PROFILES = 10
# Longest profile name accepted from the picker's footer name box. Mirrors
# keybinds_picker._NAME_MAX  the box caps what it accepts, this caps what a
# hand-edited settings.json can smuggle past it.
_MAX_PROFILE_NAME = 24

DEFAULT_SETTINGS = {
    # Default OFF: a freshly downloaded, unsigned binary that silently adds
    # itself to autostart on first launch is exactly the "drive-by persistence"
    # pattern Defender's ML flags. Users opt in via the tray "Start with
    # Windows" toggle, which is a deliberate, user-initiated action.
    "start_with_windows": False,
    "disable_while_steam_running": True,
    "exit_on_steam_launch": False,
    # Options → Virtual Menus: Steam-style touch menus for the SC / Deck
    # trackpads (see keybinds_runtime.vmenus_sanitize for the shape). A fresh
    # install ships with one menu already built and armed on Guide + DPad Up
    # rather than an empty list  see default_virtual_menus() for why. Only a
    # settings.json with NO virtual_menus key of its own picks this up, so an
    # existing user who deleted all their menus keeps an empty list.
    #
    # Never mutated in place: every reader runs it through vmenus_sanitize(),
    # which deep-copies, and every writer replaces the whole list.
    "virtual_menus": keybinds_runtime.default_virtual_menus(),
    # Options → General "Interactive Controller Preview": lights up the
    # on-screen controller live from SC input in the Keybinds picker. Off keeps
    # the static controller image but skips the live viewer (and its startup
    # rasterize) to save resources.
    "controller_preview": True,
    # Latched the first time the guided tour is finished OR skipped, so a
    # first launch runs it exactly once. Options → General has a "Show
    # Tutorial" button to replay it on demand; this flag is never cleared by
    # that, since "seen it" stays true either way.
    "tutorial_done": False,
    # When on, the controller is presented to the OS as a virtual Xbox 360
    # gamepad (via ViGEm). Lizard mode (the firmware mouse/kb emulation)
    # is disabled while this is active. Steam+X still opens the OSK.
    "gamepad_mode": False,
    # When on, gamepad mode is automatically toggled on while a fullscreen
    # game is in the foreground, and back off when that process exits.
    # Default ON for first-run users so the controller "just works" in games
    # without requiring a manual toggle.
    "auto_gamepad_mode": True,
    # "Manual" gamepad state (picker: ViGEm Bus Driver ON + Auto Enable OFF):
    # the virtual pad stays loaded (vg_should_live) but the controller defaults
    # to DESKTOP controls  you switch to gamepad with the "Hotkey Gamepad/
    # Desktop Toggle" chord. Distinct from "always" (gamepad_mode=True) and from
    # "off" (no pad). Mutually exclusive with gamepad_mode/auto_gamepad_mode.
    "gamepad_manual": False,
    # Per-controller haptics: gates the on-screen-keyboard click feedback AND
    # gamepad/desktop rumble for that controller. Each controller's tray submenu
    # has its own Vibration toggle (no global switch). "sc" = Steam Controller,
    # "switch" = the Nintendo Switch Pro (and other SDL pads).
    "rumble_enabled_sc": True,
    "rumble_enabled_switch": True,
    # Game force-feedback while in gamepad (XInput) mode  separate from the
    # desktop "Vibration" toggles above, which gate the app's own UI haptics.
    "rumble_gamepad_sc": True,
    "rumble_gamepad_switch": True,
    # Options -> Switch Pro "Bluetooth Safe Mode". Nintendo pads put every
    # non-Switch host into Bluetooth sniff mode, whose thin bandwidth our
    # rumble traffic (and the IMU flood the rumble itself provokes) saturates
    # until the link drops - the notorious ~20-minute Joy-Con/Pro dropout. On,
    # we pace the rumble packets we send those pads and trickle a keepalive so
    # the link stays up. Applies ONLY to Nintendo pads on Bluetooth; a USB-C
    # Pro Controller is never touched. See nintendo_bt.py.
    "nintendo_bt_safe": True,
    # Options -> Switch Pro "Keep Gamepad Slot On Dropout". When a wireless pad
    # disappears we hold its virtual gamepad open (zeroed) for
    # _PAD_DROPOUT_GRACE seconds and hand the SAME device back to the same
    # physical controller when it reconnects, so a game doesn't see its
    # gamepad vanish and re-appear as a new one. Applies to every wireless
    # controller; Nintendo pads are just the ones that need it constantly.
    "bt_hold_slot": True,
    # Options → (Nintendo controller) "Combine Joy-Cons Into One Controller".
    # On (SDL's own default), a connected left+right Joy-Con act as a single
    # Pro-Controller-shaped pad. Off, each half is its own controller, so two
    # people can play with one Joy-Con each. Read by SDL at init, so it applies
    # on the next launch.
    "joycon_combine": True,
    # "Hold Single Joy-Con Upright". A lone Joy-Con is normally played sideways
    # (its rail buttons SL/SR become the shoulders); on, SDL presents it in the
    # upright orientation instead. Also init-time.
    "joycon_vertical": False,
    # "Rotate Single Joy-Con Stick". A lone Joy-Con is held sideways, so its
    # stick needs a quarter turn to line "up" up with the user's up  but
    # whether the driver has ALREADY done that turn varies. On, we apply it
    # ourselves (the correct way round per half  see pads.single_joycon_side).
    # Unlike the two hints above this is ours, not SDL's, so it applies live.
    "joycon_stick_rotate": False,
    # Pinch To Zoom (Touchpads page): one finger on EACH pad  spread apart
    # zooms the desktop via the Magnification API, common motion pans.
    "pinch_zoom": False,
    # Zoomed 360°-pan camera sensitivity (slider under the Pinch To Zoom
    # toggle): 0..1 float mapped 1:1 to _Watcher.LPAN_SENS. 0.7 = shipped
    # default = the slider's 70% mark.
    "pinch_sensitivity": 0.7,
    # "Swipe Between Pages" (Touchpads page): a fast horizontal flick on the
    # LEFT pad = Back/Forward (browser, File Explorer, Settings)  the
    # macbook two-finger page swipe.
    "swipe_pages": False,
    # Per-direction Swipe Between Pages outputs (Touchpads cog modal): a
    # picker action-vocabulary VALUE id, same list as a Hotkeys "Button
    # Combo" output. Defaults match the original hardcoded behavior (flick
    # right = Back, flick left = Forward).
    "swipe_right_output": "page_prev",
    "swipe_left_output": "page_next",
    # "Right Touchpad Tap to Click" (Touchpads page): a quick, still
    # touch-and-lift on the RIGHT pad = a left click  the laptop touchpad
    # tap. Double-tap = double-click (the shake-freeze keeps the two clicks
    # inside the double-click rectangle).
    "tap_to_click": False,
    # "Left Touchpad Tap to Click" (Touchpads page): the left-pad twin of the
    # above  a quick, still touch-and-lift on the LEFT pad fires a RIGHT
    # click instead.
    "tap_to_click_left": False,
    # "Trackpad Keyboard Typing Mode" (Touchpads page): ONE dropdown picking how the
    # trackpads drive the on-screen keyboard. The three behaviours it selects
    # used to be independent toggles (release_to_type / touch_typing /
    # swipe_typing); they are strictly escalating variations on the same
    # gesture, so a single-select mode replaced them. See TYPING_MODE_FLAGS
    # for what each one switches on, and _load_settings for the migration off
    # the old booleans.
    "typing_mode": "default",
    # "Block SteamInput Steam Controller grab": open the physical Steam Controller
    # HID exclusively so Steam can't read it (no Steam Input / forced lizard while
    # we hold it). Applies in ALL modes (desktop + gamepad) on its own  see the
    # use_exclusive line in launcher_thread. Must be enabled before Steam opens the
    # controller to win the grab.
    "block_sc_hid": False,
    # "Block SteamInput Xbox Controller grab": hide the VIRTUAL ViGEm Xbox 360 pad
    # from Steam (via the SDL_GAMECONTROLLER_IGNORE_DEVICES user env var) so Steam
    # Input can't grab it. Independent of block_sc_hid; takes effect the next time
    # Steam is launched. See _set_xbox_ignore.
    "block_gamepad_takeover": False,
    # When False the Debug submenu is hidden; toggled via the "Debug menu"
    # item in the Startup submenu.
    "debug_menu_unlocked": False,
    # Name of the selected Steam on-screen-keyboard skin (a .css under
    # data/skins/). Unlike the others this is a string, not a bool  see the
    # type-aware coercion in _load_settings. Applied when the OSK next opens.
    "skin": "DefaultTheme",
    # OSK transparency as a CONTINUOUS 0..1 slider fraction (Options-tab "On
    # Screen Keyboard → Transparency" + tray "Keyboard Skin → Transparent"
    # submenu). 0 = off (opaque); higher = more transparent. The named tray
    # notches sit at 0 / 1/3 / 2/3 / 1 (off/low/medium/high); positions between
    # interpolate the opacity scale (see skins.set_transparency_fraction). Old
    # string values are migrated in _load_settings.
    "osk_transparency": 0.0,
    # OSK window size (tray "Keyboard Skin → Size" submenu): "small" /
    # "medium" (the original 1286x369 size, default) / "full" (fills the
    # primary display's usable bounds edge-to-edge - good for touchscreens
    # like the Steam Deck). Applied on the next OSK open after the setting
    # changes (see App._rebuild_cached_screen).
    "osk_size": "medium",
    # "Keyboard Layout" (Options -> Keyboard): "classic" (the Steam-style full
    # QWERTY) or "phone" (Android-style: number row, corner symbol hints, and
    # two ?123 symbol pages). Read when the OSK opens.
    "osk_layout": "classic",
    # "Split Keyboard" (Options -> Keyboard): break the board into left/right
    # halves anchored to the screen edges with a transparent band between them,
    # so each trackpad covers its own half and neither thumb reaches across the
    # controller. Applied live.
    "osk_split_layout": False,
    # "Scale With Resolution": size the keyboard against the 1080p reference
    # the layout was designed at instead of a fixed pixel size, so it keeps the
    # same relative footprint on a 4K panel (capped at 1.5x). Off by default so
    # nobody's keyboard changes size on upgrade.
    "osk_scale_display": False,
    # "Hold For Accents": holding a letter key opens a row of its accented
    # variants; releasing over one types it. A quick tap still types the base
    # letter with no added latency.
    "osk_diacritics": True,
    # "Accent Language": which locale's variant map the row uses. "auto"
    # resolves from the Windows keyboard layout at startup.
    "osk_diacritic_locale": "auto",
    # "Key Hit Assist": px of grab radius around every key when a press is
    # resolved, so fast two-finger typing stops mistyping on near-misses.
    # 0 = exact key rects.
    "osk_hit_assist": 10,
    # "Press To Focus Key": pressing a pad down (or pulling past the focus
    # point) freezes the pointer on the key centre for the rest of the press,
    # so the shove that comes with a click can't slide it onto the neighbour.
    "osk_press_focus": True,
    # Where in an L2/R2 pull that freeze engages, 0-100% (the click still fires
    # at its own actuation setting  this only decides when the AIM locks).
    "osk_focus_pull": 50,
    # "Keyboard Sounds": play Steam's own OSK audio (key click, open, close)
    # from the Steam install. Silent on a machine without Steam.
    "osk_key_sound": True,
    # "Remember Per App": each foreground app reopens the keyboard with the
    # spot, size and skin it was last left at. The three maps below are the
    # remembered values, {exe name: value}  written by the OSK, no UI.
    "osk_per_app": False,
    "osk_pos_per_app": {},
    "osk_size_per_app": {},
    "osk_skin_per_app": {},
    # "LStick Controls"  left stick navigates OSK selection cursor.
    # OFF = left stick drives desktop arrows while the OSK is open.
    "kbd_lstick_mouse": True,
    # "Mouse Controls"  mouse/right-stick can hover and click OSK keys.
    # OFF = mouse only reaches the close X; keys are not hoverable/clickable.
    "kbd_rstick_mouse": True,
    # --- "Gyro To Type" (the cog card at the top of the Options → Keyboard
    # page). GLOBAL for every controller, unlike the per-kind Gyro To Mouse
    # tuning  see adusk_state.KBD_GYRO_DEFAULTS.
    # "Always Type With Gyro"  the gyro steers the keyboard pointer from the
    # moment the keyboard opens, without first toggling Gyro To Mouse. Off by
    # default; the per-controller gyro hotkey still flips it while typing.
    "kbd_gyro_always": False,
    "kbd_gyro_sens": 2.5,          # pointer speed multiplier
    "kbd_gyro_accel": "off",       # off / linear / relaxed / aggressive
    "kbd_gyro_deadzone": 0.36,     # °/s hand-shake filter
    "kbd_gyro_precision": 0.75,    # °/s below which sensitivity scales down
    # OSK function → SC button map (Options "On Screen Keyboard" page). Picks
    # which physical Steam Controller button drives each OSK action; the defaults
    # reproduce the built-in mapping. A nested dict, NOT a bool, so _load_settings
    # passes it through unchanged (missing keys fall back to these defaults via
    # adusk_state.set_osk_buttons / the picker).
    "osk_buttons": {
        "caps": "l3", "shift": "l2", "enter": "r2", "space": "y", "backspace": "x",
    },
    # Which gamepad button inputs the highlighted key while the left/right
    # touchpad is touched (Options → On Screen Keyboard). Default L2 / R2.
    "lpad_click_button": "l2",
    "rpad_click_button": "r2",
    # Steam Controller-only OSK settings (tray "Steam Controller" submenu).
    # L2/R2 actuation point. Tray menu stores a named level "high" (firmware
    # full pull) / "default" (light ~35% pull, the program default) / "low";
    # the Options-tab gradual slider stores an int analog threshold (see
    # _sc_actuation_threshold) between the high/low endpoints.
    "sc_osk_trigger_actuation": "default",
    # L2/R2 DESKTOP MOUSE left/right-click actuation  a SEPARATE setting from
    # the OSK one above (same named levels / int threshold format).
    "sc_mouse_trigger_actuation": "default",
    # L2/R2 GAMEPAD-mode actuation  the analog pull at which the triggers count
    # as pressed for the virtual Xbox pad, so a trigger REBOUND to a digital
    # button/keyboard action in the Gamepad tab fires at this pull instead of only
    # the firmware full-pull bit. SEPARATE setting (same format as the two above).
    "sc_gamepad_trigger_actuation": "default",
    # Right-stick mouse pointer speed. Tray menu stores a named level "low" /
    # "medium" (default) / "high"; the Options-tab gradual slider stores a float
    # multiplier (see _sc_speed_mult) anchored to those same endpoints.
    "sc_pointer_speed": "medium",
    # SC desktop-takeover trackpad speeds (tray "Steam Controller" submenu).
    # Right trackpad → cursor, left trackpad → scroll wheel; firmware lizard is
    # off in desktop mode now, so our _Watcher drives the pads directly and these
    # scale its sensitivity. "low" / "medium" (default) / "high".
    "sc_trackpad_speed": "medium",
    "sc_scroll_speed": "medium",
    # Left-trackpad scroll style (Options → Touchpads): "normal" = direct 1:1
    # wheel notches only; "laptop" = a quick swipe also sets the page coasting
    # with a smooth deceleration (kinetic scrolling), caught with a gentle tap;
    # "wheel" = the left pad becomes a circular scroll dial (clockwise down,
    # counter-clockwise up, one notch per WHEEL_STEP_DEG, haptic tick per notch);
    # "wheel_smooth" = the same dial as a continuous hi-res analog glide (no ticks).
    "sc_scroll_mode": "wheel",
    # "Invert Scrolling" (Options → Touchpads scroll-settings cog): flips the
    # scroll direction for ALL scroll modes.
    "sc_scroll_invert": False,
    # "Text Wheel Selection" (Options → Touchpads): while the LEFT mouse button
    # is held over text, circling a thumb on the left pad nudges the cursor a
    # few pixels per detent so the live drag-selection extends roughly one
    # character at a time (the app snaps to character boundaries)  clockwise
    # forward, ccw back. Polled per input frame in the desktop takeover.
    "text_wheel_selection": False,
    # "Video Timeline Scrubbing" (Options → Touchpads): while a video is
    # focused (YouTube for now), the left trackpad becomes a circular timeline
    # dial  clockwise scrubs forward, counter-clockwise back. "off" / "frame"
    # (precise, pauses per-frame) / "seek" (fast 5s-per-detent, no pause).
    "video_scrub": "off",
    # Switch Pro Controller submenu (shown only while a Switch Pro / SDL pad
    # is connected): pointer speed only (no trigger actuation).
    "switch_pointer_speed": "medium",
    # Which controller most recently drove the on-screen keyboard: "sc" (Steam
    # Controller) or an SDL pad kind from the pads.py catalog ("switch",
    # "xbox", "ps5", ...  the legacy "sdl" value is accepted as "switch").
    # Picks which Shift/Enter trigger glyphs the OSK shows. A string, not a
    # bool. Updated live as each controller is used and persisted so the
    # glyphs match the last-used pad on the next open  even after a reboot.
    "last_osk_controller": "sc",
    # Controller kinds (pads.py catalog) that have EVER been detected. Each
    # detection permanently unlocks that controller's tab in the picker's top
    # bar and its Options category (the Steam Controller is always unlocked).
    # A list, NOT a bool, so _load_settings passes it through unchanged.
    "seen_controllers": [],
    # PER-CONTROLLER OSK function → button maps ({kind: {func: control_id}}) 
    # each controller's Options category has its own Caps/Shift/Enter/Space/
    # Backspace dropdowns. The legacy flat "osk_buttons" doubles as the Steam
    # Controller's map when it has no entry here. A dict, NOT a bool.
    "osk_buttons_by_kind": {},
    # Per-controller keybind layout (tray "Keybinds" picker). Maps each control
    # id to an action value, under "sc" / "switch" sub-dicts; {} means "use the
    # built-in defaults" (keybinds_picker.default_binds). A nested dict, NOT a
    # bool, so _load_settings passes it through unchanged. Editing it currently
    # saves the layout; wiring it into the live input path is a follow-up.
    "keybinds": {},
    # Up to _MAX_KEYBIND_PROFILES saved keybind profiles (the picker footer's
    # 1-N buttons). Each slot ("1".."10", sparse) holds a full "keybinds"-shaped
    # nested map covering the Desktop / Gamepad / Chords layout tabs; Options
    # settings are NOT part of a profile. "keybinds" above always MIRRORS the
    # active slot, so every runtime consumer keeps reading the one canonical
    # place.
    "keybind_profiles": {},
    # The active profile slot PER LAYOUT TAB  Desktop (pc) / Gamepad (gamepad)
    # / Chords (guide) each pick their own slot 1-10, so the live "keybinds"
    # mirror takes each mode's binds from that mode's selected slot.
    "keybind_profile": {"pc": 1, "gamepad": 1, "guide": 1},
    # How many profile slots currently EXIST, PER LAYOUT TAB (the picker footer's
    # 1..N buttons + a "+" to add up to _MAX_KEYBIND_PROFILES). Each tab (Desktop
    # / Gamepad / Chords) owns its own count, so the "+" grows only the visible
    # tab and a right-click deletes a slot from just that tab. Fresh installs
    # start with a single slot each; the "Profile Cycle" actions wrap within
    # that tab's count.
    "keybind_profile_count": {"pc": 1, "gamepad": 1, "guide": 1},
    # The NAME the user typed for each profile slot, per (controller, layout
    # tab, slot): {kind: {"pc"|"gamepad"|"guide": {"1": "Photoshop", ...}}}.
    # Written by the picker footer's name box (live, not on Save); an absent or
    # empty name means the slot shows the generic "<Tab> Profile" placeholder.
    # Purely cosmetic  nothing in the input path reads it except the profile-
    # cycle toast, which names the slot it switched to.
    "keybind_profile_names": {},
    # Two-button desktop chords for the Steam Controller (tray "Keybinds" picker,
    # Chords editor). A list of {"buttons": ["a","b"], "type": "keys",
    # "keys": ["ctrl","alt","i"]} or {..., "type": "launch", "path": "...",
    # "args": "..."}. Fired by tray's _Watcher when both buttons are held. A
    # list, NOT a bool, so _load_settings passes it through unchanged.
    # Also home to the per-controller "Gyro To Mouse" hotkeys
    # ({"type":"gyro_toggle"}), which the gyro cog modal owns  every gyro-
    # capable kind is seeded with the default both-thumbsticks one on first
    # load (see _seed_default_gyro_chords).
    "chords": [],
    # One-time marker for that seeding. False = the default L3 + R3 "Gyro To
    # Mouse" hotkey hasn't been planted yet (fresh install, or a settings file
    # from before it existed). Once it's True a user who DELETES the hotkey
    # keeps it deleted.
    "gyro_defaults_seeded": False,
    # True once the one-time "Steam detected" / "SteamlessInput paused" toast
    # has been shown (see steam_watch_thread)  never shown again after that,
    # even across restarts.
    "steam_pause_toast_shown": False,
    # --- Options → Big Picture (big_picture.py engine) ---
    # Open Big Picture when a controller connects: "off" / "steam" (only while
    # Steam is already running) / "always" (starts Steam too).
    "bp_auto_launch": "off",
    # Close Big Picture when the LAST controller disconnects (skipped while a
    # game is running  the steamapps/common process guard).
    "bp_auto_close": False,
    # Options → Sleep Manager master toggle (imported from the user's
    # SleepManager.bat). Default OFF: the page's controls alter the Windows
    # power configuration (powercfg), so the user must arm them explicitly 
    # the picker shows a confirmation popup explaining that when this is
    # turned back off everything is restored to the values captured below.
    "sleep_manager": False,
    # powercfg snapshot taken the moment the master toggle was enabled (see
    # sleep_snapshot). Written back by sleep_restore when the toggle turns
    # off, then cleared. {} = no snapshot held. A dict, NOT a bool, so
    # _load_settings passes it through unchanged.
    "sleep_manager_snapshot": {},
    # UI memory for the Sleep Manager page: the "Use Sleep Mode" dropdown's
    # choice ("sleep" standby only / "sleep_hib" standby + auto-hibernate /
    # "s4" pure hibernate / "hybrid" S3+S4 / "off" = Disable All Sleep) and
    # each slider's minutes ("standby" = the Sleep Timeout slider, shared by
    # sleep / sleep_hib / hybrid; "hibernate" = the Hibernate Timeout slider,
    # shared by s4 / sleep_hib). Windows can't represent "disabled but
    # remembered" (Disable All Sleep zeroes every timer), so the controls
    # reseed from these when the live powercfg state doesn't carry them.
    "sleep_last_mode": "sleep",
    "sleep_monitor_min": 30,
    "sleep_standby_min": 30,
    "sleep_hibernate_min": 60,
}

# "Trackpad Keyboard Typing Mode" → the three adusk flags it switches on, as
# (release_to_type, touch_typing, swipe_typing). The runtime still reads three
# independent booleans (adusk.state / adusk.controller); this table is the only
# place the single-select mode is turned back into them.
#
#   default  the Steam Controller's own scheme: the pads glide a smoothed
#             pointer over its own half of the board and you enter a key by
#             clicking the pad or pulling L2/R2.
#   release  same glide, but LIFTING off types the hovered key.
#   touch    each pad becomes a fixed 1:1 map of its half: a fresh touch lands
#             the pointer where the thumb did instead of gliding in. Lift-to-type
#             comes with it (handle_pad_input treats touch_typing as implying
#             release_to_type  a tap on glass has to enter the key).
#   swipe    trace through a word and lift to type the whole word. Carries
#             touch_typing because a trace must START under the thumb, and
#             widens BOTH pads to the whole keyboard (a word needs the full
#             board), which is why it is the mode that supersedes `touch`.
TYPING_MODE_FLAGS = {
    "default": (False, False, False),
    "release": (True,  False, False),
    "touch":   (False, True,  False),
    "swipe":   (False, True,  True),
}


def typing_mode_flags(mode):
    """(release_to_type, touch_typing, swipe_typing) for a stored mode name,
    falling back to the Steam Controller default for anything unrecognized."""
    return TYPING_MODE_FLAGS.get(mode, TYPING_MODE_FLAGS["default"])


# Foreground processes that legitimately run fullscreen but aren't games.
_NON_GAME_FULLSCREEN = {
    "explorer.exe",
    "searchapp.exe",
    "searchui.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "applicationframehost.exe",
    "lockapp.exe",
    "logonui.exe",
    "dwm.exe",
    "textinputhost.exe",
    "steam.exe",
    "steamwebhelper.exe",
    "steamcontrollerkeyboard.exe",
}

# Image / video / document viewers people commonly fullscreen but which aren't
# games. (Browsers and media players are covered by _NON_GAME_INPUT_USERS,
# which the fullscreen check also consults  see _foreground_game_pid.)
_NON_GAME_VIEWERS = {
    # Windows Photos / Photo Viewer
    "microsoft.photos.exe", "photos.exe", "windowsphotoviewer.exe",
    # third-party image viewers
    "irfanview.exe", "i_view64.exe", "i_view32.exe",
    "nomacs.exe", "imageglass.exe", "honeyview.exe", "jpegview.exe",
    "xnview.exe", "xnviewmp.exe", "fsviewer.exe", "qimgv.exe",
    # PDF / document viewers
    "acrobat.exe", "acrord32.exe", "sumatrapdf.exe", "foxitpdfreader.exe",
}

# Foreground window CLASS names registered by the major game engines/frameworks.
# A strong POSITIVE game signal: catches windowed indie games that come from no
# storefront, sit in no "games" folder, and whose anticheat denies the DLL scan.
# Checked on the FOREGROUND window only (GetClassNameW  one cheap Win32 call).
# Lowercase for comparison.
_GAME_WINDOW_CLASSES = {
    "unitywndclass",                 # Unity
    "unrealwindow",                  # Unreal Engine 4/5
    "launchunrealuwindowsclient",    # older Unreal
    "sdl_app",                       # SDL2/SDL3 (also FNA/MonoGame)
    "glfw30",                        # GLFW (many GL/Vulkan indies)
    "valve001",                      # Source engine
    "rgss player",                   # RPG Maker XP/VX
    "cryengine",                     # CryEngine
    "d3dproxywindow",                # various D3D titles
    "yygamemakeryy",                 # GameMaker Studio
}

# Known game-store / launcher executables. A process whose parent is one of
# these is treated as a likely game, regardless of windowing mode.
_GAME_LAUNCHERS = {
    "steam.exe",
    "epicgameslauncher.exe",
    "galaxyclient.exe",
    "eadesktop.exe",
    "origin.exe",
    "battle.net.exe",
    "upc.exe",
    "ubisoftconnect.exe",
    "rockstargameslauncher.exe",
    "amazongameslauncher.exe",
    "itch.exe",
    "playniteui.exe",
}

# Apps that load XInput / DirectInput for legitimate non-game reasons (PTT,
# remapping, recording). Without this list the XInput-DLL heuristic would
# false-trigger on them. Process-name (basename, lowercase).
_NON_GAME_INPUT_USERS = {
    # Browsers (some implement Gamepad API which dlopens xinput)
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "opera.exe", "opera_gx.exe", "vivaldi.exe", "iexplore.exe",
    "chromium.exe", "arc.exe", "waterfox.exe", "librewolf.exe",
    "floorp.exe", "thorium.exe", "palemoon.exe", "zen.exe",
    # Chat / voice (gamepad-as-PTT features)
    "discord.exe", "discordcanary.exe", "discordptb.exe",
    "slack.exe", "teams.exe", "ms-teams.exe", "zoom.exe", "skype.exe",
    # IDEs / dev tools
    "code.exe", "code - insiders.exe", "devenv.exe",
    "idea64.exe", "pycharm64.exe", "rider64.exe", "webstorm64.exe",
    "clion64.exe", "goland64.exe", "phpstorm64.exe",
    # Controller / remapper utilities
    "ds4windows.exe", "x360ce.exe", "joytokey.exe", "rewasd.exe",
    "controllercompanion.exe", "steaminput.exe",
    # Media
    "spotify.exe", "vlc.exe", "mpc-hc.exe", "mpc-hc64.exe",
    "mpc-be.exe", "mpc-be64.exe", "obs64.exe", "obs32.exe",
    "mpv.exe", "potplayer64.exe", "potplayermini64.exe",
    "potplayermini.exe", "kodi.exe", "wmplayer.exe",
    "plex.exe", "plex htpc.exe", "jellyfin media player.exe",
    # Remote desktop (fullscreen sessions aren't local games)
    "mstsc.exe", "teamviewer.exe", "anydesk.exe", "rustdesk.exe",
    "vncviewer.exe", "chrome remote desktop host.exe",
    # Office
    "winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe",
    "onenote.exe",
}

# Common desktop apps that are neither shell/system nor known input-DLL users
# but still end up fullscreen/foreground (F11 terminals and editors, chat,
# system utilities, Wallpaper Engine's renderer). Vetoed by game DETECTION
# only  unlike _NON_GAME_FULLSCREEN they remain valid targets for the
# explicit force-kill chord.
_NON_GAME_APPS = {
    # Terminals / consoles
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "alacritty.exe", "wezterm-gui.exe",
    "hyper.exe", "tabby.exe",
    # Editors / notes
    "notepad.exe", "notepad++.exe", "sublime_text.exe", "obsidian.exe",
    "wordpad.exe",
    # Chat / social
    "telegram.exe", "whatsapp.exe", "signal.exe", "element.exe",
    # System utilities
    "taskmgr.exe", "regedit.exe", "mmc.exe", "eventvwr.exe",
    "perfmon.exe", "resmon.exe", "procexp.exe", "procexp64.exe",
    "processhacker.exe", "systeminformer.exe",
    # Wallpaper Engine renderers: Steam-launched, cover the monitor AND load
    # input DLLs  they match every game heuristic while never being a game.
    "wallpaper32.exe", "wallpaper64.exe",
    # Installers
    "msiexec.exe", "setup.exe", "unins000.exe",
}

# DLL basename prefixes that indicate a game runtime is loaded in a process:
# gamepad-input stacks, storefront SDKs (steam_api ships in virtually every
# Steam game, DRM-free included), engine runtimes and game-audio middleware.
# Used as corroborating evidence  a fullscreen window plus any of these is a
# game; a browser/terminal/editor loads none of them.
_GAME_DLL_PREFIXES = (
    "xinput", "dinput8", "xgameruntime", "gameinput",         # gamepad input
    "steam_api", "eossdk", "galaxy",                          # storefront SDKs
    "unityplayer", "gameassembly",                            # Unity runtime
    "xaudio2", "fmod", "openal", "aksoundengine", "cri_ware", # game audio
)

# Our own PID  never treat ourselves as a game regardless of which DLLs
# SDL3 or ViGEm pull in (xinput appears in our address space too).
_OWN_PID = os.getpid()

# Helper processes spawned by the launchers themselves  these have launcher
# parents AND visible windows, so we need to explicitly exclude them.
_LAUNCHER_HELPERS = {
    # Steam
    "steamwebhelper.exe", "steamservice.exe", "gameoverlayui.exe",
    "streaming_client.exe", "vrserver.exe", "vrcompositor.exe",
    "vrdashboard.exe", "vrmonitor.exe", "vrstartup.exe",
    "html5app_steam.exe", "crashhandler.exe",
    # Epic
    "epicwebhelper.exe", "epiconlineservices.exe",
    "epiconlineservicesuihelper.exe", "epiconlineservicesinstaller.exe",
    # GOG
    "galaxyclient helper.exe", "galaxycommunication.exe",
    "galaxyoverlay.exe",
    # EA / Origin
    "eabackgroundservice.exe", "originwebhelperservice.exe",
    "ealink.exe",
    # Battle.net
    "battle.net helper.exe", "agent.exe",
    # Ubisoft
    "upcwebbrowser.exe", "upcrenderinghost.exe",
}


# --- Steam-running detection ------------------------------------------------

def _steam_running():
    """True if a steam.exe process is currently running."""
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(attrs=["name"]):
        try:
            if (proc.info.get("name") or "").lower() == STEAM_PROC_NAME:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# --- Foreground-game detection ----------------------------------------------

class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _pids_with_visible_windows():
    """PIDs that own at least one visible top-level window with a non-empty
    title. Used to filter out background-only processes (services, helpers)
    when scanning for launcher-child games."""
    result = set()
    user32 = ctypes.windll.user32

    def cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                result.add(pid.value)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_ENUM_WINDOWS_PROC(cb), 0)
    except Exception:
        return set()
    return result


def _window_covers_monitor(hwnd):
    """True if `hwnd` covers its monitor  within a few px of the exact rect
    (DPI-virtualization / borderless quirks used to fail the old EXACT-equality
    test) or extending past it (oversized borderless / spanning windows)."""
    try:
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return False
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        tol = 4
        return (rect.left <= mi.rcMonitor.left + tol
                and rect.top <= mi.rcMonitor.top + tol
                and rect.right >= mi.rcMonitor.right - tol
                and rect.bottom >= mi.rcMonitor.bottom - tol)
    except Exception:
        return False


def _window_fullscreen(hwnd):
    """TRUE fullscreen  a borderless window covering its monitor (F11 /
    YouTube's fullscreen player)  as opposed to a MAXIMIZED window
    (IsZoomed), which also covers the monitor but is still a normal page
    with the video somewhere inside it. Video Timeline Scrubbing must treat
    maximized as windowed: the progress bar is NOT at the window bottom and
    the cursor-on-video gate still applies."""
    try:
        if ctypes.windll.user32.IsZoomed(hwnd):
            return False
    except Exception:
        pass
    return _window_covers_monitor(hwnd)


def _window_class(hwnd):
    """Lowercased window class name of `hwnd`, or ''. One cheap Win32 call."""
    try:
        buf = ctypes.create_unicode_buffer(128)
        if ctypes.windll.user32.GetClassNameW(hwnd, buf, 128):
            return buf.value.lower()
    except Exception:
        pass
    return ""


def _resolve_uwp_pid(hwnd, pid):
    """UWP / GamePass apps host their content inside ApplicationFrameHost's
    frame window; the actual app owns a Windows.UI.Core.CoreWindow CHILD of
    it. When the foreground pid is the frame host, return the child window's
    pid so detection and focus tracking see the real game (Minecraft,
    GamePass titles) instead of the vetoed shell host."""
    try:
        import psutil
        if (psutil.Process(pid).name() or "").lower() != \
                "applicationframehost.exe":
            return pid
    except Exception:
        return pid
    try:
        u = ctypes.windll.user32
        child = u.FindWindowExW(ctypes.c_void_p(hwnd), None,
                                "Windows.UI.Core.CoreWindow", None)
        if child:
            cpid = wintypes.DWORD()
            u.GetWindowThreadProcessId(child, ctypes.byref(cpid))
            if cpid.value:
                return cpid.value
    except Exception:
        pass
    return pid


def _foreground_game_pid():
    """Return the PID of a likely-game foreground window, or None.

    Positive signals (blocklists always veto):
      * the window CLASS is a known game-engine class (_GAME_WINDOW_CLASSES) 
        catches WINDOWED games that come from no storefront, live in no games
        folder, and whose anticheat denies the DLL scan;
      * TRUE fullscreen (covers its monitor and is not merely maximized) AND
        corroborating game evidence (_game_evidence: games-dir path,
        storefront launcher ancestor, game-runtime DLLs, or an anticheat-
        protected process).
    Bare fullscreen alone no longer latches: any F11'd app the blocklists
    didn't happen to name, and maximized windows over an auto-hidden taskbar,
    were the main false-switch sources  blocklists can't enumerate the
    world, so unknown fullscreen apps now need real game evidence."""
    try:
        import psutil
    except ImportError:
        return None

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        engine_class = _window_class(hwnd) in _GAME_WINDOW_CLASSES
        fullscreen = _window_fullscreen(hwnd)
        if not (engine_class or fullscreen):
            return None

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pidv = _resolve_uwp_pid(hwnd, pid.value) if pid.value else 0
        if not pidv or pidv == _OWN_PID:
            return None

        try:
            proc = psutil.Process(pidv)
            name = (proc.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        # Shell/system, viewers (image/video/PDF), known non-game input users
        # (browsers, media players, chat, dev tools) and common desktop apps
        # can all cover the whole monitor without being a game.
        if (name in _NON_GAME_FULLSCREEN
                or name in _NON_GAME_VIEWERS
                or name in _NON_GAME_INPUT_USERS
                or name in _NON_GAME_APPS):
            return None
        if engine_class:
            return pidv
        return pidv if _game_evidence(proc) else None
    except Exception:
        return None


def _foreground_window_kill_pid():
    """PID of the foreground window's process for the EXPLICIT Home+B force-kill
    chord  like _foreground_game_pid but WITHOUT the fullscreen requirement, so
    it also closes WINDOWED games (the user deliberately asked to kill whatever
    is in front). Still refuses to target the shell/system, Steam, or our own
    process so the desktop / launcher can't be killed by accident."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # Resolve UWP frame-host windows to the hosted app so the chord kills
        # the actual game, not (refusing to kill) ApplicationFrameHost.
        pidv = _resolve_uwp_pid(hwnd, pid.value) if pid.value else 0
        if not pidv or pidv == os.getpid():
            return None
        try:
            name = (psutil.Process(pidv).name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        # Never kill the shell/system/Steam/our own app (they'd break the
        # desktop or the launcher). Other foreground apps ARE fair game  the
        # user explicitly pressed the chord to kill what's in front.
        if name in _NON_GAME_FULLSCREEN:
            return None
        return pidv
    except Exception:
        return None


# Path segment names (case-insensitive) that indicate a game install dir.
# Detection: split the exe path on / and \ and check if any segment matches.
# Catches both storefront install layouts (steamapps/, "Epic Games/") and
# common user-organized folders ("Games", "My Games", etc.).
_GAME_DIR_NAMES = {
    # Storefront install roots
    "steamapps",
    "epic games",
    "gog games", "gog galaxy",
    "ea games", "origin games",
    "ubisoft", "uplay",
    "battle.net",
    "amazon games",
    "riot games",
    "itch.io", "itch",
    "playnite",
    # User-organized game folders
    "games", "game",
    "my games", "pc games", "steam games", "portable games",
    "emulators",
}


def _exe_in_game_dir(exe_path):
    """True if any segment of `exe_path` is a recognized games-folder name."""
    if not exe_path:
        return False
    norm = exe_path.lower().replace("\\", "/")
    for seg in norm.split("/"):
        if seg in _GAME_DIR_NAMES:
            return True
    return False


def _foreground_pid():
    """PID of the process owning the current foreground window (0 if none),
    with UWP frame-host windows resolved to the hosted app  so focus
    tracking follows the actual GamePass/Store game, not the shell host."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return 0
        return _resolve_uwp_pid(hwnd, pid.value)
    except Exception:
        return 0


def _ancestor_pids(pid, max_depth=6):
    """Yield ancestor PIDs of `pid`, starting at its direct parent."""
    try:
        import psutil
    except ImportError:
        return
    try:
        current = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for _ in range(max_depth):
        try:
            current = current.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        if current is None:
            return
        yield current.pid


def _is_latched_focused(latched_pid):
    """True if the foreground window belongs to `latched_pid` or one of its
    descendants (handles games whose foreground window is a child process)."""
    fp = _foreground_pid()
    if not fp:
        return False
    if fp == latched_pid:
        return True
    for ancestor_pid in _ancestor_pids(fp):
        if ancestor_pid == latched_pid:
            return True
    return False


def _process_loads_game_dll(proc):
    """Scan `proc`'s mapped DLLs for game-runtime signatures (gamepad input,
    storefront SDKs, engine runtimes, game-audio middleware). Returns
    True/False, or None when the process refuses even OUR handle
    (memory_maps access denied): we run elevated, ordinary apps can't deny
    us  kernel-anticheat-protected games do, so a refusal is itself a
    usable game signal for the caller."""
    try:
        import psutil
    except ImportError:
        return False
    try:
        maps = proc.memory_maps()
    except psutil.AccessDenied:
        return None
    except Exception:
        # NotImplemented, NoSuchProcess, etc.  be silent and let the caller
        # fall back to other heuristics.
        return False
    for mm in maps:
        path = (getattr(mm, "path", "") or "").lower().replace("\\", "/")
        if not path:
            continue
        base = path.rsplit("/", 1)[-1]
        for prefix in _GAME_DLL_PREFIXES:
            if base.startswith(prefix):
                return True
    return False


def _game_evidence(proc):
    """Non-window evidence that `proc` is a game  corroborates a fullscreen
    window and detects windowed games. Checks cheapest-first: exe path in a
    games folder, a storefront launcher in the parent chain, then mapped
    game-runtime DLLs (the memory scan being DENIED counts too  see
    _process_loads_game_dll). Returns a reason string, or None."""
    exe = ""
    try:
        exe = proc.exe() or ""
    except Exception:
        pass
    if _exe_in_game_dir(exe):
        return "game-dir-path"
    for _depth, an in _ancestor_names(proc):
        if an in _GAME_LAUNCHERS:
            return "launcher-ancestor:" + an
    loaded = _process_loads_game_dll(proc)
    if loaded is True:
        return "loads-game-dll"
    if loaded is None:
        return "dll-scan-denied(protected)"
    return None


def _ancestor_names(proc, max_depth=6):
    """Yield (depth, name_lower) for each ancestor of `proc`, starting at
    its direct parent (depth=1). Stops on permission errors or root."""
    try:
        import psutil
    except ImportError:
        return
    current = proc
    for depth in range(1, max_depth + 1):
        try:
            current = current.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        if current is None:
            return
        try:
            yield depth, (current.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return


def _launched_game_pid(debug_log=None):
    """Return the PID of a FOREGROUND-CHAIN process that looks like a
    launched game (storefront parent, game-library path, or game-runtime
    DLLs  see _game_evidence) and owns at least one visible window. Catches
    windowed games that _foreground_game_pid() misses.

    Only the focused process and its ancestors are considered. Detection
    used to trust ANY visible window, which let always-running storefront
    children (Wallpaper Engine, installers, overlays) latch without ever
    being focused  and a wrong background latch also BLOCKED the real game
    from latching. Auto-gamepad only activates while the latched game is
    focused anyway, so gating detection on focus costs nothing: a game
    that hasn't taken focus yet is picked up by the rescan its focus
    change triggers.

    If `debug_log` is a writable file, dump per-process diagnostic info so
    the user can see why detection did or did not fire."""
    try:
        import psutil
    except ImportError:
        return None

    fg_pid = _foreground_pid()
    if not fg_pid:
        return None
    fg_chain = {fg_pid}
    fg_chain.update(_ancestor_pids(fg_pid))

    visible = _pids_with_visible_windows()
    if not visible:
        if debug_log:
            debug_log.write("  (no visible top-level windows)\n")
        return None
    if debug_log:
        debug_log.write(f"  fg-chain: {sorted(fg_chain)}\n")

    candidates = []  # (create_time, pid)  newer wins
    for proc in psutil.process_iter(attrs=["pid", "name", "ppid", "create_time"]):
        try:
            info = proc.info
            pid = info.get("pid")
            if pid is None or pid not in fg_chain or pid not in visible:
                continue
            if pid == _OWN_PID:
                if debug_log:
                    debug_log.write(f"  skip pid={pid} (own process)\n")
                continue
            name = (info.get("name") or "").lower()
            if name in _NON_GAME_FULLSCREEN or name in _LAUNCHER_HELPERS:
                if debug_log:
                    debug_log.write(f"  skip pid={pid} {name} (helper/system)\n")
                continue
            if name in _GAME_LAUNCHERS:
                if debug_log:
                    debug_log.write(f"  skip pid={pid} {name} (launcher itself)\n")
                continue
            # Known non-game apps are excluded from EVERY signal here, not just
            # the DLL one  a browser/Discord opened FROM Steam used to match
            # "launcher-ancestor:steam.exe" and latch as a game.
            if (name in _NON_GAME_INPUT_USERS or name in _NON_GAME_VIEWERS
                    or name in _NON_GAME_APPS):
                if debug_log:
                    debug_log.write(f"  skip pid={pid} {name} (known non-game)\n")
                continue

            match_reason = _game_evidence(proc)

            if debug_log:
                tag = f"MATCH({match_reason})" if match_reason else "no-match"
                debug_log.write(f"  fg-chain pid={pid} name={name} {tag}\n")

            if match_reason:
                candidates.append((info.get("create_time", 0.0), pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _foreground_hwnd():
    """Current foreground window handle as an int (0 if none). Cheap (one Win32
    call)  used to skip the heavy game scan while the foreground is unchanged."""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        return int(u.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _foreground_title():
    """Foreground window title text ("" if none). Cheap (two Win32 calls) 
    used by the Video Timeline Scrubbing focus check: browser tabs put the
    site name in the window title (e.g. "... - YouTube - Google Chrome")."""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 512)
        return buf.value or ""
    except Exception:
        return ""


def _foreground_rect():
    """Foreground window rect as (left, top, right, bottom), or None. Used by
    the Video Timeline Scrubbing hover mode to estimate where the video's
    progress bar sits on screen."""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None


def _scan_progress_playhead(x0, x1, bar_y):
    """Locate YouTube's playhead on screen. Captures a TALL strip around the
    estimated progress-bar line (tolerant of layout/DPI drift in the 68px
    guess) and finds the played-portion fill by its signature: a horizontal
    run of strongly-RED pixels ANCHORED AT THE BAR'S LEFT EDGE. YouTube draws
    the played fill + scrubber knob in pure red on every theme; the grey
    buffered/"cache" segment ahead of the playhead can never match the red
    test, so the fill's right edge IS the playhead. Returns (playhead_x,
    bar_row_y)  the row so the caller can correct its bar-height estimate
    too  or None (controls hidden / nothing matches).

    Two passes keep pure Python fast: (1) find candidate bar ROWS by checking
    only the leftmost 60px (the played fill always starts at the bar's left
    edge; the knob covers it even at 0:00), (2) walk just those rows
    right→left for the rightmost red pixel. That edge is the knob's right
    arc, so half a knob is subtracted to land on the knob's center."""
    try:
        w = int(x1 - x0)
        top = int(bar_y) - 72   # strip covers estimate ±(72/48)  generous
        h = 120
        if w <= 60:
            return None
        u = ctypes.windll.user32
        gdi = ctypes.windll.gdi32
        sdc = u.GetDC(None)
        if not sdc:
            return None
        mdc = bmp = None
        try:
            mdc = gdi.CreateCompatibleDC(sdc)
            bmp = gdi.CreateCompatibleBitmap(sdc, w, h)
            gdi.SelectObject(mdc, bmp)
            if not gdi.BitBlt(mdc, 0, 0, w, h, sdc,
                              int(x0), top, 0x00CC0020):
                return None

            class _BIH(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD),
                            ("biWidth", ctypes.c_long),
                            ("biHeight", ctypes.c_long),
                            ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long),
                            ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]

            bih = _BIH()
            bih.biSize = ctypes.sizeof(_BIH)
            bih.biWidth = w
            bih.biHeight = -h  # negative = top-down row order
            bih.biPlanes = 1
            bih.biBitCount = 32
            bih.biCompression = 0
            buf = (ctypes.c_ubyte * (w * h * 4))()
            if not gdi.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0):
                return None

            # 32bpp DIB byte order is B,G,R,X. Strict red so video content
            # bleeding through the control overlay can't fake a playhead.
            def _red(i):
                return (buf[i + 2] >= 190 and buf[i + 1] <= 70
                        and buf[i] <= 70)

            # Pass 1: candidate bar rows = rows with ≥3 red px in the
            # leftmost 60 (the played fill / knob at the bar's left edge).
            cand = []
            for row in range(h):
                base = row * w * 4
                hits = 0
                for col in range(60):
                    if _red(base + col * 4):
                        hits += 1
                        if hits >= 3:
                            cand.append(row)
                            break
                if len(cand) >= 24:
                    break  # bar + knob span ~20 rows max; cap the pass-2 cost
            if not cand:
                return None
            # Pass 2: rightmost red across those rows = playhead edge.
            best_col = best_row = None
            for row in cand:
                base = row * w * 4
                for col in range(w - 1, -1, -1):
                    if _red(base + col * 4):
                        if best_col is None or col > best_col:
                            best_col, best_row = col, row
                        break
            if best_col is None:
                return None
            # The rightmost red is the hover-enlarged knob's right arc;
            # its CENTER is the playhead  pull back half a knob.
            px = max(int(x0), int(x0) + best_col - 7)
            return px, top + best_row
        finally:
            if bmp:
                gdi.DeleteObject(bmp)
            if mdc:
                gdi.DeleteDC(mdc)
            u.ReleaseDC(None, sdc)
    except Exception:
        return None


# Strongly-red pixel in 32bpp DIB byte order (B,G,R,X): B≤0x37, G≤0x37,
# R≥0xC8. Compiled once; finditer runs the hunt at C speed over the whole
# capture. A byte-class regex (not an exact #FF0000 find) because DPI
# virtualization / display scaling blends the thin bar's colors slightly 
# the exact match came back empty on scaled displays.
_RED_PX_RE = re.compile(rb"[\x00-\x37][\x00-\x37][\xc8-\xff]")


def _scan_playhead_windowed(l, t, r, b):
    """Find YouTube's playhead in a WINDOWED player, where the progress bar
    sits at the bottom of the video ELEMENT  somewhere unknown inside the
    window  rather than near the window's bottom edge. Searches the whole
    window for the played-fill's signature: a THIN horizontal run of
    strongly-red pixels (the UI-drawn fill/knob; _RED_PX_RE does the hunt at
    C speed). The masthead's red YouTube logo is excluded by ignoring the
    top 120px; big red video frames are rejected by a thin-ness check (the
    fill+knob span ~20 rows, a frame spans hundreds). Returns
    (playhead_x, bar_y, bar_left_x) in screen coords, or None."""
    try:
        w = int(r - l)
        h = min(int(b - t), 2400)
        if w < 200 or h < 200:
            return None
        u = ctypes.windll.user32
        gdi = ctypes.windll.gdi32
        sdc = u.GetDC(None)
        if not sdc:
            return None
        mdc = bmp = None
        try:
            mdc = gdi.CreateCompatibleDC(sdc)
            bmp = gdi.CreateCompatibleBitmap(sdc, w, h)
            gdi.SelectObject(mdc, bmp)
            if not gdi.BitBlt(mdc, 0, 0, w, h, sdc,
                              int(l), int(t), 0x00CC0020):
                return None

            class _BIH(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD),
                            ("biWidth", ctypes.c_long),
                            ("biHeight", ctypes.c_long),
                            ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long),
                            ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]

            bih = _BIH()
            bih.biSize = ctypes.sizeof(_BIH)
            bih.biWidth = w
            bih.biHeight = -h  # negative = top-down row order
            bih.biPlanes = 1
            bih.biBitCount = 32
            bih.biCompression = 0
            buf = (ctypes.c_ubyte * (w * h * 4))()
            if not gdi.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0):
                return None
            raw = bytes(buf)
        finally:
            if bmp:
                gdi.DeleteObject(bmp)
            if mdc:
                gdi.DeleteDC(mdc)
            u.ReleaseDC(None, sdc)
        # Hunt strongly-red pixels (4-aligned = the B,G,R of one pixel) and
        # bucket them per row: [min col, max col, count]. Inside a solid red
        # run finditer's non-overlapping stride still lands on every aligned
        # pixel (the X byte can't start a match).
        row_hits = {}
        seen = 0
        for m in _RED_PX_RE.finditer(raw):
            i = m.start()
            if i % 4:
                continue
            seen += 1
            if seen > 300000:
                break
            p = i // 4
            row = p // w
            col = p - row * w
            rh = row_hits.get(row)
            if rh is None:
                row_hits[row] = [col, col, 1]
            else:
                if col < rh[0]:
                    rh[0] = col
                if col > rh[1]:
                    rh[1] = col
                rh[2] += 1
        if not row_hits:
            return None
        # Candidate rows: below the masthead, wide enough to be a fill/knob
        # (≥10px) and mostly CONTIGUOUS (≥60% of the min..max span red 
        # chapter gaps pass, scattered noise fails). Widest first.
        cands = sorted(
            ((n, row) for row, (lo, hi, n) in row_hits.items()
             if row >= 120 and n >= 10 and n >= (hi - lo + 1) * 0.6),
            reverse=True)
        for _n, row in cands[:8]:
            # Cluster shape check, scale-independent: gather the red rows
            # around the candidate. Too tall (>30) = a red video frame.
            # The player bar's cluster includes the round scrubber KNOB,
            # whose top/bottom arc rows are much NARROWER than the fill 
            # that width contrast is the knob's signature. A watched-video
            # THUMBNAIL's red progress strip in the sidebar has uniform-
            # width rows (no knob); grabbing that would click a link, so
            # skip to the next candidate instead.
            cluster = []
            for rr in range(row - 30, row + 31):
                rh = row_hits.get(rr)
                if rh is not None and rh[2] >= 3:
                    cluster.append(rh[2])
            if not (3 <= len(cluster) <= 30):
                continue
            wmax = max(cluster)
            if not any(c <= max(4, wmax * 0.3) for c in cluster):
                continue  # flat strip, no knob-arc rows
            lo, hi, _cnt = row_hits[row]
            # Rightmost red is the knob's right arc → pull back half a knob.
            px = max(int(l) + lo, int(l) + hi - 7)
            return px, int(t) + row, int(l) + lo
        return None
    except Exception:
        return None


# --- Pinch To Zoom (fullscreen Magnification API) -----------------------------
# MagSetFullscreenTransform takes a FLOAT scale + integer source offsets and is
# GPU-composited by DWM  continuous per-frame updates give smooth optical zoom
# (Magnifier's 100% steps are its own hotkey UX, not an API limit). The
# transform auto-reverts if this process dies, so a crash can't strand the
# desktop zoomed; _mag_reset() still restores explicitly on toggle-off/exit.
_mag_ready = False


def _mag_init():
    global _mag_ready
    if _mag_ready:
        return True
    try:
        if ctypes.windll.magnification.MagInitialize():
            _mag_ready = True
    except Exception:
        pass
    return _mag_ready


def _mag_apply(scale, cx, cy):
    """Fullscreen-zoom the desktop to `scale`, centered on desktop point
    (cx, cy). Offsets are the top-left of the UNMAGNIFIED source region, so
    the visible slice is (sw/scale × sh/scale)  clamped to the screen."""
    if not _mag_init():
        return
    try:
        u = ctypes.windll.user32
        sw = u.GetSystemMetrics(0)
        sh = u.GetSystemMetrics(1)
        mag = ctypes.windll.magnification
        if scale <= 1.001:
            mag.MagSetFullscreenTransform(ctypes.c_float(1.0), 0, 0)
            return
        vw = sw / scale
        vh = sh / scale
        x = int(round(min(max(cx - vw / 2.0, 0.0), sw - vw)))
        y = int(round(min(max(cy - vh / 2.0, 0.0), sh - vh)))
        mag.MagSetFullscreenTransform(ctypes.c_float(scale), x, y)
    except Exception:
        pass


def _mag_reset():
    """Restore 1:1 and release the magnification context (toggle-off/exit)."""
    global _mag_ready
    if not _mag_ready:
        return
    try:
        mag = ctypes.windll.magnification
        mag.MagSetFullscreenTransform(ctypes.c_float(1.0), 0, 0)
        mag.MagUninitialize()
    except Exception:
        pass
    _mag_ready = False


# Video Timeline Scrubbing hides the pointer while it drives the cursor
# around (looks like the player is being scrubbed by magic instead of a
# flying mouse). SetSystemCursor is session-GLOBAL, so it MUST be undone 
# _show_system_cursor() reloads the user's cursor scheme from the registry.
_cursor_hidden = False


def _build_red_dot_cursor(size=32, dot_r=6, ring=2):
    """Build a 32x32 HCURSOR: a filled red circle with a white outline ring,
    centered (hotspot = the exact center)  matches the red scrub-dot look
    of YouTube's own timeline knob, and centering the hotspot means the dot
    lands precisely on the position the code drives the cursor to, not
    offset the way an arrow-tip hotspot would be. Transparent everywhere
    else. Returns None on any failure (caller leaves that cursor untouched)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx = cy = size // 2
        # White ring first (drawn larger), red fill on top  reads clearly
        # against light OR dark backgrounds, like the white-rimmed knob
        # YouTube shows on its own red progress bar.
        d.ellipse((cx - dot_r - ring, cy - dot_r - ring,
                  cx + dot_r + ring, cy + dot_r + ring),
                 fill=(255, 255, 255, 255))
        d.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r),
                 fill=(230, 0, 18, 255))

        gdi = ctypes.windll.gdi32
        u = ctypes.windll.user32

        class _BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD),
                        ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long),
                        ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bih = _BIH()
        bih.biSize = ctypes.sizeof(_BIH)
        bih.biWidth = size
        bih.biHeight = -size  # negative = top-down row order
        bih.biPlanes = 1
        bih.biBitCount = 32
        bih.biCompression = 0

        bits_ptr = ctypes.c_void_p()
        hbm_color = gdi.CreateDIBSection(None, ctypes.byref(bih), 0,
                                         ctypes.byref(bits_ptr), None, 0)
        if not hbm_color or not bits_ptr.value:
            return None
        buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits_ptr.value)
        px = img.load()
        for y in range(size):
            for x in range(size):
                r, g, b, a = px[x, y]
                # Premultiplied top-down BGRA (matches biHeight < 0).
                o = (y * size + x) * 4
                buf[o] = (b * a) // 255
                buf[o + 1] = (g * a) // 255
                buf[o + 2] = (r * a) // 255
                buf[o + 3] = a

        mask_bytes = ((size + 15) // 16) * 2 * size  # WORD-aligned rows
        mask_buf = (ctypes.c_ubyte * mask_bytes)()   # all-zero = opaque
        hbm_mask = gdi.CreateBitmap(size, size, 1, 1, ctypes.byref(mask_buf))
        if not hbm_mask:
            gdi.DeleteObject(hbm_color)
            return None

        class _ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL),
                        ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD),
                        ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]

        info = _ICONINFO()
        info.fIcon = 0  # cursor, not icon
        info.xHotspot = size // 2
        info.yHotspot = size // 2
        info.hbmMask = hbm_mask
        info.hbmColor = hbm_color
        cur = u.CreateIconIndirect(ctypes.byref(info))
        gdi.DeleteObject(hbm_color)
        gdi.DeleteObject(hbm_mask)
        return cur if cur else None
    except Exception:
        return None


def _hide_system_cursor():
    """Swap the system arrow + hand cursors for a small red dot (matches
    YouTube's scrub-bar playhead). Video Timeline Scrubbing's "Hover
    preview" mode drives the REAL cursor around the video's progress bar 
    a red dot reads as the timeline scrubbing itself, not a mouse flying
    around. Falls back to a fully invisible cursor if PIL is unavailable
    (matches the old behavior)."""
    global _cursor_hidden
    if _cursor_hidden:
        return
    try:
        u = ctypes.windll.user32
        for ocr in (32512, 32649):  # OCR_NORMAL (arrow), OCR_HAND (link)
            # SetSystemCursor takes ownership/destroys the handle, so each
            # slot needs its OWN cursor built fresh  can't share one HICON.
            cur = _build_red_dot_cursor()
            if cur is None:
                # 32x32 monochrome fallback: AND mask all 1s + XOR all 0s =
                # invisible (the pre-red-dot behavior).
                and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
                xor_mask = (ctypes.c_ubyte * 128)(*([0x00] * 128))
                cur = u.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)
            if cur:
                u.SetSystemCursor(cur, ocr)  # takes ownership of `cur`
        _cursor_hidden = True
    except Exception:
        pass


def _show_system_cursor():
    """Undo _hide_system_cursor by reloading the user's cursor scheme."""
    global _cursor_hidden
    if not _cursor_hidden:
        return
    try:
        SPI_SETCURSORS = 0x0057
        ctypes.windll.user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
    except Exception:
        pass
    _cursor_hidden = False


# --- Brightness Up/Down (Options special actions) ---------------------------
# NATIVE in-process path via the power-scheme API (powrprof.dll): read the
# active scheme's display-brightness index, add the step, write it back (AC+DC)
# and re-apply the scheme  the same plumbing the Windows brightness slider
# uses, takes a few ms and spawns NOTHING. The first implementation launched a
# PowerShell + WMI query PER PRESS (~1s startup each); spamming the bound button
# forked dozens of processes, lagging the whole machine and failing with
# 0xc0000142 process-init errors. A single daemon worker now drains a COALESCED
# pending delta, so N spammed presses collapse into one ±N*10% application, and
# the WMI PowerShell survives only as a synchronous (waited-on, so it can never
# storm) fallback for panels the power-scheme path can't drive.

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        p = s.strip("{}").split("-")
        self.Data1, self.Data2, self.Data3 = (
            int(p[0], 16), int(p[1], 16), int(p[2], 16))
        self.Data4 = (ctypes.c_ubyte * 8)(*bytes.fromhex(p[3] + p[4]))


# Video-settings subgroup + display-brightness setting of a power scheme.
_GUID_VIDEO_SUBGROUP = "7516b95f-f776-4464-8c53-06167f40cc99"
_GUID_VIDEO_BRIGHTNESS = "aded5e82-b909-4619-9949-f5d71dac0bcb"


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32),
                ("BatteryFullLifeTime", ctypes.c_uint32)]


def _brightness_apply_native(delta):
    """Apply a ±percent step through the power-scheme brightness index.
    Returns True on success, False if any call fails (→ caller falls back)."""
    try:
        pp = ctypes.windll.powrprof
        scheme = ctypes.c_void_p()
        if pp.PowerGetActiveScheme(None, ctypes.byref(scheme)):
            return False
        try:
            sub = _GUID(_GUID_VIDEO_SUBGROUP)
            setting = _GUID(_GUID_VIDEO_BRIGHTNESS)
            # Read the index for the CURRENT power source (AC vs battery).
            sps = _SYSTEM_POWER_STATUS()
            ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps))
            on_ac = sps.ACLineStatus == 1
            read = (pp.PowerReadACValueIndex if on_ac
                    else pp.PowerReadDCValueIndex)
            cur = ctypes.c_ulong()
            if read(None, scheme, ctypes.byref(sub), ctypes.byref(setting),
                    ctypes.byref(cur)):
                return False
            n = max(0, min(100, int(cur.value) + delta))
            if n == int(cur.value):
                return True         # already clamped at the rail  nothing to do
            # Write BOTH rails so the level doesn't snap back when the power
            # source flips, then re-apply the scheme to make it take effect.
            if pp.PowerWriteACValueIndex(None, scheme, ctypes.byref(sub),
                                         ctypes.byref(setting), n):
                return False
            pp.PowerWriteDCValueIndex(None, scheme, ctypes.byref(sub),
                                      ctypes.byref(setting), n)
            return pp.PowerSetActiveScheme(None, scheme) == 0
        finally:
            ctypes.windll.kernel32.LocalFree(scheme)
    except Exception:
        return False


def _brightness_apply_wmi(delta):
    """Fallback: one SYNCHRONOUS (waited-on) hidden PowerShell applying the whole
    coalesced delta via the WMI monitor-brightness provider. Blocking the worker
    (never the input loop) guarantees at most ONE process exists at a time."""
    import subprocess
    ps = ("$b=(Get-WmiObject -Namespace root/WMI -Class "
          "WmiMonitorBrightness).CurrentBrightness;"
          "$n=[Math]::Max(0,[Math]::Min(100,[int]$b+(%d)));"
          "(Get-WmiObject -Namespace root/WMI -Class "
          "WmiMonitorBrightnessMethods).WmiSetBrightness(1,$n)") % delta
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    except Exception as e:
        print(f"brightness fallback failed: {e!r}")


_bright_lock = threading.Lock()
_bright_pending = 0          # net ±percent not yet applied (coalesces spam)
_bright_evt = threading.Event()
_bright_thread = None


def _brightness_worker():
    while True:
        _bright_evt.wait()
        _bright_evt.clear()
        while True:
            global _bright_pending
            with _bright_lock:
                delta, _bright_pending = _bright_pending, 0
            if not delta:
                break
            if not _brightness_apply_native(delta):
                _brightness_apply_wmi(delta)


def _brightness_request(step):
    """Queue a ±percent brightness step (called from the input loop  returns
    immediately; the worker coalesces and applies)."""
    global _bright_thread, _bright_pending
    with _bright_lock:
        _bright_pending += step
        if _bright_thread is None or not _bright_thread.is_alive():
            _bright_thread = threading.Thread(
                target=_brightness_worker, daemon=True,
                name="brightness-worker")
            _bright_thread.start()
    _bright_evt.set()


def _detect_game_pid(debug_log=None):
    """Combined detection: fullscreen-foreground (fast), then process-scan
    for launcher-child / game-library-path processes (catches windowed)."""
    pid = _foreground_game_pid()
    if pid:
        if debug_log:
            debug_log.write(f"  foreground-fullscreen MATCH pid={pid}\n")
        return pid
    return _launched_game_pid(debug_log=debug_log)


def _force_kill_foreground_game():
    """Force-shutdown the foreground game, leaving its launcher ('parent')
    alive. Climbs from the foreground fullscreen process up to the highest
    ancestor that is still BELOW a known launcher (steam.exe etc.) or the
    shell  i.e. the game's own root process  then force-kills that whole
    subtree. Returns the killed root pid, or None if no game was found.

    Stopping the climb at a launcher/shell is the 'cleared from parent' part:
    we never kill Steam/Explorer, only the game and everything it spawned."""
    try:
        import psutil
    except ImportError:
        return None
    # Use the non-fullscreen foreground pid so WINDOWED games close too (the
    # fullscreen-only _foreground_game_pid is for auto gamepad mode, not this
    # explicit kill chord).
    pid = _foreground_window_kill_pid()
    if not pid:
        return None
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    # Climb to the game's root: keep moving up while the parent is an ordinary
    # process. Stop when the parent is a launcher, a shell/system process, or
    # gone  that parent is the boundary we must not cross. Depth-capped so a
    # weird chain can't walk us up to init.
    root = proc
    try:
        cur = proc
        for _ in range(8):
            par = cur.parent()
            if par is None:
                break
            pname = (par.name() or "").lower()
            if (pname in _GAME_LAUNCHERS
                    or pname in _NON_GAME_FULLSCREEN
                    or par.pid <= 4):
                break  # parent is the launcher / shell / system  stop here
            root = cur = par
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # Kill the whole subtree: children first, then the root.
    victims = []
    try:
        victims = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    victims.append(root)
    killed_root = root.pid
    for p in victims:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed_root


# --- Settings persistence ---------------------------------------------------

# Named OSK transparency notch → continuous 0..1 slider fraction. Used to migrate
# old string settings and to map the tray "Transparent" submenu radio items.
_OSK_TRANSP_NAME_FRAC = {"off": 0.0, "low": 1.0 / 3, "medium": 2.0 / 3, "high": 1.0}


def _settings_paths():
    """Candidate settings.json locations, in preference order:
      1. next to the exe  the portable-install contract (unchanged default);
      2. the per-user config dir  %APPDATA%\\SteamlessInput on Windows,
         $XDG_CONFIG_HOME/SteamlessInput elsewhere.
    The fallback exists for read-only installs (Program Files, an admin-owned
    folder): _save_settings used to print one line to an invisible stderr and
    drop the write, so every option silently reverted on restart."""
    paths = [os.path.join(_exe_dir(), SETTINGS_FILENAME)]
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    if base:
        paths.append(os.path.join(base, "SteamlessInput", SETTINGS_FILENAME))
    return paths


def _settings_read_path():
    """The settings file to LOAD: the first existing candidate, else None.
    Exe-dir wins when both exist, keeping the portable contract; a fallback
    file only ever exists on installs where the exe dir was unwritable, so
    the two can't meaningfully diverge in practice."""
    for p in _settings_paths():
        if os.path.exists(p):
            return p
    return None


def _seed_default_gyro_chords(settings):
    """Plant the program's default "Gyro To Mouse" hotkey  BOTH thumbsticks
    clicked (pads.GYRO_TOGGLE_DEFAULT_BUTTONS) toggles the gyro  on every
    gyro-capable controller that hasn't got one, then latch the one-time
    marker. Paired with the "toggle" gyro-mode default (adusk_state
    .GYRO_DEFAULTS) and the "mouse" output default, so all three of the modal's
    top settings arrive ready to use.

    Runs ONCE per settings file: after it, deleting the hotkey or picking None
    in the cog modal sticks. A kind that already carries a gyro_toggle chord
    keeps it, and a mode the user actually chose (hold_enable/hold_suppress) is
    never overwritten  only the OLD "none" default, and files predating the
    key, are lifted to "toggle". Mutates and returns `settings`."""
    if settings.get("gyro_defaults_seeded"):
        return settings
    chords = {k: list(keybinds_runtime.chords_for(settings.get("chords", []), k))
              for k in pads.KINDS}
    for kind, lst in chords.items():
        if not pads.has_gyro(kind):
            continue
        mkey = pads.setting_key(kind, "gyro_mode")
        if settings.get(mkey, "none") == "none":
            settings[mkey] = adusk_state.GYRO_DEFAULTS["mode"]
        settings.setdefault(pads.setting_key(kind, "gyro_output"),
                            adusk_state.GYRO_DEFAULTS["output"])
        ch = pads.default_gyro_toggle_chord(kind)
        if ch is None or any(isinstance(c, dict) and c.get("type") == "gyro_toggle"
                             for c in lst):
            continue
        lst.append(ch)
    settings["chords"] = chords
    settings["gyro_defaults_seeded"] = True
    return settings


def _load_settings():
    path = _settings_read_path()
    if path is None:
        return _seed_default_gyro_chords(dict(DEFAULT_SETTINGS))
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # A file holding valid JSON that ISN'T an object ("[]", "null", "3")
        # parses fine and then explodes on .items() below  an uncaught
        # AttributeError before the tray icon even appears. Treat anything
        # that isn't a dict as corrupt and fall back to defaults.
        if not isinstance(data, dict):
            raise ValueError("settings.json is not a JSON object")
        merged = dict(DEFAULT_SETTINGS)
        # Coerce each known key to the type of its default: bools stay bool
        # (legacy files stored 0/1), string settings (e.g. "skin") pass through.
        # Per-kind copied settings ("<base>_<kind>"  trigger actuations, gyro
        # tuning, haptics, ...) are GENERATED keys with no DEFAULT_SETTINGS
        # entry; accept them via parse_setting_key or every per-kind Options
        # value silently reverts to default on the next launch.
        for k, val in data.items():
            if k not in DEFAULT_SETTINGS:
                if pads.parse_setting_key(k) is not None:
                    merged[k] = val
                continue
            merged[k] = bool(val) if isinstance(DEFAULT_SETTINGS[k], bool) else val
    except (OSError, ValueError, AttributeError, TypeError):
        # ValueError covers json.JSONDecodeError (its subclass) and the
        # not-a-dict raise above; Attribute/TypeError catch a structurally
        # valid but wrongly-shaped file. A corrupt settings.json must never
        # be able to stop the app from starting.
        return _seed_default_gyro_chords(dict(DEFAULT_SETTINGS))
    # Gamepad mode is now mutually exclusive  if a settings file from an
    # older build has both on, prefer Auto-enable.
    if merged["gamepad_mode"] and merged["auto_gamepad_mode"]:
        merged["gamepad_mode"] = False
    # Migrate old exclusive_access key to block_sc_hid.
    if "exclusive_access" in data:
        merged["block_sc_hid"] = bool(data["exclusive_access"])
    # The single global "rumble_enabled" split into per-controller toggles  seed
    # both from the old value so a saved preference carries over.
    if "rumble_enabled" in data:
        on = bool(data["rumble_enabled"])
        merged["rumble_enabled_sc"] = on
        merged["rumble_enabled_switch"] = on
    # The two-level "low"(6000)/"lower"(3000) actuation collapsed to a single
    # "low" using the lighter 3000 pull  fold a saved "lower" into "low".
    if merged.get("sc_osk_trigger_actuation") == "lower":
        merged["sc_osk_trigger_actuation"] = "low"
    # OSK transparency went from a named level to a continuous 0..1 fraction 
    # migrate old "off"/"low"/"medium"/"high" strings to their notch positions.
    tv = merged.get("osk_transparency")
    if isinstance(tv, str):
        merged["osk_transparency"] = _OSK_TRANSP_NAME_FRAC.get(tv, 0.0)
    elif not isinstance(tv, (int, float)):
        merged["osk_transparency"] = 0.0
    # The per-mode Sleep Manager sliders became two GENERIC timeout sliders
    # ("Sleep Timeout" = the standby timer, "Hibernate Timeout" = the
    # auto-hibernate timer)  carry a saved file's old per-mode minutes over.
    if "sleep_hybrid_min" in data:
        merged["sleep_standby_min"] = data["sleep_hybrid_min"]
    if "sleep_s4_min" in data:
        merged["sleep_hibernate_min"] = data["sleep_s4_min"]
    # The three independent typing toggles collapsed into the single
    # "Trackpad Keyboard Typing Mode" dropdown. Only when the file predates the new key,
    # so a genuine "default" choice isn't overwritten by stale booleans that
    # DEFAULT_SETTINGS no longer carries. Most-specific wins, matching how the
    # old combinations actually behaved: swipe widened both pads and overrode
    # touch typing's fixed halves, and touch typing already implied lift-to-type.
    if "typing_mode" not in data:
        if data.get("swipe_typing"):
            merged["typing_mode"] = "swipe"
        elif data.get("touch_typing"):
            merged["typing_mode"] = "touch"
        elif data.get("release_to_type"):
            merged["typing_mode"] = "release"
    elif merged.get("typing_mode") not in TYPING_MODE_FLAGS:
        merged["typing_mode"] = "default"
    return _seed_default_gyro_chords(merged)


# Settings writes are coalesced. A gradual Options slider applies LIVE on every
# <B1-Motion> tick, and every apply persists  which meant a full serialize +
# rewrite of settings.json (~7 ms on a 110-key file) per mouse-motion event,
# synchronously on the picker's Tk thread. A one-second drag spent most of
# itself inside the file write and pushed over a megabyte at the disk.
#
# Two changes, both invisible to callers:
#   * the text is built with json.dumps and written in ONE f.write. Streaming
#     json.dump() into the file issues a write() per token through the
#     TextIOWrapper and measured ~4x slower for the same bytes (7.0 vs 1.7 ms).
#   * the first save in a burst goes to disk immediately (an isolated toggle,
#     menu click or reset still lands before the user can act on it); further
#     saves inside _SAVE_COALESCE_S collapse into one trailing write.
# Only the WRITE is deferred  the runtime apply beside it still happens on the
# tick, so live controls stay live. Serializing on the CALLER's thread also
# keeps the deferred write off the live settings dict other threads mutate.
_SAVE_COALESCE_S = 0.5

_save_lock = threading.Lock()
_save_timer = None       # threading.Timer for the trailing write (None = idle)
_save_pending = None     # newest serialized settings a deferred write owes
_save_last = 0.0         # time.monotonic() of the last write that landed


def _write_settings_text(text):
    # Portable exe-dir first; on OSError (Program Files and other read-only
    # install dirs) fall through to the per-user config dir so settings
    # actually persist instead of silently reverting every restart.
    last_err = None
    for i, path in enumerate(_settings_paths()):
        try:
            if i:   # fallback location  its directory may not exist yet
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return
        except OSError as e:
            last_err = e
    print(f"settings save failed: {last_err}")


def _flush_settings():
    """Land whatever the coalescing window is still holding. Fired by the
    trailing timer, registered with atexit, and called explicitly on the way
    out  a deferred write must never be lost to shutdown or a relaunch."""
    global _save_timer, _save_pending, _save_last
    with _save_lock:
        if _save_timer is not None:
            _save_timer.cancel()        # no-op when we ARE the timer
            _save_timer = None
        text, _save_pending = _save_pending, None
        if text is None:
            return
        _save_last = time.monotonic()
        _write_settings_text(text)


atexit.register(_flush_settings)


def _save_settings(settings):
    """Persist settings.json (burst-coalesced  see _SAVE_COALESCE_S)."""
    global _save_timer, _save_pending, _save_last
    # Serialize BEFORE taking the lock: this is the per-drag-tick cost, and it
    # reads the caller's own live dict. The write itself happens under the lock
    # so a trailing timer and a foreground save can never interleave inside the
    # file, or land out of order.
    text = json.dumps(settings, indent=2)
    with _save_lock:
        now = time.monotonic()
        if _save_timer is None and now - _save_last >= _SAVE_COALESCE_S:
            _save_last = now
            _save_pending = None
            _write_settings_text(text)
            return
        # Inside the window: keep the newest text and make sure a trailing
        # write is armed to land it.
        _save_pending = text
        if _save_timer is None:
            _save_timer = threading.Timer(
                max(0.0, _SAVE_COALESCE_S - (now - _save_last)),
                _flush_settings)
            _save_timer.daemon = True
            _save_timer.start()


def _save_settings_if_exists(settings):
    """Like _save_settings but never CREATES settings.json  only updates it if
    it's already there. For automatic/background state changes (e.g. the
    one-time Steam-pause toast flag, the last-used-controller glyph memory)
    that aren't a user-driven setting change, so a fresh install doesn't get a
    settings.json until the user actually changes something in the tray/picker
    UI. Once the file exists (from a real user change), these writes land
    normally."""
    if _settings_read_path() is None:
        return
    _save_settings(settings)


# --- Steam client settings (Options → Steam) ---------------------------------
# Toggles imported from the user's SteamManager.bat: each one's state lives in
# Steam's OWN files/registry (not settings.json), read live when the picker
# opens and written on toggle. Account-agnostic: the Steam dir comes from the
# registry and per-account files are found by enumerating userdata\<id>\ (all
# accounts are written; state is read from the most recently used one).
# File edits force-close Steam first  a running Steam rewrites its configs on
# exit, silently undoing the change (same order SteamManager.bat uses).

_STEAM_REG_KEY = r"Software\Valve\Steam"
_RUN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_APPROVED_REG_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                         r"\Explorer\StartupApproved\Run")
# Steam Input = the per-controller-type support flags in localconfig.vdf.
_STEAM_INPUT_KEYS = [
    "SteamController_XBoxSupport", "SteamController_PSSupport",
    "SteamController_SwitchSupport", "SteamController_GenericGamepadSupport",
]


def _steam_install_dir():
    """Steam's install dir from the registry (SteamPath  set per-user by the
    installer), falling back to the default location. None if not found."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STEAM_REG_KEY) as k:
            p = os.path.normpath(str(winreg.QueryValueEx(k, "SteamPath")[0]))
        if os.path.isdir(p):
            return p
    except Exception:
        pass
    p = r"C:\Program Files (x86)\Steam"
    return p if os.path.isdir(p) else None


def _steam_account_dirs():
    """All per-account dirs under Steam's userdata\\ (numeric names, one per
    account that ever logged in)  settings apply to every account so they
    work no matter which one signs in."""
    d = _steam_install_dir()
    if not d:
        return []
    out = []
    try:
        ud = os.path.join(d, "userdata")
        for name in os.listdir(ud):
            if name.isdigit() and name != "0":
                out.append(os.path.join(ud, name))
    except OSError:
        pass
    return out


def _newest_existing(paths):
    """The most recently modified existing path (None if none exist)  used to
    read a setting's state from the account that was signed in last."""
    best = None
    best_t = -1.0
    for p in paths:
        try:
            t = os.path.getmtime(p)
        except OSError:
            continue
        if t > best_t:
            best, best_t = p, t
    return best


def _steam_localconfigs():
    return [os.path.join(u, "config", "localconfig.vdf")
            for u in _steam_account_dirs()]


def _steam_sharedconfigs():
    # App 7 = the Steam client itself; its per-account sharedconfig.vdf holds
    # the account-wide Cloud toggle.
    return [os.path.join(u, "7", "remote", "sharedconfig.vdf")
            for u in _steam_account_dirs()]


def _steam_loginusers():
    d = _steam_install_dir()
    return os.path.join(d, "config", "loginusers.vdf") if d else None


def _close_steam():
    """Force-close Steam (steam.exe + steamwebhelper) before touching its
    config files/registry. Force-kill means no graceful shutdown, so Steam
    never gets the chance to rewrite the file we're about to edit."""
    try:
        import psutil
    except ImportError:
        return
    victims = []
    for p in psutil.process_iter(attrs=["name"]):
        try:
            if (p.info.get("name") or "").lower() in (
                    "steam.exe", "steamwebhelper.exe"):
                p.kill()
                victims.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for p in victims:
        try:
            p.wait(timeout=3)
        except Exception:
            pass


def _edit_ro_file(path, edit):
    """Run `edit()` on a file that may be read-only (the user's SteamManager
    locks Steam configs +R so Steam can't rewrite them): clear the attribute
    for the edit, restore it afterwards so their lock survives."""
    import stat
    try:
        ro = not (os.stat(path).st_mode & stat.S_IWRITE)
    except OSError:
        ro = False
    if ro:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    try:
        edit()
    finally:
        if ro:
            try:
                os.chmod(path, stat.S_IREAD)
            except OSError:
                pass


def _vdf_set_keys(path, keys, value, anchor, indent):
    """Set (or insert) '"key"\\t\\t"value"' entries in a Valve VDF file 
    Python port of the user's set_vdf_value.ps1. An existing key is rewritten
    wherever it sits; a missing key is inserted right below `anchor`'s opening
    brace with `indent` tabs. Values here are always single digits, matching
    the \\d+ pattern the .ps1 used."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()
    except OSError as e:
        print(f"vdf edit failed ({path}): {e}")
        return
    anchor_pat = re.compile(
        r'("' + re.escape(anchor) + r'"\s*\r?\n\s*\{\s*\r?\n)')
    for k in keys:
        kv = '"%s"\t\t"%s"' % (k, value)
        key_pat = re.compile(r'"' + re.escape(k) + r'"\s+"\d+"')
        if key_pat.search(content):
            content = key_pat.sub(lambda _m, kv=kv: kv, content)
        else:
            m = anchor_pat.search(content)
            if m is None:
                continue
            content = (content[:m.end()] + "\t" * indent + kv + "\r\n"
                       + content[m.end():])
    try:
        with open(path, "w", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            f.write(content)
    except OSError as e:
        print(f"vdf edit failed ({path}): {e}")


def steam_get_bigpicture():
    """True if Steam is set to start in Big Picture (StartupMode=4)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STEAM_REG_KEY) as k:
            return int(winreg.QueryValueEx(k, "StartupMode")[0]) == 4
    except Exception:
        return False


def steam_set_bigpicture(on):
    _close_steam()  # Steam rewrites StartupMode from its own UI state on exit
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _STEAM_REG_KEY) as k:
            winreg.SetValueEx(k, "StartupMode", 0, winreg.REG_DWORD,
                              4 if on else 7)
    except Exception as e:
        print(f"steam bigpicture toggle failed: {e}")


def steam_get_autostart():
    """True if Steam runs at PC start: its Run-key entry exists AND the shell's
    StartupApproved record isn't blocking it (first byte 03 = disabled)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_REG_KEY) as k:
            winreg.QueryValueEx(k, "Steam")
    except Exception:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            _RUN_APPROVED_REG_KEY) as k:
            data = winreg.QueryValueEx(k, "Steam")[0]
        if data and data[0] == 3:
            return False
    except Exception:
        pass  # no StartupApproved record = enabled
    return True


def steam_set_autostart(on):
    # Pure registry Run-key change  Steam doesn't need to be closed for it.
    try:
        import winreg
        if on:
            d = _steam_install_dir()
            if not d:
                return
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_REG_KEY) as k:
                winreg.SetValueEx(
                    k, "Steam", 0, winreg.REG_SZ,
                    '"%s" -silent' % os.path.join(d, "steam.exe"))
            # 02 00...00 = "enabled"  clears any leftover shell block.
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  _RUN_APPROVED_REG_KEY) as k:
                winreg.SetValueEx(k, "Steam", 0, winreg.REG_BINARY,
                                  bytes([2]) + bytes(11))
        else:
            for key in (_RUN_REG_KEY, _RUN_APPROVED_REG_KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                                        winreg.KEY_SET_VALUE) as k:
                        winreg.DeleteValue(k, "Steam")
                except OSError:
                    pass
    except Exception as e:
        print(f"steam autostart toggle failed: {e}")


def steam_get_ask_account():
    """True if Steam asks which account to use at start (RememberPassword=0
    in loginusers.vdf  covers every saved account)."""
    p = _steam_loginusers()
    if not p:
        return False
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return re.search(r'"RememberPassword"\s+"0"', f.read()) is not None
    except OSError:
        return False


def steam_set_ask_account(on):
    p = _steam_loginusers()
    if not p or not os.path.exists(p):
        return
    _close_steam()
    v = "0" if on else "1"

    def edit():
        with open(p, encoding="utf-8", errors="surrogateescape") as f:
            c = f.read()
        # Rewrite EVERY account's entry so the choice sticks no matter which
        # account is active.
        c = re.sub(r'"RememberPassword"\s+"\d"',
                   '"RememberPassword"\t\t"%s"' % v, c)
        c = re.sub(r'"AllowAutoLogin"\s+"\d"',
                   '"AllowAutoLogin"\t\t"%s"' % v, c)
        with open(p, "w", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            f.write(c)

    _edit_ro_file(p, edit)


# Offline Mode = per-account flags in the SAME loginusers.vdf. Steam reads
# WantsOfflineMode at start-up and signs in from its cached credentials
# instead of contacting Valve; SkipOfflineModeWarning rides along so the
# "you are in offline mode" prompt doesn't need a click every launch.
_STEAM_OFFLINE_KEYS = ("WantsOfflineMode", "SkipOfflineModeWarning")

# Opening line of one account block in loginusers.vdf: "<SteamID64>" then the
# brace on its own line. The digit run is what keeps this from matching an
# ordinary '"key"  "value"' pair  those never have a brace under them.
_STEAM_USER_BLOCK_RE = re.compile(r'"\d{6,}"[ \t]*\r?\n[ \t]*\{[ \t]*\r?\n')


def _steam_user_blocks(content):
    """(start, end) body spans of every per-account block in loginusers.vdf,
    in file order. Brace-COUNTED rather than sliced at the next '}' so a
    nested sub-block (Steam has added them before) can't end an account early
    and leave us reading/writing a half block."""
    out = []
    n = len(content)
    for m in _STEAM_USER_BLOCK_RE.finditer(content):
        depth, i = 1, m.end()
        while i < n and depth:
            c = content[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if not depth:
                    break
            i += 1
        out.append((m.end(), min(i, n)))
    return out


def steam_get_offline():
    """True if Steam is set to launch in Offline Mode.

    Read from the account Steam will ACTUALLY sign in as  the one flagged
    "MostRecent" "1"  so an old second account that was left offline can't
    answer for the live one. Only with no MostRecent marker at all does any
    offline-flagged account count."""
    p = _steam_loginusers()
    if not p:
        return False
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return False
    blocks = [content[s:e] for s, e in _steam_user_blocks(content)]
    if not blocks:
        # No parsable account blocks (empty/odd file)  fall back to the whole
        # file so a single-account vdf we failed to split still reads right.
        blocks = [content]
    recent = [b for b in blocks if re.search(r'"MostRecent"\s+"1"', b)]
    return any(re.search(r'"WantsOfflineMode"\s+"1"', b)
               for b in (recent or blocks))


def steam_set_offline(on):
    """Flag every saved account offline (or back online). Written to ALL
    accounts  like the other loginusers.vdf setting  so the choice sticks
    whichever one signs in, and inserted when Steam hasn't written the keys
    yet (they only appear once offline mode has been used once)."""
    p = _steam_loginusers()
    if not p or not os.path.exists(p):
        return
    _close_steam()  # Steam rewrites loginusers.vdf on exit
    v = "1" if on else "0"

    def edit():
        # newline="" on BOTH sides: read in universal-newline mode and every
        # CRLF in Steam's file would come back as a bare \n and get written
        # out that way, silently rewriting the whole file's line endings.
        with open(p, encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            c = f.read()
        spans = _steam_user_blocks(c)
        if not spans:
            return
        nl = "\r\n" if "\r\n" in c else "\n"   # match the file we were handed
        # Back to front: each edit shifts everything after it, so later spans
        # must be consumed before the earlier ones move them.
        for s, e in reversed(spans):
            body = c[s:e]
            for k in _STEAM_OFFLINE_KEYS:
                kv = '"%s"\t\t"%s"' % (k, v)
                pat = re.compile(r'"' + k + r'"\s+"\d+"')
                if pat.search(body):
                    body = pat.sub(lambda _m, kv=kv: kv, body)
                else:
                    # Account keys sit two levels deep ("users" > SteamID).
                    body = "\t\t" + kv + nl + body
            c = c[:s] + body + c[e:]
        with open(p, "w", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            f.write(c)

    _edit_ro_file(p, edit)


def steam_get_cloud():
    """True unless Steam Cloud is explicitly off ("CloudEnabled" "0") for the
    most recently used account  Steam's default (key absent) is ON."""
    p = _newest_existing(_steam_sharedconfigs())
    if not p:
        return True
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return re.search(r'"CloudEnabled"\s+"0"', f.read()) is None
    except OSError:
        return True


def steam_set_cloud(on):
    paths = [p for p in _steam_sharedconfigs() if os.path.exists(p)]
    if not paths:
        return
    _close_steam()
    v = "1" if on else "0"
    for p in paths:
        _edit_ro_file(p, lambda p=p: _vdf_set_keys(
            p, ["CloudEnabled"], v, "Steam", 5))


_APPCOMPAT_LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"


def steam_get_run_admin():
    """True if steam.exe has the "Run this program as an administrator"
    compatibility flag set (AppCompatFlags\\Layers, keyed by steam.exe's full
    path  the same mechanism as the exe's own Properties > Compatibility
    checkbox)."""
    d = _steam_install_dir()
    if not d:
        return False
    exe = os.path.join(d, "steam.exe")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            _APPCOMPAT_LAYERS_KEY) as k:
            v = str(winreg.QueryValueEx(k, exe)[0])
        return "RUNASADMIN" in v.upper().split()
    except Exception:
        return False


def steam_set_run_admin(on):
    # Pure registry compatibility-flag change  Steam doesn't need to be
    # closed for it, same as autostart.
    d = _steam_install_dir()
    if not d:
        return
    exe = os.path.join(d, "steam.exe")
    try:
        import winreg
        flags = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                _APPCOMPAT_LAYERS_KEY) as k:
                flags = str(winreg.QueryValueEx(k, exe)[0]).split()
        except OSError:
            pass
        flags = [f for f in flags if f.upper() not in ("RUNASADMIN", "~")]
        if on:
            flags.append("RUNASADMIN")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              _APPCOMPAT_LAYERS_KEY) as k:
            if flags:
                winreg.SetValueEx(k, exe, 0, winreg.REG_SZ,
                                  "~ " + " ".join(flags))
            else:
                try:
                    winreg.DeleteValue(k, exe)
                except OSError:
                    pass
    except Exception as e:
        print(f"steam run-as-admin toggle failed: {e}")


def steam_get_steam_input():
    """True unless Steam Input's Xbox controller support is explicitly "0" for
    the most recently used account (Steam's default is ON; the four
    per-controller-type flags are always written together)."""
    p = _newest_existing(_steam_localconfigs())
    if not p:
        return True
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return re.search(
                r'"SteamController_XBoxSupport"\s+"0"', f.read()) is None
    except OSError:
        return True


def steam_set_steam_input(on):
    paths = [p for p in _steam_localconfigs() if os.path.exists(p)]
    if not paths:
        return
    _close_steam()
    v = "1" if on else "0"
    for p in paths:
        _edit_ro_file(p, lambda p=p: _vdf_set_keys(
            p, _STEAM_INPUT_KEYS, v, "UserLocalConfigStore", 1))


# --- Sleep Manager (Options → Sleep Manager) ---------------------------------
# Imported from the user's SleepManager.bat: configures which sleep states
# Windows uses via GUID-alias powercfg calls (setacvalueindex etc.  identical
# from Windows 7 through 11). Like the Steam page, the STATE lives in Windows
# itself (the active power scheme + the hibernation flag), read live when the
# picker opens and written on each control change  with ONE settings.json
# exception: the master "Sleep Manager" toggle and the powercfg SNAPSHOT it
# takes. Enabling the master captures every value these controls can touch;
# disabling it writes that snapshot back, so turning Sleep Manager off returns
# the machine to exactly the configuration it had when it was turned on.
# Windows-only (powercfg)  deliberately NOT mirrored to the Linux tree, like
# the lock-screen guard.

_SLEEP_MAX_MIN = 240   # slider ceiling (minutes); 0 = never / manual only


def _powercfg(*args, timeout=15):
    """Run powercfg hidden; returns (rc, combined output). rc -1 = failed to
    launch. powercfg writes localized text  callers only parse the
    locale-stable hex 'Power Setting Index' lines."""
    import subprocess
    try:
        r = subprocess.run(
            ["powercfg"] + [str(a) for a in args],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        print(f"powercfg {args} failed: {e}")
        return -1, ""


def _sleep_get_values(subgroup, setting):
    """Current (AC, DC) values of a power setting in the active scheme, as
    ints; -1 where unreadable. Parses the '0x...' index lines of
    'powercfg /qh' positionally (AC line first, DC second) so it works on
    non-English Windows too."""
    rc, out = _powercfg("/qh", "SCHEME_CURRENT", subgroup, setting)
    if rc != 0:
        return -1, -1
    vals = re.findall(r"Index:\s*(0x[0-9a-fA-F]+)", out)
    if len(vals) < 2:
        # Non-English fallback: "Index" is localized, so take hex tokens
        # positionally  the current AC and DC values are always the LAST two
        # (after the min/max/increment lines).
        vals = re.findall(r":\s*(0x[0-9a-fA-F]+)\s*$", out, re.MULTILINE)
    try:
        if len(vals) >= 2:
            return int(vals[-2], 16), int(vals[-1], 16)
        if len(vals) == 1:
            return int(vals[0], 16), -1
        return -1, -1
    except ValueError:
        return -1, -1


def _sleep_set_value(subgroup, setting, ac, dc=None, activate=False):
    """Write a power setting's AC (and DC) value in the active scheme.
    Returns True when every issued command succeeded."""
    ok = True
    if ac is not None and ac >= 0:
        ok &= _powercfg("/setacvalueindex", "SCHEME_CURRENT",
                        subgroup, setting, int(ac))[0] == 0
    d = ac if dc is None else dc
    if d is not None and d >= 0:
        ok &= _powercfg("/setdcvalueindex", "SCHEME_CURRENT",
                        subgroup, setting, int(d))[0] == 0
    if activate:
        ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    return ok


def sleep_get_hibernate_enabled():
    """True if hibernation is enabled  read from the registry
    (HibernateEnabled / the newer HibernateEnabledDefault), falling back to
    hiberfil.sys existing, mirroring the .bat's non-English fallback chain."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Power") as k:
            for name in ("HibernateEnabled", "HibernateEnabledDefault"):
                try:
                    return bool(int(winreg.QueryValueEx(k, name)[0]))
                except OSError:
                    continue
    except Exception:
        pass
    return os.path.exists(
        os.path.join(os.environ.get("SystemDrive", "C:") + "\\",
                     "hiberfil.sys"))


def _sleep_hiberfile_type():
    """1 = reduced, 2 = full, 0 = unknown (HiberFileType registry value 
    mapping verified empirically: 'powercfg /h /type reduced' writes 1)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Power") as k:
            return int(winreg.QueryValueEx(k, "HiberFileType")[0])
    except Exception:
        return 0


def sleep_get_status():
    """Live sleep configuration, mirroring the .bat's REFRESH_STATUS: monitor /
    standby / hibernate timeouts in minutes (AC; -1 unreadable), the hybrid
    flag, hibernation on/off, the sign-in-after-sleep flag, and the inferred
    active mode: 'sleep' (standby only), 'sleep_hib' (standby now +
    auto-hibernate later), 's4' (pure hibernate), 'hybrid' (S3+S4) or 'off'
    (all sleep disabled). Manual-only configs (every timer 0) can be
    ambiguous  hibernation-on infers 's4'  so callers that KNOW the chosen
    mode (settings' sleep_last_mode) should prefer it."""
    mon_ac, _ = _sleep_get_values("SUB_VIDEO", "VIDEOIDLE")
    hyb_ac, _ = _sleep_get_values("SUB_SLEEP", "HYBRIDSLEEP")
    sby_ac, _ = _sleep_get_values("SUB_SLEEP", "STANDBYIDLE")
    hib_ac, _ = _sleep_get_values("SUB_SLEEP", "HIBERNATEIDLE")
    lock_ac, _ = _sleep_get_values("SUB_NONE", "CONSOLELOCK")
    hib_on = sleep_get_hibernate_enabled()
    hybrid_on = hyb_ac >= 1
    mon_min = mon_ac // 60 if mon_ac >= 0 else -1
    sby_min = sby_ac // 60 if sby_ac >= 0 else -1
    hibt_min = hib_ac // 60 if hib_ac >= 0 else -1
    if hib_on and hybrid_on:
        mode = "hybrid"
    elif sby_min >= 1 and hib_on and hibt_min >= 1:
        mode = "sleep_hib"
    elif sby_min >= 1:
        mode = "sleep"
    elif hib_on:
        mode = "s4"
    else:
        mode = "off"
    return {
        "monitor_min": mon_min, "standby_min": sby_min,
        "hibernate_min": hibt_min, "hybrid_on": hybrid_on, "hib_on": hib_on,
        "signin_on": lock_ac >= 1, "mode": mode,
    }


def sleep_snapshot():
    """Capture every Windows power value the Sleep Manager controls can touch,
    so disabling the master toggle can put the machine back EXACTLY as it was
    when the toggle was enabled: AC+DC of the monitor / hybrid / standby /
    hibernate / console-lock / power-button settings, the hibernation flag,
    the hiberfile type, Fast Startup, and the ACTIVE POWER SCHEME's GUID (so
    a restore lands on the same plan even after a plan switch)."""
    snap = {}
    for name, (sub, setting) in _SLEEP_SETTINGS.items():
        ac, dc = _sleep_get_values(sub, setting)
        snap[name + "_ac"] = ac
        snap[name + "_dc"] = dc
    snap["hib_enabled"] = sleep_get_hibernate_enabled()
    snap["hiberfile_type"] = _sleep_hiberfile_type()
    snap["fast_startup"] = sleep_get_fast_startup()
    snap["scheme"] = _sleep_active_scheme()
    return snap


_SLEEP_SETTINGS = {
    "mon":  ("SUB_VIDEO", "VIDEOIDLE"),
    "hyb":  ("SUB_SLEEP", "HYBRIDSLEEP"),
    "sby":  ("SUB_SLEEP", "STANDBYIDLE"),
    "hib":  ("SUB_SLEEP", "HIBERNATEIDLE"),
    "lock": ("SUB_NONE", "CONSOLELOCK"),
    "pbtn": ("SUB_BUTTONS", "PBUTTONACTION"),
    # LIDACTION is deliberately NOT here: the two "Choose what closing the
    # lid does" dropdowns sit above the master toggle and are independent of
    # it, so the arm-snapshot/disarm-restore must leave them alone.
}


def _sleep_lid_index(choice, settings):
    """Map a lid-close dropdown choice to its LIDACTION index (0 do nothing /
    1 sleep / 2 hibernate / 3 shut down / 4 turn off the display).
    "active_sleep" follows the Sleep Manager: plain Sleep normally, Hibernate
    while an armed manager runs pure S4  closing the lid then does what
    "go to sleep" actually means on this machine."""
    if choice == "nothing":
        return 0
    if choice == "shutdown":
        return 3
    if choice == "screen_off":
        return 4
    if choice == "hibernate":
        return 2
    if (settings.get("sleep_manager")
            and settings.get("sleep_last_mode") == "s4"):
        return 2
    return 1


def _sleep_lid_choice(index, settings=None):
    """The inverse read: a live LIDACTION index back to its dropdown choice.

    Index 2 is ambiguous  it is both an explicit "Hibernate" pick and what
    "active_sleep" resolves to while an armed manager runs pure S4  so the
    settings decide which reading is right. Without that check, arming the
    manager in S4 mode would silently flip a lid dropdown that says "Active
    Sleep Mode" over to "Hibernate". Index 1 and anything unreadable read back
    as "active_sleep" (Windows' own lid default is Sleep)."""
    if index == 2 and settings is not None:
        if (settings.get("sleep_manager")
                and settings.get("sleep_last_mode") == "s4"):
            return "active_sleep"
        return "hibernate"
    return {0: "nothing", 2: "hibernate", 3: "shutdown",
            4: "screen_off"}.get(index, "active_sleep")


def sleep_restore(snap):
    """Write a sleep_snapshot() back: re-activate the snapshot's power scheme
    first (so SCHEME_CURRENT means the same plan the values came from), then
    the hibernation flag (and hiberfile type)  hybrid sleep needs the
    hiberfile to exist before its flag is meaningful  then every saved AC/DC
    value, then activate the scheme. Values the snapshot couldn't read (-1)
    are left untouched."""
    if not snap:
        return True
    ok = True
    scheme = snap.get("scheme")
    if scheme:
        _powercfg("/setactive", scheme)   # plan gone since? keep the current
    if "fast_startup" in snap:
        sleep_set_fast_startup(bool(snap["fast_startup"]))
    if snap.get("hib_enabled"):
        ok &= _powercfg("/hibernate", "on")[0] == 0
        ft = snap.get("hiberfile_type")
        if ft == 2:
            _powercfg("/h", "/type", "full")
        elif ft == 1:
            _powercfg("/h", "/type", "reduced")
    else:
        ok &= _powercfg("/hibernate", "off")[0] == 0
    for name, (sub, setting) in _SLEEP_SETTINGS.items():
        ok &= _sleep_set_value(sub, setting,
                               snap.get(name + "_ac", -1),
                               snap.get(name + "_dc", -1))
    ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    return ok


def sleep_apply_monitor(minutes):
    """[1] Monitor Sleep Settings: minutes before the monitor turns off,
    0 = never  AC and DC (the .bat's TOGGLE_MONITOR)."""
    ok = _sleep_set_value("SUB_VIDEO", "VIDEOIDLE", int(minutes) * 60,
                          activate=True)
    print("sleep: monitor timeout %s" %
          ("disabled" if not minutes else f"set to {minutes} minutes")
          + ("" if ok else " (FAILED  administrator rights?)"))
    return ok


def _sleep_enable_hiberfile():
    """powercfg /hibernate on + force the hiberfile to full size (a reduced
    hiberfile supports Fast Startup only, not real S4). Port of the shared
    head of APPLY_S4 / APPLY_HYBRID."""
    ok = _powercfg("/hibernate", "on")[0] == 0
    if _powercfg("/h", "/type", "full")[0] != 0:
        _powercfg("/h", "/size", "100")  # legacy fallback
    return ok


def _sleep_set_monitor_relative(minutes):
    """Monitor turns off 36% earlier than the sleep timeout (the .bat's
    SET_MONITOR_RELATIVE: 64%, floored at 1 minute; 0 stays 0)."""
    m = int(minutes) * 64 // 100
    if minutes > 0 and m < 1:
        m = 1
    _sleep_set_value("SUB_VIDEO", "VIDEOIDLE", m * 60)
    return m


def sleep_apply_s4(minutes):
    """[2] Hibernate (S4)  0 W, saved to disk (the .bat's APPLY_S4):
    enable hibernation and set the hiberfile to full size, disable hybrid
    sleep (S3+S4), disable the S3 standby timer (machine hibernates
    directly), set the hibernate timeout (AC and DC, 0 = only when triggered
    manually) and set the monitor timeout to 36% less than the value."""
    ok = _sleep_enable_hiberfile()
    ok &= _sleep_set_value("SUB_SLEEP", "HYBRIDSLEEP", 0)
    ok &= _sleep_set_value("SUB_SLEEP", "STANDBYIDLE", 0)
    ok &= _sleep_set_value("SUB_SLEEP", "HIBERNATEIDLE", int(minutes) * 60)
    _sleep_set_monitor_relative(minutes)
    ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    print(f"sleep: S4 Hibernate configured, timeout {minutes} min"
          + ("" if ok else " (FAILED  administrator rights?)"))
    return ok


def sleep_apply_hybrid(minutes):
    """[3] Hybrid Sleep (S3+S4)  1-5 W, RAM + disk safety copy (the .bat's
    APPLY_HYBRID): enable hibernation (hybrid sleep NEEDS the hiberfile),
    enable hybrid sleep, set the standby timeout (AC and DC, 0 = only when
    triggered manually), disable the auto-hibernate timer (stays in hybrid
    sleep) and set the monitor timeout to 36% less than the value."""
    ok = _sleep_enable_hiberfile()
    ok &= _sleep_set_value("SUB_SLEEP", "HYBRIDSLEEP", 1)
    ok &= _sleep_set_value("SUB_SLEEP", "STANDBYIDLE", int(minutes) * 60)
    ok &= _sleep_set_value("SUB_SLEEP", "HIBERNATEIDLE", 0)
    _sleep_set_monitor_relative(minutes)
    ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    print(f"sleep: Hybrid Sleep configured, timeout {minutes} min"
          + ("" if ok else " (FAILED  administrator rights?)"))
    return ok


def sleep_apply_sleep(minutes):
    """Plain Sleep (standby)  the mode nearly every machine supports:
    classic S3 boxes suspend to RAM, Modern Standby (S0) machines enter their
    connected standby. Instant resume, the console-standby feel. Disables
    hybrid sleep and the auto-hibernate timer, sets the standby timeout
    (AC and DC, 0 = only when triggered manually) and pulls the monitor
    timeout to 36% less. The hibernation flag is left alone (Fast Startup
    needs it; a hiberfile lying dormant costs nothing while asleep)."""
    ok = _sleep_set_value("SUB_SLEEP", "HYBRIDSLEEP", 0)
    ok &= _sleep_set_value("SUB_SLEEP", "STANDBYIDLE", int(minutes) * 60)
    ok &= _sleep_set_value("SUB_SLEEP", "HIBERNATEIDLE", 0)
    _sleep_set_monitor_relative(minutes)
    ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    print(f"sleep: Sleep (standby) configured, timeout {minutes} min"
          + ("" if ok else " (FAILED  administrator rights?)"))
    return ok


def sleep_apply_sleep_hib(sleep_minutes, hib_minutes):
    """Sleep, then Hibernate  the true console behavior: standby after
    `sleep_minutes` (instant resume all evening), then the sleeping machine
    auto-hibernates after `hib_minutes` more (0 W overnight). Enables
    hibernation (full hiberfile) for the second stage, disables hybrid sleep,
    sets BOTH timers and pulls the monitor timeout to 36% under the sleep
    timeout. Either timer can be 0 = that stage only when triggered
    manually."""
    ok = _sleep_enable_hiberfile()
    ok &= _sleep_set_value("SUB_SLEEP", "HYBRIDSLEEP", 0)
    ok &= _sleep_set_value("SUB_SLEEP", "STANDBYIDLE",
                           int(sleep_minutes) * 60)
    ok &= _sleep_set_value("SUB_SLEEP", "HIBERNATEIDLE",
                           int(hib_minutes) * 60)
    _sleep_set_monitor_relative(sleep_minutes)
    ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    print(f"sleep: Sleep-then-Hibernate configured, sleep {sleep_minutes} min"
          f" + hibernate {hib_minutes} min"
          + ("" if ok else " (FAILED  administrator rights?)"))
    return ok


def sleep_apply_off():
    """[4] Disable All Sleep (the .bat's APPLY_OFF): disable hybrid sleep,
    set the standby and hibernate timers to never, then disable hibernation
    (deletes the hiberfile)."""
    ok = _sleep_set_value("SUB_SLEEP", "HYBRIDSLEEP", 0)
    ok &= _sleep_set_value("SUB_SLEEP", "STANDBYIDLE", 0)
    ok &= _sleep_set_value("SUB_SLEEP", "HIBERNATEIDLE", 0)
    ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    ok &= _powercfg("/hibernate", "off")[0] == 0
    print("sleep: all sleep modes disabled"
          + ("" if ok else " (FAILED  administrator rights?)"))
    return ok


def sleep_set_signin(on):
    """[5] Require Sign-in After Sleep: password/PIN prompt when resuming
    from sleep or hibernate (CONSOLELOCK, AC and DC  the .bat's
    APPLY_SIGNIN). Note: Group Policy can override the power-scheme value."""
    ok = _sleep_set_value("SUB_NONE", "CONSOLELOCK", 1 if on else 0,
                          activate=True)
    print("sleep: sign-in after sleep %s" % ("required" if on else "not required")
          + ("" if ok else " (FAILED  administrator rights?)"))
    return ok


def sleep_is_admin():
    """True when the process can actually write power settings  powercfg
    /hibernate and HKLM writes silently fail without elevation."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# powercfg /a row labels (English output)  the .bat's CLASSIFY_STATE targets.
_SLEEP_AVAIL_ROWS = {
    "s3": "Standby (S3)",
    "s4": "Hibernate",
    "hybrid": "Hybrid Sleep",
    "s0": "(S0 Low Power Idle)",
    "fast_startup": "Fast Startup",
}
# Reason-text -> state classification. Sequential like the .bat (a later
# match overrides an earlier one).
_SLEEP_REASON_STATES = [
    ("is not available", "NEEDS_S3"),
    ("Hibernation is not", "NEEDS_HIB"),
    ("has not been enabled", "WINDOWS_OFF"),
    ("internal system component", "BLOCKED"),
    ("S0 low power idle", "BLOCKED_S0"),
    ("hypervisor", "HV"),
    ("firmware", "FIRMWARE"),
]
# States that mean a mode can NEVER be configured on this machine (vs merely
# being off right now)  the picker hides those dropdown options.
SLEEP_HARD_BLOCKS = ("FIRMWARE", "HV", "BLOCKED", "BLOCKED_S0", "NEEDS_S3",
                     "OTHER")


def sleep_availability():
    """Parse 'powercfg /a' into per-state availability (the .bat's
    REFRESH_AVAILABILITY): {"parsed": bool, "states": {key: STATE},
    "reasons": {key: raw powercfg reason line}}. Non-English Windows can't be
    parsed  everything reports UNKNOWN and callers skip the gating/verify
    (apply still works; the value writes are locale-independent)."""
    rc, out = _powercfg("/a")
    av = {"parsed": False,
          "states": {k: "UNKNOWN" for k in _SLEEP_AVAIL_ROWS},
          "reasons": {}}
    if rc != 0 or "sleep states" not in out:
        return av
    av["parsed"] = True
    lines = out.splitlines()
    na_line = next((i for i, l in enumerate(lines)
                    if "not available on this system" in l), len(lines))
    for key, label in _SLEEP_AVAIL_ROWS.items():
        idx = next((i for i, l in enumerate(lines) if label in l), None)
        if idx is None:
            continue
        if idx < na_line:
            av["states"][key] = "AVAILABLE"
            continue
        reason = next((l.strip() for l in lines[idx + 1:] if l.strip()), "")
        av["reasons"][key] = reason
        state = "OTHER"
        for pat, s in _SLEEP_REASON_STATES:
            if pat in reason:
                state = s
        av["states"][key] = state
    return av


def sleep_verify(target):
    """Re-probe availability after an apply and report whether it really took
    (the .bat's VERIFY_STATE / APPLY_OFF check). target: "sleep" /
    "sleep_hib" / "s4" / "hybrid" / "off". Returns (ok, message)."""
    av = sleep_availability()
    if not av["parsed"]:
        return True, "verification skipped - could not parse powercfg output"
    st = av["states"]

    def _standby_ok():
        # Plain sleep is satisfied by EITHER classic S3 or Modern Standby S0.
        return "AVAILABLE" in (st.get("s3"), st.get("s0"))

    if target == "off":
        if st.get("s4") == "AVAILABLE":
            return False, "Windows still lists Hibernate as available."
        return True, "Verified: all sleep-to-disk modes are off."
    if target == "sleep":
        if _standby_ok():
            return True, "Verified: now active and available."
        return False, (av["reasons"].get("s3")
                       or "no standby state is available.")
    if target == "sleep_hib":
        if not _standby_ok():
            return False, (av["reasons"].get("s3")
                           or "no standby state is available.")
        if st.get("s4") != "AVAILABLE":
            return False, (av["reasons"].get("s4")
                           or "Hibernate is still not available.")
        return True, "Verified: now active and available."
    if st.get(target) == "AVAILABLE":
        return True, "Verified: now active and available."
    return False, (av["reasons"].get(target)
                   or "the state is still not available.")


def _sleep_active_scheme():
    """The active power scheme's GUID (None if unreadable)  recorded in the
    snapshot so a restore lands on the SAME plan even if the user (or OEM
    software) switched plans while Sleep Manager was armed."""
    rc, out = _powercfg("/getactivescheme")
    if rc != 0:
        return None
    m = re.search(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
                  out)
    return m.group(1) if m else None


_HIBERBOOT_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Power"


def sleep_get_fast_startup():
    """Windows Fast Startup (hiberboot) on/off  HiberbootEnabled. Missing
    value = enabled (the Windows default)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _HIBERBOOT_KEY) as k:
            return bool(int(winreg.QueryValueEx(k, "HiberbootEnabled")[0]))
    except Exception:
        return True


def sleep_set_fast_startup(on):
    """Write HiberbootEnabled (needs admin  HKLM). Fast Startup only does
    anything while hibernation is enabled (it boots from the hiberfile)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _HIBERBOOT_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "HiberbootEnabled", 0, winreg.REG_DWORD,
                              1 if on else 0)
        print(f"sleep: fast startup {'enabled' if on else 'disabled'}")
        return True
    except Exception as e:
        print(f"sleep: fast startup write failed: {e}")
        return False


def sleep_signin_policy_locked():
    """True when Group Policy also forces the require-sign-in-on-wake value
    (Computer Config > Admin Templates > Power Management > Sleep Settings) 
    it overrides whatever the power scheme says."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Policies\Microsoft\Power\PowerSettings"
                r"\0e796bdb-100d-47d6-a2d5-f7d2daa51f51") as k:
            winreg.QueryValueEx(k, "ACSettingIndex")
        return True
    except Exception:
        return False


def sleep_hiberfile_info():
    """(estimated full-hiberfile GB, free GB on the system drive)  the page
    forces a FULL hiberfile (40% of RAM by default), which can be a lot on a
    big-RAM machine with a small boot SSD. (None, None) if unreadable."""
    try:
        class _MSX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32),
                        ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        msx = _MSX()
        msx.dwLength = ctypes.sizeof(_MSX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(msx)):
            return None, None
        import shutil
        free = shutil.disk_usage(
            os.environ.get("SystemDrive", "C:") + "\\").free
        return (round(msx.ullTotalPhys * 0.4 / 2 ** 30, 1),
                round(free / 2 ** 30, 1))
    except Exception:
        return None, None


def sleep_mode_line(st=None):
    """Human 'Active mode' line, mirroring the .bat's menu banner."""
    st = st or sleep_get_status()

    def _mins(m):
        if m == 0:
            return "manual only"
        return "%d min" % m if m > 0 else "on"

    mode = st["mode"]
    if mode == "hybrid":
        return "Hybrid Sleep S3+S4 (%s)" % _mins(st["standby_min"])
    if mode == "sleep_hib":
        return "Sleep then Hibernate (sleep %s, hibernate +%s)" % (
            _mins(st["standby_min"]), _mins(st["hibernate_min"]))
    if mode == "sleep":
        return "Sleep - standby (%s)" % _mins(st["standby_min"])
    if mode == "s4":
        return "S4 Hibernate (%s)" % _mins(st["hibernate_min"])
    return "All Sleep Disabled"


def _sleep_now_async():
    """'Sleep PC' bindable action: enter the currently configured sleep mode
    (the .bat's [8] Sleep Now). Pure-S4 machines hibernate; everything else
    suspends via SetSuspendState(Hibernate=False), which Windows auto-upgrades
    to hybrid sleep when that is enabled. Runs on its own thread  the call
    doesn't return until resume, and the input loop must not be wedged through
    the transition."""
    def _go():
        try:
            # Only a PURE-S4 config hibernates immediately; sleep, hybrid and
            # sleep-then-hibernate all suspend (Windows auto-upgrades the
            # suspend to hybrid / schedules the deep hibernate itself).
            hibernate = sleep_get_status()["mode"] == "s4"
            ctypes.windll.powrprof.SetSuspendState(1 if hibernate else 0, 0, 0)
        except Exception as e:
            print(f"sleep_pc failed: {e!r}")
    threading.Thread(target=_go, daemon=True, name="sleep-now").start()


def _shutdown_now():
    """'Shutdown PC' bindable action: immediate full shutdown."""
    import subprocess
    try:
        subprocess.Popen(["shutdown", "/s", "/t", "0"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print(f"shutdown_pc failed: {e!r}")


# --- virtual-pad backend presence (tray "Game Mode" gate) -------------------
# The same probe the picker's Gamepad Mode page gates its master toggle on (see
# keybinds_picker._vigem_bus_available): Gamepad Mode needs a virtual-pad
# backend, which on Windows is the ViGEmBus KERNEL driver  a separate install,
# and ViGEmClient.dll ships with us so nothing else notices it's missing. On
# Linux the backend is uinput, which the runtime always has, so this is always
# True here and the tray item is always offered.
def _vigem_bus_ok():
    """True when a virtual gamepad could actually be created right now."""
    if sys.platform != "win32":
        return True                  # Linux: uinput, no driver to install
    try:
        from steamcontroller.gamepad import vigem_bus_available
        return bool(vigem_bus_available())
    except Exception:
        # Can't verify  don't hide a feature over a probe that failed.
        return True


# --- "Block SteamInput Xbox Controller grab" --------------------------------
# Hide the VIRTUAL ViGEm Xbox 360 pad (VID 045E / PID 028E) from Steam so Steam
# Input can't grab it. Steam  like SDL, which it uses to enumerate controllers
#  skips any controller listed in the SDL_GAMECONTROLLER_IGNORE_DEVICES *user*
# env var, which it reads when it launches. That matches the intended workflow
# (enable the block, THEN open Steam). Verified: with this set, SDL stops
# enumerating the Xbox 360 pad entirely. Tradeoff while it's on: Steam and other
# SDL apps also skip real Xbox-360-type pads; XInput games still see our pad
# (XInput doesn't consult this list). Windows-only (HKCU\Environment); the
# helper no-ops elsewhere so the Linux mirror stays import-safe.
_IGNORE_ENV = "SDL_GAMECONTROLLER_IGNORE_DEVICES"
_VIGEM_X360_IGNORE = "0x045E/0x028E"


def _set_xbox_ignore(enabled):
    """Add (enabled) or remove (not enabled) our ViGEm Xbox 360 pad from the
    user's SDL ignore list, preserving any entries the user set themselves, then
    broadcast the change so a Steam launched afterwards inherits it. No-op off
    Windows."""
    if os.name != "nt":
        return
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                cur = str(winreg.QueryValueEx(k, _IGNORE_ENV)[0])
        except OSError:
            cur = ""
        parts = [p.strip() for p in cur.split(",") if p.strip()]
        tgt = _VIGEM_X360_IGNORE.lower()
        has = any(p.lower() == tgt for p in parts)
        if enabled and not has:
            parts.append(_VIGEM_X360_IGNORE)
        elif not enabled and has:
            parts = [p for p in parts if p.lower() != tgt]
        else:
            return  # already in the desired state
        new_val = ",".join(parts)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            if new_val:
                winreg.SetValueEx(k, _IGNORE_ENV, 0, winreg.REG_SZ, new_val)
            else:
                try:
                    winreg.DeleteValue(k, _IGNORE_ENV)
                except OSError:
                    pass
        # Nudge Explorer (which launches Steam) to refresh its environment block.
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.c_wchar_p("Environment"),
            0x0002, 2000, ctypes.byref(ctypes.c_ulong()))
    except Exception as e:
        print(f"_set_xbox_ignore failed: {e!r}")


def _chime_log(msg):
    """Best-effort diagnostic log for the gamepad-mode chime trigger, written
    next to the EXE as chime_debug.log. Opt-in via ADUSK_GAMEPAD_DEBUG (same
    switch as the auto-gamepad debug log) so normal use writes nothing."""
    if not os.environ.get("ADUSK_GAMEPAD_DEBUG"):
        return
    try:
        path = os.path.join(_exe_dir(), "chime_debug.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# --- Windows "launch at logon" ----------------------------------------------
# Autostart is a Start Menu Startup-folder shortcut, not an HKCU\...\Run value:
# Microsoft Defender's behavioral ML flagged the Run-key write
# (Behavior:Win32/Persistence.A!ml) on unsigned, freshly downloaded binaries and
# quarantined the app on first launch. The Startup shortcut hooks the same logon
# event without that signature. See autostart.py; set_enabled() also clears the
# old Run value so migrating users stop tripping the detection.

def _apply_autostart(enabled):
    autostart.set_enabled(bool(enabled))


# --- Lock-screen guard ------------------------------------------------------
#
# This tray app runs in the *interactive user session* and keeps reading the
# controller even while the PC is locked. Without this guard, pressing X on the
# lock screen would pop our keyboard up on the user's (Default) desktop 
# invisible *behind* the secure Winlogon lock screen  instead of doing nothing.
# (The lock screen has its own separate keyboard launched via the accessibility
# hook.) OpenInputDesktop succeeds only when the *Default* desktop owns input;
# while the secure desktop is up (lock screen, UAC, Ctrl+Alt+Del) it fails from
# a user-session process, which is exactly our "is it locked?" signal.

_user32 = ctypes.windll.user32
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
_user32.CloseDesktop.restype = wintypes.BOOL


def _start_esc_hook(app):
    """Install a WH_KEYBOARD_LL hook that closes the OSK on physical Escape.
    Returns an opaque state dict (keep a reference to prevent GC of the hook).
    Skips injected (LLKHF_INJECTED) key events so virtual Escape presses sent
    by pynput's Controller don't interfere  mixing pynput Listener + Controller
    on the same key eats events on Windows.

    Uses pointer-sized LRESULT (c_ssize_t) and WINFUNCTYPE so the hook chain
    isn't corrupted on 64-bit, and dispatches the close on a worker thread so the
    callback returns instantly (LL hooks are dropped if the callback is slow)."""
    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN = 0x0100
    _WM_SYSKEYDOWN = 0x0104
    _LLKHF_INJECTED = 0x10
    _VK_ESCAPE = 0x1B

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode",     wintypes.DWORD),
            ("scanCode",   wintypes.DWORD),
            ("flags",      wintypes.DWORD),
            ("time",       wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    _LRESULT = ctypes.c_ssize_t
    _u32 = ctypes.windll.user32
    _u32.CallNextHookEx.restype = _LRESULT
    _u32.CallNextHookEx.argtypes = [
        wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    _u32.SetWindowsHookExW.restype = wintypes.HHOOK
    _u32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]

    _HOOKPROC = ctypes.WINFUNCTYPE(
        _LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    state = {"hook": None, "cb": None}

    def _do_close():
        try:
            if not adusk_state.take_esc_close_suppressed():
                app.toggle_keyboard_hotkey()
        except Exception as e:
            print(f"esc close failed: {e!r}")

    def _hook_proc(nCode, wParam, lParam):
        try:
            if nCode >= 0 and wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                kb = ctypes.cast(
                    lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                # Only a real (non-injected) Escape closes the OSK, so virtual
                # Escapes our own controller path injects are ignored.
                if (kb.vkCode == _VK_ESCAPE
                        and not (kb.flags & _LLKHF_INJECTED)
                        and app._kbd_open):
                    threading.Thread(target=_do_close, daemon=True).start()
        except Exception as e:
            print(f"esc hook_proc error: {e!r}")
        return _u32.CallNextHookEx(state["hook"], nCode, wParam, lParam)

    cb = _HOOKPROC(_hook_proc)
    state["cb"] = cb  # prevent GC

    def _thread_main():
        hk = _u32.SetWindowsHookExW(_WH_KEYBOARD_LL, cb, None, 0)
        if not hk:
            print(f"esc hook: SetWindowsHookExW failed err={ctypes.get_last_error()}")
            return
        state["hook"] = hk
        msg = wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))
        _u32.UnhookWindowsHookEx(hk)
        state["hook"] = None

    t = threading.Thread(target=_thread_main, daemon=True, name="esc-hook")
    t.start()
    return state


def _start_osk_hotkey_hook(app):
    """Install a WH_KEYBOARD_LL hook for Win+Ctrl+O  opens (or closes) the
    on-screen keyboard, so it can be tried without a Steam Controller.

    Win+Ctrl+O is ALSO Windows' own built-in Ease-of-Access shortcut for its
    native on-screen keyboard. Binding the same combo here means the OS's own
    OSK would toggle too on every press unless we swallow the keystroke
    ourselves  a plain listener (pynput's GlobalHotKeys, used for the old
    Ctrl+Alt+K binding this replaces) can only observe key events, not block
    them, so it can't prevent that double-open. A low-level hook can: return
    from the hook procedure WITHOUT calling CallNextHookEx and the event never
    reaches later hooks (or the OS's own hotkey processing), so only OUR
    keyboard opens.

    Returns an opaque state dict (keep a reference to prevent GC of the hook).
    Skips injected (LLKHF_INJECTED) presses so virtual key traffic  from our
    own OSK or elsewhere in the app  never retriggers this."""
    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN = 0x0100
    _WM_SYSKEYDOWN = 0x0104
    _WM_KEYUP = 0x0101
    _WM_SYSKEYUP = 0x0105
    _LLKHF_INJECTED = 0x10
    _VK_O = 0x4F
    _VK_CONTROL = 0x11         # GetAsyncKeyState: true for either L/R Ctrl
    _VK_LWIN = 0x5B
    _VK_RWIN = 0x5C

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode",     wintypes.DWORD),
            ("scanCode",   wintypes.DWORD),
            ("flags",      wintypes.DWORD),
            ("time",       wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    _LRESULT = ctypes.c_ssize_t
    _u32 = ctypes.windll.user32
    _u32.CallNextHookEx.restype = _LRESULT
    _u32.CallNextHookEx.argtypes = [
        wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    _u32.SetWindowsHookExW.restype = wintypes.HHOOK
    _u32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
    _u32.GetAsyncKeyState.restype = ctypes.c_short
    _u32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    _HOOKPROC = ctypes.WINFUNCTYPE(
        _LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    # "held" gates repeat-fire while O is held down; "suppressed" remembers
    # that we ate the key-down so the matching key-up is eaten too (leaving a
    # dangling up-without-down would confuse whatever the hook chain feeds).
    state = {"hook": None, "cb": None, "held": False, "suppressed": False}

    def _mods_down():
        return (bool(_u32.GetAsyncKeyState(_VK_CONTROL) & 0x8000)
                and (bool(_u32.GetAsyncKeyState(_VK_LWIN) & 0x8000)
                     or bool(_u32.GetAsyncKeyState(_VK_RWIN) & 0x8000)))

    def _do_toggle():
        try:
            app.toggle_keyboard_hotkey()
        except Exception as e:
            print(f"osk hotkey toggle failed: {e!r}")

    def _hook_proc(nCode, wParam, lParam):
        try:
            if nCode >= 0:
                kb = ctypes.cast(
                    lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == _VK_O and not (kb.flags & _LLKHF_INJECTED):
                    if wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                        if _mods_down():
                            if not state["held"]:
                                state["held"] = True
                                threading.Thread(target=_do_toggle,
                                                 daemon=True).start()
                            state["suppressed"] = True
                            return 1  # swallow  stops Windows' own OSK
                                      # shortcut (and a stray typed "o")
                    elif wParam in (_WM_KEYUP, _WM_SYSKEYUP):
                        state["held"] = False
                        if state["suppressed"]:
                            state["suppressed"] = False
                            return 1  # swallow the matching key-up too
        except Exception as e:
            print(f"osk hotkey hook_proc error: {e!r}")
        return _u32.CallNextHookEx(state["hook"], nCode, wParam, lParam)

    cb = _HOOKPROC(_hook_proc)
    state["cb"] = cb  # prevent GC

    def _thread_main():
        hk = _u32.SetWindowsHookExW(_WH_KEYBOARD_LL, cb, None, 0)
        if not hk:
            print(f"osk hotkey hook: SetWindowsHookExW failed "
                  f"err={ctypes.get_last_error()}")
            return
        state["hook"] = hk
        msg = wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))
        _u32.UnhookWindowsHookEx(hk)
        state["hook"] = None

    t = threading.Thread(target=_thread_main, daemon=True, name="osk-hotkey-hook")
    t.start()
    return state


def _workstation_locked():
    """True while the secure desktop owns input (lock screen / UAC / Secure
    Attention Sequence), so we must NOT open the keyboard behind it."""
    hdesk = _user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not hdesk:
        return True
    _user32.CloseDesktop(hdesk)
    return False


# Shell / desktop / system window classes that are never a real "type into me"
# target  so a stray firmware click onto the empty desktop or taskbar (or our
# own OSK) doesn't get remembered as the window to restore focus to.
_SHELL_WINDOW_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow", "ForegroundStaging", "MultitaskingViewFrame",
    "XamlExplorerHostIslandWindow",
}


def _foreground_target_hwnd():
    """The foreground window the user is typing in: a normal window owned by
    ANOTHER process. Returns None for our own windows and for the shell/desktop,
    so those never get recorded as the focus-restore target. HWND as an int."""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return None
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
        if buf.value in _SHELL_WINDOW_CLASSES:
            return None
        return int(hwnd)
    except Exception:
        return None


def _launch_program(path, args=""):
    """Launch a program/script for a user-configured chord. With args, run the
    quoted path + args through the shell (so the user can type a command line as
    they would in Run); without args, use ShellExecute (os.startfile) so any
    file/URL/folder opens with its default handler. Non-blocking; runs on the
    HID read thread, so any failure is caught and logged, never raised.

    Expands %LOCALAPPDATA%/%APPDATA%/... tokens itself before either branch 
    a portable launch target (see _make_launch_target_portable in
    keybinds_runtime.py, used for e.g. Spotify/Discord) stores those
    unexpanded so the SAME saved config resolves correctly on whichever
    account/PC actually runs it; os.startfile doesn't expand them on its own,
    and relying on cmd.exe to do it in the args branch would be inconsistent
    between the two paths.

    `path` may also be one of two sentinels, both resolved HERE (at fire time,
    not config time) so the shipped default menu works on a PC that looks
    nothing like the one it was authored on:
      * VMENU_LAUNCH_DEFAULT_BROWSER  whatever browser is ACTUALLY the OS
        default right now, launched bare; any stored args are ignored, since
        the whole point is opening the browser's own start page, not a fixed
        site.
      * VMENU_LAUNCH_STEAM  Steam's real install path. This one KEEPS its
        args ("-bigpicture"), so it falls through to the normal launch below
        instead of returning early."""
    try:
        path = (path or "").strip()
        if path == keybinds_runtime.VMENU_LAUNCH_DEFAULT_BROWSER:
            exe = keybinds_runtime.resolve_default_browser_exe()
            if not exe:
                return
            os.startfile(exe)
            return
        if path == keybinds_runtime.VMENU_LAUNCH_STEAM:
            path = keybinds_runtime.resolve_steam_exe() or ""
        path = os.path.expandvars(path)
        if not path:
            return
        args = os.path.expandvars(str(args))
        if args.strip():
            import shlex
            import subprocess
            subprocess.Popen([path] + shlex.split(args))
        else:
            os.startfile(path)  # Windows ShellExecute; handles exe/doc/url/dir
    except Exception as e:
        print(f"chord launch failed for {path!r}: {e}")


def _run_user_script(script):
    """Run a menu button's pasted script (the cog modal's "PowerShell / CMD"
    effect). Non-blocking; runs on the HID read thread, so any failure is
    caught and logged, never raised.

    PowerShell needs PowerShell Core (`pwsh`) installed  Linux bundles no
    PowerShell  and batch/CMD has no Linux interpreter AT ALL. Either
    missing case is a logged no-op rather than an error: the same
    settings.json may well have been written on Windows, where both run, and
    a shared config shouldn't blow up here just because one button's script
    is Windows-only.

    The script goes to a temp .ps1 run with -File rather than through
    -Command: a pasted script is multi-line and can contain any quoting, which
    a single command-line string mangles. No -ExecutionPolicy: that's a
    Windows-only switch (pwsh on Linux defaults to Unrestricted anyway). The
    temp file is cleaned up by a watcher thread once the script exits; it
    can't be deleted upfront because pwsh needs to read it, and the script
    may run for a while."""
    try:
        script = str(script or "")
        if not script.strip():
            return
        import shutil
        import subprocess
        import tempfile
        import threading
        if keybinds_runtime.vmenu_script_is_batch(script):
            print("script skipped: CMD/batch scripts only run on Windows")
            return
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            print("script skipped: pwsh is not installed")
            return
        fd, path = tempfile.mkstemp(suffix=".ps1", prefix="slinput_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        proc = subprocess.Popen([exe, "-NoProfile", "-File", path])

        def _cleanup():
            try:
                proc.wait()
            except Exception:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()
    except Exception as e:
        print(f"user script failed: {e}")


# --- Steam+X chord watcher (reused from adusk_launcher) ---------------------


# --- "Gyro To Mouse" as a BINDABLE action ------------------------------------
# The Desktop / Chords / Gamepad tabs offer "Gyro To Mouse" alongside the other
# system actions. It resolves to ("hold", keybinds_runtime.GYRO_MOUSE_KEY), so
# every hold-capable dispatch site (per-control overrides, gamepad key
# overrides, advanced presses, button combos) reports press and release through
# the same plumbing Push to Talk uses  including the bulk releases that stop a
# hold from stranding.
#
# What a press MEANS is not decided here: it is read from the controller's own
# Options → Gyro To Mouse mode, exactly as App.handle_gyro_toggle reads it for
# the modal's hotkey bars. A button bound on a layout tab and a chord picked in
# that modal therefore behave identically, and all the tuning (output, dots per
# 360°, sensitivity, acceleration, ...) is shared  there is deliberately no
# second copy of these settings hanging off the bind.
_gyro_act_lock = threading.Lock()
_gyro_act_holders = {}      # kind -> {holder ids currently asserting the action}
# App._toggle_gyro_mouse  flips the kind's live state AND gives the haptic
# tick. Set once at App init; None before then (and in any stripped harness),
# where the flip still happens, silently.
_gyro_flip_hook = None
# App._do_toggle_gamepad_mode  see gamepad_mode_flip. Set once at App init.
_gamepad_mode_flip_hook = None


def gyro_action_hold(kind, holder, want):
    """Assert/release the bindable "Gyro To Mouse" action for one controller
    kind on behalf of `holder`. Reference-counted per kind (first holder =
    pressed, last holder let go = released) so several bound controls, or one
    control reached through more than one dispatch path, can't fight."""
    if not kind:
        return
    with _gyro_act_lock:
        holders = _gyro_act_holders.setdefault(kind, set())
        was = bool(holders)
        if want:
            holders.add(holder)
        else:
            holders.discard(holder)
        held = bool(holders)
        if held == was:
            return
    mode = adusk_state.get_gyro_mode(kind)
    if mode == "toggle":
        if held:
            gyro_action_flip(kind)      # once per press, not per frame
    elif mode == "hold_enable":
        adusk_state.set_gyro_mouse(kind, held)
    elif mode == "hold_suppress":
        adusk_state.set_gyro_mouse(kind, not held)
    # "none"  the gyro is switched off for this controller, so a bound button
    # does nothing. The picker says so on the row's badge (which links to the
    # Options page where the mode is chosen) rather than silently guessing one.


def gamepad_mode_flip():
    """Flip the live control scheme between Desktop and Gamepad Mode  the
    bindable "Toggle Gamepad Mode" action. Same effect the Hotkeys "Gamepad
    Mode Toggle" chord bars fire (App._do_toggle_gamepad_mode): CONTROLS only,
    never the ViGEm Bus Driver setting. Reached through a hook rather than a
    constructor argument because the dispatch sites that need it  the SC
    watcher and the SDL desktop controller  are not the objects that own the
    App."""
    fn = _gamepad_mode_flip_hook
    if fn is None:
        return
    try:
        fn()
    except Exception as e:
        print(f"gamepad mode toggle failed: {e!r}")


def big_picture_toggle_async():
    """Open Big Picture, or leave it if it's already up  the bindable "Toggle
    Big Picture" action. On its own thread: the Windows state check walks the
    window list and both steam:// handoffs go through the shell, none of which
    the input loop should wait on."""
    def _go():
        try:
            big_picture.toggle_big_picture()
        except Exception as e:
            print(f"big picture toggle failed: {e!r}")
    threading.Thread(target=_go, name="bp-toggle", daemon=True).start()


def gyro_action_flip(kind):
    """Flip the kind's gyro on/off. The degraded form of the action for
    EDGE-ONLY dispatch sites  a Chords-tab bind, a stick zone, a Steam/"..."
    tap  which see a press but never a release, so "hold to enable" has
    nothing to end on (the same latch fallback Push to Talk takes there)."""
    if not kind or adusk_state.get_gyro_mode(kind) == "none":
        return
    fn = _gyro_flip_hook
    if fn is None:
        adusk_state.toggle_gyro_mouse(kind)
        return
    try:
        fn(kind)
    except Exception:
        adusk_state.toggle_gyro_mouse(kind)


class _ChordState:
    """Persistent state for the Steam+VIEW=Alt+Tab chord. Lives at the App
    level (not on _Watcher) because sc.run() can be kicked mid-chord by
    auto-gamepad-detect when alt-tab steals focus from the game. If this
    state lived on _Watcher, the rebuild would forget that Alt was held
    and the subsequent Alt release would never fire  leaving Alt stuck
    at the OS level (every keypress turns into Alt+key)."""

    def __init__(self):
        self.kb = sui.Keyboard()
        self.mouse = sui.Mouse()
        # True while LEFTALT is currently being held by us.
        self.alt_held = False
        # Rising-edge tracking for VIEW so one physical press = one Tab.
        self.view_was_pressed = False
        # Desktop-mode held paddle modifiers: L4 = Shift, L5 = Windows key.
        # Held here (not on _Watcher) for the same reason as alt_held  a
        # mid-hold sc.run() rebuild must not strand them pressed at the OS
        # level.
        self.shift_held = False
        self.win_held = False
        # Injected mouse buttons held by the gamepad-mode Steam+stick mouse
        # mode (L2 = left, R2 = right). Held here so a mid-hold rebuild can't
        # strand the button down at the OS level.
        self.mouse_left_held = False
        self.mouse_right_held = False
        # Desktop-takeover mouse-button holds, by source. Multiple controls can
        # map to the same OS button (e.g. right-pad-click AND L2 → left), so we
        # track the SET of sources holding each button and only press on the
        # first / release on the last. Lives here (not on _Watcher) so a sc.run()
        # rebuild mid-hold can't strand a button down. See _Watcher pad/trigger
        # click handlers and set_mouse_button().
        self.mouse_holders = {"left": set(), "right": set(), "middle": set()}
        # Desktop-takeover KEY holds by source (sui.Key -> set of sources), for
        # rebindable hold-modifier controls. Same ref-count contract as the mouse
        # holders so a rebuild mid-hold can't strand a modifier down. See
        # set_key() and _Watcher._handle_overrides.
        self.key_holders = {}
        # Sources currently holding the "Gyro To Mouse" action, source -> the
        # controller kind it was asserted for. That action isn't a key, so it
        # can't ride key_holders (whose ref-count is per KEY, and whose release
        # would have no kind to hand back)  see set_key / gyro_action_hold.
        self.gyro_holders = {}

    def release_alt(self):
        if self.alt_held:
            self.kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self.alt_held = False

    def release_shift(self):
        if self.shift_held:
            self.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self.shift_held = False

    def release_win(self):
        if self.win_held:
            self.kb.releaseEvent([sui.Keys.KEY_LEFTMETA])
            self.win_held = False

    def release_mouse_buttons(self):
        if self.mouse_left_held:
            self.mouse.release("left")
            self.mouse_left_held = False
        if self.mouse_right_held:
            self.mouse.release("right")
            self.mouse_right_held = False

    def set_mouse_button(self, button, source, want):
        """Hold/release an OS mouse `button` on behalf of a named `source`.
        Reference-counted by source so two controls mapping to the same button
        don't fight: press on the first holder, release only when the last one
        lets go. Used by the desktop-takeover pad-click / trigger-click handlers."""
        holders = self.mouse_holders[button]
        was_held = bool(holders)
        if want:
            holders.add(source)
        else:
            holders.discard(source)
        is_held = bool(holders)
        if is_held and not was_held:
            self.mouse.press(button)
        elif was_held and not is_held:
            self.mouse.release(button)

    def release_mouse_held(self):
        for button, holders in self.mouse_holders.items():
            if holders:
                holders.clear()
                self.mouse.release(button)

    def set_key(self, key, source, want, kind=None):
        """Hold/release a keyboard `key` on behalf of a named `source`,
        ref-counted like set_mouse_button (press on the first holder, release on
        the last). Used by rebindable hold-modifier controls.

        `key` may also be keybinds_runtime.GYRO_MOUSE_KEY  the "Gyro To Mouse"
        sentinel  in which case the hold enables/suppresses/flips `kind`'s
        gyro per that controller's own Options gyro mode instead of pressing a
        key. Routing it through here means every hold-capable dispatch site
        (per-control overrides, gamepad key overrides, advanced presses, button
        combos) gets it for free, including the bulk release paths that stop a
        hold from stranding."""
        if key == keybinds_runtime.GYRO_MOUSE_KEY:
            # Kept out of key_holders: the gyro action is ref-counted per
            # CONTROLLER KIND (gyro_action_hold), and a release arrives with no
            # kind of its own, so remember which one the press was for.
            if want:
                self.gyro_holders[source] = kind
            else:
                kind = self.gyro_holders.pop(source, kind)
            gyro_action_hold(kind, "sc:" + source, want)
            return
        holders = self.key_holders.setdefault(key, set())
        was_held = bool(holders)
        if want:
            holders.add(source)
        else:
            holders.discard(source)
        is_held = bool(holders)
        if is_held and not was_held:
            self.kb.pressEvent([key])
        elif was_held and not is_held:
            self.kb.releaseEvent([key])

    def release_keys_held(self):
        for key, holders in list(self.key_holders.items()):
            if holders:
                holders.clear()
                self.kb.releaseEvent([key])
        # Same for the gyro action's holds: a mode switch that drops the desktop
        # layer mid-hold must not leave "hold to enable" stuck on (or "hold to
        # suppress" stuck off).
        for source, kind in list(self.gyro_holders.items()):
            gyro_action_hold(kind, "sc:" + source, False)
        self.gyro_holders.clear()

    def release_all_held(self):
        self.release_alt()
        self.release_shift()
        self.release_win()
        self.release_mouse_buttons()
        self.release_mouse_held()
        self.release_keys_held()


class _GyroMouse:
    """Shared gyro-to-mouse integrator: angular velocity (°/s) → relative
    cursor motion with fractional-pixel carry. One instance per input path
    (the HID watcher and the SDL pad loop each keep their own). Fed per input
    frame while that controller's "Gyro To Mouse" is active; yaw steers X and
    pitch steers Y the way Steam Input's gyro mouse does (turn the controller
    left → cursor left, tilt it up → cursor up). All tuning is the kind's
    cog-modal config in adusk_state: gain = Dots-Per-360°/360 × sensitivity
    (px per degree), and gyro_shape applies the speed deadzone / precision /
    acceleration curves (a controller at rest never drifts). dt is clamped so
    the first frame after a toggle/reconnect can't fling."""

    MAX_DT = 0.1             # seconds  dt clamp across gaps

    def __init__(self, move):
        self._move = move    # move(dx, dy)  relative mouse injection
        self._t = 0.0
        self._acc_x = 0.0
        self._acc_y = 0.0

    def reset(self):
        self._t = 0.0
        self._acc_x = self._acc_y = 0.0

    def feed(self, yaw_dps, pitch_dps, now, kind):
        """Integrate one gyro sample; True when the cursor actually moved."""
        dt = now - self._t
        self._t = now
        if dt <= 0.0 or dt > self.MAX_DT:
            return False
        yaw_dps, pitch_dps = adusk_state.gyro_shape(kind, yaw_dps, pitch_dps)
        if not (yaw_dps or pitch_dps):
            return False
        k = adusk_state.get_gyro_gain(kind) * dt
        self._acc_x += -yaw_dps * k    # + yaw = turn left → cursor left
        self._acc_y += -pitch_dps * k  # + pitch = tilt up → cursor up (-y)
        mvx = int(self._acc_x)
        mvy = int(self._acc_y)
        self._acc_x -= mvx
        self._acc_y -= mvy
        if mvx or mvy:
            self._move(mvx, mvy)
            return True
        return False


class _Watcher:
    def __init__(self, should_abort, gamepad=None, chord=None, takeover=False,
                 chords=None, guide_chords=None, sc_overrides=None,
                 lstick_mouse=False, lstick_actions=None,
                 rstick_mouse=True, rstick_actions=None, guide_taps=None,
                 guide_binds=None, guide_rstick_zones=None,
                 guide_lstick_zones=None, gamepad_map=None,
                 gamepad_lt_analog=True, gamepad_rt_analog=True,
                 gamepad_lstick_map=None, gamepad_rstick_map=None,
                 gamepad_toggle_masks=None, on_gamepad_toggle=None,
                 on_mode_hold=None,
                 gyro_toggle_masks=None, on_gyro_toggle=None, gyro_active=None,
                 gamepad_key_overrides=None, button_combos=None,
                 on_profile_cycle=None, kind="sc", adv_engine=None,
                 adv_engine_pc=None, guide_taps_gp=None, on_toggle_gui=None,
                 on_show_keyboard=None):
        self.triggered = False
        # Which HID controller family this watcher is driving ("sc",
        # "sc2015" or "steam_deck"  the same trackpad hardware and the same
        # SCButtons bit space).
        # Selects the per-kind haptics toggle and the profile-cycle target.
        self._kind = kind
        # App callback for the "<Mode> Profile Cycle" bound actions  advances a
        # tab's active profile slot (App-side, then kicks the watcher). Optional.
        self._on_profile_cycle = on_profile_cycle
        # App callback for the "Toggle Config GUI" bound action (default on the
        # Guide button, both modes)  opens/closes the Keybinds picker and hands
        # focus back to the game on close. Optional.
        self._on_toggle_gui = on_toggle_gui
        # App callback for the "Show Keyboard" action when there is NO
        # controller behind the dispatch (see _fire_guide_action). Optional.
        self._on_show_keyboard = on_show_keyboard
        # Desktop takeover: firmware lizard is OFF and WE drive the trackpads,
        # pad clicks and triggers (set whenever there's no gamepad = desktop
        # mode). The pads stream absolute positions; we convert touch-motion to
        # relative cursor / scroll. None = "not currently touching" so a fresh
        # touch (or a re-touch elsewhere after a lift) starts clean with no jump.
        self._takeover = takeover
        self._rpad_prev = None        # last (x, y) while right pad touched
        self._lpad_prev = None        # last (x, y) while left pad touched
        self._pad_mouse_acc_x = 0.0   # fractional-pixel carry (right pad → mouse)
        self._pad_mouse_acc_y = 0.0
        self._rpad_filt = None        # filtered absolute pad pos [x,y] (None=lifted)
        self._rpad_dfilt = [0.0, 0.0]  # 1€-filtered pad velocity (units/sec)
        self._rpad_anchor = [0.0, 0.0]  # snap dead-zone anchor (last emitted point)
        self._rpad_last_t = None      # last right-pad frame time, for dt
        self._rpad_vx = 0.0           # lift velocity (cursor px/sec), captured live
        self._rpad_vy = 0.0
        self._rpad_touched_was = False  # right-pad lift edge detector
        self._rpad_history = deque()  # (t, x, y) samples for lift-velocity average
        # "Right Touchpad Tap to Click" candidate for the CURRENT touch:
        # (t, x, y) at touch-down, or None once the touch stops qualifying
        # (too long / clicked / pinched / caught a fling / moved the cursor).
        self._tap_start = None
        self._tap_hist = deque()      # (t, x, y) raw samples while the candidate lives
        self._tap_moved = 0.0         # cursor px emitted during the candidate touch
        # "Left Touchpad Tap to Click" candidate  the left-pad twin, tracked
        # independently of whatever mode (scroll/dial/scrub/swipe/pan)
        # currently owns the pad, off the RAW touch bit/position so it can't
        # be confused by those handlers repeatedly resetting their own touch
        # state. No cursor-px gate (a still left-pad touch never moves the
        # cursor under any mode), so no _lpad_tap_moved equivalent.
        self._lpad_tap_start = None
        self._lpad_tap_hist = deque()
        self._lpad_raw_touch_prev = False
        self._lpad_tap_last_t = None
        self._fling_active = False    # kinetic-fling impulse in progress
        self._fling_t0 = 0.0          # impulse start time
        self._fling_last_t = 0.0      # last impulse frame time, for dt
        self._fling_v0 = 0.0          # lift speed (impulse ramp-up start, px/sec)
        self._fling_peak = 0.0        # boosted peak speed (px/sec)
        self._fling_dirx = 0.0        # impulse unit direction
        self._fling_diry = 0.0
        self._scroll_acc = 0.0        # fractional-notch carry (left pad → scroll)
        self._lpad_filt = 0.0         # filtered absolute left-pad Y (position LP)
        self._lpad_dfilt = 0.0        # 1€-filtered Y velocity (laptop jolt smoothing)
        self._lpad_anchor = 0.0       # trailing dead-zone anchor (left pad Y)
        self._lpad_last_t = None      # last left-pad frame time, for dt
        self._lscroll_hist = deque()  # (t, cumulative notches)  lift-velocity window
        self._lscroll_pos = 0.0       # cumulative scroll (notches) this touch
        self._scroll_fling_v = 0.0    # laptop-mode coast velocity (notches/sec)
        self._scroll_fling_last_t = 0.0  # last coast frame time, for dt
        # "Wheel scrolling" dial state (left pad circular scroll):
        self._wheel_angle = None      # last dial angle (rad); None = not tracking
        self._wheel_acc = 0.0         # accumulated rotation toward the next notch
        # "Wheel smooth" 1€ smoothing state (same tuning as laptop scrolling):
        self._wheel_raw = 0.0         # unwrapped raw rotation this touch (rad)
        self._wheel_filt = None       # 1€-filtered rotation; None = reseed
        self._wheel_dfilt = 0.0       # filtered arc speed estimate (pad-units/s)
        self._wheel_last_t = 0.0      # last dial frame time, for dt
        # "Text Wheel Selection" dial state (left pad circular text-select while
        # the LEFT mouse button is held  see _handle_pad_text_wheel):
        self._textwheel_angle = None  # last dial angle (rad); None = not tracking
        self._textwheel_acc = 0.0     # accumulated rotation toward the next nudge
        # Video Timeline Scrubbing dial state (left pad, video focused):
        self._scrub_angle = None      # last dial angle (rad); None = not tracking
        self._scrub_acc = 0.0         # accumulated rotation toward the next step
        self._scrub_stepped = False   # dial emitted a frame-step this touch
        self._scrub_focus = False     # cached "a video is focused" answer
        self._scrub_focus_at = 0.0    # when that cache was last refreshed
        # "hover" scrub-mode state (cursor riding the progress bar):
        self._hover_x = None          # virtual scrub x (screen px); None = idle
        self._hover_bar = None        # (x0, x1, y)  estimated progress bar
        self._hover_restore = None    # cursor pos to put back after the scrub
        self._hover_tick_acc = 0.0    # px travelled since the last haptic tick
        self._hover_scan_at = None    # when to pixel-scan for the playhead
        self._hover_moved = 0.0       # px of dial travel since engage (so the
                                      # playhead snap keeps early rotation)
        self._hover_win = None        # (l, t, r, b) window rect at engage
        self._hover_drag = False      # windowed mode (micro-drag commit)
        self._hover_pressed = False   # left button held down on the knob
        self._hover_start_x = None    # knob x at grab (drag cancel point)
        self._hover_scan_tries = 0    # deferred-scan retries used (windowed)
        self._video_region = None     # (hwnd, win_rect, (l,t,r,b)) learned
                                      # video area for the cursor-on-video gate
        self._hover_wiggle = False    # alternating 1px wake-wiggle phase
        self._probe_cx = None         # cursor pos at last probe check (None
        self._probe_cy = None         # until the first frame seeds it)
        self._probe_move_at = 0.0     # when the cursor last actually moved
        self._probe_at = 0.0          # when the last bar probe scan ran
        self._scrub_title = ""        # last foreground title (navigation
                                      # detection: change voids the region)
        self._pinch_anchor = None     # (lx,ly,rx,ry, scale,cx,cy) at engage
        self._pinch_filt = None       # 1€-filtered [lx,ly,rx,ry]
        self._pinch_dfilt = None      # 1€-filtered per-point velocities
        self._pinch_dz = None         # trailing dead-zone anchors (per pad)
        self._pinch_eff = None        # effective points the zoom math sees
        self._pinch_prev = None       # last raw positions (pad-speed calc)
        self._pinch_last_t = 0.0
        self._zoom_scale = 1.0        # current fullscreen magnification
        self._zoom_cx = None          # view center in desktop px (None until
        self._zoom_cy = None          # first gesture seeds screen center)
        # Zoomed 360° pan (left pad while the desktop is magnified):
        self._pan_filt = None         # 1€-filtered touch [x, y]; None = no touch
        self._pan_dfilt = [0.0, 0.0]  # filtered velocity (1€ cutoff)
        self._pan_dz = [0.0, 0.0]     # trailing dead-zone anchor
        self._pan_prev = (0.0, 0.0)   # last raw touch (velocity calc)
        self._pan_last_t = 0.0
        self._pan_pos = [0.0, 0.0]    # cumulative pan deltas (lift window)
        self._pan_hist = deque()      # (t, pos_x, pos_y)  lift-velocity window
        self._pan_fling_vx = 0.0      # lift-throw coast velocity (pad-units/s)
        self._pan_fling_vy = 0.0
        self._pan_fling_last_t = 0.0
        self._swipe_track = None      # live left-pad touch stats (page swipe)
        self._swipe_fire_t = -1e9     # last page-swipe fire time (cooldown;
                                      # "never"  0.0 would block fires in the
                                      # first COOLDOWN sec of monotonic time)
        # Virtual Menus (Options → Virtual Menus): touch a pad with an
        # assigned menu → the on-screen grid shows; thumb highlights, pad
        # CLICK fires the cell's action; lift hides. The overlay window is
        # created lazily ON THIS THREAD and reused across frames.
        self._vmenu_overlay = None
        self._vmenu_ver = -1          # adusk_state menus version last seen
        self._vmenu_by_trigger = {}   # trigger id -> ENABLED menu dict
        self._vmenu_hold_bit = {}     # trigger id -> SCButtons bit(s) (held/touch),
                                      # ORed with the optional 2nd combo button
        self._vmenu_combo_bit = {}    # trigger id -> the 2nd combo button's OWN
                                      # bit alone (0 if this trigger has none) 
                                      # used to mask just that button's normal
                                      # action while it's holding the menu open
        self._vmenu_trig_order = []   # trigger ids, priority order (buttons 1st)
        self._vmenu_trigger = None    # trigger currently showing its menu
        self._vmenu_hl = None         # highlighted entry index
        self._vmenu_click_prev = False
        # "toggle" activation style: latched open/closed state per trigger id,
        # and the raw held/touched state each was at on the LAST frame (so a
        # fresh press-edge can be told apart from an already-held trigger).
        # See _vmenu_trig_active / the edge pass in _handle_virtual_menu.
        self._vmenu_toggle_on = {}
        self._vmenu_held_prev = {}
        self._vmenu_hotbar_idx = {}   # (trigger, menu name) -> current slot
        self._vmenu_last_fire = 0.0   # last fire time (Continuous throttle)
        self._vmenu_pulse_xflags = 0  # Button-Combo Xbox outputs pulsing now
        self._vmenu_pulse_until = 0.0  # ... until this monotonic time
        self._vmenu_suppress_bits = 0  # button-trigger bit to mask this frame
        self._vm_owned_now = ()       # pads a menu owns THIS frame
        # Full-takeover box-selection input (see _vmenu_full_takeover): DPAD
        # + left stick move the highlight, A fires it, tap-then-repeat like
        # the desktop D-pad arrows (_dpad_zone_prev/_dpad_repeat_at) but
        # tracked separately since the two features never run in the same
        # frame (this one runs BEFORE anything else, including that one).
        self._vmenu_nav_zone_prev = "NEUTRAL"
        self._vmenu_nav_repeat_at = 0.0
        self._vmenu_a_prev = False
        # Trigger ids force-closed by an A-button fire (see
        # _vmenu_full_takeover): a HOLD-style trigger can't just be dropped 
        # the button/touch is often still down the instant A fires, and
        # _vmenu_trig_active would re-engage it again next frame. Held here
        # until that trigger reads NOT held at least once (the edge pass in
        # _handle_virtual_menu clears it), so choosing a box actually closes
        # the menu instead of it blinking shut and reopening the same frame.
        self._vmenu_force_closed = set()
        self._lpad_scrub_latch = None  # scrub-vs-scroll picked at touch start
        self._rpad_click_prev = False  # right pad click edge (haptic)
        self._lpad_click_prev = False  # left pad click edge (haptic)
        # Pinch To Zoom claims BOTH pad clicks (it engages on a hard press of
        # each). While a pad's click is owned by pinch it must NOT also fire its
        # mouse button; the lock stays set until that pad is physically released,
        # so ending a pinch never leaves a phantom left/middle click held down.
        self._rpad_click_lock = False
        self._lpad_click_lock = False
        # Analog pad pressure at the moment each pad last physically CLICKED
        # (learned live from normal pad clicks). Pinch engages at PINCH_FORCE_FRAC
        # of this  a lighter press than a full click. 0 = not calibrated yet →
        # fall back to the click bit for that pad.
        self._rpad_click_force = 0
        self._lpad_click_force = 0
        # Pinch latch: a hard press of BOTH pads engages it, then it STAYS
        # engaged while both fingers keep TOUCHING (no need to keep pressing
        # hard). Dropped only when a finger lifts; re-arm with another hard press.
        self._pinch_latched = False
        self._mouse_freeze_until = 0.0  # click-shake guard deadline
        self._freeze_acc = 0.0          # motion swallowed during the freeze
        # Takeover button defaults firmware lizard used to provide (now off):
        # A → Enter, B → Escape (edge-tap; desktop + no-Steam so Steam+B stays
        # the force-kill chord), D-pad → arrow keys (tap-then-repeat like the
        # left stick). Confirmed on hardware these were firmware-driven.
        self._a_was_pressed = False
        self._b_alone_was_pressed = False
        self._dpad_zone_prev = "NEUTRAL"
        self._dpad_repeat_at = 0.0
        # Two-button desktop chords (built by keybinds_runtime.build_chords):
        # list of (button_mask, action). Each fires once on the both-held rising
        # edge; while held, its buttons are masked out of the single-button
        # handlers so they don't also fire. Parallel list tracks active state.
        self._chords_runtime = chords or []
        self._chord_was_active = [False] * len(self._chords_runtime)
        # Gamepad-mode toggle chords (Hotkeys "Gamepad Mode Toggle" action):
        # button masks evaluated EVERY frame in BOTH modes (the normal chord path
        # above is desktop-only, so it could only ever switch gamepad mode ON).
        # on_gamepad_toggle(held) is the App callback; the App latches the fire
        # across the mode-switch watcher rebuild so holding the chord can't
        # ping-pong. The masked bits are dropped from the frame so the chord's
        # buttons don't also fire their own action (desktop) or leak to the
        # virtual pad (gamepad).
        self._gp_toggle_masks = gamepad_toggle_masks or []
        self._on_gamepad_toggle = on_gamepad_toggle
        # Built-in "hold ≡ (Start/Menu) to switch Desktop <-> Gamepad" gesture.
        # on_mode_hold(buttons) is the App callback: it owns the hold timer (so
        # it survives this watcher's rebuild) and returns the bits to strip.
        self._on_mode_hold = on_mode_hold
        # Gyro-to-mouse (per-controller Options "Gyro To Mouse" hotkey bars):
        # same evaluate-every-frame / App-latched contract as the gamepad-mode
        # toggle above. on_gyro_toggle(held) fires the App latch; gyro_active()
        # reads the App-side on/off state (it survives watcher rebuilds), and
        # the watcher turns the device's IMU stream on/off to follow it.
        self._gyro_toggle_masks = gyro_toggle_masks or []
        self._on_gyro_toggle = on_gyro_toggle
        self._gyro_active = gyro_active
        self._gyro_imu_on = False       # last IMU state pushed to the device
        self._gyro_mouse = _GyroMouse(chord.mouse.move) if chord else None
        # Guide chords (built by keybinds_runtime.build_guide_chords): a Hotkeys
        # chord whose component is the Guide button. list of (other_button_bit,
        # action). Same gesture as the Chords tab (Steam HELD + button), so fired
        # in the Steam-held path; their bits join _guide_bind_bits so the built-in
        # Steam+X/VIEW handlers defer to them. _guide_chords_prev: bit -> pressed.
        self._guide_chords = guide_chords or []
        self._guide_chords_prev = {}
        # Per-control desktop rebinds that differ from the default (built by
        # keybinds_runtime.resolve_sc_overrides): list of (cid, bit, action).
        # Their bits are masked out of the hardcoded handlers and dispatched
        # here instead, so only edited controls diverge. _ov_prev tracks the
        # rising edge of each control's tap/combo/scroll action.
        self._sc_overrides = sc_overrides or []
        self._ov_prev = {}
        # GAMEPAD-mode per-control keyboard/mouse/system binds ([(cid, bit,
        # action), ...])  SC controls the user bound to a desktop action on the
        # Gamepad tab. Dispatched by _handle_gamepad_key_overrides while driving
        # the virtual pad; already excluded from the XInput button_map.
        self._gp_key_overrides = gamepad_key_overrides or []
        self._gp_key_prev = {}
        # Advanced press actions (Gamepad tab "__adv" rows): a
        # keybinds_runtime.AdvPressEngine deciding Long/Double/Soft presses.
        # Its owned bits are masked out of the button_map / key-override paths
        # (the engine emits their regular action itself as a deferred pulse);
        # asserted specs are applied per frame by _apply_adv_specs.
        self._adv_engine = adv_engine
        # DESKTOP-mode twin (built from the pc submap's "__adv" rows): same
        # engine class, key-action specs only, stepped in the takeover path.
        # Only one of the two engines is live per watcher (takeover XOR
        # gamepad), so the prev/xflags state is safely shared.
        self._adv_engine_pc = adv_engine_pc
        self._adv_prev = {}          # slot -> spec asserted last frame
        self._adv_xflags = 0         # XUSB flags asserted this frame
        # Gyro→right-stick deflection computed by the gyro block this frame
        # (gamepad mode + gyro_output "rstick"); consumed by gamepad.update.
        self._gyro_stick = None
        # Hotkeys "Button Combo" effects: while a trigger chord is held, HOLD the
        # selected outputs. keybinds_runtime.build_button_combos gives us
        # (mask, is_gamepad, xbox_output_ids, key_actions); the Xbox digital
        # buttons are precompiled to a single OR'd XUSB flag against the live pad
        # (0 when there's no pad / all outputs are keyboard actions). key_actions
        # (keyboard/mouse/system) are held or edge-fired by _handle_button_combos.
        self._button_combos = []
        for mask, is_gp, xbox_ids, key_actions, guide in (button_combos or []):
            xflags = 0
            if gamepad is not None:
                for oid in xbox_ids:
                    xflags |= gamepad.action_flag(oid)
            self._button_combos.append({
                "mask": mask, "is_gamepad": is_gp, "guide": guide,
                "xflags": xflags, "key_actions": key_actions})
        self._combo_was = [False] * len(self._button_combos)  # per-combo held edge
        self._combo_extra = 0        # XUSB flags to OR into the pad this frame
        self._combo_suppress = 0     # trigger bits to mask from single-button paths
        # Steam / "..." (QAM) TAP rebinds ({cid: resolve_action tuple}). A short
        # press+release with NO other button touched during the hold fires the
        # bound action; any chord (Steam held + another button) cancels the tap so
        # the existing Steam/"..." chords are unaffected. Desktop (takeover) only.
        self._guide_taps = guide_taps or {}
        # GAMEPAD-mode Steam/"..." TAP rebinds  the Guide button's Gamepad-tab
        # binding (default "Toggle Config GUI"). Fires on the same clean-tap
        # detector while a virtual pad is driven, so tapping Guide in gamepad
        # mode pops the config GUI; HOLD still runs the gaming chords.
        self._guide_taps_gp = guide_taps_gp or {}
        # previous-frame STEAM|QAM held. None = this watcher hasn't seen a
        # frame yet: a guide button that is ALREADY down on the first one was
        # pressed against the PREVIOUS watcher (the launcher rebuilds this
        # object on every OSK close / mode flip / keybind save), so its rising
        # edge is lost and its release must not read as a fresh tap  see
        # _handle_guide_taps.
        self._guide_prev = None
        self._guide_press_t = 0.0      # rising-edge time of the current hold
        self._guide_other = False      # a non-guide button was seen during the hold
        self._guide_bits = 0           # which guide bits (STEAM/QAM) were pressed
        # Guide-hold binds: [(bit, action), ...] for Steam+button chords from
        # the picker's "Chords" tab. Fired once on the rising edge of each button
        # while Steam is held. Excludes buttons with dedicated hardcoded handlers.
        self._guide_binds = guide_binds or []
        self._guide_binds_prev = {}    # bit -> was_pressed last frame
        # Set of int(bit) values that have non-none guide binds OR a guide chord;
        # used to gate the hardcoded Steam+B/Y/X/VIEW handlers so user rebinds /
        # guide chords take over (e.g. a Guide+X chord stops Steam+X opening OSK).
        self._guide_bind_bits = (frozenset(bit for bit, _ in self._guide_binds)
                                 | frozenset(bit for bit, _ in self._guide_chords))
        # Right-stick directional guide zones: {zone: action} (UP/DOWN/LEFT/RIGHT).
        # When non-empty, fires on zone entry while Steam is held and suppresses
        # the cursor-mode right-stick so the stick isn't doing two things at once.
        self._guide_rstick_zones = guide_rstick_zones or {}
        self._guide_rstick_zone_prev = "NEUTRAL"
        # Left-stick directional guide zones: {zone: action}. When non-empty,
        # fires on zone entry while Steam is held and suppresses the hardcoded
        # media-chord zone handling so the stick isn't doing two things at once.
        self._guide_lstick_zones = guide_lstick_zones or {}
        self._guide_lstick_zone_prev = "NEUTRAL"
        # Left stick = directional actions per zone (default arrow keys), applied
        # with tap-then-repeat. Right stick = cursor only when rstick_mouse. Both
        # rebindable (keybinds_runtime.resolve_sc_sticks); defaults reproduce the
        # built-in left=arrows / right=mouse behavior exactly.
        self._lstick_mouse = lstick_mouse
        self._lstick_actions = lstick_actions or {
            "UP": ("tap", sui.Keys.KEY_UP), "DOWN": ("tap", sui.Keys.KEY_DOWN),
            "LEFT": ("tap", sui.Keys.KEY_LEFT), "RIGHT": ("tap", sui.Keys.KEY_RIGHT),
        }
        self._lmouse_last_t = None
        self._lmouse_acc_x = 0.0
        self._lmouse_acc_y = 0.0
        self._rstick_mouse = rstick_mouse
        self._rstick_actions = rstick_actions or {}
        self._rstick_zone_prev = "NEUTRAL"
        self._rstick_repeat_at = 0.0
        # HWND (int) of the desktop window the user was typing in just before
        # an OSK-open press, sampled while neither Steam nor X is held. The
        # launcher hands this to adusk so it can restore focus after the OSK
        # opens (a controller-open's firmware mouse-click can steal it).
        self._last_user_hwnd = None
        self._fg_poll_at = 0.0
        # Callable returning True when the sc.run() loop should exit early
        # (e.g. tray-Exit was clicked, or Steam started).
        self._should_abort = should_abort
        # Optional VirtualGamepad  when present, every input frame is
        # forwarded to ViGEm so the controller acts as an Xbox 360 pad.
        self._gamepad = gamepad
        # Gamepad-mode per-control remap (the picker's SC "gamepad" binds). The
        # raw [(sc_bit, action)] map is compiled once (action->XUSB flag) against
        # the live pad; update() ORs the flags. None = the pad's built-in 1:1
        # translation. lt/rt analog flags go dark when L2/R2 is bound to a button.
        self._gamepad_map = None
        self._gamepad_lt_analog = gamepad_lt_analog
        self._gamepad_rt_analog = gamepad_rt_analog
        if gamepad is not None and gamepad_map:
            try:
                self._gamepad_map = gamepad.compile_button_map(gamepad_map)
            except Exception as e:
                print(f"gamepad button-map compile failed; using default: {e!r}")
        # Stick direction → XUSB flag maps for gamepad mode. None = analog passthrough.
        # When set, the analog axis is zeroed and the flag for the active zone is OR'd in.
        self._gp_lstick_dir = None
        self._gp_rstick_dir = None
        if gamepad is not None:
            for src, attr in ((gamepad_lstick_map, "_gp_lstick_dir"),
                              (gamepad_rstick_map, "_gp_rstick_dir")):
                if src:
                    m = {z: gamepad.action_flag(a) for z, a in src.items()}
                    m = {z: f for z, f in m.items() if f}
                    if m:
                        setattr(self, attr, m)
        # Tracks whether we've asked the controller to switch into firmware
        # lizard mode for the duration of a Steam-button hold. Only meaningful
        # when _gamepad is not None (gamepad mode is active).
        self._steam_hold_lizard = False
        # Tracks the lizard state we last set in gamepad mode so we only
        # send a feature report when it actually needs to change.
        self._gamepad_lizard_on = False
        # Latches set while Steam is held: pad-touch engages lizard for the
        # rest of the hold (so brief finger lifts don't flicker the firmware
        # mouse); VIEW commits to chord mode for the rest of the hold (so
        # subsequent VIEW taps don't flip lizard on/off mid-Alt-Tab).
        self._steam_hold_pad_used = False
        self._steam_hold_chord_used = False
        # Shared chord state (Alt held flag, VIEW edge, kb) so the chord
        # survives sc.run() restarts. Falls back to a local _ChordState if
        # the caller doesn't supply one (e.g. tests).
        self._chord = chord if chord is not None else _ChordState()
        # Steam + left-stick media chords (volume / track skip) and Steam + L3
        # (play/pause). Mirrors adusk/controller.py so the chords work whether
        # or not the on-screen keyboard is open.
        self._stick_zone_prev = "NEUTRAL"
        self._stick_repeat_at = 0.0
        self._l3_was_pressed = False
        # Left stick → arrow keys in passive/desktop mode (no Steam). Dominant
        # axis, auto-repeating while held so it feels like holding an arrow.
        self._arrow_zone_prev = "NEUTRAL"
        self._arrow_repeat_at = 0.0
        # Right stick → mouse in passive/desktop mode. Velocity scales with
        # deflection; movement is integrated over real time and fractional
        # pixels are carried between frames so slow movement isn't lost.
        self._mouse_last_t = 0.0
        self._mouse_acc_x = 0.0
        self._mouse_acc_y = 0.0
        # Steam + Y → power off the controller (like Steam Input). _powered_off
        # latches so we only send the command once per chord press.
        self._powered_off = False
        # "Toggle Screen" (Options special action)  flips each press: off, then
        # the NEXT press turns it back on. Persists across presses (not a
        # per-hold debounce latch like _powered_off above).
        self._screen_off = False
        # Steam + B → force-kill the foreground game (cleared from its parent
        # launcher). Latches so it fires once per chord press.
        self._force_kill_done = False
        # Y alone (no Steam) in passive/desktop mode → Space. Rising-edge
        # latch so one press = one Space. NOTE: firmware lizard is still on in
        # passive mode, so the controller may also emit its own Y action.
        self._y_alone_was_pressed = False
        # X opens the on-screen keyboard (bare X in desktop mode, Steam+X in
        # any mode). Rising-edge latch so one press = one open.
        self._x_open_was_pressed = False
        # Right back paddles in passive/desktop mode: R4 (RGRIP1) → Page Up,
        # R5 (RGRIP2) → Page Down. Rising-edge latches.
        self._r4_was_pressed = False
        self._r5_was_pressed = False
        # L1 / R1 (bumpers) in desktop mode → previous / next browser tab.
        # Rising-edge latches.
        self._lb_was_pressed = False
        self._rb_was_pressed = False
        # L3 (left stick click) alone in desktop mode → middle click at the
        # cursor (Steam+L3 is Play/Pause). Rising-edge latch, tracked every frame.
        self._l3_mid_prev = False
        # L2 / R2 full-pull (firmware mouse left/right click in desktop mode):
        # rising-edge latches so each full pull buzzes the haptic click once.
        self._lt_was_pressed = False
        self._rt_was_pressed = False
        # GAMEPAD-mode L2/R2 actuation press state (its own hysteresis latch,
        # separate from the desktop mouse-click latches above  it uses the
        # Gamepad Mode Trigger Actuation threshold).
        self._gp_lt_was = False
        self._gp_rt_was = False

    # Left-stick deflection (int16) past this magnitude counts as a direction.
    STICK_DEADZONE = 14000
    # Volume feel: a tap = one step. Holding up/down past STICK_HOLD_DELAY
    # seconds then rapidly ramps, one step every STICK_VOL_REPEAT seconds.
    STICK_HOLD_DELAY = 0.5
    STICK_VOL_REPEAT = 0.021
    # Arrow-key feel: a tap = one press; held past ARROW_HOLD_DELAY it repeats
    # every ARROW_REPEAT seconds (like an OS key-repeat). 0.05 gave ~20s to
    # scroll a test page; /0.7 made it 30% slower, then *1.1 another 10% slower
    # (user-tuned to match the Switch Pro, which gets the same factors below).
    ARROW_HOLD_DELAY = 0.35
    ARROW_REPEAT = 0.05 / 0.7 * 1.1
    # Right-stick mouse: deadzone (int16), top speed in px/sec at full
    # deflection, and an exponent >1 for fine control near center. A bigger
    # exponent = a longer ramp (more of the stick travel maps to slow speeds),
    # so precise cursor control needs less surgical thumb precision.
    MOUSE_DEADZONE = 6000
    MOUSE_SPEED = 1400.0
    MOUSE_EXPONENT = 5.0
    # Minimum speed (fraction of full) the instant the stick passes the deadzone,
    # so the first bit of travel moves a usable amount (>1px/frame) for fine
    # control instead of the near-zero the steep exponent gives.
    MOUSE_MIN = 0.05
    # Desktop takeover trackpad sensitivity (pads stream int16 positions). Base
    # scale so a normal swipe moves a usable distance; the tray "Trackpad Mouse
    # Speed" / "Left Trackpad Scroll Speed" multipliers scale these.
    PAD_MOUSE_SCALE = 0.015         # right pad delta (units) → cursor px
                                    # (was 0.03; user-tuned 50% down with the
                                    # 1€ filter  raw feel wanted less reach)
    PAD_SCROLL_SCALE = 1.0 / 3000.0  # left pad Y delta (units) → wheel notches
    # LEFT-pad scroll de-jitter: position-domain low-pass with an ADAPTIVE time
    # constant blended by raw pad speed  heavy when slow (de-shake a shaky
    # thumb), near-raw on a fast swipe (snappy). The RIGHT-pad cursor used this
    # blend too until it moved to the 1€ filter below; scroll keeps it (its
    # feel was tuned separately). TAU in seconds; SPEED in pad units/sec.
    PAD_SMOOTH_TAU_SLOW = 0.05     # slow/near-still: heavy de-shake smoothing
    PAD_SMOOTH_TAU_FAST = 0.002    # full-swipe: almost raw (very responsive)
    PAD_SMOOTH_SPEED_LO = 12000.0  # ≤ this pad speed → full SLOW smoothing
    PAD_SMOOTH_SPEED_HI = 25000.0  # ≥ this pad speed → full FAST smoothing
    # RIGHT-pad cursor smoothing = a 1€ filter (Casiez et al.) on the ABSOLUTE
    # pad position. The old two-knee tau blend above collapsed to near-RAW past
    # a gentle ~15 mm/s, so all real motion came out unfiltered and harsh;
    # Steam Input instead keeps a constant silky low-pass with a touch of lag 
    # this matches that feel. The cutoff ADAPTS continuously with pad speed:
    #   fc(Hz) = MINCUTOFF + BETA * speed(pad-units/sec);  tau = 1/(2π·fc)
    # Near-still → fc ≈ MINCUTOFF (heavy de-shake, ~80 ms of glide-lag); a fast
    # flick opens the cutoff (lag shrinks to ~15 ms, still snappy). The speed
    # estimate feeding fc is itself low-passed at DCUTOFF so one noisy frame
    # can't pop the filter open (and a tremor's alternating deltas average out
    # instead of defeating the smoothing).
    PAD_EURO_MINCUTOFF = 2.0   # Hz; floor cutoff at rest/slow  lower = smoother/laggier
    PAD_EURO_BETA = 0.00008    # cutoff gain per pad-unit/sec  higher = snappier flicks
                               # (0.000033 read as "waiting for the cursor to
                               # catch up" on fast moves; 0.00008 cuts the
                               # catch-up tail ~4× and slow-motion silk keeps)
    PAD_EURO_DCUTOFF = 2.5     # Hz; low-pass on the speed estimate itself
    # Snap dead-zone radius (pad-units): the cursor doesn't move until the
    # filtered point leaves this radius, then the anchor re-centers on it. A
    # resting/tremoring thumb stays inside → ZERO movement; real motion tracks.
    # Raise if a shaky thumb still nudges the cursor; lower if slow moves step.
    PAD_DEADZONE = 70.0
    # Resting-anchor recenter: after a move stops, the trailing anchor sits
    # exactly ON the dead-zone edge  zero slack left along the direction just
    # traveled, so the very next tremor blip leaked straight out (the residual
    # still-finger wiggle). While the filtered pad speed sits below REST_SPEED
    # (a true rest  deliberate slow drags run faster) the anchor eases back
    # onto the filtered point, restoring the FULL radius of slack all around
    # the resting finger: tremor must now cross the whole dead-zone, in any
    # direction, to move the cursor at all.
    PAD_REST_SPEED = 3200.0          # pad-units/sec; below = resting → recenter
    PAD_ANCHOR_RECENTER_TAU = 0.10   # sec; how quickly the slack is restored
    # Click-shake guard: squeezing a trigger / clicking a pad physically
    # wobbles the finger resting on the right pad, and that wobble used to
    # leak out as a tiny drag between the two clicks of a double-click
    # (folders started dragging instead of opening). Any trigger/pad-click
    # EDGE freezes pad-mouse output briefly; a deliberate drag breaks out by
    # moving farther than the wobble ever does.
    PAD_CLICK_FREEZE_S = 0.40     # cursor freeze after a click/trigger edge
    PAD_FREEZE_BREAKOUT = 650.0   # cumulative pad-units of real motion that
                                  # end the freeze early (intentional drag)
    # "Right Touchpad Tap to Click" (Options → Touchpads): a quick, STILL
    # touch-and-lift on the right pad = a left click, like a laptop touchpad.
    # A tap must lift within MAX_S, have lasted at least MIN_S (a one-frame
    # electrical ghost contact is not a finger), have wandered no more than
    # MAX_DIST raw pad-units from the touch-down point (measured EXCLUDING
    # the final PAD_LIFT_SKIP  the finger peeling off writes a garbage blip
    # that would otherwise veto genuine taps), and have moved the cursor no
    # more than MAX_PX real pixels (a quick corrective nudge that visibly
    # moved the pointer is pointing, not tapping  this is the discriminator
    # for short strokes that pass the raw-distance gate). A tap candidate is
    # also cancelled outright by any physical pad/trigger click edge (the
    # real button wins), by touching down while a fling is coasting (that
    # touch CATCHES the cursor, it doesn't click), while a trigger already
    # holds a mouse button, and by the Pinch To Zoom posture. The click is
    # injected ref-counted (source "rpad_tap") so it can never yank a left
    # button another control is holding, and it fires _mouse_shake_guard so
    # the second tap of a double-tap can't smear the cursor between the two
    # clicks (double-tap opens folders instead of dragging them).
    PAD_TAP_MAX_S = 0.26          # touch must lift within this to be a tap
                                  # (0.22 missed ~1 in 5 real taps; user-tuned)
    PAD_TAP_MIN_S = 0.02          # and last at least this (ghost-blip floor)
    PAD_TAP_START_SKIP = 0.04     # sec dropped from the START of the touch 
                                  # the pad resolving a fresh contact writes a
                                  # garbage position blip in the first frames
                                  # (the touch-down twin of the lift peel blip)
                                  # that used to poison the wander origin and
                                  # read a still tap as a swipe
    PAD_TAP_MAX_DIST = 1300.0     # raw pad-units of wander allowed (~0.8 mm),
                                  # judged as the bounding-box spread of the
                                  # CORE samples (start-skip → lift-skip), not
                                  # distance from the first-contact blip
    PAD_TAP_MAX_PX = 12.0         # emitted cursor px allowed during the touch
                                  # (8 still ate taps  the down-blip leaks a
                                  # few px before the core settles; user-tuned)
    # Analog L2/R2 click hysteresis (0..32767 trigger units): once the pull
    # crosses the actuation threshold and the click ENGAGES, it stays engaged
    # until the pull drops this far BELOW the threshold. Without it, sensor
    # noise + finger tremor at a slowly-held actuation point flip the pull
    # across the single threshold every frame → rapid spam-clicking.
    TRIGGER_CLICK_HYSTERESIS = 2200
    # Lift fling = a punchy velocity IMPULSE (not a decaying coast, which read as
    # "on ice / constant velocity"). On a fast swipe-and-lift the glide velocity
    # ramps UP from the lift speed to a boosted peak over PAD_FLING_RAMPUP_T, then
    # ramps DOWN from the peak to a stop over PAD_FLING_RAMPDOWN_T  a hump (quick
    # accel, gentler settle). The two phases are INDEPENDENT times (sec) so the
    # attack and the release tune separately. A fling only STARTS if the lift
    # velocity exceeds PAD_FLING_TRIGGER (cursor px/sec), so slow/small moves stop
    # dead. Peak ∝ lift speed → travel ∝ how hard you swiped. Tuning: snappier kick
    # → smaller RAMPUP_T / higher BOOST; slower settle (and more travel) → larger
    # RAMPDOWN_T. px/sec & seconds (mirrored to linux/tray_linux.py).
    PAD_VELOCITY_WINDOW = 0.12    # sec of motion history kept for lift velocity
    PAD_LIFT_SKIP = 0.03          # sec dropped from the END of the history at
                                  # lift  the finger PEELING off the pad writes
                                  # a garbage position blip in the final frames
                                  # that used to read as a violent swipe and
                                  # "throw" the cursor from a standing-still lift
    PAD_FLING_MIN_SPAN = 0.05     # the pre-peel history must span at least this
                                  # much real, sustained motion to fling at all
    PAD_FLING_TRIGGER = 250.0     # lift must exceed this (px/sec) to fling at all
                                  # (halved with PAD_MOUSE_SCALE so the same
                                  # PHYSICAL swipe speed still triggers a fling)
    PAD_FLING_GAIN = 1.5          # fling speed = tracking lift × this  throw decoupled
                                  # from cursor speed (raise → flings go further)
    PAD_FLING_BOOST = 1.4         # peak glide speed = fling speed × this (ramp-up kick)
    PAD_FLING_RAMPUP_T = 0.05     # sec ramping UP from lift speed to the peak (fast)
    PAD_FLING_RAMPDOWN_T = 0.34   # sec ramping DOWN from the peak to a stop (slower)
    # "Laptop scrolling" (Options → Touchpads): a quick swipe-and-lift on the
    # LEFT pad sets the page coasting  the scroll velocity at lift carries on
    # and decays exponentially (smooth deceleration), and ANY new touch catches
    # the page dead (the gentle tap). Wheel notches are emitted from the
    # decaying velocity through the fractional accumulator, so the notch
    # cadence itself slows naturally as the page settles. Lift velocity is
    # averaged over the last SCROLL_VELOCITY_WINDOW of samples so one noisy
    # frame can't launch (or kill) a fling. notches/sec & seconds.
    # "Laptop scrolling" jolt smoothing: skin stick-slip on the pad makes the
    # finger catch then suddenly slip a tiny bit  a 1-frame velocity spike
    # that the two-knee blend reads as "fast" (near-raw tau) and passes
    # straight to the page as a vertical jolt. Laptop mode swaps in a GENTLE
    # 1€ filter: the cutoff's speed estimate is itself low-passed (DCUTOFF),
    # so a single-frame spike barely opens the filter (the jolt is smeared
    # smooth) while a SUSTAINED swipe opens it within ~60 ms and scrolls as
    # directly as before. MINCUTOFF matches the old slow-speed tau (~50 ms)
    # so the baseline feel is unchanged  this ONLY softens the jolts.
    # Laptop mode only; Normal scrolling keeps the two-knee blend untouched.
    LSCROLL_EURO_MINCUTOFF = 3.2   # Hz; ≈ the old 0.05 s slow tau (baseline feel)
    LSCROLL_EURO_BETA = 0.00008    # cutoff gain per pad-unit/sec of SUSTAINED speed
    LSCROLL_EURO_DCUTOFF = 2.5     # Hz; low-pass on the speed estimate itself
    SCROLL_VELOCITY_WINDOW = 0.08  # sec of samples averaged for lift velocity
    SCROLL_FLING_TRIGGER = 6.0     # lift must exceed this to coast at all
    SCROLL_FLING_MAX = 80.0        # cap on the initial coast speed
    SCROLL_FLING_TAU = 0.65        # exponential decay time constant (sec)
    SCROLL_FLING_STOP = 1.5        # coast ends below this speed
    # "Video Timeline Scrubbing" (Options → Touchpads dropdown): while a video
    # is focused, the LEFT pad becomes a circular dial. Two modes:
    #   "frame"  precise: SCRUB_STEP_DEG(frame) of rotation per FRAME-STEP
    #     key tap ("." forward / "," back on YouTube). The first step pauses
    #     playback and every step shows the EXACT frame (precise version of
    #     the timeline hover preview); lifting taps "K" to resume playing at
    #     that frame  only if the dial actually stepped, so an idle tap
    #     can't pause a playing video.
    #   "seek"  fast: SCRUB_STEP_DEG(seek) of rotation per Right/Left-arrow
    #     tap (±5s on YouTube). No pause/resume  arrow-seeking never stops
    #     playback, so lifting just leaves it playing at the new spot.
    # Both give a haptic detent tick per step. The dial angle is only sampled
    # outside SCRUB_MIN_RADIUS (atan2 noise near the pad center would spin
    # the dial randomly). Focus = foreground window TITLE contains one of
    # _SCRUB_TITLE_TOKENS (browser tabs carry the site name in the title),
    # cached SCRUB_FOCUS_TTL sec so the Win32 title read stays off the
    # ~266 Hz input path. YouTube only for now  extend by adding tokens
    # (netflix, vlc, mpv, ...) once their seek/frame keys are wired.
    _SCRUB_TITLE_TOKENS = ("youtube",)
    # mode -> (step_deg, key_forward, key_back, pauses_playback)
    _SCRUB_MODES = {
        "frame": (9.0, "KEY_DOT", "KEY_COMMA", True),
        "seek":  (30.0, "KEY_RIGHT", "KEY_LEFT", False),
    }
    SCRUB_MIN_RADIUS = 9000.0   # pad units; ignore angles inside this radius
    SCRUB_FOCUS_TTL = 1.0       # sec between foreground-title re-checks
    # "Wheel scrolling" dial (Options → Touchpads): one wheel notch per this
    # much thumb rotation on the LEFT pad  clockwise = down, ccw = up. 15° ≈
    # 24 detents per full circle, a real scroll-wheel feel. The same
    # SCRUB_MIN_RADIUS center dead-zone keeps atan2 noise from spinning it.
    WHEEL_STEP_DEG = 15.0
    # "Text Wheel Selection" dial: while the LEFT mouse button is held over
    # text, one horizontal cursor nudge of TEXTWHEEL_STEP_PX per this much
    # thumb rotation on the LEFT pad  the live drag's selection endpoint
    # follows the cursor, snapped to character boundaries BY THE APP. A bit
    # coarser than the scroll wheel so single letters are easy to land  18° ≈
    # 20 detents per full circle, each with a haptic tick. Reuses the wheel
    # dial's SCRUB_MIN_RADIUS dead-zone.
    TEXTWHEEL_STEP_DEG = 18.0
    # Horizontal pixels the cursor moves per detent  roughly one average
    # character at 100% scaling. The app's drag logic does the exact character
    # snapping, so a wide/narrow glyph just takes one detent more/less.
    TEXTWHEEL_STEP_PX = 8
    # "Wheel smooth" dial: instead of clicky notches, stream hi-res wheel units
    # proportional to the rotation (this many 1/120-notch units per WHEEL_STEP_DEG
    # of thumb travel) so a full circle scrolls the SAME amount as the 24 notch
    # detents, but continuously  browsers render it as a pixel-smooth analog
    # glide (the same MOUSEEVENTF_WHEEL hi-res trick "Laptop scrolling" uses).
    WHEEL_SMOOTH_UNITS_PER_NOTCH = 120.0
    # "Wheel smooth" runs the dial's rotation through the SAME gentle 1€
    # filter as Laptop scrolling (the LSCROLL_EURO_* constants), so skin
    # stick-slip catches on the circling thumb are smeared out identically.
    # The shared constants are tuned in linear pad-units, so the angle is
    # converted to ARC pad-units at this nominal circling radius (thumb rings
    # the pad at roughly this radius; must be > SCRUB_MIN_RADIUS)  that way
    # "the same physical finger speed" opens the cutoff the same amount.
    WHEEL_FILT_RADIUS = 20000.0
    # Both wheel dials scale by the Options "Scrolling Sensitivity" slider (the
    # same get_sc_scroll_speed() multiplier the linear scroll uses). This is the
    # reference multiplier at which the dial reproduces the tuned WHEEL_STEP_DEG
    # feel  the DEFAULT (medium) scroll multiplier (= _SC_SCROLL_SPEEDS["medium"])
    # so a stock config feels as tuned; a slower slider gives fewer notches / a
    # gentler glide per rotation, a faster slider more. Higher slider = faster.
    WHEEL_SCROLL_SPEED_REF = 0.55
    # "hover" mode (the mouse-like scrub): the REAL cursor rides along the
    # video's progress bar while the thumb circles, so the player shows its
    # hover thumbnail/preview and the video KEEPS PLAYING; lifting the thumb
    # left-clicks the hovered spot (the actual seek) and puts the cursor back.
    # The bar is estimated from the foreground window rect: BAR_BOTTOM px
    # above the bottom edge, spanning the width minus BAR_MARGIN each side 
    # tuned for fullscreen playback, where the bar spans the window. Travel
    # is PX_PER_DEG cursor px per degree of dial (absolute-positioned every
    # frame, sub-degree rotations accumulate  pixel-exact), with a light
    # haptic tick every TICK_PX of travel so distance is felt.
    SCRUB_HOVER_PX_PER_DEG = 1.2
    SCRUB_HOVER_BAR_BOTTOM = 68   # bar sits ~this many px above window bottom
    SCRUB_HOVER_BAR_MARGIN = 24   # dead margin at both ends of the bar
    SCRUB_HOVER_TICK_PX = 40      # haptic tick per this much cursor travel
    SCRUB_PROBE_GAP = 1.0         # min sec between background bar probes
    SCRUB_PROBE_MOVE_WINDOW = 3.0  # probe only this soon after cursor motion
                                   # (that's how long the controls stay up)
    # Pinch To Zoom (one finger per pad, desktop takeover only):
    PINCH_ZOOM_MAX = 8.0            # fullscreen magnification ceiling
    PINCH_SCALE_PER_UNIT = 3.0 / 65536.0  # LINEAR spread→scale (a full-pad
                                    # spread on both pads = +3.0x; no curve)
    PINCH_PAN_SENS = 1.4            # pan speed multiplier (screen-relative)
    PINCH_FORCE_FRAC = 0.5         # pinch engages at this fraction of a pad's
                                    # learned CLICK pressure (half = press half
                                    # as hard as a full click)
    # Zoomed 360° pan: while Pinch To Zoom holds ANY magnification (even 1%),
    # the LEFT pad swaps from the chosen scrolling mode to a macbook-style
    # two-axis pan of the magnified view  content follows the finger  and
    # hands the scrolling back the moment the desktop is fully zoomed out.
    # Touch smoothing = the right-pad cursor pipeline (PAD_EURO_* 1€ filter,
    # trailing dead-zone, resting recenter), so no wiggle/analog feel. The
    # pan itself is ALWAYS free 360° while the finger moves  content follows
    # the finger in any direction, no axis locking (the user tried the
    # during-scroll h/v lock across several revisions and rejected it). The
    # ONLY axis discipline is on the THROW: a fast lift coasts the view with
    # laptop-scroll physics (windowed lift velocity, exponential decay, any
    # touch catches it dead), and a release aimed near an axis snaps that
    # coast dead-straight  see LPAN_THROW_LOCK_*.
    LPAN_SENS = 0.7               # pan speed, screen-relative (½ of PINCH_PAN_SENS)
    LPAN_FLING_TRIGGER = 30000.0  # lift speed (pad-units/s) that throws the view
    LPAN_FLING_MAX = 300000.0     # cap on the initial throw speed
    LPAN_FLING_TAU = 0.55         # throw decay time constant (sec)
    LPAN_FLING_STOP = 2500.0      # coast ends below this speed
    # Throw axis snap: the lift-throw snaps onto an axis when its release
    # velocity points near one  VERY aggressive for vertical (throwing the
    # view up/down a page must coast dead straight), conservative for
    # horizontal so diagonal flings keep their angle. Only the THROW snaps;
    # the live pan is never axis-locked.
    LPAN_THROW_LOCK_V_DEG = 50.0  # throw within this of vertical → pure-v coast
    LPAN_THROW_LOCK_H_DEG = 25.0  # throw within this of horizontal → pure-h
    _LPAN_THROW_V_TAN = math.tan(math.radians(LPAN_THROW_LOCK_V_DEG))
    _LPAN_THROW_H_TAN = math.tan(math.radians(LPAN_THROW_LOCK_H_DEG))
    # "Swipe Between Pages" (Touchpads toggle): a fast, short, decisively
    # HORIZONTAL flick on the LEFT pad = mouse Back/Forward (XBUTTON1/2 
    # browsers, File Explorer and Windows Settings all honor them). macbook
    # semantics: flick RIGHT = Back, flick LEFT = Forward. Every gate below
    # exists to kill misfires: the touch must be BRIEF (a flick, not a
    # scroll drag or a resting thumb), travel FAR, move FAST, and stay FLAT
    # (max vertical excursion capped relative to the horizontal travel 
    # 0.45 ≈ within 24°, which still admits the natural ±18° thumb-arc
    # droop); a cooldown blocks double-navigation; and a touch is POISONED
    # outright while any other left-pad gesture owns it.
    SWIPE_MAX_DUR = 0.35       # sec; longer = a drag/scroll, never a flick
    SWIPE_MIN_DUR = 0.03       # sec; shorter = contact noise/blip
    SWIPE_MIN_DX = 14000.0     # pad-units net horizontal travel (~9 mm)
    SWIPE_MAX_CROSS = 0.45     # max |dy| excursion as a fraction of |dx|
    SWIPE_MIN_SPEED = 70000.0  # pad-units/s mean horizontal speed
    SWIPE_COOLDOWN = 0.45      # sec between fires (one page per flick)

    # Zone→key maps, built once at class scope. Previously these were dict
    # literals rebuilt on every HID frame inside the stick handlers  pure
    # per-frame allocation churn on the hot path.
    _MEDIA_KEYS = {
        "UP":    sui.Keys.KEY_VOLUMEUP,
        "DOWN":  sui.Keys.KEY_VOLUMEDOWN,
        "LEFT":  sui.Keys.KEY_PREVIOUSSONG,
        "RIGHT": sui.Keys.KEY_NEXTSONG,
    }
    _ARROW_KEYS = {
        "UP":    sui.Keys.KEY_UP,
        "DOWN":  sui.Keys.KEY_DOWN,
        "LEFT":  sui.Keys.KEY_LEFT,
        "RIGHT": sui.Keys.KEY_RIGHT,
    }

    def _handle_media_chords(self, sc, sci, steam_now, now):
        """Steam + left stick → media transport (Up/Down = volume, repeating
        while held; Left/Right = previous/next track, one per deflection).
        Steam + L3 (stick click) → Play/Pause. Edge-triggered so one
        deflection / click = one media key."""
        # Steam + L3 → Play/Pause. Skipped when lstick_click has a guide bind.
        l3_now = bool(sci.buttons & SCButtons.L3)
        if steam_now and l3_now and not self._l3_was_pressed:
            if int(SCButtons.L3) not in self._guide_bind_bits:
                self._chord.kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
                self._chord.kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
        self._l3_was_pressed = l3_now

        x = sci.lstick_x
        y = sci.lstick_y  # positive = up (same hardware sign as the pads)
        zone = "NEUTRAL"
        # Skip zone detection during Steam hold when guide lstick zones are bound
        # (_handle_guide_lstick handles those instead).
        if steam_now and not self._guide_lstick_zones and (
                abs(x) > self.STICK_DEADZONE or abs(y) > self.STICK_DEADZONE):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        key = self._MEDIA_KEYS.get(zone)

        fire = False
        is_edge = False
        if zone != self._stick_zone_prev:
            # Edge fires once (the tap), then wait STICK_HOLD_DELAY before any
            # rapid repeat  so a tap or sub-second hold is exactly one step.
            fire = zone != "NEUTRAL"
            is_edge = fire
            self._stick_repeat_at = now + self.STICK_HOLD_DELAY
        elif zone in ("UP", "DOWN") and now >= self._stick_repeat_at:
            # Held past the delay: volume ramps fast. Track skip never repeats.
            fire = True
            self._stick_repeat_at = now + self.STICK_VOL_REPEAT
        self._stick_zone_prev = zone

        if fire and key is not None:
            self._chord.kb.pressEvent([key])
            self._chord.kb.releaseEvent([key])
            # Haptic tick on a volume TAP only (one 2% step)  not the rapid
            # hold-ramp, and not track skip (left/right). Gated by the global
            # haptics switch.
            if is_edge and zone in ("UP", "DOWN") and adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_click()

    def _fire_directional(self, action):
        """Fire a stick/d-pad directional action: tap a key, fire a combo, or
        scroll. Click/hold/none/show_keyboard aren't meaningful for a held
        direction, so they're ignored."""
        typ = action[0]
        if typ == "tap":
            self._chord.kb.pressEvent([action[1]])
            self._chord.kb.releaseEvent([action[1]])
        elif typ == "combo":
            for k in action[1]:
                self._chord.kb.pressEvent([k])
            for k in reversed(action[1]):
                self._chord.kb.releaseEvent([k])
        elif typ == "scroll":
            self._chord.mouse.scroll(0, action[1])

    def _handle_mouse_lstick(self, sci, now):
        """Left stick moves the mouse cursor (lstick_mouse mode). Same velocity
        curve as the right-stick mouse; disabled in gamepad mode."""
        dt = now - self._lmouse_last_t if self._lmouse_last_t else 0.0
        self._lmouse_last_t = now
        x = sci.lstick_x
        y = sci.lstick_y
        mag = (x * x + y * y) ** 0.5
        if mag <= self.MOUSE_DEADZONE:
            self._lmouse_acc_x = 0.0
            self._lmouse_acc_y = 0.0
            return
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0
        m = min(1.0, (mag - self.MOUSE_DEADZONE) / (32767.0 - self.MOUSE_DEADZONE))
        unit = self.MOUSE_MIN + (1.0 - self.MOUSE_MIN) * (m ** self.MOUSE_EXPONENT)
        scaled = unit / mag
        speed = self.MOUSE_SPEED * adusk_state.get_sc_mouse_speed()
        self._lmouse_acc_x += (x * scaled) * speed * dt
        self._lmouse_acc_y += -(y * scaled) * speed * dt
        mvx = int(self._lmouse_acc_x)
        mvy = int(self._lmouse_acc_y)
        self._lmouse_acc_x -= mvx
        self._lmouse_acc_y -= mvy
        if mvx or mvy:
            self._chord.mouse.move(mvx, mvy)

    def _handle_arrow_stick(self, sci, steam_now, now):
        """Desktop mode: left stick → its bound per-direction action (dominant
        axis), one per deflection then auto-repeating while held. Defaults to the
        arrow keys; rebindable via the picker (self._lstick_actions). Disabled in
        gamepad mode (the stick is the analog stick) and while Steam is held
        (that's the media chord)."""
        active = self._gamepad is None and not steam_now
        x = sci.lstick_x
        y = sci.lstick_y  # positive = up (same hardware sign as the pads)
        zone = "NEUTRAL"
        if active and (abs(x) > self.STICK_DEADZONE
                       or abs(y) > self.STICK_DEADZONE):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        action = self._lstick_actions.get(zone)

        fire = False
        if zone != self._arrow_zone_prev:
            # New direction (or release): the press fires immediately, then we
            # wait ARROW_HOLD_DELAY before the first repeat.
            fire = zone != "NEUTRAL"
            self._arrow_repeat_at = now + self.ARROW_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._arrow_repeat_at:
            fire = True
            self._arrow_repeat_at = now + self.ARROW_REPEAT
        self._arrow_zone_prev = zone

        if fire and action is not None and action[0] != "none":
            self._fire_directional(action)

    def _handle_arrow_rstick(self, sci, steam_now, now):
        """Right stick → discrete key presses (directional mode). Mirrors
        _handle_arrow_stick; only active when rstick_mouse is False."""
        active = self._gamepad is None and not steam_now
        x = sci.rstick_x
        y = sci.rstick_y
        zone = "NEUTRAL"
        if active and (abs(x) > self.STICK_DEADZONE or abs(y) > self.STICK_DEADZONE):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        action = self._rstick_actions.get(zone)

        fire = False
        if zone != self._rstick_zone_prev:
            fire = zone != "NEUTRAL"
            self._rstick_repeat_at = now + self.ARROW_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._rstick_repeat_at:
            fire = True
            self._rstick_repeat_at = now + self.ARROW_REPEAT
        self._rstick_zone_prev = zone

        if fire and action is not None and action[0] != "none":
            self._fire_directional(action)

    def _handle_mouse_stick(self, sci, now):
        """Right stick moves the mouse cursor. Velocity scales with deflection
        past the deadzone (with an exponent for fine control), integrated over
        real elapsed time so the speed is frame-rate independent. The caller
        gates *when* this runs: every frame in desktop mode, and only during a
        Steam/"..." hold in gamepad mode (XInput is paused then, so the right
        stick is free to act as a mouse  mirroring the Steam+trackpad latch).
        """
        dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
        self._mouse_last_t = now

        x = sci.rstick_x
        y = sci.rstick_y  # positive = up
        mag = (x * x + y * y) ** 0.5
        if mag <= self.MOUSE_DEADZONE:
            # Idle: reset accumulators so a fresh push starts clean, and don't
            # carry a stale dt forward.
            self._mouse_acc_x = 0.0
            self._mouse_acc_y = 0.0
            return
        # Clamp dt so a pause between reports (or the first frame) can't fling
        # the cursor; assume a typical ~60 Hz frame if it's out of range.
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0

        # RADIAL speed: apply the curve to the stick's DISTANCE from center, then
        # move along its unit direction, so a diagonal push is as fast as a pure
        # horizontal/vertical one. (Per-axis exponent made diagonals much slower,
        # very visible at high exponents.)
        m = min(1.0, (mag - self.MOUSE_DEADZONE) / (32767.0 - self.MOUSE_DEADZONE))
        unit = self.MOUSE_MIN + (1.0 - self.MOUSE_MIN) * (m ** self.MOUSE_EXPONENT)
        scaled = unit / mag
        # Screen Y grows downward, so stick-up (positive y) moves up (-dy).
        # "Pointer Speed" (tray Steam Controller menu) scales the base px/sec,
        # matching the OSK right-stick mouse so the pointer feels the same
        # whether the keyboard is open or closed.
        speed = self.MOUSE_SPEED * adusk_state.get_sc_mouse_speed()
        self._mouse_acc_x += (x * scaled) * speed * dt
        self._mouse_acc_y += -(y * scaled) * speed * dt
        mvx = int(self._mouse_acc_x)
        mvy = int(self._mouse_acc_y)
        self._mouse_acc_x -= mvx
        self._mouse_acc_y -= mvy
        if mvx or mvy:
            self._chord.mouse.move(mvx, mvy)

    def _handle_gamepad_mouse_clicks(self, sc, sci, steam_now):
        """Gamepad mode: while Steam/"..." is held (the right-stick / trackpad
        mouse mode), L2 → left click and R2 → right click  injected as real
        mouse buttons, since firmware lizard isn't driving them during the hold.
        Press/release (held while the trigger is) so it can drag-select.
        Reconciled every gamepad-mode frame so releasing Steam OR the trigger
        releases the button. A press edge gets the same haptic snap as the
        desktop-mode trigger click.

        Suppressed while the Steam+trackpad lizard mouse latch is on: there the
        firmware already injects L2/R2 clicks, so injecting again would
        double-fire. This is the right-stick mouse path's clicks."""
        allow = steam_now and not self._gamepad_lizard_on
        want_left = allow and bool(sci.buttons & SCButtons.LT)
        want_right = allow and bool(sci.buttons & SCButtons.RT)
        if want_left != self._chord.mouse_left_held:
            if want_left:
                self._chord.mouse.press("left")
                if adusk_state.is_rumble_enabled(self._kind):
                    sc.haptic_click()
            else:
                self._chord.mouse.release("left")
            self._chord.mouse_left_held = want_left
        if want_right != self._chord.mouse_right_held:
            if want_right:
                self._chord.mouse.press("right")
                if adusk_state.is_rumble_enabled(self._kind):
                    sc.haptic_click()
            else:
                self._chord.mouse.release("right")
            self._chord.mouse_right_held = want_right

    def _handle_chords(self, sc, sci, steam_now):
        """Fire two-button Hotkeys chords. A chord fires once on the rising edge
        of BOTH its buttons being held (and only when Steam isn't held, so the
        built-in Steam chords win). Mode-scoped by `is_gamepad` (see
        keybinds_runtime.build_chords): a chord built from green "xi_" Gamepad-
        Layout aliases only fires in Gamepad Mode; a chord built from physical
        button ids only fires in Desktop Mode  otherwise both flavors would
        fire everywhere at once, which is confusing (a physical L1+R1 hotkey
        firing mid-game, or a "Select"+"Start" hotkey firing on the desktop
        where those XInput identities don't even apply). Returns a bitmask of
        the buttons belonging to currently-held, IN-SCOPE chords so the caller
        can mask them out of the single-button handlers (otherwise A+B would
        also fire A's and B's own actions)."""
        suppress = 0
        gamepad_mode = self._gamepad is not None
        for i, (mask, action, is_gamepad) in enumerate(self._chords_runtime):
            if is_gamepad != gamepad_mode:
                self._chord_was_active[i] = False
                continue
            active = (not steam_now) and ((sci.buttons & mask) == mask)
            if active:
                suppress |= mask
                if not self._chord_was_active[i]:
                    self._fire_chord(sc, action)
            self._chord_was_active[i] = active
        return suppress

    def _fire_chord(self, sc, action):
        """Run a chord's action: a key combo (press modifiers+key, release in
        reverse) or launch a program/script. A short haptic confirms the fire."""
        try:
            if action["type"] == "keys":
                keys = action["keys"]
                for k in keys:
                    self._chord.kb.pressEvent([k])
                for k in reversed(keys):
                    self._chord.kb.releaseEvent([k])
            elif action["type"] == "launch":
                _launch_program(action["path"], action.get("args", ""))
        except Exception as e:
            print(f"chord fire failed: {e!r}")
        if adusk_state.is_rumble_enabled(self._kind):
            sc.haptic_click()

    def _handle_overrides(self, sc, raw_buttons, steam_now):
        """Dispatch per-control desktop rebinds (controls the user changed from
        their default). Returns a bitmask of the overridden controls so the
        caller masks them out of the hardcoded handlers below. Gated to non-Steam
        so the built-in Steam chords keep the raw buttons; while Steam is held
        every override is treated as released (so holds/clicks don't strand)."""
        suppress = 0
        for cid, bit, action in self._sc_overrides:
            pressed = (not steam_now) and bool(raw_buttons & bit)
            if not steam_now:
                suppress |= bit
            typ = action[0]
            if typ == "click":
                # True held click (drag-friendly)  not momentary.
                self._chord.set_mouse_button(action[1], "ov:" + cid, pressed)
            elif typ == "hold":
                # True held modifier  pressed while the button is held.
                self._chord.set_key(action[1], "ov:" + cid, pressed,
                                    kind=self._kind)
            elif typ != "none":
                # Everything else (tap / combo / scroll / show-keyboard / media /
                # system action) fires once per press via the shared dispatcher.
                if pressed and not self._ov_prev.get(cid, False):
                    self._fire_guide_action(sc, action)
                self._ov_prev[cid] = pressed
        return suppress

    def _handle_gamepad_key_overrides(self, sc, raw_buttons, guide_now):
        """GAMEPAD-mode counterpart of _handle_overrides: dispatch SC gamepad
        controls the user bound to a keyboard/mouse/system action (the Gamepad
        tab now also offers the desktop vocabulary). These controls are already
        excluded from the XInput button_map (resolve_sc_gamepad), so no sci
        masking is needed  they simply also inject their key/click here.
        Gated on NOT guide_now so Steam-held gaming chords keep priority; while
        Steam is held every override is treated as released so holds/clicks
        don't strand. Uses "gpk:" holder keys, distinct from desktop "ov:"."""
        for cid, bit, action in self._gp_key_overrides:
            pressed = (not guide_now) and bool(raw_buttons & bit)
            typ = action[0]
            if typ == "click":
                self._chord.set_mouse_button(action[1], "gpk:" + cid, pressed)
            elif typ == "hold":
                self._chord.set_key(action[1], "gpk:" + cid, pressed,
                                    kind=self._kind)
            elif typ != "none":
                if pressed and not self._gp_key_prev.get(cid, False):
                    self._fire_guide_action(sc, action, mode="gamepad")
                self._gp_key_prev[cid] = pressed

    def _apply_adv_specs(self, sc, asserted, mode="gamepad"):
        """Apply this frame's AdvPressEngine decisions. `asserted` = {slot:
        spec} from engine.step(). XUSB specs OR their flag into
        self._adv_xflags (read by the gamepad branch's update call); key specs
        press on appearance / release on disappearance (click + hold), or fire
        once on appearance (tap / combo / system)  the same semantics as
        _handle_gamepad_key_overrides, under "adv:" holder keys. `mode` names
        the tab whose engine is running ("gamepad"/"pc")  it scopes system
        actions like the profile-slot cycle."""
        self._adv_xflags = 0
        gamepad = self._gamepad
        for slot, spec in asserted.items():
            if spec[0] == "xusb":
                if gamepad is not None:
                    self._adv_xflags |= gamepad.action_flag(spec[1])
            elif slot not in self._adv_prev:
                action = spec[1]
                typ = action[0]
                if typ == "click":
                    self._chord.set_mouse_button(action[1], "adv:" + slot, True)
                elif typ == "hold":
                    self._chord.set_key(action[1], "adv:" + slot, True,
                                        kind=self._kind)
                elif typ != "none":
                    self._fire_guide_action(sc, action, mode=mode)
        for slot, spec in self._adv_prev.items():
            if slot in asserted or spec[0] != "key":
                continue
            action = spec[1]
            if action[0] == "click":
                self._chord.set_mouse_button(action[1], "adv:" + slot, False)
            elif action[0] == "hold":
                self._chord.set_key(action[1], "adv:" + slot, False,
                                    kind=self._kind)
        self._adv_prev = asserted

    def _handle_button_combos(self, sc, sci, steam_now, guide_now):
        """Hotkeys "Button Combo" effects: while a trigger is HELD, hold the
        selected outputs. A GUIDE trigger (cb["guide"]) fires while Steam/"..."
        is held + the other button, in BOTH modes; a normal trigger is mode-
        scoped like _handle_chords (is_gamepad → Gamepad Mode, physical → Desktop)
        and gated on `not steam_now` so the built-in Steam chords win. Sets
        self._combo_extra (XUSB flags to OR into the pad this frame  read in the
        gamepad branch, so this MUST run before gamepad.update) and
        self._combo_suppress (trigger bits to mask out of the single-button
        handlers). Xbox outputs only take effect in Gamepad Mode (there's a pad);
        keyboard/mouse/system outputs are held/edge-fired in either mode."""
        self._combo_extra = 0
        self._combo_suppress = 0
        gamepad_mode = self._gamepad is not None
        for i, cb in enumerate(self._button_combos):
            if cb["guide"]:
                # Guide alone (mask 0) fires whenever Steam/"..." is held; Guide
                # + button additionally requires that button.
                held = guide_now and (cb["mask"] == 0
                                      or bool(sci.buttons & cb["mask"]))
                combo_mode = "guide"
            else:
                if cb["is_gamepad"] != gamepad_mode:
                    self._release_combo_keys(i, cb)
                    self._combo_was[i] = False
                    continue
                held = (not steam_now) and ((sci.buttons & cb["mask"]) == cb["mask"])
                combo_mode = "gamepad" if cb["is_gamepad"] else "pc"
            rising = held and not self._combo_was[i]
            if held:
                self._combo_suppress |= cb["mask"]
                if gamepad_mode:
                    self._combo_extra |= cb["xflags"]
                if rising and adusk_state.is_rumble_enabled(self._kind):
                    sc.haptic_click()
            for j, action in enumerate(cb["key_actions"]):
                typ = action[0]
                holder = "bc:%d:%d" % (i, j)
                if typ == "click":
                    self._chord.set_mouse_button(action[1], holder, held)
                elif typ == "hold":
                    self._chord.set_key(action[1], holder, held,
                                        kind=self._kind)
                elif typ != "none" and rising:
                    self._fire_guide_action(sc, action, mode=combo_mode)  # tap/combo/system: once
            self._combo_was[i] = held

    def _release_combo_keys(self, i, cb):
        """Drop any held click/modifier holders for an out-of-scope combo (e.g.
        after a mode switch) so nothing strands pressed."""
        if not self._combo_was[i]:
            return
        for j, action in enumerate(cb["key_actions"]):
            holder = "bc:%d:%d" % (i, j)
            if action[0] == "click":
                self._chord.set_mouse_button(action[1], holder, False)
            elif action[0] == "hold":
                self._chord.set_key(action[1], holder, False,
                                    kind=self._kind)

    def _handle_guide_binds(self, sc, sci):
        """Fire guide-hold binds (Steam+button → action) from the picker's Chords
        tab. Fires once per rising edge of each button while Steam is held.
        Falling edge resets one-shot latches so the action can re-trigger on the
        next press (relevant for force_kill / power_off which use latches)."""
        b = sci.buttons
        for bit, action in self._guide_binds:
            pressed = bool(b & bit)
            was = self._guide_binds_prev.get(bit, False)
            if pressed and not was:
                self._fire_guide_action(sc, action, mode="guide")
            elif not pressed and was:
                typ = action[0]
                if typ == "force_kill":
                    self._force_kill_done = False
                elif typ == "power_off":
                    self._powered_off = False
            self._guide_binds_prev[bit] = pressed

    def _handle_guide_chords(self, sc, sci, guide_now):
        """Fire Guide (Steam-held) chords from the Hotkeys tab  a key combo or a
        program launch. Guide + button fires once per rising edge of the other
        button while Steam is held; Guide ALONE (bit 0) fires once per Steam/"..."
        hold. Called every frame (keyed by index) so the edge resets when Steam or
        the button is released. The conflicting Chords-tab bind was cleared in the
        picker, so only the chord fires."""
        b = sci.buttons
        for i, (bit, action) in enumerate(self._guide_chords):
            pressed = guide_now and (True if bit == 0 else bool(b & bit))
            if pressed and not self._guide_chords_prev.get(i, False):
                self._fire_chord(sc, action)
            self._guide_chords_prev[i] = pressed

    def _handle_guide_rstick(self, sc, sci):
        """Fire right-stick directional guide binds on zone entry while Steam is
        held. Fires once per zone transition (no auto-repeat)."""
        x = sci.rstick_x
        y = sci.rstick_y
        zone = "NEUTRAL"
        if abs(x) > self.STICK_DEADZONE or abs(y) > self.STICK_DEADZONE:
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"
        if zone != self._guide_rstick_zone_prev and zone != "NEUTRAL":
            action = self._guide_rstick_zones.get(zone)
            if action:
                self._fire_guide_action(sc, action, mode="guide")
        self._guide_rstick_zone_prev = zone

    def _handle_guide_lstick(self, sc, sci):
        """Fire left-stick directional guide binds on zone entry while Steam is
        held. Fires once per zone transition (no auto-repeat)."""
        x = sci.lstick_x
        y = sci.lstick_y
        zone = "NEUTRAL"
        if abs(x) > self.STICK_DEADZONE or abs(y) > self.STICK_DEADZONE:
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"
        if zone != self._guide_lstick_zone_prev and zone != "NEUTRAL":
            action = self._guide_lstick_zones.get(zone)
            if action:
                self._fire_guide_action(sc, action, mode="guide")
        self._guide_lstick_zone_prev = zone

    def _fire_guide_action(self, sc, action, mode="pc"):
        """Dispatch a single edge-triggered action (one press = one fire). Shared
        by guide-hold binds, the Steam/"..." TAP, and the desktop per-control
        override edge path, so all three understand the full action vocabulary.
        click/hold here are momentary (press+release); the desktop override path
        handles true held click/modifier separately before delegating here.
        `mode` names which tab ("pc"/"gamepad"/"guide") this dispatch's binding
        lives in  read by the "profile_cycle" action so ONE dropdown entry
        cycles whichever mode was actually active when it fired.

        `sc` may be None when the dispatch has no controller behind it at all
        (a keyboard/mouse-triggered Virtual Menu  see _KeyVMenuRunner); the
        two actions that talk to the hardware degrade accordingly."""
        typ = action[0]
        if typ == "tap":
            self._chord.kb.pressEvent([action[1]])
            self._chord.kb.releaseEvent([action[1]])
        elif typ == "combo":
            for k in action[1]:
                self._chord.kb.pressEvent([k])
            for k in reversed(action[1]):
                self._chord.kb.releaseEvent([k])
        elif typ == "click":
            self._chord.mouse.press(action[1])
            self._chord.mouse.release(action[1])
        elif typ == "hold":
            if action[1] == keybinds_runtime.GYRO_MOUSE_KEY:
                # "Gyro To Mouse" reached an EDGE-only dispatch site (a stick
                # zone, a Steam/"..." tap) where there is no button-up to end a
                # hold on, so the press flips it (see gyro_action_flip).
                gyro_action_flip(self._kind)
            else:
                self._chord.kb.pressEvent([action[1]])
                self._chord.kb.releaseEvent([action[1]])
        elif typ == "scroll":
            self._chord.mouse.scroll(0, action[1])
        elif typ == "toggle_magnifier":
            import subprocess
            _NO_WIN = subprocess.CREATE_NO_WINDOW
            try:
                r = subprocess.run(
                    ["tasklist", "/fi", "imagename eq Magnify.exe", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, timeout=2,
                    creationflags=_NO_WIN)
                if "Magnify.exe" in r.stdout:
                    subprocess.Popen(["taskkill", "/f", "/im", "Magnify.exe"],
                                     creationflags=_NO_WIN)
                else:
                    subprocess.Popen(["Magnify.exe"], creationflags=_NO_WIN)
            except Exception as e:
                print(f"toggle_magnifier failed: {e!r}")
        elif typ == "show_keyboard":
            if not _workstation_locked():
                if sc is None:
                    # No controller behind this dispatch  go through the same
                    # App entry point the Win+Ctrl+O global hotkey uses instead
                    # of the input loop's exit-and-open handshake.
                    if self._on_show_keyboard is not None:
                        self._on_show_keyboard()
                else:
                    self.triggered = True
                    sc.addExit()
        elif typ == "power_off":
            # Powering the controller off needs the controller (no-op without).
            if sc is not None and not self._powered_off:
                self._powered_off = True
                sc.turn_off()
        elif typ == "force_kill":
            if not self._force_kill_done:
                self._force_kill_done = True
                _force_kill_foreground_game()
        elif typ == "alt_tab":
            # Hold Alt across repeated presses (don't release it here) so the
            # switcher UI stays open and each subsequent press just taps Tab to
            # cycle  mirrors the hardcoded Steam+VIEW behavior. Alt is released
            # generically when the guide hold ends (see "if not guide_now:
            # self._chord.release_alt()" near the top of on_input), regardless
            # of which button dispatched this.
            if not self._chord.alt_held:
                self._chord.kb.pressEvent([sui.Keys.KEY_LEFTALT])
                self._chord.alt_held = True
            self._chord.kb.pressEvent([sui.Keys.KEY_TAB])
            self._chord.kb.releaseEvent([sui.Keys.KEY_TAB])
        elif typ == "xbutton":
            # Page Previous/Next: mouse Back/Forward (XBUTTON1/2)  same
            # injection "Swipe Between Pages" uses, honored by browsers, File
            # Explorer and Windows Settings/Control Panel.
            try:
                u = ctypes.windll.user32
                u.mouse_event(0x0080, 0, 0, action[1], 0)   # MOUSEEVENTF_XDOWN
                u.mouse_event(0x0100, 0, 0, action[1], 0)   # MOUSEEVENTF_XUP
            except Exception:
                pass
        elif typ in ("brightness_up", "brightness_down"):
            # Internal-panel brightness in ±10% steps. Queued to the coalescing
            # brightness worker (native power-scheme API, no subprocess) so the
            # input loop never blocks and spamming can't storm processes  see
            # _brightness_request.
            _brightness_request(10 if typ == "brightness_up" else -10)
        elif typ == "lock_pc":
            # Lock the workstation. LockWorkStation() is the reliable API  an
            # injected Win+L is swallowed by the OS shell.
            try:
                ctypes.windll.user32.LockWorkStation()
            except Exception as e:
                print(f"lock_pc failed: {e!r}")
        elif typ == "screen_off":
            # Toggle: press once = off, press again = back on. WM_SYSCOMMAND /
            # SC_MONITORPOWER broadcast to all top-level windows (2 = off,
            # -1 = on  the message is delivered via the OS message queue, so it
            # reaches windows regardless of the physical display state). A
            # zero-delta synthetic mouse move rides along on "on" as a guaranteed
            # OS-level wake signal, since some drivers/multi-monitor setups don't
            # fully wake from the SC_MONITORPOWER message alone.
            self._screen_off = not self._screen_off
            try:
                if self._screen_off:
                    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
                else:
                    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, -1)
                    ctypes.windll.user32.mouse_event(0x0001, 0, 0, 0, 0)
            except Exception as e:
                print(f"screen_off failed: {e!r}")
        elif typ == "sleep_pc":
            # Enter the configured sleep mode (console-style standby button).
            # Edge-triggered dispatch = one fire per press; the suspend runs on
            # its own thread so the input loop isn't wedged through the
            # transition.
            _sleep_now_async()
        elif typ == "shutdown_pc":
            _shutdown_now()
        elif typ == "profile_cycle":
            # Advance the Steam Controller's active profile slot for whichever
            # tab this binding's dispatch site is in (`mode`, passed by the
            # caller) to the next existing one (wraps to 1). Edge-triggered
            # dispatch = one advance per press. This watcher is SC-only.
            if self._on_profile_cycle is not None:
                self._on_profile_cycle(self._kind, mode)
        elif typ == "toggle_gui":
            # Open/close the config GUI (default Guide-button tap). The App owns
            # the picker + game-focus restore; hop off the input thread so a slow
            # first build never stalls input handling.
            if self._on_toggle_gui is not None:
                self._on_toggle_gui()
        elif typ == "gamepad_mode_toggle":
            # Flip Desktop <-> Gamepad controls. Edge-triggered = one flip per
            # press; the same App call the Hotkeys toggle chords make.
            gamepad_mode_flip()
        elif typ == "big_picture":
            # Open Big Picture, or leave it if it's already up.
            big_picture_toggle_async()

    # Max press→release time (s) for a Steam/"..." TAP. Longer holds are treated
    # as a (possibly chord) hold and never fire the tap rebind.
    _GUIDE_TAP_S = 0.28

    def _handle_guide_taps(self, sc, sci, now, taps=None):
        """Steam / "..." (QAM) TAP → bound action. Fires only on a clean tap: a
        short press+release with NO other button touched during the hold, so the
        held Steam/"..." chords are untouched. `taps` selects the {cid: action}
        map to fire (the pc binds in desktop mode, the gamepad binds while a
        virtual pad is driven  the Guide button toggles the config GUI in both
        by default). No-op when neither guide button is bound."""
        if taps is None:
            taps = self._guide_taps
        GUIDE = SCButtons.STEAM | SCButtons.QAM
        # Passive/capacitive sensors must NOT count as "another button" or a
        # normal tap would never fire: a resting thumb on a pad/stick, and  the
        # important one  the always-on grip-rest sensors, which are set simply by
        # holding the controller. Without excluding the grips, every tap is flagged
        # as a chord and cancelled (the rebind never fires).
        TOUCH = (SCButtons.RPADTOUCH | SCButtons.LPADTOUCH
                 | SCButtons.RPADJOY_TOUCH | SCButtons.LPADJOY_TOUCH
                 | SCButtons.RGRIP_REST | SCButtons.LGRIP_REST)
        raw = sci.buttons & GUIDE
        held = bool(raw)
        if self._guide_prev is None:
            # First frame of this watcher's life. A guide button already down
            # here belongs to a press this object never saw start (closing the
            # OSK with Steam/QAM held rebuilds the reader mid-hold)  adopt it
            # as an in-progress, already-chorded hold so the release that ends
            # it fires nothing. Without this, letting go right after the OSK
            # closed fired the tap action and popped the config GUI open.
            self._guide_prev = held
            self._guide_other = held
            self._guide_bits = raw
            self._guide_press_t = now
            return
        if held and not self._guide_prev:                 # rising edge
            self._guide_press_t = now
            self._guide_other = False
            self._guide_bits = raw
        elif held:                                        # during the hold
            if sci.buttons & ~(GUIDE | TOUCH):
                self._guide_other = True                  # a chord  cancel the tap
            self._guide_bits |= raw
        elif self._guide_prev:                            # falling edge
            if (now - self._guide_press_t) <= self._GUIDE_TAP_S and not self._guide_other:
                cid = "steam" if (self._guide_bits & SCButtons.STEAM) else "qam"
                mode = "gamepad" if self._gamepad is not None else "pc"
                self._dispatch_guide_tap(sc, taps.get(cid), mode)
        self._guide_prev = held

    def _dispatch_guide_tap(self, sc, action, mode="pc"):
        if not action or action[0] == "none":
            return
        if sc_viewer.tutorial_claimed():
            # The first-run tour holds the Guide button open as a chord
            # modifier all the way through, and the default tap action closes
            # the config GUI  i.e. deletes the tour. Nothing is worth a tap
            # while it is up: mistiming a chord must not end the tutorial.
            return
        self._fire_guide_action(sc, action, mode=mode)

    def _handle_dpad_arrows(self, sci, steam_now, now):
        """Desktop: D-pad → arrow keys, tap-then-repeat (same cadence as the
        left-stick arrows). Firmware lizard drove this before; in takeover we do.
        Gated to desktop + no-Steam. Priority up>down>left>right for diagonals."""
        active = self._gamepad is None and not steam_now
        b = sci.buttons
        zone = "NEUTRAL"
        if active:
            if b & SCButtons.DPAD_UP:
                zone = "UP"
            elif b & SCButtons.DPAD_DOWN:
                zone = "DOWN"
            elif b & SCButtons.DPAD_LEFT:
                zone = "LEFT"
            elif b & SCButtons.DPAD_RIGHT:
                zone = "RIGHT"
        key = self._ARROW_KEYS.get(zone)
        fire = False
        if zone != self._dpad_zone_prev:
            fire = zone != "NEUTRAL"
            self._dpad_repeat_at = now + self.ARROW_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._dpad_repeat_at:
            fire = True
            self._dpad_repeat_at = now + self.ARROW_REPEAT
        self._dpad_zone_prev = zone
        if fire and key is not None:
            self._chord.kb.pressEvent([key])
            self._chord.kb.releaseEvent([key])

    def _handle_pinch_zoom(self, sci, now):
        """Pinch To Zoom: press EACH trackpad HARD (a physical click on both,
        gated in on_input on SCButtons.LPAD+RPAD) and it drives the fullscreen
        Magnification API like a two-finger pinch. OPPOSITE horizontal motion
        (the pads spreading apart / coming together) zooms  mapped LINEARLY
        from spread distance to the scale float (no curve), anchored at the
        zoom level the gesture started at, so it's position-mapped: lift and
        the zoom HOLDS. COMMON motion pans the zoomed view like touch-dragging
        the desktop (fingers down = view up, per the user's spec)  both
        happen simultaneously from the same two deltas, exactly like a real
        pinch. Each touch point gets the SAME smoothing as the right-pad
        mouse: a 1€ filter (speed-adaptive low-pass, PAD_EURO_*  constant
        silky smoothing with a touch of lag through normal motion, cutoff
        opening on a fast move) plus a trailing dead-zone anchor  the
        EFFECTIVE point the zoom math sees only advances by the overshoot
        past PAD_DEADZONE, and at REST the anchor eases back onto the
        filtered point (full slack restored)  so resting/tremoring thumbs
        give ZERO zoom/pan drift while real motion ramps in smoothly."""
        lx, ly = float(sci.lpad_x), float(sci.lpad_y)
        rx, ry = float(sci.rpad_x), float(sci.rpad_y)
        if self._pinch_filt is None:
            # Fresh two-finger contact: seed the filters, trailing dead-zone
            # anchors and effective points AT the touch positions so the
            # first frame can't jump the zoom.
            self._pinch_filt = [lx, ly, rx, ry]
            self._pinch_dfilt = [0.0, 0.0, 0.0, 0.0]
            self._pinch_dz = [lx, ly, rx, ry]
            self._pinch_eff = [lx, ly, rx, ry]
            self._pinch_prev = (lx, ly, rx, ry)
            self._pinch_last_t = now
        else:
            dt = now - self._pinch_last_t if self._pinch_last_t else 0.0
            if dt <= 0.0 or dt > 0.1:
                dt = 1.0 / 60.0
            self._pinch_last_t = now
            f = self._pinch_filt
            df = self._pinch_dfilt
            dz = self._pinch_dz
            eff = self._pinch_eff
            prev = self._pinch_prev
            ad = dt / (1.0 / (2.0 * math.pi * self.PAD_EURO_DCUTOFF) + dt)
            for i, (px, py) in enumerate(((lx, ly), (rx, ry))):
                xi, yi = i * 2, i * 2 + 1
                # 1€ filter (same as the right-pad cursor): low-pass this
                # pad's raw velocity to a stable speed estimate, then open
                # the position cutoff with that speed  silky constant
                # smoothing through normal pinch motion, near-raw on a fast
                # spread so the zoom never feels stuck.
                df[xi] += ad * ((px - prev[xi]) / dt - df[xi])
                df[yi] += ad * ((py - prev[yi]) / dt - df[yi])
                pad_speed = (df[xi] * df[xi] + df[yi] * df[yi]) ** 0.5
                fc = self.PAD_EURO_MINCUTOFF + self.PAD_EURO_BETA * pad_speed
                a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
                f[xi] += a * (px - f[xi])
                f[yi] += a * (py - f[yi])
                # Trailing dead-zone: the effective point advances only by
                # the filtered point's OVERSHOOT past PAD_DEADZONE, and the
                # anchor re-trails at exactly the radius  still fingers
                # inside it move nothing.
                ddx = f[xi] - dz[xi]
                ddy = f[yi] - dz[yi]
                dist = (ddx * ddx + ddy * ddy) ** 0.5
                if dist > self.PAD_DEADZONE:
                    over = dist - self.PAD_DEADZONE
                    ux, uy = ddx / dist, ddy / dist
                    eff[xi] += over * ux
                    eff[yi] += over * uy
                    dz[xi] = f[xi] - ux * self.PAD_DEADZONE
                    dz[yi] = f[yi] - uy * self.PAD_DEADZONE
                elif pad_speed < self.PAD_REST_SPEED:
                    # Resting inside the dead-zone: ease the anchor back onto
                    # the filtered point so the full radius of slack returns
                    # around the still thumb (post-move it sat pinned ON the
                    # edge  the next tremor blip leaked as zoom/pan wiggle).
                    ra = dt / (self.PAD_ANCHOR_RECENTER_TAU + dt)
                    dz[xi] += ra * (f[xi] - dz[xi])
                    dz[yi] += ra * (f[yi] - dz[yi])
            self._pinch_prev = (lx, ly, rx, ry)
        flx, fly, frx, fry = self._pinch_eff
        try:
            u = ctypes.windll.user32
            sw = float(u.GetSystemMetrics(0))
            sh = float(u.GetSystemMetrics(1))
        except Exception:
            return
        if not sw or not sh:
            return
        if self._zoom_cx is None:
            self._zoom_cx = sw / 2.0
            self._zoom_cy = sh / 2.0
        if self._pinch_anchor is None:
            # Engage: anchor both fingers, the current zoom, AND the mouse
            # cursor as the zoom's FOCAL POINT  its desktop position plus
            # where it currently sits on screen. While the scale changes,
            # the view center is derived so that desktop point stays pinned
            # to that same on-screen spot: the zoom converges on the cursor
            # (like wheel-zoom in an image editor), not the screen center.
            try:
                pcx, pcy = self._chord.mouse.get_position()
            except Exception:
                pcx, pcy = int(self._zoom_cx), int(self._zoom_cy)
            s0 = self._zoom_scale
            sx = (pcx - (self._zoom_cx - sw / (2.0 * s0))) * s0
            sy = (pcy - (self._zoom_cy - sh / (2.0 * s0))) * s0
            self._pinch_anchor = (flx, fly, frx, fry, s0,
                                  float(pcx), float(pcy), sx, sy)
            return
        alx, aly, arx, ary, ascale, pcx, pcy, sx, sy = self._pinch_anchor
        spread = (frx - arx) - (flx - alx)  # >0 = fingers moving apart
        scale = ascale + spread * self.PINCH_SCALE_PER_UNIT
        scale = max(1.0, min(self.PINCH_ZOOM_MAX, scale))
        # Zoom toward the CURSOR: center derived so the desktop point that
        # was under the pointer keeps its on-screen position at any scale.
        czx = pcx + (sw / 2.0 - sx) / scale
        czy = pcy + (sh / 2.0 - sy) / scale
        # Pan by the fingers' COMMON motion (their average delta): the zoomed
        # content follows the fingers, so the view center moves opposite the
        # drag. Pad Y grows upward, screen Y grows downward  dragging DOWN
        # (negative pad dy) moves the view UP. Divided by scale so pan speed
        # feels constant on screen at any zoom.
        avg_dx = ((flx - alx) + (frx - arx)) / 2.0
        avg_dy = ((fly - aly) + (fry - ary)) / 2.0
        k = (sw / 65536.0) * self.PINCH_PAN_SENS / max(scale, 1.0)
        cx = czx - avg_dx * k
        cy = czy + avg_dy * k
        vw = sw / scale
        vh = sh / scale
        cx = min(max(cx, vw / 2.0), sw - vw / 2.0)
        cy = min(max(cy, vh / 2.0), sh - vh / 2.0)
        if (scale, cx, cy) != (self._zoom_scale, self._zoom_cx, self._zoom_cy):
            self._zoom_scale = scale
            self._zoom_cx = cx
            self._zoom_cy = cy
            _mag_apply(scale, cx, cy)

    def _end_pinch_zoom(self):
        """A finger lifted: drop the gesture anchors (the ZOOM level stays 
        position-mapped, like a real pinch). Near-1x snaps to exactly 1:1 so
        an almost-fully-pinched-in desktop doesn't linger microscopically
        zoomed."""
        if self._pinch_anchor is None:
            return
        self._pinch_anchor = None
        self._pinch_filt = None
        if 1.0 < self._zoom_scale < 1.05:
            self._zoom_scale = 1.0
            _mag_apply(1.0, 0.0, 0.0)

    def _apply_pan(self, ddx, ddy, k, sw, sh, scale):
        """Move the magnifier view center by a (locked) pad-space delta 
        the same content-follows-finger mapping as the pinch pan (pad Y
        grows upward, screen Y downward), clamped to the desktop edges.
        Returns per-axis whether the center actually moved, so an
        edge-pinned coast axis can be killed instead of grinding the clamp."""
        cx = self._zoom_cx - ddx * k
        cy = self._zoom_cy + ddy * k
        vw = sw / scale
        vh = sh / scale
        cx = min(max(cx, vw / 2.0), sw - vw / 2.0)
        cy = min(max(cy, vh / 2.0), sh - vh / 2.0)
        moved_x = cx != self._zoom_cx
        moved_y = cy != self._zoom_cy
        if moved_x or moved_y:
            self._zoom_cx = cx
            self._zoom_cy = cy
            _mag_apply(scale, cx, cy)
        return moved_x, moved_y

    def _handle_pad_pan(self, sci, now):
        """Zoomed 360° pan (macbook-style): while Pinch To Zoom holds ANY
        magnification the LEFT pad stops scrolling and pans the zoomed view
        in any direction  content follows the finger, like two-finger
        scrolling on a zoomed macbook (on_input hands the chosen scrolling
        mode back the moment the desktop is fully zoomed out). The touch
        runs the SAME pipeline as the right-pad cursor: 1€ filter
        (PAD_EURO_*) plus trailing dead-zone with resting recenter, so a
        parked thumb never wiggles the view. The live pan is ALWAYS free
        360°  no axis locking while the finger moves (the user rejected
        the during-scroll h/v lock). A fast lift throws the view: windowed
        lift velocity so one noisy lift frame can't launch it, exponential
        decay, any new touch catches it dead  and a throw aimed near an
        axis snaps dead-straight (see LPAN_THROW_LOCK_*)."""
        if self._zoom_cx is None:
            return
        try:
            u = ctypes.windll.user32
            sw = float(u.GetSystemMetrics(0))
            sh = float(u.GetSystemMetrics(1))
        except Exception:
            return
        if not sw or not sh:
            return
        scale = self._zoom_scale
        # Live camera-sensitivity slider (Touchpads, under Pinch To Zoom);
        # 0..1, default LPAN_SENS. Scales both the live pan and the throw
        # coast (both read this k), so the whole feel tracks the slider.
        sens = adusk_state.get_pinch_sensitivity()
        k = (sw / 65536.0) * sens / max(scale, 1.0)
        if not (sci.buttons & SCButtons.LPADTOUCH):
            if self._pan_filt is not None:
                # Lift edge: average the last SCROLL_VELOCITY_WINDOW of
                # travel and coast only past the trigger  a gentle stop
                # stays put, a real fling keeps the view gliding.
                self._pan_filt = None
                if len(self._pan_hist) >= 2:
                    t0, px0, py0 = self._pan_hist[0]
                    dt = now - t0
                    if dt > 1e-3:
                        vx = (self._pan_pos[0] - px0) / dt
                        vy = (self._pan_pos[1] - py0) / dt
                        v = math.hypot(vx, vy)
                        if v >= self.LPAN_FLING_TRIGGER:
                            # Throw axis snap: a near-vertical release coasts
                            # DEAD vertical, aggressively; horizontal only
                            # when the fling is clearly flat. This is the
                            # ONLY axis discipline  the live pan is free.
                            avx, avy = abs(vx), abs(vy)
                            if avx <= avy * self._LPAN_THROW_V_TAN:
                                vx = 0.0
                            elif avy <= avx * self._LPAN_THROW_H_TAN:
                                vy = 0.0
                            if v > self.LPAN_FLING_MAX:
                                vx *= self.LPAN_FLING_MAX / v
                                vy *= self.LPAN_FLING_MAX / v
                            self._pan_fling_vx = vx
                            self._pan_fling_vy = vy
                            self._pan_fling_last_t = now
                self._pan_hist.clear()
            if self._pan_fling_vx or self._pan_fling_vy:
                dt = now - self._pan_fling_last_t
                dt = max(1e-3, min(dt, 1.0 / 30.0))  # a stall can't lurch
                self._pan_fling_last_t = now
                mx, my = self._apply_pan(self._pan_fling_vx * dt,
                                         self._pan_fling_vy * dt,
                                         k, sw, sh, scale)
                decay = math.exp(-dt / self.LPAN_FLING_TAU)
                self._pan_fling_vx = self._pan_fling_vx * decay if mx else 0.0
                self._pan_fling_vy = self._pan_fling_vy * decay if my else 0.0
                if math.hypot(self._pan_fling_vx,
                              self._pan_fling_vy) < self.LPAN_FLING_STOP:
                    self._pan_fling_vx = 0.0
                    self._pan_fling_vy = 0.0
            return
        # Touching: any contact catches a coasting view dead (the gentle tap).
        self._pan_fling_vx = 0.0
        self._pan_fling_vy = 0.0
        x, y = float(sci.lpad_x), float(sci.lpad_y)
        if self._pan_filt is None:
            # Fresh touch: seed everything AT the touch point so the first
            # frame can't jump.
            self._pan_filt = [x, y]
            self._pan_dfilt = [0.0, 0.0]
            self._pan_dz = [x, y]
            self._pan_prev = (x, y)
            self._pan_last_t = now
            self._pan_pos = [0.0, 0.0]
            self._pan_hist.clear()
            return
        dt = now - self._pan_last_t if self._pan_last_t else 0.0
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0
        self._pan_last_t = now
        f, df, dz = self._pan_filt, self._pan_dfilt, self._pan_dz
        # 1€ filter  identical tuning to the right-pad cursor: the
        # low-passed speed estimate opens the position cutoff (silky
        # constant smoothing at rest/slow, near-raw on a fast swipe).
        ad = dt / (1.0 / (2.0 * math.pi * self.PAD_EURO_DCUTOFF) + dt)
        df[0] += ad * ((x - self._pan_prev[0]) / dt - df[0])
        df[1] += ad * ((y - self._pan_prev[1]) / dt - df[1])
        self._pan_prev = (x, y)
        pad_speed = math.hypot(df[0], df[1])
        fc = self.PAD_EURO_MINCUTOFF + self.PAD_EURO_BETA * pad_speed
        a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
        f[0] += a * (x - f[0])
        f[1] += a * (y - f[1])
        # Trailing dead-zone + resting recenter (same as the cursor): the
        # pan advances only by the filtered point's overshoot past
        # PAD_DEADZONE, and a resting thumb gets its slack back  zero view
        # wiggle while the finger merely rests.
        ddx = f[0] - dz[0]
        ddy = f[1] - dz[1]
        dist = math.hypot(ddx, ddy)
        ex = ey = 0.0
        if dist > self.PAD_DEADZONE:
            over = dist - self.PAD_DEADZONE
            ux, uy = ddx / dist, ddy / dist
            ex, ey = over * ux, over * uy
            dz[0] = f[0] - ux * self.PAD_DEADZONE
            dz[1] = f[1] - uy * self.PAD_DEADZONE
        elif pad_speed < self.PAD_REST_SPEED:
            ra = dt / (self.PAD_ANCHOR_RECENTER_TAU + dt)
            dz[0] += ra * (f[0] - dz[0])
            dz[1] += ra * (f[1] - dz[1])
        # Free 360° pan  content follows the finger in any direction, no
        # axis locking while moving. _pan_pos tracks the true path so the
        # lift-throw can read the real release direction (and snap it).
        if ex or ey:
            self._apply_pan(ex, ey, k, sw, sh, scale)
            self._pan_pos[0] += ex
            self._pan_pos[1] += ey
        self._pan_hist.append((now, self._pan_pos[0], self._pan_pos[1]))
        while (self._pan_hist
               and now - self._pan_hist[0][0] > self.SCROLL_VELOCITY_WINDOW):
            self._pan_hist.popleft()

    def _handle_page_swipe(self, sc, sci, now):
        """"Swipe Between Pages" (Touchpads toggle): watch every LEFT-pad
        touch in desktop takeover and, on lift, fire the configured output
        for that direction (Touchpads → Swipe Between Pages cog modal 
        "Swipe Right"/"Swipe Left", same action vocabulary as a Hotkeys
        Button Combo output; defaults are mouse Back/Forward, XBUTTON1/
        XBUTTON2, honored by browsers, File Explorer and Windows Settings)
        when the touch was a genuine macbook-style page flick: brief, far,
        fast and decisively horizontal (flick RIGHT = the "Swipe Right"
        output, LEFT = "Swipe Left"  content follows the finger, like
        macbook's swipe). Runs as a passive OBSERVER alongside the scroll
        handlers (scrolling itself is untouched); a touch is POISONED  can
        never fire  the moment any other gesture owns the pad (pinch, the
        zoomed 360° pan, the video-scrub dial) or while the toggle is off,
        and the SWIPE_* gates (duration window, travel, flatness, speed,
        cooldown) make an unintended fire out of ordinary scrolling
        effectively impossible. Fires a haptic tick so a successful flick is
        felt."""
        if not (sci.buttons & SCButtons.LPADTOUCH):
            tr = self._swipe_track
            self._swipe_track = None
            if tr is None or tr["bad"]:
                return
            dur = now - tr["t0"]
            dx = tr["x"] - tr["x0"]
            adx = abs(dx)
            if (self.SWIPE_MIN_DUR <= dur <= self.SWIPE_MAX_DUR
                    and adx >= self.SWIPE_MIN_DX
                    and tr["cross"] <= adx * self.SWIPE_MAX_CROSS
                    and adx / max(dur, 1e-3) >= self.SWIPE_MIN_SPEED
                    and now - self._swipe_fire_t >= self.SWIPE_COOLDOWN):
                out_id = (adusk_state.get_swipe_right_output() if dx > 0
                          else adusk_state.get_swipe_left_output())
                act = keybinds_runtime.resolve_action(out_id, sui.Keys)
                if act[0] == "none":
                    return
                self._swipe_fire_t = now
                self._fire_guide_action(sc, act)
                if adusk_state.is_rumble_enabled(self._kind):
                    sc.haptic_pad_click()
            return
        x, y = float(sci.lpad_x), float(sci.lpad_y)
        tr = self._swipe_track
        if tr is None:
            tr = self._swipe_track = {"t0": now, "x0": x, "y0": y,
                                      "x": x, "y": y, "cross": 0.0,
                                      "bad": False}
        else:
            tr["x"], tr["y"] = x, y
            c = abs(y - tr["y0"])
            if c > tr["cross"]:
                tr["cross"] = c
        if (not adusk_state.is_swipe_pages_enabled()
                or self._pinch_latched
                or self._zoom_scale > 1.0
                or self._lpad_scrub_latch):
            tr["bad"] = True

    def _handle_pad_mouse(self, sc, sci, now):
        """Desktop takeover: RIGHT trackpad → cursor. The cursor tracks a 1€
        filter (speed-adaptive low-pass, PAD_EURO_*) of the pad's ABSOLUTE
        position and moves by the delta of that filtered position (POSITION
        domain, not velocity). When the finger holds still the filtered point
        stops and the cursor stays put  no drift/coast tail. The cutoff stays
        LOW through normal motion (constant silky smoothing with a touch of
        lag, the Steam Input feel) and opens with speed so a fast flick stays
        snappy. A soft dead-zone (PAD_DEADZONE pad-units) absorbs a shaky
        thumb: no motion until the filtered point leaves the radius, then the
        anchor TRAILS it at the radius and the cursor moves only by the
        overshoot (ramps from zero  no lurch); at REST the anchor eases back
        onto the filtered point so the full radius of slack surrounds a
        resting finger again  so tremor gives ZERO movement while real motion
        tracks 1:1. On lift, a FAST swipe keeps coasting (kinetic fling,
        velocity ∝ how hard you swiped) and decays; slow/small moves stop
        dead. Speed = tray 'Trackpad Mouse Speed' multiplier."""
        touched = bool(sci.buttons & SCButtons.RPADTOUCH)
        if touched:
            x, y = sci.rpad_x, sci.rpad_y
            speed = self.PAD_MOUSE_SCALE * adusk_state.get_sc_trackpad_speed()
            if self._rpad_filt is None:
                # Fresh contact: seed the filter + anchor at the touch point so the
                # first frame can't fling, and cancel any in-flight glide (a touch
                # "catches" a coasting cursor).
                # Tap-to-click candidate  armed only on a TRUE fresh contact
                # (_rpad_touched_was False; a mid-touch reseed after Pinch To
                # Zoom released the pad must not restart the tap clock), only
                # when NOT catching a coasting fling (that touch stops the
                # cursor, it doesn't click), and only while no trigger/pad
                # click already holds a mouse button (lifting the pad finger
                # mid-drag must not inject an extra click).
                if (adusk_state.is_tap_to_click_enabled()
                        and not self._rpad_touched_was
                        and not self._fling_active
                        and not (self._lt_was_pressed or self._rt_was_pressed
                                 or self._rpad_click_prev
                                 or self._lpad_click_prev)):
                    self._tap_start = (now, float(x), float(y))
                    self._tap_hist.clear()
                    self._tap_hist.append((now, float(x), float(y)))
                    self._tap_moved = 0.0
                else:
                    self._tap_start = None
                self._rpad_filt = [float(x), float(y)]
                self._rpad_dfilt = [0.0, 0.0]
                self._rpad_anchor = [float(x), float(y)]
                self._rpad_prev = (x, y)
                self._rpad_last_t = now
                self._rpad_vx = 0.0
                self._rpad_vy = 0.0
                self._fling_active = False   # a touch CATCHES a coasting cursor
                self._rpad_history.clear()
                self._rpad_history.append((now, x, y))
                self._rpad_touched_was = True
                return
            dt = now - self._rpad_last_t if self._rpad_last_t else 0.0
            if dt <= 0.0 or dt > 0.1:
                dt = 1.0 / 60.0
            # 1€ filter on the ABSOLUTE position: low-pass the raw velocity
            # (DCUTOFF) to a stable speed estimate, then open the position
            # cutoff with that speed  silky constant smoothing through normal
            # motion (the Steam-like glide), rising on a fast flick so it
            # never feels stuck.
            ad = dt / (1.0 / (2.0 * math.pi * self.PAD_EURO_DCUTOFF) + dt)
            self._rpad_dfilt[0] += ad * (
                (x - self._rpad_prev[0]) / dt - self._rpad_dfilt[0])
            self._rpad_dfilt[1] += ad * (
                (y - self._rpad_prev[1]) / dt - self._rpad_dfilt[1])
            pad_speed = (self._rpad_dfilt[0] ** 2
                         + self._rpad_dfilt[1] ** 2) ** 0.5
            fc = self.PAD_EURO_MINCUTOFF + self.PAD_EURO_BETA * pad_speed
            a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
            self._rpad_filt[0] += a * (x - self._rpad_filt[0])
            self._rpad_filt[1] += a * (y - self._rpad_filt[1])
            # Soft dead-zone: the anchor TRAILS the filtered point by PAD_DEADZONE
            # pad-units. The cursor moves only by the OVERSHOOT past the radius
            # (dist - PAD_DEADZONE), not the whole vector, and the anchor re-trails
            # to stay exactly PAD_DEADZONE behind. So a still/tremoring finger
            # inside the radius gives ZERO movement (no drift/wobble), and when it
            # DOES cross, the motion ramps from zero instead of lurching the full
            # radius  which kills residual standstill blips and the chunky feel a
            # bigger SNAP radius gave. Continuous motion tracks 1:1 (radius slack).
            dx = self._rpad_filt[0] - self._rpad_anchor[0]
            dy = self._rpad_filt[1] - self._rpad_anchor[1]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > self.PAD_DEADZONE:
                over = dist - self.PAD_DEADZONE
                ux, uy = dx / dist, dy / dist
                self._rpad_anchor[0] = self._rpad_filt[0] - self.PAD_DEADZONE * ux
                self._rpad_anchor[1] = self._rpad_filt[1] - self.PAD_DEADZONE * uy
                if now < self._mouse_freeze_until:
                    # Click-shake freeze: swallow the wobble (the anchor
                    # still trails, so nothing pent-up lurches out after) 
                    # unless the motion grows past the breakout, which only
                    # a REAL drag does; then release the freeze.
                    self._freeze_acc += over
                    if self._freeze_acc >= self.PAD_FREEZE_BREAKOUT:
                        self._mouse_freeze_until = 0.0
                else:
                    self._pad_mouse_acc_x += (over * ux) * speed
                    self._pad_mouse_acc_y += -(over * uy) * speed  # pad +y up→screen -y
                    mvx = int(self._pad_mouse_acc_x)
                    mvy = int(self._pad_mouse_acc_y)
                    self._pad_mouse_acc_x -= mvx
                    self._pad_mouse_acc_y -= mvy
                    if mvx or mvy:
                        self._chord.mouse.move(mvx, mvy)
                        if self._tap_start is not None:
                            # The cursor VISIBLY moved during this touch  a
                            # quick corrective nudge is pointing, not tapping.
                            # (Sensitivity-independent raw wander is gated at
                            # lift; this catches short strokes that slip under
                            # that gate but still moved the pointer.)
                            self._tap_moved += abs(mvx) + abs(mvy)
                            if self._tap_moved > self.PAD_TAP_MAX_PX:
                                self._tap_start = None
            elif pad_speed < self.PAD_REST_SPEED:
                # Resting inside the dead-zone: ease the anchor back onto the
                # filtered point so the full radius of slack is restored all
                # around the still finger (post-move it sat pinned ON the edge
                #  zero slack along the travel direction, so the very next
                # tremor blip leaked straight out as cursor wiggle).
                ra = dt / (self.PAD_ANCHOR_RECENTER_TAU + dt)
                self._rpad_anchor[0] += ra * (
                    self._rpad_filt[0] - self._rpad_anchor[0])
                self._rpad_anchor[1] += ra * (
                    self._rpad_filt[1] - self._rpad_anchor[1])
            # Tap-to-click candidate upkeep: a touch held past the tap window
            # is a rest or a drag, never a tap; while the candidate lives,
            # record the raw samples so the lift can judge total wander with
            # the peel-off tail trimmed (a peel blip mustn't veto a real tap,
            # so wander is NOT judged live here).
            if self._tap_start is not None:
                if now - self._tap_start[0] > self.PAD_TAP_MAX_S:
                    self._tap_start = None
                    self._tap_hist.clear()
                else:
                    self._tap_hist.append((now, float(x), float(y)))
            # Track recent motion for the lift velocity  kept a little longer
            # than the averaging span so the lift can drop the peel-off tail.
            self._rpad_history.append((now, x, y))
            cutoff = now - self.PAD_VELOCITY_WINDOW
            while self._rpad_history and self._rpad_history[0][0] < cutoff:
                self._rpad_history.popleft()
            self._rpad_prev = (x, y)
            self._rpad_last_t = now
            self._rpad_touched_was = True
            return
        # --- Lifted: launch a fling impulse, but only if the swipe was fast enough.
        if self._rpad_touched_was:
            # "Right Touchpad Tap to Click": a quick, still touch-and-lift =
            # one left click. Judged BEFORE the fling so a fired tap's shake-
            # freeze (below) vetoes the fling via the existing freeze check 
            # a tap never also throws the cursor. Wander is the bounding-box
            # spread of the CORE samples  EXCLUDING the first
            # PAD_TAP_START_SKIP (the pad resolving a fresh contact writes a
            # garbage blip, which as the wander ORIGIN read still taps as
            # swipes) and the final PAD_LIFT_SKIP (the finger-peel twin) 
            # so both contact transients are trimmed; an ultra-short tap
            # with no core samples passes outright (nothing that brief
            # travels anywhere).
            tap = self._tap_start
            self._tap_start = None
            if tap is not None and adusk_state.is_tap_to_click_enabled():
                dur = (self._rpad_last_t or now) - tap[0]
                if self.PAD_TAP_MIN_S <= dur <= self.PAD_TAP_MAX_S:
                    t_lo = tap[0] + self.PAD_TAP_START_SKIP
                    t_hi = self._rpad_last_t - self.PAD_LIFT_SKIP
                    min_x = min_y = max_x = max_y = None
                    for ts, xs, ys in self._tap_hist:
                        if ts < t_lo:
                            continue
                        if ts > t_hi:
                            break
                        if min_x is None:
                            min_x = max_x = xs
                            min_y = max_y = ys
                        else:
                            min_x = min(min_x, xs)
                            max_x = max(max_x, xs)
                            min_y = min(min_y, ys)
                            max_y = max(max_y, ys)
                    d2 = (0.0 if min_x is None
                          else (max_x - min_x) ** 2 + (max_y - min_y) ** 2)
                    if d2 <= self.PAD_TAP_MAX_DIST ** 2:
                        # Ref-counted press+release: if a trigger/pad click is
                        # somehow holding "left" both edges are no-ops, so a
                        # tap can never drop someone else's drag mid-flight.
                        self._chord.set_mouse_button("left", "rpad_tap", True)
                        self._chord.set_mouse_button("left", "rpad_tap", False)
                        if adusk_state.is_rumble_enabled(self._kind):
                            sc.haptic_pad_click()
                        # A click is user activity  arm the video-region
                        # probe like a physical pad click does.
                        self._probe_move_at = now
                        # Freeze the cursor exactly like an R2/pad-click edge:
                        # the SECOND tap of a double-tap lands during this
                        # freeze, so its touch wobble can't smear the pointer
                        # between the two clicks  the double-click stays
                        # inside the OS double-click rectangle and folders
                        # OPEN instead of dragging/re-selecting.
                        self._mouse_shake_guard()
            self._tap_hist.clear()
            # Just lifted. Lift velocity = the average over the history
            # EXCLUDING the final PAD_LIFT_SKIP  the finger peeling off the
            # pad writes a garbage position blip in the last frames, which
            # used to read as a violent swipe and "throw" the cursor from a
            # standing-still (or aggressive) lift. The remaining span must
            # also cover PAD_FLING_MIN_SPAN of real, sustained motion, and a
            # click-shake freeze vetoes the fling outright (a trigger-squeeze
            # wobble is never a throw).
            speed = self.PAD_MOUSE_SCALE * adusk_state.get_sc_trackpad_speed()
            cutoff_t = self._rpad_last_t - self.PAD_LIFT_SKIP
            hist = [s for s in self._rpad_history if s[0] <= cutoff_t]
            vx = vy = 0.0
            if len(hist) >= 2 and now >= self._mouse_freeze_until:
                t0, x0, y0 = hist[0]
                t1, x1, y1 = hist[-1]
                span = t1 - t0
                if span >= self.PAD_FLING_MIN_SPAN:
                    vx = (x1 - x0) * speed / span
                    vy = -(y1 - y0) * speed / span
            lift_speed = (vx * vx + vy * vy) ** 0.5
            if lift_speed >= self.PAD_FLING_TRIGGER:
                # Fling runs at GAIN × the tracking lift velocity, so the throw is
                # decoupled from (and faster than) the cursor speed.
                fling_speed = lift_speed * self.PAD_FLING_GAIN
                self._fling_active = True
                self._fling_t0 = now
                self._fling_last_t = now
                self._fling_v0 = fling_speed
                self._fling_peak = fling_speed * self.PAD_FLING_BOOST
                self._fling_dirx = vx / lift_speed
                self._fling_diry = vy / lift_speed
            else:
                self._fling_active = False
            self._rpad_filt = None
            self._rpad_prev = None
            self._rpad_history.clear()
            self._rpad_touched_was = False
        # Drive the fling: a velocity HUMP  ramp UP from the lift speed to the
        # boosted peak over PAD_FLING_RAMPUP_T (fast accel), then ramp DOWN from the
        # peak to a stop over PAD_FLING_RAMPDOWN_T (gentler settle). Thrown-and-
        # caught feel, not an icy coast.
        if self._fling_active:
            elapsed = now - self._fling_t0
            up_t = max(1e-3, self.PAD_FLING_RAMPUP_T)
            down_t = max(1e-3, self.PAD_FLING_RAMPDOWN_T)
            if elapsed >= up_t + down_t:
                self._fling_active = False
            else:
                if elapsed < up_t:
                    v = self._fling_v0 + (self._fling_peak - self._fling_v0) * (
                        elapsed / up_t)
                else:
                    # Ease-OUT ramp-down: velocity tapers smoothly to zero (zero
                    # slope at the end) so it glides to a standstill instead of
                    # stopping with a hard linear corner ("sudden stop").
                    s = (elapsed - up_t) / down_t
                    v = self._fling_peak * (1.0 - s) * (1.0 - s)
                dt = now - self._fling_last_t
                dt = max(1e-3, min(dt, 1.0 / 30.0))  # clamp so a stall can't lurch
                self._fling_last_t = now
                self._pad_mouse_acc_x += v * self._fling_dirx * dt
                self._pad_mouse_acc_y += v * self._fling_diry * dt
                mvx = int(self._pad_mouse_acc_x)
                mvy = int(self._pad_mouse_acc_y)
                self._pad_mouse_acc_x -= mvx
                self._pad_mouse_acc_y -= mvy
                if mvx or mvy:
                    self._chord.mouse.move(mvx, mvy)

    def _handle_lpad_tap(self, sc, sci, now, pinch_active):
        """"Left Touchpad Tap to Click": the left-pad twin of the right-pad
        tap-to-click  a quick, still touch-and-lift on the LEFT pad fires a
        RIGHT click. Tracked independently of whatever mode (scrolling /
        wheel dial / video scrub / text-wheel select / zoomed pan) currently
        owns the pad, off the RAW LPADTOUCH bit and RAW position  those
        handlers repeatedly reset their own touch state (_lpad_prev etc.) for
        their own reasons, which would false-fire or starve a naive tracker
        piggybacking on them. Same qualification math as the right-pad tap
        (PAD_TAP_* constants): lift within MAX_S, lasted at least MIN_S,
        wander (bounding-box of the core samples with both contact-transient
        trims) within MAX_DIST. No cursor-px gate  a still left-pad touch
        never moves the cursor under any mode, so there's nothing to
        discriminate there; a genuine scroll/dial/swipe already fails the
        wander gate. Cancelled by a physical click/trigger edge (via
        _mouse_shake_guard, which also clears this candidate), by touching
        down while a coasting scroll/pan fling is caught (that touch stops
        the coast, it doesn't tap), while a trigger/click already holds a
        mouse button, or during Pinch To Zoom (a two-finger gesture, never a
        tap)."""
        raw_touch = bool(sci.buttons & SCButtons.LPADTOUCH)
        if raw_touch:
            x, y = float(sci.lpad_x), float(sci.lpad_y)
            if not self._lpad_raw_touch_prev:
                # Fresh contact only (the raw bit was clear last frame  a
                # mode handoff mid-touch, e.g. pinch releasing the pad, must
                # not restart the tap clock on a finger that never lifted).
                if (adusk_state.is_tap_to_click_left_enabled()
                        and not pinch_active
                        and not (self._scroll_fling_v or self._pan_fling_vx
                                 or self._pan_fling_vy)
                        and not (self._lt_was_pressed or self._rt_was_pressed
                                 or self._rpad_click_prev
                                 or self._lpad_click_prev)):
                    self._lpad_tap_start = (now, x, y)
                    self._lpad_tap_hist.clear()
                    self._lpad_tap_hist.append((now, x, y))
                else:
                    self._lpad_tap_start = None
            elif self._lpad_tap_start is not None:
                if now - self._lpad_tap_start[0] > self.PAD_TAP_MAX_S:
                    self._lpad_tap_start = None
                    self._lpad_tap_hist.clear()
                else:
                    self._lpad_tap_hist.append((now, x, y))
            self._lpad_tap_last_t = now
        elif self._lpad_raw_touch_prev:
            # Just lifted (for real  this method sees the raw bit, not any
            # mode's Steam/pinch-masked view of the pad).
            tap = self._lpad_tap_start
            self._lpad_tap_start = None
            if tap is not None and adusk_state.is_tap_to_click_left_enabled():
                dur = (self._lpad_tap_last_t or now) - tap[0]
                if self.PAD_TAP_MIN_S <= dur <= self.PAD_TAP_MAX_S:
                    t_lo = tap[0] + self.PAD_TAP_START_SKIP
                    t_hi = (self._lpad_tap_last_t or now) - self.PAD_LIFT_SKIP
                    min_x = min_y = max_x = max_y = None
                    for ts, xs, ys in self._lpad_tap_hist:
                        if ts < t_lo:
                            continue
                        if ts > t_hi:
                            break
                        if min_x is None:
                            min_x = max_x = xs
                            min_y = max_y = ys
                        else:
                            min_x = min(min_x, xs)
                            max_x = max(max_x, xs)
                            min_y = min(min_y, ys)
                            max_y = max(max_y, ys)
                    d2 = (0.0 if min_x is None
                          else (max_x - min_x) ** 2 + (max_y - min_y) ** 2)
                    if d2 <= self.PAD_TAP_MAX_DIST ** 2:
                        self._chord.set_mouse_button("right", "lpad_tap", True)
                        self._chord.set_mouse_button("right", "lpad_tap", False)
                        if adusk_state.is_rumble_enabled(self._kind):
                            sc.haptic_pad_click()
                        self._probe_move_at = now
                        self._mouse_shake_guard()
            self._lpad_tap_hist.clear()
        self._lpad_raw_touch_prev = raw_touch

    def _video_focused(self, now):
        """Cached foreground check for Video Timeline Scrubbing: True while
        the foreground window title names a known video site/player."""
        if now - self._scrub_focus_at >= self.SCRUB_FOCUS_TTL:
            self._scrub_focus_at = now
            title = _foreground_title().lower()
            if title != self._scrub_title:
                # Navigation / tab or window switch: whatever video region
                # was learned no longer describes what's on screen.
                self._scrub_title = title
                self._video_region = None
            self._scrub_focus = any(
                tok in title for tok in self._SCRUB_TITLE_TOKENS)
        return self._scrub_focus

    def _cursor_on_video(self):
        """Cursor gate for Video Timeline Scrubbing: scrub ONLY on confirmed
        evidence  true fullscreen, or the pointer inside the video region
        learned by _probe_video_region. Unknown = scroll: a wrong scroll is
        a page nudge, a wrong scrub eats the gesture (or worse, pokes page
        UI), so the gate never guesses. Position is checked, not motion, and
        the region has no time expiry (only invalidation events void it), so
        a mouse RESTING on the video for minutes still scrubs."""
        hwnd = _foreground_hwnd()
        if hwnd and _window_fullscreen(hwnd):
            return True
        reg = self._video_region
        if reg is None or reg[0] != hwnd:
            return False
        rect = _foreground_rect()
        if rect is None or reg[1] != rect:
            return False
        try:
            cx, cy = self._chord.mouse.get_position()
        except Exception:
            return False
        vl, vt, vr, vb = reg[2]
        return vl <= cx < vr and vt <= cy < vb

    def _learn_video_region(self, bar_left, bar_row, win=None):
        """Remember where the video sits in this window (from a successful
        playhead scan) for _cursor_on_video: the bar row is the video's
        bottom edge and the bar's left edge ≈ its left. Top/right aren't
        detectable, so they fall back to just under the browser chrome and
        the window's right edge. Keyed on hwnd + window rect, so a move or
        resize voids it; scrolling, navigation and a failed scrub gesture
        void it explicitly (no time expiry)."""
        win = win if win is not None else self._hover_win
        if win is None:
            return
        hwnd = _foreground_hwnd()
        if not hwnd:
            return
        l, t, r, b = win
        self._video_region = (
            hwnd, (l, t, r, b),
            (max(l, bar_left - 16), t + 100, r, bar_row + 30))

    def _probe_video_region(self, now):
        """Background learner for the cursor-on-video gate: while a YouTube
        WATCH page is focused windowed and there's no valid region yet, scan
        for the progress bar whenever the cursor has moved recently  mouse
        motion over the player is exactly what pops the controls (and their
        red bar) up, so scans piggyback on it. At most one scan per
        SCRUB_PROBE_GAP, and none at all once a region is learned or while
        the pointer is idle, so the steady-state cost is zero."""
        try:
            cx, cy = self._chord.mouse.get_position()
        except Exception:
            return
        if self._probe_cx is None:
            self._probe_cx, self._probe_cy = cx, cy
            return
        if abs(cx - self._probe_cx) + abs(cy - self._probe_cy) > 2:
            self._probe_cx, self._probe_cy = cx, cy
            self._probe_move_at = now
        if now - self._probe_move_at > self.SCRUB_PROBE_MOVE_WINDOW:
            return  # no recent activity → controls faded, a scan can't hit
        if not self._video_focused(now):
            return
        # Watch pages are titled "<video> - YouTube"; the home/subscriptions
        # feeds are plain "YouTube"  skip pages that can't have a player
        # bar so browsing never pays for scans.
        if " - youtube" not in self._scrub_title:
            return
        hwnd = _foreground_hwnd()
        if not hwnd or _window_fullscreen(hwnd):
            return  # fullscreen needs no region
        reg = self._video_region
        rect = _foreground_rect()
        if rect is None:
            return
        if reg is not None and reg[0] == hwnd and reg[1] == rect:
            return  # region already valid for this exact layout
        l, t, r, b = rect
        if not (l <= cx < r and t <= cy < b):
            return
        # Regionless but recently active: try a scan. We do NOT synthesize any
        # cursor motion to "wake" YouTube's controls  a background helper must
        # never move the user's real pointer. An earlier ±1px wake-wiggle here
        # (meant to pop the controls where a parked cursor sits) vibrated the
        # cursor every frame for the whole post-motion window (SCRUB_PROBE_
        # MOVE_WINDOW = 3s) after the user let go of the pad  a very visible
        # "the cursor wiggles for ~4s then stops, only on YouTube" bug. The
        # scan now piggybacks purely on the user's GENUINE motion over the
        # player (that motion is exactly what pops the controls + their red bar
        # up); a parked cursor just means the region is learned a little later,
        # and the hover scrub does its own scan on engage regardless.
        if now - self._probe_at < self.SCRUB_PROBE_GAP:
            return
        self._probe_at = now
        hit = _scan_playhead_windowed(l, t, r, b)
        if hit is not None:
            _px, row, bx0 = hit
            self._learn_video_region(bx0, row, win=rect)

    def _abandon_hover_scrub(self):
        """Cancel an in-flight hover scrub WITHOUT seeking (focus/mode changed
        mid-scrub): if a windowed drag is holding the knob, drag it back to
        where it started before letting go (≈ no seek), then put the cursor
        back and drop all hover state."""
        if self._hover_pressed and self._hover_bar is not None:
            x0, x1, bar_y = self._hover_bar
            if self._hover_start_x is not None:
                self._chord.mouse.set_position(
                    int(self._hover_start_x), bar_y)
            self._chord.mouse.release("left")
        if self._hover_restore is not None:
            self._chord.mouse.set_position(*self._hover_restore)
        _show_system_cursor()
        self._hover_x = None
        self._hover_bar = None
        self._hover_restore = None
        self._hover_scan_at = None
        self._hover_win = None
        self._hover_drag = False
        self._hover_pressed = False
        self._hover_start_x = None

    def _handle_pad_scrub_hover(self, sc, sci, now):
        """Video Timeline Scrubbing, "hover" mode  the mouse-like scrub: the
        REAL cursor rides along the video's progress bar while the thumb
        circles, so the player shows its hover thumbnail/preview and the
        video KEEPS PLAYING under it. Lifting the thumb left-clicks the
        hovered spot  that's the seek  and puts the cursor back where it
        was. Clockwise = later, counter-clockwise = earlier; the cursor is
        absolute-positioned every frame from the accumulated rotation, so
        the preview tracks the dial pixel-exactly. The dial starts from the
        video's CURRENT playback position, and the cursor's only visible
        move is STRAIGHT onto the playhead: engaging jiggles the cursor in
        place (any motion over the player wakes the controls) and pixel-
        scans for the red played-portion edge  immediately if the controls
        are already up, else deferred past the fade-in. Rotation made before
        the playhead is found is banked (_hover_moved) and applied on the
        snap, so nothing is lost."""
        if not (sci.buttons & SCButtons.LPADTOUCH):
            if self._hover_bar is not None:
                x0, x1, bar_y = self._hover_bar
                if self._hover_scan_at is not None:
                    # Lifted before the deferred playhead scan ran (a quick
                    # flick)  scan now so the flick still seeks relative to
                    # the real playhead.
                    self._hover_scan_at = None
                    if self._hover_drag:
                        hit = (_scan_playhead_windowed(*self._hover_win)
                               if self._hover_win is not None else None)
                        if hit is not None:
                            px, bar_y, bx0 = hit
                            self._learn_video_region(bx0, bar_y)
                            x0 = bx0
                            self._hover_x = max(
                                float(x0),
                                min(float(x1), px + self._hover_moved))
                    else:
                        hit = _scan_progress_playhead(x0, x1, bar_y)
                        if hit is not None:
                            px, bar_y = hit  # click the DETECTED bar row
                            self._hover_x = max(
                                float(x0),
                                min(float(x1), px + self._hover_moved))
                committed = False
                if self._hover_x is not None:
                    if self._hover_drag:
                        # Windowed commit: a milliseconds-long micro-DRAG 
                        # press on the LIVE playhead (rescanned now; the
                        # video kept playing while the dial hovered), pull
                        # to the target, release. The player's pointer
                        # capture keeps the whole gesture on the bar, so a
                        # target past the bar's unknown right edge can't
                        # click page UI; if the knob can't be found, NOTHING
                        # is clicked.
                        hit = (_scan_playhead_windowed(*self._hover_win)
                               if self._hover_win is not None else None)
                        if hit is not None:
                            px, row, _bx0 = hit
                            self._learn_video_region(_bx0, row)
                            self._chord.mouse.set_position(px, row)
                            self._chord.mouse.press("left")
                            time.sleep(0.02)  # let the press register first
                            self._chord.mouse.set_position(
                                int(round(self._hover_x)), row)
                            time.sleep(0.02)
                            self._chord.mouse.release("left")
                            committed = True
                    else:
                        # Fullscreen commit: click the hovered spot.
                        self._chord.mouse.set_position(
                            int(round(self._hover_x)), bar_y)
                        self._chord.mouse.press("left")
                        self._chord.mouse.release("left")
                        committed = True
                # Restore the cursor even when nothing was found  the
                # engage may have parked it on the learned bar spot.
                if self._hover_restore is not None:
                    self._chord.mouse.set_position(*self._hover_restore)
                if committed and adusk_state.is_rumble_enabled(self._kind):
                    sc.haptic_pad_click()  # "committed" tick
                if (self._hover_drag and self._hover_x is None
                        and self._hover_scan_tries >= 2):
                    # A full-length gesture never found the bar where the
                    # learned region promised it  the layout changed under
                    # us (a scroll or toggle we couldn't see). Void the
                    # region: touches go back to scrolling until the probe
                    # re-learns it. Playhead never found also means nothing
                    # was clicked; the gesture is silently dropped.
                    self._video_region = None
            _show_system_cursor()
            self._hover_x = None
            self._hover_bar = None
            self._hover_restore = None
            self._hover_scan_at = None
            self._hover_win = None
            self._hover_drag = False
            self._hover_pressed = False
            self._hover_start_x = None
            self._scrub_angle = None
            return
        if self._hover_bar is not None:
            # Keep FEEDING mouse events for the whole gesture: YouTube
            # ignores an isolated synthetic move and hides its controls
            # after a few idle seconds (even paused)  one 1px jiggle was
            # NOT enough to bring the bar up for the scans. A per-frame
            # alternating 1px wiggle (~266Hz; the pointer is hidden) reads
            # as real mouse motion, so the controls  and the red bar the
            # scans hunt  stay on screen right through the lift rescan.
            self._hover_wiggle = not self._hover_wiggle
            self._chord.mouse.move(1 if self._hover_wiggle else -1, 0)
        x, y = float(sci.lpad_x), float(sci.lpad_y)
        if (x * x + y * y) ** 0.5 < self.SCRUB_MIN_RADIUS:
            # Center of the pad  atan2 noise; hold position.
            return
        ang = math.atan2(y, x)
        if self._scrub_angle is None:
            # Engage. FULLSCREEN: the bar spans the window near its bottom 
            # jiggle the cursor in place to wake the controls, find the red
            # playhead, hover it (video keeps playing; click on lift seeks).
            # WINDOWED: the bar sits at the bottom of the video ELEMENT,
            # located by the whole-window scan  grab the knob with a left-
            # button press and DRAG it (the player's pointer capture keeps
            # the drag on the bar however far the cursor strays, so no
            # bar-width estimate is needed); release on lift is the seek.
            self._scrub_angle = ang
            rect = _foreground_rect()
            if rect is None:
                return
            l, t, r, b = rect
            x0 = l + self.SCRUB_HOVER_BAR_MARGIN
            x1 = r - self.SCRUB_HOVER_BAR_MARGIN
            if x1 <= x0:
                return
            self._hover_restore = self._chord.mouse.get_position()
            self._hover_x = None      # unknown until the playhead is found
            self._hover_tick_acc = 0.0
            self._hover_moved = 0.0
            self._hover_win = (l, t, r, b)
            hwnd = _foreground_hwnd()
            self._hover_drag = not (hwnd and _window_fullscreen(hwnd))
            # Hide the pointer for the whole gesture  the scrub reads as
            # the timeline moving by itself, not a mouse flying around.
            # Restored on lift/abandon (and it's session-global, so those
            # paths ALWAYS run it).
            _hide_system_cursor()
            self._chord.mouse.move(1, 0)
            self._chord.mouse.move(-1, 0)
            if self._hover_drag:
                self._hover_bar = (x0, x1, b)  # placeholder row until found
                self._hover_scan_tries = 0
                hit = _scan_playhead_windowed(l, t, r, b)
                if hit is not None:
                    px, row, bx0 = hit
                    self._learn_video_region(bx0, row)
                    self._hover_bar = (bx0, x1, row)
                    self._hover_x = float(px)
                    self._hover_start_x = px
                    # HOVER only  the video keeps playing and the preview
                    # follows the dial; the actual seek is a milliseconds-
                    # long micro-drag done at lift time.
                    self._chord.mouse.set_position(px, row)
                else:
                    # Controls are probably hidden, and windowed they only
                    # wake while the cursor is OVER the player. If a FRESH
                    # region was learned for this window, hover the learned
                    # BAR itself (hovering the control area keeps the
                    # controls pinned up  far more reliable than a guess).
                    # Otherwise DON'T move the cursor at all: the gate says
                    # the pointer is where the user aimed, and the per-frame
                    # wiggle wakes the controls right there  while a wrong
                    # optimistic engage (video scrolled away, cursor over
                    # comments) just scans out and drops, instead of the
                    # old guess-jump landing on page UI (the "cursor jumped
                    # to the like button" bug). Rescan after the fade-in.
                    reg = self._video_region
                    if (reg is not None and reg[0] == hwnd
                            and reg[1] == (l, t, r, b)):
                        vl, vt, vr, vb = reg[2]
                        self._chord.mouse.set_position(vl + 60, vb - 30)
                    self._hover_scan_at = now + 0.35
                return
            bar_y = b - self.SCRUB_HOVER_BAR_BOTTOM
            self._hover_bar = (x0, x1, bar_y)
            hit = _scan_progress_playhead(x0, x1, bar_y)
            if hit is not None:
                px, bar_y = hit
                self._hover_bar = (x0, x1, bar_y)
                self._hover_x = float(px)
                self._hover_scan_at = None
                self._chord.mouse.set_position(px, bar_y)
            else:
                self._hover_scan_at = now + 0.3  # after the controls fade in
            return
        d = ang - self._scrub_angle
        # Wrap to (-pi, pi] so crossing the ±180° seam doesn't spin the dial.
        if d > math.pi:
            d -= 2.0 * math.pi
        elif d <= -math.pi:
            d += 2.0 * math.pi
        self._scrub_angle = ang
        if self._hover_bar is None:
            return  # engage failed (no window rect); keep tracking the angle
        x0, x1, bar_y = self._hover_bar
        if self._hover_scan_at is not None and now >= self._hover_scan_at:
            # Deferred playhead snap: the controls have faded in, so find
            # the red played-portion edge and jump the dial there  plus any
            # rotation already banked, so early spinning isn't lost.
            self._hover_scan_at = None
            if self._hover_drag:
                # Windowed: locate the bar anywhere in the window and start
                # HOVERING it (no press  the video keeps playing; the seek
                # is a micro-drag at lift). Not found yet → the controls may
                # still be fading in, so retry a couple of times; after
                # that, leave _hover_x None and the gesture drops silently
                # on lift (clicking a guessed spot inside a window full of
                # page UI is far worse than doing nothing).
                hit = (_scan_playhead_windowed(*self._hover_win)
                       if self._hover_win is not None else None)
                if hit is not None:
                    px, row, bx0 = hit
                    self._learn_video_region(bx0, row)
                    self._hover_bar = (bx0, x1, row)
                    x0, bar_y = bx0, row
                    self._hover_x = max(
                        float(x0), min(float(x1), px + self._hover_moved))
                    self._hover_start_x = px
                    self._chord.mouse.set_position(
                        int(round(self._hover_x)), bar_y)
                elif self._hover_scan_tries < 6:
                    self._hover_scan_tries += 1
                    self._hover_scan_at = now + 0.45
            else:
                # Fullscreen: the scan also returns the bar's REAL row,
                # correcting the 68px estimate (a wrong row made the scan
                # miss entirely). Still nothing red → fall back to the old
                # mid-bar park so the hover preview at least engages.
                hit = _scan_progress_playhead(x0, x1, bar_y)
                if hit is not None:
                    px, bar_y = hit
                    self._hover_bar = (x0, x1, bar_y)
                    self._hover_x = max(
                        float(x0), min(float(x1), px + self._hover_moved))
                else:
                    self._hover_x = (x0 + x1) / 2.0
                self._chord.mouse.set_position(
                    int(round(self._hover_x)), bar_y)
        # Clockwise thumb motion DECREASES the atan2 angle → later in the
        # video → rightward along the bar.
        move = -math.degrees(d) * self.SCRUB_HOVER_PX_PER_DEG
        self._hover_moved += move
        if self._hover_x is None:
            return  # playhead not found yet; rotation is banked above
        self._hover_x = max(float(x0), min(float(x1), self._hover_x + move))
        self._chord.mouse.set_position(int(round(self._hover_x)), bar_y)
        self._hover_tick_acc += abs(move)
        if self._hover_tick_acc >= self.SCRUB_HOVER_TICK_PX:
            self._hover_tick_acc = 0.0
            if adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_pad_click()  # travel tick  feel the distance

    def _handle_pad_scrub(self, sc, sci, now, mode):
        """Video Timeline Scrubbing: LEFT pad as a circular dial. Track the
        touch's angle around the pad center and tap the player's step key
        once per this mode's rotation step  clockwise = forward, counter-
        clockwise = back  with a haptic detent tick per step. In "frame"
        mode TOUCHING the pad pauses playback (K) and lifting taps "K"
        again to resume at the exact frame; "seek" mode never pauses, so
        lifting is a no-op. "hover" mode is its own handler  the cursor
        rides the progress bar instead of tapping keys."""
        if mode == "hover":
            self._handle_pad_scrub_hover(sc, sci, now)
            return
        step_deg, key_fwd, key_back, pauses = self._SCRUB_MODES[mode]
        if not (sci.buttons & SCButtons.LPADTOUCH):
            if pauses and self._scrub_stepped:
                # Thumb lifted after scrubbing  resume playback right here
                # (frame-stepping left the player paused on this frame).
                self._chord.kb.pressEvent([sui.Keys.KEY_K])
                self._chord.kb.releaseEvent([sui.Keys.KEY_K])
            self._scrub_stepped = False
            self._scrub_angle = None
            self._scrub_acc = 0.0
            return
        if pauses and not self._scrub_stepped:
            # Frame mode pauses the INSTANT the pad is touched (K), so the
            # frame steps happen on a parked video; the lift branch above
            # taps K again to resume. (K toggles  a video that was already
            # paused briefly plays for the touch, unavoidable without DOM
            # access.)
            self._chord.kb.pressEvent([sui.Keys.KEY_K])
            self._chord.kb.releaseEvent([sui.Keys.KEY_K])
            self._scrub_stepped = True
        x, y = float(sci.lpad_x), float(sci.lpad_y)
        if (x * x + y * y) ** 0.5 < self.SCRUB_MIN_RADIUS:
            # Too close to the center  atan2 is all noise there. Keep the
            # last angle so re-entering the ring continues smoothly.
            return
        ang = math.atan2(y, x)
        if self._scrub_angle is None:
            self._scrub_angle = ang  # first ring sample: reference only
            return
        d = ang - self._scrub_angle
        # Wrap to (-pi, pi] so crossing the ±180° seam doesn't spin the dial.
        if d > math.pi:
            d -= 2.0 * math.pi
        elif d <= -math.pi:
            d += 2.0 * math.pi
        self._scrub_angle = ang
        # Clockwise thumb motion DECREASES the atan2 angle → forward.
        self._scrub_acc -= d
        step = math.radians(step_deg)
        while abs(self._scrub_acc) >= step:
            if self._scrub_acc > 0:
                self._scrub_acc -= step
                key = key_fwd
            else:
                self._scrub_acc += step
                key = key_back
            self._chord.kb.pressEvent([key])
            self._chord.kb.releaseEvent([key])
            self._scrub_stepped = True
            if adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_pad_click()  # dial detent tick

    def _emit_scroll(self, laptop):
        """Drain the fractional-notch accumulator to the OS. Normal mode emits
        whole wheel notches (the classic per-line tick). Laptop mode emits raw
        1/120-notch wheel units straight through mouse_event, so browsers and
        other modern apps glide pixel-smooth (like middle-button autoscroll)
        instead of stepping a block of lines per notch  at ~120 units per
        notch and one drain per input frame the motion is effectively
        continuous. Legacy apps that only react to whole notches still scroll:
        Windows accumulates the units and they cross the 120 boundary at the
        same overall rate."""
        # "Invert Scrolling" (Touchpads scroll-settings cog): flip direction
        # for every mode. Applied at the single emit point so the fling coast
        # (which flows through here) inverts too.
        inv = -1 if adusk_state.is_sc_scroll_invert_enabled() else 1
        if laptop:
            units = int(self._scroll_acc * 120.0)
            if units:
                self._scroll_acc -= units / 120.0
                # MOUSEEVENTF_WHEEL with a sub-notch signed delta (pynput's
                # scroll() can only send whole ±120 notches).
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, units * inv, 0)
                # Scrolling moves the page  any learned video region no
                # longer describes what's under the cursor. But it's also
                # user ACTIVITY: arm the probe (like cursor motion does) so
                # scrolling back up to a parked-cursor video re-learns the
                # region without the mouse having to move.
                self._video_region = None
                self._probe_move_at = time.monotonic()
        else:
            steps = int(self._scroll_acc)
            if steps:
                self._scroll_acc -= steps
                self._chord.mouse.scroll(0, steps * inv)
                # Scrolling moves the page  any learned video region no
                # longer describes what's under the cursor. But it's also
                # user ACTIVITY: arm the probe (like cursor motion does) so
                # scrolling back up to a parked-cursor video re-learns the
                # region without the mouse having to move.
                self._video_region = None
                self._probe_move_at = time.monotonic()

    def _handle_pad_wheel(self, sc, sci, now, smooth):
        """Desktop takeover, "Wheel scrolling" modes: LEFT pad as a circular
        scroll dial. Track the touch's angle around the pad center  CLOCKWISE
        scrolls DOWN, counter-clockwise scrolls UP. "wheel" emits one discrete
        wheel notch per WHEEL_STEP_DEG with a haptic detent tick (a real clicky
        wheel); "wheel_smooth" (smooth=True) instead streams hi-res 1/120-notch
        wheel units proportional to the rotation, so browsers glide pixel-smooth
        with an analog feel (no ticks, no haptic). Reuses the video-scrub dial's
        angle math."""
        # Dial mode owns no linear coast/touch state  make sure any left over
        # from a mode switch can't leak a phantom scroll.
        self._scroll_fling_v = 0.0
        self._lpad_prev = None
        if not (sci.buttons & SCButtons.LPADTOUCH):
            self._wheel_angle = None
            self._wheel_acc = 0.0
            return
        x, y = float(sci.lpad_x), float(sci.lpad_y)
        if (x * x + y * y) ** 0.5 < self.SCRUB_MIN_RADIUS:
            # Near the center atan2 is all noise  hold the last angle so
            # re-entering the ring continues smoothly.
            return
        ang = math.atan2(y, x)
        if self._wheel_angle is None:
            self._wheel_angle = ang  # first ring sample: reference only
            self._wheel_filt = None  # reseed the smooth-mode filter too
            self._wheel_last_t = now
            return
        d = ang - self._wheel_angle
        # Wrap to (-pi, pi] so crossing the ±180° seam doesn't spin the dial.
        if d > math.pi:
            d -= 2.0 * math.pi
        elif d <= -math.pi:
            d += 2.0 * math.pi
        self._wheel_angle = ang
        if smooth:
            # "Wheel smooth" gets the SAME gentle 1€ smoothing as Laptop
            # scrolling (shared LSCROLL_EURO_* tuning): the low-passed speed
            # estimate ignores 1-frame stick-slip spikes of the circling thumb
            # (the skin-catch jolts get smeared out) while a sustained spin
            # opens the cutoff and scrolls as directly as before. Angle speed
            # is converted to ARC pad-units (× WHEEL_FILT_RADIUS) so the
            # shared constants mean the same physical finger motion.
            dt = now - self._wheel_last_t
            if dt <= 0.0 or dt > 0.1:
                dt = 1.0 / 60.0
            if self._wheel_filt is None:
                self._wheel_raw = 0.0
                self._wheel_filt = 0.0
                self._wheel_dfilt = 0.0
            ad = dt / (1.0 / (2.0 * math.pi * self.LSCROLL_EURO_DCUTOFF) + dt)
            self._wheel_dfilt += ad * (
                (d / dt) * self.WHEEL_FILT_RADIUS - self._wheel_dfilt)
            fc = (self.LSCROLL_EURO_MINCUTOFF
                  + self.LSCROLL_EURO_BETA * abs(self._wheel_dfilt))
            a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
            self._wheel_raw += d
            _prev_filt = self._wheel_filt
            self._wheel_filt += a * (self._wheel_raw - self._wheel_filt)
            d = self._wheel_filt - _prev_filt
        self._wheel_last_t = now
        # Clockwise thumb motion DECREASES the atan2 angle, so accumulating the
        # raw signed delta drives the accumulator NEGATIVE clockwise → down.
        self._wheel_acc += d
        # Scale by the Options "Scrolling Sensitivity" slider (the same
        # multiplier the linear scroll reads), referenced so the default
        # sensitivity reproduces the tuned WHEEL_STEP_DEG feel; higher = faster.
        mult = adusk_state.get_sc_scroll_speed() / self.WHEEL_SCROLL_SPEED_REF
        if mult < 0.05:
            mult = 0.05
        # "Invert Scrolling" (Touchpads scroll-settings cog): flips the dial
        # direction too.
        inv = -1 if adusk_state.is_sc_scroll_invert_enabled() else 1
        if smooth:
            # Analog: convert the accumulated rotation to hi-res wheel units
            # (WHEEL_SMOOTH_UNITS_PER_NOTCH per WHEEL_STEP_DEG, scaled by the
            # sensitivity) and stream the whole-unit part straight through
            # MOUSEEVENTF_WHEEL, carrying the sub-unit remainder as radians.
            # Modern apps (browsers) read these 1/120-notch deltas as smooth
            # pixel scrolling. Negative = down (clockwise), matching notch mode.
            k = (self.WHEEL_SMOOTH_UNITS_PER_NOTCH
                 / math.radians(self.WHEEL_STEP_DEG)) * mult
            units = int(self._wheel_acc * k)
            if units:
                self._wheel_acc -= units / k
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, units * inv, 0)
            return
        # Clicky: one whole notch per (sensitivity-scaled) WHEEL_STEP_DEG, with
        # a haptic detent tick. A faster slider = a smaller effective step =
        # more notches (and ticks) per rotation = faster scroll.
        step = math.radians(self.WHEEL_STEP_DEG) / mult
        while abs(self._wheel_acc) >= step:
            if self._wheel_acc <= -step:
                self._wheel_acc += step
                self._chord.mouse.scroll(0, -1 * inv)  # clockwise → scroll down
            else:
                self._wheel_acc -= step
                self._chord.mouse.scroll(0, 1 * inv)   # counter-clockwise → up
            if adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_pad_click()  # wheel detent tick

    def _handle_pad_text_wheel(self, sc, sci, now):
        """Text Wheel Selection (Options → Touchpads): while a left-click
        control (R2 / right-pad click) HOLDS the left mouse button over text,
        the LEFT pad becomes a fine text-selection dial. Each
        TEXTWHEEL_STEP_DEG of thumb rotation nudges the CURSOR horizontally by
        TEXTWHEEL_STEP_PX with the drag still live  the app's own
        drag-selection then snaps the endpoint to character boundaries  with a
        haptic detent tick. CLOCKWISE extends forward, counter-clockwise back.
        Driving the REAL drag (instead of injecting Shift+Arrow, the first two
        attempts) is what makes it work everywhere: keyboard selection is dead
        on non-editable content (browser pages) without caret browsing, only
        extends an EXISTING highlight, and dies when the selection collapses at
        its anchor  a live mouse drag has none of those limits (it selects
        from zero and shrinks through the start point to grow the other way).
        Same dial angle math (atan2 + seam wrap + dead-zone) as
        _handle_pad_wheel."""
        if not (sci.buttons & SCButtons.LPADTOUCH):
            self._textwheel_angle = None
            self._textwheel_acc = 0.0
            return
        x, y = float(sci.lpad_x), float(sci.lpad_y)
        if (x * x + y * y) ** 0.5 < self.SCRUB_MIN_RADIUS:
            # Near the center atan2 is all noise  hold the last angle so
            # re-entering the ring continues smoothly.
            return
        ang = math.atan2(y, x)
        if self._textwheel_angle is None:
            self._textwheel_angle = ang   # first ring sample: reference only
            return
        d = ang - self._textwheel_angle
        # Wrap to (-pi, pi] so crossing the ±180° seam doesn't spin the dial.
        if d > math.pi:
            d -= 2.0 * math.pi
        elif d <= -math.pi:
            d += 2.0 * math.pi
        self._textwheel_angle = ang
        # Clockwise thumb motion DECREASES the atan2 angle → select forward.
        self._textwheel_acc -= d
        step = math.radians(self.TEXTWHEEL_STEP_DEG)
        while abs(self._textwheel_acc) >= step:
            if self._textwheel_acc > 0:
                self._textwheel_acc -= step
                dx = self.TEXTWHEEL_STEP_PX       # clockwise → extend right
            else:
                self._textwheel_acc += step
                dx = -self.TEXTWHEEL_STEP_PX      # ccw → extend left
            self._chord.mouse.move(dx, 0)
            if adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_pad_click()             # detent tick

    def _handle_pad_scroll(self, sc, sci, now):
        """Desktop takeover: LEFT trackpad → scroll wheel. Accumulate the touch's
        vertical delta and emit whole wheel notches past the threshold, carrying
        the remainder. Speed = the tray 'Left Trackpad Scroll Speed' multiplier.
        In "Laptop scrolling" (Options → Touchpads) a quick swipe-and-lift keeps
        the page coasting with a smooth deceleration; a gentle tap catches it. In
        "Wheel scrolling" the left pad becomes a circular dial instead (see
        _handle_pad_wheel)."""
        mode = adusk_state.get_sc_scroll_mode()
        if mode in ("wheel", "wheel_smooth"):
            self._handle_pad_wheel(sc, sci, now, smooth=(mode == "wheel_smooth"))
            return
        laptop = mode == "laptop"
        if not (sci.buttons & SCButtons.LPADTOUCH):
            if self._lpad_prev is not None:
                # Lift edge: fast enough swipe → start coasting at the finger's
                # release velocity (windowed average, see SCROLL_VELOCITY_WINDOW).
                self._lpad_prev = None
                if laptop and len(self._lscroll_hist) >= 2:
                    t0, p0 = self._lscroll_hist[0]
                    dt = now - t0
                    if dt > 1e-3:
                        v = (self._lscroll_pos - p0) / dt
                        if abs(v) >= self.SCROLL_FLING_TRIGGER:
                            self._scroll_fling_v = max(
                                -self.SCROLL_FLING_MAX,
                                min(self.SCROLL_FLING_MAX, v))
                            self._scroll_fling_last_t = now
                self._lscroll_hist.clear()
            if self._scroll_fling_v:
                if not laptop:  # mode switched mid-coast  stop dead
                    self._scroll_fling_v = 0.0
                    return
                dt = now - self._scroll_fling_last_t
                dt = max(1e-3, min(dt, 1.0 / 30.0))  # clamp so a stall can't lurch
                self._scroll_fling_last_t = now
                self._scroll_acc += self._scroll_fling_v * dt
                self._scroll_fling_v *= math.exp(-dt / self.SCROLL_FLING_TAU)
                if abs(self._scroll_fling_v) < self.SCROLL_FLING_STOP:
                    self._scroll_fling_v = 0.0
                self._emit_scroll(laptop)
            return
        # Touching: any contact catches a coasting page dead (the gentle tap).
        self._scroll_fling_v = 0.0
        x, y = sci.lpad_x, sci.lpad_y
        if self._lpad_prev is None:
            # Fresh touch  seed the filter + anchor at the touch point (so the
            # first frame can't jump) and restart the lift-velocity tracking.
            self._lpad_filt = float(y)
            self._lpad_dfilt = 0.0
            self._lpad_anchor = float(y)
            self._lpad_last_t = now
            self._lscroll_pos = 0.0
            self._lscroll_hist.clear()
        else:
            dt = now - self._lpad_last_t if self._lpad_last_t else 0.0
            if dt <= 0.0 or dt > 0.1:
                dt = 1.0 / 60.0
            self._lpad_last_t = now
            if laptop:
                # Laptop mode: gentle 1-D 1€ filter (LSCROLL_EURO_*)  the
                # low-passed speed estimate ignores 1-frame stick-slip spikes
                # (the skin-catch jolts stay closed and get smeared smooth),
                # while a sustained swipe opens the cutoff and scrolls as
                # directly as before.
                ad = dt / (1.0 / (2.0 * math.pi * self.LSCROLL_EURO_DCUTOFF) + dt)
                self._lpad_dfilt += ad * (
                    (y - self._lpad_prev[1]) / dt - self._lpad_dfilt)
                fc = (self.LSCROLL_EURO_MINCUTOFF
                      + self.LSCROLL_EURO_BETA * abs(self._lpad_dfilt))
                a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
            else:
                # Normal mode: adaptive position-domain low-pass (two-knee tau
                # blend): heavy when the finger moves slowly (de-shakes a shaky
                # thumb), near-raw on a fast swipe (snappy)  tuned separately;
                # swipes want the raw top end.
                pad_speed = abs(y - self._lpad_prev[1]) / dt
                t = (pad_speed - self.PAD_SMOOTH_SPEED_LO) / (
                    self.PAD_SMOOTH_SPEED_HI - self.PAD_SMOOTH_SPEED_LO)
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                tau = self.PAD_SMOOTH_TAU_SLOW + (
                    self.PAD_SMOOTH_TAU_FAST - self.PAD_SMOOTH_TAU_SLOW) * t
                a = dt / (tau + dt)
            self._lpad_filt += a * (y - self._lpad_filt)
            # Same trailing dead-zone as the right pad, 1-D: the page scrolls
            # only by the filtered point's OVERSHOOT past PAD_DEADZONE, and the
            # anchor re-trails to stay exactly PAD_DEADZONE behind  a resting
            # or tremoring finger gives ZERO scroll (no jiggle), real motion
            # ramps in from zero and then tracks 1:1.
            d = self._lpad_filt - self._lpad_anchor
            if abs(d) > self.PAD_DEADZONE:
                sign = 1.0 if d > 0 else -1.0
                over = (abs(d) - self.PAD_DEADZONE) * sign
                self._lpad_anchor = self._lpad_filt - self.PAD_DEADZONE * sign
                speed = self.PAD_SCROLL_SCALE * adusk_state.get_sc_scroll_speed()
                # Pad up (+y) scrolls up (+notches), natural wheel direction.
                delta = over * speed
                self._scroll_acc += delta
                self._lscroll_pos += delta
                self._emit_scroll(laptop)
        self._lpad_prev = (x, y)
        self._lscroll_hist.append((now, self._lscroll_pos))
        while (self._lscroll_hist
               and now - self._lscroll_hist[0][0] > self.SCROLL_VELOCITY_WINDOW):
            self._lscroll_hist.popleft()

    def _mouse_shake_guard(self):
        """Freeze pad-mouse output for PAD_CLICK_FREEZE_S: squeezing a
        trigger or clicking a pad physically wobbles the finger resting on
        the right pad, and that wobble used to leak out as a tiny drag
        between the two clicks of a double-click (folders dragged instead of
        opening). The dead-zone anchor snaps to the filtered point so any
        pending overshoot dies with it; _handle_pad_mouse lets a REAL drag
        break out of the freeze early."""
        self._mouse_freeze_until = time.monotonic() + self.PAD_CLICK_FREEZE_S
        self._freeze_acc = 0.0
        # A physical pad/trigger click edge also kills any live tap-to-click
        # candidate (both pads): the real button won this touch, so the
        # eventual lift must not add a second, synthetic click.
        self._tap_start = None
        self._lpad_tap_start = None
        if self._rpad_filt is not None and self._rpad_anchor is not None:
            self._rpad_anchor[0] = self._rpad_filt[0]
            self._rpad_anchor[1] = self._rpad_filt[1]

    def _trigger_click_now(self, was_pressed, digital, analog, thr):
        """Whether an L2/R2 trigger counts as a mouse click this frame. The
        firmware full-pull digital bit always counts. With an analog actuation
        threshold set, the pull also counts past it  but with HYSTERESIS: it
        engages at `thr` and only releases once the pull falls
        TRIGGER_CLICK_HYSTERESIS below `thr`. That dead band absorbs the
        sensor noise / finger tremor that otherwise spam-clicks when the pull
        is held right at the actuation point."""
        if thr is None:
            return digital
        engage = thr if not was_pressed else thr - self.TRIGGER_CLICK_HYSTERESIS
        return digital or analog >= engage

    def _handle_virtual_menu(self, sc, sci, now):
        """Virtual Menus: while a pad with an assigned menu is being
        touched, the on-screen menu shows. Touch menus + radial menus
        highlight from the thumb position (grid cell / donut sector) and a
        pad CLICK fires the highlighted entry; a Hot Bar ignores the thumb
        and is CLICKED THROUGH  each pad click advances to the next slot
        and fires it. Actions are the desktop keyboard/mouse/system
        vocabulary, dispatched like a chord. Returns the set of pads a menu
        owns THIS frame  the callers suppress those pads' normal
        mouse/scroll/click behavior. Works in desktop takeover AND gamepad
        mode (the gamepad path masks the owned pads' bits out of the
        XInput frame)."""
        ver = adusk_state.get_virtual_menus_version()
        if ver != self._vmenu_ver:
            self._vmenu_ver = ver
            self._vmenu_rebuild_triggers()
            if self._vmenu_overlay is not None:
                self._vmenu_overlay.hide()
            self._vmenu_trigger = None
            self._vmenu_hl = None
            self._vmenu_click_prev = False
            self._vmenu_hotbar_idx = {}
            self._vmenu_toggle_on = {}
            self._vmenu_held_prev = {}
            self._vmenu_force_closed = set()
        self._vm_owned_now = ()
        self._vmenu_suppress_bits = 0
        if not self._vmenu_by_trigger:
            return ()
        click_bits = {"lpad": SCButtons.LPAD, "rpad": SCButtons.RPAD}
        # "toggle" activation style: latch each such trigger's open/closed
        # state on its OWN fresh press-edge (touch-down for a pad trigger,
        # button-down for everything else)  tracked for every armed trigger
        # every frame, independent of which one ends up picked below, so a
        # backgrounded trigger's edge is never missed while another menu is
        # showing. Non-toggle triggers just get their held state recorded (and
        # any stale latch dropped, in case a menu's style was just switched
        # away from "toggle").
        #
        # Closing your OWN toggle is always allowed. OPENING one is only
        # allowed when nothing else is currently engaged (self._vmenu_trigger
        # is None or this same trigger)  without that guard, pressing a
        # second toggle trigger while a different menu is showing would arm
        # its latch silently in the background, and it would pop up
        # unannounced the moment the first one closes instead of needing its
        # own press. "One menu at a time" holds for toggle exactly like it
        # already does for the hold-based styles.
        #
        # closing_now collects any BUTTON trigger whose press-edge just flipped
        # its latch OFF (the "close" half of the toggle): that press must still
        # be masked out of the frame below exactly like an "open" press always
        # was, but by the time the active/suppress logic downstream runs, the
        # latch already reads closed and would otherwise skip masking it 
        # letting the same press that puts the menu away also fire the
        # button's own normal action underneath it.
        closing_now = set()
        for t in self._vmenu_hold_bit:
            raw = self._vmenu_trig_held(t, sci.buttons)
            if not raw:
                # Released at least once since an A-button fire force-closed
                # it (see _vmenu_force_closed)  the suppression has done its
                # job; a fresh press is free to reopen the menu normally.
                self._vmenu_force_closed.discard(t)
            mt = self._vmenu_by_trigger.get(t)
            if mt is not None and mt.get("activate", "toggle") == "toggle":
                if raw and not self._vmenu_held_prev.get(t, False):
                    if self._vmenu_toggle_on.get(t, False):
                        self._vmenu_toggle_on[t] = False
                        if t not in keybinds_runtime.VMENU_PAD_TRIGGERS:
                            closing_now.add(t)
                    elif self._vmenu_trigger in (None, t):
                        self._vmenu_toggle_on[t] = True
            else:
                self._vmenu_toggle_on.pop(t, None)
            self._vmenu_held_prev[t] = raw
        # Multiple menus can be armed at once (each on its own trigger); ONE is
        # engaged at a time  a menu keeps its trigger until it's released (or,
        # for "toggle", until the latch above flips back off), and a fresh
        # pick prefers a held BUTTON over an incidental pad touch (see
        # _vmenu_trig_order). A PAD trigger's held-bit is its TOUCH bit; a
        # BUTTON trigger's is the button itself.
        # _vmenu_hold_bit may be TWO bits ORed together (an optional second
        # combo button  see _vmenu_rebuild_triggers), so "held" means ALL of
        # them are down, not just any one  `& mask == mask`, not the old
        # truthy `&` test.
        active = self._vmenu_trigger
        if active is not None and (
                active not in self._vmenu_by_trigger
                or not self._vmenu_trig_active(active, sci.buttons)):
            active = None
        if active is None:
            for t in self._vmenu_trig_order:
                if self._vmenu_trig_active(t, sci.buttons):
                    active = t
                    break
        prev = self._vmenu_trigger
        self._vmenu_trigger = active
        if active is None:
            if prev is not None:
                if self._vmenu_overlay is not None:
                    self._vmenu_overlay.hide()
                # Fire on release for the Touch Release style (and for Release
                # when the nav-pad click let go on the same frame as the
                # trigger release). Uses the entry highlighted at release.
                pm = self._vmenu_by_trigger.get(prev)
                if pm is not None and self._vmenu_hl is not None:
                    pst = pm.get("activate", "toggle")
                    if pst == "touch_release" or (
                            pst == "release" and self._vmenu_click_prev):
                        self._vmenu_fire_entry(
                            sc, pm, prev, pm.get("type", "touch"),
                            pm.get("entries") or [], self._vmenu_hl, now)
            # See closing_now above: `prev` itself may be the trigger whose
            # OWN press just closed it (active already reads None this same
            # frame), so mask its bit here too  otherwise this exact press
            # leaks through as a normal button press the instant the menu
            # goes away.
            if prev in closing_now:
                self._vmenu_suppress_bits = int(self._vmenu_hold_bit.get(prev, 0))
            self._vmenu_hl = None
            self._vmenu_click_prev = False
            return ()
        m = self._vmenu_by_trigger[active]
        entries = m["entries"]
        style = m.get("type", "touch")
        # A pad trigger navigates by its OWN thumb; a button trigger navigates
        # by the RIGHT pad's thumb (right-pad click fires).
        nav_pad = active if active in keybinds_runtime.VMENU_PAD_TRIGGERS \
            else "rpad"
        x = float(sci.lpad_x if nav_pad == "lpad" else sci.rpad_x)
        y = float(sci.lpad_y if nav_pad == "lpad" else sci.rpad_y)
        nx = min(1.0, max(0.0, (x + 32768.0) / 65535.0))
        ny = min(1.0, max(0.0, (32767.0 - y) / 65535.0))
        # Thumb-based aiming only runs while a finger is actually ON the nav
        # pad  an untouched pad has no real position to aim from (nx/ny just
        # reads whatever the hardware last reported, often the center), and
        # recomputing idx from it every frame would fight the DPAD/left-stick
        # navigation _vmenu_full_takeover adds on top. Untouched, the
        # highlight simply stays wherever it already is.
        touch_bit = (SCButtons.LPADTOUCH if nav_pad == "lpad"
                    else SCButtons.RPADTOUCH)
        pad_touched = bool(sci.buttons & touch_bit)
        if style == "hotbar":
            # Clicked-through: the highlight is the REMEMBERED slot, not
            # the thumb (position is ignored by design).
            idx = self._vmenu_hotbar_idx.get(
                (active, m.get("name", "")), 0) % max(1, len(entries))
        elif not pad_touched:
            idx = self._vmenu_hl
        elif style == "radial":
            idx = keybinds_runtime.vmenu_radial_at(len(entries), nx, ny)
        else:
            # Hysteresis around cell edges: a finger resting on a boundary
            # can't rapid-flip the highlight (and spam the haptic tick) on
            # pad jitter  it must move clearly INTO the neighbor to switch.
            idx = keybinds_runtime.vmenu_cell_at_hyst(
                len(entries), nx, ny, self._vmenu_hl)
        if self._vmenu_overlay is None:
            self._vmenu_overlay = vmenu.TouchMenuOverlay()
        # Draw the OSK thumb cursor only while touching the nav pad  a
        # hotbar never shows it (position is ignored by design) and an
        # untouched pad has nothing real to point at.
        thumb = (nx, ny) if (style != "hotbar" and pad_touched) else None
        if idx != self._vmenu_hl:
            if style != "hotbar" and idx is not None \
                    and self._vmenu_hl is not None \
                    and adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_click(freq_hz=280, count=2)   # cell-change tick
            self._vmenu_hl = idx
        # Always call show()  it re-renders only when its key (which now
        # includes the quantized thumb position) actually changed, so the
        # cursor tracks the finger smoothly without redundant redraws.
        self._vmenu_overlay.show(entries, highlight=idx, style=style,
                                 thumb=thumb, **self._vmenu_overlay_opts(m))
        # Activation Style  when the highlighted entry actually fires:
        #   toggle      : fires on the CLICK press edge, same as "click"  the
        #                 style only changes how the MENU shows (see the edge
        #                 pass above), not when a highlighted entry fires
        #   click       : on the pad/stick CLICK press edge (default)
        #   release     : when that click is let go (on-pad, or on lift)
        #   touch_release: on finger-lift (handled in the active-None branch)
        #   continuous  : repeatedly while touching (throttled auto-repeat)
        act_style = m.get("activate", "toggle")
        click = bool(sci.buttons & click_bits[nav_pad])
        press_edge = click and not self._vmenu_click_prev
        release_edge = (not click) and self._vmenu_click_prev
        do_fire = False
        if act_style in ("click", "toggle"):
            do_fire = press_edge
        elif act_style == "release":
            do_fire = release_edge
        elif act_style == "continuous":
            do_fire = (now - self._vmenu_last_fire) >= self._VMENU_CONTINUOUS_S
        if do_fire:
            self._vmenu_fire_entry(sc, m, active, style, entries, idx, now,
                                   haptic=(act_style != "continuous"))
            self._vmenu_last_fire = now
        self._vmenu_click_prev = click
        # The NAV pad's mouse-click path must stay quiet while the menu owns it
        #  the same self-clearing lock the pinch guard uses (held until the
        # pad is physically released, so no click leaks on the way out).
        if nav_pad == "lpad":
            self._lpad_click_lock = True
        else:
            self._rpad_click_lock = True
        # A menu-owned left pad can never fire a page swipe on lift.
        if nav_pad == "lpad" and self._swipe_track is not None:
            self._swipe_track["bad"] = True
        # A BUTTON trigger's own bit (PLUS an optional 2nd combo button, on
        # either kind of primary trigger) is masked out of the frame this turn
        # so holding it to open the menu can't also fire its normal action. A
        # pad-primary trigger's touch bit is never an "action" to suppress 
        # its click already goes through the pad lock above.
        if active not in keybinds_runtime.VMENU_PAD_TRIGGERS:
            self._vmenu_suppress_bits = int(self._vmenu_hold_bit[active])
        else:
            self._vmenu_suppress_bits = int(self._vmenu_combo_bit.get(active, 0))
        self._vm_owned_now = (nav_pad,)
        return self._vm_owned_now

    def _vmenu_full_takeover(self, sc, sci, now):
        """Runs FIRST in on_input, before anything else touches the frame:
        while a Virtual Menu is showing, the DPAD and LEFT STICK become its
        box-selection input and A fires the highlighted entry  nothing else
        sees this frame at all while that's true: no chord, no desktop
        mouse/keyboard, no gamepad output. Returns True while the frame
        belongs to the menu (the caller must stop processing it further),
        which includes the one CLOSING frame too  the press that dismisses
        the menu must not also leak through as its own action underneath it,
        same reasoning as _vmenu_suppress_bits already applied to the pad
        path alone.

        Engagement, the trigger itself, thumb-based aiming, the overlay and
        each Activation Style's own fire timing are ALL still exactly
        _handle_virtual_menu  this only adds the digital nav + A-button fire
        on top of an already-engaged menu, and owns the stop-everything-else
        decision for the caller."""
        was_active = self._vmenu_trigger is not None
        self._handle_virtual_menu(sc, sci, now)
        active = self._vmenu_trigger
        # Tell the config GUI to stand its own navigation down while the menu
        # is up: it polls the published frames itself, so returning early
        # below stops OUR dispatch but not its (see sc_viewer.set_vmenu_open).
        sc_viewer.set_vmenu_open(active is not None)
        if active is None:
            self._vmenu_nav_zone_prev = "NEUTRAL"
            self._vmenu_a_prev = False
            if not was_active:
                return False        # nothing to do with this frame at all
        else:
            m = self._vmenu_by_trigger.get(active)
            entries = (m.get("entries") or []) if m is not None else []
            style = m.get("type", "touch") if m is not None else "touch"
            b = sci.buttons
            zone = "NEUTRAL"
            if b & SCButtons.DPAD_UP or sci.lstick_y > self.STICK_DEADZONE:
                zone = "UP"
            elif b & SCButtons.DPAD_DOWN or sci.lstick_y < -self.STICK_DEADZONE:
                zone = "DOWN"
            elif b & SCButtons.DPAD_LEFT or sci.lstick_x < -self.STICK_DEADZONE:
                zone = "LEFT"
            elif b & SCButtons.DPAD_RIGHT or sci.lstick_x > self.STICK_DEADZONE:
                zone = "RIGHT"
            move = False
            if zone != self._vmenu_nav_zone_prev:
                move = zone != "NEUTRAL"
                self._vmenu_nav_repeat_at = now + self.ARROW_HOLD_DELAY
            elif zone != "NEUTRAL" and now >= self._vmenu_nav_repeat_at:
                move = True
                self._vmenu_nav_repeat_at = now + self.ARROW_REPEAT
            self._vmenu_nav_zone_prev = zone
            if move and entries and m is not None:
                new_hl = keybinds_runtime.vmenu_neighbor(
                    style, len(entries), self._vmenu_hl, zone.lower())
                if new_hl != self._vmenu_hl:
                    self._vmenu_hl = new_hl
                    if self._vmenu_overlay is not None:
                        self._vmenu_overlay.show(
                            entries, highlight=new_hl, style=style,
                            thumb=None, **self._vmenu_overlay_opts(m))
                    if adusk_state.is_rumble_enabled(self._kind):
                        sc.haptic_click(freq_hz=280, count=2)
            # A fires immediately on its own press-edge, independent of the
            # menu's Activation Style (that style is about the TOUCH/pad
            # click's timing nuances  release-on-lift, continuous-repeat 
            # which don't apply to a discrete confirm button). Skipped when A
            # is itself part of the ACTIVE trigger, so a menu triggered by
            # holding A can't fire its very first frame off the same press
            # that opened it, before the user has aimed anything.
            a_bit = int(SCButtons.A)
            a_click = bool(b & a_bit)
            a_edge = a_click and not self._vmenu_a_prev
            self._vmenu_a_prev = a_click
            trig_bit = self._vmenu_hold_bit.get(active, 0)
            if (a_edge and entries and m is not None
                    and self._vmenu_hl is not None and not (trig_bit & a_bit)):
                self._vmenu_fire_entry(sc, m, active, style, entries,
                                       self._vmenu_hl, now)
                # Choosing a box with A closes the menu  it's a confirm
                # button, not just another way to click. "toggle" closes via
                # its own latch; every other style needs the force-closed
                # suppression too, since its trigger (a button held, or a pad
                # still touched) is very often still satisfied THIS frame,
                # and would otherwise reopen the menu again next frame.
                self._vmenu_toggle_on[active] = False
                self._vmenu_force_closed.add(active)
                if self._vmenu_overlay is not None:
                    self._vmenu_overlay.hide()
                self._vmenu_trigger = None
                self._vmenu_hl = None
                self._vmenu_click_prev = False
        # Full stop: drop anything the desktop dispatch might still be
        # holding from just before the menu opened (a mouse button, a
        # modifier key), and push one neutral report to the virtual pad so a
        # button held at that same moment doesn't stay reported as held
        # in-game for as long as the menu is up.
        self._chord.release_all_held()
        if self._gamepad is not None:
            try:
                self._gamepad.update(sci._replace(buttons=0, ltrig=0, rtrig=0),
                                     lstick_zero=True, rstick_zero=True)
            except Exception as e:
                print(f"gamepad update (vmenu takeover) failed; disabling: {e!r}")
                self._gamepad = None
        return True

    def _vmenu_trig_held(self, trig, buttons):
        """True when EVERY bit `trig` needs is currently down  its own bit,
        AND the optional 2nd combo button's bit if one is set (see
        _vmenu_rebuild_triggers). A plain truthy `buttons & mask` would fire
        on ANY one of the two, which is what a single-button trigger already
        does on its own bit; a combo needs both together, like every other
        chord in the app."""
        mask = self._vmenu_hold_bit.get(trig, 0)
        return mask != 0 and (buttons & mask) == mask

    def _vmenu_trig_active(self, trig, buttons):
        """Whether `trig` currently wants its menu shown: the latched
        open/closed state for a "toggle"-style menu (flipped by the per-frame
        edge pass at the top of _handle_virtual_menu), or the raw held/touched
        state for every other style  the behavior "toggle" didn't change.
        Force-closed (see _vmenu_force_closed) always wins  an A-button
        selection stays closed even while its hold-style trigger is still
        physically down, until that trigger is released at least once."""
        if trig in self._vmenu_force_closed:
            return False
        m = self._vmenu_by_trigger.get(trig)
        if m is not None and m.get("activate", "toggle") == "toggle":
            return self._vmenu_toggle_on.get(trig, False)
        return self._vmenu_trig_held(trig, buttons)

    def _vmenu_rebuild_triggers(self):
        """(Re)compile the enabled menus into trigger→menu + trigger→held-bit
        maps and a priority order (button triggers first, then rpad, lpad).
        A menu is armed only when its `enabled` toggle is on and it has a valid
        trigger + at least one entry. `hold_bit[trig]` is the primary trigger's
        bit ORed with an optional 2nd combo button's bit (so the two form a
        chord  see _vmenu_trig_held); `combo_bit[trig]` keeps that 2nd
        button's bit ALONE (0 if there isn't one) for the suppression logic in
        _handle_virtual_menu, which needs to mask it independently of the
        primary."""
        by_trig, hold_bit, combo_bit = {}, {}, {}
        for m in adusk_state.get_virtual_menus():
            trig = m.get("pad")
            if not m.get("enabled", True):
                continue
            if m.get("type") not in ("touch", "radial", "hotbar"):
                continue
            if trig == "none" or trig not in keybinds_runtime.VMENU_TRIGGER_IDS:
                continue
            if not m.get("entries"):
                continue
            if trig in keybinds_runtime.VMENU_PAD_TRIGGERS:
                bit = int(SCButtons.LPADTOUCH if trig == "lpad"
                          else SCButtons.RPADTOUCH)
            else:
                attr = keybinds_runtime.VMENU_TRIGGER_HOLD_BIT.get(trig)
                bit = int(getattr(SCButtons, attr, 0)) if attr else 0
                if not bit:
                    continue
            # Optional 2nd combo button  the two form a chord (both held
            # together). Invalid/unknown ids were already dropped to "none" by
            # vmenus_sanitize, so this is just a lookup.
            extra = 0
            trig2 = m.get("pad2", "none")
            if trig2 != "none":
                attr2 = keybinds_runtime.VMENU_TRIGGER_HOLD_BIT.get(trig2)
                extra = int(getattr(SCButtons, attr2, 0)) if attr2 else 0
            by_trig[trig] = m       # unique trigger (editor enforces this)
            hold_bit[trig] = bit | extra
            combo_bit[trig] = extra
        self._vmenu_by_trigger = by_trig
        self._vmenu_hold_bit = hold_bit
        self._vmenu_combo_bit = combo_bit
        self._vmenu_trig_order = [
            t for t, _l in keybinds_runtime.VMENU_TRIGGERS
            if t in by_trig and t not in keybinds_runtime.VMENU_PAD_TRIGGERS
        ] + [t for t in ("rpad", "lpad") if t in by_trig]

    # Continuous activation re-fires the highlighted entry this often while
    # the pad/stick stays touched (~8/s  auto-repeat without flooding).
    _VMENU_CONTINUOUS_S = 0.12
    # A Button-Combo entry pulses its Xbox digital outputs into the virtual
    # pad for this long (gamepad mode)  long enough for a game to register a
    # press from the momentary menu fire.
    _VMENU_PULSE_S = 0.09

    @staticmethod
    def _vmenu_overlay_opts(m):
        """The per-menu overlay presentation kwargs (position / size /
        opacity) pulled from a menu dict, for TouchMenuOverlay.show()."""
        return {"hpos": m.get("hpos"), "vpos": m.get("vpos"),
                "size": m.get("size", 100), "opacity": m.get("opacity", 100)}

    def _vmenu_fire_entry(self, sc, m, active, style, entries, idx, now,
                          haptic=True, overlay=None):
        """Fire one virtual-menu entry. For a Hot Bar this ADVANCES to the
        next slot first (weapon-cycle feel) and fires that, re-showing the
        overlay on the new slot. `idx` is the currently highlighted slot/cell.
        A button's OUTPUT = the row's simple `action` dropdown PLUS every
        extra Hotkey-style action in `actions` (the cog modal):
          keys         -> a Key Combo (press+release, modifiers first)
          launch       -> run the program
          button_combo -> fire key/mouse/system outputs momentarily + pulse
                          any Xbox digital outputs into the virtual pad.

        `overlay` names the window a Hot Bar re-shows itself on; it defaults to
        this watcher's own. The keyboard/mouse trigger path (_KeyVMenuRunner)
        borrows this method with its OWN overlay and sc=None  Win32 overlay
        windows belong to the thread that created them, so a Hot Bar advanced
        from the key thread must never touch the SC thread's window."""
        if not entries:
            return
        if overlay is None:
            overlay = self._vmenu_overlay
        fire_idx = idx
        if style == "hotbar":
            fire_idx = ((idx if idx is not None else 0) + 1) \
                % max(1, len(entries))
            self._vmenu_hotbar_idx[(active, m.get("name", ""))] = fire_idx
            self._vmenu_hl = fire_idx
            if overlay is not None:
                overlay.show(entries, highlight=fire_idx, style=style,
                             **self._vmenu_overlay_opts(m))
        if fire_idx is None or not (0 <= fire_idx < len(entries)):
            return
        e = entries[fire_idx]
        # Tell the guided tour which button was pressed  its Virtual Menus
        # slide asks for one specific entry. Published before the action runs
        # so an entry with NO action (the tour's demo button is exactly that)
        # still registers.
        sc_viewer.note_vmenu_fire(e.get("icon") or "none")
        # Drop any stale Button-Combo pulse before this fire OR's fresh flags.
        if now >= self._vmenu_pulse_until:
            self._vmenu_pulse_xflags = 0
        try:
            act_id = e.get("action") or "none"
            act = keybinds_runtime.resolve_action(act_id, sui.Keys)
            if act and act[0] != "none":
                self._fire_guide_action(sc, act, "pc")
            for a in (e.get("actions") or []):
                self._vmenu_fire_action(sc, a, now)
        except Exception as ex:
            print(f"vmenu fire failed: {ex!r}")
        if haptic and sc is not None and adusk_state.is_rumble_enabled(
                self._kind):
            sc.haptic_pad_click()

    def _vmenu_fire_action(self, sc, a, now):
        """Fire one extra Hotkey action from a menu button's `actions` list."""
        t = a.get("type")
        if t == "keys":
            self._vmenu_fire_keys(a.get("keys") or [])
        elif t == "launch":
            path = (a.get("path") or "").strip()
            if path:
                _launch_program(path, a.get("args", ""))
        elif t == "button_combo":
            self._vmenu_fire_button_combo(sc, a.get("outputs") or [], now)
        elif t == "powershell":
            # Type name is historical  the effect now takes PowerShell OR
            # batch, picked apart by _run_user_script.
            _run_user_script(a.get("script") or "")

    def _vmenu_fire_keys(self, keys_tokens):
        """Press+release a stored Key Combo token list (modifiers first)."""
        act = keybinds_runtime._parse_chord_action(
            {"type": "keys", "keys": keys_tokens}, sui.Keys)
        if not act:
            return
        for k in act["keys"]:
            self._chord.kb.pressEvent([k])
        for k in reversed(act["keys"]):
            self._chord.kb.releaseEvent([k])

    def _vmenu_fire_button_combo(self, sc, outputs, now):
        """Fire a Button-Combo action: keyboard/mouse/system outputs fire once
        (momentary), and Xbox DIGITAL outputs are pulsed into the virtual pad
        for _VMENU_PULSE_S (gamepad mode only  no pad, no Xbox output). Flags
        are OR'd so several combos in one button pulse together."""
        xflags = 0
        for oid in outputs:
            if oid in keybinds_runtime._GAMEPAD_OUTPUT_IDS:
                if oid not in ("none", "analog") \
                        and oid not in keybinds_runtime._GAMEPAD_ANALOG \
                        and self._gamepad is not None:
                    xflags |= self._gamepad.action_flag(oid)
            else:
                act = keybinds_runtime.resolve_action(oid, sui.Keys)
                if act and act[0] != "none":
                    self._fire_guide_action(sc, act, "pc")
        if xflags and self._gamepad is not None:
            self._vmenu_pulse_xflags |= xflags
            self._vmenu_pulse_until = now + self._VMENU_PULSE_S

    def _handle_pad_clicks(self, sc, sci):
        """Desktop takeover: pad CLICK buttons → mouse buttons (firmware lizard
        used to do this). Defaults preserve today's feel: right pad click = left
        click, left pad click = middle click. Held (ref-counted via the chord
        state) so a click can drag; a haptic pad-tick fires on the press edge.

        Pinch guard: while Pinch To Zoom is ENABLED, a pad click only counts
        when just ONE pad is touched. With BOTH pads touched (the two-finger
        pinch posture) every pad click is suppressed  so pressing the pads
        HARD to engage/hold a pinch can never also fire a left/middle mouse
        click, regardless of which pad crosses the click threshold first. A pad
        suppressed this way is LOCKED until it's physically released, so
        dropping back to one finger while the other is still pressed can't leak
        a stray click on the way out. (Guard is inert when Pinch is off  pad
        clicks behave exactly as before.)"""
        pinch_guard = bool(
            adusk_state.is_pinch_zoom_enabled()
            and (sci.buttons & SCButtons.LPADTOUCH)
            and (sci.buttons & SCButtons.RPADTOUCH))

        rpad = bool(sci.buttons & SCButtons.RPAD)
        if pinch_guard:
            self._rpad_click_lock = True
        if self._rpad_click_lock:
            if rpad:
                rpad = False          # suppressed by the pinch guard/lock
            else:
                self._rpad_click_lock = False  # released; a fresh click counts again
        if rpad != self._rpad_click_prev:
            self._mouse_shake_guard()
        if rpad and not self._rpad_click_prev:
            if adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_pad_click()
            # Learn this pad's CLICK pressure so Pinch To Zoom can engage at a
            # fraction of it (a lighter press than a full click).
            if sci.rpad_force:
                self._rpad_click_force = sci.rpad_force
            # A click is user activity (it also pops a video player's
            # controls)  arm the video-region probe like cursor motion
            # does, so pausing/clicking a video re-learns the region.
            self._probe_move_at = time.monotonic()
        self._chord.set_mouse_button("left", "rpad", rpad)
        self._rpad_click_prev = rpad

        lpad = bool(sci.buttons & SCButtons.LPAD)
        if pinch_guard:
            self._lpad_click_lock = True
        if self._lpad_click_lock:
            if lpad:
                lpad = False
            else:
                self._lpad_click_lock = False
        if lpad != self._lpad_click_prev:
            self._mouse_shake_guard()
        if lpad and not self._lpad_click_prev:
            if adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_pad_click()
            if sci.lpad_force:
                self._lpad_click_force = sci.lpad_force
        self._chord.set_mouse_button("middle", "lpad", lpad)
        self._lpad_click_prev = lpad

    def on_input(self, sc, sci):
        if sci.status != SCStatus.INPUT:
            return
        # Live keybinds-picker controller preview: hand the RAW frame over.
        # One call + one flag test per frame; a no-op while the picker is
        # hidden or showing a non-SC tab (the picker gates sc_viewer._active).
        sc_viewer.publish(sci)
        if self._should_abort():
            # Drop any held modifiers so they don't stick at the OS level when
            # this watcher tears down (e.g. tray Exit / Steam launch).
            self._chord.release_all_held()
            # Same for the Virtual Menu claim: a watcher that stops running
            # would otherwise leave the config GUI's navigation dead for good.
            sc_viewer.set_vmenu_open(False)
            sc.addExit()
            return

        # Keybinds-picker controller navigation: while the picker window is
        # visible AND foreground it consumes dpad/A/B/LB/RB to steer its own
        # UI (published raw above). Mask those bits out of EVERYTHING below 
        # desktop dispatch, chords and the virtual pad  so highlighting a
        # row doesn't also type/click/fire in-game input. Pads, sticks,
        # triggers and Steam/QAM stay live (mouse control keeps working).
        # A Steam/QAM-HELD frame is exempt: the picker navigates on bare
        # buttons only, so those chords belong to the normal dispatch (see
        # _GUIDE_BITS  this is what keeps Steam+X opening the OSK with the
        # GUI up, and stops a masked chord reading as a clean Steam tap).
        # A Virtual Menu that is ALREADY SHOWING outranks even the picker's
        # navigation claim: it is a full-screen-ish overlay the user is
        # actively steering, so it gets the RAW frame (dpad/left-stick move
        # its selection, A fires  see _vmenu_full_takeover) before the mask
        # below could strip those very bits out. Once engaged the takeover
        # always returns True, so this never falls through mid-menu.
        if self._vmenu_trigger is not None:
            self._vmenu_full_takeover(sc, sci, time.monotonic())
            return
        if sc_viewer.nav_claimed():
            if sc_viewer.listen_claimed():
                # Listen bind-capture: swallow EVERY press (see mask comment).
                # No guide exemption here  the press being captured as a
                # binding must not fire ANY action, chord or not.
                sci = sci._replace(buttons=sci.buttons & _PICKER_LISTEN_KEEP)
            elif sc_viewer.tutorial_claimed():
                # The first-run tour: an ALLOW-list, not a deny-list, and no
                # guide exemption. Only what the current slide actually
                # teaches survives (the tour publishes it through
                # sc_viewer.set_nav_keep) plus the mouse it needs to stay
                # usable. Somebody mashing every button to find the one the
                # slide wants must not be able to alt-tab, kill the foreground
                # game, flip to gamepad mode or close the manager by accident.
                # Detection is unaffected: publish() ran above this.
                sci = sci._replace(
                    buttons=sci.buttons & (sc_viewer.nav_keep()
                                           | _PICKER_LISTEN_KEEP))
            elif not (sci.buttons & _GUIDE_BITS):
                # ...minus anything the picker has asked us to spare: the tour's
                # keyboard slide teaches the bare-X open, and a masked X would
                # open nothing (see sc_viewer.set_nav_keep). One bit, only while
                # that step is outstanding.
                _mask = _PICKER_NAV_MASK & ~sc_viewer.nav_keep()
                sci = sci._replace(buttons=sci.buttons & ~_mask)
        # OPENING one runs after that mask, and so is gated by it: while the
        # config GUI is foreground a bare nav button can't pop a menu out from
        # under it, but a Guide-held chord (exempt from the mask) still can 
        # which is what the tour's own Guide + D-Pad Up demo menu relies on.
        if self._vmenu_full_takeover(sc, sci, time.monotonic()):
            return

        # Gamepad-mode toggle chord (Hotkeys "Gamepad Mode Toggle"). Evaluated
        # off the RAW frame in BOTH modes, and BEFORE steam_now/guide_now/x_now
        # are read, so Guide+button toggle chords that clear the STEAM bit from
        # sci also suppress all Steam-held logic below. The App fires once per
        # press and latches across the mode-switch watcher rebuild (so holding
        # the chord can't ping-pong). Held bits are masked out of sci so they
        # don't also fire their own action / hit the virtual pad.
        if self._gp_toggle_masks and self._on_gamepad_toggle is not None:
            _t_held = False
            _t_mask = 0
            for _m in self._gp_toggle_masks:
                if (sci.buttons & _m) == _m:
                    _t_held = True
                    _t_mask |= _m
            self._on_gamepad_toggle(_t_held)
            if _t_mask:
                sci = sci._replace(buttons=sci.buttons & ~_t_mask)

        # Built-in "hold ≡ (Start/Menu) to switch Desktop <-> Gamepad" gesture.
        # Same contract as the toggle chord above and evaluated right after it,
        # so a Start-component chord wins the frame (its bits are already gone
        # by the time the gesture sees them, and the gesture only fires on a
        # Start held ALONE anyway). Once it fires, the App returns the Start bit
        # for the rest of the press so it's swallowed here instead of also
        # firing Start's own binding / reaching the virtual pad.
        if self._on_mode_hold is not None:
            _h_mask = self._on_mode_hold(sci.buttons)
            if _h_mask:
                sci = sci._replace(buttons=sci.buttons & ~_h_mask)

        # Gyro-to-mouse toggle chord (per-controller Options "Gyro To Mouse"
        # hotkey)  same contract as the gamepad-mode toggle above: raw-frame
        # evaluation in BOTH modes, App-side latch, held bits masked out.
        if self._gyro_toggle_masks and self._on_gyro_toggle is not None:
            _g_held = False
            _g_mask = 0
            for _m in self._gyro_toggle_masks:
                if (sci.buttons & _m) == _m:
                    _g_held = True
                    _g_mask |= _m
            self._on_gyro_toggle(_g_held)
            if _g_mask:
                sci = sci._replace(buttons=sci.buttons & ~_g_mask)

        # Gyro-to-mouse drive: while toggled on, the controller's IMU angular
        # velocity moves the OS cursor (both modes  in gamepad mode it's a
        # gyro aim on top of the virtual pad, like Steam Input's gyro mouse).
        # The device's IMU stream follows the toggle so gyro data only costs
        # battery/CPU while actually in use.
        self._gyro_stick = None
        if self._gyro_mouse is not None:
            _g_act = bool(self._gyro_active()) if self._gyro_active else False
            if _g_act != self._gyro_imu_on:
                self._gyro_imu_on = _g_act
                sc.set_imu(_g_act)
                self._gyro_mouse.reset()
            if _g_act:
                _yaw = sci.gyaw * GYRO_DEG_PER_SEC
                _pitch = sci.gpitch * GYRO_DEG_PER_SEC
                if (self._gamepad is not None
                        and adusk_state.get_gyro_output(self._kind) == "rstick"):
                    # Gyro → right stick (Steam's "Gyro To Joystick"): shape
                    # with the same deadzone/precision/accel curves, then map
                    # rate to deflection. + yaw = turn left → stick left
                    # (-X); + pitch = tilt up → stick up (+Y). Consumed by
                    # gamepad.update below; the OS cursor stays untouched.
                    _yaw, _pitch = adusk_state.gyro_shape(
                        self._kind, _yaw, _pitch)
                    _k = adusk_state.get_gyro_stick_gain(self._kind)
                    if _yaw or _pitch:
                        self._gyro_stick = (int(-_yaw * _k), int(_pitch * _k))
                else:
                    self._gyro_mouse.feed(_yaw, _pitch,
                                          time.monotonic(), self._kind)

        # Steam and "..." (QAM) are separate bits, but BOTH act as the guide/chord
        # modifier: holding either one fires the guide chords (Chords tab binds,
        # Hotkeys guide chords, guide stick zones and the built-in Y/B/VIEW/X/media
        # chords) and suppresses the plain single-button desktop handlers so the
        # held button feeds the chord. `guide_now` = "Steam OR ..." in both modes.
        # `steam_now` alone is kept ONLY for `_handle_chords` (the Hotkeys two-
        # button editor), so "..." stays usable there as an independent button.
        # Read AFTER the toggle block so Guide+button toggle chords (which clear
        # STEAM from sci) also suppress the Steam-held paths below.
        steam_now = bool(sci.buttons & SCButtons.STEAM)
        qam_now = bool(sci.buttons & SCButtons.QAM)
        guide_now = steam_now or qam_now
        x_now = bool(sci.buttons & SCButtons.X)

        # Hotkeys "Button Combo" effects. Evaluated HERE (before gamepad.update)
        # so the held combos' XUSB flags (self._combo_extra) can be OR'd into the
        # pad frame below; also holds/edge-fires any keyboard/mouse/system outputs
        # and sets self._combo_suppress (masked out with the chord suppress later).
        if self._button_combos:
            self._handle_button_combos(sc, sci, steam_now, guide_now)

        # Release Alt-Tab on Steam release BEFORE we touch the gamepad. If
        # we let gamepad.update push an XInput frame before releasing Alt,
        # the next-window commit gets dropped in gamepad mode (alt-tab UI
        # stays up and the user has to press A to confirm). In passive mode
        # this didn't matter because nothing was pushing XInput.
        if not guide_now:
            self._chord.release_alt()

        if self._gamepad is not None:
            # On the rising edge of Steam/QAM, clear any held XInput state so
            # buttons pressed just before the chord can't get stuck in the game.
            if guide_now and not self._steam_hold_lizard:
                self._gamepad.reset()
                self._steam_hold_lizard = True
            elif not guide_now and self._steam_hold_lizard:
                self._steam_hold_lizard = False

            # Latch-based mode selection during a Steam hold:
            #   * Touch the right pad → "mouse mode" latched on for the rest
            #     of the hold (lizard ON). Capacitive touch flickers when
            #     fingers shift, so latching avoids rapid lizard toggling
            #     that would break click and make movement feel stuttery.
            #   * Press VIEW → "chord mode" latched on for the rest of the
            #     hold (lizard OFF). Wins over mouse mode so the Steam+VIEW
            #     =Alt+Tab injection isn't fighting firmware-emitted keys.
            # Both latches reset when Steam is released.
            rpad_touched = bool(sci.buttons & SCButtons.RPADTOUCH)
            view_for_lizard = bool(sci.buttons & SCButtons.VIEW)
            if not guide_now:
                self._steam_hold_pad_used = False
                self._steam_hold_chord_used = False
            else:
                if rpad_touched:
                    self._steam_hold_pad_used = True
                if view_for_lizard:
                    self._steam_hold_chord_used = True
            want_lizard = (guide_now
                           and self._steam_hold_pad_used
                           and not self._steam_hold_chord_used)
            if want_lizard != self._gamepad_lizard_on:
                sc.set_lizard(want_lizard)
                self._gamepad_lizard_on = want_lizard

            # Gamepad Mode Trigger Actuation: synthesize the L2/R2 digital bits
            # from the analog pull vs the configured threshold (with the same
            # hysteresis _trigger_click_now uses for the desktop mouse click), so
            # a trigger REBOUND in the Gamepad tab to a digital button or a
            # keyboard action fires at that pull instead of only the firmware
            # full-pull bit. A trigger left as the analog LT/RT axis is
            # unaffected  its bit is not in the button_map / key overrides, and
            # the analog axis passthrough reads sci.ltrig/rtrig directly. Kept
            # LOCAL (sci_gp) so chords / button-combos still read the real
            # firmware bits from sci. Threshold None ("High") = firmware only.
            _gp_thr = adusk_state.get_sc_gamepad_trigger_threshold()
            _lt_dig = bool(sci.buttons & SCButtons.LT)
            _rt_dig = bool(sci.buttons & SCButtons.RT)
            self._gp_lt_was = self._trigger_click_now(
                self._gp_lt_was, _lt_dig, sci.ltrig, _gp_thr)
            self._gp_rt_was = self._trigger_click_now(
                self._gp_rt_was, _rt_dig, sci.rtrig, _gp_thr)
            _eb = sci.buttons
            _eb = (_eb | SCButtons.LT) if self._gp_lt_was else (_eb & ~SCButtons.LT)
            _eb = (_eb | SCButtons.RT) if self._gp_rt_was else (_eb & ~SCButtons.RT)
            sci_gp = sci if _eb == sci.buttons else sci._replace(buttons=_eb)

            # Virtual Menus: handled ONCE, up front in on_input, as a full
            # frame takeover (_vmenu_full_takeover)  a menu showing never
            # reaches this far down, so there is nothing left to mask here.

            # Advanced press actions (Long/Double/Soft rows): the engine
            # decides what each owned control's press means this frame. Owned
            # bits are masked out of the frame the button_map / key overrides
            # see (the engine emits their regular action itself); asserted
            # specs land as XUSB flags (_adv_xflags, OR'd into update below)
            # or injected keys. Disabled while Steam/"..." is held so guide
            # chords keep priority  matching the key-override gating.
            if self._adv_engine is not None:
                _asserted = self._adv_engine.step(
                    sci_gp.buttons, sci.ltrig, sci.rtrig,
                    time.monotonic(), enabled=not guide_now)
                self._apply_adv_specs(sc, _asserted)
                # frame_mask = long/double owners + shift holders + the
                # targets of any HELD shift (their normal binding pauses
                # while the layer is active).
                _owned = self._adv_engine.frame_mask
                if _owned:
                    sci_gp = sci_gp._replace(buttons=sci_gp.buttons & ~_owned)

            # Always push an XInput frame  Steam/QAM bits are in the
            # button_map so the user's remapped Guide (or other) button stays
            # live while held. Chord buttons (VIEW, X, etc.) also output their
            # mapped XInput button during a chord, which is acceptable since
            # gaming chords (Steam+X = OSK, Steam+VIEW = Alt-Tab) are desktop
            # overlays, not in-game actions.
            try:
                # Base extra flags = any held Button-Combo Xbox outputs + the
                # advanced-press engine's asserted outputs this frame + any
                # momentary Virtual-Menu Button-Combo pulse still in its window.
                _extra = self._combo_extra | self._adv_xflags
                if time.monotonic() < self._vmenu_pulse_until:
                    _extra |= self._vmenu_pulse_xflags
                _lz = self._gp_lstick_dir is not None
                _rz = self._gp_rstick_dir is not None
                if _lz:
                    _lx, _ly = sci.lstick_x, sci.lstick_y
                    if abs(_lx) > self.STICK_DEADZONE or abs(_ly) > self.STICK_DEADZONE:
                        _lzone = ("UP" if _ly > 0 else "DOWN") if abs(_ly) >= abs(_lx) \
                            else ("RIGHT" if _lx > 0 else "LEFT")
                        _extra |= self._gp_lstick_dir.get(_lzone, 0)
                if _rz:
                    _rx, _ry = sci.rstick_x, sci.rstick_y
                    if abs(_rx) > self.STICK_DEADZONE or abs(_ry) > self.STICK_DEADZONE:
                        _rzone = ("UP" if _ry > 0 else "DOWN") if abs(_ry) >= abs(_rx) \
                            else ("RIGHT" if _rx > 0 else "LEFT")
                        _extra |= self._gp_rstick_dir.get(_rzone, 0)
                self._gamepad.update(sci_gp, self._gamepad_map,
                                     self._gamepad_lt_analog,
                                     self._gamepad_rt_analog,
                                     _extra, _lz, _rz,
                                     rstick_add=self._gyro_stick)
            except Exception as e:
                print(f"gamepad update failed; disabling: {e!r}")
                self._gamepad = None
            # After pushing the XInput frame, inject any gamepad-mode controls
            # the user bound to a keyboard/mouse/system action (excluded from
            # the button_map above, so they don't also emit XInput). Uses the
            # actuation-adjusted bits so a trigger bound to a key/click also
            # honours the Gamepad Mode Trigger Actuation threshold.
            if self._gp_key_overrides:
                self._handle_gamepad_key_overrides(sc, sci_gp.buttons, guide_now)
        else:
            # Desktop takeover: firmware lizard is already OFF (kept off by the
            # SteamController watchdog), so there's nothing to suppress on a Steam
            # hold  our chord/stick/pad injectors own the keyboard & mouse. The
            # actual desktop driving (trackpads, pad clicks, triggers, sticks,
            # Steam chords) happens below, gated on `self._gamepad is None`.
            pass

        # Two-button Hotkeys chords  fire in BOTH desktop and gamepad mode
        # (not gated on self._takeover). Fire on the both-held rising edge,
        # then mask the chord's buttons out of `sci` so the single-button
        # handlers below don't ALSO fire (e.g. A+B → chord, not Enter+Esc);
        # those handlers are all desktop-only, so the mask is a no-op in
        # gamepad mode. self._gamepad.update() above already ran with the
        # UNMASKED frame, so a chord's buttons may also register as their
        # normal XInput input in-game during the hold  acceptable, same as
        # the guide-chord precedent above ("gaming chords... are desktop
        # overlays, not in-game actions"). Guide-paired rows are a separate
        # path (build_guide_chords/_handle_guide_chords) and stay desktop-
        # only. Reads BEFORE this point (steam_now/x_now, the gamepad
        # branch) use the unmasked frame.
        if self._chords_runtime:
            suppress = self._handle_chords(sc, sci, steam_now)
            if suppress:
                sci = sci._replace(buttons=sci.buttons & ~suppress)
        # Mask Button-Combo trigger bits too (set in _handle_button_combos above),
        # so a combo's trigger buttons don't ALSO fire their single-button actions.
        if self._combo_suppress:
            sci = sci._replace(buttons=sci.buttons & ~self._combo_suppress)

        # Desktop-mode Advanced Presses: the pc engine decides Long/Double/
        # Soft/Shift/Extra presses on the takeover path. Its frame mask
        # (long/double owners + shift holders + held-shift targets) leaves
        # the frame BEFORE the per-control overrides and every hardcoded
        # handler, so an owned control's normal behavior is fully deferred
        # to the engine's decision; the trigger ANALOG is zeroed for owned
        # triggers so the actuation-threshold mouse clicks pause too.
        # Disabled while Steam/"..." is held (guide chords keep priority).
        if self._takeover and self._adv_engine_pc is not None:
            _a = self._adv_engine_pc.step(
                sci.buttons, sci.ltrig, sci.rtrig,
                time.monotonic(), enabled=not guide_now)
            self._apply_adv_specs(sc, _a, mode="pc")
            _m = self._adv_engine_pc.frame_mask
            if _m:
                _repl = {"buttons": sci.buttons & ~_m}
                if _m & SCButtons.LT:
                    _repl["ltrig"] = 0
                if _m & SCButtons.RT:
                    _repl["rtrig"] = 0
                sci = sci._replace(**_repl)

        # Per-control desktop rebinds (only controls the user changed from their
        # default). Dispatch them, then mask their bits so the hardcoded handlers
        # below skip those controls  unedited controls keep the built-in path.
        if self._takeover and self._sc_overrides:
            ov_suppress = self._handle_overrides(sc, sci.buttons, guide_now)
            if ov_suppress:
                sci = sci._replace(buttons=sci.buttons & ~ov_suppress)

        # Remember the window the user is typing in, sampled (≤10 Hz) only while
        # neither Steam nor X is held  i.e. BEFORE the opening press. When X is
        # then pressed to open the OSK, the firmware lizard also fires X's mouse
        # action onto the desktop, which can land off the field and steal focus;
        # adusk re-focuses this saved window after the OSK is up. Skip in active
        # gamepad mode (controller is a pad, not a desktop mouse/kb).
        if self._gamepad is None and not guide_now and not x_now:
            _now = time.monotonic()
            if _now - self._fg_poll_at > 0.1:
                self._fg_poll_at = _now
                tgt = _foreground_target_hwnd()
                if tgt:
                    self._last_user_hwnd = tgt

        # X opens the on-screen keyboard. In desktop mode bare X works (and
        # Steam+X too); in gamepad mode bare X is a face button, so only
        # Steam+X opens it. Rising-edge so one press = one open; releasing the
        # controller here lets adusk grab it. Suppressed while the workstation
        # is locked so it can't open behind the secure lock-screen desktop.
        # X alone always opens OSK in desktop mode; Steam+X also opens it unless
        # the user has rebound Steam+X in the Chords tab (guide bind takes over).
        # Desktop override rebinds for X apply to X alone only (Steam is held →
        # overrides are gated off, guide bind handles it instead).
        # `sci` was masked above for any active chord/override, so an X that's the
        # second button of a chord (e.g. "QAM + X" or "L1 + X") no longer leaks
        # through to open the OSK  the chord owns it.
        x_opens = (x_now and bool(sci.buttons & SCButtons.X)
                   and (self._gamepad is None or guide_now)
                   and not (guide_now and int(SCButtons.X) in self._guide_bind_bits))
        if x_opens and not self._x_open_was_pressed and not _workstation_locked():
            self.triggered = True
            sc.addExit()
        self._x_open_was_pressed = x_opens

        # Steam + VIEW (⧉) → Alt+Tab (hold-cycles the switcher) when the user
        # hasn't rebound VIEW in the Chords tab. When VIEW has a guide bind,
        # the guide dispatcher handles it and this block is skipped.
        view_now = bool(sci.buttons & SCButtons.VIEW)
        if int(SCButtons.VIEW) not in self._guide_bind_bits:
            if guide_now and view_now and not self._chord.view_was_pressed:
                if not self._chord.alt_held:
                    self._chord.kb.pressEvent([sui.Keys.KEY_LEFTALT])
                    self._chord.alt_held = True
                self._chord.kb.pressEvent([sui.Keys.KEY_TAB])
                self._chord.kb.releaseEvent([sui.Keys.KEY_TAB])
        self._chord.view_was_pressed = view_now
        # Alt release on Steam-release is handled near the top of this
        # method, before gamepad.update fires (see comment there).

        # One clock read shared by all the time-based handlers below (was three
        # separate monotonic() calls per frame).
        now = time.monotonic()

        # Steam / "..." TAP → bound action (hold + button still = chords). Runs
        # in BOTH modes: desktop fires the pc-tab tap, gamepad the gamepad-tab
        # tap (Guide → toggle the config GUI by default). The clean-tap detector
        # keeps the held gaming chords untouched.
        _taps = self._guide_taps_gp if self._gamepad is not None else self._guide_taps
        if _taps:
            self._handle_guide_taps(sc, sci, now, _taps)

        # Desktop: Steam/"..." HELD + button → guide-hold bind (Chords tab).
        if self._gamepad is None and guide_now and self._guide_binds:
            self._handle_guide_binds(sc, sci)

        # Desktop: Steam/"..." HELD → Guide chord (Hotkeys key combo / launch).
        # Called every frame (not gated on guide_now) so a Guide-alone chord's
        # per-hold edge resets when Steam is released.
        if self._gamepad is None and self._guide_chords:
            self._handle_guide_chords(sc, sci, guide_now)

        # Desktop: Steam/"..." HELD + right-stick zone → directional guide bind.
        if self._gamepad is None and guide_now and self._guide_rstick_zones:
            self._handle_guide_rstick(sc, sci)

        # Desktop: Steam/"..." HELD + left-stick zone → directional guide bind.
        if self._gamepad is None and guide_now and self._guide_lstick_zones:
            self._handle_guide_lstick(sc, sci)

        # Desktop mode: L3 (left stick click) ALONE → middle click at the cursor
        # (Steam+L3 is Play/Pause, handled in the media chords). Great for web
        # browsing  middle-click a link to open it in a new background tab, or a
        # tab to close it. The edge is tracked every frame so releasing Steam
        # while still holding L3 can't spuriously fire a click.
        l3_mid_now = bool(sci.buttons & SCButtons.L3)
        if (self._gamepad is None and not guide_now
                and l3_mid_now and not self._l3_mid_prev):
            self._chord.mouse.press("middle")
            self._chord.mouse.release("middle")
        self._l3_mid_prev = l3_mid_now

        # Steam + left stick / L3 → media transport. Cheap when Steam isn't held
        # (it just keeps its zone/edge bookkeeping in sync), so it stays called
        # every frame to preserve exact edge behavior.
        self._handle_media_chords(sc, sci, guide_now, now)

        # Left stick → arrow keys, right stick → mouse. In desktop mode both run
        # every frame. In gamepad mode the sticks are the analog sticks, so they
        # stay off the gameplay hot path  EXCEPT the right-stick mouse still
        # runs during a Steam/"..." hold (XInput is paused then), so Steam+right
        # stick moves the cursor just like the Steam+trackpad mouse latch.
        if self._gamepad is None:
            if self._lstick_mouse:  # left stick → cursor
                self._handle_mouse_lstick(sci, now)
            else:
                self._handle_arrow_stick(sci, guide_now, now)
            if self._rstick_mouse:  # right stick → cursor
                # Suppress cursor during a Steam/"..." hold when rstick guide zones
                # are bound (the guide rstick handler owns the stick then).
                if not (guide_now and self._guide_rstick_zones):
                    self._handle_mouse_stick(sci, now)
            elif self._rstick_actions:  # right stick → directional keys
                self._handle_arrow_rstick(sci, guide_now, now)
            # Desktop takeover (lizard off): WE drive the trackpads, pad clicks,
            # D-pad and triggers that the firmware used to. Right pad = cursor,
            # left pad = scroll, pad clicks = mouse buttons, D-pad = arrows.
            # Triggers + A/B are handled in the button block below.
            if self._takeover:
                self._handle_dpad_arrows(sci, guide_now, now)
                # Page-swipe observer runs on EVERY frame (it must see the
                # lift edge even when pinch/scrub own the pad  those touches
                # poison it via last frame's ownership flags, so they can
                # never fire a navigation).
                self._handle_page_swipe(sc, sci, now)
                # Virtual Menus: handled ONCE, up front in on_input, as a
                # full frame takeover (_vmenu_full_takeover)  a menu showing
                # never reaches this far down. _vm_owned stays here (rather
                # than stripping every "in _vm_owned" check below) so the
                # pinch/pad-mouse/scroll logic they guard is untouched; it is
                # simply always empty on any frame that gets this far.
                _vm_owned = ()
                _pinch_on = adusk_state.is_pinch_zoom_enabled()
                if not _pinch_on and self._zoom_scale != 1.0:
                    # Toggled off (the tray reset the magnifier itself) 
                    # forget the level so re-enabling starts from 1:1.
                    self._zoom_scale = 1.0
                # Engage on a HARD press of BOTH pads  but at PINCH_FORCE_FRAC
                # of each pad's learned click pressure (a lighter press than a
                # full click). Until a pad has been clicked once (force still 0)
                # fall back to its digital click bit.
                _l_hard = (sci.lpad_force >= self._lpad_click_force * self.PINCH_FORCE_FRAC
                           if self._lpad_click_force
                           else bool(sci.buttons & SCButtons.LPAD))
                _r_hard = (sci.rpad_force >= self._rpad_click_force * self.PINCH_FORCE_FRAC
                           if self._rpad_click_force
                           else bool(sci.buttons & SCButtons.RPAD))
                # Latch: the hard press only needs to happen ONCE. Pinch stays
                # engaged while both pads keep TOUCHING; it drops when a finger
                # lifts (or the toggle turns off), and re-arms on the next hard
                # press.
                _both_touch = bool((sci.buttons & SCButtons.LPADTOUCH)
                                   and (sci.buttons & SCButtons.RPADTOUCH))
                if not (_pinch_on and _both_touch):
                    self._pinch_latched = False
                elif _l_hard and _r_hard:
                    self._pinch_latched = True
                _pinch_active = self._pinch_latched
                self._handle_lpad_tap(sc, sci, now,
                                      _pinch_active or "lpad" in _vm_owned)
                if _pinch_active:
                    # Pinch To Zoom engages only while BOTH pads are pressed
                    # HARD  a physical CLICK on each (SCButtons.LPAD/RPAD), not
                    # a light two-finger rest  so it can never trigger by
                    # accident. It owns both pads while held: mouse/scroll/scrub
                    # are suppressed and their filters reset so releasing resumes
                    # them cleanly (no cursor jump / scroll lurch). Pad clicks
                    # are suppressed by the pinch guard in _handle_pad_clicks
                    # (both pads touched → no clicks) while Pinch is enabled.
                    self._handle_pinch_zoom(sci, now)
                    self._rpad_filt = None
                    self._tap_start = None   # pinch owns the pads  never a tap
                    self._lpad_tap_start = None
                    self._fling_active = False
                    self._rpad_vx = 0.0
                    self._rpad_vy = 0.0
                    self._lpad_prev = None
                    self._scroll_fling_v = 0.0
                    self._wheel_angle = None
                    self._wheel_acc = 0.0
                    self._textwheel_angle = None
                    self._textwheel_acc = 0.0
                    self._scrub_angle = None
                    self._scrub_acc = 0.0
                    self._scrub_stepped = False
                    self._abandon_hover_scrub()
                    self._lpad_scrub_latch = None
                    self._pan_filt = None       # pinch owns both pads  the
                    self._pan_fling_vx = 0.0    # zoomed pan reseeds when the
                    self._pan_fling_vy = 0.0    # right finger lifts
                    # (Pad-click suppression is handled by the pinch guard in
                    # _handle_pad_clicks  both-pads-touched → no clicks  which
                    # also covers the pre-latch hard press, so no lock is set
                    # here.)
                else:
                    self._end_pinch_zoom()
                    if "rpad" in _vm_owned:
                        # The menu owns the right pad  freeze the pad-mouse
                        # so releasing resumes it with no cursor jump.
                        self._rpad_filt = None
                        self._tap_start = None
                        self._fling_active = False
                        self._rpad_vx = 0.0
                        self._rpad_vy = 0.0
                    else:
                        self._handle_pad_mouse(sc, sci, now)
                    _scrub_mode = adusk_state.get_video_scrub_mode()
                    if _scrub_mode != "off" and self._hover_bar is None:
                        # Background bar probe: learns where the video sits
                        # while the mouse moves around a watch page, so the
                        # gate below has real evidence instead of guessing.
                        self._probe_video_region(now)
                    _lpad_now = bool(sci.buttons & SCButtons.LPADTOUCH)
                    if "lpad" in _vm_owned:
                        # The menu owns the left pad  kill scroll/scrub/dial
                        # + zoomed-pan state so releasing hands the pad back
                        # cleanly.
                        self._scroll_fling_v = 0.0
                        self._lpad_prev = None
                        self._wheel_angle = None
                        self._wheel_acc = 0.0
                        self._textwheel_angle = None
                        self._textwheel_acc = 0.0
                        self._scrub_angle = None
                        self._scrub_acc = 0.0
                        self._scrub_stepped = False
                        self._abandon_hover_scrub()
                        self._lpad_scrub_latch = None
                        self._pan_filt = None
                        self._pan_fling_vx = 0.0
                        self._pan_fling_vy = 0.0
                    elif _pinch_on and self._zoom_scale > 1.0:
                        # Zoomed in  even 1%  so the LEFT pad becomes the
                        # macbook-style 360° pan surface instead of the
                        # chosen scrolling mode (it outranks the video-scrub
                        # dial too: while magnified, moving the zoomed view
                        # is what the pad is for). Kill the scroll + scrub
                        # touch/coast state so zooming back out hands the
                        # pad back from a clean slate.
                        self._scroll_fling_v = 0.0
                        self._lpad_prev = None
                        self._wheel_angle = None
                        self._wheel_acc = 0.0
                        self._textwheel_angle = None
                        self._textwheel_acc = 0.0
                        self._scrub_angle = None
                        self._scrub_acc = 0.0
                        self._scrub_stepped = False
                        self._abandon_hover_scrub()
                        self._lpad_scrub_latch = None
                        self._handle_pad_pan(sci, now)
                    else:
                        if (self._pan_filt is not None or self._pan_fling_vx
                                or self._pan_fling_vy):
                            # Fully zoomed back out mid-touch/mid-coast:
                            # stop the pan dead and reseed the scrolling
                            # handlers fresh (their anchors are stale).
                            self._pan_filt = None
                            self._pan_fling_vx = 0.0
                            self._pan_fling_vy = 0.0
                        # Text Wheel Selection engages while a left-click
                        # control HOLDS the left mouse button (R2 actuated per
                        # the Mouse Trigger Actuation setting, or a right-pad
                        # click  gated on the 1-frame-stale CLICK flags, NOT
                        # the raw SCButtons.RT bit, which only reports a FULL
                        # pull and so never fired on a 35%-actuation hold) AND a
                        # finger is on the left pad. The drag stays live: the
                        # dial nudges the cursor and the app's own drag-
                        # selection extends character-snapped under it (see
                        # _handle_pad_text_wheel for why keyboard Shift+Arrow
                        # was abandoned).
                        if (adusk_state.is_text_wheel_selection_enabled()
                                and _lpad_now
                                and (self._rt_was_pressed
                                     or self._rpad_click_prev)):
                            # Outranks BOTH scrolling and the video-scrub dial 
                            # kill their touch/coast/latch state so nothing runs
                            # underneath it.
                            self._scroll_fling_v = 0.0
                            self._lpad_prev = None
                            self._wheel_angle = None
                            self._wheel_acc = 0.0
                            self._scrub_angle = None
                            self._scrub_acc = 0.0
                            self._scrub_stepped = False
                            self._abandon_hover_scrub()
                            self._lpad_scrub_latch = None
                            self._handle_pad_text_wheel(sc, sci, now)
                        else:
                            # Not selecting  drop the text-dial anchor so the
                            # next engage reseeds from a clean angle.
                            self._textwheel_angle = None
                            self._textwheel_acc = 0.0
                            if _lpad_now and self._lpad_scrub_latch is None:
                                # Scrub vs scroll is decided ONCE per touch (moving
                                # the cursor off the video mid-gesture doesn't yank
                                # the dial away): windowed, scrub only when the
                                # pointer sits over the video; anywhere else keeps
                                # the chosen scrolling.
                                self._lpad_scrub_latch = (
                                    _scrub_mode != "off" and self._video_focused(now)
                                    and self._cursor_on_video())
                            if (self._lpad_scrub_latch
                                    and _scrub_mode != "off"
                                    and self._video_focused(now)):
                                # Video Timeline Scrubbing: the left pad is the
                                # timeline dial while a video is focused. Kill any
                                # scroll coast + touch state so scrolling can't run
                                # under the video.
                                self._scroll_fling_v = 0.0
                                self._lpad_prev = None
                                self._wheel_angle = None
                                self._wheel_acc = 0.0
                                self._handle_pad_scrub(sc, sci, now, _scrub_mode)
                            else:
                                self._scrub_angle = None
                                self._scrub_acc = 0.0
                                # Focus left mid-scrub: drop the pending resume
                                # (blind-firing "K" at whatever window is focused
                                # now is worse than leaving the video paused), and
                                # abandon any hover scrub without clicking (no
                                # blind seek either).
                                self._scrub_stepped = False
                                self._abandon_hover_scrub()
                                self._handle_pad_scroll(sc, sci, now)
                    if not _lpad_now:
                        # Latch cleared AFTER the handlers ran, so the scrub
                        # handler still sees the lift frame and commits the
                        # seek.
                        self._lpad_scrub_latch = None
                self._handle_pad_clicks(sc, sci)
        else:
            # Gamepad mode: Steam/"..." + right stick moves the cursor and
            # L2/R2 click. The mouse-stick only runs during the hold (XInput is
            # paused then); the click handler runs EVERY frame so releasing
            # Steam or the trigger releases the injected mouse button.
            if guide_now:
                self._handle_mouse_stick(sci, now)
            self._handle_gamepad_mouse_clicks(sc, sci, guide_now)

        # Steam + Y → power off the controller. Skipped when Y has a Chords-tab
        # guide bind (the guide dispatcher handles it instead).
        y_now = bool(sci.buttons & SCButtons.Y)
        if int(SCButtons.Y) not in self._guide_bind_bits:
            if guide_now and y_now:
                if not self._powered_off:
                    self._powered_off = True
                    sc.turn_off()
            else:
                self._powered_off = False

        # Steam + B → force-shutdown the foreground game. Skipped when B has a
        # Chords-tab guide bind (the guide dispatcher handles it instead).
        b_now = bool(sci.buttons & SCButtons.B)
        if int(SCButtons.B) not in self._guide_bind_bits:
            if guide_now and b_now:
                if not self._force_kill_done:
                    self._force_kill_done = True
                    killed = _force_kill_foreground_game()
                    print(f"Steam+B force-kill game: pid={killed}")
            else:
                self._force_kill_done = False

        # Passive/desktop-mode button keys (skipped in gamepad mode, where
        # these are pad buttons, and when Steam/"..." is held so the button
        # feeds the guide chord instead). All edge-triggered = one press each.
        if self._gamepad is None:
            # Y alone → Space (Guide+Y stays the power-off chord above). Firmware
            # lizard is off in takeover, so this is Y's only desktop action.
            y_alone = y_now and not guide_now
            if y_alone and not self._y_alone_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_SPACE])
                self._chord.kb.releaseEvent([sui.Keys.KEY_SPACE])
            self._y_alone_was_pressed = y_alone

            # A alone → Enter (firmware desktop default; restored in takeover).
            a_alone = bool(sci.buttons & SCButtons.A) and not guide_now
            if a_alone and not self._a_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_ENTER])
                self._chord.kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._a_was_pressed = a_alone

            # B alone → Escape (Steam+B stays the force-kill chord above). No
            # OSK-open guard needed: while the OSK is open the _Watcher doesn't
            # drive the controller, and the Esc hook ignores injected keystrokes
            # anyway, so this injected Escape can never close the keyboard.
            b_alone = b_now and not guide_now
            if b_alone and not self._b_alone_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_ESC])
                self._chord.kb.releaseEvent([sui.Keys.KEY_ESC])
            self._b_alone_was_pressed = b_alone

            # R4 (right upper paddle) → Page Up.
            r4_now = bool(sci.buttons & SCButtons.RGRIP1) and not guide_now
            if r4_now and not self._r4_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_PAGEUP])
                self._chord.kb.releaseEvent([sui.Keys.KEY_PAGEUP])
            self._r4_was_pressed = r4_now

            # R5 (right lower paddle) → Page Down.
            r5_now = bool(sci.buttons & SCButtons.RGRIP2) and not guide_now
            if r5_now and not self._r5_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_PAGEDOWN])
                self._chord.kb.releaseEvent([sui.Keys.KEY_PAGEDOWN])
            self._r5_was_pressed = r5_now

            # L2 → RIGHT click, R2 → LEFT click (right trigger = primary, the
            # Steam-Deck convention; confirmed on hardware). Held via the chord
            # holders so a click can drag and survives a rebuild, plus the same
            # haptic "click" the OSK uses on the press edge. Actuation honours
            # its OWN "Mouse Trigger Actuation" setting (Options → Steam
            # Controller  separate from "Keyboard Trigger Actuation", which
            # only governs the OSK's Shift/Enter): the firmware full-pull
            # digital bit ALWAYS counts, and  unless the setting is "High"
            # (full-pull only)  an analog pull past the threshold also counts.
            _act_thr = adusk_state.get_sc_mouse_trigger_threshold()
            lt_now = self._trigger_click_now(
                self._lt_was_pressed, bool(sci.buttons & SCButtons.LT),
                sci.ltrig, _act_thr)
            if lt_now and not self._lt_was_pressed and adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_click()
            if self._takeover:
                self._chord.set_mouse_button("right", "lt", lt_now)
                if lt_now != self._lt_was_pressed:
                    self._mouse_shake_guard()
            self._lt_was_pressed = lt_now

            rt_now = self._trigger_click_now(
                self._rt_was_pressed, bool(sci.buttons & SCButtons.RT),
                sci.rtrig, _act_thr)
            if rt_now and not self._rt_was_pressed and adusk_state.is_rumble_enabled(self._kind):
                sc.haptic_click()
            if self._takeover:
                self._chord.set_mouse_button("left", "rt", rt_now)
                if rt_now != self._rt_was_pressed:
                    self._mouse_shake_guard()
            self._rt_was_pressed = rt_now

        # L4 (left upper paddle) → hold Left Shift; L5 (left lower paddle) →
        # hold the Windows key. Held modifiers (not taps), tracked on the
        # shared chord state so a rebuild mid-hold can't strand them. The
        # release branch runs in EVERY mode (gamepad too), so switching into a
        # game while a paddle is held still drops the modifier; only the
        # engage side is gated to desktop mode.
        l4_hold = (self._gamepad is None
                   and bool(sci.buttons & SCButtons.LGRIP1) and not guide_now)
        if l4_hold and not self._chord.shift_held:
            self._chord.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
            self._chord.shift_held = True
        elif not l4_hold and self._chord.shift_held:
            self._chord.release_shift()

        l5_hold = (self._gamepad is None
                   and bool(sci.buttons & SCButtons.LGRIP2) and not guide_now)
        if l5_hold and not self._chord.win_held:
            self._chord.kb.pressEvent([sui.Keys.KEY_LEFTMETA])
            self._chord.win_held = True
        elif not l5_hold and self._chord.win_held:
            self._chord.release_win()


# -- keyboard / mouse triggered Virtual Menus ---------------------------------
# Low-level hook constants + the small lookup tables the hook callbacks use.
# Kept module-level so the callbacks stay a dict lookup and a compare (a slow
# WH_*_LL callback is silently dropped by Windows, taking the input with it).
_WH_KEYBOARD_LL_ID = 13
_WH_MOUSE_LL_ID = 14
_WM_KEYDOWN_MSG = 0x0100
_WM_KEYUP_MSG = 0x0101
_WM_SYSKEYDOWN_MSG = 0x0104
_WM_SYSKEYUP_MSG = 0x0105
_LLKHF_INJECTED_FLAG = 0x10
_LLMHF_INJECTED_FLAG = 0x01
# Ask the hook thread to re-check which hooks should be installed.
_WM_APP_VMENU_SYNC = 0x0400 + 71
# WM_*BUTTON* -> (button name, is_press). "x" is resolved to x1/x2 from the
# event's mouseData high word.
_VMENU_MOUSE_MSGS = {
    0x0201: ("left", True), 0x0202: ("left", False),
    0x0204: ("right", True), 0x0205: ("right", False),
    0x0207: ("middle", True), 0x0208: ("middle", False),
    0x020B: ("x", True), 0x020C: ("x", False),
}
# Arrow keys move between boxes while a key-triggered menu is open.
_VMENU_ARROW_VK = {0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down"}
_VK_RETURN_KEY = 0x0D
_VK_SPACE_KEY = 0x20
_VK_ESCAPE_KEY = 0x1B
# Enter / Space stand in for the pad click while a key-triggered menu is open.
_VMENU_FIRE_VK = (_VK_RETURN_KEY, _VK_SPACE_KEY)


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _KBDLLHOOKSTRUCT2(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


def _cursor_point():
    """Current mouse position in physical screen pixels  the same space
    vmenu.py places its overlay in (both come from the DPI-aware Win32 API)."""
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


class _KeyVMenuRunner:
    """Keyboard / mouse triggered Virtual Menus  the SAME menus the trackpads
    drive, opened with no controller in the room.

    A menu whose "Show virtual menu while touching Key/Mouse" dropdown is set
    (its `key` field) shows its overlay while that key or mouse button is
    HELD, and is steered with the mouse and keyboard instead of a thumb:

      * moving the MOUSE highlights the box under the cursor, and a LEFT CLICK
        on the menu fires it. The overlay window is click-through, so the
        click is matched and SWALLOWED here rather than handled by the window
         it never reaches the game/app underneath.
      * the ARROW KEYS move between boxes (wrapping, see vmenu_neighbor) and
        ENTER (or Space) fires the highlighted one. Once an arrow is used the
        highlight stops chasing the cursor until the mouse actually moves
        again, so the two never fight.
      * ESCAPE closes without firing.
      * the TRIGGER key/button is never swallowed  holding "T" opens the
        menu and still types "t" like any unbound key would, unlike a
        controller BUTTON trigger (which masks its bit out of the frame,
        _vmenu_suppress_bits). Only the navigation keys above are eaten.
      * a menu's trigger can be TWO keys/buttons held together (the "+" second
        dropdown next to "Show virtual menu while touching Key/Mouse"  same
        chord idea as the controller trigger's optional 2nd button). The menu
        opens the moment BOTH are down (whichever is pressed second), and
        closes the moment EITHER is released  see _triggers_held. EXCEPT for
        the "toggle" Activation Style below, where release does nothing.

    The menu's Activation Style still decides WHEN the highlighted box fires,
    with the click/Enter press standing in for the pad click and Touch Release
    meaning "when the held trigger is let go"  with one exception: "toggle"
    changes what the TRIGGER itself does, the same way it does on the
    controller path (_Watcher._handle_virtual_menu). A fresh press opens the
    menu and self._open stays set through the release  the trigger no longer
    has to be held  and a second, fresh press of that same trigger (not the
    OS's key-repeat of a still-held one, see `was_held` in _on_key/_on_mouse)
    closes it again, without firing. The highlighted box still fires on a
    plain click/Enter meanwhile, same as "click".

    Threading  three parties:
      hook thread : installs WH_KEYBOARD_LL / WH_MOUSE_LL and pumps their
                    message loop. Its callbacks must return in microseconds
                    (Windows drops a slow low-level hook, and the input with
                    it), so they only read a snapshot dict, decide whether to
                    swallow, and queue an event.
      pump thread : owns the TouchMenuOverlay  a Win32 window belongs to the
                    thread that created it, so ALL rendering and every hot-bar
                    re-show happen here  and dispatches the fired actions.
      any thread  : publishes menus; the pump notices the adusk_state version
                    bump exactly like the controller path does.

    Hooks are installed lazily: the keyboard hook only while some menu has a
    key trigger, the mouse hook only while a mouse trigger is armed or a menu
    is actually open. A global LL mouse hook makes every mouse move round-trip
    through Python, so it stays down whenever nothing needs it."""

    def __init__(self, app):
        self._app = app
        self._q = deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._overlay = None            # pump-thread owned
        self._disp = None               # headless _Watcher action dispatcher
        self._ver = -1                  # adusk_state menus version last seen
        # Snapshots the HOOK thread reads. Never mutated in place  the pump
        # swaps in freshly built dicts, so a hook callback always sees one
        # consistent map. Each VK/button maps to a LIST of (menu, triggers)
        # records  `triggers` is a frozenset of one or two ("vk", vk) /
        # ("mouse", name) tuples (an optional 2nd key/mouse button forms a
        # CHORD with the first, same "[Button A] + [Button B]" idea as the
        # controller pad trigger's `pad2`  see _sync_menus). A menu with a
        # combo is indexed under BOTH halves so pressing either one (while the
        # other is already held) can complete it.
        self._armed_vk = {}             # Win32 VK -> [(menu, triggers), ...]
        self._armed_mouse = {}          # "left"/"middle"/"x1"/... -> [(menu, triggers), ...]
        # Hook-thread state.
        self._open = None              # {"triggers": frozenset, "m": menu}
        self._held_vk = set()           # every VK currently down (chord test)
        self._held_mouse = set()        # every mouse button currently down
        self._eaten = set()             # VKs whose key-DOWN we swallowed
        self._eaten_mouse = set()       # buttons whose press we swallowed
        self._hook_tid = 0
        # Set when a hook-state nudge couldn't be delivered (the hook thread
        # hadn't published its id yet); the pump retries it next tick, so an
        # armed menu can never end up with its hooks left uninstalled.
        self._sync_pending = False
        self._kb_hook = None
        self._mouse_hook = None
        self._kb_cb = None              # keep the CFUNCTYPEs alive
        self._mouse_cb = None
        # Pump-thread state: the live session, plus the overlay rect it last
        # drew at (published for the hook's click hit-test).
        self._live = None
        self._rect = None

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        # Hook thread first so it has published its id by the time the pump's
        # first menu sync wants to nudge it (a miss is retried, not lost).
        threading.Thread(target=self._hook_main, daemon=True,
                         name="vmenu-key-hook").start()
        threading.Thread(target=self._pump_main, daemon=True,
                         name="vmenu-key-pump").start()

    # -- hook thread --------------------------------------------------------
    def _hook_main(self):
        u32 = ctypes.windll.user32
        u32.CallNextHookEx.restype = ctypes.c_ssize_t
        u32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u32.SetWindowsHookExW.restype = wintypes.HHOOK
        u32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
        proto = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        def _kb_proc(n_code, w_param, l_param):
            try:
                if n_code >= 0:
                    kb = ctypes.cast(
                        l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT2)).contents
                    down = w_param in (_WM_KEYDOWN_MSG, _WM_SYSKEYDOWN_MSG)
                    up = w_param in (_WM_KEYUP_MSG, _WM_SYSKEYUP_MSG)
                    if (down or up) and self._on_key(
                            kb.vkCode, down,
                            bool(kb.flags & _LLKHF_INJECTED_FLAG)):
                        return 1
            except Exception as e:
                print(f"vmenu key hook error: {e!r}")
            return u32.CallNextHookEx(self._kb_hook, n_code, w_param, l_param)

        def _mouse_proc(n_code, w_param, l_param):
            try:
                if n_code >= 0:
                    ms = ctypes.cast(
                        l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    if self._on_mouse(
                            w_param, ms.pt.x, ms.pt.y, ms.mouseData,
                            bool(ms.flags & _LLMHF_INJECTED_FLAG)):
                        return 1
            except Exception as e:
                print(f"vmenu mouse hook error: {e!r}")
            return u32.CallNextHookEx(self._mouse_hook, n_code, w_param,
                                      l_param)

        self._kb_cb = proto(_kb_proc)
        self._mouse_cb = proto(_mouse_proc)
        self._hook_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self._reconcile_hooks()
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_APP_VMENU_SYNC:
                self._reconcile_hooks()
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _sync_hooks(self):
        """Nudge the hook thread to re-evaluate which hooks it needs. Safe from
        any thread (a hook can only be installed/removed by the thread whose
        message loop services it). A nudge that can't be delivered yet is
        remembered and retried by the pump rather than dropped."""
        tid = self._hook_tid
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    tid, _WM_APP_VMENU_SYNC, 0, 0)
                self._sync_pending = False
                return
            except Exception as e:
                print(f"vmenu hook sync failed: {e!r}")
        self._sync_pending = True

    def _reconcile_hooks(self):
        u32 = ctypes.windll.user32
        # `self._open` on the keyboard side is for a menu opened with NO key
        # trigger of its own (the tray submenu  see toggle_from_tray): nothing
        # armed it, so neither armed map is populated, and without this the
        # arrow keys / Enter / Escape that steer it would have no hook to
        # arrive through. An armed MOUSE trigger wants the keyboard for the
        # same reason: its menu is navigated by key even though it opens by
        # click.
        want_kb = (bool(self._armed_vk) or bool(self._armed_mouse)
                   or self._open is not None)
        want_mouse = bool(self._armed_mouse) or self._open is not None
        for want, attr, hid, cb in (
                (want_kb, "_kb_hook", _WH_KEYBOARD_LL_ID, self._kb_cb),
                (want_mouse, "_mouse_hook", _WH_MOUSE_LL_ID, self._mouse_cb)):
            have = getattr(self, attr)
            if want and not have:
                hk = u32.SetWindowsHookExW(hid, cb, None, 0)
                if not hk:
                    print("vmenu hook %d install failed err=%d"
                          % (hid, ctypes.get_last_error()))
                setattr(self, attr, hk or None)
            elif have and not want:
                try:
                    u32.UnhookWindowsHookEx(have)
                except Exception:
                    pass
                setattr(self, attr, None)

    @staticmethod
    def _blocked():
        """Don't hijack input while the config GUI is driving itself with the
        very same arrow keys, or behind the lock screen."""
        return sc_viewer.nav_claimed() or _workstation_locked()

    def _on_key(self, vk, down, injected):
        """Hook thread. True = swallow this key event.

        The TRIGGER key is never swallowed  holding "T" opens the menu AND
        still types "t" (held, it auto-repeats normally too), exactly like an
        unbound key would. Only the NAVIGATION keys (arrows/Enter/Escape) are
        eaten once a menu is open, since those exist purely to steer it.

        `injected` (LLKHF_INJECTED) gates those navigation keys: a menu's own
        Key Combo output types through the same backend we read here, so
        letting a synthetic arrow/Enter steer the menu would let a fired
        action drive the menu that fired it. The TRIGGER is honored either
        way  plenty of keyboards and mice deliver their macro and side
        buttons as injected input, and refusing those would make the trigger
        silently dead on that hardware."""
        op = self._open
        comp = ("vk", vk)
        if not down:
            swallow = vk in self._eaten
            self._eaten.discard(vk)
            self._held_vk.discard(vk)
            if op is not None:
                if comp in op["triggers"]:
                    # "toggle": releasing the trigger does nothing  the menu
                    # stays open until a fresh press of it (below) or Escape.
                    if op["m"].get("activate", "toggle") != "toggle":
                        self._end(fire=True)
                elif vk in _VMENU_FIRE_VK and not injected:
                    self._post(("press", False))
            return swallow
        was_held = vk in self._held_vk
        self._held_vk.add(vk)
        if op is None:
            recs = self._armed_vk.get(vk)
            if recs is None or self._blocked():
                return False
            # A combo's other half might already be held (from before this
            # key went down)  the FIRST fully-satisfied record wins; the
            # editor keeps a single vk/button from arming more than one menu
            # at once bar an intentional cross-menu key2 overlap, same
            # simplification the pad-combo editor makes for pad2.
            for m, triggers in recs:
                if self._triggers_held(triggers):
                    self._begin(triggers, m)
                    break
            return False                 # let the trigger itself go through
        if comp in op["triggers"]:
            # A FRESH press (not the OS's key-repeat of a key still held from
            # opening it) of a "toggle" menu's own trigger closes it  the
            # second half of the toggle, mirroring the controller path. Any
            # other style ignores this; its release above is what ends the
            # session.
            if op["m"].get("activate", "toggle") == "toggle" and not was_held:
                self._end(fire=False)
            return False                 # ...and its auto-repeat too
        if injected:
            return False
        d = _VMENU_ARROW_VK.get(vk)
        if d is not None:
            self._post(("nav", d))
            self._eaten.add(vk)
            return True
        if vk in _VMENU_FIRE_VK:
            self._post(("press", True))
            self._eaten.add(vk)
            return True
        if vk == _VK_ESCAPE_KEY:
            self._end(fire=False)
            self._eaten.add(vk)
            return True
        return False

    def _on_mouse(self, msg, x, y, data, injected):
        """Hook thread. True = swallow this mouse event. Only BUTTON events are
        looked at  the highlight follows the cursor by polling GetCursorPos on
        the pump tick instead. That keeps mouse MOTION out of Python entirely
        (a global LL hook sees every move, and a hundred round-trips a second
        through the GIL is a visible cost), and it also tracks moves that never
        reach a low-level hook at all, like a SetCursorPos warp."""
        hit = _VMENU_MOUSE_MSGS.get(msg)
        if hit is None:
            return False
        name, down = hit
        if name == "x":
            name = "x1" if ((data >> 16) & 0xFFFF) == 1 else "x2"
        comp = ("mouse", name)
        op = self._open
        if not down:
            swallow = name in self._eaten_mouse
            self._eaten_mouse.discard(name)
            self._held_mouse.discard(name)
            if op is not None:
                if comp in op["triggers"]:
                    # "toggle": releasing the trigger does nothing  the menu
                    # stays open until a fresh press of it (below) or Escape.
                    if op["m"].get("activate", "toggle") != "toggle":
                        self._end(fire=True)
                elif name == "left" and not injected:
                    self._post(("press", False))
            return swallow
        was_held = name in self._held_mouse
        self._held_mouse.add(name)
        if op is None:
            recs = self._armed_mouse.get(name)
            if recs is None or self._blocked():
                return False
            for m, triggers in recs:
                if self._triggers_held(triggers):
                    self._begin(triggers, m)
                    break
            return False                 # let the trigger button go through
        if comp in op["triggers"]:
            # A fresh press of a "toggle" menu's own trigger closes it  see
            # the matching branch in _on_key.
            if op["m"].get("activate", "toggle") == "toggle" and not was_held:
                self._end(fire=False)
            return False
        # Only a click that actually LANDS on the menu is claimed; clicking
        # away from it stays a normal click for whatever is underneath. An
        # injected click is a fired action's own output (see _on_key).
        if name == "left" and not injected and self._in_menu(x, y):
            self._post(("press", True))
            self._eaten_mouse.add(name)
            return True
        return False

    def _in_menu(self, x, y):
        r = self._rect
        return r is not None and r[0] <= x < r[0] + r[2] \
            and r[1] <= y < r[1] + r[3]

    def _triggers_held(self, triggers):
        """True while EVERY component of a trigger set is currently down 
        its own bit for a single-key trigger, AND the optional 2nd
        key/mouse-button too for a combo (see _sync_menus). The two hook
        callbacks keep _held_vk/_held_mouse current on every down/up edge, so
        this is a plain membership check, not a fresh poll."""
        for kind, val in triggers:
            if kind == "vk":
                if val not in self._held_vk:
                    return False
            elif val not in self._held_mouse:
                return False
        return True

    @staticmethod
    def _trig_key(triggers):
        """A stable string identity for a trigger set  used as the hot-bar
        per-trigger slot-memory key (see _open_menu), which used to be the
        plain "kind:id" a single trigger naturally formats as. Sorted so a
        2-button combo's key doesn't depend on press order."""
        return "+".join(sorted("%s:%s" % t for t in triggers))

    def _begin(self, triggers, m):
        self._open = {"triggers": triggers, "m": m}
        self._rect = None
        self._post(("open", m, triggers))
        self._sync_hooks()             # a keyboard trigger wants the mouse

    def _end(self, fire):
        self._open = None
        self._post(("close", fire))
        self._sync_hooks()

    # -- opened from the tray menu -------------------------------------------
    # The tray's "Virtual Menu" submenu lists every menu and toggles it here.
    # A tray-opened session is an ordinary one with a trigger set that no input
    # can ever match: ("tray", name) is neither ("vk", …) nor ("mouse", …), so
    # every `comp in op["triggers"]` test in the two hook callbacks is False
    # and the menu simply cannot be closed by releasing something. It stays up
    # until the same tray item is clicked again, or Escape, exactly like the
    # "toggle" activation style  which is the only behaviour that makes sense
    # for a menu opened from a click that is already over and done with.
    _TRAY_TRIGGER_KIND = "tray"

    @staticmethod
    def _tray_openable(m):
        """The same three conditions _sync_menus requires before it will arm a
        menu  enabled, has entries, a style the overlay can draw. Applied to
        the tray list too, so the submenu can never offer a menu that
        _open_menu would then silently refuse."""
        return bool(m.get("enabled", True)) and bool(m.get("entries")) \
            and m.get("type") in ("touch", "radial", "hotbar")

    def tray_menu_rows(self):
        """[(name, is_open), ...] for the tray submenu  every openable menu,
        in the user's own order, each flagged with whether it is the one
        currently on screen. Unlike _sync_menus this does NOT require a
        key/mouse trigger: a menu with no trigger at all is precisely the one
        that most needs a way in."""
        live = self._live
        open_name = None
        if live is not None:
            open_name = live["m"].get("name", "")
        rows = []
        for m in adusk_state.get_virtual_menus():
            if not self._tray_openable(m):
                continue
            name = str(m.get("name") or "").strip() or "Virtual Menu"
            rows.append((name, name == open_name))
        return rows

    def toggle_from_tray(self, name):
        """Show `name`'s overlay, or hide it if that same menu is already up.
        Clicking a DIFFERENT menu while one is open switches to it rather than
        stacking  there is one overlay window and one live session.

        Matched by name because that is what the user clicked; an index would
        silently point at the wrong menu if the list changed between the menu
        being built and the click landing."""
        op = self._open
        if op is not None and str(op["m"].get("name") or "").strip() == name:
            self._end(fire=False)
            return
        target = None
        for m in adusk_state.get_virtual_menus():
            if not self._tray_openable(m):
                continue
            if (str(m.get("name") or "").strip() or "Virtual Menu") == name:
                target = m
                break
        if target is None:
            return                      # deleted/renamed since the menu built
        if op is not None:
            self._end(fire=False)
        self._begin(frozenset({(self._TRAY_TRIGGER_KIND, name)}), target)

    def _post(self, cmd):
        self._q.append(cmd)
        self._wake.set()

    # -- pump thread --------------------------------------------------------
    def _pump_main(self):
        while not self._stop.is_set():
            try:
                self._sync_menus()
                if self._sync_pending:
                    self._sync_hooks()
                self._drain()
                self._tick()
            except Exception as e:
                print(f"vmenu key pump: {e!r}")
                self._live = None
            # A live menu ticks at ~33 Hz (the Continuous activation style
            # needs a heartbeat and the highlight follows the cursor); idle we
            # just wait for the hook, waking once a second to notice a
            # republished menu list.
            self._wake.wait(0.03 if self._live is not None else 1.0)
            self._wake.clear()

    @staticmethod
    def _resolve_trigger(kid):
        """A VMENU_KEY_TRIGGERS id -> ("vk", vk) or ("mouse", name); None for
        "none" or an id neither table recognises."""
        if not kid or kid == "none":
            return None
        btn = keybinds_runtime.VMENU_KEY_MOUSE.get(kid)
        if btn:
            return ("mouse", btn)
        vk = keybinds_runtime.VMENU_KEY_VK.get(kid)
        return ("vk", vk) if vk else None

    def _sync_menus(self):
        ver = adusk_state.get_virtual_menus_version()
        if ver == self._ver:
            return
        self._ver = ver
        by_vk, by_mouse = {}, {}
        for m in adusk_state.get_virtual_menus():
            if not m.get("enabled", True) or not m.get("entries"):
                continue
            if m.get("type") not in ("touch", "radial", "hotbar"):
                continue
            r1 = self._resolve_trigger(m.get("key"))
            if r1 is None:
                continue
            r2 = self._resolve_trigger(m.get("key2"))
            triggers = frozenset((r1,) if r2 is None else (r1, r2))
            rec = (m, triggers)
            # Indexed under BOTH halves of a combo (a lone entry for a plain
            # single-key trigger) so pressing EITHER one  whichever the user
            # happens to press second  can complete it.
            for kind, val in triggers:
                (by_vk if kind == "vk" else by_mouse).setdefault(
                    val, []).append(rec)
        # Whole-dict swap so a concurrent hook callback never sees a half-built
        # map (it only ever reads these two attributes).
        self._armed_vk, self._armed_mouse = by_vk, by_mouse
        if self._live is not None:
            self._close_menu(False)     # the edited menu may be gone entirely
        self._open = None
        self._sync_hooks()

    def _drain(self):
        while True:
            try:
                cmd = self._q.popleft()
            except IndexError:
                return
            op = cmd[0]
            if op == "open":
                self._open_menu(cmd[1], cmd[2])
            elif op == "close":
                self._close_menu(cmd[1])
            elif self._live is None:
                continue
            elif op == "nav":
                self._nav(cmd[1])
            elif op == "press":
                self._press(cmd[1])

    def _open_menu(self, m, src):
        entries = m.get("entries") or []
        if not entries:
            return
        style = m.get("type", "touch")
        trig = self._trig_key(src)
        # Same claim the controller path makes: a menu on screen owns the
        # input, so the config GUI stops navigating itself (a key-triggered
        # menu can still be steered by a pad the picker would otherwise read).
        sc_viewer.set_vmenu_open(True)
        self._live = {
            "m": m, "trig": trig, "style": style, "entries": entries,
            "hl": None, "pt": _cursor_point(), "kbd": False, "pressed": False,
            "last_fire": 0.0}
        if style == "hotbar":
            # Clicked-through: the highlight is the REMEMBERED slot (kept on
            # the dispatcher across opens), never a hit-test.
            self._live["hl"] = self._dispatcher()._vmenu_hotbar_idx.get(
                (trig, m.get("name", "")), 0) % len(entries)
        self._render()

    def _close_menu(self, fire):
        live = self._live
        self._live = None
        sc_viewer.set_vmenu_open(False)
        if self._overlay is not None:
            self._overlay.hide()
        self._rect = None
        if live is None or not fire or live["hl"] is None:
            return
        act = live["m"].get("activate", "toggle")
        # Touch Release fires on letting the trigger go; Release fires too when
        # the click/Enter was still down at that moment (same rule the pad path
        # applies when the trigger and the click end on one frame). The overlay
        # is already hidden, so a hot-bar advance must not re-show it. ("toggle"
        # never reaches here with fire=True  closing a toggle never fires on
        # its own, see _on_key/_on_mouse.)
        if act == "touch_release" or (act == "release" and live["pressed"]):
            self._fire(live, overlay=None)

    def _nav(self, direction):
        live = self._live
        live["kbd"] = True
        live["hl"] = keybinds_runtime.vmenu_neighbor(
            live["style"], len(live["entries"]), live["hl"], direction)
        if live["style"] == "hotbar":
            self._dispatcher()._vmenu_hotbar_idx[
                (live["trig"], live["m"].get("name", ""))] = live["hl"]
        self._render()

    def _press(self, down):
        """A click/Enter/Space confirming an entry. Mirrors the controller
        path's A button (see _vmenu_full_takeover): firing the highlighted
        entry this way is a CONFIRM, not just another way to click, so it
        always closes the menu afterward  regardless of Activation Style.
        (Continuous style has no confirm here; it fires on its own from
        _tick, and a click during it isn't wired to anything.)

        self._open (hook-thread state, tracking whether the trigger is still
        physically held) is deliberately left alone  it self-clears the
        normal way when the trigger is actually released (see _on_key/
        _on_mouse), so a menu closed by a confirm click while its trigger is
        still down simply can't be re-armed until that release happens, same
        as it couldn't before this fired."""
        live = self._live
        act = live["m"].get("activate", "toggle")
        fired = False
        if down and act in ("click", "toggle"):
            self._fire(live)
            fired = True
        elif not down and act == "release" and live["pressed"]:
            self._fire(live)
            fired = True
        live["pressed"] = down
        if fired:
            self._close_menu(fire=False)

    # How far (px) the mouse must physically move to take the highlight back
    # off the arrow keys  big enough to ignore sensor noise under a still
    # hand, small enough that a deliberate nudge is felt immediately.
    _MOUSE_RECLAIM_PX = 3

    def _tick(self):
        live = self._live
        if live is None:
            return
        pt = _cursor_point()
        if live["kbd"] and (abs(pt[0] - live["pt"][0]) > self._MOUSE_RECLAIM_PX
                            or abs(pt[1] - live["pt"][1])
                            > self._MOUSE_RECLAIM_PX):
            live["kbd"] = False         # the mouse takes the highlight back
        live["pt"] = pt
        if live["style"] != "hotbar" and not live["kbd"]:
            # No hysteresis here: a mouse doesn't jitter the way a resting
            # thumb does, and stickiness would just feel laggy.
            live["hl"] = self._hit_test(live)
        if live["m"].get("activate") == "continuous":
            now = time.monotonic()
            if now - live["last_fire"] >= _Watcher._VMENU_CONTINUOUS_S:
                live["last_fire"] = now
                self._fire(live)
        self._render()

    def _hit_test(self, live):
        r = self._rect
        pt = live["pt"]
        if r is None or pt is None or r[2] <= 0 or r[3] <= 0:
            return live["hl"]
        nx = (pt[0] - r[0]) / float(r[2])
        ny = (pt[1] - r[1]) / float(r[3])
        n = len(live["entries"])
        if live["style"] == "radial":
            return keybinds_runtime.vmenu_radial_at(n, nx, ny)
        return keybinds_runtime.vmenu_cell_at(n, nx, ny)

    def _render(self):
        live = self._live
        if live is None:
            return
        if self._overlay is None:
            self._overlay = vmenu.TouchMenuOverlay()
        # thumb=None: the real mouse cursor is already on screen, so the OSK
        # thumb dot the pad path draws would just be a second pointer.
        self._overlay.show(live["entries"], highlight=live["hl"],
                           style=live["style"], thumb=None,
                           **_Watcher._vmenu_overlay_opts(live["m"]))
        self._rect = self._overlay.geometry()

    def _fire(self, live, overlay=True):
        self._dispatcher()._vmenu_fire_entry(
            None, live["m"], live["trig"], live["style"], live["entries"],
            live["hl"], time.monotonic(), haptic=False,
            overlay=self._overlay if overlay else None)

    def _dispatcher(self):
        """A headless _Watcher used ONLY as this path's action dispatcher  it
        never sees a controller frame, so every fire runs with sc=None (see
        _fire_guide_action). It shares the App's _ChordState so anything it
        holds stays ref-counted against the controller paths, and it keeps its
        own hot-bar slot memory across opens.

        Xbox digital outputs from a Button Combo are the one thing that can't
        follow: those are pulsed into the virtual pad by the controller frame
        loop, which this watcher is not part of. Key/mouse/system outputs (the
        rest of the vocabulary) all fire normally."""
        if self._disp is None:
            app = self._app
            self._disp = _Watcher(
                lambda: False, chord=app._chord,
                on_profile_cycle=app.cycle_keybind_profile,
                on_toggle_gui=app.toggle_config_gui,
                on_show_keyboard=app.toggle_keyboard_hotkey)
        return self._disp


class _SdlDesktopController:
    """Turns a non-Steam SDL pad (Switch / Xbox / DualSense / Deck / handheld
    built-ins / ...) into a desktop mouse + keyboard with FULL Steam-Controller
    parity: every Desktop-tab control dispatches its bound action through the
    same vocabulary as the SC (keys, clicks, combos, system actions), the
    Chords tab drives the Guide(Home)-held layer (defaults reproduce the old
    hardcoded Home chords), the Hotkeys chords fire from any pad, and stick
    directions are rebindable. Driven from sdl_gamepad_thread while the pad
    isn't feeding a focused game.

    Defaults (empty binds): right stick = cursor, left stick + D-pad = arrows,
    ZR/ZL = left/right click, positional A = Enter / B = Esc / Y = Space,
    bumpers = browser tab switch, L3 = middle click; Home+L3 = Play/Pause,
    Home+left stick = volume/track, Home+"+" = Alt+Tab, Home+B = force-kill.
    The positional X (physical Y on a Switch) opens the OSK via `open_bits`
    (dispatched by the tray thread, which owns the open cooldown/lock gating).

    Only the plain STEAM bit is the guide here  QAM is the spare
    Capture/Mute/extra button (bindable), mirroring the SC's desktop-mode
    Steam/QAM split."""

    MOUSE_DEADZONE = 6000
    MOUSE_SPEED = 1400.0       # px/sec at full stick deflection
    MOUSE_EXPONENT = 1.6
    # Stick direction zones: deadzone + tap-then-repeat cadence (matches the
    # OSK's stick navigation feel). __init__ overrides the cadence from the OS
    # keyboard repeat rate so a held stick scrolls like the Steam Controller.
    ARROW_DEADZONE = 14000
    ARROW_HOLD_DELAY = 0.30
    ARROW_REPEAT = 0.04
    # Triggers (ZR/ZL) as mouse buttons for the GAMEPAD-mode Home-hold mouse
    # (update_mouse_only)  the desktop path is fully bind-driven instead.
    _CLICKS = ((SCButtons.RT, "left"), (SCButtons.LT, "right"))
    # Guide-held stick zones: deadzone + the volume-ramp cadence.
    MEDIA_DEADZONE = 14000
    MEDIA_HOLD_DELAY = 0.5
    MEDIA_VOL_REPEAT = 0.021
    # Max press→release time (s) for a Guide/Home TAP (mirrors the SC's).
    _GUIDE_TAP_S = 0.28

    def __init__(self, force_kill=None, binds=None, chords=None,
                 on_profile_cycle=None, trigger_haptic=None,
                 on_toggle_gui=None):
        self._mouse = sui.Mouse()
        self._kb = sui.Keyboard()
        # Callable that force-shutdowns the foreground game (guide chord).
        self._force_kill = force_kill
        # Callable buzzing the active pad on an L2/R2 actuation edge  the
        # same "the rumble is the click" feedback the SC watcher's desktop
        # trigger clicks give. The target (Sdl3GamepadSource.
        # haptic_trigger_click) gates per pad kind: only ANALOG-trigger
        # controllers buzz; the Switch's digital ZL/ZR click mechanically and
        # stay silent.
        self._trigger_haptic = trigger_haptic
        # Previous LT/RT bits for the actuation edge above.
        self._trig_prev = 0
        # App callback for the "<Mode> Profile Cycle" bound actions.
        self._on_profile_cycle = on_profile_cycle
        # App callback for the "Toggle Config GUI" bound action (default Guide
        # button, both modes)  opens/closes the picker + restores game focus.
        self._on_toggle_gui = on_toggle_gui
        self._last_t = 0.0
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._prev = 0
        # Held state: cid -> mouse button name / sui key currently held down by
        # a "click"/"hold" bind (released on button-up / guide / reset).
        self._down_clicks = {}
        self._down_keys = {}
        # cid -> controller kind, for the "Gyro To Mouse" holds inside
        # _down_keys: that action's ref-count is per kind and a release carries
        # none of its own (see _set_key).
        self._gyro_key_kinds = {}
        # Edge state: cid -> pressed, for tap/combo/system dispatch.
        self._ov_prev = {}
        # Guide layer state: bind-bit -> pressed, chord-index -> active,
        # guide-alone-chord fired latch, stick zones.
        self._guide_edge = {}
        self._guide_alone_fired = False
        self._guide_lzone = "NEUTRAL"
        self._guide_lzone_at = 0.0
        self._guide_rzone = "NEUTRAL"
        # Desktop stick-zone state (left / right), tap-then-repeat.
        self._lzone = "NEUTRAL"
        self._lzone_at = 0.0
        self._rzone = "NEUTRAL"
        self._rzone_at = 0.0
        # Hotkeys chords active flags (parallel to _chords_runtime).
        self._chord_was_active = []
        # ...and the same for the GAMEPAD-scoped half of that list, which fires
        # from sdl_gamepad_thread instead of update(). Index -> held.
        self._gp_chord_was = {}
        # Button-combo active flags (parallel to _button_combos); combos HOLD
        # their key outputs while the trigger is held.
        self._combo_was_active = []
        self._alt_held = False
        self._screen_off = False
        # Guide/Home TAP tracking (short press, no chord → tap action).
        # None = no frame seen since the last reset(): a Home button already
        # down on the first one was pressed while somebody else owned the pad
        # (the OSK), so its release must not read as a tap  see
        # _track_home_tap.
        self._guide_prev = None
        self._guide_press_t = 0.0
        self._guide_other = False
        # Set by a dispatched "show_keyboard" action; the tray thread services
        # it with its own OSK-open gating (cooldown / lock / mode).
        self.open_request = False
        # Match the OS keyboard auto-repeat, then slow to the Steam
        # Controller's measured scroll speed (see _os_key_repeat).
        self._arrow_hold_delay, self._arrow_repeat = self._os_key_repeat()
        self._arrow_repeat *= (20.0 / 14.0) / 0.7 * 1.1
        # Controller kind whose binds are currently loaded (set by apply_binds)
        #  read by _fire_action so a Profile Cycle action advances THIS pad's
        # own profile slots, not a shared/wrong controller's.
        self._active_kind = "switch"
        self.apply_binds(binds, chords, kind="switch")

    def apply_binds(self, kind_binds, chords=None, kind=None):
        """(Re)build every dispatch table from one controller kind's FULL saved
        binds ({"pc":...,"gamepad":...,"guide":...} or legacy flat) + its
        Hotkeys chord list. Safe to call live  the picker's Save path and the
        active-pad kind switch both call it. `kind` records which controller
        these binds belong to (see self._active_kind); omit to leave it
        unchanged (a same-kind re-apply, e.g. after a Save). Unset controls
        fall back to the SDL_*_DEFAULTS, so empty binds reproduce the built-in
        behavior."""
        if kind is not None:
            self._active_kind = kind
        pc = keybinds_runtime.pc_submap(kind_binds)
        guide = (kind_binds.get("guide") if isinstance(kind_binds, dict)
                 else None) or {}
        K = sui.Keys
        _SDL_IDS = keybinds_runtime.SDL_CHORD_BUTTONS
        # Per-kind fallback table: a pad whose hardware doesn't match the
        # shared two-stick assumption gets its own defaults (a lone Joy-Con's
        # single stick drives the cursor  see pads.desktop_defaults).
        _dd = pads.desktop_defaults(self._active_kind,
                                    keybinds_runtime.SDL_DESKTOP_DEFAULTS)
        self._btn_actions = keybinds_runtime.resolve_sdl_desktop(
            pc, SCButtons, K, _dd)
        # Bits that OPEN the OSK (default: positional X)  read by the tray
        # thread, which owns the open gating.
        self.open_bits = keybinds_runtime.resolve_sdl_open_bits(pc, SCButtons)
        # Bits that CLOSE the OSK (bound to Escape; B by default)  the tray
        # unions these into adusk's close set.
        self.close_bits = keybinds_runtime.resolve_sdl_close_buttons(
            pc, SCButtons)
        (self._lstick_mouse, self._lstick_zones,
         self._rstick_mouse, self._rstick_zones) = \
            keybinds_runtime.resolve_sdl_sticks(pc, K, _dd)
        self._home_tap = keybinds_runtime.resolve_sdl_home_tap(pc, K)
        # GAMEPAD-mode Home TAP (the Gamepad tab's Home binding, default "Toggle
        # Config GUI"). Fired by the gamepad loop's tap tracker while a virtual
        # pad is driven  tapping Home pops the config GUI; HOLD still runs the
        # gaming chords.
        self._home_tap_gp = keybinds_runtime.resolve_sdl_home_tap(
            keybinds_runtime.gamepad_submap(kind_binds), K,
            keybinds_runtime.SDL_GAMEPAD_DEFAULTS)
        self._guide_binds = keybinds_runtime.resolve_sdl_guide(
            guide, SCButtons, K)
        self._guide_lstick = keybinds_runtime.resolve_sdl_guide_lstick(guide, K)
        self._guide_rstick = keybinds_runtime.resolve_sdl_guide_rstick(guide, K)
        if chords is not None:
            self._chords_src = list(chords)
        ch = getattr(self, "_chords_src", [])
        self._chords_runtime = keybinds_runtime.build_chords(
            ch, SCButtons, K, _SDL_IDS)
        self._chord_was_active = [False] * len(self._chords_runtime)
        self._gp_chord_was = {}
        self._guide_chords = keybinds_runtime.build_guide_chords(
            ch, SCButtons, K, _SDL_IDS)
        # Read by the tray thread: gamepad-mode-toggle chord masks (evaluated
        # in BOTH modes) and Button-Combo entries.
        self.gamepad_toggle_masks = keybinds_runtime.build_gamepad_toggle_masks(
            ch, SCButtons, _SDL_IDS)
        # "Gyro To Mouse" hotkey masks (per-controller Options card)  also
        # evaluated by the tray thread in both modes.
        self.gyro_toggle_masks = keybinds_runtime.build_gyro_toggle_masks(
            ch, SCButtons, _SDL_IDS)
        self._button_combos = keybinds_runtime.build_button_combos(
            ch, SCButtons, K, _SDL_IDS)
        self._combo_was_active = [False] * len(self._button_combos)
        # Fresh tables → drop stale edges so nothing fires off old state.
        self._ov_prev.clear()
        self._guide_edge.clear()

    @staticmethod
    def _os_key_repeat():
        """(hold_delay, repeat_interval) in seconds from the Windows keyboard
        settings. SPI_GETKEYBOARDDELAY 0..3 -> 250..1000 ms; SPI_GETKEYBOARDSPEED
        0..31 -> ~2.5..30 repeats/sec. Falls back to the class defaults."""
        try:
            u = ctypes.windll.user32
            speed = ctypes.c_int(0)
            delay = ctypes.c_int(0)
            u.SystemParametersInfoW(0x000A, 0, ctypes.byref(speed), 0)  # GETKEYBOARDSPEED
            u.SystemParametersInfoW(0x0016, 0, ctypes.byref(delay), 0)  # GETKEYBOARDDELAY
            rps = 2.5 + (max(0, min(31, speed.value)) / 31.0) * (30.0 - 2.5)
            return (max(0, min(3, delay.value)) + 1) * 0.25, 1.0 / rps
        except Exception:
            return _SdlDesktopController.ARROW_HOLD_DELAY, _SdlDesktopController.ARROW_REPEAT

    def reset(self):
        """Release every held click/key and clear edge/accumulator state, so a
        handoff (OSK open, gamepad mode, pad unplug) never strands a button
        down or fires a stale edge."""
        for cid in list(self._down_clicks):
            self._set_click(cid, self._down_clicks[cid], False)
        for cid in list(self._down_keys):
            self._set_key(cid, self._down_keys[cid], False)
        self._release_combo_holds()
        if self._alt_held:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held = False
        self._prev = 0
        self._acc_x = self._acc_y = 0.0
        self._last_t = 0.0
        self._ov_prev.clear()
        self._guide_edge.clear()
        self._guide_alone_fired = False
        self._guide_lzone = self._guide_rzone = "NEUTRAL"
        self._lzone = self._rzone = "NEUTRAL"
        self._chord_was_active = [False] * len(self._chords_runtime)
        self._combo_was_active = [False] * len(self._button_combos)
        # None (not False): the next frame re-seeds the tap detector, so a Home
        # still held when we take the pad back  closing the OSK with Home
        # down, resuming from the Steam pause  can't produce a phantom
        # press+release and fire the tap action.
        self._guide_prev = None
        self._guide_other = False

    @staticmethod
    def _axis(v, deadzone, exponent):
        if abs(v) <= deadzone:
            return 0.0
        sign = 1.0 if v > 0 else -1.0
        mag = min(1.0, (abs(v) - deadzone) / (32767.0 - deadzone))
        return sign * (mag ** exponent)

    # -- held click / key primitives ------------------------------------------

    def _set_click(self, cid, name, pressed):
        """True held mouse button (drag-friendly) for a 'click' bind."""
        if pressed and cid not in self._down_clicks:
            self._mouse.press(name)
            self._down_clicks[cid] = name
        elif not pressed and cid in self._down_clicks:
            self._mouse.release(self._down_clicks.pop(cid))

    def _set_key(self, cid, key, pressed, kind=None):
        """True held key (modifier-friendly) for a 'hold' bind. `key` may be the
        "Gyro To Mouse" sentinel (keybinds_runtime.GYRO_MOUSE_KEY), which drives
        `kind`'s gyro per that controller's own Options gyro mode instead of
        pressing a key  see _ChordState.set_key for why it rides the hold
        contract. `kind` defaults to whichever kind's binds are loaded; the
        gamepad-mode per-pad path passes the FIRING pad's kind, which may not be
        that one (see _feed_one_sdl_pad)."""
        gyro = keybinds_runtime.GYRO_MOUSE_KEY
        if pressed and cid not in self._down_keys:
            if key == gyro:
                # Remember the kind on the holder: the release below arrives
                # without one, and the ref-count is per kind.
                self._gyro_key_kinds[cid] = kind or self._active_kind
                gyro_action_hold(self._gyro_key_kinds[cid], "sdl:" + cid, True)
            else:
                self._kb.pressEvent([key])
            self._down_keys[cid] = key
        elif not pressed and cid in self._down_keys:
            k = self._down_keys.pop(cid)
            if k == gyro:
                gyro_action_hold(self._gyro_key_kinds.pop(cid, None),
                                 "sdl:" + cid, False)
            else:
                self._kb.releaseEvent([k])

    def _tap(self, key):
        self._kb.pressEvent([key])
        self._kb.releaseEvent([key])

    # -- shared action dispatcher ----------------------------------------------

    def _fire_action(self, action, kind=None, mode="pc"):
        """Dispatch one edge-triggered action (one press = one fire). Same
        vocabulary as the SC watcher's _fire_guide_action; click/hold are
        momentary here (the button tables handle true holds separately).
        `kind` overrides self._active_kind for a Profile Cycle dispatch  used
        by the gamepad-mode per-pad key-override path, where the firing pad
        may not be the one whose binds are currently loaded into this
        controller (see _feed_one_sdl_pad). `mode` names which tab
        ("pc"/"gamepad"/"guide") this dispatch's binding lives in  read by
        the "profile_cycle" action so ONE dropdown entry cycles whichever mode
        was actually active when it fired."""
        typ = action[0]
        if typ == "tap":
            self._tap(action[1])
        elif typ == "combo":
            for k in action[1]:
                self._kb.pressEvent([k])
            for k in reversed(action[1]):
                self._kb.releaseEvent([k])
        elif typ == "click":
            self._mouse.press(action[1])
            self._mouse.release(action[1])
        elif typ == "hold":
            if action[1] == keybinds_runtime.GYRO_MOUSE_KEY:
                # Edge-only site (stick zone / Home tap): no release to end a
                # hold on, so the press flips the gyro (see gyro_action_flip).
                gyro_action_flip(kind or self._active_kind)
            else:
                self._tap(action[1])
        elif typ == "scroll":
            self._mouse.scroll(0, action[1])
        elif typ == "show_keyboard":
            self.open_request = True   # serviced by the tray thread's gating
        elif typ == "alt_tab":
            # Hold Alt across repeated presses so the switcher stays up; each
            # press taps Tab. Alt drops when the guide hold ends (see update).
            if not self._alt_held:
                self._kb.pressEvent([sui.Keys.KEY_LEFTALT])
                self._alt_held = True
            self._tap(sui.Keys.KEY_TAB)
        elif typ == "force_kill":
            if self._force_kill is not None:
                try:
                    self._force_kill()
                except Exception:
                    pass  # no console on the --windowed build
        elif typ == "xbutton":
            try:
                u = ctypes.windll.user32
                u.mouse_event(0x0080, 0, 0, action[1], 0)   # MOUSEEVENTF_XDOWN
                u.mouse_event(0x0100, 0, 0, action[1], 0)   # MOUSEEVENTF_XUP
            except Exception:
                pass
        elif typ == "toggle_magnifier":
            import subprocess
            _NO_WIN = subprocess.CREATE_NO_WINDOW
            try:
                r = subprocess.run(
                    ["tasklist", "/fi", "imagename eq Magnify.exe", "/fo",
                     "csv", "/nh"],
                    capture_output=True, text=True, timeout=2,
                    creationflags=_NO_WIN)
                if "Magnify.exe" in r.stdout:
                    subprocess.Popen(["taskkill", "/f", "/im", "Magnify.exe"],
                                     creationflags=_NO_WIN)
                else:
                    subprocess.Popen(["Magnify.exe"], creationflags=_NO_WIN)
            except Exception:
                pass
        elif typ in ("brightness_up", "brightness_down"):
            _brightness_request(10 if typ == "brightness_up" else -10)
        elif typ == "lock_pc":
            try:
                ctypes.windll.user32.LockWorkStation()
            except Exception:
                pass
        elif typ == "screen_off":
            self._screen_off = not self._screen_off
            try:
                if self._screen_off:
                    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
                else:
                    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, -1)
                    ctypes.windll.user32.mouse_event(0x0001, 0, 0, 0, 0)
            except Exception:
                pass
        elif typ == "sleep_pc":
            _sleep_now_async()
        elif typ == "shutdown_pc":
            _shutdown_now()
        elif typ == "profile_cycle":
            # Advance THIS controller's active profile slot for whichever tab
            # this binding's dispatch site is in (`mode`, passed by the
            # caller)  kind defaults to whichever kind's binds are currently
            # loaded (self._active_kind), overridable for the per-pad
            # gamepad-mode key-override path.
            if self._on_profile_cycle is not None:
                self._on_profile_cycle(kind or self._active_kind, mode)
        elif typ == "toggle_gui":
            # Open/close the config GUI (default Guide-button tap)  the App owns
            # the picker + game-focus restore.
            if self._on_toggle_gui is not None:
                self._on_toggle_gui()
        elif typ == "gamepad_mode_toggle":
            # Flip Desktop <-> Gamepad controls. Edge-triggered = one flip per
            # press; the same App call the Hotkeys toggle chords make.
            gamepad_mode_flip()
        elif typ == "big_picture":
            # Open Big Picture, or leave it if it's already up.
            big_picture_toggle_async()
        # power_off (SC firmware shutdown) has no SDL-pad equivalent → no-op.

    def _fire_chord_action(self, action):
        """Run a Hotkeys chord's action: key combo or launch a program."""
        try:
            if action["type"] == "keys":
                keys = action["keys"]
                for k in keys:
                    self._kb.pressEvent([k])
                for k in reversed(keys):
                    self._kb.releaseEvent([k])
            elif action["type"] == "launch":
                _launch_program(action["path"], action.get("args", ""))
        except Exception:
            pass

    def handle_gamepad_chords(self, sci):
        """Fire the GAMEPAD-scoped Hotkeys chords (the ones built from green
        "xi_" Gamepad-Layout aliases) and return their held OR-mask so the
        caller can keep those bits out of the virtual pad.

        update()  which owns the desktop-scoped half of the same list  is not
        called at all in gamepad mode, so without this the mode-scoped split
        that the Steam Controller's _Watcher._handle_chords implements had no
        SDL counterpart: an xi_ chord could be saved but never fired. Called
        once per poll from sdl_gamepad_thread's gamepad branch, off the merged
        frame, with its own rising-edge latch (self._gp_chord_was)."""
        suppress = 0
        for i, (mask, action, is_gamepad) in enumerate(self._chords_runtime):
            if not is_gamepad:
                self._gp_chord_was[i] = False
                continue
            active = (sci.buttons & mask) == mask
            if active:
                suppress |= mask
                if not self._gp_chord_was.get(i):
                    self._fire_chord_action(action)
            self._gp_chord_was[i] = active
        return suppress

    def _trigger_click_feedback(self, b):
        """Buzz on the rising edge of an L2/R2 actuation (the synthesized
        digital bit already honors the per-kind actuation slider). Fires on
        the raw pull regardless of what the trigger is bound to  it's
        actuation feel, not action feedback  matching the SC watcher."""
        trig = b & int(SCButtons.LT | SCButtons.RT)
        rising = trig & ~self._trig_prev
        self._trig_prev = trig
        if rising and self._trigger_haptic is not None:
            try:
                self._trigger_haptic()
            except Exception:
                pass

    # -- main per-frame update --------------------------------------------------

    def update(self, sci, now):
        b = sci.buttons
        self._trigger_click_feedback(b)
        dt = now - self._last_t if self._last_t else 0.0
        self._last_t = now
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0

        # Only the plain STEAM bit is the guide (QAM = the bindable
        # Capture/extra button, mirroring the SC's desktop-mode split).
        guide_held = bool(b & SCButtons.STEAM)

        # Guide/Home TAP (short press, no chord → the bound tap action).
        self._track_home_tap(b, now, guide_held)

        # Cursor: right stick when it's the mouse (suppressed while Guide is
        # held AND guide right-stick zones are bound); left stick too when the
        # user bound it to Joystick Mouse.
        _spd = self.MOUSE_SPEED * adusk_state.get_mouse_speed_for(
            adusk_state.get_active_controller())
        if self._rstick_mouse and not (guide_held and self._guide_rstick):
            self._acc_x += self._axis(sci.rstick_x, self.MOUSE_DEADZONE,
                                      self.MOUSE_EXPONENT) * _spd * dt
            self._acc_y += -self._axis(sci.rstick_y, self.MOUSE_DEADZONE,
                                       self.MOUSE_EXPONENT) * _spd * dt
        if self._lstick_mouse and not guide_held:
            self._acc_x += self._axis(sci.lstick_x, self.MOUSE_DEADZONE,
                                      self.MOUSE_EXPONENT) * _spd * dt
            self._acc_y += -self._axis(sci.lstick_y, self.MOUSE_DEADZONE,
                                       self.MOUSE_EXPONENT) * _spd * dt
        mvx, mvy = int(self._acc_x), int(self._acc_y)
        self._acc_x -= mvx
        self._acc_y -= mvy
        if mvx or mvy:
            self._mouse.move(mvx, mvy)

        if guide_held:
            # Guide layer owns the frame: release desktop holds so nothing
            # strands, run the bind-driven guide dispatch, skip desktop tables.
            self._release_desktop_state()
            self._handle_guide_layer_inner(sci, now, b)
            self._prev = b
            return

        # Guide released: drop Alt (Alt+Tab hold) + guide edges/zones.
        if self._alt_held:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held = False
        self._guide_edge.clear()
        self._guide_alone_fired = False
        self._guide_lzone = self._guide_rzone = "NEUTRAL"

        # Hotkeys chords (desktop-scoped) → suppress their buttons so A+B
        # doesn't also fire A's and B's own actions.
        suppress = 0
        for i, (mask, action, is_gamepad) in enumerate(self._chords_runtime):
            if is_gamepad:
                self._chord_was_active[i] = False
                continue
            active = (b & mask) == mask
            if active:
                suppress |= mask
                if not self._chord_was_active[i]:
                    self._fire_chord_action(action)
            self._chord_was_active[i] = active

        # Button Combos (desktop-scoped): HOLD the key outputs while the
        # trigger is held (Xbox outputs only matter in gamepad mode).
        for i, (mask, is_gamepad, _xbox, key_actions, guide) in enumerate(
                self._button_combos):
            if is_gamepad or guide:
                self._combo_was_active[i] = False
                continue
            active = (b & mask) == mask
            if active:
                suppress |= mask
                if not self._combo_was_active[i]:
                    for j, act in enumerate(key_actions):
                        self._combo_hold(i, j, act, True, mode="pc")
            elif self._combo_was_active[i]:
                for j, act in enumerate(key_actions):
                    self._combo_hold(i, j, act, False, mode="pc")
            self._combo_was_active[i] = active

        eff = b & ~suppress

        # Stick direction zones (bind-driven; skipped while that stick is the
        # mouse). Default left = arrows, right = mouse.
        if not self._lstick_mouse:
            self._lzone, self._lzone_at = self._stick_zone_dispatch(
                sci.lstick_x, sci.lstick_y, now, self._lstick_zones,
                self._lzone, self._lzone_at)
        if not self._rstick_mouse:
            self._rzone, self._rzone_at = self._stick_zone_dispatch(
                sci.rstick_x, sci.rstick_y, now, self._rstick_zones,
                self._rzone, self._rzone_at)

        # Digital controls: held click/hold semantics; everything else fires
        # once per press through the shared dispatcher.
        for cid, bit, action in self._btn_actions:
            pressed = bool(eff & bit)
            typ = action[0]
            if typ == "click":
                self._set_click(cid, action[1], pressed)
            elif typ == "hold":
                self._set_key(cid, action[1], pressed)
            else:
                if pressed and not self._ov_prev.get(cid, False):
                    self._fire_action(action, mode="pc")
                self._ov_prev[cid] = pressed

        self._prev = b

    def _release_desktop_state(self):
        """Treat every desktop bind as released (guide layer taking over)."""
        for cid in list(self._down_clicks):
            self._set_click(cid, self._down_clicks[cid], False)
        for cid in list(self._down_keys):
            self._set_key(cid, self._down_keys[cid], False)
        self._release_combo_holds()
        for cid in list(self._ov_prev):
            self._ov_prev[cid] = False
        self._chord_was_active = [False] * len(self._chords_runtime)
        self._lzone = self._rzone = "NEUTRAL"

    def _combo_hold(self, i, j, action, pressed, mode="pc"):
        """Hold/release one Button-Combo output (keyed 'cb:i:j'). `mode` names
        which tab this combo's trigger lives in (desktop-scoped combos are
        "pc"; the gamepad-scoped per-pad path passes "gamepad")."""
        cid = "cb:%d:%d" % (i, j)
        typ = action[0]
        if typ == "click":
            self._set_click(cid, action[1], pressed)
        elif typ in ("hold", "tap"):
            self._set_key(cid, action[1], pressed)
        elif pressed:
            self._fire_action(action, mode=mode)   # combos/system actions: fire on engage

    def _release_combo_holds(self):
        self._combo_was_active = [False] * len(getattr(self, "_button_combos", []))

    def _stick_zone_dispatch(self, x, y, now, zones, prev_zone, repeat_at):
        """One stick's direction-zone dispatch: fire the bound action on zone
        entry, then auto-repeat while held (arrow-key cadence). Returns the
        (zone, next_repeat_at) state."""
        zone = "NEUTRAL"
        if abs(x) > self.ARROW_DEADZONE or abs(y) > self.ARROW_DEADZONE:
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"
        fire = False
        if zone != prev_zone:
            fire = zone != "NEUTRAL"
            repeat_at = now + self._arrow_hold_delay
        elif zone != "NEUTRAL" and now >= repeat_at:
            fire = True
            repeat_at = now + self._arrow_repeat
        if fire:
            action = zones.get(zone)
            if action is not None and action[0] != "none":
                self._fire_action(action, mode="pc")
        return zone, repeat_at

    # -- guide (Home-held) layer -------------------------------------------------

    def handle_guide_layer(self, sci, now):
        """GAMEPAD-mode entry point: run the bind-driven guide dispatch for a
        frame whose Home/'...' is held (the caller pauses that pad's XInput)."""
        self._handle_guide_layer_inner(sci, now, sci.buttons)
        self._prev = sci.buttons

    def guide_release(self):
        """Called when the guide hold ends in gamepad mode: drop Alt + edges."""
        if self._alt_held:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held = False
        self._guide_edge.clear()
        self._guide_alone_fired = False
        self._guide_lzone = self._guide_rzone = "NEUTRAL"

    def _handle_guide_layer_inner(self, sci, now, b):
        # Digital guide binds (Chords tab)  rising edge per bit.
        for bit, action in self._guide_binds:
            pressed = bool(b & bit)
            key = "gb:%d" % bit
            if pressed and not self._guide_edge.get(key, False):
                self._fire_action(action, mode="guide")
            self._guide_edge[key] = pressed
        # Hotkeys chords with a Guide component: bit 0 = Guide alone (once per
        # hold); otherwise fire on the other button's rising edge.
        for i, (bit, action) in enumerate(self._guide_chords):
            if bit == 0:
                if not self._guide_alone_fired:
                    self._guide_alone_fired = True
                    self._fire_chord_action(action)
                continue
            pressed = bool(b & bit)
            key = "gc:%d:%d" % (i, bit)
            if pressed and not self._guide_edge.get(key, False):
                self._fire_chord_action(action)
            self._guide_edge[key] = pressed
        # Guide + left stick: bound zone actions (default volume/track), with
        # the volume ramp on UP/DOWN.
        if self._guide_lstick:
            x, y = sci.lstick_x, sci.lstick_y
            zone = "NEUTRAL"
            if abs(x) > self.MEDIA_DEADZONE or abs(y) > self.MEDIA_DEADZONE:
                if abs(y) >= abs(x):
                    zone = "UP" if y > 0 else "DOWN"
                else:
                    zone = "RIGHT" if x > 0 else "LEFT"
            fire = False
            if zone != self._guide_lzone:
                fire = zone != "NEUTRAL"
                self._guide_lzone_at = now + self.MEDIA_HOLD_DELAY
            elif zone in ("UP", "DOWN") and now >= self._guide_lzone_at:
                fire = True
                self._guide_lzone_at = now + self.MEDIA_VOL_REPEAT
            self._guide_lzone = zone
            if fire:
                action = self._guide_lstick.get(zone)
                if action is not None:
                    self._fire_action(action, mode="guide")
        # Guide + right stick zones (bound → fires on entry; overrides mouse).
        if self._guide_rstick:
            x, y = sci.rstick_x, sci.rstick_y
            zone = "NEUTRAL"
            if abs(x) > self.MEDIA_DEADZONE or abs(y) > self.MEDIA_DEADZONE:
                if abs(y) >= abs(x):
                    zone = "UP" if y > 0 else "DOWN"
                else:
                    zone = "RIGHT" if x > 0 else "LEFT"
            if zone != self._guide_rzone and zone != "NEUTRAL":
                action = self._guide_rstick.get(zone)
                if action is not None:
                    self._fire_action(action, mode="guide")
            self._guide_rzone = zone

    def _track_home_tap(self, b, now, guide_held, tap=None, mode="pc"):
        """Guide/Home TAP → bound action (mirror of the SC's Steam/QAM tap):
        fires only on a clean short press with NO other button during the hold,
        so the guide chords are untouched. `tap` selects the action (pc-tab tap
        in desktop mode, gamepad-tab tap while a virtual pad is driven  the
        Home button toggles the config GUI in both by default)."""
        if tap is None:
            tap = self._home_tap
        if self._guide_prev is None:
            # First frame since the last reset()  adopt an already-held Home
            # as an in-progress, already-chorded hold (see __init__/reset).
            self._guide_prev = guide_held
            self._guide_other = guide_held
            self._guide_press_t = now
            return
        if guide_held and not self._guide_prev:              # rising edge
            self._guide_press_t = now
            self._guide_other = False
        elif guide_held:
            if b & ~int(SCButtons.STEAM):
                self._guide_other = True                     # a chord  no tap
        elif self._guide_prev:                               # falling edge
            if ((now - self._guide_press_t) <= self._GUIDE_TAP_S
                    and not self._guide_other
                    and tap and tap[0] != "none"
                    # ...and not while the first-run tour is up: its chords all
                    # hold Guide, and the default tap action closes the config
                    # GUI, which would delete the tour mid-slide.
                    and not sc_viewer.tutorial_claimed()):
                self._fire_action(tap, mode=mode)
        self._guide_prev = guide_held

    def update_mouse_only(self, sci, now):
        """GAMEPAD-mode Home-hold: right stick = cursor, ZR/ZL = left/right
        mouse click  and nothing else (no arrow keys, no key taps). Mirrors the
        Steam Controller's Steam-hold mouse behavior in gamepad mode. The caller
        pauses the ViGEm pad while Home is held; call reset() when leaving this
        mode to release any still-held click."""
        b = sci.buttons
        self._trigger_click_feedback(b)
        dt = now - self._last_t if self._last_t else 0.0
        self._last_t = now
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0
        _spd = self.MOUSE_SPEED * adusk_state.get_mouse_speed_for(
            adusk_state.get_active_controller())
        self._acc_x += self._axis(sci.rstick_x, self.MOUSE_DEADZONE, self.MOUSE_EXPONENT) * _spd * dt
        self._acc_y += -self._axis(sci.rstick_y, self.MOUSE_DEADZONE, self.MOUSE_EXPONENT) * _spd * dt
        mvx, mvy = int(self._acc_x), int(self._acc_y)
        self._acc_x -= mvx
        self._acc_y -= mvy
        if mvx or mvy:
            self._mouse.move(mvx, mvy)
        # ZR/ZL -> left/right click (press/release for drag). NOT gated on Guide
        # here  the Home-hold is what activated this mouse mode.
        rising = b & ~self._prev
        falling = ~b & self._prev
        for bit, name in self._CLICKS:
            cid = "mo:%d" % bit
            if rising & bit:
                self._set_click(cid, name, True)
            elif (falling & bit) and cid in self._down_clicks:
                self._set_click(cid, name, False)
        self._prev = b


# --- App orchestration ------------------------------------------------------

# Steam Controller L2/R2 actuation levels → analog trigger threshold (0..32767;
# None = firmware full-pull digital bit only). "high" is the old full-pull-only
# "Default"; "default" is now a lighter ~35%-pull point and is the shipped
# program default (used for BOTH the OSK Shift/Enter functions AND the desktop
# takeover's L2/R2 mouse-click actuation). "low" is the lightest pull.
_SC_ACTUATION_THRESHOLDS = {"high": None, "default": 16728, "low": 3000}


# "Focus Pull Point" (Options -> Keyboard) as a 0-100 percentage -> the raw
# analog trigger reading (0..32767) at which the press-to-focus lock engages.
# 0% locks the moment the trigger moves, 100% only at a full pull; the shipped
# 50% is half a pull, which lands well before the click itself fires.
_OSK_FOCUS_PULL_MAX = 32767


def _osk_focus_pull_raw(pct):
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        pct = 50
    return int(round(max(0, min(100, pct)) / 100.0 * _OSK_FOCUS_PULL_MAX))


def _sc_actuation_threshold(val):
    """Trigger actuation threshold (analog 0..32767, or None for full-pull only)
    from either a named level ("high"/"default"/"low", the tray menu) OR a raw
    int threshold (the Options-tab gradual slider's in-between values). Higher =
    heavier pull required; both inputs share one continuous scale."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    return _SC_ACTUATION_THRESHOLDS.get(val, 16728)


def _sdl_actuation_threshold(val):
    """Per-kind SDL-pad trigger actuation → analog threshold, or None to keep
    the source's built-in engage point (inputsrc._TRIGGER_DIGITAL_ON)  so an
    untouched slider changes nothing vs the pre-per-kind behavior. Named levels
    reuse the SC scale; a raw slider int passes through."""
    if isinstance(val, bool) or val in (None, "default"):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    return _SC_ACTUATION_THRESHOLDS.get(val)

# Steam Controller "Pointer Speed" → right-stick mouse speed multiplier (1.0 =
# the tuned default). Scales the OSK right-stick mouse + the SC desktop mouse.
_SC_MOUSE_SPEEDS = {"low": 0.6, "medium": 1.0, "high": 1.6}


def _sc_speed_mult(val, default=1.0):
    """Right-stick pointer-speed multiplier from either a named level
    (low/medium/high, the tray menu) OR a raw float multiplier (the Options-tab
    'Right Joystick Sensitivity' slider's gradual in-between values). Floats are
    anchored to the same Low/Medium/High endpoints by the picker, so the named
    levels and the slider share one continuous scale."""
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    return _SC_MOUSE_SPEEDS.get(val, default)
# SC desktop-takeover trackpad multipliers (tray "Steam Controller" submenu).
# Multiply the base sensitivity in _Watcher._handle_pad_mouse / _handle_pad_scroll.
# Calibrated 2026-06-21: tuned so a full right-pad swipe ≈5.5 of cursor travel (the
# ×1.30 "30% faster" run measured 7.2, scaled back ×5.5/7.2 to 5.5). Original
# baseline 0.6/1.0/1.7 = 10.8 swipe. The FLING is now DECOUPLED from this via
# PAD_FLING_GAIN (fling speed = tracking lift × that), so the throw runs faster than
# the cursor  tracking speed and fling distance tune independently.
_SC_TRACKPAD_SPEEDS = {"low": 0.3035, "medium": 0.5058, "high": 0.8597}
_SC_SCROLL_SPEEDS = {"low": 0.33, "medium": 0.55, "high": 0.99}


def _sc_trackpad_mult(val):
    """Right-pad cursor multiplier from either a named level (low/medium/high,
    the tray menu) OR a raw float multiplier (the Options-tab 'Right Trackpad
    Sensitivity' slider's gradual linear in-between values)  the same
    dual-typed scheme as _sc_scroll_mult, anchored to _SC_TRACKPAD_SPEEDS."""
    if isinstance(val, bool):
        return _SC_TRACKPAD_SPEEDS["medium"]
    if isinstance(val, (int, float)):
        return float(val)
    return _SC_TRACKPAD_SPEEDS.get(val, _SC_TRACKPAD_SPEEDS["medium"])


def _sc_scroll_mult(val):
    """Left-pad scroll multiplier from either a named level (low/medium/high,
    the tray menu) OR a raw float multiplier (the Options-tab Touchpads
    'Scrolling Sensitivity' slider's gradual in-between values)  the same
    dual-typed scheme as _sc_speed_mult, anchored to _SC_SCROLL_SPEEDS."""
    if isinstance(val, bool):
        return _SC_SCROLL_SPEEDS["medium"]
    if isinstance(val, (int, float)):
        return float(val)
    return _SC_SCROLL_SPEEDS.get(val, _SC_SCROLL_SPEEDS["medium"])


class _ScExtra:
    """One EXTRA (player 2+) Steam Controller / Deck: the reader thread, the HID
    session it opened, and the dedicated ViGEm pad it feeds.

    Owned by sc_extra_thread (the only writer of App._sc_extras); `sc`, `path`
    and `pad` are written by this extra's own worker thread and only ever read
    by the supervisor, so a plain attribute assignment is enough under the GIL."""

    __slots__ = ("sc", "path", "claim", "kind", "pad", "thread", "exclusive",
                 "opening", "zeroed", "last_rumble")

    def __init__(self, exclusive, claim=()):
        self.sc = None
        self.kind = None
        # HID path, filled in once the reader has probed and opened a device.
        self.path = None
        # Every path this reader might still land on, held from BEFORE the
        # thread starts until the probe settles. Player 1's reader is excluded
        # from all of them for that window: it rebuilds constantly, and if it
        # opened the interface we were mid-probe on, both readers would end up
        # streaming the same physical controller (hidapi allows two shared
        # handles on one device) and one pad would shadow the other.
        self.claim = frozenset(claim)
        self.pad = None
        self.thread = None
        # block_sc_hid as it stood when this reader opened; the supervisor
        # recycles the reader when the toggle no longer matches.
        self.exclusive = exclusive
        self.opening = True
        # True while the pad has been zeroed for an inactive gamepad mode, so
        # we push the empty report once instead of on every 250 Hz frame.
        self.zeroed = False
        # Last (large, small) game rumble forwarded, so an unchanged force-
        # feedback update doesn't re-write the HID haptic reports.
        self.last_rumble = (None, None)

class App:
    def __init__(self):
        # No settings.json yet == first-ever launch. Checked before
        # _load_settings() creates any in-memory defaults, so a fresh install is
        # distinguished from "user deleted settings.json" the same way (both are
        # treated as first-run, which is fine  the goal is just "open the GUI
        # manager once so a new user isn't dropped into a bare tray icon").
        self._is_first_run = _settings_read_path() is None
        self.settings = _load_settings()
        # Keybind-profile migration: settings saved before profiles existed
        # have only the flat "keybinds"  seed slot 1 with it so the picker's
        # profile buttons start from the user's current layout. Also clamp the
        # active slot to 1-4.
        _profs = self.settings.get("keybind_profiles")
        if not isinstance(_profs, dict) or not _profs:
            self.settings["keybind_profiles"] = {
                "1": self.settings.get("keybinds", {}) or {}}
        # keybind_profile is PER (CONTROLLER, TAB): {kind: {"pc","gamepad",
        # "guide"}} each naming a slot 1-4, so every controller  and within it
        # Desktop / Gamepad / Chords  can sit on a different profile,
        # completely independent of every other controller. Migrate the two
        # older shapes: a bare int (ancient, every tab of every controller
        # shared one slot) or a flat {mode: slot} dict (pre-multi-controller,
        # every CONTROLLER shared one slot per tab)  both seed every catalog
        # kind with the same starting slot so nobody loses their current setup;
        # controllers then diverge independently going forward.
        _kp = self.settings.get("keybind_profile", 1)
        if isinstance(_kp, dict) and any(isinstance(v, dict)
                                         for v in _kp.values()):
            _kp_nested = _kp
        else:
            if isinstance(_kp, dict):
                _flat = _kp
            else:
                try:
                    _slot = min(_MAX_KEYBIND_PROFILES, max(1, int(_kp)))
                except (TypeError, ValueError):
                    _slot = 1
                _flat = {m: _slot for m in ("pc", "gamepad", "guide")}
            _kp_nested = {k: dict(_flat) for k in pads.KINDS}
        _clean = {}
        for _k in pads.KINDS:
            _clean[_k] = {}
            _kmap = _kp_nested.get(_k) or {}
            for _m in ("pc", "gamepad", "guide"):
                try:
                    _clean[_k][_m] = min(_MAX_KEYBIND_PROFILES,
                                         max(1, int(_kmap.get(_m, 1))))
                except (TypeError, ValueError):
                    _clean[_k][_m] = 1
        self.settings["keybind_profile"] = _clean
        # Profile-slot count is PER (CONTROLLER, TAB) too: {kind: {"pc",
        # "gamepad","guide"}} each count 1-_MAX_KEYBIND_PROFILES. Fresh installs
        # start at 1 each,
        # but never hide a slot an existing (kind, mode) already uses  infer
        # each one's floor from the highest slot that's either populated with
        # THAT (kind, mode)'s binds or selected by it. Migrate the same two
        # legacy shapes (bare int / flat {mode: count}) as above.
        _kpc = self.settings.get("keybind_profile_count", 1)
        if isinstance(_kpc, dict) and any(isinstance(v, dict)
                                          for v in _kpc.values()):
            _kpc_nested = _kpc
        else:
            if isinstance(_kpc, dict):
                _flat = _kpc
            else:
                try:
                    _base = min(_MAX_KEYBIND_PROFILES, max(1, int(_kpc)))
                except (TypeError, ValueError):
                    _base = 1
                _flat = {m: _base for m in ("pc", "gamepad", "guide")}
            _kpc_nested = {k: dict(_flat) for k in pads.KINDS}
        _profs_all = self.settings.get("keybind_profiles") or {}
        _counts = {}
        for _k in pads.KINDS:
            _counts[_k] = {}
            _kmap = _kpc_nested.get(_k) or {}
            for _m in ("pc", "gamepad", "guide"):
                try:
                    _c = min(_MAX_KEYBIND_PROFILES,
                             max(1, int(_kmap.get(_m, 1))))
                except (TypeError, ValueError):
                    _c = 1
                _used = [_c, _clean.get(_k, {}).get(_m, 1)]
                for _slot, _smap in _profs_all.items():
                    try:
                        _si = int(_slot)
                    except (TypeError, ValueError):
                        continue
                    if ((_smap or {}).get(_k) or {}).get(_m):
                        _used.append(_si)
                _counts[_k][_m] = min(_MAX_KEYBIND_PROFILES,
                                      max(1, max(_used)))
        self.settings["keybind_profile_count"] = _counts
        # Profile NAMES are per (CONTROLLER, TAB, SLOT)  {kind: {mode: {slot:
        # "name"}}}. Cosmetic and always sparse (an unnamed slot simply has no
        # entry), so there's no legacy shape to migrate: just normalise what's
        # on disk to that nesting, drop anything that isn't a string or names a
        # slot past the ceiling, and let the rest default to empty.
        _names_in = self.settings.get("keybind_profile_names")
        _names = {}
        for _k in pads.KINDS:
            _kmap = (_names_in or {}).get(_k)
            if not isinstance(_kmap, dict):
                continue
            for _m in ("pc", "gamepad", "guide"):
                _smap = _kmap.get(_m)
                if not isinstance(_smap, dict):
                    continue
                for _s, _nm in _smap.items():
                    if not isinstance(_nm, str) or not _nm.strip():
                        continue
                    try:
                        _si = int(_s)
                    except (TypeError, ValueError):
                        continue
                    if not 1 <= _si <= _MAX_KEYBIND_PROFILES:
                        continue
                    _names.setdefault(_k, {}).setdefault(_m, {})[str(_si)] = (
                        _nm.strip()[:_MAX_PROFILE_NAME])
        self.settings["keybind_profile_names"] = _names
        # Compose the live keybinds mirror from the per-tab slots.
        self.settings["keybinds"] = self._compose_keybinds()
        # Push the current startup setting into the registry so the on-disk
        # state matches the user's saved preference.
        _apply_autostart(self.settings["start_with_windows"])
        # Publish the per-controller haptics switches to the shared runtime flags
        # all haptic paths (UI ticks + gamepad/desktop rumble) read.
        adusk_state.set_rumble_enabled("sc", self.settings["rumble_enabled_sc"])
        adusk_state.set_rumble_enabled("sdl", self.settings["rumble_enabled_switch"])
        adusk_state.set_pinch_zoom(self.settings.get("pinch_zoom", False))
        adusk_state.set_pinch_sensitivity(
            self.settings.get("pinch_sensitivity", 0.7))
        adusk_state.set_swipe_pages(self.settings.get("swipe_pages", False))
        adusk_state.set_swipe_right_output(
            self.settings.get("swipe_right_output", "page_prev"))
        adusk_state.set_swipe_left_output(
            self.settings.get("swipe_left_output", "page_next"))
        adusk_state.set_tap_to_click(self.settings.get("tap_to_click", False))
        adusk_state.set_tap_to_click_left(
            self.settings.get("tap_to_click_left", False))
        _rtt, _tt, _st = typing_mode_flags(self.settings.get("typing_mode"))
        adusk_state.set_release_to_type(_rtt)
        adusk_state.set_touch_typing(_tt)
        adusk_state.set_swipe_typing(_st)
        adusk_state.set_osk_layout(self.settings.get("osk_layout", "classic"))
        # Merged-in OSK features (Options -> Keyboard). All read live by the
        # OSK, so publishing them here is enough  no reopen needed.
        adusk_state.set_split_layout(
            self.settings.get("osk_split_layout", False))
        adusk_screen.set_display_scaling(
            self.settings.get("osk_scale_display", False))
        adusk_state.set_diacritics_enabled(
            self.settings.get("osk_diacritics", True))
        adusk_state.set_diacritic_locale(
            self.settings.get("osk_diacritic_locale", "auto"))
        adusk_state.set_hit_expand(self.settings.get("osk_hit_assist", 10))
        adusk_state.set_press_focus(self.settings.get("osk_press_focus", True))
        adusk_state.set_trigger_focus_pull(
            _osk_focus_pull_raw(self.settings.get("osk_focus_pull", 50)))
        # Steam's OSK audio: register the players, then the on/off switch.
        adusk_state.set_key_sound(adusk_key_sound.play_key_sound)
        adusk_state.set_key_sound_open(adusk_key_sound.play_open_sound)
        adusk_state.set_key_sound_close(adusk_key_sound.play_close_sound)
        adusk_state.set_key_sound_enabled(
            self.settings.get("osk_key_sound", True))
        # Per-app OSK memory: publish the saved maps, then the switch.
        adusk_state.set_per_app_maps(
            position=self.settings.get("osk_pos_per_app") or {},
            size=self.settings.get("osk_size_per_app") or {},
            skin=self.settings.get("osk_skin_per_app") or {})
        adusk_state.set_per_app_memory(self.settings.get("osk_per_app", False))
        # Normalize + publish the selected OSK skin so screen.Screen picks it up
        # the next time the keyboard opens. Fall back to the default if the
        # saved name no longer matches a bundled skin.
        if self.settings.get("skin") not in adusk_skins.available_skins():
            self.settings["skin"] = adusk_skins.DEFAULT_SKIN
        adusk_skins.set_active_skin(self.settings["skin"])
        # Publish the OSK transparency (continuous 0..1 fraction) so screen.Screen
        # renders it.
        adusk_skins.set_transparency_fraction(self._osk_transparency_fraction())
        # Publish the OSK window size so screen.Screen() builds the cached
        # window (below) at the right dimensions.
        adusk_screen.set_osk_size(self.settings.get("osk_size", "medium"))
        # True once a size change is saved while the OSK is open  the cached
        # Screen can't be rebuilt while adusk.main() is using it, so
        # launcher_thread rebuilds it right after that run finishes.
        self._pending_size_change = False
        # Publish global keyboard LStick & Mouse toggle + per-controller OSK
        # settings so controller.py applies them on the input thread.
        adusk_state.set_kbd_stick_nav(self.settings.get("kbd_lstick_mouse", True))
        adusk_state.set_kbd_mouse_nav(self.settings.get("kbd_rstick_mouse", True))
        adusk_state.set_kbd_gyro_always(self.settings.get("kbd_gyro_always", False))
        self._publish_kbd_gyro_config()
        adusk_state.set_osk_buttons(self.settings.get("osk_buttons"))
        adusk_state.set_lpad_click_button(self.settings.get("lpad_click_button", "l2"))
        adusk_state.set_rpad_click_button(self.settings.get("rpad_click_button", "r2"))
        adusk_state.set_sc_osk_trigger_threshold(
            _sc_actuation_threshold(self.settings.get("sc_osk_trigger_actuation", "default")))
        adusk_state.set_sc_mouse_trigger_threshold(
            _sc_actuation_threshold(self.settings.get("sc_mouse_trigger_actuation", "default")))
        adusk_state.set_sc_gamepad_trigger_threshold(
            _sc_actuation_threshold(self.settings.get("sc_gamepad_trigger_actuation", "default")))
        adusk_state.set_sc_mouse_speed(
            _sc_speed_mult(self.settings.get("sc_pointer_speed", "medium")))
        adusk_state.set_switch_mouse_speed(
            _SC_MOUSE_SPEEDS.get(self.settings.get("switch_pointer_speed", "medium"), 1.0))
        adusk_state.set_sc_trackpad_speed(
            _sc_trackpad_mult(self.settings.get("sc_trackpad_speed", "medium")))
        adusk_state.set_sc_scroll_speed(
            _sc_scroll_mult(self.settings.get("sc_scroll_speed", "medium")))
        adusk_state.set_sc_scroll_mode(self.settings.get("sc_scroll_mode", "normal"))
        adusk_state.set_sc_scroll_invert(self.settings.get("sc_scroll_invert", False))
        adusk_state.set_text_wheel_selection(
            self.settings.get("text_wheel_selection", False))
        adusk_state.set_video_scrub_mode(self.settings.get("video_scrub", "off"))
        adusk_state.set_virtual_menus(keybinds_runtime.vmenus_sanitize(
            self.settings.get("virtual_menus")))
        # Sync "Block SteamInput Xbox Controller grab" to the user env var so a
        # Steam started this session honors it  and a stale entry from a previous
        # run with it ON is cleared when it's now off. See _set_xbox_ignore.
        _set_xbox_ignore(self.settings.get("block_gamepad_takeover", False))
        # Per-kind controller settings (each unlocked controller's Options
        # category): haptics, pointer speed, OSK button maps and SDL trigger
        # actuation for every catalog kind. The SC/Switch keys were already
        # seeded above; this covers the rest (missing settings = defaults).
        osk_by_kind = self.settings.get("osk_buttons_by_kind") or {}
        for _kind in pads.KINDS:
            if _kind != "sc":
                adusk_state.set_rumble_enabled(_kind, self.settings.get(
                    pads.setting_key(_kind, "rumble_enabled"), True))
                adusk_state.set_kind_mouse_speed(_kind, _sc_speed_mult(
                    self.settings.get(pads.setting_key(_kind, "pointer_speed"),
                                      "medium")))
                for _which, _base in (("osk", "osk_trigger_actuation"),
                                      ("mouse", "mouse_trigger_actuation"),
                                      ("gamepad", "gamepad_trigger_actuation")):
                    adusk_state.set_sdl_trigger_threshold(
                        _kind, _which, _sdl_actuation_threshold(
                            self.settings.get(pads.setting_key(_kind, _base))))
            # Every kind gets an explicit OSK map so no kind silently inherits
            # another's customization; the SC's comes from the legacy flat key.
            if _kind == "sc":
                adusk_state.set_osk_buttons_for(
                    "sc", osk_by_kind.get("sc") or self.settings.get("osk_buttons"))
            else:
                _kmap = dict(pads.osk_default_buttons(_kind))
                _kmap.update(osk_by_kind.get(_kind) or {})
                adusk_state.set_osk_buttons_for(_kind, _kmap)
        # Publish every gyro-capable kind's "Gyro To Mouse" hotkey masks so
        # the OSK can evaluate the toggle chord while IT owns the controllers
        # (re-published on every chords save  see _save_keybinds), plus the
        # cog-modal gyro tuning + each kind's mode-seeded on/off state.
        self._publish_gyro_masks()
        self._publish_gyro_config()
        # Cache for the sdl thread's per-poll "new controller kind?" test.
        self._seen_kind_cache = {pads.canon(k) for k in
                                 (self.settings.get("seen_controllers") or ())}
        self._seen_kind_cache.discard(None)
        # A handheld PC's built-in pad IS a controller  unlock its category
        # right away (it may present as a plain XInput device only later).
        _mk = pads.machine_kind()
        if _mk:
            self._note_seen_controller(_mk, save=False)
        # Seed the OSK's Shift/Enter glyph set from the last-used controller so
        # the right hints (SC L2/R2 vs Switch ZL/ZR vs Xbox LT/RT ...) show on
        # the very first open after launch, before any input. Then register a
        # hook so a live controller switch is saved back to disk and survives a
        # reboot.
        saved_ctrl = pads.canon(self.settings.get("last_osk_controller", "sc")) or "sc"
        adusk_state.init_active_controller(saved_ctrl)
        adusk_state.set_active_controller_persist(self._persist_active_controller)

        self._stop_event = threading.Event()
        # Set when Steam is running AND the user opted into pausing for Steam.
        self._steam_active = threading.Event()
        # Wake events so the background threads can BLOCK (zero polling) while
        # their feature is inactive instead of waking on a timer. A tray-menu
        # toggle (or shutdown) sets the relevant event to wake the thread.
        self._auto_gamepad_wake = threading.Event()
        self._steam_watch_wake = threading.Event()
        # Set by _kick_sc() so launcher_thread can tell a deliberate kick (mode
        # toggle / auto focus change) from an unexpected device drop: a kick
        # should rebuild immediately, while a drop keeps the reconnect backoff.
        # Without this, the 1s backoff also delayed the post-switch mode chime.
        self._intentional_kick = threading.Event()
        self._current_sc = None
        # Which HID controller family the takeover runtime is driving this
        # iteration ("sc" / "steam_deck"), and the sticky preference (the
        # family that last actually opened). See launcher_thread's selection.
        self._hid_kind = "sc"
        self._hid_prefer = None
        # Win+Ctrl+O hotkey support: _open_kbd_event asks launcher_thread to
        # open the on-screen keyboard (so people without a controller can try
        # it); _launcher_wake wakes the launcher out of its reconnect backoff
        # so the request is honored promptly even with no controller attached;
        # _kbd_open tracks whether adusk_app.main() is currently running.
        self._open_kbd_event = threading.Event()
        self._launcher_wake = threading.Event()
        self._kbd_open = False
        # Desired-state flag for the Options-tab live OSK preview: True while the
        # user is pressing a Size/Transparency slider in the picker (the keyboard
        # is shown so they can see the effect; closed on release). Drives an
        # animation-free open in launcher_thread; only ever closes the OSK the
        # preview itself opened (never one the user opened via controller/hotkey).
        self._osk_preview = False
        # Same but for the Menu/≡ "Enter Value" typing open (see _open_osk_typing).
        self._osk_typing = False
        # Window to restore focus to after an SDL-pad / hotkey OSK open (the
        # Steam Controller path uses the watcher's own capture instead).
        self._pending_restore_hwnd = None
        # Controller family ("sdl"/None) that requested the pending OSK open via
        # toggle_keyboard_hotkey, so the launcher can start the OSK on that
        # controller's glyphs (a Steam Controller Steam+X open is detected
        # separately via watcher.triggered). None = a non-controller open (tray
        # menu / Win+Ctrl+O)  leave the glyphs on the last-used controller.
        self._pending_open_controller = None
        self._osk_hotkey_hook_state = None
        # Set by auto_gamepad_thread to the PID of the detected game while
        # auto gamepad mode has it latched on; None when no game is active.
        self._auto_gamepad_pid = None
        # True iff the latched game (or one of its descendants) currently
        # owns the foreground window. Gates whether we push XInput frames:
        # game in focus → gamepad active; game backgrounded → lizard mode so
        # the controller works as mouse/kb on the desktop / in Discord / etc.
        self._auto_gamepad_focused = False
        # Long-lived ViGEm virtual pad. Kept alive while either gamepad_mode
        # or auto_gamepad_mode is on, so games enumerate it at *their* startup
        # rather than missing it if we create it after the game has launched.
        # Lifecycle is owned by launcher_thread (single-writer).
        self._persistent_gamepad = None
        # Automatic multiplayer: one dedicated ViGEm pad per ADDITIONAL SDL
        # controller, keyed by SDL instance id (the FIRST controller reuses
        # _persistent_gamepad as player 1, so a lone pad never spawns a phantom
        # 2nd device). Owned by sdl_gamepad_thread (single-writer); empty unless
        # 2+ controllers are live in gamepad mode.
        self._sdl_gamepads = {}
        # SDL instance id of the pad currently reusing _persistent_gamepad as
        # player 1 (None when a Steam Controller owns it, or no SDL pad is live).
        self._primary_sdl_jid = None
        # --- Wireless dropout grace (see _park_sdl_gamepad) -------------------
        # A wireless pad that vanishes is usually BACK in a few seconds - most
        # of all a Nintendo pad, whose firmware drops the Bluetooth link every
        # ~20 minutes on any non-Switch host (nintendo_bt.py). Tearing its
        # virtual gamepad down and building a new one on reconnect is what
        # makes games lose the controller for good, so instead we PARK it:
        # {pad_uid: (VirtualGamepad, expiry)} for pads whose controller is gone
        # but whose device is still open and zeroed, ready to be handed back to
        # the same physical controller.
        self._sdl_parked = {}
        # Same idea for the persistent pad (player 1): (uid, expiry) reserving
        # that slot for the controller that just dropped, so a second pad can't
        # take it in the gap and the returning one re-inherits it.
        self._primary_hold = None
        # One-shot per session: whether the "your Nintendo pad dropped, here's
        # why" notice has been shown.
        self._nintendo_drop_notified = False
        # Multi-Steam-Controller: the EXTRA (player 2+) Steam Controllers / Decks
        # that the launcher's single reader did not claim  one _ScExtra each,
        # keyed by an id() handle so a record exists before its HID path is
        # known. Owned by sc_extra_thread (single-writer); empty with one
        # controller attached, which is the overwhelmingly common case.
        self._sc_extras = {}
        # Snapshot of the HID paths those extras hold, as a frozenset, passed to
        # the PRIMARY SteamController as exclude_paths so its constant rebuilds
        # (mode change, OSK toggle, keybind save) can never open a device
        # another player already owns. Rebuilt on every extras-dict change.
        self._sc_extra_paths = frozenset()
        # Wakes sc_extra_thread out of its poll sleep (shutdown, Steam pause
        # change, block_sc_hid toggle) so extras react as fast as player 1.
        self._sc_extra_wake = threading.Event()
        # time.monotonic() before which no new extra reader is spawned  set
        # after a probe found nothing responsive among the free interfaces (an
        # unpaired dongle slot is the normal case, and probing costs 1.5 s each).
        self._sc_extra_retry_at = 0.0
        # Per-kind gamepad-mode remap cache for extra controllers (see
        # _sc_extra_conf). Cleared wherever _sdl_gp_cache is.
        self._sc_extra_gp_cache = {}
        # True while sdl_gamepad_thread is holding a process high-responsiveness
        # request (adusk_power) because an SDL pad is live and may be driving the
        # desktop mouse. Tracked so the request/release stays balanced as pads
        # connect / disconnect / get ceded for Steam. See _set_sdl_hi_res.
        self._sdl_hi_res = False
        # SDL3 gamepad backend for non-Steam pads (Xbox/DualSense/Switch/...).
        # The tray owns a persistent SDL_INIT_GAMEPAD (the OSK borrows it via
        # SDL_InitSubSystem so it survives keyboard open/close). sdl_gamepad_thread
        # polls _sdl_source to open the OSK (Guide+X) and feed ViGEm. Stays None
        # if SDL init fails  the Steam Controller path is wholly unaffected.
        self._sdl_source = None
        # The live _SdlDesktopController (set when sdl_gamepad_thread starts), so
        # the Keybinds picker's Save can re-apply Switch binds without a reconnect.
        self._sdl_desktop = None
        # Per-kind gamepad-mode remap cache for SDL pads ({kind: conf}  see
        # _sdl_gp_conf) + per-pad Button-Combo / gamepad-key edge state.
        # Cache cleared on every keybinds save / profile switch.
        self._sdl_gp_cache = {}
        self._sdl_combo_state = {}
        self._sdl_gpk_state = {}
        # Per-pad advanced-press engines ({jid: (conf, engine)}  rebuilt when
        # the kind's conf cache entry is replaced) + their per-slot edge state,
        # and the per-pad gyro→right-stick deflection for this tick ({jid:
        # (dx, dy)}  written by the SDL loop's gyro block, consumed by
        # _feed_one_sdl_pad's pad.update).
        self._sdl_adv_engines = {}
        self._sdl_adv_prev = {}
        self._sdl_gyro_stick = {}
        # DESKTOP-mode Advanced Presses for SDL pads: {kind: engine | False}
        # (False = that kind has no pc adv rows) + the active slot state.
        # Cache cleared wherever _sdl_gp_cache is (keybinds save / profile
        # switch) so edits apply live.
        self._sdl_pc_adv = {}
        self._sdl_pc_adv_prev = {}
        # SCButtons bits that close the OSK for SDL pads (bound to Escape; B by
        # default). Unioned with the SC's set in the launcher publish.
        self._sdl_close_bits = keybinds_runtime.resolve_sdl_close_buttons(
            {}, SCButtons)
        # Process gamepad input even when no SDL window is focused  the OSK
        # window is NOACTIVATE, and without this SDL drops all pad events while
        # it's open (every SDL pad reads all-zero). Set before the GAMEPAD init.
        try:
            S.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
            # Keep SDL's HIDAPI driver off BOTH Steam Controllers. We drive
            # them entirely through our own steamcontroller HID backend (never
            # as SDL gamepads), but SDL3 (unlike SDL2) recognizes the Triton
            # PIDs 0x1304/0x1302  and this same hint covers the 2015 unit's
            # 0x1102/0x1142, which SDL2 already knew  and, on GAMEPAD init,
            # opens a *shared* handle on the
            # device. That shared handle makes our exclusive CreateFileW
            # (dwShareMode=0) fail with ERROR_SHARING_VIOLATION, silently
            # breaking "Block SteamInput Steam Controller grab" (block_sc_hid)
            #  it would just fall back to shared and do nothing. Disabling the
            # Steam HIDAPI driver leaves the device free for our exclusive open
            # while keeping SDL's other pad drivers (Xbox/Switch/PlayStation).
            S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"0")
            # Same for the Steam Deck's built-in pad (PID 0x1205): we drive it
            # through our own steamcontroller HID backend at full trackpad
            # parity  SDL's hidapi steamdeck driver would otherwise hold the
            # device, fight our lizard-mode control (it feeds the deck's
            # lizard watchdog its own way) and double-drive input.
            S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAMDECK", b"0")
            # The OSK window is WS_EX_NOACTIVATE; without this, SDL eats clicks
            # on it (treats them as focus-gaining) so mouse keys never type.
            # Set before SDL_Init so it's in effect for the cached OSK window.
            S.SDL_SetHint(b"SDL_MOUSE_FOCUS_CLICKTHROUGH", b"1")
            self._apply_nintendo_sdl_hints()
        except Exception:
            pass
        try:
            if S.SDL_Init(S.SDL_INIT_GAMEPAD | S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS):
                self._sdl_source = adusk_inputsrc.Sdl3GamepadSource()
                # Nintendo Bluetooth guard on/off before the first pad opens,
                # so a pad connected at startup is classified correctly.
                self._sdl_source.set_bt_safe(
                    self.settings.get("nintendo_bt_safe", True))
                self._sdl_source.set_joycon_stick_rotate(
                    self.settings.get("joycon_stick_rotate", False))
                S.TTF_Init()
            else:
                print(f"SDL init failed: {S.get_error()}")
        except Exception as e:
            print(f"SDL backend unavailable: {e!r}")
        # Hand the source to adusk so its main loop can poll the pad on the SDL
        # event-pump thread while the OSK is open (see state._sdl_source).
        adusk_state.set_sdl_source(self._sdl_source)
        # The OSK Screen (SDL window + renderer + 6 TTF fonts) is reused across
        # opens to keep open latency near-zero, but it is built LAZILY on
        # launcher_thread (_ensure_cached_screen), NOT here. launcher_thread is
        # the thread adusk.main() renders on, and an SDL window/renderer created
        # on a DIFFERENT thread than the one it's presented on makes every
        # SDL_RenderPresent a cross-thread call that blocks ~70ms/frame (the loop
        # falls to ~13Hz) and routes the window's mouse messages through the wrong
        # thread's pump, so clicks get dropped  the OSK "doesn't respond to the
        # mouse", worst on the first open at a freshly-built size.
        self._cached_screen = None
        # True while launcher_thread wants real XInput output (gamepad mode on,
        # or auto-mode game focused); gates SDL->ViGEm feeding in the SDL thread.
        self._gamepad_active = False
        # Latch for the Hotkeys "Gamepad Mode Toggle" chord. Lives on the App (not
        # the _Watcher) so it survives the watcher rebuild the toggle triggers:
        # set when the chord fires, cleared when the chord is fully released, so
        # holding the chord through the mode switch can't re-fire and ping-pong.
        self._gp_toggle_latched = False
        # Built-in "hold ≡ (Start/Menu) to switch Desktop <-> Gamepad" gesture.
        # One detector per input path (the HID watcher and the SDL pad loop run
        # concurrently and see different frames), both living HERE rather than
        # on the watcher so the hold state survives the watcher rebuild the
        # mode switch kicks off  a fresh detector would restart its timer while
        # the button is still down and ping-pong the mode.
        self._mode_hold_hid = keybinds_runtime.ModeHoldGesture()
        self._mode_hold_sdl = keybinds_runtime.ModeHoldGesture()
        # "Gyro To Mouse" runtime state lives in adusk_state (the shared
        # gyro_mouse_kinds set) so the tray paths AND the OSK (which owns the
        # controllers while open, and evaluates the same hotkey there) stay in
        # sync. Session-only  every controller starts with gyro-mouse off.
        # Press/release edges from BOTH sources  the modal's hotkey chords
        # and buttons bound to the "Gyro To Mouse" action  are ref-counted per
        # kind by gyro_action_hold, which is also where the once-per-press
        # "toggle" latching now lives.
        # Let the module-level bindable action flip through the SAME helper the
        # modal's hotkey chords use, so a flip from either source gets the
        # haptic tick and the console line.
        global _gyro_flip_hook, _gamepad_mode_flip_hook
        _gyro_flip_hook = self._toggle_gyro_mouse
        # ...and the same for the bindable "Toggle Gamepad Mode" action, which
        # flips exactly what the Hotkeys toggle chords flip.
        _gamepad_mode_flip_hook = self._do_toggle_gamepad_mode
        # Runtime control-scheme override set by the "Hotkey Gamepad/Desktop
        # Toggle" chord. None = follow the ViGEm Bus Driver setting; True = force
        # gamepad controls; False = force desktop controls. It flips ONLY the live
        # control scheme (gamepad_active)  it never enables/disables the ViGEm
        # driver or tears down the virtual pad (vg_should_live stays driven by the
        # setting). Cleared whenever the gamepad-mode setting is changed in the UI.
        self._mode_override = None
        # Chord state shared across every _Watcher rebuild so an in-progress
        # Steam+VIEW=Alt+Tab doesn't lose track of held keys when sc.run()
        # is kicked mid-chord (e.g. by auto-gamepad-detect on focus change).
        self._chord = _ChordState()
        # Last (large, small) rumble we forwarded to the controller, so the
        # ViGEm force-feedback callback only writes when it actually changes.
        self._last_rumble = (None, None)
        # Last seen gamepad<->lizard state (the real "gamepad active" flag, not
        # just the selected mode). The on/off chime fires on every transition:
        # menu toggles AND auto-mode game focus changes both flip it. None until
        # launcher_thread seeds it on its first loop, so startup is silent.
        self._chime_prev_active = None
        # Battery status (see battery_thread). _battery is the last
        # SteamControllerBattery polled from the live SteamController, or None
        # until one streams a power report. _battery_label is the cached menu /
        # tooltip text. _low_warned_at is the lowest low-battery band we've
        # already toasted this discharge cycle, so each band warns once; it
        # resets ONLY when a charger is next connected  never on a % reading
        # drifting back up on its own  so a pack idling near a band can't spam.
        # _charge_complete_notified latches the "fully charged" toast.
        # _was_charging tracks the (debounced) charge state across polls so we
        # toast the charger-connected / charger-disconnected edges.
        # _batt_charge_seen / _batt_charging / _batt_charge_complete debounce the
        # charge-state byte: a stray/transient charge frame must agree on two
        # consecutive polls before we act on it, so a flickering charger can't
        # spam toasts or churn the tray menu.
        self._battery = None
        self._battery_label = None
        self._low_warned_at = None
        self._charge_complete_notified = False
        self._batt_charge_seen = None
        self._batt_charge_pending_frame = None
        self._batt_charging = False
        self._batt_charge_complete = False
        # Latched True once a Steam Controller is ever detected this session, so
        # the "Steam Controller" tray menu stays visible the whole session (see
        # is_sc_connected). Set by battery_thread and is_sc_connected.
        self._sc_ever_connected = False
        # Same latch for a Nintendo Switch Pro / SDL pad  set in
        # sdl_gamepad_thread when a pad frame is read; gates the "Nintendo Switch
        # Controller" tray submenu. See is_switch_connected.
        self._switch_ever_connected = False
        self._was_charging = False
        # The last _menu_state() the tray popup was built for, so the
        # state-driven items (Game Mode's tick, the Virtual Menu submenu) are
        # only rebuilt when they'd actually differ. None = never built, so the
        # first tick always syncs.
        self._tray_menu_state = None
        # Options → Big Picture controller-connect automation (big_picture.py,
        # an Auto-Big-Picture port): opens/closes Steam Big Picture on
        # joystick connect/disconnect edges. Started from setup().
        self._bp_engine = big_picture.BigPictureEngine(
            settings=self.settings, notify=self._notify,
            # Paused for Steam = controllers ceded, so joystick presence says
            # nothing; ignore edges across the pause or closing Steam looks
            # like a fresh connect and re-launches Big Picture (which restarts
            # Steam, which pauses us again).
            controller_paused=self._steam_active.is_set)

    # tray menu state predicates --------------------------------------------

    def is_start_with_windows_checked(self, item):
        return self.settings["start_with_windows"]

    def is_disable_while_steam_checked(self, item):
        return self.settings["disable_while_steam_running"]

    def is_exit_on_steam_checked(self, item):
        return self.settings["exit_on_steam_launch"]

    def is_gamepad_mode_checked(self, item):
        return self.settings["gamepad_mode"]

    def is_auto_gamepad_mode_checked(self, item):
        return self.settings["auto_gamepad_mode"]

    def is_gamepad_off_checked(self, item):
        # "Off" reflects the absence of every gamepad state (always / auto /
        # manual)  i.e. no virtual pad at all.
        return (not self.settings["gamepad_mode"]
                and not self.settings["auto_gamepad_mode"]
                and not self.settings.get("gamepad_manual", False))

    def is_sc_rumble_checked(self, item):
        return self.settings["rumble_enabled_sc"]

    def is_switch_rumble_checked(self, item):
        return self.settings["rumble_enabled_switch"]

    def is_block_sc_hid_checked(self, item):
        return self.settings["block_sc_hid"]

    def is_block_gamepad_takeover_checked(self, item):
        return self.settings["block_gamepad_takeover"]

    def is_debug_unlocked(self, item):
        """Visibility callback for the hidden Debug submenu."""
        return self.settings["debug_menu_unlocked"]

    def toggle_debug_menu(self, icon, item):
        self.settings["debug_menu_unlocked"] = not item.checked
        _save_settings(self.settings)

    def _persist_active_controller(self, kind):
        """Save the controller kind ("sc"/"switch"/"xbox"/"ps5"/...) last used
        on the OSK so its Shift/Enter glyphs persist across restarts. Called by
        adusk.state only when the active controller actually changes (on the
        input thread), so writes are rare. No menu refresh  this is invisible
        to the tray UI; it only affects which glyphs the keyboard draws."""
        kind = pads.canon(kind)
        if kind is None or self.settings.get("last_osk_controller") == kind:
            return
        self.settings["last_osk_controller"] = kind
        _save_settings_if_exists(self.settings)

    def _note_seen_controller(self, kind, save=True):
        """Record that a controller kind has been detected  permanently
        unlocking its tab in the picker's top bar and its Options category.
        Idempotent and cheap when already seen. A picker built before this
        kind existed is stale: tear it down (hidden rebuilds are invisible;
        a visible picker rebuilds on its next open)."""
        kind = pads.canon(kind)
        if kind is None or kind == "sc":
            return
        cache = getattr(self, "_seen_kind_cache", None)
        if cache is not None and kind in cache:
            return
        seen = [k for k in (self.settings.get("seen_controllers") or ())
                if pads.canon(k)]
        if kind not in seen:
            seen.append(kind)
            self.settings["seen_controllers"] = seen
            if save:
                _save_settings_if_exists(self.settings)
            # Rebuild the (warm-built) picker so the new controller's tab +
            # Options category exist. Only a HIDDEN picker is torn down 
            # closing a window the user is looking at would be jarring; a
            # visible one simply keeps its current tabs until reopened.
            try:
                import keybinds_picker
                keybinds_picker.rebuild_if_hidden()
            except Exception:
                pass
        if cache is not None:
            cache.add(kind)

    # Skin submenu: one radio item per bundled skin. pystray needs a distinct
    # checked-predicate and action per name, so we build small closures.
    def is_skin_checked(self, name):
        return lambda item: self.settings.get("skin") == name

    def select_skin(self, name):
        def _select(icon, item):
            self.settings["skin"] = name
            _save_settings(self.settings)
            adusk_skins.set_active_skin(name)
            # If the keyboard is open it re-skins live on its next frame (the
            # render loop polls skins.get_generation); otherwise it just opens
            # with the new skin next time.
            self._refresh_menu()
        return _select

    def _osk_transparency_fraction(self):
        """Current OSK transparency as a 0..1 fraction (tolerates an old named
        string slipping through from a stale settings file)."""
        v = self.settings.get("osk_transparency", 0.0)
        if isinstance(v, str):
            return _OSK_TRANSP_NAME_FRAC.get(v, 0.0)
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    def is_transparency_checked(self, level):
        # A named item is checked only when the slider sits exactly on its notch;
        # an in-between (custom) value leaves all four unchecked.
        target = _OSK_TRANSP_NAME_FRAC.get(level, 0.0)
        return lambda item: abs(self._osk_transparency_fraction() - target) < 1e-3

    def select_transparency(self, level):
        # OSK transparency level (Keyboard Skin → Transparent submenu). Stores the
        # notch's continuous fraction. Shares the skin generation counter, so an
        # open keyboard switches live on its next frame; else on the next open.
        def _select(icon, item):
            frac = _OSK_TRANSP_NAME_FRAC.get(level, 0.0)
            self.settings["osk_transparency"] = frac
            _save_settings(self.settings)
            adusk_skins.set_transparency_fraction(frac)
            self._refresh_menu()
        return _select

    # OSK size (Keyboard Skin → Size submenu): "small" / "medium" (default) /
    # "full" (fills the display - good for a Steam Deck). Unlike skin/
    # transparency this changes the window's pixel size and font sizes, which
    # are baked in at Screen() construction time, so it needs the cached
    # Screen rebuilt (see _rebuild_cached_screen).
    def is_osk_size_checked(self, name):
        return lambda item: self.settings.get("osk_size", "medium") == name

    def select_osk_size(self, name):
        def _select(icon, item):
            self.settings["osk_size"] = name
            _save_settings(self.settings)
            adusk_screen.set_osk_size(name)
            # Rebuilding the Screen creates/destroys an SDL window+renderer, which
            # must happen on launcher_thread (the render thread), NEVER here on the
            # tray menu's (main) thread. Defer it: launcher_thread rebuilds the
            # cached Screen before the next open, and after the current one if the
            # OSK is open right now (see toggle_keyboard_hotkey).
            self._pending_size_change = True
            self._refresh_menu()
        return _select

    def _ensure_cached_screen(self):
        """Build the cached OSK Screen if it doesn't exist yet. MUST run on
        launcher_thread  the same thread adusk.main() presents on (see
        __init__ for why cross-thread rendering breaks the mouse)."""
        if self._cached_screen is not None:
            return
        try:
            self._cached_screen = adusk_screen.Screen()
            from adusk import adusk as _adusk_mod
            _adusk_mod._make_window_non_activating(self._cached_screen.window)
        except Exception as e:
            print(f"Screen build failed (will retry on next open): {e!r}")
            self._cached_screen = None

    def _rebuild_cached_screen(self):
        """Destroy and recreate the cached OSK Screen so a new "Size" setting
        takes effect on the next open. MUST run on launcher_thread (same thread
        adusk.main() uses it on  both creation and destruction are thread-bound;
        see _ensure_cached_screen / __init__)."""
        if self._cached_screen is None:
            return
        try:
            S.SDL_DestroyRenderer(self._cached_screen.renderer)
            S.SDL_DestroyWindow(self._cached_screen.window)
        except Exception:
            pass
        try:
            self._cached_screen = adusk_screen.Screen()
            from adusk import adusk as _adusk_mod
            _adusk_mod._make_window_non_activating(self._cached_screen.window)
        except Exception as e:
            print(f"Screen rebuild failed: {e!r}")
            self._cached_screen = None

    # --- Steam Controller submenu (shown only while an SC is connected) -------
    def is_sc_connected(self, item):
        # Latched: once an SC is ever detected the menu stays for the whole
        # session. The live signal flickers (_current_sc goes None while adusk
        # owns the SC with the OSK open), which made the menu vanish; battery_thread
        # also sets the latch so it's set even if the menu is never opened live.
        # The reader's OWN family decides, not the presence of a battery
        # reading: the 2015 controller reports one too, and this legacy submenu
        # writes the SC's flat sc_* settings keys, which don't apply to it (its
        # settings live on its own Options category, under per-kind keys).
        _sc = self._current_sc
        if _sc is not None and "sc" in getattr(_sc, "_kinds", ("sc",)):
            self._sc_ever_connected = True
        # Debug menu mode forces every controller submenu visible regardless of
        # connection, so settings can be tweaked without the hardware attached.
        return self._sc_ever_connected or self.settings["debug_menu_unlocked"]

    def is_switch_connected(self, item):
        # Latched like is_sc_connected; set in sdl_gamepad_thread when a pad frame
        # is read. Gates the "Switch Pro Controller" submenu.
        return self._switch_ever_connected or self.settings["debug_menu_unlocked"]

    def is_kbd_lstick_mouse_checked(self, item):
        return self.settings.get("kbd_lstick_mouse", True)

    def toggle_kbd_lstick_mouse(self, icon, item):
        self.settings["kbd_lstick_mouse"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_kbd_stick_nav(self.settings["kbd_lstick_mouse"])

    def is_kbd_rstick_mouse_checked(self, item):
        return self.settings.get("kbd_rstick_mouse", True)

    def toggle_kbd_rstick_mouse(self, icon, item):
        self.settings["kbd_rstick_mouse"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_kbd_mouse_nav(self.settings["kbd_rstick_mouse"])

    def is_sc_actuation_checked(self, level):
        return lambda item: self.settings.get("sc_osk_trigger_actuation", "default") == level

    def select_sc_actuation(self, level):
        def _select(icon, item):
            self.settings["sc_osk_trigger_actuation"] = level
            _save_settings(self.settings)
            adusk_state.set_sc_osk_trigger_threshold(_SC_ACTUATION_THRESHOLDS.get(level))
        return _select

    def is_sc_mouse_actuation_checked(self, level):
        return lambda item: self.settings.get("sc_mouse_trigger_actuation", "default") == level

    def select_sc_mouse_actuation(self, level):
        def _select(icon, item):
            self.settings["sc_mouse_trigger_actuation"] = level
            _save_settings(self.settings)
            adusk_state.set_sc_mouse_trigger_threshold(_SC_ACTUATION_THRESHOLDS.get(level))
        return _select

    def is_sc_pointer_speed_checked(self, level):
        return lambda item: self.settings.get("sc_pointer_speed", "medium") == level

    def select_sc_pointer_speed(self, level):
        def _select(icon, item):
            self.settings["sc_pointer_speed"] = level
            _save_settings(self.settings)
            adusk_state.set_sc_mouse_speed(_SC_MOUSE_SPEEDS.get(level, 1.0))
        return _select

    def is_sc_trackpad_speed_checked(self, level):
        return lambda item: self.settings.get("sc_trackpad_speed", "medium") == level

    def select_sc_trackpad_speed(self, level):
        def _select(icon, item):
            self.settings["sc_trackpad_speed"] = level
            _save_settings(self.settings)
            adusk_state.set_sc_trackpad_speed(_SC_TRACKPAD_SPEEDS.get(level, 1.0))
        return _select

    def is_sc_scroll_speed_checked(self, level):
        return lambda item: self.settings.get("sc_scroll_speed", "medium") == level

    def select_sc_scroll_speed(self, level):
        def _select(icon, item):
            self.settings["sc_scroll_speed"] = level
            _save_settings(self.settings)
            adusk_state.set_sc_scroll_speed(_SC_SCROLL_SPEEDS.get(level, 1.0))
        return _select

    # --- Switch Pro Controller submenu (no actuation, no lstick toggle) ----

    def is_switch_pointer_speed_checked(self, level):
        return lambda item: self.settings.get("switch_pointer_speed", "medium") == level

    def select_switch_pointer_speed(self, level):
        def _select(icon, item):
            self.settings["switch_pointer_speed"] = level
            _save_settings(self.settings)
            adusk_state.set_switch_mouse_speed(_SC_MOUSE_SPEEDS.get(level, 1.0))
        return _select

    # tray menu actions -----------------------------------------------------

    def toggle_start_with_windows(self, icon, item):
        self.settings["start_with_windows"] = not item.checked
        _save_settings(self.settings)
        _apply_autostart(self.settings["start_with_windows"])

    def toggle_block_sc_hid(self, icon, item):
        self.settings["block_sc_hid"] = not item.checked
        _save_settings(self.settings)
        self._kick_sc()

    def toggle_block_gamepad_takeover(self, icon, item):
        # "Block SteamInput Xbox Controller grab"  hide the virtual ViGEm Xbox
        # 360 pad from Steam (see _set_xbox_ignore). Independent of block_sc_hid;
        # takes effect the next time Steam is launched, so no SC kick is needed.
        self.settings["block_gamepad_takeover"] = not item.checked
        _save_settings(self.settings)
        _set_xbox_ignore(self.settings["block_gamepad_takeover"])

    def toggle_sc_rumble(self, icon, item):
        # Steam Controller haptics  gates its OSK ticks, desktop/gamepad rumble.
        self.settings["rumble_enabled_sc"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sc", self.settings["rumble_enabled_sc"])
        # Turning it off mid-rumble: stop any SC motors currently playing.
        if not self.settings["rumble_enabled_sc"]:
            self._last_rumble = (None, None)
            sc = self._current_sc
            if sc is not None:
                try:
                    sc.set_rumble(0, 0)
                except Exception:
                    pass

    def toggle_switch_rumble(self, icon, item):
        # Nintendo Switch (SDL pad) haptics  gates its OSK ticks + rumble pulses.
        self.settings["rumble_enabled_switch"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sdl", self.settings["rumble_enabled_switch"])

    def toggle_disable_while_steam(self, icon, item):
        self.settings["disable_while_steam_running"] = not item.checked
        # Mutually exclusive with "Exit on Steam Launch"  only one at a time.
        if self.settings["disable_while_steam_running"]:
            self.settings["exit_on_steam_launch"] = False
        _save_settings(self.settings)
        # If the user just turned it off, clear the pause flag so the listener
        # resumes immediately even if Steam is still running.
        if not self.settings["disable_while_steam_running"]:
            self._steam_active.clear()
        # Wake the steam-watch thread so it re-evaluates whether to poll/idle.
        self._steam_watch_wake.set()

    def toggle_exit_on_steam(self, icon, item):
        self.settings["exit_on_steam_launch"] = not item.checked
        # Mutually exclusive with "Disable While Steam Is Running"  turning
        # this on forces that off (so the listener isn't left paused).
        if self.settings["exit_on_steam_launch"]:
            self.settings["disable_while_steam_running"] = False
            self._steam_active.clear()
        _save_settings(self.settings)
        self._steam_watch_wake.set()

    def handle_gamepad_toggle(self, held):
        """Called every SC frame (both modes) with whether a Hotkeys 'Gamepad
        Mode Toggle' chord is fully held. Fires the toggle ONCE per press; the
        latch (on the App, so it survives the watcher rebuild the toggle kicks)
        is cleared only when the chord is released, so holding it can't flip the
        mode repeatedly. Safe from the SC callback thread  _do_toggle drives the
        same addExit()/event path the watcher already uses."""
        if held and not self._gp_toggle_latched:
            self._gp_toggle_latched = True
            self._do_toggle_gamepad_mode()
        elif not held:
            self._gp_toggle_latched = False

    def handle_mode_hold(self, buttons, sdl=False):
        """Built-in "hold ≡ to switch Desktop <-> Gamepad" gesture, fed the raw
        button word every frame by whichever input path owns the controller (the
        HID watcher or the SDL pad loop  `sdl` picks that path's detector).

        Holding Start / Menu / ≡ / + / Options BY ITSELF for MODE_HOLD_SEC flips
        the live control scheme, exactly as the "Gamepad Mode Toggle" hotkey
        chord does. It exists because that chord has to be BOUND first, and a
        user who is stuck in gamepad mode on a couch can't go bind it: this
        gesture needs no setup. Passive trackpad/grip contact doesn't cancel the
        hold; any real button does, so Start-based chords are untouched.

        Returns the bits to strip from this frame  the Start bit for the rest
        of the press once the gesture fires, so the press that switched modes
        doesn't also fire Start's own binding on the way out."""
        fired, mask = (self._mode_hold_sdl if sdl else self._mode_hold_hid).step(
            buttons, int(SCButtons.START), time.monotonic(),
            passive_mask=_MODE_HOLD_PASSIVE_MASK)
        if fired:
            self._do_toggle_gamepad_mode()
            self._notify_mode_switch()
        return mask

    def _notify_mode_switch(self):
        """Toast the control scheme the hold-≡ gesture just selected. Read off
        _mode_override (the INTENT) rather than _gamepad_active, which only
        catches up once launcher_thread re-evaluates after the kick. Forcing
        gamepad controls is a no-op while the ViGEm Bus Driver is off  there's
        no virtual pad to drive  so say that instead of claiming a switch that
        didn't happen."""
        if self._mode_override:
            if not (self.settings["gamepad_mode"]
                    or self.settings["auto_gamepad_mode"]
                    or self.settings.get("gamepad_manual", False)):
                self._notify(
                    "Gamepad Mode unavailable",
                    "Turn on the ViGEm Bus Driver in Options → Gamepad Mode "
                    "first.")
                return
            self._notify("Gamepad Mode",
                         "Your controller now acts as an Xbox pad.")
        else:
            self._notify("Desktop Mode",
                         "Mouse, keyboard and your Desktop bindings are back.")

    def _do_toggle_gamepad_mode(self):
        """Flip the LIVE control scheme between gamepad and desktop  used by the
        "Hotkey Gamepad/Desktop Toggle" chord. This switches CONTROLS only: it
        sets a runtime override and does NOT change the ViGEm Bus Driver setting
        or tear down the virtual pad, so a game keeps seeing a connected
        controller while you use desktop controls (and vice-versa). Forcing
        gamepad controls has no effect while the driver is disabled (no pad to
        drive  the override never turns the driver on). Mirrors the kick/wake
        side effects so the launcher re-evaluates immediately."""
        # Flip the currently-published effective control mode to its opposite.
        self._mode_override = not self._gamepad_active
        self._kick_sc()
        self._auto_gamepad_wake.set()

    def handle_gyro_toggle(self, held, kind):
        """Called every HID frame (both modes) with whether a "Gyro To Mouse"
        hotkey chord  one of the bars inside that controller's gyro modal  is
        fully held.

        Handed straight to gyro_action_hold as one named holder, so the modal's
        chord and any BUTTON bound to "Gyro To Mouse" on a layout tab are two
        holders of the SAME thing. That keeps the mode semantics (Enable /
        Suppress / Toggle / None) in one place, keeps "toggle" firing once per
        press rather than per frame, and  the reason it matters  stops this
        frame-driven path from writing `held=False` over a hold the bound
        button is asserting."""
        gyro_action_hold(kind, "chord", held)

    def _toggle_gyro_mouse(self, kind):
        """Flip "Gyro To Mouse" for one controller kind (shared adusk_state 
        the OSK reads/toggles the same set). Session state only  gyro-mouse
        always starts OFF; the per-controller Options hotkey turns it on and
        off live. Confirmed with a light haptic tick on the pad, gated by that
        kind's own Haptics toggle."""
        on = adusk_state.toggle_gyro_mouse(kind)
        print(f"gyro-to-mouse {'on' if on else 'off'} ({kind})")
        if not adusk_state.is_rumble_enabled(kind):
            return
        try:
            if kind in pads.HID_KINDS:
                sc = self._current_sc
                if sc is not None and sc.is_live():
                    sc.haptic_pad_click()
            else:
                src = self._sdl_source
                if src is not None:
                    src.haptic_pad_click()
        except Exception:
            pass

    def _publish_gyro_masks(self):
        """Publish every gyro-capable kind's "Gyro To Mouse" hotkey masks to
        the shared adusk_state slot, so the OSK's own input path can flip the
        toggle while it owns the controllers (the tray paths cede then). HID
        kinds use the SC chord-button table; SDL kinds the SDL one  matching
        how each runtime builds its own masks."""
        adusk_state.set_gyro_toggle_masks({
            k: keybinds_runtime.build_gyro_toggle_masks(
                keybinds_runtime.chords_for(self.settings.get("chords", []), k),
                SCButtons,
                None if k in pads.HID_KINDS
                else keybinds_runtime.SDL_CHORD_BUTTONS)
            for k in pads.KINDS if pads.has_gyro(k)})

    def _publish_gyro_config(self):
        """Publish every gyro-capable kind's cog-modal tuning (mode / Dots Per
        360° / sensitivity / acceleration / deadzone / precision) to the
        shared adusk_state slots, and seed each kind's live on/off state from
        its mode: hold_suppress = gyro on until the hotkey suppresses it;
        every other mode (none / hold_enable / toggle) starts off."""
        g = self.settings
        for k in pads.KINDS:
            if not pads.has_gyro(k):
                continue
            adusk_state.set_gyro_config(
                k,
                mode=g.get(pads.setting_key(k, "gyro_mode"),
                           adusk_state.GYRO_DEFAULTS["mode"]),
                dots=g.get(pads.setting_key(k, "gyro_dots"), 6545),
                sens=g.get(pads.setting_key(k, "gyro_sens"), 2.5),
                accel=g.get(pads.setting_key(k, "gyro_accel"), "off"),
                deadzone=g.get(pads.setting_key(k, "gyro_deadzone"), 0.36),
                precision=g.get(pads.setting_key(k, "gyro_precision"), 0.75),
                output=g.get(pads.setting_key(k, "gyro_output"), "mouse"))
            adusk_state.set_gyro_mouse(
                k, adusk_state.get_gyro_mode(k) == "hold_suppress")

    # "Gyro To Type" keys, in one place: the Options → Keyboard cog's global
    # gyro tuning. (base, default)  the base IS the adusk_state config key
    # with the "kbd_gyro_" prefix stripped.
    _KBD_GYRO_KEYS = (("sens", 2.5), ("accel", "off"),
                      ("deadzone", 0.36), ("precision", 0.75))

    def _publish_kbd_gyro_config(self):
        """Publish the GLOBAL gyro-typing tuning (Options → Keyboard → Gyro To
        Type) to adusk_state  one config for every controller, read by the
        OSK's gyro pointer."""
        g = self.settings
        adusk_state.set_kbd_gyro_config(
            **{base: g.get("kbd_gyro_" + base, dv)
               for base, dv in self._KBD_GYRO_KEYS})

    def toggle_gamepad_mode(self, icon, item):
        # Mutually exclusive with auto mode: turning Always-On on forces
        # Auto-enable off (and drops any latched game), so the two options
        # behave like radio buttons.
        self.settings["gamepad_mode"] = not item.checked
        if self.settings["gamepad_mode"]:
            self.settings["auto_gamepad_mode"] = False
            self.settings["gamepad_manual"] = False
            if self._auto_gamepad_pid is not None:
                self._auto_gamepad_pid = None
                self._auto_gamepad_focused = False
        _save_settings(self.settings)
        # A deliberate mode change clears any Hotkey-toggle control override.
        self._mode_override = None
        # Kick the current SC loop so the launcher thread picks up the new
        # mode immediately instead of waiting for the next chord event.
        self._kick_sc()
        # Wake the (now idle) auto-gamepad thread so it re-evaluates.
        self._auto_gamepad_wake.set()

    def toggle_auto_gamepad_mode(self, icon, item):
        # Mutually exclusive with manual mode: turning Auto-enable on forces
        # Always-On off.
        self.settings["auto_gamepad_mode"] = not item.checked
        if self.settings["auto_gamepad_mode"]:
            self.settings["gamepad_mode"] = False
            self.settings["gamepad_manual"] = False
        _save_settings(self.settings)
        # A deliberate mode change clears any Hotkey-toggle control override.
        self._mode_override = None
        # If the user just turned auto mode off, drop any latched game
        # immediately so the launcher reverts to the manual setting.
        if not self.settings["auto_gamepad_mode"] and self._auto_gamepad_pid is not None:
            self._auto_gamepad_pid = None
            self._auto_gamepad_focused = False
        self._kick_sc()
        # Wake the auto-gamepad thread so it starts scanning (or idles) now.
        self._auto_gamepad_wake.set()

    def select_gamepad_off(self, icon, item):
        # Third radio option: disable every gamepad path. No-op if already off
        # (clicking the checked item shouldn't toggle anything on).
        if (not self.settings["gamepad_mode"]
                and not self.settings["auto_gamepad_mode"]
                and not self.settings.get("gamepad_manual", False)):
            return
        self.settings["gamepad_mode"] = False
        self.settings["auto_gamepad_mode"] = False
        self.settings["gamepad_manual"] = False
        _save_settings(self.settings)
        # A deliberate mode change clears any Hotkey-toggle control override.
        self._mode_override = None
        if self._auto_gamepad_pid is not None:
            self._auto_gamepad_pid = None
            self._auto_gamepad_focused = False
        self._kick_sc()
        # Wake the auto-gamepad thread so it idles immediately.
        self._auto_gamepad_wake.set()

    # -- tray "Game Mode" ----------------------------------------------------
    # ONE hard on/off in the tray menu for what Options → Gamepad Mode spreads
    # over a master switch and a three-way dropdown. The tray is where somebody
    # reaches when a game is already running and the controller is doing the
    # wrong thing, so it gets the blunt version:
    #   ON  -> Gamepad Mode Activation = "Always on Gamepad Mode"
    #   OFF -> the master ViGEm Bus Driver switch off (no virtual pad at all)
    # Both go through the picker's own Options channel (_apply_general_setting
    # with the "always"/"off" values it emits), so the mutual exclusion, the
    # save, the SC kick and the auto-switcher wake are the ones already written
    # for that page  the tray does not get its own half-copy of that logic.
    #
    # Deliberately NOT a three-way here: "auto" and "manual" both mean "gamepad
    # sometimes", which a checkbox cannot honestly show. Anyone who wants those
    # picks them on the page; this item only ever answers "is it forced on right
    # now", and unticking it lands on the one state that is unambiguously off.

    def game_mode_available(self, item=None):
        """Visibility callback for the tray item. Always True on Linux (the
        virtual-pad backend is uinput, which is always there); on Windows it
        hides outright without the ViGEmBus driver, since a tray checkbox that
        silently refuses is worse than no checkbox."""
        return _vigem_bus_ok()

    def is_game_mode_checked(self, item=None):
        """Ticked only for "always on"  see the note above on why auto/manual
        deliberately read as unticked."""
        return bool(self.settings.get("gamepad_mode", False))

    def toggle_game_mode(self, icon=None, item=None):
        # Read the live setting rather than item.checked: the menu's rendered
        # state can lag a mode change made by the chord or the auto-switcher
        # (the menu is only rebuilt on the tray-state tick), and toggling off a
        # stale tick would silently do the opposite of what was clicked.
        on = not self.is_game_mode_checked(None)
        self._apply_general_setting("gamepad_mode", "always" if on else "off")
        self._refresh_menu()

    # -- tray "Virtual Menu" submenu -----------------------------------------
    # Every virtual menu the user has built, listed as its own item; clicking
    # one shows its overlay and clicking it again puts it away. This is the
    # only way to open a menu that has NO trigger bound at all  a freshly
    # created one, or the shipped default on a PC with no controller attached 
    # so it doubles as the "what did I just build?" preview.
    #
    # The overlay itself is _KeyVMenuRunner's (the keyboard/mouse-trigger
    # path), not the controller watcher's: a tray-opened menu is steered by the
    # mouse and the arrow keys, which is exactly what that runner already does.
    # See its open_from_tray().

    def _tray_vmenu_runner(self):
        return getattr(self, "_key_vmenus", None)

    def virtual_menu_items(self):
        """Generator behind the "Virtual Menu" submenu  pystray re-runs it on
        every menu rebuild, so the list follows the user's menus with nothing
        to invalidate. Yielding NOTHING hides the parent item automatically
        (pystray drops a MenuItem whose submenu has no visible items), which is
        what a user who hasn't made any menus should see."""
        runner = self._tray_vmenu_runner()
        if runner is None:
            return
        try:
            rows = runner.tray_menu_rows()
        except Exception as e:
            print(f"tray virtual-menu list failed: {e!r}")
            return
        for name, is_open in rows:
            activate, checked = self._vmenu_item_callbacks(name, is_open)
            yield pystray.MenuItem(name, activate, checked=checked)

    def _vmenu_item_callbacks(self, name, is_open):
        """(action, checked) for one submenu row, closed over this row's name.

        A closure rather than the usual default-argument capture on purpose:
        pystray decides how to call an action from its `co_argcount`, and a
        default parameter counts toward that  a `lambda icon, item, n=name`
        reads as arity 3 and is REJECTED outright (_assert_action raises)."""
        def activate(_icon, _item):
            self.toggle_virtual_menu(name)

        def checked(_item):
            return is_open

        return activate, checked

    def toggle_virtual_menu(self, name):
        runner = self._tray_vmenu_runner()
        if runner is None:
            return
        try:
            runner.toggle_from_tray(name)
        except Exception as e:
            print(f"tray virtual-menu toggle failed for {name!r}: {e!r}")
        self._refresh_menu()

    def _menu_state(self):
        """A cheap snapshot of everything the tray MENU renders from state
        rather than from a fixed string: the Game Mode tick, and the virtual
        menus with which one is on screen. Compared against the last one to
        decide whether the menu needs rebuilding (see _refresh_menu_state)."""
        runner = self._tray_vmenu_runner()
        rows = ()
        if runner is not None:
            try:
                rows = tuple(runner.tray_menu_rows())
            except Exception:
                rows = ()
        return (bool(self.settings.get("gamepad_mode", False)), rows)

    def _refresh_menu_state(self):
        """Rebuild the tray menu when what it would DRAW has changed.

        pystray builds the popup menu when it is set, not on each right-click,
        so a `checked` callback is only re-read when something calls
        update_menu(). The click handlers do that for their own change, but
        Game Mode is equally reachable from the picker, the Hotkey Gamepad/
        Desktop chord, the hold-Start gesture and the auto-switcher, and a menu
        can be closed with Escape or by editing the list. This is the catch-all
        for all of those: one tuple compare per tick, and a rebuild only when it
        actually moved."""
        state = self._menu_state()
        if state == self._tray_menu_state:
            return
        self._tray_menu_state = state
        self._refresh_menu()

    def tray_menu_thread(self):
        """Keep the menu's state-driven items honest against changes made
        anywhere else. The Windows tree folds this into tray_icon_thread (it
        has a state-driven tray ICON to poll for as well); this tree has no
        such thread, so the menu gets its own  same 0.5 s tick, and it does
        nothing at all unless _menu_state() actually moved."""
        while not self._stop_event.is_set():
            self._refresh_menu_state()
            self._stop_event.wait(0.5)

    def _start_chime(self, sc, on):
        """Play the gamepad on/off chime on `sc` in a daemon thread once it's
        live. The launcher caller is about to block in sc.run(), which is what
        actually opens the device (~1s later  see the rebuild-latency note),
        so we wait for sc.is_live() rather than playing on a not-yet-open handle
        (the bug that first made the chime silent). Gated by the global haptics
        switch. Logging is opt-in via ADUSK_GAMEPAD_DEBUG."""
        if not adusk_state.is_rumble_enabled(self._hid_kind):
            _chime_log(f"chime(on={on}) skipped: haptics switch off")
            return

        def _worker():
            for i in range(250):  # up to ~5s (250 * 20ms)
                if self._stop_event.is_set():
                    return
                if sc.is_live():
                    _chime_log(f"chime(on={on}): device live after {i*20}ms, playing")
                    try:
                        sc.play_chime(on)
                    except Exception as e:
                        _chime_log(f"chime(on={on}): play_chime raised: {e!r}")
                    return
                time.sleep(0.02)
            _chime_log(f"chime(on={on}): gave up, device never opened (~5s)")

        threading.Thread(target=_worker, daemon=True).start()

    def _publish_hid_settings(self, kind):
        """Publish `kind`'s per-controller settings into the shared takeover-
        runtime slots (the sc_* adusk_state values the _Watcher and OSK read
        per frame). One HID controller family drives at a time, so a single
        slot set per setting is enough  the launcher republishes on every
        rebuild, and _apply_general_setting refreshes live edits that target
        the currently-active family. The SC's keys are the legacy flat names;
        every other HID family's are the per-kind "<base>_<kind>" copies
        ("trackpad_speed_steam_deck", "pointer_speed_sc2015", ...)."""
        g = self.settings
        adusk_state.set_sc_osk_trigger_threshold(_sc_actuation_threshold(
            g.get(pads.setting_key(kind, "osk_trigger_actuation"), "default")))
        adusk_state.set_sc_mouse_trigger_threshold(_sc_actuation_threshold(
            g.get(pads.setting_key(kind, "mouse_trigger_actuation"), "default")))
        adusk_state.set_sc_gamepad_trigger_threshold(_sc_actuation_threshold(
            g.get(pads.setting_key(kind, "gamepad_trigger_actuation"), "default")))
        adusk_state.set_sc_mouse_speed(_sc_speed_mult(
            g.get(pads.setting_key(kind, "pointer_speed"), "medium")))
        adusk_state.set_sc_trackpad_speed(_sc_trackpad_mult(
            g.get(pads.setting_key(kind, "trackpad_speed"), "medium")))
        adusk_state.set_lpad_click_button(
            g.get(pads.setting_key(kind, "lpad_click_button"), "l2"))
        adusk_state.set_rpad_click_button(
            g.get(pads.setting_key(kind, "rpad_click_button"), "r2"))

    def _kick_sc(self):
        """Force the current SteamController loop to exit so launcher_thread
        re-evaluates settings (gamepad mode, auto-detected game state). Flags
        the exit as intentional so the launcher rebuilds immediately instead of
        applying the reconnect backoff (which otherwise delays the mode chime)."""
        self._intentional_kick.set()
        if self._current_sc is not None:
            try:
                self._current_sc.addExit()
            except Exception:
                pass
        # Also wake the launcher out of its reconnect-backoff sleep. With NO
        # Steam Controller present (e.g. only a Switch Pro), there's no sc.run()
        # to break, so without this the launcher wouldn't recompute
        # _gamepad_active until the backoff (up to 5s) expired  making auto
        # gamepad mode lag badly for SDL pads. The SDL thread reads
        # _gamepad_active, so this makes its mode switch as instant as the SC's.
        self._launcher_wake.set()
        # Same for player 2+: a kick can mean the Steam pause or the block_sc_hid
        # sharing mode changed, both of which their readers have to follow.
        self._sc_extra_wake.set()

    def _ensure_persistent_gamepad(self):
        """Construct the ViGEm virtual pad if it doesn't already exist.
        Sets self._persistent_gamepad to None on failure."""
        if self._persistent_gamepad is not None:
            return
        try:
            self._persistent_gamepad = VirtualGamepad()
            # Forward game force-feedback to the physical rumble motors.
            self._persistent_gamepad.register_rumble(self._on_game_rumble)
        except ViGEmUnavailable as e:
            print(f"gamepad requested but unavailable: {e}")
            self._persistent_gamepad = None

    def _on_game_rumble(self, large, small):
        """ViGEm force-feedback callback for the PERSISTENT pad (player 1).
        Forward the game's large/small motor intensities (0..255) to whichever
        physical controller currently owns that pad: the live Steam Controller,
        or  when no SC is live  the primary SDL pad (the first controller,
        which reuses the persistent pad). Each ADDITIONAL SDL pad has its own
        virtual pad with its own rumble callback, so players never cross-buzz.
        Runs on a ViGEm thread; dedups so we only write when the value changes."""
        vals = (int(large), int(small))
        sc = self._current_sc
        if sc is not None and sc.is_live():
            _gp_rumble_key = pads.setting_key(
                getattr(sc, "kind", None) or self._hid_kind, "rumble_gamepad")
            if not self.settings.get(_gp_rumble_key, True):
                # This controller's gamepad-mode vibration is off  drop game
                # FFB (the desktop "Vibration" toggle only gates the app's own
                # UI haptics).
                self._last_rumble = (None, None)
                return
            if vals == self._last_rumble:
                return
            self._last_rumble = vals
            sc.set_rumble(vals[0], vals[1])
            return
        # No live SC → the persistent pad is the primary SDL controller's slot;
        # rumble only that one physical pad (by its SDL instance id).
        if not self.settings.get("rumble_gamepad_switch", True):
            self._last_rumble = (None, None)
            return
        if vals == self._last_rumble:
            return
        self._last_rumble = vals
        src = self._sdl_source
        jid = self._primary_sdl_jid
        if src is not None and jid is not None:
            try:
                src.set_rumble_pad(jid, vals[0], vals[1])
            except Exception:
                pass

    def _close_persistent_gamepad(self):
        pad = self._persistent_gamepad
        self._persistent_gamepad = None
        if pad is not None:
            try:
                pad.close()
            except Exception:
                pass

    # --- Multi-Steam-Controller: one virtual pad per EXTRA controller --------
    #
    # The launcher drives exactly ONE Steam Controller / Deck: player 1, the pad
    # that owns the desktop takeover, the OSK, chords, haptics, the live
    # controller preview and _persistent_gamepad. A SECOND Steam Controller (the
    # Triton dongle exposes one data interface per paired pad, and a wired unit
    # adds another) used to be left completely untouched  still in firmware
    # lizard mode, so it fought player 1 over the one cursor on the desktop, and
    # invisible to games because nothing ever built it a virtual pad.
    #
    # sc_extra_thread claims every controller player 1 did NOT take, one reader
    # thread each, and gives it its own ViGEm device  so N Steam Controllers
    # enumerate as N XInput players, the same "it just works" multiplayer the
    # SDL pads already get below (see _feed_sdl_gamepads).
    #
    # Extras are deliberately GAMEPAD-ONLY. Their lizard mode goes off, so they
    # can no longer type or move the cursor behind player 1's back, but they
    # drive no desktop input, no OSK, no chords, no picker navigation and no
    # controller preview: one cursor, one owner. Player 2 gets a gamepad.
    #
    # LINUX CAVEAT (see steamcontroller._claim_remaining_interfaces): with
    # block_sc_hid on, an exclusive open also parks a handle on every OTHER
    # interface it was allowed to see, to keep Steam off the whole dongle. The
    # path filters keep the players out of each other's way, but a controller
    # that pairs AFTER player 1 opened exclusively lands on an interface player 1
    # is already sitting on, and its reader will not get it until the next
    # rebuild. Turning block_sc_hid off, or connecting both pads first, avoids
    # it; a real fix belongs in that claim loop, not here.

    _SC_EXTRA_POLL = 2.0       # hot-plug rescan interval (cheap HID listing)
    _SC_EXTRA_RETRY = 5.0      # backoff after a probe found nothing responsive

    def sc_extra_thread(self):
        """Crash-proof wrapper around the extra-controller supervisor, matching
        launcher_thread's contract: an unexpected exception restarts the loop
        instead of silently killing multiplayer for the rest of the session."""
        while not self._stop_event.is_set():
            try:
                self._sc_extra_loop()
                return                      # clean exit (shutdown)
            except Exception:
                traceback.print_exc()
                print("sc extra thread crashed; restarting in 2s")
                self._stop_event.wait(2.0)
        self._close_sc_extras()

    def _sc_extra_loop(self):
        while not self._stop_event.is_set():
            self._sc_extra_wake.wait(self._SC_EXTRA_POLL)
            self._sc_extra_wake.clear()
            if self._stop_event.is_set():
                break
            if self._steam_active.is_set():
                # Paused for Steam: cede EVERY controller, not just player 1, so
                # Steam can claim them all and present its own virtual pads.
                self._close_sc_extras()
                continue
            self._recycle_stale_sc_extras()
            self._claim_next_sc_extra()
        self._close_sc_extras()

    def _refresh_sc_extra_paths(self):
        """Re-snapshot the HID paths the extras hold for the primary's
        exclude_paths filter. Called from both the supervisor and the extras'
        own worker threads, so it iterates a copy."""
        paths = set()
        for r in list(self._sc_extras.values()):
            if r.path:
                paths.add(r.path)
            else:
                paths.update(r.claim)   # still probing  hold the whole list
        self._sc_extra_paths = frozenset(paths)

    def _recycle_stale_sc_extras(self):
        """Drop extras whose exclusivity no longer matches the block_sc_hid
        toggle. The next tick re-claims the same device with the new sharing
        mode, so the toggle applies to player 2+ exactly as it does to player 1
        (whose reader is rebuilt by _kick_sc)."""
        want = bool(self.settings.get("block_sc_hid"))
        for rec in list(self._sc_extras.values()):
            if rec.exclusive != want and not rec.opening:
                self._stop_sc_extra(rec)

    def _claim_next_sc_extra(self):
        """Hand the next unclaimed controller its own reader  at most ONE per
        tick, deliberately.

        A reader that is still probing has no path yet but has a claim on every
        free interface (it walks them looking for the first responsive one), so
        starting a second one concurrently could race it onto the same device.
        Serializing costs at most a couple of seconds on a cold start with 3+
        controllers, and removes the race entirely."""
        if time.monotonic() < self._sc_extra_retry_at:
            return
        # Player 1 first: until its reader is open we don't know which device is
        # its, and claiming blind could take the one it is about to rebuild onto.
        primary = self._current_sc
        ppath = getattr(primary, "path", None)
        if not ppath or not primary.is_live():
            return
        if any(r.opening for r in list(self._sc_extras.values())):
            return
        owned = {ppath}
        owned.update(self._sc_extra_paths)
        try:
            free = [c for c in enumerate_data_interfaces(pads.HID_KINDS)
                    if c['path'] not in owned]
        except Exception as e:
            print(f"multiplayer: HID enumeration failed: {e!r}")
            return
        if not free:
            return
        self._spawn_sc_extra(free)

    def _spawn_sc_extra(self, cands):
        """Start a reader for the first RESPONSIVE interface among `cands`.

        The candidate list (not a single pinned path) is what makes this robust:
        the dongle lists an interface for every pairing slot and the Deck lists
        its lizard mouse/kb interfaces, most of which never stream. Handing the
        whole free list to the opener reuses its existing probe to skip them."""
        exclusive = bool(self.settings.get("block_sc_hid"))
        kinds = tuple(k for k in pads.HID_KINDS
                      if any(c.get('_kind') == k for c in cands))
        paths = [c['path'] for c in cands]
        rec = _ScExtra(exclusive, claim=paths)
        rec.sc = SteamController(
            callback=lambda _sc, _sci, _rec=rec: self._on_sc_extra_input(
                _rec, _sc, _sci),
            passive=False,
            exclusive=exclusive,
            kinds=kinds or pads.HID_KINDS,
            paths=paths)
        key = id(rec)
        self._sc_extras[key] = rec
        # Publish the claim BEFORE the reader starts probing, so player 1 can't
        # open one of these interfaces out from under it (see _ScExtra.claim).
        self._refresh_sc_extra_paths()
        rec.thread = threading.Thread(target=self._sc_extra_worker,
                                      args=(key, rec), daemon=True,
                                      name="sc-extra")
        rec.thread.start()

    def _sc_extra_worker(self, key, rec):
        """Own one extra controller for its whole session: sc.run() opens the
        device, disables lizard mode and blocks streaming frames to
        _on_sc_extra_input, returning when the pad disconnects, is powered off,
        or we ask it to stop."""
        sc = rec.sc
        try:
            sc.run()
        except Exception as e:
            print(f"multiplayer: extra reader ended: {e!r}")
        finally:
            never_opened = not sc.opened
            rec.opening = False
            pad = rec.pad
            rec.pad = None
            if pad is not None:
                try:
                    pad.close()
                except Exception:
                    pass
            self._sc_extras.pop(key, None)
            self._refresh_sc_extra_paths()
            if never_opened:
                # Nothing responsive among the free interfaces  an unpaired
                # dongle slot, or a controller that is simply switched off.
                # Back off so the supervisor doesn't respawn a 1.5 s-per-
                # candidate probe every couple of seconds forever.
                self._sc_extra_retry_at = (time.monotonic()
                                           + self._SC_EXTRA_RETRY)
            else:
                print("multiplayer: extra controller disconnected")

    def _stop_sc_extra(self, rec):
        """Ask one extra's reader to exit; its worker does the teardown. sc.run()
        polls HID with a 200 ms timeout, so this lands promptly even when the
        controller is idle and sending nothing."""
        sc = rec.sc
        if sc is not None:
            try:
                sc.addExit()
            except Exception:
                pass

    def _close_sc_extras(self):
        """Release every extra controller (shutdown, or pausing for Steam).
        Each worker restores that pad's lizard mode and closes its HID handle on
        the way out, so the controllers go back to being usable by Steam / as a
        firmware mouse the moment we let go."""
        for rec in list(self._sc_extras.values()):
            self._stop_sc_extra(rec)

    def _sc_extra_conf(self, kind, pad):
        """Cached gamepad-mode remap for an extra Steam Controller / Deck: the
        picker's Gamepad-tab binds for `kind`, compiled the same way player 1's
        are (resolve_sc_gamepad  NOT the SDL flavour, whose control table has
        no back paddles or pad clicks). Invalidated wherever _sdl_gp_cache is."""
        conf = self._sc_extra_gp_cache.get(kind)
        if conf is not None:
            return conf
        gp = keybinds_runtime.gamepad_submap(
            (self.settings.get("keybinds") or {}).get(kind) or {})
        bmap, lt, rt = keybinds_runtime.resolve_sc_gamepad(gp, SCButtons)
        compiled = None
        try:
            compiled = pad.compile_button_map(bmap)
        except Exception as e:
            print(f"multiplayer: button-map compile failed ({kind}): {e!r}")
        lmap, rmap = keybinds_runtime.resolve_sc_gamepad_sticks(gp)
        lflags = rflags = None
        try:
            if lmap:
                lflags = {z: pad.action_flag(a) for z, a in lmap.items()}
                lflags = {z: fl for z, fl in lflags.items() if fl} or None
            if rmap:
                rflags = {z: pad.action_flag(a) for z, a in rmap.items()}
                rflags = {z: fl for z, fl in rflags.items() if fl} or None
        except Exception:
            lflags = rflags = None
        conf = {"map": compiled, "lt": lt, "rt": rt,
                "lflags": lflags, "rflags": rflags}
        self._sc_extra_gp_cache[kind] = conf
        return conf

    def _ensure_sc_extra_pad(self, rec):
        """Build this extra's own ViGEm device, with force-feedback routed back
        to THIS physical controller only, so player 2's rumble never buzzes
        player 1. Returns None (once, quietly) if ViGEm is unavailable."""
        try:
            pad = VirtualGamepad()
        except ViGEmUnavailable as e:
            print(f"multiplayer: extra virtual pad unavailable: {e}")
            return None

        def _rumble(large, small, _rec=rec):
            # Same gate player 1 uses: this controller kind's "Haptics Gamepad
            # Mode" toggle, not the desktop Vibration one.
            if not self.settings.get(
                    pads.setting_key(_rec.kind or "sc", "rumble_gamepad"), True):
                _rec.last_rumble = (None, None)
                return
            vals = (int(large), int(small))
            if vals == _rec.last_rumble:
                return
            _rec.last_rumble = vals
            _sc = _rec.sc
            if _sc is not None and _sc.is_live():
                try:
                    _sc.set_rumble(vals[0], vals[1])
                except Exception:
                    pass

        try:
            pad.register_rumble(_rumble)
        except Exception:
            pass
        rec.pad = pad
        rec.zeroed = False
        print(f"multiplayer: extra {rec.kind} controller is now its own "
              "XInput player")
        return pad

    def _on_sc_extra_input(self, rec, sc, sci):
        """Per-frame callback for ONE extra controller, on its own reader thread.

        Everything player 1's _Watcher does (desktop takeover, chords, OSK,
        viewer, gyro, haptic feedback) is intentionally absent  this only ever
        writes to this controller's own virtual pad."""
        if sci.status != SCStatus.INPUT:
            return
        if rec.opening:
            # First frame: the probe has settled on a device, so publish which
            # path this player owns (the primary's exclude filter reads it).
            rec.opening = False
            rec.path = sc.path
            rec.kind = sc.kind
            rec.claim = frozenset()     # narrow the claim to the one device
            self._refresh_sc_extra_paths()
            print(f"multiplayer: claimed extra {sc.kind} controller")
        if self._stop_event.is_set() or self._steam_active.is_set():
            sc.addExit()
            return
        pad = rec.pad
        if self._persistent_gamepad is None:
            # No gamepad output anywhere this session (pure desktop use): don't
            # hold a virtual device. This controller stays silent  lizard is
            # off, so it can't fight player 1 for the cursor.
            if pad is not None:
                rec.pad = None
                try:
                    pad.close()
                except Exception:
                    pass
            return
        if pad is None:
            pad = self._ensure_sc_extra_pad(rec)
            if pad is None:
                return
        if not self._gamepad_active:
            # Player 1 is on the desktop control scheme. Keep the pad ALIVE (so
            # a game that starts now still enumerates every player) but zeroed,
            # exactly as the persistent pad behaves in the same state.
            if not rec.zeroed:
                rec.zeroed = True
                try:
                    pad.reset()
                except Exception:
                    pass
            return
        rec.zeroed = False
        try:
            self._feed_sc_extra(rec, pad, sci)
        except Exception as e:
            print(f"multiplayer: extra pad update failed: {e!r}")

    def _feed_sc_extra(self, rec, pad, sci):
        """Push one extra controller's frame to its virtual XInput device,
        honoring its kind's Gamepad-tab remap (compiled button map, trigger
        analog gates, digital stick zones).

        Advanced presses, Button Combos and keyboard-action binds are NOT
        applied here on purpose: those inject into the desktop (keys, clicks,
        system actions), which player 2 must not be able to do from a game."""
        conf = self._sc_extra_conf(rec.kind or "sc", pad)
        extra = 0
        lz = conf["lflags"] is not None
        rz = conf["rflags"] is not None
        if lz:
            lx, ly = sci.lstick_x, sci.lstick_y
            if (abs(lx) > self._SDL_GP_STICK_DEADZONE
                    or abs(ly) > self._SDL_GP_STICK_DEADZONE):
                zone = ("UP" if ly > 0 else "DOWN") if abs(ly) >= abs(lx) \
                    else ("RIGHT" if lx > 0 else "LEFT")
                extra |= conf["lflags"].get(zone, 0)
        if rz:
            rx, ry = sci.rstick_x, sci.rstick_y
            if (abs(rx) > self._SDL_GP_STICK_DEADZONE
                    or abs(ry) > self._SDL_GP_STICK_DEADZONE):
                zone = ("UP" if ry > 0 else "DOWN") if abs(ry) >= abs(rx) \
                    else ("RIGHT" if rx > 0 else "LEFT")
                extra |= conf["rflags"].get(zone, 0)
        pad.update(sci, conf["map"], conf["lt"], conf["rt"], extra, lz, rz)

    # --- Automatic multiplayer: one dedicated virtual pad per SDL controller -
    #
    # All owned by sdl_gamepad_thread (single-writer), so no lock is needed on
    # self._sdl_gamepads. Rumble callbacks run on ViGEm threads but only call
    # back into SDL rumble (defensive / thread-safe enough). Active whenever
    # gamepad output is live and a 2nd+ controller is present (the first reuses
    # the persistent pad); otherwise the pool stays empty.

    # How long a vanished wireless controller's virtual gamepad is held open
    # for it. Covers a Nintendo Bluetooth dropout (the controller is usually
    # back within a few seconds) with room to spare, while staying short enough
    # that a genuinely-switched-off pad frees its device promptly.
    _PAD_DROPOUT_GRACE = 20.0

    def _apply_nintendo_sdl_hints(self):
        """Nintendo controller support, declared to SDL before SDL_Init.

        Everything Nintendo made for the Switch 1  the Pro Controller, both
        Joy-Cons, the NSO SNES/NES/N64/Genesis pads and the GameCube adapter 
        is driven by SDL's own HIDAPI drivers once it is paired, so most of
        this is making the defaults explicit rather than changing them: the
        drivers all inherit SDL_JOYSTICK_HIDAPI (on), and naming them means a
        future SDL that ships one of them OFF by default can't quietly drop a
        controller family.

        The two that DO carry a user choice are the Joy-Con ones. SDL treats a
        connected L+R as a single Pro-Controller-shaped pad by default, which
        is what most people want; turning that off gives each half its own pad,
        so two people can play with one Joy-Con each. A lone Joy-Con is
        presented sideways unless told otherwise. Both are read by SDL at init,
        so a change needs a restart.

        SWITCH2 covers the Switch 2 pads. The SDL shipped here (3.4.10) has no
        Switch 2 driver  verified, its hint table doesn't contain the name 
        so this does nothing today and costs nothing; it is here so that
        dropping in an SDL that does have one is the whole upgrade."""
        for hint, value in (
            (b"SDL_JOYSTICK_HIDAPI_JOY_CONS", b"1"),
            (b"SDL_JOYSTICK_HIDAPI_NINTENDO_CLASSIC", b"1"),   # NSO retro pads
            (b"SDL_JOYSTICK_HIDAPI_GAMECUBE", b"1"),
            (b"SDL_JOYSTICK_HIDAPI_SWITCH2", b"1"),
            (b"SDL_JOYSTICK_HIDAPI_COMBINE_JOY_CONS",
             b"1" if self.settings.get("joycon_combine", True) else b"0"),
            (b"SDL_JOYSTICK_HIDAPI_VERTICAL_JOY_CONS",
             b"1" if self.settings.get("joycon_vertical", False) else b"0"),
        ):
            try:
                S.SDL_SetHint(hint, value)
            except Exception:
                pass

    def _pad_uid(self, jid):
        """Stable identity of the controller behind an SDL instance id - what
        the park bookkeeping keys off, since instance ids are not reused."""
        src = self._sdl_source
        uid = None
        if src is not None:
            try:
                uid = src.uid_of(jid)
            except Exception:
                uid = None
        return uid or ("jid:%s" % jid)

    def _hold_slot_enabled(self):
        return bool(self.settings.get("bt_hold_slot", True))

    def _park_sdl_gamepad(self, jid, pad):
        """A controller disappeared: keep its virtual gamepad open and zeroed
        for _PAD_DROPOUT_GRACE, reserved for that same controller.

        This is the half of the Nintendo fix a game actually notices. Nintendo
        pads drop the Bluetooth link roughly every 20 minutes no matter what
        the host does (see nintendo_bt.py) and reconnect on their own seconds
        later; if we destroyed the virtual device in between, the game saw its
        gamepad unplugged - and plenty of games never re-acquire one, or
        re-acquire it as a different player. Parking means the device never
        goes away: input just stops for a moment and then resumes.

        Falls back to closing the device when the hold is turned off."""
        if not self._hold_slot_enabled():
            try:
                pad.close()
            except Exception:
                pass
            return
        try:
            pad.reset()   # nothing may stay held down while the pad is away
        except Exception:
            pass
        uid = self._pad_uid(jid)
        old = self._sdl_parked.pop(uid, None)
        if old is not None and old[0] is not pad:
            try:
                old[0].close()
            except Exception:
                pass
        self._sdl_parked[uid] = (pad, time.monotonic() + self._PAD_DROPOUT_GRACE)
        print(f"sdl pad {jid} dropped - holding its gamepad slot for "
              f"{self._PAD_DROPOUT_GRACE:.0f}s ({uid})")

    def _claim_parked_gamepad(self, jid):
        """Hand a reconnecting controller back the virtual pad it was using
        before it dropped, or None if it has none parked."""
        entry = self._sdl_parked.pop(self._pad_uid(jid), None)
        if entry is None:
            return None
        pad, expiry = entry
        if time.monotonic() > expiry:
            try:
                pad.close()
            except Exception:
                pass
            return None
        print(f"sdl pad {jid} reconnected - resumed its held gamepad slot")
        return pad

    def _sweep_parked_gamepads(self, now=None):
        """Free parked devices whose controller never came back."""
        if not self._sdl_parked:
            if self._primary_hold and (now or time.monotonic()) > self._primary_hold[1]:
                self._primary_hold = None
            return
        now = now or time.monotonic()
        for uid, (pad, expiry) in list(self._sdl_parked.items()):
            if now > expiry:
                del self._sdl_parked[uid]
                try:
                    pad.close()
                except Exception:
                    pass
                print(f"sdl pad slot released - {uid} did not return")
        if self._primary_hold and now > self._primary_hold[1]:
            self._primary_hold = None

    def _may_claim_primary(self, jid):
        """May this controller take the free persistent pad (player 1)?

        No, while that slot is still reserved for a DIFFERENT controller that
        dropped moments ago - otherwise a second pad walks into player 1 during
        a Nintendo Bluetooth dropout and the two swap places when the first one
        returns. The reserved controller itself is of course welcome back."""
        hold = self._primary_hold
        if hold is None:
            return True
        uid, expiry = hold
        if time.monotonic() > expiry:
            self._primary_hold = None
            return True
        return uid == self._pad_uid(jid)

    def _ensure_sdl_gamepad(self, jid):
        """Get/create the dedicated ViGEm pad for SDL instance `jid`, wiring its
        game force-feedback back to that ONE physical controller. Returns the
        pad, or None if ViGEm is unavailable."""
        pad = self._sdl_gamepads.get(jid)
        if pad is not None:
            return pad
        # A controller that just came back from a dropout resumes the device it
        # already had - the game never saw it leave.
        pad = self._claim_parked_gamepad(jid)
        if pad is None:
            try:
                pad = VirtualGamepad()
            except ViGEmUnavailable as e:
                print(f"separate-xinput pad for {jid} unavailable: {e}")
                return None
        # Route THIS pad's force-feedback to only this physical pad (by id).
        # Re-registered on a resumed pad too: the reconnected controller has a
        # NEW SDL instance id, and the old closure would rumble a pad that no
        # longer exists.
        src = self._sdl_source

        def _rumble(large, small, _jid=jid, _src=src):
            # Gate on THIS pad kind's "Haptics Gamepad Mode" toggle (each
            # controller's Options category has its own).
            _kind = _src.kind_of(_jid) if _src is not None else "switch"
            if not self.settings.get(
                    pads.setting_key(_kind, "rumble_gamepad"), True):
                return
            if _src is not None:
                try:
                    _src.set_rumble_pad(_jid, large, small)
                except Exception:
                    pass

        try:
            pad.register_rumble(_rumble)
        except Exception:
            pass
        self._sdl_gamepads[jid] = pad
        return pad

    def _release_parked_gamepads(self):
        """Drop every held-for-a-dropout device (gamepad mode ended / exit /
        the hold turned off in Options - the last of which is the one caller
        that isn't sdl_gamepad_thread; the dict swap is why that's safe)."""
        parked = self._sdl_parked
        self._sdl_parked = {}
        self._primary_hold = None
        for pad, _expiry in parked.values():
            try:
                pad.close()
            except Exception:
                pass

    def _close_sdl_gamepads(self, park=False):
        """Free every per-controller SDL pad (multiplayer mode off / paused).

        `park=True` means the controllers VANISHED rather than the mode ending
        - the last pad dropping off Bluetooth lands here - so their virtual
        devices are held for the dropout grace instead of destroyed. Parked
        devices from an earlier drop are always released when park=False: the
        user left gamepad mode, so nothing is waiting for them."""
        pads = self._sdl_gamepads
        self._sdl_gamepads = {}
        for jid, pad in pads.items():
            if park:
                self._park_sdl_gamepad(jid, pad)
                continue
            try:
                pad.close()
            except Exception:
                pass
        if not park:
            self._release_parked_gamepads()
        # Advanced-press engines/edge state are per-pad-session: the desktop
        # handoff's desktop.reset() already released their held keys, so a
        # stale prev-dict would only suppress the next session's first press.
        self._sdl_adv_engines.clear()
        self._sdl_adv_prev.clear()
        self._sdl_gyro_stick.clear()

    def _reset_sdl_gamepads(self):
        """Zero every per-controller SDL pad WITHOUT freeing it (e.g. while the
        OSK temporarily owns the pad) so no input sticks, then they resume."""
        for pad in list(self._sdl_gamepads.values()):
            try:
                pad.reset()
            except Exception:
                pass

    def _feed_sdl_gamepads(self, frames, sc_live, suppress=0):
        """Automatic multiplayer: drive one XInput pad per connected SDL
        controller from the given per-pad frames. The FIRST controller to appear
        while no Steam Controller owns the persistent pad inherits it as player 1
        (so a lone controller never spawns a 2nd phantom device); every other
        controller gets its OWN dedicated pad, created on connect and PARKED on
        disconnect (held for its controller through a wireless dropout, see
        _park_sdl_gamepad)  any number, any mix. A pad whose OWN Home/"..." is held is
        driving the desktop (mouse/chords), so its XInput output is paused: Home
        never leaks through as the Guide button and the held sticks stay out of
        that game.

        Pad assignment is STICKY: a controller keeps whatever virtual device it
        already has and is NEVER reshuffled by a transient change in `sc_live`.
        That is what stops the XInput pad disconnecting/reconnecting every time
        the OSK is toggled  opening the OSK kicks the Steam Controller and it
        takes ~1 s to rebuild, during which sc_live briefly reads False; an
        already-assigned SDL pad must NOT grab the persistent pad in that gap and
        then hand it straight back. Only a genuine SC connect migrates a pad.

        `suppress` is an OR-mask of bits currently consumed by a gamepad-scoped
        Hotkeys chord  masked out of every pad's frame so the chord's trigger
        buttons don't ALSO reach the game."""
        _HOME = SCButtons.STEAM | SCButtons.QAM
        # A live Steam Controller owns the persistent pad (player 1, fed by the
        # launcher). If an SDL pad had been using it as player 1, give it its OWN
        # pad instead  a one-time migration on a genuine SC connect. (An OSK
        # toggle never triggers this mid-rebuild: the thread cedes the pad while
        # _kbd_open, so sc_live only reads True here once the SC is fully back.)
        if sc_live and self._primary_sdl_jid is not None:
            self._primary_sdl_jid = None
        primary = self._primary_sdl_jid
        # If the player-1 SDL pad disconnected, release the slot so the next pad
        # to appear can inherit it - but RESERVE it for that same controller for
        # the dropout grace first (a Nintendo pad drops off Bluetooth every ~20
        # minutes and comes straight back; player 1 must still be player 1), and
        # zero the persistent pad so nothing it was holding stays held.
        if primary is not None and primary not in frames:
            if self._hold_slot_enabled():
                self._primary_hold = (self._pad_uid(primary),
                                      time.monotonic() + self._PAD_DROPOUT_GRACE)
                print(f"sdl pad {primary} dropped - reserving player 1 for "
                      f"{self._PAD_DROPOUT_GRACE:.0f}s")
            if self._persistent_gamepad is not None:
                try:
                    self._persistent_gamepad.reset()
                except Exception:
                    pass
            primary = None
            self._primary_sdl_jid = None
        # Park dedicated pads whose controller disconnected (see
        # _park_sdl_gamepad - the device stays open for its controller).
        for jid in list(self._sdl_gamepads):
            if jid not in frames:
                self._park_sdl_gamepad(jid, self._sdl_gamepads.pop(jid))
        self._sweep_parked_gamepads()
        # Feed each live controller. STICKY: keep the pad it already owns; only a
        # brand-new controller is assigned (the free persistent pad if available,
        # else its own). Pause whichever pad is holding its Home.
        for jid, f in frames.items():
            if jid == primary:
                pad = self._persistent_gamepad
            elif jid in self._sdl_gamepads:
                pad = self._sdl_gamepads[jid]
            elif (primary is None and not sc_live
                    and self._persistent_gamepad is not None
                    and self._may_claim_primary(jid)):
                # Persistent pad is free → this new controller becomes player 1
                # (so a lone pad doesn't spawn a 2nd phantom XInput device).
                primary = jid
                self._primary_sdl_jid = jid
                self._primary_hold = None
                pad = self._persistent_gamepad
            else:
                pad = self._ensure_sdl_gamepad(jid)
            if pad is None:
                continue
            if f.buttons & _HOME:
                try:
                    pad.reset()
                except Exception:
                    pass
            else:
                try:
                    if suppress:
                        f = f._replace(buttons=f.buttons & ~suppress)
                    self._feed_one_sdl_pad(jid, pad, f)
                except Exception as e:
                    print(f"sdl gamepad update failed for {jid}: {e!r}")

    def _sdl_gp_conf(self, kind, pad):
        """Cached gamepad-mode remap config for a controller kind: the picker's
        Gamepad-tab binds resolved into a compiled XUSB button_map, trigger
        analog gates, stick direction→flag maps and desktop/keyboard action
        overrides (the Gamepad tab offers the merged vocabulary on every kind).
        Invalidated (cache cleared) whenever keybinds save/profile switch."""
        conf = self._sdl_gp_cache.get(kind)
        if conf is not None:
            return conf
        gp = keybinds_runtime.gamepad_submap(
            (self.settings.get("keybinds") or {}).get(kind) or {})
        bmap, lt, rt = keybinds_runtime.resolve_sdl_gamepad(gp, SCButtons)
        compiled = None
        try:
            compiled = pad.compile_button_map(bmap)
        except Exception as e:
            print(f"sdl gamepad button-map compile failed ({kind}): {e!r}")
        lmap, rmap = keybinds_runtime.resolve_sdl_gamepad_sticks(gp)
        lflags = rflags = None
        try:
            if lmap:
                lflags = {z: pad.action_flag(a) for z, a in lmap.items()}
                lflags = {z: fl for z, fl in lflags.items() if fl} or None
            if rmap:
                rflags = {z: pad.action_flag(a) for z, a in rmap.items()}
                rflags = {z: fl for z, fl in rflags.items() if fl} or None
        except Exception:
            lflags = rflags = None
        keys = keybinds_runtime.resolve_sdl_gamepad_keys(
            gp, SCButtons, sui.Keys)
        # Advanced press rows (Long/Double/Soft): per-kind engine CONFIG here;
        # each pad (jid) gets its own AdvPressEngine instance lazily in
        # _feed_one_sdl_pad (press timing is per physical pad). Owned bits
        # leave the plain map/keys tables so a control never double-acts.
        adv_c, adv_s, adv_sh, adv_p, adv_owned = \
            keybinds_runtime.resolve_adv_config(gp, kind, SCButtons, sui.Keys)
        if adv_owned:
            if compiled:
                compiled = [(b, f) for b, f in compiled if not (b & adv_owned)]
            keys = [(c, b, a) for c, b, a in keys if not (b & adv_owned)]
        conf = {
            "map": compiled, "lt": lt, "rt": rt,
            "lflags": lflags, "rflags": rflags,
            "keys": keys,
            "adv": ((adv_c, adv_s, adv_sh, adv_p)
                    if (adv_c or adv_s or adv_sh or adv_p) else None),
            # This kind's tuned press thresholds, cached with the config so
            # every pad built from it shares them (see _apply_general_setting_
            # locked, which drops this cache when they change).
            "adv_timing": keybinds_runtime.adv_timing(self.settings, kind),
            # Guide (Home) bits  rows on the Guide button run alongside the
            # chord layer instead of taking the button over.
            "adv_guide": keybinds_runtime.adv_guide_mask(kind, SCButtons),
        }
        self._sdl_gp_cache[kind] = conf
        return conf

    # Stick deflection past this counts as a digital-zone press in gamepad mode
    # (mirrors the SC watcher's STICK_DEADZONE for its gamepad stick maps).
    _SDL_GP_STICK_DEADZONE = 14000

    def _feed_one_sdl_pad(self, jid, pad, f):
        """Push one physical pad's frame to its virtual XInput device, honoring
        that pad KIND's Gamepad-tab remap: compiled button_map, trigger analog
        gates, digital stick zones, held Button-Combo Xbox outputs, and
        keyboard-action overrides (injected as desktop keys while gaming)."""
        src = self._sdl_source
        kind = src.kind_of(jid) if src is not None else "switch"
        conf = self._sdl_gp_conf(kind, pad)
        d = self._sdl_desktop
        extra = 0
        fbuttons = f.buttons
        # Button Combos (gamepad-scoped + guide-scoped): OR the held combos'
        # Xbox outputs into this pad while the trigger mask is held; suppress
        # the trigger bits so they don't ALSO emit their own mapped outputs.
        if d is not None and d._button_combos:
            st = self._sdl_combo_state.setdefault(jid, {})
            for i, (mask, is_gp, xbox_ids, key_actions, guide) in enumerate(
                    d._button_combos):
                if guide:
                    continue    # guide combos ride the Home-hold path
                active = (fbuttons & mask) == mask
                if active:
                    for out_id in xbox_ids:
                        try:
                            extra |= pad.action_flag(out_id) or 0
                        except Exception:
                            pass
                    fbuttons &= ~mask
                    if not st.get(i, False):
                        for j, act in enumerate(key_actions):
                            d._combo_hold(i, j, act, True, mode="gamepad")
                elif st.get(i, False):
                    for j, act in enumerate(key_actions):
                        d._combo_hold(i, j, act, False, mode="gamepad")
                st[i] = active
        # Digital stick zones (Gamepad tab stick directions bound to outputs).
        lz = conf["lflags"] is not None
        rz = conf["rflags"] is not None
        if lz:
            lx, ly = f.lstick_x, f.lstick_y
            if (abs(lx) > self._SDL_GP_STICK_DEADZONE
                    or abs(ly) > self._SDL_GP_STICK_DEADZONE):
                zone = ("UP" if ly > 0 else "DOWN") if abs(ly) >= abs(lx) \
                    else ("RIGHT" if lx > 0 else "LEFT")
                extra |= conf["lflags"].get(zone, 0)
        if rz:
            rx, ry = f.rstick_x, f.rstick_y
            if (abs(rx) > self._SDL_GP_STICK_DEADZONE
                    or abs(ry) > self._SDL_GP_STICK_DEADZONE):
                zone = ("UP" if ry > 0 else "DOWN") if abs(ry) >= abs(rx) \
                    else ("RIGHT" if rx > 0 else "LEFT")
                extra |= conf["rflags"].get(zone, 0)
        # Advanced press rows: this pad's engine decides Long/Double/Soft
        # presses; owned bits leave the frame, asserted xusb specs join
        # `extra`, key specs inject via the shared desktop controller.
        if conf["adv"] is not None:
            ent = self._sdl_adv_engines.get(jid)
            if ent is None or ent[0] is not conf:
                ent = (conf, keybinds_runtime.AdvPressEngine(
                    *conf["adv"], timing=conf.get("adv_timing"),
                    guide_bit=conf.get("adv_guide", 0)))
                self._sdl_adv_engines[jid] = ent
            eng = ent[1]
            asserted = eng.step(fbuttons, f.ltrig, f.rtrig, time.monotonic())
            prev = self._sdl_adv_prev.setdefault(jid, {})
            for slot, spec in asserted.items():
                if spec[0] == "xusb":
                    try:
                        extra |= pad.action_flag(spec[1]) or 0
                    except Exception:
                        pass
                elif slot not in prev and d is not None:
                    action = spec[1]
                    holder = "adv:%s:%s" % (jid, slot)
                    if action[0] == "click":
                        d._set_click(holder, action[1], True)
                    elif action[0] == "hold":
                        d._set_key(holder, action[1], True, kind=kind)
                    elif action[0] != "none":
                        d._fire_action(action, kind=kind, mode="gamepad")
            if d is not None:
                for slot, spec in prev.items():
                    if slot in asserted or spec[0] != "key":
                        continue
                    action = spec[1]
                    holder = "adv:%s:%s" % (jid, slot)
                    if action[0] == "click":
                        d._set_click(holder, action[1], False)
                    elif action[0] == "hold":
                        d._set_key(holder, action[1], False, kind=kind)
            self._sdl_adv_prev[jid] = asserted
            if eng.frame_mask:
                fbuttons &= ~eng.frame_mask
        if fbuttons != f.buttons:
            f = f._replace(buttons=fbuttons)
        pad.update(f, conf["map"], conf["lt"], conf["rt"], extra, lz, rz,
                   rstick_add=self._sdl_gyro_stick.get(jid))
        # Gamepad-tab controls bound to keyboard/mouse/system actions: inject
        # while gaming (edge-fired / held via the shared desktop controller).
        if d is not None and conf["keys"]:
            st = self._sdl_gpk_state.setdefault(jid, {})
            for cid, bit, action in conf["keys"]:
                pressed = bool(f.buttons & bit)
                typ = action[0]
                if typ == "click":
                    d._set_click("gpk:%s:%s" % (jid, cid), action[1], pressed)
                elif typ == "hold":
                    d._set_key("gpk:%s:%s" % (jid, cid), action[1], pressed,
                               kind=kind)
                else:
                    if pressed and not st.get(cid, False):
                        d._fire_action(action, kind=kind, mode="gamepad")
                    st[cid] = pressed

    def _sdl_pc_adv_step(self, d, kind, sci):
        """DESKTOP-mode Advanced Presses for the active SDL pad: step `kind`'s
        pc engine (built lazily from its Desktop-tab "__adv" rows), inject
        key specs through the shared desktop controller, and return the
        frame with the engine-owned bits (and owned triggers' analog)
        masked so the bind-driven desktop dispatch defers to the engine.
        Disabled while Home/"..." is held (the guide layer owns chords)."""
        eng = self._sdl_pc_adv.get(kind)
        if eng is None:
            gpc = keybinds_runtime.pc_submap(
                (self.settings.get("keybinds") or {}).get(kind) or {})
            c, s, sh, p, _o = keybinds_runtime.resolve_adv_config(
                gpc, kind, SCButtons, sui.Keys, mode="pc")
            eng = (keybinds_runtime.AdvPressEngine(
                       c, s, sh, p,
                       timing=keybinds_runtime.adv_timing(self.settings, kind),
                       guide_bit=keybinds_runtime.adv_guide_mask(
                           kind, SCButtons))
                   if (c or s or sh or p) else False)
            self._sdl_pc_adv[kind] = eng
        if eng is False:
            return sci
        guide = bool(sci.buttons & (SCButtons.STEAM | SCButtons.QAM))
        asserted = eng.step(sci.buttons, sci.ltrig, sci.rtrig,
                            time.monotonic(), enabled=not guide)
        prev = self._sdl_pc_adv_prev
        for slot, spec in asserted.items():
            if spec[0] != "key" or slot in prev:
                continue
            action = spec[1]
            holder = "advpc:" + slot
            if action[0] == "click":
                d._set_click(holder, action[1], True)
            elif action[0] == "hold":
                d._set_key(holder, action[1], True, kind=kind)
            elif action[0] != "none":
                d._fire_action(action, kind=kind, mode="pc")
        for slot, spec in prev.items():
            if slot in asserted or spec[0] != "key":
                continue
            action = spec[1]
            holder = "advpc:" + slot
            if action[0] == "click":
                d._set_click(holder, action[1], False)
            elif action[0] == "hold":
                d._set_key(holder, action[1], False, kind=kind)
        self._sdl_pc_adv_prev = asserted
        m = eng.frame_mask
        if m:
            repl = {"buttons": sci.buttons & ~m}
            if m & SCButtons.LT:
                repl["ltrig"] = 0
            if m & SCButtons.RT:
                repl["rtrig"] = 0
            sci = sci._replace(**repl)
        return sci

    def _set_sdl_hi_res(self, want):
        """Hold/drop a process high-responsiveness request (adusk_power) from the
        SDL thread so a live SDL pad driving the DESKTOP mouse (OSK closed) keeps
        the 1 ms timer + EcoQoS opt-out it needs to stay smooth, while a desktop
        with NO pad connected lets the process fall back to true background power.
        Reference-counted in adusk_power, so this composes with the OSK loop's own
        request. Call only from sdl_gamepad_thread (boost targets THIS thread)."""
        if want == self._sdl_hi_res:
            return
        self._sdl_hi_res = want
        if want:
            adusk_power.request()
            adusk_power.boost_current_thread()
        else:
            adusk_power.unboost_current_thread()
            adusk_power.release()

    def sdl_gamepad_thread(self):
        """Poll SDL-recognized pads (Xbox/DualSense/Switch Pro/8BitDo/...) so a
        non-Steam controller can (a) open the OSK with Guide+X, (b) feed the
        ViGEm virtual pad in gamepad mode, and (c) act as a desktop mouse/keyboard
        otherwise (the synthesized equivalent of the Steam Controller's firmware
        lizard mode). The Steam Controller is handled by launcher_thread and is
        excluded by Sdl3GamepadSource (name match), so the two never fight.
        Defensive throughout  any error here must never take down the tray."""
        src = self._sdl_source
        if src is None:
            return
        guide_x_prev = False
        # force_kill = Home+B → force-shutdown the foreground game and its
        # children (the SDL-pad equivalent of the SC's Steam+B chord).
        desktop = _SdlDesktopController(
            force_kill=_force_kill_foreground_game,
            binds=self.settings.get("keybinds", {}).get("switch"),
            chords=keybinds_runtime.chords_for(
                self.settings.get("chords", []), "switch"),
            on_profile_cycle=self.cycle_keybind_profile,
            on_toggle_gui=self.toggle_config_gui,
            # Light desktop mouse-click weight; the source itself skips kinds
            # without analog triggers (Switch) and honors the Haptics toggle.
            trigger_haptic=lambda: src.haptic_trigger_click(strong=False))
        self._sdl_desktop = desktop  # exposed so _save_keybinds re-applies live
        self._sdl_close_bits = desktop.close_bits
        # Controller kind whose Desktop-tab keybinds are currently loaded into
        # `desktop`  swapped live to follow the active pad (see the loop).
        desktop_binds_kind = "switch"
        self._sdl_desktop_kind = lambda: desktop_binds_kind
        _was_kbd_open = False
        _osk_close_time = 0.0   # monotonic time of last OSK close (for debounce)
        _OSK_REOPEN_COOLDOWN = 0.4  # seconds to ignore Y presses after OSK closes
        _sdl_toggle_latch = False    # SDL-side gamepad-toggle chord latch
        # (the "Gyro To Mouse" chord needs no local latch  gyro_action_hold
        #  owns the once-per-press edge for every source of that action)
        # Gyro-to-mouse integrator for the SDL pads (SDL sensor API  rad/s).
        _gyro_mouse = _GyroMouse(desktop._mouse.move)
        _RAD_TO_DEG = 180.0 / math.pi
        _guide_layer_prev = False    # gamepad-mode Home-hold state (for release)
        _ga_prev = None              # last gamepad-mode state (for the toggle rumble)
        _steam_kill_prev = False     # Home+face edge while Steam-ceded (force-kill)
        idle_polls = 0               # consecutive idle polls (drives the backoff)
        last_pad_active = 0.0        # monotonic time the pad last had real input
        _PAD_IDLE_GRACE = 0.6        # hold the fast poll this long after last input
        while not self._stop_event.is_set():
            # Paused for Steam (disable_while_steam_running + Steam up): let
            # Steam own the controllers. Don't inject desktop kb/mouse or feed
            # ViGEm from the SDL pad  the launcher pauses the Steam Controller
            # the same way. Without this, the SDL pad kept driving desktop
            # mouse/keyboard into the Steam game.
            if self._steam_active.is_set():
                # Paused for Steam → cede the pads; drop any responsiveness hold
                # (the launcher/OSK aren't running either, so the process can be
                # true background while Steam owns the controllers).
                self._set_sdl_hi_res(False)
                desktop.reset()
                self._close_sdl_gamepads()  # let Steam own the pads
                guide_x_prev = False
                # BUT still honor Home+B force-shutdown  its whole purpose is to
                # kill a running (often Steam) game, which is exactly when we're
                # ceded. Nothing else is injected. (Skip while the OSK is open so
                # we don't double-poll the pad against adusk.)
                sci = None
                if not self._kbd_open:
                    try:
                        sci = src.poll()
                    except Exception:
                        sci = None
                kill_now = bool(sci is not None
                                and (sci.buttons & (SCButtons.STEAM | SCButtons.QAM))
                                and (sci.buttons & SCButtons.B))  # Home + Switch A
                if kill_now and not _steam_kill_prev:
                    try:
                        _force_kill_foreground_game()
                    except Exception:
                        pass
                _steam_kill_prev = kill_now
                # 10 Hz is plenty here: the only job is edge-detecting a
                # deliberately HELD Home+B chord, and this branch runs for
                # entire gaming sessions  don't wake 20x/sec for it.
                self._stop_event.wait(0.1)
                continue
            _steam_kill_prev = False
            # While the OSK is open, adusk.main owns the pad: it polls it on its
            # own SDL event-pump thread and publishes frames (SDL only refreshes
            # gamepad state on the thread pumping its events, so polling here
            # would read all-zero). Cede the pad until the OSK closes.
            if self._kbd_open:
                _was_kbd_open = True
                desktop.reset()
                self._reset_sdl_gamepads()  # OSK owns the pad; no stuck input
                guide_x_prev = True  # treat Y as "held" so release doesn't re-open
                # While the OSK consumes the LT/RT digital bits, each pad's
                # per-kind "Keyboard Trigger Actuation" applies.
                src.trigger_mode = "osk"
                # Nothing to do until the OSK closes  a slow tick is plenty
                # (the 0.4 s reopen cooldown dwarfs the ≤0.1 s resume lag).
                self._stop_event.wait(0.1)
                continue
            # Record the moment the OSK just closed so the cooldown can gate
            # re-opens  prevents buffered Y presses during close from firing.
            if _was_kbd_open:
                _was_kbd_open = False
                _osk_close_time = time.monotonic()
                guide_x_prev = True  # force a clean rising-edge on the next press
            # Light the controller's LED (blue) while gamepad mode is active,
            # off otherwise. set_home_led only flags the change; the SDL pump
            # applies it on this (SDL) thread. On the gamepad-mode TRANSITION,
            # buzz a two-pulse confirmation (light→strong on, strong→light off);
            # _ga_prev=None on the first pass so startup doesn't rumble.
            ga = self._gamepad_active
            if _ga_prev is not None and ga != _ga_prev:
                src.play_mode_rumble(ga)
            _ga_prev = ga
            src.set_home_led(ga)
            # Which per-kind trigger-actuation family the LT/RT digital bits
            # honor right now: "gamepad" while feeding the virtual pad, else
            # "mouse" (desktop ZL/ZR clicks). The OSK branch above sets "osk".
            src.trigger_mode = "gamepad" if ga else "mouse"
            # ONE pump → the OR-merged frame (drives OSK-open detection) AND a
            # per-pad dict {jid: frame} (drives one dedicated XInput pad per
            # physical controller  automatic multiplayer, no toggle needed).
            try:
                sci, frames = src.poll_all()
            except Exception as e:
                print(f"sdl gamepad poll error: {e!r}")
                sci, frames = None, {}
            # A pad just dropped off Bluetooth? Say so once per session - a
            # Nintendo pad doing it every ~20 minutes is firmware, not us, and
            # the user deserves to know that (and how to avoid it) instead of
            # blaming the app.
            try:
                for _jid, _uid, _dkind, _guarded in src.take_drop_events():
                    if _guarded and not self._nintendo_drop_notified:
                        self._nintendo_drop_notified = True
                        self._notify(
                            "Switch controller disconnected",
                            "Nintendo's Bluetooth firmware drops the link every "
                            "~20 min on any non-Switch host. It should reconnect "
                            "on its own"
                            + (" - its gamepad slot is held open so games keep "
                               "working." if self._hold_slot_enabled() else ".")
                            + " Connect over USB-C to avoid it entirely.")
            except Exception:
                pass
            # New controller kind? Permanently unlock its picker tab + Options
            # category (cheap: set lookup per connected pad).
            for _kind in src._pad_kinds.values():
                if _kind not in self._seen_kind_cache:
                    self._note_seen_controller(_kind)
            # Keybinds picker controller navigation: EVERY controller steers
            # the picker, not just the Steam Controller. Publish the raw
            # merged frame for the picker's nav pump, then  while the picker
            # is visible + foreground  mask the navigation buttons out of
            # everything below (desktop dispatch AND the per-pad XInput
            # frames) so highlighting a row doesn't also type/click/fire
            # in-game input.
            if sci is not None:
                sc_viewer.publish_nav(sci, src.active_kind())
                if sc_viewer.nav_claimed():
                    if sc_viewer.listen_claimed():
                        # Listen bind-capture: swallow EVERY press so the
                        # captured button can't also fire desktop/XInput.
                        sci = sci._replace(
                            buttons=sci.buttons & _PICKER_LISTEN_KEEP)
                        frames = {j: f._replace(
                                      buttons=f.buttons & _PICKER_LISTEN_KEEP)
                                  for j, f in frames.items()}
                    elif sc_viewer.tutorial_claimed():
                        # First-run tour: allow-list, no guide exemption  see
                        # the matching branch in the SC watcher.
                        _tkeep = sc_viewer.nav_keep() | _PICKER_LISTEN_KEEP
                        sci = sci._replace(buttons=sci.buttons & _tkeep)
                        frames = {j: f._replace(buttons=f.buttons & _tkeep)
                                  for j, f in frames.items()}
                    elif not (sci.buttons & _GUIDE_BITS):
                        # Home/Guide-held frames are exempt (see _GUIDE_BITS):
                        # the picker navigates on bare buttons only, so a
                        # Home+button chord is normal dispatch  and masking it
                        # made the Home TAP detector see a clean tap and fire
                        # "Toggle Config GUI".
                        # ...and so is anything the picker asks us to spare 
                        # the tour's keyboard slide teaching the bare-X open
                        # (see sc_viewer.set_nav_keep).
                        _pmask = _PICKER_NAV_MASK & ~sc_viewer.nav_keep()
                        sci = sci._replace(
                            buttons=sci.buttons & ~_pmask)
                        frames = {j: f._replace(
                                      buttons=f.buttons & ~_pmask)
                                  for j, f in frames.items()}
            # The desktop/guide/chord tables follow the ACTIVE pad's kind, so
            # each controller family gets its own Desktop/Chords/Hotkeys binds.
            _ak = src.active_kind()
            if _ak != desktop_binds_kind:
                desktop_binds_kind = _ak
                try:
                    desktop.apply_binds(
                        self.settings.get("keybinds", {}).get(_ak),
                        keybinds_runtime.chords_for(
                            self.settings.get("chords", []), _ak),
                        kind=_ak)
                    self._sdl_close_bits = desktop.close_bits
                except Exception:
                    pass
            # Hotkeys "Gamepad Mode Toggle" chords from the active kind: fire
            # once per press in BOTH modes (own latch, separate from the SC's).
            if desktop.gamepad_toggle_masks and sci is not None:
                _t = any((sci.buttons & m) == m
                         for m in desktop.gamepad_toggle_masks)
                if _t and not _sdl_toggle_latch:
                    _sdl_toggle_latch = True
                    try:
                        self._do_toggle_gamepad_mode()
                    except Exception:
                        pass
                elif not _t:
                    _sdl_toggle_latch = False
            # Built-in "hold + / Options / ≡ to switch Desktop <-> Gamepad"
            # gesture  the SDL twin of the HID watcher's, on its own detector
            # (the two paths see different frames). The bits it returns are
            # stripped from BOTH the merged frame and every per-pad frame, so
            # the press that switched modes doesn't also reach the desktop
            # binds or the per-controller virtual pads on its way out.
            if sci is not None:
                _h_mask = self.handle_mode_hold(sci.buttons, sdl=True)
                if _h_mask:
                    sci = sci._replace(buttons=sci.buttons & ~_h_mask)
                    frames = {j: f._replace(buttons=f.buttons & ~_h_mask)
                              for j, f in frames.items()}
            # "Gyro To Mouse" hotkey chords from the active kind  MODE-aware
            # (Enable/Suppress/Toggle); "toggle" uses the same once-per-press
            # latch pattern as the gamepad-mode toggle above.
            if desktop.gyro_toggle_masks and sci is not None:
                gyro_action_hold(_ak, "chord",
                                 any((sci.buttons & m) == m
                                     for m in desktop.gyro_toggle_masks))
            # Gyro-to-mouse drive: stream the gyro on exactly the pads whose
            # kind has it toggled on (per-pad SDL sensor enable  costs
            # nothing while off) and turn their angular velocity into cursor
            # motion. Runs in BOTH modes: in gamepad mode it's a gyro aim on
            # top of the virtual pad, like Steam Input's gyro mouse.
            _gyro_moved = False
            _gyro_kinds = {k for k in set(src._pad_kinds.values())
                           if adusk_state.is_gyro_mouse_active(k)
                           and pads.has_gyro(k)}
            src.set_gyro_kinds(_gyro_kinds)
            self._sdl_gyro_stick.clear()
            if _gyro_kinds:
                _gnow = time.monotonic()
                for _jid, _gk, _gx, _gy, _gz in src.read_gyro():
                    # SDL gyro is rad/s in the same axis convention as the
                    # HID path after its swizzle: X = pitch, Y = yaw. (With
                    # several gyro pads live the shared integrator's dt gate
                    # lets one pad drive per tick  they don't sum.)
                    _yaw = _gy * _RAD_TO_DEG
                    _pitch = _gx * _RAD_TO_DEG
                    if (self._gamepad_active
                            and adusk_state.get_gyro_output(_gk) == "rstick"):
                        # Gyro → right stick on this pad's own virtual Xbox
                        # device (Steam's "Gyro To Joystick"); shaped by the
                        # same deadzone/precision/accel curves. Consumed by
                        # _feed_one_sdl_pad below this same tick.
                        _yaw, _pitch = adusk_state.gyro_shape(_gk, _yaw, _pitch)
                        if _yaw or _pitch:
                            _k2 = adusk_state.get_gyro_stick_gain(_gk)
                            self._sdl_gyro_stick[_jid] = (
                                int(-_yaw * _k2), int(_pitch * _k2))
                            _gyro_moved = True
                    elif _gyro_mouse.feed(_yaw, _pitch, _gnow, _gk):
                        _gyro_moved = True
            # A guide-bound (or Chords-tab) "show_keyboard" action requested an
            # OSK open  honor it with the same gating as the button path.
            if desktop.open_request:
                desktop.open_request = False
                if (not self._kbd_open and not _workstation_locked()
                        and (time.monotonic() - _osk_close_time)
                        > _OSK_REOPEN_COOLDOWN):
                    self.toggle_keyboard_hotkey(opener=src.active_kind())
            # A connected pad only needs the fast poll + high-responsiveness
            # while it's actually IN USE (any input) or feeding a game  a
            # connected-but-untouched pad, or our own idle ViGEm pad that SDL
            # surfaces in auto mode, must not pin the process at 125 Hz. The grace
            # window snaps back instantly on the first input.
            now_m = time.monotonic()
            if self._gamepad_active or _gyro_moved or (
                    sci is not None and adusk_inputsrc._frame_has_activity(sci)):
                last_pad_active = now_m
            pad_busy = sci is not None and (now_m - last_pad_active) < _PAD_IDLE_GRACE
            self._set_sdl_hi_res(pad_busy)
            if sci is not None:
                # A pad frame means a Switch Pro / SDL pad is connected  latch
                # it so the "Switch Pro Controller" tray submenu appears.
                if not self._switch_ever_connected:
                    self._switch_ever_connected = True
                # OSK-open buttons follow the Desktop-tab binding (controls
                # bound to "Show Keyboard"  the positional X by default; on
                # the Switch Pro that's physical Y). In DESKTOP mode a bare
                # press opens (rising edge); in GAMEPAD mode the button is a
                # face button the game needs, so the OSK only opens on
                # Steam(Home)+button  matching the Steam Controller. Cooldown
                # after OSK close prevents buffered presses from re-opening.
                x = bool(sci.buttons & desktop.open_bits)
                steam = bool(sci.buttons & SCButtons.STEAM)
                x_opens = x and (not self._gamepad_active or steam)
                if (x_opens and not guide_x_prev and not self._kbd_open
                        and not _workstation_locked()
                        and (time.monotonic() - _osk_close_time) > _OSK_REOPEN_COOLDOWN):
                    # An SDL pad opened it → start on that pad family's glyphs.
                    self.toggle_keyboard_hotkey(opener=src.active_kind())
                guide_x_prev = x_opens
                _sc = self._current_sc
                _sc_live = _sc is not None and _sc.is_live()
                if self._gamepad_active:
                    # Gamepad mode → automatic multiplayer: every connected SDL
                    # pad drives its OWN dedicated XInput device (the first reuses
                    # the persistent pad; see _feed_sdl_gamepads), so any number /
                    # mix of controllers each become a separate player.
                    #
                    # The single human desktop user's layer still runs, driven by
                    # whichever pad is holding its Home/"..." (its OWN frame, not
                    # the merge, so other players' sticks don't reach the cursor):
                    #   • Hold Home → mouse mode (right stick = cursor, ZR/ZL =
                    #     click) + Steam chords (media / play-pause / Alt+Tab /
                    #     force-kill on left stick + L3 + "+" + B).
                    # The pad whose Home is held has its OWN XInput output paused
                    # inside _feed_sdl_gamepads, so Home never leaks through as the
                    # Guide button and the held sticks don't reach that game.
                    _now = time.monotonic()
                    home_frame = None
                    for _f in frames.values():
                        if _f.buttons & (SCButtons.STEAM | SCButtons.QAM):
                            home_frame = _f
                            break
                    if home_frame is not None:
                        desktop.update_mouse_only(home_frame, _now)
                        desktop.handle_guide_layer(home_frame, _now)
                        _guide_layer_prev = True
                    else:
                        if _guide_layer_prev:
                            desktop.guide_release()  # drop held Alt + edges
                            _guide_layer_prev = False
                        desktop.reset()  # release any click held during the hold
                    # Home TAP (clean short press, no chord) → the gamepad-tab
                    # Home binding (default: toggle the config GUI). Tracked on
                    # the physical Home/STEAM bit across held+released frames so
                    # the falling edge is seen even once home_frame goes away.
                    _hf = next((_f for _f in frames.values()
                                if _f.buttons & SCButtons.STEAM), None)
                    desktop._track_home_tap(
                        _hf.buttons if _hf is not None else 0, _now,
                        _hf is not None, desktop._home_tap_gp, mode="gamepad")
                    # Gamepad-scoped Hotkeys chords (green xi_ aliases): the
                    # desktop dispatch is idle in this mode, so fire them here
                    # and keep their buttons out of the games' pads.
                    _gp_chord_sup = desktop.handle_gamepad_chords(sci)
                    self._feed_sdl_gamepads(frames, _sc_live,
                                            suppress=_gp_chord_sup)
                else:
                    # Desktop mode: ALWAYS drive the mouse/keyboard from the SDL
                    # pad. A merely-connected Steam Controller dongle must NOT
                    # block this  gating it on the SC killed the Switch Pro
                    # mouse whenever the puck was plugged in. (If the physical
                    # Steam Controller is also actively driving its firmware
                    # lizard mouse, both move the cursor, but in practice only
                    # one controller is used at a time.)
                    if self._sdl_gamepads:
                        self._close_sdl_gamepads()  # no XInput off the desktop
                    self._primary_sdl_jid = None
                    try:
                        sci_d = self._sdl_pc_adv_step(
                            desktop, desktop_binds_kind, sci)
                        desktop.update(sci_d, time.monotonic())
                    except Exception as e:
                        print(f"sdl desktop update failed: {e!r}")
            else:
                guide_x_prev = False
                desktop.reset()
                if self._sdl_gamepads:
                    # All pads gone. In gamepad mode that is usually a WIRELESS
                    # DROPOUT (a Nintendo pad does it every ~20 min), so park
                    # their virtual devices for the controllers rather than
                    # destroying them under the game's feet.
                    self._close_sdl_gamepads(park=self._gamepad_active)
                if self._primary_sdl_jid is not None:
                    if self._gamepad_active and self._hold_slot_enabled():
                        self._primary_hold = (
                            self._pad_uid(self._primary_sdl_jid),
                            time.monotonic() + self._PAD_DROPOUT_GRACE)
                    if self._persistent_gamepad is not None:
                        try:
                            self._persistent_gamepad.reset()
                        except Exception:
                            pass
                self._primary_sdl_jid = None
                self._sweep_parked_gamepads()
            # Pace the poll. Full ~125 Hz only while the pad is busy (recent input
            # or feeding a game) for low latency; an idle pad (untouched, or our
            # own ViGEm pad) and a no-pad desktop both ramp down to ~30 Hz so the
            # thread isn't woken 125x/sec for nothing. The next input is caught
            # within one (≤33 ms) tick, which snaps the rate straight back up.
            if pad_busy:
                idle_polls = 0
                self._stop_event.wait(0.008)
            elif sci is None:
                # No pad connected at all (controller off  the common idle
                # case). Only SDL hotplug needs this pump, and the source
                # rescans on its own 0.5 s timer, so a newly-attached pad is
                # still caught within ~0.5 s no matter how slow this ticks 
                # halve the no-controller wakeups vs an idle-but-connected pad.
                idle_polls = 4
                self._stop_event.wait(0.066)
            else:
                idle_polls = min(idle_polls + 1, 4)
                self._stop_event.wait(min(0.033, 0.008 * idle_polls))
        # The tray is shutting down  drop any responsiveness hold we still have.
        self._set_sdl_hi_res(False)

    def exit_app(self, icon, item):
        self._stop_event.set()
        # Land any coalesced settings write before teardown starts, so a
        # setting changed a moment ago (or the reset that relaunches us) can
        # never be lost to the trailing timer never getting its turn.
        _flush_settings()
        # Stop every Tk finalizer talking to Tcl BEFORE any teardown below
        # runs. A GC pass on THIS thread that collects a leftover picker
        # PhotoImage would otherwise marshal an `image delete` to the picker's
        # Tk thread and wait on it forever (Tcl_ConditionWait, with the GIL
        # held)  which freezes the manager window into "not responding" and
        # hangs the exit for good. Only if the picker was ever imported: don't
        # drag tkinter in just to quit.
        _kp = sys.modules.get("keybinds_picker")
        if _kp is not None:
            try:
                _kp._disable_tk_finalizers()
            except Exception:
                pass
        # Wake any event-idle background threads so they observe the stop.
        self._auto_gamepad_wake.set()
        self._steam_watch_wake.set()
        self._launcher_wake.set()
        self._sc_extra_wake.set()
        # _osk_hotkey_hook_state (like _esc_hook_state) is a raw daemon thread
        # pumping a message loop  nothing to explicitly stop, it dies with
        # the process same as the Escape hook does.
        adusk_state.close()
        if self._current_sc is not None:
            try:
                self._current_sc.addExit()
            except Exception:
                pass
        # Player 2+ readers restore their own controller's lizard mode and close
        # its HID handle as they unwind, so ask them to stop here too.
        self._close_sc_extras()
        # Defensive: if exit happens mid-chord, make sure we don't leave
        # Alt held at the OS level.
        try:
            self._chord.release_alt()
        except Exception:
            pass
        self._close_persistent_gamepad()
        self._close_sdl_gamepads()
        if self._sdl_source is not None:
            try:
                self._sdl_source.close()
            except Exception:
                pass
        try:
            S.SDL_Quit()
        except Exception:
            pass
        # Tear the keybinds picker down on its own Tk thread (if it was opened),
        # so the Tk interpreter isn't finalized on the wrong thread at exit (Tcl
        # async-handler panic). No-op if the picker was never imported/opened.
        try:
            import keybinds_picker
            keybinds_picker.shutdown()
        except Exception:
            pass
        # Big Picture automation engine  plain thread stop (the Linux side
        # holds no system state to restore).
        try:
            self._bp_engine.stop()
        except Exception:
            pass
        # If an exit lands mid-scrub, don't strand the user with an invisible
        # pointer (SetSystemCursor is session-global). No-op when not hidden.
        _show_system_cursor()
        # Likewise don't leave the desktop pinch-zoomed (the transform would
        # auto-revert on process death anyway, but restore explicitly).
        _mag_reset()
        icon.stop()

    # background threads ----------------------------------------------------

    def _should_abort_sc(self):
        return self._stop_event.is_set() or self._steam_active.is_set()

    def _launcher_wait(self, timeout):
        """Backoff sleep for launcher_thread that also wakes early on a stop or
        an open-keyboard request (so Win+Ctrl+O is responsive even when no
        controller is attached and the loop is in its reconnect backoff)."""
        self._launcher_wake.wait(timeout)
        self._launcher_wake.clear()

    def _kbd_menu_label(self, item):
        """Dynamic label for the tray's top menu item: shows the action that a
        click will perform given the keyboard's current open/closed state."""
        return "Close Keyboard" if self._kbd_open else "Open Keyboard"

    def open_or_close_keyboard(self, icon, item):
        """Tray menu: open the on-screen keyboard, or close it if it's already
        open. Shares the Win+Ctrl+O toggle path (launcher_thread owns the
        window)."""
        self.toggle_keyboard_hotkey()

    def open_keybinds(self, icon=None, item=None):
        """Tray menu ("Keybinds"): open the Steam-style binding picker. Menu
        actions must take at most (icon, item)  pystray's _assert_action
        REJECTS callables with a third parameter, even defaulted  so the
        warm-build flag lives on _open_keybinds below, not here."""
        self._open_keybinds()

    def _open_keybinds(self, warm=False):
        """Open (or warm pre-build) the Steam-style binding picker. Imported
        lazily and defensively so the tray never depends on tkinter at startup.
        On Windows CPython always bundles Tk, so a failure here is a real import
        bug, not a missing OS package  surface the actual error instead of
        guessing "install Tk" (the misleading message this used to show). The
        picker runs its own Tk loop on its own thread and calls _save_keybinds.
        `warm=True` (startup pre-build) constructs the window hidden and does
        NOT show it  the first real click then opens instantly."""
        try:
            import keybinds_picker
        except Exception as e:
            print(f"keybinds picker failed to import: {e!r}")
            if not warm:
                self._notify("Keybinds", f"Binding picker failed to open: {e}")
            return
        kind = pads.canon(self.settings.get("last_osk_controller", "sc")) or "sc"
        keybinds_picker.open_picker(self.settings.get("keybinds", {}),
                                    self.settings.get("chords", []),
                                    self._save_keybinds, kind,
                                    general=self._general_settings(),
                                    on_general=self._apply_general_setting,
                                    profiles=self.settings.get(
                                        "keybind_profiles", {}),
                                    active=self.settings.get(
                                        "keybind_profile", 1),
                                    on_profile=self._select_keybind_profile,
                                    profile_count=self.settings.get(
                                        "keybind_profile_count",
                                        {"pc": 1, "gamepad": 1, "guide": 1}),
                                    on_add_profile=self._add_keybind_profile,
                                    on_delete_profile=self._delete_keybind_profile,
                                    profile_names=self.settings.get(
                                        "keybind_profile_names", {}),
                                    on_rename_profile=self._rename_keybind_profile,
                                    warm=warm)

    def toggle_config_gui(self, *args):
        """The "Toggle Config GUI" bound action (default on the Guide button,
        both Desktop and Gamepad tabs): reveal the Keybinds picker if it's
        hidden, hide it  handing the OS foreground back to the game  if it's
        shown. Lets players pop the picker up mid-game to tweak bindings and
        land back in a borderless-windowed title with focus.

        NOTE: a borderless-windowed / windowed game only. A game in EXCLUSIVE
        fullscreen owns the display and a normal window can't draw over it
        without overlay injection (the D3D/GL/Vulkan present hook Steam's own
        overlay uses)  there, toggling the GUI minimises the game. Runs on a
        controller input thread; the picker's own Tk loop services the request.
        Accepts *args so it works as either a bare action or a (kind, mode)
        callback."""
        try:
            import keybinds_picker
        except Exception as e:
            print(f"toggle_config_gui: picker import failed: {e!r}")
            return
        # Toggle an already-built window; if none exists yet (never opened, or a
        # warm build is still in flight), fall back to a full open (which builds
        # it and shows it).
        if not keybinds_picker.toggle_picker():
            self._open_keybinds()

    def _general_settings(self):
        """Current values for the picker's General page (mirrors the Startup
        submenu). 'when_steam' collapses the two mutually-exclusive bools to one
        choice; default = pause (matches DEFAULT_SETTINGS)."""
        if self.settings.get("auto_gamepad_mode"):
            _gm = "auto"
        elif self.settings.get("gamepad_mode"):
            _gm = "always"
        elif self.settings.get("gamepad_manual"):
            _gm = "manual"
        else:
            _gm = "off"
        return {
            "start_with_windows": self.settings.get("start_with_windows", False),
            "when_steam": "exit" if self.settings.get("exit_on_steam_launch")
                          else "pause" if self.settings.get("disable_while_steam_running")
                          else "use",
            "controller_preview": self.settings.get("controller_preview", True),
            "tutorial_done": self.settings.get("tutorial_done", False),
            "block_sc_hid": self.settings.get("block_sc_hid", False),
            "block_gamepad_takeover": self.settings.get("block_gamepad_takeover", False),
            "gamepad_mode": _gm,
            "virtual_menus": keybinds_runtime.vmenus_sanitize(
                self.settings.get("virtual_menus")),
            # Steam page  state lives in Steam's own files/registry, read
            # live here (NOT persisted to settings.json; see the Steam client
            # settings module).
            "steam_bigpicture": steam_get_bigpicture(),
            "steam_run_admin": steam_get_run_admin(),
            "steam_offline": steam_get_offline(),
            "steam_autostart": steam_get_autostart(),
            "steam_ask_account": steam_get_ask_account(),
            "steam_cloud": steam_get_cloud(),
            "steam_input": steam_get_steam_input(),
            # Sleep Manager page  master toggle + snapshot flag from
            # settings.json, everything else read live from the Windows power
            # scheme (only when armed: powercfg subprocess reads are skipped
            # while the master is off, where the controls are greyed anyway).
            **self._sleep_general_settings(),
            # Big Picture page  controller-connect automation only on Linux
            # (see big_picture.py).
            "bp_auto_launch": self.settings.get("bp_auto_launch", "off"),
            "bp_auto_close": bool(self.settings.get("bp_auto_close")),
            # On Screen Keyboard page (mirrors the tray "Keyboard Settings" submenu).
            "kbd_lstick_mouse": self.settings.get("kbd_lstick_mouse", True),
            "kbd_rstick_mouse": self.settings.get("kbd_rstick_mouse", True),
            "kbd_gyro_always": self.settings.get("kbd_gyro_always", False),
            # Gyro To Type cog-modal tuning (global  no kind prefix).
            **{"kbd_gyro_" + b: self.settings.get("kbd_gyro_" + b, dv)
               for b, dv in self._KBD_GYRO_KEYS},
            "osk_buttons": self.settings.get("osk_buttons") or {},
            "lpad_click_button": self.settings.get("lpad_click_button", "l2"),
            "rpad_click_button": self.settings.get("rpad_click_button", "r2"),
            "osk_size": self.settings.get("osk_size", "medium"),
            "osk_layout": self.settings.get("osk_layout", "classic"),
            "osk_split_layout": self.settings.get("osk_split_layout", False),
            "osk_scale_display": self.settings.get("osk_scale_display", False),
            "osk_diacritics": self.settings.get("osk_diacritics", True),
            "osk_diacritic_locale": self.settings.get("osk_diacritic_locale",
                                                      "auto"),
            "osk_hit_assist": self.settings.get("osk_hit_assist", 10),
            "osk_press_focus": self.settings.get("osk_press_focus", True),
            "osk_focus_pull": self.settings.get("osk_focus_pull", 50),
            "osk_key_sound": self.settings.get("osk_key_sound", True),
            "osk_per_app": self.settings.get("osk_per_app", False),
            "osk_transparency": self._osk_transparency_fraction(),
            "skin": self.settings.get("skin", "DefaultTheme"),
            "skins": adusk_skins.available_skins(),
            # Steam Controller page (mirrors the tray "Steam Controller" submenu).
            "rumble_enabled_sc": self.settings.get("rumble_enabled_sc", True),
            "rumble_gamepad_sc": self.settings.get("rumble_gamepad_sc", True),
            "sc_osk_trigger_actuation": self.settings.get("sc_osk_trigger_actuation", "default"),
            "sc_mouse_trigger_actuation": self.settings.get("sc_mouse_trigger_actuation", "default"),
            "sc_gamepad_trigger_actuation": self.settings.get("sc_gamepad_trigger_actuation", "default"),
            "sc_pointer_speed": self.settings.get("sc_pointer_speed", "medium"),
            "sc_trackpad_speed": self.settings.get("sc_trackpad_speed", "medium"),
            "sc_scroll_speed": self.settings.get("sc_scroll_speed", "medium"),
            # Touchpads page.
            "sc_scroll_mode": self.settings.get("sc_scroll_mode", "normal"),
            "sc_scroll_invert": self.settings.get("sc_scroll_invert", False),
            "text_wheel_selection": self.settings.get("text_wheel_selection", False),
            "video_scrub": self.settings.get("video_scrub", "off"),
            "pinch_zoom": self.settings.get("pinch_zoom", False),
            "pinch_sensitivity": self.settings.get("pinch_sensitivity", 0.7),
            "swipe_pages": self.settings.get("swipe_pages", False),
            "swipe_right_output": self.settings.get("swipe_right_output", "page_prev"),
            "swipe_left_output": self.settings.get("swipe_left_output", "page_next"),
            "tap_to_click": self.settings.get("tap_to_click", False),
            "tap_to_click_left": self.settings.get("tap_to_click_left", False),
            "typing_mode": self.settings.get("typing_mode", "default"),
            # Switch Pro Controller page (mirrors the tray "Switch Pro Controller" submenu).
            "rumble_enabled_switch": self.settings.get("rumble_enabled_switch", True),
            "rumble_gamepad_switch": self.settings.get("rumble_gamepad_switch", True),
            "switch_pointer_speed": self.settings.get("switch_pointer_speed", "medium"),
            # Nintendo Bluetooth dropout mitigations (Switch Pro page).
            "nintendo_bt_safe": self.settings.get("nintendo_bt_safe", True),
            "bt_hold_slot": self.settings.get("bt_hold_slot", True),
            "joycon_combine": self.settings.get("joycon_combine", True),
            "joycon_vertical": self.settings.get("joycon_vertical", False),
            "joycon_stick_rotate": self.settings.get("joycon_stick_rotate",
                                                     False),
            # Which controller kinds have ever been detected  unlocks their
            # top-bar tab + Options category permanently (SC always unlocked).
            "seen_controllers": list(self.settings.get("seen_controllers") or ()),
            # Per-controller pages for every OTHER catalog kind: the copied SC
            # settings (per-kind keys) + that kind's OSK button map (flattened
            # to oskbtn_<kind>_<func> so the picker's snapshot/revert works).
            **self._kind_general_settings(),
        }

    def _kind_general_settings(self):
        """Per-kind Options values for the picker (see _general_settings)."""
        out = {}
        osk_by_kind = self.settings.get("osk_buttons_by_kind") or {}
        for kind in pads.KINDS:
            if kind != "sc":
                bases = ["osk_trigger_actuation", "mouse_trigger_actuation",
                         "gamepad_trigger_actuation", "pointer_speed",
                         "rumble_enabled", "rumble_gamepad"]
                if kind in pads.HID_KINDS:
                    # HID-takeover kinds (Steam Deck) carry the SC's trackpad
                    # settings too: Right Trackpad Sensitivity + the two
                    # Touchpad Keyboard Input buttons.
                    bases += ["trackpad_speed",
                              "lpad_click_button", "rpad_click_button"]
                for base in bases:
                    key = pads.setting_key(kind, base)
                    if key in out or key in ("rumble_enabled_switch",
                                             "rumble_gamepad_switch",
                                             "switch_pointer_speed"):
                        continue  # legacy keys are already in _general_settings
                    if base.startswith("rumble"):
                        out[key] = self.settings.get(key, True)
                    elif base in ("pointer_speed", "trackpad_speed"):
                        out[key] = self.settings.get(key, "medium")
                    elif base == "lpad_click_button":
                        out[key] = self.settings.get(key, "l2")
                    elif base == "rpad_click_button":
                        out[key] = self.settings.get(key, "r2")
                    else:
                        out[key] = self.settings.get(key, "default")
            kmap = osk_by_kind.get(kind) or {}
            if kind != "sc":
                for func, cid in kmap.items():
                    out["oskbtn_%s_%s" % (kind, func)] = cid
            # Gyro To Mouse cog-modal values (every gyro-capable kind, the SC
            # included)  surfaced so the modal shows the PERSISTED tuning
            # after a restart instead of falling back to its defaults.
            if pads.has_gyro(kind):
                for base, dv in (("gyro_mode", "toggle"), ("gyro_dots", 6545),
                                 ("gyro_sens", 2.5), ("gyro_accel", "off"),
                                 ("gyro_deadzone", 0.36),
                                 ("gyro_precision", 0.75),
                                 ("gyro_output", "mouse")):
                    key = pads.setting_key(kind, base)
                    out[key] = self.settings.get(key, dv)
        return out

    def _sleep_general_settings(self):
        """Sleep Manager page values for the picker. Master + slider memory
        come from settings.json; when the master is armed the live Windows
        power scheme wins for everything it can express (Disable All Sleep
        zeroes the timers, so slider minutes fall back to the saved memory)."""
        s = self.settings
        clamp = lambda m, dflt: (max(0, min(_SLEEP_MAX_MIN, int(m)))
                                 if isinstance(m, (int, float)) and m >= 0
                                 else dflt)
        mode = s.get("sleep_last_mode", "sleep")
        av = sleep_availability()
        hiber_gb, free_gb = sleep_hiberfile_info()
        pbtn_ac, _ = _sleep_get_values("SUB_BUTTONS", "PBUTTONACTION")
        lid_ac, lid_dc = _sleep_get_values("SUB_BUTTONS", "LIDACTION")
        st = sleep_get_status()
        out = {
            "sleep_manager": bool(s.get("sleep_manager", False)),
            "sleep_monitor": clamp(s.get("sleep_monitor_min", 30), 30),
            # The two generic timeout sliders: "standby" (Sleep Timeout 
            # sleep / sleep_hib / hybrid) and "hibernate" (Hibernate Timeout 
            # s4 / sleep_hib).
            "sleep_standby": clamp(s.get("sleep_standby_min", 30), 30),
            "sleep_hibernate": clamp(s.get("sleep_hibernate_min", 60), 60),
            # "Use Sleep Mode" dropdown: off / sleep / sleep_hib / s4 / hybrid.
            "sleep_mode": (mode if mode in ("off", "sleep", "sleep_hib",
                                            "s4", "hybrid") else "sleep"),
            "sleep_signin": bool(st["signin_on"]),
            # Diagnostics for the page's status card + option gating.
            "sleep_is_admin": sleep_is_admin(),
            "sleep_avail": dict(av["states"]),
            "sleep_avail_reasons": dict(av["reasons"]),
            "sleep_avail_parsed": bool(av["parsed"]),
            "sleep_mode_line": sleep_mode_line(st),
            "sleep_signin_locked": sleep_signin_policy_locked(),
            "sleep_hiber_gb": hiber_gb,
            "sleep_disk_free_gb": free_gb,
            # Extra Windows-state controls (read live; snapshotted like the rest).
            # 4 = "Turn Off Screen", which powercfg supports and the lid
            # dropdowns already offered  it was missing here, so a machine
            # already set that way read back as plain Sleep.
            "sleep_pbutton": pbtn_ac if pbtn_ac in (0, 1, 2, 3, 4) else 1,
            "sleep_fast_startup": sleep_get_fast_startup(),
            # Lid-close dropdowns (read live; independent of the master, so
            # never snapshotted/restored). AC = plugged in, DC = on battery.
            "sleep_lid_ac": _sleep_lid_choice(lid_ac, s),
            "sleep_lid_dc": _sleep_lid_choice(lid_dc, s),
        }
        if not out["sleep_manager"]:
            return out
        out["sleep_monitor"] = clamp(st["monitor_min"], out["sleep_monitor"])
        # Live Windows state wins for the dropdown + the sliders it carries 
        # EXCEPT for the manual-only ambiguity: a 0 timer makes the inference
        # under-determined (sleep with a manual-only standby looks like "s4"
        # or "off"; sleep_hib with a manual-only stage looks like "s4" or
        # "sleep"), so the saved choice breaks the tie when the live raw
        # values are consistent with it.
        live = st["mode"]
        saved = out["sleep_mode"]
        if saved == "sleep" and live in ("s4", "off") and st["standby_min"] == 0:
            live = "sleep"              # manual-only standby
        elif saved == "sleep_hib" and st["hib_on"] and not st["hybrid_on"]:
            if live == "s4" and st["standby_min"] == 0:
                live = "sleep_hib"      # manual-only standby stage
            elif live == "sleep" and st["hibernate_min"] == 0:
                live = "sleep_hib"      # manual-only hibernate stage
        out["sleep_mode"] = live
        if live in ("sleep", "sleep_hib", "hybrid"):
            out["sleep_standby"] = clamp(st["standby_min"],
                                         out["sleep_standby"])
        if live in ("s4", "sleep_hib"):
            out["sleep_hibernate"] = clamp(st["hibernate_min"],
                                           out["sleep_hibernate"])
        return out

    def _sleep_apply_mode(self, mode):
        """Apply a Use Sleep Mode choice at the remembered slider minutes.
        Returns (apply_ok, verify_ok, verify_msg)  shared by the dropdown
        handler and the two timeout sliders (which re-apply the active
        mode)."""
        sby = self.settings.get("sleep_standby_min", 30)
        hib = self.settings.get("sleep_hibernate_min", 60)
        if mode == "sleep":
            ok = sleep_apply_sleep(sby)
        elif mode == "sleep_hib":
            ok = sleep_apply_sleep_hib(sby, hib)
        elif mode == "s4":
            ok = sleep_apply_s4(hib)
        elif mode == "hybrid":
            ok = sleep_apply_hybrid(sby)
        else:
            ok = sleep_apply_off()
            mode = "off"
        vok, msg = sleep_verify(mode)
        return ok, vok, msg

    def _sleep_toast(self, ok, ok_msg, fail_msg):
        """Sleep Manager feedback toast. Failures ALWAYS surface (the appliers
        otherwise only print to a console nobody sees in the windowed exe);
        successes only when a message is given (mode changes  slider tweaks
        stay quiet)."""
        if ok:
            if ok_msg:
                self._notify("Sleep Manager", ok_msg)
        else:
            self._notify("Sleep Manager", fail_msg)

    # Options whose REAL state does NOT live in settings.json  Steam's own
    # config files, the system power settings, the autostart entry  or whose
    # apply can be REFUSED outright (Sleep Manager without admin rights, Steam
    # config files that aren't there). For all of these the picker must read
    # the truth BACK after applying instead of assuming its own value took, or
    # a failed apply leaves a control showing On while the machine is still
    # Off. Maps setting -> which live-read group answers it; every "sleep_*"
    # key routes to the sleep group. Settings absent from this map are plain
    # settings.json writes that cannot fail, so the picker's value is already
    # the truth and no read-back is needed. (The Windows tree also lists the
    # scaling / playback / display / Big-Picture-device groups  those pages
    # don't exist here.)
    _READBACK_GROUPS = {
        "start_with_windows": "autostart",
        "steam_bigpicture": "steam", "steam_run_admin": "steam",
        "steam_offline": "steam",
        "steam_autostart": "steam", "steam_ask_account": "steam",
        "steam_cloud": "steam", "steam_input": "steam",
    }

    # Gamepad Mode is POLL-ONLY (below), never in _READBACK_GROUPS: its apply
    # is a settings.json write that cannot fail, so re-reading right after one
    # would tell the picker what it just said. What the poll IS for is the
    # other direction  the tray's own "Game Mode" item (toggle_game_mode) and
    # the Hotkey/hold-Start mode switches all change this behind an open
    # picker, which would otherwise sit there showing the stale page.
    _READBACK_POLL_EXTRA = {"gamepad_mode": "gamepad"}

    # The picker's BACKGROUND poll (see its _ext_poll) reads the same groups
    # plus the poll-only extras. The Windows tree also adds Loudness
    # Equalization  that page has no Linux counterpart, which is the only
    # difference between the two trees' maps.
    _READBACK_POLL_GROUPS = dict(_READBACK_GROUPS, **_READBACK_POLL_EXTRA)

    _STEAM_READBACK = {
        "steam_bigpicture": steam_get_bigpicture,
        "steam_run_admin": steam_get_run_admin,
        "steam_offline": steam_get_offline,
        "steam_autostart": steam_get_autostart,
        "steam_ask_account": steam_get_ask_account,
        "steam_cloud": steam_get_cloud,
        "steam_input": steam_get_steam_input,
    }

    # Serializes the Options apply / read-back / poll paths. The applies run on
    # the picker's Tk thread while the poll runs on its own worker, so a poll
    # landing mid-apply must not interleave with it. One App instance, so a
    # class-level lock is the instance lock; RLock because the readback path is
    # reached from inside the same guard.
    _general_io_lock = threading.RLock()

    def _general_readback(self, keys, poll=False):
        """Re-read the TRUE current value of each requested Options setting from
        its authoritative source and return {setting: real_value}  the picker
        calls this right after an apply so a control that did NOT take snaps
        back to reality (see _READBACK_GROUPS for what qualifies and why).
        Values come back in exactly the form _general_settings publishes them,
        so the picker can drop them straight into its mirror. Each group's live
        read runs AT MOST ONCE, and a pending set that touches none of them
        costs nothing  this is on the Save path, so it must not turn every tab
        switch into a power-settings read."""
        groups = (self._READBACK_POLL_GROUPS if poll else self._READBACK_GROUPS)
        want = {}
        for k in keys or ():
            grp = "sleep" if str(k).startswith("sleep_") else groups.get(k)
            if grp:
                want.setdefault(grp, []).append(k)
        out = {}
        for grp, ks in want.items():
            try:
                if grp == "steam":
                    for k in ks:
                        out[k] = bool(self._STEAM_READBACK[k]())
                elif grp == "autostart":
                    out["start_with_windows"] = bool(autostart.is_enabled())
                elif grp == "gamepad":
                    # The three mutually-exclusive bools collapsed back to the
                    # one "always"/"auto"/"manual"/"off" choice the page shows,
                    # in exactly the form _general_settings publishes it.
                    out["gamepad_mode"] = self._general_settings()["gamepad_mode"]
                else:
                    live = self._sleep_general_settings()
                    for k in ks:
                        if k in live:
                            out[k] = live[k]
            except Exception as e:
                print(f"settings read-back ({grp}) failed: {e!r}")
        return out

    def _apply_general_setting(self, setting, value):
        """Options channel from the picker. Two pseudo-settings are reads, not
        applies: "__readback__" (Tk thread, right after an apply  what
        actually took) and "__poll__" (the picker's background worker  has an
        outside change happened). Everything else is a real apply and runs
        under the shared lock; see _apply_general_setting_locked."""
        if setting == "__poll__":
            # Background poll. NEVER waits: when an apply or read-back is in
            # flight on the Tk thread we skip this tick entirely rather than
            # hold a worker on the lock (or worse, make the next user apply
            # queue behind a power-settings read). It comes round again in
            # seconds.
            if not self._general_io_lock.acquire(blocking=False):
                return {}
            try:
                return self._general_readback(value, poll=True)
            finally:
                self._general_io_lock.release()
        with self._general_io_lock:
            if setting == "__readback__":
                return self._general_readback(value)
            return self._apply_general_setting_locked(setting, value)

    def _apply_general_setting_locked(self, setting, value):
        """Apply a General-page change from the picker (runs on the picker's Tk
        thread). Mirrors the tray Startup-submenu handlers: persist + side effects.
        File write + Event signals are thread-safe, so calling from the picker
        thread is fine."""
        if setting == "gamepad_mode":
            # "auto" / "always" / "manual" / "off". The picker emits auto /
            # manual / off (master ViGEm Bus Driver toggle + Auto Enable toggle);
            # the tray radios still emit always / auto / off. All four are
            # mutually exclusive.
            if value == "always":
                self.settings["gamepad_mode"] = True
                self.settings["auto_gamepad_mode"] = False
                self.settings["gamepad_manual"] = False
                if self._auto_gamepad_pid is not None:
                    self._auto_gamepad_pid = None
                    self._auto_gamepad_focused = False
            elif value == "auto":
                self.settings["auto_gamepad_mode"] = True
                self.settings["gamepad_mode"] = False
                self.settings["gamepad_manual"] = False
            elif value == "manual":
                # Driver/pad stays loaded, but DESKTOP by default  gamepad is
                # reached only via the Hotkey Gamepad/Desktop Toggle chord.
                self.settings["gamepad_manual"] = True
                self.settings["gamepad_mode"] = False
                self.settings["auto_gamepad_mode"] = False
                if self._auto_gamepad_pid is not None:
                    self._auto_gamepad_pid = None
                    self._auto_gamepad_focused = False
            else:  # "off"
                if (not self.settings["gamepad_mode"]
                        and not self.settings["auto_gamepad_mode"]
                        and not self.settings.get("gamepad_manual", False)):
                    return
                self.settings["gamepad_mode"] = False
                self.settings["auto_gamepad_mode"] = False
                self.settings["gamepad_manual"] = False
                if self._auto_gamepad_pid is not None:
                    self._auto_gamepad_pid = None
                    self._auto_gamepad_focused = False
            _save_settings(self.settings)
            # A deliberate mode change clears any Hotkey-toggle control override.
            self._mode_override = None
            self._kick_sc()
            self._auto_gamepad_wake.set()
        elif setting == "virtual_menus":
            # Virtual Menus: persist + publish; the SC watcher notices the
            # version bump on its own thread (it owns the overlay window).
            menus = keybinds_runtime.vmenus_sanitize(value)
            self.settings["virtual_menus"] = menus
            _save_settings(self.settings)
            adusk_state.set_virtual_menus(menus)
        elif setting == "start_with_windows":
            self.settings["start_with_windows"] = bool(value)
            _save_settings(self.settings)
            _apply_autostart(self.settings["start_with_windows"])
        elif setting == "when_steam":
            if value == "exit":
                self.settings["exit_on_steam_launch"] = True
                self.settings["disable_while_steam_running"] = False
                self._steam_active.clear()
            elif value == "use":
                # Neither Steam-reactive behavior  SteamlessInput keeps
                # running normally regardless of Steam's state.
                self.settings["exit_on_steam_launch"] = False
                self.settings["disable_while_steam_running"] = False
                self._steam_active.clear()
            else:  # "pause"
                self.settings["disable_while_steam_running"] = True
                self.settings["exit_on_steam_launch"] = False
            _save_settings(self.settings)
            self._steam_watch_wake.set()
        elif setting == "steam_bigpicture":
            # Steam-page toggles write Steam's own files/registry  nothing
            # is persisted to settings.json (the Steam config IS the state).
            steam_set_bigpicture(bool(value))
        elif setting == "steam_run_admin":
            steam_set_run_admin(bool(value))
        elif setting == "steam_offline":
            steam_set_offline(bool(value))
        elif setting == "steam_autostart":
            steam_set_autostart(bool(value))
        elif setting == "steam_ask_account":
            steam_set_ask_account(bool(value))
        elif setting == "steam_cloud":
            steam_set_cloud(bool(value))
        elif setting == "steam_input":
            steam_set_steam_input(bool(value))
        elif setting == "sleep_manager":
            # Master Sleep Manager toggle. Arming captures a full powercfg
            # snapshot FIRST (the picker fires the page's other controls right
            # after, so the pristine state is already safe); disarming writes
            # the snapshot back  everything returns to the values it had at
            # arm time  then drops it. Arming without admin rights is refused
            # (the picker gates this too; belt and braces).
            on = bool(value)
            if on and not self.settings.get("sleep_manager"):
                if not sleep_is_admin():
                    self._sleep_toast(
                        False, None,
                        "Administrator rights required  Sleep Manager was "
                        "not enabled.")
                    return
                self.settings["sleep_manager_snapshot"] = sleep_snapshot()
            elif not on and self.settings.get("sleep_manager"):
                ok = sleep_restore(
                    self.settings.get("sleep_manager_snapshot") or {})
                self._sleep_toast(
                    ok, "Disabled  your Windows power settings were restored.",
                    "Disabled, but restoring the saved Windows power settings "
                    "reported errors.")
                self.settings["sleep_manager_snapshot"] = {}
            self.settings["sleep_manager"] = on
            _save_settings(self.settings)
            if not on:
                # The lid dropdowns live OUTSIDE the snapshot (they sit above
                # the master), but a lid on "Active Sleep Mode" may have been
                # resolved to Hibernate while the armed manager ran pure S4 
                # with the manager gone it must mean plain Sleep again.
                lid_ac, lid_dc = _sleep_get_values("SUB_BUTTONS", "LIDACTION")
                if lid_ac == 2 or lid_dc == 2:
                    _sleep_set_value("SUB_BUTTONS", "LIDACTION",
                                     1 if lid_ac == 2 else -1,
                                     1 if lid_dc == 2 else -1,
                                     activate=True)
        elif setting == "sleep_monitor":
            # Sleep Manager → "Monitor Sleep Settings" slider: minutes before
            # the monitor turns off, 0 = never (Off). Gated on the master so a
            # Disregard that reverts the master to off can't clobber the
            # just-restored snapshot with a late slider re-apply.
            if self.settings.get("sleep_manager"):
                self.settings["sleep_monitor_min"] = int(value)
                _save_settings(self.settings)
                ok = sleep_apply_monitor(int(value))
                self._sleep_toast(ok, None,
                                  "Could not set the monitor timeout.")
        elif setting in ("sleep_standby", "sleep_hibernate"):
            # Sleep Manager → the "Sleep Timeout" / "Hibernate Timeout"
            # sliders. Each slider is only movable while the Use Sleep Mode
            # dropdown selects a mode that uses it, and moving it RE-APPLIES
            # that mode with the new minutes (both timers for sleep_hib).
            if self.settings.get("sleep_manager"):
                key = ("sleep_standby_min" if setting == "sleep_standby"
                       else "sleep_hibernate_min")
                self.settings[key] = int(value)
                _save_settings(self.settings)
                ok, vok, msg = self._sleep_apply_mode(
                    self.settings.get("sleep_last_mode", "sleep"))
                self._sleep_toast(ok and vok, None,
                                  "Could not set the timeout."
                                  if vok else "Sleep timeout: %s" % msg)
        elif setting == "sleep_mode":
            # Sleep Manager → "Use Sleep Mode" dropdown: apply the chosen mode
            # at its remembered slider minutes ("off" = the .bat's [4] Disable
            # All Sleep). Each apply is re-verified against powercfg /a (the
            # .bat's VERIFY_STATE) and the outcome is toasted.
            if self.settings.get("sleep_manager"):
                mode = (value if value in ("off", "sleep", "sleep_hib",
                                           "s4", "hybrid") else "off")
                self.settings["sleep_last_mode"] = mode
                _save_settings(self.settings)
                ok, vok, msg = self._sleep_apply_mode(mode)
                # A lid dropdown left on "Active Sleep Mode" tracks this
                # choice: rewrite any live lid value that currently means
                # active-sleep (1 sleep / 2 hibernate) to the new resolution
                # so switching to/from Hibernate (S4) keeps the lid honest.
                lid_ac, lid_dc = _sleep_get_values("SUB_BUTTONS", "LIDACTION")
                new_lid = _sleep_lid_index("active_sleep", self.settings)
                w_ac = new_lid if (lid_ac in (1, 2)
                                   and lid_ac != new_lid) else -1
                w_dc = new_lid if (lid_dc in (1, 2)
                                   and lid_dc != new_lid) else -1
                if w_ac >= 0 or w_dc >= 0:
                    _sleep_set_value("SUB_BUTTONS", "LIDACTION",
                                     w_ac, w_dc, activate=True)
                names = {"sleep": "Sleep", "sleep_hib": "Sleep then Hibernate",
                         "s4": "Hibernate (S4)",
                         "hybrid": "Hybrid Sleep (S3+S4)"}
                if mode == "off":
                    self._sleep_toast(ok and vok, "All sleep modes disabled.",
                                      "Disable All Sleep: %s" % msg)
                else:
                    self._sleep_toast(
                        ok and vok,
                        "%s active  %s." % (names[mode],
                                             sleep_mode_line()),
                        "%s failed: %s" % (names[mode], msg))
        elif setting == "sleep_signin":
            # Sleep Manager → "Require Sign-in After Sleep" toggle.
            if self.settings.get("sleep_manager"):
                ok = sleep_set_signin(bool(value))
                self._sleep_toast(
                    ok, None, "Could not change the sign-in-on-wake setting.")
        elif setting == "sleep_pbutton":
            # Sleep Manager → "Power Button Action" dropdown: what the case's
            # power button does (0 nothing / 1 sleep / 2 hibernate / 3 shut
            # down). State lives in the power scheme; snapshotted like the
            # rest.
            if self.settings.get("sleep_manager"):
                ok = _sleep_set_value("SUB_BUTTONS", "PBUTTONACTION",
                                      int(value), activate=True)
                self._sleep_toast(
                    ok, None, "Could not change the power button action.")
        elif setting in ("sleep_lid_ac", "sleep_lid_dc"):
            # Sleep Manager → the two "Choose what closing the lid does"
            # dropdowns (LIDACTION; AC = plugged in, DC = on battery). They
            # sit above the master toggle and apply regardless of it (and are
            # therefore excluded from the arm-snapshot). "active_sleep"
            # resolves against the active Use Sleep Mode.
            idx = _sleep_lid_index(value, self.settings)
            ok = _sleep_set_value(
                "SUB_BUTTONS", "LIDACTION",
                idx if setting == "sleep_lid_ac" else -1,
                idx if setting == "sleep_lid_dc" else -1,
                activate=True)
            self._sleep_toast(
                ok, None, "Could not change the lid close action.")
        elif setting == "sleep_fast_startup":
            # Sleep Manager → "Fast Startup" toggle (hiberboot). Registry
            # write; snapshotted/restored with everything else.
            if self.settings.get("sleep_manager"):
                ok = sleep_set_fast_startup(bool(value))
                self._sleep_toast(
                    ok, None, "Could not change Fast Startup.")
        elif setting in ("bp_auto_launch", "bp_auto_close"):
            # Big Picture page: persist + poke the engine so its monitor
            # thread re-evaluates what to watch. Save-gated in the picker
            # (_LIVE_EXCLUDE).
            if setting == "bp_auto_launch":
                self.settings[setting] = (value if value in ("off", "steam",
                                                             "always")
                                          else "off")
            else:
                self.settings[setting] = bool(value)
            _save_settings(self.settings)
            self._bp_engine.refresh()
        elif setting == "block_sc_hid":
            self.settings["block_sc_hid"] = bool(value)
            _save_settings(self.settings)
            self._kick_sc()
        elif setting == "block_gamepad_takeover":
            self.settings["block_gamepad_takeover"] = bool(value)
            _save_settings(self.settings)
        elif setting == "controller_preview":
            # Just persist  the picker attaches/detaches its own live viewer in
            # place (see _set_controller_preview). Read at the next picker open
            # (and to gate the startup prewarm).
            self.settings["controller_preview"] = bool(value)
            _save_settings(self.settings)
        elif setting == "tutorial_done":
            # Latched by the tutorial overlay when it finishes or is skipped.
            # Persist immediately (not on Save): a user who skips it and then
            # kills the app should not be shown it again on the next launch.
            self.settings["tutorial_done"] = bool(value)
            _save_settings(self.settings)
        elif setting == "reset_settings":
            # A button, not a setting: the picker's own confirm dialog is the
            # user's consent. Write clean DEFAULT_SETTINGS and relaunch rather
            # than trying to hot-apply hundreds of settings across every live
            # subsystem  a fresh process reading a fresh file can't miss one.
            # Deferred to a background thread: this call itself runs ON the
            # picker's Tk thread, and exit_app() tears the picker down on that
            # SAME thread (keybinds_picker.shutdown() would deadlock waiting
            # for itself).
            _save_settings(dict(DEFAULT_SETTINGS))
            try:
                import subprocess
                exe = _exe_path()
                args = [exe] if _is_frozen() else [sys.executable, exe]
                subprocess.Popen(args, close_fds=True)
            except Exception as e:
                print(f"relaunch after settings reset failed: {e!r}")
                return False
            icon = self._icon_ref
            threading.Timer(0.1, lambda: self.exit_app(icon, None)).start()
            return True
        elif setting == "kbd_lstick_mouse":
            self.settings["kbd_lstick_mouse"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_kbd_stick_nav(self.settings["kbd_lstick_mouse"])
        elif setting == "kbd_rstick_mouse":
            self.settings["kbd_rstick_mouse"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_kbd_mouse_nav(self.settings["kbd_rstick_mouse"])
        elif setting == "kbd_gyro_always":
            self.settings["kbd_gyro_always"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_kbd_gyro_always(self.settings["kbd_gyro_always"])
        elif setting.startswith("kbd_gyro_"):
            # Gyro To Type cog: the global gyro-typing curves (sens / accel /
            # deadzone / precision), live to every controller at once.
            self.settings[setting] = value
            _save_settings(self.settings)
            adusk_state.set_kbd_gyro_config(
                **{setting[len("kbd_gyro_"):]: value})
        elif setting in ("lpad_click_button", "rpad_click_button"):
            # The SC's Touchpad Keyboard Input buttons (legacy flat keys; the
            # Steam Deck's copies are per-kind and land in the parse_setting_key
            # branch below). Only refresh the live runtime slot while the SC is
            # the active HID family  a live Deck keeps its own values.
            self.settings[setting] = value
            _save_settings(self.settings)
            if self._hid_kind == "sc":
                if setting == "lpad_click_button":
                    adusk_state.set_lpad_click_button(value)
                else:
                    adusk_state.set_rpad_click_button(value)
        elif setting in ("osk_caps", "osk_shift", "osk_enter",
                         "osk_space", "osk_backspace"):
            # One OSK function remapped to a SC button (value is a control id).
            func = setting[len("osk_"):]
            m = dict(self.settings.get("osk_buttons") or {})
            m[func] = value
            self.settings["osk_buttons"] = m
            _save_settings(self.settings)
            adusk_state.set_osk_button(func, value)
            # The SC's per-kind map (get_osk_buttons_for("sc")) must track the
            # flat map too, or a live rebind wouldn't reach the OSK.
            _by = self.settings.get("osk_buttons_by_kind") or {}
            adusk_state.set_osk_buttons_for("sc", _by.get("sc") or m)
        elif setting == "skin":
            self.settings["skin"] = value
            _save_settings(self.settings)
            # Re-skins an open OSK live on its next frame (render loop polls
            # skins.get_generation); otherwise applies on the next open.
            adusk_skins.set_active_skin(value)
        elif setting == "skin_preview":
            # Transient live skin for the Skin-dropdown hover preview: apply
            # WITHOUT persisting (no settings write). value=None reverts to the
            # saved skin (dropdown dismissed); a real pick persists via the
            # "skin" branch above, fired by the dropdown's on_change right after.
            name = value or self.settings.get("skin", "DefaultTheme")
            if name not in adusk_skins.available_skins():
                name = adusk_skins.DEFAULT_SKIN
            adusk_skins.set_active_skin(name)
        elif setting == "osk_transparency":
            # value is the continuous 0..1 slider fraction from the picker.
            try:
                frac = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                frac = 0.0
            self.settings["osk_transparency"] = frac
            _save_settings(self.settings)
            # Same live path as the old tray "Transparent" submenu (shares the
            # skin generation counter, so an open OSK switches next frame).
            adusk_skins.set_transparency_fraction(frac)
        elif setting == "osk_transparency_preview":
            # Transient preview while the picker's Transparency slider is held:
            # apply WITHOUT persisting (settings are save-on-demand now).
            # value=None reverts to the saved fraction (slider released /
            # staged edit discarded); Save persists via "osk_transparency".
            if value is None:
                frac = self._osk_transparency_fraction()
            else:
                try:
                    frac = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    frac = 0.0
            adusk_skins.set_transparency_fraction(frac)
        elif setting == "osk_size":
            # Same path as the tray "Size" submenu: publish the new size and flag a
            # cached-Screen rebuild (launcher_thread rebuilds it before the next
            # open / after the current one  never on this thread). value is one
            # of "small"/"medium"/"full".
            self.settings["osk_size"] = value
            _save_settings(self.settings)
            adusk_screen.set_osk_size(value)
            self._pending_size_change = True
        elif setting == "osk_size_preview":
            # Transient preview while the picker's Size slider is held: apply
            # WITHOUT persisting. value=None reverts to the saved size; Save
            # persists via "osk_size".
            size = value or self.settings.get("osk_size", "medium")
            adusk_screen.set_osk_size(size)
            self._pending_size_change = True
        elif setting == "rumble_enabled_sc":
            self.settings["rumble_enabled_sc"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_rumble_enabled("sc", bool(value))
        elif setting in ("rumble_gamepad_sc", "rumble_gamepad_switch"):
            # Gamepad-mode game FFB gate  read live by the rumble callbacks,
            # so persisting is all that's needed.
            self.settings[setting] = bool(value)
            _save_settings(self.settings)
        elif setting == "nintendo_bt_safe":
            # Options -> Switch Pro "Bluetooth Safe Mode". Applies LIVE: the
            # source re-classifies every open pad, so the guard starts/stops
            # without unplugging anything.
            self.settings["nintendo_bt_safe"] = bool(value)
            _save_settings(self.settings)
            if self._sdl_source is not None:
                try:
                    self._sdl_source.set_bt_safe(bool(value))
                except Exception:
                    pass
        elif setting in ("joycon_combine", "joycon_vertical"):
            # Joy-Con presentation. SDL reads these hints when the gamepad
            # subsystem initializes, so persisting is all we can do here  the
            # picker's info text says it takes a restart.
            self.settings[setting] = bool(value)
            _save_settings(self.settings)
        elif setting == "joycon_stick_rotate":
            # OUR rotation, not SDL's  applies live so the user can flip it
            # while holding the Joy-Con and feel which way is right.
            self.settings["joycon_stick_rotate"] = bool(value)
            _save_settings(self.settings)
            if self._sdl_source is not None:
                try:
                    self._sdl_source.set_joycon_stick_rotate(bool(value))
                except Exception:
                    pass
        elif setting == "bt_hold_slot":
            # "Keep Gamepad Slot On Dropout". Turning it OFF releases anything
            # currently parked, so the devices don't linger.
            self.settings["bt_hold_slot"] = bool(value)
            _save_settings(self.settings)
            if not value:
                self._release_parked_gamepads()
        elif setting == "rumble_enabled_switch":
            self.settings["rumble_enabled_switch"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_rumble_enabled("sdl", bool(value))
        elif setting == "sc_pointer_speed":
            # Options-tab "Right Joystick Sensitivity" slider (gradual). value is a
            # float multiplier anchored to the Low/Medium/High endpoints; clears no
            # tray-menu radio when in-between. Same live path as select_sc_pointer_speed.
            self.settings["sc_pointer_speed"] = value
            _save_settings(self.settings)
            if self._hid_kind == "sc":
                adusk_state.set_sc_mouse_speed(_sc_speed_mult(value))
        elif setting == "sc_osk_trigger_actuation":
            # Options-tab "Keyboard Trigger Actuation" slider (gradual). value is an
            # int analog threshold between the Default/Low endpoints; clears no
            # tray-menu radio when in-between. Same live path as select_sc_actuation.
            self.settings["sc_osk_trigger_actuation"] = value
            _save_settings(self.settings)
            if self._hid_kind == "sc":
                adusk_state.set_sc_osk_trigger_threshold(
                    _sc_actuation_threshold(value))
        elif setting == "sc_mouse_trigger_actuation":
            # Options-tab "Mouse Trigger Actuation" slider (gradual)  same
            # High/Default/Low scale as the Keyboard one, but a SEPARATE
            # setting/threshold. Same live path as select_sc_mouse_actuation.
            self.settings["sc_mouse_trigger_actuation"] = value
            _save_settings(self.settings)
            if self._hid_kind == "sc":
                adusk_state.set_sc_mouse_trigger_threshold(
                    _sc_actuation_threshold(value))
        elif setting == "sc_gamepad_trigger_actuation":
            # Options-tab "Gamepad Mode Trigger Actuation" slider (gradual)  same
            # High/Default/Low scale as the two above, but a SEPARATE setting that
            # governs when L2/R2 fire their gamepad-mode output (button or key).
            self.settings["sc_gamepad_trigger_actuation"] = value
            _save_settings(self.settings)
            if self._hid_kind == "sc":
                adusk_state.set_sc_gamepad_trigger_threshold(
                    _sc_actuation_threshold(value))
        elif setting == "sc_trackpad_speed":
            # Options-tab "Right Trackpad Sensitivity" slider (gradual, LINEAR;
            # the current speed sits at the slider midpoint). value is a float
            # multiplier; the tray-menu radios still store named levels. The
            # watcher reads the state getter per frame, so this applies live.
            self.settings["sc_trackpad_speed"] = value
            _save_settings(self.settings)
            if self._hid_kind == "sc":
                adusk_state.set_sc_trackpad_speed(_sc_trackpad_mult(value))
        elif setting == "sc_scroll_speed":
            # Options-tab Touchpads → "Scrolling Sensitivity" slider (gradual).
            # value is a float multiplier anchored to the Low/Medium/High
            # endpoints; the tray-menu radios still store named levels.
            self.settings["sc_scroll_speed"] = value
            _save_settings(self.settings)
            adusk_state.set_sc_scroll_speed(_sc_scroll_mult(value))
        elif setting == "sc_scroll_mode":
            # Options-tab Touchpads → "Left Touchpad Scrolling" dropdown:
            # "normal" (direct wheel notches) / "laptop" (kinetic coasting) /
            # "wheel" (circular scroll dial) / "wheel_smooth" (analog dial). The
            # watcher polls adusk_state per frame, so this applies live.
            self.settings["sc_scroll_mode"] = value
            _save_settings(self.settings)
            adusk_state.set_sc_scroll_mode(value)
        elif setting == "sc_scroll_invert":
            # Options-tab Touchpads scroll-settings cog → "Invert Scrolling"
            # toggle: flips scroll direction for all modes. Applies live.
            self.settings["sc_scroll_invert"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_sc_scroll_invert(bool(value))
        elif setting == "text_wheel_selection":
            # Options-tab Touchpads → "Text Wheel Selection" toggle: hold the
            # left mouse button + circle the left pad to select text letter by
            # letter. Applies live (the watcher polls adusk_state per frame).
            self.settings["text_wheel_selection"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_text_wheel_selection(bool(value))
        elif setting == "video_scrub":
            # Options-tab Touchpads → "Video Timeline Scrubbing" dropdown:
            # "off" / "frame" (precise) / "seek" (fast). Applies live (the
            # watcher polls adusk_state per frame).
            self.settings["video_scrub"] = value
            _save_settings(self.settings)
            adusk_state.set_video_scrub_mode(value)
        elif setting == "pinch_zoom":
            # Touchpads → "Pinch To Zoom" toggle. Applies live; turning it
            # OFF also restores 1:1 right away (the watcher notices and
            # forgets its zoom level) so the desktop can't be stranded
            # zoomed with the gesture gone.
            self.settings["pinch_zoom"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_pinch_zoom(bool(value))
            if not value:
                _mag_reset()
        elif setting == "pinch_sensitivity":
            # Touchpads → the "Camera Sensitivity" slider under Pinch To Zoom:
            # 0..1 float driving _handle_pad_pan's LPAN_SENS. Applies live.
            try:
                self.settings["pinch_sensitivity"] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                self.settings["pinch_sensitivity"] = 0.7
            _save_settings(self.settings)
            adusk_state.set_pinch_sensitivity(self.settings["pinch_sensitivity"])
        elif setting == "swipe_pages":
            # Touchpads → "Swipe Between Pages" toggle. Applies live (the
            # watcher polls adusk_state per frame).
            self.settings["swipe_pages"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_swipe_pages(bool(value))
        elif setting == "swipe_right_output":
            # Touchpads → Swipe Between Pages cog modal, "Swipe Right"
            # dropdown. Value is a picker action-vocabulary id (Hotkeys
            # Button Combo vocabulary). Applies live.
            self.settings["swipe_right_output"] = value or "page_prev"
            _save_settings(self.settings)
            adusk_state.set_swipe_right_output(self.settings["swipe_right_output"])
        elif setting == "swipe_left_output":
            # Touchpads → Swipe Between Pages cog modal, "Swipe Left" dropdown.
            self.settings["swipe_left_output"] = value or "page_next"
            _save_settings(self.settings)
            adusk_state.set_swipe_left_output(self.settings["swipe_left_output"])
        elif setting == "tap_to_click":
            # Touchpads → "Right Touchpad Tap to Click" toggle. Applies live
            # (the watcher polls adusk_state per frame).
            self.settings["tap_to_click"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_tap_to_click(bool(value))
        elif setting == "tap_to_click_left":
            # Touchpads → "Left Touchpad Tap to Click" toggle. Applies live
            # (the watcher polls adusk_state per frame).
            self.settings["tap_to_click_left"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_tap_to_click_left(bool(value))
        elif setting == "typing_mode":
            # Touchpads → "Trackpad Keyboard Typing Mode" dropdown. Applies live: the
            # OSK's pad handler reads adusk_state every frame (and at every
            # lift), so switching mode takes effect with the keyboard already
            # open  including the full-keyboard pad reach that "swipe" turns on.
            mode = value if value in TYPING_MODE_FLAGS else "default"
            self.settings["typing_mode"] = mode
            _save_settings(self.settings)
            rtt, tt, st = typing_mode_flags(mode)
            adusk_state.set_release_to_type(rtt)
            adusk_state.set_touch_typing(tt)
            adusk_state.set_swipe_typing(st)
        elif setting == "osk_split_layout":
            # Options -> Keyboard -> "Split Keyboard". Applies live: the OSK's
            # main loop notices the window size it should have has changed and
            # resizes in place on its next poll.
            self.settings["osk_split_layout"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_split_layout(bool(value))
        elif setting == "osk_scale_display":
            # "Scale With Resolution"  same live-resize path as the split
            # toggle above.
            self.settings["osk_scale_display"] = bool(value)
            _save_settings(self.settings)
            adusk_screen.set_display_scaling(bool(value))
        elif setting == "osk_diacritics":
            self.settings["osk_diacritics"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_diacritics_enabled(bool(value))
        elif setting == "osk_diacritic_locale":
            self.settings["osk_diacritic_locale"] = str(value or "auto")
            _save_settings(self.settings)
            adusk_state.set_diacritic_locale(self.settings["osk_diacritic_locale"])
        elif setting == "osk_hit_assist":
            self.settings["osk_hit_assist"] = int(value)
            _save_settings(self.settings)
            adusk_state.set_hit_expand(int(value))
        elif setting == "osk_press_focus":
            self.settings["osk_press_focus"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_press_focus(bool(value))
        elif setting == "osk_focus_pull":
            self.settings["osk_focus_pull"] = int(value)
            _save_settings(self.settings)
            adusk_state.set_trigger_focus_pull(_osk_focus_pull_raw(int(value)))
        elif setting == "osk_key_sound":
            self.settings["osk_key_sound"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_key_sound_enabled(bool(value))
        elif setting == "osk_per_app":
            self.settings["osk_per_app"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_per_app_memory(bool(value))
        elif setting == "osk_layout":
            # Options → Keyboard → "Keyboard Layout" dropdown. Applies live 
            # an open (or preview) OSK rebuilds its pages from the new YAML on
            # its next loop iteration (see adusk.main()'s last_layout check).
            self.settings["osk_layout"] = (
                value if value in adusk_state.OSK_LAYOUTS else "classic")
            _save_settings(self.settings)
            adusk_state.set_osk_layout(self.settings["osk_layout"])
        elif setting == "osk_layout_preview":
            # Transient live layout for the Keyboard Layout-dropdown hover
            # preview: apply WITHOUT persisting (no settings write). value=None
            # reverts to the saved layout (dropdown dismissed); a real pick
            # persists via the "osk_layout" branch above, fired by the
            # dropdown's on_change right after.
            name = (value if value in adusk_state.OSK_LAYOUTS
                    else self.settings.get("osk_layout", "classic"))
            adusk_state.set_osk_layout(name)
        elif setting == "switch_pointer_speed":
            self.settings["switch_pointer_speed"] = value
            _save_settings(self.settings)
            adusk_state.set_switch_mouse_speed(_SC_MOUSE_SPEEDS.get(value, 1.0))
        elif setting.startswith("oskbtn_"):
            # Per-controller OSK button remap ("oskbtn_<kind>_<func>", value is
            # a control id) from that controller's Options category.
            kind, _, func = setting[len("oskbtn_"):].rpartition("_")
            kind = pads.canon(kind)
            if kind:
                by = dict(self.settings.get("osk_buttons_by_kind") or {})
                m = dict(by.get(kind) or {})
                m[func] = value
                by[kind] = m
                self.settings["osk_buttons_by_kind"] = by
                _save_settings(self.settings)
                adusk_state.set_osk_buttons_for(kind, m)
        elif pads.parse_setting_key(setting) is not None:
            # A copied per-controller setting for a generic SDL kind (the SC and
            # Switch keep their dedicated branches above): trigger actuations,
            # pointer speed, haptics toggles.
            kind, base = pads.parse_setting_key(setting)
            self.settings[setting] = value
            _save_settings(self.settings)
            # When the edited kind is the ACTIVE HID-takeover family (a Steam
            # Deck driving the SC runtime), also refresh the live sc_* slots
            # the watcher/OSK read per frame  same live feel as the SC's own
            # dedicated branches above.
            _live_hid = kind == self._hid_kind
            if base == "rumble_enabled":
                adusk_state.set_rumble_enabled(kind, bool(value))
            elif base == "pointer_speed":
                adusk_state.set_kind_mouse_speed(kind, _sc_speed_mult(value))
                if _live_hid:
                    adusk_state.set_sc_mouse_speed(_sc_speed_mult(value))
            elif base == "osk_trigger_actuation":
                adusk_state.set_sdl_trigger_threshold(
                    kind, "osk", _sdl_actuation_threshold(value))
                if _live_hid:
                    adusk_state.set_sc_osk_trigger_threshold(
                        _sc_actuation_threshold(value))
            elif base == "mouse_trigger_actuation":
                adusk_state.set_sdl_trigger_threshold(
                    kind, "mouse", _sdl_actuation_threshold(value))
                if _live_hid:
                    adusk_state.set_sc_mouse_trigger_threshold(
                        _sc_actuation_threshold(value))
            elif base == "gamepad_trigger_actuation":
                adusk_state.set_sdl_trigger_threshold(
                    kind, "gamepad", _sdl_actuation_threshold(value))
                if _live_hid:
                    adusk_state.set_sc_gamepad_trigger_threshold(
                        _sc_actuation_threshold(value))
            elif base in ("adv_long_ms", "adv_double_ms", "adv_soft_pct"):
                # Advanced Presses timing. The engines bake their thresholds in
                # at construction, so drop every cached one and kick the SC
                # watcher  both rebuild on their next frame with the new feel,
                # which is what makes the slider testable by just holding the
                # button.
                self._sdl_gp_cache = {}
                self._sdl_pc_adv = {}
                self._sdl_adv_engines.clear()
                if _live_hid or kind == "sc":
                    self._kick_sc()
            elif base == "trackpad_speed":
                # Steam Deck "Right Trackpad Sensitivity" (per-kind copy of
                # the SC's). Applies live while the Deck drives.
                if _live_hid:
                    adusk_state.set_sc_trackpad_speed(_sc_trackpad_mult(value))
            elif base == "lpad_click_button":
                if _live_hid:
                    adusk_state.set_lpad_click_button(value)
            elif base == "rpad_click_button":
                if _live_hid:
                    adusk_state.set_rpad_click_button(value)
            elif base.startswith("gyro_"):
                # The cog-modal gyro tuning (mode/dots/sens/accel/deadzone/
                # precision)  publish live to the shared per-kind config
                # every gyro consumer reads.
                adusk_state.set_gyro_config(kind, **{base[len("gyro_"):]: value})
                if base == "gyro_mode":
                    # Re-seed the live on/off state for the new mode:
                    # hold_suppress = gyro on until the hotkey suppresses it;
                    # every other mode starts off. Drop any asserted holders
                    # with it  they were pressed under the OLD mode's meaning,
                    # and frame-driven sources re-assert on their next frame.
                    with _gyro_act_lock:
                        _gyro_act_holders.pop(kind, None)
                    adusk_state.set_gyro_mouse(kind, value == "hold_suppress")
            # rumble_gamepad: persisting is all that's needed (the game-FFB
            # gate reads settings live, like rumble_gamepad_sc/switch).
        elif setting == "osk_preview":
            # Live Options-tab preview: show the OSK while a Size/Transparency
            # slider is held (value True), hide it on release (value False).
            # Not persisted  purely a transient on-screen preview.
            if value:
                self._open_osk_preview()
            else:
                self._close_osk_preview()
        elif setting == "osk_typing":
            # Menu/≡ "Enter Value" on a slider: show the OSK so the user can
            # TYPE into the picker's value entry with the controller. Unlike
            # osk_preview this open must be interactive (real input), so it
            # goes through the same trigger path as the keyboard hotkey instead
            # of the input-ignoring preview path. Not persisted.
            if value:
                self._open_osk_typing()
            else:
                self._close_osk_typing()

    def _save_keybinds(self, new_binds, new_chords=None, profile=None):
        """Persist an edited keybind layout + chords from the picker (runs on the
        picker's Tk thread). Applies the Switch Pro PC-mode desktop binds live;
        `new_chords` is now PER-CONTROLLER ({"sc":[...],"switch":[...]})  the SC
        chords take effect by kicking the SteamController so launcher_thread
        rebuilds its watcher with `chords_for(...,"sc")`  the same kick re-reads
        the SC per-button desktop rebinds and the gamepad-mode remap. (Switch
        chords are stored but applied in a follow-up.) `profile` is the picker's
        per-(controller, tab) {kind: {mode: slot}} active map: each controller's
        each layout tab's binds land in its OWN slot AND the live "keybinds"
        mirror is recomposed from all the slots."""
        if profile:
            # `profile` is the picker's {kind: {mode: slot}} map. Distribute
            # each (kind, mode)'s binds into ITS OWN slot (leaving that slot's
            # other (kind, mode) pairs, owned by other controllers/tabs,
            # untouched), then recompose the live mirror.
            profs = dict(self.settings.get("keybind_profiles") or {})
            active = dict(self.settings.get("keybind_profile") or {})
            for kind, modes in profile.items():
                if kind not in pads.KINDS or not isinstance(modes, dict):
                    continue
                kactive = dict(active.get(kind) or {})
                for mode, slot in modes.items():
                    slot = str(slot)
                    slot_map = dict(profs.get(slot) or {})
                    kb = dict(slot_map.get(kind) or {})
                    kb[mode] = dict((new_binds.get(kind) or {}).get(mode) or {})
                    slot_map[kind] = kb
                    profs[slot] = slot_map
                    try:
                        kactive[mode] = int(slot)
                    except (TypeError, ValueError):
                        pass
                active[kind] = kactive
            self.settings["keybind_profiles"] = profs
            self.settings["keybind_profile"] = active
            self.settings["keybinds"] = self._compose_keybinds()
        else:
            self.settings["keybinds"] = new_binds
        if new_chords is not None:
            self.settings["chords"] = new_chords
        _save_settings(self.settings)
        self._sdl_gp_cache = {}
        self._sc_extra_gp_cache = {}
        self._sdl_pc_adv = {}
        d = self._sdl_desktop
        if d is not None:
            try:
                _dk = getattr(self, "_sdl_desktop_kind", lambda: "switch")()
                d.apply_binds((new_binds or {}).get(_dk),
                              keybinds_runtime.chords_for(
                                  self.settings.get("chords", []), _dk),
                              kind=_dk)
                self._sdl_close_bits = d.close_bits
            except Exception as e:
                print(f"apply keybinds failed: {e!r}")
        # Rebuild the SC watcher so edited chords apply without a restart.
        if new_chords is not None:
            try:
                self._kick_sc()
            except Exception as e:
                print(f"kick after chord save failed: {e!r}")
            # Re-publish the "Gyro To Mouse" hotkey masks the OSK evaluates.
            try:
                self._publish_gyro_masks()
            except Exception as e:
                print(f"gyro mask publish failed: {e!r}")
        print("keybinds saved")

    def _compose_keybinds(self):
        """Assemble the live 'keybinds' mirror from the per-(controller, tab)
        active profile slots. Every catalog kind, and within it Desktop (pc) /
        Gamepad (gamepad) / Chords (guide), can each sit on a different slot 
        completely independent of every other controller  so each (kind,
        mode)'s binds are taken from its OWN selected slot. Runtime consumers
        read only this composed mirror, so the per-(kind, mode) profiles need
        no other plumbing."""
        profs = self.settings.get("keybind_profiles") or {}
        active = self.settings.get("keybind_profile") or {}
        out = {}
        for kind in pads.KINDS:
            out[kind] = {}
            kactive = active.get(kind) or {}
            for mode in ("pc", "gamepad", "guide"):
                slot = str(kactive.get(mode, 1))
                kb = (profs.get(slot) or {}).get(kind) or {}
                out[kind][mode] = dict(kb.get(mode) or {})
        return out

    def _select_keybind_profile(self, kind, mode, slot):
        """Picker footer profile button (1-4): switch the live profile slot for
        ONE controller's ONE tab (mode = "pc"/"gamepad"/"guide")  every other
        controller, and this controller's other tabs, keep their own slots.
        That tab's binds jump to the slot's SAVED state immediately; edits
        within a profile still only land via Save Layout. Runs on the picker's
        Tk thread (file write + kick are thread-safe, same as _save_keybinds)."""
        if kind not in pads.KINDS or mode not in ("pc", "gamepad", "guide"):
            return
        active = self.settings.get("keybind_profile")
        active = dict(active) if isinstance(active, dict) else {}
        kactive = dict(active.get(kind) or {})
        try:
            kactive[mode] = int(slot)
        except (TypeError, ValueError):
            return
        active[kind] = kactive
        self.settings["keybind_profile"] = active
        self.settings["keybinds"] = self._compose_keybinds()
        _save_settings(self.settings)
        self._sdl_gp_cache = {}
        self._sc_extra_gp_cache = {}
        self._sdl_pc_adv = {}
        d = self._sdl_desktop
        if d is not None:
            try:
                _dk = getattr(self, "_sdl_desktop_kind", lambda: "switch")()
                d.apply_binds((self.settings["keybinds"] or {}).get(_dk),
                              keybinds_runtime.chords_for(
                                  self.settings.get("chords", []), _dk),
                              kind=_dk)
                self._sdl_close_bits = d.close_bits
            except Exception as e:
                print(f"apply profile binds failed: {e!r}")
        # Rebuild the SC watcher so the profile's binds/chords-tab layout apply
        # without a restart.
        try:
            self._kick_sc()
        except Exception as e:
            print(f"kick after profile switch failed: {e!r}")
        print(f"keybind profile {kind}/{mode}={slot} selected")

    def _add_keybind_profile(self, kind, mode, count):
        """Picker footer "+" button: persist the new profile-slot count
        (1-_MAX_KEYBIND_PROFILES) for ONE controller's ONE tab (mode =
        "pc"/"gamepad"/"guide")  every other controller (and this controller's
        other tabs) is untouched. The new slot is empty and unnamed until the
        user edits + Saves it. Runs on the picker's Tk thread (settings write is
        thread-safe)."""
        if kind not in pads.KINDS or mode not in ("pc", "gamepad", "guide"):
            return
        try:
            count = min(_MAX_KEYBIND_PROFILES, max(1, int(count)))
        except (TypeError, ValueError):
            return
        counts = self.settings.get("keybind_profile_count")
        counts = dict(counts) if isinstance(counts, dict) else {}
        kcounts = dict(counts.get(kind) or {})
        kcounts[mode] = count
        counts[kind] = kcounts
        self.settings["keybind_profile_count"] = counts
        _save_settings(self.settings)
        print(f"keybind profile count {kind}/{mode} -> {count}")

    def _delete_keybind_profile(self, kind, mode, slot):
        """Picker footer right-click: delete a profile slot from ONE
        controller's ONE tab. This (kind, mode)'s higher slots renumber down to
        stay contiguous (slot n takes n+1's binds AND name, …, the top slot
        cleared)  only THIS controller's THIS mode's binds move; every other
        (kind, mode) sharing the same slot storage is untouched. Mirrors the
        picker's own in-memory renumber so both stay in sync. The active slot for
        this (kind, mode) follows the shift; the live "keybinds" mirror is
        recomposed and the SC watcher kicked. Runs on the picker's Tk thread."""
        if kind not in pads.KINDS or mode not in ("pc", "gamepad", "guide"):
            return
        counts = self.settings.get("keybind_profile_count")
        counts = dict(counts) if isinstance(counts, dict) else {}
        kcounts = dict(counts.get(kind) or {})
        try:
            count = min(_MAX_KEYBIND_PROFILES, max(1, int(kcounts.get(mode, 1))))
        except (TypeError, ValueError):
            count = 1
        try:
            n = int(slot)
        except (TypeError, ValueError):
            return
        if count <= 1 or not (1 <= n <= count):
            return
        profs = dict(self.settings.get("keybind_profiles") or {})
        for s in range(n, count):
            src = profs.get(str(s + 1)) or {}
            dst = dict(profs.get(str(s)) or {})
            kb = dict(dst.get(kind) or {})
            kb[mode] = dict((src.get(kind) or {}).get(mode) or {})
            dst[kind] = kb
            profs[str(s)] = dst
        top = dict(profs.get(str(count)) or {})
        kb = dict(top.get(kind) or {})
        kb[mode] = {}
        top[kind] = kb
        profs[str(count)] = top
        self.settings["keybind_profiles"] = profs
        # Names ride along with the binds they name (same shift, same clear).
        names = dict(self.settings.get("keybind_profile_names") or {})
        knames = dict(names.get(kind) or {})
        mnames = dict(knames.get(mode) or {})
        if mnames:
            for s in range(n, count):
                nxt = mnames.get(str(s + 1), "")
                if nxt:
                    mnames[str(s)] = nxt
                else:
                    mnames.pop(str(s), None)
            mnames.pop(str(count), None)
            knames[mode] = mnames
            names[kind] = knames
            self.settings["keybind_profile_names"] = names
        active = self.settings.get("keybind_profile")
        active = dict(active) if isinstance(active, dict) else {}
        kactive = dict(active.get(kind) or {})
        try:
            cur = int(kactive.get(mode, 1))
        except (TypeError, ValueError):
            cur = 1
        if cur == n:
            cur = n if n < count else count - 1
        elif cur > n:
            cur -= 1
        kactive[mode] = min(count - 1, max(1, cur))
        active[kind] = kactive
        self.settings["keybind_profile"] = active
        kcounts[mode] = count - 1
        counts[kind] = kcounts
        self.settings["keybind_profile_count"] = counts
        self.settings["keybinds"] = self._compose_keybinds()
        _save_settings(self.settings)
        self._sdl_gp_cache = {}
        self._sc_extra_gp_cache = {}
        self._sdl_pc_adv = {}
        d = self._sdl_desktop
        if d is not None:
            try:
                _dk = getattr(self, "_sdl_desktop_kind", lambda: "switch")()
                d.apply_binds((self.settings["keybinds"] or {}).get(_dk),
                              keybinds_runtime.chords_for(
                                  self.settings.get("chords", []), _dk),
                              kind=_dk)
                self._sdl_close_bits = d.close_bits
            except Exception as e:
                print(f"apply after profile delete failed: {e!r}")
        try:
            self._kick_sc()
        except Exception as e:
            print(f"kick after profile delete failed: {e!r}")
        print(f"keybind profile {kind}/{mode} slot {n} deleted -> count {count - 1}")

    def _rename_keybind_profile(self, kind, mode, slot, name):
        """Picker footer name box: store the user's name for ONE controller's
        ONE tab's ONE slot. Cosmetic  nothing in the input path changes, so
        there's no recompose and no kick, just the settings write. An empty name
        DELETES the entry (the slot falls back to the generic "<Tab> Profile"
        placeholder) rather than storing "". Runs on the picker's Tk thread,
        debounced there so a burst of keystrokes is one write."""
        if kind not in pads.KINDS or mode not in ("pc", "gamepad", "guide"):
            return
        try:
            n = int(slot)
        except (TypeError, ValueError):
            return
        if not 1 <= n <= _MAX_KEYBIND_PROFILES:
            return
        name = (name or "").strip()[:_MAX_PROFILE_NAME]
        names = self.settings.get("keybind_profile_names")
        names = dict(names) if isinstance(names, dict) else {}
        knames = dict(names.get(kind) or {})
        mnames = dict(knames.get(mode) or {})
        if name:
            if mnames.get(str(n)) == name:
                return                     # no-op write (a re-flush)  skip it
            mnames[str(n)] = name
        else:
            if str(n) not in mnames:
                return
            mnames.pop(str(n), None)
        knames[mode] = mnames
        names[kind] = knames
        self.settings["keybind_profile_names"] = names
        _save_settings(self.settings)
        print(f"keybind profile {kind}/{mode} slot {n} named {name!r}")

    def _keybind_profile_name(self, kind, mode, slot):
        """One slot's display name for a toast: the user's name if they gave it
        one, else the generic "<Tab> Profile N"."""
        try:
            stored = ((self.settings.get("keybind_profile_names") or {})
                      .get(kind, {}).get(mode, {}).get(str(slot), ""))
        except AttributeError:
            stored = ""
        if stored:
            return stored
        return "%s profile %s" % ({"pc": "Desktop", "gamepad": "Gamepad",
                                   "guide": "Chords"}.get(mode, ""), slot)

    def cycle_keybind_profile(self, kind, mode):
        """"<Mode> Profile Cycle" bound action: advance ONE controller's active
        slot for that tab to the next EXISTING slot, wrapping back to 1 (no-op
        when only one slot exists). `kind` is the controller that dispatched
        the action (see _SdlDesktopController._active_kind / the SC watcher,
        which is always "sc"). Reuses _select_keybind_profile for the live
        switch + kick. Runs on the SC callback thread / the SDL gamepad thread
         same thread-safe write/kick path the Hotkeys Gamepad-Mode-Toggle
        chord already uses."""
        kind = pads.canon(kind) or "sc"
        if kind not in pads.KINDS or mode not in ("pc", "gamepad", "guide"):
            return
        active = self.settings.get("keybind_profile")
        active = active if isinstance(active, dict) else {}
        kactive = active.get(kind) or {}
        counts = self.settings.get("keybind_profile_count")
        counts = counts if isinstance(counts, dict) else {}
        kcounts = counts.get(kind) or {}
        try:
            count = min(_MAX_KEYBIND_PROFILES, max(1, int(kcounts.get(mode, 1))))
        except (TypeError, ValueError):
            count = 1
        try:
            cur = int(kactive.get(mode, 1))
        except (TypeError, ValueError):
            cur = 1
        nxt = (cur % count) + 1        # 1..count, wraps back to 1
        if nxt == cur:
            return                     # single slot → nothing to cycle
        self._select_keybind_profile(kind, mode, nxt)
        try:
            # Named slots announce themselves by NAME  with up to
            # _MAX_KEYBIND_PROFILES of them, "profile 7" says nothing.
            self._notify("Keybinds", "%s  %s"
                         % (pads.display_name(kind),
                            self._keybind_profile_name(kind, mode, nxt)))
        except Exception:
            pass

    def toggle_keyboard_hotkey(self, opener=None):
        """Win+Ctrl+O: open the on-screen keyboard, or close it if it's open.
        Lets people without a Steam Controller preview the keyboard. Runs on the
        raw hotkey-hook's worker thread, so it only signals  launcher_thread owns
        the window and actually opens/closes it. `opener` names the controller family
        requesting the open ("sdl" for an SDL pad such as the Switch Pro), so the
        launcher can start the OSK on that controller's glyphs; None for a
        non-controller open (tray menu / hotkey)."""
        if self._kbd_open:
            adusk_state.close()
            return
        self._pending_open_controller = opener
        # Remember the window the user was in so adusk can restore focus after
        # the OSK opens (SDL-pad / hotkey opens don't go through the Steam
        # Controller watcher that normally captures this). Foreground is still
        # the user's app here  an SDL pad's buttons don't inject anything.
        self._pending_restore_hwnd = _foreground_target_hwnd()
        self._open_kbd_event.set()
        self._launcher_wake.set()
        # Break the current sc.run() (if a controller is connected) so the
        # launcher loop proceeds straight to opening the keyboard.
        sc = self._current_sc
        if sc is not None:
            try:
                sc.addExit()
            except Exception:
                pass

    def _open_osk_preview(self):
        """Show the OSK for a live size/transparency preview (picker slider
        press). Runs on the picker's Tk thread  only signals; launcher_thread
        owns the window and opens it without an animation. No-op if the keyboard
        is already open (so releasing the slider can never close an OSK the user
        opened themselves)."""
        if self._kbd_open or self._osk_preview:
            return
        self._osk_preview = True
        self._pending_open_controller = None
        # Foreground here is the picker window  restore focus to it after open.
        self._pending_restore_hwnd = _foreground_target_hwnd()
        self._launcher_wake.set()
        sc = self._current_sc
        if sc is not None:
            try:
                sc.addExit()
            except Exception:
                pass

    def _close_osk_preview(self):
        """Hide the preview OSK (picker slider release). Only closes the OSK the
        preview itself opened; a no-op otherwise."""
        if not self._osk_preview:
            return
        self._osk_preview = False
        adusk_state.close()

    def _open_osk_typing(self):
        """Show the OSK so the user can type a slider value with the controller
        (Menu/≡ "Enter Value"). Runs on the picker's Tk thread  only signals;
        launcher_thread owns the window. Unlike _open_osk_preview this goes
        through the normal open_kbd trigger so input_thread runs with
        preview=False and actually processes clicks/typing. No-op if the
        keyboard is already open (so closing the entry can never close an OSK
        the user opened themselves)."""
        if self._kbd_open:
            return
        self._osk_typing = True
        self._pending_open_controller = None
        # Foreground here is the picker window  restore focus to it after open.
        self._pending_restore_hwnd = _foreground_target_hwnd()
        self._open_kbd_event.set()
        self._launcher_wake.set()
        sc = self._current_sc
        if sc is not None:
            try:
                sc.addExit()
            except Exception:
                pass

    def _close_osk_typing(self):
        """Hide the typing OSK (value entry committed/cancelled). Only closes
        the OSK the typing-open itself opened; a no-op otherwise."""
        if not self._osk_typing:
            return
        self._osk_typing = False
        adusk_state.close()

    # battery status --------------------------------------------------------

    # Discharge bands that each fire a single low-battery toast (only while
    # running off the charger), ascending so `next(b for b in bands if pct <= b)`
    # picks the tightest (most severe) band the pack is under. A reading that
    # lands BETWEEN bands  or a first reading that's already low  still warns
    # once at the appropriate band, and dropping to a more-severe (lower) band
    # warns again; the latch is re-armed only when a charger is connected (see
    # _battery_notifications), NOT on a % recovery, which is the anti-spam fix.
    _LOW_BATT_BANDS = (5, 10, 20, 30)
    # How often to poll the live controller's cached battery reading. Kept short
    # so charger connect/disconnect and % changes surface promptly; the poll is a
    # single cached-attribute read and we only touch the UI when the reading
    # actually changes, so a tight cadence is cheap. The charge-state guard that
    # rejects stray frames confirms across distinct battery frames (not a poll
    # count / wall-clock), so a fast poll only tightens latency, never the guard.
    _BATTERY_POLL_SECONDS = 1.5
    # Drop the battery display after the controller has been gone this long, so
    # a USB-C unplug doesn't leave a stale "(charging)" line in the menu. Longer
    # than a normal sc rebuild (gamepad-mode toggle / brief drop) so those don't
    # blink the line off and back on.
    _BATTERY_STALE_SECONDS = 8.0

    def is_battery_known(self, item):
        """Visibility callback for the battery menu line  hidden until the
        controller has actually reported a level."""
        return self._battery is not None

    def battery_menu_label(self, item):
        return self._battery_label or "Steam Controller: …"

    def _notify(self, title, message):
        icon = self._icon_ref
        if icon is None:
            return
        try:
            icon.notify(message, title)
        except Exception:
            pass

    def _debounce_charge(self, charging, charge_complete, frame_id):
        """Commit a charge-state change only once it has been corroborated by a
        SECOND, freshly-arrived battery frame (`frame_id` from
        SteamController.battery_frame_id), so a single stray/corrupt 0x43 frame
        (RF glitch, a brief source-validate) can't flip us and spam a
        connect/disconnect toast pair. The 0x43 report streams at only ~0.4 Hz,
        so confirming across DISTINCT frames  rather than a fixed wall-clock
        window  is both glitch-safe and as fast as the hardware allows (the old
        wall-clock pad added ~3.5s on top of the ~2.5s frame interval, which is
        why toasts lagged ~9s). Returns the debounced (charging, charge_complete).
        Must run every poll  it tracks the pending change across frames."""
        raw = (charging, charge_complete)
        # Accept the first reading (after launch or a stale-clear) at once so the
        # baseline is right immediately.
        if self._batt_charge_seen is None:
            self._batt_charging, self._batt_charge_complete = raw
            self._batt_charge_seen = raw
            self._batt_charge_pending_frame = None
            return raw
        committed = (self._batt_charging, self._batt_charge_complete)
        if raw == committed:
            # Back to (or still at) what we've committed  nothing pending.
            self._batt_charge_pending_frame = None
        elif raw != self._batt_charge_seen or self._batt_charge_pending_frame is None:
            # A new (or freshly-changed) value  remember the frame it first
            # appeared on; a value that keeps flip-flopping never corroborates.
            self._batt_charge_pending_frame = frame_id
        elif frame_id != self._batt_charge_pending_frame:
            # The same change has now shown up on a DIFFERENT, fresher frame 
            # it's real, commit it.
            self._batt_charging, self._batt_charge_complete = raw
            self._batt_charge_pending_frame = None
        self._batt_charge_seen = raw
        return self._batt_charging, self._batt_charge_complete

    def _update_battery_ui(self, pct, charging, charge_complete):
        """Refresh the tray tooltip + menu line from a (debounced) reading."""
        if charge_complete:
            state = f"{pct}% (charged)"
        elif charging:
            state = f"{pct}% (charging)"
        else:
            state = f"{pct}%"
        self._battery_label = f"Steam Controller: {state}"
        icon = self._icon_ref
        if icon is not None:
            try:
                icon.title = f"SteamlessInput  Steam Controller {state}"
            except Exception:
                pass
        self._refresh_menu()

    def _battery_notifications(self, pct, charging, charge_complete):
        """Fire battery toasts: charger connected / disconnected, fully charged,
        and the 30/20/10/5% low-battery band crossings (the last only while off
        the charger). `charging`/`charge_complete` are already debounced, so
        every edge here is a real one."""
        # Charger connected / disconnected  driven purely off the debounced
        # `charging` bit, so the wireless puck and a USB-C cable share ONE pair
        # of toasts (no per-source duplication). `charging` stays True through
        # ChargingDone, so reaching full never reads as an unplug; only a real
        # disconnect (→ Discharging) flips it False.
        if charging and not self._was_charging:
            # Fresh charge cycle → re-arm the low-battery bands for next time.
            self._low_warned_at = None
            # Don't announce "connected" when we link straight to an already-full
            # pack  the "fully charged" toast below is the right one there.
            if not charge_complete:
                self._notify("Steam Controller charging",
                             f"Charger connected  {pct}%.")
        elif not charging and self._was_charging:
            self._notify("Steam Controller unplugged",
                         f"Charger disconnected  {pct}% on battery.")
        self._was_charging = charging

        # Fully charged: notify once per charge completion.
        if charge_complete:
            if not self._charge_complete_notified:
                self._charge_complete_notified = True
                self._notify("Steam Controller fully charged",
                             "Steam Controller battery is full.")
        else:
            self._charge_complete_notified = False

        # Low-battery bands, only while running off the charger. Each band fires
        # at most once per discharge cycle; the latch is re-armed ONLY when a
        # charger is next connected (above), never on a % reading climbing back
        # up on its own (voltage recovery under light load)  that self-recovery
        # re-arm is exactly what used to re-fire the same warning repeatedly.
        if charging:
            return

        band = next((b for b in self._LOW_BATT_BANDS if pct <= b), None)
        if band is None:
            return
        # Warn on the first band hit (incl. a first reading that's already low or
        # lands between bands), and again each time we drop to a more-severe
        # (lower) band  but never twice within the same band.
        if self._low_warned_at is not None and band >= self._low_warned_at:
            return
        self._low_warned_at = band
        if band <= 5:
            self._notify("Steam Controller battery critical",
                         f"{pct}% left  charge now.")
        elif band <= 10:
            self._notify("Steam Controller battery very low",
                         f"{pct}% left  charge soon.")
        elif band <= 20:
            self._notify("Steam Controller battery low",
                         f"{pct}% remaining.")
        else:
            self._notify("Steam Controller battery getting low",
                         f"{pct}% remaining.")
        # A short haptic nudge so it's noticeable mid-game (haptics switch
        # permitting, and only if the device is still live).
        sc = self._current_sc
        if sc is not None and adusk_state.is_rumble_enabled("sc"):
            try:
                sc.haptic_click()
            except Exception:
                pass

    def battery_thread(self):
        """Poll the live controller's cached battery reading and drive the
        tray tooltip/menu plus low-battery / charged notifications. The reading
        itself is captured for free on the SteamController read loop; this
        thread just samples it on a slow timer (battery changes slowly) so the
        gaming hot path stays untouched."""
        last_key = None
        last_seen = None
        while not self._stop_event.is_set():
            if self._steam_active.is_set():
                # Paused for Steam: the SC is ceded to Steam, so there's nothing
                # to read  back off instead of sampling the (now-absent) battery.
                self._stop_event.wait(self._BATTERY_POLL_SECONDS)
                continue
            sc = self._current_sc
            batt = sc.get_battery() if sc is not None else None
            # Latch SC-ever-connected so the "Steam Controller" menu stays for
            # the session once detected (even while adusk owns the SC, OSK open).
            # Keyed on the reader's own family: the Steam Deck reports no
            # battery here at all, and the 2015 controller reports one but has
            # its own Options category (see is_sc_connected).
            if sc is not None and "sc" in getattr(sc, "_kinds", ("sc",)):
                self._sc_ever_connected = True
            now = time.monotonic()
            if batt is not None:
                last_seen = now
                self._battery = batt
                # Debounce the charge state every poll, then only touch the UI /
                # re-evaluate notifications when the DEBOUNCED reading actually
                # changes  so a tight poll, a 1% jiggle, or a charger flicker
                # neither rebuilds the tray menu nor re-toasts.
                charging, charge_complete = self._debounce_charge(
                    batt.charging, batt.charge_complete, sc.battery_frame_id())
                key = (batt.percent, charging, charge_complete)
                if key != last_key:
                    last_key = key
                    self._update_battery_ui(batt.percent, charging, charge_complete)
                    self._battery_notifications(batt.percent, charging, charge_complete)
            elif self._battery is not None and (
                    sc is not None
                    or last_seen is None
                    or now - last_seen > self._BATTERY_STALE_SECONDS):
                # Drop the now-stale reading. `sc is not None` = the controller
                # link is up but it reported no battery (powered off via Steam+Y
                # or dropped its wireless link while the dongle stays plugged) 
                # clear promptly. Otherwise (sc None: a brief rebuild or a full
                # unplug) wait the grace window so a gamepad-mode rebuild doesn't
                # blink the line off and back on. Reset the latches so a
                # reconnect is treated as a fresh charge cycle.
                # A USB-C wired controller can go completely silent the instant
                # the cable is pulled (no internal pack keeping it alive), so the
                # last frame we saw was still "charging"  that edge never hits
                # _battery_notifications normally, it lands here instead. Fire the
                # disconnect toast now so unplugging the cable isn't silent.
                if self._was_charging:
                    self._notify("Steam Controller unplugged",
                                 f"Charger disconnected  {self._battery.percent}% on battery.")
                self._battery = None
                self._battery_label = None
                last_key = None
                self._was_charging = False
                self._low_warned_at = None
                self._charge_complete_notified = False
                self._batt_charge_seen = None
                self._batt_charge_pending_frame = None
                self._batt_charging = False
                self._batt_charge_complete = False
                icon = self._icon_ref
                if icon is not None:
                    try:
                        icon.title = "SteamlessInput"
                    except Exception:
                        pass
                self._refresh_menu()
            self._stop_event.wait(self._BATTERY_POLL_SECONDS)

    def steam_watch_thread(self):
        last_running = False
        while not self._stop_event.is_set():
            exit_on_launch = self.settings["exit_on_steam_launch"]
            disable_while = self.settings["disable_while_steam_running"]

            if not exit_on_launch and not disable_while:
                # Neither Steam-reactive setting is enabled  make sure any
                # latched pause flag is cleared, then BLOCK (no polling) until a
                # tray toggle or shutdown wakes us. Zero wakeups while idle.
                if self._steam_active.is_set():
                    self._steam_active.clear()
                last_running = False
                self._steam_watch_wake.wait()
                self._steam_watch_wake.clear()
                continue

            running = _steam_running()
            just_started = running and not last_running

            if just_started and exit_on_launch:
                # "Exit on Steam Launch" wins over "Disable While …"  fully
                # tear down the tray app so Steam has the controller to itself.
                print("Steam detected; exiting per 'Exit on Steam Launch'.")
                self._stop_event.set()
                adusk_state.close()
                if self._current_sc is not None:
                    try:
                        self._current_sc.addExit()
                    except Exception:
                        pass
                self._exit_icon_ref()
                return

            if disable_while:
                if running and not self._steam_active.is_set():
                    # Pause the listener and close any open OSK so Steam can
                    # grab the controller for itself.
                    self._steam_active.set()
                    if not self.settings.get("steam_pause_toast_shown", False):
                        # One-time-ever toast (persisted)  only the very first
                        # time this pause behavior actually fires, not every
                        # Steam launch.
                        self._notify("Steam detected",
                                     "SteamlessInput paused")
                        self.settings["steam_pause_toast_shown"] = True
                        _save_settings_if_exists(self.settings)
                    adusk_state.close()
                    if self._current_sc is not None:
                        try:
                            self._current_sc.addExit()
                        except Exception:
                            pass
                elif not running and self._steam_active.is_set():
                    # Steam exited  resume. Force an intentional rebuild from
                    # CURRENT settings: the launcher only re-reads gamepad mode /
                    # block_sc_hid / keybinds / chords when it rebuilds its
                    # watcher, so without this kick a setting changed while paused
                    # wouldn't take effect until the user manually re-toggled it.
                    # _kick_sc() also sets _launcher_wake, breaking the launcher's
                    # paused _launcher_wait() so it rebuilds right now, not in 5s.
                    self._steam_active.clear()
                    self._kick_sc()
                    self._auto_gamepad_wake.set()

            last_running = running
            self._stop_event.wait(5.0)

    def auto_gamepad_thread(self):
        """Detect a likely-game process and latch onto it. While latched,
        poll the foreground window every 500ms so gamepad mode follows the
        game's focus state (alt-tab out → lizard mode for the desktop;
        alt-tab back → gamepad mode). When the game exits, release the
        latch. Diagnostic logging is opt-in via the ADUSK_GAMEPAD_DEBUG env
        var; without it the scan does no disk I/O."""
        try:
            import psutil
        except ImportError:
            return

        debug_enabled = bool(os.environ.get("ADUSK_GAMEPAD_DEBUG"))
        log_path = os.path.join(_exe_dir(), "auto_gamepad_debug.log")

        def _scan(state="unlatched"):
            # Detection runs unconditionally; the per-process diagnostic log is
            # written only when ADUSK_GAMEPAD_DEBUG is set, so normal desktop
            # use does no continuous disk I/O or log formatting.
            if not debug_enabled:
                return _detect_game_pid()
            try:
                if (os.path.exists(log_path)
                        and os.path.getsize(log_path) > 256 * 1024):
                    open(log_path, "w").close()
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"scan (auto-mode on, {state}) ===\n")
                    pid = _detect_game_pid(debug_log=f)
                    f.write(f"  result: {'pid=' + str(pid) if pid else 'NO MATCH'}\n")
                    return pid
            except Exception:
                # If logging fails for any reason, fall back to silent detection
                # so the auto thread keeps working.
                return _detect_game_pid()

        # The heavy unlatched scan is skipped while the foreground window is
        # unchanged (see the else branch below); -1 forces a scan on the first
        # loop and whenever auto mode is (re)enabled. last_latched_fg is the
        # same change-detector for the latched-but-unfocused RE-latch scan.
        last_scan_fg = -1
        last_latched_fg = -1
        while not self._stop_event.is_set():
            if not self.settings["auto_gamepad_mode"]:
                # Auto mode is off (Gamepad Off or Always-On)  nothing to scan.
                # BLOCK until a tray toggle or shutdown wakes us, so this thread
                # costs zero wakeups in those modes instead of polling every 2s.
                if self._auto_gamepad_pid is not None:
                    self._auto_gamepad_pid = None
                    self._auto_gamepad_focused = False
                    self._kick_sc()
                last_scan_fg = -1  # force a scan when auto mode is re-enabled
                self._auto_gamepad_wake.wait()
                self._auto_gamepad_wake.clear()
                continue

            if self._auto_gamepad_pid is not None:
                # Latched  cheap checks at 500ms so alt-tab is responsive.
                if not psutil.pid_exists(self._auto_gamepad_pid):
                    self._auto_gamepad_pid = None
                    self._auto_gamepad_focused = False
                    # The latched game exited  drop any "Hotkey Gamepad/Desktop
                    # Toggle" control override so the NEXT detected game returns
                    # to gamepad mode via auto-enable (without this, toggling to
                    # desktop during a game would stick across a close/reopen).
                    self._mode_override = None
                    self._kick_sc()
                else:
                    now_focused = _is_latched_focused(self._auto_gamepad_pid)
                    # Defer focus-change restarts while the Steam+VIEW chord
                    # is active  otherwise the alt-tab switcher stealing
                    # focus from the game would trigger a sc.run() rebuild
                    # that swallows subsequent VIEW presses (so cycling
                    # through windows stops working after the first press).
                    if (now_focused != self._auto_gamepad_focused
                            and not self._chord.alt_held):
                        self._auto_gamepad_focused = now_focused
                        self._kick_sc()
                    if now_focused:
                        last_latched_fg = -1  # re-arm the re-latch detector
                    elif not self._chord.alt_held:
                        # Latched but something ELSE is focused: if that
                        # something scans as a game, RE-latch onto it  the
                        # user switched games (or the first latch was wrong),
                        # and without this the old latch blocks the new game
                        # from ever getting gamepad mode until the old one
                        # exits. Foreground-change gated like the unlatched
                        # scan, so an idle latched-background costs nothing.
                        fg = _foreground_hwnd()
                        fg_key = (fg,
                                  _window_covers_monitor(fg) if fg else False,
                                  _window_fullscreen(fg) if fg else False)
                        if fg_key != last_latched_fg:
                            last_latched_fg = fg_key
                            pid = _scan("latched, other window focused")
                            if pid and pid != self._auto_gamepad_pid:
                                self._auto_gamepad_pid = pid
                                self._auto_gamepad_focused = \
                                    _is_latched_focused(pid)
                                self._mode_override = None
                                self._kick_sc()
                self._stop_event.wait(0.5)
            else:
                # Unlatched  the full scan (process enumeration + DLL checks) is
                # heavy, so skip it entirely while the FOREGROUND window is
                # unchanged: a game only becomes relevant once it's focused, which
                # changes the foreground, and the fast fullscreen check inside the
                # scan also keys off the foreground. So an idle/AFK desktop costs
                # just one GetForegroundWindow() per tick instead of enumerating
                # every process; a foreground change (game launch / alt-tab) runs
                # a real scan. The key includes covers-monitor AND true-
                # fullscreen so the SAME window flipping windowed<->fullscreen
                # (Alt+Enter, F11 from maximized) rescans too  the hwnd alone
                # doesn't change on those transitions.
                fg = _foreground_hwnd()
                fg_key = (fg,
                          _window_covers_monitor(fg) if fg else False,
                          _window_fullscreen(fg) if fg else False)
                if fg_key != last_scan_fg:
                    last_scan_fg = fg_key
                    pid = _scan()
                    if pid:
                        self._auto_gamepad_pid = pid
                        self._auto_gamepad_focused = _is_latched_focused(pid)
                        # A freshly detected game resets any control override so
                        # auto-enable can put it straight into gamepad mode.
                        self._mode_override = None
                        self._kick_sc()
                self._stop_event.wait(3.5)

    # Set by main() so the watch thread can stop the tray icon on Steam exit.
    _icon_ref = None

    def _exit_icon_ref(self):
        icon = self._icon_ref
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def _refresh_menu(self):
        """Rebuild the tray menu so the dynamic Open/Close Keyboard label
        re-reads _kbd_open. Called whenever _kbd_open flips on the launcher
        thread  the keyboard opens/closes asynchronously, so the rebuild
        pystray does right after a menu click happens before _kbd_open has
        actually changed and would otherwise leave the label stale."""
        icon = self._icon_ref
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    def _seed_steam_pause(self):
        """One SYNCHRONOUS Steam check before any HID handle is opened.

        _steam_active starts clear and is only ever set by steam_watch_thread,
        which is started in the same breath as launcher_thread (see main's
        setup)  so with Steam already running, the launcher's first iteration
        used to sail past the pause check and grab the controller anyway, only
        to be kicked off it a moment later by the watcher's first poll. Which
        thread won was decided by relative cost (the launcher's SDL screen
        prebuild vs the watcher's cold `import psutil`), not by any
        synchronization: a happy accident on most machines, and with
        block_sc_hid ON the grab is EXCLUSIVE, so losing that race actively
        denies Steam the controller during Steam's own startup enumeration.

        Doing the check here (rather than in setup) keeps the launcher
        self-protecting: it cannot open a device without having looked first.
        Setting the latch early is also harmless for steam_watch_thread  its
        `running and not _steam_active.is_set()` edge test just sees the work
        as already done and won't re-toast or re-close."""
        try:
            if self.settings.get("disable_while_steam_running") and _steam_running():
                self._steam_active.set()
        except Exception as e:
            print(f"initial steam check failed: {e!r}")

    def launcher_thread(self):
        """Crash-proof wrapper around the real loop.

        _launcher_loop has no internal catch-all: an unexpected exception
        anywhere in it (a malformed keybind resolving, an odd HID enumeration)
        would kill this thread outright and leave the app a zombie  tray icon
        alive and responsive, controller support silently dead until a full
        restart. Restart the loop instead, with a short backoff so a
        deterministic failure can't spin the CPU."""
        self._seed_steam_pause()
        while not self._stop_event.is_set():
            try:
                self._launcher_loop()
                return           # clean exit (shutdown / KeyboardInterrupt)
            except Exception:
                traceback.print_exc()
                print("launcher thread crashed; restarting in 2s")
                self._stop_event.wait(2.0)

    def _launcher_loop(self):
        # Reconnect backoff: when no controller is present, opening fails fast
        # and we'd otherwise re-enumerate HID every second forever (the common
        # case  tray app running with the controller turned off). Back off up
        # to RECONNECT_WAIT_MAX, resetting the instant a controller appears.
        reconnect_wait = 1.0
        RECONNECT_WAIT_MIN = 1.0
        RECONNECT_WAIT_MAX = 5.0
        # Pre-build the OSK Screen on THIS (render) thread so the first open is
        # instant AND the window/renderer never live on another thread (see
        # App.__init__ for why cross-thread rendering kills mouse input).
        self._ensure_cached_screen()
        while not self._stop_event.is_set():
            # If Steam is currently running and we're configured to pause,
            # release ViGEm so Steam can present its own virtual pad, and
            # wait it out without holding the controller HID handle open.
            # Wait on _launcher_wake (not a blind _stop_event sleep) so the
            # steam-exit kick from steam_watch_thread resumes us IMMEDIATELY and
            # rebuilds from current settings  otherwise any setting changed
            # while paused (gamepad mode, block_sc_hid, keybinds, chords) would
            # linger until the next lazy loop / a manual re-toggle. Shutdown also
            # sets _launcher_wake, so this stays responsive to exit.
            if self._steam_active.is_set():
                self._close_persistent_gamepad()
                self._launcher_wait(5.0)
                continue

            # Which HID controller family this iteration drives: the 2026
            # Steam Controller ("sc"), the original 2015 one ("sc2015") or
            # the Steam Deck's built-in pad ("steam_deck")  all the same
            # trackpad hardware, the same SCButtons bit space, one takeover
            # runtime. Prefer the family that last actually opened; with
            # several enumerable and no history, try them in catalog order and
            # alternate when a probe finds no responsive interface (e.g. the
            # SC dongle is plugged into a Deck but the controller is asleep 
            # the Deck must still drive).
            kinds_present = tuple(k for k in present_hid_kinds()
                                  if k in pads.HID_KINDS)
            if self._hid_prefer in kinds_present:
                hid_kind = self._hid_prefer
            elif kinds_present:
                hid_kind = kinds_present[0]
            else:
                hid_kind = self._hid_kind  # nothing present; open fails fast
            self._hid_kind = hid_kind
            # Tag the picker's HID nav channel so its hint glyphs (footer
            # button hints / header bumpers) match this family's art.
            sc_viewer.set_hid_kind(hid_kind)
            # Publish the active family's per-kind trackpad/trigger/pointer
            # settings into the shared takeover-runtime slots the watcher and
            # OSK read per frame (single-slot is safe: one HID device drives
            # at a time).
            self._publish_hid_settings(hid_kind)

            # Snapshot toggles for this iteration; toggle_*_mode and
            # auto_gamepad_thread both call _kick_sc() to force re-eval.
            manual_on = self.settings["gamepad_mode"]
            auto_enabled = self.settings["auto_gamepad_mode"]
            auto_latched = self._auto_gamepad_pid is not None
            auto_focused = auto_latched and self._auto_gamepad_focused

            # Keep ViGEm alive whenever the user might want gamepad output
            # any time soon, so games enumerate it at *their* startup. We
            # only push real input frames when "active"  manual on, OR
            # auto has latched a running game AND that game is focused
            # (when the game is backgrounded the controller reverts to
            # firmware mouse/kb so it's usable on the desktop).
            # "manual" (driver on, desktop default) keeps the pad alive so the
            # Hotkey toggle can switch to gamepad, but never makes gamepad the
            # base mode  only vg_should_live, not gamepad_active.
            driver_manual = self.settings.get("gamepad_manual", False)
            vg_should_live = manual_on or auto_enabled or driver_manual
            gamepad_active = manual_on or auto_focused
            # "Hotkey Gamepad/Desktop Toggle" runtime override: flips the live
            # control scheme WITHOUT changing vg_should_live (so the virtual pad
            # stays alive and the driver is never enabled/disabled by the chord).
            # Forcing gamepad controls only takes effect if a pad can be live.
            if self._mode_override is True:
                gamepad_active = vg_should_live
            elif self._mode_override is False:
                gamepad_active = False
            # Published for sdl_gamepad_thread's SDL->ViGEm gate.
            self._gamepad_active = gamepad_active

            if vg_should_live:
                self._ensure_persistent_gamepad()
                if self._persistent_gamepad is None:
                    # ViGEm construction failed  fall back to non-gamepad.
                    gamepad_active = False
            else:
                self._close_persistent_gamepad()

            # Chime on the real gamepad<->lizard transition. gamepad_active is
            # the single source of truth: it flips for menu toggles (Always-On,
            # Off) AND for auto-mode game focus changes, so one check covers
            # both. The first loop just seeds the state (silent at startup);
            # the chime plays on the device built below, once it opens (~1s).
            chime_now = None
            if self._chime_prev_active is None:
                self._chime_prev_active = gamepad_active
            elif gamepad_active != self._chime_prev_active:
                self._chime_prev_active = gamepad_active
                chime_now = gamepad_active

            # Lizard mode (firmware mouse/kb emulation) is now ALWAYS off:
            #   * gamepad mode  → off so it doesn't fight the XInput output;
            #   * desktop mode  → off because WE drive the desktop (takeover):
            #     the firmware's trackpad/click bindings aren't host-configurable,
            #     so to customize trackpad speed/scroll/click + chords we replace
            #     it entirely. `takeover` tells the watcher it owns the pads,
            #     pad-clicks and triggers (no gamepad = desktop = takeover).
            _sc_binds = self.settings.get("keybinds", {}).get(hid_kind)
            _sc_pc = keybinds_runtime.pc_submap(_sc_binds)
            _sc_guide = (_sc_binds.get("guide", {})
                         if isinstance(_sc_binds, dict) else {})
            # Publish which SC buttons close the OSK (those bound to Escape, B by
            # default) so adusk's OSK handler closes on them  mirrors the
            # keyboard Escape and follows the binding. Refreshed every loop so a
            # live rebind takes effect on the next OSK open.
            adusk_state.set_osk_close_buttons(
                keybinds_runtime.resolve_sc_close_buttons(_sc_pc, SCButtons)
                | set(self._sdl_close_bits))
            _lstick_mouse, _lstick, _rstick_mouse, _rstick = \
                keybinds_runtime.resolve_sc_sticks(_sc_pc, sui.Keys)
            # Gamepad-mode remap (the picker's SC "gamepad" binds)  only built
            # when we're actually driving the virtual pad this iteration.
            _gp_map, _gp_lt, _gp_rt = [], True, True
            _gp_lstick_map = _gp_rstick_map = None
            _gp_key_overrides = []
            _adv_engine = None
            _adv_engine_pc = None
            if not gamepad_active:
                # Desktop-mode Advanced Presses (the pc submap's "__adv"
                # rows)  stepped by the watcher's takeover path.
                _c2, _s2, _sh2, _p2, _o2 = keybinds_runtime.resolve_adv_config(
                    _sc_pc, hid_kind, SCButtons, sui.Keys, mode="pc")
                if _c2 or _s2 or _sh2 or _p2:
                    _adv_engine_pc = keybinds_runtime.AdvPressEngine(
                        _c2, _s2, _sh2, _p2,
                        timing=keybinds_runtime.adv_timing(
                            self.settings, hid_kind),
                        guide_bit=keybinds_runtime.adv_guide_mask(
                            hid_kind, SCButtons))
            if gamepad_active:
                _gp_binds = keybinds_runtime.gamepad_submap(_sc_binds)
                _gp_map, _gp_lt, _gp_rt = keybinds_runtime.resolve_sc_gamepad(
                    _gp_binds, SCButtons)
                _gp_lstick_map, _gp_rstick_map = \
                    keybinds_runtime.resolve_sc_gamepad_sticks(_gp_binds)
                # Gamepad-tab controls bound to a keyboard/mouse/system action
                # (dispatched by the watcher; excluded from _gp_map above).
                _gp_key_overrides = keybinds_runtime.resolve_sc_gamepad_keys(
                    _gp_binds, SCButtons, sui.Keys)
                # Advanced press actions (Long/Double/Soft rows)  the engine
                # owns those controls' press decisions; mask its owned bits
                # out of the plain button_map/key-override tables so a control
                # never double-acts.
                _adv_c, _adv_s, _adv_sh, _adv_p, _adv_owned = \
                    keybinds_runtime.resolve_adv_config(
                        _gp_binds, hid_kind, SCButtons, sui.Keys)
                if _adv_c or _adv_s or _adv_sh or _adv_p:
                    _adv_engine = keybinds_runtime.AdvPressEngine(
                        _adv_c, _adv_s, _adv_sh, _adv_p,
                        timing=keybinds_runtime.adv_timing(
                            self.settings, hid_kind),
                        guide_bit=keybinds_runtime.adv_guide_mask(
                            hid_kind, SCButtons))
                if _adv_owned:
                    _gp_map = [(b, a) for b, a in _gp_map
                               if not (b & _adv_owned)]
                    _gp_key_overrides = [(c, b, a) for c, b, a
                                         in _gp_key_overrides
                                         if not (b & _adv_owned)]
            watcher = _Watcher(
                self._should_abort_sc,
                kind=hid_kind,
                gamepad=self._persistent_gamepad if gamepad_active else None,
                chord=self._chord,
                takeover=not gamepad_active,
                chords=keybinds_runtime.build_chords(
                    keybinds_runtime.chords_for(
                        self.settings.get("chords", []), hid_kind),
                    SCButtons, sui.Keys),
                guide_chords=keybinds_runtime.build_guide_chords(
                    keybinds_runtime.chords_for(
                        self.settings.get("chords", []), hid_kind),
                    SCButtons, sui.Keys),
                sc_overrides=keybinds_runtime.resolve_sc_overrides(
                    _sc_pc, SCButtons, sui.Keys),
                lstick_mouse=_lstick_mouse,
                lstick_actions=_lstick,
                rstick_mouse=_rstick_mouse,
                rstick_actions=_rstick,
                guide_taps=keybinds_runtime.resolve_guide_taps(_sc_pc, sui.Keys),
                guide_taps_gp=keybinds_runtime.resolve_guide_taps(
                    keybinds_runtime.gamepad_submap(_sc_binds), sui.Keys,
                    keybinds_runtime.SC_GAMEPAD_DEFAULTS),
                on_toggle_gui=self.toggle_config_gui,
                guide_binds=keybinds_runtime.resolve_sc_guide(
                    _sc_guide, SCButtons, sui.Keys),
                guide_rstick_zones=keybinds_runtime.resolve_sc_guide_rstick(
                    _sc_guide, sui.Keys),
                guide_lstick_zones=keybinds_runtime.resolve_sc_guide_lstick(
                    _sc_guide, sui.Keys),
                gamepad_map=_gp_map,
                gamepad_lt_analog=_gp_lt,
                gamepad_rt_analog=_gp_rt,
                gamepad_lstick_map=_gp_lstick_map,
                gamepad_rstick_map=_gp_rstick_map,
                gamepad_toggle_masks=keybinds_runtime.build_gamepad_toggle_masks(
                    keybinds_runtime.chords_for(
                        self.settings.get("chords", []), hid_kind),
                    SCButtons),
                on_gamepad_toggle=self.handle_gamepad_toggle,
                on_mode_hold=self.handle_mode_hold,
                gyro_toggle_masks=keybinds_runtime.build_gyro_toggle_masks(
                    keybinds_runtime.chords_for(
                        self.settings.get("chords", []), hid_kind),
                    SCButtons),
                on_gyro_toggle=lambda held, _k=hid_kind:
                    self.handle_gyro_toggle(held, _k),
                gyro_active=lambda _k=hid_kind:
                    adusk_state.is_gyro_mouse_active(_k),
                gamepad_key_overrides=_gp_key_overrides,
                button_combos=keybinds_runtime.build_button_combos(
                    keybinds_runtime.chords_for(
                        self.settings.get("chords", []), hid_kind),
                    SCButtons, sui.Keys),
                on_profile_cycle=self.cycle_keybind_profile,
                adv_engine=_adv_engine,
                adv_engine_pc=_adv_engine_pc,
            )
            # block_sc_hid opens the physical Steam Controller HID exclusively so
            # Steam can't read it  applied in ALL modes (desktop AND gamepad), so
            # the toggle blocks Steam from the Steam Controller on its own. (It used
            # to also require block_gamepad_takeover in gamepad mode, which surprised
            # users: unchecking the Xbox toggle re-exposed the SC to Steam.) The two
            # blocks are now independent; block_gamepad_takeover hides the VIRTUAL
            # Xbox 360 pad from Steam separately (see _set_xbox_ignore).
            use_exclusive = self.settings["block_sc_hid"]
            # passive=False in BOTH modes now: it disables firmware lizard on open,
            # runs the watchdog that keeps it off, and restores it on close (so the
            # SC works as a normal mouse/kb again whenever our app isn't holding it).
            sc = SteamController(callback=watcher.on_input,
                                 passive=False,
                                 exclusive=use_exclusive,
                                 kinds=(hid_kind,),
                                 # Never re-open a controller a player-2+ reader
                                 # already holds. Evaluated at open time, not
                                 # now: this reader is rebuilt on every mode
                                 # change / OSK toggle / keybind save, and extras
                                 # connect and disconnect in between.
                                 exclude_paths=lambda: self._sc_extra_paths)
            self._current_sc = sc
            # New device instance starts with motors off; forget the last
            # forwarded rumble so the next FFB update is always re-applied.
            self._last_rumble = (None, None)
            # If an OSK preview is already pending (skin/slider dropdown opened
            # while we were in the reconnect wait), skip the SC probe entirely so
            # the OSK opens immediately instead of waiting up to 1.5 s per HID
            # candidate.  addExit() also interrupts a mid-flight probe (fix 2).
            if self._osk_preview:
                self._current_sc = None
            else:
                # If gamepad<->lizard just flipped, chime once on this device as
                # soon as it's open (a daemon waits for the open, then plays).
                if chime_now is not None:
                    self._start_chime(sc, chime_now)
                # While actively translating SC input to the virtual pad, hold a
                # process high-responsiveness request and boost THIS thread 
                # sc.run()'s blocking HID read, watcher.on_input, and the ViGEm
                # submit all execute right here. A fullscreen game makes us a
                # BACKGROUND process at exactly that moment, which is when
                # Windows EcoQoS would park/throttle this thread and add input
                # latency jitter (same starvation mode that broke the OSK mouse;
                # sdl_gamepad_thread already holds this for a connected SDL pad).
                # Scoped to gamepad mode so an SC idling on the desktop doesn't
                # pin the 1 ms timer. Reference-counted, so it composes with the
                # OSK/SDL holds (see adusk/power.py).
                if gamepad_active:
                    adusk_power.request()
                    adusk_power.boost_current_thread()
                try:
                    sc.run()
                except KeyboardInterrupt:
                    self._close_persistent_gamepad()
                    return
                finally:
                    if gamepad_active:
                        adusk_power.unboost_current_thread()
                        adusk_power.release()
                    self._current_sc = None
                # HID-family bookkeeping: a successful open makes this family
                # sticky (and unlocks its picker tab / Options category); a
                # silent probe with more than one family enumerable ROTATES the
                # preference to the next one so each gets tried in turn (an
                # asleep SC dongle must never starve the Deck's built-in pad).
                # Rotating rather than "pick the first other one" matters once
                # three families can be present at once  that would ping-pong
                # between two and never reach the third.
                if sc.opened:
                    self._hid_prefer = hid_kind
                    self._note_seen_controller(hid_kind)
                elif len(kinds_present) > 1:
                    try:
                        _i = kinds_present.index(hid_kind) + 1
                    except ValueError:
                        _i = 0
                    self._hid_prefer = kinds_present[_i % len(kinds_present)]

            if self._stop_event.is_set():
                return
            if self._steam_active.is_set():
                # Pause-for-Steam fired; loop back to wait state.
                continue
            # Open the keyboard on a controller Steam+X (watcher.triggered), a
            # Win+Ctrl+O hotkey request (_open_kbd_event), OR an Options-tab live
            # preview (_osk_preview, the user holding a Size/Transparency slider).
            open_kbd = (watcher.triggered or self._open_kbd_event.is_set()
                        or self._osk_preview)
            # This open is a preview iff the preview flag is the ONLY trigger 
            # then we show it instantly (no animation). If the slider was already
            # released before we got here (fast click), _osk_preview is False and
            # open_kbd is False too, so nothing opens (nothing to get stuck).
            preview_open = (self._osk_preview and not watcher.triggered
                            and not self._open_kbd_event.is_set())
            self._open_kbd_event.clear()
            if not open_kbd:
                # sc.run() returned without an open request. Two cases:
                if sc.opened:
                    # It opened and ran, so this was a deliberate kick (gamepad-
                    # mode toggle / focus change) or the device dropped mid-use.
                    reconnect_wait = RECONNECT_WAIT_MIN
                    if self._intentional_kick.is_set():
                        # Deliberate kick  rebuild immediately so the new mode
                        # (and its on/off chime) applies without a 1s lag.
                        self._intentional_kick.clear()
                    else:
                        # Unexpected drop mid-use  brief backoff before retry.
                        self._launcher_wait(RECONNECT_WAIT_MIN)
                else:
                    # Open failed  no controller present. Back off so we don't
                    # re-enumerate HID every second while it stays disconnected.
                    self._launcher_wait(reconnect_wait)
                    reconnect_wait = min(reconnect_wait * 2, RECONNECT_WAIT_MAX)
                continue

            # Steam+X or Win+Ctrl+O  reset the backoff and open the keyboard.
            reconnect_wait = RECONNECT_WAIT_MIN
            # Snapshot the window the user was typing in NOW, before the HID
            # handoff  the watcher sampled it just before the opening press, so
            # adusk can restore focus to it once the OSK is up (the controller-
            # open's firmware mouse-click can otherwise leave the field unfocused).
            # Steam Controller open → the watcher's sample; SDL-pad / hotkey
            # open → the window captured in toggle_keyboard_hotkey.
            # A live Size/Transparency PREVIEW is driven from our own picker
            # window, so there's nothing to "restore"  pulling the last user
            # app to the foreground would BURY the picker behind it (the slider
            # keeps tracking via Tk's pointer grab, so it just looks like the
            # GUI vanished). Leave focus on the picker; the NOACTIVATE OSK never
            # steals it.
            # The first-run tutorial is the same situation: its "press Steam+X"
            # step is a DEMONSTRATION staged inside our own window, so pulling
            # the user's last app forward would bury the tour behind it.
            if preview_open or sc_viewer.tutorial_claimed():
                restore_hwnd = None
            else:
                restore_hwnd = watcher._last_user_hwnd or self._pending_restore_hwnd
            self._pending_restore_hwnd = None
            # Start the OSK on the glyphs of the controller that opened it: a
            # Steam Controller Steam+X sets watcher.triggered; an SDL pad
            # (Switch Pro) tagged the pending open as "sdl". A non-controller
            # open (tray menu / Win+Ctrl+O) leaves it on the last-used controller.
            opener = hid_kind if watcher.triggered else self._pending_open_controller
            self._pending_open_controller = None
            if opener is not None:
                adusk_state.set_active_controller(opener)
            # Brief HID-handoff settle, then open the keyboard in-process.
            time.sleep(0.1)
            adusk_state.reset_session()
            # reset_session() just cleared the close flag  if the preview slider
            # was released during this handoff (a quick press/release), re-issue
            # the close so the preview OSK doesn't open and get stuck.
            if preview_open and not self._osk_preview:
                adusk_state.close()
            adusk_state.set_focus_restore_target(restore_hwnd)
            # Build/rebuild the cached Screen on THIS launcher thread (the render
            # thread) before handing it to adusk.main(): apply a Size change that
            # was requested while the OSK was closed, and cover the first open.
            if self._pending_size_change:
                self._pending_size_change = False
                self._rebuild_cached_screen()
            self._ensure_cached_screen()
            self._kbd_open = True
            # Publish it: the keyboard is always-on-top, so an overlay that
            # covers the manager (the tutorial) has to know when it is under it.
            sc_viewer.set_osk_open(True)
            self._refresh_menu()  # label → "Close Keyboard"
            try:
                adusk_app.main(cached_screen=self._cached_screen,
                               preview=preview_open)
            except Exception as e:
                print(f"adusk crashed: {e!r}")
            finally:
                # adusk.main() releases its own high-responsiveness hold at
                # teardown, but if it raised before reaching that, release here so
                # the process doesn't stay opted-out of EcoQoS / holding the 1 ms
                # timer forever. Reference-counted + clamped, so doing it in both
                # places on the normal path is a harmless no-op. Runs on the
                # launcher (OSK render) thread, so unboost targets that thread.
                adusk_power.unboost_current_thread()
                adusk_power.release()
                self._kbd_open = False
                sc_viewer.set_osk_open(False)
                # Clear the preview flag on EVERY close so it can't outlive the
                # window and make the next loop iteration immediately reopen.
                self._osk_preview = False
                # "Remember Per App": persist whatever the session recorded
                # (the spot the Move key landed on, plus a size/skin the user
                # switched to while this app was foreground). Drained here
                # rather than per change, so one close costs one write.
                self._persist_per_app_osk()
                self._refresh_menu()  # label → "Open Keyboard"
                # A "Size" change was selected while the OSK was open (the
                # cached Screen was busy on this thread)  rebuild it now so
                # the new size takes effect on the next open.
                if self._pending_size_change:
                    self._pending_size_change = False
                    self._rebuild_cached_screen()
            time.sleep(0.1)


def _load_icon_image():
    # Prefer the multi-resolution app_icon.ico (hand-tuned per size, so
    # the small tray frame is crisp). Falls back to the in-OSK keyboard
    # glyph PNG if the ico isn't present.
    base = os.path.join(_bundle_dir(), "data", "images")
    try:
        small = ctypes.windll.user32.GetSystemMetrics(49)  # SM_CXSMICON
    except Exception:
        small = 16
    target = max(small * 2, 32)  # 2× for HiDPI headroom

    ico_path = os.path.join(base, "app_icon.ico")
    if os.path.isfile(ico_path):
        ico = Image.open(ico_path)
        # Pick the smallest embedded frame that's >= target so we sharpen
        # by downscaling, not upscaling, then LANCZOS to the exact size.
        sizes = sorted(ico.info.get("sizes", [ico.size]))
        pick = next((s for s in sizes if s[0] >= target), sizes[-1])
        ico.size = pick
        return ico.convert("RGBA").resize((target, target), Image.LANCZOS)

    fallback = os.path.join(base, "glyphs", "glyph_keyboard.png")
    if os.path.isfile(fallback):
        return Image.open(fallback).convert("RGBA").resize(
            (target, target), Image.LANCZOS)
    raise FileNotFoundError("no tray icon found under data/images/")


# How long the first-launch reveal waits for the Keybinds window to finish
# building before giving up and showing the keyboard anyway. Generous: a cold
# first launch is the slowest possible build (nothing cached).
_FIRST_RUN_BUILD_TIMEOUT = 20.0


def _first_run_reveal(app):
    """First launch only: build the Keybinds GUI hidden, then reveal it with
    the guided tutorial running over it.

    ONLY the tutorial appears. Neither the manager nor the keyboard is opened
    alongside it: the keyboard is always-on-top and would sit over the tour
    (and its own slide teaches opening it, which is what makes the chord
    stick), and the manager is what the tour's welcome slide takes away in
    order to teach the tray icon  revealing it first only to hide it a second
    later was a flash of window a new user had no way to interpret. The warm
    build leaves it ghost-hidden, so start_tutorial(show=False) puts the tour
    on screen over a window that was never seen.

    Runs on its own daemon thread  the wait must not block the tray's setup
    callback.

    Bounded wait: if the build wedges we stop waiting and reveal whatever
    exists, so a new user is never left staring at a bare tray icon."""
    app._open_keybinds(warm=True)   # slow Tk build, hidden
    try:
        import keybinds_picker
    except Exception as e:
        print(f"first-run reveal: picker import failed: {e!r}")
        return

    deadline = time.monotonic() + _FIRST_RUN_BUILD_TIMEOUT
    while time.monotonic() < deadline:
        if keybinds_picker.picker_ready():
            break
        time.sleep(0.05)
    else:
        print("first-run reveal: timed out waiting for the picker")

    # show=False: the tour goes up over the still-hidden manager, so nothing
    # flashes on screen before it (see request_tutorial).
    if not keybinds_picker.start_tutorial(show=False):
        # No window at all  the build wedged. Open the manager so the launch
        # still lands somewhere useful; the keyboard stays shut either way.
        app._open_keybinds()


def main():
    app = App()
    image = _load_icon_image()

    # The tray menu is ACTIONS ONLY  every setting that used to live here
    # (Startup / Gamepad Mode / per-controller submenus / Keyboard Settings /
    # Advanced) moved into the Keybinds manager's Options tab. The App's
    # toggle_*/select_*/is_*_checked handlers are kept (they're the documented
    # live paths and some are shared by the Options apply flow)  only the
    # menu entries are gone.
    menu = pystray.Menu(
        pystray.MenuItem(
            app.battery_menu_label,
            None,
            enabled=False,
            visible=app.is_battery_known,
        ),
        pystray.MenuItem(
            app._kbd_menu_label,
            app.open_or_close_keyboard,
        ),
        pystray.MenuItem("Keybinds", app.open_keybinds, default=True),
        # Two state-driven ACTIONS (not settings  see the note above): the
        # things you reach for without wanting to open a window, typically
        # mid-game with a controller in hand. Both are rebuilt on the
        # tray_menu_thread tick so their tick marks stay honest against
        # changes made anywhere else.
        pystray.MenuItem("Game Mode", app.toggle_game_mode,
                         checked=app.is_game_mode_checked,
                         visible=app.game_mode_available),
        pystray.MenuItem("Virtual Menu",
                         pystray.Menu(app.virtual_menu_items)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", app.exit_app),
    )

    icon = pystray.Icon("SteamControllerKeyboard", image,
                        "SteamlessInput", menu)
    app._icon_ref = icon

    def open_gui_watch_thread():
        """Wait on the named open-GUI event: a SECOND launch of the exe sets
        it (see _ensure_single_instance) and exits, and this thread answers by
        opening the Keybinds manager  so double-clicking the exe while it's
        already running brings up the GUI instead of doing nothing. The event
        is auto-reset, so each launch = one open request."""
        if _open_gui_event is None:
            return
        k32 = ctypes.WinDLL("kernel32")
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        WAIT_OBJECT_0 = 0
        while True:
            # 2s timeout so a daemon-thread exit at shutdown isn't blocked
            # forever inside the native wait.
            if k32.WaitForSingleObject(_open_gui_event, 2000) == WAIT_OBJECT_0:
                try:
                    # Show-only (never toggle-hide an open window); falls back
                    # to a full open when the picker isn't built yet  which
                    # also queues the show if a warm build is still running.
                    import keybinds_picker
                    if not keybinds_picker.show_picker():
                        app._open_keybinds()
                except Exception as e:
                    print(f"open-gui signal failed: {e!r}")

    def setup(icon):
        icon.visible = True
        threading.Thread(target=app.launcher_thread, daemon=True).start()
        threading.Thread(target=app.steam_watch_thread, daemon=True).start()
        threading.Thread(target=app.auto_gamepad_thread, daemon=True).start()
        threading.Thread(target=app.sdl_gamepad_thread, daemon=True).start()
        threading.Thread(target=app.sc_extra_thread, daemon=True).start()
        threading.Thread(target=app.battery_thread, daemon=True).start()
        threading.Thread(target=app.tray_menu_thread, daemon=True).start()
        threading.Thread(target=open_gui_watch_thread, daemon=True).start()
        # Big Picture controller-connect automation (blocks while disabled).
        app._bp_engine.start()
        # Reclaim %TEMP% from onefile runs that were killed rather than exited
        # (see _sweep_orphan_meipass). Background + best-effort: it must never
        # delay the tray icon or be able to break startup.
        threading.Thread(target=_sweep_orphan_meipass, daemon=True,
                         name="meipass-sweep").start()
        # Global Win+Ctrl+O opens (or closes) the on-screen keyboard, so it can
        # be tried without a Steam Controller to press Steam+X. A raw
        # WH_KEYBOARD_LL hook, not pynput's GlobalHotKeys  that combo is ALSO
        # Windows' own built-in OSK shortcut, so the hook has to SWALLOW the
        # keystroke (see _start_osk_hotkey_hook) or both keyboards would open.
        try:
            app._osk_hotkey_hook_state = _start_osk_hotkey_hook(app)
        except Exception as e:
            print(f"osk hotkey hook failed to start: {e!r}")
        # Global Escape closes the on-screen keyboard when it's open.
        # Uses a raw WH_KEYBOARD_LL ctypes hook (not pynput Listener) so that
        # virtual Escape presses sent by pynput's own Controller are ignored via
        # LLKHF_INJECTED  pynput Listener + Controller on the same Escape key
        # causes eaten events on Windows.
        _esc_hook_state = _start_esc_hook(app)
        app._esc_hook_state = _esc_hook_state
        # Virtual Menus with a keyboard/mouse trigger ("Show Virtual Menu while
        # Holding Key")  opens the same overlay the trackpads drive, steered
        # with the mouse and arrow keys. Its low-level hooks stay UNINSTALLED
        # until a menu actually has such a trigger.
        try:
            app._key_vmenus = _KeyVMenuRunner(app)
            app._key_vmenus.start()
        except Exception as e:
            print(f"key virtual-menu runner failed to start: {e!r}")
        # First-ever launch (no settings.json found in __init__): open the
        # Keybinds GUI manager and run the guided tutorial over it (see
        # _first_run_reveal), so a new user lands somewhere other than a bare
        # tray icon AND leaves knowing the chords  which is the only way any
        # of them are discoverable. Nothing else auto-opens (no gamepad mode,
        # and no longer the OSK: the tour's first step opens that).
        # `tutorial_done` is belt-and-braces here  a fresh install can't have
        # it set  but it keeps "was the tour already seen" a single question.
        # Rasterize the controller-viewer art (pure PIL, ~1s) on its own
        # daemon thread NOW, so the picker's Tk-thread build below only wraps
        # the finished images into PhotoImages instead of paying the render.
        # Needed regardless of the Interactive Controller Preview toggle  OFF
        # still shows this same line-art, just frozen at rest (see
        # _paint_ctrl_canvas), not the older flat controller_triton.png.
        threading.Thread(target=sc_viewer.prewarm, daemon=True).start()
        if app._is_first_run and not app.settings.get("tutorial_done"):
            threading.Thread(target=_first_run_reveal, args=(app,),
                             daemon=True).start()
        else:
            # Pre-build the Keybinds GUI hidden a moment after startup (its
            # own Tk thread; the delay keeps controller/tray init snappy) so
            # the first tray click reveals an already-painted window instantly
            # instead of constructing four tabs of widgets while the user
            # watches. A click that lands mid-build is queued, not dropped.
            warm_t = threading.Timer(2.0, lambda: app._open_keybinds(warm=True))
            warm_t.daemon = True
            warm_t.start()

    try:
        icon.run(setup=setup)
    except OSError as e:
        # pystray's win32 backend can raise "[WinError 1401] Invalid menu
        # handle" while tearing down the tray menu during Exit (icon.stop()).
        # The app is already shutting down, so swallow that specific error to
        # avoid a spurious PyInstaller crash dialog; re-raise anything else.
        if getattr(e, "winerror", None) != 1401:
            raise


if __name__ == "__main__":
    main()
