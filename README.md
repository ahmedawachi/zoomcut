<div align="center">

<img src="docs/banner.png" alt="Zoomcut — record your screen, get a video that looks edited" width="100%">

Zoomcut watches what you actually did — typed into a box, opened a panel, got a result back —
and moves a virtual camera to match. Your recording floats on your macOS wallpaper with a soft
shadow, the camera pushes in where it matters and **holds perfectly still where it doesn't**.

**You never place a keyframe.**

[![tests](https://github.com/ahmedawachi/zoomcut/actions/workflows/tests.yml/badge.svg)](https://github.com/ahmedawachi/zoomcut/actions/workflows/tests.yml)
[![platform](https://img.shields.io/badge/platform-macOS-000?logo=apple&logoColor=white)](#requirements)
[![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](#requirements)
[![license](https://img.shields.io/badge/license-MIT-6c63ff)](LICENSE)
[![dependencies](https://img.shields.io/badge/deps-numpy%20%C2%B7%20Pillow%20%C2%B7%20ffmpeg-informational)](#requirements)

</div>

---

```bash
git clone https://github.com/ahmedawachi/zoomcut.git
cd zoomcut && ./zoomcut-cli ui
```

Pick a window. Hit record. Hit stop. That's the whole workflow.

---

## Contents

- [Why](#why)
- [Requirements](#requirements)
- [Install](#install)
- [The app](#the-app)
- [The command line](#the-command-line)
- [How the camera decides](#how-the-camera-decides)
- [Two macOS traps it handles for you](#two-macos-traps-it-handles-for-you)
- [Project files](#project-files)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [How it's built](#how-its-built)
- [License](#license)

## Why

A raw screen recording is hard to watch. The interesting thing is usually 4% of a 4K frame, and
the viewer has no idea where to look. The usual fix is to open an editor and hand-place zooms,
which is slow and easy to overdo — most auto-zoom tools chase the cursor and never stop moving.

Zoomcut takes the opposite position: **stillness is the default and motion has to be earned.**
It finds the handful of moments that deserve a camera move, makes those moves, and otherwise
leaves the frame alone.

## Requirements

| | |
|---|---|
| **OS** | macOS 13+ (built and tested on macOS 26 "Tahoe") |
| **ffmpeg** | `brew install ffmpeg` |
| **Python** | 3.10+ with `numpy` and `Pillow` |
| **Permission** | Screen Recording, for whatever launches Zoomcut |

> **Screen Recording permission**
> System Settings → Privacy & Security → Screen & System Audio Recording → enable your terminal
> (Terminal, iTerm, VS Code…), then **restart that app**. The UI shows a green dot when it's good.

No menu-bar agent, no login item, no telemetry. Nothing leaves your machine — the web app is
bound to `127.0.0.1` and only serves files it produced itself.

## Install

**From a clone** — nothing to install beyond the dependencies:

```bash
git clone https://github.com/ahmedawachi/zoomcut.git
cd zoomcut
pip3 install numpy pillow
./zoomcut-cli ui
```

**As a package**, to get a `zoomcut` command anywhere:

```bash
pip3 install -e .
zoomcut ui
```

## The app

```bash
zoomcut ui            # opens http://127.0.0.1:8765
```

Four steps, top to bottom:

| Step | What you do |
|---|---|
| **1 · Capture** | Pick **a window** (every real window, listed with its size), a display, a region, or let macOS ask. Record → Stop. |
| **2 · Auto-cut** | The shot plan appears as a table. Every number is editable; *Apply edits* re-cuts the camera. |
| **3 · Look** | Background wallpaper, dim, blur, padding, output size, fps. |
| **4 · Preview & export** | Scrub to any moment for a still, render a 720p draft, or export at full quality. |

Finished files land in `~/Movies/Zoomcut`.

## The command line

```bash
zoomcut windows                                  # what you can record
zoomcut shoot --mode window --window 2375        # record → auto-cut → render
zoomcut auto demo.mov                            # existing recording → finished video
zoomcut auto demo.mov --preview                  # fast 720p draft
zoomcut analyze demo.mov -o demo.json            # just the plan, to edit by hand
zoomcut render demo.json -o out.mp4
zoomcut still demo.json 4.5 -o frame.png         # one composited frame
zoomcut wallpapers
```

<details>
<summary><b>All flags</b></summary>

**Look** — on `auto`, `shoot`, `analyze`

| flag | default | what it does |
|---|---|---|
| `--background` | your Mac's wallpaper | wallpaper name, an image path, `gradient` or `color` |
| `--dim` | `0.12` | darken the background, `0`–`1` |
| `--blur` | `0` | soften the background, in pixels |
| `--padding` | `0.0702` | margin around the window, as a fraction of the short edge |
| `--size` | `1440p` | `1440p`, `1080p` or `4k` |
| `--fps` | `60` | output frame rate |
| `--style-preset` | – | an editor project `.json` to take the look from |
| `--preset-zooms` | off | also use that file's zoom ranges instead of the automatic ones |

**Camera** — how eager the auto-director is

| flag | default | what it does |
|---|---|---|
| `--max-zoom` | `1.60` | how tight a zoom may get |
| `--min-zoom` | `1.22` | below this, a move isn't worth making — it stays wide |
| `--context` | `1.20` | breathing room around the action |
| `--min-shot` | `1.00` | shots shorter than this get merged away |
| `--wide-hold` | `1.10` | seconds spent wide after a view change |

**Capture** — on `record` and `shoot`

| flag | what it does |
|---|---|
| `--mode` | `window`, `display`, `region` or `interactive` |
| `--window` | window id from `zoomcut windows` |
| `--region` | `x,y,w,h` in points |
| `--display` | display number, `1` is main |
| `--seconds` | stop automatically after N seconds |
| `--no-cursor` / `--clicks` | hide the pointer / highlight mouse clicks |

</details>

## How the camera decides

```
recording ──▶ decode small + grey at 20fps
                       │
                       ├─▶ per-frame difference ──▶ heat map of what changed
                       │                         └─▶ global spikes  = cuts
                       └─▶ gradient of the mean frame = where the interface is busy
                                                           │
                                     ┌─────────────────────┘
                                     ▼
                     plan shots ──▶ merge runts ──▶ spring ──▶ composite ──▶ h.264
```

1. **Cuts.** A whole-screen change — a view switching, a panel opening — becomes a structural beat.
2. **Shots.** Between cuts it frames the heat. Zoom comes from how spread out the action is;
   position is chosen to **capture the most activity** while keeping the crop's edges on *quiet*
   parts of the interface. That second term is what stops a crop slicing through a sidebar: when
   the action hugs an edge, the best-covering window is the one flush against it, which cuts
   nothing.
3. **Stillness.** One shot per stretch, held exactly. The camera does not creep while nothing is
   happening.
4. **Reveals.** It pulls back to the *whole, uncropped* window slightly **before** a cut lands, so
   a change is never revealed half-framed.
5. **Restraint.** Shots below `--min-shot` are merged, near-identical neighbours fuse, and zooms
   that would be too timid to notice are dropped. It opens and closes on the full window.

Moves are integrated through a **damped spring** (ζ ≈ 0.80) rather than an easing curve, so they
settle like physics instead of like a tween.

The background is dithered before encoding and the encoder runs with `aq-mode=3`. A static
gradient or sky will otherwise band badly in 8-bit 4:2:0 — and because the background never
moves, that banding would sit on screen for the entire clip. Measured on a real export, this
takes flat-runs in the sky from **83px down to 2.4px**.

## Two macOS traps it handles for you

Both are handled automatically. They're documented because they will confuse you if you ever
script `screencapture` yourself.

**1. Recordings get silently truncated.**
`screencapture -v` writes variable frame rate and ends the file at the *last on-screen change*.
Record 10 seconds that finish on a still screen and the file honestly claims about 6. Zoomcut
measures wall-clock time and clones the final frame to match, so the end of your demo never
quietly disappears.

**2. It refuses to write dot-files — and still exits 0.**
`screencapture -x -t png /tmp/.probe.png` writes nothing and reports success. Output names
beginning with `.` are rejected up front rather than failing mysteriously later.

Window capture also passes `-o`, so the window's own drop shadow isn't baked in and Zoomcut's
shadow is the only one in the frame.

## Project files

`zoomcut analyze` writes plain, hand-editable JSON:

```jsonc
{
  "source": "/Users/you/Movies/Zoomcut/capture.mov",
  "trim": [0.0, null],
  "output": { "width": 2560, "height": 1440, "fps": 60, "crf": 17 },
  "style": {
    "background": { "type": "wallpaper", "name": "Tahoe Day", "dim": 0.12, "blur": 0 },
    "paddingRatio": 0.0702,
    "cornerRadius": 0.012,
    "shadow": { "distance": 0.0271, "blur": 0.0215, "alpha": 0.52 }
  },
  "camera": {
    "spring": { "mass": 2.25, "stiffness": 200, "damping": 36 },
    "shots": [ { "start": 0.0, "end": 1.0, "zoom": 1.0, "cx": 0.5, "cy": 0.5, "reason": "open" } ],
    "keys":  [ [0.0, 1.0, 0.5, 0.5], [1.0, 1.0, 0.5, 0.5] ]
  }
}
```

`shots` is the readable plan; `keys` is what the renderer consumes. Edit `shots` in the app and
`keys` are rebuilt for you. `zoom: 1.0` always means the whole, uncropped window; `cx`/`cy` are
`0`–`1` across the recording.

## Tests

```bash
python3 tests/test_all.py            # everything (needs a desktop session)
python3 tests/test_all.py --quick    # skips wallpapers, window list, live recording
```

**119 checks**, covering:

- camera maths — spring convergence and overshoot, crop clamping under hostile input
- director invariants on synthetic clips, including a dead-still one and a strobing one:
  always opens and closes on the full window, shots tile the timeline with no gaps, every crop
  stays inside the frame, keys are monotonic
- edge cases that would ruin a first run — 0.6s clips, 160×120, portrait, missing files,
  dot-file output, region mode with no region, recording a window id that no longer exists
- style-preset import, wallpaper discovery and fuzzy matching
- the renderer, including trims, gradient backgrounds and a **banding check**
- **every web endpoint**, including confirming that `/etc/passwd` and `~/.ssh/id_rsa` are *not*
  servable and that video is served with `Range` support so it can seek
- a **real window recording** taken through the entire record → auto-cut → render cycle

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Permission needed" in the UI | Grant Screen Recording to the app that launched Zoomcut, then restart it |
| The camera zooms somewhere odd | Something else on screen was moving — a clock, a notification. Edit the shot in step 2, or raise `--min-shot` |
| Too much zooming | Lower `--max-zoom`, or raise `--min-zoom` so marginal moves stay wide |
| Zooms feel too tight | Raise `--context` for more breathing room around the action |
| Nothing zooms at all | The whole screen was changing at once, so every beat is a cut. Lower `--wide-hold` |
| Background bands after sharing | A platform re-compressed it. Export `--size 1080p` |
| `ffmpeg not found` | `brew install ffmpeg` |

## How it's built

Pure Python, no framework. `numpy` and `Pillow` do the compositing, `ffmpeg` does codecs, and the
window list comes from CoreGraphics through `ctypes` — so it works on a stock macOS Python with
no `pyobjc`.

| module | what it does |
|---|---|
| `analyze.py` | decodes once, small and grey; builds the heat map, cuts and structure profiles |
| `director.py` | turns that into a shot plan, then into camera keyframes |
| `render.py` | background, shadow, rounded window, spring camera, encode |
| `recorder.py` | `screencapture` wrapper, including the truncation fix |
| `windows.py` | CoreGraphics window list via `ctypes` |
| `wallpapers.py` | finds and caches the wallpapers installed on your Mac |
| `project.py` | the project file, and style-preset import |
| `server.py` + `web/` | the local app |

## License

[MIT](LICENSE) © Ahmed Awachi
