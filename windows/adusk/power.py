# -*- coding: utf-8 -*-
"""Process / thread responsiveness control (scoped, reference-counted).

The OSK render+input loop and the SDL desktop-mouse poller both need Windows to
keep this (never-foreground) process OFF the EcoQoS efficiency path and the
multimedia timer pinned at 1 ms  otherwise their high-rate loops stall on the
efficiency cores / coarse system timer and input feels laggy or goes dead (see
the osk-mouse-eco-throttling / osk-crossthread-render-clicks findings).

But holding that for the WHOLE time the tray is alive is wasteful: the global
1 ms timer raises system-wide power draw, and the EcoQoS opt-out keeps the
process off the efficiency cores even while it sits idle in the tray. So callers
REQUEST high responsiveness only while they actually need to be fast (the OSK is
open, OR an SDL pad is live-driving the desktop) and RELEASE it when done  the
cost is paid only while something needs the speed, and the process falls back to
true background (Eco) the rest of the time.

Reference-counted and thread-safe: request()/release() come from BOTH the
launcher/OSK thread and the SDL pad thread. All functions are no-ops off
Windows (no WM EcoQoS-parks our threads there)."""

import ctypes
import sys
import threading

_IS_WINDOWS = sys.platform == "win32"

# PROCESS_INFORMATION_CLASS.ProcessPowerThrottling = 4 and THREAD_INFORMATION_
# CLASS.ThreadPowerThrottling = 3 both take the same 3-DWORD state struct.
# ControlMask selects EXECUTION_SPEED; StateMask 0 = "don't throttle" (opt out),
# ControlMask 0 = "stop controlling this  let the system decide" (the default
# for a background process is EcoQoS, i.e. throttled).
_PT_VERSION = 1
_PT_EXECUTION_SPEED = 0x1
_ProcessPowerThrottling = 4
_ThreadPowerThrottling = 3
_THREAD_PRIORITY_NORMAL = 0
_THREAD_PRIORITY_HIGHEST = 2


class _POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_uint32),
                ("ControlMask", ctypes.c_uint32),
                ("StateMask", ctypes.c_uint32)]


_lock = threading.Lock()
_refcount = 0


def _set_process_throttling(control_mask, state_mask):
    st = _POWER_THROTTLING_STATE(_PT_VERSION, control_mask, state_mask)
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.SetProcessInformation.restype = ctypes.c_bool
    k.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_void_p, ctypes.c_uint32]
    k.SetProcessInformation(k.GetCurrentProcess(), _ProcessPowerThrottling,
                            ctypes.byref(st), ctypes.sizeof(st))


def request():
    """Enter high-responsiveness mode (reference-counted; idempotent). On the
    0->1 transition raise the multimedia timer to 1 ms and opt the PROCESS out
    of EcoQoS execution-speed throttling. Best-effort; older Windows lacking the
    APIs simply no-ops, as does any non-Windows platform."""
    global _refcount
    if not _IS_WINDOWS:
        return
    with _lock:
        _refcount += 1
        if _refcount != 1:
            return
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        try:
            _set_process_throttling(_PT_EXECUTION_SPEED, 0)  # opt OUT of throttling
        except Exception:
            pass


def release():
    """Leave high-responsiveness mode (reference-counted; clamped at zero so a
    defensive double-release is harmless). On the 1->0 transition drop the 1 ms
    timer and hand EcoQoS control back to the system, so the idle background
    process can be parked on efficiency cores again."""
    global _refcount
    if not _IS_WINDOWS:
        return
    with _lock:
        if _refcount == 0:
            return
        _refcount -= 1
        if _refcount != 0:
            return
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        try:
            _set_process_throttling(0, 0)  # ControlMask 0 -> system decides (Eco)
        except Exception:
            pass


def boost_current_thread():
    """Opt the CALLING thread out of EcoQoS and raise its priority to HIGHEST.
    The process opt-out (request) stops CPU-frequency throttling, but a loop on a
    never-foreground thread can still be starved of quanta when a busy foreground
    app contends  so the thread running a latency-critical loop (the OSK render/
    input loop; the SDL desktop-mouse poll) pins itself here. Paired with
    unboost_current_thread() on the SAME thread."""
    if not _IS_WINDOWS:
        return
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentThread.restype = ctypes.c_void_p
        k.SetThreadInformation.restype = ctypes.c_bool
        k.SetThreadInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_uint32]
        ts = _POWER_THROTTLING_STATE(_PT_VERSION, _PT_EXECUTION_SPEED, 0)
        k.SetThreadInformation(k.GetCurrentThread(), _ThreadPowerThrottling,
                               ctypes.byref(ts), ctypes.sizeof(ts))
        k.SetThreadPriority.restype = ctypes.c_bool
        k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetThreadPriority(k.GetCurrentThread(), _THREAD_PRIORITY_HIGHEST)
    except Exception:
        pass


def unboost_current_thread():
    """Undo boost_current_thread() on the CALLING thread: hand EcoQoS control of
    the thread back to the system and restore NORMAL priority, so it's a good
    background citizen between bursts."""
    if not _IS_WINDOWS:
        return
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentThread.restype = ctypes.c_void_p
        k.SetThreadInformation.restype = ctypes.c_bool
        k.SetThreadInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_uint32]
        ts = _POWER_THROTTLING_STATE(_PT_VERSION, 0, 0)  # ControlMask 0 -> system decides
        k.SetThreadInformation(k.GetCurrentThread(), _ThreadPowerThrottling,
                               ctypes.byref(ts), ctypes.sizeof(ts))
        k.SetThreadPriority.restype = ctypes.c_bool
        k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetThreadPriority(k.GetCurrentThread(), _THREAD_PRIORITY_NORMAL)
    except Exception:
        pass
