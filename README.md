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

# SteamlessInput

An open-source, Steam Input recreation that turns any gamepad into a Steam Controller with improved PC Controls, Virtual Keyboard, Trackpad Gestures, and customizable Virtual Menus for when Steam stops running! Plus Steam & Big Picture launch options and tools to turn Windows into a console.

SteamlessInput is a platform for building and improving controller features. It fills the gaps Steam Input leaves behind especially around desktop navigation. It's a companion to Steam, not a replacement. Got an idea for something your controller should do? Open an issue and describe it. Code contributions are welcome too.

Some things it's trying to be:

1. Full Steam Input parity.
desktop mouse/keyboard/media bindings, button remapping, and community configs imports via SteamInputDB. Also fixes Steam Input's inability to interact with elevated windows (Task Manager, installers, UAC prompts).

2. A home for experiments.
Adds new features to Steam Controller & DualSense: trackpad gestures (laptop scrolling, pinch to zoom, swipe between pages, text wheel selection, timeline scrubbing), Added gyro typing to the on-screen keyboard — alongside swipe typing, multiple layouts, and custom keyboard skins.

3. Living-room couch gaming.
Turn windows into a console with handy Steam & Big Picture launch options, Sleep Manager, Power Management, display/audio output switching, HDR and Night Light control, media pause and cursor hiding on launch, and a lock-screen keyboard to log into windows.

4. Covers the edge cases Steam Input misses. Translates any controller to a virtual Xbox 360 controller so non-Steam games, launchers, and emulators just work.


---

# Features

**SteamInput parity**

- **Advanced Presses** — long press, double press, soft pull, mode shift
- **Hotkey chords**
- **Community Configs** — import from [SteamInputDB](https://www.steaminputdb.com)
- **Virtual Menus** — streamdeck like folder and layers *(planned)*

## On-screen keyboard

- **Layouts** — QWERTY, phone-style, 75% (F1-F12 and arrow keys), and split
- **Input methods** — trackpad, stick, gyro, mouse
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

- Steam and Big Picture launch options, account settings
- Display and audio output device switching
- HDR and Night Light toggles
- Media pause on launch, cursor hiding on launch
- Sleep Manager power settings
- Lock-screen keyboard

## Input Relay

Fixes the inability to interact with elevated windows (Task Manager, installers, UAC prompts).
Steam Input has the same problem. This optional input relay keeps the controller working there.


## Supported controllers

<p align="center">
  <img src="https://img.shields.io/badge/Steam%20Controller%20%282026%29-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Steam%20Controller%20%282015%29-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Steam%20Deck-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Xbox%20Controller-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/DualSense%20%28PS5%29-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/DualShock%204%20%28PS4%29-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Switch%20Pro%20Controller-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Switch%202%20Pro%20Controller-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Joy--Con-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Joy--Con%202-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/NSO%20SNES%20%2F%20NES%20%2F%20N64%20%2F%20Genesis-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/GameCube%20Controller-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/ROG%20Ally%20%C2%B7%20Legion%20Go%20%C2%B7%20MSI%20Claw-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/GPD%20Win%20%C2%B7%20OneXPlayer%20%C2%B7%20AYANEO-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/8BitDo%20controllers-0A7FB0?style=flat-square&labelColor=0A1A38">
  <img src="https://img.shields.io/badge/Any%20other%20SDL%20gamepad-0A7FB0?style=flat-square&labelColor=0A1A38">
</p>

---

## Installation
Download and run the setup wizard from the [Releases page](https://github.com/PietPetGit/SteamlessInput/releases).

---

## Support the project

If I saved you buying a wireless keyboard, or made your setup better:

- ⭐ **Star the repo.** It's free, and it's the single thing that most helps
  other people find this.
- 💸 **Donate, or leave a tip** I'm a frugal person, so I promise
  no penny goes to waste
- 🛠 **Pay me to add a feature.** Want something big built? DM me or open an issue and we'll talk.

---

## Credits

- On-screen keyboard base built by me, with significant features and improvements merged in from [Mateusz Kłysz's DualTouch](https://github.com/mateuszklysz/dualtouch). Many thanks to Mateusz for his hard work.
- Virtual gamepad driver by [Nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus)
- Live controller preview ported from [Ramonchi_5's Steam Controller Gamepad Viewer](https://github.com/ramonchi5/Steam-Controller-Gamepad-Viewer-by-Ramonchi_5)
- Big Picture automation
  - Night Light control and Big Picture window detection ported from [magrega/BigPictureManager](https://github.com/magrega/BigPictureManager)
  - Linux controller-connect auto open/close ported from [goatvisuals/Auto-Big-Picture](https://github.com/goatvisuals/Auto-Big-Picture)

---

## Disclaimer

SteamlessInput is an independent, unofficial project. It is **not affiliated
with, endorsed by, or sponsored by Valve Corporation**. "Steam", "Steam Deck",
"Steam Controller" and "Steam Input" are trademarks of Valve Corporation, used
here only to describe the hardware and software this tool works with.

Some bundled interface artwork originates from the Steam client and remains the
property of Valve Corporation; it is not covered by this project's licence. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for full attribution.
