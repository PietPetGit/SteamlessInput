#!/usr/env/python3
# -*- coding: utf-8 -*-

import ctypes
import os
import sys
import time
from threading import Thread

import sdl3w as S
import steamcontroller.uinput as sui
from steamcontroller import GYRO_DEG_PER_SEC as _GYRO_DEG_PER_SEC

from adusk import screen
from adusk import skins
from adusk.screen import CoordFraction
from adusk import config
from adusk import diacritics
from adusk import controller
from adusk import state
from adusk import swipe
from adusk import vkb
from adusk import vptr
from adusk import power


_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")

if _IS_LINUX:
    # The OSK's no-keyboard-focus relies on X11 WM hints (set via libX11 below),
    # so pin SDL to the X11 backend  natively or via XWayland  rather than
    # letting SDL3 default to Wayland, where those hints don't apply. setdefault
    # so a user can still override with SDL_VIDEODRIVER in the environment.
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")


# ---------------------------------------------------------------------------
# X11/XWayland: deny keyboard focus
# ---------------------------------------------------------------------------
#
# SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN is a no-op on SDL's X11 backend, so by
# default the OSK window steals focus the moment it's mapped. KWin (Plasma 6)
# and most other WMs accept two parallel signals to keep a window out of the
# focus rotation:
#
#   1. WM_HINTS.input = False    ICCCM "don't give me keyboard focus"
#   2. _NET_WM_WINDOW_TYPE_DOCK  EWMH dock windows are not focusable
#
# Plus SKIP_TASKBAR/SKIP_PAGER/ABOVE so the OSK doesn't clutter the task
# switcher and stays on top. The window can still receive its own pointer
# events; only kbd focus is denied.

_X11_INPUT_HINT = 1 << 0  # InputHint from <X11/Xutil.h>


class _XWMHints(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_long),
        ("input", ctypes.c_int),
        ("initial_state", ctypes.c_int),
        ("icon_pixmap", ctypes.c_ulong),
        ("icon_window", ctypes.c_ulong),
        ("icon_x", ctypes.c_int),
        ("icon_y", ctypes.c_int),
        ("icon_mask", ctypes.c_ulong),
        ("window_group", ctypes.c_ulong),
    ]


_libx11_cache = None


def _libx11():
    global _libx11_cache
    if _libx11_cache is not None:
        return _libx11_cache
    try:
        lib = ctypes.cdll.LoadLibrary("libX11.so.6")
    except OSError:
        return None
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XChangeProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
    ]
    lib.XChangeProperty.restype = ctypes.c_int
    lib.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
    lib.XSetWMHints.restype = ctypes.c_int
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XFlush.restype = ctypes.c_int
    # Active-window read (_NET_ACTIVE_WINDOW on root) + EWMH activate request,
    # used to keep the user's text field focused while the OSK is up.
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XSendEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
        ctypes.c_void_p,
    ]
    lib.XSendEvent.restype = ctypes.c_int
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XFree.restype = ctypes.c_int
    _libx11_cache = lib
    return lib


def _x11_handles(sdl_window):
    """Pull the (X11 Display*, Window) pair out of the SDL3 window properties,
    or (None, None) when SDL isn't on the X11 backend / the call failed."""
    display = S.get_x11_display(sdl_window)
    window = S.get_x11_window(sdl_window)
    if not display or not window:
        return None, None
    return display, window


def _make_window_no_focus_x11(sdl_window):
    """Mark the OSK as non-focusable to the X11 WM. Must run BEFORE
    SDL_ShowWindow  KWin reads WM_HINTS at map time. The SDL window is
    created with SDL_WINDOW_HIDDEN precisely to give us this window."""
    if not _IS_LINUX:
        return
    display, window = _x11_handles(sdl_window)
    if not display or not window:
        return
    x11 = _libx11()
    if x11 is None:
        return
    try:
        hints = _XWMHints(flags=_X11_INPUT_HINT, input=0)
        x11.XSetWMHints(display, window, ctypes.byref(hints))

        type_atom = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE", 0)
        dock_atom = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE_DOCK", 0)
        atom_val = (ctypes.c_ulong * 1)(dock_atom)
        # XA_ATOM=4, PropModeReplace=0, format=32, nelements=1
        x11.XChangeProperty(display, window, type_atom, 4, 32, 0,
                            ctypes.cast(atom_val, ctypes.c_void_p), 1)

        state_atom = x11.XInternAtom(display, b"_NET_WM_STATE", 0)
        skip_tb = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_TASKBAR", 0)
        skip_pg = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_PAGER", 0)
        above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", 0)
        states = (ctypes.c_ulong * 3)(skip_tb, skip_pg, above)
        x11.XChangeProperty(display, window, state_atom, 4, 32, 0,
                            ctypes.cast(states, ctypes.c_void_p), 3)
        x11.XFlush(display)
    except Exception as e:
        print(f"warning: X11 no-focus setup failed: {e!r}")


# --- EWMH active-window tracking, the X11 analog of GetForegroundWindow /
# SetForegroundWindow on Windows. A PHYSICAL mouse click on the OSK can, on some
# WMs, pull keyboard focus onto our window despite the input=0 dock hint  the
# next typed key would then miss the user's field. We read _NET_ACTIVE_WINDOW to
# remember the field and, if our own window ever becomes active, ask the WM to
# re-activate the field via a _NET_ACTIVE_WINDOW ClientMessage (sent to the root,
# so it can never raise BadWindow on a stale target).
_NET_WM_SOURCE_PAGER = 2  # EWMH source indication: direct user action
_SUBSTRUCTURE_REDIRECT = 1 << 20
_SUBSTRUCTURE_NOTIFY = 1 << 19
_X_CLIENT_MESSAGE = 33  # ClientMessage event type


class _XClientMessageData(ctypes.Union):
    _fields_ = [("b", ctypes.c_char * 20),
                ("s", ctypes.c_short * 10),
                ("l", ctypes.c_long * 5)]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _XClientMessageData),
    ]


class _XEvent(ctypes.Union):
    # Padded to the real XEvent union size (24 longs) so XSendEvent never reads
    # past the struct, whatever member it inspects.
    _fields_ = [
        ("type", ctypes.c_int),
        ("xclient", _XClientMessageEvent),
        ("pad", ctypes.c_long * 24),
    ]


def _x11_active_window(display):
    """Return the EWMH _NET_ACTIVE_WINDOW (the focused top-level), or 0."""
    x11 = _libx11()
    if x11 is None or not display:
        return 0
    try:
        root = x11.XDefaultRootWindow(display)
        prop = x11.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0)
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.c_void_p()
        # offset=0, length=1 long, delete=0, req_type=AnyPropertyType(0)
        status = x11.XGetWindowProperty(
            display, root, prop, 0, 1, 0, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(bytes_after),
            ctypes.byref(data))
        if status != 0 or not data or nitems.value < 1:
            if data:
                x11.XFree(data)
            return 0
        win = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0]
        x11.XFree(data)
        return win
    except Exception:
        return 0


def _x11_activate_window(display, win):
    """Ask the WM to give input focus back to `win` (a top-level the user was
    typing in) via a _NET_ACTIVE_WINDOW ClientMessage to the root window."""
    x11 = _libx11()
    if x11 is None or not display or not win:
        return
    try:
        root = x11.XDefaultRootWindow(display)
        msg_atom = x11.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0)
        ev = _XEvent()
        ev.xclient.type = _X_CLIENT_MESSAGE
        ev.xclient.send_event = 1
        ev.xclient.display = display
        ev.xclient.window = win
        ev.xclient.message_type = msg_atom
        ev.xclient.format = 32
        ev.xclient.data.l[0] = _NET_WM_SOURCE_PAGER
        ev.xclient.data.l[1] = 0  # timestamp = CurrentTime
        ev.xclient.data.l[2] = 0  # requestor's currently-active window (none)
        mask = _SUBSTRUCTURE_REDIRECT | _SUBSTRUCTURE_NOTIFY
        x11.XSendEvent(display, root, 0, mask, ctypes.byref(ev))
        x11.XFlush(display)
    except Exception:
        pass


class _XRectangle(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


# From <X11/extensions/shape.h>: ShapeInput is the shape kind that controls
# which pixels of a window accept pointer input (independent of the bounding
# shape used for drawing). ShapeSet replaces it outright.
_SHAPE_INPUT = 2
_SHAPE_SET = 0

_libxext_cache = None


def _libxext():
    global _libxext_cache
    if _libxext_cache is not None:
        return _libxext_cache
    try:
        lib = ctypes.cdll.LoadLibrary("libXext.so.6")
    except OSError:
        return None
    lib.XShapeCombineRectangles.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(_XRectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    lib.XShapeCombineRectangles.restype = None
    lib.XShapeCombineMask.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_ulong, ctypes.c_int,
    ]
    lib.XShapeCombineMask.restype = None
    _libxext_cache = lib
    return lib


def _set_x11_click_through(sdl_window, enabled):
    """Set/clear the OSK's X11 INPUT shape via the Shape extension.

    With an EMPTY input shape (enabled=True) the window still draws normally
    but the X server delivers no pointer events to it at all  motion, clicks
    and the scroll wheel land on whatever window is behind it, exactly like
    WS_EX_TRANSPARENT on Windows. ShapeCombineMask with a NULL pixmap
    (enabled=False) resets the input shape back to the window's default (the
    whole window), restoring normal OSK mouse interaction."""
    display, window = _x11_handles(sdl_window)
    if not display or not window:
        return
    xext = _libxext()
    if xext is None:
        return
    try:
        if enabled:
            xext.XShapeCombineRectangles(
                display, window, _SHAPE_INPUT, 0, 0, None, 0, _SHAPE_SET, 0)
        else:
            xext.XShapeCombineMask(
                display, window, _SHAPE_INPUT, 0, 0, 0, _SHAPE_SET)
        x11 = _libx11()
        if x11 is not None:
            x11.XFlush(display)
    except Exception as e:
        print(f"warning: X11 click-through shape failed: {e!r}")


def _hwnd_of(sdl_window):
    # SDL3 dropped SDL_GetWindowWMInfo for window properties; sdl3w wraps it.
    # On Linux this returns None (no win32 prop)  the Win32 path below is dead.
    return S.get_win32_hwnd(sdl_window)


# Win32 constants used by the focus / z-order helpers below (dead on Linux,
# kept so this file mirrors the Windows tree 1:1).
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST    = 0x00000008
# Click-through: the mouse (move/click/wheel) passes straight to the window
# behind. Toggled live so the OSK can become a pure touchpad-typing overlay
# while the sticks/mouse drive the desktop. WS_EX_TRANSPARENT alone only passes
# through to SIBLING windows in our own process; to pass to OTHER apps the
# window must ALSO be WS_EX_LAYERED. SDL3 makes SDL_WINDOW_TRANSPARENT via DWM
# (NOT a layered window  verified at runtime), so we add WS_EX_LAYERED here.
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED     = 0x00080000
_LWA_ALPHA         = 0x00000002
_SW_SHOWNOACTIVATE = 4
_SWP_NOMOVE        = 0x0002
_SWP_NOSIZE        = 0x0001
_SWP_NOACTIVATE    = 0x0010
_SWP_FRAMECHANGED  = 0x0020
_SWP_SHOWWINDOW    = 0x0040
# HWND_TOPMOST is the sentinel (-1) for SetWindowPos's hWndInsertAfter param.
_HWND_TOPMOST = ctypes.c_void_p(-1)


def _user32():
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
    user32.SetWindowPos.restype = ctypes.c_bool
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.ShowWindow.restype = ctypes.c_bool
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.AttachThreadInput.restype = ctypes.c_bool
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint]
    return user32


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _kernel32():
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


def _force_topmost(hwnd):
    """Make `hwnd` topmost without stealing focus (Windows only)."""
    user32 = _user32()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

    flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
    foreground = user32.GetForegroundWindow()
    fg_thread = (user32.GetWindowThreadProcessId(foreground, None)
                 if foreground else 0)
    cur_thread = kernel32.GetCurrentThreadId()

    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    try:
        user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags)
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)


def _set_always_on_top_portable(sdl_window):
    """Ask SDL to keep the window above others without stealing focus. Used on
    Linux/X11 to request _NET_WM_STATE_ABOVE."""
    try:
        S.SDL_SetWindowAlwaysOnTop(sdl_window, True)
    except Exception:
        pass


def _reassert_topmost(sdl_window):
    """Re-assert the OSK window's topmost z-order. The open animation's settle
    phase issues a burst of SDL_SetWindowPosition calls (~30 in well under a
    second); each is a chance for the WM to silently re-resolve z-order
    against another always-on-top window (e.g. a fullscreen game), which can
    leave the OSK visible but no longer the window that receives mouse input.
    Called once the animation finishes settling."""
    if _IS_WINDOWS:
        hwnd = _hwnd_of(sdl_window)
        if hwnd is not None:
            _force_topmost(hwnd)
    else:
        _set_always_on_top_portable(sdl_window)


def _make_window_non_activating(sdl_window):
    """On Windows: apply WS_EX_NOACTIVATE|TOOLWINDOW|TOPMOST to the HWND while
    hidden. On Linux the SDL "no activation" hint is a no-op for X11, so we
    patch WM_HINTS.input=False + _NET_WM_WINDOW_TYPE_DOCK via libX11 directly.
    The SDL window is still SDL_WINDOW_HIDDEN at this point  the WM only reads
    these atoms when the window is mapped."""
    if not _IS_WINDOWS:
        _make_window_no_focus_x11(sdl_window)
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        print("warning: win32 HWND lookup failed; window will steal focus")
        return
    user32 = _user32()
    current = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE,
                             current | _WS_EX_NOACTIVATE
                             | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST)
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
                        | _SWP_FRAMECHANGED)


def _reposition_bottom_center(sdl_window):
    """Re-assert the OSK at the bottom-center of the primary display's usable
    area AFTER it's mapped. Screen.__init__ already positions it while hidden,
    but some compositors (notably under Wayland/XWayland) ignore a position set
    on a still-hidden window and map it centered  setting it again once shown
    fixes that on X11."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
        win_x = bounds.x + max(0, (bounds.w - screen.width) // 2)
        win_y = bounds.y + max(0, bounds.h - screen.height - screen.TASKBAR_GAP)
        S.SDL_SetWindowPosition(sdl_window, win_x, win_y)


def _set_click_through(sdl_window, enabled):
    """When enabled the mouse ignores the OSK entirely  moves, clicks and the
    scroll wheel fall through to the app behind it  so the right-stick mouse
    and left-stick scroll drive the desktop while the touchpads still type on
    the keyboard.

    On Windows this adds/removes WS_EX_TRANSPARENT (+LAYERED) on the OSK HWND.
    On X11 it's done via _set_x11_click_through (an empty Shape-extension INPUT
    region), since there's no HWND."""
    if not _IS_WINDOWS:
        _set_x11_click_through(sdl_window, enabled)
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        return
    user32 = _user32()
    current = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    bits = _WS_EX_LAYERED | _WS_EX_TRANSPARENT
    new = (current | bits) if enabled else (current & ~bits)
    if new == current:
        return
    user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, new)
    if enabled:
        # A freshly-layered window has undefined alpha until we set it; force it
        # fully opaque so the keyboard stays visible (uniform alpha overrides the
        # per-pixel transparent-skin see-through while click-through is active).
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA)
    # Flush the latent ex-style change so hit-testing picks it up immediately.
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
                        | _SWP_FRAMECHANGED)


def _show_window_noactivate(sdl_window, pos=None):
    """Bring the OSK on screen without stealing focus and force it topmost.

    On X11 we use SDL's portable show + always-on-top; combined with the
    WM_HINTS.input=False + _NET_WM_WINDOW_TYPE_DOCK atoms set above, most
    compositors skip focus on map.

    `pos`, if given, is the (x, y) to re-assert once mapped  used by the open
    animation to keep the window at its RAISED starting position (a
    hidden-window position can otherwise be ignored by the compositor and
    re-mapped centered, or reset back to rest here, which would skip the
    animation's downward settle). If None, re-assert the remembered Move-key
    rest position instead (at index 0 this is the default bottom-center
    spot)."""
    if not _IS_WINDOWS:
        S.SDL_ShowWindow(sdl_window)
        _set_always_on_top_portable(sdl_window)
        if pos is not None:
            S.SDL_SetWindowPosition(sdl_window, pos[0], pos[1])
        else:
            _apply_window_position(sdl_window)
        # Block until the WM has actually mapped/positioned the window before
        # the open animation starts presenting frames. Without this, the first
        # animation frame(s) can render while the window is still at its old
        # (or default-centered) position/visibility under XWayland, producing
        # a one-frame flash/jump at the start of the animation.
        S.SDL_SyncWindow(sdl_window)
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        S.SDL_ShowWindow(sdl_window)
        return
    user32 = _user32()
    user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    _force_topmost(hwnd)


_ASFW_ANY = ctypes.c_ulong(-1).value


def _restore_foreground(hwnd):
    """Re-focus the window the user was typing in before the OSK opened.

    On Windows a controller-open fires the firmware lizard's mouse-click, which
    can land off the target field and steal focus; the saved window is then
    re-activated (the OSK is NOACTIVATE, so it never takes focus itself).

    TODO(linux): port the capture (tray) + an X11 restore here via
    _NET_ACTIVE_WINDOW / XSetInputFocus. For now this is a no-op off Windows,
    and the saved target is always None on Linux, so nothing changes."""
    if not _IS_WINDOWS or not hwnd:
        return
    user32 = _user32()
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        return
    try:
        user32.AllowSetForegroundWindow(_ASFW_ANY)
    except Exception:
        pass
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    try:
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)


# The SDL VIDEO subsystem is inited once per process and kept up across OSK
# opens (see main()). Re-initing it costs ~400 ms (it rebuilds the XWayland
# connection + video driver), which dominated open latency; keeping it inited
# while the OSK is closed costs no CPU (no window, no render loop)  just an
# idle X connection. False until the first open inits it.
_video_inited = False

# Index into the 6-position window rotation, advanced by Shift+Move.
# 0 starts at down-mid (the default open location).
_position_index = [0]
# Index of the up-right spot in _apply_window_position's seq, used to force
# the OSK there on open (without disturbing _position_index) when the Windows
# Start menu is covering its usual spot.
_POS_UP_MID = 4

# Processes that host the Start menu and its search-results view, across
# different Windows builds. On 24H2+ both the Start launcher and its search
# view run as SearchApp.exe; older builds use StartMenuExperienceHost/
# SearchHost. While typing into Start's search box (e.g. via the OSK), focus
# hands between these without the menu visually closing, so
# _is_start_menu_open() must treat any of them as "Start is open".
_START_MENU_PROCESSES = {
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe"}

# Process that hosts the Windows emoji/symbol picker (Win+.). When the picker is
# open it takes the foreground; used to notice the user closing it by ANY means
# (its own close button, a pick, click-away) so the emoji desktop-mode override
# can be lifted even though only the OSK emoji key flips our flag directly.
# (Inert on Linux: _foreground_process_name returns None there  the picker is
# DE-specific; the flag still clears via the emoji key / OSK close.)
_EMOJI_PICKER_PROCESSES = {"textinputhost.exe"}
# How often to poll the foreground process while the emoji picker is open.
_EMOJI_CHECK_INTERVAL = 0.2


def _foreground_process_name():
    """Lowercase exe name of the foreground window's process, or None (off
    Windows, or if the lookup fails)."""
    if not _IS_WINDOWS:
        return None
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


# Foregrounds that must never own a per-app OSK memory entry: our own
# windows, the desktop shell, and Steam. They are what is focused WHILE the
# keyboard is being set up, so remembering against them would overwrite the
# entry of the app the user was actually typing into.
_NON_APP_PROCESSES = {
    "steam", "steamwebhelper", "steamlessinput", "python3", "python",
    "gnome-shell", "plasmashell", "kwin_x11", "kwin_wayland", "xfdesktop",
}


def _x11_active_process(display):
    """Lowercase process name of the EWMH-active window, or None.

    The Windows path asks the OS for the foreground window's exe; the X11
    equivalent is _NET_ACTIVE_WINDOW -> _NET_WM_PID -> /proc/<pid>/comm. Every
    step is optional  a WM that doesn't set _NET_WM_PID, or a Wayland session
    with no X11 display, simply yields None and the caller falls back to the
    global OSK settings."""
    x11 = _libx11()
    win = _x11_active_window(display)
    if x11 is None or not display or not win:
        return None
    try:
        prop = x11.XInternAtom(display, b"_NET_WM_PID", 1)
        if not prop:
            return None
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.c_void_p()
        status = x11.XGetWindowProperty(
            display, win, prop, 0, 1, 0, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(bytes_after),
            ctypes.byref(data))
        if status != 0 or not data or nitems.value < 1:
            if data:
                x11.XFree(data)
            return None
        pid = int(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0])
        x11.XFree(data)
    except Exception:
        return None
    if pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            return f.read().strip().lower() or None
    except OSError:
        return None


# The X11 display the OSK session opened, published by main() so _per_app_key
# can reach it from module scope (it runs before and after the loop).
_x11_display = [None]


def _per_app_key():
    """The process name the OSK's per-app memory should key off, or None when
    the foreground isn't a real app to remember against ("Remember Per App"
    off, a shell surface, or the lookup failed)."""
    if not state.is_per_app_memory_enabled():
        return None
    name = _x11_active_process(_x11_display[0]) or _foreground_process_name()
    if not name or name in _NON_APP_PROCESSES:
        return None
    return name


def _is_start_menu_open():
    """True if the Windows Start menu (or its search-results view) is
    currently open and focused. Start opens from the bottom-center and grows
    upward, covering the keyboard at its usual remembered spot  so the open
    call sites force _POS_UP_MID instead while this is true."""
    return _foreground_process_name() in _START_MENU_PROCESSES


# How often to re-check _is_start_menu_open() while the OSK is already visible,
# to live-reposition if the Start menu opens/closes underneath it. Cheap
# (GetForegroundWindow + a process-name lookup), but not free, so this is
# throttled rather than checked every frame  Start opening/closing is a
# human-timescale event.
_START_MENU_POLL_INTERVAL = 0.2

# How long the close "X" (top-right corner) stays visible after the last mouse
# move over the OSK. Long enough to aim for and click it, short enough that it
# disappears once the cursor settles or leaves.
_CLOSE_X_SHOW_SECS = 1.8

# Free mouse drag  grab the keyboard and slide it anywhere on the desktop,
# instead of stepping through the 6 fixed slots. Two grabs, both driven by the
# same machinery (_begin_drag / _drag_motion / _end_drag):
#   • LEFT-drag the "Move" key, the key that already owns window placement. A
#     press that never travels this far still fires Move's normal action on
#     release (close the OSK, or cycle the slot with Shift held), so a click on
#     it behaves exactly as before  only a real drag takes the window.
#   • MIDDLE-drag anywhere on the keyboard, for grabbing it without aiming
#     (the middle button had no other OSK job).
# X11 only in practice: Wayland doesn't let a client place its own toplevels,
# so SDL_SetWindowPosition (and with it every slot in the rotation above) is
# already a no-op there.
_DRAG_THRESHOLD_PX = 6
# Never let a drag park the keyboard so far off the desktop that there's nothing
# left to grab: this much of it always stays within the display union.
_DRAG_MIN_VISIBLE_PX = 90


# OSK OPEN animation (see screen.render_open_anim + main's render loop), over
# _OPEN_ANIM_SECS. Three eased effects, each on its own slice of the timeline:
#   • opacity 0→100% within the first _OPEN_ANIM_FADE_FRAC (a gradual fade-in);
#   • the bottom _OPEN_ANIM_CUT_PX, hidden at the start, revealed as the cut
#     slides down over the first _OPEN_ANIM_REVEAL_FRAC;
#   • then a _OPEN_ANIM_DROP_PX downward settle into the final position, begun at
#     _OPEN_ANIM_MOVE_START_FRAC (earlier than the reveal end, so they overlap)
#     and finishing at the end.
# Tuned to feel like the keyboard rising into place.
_OPEN_ANIM_SECS = 0.40
_OPEN_ANIM_CUT_PX = 140
_OPEN_ANIM_DROP_PX = 35
_OPEN_ANIM_REVEAL_FRAC = 0.66
_OPEN_ANIM_FADE_FRAC = 1.0          # opacity fades 0→100% across the WHOLE open
                                   #  a gradual fade-in (~0.40s, ~3.4x longer
                                   # than the old 0.117s first-third fade)
_OPEN_ANIM_MOVE_START_FRAC = 0.33  # downward settle starts here (earlier)

# OSK CLOSE animation: fade out with a slight scale-down, so closing is as
# finished-feeling as opening. Deliberately much shorter than the open and
# deliberately NOT springy  a close that overshoots reads as a glitch, and a
# slow one just delays whatever the user pressed close to get to. The window
# is never MOVED during the fade: shifting a layered per-pixel-alpha window
# mid-fade makes DWM re-composite stale opaque content, which shows as a flash.
_CLOSE_ANIM_SECS = 0.16
_CLOSE_ANIM_SCALE = 0.08   # how far it shrinks by the end (8%)
_CLOSE_ANIM_FRAME = 1.0 / 120.0   # pace the fade, never busy-spin it


def _ease_out_cubic(t):
    """Decelerating ease (fast start, soft landing) on a 0..1 progress."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return 1.0 - (1.0 - t) ** 3


def _apply_window_position(sdl_window, index=None):
    """Move the OSK to the spot for `index` (default: the CURRENT
    _position_index, 0 = down-mid). Used to restore the remembered position
    when the OSK (re)opens  also re-asserting it after the window maps, which
    some Wayland/XWayland compositors require  after advancing the index by
    _cycle_window_position, or to force _POS_UP_MID on open when the Start
    menu is covering the usual spot (see _is_start_menu_open).

    A spot the user dragged the keyboard to with the mouse (_free_position)
    overrides the rotation whenever no explicit `index` is asked for  re-clamped
    each time, since the size and the display layout can both change under it."""
    if index is None and _free_position[0] is not None:
        pos = _clamp_to_desktop(*_free_position[0])
        _free_position[0] = pos
        S.SDL_SetWindowPosition(sdl_window, pos[0], pos[1])
        return pos
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if not (disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds))):
        return None
    w = screen.width
    h = screen.height
    x_left = bounds.x
    x_mid = bounds.x + max(0, (bounds.w - w) // 2)
    x_right = bounds.x + max(0, bounds.w - w)
    y_top = bounds.y
    # TASKBAR_GAP of daylight between the keyboard and the taskbar, instead of
    # sitting flush on it, at every down-* slot in the position cycle.
    y_bot = bounds.y + max(0, bounds.h - h - screen.TASKBAR_GAP)
    # 0 down-mid (start) → 1 down-left → 2 up-left → 3 up-mid → 4 up-right → 5 down-right → 0.
    seq = [
        (x_mid,   y_bot),
        (x_left,  y_bot),
        (x_left,  y_top),
        (x_mid,   y_top),
        (x_right, y_top),
        (x_right, y_bot),
    ]
    x, y = seq[_position_index[0] if index is None else index]
    S.SDL_SetWindowPosition(sdl_window, x, y)
    # The resting (x, y)  the open animation settles the window down INTO this.
    return (x, y)


def _cycle_window_position(sdl_window, app_key=None):
    # Cycling puts the keyboard back on the 6-slot rotation, so a free
    # (mouse-dragged) spot is dropped  the slot the index names wins.
    _free_position[0] = None
    if state.is_split_layout_enabled():
        # A split board spans the display, so left/mid/right all resolve to the
        # same x  the rotation collapses to DOWN (0/1/5) and UP (2/3/4), and
        # Move just alternates between them.
        _position_index[0] = 0 if _position_index[0] not in (0, 1, 5) else 3
    else:
        _position_index[0] = (_position_index[0] + 1) % 6
    # "Remember Per App": this is the spot the user chose for the app they are
    # typing into, so it is what should come back next time.
    state.note_per_app(app_key, "position", _position_index[0])
    _apply_window_position(sdl_window)


# ---------------------------------------------------------------------------
# Free mouse drag (see _DRAG_THRESHOLD_PX)
# ---------------------------------------------------------------------------
# Where a mouse drag last dropped the keyboard, or None while it's following the
# 6-slot rotation. Set on drop; cleared by the next Move-key cycle or by an
# explicit position request (the lock-screen jump). Re-applied  re-clamped for
# the current size  every time the OSK re-opens, so a drag sticks for the
# session exactly like _position_index does.
_free_position = [None]


def _desktop_bounds():
    """The whole virtual desktop as (x, y, w, h): the union of every display's
    FULL bounds. Full, not usable, bounds  a deliberate drag is allowed to park
    the keyboard over the panel/dock. Falls back to the primary display alone,
    then to None if even that can't be read."""
    rects = []
    count = ctypes.c_int(0)
    ids = S.SDL_GetDisplays(ctypes.byref(count))
    if ids:
        try:
            for i in range(count.value):
                r = S.SDL_Rect()
                if S.SDL_GetDisplayBounds(ids[i], ctypes.byref(r)):
                    rects.append(r)
        finally:
            S.SDL_free(ids)
    if not rects:
        r = S.SDL_Rect()
        disp = S.SDL_GetPrimaryDisplay()
        if not (disp and S.SDL_GetDisplayBounds(disp, ctypes.byref(r))):
            return None
        rects.append(r)
    x0 = min(r.x for r in rects)
    y0 = min(r.y for r in rects)
    x1 = max(r.x + r.w for r in rects)
    y1 = max(r.y + r.h for r in rects)
    return (x0, y0, x1 - x0, y1 - y0)


def _clamp_to_desktop(x, y, bounds=None):
    """Pin a dragged window origin to somewhere it can still be grabbed:
    _DRAG_MIN_VISIBLE_PX of the keyboard stays on the desktop horizontally, and
    it may hang off the bottom but never above the top edge (the Move key lives
    in the bottom row  letting the top slide away keeps the handle reachable).

    `bounds` is a pre-read _desktop_bounds(); a drag passes the one it took at
    grab time rather than re-enumerating the displays on every motion event."""
    if bounds is None:
        bounds = _desktop_bounds()
    if bounds is None:
        return (int(x), int(y))
    bx, by, bw, bh = bounds
    keep_x = min(screen.width, _DRAG_MIN_VISIBLE_PX)
    keep_y = min(screen.height, _DRAG_MIN_VISIBLE_PX)
    x = min(max(x, bx - (screen.width - keep_x)), bx + bw - keep_x)
    y = min(max(y, by), by + bh - keep_y)
    return (int(x), int(y))


def _window_origin(sdl_window):
    """The window's current desktop (x, y), or None if SDL won't say."""
    x = ctypes.c_int(0)
    y = ctypes.c_int(0)
    if not S.SDL_GetWindowPosition(sdl_window, ctypes.byref(x), ctypes.byref(y)):
        return None
    return (x.value, y.value)


def _is_move_cell(virtual_kb, rc):
    """True if the (row, col) cell is the Move key  the OSK's drag handle.
    Identified by its behaviour, not its label, so it tracks the layout."""
    if rc is None:
        return False
    row, col = rc
    if not (0 <= row < len(virtual_kb.keys) and 0 <= col < len(virtual_kb.keys[row])):
        return False
    return virtual_kb.keys[row][col].callback is vkb.on_key_move


def _begin_drag(sdl_window, x, y, button, tap_cell):
    """Grab the window for a free drag at the window-relative point (x, y).

    `tap_cell` is the (row, col) whose key should fire if the grab never travels
    _DRAG_THRESHOLD_PX (the Move key's own close/cycle action); None for a grab
    with no tap meaning of its own (the middle-button one). Returns the drag
    record, or None if the window position can't be read  no drag at all beats
    a drag that jumps the keyboard somewhere unpredictable."""
    origin = _window_origin(sdl_window)
    if origin is None:
        return None
    # Capture so a fast grab that outruns the window keeps its motion  and,
    # crucially, its release  instead of handing them to the window behind.
    S.SDL_CaptureMouse(True)
    return {"button": button, "grab": (x, y), "press": (x, y),
            "origin": origin, "moved": False, "tap": tap_cell,
            # A grab that hasn't moved by then is a HOLD of the key underneath,
            # not a drag (see the loop's hold-commit): the corner zones sit on
            # real keys, and Backspace in the top-right one has to keep
            # repeating. Only meaningful for a grab that has a tap key at all.
            "hold_at": (float("inf") if tap_cell is None
                        else time.monotonic() + vkb.KEY_REPEAT_DELAY),
            # Read once here: the clamp runs on every motion event, and
            # enumerating the displays that often is pure overhead.
            "bounds": _desktop_bounds()}


def _drag_motion(sdl_window, drag, mpos):
    """Slide a grabbed window so the grabbed point stays under the cursor.

    `mpos` is the motion event's WINDOW-relative position; added to the origin we
    last set ourselves it recovers the cursor's absolute desktop position. That
    self-consistency is the point: the OS emits a motion event every time the
    window slides under a stationary cursor, and those report a moved cursor in
    window coordinates  resolved back to absolute they land on the same spot, so
    they can't feed back and run the window away. Off Windows that only holds if
    the move has actually been applied, hence the SDL_SyncWindow (X11/XWayland
    apply window moves asynchronously; see the binding's note).

    Returns True once the grab has travelled far enough to count as a drag."""
    if not drag["moved"]:
        px, py = drag["press"]
        if (abs(mpos[0] - px) < _DRAG_THRESHOLD_PX
                and abs(mpos[1] - py) < _DRAG_THRESHOLD_PX):
            return False
        drag["moved"] = True
    ox, oy = drag["origin"]
    gx, gy = drag["grab"]
    pos = _clamp_to_desktop(ox + mpos[0] - gx, oy + mpos[1] - gy, drag["bounds"])
    drag["origin"] = pos
    S.SDL_SetWindowPosition(sdl_window, pos[0], pos[1])
    if not _IS_WINDOWS:
        S.SDL_SyncWindow(sdl_window)
    return True


def _end_drag(drag):
    """Let go. Returns the tap (row, col) when the grab never became a drag  the
    caller then fires that key, so a click on Move still closes/cycles. Otherwise
    the drop point becomes the remembered window position and None comes back."""
    S.SDL_CaptureMouse(False)
    if not drag["moved"]:
        return drag["tap"]
    _free_position[0] = drag["origin"]
    return None


def _begin_open_anim(scr, virtual_kb, controller_state, rest):
    """Prime the OSK open animation: pre-render the invisible first frame (so the
    just-shown window never flashes its background), then raise the window by the
    settle distance so the animation can drop it back into place. Returns the
    monotonic start time, or None if the animation can't run (no display bounds
    or no GPU render target)  the caller then just shows the keyboard normally.

    `rest` is the resting (x, y) from _apply_window_position."""
    if rest is None:
        return None
    pointers = controller_state.get_pointers()
    # fade=0 + full cut => a fully transparent frame: the window is invisible the
    # instant it's shown, then the loop fades/reveals it in.
    if not scr.render_open_anim(virtual_kb, pointers, 0.0, _OPEN_ANIM_CUT_PX):
        return None
    rx, ry = rest
    if _IS_WINDOWS:
        S.SDL_SetWindowPosition(scr.window, rx, ry - _OPEN_ANIM_DROP_PX)
    else:
        # Map the window OFF-SCREEN (below the display). On X11/XWayland the
        # first frame KWin composites for a newly-mapped window is an opaque
        # black backbuffer regardless of what we've already Present()ed into
        # it -- that's the black flash before the OSK fades in. Our window is
        # _NET_WM_WINDOW_TYPE_DOCK, which KWin positions exactly where the app
        # asks (panels self-place) instead of clamping it on-screen, so we let
        # that black map-frame happen off-screen; _finish_open_anim_map then
        # moves the window (now holding the correct transparent buffer) onto
        # its raised start position. This replaces an earlier SetWindowOpacity
        # hide whose 0->1 reveal triggered KWin's compositor Fade effect on top
        # of our own animation -- a visibly slower, less responsive open.
        S.SDL_SetWindowPosition(scr.window, rx, _open_anim_offscreen_y(ry))
    return time.monotonic()


def _open_anim_offscreen_y(ry):
    """Window-top Y that parks the OSK fully BELOW the display, so the black
    map-frame (see _begin_open_anim) stays off-screen no matter WHERE the OSK
    will rest. Anchored to the display's bottom edge  NOT to `ry`  because the
    Move key can put the rest at the TOP of the screen (ry ≈ 0), where the old
    `ry + height` landed mid-screen and the black frame flashed there. Pushing a
    full window-height past the usable bottom also clears any taskbar/panel gap
    between the usable area and the physical screen edge. `ry` is only the
    fallback when the display bounds are unavailable."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
        return bounds.y + bounds.h + screen.height + 50
    return ry + screen.height + 50


def _open_anim_show_pos(open_anim_start, open_anim_rest):
    """The (x, y) _show_window_noactivate should re-assert once the window is
    mapped. If an open animation just started, that's the position
    _begin_open_anim moved the (still-hidden) window to  re-asserting `rest`
    here instead would snap the window straight to its final spot and skip the
    animation. On Windows that's the RAISED start position; on X11 it's the
    OFF-SCREEN map position (the black map-frame stays off-screen, then
    _finish_open_anim_map moves it on-screen). Otherwise it's just `rest`."""
    if open_anim_start is None or open_anim_rest is None:
        return open_anim_rest
    rx, ry = open_anim_rest
    if _IS_WINDOWS:
        return (rx, ry - _OPEN_ANIM_DROP_PX)
    return (rx, _open_anim_offscreen_y(ry))


def _finish_open_anim_map(scr, virtual_kb, controller_state,
                          open_anim_start, open_anim_rest):
    """X11 only: after the window was shown+synced off-screen (so its black
    map-frame stayed off-screen), re-present the transparent open frame and
    move the window onto its raised on-screen start position to begin the
    animation. No-op on Windows or when the animation isn't running."""
    if open_anim_start is None or open_anim_rest is None or _IS_WINDOWS:
        return
    # Re-present the transparent open frame, then prime EVERY swap-chain buffer
    # with a transparent frame while the window is still off-screen  otherwise
    # the compositor's first on-screen composite occasionally lands on an
    # unpresented (black) back-buffer, flashing a one-frame black box (~1 in N
    # opens) over the desktop in transparent mode. See prime_open_anim_buffers.
    scr.render_open_anim(virtual_kb, controller_state.get_pointers(),
                         0.0, _OPEN_ANIM_CUT_PX)
    scr.prime_open_anim_buffers()
    rx, ry = open_anim_rest
    S.SDL_SetWindowPosition(scr.window, rx, ry - _OPEN_ANIM_DROP_PX)


# Layout name -> its YAML file under data/cfg. "classic" is the shipped
# Steam-style board and doubles as the fallback for anything unreadable.
_LAYOUT_FILES = {
    "classic": "keyboard-layout.yaml",
    "phone": "keyboard-layout-phone.yaml",
    "full75": "keyboard-layout-75.yaml",
}


# Horizontal MOUSE travel (px) that fires one Shift+Left/Right while the Select
# key is held. The mouse moves in screen pixels, so the step is far smaller
# than the pad's raw-unit equivalent  a few px per character reads as precise.
_SELECT_MOUSE_DRAG_STEP = 48


def _end_mouse_select():
    """End a mouse Select session if one is running, dropping the OS Shift it
    was holding. Returns the new anchor (always None) so the caller can assign
    it in one line."""
    if state.is_select_active():
        vkb.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_select_active(False)
    return None


def _flush_mouse_deferred(virtual_kb, cell):
    """Finish a mouse press that was deferred because it landed on a letter
    with accented variants.

    If the hold opened that key's row, commit the highlighted variant  the
    base was never typed, so nothing has to be rubbed out, and a release with
    no pick takes the FIRST variant (the row's model is "only accented letters
    are selectable"). Otherwise it was a quick tap: type the base, silently,
    because the press edge already clicked. Returns the new deferred cell
    (always None)."""
    if state.is_diacritic_open() and state.get_diacritic_source() == "mouse":
        char = state.get_diacritic_selected_char()
        if char is None:
            variants = state.get_diacritic_variants_list()
            char = variants[0] if variants else None
        if char is not None:
            vkb.commit_diacritic(char)
        else:
            state.close_diacritic()
    elif cell is not None:
        state.queue_key_press(*cell, silent=True)
    return None


def load_kb_config():
    """Read the selected layout. The phone layout is multi-page, so the file's
    `pages` map is handed through as well; the single-page layouts have only
    `keys`. An unreadable non-classic layout falls back to the classic one
    rather than leaving the OSK with nothing to draw."""
    kb_config = vkb.VirtualKeyboardConfig()
    want = state.get_osk_layout()
    name = _LAYOUT_FILES.get(want, _LAYOUT_FILES["classic"])
    try:
        kb_layout_file = config.YamlFile(name)
        kb_layout_file.read()
    except Exception as e:
        if name == _LAYOUT_FILES["classic"]:
            raise
        print(f"adusk: cannot load {name} ({e!r}); using the classic layout")
        kb_layout_file = config.YamlFile(_LAYOUT_FILES["classic"])
        kb_layout_file.read()
    if "pages" in kb_layout_file.yaml_data:
        kb_layout_file.add_to_config("pages", kb_config)
    else:
        kb_layout_file.add_to_config("keys", kb_config)
    return kb_config


def main(preview=False):
    # `preview=True` means this open is a live Options-tab size/transparency
    # preview (the user is dragging a slider in the picker): show the keyboard
    # INSTANTLY with no open animation  the fade/reveal glitches while the
    # window is being resized / re-skinned under it on the same frames.
    # NOTE: _position_index is NOT reset here  the Move-key window position is
    # remembered across OSK opens within a session and only resets to down-mid
    # on a program restart (when this module is freshly imported). It's restored
    # in _show_window_noactivate when the window maps.

    # Enter high-responsiveness mode for as long as the OSK is open (Windows-only;
    # a no-op here on Linux): opt the process out of EcoQoS + raise the timer to
    # 1 ms and pin this render/input thread. Released at teardown so a closed OSK
    # goes back to background power. Reference-counted; mirrors the Windows tree.
    power.request()
    power.boost_current_thread()

    controller_state = controller.ControllerState()
    controller_state.set_pointers(
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(1/4, 1/2)),
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(3/4, 1/2))
    )

    # Every page of the selected layout. Classic layouts have the single page
    # "main"; the phone layout has abc / sym1 / sym2, switched live by its
    # ?123, =\< and ABC keys (see the page follow below). Always open on the
    # layout's FIRST page, so the keyboard never comes back up on a symbol
    # page from last time.
    kb_pages = load_kb_config().construct_pages()
    _first_page = next(iter(kb_pages))
    state.set_osk_page(_first_page)
    virtual_kb = kb_pages[_first_page]
    # Tracks the layout kb_pages was last built from, so the loop below can
    # rebuild it live when the Keyboard Layout dropdown's hover preview (or a
    # real pick) changes state.get_osk_layout() while this OSK is on screen.
    last_layout = state.get_osk_layout()
    # Publish per-row key counts so the controller thread can clamp DPAD
    # navigation against the actual layout.
    state.set_grid_dims([len(r) for r in virtual_kb.keys])
    # Pull the Swipe Typing lexicon in on a background thread now, so the cost
    # lands during the open instead of on the first swipe's lift.
    if state.is_swipe_typing_enabled():
        swipe.warm_up()

    # Keep gamepad input flowing while the OSK window is up. Our window is
    # no-focus (X11 WM_HINTS.input=False / _NET_WM_WINDOW_TYPE_DOCK), and SDL by
    # default DROPS joystick/gamepad events whenever no SDL window has input
    # focus  which froze every SDL pad (Switch Pro/Xbox/...) to all-zero buttons
    # the instant the OSK opened (the tray's poll reads stale state then), so the
    # OSK got zero controller input. Must be set before the gamepad event pump.
    S.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
    # Keep SDL's HIDAPI driver off the Steam Controller  we own the SC via our
    # steamcontroller HID backend, and SDL3 grabbing it (Triton PIDs) blocks our
    # exclusive open (see tray / block_sc_hid). Under the tray this is already
    # set before its SDL_Init; set it here too for the standalone OSK path.
    S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"0")
    # NOTE: unlike Windows, SDL_JOYSTICK_HIDAPI_STEAMDECK stays ON here  the
    # Linux runtime (tray_linux.py) has no HID-takeover watcher yet, so the
    # Steam Deck's built-in pad is still driven as an SDL pad on this platform
    # (see pads.HID_KINDS). Flip this with the takeover port.
    # Our OSK window never takes input focus; by default SDL treats a click on an
    # unfocused window as a focus-gaining click and swallows it, so no button
    # event reaches the loop and mouse clicks never type. This hint makes SDL
    # deliver the button event anyway. (Win32 NOACTIVATE makes this critical
    # there; harmless and consistent to set on every platform.)
    S.SDL_SetHint(b"SDL_MOUSE_FOCUS_CLICKTHROUGH", b"1")
    # SDL3: bring up video+events+gamepad BEFORE the input thread starts, so its
    # Sdl3GamepadSource can safely poll. InitSubSystem/QuitSubSystem (not
    # SDL_Init/SDL_Quit) so a tray-owned persistent SDL_INIT_GAMEPAD survives
    # the OSK closing; refcounted, balances the QuitSubSystem at teardown.
    #
    # VIDEO is inited ONCE per process and deliberately never quit (see
    # teardown). Re-initing it costs ~400 ms  it rebuilds the XWayland
    # connection + video driver  which dominated the OSK's open latency and
    # made rapid open/close sluggish. Keeping it inited between opens costs no
    # CPU: there's no window and no render loop while the OSK is closed (main()
    # returns and the window/renderer are destroyed), just an idle X connection
    # held open. The keyboard still fully disappears on close.
    global _video_inited
    if not _video_inited:
        if not S.SDL_InitSubSystem(S.SDL_INIT_VIDEO):
            raise RuntimeError("SDL_InitSubSystem(VIDEO) failed: " + S.get_error())
        _video_inited = True
    if not S.SDL_InitSubSystem(S.SDL_INIT_EVENTS | S.SDL_INIT_GAMEPAD):
        raise RuntimeError("SDL_InitSubSystem failed: " + S.get_error())
    if not S.TTF_Init():
        raise RuntimeError("TTF_Init failed: " + S.get_error())

    sc_thread = Thread(target=controller.input_thread,
                       args=(controller_state,),
                       kwargs={"closing_haptic": not preview, "preview": preview},
                       daemon=True)
    sc_thread.start()

    scr = screen.Screen()
    # Tag the still-hidden window as non-focusable (X11 WM hints / Win32
    # NOACTIVATE), then bring it on screen without stealing focus.
    _make_window_non_activating(scr.window)

    # --- Live OSK size --------------------------------------------------
    # Two things drive the size while the OSK is open: a temporary "small"
    # override whenever the Start menu is open (we also force the position to
    # up-mid below  both dormant on Linux, where _is_start_menu_open() is
    # always False), and live "Keyboard Skin -> Size" changes from the tray
    # menu. Both resize the SINGLE existing window IN PLACE (Screen.resize)
    # rather than building a second window mid-session  creating a new
    # transparent window while one is already showing proved unreliable (it
    # could come up invisible). base_size is the size scr was built at.
    base_size = screen.get_osk_size()
    current_size = [base_size]

    def _apply_size(want):
        # Re-assert when the requested size changed OR the module globals have
        # drifted from it (a stale resize event, a prior session, ...), so the
        # OSK can't get stuck rendering at the wrong width across reopens.
        if (want != current_size[0]
                or (screen.width, screen.height) != screen._compute_size(want)):
            scr.resize(want)
            current_size[0] = want
            # Every page, not just the visible one  key sizes derive from the
            # window size, so a page switched to later would otherwise still be
            # laid out for the old one.
            for _pg in kb_pages.values():
                _pg.update_dimensions()

    # Place the window at its resting spot, then prime the open animation
    # (pre-renders the invisible first frame + raises the window) BEFORE showing,
    # so the keyboard fades/rises in instead of popping. open_anim_start is None
    # if it can't run → plain instant show. If the Start menu is open, force
    # up-mid instead (without touching _position_index) so its
    # search-results panel doesn't cover the keyboard.
    start_menu_open = _is_start_menu_open()
    # "Remember Per App": reopen with the size, skin and spot this app was last
    # left at. Resolved BEFORE the size/position are applied, so the keyboard
    # never appears at the global look and then jumps. Captured once for the
    # session  the foreground is stable from here on, and re-reading it per
    # frame would let a transient focus blip rewrite the wrong app's entry.
    # _per_app_key reads the active window through X11, so hand it this
    # session's display before the first lookup (the handles are re-fetched
    # below for the focus-restore path, which needs the OSK's own window too).
    if _IS_LINUX:
        _x11_display[0] = _x11_handles(scr.window)[0]
    # --- open-time repairs on OS-level state -------------------------------
    # All three survive an OSK close, because they are OS / library state
    # rather than session state, and each one silently breaks ALL typing until
    # something clears it.
    #   * Caps Lock ON makes every key come out uppercase, and the on-screen
    #     Caps key can't fix it when a remapper (PowerToys Caps->Esc) eats the
    #     keystroke  force_caps_off sends the raw scancode instead.
    #   * A dropped modifier key-up from a previous session leaves Ctrl or Alt
    #     physically held, turning every later key into a shortcut. Now that
    #     the 75% board's Ctrl/Alt keys latch real OS modifiers, this is a
    #     reachable state, not a theoretical one. (Shift is deliberately left
    #     alone: the user may be holding it legitimately.)
    #   * The module-global pynput Keyboard outlives the session, and the raw
    #     SendInput paths (the accent commit's paste) don't update its INTERNAL
    #     modifier set  a stale entry there types every letter as a chord.
    # Each is internally defensive (they swallow their own backend errors), so
    # one shared guard is enough and a failure here can never stop the OSK
    # from opening.
    try:
        vkb.kb.force_caps_off()
        vkb.kb._release_stuck_ctrl()
        vkb.kb.reset_modifier_state()
    except Exception as e:
        print(f"adusk: open-time modifier repair skipped ({e!r})")
    # Publish the board for the input thread NOW, not at the first render: the
    # controller thread starts before that and its press-to-focus lock and
    # accent holds both need to resolve a pixel position to a key cell.
    state.set_virtual_kb(virtual_kb)

    app_key = _per_app_key()
    if app_key is not None:
        _remembered = state.get_per_app_skin(app_key)
        if _remembered and _remembered != skins.get_active_skin():
            skins.set_active_skin(_remembered)
        _remembered = state.get_per_app_size(app_key)
        if _remembered:
            screen.set_osk_size(_remembered)
        _remembered = state.get_per_app_position(app_key)
        if _remembered is not None:
            _position_index[0] = _remembered
            _free_position[0] = None
    # What this session STARTED with, so the close below can tell an actual
    # user choice from the value that was simply inherited. Without this, every
    # app would get its look pinned the first time it saw the keyboard and the
    # global Size/Skin settings would stop reaching it.
    open_look = (screen.get_osk_size(), skins.get_active_skin())
    _apply_size("small" if start_menu_open else screen.get_osk_size())
    open_anim_rest = _apply_window_position(
        scr.window, _POS_UP_MID if start_menu_open else None)
    # Steam's own keyboard-open chime, if the sound setting is on.
    state.key_sound_open()
    # A live preview open skips the animation entirely: render the final frame
    # first (so the window never flashes its background) then show it instantly.
    if preview:
        scr.render(virtual_kb, controller_state.get_pointers())
        open_anim_start = None
    else:
        open_anim_start = _begin_open_anim(scr, virtual_kb, controller_state, open_anim_rest)
    _show_window_noactivate(scr.window, _open_anim_show_pos(open_anim_start, open_anim_rest))
    _finish_open_anim_map(scr, virtual_kb, controller_state,
                          open_anim_start, open_anim_rest)
    if open_anim_start is not None:
        # Start the animation clock NOW that the window is actually on-screen.
        # The show + SDL_SyncWindow + off-screen->on-screen move above can take
        # longer than the fade phase itself (~117ms of the 350ms animation), so
        # timing from _begin_open_anim would leave the fade already finished
        # before the window is ever visible  no visible fade-in.
        open_anim_start = time.monotonic()
    # Restore focus to the field the user was typing in (a controller-open's
    # firmware mouse-click can steal it). No-op on Linux until the X11 capture +
    # restore is ported  the saved target is always None there.
    _restore_foreground(state.get_focus_restore_target())
    was_visible = True
    # Keep the user's text field focused while the OSK is up. A PHYSICAL mouse
    # click on the OSK can, on some WMs, pull keyboard focus onto our own window
    # despite the input=0 dock hint  the next typed key would then land on the
    # OSK instead of the field. Each iteration we remember the last real active
    # window (the field being typed in) via _NET_ACTIVE_WINDOW; if our own window
    # ever becomes active, we ask the WM to re-activate the field BEFORE
    # dispatching keys. (X11 analog of the Windows GetForegroundWindow path.)
    _x11_disp, osk_self_xwin = (
        _x11_handles(scr.window) if _IS_LINUX else (None, None))
    last_user_fg = state.get_focus_restore_target()
    # Tracks the emoji-picker desktop-mode override (see _EMOJI_PICKER_PROCESSES):
    # _emoji_picker_seen latches once the picker has actually taken the foreground
    # after the emoji key opened it, so we only treat "picker no longer foreground"
    # as a close AFTER it appeared. Inert on Linux (_foreground_process_name None).
    emoji_picker_seen = False
    next_emoji_check = 0.0
    # Last key under each touchpad pointer, for haptic "switched key" ticks.
    last_hover = [None, None]
    # Next time the held left mouse button re-fires its key (inf = not armed).
    mouse_repeat_at = float("inf")
    # Mouse Select drag anchor (None = not selecting) and the cell a
    # deferred variant-capable press landed on (None = nothing deferred).
    mouse_select_anchor = None
    mouse_deferred_cell = None
    # The mouse highlight only follows a REAL move: the first motion event after
    # the OSK opens is usually SDL reporting the cursor's position because the
    # window appeared under it  we record that as this anchor WITHOUT jumping
    # the highlight there. None = re-prime on the next open/show.
    mouse_anchor = None
    mouse_kbd_suppressed = False
    # Active free mouse drag (see _begin_drag), or None when nothing is grabbed.
    drag = None
    # Tracks whether the OSK window is currently click-through (the mouse falls
    # through to the app behind). Driven by the SC "Keyboard Sticks/Mouse
    # controls" toggle: OFF (desktop_mode) → click-through ON so the right-stick
    # mouse + L2/R2 buttons drive the desktop while the touchpads still type.
    # None = re-prime on the next show. X11: empty Shape INPUT region; Windows:
    # WS_EX_TRANSPARENT|LAYERED (see _set_click_through).
    clickthrough_on = None
    # While the mouse is moving over the OSK, a close "X" shows in the top-right
    # corner; clicking it closes the keyboard. This holds the monotonic time the
    # X stays visible until  bumped on every real mouse move, so the X fades out
    # shortly after the cursor goes still or leaves the window. 0 = hidden.
    close_x_until = 0.0
    # Last window-relative mouse position (from motion events); used to keep the
    # close "X" shown  and clickable  while the cursor is parked on it, even
    # after the move-recency timer above lapses.
    last_mpos = None
    # Next time to re-poll _is_start_menu_open() while visible (see
    # _START_MENU_POLL_INTERVAL). 0 = check on the first iteration too, though
    # start_menu_open above already matches reality so that check is a no-op.
    next_start_check = 0.0

    # Adaptive render rate. The keyboard only needs a fast loop while the user
    # is actually doing something (smooth pointer + low-latency hover haptics);
    # an open-but-idle keyboard redrawing 120x/sec is wasted CPU. So we run at
    # ACTIVE_FPS while there's activity and for a short grace period after, then
    # drop to IDLE_FPS. The next input snaps it straight back to ACTIVE_FPS.
    ACTIVE_FPS = 120
    IDLE_FPS = 15
    IDLE_GRACE = 0.4
    current_fps = ACTIVE_FPS
    last_active = time.monotonic()
    # Tracks the Shift latch across frames so a change can be flagged as
    # activity (keeps ACTIVE_FPS through the key slide/fade animation).
    prev_shift = state.is_shift_held()

    # One reusable event struct polled each frame (SDL3 SDL_PollEvent fills it).
    ev = S.SDL_Event()

    while not state.should_close():
        activity = False
        now = time.monotonic()
        # The close "X" shows while the mouse moved recently OR is parked on the
        # button itself (so it never vanishes from under a cursor that's lining
        # up the click). Computed before event handling so the click handler
        # below reads a value consistent with what was last drawn.
        scr.show_close_x = (now < close_x_until
                            or (last_mpos is not None
                                and scr.close_x_hit(*last_mpos)))
        while S.SDL_PollEvent(ctypes.byref(ev)):
            et = ev.type
            if et == S.SDL_EVENT_QUIT:
                state.close()
                break
            if et == S.SDL_EVENT_WINDOW_RESIZED:
                # We drive every OSK resize ourselves (Screen.resize), so keep
                # the module globals tied to the size WE intend. A late/coalesced
                # resize event carrying a PREVIOUS size (common during rapid
                # size switching) would otherwise clobber width/height, leaving
                # the OSK mis-placed (left-anchored) with its right edge clipped.
                screen.width, screen.height = screen._compute_size(current_size[0])
                activity = True
            # Mouse control: hovering highlights the key under the pointer,
            # left-click presses it (the Shift key toggles latched Shift), and
            # the standard side buttons handle the keys you can't otherwise
            # reach mouse-only. Right-click = Shift, X1 (back) = Backspace,
            # X2 (forward) = Space. The OSK window never takes focus, so
            # clicking it doesn't disturb the app being typed into. (SDL3
            # reports mouse x/y as floats; find_key* handles that.)
            elif et == S.SDL_EVENT_MOUSE_MOTION and drag is not None:
                # Grabbed: the keyboard follows the cursor instead of typing.
                if not (int(ev.motion.state) & S.button_mask(drag["button"])):
                    # The release went somewhere else (a capture we didn't get,
                    # or a button let go over another window)  this motion is
                    # the first word of it, so drop the keyboard here.
                    _tap = _end_drag(drag)
                    drag = None
                    mouse_anchor = None
                    state.set_mouse_press_cell(None)
                    mouse_repeat_at = float("inf")
                    if _tap is not None:
                        if not mouse_kbd_suppressed:
                            state.queue_key_press(*_tap)
                    else:
                        _reassert_topmost(scr.window)
                elif _drag_motion(scr.window, drag, (ev.motion.x, ev.motion.y)):
                    # A grab that became a real drag must not ALSO fire the key
                    # it started on, so clear the press it painted.
                    state.set_mouse_press_cell(None)
                    mouse_repeat_at = float("inf")
                activity = True
            elif et == S.SDL_EVENT_MOUSE_MOTION and state.is_visible():
                mpos = (ev.motion.x, ev.motion.y)
                if open_anim_start is not None:
                    pass
                elif mouse_anchor is not None and mpos != mouse_anchor:
                    close_x_until = now + _CLOSE_X_SHOW_SECS
                    last_mpos = mpos
                    if not mouse_kbd_suppressed:
                        rc = virtual_kb.find_key_rc(*mpos)
                        if rc is not None:
                            state.set_cursor(*rc)
                            activity = True
                mouse_anchor = mpos
                # Accent row, mouse-driven: while a row this mouse opened is
                # up, the pointer's x picks the candidate under it. Only a
                # "mouse"-sourced row follows the mouse  a pad- or button-held
                # row must keep its own highlight.
                if (state.is_diacritic_open()
                        and state.get_diacritic_source() == "mouse"):
                    _drect = state.get_diacritic_rect()
                    if _drect is not None:
                        state.set_diacritic_index(
                            diacritics.variant_index_at_point(
                                _drect, mpos[0], mpos[1],
                                state.get_diacritic_variant_count()))
                # Select mode: with the left button held on the Select key,
                # horizontal travel fires Shift+Left/Right, so dragging selects
                # text. The anchor moves with the pointer, so a continued drag
                # keeps selecting.
                if mouse_select_anchor is not None:
                    if int(ev.motion.state) & S.SDL_BUTTON_LMASK:
                        _step = _SELECT_MOUSE_DRAG_STEP
                        while abs(mpos[0] - mouse_select_anchor) >= _step:
                            _dirn = 1 if mpos[0] > mouse_select_anchor else -1
                            vkb.tap_keycode(sui.Keys.KEY_RIGHT if _dirn > 0
                                            else sui.Keys.KEY_LEFT)
                            mouse_select_anchor += _step * _dirn
                    else:
                        mouse_select_anchor = _end_mouse_select()
                if not (int(ev.motion.state) & S.SDL_BUTTON_LMASK):
                    state.set_mouse_press_cell(None)
                    mouse_repeat_at = float("inf")
                    mouse_deferred_cell = _flush_mouse_deferred(
                        virtual_kb, mouse_deferred_cell)
            elif et == S.SDL_EVENT_MOUSE_BUTTON_DOWN and state.is_visible():
                btn = ev.button.button
                mouse_anchor = (ev.button.x, ev.button.y)
                if (btn == S.SDL_BUTTON_LEFT and scr.show_close_x
                        and scr.close_x_hit(ev.button.x, ev.button.y)):
                    state.close()
                    break
                # Free-move grabs: middle-drag from anywhere, or left-drag the
                # Move key / one of the four outer corners. Never
                # mid-open-animation  the animation is driving the window
                # position itself. A left grab paints the key under it pressed
                # and holds that key's action until release (see _end_drag).
                _grab = None
                _rc = virtual_kb.find_key_rc(ev.button.x, ev.button.y)
                if drag is None and open_anim_start is None:
                    if btn == S.SDL_BUTTON_MIDDLE:
                        _grab = _begin_drag(scr.window, ev.button.x, ev.button.y,
                                            btn, None)
                    elif (btn == S.SDL_BUTTON_LEFT
                            and (_is_move_cell(virtual_kb, _rc)
                                 or scr.corner_grab_hit(ev.button.x, ev.button.y))):
                        _grab = _begin_drag(scr.window, ev.button.x, ev.button.y,
                                            btn, _rc)
                if _grab is not None:
                    drag = _grab
                    if _grab["tap"] is not None and not mouse_kbd_suppressed:
                        state.set_cursor(*_rc)
                        state.set_mouse_press_cell(_rc)
                elif not mouse_kbd_suppressed:
                    if btn == S.SDL_BUTTON_LEFT:
                        if _rc is not None:
                            state.set_cursor(*_rc)
                            state.set_mouse_press_cell(_rc)
                            _dkey = virtual_kb.keys[_rc[0]][_rc[1]]
                            if _dkey.is_select:
                                # Press on Select: hold OS Shift for the whole
                                # gesture and anchor the drag here, so travel
                                # from this point selects text.
                                mouse_select_anchor = ev.button.x
                                vkb.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                                state.set_select_active(True)
                            elif vkb.diacritic_variants_for_key(_dkey) is not None:
                                # Tap-first, hold-to-extend  same defer model
                                # the pad and A button use: type nothing yet,
                                # let the hold open the row, decide on release.
                                mouse_deferred_cell = _rc
                                state.key_sound_tick()
                                mouse_repeat_at = now + vkb.KEY_REPEAT_DELAY
                            else:
                                state.queue_key_press(*_rc)
                                mouse_repeat_at = now + vkb.KEY_REPEAT_DELAY
                    elif btn == S.SDL_BUTTON_RIGHT:
                        vkb.toggle_shift()
                    elif btn == S.SDL_BUTTON_X1:
                        vkb.tap_keycode(sui.Keys.KEY_BACKSPACE)
                    elif btn == S.SDL_BUTTON_X2:
                        vkb.tap_keycode(sui.Keys.KEY_SPACE)
                activity = True
            elif (et == S.SDL_EVENT_MOUSE_BUTTON_UP and drag is not None
                    and ev.button.button == drag["button"]):
                # Drop. A grab that never travelled fires the key it started on
                # (Move: close, or cycle with Shift), so clicking Move is
                # unchanged; a real drag instead remembers where it landed.
                _tap = _end_drag(drag)
                drag = None
                state.set_mouse_press_cell(None)
                mouse_repeat_at = float("inf")
                # The window moved out from under the cursor, so the hover
                # highlight has to re-prime like it does after a Move-key cycle.
                mouse_anchor = None
                if _tap is not None:
                    if not mouse_kbd_suppressed:
                        state.queue_key_press(*_tap)
                else:
                    # Repositioning can silently cost the OSK its topmost
                    # z-order, leaving it visible but no longer the window that
                    # receives the mouse (see _reassert_topmost).
                    _reassert_topmost(scr.window)
                activity = True
            elif (et == S.SDL_EVENT_MOUSE_BUTTON_UP
                    and ev.button.button == S.SDL_BUTTON_LEFT):
                state.set_mouse_press_cell(None)
                mouse_repeat_at = float("inf")
                mouse_select_anchor = _end_mouse_select()
                mouse_deferred_cell = _flush_mouse_deferred(
                    virtual_kb, mouse_deferred_cell)
                activity = True

        # Poll any SDL pad (Xbox/DualSense/Switch Pro) HERE  on the thread that
        # just drained the SDL event queue. SDL only refreshes gamepad state on
        # its event-pump thread, so the tray's sdl_gamepad_thread reads stale /
        # all-zero frames once this loop is running (which froze SDL pads to no
        # input, and a frozen deflected stick slowly drifted the cursor). We read
        # the fresh state here and publish it for the input thread's
        # SharedSdlFrameSource (which feeds handle_input). The tray cedes while
        # the OSK is open, so this is the sole SDL poller then.
        _sdl_src = state.get_sdl_source()
        if _sdl_src is not None:
            try:
                # Keep gyro streaming aligned with the shared "Gyro To Mouse"
                # state while WE own the pads (the tray's loop, which normally
                # maintains this, cedes while the OSK is open)  the poll's
                # _pump applies it on this (SDL) thread.
                _sdl_src.set_gyro_kinds(state.get_gyro_stream_kinds())
                _f = _sdl_src.poll()
                # Stamp the active pad's gyro (SDL sensor API, rad/s) into the
                # frame's raw-unit gyro fields so the OSK's gyro trackpad-
                # circle pointer reads SDL pads exactly like the HID kinds.
                if _f is not None:
                    _g = _sdl_src.read_gyro()
                    if _g:
                        _jid, _gk, _gx, _gy, _gz = _g[0]
                        # rad/s → raw int16 units (±32768 = ±2000 °/s); SDL
                        # axes already match the HID swizzle (X=pitch, Y=yaw).
                        _s = 57.29577951308232 / _GYRO_DEG_PER_SEC
                        _f = _f._replace(gpitch=int(_gx * _s),
                                         gyaw=int(_gy * _s),
                                         groll=int(_gz * _s))
                # Tag the frame with the active pad's controller kind so the
                # OSK glyph swap shows that family's art (Switch ZL/ZR vs
                # Xbox LT/RT vs PS L2/R2 ...).
                state.set_sdl_frame(_f, kind=_sdl_src.active_kind())
            except Exception:
                pass

        cur_visible = state.is_visible()
        if cur_visible != was_visible:
            if cur_visible:
                # Re-prime the open animation (position + invisible first frame +
                # raise) BEFORE showing, so a re-open fades/rises in like the first.
                # Same Start-menu up-mid + small-size override as the initial open.
                start_menu_open = _is_start_menu_open()
                _apply_size("small" if start_menu_open else screen.get_osk_size())
                open_anim_rest = _apply_window_position(
                    scr.window, _POS_UP_MID if start_menu_open else None)
                open_anim_start = _begin_open_anim(
                    scr, virtual_kb, controller_state, open_anim_rest)
                _show_window_noactivate(scr.window, _open_anim_show_pos(open_anim_start, open_anim_rest))
                _finish_open_anim_map(scr, virtual_kb, controller_state,
                                      open_anim_start, open_anim_rest)
                if open_anim_start is not None:
                    # Start the clock now the window is on-screen  see the
                    # matching comment at the initial open above.
                    open_anim_start = time.monotonic()
                # Re-prime so the open's spurious motion doesn't jump the cursor.
                mouse_anchor = None
                mouse_kbd_suppressed = False
                # Re-prime click-through so it re-applies for this fresh show
                # (the X input shape / ex-style is reset when hidden).
                clickthrough_on = None
            else:
                # A grab in flight when the keyboard is closed (by a controller,
                # Escape, ...) is abandoned where it stands: release the mouse
                # capture and keep the spot it had reached.
                if drag is not None:
                    _end_drag(drag)
                    drag = None
                # Don't leave a mouse-latched Shift stuck down on the OS.
                vkb.release_shift()
                vkb.release_ctrl()
                vkb.release_alt()
                state.close_diacritic()
                # Restore normal mouse input before hiding so a later non-desktop
                # open isn't stuck click-through.
                if clickthrough_on:
                    _set_click_through(scr.window, False)
                    clickthrough_on = False
                S.SDL_HideWindow(scr.window)
                open_anim_start = None  # abort any in-flight open animation
            was_visible = cur_visible
            activity = True

        if cur_visible:
            mouse_kbd_suppressed = not state.is_kbd_mouse_nav_enabled()
            # OSK click-through: when the active controller's stick/mouse nav is
            # OFF (desktop_mode), make the mouse fall through to the app behind
            # so the right-stick mouse + L2/R2 buttons (controller.py desktop_mode)
            # drive the desktop while the touchpads still type. Matches the
            # is_kbd_stick_nav_enabled_for(active) test controller.py uses.
            _active = state.get_active_controller()
            _desktop_mode = not state.is_kbd_stick_nav_enabled_for(_active)
            if _desktop_mode != clickthrough_on:
                _set_click_through(scr.window, _desktop_mode)
                clickthrough_on = _desktop_mode
            state.set_close_x_rect(scr._close_x_geom())
            _mouse_on_x = (last_mpos is not None
                           and scr.close_x_hit(*last_mpos))
            state.set_close_x_active(_mouse_on_x)
            if not scr.show_close_x:
                _ap = controller_state.get_pointers()
                for _p in _ap:
                    if _p.state != state.InputState.INACTIVE:
                        _px, _py = _p.coord_frac.to_absolute()
                        if scr.close_x_hit(_px, _py):
                            scr.show_close_x = True
                            break
            # Apply a tray-side skin change live (no-op unless it changed).
            scr.maybe_reload_skin()
            # Apply a tray-side layout change live (Keyboard Layout dropdown
            # hover preview, or a real pick): rebuild every page from the
            # newly-selected YAML and jump back to its first page, same as a
            # fresh open. A no-op unless the layout actually changed.
            _layout = state.get_osk_layout()
            if _layout != last_layout:
                last_layout = _layout
                kb_pages = load_kb_config().construct_pages()
                _first_page = next(iter(kb_pages))
                state.set_osk_page(_first_page)
                virtual_kb = kb_pages[_first_page]
                state.set_grid_dims([len(r) for r in virtual_kb.keys])
                state.set_cursor(0, 0)
            virtual_kb.update_dimensions()
            # Follow a page switch (phone layout ?123 / =\< / ABC). The pages
            # are separate VirtualKeyboard objects, so simply rebinding here is
            # enough for rendering, hit-testing, the swipe decoder's geometry
            # and the render dirty-flag to all pick the new one up.
            _page = state.get_osk_page()
            if _page in kb_pages and kb_pages[_page] is not virtual_kb:
                virtual_kb = kb_pages[_page]
                virtual_kb.update_dimensions()
                state.set_grid_dims([len(r) for r in virtual_kb.keys])
                # The pages differ in shape, so a cursor parked on (say) the
                # last column of a longer row would land out of bounds.
                state.set_cursor(0, 0)
                # Anything queued against the OLD page's coordinates would now
                # hit whatever key happens to sit there instead.
                controller_state.click_queue.clear()
                state.drain_key_press_queue()
            # DPAD: step the cursor using the actual layout pixel positions.
            dpad_steps = state.drain_dpad_queue()
            for direction, haptic in dpad_steps:
                if (state.get_diacritic_source() == "button"
                        and direction in ("LEFT", "RIGHT")):
                    # An accent row opened by holding the A button: left/right
                    # walk the candidates instead of the cursor, so the whole
                    # pick is one gesture. Up/down still leave the row.
                    state.set_diacritic_index(diacritics.step_variant_index(
                        state.get_diacritic_index(), 1 if direction == "RIGHT"
                        else -1, state.get_diacritic_variant_count()))
                    if haptic:
                        state.haptic_tick()
                    continue
                if state.get_diacritic_source() == "button":
                    state.close_diacritic()
                vkb.step_cursor(virtual_kb, direction, haptic=haptic)
            # Re-assert the user's field as the active window before any key
            # dispatch this iteration. No-op in the common case (the field is
            # already active → we just record it); only acts when a mouse click
            # has pulled focus onto the OSK, so the keystroke still lands in the
            # field the user was typing in.
            if osk_self_xwin:
                _act = _x11_active_window(_x11_disp)
                if _act and _act != osk_self_xwin:
                    last_user_fg = _act
                elif _act == osk_self_xwin and last_user_fg:
                    _x11_activate_window(_x11_disp, last_user_fg)
            # Emoji desktop-mode override: while the picker is open, watch the
            # foreground process. Once the picker has actually shown (taken the
            # foreground) we know it's up; when focus then LEAVES it the user has
            # dismissed it by ANY means (its close button, a pick, click-away) 
            # so clear the flag and restore LStick/Mouse nav. The OSK emoji key
            # path clears the flag directly, so this only matters for other
            # close gestures. No-op off Windows (_foreground_process_name None).
            if state.is_emoji_open():
                if now >= next_emoji_check:
                    next_emoji_check = now + _EMOJI_CHECK_INTERVAL
                    _proc = _foreground_process_name()
                    if _proc in _EMOJI_PICKER_PROCESSES:
                        emoji_picker_seen = True
                    elif emoji_picker_seen:
                        state.set_emoji_open(False)
                        emoji_picker_seen = False
            elif emoji_picker_seen:
                emoji_picker_seen = False
            if controller_state.click_queue:
                activity = True
                # A controller PAD click closes the OSK only when the PAD pointer
                # itself lands on the close X  NOT when the (separate) system
                # mouse happens to hover it, which would hijack a pad click aimed
                # at a key (e.g. pressing the emoji key to dismiss the picker).
                # The mouse-hover + A/R2-button close path lives in controller.py
                # (via state.is_close_x_active()), so this stays pad-only.
                _close_via_cq = False
                if scr.show_close_x:
                    for _cq_item in list(controller_state.click_queue):
                        _cq_rep = (isinstance(_cq_item, tuple)
                                   and _cq_item and _cq_item[0] == "repeat")
                        _cq_coord = _cq_item[1] if _cq_rep else _cq_item
                        _cx, _cy = _cq_coord.to_absolute()
                        if scr.close_x_hit(_cx, _cy):
                            _close_via_cq = True
                            break
                if _close_via_cq:
                    controller_state.click_queue.clear()
                    state.close()
            vkb.process_click_queue(virtual_kb, controller_state.click_queue)
            # Hold-commit: a grab that is still parked on its start point when
            # the hold-to-repeat delay elapses isn't a drag  it's the key under
            # it being held down (the corner zones sit on real keys, and
            # Backspace occupies the top-right one). Let go of the window and
            # hand the press to the normal key path, which fires it now and
            # repeats it from here on.
            if (drag is not None and not drag["moved"]
                    and now >= drag["hold_at"]):
                _hold_cell = _end_drag(drag)
                drag = None
                mouse_anchor = None
                if _hold_cell is not None and not mouse_kbd_suppressed:
                    state.set_cursor(*_hold_cell)
                    state.set_mouse_press_cell(_hold_cell)
                    state.queue_key_press(*_hold_cell)
                    mouse_repeat_at = now + vkb.KEY_REPEAT_DELAY
                activity = True
            # Mouse left-button hold-to-repeat: while held over a repeatable
            # key (Backspace / arrows), re-queue it on the shared cadence.
            # Queued before the drain so it dispatches this same frame. Keeps
            # the loop "active" so the repeat stays smooth at full FPS.
            press_cell = state.get_mouse_press_cell()
            if press_cell is not None:
                activity = True
                if now >= mouse_repeat_at:
                    pr, pc = press_cell
                    if (0 <= pr < len(virtual_kb.keys) and 0 <= pc < len(virtual_kb.keys[pr])
                            and (vkb.is_repeatable(virtual_kb.keys[pr][pc])
                                 or virtual_kb.keys[pr][pc].hold_callback is not None)):
                        state.queue_key_press(pr, pc, repeat=True)
                        mouse_repeat_at = now + vkb.KEY_REPEAT_INTERVAL
                    else:
                        # Letters can't repeat  but a held one whose press was
                        # deferred turns into its accent row instead, which the
                        # pointer then steers and the button release commits.
                        if mouse_deferred_cell is not None:
                            vkb.open_diacritic_rc(virtual_kb, pr, pc, "mouse")
                        mouse_repeat_at = float("inf")
            # Key presses: fire the callback of the queued key. A repeat hit
            # (something held) only fires over a repeatable key (Backspace /
            # arrows), so holding rubs out / steps without machine-gunning
            # ordinary keys.
            key_presses = state.drain_key_press_queue()
            for row, col, is_repeat, is_silent in key_presses:
                if 0 <= row < len(virtual_kb.keys) and 0 <= col < len(virtual_kb.keys[row]):
                    key = virtual_kb.keys[row][col]
                    if is_repeat:
                        # Held: a `hold` key fires that behaviour once (the
                        # phone Shift's long-press to the symbol page); an
                        # ordinary key only repeats if it's repeatable.
                        if key.hold_callback is not None:
                            vkb.fire_hold(virtual_kb, key)
                            continue
                        if not vkb.is_repeatable(key):
                            # Letters can't repeat, so a held letter turns into
                            # its accent row instead: left/right then walk the
                            # candidates and the next press commits one.
                            vkb.open_diacritic_rc(virtual_kb, row, col, "button")
                            continue
                    # A deferred base letter already clicked at its press
                    # edge; dispatching it silently stops a second tick.
                    vkb._dispatch_silent = is_silent
                    try:
                        vkb.dispatch_key(virtual_kb, key)
                    finally:
                        vkb._dispatch_silent = False
            if state.take_position_cycle_request():
                # A cycle re-places the window outright, so any grab still on it
                # is dropped first (its origin is about to be wrong).
                if drag is not None:
                    _end_drag(drag)
                    drag = None
                _cycle_window_position(scr.window, app_key)
                activity = True
                # Repositioning can silently drop the OSK's topmost z-order,
                # leaving it visible but no longer the window that receives the
                # mouse (see _reassert_topmost)  re-assert it. Moving the window
                # also slides it under a stationary mouse, firing a spurious
                # motion (new window-relative coords), so re-prime mouse_anchor
                # too, or the highlight jumps to the mouse after a Move.
                _reassert_topmost(scr.window)
                mouse_anchor = None
            req = state.take_window_position_request()
            if req is not None:
                # An explicit jump (the lock screen dodging the password box)
                # outranks a free spot the mouse dragged the keyboard to, and
                # outranks a grab still holding it.
                if drag is not None:
                    _end_drag(drag)
                    drag = None
                _free_position[0] = None
                _position_index[0] = req % 6
                _apply_window_position(scr.window)
                activity = True
                _reassert_topmost(scr.window)  # reposition can drop topmost
                mouse_anchor = None
            # Poll for changes the open-time check can't see because they happen
            # while the OSK is ALREADY showing: the Start menu opening/closing
            # (reposition to/from up-mid + small  a no-op on Linux, where
            # _is_start_menu_open() is always False), AND a live "Keyboard Skin
            # -> Size" change from the tray menu (the tray updates the size
            # global immediately; we pick it up here and resize on the spot).
            # Held off while the mouse has hold of the keyboard: both branches
            # below re-place the window, which would fight the drag.
            if now >= next_start_check and drag is None:
                next_start_check = now + _START_MENU_POLL_INTERVAL
                now_start_open = _is_start_menu_open()
                want_size = "small" if now_start_open else screen.get_osk_size()
                pos_changed = now_start_open != start_menu_open
                # Also catch a change that leaves the size NAME alone but
                # moves the pixels: toggling Split Keyboard or Scale With
                # Resolution while the keyboard is on screen.
                size_changed = (want_size != current_size[0]
                                or (screen.width, screen.height)
                                != screen._compute_size(want_size))
                start_menu_open = now_start_open
                if size_changed:
                    # Resize the existing window in place (NO open animation for
                    # a live resize), then reposition and render one correct
                    # frame. The window stays visible throughout  no second
                    # window, no hide/show.
                    _apply_size(want_size)
                    open_anim_start = None
                    open_anim_rest = _apply_window_position(
                        scr.window, _POS_UP_MID if start_menu_open else None)
                    # SetWindowSize + SetWindowPosition can each cost the OSK its
                    # topmost z-order, leaving it visible but no longer receiving
                    # mouse input (the same hazard the open animation's settle
                    # guards against)  re-assert before rendering the new frame.
                    _reassert_topmost(scr.window)
                    scr.render(virtual_kb, controller_state.get_pointers())
                    activity = True
                    mouse_anchor = None
                elif pos_changed:
                    _apply_window_position(
                        scr.window, _POS_UP_MID if start_menu_open else None)
                    _reassert_topmost(scr.window)  # reposition can drop topmost
                    activity = True
                    mouse_anchor = None
            if dpad_steps or key_presses:
                activity = True
            pointers = controller_state.get_pointers()
            # Haptic tick when a touchpad pointer moves onto a different key
            # (touchpad mode only  the pointer is INACTIVE when not touching).
            for i in (0, 1):
                ptr = pointers[i]
                if ptr.state != state.InputState.INACTIVE:
                    activity = True  # finger on the pad → keep the loop fast
                    px, py = ptr.coord_frac.to_absolute()
                    hovered = virtual_kb.find_key(px, py)
                    if hovered is not None and hovered is not last_hover[i]:
                        state.haptic_tick()
                        last_hover[i] = hovered
                else:
                    last_hover[i] = None
            # A Shift state change (L2/ZL trigger, DPAD+A, right-click) drives
            # the dual-key slide/fade animation. Flag it as activity so the
            # grace period holds ACTIVE_FPS for the ~130 ms transition instead
            # of rendering it at the idle frame rate.
            cur_shift = state.is_shift_held()
            if cur_shift != prev_shift:
                activity = True
                prev_shift = cur_shift
            if open_anim_start is not None:
                # --- OSK OPEN animation frame (render at full FPS until done) ---
                activity = True
                p = (now - open_anim_start) / _OPEN_ANIM_SECS
                if p >= 1.0:
                    # Done: settle exactly at rest, then resume normal render.
                    if open_anim_rest is not None:
                        S.SDL_SetWindowPosition(
                            scr.window, open_anim_rest[0], open_anim_rest[1])
                    # The settle phase's burst of SDL_SetWindowPosition calls
                    # can cost the OSK its topmost z-order (see
                    # _reassert_topmost)  left unfixed, the window stays
                    # visible but stops receiving mouse input. Re-prime the
                    # anchor too, so any spurious motion the repositioning
                    # caused doesn't leave the highlight stuck mid-jump.
                    _reassert_topmost(scr.window)
                    mouse_anchor = None
                    open_anim_start = None
                    scr.render(virtual_kb, pointers)
                else:
                    # Opacity eases 0→1 over the first FADE_FRAC; the bottom cut
                    # reveals over the first REVEAL_FRAC; the window settles
                    # DROP_PX downward from MOVE_START_FRAC to the end.
                    fade = _ease_out_cubic(min(1.0, p / _OPEN_ANIM_FADE_FRAC))
                    reveal_t = _ease_out_cubic(min(1.0, p / _OPEN_ANIM_REVEAL_FRAC))
                    cut = _OPEN_ANIM_CUT_PX * (1.0 - reveal_t)
                    if p > _OPEN_ANIM_MOVE_START_FRAC and open_anim_rest is not None:
                        move_t = _ease_out_cubic(
                            (p - _OPEN_ANIM_MOVE_START_FRAC)
                            / (1.0 - _OPEN_ANIM_MOVE_START_FRAC))
                        rx, ry = open_anim_rest
                        S.SDL_SetWindowPosition(
                            scr.window, rx,
                            int(round(ry - _OPEN_ANIM_DROP_PX * (1.0 - move_t))))
                    if not scr.render_open_anim(virtual_kb, pointers, fade, cut):
                        open_anim_start = None  # target gone → stop animating
            else:
                scr.render(virtual_kb, pointers)
        else:
            # Drain any clicks that fired while hidden so they don't pile up.
            controller_state.click_queue.clear()
            state.drain_key_press_queue()
            state.drain_dpad_queue()

        # Choose the frame cap from recent activity. The grace period keeps us at
        # ACTIVE_FPS through brief pauses (e.g. between keystrokes) so the rate
        # doesn't flap; a genuinely idle keyboard settles to IDLE_FPS.
        nowt = time.monotonic()
        if activity:
            last_active = nowt
        desired_fps = ACTIVE_FPS if (nowt - last_active) < IDLE_GRACE else IDLE_FPS
        if desired_fps != current_fps:
            scr.set_framerate(desired_fps)
            current_fps = desired_fps
        scr.delay()

    # --- OSK CLOSE animation ----------------------------------------------
    # Played while the window is still visible, before the teardown below
    # hides it. One smoothstep curve drives both the fade and the scale, at
    # the same ~120fps cadence the open uses. Falls back to the instant hide
    # if the renderer can't give us an offscreen target.
    try:
        _close_ptrs = controller_state.get_pointers()
        if scr.render_close_anim(virtual_kb, _close_ptrs, 1.0, 1.0):
            _close_start = time.monotonic()
            _close_next = _close_start
            while True:
                _cnow = time.monotonic()
                _cp = (_cnow - _close_start) / _CLOSE_ANIM_SECS
                if _cp >= 1.0:
                    break
                _ce = _cp * _cp * (3.0 - 2.0 * _cp)      # smoothstep
                scr.render_close_anim(virtual_kb, _close_ptrs, 1.0 - _ce,
                                      1.0 - _CLOSE_ANIM_SCALE * _ce)
                _close_next += _CLOSE_ANIM_FRAME
                _csleep = _close_next - time.monotonic()
                if _csleep > 0:
                    time.sleep(_csleep)
            # Commit one fully transparent frame, so the hide below acts on an
            # already alpha-0 window instead of flashing its last opaque one.
            scr.render_close_anim(virtual_kb, _close_ptrs, 0.0,
                                  1.0 - _CLOSE_ANIM_SCALE)
    except Exception as e:
        print(f"adusk: close animation skipped ({e!r})")

    # "Remember Per App": record the size/skin the user actually switched to
    # during this session (the position is recorded as the Move key cycles it),
    # so the app reopens with the look it was left at.
    if app_key is not None:
        if screen.get_osk_size() != open_look[0]:
            state.note_per_app(app_key, "size", screen.get_osk_size())
        if skins.get_active_skin() != open_look[1]:
            state.note_per_app(app_key, "skin", skins.get_active_skin())
    # Release every latched modifier before tearing down, so closing the
    # keyboard never leaves the OS holding one down.
    vkb.release_shift()
    vkb.release_ctrl()
    vkb.release_alt()
    # An accent row left open would swallow the next session's first hold.
    state.close_diacritic()
    state.key_sound_close()
    # If the OS emoji picker is still open, close it too  it shouldn't linger
    # on screen after the keyboard it was opened from is gone.
    vkb.close_emoji_picker()
    # Restore the window to the base size before teardown so the module-level
    # width/height the key layout reads stay in sync with the window.
    if current_size[0] != base_size:
        scr.resize(base_size)
        current_size[0] = base_size
    # Give the controller thread up to 1 second to run its cleanup (sends
    # the enable-lizard packet before closing the HID handle). Without this
    # wait the daemon thread is killed before it can re-enable lizard mode.
    sc_thread.join(timeout=1.0)
    # Free this session's window/renderer, then drop our SDL subsystem refs
    # (GAMEPAD/EVENTS stay up for a tray-owned persistent watcher).
    try:
        S.SDL_DestroyRenderer(scr.renderer)
        S.SDL_DestroyWindow(scr.window)
    except Exception:
        pass
    S.TTF_Quit()
    # VIDEO is intentionally NOT quit  it's inited once and kept for the life
    # of the process so the next open skips the ~400 ms subsystem re-init (see
    # main()'s init). This is just an idle X connection while closed (no window,
    # no loop → no CPU). EVENTS/GAMEPAD stay refcounted with the tray's watcher.
    S.SDL_QuitSubSystem(S.SDL_INIT_EVENTS | S.SDL_INIT_GAMEPAD)
    # Leave high-responsiveness mode now the OSK is closed (no-op on Linux).
    power.unboost_current_thread()
    power.release()


if __name__ == '__main__':
    main()
