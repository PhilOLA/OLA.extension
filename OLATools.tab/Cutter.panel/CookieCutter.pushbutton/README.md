# Cookie Cutter Family Builder — Setup & Usage

## Files

| File | Purpose |
|---|---|
| `script.py` | pyRevit pushbutton script (IronPython 2.7) |
| `image_processor.py` | CPython image processing helper (called via subprocess) |

Both files **must live in the same folder** inside your pyRevit extension.

---

## 1. Install CPython Dependencies

Install CPython 3.11 from https://www.python.org/downloads/  
Then open a command prompt and run:

```
pip install opencv-python numpy scipy
```

---

## 2. pyRevit Button Setup

Place both files in your pyRevit extension panel folder, e.g.:

```
MyExtension.extension/
  MyPanel.panel/
    CookieCutter.pushbutton/
      script.py
      image_processor.py
      icon.png          ← add your own 32×32 icon
```

Reload pyRevit — the button will appear in your panel.

---

## 3. Usage

1. Click the **Cookie Cutter Family Builder** button in Revit
2. Fill in the form:
   - **Source Image** — PNG or JPG of the shape to trace (line art on white background works best)
   - **Family Template** — defaults to Revit 2024 Generic Model; browse if different
   - **Save As** — where to save the new `.rfa`
   - **Cutter Height** — extrusion height in mm (default 10mm)
   - **Wall Thickness** — shell wall thickness in mm (default 1.5mm)
   - **Smoothing** — 1 = preserve all detail, 10 = very smooth curves
   - **Min Detail Size** — features smaller than this many pixels are ignored
   - **Fit Tolerance** — how closely arcs/lines must match the outline (mm)
   - **CPython Path** — auto-detected; browse if not found
3. Click **Build Family** — processing takes 5–20 seconds
4. The `.rfa` opens ready to load into your project

---

## 4. Tips for Best Results

- **Line art on white background** traces most cleanly (like the cupcake image)
- Start with **Smoothing = 5** and adjust up for simpler shapes, down for complex ones
- **Min Detail = 5px** ignores tiny features like the cherry stem cross-hatch
- If the extrusion fails in Revit, increase Fit Tolerance slightly (e.g. 0.8mm)
- The family is saved as a new `.rfa` — your template is never modified

---

## 5. Troubleshooting

| Issue | Fix |
|---|---|
| `opencv-python not installed` | Run `pip install opencv-python numpy scipy` in CPython |
| `Subprocess failed` | Check CPython path in the form — browse to `python.exe` |
| `No segments generated` | Lower Min Detail Size or Smoothing |
| Extrusion fails | Increase Fit Tolerance; some very complex silhouettes may need manual cleanup |
| Template not found | Browse to your `.rft` file — check Revit year/language folder |
