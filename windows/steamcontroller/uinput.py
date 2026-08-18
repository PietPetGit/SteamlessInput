"""Windows replacement for the Linux uinput key sender.
Maps the KEY_* names used by adusk to pynput keys and sends them via the
OS-level injection layer so the keystrokes land in whichever window is
focused (which on Windows is whatever the user had focused before adusk
started, since the SDL2 window doesn't steal focus unless clicked)."""

import ctypes
import time

from pynput.keyboard import Controller as _Controller, Key as _Key, KeyCode as _KeyCode
from pynput.mouse import Controller as _MouseController, Button as _MouseButton

_MOUSE_BUTTONS = {
    "left": _MouseButton.left,
    "right": _MouseButton.right,
    "middle": _MouseButton.middle,
}


class _KeysProxy:
    """`Keys[name]` and `Keys.NAME` both return the name string so the rest of
    the code can pass keycodes around as strings and we resolve them inside
    Keyboard.pressEvent / releaseEvent."""

    def __getitem__(self, name):
        return name

    def __getattr__(self, name):
        return name


Keys = _KeysProxy()


def _build_keymap():
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m['KEY_' + c.upper()] = _KeyCode.from_char(c)
    for d in "0123456789":
        m['KEY_' + d] = _KeyCode.from_char(d)
    m.update({
        'KEY_SPACE':      _Key.space,
        'KEY_ENTER':      _Key.enter,
        'KEY_BACKSPACE':  _Key.backspace,
        'KEY_TAB':        _Key.tab,
        'KEY_ESC':        _Key.esc,
        'KEY_CAPSLOCK':   _Key.caps_lock,
        'KEY_LEFTSHIFT':  _Key.shift,
        'KEY_RIGHTSHIFT': _Key.shift_r,
        'KEY_LEFTCTRL':   _Key.ctrl,
        'KEY_RIGHTCTRL':  _Key.ctrl_r,
        'KEY_LEFTALT':    _Key.alt,
        'KEY_RIGHTALT':   _Key.alt_r,
        'KEY_LEFTMETA':   _Key.cmd,    # Windows / Super key
        'KEY_LEFTWIN':    _Key.cmd,
        'KEY_MINUS':      _KeyCode.from_char('-'),
        'KEY_EQUAL':      _KeyCode.from_char('='),
        'KEY_DOT':        _KeyCode.from_char('.'),
        'KEY_COMMA':      _KeyCode.from_char(','),
        'KEY_SLASH':      _KeyCode.from_char('/'),
        'KEY_BACKSLASH':  _KeyCode.from_char('\\'),
        'KEY_SEMICOLON':  _KeyCode.from_char(';'),
        'KEY_APOSTROPHE': _KeyCode.from_char("'"),
        'KEY_GRAVE':      _KeyCode.from_char('`'),
        'KEY_LEFTBRACE':  _KeyCode.from_char('['),
        'KEY_RIGHTBRACE': _KeyCode.from_char(']'),
        'KEY_QUESTION':   _KeyCode.from_char('?'),
        'KEY_INSERT':     _Key.insert,
        'KEY_LEFT':       _Key.left,
        'KEY_RIGHT':      _Key.right,
        'KEY_UP':         _Key.up,
        'KEY_DOWN':       _Key.down,
        'KEY_PAGEUP':     _Key.page_up,
        'KEY_PAGEDOWN':   _Key.page_down,
        'KEY_HOME':       _Key.home,
        'KEY_END':        _Key.end,
        # Media transport keys (driven by the Steam + left-stick chords).
        'KEY_VOLUMEUP':    _Key.media_volume_up,
        'KEY_VOLUMEDOWN':  _Key.media_volume_down,
        'KEY_MUTE':        _Key.media_volume_mute,
        'KEY_PREVIOUSSONG': _Key.media_previous,
        'KEY_NEXTSONG':    _Key.media_next,
        'KEY_PLAYPAUSE':   _Key.media_play_pause,
        # Print Screen (VK_SNAPSHOT = 0x2C)
        'KEY_SYSRQ':       _KeyCode.from_vk(0x2C),
        # Numpad  raw virtual keys so games see the real numpad scancodes
        # (VK_NUMPAD0..9 = 0x60..0x69, then * + - . / = 0x6A/6B/6D/6E/6F).
        'KEY_KPASTERISK':  _KeyCode.from_vk(0x6A),
        'KEY_KPPLUS':      _KeyCode.from_vk(0x6B),
        'KEY_KPMINUS':     _KeyCode.from_vk(0x6D),
        'KEY_KPDOT':       _KeyCode.from_vk(0x6E),
        'KEY_KPSLASH':     _KeyCode.from_vk(0x6F),
        'KEY_KPENTER':     _Key.enter,
    })
    for _n in range(10):
        m['KEY_KP%d' % _n] = _KeyCode.from_vk(0x60 + _n)
    # Function row (the 75% layout's top row). Not in the dict literal above
    # because pynput exposes them as Key.f1..Key.f12  a plain loop.
    for _n in range(1, 13):
        m['KEY_F%d' % _n] = getattr(_Key, 'f%d' % _n)
    # NOTE: KEY_SELECT is deliberately ABSENT. It is the sentinel keycode of
    # the on-screen "Select" key, whose whole behaviour lives in the pad/mouse
    # hold-and-drag handlers  leaving it unmapped means a stray dispatch of it
    # can never send a real keystroke.
    return m


_KEYMAP = _build_keymap()


# --- UIPI injection gate ---------------------------------------------------
# Windows' User Interface Privilege Isolation drops EVERY injected event a
# medium-integrity process aims at a higher-integrity window (Task Manager,
# regedit, an elevated console, the UAC consent desktop) -- SendInput is
# documented as "applications are permitted to inject input only into
# applications that are at an equal or lesser integrity level". While that is
# the case the tray hands control back to the controller's own firmware
# (lizard mode), whose reports enter through the HID driver stack and are not
# injected at all, so UIPI never sees them. That leaves the two sources
# fighting over the cursor, so the tray also flips this gate (see
# tray._UipiGuard) and our own injection goes quiet for the duration.
#
# RELEASES ARE NEVER SUPPRESSED. Engaging mid-hold must not strand a modifier
# at the OS level, so only press / move / scroll are gated -- a release always
# goes through, even if its press did not.
_suppressed = False

# --- uiAccess relay route ---------------------------------------------------
# Better than the lizard fallback when it's available: a uiAccess helper
# (uia_relay.py) is exempt from the UIPI filter, so routing through it keeps
# the user's REAL bindings, the pad cursor and the on-screen keyboard alive on
# an administrator window instead of dropping them for the firmware's fixed
# layout. The tray prefers it and only falls back to lizard mode when the
# relay isn't installed/signed (see tray._UipiGuard.update).
#
# _relay_active is set INSTEAD of _suppressed, never alongside it: one says
# "send it somewhere else", the other says "don't send it at all".
_relay = None
_relay_active = False


def set_relay(client):
    """Install the relay client object (uia_client.CLIENT), or None."""
    global _relay
    _relay = client


def set_relay_active(on):
    """Route injected events through the relay instead of pynput."""
    global _relay_active
    _relay_active = bool(on)


def relay_ready():
    """True when a relay is installed AND actually holds uiAccess."""
    return _relay is not None and _relay.ready()


def relay_installed():
    """Whether a relay exists on disk (cached). Input sources check this
    BEFORE doing any elevated-window work, so a user without the helper pays
    nothing at all for the feature."""
    return _relay is not None and _relay.installed()


def relay_forget_install():
    """Re-scan for the relay after the in-app installer runs."""
    if _relay is not None:
        _relay.forget_install()


def relay_start():
    """Try to bring the relay up. Rate-limited inside the client."""
    return _relay is not None and _relay.start()


def relay_reason():
    return _relay.reason() if _relay is not None else "no relay"


def _routed():
    """The relay to send through right now, or None for the normal path."""
    if _relay_active and _relay is not None and _relay.ready():
        return _relay
    return None


def set_suppressed(on):
    """Silence (or re-enable) injected presses/moves/scrolls process-wide."""
    global _suppressed
    _suppressed = bool(on)


def suppressed():
    """True while injection is gated -- also read by the tray's direct
    mouse_event call sites (wheel notches, mouse Back/Forward), which bypass
    these wrappers. False while the RELAY is carrying the input: those call
    sites have their own relay handling and must not drop the event."""
    return _suppressed and not _relay_active


def _vk_of(key):
    """Virtual-key code for a resolved pynput key, or None. Key.<name> wraps a
    KeyCode in .value; KeyCode carries .vk directly (and for a printable char
    with no vk, VkKeyScan resolves one)."""
    k = getattr(key, "value", key)
    vk = getattr(k, "vk", None)
    if vk:
        return vk
    ch = getattr(k, "char", None)
    if ch:
        try:
            v = ctypes.windll.user32.VkKeyScanW(ord(ch)) & 0xFF
            if v not in (0, 0xFF):
                return v
        except Exception:
            return None
    return None


# PRIVATE handles for the raw SendInput / clipboard work below.
# ctypes.windll.user32 is a PROCESS-WIDE singleton that pynput ALSO binds to
# (pynput._util.win32 exposes the same function objects  VkKeyScan IS
# user32.VkKeyScanW). Setting restype/argtypes on the shared handle breaks
# pynput's next call with "'str' object cannot be interpreted as an integer",
# which kills every normal letter after one variant paste. Private handles keep
# our argtypes mutations off pynput's.
_U32 = ctypes.WinDLL("user32.dll", use_last_error=False)
_K32 = ctypes.WinDLL("kernel32.dll", use_last_error=False)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


class Keyboard:
    def __init__(self):
        self._kb = _Controller()

    def _resolve(self, code):
        if isinstance(code, str):
            return _KEYMAP.get(code)
        return None

    def pressEvent(self, keys):
        relay = _routed()
        if relay is None and _suppressed:
            return
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            if relay is not None:
                vk = _vk_of(k)
                if vk and relay.key(vk, True):
                    continue
                # No vk, or the relay dropped out mid-stream: fall through to
                # the normal path rather than losing the keystroke entirely.
            try:
                self._kb.press(k)
            except Exception as e:
                print(f"uinput: press {code!r} failed: {e}")

    def releaseEvent(self, keys):
        relay = _routed()
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            if relay is not None:
                vk = _vk_of(k)
                if vk and relay.key(vk, False):
                    continue
            try:
                self._kb.release(k)
            except Exception as e:
                print(f"uinput: release {code!r} failed: {e}")

    def type_text(self, text):
        """Type a literal character (or short string) as itself.

        The phone layout's symbol pages need keys that produce '@', '{', '%' 
        and '€', 'π', '√', '™'  directly, with no Shift held and regardless of
        the user's keyboard layout. A keycode can't express that: KEY_2 only
        gives '@' if Shift happens to be down, and there is no keysym at all for
        the typographic ones.

        pynput's Windows backend already does exactly the right thing here  it
        uses the plain virtual key only when the char needs NO modifier, and
        otherwise falls back to a KEYEVENTF_UNICODE scan code, which types the
        character literally. So this deliberately hands it the char and does not
        go through _KEYMAP.

        The uiAccess relay is skipped on purpose: it forwards raw virtual keys,
        and VkKeyScan strips the shift state a char like '@' depends on, so
        relaying would silently type '2'. Symbols on elevated windows keep the
        pre-existing limitation rather than emitting the wrong character."""
        if _suppressed:
            return
        for ch in text:
            try:
                k = _KeyCode.from_char(ch)
                self._kb.press(k)
                self._kb.release(k)
            except Exception as e:
                print(f"uinput: type {ch!r} failed: {e}")

    def tap_with_modifier(self, modifier_code, key_code):
        """Press modifier+key as a true virtual-key chord, then release it.

        pynput injects printable character keys (KeyCode.from_char, vk=None) as
        char/Unicode events that DON'T combine with a held modifier  so Ctrl+'v'
        (paste) or Win+'.' (emoji) come through as a plain 'v' / '.' instead of
        the shortcut (and inconsistently, since the char→vk resolution is
        state-dependent). Resolving the char to its raw virtual key via
        VkKeyScan and pressing THAT makes it combine with the modifier reliably.
        """
        if _suppressed:
            return
        mod = self._resolve(modifier_code)
        key = self._resolve(key_code)
        if mod is None or key is None:
            return
        try:
            ch = getattr(key, "char", None)
            if ch:
                vk = ctypes.windll.user32.VkKeyScanW(ord(ch)) & 0xFF
                if vk not in (0, 0xFF):
                    key = _KeyCode.from_vk(vk)
        except Exception:
            pass
        try:
            self._kb.press(mod)
            time.sleep(0.01)          # let the OS register the modifier first
            self._kb.press(key)
            self._kb.release(key)
            self._kb.release(mod)
        except Exception as e:
            print(f"uinput: tap_with_modifier {key_code!r} failed: {e}")


    def reset_shift_state(self):
        """Drop pynput's INTERNAL shift/caps bookkeeping without sending any
        OS key event.

        pynput's Controller._resolve() uppercases any character key while this
        instance's shift_pressed is True, and shift_pressed is INSTANCE-LOCAL
        (Key.shift in _modifiers, or _caps_lock). A Shift pressed on this
        instance and released on the controller thread's SEPARATE instance
        (the paste/emoji/arrow re-press, see vkb.on_key_paste), or a single tap
        of the on-screen Caps key (whose OS-side effect PowerToys remaps away),
        leaves this instance believing Shift is held forever — so every letter
        comes out uppercase regardless of the real OS shift state. Clearing the
        internal flags here makes the injected letter's case follow the REAL
        OS shift only."""
        try:
            kb = self._kb
            # pynput internals (exist at runtime; absent from its stubs).
            with kb._modifiers_lock:  # type: ignore[reportAttributeAccessIssue]
                for mod in (
                    kb._Key.shift.value,
                    kb._Key.shift_l.value,
                    kb._Key.shift_r.value,
                ):
                    kb._modifiers.discard(mod)  # type: ignore[reportAttributeAccessIssue]
            kb._caps_lock = False  # type: ignore[reportAttributeAccessIssue]
        except Exception:
            pass

    def reset_modifier_state(self):
        """Clear pynput's ENTIRE internal modifier bookkeeping (Shift, Ctrl,
        Alt, Meta) without sending OS events. The OSK's raw SendInput paths
        (variant paste Ctrl+V, AltGr chords) do NOT update pynput's internal
        _modifiers set, so a stale entry there makes every subsequent NORMAL
        letter (which goes through pynput) type as if a modifier were held —
        "nothing shows up", surviving OSK reopen because the module-global
        pynput Keyboard is never recreated, and physical keys don't clear
        internal state. Called at OSK open so a fresh session starts clean."""
        try:
            kb = self._kb
            # pynput internals (exist at runtime; absent from its stubs).
            with kb._modifiers_lock:  # type: ignore[reportAttributeAccessIssue]
                kb._modifiers.clear()  # type: ignore[reportAttributeAccessIssue]
            kb._caps_lock = False  # type: ignore[reportAttributeAccessIssue]
        except Exception:
            pass

    def force_caps_off(self):
        """Ensure OS Caps Lock is OFF, so the OSK types lowercase by default
        and Shift alone controls capitalization.

        The OSK Caps key / L3 send KEY_CAPSLOCK, which PowerToys Keyboard
        Manager remaps to Esc (Caps -> Esc), so the OS caps state can never
        be toggled through the OSK — if caps is ON (e.g. toggled on the
        physical keyboard), every typed letter comes out uppercase and the
        OSK can't turn it off. This forces caps OFF by sending the Caps
        hardware SCAN CODE (wVk=0, KEYEVENTF_SCANCODE) via SendInput when
        the current state is ON — PowerToys has no virtual key to remap,
        so the real Caps Lock toggles off."""
        try:
            # Private handle: pynput shares ctypes.windll.user32 and binds
            # SendInput to it — our argtypes mutation would break pynput.
            user32 = _U32
            user32.GetKeyState.restype = ctypes.c_short
            user32.GetKeyState.argtypes = [ctypes.c_int]
            if not (user32.GetKeyState(0x14) & 1):
                return  # caps already off
            ins = (_INPUT * 2)()
            ins[0].type = 1
            ins[0].u.ki.wVk = 0
            ins[0].u.ki.wScan = 0x3A
            ins[0].u.ki.dwFlags = 0x0008  # KEYEVENTF_SCANCODE
            ins[1].type = 1
            ins[1].u.ki.wVk = 0
            ins[1].u.ki.wScan = 0x3A
            ins[1].u.ki.dwFlags = 0x0008 | 0x0002  # SCANCODE | KEYUP
            user32.SendInput.restype = ctypes.c_uint
            user32.SendInput.argtypes = [
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            user32.SendInput(
                2, ctypes.cast(ins, ctypes.c_void_p), ctypes.sizeof(_INPUT) * 2
            )
        except Exception:
            pass

    def tap_char(self, char):
        """Type a single Unicode character.

        Three injection paths, tried in order:
          1. Real virtual key (VkKeyScanW) — accepted by EVERY app that handles
             normal typing (games, raw-input apps). Only used when the char's
             key is plain or shift-only on the active layout: an AltGr form
             (state bits CTRL|ALT = 0x06, e.g. Polish ą ę ś ż) must NOT be
             tapped as the bare VK, which would type the unaccented letter.
          2. Clipboard paste (Ctrl+V) — works where UNICODE is ignored: games
             commonly drop KEYEVENTF_UNICODE while still handling Ctrl+V.
          3. SendInput KEYEVENTF_UNICODE — for apps that accept WM_CHAR.
        Returns True if any path injected the char.

        Honors the process-wide injection gate: while `_suppressed` and no
        relay is carrying the input, nothing is sent  the same contract
        pressEvent and type_text follow, so a paused OSK never types behind
        the user's back."""
        if not char or len(char) != 1 or ord(char) > 0xFFFF:
            # Single character only: every path below is a one-codepoint
            # injection, and ord() on a longer string raises.
            return False
        if _routed() is None and _suppressed:
            return False
        try:
            # PRIVATE handle: ctypes.windll.user32 is a shared singleton that
            # pynput also binds to (pynput._util.win32.VkKeyScan IS
            # user32.VkKeyScanW). Mutating its argtypes here would make pynput's
            # next letter call fail with "'str' object cannot be interpreted as
            # an integer" — the exact "letters stop after a variant" bug. Load a
            # private copy so our restype/argtypes can't leak into pynput.
            user32 = _U32
            user32.VkKeyScanW.restype = ctypes.c_short
            user32.VkKeyScanW.argtypes = [ctypes.c_uint]
            scan = user32.VkKeyScanW(ord(char)) & 0xFFFF
            state_bits = (scan >> 8) & 0xFF
            # Plain (0x00) or shift-only (0x01) keys are safe to tap as the
            # bare VK. AltGr (0x06) or other modifier forms are NOT — the VK
            # alone produces the wrong (unaccented) letter, so skip to paste.
            # VkKeyScanW modifier byte: 0x00 = plain, 0x01 = shift, 0x06 =
            # AltGr (Ctrl+Alt). Only plain and shift-only forms are safe to
            # tap as a bare VK — a synthesized Ctrl+Alt chord is NOT accepted
            # as AltGr by most apps (they recognize AltGr by its SCANCODE, so
            # the chord degrades to the unaccented letter, e.g. ś → s; the log
            # proves it). AltGr forms (0x06) fall through to paste/UNICODE,
            # which type the exact char.
            if scan != 0xFFFF and state_bits in (0x00, 0x01):
                # The case-carrying row already uppercased the variant when
                # Shift is logically held, so the char's VkKeyScanW shift bit
                # and the REAL held-Shift state agree. Only synthesize Shift
                # when it's actually needed AND not already held (L2/latched
                # Shift holds it on the OS) — pressing VK_SHIFT down/up over a
                # held shift would drop the user's shift early.
                shift_needed = state_bits & 0x01
                shift_held = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
                ok = self._tap_vk(
                    scan & 0xFF, bool(shift_needed) and not shift_held
                )
                return ok
        except Exception as e:
            print(f"uinput: tap_char {char!r} vk failed: {e}")
        # UNICODE first: it provably works in editors/File Explorer (the log's
        # 'á unicode ok=True'), types the exact char, and has no clipboard
        # side effects. Paste is only the fallback for apps that hard-reject
        # UNICODE (sent != 2).
        if self._send_unicode(char):
            return True
        if self._paste_char(char):
            # The paste sent raw Ctrl+V via SendInput, which pynput (used for
            # NORMAL letters) does not track. Clear its internal modifier set
            # so the next normal letter isn't typed with a phantom Ctrl.
            self.reset_modifier_state()
            return True
        return False

    def _send_unicode(self, char):
        """Inject `char` as a KEYEVENTF_UNICODE SendInput pair. Layout-
        independent, but many games/raw-input apps ignore UNICODE events.

        Uses a PRIVATE user32 handle: pynput also binds to the shared
        ctypes.windll.user32, and our restype/argtypes mutations would break
        pynput's next SendInput/VkKeyScan call."""
        try:
            ins = (_INPUT * 2)()
            for i, flags in enumerate((0x0004, 0x0004 | 0x0002)):
                ins[i].type = 1
                ins[i].u.ki.wVk = 0
                ins[i].u.ki.wScan = ord(char)
                ins[i].u.ki.dwFlags = flags
            user32 = _U32
            user32.SendInput.restype = ctypes.c_uint
            user32.SendInput.argtypes = [
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            sent = user32.SendInput(
                2, ctypes.cast(ins, ctypes.c_void_p), ctypes.sizeof(_INPUT) * 2
            )
            return sent == 2
        except Exception as e:
            print(f"uinput: tap_char {char!r} unicode failed: {e}")
            return False

    def _paste_char(self, char):
        """Type `char` via the clipboard: snapshot whatever text is on the
        clipboard, replace it with `char`, send Ctrl+V, then restore the
        snapshot. Games and raw-input apps that ignore KEYEVENTF_UNICODE still
        accept Ctrl+V, so this is the reliable fallback for accents with no
        plain VK on the active layout.

        Runs on the main (SDL) thread, so it MUST never block: OpenClipboard
        can wait indefinitely when another process holds the clipboard, which
        would freeze the whole OSK (and base-letter injection). Every clipboard
        open is guarded by a short retry loop and abandoned on timeout.

        Uses PRIVATE user32/kernel32 handles: mutating the shared
        ctypes.windll functions' argtypes would break pynput (same objects)."""
        user32 = _U32
        kernel32 = _K32
        # Without restype/argtypes, ctypes coerces the returned 64-bit HGLOBAL
        # handle to c_int — truncated, so GlobalLock/SetClipboardData get a
        # garbage handle and paste always fails. Declare them once here.
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.OpenClipboard.restype = ctypes.c_int
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.CloseClipboard.restype = ctypes.c_int
        user32.CloseClipboard.argtypes = []
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.EmptyClipboard.argtypes = []
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        prev = None

        def _open_clipboard():
            for _ in range(10):
                if user32.OpenClipboard(None):
                    return True
                time.sleep(0.01)
            return False

        def _read():
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            if not _open_clipboard():
                return None
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return None
                p = kernel32.GlobalLock(h)
                if not p:
                    return None
                try:
                    return ctypes.wstring_at(p)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()

        def _write(text):
            if not _open_clipboard():
                return False
            try:
                user32.EmptyClipboard()
                buf = ctypes.create_unicode_buffer(text or "")
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(buf))
                if not h:
                    return False
                p = kernel32.GlobalLock(h)
                try:
                    ctypes.memmove(p, buf, ctypes.sizeof(buf))
                finally:
                    kernel32.GlobalUnlock(h)
                user32.SetClipboardData(CF_UNICODETEXT, h)
                return True
            except Exception:
                return False
            finally:
                # MUST always close: a process that leaves the clipboard open
                # locks it system-wide (OpenClipboard by ANY app then blocks),
                # which breaks paste everywhere AND can look like input died.
                user32.CloseClipboard()

        try:
            # Never leave a throttled Ctrl down from an earlier attempt.
            self._release_stuck_ctrl()
            prev = _read()
            if not _write(char):
                return False
            # The paste (Ctrl+V) and the restore are wrapped so the user's
            # clipboard is ALWAYS put back — even if the paste throws or the
            # process is killed mid-way, `prev` is restored in the finally.
            # Replacing the clipboard with `char` and dying before restoring
            # would silently destroy the user's clipboard contents.
            ok = False
            try:
                ok = self._tap_vk(0x56, ctrl=True)  # Ctrl+V
            finally:
                # Release Ctrl again in case the tap's key-up was throttled.
                self._release_stuck_ctrl()
                if prev is not None:
                    try:
                        # Give the focused app a moment to consume the paste
                        # before restoring the clipboard. Keep it short —
                        # this runs on the main thread.
                        time.sleep(0.02)
                        _write(prev)
                    except Exception:
                        pass
            return ok
        except Exception as e:
            print(f"uinput: paste {char!r} failed: {e}")
            return False

    def _tap_vk(self, vk, shift=False, ctrl=False, altgr=False):
        """Tap a raw virtual key (optionally with Shift / Ctrl held, or as an
        AltGr chord — Ctrl+Alt held). Sends via SendInput KEYBDINPUTs (wVk
        path), which games and raw-input apps accept even though they ignore
        KEYEVENTF_UNICODE.

        Each event is its own SendInput call and modifier key-UPs always fire,
        so a throttled/dropped event can never leave a modifier physically
        held down (the "all typing dies" bug). Modifier downs and ups are
        separate calls precisely so a partial delivery can't strand the key."""
        try:

            def _ev(wVk, flags):
                e = _INPUT()
                e.type = 1
                e.u.ki.wVk = wVk
                e.u.ki.wScan = 0
                e.u.ki.dwFlags = flags
                return e

            def _send(items):
                if not items:
                    return True
                ins = (_INPUT * len(items))(*items)
                # Private handle: pynput shares ctypes.windll.user32 and its
                # SendInput binding — mutating argtypes would break pynput.
                user32 = _U32
                user32.SendInput.restype = ctypes.c_uint
                user32.SendInput.argtypes = [
                    ctypes.c_uint,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                sent = user32.SendInput(
                    len(items),
                    ctypes.cast(ins, ctypes.c_void_p),
                    ctypes.sizeof(_INPUT) * len(items),
                )
                return sent == len(items)

            mods_down = []
            if shift:
                mods_down.append(_ev(0x10, 0))  # VK_SHIFT down
            if altgr:
                mods_down.append(_ev(0x11, 0))  # VK_CONTROL down
                mods_down.append(_ev(0x12, 0))  # VK_MENU down
            elif ctrl:
                mods_down.append(_ev(0x11, 0))  # VK_CONTROL down
            mods_up = []
            if altgr:
                mods_up.append(_ev(0x12, 0x0002))  # VK_MENU up
                mods_up.append(_ev(0x11, 0x0002))  # VK_CONTROL up
            elif ctrl:
                mods_up.append(_ev(0x11, 0x0002))  # VK_CONTROL up
            if shift:
                mods_up.append(_ev(0x10, 0x0002))  # VK_SHIFT up

            ok = True
            if mods_down:
                ok = _send(mods_down) and ok
            ok = _send([_ev(vk, 0)]) and ok
            ok = _send([_ev(vk, 0x0002)]) and ok
            if mods_up:
                ok = _send(mods_up) and ok
            return ok
        except Exception as e:
            print(f"uinput: tap_vk 0x{vk:02x} failed: {e}")
            return False

    def _release_stuck_ctrl(self):
        """Repair path: force every modifier key up if it is physically down.
        A throttled/dropped modifier key-up from an earlier injection leaves
        the OS key held, turning every later letter into a Ctrl/Shift/Alt
        shortcut (nothing types) — the exact symptom that survives OSK reopen
        because it is OS key state. A key-up on a not-held key is harmless
        (idempotent), so this is safe to run before each paste and at open.
        Uses keybd_event so the release itself cannot be throttled away."""
        try:
            # Private handle: pynput shares ctypes.windll.user32.
            user32 = _U32
            user32.GetAsyncKeyState.restype = ctypes.c_short
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            KEYUP = 0x0002
            # VK_CONTROL and VK_MENU only — NOT VK_SHIFT: Shift is legitimately
            # held by the user (L2 hold / latched Shift) while typing, and
            # releasing it here would drop the user's shift mid-press. Ctrl and
            # Alt are never intentionally held by the OSK outside a transient
            # paste, so an unexpected held Ctrl/Alt is always a stuck key.
            # Sent via SendInput (one KEYUP per key, matching _tap_vk).
            for vk in (0x11, 0x12, 0xA2, 0xA3, 0xA4, 0xA5):
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    e = _INPUT()
                    e.type = 1
                    e.u.ki.wVk = vk
                    e.u.ki.wScan = 0
                    e.u.ki.dwFlags = KEYUP
                    user32.SendInput.restype = ctypes.c_uint
                    user32.SendInput.argtypes = [
                        ctypes.c_uint,
                        ctypes.c_void_p,
                        ctypes.c_int,
                    ]
                    user32.SendInput(
                        1,
                        ctypes.cast(ctypes.byref(e), ctypes.c_void_p),
                        ctypes.sizeof(_INPUT),
                    )
        except Exception:
            pass


class Mouse:
    """Thin wrapper over pynput's mouse for relative cursor movement, so the
    stick-as-mouse code can stay symmetric with the Keyboard wrapper."""

    def __init__(self):
        self._m = _MouseController()

    def move(self, dx, dy):
        if not dx and not dy:
            return
        relay = _routed()
        if relay is not None:
            if relay.move(dx, dy):
                return
        elif _suppressed:
            return
        try:
            self._m.move(int(dx), int(dy))
        except Exception as e:
            print(f"uinput: mouse move ({dx},{dy}) failed: {e}")

    def get_position(self):
        """Absolute cursor position (x, y) in screen px; (0, 0) on failure.

        Free on Windows  pynput is GetCursorPos here. The Linux tree has to
        dead-reckon this instead: Wayland exposes no global cursor query, so
        there is simply nothing to read. See linux/steamcontroller/uinput.py.
        """
        try:
            px, py = self._m.position
            return int(px), int(py)
        except Exception as e:
            print(f"uinput: mouse get_position failed: {e}")
            return 0, 0

    def set_position(self, x, y):
        """Warp the cursor to an absolute screen position (used by the
        Video Timeline Scrubbing hover mode to ride the progress bar).

        Also free here  pynput is SetCursorPos. The Linux counterpart needs a
        whole extra uinput device advertising ABS_X/ABS_Y, because XTest (what
        pynput uses there) can't move a Wayland cursor at all."""
        if _suppressed:
            return
        try:
            self._m.position = (int(x), int(y))
        except Exception as e:
            print(f"uinput: mouse set_position ({x},{y}) failed: {e}")

    def press(self, button="left"):
        relay = _routed()
        if relay is not None:
            if relay.button(button, True):
                return
        elif _suppressed:
            return
        try:
            self._m.press(_MOUSE_BUTTONS[button])
        except Exception as e:
            print(f"uinput: mouse press {button} failed: {e}")

    def release(self, button="left"):
        relay = _routed()
        if relay is not None and relay.button(button, False):
            return
        try:
            self._m.release(_MOUSE_BUTTONS[button])
        except Exception as e:
            print(f"uinput: mouse release {button} failed: {e}")

    def scroll(self, dx, dy):
        if not dx and not dy:
            return
        relay = _routed()
        if relay is not None:
            # pynput scrolls in notches; the relay speaks wheel UNITS.
            if relay.wheel(int(dy) * 120):
                if not dx:
                    return
                if relay.wheel(int(dx) * 120, horizontal=True):
                    return
        elif _suppressed:
            return
        try:
            self._m.scroll(int(dx), int(dy))
        except Exception as e:
            print(f"uinput: mouse scroll ({dx},{dy}) failed: {e}")
