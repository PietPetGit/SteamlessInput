"""Build the SteamlessInput install wizard into `dist/SteamlessInput-Setup.exe`.

Run:
    python build_installer.py                 # setup exe only (a few MB)
    python build_installer.py --bundle        # + the app baked in (one file)

Two shapes, because the two distributions want different things:

* **Unbundled** (default)  the setup exe carries only its own UI plus the
  couple of small files it needs to talk to Windows (the app icon, the relay
  install script). It finds `SteamlessInput-windows.exe` and the optional
  add-ons *beside itself*, which is exactly how the release zip is laid out.
  Fast to build, and the release zip does not carry the 30 MB app twice.

* **Bundled** (`--bundle`)  everything the wizard can install is embedded
  under `payload/` in the setup exe, so a single download installs offline.
  Roughly doubles the release size, which is why it isn't the default.

The lock-screen keyboard and the uiAccess relay are only bundled when they have
already been built (`build.py` without --skip-lockscreen, `build_uia_relay.py`);
the wizard shows those components as unavailable when they're missing rather
than failing, so a partial payload is a supported state.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = "installer.py"
OUTPUT_NAME = "SteamlessInput-Setup"
APP_ICON_ICO = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")
DIST = os.path.join(PROJECT_DIR, "dist")


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Build the SteamlessInput Windows install wizard.")
    p.add_argument("--bundle", action="store_true",
                   help="embed the app (and any built add-ons) in the setup "
                        "exe so it installs with no sibling files")
    return p.parse_args(argv)


def _rel(*parts):
    return os.path.join(PROJECT_DIR, *parts)


def _payload_items():
    """(source, dest-inside-bundle) pairs for --bundle, skipping what isn't
    built yet."""
    items = []
    app = _rel("dist", "SteamlessInput-windows.exe")
    if not os.path.isfile(app):
        raise SystemExit(
            "dist\\SteamlessInput-windows.exe not found  run "
            "`python build.py --skip-lockscreen` first, or drop --bundle.")
    items.append((app, "payload"))

    # --onedir: the exe is inert without _internal/, so a --bundle setup exe
    # has to embed that tree too or the payload it unpacks will not run.
    # ONE directory entry, not one per file: _internal is ~1700 files, and
    # naming them individually pushed the PyInstaller command line past
    # Windows' 32 KB limit outright (WinError 206). PyInstaller copies a
    # directory source recursively, which is exactly what a payload wants 
    # --add-data (not --add-binary) so it is carried verbatim instead of being
    # run through binary dependency analysis a second time.
    internal = _rel("dist", "_internal")
    if not os.path.isdir(internal):
        raise SystemExit(
            r"dist\_internal not found  the app build is --onedir now; run "
            "`python build.py --skip-lockscreen` first, or drop --bundle.")
    items.append((internal, "payload/_internal"))

    lock = _rel("lockscreen-keyboard", "LockScreenKeyboard.exe")
    if os.path.isfile(lock):
        items.append((lock, "payload/lockscreen-keyboard"))
    else:
        print("note: LockScreenKeyboard.exe not built  the lock-screen "
              "component will show as unavailable")

    relay_dir = _rel("dist", "uia-relay")
    if os.path.isdir(relay_dir):
        for path in glob.glob(os.path.join(relay_dir, "**", "*"),
                              recursive=True):
            if os.path.isfile(path):
                sub = os.path.relpath(os.path.dirname(path), relay_dir)
                dest = "payload/uia-relay"
                if sub not in (".", ""):
                    dest += "/" + sub.replace(os.sep, "/")
                items.append((path, dest))
    else:
        print("note: dist\\uia-relay not built  the input-relay component "
              "will show as unavailable")

    for doc in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        p = os.path.join(os.path.dirname(PROJECT_DIR), doc)
        if os.path.isfile(p):
            items.append((p, "payload"))
    return items


def _run_pyinstaller(bundle):
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", OUTPUT_NAME,
        "--icon", APP_ICON_ICO,
        # The wizard is stdlib-only, but PyInstaller can't see tkinter through
        # the lazy `import tkinter` inside main(), and autostart is imported by
        # name from _shortcut_api().
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        "--hidden-import", "autostart",
        # Small files the wizard reads at runtime regardless of what sits
        # beside it: its own window icon/logo, the font that matches the app,
        # and the relay installer it shells out to.
        "--add-data", f"{APP_ICON_ICO};data/images",
        # The 64px pre-downscaled logo (PIL LANCZOS, see installer.py's
        # _build_chrome)  not the full-res source, which the wizard only
        # falls back to via naive subsample() in a dev checkout without it.
        "--add-data", f"{_rel('assets', 'SteamlessController_seethrough_64.png')};assets",
        "--add-data", f"{_rel('data', 'fonts', 'PlusJakartaSans-Regular.ttf')};data/fonts",
        "--add-data", f"{_rel('install_uia_relay.ps1')};.",
    ]
    # Nothing here needs the scientific stack or an image library; PIL's hook
    # would otherwise drag numpy in through a transitive import and turn a
    # 12 MB setup exe into a 40 MB one.
    for mod in ("numpy", "scipy", "PIL", "pandas", "matplotlib",
                "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython",
                "pytest", "setuptools", "pip", "pystray", "pynput",
                "vgamepad", "sdl3w", "hid"):
        cmd += ["--exclude-module", mod]

    if bundle:
        for src, dest in _payload_items():
            flag = "--add-binary" if src.lower().endswith(
                (".exe", ".dll")) else "--add-data"
            cmd += [flag, f"{src};{dest}"]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


# PowerShell does not redirect stdout for GUI-subsystem processes  proven by
# flipping ONLY the PE subsystem byte on a copy of the setup exe: as subsystem 2
# `setup.exe --list > out.txt` writes 0 bytes, as subsystem 3 the same bytes
# write 305. So `--console`/`--list` output vanishes for PowerShell users doing
# the most natural thing.
#
# Building the wizard console-subsystem instead would flash a console window on
# every double-click, and shipping a second 11 MB console-subsystem copy to fix
# log capture is out of proportion. cmd.exe IS console-subsystem, so PowerShell
# redirects ITS stdout, and the GUI exe launched from it inherits that
# already-redirected handle  which _bind_std_streams() then picks up from
# GetStdHandle. ~50 bytes, and redirection, pipes and exit codes all work.
CLI_SHIM_NAME = "SteamlessInput-Setup.cmd"
CLI_SHIM = """\
@echo off
rem Console-subsystem entry point for SteamlessInput-Setup.exe.
rem
rem Use this one from PowerShell when you want to capture output:
rem     SteamlessInput-Setup.cmd --list > components.txt
rem PowerShell's ">" does not capture GUI-subsystem programs, so redirecting
rem the .exe directly yields an empty file. Going through cmd fixes that; the
rem exit code is passed straight back. The .exe remains the thing to
rem double-click for the graphical wizard.
"%~dp0SteamlessInput-Setup.exe" %*
"""


def _write_cli_shim():
    path = os.path.join(DIST, CLI_SHIM_NAME)
    os.makedirs(DIST, exist_ok=True)
    # CRLF: a .cmd with bare LF line endings is parsed unreliably by cmd.exe.
    with open(path, "w", encoding="ascii", newline="\r\n") as f:
        f.write(CLI_SHIM)
    print(f"cli shim: {path}")


def _cleanup():
    """Same policy as build.py: drop the big regenerable staging blobs, keep
    PyInstaller's analysis cache so the next build stays incremental."""
    build_dir = os.path.join(PROJECT_DIR, "build", OUTPUT_NAME)
    if not os.path.isdir(build_dir):
        return
    p = os.path.join(build_dir, "localpycs")
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    for pattern in ("PKG-*.pkg", "*.exe"):
        for f in glob.glob(os.path.join(build_dir, pattern)):
            try:
                os.remove(f)
            except OSError:
                pass


def main(argv=None):
    args = _parse_args(argv)
    if not os.path.isfile(APP_ICON_ICO):
        raise SystemExit(f"app icon not found: {APP_ICON_ICO}")

    _run_pyinstaller(args.bundle)
    _cleanup()
    _write_cli_shim()

    out = os.path.join(DIST, f"{OUTPUT_NAME}.exe")
    size = os.path.getsize(out) / (1024 * 1024) if os.path.isfile(out) else 0
    print(f"\nbuilt: {out}  ({size:.1f} MB)")
    if not args.bundle:
        print("this setup exe expects SteamlessInput-windows.exe beside it "
              "(--bundle embeds it instead)")


if __name__ == "__main__":
    main()
