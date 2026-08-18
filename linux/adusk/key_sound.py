"""Steam's own on-screen-keyboard audio: key click, open, and close.

The Steam OSK plays three wavs out of the steamui web root, and they are the
sounds people associate with typing in Big Picture:

  key press -> steamui/sounds/deck_ui_typing.wav          (PN.Typing)
  open      -> steamui/sounds/deck_ui_side_menu_fly_in.wav  (PN.OpenSideMenu)
  close     -> steamui/sounds/deck_ui_side_menu_fly_out.wav (PN.CloseSideMenu)

Paths are resolved from the Steam install at runtime and cached, so the
per-key hot path only stats the files once and a machine without Steam simply
stays silent. The tray registers `play_key_sound` / `play_open_sound` /
`play_close_sound` as the hooks in `adusk.state`, which owns the on/off switch.
"""

import os
import sys
import threading
import time

_IS_WINDOWS = sys.platform == "win32"

# Sound name -> path relative to the Steam install root.
_SOUNDS = {
    "key": os.path.join("steamui", "sounds", "deck_ui_typing.wav"),
    "open": os.path.join("steamui", "sounds", "deck_ui_side_menu_fly_in.wav"),
    "close": os.path.join("steamui", "sounds", "deck_ui_side_menu_fly_out.wav"),
}

_path_cache = {}
_resolved = False
_resolve_lock = threading.Lock()

# Two-finger typing dispatches two keys back to back (one per thumb); a second
# click starting on top of the first doubles and garbles it. Collapse a burst
# into ONE clean tick.
_CLICK_DEBOUNCE_S = 0.025
_last_key_click = 0.0


def _steam_root():
    """Steam install root, or None. Registry first (the authoritative answer on
    Windows), then the handful of default install paths."""
    if _IS_WINDOWS:
        try:
            import winreg

            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for val in ("SteamPath", "InstallPath"):
                            try:
                                p = winreg.QueryValueEx(k, val)[0]
                                if p and os.path.isdir(p):
                                    return p
                            except FileNotFoundError:
                                pass
                except OSError:
                    pass
        except ImportError:
            pass
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam",
              os.path.expanduser("~/.steam/steam"),
              os.path.expanduser("~/.local/share/Steam"),
              os.path.expanduser(
                  "~/.var/app/com.valvesoftware.Steam/data/Steam")):
        if os.path.isdir(p):
            return p
    return None


def _sound_path(name):
    """Absolute path to one Steam keyboard sound, or None. Resolved once for
    all three names  the per-key path must never re-stat."""
    global _resolved
    if _resolved:
        return _path_cache.get(name)
    with _resolve_lock:
        if not _resolved:
            root = _steam_root()
            for n, rel in _SOUNDS.items():
                cand = os.path.join(root, rel) if root else None
                _path_cache[n] = cand if cand and os.path.isfile(cand) else None
            _resolved = True
    return _path_cache.get(name)


def available():
    """True when at least the key-click sound was found  the tray uses this to
    decide whether the setting can do anything on this machine."""
    return _sound_path("key") is not None


if _IS_WINDOWS:
    import winsound

    def _play(name):
        p = _sound_path(name)
        if not p:
            return
        try:
            # SND_ASYNC so the render loop never blocks on audio; SND_NODEFAULT
            # so a missing file is silence rather than the system beep.
            winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
        except Exception:
            pass
else:
    import shutil
    import subprocess

    # Linux has no winsound: shell out to whichever tiny wav player is present.
    # Resolved once  a missing player means silence, never a per-key spawn
    # attempt that fails.
    _PLAYERS = (("pw-play", ()), ("paplay", ()), ("aplay", ("-q",)),
                ("ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet")))
    _player = None
    _player_resolved = False

    def _find_player():
        global _player, _player_resolved
        if not _player_resolved:
            for exe, args in _PLAYERS:
                path = shutil.which(exe)
                if path:
                    _player = (path,) + args
                    break
            _player_resolved = True
        return _player

    def _play(name):
        p = _sound_path(name)
        if not p:
            return
        cmd = _find_player()
        if not cmd:
            return
        try:
            subprocess.Popen(list(cmd) + [p], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception:
            pass


def play_key_sound():
    """One keyboard click. Debounced, so a two-finger burst ticks once."""
    global _last_key_click
    now = time.monotonic()
    if now - _last_key_click < _CLICK_DEBOUNCE_S:
        return
    _last_key_click = now
    _play("key")


def play_open_sound():
    _play("open")


def play_close_sound():
    _play("close")
