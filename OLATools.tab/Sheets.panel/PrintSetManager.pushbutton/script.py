# -*- coding: utf-8 -*-
"""Print Set Manager v1.5.0
Create and manage Revit print sets from joinery sheets.
Tab 1 -- Manage Sets: create/edit/delete ViewSheetSets.
Tab 2 -- New Sheets:  independent scan -- pick a set, see missing sheets, add them.
Both tabs have a live filter box: type a sheet number prefix (e.g. 84) to narrow the
list, then Select All grabs only the matching visible rows.
"""
__title__  = "Print Set\nManager"
__author__ = "OLA"

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Collections")

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilyInstance, ViewSheetSet,
    ViewSheet, ScheduleSheetInstance, PrintManager, PrintRange,
    BuiltInParameter, XYZ, Transaction, ViewSet
)
from Autodesk.Revit.DB.Architecture import Room

from System.Collections.ObjectModel import ObservableCollection
import System.Windows
from System.Windows import (
    Window, HorizontalAlignment, VerticalAlignment, Thickness,
    FontWeights, ResizeMode, GridLength, GridUnitType,
    MessageBox, MessageBoxButton, MessageBoxResult,
    Visibility, Style, Setter
)
from System.Windows.Controls import (
    Grid, RowDefinition, ColumnDefinition,
    ListBox, ListBoxItem, CheckBox,
    TabControl, TabItem,
    StackPanel, Orientation, ScrollViewer,
    TextBlock, TextBox, Button, Label,
    ComboBox, ComboBoxItem,
    Separator
)
from System.Windows.Controls.Primitives import DataGridColumnHeader
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Data import Binding

VERSION = "v1.5.0"

doc = __revit__.ActiveUIDocument.Document

# -- Colour helpers -----------------------------------------------------------
def hex_brush(h):
    h = h.lstrip("#")
    return SolidColorBrush(Color.FromArgb(255, int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)))

BG       = hex_brush("#1E1E1E")
PANEL    = hex_brush("#262626")
DARK     = hex_brush("#181818")
BTN_BG   = hex_brush("#2D2D2D")
SELECTED = hex_brush("#1E2A38")
TEXT     = hex_brush("#E0E0E0")
MUTED    = hex_brush("#888780")
ACCENT   = hex_brush("#F4723A")
BLUE     = hex_brush("#4FC3F7")
SUCCESS  = hex_brush("#4DB6AC")
WARNING  = hex_brush("#FFB74D")
ERROR    = hex_brush("#FF6B6B")
LINE     = hex_brush("#3A3A3A")
LINE2    = hex_brush("#2D2D2D")

# -- Constants ----------------------------------------------------------------
PARAM        = "AR_RLS_ITEM CODE"
SCHED_PREFIX = "ROOM FIXTURE SCHEDULE"
RLS_SUFFIXES = {"0", "1", "2"}

# -- Phase helper -------------------------------------------------------------
def get_last_phase():
    last = None
    try:
        for phase in doc.Phases:
            last = phase
    except Exception:
        pass
    return last

LAST_PHASE = get_last_phase()

# -- Room lookup --------------------------------------------------------------
def _room_at(pt):
    try:
        r = doc.GetRoomAtPoint(pt, LAST_PHASE) if LAST_PHASE else doc.GetRoomAtPoint(pt)
        if isinstance(r, Room):
            return r
    except Exception:
        pass
    return None

def get_room(fi):
    try:
        loc = fi.Location
        if loc is not None:
            pt = loc.Point
            for z in (0.1, 0.5, 1.0):
                r = _room_at(XYZ(pt.X, pt.Y, pt.Z + z))
                if r is not None:
                    return r
    except Exception:
        pass
    try:
        bb = fi.get_BoundingBox(None)
        if bb is not None:
            mid = XYZ((bb.Min.X+bb.Max.X)/2.0,
                      (bb.Min.Y+bb.Max.Y)/2.0,
                      bb.Min.Z + 0.5)
            r = _room_at(mid)
            if r is not None:
                return r
    except Exception:
        pass
    return None

# -- Core data functions ------------------------------------------------------
def get_joinery_room_numbers():
    room_nos = set()
    for fi in FilteredElementCollector(doc).OfClass(FamilyInstance):
        p = fi.Symbol.LookupParameter(PARAM) if fi.Symbol else None
        if not p or not p.HasValue:
            p = fi.LookupParameter(PARAM)
        if not p or not p.HasValue:
            continue
        code = (p.AsString() or "").strip()
        if not code.upper().startswith("J"):
            continue
        room = get_room(fi)
        if room:
            try:
                rno = room.get_Parameter(BuiltInParameter.ROOM_NUMBER).AsString() or ""
                if rno:
                    room_nos.add(rno.upper())
            except Exception:
                pass
    return room_nos


def get_joinery_base_numbers(joinery_room_nos):
    bases = set()
    for ssi in FilteredElementCollector(doc).OfClass(ScheduleSheetInstance):
        try:
            sheet = doc.GetElement(ssi.OwnerViewId)
            if sheet is None:
                continue
            p = sheet.get_Parameter(BuiltInParameter.SHEET_NUMBER)
            sheet_no = (p.AsString() or "").strip() if p else ""
            if not sheet_no or "-" not in sheet_no:
                continue
            suffix = sheet_no.rsplit("-", 1)[-1]
            if suffix not in RLS_SUFFIXES:
                continue
            schedule = doc.GetElement(ssi.ScheduleId)
            if schedule is None:
                continue
            view_name = (schedule.Name or "").strip()
            if not view_name.upper().startswith(SCHED_PREFIX):
                continue
            sep = view_name.find(" - ")
            if sep == -1:
                continue
            room_no = view_name[sep+3:].strip().upper()
            if room_no not in joinery_room_nos:
                continue
            base = sheet_no.rsplit("-", 1)[0]
            bases.add(base)
        except Exception:
            pass
    return bases


def get_all_sheets_map():
    result = {}
    for s in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            p = s.get_Parameter(BuiltInParameter.SHEET_NUMBER)
            no = (p.AsString() or "").strip() if p else ""
            if no:
                result[no] = s
        except Exception:
            pass
    return result


def get_sheet_full_title(sheet):
    parts = []
    try:
        p = sheet.get_Parameter(BuiltInParameter.SHEET_NAME)
        v = (p.AsString() or "").strip() if p else ""
        if v:
            parts.append(v)
    except Exception:
        pass
    for param_name in ("OLA Sheet Title 2", "OLA Sheet Title 3"):
        try:
            p = sheet.LookupParameter(param_name)
            if p and p.HasValue:
                v = (p.AsString() or "").strip()
                if v:
                    parts.append(v)
        except Exception:
            pass
    return u"  --  ".join(parts) if parts else ""


def expand_series(bases, all_sheets_map):
    results = []
    for sheet_no, sheet in all_sheets_map.items():
        if "-" not in sheet_no:
            continue
        base = sheet_no.rsplit("-", 1)[0]
        if base not in bases:
            continue
        title = get_sheet_full_title(sheet)
        results.append({
            "sheet_no": sheet_no,
            "title":    title,
            "sheet_id": sheet.Id,
            "base":     base
        })

    def _key(info):
        parts = info["sheet_no"].rsplit("-", 1)
        base  = parts[0]
        try:
            sfx = int(parts[1])
        except Exception:
            sfx = 0
        return (base, sfx)

    results.sort(key=_key)
    return results


def get_existing_print_sets():
    sets = []
    for vss in FilteredElementCollector(doc).OfClass(ViewSheetSet):
        try:
            name = vss.Name or ""
            sheet_ids = set()
            for v in vss.Views:
                sheet_ids.add(v.Id)
            sets.append({
                "name":      name,
                "id":        vss.Id,
                "sheet_ids": sheet_ids
            })
        except Exception:
            pass
    sets.sort(key=lambda x: x["name"].lower())
    return sets


def sheet_to_set_map(print_sets):
    mapping = {}
    for ps in print_sets:
        for sid in ps["sheet_ids"]:
            if sid not in mapping:
                mapping[sid] = []
            mapping[sid].append(ps["name"])
    return mapping


def _suppress_dialog(sender, e):
    try:
        e.OverrideResult(1)
    except Exception:
        pass


def save_print_set(name, sheet_elements, existing_id=None):
    __revit__.DialogBoxShowing += _suppress_dialog
    try:
        pm = doc.PrintManager
        pm.PrintRange = PrintRange.Select
        vss = pm.ViewSheetSetting

        vs = ViewSet()
        for s in sheet_elements:
            vs.Insert(s)

        with Transaction(doc, "Print Set Manager - Save Set") as t:
            t.Start()
            if existing_id is not None:
                existing = doc.GetElement(existing_id)
                if existing:
                    vss.CurrentViewSheetSet = existing
                    vss.Delete()
            vss.SaveAs(name)
            vss.CurrentViewSheetSet.Views = vs
            vss.Save()
            t.Commit()
        return True, "Set '{}' saved - {} sheets".format(name, len(sheet_elements))
    except Exception as ex:
        return False, "Error saving set: {}".format(str(ex))
    finally:
        __revit__.DialogBoxShowing -= _suppress_dialog


def delete_print_set(element_id):
    try:
        with Transaction(doc, "Print Set Manager - Delete Set") as t:
            t.Start()
            doc.Delete(element_id)
            t.Commit()
        return True, "Set deleted."
    except Exception as ex:
        return False, "Error deleting set: {}".format(str(ex))


# -- Shared data --------------------------------------------------------------
class AppData(object):
    def __init__(self):
        self.refresh()

    def refresh(self):
        self.joinery_room_nos = get_joinery_room_numbers()
        self.bases            = get_joinery_base_numbers(self.joinery_room_nos)
        self.all_sheets_map   = get_all_sheets_map()
        self.joinery_sheets   = expand_series(self.bases, self.all_sheets_map)
        self.print_sets       = get_existing_print_sets()
        self.set_map          = sheet_to_set_map(self.print_sets)


# -- WPF helpers --------------------------------------------------------------
def make_button(text, fg, border, width=110, height=28, margin=(8,0,0,0)):
    b = Button()
    b.Content         = text
    b.Width           = width
    b.Height          = height
    b.Margin          = Thickness(*margin)
    b.Background      = BTN_BG
    b.Foreground      = fg
    b.BorderBrush     = border
    b.BorderThickness = Thickness(1)
    return b


def make_label(text, fg=None, size=11, weight=None, margin=(0,0,0,0)):
    tb = TextBlock()
    tb.Text       = text
    tb.Foreground = fg or TEXT
    tb.FontSize   = size
    if weight:
        tb.FontWeight = weight
    tb.Margin    = Thickness(*margin)
    tb.VerticalAlignment = VerticalAlignment.Center
    return tb


def section_header(text):
    tb = TextBlock()
    tb.Text       = text
    tb.Foreground = ACCENT
    tb.FontSize   = 10
    tb.FontWeight = FontWeights.SemiBold
    tb.Margin     = Thickness(0, 8, 0, 4)
    return tb


# -- Main Window --------------------------------------------------------------
class PrintSetManagerWindow(Window):
    def __init__(self, data):
        self._data       = data
        self._editing_id = None
        self._scan_done  = False

        self.Title      = "Print Set Manager  {}".format(VERSION)
        self.Width      = 1050
        self.Height     = 660
        self.MinWidth   = 840
        self.MinHeight  = 520
        self.Background = BG
        self.ResizeMode = ResizeMode.CanResize

        self._build_ui()
        self._refresh_sets_list()
        self._refresh_scan_set_list()

    # -------------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------------
    def _build_ui(self):
        root = Grid()
        root.Margin = Thickness(12)
        for h in (36, -1, 28):
            rd = RowDefinition()
            rd.Height = GridLength(1, GridUnitType.Star) if h == -1 else GridLength(h)
            root.RowDefinitions.Add(rd)
        self.Content = root

        # Title bar
        title_panel = StackPanel()
        title_panel.Orientation = Orientation.Horizontal
        title_panel.VerticalAlignment = VerticalAlignment.Center
        Grid.SetRow(title_panel, 0)
        root.Children.Add(title_panel)

        t = TextBlock()
        t.Text       = "Print Set Manager"
        t.Foreground = BLUE
        t.FontSize   = 14
        t.FontWeight = FontWeights.SemiBold
        t.VerticalAlignment = VerticalAlignment.Center
        title_panel.Children.Add(t)

        ver = TextBlock()
        ver.Text       = "  {}".format(VERSION)
        ver.Foreground = MUTED
        ver.FontSize   = 11
        ver.VerticalAlignment = VerticalAlignment.Center
        title_panel.Children.Add(ver)

        # Tab control
        tabs = TabControl()
        tabs.Background = PANEL
        tabs.BorderBrush = LINE
        Grid.SetRow(tabs, 1)
        root.Children.Add(tabs)

        def make_tab_header(text):
            tb = TextBlock()
            tb.Text       = text
            tb.Foreground = DARK
            tb.FontSize   = 11
            tb.FontWeight = FontWeights.SemiBold
            tb.Padding    = Thickness(4, 2, 4, 2)
            return tb

        tab1 = TabItem()
        tab1.Header  = make_tab_header("  Manage Sets  ")
        tab1.Content = self._build_manage_tab()
        tabs.Items.Add(tab1)

        tab2 = TabItem()
        tab2.Header  = make_tab_header("  New Sheets  ")
        tab2.Content = self._build_newsheets_tab()
        tabs.Items.Add(tab2)

        # Status bar
        self._status = TextBlock()
        self._status.Text       = "Ready"
        self._status.Foreground = MUTED
        self._status.FontSize   = 11
        self._status.VerticalAlignment = VerticalAlignment.Center
        self._status.Margin     = Thickness(4, 0, 0, 0)
        Grid.SetRow(self._status, 2)
        root.Children.Add(self._status)

    # -------------------------------------------------------------------------
    # Manage Sets tab
    # -------------------------------------------------------------------------
    def _build_manage_tab(self):
        outer = Grid()
        outer.Margin = Thickness(8)

        c0 = ColumnDefinition(); c0.Width = GridLength(260)
        c1 = ColumnDefinition(); c1.Width = GridLength(8)
        c2 = ColumnDefinition(); c2.Width = GridLength(1, GridUnitType.Star)
        for c in (c0, c1, c2):
            outer.ColumnDefinitions.Add(c)

        # LEFT: existing sets
        left = Grid()
        for h in (-1, 8, 32, 8, 28):
            rd = RowDefinition()
            rd.Height = GridLength(1, GridUnitType.Star) if h == -1 else GridLength(h)
            left.RowDefinitions.Add(rd)
        Grid.SetColumn(left, 0)
        outer.Children.Add(left)

        lbl_sets = section_header("EXISTING PRINT SETS")
        Grid.SetRow(lbl_sets, 0)
        left.Children.Add(lbl_sets)

        self._sets_listbox = ListBox()
        self._sets_listbox.Background        = DARK
        self._sets_listbox.Foreground        = TEXT
        self._sets_listbox.BorderBrush       = LINE
        self._sets_listbox.BorderThickness   = Thickness(1)
        self._sets_listbox.SelectionChanged += self._on_set_selected
        Grid.SetRow(self._sets_listbox, 0)
        left.Children.Add(self._sets_listbox)

        name_lbl = make_label("Set name:", MUTED, size=10)
        Grid.SetRow(name_lbl, 2)
        left.Children.Add(name_lbl)

        self._name_box = TextBox()
        self._name_box.Background      = DARK
        self._name_box.Foreground      = TEXT
        self._name_box.BorderBrush     = LINE
        self._name_box.BorderThickness = Thickness(1)
        self._name_box.Padding         = Thickness(4, 2, 4, 2)
        self._name_box.FontSize        = 12
        Grid.SetRow(self._name_box, 4)
        left.Children.Add(self._name_box)

        # RIGHT: sheet checklist
        right = Grid()
        for h in (28, -1, 8, 28):
            rd = RowDefinition()
            rd.Height = GridLength(1, GridUnitType.Star) if h == -1 else GridLength(h)
            right.RowDefinitions.Add(rd)
        Grid.SetColumn(right, 2)
        outer.Children.Add(right)

        top_bar = StackPanel()
        top_bar.Orientation       = Orientation.Horizontal
        top_bar.VerticalAlignment = VerticalAlignment.Center
        Grid.SetRow(top_bar, 0)
        right.Children.Add(top_bar)

        self._sheet_count_lbl = TextBlock()
        self._sheet_count_lbl.Foreground        = MUTED
        self._sheet_count_lbl.FontSize          = 10
        self._sheet_count_lbl.VerticalAlignment = VerticalAlignment.Center
        top_bar.Children.Add(self._sheet_count_lbl)

        self._manage_filter = TextBox()
        self._manage_filter.Background      = DARK
        self._manage_filter.Foreground      = TEXT
        self._manage_filter.BorderBrush     = LINE
        self._manage_filter.BorderThickness = Thickness(1)
        self._manage_filter.Padding         = Thickness(4, 1, 4, 1)
        self._manage_filter.FontSize        = 11
        self._manage_filter.Width           = 140
        self._manage_filter.Height          = 22
        self._manage_filter.Margin          = Thickness(12, 0, 0, 0)
        self._manage_filter.ToolTip         = "Filter by sheet number or title"
        self._manage_filter.TextChanged    += self._on_manage_filter_changed
        top_bar.Children.Add(self._manage_filter)

        filter_hint = make_label("filter sheets...", MUTED, size=10, margin=(0,0,0,0))
        # overlay hint is simpler as a watermark-style placeholder via tag
        # (we'll hide it on TextChanged)

        sel_all_btn = make_button("Select All", MUTED, LINE, width=80, height=22, margin=(8,0,0,0))
        sel_all_btn.FontSize = 10
        sel_all_btn.Click   += self._on_select_all
        top_bar.Children.Add(sel_all_btn)

        desel_all_btn = make_button("Deselect All", MUTED, LINE, width=84, height=22, margin=(4,0,0,0))
        desel_all_btn.FontSize = 10
        desel_all_btn.Click   += self._on_deselect_all
        top_bar.Children.Add(desel_all_btn)

        sv = ScrollViewer()
        sv.Background      = DARK
        sv.BorderBrush     = LINE
        sv.BorderThickness = Thickness(1)
        Grid.SetRow(sv, 1)
        right.Children.Add(sv)

        self._sheet_panel = StackPanel()
        self._sheet_panel.Background = DARK
        sv.Content = self._sheet_panel

        self._populate_sheet_checklist()

        btn_row = StackPanel()
        btn_row.Orientation         = Orientation.Horizontal
        btn_row.HorizontalAlignment = HorizontalAlignment.Right
        Grid.SetRow(btn_row, 3)
        right.Children.Add(btn_row)

        self._new_btn = make_button("New Set", TEXT, LINE)
        self._new_btn.Click += self._on_new_set
        btn_row.Children.Add(self._new_btn)

        self._save_btn = make_button("Save Set", ACCENT, ACCENT)
        self._save_btn.Click += self._on_save_set
        btn_row.Children.Add(self._save_btn)

        self._del_btn = make_button("Delete Set", ERROR, ERROR)
        self._del_btn.Click += self._on_delete_set
        btn_row.Children.Add(self._del_btn)

        return outer

    # -------------------------------------------------------------------------
    # New Sheets tab
    # -------------------------------------------------------------------------
    def _build_newsheets_tab(self):
        """
        Fully independent scan tab.
        Left: list of all print sets (pick one to scan against).
        Right: scan results -- joinery sheets not yet in the selected set.
        Bottom: Add to Set button.
        """
        outer = Grid()
        outer.Margin = Thickness(8)

        # Two columns: left set list | right scan area
        c0 = ColumnDefinition(); c0.Width = GridLength(260)
        c1 = ColumnDefinition(); c1.Width = GridLength(8)
        c2 = ColumnDefinition(); c2.Width = GridLength(1, GridUnitType.Star)
        for c in (c0, c1, c2):
            outer.ColumnDefinitions.Add(c)

        # ---- LEFT: print set list ----
        left = Grid()
        for h in (24, -1):
            rd = RowDefinition()
            rd.Height = GridLength(1, GridUnitType.Star) if h == -1 else GridLength(h)
            left.RowDefinitions.Add(rd)
        Grid.SetColumn(left, 0)
        outer.Children.Add(left)

        hdr_lbl = section_header("SELECT A PRINT SET TO SCAN")
        Grid.SetRow(hdr_lbl, 0)
        left.Children.Add(hdr_lbl)

        self._scan_set_listbox = ListBox()
        self._scan_set_listbox.Background        = DARK
        self._scan_set_listbox.Foreground        = TEXT
        self._scan_set_listbox.BorderBrush       = LINE
        self._scan_set_listbox.BorderThickness   = Thickness(1)
        self._scan_set_listbox.SelectionChanged += self._on_scan_set_selected
        Grid.SetRow(self._scan_set_listbox, 1)
        left.Children.Add(self._scan_set_listbox)

        # ---- RIGHT: scan area ----
        right = Grid()
        for h in (32, -1, 8, 28):
            rd = RowDefinition()
            rd.Height = GridLength(1, GridUnitType.Star) if h == -1 else GridLength(h)
            right.RowDefinitions.Add(rd)
        Grid.SetColumn(right, 2)
        outer.Children.Add(right)

        # Row 0: instruction + scan/select buttons
        ctrl_row = StackPanel()
        ctrl_row.Orientation       = Orientation.Horizontal
        ctrl_row.VerticalAlignment = VerticalAlignment.Center
        Grid.SetRow(ctrl_row, 0)
        right.Children.Add(ctrl_row)

        self._scan_hint_lbl = TextBlock()
        self._scan_hint_lbl.Foreground        = MUTED
        self._scan_hint_lbl.FontSize          = 11
        self._scan_hint_lbl.VerticalAlignment = VerticalAlignment.Center
        self._scan_hint_lbl.Text = "Pick a print set on the left, then click Scan Now."
        ctrl_row.Children.Add(self._scan_hint_lbl)

        scan_btn = make_button("  Scan Now  ", BLUE, BLUE, width=100, margin=(12,0,0,0))
        scan_btn.Click += self._on_scan
        ctrl_row.Children.Add(scan_btn)

        self._scan_filter = TextBox()
        self._scan_filter.Background      = DARK
        self._scan_filter.Foreground      = TEXT
        self._scan_filter.BorderBrush     = LINE
        self._scan_filter.BorderThickness = Thickness(1)
        self._scan_filter.Padding         = Thickness(4, 1, 4, 1)
        self._scan_filter.FontSize        = 11
        self._scan_filter.Width           = 140
        self._scan_filter.Height          = 22
        self._scan_filter.Margin          = Thickness(12, 0, 0, 0)
        self._scan_filter.ToolTip         = "Filter by sheet number or title"
        self._scan_filter.TextChanged    += self._on_scan_filter_changed
        ctrl_row.Children.Add(self._scan_filter)

        sel_all_btn2 = make_button("Select All", MUTED, LINE, width=80, height=22, margin=(8,0,0,0))
        sel_all_btn2.FontSize = 10
        sel_all_btn2.Click   += self._on_scan_select_all
        ctrl_row.Children.Add(sel_all_btn2)

        desel_all_btn2 = make_button("Deselect All", MUTED, LINE, width=84, height=22, margin=(4,0,0,0))
        desel_all_btn2.FontSize = 10
        desel_all_btn2.Click   += self._on_scan_deselect_all
        ctrl_row.Children.Add(desel_all_btn2)

        # Row 1: scan results scroll
        sv2 = ScrollViewer()
        sv2.Background      = DARK
        sv2.BorderBrush     = LINE
        sv2.BorderThickness = Thickness(1)
        Grid.SetRow(sv2, 1)
        right.Children.Add(sv2)

        self._scan_panel = StackPanel()
        self._scan_panel.Background = DARK
        sv2.Content = self._scan_panel

        # Row 3: Add to Set button
        bottom = StackPanel()
        bottom.Orientation         = Orientation.Horizontal
        bottom.HorizontalAlignment = HorizontalAlignment.Right
        bottom.VerticalAlignment   = VerticalAlignment.Center
        Grid.SetRow(bottom, 3)
        right.Children.Add(bottom)

        self._scan_add_lbl = make_label("", MUTED, size=10, margin=(0,0,8,0))
        bottom.Children.Add(self._scan_add_lbl)

        self._add_btn = make_button("Add to Set", ACCENT, ACCENT, width=100, margin=(0,0,0,0))
        self._add_btn.IsEnabled = False
        self._add_btn.Click    += self._on_add_to_set
        bottom.Children.Add(self._add_btn)

        return outer

    # -------------------------------------------------------------------------
    # Sheet checklist helpers (Manage Sets tab)
    # -------------------------------------------------------------------------
    def _populate_sheet_checklist(self, checked_ids=None, highlight_ids=None):
        self._sheet_panel.Children.Clear()
        checked_ids   = checked_ids   or set()
        highlight_ids = highlight_ids or set()
        check_all     = (checked_ids == set())

        checked_count = 0
        filt = (self._manage_filter.Text or "").strip().lower() if hasattr(self, "_manage_filter") else ""

        for info in self._data.joinery_sheets:
            sid        = info["sheet_id"]
            is_checked = check_all or (sid in checked_ids)
            in_other   = sid in highlight_ids

            # Filter: prefix-match on sheet number, OR substring match on title only
            # (sheet number is prefix-matched so "80" won't match "8559" via a title room code)
            if filt:
                no_match    = info["sheet_no"].lower().startswith(filt)
                title_match = filt in info["title"].lower()
                if not no_match and not title_match:
                    continue

            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin      = Thickness(4, 2, 4, 2)

            cb = CheckBox()
            cb.IsChecked         = is_checked
            cb.VerticalAlignment = VerticalAlignment.Center
            cb.Margin            = Thickness(0, 0, 8, 0)
            cb.Tag               = sid
            cb.Checked          += self._on_sheet_checked
            cb.Unchecked        += self._on_sheet_checked
            row.Children.Add(cb)

            no_lbl = TextBlock()
            no_lbl.Text              = info["sheet_no"]
            no_lbl.Foreground        = WARNING if in_other else TEXT
            no_lbl.FontSize          = 11
            no_lbl.Width             = 120
            no_lbl.VerticalAlignment = VerticalAlignment.Center
            row.Children.Add(no_lbl)

            title_lbl = TextBlock()
            title_lbl.Text              = info["title"]
            title_lbl.Foreground        = WARNING if in_other else MUTED
            title_lbl.FontSize          = 11
            title_lbl.VerticalAlignment = VerticalAlignment.Center
            row.Children.Add(title_lbl)

            set_names = self._data.set_map.get(sid, [])
            if set_names and not in_other:
                badge = TextBlock()
                badge.Text              = u"  * " + ", ".join(set_names)
                badge.Foreground        = SUCCESS
                badge.FontSize          = 10
                badge.VerticalAlignment = VerticalAlignment.Center
                row.Children.Add(badge)
            elif in_other:
                badge = TextBlock()
                badge.Text              = u"  ! Not in this set"
                badge.Foreground        = WARNING
                badge.FontSize          = 10
                badge.VerticalAlignment = VerticalAlignment.Center
                row.Children.Add(badge)

            self._sheet_panel.Children.Add(row)
            if is_checked:
                checked_count += 1

        self._update_sheet_count(checked_count)

    def _update_sheet_count(self, count=None):
        if count is None:
            count = sum(
                1 for i in range(self._sheet_panel.Children.Count)
                for row in [self._sheet_panel.Children[i]]
                if isinstance(row, StackPanel)
                and row.Children.Count > 0
                and isinstance(row.Children[0], CheckBox)
                and row.Children[0].IsChecked
            )
        visible = self._sheet_panel.Children.Count
        total   = len(self._data.joinery_sheets)
        if visible < total:
            self._sheet_count_lbl.Text = "JOINERY SHEETS  -  {} of {} selected  ({} shown)".format(
                count, total, visible)
        else:
            self._sheet_count_lbl.Text = "JOINERY SHEETS  -  {} of {} selected".format(count, total)

    def _on_select_all(self, sender, e):
        # Only affects visible (filtered) rows -- hidden rows keep their state
        for i in range(self._sheet_panel.Children.Count):
            row = self._sheet_panel.Children[i]
            if isinstance(row, StackPanel) and row.Children.Count > 0:
                cb = row.Children[0]
                if isinstance(cb, CheckBox):
                    cb.IsChecked = True
        self._update_sheet_count()

    def _on_deselect_all(self, sender, e):
        # Only affects visible (filtered) rows -- hidden rows keep their state
        for i in range(self._sheet_panel.Children.Count):
            row = self._sheet_panel.Children[i]
            if isinstance(row, StackPanel) and row.Children.Count > 0:
                cb = row.Children[0]
                if isinstance(cb, CheckBox):
                    cb.IsChecked = False
        self._update_sheet_count()

    def _on_scan_select_all(self, sender, e):
        for i in range(self._scan_panel.Children.Count):
            row = self._scan_panel.Children[i]
            if isinstance(row, StackPanel) and row.Children.Count > 0 \
                    and row.Visibility == Visibility.Visible:
                cb = row.Children[0]
                if isinstance(cb, CheckBox):
                    cb.IsChecked = True

    def _on_scan_deselect_all(self, sender, e):
        for i in range(self._scan_panel.Children.Count):
            row = self._scan_panel.Children[i]
            if isinstance(row, StackPanel) and row.Children.Count > 0 \
                    and row.Visibility == Visibility.Visible:
                cb = row.Children[0]
                if isinstance(cb, CheckBox):
                    cb.IsChecked = False

    def _on_sheet_checked(self, sender, e):
        self._update_sheet_count()

    def _on_manage_filter_changed(self, sender, e):
        """Re-render the checklist respecting current filter text."""
        # Preserve current check states before re-rendering
        checked = self._get_checked_sheet_ids()
        # Also collect ids that are NOT shown (hidden by filter) to preserve their state
        filt = (self._manage_filter.Text or "").strip().lower()
        if filt:
            hidden_ids = set(
                info["sheet_id"] for info in self._data.joinery_sheets
                if not info["sheet_no"].lower().startswith(filt)
                and filt not in info["title"].lower()
            )
        else:
            hidden_ids = set()
        # Merge: visible checked + previously hidden checked
        if not checked and not hidden_ids:
            # All were checked (check_all mode) -- keep that
            self._populate_sheet_checklist(
                checked_ids=set(),
                highlight_ids=self._current_highlight_ids()
            )
        else:
            all_checked = checked | (self._get_all_checked_ids_including_hidden())
            self._populate_sheet_checklist(
                checked_ids=all_checked,
                highlight_ids=self._current_highlight_ids()
            )

    def _current_highlight_ids(self):
        """Return the current unassigned highlight set (sheets not in loaded set)."""
        if self._editing_id is None:
            return set()
        ps = next((p for p in self._data.print_sets if p["id"] == self._editing_id), None)
        if ps is None:
            return set()
        in_set = ps["sheet_ids"]
        return set(info["sheet_id"] for info in self._data.joinery_sheets
                   if info["sheet_id"] not in in_set)

    def _get_all_checked_ids_including_hidden(self):
        """Return checked ids from visible rows only (hidden rows not present in panel)."""
        return self._get_checked_sheet_ids()

    def _on_scan_filter_changed(self, sender, e):
        """Re-render scan results respecting filter text."""
        self._apply_scan_filter()

    def _apply_scan_filter(self):
        """Show/hide scan result rows based on filter text."""
        filt = (self._scan_filter.Text or "").strip().lower() if hasattr(self, "_scan_filter") else ""
        for i in range(self._scan_panel.Children.Count):
            row = self._scan_panel.Children[i]
            if not isinstance(row, StackPanel) or row.Children.Count < 2:
                continue
            cb = row.Children[0]
            if not isinstance(cb, CheckBox):
                continue
            # second child is the sheet number label
            no_lbl    = row.Children[1] if row.Children.Count > 1 else None
            title_lbl = row.Children[2] if row.Children.Count > 2 else None
            sheet_no  = no_lbl.Text    if no_lbl    else ""
            title     = title_lbl.Text if title_lbl else ""
            if filt and not sheet_no.lower().startswith(filt) and filt not in title.lower():
                row.Visibility = Visibility.Collapsed
            else:
                row.Visibility = Visibility.Visible

    def _get_checked_sheet_ids(self):
        ids = set()
        for i in range(self._sheet_panel.Children.Count):
            row = self._sheet_panel.Children[i]
            if isinstance(row, StackPanel) and row.Children.Count > 0:
                cb = row.Children[0]
                if isinstance(cb, CheckBox) and cb.IsChecked:
                    ids.add(cb.Tag)
        return ids

    # -------------------------------------------------------------------------
    # Manage Sets list helpers
    # -------------------------------------------------------------------------
    def _refresh_sets_list(self):
        self._sets_listbox.Items.Clear()
        for ps in self._data.print_sets:
            item = ListBoxItem()
            item.Background = DARK
            item.Foreground = TEXT
            item.Tag        = ps

            sp = StackPanel()
            sp.Orientation = Orientation.Horizontal

            name_lbl = TextBlock()
            name_lbl.Text       = ps["name"]
            name_lbl.FontSize   = 12
            name_lbl.Foreground = TEXT
            sp.Children.Add(name_lbl)

            count_lbl = TextBlock()
            count_lbl.Text              = "  ({} sheets)".format(len(ps["sheet_ids"]))
            count_lbl.FontSize          = 10
            count_lbl.Foreground        = MUTED
            count_lbl.VerticalAlignment = VerticalAlignment.Center
            sp.Children.Add(count_lbl)

            item.Content = sp
            self._sets_listbox.Items.Add(item)

    # -------------------------------------------------------------------------
    # New Sheets set list helpers
    # -------------------------------------------------------------------------
    def _refresh_scan_set_list(self):
        """Populate the print-set list on the New Sheets tab."""
        prev_id = None
        if self._scan_set_listbox.SelectedItem is not None:
            prev_id = self._scan_set_listbox.SelectedItem.Tag.get("id")

        self._scan_set_listbox.Items.Clear()
        restore_item = None
        for ps in self._data.print_sets:
            item = ListBoxItem()
            item.Background = DARK
            item.Foreground = TEXT
            item.Tag        = ps

            sp = StackPanel()
            sp.Orientation = Orientation.Horizontal

            name_lbl = TextBlock()
            name_lbl.Text       = ps["name"]
            name_lbl.FontSize   = 12
            name_lbl.Foreground = TEXT
            sp.Children.Add(name_lbl)

            count_lbl = TextBlock()
            count_lbl.Text              = "  ({} sheets)".format(len(ps["sheet_ids"]))
            count_lbl.FontSize          = 10
            count_lbl.Foreground        = MUTED
            count_lbl.VerticalAlignment = VerticalAlignment.Center
            sp.Children.Add(count_lbl)

            item.Content = sp
            self._scan_set_listbox.Items.Add(item)

            if prev_id is not None and ps["id"] == prev_id:
                restore_item = item

        if restore_item is not None:
            self._scan_set_listbox.SelectedItem = restore_item

    def _get_scan_selected_ps(self):
        """Return the print_set dict currently selected on the New Sheets tab, or None."""
        item = self._scan_set_listbox.SelectedItem
        if item is None:
            return None
        return item.Tag

    def _on_scan_set_selected(self, sender, e):
        ps = self._get_scan_selected_ps()
        if ps is None:
            return
        self._scan_add_lbl.Text   = "Add checked sheets to '{}':".format(ps["name"])
        self._add_btn.IsEnabled   = False   # require a fresh scan after switching sets
        self._scan_done           = False
        self._scan_hint_lbl.Text  = "Set selected. Click 'Scan Now' to find missing joinery sheets."
        self._scan_panel.Children.Clear()
        self._scan_panel.Children.Add(make_label(
            "Press 'Scan Now' to check which joinery sheets are not yet in '{}'.".format(ps["name"]),
            MUTED, size=11, margin=(12, 12, 0, 0)))

    # -------------------------------------------------------------------------
    # Manage Sets event handlers
    # -------------------------------------------------------------------------
    def _on_set_selected(self, sender, e):
        item = self._sets_listbox.SelectedItem
        if item is None:
            return
        ps = item.Tag
        self._editing_id    = ps["id"]
        self._name_box.Text = ps["name"]

        in_set         = ps["sheet_ids"]
        unassigned_ids = set(
            info["sheet_id"] for info in self._data.joinery_sheets
            if info["sheet_id"] not in in_set
        )

        self._populate_sheet_checklist(
            checked_ids=in_set,
            highlight_ids=unassigned_ids
        )
        self._set_status("Editing set: '{}'  ({} sheets)".format(ps["name"], len(in_set)), BLUE)

    def _on_new_set(self, sender, e):
        self._editing_id    = None
        self._name_box.Text = ""
        self._sets_listbox.SelectedItem = None
        self._populate_sheet_checklist()
        self._set_status("New set - enter a name and select sheets.", MUTED)

    def _on_save_set(self, sender, e):
        name = (self._name_box.Text or "").strip()
        if not name:
            self._set_status("Enter a set name before saving.", ERROR)
            return

        checked_ids = self._get_checked_sheet_ids()
        if not checked_ids:
            self._set_status("No sheets selected.", WARNING)
            return

        existing_names = {ps["name"]: ps for ps in self._data.print_sets}
        if self._editing_id is None and name in existing_names:
            res = MessageBox.Show(
                "A print set named '{}' already exists.\nOverwrite it?".format(name),
                "Overwrite?",
                MessageBoxButton.YesNo)
            if res != MessageBoxResult.Yes:
                return
            self._editing_id = existing_names[name]["id"]

        sheet_elements = [
            self._data.all_sheets_map[no]
            for info in self._data.joinery_sheets
            if info["sheet_id"] in checked_ids
            for no in [info["sheet_no"]]
            if no in self._data.all_sheets_map
        ]

        ok, msg = save_print_set(name, sheet_elements, self._editing_id)
        if ok:
            self._data.refresh()
            self._refresh_sets_list()
            self._refresh_scan_set_list()
            self._populate_sheet_checklist()
            self._editing_id    = None
            self._name_box.Text = ""
            self._set_status(msg, SUCCESS)
        else:
            self._set_status(msg, ERROR)

    def _on_delete_set(self, sender, e):
        if self._editing_id is None:
            self._set_status("Select a set to delete.", WARNING)
            return
        name = self._name_box.Text or "this set"
        res = MessageBox.Show(
            "Delete print set '{}'?\nThis cannot be undone.".format(name),
            "Confirm Delete",
            MessageBoxButton.YesNo)
        if res != MessageBoxResult.Yes:
            return
        ok, msg = delete_print_set(self._editing_id)
        if ok:
            self._data.refresh()
            self._refresh_sets_list()
            self._refresh_scan_set_list()
            self._populate_sheet_checklist()
            self._editing_id    = None
            self._name_box.Text = ""
            self._set_status(msg, SUCCESS)
        else:
            self._set_status(msg, ERROR)

    # -------------------------------------------------------------------------
    # New Sheets tab event handlers
    # -------------------------------------------------------------------------
    def _on_scan(self, sender, e):
        ps = self._get_scan_selected_ps()
        if ps is None:
            self._set_status("Select a print set from the list on the left first.", WARNING)
            return

        self._data.refresh()
        self._refresh_sets_list()
        self._refresh_scan_set_list()

        # Re-fetch the set (may have changed after refresh)
        ps = self._get_scan_selected_ps()
        if ps is None:
            self._set_status("The selected set no longer exists.", ERROR)
            return

        set_name = ps["name"]
        in_set   = ps["sheet_ids"]

        missing = [
            info for info in self._data.joinery_sheets
            if info["sheet_id"] not in in_set
        ]

        self._scan_panel.Children.Clear()
        self._scan_done = True

        if not missing:
            self._scan_panel.Children.Add(make_label(
                u"All joinery sheets are already in '{}'.".format(set_name),
                SUCCESS, size=12, margin=(12, 12, 0, 0)))
            self._scan_hint_lbl.Text = "Scan complete - no missing sheets."
            self._scan_add_lbl.Text  = "Add checked sheets to '{}':".format(set_name)
            self._add_btn.IsEnabled  = False
            self._set_status("Scan complete - no missing sheets.", SUCCESS)
            return

        self._scan_panel.Children.Add(make_label(
            "{} joinery sheet{} not yet in '{}':".format(
                len(missing), "s" if len(missing) != 1 else "", set_name),
            WARNING, size=11, margin=(8, 8, 0, 6)))

        for info in missing:
            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin      = Thickness(8, 2, 8, 2)

            cb = CheckBox()
            cb.IsChecked         = True
            cb.VerticalAlignment = VerticalAlignment.Center
            cb.Margin            = Thickness(0, 0, 8, 0)
            cb.Tag               = info["sheet_id"]
            row.Children.Add(cb)

            no_lbl = TextBlock()
            no_lbl.Text              = info["sheet_no"]
            no_lbl.Foreground        = WARNING
            no_lbl.FontSize          = 11
            no_lbl.Width             = 130
            no_lbl.VerticalAlignment = VerticalAlignment.Center
            row.Children.Add(no_lbl)

            title_lbl = TextBlock()
            title_lbl.Text              = info["title"]
            title_lbl.Foreground        = TEXT
            title_lbl.FontSize          = 11
            title_lbl.VerticalAlignment = VerticalAlignment.Center
            row.Children.Add(title_lbl)

            self._scan_panel.Children.Add(row)

        self._scan_hint_lbl.Text = "Scan complete - {} sheet{} missing.".format(
            len(missing), "s" if len(missing) != 1 else "")
        self._scan_add_lbl.Text  = "Add checked sheets to '{}':".format(set_name)
        self._add_btn.IsEnabled  = True
        # Apply any existing filter text to new results
        self._apply_scan_filter()
        self._set_status(
            "Scan complete - {} sheet{} missing from '{}'.".format(
                len(missing), "s" if len(missing) != 1 else "", set_name),
            WARNING)

    def _on_add_to_set(self, sender, e):
        if not self._scan_done:
            self._set_status("Run 'Scan Now' first.", WARNING)
            return

        ps = self._get_scan_selected_ps()
        if ps is None:
            self._set_status("No set selected.", WARNING)
            return

        new_ids = set()
        for i in range(self._scan_panel.Children.Count):
            row = self._scan_panel.Children[i]
            if isinstance(row, StackPanel) and row.Children.Count > 0:
                cb = row.Children[0]
                if isinstance(cb, CheckBox) and cb.IsChecked:
                    new_ids.add(cb.Tag)

        if not new_ids:
            self._set_status("No sheets checked to add.", WARNING)
            return

        existing_sheet_elements = [
            self._data.all_sheets_map[no]
            for no, sheet in self._data.all_sheets_map.items()
            if sheet.Id in ps["sheet_ids"]
        ]
        new_sheet_elements = [
            self._data.all_sheets_map[info["sheet_no"]]
            for info in self._data.joinery_sheets
            if info["sheet_id"] in new_ids
            and info["sheet_no"] in self._data.all_sheets_map
        ]
        all_elements = list({s.Id: s for s in existing_sheet_elements + new_sheet_elements}.values())

        ok, msg = save_print_set(ps["name"], all_elements, ps["id"])
        if ok:
            self._data.refresh()
            self._refresh_sets_list()
            self._refresh_scan_set_list()
            self._scan_panel.Children.Clear()
            self._scan_panel.Children.Add(make_label(
                "Sheets added to '{}'. Press 'Scan Now' to recheck.".format(ps["name"]),
                SUCCESS, size=11, margin=(12, 12, 0, 0)))
            self._scan_done        = False
            self._add_btn.IsEnabled = False
            self._set_status(msg, SUCCESS)
        else:
            self._set_status(msg, ERROR)

    # -------------------------------------------------------------------------
    # Status bar
    # -------------------------------------------------------------------------
    def _set_status(self, msg, colour=None):
        self._status.Text       = msg
        self._status.Foreground = colour or MUTED


# -- Entry point --------------------------------------------------------------
try:
    data = AppData()
except Exception as ex:
    MessageBox.Show(
        "Failed to load model data:\n{}".format(str(ex)),
        "Print Set Manager",
        MessageBoxButton.OK)
    raise

if not data.joinery_sheets:
    MessageBox.Show(
        "No joinery sheets found in the model.\n"
        "Ensure elements with AR_RLS_ITEM CODE starting with 'J' exist "
        "and have Room Fixture Schedules placed on sheets.",
        "Print Set Manager",
        MessageBoxButton.OK)
else:
    PrintSetManagerWindow(data).ShowDialog()
