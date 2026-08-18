"""A silent two-track media session, for the tutorial's Media Controls slide.

Why this exists: that slide asks for Volume Up, Volume Down, Next Song and
Play/Pause. Volume announces itself  the OS volume OSD moves. The two
TRANSPORT keys announce nothing at all unless something is already playing, and
if something IS playing it is the user's own music: the tour would be teaching
"Next Song" by skipping their track. Neither outcome is feedback.

So for exactly as long as that slide is up, the tour becomes the machine's
media player. It publishes a real System Media Transport Controls session
backed by a MediaPlayer playing SILENCE, which buys three things at once:

  * the media keys route HERE. Windows hands them to the session that most
    recently started playing, so from the moment the slide opens Next and
    Play/Pause hit this silent track instead of whatever was playing before 
    which keeps going, untouched, out of the way.
  * Windows shows its own media flyout on every press, with our cover art in
    it. That is the big feedback surface, and it is why the two tracks look
    nothing alike: skipping is only convincing if the picture changes.
  * the tour can read back what happened (which track, playing or paused) and
    mirror it on the slide itself  see Tutorial._art_media's now-playing card.

Two items in a MediaPlaybackList, not one item with a hand-written "next"
handler: with a MediaPlayer-backed session the MediaPlaybackCommandManager
decides whether NEXT is even offered, and with a single item it reports next as
unavailable  Windows then never delivers the key at all (verified: no
ButtonPressed, no CommandManager event). Two items with auto-repeat make Next
real, and the title/cover swap comes free from each item's own display
properties.

Everything WinRT happens on one dedicated MTA thread (_run). The tutorial only
ever reads the plain ints this module publishes, so no Tk callback ever touches
a WinRT object or a foreign apartment.

Windows-only by construction  SMTC is a WinRT API. Every failure path (no
projection packages, no audio endpoint, no MediaFoundation) leaves start()
returning False, and the slide falls back to static artwork.
"""

import os
import sys
import tempfile
import threading
import time
import wave

# Track metadata. The titles say "silent" out loud: the flyout is going to name
# the thing that just stole the media keys, and a user who sees an unfamiliar
# track name should be able to read it and understand immediately.
_TRACKS = (
    {"title": "Song one", "artist": "SteamlessInput Tutorial"},
    {"title": "Song two", "artist": "SteamlessInput Tutorial"},
)
_ALBUM = "Media Controls"

# Five minutes of silence per item. Long enough that a tour never reaches the
# end of one (which would auto-advance the list and swap the cover with nobody
# touching anything), short enough that the file stays small: 8 kHz 16-bit mono
# is 4.8 MB, and MediaFoundation resamples it to the endpoint rate for free.
_SILENCE_S = 300
_RATE = 8000

_COVER_PX = 512          # what Windows scales into the flyout thumbnail

# Published state, written ONLY by the worker thread and read by anyone. Plain
# ints/bools by design (see the module docstring).
#
# "gen" is which worker owns it. stop() no longer waits for the worker to die
# (the tutorial calls it from the Tk thread on a slide change, and a user
# clicking through the tour must not sit through a join), so a torn-down
# session can still be running its teardown while the NEXT one is coming up.
# Every write is gated on the writer still being the current generation, which
# stops a dying worker's "ready = False" from wiping a live session's card.
_state = {"ready": False, "track": 0, "playing": False, "gen": 0}
_lock = threading.Lock()
_gen = 0
_thread = None
_stop_evt = None
_ready_evt = None
_assets = None           # (wav_path, [cover_path, ...]) once built


def _asset_dir():
    d = os.path.join(tempfile.gettempdir(), "SteamlessInput")
    os.makedirs(d, exist_ok=True)
    return d


def _font(px):
    """The app's own face at `px`, or PIL's default if the bundled TTF isn't
    there. Resolved locally rather than through keybinds_picker so this module
    stays free of project imports (it is loaded from a WinRT worker thread)."""
    from PIL import ImageFont
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "data", "fonts", "PlusJakartaSans-Regular.ttf")
    try:
        return ImageFont.truetype(path, px)
    except Exception:
        return ImageFont.load_default()


def _write_silence(path):
    if os.path.isfile(path) and os.path.getsize(path) > _RATE * 2 * _SILENCE_S:
        return path
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(b"\0" * (_RATE * 2 * _SILENCE_S))
    return path


def _gradient(draw, w, h, top, bottom):
    for y in range(h):
        t = y / float(h - 1)
        draw.line((0, y, w, y),
                  fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))


def _draw_cover(idx, path):
    """One album cover. The two are deliberately unalike in BOTH hue and
    geometry  the flyout thumbnail is tiny, and "the picture changed" has to
    survive being 40 pixels wide. The big numeral is the belt-and-braces
    version of that: unmistakable even at a glance, which is the whole job."""
    from PIL import Image, ImageDraw
    px = _COVER_PX
    img = Image.new("RGB", (px, px))
    d = ImageDraw.Draw(img, "RGBA")
    if idx == 0:
        # Navy -> cyan, with a vinyl record's concentric rings.
        _gradient(d, px, px, (11, 18, 32), (14, 110, 160))
        cx = cy = px // 2
        for i, r in enumerate((px * 0.40, px * 0.31, px * 0.22, px * 0.13)):
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255,
                      90 if i % 2 else 150), width=max(2, px // 90))
        r = px * 0.05
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 230))
    else:
        # Plum -> gold, with an equaliser. Nothing about it rhymes with the
        # rings above, which is the point.
        _gradient(d, px, px, (58, 16, 66), (226, 128, 42))
        bars = (0.30, 0.62, 0.44, 0.86, 0.52, 0.70)
        bw = px * 0.085
        gap = (px - bw * len(bars)) / float(len(bars) + 1)
        x = gap
        for frac in bars:
            top = px * (0.86 - 0.62 * frac)
            d.rounded_rectangle((x, top, x + bw, px * 0.86),
                                radius=bw * 0.35, fill=(255, 255, 255, 225))
            x += bw + gap
    f = _font(int(px * 0.30))
    d.text((px * 0.07, px * 0.02), str(idx + 1), font=f,
           fill=(255, 255, 255, 235))
    img.save(path)
    return path


def _build_assets():
    global _assets
    if _assets is not None:
        return _assets
    d = _asset_dir()
    wav = _write_silence(os.path.join(d, "tutorial_silence.wav"))
    covers = []
    for i in range(len(_TRACKS)):
        p = os.path.join(d, "tutorial_cover_%d.png" % (i + 1))
        if not os.path.isfile(p):
            _draw_cover(i, p)
        covers.append(p)
    _assets = (wav, covers)
    return _assets


def cover_path(idx):
    """The cover PNG for a track, so the slide can draw the SAME art the
    Windows flyout is showing. Built on demand: the slide asks for this before
    (and whether or not) the session itself ever comes up."""
    try:
        return _build_assets()[1][idx % len(_TRACKS)]
    except Exception as e:
        print(f"media demo: cover art failed: {e!r}")
        return None


def track_title(idx):
    return _TRACKS[idx % len(_TRACKS)]["title"]


def state():
    """(track index, is playing) when the session is up, else None."""
    with _lock:
        if not _state["ready"]:
            return None
        return _state["track"], _state["playing"]


def start(timeout=0.0):
    """Claim the media keys with the silent session. Idempotent.

    Returns True only if the session is ALREADY live  with the default
    timeout it does not wait for one to come up. Bringing a WinRT session up
    took the better part of a second on the machine this was written on and is
    allowed six by _await_playing, and the caller is the Tk thread mid-slide-
    change: waiting there froze the whole tour on that slide. The tutorial
    draws a static fallback until the session reports ready and repaints when
    it does (see _poll_media), so "not yet" is a state it already handles.

    Pass a timeout to wait anyway."""
    global _thread, _stop_evt, _ready_evt, _gen
    if sys.platform != "win32":
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _state["ready"]
        _gen += 1
        gen = _gen
        _stop_evt = threading.Event()
        _ready_evt = threading.Event()
        _state.update({"ready": False, "track": 0, "playing": False,
                       "gen": gen})
        _thread = threading.Thread(target=_run,
                                   args=(_stop_evt, _ready_evt, gen),
                                   name="tutorial-media", daemon=True)
        _thread.start()
        ready = _ready_evt
    if timeout:
        ready.wait(timeout)
    with _lock:
        return _state["ready"]


def stop():
    """Give the media keys back. Safe to call when nothing was ever started 
    every tutorial exit path funnels through here (see _settle_slide).

    Does NOT wait for the worker to finish tearing down. It is called from the
    Tk thread on a slide change, and the join it used to do put a fifth of a
    second (allowed two) into every exit from the media slide  paid by
    anyone clicking past it. The worker is a daemon that only has a pause and
    a close left to do, it owns its own MediaPlayer (so its teardown can't
    disturb a session started after it), and the generation bump here makes
    any state it still writes be ignored  see _state."""
    global _thread, _gen
    with _lock:
        _thread, evt = None, _stop_evt
        _gen += 1
        _state.update({"ready": False, "track": 0, "playing": False,
                       "gen": _gen})
    if evt is not None:
        evt.set()


def _publish(gen, **kw):
    """Write published state, but only while THIS worker still owns it (see
    _state's "gen"). A stopped worker finishing its teardown must not touch
    what its replacement has already put there."""
    with _lock:
        if _state.get("gen") != gen:
            return False
        _state.update(kw)
        return True


def _run(stop_evt, ready_evt, gen=0):
    """The whole WinRT lifetime, on one MTA thread."""
    player = None
    try:
        from winrt.runtime import init_apartment, uninit_apartment, MTA
    except Exception as e:
        print(f"media demo: WinRT unavailable: {e!r}")
        ready_evt.set()
        return
    try:
        init_apartment(MTA)
    except Exception as e:
        print(f"media demo: apartment init failed: {e!r}")
        ready_evt.set()
        return
    try:
        player, plist = _build_session()
        # Don't report ready until the track is actually PLAYING. play() only
        # asks  MediaFoundation still has to open the file  and reporting
        # ready a moment early means the slide paints its now-playing card in
        # the paused state and then flips it a heartbeat later, which reads as
        # the card changing under the user's eyes the instant it appears.
        _await_playing(player, stop_evt)
        _publish(gen, ready=True)
        ready_evt.set()
        _pump(player, plist, stop_evt, gen)
    except Exception as e:
        print(f"media demo: session failed: {e!r}")
        ready_evt.set()
    finally:
        _publish(gen, ready=False)
        try:
            if player is not None:
                player.pause()
                player.close()      # drops the SMTC session with it
        except Exception as e:
            print(f"media demo: teardown: {e!r}")
        try:
            uninit_apartment()
        except Exception:
            pass


def _build_session():
    from winrt.windows.foundation import Uri
    from winrt.windows.media import MediaPlaybackType
    from winrt.windows.media.core import MediaSource
    from winrt.windows.media.playback import (MediaPlaybackItem,
                                              MediaPlaybackList, MediaPlayer)
    from winrt.windows.storage import StorageFile
    from winrt.windows.storage.streams import RandomAccessStreamReference

    wav, covers = _build_assets()
    uri = Uri("file:///" + wav.replace("\\", "/"))

    def item(i):
        it = MediaPlaybackItem(MediaSource.create_from_uri(uri))
        props = it.get_display_properties()
        props.type = MediaPlaybackType.MUSIC
        props.music_properties.title = _TRACKS[i]["title"]
        props.music_properties.artist = _TRACKS[i]["artist"]
        props.music_properties.album_title = _ALBUM
        f = StorageFile.get_file_from_path_async(covers[i]).get()
        props.thumbnail = RandomAccessStreamReference.create_from_file(f)
        # apply_display_properties, not the SMTC DisplayUpdater: a
        # MediaPlayer-driven session refreshes the updater from the CURRENT
        # ITEM whenever it changes, so anything written straight to the
        # updater is overwritten the moment playback opens (verified: the
        # title came back empty from another process).
        it.apply_display_properties(props)
        return it

    plist = MediaPlaybackList()
    plist.auto_repeat_enabled = True       # Next off the last track wraps
    for i in range(len(_TRACKS)):
        plist.items.append(item(i))

    player = MediaPlayer()
    player.auto_play = False
    # Silence twice over: the file is zeroes AND the player is muted, so a
    # broken asset can never put a noise on someone's speakers mid-tutorial.
    player.volume = 0.0
    player.is_muted = True
    player.source = plist
    smtc = player.system_media_transport_controls
    smtc.is_enabled = True
    player.play()
    return player, plist


def _await_playing(player, stop_evt, timeout=2.0):
    """Wait (briefly) for playback to actually start, publishing the state as
    it goes. Bounded: a machine with no audio endpoint never gets there, and
    the slide is better off with a card that says Paused than with no card."""
    from winrt.windows.media.playback import MediaPlaybackState
    deadline = time.monotonic() + timeout
    while not stop_evt.is_set() and time.monotonic() < deadline:
        try:
            if (player.playback_session.playback_state
                    == MediaPlaybackState.PLAYING):
                with _lock:
                    _state["playing"] = True
                return True
        except Exception:
            return False
        stop_evt.wait(0.05)
    return False


def _pump(player, plist, stop_evt, gen=0):
    """Publish (track, playing) until told to stop. Polled rather than event-
    driven on purpose: the state we want is two properties, the events arrive
    on threadpool threads in an order that isn't worth reasoning about, and
    100 ms is well inside "instant" for a checklist that repaints at 150."""
    from winrt.windows.media.playback import MediaPlaybackState
    while not stop_evt.is_set():
        try:
            idx = plist.current_item_index
            if idx is None or idx >= len(_TRACKS):
                idx = 0          # unset reads back as UINT32_MAX
            playing = (player.playback_session.playback_state
                       == MediaPlaybackState.PLAYING)
            if not _publish(gen, track=int(idx), playing=bool(playing)):
                return          # superseded by a newer session
        except Exception as e:
            print(f"media demo: state read failed: {e!r}")
            return
        stop_evt.wait(0.1)
