"""Big Picture automation engine  Linux side (Options → Big Picture).

The Linux page carries the AUTOMATION half only: open Steam's Big Picture when
a controller connects and close it again when the last one disconnects
(skipped while a game is running). This is a port of goatvisuals'
Auto-Big-Picture systemd service into the tray process: joystick presence is
polled from /dev/input, Steam is driven through its steam://open/bigpicture
and steam://close/bigpicture URLs (native or flatpak install), and the
game-in-progress guard looks for processes running out of steamapps/common.

The Windows tree's "While Big Picture Is Open" feature set (display/audio
switching, HDR, Night Light, cursor hiding, power plan  windows/big_picture.py)
is built on Windows-only APIs (DisplayConfig, MMDevice/IPolicyConfig, the
CloudStore night-light registry) and has no Linux equivalent here.

LICENCE NOTE for contributors: SteamlessInput is GPL-3.0. Port code into this
module only from GPL-3.0-compatible sources (MIT / BSD / Apache-2.0 / LGPL any
version / GPL-2.0-or-later / GPL-3.0)  proprietary code cannot be carried
here, and AGPL-3.0 code drags its network-use obligation along with it.
"""

import glob
import os
import subprocess
import threading
import time

_JOYSTICK_GLOB = "/dev/input/by-id/*-event-joystick"
_DISCONNECT_GRACE_S = 3.0    # joystick gone this long before auto-close
# How long the controller signal stays untrusted after the tray un-pauses
# (see _tick). While SteamlessInput is paused for Steam it cedes the
# controllers, so a tray-supplied controller_connected() reports False no
# matter what is plugged in; when Steam exits the pad "reappears". Without a
# settle window that reads as a fresh connect and re-opens Big Picture, which
# restarts Steam, which pauses us again  an infinite reopen loop.
_PAUSE_SETTLE_S = 10.0


def get_steam_cmd():
    """The command prefix that reaches the user's Steam install: native
    ["steam"] when it's on PATH, else the flatpak app when installed, else
    None (Auto-Big-Picture's get_steam_cmd)."""
    try:
        if subprocess.run(["which", "steam"],
                          capture_output=True).returncode == 0:
            return ["steam"]
        if subprocess.run(["which", "flatpak"],
                          capture_output=True).returncode == 0:
            result = subprocess.run(
                ["flatpak", "list", "--app", "--columns=application"],
                capture_output=True, text=True)
            if "com.valvesoftware.Steam" in result.stdout:
                return ["flatpak", "run", "com.valvesoftware.Steam"]
    except Exception as e:
        print(f"big_picture: steam lookup failed: {e!r}")
    return None


def steam_running():
    try:
        return subprocess.run(["pgrep", "-x", "steam"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def steam_game_running():
    """True while anything runs out of steamapps/common  the "don't close
    Big Picture mid-game" guard (d3ddriverquery64.exe is Proton's own idle
    helper, not a game)."""
    try:
        cmd = ["sh", "-c",
               "pgrep -af 'steamapps/common' | grep -v 'd3ddriverquery64.exe'"]
        return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def open_big_picture():
    """Ask Steam to open Big Picture (starts Steam first when needed)."""
    cmd = get_steam_cmd()
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd + ["steam://open/bigpicture"])
        return True
    except OSError as e:
        print(f"big_picture: open failed: {e!r}")
        return False


def close_big_picture():
    """Ask a RUNNING Steam to leave Big Picture."""
    cmd = get_steam_cmd()
    if not cmd or not steam_running():
        return False
    try:
        subprocess.Popen(cmd + ["steam://close/bigpicture"])
        return True
    except OSError as e:
        print(f"big_picture: close failed: {e!r}")
        return False

# Whether WE last asked for Big Picture  the toggle's only state signal on
# Linux, which has no window-level Big Picture detector (the Windows build
# reads the real window; see its is_big_picture_running). Both steam:// URLs
# are fire-and-forget either way, so the worst a stale latch costs is one
# press that repeats what Big Picture is already doing.
_bp_opened = False


def toggle_big_picture():
    """Open Big Picture, or leave it if we opened it  the bindable "Toggle
    Big Picture" action."""
    global _bp_opened
    if _bp_opened and steam_running():
        _bp_opened = False
        return close_big_picture()
    _bp_opened = bool(open_big_picture())
    return _bp_opened


def joystick_connected():
    """True while at least one joystick event node exists (USB + Bluetooth
    pads both surface under /dev/input/by-id as *-event-joystick)."""
    try:
        return bool(glob.glob(_JOYSTICK_GLOB))
    except OSError:
        return False


class BigPictureEngine:
    """Monitor thread for the controller-connect automation. Mirrors the
    Windows engine's surface (start/stop/refresh + the same bp_auto_launch /
    bp_auto_close settings keys) so both trays wire it identically; the
    session-features half of the Windows engine has no Linux counterpart."""

    def __init__(self, settings, save_settings=None, notify=None,
                 controller_connected=None, controller_paused=None):
        self.settings = settings
        self._notify = notify or (lambda *a: None)
        # The tray can supply its own live pad state; default = /dev/input.
        self._pads_live = controller_connected or joystick_connected
        # True while the tray has ceded the controllers (paused for Steam), so
        # the pad signal says nothing about what is plugged in.
        self._pads_paused = controller_paused or (lambda: False)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._pads_seen = None       # None until the first sample (no edge)
        self._pads_gone_at = None
        # Pause-boundary guard: _was_paused latches a pause so the RESUME can
        # be detected, which arms _pads_settle_until (edges ignored until then).
        self._was_paused = False
        self._pads_settle_until = 0.0

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="big-picture", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def refresh(self):
        """A bp_* setting changed  re-evaluate the enabled state promptly."""
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            if self.settings.get("bp_auto_launch", "off") == "off":
                self._pads_seen = None
                self._pads_gone_at = None
                self._wake.wait()        # nothing enabled  block until poked
                self._wake.clear()
                continue
            try:
                self._tick()
            except Exception as e:
                print(f"big_picture: tick error: {e!r}")
            self._wake.wait(1.0)
            self._wake.clear()

    def _tick(self):
        mode = self.settings.get("bp_auto_launch", "off")
        # Edges ARE sampled while paused for Steam  the "When Steam Is
        # Running" auto-open and the auto-close can only ever fire while Steam
        # is up, i.e. while paused. That works here because the default
        # presence signal (joystick_connected) reads /dev/input directly and
        # stays truthful no matter what the tray has ceded. What must NOT be
        # trusted is the pause BOUNDARY: a tray-supplied signal swaps source
        # there and the controller stack takes seconds to rebuild after Steam
        # exits, so a pad "appearing" then is an artefact  left unguarded it
        # re-opens Big Picture, restarting Steam and pausing us again, forever.
        paused = self._pads_paused()
        if paused:
            self._was_paused = True
        elif self._was_paused:
            self._was_paused = False
            self._pads_settle_until = time.monotonic() + _PAUSE_SETTLE_S
        if time.monotonic() < self._pads_settle_until:
            self._pads_seen = None
            self._pads_gone_at = None
            return
        pads = bool(self._pads_live())
        prev = self._pads_seen
        self._pads_seen = pads
        if prev is None:
            return                       # first sample after enable  no edge
        if pads and not prev:
            self._pads_gone_at = None
            if mode == "always" or steam_running():
                print("big_picture: controller connected  opening BP")
                open_big_picture()
        elif not pads and prev:
            self._pads_gone_at = time.monotonic()
        if (not pads and self._pads_gone_at is not None
                and time.monotonic() - self._pads_gone_at
                >= _DISCONNECT_GRACE_S):
            self._pads_gone_at = None
            if (self.settings.get("bp_auto_close") and steam_running()
                    and not steam_game_running()):
                print("big_picture: last controller gone  closing BP")
                close_big_picture()
