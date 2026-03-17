# Gerber → 3D Converter

Convert Gerber PCB files into STEP and STL 3D models via a simple GUI.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** `cadquery` is the heaviest dependency. If you hit issues installing it,
> use their conda channel instead:
> ```bash
> conda install -c cadquery cadquery
> pip install gerber-rs274x shapely
> ```

### 2. Run the app

```bash
python app.py
```

---

## Usage

1. Click **Browse** next to *Input File* and select your **Gerber ZIP** (exported from KiCad, Altium, Eagle, etc.)
2. Choose an **output directory**
3. Set the **PCB thickness** (standard FR4 is 1.6 mm)
4. Tick **STEP** and/or **STL**
5. Click **⚡ CONVERT TO 3D**

Output files are named after your input file (e.g. `myboard.step`, `myboard.stl`).

---

## What it does

| Step | Detail |
|------|--------|
| Unzip | Extracts your Gerber ZIP to a temp folder |
| Outline detection | Looks for the Edge.Cuts / `*.gko` / `*.gm1` layer by filename heuristics |
| Polygon parsing | Traces lines/arcs in the outline Gerber into a 2D Shapely polygon |
| Drill parsing | Reads Excellon `.drl` files to get hole positions and diameters |
| 3D extrusion | CadQuery extrudes the outline to the chosen thickness and punches drill holes |
| Export | Writes `.step` (CAD-friendly) and/or `.stl` (3D-print-friendly) |

---

## Outline layer naming

The app auto-detects the outline layer by looking for these keywords in the filename:

`edge`, `outline`, `border`, `gko`, `gm1`, `gm2`, `profile`, `board`, `contour`, `mech`

If detection fails, rename your outline Gerber to include one of these words, e.g. `myboard_edge.gbr`.

---

## Limitations

- Only the **board outline + drill holes** are modelled (no copper traces, silkscreen, or components)
- Very complex outlines with many arcs may take 30–60 s
- Requires the outline to be a **closed loop** in the Gerber file

---

## License

MIT