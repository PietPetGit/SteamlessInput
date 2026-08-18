"""Thin hand-rolled ctypes binding for the subset of SDL3 + SDL3_ttf this
project uses: core/video/window, renderer, surface+texture, TTF text, events,
and the gamepad API.

SDL3 renamed/re-signatured much of SDL2  the notable ones handled here:
  * SDL_CreateRGBSurfaceWithFormatFrom -> SDL_CreateSurfaceFrom (w,h,format,px,pitch)
  * SDL_FreeSurface                    -> SDL_DestroySurface
  * SDL_RenderCopy (int SDL_Rect)      -> SDL_RenderTexture (float SDL_FRect)
  * SDL_GetWindowWMInfo                -> window properties (win32 HWND pointer)
  * SDL_CreateWindow drops x,y         -> position set via SDL_SetWindowPosition
  * SDL_ScaleModeLinear                -> SDL_SCALEMODE_LINEAR
Window flags are Uint64 in SDL3, and most functions return bool (true=success).
"""

import ctypes

from sdl3w import _loader

SDL, TTF, DLL_DIR = _loader.load()

SDL3_VERSION = _loader.SDL3_VERSION
SDL3_TTF_VERSION = _loader.SDL3_TTF_VERSION


def _bind(lib, name, restype, argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


# ===========================================================================
# Types
# ===========================================================================
SDL_JoystickID = ctypes.c_uint32
SDL_DisplayID = ctypes.c_uint32
SDL_PropertiesID = ctypes.c_uint32


class SDL_Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]


class SDL_FRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float),
                ("w", ctypes.c_float), ("h", ctypes.c_float)]


class SDL_FPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


class SDL_Color(ctypes.Structure):
    _fields_ = [("r", ctypes.c_ubyte), ("g", ctypes.c_ubyte),
                ("b", ctypes.c_ubyte), ("a", ctypes.c_ubyte)]


class SDL_Surface(ctypes.Structure):
    # SDL3 layout  we only read w/h (both precede the first pointer field, so
    # alignment padding after them is irrelevant).
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("format", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
        ("pitch", ctypes.c_int),
        ("pixels", ctypes.c_void_p),
        ("refcount", ctypes.c_int),
        ("reserved", ctypes.c_void_p),
    ]


# ===========================================================================
# Constants
# ===========================================================================
# SDL_InitFlags
SDL_INIT_VIDEO = 0x00000020
SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMEPAD = 0x00002000
SDL_INIT_EVENTS = 0x00004000

# SDL_WindowFlags (Uint64)
SDL_WINDOW_HIDDEN = 0x0000000000000008
SDL_WINDOW_BORDERLESS = 0x0000000000000010
SDL_WINDOW_ALWAYS_ON_TOP = 0x0000000000000100
SDL_WINDOW_UTILITY = 0x0000000000020000
SDL_WINDOW_NOT_FOCUSABLE = 0x0000000000080000
# Per-pixel-alpha (layered/composited) window  lets the OSK render with a
# transparent background and translucent keys (see screen.Screen + skins).
SDL_WINDOW_TRANSPARENT = 0x0000000040000000

SDL_WINDOWPOS_CENTERED = 0x2FFF0000

# Win32 HWND window property name.
SDL_PROP_WINDOW_WIN32_HWND_POINTER = b"SDL.window.win32.hwnd"

# Pixel format (same packed value as SDL2): ABGR8888 matches Pillow RGBA bytes
# on little-endian.
SDL_PIXELFORMAT_ABGR8888 = 0x16762004

# Scale / blend modes
SDL_SCALEMODE_NEAREST = 0
SDL_SCALEMODE_LINEAR = 1
SDL_BLENDMODE_NONE = 0x00000000
SDL_BLENDMODE_BLEND = 0x00000001
# Straight-alpha BLEND re-multiplies a texture's RGB by its alpha (and the
# alpha-mod) again at composite time; for a texture rendered onto a cleared
# (0,0,0,0) target with BLEND (which leaves it holding PREMULTIPLIED RGB 
# SDL's well-known render-to-transparent-texture quirk), that double-applies
# the alpha and darkens it. PREMULTIPLIED skips the redundant RGB multiply 
# used for the OSK open-animation's offscreen composite (see
# screen._ensure_anim_target).
SDL_BLENDMODE_BLEND_PREMULTIPLIED = 0x00000010

# SDL_TextureAccess  TARGET makes a texture usable as a render target
# (SDL_SetRenderTarget), e.g. the OSK open-animation offscreen buffer.
SDL_TEXTUREACCESS_STATIC = 0
SDL_TEXTUREACCESS_STREAMING = 1
SDL_TEXTUREACCESS_TARGET = 2

# Event types
SDL_EVENT_QUIT = 0x100
SDL_EVENT_WINDOW_RESIZED = 0x206
SDL_EVENT_MOUSE_MOTION = 0x400
SDL_EVENT_MOUSE_BUTTON_DOWN = 0x401
SDL_EVENT_MOUSE_BUTTON_UP = 0x402

# Mouse buttons + button mask
SDL_BUTTON_LEFT = 1
SDL_BUTTON_MIDDLE = 2
SDL_BUTTON_RIGHT = 3
SDL_BUTTON_X1 = 4
SDL_BUTTON_X2 = 5
SDL_BUTTON_LMASK = 1 << (SDL_BUTTON_LEFT - 1)
SDL_BUTTON_MMASK = 1 << (SDL_BUTTON_MIDDLE - 1)


def button_mask(button):
    """SDL_BUTTON_MASK: the SDL_MouseMotionEvent.state bit for a button index.
    Lets a held-button check be written against whichever button started a
    gesture (e.g. the OSK's free window drag) instead of a fixed one."""
    return 1 << (button - 1)

# Hints
SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN = b"SDL_WINDOW_ACTIVATE_WHEN_SHOWN"


# ===========================================================================
# Event structs / union
# ===========================================================================
class SDL_CommonEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                ("timestamp", ctypes.c_uint64)]


class SDL_WindowEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                ("timestamp", ctypes.c_uint64), ("windowID", ctypes.c_uint32),
                ("data1", ctypes.c_int32), ("data2", ctypes.c_int32)]


class SDL_MouseMotionEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                ("timestamp", ctypes.c_uint64), ("windowID", ctypes.c_uint32),
                ("which", ctypes.c_uint32), ("state", ctypes.c_uint32),
                ("x", ctypes.c_float), ("y", ctypes.c_float),
                ("xrel", ctypes.c_float), ("yrel", ctypes.c_float)]


class SDL_MouseButtonEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                ("timestamp", ctypes.c_uint64), ("windowID", ctypes.c_uint32),
                ("which", ctypes.c_uint32), ("button", ctypes.c_ubyte),
                ("down", ctypes.c_bool), ("clicks", ctypes.c_ubyte),
                ("padding", ctypes.c_ubyte), ("x", ctypes.c_float),
                ("y", ctypes.c_float)]


class SDL_Event(ctypes.Union):
    _fields_ = [("type", ctypes.c_uint32),
                ("common", SDL_CommonEvent),
                ("window", SDL_WindowEvent),
                ("motion", SDL_MouseMotionEvent),
                ("button", SDL_MouseButtonEvent),
                ("padding", ctypes.c_ubyte * 128)]


# ===========================================================================
# Core
# ===========================================================================
SDL_Init = _bind(SDL, "SDL_Init", ctypes.c_bool, [ctypes.c_uint32])
SDL_InitSubSystem = _bind(SDL, "SDL_InitSubSystem", ctypes.c_bool, [ctypes.c_uint32])
SDL_QuitSubSystem = _bind(SDL, "SDL_QuitSubSystem", None, [ctypes.c_uint32])
SDL_Quit = _bind(SDL, "SDL_Quit", None, [])
SDL_GetError = _bind(SDL, "SDL_GetError", ctypes.c_char_p, [])
SDL_GetVersion = _bind(SDL, "SDL_GetVersion", ctypes.c_int, [])
SDL_free = _bind(SDL, "SDL_free", None, [ctypes.c_void_p])
SDL_SetHint = _bind(SDL, "SDL_SetHint", ctypes.c_bool, [ctypes.c_char_p, ctypes.c_char_p])


def get_error():
    err = SDL_GetError()
    return err.decode("utf-8", "replace") if err else ""


def version_tuple():
    v = SDL_GetVersion()
    return (v // 1000000, (v // 1000) % 1000, v % 1000)


# ===========================================================================
# Video / window
# ===========================================================================
SDL_GetPrimaryDisplay = _bind(SDL, "SDL_GetPrimaryDisplay", SDL_DisplayID, [])
SDL_GetDisplayUsableBounds = _bind(SDL, "SDL_GetDisplayUsableBounds", ctypes.c_bool,
                                   [SDL_DisplayID, ctypes.POINTER(SDL_Rect)])
# Every connected display, and each one's FULL bounds (usable bounds above
# exclude the taskbar). Together they give the virtual desktop the OSK's free
# mouse drag is clamped against, so the keyboard can be dragged to any monitor.
# The returned array is SDL-allocated  SDL_free it.
SDL_GetDisplays = _bind(SDL, "SDL_GetDisplays", ctypes.POINTER(SDL_DisplayID),
                        [ctypes.POINTER(ctypes.c_int)])
SDL_GetDisplayBounds = _bind(SDL, "SDL_GetDisplayBounds", ctypes.c_bool,
                             [SDL_DisplayID, ctypes.POINTER(SDL_Rect)])
SDL_CreateWindow = _bind(SDL, "SDL_CreateWindow", ctypes.c_void_p,
                         [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint64])
SDL_DestroyWindow = _bind(SDL, "SDL_DestroyWindow", None, [ctypes.c_void_p])
SDL_SetWindowPosition = _bind(SDL, "SDL_SetWindowPosition", ctypes.c_bool,
                              [ctypes.c_void_p, ctypes.c_int, ctypes.c_int])
SDL_GetWindowPosition = _bind(SDL, "SDL_GetWindowPosition", ctypes.c_bool,
                              [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                               ctypes.POINTER(ctypes.c_int)])
SDL_SetWindowSize = _bind(SDL, "SDL_SetWindowSize", ctypes.c_bool,
                          [ctypes.c_void_p, ctypes.c_int, ctypes.c_int])
SDL_ShowWindow = _bind(SDL, "SDL_ShowWindow", ctypes.c_bool, [ctypes.c_void_p])
SDL_HideWindow = _bind(SDL, "SDL_HideWindow", ctypes.c_bool, [ctypes.c_void_p])
SDL_SetWindowAlwaysOnTop = _bind(SDL, "SDL_SetWindowAlwaysOnTop", ctypes.c_bool,
                                 [ctypes.c_void_p, ctypes.c_bool])
SDL_GetWindowProperties = _bind(SDL, "SDL_GetWindowProperties", SDL_PropertiesID,
                                [ctypes.c_void_p])
SDL_GetPointerProperty = _bind(SDL, "SDL_GetPointerProperty", ctypes.c_void_p,
                               [SDL_PropertiesID, ctypes.c_char_p, ctypes.c_void_p])


def get_win32_hwnd(window):
    """Return the Win32 HWND (as an int) backing an SDL window, or None."""
    props = SDL_GetWindowProperties(window)
    if not props:
        return None
    hwnd = SDL_GetPointerProperty(props, SDL_PROP_WINDOW_WIN32_HWND_POINTER, None)
    return int(hwnd) if hwnd else None


# ===========================================================================
# Renderer
# ===========================================================================
SDL_CreateRenderer = _bind(SDL, "SDL_CreateRenderer", ctypes.c_void_p,
                           [ctypes.c_void_p, ctypes.c_char_p])
SDL_DestroyRenderer = _bind(SDL, "SDL_DestroyRenderer", None, [ctypes.c_void_p])
SDL_SetRenderDrawColor = _bind(SDL, "SDL_SetRenderDrawColor", ctypes.c_bool,
                               [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte,
                                ctypes.c_ubyte, ctypes.c_ubyte])
SDL_SetRenderDrawBlendMode = _bind(SDL, "SDL_SetRenderDrawBlendMode", ctypes.c_bool,
                                   [ctypes.c_void_p, ctypes.c_uint])
SDL_RenderClear = _bind(SDL, "SDL_RenderClear", ctypes.c_bool, [ctypes.c_void_p])
# Clip rendering to a rect (int SDL_Rect); pass NULL/None to disable clipping.
SDL_SetRenderClipRect = _bind(SDL, "SDL_SetRenderClipRect", ctypes.c_bool,
                              [ctypes.c_void_p, ctypes.POINTER(SDL_Rect)])
SDL_RenderFillRect = _bind(SDL, "SDL_RenderFillRect", ctypes.c_bool,
                           [ctypes.c_void_p, ctypes.POINTER(SDL_FRect)])
SDL_RenderLine = _bind(SDL, "SDL_RenderLine", ctypes.c_bool,
                       [ctypes.c_void_p, ctypes.c_float, ctypes.c_float,
                        ctypes.c_float, ctypes.c_float])
# One connected polyline per call  the swipe trail is hundreds of segments a
# frame, which as individual SDL_RenderLine calls would cost more in ctypes
# marshalling than in drawing.
SDL_RenderLines = _bind(SDL, "SDL_RenderLines", ctypes.c_bool,
                        [ctypes.c_void_p, ctypes.POINTER(SDL_FPoint),
                         ctypes.c_int])
SDL_RenderTexture = _bind(SDL, "SDL_RenderTexture", ctypes.c_bool,
                          [ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.POINTER(SDL_FRect), ctypes.POINTER(SDL_FRect)])
SDL_RenderPresent = _bind(SDL, "SDL_RenderPresent", ctypes.c_bool, [ctypes.c_void_p])
# Render-to-texture: redirect drawing into a TARGET texture, or pass None to
# restore the window as the target. Used by the OSK open animation, which draws
# the keyboard into an offscreen texture at full opacity, then composites it to
# the window faded + clipped (the fade/reveal can't be done per-pixel + uniform
# at once on a layered Win32 window otherwise  see screen.render_open_anim).
SDL_SetRenderTarget = _bind(SDL, "SDL_SetRenderTarget", ctypes.c_bool,
                            [ctypes.c_void_p, ctypes.c_void_p])
SDL_GetRenderTarget = _bind(SDL, "SDL_GetRenderTarget", ctypes.c_void_p, [ctypes.c_void_p])


# ===========================================================================
# Surface / texture
# ===========================================================================
SDL_CreateSurfaceFrom = _bind(SDL, "SDL_CreateSurfaceFrom", ctypes.POINTER(SDL_Surface),
                              [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_int])
SDL_DestroySurface = _bind(SDL, "SDL_DestroySurface", None, [ctypes.POINTER(SDL_Surface)])
SDL_CreateTexture = _bind(SDL, "SDL_CreateTexture", ctypes.c_void_p,
                          [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int])
SDL_CreateTextureFromSurface = _bind(SDL, "SDL_CreateTextureFromSurface", ctypes.c_void_p,
                                     [ctypes.c_void_p, ctypes.POINTER(SDL_Surface)])
SDL_DestroyTexture = _bind(SDL, "SDL_DestroyTexture", None, [ctypes.c_void_p])
SDL_SetTextureScaleMode = _bind(SDL, "SDL_SetTextureScaleMode", ctypes.c_bool,
                                [ctypes.c_void_p, ctypes.c_int])
SDL_SetTextureBlendMode = _bind(SDL, "SDL_SetTextureBlendMode", ctypes.c_bool,
                                [ctypes.c_void_p, ctypes.c_uint])
SDL_SetTextureColorMod = _bind(SDL, "SDL_SetTextureColorMod", ctypes.c_bool,
                               [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte])
SDL_SetTextureAlphaMod = _bind(SDL, "SDL_SetTextureAlphaMod", ctypes.c_bool,
                               [ctypes.c_void_p, ctypes.c_ubyte])


# ===========================================================================
# TTF text
# ===========================================================================
TTF_Init = _bind(TTF, "TTF_Init", ctypes.c_bool, [])
TTF_Quit = _bind(TTF, "TTF_Quit", None, [])
TTF_OpenFont = _bind(TTF, "TTF_OpenFont", ctypes.c_void_p, [ctypes.c_char_p, ctypes.c_float])
TTF_CloseFont = _bind(TTF, "TTF_CloseFont", None, [ctypes.c_void_p])
# True when the font actually has a glyph for this codepoint. The accent
# row asks before rendering, so a character the label font lacks falls back
# to the symbol font instead of drawing a tofu box.
TTF_FontHasGlyph = _bind(TTF, "TTF_FontHasGlyph", ctypes.c_bool,
                         [ctypes.c_void_p, ctypes.c_uint32])
# SDL3_ttf: text + byte length (0 = NUL-terminated) + SDL_Color by value.
TTF_RenderText_Blended = _bind(TTF, "TTF_RenderText_Blended", ctypes.POINTER(SDL_Surface),
                               [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, SDL_Color])


# ===========================================================================
# Events
# ===========================================================================
SDL_PollEvent = _bind(SDL, "SDL_PollEvent", ctypes.c_bool, [ctypes.POINTER(SDL_Event)])


# ===========================================================================
# Mouse
# ===========================================================================
# Keep mouse motion/button events coming to OUR window even while the cursor is
# outside it. Used by the OSK's free window drag: a fast grab can outrun the
# window it's dragging, and without capture the motion (and, worse, the release)
# would be delivered to whatever is under the cursor instead.
SDL_CaptureMouse = _bind(SDL, "SDL_CaptureMouse", ctypes.c_bool, [ctypes.c_bool])


# ===========================================================================
# Gamepad (enumeration now; full poll/rumble/touchpad added with the input
# backend)
# ===========================================================================
SDL_GetGamepads = _bind(SDL, "SDL_GetGamepads", ctypes.POINTER(SDL_JoystickID),
                        [ctypes.POINTER(ctypes.c_int)])
SDL_IsGamepad = _bind(SDL, "SDL_IsGamepad", ctypes.c_bool, [SDL_JoystickID])
SDL_GetGamepadNameForID = _bind(SDL, "SDL_GetGamepadNameForID", ctypes.c_char_p,
                                [SDL_JoystickID])
# Identification (pads.identify): USB vendor/product ids + SDL's own coarse
# controller-type guess, all queryable WITHOUT opening the device.
SDL_GetJoystickVendorForID = _bind(SDL, "SDL_GetJoystickVendorForID",
                                   ctypes.c_uint16, [SDL_JoystickID])
SDL_GetJoystickProductForID = _bind(SDL, "SDL_GetJoystickProductForID",
                                    ctypes.c_uint16, [SDL_JoystickID])
SDL_GetGamepadTypeForID = _bind(SDL, "SDL_GetGamepadTypeForID",
                                ctypes.c_int, [SDL_JoystickID])


def list_gamepads():
    """Return [(instance_id, name)] for connected gamepads (needs SDL_INIT_GAMEPAD)."""
    return [(jid, name) for jid, name, _v, _p, _t in list_gamepads_ex()]


def list_gamepads_ex():
    """Return [(instance_id, name, vid, pid, sdl_type)] for connected gamepads
    (needs SDL_INIT_GAMEPAD). vid/pid are 0 when the backend can't tell."""
    count = ctypes.c_int(0)
    arr = SDL_GetGamepads(ctypes.byref(count))
    out = []
    if arr:
        try:
            for i in range(count.value):
                jid = arr[i]
                name = SDL_GetGamepadNameForID(jid)
                try:
                    vid = int(SDL_GetJoystickVendorForID(jid))
                    pid = int(SDL_GetJoystickProductForID(jid))
                    typ = int(SDL_GetGamepadTypeForID(jid))
                except Exception:
                    vid = pid = typ = 0
                out.append((int(jid),
                            name.decode("utf-8", "replace") if name else "?",
                            vid, pid, typ))
        finally:
            SDL_free(ctypes.cast(arr, ctypes.c_void_p))
    return out


# ===========================================================================
# Gamepad input backend  open/poll/rumble/touchpad/power (used by the SDL3
# input source that synthesizes SteamControllerInput for non-Steam pads).
# An SDL_Gamepad* is opaque → c_void_p.
# ===========================================================================
# SDL_GamepadButton
SDL_GAMEPAD_BUTTON_SOUTH = 0           # A / Cross
SDL_GAMEPAD_BUTTON_EAST = 1            # B / Circle
SDL_GAMEPAD_BUTTON_WEST = 2            # X / Square
SDL_GAMEPAD_BUTTON_NORTH = 3          # Y / Triangle
SDL_GAMEPAD_BUTTON_BACK = 4
SDL_GAMEPAD_BUTTON_GUIDE = 5
SDL_GAMEPAD_BUTTON_START = 6
SDL_GAMEPAD_BUTTON_LEFT_STICK = 7
SDL_GAMEPAD_BUTTON_RIGHT_STICK = 8
SDL_GAMEPAD_BUTTON_LEFT_SHOULDER = 9
SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER = 10
SDL_GAMEPAD_BUTTON_DPAD_UP = 11
SDL_GAMEPAD_BUTTON_DPAD_DOWN = 12
SDL_GAMEPAD_BUTTON_DPAD_LEFT = 13
SDL_GAMEPAD_BUTTON_DPAD_RIGHT = 14
SDL_GAMEPAD_BUTTON_MISC1 = 15
SDL_GAMEPAD_BUTTON_RIGHT_PADDLE1 = 16
SDL_GAMEPAD_BUTTON_LEFT_PADDLE1 = 17
SDL_GAMEPAD_BUTTON_RIGHT_PADDLE2 = 18
SDL_GAMEPAD_BUTTON_LEFT_PADDLE2 = 19
SDL_GAMEPAD_BUTTON_TOUCHPAD = 20

# SDL_GamepadAxis (sticks are int16 ±32768; triggers are 0..32767)
SDL_GAMEPAD_AXIS_LEFTX = 0
SDL_GAMEPAD_AXIS_LEFTY = 1
SDL_GAMEPAD_AXIS_RIGHTX = 2
SDL_GAMEPAD_AXIS_RIGHTY = 3
SDL_GAMEPAD_AXIS_LEFT_TRIGGER = 4
SDL_GAMEPAD_AXIS_RIGHT_TRIGGER = 5

# SDL_PowerState
SDL_POWERSTATE_ERROR = -1
SDL_POWERSTATE_UNKNOWN = 0
SDL_POWERSTATE_ON_BATTERY = 1
SDL_POWERSTATE_NO_BATTERY = 2
SDL_POWERSTATE_CHARGING = 3
SDL_POWERSTATE_CHARGED = 4

# Gamepad hotplug event types
SDL_EVENT_GAMEPAD_ADDED = 0x650
SDL_EVENT_GAMEPAD_REMOVED = 0x651

SDL_OpenGamepad = _bind(SDL, "SDL_OpenGamepad", ctypes.c_void_p, [SDL_JoystickID])
SDL_CloseGamepad = _bind(SDL, "SDL_CloseGamepad", None, [ctypes.c_void_p])
SDL_GamepadConnected = _bind(SDL, "SDL_GamepadConnected", ctypes.c_bool, [ctypes.c_void_p])
SDL_GetGamepadID = _bind(SDL, "SDL_GetGamepadID", SDL_JoystickID, [ctypes.c_void_p])
SDL_GetGamepadName = _bind(SDL, "SDL_GetGamepadName", ctypes.c_char_p, [ctypes.c_void_p])
SDL_UpdateGamepads = _bind(SDL, "SDL_UpdateGamepads", None, [])
SDL_GetGamepadButton = _bind(SDL, "SDL_GetGamepadButton", ctypes.c_bool,
                             [ctypes.c_void_p, ctypes.c_int])
SDL_GetGamepadAxis = _bind(SDL, "SDL_GetGamepadAxis", ctypes.c_int16,
                           [ctypes.c_void_p, ctypes.c_int])
SDL_RumbleGamepad = _bind(SDL, "SDL_RumbleGamepad", ctypes.c_bool,
                          [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32])
# RGB/brightness LED (DualSense lightbar, etc.). Returns false on pads without a
# settable LED. SDL_SendGamepadEffect sends a controller-specific raw packet
# (e.g. a Nintendo Switch output report) for finer control if SetLED is a no-op.
SDL_SetGamepadLED = _bind(SDL, "SDL_SetGamepadLED", ctypes.c_bool,
                          [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8])
SDL_SendGamepadEffect = _bind(SDL, "SDL_SendGamepadEffect", ctypes.c_bool,
                              [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int])
SDL_GetNumGamepadTouchpads = _bind(SDL, "SDL_GetNumGamepadTouchpads", ctypes.c_int,
                                   [ctypes.c_void_p])
SDL_GetNumGamepadTouchpadFingers = _bind(SDL, "SDL_GetNumGamepadTouchpadFingers", ctypes.c_int,
                                         [ctypes.c_void_p, ctypes.c_int])
SDL_GetGamepadTouchpadFinger = _bind(
    SDL, "SDL_GetGamepadTouchpadFinger", ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
     ctypes.POINTER(ctypes.c_bool), ctypes.POINTER(ctypes.c_float),
     ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)])
SDL_GetGamepadPowerInfo = _bind(SDL, "SDL_GetGamepadPowerInfo", ctypes.c_int,
                                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)])

# SDL_JoystickConnectionState  how the pad is attached. WIRED vs WIRELESS is
# what the Nintendo Bluetooth guard (nintendo_bt.py) keys off: Switch pads only
# have the ~20-minute firmware dropout over Bluetooth, never over USB-C.
SDL_JOYSTICK_CONNECTION_INVALID = -1
SDL_JOYSTICK_CONNECTION_UNKNOWN = 0
SDL_JOYSTICK_CONNECTION_WIRED = 1
SDL_JOYSTICK_CONNECTION_WIRELESS = 2

SDL_GetGamepadConnectionState = _bind(SDL, "SDL_GetGamepadConnectionState",
                                      ctypes.c_int, [ctypes.c_void_p])
# Hardware serial (the Bluetooth MAC on a Switch Pro / DualSense), or NULL when
# the backend has none. Used as a pad's STABLE identity across a disconnect:
# SDL instance ids are not reused on reconnect, so the tray can only park and
# hand back the same virtual XInput device by matching this.
SDL_GetGamepadSerial = _bind(SDL, "SDL_GetGamepadSerial", ctypes.c_char_p,
                             [ctypes.c_void_p])


def gamepad_is_wireless(gamepad):
    """True when SDL reports this pad as a WIRELESS (Bluetooth) connection.
    UNKNOWN counts as wireless: the guard it feeds is a light-touch mitigation,
    and applying it to a wired pad costs nothing, while skipping it on a
    Bluetooth pad we failed to classify costs the connection."""
    try:
        return int(SDL_GetGamepadConnectionState(gamepad)) != \
            SDL_JOYSTICK_CONNECTION_WIRED
    except Exception:
        return True


def gamepad_serial(gamepad):
    """The pad's serial as a str, or None when the backend exposes none."""
    try:
        s = SDL_GetGamepadSerial(gamepad)
    except Exception:
        return None
    return s.decode("utf-8", "replace") if s else None

# Gamepad sensors (SDL_SensorType). Gyro data arrives as 3 floats in rad/s
# (SDL's right-handed convention: +X pitch up, +Y yaw left, +Z roll ccw);
# enable streaming per pad with SDL_SetGamepadSensorEnabled first. Used by the
# tray's "Gyro To Mouse" runtime for the SDL-backed kinds (Switch Pro / PS4 /
# PS5  Xbox-style pads report no gyro and HasSensor returns false).
SDL_SENSOR_ACCEL = 1
SDL_SENSOR_GYRO = 2

SDL_GamepadHasSensor = _bind(SDL, "SDL_GamepadHasSensor", ctypes.c_bool,
                             [ctypes.c_void_p, ctypes.c_int])
SDL_GamepadSensorEnabled = _bind(SDL, "SDL_GamepadSensorEnabled", ctypes.c_bool,
                                 [ctypes.c_void_p, ctypes.c_int])
SDL_SetGamepadSensorEnabled = _bind(SDL, "SDL_SetGamepadSensorEnabled", ctypes.c_bool,
                                    [ctypes.c_void_p, ctypes.c_int, ctypes.c_bool])
SDL_GetGamepadSensorData = _bind(SDL, "SDL_GetGamepadSensorData", ctypes.c_bool,
                                 [ctypes.c_void_p, ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_float), ctypes.c_int])


def gamepad_gyro(gamepad):
    """Return the pad's current gyro reading as (x, y, z) in rad/s, or None
    when the pad has no gyro / streaming isn't enabled. Values refresh on
    SDL_UpdateGamepads, so call from the thread pumping the gamepads."""
    vals = (ctypes.c_float * 3)()
    if not SDL_GetGamepadSensorData(gamepad, SDL_SENSOR_GYRO, vals, 3):
        return None
    return (vals[0], vals[1], vals[2])


def gamepad_touchpad_finger(gamepad, touchpad=0, finger=0):
    """Return (down, x, y, pressure) for one touchpad finger (x/y 0..1), or
    None. Used to map a DualSense-style touchpad onto the SC left trackpad."""
    down = ctypes.c_bool(False)
    x = ctypes.c_float(0.0)
    y = ctypes.c_float(0.0)
    pres = ctypes.c_float(0.0)
    ok = SDL_GetGamepadTouchpadFinger(gamepad, touchpad, finger,
                                      ctypes.byref(down), ctypes.byref(x),
                                      ctypes.byref(y), ctypes.byref(pres))
    if not ok:
        return None
    return (bool(down.value), x.value, y.value, pres.value)


def gamepad_power(gamepad):
    """Return (power_state, percent). percent is -1 when unknown."""
    pct = ctypes.c_int(-1)
    st = SDL_GetGamepadPowerInfo(gamepad, ctypes.byref(pct))
    return (int(st), int(pct.value))


def smoke():
    """Prove the vendored SDL3 loads and can enumerate gamepads."""
    print(f"sdl3w loaded from: {DLL_DIR}")
    print(f"pinned: SDL3={SDL3_VERSION} ttf={SDL3_TTF_VERSION}; runtime SDL3={version_tuple()}")
    if not SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMEPAD | SDL_INIT_EVENTS):
        print("SDL_Init FAILED:", get_error())
        return False
    try:
        pads = list_gamepads()
        print(f"gamepads detected: {len(pads)}")
        for jid, name in pads:
            print(f"  [{jid}] {name}")
    finally:
        SDL_Quit()
    print("smoke OK")
    return True
