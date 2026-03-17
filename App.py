"""
Gerber → 3D Model Converter
Converts Gerber PCB files (.zip or individual .gbr) to STEP and STL 3D models.

Dependencies:
    pip install -r requirements.txt
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import os
import zipfile
import tempfile
import shutil
import math
from pathlib import Path


# ── Conversion engine ─────────────────────────────────────────────────────────

def find_outline_layer(files: list) -> Path | None:
    """Heuristically identify the board outline (Edge.Cuts / GKO / GM1 etc.)"""
    priority_keywords = [
        "edge", "outline", "border", "gko", "gm1", "gm2",
        "profile", "board", "contour", "mech", "mechanical",
    ]
    priority_exts = {".gko", ".gm1", ".gm2", ".gml", ".gm"}

    for f in files:
        name = f.name.lower()
        if any(kw in name for kw in priority_keywords):
            return f
    for f in files:
        if f.suffix.lower() in priority_exts:
            return f
    return None


def find_drill_files(files: list) -> list:
    drill_exts = {".drl", ".drill", ".xln", ".ncd", ".exc", ".xnc", ".txt"}
    drill_keywords = ["drill", "drl", "excellon", "through"]
    result = []
    for f in files:
        ext = f.suffix.lower()
        name = f.name.lower()
        if ext in drill_exts and any(kw in name for kw in drill_keywords):
            result.append(f)
        elif ext in {".drl", ".drill", ".xln", ".ncd", ".exc", ".xnc"}:
            result.append(f)
    return result


def parse_gerber_outline_gerbonara(filepath: Path):
    """
    Parse a Gerber outline file using gerbonara and return a Shapely polygon.
    Traces all line/arc primitives -> closed polygon via shapely polygonize.
    """
    from gerbonara import GerberFile
    from gerbonara.graphic_objects import Line, Arc
    from shapely.geometry import LineString
    from shapely.ops import unary_union, polygonize

    gf = GerberFile.open(str(filepath))
    segments = []

    for obj in gf.objects:
        if isinstance(obj, Line):
            x1, y1 = obj.x1, obj.y1
            x2, y2 = obj.x2, obj.y2
            segments.append(LineString([(x1, y1), (x2, y2)]))
        elif isinstance(obj, Arc):
            cx, cy = obj.cx, obj.cy
            r = math.hypot(obj.x1 - cx, obj.y1 - cy)
            a1 = math.atan2(obj.y1 - cy, obj.x1 - cx)
            a2 = math.atan2(obj.y2 - cy, obj.x2 - cx)

            clockwise = getattr(obj, "clockwise", False)
            if clockwise:
                if a2 > a1:
                    a2 -= 2 * math.pi
            else:
                if a2 < a1:
                    a2 += 2 * math.pi

            n_segs = max(16, int(abs(a2 - a1) / (2 * math.pi) * 64))
            pts = []
            for i in range(n_segs + 1):
                t = a1 + (a2 - a1) * i / n_segs
                pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
            segments.append(LineString(pts))

    if not segments:
        raise ValueError(
            "No line/arc primitives found in the outline layer.\n"
            "Make sure your Edge.Cuts / outline Gerber is selected correctly."
        )

    merged = unary_union(segments)
    polys = list(polygonize(merged))

    if not polys:
        raise ValueError(
            "Could not reconstruct a closed polygon from the outline layer.\n"
            "Ensure your Edge.Cuts layer forms a complete closed loop."
        )

    return max(polys, key=lambda p: p.area)


def parse_excellon_holes_gerbonara(drill_files: list) -> list:
    """
    Parse Excellon drill files using gerbonara.
    Returns list of (x, y, diameter_mm) tuples.
    """
    from gerbonara import ExcellonFile

    holes = []
    for df in drill_files:
        try:
            ef = ExcellonFile.open(str(df))
            for obj in ef.objects:
                x = getattr(obj, "x", None)
                y = getattr(obj, "y", None)
                d = getattr(obj, "diameter", None)
                if x is not None and y is not None and d and d > 0:
                    holes.append((x, y, d))
        except Exception:
            pass
    return holes


def build_3d_model(
    outline_poly,
    holes: list,
    thickness_mm: float,
    output_stem: Path,
    export_step: bool,
    export_stl: bool,
    progress_cb=None,
):
    import cadquery as cq

    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    _p("Building board geometry...")

    cx = outline_poly.centroid.x
    cy = outline_poly.centroid.y
    coords = [(x - cx, y - cy) for x, y in outline_poly.exterior.coords]

    _p("Extruding PCB thickness...")
    board = (
        cq.Workplane("XY")
        .polyline(coords)
        .close()
        .extrude(thickness_mm)
    )

    if holes:
        _p(f"Cutting {len(holes)} drill holes...")
        for (hx, hy, hd) in holes:
            try:
                board = (
                    board
                    .faces(">Z")
                    .workplane()
                    .moveTo(hx - cx, hy - cy)
                    .circle(hd / 2.0)
                    .cutThruAll()
                )
            except Exception:
                pass

    if export_step:
        step_path = str(output_stem) + ".step"
        _p("Exporting STEP...")
        cq.exporters.export(board, step_path)
        _p(f"STEP saved: {Path(step_path).name}")

    if export_stl:
        stl_path = str(output_stem) + ".stl"
        _p("Exporting STL...")
        cq.exporters.export(board, stl_path, exportType="STL")
        _p(f"STL saved:  {Path(stl_path).name}")


def convert(
    input_path: str,
    output_dir: str,
    thickness_mm: float,
    export_step: bool,
    export_stl: bool,
    progress_cb=None,
    done_cb=None,
    error_cb=None,
):
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    tmpdir = None
    try:
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if input_path.suffix.lower() == ".zip":
            _p("Extracting ZIP...")
            tmpdir = tempfile.mkdtemp()
            with zipfile.ZipFile(input_path, "r") as z:
                z.extractall(tmpdir)
            gerber_dir = Path(tmpdir)
        else:
            gerber_dir = input_path if input_path.is_dir() else input_path.parent

        all_files = [f for f in gerber_dir.rglob("*") if f.is_file()]

        _p("Detecting outline layer...")
        outline_file = find_outline_layer(all_files)
        if outline_file is None:
            raise FileNotFoundError(
                "Could not auto-detect the board outline layer.\n"
                "Rename your edge/outline Gerber to include one of:\n"
                "  edge, outline, border, gko, gm1, gm2, profile, contour"
            )
        _p(f"Outline layer: {outline_file.name}")

        _p("Parsing Gerber outline...")
        outline_poly = parse_gerber_outline_gerbonara(outline_file)
        b = outline_poly.bounds
        w, h = b[2] - b[0], b[3] - b[1]
        _p(f"Board size: {w:.2f} x {h:.2f} mm")

        drill_files = find_drill_files(all_files)
        if drill_files:
            _p(f"Parsing {len(drill_files)} drill file(s)...")
            holes = parse_excellon_holes_gerbonara(drill_files)
            _p(f"{len(holes)} holes found")
        else:
            _p("No drill files detected - skipping holes")
            holes = []

        stem = output_dir / input_path.stem
        build_3d_model(
            outline_poly, holes, thickness_mm,
            stem, export_step, export_stl,
            progress_cb=_p,
        )

        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if done_cb:
            done_cb(str(output_dir))

    except Exception as exc:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if error_cb:
            error_cb(str(exc))


# ── GUI ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gerber -> 3D Converter")
        self.resizable(False, False)
        self.configure(bg="#0f1117")
        self._build_ui()
        self._center()

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _build_ui(self):
        BG      = "#0f1117"
        SURFACE = "#1a1d27"
        ACCENT  = "#00e5ff"
        ACCENT2 = "#7c3aed"
        FG      = "#e2e8f0"
        FG_DIM  = "#64748b"

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame",       background=BG)
        style.configure("Card.TFrame",  background=SURFACE)
        style.configure("TLabel",       background=BG,      foreground=FG,     font=("Courier New", 10))
        style.configure("Head.TLabel",  background=BG,      foreground=ACCENT, font=("Courier New", 18, "bold"))
        style.configure("Sub.TLabel",   background=BG,      foreground=FG_DIM, font=("Courier New", 9))
        style.configure("Card.TLabel",  background=SURFACE, foreground=FG,     font=("Courier New", 10))
        style.configure("Dim.TLabel",   background=SURFACE, foreground=FG_DIM, font=("Courier New", 9))
        style.configure("TCheckbutton", background=SURFACE, foreground=FG,     font=("Courier New", 10))
        style.configure("TSpinbox",     fieldbackground="#252836", foreground=FG,
                        background=SURFACE, arrowcolor=ACCENT)
        style.map("TCheckbutton", background=[("active", SURFACE)])
        style.configure("TProgressbar", troughcolor=SURFACE, background=ACCENT,
                        bordercolor=SURFACE, lightcolor=ACCENT, darkcolor=ACCENT)

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=24, pady=24)

        # Header
        ttk.Label(outer, text="[ PCB -> 3D ]", style="Head.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Gerber ZIP  ->  STEP / STL", style="Sub.TLabel").pack(anchor="w")
        tk.Frame(outer, height=1, bg=ACCENT2).pack(fill="x", pady=(10, 18))

        # Input file
        card1 = ttk.Frame(outer, style="Card.TFrame", padding=14)
        card1.pack(fill="x", pady=(0, 12))
        ttk.Label(card1, text="INPUT FILE", style="Dim.TLabel").pack(anchor="w")
        row1 = ttk.Frame(card1, style="Card.TFrame")
        row1.pack(fill="x", pady=(4, 0))
        self.input_var = tk.StringVar(value="No file selected...")
        ttk.Label(row1, textvariable=self.input_var, style="Card.TLabel",
                  wraplength=340, justify="left").pack(side="left", expand=True, fill="x")
        tk.Button(row1, text="Browse", font=("Courier New", 9, "bold"),
                  bg=ACCENT2, fg="#fff", relief="flat", padx=10, pady=4,
                  activebackground="#6d28d9", cursor="hand2",
                  command=self._browse_input).pack(side="right", padx=(8, 0))

        # Output dir
        card2 = ttk.Frame(outer, style="Card.TFrame", padding=14)
        card2.pack(fill="x", pady=(0, 12))
        ttk.Label(card2, text="OUTPUT DIRECTORY", style="Dim.TLabel").pack(anchor="w")
        row2 = ttk.Frame(card2, style="Card.TFrame")
        row2.pack(fill="x", pady=(4, 0))
        self.output_var = tk.StringVar(value=str(Path.home() / "Desktop"))
        ttk.Label(row2, textvariable=self.output_var, style="Card.TLabel",
                  wraplength=340, justify="left").pack(side="left", expand=True, fill="x")
        tk.Button(row2, text="Browse", font=("Courier New", 9, "bold"),
                  bg=ACCENT2, fg="#fff", relief="flat", padx=10, pady=4,
                  activebackground="#6d28d9", cursor="hand2",
                  command=self._browse_output).pack(side="right", padx=(8, 0))

        # Settings
        card3 = ttk.Frame(outer, style="Card.TFrame", padding=14)
        card3.pack(fill="x", pady=(0, 12))
        ttk.Label(card3, text="SETTINGS", style="Dim.TLabel").pack(anchor="w", pady=(0, 6))
        srow = ttk.Frame(card3, style="Card.TFrame")
        srow.pack(fill="x")
        ttk.Label(srow, text="PCB thickness (mm):", style="Card.TLabel").pack(side="left")
        self.thickness_var = tk.DoubleVar(value=1.6)
        ttk.Spinbox(srow, from_=0.4, to=6.0, increment=0.1,
                    textvariable=self.thickness_var, width=6,
                    font=("Courier New", 10)).pack(side="left", padx=(8, 24))
        self.export_step = tk.BooleanVar(value=True)
        self.export_stl  = tk.BooleanVar(value=True)
        ttk.Checkbutton(srow, text="STEP", variable=self.export_step).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(srow, text="STL",  variable=self.export_stl).pack(side="left")

        # Log
        card4 = ttk.Frame(outer, style="Card.TFrame", padding=14)
        card4.pack(fill="x", pady=(0, 14))
        ttk.Label(card4, text="LOG", style="Dim.TLabel").pack(anchor="w", pady=(0, 4))
        log_frame = ttk.Frame(card4, style="Card.TFrame")
        log_frame.pack(fill="x")
        self.log_text = tk.Text(
            log_frame, height=9, bg="#0d0f18", fg=ACCENT,
            font=("Courier New", 9), relief="flat",
            insertbackground=ACCENT, state="disabled", wrap="word",
        )
        self.log_text.pack(side="left", fill="x", expand=True)
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview, bg=SURFACE)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)

        # Progress
        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=480)
        self.progress.pack(fill="x", pady=(0, 12))

        # Convert button
        self.btn_convert = tk.Button(
            outer, text="  CONVERT TO 3D",
            font=("Courier New", 12, "bold"),
            bg=ACCENT, fg="#000", relief="flat", padx=18, pady=10,
            cursor="hand2", activebackground="#00b8cc",
            command=self._start_conversion,
        )
        self.btn_convert.pack(fill="x")

        ttk.Label(outer,
                  text="Requires: gerbonara  shapely  cadquery  (see requirements.txt)",
                  style="Sub.TLabel").pack(pady=(8, 0))

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select Gerber ZIP",
            filetypes=[
                ("Gerber ZIP", "*.zip"),
                ("Gerber file", "*.gbr *.ger *.gko *.gtl *.gbl *.gm1 *.gm2 *.drl *.xln"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.input_var.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            self.output_var.set(path)

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"> {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _start_conversion(self):
        inp = self.input_var.get()
        if not inp or inp == "No file selected...":
            messagebox.showwarning("No input", "Please select a Gerber ZIP file first.")
            return
        if not Path(inp).exists():
            messagebox.showerror("Not found", f"File not found:\n{inp}")
            return
        if not self.export_step.get() and not self.export_stl.get():
            messagebox.showwarning("No output", "Select at least one format (STEP / STL).")
            return

        self.btn_convert.configure(state="disabled", text="Converting...")
        self.progress.start(12)
        self._log(f"Input: {Path(inp).name}")

        threading.Thread(
            target=convert,
            kwargs=dict(
                input_path=inp,
                output_dir=self.output_var.get(),
                thickness_mm=self.thickness_var.get(),
                export_step=self.export_step.get(),
                export_stl=self.export_stl.get(),
                progress_cb=lambda m: self.after(0, self._log, m),
                done_cb=lambda d: self.after(0, self._on_done, d),
                error_cb=lambda e: self.after(0, self._on_error, e),
            ),
            daemon=True,
        ).start()

    def _on_done(self, output_dir: str):
        self.progress.stop()
        self.btn_convert.configure(state="normal", text="  CONVERT TO 3D")
        self._log("Done!")
        messagebox.showinfo("Success", f"3D model(s) saved to:\n{output_dir}")

    def _on_error(self, err: str):
        self.progress.stop()
        self.btn_convert.configure(state="normal", text="  CONVERT TO 3D")
        self._log(f"ERROR: {err}")
        messagebox.showerror("Conversion failed", err)


if __name__ == "__main__":
    app = App()
    app.mainloop()