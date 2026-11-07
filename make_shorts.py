#!/usr/bin/env python3
"""
make_shorts.py - turn already-cropped Steam gameplay videos into YouTube Shorts.

For every input video it builds this 9:16 canvas (1080x1920):

    +---------------------------+
    |   blurred + zoomed bg     |   <- top "black border" filled with the
    |                           |      same video, zoomed in and blurred
    +---------------------------+
    |                           |
    |   ~1:1 gameplay crop      |   <- full source height, centered in the
    |   (middle of the screen)  |      middle of the canvas
    |                           |
    +---------------------------+
    | [items]   blurred bg      |   <- small crop of the items/HUD, placed
    +---------------------------+      in the bottom "black border" area

Everything (sizes, positions, zoom, blur) is driven by a *layout*: either
the CONFIG block below or a layout.json file written by the visual editor.
Use --config to open the editor (pause a video, drag the crop/placement
boxes around, watch the preview update live, save the layout). When a
layout file exists it is used automatically when rendering. Use
--preview to render single frames first (takes ~1 second) so you can
fine-tune before rendering full videos.

Usage:
    python make_shorts.py --config               # open the layout editor UI
    python make_shorts.py --config my.json       # edit a specific layout file
    python make_shorts.py                        # every video in ./input
    python make_shorts.py clip.mp4               # one file
    python make_shorts.py folder1 folder2        # every video in folders
    python make_shorts.py clip.mp4 --preview 30  # render 1 test frame at 30s
    python make_shorts.py clip.mp4 --layout my.json  # render with a layout file
    python make_shorts.py clip.mp4 --dry-run     # print the ffmpeg command
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# ============================== CONFIG =================================
# Tweak these numbers and re-run. All "*_PCT" values are fractions
# (0.0 -> 1.0) of the frame they refer to.

OUTPUT_W, OUTPUT_H = 1080, 1920  # YouTube Shorts canvas (9:16)
CRF = 18                         # x264 quality (lower = better/bigger)
PRESET = "medium"                # x264 speed

# ---- MAIN PANEL: the ~1:1 crop of the gameplay, in the middle --------
MAIN_ASPECT = 1.16        # crop width = source_height * this (1.0 = perfect square)
MAIN_CENTER_X_PCT = 0.5  # crop position in the SOURCE: 0 = left edge, 1 = right edge
MAIN_WIDTH_PCT = 1.0     # panel width on the output canvas (1.0 = full 1080 px)
MAIN_CENTER_Y_PCT = 0.5  # panel position on the output canvas: 0 = top, 1 = bottom

# ---- BACKGROUND: fills the top/bottom "black" borders ----------------
BG_ZOOM = 1.4    # how much the background is zoomed in (1.0 = plain cover fit)
BG_BLUR = 40.0   # blur strength (gblur sigma, bigger = smoother)

# ---- ITEMS PANEL: small rectangle of the items/HUD -------------------
# Where it is cropped FROM in the source video (top-left corner + size):
ITEMS_SRC_X_PCT = 0.008
ITEMS_SRC_Y_PCT = 0.722
ITEMS_SRC_W_PCT = 0.175
ITEMS_SRC_H_PCT = 0.265
# Where it is placed ON the output canvas (top-left corner of the panel):
ITEMS_OUT_W_PCT = 0.65   # display width (height keeps the crop's aspect)
ITEMS_OUT_X_PCT = 0.02
ITEMS_OUT_Y_PCT = 0.76   # 0.80 = inside the bottom border area

INPUT_DIR = "input"                # used when no input paths are given on the CLI
CONVERTED_DIR = "input/converted"  # originals are moved here after a successful render
OUTPUT_DIR = "output"              # default output folder
PUBLISHED_DIR = "output/published"  # move finished shorts here after uploading them

DEFAULT_LAYOUT_NAME = "layout.json"  # written by --config, read when rendering

# ---- ENCODERS: GPU acceleration, with the CPU (x264) as the fallback ---
ENCODERS = {  # key -> label shown to the user
    "nvidia": "NVIDIA (NVENC)",
    "amd": "AMD (AMF)",
    "intel": "Intel (QSV)",
    "cpu": "CPU (x264)",
}
GPU_ORDER = ("nvidia", "amd", "intel")  # "auto" picks the first one that works

# ---- OVERLAYS: texts/images drawn on top of the short ----------------
TITLES_FILE = "input/titles.json"  # per-video {title}/{credit} values, typed in gui.py
FONTS_DIR = "fonts"                # optional extra .ttf/.otf files for the overlays
DEFAULT_FONT = "impact.ttf"        # ships with Windows
# ======================================================================

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".mts",
}


def even(value: float) -> int:
    """Round to the nearest even integer (yuv420p wants even dimensions)."""
    return int(round(value / 2.0) * 2)


def num(value: float) -> str:
    """Format a number for the filter graph (no trailing zeros)."""
    return f"{value:.6g}"


def find_ffmpeg() -> str | None:
    """ffmpeg from PATH, or the binary bundled with imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def blurred_bg(width: int, height: int, zoom: float, blur: float) -> str:
    """Filter chain for the zoomed "cover" background, blurred.
    A heavy blur wipes out fine detail anyway, so it is done on a frame shrunk
    by up to 8x (with the sigma shrunk to match) and scaled back up: it looks
    the same and is far cheaper than blurring the full 1080x1920 frame."""
    f = max(1, min(8, int(blur // 6)))
    w, h = even(width / f), even(height / f)
    chain = (f"scale={even(w * zoom)}:{even(h * zoom)}:force_original_aspect_ratio=increase,"
             f"crop={w}:{h}")
    if blur > 0:
        chain += f",gblur=sigma={num(blur / f)}"
    if f > 1:
        chain += f",scale={width}:{height}"
    return chain


def build_filter_complex() -> str:
    """Build the ffmpeg filter graph from the CONFIG numbers."""
    # main panel geometry on the output canvas
    main_w = even(OUTPUT_W * MAIN_WIDTH_PCT)
    main_h = main_w / MAIN_ASPECT
    main_x = even((OUTPUT_W - main_w) / 2)
    main_y = even((OUTPUT_H - main_h) * MAIN_CENTER_Y_PCT)

    # items panel geometry on the output canvas
    items_w = even(OUTPUT_W * ITEMS_OUT_W_PCT)
    items_x = even(OUTPUT_W * ITEMS_OUT_X_PCT)
    items_y = even(OUTPUT_H * ITEMS_OUT_Y_PCT)

    # crop boxes, expressed in source pixels from the *_PCT values.
    # (commas inside expressions must be escaped as \, for ffmpeg)
    main_crop = (
        f"crop=w=min(floor(ih*{num(MAIN_ASPECT)}/2)*2\\,floor(iw/2)*2)"
        f":h=ih:x=(iw-ow)*{num(MAIN_CENTER_X_PCT)}:y=0"
    )
    items_crop = (
        f"crop=w=floor(iw*{num(ITEMS_SRC_W_PCT)}/2)*2"
        f":h=floor(ih*{num(ITEMS_SRC_H_PCT)}/2)*2"
        f":x=iw*{num(ITEMS_SRC_X_PCT)}:y=ih*{num(ITEMS_SRC_Y_PCT)}"
    )

    return (
        f"[0:v]split=3[bg_in][main_in][items_in];"
        f"[bg_in]{blurred_bg(OUTPUT_W, OUTPUT_H, BG_ZOOM, BG_BLUR)}[bg];"
        f"[main_in]{main_crop},scale={main_w}:-2[main];"
        f"[items_in]{items_crop},scale={items_w}:-2[items];"
        f"[bg][main]overlay=x={main_x}:y={main_y}[tmp];"
        f"[tmp][items]overlay=x={items_x}:y={items_y},setsar=1[vout]"
    )


# ============================ LAYOUT MODEL ============================
# A "layout" is the JSON form of the CONFIG block: output canvas settings,
# background settings and a list of named boxes. Each box has a source
# rectangle (where it is cropped FROM, fractions of the source frame) and
# an output rectangle (where it is placed ON the canvas, fractions of the
# output frame). Boxes are drawn in list order (later = on top).


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def default_layout(src_w: int = 1920, src_h: int = 1080) -> dict:
    """Layout equivalent to the CONFIG block above, for a src_w x src_h source."""
    main_w = min(MAIN_ASPECT * src_h / src_w, 1.0)
    main_out_h = (OUTPUT_W * MAIN_WIDTH_PCT / MAIN_ASPECT) / OUTPUT_H
    items_out_h = (
        ITEMS_OUT_W_PCT * OUTPUT_W
        * (ITEMS_SRC_H_PCT * src_h) / (ITEMS_SRC_W_PCT * src_w)
        / OUTPUT_H
    )
    return {
        "version": 1,
        "output": {"width": OUTPUT_W, "height": OUTPUT_H, "crf": CRF, "preset": PRESET},
        "background": {"enabled": True, "zoom": BG_ZOOM, "blur": BG_BLUR},
        "boxes": [
            {
                "name": "main", "visible": True, "keep_aspect": True,
                "src": {"x": round(MAIN_CENTER_X_PCT * (1.0 - main_w), 6), "y": 0.0,
                        "w": round(main_w, 6), "h": 1.0},
                "out": {"x": 0.0, "y": round((1.0 - main_out_h) * MAIN_CENTER_Y_PCT, 6),
                        "w": MAIN_WIDTH_PCT, "h": round(main_out_h, 6)},
            },
            {
                "name": "items", "visible": True, "keep_aspect": True,
                "src": {"x": ITEMS_SRC_X_PCT, "y": ITEMS_SRC_Y_PCT,
                        "w": ITEMS_SRC_W_PCT, "h": ITEMS_SRC_H_PCT},
                "out": {"x": ITEMS_OUT_X_PCT, "y": ITEMS_OUT_Y_PCT,
                        "w": ITEMS_OUT_W_PCT, "h": round(items_out_h, 6)},
            },
        ],
    }


def normalize_layout(raw: dict) -> dict:
    """Validate a layout dict and return a clean copy (raises ValueError)."""
    if not isinstance(raw, dict):
        raise ValueError("layout: top level must be a JSON object")

    out_raw = raw.get("output") or {}
    output = {
        "width": int(out_raw.get("width", OUTPUT_W)),
        "height": int(out_raw.get("height", OUTPUT_H)),
        "crf": int(out_raw.get("crf", CRF)),
        "preset": str(out_raw.get("preset", PRESET)),
    }
    if output["width"] < 2 or output["height"] < 2:
        raise ValueError("layout: output width/height must be at least 2")

    bg_raw = raw.get("background") or {}
    background = {
        "enabled": bool(bg_raw.get("enabled", True)),
        "zoom": max(1.0, float(bg_raw.get("zoom", BG_ZOOM))),
        "blur": max(0.0, float(bg_raw.get("blur", BG_BLUR))),
    }

    boxes_raw = raw.get("boxes")
    if not isinstance(boxes_raw, list):
        raise ValueError("layout: missing 'boxes' list")

    boxes = []
    for i, box_raw in enumerate(boxes_raw):
        if not isinstance(box_raw, dict):
            raise ValueError(f"layout: box #{i + 1} must be a JSON object")
        name = str(box_raw.get("name") or f"box {i + 1}")

        def rect(key: str, must_fit: bool) -> dict:
            r = box_raw.get(key)
            if not isinstance(r, dict):
                raise ValueError(f"layout: box '{name}' is missing its '{key}' rectangle")
            w = max(clamp01(r.get("w", 0)), 0.001)
            h = max(clamp01(r.get("h", 0)), 0.001)
            x = clamp01(r.get("x", 0))
            y = clamp01(r.get("y", 0))
            if must_fit:  # the source crop must lie inside the frame
                x = min(x, 1.0 - w)
                y = min(y, 1.0 - h)
            return {"x": x, "y": y, "w": w, "h": h}

        boxes.append({
            "name": name,
            "visible": bool(box_raw.get("visible", True)),
            "keep_aspect": bool(box_raw.get("keep_aspect", False)),
            "src": rect("src", must_fit=True),
            "out": rect("out", must_fit=False),
        })

    overlays = [normalize_overlay(ov, i) for i, ov in enumerate(raw.get("overlays") or [])]

    return {
        "version": 1, "output": output, "background": background, "boxes": boxes,
        "overlays_enabled": bool(raw.get("overlays_enabled", bool(overlays))),
        "overlays": overlays,
    }


# ============================== OVERLAYS ==============================
# Texts and images drawn on top of the composed short. Each overlay has an
# "out" rectangle (fractions of the output canvas) like a box. A text is
# wrapped to the rectangle's width and shrunk until it fits its height, so
# titles of any length stay inside it. "{title}" and "{credit}" in a text
# are replaced per video (TITLES_FILE); an overlay whose variable is empty
# is skipped. Texts and still images are drawn with Pillow into transparent
# PNGs, which ffmpeg lays over the video - the editor preview uses the same
# code. GIFs and videos ("media") are extra ffmpeg inputs looped forever and
# fitted into their rectangle; the short still ends with the gameplay.

FONT_EXTS = {".ttf", ".otf", ".ttc"}
ANIMATED_EXTS = {".gif"} | VIDEO_EXTS

# "animate": the stop-motion wobble of Deadlock's ability cards - the item snaps
# to a new small tilt (on top of its own rotation) a couple of times a second
ANIM_FPS = 2          # poses per second
ANIM_DEGREES = 4.0    # largest tilt added, either way
ANIM_POSES = 8        # poses before the wobble repeats
# what wobbles on a text: box + text in sync, box + text each on its own, box, text
ANIM_TARGETS = ("all", "apart", "box", "text")
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def normalize_overlay(raw: dict, i: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"layout: overlay #{i + 1} must be a JSON object")
    kind = raw.get("type", "text")
    if kind not in ("text", "image"):
        raise ValueError(f"layout: overlay #{i + 1} has unknown type '{kind}'")
    r = raw.get("out") or {}
    w = max(clamp01(r.get("w", 0.5)), 0.005)
    h = max(clamp01(r.get("h", 0.1)), 0.005)
    ov = {
        "type": kind,
        "visible": bool(raw.get("visible", True)),
        "out": {"x": clamp01(r.get("x", 0)), "y": clamp01(r.get("y", 0)), "w": w, "h": h},
        # degrees clockwise around the rectangle's center, kept in -180..180
        "rotation": (float(raw.get("rotation", 0)) + 180) % 360 - 180,
        "animate": bool(raw.get("animate", False)),
        "anim_target": raw.get("anim_target") if raw.get("anim_target") in ANIM_TARGETS else "all",
    }
    if kind == "image":
        ov["path"] = str(raw.get("path", ""))
        return ov

    def color(key: str, default: str) -> str:
        value = str(raw.get(key, default))
        return value if HEX_COLOR_RE.fullmatch(value) else default

    ov.update({
        "text": str(raw.get("text", "")),
        "font": str(raw.get("font") or DEFAULT_FONT),
        "size": max(0.005, min(0.5, float(raw.get("size", 0.045)))),  # fraction of output height
        "color": color("color", "#FFFFFF"),
        "bg_enabled": bool(raw.get("bg_enabled", True)),
        "bg": color("bg", "#000000"),
        "outline": bool(raw.get("outline", False)),
        "align": raw.get("align") if raw.get("align") in ("left", "center", "right") else "center",
    })
    return ov


def example_overlays() -> list[dict]:
    """A credit line + a title above the gameplay (the default CONFIG leaves ~25% free on top)."""
    return [
        normalize_overlay({"type": "text", "text": "CREDIT: {credit}", "size": 0.03,
                           "color": "#000000", "bg": "#FFFFFF",
                           "out": {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.06}}, 0),
        normalize_overlay({"type": "text", "text": "{title}", "size": 0.05,
                           "color": "#FFFFFF", "bg": "#000000",
                           "out": {"x": 0.05, "y": 0.12, "w": 0.9, "h": 0.12}}, 1),
    ]


def font_dirs() -> list[Path]:
    dirs = [Path(FONTS_DIR)]
    if sys.platform == "win32":
        dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
        if os.environ.get("LOCALAPPDATA"):
            dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    else:
        dirs += [Path.home() / ".fonts", Path("/usr/share/fonts"), Path("/Library/Fonts")]
    return dirs


def resolve_font(name: str) -> Path | None:
    """A layout's font (file name, or a full path) -> the file on this machine."""
    path = Path(name)
    if path.is_absolute() or path.parent != Path("."):
        return path if path.exists() else None  # a full or project-relative path
    for d in font_dirs():
        if (d / name).exists():
            return d / name
    # ./fonts may be organized in subfolders (e.g. fonts/deadlock/)
    return next(Path(FONTS_DIR).rglob(name), None) if Path(FONTS_DIR).is_dir() else None


def custom_fonts() -> list[tuple[str, dict[str, str]]]:
    """The fonts dropped in ./fonts, grouped by subfolder: [(group, {display name ->
    project-relative path})], loose files first under "fonts". The path keeps the
    layout pointing at this exact file even if an installed font has the same name."""
    root = Path(FONTS_DIR)
    groups: dict[str, dict[str, str]] = {}
    if root.is_dir():
        for f in sorted(root.rglob("*"), key=lambda p: p.as_posix().lower()):
            if f.suffix.lower() in FONT_EXTS:
                rel = f.parent.relative_to(root).as_posix()
                group = "fonts" if rel == "." else rel
                groups.setdefault(group, {})[f.stem] = f.as_posix()
    return sorted(groups.items(), key=lambda kv: (kv[0] != "fonts", kv[0].lower()))


def system_fonts() -> dict[str, str]:
    """Display name -> font file name, for the fonts installed on this machine."""
    fonts: dict[str, str] = {}
    if sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            except OSError:
                continue
            with key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name, file, _ = winreg.EnumValue(key, i)
                    if isinstance(file, str) and Path(file).suffix.lower() in FONT_EXTS:
                        fonts[re.sub(r"\s*\((TrueType|OpenType)\)$", "", name)] = Path(file).name
    for d in font_dirs():
        if d.is_dir() and d != Path(FONTS_DIR) and sys.platform != "win32":
            for f in d.rglob("*"):
                if f.suffix.lower() in FONT_EXTS:
                    fonts.setdefault(f.stem, f.name)
    return dict(sorted(fonts.items(), key=lambda kv: kv[0].lower()))


@functools.lru_cache(maxsize=64)
def load_font(name: str, size: int):
    from PIL import ImageFont

    path = resolve_font(name)
    if path:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def fill_text(template: str, values: dict) -> str | None:
    """Replace {title}/{credit}; None when a variable used is empty (the overlay is skipped)."""
    missing = False

    def sub(m: re.Match) -> str:
        nonlocal missing
        value = str(values.get(m[1], "")).strip()
        missing = missing or not value
        return value

    text = re.sub(r"\{(\w+)\}", sub, template)
    return None if missing or not text.strip() else text


def _wrap(draw, text: str, font, width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            test = f"{line} {word}".strip()
            if line and draw.textlength(test, font=font) > width:
                lines.append(line)
                line = word
            else:
                line = test
        lines.append(line)
    return lines


@functools.lru_cache(maxsize=256)
def _fit_text(text: str, font_name: str, size: int, bw: float, bh: float, bg_enabled: bool,
              outline: bool) -> tuple:
    """Largest size <= `size` whose wrapped text fits a bw x bh rectangle:
    (font, pad, stroke, lines, widths, line height, block w, block h). Cached: the
    editor redraws the same texts several times a second while they wobble."""
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while True:
        font = load_font(font_name, size)
        pad = round(size * 0.3) if bg_enabled else 0
        stroke = max(1, round(size * 0.08)) if outline else 0
        lines = _wrap(draw, text, font, bw - 2 * pad)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        widths = [draw.textlength(line, font=font) + 2 * stroke for line in lines]
        block_w = max(widths) + 2 * pad
        block_h = line_h * len(lines) + 2 * pad
        if (block_w <= bw and block_h <= bh) or size <= 4:
            return font, pad, stroke, lines, widths, line_h, block_w, block_h
        size = max(4, int(size * 0.92))


# Overlay pieces are drawn only as big as what they show and cached, and every
# turn of a piece is cached too: a wobbling item repeats the same few poses (and
# its other part often doesn't move at all), so each pose is drawn and turned
# once instead of on every frame of the editor preview / every pose of a render.
@functools.lru_cache(maxsize=64)
def _text_pieces(text: str, font_name: str, size: int, color: str, bg_enabled: bool, bg: str,
                 outline: bool, align: str, rect: tuple[float, float, float, float]) -> tuple:
    """(box, letters, both, origin) of a text laid out in rect = (x, y, w, h) canvas
    pixels: images just as big as the drawn block (+ a margin for glyph overhangs),
    all with their top-left corner at `origin` on the canvas. box is None without
    a background; both = the letters on the box."""
    from PIL import Image, ImageDraw

    bx, by, bw, bh = rect
    font, pad, stroke, lines, widths, line_h, block_w, block_h = _fit_text(
        text, font_name, size, bw, bh, bg_enabled, outline)
    if align == "left":
        x0 = bx
    elif align == "right":
        x0 = bx + bw - block_w
    else:
        x0 = bx + (bw - block_w) / 2
    y0 = by + (bh - block_h) / 2
    margin = max(4, size // 3)
    ox, oy = math.floor(x0) - margin, math.floor(y0) - margin
    w, h = math.ceil(x0 + block_w) + margin - ox, math.ceil(y0 + block_h) + margin - oy
    x0, y0 = x0 - ox, y0 - oy  # from here on, in the pieces' coordinates

    box = None
    if bg_enabled:
        box = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(box).rectangle((x0, y0, x0 + block_w, y0 + block_h), fill=bg)
    letters = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(letters)
    inner_w = block_w - 2 * pad
    for i, (line, lw) in enumerate(zip(lines, widths)):
        if align == "left":
            lx = x0 + pad
        elif align == "right":
            lx = x0 + pad + inner_w - lw
        else:
            lx = x0 + pad + (inner_w - lw) / 2
        draw.text((lx + stroke, y0 + pad + i * line_h), line, font=font, fill=color,
                  stroke_width=stroke, stroke_fill="#000000")
    both = letters if box is None else Image.alpha_composite(box, letters)
    return box, letters, both, (ox, oy)


@functools.lru_cache(maxsize=32)
def _image_piece(path: str, mtime: float, rect: tuple[float, float, float, float]) -> tuple:
    """(picture fitted inside rect keeping its aspect, origin) - a video/GIF shows
    its first frame - or (None, None) when the file can't be read."""
    from PIL import Image

    try:
        if Path(path).suffix.lower() in VIDEO_EXTS:
            img = _media_still(path, mtime)
            if img is None:
                return None, None
        else:
            img = Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        return None, None
    bx, by, bw, bh = rect
    scale = min(bw / img.width, bh / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.LANCZOS), (round(bx + (bw - size[0]) / 2), round(by + (bh - size[1]) / 2))


@functools.lru_cache(maxsize=96)
def _turned(maker, args: tuple, part: int, angle: float):
    """Piece `part` of maker(*args), turned `angle` degrees clockwise about its own
    center (grown to fit)."""
    from PIL import Image

    return maker(*args)[part].rotate(-angle, resample=Image.BICUBIC, expand=True)


def _composite(canvas, maker, args: tuple, part: int, pivot: tuple[float, float],
               angle: float) -> None:
    """Lay piece `part` of maker(*args) (an image + its canvas origin, see
    _text_pieces) on the canvas, turned `angle` degrees clockwise about `pivot` -
    the center of the overlay's rectangle."""
    pieces = maker(*args)
    img = pieces[part]
    if img is None:
        return
    ox, oy = pieces[-1]
    if angle:
        w, h = img.size
        img = _turned(maker, args, part, angle)
        # the piece's center swings around the pivot (y points down: clockwise)
        a = math.radians(angle)
        cx, cy = ox + w / 2 - pivot[0], oy + h / 2 - pivot[1]
        ox = round(pivot[0] + cx * math.cos(a) - cy * math.sin(a) - img.width / 2)
        oy = round(pivot[1] + cx * math.sin(a) + cy * math.cos(a) - img.height / 2)
    # alpha_composite takes no negative destination: cut off what is left of / above the canvas
    sx, sy = max(0, -ox), max(0, -oy)
    if sx < img.width and sy < img.height:
        canvas.alpha_composite(img, dest=(ox + sx, oy + sy), source=(sx, sy))


def _rect_px(canvas, ov: dict) -> tuple[float, float, float, float]:
    cw, ch = canvas.size
    r = ov["out"]
    return r["x"] * cw, r["y"] * ch, r["w"] * cw, r["h"] * ch


def _draw_text(canvas, ov: dict, values: dict) -> None:
    text = fill_text(ov["text"], values)
    if text is None:
        return
    rect = _rect_px(canvas, ov)
    pivot = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
    args = (text, ov["font"], max(4, round(ov["size"] * canvas.size[1])), ov["color"],
            ov["bg_enabled"], ov["bg"], ov["outline"], ov["align"], rect)
    rotation = ov.get("rotation", 0)
    box_tilt, text_tilt = ov.get("_tilt_box", 0.0), ov.get("_tilt_text", 0.0)
    if ov["bg_enabled"] and box_tilt != text_tilt:  # the box wobbles apart from the letters
        _composite(canvas, _text_pieces, args, 0, pivot, rotation + box_tilt)
        _composite(canvas, _text_pieces, args, 1, pivot, rotation + text_tilt)
    else:
        tilt = box_tilt if ov["bg_enabled"] else text_tilt
        _composite(canvas, _text_pieces, args, 2, pivot, rotation + tilt)


def is_media(ov: dict) -> bool:
    """A GIF or a video: looped by ffmpeg instead of drawn as a still."""
    return ov["type"] == "image" and Path(ov["path"]).suffix.lower() in ANIMATED_EXTS


@functools.lru_cache(maxsize=16)
def _media_still(path: str, mtime: float):
    """First frame of a video (for previews), via ffmpeg."""
    import io

    from PIL import Image

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path, "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )
    if res.returncode != 0 or not res.stdout:
        return None
    return Image.open(io.BytesIO(res.stdout)).convert("RGBA")


def _draw_image(canvas, ov: dict) -> None:
    path = Path(ov["path"])
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    rect = _rect_px(canvas, ov)
    pivot = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
    _composite(canvas, _image_piece, (str(path), mtime, rect), 0, pivot,
               ov.get("rotation", 0) + ov.get("_tilt_box", 0.0))


@functools.lru_cache(maxsize=256)
def wobble_tilts(seed: int) -> tuple[float, ...]:
    """The ANIM_POSES tilts of one item (seeded by its place in the list, so every
    item wobbles on its own); each pose differs clearly from the one before."""
    import random

    rng = random.Random(seed * 7919 + 17)
    tilts: list[float] = []
    while len(tilts) < ANIM_POSES:
        t = rng.uniform(-ANIM_DEGREES, ANIM_DEGREES)
        if not tilts or abs(t - tilts[-1]) >= ANIM_DEGREES / 2:
            tilts.append(t)
    return tuple(tilts)


def wobble_pose(ov: dict, seed: int, step: int) -> dict:
    """The overlay as drawn at wobble half-step `step` (2 per pose; unchanged when it
    isn't animated): the tilt goes to the box and/or the text of a text as its
    anim_target says (an image is all "box"). "apart" gives the text tilts of its
    own that change half a pose after the box's, so the two never move together."""
    if not ov.get("animate"):
        return ov
    tilt = wobble_tilts(seed)[step // 2 % ANIM_POSES]
    target = ov.get("anim_target", "all") if ov["type"] == "text" else "all"
    if target == "apart":
        text_tilt = wobble_tilts(seed + 1000)[(step + 1) // 2 % ANIM_POSES]
        return {**ov, "_tilt_box": tilt, "_tilt_text": text_tilt}
    return {**ov, "_tilt_box": tilt if target in ("all", "box") else 0.0,
            "_tilt_text": tilt if target in ("all", "text") else 0.0}


# the cached fonts and pieces are shared: one thread draws at a time (the GUI
# prepares the next video's overlays while ffmpeg renders, and may run a preview)
_DRAW_LOCK = threading.RLock()


def render_overlays(layout: dict, values: dict, size: tuple[int, int], items: list | None = None,
                    step: int = 0):
    """Overlays on a transparent RGBA image of `size`, or None. By default all the
    layout's visible ones (GIFs/videos as their first frame, animated ones at wobble
    half-step `step`, for previews); `items` are drawn exactly as given."""
    with _DRAW_LOCK:
        return _render_overlays(layout, values, size, items, step)


def _render_overlays(layout: dict, values: dict, size: tuple[int, int], items: list | None,
                     step: int):
    if items is None:
        if not layout.get("overlays_enabled"):
            return None
        items = [wobble_pose(ov, i, step) for i, ov in enumerate(layout.get("overlays", []))
                 if ov["visible"]]
    if not items:
        return None
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("texts/images need Pillow: python -m pip install pillow") from exc
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for ov in items:
        if ov["type"] == "text":
            _draw_text(canvas, ov, values)
        else:
            _draw_image(canvas, ov)
    return canvas


def overlay_layers(layout: dict, values: dict, tmp_prefix: Path,
                   still: bool = False) -> tuple[list, list[str]]:
    """Split one video's overlays into ffmpeg layers, keeping their stacking order.
    still=True (a single-frame preview) draws animated texts/stills in their first
    pose instead of preparing every pose.

    Returns (layers, input args): one layer per extra input (1, 2, ...) - None for a
    full-canvas PNG of consecutive texts/stills, {"anim", "x", "y"} for the looped
    wobble poses of an animated text/still (cropped PNGs placed at x, y), or the
    GIF/video overlay dict (plus its "seed" for the wobble)."""
    if not layout.get("overlays_enabled"):
        return [], []
    out = layout["output"]
    size = (even(out["width"]), even(out["height"]))
    layers: list = []
    inputs: list[str] = []
    run: list[dict] = []  # consecutive stills, drawn into one PNG

    def flush() -> None:
        img = render_overlays(layout, values, size, run) if run else None
        if img is not None and img.getbbox() is not None:
            png = tmp_prefix.with_name(f"{tmp_prefix.name}_{len(layers)}.png")
            img.save(png, compress_level=1)
            layers.append(None)
            inputs.extend(["-i", str(png)])
        run.clear()

    def add_wobble(ov: dict, seed: int) -> None:
        # one frame per half-step only for an "apart" text (its letters change between
        # the box's poses); anything else changes once per pose
        apart = ov["type"] == "text" and ov.get("anim_target") == "apart"
        steps = range(2 * ANIM_POSES) if apart else range(0, 2 * ANIM_POSES, 2)
        drawn: dict = {}  # repeated poses share their image
        poses = []
        for p in steps:
            posed = wobble_pose(ov, seed, p)
            key = (posed.get("_tilt_box"), posed.get("_tilt_text"))
            if key not in drawn:
                drawn[key] = render_overlays(layout, values, size, [posed])
            poses.append(drawn[key])
        boxes = [b for b in (img.getbbox() for img in drawn.values() if img is not None) if b]
        if not boxes:
            return  # e.g. a {title} text on a video without a title
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        pattern = tmp_prefix.with_name(f"{tmp_prefix.name}_{len(layers)}_%02d.png")
        for p, img in enumerate(poses):  # temp files: fast compression beats small
            img.crop((x0, y0, x1, y1)).save(str(pattern) % p, compress_level=1)
        layers.append({"anim": True, "x": x0, "y": y0})
        inputs.extend(["-loop", "1", "-framerate", str(len(poses) // ANIM_POSES * ANIM_FPS),
                       "-i", str(pattern)])

    for seed, ov in enumerate(layout["overlays"]):
        if not ov["visible"]:
            continue
        if is_media(ov):
            if Path(ov["path"]).is_file():
                flush()
                layers.append({**ov, "seed": seed})
                inputs.extend(["-stream_loop", "-1", "-i", ov["path"]])
        elif ov.get("animate") and not still:
            flush()
            add_wobble(ov, seed)
        else:
            run.append(wobble_pose(ov, seed, 0))
    flush()
    return layers, inputs


def filter_for_video(layout: dict | None, values: dict, tmp_prefix: Path,
                     still: bool = False) -> tuple[str, list[str]]:
    """Filter graph for one video (built-in CONFIG when layout is None) + the extra
    ffmpeg input args its overlays need (PNG files are written next to tmp_prefix).
    still=True for single-frame previews (see overlay_layers)."""
    if layout is None:
        return build_filter_complex(), []
    layers, inputs = overlay_layers(layout, values, tmp_prefix, still)
    return build_filter_complex_from_layout(layout, layers), inputs


def load_titles() -> dict[str, dict]:
    """Video file name -> {"title": ..., "credit": ...}."""
    try:
        data = json.loads(Path(TITLES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_titles(titles: dict[str, dict]) -> None:
    Path(TITLES_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(TITLES_FILE).write_text(json.dumps(titles, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")


def load_layout(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"layout: {path} is not valid JSON ({exc})") from exc
    return normalize_layout(raw)


def save_layout(path: Path, layout: dict) -> None:
    path.write_text(json.dumps(normalize_layout(layout), indent=2) + "\n", encoding="utf-8")


def _layout_parts(layout: dict, src: str = "0:v", tag: str = "") -> tuple[list[str], str]:
    """Background + boxes of a layout, fed by [src]. Every label gets the `tag`
    prefix (so several copies can share one graph); returns (parts, last label)."""
    out = layout["output"]
    width, height = even(out["width"]), even(out["height"])
    boxes = [b for b in layout["boxes"] if b.get("visible", True)]
    bg = layout["background"]

    # one stream per layer: the background plus every visible box
    sources = [f"{tag}bg_in"] + [f"{tag}b{i}_in" for i in range(len(boxes))]
    parts: list[str] = []
    if len(sources) == 1:
        parts.append(f"[{src}]null[{tag}bg_in]")
    else:
        parts.append(f"[{src}]split={len(sources)}" + "".join(f"[{s}]" for s in sources))

    # background: zoomed-in "cover" fit of the whole canvas, then blurred
    # (or plain black when disabled - still driven by the source so timing matches)
    if bg["enabled"]:
        parts.append(f"[{tag}bg_in]{blurred_bg(width, height, bg['zoom'], bg['blur'])}[{tag}bg]")
    else:
        parts.append(f"[{tag}bg_in]scale={width}:{height},lutrgb=r=0:g=0:b=0[{tag}bg]")

    # every box: crop from the source (fractions of iw/ih), scale to its
    # output rectangle (or keep the crop's aspect when keep_aspect is set)
    for i, box in enumerate(boxes):
        src_box, dst = box["src"], box["out"]
        crop = (
            f"crop=w=min(floor(iw*{num(src_box['w'])}/2)*2\\,floor(iw/2)*2)"
            f":h=min(floor(ih*{num(src_box['h'])}/2)*2\\,floor(ih/2)*2)"
            f":x=min(round(iw*{num(src_box['x'])}/2)*2\\,iw-ow)"
            f":y=min(round(ih*{num(src_box['y'])}/2)*2\\,ih-oh)"
        )
        out_w = even(width * dst["w"])
        scale = f"scale={out_w}:-2" if box.get("keep_aspect") else f"scale={out_w}:{even(height * dst['h'])}"
        parts.append(f"[{tag}b{i}_in]{crop},{scale},setsar=1[{tag}b{i}]")

    # stack the boxes on the background, in list order
    prev = f"{tag}bg"
    for i, box in enumerate(boxes):
        dst = box["out"]
        label = f"{tag}tmp{i}" if i < len(boxes) - 1 else f"{tag}stacked"
        x, y = even(width * dst["x"]), even(height * dst["y"])
        parts.append(f"[{prev}][{tag}b{i}]overlay=x={x}:y={y}[{label}]")
        prev = label
    return parts, prev


def _overlay_parts(layout: dict, prev: str, layers: list, first_input: int = 1) -> list[str]:
    """Lay the overlay layers (extra inputs first_input, first_input + 1, ...) on
    [prev]: texts/images are a full-canvas PNG, looped GIFs/videos are fitted in
    their rect. Ends in [vout]."""
    out = layout["output"]
    width, height = even(out["width"]), even(out["height"])
    parts: list[str] = []
    for k, ov in enumerate(layers, start=first_input):
        if ov is None:
            parts.append(f"[{prev}][{k}:v]overlay=x=0:y=0[l{k}]")
        elif ov.get("anim"):
            # the poses loop forever: shortest=1 stops them with the gameplay
            parts.append(f"[{prev}][{k}:v]overlay=x={ov['x']}:y={ov['y']}:shortest=1[l{k}]")
        else:
            r = ov["out"]
            bw, bh = even(width * r["w"]), even(height * r["h"])
            x, y = round(width * r["x"]), round(height * r["y"])
            # rotated: the frame grows to fit the turned picture (transparent corners);
            # the overlay below stays centered on the rectangle either way
            if ov.get("animate"):
                # step through the same wobble poses as the editor preview
                angles = [math.radians(ov.get("rotation", 0) + t) for t in wobble_tilts(ov["seed"])]
                expr = num(angles[-1])
                for i in range(len(angles) - 2, -1, -1):
                    expr = f"if(eq(ld(0)\\,{i})\\,{num(angles[i])}\\,{expr})"
                widest = num(max(angles, key=abs))
                turn = (f"format=rgba,rotate=a=st(0\\,mod(floor(t*{ANIM_FPS})\\,{len(angles)}))*0+{expr}"
                        f":ow=rotw({widest}):oh=roth({widest}):c=black@0,")
            else:
                rad = num(math.radians(ov.get("rotation", 0)))
                turn = (f"format=rgba,rotate={rad}:ow=rotw({rad}):oh=roth({rad}):c=black@0,"
                        if ov.get("rotation") else "")
            parts.append(f"[{k}:v]scale=w={bw}:h={bh}:force_original_aspect_ratio=decrease,"
                         f"{turn}setsar=1[m{k}]")
            # shortest=1: the endlessly looped input stops when the gameplay does
            parts.append(f"[{prev}][m{k}]overlay=x={x}+({bw}-overlay_w)/2"
                         f":y={y}+({bh}-overlay_h)/2:shortest=1[l{k}]")
        prev = f"l{k}"
    parts.append(f"[{prev}]setsar=1[vout]")
    return parts


def build_filter_complex_from_layout(layout: dict, layers: list | None = None) -> str:
    """Build the ffmpeg filter graph from a layout dict (see default_layout).
    `layers` describes extra inputs 1, 2, ... laid on top in order (see overlay_layers)."""
    parts, prev = _layout_parts(layout)
    return ";".join(parts + _overlay_parts(layout, prev, layers or []))
# ======================================================================


NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# x264 preset name -> NVENC preset (p1 = fastest ... p7 = best quality)
_NVENC_PRESETS = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3",
    "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7",
}


# GPU encoders spend more bits than x264 for the same number: at CQ = CRF an
# NVENC file came out ~2x bigger with no visible gain, CQ = CRF + 5 matched
# x264's size. AMF's QP is assumed to behave alike (not measured).
_GPU_Q_OFFSET = 5


def encoder_args(encoder: str, crf: int = CRF, preset: str = PRESET) -> list[str]:
    """Video codec options for an ENCODERS key, mapping CRF/preset to its closest equivalent."""
    if encoder == "nvidia":
        return ["-c:v", "h264_nvenc", "-preset", _NVENC_PRESETS.get(preset, "p5"),
                "-rc", "vbr", "-cq", str(crf + _GPU_Q_OFFSET), "-b:v", "0", "-pix_fmt", "yuv420p"]
    if encoder == "amd":
        quality = ("speed" if preset in ("ultrafast", "superfast", "veryfast", "faster")
                   else "quality" if preset in ("slow", "slower", "veryslow") else "balanced")
        qp = str(crf + _GPU_Q_OFFSET)
        return ["-c:v", "h264_amf", "-quality", quality, "-rc", "cqp",
                "-qp_i", qp, "-qp_p", qp, "-qp_b", qp, "-pix_fmt", "yuv420p"]
    if encoder == "intel":
        qsv_preset = preset if preset not in ("ultrafast", "superfast") else "veryfast"
        return ["-c:v", "h264_qsv", "-preset", qsv_preset, "-global_quality", str(crf),
                "-pix_fmt", "nv12"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]


def encoder_works(ffmpeg: str, encoder: str) -> bool:
    """Encode a few black frames: being compiled into ffmpeg doesn't mean the GPU is there."""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=black:size=640x360:rate=30",
        "-frames:v", "5", *encoder_args(encoder), "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=20, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_encoders(ffmpeg: str) -> list[str]:
    """Working GPU encoders (in GPU_ORDER), always followed by "cpu"."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(len(GPU_ORDER)) as pool:
        works = list(pool.map(lambda e: encoder_works(ffmpeg, e), GPU_ORDER))
    return [e for e, ok in zip(GPU_ORDER, works) if ok] + ["cpu"]


def decode_args(encoder: str) -> list[str]:
    """Decode the gameplay on the GPU too when encoding on it: big 1440p/4K
    sources cost as much to decode as the rest of the filter graph. d3d11va
    works on NVIDIA, AMD and Intel; ffmpeg falls back to the CPU on its own
    when the codec isn't supported, and the CPU retry doesn't use it at all."""
    if encoder == "cpu":
        return []
    return ["-hwaccel", "d3d11va" if sys.platform == "win32" else "auto"]


def trim_args(start: float = 0.0, end: float | None = None) -> list[str]:
    """Input options that keep only start..end (seconds) of the next input."""
    args = ["-ss", f"{start:g}"] if start > 0 else []
    if end is not None:
        args += ["-t", f"{max(0.0, end - start):g}"]
    return args


def build_render_cmd(ffmpeg: str, src: Path, dst: Path, fc: str, encoder: str = "cpu",
                     extra_inputs: list[str] | None = None,
                     trim: tuple[float, float | None] = (0.0, None)) -> list[str]:
    return [
        ffmpeg, "-y", "-hide_banner",
        *decode_args(encoder),
        *trim_args(*trim),
        "-i", str(src),
        *(extra_inputs or []),
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "0:a?",
        *encoder_args(encoder),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]


_LABEL_RE = re.compile(r"\[(?:(\d+):v|([A-Za-z_]\w*))\]")


def merge_filter_graphs(graphs: list[tuple[str, list[str]]]) -> tuple[str, list[str]]:
    """Join several (filter graph, extra inputs) pairs into one graph that decodes
    the gameplay once and splits it: output i ends in [o{i}vout]. Each graph's
    labels get an o{i} prefix and its extra inputs are renumbered after the others'."""
    parts = [f"[0:v]split={len(graphs)}" + "".join(f"[o{i}src]" for i in range(len(graphs)))]
    inputs: list[str] = []
    for i, (fc, extra) in enumerate(graphs):
        offset = inputs.count("-i")  # inputs taken by the graphs before this one

        def relabel(m: re.Match, i=i, offset=offset) -> str:
            if m.group(1) is None:
                return f"[o{i}{m.group(2)}]"
            k = int(m.group(1))
            return f"[o{i}src]" if k == 0 else f"[{k + offset}:v]"

        parts.append(_LABEL_RE.sub(relabel, fc))
        inputs += extra
    return ";".join(parts), inputs


def build_multi_render_cmd(ffmpeg: str, src: Path, outputs: list[tuple[Path, str, list[str]]],
                           encoder: str = "cpu",
                           trim: tuple[float, float | None] = (0.0, None)) -> list[str]:
    """Render one video into several layouts at once: outputs = [(dst, fc, extra_inputs)].
    The source is decoded once and every output is encoded in parallel."""
    if len(outputs) == 1:
        dst, fc, extra = outputs[0]
        return build_render_cmd(ffmpeg, src, dst, fc, encoder, extra, trim)
    fc, extra = merge_filter_graphs([(fc, extra) for _, fc, extra in outputs])
    cmd = [ffmpeg, "-y", "-hide_banner", *decode_args(encoder), *trim_args(*trim),
           "-i", str(src), *extra, "-filter_complex", fc]
    for i, (dst, _, _) in enumerate(outputs):
        cmd += ["-map", f"[o{i}vout]", "-map", "0:a?", *encoder_args(encoder),
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
    return cmd


def probe_media(ffmpeg: str, path: Path) -> tuple[float, bool]:
    """(duration in seconds, has an audio stream), read from ffmpeg's own banner
    so ffprobe isn't needed. Duration is 0 when it can't be read."""
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
                            text=True, errors="replace", stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    return duration, re.search(r"Stream #0:\d+.*: Audio:", result.stderr) is not None


def build_join_cmd(ffmpeg: str, clips: list[tuple[Path, float, float, bool]], layout: dict,
                   layers: list, extra_inputs: list[str], dst: Path,
                   encoder: str = "cpu") -> list[str]:
    """Several clips, one after the other, in one short. clips = [(path, start,
    end, has_audio)]: each clip goes through the layout on its own (so sources
    of different sizes are fine), they are concatenated, and only then the
    overlays go on top, so texts stay put and GIFs keep looping across the cuts.
    Clips without audio get silence so the concat lines up."""
    n = len(clips)
    cmd = [ffmpeg, "-y", "-hide_banner"]
    parts: list[str] = []
    for i, (path, start, end, has_audio) in enumerate(clips):
        cmd += [*decode_args(encoder), *trim_args(start, end), "-i", str(path)]
        clip_parts, last = _layout_parts(layout, f"{i}:v", f"c{i}_")
        parts += clip_parts
        parts.append(f"[{last}]setsar=1,format=yuv420p[c{i}v]")
        if has_audio:
            parts.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo[c{i}a]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={end - start:g}[c{i}a]")
    parts.append("".join(f"[c{i}v][c{i}a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[joined][aout]")
    parts += _overlay_parts(layout, "joined", layers, first_input=n)
    return cmd + [
        *extra_inputs,
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        *encoder_args(encoder),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]


def build_preview_cmd(ffmpeg: str, src: Path, dst: Path, fc: str, at: float,
                      extra_inputs: list[str] | None = None) -> list[str]:
    return [
        ffmpeg, "-y", "-hide_banner",
        "-ss", f"{at:g}",
        "-i", str(src),
        *(extra_inputs or []),
        "-filter_complex", fc,
        "-map", "[vout]",
        "-frames:v", "1",
        "-update", "1",
        str(dst),
    ]


def ensure_dirs() -> None:
    """Create the working folder layout if it is missing (runs on every init)."""
    for d in (INPUT_DIR, CONVERTED_DIR, OUTPUT_DIR, PUBLISHED_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)


def move_to_converted(src: Path) -> Path:
    """Move a processed original into CONVERTED_DIR (never overwrites)."""
    converted = Path(CONVERTED_DIR)
    if src.resolve().parent == converted.resolve():
        return src  # already in the converted folder
    dst = converted / src.name
    stem, suffix = dst.stem, dst.suffix
    n = 2
    while dst.exists():
        dst = converted / f"{stem} ({n}){suffix}"
        n += 1
    shutil.move(str(src), str(dst))
    return dst


def collect_inputs(raw_paths: list[str]) -> list[Path]:
    """Expand CLI paths (files and/or folders) into a list of video files."""
    files: list[Path] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(
                sorted(f for f in path.iterdir() if f.suffix.lower() in VIDEO_EXTS)
            )
        elif path.is_file():
            files.append(path)
        else:
            sys.exit(f"error: input not found: {path}")
    # de-duplicate, keep order
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            unique.append(f)
    return unique


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crop Steam gameplay videos into 9:16 YouTube Shorts "
        "(square gameplay in the middle, blurred video behind, items panel at the bottom).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "inputs", nargs="*", metavar="VIDEO_OR_DIR",
        help=f"video files and/or folders (default: ./{INPUT_DIR})",
    )
    parser.add_argument(
        "-o", "--output", default=OUTPUT_DIR,
        help="output folder",
    )
    parser.add_argument(
        "--preview", type=float, nargs="?", const=5.0, default=None, metavar="SECONDS",
        help="render a single test frame at SECONDS (default 5) instead of the full video",
    )
    parser.add_argument(
        "--config", nargs="?", const=DEFAULT_LAYOUT_NAME, default=None, metavar="FILE",
        help=f"open the visual layout editor UI on FILE (default: ./{DEFAULT_LAYOUT_NAME})",
    )
    parser.add_argument(
        "--layout", default=None, metavar="FILE",
        help=f"layout file to use when rendering "
             f"(default: ./{DEFAULT_LAYOUT_NAME} if it exists, else the built-in CONFIG)",
    )
    parser.add_argument(
        "--encoder", choices=["auto", *ENCODERS], default="auto",
        help="video encoder: auto = first working GPU (nvidia, amd, intel), else cpu; "
             "a failed GPU render is retried on the cpu",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the ffmpeg command without running it",
    )
    args = parser.parse_args()

    ensure_dirs()  # make sure input/, input/converted/, output/, output/published/ exist

    ffmpeg = find_ffmpeg()
    if not ffmpeg and not args.dry_run:
        sys.exit(
            "error: ffmpeg not found.\n"
            "  - install it (e.g. 'winget install ffmpeg' on Windows) and add it to PATH,\n"
            "  - or run: python -m pip install imageio-ffmpeg"
        )

    if args.config is not None:
        # ---- layout editor mode: no rendering, just the UI ------------
        import layout_editor

        candidates: list[Path] = []
        roots = args.inputs or ([INPUT_DIR] if Path(INPUT_DIR).exists() else [])
        for raw in roots:
            if Path(raw).exists():
                candidates.extend(collect_inputs([raw]))
        sys.exit(layout_editor.run_editor(Path(args.config), candidates))

    raw_inputs = args.inputs or [INPUT_DIR]
    if not args.inputs and not Path(INPUT_DIR).exists():
        parser.print_usage()
        sys.exit(f"\nerror: no inputs given and ./{INPUT_DIR} does not exist")

    files = collect_inputs(raw_inputs)
    if not files:
        sys.exit("error: no video files found in the given inputs")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "published").mkdir(exist_ok=True)  # drop zone for already-uploaded shorts

    # pick the layout: explicit --layout, else ./layout.json if present,
    # else fall back to the built-in CONFIG block
    layout_file = Path(args.layout) if args.layout else Path(DEFAULT_LAYOUT_NAME)
    if args.layout and not layout_file.exists():
        sys.exit(f"error: layout file not found: {layout_file}")
    layout: dict | None = None
    if layout_file.exists():
        try:
            layout = load_layout(layout_file)
        except ValueError as exc:
            sys.exit(f"error: {exc}")
        fc = build_filter_complex_from_layout(layout)
        boxes = len([b for b in layout["boxes"] if b.get("visible", True)])
        source = f"{layout_file} ({layout['output']['width']}x{layout['output']['height']}, {boxes} box(es))"
        if layout["overlays_enabled"]:
            source += f", {len(layout['overlays'])} text/image overlay(s)"
    else:
        fc = build_filter_complex()
        source = "built-in CONFIG"
    titles = load_titles()
    tmp_dir = Path(tempfile.mkdtemp(prefix="shorts_overlay_"))

    # pick the encoder: only GPUs that pass a quick test encode, else the cpu
    available = detect_encoders(ffmpeg) if ffmpeg and args.preview is None else ["cpu"]
    encoder = available[0] if args.encoder == "auto" else args.encoder
    if encoder not in available:
        print(f"warning: {ENCODERS[encoder]} is not available here, using {ENCODERS['cpu']}")
        encoder = "cpu"

    print(f"ffmpeg:   {ffmpeg or '(not found)'}")
    print(f"encoder:  {ENCODERS[encoder]}")
    print(f"layout:   {source}")
    print(f"inputs:   {len(files)} video(s)")
    print(f"filter:   {fc}\n")

    failures = 0
    for n, src in enumerate(files):
        # {title}/{credit} differ per video, so the overlays are drawn per video
        try:
            fc, extra = filter_for_video(layout, titles.get(src.name, {}), tmp_dir / str(n))
        except RuntimeError as exc:
            sys.exit(f"error: {exc}")
        if args.preview is not None:
            dst = out_dir / f"{src.stem}_preview_{args.preview:g}s.png"
            cmd = build_preview_cmd(ffmpeg, src, dst, fc, args.preview, extra)
        else:
            dst = out_dir / f"{src.stem}_short.mp4"
            cmd = build_render_cmd(ffmpeg, src, dst, fc, encoder, extra)

        if args.dry_run:
            print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
            continue

        print(f"[render] {src.name} -> {dst}")
        result = subprocess.run(cmd)
        if result.returncode != 0 and args.preview is None and encoder != "cpu":
            print(f"[fallback] {ENCODERS[encoder]} failed on {src.name}, retrying on {ENCODERS['cpu']}")
            result = subprocess.run(build_render_cmd(ffmpeg, src, dst, fc, "cpu", extra))
        if result.returncode != 0:
            failures += 1
            print(f"[FAILED] {src.name} (ffmpeg exit code {result.returncode})")
        elif args.preview is None:
            moved = move_to_converted(src)
            if moved != src:
                print(f"[moved]  {src.name} -> {moved}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    if args.dry_run:
        return
    if failures:
        sys.exit(f"\nDone with {failures} failure(s) out of {len(files)}.")
    print(f"\nDone: {len(files)} video(s) written to {out_dir.resolve()}")


if __name__ == "__main__":
    main()
