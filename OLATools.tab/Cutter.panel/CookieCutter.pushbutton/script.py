# -*- coding: utf-8 -*-
"""
Cookie Cutter Family Builder
pyRevit pushbutton — IronPython 2.7

Traces an SVG outline and creates a Metric Generic Model .rfa cookie cutter.
If extrusion fails, automatically saves a model-line fallback .rfa.

Settings persisted to settings.json in the pushbutton folder.
"""

import os
import sys
import json
import subprocess
import tempfile
import copy
import math
import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from Autodesk.Revit.DB import (
    Transaction, XYZ,
    Line, Arc, Curve, CurveLoop, CurveArray, CurveArrArray,
    Plane, SketchPlane, ElementId,
    SaveAsOptions, UnitTypeId, UnitUtils,
    FilteredElementCollector
)
from Autodesk.Revit.UI import UIApplication

import System
from System.Windows.Forms import (
    Form, Label, TextBox, Button, TrackBar, DialogResult,
    OpenFileDialog, MessageBox, MessageBoxButtons, MessageBoxIcon,
    FormStartPosition, Panel, ToolTip, NumericUpDown,
    FormBorderStyle, AnchorStyles, CheckBox
)
from System.Drawing import (
    Size, Point, Color, Font, FontStyle, FontFamily
)

# ---------------------------------------------------------------------------
# UNIT HELPERS
# ---------------------------------------------------------------------------

def mm_to_ft(mm):
    return UnitUtils.ConvertToInternalUnits(mm, UnitTypeId.Millimeters)

def ft_to_mm(ft):
    return UnitUtils.ConvertFromInternalUnits(ft, UnitTypeId.Millimeters)

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
PROCESSOR     = os.path.join(SCRIPT_DIR, "image_processor.py")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")

DEFAULT_TEMPLATE = (
    r"C:\ProgramData\Autodesk\RVT 2024\Family Templates\English\Metric Generic Model.rft"
)

# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

DEFAULTS = {
    "image_path":    "",
    "template_path": DEFAULT_TEMPLATE,
    "save_path":     os.path.join(os.path.expanduser("~"), "Desktop", "CookieCutter.rfa"),
    "python_path":   "",
    "height_mm":     10.0,
    "wall_mm":       1.5,
    "smoothing":     1,
    "min_detail":    1,
    "tolerance":     0.5,
    "fallback_lines": True,
}

def load_settings():
    s = dict(DEFAULTS)
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                s.update(json.load(f))
        except Exception:
            pass
    return s

def save_settings(d):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# STYLING CONSTANTS  (matching other OLA tools)
# ---------------------------------------------------------------------------

COL_BG        = Color.FromArgb(30, 30, 30)       # dark background
COL_PANEL     = Color.FromArgb(45, 45, 45)       # section panels
COL_TEXT      = Color.White
COL_LABEL     = Color.FromArgb(180, 180, 180)
COL_ACCENT    = Color.FromArgb(0, 150, 136)      # teal accent
COL_BTN_BUILD = Color.FromArgb(0, 150, 136)
COL_BTN_CLOSE = Color.FromArgb(80, 80, 80)
COL_INPUT_BG  = Color.FromArgb(60, 60, 60)
COL_INPUT_FG  = Color.White
FONT_UI       = Font("Segoe UI", 9)
FONT_BOLD     = Font("Segoe UI", 9, FontStyle.Bold)
FONT_SECTION  = Font("Segoe UI", 8, FontStyle.Bold)

def styled_label(text, location, size, bold=False, color=None):
    lbl = Label()
    lbl.Text      = text
    lbl.Location  = location
    lbl.Size      = size
    lbl.Font      = FONT_BOLD if bold else FONT_UI
    lbl.ForeColor = color if color else (COL_ACCENT if bold else COL_LABEL)
    lbl.BackColor = Color.Transparent
    return lbl

def styled_textbox(text, location, size):
    tb = TextBox()
    tb.Text      = text
    tb.Location  = location
    tb.Size      = size
    tb.BackColor = COL_INPUT_BG
    tb.ForeColor = COL_INPUT_FG
    tb.Font      = FONT_UI
    tb.BorderStyle = System.Windows.Forms.BorderStyle.FixedSingle
    return tb

def styled_button(text, location, size, color=None):
    btn = Button()
    btn.Text      = text
    btn.Location  = location
    btn.Size      = size
    btn.BackColor = color if color else COL_BTN_CLOSE
    btn.ForeColor = COL_TEXT
    btn.Font      = FONT_BOLD
    btn.FlatStyle = System.Windows.Forms.FlatStyle.Flat
    btn.FlatAppearance.BorderSize = 0
    return btn

def browse_button(location, handler):
    btn = styled_button("...", location, Size(36, 24))
    btn.Click += handler
    return btn

def styled_numeric(value, location, size, minv, maxv, dec=1):
    nm = NumericUpDown()
    nm.Minimum       = System.Decimal(minv)
    nm.Maximum       = System.Decimal(maxv)
    nm.Value         = System.Decimal(max(minv, min(maxv, value)))
    nm.DecimalPlaces = dec
    nm.Increment     = System.Decimal(0.5)
    nm.Location      = location
    nm.Size          = size
    nm.BackColor     = COL_INPUT_BG
    nm.ForeColor     = COL_INPUT_FG
    nm.Font          = FONT_UI
    return nm

def styled_slider(minv, maxv, value, location, size):
    tb = TrackBar()
    tb.Minimum       = minv
    tb.Maximum       = maxv
    tb.Value         = max(minv, min(maxv, int(value)))
    tb.Location      = location
    tb.Size          = size
    tb.TickFrequency = 1
    tb.BackColor     = COL_PANEL
    return tb

def styled_checkbox(text, location, size, checked=False):
    cb = CheckBox()
    cb.Text      = text
    cb.Location  = location
    cb.Size      = size
    cb.Checked   = checked
    cb.ForeColor = COL_LABEL
    cb.BackColor = Color.Transparent
    cb.Font      = FONT_UI
    return cb

def section_panel(location, size, title=""):
    pnl = Panel()
    pnl.Location  = location
    pnl.Size      = size
    pnl.BackColor = COL_PANEL
    pass  # BorderStyle
    return pnl

class NotifyDialog(Form):
    """Custom dark-themed notification dialog matching the main form style."""
    def __init__(self, title, message, kind="info"):
        self.Text            = title
        self.Size            = Size(440, 200)
        self.StartPosition   = FormStartPosition.CenterScreen
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.MaximizeBox     = False
        self.MinimizeBox     = False
        self.BackColor       = COL_BG

        # Accent bar colour by kind
        bar_color = {
            "info":    COL_ACCENT,
            "error":   Color.FromArgb(200, 60, 60),
            "warning": Color.FromArgb(200, 140, 0),
        }.get(kind, COL_ACCENT)

        # Left accent bar
        bar = Panel()
        bar.Location  = Point(0, 0)
        bar.Size      = Size(5, 200)
        bar.BackColor = bar_color
        self.Controls.Add(bar)

        # Message label — auto-size height
        lbl = Label()
        lbl.Text      = message
        lbl.Location  = Point(20, 20)
        lbl.Size      = Size(390, 110)
        lbl.Font      = FONT_UI
        lbl.ForeColor = COL_TEXT
        lbl.BackColor = Color.Transparent
        lbl.AutoSize  = False
        self.Controls.Add(lbl)

        # Resize form to fit message
        lines = message.count("\n") + 1
        msg_h = max(80, lines * 22)
        form_h = msg_h + 120  # padding + button row
        form_h = max(200, form_h)

        self.Size       = Size(460, form_h)
        bar.Size        = Size(5, form_h)
        lbl.Location    = Point(20, 20)
        lbl.Size        = Size(415, msg_h)

        # OK button — fixed distance from bottom
        btn = styled_button("OK", Point(350, form_h - 62),
                            Size(80, 32), bar_color)
        btn.Click += lambda s, e: self.Close()
        self.Controls.Add(btn)
        self.AcceptButton = btn


def _show_dialog(title, message, kind="info"):
    dlg = NotifyDialog(title, message, kind)
    dlg.ShowDialog()


def show_info(title, message, parent=None):
    _show_dialog(title, message, "info")

def show_error(title, message, parent=None):
    _show_dialog(title, message, "error")

def show_warning(title, message, parent=None):
    _show_dialog(title, message, "warning")

# ---------------------------------------------------------------------------
# MAIN FORM
# ---------------------------------------------------------------------------

class CookieCutterForm(Form):
    def __init__(self):
        self._s = load_settings()
        if not self._s.get("python_path") or not os.path.isfile(self._s["python_path"]):
            self._s["python_path"] = self._find_python()

        self.Text            = "Cookie Cutter Family Builder"
        self.Size            = Size(520, 760)
        self.StartPosition   = FormStartPosition.CenterScreen
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.MaximizeBox     = False
        self.BackColor       = COL_BG

        self._build_ui()

    def _build_ui(self):
        y = 12

        def row_label(text, top):
            lbl = styled_label(text, Point(16, top), Size(480, 18), bold=True)
            self.Controls.Add(lbl)

        def row_textbox(value, top, width=420):
            tb = styled_textbox(value, Point(16, top), Size(width, 24))
            self.Controls.Add(tb)
            return tb

        def row_browse(top, left, handler):
            btn = browse_button(Point(left, top), handler)
            self.Controls.Add(btn)
            return btn

        def row_numeric(value, top, minv, maxv, dec=1, width=110):
            nm = styled_numeric(value, Point(16, top), Size(width, 24), minv, maxv, dec)
            self.Controls.Add(nm)
            return nm

        def row_slider(minv, maxv, value, top, width=340):
            sl = styled_slider(minv, maxv, value, Point(16, top), Size(width, 36))
            self.Controls.Add(sl)
            return sl

        def val_label(top, left=368):
            lbl = styled_label("", Point(left, top + 8), Size(100, 20))
            self.Controls.Add(lbl)
            return lbl

        def divider(top):
            pnl = Panel()
            pnl.Location  = Point(16, top)
            pnl.Size      = Size(472, 1)
            pnl.BackColor = Color.FromArgb(70, 70, 70)
            self.Controls.Add(pnl)

        # ── Source Image ────────────────────────────────────────────────────
        row_label("Source Image (SVG Recommended):", y); y += 20
        self.tb_image = row_textbox(self._s["image_path"], y)
        row_browse(y, 440, self._pick_image); y += 32

        self.lbl_seg_count = styled_label(
            "Select an SVG to see segment count",
            Point(16, y), Size(460, 18), bold=False,
            color=Color.FromArgb(120, 120, 120)
        )
        self.Controls.Add(self.lbl_seg_count); y += 30

        divider(y); y += 10

        # ── Template ────────────────────────────────────────────────────────
        row_label("Family Template (.rft):", y); y += 20
        self.tb_template = row_textbox(self._s["template_path"], y)
        row_browse(y, 440, self._pick_template); y += 32

        divider(y); y += 10

        # ── Save Path ───────────────────────────────────────────────────────
        row_label("Save Family As (.rfa):", y); y += 20
        self.tb_save = row_textbox(self._s["save_path"], y)
        row_browse(y, 440, self._pick_save); y += 32

        self.chk_fallback = styled_checkbox(
            "Also create Model Line .rfa",
            Point(16, y), Size(460, 22),
            checked=self._s.get("fallback_lines", True)
        )
        self.Controls.Add(self.chk_fallback); y += 28

        divider(y); y += 10

        # ── Dimensions ──────────────────────────────────────────────────────
        row_label("Cutter Height (mm):", y); y += 20
        self.nm_height = row_numeric(self._s["height_mm"], y, 3.0, 100.0, dec=1); y += 32

        row_label("Wall Thickness (mm):", y); y += 20
        self.nm_wall = row_numeric(self._s["wall_mm"], y, 0.5, 10.0, dec=1); y += 32

        divider(y); y += 10

        # ── Smoothing ───────────────────────────────────────────────────────
        row_label("Outline Smoothing  (1 = minimal  →  10 = very smooth):", y); y += 20
        self.sl_smooth  = row_slider(1, 10, self._s["smoothing"], y)
        self.lbl_smooth = val_label(y)
        self.sl_smooth.ValueChanged += lambda s, e: self._update_labels()
        y += 44

        # ── Min Segment ─────────────────────────────────────────────────────
        row_label("Minimum Segment Length (mm — smaller = more detail):", y); y += 20
        self.sl_detail  = row_slider(1, 20, self._s["min_detail"], y)
        self.lbl_detail = val_label(y)
        self.sl_detail.ValueChanged += lambda s, e: self._update_labels()
        y += 44

        divider(y); y += 10

        # ── CPython ─────────────────────────────────────────────────────────
        row_label("CPython 3.x Executable:", y); y += 20
        self.tb_python = row_textbox(self._s["python_path"], y)
        row_browse(y, 440, self._pick_python); y += 32

        divider(y); y += 10

        # ── Options ─────────────────────────────────────────────────────────
        self.chk_debug = styled_checkbox(
            "Debug mode — draw model lines only (no extrusion)",
            Point(16, y), Size(460, 22),
            checked=False
        )
        self.Controls.Add(self.chk_debug); y += 26

        self.chk_open = styled_checkbox(
            "Open RFA in Revit after building",
            Point(16, y), Size(460, 22),
            checked=self._s.get("open_after", False)
        )
        self.Controls.Add(self.chk_open); y += 32

        divider(y); y += 12

        # ── Buttons ─────────────────────────────────────────────────────────
        self.btn_build = styled_button("Build Family", Point(172, y), Size(160, 36), COL_BTN_BUILD)
        self.btn_build.Click += self._on_ok
        self.Controls.Add(self.btn_build)

        # No close button — use window X

        self._update_labels()
        # Show count for pre-loaded image from settings
        self._update_seg_count()

    # ── Label updater ────────────────────────────────────────────────────────

    def _update_labels(self):
        self.lbl_smooth.Text = str(self.sl_smooth.Value)
        self.lbl_detail.Text = str(self.sl_detail.Value) + " mm"
        self._update_seg_count()

    # ── Python finder ────────────────────────────────────────────────────────

    def _update_seg_count(self):
        """
        Count SVG path nodes directly in IronPython for instant feedback.
        Uses the same min_detail logic as CPython to estimate segment count.
        """
        path = self.tb_image.Text
        if not path or not path.lower().endswith(".svg") or not os.path.isfile(path):
            self.lbl_seg_count.Text = ""
            return
        try:
            import re as _re
            with open(path, "r") as f:
                svg_content = f.read()

            # Count all coordinate pairs in path d attributes — rough node count
            # Extract d= attribute values — try double then single quotes
            d_attrs = _re.findall('d="([^"]+)"', svg_content)
            if not d_attrs:
                d_attrs = _re.findall("d='([^']+)'", svg_content)

            total_nodes = 0
            for d in d_attrs:
                # Count coordinate pairs — each pair = one node
                nums = _re.findall(
                    r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d)
                # Rough: 2 numbers per coordinate pair
                total_nodes += len(nums) // 2

            # Estimate segments after min_detail filtering
            # At 1mm min_detail, ~190mm height, avg segment ~0.9mm
            # So effective segments ≈ perimeter_nodes * (1 / min_detail_factor)
            min_mm = max(1, self.sl_detail.Value)
            # Scale factor: at min_detail=1 keep ~85%, at 5 keep ~25%
            keep_ratio = max(0.05, 1.0 / (min_mm * 0.7))
            est = int(total_nodes * min(1.0, keep_ratio))

            if total_nodes > 0:
                self.lbl_seg_count.Text = (
                    "SVG nodes found: {}  |  Estimated segments at current settings: ~{}".format(
                        total_nodes, est)
                )
                self.lbl_seg_count.ForeColor = Color.FromArgb(160, 160, 160)
            else:
                self.lbl_seg_count.Text = "No path data found in SVG"
                self.lbl_seg_count.ForeColor = Color.FromArgb(200, 80, 80)
        except Exception as ex:
            self.lbl_seg_count.Text = "Could not read SVG: {}".format(str(ex)[:60])
            self.lbl_seg_count.ForeColor = Color.FromArgb(200, 80, 80)

    def _find_python(self):
        username = os.environ.get("USERNAME", "")
        candidates = [
            r"C:\Users\{}\AppData\Local\Python\pythoncore-3.14-64\python.exe".format(username),
            r"C:\Python311\python.exe",
            r"C:\Python310\python.exe",
            r"C:\Users\{}\AppData\Local\Programs\Python\Python311\python.exe".format(username),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return "python"

    # ── Browse handlers ──────────────────────────────────────────────────────

    def _pick_image(self, s, e):
        dlg = OpenFileDialog()
        dlg.Title  = "Select Source Image"
        dlg.Filter = "SVG Files (*.svg)|*.svg|Images (*.png;*.jpg;*.jpeg;*.bmp)|*.png;*.jpg;*.jpeg;*.bmp"
        last = self.tb_image.Text
        if last and os.path.isdir(os.path.dirname(last)):
            dlg.InitialDirectory = os.path.dirname(last)
        if dlg.ShowDialog() == DialogResult.OK:
            self.tb_image.Text = dlg.FileName
            self._update_seg_count()

    def _pick_template(self, s, e):
        dlg = OpenFileDialog()
        dlg.Title  = "Select Family Template"
        dlg.Filter = "Family Templates (*.rft)|*.rft"
        last = self.tb_template.Text
        if last and os.path.isdir(os.path.dirname(last)):
            dlg.InitialDirectory = os.path.dirname(last)
        if dlg.ShowDialog() == DialogResult.OK:
            self.tb_template.Text = dlg.FileName

    def _pick_save(self, s, e):
        from System.Windows.Forms import SaveFileDialog
        dlg = SaveFileDialog()
        dlg.Title    = "Save Family As"
        dlg.Filter   = "Revit Family (*.rfa)|*.rfa"
        dlg.FileName = os.path.basename(self.tb_save.Text) or "CookieCutter.rfa"
        last = self.tb_save.Text
        if last and os.path.isdir(os.path.dirname(last)):
            dlg.InitialDirectory = os.path.dirname(last)
        if dlg.ShowDialog() == DialogResult.OK:
            self.tb_save.Text = dlg.FileName

    def _pick_python(self, s, e):
        dlg = OpenFileDialog()
        dlg.Title  = "Locate CPython Executable"
        dlg.Filter = "Executables (*.exe)|*.exe"
        if dlg.ShowDialog() == DialogResult.OK:
            self.tb_python.Text = dlg.FileName

    # ── Validate & save settings ─────────────────────────────────────────────

    def _on_ok(self, s, e):
        errors = []
        if not self.tb_image.Text or not os.path.isfile(self.tb_image.Text):
            errors.append("Please select a valid source image.")
        if not self.tb_template.Text or not os.path.isfile(self.tb_template.Text):
            errors.append("Please select a valid family template (.rft).")
        if not self.tb_save.Text:
            errors.append("Please specify a save path for the .rfa file.")
        if errors:
            show_warning("Validation", "\n".join(errors))
            return

        save_settings({
            "image_path":    self.tb_image.Text,
            "template_path": self.tb_template.Text,
            "save_path":     self.tb_save.Text,
            "python_path":   self.tb_python.Text,
            "height_mm":     float(self.nm_height.Value),
            "wall_mm":       float(self.nm_wall.Value),
            "smoothing":     self.sl_smooth.Value,
            "min_detail":    self.sl_detail.Value,
            "tolerance":     0.5,
            "fallback_lines": self.chk_fallback.Checked,
            "open_after":     self.chk_open.Checked,
        })

        self.DialogResult = DialogResult.OK
        self.Close()

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def image_path(self):     return self.tb_image.Text
    @property
    def template_path(self):  return self.tb_template.Text
    @property
    def save_path(self):      return self.tb_save.Text
    @property
    def python_path(self):    return self.tb_python.Text
    @property
    def height_mm(self):      return float(self.nm_height.Value)
    @property
    def wall_mm(self):        return float(self.nm_wall.Value)
    @property
    def smoothing(self):      return self.sl_smooth.Value
    @property
    def min_detail(self):     return self.sl_detail.Value
    @property
    def tolerance(self):      return 0.5
    @property
    def debug_mode(self):     return self.chk_debug.Checked
    @property
    def open_after(self):     return self.chk_open.Checked
    @property
    def fallback_lines(self): return self.chk_fallback.Checked


# ---------------------------------------------------------------------------
# IMAGE PROCESSOR CALL
# ---------------------------------------------------------------------------

def run_image_processor(form):
    tmp_csv = os.path.join(tempfile.gettempdir(), "cookie_cutter_out.txt")
    outline_height_mm = 190.0

    cmd = [
        form.python_path,
        PROCESSOR,
        "--image",      form.image_path,
        "--height_mm",  str(outline_height_mm),
        "--smoothing",  str(form.smoothing),
        "--min_detail", str(form.min_detail),
        "--tolerance",  str(form.tolerance),
        "--output",     tmp_csv,
    ]
    cmd_str = " ".join('"{}"'.format(a) if " " in str(a) else str(a) for a in cmd)

    try:
        proc = subprocess.Popen(cmd_str, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=True)
        stdout, stderr = proc.communicate()
        stdout = stdout.decode("utf-8", errors="replace").strip()
        stderr = stderr.decode("utf-8", errors="replace").strip()
    except Exception as ex:
        stdout = ""
        stderr = str(ex)
        raise RuntimeError("Subprocess failed: {}".format(ex))

    if not os.path.isfile(tmp_csv):
        raise RuntimeError(
            "image_processor.py produced no output.\n\nSTDOUT:\n{}\nSTDERR:\n{}".format(
                stdout, stderr))

    with open(tmp_csv, "r") as f:
        raw = f.read()
    os.remove(tmp_csv)

    stripped = raw.strip()

    if stripped.startswith("{") and "error" in stripped[:100]:
        try:
            err = json.loads(stripped)
            if "error" in err:
                raise RuntimeError("Processor error: {}".format(err["error"]))
        except ValueError:
            pass

    lines = stripped.splitlines()

    if lines and lines[0].startswith("count:"):
        segments = []
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) != 4:
                continue
            try:
                sx,sy,ex,ey = float(parts[0]),float(parts[1]),float(parts[2]),float(parts[3])
                segments.append({"type":"line","start":[sx,sy],"end":[ex,ey]})
            except ValueError:
                continue
        data = {"segments": segments, "segment_count": len(segments), "_stdout": stdout}
        return data

    elif stripped.startswith("{"):
        raise RuntimeError(
            "image_processor.py is outdated — please replace it with the latest version.")
    else:
        raise RuntimeError("Unexpected output:\n" + stripped[:300])


# ---------------------------------------------------------------------------
# GEOMETRY HELPERS
# ---------------------------------------------------------------------------

MIN_CURVE_FT = 0.8 / 304.8   # 0.8mm in feet


def make_xyz(x_mm, y_mm):
    return XYZ(mm_to_ft(x_mm), mm_to_ft(y_mm), 0)


def offset_segment(seg, offset_mm):
    s = copy.deepcopy(seg)
    if seg["type"] == "line":
        sx, sy = seg["start"]
        ex, ey = seg["end"]
        dx, dy = ex - sx, ey - sy
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return s
        nx, ny = -dy / length, dx / length
        s["start"] = [sx + nx * offset_mm, sy + ny * offset_mm]
        s["end"]   = [ex + nx * offset_mm, ey + ny * offset_mm]
    return s


def make_curve_array(segments, wall_offset_mm=0.0):
    """
    Build a flat list of Revit Line objects.
    Snaps closing gap if sub-minimum to guarantee a closed loop.
    """
    if wall_offset_mm != 0.0:
        segments = [offset_segment(s, wall_offset_mm) for s in segments]
    if not segments:
        return []

    MIN_MM = 0.8

    pts = [list(seg["start"]) for seg in segments]

    # Merge consecutive points that are too close
    kept = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0]-kept[-1][0], p[1]-kept[-1][1]) >= MIN_MM:
            kept.append(p)

    # Handle closing gap
    closing = math.hypot(kept[0][0]-kept[-1][0], kept[0][1]-kept[-1][1])
    if closing < MIN_MM:
        kept[-1] = list(kept[0])   # snap last to first
    else:
        kept.append(list(kept[0])) # add closing point

    if len(kept) < 4:
        return []

    xyz_pts = [XYZ(mm_to_ft(p[0]), mm_to_ft(p[1]), 0) for p in kept]

    curves = []
    for i in range(len(xyz_pts) - 1):
        s_ft = xyz_pts[i]
        e_ft = xyz_pts[i + 1]
        if s_ft.DistanceTo(e_ft) < MIN_CURVE_FT:
            continue
        try:
            curves.append(Line.CreateBound(s_ft, e_ft))
        except Exception:
            pass

    return curves


def get_ref_plane(fam_doc):
    """Find or create the Ref Level sketch plane."""
    collector = FilteredElementCollector(fam_doc).OfClass(SketchPlane)
    for sp in collector:
        try:
            sp_name = sp.Name or ""
            if "Ref. Level" in sp_name or "Reference" in sp_name or "Level" in sp_name:
                return sp
        except Exception:
            continue
    plane = Plane.CreateByNormalAndOrigin(XYZ.BasisZ, XYZ.Zero)
    return SketchPlane.Create(fam_doc, plane)


# ---------------------------------------------------------------------------
# DRAW MODEL LINES (debug / fallback)
# ---------------------------------------------------------------------------

def draw_model_lines(app, form, segments, save_path):
    """Place raw line segments as model lines — no extrusion."""
    fam_doc = app.OpenDocumentFile(form.template_path)

    with Transaction(fam_doc, "Cookie Cutter — Model Lines") as t:
        t.Start()
        ref_plane = get_ref_plane(fam_doc)
        placed = 0
        for seg in segments:
            try:
                sx, sy = seg["start"]
                ex, ey = seg["end"]
                s_ft = make_xyz(sx, sy)
                e_ft = make_xyz(ex, ey)
                if s_ft.DistanceTo(e_ft) < MIN_CURVE_FT:
                    continue
                fam_doc.FamilyCreate.NewModelCurve(
                    Line.CreateBound(s_ft, e_ft), ref_plane)
                placed += 1
            except Exception:
                pass
        t.Commit()

    save_opts = SaveAsOptions()
    save_opts.OverwriteExistingFile = True
    for path in [save_path,
                 os.path.join(tempfile.gettempdir(), os.path.basename(save_path))]:
        try:
            fam_doc.SaveAs(path, save_opts)
            fam_doc.Close(False)
            return placed, path
        except Exception:
            continue

    fam_doc.Close(False)
    raise RuntimeError("Could not save model line family.")


# ---------------------------------------------------------------------------
# CREATE EXTRUDED FAMILY
# ---------------------------------------------------------------------------

def create_family(app, form, segments):
    """Open template, create hollow extrusion, save .rfa."""
    fam_doc   = app.OpenDocumentFile(form.template_path)
    height_ft = mm_to_ft(form.height_mm)

    with Transaction(fam_doc, "Cookie Cutter Extrusion") as t:
        t.Start()
        ref_plane = get_ref_plane(fam_doc)

        outer_curves = make_curve_array(segments)
        inner_curves = make_curve_array(segments, wall_offset_mm=form.wall_mm)

        if len(outer_curves) < 3:
            t.RollBack()
            fam_doc.Close(False)
            raise RuntimeError(
                "Too few curves for outer loop ({}).".format(len(outer_curves)))

        profile = CurveArrArray()

        outer_arr = CurveArray()
        for c in outer_curves:
            outer_arr.Append(c)
        profile.Append(outer_arr)

        if len(inner_curves) >= 3:
            inner_arr = CurveArray()
            for c in inner_curves:
                inner_arr.Append(c)
            profile.Append(inner_arr)

        fam_doc.FamilyCreate.NewExtrusion(True, profile, ref_plane, height_ft)
        t.Commit()

    save_opts = SaveAsOptions()
    save_opts.OverwriteExistingFile = True
    save_path = form.save_path
    saved = False

    for path in [save_path,
                 os.path.join(os.path.expanduser("~"), "Desktop",
                              os.path.basename(save_path)),
                 os.path.join(tempfile.gettempdir(), os.path.basename(save_path))]:
        try:
            fam_doc.SaveAs(path, save_opts)
            save_path = path
            saved = True
            break
        except Exception:
            continue

    fam_doc.Close(False)
    if not saved:
        raise RuntimeError("Family created but could not be saved.")
    return save_path


# ---------------------------------------------------------------------------
# MODEL LINE FALLBACK SAVE PATH
# ---------------------------------------------------------------------------

def fallback_save_path(primary_path):
    """Insert '_model lines' suffix before the .rfa extension."""
    base, ext = os.path.splitext(primary_path)
    return base + "_model lines" + ext


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    uiapp = __revit__  # noqa
    app   = uiapp.Application

    form = CookieCutterForm()
    if form.ShowDialog() != DialogResult.OK:
        return

    # Process image — run directly, no blocking dialog needed
    try:
        data = run_image_processor(form)
    except Exception as ex:
        show_error("Image Processing Failed", str(ex))
        return

    segments = data.get("segments", [])
    if not segments:
        show_warning("No Geometry",
                     "No segments were generated.\n"
                     "Try reducing Minimum Segment Length.")
        return

    # ── Debug mode: model lines only ────────────────────────────────────────
    if form.debug_mode:
        ml_path = fallback_save_path(form.save_path)
        try:
            placed, saved_to = draw_model_lines(app, form, segments, ml_path)
            show_info("Debug Complete",
                      "Model lines placed: {}\n\nSaved to:\n{}".format(placed, saved_to))
        except Exception as ex:
            show_error("Debug Failed", str(ex))
        return

    # ── Full extrusion ───────────────────────────────────────────────────────
    extrusion_ok   = False
    extrusion_path = ""
    extrusion_error = ""

    try:
        extrusion_path = create_family(app, form, segments)
        extrusion_ok = True
    except Exception as ex:
        extrusion_error = str(ex)

    # ── Model lines — always if ticked, or silently as fallback on failure ───
    ml_path   = fallback_save_path(form.save_path)
    ml_saved  = False
    ml_to     = ""

    if form.fallback_lines or not extrusion_ok:
        try:
            placed, ml_to = draw_model_lines(app, form, segments, ml_path)
            ml_saved = True
        except Exception:
            pass   # silent — don't interrupt success message

    # ── Report results ───────────────────────────────────────────────────────
    if extrusion_ok:
        show_info("Success",
                  "Family saved successfully!\n\n"
                  "{} segments placed.\n\n"
                  "Path: {}".format(len(segments), extrusion_path))
        if form.open_after and os.path.isfile(extrusion_path):
            try:
                uiapp.OpenAndActivateDocument(extrusion_path)
            except Exception:
                try:
                    import subprocess as _sp
                    _sp.Popen(["explorer", extrusion_path], shell=True)
                except Exception:
                    pass
    else:
        if ml_saved:
            show_warning("Extrusion Failed — Model Line .rfa Saved",
                         "The extrusion could not be created:\n{}\n\n"
                         "A Model Line .rfa has been saved to:\n{}\n\n"
                         "Open this family to manually create the extrusion "
                         "from the traced lines.".format(extrusion_error, ml_to))
        else:
            show_error("Family Creation Failed", extrusion_error)


main()
