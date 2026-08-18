"""Small numeric helpers shared by the OSK's layout and pointer maths.

Kept deliberately tiny: every caller is on a per-frame path (key-rect layout in
`vkb`, pointer smoothing in `vptr`, pad-to-screen mapping in `controller`), so
these stay plain functions with no imports and no branching beyond what the
maths needs.
"""

import math


def round_to_int(f):
    """Round to the nearest whole pixel and return a real `int`.

    Callers feed the result straight into rect/coordinate fields that must be
    integral, so the `int()` is load-bearing: `round()` alone returns a float
    for float input on Python 3 only when given a second argument, but being
    explicit keeps the contract obvious at the call sites.
    """
    return int(round(f))


def clamp(value, low, high):
    """Constrain `value` to the inclusive range [`low`, `high`]."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def compute_lowpass(curr, prev, alpha):
    """One first-order low-pass step from `prev` toward `curr`.

    `alpha` is the per-frame blend factor: 0.0 freezes at `prev`, 1.0 snaps
    straight to `curr`. Expressed as an interpolation between the two endpoints
    so that the `alpha` extremes land exactly on `prev`/`curr` without drift
    from repeated accumulation.
    """
    return (1.0 - alpha) * prev + alpha * curr


def spring_p(t, zeta, omega0):
    """Underdamped-spring step response from rest toward 1 at time `t`.

    Closed form of m*x'' + c*x' + k*x = 0, so it is time-based rather than
    per-frame and therefore identical at 60/120/144 fps:

        p(t) = 1 - e^(-zeta*omega0*t) * [cos(wd*t) + (zeta/sqrt(1-zeta^2))*sin(wd*t)]
        wd   = omega0 * sqrt(1 - zeta^2)

    `zeta` (damping ratio) sets the bounce  0.5 pronounced, 0.7 subtle, 1.0
    none  and `omega0` (natural frequency) sets the speed. May overshoot 1 for
    zeta < 1, which is the point: it is what makes a released key spring back
    rather than slide back. One exp + one sin + one cos, so it is cheap enough
    for the handful of keys under a thumb at 120 fps.
    """
    if t <= 0.0:
        return 0.0
    s = 1.0 - zeta * zeta
    if s <= 0.0:
        return 1.0          # critically/over-damped: no oscillation to solve
    wd = omega0 * math.sqrt(s)
    e = math.exp(-zeta * omega0 * t)
    return 1.0 - e * (math.cos(wd * t) + (zeta / math.sqrt(s)) * math.sin(wd * t))
