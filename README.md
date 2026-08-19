<!--
  Looping hero slideshow. Animated WebP for browsers that take it, animated PNG
  for the rest; both are built by docs/tools/ from real window grabs.
-->
<p align="center">
  <picture>
    <source srcset="docs/assets/showcase-slideshow.webp" type="image/webp">
    <img src="docs/assets/showcase-slideshow.png" width="100%"
         alt="SteamlessInput in action: the desktop binding editor, the virtual-menu builder with its live preview, and the on-screen keyboard wearing the Hatsune Miku skin">
  </picture>
</p>

<p align="center">
  <sub>
    <a href="docs/assets/showcase-1-desktop.png">Desktop layout</a> ·
    <a href="docs/assets/showcase-2-virtual-menus.png">Virtual menus</a> ·
    <a href="docs/assets/showcase-3-keyboard.png">On-screen keyboard</a>
  </sub>
</p>

# SteamlessInput

An open-source, "easier" to use Steam Input that turns any gamepad into a Steam Controller with improved PC Controls, Virtual Keyboard, Trackpad Gestures, and customizable Virtual Menus without Steam running! Plus tools to configure Steam & Big Picture for couch-console gaming.

SteamlessInput is an open-source **platform to build and add controller
features on** for windows/linux desktop, productivity and in game control.
it runs as a small tray app with no Steam client, no game library and no account required.

Some things it's trying to be:

1. **Easier to use.** Steam Input is enormously powerful and enormously fiddly.
   Here the settings sit on one page
2. **A home for experiments.** Trackpad gestures, gyro typing, swipe typing,
   virtual menus like a streamdeck, pinch-to-zoom, timeline scrubbing
3. **Your living-room setup.** tools to configure Steam & Big Picture for couch-console gaming, console like power management
   Steam launch settings, Big Picture automation can switch your TV display
  and audio output, turn HDR on, kill Night Light, pause media and hide the
  cursor, insane quality of life settings for gamers.
5. **turns any gamepad into a Steam Controller with improved PC Controls** Everything you'd otherwise need a wireless
   keyboard for, done from the pad already in your hands. Mouse and keyboard controls, media controls like volume, Alt+Tab. Recreates the Steam Controller's default desktop bindings on every controller.
6. - **For those edge cases where your controller does nothing outside Steam.**
  Steam Input only helps inside Steam; this doesn't care. By translating any pad into a virtual Xbox 360 controller everything can be played, non-Steam games, Launchers, emulators. all while being lower latency than projects like SISR and VIIPER


---

# Features

## Core

Turns any gamepad into a Steam Controller — mouse, keyboard and media playback
control, with full button and stick remapping. Can also present the pad as an
Xbox 360 controller.

**SteamInput parity**

- **Advanced Presses** — long press, double press, soft pull, mode shift
- **Hotkey chords**
- **Community Configs** — import from [SteamInputDB](https://www.steaminputdb.com)
- **Virtual Menus** — folders and layers *(planned)*

## On-screen keyboard

- **Layouts** — QWERTY, phone-style, 75% (F1-F12 and arrow keys), and split
- **Input methods** — trackpad, stick, gyro, mouse or manual keyboard
- **Typing modes** — two-thumb simultaneous, swipe, touch, release-touch
- **Gyro typing**
- **Skins** — transparency and size control, including custom anime skins

## Trackpad gestures

*Steam Controller and Steam Deck. PlayStation DualSense planned.*

- Laptop-style smooth scrolling
- Tap to click
- Pinch to zoom
- Swipe between pages
- Text wheel selection
- YouTube timeline scrubbing

## Living-room automation

*Companion app for Steam and Big Picture.*

- Steam and Big Picture launch options, account settings
- Display and audio output device switching
- HDR and Night Light toggles
- Media pause on launch, cursor hiding on launch
- **Sleep Manager** — power settings
- Lock-screen keyboard

## Elevated windows

Elevated windows (Task Manager, installers, UAC prompts) freeze controller input
and force you back to a mouse and keyboard. Steam Input has the same problem. An
optional input relay keeps the controller working there.


## Supported controllers

Every pad below gets its own page in the GUI, with the right button labels and
glyphs. **Steam Controllers and the Steam Deck** are driven by a dedicated HID
takeover (trackpads, haptics, chords, full remap); everything else runs through
SDL3.

| Controller | Driver | Trackpads | Gyro |
|---|:--:|:--:|:--:|
| Steam Controller (2026) | HID | ✔ | ✔ |
| Steam Controller (2015) | HID | ✔ | ✔ |
| Steam Deck (built-in) | HID *(Windows)* | ✔ | ✔ |
| Xbox Controller | SDL | | |
| DualSense (PS5) | SDL | | ✔ |
| DualShock 4 (PS4) | SDL | | ✔ |
| Switch Pro Controller | SDL | | ✔ |
| Switch 2 Pro Controller | SDL | | ✔ |
| Joy-Con — as a pair, or each half alone | SDL | | ✔ |
| Joy-Con 2 — as a pair, or each half alone | SDL | | ✔ |
| ROG Ally · Legion Go · MSI Claw | SDL | | |
| GPD Win · OneXPlayer · AYANEO | SDL | | |
| 8BitDo controllers | SDL | | |
| Any other SDL gamepad | SDL | | |

---

## Installation

### The easy way — run the setup wizard

Download the release package from the [Releases page](https://github.com/PietPetGit/SteamlessInput/releases), unpack it, and run **`SteamlessInput-Setup`** (`.exe` on Windows).

## Controller keybinds (desktop mode)

These are the defaults — every one of them is rebindable in the GUI.

| Input | Action |
|-------|--------|
| <img src="windows/assets/shared_button_x_md.png" width="32" align="middle"> | Open the on-screen keyboard (or Windows + Ctrl + O) |
| <img src="windows/assets/sd_button_menu_md.png" width="32" align="middle"> **(hold)** | Switch between Desktop and Gamepad controls — hold ≡ (Start / Menu / + / Options) **by itself** for about ¾ of a second and a toast tells you which mode you landed in. Works in **both** modes, on every controller, with nothing to bind first. Holding it together with any other button is still a normal chord, and a quick press is still just Start. |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_button_b_md.png" width="32" align="middle"> | Force-shutdown game |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_button_y_md.png" width="32" align="middle"> | Turn off the controller |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/sd_button_menu_md.png" width="32" align="middle"> | Alt+Tab — hold Steam to keep the switcher open; each VIEW press advances one slot |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/sd_rtrackpad_md.png" width="32" align="middle"> | Use the trackpad as a mouse while in gamepad mode |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_up_md.png" width="32" align="middle"> | Volume up — tap for one step, hold to ramp |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_down_md.png" width="32" align="middle"> | Volume down — tap for one step, hold to ramp |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_left_md.png" width="32" align="middle"> | Previous song |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_right_md.png" width="32" align="middle"> | Next song |
| <img src="windows/data/images/glyphs/glyph_l3.png" width="32" align="middle"> | Middle click — click the left stick in (e.g. open a link in a new tab, or close a tab) |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/data/images/glyphs/glyph_l3.png" width="32" align="middle"> | Play / pause (click the left stick in) |
| <img src="windows/data/images/glyphs/glyph_l3.png" width="32" align="middle"> + <img src="windows/data/images/glyphs/shared_r3.png" width="32" align="middle"> **(while the on-screen keyboard is open)** | Turn on Gyro To Type for the current controller if it's off; if it's already on, recenter the pointer — no keybind needed, see Options → Keyboard → Gyro To Type |

---

## Support the project

This is a one-person, unpaid project that already tries to do a lot at once. If
it saved you buying a wireless keyboard, or made your living-room PC usable:

- ⭐ **Star the repo.** It's free, and it's the single thing that most helps
  other people find this.
- 💸 **Donate, or leave a tip** I'm a frugal person, so I promise
  no penny goes to waste
- 🛠 **Pay me to add a feature.** Want something specific built, or your
  controller supported? Sponsor it and I'll prioritise it — open an issue
  describing what you want and we'll talk.

<!-- TODO: replace these with your real donation links before publishing -->
[**Donate / tip**](https://ko-fi.com/YOUR-USERNAME) · [**Sponsor**](https://github.com/sponsors/YOUR-USERNAME)

## Help wanted — bring ideas

The point of this project is to be a **place to try controller ideas Steam
wouldn't ship**. If you've ever thought *"my controller should be able to do X on
the desktop"*, this is where X gets built.

I'm especially looking for people with **creative ideas** — you don't have to
write any code. Open an issue and describe it:

- New trackpad, gyro or motion gestures
- Ways to make couch PC use less painful
- Anything Steam Input can't or won't do

Code contributions are welcome too.

---

## Credits

- Split Keyboard, the hold-for-accents row, the 75% layout with its
  hold-and-drag Select key, Key Hit Assist, Press To Focus Key, per-app
  keyboard memory and the Steam keyboard sounds are merged in from
  [DualTouch](https://github.com/PietPetGit/dualtouch) (LGPL-3.0), a Windows
  fork of this keyboard — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- Virtual gamepad driver by [Nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus)
- Live controller preview ported from [Ramonchi_5's Steam Controller Gamepad Viewer](https://github.com/ramonchi5/Steam-Controller-Gamepad-Viewer-by-Ramonchi_5) (MIT) — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- Big Picture automation (Options → Big Picture) builds on three open-source projects, all MIT — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md):
  - Night Light control and Big Picture window detection ported from **BigPictureManager** by magrega <!-- TODO: add upstream URL - the local copy carries no repo metadata --> — its byte-level CloudStore registry codec is the reason "Disable Night Light" restores your exact manual/scheduled state instead of blindly toggling
  - Linux controller-connect auto open/close ported from [goatvisuals/Auto-Big-Picture](https://github.com/goatvisuals/Auto-Big-Picture) — `linux/big_picture.py` is essentially its service logic (Steam native/flatpak detection, the `steamapps/common` in-game guard, joystick hotplug polling) moved into the tray process
  - Cursor hiding ported from **Big Picture Portal**'s `cursor.go` (blank system cursors with a move-to-reveal poll) <!-- TODO: add upstream URL - the local copy's README has a placeholder repo path -->

---

## Disclaimer

SteamlessInput is an independent, unofficial project. It is **not affiliated
with, endorsed by, or sponsored by Valve Corporation**. "Steam", "Steam Deck",
"Steam Controller" and "Steam Input" are trademarks of Valve Corporation, used
here only to describe the hardware and software this tool works with.

Some bundled interface artwork originates from the Steam client and remains the
property of Valve Corporation; it is not covered by this project's licence. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for full attribution.
