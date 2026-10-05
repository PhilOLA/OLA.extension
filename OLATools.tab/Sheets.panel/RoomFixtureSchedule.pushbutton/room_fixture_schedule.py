# -*- coding: utf-8 -*-
"""
Room Fixture Schedule Tool  v1.0
=================================
pyRevit IronPython pushbutton — Revit 2024 / IronPython 2.7

Operations
----------
1. Check & Fix   — verify name + Room:Number filter on plan and elevation sheets
2. Create Missing — copy template, rename, set filter, place (plan → elev → new sheet)
3. Place Missing  — place already-created schedules with same priority logic

Config remembered via pyRevit config: template_name

Error handling
--------------
- Each sheet is wrapped in its own try/except so one bad sheet never stops the batch
- Risky sub-steps (schedule size, filter update, placement) have their own try/except
  so a failure in one step doesn't skip the rest of that sheet's processing
- Fatal errors at operation level are caught, logged and counted, then execution continues
"""

from __future__ import print_function
import re
import datetime

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System")

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    ScheduleSheetInstance,
    ViewSchedule,
    ViewSheet,
    ElementId,
    XYZ,
    Transaction,
    BuiltInParameter,
    ScheduleFilter,
    ScheduleFilterType,
)
from Autodesk.Revit import DB

import System
import System.Windows
from System.Windows import (
    Window, Thickness,
    MessageBox, MessageBoxButton, MessageBoxImage,
)
from System.Windows.Controls import (
    StackPanel, ScrollViewer, Button, CheckBox, TextBox,
    TextBlock, Separator, ListBox, ListBoxItem,
    ScrollBarVisibility, Orientation, ProgressBar,
)
from System.Windows.Media import SolidColorBrush, Color
from Microsoft.Win32 import SaveFileDialog

from pyrevit import revit, script
doc    = revit.doc
uidoc  = revit.uidoc
config = script.get_config()

VERSION = "v1.0"

# ── Config ────────────────────────────────────────────────────────────────────
CFG_TEMPLATE          = "template_name"
DEFAULT_TEMPLATE_NAME = "ROOM FIXTURE SCHEDULE - TEMPLATE"

def cfg_get(key, default=""):
    try:
        return getattr(config, key) or default
    except Exception:
        return default

def cfg_set(key, value):
    try:
        setattr(config, key, value)
        config.save_changes()
    except Exception:
        pass

_template_name = cfg_get(CFG_TEMPLATE, DEFAULT_TEMPLATE_NAME)

SCHEDULE_PREFIX      = "ROOM FIXTURE SCHEDULE - "
MM_PER_FOOT          = 304.8
RIGHT_OFFSET_MM      = 10.0
TOP_OFFSET_MM        = 15.0
PLAN_SHEET_SUFFIX_RE = re.compile(r"^(.+)-0$")
ELEV_SHEET_SUFFIX_RE = re.compile(r"^(.+)-([1-9]\d*)$")

# ── Colours ───────────────────────────────────────────────────────────────────
C_BG      = Color.FromRgb(0x26, 0x26, 0x26)
C_TEXT    = Color.FromRgb(0xEE, 0xEE, 0xEE)
C_AMBER   = Color.FromRgb(0xFF, 0xB7, 0x4D)
C_BLUE    = Color.FromRgb(0x4F, 0xC3, 0xF7)
C_GREEN   = Color.FromRgb(0x81, 0xC7, 0x84)
C_RED     = Color.FromRgb(0xEF, 0x53, 0x50)
C_SUBTEXT = Color.FromRgb(0x9E, 0x9E, 0x9E)
C_PANEL   = Color.FromRgb(0x1A, 0x1A, 0x1A)
C_SEP     = Color.FromRgb(0x44, 0x44, 0x44)
C_BTN     = Color.FromRgb(0x1E, 0x88, 0xE5)
C_BTN2    = Color.FromRgb(0x42, 0x42, 0x42)
C_TEAL    = Color.FromRgb(0x4D, 0xB6, 0xAC)   # teal = FIXED in log

def brush(c):
    return SolidColorBrush(c)


# ═══════════════════════════════════════════════════════════════════════════════
#  CORE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def mm_to_ft(mm):
    return mm / MM_PER_FOOT

def get_template_name():
    return _template_name

def get_all_rls_plan_sheets():
    sheets = FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
    result = [s for s in sheets if PLAN_SHEET_SUFFIX_RE.match(s.SheetNumber)]
    return sorted(result, key=lambda s: s.SheetNumber)


def get_base_number(sheet_number):
    m = PLAN_SHEET_SUFFIX_RE.match(sheet_number)
    return m.group(1) if m else sheet_number

def get_elevation_sheets_for_base(base_number):
    sheets = FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
    result = []
    for s in sheets:
        m = ELEV_SHEET_SUFFIX_RE.match(s.SheetNumber)
        if m and m.group(1) == base_number:
            result.append(s)
    return sorted(result, key=lambda s: s.SheetNumber)

def get_plan_view_on_sheet(sheet):
    plan_types = [DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan, DB.ViewType.AreaPlan]
    viewports  = FilteredElementCollector(doc).OfClass(DB.Viewport)\
        .OwnedByView(sheet.Id).ToElements()
    candidates = []
    for vp in viewports:
        view = doc.GetElement(vp.ViewId)
        if view and view.ViewType in plan_types:
            candidates.append(view)
    if not candidates:
        return None
    prefer = [v for v in candidates if v.Name.upper().endswith("PLAN")]
    return prefer[0] if prefer else candidates[0]

def parse_room_number_from_view(view):
    if view is None:
        return None
    parts = view.Name.strip().split()
    return parts[0] if parts else None

def get_template_schedule():
    name = get_template_name()
    for s in FilteredElementCollector(doc).OfClass(ViewSchedule).ToElements():
        if s.Name == name:
            return s
    return None

def get_schedule_on_sheet(sheet):
    """Return (ViewSchedule, ScheduleSheetInstance) or (None, None)."""
    for inst in FilteredElementCollector(doc)\
            .OfClass(ScheduleSheetInstance).OwnedByView(sheet.Id).ToElements():
        sched = doc.GetElement(inst.ScheduleId)
        if sched and sched.Name.startswith(SCHEDULE_PREFIX):
            return sched, inst
    return None, None

def find_schedule_by_room_number(room_number):
    target = SCHEDULE_PREFIX + room_number
    for s in FilteredElementCollector(doc).OfClass(ViewSchedule).ToElements():
        if s.Name == target:
            return s
    return None

def get_schedule_filter_room_number(schedule):
    defn = schedule.Definition
    for i in range(defn.GetFilterCount()):
        f     = defn.GetFilter(i)
        field = defn.GetField(f.FieldId)
        if field and field.ParameterId == ElementId(BuiltInParameter.ROOM_NUMBER):
            return f.GetStringValue()
    return None

def set_schedule_filter_room_number(schedule, room_number):
    defn = schedule.Definition
    for i in range(defn.GetFilterCount()):
        f     = defn.GetFilter(i)
        field = defn.GetField(f.FieldId)
        if field and field.ParameterId == ElementId(BuiltInParameter.ROOM_NUMBER):
            defn.SetFilter(i, ScheduleFilter(f.FieldId, ScheduleFilterType.Equal, room_number))
            return True
    return False

def copy_template_schedule(room_number):
    """Returns (ViewSchedule, error_msg_or_None)."""
    template = get_template_schedule()
    if template is None:
        return None, "Template '{}' not found.".format(get_template_name())
    new_id    = template.Duplicate(DB.ViewDuplicateOption.Duplicate)
    new_sched = doc.GetElement(new_id)
    if new_sched is None:
        return None, "Duplicate returned None."
    new_sched.Name = SCHEDULE_PREFIX + room_number
    if not set_schedule_filter_room_number(new_sched, room_number):
        return new_sched, "WARNING: could not set Room:Number filter."
    return new_sched, None


# ── Sheet geometry ─────────────────────────────────────────────────────────────

def get_title_block_outline(sheet):
    for tb in FilteredElementCollector(doc).OfClass(DB.FamilyInstance)\
            .OwnedByView(sheet.Id).ToElements():
        if tb.Category and tb.Category.Id == ElementId(DB.BuiltInCategory.OST_TitleBlocks):
            bb = tb.get_BoundingBox(sheet)
            if bb:
                return bb
    outline = sheet.Outline
    if outline:
        bb     = DB.BoundingBoxXYZ()
        bb.Min = XYZ(outline.Min.U, outline.Min.V, 0)
        bb.Max = XYZ(outline.Max.U, outline.Max.V, 0)
        return bb
    return None

def get_schedule_size(schedule):
    """Return (width_ft, height_ft). Falls back to 150×100 mm."""
    try:
        table = schedule.GetTableData()
        body  = table.GetSectionData(DB.SectionType.Body)
        hdr   = table.GetSectionData(DB.SectionType.Header)
        col   = table.GetSectionData(DB.SectionType.ColumnHeaders)
        w = body.TableWidth
        h = body.TableHeight
        if hdr: h += hdr.TableHeight
        if col: h += col.TableHeight
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return mm_to_ft(150), mm_to_ft(100)

def get_top_right_pos(sheet, sched_w, sched_h):
    bb = get_title_block_outline(sheet)
    if bb is None:
        return None
    if sched_w > (bb.Max.X - bb.Min.X) or sched_h > (bb.Max.Y - bb.Min.Y):
        return None
    return XYZ(
        bb.Max.X - mm_to_ft(RIGHT_OFFSET_MM) - sched_w,
        bb.Max.Y - mm_to_ft(TOP_OFFSET_MM),
        0,
    )

def check_top_right_free(sheet, sched_w, sched_h):
    """
    True if the top-right region is free of viewports and schedule instances.

    GetBoxOutline() MinimumPoint/MaximumPoint type varies by Revit version:
    - Sometimes UV  → use .U / .V
    - Sometimes XYZ → use .X / .Y
    We read both defensively and fall back to a centre-point guard if needed.

    ScheduleSheetInstance.Point is always XYZ → use .X / .Y
    """
    pos = get_top_right_pos(sheet, sched_w, sched_h)
    if pos is None:
        return False

    pad = mm_to_ft(5)
    sx0 = pos.X - pad
    sx1 = pos.X + sched_w + pad
    sy0 = pos.Y - sched_h - pad
    sy1 = pos.Y + pad

    for vp in FilteredElementCollector(doc).OfClass(DB.Viewport)\
            .OwnedByView(sheet.Id).ToElements():
        try:
            outline = vp.GetBoxOutline()
            if outline is None:
                continue
            mn = outline.MinimumPoint
            mx = outline.MaximumPoint
            # Determine coordinate type: UV has .U, XYZ has .X
            try:
                vx0, vy0 = mn.U, mn.V
                vx1, vy1 = mx.U, mx.V
            except AttributeError:
                vx0, vy0 = mn.X, mn.Y
                vx1, vy1 = mx.X, mx.Y
            if sx0 < vx1 and sx1 > vx0 and sy0 < vy1 and sy1 > vy0:
                return False
        except Exception:
            # If outline read fails entirely, fall back to centre-point guard
            try:
                ctr = vp.GetBoxCenter()
                guard = mm_to_ft(100)
                if sx0 < ctr.X + guard and sx1 > ctr.X - guard \
                        and sy0 < ctr.Y + guard and sy1 > ctr.Y - guard:
                    return False
            except Exception:
                pass   # can't determine position; skip this viewport

    for inst in FilteredElementCollector(doc).OfClass(ScheduleSheetInstance)\
            .OwnedByView(sheet.Id).ToElements():
        try:
            p     = inst.Point   # always XYZ
            guard = mm_to_ft(50)
            if sx0 < p.X + guard and sx1 > p.X - guard \
                    and sy0 < p.Y + guard and sy1 > p.Y - guard:
                return False
        except Exception:
            pass

    return True

def compute_centre_pos(sheet, sched_w, sched_h):
    bb = get_title_block_outline(sheet)
    if bb is None:
        return XYZ(0, 0, 0)
    cx = (bb.Min.X + bb.Max.X) / 2.0
    cy = (bb.Min.Y + bb.Max.Y) / 2.0
    return XYZ(cx - sched_w / 2.0, cy + sched_h / 2.0, 0)

def place_schedule(schedule, sheet, pos):
    return ScheduleSheetInstance.Create(doc, sheet.Id, schedule.Id, pos)

def find_best_placement(plan_sheet, sched_w, sched_h):
    base_num = get_base_number(plan_sheet.SheetNumber)
    if check_top_right_free(plan_sheet, sched_w, sched_h):
        pos = get_top_right_pos(plan_sheet, sched_w, sched_h)
        return plan_sheet, pos, "plan sheet {}".format(plan_sheet.SheetNumber)
    for elev in get_elevation_sheets_for_base(base_num):
        if check_top_right_free(elev, sched_w, sched_h):
            pos = get_top_right_pos(elev, sched_w, sched_h)
            return elev, pos, "elevation sheet {}".format(elev.SheetNumber)
    return None, None, "new overflow sheet"


# ── Overflow sheet ────────────────────────────────────────────────────────────

def next_overflow_suffix(base_number):
    existing = set(s.SheetNumber for s in
                   FilteredElementCollector(doc).OfClass(ViewSheet).ToElements())
    suffix = 2
    while "{}-{}".format(base_number, suffix) in existing:
        suffix += 1
    return "{}-{}".format(base_number, suffix)

def get_title_block_type_id(sheet):
    for tb in FilteredElementCollector(doc).OfClass(DB.FamilyInstance)\
            .OwnedByView(sheet.Id).ToElements():
        if tb.Category and tb.Category.Id == ElementId(DB.BuiltInCategory.OST_TitleBlocks):
            return tb.GetTypeId()
    return ElementId.InvalidElementId

def create_overflow_sheet(plan_sheet, base_number):
    tb_id     = get_title_block_type_id(plan_sheet)
    new_num   = next_overflow_suffix(base_number)
    new_sheet = ViewSheet.Create(doc, tb_id)
    new_sheet.SheetNumber = new_num
    new_sheet.Name        = plan_sheet.Name
    t2p = plan_sheet.LookupParameter("OLA Sheet Title 2")
    if t2p:
        t2v = re.sub(r"\s*PLAN\s*$", "", t2p.AsString() or "", flags=re.IGNORECASE).strip()
        p   = new_sheet.LookupParameter("OLA Sheet Title 2")
        if p and not p.IsReadOnly:
            p.Set(t2v)
    t3p = new_sheet.LookupParameter("OLA Sheet Title 3")
    if t3p and not t3p.IsReadOnly:
        t3p.Set("ROOM FIXTURE SCHEDULE")
    return new_sheet, new_num


# ═══════════════════════════════════════════════════════════════════════════════
#  OpResult
# ═══════════════════════════════════════════════════════════════════════════════

class OpResult(object):
    def __init__(self, sheet_num, room_num):
        self.sheet_num = sheet_num
        self.room_num  = room_num
        self.actions   = []
        self.warnings  = []
        self.errors    = []

    def ok(self, msg):   self.actions.append(msg)
    def warn(self, msg): self.warnings.append(msg)
    def err(self, msg):  self.errors.append(msg)

    @property
    def status(self):
        if self.errors:   return "ERROR"
        if self.warnings: return "WARNING"
        if self.actions:  return "OK"
        return "SKIPPED"


# ═══════════════════════════════════════════════════════════════════════════════
#  OPERATIONS
#  Each sheet has an outer try/except so one bad sheet never halts the batch.
#  Sub-steps (filter, placement) each have their own inner try/except so a
#  failure in step N is logged but step N+1 still runs on the same sheet.
# ═══════════════════════════════════════════════════════════════════════════════

def op_check_fix(log_cb, progress_cb):
    results     = []
    plan_sheets = get_all_rls_plan_sheets()
    total       = len(plan_sheets)
    log_cb("INFO", "Scanning {} RLS plan sheets + elevation sheets...".format(total))

    with Transaction(doc, "RFS: Check & Fix") as t:
        t.Start()
        for idx, plan_sheet in enumerate(plan_sheets):
            progress_cb(idx + 1, total)
            try:
                plan_view = get_plan_view_on_sheet(plan_sheet)
                room_num  = parse_room_number_from_view(plan_view)
                r = OpResult(plan_sheet.SheetNumber, room_num or "?")

                if room_num is None:
                    r.err("No plan view — cannot determine room number.")
                    log_cb("ERROR", "  {} — no plan view".format(plan_sheet.SheetNumber))
                    results.append(r)
                    continue

                base_num   = get_base_number(plan_sheet.SheetNumber)
                all_sheets = [plan_sheet] + get_elevation_sheets_for_base(base_num)
                found_sched, found_sheet = None, None

                for sh in all_sheets:
                    try:
                        sched, inst = get_schedule_on_sheet(sh)
                        if sched is not None:
                            found_sched = sched
                            found_sheet = sh
                            break
                    except Exception as ex:
                        log_cb("WARN", "  {} — could not read sheet {}: {}".format(
                            plan_sheet.SheetNumber, sh.SheetNumber, ex))

                if found_sched is None:
                    r.warn("No schedule on plan or elevation sheets — run Create Missing.")
                    log_cb("WARN", "  {} [{}] — no schedule".format(
                        plan_sheet.SheetNumber, room_num))
                    results.append(r)
                    continue

                expected  = SCHEDULE_PREFIX + room_num
                name_ok   = (found_sched.Name == expected)

                try:
                    fval      = get_schedule_filter_room_number(found_sched)
                    filter_ok = (fval == room_num)
                except Exception as ex:
                    fval, filter_ok = None, False
                    r.warn("Could not read filter: {}".format(ex))
                    log_cb("WARN", "  {} — filter read error: {}".format(
                        plan_sheet.SheetNumber, ex))

                if name_ok and filter_ok:
                    r.ok("Name and filter correct (on {}).".format(found_sheet.SheetNumber))
                    log_cb("OK", "  {} [{}] — OK".format(plan_sheet.SheetNumber, room_num))
                else:
                    if not name_ok:
                        old = found_sched.Name
                        try:
                            found_sched.Name = expected
                            r.ok("Renamed '{}' → '{}'".format(old, expected))
                        except Exception as ex:
                            r.err("Rename failed: {}".format(ex))
                            log_cb("ERROR", "  {} — rename error: {}".format(
                                plan_sheet.SheetNumber, ex))

                    if not filter_ok:
                        try:
                            if set_schedule_filter_room_number(found_sched, room_num):
                                r.ok("Filter '{}' → '{}'".format(fval, room_num))
                            else:
                                r.err("Failed to update Room:Number filter.")
                                log_cb("ERROR", "  {} — filter update failed".format(
                                    plan_sheet.SheetNumber))
                        except Exception as ex:
                            r.err("Filter update error: {}".format(ex))
                            log_cb("ERROR", "  {} — filter error: {}".format(
                                plan_sheet.SheetNumber, ex))

                    log_cb("FIXED", "  {} [{}] — FIXED (on {})".format(
                        plan_sheet.SheetNumber, room_num, found_sheet.SheetNumber))

                results.append(r)

            except Exception as ex:
                r = OpResult(plan_sheet.SheetNumber, "?")
                r.err("Unexpected error: {}".format(ex))
                log_cb("ERROR", "  {} — unexpected error: {}".format(
                    plan_sheet.SheetNumber, ex))
                results.append(r)

        t.Commit()

    progress_cb(total, total)
    return results


def op_create_missing(log_cb, progress_cb):
    results     = []
    plan_sheets = get_all_rls_plan_sheets()
    total       = len(plan_sheets)
    log_cb("INFO", "Scanning {} plan sheets for missing schedules...".format(total))

    if get_template_schedule() is None:
        log_cb("ERROR", "Template '{}' not found — aborting.".format(get_template_name()))
        return []

    with Transaction(doc, "RFS: Create Missing Schedules") as t:
        t.Start()
        for idx, plan_sheet in enumerate(plan_sheets):
            progress_cb(idx + 1, total)
            try:
                plan_view = get_plan_view_on_sheet(plan_sheet)
                room_num  = parse_room_number_from_view(plan_view)
                r = OpResult(plan_sheet.SheetNumber, room_num or "?")

                if room_num is None:
                    r.err("No plan view — cannot determine room number.")
                    log_cb("ERROR", "  {} — no plan view".format(plan_sheet.SheetNumber))
                    results.append(r)
                    continue

                base_num   = get_base_number(plan_sheet.SheetNumber)
                all_sheets = [plan_sheet] + get_elevation_sheets_for_base(base_num)

                if any(get_schedule_on_sheet(s)[1] is not None for s in all_sheets):
                    r.ok("Already placed in sheet group.")
                    log_cb("OK", "  {} [{}] — already placed".format(
                        plan_sheet.SheetNumber, room_num))
                    results.append(r)
                    continue

                # ── Create schedule view
                new_sched = find_schedule_by_room_number(room_num)
                if new_sched:
                    r.ok("Found existing unplaced schedule.")
                else:
                    try:
                        new_sched, err = copy_template_schedule(room_num)
                        if new_sched is None:
                            r.err("Create failed: {}".format(err))
                            log_cb("ERROR", "  {} [{}] — create failed: {}".format(
                                plan_sheet.SheetNumber, room_num, err))
                            results.append(r)
                            continue
                        if err:
                            r.warn(err)
                            log_cb("WARN", "  {} [{}] — {}".format(
                                plan_sheet.SheetNumber, room_num, err))
                        r.ok("Created '{}'.".format(new_sched.Name))
                    except Exception as ex:
                        r.err("Create exception: {}".format(ex))
                        log_cb("ERROR", "  {} [{}] — create exception: {}".format(
                            plan_sheet.SheetNumber, room_num, ex))
                        results.append(r)
                        continue

                # ── Get schedule size (non-fatal if it falls back)
                try:
                    w_ft, h_ft = get_schedule_size(new_sched)
                except Exception as ex:
                    w_ft, h_ft = mm_to_ft(150), mm_to_ft(100)
                    log_cb("WARN", "  {} [{}] — size read failed (using default): {}".format(
                        plan_sheet.SheetNumber, room_num, ex))

                # ── Find best sheet to place on
                try:
                    target, pos, label = find_best_placement(plan_sheet, w_ft, h_ft)
                    if target is None:
                        target, overflow_num = create_overflow_sheet(plan_sheet, base_num)
                        pos   = compute_centre_pos(target, w_ft, h_ft)
                        label = "new overflow sheet {}".format(overflow_num)
                except Exception as ex:
                    r.err("Placement decision failed: {}".format(ex))
                    log_cb("ERROR", "  {} [{}] — placement decision error: {}".format(
                        plan_sheet.SheetNumber, room_num, ex))
                    results.append(r)
                    continue

                # ── Place
                try:
                    place_schedule(new_sched, target, pos)
                    r.ok("Placed on {}.".format(label))
                    log_cb("OK", "  {} [{}] — CREATED & PLACED on {}".format(
                        plan_sheet.SheetNumber, room_num, label))
                except Exception as ex:
                    r.err("Placement failed: {}".format(ex))
                    log_cb("ERROR", "  {} [{}] — placement error: {}".format(
                        plan_sheet.SheetNumber, room_num, ex))

                results.append(r)

            except Exception as ex:
                r = OpResult(plan_sheet.SheetNumber, "?")
                r.err("Unexpected error: {}".format(ex))
                log_cb("ERROR", "  {} — unexpected error: {}".format(
                    plan_sheet.SheetNumber, ex))
                results.append(r)

        t.Commit()

    progress_cb(total, total)
    return results


def op_place_missing(log_cb, progress_cb):
    results     = []
    plan_sheets = get_all_rls_plan_sheets()
    total       = len(plan_sheets)
    log_cb("INFO", "Scanning {} plan sheets for unplaced schedules...".format(total))

    if get_template_schedule() is None:
        log_cb("ERROR", "Template '{}' not found — aborting.".format(get_template_name()))
        return []

    with Transaction(doc, "RFS: Place Missing Schedules") as t:
        t.Start()
        for idx, plan_sheet in enumerate(plan_sheets):
            progress_cb(idx + 1, total)
            try:
                plan_view = get_plan_view_on_sheet(plan_sheet)
                room_num  = parse_room_number_from_view(plan_view)
                r = OpResult(plan_sheet.SheetNumber, room_num or "?")

                if room_num is None:
                    r.err("No plan view — cannot determine room number.")
                    log_cb("ERROR", "  {} — no plan view".format(plan_sheet.SheetNumber))
                    results.append(r)
                    continue

                base_num   = get_base_number(plan_sheet.SheetNumber)
                all_sheets = [plan_sheet] + get_elevation_sheets_for_base(base_num)

                if any(get_schedule_on_sheet(s)[1] is not None for s in all_sheets):
                    r.ok("Already placed.")
                    log_cb("OK", "  {} [{}] — already placed".format(
                        plan_sheet.SheetNumber, room_num))
                    results.append(r)
                    continue

                # ── Find or create schedule view
                new_sched = find_schedule_by_room_number(room_num)
                if new_sched is None:
                    try:
                        new_sched, err = copy_template_schedule(room_num)
                        if new_sched is None:
                            r.err("Cannot create schedule: {}".format(err))
                            log_cb("ERROR", "  {} [{}] — {}".format(
                                plan_sheet.SheetNumber, room_num, err))
                            results.append(r)
                            continue
                        if err:
                            r.warn(err)
                            log_cb("WARN", "  {} [{}] — {}".format(
                                plan_sheet.SheetNumber, room_num, err))
                        r.ok("Created '{}'.".format(new_sched.Name))
                    except Exception as ex:
                        r.err("Create exception: {}".format(ex))
                        log_cb("ERROR", "  {} [{}] — create exception: {}".format(
                            plan_sheet.SheetNumber, room_num, ex))
                        results.append(r)
                        continue

                # ── Size
                try:
                    w_ft, h_ft = get_schedule_size(new_sched)
                except Exception as ex:
                    w_ft, h_ft = mm_to_ft(150), mm_to_ft(100)
                    log_cb("WARN", "  {} [{}] — size read failed (using default): {}".format(
                        plan_sheet.SheetNumber, room_num, ex))

                # ── Placement decision
                try:
                    target, pos, label = find_best_placement(plan_sheet, w_ft, h_ft)
                    if target is None:
                        target, overflow_num = create_overflow_sheet(plan_sheet, base_num)
                        pos   = compute_centre_pos(target, w_ft, h_ft)
                        label = "new overflow sheet {}".format(overflow_num)
                except Exception as ex:
                    r.err("Placement decision failed: {}".format(ex))
                    log_cb("ERROR", "  {} [{}] — placement decision error: {}".format(
                        plan_sheet.SheetNumber, room_num, ex))
                    results.append(r)
                    continue

                # ── Place
                try:
                    place_schedule(new_sched, target, pos)
                    r.ok("Placed on {}.".format(label))
                    log_cb("OK", "  {} [{}] — PLACED on {}".format(
                        plan_sheet.SheetNumber, room_num, label))
                except Exception as ex:
                    r.err("Placement failed: {}".format(ex))
                    log_cb("ERROR", "  {} [{}] — placement error: {}".format(
                        plan_sheet.SheetNumber, room_num, ex))

                results.append(r)

            except Exception as ex:
                r = OpResult(plan_sheet.SheetNumber, "?")
                r.err("Unexpected error: {}".format(ex))
                log_cb("ERROR", "  {} — unexpected error: {}".format(
                    plan_sheet.SheetNumber, ex))
                results.append(r)

        t.Commit()

    progress_cb(total, total)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  WPF DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class RoomFixtureScheduleDialog(Window):

    def __init__(self):
        self.Title      = "Room Fixture Schedule Tool  {}".format(VERSION)
        self.Width      = 720
        self.Height     = 880
        self.MinWidth   = 540
        self.MinHeight  = 620
        self.Background = brush(C_BG)
        self.WindowStartupLocation = System.Windows.WindowStartupLocation.CenterScreen
        self._log_lines = []
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = ScrollViewer()
        outer.VerticalScrollBarVisibility   = ScrollBarVisibility.Auto
        outer.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        self.Content = outer

        root = StackPanel()
        root.Margin = Thickness(18, 18, 18, 18)
        outer.Content = root

        self._lbl(root, "Room Fixture Schedule Tool  {}".format(VERSION), 18, C_AMBER, bold=True)
        self._lbl(root, "Manages ROOM FIXTURE SCHEDULE views on RLS plan and elevation sheets.",
                  11, C_SUBTEXT)
        self._sep(root)

        # Template name
        self._lbl(root, "Template Schedule Name", 12, C_BLUE, bold=True)
        self.txt_template = TextBox()
        self.txt_template.Text       = cfg_get(CFG_TEMPLATE, DEFAULT_TEMPLATE_NAME)
        self.txt_template.Background = brush(C_PANEL)
        self.txt_template.Foreground = brush(C_TEXT)
        self.txt_template.FontSize   = 11
        self.txt_template.Margin     = Thickness(0, 4, 0, 2)
        self.txt_template.Padding    = Thickness(4, 3, 4, 3)
        root.Children.Add(self.txt_template)
        self._lbl(root, "Must match the schedule view name exactly as it appears in the project.",
                  10, C_SUBTEXT)
        self._sep(root)

        # Operations — each checkbox gets a Checked/Unchecked handler to update the count line
        self._lbl(root, "Select Operations", 13, C_BLUE, bold=True)
        self.chk_check  = self._chk(root,
            "1 — Check & Fix   (verify name + filter on plan and elevation sheets)", C_AMBER)
        self.chk_create = self._chk(root,
            "2 — Create Missing   (copy template, rename, set filter, place on sheet)", C_AMBER)
        self.chk_place  = self._chk(root,
            "3 — Place Missing   (place existing schedule: plan → elev → new sheet)", C_AMBER)
        self._sep(root)

        # Progress — one bar per operation, reset between ops
        self._lbl(root, "Progress", 11, C_BLUE, bold=True)
        self.progress = ProgressBar()
        self.progress.Minimum    = 0
        self.progress.Maximum    = 100
        self.progress.Value      = 0
        self.progress.Height     = 18
        self.progress.Margin     = Thickness(0, 4, 0, 2)
        self.progress.Foreground = brush(Color.FromRgb(0x4F, 0xC3, 0xF7))
        self.progress.Background = brush(C_PANEL)
        root.Children.Add(self.progress)

        self.lbl_progress = TextBlock()
        self.lbl_progress.Text       = "Ready."
        self.lbl_progress.Foreground = brush(C_SUBTEXT)
        self.lbl_progress.FontSize   = 10
        self.lbl_progress.Margin     = Thickness(0, 0, 0, 6)
        root.Children.Add(self.lbl_progress)

        # Run button
        btn_run = Button()
        btn_run.Content    = "Run Selected Operations"
        btn_run.Height     = 36
        btn_run.Margin     = Thickness(0, 2, 0, 6)
        btn_run.Background = brush(C_BTN)
        btn_run.Foreground = brush(C_TEXT)
        btn_run.FontSize   = 13
        btn_run.Click     += self._on_run
        root.Children.Add(btn_run)

        # Log
        self._lbl(root, "Log", 12, C_BLUE, bold=True)
        self.log_box = ListBox()
        self.log_box.Height     = 280
        self.log_box.Background = brush(C_PANEL)
        self.log_box.Foreground = brush(C_TEXT)
        self.log_box.FontFamily = System.Windows.Media.FontFamily("Consolas")
        self.log_box.FontSize   = 11
        self.log_box.Margin     = Thickness(0, 4, 0, 4)
        root.Children.Add(self.log_box)

        # Summary
        self._sep(root)
        self._lbl(root, "Summary", 12, C_BLUE, bold=True)
        self.lbl_summary = TextBlock()
        self.lbl_summary.Foreground   = brush(C_GREEN)
        self.lbl_summary.FontSize     = 12
        self.lbl_summary.FontWeight   = System.Windows.FontWeights.Bold
        self.lbl_summary.TextWrapping = System.Windows.TextWrapping.Wrap
        self.lbl_summary.Margin       = Thickness(0, 4, 0, 8)
        root.Children.Add(self.lbl_summary)

        # Bottom buttons
        btn_row = StackPanel()
        btn_row.Orientation = Orientation.Horizontal
        btn_row.Margin      = Thickness(0, 4, 0, 4)

        btn_save = Button()
        btn_save.Content    = "Save Log…"
        btn_save.Height     = 30
        btn_save.Width      = 110
        btn_save.Margin     = Thickness(0, 0, 8, 0)
        btn_save.Background = brush(Color.FromRgb(0x2E, 0x7D, 0x32))
        btn_save.Foreground = brush(C_TEXT)
        btn_save.FontSize   = 12
        btn_save.Click     += self._on_save_log
        btn_row.Children.Add(btn_save)

        btn_close = Button()
        btn_close.Content    = "Close"
        btn_close.Height     = 30
        btn_close.Width      = 90
        btn_close.Background = brush(C_BTN2)
        btn_close.Foreground = brush(C_TEXT)
        btn_close.FontSize   = 12
        btn_close.Click     += lambda s, e: self.Close()
        btn_row.Children.Add(btn_close)

        root.Children.Add(btn_row)

    def _lbl(self, parent, text, size, colour, bold=False):
        tb = TextBlock()
        tb.Text       = text
        tb.FontSize   = size
        tb.Foreground = brush(colour)
        tb.Margin     = Thickness(0, 3, 0, 1)
        if bold:
            tb.FontWeight = System.Windows.FontWeights.Bold
        parent.Children.Add(tb)
        return tb

    def _sep(self, parent):
        sep = Separator()
        sep.Margin     = Thickness(0, 8, 0, 8)
        sep.Background = brush(C_SEP)
        parent.Children.Add(sep)

    def _chk(self, parent, text, colour):
        cb = CheckBox()
        cb.Content    = text
        cb.Foreground = brush(colour)
        cb.FontSize   = 12
        cb.Margin     = Thickness(0, 4, 0, 2)
        cb.IsChecked  = False
        parent.Children.Add(cb)
        return cb

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, level, msg):
        self._log_lines.append("[{}] {}".format(level, msg))
        item = ListBoxItem()
        item.Content  = msg
        item.FontSize = 11
        if level in ("ERROR", "FATAL"):
            item.Foreground = brush(C_RED)
        elif level == "WARN":
            item.Foreground = brush(C_AMBER)
        elif level == "OK":
            item.Foreground = brush(C_GREEN)
        elif level == "FIXED":
            item.Foreground = brush(C_TEAL)
        else:
            item.Foreground = brush(C_TEXT)
        self.log_box.Items.Add(item)
        self.log_box.ScrollIntoView(item)
        self.log_box.UpdateLayout()

    def _set_progress(self, pct, label):
        self.progress.Value    = max(0, min(100, pct))
        self.lbl_progress.Text = label
        self.progress.UpdateLayout()
        self.lbl_progress.UpdateLayout()

    def _reset_progress(self, label=""):
        """Reset bar to 0 between operations."""
        self.progress.Value    = 0
        self.lbl_progress.Text = label
        self.progress.UpdateLayout()
        self.lbl_progress.UpdateLayout()

    # ── Run ───────────────────────────────────────────────────────────────────

    def _on_run(self, sender, args):
        do_check  = self.chk_check.IsChecked
        do_create = self.chk_create.IsChecked
        do_place  = self.chk_place.IsChecked

        if not (do_check or do_create or do_place):
            self._log("WARN", "No operations selected.")
            return

        tname = (self.txt_template.Text or "").strip()
        if not tname:
            self._log("ERROR", "Template name is empty.")
            return
        global _template_name
        _template_name = tname
        cfg_set(CFG_TEMPLATE, tname)

        self.log_box.Items.Clear()
        self._log_lines   = []
        self.lbl_summary.Text = ""
        self._reset_progress("Starting…")

        all_results  = []
        fatal_errors = [0]

        def make_progress(op_label):
            """Returns a per-operation progress callback that resets the bar each time."""
            def cb(current, total):
                if total <= 0:
                    return
                pct = int(float(current) / total * 100)
                self._set_progress(pct, "{} — {} / {}".format(op_label, current, total))
            return cb

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log("INFO", "=== Room Fixture Schedule Tool {}  {} ===".format(VERSION, ts))
        self._log("INFO", "Template: {}".format(tname))
        self._log("INFO", "")

        if do_check:
            self._log("INFO", "─── Operation 1: Check & Fix ───")
            self._reset_progress("Operation 1: Check & Fix…")
            try:
                results = op_check_fix(self._log, make_progress("Check & Fix"))
                all_results.extend(results)
            except Exception as ex:
                self._log("FATAL", "Operation 1 fatal error: {}".format(ex))
                fatal_errors[0] += 1

        if do_create:
            self._log("INFO", "")
            self._log("INFO", "─── Operation 2: Create Missing ───")
            self._reset_progress("Operation 2: Create Missing…")
            try:
                results = op_create_missing(self._log, make_progress("Create Missing"))
                all_results.extend(results)
            except Exception as ex:
                self._log("FATAL", "Operation 2 fatal error: {}".format(ex))
                fatal_errors[0] += 1

        if do_place:
            self._log("INFO", "")
            self._log("INFO", "─── Operation 3: Place Missing ───")
            self._reset_progress("Operation 3: Place Missing…")
            try:
                results = op_place_missing(self._log, make_progress("Place Missing"))
                all_results.extend(results)
            except Exception as ex:
                self._log("FATAL", "Operation 3 fatal error: {}".format(ex))
                fatal_errors[0] += 1

        self._set_progress(100, "Done.")

        # Summary
        valid   = [r for r in all_results if isinstance(r, OpResult)]
        ok_n    = sum(1 for r in valid if r.status in ("OK", "SKIPPED"))
        warn_n  = sum(1 for r in valid if r.status == "WARNING")
        err_n   = sum(1 for r in valid if r.status == "ERROR") + fatal_errors[0]
        total_n = len(valid)

        summary = (
            "Done.   Sheets: {}    OK/Skipped: {}    Warnings: {}    Errors: {}"
            .format(total_n, ok_n, warn_n, err_n)
        )
        self.lbl_summary.Text = summary

        if err_n:
            self.lbl_summary.Foreground = brush(C_RED)
            slevel = "ERROR"
        elif warn_n:
            self.lbl_summary.Foreground = brush(C_AMBER)
            slevel = "WARN"
        else:
            self.lbl_summary.Foreground = brush(C_GREEN)
            slevel = "OK"

        self._log("INFO", "")
        self._log(slevel, summary)
        self._log("INFO", "=== Complete ===")

    # ── Save log ──────────────────────────────────────────────────────────────

    def _on_save_log(self, sender, args):
        if not self._log_lines:
            MessageBox.Show("Nothing to save — run an operation first.",
                            "Save Log", MessageBoxButton.OK, MessageBoxImage.Information)
            return
        dlg = SaveFileDialog()
        dlg.Title    = "Save Log"
        dlg.Filter   = "Text files (*.txt)|*.txt|All files (*.*)|*.*"
        dlg.FileName = "RFS_Log_{}.txt".format(
            datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        if dlg.ShowDialog():
            try:
                with open(dlg.FileName, "w") as f:
                    f.write("\n".join(self._log_lines))
                self._log("OK", "Log saved: {}".format(dlg.FileName))
            except Exception as ex:
                self._log("ERROR", "Save failed: {}".format(ex))


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    dlg = RoomFixtureScheduleDialog()
    dlg.ShowDialog()
