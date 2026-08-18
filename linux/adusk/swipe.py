"""Swipe Typing ("shape writing") for the on-screen keyboard.

Drag a thumb across a trackpad through roughly the letters of a word, lift, and
the traced path is matched against a lexicon and the winning word typed  the
phone gesture, on a Steam Controller. This is the SHARK2/Swype idea: the SHAPE
a word draws on a FIXED keyboard is very nearly unique, so the decoder never
needs the path to actually land on each key. That tolerance is the whole point,
and it is what makes the gesture possible at all on a trackpad whose reach is
coarser than a fingertip on glass.

A path is scored against the lexicon on five channels:

  * ENDPOINTS  how far the word's first and last letters sit from where the
    finger actually landed and stopped. Those two keys are the only ones the
    user deliberately aims at and pauses on, so they are worth far more than
    the pass-through middle; without this channel a start letter freely drifts
    to its neighbour and frequency then picks the wrong word ("settings" read
    as "wings").
  * COVERAGE  how many of the word's letters the path never went anywhere
    near, as a letter bitmask AND with the set of keys the path passed over.
    Cheap and sharply discriminative: it is what stops a "swipe" path from
    decoding as "word".
  * LENGTH  how far the word's ideal tracing would travel versus how far the
    finger actually did. Separates words that share a start, an end and an
    alphabet but not a route ("settings" vs "sins").
  * SHAPE  both curves resampled to a fixed point count and normalised into a
    unit box, so the match is about HOW the word is written, independent of
    where on the keyboard it was drawn or how big.
  * LOCATION  the same two curves in raw keyboard pixels, so two words that
    trace a similar shape in different PLACES stay apart.

Plus a FREQUENCY prior, which is not a nicety but a requirement: shape writing
produces exact ties by construction  a doubled letter traces no extra path, so
"hello" and "helo" are the SAME curve  and only relative word frequency can
break them.

On Windows the surviving candidates are then rescored by
`Windows.Data.Text.TextPredictionGenerator`, the prediction engine behind the
Windows touch keyboard, reached through the WinRT projection. It knows
real-world AND personal word frequency, which a static bundled list cannot. It
is strictly a RE-RANKER: measured against raw swipe skeletons it returns
nothing at all ("hgfdrewertyuiol" -> []), because it corrects typos
(substitutions) rather than the pass-through letters a swipe path is made of.
It cannot decode a path on its own, and nothing here asks it to.

Everything degrades rather than breaking: no lexicon file, no `winrt` package,
no prediction language installed, an unknown key layout  the feature just gets
less clever, or quietly does nothing.

PERFORMANCE. A decode runs on the controller input thread the instant the
finger lifts, so it is written for a slow interpreter, not for elegance.
Distances are kept SQUARED and computed inline (`dx*dx + dy*dy`) over FLAT
[x0,y0,x1,y1,...] arrays, because a single `math.hypot` call can cost several
microseconds and the inner loop runs tens of thousands of times. Words are
pre-digested at load into (prior, word, letter indices, letter bitmask) so the
hot loop does integer and list work only  no string slicing, no set building,
no math module. Two passes: a template-free geometric pass over every gated
word, then the full score on a few hundred survivors.

That ordering matters for correctness, not just speed. An earlier version
capped the gated set BY FREQUENCY before scoring, which silently made every
word past the cap undecodable no matter how perfectly it was traced  the cheap
pass must be geometric, so a rare word can still earn its way in.

The bundled lexicon (`data/lexicon/en.txt`) is the top 30,000 pure-a-z entries
of the MIT-licensed English list from hermitdave/FrequencyWords, kept in the
source's own frequency order so a word's LINE NUMBER is its rank. See
THIRD_PARTY_NOTICES.md.
"""

import math
import os
import threading

from adusk import resources

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------
# Points both the user path and every word template are resampled to. SHARK2
# used 100 on a desktop CPU; the ranking is unchanged far below that, and every
# extra point costs across hundreds of candidates.
_RESAMPLE_N = 12
_FLAT_N = _RESAMPLE_N * 2
# How far, in key widths, a word's first/last letter may sit from the path's
# start/end and still be considered. The finger genuinely aims at those two
# letters, so this gate is both safe and the single biggest speed win: it takes
# 30,000 words down to around a thousand before any scoring happens.
_GATE_KEYS = 1.3
# Words surviving the cheap pass into the full five-channel score.
_STAGE_B = 260
# Absolute safety valve on the gated set (cut by frequency, the only ordering
# available that early). Set well above any realistic gate result so it only
# ever fires on a pathological path.
_MAX_GATED = 6000
# Resampled templates are cached per layout; past this many the cache is simply
# dropped and refilled, which bounds memory without any LRU bookkeeping.
_TPL_CACHE_MAX = 4000
# Coverage channel: the path is sampled this many times, and a letter counts as
# "visited" if any sample lands within this many key widths of its centre.
_COVER_SAMPLES = 28
_COVER_KEYS = 1.0
# Channel weights. Endpoints is (start miss + end miss) in key widths, ~0 for
# the right word and ~0.85 per drifted-to-a-neighbour end; coverage is the
# FRACTION of the word's distinct letters never visited (0..1); length is the
# relative difference between traced and ideal path length (0..1); shape is an
# RMS distance in unit-box units (a good match ~0.06, a bad one ~0.4); location
# is RMS in key widths (good ~0.4, bad ~2); the frequency term is log10 of the
# rank, 1.0 for the commonest words and ~4.5 at the tail  strong enough to
# settle exact shape ties, too weak to beat a clearly better-fitting curve.
_W_ENDS = 1.5
_W_COVER = 2.6
_W_LEN = 1.6
_W_SHAPE = 4.2
_W_LOCATION = 2.4
_W_FREQ = 0.20
# A path shorter than this many key widths is a tap or a wobble, not a word.
_MIN_PATH_KEYS = 1.5
# Candidates handed to the Windows predictor, and how much a hit there can move
# a candidate. Capped well below the gap between a good and a bad shape match:
# the system engine reorders near-ties, it never overrules the geometry.
_PREDICT_N = 12
_W_PREDICT = 0.55
# ...and the runner-up must be within this much of the winner before the engine
# is consulted at all. The call is a blocking cross-ABI hop costing tens of
# milliseconds  real money on the input thread  and it can only ever change
# the answer when two candidates are already close, so a decisive geometric win
# skips it entirely. Slightly below _W_PREDICT: outside this margin no bonus
# the engine can award would reorder anything anyway.
_PREDICT_MARGIN = 0.5

# The bundled lexicon is English, so the predictor is pinned to English too 
# mixing a differently-languaged re-ranker into an English candidate list would
# only ever add noise.
_PREDICT_LANG = "en-US"


# ---------------------------------------------------------------------------
# Key geometry (published by the render thread, read by the input thread)
# ---------------------------------------------------------------------------
# One immutable tuple rebound as a whole:
#   (centres, key_width, template_cache, pair_dist, idx_centres)
# Rebinding a module global is atomic under the GIL, so the reader either sees
# the entire old layout or the entire new one  never a half-updated map, and
# never a template cache that belongs to different geometry. That is why the
# cache travels INSIDE the tuple instead of being cleared in place.
_geom = None
_geom_sig = None


def set_key_geometry(virtual_kb):
    """Publish the live letter-key centres for the decoder. Called from the
    render loop every frame; returns immediately unless the layout actually
    changed (OSK resize, skin/layout rebuild)."""
    global _geom, _geom_sig
    try:
        # Key dimensions are derived from the window size, so they already
        # encode a resize  which lets this module stay free of an
        # adusk.screen import and keeps screen.py able to import IT.
        sig = (id(virtual_kb), virtual_kb.key_rows,
               round(virtual_kb.key_height, 3),
               round(virtual_kb.key_width[0], 3))
    except Exception:
        return
    if sig == _geom_sig:
        return
    centres = {}
    widths = []
    try:
        for lay in virtual_kb.gen_key_layouts():
            try:
                kb_key = virtual_kb.keys[lay.row][lay.col]
            except (IndexError, TypeError):
                continue
            label = getattr(kb_key, "str", "") or ""
            # Only the single-letter alphabetic keys can appear in a word.
            if len(label) != 1 or not (label.isascii() and label.isalpha()):
                continue
            centres[label.lower()] = (lay.x + lay.w / 2.0, lay.y + lay.h / 2.0)
            widths.append(lay.w)
    except Exception:
        return
    if not centres:
        # A page with no letter keys at all  the phone layout's symbol pages.
        # Publish NOTHING rather than returning early: leaving the previous
        # page's letters live would let a swipe here decode into a word built
        # from keys that are not on screen. The signature is still recorded, so
        # switching back to the letter page republishes normally.
        _geom = None
        _geom_sig = sig
        return
    # Letter index (a=0..z=25) -> centre, and the 26x26 centre-to-centre
    # distance table. Both let the hot loop work in integers off flat lists
    # instead of hashing single-character strings.
    idx_centres = [None] * 26
    for ch, c in centres.items():
        idx_centres[ord(ch) - 97] = c
    pair = [0.0] * 676
    for a in range(26):
        ca = idx_centres[a]
        if ca is None:
            continue
        for b in range(26):
            cb = idx_centres[b]
            if cb is None:
                continue
            dx = ca[0] - cb[0]
            dy = ca[1] - cb[1]
            pair[a * 26 + b] = (dx * dx + dy * dy) ** 0.5
    _geom = (centres, sum(widths) / len(widths), {}, pair, idx_centres)
    _geom_sig = sig


def key_width():
    """Average letter-key width in px, or 0.0 before any layout is published."""
    g = _geom
    return g[1] if g else 0.0


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------
_lex_lock = threading.Lock()
_lexicon = None       # {first letter: [entry, ...]} once loaded


def load_lexicon():
    """Load + index the bundled word list. Safe to call from any thread and
    from anywhere; the first call does the work, the rest is a dict read.

    Every word is pre-digested into the tuple the hot loop wants:

        (prior, word, run, mask, n_distinct)

    `prior` is log10(rank + 10), baked in so the hot path never calls
    math.log10 and sorting by prior IS sorting by rank. `run` is the word's
    letters as a tuple of 0..25 indices with CONSECUTIVE DUPLICATES REMOVED 
    a finger cannot trace "ll" as anything but a single visit, so the doubled
    letter is not part of the curve (which is exactly why the frequency prior
    has to exist). `mask` is the distinct-letter bitmask for the coverage
    channel."""
    global _lexicon
    lex = _lexicon
    if lex is not None:
        return lex
    with _lex_lock:
        if _lexicon is not None:
            return _lexicon
        by_first = {}
        path = resources.find_data_resource(os.path.join("lexicon", "en.txt"))
        if path is None:
            print("swipe: no bundled lexicon found; Swipe Typing is inert")
            _lexicon = by_first
            return by_first
        try:
            with open(path, encoding="utf-8") as fh:
                rank = 0
                for line in fh:
                    word = line.strip()
                    if len(word) < 2:
                        continue
                    run = []
                    mask = 0
                    prev = -1
                    bad = False
                    for ch in word:
                        i = ord(ch) - 97
                        if i < 0 or i > 25:
                            bad = True
                            break
                        mask |= 1 << i
                        if i != prev:
                            run.append(i)
                            prev = i
                    if bad or len(run) < 2:
                        continue
                    by_first.setdefault(word[0], []).append(
                        (math.log10(rank + 10), word, tuple(run), mask,
                         mask.bit_count()))
                    rank += 1
        except OSError as e:
            print(f"swipe: cannot read lexicon {path} ({e!r})")
        _lexicon = by_first
        return by_first


def warm_up():
    """Load the lexicon off the input thread so the first swipe isn't the one
    that pays for it. Fire-and-forget; failures are already swallowed."""
    threading.Thread(target=load_lexicon, name="swipe-warmup",
                     daemon=True).start()


# ---------------------------------------------------------------------------
# Curve maths
# ---------------------------------------------------------------------------
def _resample(pts, n):
    """Resample a polyline to exactly n points spaced equally ALONG ITS LENGTH
    (not along time). This is what makes a slow tracing and a fast flick of the
    same route compare identically, and it is also what lets a 5-letter and a
    9-letter template be compared point-for-point at all."""
    if not pts:
        return []
    if len(pts) == 1:
        return [pts[0]] * n
    seg = []
    total = 0.0
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        d = (dx * dx + dy * dy) ** 0.5
        seg.append(d)
        total += d
    if total <= 1e-9:
        return [pts[0]] * n
    step = total / (n - 1)
    out = [pts[0]]
    i = 0        # index of the segment currently being walked
    acc = 0.0    # path length up to the START of segment i
    for k in range(1, n):
        target = step * k
        while i < len(seg) - 1 and acc + seg[i] < target:
            acc += seg[i]
            i += 1
        t = (target - acc) / seg[i] if seg[i] > 1e-9 else 1.0
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        out.append((pts[i][0] + t * (pts[i + 1][0] - pts[i][0]),
                    pts[i][1] + t * (pts[i + 1][1] - pts[i][1])))
    return out


def _flat(pts):
    """[(x, y), ...] -> [x0, y0, x1, y1, ...] so the scoring loop indexes a
    plain float list instead of unpacking a tuple per point."""
    out = []
    for x, y in pts:
        out.append(x)
        out.append(y)
    return out


def _normalise_flat(pts):
    """Translate a resampled curve to its centroid and scale it so its longer
    bounding-box side is 1  the SHAPE channel  returned flat. Scaling by the
    LONGER side (not each axis independently) keeps the aspect ratio, which
    carries most of a word's identity; per-axis scaling would make every
    straight line identical."""
    n = len(pts)
    cx = cy = 0.0
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for x, y in pts:
        cx += x
        cy += y
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
    cx /= n
    cy /= n
    span = max_x - min_x
    if max_y - min_y > span:
        span = max_y - min_y
    if span <= 1e-9:
        return [0.0] * (n * 2)
    inv = 1.0 / span
    out = []
    for x, y in pts:
        out.append((x - cx) * inv)
        out.append((y - cy) * inv)
    return out


def _template(word, run, idx_centres, cache):
    """Cached (raw_flat, norm_flat) for a perfect tracing of `word`, built from
    its pre-deduplicated letter-index run."""
    hit = cache.get(word)
    if hit is not None:
        return hit
    pts = []
    for i in run:
        c = idx_centres[i]
        if c is None:
            cache[word] = False       # a letter this layout doesn't have
            return False
        pts.append(c)
    res = _resample(pts, _RESAMPLE_N)
    val = (_flat(res), _normalise_flat(res))
    if len(cache) >= _TPL_CACHE_MAX:
        cache.clear()
    cache[word] = val
    return val


def path_length(path):
    """Total traced length of a path in keyboard pixels."""
    total = 0.0
    for i in range(len(path) - 1):
        dx = path[i + 1][0] - path[i][0]
        dy = path[i + 1][1] - path[i][1]
        total += (dx * dx + dy * dy) ** 0.5
    return total


def is_swipe(path):
    """True if this touch travelled far enough across the keyboard to be a word
    gesture rather than a tap, a hover correction or a resting thumb. Measured
    in KEY WIDTHS so it scales with the OSK size setting."""
    g = _geom
    if not g or len(path) < 3:
        return False
    return path_length(path) >= _MIN_PATH_KEYS * g[1]


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
def decode(path, max_results=5):
    """Match a swipe path  a list of (x, y) in KEYBOARD pixel coordinates 
    against the lexicon. Returns [(word, cost), ...] best first (lower cost is
    better), or [] when the path cannot be a word."""
    g = _geom
    if not g:
        return []
    centres, kw, cache, pair, idx_centres = g
    if kw <= 0 or len(path) < 3:
        return []
    by_first = load_lexicon()
    if not by_first:
        return []

    res = _resample(path, _RESAMPLE_N)
    u_raw = _flat(res)
    u_norm = _normalise_flat(res)
    traced = path_length(path)
    inv_kw = 1.0 / kw

    # Which letters the path actually went near (coverage channel), as a
    # bitmask so a candidate's misses are one AND and one popcount. Walking
    # letters-outer / samples-inner lets the common case break early.
    cover_r2 = (_COVER_KEYS * kw) ** 2
    probe = _resample(path, _COVER_SAMPLES)
    touched_mask = 0
    for ch, (cx, cy) in centres.items():
        for px, py in probe:
            dx = cx - px
            dy = cy - py
            if dx * dx + dy * dy <= cover_r2:
                touched_mask |= 1 << (ord(ch) - 97)
                break

    # The two letters the user genuinely aimed at bound the search.
    sx, sy = path[0]
    ex, ey = path[-1]
    gate2 = (_GATE_KEYS * kw) ** 2
    first_ok = []
    end_d = {}
    start_d = {}
    for ch, (cx, cy) in centres.items():
        dx = cx - sx
        dy = cy - sy
        d2 = dx * dx + dy * dy
        if d2 <= gate2:
            first_ok.append(ch)
            start_d[ch] = d2 ** 0.5 * inv_kw
        dx = cx - ex
        dy = cy - ey
        d2 = dx * dx + dy * dy
        if d2 <= gate2:
            end_d[ch] = d2 ** 0.5 * inv_kw
    if not first_ok or not end_d:
        return []

    cands = []
    for ch in first_ok:
        head = start_d[ch]
        for entry in by_first.get(ch, ()):
            tail = end_d.get(entry[1][-1])
            if tail is not None:
                cands.append((entry, head + tail))
    if not cands:
        return []
    if len(cands) > _MAX_GATED:
        cands.sort(key=_entry_prior)
        del cands[_MAX_GATED:]

    # --- Pass 1: endpoints + coverage + length + frequency. No templates and
    # no resampling, so it is cheap enough to run on every gated word  which
    # is what lets a rare word survive on geometry instead of being cut for
    # being rare.
    coarse = []
    for entry, ends in cands:
        prior, word, run, mask, n_distinct = entry
        missed = (mask & ~touched_mask).bit_count()
        ideal = 0.0
        prev = run[0]
        for k in range(1, len(run)):
            cur = run[k]
            ideal += pair[prev * 26 + cur]
            prev = cur
        longer = ideal if ideal > traced else traced
        lendiff = abs(traced - ideal) / longer if longer > 1e-9 else 0.0
        coarse.append((_W_ENDS * ends
                       + _W_COVER * (missed / n_distinct)
                       + _W_LEN * lendiff
                       + _W_FREQ * prior,
                       ends, entry))
    if len(coarse) > _STAGE_B:
        coarse.sort(key=_first)
        del coarse[_STAGE_B:]

    # --- Pass 2: add the two curve channels on the survivors.
    n_inv = 1.0 / _RESAMPLE_N
    scored = []
    for base, ends, entry in coarse:
        word, run = entry[1], entry[2]
        tpl = _template(word, run, idx_centres, cache)
        if not tpl:
            continue
        t_raw, t_norm = tpl
        shape = 0.0
        loc = 0.0
        for i in range(0, _FLAT_N, 2):
            dx = u_norm[i] - t_norm[i]
            dy = u_norm[i + 1] - t_norm[i + 1]
            shape += dx * dx + dy * dy
            dx = u_raw[i] - t_raw[i]
            dy = u_raw[i + 1] - t_raw[i + 1]
            loc += dx * dx + dy * dy
        scored.append((base
                       + _W_SHAPE * (shape * n_inv) ** 0.5
                       + _W_LOCATION * (loc * n_inv) ** 0.5 * inv_kw,
                       word))
    if not scored:
        return []
    scored.sort(key=_first)
    best = [(w, c) for c, w in scored[:max_results]]
    return _rescore(best)


def _first(t):
    return t[0]


def _entry_prior(t):
    return t[0][0]


# ---------------------------------------------------------------------------
# Windows system prediction layer
# ---------------------------------------------------------------------------
# None = not tried yet, False = unavailable (never retried), else the generator.
_predictor = None


def _get_predictor():
    """The Windows touch-keyboard prediction engine, or None. Lazily imported
    exactly once  like big_picture.pause_media's SMTC, `winrt` is an optional
    dependency and its absence is a downgrade, not an error."""
    global _predictor
    if _predictor is not None:
        return _predictor or None
    try:
        try:
            from winrt.runtime import init_apartment
            init_apartment()
        except Exception:
            # Already initialised on this thread, or a build that does it
            # implicitly  either way the generator below is the real test.
            pass
        from winrt.windows.data.text import TextPredictionGenerator
        gen = TextPredictionGenerator(_PREDICT_LANG)
        if gen.language_available_but_not_installed:
            print("swipe: Windows prediction data for "
                  f"{_PREDICT_LANG} is not installed; using bundled ranking")
            _predictor = False
            return None
        _predictor = gen
        return gen
    except Exception as e:
        print(f"swipe: TextPredictionGenerator unavailable ({e!r}); "
              "using bundled ranking")
        _predictor = False
        return None


def _predict(word, count):
    """Candidate list from the system engine for `word`, lowercased. []
    whenever the engine isn't there or the call fails."""
    gen = _get_predictor()
    if gen is None:
        return []
    try:
        fn = getattr(gen, "get_candidates_with_max_count_async", None)
        op = (fn(word, count) if fn is not None
              else gen.get_candidates_async(word))
        return [str(s).lower() for s in op.get()]
    except Exception:
        return []


def _rescore(cands):
    """Let the system engine reorder near-ties among our shape candidates.

    Seeded with our OWN best guess: the engine corrects a word to its
    neighbours, so its reply is a frequency-and-personalisation-ordered
    neighbourhood of that guess, and any of our candidates appearing in it gets
    a bonus scaled by how highly the engine rated it. A candidate the engine
    doesn't mention is left exactly as the geometry scored it.

    Skipped outright when the geometry already won decisively  see
    _PREDICT_MARGIN. That keeps the blocking WinRT call off the overwhelming
    majority of decodes while still catching the ties it exists to settle."""
    if len(cands) < 2 or cands[1][1] - cands[0][1] > _PREDICT_MARGIN:
        return cands
    sugg = _predict(cands[0][0], _PREDICT_N)
    if not sugg:
        return cands
    pos = {}
    for i, s in enumerate(sugg):
        pos.setdefault(s, i)
    span = float(len(sugg))
    out = [(w, c - _W_PREDICT * (1.0 - pos[w] / span)) if w in pos else (w, c)
           for w, c in cands]
    out.sort(key=lambda wc: wc[1])
    return out
