# -*- coding: utf-8 -*-
"""SteamlessInput install wizard (Linux).

Mirror of ``windows/installer.py``: same pick-what-you-want model, same page
flow, same two-phase privilege split. What differs is everything underneath,
because the two platforms need entirely different things installed 

    Windows                         Linux
    -------                         -----
    ViGEmBus kernel driver          (nothing  uinput is in the kernel)
    HidHide, plus the wizard step    (nothing  no phantom-input problem, so
      that hides the Nintendo pad     nothing to hide and nothing to configure)
      from games for you
    uiAccess relay                  (nothing  no UIPI)
    lock-screen Utilman swap        (not ported)
    Start Menu / Startup .lnk       XDG .desktop entries
                                   GTK3 / AppIndicator tray libraries
                                   udev rules for controller + uinput access

The GTK/udev pieces are the Linux equivalents of "the app can't work until this
is set up", and they are exactly the steps people currently have to copy-paste
out of the README, so they are what this wizard is for.

Privilege split: everything under ``$HOME`` runs as the user, in-process. The
system-wide steps (distro packages, ``/etc/udev/rules.d``) are batched into ONE
``pkexec`` re-exec of this same binary with ``--run-plan``, which streams its
progress back through a JSONL log the parent tails  one password prompt, at a
point where the wizard has already said it is coming.

Run:
    python3 installer.py                  # the wizard
    python3 installer.py --uninstall
    python3 installer.py --console        # text mode (no tkinter needed)
    python3 installer.py --console --yes --with app,menu,autostart
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback


# --- HiDPI: point-size pin ----------------------------------------------------
# The wizard is laid out in hardcoded pixels (Wizard.W/H, fixed padding
# everywhere), same as keybinds_picker.py. Tk derives `tk scaling` from the
# display's reported dpi, which inflates every point-sized font past that fixed
# pixel geometry on a scaled display. Unlike windows/installer.py there's no
# Windows-style DPI-unaware bitmap-stretch to also fix here  X11/Wayland Tk
# already reports the real dpi without an explicit awareness call  so this is
# only the point-size half of that fix.
_DESIGN_DPI = 96.0
_TK_SCALING = _DESIGN_DPI / 72.0        # 1.3333 px per point


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
PROJECT_URL = "https://github.com/PietPetGit/SteamlessInput"

# The release tarball ships an extensionless ELF binary; keep that name.
INSTALLED_BIN = "SteamlessInput"
SETUP_COPY_NAME = "SteamlessInput-Setup"

SRC_BINS = ("SteamlessInput", "SteamlessInput-linux")
# The app is a --onedir build: this folder sits beside the binary and has to be
# installed with it (see _step_app). PyInstaller resolves it from the binary's
# directory, so the install-time rename to INSTALLED_BIN does not affect it.
APP_INTERNAL_DIR = "_internal"
SRC_ICONS = ("SteamlessInput.png", "data/images/app_icon.ico")
SRC_DOCS = ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")

# Same filename tray_linux.py uses, so the tray's own "Start at login" toggle
# and this wizard drive the SAME entry instead of fighting over two.
AUTOSTART_DESKTOP_NAME = "SteamlessInput.desktop"
MENU_DESKTOP_NAME = "SteamlessInput.desktop"

UDEV_RULES_PATH = "/etc/udev/rules.d/99-steamlessinput.rules"
UINPUT_RULES_PATH = "/etc/udev/rules.d/99-steamlessinput-uinput.rules"
UINPUT_MODULE_CONF = "/etc/modules-load.d/steamlessinput-uinput.conf"

# Valve VID 0x28DE covers every Steam Controller generation and the Deck
# (see steamcontroller/__init__.py); 0x057E is Nintendo (Switch Pro, Joy-Cons,
# the NSO pads). uaccess is how Valve's own steam-devices package does it:
# systemd-logind hands the device to whoever is logged in at the seat, so no
# group juggling and nothing world-readable.
UDEV_RULES = """\
# SteamlessInput  let the logged-in user talk to game controllers.
# Installed by the SteamlessInput setup wizard. Remove this file to undo.
#
# TAG+="uaccess" is the modern, seat-aware mechanism (systemd-logind grants the
# device to the active local session)  the same approach Valve's steam-devices
# package uses. No group membership and no re-login required.

# Valve  Steam Controller 2015 (1102 wired / 1142 dongle), Steam Controller
# 2026 "Triton" (1302 wired / 1304 puck), Steam Deck built-in (1205).
SUBSYSTEM=="usb", ATTRS{idVendor}=="28de", MODE="0660", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="28de", MODE="0660", TAG+="uaccess"

# Nintendo  Switch Pro, Joy-Cons, NSO and GameCube pads.
SUBSYSTEM=="usb", ATTRS{idVendor}=="057e", MODE="0660", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="057e", MODE="0660", TAG+="uaccess"
"""

UINPUT_RULES = """\
# SteamlessInput  writable /dev/uinput for the logged-in user.
# Installed by the SteamlessInput setup wizard. Remove this file to undo.
#
# Without this the app falls back to injecting through X11 (see
# steamcontroller/uinput.py), which Wayland compositors ignore. uinput events
# come from the kernel, so they work under Wayland, XWayland and X11 alike.
KERNEL=="uinput", SUBSYSTEM=="misc", MODE="0660", TAG+="uaccess", \\
    OPTIONS+="static_node=uinput"
"""

# Tray/notification stack per distro, from the README's dependency list.
DISTRO_PACKAGES = {
    "pacman": ["gtk3", "gobject-introspection", "libnotify",
               "libayatana-appindicator", "xorg-xwayland"],
    "apt": ["gir1.2-gtk-3.0", "gobject-introspection", "libnotify4",
            "gir1.2-ayatanaappindicator3-0.1", "xwayland"],
    "dnf": ["gtk3", "gobject-introspection", "libnotify",
            "libayatana-appindicator-gtk3", "xorg-x11-server-Xwayland"],
    "zypper": ["gtk3", "gobject-introspection", "libnotify4",
               "libayatana-appindicator3-1", "xwayland"],
}

DISTRO_INSTALL_CMD = {
    "pacman": ["pacman", "-S", "--needed", "--noconfirm"],
    "apt": ["apt-get", "install", "-y"],
    "dnf": ["dnf", "install", "-y"],
    "zypper": ["zypper", "--non-interactive", "install"],
}


# =============================================================================
# small helpers
# =============================================================================

def _frozen():
    return getattr(sys, "frozen", False)


def _self_path():
    return os.path.abspath(sys.executable if _frozen()
                           else os.path.abspath(__file__))


def _self_dir():
    return os.path.dirname(_self_path())


def _relaunch_argv(extra):
    if _frozen():
        return [_self_path()] + list(extra)
    return [sys.executable, os.path.abspath(__file__)] + list(extra)


_EXTRA_ROOT = None


def _search_roots():
    """Where payload files may live, most-specific first."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots += [os.path.join(meipass, "payload"), meipass]
    here = _self_dir()
    roots += [here, os.path.join(here, "dist"), os.path.dirname(here)]
    if _EXTRA_ROOT:
        roots += [_EXTRA_ROOT, os.path.join(_EXTRA_ROOT, "dist")]
    roots += [os.getcwd()]
    seen, out = set(), []
    for r in roots:
        k = os.path.abspath(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _find(names, want_dir=False):
    for root in _search_roots():
        for name in names:
            p = os.path.join(root, name)
            if (os.path.isdir(p) if want_dir else os.path.isfile(p)):
                return os.path.abspath(p)
    return None


def _is_root():
    return os.geteuid() == 0


def _real_user():
    """(name, uid, gid, home) of the human behind a pkexec/sudo re-exec.

    The elevated half still writes user-owned things  the app icon, the
    install-state file  and by then ``$HOME`` is ``/root`` and the XDG_* vars
    have been scrubbed. Everything user-facing has to be resolved against THIS,
    not against the effective uid, or a `/opt` install silently scatters files
    into root's home."""
    import pwd

    uid = os.environ.get("PKEXEC_UID")
    if uid is None and os.environ.get("SUDO_UID"):
        uid = os.environ.get("SUDO_UID")
    if uid is not None:
        try:
            pw = pwd.getpwuid(int(uid))
            return pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir
        except (KeyError, ValueError):
            pass
    name = os.environ.get("SUDO_USER")
    if name:
        try:
            pw = pwd.getpwnam(name)
            return pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir
        except KeyError:
            pass
    try:
        pw = pwd.getpwuid(os.getuid())
        return pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir
    except KeyError:
        return "root", os.getuid(), os.getgid(), os.path.expanduser("~")


def _user_home():
    return _real_user()[3]


def _xdg(var, default):
    """XDG dir, resolved for the INVOKING user rather than the effective one.

    The env var is only trusted when we aren't elevated: under pkexec it is
    either absent or root's."""
    if not _is_root():
        env = os.environ.get(var)
        if env:
            return env
    return os.path.join(_user_home(), default.replace("~/", "", 1))


def _chown_to_user(path):
    """Hand a file the root phase created back to the invoking user.

    Without this, a `/opt` install leaves root-owned files in the user's
    ~/.local, and the app  running as the user  can't rewrite them."""
    if not _is_root():
        return
    _name, uid, gid, _home = _real_user()
    if uid == 0:
        return
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def _default_install_dir():
    # ~/.local/bin is on PATH on every modern distro and needs no root; the app
    # is portable and keeps settings.json beside itself (tray_linux's
    # _settings_paths), so a user-owned folder also keeps settings writable.
    return os.path.normpath(os.path.join(_user_home(), ".local", "bin",
                                         "SteamlessInput"))


def _autostart_dir():
    return os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "autostart")


def _autostart_path():
    return os.path.join(_autostart_dir(), AUTOSTART_DESKTOP_NAME)


def _applications_dir():
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "applications")


def _menu_path():
    return os.path.join(_applications_dir(), MENU_DESKTOP_NAME)


def _icon_path():
    """Same path tray_linux._xdg_icon_path() writes, so the tray finds the icon
    the wizard installed instead of writing a second copy."""
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"),
                        "icons", "SteamlessInput.png")


def _dir_writable(path):
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent
    return os.access(probe, os.W_OK | os.X_OK)


def _free_space(path):
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        st = os.statvfs(probe)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def _human(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return "?"


def _run(cmd, timeout=None):
    try:
        p = subprocess.run(cmd, timeout=timeout, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


def _app_running():
    rc, out = _run(["pgrep", "-f", INSTALLED_BIN], timeout=15)
    return rc == 0 and out.strip() != ""


def _stop_app():
    _run(["pkill", "-f", INSTALLED_BIN], timeout=15)
    time.sleep(0.6)


def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def _package_manager():
    """(tool, packages, install-argv) for this distro, or (None, [], [])."""
    for tool in ("pacman", "apt-get", "dnf", "zypper"):
        if shutil.which(tool):
            key = "apt" if tool == "apt-get" else tool
            return key, DISTRO_PACKAGES[key], DISTRO_INSTALL_CMD[key]
    return None, [], []


def _write_root_file(path, contents, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    os.chmod(path, mode)


def _reload_udev(log):
    for cmd in (["udevadm", "control", "--reload-rules"],
                ["udevadm", "trigger", "--subsystem-match=usb"],
                ["udevadm", "trigger", "--subsystem-match=hidraw"],
                ["udevadm", "trigger", "--subsystem-match=misc"]):
        if shutil.which(cmd[0]):
            _run(cmd, timeout=60)
    log("info", "Reloaded udev rules  replug the controller to pick them up")


# =============================================================================
# component model  (mirrors windows/installer.py)
# =============================================================================

class Ctx(object):
    def __init__(self, install_dir, log, opts=None):
        self.install_dir = os.path.normpath(os.path.abspath(install_dir))
        self.log = log
        self.opts = opts or {}

    @property
    def app_bin(self):
        return os.path.join(self.install_dir, INSTALLED_BIN)


class Step(object):
    def __init__(self, key, title, blurb, run, *, undo=None, detect=None,
                 default=True, required=False, admin=False, danger=False,
                 needs=None, note=None, warning=None, uninstall_default=True,
                 uninstall_title=None, skip_if_present=False,
                 requires=(), auto=False):
        self.key = key
        self.title = title
        self.blurb = blurb
        self.run = run
        self.undo = undo
        self.detect = detect
        self.default = default        # bool, or a () -> bool probe
        self.required = required
        # Hard dependency: choosing this step also chooses `requires`, and
        # dropping one of those drops this. `auto` is the same edge the other
        # way  choosing what this needs also chooses this. Unused on Linux so
        # far (it is what pairs the Windows tree's HidHide driver with the
        # configuration that makes it do anything); kept here so both trees'
        # component model stays the same file with the same behaviour.
        self.requires = tuple(requires)
        self.auto = auto
        self.admin = admin
        self.danger = danger
        self.needs = needs
        self.note = note
        self.warning = warning
        self.uninstall_default = uninstall_default
        self.uninstall_title = uninstall_title or title
        self.skip_if_present = skip_if_present

    def initial_tick(self):
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

def _typelib_dirs():
    dirs = []
    for base in ("/usr/lib64/girepository-1.0", "/usr/lib/girepository-1.0",
                 "/usr/lib/x86_64-linux-gnu/girepository-1.0"):
        if os.path.isdir(base):
            dirs.append(base)
    return dirs


def _detect_deps():
    """True when the GTK3 + AppIndicator typelibs the tray needs are present.

    Checked as FILES, not as an `import gi`: the frozen build deliberately
    prepends the system girepository at runtime (see the Linux GI typelib fix),
    so what matters is whether those .typelib files exist on this machine, not
    whether this interpreter can import them."""
    dirs = _typelib_dirs()
    if not dirs:
        return False
    want = ("Gtk-3.0.typelib",)
    indicators = ("AyatanaAppIndicator3-0.1.typelib",
                  "AppIndicator3-0.1.typelib")
    have_gtk = any(os.path.isfile(os.path.join(d, n))
                   for d in dirs for n in want)
    have_ind = any(os.path.isfile(os.path.join(d, n))
                   for d in dirs for n in indicators)
    return bool(have_gtk and have_ind)


def _detect_udev():
    return os.path.isfile(UDEV_RULES_PATH)


def _detect_uinput():
    if os.path.isfile(UINPUT_RULES_PATH):
        return True
    # The rule may be absent yet the node already writable (some distros ship
    # an equivalent), which is what actually matters.
    return os.path.exists("/dev/uinput") and os.access("/dev/uinput", os.W_OK)


def _detect_autostart():
    return os.path.isfile(_autostart_path())


def _detect_menu():
    return os.path.isfile(_menu_path())


def _state_path():
    """Where the wizard records what it installed. Linux has no Add/Remove
    Programs registry to read back, so keep a tiny JSON of our own."""
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"),
                        "SteamlessInput", "install-state.json")


def _read_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data):
    try:
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        _chown_to_user(os.path.dirname(_state_path()))
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _chown_to_user(_state_path())
        return True
    except OSError:
        return False


def _detect_installed():
    """(install_dir, version) of a previous wizard install, or (None, None)."""
    st = _read_state()
    d = st.get("install_dir")
    if d and os.path.isdir(d):
        return d, st.get("version")
    return None, None


# --- payload availability ----------------------------------------------------

def _need_app():
    p = _find(SRC_BINS)
    if p and not os.path.isdir(os.path.join(os.path.dirname(p),
                                            APP_INTERNAL_DIR)):
        # A binary with no _internal/ beside it is half a download, not an app.
        return False, (f"{os.path.basename(p)} is here but its "
                       f"{APP_INTERNAL_DIR}/ folder is missing. Unpack the "
                       f"whole tarball and keep the files together.")
    if p:
        return True, p
    return False, ("The SteamlessInput binary was not found next to this "
                   "installer. Keep the release tarball's files together.")


def _need_deps():
    tool, pkgs, _cmd = _package_manager()
    if tool:
        return True, ""
    return False, ("No supported package manager found (pacman / apt / dnf / "
                   "zypper). Install the GTK3 and AppIndicator packages by "
                   "hand  see the README.")


# --- steps -------------------------------------------------------------------

def _install_icon(ctx):
    """Write the app icon where tray_linux._xdg_icon_path() expects it."""
    dst = _icon_path()
    src = _find(SRC_ICONS)
    if not src:
        return None
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _chown_to_user(os.path.dirname(dst))
        if src.endswith(".png"):
            shutil.copy2(src, dst)
        else:
            # Only the .ico is in the payload: convert if Pillow happens to be
            # around, otherwise skip  a missing icon costs a generic launcher
            # glyph, nothing more, and the tray writes one on first run anyway.
            try:
                from PIL import Image
                img = Image.open(src)
                sizes = sorted(img.info.get("sizes", set()))
                if sizes:
                    img.size = max(sizes)
                    img.load()
                img.convert("RGBA").save(dst, "PNG")
            except Exception:
                return None
        _chown_to_user(dst)
        return dst
    except OSError:
        return None


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

    dst = ctx.app_bin
    try:
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
    except OSError as e:
        ctx.log("err", f"Copy failed: {e}")
        return False

    # The app is a PyInstaller --onedir build: the binary is inert without the
    # `_internal/` folder shipped beside it, and PyInstaller finds that folder
    # from the binary's DIRECTORY, not its name  so the install-time rename is
    # fine as long as _internal/ lands next to it. Replaced wholesale rather
    # than merged, so an upgrade can't leave a previous version's stray module
    # behind to be imported. Mirrors windows/installer.py's _step_app.
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
        ctx.log("err", f"{APP_INTERNAL_DIR}/ was not found next to "
                       f"{os.path.basename(src)}. Keep the tarball's files "
                       f"together and run the installer again.")
        return False
    ctx.log("ok", f"Installed {INSTALLED_BIN} to {ctx.install_dir}")

    for name in SRC_DOCS:
        p = _find((name,))
        if p and os.path.abspath(p) != os.path.join(ctx.install_dir, name):
            try:
                shutil.copy2(p, os.path.join(ctx.install_dir, name))
            except OSError:
                pass

    if _install_icon(ctx):
        ctx.log("ok", "Installed the app icon")

    if _frozen():
        copy = os.path.join(ctx.install_dir, SETUP_COPY_NAME)
        try:
            if os.path.abspath(_self_path()) != copy:
                shutil.copy2(_self_path(), copy)
                os.chmod(copy, 0o755)
        except OSError as e:
            ctx.log("warn", f"Could not save the uninstaller: {e}")

    st = _read_state()
    st.update({"install_dir": ctx.install_dir, "version": SETUP_VERSION})
    _write_state(st)

    if not _on_path(ctx.install_dir):
        ctx.log("warn", f"{ctx.install_dir} is not on your PATH  launch it "
                        f"from the menu entry, or add it to PATH.")
    return True


def _on_path(d):
    real = os.path.realpath(d)
    return any(os.path.realpath(p) == real
               for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _undo_app(ctx):
    if _app_running():
        ctx.log("info", "Closing SteamlessInput...")
        _stop_app()

    keep = ctx.opts.get("keep_settings", True)
    removed_any = kept = False
    d = ctx.install_dir
    if os.path.isdir(d):
        for name in os.listdir(d):
            if keep and name == "settings.json":
                kept = True
                continue
            p = os.path.join(d, name)
            if os.path.abspath(p) == os.path.abspath(_self_path()):
                continue          # can't remove the binary we're running
            try:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
                removed_any = True
            except OSError as e:
                ctx.log("warn", f"Could not remove {name}: {e}")
        try:
            os.rmdir(d)
        except OSError:
            pass
    for p in (_icon_path(), _state_path()):
        try:
            os.remove(p)
        except OSError:
            pass
    ctx.log("ok", "Removed the app files" if removed_any
            else "App files were already gone")
    if kept:
        ctx.log("info", f"Kept your settings.json in {d}")
    return True


def _desktop_entry(exec_path, icon):
    icon_line = f"Icon={icon}\n" if icon else ""
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=Control your PC with any gamepad\n"
            f"Exec={exec_path}\n"
            f"{icon_line}"
            "Terminal=false\n"
            "Categories=Utility;\n")


def _step_menu(ctx):
    icon = _icon_path() if os.path.isfile(_icon_path()) else None
    try:
        os.makedirs(_applications_dir(), exist_ok=True)
        with open(_menu_path(), "w", encoding="utf-8") as f:
            f.write(_desktop_entry(ctx.app_bin, icon))
        os.chmod(_menu_path(), 0o644)
    except OSError as e:
        ctx.log("warn", f"Could not write the menu entry: {e}")
        return False
    if shutil.which("update-desktop-database"):
        _run(["update-desktop-database", _applications_dir()], timeout=60)
    ctx.log("ok", "Added an application-menu entry")
    return True


def _undo_menu(ctx):
    return _remove_file(ctx, _menu_path(), "application-menu entry")


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
    """Write the SAME file tray_linux._apply_autostart() writes, so the tray's
    own "Start at login" toggle reads back as ON and toggling it off removes
    this entry rather than leaving a duplicate behind."""
    icon = _icon_path() if os.path.isfile(_icon_path()) else None
    icon_line = f"Icon={icon}\n" if icon else ""
    contents = ("[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_NAME}\n"
                f"Exec={ctx.app_bin}\n"
                f"{icon_line}"
                "X-GNOME-Autostart-enabled=true\n"
                "Terminal=false\n"
                "Categories=Utility;\n")
    try:
        os.makedirs(_autostart_dir(), exist_ok=True)
        with open(_autostart_path(), "w", encoding="utf-8") as f:
            f.write(contents)
        os.chmod(_autostart_path(), 0o644)
    except OSError as e:
        ctx.log("warn", f"Could not write the autostart entry: {e}")
        return False
    ctx.log("ok", "SteamlessInput will start at login")
    return True


def _undo_autostart(ctx):
    return _remove_file(ctx, _autostart_path(), "autostart entry")


# --- root-phase steps --------------------------------------------------------

def _step_deps(ctx):
    tool, pkgs, argv = _package_manager()
    if not tool:
        ctx.log("err", _need_deps()[1])
        return False
    ctx.log("info", f"Installing tray libraries with {tool}: "
                    f"{' '.join(pkgs)}")
    if tool == "apt":
        _run(["apt-get", "update"], timeout=600)
    rc, out = _run(argv + pkgs, timeout=1800)
    for line in out.splitlines()[-12:]:
        if line.strip():
            ctx.log("info", "  " + line.strip())
    if rc == 0:
        ctx.log("ok", "Tray libraries installed")
        return True
    ctx.log("warn", f"{tool} exited with {rc}. The app still runs; only the "
                    f"tray icon and notifications need these.")
    return False


def _step_udev(ctx):
    try:
        _write_root_file(UDEV_RULES_PATH, UDEV_RULES)
    except OSError as e:
        ctx.log("err", f"Could not write {UDEV_RULES_PATH}: {e}")
        return False
    ctx.log("ok", f"Wrote {UDEV_RULES_PATH}")
    _reload_udev(ctx.log)
    return True


def _undo_udev(ctx):
    ok = _remove_file(ctx, UDEV_RULES_PATH, "controller udev rule")
    if ok:
        _reload_udev(ctx.log)
    return ok


def _step_uinput(ctx):
    try:
        _write_root_file(UINPUT_RULES_PATH, UINPUT_RULES)
        # Load it now and at every boot: the module is usually built but not
        # auto-loaded, and a udev rule for a node that doesn't exist does
        # nothing at all.
        _write_root_file(UINPUT_MODULE_CONF, "uinput\n")
    except OSError as e:
        ctx.log("err", f"Could not write the uinput rule: {e}")
        return False
    if shutil.which("modprobe"):
        _run(["modprobe", "uinput"], timeout=60)
    ctx.log("ok", f"Wrote {UINPUT_RULES_PATH} and loaded the uinput module")
    _reload_udev(ctx.log)
    return True


def _undo_uinput(ctx):
    ok = _remove_file(ctx, UINPUT_RULES_PATH, "uinput udev rule")
    _remove_file(ctx, UINPUT_MODULE_CONF, "uinput module-load entry")
    if ok:
        _reload_udev(ctx.log)
    return ok


def build_steps():
    """The component table, in display order."""
    return [
        Step("app", APP_NAME, "",
             _step_app, undo=_undo_app, required=True, needs=_need_app,
             uninstall_title=f"{APP_NAME} and its settings"),

        Step("menu", "Application-menu entry", "",
             _step_menu, undo=_undo_menu, detect=_detect_menu),

        Step("autostart", "Start at login", "",
             _step_autostart, undo=_undo_autostart, detect=_detect_autostart),

        Step("deps", "Tray libraries (GTK3 / AppIndicator)",
             "The tray icon and desktop notifications need GTK3, "
             "gobject-introspection, libnotify and AppIndicator. These are "
             "system packages, not bundled in the binary.",
             _step_deps, detect=_detect_deps, admin=True, needs=_need_deps,
             skip_if_present=True,
             note="Also pulls in XWayland, which the input path uses on "
                  "Wayland sessions. Not removed by this uninstaller  other "
                  "apps use these.",
             uninstall_default=False),

        Step("udev", "Controller access (udev rule)",
             "Lets you read the controller without root. Without it a Steam "
             "Controller or Switch Pro is only reachable as root, so the app "
             "reports no controller found.",
             _step_udev, undo=_undo_udev, detect=_detect_udev, admin=True,
             skip_if_present=True,
             note=f"Writes {UDEV_RULES_PATH}, using uaccess  the same "
                  f"mechanism Valve's steam-devices package uses."),

        Step("uinput", "Wayland input (uinput rule)",
             "Makes /dev/uinput writable so key and mouse events are injected "
             "at the kernel level. Without it input goes through X11, which "
             "Wayland-native windows ignore.",
             _step_uinput, undo=_undo_uinput, detect=_detect_uinput,
             admin=True, skip_if_present=True,
             note=f"Writes {UINPUT_RULES_PATH} and loads the uinput module at "
                  f"boot."),
    ]


# =============================================================================
# plan execution  (mirrors windows/installer.py)
# =============================================================================

def _expand_selection(steps, selected):
    """Close a chosen set over Step.requires.

    Selection is where dependencies belong, not execution: `execute()` runs
    exactly the keys it is handed, while every place a HUMAN picks components
    goes through here first."""
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
    keys = [s.key for s in steps if s.key in selected and s.admin]
    if "app" in selected and not _dir_writable(install_dir):
        keys.insert(0, "app")
    return keys


def _run_steps(steps, keys, ctx):
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


def _jsonl_log(path):
    def sink(level, text):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"level": level, "text": text}) + "\n")
                f.flush()
        except OSError:
            pass
    return sink


def run_plan(plan_path):
    """Entry point for the pkexec'd child (``--run-plan``)."""
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


def _elevator():
    """(argv-prefix, human name) for the root escalation available here."""
    if shutil.which("pkexec"):
        return ["pkexec"], "pkexec"
    if shutil.which("sudo"):
        # -n first so a cached credential works without a prompt we can't show
        # from a GUI; the console flow falls back to an interactive sudo.
        return ["sudo"], "sudo"
    return None, None


def _elevated_phase(keys, install_dir, log, opts=None, mode="install"):
    tmp = tempfile.mkdtemp(prefix="si-setup-")
    os.chmod(tmp, 0o777)          # the root child writes the log back in here
    plan_path = os.path.join(tmp, "plan.json")
    log_path = os.path.join(tmp, "log.jsonl")
    plan = {"steps": list(keys), "install_dir": install_dir,
            "log": log_path, "opts": opts or {}, "mode": mode,
            "payload": _self_dir()}
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f)
    os.chmod(plan_path, 0o644)
    open(log_path, "w", encoding="utf-8").close()
    os.chmod(log_path, 0o666)

    prefix, name = _elevator()
    argv = _relaunch_argv(["--run-plan", plan_path])
    if not prefix:
        log("err", "Neither pkexec nor sudo is available. Run this by hand:")
        log("info", "  sudo " + " ".join(argv))
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    log("info", f"Asking for administrator rights via {name}...")
    try:
        proc = subprocess.Popen(prefix + argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
    except OSError as e:
        log("err", f"Could not start the elevated step: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    pos, result = 0, None
    draining = False
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
        if result is not None or draining:
            break
        if proc.poll() is not None:
            draining = True       # one more read pass, then stop
        else:
            time.sleep(0.15)

    rc = proc.wait()
    err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip() \
        if proc.stderr else ""
    shutil.rmtree(tmp, ignore_errors=True)
    if result is None:
        # 126 == the polkit/sudo prompt was dismissed.
        if rc == 126:
            log("err", "Administrator rights were declined  the steps that "
                       "need them were skipped.")
        elif err:
            log("err", err.splitlines()[-1])
        result = (rc == 0)
    return result


def execute(steps, selected, install_dir, log, opts=None, mode="install"):
    ctx = Ctx(install_dir, log, opts)
    if mode == "uninstall":
        admin_keys = [s.key for s in steps if s.key in selected and s.admin]
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
        if _is_root():
            _d, f = runner(steps, admin_keys, ctx)
            failed += f
        else:
            if not _elevated_phase(admin_keys, install_dir, log, opts, mode):
                failed += admin_keys
    return (not failed), failed


# =============================================================================
# theme + tkinter wizard  (mirrors windows/installer.py)
# =============================================================================

BG = "#0e141b"
PANEL = "#1b2838"
CARD = "#23262e"
CARD_HI = "#2b2d33"
FG = "#ced0d2"
MUTED = "#8b929a"
ACCENT = "#1a9fff"
ACCENT_DIM = "#14639e"
GREEN = "#5fd75f"
GOLD = "#d6ae51"
ROSE = "#d16190"
FIELD = "#44464d"
LINE = "#3a3d45"

# No Segoe UI here; DejaVu Sans is the one family present on essentially every
# desktop Linux, and Tk falls back to it anyway.
FONT = "DejaVu Sans"


class Check(object):
    """Dark-theme checkbox drawn on a canvas (ttk's indicator ignores the
    palette on several GTK themes, exactly as it does on Windows)."""

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
                  highlightthickness=0, disabledforeground=fg)
    b._si_primary = primary
    b._si_command = command
    return b


def _set_enabled(btn, enabled):
    """Flip a _button() between live and inert IN PLACE  destroying and
    re-packing would reshuffle the footer order."""
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
    W, H = 760, 610

    def __init__(self, mode="install", preset_dir=None):
        import tkinter as tk

        self.tk = tk
        self.mode = mode
        self.steps = build_steps()
        self.selected = {}
        self.result_ok = None
        self.failed = []

        prev_dir, _v = _detect_installed()
        self.upgrade = bool(prev_dir and os.path.isdir(prev_dir))
        default_dir = preset_dir or prev_dir or _default_install_dir()

        self.root = tk.Tk()
        _pin_tk_scaling(self.root)
        self.root.withdraw()
        self.root.title(f"{APP_NAME} Setup" if mode == "install"
                        else f"Uninstall {APP_NAME}")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self._set_icon()

        self.dir_var = tk.StringVar(value=default_dir)
        self.launch_var = tk.BooleanVar(value=True)
        self.keep_settings_var = tk.BooleanVar(value=True)

        for s in self.steps:
            self.selected[s.key] = s.initial_tick()
        # A default tick can imply another component. Settle that HERE, in the
        # boxes the user is looking at, rather than silently at plan time.
        by_key = {s.key: s for s in self.steps}
        for key in _expand_selection(self.steps, {k for k, v in
                                                  self.selected.items() if v}):
            if not self.selected.get(key) and by_key[key].present() is not True:
                self.selected[key] = True

        self._build_chrome()
        if mode == "install":
            self.pages = [self._page_welcome, self._page_location,
                          self._page_components, self._page_review,
                          self._page_progress, self._page_done]
        else:
            self.pages = [self._page_uninstall_pick, self._page_progress,
                          self._page_done]
        self.page_index = 0
        self._show(0)
        self._center()
        self.root.deiconify()

    def _set_icon(self):
        png = _find(SRC_ICONS[:1]) or (_icon_path()
                                       if os.path.isfile(_icon_path()) else None)
        if png:
            try:
                self._icon_img = self.tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon_img)
            except Exception:
                pass

    def _center(self):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - self.W) // 2
        y = max(0, (self.root.winfo_screenheight() - self.H) // 2 - 30)
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

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
                          "SteamlessInput.png"))
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

        # Footer BEFORE body  a body packed first with expand=True claims
        # everything a tall page asks for and pushes the buttons off-window.
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
        for b in (self.btn_next, self.btn_back, self.btn_cancel):
            b.destroy()
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
        """Bound methods are rebuilt on every attribute access, so page identity
        must be compared with == rather than `is`."""
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
        if self.mode == "install" and self.launch_var.get() \
                and self.result_ok is not False:
            binp = os.path.join(self.dir_var.get(), INSTALLED_BIN)
            if os.path.isfile(binp):
                try:
                    subprocess.Popen([binp], cwd=os.path.dirname(binp),
                                     start_new_session=True)
                except OSError:
                    pass
        self.root.destroy()

    def _chosen_keys(self):
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
        tk = self.tk
        outer = tk.Frame(self.body, bg=BG)
        outer.pack(fill="both", expand=True, padx=(22, 8), pady=(4, 8))
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                           troughcolor=BG, bg=FIELD, activebackground=MUTED,
                           bd=0, relief="flat", width=12, highlightthickness=0)
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

        def _wheel(step):
            # X11 Tk reports the wheel as Button-4/5, not <MouseWheel>.
            def handler(_e):
                if canvas.winfo_exists():
                    canvas.yview_scroll(step, "units")
            return handler
        self.root.bind_all("<Button-4>", _wheel(-3))
        self.root.bind_all("<Button-5>", _wheel(3))
        self.root.bind_all("<MouseWheel>",
                           lambda e: canvas.winfo_exists() and
                           canvas.yview_scroll(-1 * (e.delta // 120), "units"))
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
                   "configure Steam & Big Picture for couch-console "
                   "gaming.").pack(anchor="w", pady=(10, 0))

        if self.upgrade:
            self._p(f, "An existing installation was found  this will update "
                       "it in place and keep your settings.",
                    color=GOLD, size=9).pack(anchor="w", pady=(18, 0))

        self._p(f, "Free software under the GNU GPL v3.0. Not affiliated with "
                   "Valve.", color=MUTED, size=9).pack(anchor="w",
                                                       side="bottom")
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
        for label, path in (("Home (recommended)", _default_install_dir()),
                            ("/opt", f"/opt/{APP_NAME}"),
                            ("/usr/local", f"/usr/local/lib/{APP_NAME}")):
            tk.Button(quick, text=label, relief="flat", bd=0, bg=CARD, fg=FG,
                      activebackground=CARD_HI, activeforeground=FG,
                      font=(FONT, 9), padx=12, pady=5, cursor="hand2",
                      highlightthickness=0,
                      command=lambda p=path: self.dir_var.set(p)).pack(
                          side="left", padx=(0, 8))

        self.dir_var.trace_add("write", lambda *_a: self._refresh_loc_note())
        self._refresh_loc_note()
        self._footer("Next")

    def _browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(
            title="Choose the install folder",
            initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if d:
            d = os.path.normpath(d)
            if os.path.basename(d) != APP_NAME:
                d = os.path.join(d, APP_NAME)
            self.dir_var.set(d)

    def _refresh_loc_note(self):
        path = self.dir_var.get().strip()
        if not path:
            self.loc_note.configure(text="Enter a folder.", fg=ROSE)
            return
        parts = [f"Free space: {_human(_free_space(path))}."]
        src = _find(SRC_BINS)
        if src:
            try:
                parts.append(f"Needs about {_human(os.path.getsize(src))}.")
            except OSError:
                pass
        writable = _dir_writable(path)
        if not writable:
            parts.append("This folder needs root  the wizard will ask once.")
        elif not _on_path(path):
            parts.append("Not on your PATH; the menu entry will still work.")
        self.loc_note.configure(text=" ".join(parts),
                                fg=(GOLD if not writable else MUTED))

    def _validate_dir(self):
        path = self.dir_var.get().strip()
        if not path.startswith("/") and not path.startswith("~"):
            self._alert("Pick a folder",
                        "Enter an absolute path, for example "
                        "~/.local/bin/SteamlessInput.")
            return False
        return True

    def _page_components(self):
        tk = self.tk
        self._set_header("Choose components", "Tick only what you need")
        head = tk.Frame(self.body, bg=BG)
        head.pack(fill="x", padx=30, pady=(22, 6))
        self._h1(head, "What should be installed?").pack(anchor="w")
        self._p(head, "Everything except the app itself is optional. Anything "
                      "already on this system is marked.").pack(anchor="w",
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

    def _badges(self, step, present, avail):
        out = []
        if step.required:
            out.append(("REQUIRED", ACCENT))
        if present is True:
            out.append(("ALREADY SET UP", GREEN))
        if not avail:
            out.append(("UNAVAILABLE", GOLD))
        if step.admin:
            out.append(("NEEDS ROOT", GOLD))
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

        tk.Frame(top, bg=ROSE, height=4).pack(fill="x")
        f = tk.Frame(top, bg=BG)
        f.pack(fill="both", expand=True, padx=26, pady=22)
        tk.Label(f, text=step.title, bg=BG, fg=ROSE, font=(FONT, 13, "bold"),
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
            ok_btn.configure(bg=(ACCENT if value else "#1f3d55"),
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

        self.root.wait_window(top)
        return state["ok"]

    def _page_review(self):
        tk = self.tk
        self._set_header("Ready to install", "")
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="both", expand=True, padx=30, pady=(22, 10))
        self._h1(f, "This is everything that will happen").pack(anchor="w")

        # _chosen_keys, not self.selected: a component pulled in as
        # another one's requirement is going to run, so it belongs on the
        # list that claims to be everything that will happen.
        keys = self._chosen_keys()
        chosen = [s for s in self.steps if s.key in keys]
        admin_keys = _plan_admin_keys(self.steps, self._chosen_keys(),
                                      self.dir_var.get())

        box = tk.Frame(f, bg=CARD)
        box.pack(fill="both", expand=True, pady=(14, 0))
        inner = tk.Frame(box, bg=CARD)
        inner.pack(fill="both", expand=True, padx=18, pady=16)
        tk.Label(inner, text=f"Install folder:  {self.dir_var.get()}", bg=CARD,
                 fg=FG, font=(FONT, 10, "bold"), anchor="w", justify="left",
                 wraplength=620).pack(anchor="w", pady=(0, 10))
        for s in chosen:
            row = tk.Frame(inner, bg=CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="✓", bg=CARD, fg=GREEN,
                     font=(FONT, 10, "bold")).pack(side="left", padx=(0, 9))
            label = s.title + ("   (root)" if s.key in admin_keys else "")
            tk.Label(row, text=label, bg=CARD, fg=FG, font=(FONT, 10),
                     anchor="w").pack(side="left")
        skipped = [s for s in self.steps if s.key not in keys]
        if skipped:
            tk.Label(inner, text="Not installed:", bg=CARD, fg=MUTED,
                     font=(FONT, 9, "bold"), anchor="w").pack(anchor="w",
                                                              pady=(14, 4))
            tk.Label(inner, text=", ".join(s.title for s in skipped), bg=CARD,
                     fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                     wraplength=620).pack(anchor="w")

        if admin_keys and not _is_root():
            _prefix, name = _elevator()
            self._p(f, f"You'll be asked for your password once "
                       f"({name or 'sudo'}), for the steps marked above. "
                       f"Everything else is done as you.",
                    color=GOLD, size=9).pack(anchor="w", pady=(12, 0))
        self._footer("Install")

    def _page_uninstall_pick(self):
        tk = self.tk
        prev_dir, _v = _detect_installed()
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
                tk.Label(trow, text="  NEEDS ROOT  ", bg=CARD, fg=GOLD,
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

        tk.Label(self.body, text="The GTK3 / AppIndicator packages stay  "
                                 "other apps use them. Remove them with your "
                                 "package manager if you want them gone.",
                 bg=BG, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                 wraplength=680).pack(anchor="w", padx=30, pady=(0, 12))
        self._footer("Uninstall", back_on=False)

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
                              font=("DejaVu Sans Mono", 9), wrap="word",
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
        self.logbox.tag_configure("step", font=("DejaVu Sans Mono", 9, "bold"))

        self._footer(next_text="Next", next_on=False, back_on=False,
                     cancel_on=False)
        self._start_work()

    def _log(self, level, text):
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
        install_dir = os.path.expanduser(self.dir_var.get().strip())

        def work():
            try:
                ok, failed = execute(self.steps, set(keys), install_dir,
                                     self._log, opts, self.mode)
            except Exception:
                self._log("err",
                          traceback.format_exc().strip().splitlines()[-1])
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

    def _page_done(self):
        tk = self.tk
        ok = self.result_ok is not False
        if self.mode == "install":
            self._set_header("All set" if ok else "Mostly done",
                             f"{APP_NAME} is ready")
        else:
            self._set_header("Uninstalled" if ok else "Mostly removed", "")
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="both", expand=True, padx=30, pady=26)

        if self.mode == "install":
            self._h1(f, f"{APP_NAME} is installed").pack(anchor="w")
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
                           f"working.", color=GOLD, size=9).pack(
                               anchor="w", pady=(14, 0))
            if not _detect_udev():
                self._p(f, "No udev rule is installed, so the controller may "
                           "only be reachable as root.", color=GOLD,
                        size=9).pack(anchor="w", pady=(10, 0))
            elif "udev" not in (self.failed or []):
                self._p(f, "Replug the controller once so the new udev rules "
                           "apply.", color=MUTED, size=9).pack(anchor="w",
                                                               pady=(10, 0))

            row = tk.Frame(f, bg=BG)
            row.pack(anchor="w", pady=(24, 0))
            launch = Check(row, value=True,
                           command=lambda v: self.launch_var.set(v))
            launch.set_bg(BG)
            launch.canvas.pack(side="left")
            tk.Label(row, text=f"Launch {APP_NAME} now", bg=BG, fg=FG,
                     font=(FONT, 10)).pack(side="left", padx=(10, 0))
        else:
            self._h1(f, f"{APP_NAME} has been removed").pack(anchor="w")
            if self.keep_settings_var.get():
                self._p(f, "Your settings.json was kept.").pack(
                    anchor="w", pady=(10, 0))

        self._footer("Finish", back_on=False, cancel_on=False)

    def _alert(self, title, text):
        from tkinter import messagebox
        messagebox.showwarning(title, text, parent=self.root)

    def run(self):
        self.root.mainloop()


# =============================================================================
# console flow
# =============================================================================

class _NoInput(Exception):
    """Raised when the console flow needs an answer and stdin can't give one."""


def _ask(prompt):
    """input() that fails LOUDLY but cleanly on a closed/redirected stdin.

    Bare input() raises EOFError there and dumps a traceback. Never fall back
    to "assume yes": an installer that starts installing because it couldn't
    read the answer is worse than one that stops."""
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
    prev_dir, _v = _detect_installed()
    install_dir = os.path.expanduser(
        args.dir or prev_dir or _default_install_dir())

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
                tags.append("already set up")
            if s.admin:
                tags.append("root")
            if s.danger:
                tags.append("SECURITY RISK")
            if not avail:
                tags.append("unavailable")
            tag = f"  [{', '.join(tags)}]" if tags else ""
            print(f"  {s.key:<10} {s.title}{tag}")
            if s.blurb:
                print(f"             {s.blurb}")
            if not avail:
                print(f"             {why}")
                continue
            if s.required and mode == "install":
                selected.add(s.key)
                continue
            default = (s.initial_tick() if mode == "install"
                       else s.uninstall_default)
            if args.yes:
                if s.danger:
                    print("             skipped  never taken by default; "
                          f"pass --with {s.key} to opt in")
                elif default:
                    selected.add(s.key)
                continue
            if s.danger and s.warning:
                print("\n" + "\n".join("             " + ln
                                       for ln in s.warning.splitlines()))
            ans = _ask(f"             install? "
                       f"[{'Y/n' if default else 'y/N'}] ").strip().lower()
            if (ans in ("y", "yes")) or (not ans and default):
                selected.add(s.key)
            print()

    if mode == "install":
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
# entry point
# =============================================================================

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="SteamlessInput Setup",
        description="Install or remove SteamlessInput and its optional parts.")
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
    p.add_argument("--run-plan", help=argparse.SUPPRESS)   # elevated child
    args = p.parse_args(argv)

    if args.run_plan:
        return run_plan(args.run_plan)

    if not sys.platform.startswith("linux"):
        print("This is the Linux installer. On Windows run "
              "windows\\installer.py.")
        return 2

    if args.list:
        for s in build_steps():
            print(f"{s.key:<10} {s.title}")
        return 0

    def _text_mode():
        try:
            return console_flow(args)
        except _NoInput:
            print("\n  Nothing was installed: this run has no interactive "
                  "input to answer the questions.\n"
                  "  For unattended use pass --yes (defaults) or "
                  "--with app,autostart,... to choose explicitly.")
            return 2

    if args.console or args.yes or args.with_:
        return _text_mode()

    try:
        import tkinter                     # noqa: F401
    except Exception:
        print("tkinter isn't available (install python3-tk for the GUI); "
              "falling back to the console wizard.\n")
        return _text_mode()

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("No display detected; falling back to the console wizard.\n")
        return _text_mode()

    Wizard("uninstall" if args.uninstall else "install",
           preset_dir=args.dir).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
