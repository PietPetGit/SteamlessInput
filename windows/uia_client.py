"""Client for the uiAccess input relay (uia_relay.py).

Starts the relay on demand, confirms it ACTUALLY holds uiAccess, and forwards
key/mouse events to it over the local pipe. Everything here is best-effort: if
the relay is missing, unsigned, installed outside a secure location or simply
refuses to run, `ready()` stays False and the caller falls back to whatever it
did before (lizard mode on a Steam Controller, nothing on other pads).

Confirming the privilege matters more than confirming the process. An exe with
a uiAccess manifest that is NOT signed-and-securely-installed still launches
perfectly happily  it just silently runs as an ordinary process, and its
SendInput is filtered exactly like ours. Routing input into that would be
strictly worse than the lizard fallback, so we read the relay's token and only
report ready when the UIAccess flag is really set (see _has_uiaccess).
"""

import ctypes
import os
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes

PIPE_NAME = r"\\.\pipe\SteamlessInput.uiarelay"
RELAY_DIR = "uia-relay"
RELAY_EXE = "SteamlessInputRelay.exe"

FRAME = struct.Struct("<chh")
OP_KEY, OP_MOVE, OP_BTN, OP_WHEEL, OP_PING = b"K", b"M", b"B", b"W", b"P"

# Mouse button ids on the wire (must match uia_relay._BTN_FLAGS).
BTN = {"left": 0, "right": 1, "middle": 2, "x1": 3, "x2": 4}

k32 = ctypes.windll.kernel32
adv = ctypes.windll.advapi32

GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
TOKEN_QUERY = 0x0008
TokenUIAccess = 26
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def relay_path():
    """Where the installed relay lives, or None if it isn't there.

    %ProgramFiles% first: that's where install_uia_relay.ps1 puts it, and it's
    the only location Windows will grant uiAccess from. Then next to us / in
    the dev tree, so this is testable from source (unprivileged, but the whole
    pipe path can be exercised)."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for base in (os.path.join(pf, "SteamlessInput"),
                 _exe_dir(), os.path.join(_exe_dir(), "dist")):
        p = os.path.join(base, RELAY_DIR, RELAY_EXE)
        if os.path.isfile(p):
            return p
    return None


def _has_uiaccess(pid):
    """True when process `pid` really holds the uiAccess privilege."""
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        tok = wintypes.HANDLE()
        # argtypes required  see uia_relay._current_user_sid_string.
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
        if not adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return False
        try:
            val = wintypes.DWORD(0)
            size = wintypes.DWORD(0)
            if not adv.GetTokenInformation(tok, TokenUIAccess,
                                           ctypes.byref(val),
                                           ctypes.sizeof(val),
                                           ctypes.byref(size)):
                return False
            return bool(val.value)
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


class RelayClient:
    """Thread-safe forwarder to the relay. All methods are no-ops when the
    relay isn't usable, so callers never have to branch."""

    # Don't retry a failed launch more than this often  a missing or
    # unsigned relay must not turn into a process-spawn loop on every frame.
    RETRY_S = 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._pipe = None
        self._proc = None
        self._ready = False
        self._last_try = 0.0
        self._reason = "not started"
        self._installed = None          # cached relay_path() lookup

    def installed(self):
        """Whether a relay is present at all, cached after the first look.

        Input sources poll this to decide whether the elevated-window route is
        even worth evaluating, so it must not hit the filesystem on every
        check  and it can't change without an install, which the user is not
        doing mid-session."""
        if self._installed is None:
            self._installed = relay_path() is not None
        return self._installed

    def forget_install(self):
        """Re-check for the relay  called after the in-app installer runs, so
        it becomes usable without restarting."""
        self._installed = None
        with self._lock:
            self._last_try = 0.0

    # -- lifecycle ---------------------------------------------------------
    def ready(self):
        with self._lock:
            return self._ready

    def reason(self):
        with self._lock:
            return self._reason

    def start(self):
        """Launch + connect + verify. Returns True when input can be routed.
        Cheap to call repeatedly: rate-limited, and a no-op once connected."""
        with self._lock:
            if self._ready:
                return True
            now = time.monotonic()
            if now - self._last_try < self.RETRY_S:
                return False
            self._last_try = now
            return self._start_locked()

    def _start_locked(self):
        path = relay_path()
        if not path:
            self._reason = "relay not installed"
            return False
        if self._proc is None or self._proc.poll() is not None:
            try:
                self._proc = subprocess.Popen(
                    [path], cwd=os.path.dirname(path),
                    creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError as e:
                self._reason = "relay would not start (%s)" % e
                return False
            time.sleep(0.4)             # let it create the pipe
        if not _has_uiaccess(self._proc.pid):
            # Running, but with no privilege  worse than useless, since its
            # SendInput is filtered exactly like ours. Shut it down and let the
            # caller fall back.
            self._reason = ("relay has no uiAccess (needs signing + install "
                            "under Program Files)")
            self._stop_locked()
            return False
        if not self._connect_locked():
            return False
        self._ready = True
        self._reason = "ok"
        return True

    def _connect_locked(self):
        h = k32.CreateFileW(ctypes.c_wchar_p(PIPE_NAME), GENERIC_WRITE, 0,
                            None, OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE or not h:
            self._reason = "could not open the relay pipe"
            return False
        self._pipe = h
        return True

    def _stop_locked(self):
        if self._pipe:
            k32.CloseHandle(ctypes.c_void_p(self._pipe))
            self._pipe = None
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._proc = None
        self._ready = False

    def stop(self):
        with self._lock:
            self._stop_locked()
            self._reason = "stopped"

    # -- wire --------------------------------------------------------------
    def _send(self, op, a, b):
        with self._lock:
            if not self._ready or not self._pipe:
                return False
            data = FRAME.pack(op, int(a), int(b))
            written = wintypes.DWORD()
            ok = k32.WriteFile(ctypes.c_void_p(self._pipe), data, len(data),
                               ctypes.byref(written), None)
            if not ok:
                # Relay died or the pipe broke  drop back to unavailable so
                # the guard reverts to its fallback instead of silently
                # swallowing every keystroke from here on.
                self._reason = "relay connection lost"
                self._stop_locked()
                return False
            return True

    def key(self, vk, down):
        return self._send(OP_KEY, vk, 1 if down else 0)

    def move(self, dx, dy):
        # int16 on the wire: clamp rather than wrap, so a wild delta can't
        # teleport the cursor to the opposite corner.
        dx = max(-32768, min(32767, int(dx)))
        dy = max(-32768, min(32767, int(dy)))
        return self._send(OP_MOVE, dx, dy)

    def button(self, name, down):
        b = BTN.get(name)
        if b is None:
            return False
        return self._send(OP_BTN, b, 1 if down else 0)

    def wheel(self, delta, horizontal=False):
        delta = max(-32768, min(32767, int(delta)))
        return self._send(OP_WHEEL, delta, 1 if horizontal else 0)

    def ping(self):
        return self._send(OP_PING, 0, 0)


CLIENT = RelayClient()
