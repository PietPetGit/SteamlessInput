"""Linux system-tray launcher for SteamlessInput.

Mirrors the structure of the Windows tray (tray.py) but limited to the
features that currently make sense on Linux:

  * Tray icon with menu (pystray on AppIndicator/Xorg)
  * Open the on-screen keyboard from the menu
  * Steam+X chord watcher (passive)  same behavior as adusk_linux.py
  * Win+Ctrl+O global hotkey (X11 only; silently no-ops on Wayland)
  * "Start at login" toggle (XDG autostart .desktop file)
  * "Pause / Exit when Steam is running" (mutually-exclusive submenu)
  * Settings persisted to settings.json next to the binary

Not yet ported (tracked separately): gamepad mode, auto gamepad mode,
ViGEm/uinput virtual gamepad, exclusive HID grab.
"""

import argparse
import atexit
import ctypes
import json
import math
import os
import shutil
import signal
import sys
import tempfile
import threading
import time


# --- Resource / path helpers ------------------------------------------------

def _is_frozen():
    return getattr(sys, "frozen", False)


def _bundle_dir():
    """Directory containing read-only bundled resources (data/, icon)."""
    if _is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _exe_dir():
    """Directory we treat as the install location  used for the settings
    file and as the working directory for the autostart entry."""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _exe_path():
    return os.path.abspath(sys.executable) if _is_frozen() else os.path.abspath(__file__)


# --- Crash capture ------------------------------------------------------------
# Mirrors windows/tray.py: when something aborts the process at the C level (a
# Tcl panic, a "Fatal Python error", an abort inside an extension), the one
# diagnostic line goes to a stderr nobody is watching and the app just
# vanishes. Route the C-level stderr (fd 2) into crash.log next to the binary
# in the frozen build and arm faulthandler, so a hard crash leaves the fatal
# message plus every thread's Python stack behind instead of nothing.
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
# Mirrors windows/tray.py (named mutex there): a second copy fights the first
# over the controller HID handles, uinput and the tray icon. Hold an exclusive
# flock on a lockfile for the process lifetime; a second launch prints and
# exits. The fd is kept open forever  the lock dies with the process, so a
# crash never leaves a stale lock behind.

def _ensure_single_instance():
    """flock-based single instance. A SECOND launch used to just print and
    exit  which read as "the program does nothing / the GUI refuses to open"
    (launching the binary again is the natural way to try to get the GUI
    back). Now it SIGNALS the running instance with SIGUSR1 (whose handler 
    registered in main()  opens the Keybinds manager) and exits silently, so
    re-launching always produces the GUI. The lock file carries the holder's
    PID for that signal."""
    try:
        import fcntl
        import signal as _signal
        path = os.path.join(tempfile.gettempdir(),
                            f"steamlesskeyboard-{os.getuid()}.lock")
        f = open(path, "a+")
        # A self-relaunch (Reset Settings writes fresh defaults and spawns a
        # new copy of the binary) can start the new process a beat before the
        # outgoing one has released its flock  that only happens when the fd
        # actually closes at process teardown, not the instant exit_app() is
        # called. Retry for up to ~3s before concluding it's a REAL second
        # launch; a genuine double-launch still resolves on the first try.
        locked = False
        for _attempt in range(15):
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                time.sleep(0.2)
        if not locked:
            # Another instance holds the lock  ask it to open the GUI.
            try:
                f.seek(0)
                pid = int((f.read().strip() or "0"))
                if pid > 0:
                    os.kill(pid, _signal.SIGUSR1)
                    print("SteamlessInput is already running  "
                          "asked it to open the Keybinds manager.",
                          file=sys.stderr)
                else:
                    raise ValueError("no pid in lock file")
            except Exception:
                print("SteamlessInput is already running  "
                      "check the system tray.", file=sys.stderr)
            sys.exit(0)
        # We own the lock: record our PID for future second launches.
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f
    except SystemExit:
        raise
    except Exception:
        return None


_single_instance_lock = _ensure_single_instance()   # held until process exit


# IMPORTANT: ADUSK_DATA must be set BEFORE importing adusk.*  adusk.resources
# captures it into a module-level tuple at import time.
os.environ.setdefault("ADUSK_DATA", os.path.join(_bundle_dir(), "data"))

# Force SDL onto the X11 backend (via XWayland) on Linux. Reasons:
#   - On a Wayland session SDL2 picks its native Wayland backend by
#     default. That backend creates an xdg_toplevel surface which the
#     compositor (KWin under Plasma 6) gives keyboard focus on map  the
#     OSK ends up stealing focus from whichever app the user was typing
#     in, so synthetic keystrokes land in the OSK and disappear.
#   - There is no portable Wayland "don't focus me" hint usable from
#     plain xdg-shell; the proper protocol (wlr_layer_shell_v1) isn't
#     bound by pysdl2. Routing through XWayland lets us reuse the X11
#     WM_HINTS.input=False + _NET_WM_WINDOW_TYPE_DOCK trick that KWin
#     does honor for XWayland clients (see adusk._make_window_no_focus_x11).
# Set before any sdl2 import so SDL_Init picks the right driver.
if sys.platform.startswith("linux"):
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")

# Prefer pystray's appindicator backend (renders correctly on most desktops),
# but fall back to the legacy xorg/XEmbed backend if importing it fails. The
# appindicator backend pulls in the GTK3 / AppIndicator / GObject-Introspection
# typelibs, which aren't bundled in the frozen build and aren't installed by
# default on minimal sessions (e.g. Fedora KDE)  without them pystray's import
# crashes with "Namespace ... not available". The xorg backend uses the older
# XEmbed protocol and only needs an X11/XWayland display; its icon rendering has
# quirks (alpha pasted onto a black RGB background, no auto-scaling) that we work
# around in _load_icon_image.
def _ensure_system_gi_typelibs():
    """Make the host distro's GObject-Introspection typelibs findable.

    The tray's AppIndicator/GTK/libnotify backend needs GI typelibs (Gtk-3.0,
    AyatanaAppIndicator3, Notify, Gio, GdkPixbuf, ...). By design we do NOT
    bundle the GTK native stack  like libSDL3/libhidapi it comes from the host
    (see build_linux.py). But PyInstaller's gi runtime hook still points
    GI_TYPELIB_PATH only at the binary's own (incomplete) typelib dir, so even
    after the user installs the system packages the frozen `gi` can't see them
    and pystray import dies with "Namespace ... not available" (issue #6).

    Fix: PREPEND the distro's girepository-1.0 dir(s) so the user's installed,
    self-consistent typelib set is searched first (the bundle dir stays a
    trailing fallback). System-first matters  a typelib pulled from the Arch
    build host would dlopen Arch sonames absent on the user's distro. Must run
    BEFORE gi is first imported (i.e. before pystray)."""
    if not sys.platform.startswith("linux"):
        return
    candidates = (
        "/usr/lib64/girepository-1.0",                 # Fedora / RHEL / openSUSE
        "/usr/lib/x86_64-linux-gnu/girepository-1.0",  # Debian / Ubuntu
        "/usr/lib/girepository-1.0",                   # Arch / generic / multilib
        "/usr/local/lib64/girepository-1.0",
        "/usr/local/lib/girepository-1.0",
    )
    found = [d for d in candidates if os.path.isdir(d)]
    if not found:
        return
    current = os.environ.get("GI_TYPELIB_PATH", "")
    tail = [p for p in current.split(os.pathsep) if p and p not in found]
    os.environ["GI_TYPELIB_PATH"] = os.pathsep.join(found + tail)


def _print_missing_tray_deps(err):
    """Both pystray backends failed  almost always the host is missing the GTK /
    AppIndicator / libnotify stack the tray needs. Print an actionable,
    distro-aware hint to stderr before the traceback so the user isn't left with
    a bare "Namespace ... not available" (issue #6)."""
    print(
        "\nSteamlessInput: couldn't start the system tray  the GTK /\n"
        f"AppIndicator / notification libraries are missing ({type(err).__name__}: {err}).\n"
        "Install them for your distro, then run it again:\n"
        "  Fedora/RHEL:   sudo dnf install gtk3 gobject-introspection "
        "libnotify libayatana-appindicator-gtk3\n"
        "  Arch/CachyOS:  sudo pacman -S gtk3 gobject-introspection "
        "libnotify libayatana-appindicator\n"
        "  Debian/Ubuntu: sudo apt install gir1.2-gtk-3.0 "
        "gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7\n"
        "Or run headless without a tray icon:  ./SteamlessInput --no-tray\n",
        file=sys.stderr,
    )


def _tk_install_hint():
    """Best-effort, distro-specific command to install Python's Tk bindings,
    parsed from /etc/os-release. Tk (system Tcl/Tk + the _tkinter C extension)
    is an optional OS package on Linux  unlike Windows, where CPython bundles
    it  so the "Keybinds" picker can't open until it's installed. Falls back to
    Arch/CachyOS, this project's reference distro."""
    try:
        ids = ""
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith(("ID=", "ID_LIKE=")):
                    ids += " " + line.split("=", 1)[1].strip().strip('"').lower()
        if any(d in ids for d in ("debian", "ubuntu", "mint")):
            return "sudo apt install python3-tk"
        if any(d in ids for d in ("fedora", "rhel", "centos")):
            return "sudo dnf install python3-tkinter"
        if "suse" in ids:
            return "sudo zypper install python3-tk"
    except Exception:
        pass
    return "sudo pacman -S tk"


def _import_pystray():  # noqa: E302
    import importlib
    # Point gi at the host's typelibs first  the frozen build doesn't bundle the
    # GTK stack (issue #6). Must happen before the first gi import below.
    _ensure_system_gi_typelibs()
    last_err = None
    for backend in ("appindicator", "xorg"):
        os.environ["PYSTRAY_BACKEND"] = backend
        for mod in [m for m in list(sys.modules) if m == "pystray" or m.startswith("pystray.")]:
            del sys.modules[mod]
        try:
            return importlib.import_module("pystray")
        except Exception as e:  # GI typelib missing, no display, etc.
            last_err = e
    _print_missing_tray_deps(last_err)
    raise last_err


# pystray is imported lazily in main() (tray mode only) so --no-tray still runs
# on a session without the GTK/AppIndicator stack. See _import_pystray.
pystray = None  # noqa: E402
from PIL import Image  # noqa: E402

import sdl3w as S  # noqa: E402
import steamcontroller.uinput as sui  # noqa: E402
from steamcontroller import SCButtons  # noqa: E402
from steamcontroller import GYRO_DEG_PER_SEC  # noqa: E402  (raw gyro → °/s)
from adusk import adusk as adusk_app  # noqa: E402
from adusk import inputsrc as adusk_inputsrc  # noqa: E402
from adusk import power as adusk_power  # noqa: E402
from adusk import key_sound as adusk_key_sound  # noqa: E402
from adusk import screen as adusk_screen  # noqa: E402
from adusk import skins as adusk_skins  # noqa: E402
from adusk import state as adusk_state  # noqa: E402
import keybinds_runtime  # noqa: E402  (import-safe: no tkinter/SDL)
import pads  # noqa: E402  (import-safe: controller catalog + identification)
import sc_viewer  # noqa: E402  (import-safe: PIL only  publish slot for the
                  # keybinds picker's live controller preview)
import big_picture  # noqa: E402  (import-safe: subprocess/glob only  the
#                     Options → Big Picture controller-connect automation)


# --- Constants --------------------------------------------------------------

SETTINGS_FILENAME = "settings.json"
AUTOSTART_DESKTOP_NAME = "SteamlessInput.desktop"
TRAY_TITLE = "SteamlessInput"

# Buttons the keybinds picker consumes for controller UI navigation while its
# window is visible + foreground (sc_viewer.nav_claimed()): dpad moves the
# highlight, A activates, B cancels, X removes / resets, Y toggles fine slider
# adjust, Menu/≡ (START) opens value entry / Listen bind mode, LB/RB cycle the
# tab pills. Masked out of on_input's dispatch so they don't double-fire the
# desktop binds.
_PICKER_NAV_MASK = (SCButtons.A | SCButtons.B
                    | SCButtons.X | SCButtons.Y
                    | SCButtons.START
                    | SCButtons.DPAD_UP | SCButtons.DPAD_DOWN
                    | SCButtons.DPAD_LEFT | SCButtons.DPAD_RIGHT
                    | SCButtons.LB | SCButtons.RB)

# While the picker's "Listen" bind-capture is armed (sc_viewer.listen_claimed())
# EVERY button bit is masked out of dispatch  the press about to be captured
# as a binding must not also fire its desktop action. Only the touch SENSORS
# survive (they gate trackpad-mouse motion, and a rest/touch is not a
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
    "start_at_login": True,
    "disable_while_steam_running": True,
    "exit_on_steam_launch": False,
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
    # Per-controller haptics  gates the OSK's UI click feedback (and rumble) for
    # that controller. Each controller's tray submenu has its own Vibration
    # toggle (no global switch). "sc" = Steam Controller, "switch" = Switch Pro.
    "rumble_enabled_sc": True,
    "rumble_enabled_switch": True,
    # Options → Switch Pro "Bluetooth Safe Mode". Nintendo pads hand every
    # non-Switch host a crippled Bluetooth mode (sniff mode), whose thin
    # bandwidth our rumble traffic  and the IMU flood the rumble itself
    # provokes  saturates until the link drops: the notorious ~20-minute
    # Joy-Con/Pro Controller dropout. On, we pace the rumble packets we send
    # those pads and trickle a keepalive so the link stays up. Applies ONLY to
    # Nintendo pads on Bluetooth; over USB-C nothing is touched. The complete
    # fixes are outside our reach  a USB-C cable, or telling BlueZ to call
    # itself Nintendo (`bluetoothctl system-alias Nintendo`), which is what the
    # controller sniffs for. See nintendo_bt.py.
    "nintendo_bt_safe": True,
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
    # like the Steam Deck). Each OSK open builds a fresh Screen(), which
    # picks this up automatically.
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
    "kbd_lstick_mouse": True,
    # "Mouse Controls"  mouse/right-stick can hover and click OSK keys.
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
    # passes it through unchanged.
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
    # L2/R2 GAMEPAD-mode actuation  the analog pull at which the triggers fire
    # their gamepad output. Windows-only runtime effect (no SC virtual-pad path
    # on Linux); persisted here for parity so the Options page round-trips.
    "sc_gamepad_trigger_actuation": "default",
    # Right-stick mouse pointer speed. Tray menu stores a named level "low" /
    # "medium" (default) / "high"; the Options-tab gradual slider stores a float
    # multiplier (see _sc_speed_mult) anchored to those same endpoints.
    "sc_pointer_speed": "medium",
    # SC desktop-takeover trackpad speeds (tray "Steam Controller" submenu):
    # right trackpad → cursor, left trackpad → scroll wheel. "low"/"medium"/"high".
    "sc_trackpad_speed": "medium",
    "sc_scroll_speed": "medium",
    # Left-trackpad scroll style (Options → Touchpads): "normal" = direct 1:1
    # wheel notches only; "laptop" = a quick swipe also sets the page coasting
    # with a smooth deceleration (kinetic scrolling), caught with a gentle tap.
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
    # "Right Touchpad Tap to Click" (Options → Touchpads): a quick, still
    # touch-and-lift on the RIGHT pad = a left click  the laptop touchpad
    # tap. Double-tap = double-click (the shake-freeze keeps the two clicks
    # inside the double-click slop).
    "tap_to_click": False,
    # "Left Touchpad Tap to Click" (Options → Touchpads): the left-pad twin
    # of the above  a quick, still touch-and-lift on the LEFT pad fires a
    # RIGHT click instead.
    "tap_to_click_left": False,
    # "Trackpad Keyboard Typing Mode" (Options → Touchpads): ONE dropdown picking how
    # the trackpads drive the on-screen keyboard. The three behaviours it
    # selects used to be independent toggles (release_to_type / touch_typing /
    # swipe_typing); they are strictly escalating variations on the same
    # gesture, so a single-select mode replaced them. See TYPING_MODE_FLAGS
    # for what each one switches on, and _load_settings for the migration off
    # the old booleans.
    "typing_mode": "default",
    # "Video Timeline Scrubbing" (Options → Touchpads): while a video is
    # focused (YouTube for now), the left trackpad becomes a circular timeline
    # dial  clockwise scrubs forward, counter-clockwise back. "off" / "frame"
    # (precise, pauses per-frame) / "seek" (fast 5s-per-detent, no pause).
    "video_scrub": "off",
    # Switch Pro Controller submenu: pointer speed only.
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
    # When False the Debug submenu is hidden; toggled via the "Debug menu"
    # item in the Startup submenu. Mirrors windows/tray.py.
    "debug_menu_unlocked": False,
    # "Block SteamInput Steam Controller grab" (Debug submenu): opens the
    # physical Steam Controller HID exclusively so Steam can't read it. See
    # [[block-steam-exclusive-hid]].
    "block_sc_hid": False,
    # Per-controller keybind layout (tray "Keybinds" picker). Nested dict
    # {"sc": {...}, "switch": {...}}; {} = built-in defaults
    # (keybinds_picker.default_binds). Saved only for now; wiring it into the
    # live input path is a follow-up.
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
    # Two-button SC desktop chords (tray "Keybinds" → Chords): list of
    # {"buttons":[a,b],"type":"keys","keys":[...]} or
    # {...,"type":"launch","path":...,"args":...}. A list, not a bool.
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


# --- Settings ---------------------------------------------------------------

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
        for k, val in data.items():
            if k not in DEFAULT_SETTINGS:
                continue
            merged[k] = bool(val) if isinstance(DEFAULT_SETTINGS[k], bool) else val
        # The two-level "low"(6000)/"lower"(3000) actuation collapsed to a single
        # "low" using the lighter 3000 pull  fold a saved "lower" into "low".
        if merged.get("sc_osk_trigger_actuation") == "lower":
            merged["sc_osk_trigger_actuation"] = "low"
        # The single global "rumble_enabled" split into per-controller toggles 
        # seed both from the old value so a saved preference carries over.
        if "rumble_enabled" in data:
            on = bool(data["rumble_enabled"])
            merged["rumble_enabled_sc"] = on
            merged["rumble_enabled_switch"] = on
        # OSK transparency went from a named level to a continuous 0..1 fraction 
        # migrate old "off"/"low"/"medium"/"high" strings to their notch positions.
        tv = merged.get("osk_transparency")
        if isinstance(tv, str):
            merged["osk_transparency"] = _OSK_TRANSP_NAME_FRAC.get(tv, 0.0)
        elif not isinstance(tv, (int, float)):
            merged["osk_transparency"] = 0.0
        # The three independent typing toggles collapsed into the single
        # "Trackpad Keyboard Typing Mode" dropdown. Only when the file predates the new
        # key, so a genuine "default" choice isn't overwritten by stale booleans
        # that DEFAULT_SETTINGS no longer carries. Most-specific wins, matching
        # how the old combinations actually behaved: swipe widened both pads and
        # overrode touch typing's fixed halves, and touch typing already implied
        # lift-to-type.
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
    except (OSError, ValueError, AttributeError, TypeError):
        # ValueError covers json.JSONDecodeError (its subclass) and the
        # not-a-dict raise above; Attribute/TypeError catch a structurally
        # valid but wrongly-shaped file. A corrupt settings.json must never
        # be able to stop the app from starting.
        return _seed_default_gyro_chords(dict(DEFAULT_SETTINGS))


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


# --- XDG autostart ----------------------------------------------------------

def _autostart_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart")


def _autostart_path():
    return os.path.join(_autostart_dir(), AUTOSTART_DESKTOP_NAME)


def _xdg_icon_path():
    """Persistent path for the app icon. ~/.local/share/icons is on the
    standard freedesktop icon search path, and absolute paths in .desktop
    Icon= fields are honored by KDE/GNOME, so referencing this file from
    autostart entries gives them the real app icon instead of the generic
    application fallback."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "icons", "SteamlessInput.png")


def _install_xdg_icon():
    """Write the bundled app icon to the XDG icon dir if missing. Called
    at startup so the autostart entry's Icon= path resolves on the first
    launch after install.

    Uses the LARGEST embedded .ico frame (typically 256x256) so the desktop
    launcher / autostart icon stays crisp at any size KDE/GNOME renders it.
    `_open_app_icon()` picks a small tray-sized frame which would look blurry
    when scaled up to 48–96px desktop icon cells."""
    path = _xdg_icon_path()
    if os.path.exists(path):
        return path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ico_path = os.path.join(_bundle_dir(), "data", "images", "app_icon.ico")
        if os.path.exists(ico_path):
            img = Image.open(ico_path)
            sizes = sorted(img.info.get("sizes", set()))
            if sizes:
                img.size = max(sizes)  # largest by width
                img.load()
            if img.mode != "RGBA":
                img = img.convert("RGBA")
        else:
            img = _open_app_icon()
            if img is None:
                return None
        img.save(path, "PNG")
        return path
    except Exception as e:
        print(f"xdg icon install failed: {e}")
        return None


def _apply_autostart(enabled):
    """Write or remove ~/.config/autostart/SteamlessInput.desktop. The
    Exec line points at the frozen binary when bundled, or at `python
    tray_linux.py` when running from source  same convention as tray.py
    on Windows."""
    path = _autostart_path()
    if not enabled:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"autostart remove failed: {e}")
        return

    if _is_frozen():
        exec_line = _exe_path()
    else:
        exec_line = f"{sys.executable} {_exe_path()}"

    icon_path = _install_xdg_icon()
    icon_line = f"Icon={icon_path}\n" if icon_path else ""

    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={TRAY_TITLE}\n"
        f"Exec={exec_line}\n"
        f"{icon_line}"
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )
    try:
        os.makedirs(_autostart_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(contents)
        os.chmod(path, 0o644)
    except OSError as e:
        print(f"autostart write failed: {e}")


# --- Steam-running detection ------------------------------------------------

def _steam_running():
    """True iff a Steam client process is alive. Scans /proc for processes
    whose comm/cmdline matches the Linux Steam launcher. The official
    package launches via /usr/bin/steam (a shell wrapper) and `steam.sh`,
    and the native client binary is `steamwebhelper`/`steam`. We match the
    common names; the wrapper script normally stays alive as the parent of
    the running session, which is what we actually care about."""
    targets = ("steam", "steam.sh", "steamwebhelper")
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in targets:
            return True
    return False


# --- Icon -------------------------------------------------------------------

TRAY_ICON_NAME = "SteamlessInput"


def _open_app_icon():
    """Open the bundled app icon. Returns a PIL RGBA Image, or None if the
    icon file can't be loaded."""
    base = os.path.join(_bundle_dir(), "data", "images")
    for candidate in ("app_icon.ico", "glyphs/glyph_keyboard.png"):
        path = os.path.join(base, candidate)
        if not os.path.exists(path):
            continue
        try:
            img = Image.open(path)
        except Exception:
            continue
        # For .ico, pick the closest-to-tray-size frame to avoid the
        # 256x256 default getting downscaled by GTK (which sometimes drops
        # to a blurry blob). PIL's ICO plugin honors `size` setter to load
        # a specific embedded frame.
        if path.endswith(".ico"):
            try:
                sizes = sorted(img.info.get("sizes", set()))
                # 24/22px tray cells  prefer 24 then 32 then anything.
                pick = None
                for target in (24, 32, 22, 48, 16, 64):
                    for s in sizes:
                        if s[0] == target:
                            pick = s
                            break
                    if pick:
                        break
                if pick:
                    img.size = pick
                    img.load()
            except Exception:
                pass
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        return img
    return None


def _load_icon_image():
    """PIL image handed to pystray.Icon(). Required by the constructor even
    on the AppIndicator backend, which actually renders the icon via
    set_icon_full() against our temp theme path."""
    img = _open_app_icon()
    if img is None:
        # Last-ditch placeholder so pystray doesn't choke.
        return Image.new("RGB", (24, 24), (60, 90, 160))
    return img


def _install_tray_icon_theme():
    """Write the bundled app icon as PNG into a stable per-user temp dir
    and return (theme_dir, icon_name). The directory is then passed to
    AppIndicator via set_icon_theme_path(); KDE Plasma 6 will resolve the
    bare icon name against it.

    Layout: theme_dir/SteamlessInput.png at the top level  flat layout
    is the simplest form GTK's icon-theme loader accepts as a search root,
    and it avoids us having to write index.theme/subdir indices."""
    theme_dir = os.path.join(
        tempfile.gettempdir(), f"SteamlessInput-tray-{os.getuid()}")
    try:
        os.makedirs(theme_dir, exist_ok=True)
    except OSError as e:
        print(f"tray: theme dir create failed: {e}")
        return None, None

    icon_path = os.path.join(theme_dir, f"{TRAY_ICON_NAME}.png")
    img = _open_app_icon()
    if img is None:
        return None, None
    try:
        img.save(icon_path, "PNG")
    except Exception as e:
        print(f"tray: icon save failed: {e}")
        return None, None
    return theme_dir, TRAY_ICON_NAME


# --- X11 focused-window helpers --------------------------------------------
#
# Used by the Steam+B "force-kill foreground game" chord. Resolves the
# active window's owning process via _NET_ACTIVE_WINDOW + _NET_WM_PID on
# the X11 root. Works for any XWayland or native-X11 client; native
# Wayland-only apps don't have an X11 window so this gracefully returns
# None for them (KWin's killWindow D-Bus would be the Wayland-native
# path; not bothering until a user reports it's needed).

_libx11_cache = None


def _libx11():
    global _libx11_cache
    if _libx11_cache is not None:
        return _libx11_cache
    try:
        lib = ctypes.cdll.LoadLibrary("libX11.so.6")
    except OSError:
        return None
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,            # display
        ctypes.c_ulong,             # window
        ctypes.c_ulong,             # property atom
        ctypes.c_long,              # long_offset
        ctypes.c_long,              # long_length
        ctypes.c_int,               # delete
        ctypes.c_ulong,             # req_type
        ctypes.POINTER(ctypes.c_ulong),  # actual_type_return
        ctypes.POINTER(ctypes.c_int),    # actual_format_return
        ctypes.POINTER(ctypes.c_ulong),  # nitems_return
        ctypes.POINTER(ctypes.c_ulong),  # bytes_after_return
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),  # prop_return
    ]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XFree.argtypes = [ctypes.c_void_p]
    _libx11_cache = lib
    return lib


def _x11_get_prop(lib, display, window, prop, expected_type, expected_format):
    """Read a single X11 property as a list of ints. Returns [] on any
    failure / mismatch."""
    actual_type = ctypes.c_ulong(0)
    actual_format = ctypes.c_int(0)
    nitems = ctypes.c_ulong(0)
    bytes_after = ctypes.c_ulong(0)
    prop_ret = ctypes.POINTER(ctypes.c_ubyte)()
    rc = lib.XGetWindowProperty(
        display, window, prop, 0, 1024, 0, expected_type,
        ctypes.byref(actual_type), ctypes.byref(actual_format),
        ctypes.byref(nitems), ctypes.byref(bytes_after),
        ctypes.byref(prop_ret),
    )
    if rc != 0 or not prop_ret:
        return []
    try:
        if actual_format.value != expected_format or actual_type.value != expected_type:
            return []
        if expected_format == 32:
            arr = ctypes.cast(prop_ret,
                              ctypes.POINTER(ctypes.c_ulong * nitems.value))
            return list(arr.contents)
        # 8/16-bit not used here
        return []
    finally:
        lib.XFree(prop_ret)


def _get_focused_window_pid():
    """PID of the process owning the currently-focused X11/XWayland
    window, or None. The Steam+B chord uses this to kill the foreground
    game on Linux."""
    lib = _libx11()
    if lib is None:
        return None
    display = lib.XOpenDisplay(None)
    if not display:
        return None
    try:
        root = lib.XDefaultRootWindow(display)
        atom_active = lib.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0)
        atom_pid = lib.XInternAtom(display, b"_NET_WM_PID", 0)
        XA_WINDOW = 33
        XA_CARDINAL = 6
        active = _x11_get_prop(lib, display, root, atom_active, XA_WINDOW, 32)
        if not active or not active[0]:
            return None
        win = active[0]
        pid_vals = _x11_get_prop(lib, display, win, atom_pid, XA_CARDINAL, 32)
        if not pid_vals:
            return None
        return int(pid_vals[0])
    finally:
        lib.XCloseDisplay(display)


def _x11_get_prop_bytes(lib, display, window, prop, expected_type):
    """Read an 8-bit-format X11 property as bytes (b"" on any failure /
    mismatch)  the string sibling of _x11_get_prop."""
    actual_type = ctypes.c_ulong(0)
    actual_format = ctypes.c_int(0)
    nitems = ctypes.c_ulong(0)
    bytes_after = ctypes.c_ulong(0)
    prop_ret = ctypes.POINTER(ctypes.c_ubyte)()
    rc = lib.XGetWindowProperty(
        display, window, prop, 0, 1024, 0, expected_type,
        ctypes.byref(actual_type), ctypes.byref(actual_format),
        ctypes.byref(nitems), ctypes.byref(bytes_after),
        ctypes.byref(prop_ret),
    )
    if rc != 0 or not prop_ret:
        return b""
    try:
        if actual_format.value != 8 or actual_type.value != expected_type:
            return b""
        return bytes(bytearray(prop_ret[:nitems.value]))
    finally:
        lib.XFree(prop_ret)


def _get_focused_window_title():
    """Title of the currently-focused X11/XWayland window ("" if none) via
    _NET_WM_NAME (UTF-8), falling back to legacy WM_NAME. Used by the Video
    Timeline Scrubbing focus check  browser tabs carry the site name in the
    title (e.g. "... - YouTube  Mozilla Firefox"). Native Wayland-only
    windows return "" here, same graceful degradation as the Steam+B kill."""
    lib = _libx11()
    if lib is None:
        return ""
    display = lib.XOpenDisplay(None)
    if not display:
        return ""
    try:
        root = lib.XDefaultRootWindow(display)
        atom_active = lib.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0)
        XA_WINDOW = 33
        active = _x11_get_prop(lib, display, root, atom_active, XA_WINDOW, 32)
        if not active or not active[0]:
            return ""
        win = active[0]
        atom_name = lib.XInternAtom(display, b"_NET_WM_NAME", 0)
        atom_utf8 = lib.XInternAtom(display, b"UTF8_STRING", 0)
        raw = _x11_get_prop_bytes(lib, display, win, atom_name, atom_utf8)
        if not raw:
            XA_STRING = 31
            atom_wm = lib.XInternAtom(display, b"WM_NAME", 0)
            raw = _x11_get_prop_bytes(lib, display, win, atom_wm, XA_STRING)
        return raw.decode("utf-8", "replace") if raw else ""
    finally:
        lib.XCloseDisplay(display)


def _kill_focused_window_process():
    """Steam+B: terminate the focused window's process. SIGTERM first
    (lets the app save / clean up); if it's still around 800 ms later,
    SIGKILL. Returns the pid we acted on (or None on failure)."""
    pid = _get_focused_window_pid()
    if pid is None or pid == os.getpid():
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except PermissionError:
        print(f"Steam+B: no permission to kill pid {pid}")
        return None

    def _followup():
        time.sleep(0.8)
        try:
            os.kill(pid, 0)  # alive?
        except ProcessLookupError:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    threading.Thread(target=_followup, daemon=True).start()
    return pid


# --- Plasma 6 KWin scripting fallback for Steam+B --------------------------
#
# Native Wayland clients (Konsole, Kate, etc. on Plasma 6) don't show up
# in the X11 _NET_ACTIVE_WINDOW atom, so the libX11 path above silently
# returns None for them. KWin's D-Bus scripting interface exposes the
# Wayland-aware `workspace.activeWindow`. We tried `w.kill()` directly
# from the script  the call returns silently without killing the
# process in Plasma 6.0/6.1 (Window.kill is a C++ slot, not a scriptable
# method on every version). Instead we have the script print the pid to
# journald with a per-call UUID marker, read it back via journalctl,
# and SIGTERM/SIGKILL the pid ourselves.

import shlex
import subprocess
import uuid

_KWIN_PID_SCRIPT_TEMPLATE = """\
var w = workspace.activeWindow;
if (w !== null && w !== undefined) {{
    print("STEAMLESS-MARKER-{marker}: pid=" + w.pid);
}} else {{
    print("STEAMLESS-MARKER-{marker}: no active window");
}}
"""


def _get_focused_window_pid_via_kwin(timeout=0.6):
    """Plasma 6 path. Returns the focused window's pid, or None."""
    marker = uuid.uuid4().hex
    script_path = os.path.join(
        tempfile.gettempdir(), f"steamless-killwin-{os.getuid()}.js")
    try:
        with open(script_path, "w") as f:
            f.write(_KWIN_PID_SCRIPT_TEMPLATE.format(marker=marker))
    except OSError as e:
        print(f"Steam+B: KWin script write failed: {e}")
        return None

    plugin = "steamlesskeyboard-killwin"
    base = ["qdbus6", "org.kde.KWin", "/Scripting"]
    try:
        subprocess.run(base + ["org.kde.kwin.Scripting.unloadScript", plugin],
                       check=False, capture_output=True, timeout=2)
        subprocess.run(base + ["org.kde.kwin.Scripting.loadScript",
                               script_path, plugin],
                       check=False, capture_output=True, timeout=2)
        subprocess.run(base + ["org.kde.kwin.Scripting.start"],
                       check=False, capture_output=True, timeout=2)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"Steam+B: KWin script invoke failed: {e}")
        return None

    # Poll the user journal for the marker line. KWin's print() output
    # gets buffered through journald  usually <100 ms but allow more on
    # a busy box. We bound the wait so the chord doesn't hang.
    deadline = time.time() + timeout
    pid = None
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["journalctl", "--user", "-n", "20",
                 "--since", "5 seconds ago", "--no-pager", "-o", "cat"],
                capture_output=True, timeout=1, text=True,
            ).stdout
        except Exception:
            break
        token = f"STEAMLESS-MARKER-{marker}: pid="
        for line in out.splitlines():
            i = line.find(token)
            if i >= 0:
                try:
                    pid = int(line[i + len(token):].split()[0])
                except (ValueError, IndexError):
                    pid = None
                break
        if pid is not None:
            break
        time.sleep(0.05)

    try:
        subprocess.run(base + ["org.kde.kwin.Scripting.unloadScript", plugin],
                       check=False, capture_output=True, timeout=2)
    except Exception:
        pass
    return pid


def _kill_pid_term_then_kill(pid):
    """SIGTERM, then SIGKILL 800 ms later if still alive."""
    if pid is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError:
        print(f"Steam+B: no permission to kill pid {pid}")
        return False

    def _followup():
        time.sleep(0.8)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    threading.Thread(target=_followup, daemon=True).start()
    return True


def _kill_focused_window():
    """Combined entry point used by Steam+B. Tries the cheap X11 lookup
    first, falls back to the KWin scripting + journald pid round-trip
    for native Wayland windows. Returns a short status string."""
    pid = _kill_focused_window_process()
    if pid is not None:
        return f"x11 pid={pid}"
    pid = _get_focused_window_pid_via_kwin()
    if pid is None:
        return "no focused window found"
    if _kill_pid_term_then_kill(pid):
        return f"kwin pid={pid}"
    return f"kwin pid={pid} (kill failed)"


def _launch_program(path, args=""):
    """Launch a program/script for a user-configured chord (Linux). With args,
    run the path + parsed args directly; otherwise hand the path to xdg-open so
    files/URLs/desktop entries open with their default handler. Non-blocking and
    failure-tolerant (runs on the HID read thread).

    Expands a $HOME token itself before either branch  a portable launch
    target (see _tokenize_home_path in keybinds_runtime.py, used for e.g. a
    per-user Spotify/Discord install) stores it unexpanded so the SAME saved
    config resolves correctly on whichever account actually runs it;
    subprocess.Popen never invokes a shell here, so nothing else would expand
    it for us.

    `path` may also be one of two sentinels, both resolved HERE (at fire time,
    not config time) so the shipped default menu works on a machine that looks
    nothing like the one it was authored on:
      * VMENU_LAUNCH_DEFAULT_BROWSER  whatever browser is ACTUALLY the OS
        default right now, launched bare; any stored args are ignored, since
        the whole point is opening the browser's own start page, not a fixed
        site.
      * VMENU_LAUNCH_STEAM  Steam as installed here (PATH, else the
        Flatpak's exported launcher). This one KEEPS its args
        ("-bigpicture"), so it falls through to the normal launch below
        instead of returning early."""
    try:
        path = (path or "").strip()
        if path == keybinds_runtime.VMENU_LAUNCH_DEFAULT_BROWSER:
            exe = keybinds_runtime.resolve_default_browser_exe()
            if exe:
                subprocess.Popen([exe])
            return
        if path == keybinds_runtime.VMENU_LAUNCH_STEAM:
            path = keybinds_runtime.resolve_steam_exe() or ""
        path = os.path.expanduser(os.path.expandvars(path))
        if not path:
            return
        args = os.path.expanduser(os.path.expandvars(str(args)))
        if args.strip():
            subprocess.Popen([path] + shlex.split(args))
        elif os.path.isfile(path) and os.access(path, os.X_OK):
            subprocess.Popen([path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"chord launch failed for {path!r}: {e}")


# --- Microphone: "Push to Talk (Mic)" + "Mute / Unmute Mic" -----------------
# Both bindable actions mute the SYSTEM DEFAULT capture source  the same
# switch the desktop's sound panel flips  so the effect is system-wide: every
# game, Discord, OBS, browser call and voice app goes silent at once, with no
# per-app hotkey to configure.
#
# `pactl ... @DEFAULT_SOURCE@` covers PulseAudio AND PipeWire (via
# pipewire-pulse, which is what a modern CachyOS/SteamOS box runs); `wpctl` on
# @DEFAULT_AUDIO_SOURCE@ is the fallback for a bare-WirePlumber setup with no
# pulse shim. Windows resolves the same two actions through
# IAudioEndpointVolume  see windows/tray.py.
#
# Ops run on a worker thread, queued and never coalesced (press/release
# ordering is what makes Push to Talk correct), so the HID read thread never
# blocks on a subprocess.
_mic_lock = threading.Lock()
_mic_ptt_holders = set()        # holder ids currently holding Push to Talk
_mic_queue = []                 # pending ops: True=mute, False=unmute, "toggle"
_mic_evt = threading.Event()
_mic_thread = None


def _mic_do(op):
    """Apply one mic op  True = mute, False = unmute, "toggle" = flip  to the
    default capture source. Returns True when a backend accepted it."""
    arg = "toggle" if op == "toggle" else ("1" if op else "0")
    for cmd in (["pactl", "set-source-mute", "@DEFAULT_SOURCE@", arg],
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", arg]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            if subprocess.run(cmd, capture_output=True, timeout=3).returncode == 0:
                return True
        except Exception as e:
            print(f"mic: {cmd[0]} {arg} failed: {e!r}")
    return False


def _mic_worker():
    while True:
        _mic_evt.wait()
        _mic_evt.clear()
        while True:
            with _mic_lock:
                op = _mic_queue.pop(0) if _mic_queue else None
            if op is None:
                break
            _mic_do(op)


def _mic_request(op):
    """Queue a mic op (called from the input loop  returns immediately). MUST
    be called with _mic_lock held so ordering matches the caller's state
    transition."""
    global _mic_thread
    _mic_queue.append(op)
    if _mic_thread is None or not _mic_thread.is_alive():
        _mic_thread = threading.Thread(target=_mic_worker, daemon=True,
                                       name="mic-worker")
        _mic_thread.start()
    _mic_evt.set()


def mic_ptt_hold(holder, want):
    """Hold/release Push to Talk on behalf of a named `holder`. Reference-counted
    like the keyboard/mouse holds (open the mic for the FIRST holder, close it
    only when the LAST one lets go), so several controls  or the same control
    reached through the desktop, gamepad and advanced-press paths  can't fight.

    Releasing MUTES rather than restoring the previous state: that is what makes
    it push-TO-talk. The mic sits closed and only opens while the button is down."""
    with _mic_lock:
        was = bool(_mic_ptt_holders)
        if want:
            _mic_ptt_holders.add(holder)
        else:
            _mic_ptt_holders.discard(holder)
        now = bool(_mic_ptt_holders)
        if now and not was:
            _mic_request(False)     # first holder: open the mic
        elif was and not now:
            _mic_request(True)      # last holder let go: close it


def mic_toggle_mute():
    """Flip the default microphone's mute state (edge action)."""
    with _mic_lock:
        _mic_request("toggle")


def _mic_ptt_release_all():
    """Drop every Push-to-Talk holder and close the mic. Called at exit so a
    process that dies mid-hold can never leave the microphone open."""
    with _mic_lock:
        if not _mic_ptt_holders:
            return
        _mic_ptt_holders.clear()
    _mic_do(True)                   # synchronous: the worker may be gone


atexit.register(_mic_ptt_release_all)


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
    Big Picture" action. On its own thread: the steam:// handoff goes through
    the Steam client, which the input loop should not wait on."""
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


# --- Shared chord state -----------------------------------------------------

class _ChordState:
    """Held-modifier state for the desktop-mode chord watcher. Outlives
    individual SteamController instances so a mid-hold device rebuild
    doesn't strand Alt/Shift/Super pressed at the OS level.

    Mirrors tray.py's _ChordState on Windows."""

    def __init__(self):
        import steamcontroller.uinput as sui
        self.kb = sui.Keyboard()
        self.mouse = sui.Mouse()
        self.alt_held = False
        self.view_was_pressed = False
        self.shift_held = False
        self.win_held = False
        # Ref-counted desktop-takeover holds (mirrors windows/tray.py): multiple
        # controls can map to the same OS button/modifier (e.g. right-pad-click
        # AND a rebound trigger → left), so we track the SET of sources and only
        # press on the first / release on the last. Held here (not on _Watcher)
        # so a sc.run() rebuild mid-hold can't strand anything down.
        self.mouse_holders = {"left": set(), "right": set(), "middle": set()}
        self.key_holders = {}
        # Sources currently holding the "Gyro To Mouse" action, source -> the
        # controller kind it was asserted for. That action isn't a key, so it
        # can't ride key_holders (whose ref-count is per KEY, and whose release
        # would have no kind to hand back)  see set_key / gyro_action_hold.
        self.gyro_holders = {}

    def set_mouse_button(self, button, source, want):
        holders = self.mouse_holders[button]
        was_held = bool(holders)
        if want:
            holders.add(source)
        else:
            holders.discard(source)
        is_held = bool(holders)
        if is_held and not was_held:
            self.mouse.button(button, True)
        elif was_held and not is_held:
            self.mouse.button(button, False)

    def release_mouse_held(self):
        for button, holders in self.mouse_holders.items():
            if holders:
                holders.clear()
                self.mouse.button(button, False)

    def set_key(self, key, source, want, kind=None):
        """Hold/release a keyboard `key` for a named `source`, ref-counted.

        `key` may also be one of the two hold SENTINELS, in which case the hold
        drives that feature instead of pressing a key:
          MIC_PTT_KEY     open/close the system microphone (Push to Talk).
          GYRO_MOUSE_KEY  enable/suppress/flip `kind`'s gyro, per that
                           controller's own Options gyro mode.
        Routing them through here means every hold-capable dispatch site
        (per-control overrides, gamepad key overrides, advanced presses, button
        combos) gets them for free, including the bulk release paths that stop a
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
        if is_held == was_held:
            return
        if key == keybinds_runtime.MIC_PTT_KEY:
            mic_ptt_hold("sc", is_held)
        elif is_held:
            self.kb.pressEvent([key])
        else:
            self.kb.releaseEvent([key])

    def release_keys_held(self):
        for key, holders in list(self.key_holders.items()):
            if holders:
                holders.clear()
                if key == keybinds_runtime.MIC_PTT_KEY:
                    mic_ptt_hold("sc", False)
                else:
                    self.kb.releaseEvent([key])
        # Same for the gyro action's holds: a mode switch that drops the desktop
        # layer mid-hold must not leave "hold to enable" stuck on (or "hold to
        # suppress" stuck off).
        for source, kind in list(self.gyro_holders.items()):
            gyro_action_hold(kind, "sc:" + source, False)
        self.gyro_holders.clear()

    def release_alt(self):
        if self.alt_held:
            import steamcontroller.uinput as sui
            self.kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self.alt_held = False

    def release_shift(self):
        if self.shift_held:
            import steamcontroller.uinput as sui
            self.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self.shift_held = False

    def release_win(self):
        if self.win_held:
            import steamcontroller.uinput as sui
            self.kb.releaseEvent([sui.Keys.KEY_LEFTMETA])
            self.win_held = False

    def release_all_held(self):
        self.release_alt()
        self.release_shift()
        self.release_win()
        self.release_mouse_held()
        self.release_keys_held()


class _GyroMouse:
    """Shared gyro-to-mouse integrator: angular velocity (°/s) → relative
    cursor motion with fractional-pixel carry. One instance per input path.
    Fed per input frame while that controller's "Gyro To Mouse" is active;
    yaw steers X and pitch steers Y the way Steam Input's gyro mouse does
    (turn the controller left → cursor left, tilt it up → cursor up). All
    tuning is the kind's cog-modal config in adusk_state: gain =
    Dots-Per-360°/360 × sensitivity (px per degree), and gyro_shape applies
    the speed deadzone / precision / acceleration curves (a controller at
    rest never drifts). dt is clamped so the first frame after a
    toggle/reconnect can't fling."""

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



class _SdlDesktopController:
    """Turns a non-Steam SDL pad (Switch / Xbox / DualSense / Deck / handheld
    built-ins / ...) into a desktop mouse + keyboard with FULL Steam-Controller
    parity: every Desktop-tab control dispatches its bound action through the
    same vocabulary as the SC (keys, clicks, combos, system actions), the
    Chords tab drives the Guide(Home)-held layer (defaults reproduce the old
    hardcoded Home chords), the Hotkeys chords fire from any pad, and stick
    directions are rebindable. Driven from sdl_gamepad_thread while the OSK is
    closed (Linux has no ViGEm / gamepad mode, so there is no game-feed or
    Home-hold mouse-only variant).

    Defaults (empty binds): right stick = cursor, left stick + D-pad = arrows,
    ZR/ZL = left/right click, positional A = Enter / B = Esc / Y = Space,
    bumpers = browser tab switch, L3 = middle click; Home+L3 = Play/Pause,
    Home+left stick = volume/track, Home+"+" = Alt+Tab, Home+B = force-kill.
    The positional X (physical Y on a Switch) opens the OSK via `open_bits`
    (dispatched by the tray thread, which owns the open cooldown gating).

    Only the plain STEAM bit is the guide here  QAM is the spare
    Capture/Mute/extra button (bindable), mirroring the SC's desktop-mode
    Steam/QAM split."""

    MOUSE_DEADZONE = 6000
    MOUSE_SPEED = 1400.0       # px/sec at full stick deflection
    MOUSE_EXPONENT = 1.6
    # Stick direction zones: deadzone + tap-then-repeat cadence, matched to
    # the Steam Controller's Linux desktop arrow cadence so both controllers
    # scroll at one speed.
    ARROW_DEADZONE = 14000
    ARROW_HOLD_DELAY = 0.35
    ARROW_REPEAT = 0.05
    # Guide-held stick zones: deadzone + the volume-ramp cadence.
    MEDIA_DEADZONE = 14000
    MEDIA_HOLD_DELAY = 0.5
    MEDIA_VOL_REPEAT = 0.021
    # Max press→release time (s) for a Guide/Home TAP (mirrors the SC's).
    _GUIDE_TAP_S = 0.28

    def __init__(self, force_kill=None, binds=None, chords=None,
                 on_profile_cycle=None, trigger_haptic=None,
                 on_toggle_gui=None):
        # uinput Mouse/Keyboard share module-global devices, so these don't
        # create new devices  they drive the same cursor/keyboard as the SC.
        self._mouse = sui.Mouse()
        self._kb = sui.Keyboard()
        # Callable that force-shutdowns the focused window (guide chord).
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
        # App callback for the "Toggle Config GUI" bound action (default on the
        # Guide/Home button)  opens/closes the picker + restores game focus.
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
        # it with its own OSK-open gating (cooldown / mode).
        self.open_request = False
        self._arrow_hold_delay = self.ARROW_HOLD_DELAY
        self._arrow_repeat = self.ARROW_REPEAT
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
        self._guide_chords = keybinds_runtime.build_guide_chords(
            ch, SCButtons, K, _SDL_IDS)
        # Built for parity; unused on Linux (no gamepad mode to toggle / no
        # virtual pad for Button-Combo Xbox outputs  their KEY actions still
        # hold in desktop mode below).
        self.gamepad_toggle_masks = keybinds_runtime.build_gamepad_toggle_masks(
            ch, SCButtons, _SDL_IDS)
        # "Gyro To Mouse" hotkey masks (per-controller Options card)  read by
        # the SDL thread every frame; fully live on Linux (desktop gyro mouse).
        self.gyro_toggle_masks = keybinds_runtime.build_gyro_toggle_masks(
            ch, SCButtons, _SDL_IDS)
        self._button_combos = keybinds_runtime.build_button_combos(
            ch, SCButtons, K, _SDL_IDS)
        self._combo_was_active = [False] * len(self._button_combos)
        # Fresh tables → drop stale edges so nothing fires off old state.
        self._ov_prev.clear()
        self._guide_edge.clear()

    def reset(self):
        """Release every held click/key and clear edge/accumulator state, so a
        handoff (OSK open, Steam-pause, pad unplug) never strands a button
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
            self._mouse.button(name, True)
            self._down_clicks[cid] = name
        elif not pressed and cid in self._down_clicks:
            self._mouse.button(self._down_clicks.pop(cid), False)

    def _set_key(self, cid, key, pressed, kind=None):
        """True held key (modifier-friendly) for a 'hold' bind. `key` may be one
        of the hold SENTINELS instead: MIC_PTT_KEY holds the system microphone
        open, GYRO_MOUSE_KEY drives `kind`'s gyro per that controller's own
        Options gyro mode  see _ChordState.set_key for why both ride the hold
        contract. `kind` defaults to whichever kind's binds are loaded; the
        gamepad-mode per-pad path passes the FIRING pad's kind, which may not be
        that one (see _feed_one_sdl_pad)."""
        gyro = keybinds_runtime.GYRO_MOUSE_KEY
        if pressed and cid not in self._down_keys:
            if key == keybinds_runtime.MIC_PTT_KEY:
                mic_ptt_hold("sdl:" + cid, True)
            elif key == gyro:
                # Remember the kind on the holder: the release below arrives
                # without one, and the ref-count is per kind.
                self._gyro_key_kinds[cid] = kind or self._active_kind
                gyro_action_hold(self._gyro_key_kinds[cid], "sdl:" + cid, True)
            else:
                self._kb.pressEvent([key])
            self._down_keys[cid] = key
        elif not pressed and cid in self._down_keys:
            k = self._down_keys.pop(cid)
            if k == keybinds_runtime.MIC_PTT_KEY:
                mic_ptt_hold("sdl:" + cid, False)
            elif k == gyro:
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
        `kind` overrides self._active_kind for a Profile Cycle dispatch.
        `mode` names which tab ("pc"/"gamepad"/"guide") this dispatch's binding
        lives in  read by the "profile_cycle" action so ONE dropdown entry
        cycles whichever mode was actually active when it fired."""
        typ = action[0]
        if typ == "tap":
            self._tap(action[1])
        elif typ == "combo":
            for k in action[1]:
                self._kb.pressEvent([k])
            for k in reversed(action[1]):
                self._kb.releaseEvent([k])
        elif typ == "click":
            self._mouse.button(action[1], True)
            self._mouse.button(action[1], False)
        elif typ == "hold":
            if action[1] == keybinds_runtime.MIC_PTT_KEY:
                # Edge-only site (stick zone / Home tap): latch the mic open,
                # fire again to close  see the SC watcher's _fire_guide_action.
                mic_ptt_hold("latch", "latch" not in _mic_ptt_holders)
            elif action[1] == keybinds_runtime.GYRO_MOUSE_KEY:
                # Likewise for "Gyro To Mouse": no release to end a hold on, so
                # the press flips it (see gyro_action_flip).
                gyro_action_flip(kind or self._active_kind)
            else:
                self._tap(action[1])
        elif typ == "scroll":
            self._mouse.scroll(0, action[1])
        elif typ == "mic_mute_toggle":
            mic_toggle_mute()
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
                    result = self._force_kill()
                    print(f"[forcekill] guide chord -> {result}")
                except Exception as e:
                    print(f"[forcekill] failed: {e!r}")
        elif typ == "xbutton":
            # Page Previous/Next: mouse Back/Forward side buttons (uinput
            # BTN_SIDE/BTN_EXTRA  honored by browsers and file managers).
            btn = "back" if action[1] == 1 else "forward"
            try:
                self._mouse.button(btn, True)
                self._mouse.button(btn, False)
            except Exception:
                pass
        elif typ == "toggle_magnifier":
            import subprocess
            try:
                r = subprocess.run(["pgrep", "-x", "xmag"],
                                   capture_output=True, timeout=2)
                if r.returncode == 0:
                    subprocess.Popen(["pkill", "-x", "xmag"])
                else:
                    subprocess.Popen(["xmag"])
            except Exception as e:
                print(f"toggle_magnifier failed: {e!r}")
        elif typ in ("brightness_up", "brightness_down"):
            import subprocess
            arg = "+10%" if typ == "brightness_up" else "10%-"
            try:
                subprocess.Popen(["brightnessctl", "set", arg])
            except Exception as e:
                print(f"brightness failed: {e!r}")
        elif typ == "lock_pc":
            import subprocess
            try:
                subprocess.Popen(["loginctl", "lock-session"])
            except Exception as e:
                print(f"lock_pc failed: {e!r}")
        elif typ == "screen_off":
            self._screen_off = not self._screen_off
            import subprocess
            arg = "off" if self._screen_off else "on"
            try:
                subprocess.Popen(["xset", "dpms", "force", arg])
            except Exception as e:
                print(f"screen_off failed: {e!r}")
        elif typ == "sleep_pc":
            import subprocess
            try:
                subprocess.Popen(["systemctl", "suspend"])
            except Exception as e:
                print(f"sleep_pc failed: {e!r}")
        elif typ == "shutdown_pc":
            import subprocess
            try:
                subprocess.Popen(["systemctl", "poweroff"])
            except Exception as e:
                print(f"shutdown_pc failed: {e!r}")
        elif typ == "profile_cycle":
            # Advance THIS controller's active profile slot for whichever tab
            # this binding's dispatch site is in (`mode`, passed by the
            # caller)  kind defaults to whichever kind's binds are currently
            # loaded (self._active_kind).
            if self._on_profile_cycle is not None:
                try:
                    self._on_profile_cycle(kind or self._active_kind, mode)
                except Exception as e:
                    print(f"profile cycle failed: {e!r}")
        elif typ == "toggle_gui":
            # Open/close the config GUI (default Guide/Home-button tap)  the App
            # owns the picker + game-focus restore.
            if self._on_toggle_gui is not None:
                try:
                    self._on_toggle_gui()
                except Exception as e:
                    print(f"toggle_gui failed: {e!r}")
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
        except Exception as e:
            print(f"chord fire failed: {e!r}")

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
        # trigger is held (Xbox outputs are meaningless without a virtual pad).
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
        "pc")."""
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

    def guide_release(self):
        """Called when a guide hold ends externally: drop Alt + edges."""
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

    def _track_home_tap(self, b, now, guide_held):
        """Guide/Home TAP → bound action (mirror of the SC's Steam/QAM tap):
        fires only on a clean short press with NO other button during the hold,
        so the guide chords are untouched."""
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
                    and self._home_tap and self._home_tap[0] != "none"):
                self._fire_action(self._home_tap)
        self._guide_prev = guide_held


# --- App --------------------------------------------------------------------

# Steam Controller L2/R2 actuation levels → analog trigger threshold (0..32767;
# None = firmware full-pull digital bit only). "high" is the old full-pull-only
# "Default"; "default" is now a lighter ~35%-pull point and is the shipped
# program default (used for BOTH the OSK Shift/Enter functions AND the desktop
# takeover's L2/R2 mouse-click actuation). "low" is the lightest pull.
_SC_ACTUATION_THRESHOLDS = {"high": None, "default": 16728, "low": 3000}


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
# SC desktop-takeover trackpad multipliers (tray "Steam Controller" submenu),
# scaling the right-pad cursor + left-pad scroll sensitivity in the _Watcher.
# Calibrated 2026-06-21: tuned so a full right-pad swipe ≈5.5 of cursor travel (the
# ×1.30 "30% faster" run measured 7.2, scaled back ×5.5/7.2 to 5.5). Original
# baseline 0.6/1.0/1.7 = 10.8 swipe. The FLING is now DECOUPLED from this via
# RPAD_FLING_GAIN (fling speed = tracking lift × that), so the throw runs faster than
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
        # Push the current autostart preference to disk so it matches the
        # saved setting (handles "user moved the binary" cases too).
        _apply_autostart(self.settings["start_at_login"])
        # Publish the per-controller haptics switches to the runtime flags the
        # OSK haptic-click paths read (gated by the active controller's toggle).
        adusk_state.set_rumble_enabled("sc", self.settings["rumble_enabled_sc"])
        adusk_state.set_rumble_enabled("sdl", self.settings["rumble_enabled_switch"])
        # Normalize + publish the selected OSK skin so screen.Screen picks it up
        # the next time the keyboard opens. Fall back to the default if the
        # saved name no longer matches a bundled skin.
        if self.settings.get("skin") not in adusk_skins.available_skins():
            self.settings["skin"] = adusk_skins.DEFAULT_SKIN
        adusk_skins.set_active_skin(self.settings["skin"])
        # Publish the OSK transparency (continuous 0..1 fraction) so screen.Screen
        # renders it.
        adusk_skins.set_transparency_fraction(self._osk_transparency_fraction())
        # Publish the OSK window size so screen.Screen() builds the window at
        # the right dimensions the next time the keyboard opens.
        adusk_screen.set_osk_size(self.settings.get("osk_size", "medium"))
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
        adusk_state.set_video_scrub_mode(self.settings.get("video_scrub", "off"))
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
        # SCButtons bits that close the OSK for SDL pads (bound to Escape; B
        # by default). Unioned with the SC's set in the watcher publish.
        self._sdl_close_bits = keybinds_runtime.resolve_sdl_close_buttons(
            {}, SCButtons)
        # Seed the OSK's Shift/Enter glyph set from the last-used controller so
        # the right hints (SC L2/R2 vs Switch ZL/ZR vs Xbox LT/RT ...) show on
        # the very first open after launch, before any input. Then register a
        # hook so a live controller switch is saved back to disk and survives
        # a reboot.
        saved_ctrl = pads.canon(self.settings.get("last_osk_controller", "sc")) or "sc"
        adusk_state.init_active_controller(saved_ctrl)
        adusk_state.set_active_controller_persist(self._persist_active_controller)

        self._stop_event = threading.Event()
        # Latch for the Hotkeys "Gamepad Mode Toggle" chord. Lives on the App (not
        # the SC watcher) so it survives the watcher rebuild the toggle kicks: set
        # when the chord fires, cleared when it's released, so holding the chord
        # can't re-fire and ping-pong. (gamepad control modes aren't a Linux
        # runtime concept  ViGEm isn't ported  so the toggle is inert here.)
        self._gp_toggle_latched = False
        # Built-in "hold ≡ (Start/Menu) to switch Desktop <-> Gamepad" gesture.
        # One detector per input path (the SC watcher and the SDL pad loop run
        # concurrently and see different frames), both living HERE rather than
        # on the watcher so the hold state survives the watcher rebuild the
        # mode switch kicks off  a fresh detector would restart its timer while
        # the button is still down and ping-pong the mode. Flips exactly what
        # the "Gamepad Mode Toggle" chord flips, so it's equally inert on Linux
        # until a gamepad-mode runtime lands.
        self._mode_hold_hid = keybinds_runtime.ModeHoldGesture()
        self._mode_hold_sdl = keybinds_runtime.ModeHoldGesture()
        # "Gyro To Mouse" runtime state lives in adusk_state (the shared
        # gyro_mouse_kinds set) so the tray paths AND the OSK (which owns the
        # controllers while open, and evaluates the same hotkey there) stay in
        # sync. Session-only. Fully live on Linux for SDL pads (SDL sensor
        # API); the SC's IMU enable rides the Triton feature-report caveat.
        # Press/release edges from BOTH sources  the modal's hotkey chords and
        # buttons bound to the "Gyro To Mouse" action  are ref-counted per
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
        # Runtime control-scheme override (parity with Windows). Inert on Linux
        # since there's no virtual-pad runtime; kept so the chord never flips or
        # persists the ViGEm Bus Driver setting.
        self._mode_override = None
        # Set when Steam is running AND the user opted into pausing.
        self._steam_active = threading.Event()
        # Set by the menu's "Open" item to ask the main thread to bring up
        # the OSK. The OSK MUST run on the main thread (SDL constraint), so
        # the menu callback only signals  the run loop in main() does the
        # actual work.
        self._open_kbd_event = threading.Event()
        # Set while the OSK is on screen so we don't try to reopen it on top
        # of itself (the second SDL_Init would fail).
        self._kbd_open = False
        # Desired-state flag for the Options-tab live OSK preview: True while the
        # user is pressing a Size/Transparency slider in the picker (the keyboard
        # is shown so they can see the effect; closed on release). Drives an
        # animation-free open; only ever closes the OSK the preview itself opened.
        self._osk_preview = False
        # Same but for the Menu/≡ "Enter Value" typing open (see
        # _open_osk_typing)  opens WITH real input processing (preview=False)
        # instead of the input-ignoring preview.
        self._osk_typing = False
        # Controller family ("sc"/"sdl"/None) that requested the pending OSK
        # open, so the OSK starts on that controller's glyphs. None = a
        # non-controller open (tray menu / Win+Ctrl+O)  keep the last-used one.
        self._pending_open_controller = None
        # Reference to the pystray Icon, set in main() after construction.
        # Used by background threads to update tooltips / hide menu items.
        self._icon = None
        # The live SteamController instance (set by chord_watcher_thread while
        # its sc.run() is active), so battery_thread can poll get_battery().
        self._current_sc = None
        # Battery status (see battery_thread). _battery is the last
        # SteamControllerBattery polled from the live controller, or None until
        # one streams a power report. _battery_label is the cached menu text.
        # _low_warned_at is the lowest low-battery band already toasted this
        # discharge cycle; it re-arms ONLY when a charger is next connected 
        # never on a % reading drifting back up  so a pack idling near a band
        # can't spam. _charge_complete_notified latches the "charged" toast.
        # _was_charging tracks the (debounced) charge state across polls so we
        # can toast the charger-connected / charger-disconnected edges.
        # _batt_charge_seen / _batt_charging /
        # _batt_charge_complete debounce the charge-state byte: it must agree on
        # two consecutive polls before we act on it, so a flickering charger
        # can't spam toasts or churn the menu.
        self._battery = None
        self._battery_label = None
        # Latched True once a Steam Controller is ever detected this session, so
        # the "Steam Controller" tray menu stays visible the whole session.
        self._sc_ever_connected = False
        # Same latch for a Nintendo Switch Pro / SDL pad  set in
        # sdl_gamepad_thread; gates the "Switch Pro Controller" submenu.
        self._switch_ever_connected = False
        # One-shot per session: whether the "your Nintendo pad dropped, here's
        # why" notice has been shown (see nintendo_bt.py).
        self._nintendo_drop_notified = False
        self._low_warned_at = None
        self._charge_complete_notified = False
        self._was_charging = False
        self._batt_charge_seen = None
        self._batt_charge_pending_frame = None
        self._batt_charging = False
        self._batt_charge_complete = False
        # Options → Big Picture controller-connect automation (big_picture.py,
        # an Auto-Big-Picture port): opens/closes Steam Big Picture on
        # joystick connect/disconnect edges. Started with the other threads.
        self._bp_engine = big_picture.BigPictureEngine(
            settings=self.settings, notify=self._notify,
            # Paused for Steam = controllers ceded, so joystick presence says
            # nothing; ignore edges across the pause or closing Steam looks
            # like a fresh connect and re-launches Big Picture (which restarts
            # Steam, which pauses us again).
            controller_paused=self._steam_active.is_set)

        # SDL3 gamepad backend (Xbox/DualSense/Switch Pro/...). The tray owns a
        # persistent SDL_INIT_GAMEPAD (on the main thread, here) so the OSK can
        # borrow it via SDL_InitSubSystem without it being torn down on OSK
        # close. sdl_gamepad_thread drives a desktop mouse/keyboard from the pad
        # and opens the OSK on bare Y; while the OSK is open adusk polls this same
        # source. Stays None if SDL init fails  the Steam Controller path is
        # wholly unaffected.
        # Allow gamepad events when no SDL window is focused  the OSK window is
        # no-focus, and without this SDL freezes the pad to all-zero while it's
        # open (set before SDL_Init in case the hint is read at subsystem init;
        # adusk re-sets it too). This is what makes the pad drive the OSK.
        S.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        # Keep SDL's HIDAPI driver off the Steam Controller. We drive the SC
        # entirely through our own steamcontroller HID backend (never as an SDL
        # gamepad); SDL3 recognizes the Triton PIDs 0x1304/0x1302 and grabs the
        # device on GAMEPAD init, which blocks our exclusive open ("Block
        # SteamInput Steam Controller grab") and can otherwise duplicate input.
        S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"0")
        # Nintendo family: Joy-Cons, the NSO retro pads, GameCube adapters
        # (and Switch 2 once SDL can reach them). Must precede SDL_Init.
        self._apply_nintendo_sdl_hints()
        self._sdl_source = None
        # True while sdl_gamepad_thread holds a process responsiveness request
        # (no-op on Linux; mirrors the Windows tree). See _set_sdl_hi_res.
        self._sdl_hi_res = False
        # The live _SdlDesktopController (set when sdl_gamepad_thread starts), so
        # the Keybinds picker's Save can re-apply Switch binds without a reconnect.
        self._sdl_desktop = None
        try:
            if S.SDL_Init(S.SDL_INIT_GAMEPAD):
                self._sdl_source = adusk_inputsrc.Sdl3GamepadSource()
                # Nintendo Bluetooth guard on/off before the first pad opens,
                # so a pad connected at startup is classified correctly.
                self._sdl_source.set_bt_safe(
                    self.settings.get("nintendo_bt_safe", True))
                self._sdl_source.set_joycon_stick_rotate(
                    self.settings.get("joycon_stick_rotate", False))
            else:
                print(f"SDL gamepad init failed: {S.get_error()}")
        except Exception as e:
            print(f"SDL gamepad backend unavailable: {e!r}")
        # Hand the source to adusk so its main loop can poll it on the SDL
        # event-pump thread while the OSK is open (this tray's sdl_gamepad_thread
        # cedes then  SDL only refreshes gamepad state on the event-pump thread,
        # so polling it here once the OSK's loop runs reads stale/blind frames).
        adusk_state.set_sdl_source(self._sdl_source)

    # tray menu state predicates --------------------------------------------

    def is_start_at_login_checked(self, item):
        return self.settings["start_at_login"]

    def is_disable_while_steam_checked(self, item):
        return self.settings["disable_while_steam_running"]

    def is_exit_on_steam_checked(self, item):
        return self.settings["exit_on_steam_launch"]

    def is_sc_rumble_checked(self, item):
        return self.settings["rumble_enabled_sc"]

    def is_switch_rumble_checked(self, item):
        return self.settings["rumble_enabled_switch"]

    def is_debug_unlocked(self, item):
        """Visibility callback for the hidden Debug submenu."""
        return self.settings["debug_menu_unlocked"]

    def is_block_sc_hid_checked(self, item):
        return self.settings["block_sc_hid"]

    def _kbd_menu_label(self, item):
        """Dynamic label for the top menu item: shows the action a click will
        perform given the OSK's current open/closed state. The menu is
        refreshed via icon.update_menu() whenever _kbd_open flips."""
        return "Close keyboard" if self._kbd_open else "Open keyboard"

    # tray menu actions -----------------------------------------------------

    def open_kbd(self, icon, item):
        """Menu handler: bring up the OSK, or close it if it's already open."""
        if self._kbd_open:
            try:
                adusk_state.close()
            except Exception:
                pass
            return
        self._open_kbd_event.set()

    def open_keybinds(self, icon=None, item=None):
        """Tray menu ("Keybinds"): open the Steam-style binding picker. Menu
        actions must take at most (icon, item)  pystray's _assert_action
        REJECTS callables with a third parameter, even defaulted  so the
        warm-build flag lives on _open_keybinds below, not here."""
        self._open_keybinds()

    def _open_keybinds(self, warm=False):
        """Open (or warm pre-build) the Steam-style binding picker. Imported
        lazily and defensively so the tray never depends on tkinter at startup.
        Tk (system Tcl/Tk + the _tkinter C extension) is the ONLY optional OS
        package the picker needs; everything else is bundled. Distinguish "Tk
        genuinely missing" from a real import bug so the message is actionable
        instead of a misleading guess (it used to always blame Tk). The picker
        runs its own Tk loop on its own thread and calls _save_keybinds on Save.
        `warm=True` (startup pre-build) constructs the window hidden and does
        NOT show it  the first real click then opens instantly; failures stay
        silent (the real click will surface them)."""
        try:
            import keybinds_picker
        except Exception as e:
            name = getattr(e, "name", "") or ""
            if isinstance(e, ImportError) and name in ("_tkinter", "tkinter"):
                cmd = _tk_install_hint()
                print(f"keybinds picker: Tk is not installed ({e!r}). Install: {cmd}")
                if not warm:
                    self._notify("Keybinds",
                                 f"The binding picker needs Tk. Install: {cmd}")
            else:
                print(f"keybinds picker failed to import: {e!r}")
                if not warm:
                    self._notify("Keybinds",
                                 f"Binding picker failed to open: {e}")
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
        """The "Toggle Config GUI" bound action (default on the Guide/Home
        button, both Desktop and Gamepad tabs): reveal the Keybinds picker if
        it's hidden, hide it  handing the X11 foreground back to the game  if
        it's shown. Lets players pop the picker up mid-game to tweak bindings and
        land back in a borderless-windowed title with focus.

        NOTE: a borderless-windowed / windowed game only. A game in EXCLUSIVE
        fullscreen owns the display and a normal window can't draw over it
        without overlay injection  there, toggling the GUI minimises the game.
        Runs on a controller input thread; the picker's own Tk loop services the
        request. Accepts *args so it works as either a bare action or a
        (kind, mode) callback."""
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
            "advanced": self.settings.get("debug_menu_unlocked", False),
            "block_sc_hid": self.settings.get("block_sc_hid", False),
            "block_gamepad_takeover": self.settings.get("block_gamepad_takeover", False),
            "gamepad_mode": _gm,
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
            "tap_to_click": self.settings.get("tap_to_click", False),
            "tap_to_click_left": self.settings.get("tap_to_click_left", False),
            "typing_mode": self.settings.get("typing_mode", "default"),
            "video_scrub": self.settings.get("video_scrub", "off"),
            # Switch Pro Controller page (mirrors the tray "Switch Pro Controller" submenu).
            "rumble_enabled_switch": self.settings.get("rumble_enabled_switch", True),
            "switch_pointer_speed": self.settings.get("switch_pointer_speed", "medium"),
            # Nintendo Bluetooth dropout mitigation (Switch Pro page).
            "nintendo_bt_safe": self.settings.get("nintendo_bt_safe", True),
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
                for base in ("osk_trigger_actuation", "mouse_trigger_actuation",
                             "gamepad_trigger_actuation", "pointer_speed",
                             "rumble_enabled", "rumble_gamepad"):
                    key = pads.setting_key(kind, base)
                    if key in out or key in ("rumble_enabled_switch",
                                             "rumble_gamepad_switch",
                                             "switch_pointer_speed"):
                        continue  # legacy keys are already in _general_settings
                    if base.startswith("rumble"):
                        out[key] = self.settings.get(key, True)
                    elif base == "pointer_speed":
                        out[key] = self.settings.get(key, "medium")
                    else:
                        out[key] = self.settings.get(key, "default")
            kmap = osk_by_kind.get(kind) or {}
            if kind != "sc":
                for func, cid in kmap.items():
                    out["oskbtn_%s_%s" % (kind, func)] = cid
        return out

    def _general_readback(self, keys):
        """Re-read the TRUE current value of each requested Options setting from
        its authoritative source, so the picker can snap a control that did NOT
        take back to reality instead of showing what was asked for (see the
        picker's _readback_general). Every Options page this runtime builds is
        a plain settings.json write that cannot be refused, so there is nothing
        to correct and the answer is always empty  the Windows tree answers
        for its Steam / Sleep Manager / display / audio pages, which have no
        Linux counterpart yet. Kept so the picker's read-back call has a real
        handler here too, and so porting one of those pages only means adding
        its group."""
        return {}

    def _apply_general_setting(self, setting, value):
        """Apply a General-page change from the picker (runs on the picker's Tk
        thread). Mirrors the tray Startup-submenu handlers: persist + side effects.
        The Steam-watch poll loop re-reads settings each cycle, so no wake needed."""
        if setting in ("__readback__", "__poll__"):
            # Not applies  the picker asking what actually took, and its slow
            # background poll asking whether anything changed outside the app.
            # Both answer empty here (nothing on this runtime's Options pages
            # has an owner outside settings.json), so the poll costs a dict
            # lookup and never spawns anything.
            return self._general_readback(value)
        if setting == "gamepad_mode":
            # "auto" / "always" / "manual" / "off"  persist only (ViGEm/auto-
            # detect not yet ported to Linux; settings are written so they
            # round-trip). "manual" = picker master ON + Auto Enable OFF.
            if value == "always":
                self.settings["gamepad_mode"] = True
                self.settings["auto_gamepad_mode"] = False
                self.settings["gamepad_manual"] = False
            elif value == "auto":
                self.settings["auto_gamepad_mode"] = True
                self.settings["gamepad_mode"] = False
                self.settings["gamepad_manual"] = False
            elif value == "manual":
                self.settings["gamepad_manual"] = True
                self.settings["gamepad_mode"] = False
                self.settings["auto_gamepad_mode"] = False
            else:  # "off"
                if (not self.settings.get("gamepad_mode")
                        and not self.settings.get("auto_gamepad_mode")
                        and not self.settings.get("gamepad_manual")):
                    return
                self.settings["gamepad_mode"] = False
                self.settings["auto_gamepad_mode"] = False
                self.settings["gamepad_manual"] = False
            _save_settings(self.settings)
            self._kick_sc()
        elif setting == "start_with_windows":
            self.settings["start_with_windows"] = bool(value)
            _save_settings(self.settings)
            _apply_autostart(self.settings["start_with_windows"])
        elif setting == "when_steam":
            if value == "exit":
                self.settings["exit_on_steam_launch"] = True
                self.settings["disable_while_steam_running"] = False
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
        elif setting == "advanced":
            self.settings["debug_menu_unlocked"] = bool(value)
            _save_settings(self.settings)
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
            icon = self._icon
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
            self.settings[setting] = value
            _save_settings(self.settings)
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
            # Same path as the old tray "Size" submenu: each OSK open builds a
            # fresh Screen(), so this just needs to be saved + published. value
            # is one of "small"/"medium"/"full".
            self.settings["osk_size"] = value
            _save_settings(self.settings)
            adusk_screen.set_osk_size(value)
        elif setting == "osk_size_preview":
            # Transient preview while the picker's Size slider is held: publish
            # WITHOUT persisting. value=None reverts to the saved size; Save
            # persists via "osk_size".
            adusk_screen.set_osk_size(
                value or self.settings.get("osk_size", "medium"))
        elif setting == "rumble_enabled_sc":
            self.settings["rumble_enabled_sc"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_rumble_enabled("sc", bool(value))
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
        elif setting == "nintendo_bt_safe":
            # Options → Switch Pro "Bluetooth Safe Mode". Applies LIVE: the
            # source re-classifies every open pad, so the guard starts/stops
            # without unplugging anything.
            self.settings["nintendo_bt_safe"] = bool(value)
            _save_settings(self.settings)
            if self._sdl_source is not None:
                try:
                    self._sdl_source.set_bt_safe(bool(value))
                except Exception:
                    pass
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
            adusk_state.set_sc_mouse_speed(_sc_speed_mult(value))
        elif setting == "sc_osk_trigger_actuation":
            # Options-tab "Keyboard Trigger Actuation" slider (gradual). value is an
            # int analog threshold between the Default/Low endpoints; clears no
            # tray-menu radio when in-between. Same live path as select_sc_actuation.
            self.settings["sc_osk_trigger_actuation"] = value
            _save_settings(self.settings)
            adusk_state.set_sc_osk_trigger_threshold(
                _sc_actuation_threshold(value))
        elif setting == "sc_mouse_trigger_actuation":
            # Options-tab "Mouse Trigger Actuation" slider (gradual)  same
            # High/Default/Low scale as the Keyboard one, but a SEPARATE
            # setting/threshold. Same live path as select_sc_mouse_actuation.
            self.settings["sc_mouse_trigger_actuation"] = value
            _save_settings(self.settings)
            adusk_state.set_sc_mouse_trigger_threshold(
                _sc_actuation_threshold(value))
        elif setting == "sc_gamepad_trigger_actuation":
            # Options-tab "Gamepad Mode Trigger Actuation" slider (gradual)  same
            # scale, SEPARATE setting. Windows-only runtime effect; persisted here
            # for parity so the Options page round-trips identically.
            self.settings["sc_gamepad_trigger_actuation"] = value
            _save_settings(self.settings)
            adusk_state.set_sc_gamepad_trigger_threshold(
                _sc_actuation_threshold(value))
        elif setting == "sc_trackpad_speed":
            # Options-tab "Right Trackpad Sensitivity" slider (gradual, LINEAR;
            # the current speed sits at the slider midpoint). value is a float
            # multiplier; the tray-menu radios still store named levels. The
            # watcher reads the state getter per frame, so this applies live.
            self.settings["sc_trackpad_speed"] = value
            _save_settings(self.settings)
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
            # "wheel" (circular scroll dial) / "wheel_smooth" (dial, no ticks).
            # The watcher polls adusk_state per frame, so this applies live.
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
        elif setting == "tap_to_click":
            # Options-tab Touchpads → "Right Touchpad Tap to Click" toggle.
            # Applies live (the watcher polls adusk_state per frame).
            self.settings["tap_to_click"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_tap_to_click(bool(value))
        elif setting == "tap_to_click_left":
            # Options-tab Touchpads → "Left Touchpad Tap to Click" toggle.
            # Applies live (the watcher polls adusk_state per frame).
            self.settings["tap_to_click_left"] = bool(value)
            _save_settings(self.settings)
            adusk_state.set_tap_to_click_left(bool(value))
        elif setting == "typing_mode":
            # Options-tab Touchpads → "Trackpad Keyboard Typing Mode" dropdown. Applies
            # live: the OSK's pad handler reads adusk_state every frame (and at
            # every lift), so switching mode takes effect with the keyboard
            # already open  including the full-keyboard pad reach "swipe" adds.
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
        elif setting == "video_scrub":
            # Options-tab Touchpads → "Video Timeline Scrubbing" dropdown:
            # "off" / "frame" (precise) / "seek" (fast). Applies live (the
            # watcher polls adusk_state per frame).
            self.settings["video_scrub"] = value
            _save_settings(self.settings)
            adusk_state.set_video_scrub_mode(value)
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
            if base == "rumble_enabled":
                adusk_state.set_rumble_enabled(kind, bool(value))
            elif base == "pointer_speed":
                adusk_state.set_kind_mouse_speed(kind, _sc_speed_mult(value))
            elif base == "osk_trigger_actuation":
                adusk_state.set_sdl_trigger_threshold(
                    kind, "osk", _sdl_actuation_threshold(value))
            elif base == "mouse_trigger_actuation":
                adusk_state.set_sdl_trigger_threshold(
                    kind, "mouse", _sdl_actuation_threshold(value))
            elif base == "gamepad_trigger_actuation":
                adusk_state.set_sdl_trigger_threshold(
                    kind, "gamepad", _sdl_actuation_threshold(value))
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
            # rumble_gamepad: persisting is all that's needed (no gamepad-mode
            # runtime on Linux; the value round-trips for the Windows build).
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
            # osk_preview this open must be interactive (real input), so
            # _open_osk_once must see _osk_typing and open with preview=False.
            # Not persisted.
            if value:
                self._open_osk_typing()
            else:
                self._close_osk_typing()

    def _open_osk_preview(self):
        """Show the OSK for a live size/transparency preview (picker slider
        press). Runs on the picker's Tk thread  only signals; the main thread
        opens it (without an animation). No-op if the keyboard is already open,
        so releasing the slider can never close an OSK the user opened
        themselves."""
        if self._kbd_open or self._osk_preview:
            return
        self._osk_preview = True
        self._pending_open_controller = None
        self._open_kbd_event.set()

    def _close_osk_preview(self):
        """Hide the preview OSK (picker slider release). Only affects the OSK the
        preview itself opened."""
        if not self._osk_preview:
            return
        self._osk_preview = False
        if self._kbd_open:
            adusk_state.close()
        else:
            # Open hasn't been serviced yet  cancel the pending request so the
            # preview never actually opens.
            self._open_kbd_event.clear()

    def _open_osk_typing(self):
        """Show the OSK so the user can type a slider value with the controller
        (Menu/≡ "Enter Value"). Runs on the picker's Tk thread  only signals;
        the main thread opens it. _open_osk_once reads _osk_typing to open with
        preview=False so the OSK actually processes clicks/typing. No-op if the
        keyboard is already open (so closing the entry can never close an OSK
        the user opened themselves)."""
        if self._kbd_open or self._osk_typing:
            return
        self._osk_typing = True
        self._pending_open_controller = None
        self._open_kbd_event.set()

    def _close_osk_typing(self):
        """Hide the typing OSK (value entry committed/cancelled). Only affects
        the OSK the typing-open itself opened."""
        if not self._osk_typing:
            return
        self._osk_typing = False
        if self._kbd_open:
            adusk_state.close()
        else:
            # Open hasn't been serviced yet  cancel the pending request so the
            # typing-open never actually opens.
            self._open_kbd_event.clear()

    def _save_keybinds(self, new_binds, new_chords=None, profile=None):
        """Persist an edited keybind layout + chords from the picker (runs on the
        picker's Tk thread). Applies the Switch Pro PC-mode desktop binds live,
        and kicks the SteamController so its _Watcher rebuilds with the new SC
        chords / per-control overrides / stick rebinds. `profile` is the
        picker's per-(controller, tab) {kind: {mode: slot}} active map: each
        controller's each layout tab's binds land in its OWN slot AND the live
        "keybinds" mirror is recomposed from all the slots."""
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
        # Rebuild the SC watcher so edited SC binds/chords apply without restart.
        sc = self._current_sc
        if sc is not None:
            try:
                sc.addExit()
            except Exception as e:
                print(f"kick after keybind save failed: {e!r}")
        # Re-publish the "Gyro To Mouse" hotkey masks the OSK evaluates.
        if new_chords is not None:
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
        sc = self._current_sc
        if sc is not None:
            try:
                sc.addExit()
            except Exception as e:
                print(f"kick after profile switch failed: {e!r}")
        print(f"keybind profile {kind}/{mode}={slot} selected")

    def _add_keybind_profile(self, kind, mode, count):
        """Picker footer "+" button: persist the new profile-slot count
        (1-_MAX_KEYBIND_PROFILES)
        for ONE controller's ONE tab (mode = "pc"/"gamepad"/"guide")  every
        other controller (and this controller's other tabs) is untouched. The
        new slot is empty until the user edits + Saves it."""
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
        cleared) 
        only THIS controller's THIS mode's binds move; every other (kind, mode)
        sharing the same slot storage is untouched. Mirrors the picker's own
        in-memory renumber so both stay in sync. The active slot for this
        (kind, mode) follows the shift; the live "keybinds" mirror is recomposed
        and the SC watcher kicked."""
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
        sc = self._current_sc
        if sc is not None:
            try:
                sc.addExit()
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
        switch + kick."""
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

    def toggle_start_at_login(self, icon, item):
        new = not self.settings["start_at_login"]
        self.settings["start_at_login"] = new
        _save_settings(self.settings)
        _apply_autostart(new)

    def toggle_disable_while_steam(self, icon, item):
        new = not self.settings["disable_while_steam_running"]
        self.settings["disable_while_steam_running"] = new
        if new:
            # Mutually exclusive with exit-on-steam.
            self.settings["exit_on_steam_launch"] = False
        _save_settings(self.settings)

    def toggle_exit_on_steam(self, icon, item):
        new = not self.settings["exit_on_steam_launch"]
        self.settings["exit_on_steam_launch"] = new
        if new:
            self.settings["disable_while_steam_running"] = False
        _save_settings(self.settings)

    def toggle_sc_rumble(self, icon, item):
        # Steam Controller haptics. Read from settings rather than item.checked:
        # pystray's AppIndicator backend doesn't always populate item.checked.
        new = not self.settings["rumble_enabled_sc"]
        self.settings["rumble_enabled_sc"] = new
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sc", new)

    def toggle_switch_rumble(self, icon, item):
        # Nintendo Switch (SDL pad) haptics. Settings-read, as above.
        new = not self.settings["rumble_enabled_switch"]
        self.settings["rumble_enabled_switch"] = new
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sdl", new)

    def toggle_debug_menu(self, icon, item):
        new = not self.settings["debug_menu_unlocked"]
        self.settings["debug_menu_unlocked"] = new
        _save_settings(self.settings)

    def toggle_block_sc_hid(self, icon, item):
        new = not self.settings["block_sc_hid"]
        self.settings["block_sc_hid"] = new
        _save_settings(self.settings)
        self._kick_sc()

    def _kick_sc(self):
        """Force the current SteamController loop to exit so chord_thread
        rebuilds it with the new `block_sc_hid` setting (exclusive vs shared
        HID open)."""
        if self._current_sc is not None:
            try:
                self._current_sc.addExit()
            except Exception:
                pass

    def handle_gamepad_toggle(self, held):
        """Called every SC frame by the desktop watcher with whether a Hotkeys
        'Gamepad Mode Toggle' chord is fully held. Fires the toggle ONCE per
        press; the latch (cleared only on release) stops a held chord re-firing.
        Safe from the SC callback thread  _do_toggle uses the addExit() kick the
        watcher already drives."""
        if held and not self._gp_toggle_latched:
            self._gp_toggle_latched = True
            self._do_toggle_gamepad_mode()
        elif not held:
            self._gp_toggle_latched = False

    def handle_mode_hold(self, buttons, sdl=False):
        """Built-in "hold ≡ to switch Desktop <-> Gamepad" gesture, fed the raw
        button word every frame by whichever input path owns the controller (the
        SC watcher or the SDL pad loop  `sdl` picks that path's detector).

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
        """Toast the control scheme the hold-≡ gesture just selected, read off
        _mode_override (there's no _gamepad_active on Linux  no virtual-pad
        runtime  so the override IS the whole state)."""
        if self._mode_override:
            self._notify("Gamepad Mode",
                         "Your controller now uses its Gamepad bindings.")
        else:
            self._notify("Desktop Mode",
                         "Mouse, keyboard and your Desktop bindings are back.")

    def _do_toggle_gamepad_mode(self):
        """Switch the live control scheme between gamepad and desktop  used by
        the "Hotkey Gamepad/Desktop Toggle" chord. It switches CONTROLS only and
        must NOT change the ViGEm Bus Driver setting. ViGEm / a virtual-pad
        runtime isn't ported to Linux, so there's nothing to drive here: just
        record the runtime override (never flip or persist the driver setting)."""
        self._mode_override = not bool(self._mode_override)
        self._kick_sc()

    def handle_gyro_toggle(self, held, kind):
        """Called every SC frame with whether a "Gyro To Mouse" hotkey chord 
        one of the bars inside that controller's gyro modal  is fully held.

        Handed straight to gyro_action_hold as one named holder, so the
        modal's chord and any BUTTON bound to "Gyro To Mouse" on a layout tab
        are two holders of the SAME thing. That keeps the mode semantics
        (Enable / Suppress / Toggle / None) in one place, keeps "toggle" firing
        once per press rather than per frame, and  the reason it matters 
        stops this frame-driven path from writing `held=False` over a hold the
        bound button is asserting."""
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
                precision=g.get(pads.setting_key(k, "gyro_precision"), 0.75))
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

    # Skin submenu: one radio item per bundled skin. pystray needs a distinct
    # checked-predicate and action per name, so we build small closures. The
    # AppIndicator backend re-reads the checked predicate when the menu opens,
    # so a selection shows up without an explicit menu refresh.
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
        return _select

    _PER_APP_OSK_KEYS = {"position": "osk_pos_per_app",
                         "size": "osk_size_per_app",
                         "skin": "osk_skin_per_app"}

    def _persist_per_app_osk(self):
        """Write out the per-app OSK memory the last session recorded. No-op
        when the feature is off or nothing changed  the common case costs one
        empty-list check."""
        writes = adusk_state.drain_per_app_writes()
        if not writes:
            return
        for exe, kind, value in writes:
            key = self._PER_APP_OSK_KEYS.get(kind)
            if not key:
                continue
            table = dict(self.settings.get(key) or {})
            table[exe] = value
            self.settings[key] = table
        _save_settings(self.settings)

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
        return _select

    # OSK size (Keyboard Skin → Size submenu): "small" / "medium" (default) /
    # "full" (fills the display - good for a Steam Deck). Each OSK open
    # builds a fresh Screen(), so this just needs to be saved/published.
    def is_osk_size_checked(self, name):
        return lambda item: self.settings.get("osk_size", "medium") == name

    def select_osk_size(self, name):
        def _select(icon, item):
            self.settings["osk_size"] = name
            _save_settings(self.settings)
            adusk_screen.set_osk_size(name)
        return _select

    # --- Steam Controller submenu (shown only while an SC is connected) -------
    def is_sc_connected(self, item):
        # Latched: once an SC is ever detected the menu stays for the whole
        # session. The live signal flickers (_current_sc goes None while adusk
        # owns the SC with the OSK open), which made the menu vanish; battery_thread
        # also sets the latch so it's set even if the menu is never opened live.
        if self._current_sc is not None or self._battery is not None:
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

    def _persist_active_controller(self, kind):
        """Save the controller kind ("sc"/"switch"/"xbox"/"ps5"/...) last used
        on the OSK so its Shift/Enter glyphs persist across restarts. Called by
        adusk.state only when the active controller actually changes (on the
        input thread), so writes are rare. No menu refresh  invisible to the
        tray UI; it only affects which glyphs the keyboard draws."""
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
            try:
                import keybinds_picker
                keybinds_picker.rebuild_if_hidden()
            except Exception:
                pass
        if cache is not None:
            cache.add(kind)

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
        # If the OSK is currently on screen, ask it to shut down. The main
        # thread is blocked inside adusk_app.main() until this fires, so
        # without it the process can't observe stop_event and tear down.
        if self._kbd_open:
            try:
                adusk_state.close()
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
        try:
            icon.stop()
        except Exception:
            pass

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
    # than a normal sc rebuild (brief drop) so that doesn't blink the line.
    _BATTERY_STALE_SECONDS = 8.0

    def is_battery_known(self, item):
        """Visibility callback for the battery menu line  hidden until the
        controller has actually reported a level."""
        return self._battery is not None

    def battery_menu_label(self, item):
        return self._battery_label or "Steam Controller: …"

    def _apply_nintendo_sdl_hints(self):
        """Nintendo controller support, declared to SDL before SDL_Init.

        Everything Nintendo made for the Switch 1  the Pro Controller, both
        Joy-Cons, the NSO SNES/NES/N64/Genesis pads and the GameCube adapter 
        is driven by SDL's own HIDAPI drivers once it is paired (on Linux the
        kernel's hid-nintendo driver handles the pairing side), so most of this
        makes the defaults explicit rather than changing them: the drivers all
        inherit SDL_JOYSTICK_HIDAPI (on), and naming them means a future SDL
        that ships one OFF by default can't quietly drop a controller family.

        The two that DO carry a user choice are the Joy-Con ones. SDL treats a
        connected L+R as a single Pro-Controller-shaped pad by default; turning
        that off gives each half its own pad, so two people can play with one
        Joy-Con each. A lone Joy-Con is presented sideways unless told
        otherwise. Both are read by SDL at init, so a change needs a restart.

        SWITCH2 covers the Switch 2 pads, whose Bluetooth is a proprietary BLE
        protocol BlueZ does not speak  they arrive over USB-C or through a
        third-party bridge, and only once SDL carries a Switch 2 driver (the
        3.4.10 shipped here does not)."""
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

    def _notify(self, title, message):
        # Spawn notify-send directly instead of pystray's icon.notify().
        # pystray reuses a single notification id (`replaces_id`) for every call,
        # so the desktop notification daemon (Plasma) silently *updates* a single
        # dismissed notification instead of popping a new one  meaning only the
        # first toast in a session would actually show. notify-send with no
        # replaces-id creates a fresh notification each time.
        try:
            subprocess.Popen(
                ["notify-send", "--app-name=SteamlessInput",
                 "--icon=input-gaming", title, message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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
        icon = self._icon
        if icon is not None:
            try:
                icon.title = f"SteamlessInput  Steam Controller {state}"
            except Exception:
                pass
            try:
                icon.update_menu()
            except Exception:
                pass

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
        thread just samples it on a slow timer (battery changes slowly)."""
        last_key = None
        last_seen = None
        while not self._stop_event.is_set():
            if self._steam_active.is_set():
                # Paused for Steam: the SC is ceded, nothing to read  back off.
                self._stop_event.wait(self._BATTERY_POLL_SECONDS)
                continue
            # While the OSK is open the chord watcher releases the controller
            # so adusk can claim it, which makes _current_sc go None and would
            # otherwise stale-clear the cached reading. The controller is still
            # very much alive  just owned by another process  so we keep the
            # last reading on screen and skip the poll until the OSK closes.
            if self._kbd_open:
                self._stop_event.wait(self._BATTERY_POLL_SECONDS)
                continue
            sc = self._current_sc
            batt = sc.get_battery() if sc is not None else None
            # Latch SC-ever-connected so the "Steam Controller" menu stays for
            # the session once detected (even while adusk owns the SC, OSK open).
            if sc is not None or batt is not None:
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
                # unplug) wait the grace window so a brief rebuild doesn't blink
                # the line off and back on. Reset the latches so a reconnect is
                # treated as a fresh charge cycle.
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
                icon = self._icon
                if icon is not None:
                    try:
                        icon.title = "SteamlessInput"
                    except Exception:
                        pass
                    try:
                        icon.update_menu()
                    except Exception:
                        pass
            self._stop_event.wait(self._BATTERY_POLL_SECONDS)


    # background threads ----------------------------------------------------

    def steam_watch_thread(self):
        """Poll for Steam at 2 Hz. Fires _steam_active so the controller
        watcher can release the device while Steam is up, and triggers
        exit_app if the user picked "Exit when Steam is running"."""
        was_running = False
        while not self._stop_event.is_set():
            running = _steam_running()
            if running and not was_running:
                if self.settings["exit_on_steam_launch"]:
                    self._stop_event.set()
                    if self._icon is not None:
                        try:
                            self._icon.stop()
                        except Exception:
                            pass
                    return
                if self.settings["disable_while_steam_running"]:
                    self._steam_active.set()
                    if not self.settings.get("steam_pause_toast_shown", False):
                        # One-time-ever toast (persisted)  only the very first
                        # time this pause behavior actually fires, not every
                        # Steam launch.
                        self._notify("Steam detected",
                                     "SteamlessInput paused")
                        self.settings["steam_pause_toast_shown"] = True
                        _save_settings_if_exists(self.settings)
            elif not running and was_running:
                self._steam_active.clear()
            was_running = running
            if self._stop_event.wait(0.5):
                return

    def chord_watcher_thread(self):
        """Controller chord watcher (desktop / passive mode). Ports the
        full Windows _Watcher in tray.py minus the gamepad-mode branches
        (no ViGEm equivalent wired up yet). Chord set:

          * Steam+X        → open OSK
          * Steam+VIEW     → Alt+Tab (held Alt + Tab tap per VIEW edge)
          * Steam+L3       → Play/Pause
          * Steam+L-stick  → Volume (up/down, hold-repeats) / Prev-Next
          * Steam+Y        → power off controller
          * Steam+B        → SIGTERM the focused window's process
          * Y alone        → Space
          * R4 / R5        → Page Up / Page Down
          * L4 / L5 (hold) → hold Shift / Super
          * Left stick     → arrow keys (hold-repeats)
          * Right stick    → mouse cursor

        Sleeps while Steam is up or the OSK is open so we don't fight
        Steam / adusk for the HID handle."""
        try:
            from steamcontroller import SteamController, SCButtons, SCStatus
            import steamcontroller.uinput as sui
        except Exception as e:
            print(f"steamcontroller unavailable, chord watcher disabled: {e}")
            return

        STICK_DEADZONE = 14000
        STICK_HOLD_DELAY = 0.5
        STICK_VOL_REPEAT = 0.021
        ARROW_HOLD_DELAY = 0.35
        ARROW_REPEAT = 0.05
        DPAD_HOLD_DELAY = 0.35
        DPAD_REPEAT = 0.05
        MOUSE_DEADZONE = 6000
        MOUSE_SPEED = 1400.0
        # Bigger exponent = longer ramp (more stick travel maps to slow speeds),
        # so precise control needs less surgical thumb precision.
        MOUSE_EXPONENT = 5.0
        # Minimum speed (fraction of full) the instant the stick passes the
        # deadzone, so the first bit of travel moves a usable amount (>1px/frame)
        # for fine control instead of the near-zero the steep exponent gives.
        MOUSE_MIN = 0.05
        # Trackpad position units are int16 (~-32767..32767). Scale to
        # screen pixels; lower = slower cursor. Was 0.0066 (matched firmware
        # lizard's trackpad-mouse feel); halved with the 1€ filter pass 
        # the user wanted 50% less raw sensitivity (mirrors Windows
        # PAD_MOUSE_SCALE 0.03→0.015).
        RPAD_SCALE = 0.0033
        # LEFT-pad scroll de-jitter: position-domain low-pass with an ADAPTIVE
        # time constant blended by raw pad speed  heavy when slow (de-shake a
        # shaky thumb), near-raw on a fast swipe (snappy). The RIGHT-pad cursor
        # used this blend too until it moved to the 1€ filter below; scroll
        # keeps it (its feel was tuned separately).
        RPAD_SMOOTH_TAU_SLOW = 0.05     # slow/near-still: heavy de-shake smoothing
        RPAD_SMOOTH_TAU_FAST = 0.002    # full-swipe: almost raw (responsive)
        RPAD_SMOOTH_SPEED_LO = 12000.0  # ≤ this pad speed → full SLOW smoothing
        RPAD_SMOOTH_SPEED_HI = 25000.0  # ≥ this pad speed → full FAST smoothing
        # RIGHT-pad cursor smoothing = a 1€ filter (Casiez et al.) on the
        # ABSOLUTE pad position. The old two-knee tau blend above collapsed to
        # near-RAW past a gentle ~15 mm/s, so all real motion came out
        # unfiltered and harsh; Steam Input instead keeps a constant silky
        # low-pass with a touch of lag  this matches that feel. The cutoff
        # ADAPTS continuously with pad speed:
        #   fc(Hz) = MINCUTOFF + BETA * speed(pad-units/sec);  tau = 1/(2π·fc)
        # Near-still → fc ≈ MINCUTOFF (heavy de-shake, ~80 ms of glide-lag); a
        # fast flick opens the cutoff (lag shrinks to ~15 ms, still snappy).
        # The speed estimate feeding fc is itself low-passed at DCUTOFF so one
        # noisy frame can't pop the filter open (and a tremor's alternating
        # deltas average out instead of defeating the smoothing).
        RPAD_EURO_MINCUTOFF = 2.0   # Hz; floor cutoff at rest/slow  lower = smoother
        RPAD_EURO_BETA = 0.00008    # cutoff gain per pad-unit/sec  higher = snappier
                                    # (0.000033 read as "waiting for the cursor
                                    # to catch up" on fast moves; this cuts the
                                    # catch-up tail ~4×, slow-motion silk keeps)
        RPAD_EURO_DCUTOFF = 2.5     # Hz; low-pass on the speed estimate itself
        # Snap dead-zone radius (pad-units): cursor doesn't move until the filtered
        # point leaves the radius, then the anchor re-centers. A resting/tremoring
        # thumb stays inside → ZERO movement; real motion tracks. Raise if a shaky
        # thumb still nudges the cursor; lower if slow moves step.
        RPAD_DEADZONE = 70.0
        # Resting-anchor recenter: after a move stops, the trailing anchor sits
        # exactly ON the dead-zone edge  zero slack left along the direction
        # just traveled, so the very next tremor blip leaked straight out (the
        # residual still-finger wiggle). While the filtered pad speed sits
        # below REST_SPEED (a true rest  deliberate slow drags run faster)
        # the anchor eases back onto the filtered point, restoring the FULL
        # radius of slack all around the resting finger: tremor must now cross
        # the whole dead-zone, in any direction, to move the cursor at all.
        RPAD_REST_SPEED = 3200.0          # pad-units/sec; below = resting → recenter
        RPAD_ANCHOR_RECENTER_TAU = 0.10   # sec; how quickly the slack is restored
        # Lift fling = a punchy velocity IMPULSE (not a decaying coast, which read
        # as "on ice"): the glide velocity ramps UP from the lift speed to a boosted
        # peak over RPAD_FLING_RAMPUP_T, then ramps DOWN from the peak to a stop over
        # RPAD_FLING_RAMPDOWN_T  INDEPENDENT phase times (sec) so attack and release
        # tune separately (quick kick, gentler settle). Peak ∝ lift speed → travel ∝
        # swipe speed. Mirrors Windows PAD_FLING_*. NOTE: Linux RPAD_SCALE (0.0066) ≠
        # Windows base scale, so these px/sec values are NOT hardware-calibrated here
        #  tune on CachyOS.
        RPAD_FLING_GAIN = 1.5         # fling speed = tracking lift × this (throw decoupled)
        RPAD_FLING_BOOST = 1.4        # peak = fling speed × this (ramp-up kick)
        RPAD_FLING_RAMPUP_T = 0.05    # sec ramping UP from lift speed to the peak
        RPAD_FLING_RAMPDOWN_T = 0.34  # sec ramping DOWN from the peak to a stop
        # A fling only starts if the lift speed clears this (px/sec); slow drags
        # lift cleanly with no glide. Halved with RPAD_SCALE so the same
        # PHYSICAL swipe speed still triggers a fling.
        RPAD_FLING_TRIGGER = 250.0
        # Motion history kept for the lift velocity. At lift the final
        # RPAD_LIFT_SKIP seconds are DROPPED  the finger peeling off the pad
        # writes a garbage position blip in the last frames that used to read
        # as a violent swipe and "throw" the cursor from a standing-still
        # lift  and what remains must span RPAD_FLING_MIN_SPAN of real,
        # sustained motion to fling at all.
        RPAD_VELOCITY_WINDOW = 0.12
        RPAD_LIFT_SKIP = 0.03
        RPAD_FLING_MIN_SPAN = 0.05
        # Click-shake guard: squeezing a trigger / clicking a pad physically
        # wobbles the finger resting on the right pad, and that wobble used
        # to leak out as a tiny drag between the two clicks of a double-click
        # (folders dragged instead of opening). Any trigger/pad-click EDGE
        # freezes pad-mouse output briefly; a deliberate drag breaks out by
        # moving farther than the wobble ever does.
        RPAD_CLICK_FREEZE_S = 0.40    # cursor freeze after a click/trigger edge
        RPAD_FREEZE_BREAKOUT = 650.0  # pad-units of real motion ending it early
        # "Right Touchpad Tap to Click" (Options → Touchpads): a quick, STILL
        # touch-and-lift on the right pad = a left click, like a laptop
        # touchpad. A tap must lift within TAP_MAX_S, have lasted at least
        # TAP_MIN_S (a one-frame ghost contact is not a finger), have
        # wandered no more than TAP_MAX_DIST raw pad-units from the touch-
        # down point (measured EXCLUDING the final RPAD_LIFT_SKIP  the
        # finger-peel blip must not veto genuine taps), and have moved the
        # cursor no more than TAP_MAX_PX real pixels (a quick corrective
        # nudge that visibly moved the pointer is pointing, not tapping). A
        # candidate is cancelled outright by any pad/trigger click edge (the
        # real button wins), by touching down while a fling coasts (that
        # touch CATCHES the cursor), while a trigger already holds a mouse
        # button, and by a Steam-hold masking the pad mid-touch. A fired tap
        # runs _mouse_shake_guard so the second tap of a double-tap can't
        # smear the cursor between the two clicks (folders OPEN instead of
        # dragging). Mirrors Windows PAD_TAP_*.
        RPAD_TAP_MAX_S = 0.26     # touch must lift within this to be a tap
                                  # (0.22 missed ~1 in 5 real taps; user-tuned)
        RPAD_TAP_MIN_S = 0.02     # and last at least this (ghost-blip floor)
        RPAD_TAP_START_SKIP = 0.04  # sec dropped from the START of the touch 
                                    # the pad resolving a fresh contact writes
                                    # a garbage position blip in the first
                                    # frames (the touch-down twin of the lift
                                    # peel blip) that used to poison the wander
                                    # origin and read a still tap as a swipe
        RPAD_TAP_MAX_DIST = 1300.0  # raw pad-units of wander allowed (~0.8 mm),
                                    # judged as the bounding-box spread of the
                                    # CORE samples (start-skip → lift-skip),
                                    # not distance from the first-contact blip
        RPAD_TAP_MAX_PX = 12.0    # emitted cursor px allowed during the touch
                                  # (8 still ate taps  the down-blip leaks a
                                  # few px before the core settles; user-tuned)
        # Analog L2/R2 click hysteresis (0..32767 trigger units): once the
        # pull crosses the actuation threshold and the click ENGAGES, it stays
        # engaged until the pull drops this far BELOW the threshold. Without
        # it, sensor noise + finger tremor at a slowly-held actuation point
        # flip the pull across the single threshold every frame → spam-click.
        TRIGGER_CLICK_HYSTERESIS = 2200
        # Left trackpad → scroll: pad-Y delta (int16) → wheel notches, scaled by
        # the tray "Left Trackpad Scroll Speed". Lower divisor = faster scroll.
        LPAD_SCROLL_SCALE = 1.0 / 3000.0
        # "Laptop scrolling" jolt smoothing: skin stick-slip on the pad makes
        # the finger catch then suddenly slip a tiny bit  a 1-frame velocity
        # spike that the two-knee blend reads as "fast" (near-raw tau) and
        # passes straight to the page as a vertical jolt. Laptop mode swaps in
        # a GENTLE 1€ filter: the cutoff's speed estimate is itself low-passed
        # (DCUTOFF), so a single-frame spike barely opens the filter (the jolt
        # is smeared smooth) while a SUSTAINED swipe opens it within ~60 ms and
        # scrolls as directly as before. MINCUTOFF matches the old 0.05 s slow
        # tau so the baseline feel is unchanged  this ONLY softens the jolts.
        # Laptop mode only; Normal scrolling keeps the two-knee blend.
        LSCROLL_EURO_MINCUTOFF = 3.2  # Hz; ≈ the old 0.05 s slow tau
        LSCROLL_EURO_BETA = 0.00008   # cutoff gain per pad-unit/sec SUSTAINED
        LSCROLL_EURO_DCUTOFF = 2.5    # Hz; low-pass on the speed estimate
        # "Laptop scrolling" (Options → Touchpads): a quick swipe-and-lift on
        # the LEFT pad sets the page coasting  the scroll velocity at lift
        # carries on and decays exponentially (smooth deceleration), and ANY
        # new touch catches the page dead (the gentle tap). Wheel notches are
        # emitted from the decaying velocity through the fractional
        # accumulator, so the notch cadence slows naturally as the page
        # settles. Lift velocity is averaged over LPAD_VELOCITY_WINDOW of
        # samples so one noisy frame can't launch (or kill) a fling.
        # notches/sec & seconds (mirrors the Windows tray _Watcher SCROLL_*).
        LPAD_VELOCITY_WINDOW = 0.08  # sec averaged for lift velocity
        LPAD_FLING_TRIGGER = 6.0     # lift must exceed this to coast at all
        LPAD_FLING_MAX = 80.0        # cap on the initial coast speed
        LPAD_FLING_TAU = 0.65        # exponential decay time constant (sec)
        LPAD_FLING_STOP = 1.5        # coast ends below this speed
        # "Video Timeline Scrubbing" (Options → Touchpads dropdown): while a
        # video is focused, the LEFT pad becomes a circular dial. Two modes:
        #   "frame"  precise: 9°/rotation per FRAME-STEP key tap ("." fwd /
        #     "," back on YouTube). First step pauses playback, every step
        #     shows the EXACT frame; lifting taps "K" to resume  only if the
        #     dial actually stepped, so an idle tap can't pause a video.
        #   "seek"  fast: 30°/rotation per Right/Left-arrow tap (±5s on
        #     YouTube). No pause/resume (arrow-seeking never stops playback).
        # Both give a haptic detent tick per step. Angle only sampled outside
        # SCRUB_MIN_RADIUS (atan2 noise near the center spins the dial).
        # Focus = focused X11 window title contains a token; cached
        # SCRUB_FOCUS_TTL sec. YouTube only for now (mirrors Windows tray).
        SCRUB_TITLE_TOKENS = ("youtube",)
        # mode -> (step_deg, key_forward, key_back, pauses_playback)
        SCRUB_MODES = {
            "frame": (9.0, "KEY_DOT", "KEY_COMMA", True),
            "seek":  (30.0, "KEY_RIGHT", "KEY_LEFT", False),
        }
        SCRUB_MIN_RADIUS = 9000.0  # pad units; ignore angles inside this
        SCRUB_FOCUS_TTL = 1.0      # sec between focused-title re-checks
        # "Wheel scrolling" dial (Options → Touchpads): one wheel notch per
        # this much thumb rotation on the LEFT pad  clockwise = down, ccw =
        # up. 15° ≈ 24 detents per full circle, a real scroll-wheel feel. The
        # same SCRUB_MIN_RADIUS center dead-zone tames atan2 noise.
        WHEEL_STEP_DEG = 15.0
        # "Text Wheel Selection" dial (Options → Touchpads): while the LEFT
        # mouse button is held over text, one horizontal cursor nudge of
        # TEXTWHEEL_STEP_PX per this much thumb rotation on the LEFT pad  the
        # live drag's selection endpoint follows, snapped to character
        # boundaries BY THE APP. Coarser than the scroll wheel so single
        # letters are easy to land  18° ≈ 20 detents per full circle, each
        # with a haptic tick. Same SCRUB_MIN_RADIUS dead-zone.
        TEXTWHEEL_STEP_DEG = 18.0
        # Horizontal pixels the cursor moves per detent  roughly one average
        # character at 100% scaling; the app's drag logic does the exact
        # snapping, so a wide/narrow glyph just takes one detent more/less.
        TEXTWHEEL_STEP_PX = 8
        # Both wheel dials scale by the Options "Scrolling Sensitivity" slider
        # (the same get_sc_scroll_speed() multiplier the linear scroll uses).
        # Reference multiplier at which the dial reproduces the tuned
        # WHEEL_STEP_DEG feel  the DEFAULT (medium) scroll multiplier so a
        # stock config feels as tuned; higher slider = smaller effective step =
        # more notches per rotation = faster.
        WHEEL_SCROLL_SPEED_REF = 0.55
        # "Wheel smooth" runs the dial's rotation through the SAME gentle 1€
        # filter as Laptop scrolling (the LSCROLL_EURO_* constants), so skin
        # stick-slip catches on the circling thumb are smeared out
        # identically. The shared constants are tuned in linear pad-units, so
        # the angle is converted to ARC pad-units at this nominal circling
        # radius (must be > SCRUB_MIN_RADIUS).
        WHEEL_FILT_RADIUS = 20000.0

        DPAD_MAP = (
            (SCButtons.DPAD_UP,    sui.Keys.KEY_UP),
            (SCButtons.DPAD_DOWN,  sui.Keys.KEY_DOWN),
            (SCButtons.DPAD_LEFT,  sui.Keys.KEY_LEFT),
            (SCButtons.DPAD_RIGHT, sui.Keys.KEY_RIGHT),
        )
        DPAD_MASK = (SCButtons.DPAD_UP | SCButtons.DPAD_DOWN
                     | SCButtons.DPAD_LEFT | SCButtons.DPAD_RIGHT)

        # Zone→key maps built once here (like DPAD_MAP above) instead of as dict
        # literals rebuilt on every HID frame inside the stick handlers  pure
        # per-frame allocation churn on the hot path.
        MEDIA_KEYS = {
            "UP":    sui.Keys.KEY_VOLUMEUP,
            "DOWN":  sui.Keys.KEY_VOLUMEDOWN,
            "LEFT":  sui.Keys.KEY_PREVIOUSSONG,
            "RIGHT": sui.Keys.KEY_NEXTSONG,
        }
        ARROW_KEYS = {
            "UP":    sui.Keys.KEY_UP,
            "DOWN":  sui.Keys.KEY_DOWN,
            "LEFT":  sui.Keys.KEY_LEFT,
            "RIGHT": sui.Keys.KEY_RIGHT,
        }

        chord = _ChordState()

        class _Watcher:
            def __init__(self, owner):
                self.owner = owner
                self.chord = chord
                # Edge / repeat-timer state.
                self._stick_zone_prev = "NEUTRAL"
                self._stick_repeat_at = 0.0
                self._l3_was_pressed = False
                self._arrow_zone_prev = "NEUTRAL"
                self._arrow_repeat_at = 0.0
                self._mouse_last_t = 0.0
                self._mouse_acc_x = 0.0
                self._mouse_acc_y = 0.0
                self._powered_off = False
                self._force_kill_done = False
                # "Toggle Screen" (Options special action)  flips each press:
                # off, then the NEXT press turns it back on. Persists across
                # presses (not a per-hold debounce latch like _powered_off).
                self._screen_off = False
                self._y_alone_was_pressed = False
                self._x_open_was_pressed = False
                self._a_was_pressed = False
                self._b_was_pressed = False
                self._r4_was_pressed = False
                self._r5_was_pressed = False
                # L1 / R1 (bumpers) → previous / next browser tab. Rising edge.
                self._lb_was_pressed = False
                self._rb_was_pressed = False
                # L3 (left stick click) alone → middle click at the cursor
                # (Steam+L3 is Play/Pause). Rising edge, tracked every frame.
                self._l3_mid_prev = False
                self._dpad_repeat_at = {}  # btn -> next-fire time
                # Right trackpad → mouse cursor. Position-deltas while
                # the finger is in contact; reset on lift so a finger
                # re-touch doesn't fling. Mirrors firmware lizard's
                # trackpad-mouse mode, which gets disabled the moment we
                # open iface 2 for Triton input on this hardware.
                self._rpad_touched_was = False
                self._rpad_prev_x = 0
                self._rpad_prev_y = 0
                self._rpad_click_was = False
                self._rpad_last_t = 0.0
                self._rpad_vx = 0.0  # carryover velocity in px/sec
                self._rpad_vy = 0.0
                self._rpad_acc_x = 0.0  # fractional pixel accumulator
                self._rpad_acc_y = 0.0
                self._rpad_filt_x = 0.0  # filtered absolute pad pos (position-domain LP)
                self._rpad_filt_y = 0.0
                self._rpad_dfilt_x = 0.0  # 1€-filtered pad velocity (units/sec)
                self._rpad_dfilt_y = 0.0
                self._rpad_anchor_x = 0.0  # snap dead-zone anchor (last emitted point)
                self._rpad_anchor_y = 0.0
                # Recent touch samples (now, x, y) used to compute a
                # smoothed lift-velocity. Trimmed to the window each
                # frame.
                from collections import deque as _deque
                self._rpad_history = _deque()
                # Kinetic-fling impulse state (velocity hump on lift).
                self._fling_active = False
                self._fling_t0 = 0.0
                self._fling_last_t = 0.0
                self._fling_v0 = 0.0     # lift speed (ramp-up start, px/sec)
                self._fling_peak = 0.0   # boosted peak speed (px/sec)
                self._fling_dirx = 0.0   # impulse unit direction
                self._fling_diry = 0.0
                # Triggers as mouse buttons. R2 = left click (primary
                # finger), L2 = right click. Edge-triggered so a hold
                # registers as a held button (drag-friendly).
                self._lt_was_pressed = False
                self._rt_was_pressed = False
                self._mouse_freeze_until = 0.0  # click-shake guard deadline
                self._freeze_acc = 0.0          # motion swallowed in the freeze
                # "Right Touchpad Tap to Click" candidate for the CURRENT
                # touch: (t, x, y) at touch-down, or None once the touch
                # stops qualifying (too long / clicked / caught a fling /
                # Steam-masked / moved the cursor).
                self._tap_start = None
                self._tap_hist = _deque()  # (t, x, y) raw samples while it lives
                self._tap_moved = 0.0      # cursor px emitted during the touch
                # RAW RPADTOUCH bit last frame (ignores the Steam-hold mask):
                # a Steam chord releasing mid-touch re-seeds the filter, and
                # that re-seed must NOT re-arm a tap with a fresh clock while
                # the finger has really been down the whole time.
                self._rpad_raw_touch_prev = False
                # "Left Touchpad Tap to Click" candidate  the left-pad twin,
                # tracked independently of whatever mode (scroll/dial/scrub/
                # text-wheel) currently owns the pad, off the RAW LPADTOUCH
                # bit/position (same reasoning as the right pad's raw-touch
                # tracking above). No cursor-px gate  a still left-pad touch
                # never moves the cursor under any mode.
                self._lpad_tap_start = None
                self._lpad_tap_hist = _deque()
                self._lpad_raw_touch_prev = False
                self._lpad_tap_last_t = None
                # Left trackpad → scroll wheel (mirrors the Windows takeover).
                self._lpad_prev = None
                self._scroll_acc = 0.0
                self._lpad_filt = 0.0    # filtered absolute left-pad Y (position LP)
                self._lpad_dfilt = 0.0   # 1€-filtered Y velocity (laptop jolt smoothing)
                self._lpad_anchor = 0.0  # trailing dead-zone anchor (left pad Y)
                self._lpad_last_t = None  # last left-pad frame time, for dt
                # "Wheel scrolling" dial state (left pad circular scroll):
                self._wheel_angle = None   # last dial angle (rad); None = idle
                self._wheel_acc = 0.0      # rotation toward the next notch
                # "Wheel smooth" 1€ smoothing state (same tuning as laptop):
                self._wheel_raw = 0.0      # unwrapped raw rotation this touch
                self._wheel_filt = None    # 1€-filtered rotation; None = reseed
                self._wheel_dfilt = 0.0    # filtered arc speed (pad-units/s)
                self._wheel_last_t = 0.0   # last dial frame time, for dt
                # "Text Wheel Selection" dial state (left pad circular text-
                # select while the LEFT mouse button is held):
                self._textwheel_angle = None  # last dial angle (rad); None=idle
                self._textwheel_acc = 0.0     # rotation toward the next nudge
                # Video Timeline Scrubbing dial state (left pad, video focused):
                self._scrub_angle = None   # last dial angle (rad); None = idle
                self._scrub_acc = 0.0      # rotation toward the next step
                self._scrub_stepped = False  # dial frame-stepped this touch
                self._scrub_focus = False  # cached "a video is focused"
                self._scrub_focus_at = 0.0  # when that cache was refreshed
                self._lscroll_hist = _deque()  # (t, cumulative notches)
                self._lscroll_pos = 0.0    # cumulative scroll (notches) this touch
                self._scroll_fling_v = 0.0  # laptop-mode coast velocity (notches/sec)
                self._scroll_fling_last_t = 0.0  # last coast frame time, for dt
                self._lpad_click_prev = False
                # Customization (tray "Keybinds"), built from owner.settings;
                # rebuilt each device cycle (a Save kicks the SC). Two-button
                # chords, per-control overrides, and the analog-stick rebind.
                # Defaults reproduce the built-in behavior exactly.
                _sc_pc = keybinds_runtime.pc_submap(
                    self.owner.settings.get("keybinds", {}).get("sc"))
                # Publish which SC buttons close the OSK (those bound to Escape, B
                # by default) so adusk's OSK handler closes on them  mirrors the
                # keyboard Escape and follows the binding.
                adusk_state.set_osk_close_buttons(
                    keybinds_runtime.resolve_sc_close_buttons(_sc_pc, SCButtons)
                    | set(self._sdl_close_bits))
                self._chords_runtime = keybinds_runtime.build_chords(
                    keybinds_runtime.chords_for(
                        self.owner.settings.get("chords", []), "sc"),
                    SCButtons, sui.Keys)
                self._chord_was_active = [False] * len(self._chords_runtime)
                # Gamepad-mode toggle chords (Hotkeys "Gamepad Mode Toggle"):
                # masks checked every frame, fired via owner.handle_gamepad_toggle.
                # The SC is always desktop on Linux, but the chord still flips the
                # (persist-only) gamepad-mode setting both ways from this watcher.
                self._gp_toggle_masks = keybinds_runtime.build_gamepad_toggle_masks(
                    keybinds_runtime.chords_for(
                        self.owner.settings.get("chords", []), "sc"),
                    SCButtons)
                # "Gyro To Mouse" hotkey chords (the SC Options category's
                # bars): same evaluate-every-frame contract, latched on the App.
                self._gyro_toggle_masks = keybinds_runtime.build_gyro_toggle_masks(
                    keybinds_runtime.chords_for(
                        self.owner.settings.get("chords", []), "sc"),
                    SCButtons)
                self._gyro_imu_on = False
                self._gyro_mouse = _GyroMouse(self.chord.mouse.move)
                # Guide chords (Hotkeys chord whose component is the Guide button):
                # the Steam-held gesture, fired in the steam path (build_guide_chords).
                self._guide_chords = keybinds_runtime.build_guide_chords(
                    keybinds_runtime.chords_for(
                        self.owner.settings.get("chords", []), "sc"),
                    SCButtons, sui.Keys)
                self._guide_chords_prev = {}
                self._sc_overrides = keybinds_runtime.resolve_sc_overrides(
                    _sc_pc, SCButtons, sui.Keys)
                self._ov_prev = {}
                (self._lstick_mouse, self._lstick_actions,
                 self._rstick_mouse, self._rstick_actions) = \
                    keybinds_runtime.resolve_sc_sticks(_sc_pc, sui.Keys)
                self._lmouse_last_t = None
                self._lmouse_acc_x = 0.0
                self._lmouse_acc_y = 0.0
                self._rstick_zone_prev = "NEUTRAL"
                self._rstick_repeat_at = 0.0
                _sc_binds = self.owner.settings.get("keybinds", {}).get("sc")
                _sc_guide = (_sc_binds.get("guide", {})
                             if isinstance(_sc_binds, dict) else {})
                self._guide_binds = keybinds_runtime.resolve_sc_guide(
                    _sc_guide, SCButtons, sui.Keys)
                self._guide_binds_prev = {}
                self._guide_bind_bits = (
                    frozenset(bit for bit, _ in self._guide_binds)
                    | frozenset(bit for bit, _ in self._guide_chords))
                self._guide_rstick_zones = keybinds_runtime.resolve_sc_guide_rstick(
                    _sc_guide, sui.Keys)
                self._guide_rstick_zone_prev = "NEUTRAL"
                self._guide_lstick_zones = keybinds_runtime.resolve_sc_guide_lstick(
                    _sc_guide, sui.Keys)
                self._guide_lstick_zone_prev = "NEUTRAL"
                # Steam TAP rebind (short press, no chord → the bound action;
                # default "Toggle Config GUI"). Only the plain STEAM bit is the
                # guide on Linux ("..."/QAM stays a free chord button), so the
                # tap tracks STEAM alone. Held Steam chords are untouched  a tap
                # is cancelled the moment any real button joins the hold.
                self._guide_taps = keybinds_runtime.resolve_guide_taps(
                    _sc_pc, sui.Keys)
                # previous-frame STEAM held. None = no frame seen yet:
                # a Steam button ALREADY down on the first one was pressed
                # against the previous reader (this watcher is rebuilt on
                # every OSK close / keybind save), so its release must not
                # read as a fresh tap  see _handle_guide_taps.
                self._gtap_prev = None
                self._gtap_press_t = 0.0      # rising-edge time of the hold
                self._gtap_other = False      # a real button seen during the hold

            def _handle_guide_binds(self, sc, sci):
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
                """Fire Guide (Steam-held) chords from the Hotkeys tab  key combo
                or program launch. Guide + button fires once per rising edge of the
                other button while Steam is held; Guide ALONE (bit 0) fires once per
                Steam hold. Called every frame (keyed by index) so the edge resets
                when Steam or the button is released. The conflicting Chords-tab bind
                was cleared in the picker, so only the chord fires."""
                b = sci.buttons
                for i, (bit, action) in enumerate(self._guide_chords):
                    pressed = guide_now and (True if bit == 0 else bool(b & bit))
                    if pressed and not self._guide_chords_prev.get(i, False):
                        self._fire_chord(sc, action)
                    self._guide_chords_prev[i] = pressed

            def _handle_guide_rstick(self, sc, sci):
                """Fire right-stick directional guide binds on zone entry while
                Steam is held. Fires once per zone transition (no auto-repeat)."""
                x = sci.rstick_x
                y = sci.rstick_y
                DEAD = 8000
                zone = "NEUTRAL"
                if abs(x) > DEAD or abs(y) > DEAD:
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
                """Fire left-stick directional guide binds on zone entry while
                Steam is held. Fires once per zone transition (no auto-repeat)."""
                x = sci.lstick_x
                y = sci.lstick_y
                DEAD = 8000
                zone = "NEUTRAL"
                if abs(x) > DEAD or abs(y) > DEAD:
                    if abs(y) >= abs(x):
                        zone = "UP" if y > 0 else "DOWN"
                    else:
                        zone = "RIGHT" if x > 0 else "LEFT"
                if zone != self._guide_lstick_zone_prev and zone != "NEUTRAL":
                    action = self._guide_lstick_zones.get(zone)
                    if action:
                        self._fire_guide_action(sc, action, mode="guide")
                self._guide_lstick_zone_prev = zone

            # Max press→release time (s) for a clean Steam TAP. Longer holds are
            # treated as a (possibly chord) hold and never fire the tap rebind.
            _GUIDE_TAP_S = 0.28

            def _handle_guide_taps(self, sc, sci, now):
                """Steam TAP → bound action (default "Toggle Config GUI"). Fires
                only on a clean tap: a short STEAM press+release with NO other
                REAL button during the hold, so the held Steam chords are
                untouched. Passive capacitive sensors (resting thumb on a pad/
                stick, and the always-on grip-rest bits set just by holding the
                controller) are excluded, or every tap would read as a chord and
                cancel. Desktop-only (the SC on Linux is always desktop)."""
                action = self._guide_taps.get("steam")
                if not action or action[0] == "none":
                    return
                TOUCH = (SCButtons.RPADTOUCH | SCButtons.LPADTOUCH
                         | SCButtons.RPADJOY_TOUCH | SCButtons.LPADJOY_TOUCH
                         | SCButtons.RGRIP_REST | SCButtons.LGRIP_REST)
                held = bool(sci.buttons & SCButtons.STEAM)
                if self._gtap_prev is None:
                    # First frame of this watcher's life: adopt an already-held
                    # Steam as an in-progress, already-chorded hold so the
                    # release that ends it fires nothing (closing the OSK with
                    # Steam down otherwise popped the config GUI open).
                    self._gtap_prev = held
                    self._gtap_other = held
                    self._gtap_press_t = now
                    return
                if held and not self._gtap_prev:              # rising edge
                    self._gtap_press_t = now
                    self._gtap_other = False
                elif held:                                    # during the hold
                    if sci.buttons & ~(int(SCButtons.STEAM) | int(TOUCH)):
                        self._gtap_other = True               # a chord  cancel
                elif self._gtap_prev:                         # falling edge
                    if ((now - self._gtap_press_t) <= self._GUIDE_TAP_S
                            and not self._gtap_other):
                        self._fire_guide_action(sc, action)
                self._gtap_prev = held

            def _fire_guide_action(self, sc, action, mode="pc"):
                """Dispatch a single edge-triggered action (one press = one fire).
                Shared by guide-hold binds and the desktop per-control override
                edge path, so both understand the full action vocabulary.
                click/hold here are momentary; the override path handles true held
                click/modifier separately before delegating here. `mode` names
                which tab ("pc"/"guide") this dispatch's binding lives in  read
                by the "profile_cycle" action so ONE dropdown entry cycles
                whichever mode was actually active when it fired."""
                typ = action[0]
                if typ == "tap":
                    self.chord.kb.pressEvent([action[1]])
                    self.chord.kb.releaseEvent([action[1]])
                elif typ == "combo":
                    for k in action[1]:
                        self.chord.kb.pressEvent([k])
                    for k in reversed(action[1]):
                        self.chord.kb.releaseEvent([k])
                elif typ == "click":
                    self.chord.mouse.button(action[1], True)
                    self.chord.mouse.button(action[1], False)
                elif typ == "hold":
                    if action[1] == keybinds_runtime.MIC_PTT_KEY:
                        # Push to Talk reached an EDGE-only dispatch site (a
                        # stick zone, a Steam/"..." tap) where there is no
                        # button-up to close on. Degrade to a latch: fire once
                        # to open the mic, again to close.
                        mic_ptt_hold("latch", "latch" not in _mic_ptt_holders)
                    elif action[1] == keybinds_runtime.GYRO_MOUSE_KEY:
                        # Same story for "Gyro To Mouse"  no release to end a
                        # hold on, so the press flips the gyro instead (see
                        # gyro_action_flip).
                        gyro_action_flip("sc")
                    else:
                        self.chord.kb.pressEvent([action[1]])
                        self.chord.kb.releaseEvent([action[1]])
                elif typ == "scroll":
                    self.chord.mouse.scroll(0, action[1])
                elif typ == "mic_mute_toggle":
                    mic_toggle_mute()
                elif typ == "toggle_magnifier":
                    import subprocess
                    try:
                        # On Linux open/close the magnifier (xmag or gnome-magnifier).
                        r = subprocess.run(
                            ["pgrep", "-x", "xmag"],
                            capture_output=True, timeout=2)
                        if r.returncode == 0:
                            subprocess.Popen(["pkill", "-x", "xmag"])
                        else:
                            subprocess.Popen(["xmag"])
                    except Exception as e:
                        print(f"toggle_magnifier failed: {e!r}")
                elif typ == "show_keyboard":
                    self.owner._pending_open_controller = "sc"
                    self.owner._open_kbd_event.set()
                    self.chord.release_all_held()
                    sc.addExit()
                elif typ == "power_off":
                    if not self._powered_off:
                        self._powered_off = True
                        sc.turn_off()
                elif typ == "force_kill":
                    if not self._force_kill_done:
                        self._force_kill_done = True
                        _force_kill_foreground_game()
                elif typ == "alt_tab":
                    # Hold Alt across repeated presses (don't release it here) so
                    # the switcher UI stays open and each subsequent press just
                    # taps Tab to cycle  mirrors the hardcoded Steam+VIEW
                    # behavior. Alt is released generically when the Steam hold
                    # ends ("if not steam_now: self.chord.release_alt()"),
                    # regardless of which button dispatched this.
                    if not self.chord.alt_held:
                        self.chord.kb.pressEvent([sui.Keys.KEY_LEFTALT])
                        self.chord.alt_held = True
                    self.chord.kb.pressEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                elif typ == "xbutton":
                    # Page Previous/Next: mouse Back/Forward side buttons.
                    btn = "back" if action[1] == 1 else "forward"
                    self.chord.mouse.button(btn, True)
                    self.chord.mouse.button(btn, False)
                elif typ in ("brightness_up", "brightness_down"):
                    # Internal-panel brightness in ±10% steps via brightnessctl
                    # (no injectable key for it). One step per press.
                    import subprocess
                    arg = "+10%" if typ == "brightness_up" else "10%-"
                    try:
                        subprocess.Popen(["brightnessctl", "set", arg])
                    except Exception as e:
                        print(f"brightness failed: {e!r}")
                elif typ == "lock_pc":
                    import subprocess
                    try:
                        subprocess.Popen(["loginctl", "lock-session"])
                    except Exception as e:
                        print(f"lock_pc failed: {e!r}")
                elif typ == "screen_off":
                    # Toggle: press once = off, press again = back on.
                    self._screen_off = not self._screen_off
                    import subprocess
                    arg = "off" if self._screen_off else "on"
                    try:
                        subprocess.Popen(["xset", "dpms", "force", arg])
                    except Exception as e:
                        print(f"screen_off failed: {e!r}")
                elif typ == "profile_cycle":
                    # Advance the Steam Controller's active profile slot for
                    # whichever tab this binding's dispatch site is in
                    # (`mode`, passed by the caller) to the next existing one
                    # (wraps to 1). Edge-triggered = one advance per press.
                    # SC-only.
                    try:
                        self.owner.cycle_keybind_profile("sc", mode)
                    except Exception as e:
                        print(f"profile cycle failed: {e!r}")
                elif typ == "toggle_gui":
                    # Open/close the config GUI (default Steam-button tap)  the
                    # App owns the picker + game-focus restore.
                    try:
                        self.owner.toggle_config_gui()
                    except Exception as e:
                        print(f"toggle_gui failed: {e!r}")
                elif typ == "gamepad_mode_toggle":
                    # Flip Desktop <-> Gamepad controls. Edge-triggered = one
                    # flip per press; the same App call the Hotkeys toggle
                    # chords make.
                    gamepad_mode_flip()
                elif typ == "big_picture":
                    # Open Big Picture, or leave it if it's already up.
                    big_picture_toggle_async()

            def _fire_directional(self, action):
                """Fire a stick directional action: tap a key, a combo, or scroll.
                Click/hold/none/show_keyboard aren't meaningful for a held
                direction, so they're ignored."""
                typ = action[0]
                if typ == "tap":
                    self.chord.kb.pressEvent([action[1]])
                    self.chord.kb.releaseEvent([action[1]])
                elif typ == "combo":
                    for k in action[1]:
                        self.chord.kb.pressEvent([k])
                    for k in reversed(action[1]):
                        self.chord.kb.releaseEvent([k])
                elif typ == "scroll":
                    self.chord.mouse.scroll(0, action[1])

            def _fire_chord(self, sc, action):
                try:
                    if action["type"] == "keys":
                        keys = action["keys"]
                        for k in keys:
                            self.chord.kb.pressEvent([k])
                        for k in reversed(keys):
                            self.chord.kb.releaseEvent([k])
                    elif action["type"] == "launch":
                        _launch_program(action["path"], action.get("args", ""))
                except Exception as e:
                    print(f"chord fire failed: {e!r}")
                if adusk_state.is_rumble_enabled("sc"):
                    sc.haptic_click()

            def _handle_chords(self, sc, sci, steam_now):
                """Fire two-button chords on the both-held rising edge (only when
                Steam isn't held). Returns the bitmask of currently-held chords so
                the caller masks those buttons out of the single-button handlers.
                keybinds_runtime.build_chords now tags each chord `is_gamepad`
                (True for chords built entirely from green "xi_" Gamepad-Layout
                aliases  see [[hotkeys-xinput-aliases]]). The SC on Linux is
                always desktop (no virtual-pad path yet), so those never apply
                here and are skipped rather than firing unconditionally."""
                suppress = 0
                for i, (mask, action, is_gamepad) in enumerate(self._chords_runtime):
                    if is_gamepad:
                        self._chord_was_active[i] = False
                        continue
                    active = (not steam_now) and ((sci.buttons & mask) == mask)
                    if active:
                        suppress |= mask
                        if not self._chord_was_active[i]:
                            self._fire_chord(sc, action)
                    self._chord_was_active[i] = active
                return suppress

            def _handle_overrides(self, sc, raw_buttons, steam_now):
                """Dispatch per-control rebinds (controls changed from default).
                Returns the bitmask of overridden controls so the caller masks
                them out of the hardcoded handlers. Gated off while Steam is held
                (so Steam chords keep the raw frame; holds/clicks release)."""
                suppress = 0
                for cid, bit, action in self._sc_overrides:
                    pressed = (not steam_now) and bool(raw_buttons & bit)
                    if not steam_now:
                        suppress |= bit
                    typ = action[0]
                    if typ == "click":
                        # True held click (drag-friendly)  not momentary.
                        self.chord.set_mouse_button(action[1], "ov:" + cid, pressed)
                    elif typ == "hold":
                        # True held modifier  pressed while the button is held.
                        self.chord.set_key(action[1], "ov:" + cid, pressed,
                                           kind="sc")
                    elif typ != "none":
                        # Everything else (tap / combo / scroll / show-keyboard /
                        # media / system action) fires once per press via the
                        # shared dispatcher.
                        if pressed and not self._ov_prev.get(cid, False):
                            self._fire_guide_action(sc, action)
                        self._ov_prev[cid] = pressed
                return suppress

            def _video_focused(self, now):
                """Cached focused-window check for Video Timeline Scrubbing:
                True while the focused X11 window title names a video site."""
                if now - self._scrub_focus_at >= SCRUB_FOCUS_TTL:
                    self._scrub_focus_at = now
                    title = _get_focused_window_title().lower()
                    self._scrub_focus = any(
                        tok in title for tok in SCRUB_TITLE_TOKENS)
                return self._scrub_focus

            def _handle_pad_scrub(self, sc, sci, now, mode):
                """Video Timeline Scrubbing: LEFT pad as a circular dial. One
                step-key tap per this mode's rotation step  clockwise =
                forward, counter-clockwise = back  with a haptic detent
                tick per step. In "frame" mode the first step pauses
                playback and lifting taps "K" to resume at the exact frame
                (only if the dial actually stepped); "seek" mode never
                pauses, so lifting is a no-op."""
                if mode == "hover":
                    # The mouse-like hover scrub (cursor rides the progress
                    # bar, click-to-seek on lift) needs the focused window's
                    # rect + absolute cursor warping  not ported to the X11
                    # runtime yet, so fall back to fast seek.
                    mode = "seek"
                step_deg, key_fwd, key_back, pauses = SCRUB_MODES[mode]
                if not (sci.buttons & SCButtons.LPADTOUCH):
                    if pauses and self._scrub_stepped:
                        # Thumb lifted after scrubbing  resume playback here
                        # (frame-stepping left the player paused).
                        self.chord.kb.pressEvent([sui.Keys.KEY_K])
                        self.chord.kb.releaseEvent([sui.Keys.KEY_K])
                    self._scrub_stepped = False
                    self._scrub_angle = None
                    self._scrub_acc = 0.0
                    return
                if pauses and not self._scrub_stepped:
                    # Frame mode pauses the INSTANT the pad is touched (K);
                    # the lift branch above taps K again to resume.
                    self.chord.kb.pressEvent([sui.Keys.KEY_K])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_K])
                    self._scrub_stepped = True
                x, y = float(sci.lpad_x), float(sci.lpad_y)
                if (x * x + y * y) ** 0.5 < SCRUB_MIN_RADIUS:
                    # Too close to the center  atan2 is all noise there.
                    # Keep the last angle so re-entering the ring is smooth.
                    return
                ang = math.atan2(y, x)
                if self._scrub_angle is None:
                    self._scrub_angle = ang  # first ring sample: reference
                    return
                d = ang - self._scrub_angle
                # Wrap to (-pi, pi] so the ±180° seam doesn't spin the dial.
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
                    self.chord.kb.pressEvent([key])
                    self.chord.kb.releaseEvent([key])
                    self._scrub_stepped = True
                    if adusk_state.is_rumble_enabled("sc"):
                        sc.haptic_pad_click()  # dial detent tick

            def _handle_pad_text_wheel(self, sc, sci, now):
                """Text Wheel Selection (Options → Touchpads): while a
                left-click control (R2 / right-pad click) HOLDS the left mouse
                button over text, the LEFT pad becomes a fine text-selection
                dial. Each TEXTWHEEL_STEP_DEG of thumb rotation nudges the
                CURSOR horizontally by TEXTWHEEL_STEP_PX with the drag still
                live  the app's own drag-selection then snaps the endpoint to
                character boundaries  with a haptic detent tick. CLOCKWISE
                extends forward, counter-clockwise back. Driving the REAL drag
                (instead of injecting Shift+Arrow, the first two attempts) is
                what makes it work everywhere: keyboard selection is dead on
                non-editable content (browser pages) without caret browsing,
                only extends an EXISTING highlight, and dies when the selection
                collapses at its anchor  a live mouse drag has none of those
                limits. Same dial angle math as _handle_pad_wheel."""
                if not (sci.buttons & SCButtons.LPADTOUCH):
                    self._textwheel_angle = None
                    self._textwheel_acc = 0.0
                    return
                x, y = float(sci.lpad_x), float(sci.lpad_y)
                if (x * x + y * y) ** 0.5 < SCRUB_MIN_RADIUS:
                    # Near the center atan2 is all noise  hold the last angle.
                    return
                ang = math.atan2(y, x)
                if self._textwheel_angle is None:
                    self._textwheel_angle = ang   # first ring sample: reference
                    return
                d = ang - self._textwheel_angle
                # Wrap to (-pi, pi] so the ±180° seam doesn't spin the dial.
                if d > math.pi:
                    d -= 2.0 * math.pi
                elif d <= -math.pi:
                    d += 2.0 * math.pi
                self._textwheel_angle = ang
                # Clockwise thumb motion DECREASES the atan2 angle → forward.
                self._textwheel_acc -= d
                step = math.radians(TEXTWHEEL_STEP_DEG)
                while abs(self._textwheel_acc) >= step:
                    if self._textwheel_acc > 0:
                        self._textwheel_acc -= step
                        dx = TEXTWHEEL_STEP_PX     # clockwise → extend right
                    else:
                        self._textwheel_acc += step
                        dx = -TEXTWHEEL_STEP_PX    # ccw → extend left
                    self.chord.mouse.move(dx, 0)
                    if adusk_state.is_rumble_enabled("sc"):
                        sc.haptic_pad_click()      # detent tick

            def _handle_pad_wheel(self, sc, sci, steam_now, now, smooth):
                """"Wheel scrolling" modes: LEFT pad as a circular scroll dial.
                Track the touch's angle around the pad center  CLOCKWISE
                scrolls DOWN, counter-clockwise scrolls UP. "wheel" emits one
                discrete wheel notch per WHEEL_STEP_DEG with a haptic detent
                tick (a real clicky wheel); "wheel_smooth" drops the tick for a
                continuous feel. NOTE: X11/XTest can only send WHOLE wheel
                notches, so on this runtime "smooth" is just the same dial
                minus the haptic tick  a TRUE hi-res analog glide (the Windows
                MOUSEEVENTF_WHEEL 1/120-notch stream) needs a uinput
                REL_WHEEL_HI_RES device (pending, same gap as laptop mode's
                hi-res scroll). Idle during a Steam-hold (pad repurposed)."""
                # Dial mode owns no linear coast/touch state  clear any left
                # over from a mode switch so it can't leak a phantom scroll.
                self._scroll_fling_v = 0.0
                self._lpad_prev = None
                if steam_now or not (sci.buttons & SCButtons.LPADTOUCH):
                    self._wheel_angle = None
                    self._wheel_acc = 0.0
                    return
                x, y = float(sci.lpad_x), float(sci.lpad_y)
                if (x * x + y * y) ** 0.5 < SCRUB_MIN_RADIUS:
                    # Near the center atan2 is all noise  hold the last angle
                    # so re-entering the ring continues smoothly.
                    return
                ang = math.atan2(y, x)
                if self._wheel_angle is None:
                    self._wheel_angle = ang  # first ring sample: reference only
                    self._wheel_filt = None  # reseed the smooth-mode filter too
                    self._wheel_last_t = now
                    return
                d = ang - self._wheel_angle
                # Wrap to (-pi, pi] so the ±180° seam doesn't spin the dial.
                if d > math.pi:
                    d -= 2.0 * math.pi
                elif d <= -math.pi:
                    d += 2.0 * math.pi
                self._wheel_angle = ang
                if smooth:
                    # "Wheel smooth" gets the SAME gentle 1€ smoothing as
                    # Laptop scrolling (shared LSCROLL_EURO_* tuning): the
                    # low-passed speed estimate ignores 1-frame stick-slip
                    # spikes of the circling thumb (skin-catch jolts smeared
                    # out) while a sustained spin opens the cutoff and scrolls
                    # as directly as before. Angle speed is converted to ARC
                    # pad-units (× WHEEL_FILT_RADIUS) so the shared constants
                    # mean the same physical finger motion.
                    dt = now - self._wheel_last_t
                    if dt <= 0.0 or dt > 0.1:
                        dt = 1.0 / 60.0
                    if self._wheel_filt is None:
                        self._wheel_raw = 0.0
                        self._wheel_filt = 0.0
                        self._wheel_dfilt = 0.0
                    ad = dt / (1.0 / (2.0 * math.pi
                                      * LSCROLL_EURO_DCUTOFF) + dt)
                    self._wheel_dfilt += ad * (
                        (d / dt) * WHEEL_FILT_RADIUS - self._wheel_dfilt)
                    fc = (LSCROLL_EURO_MINCUTOFF
                          + LSCROLL_EURO_BETA * abs(self._wheel_dfilt))
                    a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
                    self._wheel_raw += d
                    _prev_filt = self._wheel_filt
                    self._wheel_filt += a * (self._wheel_raw - self._wheel_filt)
                    d = self._wheel_filt - _prev_filt
                self._wheel_last_t = now
                # Clockwise thumb motion DECREASES the atan2 angle, so
                # accumulating the raw signed delta drives it NEGATIVE
                # clockwise → down.
                self._wheel_acc += d
                # Scale by the Options "Scrolling Sensitivity" slider (same
                # multiplier the linear scroll reads), referenced so the
                # default sensitivity reproduces the tuned WHEEL_STEP_DEG feel;
                # higher slider = smaller step = more notches = faster. (X11
                # can only emit whole notches, so this scales the notch spacing
                # for BOTH wheel + wheel_smooth here.)
                mult = adusk_state.get_sc_scroll_speed() / WHEEL_SCROLL_SPEED_REF
                if mult < 0.05:
                    mult = 0.05
                # "Invert Scrolling" (Touchpads scroll-settings cog): flip dir.
                inv = -1 if adusk_state.is_sc_scroll_invert_enabled() else 1
                step = math.radians(WHEEL_STEP_DEG) / mult
                while abs(self._wheel_acc) >= step:
                    if self._wheel_acc <= -step:
                        self._wheel_acc += step
                        self.chord.mouse.scroll(0, -1 * inv)  # clockwise → down
                    else:
                        self._wheel_acc -= step
                        self.chord.mouse.scroll(0, 1 * inv)   # ccw → up
                    if not smooth and adusk_state.is_rumble_enabled("sc"):
                        sc.haptic_pad_click()  # wheel detent tick

            def _handle_pad_scroll(self, sc, sci, steam_now, now):
                """Left trackpad → scroll wheel (vertical delta of the touch),
                scaled by the tray 'Left Trackpad Scroll Speed'. In "Laptop
                scrolling" (Options → Touchpads) a quick swipe-and-lift keeps
                the page coasting with a smooth deceleration; a tap catches it.
                In "Wheel scrolling" the left pad is a circular dial instead
                (see _handle_pad_wheel)."""
                mode = adusk_state.get_sc_scroll_mode()
                if mode in ("wheel", "wheel_smooth"):
                    self._handle_pad_wheel(sc, sci, steam_now, now,
                                           smooth=(mode == "wheel_smooth"))
                    return
                laptop = mode == "laptop"
                # "Invert Scrolling" (Touchpads scroll-settings cog): flips
                # direction for both the live scroll and the fling coast.
                inv = -1 if adusk_state.is_sc_scroll_invert_enabled() else 1
                if steam_now or not (sci.buttons & SCButtons.LPADTOUCH):
                    if self._lpad_prev is not None:
                        # Lift edge: fast enough swipe → start coasting at the
                        # finger's release velocity (windowed average).
                        self._lpad_prev = None
                        if (laptop and not steam_now
                                and len(self._lscroll_hist) >= 2):
                            t0, p0 = self._lscroll_hist[0]
                            dt = now - t0
                            if dt > 1e-3:
                                v = (self._lscroll_pos - p0) / dt
                                if abs(v) >= LPAD_FLING_TRIGGER:
                                    self._scroll_fling_v = max(
                                        -LPAD_FLING_MAX,
                                        min(LPAD_FLING_MAX, v))
                                    self._scroll_fling_last_t = now
                        self._lscroll_hist.clear()
                    if self._scroll_fling_v:
                        if not laptop or steam_now:  # mode off / chord  stop dead
                            self._scroll_fling_v = 0.0
                            return
                        dt = now - self._scroll_fling_last_t
                        dt = max(1e-3, min(dt, 1.0 / 30.0))
                        self._scroll_fling_last_t = now
                        self._scroll_acc += self._scroll_fling_v * dt
                        self._scroll_fling_v *= math.exp(-dt / LPAD_FLING_TAU)
                        if abs(self._scroll_fling_v) < LPAD_FLING_STOP:
                            self._scroll_fling_v = 0.0
                        steps = int(self._scroll_acc)
                        if steps:
                            self._scroll_acc -= steps
                            self.chord.mouse.scroll(0, steps * inv)
                    return
                # Touching: any contact catches a coasting page (the gentle tap).
                self._scroll_fling_v = 0.0
                x, y = sci.lpad_x, sci.lpad_y
                if self._lpad_prev is None:
                    # Fresh touch  seed the filter + anchor at the touch point
                    # (first frame can't jump) and restart lift-velocity tracking.
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
                        # Laptop mode: gentle 1-D 1€ filter (LSCROLL_EURO_*) 
                        # the low-passed speed estimate ignores 1-frame
                        # stick-slip spikes (the skin-catch jolts stay closed
                        # and get smeared smooth), while a sustained swipe
                        # opens the cutoff and scrolls as directly as before.
                        ad = dt / (1.0 / (2.0 * math.pi
                                          * LSCROLL_EURO_DCUTOFF) + dt)
                        self._lpad_dfilt += ad * (
                            (y - self._lpad_prev[1]) / dt - self._lpad_dfilt)
                        fc = (LSCROLL_EURO_MINCUTOFF
                              + LSCROLL_EURO_BETA * abs(self._lpad_dfilt))
                        a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
                    else:
                        # Normal mode: adaptive position-domain low-pass
                        # (two-knee tau blend): heavy when the finger moves
                        # slowly (de-shakes a shaky thumb), near-raw on a fast
                        # swipe  tuned separately; swipes want the raw top end.
                        pad_speed = abs(y - self._lpad_prev[1]) / dt
                        t = (pad_speed - RPAD_SMOOTH_SPEED_LO) / (
                            RPAD_SMOOTH_SPEED_HI - RPAD_SMOOTH_SPEED_LO)
                        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                        tau = RPAD_SMOOTH_TAU_SLOW + (
                            RPAD_SMOOTH_TAU_FAST - RPAD_SMOOTH_TAU_SLOW) * t
                        a = dt / (tau + dt)
                    self._lpad_filt += a * (y - self._lpad_filt)
                    # Same trailing dead-zone as the right pad, 1-D: scroll
                    # only by the filtered point's OVERSHOOT past RPAD_DEADZONE
                    # and re-trail the anchor  a resting/tremoring finger
                    # gives ZERO scroll (no jiggle), real motion ramps in.
                    d = self._lpad_filt - self._lpad_anchor
                    if abs(d) > RPAD_DEADZONE:
                        sign = 1.0 if d > 0 else -1.0
                        over = (abs(d) - RPAD_DEADZONE) * sign
                        self._lpad_anchor = self._lpad_filt - RPAD_DEADZONE * sign
                        speed = LPAD_SCROLL_SCALE * adusk_state.get_sc_scroll_speed()
                        delta = over * speed
                        self._scroll_acc += delta
                        self._lscroll_pos += delta
                        steps = int(self._scroll_acc)
                        if steps:
                            self._scroll_acc -= steps
                            self.chord.mouse.scroll(0, steps * inv)
                self._lpad_prev = (x, y)
                self._lscroll_hist.append((now, self._lscroll_pos))
                while (self._lscroll_hist and
                       now - self._lscroll_hist[0][0] > LPAD_VELOCITY_WINDOW):
                    self._lscroll_hist.popleft()

            def _mouse_shake_guard(self):
                """Freeze pad-mouse output for RPAD_CLICK_FREEZE_S: squeezing
                a trigger or clicking a pad physically wobbles the finger
                resting on the right pad, and that wobble used to leak out as
                a tiny drag between the two clicks of a double-click (folders
                dragged instead of opening). The dead-zone anchor snaps to
                the filtered point so any pending overshoot dies with it;
                _handle_trackpad_mouse lets a REAL drag break out early."""
                self._mouse_freeze_until = time.monotonic() + RPAD_CLICK_FREEZE_S
                self._freeze_acc = 0.0
                # A physical pad/trigger click edge also kills any live
                # tap-to-click candidate (both pads): the real button won
                # this touch, so the eventual lift must not add a second,
                # synthetic click.
                self._tap_start = None
                self._lpad_tap_start = None
                if self._rpad_touched_was:
                    self._rpad_anchor_x = self._rpad_filt_x
                    self._rpad_anchor_y = self._rpad_filt_y

            def _trigger_click_now(self, was_pressed, digital, analog, thr):
                """Whether an L2/R2 trigger counts as a mouse click this
                frame. The firmware full-pull digital bit always counts. With
                an analog actuation threshold set, the pull also counts past
                it  but with HYSTERESIS: it engages at `thr` and only
                releases once the pull falls TRIGGER_CLICK_HYSTERESIS below
                `thr`. That dead band absorbs the sensor noise / finger
                tremor that otherwise spam-clicks when the pull is held right
                at the actuation point."""
                if thr is None:
                    return digital
                engage = thr if not was_pressed else thr - TRIGGER_CLICK_HYSTERESIS
                return digital or analog >= engage

            def _handle_lpad_click(self, sc, sci, steam_now):
                """Left trackpad CLICK → middle mouse button (held, ref-counted)."""
                lpad = bool(sci.buttons & SCButtons.LPAD) and not steam_now
                if lpad != self._lpad_click_prev:
                    self._mouse_shake_guard()
                if lpad and not self._lpad_click_prev and adusk_state.is_rumble_enabled("sc"):
                    sc.haptic_pad_click()
                self.chord.set_mouse_button("middle", "lpad", lpad)
                self._lpad_click_prev = lpad

            def _handle_lpad_tap(self, sc, sci, steam_now, now):
                """"Left Touchpad Tap to Click": the left-pad twin of the
                right-pad tap-to-click  a quick, still touch-and-lift on the
                LEFT pad fires a RIGHT click. Tracked independently of
                whatever mode (scroll/dial/scrub/text-wheel) currently owns
                the pad, off the RAW LPADTOUCH bit/position  those handlers
                repeatedly reset their own touch state (_lpad_prev etc.) for
                their own reasons, which would false-fire or starve a naive
                tracker piggybacking on them. Same qualification math as the
                right-pad tap (RPAD_TAP_* constants): lift within MAX_S,
                lasted at least MIN_S, wander (bounding-box of the core
                samples with both contact-transient trims) within MAX_DIST.
                No cursor-px gate  a still left-pad touch never moves the
                cursor under any mode. Cancelled by a physical click/trigger
                edge (via _mouse_shake_guard, which also clears this
                candidate), by touching down while a coasting scroll fling is
                caught (that touch stops the coast, it doesn't tap), while a
                trigger/click already holds a mouse button, or by a
                Steam-hold masking the pad mid-touch  mirrors the right
                pad's raw-touch tracking so a Steam release can't re-arm a
                tap on a finger that never lifted."""
                raw_touch = bool(sci.buttons & SCButtons.LPADTOUCH)
                if raw_touch:
                    x, y = float(sci.lpad_x), float(sci.lpad_y)
                    if not self._lpad_raw_touch_prev:
                        if (adusk_state.is_tap_to_click_left_enabled()
                                and not steam_now
                                and not self._scroll_fling_v
                                and not (self._lt_was_pressed
                                         or self._rt_was_pressed
                                         or self._rpad_click_was
                                         or self._lpad_click_prev)):
                            self._lpad_tap_start = (now, x, y)
                            self._lpad_tap_hist.clear()
                            self._lpad_tap_hist.append((now, x, y))
                        else:
                            self._lpad_tap_start = None
                    elif self._lpad_tap_start is not None:
                        if now - self._lpad_tap_start[0] > RPAD_TAP_MAX_S:
                            self._lpad_tap_start = None
                            self._lpad_tap_hist.clear()
                        else:
                            self._lpad_tap_hist.append((now, x, y))
                    self._lpad_tap_last_t = now
                elif self._lpad_raw_touch_prev:
                    tap = self._lpad_tap_start
                    self._lpad_tap_start = None
                    if (tap is not None and not steam_now
                            and adusk_state.is_tap_to_click_left_enabled()):
                        dur = (self._lpad_tap_last_t or now) - tap[0]
                        if RPAD_TAP_MIN_S <= dur <= RPAD_TAP_MAX_S:
                            t_lo = tap[0] + RPAD_TAP_START_SKIP
                            t_hi = ((self._lpad_tap_last_t or now)
                                    - RPAD_LIFT_SKIP)
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
                                  else (max_x - min_x) ** 2
                                  + (max_y - min_y) ** 2)
                            if d2 <= RPAD_TAP_MAX_DIST ** 2:
                                self.chord.set_mouse_button(
                                    "right", "lpad_tap", True)
                                self.chord.set_mouse_button(
                                    "right", "lpad_tap", False)
                                if adusk_state.is_rumble_enabled("sc"):
                                    sc.haptic_pad_click()
                                self._mouse_shake_guard()
                    self._lpad_tap_hist.clear()
                self._lpad_raw_touch_prev = raw_touch

            def _handle_media_chords(self, sc, sci, steam_now, now):
                # Steam+L3 → Play/Pause. Skipped when lstick_click has a guide bind.
                l3_now = bool(sci.buttons & SCButtons.L3)
                if steam_now and l3_now and not self._l3_was_pressed:
                    if int(SCButtons.L3) not in self._guide_bind_bits:
                        self.chord.kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
                        self.chord.kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
                self._l3_was_pressed = l3_now

                x = sci.lstick_x
                y = sci.lstick_y
                zone = "NEUTRAL"
                # Skip zone detection when guide lstick zones are bound.
                if steam_now and not self._guide_lstick_zones and (
                        abs(x) > STICK_DEADZONE or abs(y) > STICK_DEADZONE):
                    if abs(y) >= abs(x):
                        zone = "UP" if y > 0 else "DOWN"
                    else:
                        zone = "RIGHT" if x > 0 else "LEFT"
                key = MEDIA_KEYS.get(zone)

                fire = False
                if zone != self._stick_zone_prev:
                    fire = zone != "NEUTRAL"
                    self._stick_repeat_at = now + STICK_HOLD_DELAY
                elif zone in ("UP", "DOWN") and now >= self._stick_repeat_at:
                    fire = True
                    self._stick_repeat_at = now + STICK_VOL_REPEAT
                self._stick_zone_prev = zone

                if fire and key is not None:
                    self.chord.kb.pressEvent([key])
                    self.chord.kb.releaseEvent([key])

            def _handle_arrow_stick(self, sci, steam_now, now):
                # Left stick → its bound per-direction action (default arrows),
                # dominant axis, tap-then-repeat. Rebindable via self._lstick_actions.
                x = sci.lstick_x
                y = sci.lstick_y
                zone = "NEUTRAL"
                if not steam_now and (abs(x) > STICK_DEADZONE
                                      or abs(y) > STICK_DEADZONE):
                    if abs(y) >= abs(x):
                        zone = "UP" if y > 0 else "DOWN"
                    else:
                        zone = "RIGHT" if x > 0 else "LEFT"
                action = self._lstick_actions.get(zone)

                fire = False
                if zone != self._arrow_zone_prev:
                    fire = zone != "NEUTRAL"
                    self._arrow_repeat_at = now + ARROW_HOLD_DELAY
                elif zone != "NEUTRAL" and now >= self._arrow_repeat_at:
                    fire = True
                    self._arrow_repeat_at = now + ARROW_REPEAT
                self._arrow_zone_prev = zone

                if fire and action is not None and action[0] != "none":
                    self._fire_directional(action)

            def _handle_dpad(self, sci, steam_now, now):
                """D-pad → arrow keys with the same tap/hold-repeat feel as
                the left stick. Skipped while Steam is held so chord uses
                of the d-pad stay free for later."""
                if steam_now:
                    # Clear repeat timers so a freshly-released hold
                    # doesn't auto-fire on the next non-Steam frame.
                    self._dpad_repeat_at.clear()
                    return
                for btn, key in DPAD_MAP:
                    held = bool(sci.buttons & btn)
                    next_at = self._dpad_repeat_at.get(btn)
                    if held and next_at is None:
                        # Rising edge: fire immediately, then wait.
                        self.chord.kb.pressEvent([key])
                        self.chord.kb.releaseEvent([key])
                        self._dpad_repeat_at[btn] = now + DPAD_HOLD_DELAY
                    elif held and now >= next_at:
                        self.chord.kb.pressEvent([key])
                        self.chord.kb.releaseEvent([key])
                        self._dpad_repeat_at[btn] = now + DPAD_REPEAT
                    elif not held and next_at is not None:
                        del self._dpad_repeat_at[btn]

            def _handle_trackpad_mouse(self, sc, sci, steam_now, now):
                """Right trackpad → mouse cursor with momentum/inertia.
                While the finger is in contact, the cursor moves by the
                deltas of a 1€-filtered (speed-adaptive low-pass,
                RPAD_EURO_*) absolute pad position: silky constant
                smoothing with a touch of lag through normal motion (the
                Steam Input feel), cutoff opening on a fast flick. A
                trailing dead-zone (+ resting recenter) zeroes out a
                still/tremoring finger. On lift, capture the velocity
                averaged over the last ~RPAD_VELOCITY_WINDOW seconds (so
                the lift's slowdown frames don't kill the carryover) and
                decay it. Right-pad click → left mouse button. Skipped
                while Steam is held."""
                raw_touch = bool(sci.buttons & SCButtons.RPADTOUCH)
                touched = raw_touch and not steam_now
                if touched:
                    x, y = sci.rpad_x, sci.rpad_y
                    # Tray "Trackpad Mouse Speed" scales the base sensitivity.
                    _sc = RPAD_SCALE * adusk_state.get_sc_trackpad_speed()
                    # Fresh contact: seed filter + anchor at the touch point so
                    # the first frame can't fling the cursor.
                    if not self._rpad_touched_was:
                        # Tap-to-click candidate  armed only on a TRUE fresh
                        # contact (the raw touch bit was clear last frame; a
                        # Steam-release re-seed mid-touch must not restart the
                        # tap clock), only when NOT catching a coasting fling
                        # (that touch stops the cursor, it doesn't click), and
                        # only while no trigger/pad click already holds a
                        # mouse button (lifting the pad finger mid-drag must
                        # not inject an extra click).
                        if (adusk_state.is_tap_to_click_enabled()
                                and not self._rpad_raw_touch_prev
                                and not self._fling_active
                                and not (self._lt_was_pressed
                                         or self._rt_was_pressed
                                         or self._rpad_click_was
                                         or self._lpad_click_prev)):
                            self._tap_start = (now, float(x), float(y))
                            self._tap_hist.clear()
                            self._tap_hist.append((now, float(x), float(y)))
                            self._tap_moved = 0.0
                        else:
                            self._tap_start = None
                        self._rpad_filt_x = float(x)
                        self._rpad_filt_y = float(y)
                        self._rpad_dfilt_x = 0.0
                        self._rpad_dfilt_y = 0.0
                        self._rpad_anchor_x = float(x)
                        self._rpad_anchor_y = float(y)
                        # Cancel any in-flight fling  a touch "catches" the cursor.
                        self._rpad_vx = 0.0
                        self._rpad_vy = 0.0
                        self._fling_active = False
                    # Live tracking: follow a POSITION-domain low-pass of the
                    # absolute pad position (no drift/coast when still).
                    if self._rpad_touched_was and self._rpad_last_t:
                        dt = now - self._rpad_last_t
                        if dt <= 0.0 or dt > 0.1:
                            dt = 1.0 / 60.0
                        # 1€ filter on the ABSOLUTE position: low-pass the raw
                        # velocity (DCUTOFF) to a stable speed estimate, then
                        # open the position cutoff with that speed  silky
                        # constant smoothing through normal motion (the
                        # Steam-like glide), rising on a fast flick so it
                        # never feels stuck.
                        ad = dt / (1.0 / (2.0 * math.pi * RPAD_EURO_DCUTOFF) + dt)
                        self._rpad_dfilt_x += ad * (
                            (x - self._rpad_prev_x) / dt - self._rpad_dfilt_x)
                        self._rpad_dfilt_y += ad * (
                            (y - self._rpad_prev_y) / dt - self._rpad_dfilt_y)
                        pad_speed = math.hypot(self._rpad_dfilt_x,
                                               self._rpad_dfilt_y)
                        fc = RPAD_EURO_MINCUTOFF + RPAD_EURO_BETA * pad_speed
                        a = dt / (1.0 / (2.0 * math.pi * fc) + dt)
                        self._rpad_filt_x += a * (x - self._rpad_filt_x)
                        self._rpad_filt_y += a * (y - self._rpad_filt_y)
                        # Soft dead-zone: the anchor TRAILS the filtered point by
                        # RPAD_DEADZONE units; the cursor moves only by the OVERSHOOT
                        # past the radius (dist - RPAD_DEADZONE), and the anchor
                        # re-trails to stay exactly RPAD_DEADZONE behind. A still/
                        # tremoring finger inside the radius → ZERO movement; when it
                        # crosses, motion ramps from zero (no lurch)  kills residual
                        # standstill blips and the chunky feel a bigger SNAP radius
                        # gave. Continuous motion tracks 1:1 (only the radius is slack).
                        ddx = self._rpad_filt_x - self._rpad_anchor_x
                        ddy = self._rpad_filt_y - self._rpad_anchor_y
                        dist = (ddx * ddx + ddy * ddy) ** 0.5
                        if dist > RPAD_DEADZONE:
                            over = dist - RPAD_DEADZONE
                            ux, uy = ddx / dist, ddy / dist
                            self._rpad_anchor_x = self._rpad_filt_x - RPAD_DEADZONE * ux
                            self._rpad_anchor_y = self._rpad_filt_y - RPAD_DEADZONE * uy
                            if now < self._mouse_freeze_until:
                                # Click-shake freeze: swallow the wobble (the
                                # anchor still trails, so nothing pent-up
                                # lurches out after)  unless the motion grows
                                # past the breakout, which only a REAL drag
                                # does; then release the freeze.
                                self._freeze_acc += over
                                if self._freeze_acc >= RPAD_FREEZE_BREAKOUT:
                                    self._mouse_freeze_until = 0.0
                            else:
                                self._rpad_acc_x += (over * ux) * _sc
                                self._rpad_acc_y += -(over * uy) * _sc   # pad +y up → screen -y
                                mvx = int(self._rpad_acc_x)
                                mvy = int(self._rpad_acc_y)
                                self._rpad_acc_x -= mvx
                                self._rpad_acc_y -= mvy
                                if mvx or mvy:
                                    self.chord.mouse.move(mvx, mvy)
                                    if self._tap_start is not None:
                                        # The cursor VISIBLY moved during this
                                        # touch  a quick corrective nudge is
                                        # pointing, not tapping. (Raw wander
                                        # is gated at lift; this catches short
                                        # strokes that slip under that gate
                                        # but still moved the pointer.)
                                        self._tap_moved += abs(mvx) + abs(mvy)
                                        if self._tap_moved > RPAD_TAP_MAX_PX:
                                            self._tap_start = None
                        elif pad_speed < RPAD_REST_SPEED:
                            # Resting inside the dead-zone: ease the anchor
                            # back onto the filtered point so the full radius
                            # of slack is restored all around the still finger
                            # (post-move it sat pinned ON the edge  zero
                            # slack along the travel direction, so the very
                            # next tremor blip leaked straight out as wiggle).
                            ra = dt / (RPAD_ANCHOR_RECENTER_TAU + dt)
                            self._rpad_anchor_x += ra * (
                                self._rpad_filt_x - self._rpad_anchor_x)
                            self._rpad_anchor_y += ra * (
                                self._rpad_filt_y - self._rpad_anchor_y)
                    # Tap-to-click candidate upkeep: a touch held past the
                    # tap window is a rest or a drag, never a tap; while the
                    # candidate lives, record the raw samples so the lift can
                    # judge total wander with the peel-off tail trimmed (a
                    # peel blip mustn't veto a real tap, so wander is NOT
                    # judged live here).
                    if self._tap_start is not None:
                        if now - self._tap_start[0] > RPAD_TAP_MAX_S:
                            self._tap_start = None
                            self._tap_hist.clear()
                        else:
                            self._tap_hist.append((now, float(x), float(y)))
                    # Keep a short rolling history so a touch's lift-
                    # velocity is the average over the last window, not
                    # the (often near-zero) last frame.
                    self._rpad_history.append((now, x, y))
                    cutoff = now - RPAD_VELOCITY_WINDOW
                    while self._rpad_history and self._rpad_history[0][0] < cutoff:
                        self._rpad_history.popleft()
                    if len(self._rpad_history) >= 2:
                        t0, x0, y0 = self._rpad_history[0]
                        t1, x1, y1 = self._rpad_history[-1]
                        span = max(1e-3, t1 - t0)
                        self._rpad_vx = (x1 - x0) * _sc / span
                        self._rpad_vy = -(y1 - y0) * _sc / span
                    self._rpad_prev_x = x
                    self._rpad_prev_y = y
                    self._rpad_last_t = now
                else:
                    # --- Lifted: launch a fling impulse, but only if the swipe was
                    # fast enough so slow/small moves stop dead (no perpetual inertia).
                    if self._rpad_touched_was:
                        # "Right Touchpad Tap to Click": a quick, still touch-
                        # and-lift = one left click. Judged BEFORE the fling
                        # so a fired tap's shake-freeze (below) vetoes the
                        # fling via the existing freeze check  a tap never
                        # also throws the cursor. Fires only on a REAL lift
                        # (raw touch bit clear, no Steam-hold masking); wander
                        # is the bounding-box spread of the CORE samples 
                        # EXCLUDING the first RPAD_TAP_START_SKIP (the pad
                        # resolving a fresh contact writes a garbage blip,
                        # which as the wander ORIGIN read still taps as
                        # swipes) and the final RPAD_LIFT_SKIP (the finger-
                        # peel twin)  so both contact transients are
                        # trimmed; an ultra-short tap with no core samples
                        # passes outright.
                        tap = self._tap_start
                        self._tap_start = None
                        if (tap is not None and not raw_touch and not steam_now
                                and adusk_state.is_tap_to_click_enabled()):
                            dur = (self._rpad_last_t or now) - tap[0]
                            if RPAD_TAP_MIN_S <= dur <= RPAD_TAP_MAX_S:
                                t_lo = tap[0] + RPAD_TAP_START_SKIP
                                t_hi = ((self._rpad_last_t or now)
                                        - RPAD_LIFT_SKIP)
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
                                      else (max_x - min_x) ** 2
                                      + (max_y - min_y) ** 2)
                                if d2 <= RPAD_TAP_MAX_DIST ** 2:
                                    # Press+release  the arming gates ensure
                                    # no trigger/pad click is holding "left",
                                    # so this can't drop a drag mid-flight.
                                    self.chord.mouse.button("left", True)
                                    self.chord.mouse.button("left", False)
                                    if adusk_state.is_rumble_enabled("sc"):
                                        sc.haptic_pad_click()
                                    # Freeze the cursor exactly like an R2/
                                    # pad-click edge: the SECOND tap of a
                                    # double-tap lands during this freeze, so
                                    # its touch wobble can't smear the pointer
                                    # between the two clicks  the double-
                                    # click stays inside the double-click slop
                                    # and folders OPEN instead of dragging.
                                    self._mouse_shake_guard()
                        self._tap_hist.clear()
                        # Lift velocity from the history EXCLUDING the final
                        # RPAD_LIFT_SKIP (the finger-peel blip), and only if
                        # what remains spans real, sustained motion  a
                        # standing-still/aggressive lift can't throw the cursor.
                        _sc = RPAD_SCALE * adusk_state.get_sc_trackpad_speed()
                        cutoff_t = (self._rpad_last_t or now) - RPAD_LIFT_SKIP
                        hist = [s for s in self._rpad_history if s[0] <= cutoff_t]
                        vx = vy = 0.0
                        if len(hist) >= 2 and now >= self._mouse_freeze_until:
                            t0, x0, y0 = hist[0]
                            t1, x1, y1 = hist[-1]
                            span = t1 - t0
                            if span >= RPAD_FLING_MIN_SPAN:
                                vx = (x1 - x0) * _sc / span
                                vy = -(y1 - y0) * _sc / span
                        lift_speed = math.hypot(vx, vy)
                        if lift_speed >= RPAD_FLING_TRIGGER:
                            # Fling runs at GAIN × the tracking lift velocity →
                            # throw decoupled from (and faster than) cursor speed.
                            fling_speed = lift_speed * RPAD_FLING_GAIN
                            self._fling_active = True
                            self._fling_t0 = now
                            self._fling_last_t = now
                            self._fling_v0 = fling_speed
                            self._fling_peak = fling_speed * RPAD_FLING_BOOST
                            self._fling_dirx = vx / lift_speed
                            self._fling_diry = vy / lift_speed
                        else:
                            self._fling_active = False
                        # Clear the touch history so the next touch starts fresh.
                        self._rpad_history.clear()
                    # Drive the fling: a velocity HUMP  ramp UP from the lift speed
                    # to the boosted peak over RPAD_FLING_RAMPUP_T (fast accel), then
                    # ramp DOWN from the peak to a stop over RPAD_FLING_RAMPDOWN_T
                    # (gentler settle). Thrown-and-caught feel, not an icy coast.
                    if self._fling_active:
                        elapsed = now - self._fling_t0
                        up_t = max(1e-3, RPAD_FLING_RAMPUP_T)
                        down_t = max(1e-3, RPAD_FLING_RAMPDOWN_T)
                        if elapsed >= up_t + down_t:
                            self._fling_active = False
                        else:
                            if elapsed < up_t:
                                v = self._fling_v0 + (
                                    self._fling_peak - self._fling_v0) * (
                                    elapsed / up_t)
                            else:
                                # Ease-OUT: taper smoothly to zero (no hard corner
                                # at the stop) so it glides to a standstill.
                                s = (elapsed - up_t) / down_t
                                v = self._fling_peak * (1.0 - s) * (1.0 - s)
                            dt = now - self._fling_last_t
                            dt = max(1e-3, min(dt, 1 / 30))
                            self._fling_last_t = now
                            self._rpad_acc_x += v * self._fling_dirx * dt
                            self._rpad_acc_y += v * self._fling_diry * dt
                            mvx = int(self._rpad_acc_x)
                            mvy = int(self._rpad_acc_y)
                            self._rpad_acc_x -= mvx
                            self._rpad_acc_y -= mvy
                            if mvx or mvy:
                                self.chord.mouse.move(mvx, mvy)
                self._rpad_touched_was = touched
                self._rpad_raw_touch_prev = raw_touch

                # Right-pad click → left mouse button.
                click_now = bool(sci.buttons & SCButtons.RPAD) and not steam_now
                if click_now != self._rpad_click_was:
                    self._mouse_shake_guard()
                    self.chord.mouse.button("left", click_now)
                self._rpad_click_was = click_now

            def _handle_arrow_rstick(self, sci, steam_now, now):
                """Right stick → discrete key presses (directional mode)."""
                active = not steam_now
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

            def _handle_mouse_lstick(self, sci, now):
                """Left stick moves the cursor (lstick_mouse mode)."""
                dt = now - self._lmouse_last_t if self._lmouse_last_t else 0.0
                self._lmouse_last_t = now
                x = sci.lstick_x
                y = sci.lstick_y
                mag = (x * x + y * y) ** 0.5
                if mag <= MOUSE_DEADZONE:
                    self._lmouse_acc_x = 0.0
                    self._lmouse_acc_y = 0.0
                    return
                if dt <= 0.0 or dt > 0.1:
                    dt = 1.0 / 60.0
                m = min(1.0, (mag - MOUSE_DEADZONE) / (32767.0 - MOUSE_DEADZONE))
                unit = MOUSE_MIN + (1.0 - MOUSE_MIN) * (m ** MOUSE_EXPONENT)
                scaled = unit / mag
                _spd = MOUSE_SPEED * adusk_state.get_sc_mouse_speed()
                self._lmouse_acc_x += (x * scaled) * _spd * dt
                self._lmouse_acc_y += -(y * scaled) * _spd * dt
                mvx = int(self._lmouse_acc_x)
                mvy = int(self._lmouse_acc_y)
                self._lmouse_acc_x -= mvx
                self._lmouse_acc_y -= mvy
                if mvx or mvy:
                    self.chord.mouse.move(mvx, mvy)

            def _handle_mouse_stick(self, sci, now):
                dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
                self._mouse_last_t = now
                x = sci.rstick_x
                y = sci.rstick_y
                mag = (x * x + y * y) ** 0.5
                if mag <= MOUSE_DEADZONE:
                    self._mouse_acc_x = 0.0
                    self._mouse_acc_y = 0.0
                    return
                if dt <= 0.0 or dt > 0.1:
                    dt = 1.0 / 60.0
                # RADIAL speed: apply the curve to the stick's DISTANCE from
                # center, then move along its unit direction, so a diagonal push
                # is as fast as a pure horizontal/vertical one. (Per-axis exponent
                # made diagonals much slower, very visible at high exponents.)
                m = min(1.0, (mag - MOUSE_DEADZONE) / (32767.0 - MOUSE_DEADZONE))
                unit = MOUSE_MIN + (1.0 - MOUSE_MIN) * (m ** MOUSE_EXPONENT)
                scaled = unit / mag
                # Screen Y grows downward; stick-up (positive y) → -dy.
                # "Pointer Speed" (tray Steam Controller menu) scales the base
                # px/sec, matching the OSK right-stick mouse.
                _spd = MOUSE_SPEED * adusk_state.get_sc_mouse_speed()
                self._mouse_acc_x += (x * scaled) * _spd * dt
                self._mouse_acc_y += -(y * scaled) * _spd * dt
                mvx = int(self._mouse_acc_x)
                mvy = int(self._mouse_acc_y)
                self._mouse_acc_x -= mvx
                self._mouse_acc_y -= mvy
                if mvx or mvy:
                    self.chord.mouse.move(mvx, mvy)

            def on_input(self, sc, sci):
                if sci.status != SCStatus.INPUT:
                    return
                # Live keybinds-picker controller preview: hand the RAW frame
                # over. One call + flag test per frame; a no-op while the
                # picker is hidden or on a non-SC tab (picker gates _active).
                sc_viewer.publish(sci)
                if (self.owner._stop_event.is_set()
                        or self.owner._steam_active.is_set()
                        or self.owner._kbd_open):
                    # Drop modifiers so they don't stick at the OS level.
                    self.chord.release_all_held()
                    sc.addExit()
                    return

                # Keybinds-picker controller navigation: while the picker is
                # visible AND foreground it consumes dpad/A/B/X/Y/Menu/LB/RB
                # to steer its own UI (published raw above). Mask those bits
                # out of everything below so highlighting a row doesn't also
                # type/click. Pads, sticks, triggers and Steam/QAM stay live.
                # A Steam/QAM-HELD frame is exempt: the picker navigates on
                # bare buttons only, so those chords belong to the normal
                # dispatch (see _GUIDE_BITS  this keeps Steam+X opening the
                # OSK with the GUI up, and stops a masked chord reading as a
                # clean Steam tap that fires "Toggle Config GUI").
                if sc_viewer.nav_claimed():
                    if sc_viewer.listen_claimed():
                        # Listen bind-capture: swallow EVERY press (see mask
                        # comment) so the captured button can't also fire.
                        # No guide exemption  the press being captured as a
                        # binding must not fire ANY action, chord or not.
                        sci = sci._replace(
                            buttons=sci.buttons & _PICKER_LISTEN_KEEP)
                    elif not (sci.buttons & _GUIDE_BITS):
                        # ...minus anything the picker asks us to spare: the
                        # tour's keyboard slide teaches the bare-X open, and a
                        # masked X opens nothing (sc_viewer.set_nav_keep).
                        _pmask = _PICKER_NAV_MASK & ~sc_viewer.nav_keep()
                        sci = sci._replace(
                            buttons=sci.buttons & ~_pmask)

                # Gamepad-mode toggle chord (Hotkeys "Gamepad Mode Toggle"):
                # fire once per press off the RAW frame BEFORE steam_now is read,
                # so Guide+button toggle chords that clear STEAM from sci also
                # suppress all Steam-held logic below. Latched on the App so
                # holding it can't ping-pong across the watcher rebuild.
                if self._gp_toggle_masks:
                    _t_held = False
                    _t_mask = 0
                    for _m in self._gp_toggle_masks:
                        if (sci.buttons & _m) == _m:
                            _t_held = True
                            _t_mask |= _m
                    self.owner.handle_gamepad_toggle(_t_held)
                    if _t_mask:
                        sci = sci._replace(buttons=sci.buttons & ~_t_mask)
                # Built-in "hold ≡ (Start/Menu) to switch Desktop <-> Gamepad"
                # gesture. Same contract as the toggle chord above and evaluated
                # right after it, so a Start-component chord wins the frame (its
                # bits are already gone by the time the gesture sees them, and
                # the gesture only fires on a Start held ALONE anyway). Once it
                # fires, the App returns the Start bit for the rest of the press
                # so it's swallowed here instead of also firing Start's own
                # binding.
                _h_mask = self.owner.handle_mode_hold(sci.buttons)
                if _h_mask:
                    sci = sci._replace(buttons=sci.buttons & ~_h_mask)
                # Gyro-to-mouse toggle chord (SC Options "Gyro To Mouse"
                # hotkey)  same contract: raw-frame evaluation, App-side
                # latch, held bits masked out.
                if self._gyro_toggle_masks:
                    _g_held = False
                    _g_mask = 0
                    for _m in self._gyro_toggle_masks:
                        if (sci.buttons & _m) == _m:
                            _g_held = True
                            _g_mask |= _m
                    self.owner.handle_gyro_toggle(_g_held, "sc")
                    if _g_mask:
                        sci = sci._replace(buttons=sci.buttons & ~_g_mask)
                # Gyro-to-mouse drive: while toggled on, the SC's IMU angular
                # velocity moves the cursor. The IMU stream follows the toggle
                # (on the Linux Triton the runtime enable rides the hidapi
                # feature-report caveat  see steamcontroller.set_imu).
                _g_act = adusk_state.is_gyro_mouse_active("sc")
                if _g_act != self._gyro_imu_on:
                    self._gyro_imu_on = _g_act
                    try:
                        sc.set_imu(_g_act)
                    except Exception:
                        pass
                    self._gyro_mouse.reset()
                if _g_act:
                    self._gyro_mouse.feed(
                        sci.gyaw * GYRO_DEG_PER_SEC,
                        sci.gpitch * GYRO_DEG_PER_SEC,
                        time.monotonic(), "sc")
                # Steam and "..." (QAM) are distinct bits. The SC on Linux is
                # always desktop (no virtual-pad path), so only the real Steam
                # button drives the Steam chords/binds below  "..." is left free
                # as a plain button in the Hotkeys chord editor (it flows through
                # build_chords / _handle_chords, which are gated on `not steam_now`).
                # Read AFTER the toggle block so Guide+button toggle chords that
                # clear STEAM from sci also suppress the Steam-held paths.
                steam_now = bool(sci.buttons & SCButtons.STEAM)
                # Two-button chords + per-control rebinds (desktop takeover). Fire
                # them, then mask their buttons out of `sci` so the single-button
                # handlers below  and the x_now/y_now/... reads next  don't ALSO
                # fire (e.g. A+B → chord, not Enter+Esc; a rebound X does its
                # action, not open the OSK). No-op unless something is active.
                if self._chords_runtime:
                    _sup = self._handle_chords(sc, sci, steam_now)
                    if _sup:
                        sci = sci._replace(buttons=sci.buttons & ~_sup)
                if self._sc_overrides:
                    _sup = self._handle_overrides(sc, sci.buttons, steam_now)
                    if _sup:
                        sci = sci._replace(buttons=sci.buttons & ~_sup)
                x_now = bool(sci.buttons & SCButtons.X)
                y_now = bool(sci.buttons & SCButtons.Y)
                b_now = bool(sci.buttons & SCButtons.B)
                view_now = bool(sci.buttons & SCButtons.VIEW)
                now = time.monotonic()

                # L3 (left stick click) ALONE → middle click at the cursor
                # (Steam+L3 is Play/Pause in the media chords). For web browsing:
                # middle-click a link to open it in a new background tab, or a tab
                # to close it. Edge tracked every frame so releasing Steam while
                # still holding L3 can't spuriously fire a click.
                l3_mid_now = bool(sci.buttons & SCButtons.L3)
                if not steam_now and l3_mid_now and not self._l3_mid_prev:
                    self.chord.mouse.button("middle", True)
                    self.chord.mouse.button("middle", False)
                self._l3_mid_prev = l3_mid_now

                # Steam release → drop Alt-Tab.
                if not steam_now:
                    self.chord.release_alt()

                # X alone → open OSK; Steam+X also opens it unless the user
                # has rebound Steam+X in the Chords tab (guide bind takes over).
                x_opens_now = x_now and not (
                    steam_now and int(SCButtons.X) in self._guide_bind_bits)
                if x_opens_now and not self._x_open_was_pressed:
                    self.owner._pending_open_controller = "sc"
                    self.owner._open_kbd_event.set()
                    self.chord.release_all_held()
                    sc.addExit()
                self._x_open_was_pressed = x_now

                # Steam+VIEW → Alt+Tab when VIEW has no guide bind.
                if int(SCButtons.VIEW) not in self._guide_bind_bits:
                    if steam_now and view_now and not self.chord.view_was_pressed:
                        if not self.chord.alt_held:
                            self.chord.kb.pressEvent([sui.Keys.KEY_LEFTALT])
                            self.chord.alt_held = True
                        self.chord.kb.pressEvent([sui.Keys.KEY_TAB])
                        self.chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                self.chord.view_was_pressed = view_now

                # Steam TAP (short press, no chord) → bound action (default
                # "Toggle Config GUI"). Called every frame so the falling-edge
                # detector sees the release; the held Steam chords are untouched.
                if self._guide_taps:
                    self._handle_guide_taps(sc, sci, now)

                # Steam HELD + button → guide-hold binds (Chords tab).
                if steam_now and self._guide_binds:
                    self._handle_guide_binds(sc, sci)

                # Steam HELD → Guide chord (Hotkeys key combo / launch). Called
                # every frame (not gated on steam_now) so a Guide-alone chord's
                # per-hold edge resets when Steam is released.
                if self._guide_chords:
                    self._handle_guide_chords(sc, sci, steam_now)

                # Steam HELD + right-stick zone → directional guide bind.
                if steam_now and self._guide_rstick_zones:
                    self._handle_guide_rstick(sc, sci)

                # Steam HELD + left-stick zone → directional guide bind.
                if steam_now and self._guide_lstick_zones:
                    self._handle_guide_lstick(sc, sci)

                # Steam + L-stick / L3 → media transport.
                self._handle_media_chords(sc, sci, steam_now, now)
                # Left stick → cursor (mouse mode) or directional action.
                if self._lstick_mouse:
                    self._handle_mouse_lstick(sci, now)
                else:
                    self._handle_arrow_stick(sci, steam_now, now)
                # Right stick → mouse cursor or directional keys.
                # Suppress cursor during Steam hold when rstick guide zones are bound.
                if self._rstick_mouse:
                    if not (steam_now and self._guide_rstick_zones):
                        self._handle_mouse_stick(sci, now)
                elif self._rstick_actions:
                    self._handle_arrow_rstick(sci, steam_now, now)
                # Left trackpad → scroll wheel + middle-click on pad press.
                # While Video Timeline Scrubbing is on AND a video is focused,
                # the left pad is a circular timeline dial instead; kill any
                # scroll coast + touch state so scroll can't run underneath.
                _scrub_mode = adusk_state.get_video_scrub_mode()
                # Text Wheel Selection engages while a left-click control HOLDS
                # the left mouse button (R2 actuated per the Mouse Trigger
                # Actuation setting, or a right-pad click  gated on the
                # 1-frame-stale CLICK flags, NOT the raw SCButtons.RT bit,
                # which only reports a FULL pull and so never fired on a
                # 35%-actuation hold) AND a finger is on the left pad. The drag
                # stays live: the dial nudges the cursor and the app's own
                # drag-selection extends character-snapped under it (see
                # _handle_pad_text_wheel for why Shift+Arrow was abandoned).
                _lpad_now = bool(sci.buttons & SCButtons.LPADTOUCH)
                if (adusk_state.is_text_wheel_selection_enabled()
                        and not steam_now and _lpad_now
                        and (self._rt_was_pressed or self._rpad_click_was)):
                    self._scroll_fling_v = 0.0
                    self._lpad_prev = None
                    self._wheel_angle = None
                    self._wheel_acc = 0.0
                    self._scrub_angle = None
                    self._scrub_acc = 0.0
                    self._scrub_stepped = False
                    self._handle_pad_text_wheel(sc, sci, now)
                elif (_scrub_mode != "off" and not steam_now
                        and self._video_focused(now)):
                    self._scroll_fling_v = 0.0
                    self._lpad_prev = None
                    self._wheel_angle = None
                    self._wheel_acc = 0.0
                    self._textwheel_angle = None
                    self._textwheel_acc = 0.0
                    self._handle_pad_scrub(sc, sci, now, _scrub_mode)
                else:
                    self._textwheel_angle = None
                    self._textwheel_acc = 0.0
                    self._scrub_angle = None
                    self._scrub_acc = 0.0
                    # Focus left mid-scrub: drop the pending resume (blind-
                    # firing "K" at whatever is focused now is worse than
                    # leaving the video paused).
                    self._scrub_stepped = False
                    self._handle_pad_scroll(sc, sci, steam_now, now)
                self._handle_lpad_tap(sc, sci, steam_now, now)
                self._handle_lpad_click(sc, sci, steam_now)
                # Right trackpad → mouse cursor + left-click on pad press.
                self._handle_trackpad_mouse(sc, sci, steam_now, now)

                # Triggers → mouse buttons. Edge-triggered: a full pull
                # sets the button down, a release lifts it (so dragging
                # works). Skipped during Steam-hold so chord uses are free
                # to repurpose triggers later. Actuation honours its OWN
                # "Mouse Trigger Actuation" setting (separate from
                # "Keyboard Trigger Actuation", which only governs the
                # OSK's Shift/Enter): the firmware full-pull digital bit
                # ALWAYS counts, and  unless the setting is "High"
                # (full-pull only)  an analog pull past the threshold
                # also counts.
                _act_thr = adusk_state.get_sc_mouse_trigger_threshold()
                lt_now = self._trigger_click_now(
                    self._lt_was_pressed, bool(sci.buttons & SCButtons.LT),
                    sci.ltrig, _act_thr) and not steam_now
                if lt_now != self._lt_was_pressed:
                    self._mouse_shake_guard()
                    self.chord.mouse.button("right", lt_now)
                    # Haptic tick on the press (rising edge) so a click feels
                    # like a button, matching the Windows desktop L2/R2 feedback.
                    # Gated by the global vibration switch.
                    if lt_now and adusk_state.is_rumble_enabled("sc"):
                        sc.haptic_click()
                self._lt_was_pressed = lt_now

                rt_now = self._trigger_click_now(
                    self._rt_was_pressed, bool(sci.buttons & SCButtons.RT),
                    sci.rtrig, _act_thr) and not steam_now
                if rt_now != self._rt_was_pressed:
                    self._mouse_shake_guard()
                    self.chord.mouse.button("left", rt_now)
                    if rt_now and adusk_state.is_rumble_enabled("sc"):
                        sc.haptic_click()
                self._rt_was_pressed = rt_now

                # Steam+Y → power off controller. Skipped when Y has a guide bind.
                if int(SCButtons.Y) not in self._guide_bind_bits:
                    if steam_now and y_now:
                        if not self._powered_off:
                            self._powered_off = True
                            try:
                                sc.turn_off()
                            except Exception as e:
                                print(f"Steam+Y turn_off failed: {e}")
                    else:
                        self._powered_off = False

                # Steam+B → kill focused window's process. Skipped when B has
                # a guide bind.
                if int(SCButtons.B) not in self._guide_bind_bits:
                    if steam_now and b_now:
                        if not self._force_kill_done:
                            self._force_kill_done = True
                            result = _kill_focused_window()
                            print(f"Steam+B kill focused: {result}")
                    else:
                        self._force_kill_done = False

                # Bare-button bindings (skipped while Steam is held 
                # the Steam-chord variants above already consumed those
                # frames). Linux disables firmware lizard, so without
                # these the controller emits nothing for A/B/d-pad. Match
                # Steam's default desktop config.

                # A alone → Enter.
                a_now = bool(sci.buttons & SCButtons.A)
                a_alone = a_now and not steam_now
                if a_alone and not self._a_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_ENTER])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_ENTER])
                self._a_was_pressed = a_alone

                # B alone → Escape. No OSK-open guard needed: while the OSK is
                # open the desktop watcher doesn't drive the controller, so this
                # injected Escape can't reach the Esc-close path.
                b_alone = b_now and not steam_now
                if b_alone and not self._b_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_ESC])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_ESC])
                self._b_was_pressed = b_alone

                # D-pad → arrow keys (tap + hold-repeat).
                self._handle_dpad(sci, steam_now, now)

                # Y alone → Space.
                y_alone = y_now and not steam_now
                if y_alone and not self._y_alone_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_SPACE])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_SPACE])
                self._y_alone_was_pressed = y_alone

                # R4 → Page Up, R5 → Page Down.
                r4_now = bool(sci.buttons & SCButtons.RGRIP1) and not steam_now
                if r4_now and not self._r4_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_PAGEUP])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_PAGEUP])
                self._r4_was_pressed = r4_now

                r5_now = bool(sci.buttons & SCButtons.RGRIP2) and not steam_now
                if r5_now and not self._r5_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_PAGEDOWN])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_PAGEDOWN])
                self._r5_was_pressed = r5_now

                # L1 / R1 (bumpers) → previous / next browser tab
                # (Ctrl+Shift+Tab / Ctrl+Tab), matching the console convention.
                lb_now = bool(sci.buttons & SCButtons.LB) and not steam_now
                if lb_now and not self._lb_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                    self.chord.kb.pressEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
                self._lb_was_pressed = lb_now

                rb_now = bool(sci.buttons & SCButtons.RB) and not steam_now
                if rb_now and not self._rb_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                    self.chord.kb.pressEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
                self._rb_was_pressed = rb_now

                # L4 → hold Shift, L5 → hold Super. The release branch
                # also runs while Steam is held so transient chords don't
                # strand the modifier.
                l4_hold = (bool(sci.buttons & SCButtons.LGRIP1)
                           and not steam_now)
                if l4_hold and not self.chord.shift_held:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                    self.chord.shift_held = True
                elif not l4_hold and self.chord.shift_held:
                    self.chord.release_shift()

                l5_hold = (bool(sci.buttons & SCButtons.LGRIP2)
                           and not steam_now)
                if l5_hold and not self.chord.win_held:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTMETA])
                    self.chord.win_held = True
                elif not l5_hold and self.chord.win_held:
                    self.chord.release_win()

        while not self._stop_event.is_set():
            # Release the controller while Steam owns it or the OSK is up.
            if self._steam_active.is_set() or self._kbd_open:
                if self._stop_event.wait(1.0):
                    return
                continue
            watcher = _Watcher(self)
            sc = SteamController(callback=watcher.on_input, passive=True,
                                  exclusive=self.settings["block_sc_hid"])
            self._current_sc = sc
            try:
                sc.run()
            except Exception as e:
                print(f"chord watcher error: {e}")
            finally:
                self._current_sc = None
            # Whatever caused sc.run() to exit, make sure no modifier is
            # stuck pressed (the watcher should have done this on the
            # last frame, but belt-and-suspenders against crashes).
            chord.release_all_held()
            if self._stop_event.is_set():
                return
            if self._stop_event.wait(1.0):
                return

    def hotkey_thread(self):
        """Listen for Win+Ctrl+O to toggle the OSK. Opens when closed,
        closes when open. X11/XWayland only  pynput's GlobalHotKeys
        silently no-ops on a pure Wayland session, but our SDL window
        runs through XWayland anyway.

        Windows' side of this binding has to swallow the keystroke with a raw
        low-level hook, because Win+Ctrl+O is ALSO Windows' own built-in OSK
        shortcut there (see windows/tray.py's _start_osk_hotkey_hook). No
        desktop environment reserves that combo by default on Linux, so a
        plain listener is fine here  nothing else on the system opens on it."""
        try:
            from pynput import keyboard as pkb
        except Exception as e:
            print(f"pynput unavailable, hotkey listener disabled: {e}")
            return

        def _on_toggle():
            if self._stop_event.is_set():
                return
            if self._kbd_open:
                try:
                    adusk_state.close()
                except Exception as e:
                    print(f"Win+Ctrl+O close failed: {e}")
            else:
                self._open_kbd_event.set()

        try:
            listener = pkb.GlobalHotKeys({"<cmd>+<ctrl>+o": _on_toggle})
            listener.daemon = True
            listener.start()
        except Exception as e:
            print(f"hotkey listener failed to start: {e}")
            return

        # Global Escape closes the on-screen keyboard when it's open.
        def _on_esc_press(key):
            if key == pkb.Key.esc and self._kbd_open:
                if adusk_state.take_esc_close_suppressed():
                    return
                if self._stop_event.is_set():
                    return
                try:
                    adusk_state.close()
                except Exception as e:
                    print(f"Esc close failed: {e}")
        esc_listener = None
        try:
            esc_listener = pkb.Listener(on_press=_on_esc_press)
            esc_listener.daemon = True
            esc_listener.start()
        except Exception as e:
            print(f"esc listener failed to start: {e}")

        self._stop_event.wait()
        try:
            listener.stop()
        except Exception:
            pass
        if esc_listener is not None:
            try:
                esc_listener.stop()
            except Exception:
                pass

    def _set_sdl_hi_res(self, want):
        """Hold/drop a process high-responsiveness request (adusk_power) from the
        SDL thread while a live SDL pad may be driving the desktop. A no-op on
        Linux (no EcoQoS), kept to mirror the Windows tree. Reference-counted, so
        it composes with the OSK loop's own request. Call only from this thread."""
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
        """Poll SDL-recognized pads (Switch Pro / Xbox / DualSense / ...) so a
        non-Steam controller can (a) OPEN the OSK and (b) act as a desktop
        mouse/keyboard  the synthesized equivalent of the Steam Controller's
        firmware lizard mode (which Linux disables). Linux has no ViGEm, so
        unlike Windows there's no game-feed path. The Steam Controller itself is
        excluded by Sdl3GamepadSource (name match), so the two never fight.

        Bare Y on the Switch Pro  positionally SCButtons.X, like the SC's bare-X
        desktop open  opens the keyboard. While the OSK is open this thread
        CEDES: `adusk.main` polls this same source on its own SDL event-pump
        thread and publishes the frames, because SDL only refreshes gamepad state
        on the event-pump thread  polling here too once that loop runs reads
        stale/blind frames (froze the pad to no input, and a frozen deflected
        stick drifted the cursor). Defensive throughout  errors must never take
        down the tray."""
        src = self._sdl_source
        if src is None:
            return
        x_prev = False
        # force_kill = Home+B → kill the focused window's process (the SDL-pad
        # equivalent of the Steam Controller's Steam+B force-shutdown chord).
        desktop = _SdlDesktopController(
            force_kill=_kill_focused_window,
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
        # Controller kind whose Desktop/Chords/Hotkeys binds are currently
        # loaded into `desktop`  swapped live to follow the active pad.
        desktop_binds_kind = "switch"
        self._sdl_desktop_kind = lambda: desktop_binds_kind
        was_kbd_open = False
        osk_close_time = 0.0       # monotonic time of last OSK close (debounce)
        OSK_REOPEN_COOLDOWN = 0.4  # ignore Y for this long after the OSK closes
        steam_kill_prev = False    # Home+face edge while Steam-ceded (force-kill)
        # (the "Gyro To Mouse" chord needs no local latch  gyro_action_hold
        #  owns the once-per-press edge for every source of that action)
        # Gyro-to-mouse integrator for the SDL pads (SDL sensor API  rad/s).
        _gyro_mouse = _GyroMouse(desktop._mouse.move)
        _RAD_TO_DEG = 180.0 / math.pi
        idle_polls = 0             # consecutive idle polls (drives the backoff)
        last_pad_active = 0.0      # monotonic time the pad last had real input
        _PAD_IDLE_GRACE = 0.6      # hold the fast poll this long after last input
        while not self._stop_event.is_set():
            # Paused for Steam (disable_while_steam_running + Steam up): let Steam
            # own the pad  don't inject desktop kb/mouse  matching how the
            # Steam Controller path pauses (its _Watcher cedes on _steam_active).
            if self._steam_active.is_set():
                self._set_sdl_hi_res(False)  # cede; drop any responsiveness hold
                desktop.reset()
                x_prev = False
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
                if kill_now and not steam_kill_prev:
                    try:
                        print(f"[forcekill] (steam-ceded) Home+face -> "
                              f"{_kill_focused_window()}")
                    except Exception as e:
                        print(f"[forcekill] failed: {e!r}")
                steam_kill_prev = kill_now
                # 10 Hz is plenty here: the only job is edge-detecting a
                # deliberately HELD Home+B chord, and this branch runs for
                # entire gaming sessions  don't wake 20x/sec for it.
                self._stop_event.wait(0.1)
                continue
            steam_kill_prev = False
            # Cede all SDL access while the OSK is open  adusk owns it then.
            if self._kbd_open:
                was_kbd_open = True
                desktop.reset()
                x_prev = True  # treat Y as held so its release doesn't re-open
                # Nothing to do until the OSK closes  a slow tick is plenty
                # (the 0.4 s reopen cooldown dwarfs the ≤0.1 s resume lag).
                self._stop_event.wait(0.1)
                continue
            # The OSK just closed  start a cooldown so buffered Y presses during
            # close don't immediately re-open it, and force a clean rising edge.
            if was_kbd_open:
                was_kbd_open = False
                osk_close_time = time.monotonic()
                x_prev = True
            try:
                sci = src.poll()
            except Exception as e:
                print(f"sdl gamepad poll error: {e!r}")
                sci = None
            # A pad just dropped off Bluetooth? Say so once per session  a
            # Nintendo pad doing it every ~20 minutes is firmware, not us, and
            # the user deserves to know that (and the two real fixes) instead
            # of blaming the app.
            try:
                for _jid, _uid, _dkind, _guarded in src.take_drop_events():
                    if _guarded and not self._nintendo_drop_notified:
                        self._nintendo_drop_notified = True
                        self._notify(
                            "Switch controller disconnected",
                            "Nintendo's Bluetooth firmware drops the link every "
                            "~20 min on any non-Switch host; it should reconnect "
                            "on its own. To avoid it: use USB-C, or run "
                            "`bluetoothctl system-alias Nintendo`  the "
                            "controller only behaves for a host with that name.")
            except Exception:
                pass
            # New controller kind? Permanently unlock its picker tab + Options
            # category (cheap: set lookup per connected pad).
            for _kind in src._pad_kinds.values():
                if _kind not in self._seen_kind_cache:
                    self._note_seen_controller(_kind)
            # Keybinds picker controller navigation: EVERY controller steers
            # the picker, not just the Steam Controller. Publish the merged
            # frame for the picker's nav pump, then  while the picker is
            # visible + foreground  mask the navigation buttons out of the
            # desktop dispatch below so highlighting a row doesn't also
            # type/click.
            if sci is not None:
                sc_viewer.publish_nav(sci, src.active_kind())
                if sc_viewer.nav_claimed():
                    if sc_viewer.listen_claimed():
                        # Listen bind-capture: swallow EVERY press so the
                        # captured button can't also fire desktop actions.
                        sci = sci._replace(
                            buttons=sci.buttons & _PICKER_LISTEN_KEEP)
                    elif not (sci.buttons & _GUIDE_BITS):
                        # Home/Guide-held frames are exempt (see _GUIDE_BITS):
                        # a Home+button chord is normal dispatch, and masking
                        # it made the Home TAP detector see a clean tap and
                        # fire "Toggle Config GUI". So is anything the picker
                        # asks us to spare  the tour's keyboard slide teaching
                        # the bare-X open (sc_viewer.set_nav_keep).
                        _pmask = _PICKER_NAV_MASK & ~sc_viewer.nav_keep()
                        sci = sci._replace(
                            buttons=sci.buttons & ~_pmask)
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
            # Built-in "hold + / Options / ≡ to switch Desktop <-> Gamepad"
            # gesture  the SDL twin of the SC watcher's, on its own detector
            # (the two paths see different frames). Held bits are stripped so
            # the press that switched modes doesn't also fire its desktop bind
            # on the way out.
            if sci is not None:
                _h_mask = self.handle_mode_hold(sci.buttons, sdl=True)
                if _h_mask:
                    sci = sci._replace(buttons=sci.buttons & ~_h_mask)
            # "Gyro To Mouse" hotkey chords from the active kind  MODE-aware
            # (Enable/Suppress/Toggle); "toggle" uses the once-per-press
            # latch, mirroring the Windows SDL thread.
            if desktop.gyro_toggle_masks and sci is not None:
                gyro_action_hold(_ak, "chord",
                                 any((sci.buttons & m) == m
                                     for m in desktop.gyro_toggle_masks))
            # Gyro-to-mouse drive: stream the gyro on exactly the pads whose
            # kind has it toggled on (per-pad SDL sensor enable  costs
            # nothing while off) and turn their angular velocity into cursor
            # motion.
            _gyro_moved = False
            _gyro_kinds = {k for k in set(src._pad_kinds.values())
                           if adusk_state.is_gyro_mouse_active(k)
                           and pads.has_gyro(k)}
            src.set_gyro_kinds(_gyro_kinds)
            if _gyro_kinds:
                _gnow = time.monotonic()
                for _jid, _gk, _gx, _gy, _gz in src.read_gyro():
                    # SDL gyro is rad/s; X = pitch, Y = yaw. (With several
                    # gyro pads live the shared integrator's dt gate lets one
                    # pad drive per tick  they don't sum.)
                    if _gyro_mouse.feed(_gy * _RAD_TO_DEG, _gx * _RAD_TO_DEG,
                                        _gnow,
                                        adusk_state.get_mouse_speed_for(_gk)):
                        _gyro_moved = True
            # A guide-bound (or Chords-tab) "show_keyboard" action requested an
            # OSK open  honor it with the same gating as the button path.
            if desktop.open_request:
                desktop.open_request = False
                if (not self._kbd_open
                        and (time.monotonic() - osk_close_time)
                        > OSK_REOPEN_COOLDOWN):
                    self._pending_open_controller = src.active_kind()
                    self._open_kbd_event.set()
            # A connected pad only needs the fast poll while it's actually IN USE
            # (any input); an untouched pad must not pin the loop at 125 Hz. The
            # grace window snaps back instantly on the first input. (hi-res is a
            # no-op on Linux; kept for parity with the Windows tree.)
            now_m = time.monotonic()
            if _gyro_moved or (
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
                # the Switch Pro that's physical Y), matching the Steam
                # Controller's bare-X desktop open. Rising edge so one press =
                # one open; gated by the close cooldown.
                x = bool(sci.buttons & desktop.open_bits)
                if (x and not x_prev and not self._kbd_open
                        and (time.monotonic() - osk_close_time) > OSK_REOPEN_COOLDOWN):
                    self._pending_open_controller = src.active_kind()
                    self._open_kbd_event.set()
                x_prev = x
                # Desktop mouse/keyboard from the SDL pad (right stick = cursor,
                # left stick / D-pad = arrows, ZR/ZL = clicks, Y = Space, bumpers
                # = PageUp/Dn). Physical Y (= SCButtons.X) is the OSK opener and
                # is excluded from the desktop key taps inside update().
                try:
                    desktop.update(sci, time.monotonic())
                except Exception as e:
                    print(f"sdl desktop update failed: {e!r}")
            else:
                x_prev = False
                desktop.reset()
            # Pace the poll: a connected pad gets the full ~125 Hz; with NO pad
            # connected (controller off  the common idle case) ramp down to
            # ~20 Hz so the thread isn't woken 125x/sec for nothing. Hotplug is
            # still caught within one (≤50 ms) tick, snapping back to 125 Hz.
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

    def _shutdown_sdl(self):
        """Tear down the persistent SDL gamepad subsystem. Called from main()
        on the main thread after the OSK loop has fully exited, so it never
        races adusk.main()'s own SDL teardown."""
        src = self._sdl_source
        self._sdl_source = None
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        try:
            S.SDL_Quit()
        except Exception:
            pass


def _open_osk_once(app):
    """Reset per-session state and run the OSK on the calling thread (SDL
    constraint: video init + event pump must be the main thread)."""
    app._kbd_open = True
    # Publish it: the keyboard is always-on-top, so an overlay that covers the
    # manager (the tutorial) has to know when it is under it.
    sc_viewer.set_osk_open(True)
    # This open is a live Options-tab preview iff the preview flag is set when we
    # get here (the user is holding a Size/Transparency slider)  show it
    # instantly with no open animation. A typing-open (_osk_typing, Menu/≡
    # "Enter Value") is NOT a preview  it must process real input, so it opens
    # like any other hotkey open (animated, preview=False).
    preview = app._osk_preview
    typing_open = app._osk_typing
    if app._icon is not None:
        try:
            app._icon.update_menu()
        except Exception:
            pass
    # Start the OSK on the glyphs of the controller that opened it: the Steam
    # Controller (Steam+X) tags "sc", an SDL pad (Switch Guide+X) tags "sdl";
    # a non-controller open (tray menu / Win+Ctrl+O) leaves it on the last-used
    # controller. set_active_controller persists the choice too.
    opener = app._pending_open_controller
    app._pending_open_controller = None
    if opener is not None:
        adusk_state.set_active_controller(opener)
    try:
        adusk_state.reset_session()
        # reset_session() just cleared the close flag  if the preview slider
        # was released, or the value entry was committed/cancelled, during
        # this handoff (a quick press/release), re-issue the close so the OSK
        # doesn't open and get stuck.
        if preview and not app._osk_preview:
            adusk_state.close()
        elif typing_open and not app._osk_typing:
            adusk_state.close()
        adusk_app.main(preview=preview)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"adusk crashed: {e!r}")
    finally:
        # adusk.main() releases its own responsiveness hold at teardown; release
        # here too in case it raised first (no-op on Linux; mirrors Windows).
        adusk_power.unboost_current_thread()
        adusk_power.release()
        app._kbd_open = False
        sc_viewer.set_osk_open(False)
        # Clear the preview/typing flags on EVERY close so they can't outlive
        # the window and make the next loop iteration immediately reopen.
        app._osk_preview = False
        app._osk_typing = False
        # "Remember Per App": persist whatever the session recorded (the spot
        # the Move key landed on, plus a size/skin the user switched to while
        # this app was foreground). Drained here rather than per change, so one
        # close costs one write.
        app._persist_per_app_osk()
        if app._icon is not None:
            try:
                app._icon.update_menu()
            except Exception:
                pass


def _build_menu(app):
    # The tray menu is ACTIONS ONLY  every setting that used to live here
    # (Startup / per-controller submenus / Keyboard Settings / Advanced) moved
    # into the Keybinds manager's Options tab. The App's toggle_*/select_*/
    # is_*_checked handlers are kept (they're the documented live paths and
    # some are shared by the Options apply flow)  only the menu entries are
    # gone.
    return pystray.Menu(
        pystray.MenuItem(
            app.battery_menu_label,
            None,
            enabled=False,
            visible=app.is_battery_known,
        ),
        pystray.MenuItem(
            app._kbd_menu_label,
            app.open_kbd,
        ),
        pystray.MenuItem("Keybinds", app.open_keybinds, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", app.exit_app),
    )


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

    Runs on its own daemon thread  the wait must not block the caller, which
    still has to reach the main OSK loop below (and therefore is the thread
    that would service an open_kbd event).

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
    parser = argparse.ArgumentParser(
        description="SteamlessInput tray launcher (Linux)."
    )
    parser.add_argument(
        "--no-tray", action="store_true",
        help="Run without the tray icon (terminal-only, like adusk_linux.py).",
    )
    args = parser.parse_args()

    app = App()

    # A second launch of the binary signals SIGUSR1 (see
    # _ensure_single_instance) to ask this instance to open the Keybinds
    # manager  re-launching the app is the natural way users try to get the
    # GUI back, so answer it instead of silently doing nothing. Show-only
    # (never toggle-hides an open window); falls back to a full open when the
    # picker isn't built yet, which also queues the show if a warm build is
    # still in flight. The handler only sets a plain flag on the picker/tray
    # thread machinery via open_keybinds' own thread-safe entry points.
    def _on_open_gui_signal(_signum, _frame):
        try:
            import keybinds_picker
            if not keybinds_picker.show_picker():
                app.open_keybinds()
        except Exception as e:
            print(f"open-gui signal failed: {e!r}")

    try:
        signal.signal(signal.SIGUSR1, _on_open_gui_signal)
    except Exception:
        pass

    if args.no_tray:
        # Headless mode: behave like adusk_linux --controller. Useful when
        # the user is debugging on a session with no compatible tray.
        threading.Thread(target=app.chord_watcher_thread, daemon=True).start()
        threading.Thread(target=app.hotkey_thread, daemon=True).start()
        threading.Thread(target=app.steam_watch_thread, daemon=True).start()
        threading.Thread(target=app.battery_thread, daemon=True).start()
        threading.Thread(target=app.sdl_gamepad_thread, daemon=True).start()
        # Big Picture controller-connect automation (blocks while disabled).
        app._bp_engine.start()
        print(f"{TRAY_TITLE} (no-tray) running. Steam+X or Win+Ctrl+O to open.")
        try:
            while not app._stop_event.is_set():
                if app._open_kbd_event.wait(timeout=1.0):
                    app._open_kbd_event.clear()
                    _open_osk_once(app)
        except KeyboardInterrupt:
            pass
        app._stop_event.set()
        app._shutdown_sdl()
        return

    # Tray mode needs pystray (GTK/AppIndicator); import it here, not at module
    # load, so the --no-tray path above still works without the GTK stack.
    global pystray
    pystray = _import_pystray()

    image = _load_icon_image()
    menu = _build_menu(app)
    icon = pystray.Icon("SteamlessInput", image, TRAY_TITLE, menu)
    app._icon = icon

    # pystray's AppIndicator backend run_detached() doesn't actually start
    # a GLib main loop  every @mainloop-decorated call (including the
    # set_status(ACTIVE) that registers the SNI item with KDE) gets queued
    # to a loop that never runs, so the tray entry never appears. We have
    # to use icon.run() (blocking, runs GLib.MainLoop.run()) instead, on
    # its own thread so the main thread stays free for SDL.
    theme_dir, icon_name = _install_tray_icon_theme()

    def setup(ic):
        ic.visible = True
        # KDE Plasma 6 won't render an SNI item whose IconName is an
        # absolute file path (pystray's default). Point AppIndicator at our
        # private theme dir holding the project icon and resolve it by name.
        # Called via the GLib mainloop on the tray thread so the
        # AppIndicator calls land on the right thread.
        try:
            from gi.repository import GLib

            def _apply():
                try:
                    if theme_dir and icon_name:
                        ic._appindicator.set_icon_theme_path(theme_dir)
                        ic._appindicator.set_icon_full(icon_name, TRAY_TITLE)
                    else:
                        # Fallback to a Breeze name that always resolves.
                        ic._appindicator.set_icon_full(
                            "input-keyboard-virtual-show", TRAY_TITLE)
                except Exception as e:
                    print(f"tray: set_icon_full failed: {e!r}")
                return False
            GLib.idle_add(_apply)
        except Exception as e:
            print(f"tray: icon-theme override failed: {e!r}")

    tray_thread = threading.Thread(
        target=lambda: icon.run(setup=setup), daemon=True)
    tray_thread.start()

    threading.Thread(target=app.chord_watcher_thread, daemon=True).start()
    threading.Thread(target=app.hotkey_thread, daemon=True).start()
    threading.Thread(target=app.steam_watch_thread, daemon=True).start()
    threading.Thread(target=app.battery_thread, daemon=True).start()
    threading.Thread(target=app.sdl_gamepad_thread, daemon=True).start()
    # Big Picture controller-connect automation (blocks while disabled).
    app._bp_engine.start()

    # First-ever launch (no settings.json found in __init__): open the
    # Keybinds GUI manager and run the guided tutorial over it (see
    # _first_run_reveal), so a new user lands somewhere other than a bare tray
    # icon AND leaves knowing the chords  which is the only way any of them
    # are discoverable. Nothing else auto-opens (and no longer the OSK: the
    # tour's first step opens that). (--no-tray headless mode above
    # intentionally skips this.) `tutorial_done` is belt-and-braces  a fresh
    # install can't have it set  but it keeps "was the tour already seen" a
    # single question.
    # Rasterize the controller-viewer art (pure PIL, ~1s) on its own daemon
    # thread NOW, so the picker's Tk-thread build below only wraps the
    # finished images into PhotoImages instead of paying the render. Needed
    # regardless of the Interactive Controller Preview toggle  OFF still
    # shows this same line-art, just frozen at rest (see _paint_ctrl_canvas),
    # not the older flat controller_triton.png.
    threading.Thread(target=sc_viewer.prewarm, daemon=True).start()
    if app._is_first_run and not app.settings.get("tutorial_done"):
        threading.Thread(target=_first_run_reveal, args=(app,),
                         daemon=True).start()
    else:
        # Pre-build the Keybinds GUI hidden a moment after startup (its own
        # Tk thread; the delay keeps controller/tray init snappy) so the
        # first tray click reveals an already-painted window instantly
        # instead of constructing four tabs of widgets while the user
        # watches. A click that lands mid-build is queued, not dropped.
        warm_t = threading.Timer(2.0, lambda: app._open_keybinds(warm=True))
        warm_t.daemon = True
        warm_t.start()

    try:
        while not app._stop_event.is_set():
            if app._open_kbd_event.wait(timeout=1.0):
                app._open_kbd_event.clear()
                _open_osk_once(app)
    except KeyboardInterrupt:
        pass
    finally:
        app._stop_event.set()
        try:
            icon.stop()
        except Exception:
            pass
        app._shutdown_sdl()


if __name__ == "__main__":
    main()
