"""Build script for the Linux on-screen-keyboard build.

Run on a Linux machine (NOT inside WSL unless your distro can actually
talk to the display server you're targeting):

    python build_linux.py

Produces a release-ready tarball next to the unpacked binary in dist/:

    dist/SteamlessInput               # ELF binary (no .exe  Linux convention)
    dist/SteamlessInput.png           # 256x256 icon
    dist/SteamlessInput.desktop       # portable launcher (uses %k for paths)
    dist/LICENSE                         # bundled into the tarball
    dist/SteamlessInput-linux.tar.gz  # ← upload this to a GitHub Release

Scope is intentionally narrow: only the on-screen keyboard is ported.
Tray, autostart, Steam-detection, and the ViGEm virtual gamepad stay
Windows-only for now.

System prerequisites (names vary by distro; Arch/CachyOS shown):
    sudo pacman -S sdl3 sdl3_ttf hidapi libxkbcommon
    # Debian/Ubuntu: libsdl3-0 libsdl3-ttf0 libhidapi-hidraw0 libxkbcommon0

Python prerequisites:
    pip install pyinstaller pillow pynput hidapi pyyaml
    # No pysdl2  rendering/input use the vendored sdl3w ctypes binding, which
    # loads the SYSTEM libSDL3 / libSDL3_ttf at runtime.

The binary loads libSDL3 / libSDL3_ttf / libhidapi from the system at runtime,
so the host distro must have the matching shared libraries installed.
"""

import glob
import os
import shutil
import subprocess
import sys
import tarfile

from PIL import Image


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PROJECT_DIR)
# tray_linux is the default entry; --no-tray on the resulting binary falls
# back to the same headless behavior adusk_linux.py provides.
ENTRY = "tray_linux.py"
# No .exe suffix  Linux binaries are extensionless. The platform tag lives
# in the release tarball name, not on the binary.
OUTPUT_NAME = "SteamlessInput"
TARBALL_NAME = "SteamlessInput-linux.tar.gz"
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def _check_platform():
    if not sys.platform.startswith("linux"):
        raise SystemExit(
            f"build_linux.py must be run on Linux (got {sys.platform!r}). "
            "Use build.py for the Windows build."
        )


def _check_tk():
    """A self-contained build must bundle Tcl/Tk for the "Keybinds" picker.
    PyInstaller's tkinter hook only collects Tcl/Tk when the build host can
    import tkinter (i.e. the OS 'tk' package is installed); otherwise it
    silently produces a binary whose picker fails at runtime ("needs Tk
    installed"). Fail fast here so we never ship that. Set SKIP_TK_CHECK=1 to
    build without the picker on purpose."""
    if os.environ.get("SKIP_TK_CHECK"):
        print("warning: SKIP_TK_CHECK set  not verifying Tk; the Keybinds "
              "picker may not work in this binary.")
        return
    try:
        import tkinter  # noqa: F401  (probes the host's _tkinter + system tcl/tk)
    except Exception as e:
        raise SystemExit(
            "build_linux.py: Tk is not available on this build host, so "
            "PyInstaller can't bundle Tcl/Tk and the Keybinds picker would fail "
            f"at runtime ({type(e).__name__}: {e}).\n"
            "Install it, then rebuild:\n"
            "  Arch/CachyOS:   sudo pacman -S tk\n"
            "  Debian/Ubuntu:  sudo apt install python3-tk\n"
            "  Fedora/RHEL:    sudo dnf install python3-tkinter\n"
            "Or set SKIP_TK_CHECK=1 to build without the picker on purpose."
        )


def _run_pyinstaller():
    # Use /tmp for intermediate build files  required when PROJECT_DIR is on an
    # NTFS-mounted Windows partition, where PyInstaller hooks (e.g. GdkPixbuf)
    # create files in the workpath before PyInstaller has fully created the dir.
    work_dir = "/tmp/SteamlessInput-build"
    # PyInstaller's --add-data separator on POSIX is ':' (it's ';' on Windows).
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        # --onedir, NOT --onefile. A onefile binary re-extracts its ENTIRE
        # payload to /tmp/_MEIxxxxx on EVERY launch before a line of our code
        # runs. Measured on the Windows twin: 6.6-10.5 s launch-to-tray-icon
        # as onefile vs 1.05-1.22 s for the same build as --onedir, with the
        # run-to-run spread collapsing from +-2 s to +-0.02 s. The release has
        # always been a tarball that extracts to a SteamlessInput/ folder, so
        # this only adds _internal/ to what the user already unpacks, and the
        # portable .desktop (which execs the binary by its own dirname) is
        # unaffected. NOT yet verified on a real Linux box  see _flatten_onedir.
        "--onedir",
        "--workpath", work_dir,
        # Linux has no "windowed" detached-from-tty mode like Windows --windowed.
        # Leave stdout/stderr attached so launch errors surface in the terminal.
        "--name", OUTPUT_NAME,
        "--add-data", f"{DATA_DIR}:data",
        # pynput's X11 backend is the one we need at runtime; the others would
        # just fail to import on Linux.
        "--hidden-import", "pynput.keyboard._xorg",
        "--hidden-import", "pynput.mouse._xorg",
        "--hidden-import", "PIL._tkinter_finder",
        # sdl3w is imported transitively (adusk.screen -> sdl3w); name it
        # explicitly so PyInstaller bundles the package. It loads the system
        # libSDL3 / libSDL3_ttf at runtime (no .so bundled into the binary).
        "--hidden-import", "sdl3w",
        # The "Keybinds" picker (tkinter) is imported lazily from the tray menu;
        # bundle it + tkinter explicitly. PyInstaller's tkinter hook also pulls
        # the host's Tcl/Tk libs INTO the binary, but only when 'tk' is installed
        # on the build host  _check_tk() in main() enforces that so shipped
        # builds are self-contained (no python3-tk needed on the user's machine).
        "--hidden-import", "keybinds_picker",
        # The first-run tutorial overlay, imported lazily from the picker
        # (so a problem in it can never stop the window opening)  name it
        # or the onefile bundle leaves it out and the tour silently never
        # runs.
        "--hidden-import", "tutorial",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        # The tutorial's silent demo track (media_demo  the MPRIS service that
        # puts the media slide's Next/Play-Pause keys on a track of OURS rather
        # than the user's music). Imported inside a try/except so a machine
        # without a session bus degrades instead of failing, which also means
        # PyInstaller's analysis can't see it.
        "--hidden-import", "media_demo",
        "--hidden-import", "gi.repository.Gio",
        "--hidden-import", "gi.repository.GLib",
        # pystray's Linux backend selection happens at import time and uses
        # dynamic imports PyInstaller can't follow. tray_linux.py forces the
        # AppIndicator backend (xorg has no menu support and doesn't render
        # well on KDE Plasma), so pull it in explicitly.
        "--hidden-import", "pystray._appindicator",
        "--hidden-import", "pystray._util.gtk",
        "--hidden-import", "pystray._util.notify_dbus",
        # gi (PyGObject) is the AppIndicator backend's import path. Without
        # this PyInstaller skips the entire gi/_gi modules and the bundle
        # crashes on first AppIndicator call. Doesn't bundle the underlying
        # GTK shared libs  those have to come from the host distro.
        "--hidden-import", "gi",
        "--hidden-import", "gi.repository.Gtk",
        "--hidden-import", "gi.repository.AyatanaAppIndicator3",
        # Windows-only modules: never include them in a Linux build. winhid is
        # only loaded when the Windows-only exclusive-HID feature is requested,
        # which the Linux entry never does  excluding it stops PyInstaller from
        # tripping on its ctypes.WinDLL("kernel32") module-level call.
        "--exclude-module", "steamcontroller.winhid",
        "--exclude-module", "vgamepad",
        "--exclude-module", "winreg",
        "--exclude-module", "pynput.keyboard._win32",
        "--exclude-module", "pynput.mouse._win32",
        # The Windows tray.py module is Windows-only and pulls in winreg etc.
        # Drop it from the build entirely so static analysis doesn't follow it.
        "--exclude-module", "tray",
    ]

    # --- bundle trim --------------------------------------------------------
    # Kept from the --onefile era, where bundle size WAS the cold-start time
    # (a onefile binary re-extracts its ENTIRE payload to /tmp/_MEIxxxxx on
    # every launch). --onedir no longer pays that per launch, but the trim
    # still governs the tarball size and the install footprint, so it stays.
    # None of the following is imported anywhere in the app (verified by grep);
    # they arrive transitively through PIL's PyInstaller hook, which pulls the
    # full optional-codec + array-interop surface:
    #   numpy       ~24 MB -- OpenBLAS (19.5 MB!) plus _multiarray_umath. PIL
    #                         only needs numpy for Image.fromarray /
    #                         __array_interface__, which we never call.
    #   PIL._avif   ~7.5 MB -- AVIF codec (Pillow 12 ships it in-tree as
    #                         PIL._avif, NOT the old pillow_avif package). We
    #                         read only PNG and ICO.
    # Measured on the Windows twin: 40.9 -> 25.8 MB binary, 78 -> 44 MB
    # extracted. Keep this list conservative  anything genuinely reachable at
    # runtime will fail LOUDLY on import, not silently degrade.
    for mod in ("numpy", "scipy", "PIL._avif", "PIL.AvifImagePlugin",
                "pillow_avif", "pandas", "matplotlib",
                "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython",
                "pytest", "setuptools", "pip"):
        cmd += ["--exclude-module", mod]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _clean_dist():
    # Wipe dist/ so stale binaries from previous builds (e.g. an old `.exe`
    # named output that PyInstaller --noconfirm won't touch) don't end up
    # in the release tarball.
    dist_dir = os.path.join(PROJECT_DIR, "dist")
    if os.path.isdir(dist_dir):
        shutil.rmtree(dist_dir, ignore_errors=True)


def _cleanup():
    """Trim the bulky intermediates but KEEP PyInstaller's analysis cache.

    This used to rmtree the whole build dir, which silently defeated the
    reason --clean is absent from the PyInstaller invocation: that directory
    is exactly where the module-graph analysis cache lives, so deleting it
    made every build a full cold analysis again. Keep the cache files
    (Analysis-*.toc, *.pyz, the graph) and drop only the big regenerable
    payloads. (Measured on the Windows twin this is worth ~4%  correct, but
    the analysis scan itself dominates, so don't expect much.)
    """
    build_dir = os.path.join("/tmp/SteamlessInput-build", OUTPUT_NAME)
    if not os.path.isdir(build_dir):
        return
    for name in ("localpycs",):
        p = os.path.join(build_dir, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    for pattern in ("PKG-*.pkg", OUTPUT_NAME):
        for p in glob.glob(os.path.join(build_dir, pattern)):
            try:
                os.remove(p)
            except OSError:
                pass


def _write_dist_icon_and_desktop():
    """Drop a portable PNG + .desktop launcher into dist/ for the release zip.

    Linux ELF binaries can't carry an embedded icon (unlike Windows EXEs).
    The portable replacement is a .desktop file whose Exec uses `%k` (the
    runtime path to the .desktop itself) so the launcher works from any
    extraction location. Icon= uses a bare XDG name  tray_linux.py's
    _install_xdg_icon() drops SteamlessInput.png into
    ~/.local/share/icons/ on first run, after which the icon resolves
    everywhere on the desktop."""
    dist_dir = os.path.join(PROJECT_DIR, "dist")

    # Convert the largest .ico frame to PNG so the bundled icon is a plain
    # image file (also lets users manually copy it to ~/.local/share/icons/
    # before the first launch if they want the .desktop icon to show in
    # their file manager right away).
    ico_path = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")
    png_path = os.path.join(dist_dir, "SteamlessInput.png")
    img = Image.open(ico_path)
    sizes = sorted(img.info.get("sizes", set()), key=lambda s: s[0], reverse=True)
    if sizes:
        img.size = sizes[0]
        img.load()
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img.save(png_path, "PNG")
    print(f"icon:    {png_path}")

    # Portable .desktop. `%k` expands to the .desktop's own path at launch,
    # so `dirname` yields the folder it sits in  same folder as the binary
    # in our release zip. Quoting handles spaces in the extraction path.
    desktop_path = os.path.join(dist_dir, "SteamlessInput.desktop")
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SteamlessInput\n"
        f"Exec=sh -c 'exec \"$(dirname \"%k\")\"/{OUTPUT_NAME}'\n"
        "Icon=SteamlessInput\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )
    with open(desktop_path, "w", encoding="utf-8") as f:
        f.write(contents)
    os.chmod(desktop_path, 0o755)
    print(f"desktop: {desktop_path}")


def _bundle_license():
    """Copy the repo LICENSE + third-party notices into dist/ so they end up
    in the release tarball. The binary embeds the MIT-ported Ramonchi_5
    viewer art, and MIT requires its notice to accompany distributions."""
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        src = os.path.join(REPO_DIR, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(PROJECT_DIR, "dist", name)
        shutil.copy2(src, dst)
        print(f"bundled: {dst}")


def _flatten_onedir():
    """Move the --onedir output up into dist/.

    PyInstaller writes --onedir to `dist/<name>/`; we hoist its contents so the
    binary keeps the EXACT path it had as a onefile (`dist/SteamlessInput`)
    with `_internal/` beside it. _make_tarball() then packs it unchanged (it
    just lists dist/ and adds `_internal` as one more entry), the portable
    .desktop still execs `$(dirname %k)/SteamlessInput`, and the installer's
    payload search is unaffected. Mirrors windows/build.py's _flatten_onedir.

    UNVERIFIED on Linux  written from the Windows result, to be confirmed on
    the next CachyOS build."""
    dist_dir = os.path.join(PROJECT_DIR, "dist")
    src = os.path.join(dist_dir, OUTPUT_NAME)
    if not os.path.isdir(src):
        raise SystemExit(f"--onedir output not found: {src}")
    for name in os.listdir(src):
        dst = os.path.join(dist_dir, name)
        # A previous build's _internal/ goes wholesale, never merged into.
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.remove(dst)
        shutil.move(os.path.join(src, name), dst)
    os.rmdir(src)


def _make_tarball():
    """Package dist/ contents into a .tar.gz suitable for a GitHub Release.

    The tarball extracts to a top-level `SteamlessInput/` folder
    (vs. tar-bombing the user's cwd)  standard Linux convention. Inside
    that folder the user finds the binary, icon, .desktop, and LICENSE
    ready to run from anywhere.

    The tarball is written into dist/ alongside the source files. Snapshot
    the file list *before* opening the tarball for write so it never tries
    to include itself."""
    dist_dir = os.path.join(PROJECT_DIR, "dist")
    sources = sorted(os.listdir(dist_dir))
    tar_path = os.path.join(dist_dir, TARBALL_NAME)
    with tarfile.open(tar_path, "w:gz") as tar:
        for name in sources:
            full = os.path.join(dist_dir, name)
            tar.add(full, arcname=os.path.join("SteamlessInput", name))
    print(f"tarball: {tar_path}")


def _build_installer(bundle):
    """Build dist/SteamlessInput-Setup  the pick-your-components wizard.

    Runs before _make_tarball() so the setup binary ships inside the release
    tarball alongside the app it installs."""
    import build_installer

    build_installer.main(["--bundle"] if bundle else [])


def _parse_args(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        description="Build the SteamlessInput Linux release.")
    p.add_argument(
        "--with-installer", action="store_true",
        help="Also build the install wizard into the tarball. Off by default: "
             "it is a second full PyInstaller pass, and the wizard only needs "
             "rebuilding when installer.py changes.")
    p.add_argument(
        "--bundle-installer", action="store_true",
        help="With --with-installer, embed the app in the setup binary.")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    _check_platform()
    _check_tk()
    _clean_dist()
    _run_pyinstaller()
    _flatten_onedir()
    _cleanup()
    _write_dist_icon_and_desktop()
    _bundle_license()
    if args.with_installer:
        _build_installer(args.bundle_installer)
    else:
        print("skipped install wizard build (--with-installer to include)")
    _make_tarball()
    out = os.path.join(PROJECT_DIR, "dist", OUTPUT_NAME)
    print(f"\nbuilt: {out}")
    print(f"test it:    ./dist/{OUTPUT_NAME}             # tray + chord + hotkey")
    print(f"            ./dist/{OUTPUT_NAME} --no-tray   # headless (terminal-only)")
    print(f"release:    upload dist/{TARBALL_NAME} to your GitHub Releases page")


if __name__ == "__main__":
    main()
