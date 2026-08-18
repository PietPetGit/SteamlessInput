"""Exercise the install wizard's winget path without a real winget.

Run:
    python winget_test.py

The wizard installs ViGEmBus and HidHide through winget, with a browser
fallback. That whole path is invisible on a machine with no winget (and fails
SOFTLY when it does run  a wrong package id just makes winget say "no package
found" and the wizard quietly opens a download page instead), so the branches
get stubbed out and asserted here rather than discovered by a user.

Guards in particular the two package identifiers, which are NOT guessable:
ViGEmBus is published under the "ViGEm" publisher, not "Nefarius", even though
both projects are Nefarius'. Verified against the winget-pkgs manifest paths:
    manifests/v/ViGEm/ViGEmBus/       -> ViGEm.ViGEmBus
    manifests/n/Nefarius/HidHide/     -> Nefarius.HidHide
    manifests/n/Nefarius/ViGEmBus/    -> 404

The Linux counterpart is linux/packages_test.py (distro package manager).
"""

import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer as I                                          # noqa: E402


FAILED = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


class Harness(object):
    """Swap out _run / _winget / webbrowser / detection; record what happened."""

    def __init__(self, winget=r"C:\fake\winget.exe", results=None,
                 present=False):
        self.winget, self.results, self.present = winget, results or [], present
        self.calls, self.opened, self.log = [], [], []

    def __enter__(self):
        self._saved = (I._winget, I._run, I._detect_vigem, I._detect_hidhide)
        I._winget = lambda: self.winget
        I._detect_vigem = lambda: self.present
        I._detect_hidhide = lambda: self.present

        def fake_run(cmd, timeout=None, shell=False):
            self.calls.append(list(cmd))
            return self.results.pop(0) if self.results else (0, "")
        I._run = fake_run

        self._wb = webbrowser.open
        webbrowser.open = lambda url: self.opened.append(url)
        return self

    def __exit__(self, *_a):
        I._winget, I._run, I._detect_vigem, I._detect_hidhide = self._saved
        webbrowser.open = self._wb

    def ctx(self):
        return I.Ctx(r"C:\tmp", lambda lvl, txt: self.log.append((lvl, txt)))

    def said(self, needle):
        return any(needle.lower() in t.lower() for _l, t in self.log)


def main():
    print("\n0. package ids still match the winget-pkgs manifests")
    check("ViGEmBus id is ViGEm.ViGEmBus (NOT Nefarius.*)",
          I.VIGEM_WINGET_ID == "ViGEm.ViGEmBus", I.VIGEM_WINGET_ID)
    check("HidHide id is Nefarius.HidHide",
          I.HIDHIDE_WINGET_ID == "Nefarius.HidHide", I.HIDHIDE_WINGET_ID)

    print("\n1. silent install succeeds -> one call, no retry, no browser")
    with Harness(results=[(0, "Successfully installed")]) as h:
        ok = I._step_vigem(h.ctx())
        check("returns True", ok is True)
        check("exactly one winget call", len(h.calls) == 1, str(h.calls))
        check("used --silent", "--silent" in h.calls[0])
        check("passed the ViGEmBus id", I.VIGEM_WINGET_ID in h.calls[0])
        check("--exact passed", "--exact" in h.calls[0])
        check("both agreement flags",
              "--accept-package-agreements" in h.calls[0]
              and "--accept-source-agreements" in h.calls[0])
        check("no browser opened", h.opened == [])
        check("reported installed", h.said("ViGEmBus installed"))

    print("\n2. silent fails -> retries interactively, succeeds")
    with Harness(results=[(1, "installer failed"), (0, "ok")]) as h:
        ok = I._step_vigem(h.ctx())
        check("returns True", ok is True)
        check("two calls", len(h.calls) == 2, str(len(h.calls)))
        check("retry drops --silent", "--silent" not in h.calls[1])
        check("retry keeps the id", I.VIGEM_WINGET_ID in h.calls[1])
        check("told the user it retried", h.said("retrying"))
        check("no browser opened", h.opened == [])

    print("\n3. both attempts fail -> warns and opens the download page")
    with Harness(results=[(1, "x"), (1, "boom")]) as h:
        ok = I._step_vigem(h.ctx())
        check("returns False", ok is False)
        check("two calls", len(h.calls) == 2)
        check("opened the ViGEmBus page", h.opened == [I.VIGEM_URL],
              str(h.opened))
        check("warned with the exit code", h.said("exit 1"))

    print("\n4. winget absent -> no calls at all, straight to the browser")
    with Harness(winget=None) as h:
        ok = I._step_vigem(h.ctx())
        check("returns False", ok is False)
        check("no subprocess calls", h.calls == [], str(h.calls))
        check("said winget isn't available", h.said("winget isn't available"))
        check("opened the page", h.opened == [I.VIGEM_URL])

    print("\n5. already installed -> short-circuits, winget never runs")
    with Harness(present=True) as h:
        ok = I._step_vigem(h.ctx())
        check("returns True", ok is True)
        check("no winget call", h.calls == [])
        check("said already installed", h.said("already installed"))

    print("\n6. HidHide uses its own id + flags the reboot")
    with Harness(results=[(0, "ok")]) as h:
        ok = I._step_hidhide(h.ctx())
        check("returns True", ok is True)
        check("passed the HidHide id", I.HIDHIDE_WINGET_ID in h.calls[0],
              str(h.calls[0]))
        check("mentions REBOOT", h.said("reboot"))
        # Deliberately NOT the old "now go and click through HidHide
        # Configuration Client" instruction: the hidhide_setup step does all
        # three of those clicks, so telling people to do them by hand again
        # would be wrong, not merely redundant.
        check("no manual Configuration Client walkthrough",
              not h.said("Configuration Client"))

    print("\n7. HidHide failure opens the HidHide page, not ViGEm's")
    with Harness(results=[(1, "x"), (1, "y")]) as h:
        I._step_hidhide(h.ctx())
        check("opened HidHide page", h.opened == [I.HIDHIDE_URL],
              str(h.opened))

    print("\n8. _winget() discovery")
    saved_which = I.shutil.which
    try:
        I.shutil.which = lambda n: (r"C:\real\winget.exe" if n == "winget"
                                    else None)
        check("finds winget on PATH", I._winget() == r"C:\real\winget.exe")

        I.shutil.which = lambda _n: None
        alias_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 "Microsoft", "WindowsApps")
        alias = os.path.join(alias_dir, "winget.exe")
        if os.path.isfile(alias):
            check("real WindowsApps alias is found", I._winget() == alias)
        else:
            check("absent everywhere -> None", I._winget() is None,
                  str(I._winget()))
            os.makedirs(alias_dir, exist_ok=True)
            open(alias, "w").close()
            try:
                check("falls back to the WindowsApps alias",
                      I._winget() == alias, str(I._winget()))
            finally:
                os.remove(alias)
    finally:
        I.shutil.which = saved_which

    hidhide_setup_tests()

    print("\n" + ("ALL PASS" if not FAILED else f"FAILURES: {FAILED}"))
    return 1 if FAILED else 0


class CloakHarness(object):
    """Stub out HidHideCLI, the device list and the machine-wide state.

    Everything here is what the wizard would otherwise learn from a driver, a
    PnP database and HKLM  none of which a test machine can be made to hold on
    demand, and one of which (the control device) wedges when poked wrongly.
    That last part is exactly what these tests are for."""

    def __init__(self, state=None, devices=(("HID\\VID_057E&PID_2009\\X",
                                             "Pro Controller"),),
                 apps=(r"C:\tmp\SteamlessInput.exe",), after=None):
        self.state = state
        self.after = after if after is not None else state
        self.devices = list(devices)
        self.apps = list(apps)
        self.calls, self.log, self.saved = [], [], {}
        self.tasks = set()

    def __enter__(self):
        self._saved = (I._hidhide_cli, I._run, I._hidhide_read_state,
                       I._nintendo_devices, I._hidhide_apps, I._hh_state_save,
                       I._hh_state_load, I._hidhide_task_create,
                       I._hidhide_task_delete, I._hidhide_task_exists,
                       I._frozen, I._self_path)
        # Pose as an installed setup exe: the deferred run schedules THAT, and
        # a source checkout has no such file to point a logon task at.
        I._frozen = lambda: True
        I._self_path = lambda: r"C:\fake\SteamlessInput-Setup.exe"
        I._hidhide_cli = lambda: r"C:\fake\HidHideCLI.exe"
        I._nintendo_devices = lambda: list(self.devices)
        I._hidhide_apps = lambda _ctx: list(self.apps)

        reads = {"n": 0}

        def fake_state(refresh=False):
            reads["n"] += 1
            return self.state if reads["n"] == 1 else self.after
        I._hidhide_read_state = fake_state

        def fake_run(cmd, timeout=None, shell=False):
            self.calls.append(list(cmd))
            return (0, "")
        I._run = fake_run

        def save(**kw):
            self.saved.update(kw)
            return True
        I._hh_state_save = save
        I._hh_state_load = lambda: {"devices": [], "apps": [],
                                    "cloaked_by_us": False, "tries": 0}
        I._hidhide_task_create = lambda exe, d: bool(self.tasks.add("t")) or True
        I._hidhide_task_delete = lambda: self.tasks.discard("t")
        I._hidhide_task_exists = lambda: bool(self.tasks)
        return self

    def __exit__(self, *_a):
        (I._hidhide_cli, I._run, I._hidhide_read_state, I._nintendo_devices,
         I._hidhide_apps, I._hh_state_save, I._hh_state_load,
         I._hidhide_task_create, I._hidhide_task_delete,
         I._hidhide_task_exists, I._frozen, I._self_path) = self._saved

    def ctx(self):
        return I.Ctx(r"C:\tmp", lambda lvl, txt: self.log.append((lvl, txt)))

    def said(self, needle):
        return any(needle.lower() in t.lower() for _l, t in self.log)

    def writes(self):
        """CLI invocations that CHANGE something."""
        verbs = ("--dev-hide", "--dev-unhide", "--cloak-on", "--cloak-off",
                 "--app-reg", "--app-unreg", "--inv-on", "--inv-off")
        return [c for c in self.calls if any(v in c for v in verbs)]


OFF = {"cloak": False, "inverse": False, "apps": [], "devices": []}


def hidhide_setup_tests():
    print("\n9. hidhide_setup does the whole thing in ONE CLI invocation")
    done = {"cloak": True, "inverse": False,
            "apps": [r"C:\tmp\SteamlessInput.exe"],
            "devices": ["HID\\VID_057E&PID_2009\\X"]}
    with CloakHarness(state=dict(OFF), after=done) as h:
        ok = I._step_hidhide_setup(h.ctx())
        check("returns True", ok is True)
        # The whole reason this lives in one command line: HidHide's control
        # device is single-client, and a call per verb wedged the driver hard
        # enough to need a reboot.
        check("exactly one write process", len(h.writes()) == 1,
              str(h.writes()))
        cmd = h.writes()[0]
        check("allow-listed the app", "--app-reg" in cmd, str(cmd))
        check("hid the controller", "--dev-hide" in cmd, str(cmd))
        check("switched hiding on", "--cloak-on" in cmd, str(cmd))
        check("recorded the device for the uninstaller",
              h.saved.get("devices") == ["HID\\VID_057E&PID_2009\\X"],
              str(h.saved))
        check("recorded that WE flipped cloaking",
              h.saved.get("cloaked_by_us") is True, str(h.saved))

    print("\n10. driver not up yet -> defer to a logon task, write nothing")
    with CloakHarness(state=None) as h:
        ok = I._step_hidhide_setup(h.ctx())
        check("returns True (it is scheduled, not failed)", ok is True)
        check("no CLI writes", h.writes() == [], str(h.writes()))
        check("scheduled the logon task", h.tasks == {"t"})
        check("says it finishes at the next sign-in", h.said("sign in"))

    print("\n11. inverse application list -> never registers the app")
    inv = dict(OFF, inverse=True, apps=[r"C:\tmp\SteamlessInput.exe"])
    with CloakHarness(state=inv, after=dict(inv, cloak=True)) as h:
        I._step_hidhide_setup(h.ctx())
        cmd = h.writes()[0]
        check("did NOT app-reg into a block list", "--app-reg" not in cmd,
              str(cmd))
        check("removed itself from it instead", "--app-unreg" in cmd, str(cmd))

    print("\n12. no app on disk -> refuses rather than blinding itself")
    with CloakHarness(state=dict(OFF), apps=()) as h:
        ok = I._step_hidhide_setup(h.ctx())
        check("returns False", ok is False)
        check("hid nothing", h.writes() == [], str(h.writes()))
        check("says which folder it looked in", h.said(r"C:\tmp"))

    print("\n13. undo puts back exactly what was changed")
    with CloakHarness(state=done) as h:
        I._hh_state_load = lambda: {"devices": ["HID\\VID_057E&PID_2009\\X"],
                                    "apps": [r"C:\tmp\SteamlessInput.exe"],
                                    "cloaked_by_us": True, "tries": 0}
        ok = I._undo_hidhide_setup(h.ctx())
        check("returns True", ok is True)
        check("exactly one write process", len(h.writes()) == 1,
              str(h.writes()))
        cmd = h.writes()[0]
        check("un-hid the controller", "--dev-unhide" in cmd, str(cmd))
        check("dropped the allow-list entry", "--app-unreg" in cmd, str(cmd))
        check("switched hiding back off", "--cloak-off" in cmd, str(cmd))
        check("removed the logon task", not h.tasks)

    print("\n15. a pad this PC has NEVER seen -> allow-list + retry at logon")
    # Windows has no device instance path for a controller that was never
    # attached, so there is nothing to blacklist yet. Everything that CAN be
    # done now is done, and the logon task catches the pad afterwards.
    with CloakHarness(state=dict(OFF), devices=(),
                      after={"cloak": True, "inverse": False,
                             "apps": [r"C:\tmp\SteamlessInput.exe"],
                             "devices": []}) as h:
        ok = I._step_hidhide_setup(h.ctx())
        check("returns True", ok is True)
        cmd = h.writes()[0]
        check("still allow-listed the app", "--app-reg" in cmd, str(cmd))
        check("still switched hiding on", "--cloak-on" in cmd, str(cmd))
        check("hid nothing (there is nothing to hide)",
              "--dev-hide" not in cmd, str(cmd))
        check("says so plainly", h.said("has ever been connected"))
        check("scheduled the retry", h.tasks == {"t"})

    print("\n16. an unplugged-but-known pad is hidden anyway")
    # The case that matters most in practice: a wireless pad is switched off
    # while somebody runs an installer. --dev-all still lists it (absent), and
    # --dev-hide accepts an absent path  verified against HidHideCLI 1.5.230.
    with CloakHarness(state=dict(OFF),
                      devices=(("HID\\VID_057E&PID_2009\\X",
                                "Pro Controller (not connected right now)"),),
                      after={"cloak": True, "inverse": False,
                             "apps": [r"C:\tmp\SteamlessInput.exe"],
                             "devices": ["HID\\VID_057E&PID_2009\\X"]}) as h:
        ok = I._step_hidhide_setup(h.ctx())
        check("returns True", ok is True)
        check("hid it while it was off", "--dev-hide" in h.writes()[0])
        check("no retry task left behind", not h.tasks)

    print("\n14. undo leaves ANOTHER tool's cloaking alone")
    shared = {"cloak": True, "inverse": False,
              "apps": [r"C:\tmp\SteamlessInput.exe"],
              "devices": ["HID\\VID_057E&PID_2009\\X", "HID\\VID_054C&X"]}
    with CloakHarness(state=shared) as h:
        I._hh_state_load = lambda: {"devices": ["HID\\VID_057E&PID_2009\\X"],
                                    "apps": [], "cloaked_by_us": True,
                                    "tries": 0}
        I._undo_hidhide_setup(h.ctx())
        cmd = h.writes()[0]
        check("left cloaking on for the other device",
              "--cloak-off" not in cmd, str(cmd))
        check("still un-hid ours", "--dev-unhide" in cmd, str(cmd))


if __name__ == "__main__":
    sys.exit(main())
