"""Nintendo Bluetooth guard  mitigations for the Switch Pro / Joy-Con
firmware bug that drops the controller after ~20 minutes of Bluetooth use.

WHAT THE BUG ACTUALLY IS
------------------------
Nintendo's controllers ship two different Bluetooth behaviours and pick between
them by sniffing the HOST's Bluetooth name. A host advertising itself as
"Nintendo" / "Nintendo Switch" / "NintendoRobson" gets the good path: an active
connection with a proper slot allocation. Every other host  every PC, on
Windows *and* Linux  is put into Bluetooth SNIFF MODE, where the controller
only listens on a reduced duty cycle and, critically, sends no periodic
keepalives of its own: it expects the host to send traffic often enough to hold
the link up.

Sniff mode leaves very little bandwidth, and the controller then saturates it
itself. Rumble spins the motor, the motor shakes the controller, the IMU picks
that shaking up as real motion, and the controller answers by flooding the host
with motion reports  while the host is still sending rumble packets the other
way. The queues overrun ("compensating for N dropped IMU reports"), reports are
missed ("timeout waiting for input report") and the link drops. Reconnect
usually succeeds, but a game that saw its gamepad vanish often never recovers.

Background: https://blog.fyralabs.com/your-joy-cons-are-vibrating-themselves-to-death/

WHAT WE CAN AND CANNOT DO
-------------------------
The only complete fixes are outside an application's reach: connect over USB-C,
or rename the host's Bluetooth adapter to "Nintendo" (on Linux,
`bluetoothctl system-alias Nintendo`; on Windows the adapter name follows the
computer name, so it means renaming the PC). Both are the user's call, so we
surface them rather than doing them.

What this module does is stop *us* from being the thing that saturates the
link, and make the drop survivable when it happens anyway:

  * `RumbleGovernor` rate-limits and coalesces the rumble output reports we
    send to a Nintendo pad on Bluetooth, and clamps a long sustained rumble.
    Stops are never delayed or dropped  a stop packet ends the motor→IMU
    feedback loop, so it is the one packet always worth sending.
  * The limits tighten while the pad is streaming IMU data (Gyro To Mouse),
    because that is the exact combination  rumble plus motion reports  the
    link cannot carry.
  * `keepalive_due` paces a tiny no-op output report during idle, which is the
    host traffic sniff mode is waiting for.

The tray pairs this with a reconnect grace period (see `_park_sdl_gamepad`):
the virtual XInput device is held open across the dropout and handed back to
the same physical controller, so the game never sees its pad disappear.

Pure logic, no SDL/Windows imports  identical in the windows/ and linux/
trees, and unit-testable on its own.
"""

# Which controller kinds this guard applies to: everything Nintendo made. The
# catalog owns that list (pads.NINTENDO_KINDS)  the fallback here is only for
# a stripped-down bundle without pads.py, and keeping it in sync matters,
# because a kind missing from it silently loses the guard. Joy-Cons are if
# anything the WORST affected: the blog post the module docstring cites is
# about Joy-Cons specifically.
_FALLBACK_NINTENDO_KINDS = (
    "switch", "joycon_pair", "joycon_l", "joycon_r",
    "nso_snes", "nso_nes", "nso_n64", "nso_genesis", "gamecube",
    "switch2", "joycon2_pair", "joycon2_l", "joycon2_r",
)

try:
    from pads import NINTENDO_KINDS
except Exception:
    NINTENDO_KINDS = _FALLBACK_NINTENDO_KINDS


def is_nintendo(kind):
    return (kind or "") in NINTENDO_KINDS


class RumbleGovernor:
    """Paces the rumble output reports sent to ONE Nintendo pad on Bluetooth.

    Not a queue  a gate. A rumble that arrives too soon after the last one is
    DROPPED, never buffered: the dropped packets are UI ticks and intermediate
    force-feedback steps, and a late tick is worse than no tick. The next
    request that arrives after the interval carries the current state anyway.

    All times are monotonic seconds supplied by the caller."""

    # Minimum spacing between motor-on packets. ~22/s is well above what a
    # rumble feels like (the motor's own attack is slower than that) and well
    # below the burst rate an OSK key-repeat or a per-frame game FFB update
    # would otherwise produce.
    MIN_INTERVAL = 0.045
    # The same, while the pad is also streaming IMU data. Rumble + motion
    # reports together is the combination that kills the link, so once gyro is
    # live we drop to ~8 packets/s and shave the amplitude.
    MIN_INTERVAL_IMU = 0.120
    IMU_AMPLITUDE = 0.60

    # A rumble held continuously for longer than this is attenuated: a long
    # motor-on window is what feeds the IMU flood, and past a few seconds the
    # user has stopped noticing the amplitude anyway.
    SUSTAIN_LIMIT = 4.0
    SUSTAIN_AMPLITUDE = 0.55

    # Idle host→device traffic keeps a sniff-mode link warm. One packet every
    # 8 s is nothing on the radio and well inside any supervision timeout.
    KEEPALIVE_INTERVAL = 8.0

    def __init__(self):
        self._last_send = 0.0        # when we last wrote ANY output report
        self._on_since = None        # when the current motor-on run started
        self._active = False         # was the last packet non-zero?
        self._ka_flip = False        # alternates the keepalive amplitude
        self.dropped = 0             # diagnostics only

    def reset(self):
        """Forget the pacing state (pad reconnected / guard turned off)."""
        self._last_send = 0.0
        self._on_since = None
        self._active = False

    def filter(self, now, low, high, ms, imu=False, droppable=True):
        """Decide what to actually send for a requested rumble.

        Returns (low, high, ms) to send, or None to drop this one.

        `imu`        the pad is streaming gyro/accel right now.
        `droppable`  False for packets that must go out regardless of pacing
                      (a stop, a deliberate confirmation buzz). A stop is also
                      recognised by amplitude, so callers rarely need this.
        """
        low = max(0, min(0xFFFF, int(low)))
        high = max(0, min(0xFFFF, int(high)))
        stopping = (low == 0 and high == 0)
        if stopping:
            # Always let a stop through, immediately. Ending the motor ends the
            # IMU feedback loop; delaying it would be exactly backwards.
            self._last_send = now
            self._on_since = None
            self._active = False
            return (0, 0, int(ms))
        gap = self.MIN_INTERVAL_IMU if imu else self.MIN_INTERVAL
        if droppable and (now - self._last_send) < gap:
            self.dropped += 1
            return None
        scale = 1.0
        if imu:
            scale *= self.IMU_AMPLITUDE
        if self._active and self._on_since is not None \
                and (now - self._on_since) > self.SUSTAIN_LIMIT:
            scale *= self.SUSTAIN_AMPLITUDE
        if scale < 1.0:
            # Keep a requested rumble audible: scale, but never down to a
            # silent 0 (that would read as a broken motor, not a quiet one).
            low = int(low * scale) or (1 if low else 0)
            high = int(high * scale) or (1 if high else 0)
        if not self._active:
            self._on_since = now
        self._active = True
        self._last_send = now
        return (low, high, int(ms))

    def keepalive_due(self, now):
        """True when the link has been quiet long enough that a no-op output
        report is worth sending to hold the sniff-mode connection up."""
        return (now - self._last_send) >= self.KEEPALIVE_INTERVAL

    def keepalive_packet(self, now):
        """The (low, high, ms) no-op to send, and book it as traffic.

        The amplitude alternates 0 ↔ 1 between calls purely so a driver that
        de-duplicates identical rumble values can't optimise the packet away 
        1/65535 moves no motor."""
        self._ka_flip = not self._ka_flip
        self._last_send = now
        # A keepalive must not look like the start of a rumble run.
        self._on_since = None
        self._active = False
        return (1 if self._ka_flip else 0, 0, 1)
