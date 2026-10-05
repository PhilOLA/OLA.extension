# -*- coding: utf-8 -*-
"""
Export & Publish v3.4.9
pyRevit pushbutton script - IronPython 2.7
Engine: IronPython2

Exports Revit sheets (from a Print Set or the active sheet) to PDF and/or DWG
with a configurable naming convention, WPF dialog, progress bar, and run history.
Optionally publishes exported files to ACC/Forma via Autodesk Desktop Connector.

Naming convention:
    {Prefix}-{OLA Building Name}-{SheetNo}-{SheetName}{ Title2}{ Title3}{-Rev X}

PDF generation: Revit's built-in doc.Export() / PDFExportOptions API (native engine).
PDF merge: CPython3 subprocess helper using pypdf PdfWriter.
Publish: File.Copy() to ACC/Forma via Autodesk Desktop Connector local paths.
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import os
import io
import re
import json
import time
import datetime
import subprocess
import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("System")
clr.AddReference("System.Xml")
clr.AddReference("System.Windows.Forms")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Data")

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    ViewSheet,
    ViewSheetSet,
    PDFExportOptions,
    ColorDepthType,
    RasterQualityType,
    ExportPaperFormat,
    DWGExportOptions,
    ExportDWGSettings,
    BuiltInCategory,
)
from Autodesk.Revit.UI import TaskDialog
import System
from System import Uri, Environment
from System.IO import Path, File, Directory
from System.Windows import Window, Thickness, Visibility
from System.Windows.Threading import Dispatcher, DispatcherPriority
from System.Data import DataTable as WpfDataTable
from System.Windows.Input import Keyboard, ModifierKeys

from pyrevit import revit, DB, forms

doc   = revit.doc
uidoc = revit.uidoc

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR   = os.path.dirname(__file__)
CONFIG_PATH  = os.path.join(SCRIPT_DIR, "export_config.json")
INVALID_CHARS = r'[<>:"/\\|?*\x00-\x1f]'

PAPER_SIZES = ["A0","A1","A2","A3","A4","ISO A0","ISO A1","ISO A2","ISO A3","ISO A4"]

# ---------------------------------------------------------------------------
# ACC / Desktop Connector helpers
# ---------------------------------------------------------------------------
def find_acc_docs_root():
    """Return the ACCDocs root path if Desktop Connector is mounted, else None."""
    user_profile = os.environ.get("USERPROFILE", "")
    if not user_profile:
        return None
    acc_root = os.path.join(user_profile, "ACCDocs")
    return acc_root if Directory.Exists(acc_root) else None

def acc_folder_accessible(path):
    """Return True if path exists and is under an accessible ACC mount."""
    if not path:
        return False
    try:
        return Directory.Exists(path)
    except Exception:
        return False

def publish_files(file_paths, dest_folder, overwrite, log_lines):
    """
    Copy file_paths into dest_folder via File.Copy().
    overwrite=True  → overwrite existing; False → skip existing.
    Returns list of (src, dest, ok, msg).
    """
    results = []
    if not Directory.Exists(dest_folder):
        try:
            Directory.CreateDirectory(dest_folder)
        except Exception as ex:
            log_lines.append(u"  [Publish] Cannot create dest folder: {}".format(ex))
            return [(p, None, False, str(ex)) for p in file_paths]

    for src in file_paths:
        fname = os.path.basename(src)
        dest  = os.path.join(dest_folder, fname)
        try:
            if File.Exists(dest):
                if overwrite:
                    File.Delete(dest)
                else:
                    log_lines.append(u"  [Publish] Skipped (exists): {}".format(fname))
                    results.append((src, dest, False, "Skipped"))
                    continue
            File.Copy(src, dest)
            log_lines.append(u"  ✓  [Publish] {}".format(fname))
            results.append((src, dest, True, ""))
        except Exception as ex:
            log_lines.append(u"  ✗  [Publish] {} [ERROR: {}]".format(fname, ex))
            results.append((src, dest, False, str(ex)))
    return results

# Muted colours for variable-mode print-setup row indicators (dark-theme safe)
SETUP_COLORS = [
    "#3D5A80", "#5C4A72", "#2D6A4F", "#7B4A00",
    "#1D4E6F", "#6B4226", "#2C5F5F", "#614051",
    "#4A5240", "#5A3E5E", "#3A5A45", "#5A4A2D",
]

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config():
    default = {
        "previous_prefixes":     [],
        "previous_pdf_folders":  [],
        "previous_dwg_folders":  [],
        "previous_acc_pdf_folders": [],
        "previous_acc_dwg_folders": [],
        "last_print_set":        "",
        "last_dwg_setup":        "",
        "last_print_setup":      "",
        "last_output_type":      "PDF",
        "last_prefix":           "",
        "last_open_folder":      True,
        "pdf_engine":            "native",
        "last_acc_pdf_folder":   "",
        "last_acc_dwg_folder":   "",
        "last_acc_overwrite":    False,
        "runs":                  [],
        "presets":               {}
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with io.open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return default

def save_config(cfg):
    try:
        with io.open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("Warning: could not save config: {}".format(e))

def add_unique(lst, value, maxlen=20):
    if value and value not in lst:
        lst.insert(0, value)
    elif value in lst:
        lst.remove(value)
        lst.insert(0, value)
    return lst[:maxlen]

# ---------------------------------------------------------------------------
# Revit data helpers
# ---------------------------------------------------------------------------
def get_print_sets():
    result = []
    try:
        for vss in FilteredElementCollector(doc).OfClass(ViewSheetSet):
            result.append((vss.Name, vss))
        result.sort(key=lambda x: x[0].lower())
    except Exception as e:
        print("get_print_sets error: {}".format(e))
    return result

def get_named_print_settings():
    """Return [(name, PrintSetting), ...] sorted by name."""
    from Autodesk.Revit.DB import PrintSetting as _PS
    try:
        items = list(FilteredElementCollector(doc).OfClass(_PS))
        return sorted([(ps.Name, ps) for ps in items], key=lambda x: x[0].lower())
    except Exception as e:
        print("get_named_print_settings error: {}".format(e))
        return []


def _params_from_print_setup(named_ps):
    """Extract paper/colour/orientation/hide params from a named PrintSetting.
    Returns a dict with keys: paper_size, black_white, landscape,
    hide_crop, hide_ref_planes, hide_scope_boxes, hide_unref_tags."""
    from Autodesk.Revit.DB import ColorDepthType, PageOrientationType
    defaults = {
        "paper_size": "A3", "black_white": False, "landscape": True,
        "hide_crop": True, "hide_ref_planes": True,
        "hide_scope_boxes": True, "hide_unref_tags": True,
    }
    if named_ps is None:
        return defaults
    try:
        p = named_ps.PrintParameters
        sz = "A3"
        try:
            sz_name = (p.PaperSize.Name or "").upper()
            for iso in ["A0", "A1", "A2", "A3", "A4"]:
                if iso in sz_name:
                    sz = iso
                    break
        except Exception:
            pass
        try:    bw    = (p.ColorDepth == ColorDepthType.BlackLine)
        except Exception: bw = False
        try:    land  = (p.PageOrientation != PageOrientationType.Portrait)
        except Exception: land = True
        try:    hcrop = p.HideCropBoundaries
        except Exception: hcrop = True
        try:    href  = p.HideReferencePlane
        except Exception: href = True
        try:    hscope = p.HideScopeBoxes
        except Exception: hscope = True
        try:    hunref = p.HideUnreferencedViewTags
        except Exception: hunref = True
        return {
            "paper_size": sz, "black_white": bw, "landscape": land,
            "hide_crop": hcrop, "hide_ref_planes": href,
            "hide_scope_boxes": hscope, "hide_unref_tags": hunref,
        }
    except Exception:
        return defaults


def _resolve_print_params(settings):
    """Look up the named print setup stored in settings and return resolved params dict."""
    from Autodesk.Revit.DB import PrintSetting as _PS2
    ps_name = settings.get("print_setup_name", "")
    named_ps = None
    if ps_name:
        try:
            for ps in FilteredElementCollector(doc).OfClass(_PS2):
                if ps.Name == ps_name:
                    named_ps = ps
                    break
        except Exception:
            pass
    return _params_from_print_setup(named_ps)


def get_sheets_from_print_set(view_sheet_set):
    sheets = []
    try:
        for v in view_sheet_set.Views:
            if isinstance(v, ViewSheet):
                sheets.append(v)
    except Exception:
        for v in view_sheet_set.Views:
            try:
                if v.GetType().Name == "ViewSheet":
                    sheets.append(v)
            except Exception:
                pass
    sheets.sort(key=lambda s: s.SheetNumber)
    return sheets

def get_sheet_size(sheet):
    """Return A0/A1/A2/A3/A4.  Three methods tried in order:
    1. sheet.Outline (paper boundary, feet)
    2. Title block family/type name
    3. Title block bounding box (feet)
    Falls back to "WxH" raw mm string for debugging if nothing matches."""
    MM_PER_FT = 304.8
    ISO_SIZES = [
        ("A4",  210,  297),
        ("A3",  297,  420),
        ("A2",  420,  594),
        ("A1",  594,  841),
        ("A0",  841, 1189),
    ]
    TOLERANCE = 35

    def classify(w_mm, h_mm):
        lo, hi = min(w_mm, h_mm), max(w_mm, h_mm)
        for label, s, l in ISO_SIZES:
            if abs(lo - s) < TOLERANCE and abs(hi - l) < TOLERANCE:
                return label
        return None

    # Method 1: ViewSheet.Outline — direct paper-boundary API
    try:
        o = sheet.Outline
        w = abs(o.Max.U - o.Min.U) * MM_PER_FT
        h = abs(o.Max.V - o.Min.V) * MM_PER_FT
        result = classify(w, h)
        if result:
            return result
        # Store raw dims for fallback display
        raw = u"{:.0f}x{:.0f}".format(max(w, h), min(w, h))
    except Exception:
        raw = "?"

    # Methods 2 & 3: title block family
    try:
        col = FilteredElementCollector(doc, sheet.Id)\
            .OfCategory(BuiltInCategory.OST_TitleBlocks)\
            .WhereElementIsNotElementType()
        for tb in col:
            # 2. Name
            try:
                name = (tb.Symbol.Family.Name + " " + tb.Symbol.Name).upper()
                for label, _, _ in ISO_SIZES:
                    if label in name:
                        return label
            except Exception:
                pass
            # 3. Bounding box
            try:
                bb = tb.get_BoundingBox(None)
                if bb:
                    w = abs(bb.Max.X - bb.Min.X) * MM_PER_FT
                    h = abs(bb.Max.Y - bb.Min.Y) * MM_PER_FT
                    result = classify(w, h)
                    if result:
                        return result
                    raw = u"{:.0f}x{:.0f}".format(max(w, h), min(w, h))
            except Exception:
                pass
    except Exception:
        pass
    return raw  # show raw mm so we can debug



def get_active_sheet():
    try:
        av = uidoc.ActiveView
        if isinstance(av, ViewSheet):
            return av
    except Exception:
        pass
    return None

def get_dwg_export_setups():
    setups = []
    seen   = set()
    def _add(name, eid):
        if name and name not in seen and "<in-session" not in name.lower():
            seen.add(name)
            setups.append((name, eid))
    try:
        for inc_types in [False, True]:
            col = FilteredElementCollector(doc).OfClass(ExportDWGSettings)
            col = col.WhereElementIsElementType() if inc_types \
                  else col.WhereElementIsNotElementType()
            for s in col:
                try: _add(s.Name, s.Id)
                except Exception: pass
    except Exception as e:
        print("DWG setups error: {}".format(e))
    setups.sort(key=lambda x: x[0].lower())
    return setups

def get_param_value(sheet, param_name):
    p = sheet.LookupParameter(param_name)
    if p and p.HasValue:
        return (p.AsString() or "").strip()
    return ""

def get_project_number():
    try:
        return (doc.ProjectInformation.Number or "").strip()
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# Filename builder
# ---------------------------------------------------------------------------
def build_sheet_filename(sheet, prefix, include_revision):
    prefix     = prefix.rstrip("- ").strip().replace("/", "_")
    sheet_no   = (sheet.SheetNumber or "").strip("- ")
    sheet_name = sheet.Name or ""
    bldg_name  = (get_param_value(sheet, "OLA Building Name") or "").rstrip("- ").strip()
    title2     = get_param_value(sheet, "OLA Sheet Title 2")
    title3     = get_param_value(sheet, "OLA Sheet Title 3")

    parts = [prefix]
    if bldg_name:
        parts.append(bldg_name)
    parts.append(sheet_no)
    numbered = "-".join(p for p in parts if p)

    full_title = sheet_name
    if title2: full_title += " " + title2
    if title3: full_title += " " + title3

    rev_suffix = ""
    if include_revision:
        rev_param = sheet.LookupParameter("Current Revision")
        rev = (rev_param.AsString() if rev_param and rev_param.HasValue else "") or ""
        rev = rev.strip()
        rev_suffix = "-Rev {}".format(rev) if rev else "-REV-"

    name = "{}-{}{}".format(numbered, full_title, rev_suffix)
    name = re.sub(INVALID_CHARS, "_", name)
    return name.strip("_. ")

# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------
def export_pdfs(sheets, settings, log_lines, progress_cb=None):
    log_lines.append(u"PDF engine: Native (Revit Export)")
    return _export_pdfs_native(sheets, settings, log_lines, progress_cb)


def _export_pdfs_native(sheets, settings, log_lines, progress_cb=None):
    output_folder = settings["pdf_folder"]
    if not Directory.Exists(output_folder):
        Directory.CreateDirectory(output_folder)
    if not Directory.Exists(output_folder):
        log_lines.append(u"  ERROR: PDF folder not accessible: {}".format(output_folder))
        return []

    results  = []
    total    = len(sheets)

    sheet_setups = settings.get("sheet_print_setups", {})

    for idx, sheet in enumerate(sheets):
        if progress_cb:
            progress_cb(idx, total, sheet.SheetNumber)

        fname    = build_sheet_filename(sheet, settings["prefix"], settings["include_revision"])
        out_path = os.path.join(output_folder, fname + ".pdf")

        if File.Exists(out_path) and settings.get("overwrite_prompt"):
            from System.Windows import MessageBox, MessageBoxButton, MessageBoxResult, MessageBoxImage
            res = MessageBox.Show(
                "{}.pdf already exists.\n\nYes = Overwrite\nNo = Skip\nCancel = Overwrite all".format(fname),
                "File Exists", MessageBoxButton.YesNoCancel, MessageBoxImage.Question)
            if res == MessageBoxResult.No:
                log_lines.append(u"  - Skipped (exists): {}".format(fname + ".pdf"))
                results.append((sheet, out_path, False, "Skipped"))
                continue
            elif res == MessageBoxResult.Cancel:
                settings["overwrite_prompt"] = False

        # Per-sheet settings override for Variable mode
        sh_settings = settings
        if sheet_setups:
            ps_name = sheet_setups.get(sheet.Id.IntegerValue, "")
            if ps_name != settings.get("print_setup_name", ""):
                sh_settings = dict(settings)
                sh_settings["print_setup_name"] = ps_name

        try:
            opts = _build_pdf_options(sh_settings)
            opts.FileName = fname
            view_ids = System.Collections.Generic.List[DB.ElementId]()
            view_ids.Add(sheet.Id)
            doc.Export(output_folder, view_ids, opts)
            log_lines.append(u"  \u2713  {}".format(fname + ".pdf"))
            results.append((sheet, out_path, True, ""))
        except Exception as e:
            log_lines.append(u"  \u2717  {} [ERROR: {}]".format(fname + ".pdf", str(e)))
            results.append((sheet, out_path, False, str(e)))

    if progress_cb:
        progress_cb(total, total, "")
    return results

def _build_pdf_options(settings):
    from Autodesk.Revit.DB import PageOrientationType, PaperPlacementType, ZoomType as RVTZoomType
    opts = PDFExportOptions()
    pp = _resolve_print_params(settings)
    _set_paper_size(opts, pp["paper_size"])
    try:
        opts.ColorDepth = ColorDepthType.BlackLine \
            if pp["black_white"] else ColorDepthType.Color
    except Exception: pass
    try:
        opts.RasterQuality = RasterQualityType.High
    except Exception: pass
    try:
        opts.PageOrientation = PageOrientationType.Landscape \
            if pp["landscape"] else PageOrientationType.Portrait
    except Exception: pass
    try: opts.HideCropBoundaries       = pp["hide_crop"]
    except Exception: pass
    try: opts.HideReferencePlane       = pp["hide_ref_planes"]
    except Exception: pass
    try: opts.HideScopeBoxes           = pp["hide_scope_boxes"]
    except Exception: pass
    try: opts.HideUnreferencedViewTags = pp["hide_unref_tags"]
    except Exception: pass
    try:
        opts.PaperPlacement = PaperPlacementType.Center
    except Exception: pass
    try:
        opts.ZoomType = RVTZoomType.Zoom
        opts.Zoom = 100
    except Exception: pass
    try: opts.ExportingAreas = False
    except Exception: pass
    # Always True for per-sheet export so Revit uses opts.FileName, not its own naming.
    # Multi-sheet PDF merging is handled separately by pypdf after all sheets are exported.
    try: opts.Combine = True
    except Exception: pass
    return opts

def _set_paper_size(opts, paper_str):
    mapping = {
        "A0": ExportPaperFormat.ISO_A0, "A1": ExportPaperFormat.ISO_A1,
        "A2": ExportPaperFormat.ISO_A2, "A3": ExportPaperFormat.ISO_A3,
        "A4": ExportPaperFormat.ISO_A4,
        "ISO A0": ExportPaperFormat.ISO_A0, "ISO A1": ExportPaperFormat.ISO_A1,
        "ISO A2": ExportPaperFormat.ISO_A2, "ISO A3": ExportPaperFormat.ISO_A3,
        "ISO A4": ExportPaperFormat.ISO_A4,
    }
    try: opts.PaperFormat = mapping.get(paper_str, ExportPaperFormat.ISO_A3)
    except Exception: pass

# ---------------------------------------------------------------------------
# DWG export
# ---------------------------------------------------------------------------
def export_dwgs(sheets, settings, log_lines, progress_cb=None):
    output_folder = settings["dwg_folder"]
    if not Directory.Exists(output_folder):
        Directory.CreateDirectory(output_folder)
    if not Directory.Exists(output_folder):
        log_lines.append(u"  ERROR: DWG folder not accessible: {}".format(output_folder))
        return []

    results    = []
    total      = len(sheets)
    setup_name = settings.get("dwg_setup_name")
    setup_id   = settings.get("dwg_setup_id")

    for idx, sheet in enumerate(sheets):
        if progress_cb:
            progress_cb(idx, total, sheet.SheetNumber)

        desired_name = build_sheet_filename(
            sheet, settings["prefix"], settings["include_revision"])
        desired_path = os.path.join(output_folder, desired_name + ".dwg")

        if File.Exists(desired_path) and settings.get("overwrite_prompt"):
            from System.Windows import MessageBox, MessageBoxButton, MessageBoxResult, MessageBoxImage
            res = MessageBox.Show(
                "{}.dwg already exists.\n\nYes = Overwrite\nNo = Skip\nCancel = Overwrite all".format(desired_name),
                "File Exists", MessageBoxButton.YesNoCancel, MessageBoxImage.Question)
            if res == MessageBoxResult.No:
                log_lines.append(u"  - Skipped (exists): {}".format(desired_name + ".dwg"))
                results.append((sheet, desired_path, False, "Skipped"))
                continue
            elif res == MessageBoxResult.Cancel:
                settings["overwrite_prompt"] = False

        try:
            opts = _load_dwg_options(setup_id, setup_name)
            view_ids = System.Collections.Generic.List[DB.ElementId]()
            view_ids.Add(sheet.Id)
            doc.Export(output_folder, desired_name, view_ids, opts)

            exported = None
            for candidate in [
                desired_path,
                os.path.join(output_folder, desired_name + ".dwg"),
                os.path.join(output_folder,
                             "{} - {}.dwg".format(sheet.SheetNumber, sheet.Name)),
            ]:
                if File.Exists(candidate):
                    exported = candidate
                    break

            if exported and exported != desired_path:
                if File.Exists(desired_path): File.Delete(desired_path)
                File.Move(exported, desired_path)

            if File.Exists(desired_path):
                log_lines.append(u"  \u2713  {}".format(desired_name + ".dwg"))
                results.append((sheet, desired_path, True, ""))
            else:
                log_lines.append(u"  \u2717  {} [ERROR: file not found]".format(
                    desired_name + ".dwg"))
                results.append((sheet, desired_path, False, "File not found"))

        except Exception as e:
            log_lines.append(u"  \u2717  {} [ERROR: {}]".format(
                desired_name + ".dwg", str(e)))
            results.append((sheet, desired_path, False, str(e)))

    if progress_cb:
        progress_cb(total, total, "")
    return results

def _load_dwg_options(setup_id, setup_name):
    opts = DWGExportOptions()
    loaded = False
    if setup_id:
        try:
            saved = doc.GetElement(setup_id)
            if saved:
                opts   = saved.GetDWGExportOptions()
                loaded = True
        except Exception: pass
    if not loaded and setup_name:
        try:
            for s in FilteredElementCollector(doc).OfClass(ExportDWGSettings):
                if s.Name == setup_name:
                    opts   = s.GetDWGExportOptions()
                    loaded = True
                    break
        except Exception: pass
    if not loaded and setup_name:
        try:
            opts   = DWGExportOptions.GetPredefinedOptions(doc, setup_name)
            loaded = True
        except Exception: pass
    return opts

# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------
def write_log(settings, log_lines, output_folder):
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(output_folder, "ExportLog_{}.txt".format(ts))
    header   = [
        u"Export & Publish v3.4.9 - Log",
        u"Date/Time : {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        u"Prefix    : {}".format(settings.get("prefix", "")),
        u"Print Set : {}".format(settings.get("print_set_name", "")),
        u"Output    : {}".format(settings.get("output_type", "")),
        u"PDF Engine: native",
        u"Print Setup: {}".format(
            "Variable (per sheet)" if settings.get("variable_print_setup")
            else settings.get("print_setup_name", "(defaults)")),
        u"Merged    : {}".format("Yes" if settings.get("merge_pdf") else "No"),
        u"-" * 60,
        u"",
    ]
    try:
        with io.open(log_path, "w", encoding="utf-8") as f:
            f.write(u"\n".join(header + log_lines))
    except Exception as e:
        print("Could not write log: {}".format(e))
    return log_path

# ---------------------------------------------------------------------------
# Dispatcher helper
# ---------------------------------------------------------------------------
def _do_events():
    try:
        Dispatcher.CurrentDispatcher.Invoke(
            System.Action(lambda: None),
            DispatcherPriority.Background
        )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# XAML — dark theme
# ---------------------------------------------------------------------------
XAML = u"""
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Export &amp; Publish — v3.4.9"
    Width="1300" Height="1200" MinHeight="700"
    WindowStartupLocation="CenterScreen"
    Background="#1E1E1E"
    Foreground="#E0E0E0"
    FontFamily="Segoe UI"
    FontSize="13">
  <Window.Resources>

    <Style x:Key="SectionLabel" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#888780"/>
      <Setter Property="FontSize" Value="10"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Margin" Value="0,14,0,4"/>
    </Style>
    <Style x:Key="Divider" TargetType="Separator">
      <Setter Property="Background" Value="#333333"/>
      <Setter Property="Height" Value="1"/>
      <Setter Property="Margin" Value="0,0,0,6"/>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="Background" Value="#2D2D2D"/>
      <Setter Property="Foreground" Value="#E0E0E0"/>
      <Setter Property="BorderBrush" Value="#444444"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="6,5"/>
      <Setter Property="Height" Value="30"/>
      <Setter Property="CaretBrush" Value="#E0E0E0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Foreground" Value="#E0E0E0"/>
      <Setter Property="BorderBrush" Value="#444444"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Height" Value="30"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ComboBox">
            <Grid x:Name="Root">
              <Border x:Name="Bd"
                      Background="#2D2D2D"
                      BorderBrush="{TemplateBinding BorderBrush}"
                      BorderThickness="{TemplateBinding BorderThickness}"
                      CornerRadius="2"/>
              <ToggleButton Focusable="False"
                            IsChecked="{Binding IsDropDownOpen, Mode=TwoWay,
                                        RelativeSource={RelativeSource TemplatedParent}}"
                            ClickMode="Press">
                <ToggleButton.Template>
                  <ControlTemplate TargetType="ToggleButton">
                    <Border Background="Transparent">
                      <Path Data="M 0 0 L 5 5 L 10 0 Z"
                            Fill="#888780" Width="10" Height="5"
                            HorizontalAlignment="Right"
                            VerticalAlignment="Center"
                            Margin="0,0,10,0"/>
                    </Border>
                  </ControlTemplate>
                </ToggleButton.Template>
              </ToggleButton>
              <ContentPresenter IsHitTestVisible="False"
                                Content="{TemplateBinding SelectionBoxItem}"
                                ContentTemplate="{TemplateBinding SelectionBoxItemTemplate}"
                                Margin="8,0,30,0"
                                VerticalAlignment="Center"
                                TextBlock.Foreground="{TemplateBinding Foreground}"/>
              <Popup x:Name="PART_Popup"
                     Placement="Bottom"
                     IsOpen="{TemplateBinding IsDropDownOpen}"
                     AllowsTransparency="True"
                     Focusable="False">
                <Border MinWidth="{Binding ActualWidth, ElementName=Root}"
                        MaxHeight="200"
                        Background="#2D2D2D"
                        BorderBrush="#555555"
                        BorderThickness="1">
                  <ScrollViewer>
                    <ItemsPresenter/>
                  </ScrollViewer>
                </Border>
              </Popup>
            </Grid>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="ComboBoxItem">
      <Setter Property="Background" Value="#2D2D2D"/>
      <Setter Property="Foreground" Value="#E0E0E0"/>
      <Setter Property="Padding" Value="8,6"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ComboBoxItem">
            <Border x:Name="Bd"
                    Background="{TemplateBinding Background}"
                    Padding="{TemplateBinding Padding}">
              <ContentPresenter TextBlock.Foreground="{TemplateBinding Foreground}"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsHighlighted" Value="True">
                <Setter TargetName="Bd" Property="Background" Value="#3D3D3D"/>
              </Trigger>
              <Trigger Property="IsSelected" Value="True">
                <Setter TargetName="Bd" Property="Background" Value="#3D4550"/>
                <Setter Property="Foreground" Value="#E0E0E0"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="CheckBox">
      <Setter Property="Foreground" Value="#E0E0E0"/>
      <Setter Property="Margin" Value="0,4,18,4"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="RadioButton">
      <Setter Property="Foreground" Value="#E0E0E0"/>
      <Setter Property="Margin" Value="0,4,18,4"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="Label">
      <Setter Property="Foreground" Value="#E0E0E0"/>
    </Style>
    <!-- Blue accent = main export action; switches to orange for publish modes via code -->
    <Style x:Key="AccentButton" TargetType="Button">
      <Setter Property="Background" Value="#4FC3F7"/>
      <Setter Property="Foreground" Value="#181818"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding" Value="22,8"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
    </Style>
    <!-- Ghost / secondary buttons -->
    <Style x:Key="GhostButton" TargetType="Button">
      <Setter Property="Background" Value="#2D2D2D"/>
      <Setter Property="Foreground" Value="#C0C0C0"/>
      <Setter Property="BorderBrush" Value="#444444"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="14,7"/>
      <Setter Property="Cursor" Value="Hand"/>
    </Style>
    <!-- Output-type toggle buttons — row 1 (no publish) -->
    <Style x:Key="OutTypeBtn" TargetType="RadioButton">
      <Setter Property="Foreground" Value="#C0C0C0"/>
      <Setter Property="Background" Value="#2D2D2D"/>
      <Setter Property="BorderBrush" Value="#444444"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="16,8"/>
      <Setter Property="Margin" Value="0,0,6,0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FontSize" Value="12"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RadioButton">
            <Border Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}"
                    Padding="{TemplateBinding Padding}"
                    CornerRadius="3">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter Property="Background" Value="#4FC3F7"/>
                <Setter Property="Foreground" Value="#181818"/>
                <Setter Property="BorderBrush" Value="#4FC3F7"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="BorderBrush" Value="#4FC3F7"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <!-- Output-type toggle buttons — row 2 (with publish) -->
    <Style x:Key="OutTypePubBtn" TargetType="RadioButton">
      <Setter Property="Foreground" Value="#C0C0C0"/>
      <Setter Property="Background" Value="#2D2D2D"/>
      <Setter Property="BorderBrush" Value="#553322"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="12,7"/>
      <Setter Property="Margin" Value="0,0,6,0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FontSize" Value="11.5"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RadioButton">
            <Border Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}"
                    Padding="{TemplateBinding Padding}"
                    CornerRadius="3">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter Property="Background" Value="#F4723A"/>
                <Setter Property="Foreground" Value="White"/>
                <Setter Property="BorderBrush" Value="#F4723A"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="BorderBrush" Value="#F4723A"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="TabItem">
      <Setter Property="Foreground" Value="#888780"/>
      <Setter Property="Background" Value="#262626"/>
      <Setter Property="Padding" Value="14,7"/>
      <Setter Property="FontSize" Value="12"/>
    </Style>
    <Style TargetType="TabControl">
      <Setter Property="Background" Value="#1E1E1E"/>
      <Setter Property="BorderBrush" Value="#333333"/>
      <Setter Property="BorderThickness" Value="1"/>
    </Style>
    <Style TargetType="ProgressBar">
      <Setter Property="Height" Value="4"/>
      <Setter Property="Foreground" Value="#4FC3F7"/>
      <Setter Property="Background" Value="#333333"/>
      <Setter Property="BorderThickness" Value="0"/>
    </Style>
    <Style TargetType="ListBox">
      <Setter Property="Background" Value="#262626"/>
      <Setter Property="BorderBrush" Value="#444444"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Foreground" Value="#E0E0E0"/>
    </Style>
  </Window.Resources>

  <Border Background="#262626" Margin="8"
          BorderBrush="#333333" BorderThickness="1">
    <Grid>
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>

      <!-- Header -->
      <StackPanel Grid.Row="0" Margin="20,18,20,10">
        <TextBlock Text="Export &amp; Publish"
                   FontSize="22" FontWeight="Bold" Foreground="#4FC3F7"/>
        <TextBlock FontSize="11" Margin="0,3,0,0">
          <Run Text="v3.4.9" Foreground="#4FC3F7"/>
          <Run Text="  ·  Batch PDF / DWG export from Revit print sets — with optional ACC/Forma publish."
               Foreground="#888780"/>
        </TextBlock>
      </StackPanel>
      <Separator Grid.Row="0" VerticalAlignment="Bottom"
                 Background="#333333" Height="1" Margin="0"/>

      <!-- Tabs -->
      <TabControl x:Name="MainTabs" Grid.Row="1" Margin="0">

        <!-- ======= GENERAL TAB ======= -->
        <TabItem Header="General">
          <Grid Background="#1E1E1E">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="14"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>

            <!-- ===== LEFT COLUMN: SHEET SELECTION ===== -->
            <Border Grid.Column="0"
                    BorderBrush="#383838" BorderThickness="1" CornerRadius="3"
                    Margin="12,8,0,8" Padding="12,10,12,10" Background="#191919">
              <Grid>
                <Grid.RowDefinitions>
                  <RowDefinition Height="Auto"/>
                  <RowDefinition Height="*"/>
                  <RowDefinition Height="Auto"/>
                </Grid.RowDefinitions>

                <!-- Header + radio buttons (always visible) -->
                <StackPanel Grid.Row="0">
                  <TextBlock Text="SHEET SELECTION" Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                  <Separator Style="{StaticResource Divider}"/>
                  <StackPanel Orientation="Horizontal" Margin="0,6,0,8">
                    <RadioButton x:Name="RbPrintSet"    Content="Print Set"
                                 GroupName="SheetSel" IsChecked="True" Margin="0,0,18,0"/>
                    <RadioButton x:Name="RbActiveSheet" Content="Current Sheet"
                                 GroupName="SheetSel" Margin="0,0,18,0"/>
                    <RadioButton x:Name="RbAllSheets"   Content="All Sheets in Model"
                                 GroupName="SheetSel"/>
                  </StackPanel>
                </StackPanel>

                <!-- PnlPrintSet: combobox + filter + stretching DataGrid -->
                <Grid Grid.Row="1" x:Name="PnlPrintSet">
                  <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                  </Grid.RowDefinitions>

                  <StackPanel Grid.Row="0">
                    <!-- Combobox + sheet count -->
                    <Grid Margin="0,0,0,4">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <ComboBox x:Name="CboPrintSet" VerticalAlignment="Center"/>
                      <TextBlock x:Name="TxtSheetCount"
                                 Grid.Column="1" VerticalAlignment="Center"
                                 Foreground="#4FC3F7" FontSize="11" Margin="10,0,0,0"/>
                    </Grid>
                    <TextBlock x:Name="TxtNoPrintSets"
                               Text="No sheet sets found — create one via File → Print → Select… → Save"
                               FontSize="10" Foreground="#F4723A"
                               Margin="0,0,0,4" TextWrapping="Wrap"
                               Visibility="Collapsed"/>
                    <!-- Variable per-sheet assign panel (shown only in Variable mode) -->
                    <Grid x:Name="PnlVariableAssign" Margin="0,0,0,4"
                          Visibility="Collapsed">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <TextBlock Grid.Column="0" Text="Assign to selected:"
                                 Foreground="#888780" FontSize="11"
                                 VerticalAlignment="Center" Margin="0,0,6,0"/>
                      <ComboBox x:Name="CboAssignSetup" Grid.Column="1"
                                VerticalAlignment="Center"/>
                      <Button x:Name="BtnApplySetup" Grid.Column="2"
                              Content="Apply"
                              Style="{StaticResource GhostButton}"
                              Margin="6,0,0,0"
                              ToolTip="Apply this print setup to all selected rows"/>
                      <!-- #7 Auto-assign by sheet size -->
                      <Button x:Name="BtnAutoAssign" Grid.Column="3"
                              Content="Auto-assign"
                              Style="{StaticResource GhostButton}"
                              Margin="4,0,0,0"
                              ToolTip="Auto-assign each sheet to the print setup whose name best matches the sheet size"/>
                    </Grid>

                    <!-- #4 Drawn By filter -->
                    <Grid Margin="0,0,0,2">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <TextBlock Grid.Column="0" Text="Drawn by:"
                                 Foreground="#888780" FontSize="11"
                                 VerticalAlignment="Center" Margin="0,0,6,0"/>
                      <ComboBox x:Name="CboDrawnByFilter" Grid.Column="1"
                                ToolTip="Filter sheet list by Drawn By value"/>
                      <Button x:Name="BtnClearFilter" Grid.Column="2"
                              Content="✕" Style="{StaticResource GhostButton}"
                              Margin="4,0,0,0" FontSize="10"
                              ToolTip="Clear Drawn By filter"/>
                    </Grid>
                    <!-- Filter + All / None -->
                    <Grid Margin="0,0,0,4">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <TextBox x:Name="TxtSheetFilter" Grid.Column="0"
                               Foreground="#AAAAAA"
                               ToolTip="Filter by sheet number, name or size (e.g. A1)"/>
                      <Button x:Name="BtnSelectAll"   Content="All"  Grid.Column="1"
                              Style="{StaticResource GhostButton}" Margin="4,0,0,0"
                              ToolTip="Select all visible sheets"/>
                      <Button x:Name="BtnDeselectAll" Content="None" Grid.Column="2"
                              Style="{StaticResource GhostButton}" Margin="4,0,0,0"
                              ToolTip="Deselect all visible sheets"/>
                    </Grid>
                  </StackPanel>

                  <!-- DataGrid fills remaining height -->
                  <DataGrid Grid.Row="1" x:Name="DgSheets"
                            AutoGenerateColumns="False"
                            CanUserAddRows="False" CanUserDeleteRows="False"
                            CanUserReorderColumns="False"
                            SelectionMode="Extended" SelectionUnit="FullRow"
                            Background="#1A1A1A" Foreground="#E0E0E0"
                            BorderBrush="#444444" BorderThickness="1"
                            RowBackground="#1A1A1A" AlternatingRowBackground="#222222"
                            GridLinesVisibility="Horizontal"
                            HorizontalGridLinesBrush="#2A2A2A"
                            HeadersVisibility="Column"
                            FontSize="12">
                    <DataGrid.ColumnHeaderStyle>
                      <Style TargetType="DataGridColumnHeader">
                        <Setter Property="Background" Value="#181818"/>
                        <Setter Property="Foreground" Value="#888780"/>
                        <Setter Property="FontSize" Value="10"/>
                        <Setter Property="FontWeight" Value="SemiBold"/>
                        <Setter Property="Padding" Value="6,5"/>
                        <Setter Property="BorderBrush" Value="#333333"/>
                        <Setter Property="BorderThickness" Value="0,0,0,1"/>
                      </Style>
                    </DataGrid.ColumnHeaderStyle>
                    <DataGrid.RowStyle>
                      <Style TargetType="DataGridRow">
                        <Setter Property="Foreground" Value="#E0E0E0"/>
                        <Setter Property="Height" Value="24"/>
                      </Style>
                    </DataGrid.RowStyle>
                    <DataGrid.CellStyle>
                      <Style TargetType="DataGridCell">
                        <Setter Property="BorderThickness" Value="0"/>
                        <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
                        <Style.Triggers>
                          <Trigger Property="IsSelected" Value="True">
                            <Setter Property="Background" Value="#2A3A4A"/>
                            <Setter Property="Foreground" Value="#E0E0E0"/>
                          </Trigger>
                        </Style.Triggers>
                      </Style>
                    </DataGrid.CellStyle>
                    <DataGrid.Columns>
                      <DataGridCheckBoxColumn Header="&#10003;" Binding="{Binding [Included]}"
                                              Width="34" CanUserSort="False"/>
                      <!-- #1 PSColor strip: thin colored bar per print-setup (Variable mode) -->
                      <DataGridTemplateColumn x:Name="ColPSColor" Header=""
                                              Width="6" CanUserSort="False"
                                              Visibility="Collapsed">
                        <DataGridTemplateColumn.CellTemplate>
                          <DataTemplate>
                            <Rectangle Fill="{Binding [PSColor]}" Width="4" Height="18"
                                       HorizontalAlignment="Center" VerticalAlignment="Center"/>
                          </DataTemplate>
                        </DataGridTemplateColumn.CellTemplate>
                      </DataGridTemplateColumn>
                      <!-- #5 Changed indicator: ★ when sheet has revisions newer than last export -->
                      <DataGridTextColumn Header="★" Binding="{Binding [Changed]}"
                                          Width="22" IsReadOnly="True"
                                          CanUserSort="True">
                        <DataGridTextColumn.ElementStyle>
                          <Style TargetType="TextBlock">
                            <Setter Property="Foreground" Value="#F4C842"/>
                            <Setter Property="HorizontalAlignment" Value="Center"/>
                            <Setter Property="ToolTip" Value="Sheet has revisions newer than last export run"/>
                          </Style>
                        </DataGridTextColumn.ElementStyle>
                      </DataGridTextColumn>
                      <DataGridTextColumn Header="Sheet No."
                                          Binding="{Binding [SheetNo]}"
                                          Width="90" IsReadOnly="True"/>
                      <DataGridTextColumn Header="Sheet Name"
                                          Binding="{Binding [SheetName]}"
                                          Width="*" IsReadOnly="True"/>
                      <DataGridTextColumn Header="Print Setup"
                                          Binding="{Binding [PrintSetup]}"
                                          Width="140" IsReadOnly="True"
                                          Visibility="Collapsed"/>
                      <DataGridTextColumn Header="Size"
                                          Binding="{Binding [Size]}"
                                          Width="46" IsReadOnly="True"/>
                    </DataGrid.Columns>
                  </DataGrid>
                </Grid>

                <!-- Active sheet name (shown when Current Sheet mode) -->
                <TextBlock Grid.Row="2" x:Name="TxtActiveSheetName"
                           Visibility="Collapsed"
                           Foreground="#4FC3F7" FontSize="12"
                           Margin="0,4,0,0"/>
              </Grid>
            </Border>

            <!-- ===== RIGHT COLUMN: settings ===== -->
            <ScrollViewer Grid.Column="2"
                          VerticalScrollBarVisibility="Auto"
                          Background="#1E1E1E" Padding="0,4,12,12">
              <StackPanel>

                <!-- #6 NAMED CONFIGURATION PRESETS -->
                <Border BorderBrush="#2A2A2A" BorderThickness="1" CornerRadius="3"
                        Margin="0,0,0,10" Padding="8,6,8,6" Background="#161616">
                  <Grid>
                    <Grid.ColumnDefinitions>
                      <ColumnDefinition Width="Auto"/>
                      <ColumnDefinition Width="*"/>
                      <ColumnDefinition Width="Auto"/>
                      <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Grid.Column="0" Text="Preset:"
                               Foreground="#888780" FontSize="11"
                               VerticalAlignment="Center" Margin="0,0,6,0"/>
                    <ComboBox x:Name="CboPresets" Grid.Column="1"
                              ToolTip="Select a saved configuration preset"/>
                    <Button x:Name="BtnSavePreset" Grid.Column="2"
                            Content="Save" Style="{StaticResource GhostButton}"
                            Margin="4,0,0,0"
                            ToolTip="Save current settings as a named preset"/>
                    <Button x:Name="BtnLoadPreset" Grid.Column="3"
                            Content="Load" Style="{StaticResource GhostButton}"
                            Margin="4,0,0,0"
                            ToolTip="Load selected preset into all settings fields"/>
                  </Grid>
                </Border>

                <!-- OUTPUT TYPE -->
                <TextBlock Text="OUTPUT TYPE" Style="{StaticResource SectionLabel}"/>
                <Separator Style="{StaticResource Divider}"/>
                <!-- Row 1: export only -->
                <StackPanel Orientation="Horizontal" Margin="0,4,0,4">
                  <RadioButton x:Name="RbPdf"  Content="PDF only"
                               GroupName="OutType" IsChecked="True"
                               Style="{StaticResource OutTypeBtn}"/>
                  <RadioButton x:Name="RbDwg"  Content="DWG only"
                               GroupName="OutType"
                               Style="{StaticResource OutTypeBtn}"/>
                  <RadioButton x:Name="RbBoth" Content="PDF + DWG"
                               GroupName="OutType"
                               Style="{StaticResource OutTypeBtn}"/>
                </StackPanel>
                <!-- Row 2: export + publish -->
                <StackPanel Orientation="Horizontal" Margin="0,0,0,4">
                  <RadioButton x:Name="RbPdfPub"    Content="PDF + Publish"
                               GroupName="OutType"
                               Style="{StaticResource OutTypePubBtn}"/>
                  <RadioButton x:Name="RbDwgPub"    Content="DWG + Publish"
                               GroupName="OutType"
                               Style="{StaticResource OutTypePubBtn}"/>
                  <RadioButton x:Name="RbBothPub"   Content="PDF+DWG + Publish"
                               GroupName="OutType"
                               Style="{StaticResource OutTypePubBtn}"/>
                </StackPanel>

                <!-- FILE NAMING -->
                <Border BorderBrush="#383838" BorderThickness="1" CornerRadius="3"
                        Margin="0,8,0,0" Padding="12,10,12,10" Background="#191919">
                  <StackPanel>
                    <TextBlock Text="FILE NAMING" Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                    <Separator Style="{StaticResource Divider}"/>
                    <Grid Margin="0,0,0,4">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="160"/>
                      </Grid.ColumnDefinitions>
                      <Label x:Name="PrefixLabel" Content="Project Number" Grid.Column="0"
                             VerticalAlignment="Center" Padding="0,0,8,0"/>
                      <TextBox x:Name="TxtPrefix" Grid.Column="1"
                               Margin="0,0,8,0"
                               ToolTip="Project number prefix e.g. 2401-AR"/>
                      <ComboBox x:Name="CboPrevPrefix" Grid.Column="2"
                                ToolTip="Previous Project No."/>
                    </Grid>
                    <TextBlock FontSize="10" Foreground="#888780"
                               Margin="100,2,0,4"
                               Text="Format: {Project No}-{Building}-{SheetNo}-{SheetName}{-Rev X}"/>
                    <CheckBox x:Name="ChkRevision"
                              Content="Append revision  (no revision shows as 'REV-')"/>
                    <TextBlock Text="Disabled when merging multiple sheets."
                               FontSize="10" Foreground="#888780" Margin="0,2,0,0"/>

                    <Separator Style="{StaticResource Divider}" Margin="0,10,0,6"/>
                    <CheckBox x:Name="ChkMerge" Content="Merge sheets into single PDF"/>
                    <StackPanel x:Name="PnlMergedName" Margin="0,6,0,0">
                      <TextBlock Text="MERGED PDF FILENAME"
                                 Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                      <TextBox x:Name="TxtMergedName"
                               ToolTip="Filename for merged PDF (no extension)"/>
                      <TextBlock Text="Filename for merged PDF (no extension)"
                                 FontSize="10" Foreground="#888780" Margin="0,3,0,0"/>
                    </StackPanel>
                  </StackPanel>
                </Border>

                <!-- PDF OUTPUT — blue group -->
                <Border x:Name="PnlPdfOutput"
                        BorderBrush="#4FC3F7" BorderThickness="1"
                        CornerRadius="3" Margin="0,8,0,0"
                        Padding="12,10,12,12"
                        Background="#07111A">
                  <StackPanel>
                    <TextBlock Text="PDF OUTPUT" FontSize="10" FontWeight="SemiBold"
                               Foreground="#4FC3F7" Margin="0,0,0,8"/>
                    <TextBlock Text="PDF OUTPUT FOLDER" Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                    <Separator Style="{StaticResource Divider}"/>
                    <Grid Margin="0,0,0,4">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <TextBox x:Name="TxtPdfFolder"
                               ToolTip="{Binding Text, RelativeSource={RelativeSource Self}}"/>
                      <StackPanel Grid.Column="1" Orientation="Horizontal" Margin="6,0,0,0">
                        <Button x:Name="BtnBrowsePdf" Content="Browse..."
                                Style="{StaticResource GhostButton}" Margin="0,0,4,0"/>
                        <Button x:Name="BtnCreatePdf" Content="Create"
                                Style="{StaticResource GhostButton}"
                                IsEnabled="False" ToolTip="Create this folder"/>
                      </StackPanel>
                    </Grid>
                    <TextBlock x:Name="TxtPdfFolderStatus"
                               FontSize="10" Margin="0,3,0,0"
                               Visibility="Collapsed"/>
                  </StackPanel>
                </Border>

                <!-- DWG OUTPUT — teal group -->
                <Border x:Name="PnlDwgOutput"
                        BorderBrush="#4DB6AC" BorderThickness="1"
                        CornerRadius="3" Margin="0,8,0,0"
                        Padding="12,10,12,12"
                        Background="#071A16">
                  <StackPanel>
                    <TextBlock Text="DWG OUTPUT" FontSize="10" FontWeight="SemiBold"
                               Foreground="#4DB6AC" Margin="0,0,0,8"/>
                    <TextBlock Text="DWG OUTPUT FOLDER" Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                    <Separator Style="{StaticResource Divider}"/>
                    <Grid Margin="0,0,0,4">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <TextBox x:Name="TxtDwgFolder"
                               ToolTip="{Binding Text, RelativeSource={RelativeSource Self}}"/>
                      <StackPanel Grid.Column="1" Orientation="Horizontal" Margin="6,0,0,0">
                        <Button x:Name="BtnBrowseDwg" Content="Browse..."
                                Style="{StaticResource GhostButton}" Margin="0,0,4,0"/>
                        <Button x:Name="BtnCreateDwg" Content="Create"
                                Style="{StaticResource GhostButton}"
                                IsEnabled="False" ToolTip="Create this folder"/>
                      </StackPanel>
                    </Grid>
                    <TextBlock x:Name="TxtDwgFolderStatus"
                               FontSize="10" Margin="0,3,0,6"
                               Visibility="Collapsed"/>
                    <TextBlock Text="DWG EXPORT SETUP" Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                    <Separator Style="{StaticResource Divider}"/>
                    <ComboBox x:Name="CboDwgSetup" Margin="0,4,0,0"/>
                  </StackPanel>
                </Border>

                <!-- ACC / FORMA OUTPUT — shown only in publish modes -->
                <Border x:Name="PnlAccDest"
                        Visibility="Collapsed"
                        BorderBrush="#F4723A" BorderThickness="1"
                        CornerRadius="3" Margin="0,8,0,0"
                        Padding="12,10,12,12"
                        Background="#1A1410">
                  <StackPanel>
                    <StackPanel Orientation="Horizontal" Margin="0,0,0,8">
                      <TextBlock Text="ACC / FORMA OUTPUT"
                                 FontSize="10" FontWeight="SemiBold"
                                 Foreground="#F4723A" VerticalAlignment="Center"/>
                      <TextBlock x:Name="TxtAccStatus"
                                 FontSize="10" Foreground="#888780"
                                 Margin="12,0,0,0" VerticalAlignment="Center"/>
                    </StackPanel>

                    <!-- PDF dest -->
                    <StackPanel x:Name="PnlAccPdfRow" Margin="0,0,0,6">
                      <TextBlock Text="PDF DESTINATION FOLDER"
                                 Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                      <Grid Margin="0,0,0,3">
                        <Grid.ColumnDefinitions>
                          <ColumnDefinition Width="*"/>
                          <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBox x:Name="TxtAccPdfFolder"
                                 ToolTip="{Binding Text, RelativeSource={RelativeSource Self}}"/>
                        <StackPanel Grid.Column="1" Orientation="Horizontal" Margin="6,0,0,0">
                          <Button x:Name="BtnBrowseAccPdf" Content="Browse..."
                                  Style="{StaticResource GhostButton}" Margin="0,0,4,0"/>
                        </StackPanel>
                      </Grid>
                      <TextBlock x:Name="TxtAccPdfStatus"
                                 FontSize="10" Margin="0,2,0,0"
                                 Visibility="Collapsed"/>
                    </StackPanel>

                    <!-- DWG dest -->
                    <StackPanel x:Name="PnlAccDwgRow" Margin="0,0,0,6">
                      <TextBlock Text="DWG DESTINATION FOLDER"
                                 Style="{StaticResource SectionLabel}" Margin="0,0,0,4"/>
                      <Grid Margin="0,0,0,3">
                        <Grid.ColumnDefinitions>
                          <ColumnDefinition Width="*"/>
                          <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBox x:Name="TxtAccDwgFolder"
                                 ToolTip="{Binding Text, RelativeSource={RelativeSource Self}}"/>
                        <StackPanel Grid.Column="1" Orientation="Horizontal" Margin="6,0,0,0">
                          <Button x:Name="BtnBrowseAccDwg" Content="Browse..."
                                  Style="{StaticResource GhostButton}" Margin="0,0,4,0"/>
                        </StackPanel>
                      </Grid>
                      <TextBlock x:Name="TxtAccDwgStatus"
                                 FontSize="10" Margin="0,2,0,0"
                                 Visibility="Collapsed"/>
                    </StackPanel>

                    <!-- Conflict resolution -->
                    <StackPanel Orientation="Horizontal" Margin="0,4,0,0">
                      <TextBlock Text="On conflict:"
                                 Foreground="#888780" FontSize="11"
                                 VerticalAlignment="Center" Margin="0,0,10,0"/>
                      <RadioButton x:Name="RbAccSkip"     Content="Skip existing"
                                   GroupName="AccConflict" IsChecked="True"
                                   Foreground="#E0E0E0" Margin="0,4,16,4"/>
                      <RadioButton x:Name="RbAccOverwrite" Content="Overwrite"
                                   GroupName="AccConflict"
                                   Foreground="#E0E0E0" Margin="0,4,0,4"/>
                    </StackPanel>
                    <TextBlock Text="Requires Autodesk Desktop Connector — ACC files are accessed via local ACCDocs mount."
                               FontSize="10" Foreground="#888780" TextWrapping="Wrap"
                               Margin="0,4,0,0"/>
                  </StackPanel>
                </Border>

                <!-- PRINT SETTINGS — collapsible expander -->
                <Border BorderBrush="#383838" BorderThickness="1" CornerRadius="3"
                        Margin="0,8,0,0" Padding="0" Background="#191919">
                  <Expander x:Name="ExpPrintSettings" IsExpanded="False">
                    <Expander.Style>
                      <Style TargetType="Expander">
                        <Setter Property="Background" Value="Transparent"/>
                        <Setter Property="Foreground" Value="#E0E0E0"/>
                        <Setter Property="FontSize" Value="11"/>
                        <Setter Property="Template">
                          <Setter.Value>
                            <ControlTemplate TargetType="Expander">
                              <StackPanel>
                                <ToggleButton x:Name="HeaderSite"
                                              IsChecked="{Binding IsExpanded, Mode=TwoWay,
                                                          RelativeSource={RelativeSource TemplatedParent}}"
                                              Background="Transparent" BorderThickness="0"
                                              Padding="12,8" HorizontalContentAlignment="Left"
                                              Cursor="Hand">
                                  <ToggleButton.Style>
                                    <Style TargetType="ToggleButton">
                                      <Setter Property="Background" Value="Transparent"/>
                                      <Setter Property="Template">
                                        <Setter.Value>
                                          <ControlTemplate TargetType="ToggleButton">
                                            <ContentPresenter/>
                                          </ControlTemplate>
                                        </Setter.Value>
                                      </Setter>
                                    </Style>
                                  </ToggleButton.Style>
                                  <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                                    <TextBlock x:Name="TxtExpandArrow" Text="▶"
                                               FontSize="16" FontWeight="Bold"
                                               Foreground="#555555"
                                               VerticalAlignment="Center"
                                               Margin="0,0,10,0"/>
                                    <TextBlock Text="⚙  PRINT SETTINGS"
                                               FontSize="11" FontWeight="SemiBold"
                                               Foreground="#888780" VerticalAlignment="Center"/>
                                  </StackPanel>
                                </ToggleButton>
                                <ContentPresenter x:Name="ExpandSite"
                                                  Visibility="Collapsed"
                                                  Margin="12,0,12,10"/>
                              </StackPanel>
                              <ControlTemplate.Triggers>
                                <Trigger Property="IsExpanded" Value="True">
                                  <Setter TargetName="ExpandSite" Property="Visibility" Value="Visible"/>
                                  <Setter TargetName="TxtExpandArrow" Property="Text" Value="▼"/>
                                </Trigger>
                              </ControlTemplate.Triggers>
                            </ControlTemplate>
                          </Setter.Value>
                        </Setter>
                      </Style>
                    </Expander.Style>
                    <StackPanel>
                      <TextBlock Text="PDF ENGINE" Style="{StaticResource SectionLabel}"/>
                      <Separator Style="{StaticResource Divider}"/>
                      <TextBlock Text="PDF engine: Native (Revit Export)"
                                 FontSize="10" Foreground="#4DB6AC" Margin="0,2,0,6"/>

                      <TextBlock Text="PRINT SETUP" Style="{StaticResource SectionLabel}"/>
                      <Separator Style="{StaticResource Divider}"/>
                      <ComboBox x:Name="CboPrintSetup" Margin="0,4,0,6"/>

                      <TextBlock Text="OPTIONS" Style="{StaticResource SectionLabel}" Margin="0,4,0,4"/>
                      <Separator Style="{StaticResource Divider}"/>
                      <WrapPanel Margin="0,2,0,0">
                        <CheckBox x:Name="ChkOpenFolder"      Content="Open folder when done"   IsChecked="True"/>
                        <CheckBox x:Name="ChkOverwritePrompt" Content="Prompt before overwrite" IsChecked="True"/>
                      </WrapPanel>
                    </StackPanel>
                  </Expander>
                </Border>

                <!-- SHEET ORDER -->
                <StackPanel x:Name="PnlSheetOrder">
                  <TextBlock Text="SHEET ORDER" Style="{StaticResource SectionLabel}"/>
                  <Separator Style="{StaticResource Divider}"/>
                  <Button x:Name="BtnEditOrder" Content="Edit Print Order..."
                          HorizontalAlignment="Left"
                          Style="{StaticResource GhostButton}"/>
                </StackPanel>

              </StackPanel>
            </ScrollViewer>

          </Grid>
        </TabItem>

        <!-- ======= HISTORY TAB ======= -->
        <TabItem Header="History">
          <StackPanel Margin="20,12,20,16">
            <TextBlock Text="PREVIOUS EXPORT RUNS"
                       Style="{StaticResource SectionLabel}"/>
            <Separator Style="{StaticResource Divider}"/>
            <TextBlock Text="Select a run and click Repeat to restore all settings."
                       FontSize="10" Foreground="#888780" Margin="0,4,0,8"/>
            <DataGrid x:Name="DgHistory"
                      AutoGenerateColumns="False" IsReadOnly="True"
                      Background="#262626" Foreground="#E0E0E0"
                      BorderBrush="#444444" BorderThickness="1"
                      RowBackground="#262626" AlternatingRowBackground="#1E1E1E"
                      GridLinesVisibility="Horizontal"
                      HorizontalGridLinesBrush="#333333"
                      HeadersVisibility="Column"
                      Height="380" SelectionMode="Single" FontSize="12">
              <DataGrid.ColumnHeaderStyle>
                <Style TargetType="DataGridColumnHeader">
                  <Setter Property="Background" Value="#181818"/>
                  <Setter Property="Foreground" Value="#888780"/>
                  <Setter Property="FontSize" Value="10"/>
                  <Setter Property="FontWeight" Value="SemiBold"/>
                  <Setter Property="Padding" Value="8,6"/>
                  <Setter Property="BorderBrush" Value="#333333"/>
                  <Setter Property="BorderThickness" Value="0,0,0,1"/>
                </Style>
              </DataGrid.ColumnHeaderStyle>
              <DataGrid.RowStyle>
                <Style TargetType="DataGridRow">
                  <Setter Property="Foreground" Value="#E0E0E0"/>
                </Style>
              </DataGrid.RowStyle>
              <DataGrid.Columns>
                <DataGridTextColumn Header="DATE"       Binding="{Binding Date}"       Width="120"/>
                <DataGridTextColumn Header="PRINT SET"  Binding="{Binding PrintSet}"   Width="120"/>
                <DataGridTextColumn Header="PREFIX"     Binding="{Binding Prefix}"     Width="110"/>
                <DataGridTextColumn Header="OUTPUT"     Binding="{Binding Output}"     Width="80"/>
                <DataGridTextColumn Header="SHEETS"     Binding="{Binding SheetCount}" Width="55"/>
                <DataGridTextColumn Header="PUBLISHED"  Binding="{Binding Published}"  Width="75"/>
              </DataGrid.Columns>
            </DataGrid>
            <Button x:Name="BtnRepeatRun" Content="Repeat Selected Run"
                    Margin="0,10,0,0" HorizontalAlignment="Left"
                    Style="{StaticResource GhostButton}"/>
          </StackPanel>
        </TabItem>

      </TabControl>

      <!-- Progress bar -->
      <ProgressBar x:Name="PrgExport" Grid.Row="2"
                   Minimum="0" Maximum="100" Value="0"
                   Visibility="Collapsed" Margin="12,0,12,0"/>

      <!-- Footer -->
      <Border Grid.Row="3" Background="#181818"
              BorderBrush="#333333" BorderThickness="0,1,0,0">
        <Grid Margin="20,10,20,12">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <TextBlock x:Name="TxtStatus" VerticalAlignment="Center"
                     Foreground="#888780" FontSize="11" TextWrapping="Wrap"/>
          <StackPanel Grid.Column="1" Orientation="Horizontal">
            <Button x:Name="BtnViewLog" Content="View Log"
                    Margin="0,0,8,0" IsEnabled="False"
                    Style="{StaticResource GhostButton}"/>
            <Button x:Name="BtnExport" Content="Export Package"
                    Margin="0,0,8,0"
                    Style="{StaticResource AccentButton}"/>
            <Button x:Name="BtnClose" Content="Close"
                    Style="{StaticResource GhostButton}"/>
          </StackPanel>
        </Grid>
      </Border>

    </Grid>
  </Border>
</Window>
"""

# ---------------------------------------------------------------------------
# History row (dark theme — adds Published column)
# ---------------------------------------------------------------------------
class _HistoryRow(object):
    def __init__(self, run):
        self.raw        = run
        self.Date       = run.get("date", "")
        self.PrintSet   = run.get("print_set", "")
        self.Prefix     = run.get("prefix", "")
        self.Output     = run.get("output_type", "")
        self.SheetCount = str(run.get("sheet_count", ""))
        pub = run.get("published", False)
        self.Published  = u"✓" if pub else u""


# ---------------------------------------------------------------------------
# Sheet Order Dialog — dark theme
# ---------------------------------------------------------------------------
SHEET_ORDER_XAML = u"""
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Edit Print Order"
    Width="640" Height="560"
    WindowStartupLocation="CenterScreen"
    Background="#1E1E1E"
    Foreground="#E0E0E0"
    FontFamily="Segoe UI" FontSize="13">
  <Window.Resources>
    <Style x:Key="GhostButton" TargetType="Button">
      <Setter Property="Background" Value="#2D2D2D"/>
      <Setter Property="Foreground" Value="#C0C0C0"/>
      <Setter Property="BorderBrush" Value="#444444"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="14,7"/>
      <Setter Property="Cursor" Value="Hand"/>
    </Style>
    <Style x:Key="AccentButton" TargetType="Button">
      <Setter Property="Background" Value="#F4723A"/>
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding" Value="22,8"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
    </Style>
  </Window.Resources>
  <Border Background="#262626" Margin="8"
          BorderBrush="#333333" BorderThickness="1">
    <Grid Margin="20,16,20,16">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>
      <TextBlock Grid.Row="0" Text="Edit Print Order"
                 FontSize="18" FontWeight="Bold" Foreground="#E0E0E0"
                 Margin="0,0,0,4"/>
      <TextBlock Grid.Row="1"
                 Text="Select a sheet and use the arrows to reorder. Order reflects the export sequence."
                 FontSize="11" Foreground="#888780" Margin="0,0,0,10"/>
      <Grid Grid.Row="2">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="40"/>
        </Grid.ColumnDefinitions>
        <DataGrid x:Name="DgSheets"
                  AutoGenerateColumns="False" IsReadOnly="True"
                  Background="#262626" Foreground="#E0E0E0"
                  BorderBrush="#444444" BorderThickness="1"
                  RowBackground="#262626" AlternatingRowBackground="#1E1E1E"
                  GridLinesVisibility="Horizontal"
                  HorizontalGridLinesBrush="#333333"
                  HeadersVisibility="Column"
                  SelectionMode="Single" FontSize="12"
                  CanUserResizeColumns="True">
          <DataGrid.ColumnHeaderStyle>
            <Style TargetType="DataGridColumnHeader">
              <Setter Property="Background" Value="#181818"/>
              <Setter Property="Foreground" Value="#888780"/>
              <Setter Property="FontSize" Value="10"/>
              <Setter Property="FontWeight" Value="SemiBold"/>
              <Setter Property="Padding" Value="8,6"/>
              <Setter Property="BorderBrush" Value="#333333"/>
              <Setter Property="BorderThickness" Value="0,0,0,1"/>
            </Style>
          </DataGrid.ColumnHeaderStyle>
          <DataGrid.RowStyle>
            <Style TargetType="DataGridRow">
              <Setter Property="Foreground" Value="#E0E0E0"/>
            </Style>
          </DataGrid.RowStyle>
          <DataGrid.Columns>
            <DataGridTextColumn Header="#"          Binding="{Binding Order}"   Width="35"/>
            <DataGridTextColumn Header="SHEET NO"   Binding="{Binding SheetNo}" Width="90"/>
            <DataGridTextColumn Header="SHEET NAME" Binding="{Binding Name}"    Width="180"/>
            <DataGridTextColumn Header="TITLE 2"    Binding="{Binding Title2}"  Width="120"/>
            <DataGridTextColumn Header="TITLE 3"    Binding="{Binding Title3}"  Width="120"/>
          </DataGrid.Columns>
        </DataGrid>
        <StackPanel Grid.Column="1" Margin="8,0,0,0" VerticalAlignment="Center">
          <Button x:Name="BtnUp"   Content="&#9650;"
                  Padding="6,8" Margin="0,0,0,6"
                  Style="{StaticResource GhostButton}"/>
          <Button x:Name="BtnDown" Content="&#9660;"
                  Padding="6,8"
                  Style="{StaticResource GhostButton}"/>
        </StackPanel>
      </Grid>
      <StackPanel Grid.Row="3" Orientation="Horizontal"
                  HorizontalAlignment="Right" Margin="0,12,0,0">
        <Button x:Name="BtnCancel" Content="Cancel"
                Margin="0,0,8,0" Style="{StaticResource GhostButton}"/>
        <Button x:Name="BtnOK" Content="Apply Order"
                Style="{StaticResource AccentButton}"/>
      </StackPanel>
    </Grid>
  </Border>
</Window>
"""


class _SheetRow(object):
    def __init__(self, order, sheet):
        self.sheet   = sheet
        self.Order   = str(order)
        self.SheetNo = sheet.SheetNumber or ""
        self.Name    = sheet.Name or ""
        self.Title2  = get_param_value(sheet, "OLA Sheet Title 2")
        self.Title3  = get_param_value(sheet, "OLA Sheet Title 3")


class SheetOrderDialog(object):
    def __init__(self, sheets):
        self._sheets = list(sheets)
        self.result  = None
        self._build_window()
        self._populate()
        self._wire()

    def _build_window(self):
        from System.IO import StringReader
        from System.Xml import XmlReader
        from System.Windows.Markup import XamlReader
        self.win     = XamlReader.Load(XmlReader.Create(StringReader(SHEET_ORDER_XAML)))
        def _(n):    return self.win.FindName(n)
        self.dg         = _("DgSheets")
        self.btn_up     = _("BtnUp")
        self.btn_down   = _("BtnDown")
        self.btn_ok     = _("BtnOK")
        self.btn_cancel = _("BtnCancel")

    def _populate(self):
        self.dg.Items.Clear()
        for i, s in enumerate(self._sheets):
            self.dg.Items.Add(_SheetRow(i + 1, s))

    def _wire(self):
        self.btn_up.Click     += self._move_up
        self.btn_down.Click   += self._move_down
        self.btn_ok.Click     += self._apply
        self.btn_cancel.Click += lambda s, e: self.win.Close()

    def _move_up(self, s, e):
        idx = self.dg.SelectedIndex
        if idx > 0:
            self._sheets[idx], self._sheets[idx-1] = \
                self._sheets[idx-1], self._sheets[idx]
            self._populate()
            self.dg.SelectedIndex = idx - 1

    def _move_down(self, s, e):
        idx = self.dg.SelectedIndex
        if 0 <= idx < len(self._sheets) - 1:
            self._sheets[idx], self._sheets[idx+1] = \
                self._sheets[idx+1], self._sheets[idx]
            self._populate()
            self.dg.SelectedIndex = idx + 1

    def _apply(self, s, e):
        self.result = list(self._sheets)
        self.win.Close()

    def show(self):
        self.win.ShowDialog()

# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
class ExportDialog(object):

    def __init__(self):
        self.cfg        = load_config()
        self.result     = None
        self.print_sets = get_print_sets()
        self.dwg_setups = get_dwg_export_setups()
        self.named_print_settings   = []     # resolved in _on_window_loaded
        self._sheets    = []
        self._last_log  = None

        self._build_window()
        # Wire Loaded so the window appears immediately; populate after
        self.win.Loaded += self._on_window_loaded

    # ------------------------------------------------------------------
    def _build_window(self):
        from System.IO import StringReader
        from System.Xml import XmlReader
        from System.Windows.Markup import XamlReader
        self.win = XamlReader.Load(XmlReader.Create(StringReader(XAML)))
        def _(n): return self.win.FindName(n)

        # Output type — row 1 (export only)
        self.rb_pdf            = _("RbPdf")
        self.rb_dwg            = _("RbDwg")
        self.rb_both           = _("RbBoth")
        # Output type — row 2 (export + publish)
        self.rb_pdf_pub        = _("RbPdfPub")
        self.rb_dwg_pub        = _("RbDwgPub")
        self.rb_both_pub       = _("RbBothPub")

        # ACC destination panel
        self.pnl_acc_dest      = _("PnlAccDest")
        self.txt_acc_status    = _("TxtAccStatus")
        self.pnl_acc_pdf_row   = _("PnlAccPdfRow")
        self.txt_acc_pdf_folder= _("TxtAccPdfFolder")
        self.btn_browse_acc_pdf= _("BtnBrowseAccPdf")
        self.txt_acc_pdf_status= _("TxtAccPdfStatus")
        self.pnl_acc_dwg_row   = _("PnlAccDwgRow")
        self.txt_acc_dwg_folder= _("TxtAccDwgFolder")
        self.btn_browse_acc_dwg= _("BtnBrowseAccDwg")
        self.txt_acc_dwg_status= _("TxtAccDwgStatus")
        self.rb_acc_skip       = _("RbAccSkip")
        self.rb_acc_overwrite  = _("RbAccOverwrite")

        # Sheet selection
        self.rb_print_set      = _("RbPrintSet")
        self.rb_active_sheet   = _("RbActiveSheet")
        self.rb_all_sheets     = _("RbAllSheets")
        self.cbo_print_set     = _("CboPrintSet")
        self.txt_no_print_sets = _("TxtNoPrintSets")
        self.txt_sheet_count   = _("TxtSheetCount")
        self.txt_active_sheet  = _("TxtActiveSheetName")
        self.pnl_print_set     = _("PnlPrintSet")
        self.txt_sheet_filter  = _("TxtSheetFilter")
        self.dg_sheets         = _("DgSheets")
        self.btn_select_all    = _("BtnSelectAll")
        self.btn_deselect_all  = _("BtnDeselectAll")
        self._sheet_dt         = None
        self._last_clicked_row = -1
        self._last_clicked_val = True

        # File naming
        self.txt_prefix        = _("TxtPrefix")
        self.cbo_prev_prefix   = _("CboPrevPrefix")
        self.chk_revision      = _("ChkRevision")

        # PDF output
        self.pnl_pdf_output    = _("PnlPdfOutput")
        self.txt_pdf_folder    = _("TxtPdfFolder")
        self.btn_browse_pdf    = _("BtnBrowsePdf")
        self.btn_create_pdf    = _("BtnCreatePdf")
        self.txt_pdf_status    = _("TxtPdfFolderStatus")

        self.chk_merge         = _("ChkMerge")
        self.pnl_merged_name   = _("PnlMergedName")
        self.txt_merged_name   = _("TxtMergedName")

        # DWG output
        self.pnl_dwg_output    = _("PnlDwgOutput")
        self.cbo_dwg_setup     = _("CboDwgSetup")
        self.txt_dwg_folder    = _("TxtDwgFolder")
        self.btn_browse_dwg    = _("BtnBrowseDwg")
        self.btn_create_dwg    = _("BtnCreateDwg")
        self.txt_dwg_status    = _("TxtDwgFolderStatus")

        # Print settings / appearance / options
        self.exp_print_settings   = _("ExpPrintSettings")
        self.cbo_print_setup      = _("CboPrintSetup")
        self.chk_open_folder      = _("ChkOpenFolder")
        self.chk_overwrite        = _("ChkOverwritePrompt")

        # Variable per-sheet print setup
        self.pnl_variable_assign  = _("PnlVariableAssign")
        self.cbo_assign_setup     = _("CboAssignSetup")
        self.btn_apply_setup      = _("BtnApplySetup")
        self.btn_auto_assign      = _("BtnAutoAssign")   # #7
        # DataGrid column indices (0=chk, 1=PSColor, 2=★, 3=SheetNo, 4=SheetName, 5=PrintSetup, 6=Size)
        self._col_print_setup_idx = 5   # shifted by 2 new columns at indices 1,2
        self._col_color_idx       = 1   # #1 PSColor strip column
        self._setup_color_map     = {}  # #1 name → hex color

        # #4 Drawn By filter
        self.cbo_drawn_by_filter  = _("CboDrawnByFilter")
        self.btn_clear_filter     = _("BtnClearFilter")

        # #6 Presets
        self.cbo_presets          = _("CboPresets")
        self.btn_save_preset      = _("BtnSavePreset")
        self.btn_load_preset      = _("BtnLoadPreset")

        # Sheet order
        self.pnl_sheet_order   = _("PnlSheetOrder")
        self.btn_edit_order    = _("BtnEditOrder")

        # History
        self.dg_history        = _("DgHistory")
        self.btn_repeat        = _("BtnRepeatRun")

        # Footer
        self.prg_export        = _("PrgExport")
        self.txt_status        = _("TxtStatus")
        self.btn_view_log      = _("BtnViewLog")
        self.btn_export        = _("BtnExport")
        self.btn_close         = _("BtnClose")
        self.main_tabs         = _("MainTabs")

    # ------------------------------------------------------------------
    def _populate(self):
        from System.Windows.Media import SolidColorBrush, Color as WMColor
        cfg = self.cfg

        # Named print setups — #8 add tooltips showing paper size/orientation
        self.named_print_settings = get_named_print_settings()
        self.cbo_print_setup.Items.Add("Variable (per sheet)")
        self.cbo_print_setup.Items.Add("(Use defaults)")
        from System.Windows.Controls import ComboBoxItem as _CBI
        for name, eid in self.named_print_settings:
            tip = self._build_setup_tooltip(eid)
            if tip:
                item = _CBI()
                item.Content = name
                item.ToolTip = tip
                self.cbo_print_setup.Items.Add(item)
            else:
                self.cbo_print_setup.Items.Add(name)
        last_ps  = cfg.get("last_print_setup", "")
        # Index 0 = Variable, 1 = (Use defaults), 2+ = named setups
        ps_idx   = next((i + 2 for i, (n, _) in enumerate(self.named_print_settings)
                         if n == last_ps), 1)   # default to "(Use defaults)"
        self.cbo_print_setup.SelectedIndex = ps_idx
        self.exp_print_settings.IsExpanded = cfg.get("last_print_expanded", False)

        # Print sets
        for name, _ in self.print_sets:
            self.cbo_print_set.Items.Add(name)
        last_ps = cfg.get("last_print_set", "")
        ps_idx  = next((i for i, (n, _) in enumerate(self.print_sets)
                        if n == last_ps), 0)
        if self.print_sets:
            self.cbo_print_set.SelectedIndex = ps_idx
            self.txt_no_print_sets.Visibility = Visibility.Collapsed
        else:
            self.txt_no_print_sets.Visibility = Visibility.Visible

        # DWG setups
        self.cbo_dwg_setup.Items.Add("(Revit default)")
        for name, _ in self.dwg_setups:
            self.cbo_dwg_setup.Items.Add(name)
        last_dwg = cfg.get("last_dwg_setup", "")
        dwg_idx  = next((i + 1 for i, (n, _) in enumerate(self.dwg_setups)
                         if n == last_dwg), 0)
        self.cbo_dwg_setup.SelectedIndex = dwg_idx

        # Prefix
        last_prefix    = cfg.get("last_prefix", "")
        proj_num       = get_project_number()
        default_prefix = last_prefix if last_prefix else \
                         ((proj_num + "-AR") if proj_num else "")
        self.txt_prefix.Text = default_prefix
        self.cbo_prev_prefix.Items.Add("Previous Project No.")
        for p in cfg["previous_prefixes"]:
            self.cbo_prev_prefix.Items.Add(p)
        self.cbo_prev_prefix.SelectedIndex = 0

        # PDF folder — restore last used
        if cfg["previous_pdf_folders"]:
            self.txt_pdf_folder.Text = cfg["previous_pdf_folders"][0]

        # DWG folder — restore last used
        if cfg["previous_dwg_folders"]:
            self.txt_dwg_folder.Text = cfg["previous_dwg_folders"][0]

        # ACC folders — restore last used
        last_acc_pdf = cfg.get("last_acc_pdf_folder", "")
        if last_acc_pdf:
            self.txt_acc_pdf_folder.Text = last_acc_pdf
        last_acc_dwg = cfg.get("last_acc_dwg_folder", "")
        if last_acc_dwg:
            self.txt_acc_dwg_folder.Text = last_acc_dwg

        # ACC overwrite preference
        if cfg.get("last_acc_overwrite", False):
            self.rb_acc_overwrite.IsChecked = True

        # Output type (includes new publish variants)
        last_out = cfg.get("last_output_type", "PDF")
        if   last_out == "DWG":      self.rb_dwg.IsChecked      = True
        elif last_out == "Both":     self.rb_both.IsChecked     = True
        elif last_out == "PDFPub":   self.rb_pdf_pub.IsChecked  = True
        elif last_out == "DWGPub":   self.rb_dwg_pub.IsChecked  = True
        elif last_out == "BothPub":  self.rb_both_pub.IsChecked = True
        else:                        self.rb_pdf.IsChecked      = True

        # ACC status label
        acc_root = find_acc_docs_root()
        if acc_root:
            self.txt_acc_status.Text = u"• Desktop Connector detected"
        else:
            self.txt_acc_status.Text = u"⚠ Desktop Connector not detected — \
ACCDocs not found"

        # History
        self._refresh_history()

        # Initial sheet count
        if self.print_sets:
            self._update_sheet_count()

        # Active sheet check
        active = get_active_sheet()
        if not active:
            self.rb_active_sheet.IsEnabled = False
            self.rb_active_sheet.ToolTip   = "No sheet is currently active"

        # Merged name default
        self.txt_merged_name.Text = self.txt_prefix.Text

    # ------------------------------------------------------------------
    def _wire_events(self):
        # Output type — row 1
        self.rb_pdf.Checked          += self._on_output_changed
        self.rb_dwg.Checked          += self._on_output_changed
        self.rb_both.Checked         += self._on_output_changed
        # Output type — row 2 (publish)
        self.rb_pdf_pub.Checked      += self._on_output_changed
        self.rb_dwg_pub.Checked      += self._on_output_changed
        self.rb_both_pub.Checked     += self._on_output_changed

        # ACC browse / history
        self.btn_browse_acc_pdf.Click += self._browse_acc_pdf
        self.btn_browse_acc_dwg.Click += self._browse_acc_dwg
        self.txt_acc_pdf_folder.TextChanged += self._on_acc_pdf_folder_changed
        self.txt_acc_dwg_folder.TextChanged += self._on_acc_dwg_folder_changed
        # Sheet selection
        self.rb_print_set.Checked    += self._on_sheet_sel_changed
        self.rb_active_sheet.Checked += self._on_sheet_sel_changed
        self.rb_all_sheets.Checked   += self._on_sheet_sel_changed
        self.cbo_print_set.SelectionChanged  += self._on_print_set_changed
        self.txt_sheet_filter.TextChanged    += self._on_sheet_filter_changed
        self.btn_select_all.Click            += self._on_select_all
        self.btn_deselect_all.Click          += self._on_deselect_all
        self.dg_sheets.PreviewMouseLeftButtonDown += self._on_dg_sheets_preview_mouse_down

        # File naming
        self.cbo_prev_prefix.SelectionChanged += self._on_prev_prefix
        self.txt_prefix.TextChanged += self._on_prefix_changed

        # PDF folder
        self.btn_browse_pdf.Click   += self._browse_pdf
        self.btn_create_pdf.Click   += self._create_pdf_folder
        self.txt_pdf_folder.TextChanged += self._on_pdf_folder_changed

        # DWG folder
        self.btn_browse_dwg.Click   += self._browse_dwg
        self.btn_create_dwg.Click   += self._create_dwg_folder
        self.txt_dwg_folder.TextChanged += self._on_dwg_folder_changed

        # Merge / merged name
        self.chk_merge.Checked      += self._on_merge_changed
        self.chk_merge.Unchecked    += self._on_merge_changed
        self.txt_merged_name.TextChanged += self._on_merged_name_changed

        # Options
        self.chk_open_folder.Checked   += self._on_open_folder_changed
        self.chk_open_folder.Unchecked += self._on_open_folder_changed

        # Variable per-sheet print setup
        self.cbo_print_setup.SelectionChanged += self._on_print_setup_mode_changed
        self.cbo_assign_setup.SelectionChanged += self._on_assign_setup_changed
        self.btn_apply_setup.Click            += self._on_apply_setup
        self.btn_auto_assign.Click            += self._auto_assign_by_size   # #7

        # #4 Drawn By filter
        self.cbo_drawn_by_filter.SelectionChanged += self._on_drawn_by_filter_changed
        self.btn_clear_filter.Click               += self._on_clear_filter

        # #6 Presets
        self.btn_save_preset.Click += self._save_preset
        self.btn_load_preset.Click += self._load_preset

        # Sheet order
        self.btn_edit_order.Click   += self._open_sheet_order_dialog

        # History
        self.btn_repeat.Click       += self._repeat_run

        # Footer
        self.btn_export.Click       += self._do_export
        self.btn_view_log.Click     += self._view_log
        self.btn_close.Click        += lambda s, e: self.win.Close()

        self.main_tabs.SelectionChanged += self._on_tab_changed

    # ------------------------------------------------------------------
    def _get_mode(self):
        """Return (base_type, publish_pdf, publish_dwg).
        base_type ∈ {'PDF', 'DWG', 'Both'}
        """
        if self.rb_pdf.IsChecked:
            return ("PDF", False, False)
        if self.rb_dwg.IsChecked:
            return ("DWG", False, False)
        if self.rb_both.IsChecked:
            return ("Both", False, False)
        if self.rb_pdf_pub.IsChecked:
            return ("PDF", True, False)
        if self.rb_dwg_pub.IsChecked:
            return ("DWG", False, True)
        if self.rb_both_pub.IsChecked:
            return ("Both", True, True)
        return ("PDF", False, False)

    # ------------------------------------------------------------------
    def _update_ui(self):
        base_type, pub_pdf, pub_dwg = self._get_mode()
        is_publish = pub_pdf or pub_dwg
        is_pdf  = base_type in ("PDF", "Both")
        is_dwg  = base_type in ("DWG", "Both")
        is_ps     = bool(self.rb_print_set.IsChecked)
        is_active = bool(self.rb_active_sheet.IsChecked)
        is_all    = bool(self.rb_all_sheets.IsChecked)
        is_merge  = bool(self.chk_merge.IsChecked)

        self.pnl_pdf_output.Visibility = \
            Visibility.Visible if is_pdf else Visibility.Collapsed
        self.pnl_dwg_output.Visibility = \
            Visibility.Visible if is_dwg else Visibility.Collapsed

        # ComboBox greyed out (not hidden) when not in Print Set mode
        self.cbo_print_set.IsEnabled = is_ps
        self.cbo_print_set.Opacity   = 1.0 if is_ps else 0.4
        # TxtActiveSheetName no longer used (DataGrid shows all modes)
        self.txt_active_sheet.Visibility = Visibility.Collapsed

        # ACC destination panel visibility
        self.pnl_acc_dest.Visibility = \
            Visibility.Visible if is_publish else Visibility.Collapsed
        # Show PDF/DWG rows based on what is being published
        self.pnl_acc_pdf_row.Visibility = \
            Visibility.Visible if pub_pdf else Visibility.Collapsed
        self.pnl_acc_dwg_row.Visibility = \
            Visibility.Visible if pub_dwg else Visibility.Collapsed

        # Export button label + colour (blue = export-only, orange = publish)
        from System.Windows.Media import SolidColorBrush, Color as WMColor
        if is_publish:
            self.btn_export.Content    = u"Export & Publish"
            self.btn_export.Background = SolidColorBrush(WMColor.FromRgb(244, 114, 58))
            self.btn_export.Foreground = SolidColorBrush(WMColor.FromRgb(255, 255, 255))
        else:
            self.btn_export.Content    = "Export Package"
            self.btn_export.Background = SolidColorBrush(WMColor.FromRgb(79, 195, 247))
            self.btn_export.Foreground = SolidColorBrush(WMColor.FromRgb(24, 24, 24))

        # Merge — only available with PDF and multiple sheets
        single_sheet = is_active   # Current Sheet mode = single sheet only
        self.chk_merge.IsEnabled = is_pdf and not single_sheet
        if single_sheet and self.chk_merge.IsChecked:
            self.chk_merge.IsChecked = False

        # Merged name field
        self.pnl_merged_name.Visibility = Visibility.Visible
        self.txt_merged_name.IsEnabled  = bool(is_merge and is_pdf)
        self.txt_merged_name.Opacity    = 1.0 if (is_merge and is_pdf) else 0.4
        self.chk_revision.IsEnabled     = not is_merge

        # View Log visibility
        open_folder = bool(self.chk_open_folder.IsChecked)
        self.btn_view_log.Visibility = \
            Visibility.Collapsed if open_folder else Visibility.Visible

        # Sheet order panel — only for print set
        self.pnl_sheet_order.Visibility = \
            Visibility.Visible if is_ps else Visibility.Collapsed

        # Sheet count
        self._update_sheet_count()

    # ------------------------------------------------------------------
    # Sheet DataGrid helpers
    def _build_sheet_table(self):
        """Rebuild the sheet DataGrid for the active sheet-selection mode."""
        is_ps     = bool(self.rb_print_set.IsChecked)
        is_active = bool(self.rb_active_sheet.IsChecked)
        is_all    = bool(self.rb_all_sheets.IsChecked)

        dt = WpfDataTable()
        dt.Columns.Add("Included",   bool)
        dt.Columns.Add("SheetNo",    str)
        dt.Columns.Add("SheetName",  str)
        dt.Columns.Add("Size",       str)
        dt.Columns.Add("ElemId",     int)   # hidden — used in collect_settings
        dt.Columns.Add("PrintSetup", str)   # per-sheet print setup (Variable mode)
        dt.Columns.Add("PSColor",    str)   # #1 color indicator for Variable mode
        dt.Columns.Add("DrawnBy",    str)   # #4 drawn-by value for filtering
        dt.Columns.Add("Changed",    str)   # #5 "★" if sheet changed since last export

        default_ps = (self.named_print_settings[0][0]
                      if self.named_print_settings else "")

        # #5 — last run date for "changed" detection
        _last_run_dt = None
        if self.cfg.get("runs"):
            try:
                _last_run_dt = datetime.datetime.strptime(
                    self.cfg["runs"][-1]["date"], "%Y-%m-%d %H:%M")
            except Exception:
                pass

        def _get_drawn_by(sh):
            try:
                from Autodesk.Revit.DB import BuiltInParameter as BIP
                p = sh.get_Parameter(BIP.SHEET_DRAWN_BY)
                return (p.AsString() or "").strip() if p else ""
            except Exception:
                return ""

        def _is_changed(sh):
            """Return True if any revision on this sheet is dated after the last export."""
            if _last_run_dt is None:
                return False
            try:
                rev_ids = sh.GetRevisionIds()
                for rid in rev_ids:
                    rev = doc.GetElement(rid)
                    if rev is None:
                        continue
                    rd = (rev.RevisionDate or "").strip()
                    if not rd:
                        continue
                    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
                        try:
                            rev_dt = datetime.datetime.strptime(rd, fmt)
                            if rev_dt > _last_run_dt:
                                return True
                            break
                        except ValueError:
                            continue
            except Exception:
                pass
            return False

        def _add_row(sh, included=True):
            size = get_sheet_size(sh)
            row = dt.NewRow()
            row["Included"]   = included
            row["SheetNo"]    = sh.SheetNumber
            row["SheetName"]  = sh.Name
            row["Size"]       = size
            row["ElemId"]     = sh.Id.IntegerValue
            row["PrintSetup"] = default_ps
            row["PSColor"]    = ""
            row["DrawnBy"]    = _get_drawn_by(sh)
            row["Changed"]    = u"★" if _is_changed(sh) else ""
            dt.Rows.Add(row)

        if is_ps:
            idx = self.cbo_print_set.SelectedIndex
            if idx >= 0 and self.print_sets:
                _, vss = self.print_sets[idx]
                sheets = get_sheets_from_print_set(vss)
                for sh in sheets:
                    _add_row(sh)
                self._sheets = list(sheets)

        elif is_all:
            collector = FilteredElementCollector(doc)\
                .OfClass(ViewSheet)\
                .WhereElementIsNotElementType()
            sheets = sorted(
                [s for s in collector.ToElements() if not s.IsPlaceholder],
                key=lambda s: s.SheetNumber
            )
            for sh in sheets:
                _add_row(sh)
            self._sheets = sheets

        elif is_active:
            active = get_active_sheet()
            if active:
                _add_row(active)
                self._sheets = [active]
            else:
                self._sheets = []

        self._sheet_dt = dt
        dt.ColumnChanged += self._on_sheet_dt_changed
        self.dg_sheets.ItemsSource = dt.DefaultView
        self._populate_drawn_by_filter()  # #4
        self._apply_sheet_filter(reset=True)
        self._refresh_sheet_count()
        self._update_variable_mode()

    def _apply_sheet_filter(self, reset=False):
        if self._sheet_dt is None:
            return
        if reset:
            self.txt_sheet_filter.Text = ""
            self.cbo_drawn_by_filter.SelectedIndex = 0
        q = (self.txt_sheet_filter.Text or "").strip().upper()
        # #4 Drawn By filter
        drawn_by_filter = ""
        try:
            sel = self.cbo_drawn_by_filter.SelectedItem
            if sel and str(sel) != "(All)":
                drawn_by_filter = str(sel).replace("'", "''")
        except Exception:
            pass

        parts = []
        if q:
            safe = q.replace("'", "''")
            parts.append(
                "(SheetNo LIKE '%{0}%' OR SheetName LIKE '%{0}%' OR Size LIKE '%{0}%')"
                .format(safe))
        if drawn_by_filter:
            parts.append("DrawnBy = '{}'".format(drawn_by_filter))

        self._sheet_dt.DefaultView.RowFilter = " AND ".join(parts)
        self._refresh_sheet_count()

    def _populate_drawn_by_filter(self):
        """Fill the Drawn By filter combobox with unique values from the current table."""
        try:
            self.cbo_drawn_by_filter.Items.Clear()
            self.cbo_drawn_by_filter.Items.Add("(All)")
            if self._sheet_dt is not None:
                seen = set()
                for row in self._sheet_dt.Rows:
                    val = (row["DrawnBy"] or "").strip()
                    if val and val not in seen:
                        seen.add(val)
                        self.cbo_drawn_by_filter.Items.Add(val)
            self.cbo_drawn_by_filter.SelectedIndex = 0
        except Exception:
            pass

    def _on_drawn_by_filter_changed(self, s, e):
        self._apply_sheet_filter()

    def _on_clear_filter(self, s, e):
        try:
            self.cbo_drawn_by_filter.SelectedIndex = 0
        except Exception:
            pass

    # ------------------------------------------------------------------
    # #7 Auto-assign print setup by sheet size
    def _auto_assign_by_size(self, s, e):
        """Map each sheet's Size value to the best-matching named print setup."""
        if self._sheet_dt is None or not self.named_print_settings:
            return
        setup_names = [n for n, _ in self.named_print_settings]
        for row in self._sheet_dt.Rows:
            size = (row["Size"] or "").strip().upper()
            best = ""
            # Look for a print setup whose name contains the size string
            for name in setup_names:
                if size and size in name.upper():
                    best = name
                    break
            if not best and setup_names:
                best = setup_names[0]
            row["PrintSetup"] = best
            row["PSColor"]    = self._get_setup_color(best)

    # ------------------------------------------------------------------
    # #6 Named presets
    def _refresh_presets(self):
        try:
            self.cbo_presets.Items.Clear()
            self.cbo_presets.Items.Add("(No preset)")
            for name in sorted((self.cfg.get("presets") or {}).keys()):
                self.cbo_presets.Items.Add(name)
            self.cbo_presets.SelectedIndex = 0
        except Exception:
            pass

    def _save_preset(self, s, e):
        try:
            name = forms.ask_for_string(
                prompt="Enter a name for this preset",
                title="Save Preset",
                default="My Preset")
            if not name:
                return
            preset = {
                "pdf_folder":  self.txt_pdf_folder.Text.strip(),
                "dwg_folder":  self.txt_dwg_folder.Text.strip(),
                "pdf_engine":  "native",
                "merge_pdf":   bool(self.chk_merge.IsChecked),
                "include_revision": bool(self.chk_revision.IsChecked),
                "open_folder": bool(self.chk_open_folder.IsChecked),
                "output_type_key": {
                    (True,  False, False): "PDF",
                    (False, True,  False): "DWG",
                    (False, False, True):  "Both",
                    (False, False, False): "PDF",
                }.get((bool(self.rb_pdf.IsChecked),
                       bool(self.rb_dwg.IsChecked),
                       bool(self.rb_both.IsChecked)), "PDF"),
            }
            self.cfg.setdefault("presets", {})[name] = preset
            save_config(self.cfg)
            self._refresh_presets()
            # Select the newly saved preset
            try:
                for i in range(self.cbo_presets.Items.Count):
                    if str(self.cbo_presets.Items[i]) == name:
                        self.cbo_presets.SelectedIndex = i
                        break
            except Exception:
                pass
            self._set_status(u"Preset '{}' saved.".format(name))
        except Exception as ex:
            self._set_status(u"Could not save preset: {}".format(str(ex)))

    def _load_preset(self, s, e):
        try:
            name = str(self.cbo_presets.SelectedItem or "")
            if not name or name == "(No preset)":
                return
            preset = (self.cfg.get("presets") or {}).get(name)
            if not preset:
                return
            if preset.get("pdf_folder"):
                self.txt_pdf_folder.Text = preset["pdf_folder"]
            if preset.get("dwg_folder"):
                self.txt_dwg_folder.Text = preset["dwg_folder"]
            if "merge_pdf" in preset:
                self.chk_merge.IsChecked = preset["merge_pdf"]
            if "include_revision" in preset:
                self.chk_revision.IsChecked = preset["include_revision"]
            if "open_folder" in preset:
                self.chk_open_folder.IsChecked = preset["open_folder"]
            ot = preset.get("output_type_key", "PDF")
            self.rb_pdf.IsChecked  = (ot == "PDF")
            self.rb_dwg.IsChecked  = (ot == "DWG")
            self.rb_both.IsChecked = (ot == "Both")
            self._update_ui()
            self._set_status(u"Preset '{}' loaded.".format(name))
        except Exception as ex:
            self._set_status(u"Could not load preset: {}".format(str(ex)))

    # ------------------------------------------------------------------
    # #2 Pre-export validation
    def _pre_export_validate(self, settings):
        """Return a list of warning strings. Empty list = all clear."""
        warnings = []
        # Check named print setups exist in model (Variable mode)
        if settings.get("variable_print_setup"):
            used_names = set(settings.get("sheet_print_setups", {}).values())
            known_names = {n for n, _ in self.named_print_settings}
            for name in sorted(used_names):
                if name and name not in known_names:
                    warnings.append(
                        u"Print setup '{}' not found in model.".format(name))
        elif settings.get("print_setup_name"):
            ps_name = settings["print_setup_name"]
            if ps_name not in ("", "(Use defaults)"):
                known_names = {n for n, _ in self.named_print_settings}
                if ps_name not in known_names:
                    warnings.append(
                        u"Print setup '{}' not found in model.".format(ps_name))
        # Check output folder is writable — only for active output types
        out_type = settings.get("output_type", "PDF")
        pub_pdf  = settings.get("publish_pdf", False)
        pub_dwg  = settings.get("publish_dwg", False)
        pdf_active = out_type in ("PDF", "Both") or pub_pdf
        dwg_active = out_type in ("DWG", "Both") or pub_dwg
        for folder, label, active in [
                (settings.get("pdf_folder"), "PDF", pdf_active),
                (settings.get("dwg_folder"), "DWG", dwg_active)]:
            if folder and active:
                try:
                    import tempfile
                    test = os.path.join(folder, ".write_test_{}".format(
                        datetime.datetime.now().strftime("%H%M%S")))
                    with open(test, "w") as f:
                        f.write("x")
                    os.remove(test)
                except Exception as ex:
                    warnings.append(
                        u"{} folder not writable: {} ({})".format(
                            label, folder, str(ex)))
        # Warn on duplicate sheet numbers
        sheet_nos = [sh.SheetNumber for sh in settings.get("sheets", [])]
        seen = {}
        for no in sheet_nos:
            seen[no] = seen.get(no, 0) + 1
        dupes = [no for no, cnt in seen.items() if cnt > 1]
        if dupes:
            warnings.append(
                u"Duplicate sheet numbers detected: {}".format(", ".join(sorted(dupes))))
        return warnings

    # ------------------------------------------------------------------
    # #9 Export summary CSV
    def _write_export_csv(self, settings, pdf_paths, dwg_paths, folder):
        """Write a CSV export summary to the output folder."""
        try:
            ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fn  = u"ExportSummary_{}.csv".format(ts)
            fp  = os.path.join(folder, fn)
            rows = []
            for path in pdf_paths:
                try:
                    size_kb = os.path.getsize(path) // 1024
                except Exception:
                    size_kb = 0
                rows.append((os.path.basename(path), "PDF",
                              u"{} KB".format(size_kb), ts, path))
            for path in dwg_paths:
                try:
                    size_kb = os.path.getsize(path) // 1024
                except Exception:
                    size_kb = 0
                rows.append((os.path.basename(path), "DWG",
                              u"{} KB".format(size_kb), ts, path))
            with open(fp, "w") as f:
                f.write(u"File,Type,Size,Exported,Full Path\n")
                for r in rows:
                    line = u",".join(
                        u'"{}"'.format(c.replace('"', '""')) for c in r)
                    f.write(line + u"\n")
        except Exception:
            pass

    def _refresh_sheet_count(self):
        if self._sheet_dt is None:
            self.txt_sheet_count.Text = ""
            return
        total    = self._sheet_dt.Rows.Count
        checked  = sum(1 for r in self._sheet_dt.Rows if bool(r["Included"]))
        visible  = len(self._sheet_dt.DefaultView)
        if visible < total:
            label = u"{}/{} selected  ({} visible)".format(checked, total, visible)
        else:
            label = u"{}/{} selected".format(checked, total)
        self.txt_sheet_count.Text = label

    def _on_sheet_dt_changed(self, s, e):
        if e.Column is not None and e.Column.ColumnName == "Included":
            self._refresh_sheet_count()

    def _update_sheet_count(self):
        """Rebuild the sheet table (called on mode or print-set change)."""
        self._build_sheet_table()

    def _update_sheet_count_display(self):
        self._refresh_sheet_count()

    # ------------------------------------------------------------------
    # Event handlers — output type / sheet sel / merge
    def _on_output_changed(self, s, e):    self._update_ui()
    def _on_sheet_sel_changed(self, s, e): self._update_ui()
    def _on_merge_changed(self, s, e):     self._update_ui()
    def _on_tab_changed(self, s, e):       self.txt_status.Text = ""

    def _on_print_set_changed(self, s, e):
        if bool(self.rb_print_set.IsChecked):
            self._build_sheet_table()

    # ------------------------------------------------------------------
    # Variable per-sheet print setup
    def _is_variable_mode(self):
        sel = self.cbo_print_setup.SelectedItem
        name = getattr(sel, "Content", None) or str(sel or "")
        return name == "Variable (per sheet)"

    def _build_setup_color_map(self):
        """Build/refresh a mapping of print-setup name → hex color string."""
        self._setup_color_map = {}
        for i, (name, _) in enumerate(self.named_print_settings):
            self._setup_color_map[name] = SETUP_COLORS[i % len(SETUP_COLORS)]

    def _get_setup_color(self, name):
        cm = getattr(self, "_setup_color_map", {})
        return cm.get(name or "", "")

    def _build_setup_tooltip(self, elem_id):
        """Return a short description string for a named print setup element."""
        try:
            ps = doc.GetElement(DB.ElementId(elem_id))
            if ps is None:
                return ""
            pp = ps.GetParameters()
            if pp is None:
                return ""
            # Try to read paper size name and orientation
            size_name = ""
            orientation = ""
            try:
                size_name = pp.PaperSize.Name if pp.PaperSize else ""
            except Exception:
                pass
            try:
                orientation = str(pp.PageOrientation)
            except Exception:
                pass
            parts = [p for p in [size_name, orientation] if p]
            return "  ".join(parts) if parts else ""
        except Exception:
            return ""

    def _update_variable_mode(self):
        is_var = self._is_variable_mode()
        # Show/hide the Print Setup column (index 5 in DgSheets: 0=chk,1=color,2=★,3=No,4=Name,5=PrintSetup,6=Size)
        try:
            col = self.dg_sheets.Columns[self._col_print_setup_idx]
            col.Visibility = Visibility.Visible if is_var else Visibility.Collapsed
        except Exception:
            pass
        # #1 Show/hide the PSColor strip column (index 1)
        try:
            col_color = self.dg_sheets.Columns[self._col_color_idx]
            col_color.Visibility = Visibility.Visible if is_var else Visibility.Collapsed
        except Exception:
            pass
        # Show/hide the assign panel
        self.pnl_variable_assign.Visibility = \
            Visibility.Visible if is_var else Visibility.Collapsed
        if is_var:
            self._build_setup_color_map()
            # Populate assign combobox with named setups + #8 tooltips
            self.cbo_assign_setup.Items.Clear()
            from System.Windows.Controls import ComboBoxItem
            for name, eid in self.named_print_settings:
                item = ComboBoxItem()
                item.Content = name
                tip = self._build_setup_tooltip(eid)
                if tip:
                    item.ToolTip = tip
                self.cbo_assign_setup.Items.Add(item)
            if self.named_print_settings:
                self.cbo_assign_setup.SelectedIndex = 0
            # Sort by Size then SheetNo so same-size sheets are grouped
            if self._sheet_dt is not None:
                self._sheet_dt.DefaultView.Sort = "Size ASC, SheetNo ASC"
            # Apply initial colors
            self._refresh_row_colors()

    def _on_print_setup_mode_changed(self, s, e):
        self._update_variable_mode()

    def _on_assign_setup_changed(self, s, e):
        """Auto-apply the newly selected print setup to highlighted rows immediately."""
        self._on_apply_setup(s, e)

    def _on_apply_setup(self, s, e):
        """Apply selected print setup from CboAssignSetup to all highlighted rows."""
        selected_item = self.cbo_assign_setup.SelectedItem
        if selected_item is None or self._sheet_dt is None:
            return
        # Handle ComboBoxItem (used when tooltips are added) or plain string
        setup = getattr(selected_item, "Content", None) or str(selected_item)
        if not setup:
            return
        color = self._get_setup_color(setup)
        selected = list(self.dg_sheets.SelectedItems)
        if not selected:
            # Nothing selected → apply to all visible rows
            view = self._sheet_dt.DefaultView
            selected = [view[i] for i in range(len(view))]
        for row_view in selected:
            try:
                row_view["PrintSetup"] = setup
                row_view["PSColor"]    = color   # #1 update color strip
            except Exception:
                pass

    def _refresh_row_colors(self):
        """Re-sync PSColor for all DataTable rows based on current color map."""
        if self._sheet_dt is None:
            return
        for row in self._sheet_dt.Rows:
            name = row["PrintSetup"] or ""
            row["PSColor"] = self._get_setup_color(name)

    def _on_sheet_filter_changed(self, s, e):
        self._apply_sheet_filter()

    def _on_select_all(self, s, e):
        if self._sheet_dt is None:
            return
        for row in self._sheet_dt.DefaultView:
            row["Included"] = True
        self._refresh_sheet_count()

    def _on_deselect_all(self, s, e):
        if self._sheet_dt is None:
            return
        for row in self._sheet_dt.DefaultView:
            row["Included"] = False
        self._refresh_sheet_count()

    def _on_dg_sheets_preview_mouse_down(self, sender, e):
        from System.Windows.Controls import DataGridCell, DataGridCheckBoxColumn
        from System.Windows.Media import VisualTreeHelper as VTH
        # Walk up to the DataGridCell under the pointer
        el = e.OriginalSource
        for _ in range(20):
            if isinstance(el, DataGridCell):
                break
            parent = VTH.GetParent(el)
            if parent is None:
                return
            el = parent
        if not isinstance(el, DataGridCell):
            return
        # Only handle the checkbox column (first column)
        if not isinstance(el.Column, DataGridCheckBoxColumn):
            return
        # Identify row index in the filtered DefaultView
        row_view = el.DataContext
        view = self._sheet_dt.DefaultView
        cur_idx = -1
        for i in range(len(view)):
            if view[i] is row_view:
                cur_idx = i
                break
        if cur_idx < 0:
            return
        # Shift+click: range-set all rows between anchor and current
        if (Keyboard.Modifiers & ModifierKeys.Shift) and self._last_clicked_row >= 0:
            lo = min(self._last_clicked_row, cur_idx)
            hi = max(self._last_clicked_row, cur_idx)
            for i in range(lo, hi + 1):
                view[i]["Included"] = self._last_clicked_val
            e.Handled = True
            self._last_clicked_row = cur_idx
            return
        # Normal single click — toggle immediately and handle event so
        # DataGrid doesn't require a second click to commit the change
        new_val = not bool(row_view["Included"])
        row_view["Included"] = new_val
        self._last_clicked_val = new_val
        self._last_clicked_row = cur_idx
        e.Handled = True

    def _on_prefix_changed(self, s, e):
        pn = self.txt_prefix.Text.strip()
        self.win.Title = u"Export & Publish  —  {}".format(pn) \
            if pn else u"Export & Publish"
        txt = self.txt_prefix.Text
        if '/' in txt:
            pos = self.txt_prefix.CaretIndex
            self.txt_prefix.Text = txt.replace('/', '_')
            self.txt_prefix.CaretIndex = pos
        self.txt_merged_name.Text = self.txt_prefix.Text

    # Previous history dropdowns
    def _on_prev_prefix(self, s, e):
        if self.cbo_prev_prefix.SelectedIndex > 0:
            val = (self.cbo_prev_prefix.SelectedItem or '').replace('/', '_')
            self.txt_prefix.Text = val


    # Folder text changed
    def _on_pdf_folder_changed(self, s, e):
        self._check_folder_state(
            self.txt_pdf_folder.Text.strip(),
            self.btn_create_pdf, self.txt_pdf_status)

    def _on_dwg_folder_changed(self, s, e):
        self._check_folder_state(
            self.txt_dwg_folder.Text.strip(),
            self.btn_create_dwg, self.txt_dwg_status)

    def _on_acc_pdf_folder_changed(self, s, e):
        self._check_acc_folder(
            self.txt_acc_pdf_folder.Text.strip(),
            self.txt_acc_pdf_status)

    def _on_acc_dwg_folder_changed(self, s, e):
        self._check_acc_folder(
            self.txt_acc_dwg_folder.Text.strip(),
            self.txt_acc_dwg_status)

    def _on_merged_name_changed(self, s, e):
        txt = self.txt_merged_name.Text
        if '/' in txt:
            pos = self.txt_merged_name.CaretIndex
            self.txt_merged_name.Text = txt.replace('/', '_')
            self.txt_merged_name.CaretIndex = pos

    def _on_open_folder_changed(self, s, e):
        open_folder = bool(self.chk_open_folder.IsChecked)
        self.btn_view_log.Visibility = \
            Visibility.Collapsed if open_folder else Visibility.Visible

    # ------------------------------------------------------------------
    # Browse helpers
    def _browse_pdf(self, s, e):
        folder = self._folder_dialog(
            "Select PDF output folder",
            initial=self.txt_pdf_folder.Text.strip())
        if folder:
            self.txt_pdf_folder.Text = folder
            self.txt_pdf_folder.CaretIndex = len(folder)
            self._check_folder_state(folder, self.btn_create_pdf, self.txt_pdf_status)

    def _browse_dwg(self, s, e):
        folder = self._folder_dialog(
            "Select DWG output folder",
            initial=self.txt_dwg_folder.Text.strip())
        if folder:
            self.txt_dwg_folder.Text = folder
            self.txt_dwg_folder.CaretIndex = len(folder)
            self._check_folder_state(folder, self.btn_create_dwg, self.txt_dwg_status)

    def _browse_acc_pdf(self, s, e):
        folder = self._folder_dialog(
            "Select ACC / Forma PDF destination folder",
            initial=self.txt_acc_pdf_folder.Text.strip() or find_acc_docs_root())
        if folder:
            self.txt_acc_pdf_folder.Text = folder
            self._check_acc_folder(folder, self.txt_acc_pdf_status)

    def _browse_acc_dwg(self, s, e):
        folder = self._folder_dialog(
            "Select ACC / Forma DWG destination folder",
            initial=self.txt_acc_dwg_folder.Text.strip() or find_acc_docs_root())
        if folder:
            self.txt_acc_dwg_folder.Text = folder
            self._check_acc_folder(folder, self.txt_acc_dwg_status)

    def _folder_dialog(self, description="Select folder", initial=None):
        from System.Windows.Forms import FolderBrowserDialog, DialogResult
        import System
        # Save and restore the process working directory — FolderBrowserDialog
        # updates it as a side effect, which causes Word and other apps to open
        # their file dialogs in the same folder.
        prev_dir = System.Environment.CurrentDirectory
        try:
            dlg = FolderBrowserDialog()
            dlg.Description = description
            if initial and Directory.Exists(initial):
                dlg.SelectedPath = initial
            result = dlg.SelectedPath if dlg.ShowDialog() == DialogResult.OK else None
        finally:
            try:
                System.Environment.CurrentDirectory = prev_dir
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # Folder state checks
    def _check_folder_state(self, path, create_btn, status_lbl):
        from System.Windows.Media import SolidColorBrush, Color as WMColor
        if not path:
            create_btn.IsEnabled  = False
            status_lbl.Visibility = Visibility.Collapsed
            return
        if Directory.Exists(path):
            create_btn.IsEnabled  = False
            create_btn.Content    = "Create"
            status_lbl.Text       = u"✓  Folder exists"
            status_lbl.Foreground = SolidColorBrush(WMColor.FromRgb(77, 182, 172))
            status_lbl.Visibility = Visibility.Visible
        else:
            create_btn.IsEnabled  = True
            create_btn.Content    = "Create"
            status_lbl.Text       = u"⚠  Folder does not exist"
            status_lbl.Foreground = SolidColorBrush(WMColor.FromRgb(255, 183, 77))
            status_lbl.Visibility = Visibility.Visible

    def _check_acc_folder(self, path, status_lbl):
        from System.Windows.Media import SolidColorBrush, Color as WMColor
        if not path:
            status_lbl.Visibility = Visibility.Collapsed
            return
        if acc_folder_accessible(path):
            status_lbl.Text       = u"✓  ACC folder accessible"
            status_lbl.Foreground = SolidColorBrush(WMColor.FromRgb(77, 182, 172))
        else:
            status_lbl.Text       = u"⚠  Folder not accessible via Desktop Connector"
            status_lbl.Foreground = SolidColorBrush(WMColor.FromRgb(255, 107, 107))
        status_lbl.Visibility = Visibility.Visible

    # ------------------------------------------------------------------
    # Create folder helpers
    def _create_pdf_folder(self, s, e):
        self._do_create_folder(
            self.txt_pdf_folder.Text.strip(),
            self.btn_create_pdf, self.txt_pdf_status)

    def _create_dwg_folder(self, s, e):
        self._do_create_folder(
            self.txt_dwg_folder.Text.strip(),
            self.btn_create_dwg, self.txt_dwg_status)

    def _do_create_folder(self, path, create_btn, status_lbl):
        from System.Windows.Media import SolidColorBrush, Color as WMColor
        if not path: return
        try:
            Directory.CreateDirectory(path)
            create_btn.IsEnabled  = False
            create_btn.Content    = u"✓ Created"
            status_lbl.Text       = u"✓  Folder created successfully"
            status_lbl.Foreground = SolidColorBrush(WMColor.FromRgb(77, 182, 172))
            status_lbl.Visibility = Visibility.Visible
        except Exception as ex:
            status_lbl.Text       = u"✗  Could not create: {}".format(str(ex))
            status_lbl.Foreground = SolidColorBrush(WMColor.FromRgb(255, 107, 107))
            status_lbl.Visibility = Visibility.Visible

    # ------------------------------------------------------------------
    def _open_sheet_order_dialog(self, s, e):
        if not self._sheets:
            forms.alert("No sheets loaded. Select a print set first.",
                        title="Edit Print Order")
            return
        dlg = SheetOrderDialog(self._sheets)
        dlg.show()
        if dlg.result is not None:
            self._sheets = dlg.result
            self._update_sheet_count_display()

    def _view_log(self, s, e):
        if self._last_log and File.Exists(self._last_log):
            subprocess.Popen(["notepad", self._last_log])

    # ------------------------------------------------------------------
    def _refresh_history(self):
        self.dg_history.Items.Clear()
        for run in reversed(self.cfg["runs"][-50:]):
            self.dg_history.Items.Add(_HistoryRow(run))

    def _repeat_run(self, s, e):
        row = self.dg_history.SelectedItem
        if not row: return
        run = row.raw
        self.txt_prefix.Text        = run.get("prefix", "")
        self.chk_revision.IsChecked = run.get("include_revision", False)
        self.chk_merge.IsChecked    = run.get("merge_pdf", False)
        ps_name = run.get("print_setup_name", "")
        if ps_name == "Variable (per sheet)":
            self.cbo_print_setup.SelectedIndex = 0
        else:
            for i, (n, _) in enumerate(self.named_print_settings):
                if n == ps_name:
                    self.cbo_print_setup.SelectedIndex = i + 2  # +2: Variable + (Use defaults)
                break
        ot = run.get("output_type", "PDF")
        self.rb_pdf.IsChecked      = (ot == "PDF")
        self.rb_dwg.IsChecked      = (ot == "DWG")
        self.rb_both.IsChecked     = (ot == "Both")
        self.rb_pdf_pub.IsChecked  = (ot == "PDFPub")
        self.rb_dwg_pub.IsChecked  = (ot == "DWGPub")
        self.rb_both_pub.IsChecked = (ot == "BothPub")

        if run.get("pdf_folder"):
            self.txt_pdf_folder.Text = run["pdf_folder"]
        if run.get("dwg_folder"):
            self.txt_dwg_folder.Text = run["dwg_folder"]
        if run.get("acc_pdf_folder"):
            self.txt_acc_pdf_folder.Text = run["acc_pdf_folder"]
        if run.get("acc_dwg_folder"):
            self.txt_acc_dwg_folder.Text = run["acc_dwg_folder"]

        ps_name = run.get("print_set", "")
        for i, (n, _) in enumerate(self.print_sets):
            if n == ps_name:
                self.cbo_print_set.SelectedIndex = i
                break

        self.main_tabs.SelectedIndex = 0
        self._update_ui()

    # ------------------------------------------------------------------
    def _collect_settings(self):
        prefix = self.txt_prefix.Text.strip()
        if not prefix:
            self.txt_status.Text = "Enter a prefix."
            return None

        base_type, pub_pdf, pub_dwg = self._get_mode()

        # Sheets
        use_active = bool(self.rb_active_sheet.IsChecked)
        if use_active:
            active = get_active_sheet()
            if not active:
                self.txt_status.Text = "No active sheet found."
                return None
            sheets         = [active]
            print_set_name = "Active Sheet"
        else:
            ps_idx = self.cbo_print_set.SelectedIndex
            if ps_idx < 0 or not self.print_sets:
                self.txt_status.Text = "Select a print set."
                return None
            print_set_name, _ = self.print_sets[ps_idx]
            if self._sheet_dt is not None and self._sheet_dt.Rows.Count > 0:
                sheets = []
                for row in self._sheet_dt.Rows:
                    if bool(row["Included"]):
                        elem_id = DB.ElementId(int(row["ElemId"]))
                        sh = doc.GetElement(elem_id)
                        if sh is not None:
                            sheets.append(sh)
            else:
                sheets = list(self._sheets) if self._sheets else \
                         get_sheets_from_print_set(self.print_sets[ps_idx][1])

        if not sheets:
            self.txt_status.Text = "No sheets found."
            return None

        pdf_folder = self.txt_pdf_folder.Text.strip()
        dwg_folder = self.txt_dwg_folder.Text.strip()

        if base_type in ("PDF", "Both") and not pdf_folder:
            self.txt_status.Text = "Set a PDF output folder."
            return None
        if base_type in ("DWG", "Both") and not dwg_folder:
            self.txt_status.Text = "Set a DWG output folder."
            return None

        # Validate local folders accessible
        for folder, label in [(pdf_folder, "PDF"), (dwg_folder, "DWG")]:
            if folder:
                drive = os.path.splitdrive(folder)[0]
                if drive and not os.path.exists(drive + "\\"):
                    self.txt_status.Text = \
                        "{} drive not accessible: {}".format(label, drive)
                    return None

        # Validate ACC folders when publishing
        acc_pdf_folder = self.txt_acc_pdf_folder.Text.strip() if pub_pdf else ""
        acc_dwg_folder = self.txt_acc_dwg_folder.Text.strip() if pub_dwg else ""

        if pub_pdf and not acc_pdf_folder:
            self.txt_status.Text = "Set an ACC PDF destination folder."
            return None
        if pub_pdf and not acc_folder_accessible(acc_pdf_folder):
            self.txt_status.Text = \
                u"ACC PDF folder not accessible: {}".format(acc_pdf_folder)
            return None
        if pub_dwg and not acc_dwg_folder:
            self.txt_status.Text = "Set an ACC DWG destination folder."
            return None
        if pub_dwg and not acc_folder_accessible(acc_dwg_folder):
            self.txt_status.Text = \
                u"ACC DWG folder not accessible: {}".format(acc_dwg_folder)
            return None

        acc_overwrite = bool(self.rb_acc_overwrite.IsChecked)

        dwg_setup_id   = None
        dwg_setup_name = None
        dwg_idx = self.cbo_dwg_setup.SelectedIndex
        if dwg_idx > 0 and self.dwg_setups:
            dwg_setup_name, dwg_setup_id = self.dwg_setups[dwg_idx - 1]

        merged_name = self.txt_merged_name.Text.strip() or prefix

        # output_type key stored in config (includes publish variants)
        out_key_map = {
            ("PDF",  False, False): "PDF",
            ("DWG",  False, False): "DWG",
            ("Both", False, False): "Both",
            ("PDF",  True,  False): "PDFPub",
            ("DWG",  False, True):  "DWGPub",
            ("Both", True,  True):  "BothPub",
        }
        out_key = out_key_map.get((base_type, pub_pdf, pub_dwg), "PDF")

        return {
            "prefix":           prefix,
            "print_set_name":   print_set_name,
            "sheets":           sheets,
            "output_type":      base_type,   # "PDF"/"DWG"/"Both" — used by exporters
            "output_type_key":  out_key,     # saved to config
            "publish_pdf":      pub_pdf,
            "publish_dwg":      pub_dwg,
            "acc_pdf_folder":   acc_pdf_folder,
            "acc_dwg_folder":   acc_dwg_folder,
            "acc_overwrite":    acc_overwrite,
            "print_setup_name": "" if self._is_variable_mode()
                                 else (getattr(self.cbo_print_setup.SelectedItem, "Content", None)
                                       or str(self.cbo_print_setup.SelectedItem or "")),
            "variable_print_setup": self._is_variable_mode(),
            "sheet_print_setups":   self._collect_variable_setups(),
            "include_revision": bool(self.chk_revision.IsChecked),
            "merge_pdf":        bool(self.chk_merge.IsChecked),
            "merged_name":      merged_name,
            "pdf_engine":       "native",
            "pdf_folder":       pdf_folder,
            "dwg_folder":       dwg_folder,
            "dwg_setup_id":     dwg_setup_id,
            "dwg_setup_name":   dwg_setup_name,
            "open_folder":      bool(self.chk_open_folder.IsChecked),
            "overwrite_prompt": bool(self.chk_overwrite.IsChecked),
        }

    def _collect_variable_setups(self):
        """Return {elem_id_int: setup_name} for all included rows (Variable mode)."""
        if not self._is_variable_mode() or self._sheet_dt is None:
            return {}
        default_ps = (self.named_print_settings[0][0]
                      if self.named_print_settings else "")
        result = {}
        for row in self._sheet_dt.Rows:
            if bool(row["Included"]):
                eid = int(row["ElemId"])
                ps  = row["PrintSetup"] or default_ps
                result[eid] = ps
        return result

    # ------------------------------------------------------------------
    def _set_status(self, text):
        self.txt_status.Text = text
        _do_events()

    def _set_progress(self, value, maximum=100):
        self.prg_export.Maximum    = maximum
        self.prg_export.Value      = value
        self.prg_export.Visibility = Visibility.Visible
        _do_events()

    def _hide_progress(self):
        self.prg_export.Visibility = Visibility.Collapsed
        self.prg_export.Value      = 0

    def _make_progress_cb(self, offset, total_phases):
        def cb(done, total, sheet_no):
            if total == 0: return
            phase_pct = float(done) / total * 100.0
            overall   = (offset + phase_pct / total_phases)
            label = u"Exporting sheet {} of {}{}...".format(
                done + 1, total,
                u" ({})".format(sheet_no) if sheet_no else u""
            ) if done < total else u""
            self._set_status(label)
            self._set_progress(overall, 100)
        return cb

    # ------------------------------------------------------------------
    def _do_export(self, s, e):
        settings = self._collect_settings()
        if not settings: return

        # #2 Pre-export validation
        warnings = self._pre_export_validate(settings)
        if warnings:
            msg = u"\n".join(u"• " + w for w in warnings)
            from pyrevit import forms as _frm
            if not _frm.alert(
                    u"Export warnings:\n\n{}\n\nContinue anyway?".format(msg),
                    title="Export Warnings",
                    ok=False,
                    yes=True,
                    no=True):
                return

        sheets    = settings["sheets"]
        out_type  = settings["output_type"]
        pub_pdf   = settings["publish_pdf"]
        pub_dwg   = settings["publish_dwg"]
        is_publish= pub_pdf or pub_dwg

        self.btn_export.IsEnabled = False
        self.btn_export.Content   = u"Exporting..."
        log_lines = []
        log_lines.append(u"Sheets in print set '{}': {}".format(
            settings["print_set_name"], len(sheets)))
        log_lines.append(u"")

        pdf_paths   = []
        merged_path = None
        last_folder = None
        phases = 2 if out_type == "Both" else 1

        # ---- PDF ----
        if out_type in ("PDF", "Both"):
            log_lines.append(u"PDF Exports:")
            cb = self._make_progress_cb(0, phases)
            pdf_results = export_pdfs(sheets, settings, log_lines, cb)
            pdf_paths   = [r[1] for r in pdf_results if r[2]]
            last_folder = settings["pdf_folder"]
            log_lines.append(u"")

            if settings["merge_pdf"] and len(pdf_paths) > 1:
                log_lines.append(u"Merging PDFs:")
                merged_fname = re.sub(INVALID_CHARS, "_", settings["merged_name"])
                merged_check = os.path.join(
                    settings["pdf_folder"], merged_fname + ".pdf")
                do_merge = True
                if File.Exists(merged_check) and settings.get("overwrite_prompt"):
                    from System.Windows import MessageBox, MessageBoxButton, \
                        MessageBoxResult, MessageBoxImage
                    res = MessageBox.Show(
                        u"{}.pdf already exists.\nOverwrite?".format(merged_fname),
                        "File Exists",
                        MessageBoxButton.YesNo,
                        MessageBoxImage.Question
                    )
                    do_merge = (res == MessageBoxResult.Yes)
                if do_merge:
                    self._set_status(u"Merging PDFs...")
                    merged_path = merge_pdfs(
                        pdf_paths,
                        settings["pdf_folder"],
                        merged_fname,
                        log_lines
                    )
                    if merged_path:
                        for p in pdf_paths:
                            try:
                                if File.Exists(p):
                                    File.Delete(p)
                            except Exception:
                                pass
                        log_lines.append(
                            u"  Individual PDFs removed — merged file retained.")
                        pdf_paths = [merged_path]   # publish merged only
                log_lines.append(u"")

        # ---- DWG ----
        dwg_paths = []
        if out_type in ("DWG", "Both"):
            log_lines.append(u"DWG Exports:")
            cb = self._make_progress_cb(100 / phases if phases == 2 else 0, phases)
            dwg_results = export_dwgs(sheets, settings, log_lines, cb)
            # export_dwgs returns list of (sheet, path, ok)
            dwg_paths   = [r[1] for r in dwg_results if r[2]]
            last_folder = settings["dwg_folder"]
            log_lines.append(u"")

        # ---- Publish to ACC ----
        published = False
        if is_publish:
            self._set_status(u"Publishing to ACC / Forma...")
            log_lines.append(u"ACC / Forma Publish:")
            overwrite = settings["acc_overwrite"]

            if pub_pdf and pdf_paths:
                acc_pdf = settings["acc_pdf_folder"]
                log_lines.append(u"  PDF → {}".format(acc_pdf))
                publish_files(pdf_paths, acc_pdf, overwrite, log_lines)
                published = True

            if pub_dwg and dwg_paths:
                acc_dwg = settings["acc_dwg_folder"]
                log_lines.append(u"  DWG → {}".format(acc_dwg))
                publish_files(dwg_paths, acc_dwg, overwrite, log_lines)
                published = True

            log_lines.append(u"")

        # ---- #9 Export summary CSV ----
        if last_folder and (pdf_paths or dwg_paths):
            self._write_export_csv(settings, pdf_paths, dwg_paths, last_folder)

        # ---- Log ----
        if last_folder:
            log_path = write_log(settings, log_lines, last_folder)
            if out_type == "Both":
                write_log(settings, log_lines, settings["pdf_folder"])
            self._last_log = log_path
            self.btn_view_log.IsEnabled = True

        # ---- Save config ----
        cfg = self.cfg
        add_unique(cfg["previous_prefixes"],    settings["prefix"])
        if settings["pdf_folder"]:
            add_unique(cfg["previous_pdf_folders"], settings["pdf_folder"])
        if settings["dwg_folder"]:
            add_unique(cfg["previous_dwg_folders"], settings["dwg_folder"])
        if settings["acc_pdf_folder"]:
            add_unique(cfg.setdefault("previous_acc_pdf_folders", []),
                       settings["acc_pdf_folder"])
            cfg["last_acc_pdf_folder"] = settings["acc_pdf_folder"]
        if settings["acc_dwg_folder"]:
            add_unique(cfg.setdefault("previous_acc_dwg_folders", []),
                       settings["acc_dwg_folder"])
            cfg["last_acc_dwg_folder"] = settings["acc_dwg_folder"]

        cfg["last_print_set"]       = settings["print_set_name"]
        cfg["last_output_type"]     = settings["output_type_key"]
        cfg["last_print_setup"]     = ("Variable (per sheet)"
                                         if settings.get("variable_print_setup")
                                         else settings.get("print_setup_name", ""))
        cfg["last_prefix"]          = settings["prefix"]
        cfg["pdf_engine"]           = settings["pdf_engine"]
        cfg["last_acc_overwrite"]   = settings["acc_overwrite"]
        cfg["last_print_expanded"]  = bool(self.exp_print_settings.IsExpanded)
        if settings.get("dwg_setup_name"):
            cfg["last_dwg_setup"] = settings["dwg_setup_name"]

        cfg["runs"].append({
            "date":             datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "print_set":        settings["print_set_name"],
            "prefix":           settings["prefix"],
            "output_type":      settings["output_type_key"],
            "sheet_count":      len(sheets),
            "pdf_folder":       settings["pdf_folder"],
            "dwg_folder":       settings["dwg_folder"],
            "acc_pdf_folder":   settings["acc_pdf_folder"],
            "acc_dwg_folder":   settings["acc_dwg_folder"],
            "include_revision":  settings["include_revision"],
            "print_setup_name":  settings.get("print_setup_name", ""),
            "merge_pdf":         settings["merge_pdf"],
            "pdf_engine":        settings["pdf_engine"],
            "published":        published,
        })
        cfg["runs"] = cfg["runs"][-100:]
        save_config(cfg)

        self._hide_progress()
        succeeded = sum(1 for l in log_lines if u"✓" in l)
        failed    = sum(1 for l in log_lines if u"✗" in l)
        pub_note  = u"  Published to ACC." if published else u""
        self._set_status(
            u"Done. {} succeeded, {} failed.  Log saved.{}".format(
                succeeded, failed, pub_note)
        )
        self.btn_export.IsEnabled = True
        self.btn_export.Content   = u"Export & Publish" \
            if (pub_pdf or pub_dwg) else "Export Package"
        self._refresh_history()

        # ---- Open folder ----
        if settings.get("open_folder") and last_folder:
            subprocess.Popen('explorer "{}"'.format(last_folder), shell=True)

    # ------------------------------------------------------------------
    def _on_window_loaded(self, s, e):
        """Called by WPF after the window is rendered — do all expensive work here."""
        self._populate()
        self._wire_events()
        self._update_ui()
        self._refresh_presets()

    # ------------------------------------------------------------------
    def show(self):
        self.win.ShowDialog()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    dlg = ExportDialog()
    dlg.show()
