#!/usr/bin/env python3
"""
gui.py - small control panel for make_shorts.py.

Lists the videos in ./input (or any you add), lets you tick the layouts to
render (one output per layout, e.g. YouTube + TikTok, named video_layout.mp4),
renders them all with per-video progress and the ffmpeg log, can cancel,
renders a single test frame, and opens the layout editor. Each video can be
trimmed (start/end), and several can be joined into one short in list order.

Usage:
    python gui.py        # pythonw gui.py hides the console window on Windows
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

HERE = Path(__file__).resolve().parent
os.chdir(HERE)  # make_shorts uses folders relative to the project
sys.path.insert(0, str(HERE))

import make_shorts as ms  # noqa: E402

BUILTIN = "(CONFIG embutido)"
BUILTIN_STEM = "config"  # output suffix for the built-in layout
SETTINGS_FILE = HERE / ms.INPUT_DIR / "gui_settings.json"
# layouts encoded by one ffmpeg run: the gameplay is decoded once for all of
# them; capped so a long list doesn't exhaust GPU encoder sessions / memory
MAX_OUTPUTS = 4
NO_WINDOW = ms.NO_WINDOW
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

ST_PENDING, ST_RUNNING, ST_DONE, ST_FAILED, ST_CANCELLED = (
    "pendente", "renderizando", "concluído", "erro", "cancelado",
)


def parse_time(text: str) -> float | None:
    """"83", "1:23" or "1:23.5" -> seconds; blank -> None. Raises ValueError."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    if secs < 0:
        raise ValueError(text)
    return secs


def fmt_time(secs: float) -> str:
    m, s = divmod(secs, 60)
    text = f"{s:04.1f}"
    return f"{int(m)}:{text[:-2] if text.endswith('.0') else text}"


def trim_text(entry: dict) -> str:
    """Trim column text: "" for the whole video, else "0:05 → 0:20" / "0:05 → fim"."""
    start, end = entry.get("start"), entry.get("end")
    if start is None and end is None:
        return ""
    return f"{fmt_time(start or 0)} → {fmt_time(end) if end is not None else 'fim'}"


def open_path(path: Path) -> None:
    """Open a file or folder with the OS default app."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ffmpeg = ms.find_ffmpeg()
        self.events: queue.Queue = queue.Queue()  # worker thread -> UI thread
        self.worker: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel = threading.Event()
        self.files: dict[str, Path] = {}  # treeview item id -> video path
        self.titles = ms.load_titles()     # video file name -> {"title", "credit"}
        self.encoder_keys: dict[str, str] = {}  # dropdown label -> ms.ENCODERS key
        self.layout_vars: dict[str, tk.BooleanVar] = {}  # layout file name -> ticked
        self.checked_layouts: set[str] = set(self.load_settings().get("layouts", []))

        ms.ensure_dirs()
        root.title("ShortVideoMaker")
        root.geometry("920x760")
        root.minsize(720, 600)
        self._build_ui()
        self.refresh_layouts()
        self.load_input_dir()

        if self.ffmpeg:
            self.log(f"ffmpeg: {self.ffmpeg}")
            threading.Thread(
                target=lambda: self.events.put(("encoders", ms.detect_encoders(self.ffmpeg))),
                daemon=True,
            ).start()
        else:
            self.log("ffmpeg não encontrado: instale (winget install ffmpeg) "
                     "ou rode: python -m pip install imageio-ffmpeg")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)

    # ------------------------------------------------------------- UI --
    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        self.btn_edit = ttk.Button(top, text="Editar layout ▾", command=self.edit_layout_menu)
        self.btn_edit.pack(side="left", padx=(0, 2))
        ttk.Button(top, text="↻", width=3, command=self.refresh_layouts).pack(side="left", padx=2)
        self.var_encoder = tk.StringVar(value="detectando GPU…")
        self.combo_encoder = ttk.Combobox(top, textvariable=self.var_encoder,
                                          state="disabled", width=26)
        self.combo_encoder.pack(side="right")
        ttk.Label(top, text="Aceleração:").pack(side="right", padx=(12, 4))

        # one checkbox per saved layout: every ticked one is a separate output
        self.frm_layouts = ttk.LabelFrame(
            self.root, text="Saídas (um vídeo por layout marcado: nomeVideo_nomeLayout.mp4)",
            padding=(6, 2))
        self.frm_layouts.pack(fill="x", padx=8, pady=(0, 6))

        # ---- video list ----
        mid = ttk.LabelFrame(self.root, text="Vídeos", padding=6)
        mid.pack(fill="both", expand=True, padx=8)
        btns = ttk.Frame(mid)
        btns.pack(side="right", fill="y", padx=(6, 0))
        for text, cmd in (
            ("Adicionar…", self.add_files),
            ("Remover", self.remove_selected),
            ("Recarregar input/", self.load_input_dir),
            ("Título / trecho…", self.edit_texts),
            ("Prévia (1 frame)", self.preview_frame),
        ):
            ttk.Button(btns, text=text, command=cmd).pack(fill="x", pady=2)
        order = ttk.Frame(btns)
        order.pack(fill="x", pady=2)
        ttk.Button(order, text="↑", width=3, command=lambda: self.move_selected(-1)).pack(
            side="left", expand=True, fill="x")
        ttk.Button(order, text="↓", width=3, command=lambda: self.move_selected(1)).pack(
            side="left", expand=True, fill="x")
        ttk.Label(btns, text="Sem seleção =\nrenderiza todos\nos pendentes.\n\nDuplo clique =\n"
                             "título, crédito\ne trecho.\n\n↑ ↓ = ordem ao\njuntar vídeos.",
                  foreground="#888", justify="left").pack(fill="x", pady=(10, 0))

        self.tree = ttk.Treeview(mid, columns=("title", "credit", "trim", "status"),
                                 selectmode="extended")
        self.tree.heading("#0", text="Arquivo", anchor="w")
        self.tree.heading("title", text="Título", anchor="w")
        self.tree.heading("credit", text="Crédito", anchor="w")
        self.tree.heading("trim", text="Trecho", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=230)
        self.tree.column("title", width=180)
        self.tree.column("credit", width=100)
        self.tree.column("trim", width=110, stretch=False)
        self.tree.column("status", width=120, stretch=False)
        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Double-1>", self.edit_texts)

        # ---- options ----
        opts = ttk.Frame(self.root, padding=(8, 6))
        opts.pack(fill="x")
        ttk.Label(opts, text="Saída:").pack(side="left")
        self.var_out = tk.StringVar(value=ms.OUTPUT_DIR)
        ttk.Entry(opts, textvariable=self.var_out, width=34).pack(side="left", padx=4)
        ttk.Button(opts, text="…", width=3, command=self.choose_output).pack(side="left")
        self.var_move = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text=f"mover originais para {ms.CONVERTED_DIR}/ após renderizar",
                        variable=self.var_move).pack(side="left", padx=12)

        # ---- progress ----
        prog = ttk.Frame(self.root, padding=(8, 0))
        prog.pack(fill="x")
        self.lbl_status = ttk.Label(prog, text="Pronto.")
        self.lbl_status.pack(anchor="w")
        self.bar_file = ttk.Progressbar(prog, maximum=100)
        self.bar_file.pack(fill="x", pady=2)
        self.bar_total = ttk.Progressbar(prog, maximum=100)
        self.bar_total.pack(fill="x", pady=2)

        # ---- actions ----
        actions = ttk.Frame(self.root, padding=8)
        actions.pack(fill="x")
        self.btn_render = ttk.Button(actions, text="▶ Renderizar", command=self.start_render)
        self.btn_render.pack(side="left")
        self.btn_cancel = ttk.Button(actions, text="■ Cancelar", command=self.cancel_render,
                                     state="disabled")
        self.btn_join = ttk.Button(actions, text="⧉ Juntar selecionados", command=self.start_join)
        self.btn_join.pack(side="left", padx=4)
        self.btn_cancel.pack(side="left", padx=4)
        ttk.Button(actions, text="Abrir pasta de saída", command=self.open_output).pack(side="right")

        self.txt_log = ScrolledText(self.root, height=8, state="disabled", font=("Consolas", 9))
        self.txt_log.pack(fill="both", padx=8, pady=(0, 8))

    def log(self, text: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text.rstrip() + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def set_running(self, running: bool) -> None:
        """Lock the render buttons and the encoder choice while the worker runs."""
        idle = "disabled" if running else "normal"
        self.btn_render.configure(state=idle)
        self.btn_join.configure(state=idle)
        self.btn_cancel.configure(state="normal" if running else "disabled")
        if running:
            self.combo_encoder.configure(state="disabled")
        elif self.encoder_keys:
            self.combo_encoder.configure(state="readonly")

    def output_dir(self) -> Path:
        return Path(self.var_out.get().strip() or ms.OUTPUT_DIR)

    # ------------------------------------------------------- encoders --
    def set_encoders(self, available: list[str]) -> None:
        """Fill the dropdown with "Auto (best)" plus every encoder that works here."""
        auto = f"Auto → {ms.ENCODERS[available[0]]}"
        self.encoder_keys = {auto: available[0]}
        self.encoder_keys.update({ms.ENCODERS[e]: e for e in available})
        self.combo_encoder.configure(values=list(self.encoder_keys), state="readonly")
        self.var_encoder.set(auto)
        missing = [ms.ENCODERS[e] for e in ms.GPU_ORDER if e not in available]
        self.log(f"aceleração disponível: {', '.join(ms.ENCODERS[e] for e in available)}"
                 + (f" (não encontrado: {', '.join(missing)})" if missing else ""))

    def selected_encoder(self) -> str:
        return self.encoder_keys.get(self.var_encoder.get(), "cpu")  # cpu while still detecting

    # -------------------------------------------------------- layouts --
    def load_settings(self) -> dict:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_settings(self) -> None:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps({"layouts": sorted(self.checked_layouts)},
                                                indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass  # only a convenience: the ticks just won't be remembered

    def refresh_layouts(self) -> None:
        """Rebuild the layout checkboxes from the *.json files, keeping the ticks."""
        names = sorted(p.name for p in HERE.glob("*.json")) + [BUILTIN]
        if not self.checked_layouts & set(names):  # first run, or the ticked files are gone
            self.checked_layouts = {ms.DEFAULT_LAYOUT_NAME if ms.DEFAULT_LAYOUT_NAME in names
                                    else BUILTIN}
        for child in self.frm_layouts.winfo_children():
            child.destroy()
        self.layout_vars = {}
        cols = 4
        for i, name in enumerate(names):
            var = tk.BooleanVar(value=name in self.checked_layouts)
            var.trace_add("write", lambda *_a, n=name: self.toggle_layout(n))
            label = name if name == BUILTIN else Path(name).stem
            ttk.Checkbutton(self.frm_layouts, text=label, variable=var).grid(
                row=i // cols, column=i % cols, sticky="w", padx=(0, 16))
            self.layout_vars[name] = var

    def toggle_layout(self, name: str) -> None:
        if self.layout_vars[name].get():
            self.checked_layouts.add(name)
        else:
            self.checked_layouts.discard(name)
        self.save_settings()

    def selected_layouts(self) -> list[tuple[str, dict | None]]:
        """(output suffix, layout) for every ticked layout; None = built-in CONFIG.
        Raises if one of the files can't be read."""
        result = []
        for name, var in self.layout_vars.items():
            if not var.get():
                continue
            if name == BUILTIN:
                result.append((BUILTIN_STEM, None))
            else:
                result.append((Path(name).stem, ms.load_layout(HERE / name)))
        return result

    def texts_for(self, path: Path) -> dict:
        """The {title}/{credit} values of a video."""
        entry = self.titles.get(path.name, {})
        return {k: entry[k] for k in ("title", "credit") if k in entry}

    def trim_for(self, path: Path) -> tuple[float, float | None]:
        """(start, end) in seconds; end None = until the end of the video."""
        entry = self.titles.get(path.name, {})
        return float(entry.get("start") or 0.0), entry.get("end")

    def edit_layout_menu(self) -> None:
        """Pop up the layout list under the button; the pick opens in the editor."""
        menu = tk.Menu(self.root, tearoff=False)
        for name in self.layout_vars:
            menu.add_command(label=name, command=lambda n=name: self.edit_layout(n))
        menu.add_separator()
        menu.add_command(label="Novo layout…", command=self.new_layout)
        btn = self.btn_edit
        menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())

    def new_layout(self) -> None:
        name = simpledialog.askstring("Novo layout", "Nome do layout (ex.: tiktok):",
                                      parent=self.root)
        name = re.sub(r'[<>:"/\\|?*]', "", (name or "").strip()).removesuffix(".json")
        if name:
            self.checked_layouts.add(f"{name}.json")  # ticked once the editor saves it
            self.edit_layout(f"{name}.json")

    def edit_layout(self, name: str) -> None:
        target = ms.DEFAULT_LAYOUT_NAME if name == BUILTIN else name
        cmd = [sys.executable, str(HERE / "make_shorts.py"), "--config", target]
        cmd += [str(p) for p in self.selected_paths()]  # editor defaults to input/
        proc = subprocess.Popen(cmd)

        def wait() -> None:  # pick up a newly saved layout file when the editor closes
            proc.wait()
            self.events.put(("layouts",))

        threading.Thread(target=wait, daemon=True).start()

    # ---------------------------------------------------------- files --
    def selected_paths(self) -> list[Path]:
        return [self.files[iid] for iid in self.tree.selection()]

    def add_paths(self, paths: list[Path]) -> None:
        known = {p.resolve() for p in self.files.values()}
        for p in paths:
            if p.resolve() in known:
                continue
            known.add(p.resolve())
            entry = self.titles.get(p.name, {})
            iid = self.tree.insert("", "end", text=p.name, values=(
                entry.get("title", ""), entry.get("credit", ""), trim_text(entry), ST_PENDING))
            self.files[iid] = p

    def edit_texts(self, _event=None) -> None:
        """Dialog for the {title}/{credit} and the trim of the selected video(s)."""
        iids = self.tree.selection()
        if not iids:
            messagebox.showinfo("Título / trecho", "Selecione um ou mais vídeos na lista.")
            return
        entries = [self.titles.get(self.files[iid].name, {}) for iid in iids]

        win = tk.Toplevel(self.root)
        win.title("Título / trecho")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack()
        caption = self.files[iids[0]].name if len(iids) == 1 else f"{len(iids)} vídeos selecionados"
        ttk.Label(frm, text=caption, font=("", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                                 sticky="w", pady=(0, 8))
        fields: dict[str, tk.StringVar] = {}
        mixed: dict[str, bool] = {}
        for row, (key, label) in enumerate((("title", "Título  {title}"),
                                            ("credit", "Crédito  {credit}"),
                                            ("start", "Início (ex.: 0:05)"),
                                            ("end", "Fim (vazio = até o final)")), start=1):
            if key in ("start", "end"):
                values = {fmt_time(e[key]) if e.get(key) is not None else "" for e in entries}
            else:
                values = {e.get(key, "") for e in entries}
            mixed[key] = len(values) > 1  # a blank mixed field keeps each video's own value
            fields[key] = tk.StringVar(value="" if mixed[key] else values.pop())
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            entry = ttk.Entry(frm, textvariable=fields[key], width=50 if row < 3 else 12)
            entry.grid(row=row, column=1, pady=3, sticky="w")
            if row == 1:
                entry.focus_set()
        ttk.Label(frm, text="Trecho: segundos (83) ou minutos:segundos (1:23).",
                  foreground="#888").grid(row=5, column=0, columnspan=2, sticky="w")
        if len(iids) > 1:
            ttk.Label(frm, text="Campos em branco com valores diferentes ficam como estão.",
                      foreground="#888").grid(row=6, column=0, columnspan=2, sticky="w")

        def save(_event=None) -> None:
            try:
                times = {k: parse_time(fields[k].get()) for k in ("start", "end")}
            except ValueError:
                messagebox.showerror("Trecho", "Use segundos (83) ou minutos:segundos (1:23).",
                                     parent=win)
                return
            updated = {}
            for iid in iids:
                entry = dict(self.titles.get(self.files[iid].name, {}))
                for key, var in fields.items():
                    value = times[key] if key in times else var.get().strip()
                    if value:  # 0 / "" = no start / no text
                        entry[key] = value
                    elif not mixed[key]:
                        entry.pop(key, None)
                if entry.get("end") is not None and entry["end"] <= entry.get("start", 0):
                    messagebox.showerror("Trecho", f"{self.files[iid].name}: o fim precisa "
                                                   "ser depois do início.", parent=win)
                    return
                updated[iid] = entry
            for iid, entry in updated.items():
                name = self.files[iid].name
                if entry:
                    self.titles[name] = entry
                else:
                    self.titles.pop(name, None)
                self.tree.set(iid, "title", entry.get("title", ""))
                self.tree.set(iid, "credit", entry.get("credit", ""))
                self.tree.set(iid, "trim", trim_text(entry))
            try:
                ms.save_titles(self.titles)
            except OSError as exc:
                messagebox.showerror("Salvar", str(exc), parent=win)
                return
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Salvar", command=save).pack(side="left", padx=2)
        ttk.Button(btns, text="Cancelar", command=win.destroy).pack(side="left", padx=2)
        win.bind("<Return>", save)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.grab_set()

    def add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(ms.VIDEO_EXTS))
        names = filedialog.askopenfilenames(
            title="Escolher vídeos", initialdir=HERE / ms.INPUT_DIR,
            filetypes=[("Vídeos", exts), ("Todos", "*.*")],
        )
        self.add_paths([Path(n) for n in names])

    def move_selected(self, delta: int) -> None:
        """Move the selected rows one place up (-1) or down (+1): the join order."""
        items = sorted(self.tree.selection(), key=self.tree.index, reverse=delta > 0)
        count = len(self.tree.get_children())
        if not items or not 0 <= self.tree.index(items[0]) + delta < count:
            return
        for iid in items:
            self.tree.move(iid, "", self.tree.index(iid) + delta)

    def remove_selected(self) -> None:
        if self.busy():
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            del self.files[iid]

    def load_input_dir(self) -> None:
        """Drop entries whose file is gone, then add any new video in ./input."""
        if self.busy():
            return
        for iid, path in list(self.files.items()):
            if not path.exists():
                self.tree.delete(iid)
                del self.files[iid]
        self.add_paths(ms.collect_inputs([ms.INPUT_DIR]))

    def choose_output(self) -> None:
        folder = filedialog.askdirectory(title="Pasta de saída", initialdir=self.output_dir())
        if folder:
            self.var_out.set(folder)

    def open_output(self) -> None:
        out = self.output_dir()
        out.mkdir(parents=True, exist_ok=True)
        open_path(out)

    # -------------------------------------------------------- preview --
    def preview_frame(self) -> None:
        paths = self.selected_paths()
        if not paths:
            messagebox.showinfo("Prévia", "Selecione um vídeo na lista.")
            return
        if not self.ffmpeg:
            messagebox.showerror("Prévia", "ffmpeg não encontrado.")
            return
        at = simpledialog.askfloat("Prévia", "Segundo do vídeo:", initialvalue=5.0,
                                   minvalue=0.0, parent=self.root)
        if at is None:
            return
        src = paths[0]
        tmp = Path(tempfile.mkdtemp(prefix="shorts_preview_"))
        out = self.output_dir()
        out.mkdir(parents=True, exist_ok=True)
        cmds = []
        try:
            for stem, layout in self.selected_layouts():
                fc, extra = ms.filter_for_video(layout, self.texts_for(src), tmp / stem)
                dst = out / f"{src.stem}_{stem}_preview_{at:g}s.png"
                cmds.append((dst, ms.build_preview_cmd(self.ffmpeg, src, dst, fc, at, extra)))
        except Exception as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            messagebox.showerror("Layout", str(exc))
            return
        if not cmds:
            shutil.rmtree(tmp, ignore_errors=True)
            messagebox.showinfo("Prévia", "Marque pelo menos um layout em Saídas.")
            return
        self.log(f"[prévia] {src.name} @ {at:g}s ({len(cmds)} layout(s))")

        def run() -> None:
            for dst, cmd in cmds:
                dst.unlink(missing_ok=True)
                result = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
                if result.returncode == 0 and dst.exists():
                    self.events.put(("open", dst))
                else:
                    tail = "\n".join(result.stderr.strip().splitlines()[-8:])
                    self.events.put(("log", f"[prévia falhou] {dst.name}\n{tail}"))
            shutil.rmtree(tmp, ignore_errors=True)

        threading.Thread(target=run, daemon=True).start()

    # --------------------------------------------------------- render --
    def start_render(self) -> None:
        if self.busy():
            return
        if not self.ffmpeg:
            messagebox.showerror("Renderizar", "ffmpeg não encontrado.")
            return
        items = list(self.tree.selection()) or [
            iid for iid in self.tree.get_children() if self.tree.set(iid, "status") != ST_DONE
        ]
        if not items:
            messagebox.showinfo("Renderizar", "Nenhum vídeo pendente na lista.")
            return
        try:
            layouts = self.selected_layouts()
        except Exception as exc:
            messagebox.showerror("Layout", str(exc))
            return
        if not layouts:
            messagebox.showinfo("Renderizar", "Marque pelo menos um layout em Saídas.")
            return

        out_dir = self.output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "published").mkdir(exist_ok=True)

        jobs = [(iid, self.files[iid], self.texts_for(self.files[iid]), self.trim_for(self.files[iid]))
                for iid in items]
        encoder = self.selected_encoder()
        self.log(f"layouts: {', '.join(stem for stem, _ in layouts)} | {ms.ENCODERS[encoder]} | "
                 f"{len(jobs)} vídeo(s) -> {out_dir.resolve()}")
        self.cancel.clear()
        self.set_running(True)
        self.bar_file["value"] = self.bar_total["value"] = 0
        self.worker = threading.Thread(
            target=self._render_all, args=(jobs, layouts, out_dir, self.var_move.get(), encoder),
            daemon=True,
        )
        self.worker.start()

    def cancel_render(self) -> None:
        self.cancel.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
        self.lbl_status.configure(text="Cancelando…")

    def _render_all(self, jobs: list[tuple[str, Path, dict, tuple]], layouts: list[tuple[str, dict | None]],
                    out_dir: Path, move: bool, encoder: str) -> None:
        """Worker thread: renders jobs one by one, reports through self.events."""
        done = failed = 0
        tmp = Path(tempfile.mkdtemp(prefix="shorts_overlay_"))
        for n, (iid, src, texts, trim) in enumerate(jobs):
            if self.cancel.is_set():
                break
            outputs = []
            try:
                for stem, layout in layouts:
                    fc, extra = ms.filter_for_video(layout, texts, tmp / f"{n}_{stem}")
                    outputs.append((out_dir / f"{src.stem}_{stem}.mp4", fc, extra))
            except Exception as exc:
                self.events.put(("log", f"[erro]   textos/imagens de {src.name}: {exc}"))
                failed += 1
                self.events.put(("status", iid, ST_FAILED))
                continue
            if self._render_video(iid, src, outputs, encoder, n, len(jobs), trim):
                done += 1
                self.events.put(("status", iid, ST_DONE))
                for dst, _, _ in outputs:
                    self.events.put(("log", f"[ok]     {src.name} -> {dst}"))
                if move:
                    try:
                        moved = ms.move_to_converted(src)
                        if moved != src:
                            self.events.put(("moved", iid, moved))
                    except OSError as exc:
                        self.events.put(("log", f"[aviso]  não foi possível mover {src.name}: {exc}"))
            elif self.cancel.is_set():
                for dst, _, _ in outputs:  # don't leave half-written files behind
                    dst.unlink(missing_ok=True)
                self.events.put(("status", iid, ST_CANCELLED))
            else:
                failed += 1
                self.events.put(("status", iid, ST_FAILED))
        shutil.rmtree(tmp, ignore_errors=True)
        self.events.put(("finished", done, failed, len(jobs)))

    def _render_video(self, iid: str, src: Path, outputs: list[tuple[Path, str, list[str]]],
                      encoder: str, n: int, total: int,
                      trim: tuple[float, float | None] = (0.0, None)) -> bool:
        """Render every (dst, fc, extra_inputs) of one video, up to MAX_OUTPUTS per
        ffmpeg run. A failed combined run is retried one layout at a time, and
        whatever still fails on the GPU is retried on the CPU."""
        todo = outputs
        for enc in dict.fromkeys([encoder, "cpu"]):
            todo = [todo[i:i + MAX_OUTPUTS] for i in range(0, len(todo), MAX_OUTPUTS)]
            if enc != encoder:
                self.events.put(("log", f"[fallback] {ms.ENCODERS[encoder]} falhou, "
                                        f"tentando de novo com {ms.ENCODERS[enc]}"))
            failed = []
            while todo:
                group = todo.pop(0)
                stems = ", ".join(d.stem.removeprefix(f"{src.stem}_") for d, _, _ in group)
                self.events.put(("start", iid, f"{src.name} → {stems}", n, total, ms.ENCODERS[enc]))
                ok = self._run_ffmpeg(ms.build_multi_render_cmd(self.ffmpeg, src, group, enc, trim),
                                      iid, n, total, trim)
                if self.cancel.is_set():
                    return False
                if ok:
                    continue
                if len(group) > 1:
                    self.events.put(("log", "[aviso]  layouts juntos falharam, "
                                            "tentando um de cada vez"))
                    todo[:0] = [[o] for o in group]
                else:
                    failed.append(group)
            if not failed:
                return True
            todo = [o for group in failed for o in group]
        return False

    def start_join(self) -> None:
        """Join the selected videos, in list order, into one short per ticked layout."""
        if self.busy():
            return
        if not self.ffmpeg:
            messagebox.showerror("Juntar", "ffmpeg não encontrado.")
            return
        selected = set(self.tree.selection())
        items = [iid for iid in self.tree.get_children() if iid in selected]
        if len(items) < 2:
            messagebox.showinfo("Juntar", "Selecione dois ou mais vídeos (Ctrl+clique).\n"
                                          "Eles são emendados na ordem da lista (use ↑ ↓).")
            return
        try:
            layouts = self.selected_layouts()
        except Exception as exc:
            messagebox.showerror("Layout", str(exc))
            return
        if not layouts:
            messagebox.showinfo("Juntar", "Marque pelo menos um layout em Saídas.")
            return

        out_dir = self.output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "published").mkdir(exist_ok=True)
        clips = [(iid, self.files[iid], self.trim_for(self.files[iid])) for iid in items]
        texts = self.texts_for(self.files[items[0]])  # the first clip's title/credit
        encoder = self.selected_encoder()
        self.log(f"juntar: {' + '.join(self.files[iid].name for iid in items)} | layouts: "
                 f"{', '.join(stem for stem, _ in layouts)} | {ms.ENCODERS[encoder]}")
        self.cancel.clear()
        self.set_running(True)
        self.bar_file["value"] = self.bar_total["value"] = 0
        self.worker = threading.Thread(
            target=self._join_all,
            args=(clips, texts, layouts, out_dir, self.var_move.get(), encoder), daemon=True,
        )
        self.worker.start()

    def _join_all(self, clips: list[tuple[str, Path, tuple]], texts: dict,
                  layouts: list[tuple[str, dict | None]], out_dir: Path, move: bool,
                  encoder: str) -> None:
        """Worker thread: one joined file per layout, each in a single ffmpeg run."""
        iids = [iid for iid, _, _ in clips]
        first = clips[0][1]
        done = failed = 0
        parts = []
        for iid, src, (start, end) in clips:
            duration, has_audio = ms.probe_media(self.ffmpeg, src)
            end = duration if end is None else min(end, duration or end)
            if end <= start:
                self.events.put(("log", f"[erro]   {src.name}: trecho vazio ou vídeo ilegível"))
                self.events.put(("status", iid, ST_FAILED))
                self.events.put(("finished", 0, len(layouts), len(layouts)))
                return
            parts.append((src, start, end, has_audio))
        length = sum(end - start for _, start, end, _ in parts)

        tmp = Path(tempfile.mkdtemp(prefix="shorts_join_"))
        for n, (stem, layout) in enumerate(layouts):
            if self.cancel.is_set():
                break
            layout = layout or ms.default_layout()
            dst = out_dir / f"{first.stem}_juntado_{stem}.mp4"
            try:
                layers, extra = ms.overlay_layers(layout, texts, tmp / stem)
            except Exception as exc:
                self.events.put(("log", f"[erro]   textos/imagens ({stem}): {exc}"))
                failed += 1
                continue
            ok = False
            for enc in dict.fromkeys([encoder, "cpu"]):
                if enc != encoder:
                    self.events.put(("log", f"[fallback] {ms.ENCODERS[encoder]} falhou, "
                                            f"tentando de novo com {ms.ENCODERS[enc]}"))
                for iid in iids:
                    self.events.put(("status", iid, ST_RUNNING))
                self.events.put(("start", iids[0], f"{len(parts)} clipes → {dst.name}",
                                 n, len(layouts), ms.ENCODERS[enc]))
                cmd = ms.build_join_cmd(self.ffmpeg, parts, layout, layers, extra, dst, enc)
                ok = self._run_ffmpeg(cmd, iids[0], n, len(layouts), length=length)
                if ok or self.cancel.is_set():
                    break
            if ok:
                done += 1
                self.events.put(("log", f"[ok]     {len(parts)} clipes -> {dst}"))
            elif self.cancel.is_set():
                dst.unlink(missing_ok=True)  # don't leave a half-written file behind
            else:
                failed += 1
        shutil.rmtree(tmp, ignore_errors=True)

        status = (ST_CANCELLED if self.cancel.is_set() and not failed
                  else ST_DONE if done == len(layouts) else ST_FAILED)
        for iid in iids:
            self.events.put(("status", iid, status))
        if status == ST_DONE and move:
            for iid, src, _ in clips:
                try:
                    moved = ms.move_to_converted(src)
                    if moved != src:
                        self.events.put(("moved", iid, moved))
                except OSError as exc:
                    self.events.put(("log", f"[aviso]  não foi possível mover {src.name}: {exc}"))
        self.events.put(("finished", done, failed, len(layouts)))

    def _run_ffmpeg(self, cmd: list[str], iid: str, index: int, total: int,
                    trim: tuple[float, float | None] = (0.0, None),
                    length: float | None = None) -> bool:
        """Run ffmpeg, reporting progress against `length` seconds of output
        (default: the input's duration, cut down to the trim)."""
        # machine-readable progress on stdout ("out_time_us=..."), no \r stats line
        cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
            )
        except OSError as exc:
            self.events.put(("log", f"[erro]   {exc}"))
            return False

        duration = 0.0
        last_pct = -1
        tail: deque[str] = deque(maxlen=12)  # last log lines, shown on failure
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                value = line.split("=", 1)[1]
                if duration and value.isdigit():
                    pct = min(100, int(int(value) / 1e6 / duration * 100))
                    if pct != last_pct:
                        last_pct = pct
                        self.events.put(("progress", iid, pct, index, total))
            elif "=" in line and " " not in line:
                continue  # other -progress keys (frame=, fps=, ...)
            elif line:
                m = DURATION_RE.search(line)
                if m and not duration:
                    h, mnt, s = m.groups()
                    full = int(h) * 3600 + int(mnt) * 60 + float(s)
                    end = full if trim[1] is None else min(trim[1], full)
                    duration = length or max(0.1, end - trim[0])
                tail.append(line)
        code = self.proc.wait()
        self.proc = None
        if code != 0 and not self.cancel.is_set():
            self.events.put(("log", f"[FALHOU] ffmpeg saiu com código {code}\n" + "\n".join(tail)))
        return code == 0

    # ------------------------------------------------- event handling --
    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, ev: tuple) -> None:
        kind, *args = ev
        if kind == "log":
            self.log(args[0])
        elif kind == "status":
            iid, text = args
            self.tree.set(iid, "status", text)
        elif kind == "start":
            iid, name, n, total, encoder_label = args
            self.tree.set(iid, "status", ST_RUNNING)
            self.tree.see(iid)
            self.lbl_status.configure(text=f"Renderizando {n + 1}/{total} [{encoder_label}]: {name}")
            self.bar_file["value"] = 0
            self.bar_total["value"] = n / total * 100
            self.log(f"[render] {name} ({encoder_label})")
        elif kind == "progress":
            iid, pct, n, total = args
            self.tree.set(iid, "status", f"{ST_RUNNING} {pct}%")
            self.bar_file["value"] = pct
            self.bar_total["value"] = (n + pct / 100) / total * 100
        elif kind == "moved":
            iid, path = args
            old = self.files[iid]
            self.files[iid] = path
            if path.name != old.name and old.name in self.titles:  # renamed to avoid a clash
                self.titles[path.name] = self.titles[old.name]
                ms.save_titles(self.titles)
            self.log(f"[movido] {path.name} -> {path.parent}")
        elif kind == "finished":
            done, failed, total = args
            self.set_running(False)
            if self.cancel.is_set():
                msg = f"Cancelado. {done} de {total} concluído(s)."
            else:
                self.bar_file["value"] = self.bar_total["value"] = 100
                msg = f"Pronto: {done} concluído(s)" + (f", {failed} com erro." if failed else ".")
            self.lbl_status.configure(text=msg)
            self.log(msg)
        elif kind == "open":
            self.log(f"[prévia] {args[0]}")
            open_path(args[0])
        elif kind == "layouts":
            self.refresh_layouts()
        elif kind == "encoders":
            self.set_encoders(args[0])

    def on_close(self) -> None:
        if self.busy():
            if not messagebox.askyesno("Sair", "Uma renderização está em andamento. Cancelar e sair?"):
                return
            self.cancel_render()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
