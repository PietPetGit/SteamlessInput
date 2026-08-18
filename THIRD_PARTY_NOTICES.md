# Third-Party Notices

SteamlessInput is licensed under the **GNU General Public License v3.0**  see
[`LICENSE`](LICENSE). This file records third-party work the project bundles,
derives from, or interoperates with, and the licence each one carries. Every
licence listed here is GPL-3.0 compatible. These notices must be kept intact in
any redistribution.

This project includes code and design adapted from the following third-party
works.

## adusk (by archshift)  origin of the on-screen keyboard

`windows/adusk/` (mirrored at `linux/adusk/`) began as a fork of
[archshift/adusk](https://github.com/archshift/adusk), a Steam Controller driven
virtual keyboard, which is licensed under the **GNU Lesser General Public
License v3.0**.

Little of the original remains. The OSK has grown from roughly 570 lines to over
8,000, and the modules that started from adusk  `vkb.py`, `screen.py`,
`controller.py`, `state.py`, `config.py`, `resources.py`, `utils.py`, `vptr.py`
 have since been reimplemented, keeping behaviour identical where the rest of
the app is calibrated against it (key geometry, the pad-to-canvas mapping). What
still corresponds to adusk is the module layout and public API surface: module
and class names, method signatures, and single-form statements such as attribute
assignments.

LGPL-3.0 is GPL-3.0 plus additional permissions, and GPLv3 §7 permits those
additional permissions to be removed. The adusk-derived portions are therefore
conveyed here under the GPL-3.0 that covers the project as a whole.

## DualTouch (by PietPetGit)  merged on-screen-keyboard features

[DualTouch](https://github.com/PietPetGit/dualtouch) is a Windows-only fork of
this project's on-screen keyboard, also licensed under the **GNU Lesser General
Public License v3.0**. Several of the features it added downstream are merged
back in here:

* **Split Keyboard**  the two-halves layout with a transparent middle band and
  a per-half trackpad mapping (`vkb.py`'s split geometry, `screen.py`'s
  `_render_split_background`, `controller.py`'s `adjust_raw_x_span`).
* **Hold For Accents**  the per-locale variant map, the candidate-strip
  geometry, and the tap-first / hold-to-extend defer model
  (`adusk/diacritics.py`, the accent paths in `vkb.py` and `controller.py`).
* **The 75% layout**  its key set and proportions
  (`data/cfg/keyboard-layout-75.yaml`), including the hold-and-drag Select key
  and its lift-off roll-back cancellation.
* **Key Hit Assist** (`find_key_expanded`), **Press To Focus Key** (the
  press/pull cursor lock), **Remember Per App**, and the Steam keyboard sounds
  (`adusk/key_sound.py`).
* The Unicode injection paths behind the accent commit  virtual key, then
  KEYEVENTF_UNICODE, then clipboard paste  in
  `windows/steamcontroller/uinput.py`. The Linux equivalent is written fresh
  against the uinput/pynput backends this tree uses.
* The **per-key press pop** (a pressed key sinks slightly and springs back)
  and the **close animation**, plus the spring solver they run on
  (`adusk/utils.py`'s `spring_p`).
* The bundled **Gruvbox** OSK skin (`data/skins/Gruvbox.css`).

The code was adapted rather than copied verbatim: this tree's OSK has diverged
(multi-controller support, multi-page layouts, swipe/touch typing, gyro typing,
a dirty-flag renderer), so each feature was re-implemented against those
structures. As with adusk above, GPLv3 §7 permits LGPL-3.0's additional
permissions to be removed, so the DualTouch-derived portions are conveyed here
under the GPL-3.0 that covers the project as a whole.

## Valve Corporation  trademarks and artwork

SteamlessInput is an independent, unofficial project. It is **not affiliated
with, endorsed by, or sponsored by Valve Corporation**. "Steam", "Steam Deck",
"Steam Controller", "Steam Input" and the Steam logo are trademarks and/or
registered trademarks of Valve Corporation. They are used here only to describe
the hardware and software this tool interoperates with (nominative use).

This repository bundles image assets under
`windows/data/images/vmenu_icons/` (mirrored at `linux/data/images/vmenu_icons/`)
that originate from Valve's Steam client. They are used to make the Virtual
Menus editor visually match Steam's own touch-menu icon picker, so a
config authored here looks like the Steam config it replaces. Two provenances:

* **Controller / control glyphs** (the picker's "Other" tab  face buttons,
  d-pad, sticks, gyro, mouse, trackpad) are derived from the Steam client's own
  installed art, `controller_base/images/api/knockout/` (the
  white-on-transparent "knockout" theme), re-rendered to PNG.
* **Generic action icons** (weapons, ammo, inventory, magic, movement, menu,
  vehicle, utility, input, media, targets, social) are the same library Steam's
  own "Select an Icon" browser offers. That library is served from Valve's CDN
  on demand rather than shipped with the client, so these were captured by
  rendering Steam's own picker and cropping each grid cell.

**These assets remain the property of Valve Corporation.** They are included
under a good-faith view of nominative/interoperability use, not under this
project's licence  the repository `LICENSE` covers this project's own source
code and does **not** grant any rights to Valve's artwork. No copyright claim is
made over them. If Valve objects, they will be removed on request: open an
issue, or see `windows/keybinds_runtime.py` (`VMENU_ICON_GROUPS`). Deleting the
assets does not break the build or the app  `_load_vmenu_icon_asset` treats any
unreadable id as "no asset" and `draw_vmenu_icon` then renders a plain circle
outline, while menu entries set to "none" show their action label as text and
user-supplied PNGs (the picker's Custom tab) keep working.

## Steam Controller Gamepad Viewer (by Ramonchi_5)

`windows/sc_viewer.py` (mirrored at `linux/sc_viewer.py`)  the live Steam
Controller preview shown on the Desktop / Chords / Gamepad tabs of the
keybinds manager  is a from-scratch Python/Tk/PIL port of the controller
artwork, geometry and layout from the bundled
`Steam-Controller-Gamepad-Viewer-by-Ramonchi_5-main/` project (its SVG art,
`styles.css` and `app.js`, referenced during the port). The HID input parsing
in that project (`SteamHidTouchpadService.cs`) also informed field layout
understanding for the Triton report.

```
MIT License

Copyright (c) 2026 Ramonchi_5 (ramonchi5)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Full original source: `Steam-Controller-Gamepad-Viewer-by-Ramonchi_5-main/`
(bundled in this repository, including its own `LICENSE` file), also published
at https://github.com/ramonchi5/Steam-Controller-Gamepad-Viewer-by-Ramonchi_5.
The keybinds manager's live SC viewer links here too  click the Steam button
glyph in the centre of the controller preview.

## Big Picture automation  BigPictureManager, Auto-Big-Picture, Big Picture Portal

The Options → Big Picture feature (`windows/big_picture.py`, mirrored in reduced
form at `linux/big_picture.py`) is built on code ported from three open-source
Big Picture helper projects, each MIT-licensed. What came from where:

* **BigPictureManager** (by magrega)  the largest single contribution. Its
  `NightLight.cs` byte-level CloudStore registry codec is ported essentially
  1:1 into `big_picture.py`'s night-light section: the two state/settings
  registry key variants, the marker byte at offset 18, the four known blob
  shapes (43/40/41/38 bytes for manual-on / schedule-on / manual-off /
  schedule-off), the splice offsets in each shape conversion, the 5-byte
  version-counter bump that makes Windows notice the write, the
  `CA 14 0E … CA 1E 0E` schedule-marker search, and the "semantic restore"
  that puts Night Light back into its exact prior manual/scheduled mode rather
  than blindly toggling it. Its `BigPictureWatcher.cs` also contributed the
  rule that a candidate Big Picture window must belong to a `steam*` process
  (so an unrelated window with "Big Picture" in its title cannot trigger), and
  its `SystemMediaPause.cs` the approach of pausing only SMTC sessions that
  actually report `Playing`.
* **Auto-Big-Picture** (by Goatvisuals)  `linux/big_picture.py` is a port of
  its `auto-big-picture.py` systemd service into our tray process: the
  native-vs-flatpak Steam command detection, the `pgrep -af steamapps/common`
  in-game guard (including its `d3ddriverquery64.exe` exclusion), the
  joystick-node polling for connect/disconnect edges, and the
  `steam://open/bigpicture` / `steam://close/bigpicture` control URLs. The
  Windows side reuses the same open/close design with the controller state
  taken from the tray and the in-game guard read from Steam's `RunningAppID`.
* **Big Picture Portal**  its `cursor.go` is ported as `big_picture.py`'s
  cursor manager: the list of system cursor IDs to override, the blank 32×32
  AND/XOR mask cursor built with `CreateCursor`, applying it via
  `SetSystemCursor`, restoring every cursor with
  `SystemParametersInfo(SPI_SETCURSORS)`, and the poll loop that reveals the
  cursor on mouse movement and re-hides it after an idle delay.

```
MIT License

Copyright (c) 2026 magrega                            (BigPictureManager)
Copyright (c) 2025 Goatvisuals                        (Auto-Big-Picture)
Copyright (c) 2025 Big Picture Portal Contributors    (Big Picture Portal)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

(The three projects ship separate but identical MIT texts; they are combined
above with each copyright line preserved.) Upstream:
https://github.com/goatvisuals/Auto-Big-Picture. The bundled copies of
BigPictureManager and Big Picture Portal carry no repository metadata  their
upstream URLs still need to be filled in here.

Everything else in the feature  Big Picture window detection, the
DisplayConfig display save/switch/restore, HDR control, and the WinRT media
pause  is written directly against the documented Win32/WinRT APIs (the same
DisplayConfig API `windows/tray.py` already drove for display scaling before
this feature existed) and is covered by this project's own licence.

## SteamInputDB.com (by Peter Repukat / Alia5)

The keybinds manager's "Community Configs" browser
(`windows/steam_input_vdf.py`, mirrored at `linux/steam_input_vdf.py`) is a
CLIENT of the public SteamInputDB.com API (https://api.steaminputdb.com)  a
community-driven, Steam-Web-API-backed database of Steam Input controller
configurations, published at https://github.com/Alia5/steaminputdb.com under
AGPL-3.0. No SteamInputDB source code is included in or linked into this
project; we call its documented HTTP API (search + config file details) and
download the configuration files themselves from Valve's own CDN
(cdn.steamusercontent.com), exactly as the Steam client would. SteamInputDB's
public schema/preview sources were consulted as documentation of the Steam
Input VDF format when writing our independent converter. SteamInputDB is not
affiliated with this project; please support them at
https://www.steaminputdb.com.

## FrequencyWords word-frequency lists (by Hermit Dave)

The Swipe Typing decoder (`windows/adusk/swipe.py`, mirrored at
`linux/adusk/swipe.py`) needs a frequency-ordered lexicon: shape writing is
inherently ambiguous  many words trace nearly the same path  so a language
prior is what picks "hello" over "helo". The bundled list at
`windows/data/lexicon/en.txt` (mirrored at `linux/data/lexicon/en.txt`) is the
top 30,000 pure-a-z entries of the English 50k list from
https://github.com/hermitdave/FrequencyWords, published by Hermit Dave under
the **MIT licence** (Copyright (c) 2016 Hermit Dave) and derived from word
counts over the OpenSubtitles corpus. Only the words are kept, in the source's
own frequency order  a word's line number IS its rank  with the raw counts,
non-ASCII forms and apostrophe clitics stripped (see the generator described in
`swipe.py`). Being subtitle-derived, the list is conversational in character,
which suits an on-screen keyboard.

No decoding logic comes from that project; the shape-writing decoder itself is
this project's own implementation of the published SHARK²-style shape +
location channel approach.

On Windows the decoder's candidate ranking is additionally rescored by
`Windows.Data.Text.TextPredictionGenerator`  the text-prediction engine behind
the Windows touch keyboard, reached through the documented WinRT projection.
Nothing from it is bundled: it reads the user's own installed language data and
personal typing dictionary at runtime, and the feature degrades to the bundled
list's ranking wherever it isn't available.
