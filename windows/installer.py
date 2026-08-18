# -*- coding: utf-8 -*-
"""SteamlessInput install wizard (Windows).

A pick-what-you-want installer. Everything SteamlessInput can use is optional
except the app itself, and several pieces are things a lot of people should
*not* install (the lock-screen keyboard in particular), so this wizard never
installs anything the user didn't tick. It shows, for every component, what it
is, what it costs, and whether the machine already has it.

Design notes
------------
* **stdlib only.** tkinter + ctypes, no PIL, no pywin32. The setup exe is a
  separate download from the app; it has no business being 40 MB.
* **Two privilege phases.** Everything that can be done as the logged-in user
  is done in-process, unelevated. The handful of steps that genuinely need
  administrator rights (lock-screen keyboard, uiAccess relay, a Program Files
  install dir) are batched into ONE elevated re-exec of this same exe with
  ``--run-plan``, so the user sees at most one UAC prompt, at a point where the
  wizard has already told them it is coming. The elevated child streams its
  progress back through a JSONL log file that the parent tails, so the UI shows
  the same running commentary for both phases.
* **Per-user by default.** The default location is ``%LOCALAPPDATA%\\Programs``,
  which needs no elevation and keeps ``settings.json`` writable next to the exe
  (the portable contract in tray.py's ``_settings_paths``). Program Files works
  too; the wizard notices it can't write there and flags the step as admin.
* **Reversible.** The wizard copies itself into the install folder and registers
  an Add/Remove Programs entry, so ``--uninstall`` can undo every box that was
  ticked, including the two machine-wide ones.

Run:
    python installer.py                 # the wizard
    python installer.py --uninstall     # the remover
    python installer.py --console       # same flow, no GUI (needs a console)
    python installer.py --console --yes --with vigem,autostart --dir "D:\\Apps\\SI"
"""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser

from ctypes import wintypes


# --- HiDPI: process awareness + point-size pin -------------------------------
# The wizard is laid out in hardcoded pixels (Wizard.W/H = 760x610, fixed
# padding everywhere) and never declared itself DPI-aware, so on any display
# above 100% scaling Windows bitmap-stretches the whole (DPI-unaware) window
# to match  every edge softens, most visibly the header logo (a raster image
# with fine detail) but really the whole wizard. Same fix keybinds_picker.py
# uses for the same reason: declare per-monitor DPI awareness so Windows stops
# stretching the window, then pin `tk scaling` back to the 96dpi this UI was
# drawn for so Tk's own point-sized fonts don't inflate past the fixed pixel
# geometry around them.
_DESIGN_DPI = 96.0
_TK_SCALING = _DESIGN_DPI / 72.0        # 1.3333 px per point


def _set_dpi_awareness():
    """Best-effort: declare this process per-monitor DPI aware. Must run
    BEFORE the first window is created (see Wizard.__init__). Silently no-ops
    pre-8.1 Windows, where the wizard just renders DPI-unaware as before."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _pin_tk_scaling(root):
    """Force `tk scaling` to the design dpi on `root`. Call immediately after
    creating the interpreter, before any widget/font, so every font (including
    Tk's own default) is computed at the pinned scale."""
    try:
        root.tk.call("tk", "scaling", _TK_SCALING)
    except Exception:
        pass


APP_NAME = "SteamlessInput"
SETUP_VERSION = "1.0"
PUBLISHER = "SteamlessInput"
PROJECT_URL = "https://github.com/PietPetGit/SteamlessInput"

# What the app is called once installed. The release asset is
# `SteamlessInput-windows.exe`; nothing in the app keys off its own filename
# (only install_uia_relay.ps1 does, and we always pass it -ClientExe), so the
# installed copy gets the tidier name.
INSTALLED_EXE = "SteamlessInput.exe"
SETUP_COPY_NAME = "SteamlessInput-Setup.exe"

# Payload filenames we look for next to the setup exe / inside its bundle.
SRC_APP_EXES = ("SteamlessInput-windows.exe", "SteamlessInput.exe")
# The app is a --onedir build: this folder sits beside the exe and has to be
# installed with it (see _step_app). PyInstaller resolves it from the exe's
# directory, so the install-time rename to INSTALLED_EXE does not affect it.
APP_INTERNAL_DIR = "_internal"
SRC_LOCKSCREEN = ("lockscreen-keyboard/LockScreenKeyboard.exe",
                  "LockScreenKeyboard.exe",
                  "dist/LockScreenKeyboard.exe")
SRC_RELAY_DIR = ("uia-relay", "dist/uia-relay")
SRC_RELAY_PS1 = ("install_uia_relay.ps1",)
SRC_DOCS = ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")

ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SteamlessInput"

VIGEM_URL = "https://github.com/nefarius/ViGEmBus/releases"
HIDHIDE_URL = "https://github.com/nefarius/HidHide/releases"
# winget package identifiers. These are NOT guessable from the vendor name:
# ViGEmBus is published under the "ViGEm" publisher, not "Nefarius", even
# though both projects are Nefarius'. Verified against the winget-pkgs manifest
# paths  manifests/v/ViGEm/ViGEmBus/ and manifests/n/Nefarius/HidHide/
# (manifests/n/Nefarius/ViGEmBus/ is a 404). A wrong id fails silently: winget
# just reports "no package found" and the wizard drops to the browser fallback,
# so the whole winget path quietly stops being useful. Re-check these if a
# component ever starts always opening the download page.
VIGEM_WINGET_ID = "ViGEm.ViGEmBus"
HIDHIDE_WINGET_ID = "Nefarius.HidHide"

# The lock-screen keyboard's install target and the Utilman hijack it registers.
# Mirrors lockscreen-keyboard/install.ps1 exactly  same folder, same key  so
# either installer can undo the other's work.
LOCKSCREEN_DIR = r"C:\LockScreenKeyboard"
IFEO_UTILMAN = (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
                r"\Image File Execution Options\Utilman.exe")

RELAY_DIR_REL = r"SteamlessInput\uia-relay"

CREATE_NO_WINDOW = 0x08000000


# =============================================================================
# small win32 / filesystem helpers
# =============================================================================

def _frozen():
    return getattr(sys, "frozen", False)


def _self_path():
    """This installer's own executable (frozen) or script path."""
    return os.path.abspath(sys.executable if _frozen() else os.path.abspath(__file__))


def _self_dir():
    return os.path.dirname(_self_path())


def _relaunch_argv(extra):
    """argv that re-runs THIS installer with `extra` appended."""
    if _frozen():
        return [sys.executable] + list(extra)
    return [sys.executable, os.path.abspath(__file__)] + list(extra)


# Extra payload root handed to the elevated child through the plan file. The
# child is normally the same exe in the same folder, but if the wizard was
# started from somewhere unusual (a mapped drive, a temp extraction) this keeps
# both halves looking at the same files.
_EXTRA_ROOT = None


def _search_roots():
    """Where payload files may live, most-specific first.

    A bundled setup exe carries its payload in ``_MEIPASS/payload``; a setup exe
    shipped inside the release zip finds it as a sibling; running from a source
    checkout finds it in ``dist/``."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots += [os.path.join(meipass, "payload"), meipass]
    here = _self_dir()
    # The parent covers a source checkout, where README/LICENSE sit at the repo
    # root while the exe is built into windows/dist.
    roots += [here, os.path.join(here, "dist"), os.path.dirname(here)]
    if _EXTRA_ROOT:
        roots += [_EXTRA_ROOT, os.path.join(_EXTRA_ROOT, "dist")]
    roots += [os.getcwd()]
    seen, out = set(), []
    for r in roots:
        k = os.path.normcase(os.path.abspath(r))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _find(names, want_dir=False):
    """First existing path among `names` (relative) across the search roots."""
    for root in _search_roots():
        for name in names:
            p = os.path.join(root, name.replace("/", os.sep))
            if (os.path.isdir(p) if want_dir else os.path.isfile(p)):
                return os.path.abspath(p)
    return None


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _dir_writable(path):
    """True when the current token could create files in `path` (creating the
    folder along the way if needed). Used to decide whether the copy step has
    to be pushed into the elevated phase."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent
    try:
        with tempfile.NamedTemporaryFile(dir=probe, prefix=".si-write-test"):
            return True
    except OSError:
        return False


def _free_space(path):
    """Free bytes on the volume holding `path`, or None."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    free = ctypes.c_ulonglong(0)
    try:
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(probe), ctypes.byref(free), None, None)
        return free.value if ok else None
    except Exception:
        return None


def _human(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return "?"


def _run(cmd, timeout=None, shell=False):
    """Run a command with no console flash. Returns (rc, combined output)."""
    try:
        p = subprocess.run(
            cmd, shell=shell, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 1460, "timed out"
    except OSError as e:
        return 1, str(e)


def _powershell(script, timeout=600):
    return _run(["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                timeout=timeout)


def _reg_open(root, path, access):
    import winreg
    return winreg.OpenKey(root, path, 0, access)


def _service_exists(name):
    """True when HKLM\\...\\Services\\<name> is present  the cheapest honest
    test for 'is this kernel driver installed', and one that needs no
    elevation and no DLL of the driver's own."""
    import winreg
    path = "\\".join((r"SYSTEM\CurrentControlSet\Services", name))
    for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
        try:
            with _reg_open(winreg.HKEY_LOCAL_MACHINE, path,
                           winreg.KEY_READ | view):
                return True
        except OSError:
            continue
    return False


def _app_running():
    rc, out = _run(["tasklist", "/FO", "CSV", "/NH"], timeout=30)
    if rc != 0:
        return False
    low = out.lower()
    return "steamlessinput" in low


def _stop_app():
    """Close any running SteamlessInput so its exe can be overwritten."""
    killed = False
    for image in (INSTALLED_EXE, "SteamlessInput-windows.exe"):
        rc, _ = _run(["taskkill", "/IM", image, "/F"], timeout=30)
        if rc == 0:
            killed = True
    if killed:
        time.sleep(0.8)      # let the file handles actually drop
    return killed


def _self_delete_later(exe, folder):
    """Schedule the running uninstaller (and its now-empty folder) for deletion.

    A process can't delete its own image while it runs, so hand the job to a
    detached cmd that waits for us to exit first. Failure is harmless  worst
    case one stale exe remains."""
    try:
        fd, bat_path = tempfile.mkstemp(suffix=".bat")
        with os.fdopen(fd, "w") as f:
            f.write("@echo off\r\n")
            f.write("ping 127.0.0.1 -n 4 >nul\r\n")
            f.write(f'del /f /q "{exe}"\r\n')
            f.write(f'rmdir "{folder}" 2>nul\r\n')
            f.write(f'del /f /q "%~f0"\r\n')
        subprocess.Popen(["cmd", "/c", bat_path], creationflags=(
            CREATE_NO_WINDOW | 0x00000008))       # DETACHED_PROCESS
    except OSError:
        pass


def _key_is_empty(root, path):
    """True when a registry key holds no values and no subkeys  the only case
    where deleting it is safe rather than destructive."""
    import winreg
    try:
        with _reg_open(root, path, winreg.KEY_READ) as k:
            subkeys, values, _t = winreg.QueryInfoKey(k)
            return subkeys == 0 and values == 0
    except OSError:
        return False


def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


# --- elevation ---------------------------------------------------------------

class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100
_WAIT_TIMEOUT = 0x00000102


def _shell_execute_elevated(exe, params):
    """ShellExecuteEx(runas) and hand back a waitable process handle.

    ``subprocess`` can't request elevation, and plain ShellExecuteW gives no
    handle  without one there is no way to know when the elevated half has
    finished, so the wizard would have to guess. Returns (handle, error)."""
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = os.path.dirname(exe)
    info.nShow = 1                                  # SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error() or ctypes.GetLastError()
        # 1223 == ERROR_CANCELLED: the user clicked No on the UAC prompt.
        return None, ("cancelled" if err == 1223 else f"error {err}")
    return info.hProcess, None


def _wait_process(handle, ms):
    """True once the handle is signalled; False on timeout."""
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    return k32.WaitForSingleObject(handle, ms) != _WAIT_TIMEOUT


def _process_exit_code(handle):
    k32 = ctypes.windll.kernel32
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    code = wintypes.DWORD(0)
    if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
        return code.value
    return 1


def _close_handle(handle):
    try:
        k32 = ctypes.windll.kernel32
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle(handle)
    except Exception:
        pass


# --- shortcuts ---------------------------------------------------------------

def _shortcut_api():
    """autostart.py owns the IShellLinkW plumbing; reuse it when importable.

    A setup exe built by build_installer.py bundles autostart.py precisely so
    this import works. The tiny inline fallback keeps installer.py runnable on
    its own from a bare checkout."""
    try:
        import autostart
        return autostart
    except Exception:
        return None


def _make_shortcut(lnk, target, workdir=None, icon=None, args=""):
    api = _shortcut_api()
    if api is None:
        return False
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    return bool(api.create_shortcut(lnk, target, args,
                                    workdir or os.path.dirname(target),
                                    icon or target))


def _known_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
        return buf.value
    return None


_CSIDL_DESKTOPDIRECTORY = 0x0010
_CSIDL_PROGRAMS = 0x0002


def _desktop_lnk():
    d = _known_folder(_CSIDL_DESKTOPDIRECTORY) or os.path.join(
        os.path.expanduser("~"), "Desktop")
    return os.path.join(d, f"{APP_NAME}.lnk")


def _startmenu_lnk():
    d = _known_folder(_CSIDL_PROGRAMS)
    if not d:
        appdata = os.environ.get("APPDATA", "")
        d = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
    return os.path.join(d, f"{APP_NAME}.lnk")


# =============================================================================
# component model
# =============================================================================

class Ctx(object):
    """Everything a step needs, plus the log sink it reports through."""

    def __init__(self, install_dir, log, opts=None):
        # Normalize here, once: this path lands in the registry, in shortcuts
        # and in the relay's allowlist, and a forward-slashed or trailing-slash
        # variant of the same folder compares unequal to all of them.
        self.install_dir = os.path.normpath(os.path.abspath(install_dir))
        self.log = log
        self.opts = opts or {}

    @property
    def app_exe(self):
        return os.path.join(self.install_dir, INSTALLED_EXE)


class Step(object):
    def __init__(self, key, title, blurb, run, *, undo=None, detect=None,
                 default=True, required=False, admin=False, danger=False,
                 needs=None, note=None, warning=None, uninstall_default=True,
                 uninstall_title=None, skip_if_present=False,
                 requires=(), auto=False):
        self.key = key
        self.title = title
        self.blurb = blurb            # one-paragraph "what is this"
        self.run = run                # (ctx) -> bool
        self.undo = undo              # (ctx) -> bool
        self.detect = detect          # () -> True/False/None
        self.default = default        # bool, or a () -> bool probe
        self.required = required
        # Hard dependency: choosing this step also chooses `requires`, and
        # dropping one of those drops this. `auto` is the same edge read the
        # other way  choosing what this needs also chooses this. Both exist
        # for one relationship: installing the HidHide driver without the
        # configuration that follows it does nothing at all, and configuring
        # without the driver can't work, so the pair travels together.
        self.requires = tuple(requires)
        self.auto = auto
        self.admin = admin            # needs the elevated phase
        self.danger = danger          # show the red confirm modal
        self.needs = needs            # () -> (ok, reason) payload availability
        self.note = note              # extra grey line under the blurb
        self.warning = warning        # full text for the danger modal
        self.uninstall_default = uninstall_default
        self.uninstall_title = uninstall_title or title
        # True for third-party installs that are pointless to re-run. Shortcuts
        # and autostart deliberately DON'T set this: re-running them re-points
        # an existing entry at the newly chosen folder, which is exactly what an
        # upgrade needs.
        self.skip_if_present = skip_if_present

    def initial_tick(self):
        """Whether this component starts ticked on a fresh run of the wizard."""
        if self.required:
            return True
        avail, _why = self.available()
        if not avail:
            return False
        if self.skip_if_present and self.present() is True:
            return False
        if callable(self.default):
            # A probe rather than a constant: "tick this only if this PC has
            # the hardware it is for" can't be known at table-build time.
            try:
                return bool(self.default())
            except Exception:
                return False
        return bool(self.default)

    def available(self):
        if self.needs is None:
            return True, ""
        try:
            return self.needs()
        except Exception as e:
            return False, str(e)

    def present(self):
        if self.detect is None:
            return None
        try:
            return self.detect()
        except Exception:
            return None


# --- detection ---------------------------------------------------------------

def _detect_vigem():
    return _service_exists("ViGEmBus") or os.path.isfile(
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "drivers", "ViGEmBus.sys"))


def _detect_hidhide():
    return _service_exists("HidHide")


def _detect_lockscreen():
    import winreg
    try:
        with _reg_open(winreg.HKEY_LOCAL_MACHINE, IFEO_UTILMAN,
                       winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, "Debugger")
            return bool(val)
    except OSError:
        return False


def _relay_dest():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    return os.path.join(pf, RELAY_DIR_REL)


def _detect_relay():
    return os.path.isfile(os.path.join(_relay_dest(), "SteamlessInputRelay.exe"))


def _detect_installed():
    """(install_dir, version) of a previous wizard install, or (None, None)."""
    import winreg
    try:
        with _reg_open(winreg.HKEY_CURRENT_USER, ARP_KEY, winreg.KEY_READ) as k:
            loc = winreg.QueryValueEx(k, "InstallLocation")[0]
            try:
                ver = winreg.QueryValueEx(k, "DisplayVersion")[0]
            except OSError:
                ver = None
            return (loc or None), ver
    except OSError:
        return None, None


def _winget():
    """Path to winget.exe, or None.

    App Installer puts it in the per-user WindowsApps alias folder, which is on
    PATH for interactive shells but not always for a process launched from a
    shortcut  so check the canonical location too before declaring it absent."""
    p = shutil.which("winget")
    if p:
        return p
    local = os.environ.get("LOCALAPPDATA")
    if local:
        alias = os.path.join(local, "Microsoft", "WindowsApps", "winget.exe")
        if os.path.isfile(alias):
            return alias
    return None


# =============================================================================
# HidHide auto-configuration  the part people used to do by hand
# =============================================================================
# Installing the HidHide driver on its own changes nothing: until something
# tells it WHICH device to hide and WHO may still see it, the Switch Pro keeps
# spamming its phantom buttons 1-8 into every game. That "last part" used to be
# three manual clicks in HidHide Configuration Client (Applications -> add the
# app, Devices -> tick the Pro Controller, Enable device hiding), and it is the
# step everybody got stuck on. This section does it for them.
#
# Two rules, both learned the hard way (see the note in tray.py's history: an
# app-driven HidHide integration was ripped out in 2026-06 for exactly this):
#
#   1. ONE process per transaction. HidHide's control device is effectively
#      single-client, and firing several HidHideCLI.exe calls back to back
#      wedged the driver hard enough to need a reboot. HidHideCLI takes a
#      SEQUENCE of commands in a single invocation ("The above commands can be
#      sequenced reducing the overall overhead involved.")  every write below
#      is one command line, one process, one open of the control device.
#   2. This lives in the INSTALLER, never in the tray. The control device needs
#      elevation, and the wizard already has an elevated phase; the tray must
#      never run elevated just for this.
#
# Verified against HidHideCLI 1.5.230.0 on Windows 10 22H2.

HIDHIDE_TASK_NAME = "SteamlessInput HidHide setup"
# Where the deferred run records what it has already tried. HKLM because the
# only writers are the elevated phase and a SYSTEM scheduled task; HKCU would
# be a different hive for each of them.
HIDHIDE_STATE_KEY = r"SOFTWARE\SteamlessInput\HidHide"
# A fresh HidHide install can't be configured until the machine reboots, so the
# work is handed to a logon task. It removes itself the moment it succeeds;
# this cap stops it living forever on a PC whose Nintendo pad never appears.
HIDHIDE_TASK_MAX_TRIES = 5

# Nintendo's USB vendor id, in the two spellings a Windows device instance path
# uses. A cabled pad enumerates as HID\VID_057E&PID_2009\...; the SAME pad over
# Bluetooth enumerates through the Bluetooth HID profile, which spells the
# vendor VID&0002057E (0002 = "vendor id source: USB-IF"). Matching only the
# first form would miss every wireless Switch Pro  which is the common case.
_NINTENDO_VID_TOKENS = ("VID_057E", "VID&0002057E")

# Fallback for devices whose instance path carries no vendor id at all (a BLE
# bridge, a third-party adapter). Deliberately narrow, and kept in step with
# pads._NINTENDO_NAME_MARKERS: a false positive here hides somebody's unrelated
# controller from every game on the machine.
_NINTENDO_NAME_MARKERS = ("nintendo", "joy-con", "joycon", "switch pro",
                          "pro controller", "switch 2")

_HH_CLI = ()            # () = not looked up yet, None = not installed
_HH_STATE = ()          # cached _hidhide_read_state result


def _hidhide_cli():
    """Path to HidHideCLI.exe, or None.

    The MSI leaves no registry breadcrumb of its own (verified: there is no
    HKLM\\SOFTWARE\\Nefarius Software Solutions key), so this checks the install
    layout first and falls back to the Add/Remove Programs entry."""
    global _HH_CLI
    if _HH_CLI != ():
        return _HH_CLI

    candidates = []
    for env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if not base:
            continue
        for arch in ("x64", "x86"):
            candidates.append(os.path.join(
                base, "Nefarius Software Solutions", "HidHide", arch,
                "HidHideCLI.exe"))
    found = next((p for p in candidates if os.path.isfile(p)), None)
    if not found:
        found = shutil.which("HidHideCLI")
    if not found:
        found = _hidhide_cli_from_arp()
    _HH_CLI = found
    return found


def _hidhide_cli_from_arp():
    """Dig HidHide's install folder out of Add/Remove Programs."""
    import winreg
    roots = ((winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion"
              r"\Uninstall"))
    for hive, path in roots:
        try:
            key = _reg_open(hive, path, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    name = winreg.EnumKey(key, i)
                    with _reg_open(key, name, winreg.KEY_READ) as sub:
                        disp = winreg.QueryValueEx(sub, "DisplayName")[0]
                        if "hidhide" not in str(disp).lower():
                            continue
                        loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                except OSError:
                    continue
                for arch in ("x64", "x86", ""):
                    p = os.path.join(loc, arch, "HidHideCLI.exe")
                    if os.path.isfile(p):
                        return p
    return None


def _hidhide_run(args, timeout=120):
    """One HidHideCLI invocation. See rule 1 at the top of this section  never
    split a transaction across several of these."""
    cli = _hidhide_cli()
    if not cli:
        return None, "HidHideCLI.exe not found"
    rc, out = _run([cli] + list(args), timeout=timeout)
    return rc, out


def _hidhide_read_state(refresh=False):
    """Current HidHide configuration, or None when the control device can't be
    opened (not elevated, or the driver hasn't initialised since its install).

    HidHideCLI reports state as the command line that would recreate it
    `--cloak-on`, `--app-reg "path"`, `--dev-hide "path"`  so the reply parses
    with the same vocabulary we write with."""
    global _HH_STATE
    if _HH_STATE != () and not refresh:
        return _HH_STATE

    state = None
    rc, out = _hidhide_run(["--cloak-state", "--inv-state", "--app-list",
                            "--dev-list"], timeout=60)
    if rc == 0:
        state = {"cloak": False, "inverse": False, "apps": [], "devices": []}
        for line in (out or "").splitlines():
            line = line.strip()
            if line == "--cloak-on":
                state["cloak"] = True
            elif line == "--inv-on":
                state["inverse"] = True
            elif line.startswith("--app-reg "):
                state["apps"].append(line[10:].strip().strip('"'))
            elif line.startswith("--dev-hide "):
                state["devices"].append(line[11:].strip().strip('"'))
    _HH_STATE = state
    return state


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
        os.path.abspath(b))


def _is_nintendo_device(instance_path, *labels):
    """True when this HID device is one of Nintendo's controllers."""
    up = (instance_path or "").upper().replace(" ", "")
    if any(tok in up for tok in _NINTENDO_VID_TOKENS):
        return True
    for label in labels:
        low = (label or "").lower()
        if any(m in low for m in _NINTENDO_NAME_MARKERS):
            return True
    return False


def _nintendo_from_registry():
    """Every Nintendo HID device this PC has ever seen, from the PnP database.

    Windows keeps a device's Enum key long after it is unplugged, so this finds
    the controller even when it is switched off  which matters, because that is
    exactly the state a Bluetooth pad is in while somebody runs an installer.
    Also the only source that works before HidHide's driver is alive."""
    import winreg
    out = []
    for sub in ("HID", "BTHENUM", "BTHLEDEVICE"):
        try:
            key = _reg_open(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Enum" + "\\" + sub,
                            winreg.KEY_READ)
        except OSError:
            continue                    # BTHENUM only exists with a BT radio
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    dev = winreg.EnumKey(key, i)
                except OSError:
                    break
                if not _is_nintendo_device(dev):
                    continue
                try:
                    node = _reg_open(key, dev, winreg.KEY_READ)
                except OSError:
                    continue
                with node:
                    for j in range(winreg.QueryInfoKey(node)[0]):
                        try:
                            inst = winreg.EnumKey(node, j)
                        except OSError:
                            break
                        out.append("\\".join((sub, dev, inst)))
    return out


def _nintendo_devices():
    """(device instance path, label) for every Nintendo pad on this PC.

    Two sources, unioned: HidHide's own `--dev-all` (which knows the friendly
    names, and is the list the driver itself matches against) and the PnP
    registry (which works unelevated and before the driver is up). `--dev-all`
    rather than `--dev-gaming`: an absent pad reports gamingDevice=false, so the
    gaming filter drops the very controller we came here for."""
    labels = {}
    rc, out = _hidhide_run(["--dev-all"], timeout=90)
    if rc == 0:
        start = (out or "").find("[")
        try:
            groups = json.loads(out[start:]) if start >= 0 else []
        except ValueError:
            groups = []
        for group in groups:
            friendly = group.get("friendlyName") or ""
            for dev in group.get("devices") or []:
                path = dev.get("deviceInstancePath") or ""
                if not path or not _is_nintendo_device(
                        path, friendly, dev.get("product"),
                        dev.get("vendor"), dev.get("description")):
                    continue
                name = (dev.get("product") or friendly
                        or dev.get("description") or "Nintendo controller")
                if not dev.get("present"):
                    name += " (not connected right now)"
                labels[path.upper()] = (path, name)

    for path in _nintendo_from_registry():
        labels.setdefault(path.upper(), (path, "Nintendo controller"))
    return [labels[k] for k in sorted(labels)]


def _nintendo_known():
    """Cheap "does this PC have a Nintendo pad at all" probe for the default
    tick  registry only, so it costs no process and needs no elevation."""
    try:
        return bool(_nintendo_from_registry())
    except Exception:
        return False


def _hidhide_apps(ctx):
    """Executables that must keep seeing the hidden controller.

    The installed exe always (it is what the shortcuts launch), plus the
    release asset's own filename when a copy of it is sitting in the same
    folder  people do run the portable exe straight out of the install dir,
    and to HidHide that is a different application."""
    apps = []
    for name in (INSTALLED_EXE,) + tuple(SRC_APP_EXES):
        exe = os.path.join(ctx.install_dir, name)
        # Only what is really on disk. By the time this step runs the app has
        # been copied (it is the first entry in the table, elevated phase
        # included), so a missing file means the folder is wrong  and a
        # path to nothing is just litter in a driver's allow-list.
        if os.path.isfile(exe) and not any(_same_path(exe, a) for a in apps):
            apps.append(exe)
    return apps


# --- deferred (post-reboot) run ----------------------------------------------

def _hh_state_open(write=False):
    import winreg
    access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
    if write:
        return winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE,
                                  HIDHIDE_STATE_KEY, 0, access)
    return _reg_open(winreg.HKEY_LOCAL_MACHINE, HIDHIDE_STATE_KEY, access)


def _hh_state_save(devices=None, apps=None, cloaked_by_us=None, tries=None):
    """Remember what WE changed, so the uninstaller can put back exactly that
    and nothing else  another tool's hidden devices are not ours to unhide.

    ACCUMULATES. Every later run adds only what it had to add, so a plain
    overwrite would erase the record of the first run  the very run that hid
    the controller and switched cloaking on."""
    import winreg
    old = _hh_state_load()
    try:
        with _hh_state_open(write=True) as k:
            if devices is not None:
                winreg.SetValueEx(k, "Devices", 0, winreg.REG_MULTI_SZ,
                                  _merge_paths(old["devices"], devices))
            if apps is not None:
                winreg.SetValueEx(k, "Apps", 0, winreg.REG_MULTI_SZ,
                                  _merge_paths(old["apps"], apps))
            if cloaked_by_us is not None:
                winreg.SetValueEx(k, "CloakedByUs", 0, winreg.REG_DWORD,
                                  1 if (cloaked_by_us or old["cloaked_by_us"])
                                  else 0)
            if tries is not None:
                winreg.SetValueEx(k, "Tries", 0, winreg.REG_DWORD, int(tries))
        return True
    except OSError:
        return False


def _merge_paths(old, new):
    """Union of two path lists, case-insensitively, keeping the first spelling
    of each  a duplicate here would be un-registered twice on the way out."""
    out, seen = [], set()
    for path in list(old) + list(new):
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _hh_state_load():
    import winreg
    out = {"devices": [], "apps": [], "cloaked_by_us": False, "tries": 0}
    try:
        with _hh_state_open() as k:
            for name, key in (("Devices", "devices"), ("Apps", "apps")):
                try:
                    out[key] = list(winreg.QueryValueEx(k, name)[0] or [])
                except OSError:
                    pass
            for name, key in (("CloakedByUs", "cloaked_by_us"),
                              ("Tries", "tries")):
                try:
                    out[key] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
    except OSError:
        pass
    out["cloaked_by_us"] = bool(out["cloaked_by_us"])
    out["tries"] = int(out["tries"] or 0)
    return out


def _hh_state_clear():
    import winreg
    for path in (HIDHIDE_STATE_KEY, r"SOFTWARE\SteamlessInput"):
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            break              # the parent still holds something  leave it


def _hidhide_task_exists():
    rc, _out = _run(["schtasks", "/Query", "/TN", HIDHIDE_TASK_NAME],
                    timeout=60)
    return rc == 0


def _hidhide_task_create(exe, install_dir):
    """Run the configuration again at the next sign-in, as SYSTEM.

    ONLOGON + /RU SYSTEM is the one form that needs no stored password and no
    UAC prompt; HKLM RunOnce would run under the user's FILTERED token and be
    denied by HidHide's control device. The task deletes itself as soon as it
    succeeds (see hidhide_flow)."""
    if not os.path.isfile(exe):
        return False
    tr = f'"{exe}" --hidhide --quiet --dir "{install_dir}"'
    rc, out = _run(["schtasks", "/Create", "/F", "/TN", HIDHIDE_TASK_NAME,
                    "/SC", "ONLOGON", "/RU", "SYSTEM", "/RL", "HIGHEST",
                    "/TR", tr], timeout=120)
    return rc == 0 or "SUCCESS" in (out or "").upper()


def _hidhide_task_delete():
    _run(["schtasks", "/Delete", "/F", "/TN", HIDHIDE_TASK_NAME], timeout=60)


def _hidhide_defer(ctx, why):
    """Hand the configuration to a logon task and say so."""
    exe = os.path.join(ctx.install_dir, SETUP_COPY_NAME)
    if not os.path.isfile(exe):
        exe = _self_path() if _frozen() else ""
    tries = _hh_state_load()["tries"]
    if tries >= HIDHIDE_TASK_MAX_TRIES:
        ctx.log("warn", f"{why} Giving up after {tries} attempts  run "
                        f"SteamlessInput-Setup --hidhide once the controller "
                        f"is connected.")
        _hidhide_task_delete()
        return False
    if exe and _hidhide_task_create(exe, ctx.install_dir):
        _hh_state_save(tries=tries + 1)
        ctx.log("warn", f"{why} It will finish by itself the next time you "
                        f"sign in  nothing for you to do.")
        return True
    ctx.log("warn", f"{why} Run SteamlessInput-Setup --hidhide after the "
                    f"restart to finish it.")
    return False


# --- payload availability ----------------------------------------------------

def _need_app():
    p = _find(SRC_APP_EXES)
    if p and not os.path.isdir(os.path.join(os.path.dirname(p),
                                            APP_INTERNAL_DIR)):
        # An exe with no _internal/ beside it is half a download, not an app.
        return False, (f"{os.path.basename(p)} is here but its "
                       f"{APP_INTERNAL_DIR} folder is missing. Unzip the "
                       f"whole release and keep the files together.")
    if p:
        return True, p
    return False, ("SteamlessInput-windows.exe was not found next to this "
                   "installer. Keep the release files together and run it again.")


def _need_lockscreen():
    p = _find(SRC_LOCKSCREEN)
    if p:
        return True, p
    # Deliberately not in the main zip: it is 44 MB and it is the one component
    # this project recommends against, so nobody pays for it by default.
    return False, ("LockScreenKeyboard.exe isn't here. It's a separate "
                   "download  grab SteamlessInput-lockscreen-addon.zip from "
                   "the Releases page, unzip it next to this installer, and "
                   "run the wizard again.")


def _need_relay():
    d = _find(SRC_RELAY_DIR, want_dir=True)
    if d and os.path.isfile(os.path.join(d, "SteamlessInputRelay.exe")):
        return True, d
    return False, ("The relay files (uia-relay\\) aren't next to this "
                   "installer. They ship inside SteamlessInput-windows.zip  "
                   "keep the unzipped files together and run it again.")


# --- the steps ---------------------------------------------------------------

def _step_app(ctx):
    ok, src = _need_app()
    if not ok:
        ctx.log("err", src)
        return False

    if _app_running():
        ctx.log("info", "Closing the running copy of SteamlessInput...")
        _stop_app()

    try:
        os.makedirs(ctx.install_dir, exist_ok=True)
    except OSError as e:
        ctx.log("err", f"Could not create {ctx.install_dir}: {e}")
        return False

    dst = ctx.app_exe
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        ctx.log("err", f"Copy failed: {e}")
        return False

    # The app is a PyInstaller --onedir build: the exe is inert without the
    # `_internal/` folder that ships beside it, and the folder is found by the
    # exe's DIRECTORY, not its name  so the install-time rename to
    # SteamlessInput.exe is fine as long as _internal/ lands next to it.
    # Replaced wholesale rather than merged, so an upgrade can't leave a
    # previous version's stray module behind to be imported.
    src_internal = os.path.join(os.path.dirname(src), APP_INTERNAL_DIR)
    if os.path.isdir(src_internal):
        dst_internal = os.path.join(ctx.install_dir, APP_INTERNAL_DIR)
        try:
            if os.path.isdir(dst_internal):
                shutil.rmtree(dst_internal)
            shutil.copytree(src_internal, dst_internal)
        except OSError as e:
            ctx.log("err", f"Copying {APP_INTERNAL_DIR} failed: {e}")
            return False
    else:
        ctx.log("err", f"{APP_INTERNAL_DIR} was not found next to "
                       f"{os.path.basename(src)}. Keep the release files "
                       f"together and run the installer again.")
        return False
    ctx.log("ok", f"Installed {INSTALLED_EXE} to {ctx.install_dir}")

    # Docs alongside the app: GPL-3.0 asks that the licence travel with the
    # binary, and the README is the only place the chords are written down.
    for name in SRC_DOCS:
        p = _find((name,))
        if p and os.path.normcase(p) != os.path.normcase(
                os.path.join(ctx.install_dir, name)):
            try:
                shutil.copy2(p, os.path.join(ctx.install_dir, name))
            except OSError:
                pass

    # Keep a copy of the wizard so Add/Remove Programs has something to call.
    setup_copy = os.path.join(ctx.install_dir, SETUP_COPY_NAME)
    if _frozen():
        try:
            if os.path.normcase(_self_path()) != os.path.normcase(setup_copy):
                shutil.copy2(_self_path(), setup_copy)
        except OSError as e:
            ctx.log("warn", f"Could not save the uninstaller: {e}")
            setup_copy = None
    else:
        setup_copy = None      # running from source: no self-contained remover

    _write_arp(ctx, setup_copy)
    return True


def _write_arp(ctx, setup_copy):
    """Register in Add/Remove Programs (per-user  matches the default install
    location, and needs no elevation)."""
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ARP_KEY) as k:
            def s(name, value):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

            s("DisplayName", APP_NAME)
            s("DisplayVersion", SETUP_VERSION)
            s("Publisher", PUBLISHER)
            s("InstallLocation", ctx.install_dir)
            s("DisplayIcon", ctx.app_exe)
            s("URLInfoAbout", PROJECT_URL)
            if setup_copy:
                s("UninstallString", f'"{setup_copy}" --uninstall')
                s("QuietUninstallString", f'"{setup_copy}" --uninstall --console --yes')
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
            size_kb = int(_dir_size(ctx.install_dir) / 1024) or 1
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        ctx.log("ok", "Listed in Apps & features")
        return True
    except OSError as e:
        ctx.log("warn", f"Could not register the uninstall entry: {e}")
        return False


def _undo_app(ctx):
    import winreg
    if _app_running():
        ctx.log("info", "Closing SteamlessInput...")
        _stop_app()
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, ARP_KEY)
    except OSError:
        pass

    keep = ctx.opts.get("keep_settings", True)
    removed_any = False
    kept_settings = False
    self_inside = None
    d = ctx.install_dir
    if os.path.isdir(d):
        for name in os.listdir(d):
            if keep and name.lower() == "settings.json":
                kept_settings = True
                continue
            # Never delete the uninstaller we are currently running from  a
            # process can't unlink its own image. It gets a post-exit sweep.
            p = os.path.join(d, name)
            if os.path.normcase(p) == os.path.normcase(_self_path()):
                self_inside = p
                continue
            try:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
                removed_any = True
            except OSError as e:
                ctx.log("warn", f"Could not remove {name}: {e}")
        try:
            os.rmdir(d)
        except OSError:
            pass
    ctx.log("ok", "Removed the app files" if removed_any
            else "App files were already gone")
    if kept_settings:
        ctx.log("info", f"Kept your settings.json in {d}")
    elif self_inside:
        _self_delete_later(self_inside, d)
        ctx.log("info", "The uninstaller removes itself once this window "
                        "closes")
    return True


def _step_startmenu(ctx):
    lnk = _startmenu_lnk()
    if _make_shortcut(lnk, ctx.app_exe):
        ctx.log("ok", "Added a Start Menu shortcut")
        return True
    ctx.log("warn", "Could not create the Start Menu shortcut")
    return False


def _undo_startmenu(ctx):
    return _remove_file(ctx, _startmenu_lnk(), "Start Menu shortcut")


def _step_desktop(ctx):
    lnk = _desktop_lnk()
    if _make_shortcut(lnk, ctx.app_exe):
        ctx.log("ok", "Added a Desktop shortcut")
        return True
    ctx.log("warn", "Could not create the Desktop shortcut")
    return False


def _undo_desktop(ctx):
    return _remove_file(ctx, _desktop_lnk(), "Desktop shortcut")


def _remove_file(ctx, path, label):
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            ctx.log("ok", f"Removed the {label}")
        except OSError as e:
            ctx.log("warn", f"Could not remove the {label}: {e}")
            return False
    return True


def _step_autostart(ctx):
    api = _shortcut_api()
    if api is None:
        ctx.log("warn", "autostart helper unavailable  skipped")
        return False
    if api.enable_for(ctx.app_exe):
        ctx.log("ok", "SteamlessInput will start with Windows")
        return True
    ctx.log("warn", "Could not create the Startup shortcut")
    return False


def _undo_autostart(ctx):
    api = _shortcut_api()
    if api is None:
        return True
    if api.disable():
        ctx.log("ok", "Removed from Windows startup")
    return True


def _detect_autostart():
    api = _shortcut_api()
    return api.is_enabled() if api else None


def _winget_install(ctx, pkg_id, label, fallback_url):
    """winget first, browser second. Never a silent download of our own: the
    packages come from the vendor's own winget manifest, and if winget isn't
    there we hand the user the release page rather than fetching an exe."""
    wg = _winget()
    if wg:
        ctx.log("info", f"Installing {label} via winget (its installer may "
                        f"ask for permission)...")
        base = [wg, "install", "--id", pkg_id, "--exact",
                "--accept-package-agreements", "--accept-source-agreements"]
        rc, out = _run(base + ["--silent"], timeout=900)
        if rc != 0:
            # A fair number of driver packages refuse a silent switch; retry
            # letting the vendor installer show its own UI before giving up.
            ctx.log("info", f"Silent install returned {rc}; retrying "
                            f"interactively...")
            rc, out = _run(base, timeout=1800)
        if rc == 0:
            ctx.log("ok", f"{label} installed")
            return True
        tail = " ".join(out.split())[-180:]
        ctx.log("warn", f"winget could not install {label} (exit {rc}). {tail}")
    else:
        ctx.log("warn", "winget isn't available on this PC.")
    ctx.log("info", f"Opening the {label} download page instead  install it "
                    f"from there, then restart SteamlessInput.")
    try:
        webbrowser.open(fallback_url)
    except Exception:
        ctx.log("info", fallback_url)
    return False


def _step_vigem(ctx):
    if _detect_vigem():
        ctx.log("ok", "ViGEmBus is already installed")
        return True
    return _winget_install(ctx, VIGEM_WINGET_ID, "ViGEmBus", VIGEM_URL)


def _step_hidhide(ctx):
    if _detect_hidhide():
        ctx.log("ok", "HidHide is already installed")
        return True
    ok = _winget_install(ctx, HIDHIDE_WINGET_ID, "HidHide", HIDHIDE_URL)
    if ok:
        # The Configuration Client is no longer part of the instructions: the
        # hidhide_setup step below does all three of its clicks.
        ctx.log("info", "HidHide needs a REBOOT before its driver comes alive.")
    return ok


# --- admin-phase steps -------------------------------------------------------

def _detect_hidhide_setup():
    """True when the controller is already hidden and we are allowed to see it,
    False when it plainly isn't, None when this process can't tell.

    None matters: reading HidHide's configuration needs the control device,
    which needs elevation, and the components page runs unelevated. A card that
    claimed "not set up" from a failed read would be lying."""
    if not _hidhide_cli():
        return False
    state = _hidhide_read_state()
    if state is None:
        return None
    if not state["cloak"]:
        return False
    hidden = {p.upper() for p in state["devices"]}
    return any(_is_nintendo_device(p) for p in hidden)


def _step_hidhide_setup(ctx):
    """Do the three Configuration Client clicks for the user."""
    if not _hidhide_cli():
        ctx.log("warn", "HidHide isn't installed, so there is nothing to "
                        "configure. Tick the HidHide component as well.")
        return False

    state = _hidhide_read_state(refresh=True)
    if state is None:
        # Denied or unreachable. On a machine that just installed HidHide this
        # is the normal answer  the driver only starts serving its control
        # device after a reboot  so hand the job to the logon task instead of
        # failing the install.
        return _hidhide_defer(
            ctx, "HidHide's driver isn't running yet (a fresh install needs a "
                 "restart first).")

    apps = _hidhide_apps(ctx)
    if not apps and not state["inverse"]:
        # Hiding the pad while nothing is on the allow-list would hide it from
        # SteamlessInput as well  a dead controller instead of a fixed one.
        ctx.log("err", f"No SteamlessInput executable in {ctx.install_dir}, so "
                       f"hiding the controller would hide it from "
                       f"SteamlessInput too. Run this again with "
                       f'--dir "<the install folder>".')
        return False

    devices = _nintendo_devices()
    if not devices:
        # Nothing to hide yet: the pad has never been attached to this PC. Get
        # the application side in place and let the logon task catch the pad.
        ctx.log("info", "No Nintendo controller has ever been connected to "
                        "this PC, so there is nothing to hide yet.")

    have_apps = {p.upper() for p in state["apps"]}
    have_devs = {p.upper() for p in state["devices"]}

    cmd = []
    registered = []
    if state["inverse"]:
        # Inverse mode turns the application list into a BLOCK list, so
        # registering ourselves there would be the one thing guaranteed to
        # break us. Somebody else set that deliberately; leave it, and make
        # sure we are absent from the list instead.
        ctx.log("warn", "HidHide's application list is inverted (another tool "
                        "set that). Keeping SteamlessInput OFF the list, which "
                        "is what grants it access in that mode.")
        for app in apps:
            if app.upper() in have_apps:
                cmd += ["--app-unreg", app]
    else:
        for app in apps:
            if app.upper() not in have_apps:
                cmd += ["--app-reg", app]
                registered.append(app)

    added = [(p, label) for p, label in devices if p.upper() not in have_devs]
    for path, _label in added:
        cmd += ["--dev-hide", path]
    cloak_flip = not state["cloak"]
    if cloak_flip:
        cmd += ["--cloak-on"]

    if not cmd:
        if devices:
            ctx.log("ok", "Already set up  the controller is hidden from "
                          "games and SteamlessInput can still see it")
            _hidhide_task_delete()
        else:
            ctx.log("ok", "SteamlessInput is allowed to see hidden devices")
            _hidhide_defer(ctx, "No Nintendo controller is attached yet.")
        return True

    for _path, label in added:
        ctx.log("info", f"Hiding {label} from games")
    # One process for the whole transaction (rule 1 at the top of the HidHide
    # section) rather than a call per command.
    rc, out = _hidhide_run(cmd, timeout=180)
    if rc != 0:
        tail = " ".join((out or "").split())[-180:]
        ctx.log("err", f"HidHideCLI refused the change (exit {rc}). {tail}")
        return False

    after = _hidhide_read_state(refresh=True)
    if after is None:
        ctx.log("warn", "Wrote the HidHide configuration but could not read it "
                        "back to confirm.")
        return False

    added_paths = [p for p, _label in added]
    # Only what THIS run added  the uninstaller undoes our edits, not the
    # user's own entries that happened to already be there.
    _hh_state_save(devices=added_paths, apps=registered,
                   cloaked_by_us=cloak_flip)
    now_hidden = {p.upper() for p in after["devices"]}
    missed = [p for p in added_paths if p.upper() not in now_hidden]
    if missed:
        ctx.log("warn", f"{len(missed)} device(s) did not stay hidden  open "
                        f"HidHide Configuration Client to finish by hand.")
    if not after["cloak"]:
        ctx.log("warn", "Device hiding is still switched off in HidHide.")
        return False
    if added:
        ctx.log("ok", f"{len(added)} Nintendo controller(s) now hidden from "
                      f"games; SteamlessInput still sees them")
    elif devices:
        ctx.log("ok", "The controller stays hidden from games; "
                      "SteamlessInput still sees it")
    else:
        ctx.log("ok", "SteamlessInput is allowed to see hidden devices")
    # A pad that has never been attached can still turn up later; the logon
    # task picks it up once and then removes itself.
    if not devices:
        _hidhide_defer(ctx, "No Nintendo controller is attached yet.")
    else:
        _hidhide_task_delete()
    return True


def _undo_hidhide_setup(ctx):
    """Put back exactly what we changed  a controller left hidden after an
    uninstall would look broken in every game on the PC."""
    _hidhide_task_delete()
    if not _hidhide_cli():
        _hh_state_clear()
        return True
    state = _hidhide_read_state(refresh=True)
    if state is None:
        ctx.log("warn", "Could not reach HidHide  if your controller stays "
                        "hidden, untick 'Enable device hiding' in HidHide "
                        "Configuration Client.")
        return False

    mine = _hh_state_load()
    have_devs = {p.upper() for p in state["devices"]}
    have_apps = {p.upper() for p in state["apps"]}
    # Fall back to "every Nintendo device that is hidden" only when we have no
    # record of our own  an upgrade from a version that didn't keep one.
    devices = mine["devices"] or [p for p in state["devices"]
                                  if _is_nintendo_device(p)]
    apps = set(mine["apps"]) | set(_hidhide_apps(ctx))

    cmd = []
    for path in devices:
        if path.upper() in have_devs:
            cmd += ["--dev-unhide", path]
    for app in apps:
        if app.upper() in have_apps:
            cmd += ["--app-unreg", app]
    # Only flip the master switch back if we were the ones who flipped it AND
    # nothing else is relying on it  DS4Windows and friends share this driver.
    left = have_devs - {p.upper() for p in devices}
    if mine["cloaked_by_us"] and not left and state["cloak"]:
        cmd += ["--cloak-off"]

    if cmd:
        rc, out = _hidhide_run(cmd, timeout=180)
        if rc != 0:
            tail = " ".join((out or "").split())[-180:]
            ctx.log("warn", f"HidHideCLI exit {rc}. {tail}")
            return False
    _hh_state_clear()
    ctx.log("ok", "The controller is visible to games again")
    return True

def _step_relay(ctx):
    ok, src = _need_relay()
    if not ok:
        ctx.log("err", src)
        return False
    ps1 = _find(SRC_RELAY_PS1)
    if not ps1:
        ctx.log("err", "install_uia_relay.ps1 is missing from this download.")
        return False

    # The script expects the relay payload at <script dir>\dist\uia-relay.
    # A bundled setup exe has them in unrelated temp folders, so stage a
    # matching layout before calling it.
    staged = None
    want = os.path.join(os.path.dirname(ps1), "dist", "uia-relay")
    if os.path.normcase(os.path.abspath(src)) != os.path.normcase(want):
        staged = tempfile.mkdtemp(prefix="si-relay-")
        shutil.copy2(ps1, os.path.join(staged, os.path.basename(ps1)))
        shutil.copytree(src, os.path.join(staged, "dist", "uia-relay"))
        ps1 = os.path.join(staged, os.path.basename(ps1))

    ctx.log("info", "Installing the input relay (certificate, Program Files, "
                    "signature)...")
    rc, out = _run(["powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", ps1,
                    "-NoPause", "-ClientExe", ctx.app_exe], timeout=900)
    for line in out.splitlines():
        line = line.strip()
        if line:
            ctx.log("info", "  " + line)
    if staged:
        shutil.rmtree(staged, ignore_errors=True)

    if rc == 0 and _detect_relay():
        ctx.log("ok", "Input relay installed")
        return True
    ctx.log("warn", f"The relay installer exited with {rc}. SteamlessInput "
                    f"still works  it falls back to lizard mode on admin "
                    f"windows.")
    return False


def _undo_relay(ctx):
    dest = _relay_dest()
    _run(["taskkill", "/IM", "SteamlessInputRelay.exe", "/F"], timeout=30)
    if os.path.isdir(dest):
        try:
            shutil.rmtree(dest)
            ctx.log("ok", "Removed the input relay")
        except OSError as e:
            ctx.log("warn", f"Could not remove the relay: {e}")
            return False
    parent = os.path.dirname(dest)
    try:
        os.rmdir(parent)
    except OSError:
        pass
    # Drop the self-signed certificate the relay installer trusted machine-wide.
    _powershell(
        "foreach ($s in 'My','Root','TrustedPublisher') {"
        " Get-ChildItem \"Cert:\\LocalMachine\\$s\" -ErrorAction SilentlyContinue |"
        " Where-Object { $_.Subject -eq 'CN=SteamlessInput Input Relay' } |"
        " Remove-Item -Force -ErrorAction SilentlyContinue }", timeout=120)
    ctx.log("ok", "Removed the relay signing certificate")
    return True


def _step_lockscreen(ctx):
    ok, src = _need_lockscreen()
    if not ok:
        ctx.log("err", src)
        return False
    import winreg

    ctx.log("info", "Copying the lock-screen keyboard...")
    try:
        os.makedirs(LOCKSCREEN_DIR, exist_ok=True)
        dst = os.path.join(LOCKSCREEN_DIR, "LockScreenKeyboard.exe")
        shutil.copy2(src, dst)
    except OSError as e:
        ctx.log("err", f"Copy failed: {e}")
        return False

    ctx.log("info", "Adding a Defender exclusion...")
    _powershell(
        f"try {{ Add-MpPreference -ExclusionPath '{LOCKSCREEN_DIR}' -ErrorAction Stop }} catch {{}};"
        f"try {{ Add-MpPreference -ExclusionProcess '{dst}' -ErrorAction Stop }} catch {{}}",
        timeout=180)

    ctx.log("info", "Linking it to the lock screen's Ease of Access button...")
    try:
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, IFEO_UTILMAN) as k:
            winreg.SetValueEx(k, "Debugger", 0, winreg.REG_SZ, f'"{dst}"')
    except OSError as e:
        ctx.log("err", f"Registry write failed: {e}")
        return False

    if _detect_lockscreen():
        ctx.log("ok", "Lock-screen keyboard installed. Win+L → Ease of Access "
                      "(bottom-right) → type → R2 to sign in.")
        return True
    ctx.log("warn", "Defender may have reverted the change  run the wizard "
                    "once more.")
    return False


def _undo_lockscreen(ctx):
    import winreg
    try:
        with _reg_open(winreg.HKEY_LOCAL_MACHINE, IFEO_UTILMAN,
                       winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, "Debugger")
            except FileNotFoundError:
                pass
    except OSError:
        pass
    # Only drop the key itself when nothing else lives in it: Utilman.exe's
    # IFEO key can legitimately hold unrelated values (GlobalFlag, MitigationOptions).
    if _key_is_empty(winreg.HKEY_LOCAL_MACHINE, IFEO_UTILMAN):
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, IFEO_UTILMAN)
        except OSError:
            pass

    _run(["taskkill", "/IM", "LockScreenKeyboard.exe", "/F"], timeout=30)
    if os.path.isdir(LOCKSCREEN_DIR):
        try:
            shutil.rmtree(LOCKSCREEN_DIR)
        except OSError as e:
            ctx.log("warn", f"Could not remove {LOCKSCREEN_DIR}: {e}")
    _powershell(
        f"try {{ Remove-MpPreference -ExclusionPath '{LOCKSCREEN_DIR}' -ErrorAction Stop }} catch {{}}",
        timeout=180)
    ctx.log("ok", "Lock screen restored to normal")
    return True


LOCKSCREEN_WARNING = (
    "The lock-screen keyboard installs the well-known \"Utilman\" "
    "accessibility swap.\n\n"
    "That means ANYONE standing at your locked PC can open a SYSTEM-level "
    "keyboard before signing in. It is a real security trade-off, not a "
    "theoretical one.\n\n"
    "Only consider this on a private home PC you fully trust. Never on a "
    "laptop, a work machine, or a shared one.\n\n"
    "Uninstalling reverses every change."
)


def build_steps():
    """The component table, in display order."""
    return [
        Step("app", "SteamlessInput", "",
             _step_app, undo=_undo_app, required=True, needs=_need_app,
             uninstall_title="SteamlessInput and its settings"),

        Step("start_menu", "Start Menu shortcut", "",
             _step_startmenu, undo=_undo_startmenu,
             detect=lambda: os.path.isfile(_startmenu_lnk())),

        Step("desktop", "Desktop shortcut", "",
             _step_desktop, undo=_undo_desktop, default=False,
             detect=lambda: os.path.isfile(_desktop_lnk())),

        Step("autostart", "Start with Windows", "",
             _step_autostart, undo=_undo_autostart, detect=_detect_autostart),

        Step("vigem", "Gamepad Mode: ViGEmBus driver",
             "Emulates an Xbox 360 gamepad so your controller works with any "
             "game. Keyboard and desktop control still function without it, "
             "and it can be toggled on/off in the program's options.",
             _step_vigem, detect=_detect_vigem, skip_if_present=True,
             uninstall_default=False),

        Step("hidhide", "HidHide  Nintendo Switch Pro fix",
             "Only for the Switch Pro controller to get Gamepad Mode working. "
             "The switch pro controller spams phantom input (buttons 1–8) to "
             "fix this you need to isntall this driver",
             _step_hidhide, detect=_detect_hidhide, default=False,
             skip_if_present=True,
             uninstall_default=False),

        Step("hidhide_setup", "Hide the Nintendo controller from games",
             "Finishes the HidHide side for you: lets SteamlessInput see the "
             "controller, hides the physical pad from games so its phantom "
             "buttons stop firing, and switches device hiding on. This is the "
             "part you used to do by hand in HidHide Configuration Client.",
             _step_hidhide_setup, undo=_undo_hidhide_setup,
             detect=_detect_hidhide_setup, admin=True,
             requires=("hidhide",), auto=True,
             default=_nintendo_known, skip_if_present=True,
             uninstall_title="Hiding of the Nintendo controller",
             note="Only touches Nintendo pads (vendor 057E) and only the "
                  "entries it added, so anything else using HidHide is left "
                  "alone. A fresh HidHide install can only be configured "
                  "after a restart  the wizard then finishes it "
                  "automatically at your next sign-in."),

        Step("relay", "Keep working on administrator windows",
             "Keeps the controller working over elevated windows (Task "
             "Manager, installers, UAC prompts). Without it, the controller "
             "freezes, forcing you to grab a mouse and keyboard. Steam Input "
             "has the same problem, this optional input helper fixes that.",
             _step_relay, undo=_undo_relay, detect=_detect_relay,
             default=False, admin=True, needs=_need_relay,
             skip_if_present=True,
             note="Installs a signed helper in Program Files. The helper never "
                  "runs elevated, it only gets permission to drive the UI. It "
                  "can't read window contents or elevate further. Removed "
                  "cleanly on uninstall."),

        Step("lockscreen", "Lock-screen keyboard",
             "Type your Windows password with the SteamlessInput on-screen "
             "keyboard. This has a genuine security cost.",
             _step_lockscreen, undo=_undo_lockscreen, detect=_detect_lockscreen,
             default=False, admin=True, danger=True, needs=_need_lockscreen,
             skip_if_present=True,
             warning=LOCKSCREEN_WARNING,
             note="Fine for a trusted home PC; not for a laptop you carry or a "
                  "work/shared machine."),
    ]


# =============================================================================
# plan execution
# =============================================================================

def _expand_selection(steps, selected):
    """Close a chosen set over Step.requires.

    Selection is where dependencies belong, not execution: `execute()` runs
    exactly the keys it is handed (the post-reboot `--hidhide` run leans on
    that  it must configure HidHide without ever re-running winget), while
    every place a HUMAN picks components goes through here first."""
    by_key = {s.key: s for s in steps}
    out = set(selected)
    pending = list(out)
    while pending:
        step = by_key.get(pending.pop())
        for dep in (step.requires if step else ()):
            if dep in by_key and dep not in out:
                out.add(dep)
                pending.append(dep)
    return out


def _plan_admin_keys(steps, selected, install_dir):
    """Which selected steps must run elevated: the ones declared admin, plus
    the app copy itself when the chosen folder isn't user-writable."""
    keys = [s.key for s in steps if s.key in selected and s.admin]
    if "app" in selected and not _dir_writable(install_dir):
        keys.insert(0, "app")
    return keys


def _run_steps(steps, keys, ctx):
    """Run `keys` in table order. Returns (done, failed) key lists."""
    by_key = {s.key: s for s in steps}
    done, failed = [], []
    for key in keys:
        step = by_key.get(key)
        if step is None:
            continue
        ctx.log("step", step.title)
        try:
            ok = bool(step.run(ctx))
        except Exception as e:
            ctx.log("err", f"{step.title} failed: {e}")
            ok = False
        (done if ok else failed).append(key)
    return done, failed


def _run_undo(steps, keys, ctx):
    by_key = {s.key: s for s in steps}
    # Reverse table order: shortcuts and registry entries before the files
    # they point at, app folder last.
    ordered = [s.key for s in steps if s.key in keys]
    ordered.sort(key=lambda k: (k == "app",))
    done, failed = [], []
    for key in ordered:
        step = by_key.get(key)
        if step is None or step.undo is None:
            continue
        ctx.log("step", f"Removing: {step.uninstall_title}")
        try:
            ok = bool(step.undo(ctx))
        except Exception as e:
            ctx.log("err", f"{step.uninstall_title}: {e}")
            ok = False
        (done if ok else failed).append(key)
    return done, failed


# --- the elevated half -------------------------------------------------------

def _jsonl_log(path):
    """A log sink that appends JSON lines, flushed per line so the parent's
    tail sees them as they happen."""
    def sink(level, text):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"level": level, "text": text}) + "\n")
                f.flush()
        except OSError:
            pass
    return sink


def run_plan(plan_path):
    """Entry point for the elevated child (``--run-plan``)."""
    global _EXTRA_ROOT
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    hint = plan.get("payload")
    if hint and os.path.isdir(hint):
        _EXTRA_ROOT = hint
    log = _jsonl_log(plan["log"])
    ctx = Ctx(plan["install_dir"], log, plan.get("opts"))
    steps = build_steps()
    try:
        if plan.get("mode") == "uninstall":
            _done, failed = _run_undo(steps, plan["steps"], ctx)
        else:
            _done, failed = _run_steps(steps, plan["steps"], ctx)
    except Exception:
        log("err", traceback.format_exc().strip().splitlines()[-1])
        failed = plan["steps"]
    log("__end__", "1" if failed else "0")
    return 1 if failed else 0


def _elevated_phase(keys, install_dir, log, opts=None, mode="install"):
    """Hand `keys` to an elevated copy of this installer and mirror its log.

    Returns True when the child reported success."""
    tmp = tempfile.mkdtemp(prefix="si-setup-")
    plan_path = os.path.join(tmp, "plan.json")
    log_path = os.path.join(tmp, "log.jsonl")
    payload_hint = _self_dir()
    plan = {"steps": list(keys), "install_dir": install_dir,
            "log": log_path, "opts": opts or {}, "mode": mode,
            "payload": payload_hint}
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f)
    open(log_path, "w", encoding="utf-8").close()

    argv = _relaunch_argv(["--run-plan", plan_path])
    exe, params = argv[0], subprocess.list2cmdline(argv[1:])
    log("info", "Windows will ask for administrator permission now.")
    handle, err = _shell_execute_elevated(exe, params)
    if handle is None:
        if err == "cancelled":
            log("err", "Administrator permission was declined  the steps "
                       "that need it were skipped.")
        else:
            log("err", f"Could not start the elevated step ({err}).")
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    # Tail the child's log until it exits, then drain whatever it wrote in the
    # final instant before exiting.
    pos, result, draining = 0, None, False
    try:
        while True:
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                chunk = ""
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("level") == "__end__":
                    result = (rec.get("text") == "0")
                    continue
                log(rec.get("level", "info"), rec.get("text", ""))
            if result is not None:
                break
            if draining:
                break
            if _wait_process(handle, 150):
                draining = True      # one more read pass, then stop
    finally:
        code = _process_exit_code(handle)
        _close_handle(handle)
        shutil.rmtree(tmp, ignore_errors=True)
    if result is None:
        result = (code == 0)
    return result


def execute(steps, selected, install_dir, log, opts=None, mode="install"):
    """Run the whole plan: user-level steps here, admin steps in one elevated
    child. Returns (ok, failed_keys)."""
    ctx = Ctx(install_dir, log, opts)
    if mode == "uninstall":
        admin_keys = [s.key for s in steps
                      if s.key in selected and s.admin]
        if "app" in selected and os.path.isdir(install_dir) \
                and not _dir_writable(install_dir):
            admin_keys.append("app")
    else:
        admin_keys = _plan_admin_keys(steps, selected, install_dir)
    user_keys = [s.key for s in steps
                 if s.key in selected and s.key not in admin_keys]

    runner = _run_undo if mode == "uninstall" else _run_steps
    failed = []
    if user_keys:
        _d, f = runner(steps, user_keys, ctx)
        failed += f
    if admin_keys:
        if _is_admin():
            _d, f = runner(steps, admin_keys, ctx)
            failed += f
        else:
            if not _elevated_phase(admin_keys, install_dir, log, opts, mode):
                failed += admin_keys
    return (not failed), failed


# =============================================================================
# theme + tkinter wizard
# =============================================================================

BG = "#0e141b"          # window background (keybinds_picker's _BG)
PANEL = "#1b2838"       # header band
CARD = "#23262e"        # component cards
CARD_HI = "#2b2d33"     # card hover
FG = "#ced0d2"
MUTED = "#8b929a"
ACCENT = "#1a9fff"
ACCENT_DIM = "#14639e"
GREEN = "#5fd75f"
GOLD = "#d6ae51"
ROSE = "#d16190"
FIELD = "#44464d"
LINE = "#3a3d45"

FONT = "Segoe UI"


def _load_bundled_font():
    """Register Plus Jakarta Sans if the payload happens to carry it, so the
    wizard matches the app's type. Purely cosmetic  Segoe UI otherwise."""
    global FONT
    for root in _search_roots():
        p = os.path.join(root, "data", "fonts", "PlusJakartaSans-Regular.ttf")
        if os.path.isfile(p):
            try:
                if ctypes.windll.gdi32.AddFontResourceExW(p, 0x10, 0):
                    FONT = "Plus Jakarta Sans"
            except Exception:
                pass
            return


class Check(object):
    """A dark-theme checkbox drawn on a canvas.

    ttk's checkbutton indicator is a native control that ignores background
    colour on Windows, so it renders as a white square on this palette. 25
    lines of canvas beats fighting the theme engine."""

    SIZE = 20

    def __init__(self, parent, value=True, enabled=True, command=None):
        import tkinter as tk
        self.var = tk.BooleanVar(value=value)
        self.enabled = enabled
        self.command = command
        self.canvas = tk.Canvas(parent, width=self.SIZE, height=self.SIZE,
                                bg=parent["bg"], highlightthickness=0,
                                cursor="hand2" if enabled else "arrow")
        self.canvas.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e=None):
        if not self.enabled:
            return
        self.var.set(not self.var.get())
        self._draw()
        if self.command:
            self.command(self.var.get())

    def set(self, value):
        self.var.set(bool(value))
        self._draw()

    def get(self):
        return bool(self.var.get())

    def set_bg(self, color):
        self.canvas.configure(bg=color)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        on = self.var.get()
        s = self.SIZE
        if on:
            fill = ACCENT if self.enabled else ACCENT_DIM
            c.create_rectangle(2, 2, s - 2, s - 2, fill=fill, outline=fill)
            c.create_line(5, s // 2, s // 2 - 1, s - 6, fill="#ffffff", width=2)
            c.create_line(s // 2 - 1, s - 6, s - 5, 5, fill="#ffffff", width=2)
        else:
            c.create_rectangle(2, 2, s - 2, s - 2, fill=BG,
                               outline=FIELD if self.enabled else "#33363c",
                               width=2)


def _button(parent, text, command, primary=False, enabled=True):
    import tkinter as tk
    bg = ACCENT if primary else FIELD
    fg = "#ffffff" if primary else FG
    if not enabled:
        bg, fg = ("#1f3d55" if primary else "#2c2e34"), "#6b7078"
    b = tk.Button(parent, text=text, command=command if enabled else None,
                  relief="flat", bd=0, bg=bg, fg=fg,
                  activebackground=(ACCENT_DIM if primary else CARD_HI),
                  activeforeground=fg, font=(FONT, 10, "bold"),
                  padx=22, pady=9, cursor="hand2" if enabled else "arrow",
                  state=("normal" if enabled else "disabled"),
                  disabledforeground=fg)
    b._si_primary = primary
    b._si_command = command
    return b


def _set_enabled(btn, enabled):
    """Flip a _button() between live and inert IN PLACE.

    Destroying and re-packing would put it back at the end of its pack order 
    which silently reshuffles the footer (Next ends up left of Back)."""
    primary = getattr(btn, "_si_primary", False)
    if enabled:
        bg, fg = (ACCENT if primary else FIELD), ("#ffffff" if primary else FG)
    else:
        bg, fg = ("#1f3d55" if primary else "#2c2e34"), "#6b7078"
    btn.configure(bg=bg, fg=fg, disabledforeground=fg,
                  cursor=("hand2" if enabled else "arrow"),
                  state=("normal" if enabled else "disabled"),
                  command=(getattr(btn, "_si_command", None) if enabled
                           else None))


class Wizard(object):
    """The whole GUI. Pages are frames swapped inside one container."""

    W, H = 760, 610

    def __init__(self, mode="install", preset_dir=None):
        import tkinter as tk

        # Before the first window: see _set_dpi_awareness's docstring.
        _set_dpi_awareness()

        _load_bundled_font()
        self.tk = tk
        self.mode = mode
        self.steps = build_steps()
        self.selected = {}
        self.result_ok = None
        self.failed = []

        prev_dir, _prev_ver = _detect_installed()
        self.upgrade = bool(prev_dir and os.path.isdir(prev_dir))
        default_dir = preset_dir or prev_dir or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "Programs", APP_NAME)

        self.root = tk.Tk()
        _pin_tk_scaling(self.root)
        self.root.withdraw()
        self.root.title(f"{APP_NAME} Setup"
                        if mode == "install" else f"Uninstall {APP_NAME}")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self._set_icon()

        self.dir_var = tk.StringVar(value=default_dir)
        self.launch_var = tk.BooleanVar(value=True)
        self.keep_settings_var = tk.BooleanVar(value=True)

        for s in self.steps:
            self.selected[s.key] = s.initial_tick()
        # A default tick can imply another component  "hide the controller"
        # is meaningless without the driver it drives. Settle that HERE, in
        # the boxes the user is looking at, rather than silently at plan time.
        by_key = {s.key: s for s in self.steps}
        for key in _expand_selection(self.steps, {k for k, v in
                                                  self.selected.items() if v}):
            if not self.selected.get(key) and by_key[key].present() is not True:
                self.selected[key] = True

        self._build_chrome()
        self.pages = []
        self.page_index = 0
        if mode == "install":
            self.pages = [self._page_welcome, self._page_location,
                          self._page_components, self._page_review,
                          self._page_progress, self._page_done]
        else:
            self.pages = [self._page_uninstall_pick, self._page_progress,
                          self._page_done]
        self._show(0)
        self._center()
        self.root.deiconify()
        # Again now the window is really on screen: at __init__ time it was
        # still withdrawn, and Tk hadn't necessarily built the top-level
        # wrapper HWND that WM_SETICON has to target (verified: the icons read
        # back as unset when only the early call ran).
        self._set_icon()

    # --- chrome -------------------------------------------------------------

    def _set_icon(self):
        ico = _find(("data/images/app_icon.ico", "app_icon.ico"))
        if not ico:
            return
        # Same Win32 route the app itself uses (keybinds_picker's
        # _set_window_icon): Tk's iconbitmap() hands Windows ONE frame out of
        # the .ico and lets it rescale that to every size it needs, so the
        # TASKBAR button (which wants a large icon  32px at 100%, more when
        # scaled) got a stretched small frame and looked blurry. app_icon.ico
        # carries 16..256px frames, so LoadImage the EXACT sizes Windows asks
        # for and hand them over directly. ctypes restypes are c_void_p so the
        # 64-bit HICON/HWND handles aren't truncated.
        try:
            u = ctypes.windll.user32
            # GA_ROOT, not GetParent: Tk's winfo_id() is an INNER content
            # window whose parent chain depth isn't guaranteed, and GetParent
            # on an already-top-level HWND walks off to the desktop  which
            # silently ate the WM_SETICON (verified via WM_GETICON reading
            # back NULL). GA_ROOT lands on the real top-level either way.
            u.GetAncestor.restype = ctypes.c_void_p
            u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            u.LoadImageW.restype = ctypes.c_void_p
            u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                     ctypes.c_uint, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_uint]
            u.SendMessageW.restype = ctypes.c_void_p
            u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_void_p, ctypes.c_void_p]
            wid = self.root.winfo_id()
            hwnd = u.GetAncestor(wid, 2) or wid      # GA_ROOT = 2
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
            ICON_SMALL, ICON_BIG = 0, 1
            # SM_CXSMICON/CYSMICON = 49/50 ; SM_CXICON/CYICON = 11/12.
            cxs, cys = u.GetSystemMetrics(49), u.GetSystemMetrics(50)
            cx, cy = u.GetSystemMetrics(11), u.GetSystemMetrics(12)
            small = u.LoadImageW(None, ico, IMAGE_ICON, cxs, cys,
                                 LR_LOADFROMFILE)
            big = u.LoadImageW(None, ico, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
            if small:
                u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
            if big:
                u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            self._icon_handles = (small, big)   # keep the HICONs alive
            if small or big:
                return
        except Exception:
            pass
        try:
            self.root.iconbitmap(ico)
        except Exception:
            pass

    def _center(self):
        self.root.update_idletasks()
        w, h = self.W, self.H
        x = (self.root.winfo_screenwidth() - w) // 2
        y = max(0, (self.root.winfo_screenheight() - h) // 2 - 30)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_chrome(self):
        tk = self.tk
        self.header = tk.Frame(self.root, bg=PANEL, height=92)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.logo_img = None
        # Pre-downscaled at build time (PIL LANCZOS) to the exact 64px this
        # header displays at  tkinter's PhotoImage has no smooth resize, so
        # the old fallback (subsample(2,2) straight from the 256px source)
        # nearest-neighbor-decimated the art and came out soft/aliased no
        # matter how crisp the display itself was. Only reached for the
        # pre-sized file missing (e.g. a dev checkout without a fresh asset
        # regen)  a real build always carries it.
        logo64 = _find(("assets/SteamlessController_seethrough_64.png",
                        "SteamlessController_seethrough_64.png"))
        if logo64:
            try:
                self.logo_img = tk.PhotoImage(file=logo64)
            except Exception:
                self.logo_img = None
        if self.logo_img is None:
            logo = _find(("assets/SteamlessController_seethrough.png",
                          "SteamlessController_seethrough.png",
                          "data/images/app_icon.png"))
            if logo:
                try:
                    img = tk.PhotoImage(file=logo)
                    while img.width() > 64:
                        img = img.subsample(2, 2)
                    self.logo_img = img
                except Exception:
                    self.logo_img = None
        if self.logo_img is not None:
            tk.Label(self.header, image=self.logo_img, bg=PANEL).pack(
                side="left", padx=(22, 14))

        # fill="both", not "y": with only vertical fill the frame keeps the
        # extra horizontal space as padding on BOTH sides, which centres the
        # title instead of anchoring it to the logo.
        htext = tk.Frame(self.header, bg=PANEL)
        htext.pack(side="left", fill="both", expand=True,
                   padx=(0 if self.logo_img else 24, 0))
        self.h_title = tk.Label(htext, text=APP_NAME, bg=PANEL, fg="#ffffff",
                                font=(FONT, 17, "bold"), anchor="w")
        self.h_title.pack(anchor="w", pady=(24, 0))
        self.h_sub = tk.Label(htext, text="", bg=PANEL, fg=MUTED,
                              font=(FONT, 9), anchor="w")
        self.h_sub.pack(anchor="w")

        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x")

        # Footer BEFORE body. pack() hands out space in call order, so a body
        # packed first with expand=True claims everything a tall page asks for
        # and shoves the buttons off the bottom of a fixed-size window.
        self.footer = tk.Frame(self.root, bg=BG, height=64)
        self.footer.pack(fill="x", side="bottom")
        self.footer.pack_propagate(False)
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x", side="bottom")

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True)

        self.btn_cancel = _button(self.footer, "Cancel", self._cancel)
        self.btn_cancel.pack(side="left", padx=(22, 0), pady=13)
        self.btn_next = _button(self.footer, "Next", self._next, primary=True)
        self.btn_next.pack(side="right", padx=(0, 22), pady=13)
        self.btn_back = _button(self.footer, "Back", self._back)
        self.btn_back.pack(side="right", padx=(0, 10), pady=13)

    def _set_header(self, title, sub):
        self.h_title.configure(text=title)
        self.h_sub.configure(text=sub)

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _show(self, index):
        self.page_index = index
        self._clear_body()
        self.pages[index]()

    def _footer(self, next_text="Next", next_on=True, back_on=True,
                cancel_text="Cancel", cancel_on=True):
        self.btn_next.destroy()
        self.btn_back.destroy()
        self.btn_cancel.destroy()
        self.btn_cancel = _button(self.footer, cancel_text, self._cancel,
                                  enabled=cancel_on)
        self.btn_cancel.pack(side="left", padx=(22, 0), pady=13)
        self.btn_next = _button(self.footer, next_text, self._next,
                                primary=True, enabled=next_on)
        self.btn_next.pack(side="right", padx=(0, 22), pady=13)
        self.btn_back = _button(self.footer, "Back", self._back,
                                enabled=back_on)
        self.btn_back.pack(side="right", padx=(0, 10), pady=13)

    def _on(self, page):
        """True when `page` (an unbound-looking bound method) is the page being
        shown. Bound methods are recreated on every attribute access, so this
        must compare by equality  `is` would always be False."""
        return self.pages[self.page_index] == page

    def _next(self):
        if self._on(self._page_location) and not self._validate_dir():
            return
        if self._on(self._page_done):
            self._finish()
            return
        self._show(self.page_index + 1)

    def _back(self):
        if self.page_index > 0:
            self._show(self.page_index - 1)

    def _cancel(self):
        if self._on(self._page_progress):
            return
        self.root.destroy()

    def _finish(self):
        if self.mode == "install" and self.launch_var.get() and self.result_ok \
                is not False:
            exe = os.path.join(self.dir_var.get(), INSTALLED_EXE)
            if os.path.isfile(exe):
                try:
                    subprocess.Popen([exe], cwd=os.path.dirname(exe),
                                     close_fds=True)
                except OSError:
                    pass
        self.root.destroy()

    # --- reusable bits ------------------------------------------------------

    def _chosen_keys(self):
        """The ticked component keys as a set (self.selected holds every key,
        ticked or not, so it must never be passed around as one)."""
        return _expand_selection(self.steps,
                                 {k for k, v in self.selected.items() if v})

    def _h1(self, parent, text):
        return self.tk.Label(parent, text=text, bg=BG, fg="#ffffff",
                             font=(FONT, 14, "bold"), anchor="w",
                             justify="left")

    def _p(self, parent, text, color=FG, size=10, width=660):
        return self.tk.Label(parent, text=text, bg=BG, fg=color,
                             font=(FONT, size), anchor="w", justify="left",
                             wraplength=width)

    def _scroll_area(self):
        """A vertically scrolling frame. Returns the inner frame."""
        tk = self.tk
        outer = tk.Frame(self.body, bg=BG)
        outer.pack(fill="both", expand=True, padx=(22, 8), pady=(4, 8))
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                           troughcolor=BG, bg=FIELD, activebackground=MUTED,
                           bd=0, relief="flat", width=12,
                           highlightthickness=0)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def _resize(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win, width=canvas.winfo_width())
        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)

        def _wheel(e):
            # bind_all outlives the page, so the canvas may already be gone by
            # the time a later page sees a wheel event.
            if canvas.winfo_exists():
                canvas.yview_scroll(-1 * (e.delta // 120), "units")
        # Bind on the toplevel: the pointer is usually over a child label, and
        # per-widget wheel binds would miss every one of them.
        self.root.bind_all("<MouseWheel>", _wheel)
        return inner

    # --- pages --------------------------------------------------------------

    def _page_welcome(self):
        tk = self.tk
        self._set_header(f"{APP_NAME} Setup", "")
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="both", expand=True, padx=30, pady=26)

        self._h1(f, "Take full control of your PC with any gamepad").pack(
            anchor="w")
        self._p(f, "An open-source, easier-to-use Steam Input that turns any "
                   "gamepad into a Steam Controller with improved PC Controls, "
                   "Virtual Keyboard, Trackpad Gestures, and customizable "
                   "Virtual Menus without Steam running! Plus tools to "
                   "configure Steam & Big Picture for couch-console gaming.",
                color=FG).pack(anchor="w", pady=(10, 0))

        if self.upgrade:
            self._p(f, "An existing installation was found  this will update "
                       "it in place and keep your settings.",
                    color=GOLD, size=9).pack(anchor="w", pady=(18, 0))

        self._p(f, "Free software under the GNU GPL v3.0. Not affiliated with "
                   "Valve.",
                color=MUTED, size=9).pack(anchor="w", side="bottom")
        self._footer("Next", back_on=False)

    def _page_location(self):
        tk = self.tk
        self._set_header("Install location", "")
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="both", expand=True, padx=30, pady=26)

        self._h1(f, "Choose a folder").pack(anchor="w", pady=(0, 18))

        row = tk.Frame(f, bg=BG)
        row.pack(fill="x")
        entry = tk.Entry(row, textvariable=self.dir_var, bg=FIELD, fg=FG,
                         insertbackground=FG, relief="flat", bd=0,
                         font=(FONT, 10), highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=ACCENT)
        entry.pack(side="left", fill="x", expand=True, ipady=8, ipadx=8)
        _button(row, "Browse…", self._browse).pack(side="left", padx=(10, 0))

        self.loc_note = tk.Label(f, text="", bg=BG, fg=MUTED, font=(FONT, 9),
                                 anchor="w", justify="left", wraplength=660)
        self.loc_note.pack(anchor="w", pady=(14, 0))

        quick = tk.Frame(f, bg=BG)
        quick.pack(anchor="w", pady=(18, 0))
        tk.Label(quick, text="Quick picks:", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="left", padx=(0, 10))
        for label, path in (
                ("Per-user (recommended)",
                 os.path.join(os.environ.get("LOCALAPPDATA",
                                             os.path.expanduser("~")),
                              "Programs", APP_NAME)),
                ("Program Files",
                 os.path.join(os.environ.get("ProgramFiles",
                                             r"C:\Program Files"), APP_NAME)),
        ):
            tk.Button(quick, text=label, relief="flat", bd=0, bg=CARD, fg=FG,
                      activebackground=CARD_HI, activeforeground=FG,
                      font=(FONT, 9), padx=12, pady=5, cursor="hand2",
                      command=lambda p=path: self._set_dir(p)).pack(
                          side="left", padx=(0, 8))

        self.dir_var.trace_add("write", lambda *_a: self._refresh_loc_note())
        self._refresh_loc_note()
        self._footer("Next")

    def _set_dir(self, path):
        self.dir_var.set(path)

    def _browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(
            title="Choose the install folder",
            initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if d:
            d = os.path.normpath(d)
            # Picking "Programs" rather than a folder inside it would scatter
            # our files among everyone else's; append the app name unless it's
            # already the leaf.
            if os.path.basename(d).lower() != APP_NAME.lower():
                d = os.path.join(d, APP_NAME)
            self.dir_var.set(d)

    def _refresh_loc_note(self):
        path = self.dir_var.get().strip()
        if not path:
            self.loc_note.configure(text="Enter a folder.", fg=ROSE)
            return
        free = _free_space(path)
        writable = _dir_writable(path)
        parts = [f"Free space on that drive: {_human(free)}."]
        src = _find(SRC_APP_EXES)
        if src:
            try:
                parts.append(f"Needs about {_human(os.path.getsize(src))}.")
            except OSError:
                pass
        if not writable:
            parts.append("This folder needs administrator rights  the wizard "
                         "will ask for them once.")
        self.loc_note.configure(text=" ".join(parts),
                                fg=(GOLD if not writable else MUTED))

    def _validate_dir(self):
        path = self.dir_var.get().strip()
        if not path or not os.path.splitdrive(path)[0]:
            self._alert("Pick a folder",
                        "Enter a full path, for example "
                        r"C:\Users\you\Programs\SteamlessInput.")
            return False
        return True

    # --- components page ----------------------------------------------------

    def _page_components(self):
        tk = self.tk
        self._set_header("Choose components", "Tick only what you need")
        head = tk.Frame(self.body, bg=BG)
        head.pack(fill="x", padx=30, pady=(22, 6))
        self._h1(head, "What should be installed?").pack(anchor="w")
        self._p(head, "Everything except the app itself is optional. Anything "
                      "already on this PC is marked.").pack(anchor="w",
                                                            pady=(6, 0))

        inner = self._scroll_area()
        self.cards = {}
        for step in self.steps:
            self._component_card(inner, step)

        self._footer("Next")

    def _component_card(self, parent, step):
        tk = self.tk
        avail, why = step.available()
        present = step.present()
        enabled = avail and not step.required

        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="x", pady=(0, 8), padx=(0, 8))
        pad = tk.Frame(card, bg=CARD)
        pad.pack(fill="x", padx=16, pady=14)

        left = tk.Frame(pad, bg=CARD)
        left.pack(side="left", anchor="n", padx=(0, 14))
        chk = Check(left, value=self.selected.get(step.key, False),
                    enabled=enabled,
                    command=lambda v, s=step: self._toggle(s, v))
        chk.set_bg(CARD)
        chk.canvas.pack()
        self.cards[step.key] = chk

        right = tk.Frame(pad, bg=CARD)
        right.pack(side="left", fill="x", expand=True)

        title_row = tk.Frame(right, bg=CARD)
        title_row.pack(fill="x")
        tk.Label(title_row, text=step.title, bg=CARD,
                 fg=(ROSE if step.danger else "#ffffff"),
                 font=(FONT, 11, "bold"), anchor="w").pack(side="left")

        for text, color in self._badges(step, present, avail):
            tk.Label(title_row, text=f"  {text}  ", bg=CARD, fg=color,
                     font=(FONT, 8, "bold")).pack(side="left", padx=(8, 0))

        # A blank blurb means the title says it all  pack nothing rather than
        # an empty label, which would still claim its pady and leave the card
        # looking like it lost a line.
        if step.blurb:
            tk.Label(right, text=step.blurb, bg=CARD, fg=FG, font=(FONT, 9),
                     anchor="w", justify="left", wraplength=580).pack(
                         anchor="w", pady=(5, 0))
        note = step.note if avail else why
        if note:
            tk.Label(right, text=note, bg=CARD,
                     fg=(GOLD if not avail else MUTED), font=(FONT, 9),
                     anchor="w", justify="left", wraplength=580).pack(
                         anchor="w", pady=(5, 0))

        if not enabled:
            for w in (card, pad, left, right):
                w.configure(bg=CARD)

    def _badges(self, step, present, avail):
        out = []
        if step.required:
            out.append(("REQUIRED", ACCENT))
        if present is True:
            out.append(("ALREADY INSTALLED", GREEN))
        if not avail:
            out.append(("UNAVAILABLE", GOLD))
        if step.admin:
            out.append(("NEEDS ADMIN", GOLD))
        if step.danger:
            out.append(("⚠ NOT RECOMMENDED", ROSE))
        return out

    def _toggle(self, step, value):
        if value and step.danger and step.warning:
            if not self._confirm_danger(step):
                self.cards[step.key].set(False)
                self.selected[step.key] = False
                return
        self.selected[step.key] = value
        self._apply_deps(step, value)

    def _apply_deps(self, step, value):
        """Keep dependent tick-boxes honest, and VISIBLY so  silently fixing
        the selection behind the review page would make the wizard's "this is
        everything that will happen" list disagree with the boxes."""
        by_key = {s.key: s for s in self.steps}
        if value:
            for dep in step.requires:
                self._set_tick(by_key.get(dep), True)
            # ...and the other way: a component that exists only to finish
            # another one follows it in (see Step.auto).
            for other in self.steps:
                if (other.auto and step.key in other.requires
                        and other.present() is not True):
                    self._set_tick(other, True)
        else:
            for other in self.steps:
                if step.key in other.requires:
                    self._set_tick(other, False)

    def _set_tick(self, step, value):
        if step is None or step.required or self.selected.get(step.key) == value:
            return
        if value and not step.available()[0]:
            return
        self.selected[step.key] = value
        # cards only exists while a page with tick-boxes is up; the selection
        # itself is authoritative either way.
        chk = getattr(self, "cards", {}).get(step.key)
        if chk is not None:
            chk.set(value)

    def _confirm_danger(self, step):
        tk = self.tk
        top = tk.Toplevel(self.root)
        top.title("Are you sure?")
        top.configure(bg=BG)
        top.transient(self.root)
        top.resizable(False, False)
        top.grab_set()

        bar = tk.Frame(top, bg=ROSE, height=4)
        bar.pack(fill="x")
        f = tk.Frame(top, bg=BG)
        f.pack(fill="both", expand=True, padx=26, pady=22)
        tk.Label(f, text=step.title,
                 bg=BG, fg=ROSE, font=(FONT, 13, "bold"),
                 anchor="w").pack(anchor="w")
        tk.Label(f, text=step.warning, bg=BG, fg=FG, font=(FONT, 10),
                 anchor="w", justify="left", wraplength=460).pack(
                     anchor="w", pady=(12, 0))

        state = {"ok": False}
        agree_row = tk.Frame(f, bg=BG)
        agree_row.pack(anchor="w", pady=(18, 0))

        btns = tk.Frame(f, bg=BG)

        def accept():
            if not agree.get():
                return
            state["ok"] = True
            top.destroy()

        def on_agree(value):
            # The confirm button has to LOOK inert until the box is ticked 
            # a live-looking button that silently does nothing reads as a bug.
            ok_btn.configure(
                bg=(ACCENT if value else "#1f3d55"),
                fg=("#ffffff" if value else "#6b7078"),
                cursor=("hand2" if value else "arrow"))

        agree = Check(agree_row, value=False, command=on_agree)
        agree.set_bg(BG)
        agree.canvas.pack(side="left")
        tk.Label(agree_row, text="I understand the risk and want it anyway",
                 bg=BG, fg=FG, font=(FONT, 10)).pack(side="left", padx=(10, 0))

        btns.pack(fill="x", pady=(22, 0))
        _button(btns, "Cancel", top.destroy).pack(side="right")
        ok_btn = _button(btns, "Enable it", accept, primary=True,
                         enabled=False)
        ok_btn.configure(state="normal", command=accept)
        ok_btn.pack(side="right", padx=(0, 10))

        top.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - top.winfo_width()) // 2
        y = self.root.winfo_y() + 90
        top.geometry(f"+{x}+{y}")
        self.root.wait_window(top)
        return state["ok"]

    # --- review -------------------------------------------------------------

    def _page_review(self):
        tk = self.tk
        self._set_header("Ready to install", "")
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="both", expand=True, padx=30, pady=(22, 10))
        self._h1(f, "This is everything that will happen").pack(anchor="w")

        # _chosen_keys, not self.selected: a component pulled in as another
        # one's requirement is going to run, so it belongs on the list that
        # claims to be everything that will happen.
        keys = self._chosen_keys()
        chosen = [s for s in self.steps if s.key in keys]
        admin_keys = _plan_admin_keys(self.steps, self._chosen_keys(),
                                      self.dir_var.get())

        box = tk.Frame(f, bg=CARD)
        box.pack(fill="both", expand=True, pady=(14, 0))
        inner = tk.Frame(box, bg=CARD)
        inner.pack(fill="both", expand=True, padx=18, pady=16)

        tk.Label(inner, text=f"Install folder:  {self.dir_var.get()}", bg=CARD,
                 fg=FG, font=(FONT, 10, "bold"), anchor="w",
                 justify="left", wraplength=620).pack(anchor="w",
                                                      pady=(0, 10))
        for s in chosen:
            row = tk.Frame(inner, bg=CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="✓", bg=CARD, fg=GREEN,
                     font=(FONT, 10, "bold")).pack(side="left", padx=(0, 9))
            label = s.title
            if s.key in admin_keys:
                label += "   (administrator)"
            tk.Label(row, text=label, bg=CARD, fg=FG, font=(FONT, 10),
                     anchor="w").pack(side="left")
        skipped = [s for s in self.steps if s.key not in keys]
        if skipped:
            tk.Label(inner, text="Not installed:", bg=CARD, fg=MUTED,
                     font=(FONT, 9, "bold"), anchor="w").pack(anchor="w",
                                                              pady=(14, 4))
            tk.Label(inner, text=", ".join(s.title for s in skipped),
                     bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w",
                     justify="left", wraplength=620).pack(anchor="w")

        if admin_keys and not _is_admin():
            self._p(f, "Windows will ask for administrator permission once, "
                       "for the steps marked above. Everything else is done "
                       "as you.", color=GOLD, size=9).pack(anchor="w",
                                                           pady=(12, 0))
        self._footer("Install")

    # --- uninstall picker ---------------------------------------------------

    def _page_uninstall_pick(self):
        tk = self.tk
        prev_dir, _ver = _detect_installed()
        if prev_dir:
            self.dir_var.set(prev_dir)
        self._set_header(f"Uninstall {APP_NAME}", "Choose what to remove")

        head = tk.Frame(self.body, bg=BG)
        head.pack(fill="x", padx=30, pady=(22, 6))
        self._h1(head, "What should be removed?").pack(anchor="w")
        self._p(head, f"Only the parts you tick are touched. Folder: "
                      f"{self.dir_var.get()}").pack(anchor="w", pady=(6, 0))

        inner = self._scroll_area()
        self.cards = {}
        self.selected = {}
        for step in self.steps:
            if step.undo is None:
                continue
            # Don't offer to remove what isn't there: a detect that says False
            # unticks the row and badges it, so the list reflects this machine.
            present = step.present()
            on = step.uninstall_default and present is not False
            self.selected[step.key] = on
            card = tk.Frame(inner, bg=CARD)
            card.pack(fill="x", pady=(0, 8), padx=(0, 8))
            pad = tk.Frame(card, bg=CARD)
            pad.pack(fill="x", padx=16, pady=13)
            chk = Check(pad, value=on,
                        command=lambda v, s=step: self.selected.__setitem__(
                            s.key, v))
            chk.set_bg(CARD)
            chk.canvas.pack(side="left", padx=(0, 14))
            right = tk.Frame(pad, bg=CARD)
            right.pack(side="left", fill="x", expand=True)
            trow = tk.Frame(right, bg=CARD)
            trow.pack(fill="x")
            tk.Label(trow, text=step.uninstall_title, bg=CARD, fg="#ffffff",
                     font=(FONT, 11, "bold"), anchor="w").pack(side="left")
            if step.admin:
                tk.Label(trow, text="  NEEDS ADMIN  ", bg=CARD, fg=GOLD,
                         font=(FONT, 8, "bold")).pack(side="left", padx=(8, 0))
            if present is False:
                tk.Label(trow, text="  NOT PRESENT  ", bg=CARD, fg=MUTED,
                         font=(FONT, 8, "bold")).pack(side="left", padx=(8, 0))
            self.cards[step.key] = chk

        opt = tk.Frame(self.body, bg=BG)
        opt.pack(fill="x", padx=30, pady=(0, 10))
        keep = Check(opt, value=True,
                     command=lambda v: self.keep_settings_var.set(v))
        keep.set_bg(BG)
        keep.canvas.pack(side="left")
        tk.Label(opt, text="Keep my settings.json (bindings, profiles, "
                           "options)", bg=BG, fg=FG,
                 font=(FONT, 10)).pack(side="left", padx=(10, 0))

        tk.Label(self.body, text="ViGEmBus and HidHide are shared drivers "
                                 "other tools use  remove them from Apps & "
                                 "features if you want them gone.",
                 bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
                 justify="left", wraplength=680).pack(anchor="w", padx=30,
                                                      pady=(0, 12))
        self._footer("Uninstall", back_on=False)

    # --- progress -----------------------------------------------------------

    def _page_progress(self):
        tk = self.tk
        verb = "Installing" if self.mode == "install" else "Removing"
        self._set_header(f"{verb}…", "This should only take a moment")
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="both", expand=True, padx=30, pady=(22, 12))

        self.status = tk.Label(f, text="Starting…", bg=BG, fg="#ffffff",
                               font=(FONT, 12, "bold"), anchor="w")
        self.status.pack(anchor="w")

        self.bar_canvas = tk.Canvas(f, height=6, bg=CARD,
                                    highlightthickness=0, bd=0)
        self.bar_canvas.pack(fill="x", pady=(12, 16))
        self.bar_fill = self.bar_canvas.create_rectangle(
            0, 0, 0, 6, fill=ACCENT, outline=ACCENT)

        wrap = tk.Frame(f, bg=CARD)
        wrap.pack(fill="both", expand=True)
        self.logbox = tk.Text(wrap, bg=CARD, fg=FG, relief="flat", bd=0,
                              font=("Consolas", 9), wrap="word",
                              highlightthickness=0, padx=14, pady=12,
                              state="disabled", cursor="arrow")
        sb = tk.Scrollbar(wrap, orient="vertical", command=self.logbox.yview,
                          troughcolor=CARD, bg=FIELD, bd=0, relief="flat",
                          width=12, highlightthickness=0)
        self.logbox.configure(yscrollcommand=sb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("info", FG), ("ok", GREEN), ("warn", GOLD),
                           ("err", ROSE), ("step", "#ffffff")):
            self.logbox.tag_configure(tag, foreground=color)
        self.logbox.tag_configure("step", font=("Consolas", 9, "bold"))

        self._footer(next_text="Next", next_on=False, back_on=False,
                     cancel_on=False)
        self._start_work()

    def _log(self, level, text):
        """Thread-safe log sink  the worker calls this from its own thread."""
        self.root.after(0, self._log_ui, level, text)

    def _log_ui(self, level, text):
        if not self.logbox.winfo_exists():
            return
        self.logbox.configure(state="normal")
        prefix = {"step": "\n▸ ", "ok": "   ✓ ", "warn": "   ! ",
                  "err": "   ✗ "}.get(level, "   • ")
        self.logbox.insert("end", prefix + text + "\n", level)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        if level == "step":
            self.status.configure(text=text)
            self._steps_done += 1
            self._set_progress(self._steps_done / max(1, self._steps_total))

    def _set_progress(self, frac):
        w = max(1, self.bar_canvas.winfo_width())
        self.bar_canvas.coords(self.bar_fill, 0, 0, int(w * min(1.0, frac)), 6)

    def _start_work(self):
        chosen = self._chosen_keys()
        keys = [s.key for s in self.steps
                if s.key in chosen
                and (self.mode == "install" or s.undo is not None)]
        self._steps_total = max(1, len(keys))
        self._steps_done = 0
        opts = {"keep_settings": bool(self.keep_settings_var.get())}
        install_dir = os.path.normpath(self.dir_var.get().strip())

        def work():
            try:
                ok, failed = execute(self.steps, set(keys), install_dir,
                                     self._log, opts, self.mode)
            except Exception:
                self._log("err", traceback.format_exc().strip()
                          .splitlines()[-1])
                ok, failed = False, keys
            self.root.after(0, self._work_done, ok, failed)

        threading.Thread(target=work, daemon=True).start()

    def _work_done(self, ok, failed):
        self.result_ok = ok
        self.failed = failed
        self._set_progress(1.0)
        verb = "Installed" if self.mode == "install" else "Removed"
        self.status.configure(
            text="Done" if ok else "Finished with some steps skipped")
        self._set_header(verb if ok else "Finished",
                         "Everything ticked is in place" if ok
                         else "Some steps were skipped  see the log")
        _set_enabled(self.btn_next, True)

    # --- done ---------------------------------------------------------------

    def _page_done(self):
        tk = self.tk
        ok = self.result_ok is not False
        if self.mode == "install":
            self._set_header("All set" if ok else "Mostly done",
                             "SteamlessInput is ready")
        else:
            self._set_header("Uninstalled" if ok else "Mostly removed", "")
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="both", expand=True, padx=30, pady=26)

        if self.mode == "install":
            self._h1(f, "SteamlessInput is installed").pack(anchor="w")
            self._p(f, "It runs in the tray. Press X on the controller (or "
                       "Ctrl + Alt + K) to open the on-screen keyboard, and "
                       "hold ≡ on its own for ¾ of a second to switch between "
                       "desktop and gamepad control.").pack(anchor="w",
                                                            pady=(10, 0))
            if self.failed:
                names = ", ".join(s.title for s in self.steps
                                  if s.key in self.failed)
                self._p(f, f"These didn't complete: {names}. The log on the "
                           f"previous page says why  everything else is "
                           f"working.", color=GOLD, size=9).pack(anchor="w",
                                                                 pady=(14, 0))
            if not _detect_vigem():
                self._p(f, "ViGEmBus isn't installed, so Gamepad Mode stays "
                           "off. The keyboard and desktop controls work "
                           "regardless.", color=GOLD, size=9).pack(
                               anchor="w", pady=(10, 0))
            # The one thing this wizard genuinely cannot finish in one run: a
            # driver installed a minute ago has no live control device yet.
            if "hidhide_setup" in self._chosen_keys() and \
                    _hidhide_task_exists():
                self._p(f, "Restart Windows to finish hiding the controller "
                           "from games  HidHide's driver only wakes up after "
                           "a reboot, so the setup completes by itself the "
                           "next time you sign in.",
                        color=GOLD, size=9).pack(anchor="w", pady=(10, 0))

            row = tk.Frame(f, bg=BG)
            row.pack(anchor="w", pady=(24, 0))
            launch = Check(row, value=True,
                           command=lambda v: self.launch_var.set(v))
            launch.set_bg(BG)
            launch.canvas.pack(side="left")
            tk.Label(row, text="Launch SteamlessInput now", bg=BG, fg=FG,
                     font=(FONT, 10)).pack(side="left", padx=(10, 0))
        else:
            self._h1(f, "SteamlessInput has been removed").pack(anchor="w")
            if self.keep_settings_var.get():
                self._p(f, "Your settings.json was kept.").pack(anchor="w",
                                                                pady=(10, 0))

        self._footer("Finish", back_on=False, cancel_on=False)

    def _alert(self, title, text):
        from tkinter import messagebox
        messagebox.showwarning(title, text, parent=self.root)

    def run(self):
        self.root.mainloop()


# =============================================================================
# console flow (no GUI available, or scripted)
# =============================================================================

def _ensure_console():
    """Make sure console mode actually HAS somewhere to print.

    The setup exe is built --windowed, and PyInstaller's windowed bootloader
    sets sys.stdout/stderr/stdin to None  so `print` raises AttributeError and
    the run dies silently with no window to show it in. That breaks `--list`,
    `--console`, and the QuietUninstallString ("--uninstall --console --yes")
    that Apps & features invokes.

    Two separate problems, and the earlier version only solved one:
      1. NO console at all (launched from Explorer) -> allocate one. Attaching
         to the parent is tried first so a run from cmd/PowerShell prints into
         the window the user is already looking at.
      2. A console (or a pipe/file) IS inherited, but Python's stream objects
         are None regardless. AttachConsole then FAILS with ERROR_ACCESS_DENIED
         because we're already attached, and bailing out there left stdout None
          the actual bug. Rebuilding the streams is required in both cases.

    Streams are rebuilt from the inherited OS handles via msvcrt, not by
    opening CONOUT$, so `setup.exe --list > out.txt` and `| Out-String` still
    redirect properly instead of writing past the redirection to the console.
    """
    if not _frozen() or sys.platform != "win32":
        return
    k32 = ctypes.windll.kernel32
    ATTACH_PARENT_PROCESS = -1
    if not k32.GetConsoleWindow():
        if not k32.AttachConsole(ATTACH_PARENT_PROCESS):
            k32.AllocConsole()
    _bind_std_streams()


def _bind_std_streams():
    """Point sys.stdin/stdout/stderr at this process's real std handles."""
    import msvcrt

    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    invalid = ctypes.c_void_p(-1).value

    for name, nstd, oflag, mode in (("stdin", -10, os.O_RDONLY, "r"),
                                    ("stdout", -11, 0, "w"),
                                    ("stderr", -12, 0, "w")):
        cur = getattr(sys, name, None)
        try:
            if cur is not None and cur.fileno() >= 0:
                continue                       # already usable, leave it alone
        except (AttributeError, OSError, ValueError):
            pass
        handle = k32.GetStdHandle(nstd)
        if not handle or handle == invalid:
            continue
        try:
            fd = msvcrt.open_osfhandle(handle, oflag)
            setattr(sys, name, open(fd, mode, encoding="utf-8",
                                    errors="replace", buffering=1))
        except OSError:
            pass


def _console_safe_stdout():
    """A legacy console is cp437/cp1252; component blurbs are not. Never let an
    em-dash abort an install."""
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream, None)
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


class _NoInput(Exception):
    """Raised when the console flow needs an answer and stdin can't give one."""


def _ask(prompt):
    """input() that fails LOUDLY but cleanly on a closed/redirected stdin.

    Bare input() raises EOFError there and dumps a traceback  and in a
    --windowed build that traceback goes to a console the user may not even be
    looking at. Never fall back to "assume yes": an installer that starts
    installing because it couldn't read the answer is worse than one that
    stops."""
    try:
        return input(prompt)
    except (EOFError, OSError):
        raise _NoInput()


def _console_log(level, text):
    prefix = {"step": "\n== ", "ok": "   [ok]   ", "warn": "   [warn] ",
              "err": "   [FAIL] "}.get(level, "   - ")
    print(prefix + text, flush=True)


def console_flow(args):
    steps = build_steps()
    mode = "uninstall" if args.uninstall else "install"
    prev_dir, _ver = _detect_installed()
    install_dir = args.dir or prev_dir or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "Programs", APP_NAME)

    if args.with_:
        wanted = {k.strip() for k in args.with_.split(",") if k.strip()}
        unknown = wanted - {s.key for s in steps}
        if unknown:
            print(f"unknown component(s): {', '.join(sorted(unknown))}")
            print("valid: " + ", ".join(s.key for s in steps))
            return 2
        selected = {s.key for s in steps
                    if s.key in wanted or (s.required and mode == "install")}
    else:
        selected = set()
        print(f"\n  {APP_NAME} setup\n")
        for s in steps:
            if mode == "uninstall" and s.undo is None:
                continue
            avail, why = s.available()
            present = s.present()
            tags = []
            if s.required:
                tags.append("required")
            if present is True:
                tags.append("already installed")
            if s.admin:
                tags.append("admin")
            if s.danger:
                tags.append("SECURITY RISK")
            if not avail:
                tags.append("unavailable")
            tag = f"  [{', '.join(tags)}]" if tags else ""
            print(f"  {s.key:<12} {s.title}{tag}")
            if s.blurb:
                print(f"               {s.blurb}")
            if not avail:
                print(f"               {why}")
                continue
            if s.required and mode == "install":
                selected.add(s.key)
                continue
            default = (s.initial_tick() if mode == "install"
                       else s.uninstall_default)
            if args.yes:
                if s.danger:
                    print("               skipped  this one is never taken "
                          "by default; pass --with lockscreen to opt in")
                elif default:
                    selected.add(s.key)
                continue
            if s.danger:
                print("\n" + "\n".join("               " + ln
                                       for ln in (s.warning or "").splitlines()))
            ans = _ask(f"               install? "
                       f"[{'Y/n' if default else 'y/N'}] ").strip().lower()
            if (ans in ("y", "yes")) or (not ans and default):
                selected.add(s.key)
            print()

    if mode == "install":
        # --with app,hidhide_setup is a human selection too: pull in
        # whatever the chosen components can't work without.
        selected = _expand_selection(steps, selected)
        print(f"\n  Folder: {install_dir}")
    print(f"  Components: {', '.join(sorted(selected)) or '(none)'}\n")
    if not args.yes:
        if _ask("  Proceed? [Y/n] ").strip().lower() in ("n", "no"):
            return 1

    opts = {"keep_settings": not args.remove_settings}
    ok, failed = execute(steps, selected, install_dir, _console_log, opts, mode)
    print()
    if ok:
        print("  Done.")
    else:
        print(f"  Finished, but these were skipped: {', '.join(failed)}")
    return 0 if ok else 1


# =============================================================================
# --hidhide: the controller-hiding setup on its own
# =============================================================================

def _hidhide_log_path():
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, APP_NAME, "hidhide-setup.log")


def _file_log(path):
    """Log sink for the unattended run. The logon task runs as SYSTEM in
    session 0, where there is no console and no window to print into, so the
    only way to ever find out what it did is a file."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return lambda _level, _text: None

    def sink(level, text):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"[{level}] {text}" + chr(10))
        except OSError:
            pass
    return sink


def _message_box(text, warn=False):
    MB_ICONWARNING, MB_ICONINFORMATION = 0x30, 0x40
    try:
        ctypes.windll.user32.MessageBoxW(
            None, text, f"{APP_NAME} Setup",
            MB_ICONWARNING if warn else MB_ICONINFORMATION)
    except Exception:
        pass


def _hidhide_install_dir(args):
    """Which SteamlessInput.exe goes on HidHide's allow-list.

    The logon task is handed the folder explicitly (--dir) because it runs as
    SYSTEM, whose HKCU is not the user's  the Add/Remove Programs entry the
    wizard writes is invisible from there."""
    if args.dir:
        return args.dir
    # Beside us: the install folder (the wizard copies itself there) and also
    # the unpacked release folder, where the portable exe still has its
    # download name  someone running the setup exe from either wants the copy
    # they can see, not one recorded in the registry months ago.
    here = _self_dir()
    for name in (INSTALLED_EXE,) + tuple(SRC_APP_EXES):
        if os.path.isfile(os.path.join(here, name)):
            return here
    prev, _ver = _detect_installed()
    return prev or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "Programs", APP_NAME)


def hidhide_flow(args):
    """Run ONLY the hide-the-controller step.

    Two callers: the logon task this wizard schedules when HidHide's driver is
    too fresh to configure (``--quiet``), and a person who paired a NEW
    controller after installing and wants it hidden too. Deliberately does NOT
    expand Step.requires  it must never re-run the winget install (or open a
    browser) behind a scheduled task's back."""
    steps = build_steps()
    install_dir = _hidhide_install_dir(args)
    text_mode = bool(args.console) and not args.quiet
    sink = _file_log(_hidhide_log_path()) if args.quiet else None
    said = []

    def log(level, text):
        said.append((level, text))
        if sink is not None:
            sink(level, text)
        elif text_mode:
            _console_log(level, text)

    # No "step" line of our own: _run_steps prints the component title, and in
    # the elevated case it arrives through the child's log as well.
    ok, _failed = execute(steps, {"hidhide_setup"}, install_dir, log)
    if not args.quiet and not text_mode:
        body = "\n".join(t for level, t in said
                         if level in ("ok", "warn", "err"))
        _message_box(body or ("Done." if ok else "Nothing was changed."),
                     warn=not ok)
    return 0 if ok else 1


# =============================================================================
# entry point
# =============================================================================

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="SteamlessInput Setup",
        description="Install or remove SteamlessInput and its optional parts.",
        epilog="PowerShell note: this is a GUI-subsystem program, and "
               "PowerShell's '>' does not capture those, so redirecting the "
               ".exe gives an empty file. Run SteamlessInput-Setup.cmd "
               "instead (same arguments) when you want to capture output; "
               "pipes and cmd.exe redirection work with either.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uninstall", action="store_true",
                   help="remove an existing installation")
    p.add_argument("--console", action="store_true",
                   help="text-mode wizard instead of the GUI")
    p.add_argument("--yes", "-y", action="store_true",
                   help="console mode: accept the defaults, ask nothing")
    p.add_argument("--with", dest="with_", metavar="a,b,c",
                   help="console mode: install exactly these components "
                        "(see --list)")
    p.add_argument("--list", action="store_true",
                   help="print the component keys and exit")
    p.add_argument("--dir", help="install folder")
    p.add_argument("--remove-settings", action="store_true",
                   help="uninstall: delete settings.json too")
    p.add_argument("--hidhide", action="store_true",
                   help="only set up HidHide so games stop seeing the "
                        "Nintendo controller (asks for administrator)")
    p.add_argument("--quiet", action="store_true",
                   help=argparse.SUPPRESS)   # the post-reboot logon task
    p.add_argument("--run-plan", help=argparse.SUPPRESS)   # elevated child
    args = p.parse_args(argv)

    if args.run_plan:
        return run_plan(args.run_plan)

    # --quiet is the unattended logon task: no console, no window, file log.
    text_mode = (bool(args.console or args.yes or args.with_ or args.list)
                 and not args.quiet)
    if text_mode:
        _ensure_console()
    _console_safe_stdout()

    if sys.platform != "win32":
        print("This is the Windows installer. On Linux run linux/installer.py.")
        return 2

    if args.list:
        for s in build_steps():
            print(f"{s.key:<14} {s.title}")
        return 0

    if args.hidhide:
        return hidhide_flow(args)

    if text_mode:
        try:
            rc = console_flow(args)
        except _NoInput:
            print("\n  Nothing was installed: this run has no interactive "
                  "input to answer the questions.\n"
                  "  For unattended use pass --yes (defaults) or "
                  "--with app,autostart,... to choose explicitly.")
            return 2
        if not args.yes:
            try:
                input("\n  Press Enter to close ")
            except (EOFError, OSError):
                pass
        return rc

    try:
        import tkinter                     # noqa: F401
    except Exception:
        print("tkinter isn't available; falling back to the console wizard.")
        return console_flow(args)

    Wizard("uninstall" if args.uninstall else "install",
           preset_dir=args.dir).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
