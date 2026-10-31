#!/usr/bin/env python3
"""
gui.py - small control panel for make_shorts.py.

Lists the videos in ./input (or any you add), lets you pick a layout file,
renders them all with per-video progress and the ffmpeg log, can cancel,
renders a single test frame, and opens the layout editor.

Usage:
    python gui.py        # pythonw gui.py hides the console window on Windows
"""

from __future__ import annotations

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
NO_WINDOW = ms.NO_WINDOW
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

ST_PENDING, ST_RUNNING, ST_DONE, ST_FAILED, ST_CANCELLED = (
    "pendente", "renderizando", "concluído", "erro", "cancelado",
)


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

        ms.ensure_dirs()
        root.title("ShortVideoMaker")
        root.geometry("860x640")
        root.minsize(680, 500)
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
        ttk.Label(top, text="Layout:").pack(side="left")
        self.var_layout = tk.StringVar()
        self.combo_layout = ttk.Combobox(top, textvariable=self.var_layout,
                                         state="readonly", width=32)
        self.combo_layout.pack(side="left", padx=4)
        ttk.Button(top, text="Editar layout…", command=self.edit_layout).pack(side="left", padx=2)
        ttk.Button(top, text="↻", width=3, command=self.refresh_layouts).pack(side="left", padx=2)
        self.var_encoder = tk.StringVar(value="detectando GPU…")
        self.combo_encoder = ttk.Combobox(top, textvariable=self.var_encoder,
                                          state="disabled", width=26)
        self.combo_encoder.pack(side="right")
        ttk.Label(top, text="Aceleração:").pack(side="right", padx=(12, 4))

        # ---- video list ----
        mid = ttk.LabelFrame(self.root, text="Vídeos", padding=6)
        mid.pack(fill="both", expand=True, padx=8)
        btns = ttk.Frame(mid)
        btns.pack(side="right", fill="y", padx=(6, 0))
        for text, cmd in (
            ("Adicionar…", self.add_files),
            ("Remover", self.remove_selected),
            ("Recarregar input/", self.load_input_dir),
            ("Título / crédito…", self.edit_texts),
            ("Prévia (1 frame)", self.preview_frame),
        ):
            ttk.Button(btns, text=text, command=cmd).pack(fill="x", pady=2)
        ttk.Label(btns, text="Sem seleção =\nrenderiza todos\nos pendentes.\n\nDuplo clique =\neditar título\ne crédito.",
                  foreground="#888", justify="left").pack(fill="x", pady=(10, 0))

        self.tree = ttk.Treeview(mid, columns=("title", "credit", "status"), selectmode="extended")
        self.tree.heading("#0", text="Arquivo", anchor="w")
        self.tree.heading("title", text="Título", anchor="w")
        self.tree.heading("credit", text="Crédito", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=250)
        self.tree.column("title", width=220)
        self.tree.column("credit", width=120)
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
    def refresh_layouts(self) -> None:
        names = sorted(p.name for p in HERE.glob("*.json"))
        values = names + [BUILTIN]
        self.combo_layout["values"] = values
        if self.var_layout.get() not in values:
            self.var_layout.set(ms.DEFAULT_LAYOUT_NAME if ms.DEFAULT_LAYOUT_NAME in names else BUILTIN)

    def load_selected_layout(self) -> tuple[dict | None, str]:
        """The chosen layout (None = built-in CONFIG) + a description of it."""
        name = self.var_layout.get()
        if name == BUILTIN:
            return None, "CONFIG embutido"
        layout = ms.load_layout(HERE / name)
        if layout["overlays_enabled"] and layout["overlays"]:
            name += f" (+{len(layout['overlays'])} texto(s)/imagem(ns))"
        return layout, name

    def texts_for(self, path: Path) -> dict:
        return dict(self.titles.get(path.name, {}))

    def edit_layout(self) -> None:
        name = self.var_layout.get()
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
            texts = self.texts_for(p)
            iid = self.tree.insert("", "end", text=p.name, values=(
                texts.get("title", ""), texts.get("credit", ""), ST_PENDING))
            self.files[iid] = p

    def edit_texts(self, _event=None) -> None:
        """Dialog for the {title}/{credit} of the selected video(s)."""
        iids = self.tree.selection()
        if not iids:
            messagebox.showinfo("Título / crédito", "Selecione um ou mais vídeos na lista.")
            return
        entries = [self.texts_for(self.files[iid]) for iid in iids]

        win = tk.Toplevel(self.root)
        win.title("Título / crédito")
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
                                            ("credit", "Crédito  {credit}")), start=1):
            values = {e.get(key, "") for e in entries}
            mixed[key] = len(values) > 1  # a blank mixed field keeps each video's own value
            fields[key] = tk.StringVar(value="" if mixed[key] else values.pop())
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            entry = ttk.Entry(frm, textvariable=fields[key], width=50)
            entry.grid(row=row, column=1, pady=3)
            if row == 1:
                entry.focus_set()
        if len(iids) > 1:
            ttk.Label(frm, text="Campos em branco com valores diferentes ficam como estão.",
                      foreground="#888").grid(row=3, column=0, columnspan=2, sticky="w")

        def save(_event=None) -> None:
            for iid in iids:
                name = self.files[iid].name
                entry = self.titles.get(name, {})
                for key, var in fields.items():
                    value = var.get().strip()
                    if value or not mixed[key]:
                        entry[key] = value
                entry = {k: v for k, v in entry.items() if v}
                if entry:
                    self.titles[name] = entry
                else:
                    self.titles.pop(name, None)
                self.tree.set(iid, "title", entry.get("title", ""))
                self.tree.set(iid, "credit", entry.get("credit", ""))
            try:
                ms.save_titles(self.titles)
            except OSError as exc:
                messagebox.showerror("Salvar", str(exc), parent=win)
                return
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
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
        try:
            layout, _ = self.load_selected_layout()
            fc, extra = ms.filter_for_video(layout, self.texts_for(src), tmp / "overlay")
        except Exception as exc:
            messagebox.showerror("Layout", str(exc))
            return
        out = self.output_dir()
        out.mkdir(parents=True, exist_ok=True)
        dst = out / f"{src.stem}_preview_{at:g}s.png"
        cmd = ms.build_preview_cmd(self.ffmpeg, src, dst, fc, at, extra)
        self.log(f"[prévia] {src.name} @ {at:g}s")

        def run() -> None:
            dst.unlink(missing_ok=True)
            result = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                    stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
            shutil.rmtree(tmp, ignore_errors=True)
            if result.returncode == 0 and dst.exists():
                self.events.put(("open", dst))
            else:
                tail = "\n".join(result.stderr.strip().splitlines()[-8:])
                self.events.put(("log", f"[prévia falhou] {src.name}\n{tail}"))

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
            layout, source = self.load_selected_layout()
        except Exception as exc:
            messagebox.showerror("Layout", str(exc))
            return

        out_dir = self.output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "published").mkdir(exist_ok=True)

        jobs = [(iid, self.files[iid], self.texts_for(self.files[iid])) for iid in items]
        encoder = self.selected_encoder()
        self.log(f"layout: {source} | {ms.ENCODERS[encoder]} | "
                 f"{len(jobs)} vídeo(s) -> {out_dir.resolve()}")
        self.cancel.clear()
        self.btn_render.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.combo_encoder.configure(state="disabled")
        self.bar_file["value"] = self.bar_total["value"] = 0
        self.worker = threading.Thread(
            target=self._render_all, args=(jobs, layout, out_dir, self.var_move.get(), encoder),
            daemon=True,
        )
        self.worker.start()

    def cancel_render(self) -> None:
        self.cancel.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
        self.lbl_status.configure(text="Cancelando…")

    def _render_all(self, jobs: list[tuple[str, Path, dict]], layout: dict | None, out_dir: Path,
                    move: bool, encoder: str) -> None:
        """Worker thread: renders jobs one by one, reports through self.events."""
        done = failed = 0
        tmp = Path(tempfile.mkdtemp(prefix="shorts_overlay_"))
        for n, (iid, src, texts) in enumerate(jobs):
            if self.cancel.is_set():
                break
            dst = out_dir / f"{src.stem}_short.mp4"
            self.events.put(("start", iid, src.name, n, len(jobs), ms.ENCODERS[encoder]))
            try:
                fc, extra = ms.filter_for_video(layout, texts, tmp / str(n))
            except Exception as exc:
                self.events.put(("log", f"[erro]   textos/imagens de {src.name}: {exc}"))
                failed += 1
                self.events.put(("status", iid, ST_FAILED))
                continue
            cmd = ms.build_render_cmd(self.ffmpeg, src, dst, fc, encoder, extra)
            ok = self._run_ffmpeg(cmd, iid, n, len(jobs))
            if not ok and encoder != "cpu" and not self.cancel.is_set():
                self.events.put(("log", f"[fallback] {ms.ENCODERS[encoder]} falhou, "
                                        f"tentando de novo com {ms.ENCODERS['cpu']}"))
                self.events.put(("start", iid, src.name, n, len(jobs), ms.ENCODERS["cpu"]))
                cmd = ms.build_render_cmd(self.ffmpeg, src, dst, fc, "cpu", extra)
                ok = self._run_ffmpeg(cmd, iid, n, len(jobs))
            if ok:
                done += 1
                self.events.put(("status", iid, ST_DONE))
                self.events.put(("log", f"[ok]     {src.name} -> {dst}"))
                if move:
                    try:
                        moved = ms.move_to_converted(src)
                        if moved != src:
                            self.events.put(("moved", iid, moved))
                    except OSError as exc:
                        self.events.put(("log", f"[aviso]  não foi possível mover {src.name}: {exc}"))
            elif self.cancel.is_set():
                dst.unlink(missing_ok=True)  # don't leave a half-written file behind
                self.events.put(("status", iid, ST_CANCELLED))
            else:
                failed += 1
                self.events.put(("status", iid, ST_FAILED))
        shutil.rmtree(tmp, ignore_errors=True)
        self.events.put(("finished", done, failed, len(jobs)))

    def _run_ffmpeg(self, cmd: list[str], iid: str, index: int, total: int) -> bool:
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
                    duration = int(h) * 3600 + int(mnt) * 60 + float(s)
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
            self.btn_render.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            if self.encoder_keys:
                self.combo_encoder.configure(state="readonly")
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
