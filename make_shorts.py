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
import json
import shutil
import subprocess
import sys
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


def build_filter_complex() -> str:
    """Build the ffmpeg filter graph from the CONFIG numbers."""
    # background: zoomed-in "cover" fit of the whole canvas, then blurred
    bg_w, bg_h = even(OUTPUT_W * BG_ZOOM), even(OUTPUT_H * BG_ZOOM)

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
        f"[bg_in]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_W}:{OUTPUT_H},gblur=sigma={num(BG_BLUR)}[bg];"
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

    return {"version": 1, "output": output, "background": background, "boxes": boxes}


def load_layout(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"layout: {path} is not valid JSON ({exc})") from exc
    return normalize_layout(raw)


def save_layout(path: Path, layout: dict) -> None:
    path.write_text(json.dumps(normalize_layout(layout), indent=2) + "\n", encoding="utf-8")


def build_filter_complex_from_layout(layout: dict) -> str:
    """Build the ffmpeg filter graph from a layout dict (see default_layout)."""
    out = layout["output"]
    width, height = even(out["width"]), even(out["height"])
    boxes = [b for b in layout["boxes"] if b.get("visible", True)]
    bg = layout["background"]

    # one stream per layer: the background plus every visible box
    sources = ["bg_in"] + [f"b{i}_in" for i in range(len(boxes))]
    parts: list[str] = []
    if len(sources) == 1:
        parts.append("[0:v]null[bg_in]")
    else:
        parts.append(f"[0:v]split={len(sources)}" + "".join(f"[{s}]" for s in sources))

    # background: zoomed-in "cover" fit of the whole canvas, then blurred
    # (or plain black when disabled - still driven by [0:v] so timing matches)
    if bg["enabled"]:
        bg_w, bg_h = even(width * bg["zoom"]), even(height * bg["zoom"])
        parts.append(
            f"[bg_in]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma={num(bg['blur'])}[bg]"
        )
    else:
        parts.append(f"[bg_in]scale={width}:{height},lutrgb=r=0:g=0:b=0[bg]")

    # every box: crop from the source (fractions of iw/ih), scale to its
    # output rectangle (or keep the crop's aspect when keep_aspect is set)
    for i, box in enumerate(boxes):
        src, dst = box["src"], box["out"]
        crop = (
            f"crop=w=min(floor(iw*{num(src['w'])}/2)*2\\,floor(iw/2)*2)"
            f":h=min(floor(ih*{num(src['h'])}/2)*2\\,floor(ih/2)*2)"
            f":x=min(round(iw*{num(src['x'])}/2)*2\\,iw-ow)"
            f":y=min(round(ih*{num(src['y'])}/2)*2\\,ih-oh)"
        )
        out_w = even(width * dst["w"])
        scale = f"scale={out_w}:-2" if box.get("keep_aspect") else f"scale={out_w}:{even(height * dst['h'])}"
        parts.append(f"[b{i}_in]{crop},{scale},setsar=1[b{i}]")

    # stack the boxes on the background, in list order
    prev = "bg"
    for i, box in enumerate(boxes):
        dst = box["out"]
        label = "tmp" if i < len(boxes) - 1 else "stacked"
        x, y = even(width * dst["x"]), even(height * dst["y"])
        parts.append(f"[{prev}][b{i}]overlay=x={x}:y={y}[{label}]")
        prev = label
    parts.append(f"[{prev}]setsar=1[vout]")
    return ";".join(parts)
# ======================================================================


def build_render_cmd(ffmpeg: str, src: Path, dst: Path, fc: str) -> list[str]:
    return [
        ffmpeg, "-y", "-hide_banner",
        "-i", str(src),
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]


def build_preview_cmd(ffmpeg: str, src: Path, dst: Path, fc: str, at: float) -> list[str]:
    return [
        ffmpeg, "-y", "-hide_banner",
        "-ss", f"{at:g}",
        "-i", str(src),
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
    if layout_file.exists():
        try:
            layout = load_layout(layout_file)
        except ValueError as exc:
            sys.exit(f"error: {exc}")
        fc = build_filter_complex_from_layout(layout)
        boxes = len([b for b in layout["boxes"] if b.get("visible", True)])
        source = f"{layout_file} ({layout['output']['width']}x{layout['output']['height']}, {boxes} box(es))"
    else:
        fc = build_filter_complex()
        source = "built-in CONFIG"

    print(f"ffmpeg:   {ffmpeg or '(not found)'}")
    print(f"layout:   {source}")
    print(f"inputs:   {len(files)} video(s)")
    print(f"filter:   {fc}\n")

    failures = 0
    for src in files:
        if args.preview is not None:
            dst = out_dir / f"{src.stem}_preview_{args.preview:g}s.png"
            cmd = build_preview_cmd(ffmpeg, src, dst, fc, args.preview)
        else:
            dst = out_dir / f"{src.stem}_short.mp4"
            cmd = build_render_cmd(ffmpeg, src, dst, fc)

        if args.dry_run:
            print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
            continue

        print(f"[render] {src.name} -> {dst}")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            failures += 1
            print(f"[FAILED] {src.name} (ffmpeg exit code {result.returncode})")
        elif args.preview is None:
            moved = move_to_converted(src)
            if moved != src:
                print(f"[moved]  {src.name} -> {moved}")

    if args.dry_run:
        return
    if failures:
        sys.exit(f"\nDone with {failures} failure(s) out of {len(files)}.")
    print(f"\nDone: {len(files)} video(s) written to {out_dir.resolve()}")


if __name__ == "__main__":
    main()
