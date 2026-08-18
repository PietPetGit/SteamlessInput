# Showcase assets

Every graphic here is a hand-written, self-contained SVG in [`docs/assets/`](assets/).
They use no external fonts, scripts or images, so they render inline on GitHub,
scale to any width, and stay sharp on a 4K monitor. Nothing needs a build step —
edit the text in a file and the graphic is updated.

| File | Size | Use it for |
|---|---|---|
| [`hero.svg`](assets/hero.svg) | 1280×420 | Top of the README |
| [`features.svg`](assets/features.svg) | 1280×760 | "Features" section |
| [`how-it-works.svg`](assets/how-it-works.svg) | 1280×520 | "What this is" / architecture |
| [`keyboard-modes.svg`](assets/keyboard-modes.svg) | 1280×520 | On-screen keyboard section |
| [`controllers.svg`](assets/controllers.svg) | 1080×748 | "Supported controllers" |
| [`comparison.svg`](assets/comparison.svg) | 1080×560 | "Not a replacement" pitch |
| [`virtual-menus.svg`](assets/virtual-menus.svg) | 1080×440 | Gamepad-mode section |
| [`big-picture.svg`](assets/big-picture.svg) | 1080×460 | Living-room / HTPC section |
| [`cheatsheet.svg`](assets/cheatsheet.svg) | 880×620 | Default keybinds |
| [`install.svg`](assets/install.svg) | 1280×300 | Installation section |
| [`logo.svg`](assets/logo.svg) | 256×256 | Icon, docs, avatars |
| [`social-preview.png`](assets/social-preview.png) | 1280×640 | GitHub **social preview** (Settings → General → Social preview) |

Three of them animate subtly on GitHub (the hero logo drifts, the swipe-typing
trace draws itself, the radial menu sweeps). They degrade to a clean static
image anywhere animation is disabled.

---

## Copy-paste blocks

### Hero (top of the README)

```markdown
<p align="center">
  <img src="docs/assets/hero.svg" alt="SteamlessInput — an open-source, easier-to-use Steam Input" width="100%">
</p>
```

### Badge row

```markdown
<p align="center">
  <a href="https://github.com/PietPetGit/SteamlessInput/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/PietPetGit/SteamlessInput?style=for-the-badge&labelColor=0A1A38&color=0A7FB0"></a>
  <a href="https://github.com/PietPetGit/SteamlessInput/releases"><img alt="Downloads" src="https://img.shields.io/github/downloads/PietPetGit/SteamlessInput/total?style=for-the-badge&labelColor=0A1A38&color=0A7FB0"></a>
  <a href="LICENSE"><img alt="Licence" src="https://img.shields.io/badge/licence-GPL--3.0-0A7FB0?style=for-the-badge&labelColor=0A1A38"></a>
  <img alt="Platforms" src="https://img.shields.io/badge/Windows%20%7C%20Linux-0A7FB0?style=for-the-badge&labelColor=0A1A38&logo=windows&logoColor=white">
  <a href="../../stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/PietPetGit/SteamlessInput?style=for-the-badge&labelColor=0A1A38&color=0A7FB0"></a>
</p>
```

### Section images

```markdown
![How SteamlessInput works](docs/assets/how-it-works.svg)
![Features](docs/assets/features.svg)
![Four ways to type](docs/assets/keyboard-modes.svg)
![Supported controllers](docs/assets/controllers.svg)
![SteamlessInput compared with Steam Input](docs/assets/comparison.svg)
![Virtual menus](docs/assets/virtual-menus.svg)
![Big Picture automation](docs/assets/big-picture.svg)
![Desktop-mode cheat sheet](docs/assets/cheatsheet.svg)
![Installing takes three steps](docs/assets/install.svg)
```

### Light/dark variants

If you ever add a light-theme version of an asset, GitHub picks between them
with `<picture>`:

```markdown
<picture>
  <source media="(prefers-color-scheme: dark)"  srcset="docs/assets/hero.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/hero-light.svg">
  <img alt="SteamlessInput" src="docs/assets/hero.svg">
</picture>
```

The current set is dark-on-dark by design and paints its own background, so it
reads correctly in both GitHub themes as-is.

---

## Diagrams you can paste anywhere

GitHub renders these Mermaid blocks natively — in the README, in issues, in
discussions and in release notes.

### Input pipeline

```mermaid
flowchart LR
  SC["Steam Controller<br/>2015 · 2026 · Deck"] --> HID["HID takeover<br/><i>trackpads · haptics · gyro</i>"]
  PS["DualSense · DS4"] --> SDL["SDL3"]
  NIN["Switch Pro · Joy-Con · NSO"] --> SDL
  XB["Xbox · 8BitDo · handhelds"] --> SDL
  HID --> CORE
  SDL --> CORE
  CORE["<b>Translation core</b><br/>remap · chords · gestures<br/>gyro · virtual menus · profiles"]
  CORE --> OSK["On-screen keyboard"]
  CORE --> DESK["Mouse · scroll · media keys"]
  CORE --> PAD["Virtual Xbox 360 pad<br/><i>ViGEm / uinput</i>"]
  CORE --> SYS["Display · audio · HDR · sleep"]
```

### Desktop ⇄ gamepad mode

```mermaid
stateDiagram-v2
  [*] --> Desktop
  Desktop --> Gamepad: game takes focus
  Desktop --> Gamepad: hold ≡ for ¾ s
  Gamepad --> Desktop: game loses focus
  Gamepad --> Desktop: hold ≡ for ¾ s
  Desktop: Desktop mode
  Desktop: mouse · scroll · keyboard · gestures
  Gamepad: Gamepad mode
  Gamepad: virtual Xbox pad · trackpad mouse on ≡ + pad
```

### What the setup wizard installs

```mermaid
flowchart TD
  A[Run SteamlessInput-Setup] --> B{Pick components}
  B --> C[SteamlessInput<br/><i>required</i>]
  B --> D[Start Menu / desktop shortcut]
  B --> E[Start at login]
  B --> F[ViGEmBus<br/><i>gamepad mode</i>]
  B --> G[HidHide<br/><i>Nintendo pads</i>]
  B --> H[Lock-screen keyboard<br/><i>opt-in, security trade-off</i>]
  F --> I[One UAC prompt, batched]
  G --> I
  H --> I
  I --> J[Registered in Apps &amp; features<br/>so it all uninstalls cleanly]
```

### Typing paths

```mermaid
mindmap
  root((Type from<br/>the couch))
    Trackpads
      Split Keyboard
      Swipe Typing
      Touch Typing
      Release Touch To Type
    Gyro
      Gyro To Type
      L3+R3 to recenter
    Sticks
      Key Hit Assist
      Press To Focus Key
    Layouts
      QWERTY
      Phone-style
      75% with F-row
      Hold For Accents
```

---

## Regenerating the raster

`social-preview.png` is rendered from `social-preview.svg`. Any headless Chromium
does it:

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --headless --disable-gpu --hide-scrollbars --window-size=1280,640 `
  --screenshot="docs\assets\social-preview.png" `
  "file:///$PWD/docs/assets/social-preview.svg".Replace('\','/')
```

---

## Editing tips

- Colours live in the `<defs>` of each file. The palette is
  `#050B18` (ink), `#0B1729`/`#12233F` (cards), `#0A7FB0` (brand),
  `#38BDF8` (accent), `#DCE9F8` (text), `#8FA3BF` (muted).
- Text is real `<text>`, not paths, so it stays searchable and easy to retype —
  but it is not auto-wrapped: each line is its own `<text>` element.
- Fonts fall back to the viewer's system UI font, so keep a little slack at the
  end of long lines.
