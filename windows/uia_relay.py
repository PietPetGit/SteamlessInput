"""uiAccess input relay  injects input into windows Windows won't let us touch.

WHY THIS EXISTS
    User Interface Privilege Isolation drops every event a medium-integrity
    process injects at a HIGHER-integrity window: Task Manager, installers,
    regedit, an elevated console. That kills the trackpad cursor, every custom
    binding and the on-screen keyboard the moment such a window takes focus.

    A process holding the uiAccess privilege is exempt from that filter. It is
    the mechanism Windows ships FOR accessibility tools, and it is NOT
    administrator: a uiAccess process still runs at medium integrity, so it
    cannot write %ProgramFiles%, cannot touch HKLM, and cannot elevate. All it
    gains is the right to drive the UI of a higher-integrity window.

    Rather than manifest the whole application uiAccess (a large Python process
    with a GUI, HID stack and network-facing config import), this relay is the
    ONLY privileged part: a few hundred lines whose entire vocabulary is
    "press this key", "move the cursor", "click". The app stays ordinary.

    Windows grants uiAccess only when the exe is BOTH Authenticode-signed by a
    certificate the machine trusts AND running from a secure location
    (%ProgramFiles%, %SystemRoot%). See build_uia_relay.py and
    lockscreen-keyboard/install.ps1. Without those it still runs  it just gets
    no exemption, and the app falls back to lizard mode as before.

SECURITY MODEL  read before changing the protocol
    This process will, on request, synthesize input into an elevated window.
    An unauthenticated version of it would be a privilege-escalation service
    for anything running on the machine. Three things keep it narrow:

    1. The pipe's DACL grants access to the creating user and SYSTEM only.
    2. Every connection is authenticated by the CLIENT'S IMAGE PATH: the
       connecting process must run from an executable inside this relay's own
       install root. That root is %ProgramFiles% in a real install, which is
       write-protected, so an attacker cannot plant a binary that qualifies.
       The expected path is derived from OUR OWN location and never taken from
       the command line  otherwise whoever launches the relay first would
       choose who may drive it.
    3. The vocabulary is fixed and tiny. There is no "run this", no path, no
       string: only key codes, button ids and cursor deltas.

    The honest limit: uiAccess crosses an INTEGRITY boundary, not a user one.
    Code already running as this user at medium integrity can inject a DLL
    into the client and borrow its identity. That is inherent to every
    uiAccess relay, and is exactly why this one is uiAccess rather than
    administrator  the blast radius stays "can drive the UI", not "owns the
    box".
"""

import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes

PIPE_NAME = r"\\.\pipe\SteamlessInput.uiarelay"

# Serve one client at a time; exit once nobody has needed us for a while so a
# privileged process never lingers past its usefulness.
IDLE_EXIT_S = 300.0
CONNECT_POLL_S = 0.25

# --- Win32 -----------------------------------------------------------------
# use_last_error=True is REQUIRED, not tidiness: ctypes.get_last_error() reads
# ctypes' own saved copy of GetLastError, which is only captured when the
# library is opened this way. With plain ctypes.windll it always reads 0  and
# ConnectNamedPipe legitimately returns FALSE/ERROR_PIPE_CONNECTED when a
# client connected in the gap after CreateNamedPipe, so a zeroed error code
# makes every such connection look like a failure and get dropped.
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PIPE_ACCESS_INBOUND = 0x00000001
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


def _current_user_sid_string():
    """SID of the user we're running as, as SDDL text."""
    TOKEN_QUERY = 0x0008
    TokenUser = 1
    tok = wintypes.HANDLE()
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    # argtypes required: the pseudo-handle is 0xFFFFFFFFFFFFFFFF once restype
    # is HANDLE, and an un-annotated ctypes argument defaults to c_int, which
    # overflows rather than passing it.
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY,
                                ctypes.byref(tok)):
        return None
    try:
        size = wintypes.DWORD(0)
        adv.GetTokenInformation(tok, TokenUser, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(tok, TokenUser, buf, size,
                                       ctypes.byref(size)):
            return None
        # TOKEN_USER is SID_AND_ATTRIBUTES: the PSID is the first pointer.
        sid = ctypes.c_void_p.from_buffer(buf).value
        out = ctypes.c_wchar_p()
        if not adv.ConvertSidToStringSidW(ctypes.c_void_p(sid),
                                          ctypes.byref(out)):
            return None
        try:
            return out.value
        finally:
            k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
    finally:
        k32.CloseHandle(tok)


def _pipe_security():
    """SECURITY_ATTRIBUTES granting the pipe to this user and SYSTEM only.

    Returns None if the descriptor can't be built  the caller then refuses to
    create the pipe rather than falling back to a default DACL. A relay that
    can inject into elevated windows must never be reachable more widely than
    intended, so "couldn't lock it down" has to mean "don't open it"."""
    sid = _current_user_sid_string()
    if not sid:
        return None
    # P = protected (no inherited ACEs), GA = generic all. SY = LocalSystem.
    sddl = "D:P(A;;GA;;;SY)(A;;GA;;;%s)" % sid
    sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            ctypes.c_wchar_p(sddl), 1, ctypes.byref(sd), None):
        return None
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = sd
    sa.bInheritHandle = False
    return sa


def _log(msg):
    """Diagnostics to the file named by SKB_RELAY_LOG, if set. The shipped
    build is --windowed, so stdout goes nowhere  without this there is no way
    to see why a connection was refused."""
    path = os.environ.get("SKB_RELAY_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%.3f %s\n" % (time.time(), msg))
    except OSError:
        pass


def _install_root():
    """Directory the relay was installed into  the trust anchor for clients."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _client_image_path(pipe):
    """Full image path of the process on the other end of `pipe`, or None."""
    pid = wintypes.DWORD()
    if not k32.GetNamedPipeClientProcessId(pipe, ctypes.byref(pid)):
        return None
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return buf.value
    finally:
        k32.CloseHandle(h)


def _authorized_clients():
    """Client exe paths an ADMINISTRATOR authorized, from authorized_clients.txt
    beside us.

    SteamlessInput is portable  it runs from the user's Desktop, a USB
    stick, anywhere  while the relay must live in %ProgramFiles% to be granted
    uiAccess. So "client inside my install root" alone can never match, and the
    app would authenticate as nobody.

    The allowlist closes that gap without weakening anything: the file sits in
    the relay's own %ProgramFiles% directory, so only an administrator can
    write it, and the installer fills it in at the same moment it asks for
    consent. Being listed there is an administrator's statement that this exe
    may drive input  which is exactly the decision being delegated."""
    out = []
    try:
        p = os.path.join(_install_root(), "authorized_clients.txt")
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(os.path.normcase(os.path.abspath(line)))
    except OSError:
        pass
    return out


def _client_is_trusted(pipe):
    """True when the caller is an executable we are willing to inject for.

    Two ways to qualify, both anchored in a directory only an administrator can
    write, and neither taken from the command line  if the expected path were
    a parameter, whoever started the relay would nominate who may drive it:

      * living inside the relay's own install root, or
      * listed in the administrator-written allowlist (see above)."""
    path = _client_image_path(pipe)
    if not path:
        _log("client image path unavailable")
        return False
    try:
        root = os.path.normcase(os.path.abspath(_install_root()))
        client = os.path.normcase(os.path.abspath(path))
        ok = os.path.commonpath([root, client]) == root
        if not ok:
            ok = client in _authorized_clients()
    except Exception as e:
        _log("trust check error: %r" % (e,))
        return False
    _log("client=%s root=%s trusted=%s" % (client, root, ok))
    return ok


# --- input synthesis --------------------------------------------------------
# SendInput structures. This is the whole point of the process: these calls
# carry the uiAccess exemption, so they land on windows the client's own
# SendInput would be filtered out of.

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
_BTN_FLAGS = {            # button id -> (down flag, up flag, mouseData)
    0: (0x0002, 0x0004, 0),      # left
    1: (0x0008, 0x0010, 0),      # right
    2: (0x0020, 0x0040, 0),      # middle
    3: (0x0080, 0x0100, 1),      # X1 (Back)
    4: (0x0080, 0x0100, 2),      # X2 (Forward)
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inp):
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def do_key(vk, down):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0,
                        dwFlags=0 if down else KEYEVENTF_KEYUP,
                        time=0, dwExtraInfo=None)
    _send(inp)


def do_move(dx, dy):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=MOUSEEVENTF_MOVE,
                        time=0, dwExtraInfo=None)
    _send(inp)


def do_button(btn, down):
    flags = _BTN_FLAGS.get(btn)
    if flags is None:
        return
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx=0, dy=0, mouseData=flags[2],
                        dwFlags=flags[0] if down else flags[1],
                        time=0, dwExtraInfo=None)
    _send(inp)


def do_wheel(delta, horizontal=False):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(
        dx=0, dy=0, mouseData=delta,
        dwFlags=MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL,
        time=0, dwExtraInfo=None)
    _send(inp)


# --- protocol ---------------------------------------------------------------
# Fixed 6-byte frames: opcode + two int16 args (little-endian). No strings, no
# paths, no variable lengths  nothing that could grow into "execute this".
FRAME = struct.Struct("<chh")
FRAME_SIZE = FRAME.size

OP_KEY = b"K"       # a=virtual key code, b=1 down / 0 up
OP_MOVE = b"M"      # a=dx, b=dy (relative)
OP_BTN = b"B"       # a=button id 0..4, b=1 down / 0 up
OP_WHEEL = b"W"     # a=delta (±120 units), b=1 horizontal
OP_PING = b"P"      # liveness probe; the client uses it to confirm uiAccess


def dispatch(op, a, b):
    if op == OP_KEY:
        do_key(a & 0xFFFF, bool(b))
    elif op == OP_MOVE:
        do_move(a, b)
    elif op == OP_BTN:
        do_button(a, bool(b))
    elif op == OP_WHEEL:
        do_wheel(a, bool(b))
    # OP_PING: presence is the answer; nothing to do.


def serve_one(pipe):
    """Read frames until the client goes away."""
    buf = ctypes.create_string_buffer(FRAME_SIZE * 64)
    got = wintypes.DWORD()
    pending = b""
    while True:
        if not k32.ReadFile(pipe, buf, len(buf), ctypes.byref(got), None):
            return                      # broken pipe = client gone
        if not got.value:
            return
        pending += buf.raw[:got.value]
        while len(pending) >= FRAME_SIZE:
            frame, pending = pending[:FRAME_SIZE], pending[FRAME_SIZE:]
            try:
                op, a, b = FRAME.unpack(frame)
            except struct.error:
                return                  # desynced  drop the connection
            dispatch(op, a, b)


def main():
    sa = _pipe_security()
    if sa is None:
        print("uia_relay: refusing to open an unrestricted pipe")
        return 2
    last_client = time.monotonic()
    first = True
    while True:
        flags = PIPE_ACCESS_INBOUND
        if first:
            # Refuse to start if an instance already owns the name  otherwise
            # an impostor could squat it and take our clients' input stream.
            flags |= FILE_FLAG_FIRST_PIPE_INSTANCE
        pipe = k32.CreateNamedPipeW(
            ctypes.c_wchar_p(PIPE_NAME), flags,
            PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1, 0, 4096, 0, ctypes.byref(sa))
        if pipe == INVALID_HANDLE_VALUE:
            print("uia_relay: CreateNamedPipe failed (%d)"
                  % ctypes.get_last_error())
            return 3
        first = False
        try:
            ok = k32.ConnectNamedPipe(pipe, None)
            err = ctypes.get_last_error()
            if not ok and err != ERROR_PIPE_CONNECTED:
                _log("ConnectNamedPipe failed (%d)" % err)
                continue
            _log("client connected (ok=%s err=%d)" % (bool(ok), err))
            if not _client_is_trusted(pipe):
                print("uia_relay: rejected an untrusted client")
                continue
            last_client = time.monotonic()
            serve_one(pipe)
            last_client = time.monotonic()
        finally:
            k32.DisconnectNamedPipe(pipe)
            k32.CloseHandle(pipe)
        if time.monotonic() - last_client > IDLE_EXIT_S:
            return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
