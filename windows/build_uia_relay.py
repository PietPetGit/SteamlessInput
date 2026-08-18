"""Build the uiAccess input relay (uia_relay.py).

Run:
    python build_uia_relay.py

Produces a ONE-DIR build at dist/uia-relay/ whose exe carries a manifest with
uiAccess="true"  the privilege that lets it inject input into administrator
windows (Task Manager, installers) that ordinary processes are locked out of.

Windows grants that privilege only when BOTH hold:

  1. the exe is Authenticode-signed by a certificate in the machine's trusted
     store, and
  2. it runs from a secure location (%ProgramFiles%, %SystemRoot%).

so this build alone is not enough  see install_uia_relay.ps1. Unsigned or run
from the dev tree it still launches, silently WITHOUT the privilege; the client
detects that (uia_client._has_uiaccess reads the token) and the app falls back
to lizard mode rather than routing input into a helper that can't deliver it.

One-dir, not one-file, for the same reason the lock-screen keyboard is: a
one-file build extracts to %TEMP%, which is user-writable and therefore not a
secure location, which voids uiAccess.
"""

import os
import shutil
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = "uia_relay.py"
OUTPUT_NAME = "SteamlessInputRelay"
APP_ICON_ICO = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")


def _run_pyinstaller():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--uac-uiaccess",
        "--name", OUTPUT_NAME,
        # Deliberately no data files and no hidden imports: the relay is pure
        # ctypes + stdlib. Keeping the privileged process's surface this small
        # is the point of splitting it out.
    ]
    if os.path.isfile(APP_ICON_ICO):
        cmd += ["--icon", APP_ICON_ICO]
    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def main():
    _run_pyinstaller()
    build_dir = os.path.join(PROJECT_DIR, "build", OUTPUT_NAME)
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
    # uia_client looks for <dir>/uia-relay/SteamlessInputRelay.exe, so the
    # one-dir output is renamed to the folder name the client expects.
    out_dir = os.path.join(PROJECT_DIR, "dist", OUTPUT_NAME)
    want_dir = os.path.join(PROJECT_DIR, "dist", "uia-relay")
    if os.path.isdir(out_dir):
        if os.path.isdir(want_dir):
            shutil.rmtree(want_dir, ignore_errors=True)
        os.rename(out_dir, want_dir)
    exe = os.path.join(want_dir, OUTPUT_NAME + ".exe")
    print("\nbuilt: %s" % exe)
    print("NOTE: unsigned and outside a secure location this has NO uiAccess "
          "privilege.\n      Run install_uia_relay.ps1 (as admin) to sign it "
          "and install it under\n      %ProgramFiles%; until then the app "
          "falls back to lizard mode.")


if __name__ == "__main__":
    main()
