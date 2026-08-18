# -*- coding: utf-8 -*-
"""On-screen Virtual Menu overlay (Steam-style touch menus).

A borderless, topmost, click-through layered window that shows a touch
menu's grid while the assigned trackpad is being touched. Rendering comes
from keybinds_runtime.render_vmenu_image (the SAME renderer behind the
Options live preview), pushed to the screen with UpdateLayeredWindow so the
window is per-pixel alpha-blended and never steals focus or input.

Threading: every method must be called from ONE thread (the SC input
thread)  the Win32 window is created lazily on first show() and lives on
that thread. Publishing new menus from the picker never touches this
object; the watcher notices the adusk_state version bump and calls hide()
itself.
"""

import ctypes
import sys

import keybinds_runtime

# Import-safe off Windows (the Linux tree mirrors this file for source
# parity; its real runtime is tray_linux.py, which has no SC takeover and
# never shows this overlay). All Win32 handles stay None there and
# _ensure_window simply refuses.
if sys.platform == "win32":
    import ctypes.wintypes as wt
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
else:                                # pragma: no cover - Linux mirror only
    wt = None
    _user32 = _gdi32 = None

_WS_POPUP = 0x80000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_SW_SHOWNOACTIVATE = 4
_SW_HIDE = 0
# SW_SHOWNOACTIVATE displays the window but does NOT re-order it within the
# topmost band, so the menu can surface UNDERNEATH another topmost window that
# was raised more recently  the tutorial's scrim/panel being the case that
# found this (the menu came up behind the dimming and looked like it had never
# opened). _raise_top re-asserts the position explicitly.
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_ULW_ALPHA = 2
_AC_SRC_OVER = 0
_AC_SRC_ALPHA = 1
_PM_REMOVE = 1

_CLASS_NAME = "SteamlessVMenuOverlay"

# Sizing for the on-screen menu is derived from keybinds_runtime
# (VMENU_BOX_BASE + vmenu_natural_size)  see show(); the grid is fit into a
# fixed box with SQUARE cells, so no per-cell constants live here anymore.


if _user32 is not None:
    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte),
                    ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte),
                    ("AlphaFormat", ctypes.c_ubyte)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                    ("bmiColors", wt.DWORD * 3)]

    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, ctypes.c_uint,
                                  wt.WPARAM, wt.LPARAM)

    def _def_wnd_proc(hwnd, msg, wp, lp):
        return _user32.DefWindowProcW(hwnd, msg, wp, lp)
else:                                # pragma: no cover - Linux mirror only
    _WNDPROC = None

    def _def_wnd_proc(hwnd, msg, wp, lp):
        return 0


class TouchMenuOverlay:
    """One reusable overlay window. show() renders + displays a menu grid
    with an optional highlighted cell; hide() conceals it (the window is
    kept for reuse)."""

    def __init__(self):
        self._hwnd = None
        # keep the callback alive for the window's lifetime
        self._proc = _WNDPROC(_def_wnd_proc) if _WNDPROC else None
        self._visible = False
        self._last = None       # full render key (incl. thumb)  exact no-op
        # Cached menu BODY (no thumb) + its inputs. The thumb moves far more
        # often than the grid/highlight changes, so a pure thumb move reuses
        # this body (a cheap copy + cursor composite) instead of re-rendering
        # every cell and re-scaling every glyph.
        self._body = None
        self._body_key = None
        self._body_pos = (0, 0)     # cached top-left (x, y) for the body
        # Screen rect (x, y, w, h) the menu was last drawn at, or None while
        # hidden. Published for geometry() so the keyboard/mouse trigger path
        # can hit-test the CURSOR against the real on-screen boxes  the
        # window itself is click-through (WS_EX_TRANSPARENT), so the tray's
        # low-level mouse hook does that mapping instead of a WM_LBUTTONDOWN.
        self._geom = None

    # -- window plumbing ----------------------------------------------------
    def _ensure_window(self):
        if self._hwnd:
            return True
        if _user32 is None:
            return False
        try:
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", ctypes.c_uint),
                            ("lpfnWndProc", _WNDPROC),
                            ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int),
                            ("hInstance", wt.HINSTANCE),
                            ("hIcon", wt.HICON),
                            ("hCursor", ctypes.c_void_p),
                            ("hbrBackground", wt.HBRUSH),
                            ("lpszMenuName", wt.LPCWSTR),
                            ("lpszClassName", wt.LPCWSTR)]
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = _CLASS_NAME
            _user32.RegisterClassW(ctypes.byref(wc))   # dup registers no-op
            self._hwnd = _user32.CreateWindowExW(
                _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOPMOST
                | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
                _CLASS_NAME, "Virtual Menu",
                ctypes.c_uint(_WS_POPUP).value,
                0, 0, 10, 10, None, None, hinst, None)
            return bool(self._hwnd)
        except Exception as e:
            print(f"vmenu overlay window failed: {e!r}")
            self._hwnd = None
            return False

    def _raise_top(self):
        """Put the overlay back at the TOP of the topmost band.

        WS_EX_TOPMOST only guarantees it beats ordinary windows; among topmost
        windows it is still last-raised-wins, and SW_SHOWNOACTIVATE does not
        re-order. Anything else topmost that got raised more recently  the
        tutorial's scrim and panel, the on-screen keyboard  would otherwise
        sit over a menu the user is actively steering. Re-asserted on every
        show(), not just the first, because those windows re-raise themselves
        periodically; it's one SetWindowPos next to an UpdateLayeredWindow
        that already runs per frame, so the cost is noise."""
        if not self._hwnd:
            return
        try:
            _user32.SetWindowPos(self._hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                                 _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        except Exception:
            pass

    def _pump(self):
        """Drain any queued messages so the window never reads as hung."""
        try:
            msg = wt.MSG()
            while _user32.PeekMessageW(ctypes.byref(msg), self._hwnd, 0, 0,
                                       _PM_REMOVE):
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass

    def _push_image(self, img, x, y, opacity=255):
        """UpdateLayeredWindow with a premultiplied-BGRA PIL image. `opacity`
        (0..255) is the window-wide SourceConstantAlpha, multiplied on top of
        the image's own per-pixel alpha  the menu's Opacity slider."""
        w, h = img.size
        raw = img.tobytes("raw", "BGRa")     # premultiplied alpha
        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        dib = _gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), 0,
                                      ctypes.byref(bits), None, 0)
        try:
            if not dib or not bits:
                return
            ctypes.memmove(bits, raw, len(raw))
            old = _gdi32.SelectObject(mem_dc, dib)
            size = wt.SIZE(w, h)
            src = wt.POINT(0, 0)
            dst = wt.POINT(x, y)
            bf = _BLENDFUNCTION(_AC_SRC_OVER, 0,
                                max(0, min(255, int(opacity))), _AC_SRC_ALPHA)
            _user32.UpdateLayeredWindow(self._hwnd, screen_dc,
                                        ctypes.byref(dst), ctypes.byref(size),
                                        mem_dc, ctypes.byref(src), 0,
                                        ctypes.byref(bf), _ULW_ALPHA)
            _gdi32.SelectObject(mem_dc, old)
        finally:
            if dib:
                _gdi32.DeleteObject(dib)
            _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)

    # -- public API ---------------------------------------------------------
    def show(self, entries, highlight=None, labels=None, style="touch",
             hpos=None, vpos=None, size=100, opacity=100, thumb=None):
        """Render + display a menu  `style` picks the shape: "touch" (the
        balanced grid), "radial" (donut sectors) or "hotbar" (one linear
        strip). `size` (50..150 %) scales the whole overlay; `hpos`/`vpos`
        (0..100, or None for the legacy centre / 40%-down placement) set its
        on-screen position; `opacity` (10..100 %) dims it; `thumb` = (nx, ny)
        0..1 pad position for the OSK thumb cursor (or None). Re-renders only
        when any of these changed since the last call."""
        if not entries or not self._ensure_window():
            return
        try:
            size = max(50, min(150, int(size)))
        except (TypeError, ValueError):
            size = 100
        try:
            opacity = max(10, min(100, int(opacity)))
        except (TypeError, ValueError):
            opacity = 100
        sw = _user32.GetSystemMetrics(0)
        sh = _user32.GetSystemMetrics(1)
        # Quantize the thumb to ~2px so a still finger's jitter doesn't force a
        # re-render every frame, while the cursor still tracks smoothly.
        tq = None if thumb is None else (round(thumb[0] * 110) / 110.0,
                                         round(thumb[1] * 110) / 110.0)
        key = (id(entries), len(entries), highlight, style, size, opacity,
               hpos, vpos, sw, sh, tq)
        if self._visible and key == self._last:
            self._pump()
            return
        self._last = key
        # Everything the BODY (thumb-less grid) depends on  a change in only
        # `tq` (the thumb) leaves this equal, letting us reuse the cached body.
        body_key = (id(entries), len(entries), highlight, style, size,
                    opacity, hpos, vpos, sw, sh)
        if self._body is not None and body_key == self._body_key:
            x, y = self._body_pos
            if tq is None:
                img = self._body
            else:
                img = self._body.copy()
                keybinds_runtime.draw_vmenu_thumb_on(img, style, tq[0], tq[1])
            self._geom = (x, y) + self._body.size
            self._push_image(img, x, y, int(255 * opacity / 100))
            if not self._visible:
                _user32.ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE)
                self._visible = True
            self._raise_top()
            self._pump()
            return
        n = min(len(entries), keybinds_runtime.VMENU_MAX_ENTRIES)
        # The menu's DEFAULT (Size=100%) footprint is a FIXED box  the grid is
        # FIT INTO it (its natural aspect normalized so the larger side = the
        # box), so adding entries subdivides the box into smaller cells instead
        # of growing the whole menu. res_scale keeps that box the same fraction
        # of the screen on any resolution (reference height 900px = the
        # 1600x900 the design was tuned on); the user's Size% rides on top.
        res_scale = max(0.5, sh / 900.0)
        s = (size / 100.0) * res_scale
        nw, nh = keybinds_runtime.vmenu_natural_size(style, n)
        fit = keybinds_runtime.VMENU_BOX_BASE / max(nw, nh)
        w = max(24, int(nw * fit * s))
        h = max(24, int(nh * fit * s))
        # Render the BODY only (thumb=None) so it can be cached and reused for
        # subsequent thumb-only moves; the cursor is composited separately.
        if style == "radial":
            body = keybinds_runtime.render_vmenu_radial(
                entries, w, highlight=highlight, labels=labels, thumb=None)
        elif style == "hotbar":
            body = keybinds_runtime.render_vmenu_image(
                entries, w, h, highlight=highlight, labels=labels, rows=[n],
                thumb=None)
        else:
            body = keybinds_runtime.render_vmenu_image(
                entries, w, h, highlight=highlight, labels=labels, thumb=None)
        # hpos/vpos place the CENTER of the menu, as a 0..100 fraction of the
        # free space AT THE BASE (Size=100%) FOOTPRINT  not the current
        # (possibly resized) one. Using the current w/h here would anchor an
        # EDGE of the menu (the math degenerates to a pure edge-margin
        # mapping unless hpos/vpos==50), so growing/shrinking the Size%
        # slider visibly grew the menu from that edge instead of from its
        # middle. Anchoring the center on the base footprint keeps that
        # center point fixed as `s` changes, so Size% scales from the middle.
        bw = max(24, int(nw * fit * res_scale))
        bh = max(24, int(nh * fit * res_scale))
        if hpos is None:
            cx = sw // 2
        else:
            cx = int((sw - bw) * max(0, min(100, int(hpos))) / 100.0) + bw // 2
        if vpos is None:
            cy = int(sh * 0.40)
        else:
            cy = int((sh - bh) * max(0, min(100, int(vpos))) / 100.0) + bh // 2
        x = cx - w // 2
        y = cy - h // 2
        x = max(0, min(max(0, sw - w), x))
        y = max(0, min(max(0, sh - h), y))
        # Cache the freshly-rendered body for reuse by later thumb-only moves.
        self._body = body
        self._body_key = body_key
        self._body_pos = (x, y)
        self._geom = (x, y, w, h)
        if tq is None:
            img = body
        else:
            img = body.copy()
            keybinds_runtime.draw_vmenu_thumb_on(img, style, tq[0], tq[1])
        self._push_image(img, x, y, int(255 * opacity / 100))
        if not self._visible:
            _user32.ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE)
            self._visible = True
        self._raise_top()
        self._pump()

    def geometry(self):
        """(x, y, w, h) of the menu as it is CURRENTLY on screen, or None when
        hidden / never shown. Used by the keyboard/mouse trigger path to map a
        screen cursor position onto a box (the window is click-through, so it
        never sees the mouse itself)."""
        return self._geom if self._visible else None

    def hide(self):
        if self._hwnd and self._visible:
            try:
                _user32.ShowWindow(self._hwnd, _SW_HIDE)
            except Exception:
                pass
        self._visible = False
        self._last = None
        self._body = None
        self._body_key = None
        self._geom = None

    def destroy(self):
        if self._hwnd:
            try:
                _user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
        self._hwnd = None
        self._visible = False
        self._last = None
        self._body = None
        self._body_key = None
        self._geom = None
