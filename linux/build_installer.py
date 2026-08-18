"""Build the SteamlessInput install wizard into `dist/SteamlessInput-Setup`.

Run on Linux:
    python3 build_installer.py                # setup binary only
    python3 build_installer.py --bundle       # + the app baked in (one file)

Mirror of `windows/build_installer.py`, with the same two distribution shapes:

* **Unbundled** (default)  the setup binary finds `SteamlessInput` beside
  itself, which is how the release tarball is laid out. Small and fast.
* **Bundled** (`--bundle`)  the app binary is embedded under `payload/`, so a
  single downloaded file installs with nothing else present.

Note the wizard does NOT need the GTK/AppIndicator stack that the tray needs 
that stack is one of the things it installs. It is tkinter-only, so it runs on
a machine that can't yet run the app, which is the whole point.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PROJECT_DIR)
ENTRY = "installer.py"
OUTPUT_NAME = "SteamlessInput-Setup"
DIST = os.path.join(PROJECT_DIR, "dist")


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Build the SteamlessInput Linux install wizard.")
    p.add_argument("--bundle", action="store_true",
                   help="embed the app binary so the setup file installs with "
                        "no sibling files")
    return p.parse_args(argv)


def _check_platform():
    if not sys.platform.startswith("linux"):
        raise SystemExit(
            f"build_installer.py must be run on Linux (got {sys.platform!r}). "
            "Use windows/build_installer.py for the Windows wizard.")


def _payload_items():
    app = os.path.join(DIST, "SteamlessInput")
    if not os.path.isfile(app):
        raise SystemExit("dist/SteamlessInput not found  run "
                         "`python3 build.py` first, or drop --bundle.")
    items = [(app, "payload")]
    icon = os.path.join(DIST, "SteamlessInput.png")
    if os.path.isfile(icon):
        items.append((icon, "payload"))
    for doc in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        p = os.path.join(REPO_DIR, doc)
        if os.path.isfile(p):
            items.append((p, "payload"))
    return items


def _small_assets():
    """Icon/logo the wizard draws with, regardless of what sits beside it."""
    out = []
    for rel, dest in (
            (os.path.join("data", "images", "app_icon.ico"), "data/images"),
            # The 64px pre-downscaled logo (PIL LANCZOS, see installer.py's
            # _build_chrome)  not the full-res source, which the wizard only
            # falls back to via naive subsample() in a dev checkout without it.
            (os.path.join("assets", "SteamlessController_seethrough_64.png"),
             "assets"),
    ):
        p = os.path.join(PROJECT_DIR, rel)
        if os.path.isfile(p):
            out.append((p, dest))
    return out


def _run_pyinstaller(bundle):
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", OUTPUT_NAME,
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
    ]
    for src, dest in _small_assets():
        cmd += ["--add-data", f"{src}:{dest}"]

    # The wizard is stdlib-only. Excluding the app's own dependency stack keeps
    # the setup binary small AND keeps it runnable on a machine that doesn't
    # have that stack yet  which is exactly the machine it has to run on.
    for mod in ("numpy", "scipy", "PIL", "pandas", "matplotlib",
                "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython",
                "pytest", "setuptools", "pip", "gi", "pystray", "pynput",
                "evdev", "hid", "sdl3w", "yaml"):
        cmd += ["--exclude-module", mod]

    if bundle:
        for src, dest in _payload_items():
            flag = "--add-binary" if os.access(src, os.X_OK) else "--add-data"
            cmd += [flag, f"{src}:{dest}"]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _cleanup():
    """Same policy as the Windows build: keep PyInstaller's analysis cache,
    drop the big regenerable staging blobs."""
    build_dir = os.path.join(PROJECT_DIR, "build", OUTPUT_NAME)
    if not os.path.isdir(build_dir):
        return
    p = os.path.join(build_dir, "localpycs")
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    for pattern in ("PKG-*.pkg",):
        for f in glob.glob(os.path.join(build_dir, pattern)):
            try:
                os.remove(f)
            except OSError:
                pass


def main(argv=None):
    args = _parse_args(argv)
    _check_platform()

    _run_pyinstaller(args.bundle)
    _cleanup()

    out = os.path.join(DIST, OUTPUT_NAME)
    if os.path.isfile(out):
        os.chmod(out, 0o755)
        size = os.path.getsize(out) / (1024 * 1024)
        print(f"\nbuilt: {out}  ({size:.1f} MB)")
    if not args.bundle:
        print("this setup binary expects SteamlessInput beside it "
              "(--bundle embeds it instead)")


if __name__ == "__main__":
    main()
