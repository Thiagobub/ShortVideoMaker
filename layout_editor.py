#!/usr/bin/env python3
"""
layout_editor.py - visual editor for the crop/place boxes used by make_shorts.py.

Launch it with:

    python make_shorts.py --config             # edits ./layout.json
    python make_shorts.py --config my.json     # edits a specific layout file

Left panel  : a paused frame of the selected video, with a box around every
              region that gets cropped FROM the source.
Right panel : a live preview of the composed 9:16 output, with a box around
              every region placed ON the output canvas.

Drag a box (or its handles) on either side to move/resize it - the preview
recomposites instantly. "Render exact frame" runs the real ffmpeg filter
graph (~1 s) so you can verify the approximated live preview pixel by pixel.

Save writes the layout JSON; make_shorts.py picks it up automatically when
rendering (or pass it explicitly with --layout FILE).
"""

from __future__ import annotations

import copy
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageFilter, ImageTk

import make_shorts as ms

# ---------------------------------------------------------------- tuning
PREVIEW_H = 620        # height (px) of the right-hand preview canvas
HANDLE = 7             # size of the resize handles
MIN_SIZE = 0.005       # smallest box edge, as a fraction of the frame
UNDO_LIMIT = 100       # how many layout states the undo history keeps
BOX_COLORS = [
    "#ff5f5f", "#57e07a", "#57a7ff", "#ffd23f",
    "#d878ff", "#3fe0dd", "#ff9b3f", "#a6e84f",
]

FPS_RE = re.compile(r"([\d.]+)\s*fps")
TBR_RE = re.compile(r"([\d.]+)\s*tbr")
DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


# ------------------------------------------------------------- helpers
def probe_video(ffmpeg: str, path: Path) -> tuple[float, float]:
    """Return (duration_seconds, fps) parsed from ffmpeg's banner output."""
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    text = res.stderr
    duration = 0.0
    m = DUR_RE.search(text)
    if m:
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    fps = 30.0
    m = FPS_RE.search(text) or TBR_RE.search(text)
    if m:
        try:
            fps = float(m[1]) or 30.0
        except ValueError:
            pass
    return duration, fps


def extract_frame(ffmpeg: str, path: Path, at: float) -> Image.Image | None:
    """Grab a single RGB frame at `at` seconds via a PNG pipe."""
    res = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-ss", f"{at:g}", "-i", str(path),
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True,
    )
    if res.returncode != 0 or not res.stdout:
        return None
    try:
        return Image.open(io.BytesIO(res.stdout)).convert("RGB")
    except Exception:
        return None


def parse_time(text: str) -> float | None:
    """Accept '95', '95.5', '1:35' or '1:35.5'."""
    text = text.strip()
    try:
        if ":" in text:
            parts = text.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return None
        return float(text)
    except ValueError:
        return None


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60.0)
    return f"{int(m):02d}:{s:04.1f}"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class ToolTip:
    """Hover tooltip for a widget - explains what a button does."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(tip, text=self.text, background="#ffffe0", foreground="#222",
                 relief="solid", borderwidth=1, font=("", 9), padx=6, pady=3).pack()
        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# ================================================================ UI
class LayoutEditor:
    def __init__(self, root: tk.Tk, layout_path: Path, layout: dict, videos: list[Path],
                 fresh_default: bool = False):
        self.root = root
        self.layout_path = layout_path
        self.layout = ms.normalize_layout(layout)
        self.videos = videos
        self.fresh_default = fresh_default  # True until the user edits a brand-new layout
        self.dirty = False

        self._snap: dict = copy.deepcopy(self.layout)  # last committed state
        self._undo: list[dict] = []        # older committed states
        self._redo: list[dict] = []        # states undone (empty = nothing to redo)
        self._commit_job = None            # pending debounce timer for the history

        self.video: Path | None = None
        self.src_w = self.src_h = 0
        self.fps = 30.0
        self.duration = 0.0
        self.time = 0.0
        self.frame: Image.Image | None = None

        self.selected = -1
        self.drag: dict | None = None
        self.left_map = (0, 0, 0, 0)     # (x0, y0, w, h) of the frame on the canvas
        self.right_map = (0, 0, 0, 0)
        self._left_cache: tuple | None = None   # (frame, w, h, photo)
        self._bg_cache: tuple | None = None     # (frame, w, h, zoom, blur, image)
        self._resize_job = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._update_title()
        self._history_reset()

        if videos:
            self.load_video(videos[0])
        else:
            self.refresh()

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.title("Layout editor")
        self.root.geometry("1520x1000")
        self.root.minsize(1100, 720)

        # ---- top bar: video + files -------------------------------
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Video:").pack(side="left")
        self.var_video = tk.StringVar()
        self.combo_video = ttk.Combobox(
            top, textvariable=self.var_video, width=42, state="readonly",
            values=[p.name for p in self.videos],
        )
        self.combo_video.pack(side="left", padx=4)
        self.combo_video.bind("<<ComboboxSelected>>", self._on_video_picked)
        ttk.Button(top, text="Open…", command=self.open_video).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="Save layout", command=self.save).pack(side="left", padx=2)
        ttk.Button(top, text="Save as…", command=self.save_as).pack(side="left", padx=2)
        ttk.Button(top, text="Load…", command=self.load_from).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn_undo = ttk.Button(top, text="⟲ Undo", command=self.undo)
        self.btn_undo.pack(side="left", padx=2)
        ToolTip(self.btn_undo, "Undo the last change (Ctrl+Z)")
        self.btn_redo = ttk.Button(top, text="⟳ Redo", command=self.redo)
        self.btn_redo.pack(side="left", padx=2)
        ToolTip(self.btn_redo, "Redo the last undone change (Ctrl+Y or Ctrl+Shift+Z)")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="Render exact frame (ffmpeg)",
                   command=self.render_exact).pack(side="left", padx=2)
        ttk.Button(top, text="Reset to defaults",
                   command=self.reset_defaults).pack(side="left", padx=2)

        # ---- time bar ---------------------------------------------
        timebar = ttk.Frame(self.root, padding=(8, 0))
        timebar.pack(side="top", fill="x")
        for text, cmd in (("‹ 1 s", lambda: self.step_time(-1.0)),
                          ("‹ 1 frame", lambda: self.step_time(-1.0 / self.fps)),
                          ("1 frame ›", lambda: self.step_time(1.0 / self.fps)),
                          ("1 s ›", lambda: self.step_time(1.0))):
            ttk.Button(timebar, text=text, width=10, command=cmd).pack(side="left", padx=2)
        ttk.Label(timebar, text="  t =").pack(side="left")
        self.var_t = tk.StringVar(value="00:00.0")
        entry_t = ttk.Entry(timebar, textvariable=self.var_t, width=9, justify="right")
        entry_t.pack(side="left", padx=4)
        entry_t.bind("<Return>", self._on_time_entry)
        entry_t.bind("<FocusOut>", self._on_time_entry)
        self.lbl_duration = ttk.Label(timebar, text="/ 00:00.0   (drag the slider, release to load the frame)")
        self.lbl_duration.pack(side="left")
        self.scale_time = ttk.Scale(timebar, from_=0.0, to=1.0, command=self._on_scale_move)
        self.scale_time.pack(side="left", fill="x", expand=True, padx=8)
        self.scale_time.bind("<ButtonRelease-1>", self._on_scale_release)

        # ---- main panes -------------------------------------------
        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.pack(side="top", fill="both", expand=True, padx=8, pady=(6, 0))

        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=3)
        panes.add(right, weight=2)

        self.lbl_left_title = ttk.Label(left, text="SOURCE — crop boxes (what is taken from the video)")
        self.lbl_left_title.pack(side="top", anchor="w")
        self.left_canvas = tk.Canvas(left, background="#141418", highlightthickness=0)
        self.left_canvas.pack(side="top", fill="both", expand=True)

        self.lbl_right_title = ttk.Label(right, text="PREVIEW (live, approximated) — placement boxes")
        self.lbl_right_title.pack(side="top", anchor="w")
        self.right_canvas = tk.Canvas(right, background="#141418", highlightthickness=0)
        self.right_canvas.pack(side="top", fill="both", expand=True)

        for canvas, side in ((self.left_canvas, "src"), (self.right_canvas, "out")):
            canvas.bind("<Button-1>", lambda e, s=side: self._on_press(s, e))
            canvas.bind("<B1-Motion>", lambda e, s=side: self._on_motion(s, e))
            canvas.bind("<ButtonRelease-1>", lambda e, s=side: self._on_release(s, e))
            canvas.bind("<Configure>", self._on_resize)

        # ---- bottom: boxes / fields / settings --------------------
        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(side="bottom", fill="x")
        bottom.columnconfigure(0, weight=1)
        bottom.columnconfigure(1, weight=2)
        bottom.columnconfigure(2, weight=2)

        # boxes list
        grp_boxes = ttk.LabelFrame(bottom, text="Boxes (top of list = bottom of the stack)")
        grp_boxes.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.listbox = tk.Listbox(grp_boxes, height=6, exportselection=False)
        self.listbox.pack(side="top", fill="x", padx=6, pady=6)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)
        row = ttk.Frame(grp_boxes)
        row.pack(side="top", fill="x", padx=6, pady=(0, 6))
        for text, cmd in (("Add", self.add_box), ("Remove", self.remove_box),
                          ("Rename", self.rename_box), ("↑", lambda: self.move_box(-1)),
                          ("↓", lambda: self.move_box(1))):
            ttk.Button(row, text=text, width=7, command=cmd).pack(side="left", padx=2)

        # selected box fields
        grp_box = ttk.LabelFrame(bottom, text="Selected box")
        grp_box.grid(row=0, column=1, sticky="nsew", padx=6)
        self.var_name = tk.StringVar(value="—")
        ttk.Label(grp_box, textvariable=self.var_name, font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 0))
        self.var_visible = tk.BooleanVar(value=True)
        self.var_keep_aspect = tk.BooleanVar(value=True)
        ttk.Checkbutton(grp_box, text="visible", variable=self.var_visible,
                        command=self._on_toggle).grid(row=1, column=0, sticky="w", padx=6)
        ttk.Checkbutton(grp_box, text="keep crop aspect for output height",
                        variable=self.var_keep_aspect,
                        command=self._on_toggle).grid(row=1, column=1, columnspan=3, sticky="w")

        self.var_src = {k: tk.StringVar() for k in "xywh"}
        self.var_out = {k: tk.StringVar() for k in "xywh"}
        fields = (("SOURCE crop", self.var_src, 2), ("OUTPUT place", self.var_out, 5))
        for label, vars_, row in fields:
            ttk.Label(grp_box, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=(6, 0))
            for col, key in enumerate("xywh"):
                f = ttk.Frame(grp_box)
                f.grid(row=row + 1, column=col, sticky="ew", padx=4)
                ttk.Label(f, text=key, width=2).pack(side="left")
                e = ttk.Entry(f, textvariable=vars_[key], width=8, justify="right")
                e.pack(side="left")
                e.bind("<Return>", lambda _e: self.apply_fields())
                e.bind("<FocusOut>", lambda _e: self.apply_fields())
        self.lbl_src_px = ttk.Label(grp_box, text="", foreground="#888")
        self.lbl_src_px.grid(row=4, column=0, columnspan=4, sticky="w", padx=6)
        self.lbl_out_px = ttk.Label(grp_box, text="", foreground="#888")
        self.lbl_out_px.grid(row=7, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        # background + output settings
        grp_cfg = ttk.LabelFrame(bottom, text="Background & output")
        grp_cfg.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self.var_bg_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(grp_cfg, text="blurred background", variable=self.var_bg_on,
                        command=self._on_settings).grid(row=0, column=0, sticky="w", padx=6, pady=(6, 0))
        self.var_bg_zoom = tk.DoubleVar(value=1.4)
        self.var_bg_blur = tk.DoubleVar(value=40.0)
        self.lbl_zoom = ttk.Label(grp_cfg, text="zoom 1.40")
        self.lbl_zoom.grid(row=1, column=0, sticky="w", padx=6)
        sc1 = ttk.Scale(grp_cfg, from_=1.0, to=3.0, variable=self.var_bg_zoom,
                        command=lambda _v: self._on_settings())
        sc1.grid(row=1, column=1, sticky="ew", padx=6)
        self.lbl_blur = ttk.Label(grp_cfg, text="blur 40")
        self.lbl_blur.grid(row=2, column=0, sticky="w", padx=6)
        sc2 = ttk.Scale(grp_cfg, from_=0.0, to=100.0, variable=self.var_bg_blur,
                        command=lambda _v: self._on_settings())
        sc2.grid(row=2, column=1, sticky="ew", padx=6)
        grp_cfg.columnconfigure(1, weight=1)

        row2 = ttk.Frame(grp_cfg)
        row2.grid(row=3, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 6))
        self.var_out_w = tk.StringVar(value="1080")
        self.var_out_h = tk.StringVar(value="1920")
        self.var_crf = tk.StringVar(value="18")
        self.var_preset = tk.StringVar(value="medium")
        for label, var, width in (("out W", self.var_out_w, 6), ("out H", self.var_out_h, 6),
                                  ("CRF", self.var_crf, 4)):
            ttk.Label(row2, text=label).pack(side="left")
            e = ttk.Entry(row2, textvariable=var, width=width, justify="right")
            e.pack(side="left", padx=(2, 8))
            e.bind("<Return>", lambda _e: self.apply_fields())
            e.bind("<FocusOut>", lambda _e: self.apply_fields())
        ttk.Label(row2, text="preset").pack(side="left")
        cb = ttk.Combobox(row2, textvariable=self.var_preset, width=9, state="readonly",
                          values=["ultrafast", "superfast", "veryfast", "faster", "fast",
                                  "medium", "slow", "slower", "veryslow"])
        cb.pack(side="left", padx=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.apply_fields())

        self.status = ttk.Label(bottom, text="", foreground="#666")
        self.status.grid(row=1, column=0, columnspan=3, sticky="w", padx=2, pady=(6, 0))

        self.root.bind("<Delete>", self._on_delete_key)
        self.root.bind("<Control-s>", lambda _e: self.save())
        self.root.bind("<Control-z>", self._on_undo_key)
        self.root.bind("<Control-y>", self._on_redo_key)
        self.root.bind("<Control-Shift-Z>", self._on_redo_key)

        self._sync_history_buttons()

    def _on_undo_key(self, _event=None):
        # don't undo while the user is typing in a text field (entries have
        # their own editing behavior)
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text, ttk.Entry)):
            return
        self.undo()
        return "break"

    def _on_redo_key(self, _event=None):
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text, ttk.Entry)):
            return
        self.redo()
        return "break"

    def _on_delete_key(self, _event=None) -> None:
        # don't delete a box while the user is typing in a text field
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text, ttk.Entry)):
            return
        self.remove_box()

    # ---------------------------------------------------- videos/time
    def open_video(self) -> None:
        path = filedialog.askopenfilename(
            title="Open a video",
            filetypes=[("Videos", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv *.wmv *.ts"),
                       ("All files", "*.*")],
        )
        if path:
            self.load_video(Path(path))

    def _on_video_picked(self, _event=None) -> None:
        name = self.var_video.get()
        for path in self.videos:
            if path.name == name:
                self.load_video(path)
                return

    def load_video(self, path: Path) -> None:
        ffmpeg = ms.find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("ffmpeg not found",
                                 "Install ffmpeg or 'pip install imageio-ffmpeg'.")
            return
        self.video = path
        self.var_video.set(path.name)
        if path not in self.videos:
            self.videos.append(path)
            self.combo_video.configure(values=[p.name for p in self.videos])
        self.duration, self.fps = probe_video(ffmpeg, path)
        self.scale_time.configure(to=max(self.duration, 1.0))
        self.time = min(self.time, self.duration)
        self.lbl_duration.configure(text=f"/ {fmt_time(self.duration)}   "
                                         f"(drag the slider, release to load the frame)")
        self._bg_cache = None

        # a brand-new layout is seeded from the CONFIG block for THIS video
        frame = extract_frame(ffmpeg, path, self.time)
        if frame is not None:
            self.frame = frame
            self.src_w, self.src_h = frame.size
        if self.fresh_default and self.src_w:
            self.layout = ms.default_layout(self.src_w, self.src_h)
            self.fresh_default = False
            self._history_reset()
        self.lbl_left_title.configure(
            text=f"SOURCE — crop boxes — {path.name} ({self.src_w}x{self.src_h} @ {self.fps:g} fps)")
        self._left_cache = None
        self.refresh()
        self.status.configure(text=f"loaded {path.name}")

    def step_time(self, dt: float) -> None:
        self.set_time(self.time + dt)

    def _on_scale_move(self, value: str) -> None:
        self.var_t.set(fmt_time(float(value)))

    def _on_scale_release(self, _event=None) -> None:
        self.set_time(float(self.scale_time.get()))

    def _on_time_entry(self, _event=None) -> None:
        t = parse_time(self.var_t.get())
        if t is None:
            self.var_t.set(fmt_time(self.time))
            return
        self.set_time(t)

    def set_time(self, t: float) -> None:
        if self.video is None:
            return
        t = clamp(t, 0.0, self.duration or 1.0)
        if abs(t - self.time) < 1e-6 and self.frame is not None:
            return
        self.time = t
        self.scale_time.set(t)
        self.var_t.set(fmt_time(t))
        frame = extract_frame(ms.find_ffmpeg(), self.video, t)
        if frame is None:
            self.status.configure(text=f"could not grab the frame at {fmt_time(t)}")
            return
        self.frame = frame
        self.src_w, self.src_h = frame.size
        self._left_cache = None
        self._bg_cache = None
        self.refresh()

    # ----------------------------------------------------- box fields
    def _on_toggle(self) -> None:
        box = self.current_box()
        if box is None:
            return
        box["visible"] = bool(self.var_visible.get())
        box["keep_aspect"] = bool(self.var_keep_aspect.get())
        self._after_edit()

    def _on_settings(self) -> None:
        self.layout["background"]["enabled"] = bool(self.var_bg_on.get())
        self.layout["background"]["zoom"] = max(1.0, float(self.var_bg_zoom.get()))
        self.layout["background"]["blur"] = max(0.0, float(self.var_bg_blur.get()))
        self.lbl_zoom.configure(text=f"zoom {self.layout['background']['zoom']:.2f}")
        self.lbl_blur.configure(text=f"blur {self.layout['background']['blur']:.0f}")
        self._bg_cache = None
        self._after_edit()

    def apply_fields(self) -> None:
        try:
            self.layout["output"]["width"] = max(2, int(float(self.var_out_w.get())))
            self.layout["output"]["height"] = max(2, int(float(self.var_out_h.get())))
            self.layout["output"]["crf"] = int(float(self.var_crf.get()))
        except ValueError:
            self.status.configure(text="output size/CRF must be whole numbers - value ignored")
            self.sync_fields()
            return
        self.layout["output"]["preset"] = self.var_preset.get()
        box = self.current_box()
        if box is not None:
            try:
                for rect_name, vars_ in (("src", self.var_src), ("out", self.var_out)):
                    for key in "xywh":
                        box[rect_name][key] = float(vars_[key].get())
            except ValueError:
                self.status.configure(text="box fields must be numbers - value ignored")
                self.sync_fields()
                return
        self._after_edit()

    def sync_fields(self) -> None:
        box = self.current_box()
        for vars_, rect_name in ((self.var_src, "src"), (self.var_out, "out")):
            for key in "xywh":
                vars_[key].set(f"{box[rect_name][key]:.4f}" if box else "")
        self.var_name.set(box["name"] if box else "—")
        self.var_visible.set(bool(box["visible"]) if box else True)
        self.var_keep_aspect.set(bool(box["keep_aspect"]) if box else True)
        if box and self.src_w:
            s, o = box["src"], box["out"]
            self.lbl_src_px.configure(
                text=f"= px  x {s['x'] * self.src_w:.0f}   y {s['y'] * self.src_h:.0f}   "
                     f"w {s['w'] * self.src_w:.0f}   h {s['h'] * self.src_h:.0f}")
            out = self.layout["output"]
            self.lbl_out_px.configure(
                text=f"= px  x {o['x'] * out['width']:.0f}   y {o['y'] * out['height']:.0f}   "
                     f"w {o['w'] * out['width']:.0f}   h {o['h'] * out['height']:.0f}")
        else:
            self.lbl_src_px.configure(text="")
            self.lbl_out_px.configure(text="")
        self.var_out_w.set(str(self.layout["output"]["width"]))
        self.var_out_h.set(str(self.layout["output"]["height"]))
        self.var_crf.set(str(self.layout["output"]["crf"]))
        self.var_preset.set(self.layout["output"]["preset"])

    def current_box(self) -> dict | None:
        boxes = self.layout["boxes"]
        if 0 <= self.selected < len(boxes):
            return boxes[self.selected]
        return None

    def _after_edit(self) -> None:
        self.layout = ms.normalize_layout(self.layout)
        self.mark_dirty()
        self.refresh()

    # ------------------------------------------------- undo / redo
    def _history_reset(self) -> None:
        """Forget the undo history and anchor it at the current layout."""
        self._cancel_commit()
        self._snap = copy.deepcopy(self.layout)
        self._undo = []
        self._redo = []
        self._sync_history_buttons()

    def _cancel_commit(self) -> None:
        if self._commit_job is not None:
            self.root.after_cancel(self._commit_job)
            self._commit_job = None

    def _schedule_commit(self) -> None:
        """Record ONE undo step for a burst of changes (mouse drags, slider
        scrubs, typing) - the step is committed once the changes settle."""
        self._cancel_commit()
        self._commit_job = self.root.after(400, self._commit)

    def _commit(self) -> None:
        self._commit_job = None
        if self.layout == self._snap:
            return
        self._undo.append(self._snap)
        del self._undo[:-UNDO_LIMIT]   # keep the history bounded
        self._redo.clear()
        self._snap = copy.deepcopy(self.layout)
        self._sync_history_buttons()

    def _apply_state(self, state: dict) -> None:
        self.layout = ms.normalize_layout(copy.deepcopy(state))
        self._snap = copy.deepcopy(self.layout)
        self._bg_cache = None
        self.selected = min(self.selected, len(self.layout["boxes"]) - 1)
        self.mark_dirty()
        self.refresh()
        self._sync_history_buttons()

    def undo(self) -> None:
        self._cancel_commit()
        if self.layout == self._snap and not self._undo:
            self.status.configure(text="nothing to undo")
            self._sync_history_buttons()
            return
        self._redo.append(copy.deepcopy(self.layout))
        # an uncommitted change is undone back to the last committed state,
        # otherwise pop the previous committed state
        state = self._snap if self.layout != self._snap else self._undo.pop()
        self._apply_state(state)
        self.status.configure(text="undone - Ctrl+Y to redo")

    def redo(self) -> None:
        self._cancel_commit()
        if not self._redo:
            self.status.configure(text="nothing to redo")
            self._sync_history_buttons()
            return
        self._undo.append(copy.deepcopy(self.layout))
        self._apply_state(self._redo.pop())
        self.status.configure(text="redone")

    def _sync_history_buttons(self) -> None:
        can_undo = bool(self._undo) or self.layout != self._snap
        self.btn_undo.configure(state="normal" if can_undo else "disabled")
        self.btn_redo.configure(state="normal" if self._redo else "disabled")

    # --------------------------------------------------- box actions
    def select(self, index: int) -> None:
        self.selected = index
        self._sync_listbox()
        self.refresh()

    def add_box(self) -> None:
        n = len(self.layout["boxes"]) + 1
        self.layout["boxes"].append({
            "name": f"box {n}", "visible": True, "keep_aspect": True,
            "src": {"x": 0.3, "y": 0.35, "w": 0.4, "h": 0.3},
            "out": {"x": 0.1, "y": 0.35, "w": 0.8, "h": 0.3},
        })
        self.select(len(self.layout["boxes"]) - 1)
        self._after_edit()

    def remove_box(self) -> None:
        if self.selected < 0:
            return
        name = self.layout["boxes"][self.selected]["name"]
        if not messagebox.askyesno("Remove box", f"Remove '{name}'?"):
            return
        del self.layout["boxes"][self.selected]
        self.selected = min(self.selected, len(self.layout["boxes"]) - 1)
        self._after_edit()
        self._sync_listbox()

    def rename_box(self) -> None:
        box = self.current_box()
        if box is None:
            return
        name = simpledialog.askstring("Rename box", "Name:", initialvalue=box["name"],
                                      parent=self.root)
        if name:
            box["name"] = name.strip()
            self._after_edit()
            self._sync_listbox()

    def move_box(self, delta: int) -> None:
        i = self.selected
        j = i + delta
        boxes = self.layout["boxes"]
        if i < 0 or not (0 <= j < len(boxes)):
            return
        boxes[i], boxes[j] = boxes[j], boxes[i]
        self.selected = j
        self._after_edit()
        self._sync_listbox()

    def reset_defaults(self) -> None:
        if not messagebox.askyesno("Reset", "Replace the current layout with the defaults?"):
            return
        self.layout = ms.default_layout(self.src_w or 1920, self.src_h or 1080)
        self.selected = -1
        self._after_edit()
        self._sync_listbox()

    def _sync_listbox(self) -> None:
        self.listbox.delete(0, "end")
        for box in self.layout["boxes"]:
            self.listbox.insert("end", box["name"] + ("" if box["visible"] else "  (hidden)"))
        if 0 <= self.selected < len(self.layout["boxes"]):
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self.selected)

    def _on_list_select(self, _event=None) -> None:
        sel = self.listbox.curselection()
        if sel and sel[0] != self.selected:
            self.select(sel[0])

    # -------------------------------------------------- mouse / drag
    def _map(self, side: str) -> tuple[int, int, int, int]:
        return self.left_map if side == "src" else self.right_map

    def _canvas(self, side: str) -> tk.Canvas:
        return self.left_canvas if side == "src" else self.right_canvas

    def _frac_at(self, side: str, event) -> tuple[float, float]:
        x0, y0, w, h = self._map(side)
        if w <= 0 or h <= 0:
            return 0.0, 0.0
        return clamp((event.x - x0) / w, 0.0, 1.0), clamp((event.y - y0) / h, 0.0, 1.0)

    def _rect_px(self, side: str, box: dict) -> tuple[float, float, float, float]:
        x0, y0, w, h = self._map(side)
        r = box[side]
        return x0 + r["x"] * w, y0 + r["y"] * h, r["w"] * w, r["h"] * h

    def _hit_handle(self, side: str, event) -> str | None:
        box = self.current_box()
        if box is None:
            return None
        x, y, w, h = self._rect_px(side, box)
        points = {"nw": (x, y), "n": (x + w / 2, y), "ne": (x + w, y),
                  "e": (x + w, y + h / 2), "se": (x + w, y + h),
                  "s": (x + w / 2, y + h), "sw": (x, y + h), "w": (x, y + h / 2)}
        for mode, (px, py) in points.items():
            if abs(event.x - px) <= HANDLE + 2 and abs(event.y - py) <= HANDLE + 2:
                return mode
        return None

    def _hit_box(self, side: str, event) -> int:
        for i in range(len(self.layout["boxes"]) - 1, -1, -1):
            box = self.layout["boxes"][i]
            if not box["visible"]:
                continue
            x, y, w, h = self._rect_px(side, box)
            if x <= event.x <= x + w and y <= event.y <= y + h:
                return i
        return -1

    def _on_press(self, side: str, event) -> None:
        if self.frame is None:
            return
        handle = self._hit_handle(side, event)
        if handle is None:
            index = self._hit_box(side, event)
            if index < 0:
                return
            if index != self.selected:
                self.select(index)
                handle = self._hit_handle(side, event)
            handle = handle or "move"
        box = self.current_box()
        self.drag = {
            "side": side, "mode": handle, "index": self.selected,
            "start": self._frac_at(side, event),
            "rect0": dict(box[side]),
        }

    def _on_motion(self, side: str, event) -> None:
        if self.drag is None or self.drag["side"] != side:
            return
        box = self.current_box()
        if box is None:
            return
        fx, fy = self._frac_at(side, event)
        sx, sy = self.drag["start"]
        dx, dy = fx - sx, fy - sy
        r = self.drag["rect0"]
        mode = self.drag["mode"]
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]

        if mode == "move":
            x, y = x + dx, y + dy
        else:
            if "w" in mode:
                x, w = x + dx, w - dx
            if "e" in mode:
                w = w + dx
            if "n" in mode:
                y, h = y + dy, h - dy
            if "s" in mode:
                h = h + dy
            w = max(w, MIN_SIZE)
            h = max(h, MIN_SIZE)

        # keep-aspect: the output height follows the width (matches scale=ow:-2)
        if side == "out" and box.get("keep_aspect") and mode != "move":
            out = self.layout["output"]
            src = box["src"]
            aspect = (src["w"] * self.src_w) / (src["h"] * self.src_h) if self.src_h else 1.0
            h = (w * out["width"] / aspect) / out["height"]

        w = min(w, 1.0)
        h = min(h, 1.0)
        if side == "src":
            # a crop must stay inside the source frame (ffmpeg requires it)
            x = clamp(x, 0.0, 1.0 - w)
            y = clamp(y, 0.0, 1.0 - h)
        else:
            # placements may run past the canvas edge - the output clips them
            x = clamp(x, 0.0, 1.0)
            y = clamp(y, 0.0, 1.0)
        box[side].update({"x": x, "y": y, "w": w, "h": h})
        self.layout = ms.normalize_layout(self.layout)
        self.mark_dirty()
        self.refresh()

    def _on_release(self, side: str, _event) -> None:
        if self.drag is not None:
            self.drag = None
            self._commit()  # one drag = exactly one undo step
            self.refresh()

    # ------------------------------------------------------ drawing
    def _on_resize(self, _event=None) -> None:
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(60, self.refresh)

    def draw_left(self) -> None:
        c = self.left_canvas
        c.delete("all")
        if self.frame is None:
            self.left_map = (0, 0, 0, 0)
            c.create_text(c.winfo_width() / 2, c.winfo_height() / 2,
                          text="Open a video to start", fill="#666", font=("", 12))
            return
        cw = max(c.winfo_width(), 2)
        ch = max(c.winfo_height(), 2)
        s = min(cw / self.src_w, ch / self.src_h)
        dw, dh = max(2, int(self.src_w * s)), max(2, int(self.src_h * s))
        x0, y0 = (cw - dw) // 2, (ch - dh) // 2
        self.left_map = (x0, y0, dw, dh)
        cache = self._left_cache
        if cache is None or cache[0] is not self.frame or cache[1:3] != (dw, dh):
            cache = (self.frame, dw, dh,
                     ImageTk.PhotoImage(self.frame.resize((dw, dh), Image.BILINEAR)))
            self._left_cache = cache
        c.create_image(x0, y0, image=cache[3], anchor="nw")
        self._draw_boxes("src")

    def compose_preview(self) -> Image.Image:
        out = self.layout["output"]
        width, height = out["width"], out["height"]
        k = PREVIEW_H / height
        pw, ph = max(2, round(width * k)), max(2, round(height * k))
        canvas = Image.new("RGB", (pw, ph), (0, 0, 0))
        if self.frame is None:
            return canvas

        bg = self.layout["background"]
        if bg["enabled"]:
            zoom, blur = bg["zoom"], bg["blur"]
            cache = self._bg_cache
            if (cache is None or cache[0] is not self.frame or cache[1:3] != (pw, ph)
                    or cache[3] != round(zoom, 3) or cache[4] != round(blur, 3)):
                tw, th = max(2, round(pw * zoom)), max(2, round(ph * zoom))
                fw, fh = self.frame.size
                scale = max(tw / fw, th / fh)
                nw, nh = max(2, round(fw * scale)), max(2, round(fh * scale))
                img = self.frame.resize((nw, nh), Image.BILINEAR)
                left = (nw - pw) // 2
                top = (nh - ph) // 2
                img = img.crop((left, top, left + pw, top + ph))
                if blur > 0:
                    img = img.filter(ImageFilter.GaussianBlur(radius=blur * k))
                cache = (self.frame, pw, ph, round(zoom, 3), round(blur, 3), img)
                self._bg_cache = cache
            canvas.paste(cache[5], (0, 0))

        for box in [b for b in self.layout["boxes"] if b["visible"]]:
            s, o = box["src"], box["out"]
            sw = max(1, s["w"] * self.src_w)
            sh = max(1, s["h"] * self.src_h)
            crop = self.frame.crop((
                round(s["x"] * self.src_w), round(s["y"] * self.src_h),
                round(s["x"] * self.src_w + sw), round(s["y"] * self.src_h + sh)))
            dw = max(2, round(o["w"] * pw))
            if box.get("keep_aspect"):
                dh = max(2, round(dw * (sh / sw)))
            else:
                dh = max(2, round(o["h"] * ph))
            crop = crop.resize((dw, dh), Image.BILINEAR)
            canvas.paste(crop, (round(o["x"] * pw), round(o["y"] * ph)))
        return canvas

    def draw_right(self) -> None:
        c = self.right_canvas
        c.delete("all")
        img = self.compose_preview()
        cw = max(c.winfo_width(), 2)
        ch = max(c.winfo_height(), 2)
        iw, ih = img.size
        s = min(cw / iw, ch / ih)
        dw, dh = max(2, int(iw * s)), max(2, int(ih * s))
        x0, y0 = (cw - dw) // 2, (ch - dh) // 2
        self.right_map = (x0, y0, dw, dh)
        self._right_photo = ImageTk.PhotoImage(img.resize((dw, dh), Image.BILINEAR))
        c.create_image(x0, y0, image=self._right_photo, anchor="nw")
        self._draw_boxes("out")

    def _draw_boxes(self, side: str) -> None:
        c = self._canvas(side)
        for i, box in enumerate(self.layout["boxes"]):
            if not box["visible"]:
                continue
            color = BOX_COLORS[i % len(BOX_COLORS)]
            x, y, w, h = self._rect_px(side, box)
            selected = i == self.selected
            c.create_rectangle(x, y, x + w, y + h, outline=color,
                               width=3 if selected else 2, tags="ovl")
            c.create_text(x + 5, y + 5, text=box["name"], anchor="nw", fill=color,
                          font=("", 9, "bold"), tags="ovl")
            if selected:
                for px, py in ((x, y), (x + w / 2, y), (x + w, y), (x + w, y + h / 2),
                               (x + w, y + h), (x + w / 2, y + h), (x, y + h), (x, y + h / 2)):
                    c.create_rectangle(px - HANDLE / 2, py - HANDLE / 2,
                                       px + HANDLE / 2, py + HANDLE / 2,
                                       fill=color, outline="#111", tags="ovl")

    def refresh(self) -> None:
        self._resize_job = None
        self.draw_left()
        self.draw_right()
        self.sync_fields()
        self._sync_listbox()

    # ------------------------------------------------- exact render
    def render_exact(self) -> None:
        if self.video is None:
            messagebox.showinfo("No video", "Load a video first.")
            return
        ffmpeg = ms.find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("ffmpeg not found",
                                 "Install ffmpeg or 'pip install imageio-ffmpeg'.")
            return
        fc = ms.build_filter_complex_from_layout(self.layout)
        tmp = Path(tempfile.mkdtemp(prefix="ytshort_exact_"))
        dst = tmp / "exact.png"
        cmd = ms.build_preview_cmd(ffmpeg, self.video, dst, fc, self.time)
        self.status.configure(text="rendering exact frame with ffmpeg…")
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        res = subprocess.run(cmd, capture_output=True)
        self.root.config(cursor="")
        if res.returncode != 0 or not dst.exists():
            messagebox.showerror("ffmpeg failed", (res.stderr or b"").decode(errors="replace")[-2000:])
            return
        self.status.configure(text=f"exact frame rendered to {dst}")

        win = tk.Toplevel(self.root)
        win.title(f"Exact ffmpeg frame @ {fmt_time(self.time)} (approx preview stays behind)")
        img = Image.open(dst)
        out = self.layout["output"]
        h = PREVIEW_H
        w = max(2, round(h * out["width"] / out["height"]))
        photo = ImageTk.PhotoImage(img.resize((w, h), Image.BILINEAR))
        ttk.Label(win, image=photo).pack(side="top", padx=8, pady=8)
        win.photo = photo  # keep a reference
        cmd_text = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        text = tk.Text(win, height=7, wrap="char", font=("Consolas", 8))
        text.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))
        text.insert("1.0", cmd_text + "\n\n" + fc)
        text.configure(state="disabled")

    # ------------------------------------------------------ file ops
    def mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()
        self._schedule_commit()  # every change becomes one undo step (debounced)

    def _update_title(self) -> None:
        star = "*" if self.dirty else ""
        self.root.title(f"Layout editor — {self.layout_path}{star}")

    def save(self) -> None:
        try:
            ms.save_layout(self.layout_path, self.layout)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.dirty = False
        self._update_title()
        self.status.configure(text=f"saved {self.layout_path}")

    def save_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save layout as", defaultextension=".json",
            initialfile=self.layout_path.name, filetypes=[("Layout JSON", "*.json")])
        if not path:
            return
        self.layout_path = Path(path)
        self.save()

    def load_from(self) -> None:
        path = filedialog.askopenfilename(
            title="Load layout", filetypes=[("Layout JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.layout = ms.load_layout(Path(path))
        except ValueError as exc:
            messagebox.showerror("Bad layout file", str(exc))
            return
        self.layout_path = Path(path)
        self.selected = -1
        self.dirty = False
        self._bg_cache = None
        self._history_reset()
        self._update_title()
        self.refresh()
        self.status.configure(text=f"loaded {self.layout_path}")

    def on_close(self) -> None:
        if self.dirty:
            answer = messagebox.askyesnocancel("Unsaved changes",
                                               "Save the layout before closing?")
            if answer is None:
                return
            if answer:
                self.save()
                if self.dirty:  # save failed
                    return
        self.root.destroy()


# ============================================================== entry
def run_editor(layout_path: Path, videos: list[Path]) -> int:
    """Open the editor on `layout_path` (created from defaults if missing)."""
    if not ms.find_ffmpeg():
        print("error: ffmpeg not found.\n"
              "  - install it (e.g. 'winget install ffmpeg' on Windows) and add it to PATH,\n"
              "  - or run: python -m pip install imageio-ffmpeg")
        return 1

    fresh = False
    if layout_path.exists():
        try:
            layout = ms.load_layout(layout_path)
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
    else:
        layout = ms.default_layout()
        fresh = True

    root = tk.Tk()
    LayoutEditor(root, layout_path, layout, videos, fresh_default=fresh)
    root.mainloop()
    return 0


if __name__ == "__main__":
    # allow running the editor directly: python layout_editor.py [file.json]
    ms.ensure_dirs()
    args = sys.argv[1:]
    path = Path(args[0]) if args else Path(ms.DEFAULT_LAYOUT_NAME)
    candidates = ms.collect_inputs([ms.INPUT_DIR]) if Path(ms.INPUT_DIR).exists() else []
    sys.exit(run_editor(path, candidates))
