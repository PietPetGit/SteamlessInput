"""The on-screen cursor each trackpad drives around the OSK.

One `VirtualPointer` per pad (left and right), rebuilt every input frame from
the pad's current touch position. `controller` hit-tests it against key rects
with `in_box()`; `smoothen()` blends it against the previous frame's pointer so
the circle glides instead of snapping to raw pad noise.
"""

from adusk import utils


class VirtualPointer:
    """A pad-driven cursor: where it is, and how hard it is being pressed."""

    def __init__(self, state, coord_frac):
        self.state = state
        self.coord_frac = coord_frac

    def in_box(self, bx, by, bw, bh):
        """True if the pointer falls inside the rect at (bx, by) sized bw x bh."""
        x, y = self.coord_frac.to_absolute()
        return bx <= x <= bx + bw and by <= y <= by + bh

    def smoothen(self, prev_vptr, alpha):
        """Low-pass this frame's position against `prev_vptr`, in place."""
        x, y = self.coord_frac.to_absolute()
        prev_x, prev_y = prev_vptr.coord_frac.to_absolute()
        self.coord_frac.update_absolute(
            _step_toward(x, prev_x, alpha),
            _step_toward(y, prev_y, alpha),
        )


def _step_toward(target, prev, alpha):
    """One low-pass step toward `target`, rounded in the DIRECTION OF TRAVEL.

    Rounding to nearest stalls the filter short of its goal: at alpha 0.15 a 3px
    gap only moves 0.45px, which rounds right back to `prev`, so the last 3px
    never close and the circle parks 3px inside wherever the thumb is actually
    pointing (the pads' inner reach measured 701px instead of 704). Rounding the
    filtered value away from `prev` guarantees any non-zero gap shrinks by at
    least 1px per frame, without changing the feel of larger sweeps (a >=7px gap
    already moves more than a pixel on its own)."""
    t = utils.round_to_int(target)
    p = utils.round_to_int(prev)
    if t == p:
        return t
    stepped = utils.round_to_int(utils.compute_lowpass(target, prev, alpha))
    if t > p:
        return min(t, max(p + 1, stepped))
    return max(t, min(p - 1, stepped))
