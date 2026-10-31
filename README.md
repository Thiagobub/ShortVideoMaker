# ShortVideoMaker

Turns already-cropped Steam gameplay videos into **YouTube Shorts** (9:16) with one command:

- a **~1:1 crop** of the gameplay, centered in the middle of the frame,
- the top/bottom "black" borders filled with the **same video, zoomed in and blurred**,
- a **small crop of the items/HUD** placed in the bottom border area.

```
+---------------------------+      1080x1920 (9:16)
|   blurred + zoomed bg     |      <- top border, no black bars
|                           |
+---------------------------+
|                           |
|   ~1:1 gameplay crop      |      <- full source height, centered
|   (middle of the screen)  |
|                           |
+---------------------------+
| [items]   blurred bg      |      <- items/HUD panel, bottom left
+---------------------------+
```

Layouts are driven by a **layout file** (`layout.json`): a list of named boxes, each with
a *source* rectangle (what gets cropped from the video) and an *output* rectangle (where it
is placed on the 9:16 canvas). Tune them **visually** with the built-in editor:

```bash
python make_shorts.py --config
```

Left: a paused video frame with the crop boxes. Right: a live preview of the
composed output with the placement boxes. Drag boxes or their handles to move/resize,
add/remove/rename boxes, adjust the background zoom/blur, and save — rendering picks the
saved file up automatically.

## Requirements

- **Python 3.10+**
- **ffmpeg** — either:
  - installed and on your `PATH` (e.g. `winget install ffmpeg`), or
  - the bundled binary from the `imageio-ffmpeg` package:

    ```bash
    python -m pip install imageio-ffmpeg
    ```

    The script picks whichever it finds (system ffmpeg first).
- **Pillow** and **tkinter** — only for the `--config` editor UI
  (`python -m pip install pillow`; tkinter ships with the standard Python installer).

Rendering itself needs nothing but Python and ffmpeg.

## Usage

```bash
python make_shorts.py --config               # open the layout editor (./layout.json)
python make_shorts.py --config my.json       # open the editor on a specific layout file
python make_shorts.py                        # every video in ./input
python make_shorts.py clip.mp4               # one file
python make_shorts.py folder1 folder2        # every video in the folders
python make_shorts.py clip.mp4 -o out        # choose the output folder
python make_shorts.py clip.mp4 --preview 30  # render ONE test frame at 30s (~1 second)
python make_shorts.py clip.mp4 --layout my.json  # render with a specific layout file
python make_shorts.py clip.mp4 --dry-run     # print the ffmpeg command, don't run it
python make_shorts.py --encoder nvidia       # force an encoder: auto | nvidia | amd | intel | cpu
```

### GPU acceleration

By default (`--encoder auto`) rendering uses the first GPU encoder that works on this
machine — **NVIDIA** (`h264_nvenc`), then **AMD** (`h264_amf`), then **Intel**
(`h264_qsv`) — and falls back to the **CPU** (`libx264`) when none does. Each one is
checked with a tiny test encode at startup, since being compiled into ffmpeg doesn't
mean the hardware is present. If a GPU render fails anyway, that video is retried on the
CPU automatically. `CRF` / `PRESET` are mapped to each encoder's closest equivalent.
The filters (crop, scale, blur) still run on the CPU, so the speedup depends on the
layout.

Outputs land in `./output` (or `-o`) as `<name>_short.mp4`.

### Control panel (GUI)

```bash
python gui.py      # or: pythonw gui.py (no console window on Windows)
```

A small window over the same pipeline: lists the videos in `./input` (add more with
*Adicionar…*), picks a layout file, renders with per-video and overall progress, can
cancel (the half-written output is deleted), renders a single test frame (*Prévia*),
opens the layout editor and the output folder. The *Título* / *Crédito* columns hold each
video's `{title}` and `{credit}` (double-click a row to edit them; saved to
`input/titles.json`). The *Aceleração* dropdown shows the
encoders detected on this machine (`Auto → …` names the one it picked) and lets you
choose another; GPU failures fall back to the CPU the same way. With no rows selected, *Renderizar*
processes every row that isn't done yet; with a selection, only those.

## Folders

The working folders are created automatically every time the script starts:

| Folder | Purpose |
|---|---|
| `input/` | Drop the raw videos here (default input when no paths are given) |
| `input/converted/` | Originals are moved here after their short renders successfully |
| `output/` | Rendered shorts land here as `<name>_short.mp4` |
| `output/published/` | Manually move shorts here once they have been uploaded |

When rendering, the layout source is picked in this order: `--layout FILE`, then
`./layout.json` if it exists, then the built-in `CONFIG` block. The command prints which
one it used.

**Tip:** `--preview <seconds>` writes a single PNG of the composed frame in about a second,
so you can verify a layout on paper before encoding whole videos.

## The layout editor (`--config`)

| Where | What |
|---|---|
| **Left panel** | The video paused at the scrubber position. Each box outline is a region cropped **from** the source. |
| **Right panel** | Live preview of the composed 9:16 output (recomposited instantly as you edit). Each box outline is where that crop is **placed** on the canvas. |
| **Time bar** | Scrub to any frame (`‹ 1 frame` / `1 frame ›` steps by a single frame) to check the layout against different moments. |
| **Boxes** | `Add`, `Remove`, `Rename`, `↑`/`↓` reorder (later in the list draws on top). |
| **Undo / Redo** | Step backwards/forwards through every change. Buttons in the top bar (hover them for a hint) or `Ctrl+Z` / `Ctrl+Y` (`Ctrl+Shift+Z` also works). One drag or one slider scrub = one step. |
| **Selected box** | Exact `x y w h` fractions for the source crop and the output placement, plus `visible` and `keep crop aspect` (output height follows the crop's shape — matches `scale=W:-2`). |
| **Background & output** | Blurred-background on/off, zoom, blur, output canvas size, CRF, x264 preset. |
| **Render exact frame** | Renders the current frame through the **real** ffmpeg filter graph (~1 s) and shows it next to the command — the live preview approximates the blur, this is pixel-exact. |
| **Texts & images** | Tick *this layout has texts / images* to put a title, a credit line or a logo on top of the short (the first tick adds a credit + title pair to start from; unticking keeps them but stops drawing them). `+ Text` / `+ Image` add more; drag their dashed rectangles on the preview. Pick any installed Windows font (or a `.ttf` with `…` / in `./fonts`), size, text color, box color, outline and alignment. `{title}` and `{credit}` are filled per video; the *preview* fields only drive the editor preview. |
| **Save layout** | Writes the JSON. It is used automatically next time you render (or pass `--layout`). |

Interactions: drag inside a box to move it, drag its corner/edge handles to resize,
`Delete` removes the selected box, `Ctrl+S` saves, `Ctrl+Z` undoes the last change,
`Ctrl+Y` (or `Ctrl+Shift+Z`) redoes it. A fresh editor starts from the
built-in `CONFIG` numbers, scaled to the first video you load.

## layout.json reference

```json
{
  "version": 1,
  "output":    { "width": 1080, "height": 1920, "crf": 18, "preset": "medium" },
  "background": { "enabled": true, "zoom": 1.4, "blur": 40.0 },
  "boxes": [
    {
      "name": "main", "visible": true, "keep_aspect": true,
      "src": { "x": 0.2572, "y": 0.0, "w": 0.4856, "h": 1.0 },
      "out": { "x": 0.0,   "y": 0.2575, "w": 1.0, "h": 0.4849 }
    }
  ]
}
```

- Every `x y w h` is a fraction (`0.0` – `1.0`) of the frame it refers to: `src` of the
  source video, `out` of the output canvas.
- `keep_aspect: true` scales the box to the output width and derives the height from the
  crop's shape (the old `scale=W:-2` behavior). `false` stretches it to `w` × `h`.
- Boxes are drawn in list order — later boxes render **on top** of earlier ones.
- A source crop must stay inside the frame; an output placement may run past the canvas
  edge (it gets clipped, which is how the items panel sits at the bottom).
- `overlays` (drawn only when `overlays_enabled` is true) are texts and images on top of
  everything, each with an `out` rectangle:

  ```json
  "overlays_enabled": true,
  "overlays": [
    { "type": "text", "visible": true, "text": "CREDIT: {credit}", "font": "impact.ttf",
      "size": 0.03, "color": "#000000", "bg_enabled": true, "bg": "#FFFFFF",
      "outline": false, "align": "center",
      "out": { "x": 0.05, "y": 0.05, "w": 0.9, "h": 0.06 } },
    { "type": "image", "visible": true, "path": "logo.png",
      "out": { "x": 0.6, "y": 0.82, "w": 0.35, "h": 0.1 } }
  ]
  ```

  A text wraps to its rectangle's width and shrinks (from `size`, a fraction of the output
  height) until it fits; `{title}` / `{credit}` come from `input/titles.json`, and a text
  whose variable is empty for a video is skipped. An image is fitted inside its rectangle.
  Texts and still images are drawn with Pillow into transparent PNGs laid over the video,
  so rendering a layout with overlays needs Pillow too.
- An `image` whose `path` is a **GIF or a video** is animated: ffmpeg loops it
  (`-stream_loop -1`) for the whole short, fitted inside its rectangle, and the short
  still ends with the gameplay. Its own audio is ignored. GIF transparency is kept. The
  editor preview shows its first frame; *Render exact frame* shows the real thing.
  Overlays stack in list order, so a text after a GIF/video is drawn on top of it.

## CONFIG reference

All `*_PCT` values are fractions (`0.0` – `1.0`) of the frame they refer to.
All of these live in the `CONFIG` block at the top of `make_shorts.py`. They are used
when no layout file exists yet, and they seed a brand-new layout in the editor.

| Constant | Default | Meaning |
|---|---|---|
| `OUTPUT_W`, `OUTPUT_H` | `1080`, `1920` | Output canvas (YouTube Shorts is 9:16) |
| `CRF` | `18` | x264 quality — lower is better quality / bigger file |
| `PRESET` | `medium` | x264 speed (`veryfast` … `veryslow`) |
| `MAIN_ASPECT` | `1.0` | Crop width = source height × this (1.0 = perfect square) |
| `MAIN_CENTER_X_PCT` | `0.5` | Where the crop sits **in the source**: 0 = left edge, 1 = right edge |
| `MAIN_WIDTH_PCT` | `1.0` | Panel width on the output canvas (1.0 = full 1080 px) |
| `MAIN_CENTER_Y_PCT` | `0.5` | Panel position on the output canvas: 0 = top, 1 = bottom |
| `BG_ZOOM` | `1.4` | Background zoom (1.0 = plain cover fit, no extra zoom) |
| `BG_BLUR` | `40.0` | Background blur strength (gblur sigma — bigger = smoother) |
| `ITEMS_SRC_X_PCT` | `0.02` | Items crop top-left X **in the source** |
| `ITEMS_SRC_Y_PCT` | `0.70` | Items crop top-left Y in the source (0.70 ≈ bottom-left area) |
| `ITEMS_SRC_W_PCT` | `0.32` | Items crop width, as a fraction of source width |
| `ITEMS_SRC_H_PCT` | `0.28` | Items crop height, as a fraction of source height |
| `ITEMS_OUT_W_PCT` | `0.55` | Items panel display width on the output canvas (height keeps the crop's aspect) |
| `ITEMS_OUT_X_PCT` | `0.02` | Items panel X on the output canvas |
| `ITEMS_OUT_Y_PCT` | `0.80` | Items panel Y on the output canvas (0.80 = inside the bottom border) |
| `INPUT_DIR` | `input` | Default input folder when no paths are given |
| `CONVERTED_DIR` | `input/converted` | Originals are moved here after a successful render |
| `OUTPUT_DIR` | `output` | Default output folder |
| `PUBLISHED_DIR` | `output/published` | Drop zone for shorts that have already been uploaded |

Example: zoom the background less and shrink the items panel:

```python
BG_ZOOM = 1.2
ITEMS_OUT_W_PCT = 0.4
```

## Output format

- H.264 (`libx264`), CRF 18, `yuv420p`, square pixels — safe for YouTube
- AAC audio, 192 kbps (copied from the original, re-encoded)
- `+faststart` so the file streams nicely when uploaded
- The audio stream is optional — silent inputs work fine

## Troubleshooting

**`error: ffmpeg not found`**
Install ffmpeg and put it on your `PATH`, or run `python -m pip install imageio-ffmpeg`.

**The items panel is in the wrong place**
Adjust `ITEMS_SRC_*` (where it is cropped *from* in your video) and `ITEMS_OUT_*`
(where it is placed *on* the final video) separately — use `--preview` to check quickly.

**Crop is off-center for my resolution**
The crop is expressed relative to the source frame, so it works at any resolution.
Nudge `MAIN_CENTER_X_PCT` (e.g. `0.45` or `0.55`) to move the window left or right.

**Output looks stretched**
`MAIN_ASPECT` sets the crop's width:height ratio (width = source height × `MAIN_ASPECT`).
The panel keeps that ratio on the output canvas, so `1.0` is always a square.
