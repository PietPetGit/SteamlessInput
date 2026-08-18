"""Exercise the install wizard's distro-package path without touching the system.

Run:
    python3 packages_test.py

Mirror of windows/winget_test.py. Where Windows shells out to winget for
ViGEmBus/HidHide, Linux shells out to the distro package manager for the GTK3 /
AppIndicator tray stack  same shape of risk: it only runs on someone else's
machine, it needs root, and a wrong package name fails softly (the app still
starts, just with no tray icon), so nobody would notice it had rotted.

Asserts the package lists and the install argv per distro, apt's extra
`apt-get update`, the "no supported package manager" path, and that the root
phase resolves USER paths rather than root's under pkexec.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer as I                                          # noqa: E402


FAILED = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


class Harness(object):
    """Pin which package manager exists; record every command run."""

    def __init__(self, tool=None, results=None):
        self.tool, self.results = tool, results or []
        self.calls, self.log = [], []

    def __enter__(self):
        self._saved = (I.shutil.which, I._run)
        I.shutil.which = lambda n: (f"/usr/bin/{n}" if n == self.tool else None)

        def fake_run(cmd, timeout=None):
            self.calls.append(list(cmd))
            return self.results.pop(0) if self.results else (0, "")
        I._run = fake_run
        return self

    def __exit__(self, *_a):
        I.shutil.which, I._run = self._saved

    def ctx(self):
        return I.Ctx("/tmp/si", lambda lvl, txt: self.log.append((lvl, txt)))

    def said(self, needle):
        return any(needle.lower() in t.lower() for _l, t in self.log)


def main():
    print("\n1. every supported distro maps to a tool, packages and argv")
    for tool, first in (("pacman", "pacman"), ("apt-get", "apt-get"),
                        ("dnf", "dnf"), ("zypper", "zypper")):
        with Harness(tool=tool) as h:
            key, pkgs, argv = I._package_manager()
            expect_key = "apt" if tool == "apt-get" else tool
            check(f"{tool}: key is {expect_key}", key == expect_key, str(key))
            check(f"{tool}: has packages", bool(pkgs))
            check(f"{tool}: argv starts with {first}",
                  argv and argv[0] == first, str(argv))
            check(f"{tool}: installs GTK3", any("gtk3" in p or "gtk-3" in p
                                                for p in pkgs), str(pkgs))
            check(f"{tool}: installs an appindicator",
                  any("appindicator" in p for p in pkgs), str(pkgs))
            check(f"{tool}: installs xwayland",
                  any("wayland" in p.lower() for p in pkgs), str(pkgs))
            check(f"{tool}: non-interactive flag present",
                  any(f in argv for f in ("--noconfirm", "-y",
                                          "--non-interactive")), str(argv))

    print("\n2. install succeeds -> reports ok")
    with Harness(tool="pacman", results=[(0, "done")]) as h:
        ok = I._step_deps(h.ctx())
        check("returns True", ok is True)
        check("one command", len(h.calls) == 1, str(h.calls))
        check("named the tool in the log", h.said("pacman"))
        check("reported installed", h.said("Tray libraries installed"))

    print("\n3. apt refreshes its index first")
    with Harness(tool="apt-get", results=[(0, ""), (0, "")]) as h:
        I._step_deps(h.ctx())
        check("two commands", len(h.calls) == 2, str(len(h.calls)))
        check("first is apt-get update",
              h.calls[0][:2] == ["apt-get", "update"], str(h.calls[0]))
        check("second is the install", "install" in h.calls[1])

    print("\n4. failure is soft  the app still runs without a tray")
    with Harness(tool="dnf", results=[(1, "could not resolve")]) as h:
        ok = I._step_deps(h.ctx())
        check("returns False", ok is False)
        check("explains the app still works", h.said("app still runs"))

    print("\n5. no supported package manager -> refuses cleanly")
    with Harness(tool=None) as h:
        ok = I._step_deps(h.ctx())
        check("returns False", ok is False)
        check("no commands run", h.calls == [], str(h.calls))
        check("names the managers it looked for", h.said("pacman"))
        avail, why = I._need_deps()
        check("component reports unavailable", avail is False)
        check("reason mentions the README", "README" in why, why)

    print("\n6. under pkexec, user paths resolve to the USER, not root")
    saved = (I._is_root, os.environ.get("PKEXEC_UID"),
             os.environ.get("XDG_DATA_HOME"))
    try:
        import pwd
        me = pwd.getpwuid(os.getuid())
        I._is_root = lambda: True
        os.environ["PKEXEC_UID"] = str(me.pw_uid)
        os.environ["XDG_DATA_HOME"] = "/root/.local/share"   # must be IGNORED
        name, uid, _gid, home = I._real_user()
        check("real user is recovered from PKEXEC_UID", uid == me.pw_uid,
              f"{name}:{uid}")
        check("home is the user's, not root's", home == me.pw_dir, home)
        check("icon path is under the user's home",
              I._icon_path().startswith(me.pw_dir), I._icon_path())
        check("root's XDG_DATA_HOME is ignored",
              not I._icon_path().startswith("/root/"), I._icon_path())
    finally:
        I._is_root = saved[0]
        for key, val in (("PKEXEC_UID", saved[1]), ("XDG_DATA_HOME", saved[2])):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    print("\n" + ("ALL PASS" if not FAILED else f"FAILURES: {FAILED}"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
