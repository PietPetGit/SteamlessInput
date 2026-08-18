"""A silent two-track media session, for the tutorial's Media Controls slide.

Why this exists: that slide asks for Volume Up, Volume Down, Next Song and
Play/Pause. Volume announces itself  the desktop's volume OSD moves. The two
TRANSPORT keys announce nothing at all unless something is already playing, and
if something IS playing it is the user's own music: the tour would be teaching
"Next Song" by skipping their track. Neither outcome is feedback.

So for exactly as long as that slide is up, the tour becomes a media player.

PLATFORM FORK  this is the Linux half of windows/media_demo.py, and it is a
hard fork rather than a port, because the mechanism is different in kind:

  * Windows: a System Media Transport Controls session, which only exists as
    the shadow of something ACTUALLY PLAYING. That side therefore plays five
    minutes of silence through a MediaPlayer to have a session at all.
  * Linux: MPRIS2  a D-Bus name and two interfaces. The desktop's media-key
    handler (KDE, GNOME, Sway's playerctl, ...) routes KEY_NEXTSONG and friends
    to an MPRIS player directly, and metadata is just a dict. No audio is
    involved, so none is played: "silent" here is literal.

What the two halves share is the contract the tutorial sees  start(), stop(),
state(), cover_path(), track_title()  and the artwork, so the same two covers
identify the same two tracks on both platforms. The covers are the point of the
exercise: "Next Song" is only convincing when something visibly becomes another
song, and mpris:artUrl is what puts that picture in the desktop's own OSD.

The D-Bus service runs on one dedicated thread with its own GLib main loop
(_run). The tutorial only ever reads the plain ints published here.

UNVERIFIED ON HARDWARE: written alongside the Windows side, and the whole
module is wrapped so that no D-Bus session bus, no PyGObject, or a name that is
already taken all leave start() returning False and the slide falling back to
its static artwork.
"""

import os
import sys
import tempfile
import threading

# Track metadata. The titles say "silent" out loud: the OSD is going to name the
# thing that just took the media keys, and a user who sees an unfamiliar track
# name should be able to read it and understand immediately.
_TRACKS = (
    {"title": "Song one", "artist": "SteamlessInput Tutorial"},
    {"title": "Song two", "artist": "SteamlessInput Tutorial"},
)
_ALBUM = "Media Controls"

_COVER_PX = 512          # what the desktop scales into its OSD thumbnail

_BUS_NAME = "org.mpris.MediaPlayer2.SteamlessInput"
_OBJ_PATH = "/org/mpris/MediaPlayer2"
_IFACE_ROOT = "org.mpris.MediaPlayer2"
_IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"

# Only what a media-key press can reach, plus the properties every MPRIS client
# reads before it will believe the player exists.
_INTROSPECTION = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

# Published state, written ONLY by the worker thread and read by anyone. Plain
# ints/bools by design (see the module docstring).
# "gen" is which worker owns this state. stop() no longer waits for the worker
# to die (the tutorial calls it from the Tk thread on a slide change, and a
# user clicking through the tour must not sit through a join), so a torn-down
# session can still be finishing while the NEXT one comes up. Every write is
# gated on the writer still being the current generation, which stops a dying
# worker's "ready = False" from wiping a live session's card.
_state = {"ready": False, "track": 0, "playing": False, "gen": 0}
_lock = threading.Lock()
_gen = 0
_thread = None
_stop_evt = None
_ready_evt = None
_loop = None             # GLib.MainLoop, so stop() can quit it
_assets = None           # [cover_path, ...] once built


def _asset_dir():
    d = os.path.join(tempfile.gettempdir(), "SteamlessInput")
    os.makedirs(d, exist_ok=True)
    return d


def _font(px):
    """The app's own face at `px`, or PIL's default if the bundled TTF isn't
    there. Resolved locally rather than through keybinds_picker so this module
    stays free of project imports (it is loaded from a worker thread)."""
    from PIL import ImageFont
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "data", "fonts", "PlusJakartaSans-Regular.ttf")
    try:
        return ImageFont.truetype(path, px)
    except Exception:
        return ImageFont.load_default()


def _gradient(draw, w, h, top, bottom):
    for y in range(h):
        t = y / float(h - 1)
        draw.line((0, y, w, y),
                  fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))


def _draw_cover(idx, path):
    """One album cover. The two are deliberately unalike in BOTH hue and
    geometry  an OSD thumbnail is tiny, and "the picture changed" has to
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
    covers = []
    for i in range(len(_TRACKS)):
        p = os.path.join(d, "tutorial_cover_%d.png" % (i + 1))
        if not os.path.isfile(p):
            _draw_cover(i, p)
        covers.append(p)
    _assets = covers
    return _assets


def cover_path(idx):
    """The cover PNG for a track, so the slide can draw the SAME art the
    desktop's OSD is showing. Built on demand: the slide asks for this before
    (and whether or not) the session itself ever comes up."""
    try:
        return _build_assets()[idx % len(_TRACKS)]
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
    timeout it does not wait for one to come up. Claiming the bus name takes
    long enough to be felt, and the caller is the Tk thread mid-slide-change:
    waiting there froze the whole tour on that slide. The tutorial draws a
    static fallback until the session reports ready and repaints when it does
    (see _poll_media), so "not yet" is a state it already handles.

    Pass a timeout to wait anyway."""
    global _thread, _stop_evt, _ready_evt, _gen
    if sys.platform == "win32":
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

    Does NOT wait for the worker to finish. It is called from the Tk thread on
    a slide change, and the join it used to do was paid by anyone clicking
    past the media slide. The worker is a daemon whose main loop has just been
    told to quit, it owns its own bus registrations, and the generation bump
    here makes any state it still writes be ignored  see _state."""
    global _thread, _gen
    with _lock:
        evt, loop = _stop_evt, _loop
        _thread = None
        _gen += 1
        _state.update({"ready": False, "track": 0, "playing": False,
                       "gen": _gen})
    if evt is not None:
        evt.set()
    if loop is not None:
        try:
            loop.quit()
        except Exception:
            pass


def _publish_state(gen, **kw):
    """Write published state, but only while THIS worker still owns it (see
    _state's "gen"). A stopped worker finishing its teardown must not touch
    what its replacement has already put there."""
    with _lock:
        if _state.get("gen") != gen:
            return False
        _state.update(kw)
        return True


def _run(stop_evt, ready_evt, gen=0):
    """The whole D-Bus lifetime, on one thread with its own main loop."""
    global _loop
    conn = reg_root = reg_player = owner_id = None
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import GLib, Gio
    except Exception as e:
        print(f"media demo: PyGObject unavailable: {e!r}")
        ready_evt.set()
        return
    try:
        _build_assets()
        node = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION)
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        srv = _Service(conn, node, gen)
        reg_root = conn.register_object(
            _OBJ_PATH, node.interfaces[0], srv.call, srv.get, srv.set)
        reg_player = conn.register_object(
            _OBJ_PATH, node.interfaces[1], srv.call, srv.get, srv.set)
        # ...and only claim the well-known name once the object answers, so a
        # media-key press can never arrive before there is anything to answer
        # it. DO_NOT_QUEUE: if some other SteamlessInput already owns it, fail
        # outright rather than silently waiting behind it.
        owner_id = Gio.bus_own_name_on_connection(
            conn, _BUS_NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, None)
        _publish_state(gen, ready=True)
        ready_evt.set()
        _loop = GLib.MainLoop()
        _loop.run()
    except Exception as e:
        print(f"media demo: session failed: {e!r}")
        ready_evt.set()
    finally:
        _loop = None
        _publish_state(gen, ready=False)
        try:
            if owner_id is not None:
                from gi.repository import Gio as _Gio
                _Gio.bus_unown_name(owner_id)
            for rid in (reg_root, reg_player):
                if rid:
                    conn.unregister_object(rid)
        except Exception as e:
            print(f"media demo: teardown: {e!r}")


class _Service:
    """The MPRIS object itself: answers the handful of methods a media key can
    produce, and republishes Metadata/PlaybackStatus so the desktop's OSD
    follows along."""

    def __init__(self, conn, node, gen=0):
        self._conn = conn
        self._node = node
        self._gen = gen

    # -- state ---------------------------------------------------------------
    def _publish(self, track, playing):
        if not _publish_state(self._gen, track=int(track) % len(_TRACKS),
                              playing=bool(playing)):
            return          # superseded by a newer session
        self._changed()

    def _read(self):
        with _lock:
            return _state["track"], _state["playing"]

    def _metadata(self):
        from gi.repository import GLib
        track, _playing = self._read()
        art = cover_path(track)
        d = {
            "mpris:trackid": GLib.Variant(
                "o", "/org/mpris/MediaPlayer2/Track/%d" % (track + 1)),
            "mpris:length": GLib.Variant("x", 0),
            "xesam:title": GLib.Variant("s", _TRACKS[track]["title"]),
            "xesam:artist": GLib.Variant("as", [_TRACKS[track]["artist"]]),
            "xesam:album": GLib.Variant("s", _ALBUM),
        }
        if art:
            d["mpris:artUrl"] = GLib.Variant("s", "file://" + art)
        return GLib.Variant("a{sv}", d)

    def _changed(self):
        """PropertiesChanged for the two things that move. Emitted by hand:
        Gio's register_object does not watch the values behind get()."""
        from gi.repository import GLib
        _track, playing = self._read()
        body = GLib.Variant(
            "(sa{sv}as)",
            (_IFACE_PLAYER,
             {"PlaybackStatus": GLib.Variant(
                 "s", "Playing" if playing else "Paused"),
              "Metadata": self._metadata()},
             []))
        try:
            self._conn.emit_signal(None, _OBJ_PATH,
                                   "org.freedesktop.DBus.Properties",
                                   "PropertiesChanged", body)
        except Exception as e:
            print(f"media demo: PropertiesChanged failed: {e!r}")

    # -- D-Bus ---------------------------------------------------------------
    def call(self, _conn, _sender, _path, iface, method, _params, inv):
        track, playing = self._read()
        if iface == _IFACE_PLAYER:
            if method == "Next":
                self._publish(track + 1, True)
            elif method == "Previous":
                self._publish(track - 1, True)
            elif method == "PlayPause":
                self._publish(track, not playing)
            elif method == "Play":
                self._publish(track, True)
            elif method in ("Pause", "Stop"):
                self._publish(track, False)
        inv.return_value(None)

    def get(self, _conn, _sender, _path, iface, prop):
        from gi.repository import GLib
        track, playing = self._read()
        if iface == _IFACE_ROOT:
            return {
                "CanQuit": GLib.Variant("b", False),
                "CanRaise": GLib.Variant("b", False),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", "SteamlessInput Tutorial"),
                "SupportedUriSchemes": GLib.Variant("as", []),
                "SupportedMimeTypes": GLib.Variant("as", []),
            }.get(prop)
        if prop == "PlaybackStatus":
            return GLib.Variant("s", "Playing" if playing else "Paused")
        if prop == "Metadata":
            return self._metadata()
        if prop == "Volume":
            return GLib.Variant("d", 0.0)
        if prop == "Position":
            return GLib.Variant("x", 0)
        # CanGoNext/CanGoPrevious/CanPlay/CanPause/CanControl are all True 
        # a desktop that reads CanGoNext as False will not send us the key at
        # all, which is the Linux twin of the Windows CommandManager problem
        # (see windows/media_demo.py's note on why the list has two items).
        if prop == "CanSeek":
            return GLib.Variant("b", False)
        return GLib.Variant("b", True)

    def set(self, _conn, _sender, _path, _iface, _prop, _value):
        return False           # nothing here is writable; Volume is decorative
