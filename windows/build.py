"""Build script for the portable Steam Controller Keyboard EXE.

Run:
    python build.py
    python build.py --skip-lockscreen

Produces `dist/SteamlessInput-windows.exe` plus the `dist/_internal/` folder
it needs beside it. Uses the prebuilt `data/images/app_icon.ico`
(multi-resolution) directly as the EXE icon and bundles the data/ folder as
PyInstaller datas. The output is a no-console --onedir build, flattened up into
dist/ so the exe keeps the path it had as a onefile (see _flatten_onedir).

By default, also rebuilds the lock-screen keyboard (build_lockscreen.py) and
copies the result over lockscreen-keyboard/LockScreenKeyboard.exe, so the
packaged lock-screen exe always matches the current adusk/ source. Release CI
passes --skip-lockscreen so Windows release assets contain only the tray app.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = "tray.py"
OUTPUT_NAME = "SteamlessInput-windows"
APP_ICON_ICO = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the portable SteamlessInput Windows executable."
    )
    parser.add_argument(
        "--skip-lockscreen",
        action="store_true",
        help="Do not build or copy the optional lock-screen keyboard helper.",
    )
    parser.add_argument(
        "--with-installer",
        action="store_true",
        help="Also build the install wizard (dist/SteamlessInput-Setup.exe). "
             "Off by default: it is a second full PyInstaller pass and the "
             "wizard finds the app exe beside itself, so it only needs "
             "rebuilding when installer.py changes.",
    )
    parser.add_argument(
        "--bundle-installer",
        action="store_true",
        help="With --with-installer, embed the app in the setup exe so it "
             "installs with no sibling files.",
    )
    parser.add_argument(
        "--with-relay",
        action="store_true",
        help="Also build the uiAccess input relay (keeps the controller "
             "working on administrator windows). Off by default because it "
             "costs a second full PyInstaller pass; include it in releases.",
    )
    return parser.parse_args(argv)


def _check_icon():
    if not os.path.isfile(APP_ICON_ICO):
        raise SystemExit(f"app icon not found: {APP_ICON_ICO}")
    print(f"exe icon: {APP_ICON_ICO}")


def _run_pyinstaller():
    # `data;data` tells PyInstaller to drop the data/ folder into the bundle
    # rooted at "data". On Windows the separator in --add-data is ";".
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        # --onedir, NOT --onefile. A onefile exe re-extracts its ENTIRE payload
        # to %TEMP% on EVERY launch before a line of our code runs: measured
        # 4.4-9.0 s of a 6.6-10.5 s launch-to-tray-icon on a ~50 MB payload.
        # The same build as --onedir reaches the tray icon in 1.05-1.22 s, and
        # the run-to-run spread collapses from +-2 s to +-0.02 s. Nothing about
        # the download changes shape for it: the release has always been a zip
        # containing a folder (app exe, setup exe, uia-relay/, docs), so this
        # only adds _internal/ to what the user already unzips.
        "--onedir",
        "--windowed",
        "--name", OUTPUT_NAME,
        "--icon", APP_ICON_ICO,
        "--add-data", f"{DATA_DIR};data",
        # pystray uses platform-specific backends loaded at runtime; pyinstaller
        # doesn't always pick them up unless we name them explicitly.
        "--hidden-import", "pystray._win32",
        "--hidden-import", "pynput.keyboard._win32",
        "--hidden-import", "pynput.mouse._win32",
        "--hidden-import", "PIL._tkinter_finder",
        # PIL.ImageTk renders the anti-aliased settings cog onto the Tk canvas;
        # name it (+ its _imagingtk C ext via the hook) so the onefile bundles it.
        "--hidden-import", "PIL.ImageTk",
        # vgamepad is vendored under windows/vgamepad; import paths are static
        # but the ViGEmClient DLLs are added explicitly below.
        "--hidden-import", "vgamepad",
        "--hidden-import", "vgamepad.win.virtual_gamepad",
        "--hidden-import", "vgamepad.win.vigem_client",
        "--hidden-import", "vgamepad.win.vigem_commons",
        # Our hand-rolled SDL3 binding is imported transitively (adusk.screen
        # -> sdl3w); name it explicitly so PyInstaller always bundles it.
        "--hidden-import", "sdl3w",
        # Nintendo Bluetooth guard  imported defensively (try/except) from
        # adusk.inputsrc, so name it to be sure the onefile bundle has it.
        "--hidden-import", "nintendo_bt",
        # The "Keybinds" picker (tkinter) is imported lazily from the tray menu
        # callback; name it + tkinter explicitly so the onefile bundle includes
        # them even though the import isn't at module scope.
        "--hidden-import", "keybinds_picker",
        # The first-run tutorial overlay, imported lazily from the picker (so
        # a problem in it can never stop the window opening)  name it or the
        # onefile bundle leaves it out and the tour silently never runs.
        "--hidden-import", "tutorial",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        # The tutorial's silent demo track (media_demo). Both it and every
        # WinRT namespace it uses are imported INSIDE functions  deliberately,
        # so a machine without the projection packages degrades instead of
        # failing to import  which also means PyInstaller's analysis can't see
        # any of them. Named here or the media slide loses its now-playing card
        # in the frozen build only, which is the worst way to find out.
        "--hidden-import", "media_demo",
        "--hidden-import", "winrt.runtime",
        "--hidden-import", "winrt.system",
        "--hidden-import", "winrt.windows.foundation",
        "--hidden-import", "winrt.windows.media",
        "--hidden-import", "winrt.windows.media.core",
        "--hidden-import", "winrt.windows.media.playback",
        "--hidden-import", "winrt.windows.storage",
        "--hidden-import", "winrt.windows.storage.streams",
    ]

    # --- bundle trim ---------------------------------------------------------
    # Kept from the --onefile era, where bundle size WAS the cold-start time
    # (78 MB extracted, 2.9-7.9 s to the single-instance guard, every launch).
    # --onedir no longer pays that per-launch, but the trim still governs the
    # download size, the install footprint, and how much Defender has to scan
    # once  so it stays.
    #
    # None of the following is imported anywhere in the app (verified by grep);
    # they arrive transitively through PIL's PyInstaller hook, which pulls the
    # full optional-codec + array-interop surface:
    #   numpy       ~24 MB -- libscipy_openblas64_*.dll (19.5 MB!) plus
    #                         _multiarray_umath (3.7 MB). PIL only needs numpy
    #                         for Image.fromarray/__array_interface__, which we
    #                         never call: every raster goes through PIL's own
    #                         Image/ImageDraw/ImageFont objects.
    #   PIL._avif   ~7.5 MB -- AVIF codec (Pillow 12 ships it in-tree as
    #                         PIL._avif, NOT the old external pillow_avif
    #                         package). We read only PNG and ICO. Excluding
    #                         AvifImagePlugin too stops PIL's plugin scan from
    #                         importing the extension we just dropped.
    # Keep this list conservative: anything genuinely reachable at runtime will
    # fail LOUDLY on import, not silently degrade.
    for mod in ("numpy", "scipy", "PIL._avif", "PIL.AvifImagePlugin",
                "pillow_avif", "pandas", "matplotlib",
                "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython",
                "pytest", "setuptools", "pip"):
        cmd += ["--exclude-module", mod]

    # sdl3w loads the vendored SDL3 family (SDL3.dll + SDL3_ttf.dll) at import
    # time from the first candidate dir that holds SDL3.dll, and loads
    # SDL3_ttf.dll from beside it (see sdl3w/_loader.py)  so the two must ship
    # into the SAME folder.
    #
    # That folder is the bundle ROOT, not sdl3w/dll: SDL3_ttf.dll imports
    # SDL3.dll, so PyInstaller's binary-dependency analysis collects SDL3.dll
    # to the root regardless of where we put our own copy. Sending ours to a
    # subdir shipped SDL3.dll TWICE  2.8 MB of a 52 MB payload, re-extracted
    # to %TEMP% on every single launch, for nothing. Same destination, one copy.
    sdl_dll_dir = os.path.join(PROJECT_DIR, "sdl3w", "dll")
    sdl_dlls = glob.glob(os.path.join(sdl_dll_dir, "*.dll"))
    if not sdl_dlls:
        raise SystemExit(f"no SDL3 DLLs found in {sdl_dll_dir}")
    for dll in sdl_dlls:
        cmd += ["--add-binary", f"{dll};."]

    vigem_client_dir = os.path.join(PROJECT_DIR, "vgamepad", "win", "vigem", "client")
    for arch in ("x64", "x86"):
        dll = os.path.join(vigem_client_dir, arch, "ViGEmClient.dll")
        if not os.path.isfile(dll):
            raise SystemExit(f"ViGEmClient.dll not found: {dll}")
        cmd += ["--add-binary", f"{dll};vgamepad/win/vigem/client/{arch}"]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _flatten_onedir():
    """Move the --onedir output up into dist/.

    PyInstaller writes --onedir to `dist/<name>/`; we hoist its contents so the
    app exe keeps the EXACT path it had as a onefile
    (`dist/SteamlessInput-windows.exe`) with `_internal/` beside it. Everything
    downstream  the installer's payload search, build_installer's --bundle, the
    CI packaging step  then only has to learn about `_internal/`, rather than a
    whole new layout. The other exes in dist/ (setup, lock-screen, relay) stay
    onefile, so nothing else there owns an `_internal/` to collide with."""
    dist = os.path.join(PROJECT_DIR, "dist")
    src = os.path.join(dist, OUTPUT_NAME)
    if not os.path.isdir(src):
        raise SystemExit(f"--onedir output not found: {src}")
    for name in os.listdir(src):
        dst = os.path.join(dist, name)
        # A previous build's _internal/ must go wholesale, not be merged into 
        # a stale file left behind by an older version is exactly the kind of
        # thing that only breaks on someone else's machine.
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.remove(dst)
        shutil.move(os.path.join(src, name), dst)
    os.rmdir(src)


def _cleanup():
    """Trim the bulky intermediates but KEEP PyInstaller's analysis cache.

    This used to rmtree build/ wholesale, which silently defeated the reason
    --clean was removed from the PyInstaller invocation: build/ is exactly
    where the module-graph analysis cache lives, so deleting it made every
    build a full cold analysis again. Keep the cache files (Analysis-*.toc,
    *.pyz, the graph) and drop only the big regenerable payloads 
    the collected binaries and the PKG/EXE staging blobs.
    """
    build_dir = os.path.join(PROJECT_DIR, "build", OUTPUT_NAME)
    if not os.path.isdir(build_dir):
        return
    # Sizeable, always-regenerated artifacts. The .toc/.pyz analysis products
    # next to them are what make an incremental rebuild fast  leave those.
    for name in ("localpycs",):
        p = os.path.join(build_dir, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    for pattern in ("PKG-*.pkg", "*.exe"):
        for p in glob.glob(os.path.join(build_dir, pattern)):
            try:
                os.remove(p)
            except OSError:
                pass


def _build_lockscreen():
    import build_lockscreen

    build_lockscreen.main()
    src = os.path.join(PROJECT_DIR, "dist", "LockScreenKeyboard.exe")
    dst = os.path.join(PROJECT_DIR, "lockscreen-keyboard", "LockScreenKeyboard.exe")
    shutil.copy2(src, dst)
    print(f"updated: {dst}")


def _build_relay():
    """Build the uiAccess input relay into dist/uia-relay/.

    Shipped UNSIGNED and un-installed: the privilege only exists once the exe
    is signed and living in a secure location, which needs administrator
    rights and therefore the user's consent. Options > General offers that as
    a one-click step (see install_uia_relay.ps1); until then the app falls
    back to lizard mode, so a build without this is fully functional."""
    import build_uia_relay

    build_uia_relay.main()


def _build_installer(bundle):
    """Build dist/SteamlessInput-Setup.exe  the pick-your-components wizard.

    Runs last so a --bundle pass can embed the app exe this same run produced."""
    import build_installer

    build_installer.main(["--bundle"] if bundle else [])


def main(argv=None):
    args = _parse_args(argv)

    _check_icon()
    _run_pyinstaller()
    _flatten_onedir()
    _cleanup()
    out = os.path.join(PROJECT_DIR, "dist", f"{OUTPUT_NAME}.exe")
    print(f"\nbuilt: {out}")

    if args.skip_lockscreen:
        print("skipped lock-screen keyboard build")
    else:
        _build_lockscreen()

    if args.with_relay:
        _build_relay()
    else:
        print("skipped uiAccess input relay build (--with-relay to include)")

    if args.with_installer:
        _build_installer(args.bundle_installer)
    else:
        print("skipped install wizard build (--with-installer to include)")


if __name__ == "__main__":
    main()
