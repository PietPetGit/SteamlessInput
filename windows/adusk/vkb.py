import time
from collections import namedtuple

import steamcontroller.uinput as sui

from adusk import config
from adusk.color import Color
from adusk import diacritics
from adusk import screen
from adusk import state
from adusk import utils

kb = sui.Keyboard()

# True while dispatch_key runs for an AUTO-REPEAT (held Backspace / arrow), so
# the key click doesn't machine-gun.
_dispatch_is_repeat = False
# True while dispatching a DEFERRED key's release  the base letter of a
# variant-capable key, whose press edge already clicked. Suppresses the second
# click so a quick tap of an accented letter ticks once, not twice.
_dispatch_silent = False

# Hold-to-repeat cadence shared by EVERY "press a key" input path (controller
# X button, A button, L2/R2/pad-click, and mouse left-click): the key fires
# once, then after KEY_REPEAT_DELAY repeats every KEY_REPEAT_INTERVAL seconds.
# Single source of truth so all input modes rub out / arrow-step at one speed.
KEY_REPEAT_DELAY = 0.4
KEY_REPEAT_INTERVAL = 0.205
# Only these keycodes auto-repeat when held: Backspace and the four arrow
# directions (the ◀▶ keys send ▲▼ under Shift). Every other key fires once.
REPEATABLE_KEYS = frozenset({
    sui.Keys.KEY_BACKSPACE,
    sui.Keys.KEY_LEFT, sui.Keys.KEY_RIGHT,
    sui.Keys.KEY_UP, sui.Keys.KEY_DOWN,
})


def is_repeatable(key):
    """True if holding this key should auto-repeat. Checks both the base and
    the shift keycode so the ◀▶ arrow keys repeat whether or not Shift is
    swapping them to ▲▼."""
    if key is None:
        return False
    return (key.keycode in REPEATABLE_KEYS
            or (key.shift_keycode is not None and key.shift_keycode in REPEATABLE_KEYS))


def _parse_hex_color(s):
    s = s.lstrip("#")
    return Color(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _decode_key_color(s):
    """A per-key color override from the layout: a literal '#rrggbb', or a
    'skin:ROLE' marker (e.g. 'skin:key', 'skin:shadow') resolved against the
    active skin at render time so the key tracks skin changes, or None."""
    if not s:
        return None
    if isinstance(s, str) and s.startswith("skin:"):
        return s
    return _parse_hex_color(s)


class VirtualKeyboard:
    # Key size and spacing are matched to Steam's Big Picture keyboard, measured
    # off a 1280x360 capture of it: 4.5px of background between two adjacent
    # keys and 5.5px from the window edge to the outermost keys, which leaves
    # ~85x66 keys (6 + 5*66 + 4*4.5 + 6 == 360 exactly). Both are fractional on
    # purpose  Steam's own gaps alternate 4px/5px down a row, which is what a
    # 4.5px gap looks like once each key rect is rounded to whole pixels.
    #
    # Keys are NOT sized directly: each row's key width falls out of the window
    # width minus these gaps, split across the row's width weights, so the two
    # numbers below are the only spacing knobs. On our 1286x369 canvas they give
    # 84.5..87.5px-wide, 68px-tall keys  Steam's proportions on a canvas that is
    # 6px wider and 9px taller. The canvas size itself is deliberately left alone:
    # the trackpad->keyboard mapping in adusk/controller.py is anchored to a
    # 1286px-wide layout, so resizing the window would move its 704px reach.
    _KEY_GAP = 4.5    # background between two adjacent keys
    _EDGE_GAP = 5.5   # background between the window edge and the outermost keys
    # Every key carries padding_inner on all four sides, so the gap between two
    # keys is twice it; the window edge then only needs the remainder on top.
    padding_inner = _KEY_GAP / 2
    padding_outer = _EDGE_GAP - _KEY_GAP / 2

    # Per-row pixels bought by one unit of width_weight, and the height every
    # row shares. Both are refreshed by update_dimensions() on canvas resize;
    # swipe.py fingerprints a layout off key_width[0]/key_height, so they stay
    # plain attributes rather than derived properties.
    key_width = []
    key_height = 0

    KeyLayout = namedtuple("KeyLayout", "x y w h row col")

    def __init__(self, keys):
        self.keys = keys
        self.key_rows = len(keys)
        # Flattened key-rect cache (see gen_key_layouts), invalidated by
        # update_dimensions  the only thing that can change the layout.
        self._layouts_cache = None
        # Split state the cached layout was built for, so a live Split
        # Keyboard toggle can invalidate it (see _ensure_current).
        self._layouts_split = None
        self.update_dimensions()

    # -- geometry ---------------------------------------------------------
    #
    # A row is a strip of CELLS. Each cell is padding_inner wider than the key
    # it carries on all four sides, so the visible gap between neighbours is
    # 2*padding_inner, and the strip is inset padding_outer from the canvas:
    #
    #     |<-outer->|   cell   |   cell   | ... |<-outer->|
    #                 [  key  ]  [  key  ]
    #
    # Whatever the canvas leaves over after the gaps is split between the keys
    # in proportion to their width_weight, so a row fills the canvas exactly no
    # matter how many keys it holds.

    def _row_pitch(self):
        """Vertical step from one row's cell top to the next."""
        return self.key_height + self.padding_inner * 2

    # "Split Keyboard": fraction of the window width left empty between the
    # two halves. Relative to the window so a "full"-size board gets a
    # proportionally wider band rather than a fixed sliver.
    SPLIT_GAP_FRACTION = 0.06

    def split_gap_px(self):
        """Width (px) of the transparent middle band, 0 when split is off."""
        if not state.is_split_layout_enabled():
            return 0
        return round(screen.width * self.SPLIT_GAP_FRACTION)

    def _split_index(self, row):
        """Column where `row` breaks into its left and right halves: the cut
        that leaves the two halves' width_weight sums as close as possible.
        Both halves get a fixed, equal region (see _split_region), so balancing
        the weights is what keeps the keys the same size on either side. At
        least one key always lands on each side."""
        keys = self.keys[row]
        n = len(keys)
        if n < 2:
            return n
        total = sum(k.width_weight for k in keys)
        best_i, best_diff = 1, None
        left = keys[0].width_weight
        for i in range(1, n):
            diff = abs(left - (total - left))
            if best_diff is None or diff < best_diff:
                best_i, best_diff = i, diff
            left += keys[i].width_weight
        return best_i

    def _split_region(self):
        """Usable width (px) of ONE split half, 0 when split is off.

        Sized from the width the board WOULD have unsplit at the current Size
        setting, not from half the display: the halves anchor to the screen
        edges, so a wide display's extra room becomes transparent band instead
        of stretching every key.

        Clamped to the ACTUAL window, though. The split window is the display's
        usable width, which on a display narrower than the unsplit board is
        LESS than that reference  sizing the halves against the reference then
        makes them overlap in the middle (the band comes out inverted) and the
        rightmost keys run off the screen. Taking the smaller of the two keeps
        the halves inside the window on any display; only the transparent band
        shrinks."""
        if not state.is_split_layout_enabled():
            return 0
        ref_w = min(screen.plain_width(), screen.width)
        gap = round(ref_w * self.SPLIT_GAP_FRACTION)
        return (ref_w - gap) / 2 - self.padding_outer

    def split_gap_band(self):
        """(left_x, right_x) bounds of the transparent middle band, or None
        when split is off. The renderer clears the band to alpha 0 so the
        desktop shows through between the halves."""
        if not state.is_split_layout_enabled():
            return None
        region = self._split_region()
        return (self.padding_outer + region,
                screen.width - self.padding_outer - region)

    def _half_weight_units(self, row):
        """(left, right) pixels-per-width_weight for `row` under split layout.
        Each half is sized against its OWN fixed region, so rows holding
        different numbers of keys still line their band up in the same place.
        Unsplit, both halves are the shared row unit."""
        if not state.is_split_layout_enabled():
            unit = self.key_width[row]
            return unit, unit
        split = self._split_index(row)
        keys = self.keys[row]
        left, right = keys[:split], keys[split:]
        region = self._split_region()
        return ((region - len(left) * self.padding_inner * 2)
                / sum(k.width_weight for k in left),
                (region - len(right) * self.padding_inner * 2)
                / sum(k.width_weight for k in right))

    def _weight_unit(self, row):
        """Pixels one unit of width_weight is worth in `row`."""
        gaps = len(self.keys[row]) * self.padding_inner * 2
        spare = (screen.width - self.padding_outer * 2 - self.split_gap_px()
                 - gaps)
        # Plain running total on purpose. builtin sum() applies compensated
        # (Neumaier) summation to floats, which lands ~1e-14 away from the naive
        # accumulation for a row like [1, 1, ..., 1.365]. No key rect moves
        # either way, but the pad->keyboard mapping in controller.py is
        # calibrated against these exact widths, so keep the arithmetic put.
        total_weight = 0
        for key in self.keys[row]:
            total_weight += key.width_weight
        return spare / total_weight

    def _row_height(self):
        """Key height shared by every row on the current canvas."""
        gaps = self.key_rows * self.padding_inner * 2
        spare = screen.height - self.padding_outer * 2 - gaps
        return spare / self.key_rows

    def update_dimensions(self):
        """Re-derive row height and per-row weight units for the canvas size.

        Also drops the key-rect cache: gen_key_layouts' output depends only on
        the values recomputed here, so this is the one place that can stale it.
        Split layout changes the widths too, which is why toggling it has to
        come back through here."""
        self.key_height = self._row_height()
        self.key_width = [self._weight_unit(row) for row in range(self.key_rows)]
        self._left_key_width = []
        self._right_key_width = []
        for row in range(self.key_rows):
            lw, rw = self._half_weight_units(row)
            self._left_key_width.append(lw)
            self._right_key_width.append(rw)
        self._layouts_cache = None
        self._layouts_split = state.is_split_layout_enabled()

    def _ensure_current(self):
        """Rebuild the layout if Split Keyboard flipped since it was derived.

        Toggling split normally resizes the window, and the resize reflows
        every page  but on a display whose width already matches the split
        window the pixels don't change, so nothing would reflow and the board
        would keep the wrong half widths. Checking the flag here (one bool
        compare on paths that already walk the whole grid) makes the geometry
        self-healing whatever the window did."""
        if self._layouts_split != state.is_split_layout_enabled():
            self.update_dimensions()

    def _walk_row(self, row):
        """Yield (col, key, left, key_w, right) across `row`, left to right.

        `left`/`right` bound the whole cell; `key_w` is just the key inside it.
        The edge is carried forward as a running sum instead of being derived
        from the column index so that hit-testing and rect generation observe
        one identical float sequence  the layout is calibrated to whole-pixel
        positions and the two must never disagree by a rounding step.

        Under split layout the run jumps the middle band at _split_index and
        each half sizes its keys off its own region, so this stays the single
        source of truth for hit-testing, rect generation AND the render pass.
        """
        self._ensure_current()
        keys = self.keys[row]
        if state.is_split_layout_enabled():
            split = self._split_index(row)
            band = self.split_gap_band()
            left = self.padding_outer
            for col, key in enumerate(keys):
                if col == split:
                    left = band[1]
                unit = (self._left_key_width[row] if col < split
                        else self._right_key_width[row])
                key_w = key.width_weight * unit
                right = left + (key_w + self.padding_inner * 2)
                yield col, key, left, key_w, right
                left = right
            return
        left = self.padding_outer
        for col, key in enumerate(keys):
            key_w = key.width_weight * self.key_width[row]
            right = left + (key_w + self.padding_inner * 2)
            yield col, key, left, key_w, right
            left = right

    # Fallback grab radius (px) when the "Key Hit Assist" setting hasn't been
    # published yet. Live value comes from state.get_hit_expand().
    HIT_TARGET_EXPAND = 10

    def find_key_expanded(self, x_coord, y_coord, expand=None):
        """The key a click at (x, y) should hit, with a small grab radius.

        Every key rect is grown by `expand` px on all sides; among the ones
        that then contain the point, the key whose UNEXPANDED rect is nearest
        wins. A point well inside a key is at distance 0, so it resolves
        exactly as find_key would; a point a few px past a boundary, in a row
        gap or in the outer gutter snaps to the key it was aimed at instead of
        the neighbour or nothing. A point far from every key still returns
        None  which is what keeps a press in the split band from typing.
        """
        best = self._find_key_expanded(x_coord, y_coord, expand)
        return self.keys[best[0]][best[1]] if best is not None else None

    def find_key_expanded_rc(self, x_coord, y_coord, expand=None):
        """(row, col) form of find_key_expanded, or None when the point is far
        from every key. Used by the press-to-focus lock, so the key it freezes
        onto is the same one an unlocked click there would have typed."""
        return self._find_key_expanded(x_coord, y_coord, expand)

    def _find_key_expanded(self, x_coord, y_coord, expand):
        if expand is None:
            expand = state.get_hit_expand()
        best = None
        best_d2 = None
        for layout in self.gen_key_layouts():
            l, t = layout.x, layout.y
            r, b = layout.x + layout.w, layout.y + layout.h
            if not (l - expand <= x_coord <= r + expand
                    and t - expand <= y_coord <= b + expand):
                continue
            dx = 0.0 if l <= x_coord <= r else min(abs(x_coord - l),
                                                   abs(x_coord - r))
            dy = 0.0 if t <= y_coord <= b else min(abs(y_coord - t),
                                                   abs(y_coord - b))
            d2 = dx * dx + dy * dy
            if best_d2 is None or d2 < best_d2:
                best = (layout.row, layout.col)
                best_d2 = d2
        return best

    def find_key_row(self, y_coord):
        """Row index under `y_coord`. May land outside the grid; callers clamp."""
        return int((y_coord - self.padding_outer) / self._row_pitch())

    def find_key(self, x_coord, y_coord):
        """The KeyButton under the point, or None past the end of the row."""
        row = utils.clamp(self.find_key_row(y_coord), 0, self.key_rows - 1)
        for _col, key, _left, _key_w, right in self._walk_row(row):
            if x_coord < right:
                return key
        return None

    def find_key_rc(self, x_coord, y_coord):
        """Like find_key but returns the (row, col) grid index  used by the
        mouse handler to drive the same cursor/press path as the DPAD. Clamps
        to the nearest in-bounds cell so an edge click never misses."""
        row = utils.clamp(self.find_key_row(y_coord), 0, self.key_rows - 1)
        for col, _key, _left, _key_w, right in self._walk_row(row):
            if x_coord < right:
                return (row, col)
        return (row, len(self.keys[row]) - 1)

    def get_key_layout(self, target_row, target_col):
        for layout in self.gen_key_layouts():
            if layout.row == target_row and layout.col == target_col:
                return layout
        return None

    def find_col_at_x(self, target_row, x):
        """Pick the column in `target_row` whose pixel range covers `x`,
        falling back to the closest by center if `x` lands in a gap."""
        if not (0 <= target_row < self.key_rows):
            return None
        layouts = [l for l in self.gen_key_layouts() if l.row == target_row]
        if not layouts:
            return None
        for l in layouts:
            if l.x <= x < l.x + l.w:
                return l.col
        best = layouts[0].col
        best_dist = abs((layouts[0].x + layouts[0].w / 2) - x)
        for l in layouts[1:]:
            d = abs((l.x + l.w / 2) - x)
            if d < best_dist:
                best = l.col
                best_dist = d
        return best

    def gen_key_layouts(self):
        """Every key's whole-pixel KeyLayout rect, in render order.

        Cached until update_dimensions: the rects depend only on the values it
        recomputes, and callers (the hover hit-test, the grab-radius scan, the
        render pass) only ever iterate, so one shared tuple is safe and saves
        rebuilding ~90 namedtuples per call  which the grab-radius scan would
        otherwise do on every pointer sample.
        """
        self._ensure_current()
        if self._layouts_cache is None:
            self._layouts_cache = tuple(self._iter_key_layouts())
        return self._layouts_cache

    def _iter_key_layouts(self):
        """Build the rects gen_key_layouts caches.

        The rect is the KEY, not its cell: inset padding_inner from the cell on
        each side, which is what leaves the 2*padding_inner gap between
        neighbours once adjacent rects are rounded.
        """
        top = self.padding_outer
        for row in range(self.key_rows):
            for col, _key, left, key_w, _right in self._walk_row(row):
                yield self.KeyLayout(
                    utils.round_to_int(left + self.padding_inner),
                    utils.round_to_int(top + self.padding_inner),
                    utils.round_to_int(key_w),
                    utils.round_to_int(self.key_height),
                    row, col)
            top += self._row_pitch()


class KeyButton:
    def __init__(self, str, keycode, callback, width_weight=1.0, shifted=None, modifier=False, align="center", valign="center", glyph=None, font="default", text_color=None, bg_color=None, swap_on_shift=False, shift_glyph=None, shift_valign=None, font_size=None, shift_keycode=None):
        self.str = str
        self.shifted = shifted   # Label shown when shift is held; None means show `str`.
        self.keycode = keycode
        self.callback = callback
        self.width_weight = width_weight
        self.modifier = modifier   # Renderer paints modifier keys with a pure-black background.
        self.align = align         # "left" | "center" | "right"  label alignment inside the key.
        self.valign = valign       # "top" | "center" | "bottom"  vertical label alignment.
        self.glyph = glyph         # Path-relative filename in data/images/glyphs/ (e.g. "glyph_l2.png").
        self.font = font           # "default" | "symbol"  picks the symbol font for glyphs Segoe lacks.
        self.text_color = text_color  # Optional Color overriding INACTIVE text color.
        self.bg_color = bg_color   # Optional Color overriding INACTIVE key background.
        # When True, the shifted variant fully replaces the main label on shift
        # (e.g. arrow keys ◀↔▲); when False the shifted variant renders as a
        # small gray "shadow" label above the main one (typewriter keys).
        self.swap_on_shift = swap_on_shift
        self.shift_glyph = shift_glyph    # Overrides glyph while shift held ("" = no glyph).
        self.shift_valign = shift_valign  # Overrides valign while shift held.
        self.font_size = font_size        # Optional explicit pixel size for the main label.
        # Optional alternate keycode used when Shift is held at dispatch time
        # (e.g. ◀ sends KEY_LEFT unshifted, KEY_UP while Shift is held).
        self.shift_keycode = shift_keycode
        # Per-key DPAD navigation overrides (target column in the adjacent
        # row); when None, the main loop falls back to pixel-x mapping.
        self.dpad_up = None
        self.dpad_down = None
        self.dpad_left = None
        self.dpad_right = None
        # Per-key horizontal label nudge in px (e.g. arrows shifted outward).
        self.text_dx = 0
        # Optional smaller font for the shadow preview only (e.g. arrow ▲▼).
        self.shadow_font_size = None
        # Keep this dual-state key's labels at their pre-2026-06-09 rest
        # positions (upper at key.y+3, lower at 4px bottom pad) instead of the
        # nudged-up defaults  set per key in the layout YAML. Animation target
        # (centered) is unchanged either way.
        self.legacy_label_pos = False
        # Per-key fine offsets (px, +down) added to the dual-label REST
        # positions only (the Shift-centered target is unchanged): top_dy nudges
        # the upper label, bottom_dy the lower label. Opposite signs pull the
        # pair together / apart; equal signs shift it. Set per key in the YAML.
        self.dual_top_dy = 0
        self.dual_bottom_dy = 0
        # Per-key transparent-mode text-outline overrides (None = use the
        # Screen defaults): outline_px = sub-pixel ring offset (thicker outline),
        # outline_opacity = 0..1 outline alpha factor. Set per key in the YAML.
        self.outline_px = None
        self.outline_opacity = None
        # Phone layout only: small grey symbol drawn in the key's top-right
        # corner (the Android long-press hint). Purely a label.
        self.hint = None
        # A width-reserving blank, not a key: the phone layout's A-L row is
        # inset half a key at each end. Drawn as nothing and skipped by the
        # cursor, so it can never become a dead cell to navigate onto.
        self.spacer = False
        # Behaviour fired when this key is HELD rather than tapped (the phone
        # layout's Shift opens the symbol page, like an Android long-press).
        # None for every ordinary key.
        self.hold_callback = None
        # Vertical placement of the glyph icon, independent of the text
        # `valign`: None (default) keeps every existing key's icon anchored
        # near the key's bottom edge (Tab/Caps/Shift's L2/L3 hints, Move's
        # keyboard icon under its top-aligned label); "center" vertically
        # centers the icon instead, for keys where the icon and label sit
        # side-by-side on one line (Backspace's X circle + "Backspace" text).
        self.glyph_valign = None
        # True for the 75% layout's "Select" key (behavior: select)  the
        # hold-and-drag text-selection key that stands where a right Shift
        # would be. The input threads watch this rather than the sentinel
        # keycode, and the renderer paints it pressed for the whole gesture.
        self.is_select = False
        # True for the "Move" key (behavior: move)  its shifted form cycles
        # the window through its 6 spots.
        self.is_move = False

    def display_label(self, shift_held, caps_on=False):
        if self.shifted is None:
            return self.str
        # Single-letter alpha keys honor BOTH shift and caps lock.
        if len(self.str) == 1 and self.str.isalpha():
            return self.shifted if (shift_held ^ caps_on) else self.str
        # Number / symbol keys only honor shift (caps lock has no effect).
        return self.shifted if shift_held else self.str


def on_key_generic(virtual_kb, keycode):
    kb.pressEvent([keycode])
    kb.releaseEvent([keycode])


def _make_text_callback(text):
    """Behaviour for a key that types a literal character (phone symbol pages).

    Symbol keys can't be expressed as keycodes: KEY_2 only yields '@' while
    Shift is down, and '€'/'π'/'™' have no keysym at all. The character is
    injected as itself instead  see Keyboard.type_text. Any Shift the user
    happens to be holding is dropped around the injection, since the OS would
    otherwise fold it in and produce a different character."""
    def _type(virtual_kb, keycode):
        shift_held = state.is_shift_held()
        if shift_held:
            kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT])
        kb.type_text(text)
        if shift_held:
            kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
    return _type


def _make_page_callback(page):
    """Behaviour for the phone layout's ?123 / =\\< / ABC page-switch keys."""
    def _switch(virtual_kb, keycode):
        # Never carry a latched Shift across a page switch: the symbol pages
        # have no Shift key to turn it back off, so it would sit stranded and
        # then surprise the user on the way back to the letters.
        clear_shift_latch(release_os=True)
        state.set_osk_page(page)
    return _switch


# A held key fires its `hold` behaviour once, not once per repeat tick. The
# page switch swaps virtual_kb underneath us, but the rest of THIS drain still
# walks the old one, so a plain "already fired" flag isn't enough  the next
# queued repeat would land on the same key again. A short cooldown is simpler
# than threading touch identity through the click queue, and a genuine second
# hold inside it is not a gesture anyone can make.
_HOLD_COOLDOWN = 0.6
_last_hold_t = 0.0


def fire_hold(virtual_kb, key):
    """Run a key's hold behaviour if one is due. True if it fired."""
    global _last_hold_t
    now = time.monotonic()
    if now - _last_hold_t < _HOLD_COOLDOWN:
        return False
    _last_hold_t = now
    key.hold_callback(virtual_kb, key.keycode)
    state.pad_click_haptic()
    return True


def tap_keycode(keycode):
    """Press + release a single keycode (used by the mouse side buttons)."""
    kb.pressEvent([keycode])
    kb.releaseEvent([keycode])


def _modifier_highlights():
    """Keycodes of every currently latched modifier, so flipping one keeps the
    others lit. A toggle must never blank the whole highlight set  unlatching
    Shift can't un-paint a latched Ctrl."""
    keys = set()
    if state.is_shift_latched():
        keys.update((sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT))
    if state.is_ctrl_latched():
        keys.update((sui.Keys.KEY_LEFTCTRL, sui.Keys.KEY_RIGHTCTRL))
    if state.is_alt_latched():
        keys.update((sui.Keys.KEY_LEFTALT, sui.Keys.KEY_RIGHTALT))
    return keys


def toggle_ctrl():
    """Flip the latched-Ctrl state (mirror of toggle_shift). Holds real
    KEY_LEFTCTRL on the OS while engaged, so the next key produces its Ctrl+
    combination (Ctrl+C, Ctrl+A, ...), and paints the on-screen Ctrl key."""
    new = not state.is_ctrl_latched()
    state.set_ctrl_latched(new)
    if new:
        kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
    else:
        kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
    state.set_highlighted(_modifier_highlights())


def toggle_alt():
    """Flip the latched-Alt state (mirror of toggle_ctrl). Holds real
    KEY_LEFTALT so the next key produces its Alt+ combination."""
    new = not state.is_alt_latched()
    state.set_alt_latched(new)
    if new:
        kb.pressEvent([sui.Keys.KEY_LEFTALT])
    else:
        kb.releaseEvent([sui.Keys.KEY_LEFTALT])
    state.set_highlighted(_modifier_highlights())


def release_ctrl():
    """Force-release the OS Ctrl key so closing the keyboard never strands it
    down (mirror of release_shift)."""
    kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
    kb.releaseEvent([sui.Keys.KEY_RIGHTCTRL])
    state.set_ctrl_latched(False)


def release_alt():
    """Force-release the OS Alt key so closing the keyboard never strands it
    down (mirror of release_ctrl)."""
    kb.releaseEvent([sui.Keys.KEY_LEFTALT])
    kb.releaseEvent([sui.Keys.KEY_RIGHTALT])
    state.set_alt_latched(False)


def on_key_ctrl(virtual_kb, keycode):
    # Clicking the on-screen Ctrl key toggles the latched Ctrl state.
    toggle_ctrl()


def on_key_alt(virtual_kb, keycode):
    # Clicking the on-screen Alt key toggles the latched Alt state.
    toggle_alt()


def on_key_select(virtual_kb, keycode):
    # The Select key selects text by being HELD and dragged (iOS hold-space
    # style), which the pad and mouse threads drive directly  press enters
    # select mode, horizontal travel fires Shift+Left/Right, release leaves it.
    # A plain tap has nothing to select, so it is a deliberate no-op.
    pass


def toggle_shift():
    """Flip the latched-Shift state. Unlike the controller's L2 (held only while
    the trigger is down), the mouse/keyboard path latches Shift so it stays on
    until clicked again  the only sane model when there's no button to hold.
    Holds real KEY_LEFTSHIFT on the OS so the next key produces its shifted
    form, and paints the on-screen Shift keys blue while engaged.

    Decides on/off from our OWN latch flag, not state.is_shift_held(): a
    connected controller rewrites is_shift_held() every input frame, so reading
    it here would see False on every click and re-press forever (never toggle
    off). The controller ORs the latch into the display state, so they cooperate."""
    new = not state.is_shift_latched()
    state.set_shift_latched(new)
    if new:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
    else:
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
    # Recompute from ALL the latches  a latched Ctrl/Alt must stay lit.
    state.set_highlighted(_modifier_highlights())
    state.set_shift_held(new)


def release_shift():
    """Force-release the OS Shift key so hiding/closing the keyboard never
    leaves Shift stuck down. Unconditional on purpose: the latched-state flag
    can be out of sync with the real OS key (a controller input frame
    overwrites state.is_shift_held() every tick, and either the mouse toggle or
    a held L2 may own the OS key), and a pynput Shift key-up is idempotent 
    harmless if nothing was held  so we always send it."""
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
    kb.releaseEvent([sui.Keys.KEY_RIGHTSHIFT])
    state.set_shift_latched(False)
    state.set_shift_held(False)
    state.set_highlighted(_modifier_highlights())


def clear_shift_latch(release_os=True):
    """Drop a mouse-latched Shift when another input source takes over (the
    Steam Controller's A button or the DPAD), reverting the sticky mouse toggle
    to the hold model those controls use. Releases the OS Shift the latch was
    holding unless release_os is False (e.g. L2 is currently holding Shift, so
    the OS key must stay down). Display state/highlight are recomputed by the
    controller's next input frame."""
    if not state.is_shift_latched():
        return
    state.set_shift_latched(False)
    if release_os:
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_shift_held(False)


def on_key_shift(virtual_kb, keycode):
    # Clicking the on-screen Shift key (mouse left-click or the A button)
    # toggles the latched Shift state.
    toggle_shift()


def on_key_paste(virtual_kb, keycode):
    # Paste, or Copy when Shift is held: Shift → Ctrl+C, otherwise Ctrl+V.
    # ALWAYS release both shifts first  not only when our logical state says
    # Shift is held. A shift-mode press re-presses Shift on THIS keyboard
    # instance (vkb.kb) to restore an L2-held Shift, but L2's release lands on
    # the controller thread's SEPARATE instance, so vkb.kb is left believing
    # Shift is still down. pynput then re-asserts that Shift around the next key,
    # breaking the chord (e.g. Ctrl+V arrives as Shift+V → "V"). Releasing here
    # resets vkb.kb's modifier state; then the chord via tap_with_modifier (raw
    # VK so it combines with Ctrl), then restore Shift if it's logically held.
    shift_held = state.is_shift_held()
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT])
    kb.tap_with_modifier(sui.Keys.KEY_LEFTCTRL,
                         sui.Keys.KEY_C if shift_held else sui.Keys.KEY_V)
    if shift_held:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])


def on_key_emoji(virtual_kb, keycode):
    # Toggle the OS emoji picker: open it with Win+. (Windows) / Meta+. (Linux),
    # or  if our last emoji press opened it  close it with Escape, so pressing
    # the on-screen emoji key again dismisses the picker. ALWAYS release both
    # shifts first so the OS sees Meta+. (not Meta+Shift+., a different shortcut)
    # AND to clear any Shift stranded in vkb.kb's modifier state by a prior L2
    # shift paste (see on_key_paste), then restore Shift only if logically held.
    shift_held = state.is_shift_held()
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT])
    if state.is_emoji_open():
        # Already open → Escape closes the focused picker.
        close_emoji_picker()
    else:
        kb.tap_with_modifier(sui.Keys.KEY_LEFTMETA, sui.Keys.KEY_DOT)
        state.set_emoji_open(True)
    if shift_held:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])


def close_emoji_picker():
    """Dismiss the OS emoji picker (Escape) if our toggle thinks it's open, and
    clear the flag. Arms the Esc-close suppressor first so the global Esc
    listener doesn't also close the OSK. No-op if already closed. Called both by
    the emoji-key toggle and by the OSK teardown (so the picker never lingers
    after the keyboard is gone)."""
    if not state.is_emoji_open():
        return
    state.suppress_esc_close()
    kb.pressEvent([sui.Keys.KEY_ESC])
    kb.releaseEvent([sui.Keys.KEY_ESC])
    state.set_emoji_open(False)


def on_key_move(virtual_kb, keycode):
    # Unshifted Move closes the keyboard; with Shift held, instead advance
    # the window through its 6-position rotation (handled in the main loop).
    if state.is_shift_held():
        state.request_position_cycle()
    else:
        state.close()


class VirtualKeyboardConfig(config.ObjectConfig):
    @staticmethod
    def decode_keycode(str):
        try:
            return sui.Keys[str]
        except KeyError:
            assert False, "Invalid keycode `{}`".format(str)

    # Phone layout page-switch behaviours: "page:<name>" swaps the OSK to that
    # page of the same layout (?123 / =\< / ABC). The page names themselves are
    # just the keys of the layout's `pages:` map, so a layout can define as many
    # as it likes without touching this decoder.
    _PAGE_PREFIX = "page:"

    @staticmethod
    def decode_callback(str):
        if str == "generic":
            pass
        elif str == "shift":
            return on_key_shift
        elif str == "ctrl":
            return on_key_ctrl
        elif str == "alt":
            return on_key_alt
        elif str == "select":
            return on_key_select
        elif str == "paste":
            return on_key_paste
        elif str == "emoji":
            return on_key_emoji
        elif str == "move":
            return on_key_move
        elif str.startswith(VirtualKeyboardConfig._PAGE_PREFIX):
            return _make_page_callback(
                str[len(VirtualKeyboardConfig._PAGE_PREFIX):])
        else:
            assert False, "Invalid behavior `{}`".format(str)
        return on_key_generic

    def construct_pages(self):
        """Every page of the layout as {name: VirtualKeyboard}.

        A layout with a bare `keys:` list is a single page named "main"  which
        is every classic layout, so nothing else has to care that pages exist.
        A layout with `pages:` (the phone one: abc / sym1 / sym2) gets one
        VirtualKeyboard per page; they are SEPARATE objects on purpose, so
        swapping pages changes the identity every consumer keys off (the swipe
        decoder's geometry cache, the render dirty-flag) for free."""
        pages = self.objects.get("pages")
        if not pages:
            return {"main": self.construct()}
        return {name: VirtualKeyboard(self._build_rows(rows))
                for name, rows in pages.items()}

    def construct(self):
        return VirtualKeyboard(self._build_rows(self.objects["keys"]))

    def _build_rows(self, yaml_rows):
        keys = []

        for yaml_row in yaml_rows:
            row = []
            for yaml_key in yaml_row:
                label = yaml_key.get("label", "")
                shifted = yaml_key.get("shifted")
                modifier = bool(yaml_key.get("modifier", False))
                align = yaml_key.get("align", "center")
                valign = yaml_key.get("valign", "center")
                glyph = yaml_key.get("glyph")
                font = yaml_key.get("font", "default")
                text_color = _decode_key_color(yaml_key.get("text_color"))
                bg_color = _decode_key_color(yaml_key.get("bg_color"))
                swap_on_shift = bool(yaml_key.get("swap_on_shift", False))
                shift_glyph = yaml_key.get("shift_glyph")
                shift_valign = yaml_key.get("shift_valign")
                font_size = yaml_key.get("font_size")
                keycode = (self.decode_keycode(yaml_key["keycode"])
                           if "keycode" in yaml_key else 0)
                shift_keycode_str = yaml_key.get("shift_keycode")
                shift_keycode = self.decode_keycode(shift_keycode_str) if shift_keycode_str else None
                behavior = yaml_key.get("behavior", "generic")
                width_weight = yaml_key.get("width_weight", 1.0)

                # `text:` overrides the behaviour: the key types that literal
                # character rather than a keycode (see _make_text_callback).
                literal = yaml_key.get("text")
                if literal:
                    callback = _make_text_callback(literal)
                else:
                    callback = self.decode_callback(behavior)
                kb_btn = KeyButton(label, keycode, callback, width_weight,
                                   shifted=shifted, modifier=modifier, align=align, valign=valign,
                                   glyph=glyph, font=font, text_color=text_color, bg_color=bg_color,
                                   swap_on_shift=swap_on_shift, shift_glyph=shift_glyph,
                                   shift_valign=shift_valign, font_size=font_size,
                                   shift_keycode=shift_keycode)
                # `behavior: select` is the hold-and-drag text-selection
                # key; the input threads watch the flag rather than the
                # sentinel keycode, and the renderer paints it pressed while
                # a selection is live.
                kb_btn.is_select = behavior == "select"
                kb_btn.is_move = behavior == "move"
                kb_btn.dpad_up = yaml_key.get("dpad_up")
                kb_btn.dpad_down = yaml_key.get("dpad_down")
                kb_btn.dpad_left = yaml_key.get("dpad_left")
                kb_btn.dpad_right = yaml_key.get("dpad_right")
                kb_btn.text_dx = yaml_key.get("text_dx", 0)
                kb_btn.shadow_font_size = yaml_key.get("shadow_font_size")
                kb_btn.legacy_label_pos = yaml_key.get("legacy_label_pos", False)
                kb_btn.dual_top_dy = yaml_key.get("dual_top_dy", 0)
                kb_btn.dual_bottom_dy = yaml_key.get("dual_bottom_dy", 0)
                kb_btn.outline_px = yaml_key.get("outline_px")
                kb_btn.outline_opacity = yaml_key.get("outline_opacity")
                kb_btn.glyph_valign = yaml_key.get("glyph_valign")
                # Phone layout: the small grey symbol in a letter key's top
                # right, mirroring what that key long-presses to on Android.
                # A label only  `shifted` can't carry it, because the renderer
                # treats a single-letter key's shifted form as its capital.
                kb_btn.hint = yaml_key.get("hint")
                kb_btn.spacer = bool(yaml_key.get("spacer", False))
                hold = yaml_key.get("hold")
                kb_btn.hold_callback = self.decode_callback(hold) if hold else None
                row.append(kb_btn)

            keys.append(row)
        return keys


def step_cursor(virtual_kb, direction, haptic=False):
    start = state.get_cursor()
    row, col = start
    rows = len(virtual_kb.keys)
    if not (0 <= row < rows):
        row = max(0, min(row, rows - 1))
        col = 0
    cur_btn = virtual_kb.keys[row][col] if 0 <= col < len(virtual_kb.keys[row]) else None

    if direction == "LEFT":
        override = cur_btn.dpad_left if cur_btn else None
        if override is not None:
            col = override
        elif col > 0:
            col -= 1
        else:
            col = len(virtual_kb.keys[row]) - 1
    elif direction == "RIGHT":
        override = cur_btn.dpad_right if cur_btn else None
        if override is not None:
            col = override
        elif col < len(virtual_kb.keys[row]) - 1:
            col += 1
        else:
            col = 0
    elif direction in ("UP", "DOWN"):
        new_row = row - 1 if direction == "UP" else row + 1
        if 0 <= new_row < rows:
            override = (cur_btn.dpad_up if direction == "UP" else cur_btn.dpad_down) if cur_btn else None
            if override is not None:
                col = override
            else:
                cur_layout = virtual_kb.get_key_layout(row, col)
                if cur_layout is not None:
                    x_center = cur_layout.x + cur_layout.w // 2
                    new_col = virtual_kb.find_col_at_x(new_row, x_center)
                    if new_col is not None:
                        col = new_col
            row = new_row

    col = max(0, min(col, len(virtual_kb.keys[row]) - 1))
    col = _skip_spacers(virtual_kb, row, col, direction)
    state.set_cursor(row, col)
    if haptic and (row, col) != start:
        state.haptic_tick()


def _skip_spacers(virtual_kb, row, col, direction):
    """Nudge the cursor off a spacer onto the nearest real key.

    Spacers exist only to reserve width (the phone layout's inset A-L row), so
    landing on one would be a dead cell. Step on in the direction of travel;
    UP/DOWN arrive vertically with no horizontal intent, so those search
    outward from where they landed and take whichever side is closer."""
    keys = virtual_kb.keys[row]
    n = len(keys)
    if not (0 <= col < n) or not keys[col].spacer:
        return col
    if direction in ("LEFT", "RIGHT"):
        step = -1 if direction == "LEFT" else 1
        for i in range(1, n + 1):
            c = (col + step * i) % n
            if not keys[c].spacer:
                return c
        return col
    for dist in range(1, n):
        for c in (col - dist, col + dist):
            if 0 <= c < n and not keys[c].spacer:
                return c
    return col


def dispatch_key(virtual_kb, key):
    # Steam's keyboard click, on each physical press (never on an auto-repeat,
    # and never on a deferred base whose press edge already clicked). Fired
    # here because dispatch_key is the one choke point every input path
    # trackpad, controller button, mouse  funnels through.
    if not _dispatch_is_repeat and not _dispatch_silent:
        state.key_sound_tick()
    # Echo the key's visible label for anyone watching (the first-run tour's
    # keyboard slide  see state.note_typed_key). Read BEFORE the dispatch,
    # since a Shift key changes the very state the label depends on.
    try:
        state.note_typed_key(key.display_label(state.is_shift_held(),
                                               state.is_caps_on()))
    except Exception:
        pass
    # Keys with a `shift_keycode` (e.g. ◀▶ → ▲▼) want to send the alternate
    # keycode WITHOUT the OS seeing a Shift modifier; otherwise Shift+Arrow
    # selects text instead of just moving the caret. Briefly drop and
    # re-raise Shift around the dispatch so the OS sees only the arrow.
    if key.shift_keycode and state.is_shift_held():
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        key.callback(virtual_kb, key.shift_keycode)
        if state.is_shift_held():
            kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
    else:
        key.callback(virtual_kb, key.keycode)


# --- Diacritic variants (hold a letter to pick its accented forms) --------


def diacritic_variants_for_key(key):
    """The accented-variant list for `key`, or None when it has none: not a
    single ASCII letter key, the feature is off, or the active locale's map has
    no entry. The map is keyed lowercase (the OSK types lowercase by default;
    case is a single tap of Shift away), so the label is folded."""
    if not state.is_diacritics_enabled():
        return None
    label = key.str
    if (not label or len(label) != 1 or not label.isascii()
            or not label.isalpha()):
        return None
    return diacritics.lookup_variants(state.get_diacritic_variants(),
                                      state.get_diacritic_locale(),
                                      label.lower())


def open_diacritic_rc(virtual_kb, row, col, source):
    """Open the variant row for the letter key at (row, col)  the key held
    past the hold delay. `source` records which input opened it ("pad" /
    "mouse" / "button") so only that input drives the highlight and the commit.
    True if a row opened; False when the key has no variants, the feature is
    off, or a row is already up."""
    if not state.is_diacritics_enabled() or state.is_diacritic_open():
        return False
    try:
        key = virtual_kb.keys[row][col]
    except IndexError:
        return False
    variants = diacritic_variants_for_key(key)
    if not variants:
        return False
    layout = virtual_kb.get_key_layout(row, col)
    if layout is None:
        return False
    rect = diacritics.variant_row_rect(layout, len(variants), screen.width)
    if rect is None:
        # A strip too wide to clamp into the window (a pathological user map,
        # or a very narrow board)  refuse rather than open a clipped row.
        return False
    # The base letter follows the OS shift state, so a shift-held hold has to
    # offer uppercase variants too  otherwise picking one would silently
    # downcase what the user was typing. A variant whose uppercase is NOT one
    # character is left as-is: German 'ss'.upper() is two letters, which would
    # both draw as a two-glyph candidate and break the single-character commit
    # path.
    if state.is_shift_held() or state.is_caps_on():
        variants = [v.upper() if len(v.upper()) == 1 else v for v in variants]
    state.open_diacritic(key.str.lower(), variants, rect, source)
    return True


def open_diacritic_at(virtual_kb, x, y, source):
    """open_diacritic_rc for a pointer position  the pad/mouse path, resolved
    with the grab radius so a slightly-off hold still opens the right key."""
    rc = virtual_kb.find_key_expanded_rc(x, y)
    if rc is None:
        return False
    return open_diacritic_rc(virtual_kb, rc[0], rc[1], source)


def commit_diacritic(char=None):
    """Type the picked variant and close the row.

    The base letter is NOT typed at the press edge for a variant-capable key
    (see the defer path in controller.py), so there is nothing to rub out
    first  the variant just goes in. `char` is read from the open session
    when not given. The row is closed in a finally: an injection that throws
    must never leave it up, or every later hold would queue a repeat instead
    and typing would silently die for the rest of the session."""
    if char is None:
        char = state.get_diacritic_selected_char()
    try:
        if not char:
            return
        kb.tap_char(char)
    finally:
        state.close_diacritic()


def process_click_queue(virtual_kb, queue):
    while len(queue) > 0:
        item = queue.popleft()
        try:
            _process_click_item(virtual_kb, item)
        except Exception as e:
            # One bad item must never kill the main loop: a throwing commit
            # (a clipboard paste failure escaping tap_char, say) would freeze
            # ALL typing for the rest of the session. Drop it and carry on.
            print(f"adusk: click item {item!r} failed: {e!r}")


def _process_click_item(virtual_kb, item):
    global _dispatch_is_repeat, _dispatch_silent
    # ("variant", char)  a diacritic commit queued by the input thread that
    # saw its press release.
    if isinstance(item, tuple) and item and item[0] == "variant":
        commit_diacritic(item[1])
        return
    # ("deferred", coord)  a variant-capable key's base letter, typed on
    # release because the press edge deliberately held it back. The press edge
    # already clicked, so this dispatch stays silent.
    if isinstance(item, tuple) and item and item[0] == "deferred":
        _dispatch_silent = True
        try:
            _process_click_item(virtual_kb, item[1])
        finally:
            _dispatch_silent = False
        return
    # A bare coord is a normal first hit (any key). A ("repeat", coord)
    # tuple is an auto-repeat from holding L2/R2/pad-click  honoured only
    # over Backspace, so holding rubs out text but won't machine-gun
    # ordinary keys (matches the X-button delete repeat).
    is_repeat = isinstance(item, tuple) and item and item[0] == "repeat"
    coord = item[1] if is_repeat else item
    if isinstance(coord, tuple):
        return          # an unexpected tagged item  drop it defensively
    x, y = coord.to_absolute()
    # Grab radius: a click a few px past a key boundary, or in a gap, still
    # lands on the key it was aimed at, so fast two-finger typing stops
    # mistyping on near-misses.
    key = virtual_kb.find_key_expanded(x, y)
    if key is None:
        return
    if is_repeat:
        # A key with a `hold` behaviour uses the repeat tick purely as the
        # "still held" signal  the phone layout's Shift opens the symbol
        # page that way, matching the phone's long-press. It fires once and
        # swallows the rest of the repeats rather than auto-repeating.
        if key.hold_callback is not None:
            fire_hold(virtual_kb, key)
            return
        if not is_repeatable(key):
            # Letters don't repeat  but the FIRST repeat tick past the hold
            # delay is exactly the "held long enough" signal the accent row
            # wants. Safety net for any pad-style source; the controller
            # thread opens the row itself at repeat-arming so it can watch
            # the same press for the release commit.
            open_diacritic_at(virtual_kb, x, y, "pad")
            return
    _dispatch_is_repeat = is_repeat
    try:
        dispatch_key(virtual_kb, key)
    finally:
        _dispatch_is_repeat = False
    # Note: the click haptic is fired earlier, on the controller thread at
    # click-detection (see ControllerManager.handle_pad_input), for lowest
    # latency  so it is intentionally NOT fired here.
