"""Locate the OSK's bundled assets (YAML layouts, fonts, glyphs, skins, lexicon).

Everything the OSK loads at runtime lives under one `data/` root:

    data/cfg/       keyboard-layout*.yaml      -> find_cfg_resource()
    data/fonts/     *.ttf                      -> find_data_resource()
    data/images/    glyphs/*.png               -> find_data_resource()
    data/skins/     *.css                      -> find_data_resource()
    data/lexicon/   en.txt                     -> find_data_resource()

The root is normally handed to us in `ADUSK_DATA` by whichever entry point
started the OSK (`tray.py`, `tray_linux.py`, `lockscreen_osk.py`,
`adusk_launcher.py`, `adusk_linux.py`)  each points it at the `data/` folder
next to the script, or inside the PyInstaller bundle when frozen.

Two deliberate differences from a plain env-var lookup:

* The variable is split on `os.pathsep`, not a hard-coded ":". On Windows a
  colon split tears the drive letter off "C:\\...\\data" and leaves a
  drive-relative "\\...\\data" that only resolves while the app happens to sit
  on the current drive. `os.pathsep` is ";" there, so the path survives intact.
* The roots are resolved lazily and cached, rather than captured at import
  time. Entry points still set `ADUSK_DATA` before importing `adusk.*`, but a
  caller that sets it afterwards no longer silently gets an empty search path.

If the variable is missing entirely we fall back to the `data/` folder shipped
alongside this package, via `sys._MEIPASS` when frozen  the same pattern the
rest of the tree uses to find bundled files.
"""

import os
import sys

_ENV_VAR = "ADUSK_DATA"

_cached_roots = None


def _bundled_data_dir():
    """`data/` as shipped next to the package, frozen or running from source."""
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        return os.path.join(frozen_base, "data")
    # .../<tree>/adusk/resources.py -> .../<tree>/data
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(package_dir), "data")


def _roots():
    """Ordered, de-duplicated list of existing data roots to search."""
    global _cached_roots
    if _cached_roots is not None:
        return _cached_roots

    candidates = []
    raw = os.environ.get(_ENV_VAR)
    if raw:
        candidates.extend(part for part in raw.split(os.pathsep) if part)
    candidates.append(_bundled_data_dir())

    roots = []
    for candidate in candidates:
        root = os.path.abspath(os.path.expanduser(candidate))
        if root not in roots and os.path.isdir(root):
            roots.append(root)

    _cached_roots = roots
    return roots


def reset_search_paths():
    """Drop the cached roots so the next lookup re-reads `ADUSK_DATA`."""
    global _cached_roots
    _cached_roots = None


def _first_existing(*relative_parts):
    for root in _roots():
        candidate = os.path.join(root, *relative_parts)
        if os.path.exists(candidate):
            return candidate
    return None


def find_cfg_resource(name):
    """Absolute path to a config file under `data/cfg/`, or None."""
    return _first_existing("cfg", name)


def find_data_resource(name):
    """Absolute path to an asset under `data/`, or None.

    `name` may contain forward slashes ("skins/dark.css", "images/glyphs/x.png");
    they are normalised for the host platform on the way out.
    """
    return _first_existing(*name.replace("\\", "/").split("/"))
