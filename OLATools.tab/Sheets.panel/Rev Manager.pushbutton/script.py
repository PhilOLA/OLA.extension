# -*- coding: utf-8 -*-
"""
Revision Manager  v1.1.0
pyRevit IronPython pushbutton tool
Create, apply, and remove revisions on selected sheets.
"""

import clr
import sys
import datetime

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("System")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import System
from System.Collections.Generic import List
from Autodesk.Revit.DB import (
    FilteredElementCollector,
    ViewSheet,
    ViewSheetSet,
    Revision,
    ElementId,
    Transaction,
)
try:
    from Autodesk.Revit.DB import RevisionNumberType
    # Verify the enum values are accessible
    _ = RevisionNumberType.Numeric
    _ = RevisionNumberType.Alphanumeric
    _HAS_NUMBER_TYPE = True
except Exception:
    RevisionNumberType = None
    _HAS_NUMBER_TYPE = False
from System.Windows import (
    Window, WindowStartupLocation, Thickness, GridLength,
    GridUnitType, VerticalAlignment, HorizontalAlignment,
    MessageBox, MessageBoxButton, MessageBoxResult,
)
from System.Windows.Controls import (
    Grid, ColumnDefinition, RowDefinition, StackPanel, ScrollViewer,
    TextBlock, TextBox, ComboBox, ComboBoxItem, CheckBox, RadioButton,
    Button, ListBox, ListBoxItem,
    SelectionMode, Orientation,
    ScrollBarVisibility,
)
from System.Windows.Controls import Border as WpfBorder
from System.Windows import CornerRadius
from System.Windows.Media import SolidColorBrush, Color

try:
    from pyrevit import forms, script as pyscript
    logger = pyscript.get_logger()
except Exception:
    logger = None

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def hex_brush(hex_str):
    hex_str = hex_str.lstrip("#")
    a = 255
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    return SolidColorBrush(Color.FromArgb(a, r, g, b))

BG       = hex_brush("#1E1E1E")
PANEL    = hex_brush("#262626")
ACCENT   = hex_brush("#F4723A")   # orange — Create New Revision border + Remove mode
BLUE     = hex_brush("#4FC3F7")   # primary blue — title, Apply button, Apply Revision border
INFO     = hex_brush("#4FC3F7")   # alias of BLUE
SUCCESS  = hex_brush("#4DB6AC")   # teal — has-revision dot, sequence preview, success messages
TEXT     = hex_brush("#E0E0E0")
MUTED    = hex_brush("#888780")
WARNING  = hex_brush("#FFB74D")
DARK     = hex_brush("#181818")
WHITE    = hex_brush("#FFFFFF")
BTN_BG   = hex_brush("#2D2D2D")
SELECTED = hex_brush("#1E2A38")

VERSION  = u"v1.1.0"

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def get_all_sheets(doc):
    sheets = FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
    result = [s for s in sheets if not s.IsPlaceholder]
    return sorted(result, key=lambda s: s.SheetNumber)


def get_print_sets(doc):
    sets = FilteredElementCollector(doc).OfClass(ViewSheetSet).ToElements()
    return sorted(sets, key=lambda s: s.Name)


def get_revisions(doc):
    revs = FilteredElementCollector(doc).OfClass(Revision).ToElements()
    return sorted(revs, key=lambda r: r.SequenceNumber)


def get_numbering_mode_label(doc):
    revs = FilteredElementCollector(doc).OfClass(Revision).ToElements()
    if not revs:
        return u"Revision numbering: Per Project (no revisions yet)"
    try:
        _ = list(revs)[0].RevisionNumber
        return u"Revision numbering: Per Project"
    except Exception:
        return u"Revision numbering: Per Sheet  (numbers vary by sheet)"


def revision_label(rev):
    try:
        num = rev.RevisionNumber or str(rev.SequenceNumber)
    except Exception:
        num = str(rev.SequenceNumber)
    desc = rev.Description or u"(no description)"
    date = rev.RevisionDate or u""
    issued = u"  [ISSUED]" if rev.Issued else u""
    return u"Rev {0} — {1}  {2}{3}".format(num, desc, date, issued)


def sheet_has_revision(sheet, rev_id):
    return rev_id in sheet.GetAdditionalRevisionIds()


def next_sequence_number(doc):
    revs = get_revisions(doc)
    if not revs:
        return 1
    return revs[-1].SequenceNumber + 1


# ---------------------------------------------------------------------------
# _GroupBox helper
# ---------------------------------------------------------------------------

class _GroupBox(object):
    """Section box: coloured WpfBorder with header label inside at the top."""

    def __init__(self, header, color):
        self.panel = WpfBorder()
        self.panel.BorderBrush = color
        self.panel.BorderThickness = Thickness(2)
        self.panel.CornerRadius = CornerRadius(4)
        self.panel.Padding = Thickness(10, 8, 10, 10)
        self.panel.Background = hex_brush("#1A1A1A")
        self.panel.Margin = Thickness(0, 0, 0, 0)

        self._inner = StackPanel()
        self._inner.Orientation = Orientation.Vertical
        self.panel.Child = self._inner

        hdr = TextBlock()
        hdr.Text = header
        hdr.Foreground = color
        hdr.FontWeight = System.Windows.FontWeights.Bold
        hdr.FontSize = 13
        hdr.Margin = Thickness(0, 0, 0, 8)
        self._inner.Children.Add(hdr)

        self._content_holder = StackPanel()
        self._inner.Children.Add(self._content_holder)

    def _set_content(self, child):
        self._inner.Children.Remove(self._content_holder)
        self._inner.Children.Add(child)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class RevisionManagerWindow(Window):

    def __init__(self, doc, uidoc):
        self._doc = doc
        self._uidoc = uidoc
        self._all_sheets = get_all_sheets(doc)
        self._print_sets = get_print_sets(doc)
        self._revisions = get_revisions(doc)
        self._filtered_sheets = list(self._all_sheets)
        self._sheet_item_dots = {}   # ListBoxItem -> indicator TextBlock

        self._setup_window()
        self._build_ui()
        self._populate_print_sets()
        self._populate_revisions()
        self._show_all_sheets()

    # -----------------------------------------------------------------------
    # Window setup
    # -----------------------------------------------------------------------

    def _setup_window(self):
        self.Title = u"Revision Manager"
        self.Width = 900
        self.Height = 760
        self.MinWidth = 720
        self.MinHeight = 600
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.Background = BG
        self.Foreground = TEXT

    # -----------------------------------------------------------------------
    # Root layout
    # -----------------------------------------------------------------------

    def _build_ui(self):
        root = Grid()
        root.Margin = Thickness(12)
        root.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))
        root.RowDefinitions.Add(RowDefinition(Height=GridLength(1, GridUnitType.Star)))
        root.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))

        # Title
        title = TextBlock()
        title.Text = u"Revision Manager  " + VERSION
        title.FontSize = 18
        title.FontWeight = System.Windows.FontWeights.Bold
        title.Foreground = BLUE
        title.Margin = Thickness(0, 0, 0, 10)
        Grid.SetRow(title, 0)
        root.Children.Add(title)

        # Main two-column area
        main = Grid()
        main.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(1, GridUnitType.Star)))
        main.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(16)))
        main.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(340)))
        Grid.SetRow(main, 1)
        root.Children.Add(main)

        self._build_sheet_panel(main)
        self._build_right_panel(main)
        self._build_apply_bar(root)

        self.Content = root

    # -----------------------------------------------------------------------
    # Left panel — sheet list
    # -----------------------------------------------------------------------

    def _build_sheet_panel(self, parent):
        panel = Grid()
        panel.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # row 0: print set filter
        panel.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # row 1: legend + select buttons
        panel.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # row 2: search box
        panel.RowDefinitions.Add(RowDefinition(Height=GridLength(1, GridUnitType.Star)))  # row 3: list
        Grid.SetColumn(panel, 0)
        parent.Children.Add(panel)

        # Row 0 — print set filter
        filter_row = Grid()
        filter_row.Margin = Thickness(0, 0, 0, 6)
        filter_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength.Auto))
        filter_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(8)))
        filter_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(1, GridUnitType.Star)))
        filter_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(8)))
        filter_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength.Auto))

        filter_lbl = self._label(u"Filter by Print Set:")
        filter_lbl.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(filter_lbl, 0)
        filter_row.Children.Add(filter_lbl)

        self._print_set_combo = ComboBox()
        self._print_set_combo.Background = BTN_BG
        self._print_set_combo.Foreground = hex_brush("#1A1A1A")
        self._print_set_combo.SelectionChanged += self._on_print_set_changed
        Grid.SetColumn(self._print_set_combo, 2)
        filter_row.Children.Add(self._print_set_combo)

        clear_btn = self._small_btn(u"\u00d7 All Sheets")
        clear_btn.Click += self._on_clear_filter
        Grid.SetColumn(clear_btn, 4)
        filter_row.Children.Add(clear_btn)

        Grid.SetRow(filter_row, 0)
        panel.Children.Add(filter_row)

        # Row 1 — legend + select buttons + count
        sel_row = StackPanel()
        sel_row.Orientation = Orientation.Horizontal
        sel_row.Margin = Thickness(0, 0, 0, 4)

        # Dot legend — explained inline so the user knows what the dots mean
        legend_has = TextBlock()
        legend_has.Text = u"\u2713"
        legend_has.Foreground = SUCCESS
        legend_has.FontWeight = System.Windows.FontWeights.Bold
        legend_has.VerticalAlignment = VerticalAlignment.Center
        legend_has.Margin = Thickness(0, 0, 2, 0)
        sel_row.Children.Add(legend_has)

        legend_has_lbl = TextBlock()
        legend_has_lbl.Text = u"has revision"
        legend_has_lbl.Foreground = MUTED
        legend_has_lbl.FontSize = 10
        legend_has_lbl.VerticalAlignment = VerticalAlignment.Center
        legend_has_lbl.Margin = Thickness(0, 0, 10, 0)
        sel_row.Children.Add(legend_has_lbl)

        legend_no = TextBlock()
        legend_no.Text = u"\u2013"
        legend_no.Foreground = MUTED
        legend_no.FontWeight = System.Windows.FontWeights.Bold
        legend_no.VerticalAlignment = VerticalAlignment.Center
        legend_no.Margin = Thickness(0, 0, 2, 0)
        sel_row.Children.Add(legend_no)

        legend_no_lbl = TextBlock()
        legend_no_lbl.Text = u"no revision"
        legend_no_lbl.Foreground = MUTED
        legend_no_lbl.FontSize = 10
        legend_no_lbl.VerticalAlignment = VerticalAlignment.Center
        legend_no_lbl.Margin = Thickness(0, 0, 14, 0)
        sel_row.Children.Add(legend_no_lbl)

        # Divider
        div = TextBlock()
        div.Text = u"|"
        div.Foreground = MUTED
        div.VerticalAlignment = VerticalAlignment.Center
        div.Margin = Thickness(0, 0, 10, 0)
        sel_row.Children.Add(div)

        sel_all = self._small_btn(u"Select All")
        sel_all.Click += self._on_select_all
        sel_row.Children.Add(sel_all)

        sel_none = self._small_btn(u"Select None")
        sel_none.Margin = Thickness(6, 0, 0, 0)
        sel_none.Click += self._on_select_none
        sel_row.Children.Add(sel_none)

        # "Select sheets that already carry the chosen revision"
        sel_rev_btn = self._small_btn(u"\u2713 Select with Revision")
        sel_rev_btn.Margin = Thickness(6, 0, 0, 0)
        sel_rev_btn.Foreground = SUCCESS
        sel_rev_btn.Click += self._on_select_with_revision
        sel_row.Children.Add(sel_rev_btn)

        self._sheet_count_lbl = TextBlock()
        self._sheet_count_lbl.Foreground = MUTED
        self._sheet_count_lbl.Margin = Thickness(10, 0, 0, 0)
        self._sheet_count_lbl.VerticalAlignment = VerticalAlignment.Center
        sel_row.Children.Add(self._sheet_count_lbl)

        Grid.SetRow(sel_row, 1)
        panel.Children.Add(sel_row)

        # Row 2 — Search box
        search_row = Grid()
        search_row.Margin = Thickness(0, 0, 0, 4)
        search_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength.Auto))
        search_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(1, GridUnitType.Star)))

        search_lbl = TextBlock()
        search_lbl.Text = u"\U0001f50d "
        search_lbl.Foreground = MUTED
        search_lbl.VerticalAlignment = VerticalAlignment.Center
        search_lbl.Margin = Thickness(0, 0, 4, 0)
        Grid.SetColumn(search_lbl, 0)
        search_row.Children.Add(search_lbl)

        self._search_box = TextBox()
        self._search_box.Background = BTN_BG
        self._search_box.Foreground = TEXT
        self._search_box.BorderBrush = hex_brush("#444444")
        self._search_box.Padding = Thickness(4, 3, 4, 3)
        self._search_box.FontSize = 12
        self._search_box.Text = u""
        self._search_box.TextChanged += self._on_search_changed
        Grid.SetColumn(self._search_box, 1)
        search_row.Children.Add(self._search_box)

        Grid.SetRow(search_row, 2)
        panel.Children.Add(search_row)

        # Row 3 — sheet list
        self._sheet_list = ListBox()
        self._sheet_list.Background = DARK
        self._sheet_list.Foreground = TEXT
        self._sheet_list.BorderThickness = Thickness(1)
        self._sheet_list.BorderBrush = hex_brush("#333333")
        self._sheet_list.SelectionMode = SelectionMode.Extended
        ScrollViewer.SetHorizontalScrollBarVisibility(
            self._sheet_list, ScrollBarVisibility.Disabled)
        self._sheet_list.SelectionChanged += self._on_sheet_selection_changed
        Grid.SetRow(self._sheet_list, 3)
        panel.Children.Add(self._sheet_list)

    # -----------------------------------------------------------------------
    # Right panel — revision selector + new revision form
    # -----------------------------------------------------------------------

    def _build_right_panel(self, parent):
        panel = StackPanel()
        panel.Orientation = Orientation.Vertical
        Grid.SetColumn(panel, 2)
        parent.Children.Add(panel)

        # Numbering mode info
        num_mode_lbl = TextBlock()
        num_mode_lbl.Text = get_numbering_mode_label(self._doc)
        num_mode_lbl.Foreground = MUTED
        num_mode_lbl.FontSize = 11
        num_mode_lbl.TextWrapping = System.Windows.TextWrapping.Wrap
        num_mode_lbl.Margin = Thickness(0, 2, 0, 10)
        panel.Children.Add(num_mode_lbl)

        # --- Apply / Remove Revision section ---
        rev_group = _GroupBox(u"Select Revision", BLUE)
        rev_inner = StackPanel()
        rev_inner.Margin = Thickness(0)

        rev_lbl = self._label(u"Select revision:")
        rev_lbl.Margin = Thickness(0, 0, 0, 4)
        rev_inner.Children.Add(rev_lbl)

        self._revision_combo = ComboBox()
        self._revision_combo.Background = BTN_BG
        self._revision_combo.Foreground = hex_brush("#1A1A1A")
        self._revision_combo.Margin = Thickness(0, 0, 0, 8)
        self._revision_combo.SelectionChanged += self._on_revision_selected
        rev_inner.Children.Add(self._revision_combo)

        # Prominent preview card for selected revision
        preview_border = WpfBorder()
        preview_border.Background = hex_brush("#0D1E2E")
        preview_border.BorderBrush = BLUE
        preview_border.BorderThickness = Thickness(1)
        preview_border.CornerRadius = CornerRadius(3)
        preview_border.Padding = Thickness(8, 6, 8, 6)
        preview_border.Margin = Thickness(0, 0, 0, 0)

        preview_stack = StackPanel()
        preview_stack.Orientation = Orientation.Vertical

        self._rev_name_lbl = TextBlock()
        self._rev_name_lbl.Foreground = BLUE
        self._rev_name_lbl.FontSize = 13
        self._rev_name_lbl.FontWeight = System.Windows.FontWeights.Bold
        self._rev_name_lbl.TextWrapping = System.Windows.TextWrapping.Wrap
        self._rev_name_lbl.Text = u"\u2014 no revision selected \u2014"
        preview_stack.Children.Add(self._rev_name_lbl)

        self._rev_preview = TextBlock()
        self._rev_preview.Foreground = MUTED
        self._rev_preview.TextWrapping = System.Windows.TextWrapping.Wrap
        self._rev_preview.FontSize = 11
        self._rev_preview.Margin = Thickness(0, 3, 0, 0)
        preview_stack.Children.Add(self._rev_preview)

        preview_border.Child = preview_stack
        rev_inner.Children.Add(preview_border)

        rev_group._set_content(rev_inner)
        panel.Children.Add(rev_group.panel)

        panel.Children.Add(self._spacer(10))

        # --- Create New Revision section ---
        new_group = _GroupBox(u"Create New Revision", ACCENT)
        new_inner = StackPanel()
        new_inner.Margin = Thickness(0)

        self._seq_preview = TextBlock()
        self._seq_preview.Foreground = SUCCESS
        self._seq_preview.FontSize = 11
        self._seq_preview.Margin = Thickness(0, 0, 0, 8)
        new_inner.Children.Add(self._seq_preview)

        new_inner.Children.Add(self._label(u"Description *"))
        self._new_desc = TextBox()
        self._new_desc.Background = BTN_BG
        self._new_desc.Foreground = TEXT
        self._new_desc.Margin = Thickness(0, 2, 0, 8)
        self._new_desc.Padding = Thickness(4)
        self._new_desc.TextChanged += self._on_desc_changed
        new_inner.Children.Add(self._new_desc)

        new_inner.Children.Add(self._label(u"Date (e.g. 26/09/26)"))
        self._new_date = TextBox()
        self._new_date.Background = BTN_BG
        self._new_date.Foreground = TEXT
        self._new_date.Margin = Thickness(0, 2, 0, 8)
        self._new_date.Padding = Thickness(4)
        today = datetime.date.today()
        self._new_date.Text = u"{0}/{1}/{2}".format(today.day, today.strftime("%m"), today.strftime("%y"))
        new_inner.Children.Add(self._new_date)

        new_inner.Children.Add(self._label(u"Issued By"))
        self._new_issued_by = TextBox()
        self._new_issued_by.Background = BTN_BG
        self._new_issued_by.Foreground = TEXT
        self._new_issued_by.Margin = Thickness(0, 2, 0, 8)
        self._new_issued_by.Padding = Thickness(4)
        new_inner.Children.Add(self._new_issued_by)

        num_type_lbl = self._label(u"Numbering Type")
        num_type_row = StackPanel()
        num_type_row.Orientation = Orientation.Horizontal
        num_type_row.Margin = Thickness(0, 2, 0, 8)

        self._num_numeric = RadioButton()
        self._num_numeric.Content = u"Numeric"
        self._num_numeric.Foreground = TEXT
        self._num_numeric.IsChecked = True
        self._num_numeric.GroupName = u"NumType"
        num_type_row.Children.Add(self._num_numeric)

        self._num_alpha = RadioButton()
        self._num_alpha.Content = u"Alphanumeric"
        self._num_alpha.Foreground = TEXT
        self._num_alpha.IsChecked = False
        self._num_alpha.GroupName = u"NumType"
        self._num_alpha.Margin = Thickness(14, 0, 0, 0)
        num_type_row.Children.Add(self._num_alpha)

        if _HAS_NUMBER_TYPE:
            new_inner.Children.Add(num_type_lbl)
            new_inner.Children.Add(num_type_row)
        else:
            # API doesn't support NumberType on this Revit version — hide controls
            num_type_lbl.Visibility = System.Windows.Visibility.Collapsed
            num_type_row.Visibility = System.Windows.Visibility.Collapsed

        self._issued_chk = CheckBox()
        self._issued_chk.Content = u"Mark as Issued immediately"
        self._issued_chk.Foreground = TEXT
        self._issued_chk.Margin = Thickness(0, 0, 0, 4)
        new_inner.Children.Add(self._issued_chk)

        new_group._set_content(new_inner)
        panel.Children.Add(new_group.panel)

    # -----------------------------------------------------------------------
    # Bottom apply bar
    # -----------------------------------------------------------------------

    def _build_apply_bar(self, parent):
        bar = Grid()
        bar.Margin = Thickness(0, 10, 0, 0)
        bar.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # row 0: mode toggle
        bar.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # row 1: status + action button

        # Row 0 — Apply / Remove mode toggle
        mode_row = StackPanel()
        mode_row.Orientation = Orientation.Horizontal
        mode_row.Margin = Thickness(0, 0, 0, 6)

        mode_lbl = TextBlock()
        mode_lbl.Text = u"Mode:"
        mode_lbl.Foreground = MUTED
        mode_lbl.VerticalAlignment = VerticalAlignment.Center
        mode_lbl.Margin = Thickness(0, 0, 10, 0)
        mode_row.Children.Add(mode_lbl)

        # Apply radio
        self._mode_apply_rb = RadioButton()
        self._mode_apply_rb.Content = u"\u25b6  Apply revision to sheets"
        self._mode_apply_rb.Foreground = SUCCESS
        self._mode_apply_rb.IsChecked = True
        self._mode_apply_rb.GroupName = u"ActionMode"
        self._mode_apply_rb.VerticalAlignment = VerticalAlignment.Center
        self._mode_apply_rb.Checked += self._on_mode_changed
        mode_row.Children.Add(self._mode_apply_rb)

        # Remove radio
        self._mode_remove_rb = RadioButton()
        self._mode_remove_rb.Content = u"\u25c0  Remove revision from sheets"
        self._mode_remove_rb.Foreground = WARNING
        self._mode_remove_rb.IsChecked = False
        self._mode_remove_rb.GroupName = u"ActionMode"
        self._mode_remove_rb.VerticalAlignment = VerticalAlignment.Center
        self._mode_remove_rb.Margin = Thickness(20, 0, 0, 0)
        self._mode_remove_rb.Checked += self._on_mode_changed
        mode_row.Children.Add(self._mode_remove_rb)

        Grid.SetRow(mode_row, 0)
        bar.Children.Add(mode_row)

        # Row 1 — status label + action button + close
        action_row = Grid()
        action_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(1, GridUnitType.Star)))
        action_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength.Auto))
        action_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength(8)))
        action_row.ColumnDefinitions.Add(ColumnDefinition(Width=GridLength.Auto))

        self._status_lbl = TextBlock()
        self._status_lbl.Foreground = MUTED
        self._status_lbl.VerticalAlignment = VerticalAlignment.Center
        self._status_lbl.TextWrapping = System.Windows.TextWrapping.Wrap
        Grid.SetColumn(self._status_lbl, 0)
        action_row.Children.Add(self._status_lbl)

        self._apply_btn = Button()
        self._apply_btn.Content = u"Apply Revision to Selected Sheets"
        self._apply_btn.Background = BLUE
        self._apply_btn.Foreground = hex_brush("#000000")
        self._apply_btn.FontWeight = System.Windows.FontWeights.Bold
        self._apply_btn.Padding = Thickness(16, 8, 16, 8)
        self._apply_btn.BorderThickness = Thickness(0)
        self._apply_btn.Click += self._on_smart_apply
        Grid.SetColumn(self._apply_btn, 1)
        action_row.Children.Add(self._apply_btn)

        close_btn = Button()
        close_btn.Content = u"Close"
        close_btn.Background = BTN_BG
        close_btn.Foreground = TEXT
        close_btn.BorderBrush = hex_brush("#444444")
        close_btn.BorderThickness = Thickness(1)
        close_btn.Padding = Thickness(16, 8, 16, 8)
        close_btn.Click += lambda s, e: self.Close()
        Grid.SetColumn(close_btn, 3)
        action_row.Children.Add(close_btn)

        Grid.SetRow(action_row, 1)
        bar.Children.Add(action_row)

        Grid.SetRow(bar, 2)
        parent.Children.Add(bar)

    # -----------------------------------------------------------------------
    # Populate helpers
    # -----------------------------------------------------------------------

    def _populate_print_sets(self):
        self._print_set_combo.Items.Clear()
        if not self._print_sets:
            item = ComboBoxItem()
            item.Content = u"(no print sets in project)"
            item.Foreground = hex_brush("#888780")
            item.IsEnabled = False
            self._print_set_combo.Items.Add(item)
        else:
            for ps in self._print_sets:
                item = ComboBoxItem()
                item.Content = ps.Name
                item.Foreground = hex_brush("#1A1A1A")
                item.Tag = ps
                self._print_set_combo.Items.Add(item)

    def _populate_revisions(self):
        self._revision_combo.Items.Clear()
        self._revisions = get_revisions(self._doc)
        if not self._revisions:
            item = ComboBoxItem()
            item.Content = u"(no revisions in project)"
            item.Foreground = hex_brush("#888780")
            item.IsEnabled = False
            self._revision_combo.Items.Add(item)
        else:
            for rev in self._revisions:
                item = ComboBoxItem()
                # Issued revisions: show lock prefix + muted colour
                if rev.Issued:
                    item.Content = u"\U0001f512 " + revision_label(rev)
                    item.Foreground = hex_brush("#888780")
                else:
                    item.Content = revision_label(rev)
                    item.Foreground = hex_brush("#1A1A1A")
                item.Tag = rev
                self._revision_combo.Items.Add(item)
            self._revision_combo.SelectedIndex = len(self._revisions) - 1

        nxt = next_sequence_number(self._doc)
        self._seq_preview.Text = u"Next revision will be sequence #{0}".format(nxt)

    def _show_all_sheets(self):
        self._filtered_sheets = list(self._all_sheets)
        self._apply_search_filter()

    def _populate_sheet_list(self, sheets):
        """Rebuild the sheet list. Each row has a coloured dot indicator."""
        self._sheet_list.Items.Clear()
        self._sheet_item_dots = {}
        rev_id = self._get_selected_revision_id()

        for sheet in sheets:
            has_rev = rev_id is not None and sheet_has_revision(sheet, rev_id)

            item = ListBoxItem()
            item.Tag = sheet
            item.Background = DARK
            item.Padding = Thickness(4, 3, 4, 3)

            row = StackPanel()
            row.Orientation = Orientation.Horizontal

            # Indicator dot — ✓ teal if has revision, – grey if not
            dot = TextBlock()
            dot.FontSize = 11
            dot.FontWeight = System.Windows.FontWeights.Bold
            dot.VerticalAlignment = VerticalAlignment.Center
            dot.Margin = Thickness(0, 0, 7, 0)
            dot.Width = 12
            if has_rev:
                dot.Text = u"\u2713"   # ✓
                dot.Foreground = SUCCESS
            else:
                dot.Text = u"\u2013"   # –
                dot.Foreground = MUTED

            txt = TextBlock()
            txt.Text = u"{0}  \u2014  {1}".format(sheet.SheetNumber, sheet.Name)
            txt.Foreground = TEXT
            txt.VerticalAlignment = VerticalAlignment.Center

            row.Children.Add(dot)
            row.Children.Add(txt)
            item.Content = row

            self._sheet_item_dots[id(item)] = (item, dot)
            self._sheet_list.Items.Add(item)

        self._update_sheet_count()

    def _refresh_sheet_indicators(self):
        """Update dot indicators without rebuilding the list (preserves selection)."""
        rev_id = self._get_selected_revision_id()
        for _id, (item, dot) in self._sheet_item_dots.items():
            if item.Tag is not None:
                has_rev = rev_id is not None and sheet_has_revision(item.Tag, rev_id)
                if has_rev:
                    dot.Text = u"\u2713"
                    dot.Foreground = SUCCESS
                else:
                    dot.Text = u"\u2013"
                    dot.Foreground = MUTED

    def _update_sheet_count(self):
        selected = sum(1 for i in self._sheet_list.Items
                       if isinstance(i, ListBoxItem) and i.IsSelected)
        total = self._sheet_list.Items.Count
        self._sheet_count_lbl.Text = u"{0} of {1} sheets selected".format(selected, total)

    # -----------------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------------

    def _on_print_set_changed(self, sender, e):
        sel = self._print_set_combo.SelectedItem
        if sel is None or not hasattr(sel, "Tag") or sel.Tag is None:
            return
        ps = sel.Tag
        ps_sheet_ids = set(v.Id for v in ps.Views)
        self._filtered_sheets = [s for s in self._all_sheets if s.Id in ps_sheet_ids]
        self._apply_search_filter()

    def _on_clear_filter(self, sender, e):
        self._print_set_combo.SelectedIndex = -1
        self._filtered_sheets = list(self._all_sheets)
        self._apply_search_filter()

    def _on_search_changed(self, sender, e):
        self._apply_search_filter()

    def _apply_search_filter(self):
        query = self._search_box.Text.strip().lower() if hasattr(self, "_search_box") else u""
        if query:
            visible = [s for s in self._filtered_sheets
                       if query in s.SheetNumber.lower() or query in s.Name.lower()]
        else:
            visible = list(self._filtered_sheets)
        self._populate_sheet_list(visible)

    def _on_select_all(self, sender, e):
        self._sheet_list.SelectAll()
        self._update_sheet_count()

    def _on_select_none(self, sender, e):
        self._sheet_list.UnselectAll()
        self._update_sheet_count()

    def _on_select_with_revision(self, sender, e):
        """Select all visible sheets that already carry the chosen revision."""
        rev_id = self._get_selected_revision_id()
        if rev_id is None:
            self._set_status(u"\u26a0 No revision selected.", WARNING)
            return
        self._sheet_list.UnselectAll()
        for item in self._sheet_list.Items:
            if isinstance(item, ListBoxItem) and item.Tag is not None:
                if sheet_has_revision(item.Tag, rev_id):
                    item.IsSelected = True
        self._update_sheet_count()

    def _on_sheet_selection_changed(self, sender, e):
        self._update_sheet_count()

    def _on_revision_selected(self, sender, e):
        sel = self._revision_combo.SelectedItem
        if sel is None or not hasattr(sel, "Tag") or sel.Tag is None:
            self._rev_name_lbl.Text = u"\u2014 no revision selected \u2014"
            self._rev_preview.Text = u""
            self._refresh_sheet_indicators()
            return
        rev = sel.Tag
        try:
            num = rev.RevisionNumber or str(rev.SequenceNumber)
        except Exception:
            num = str(rev.SequenceNumber)
        desc = rev.Description or u"(no description)"
        date = rev.RevisionDate or u"no date"
        issued_by = rev.IssuedBy or u"\u2014"
        if rev.Issued:
            status = u"\U0001f512 ISSUED"
            self._rev_name_lbl.Foreground = WARNING
        else:
            status = u"Not issued"
            self._rev_name_lbl.Foreground = BLUE
        self._rev_name_lbl.Text = u"Rev {0}  \u2014  {1}".format(num, desc)
        self._rev_preview.Text = u"Date: {0}   |   By: {1}   |   {2}".format(
            date, issued_by, status)
        # Refresh dot indicators for the newly selected revision
        self._refresh_sheet_indicators()

    def _on_mode_changed(self, sender, e):
        """Switch between Apply and Remove modes — update button colour and label."""
        if not hasattr(self, "_apply_btn"):
            return
        if self._mode_remove_rb.IsChecked == True:
            self._apply_btn.Background = ACCENT
            if not self._new_desc.Text.strip():
                self._apply_btn.Content = u"Remove Revision from Selected Sheets"
        else:
            self._apply_btn.Background = BLUE
            if not self._new_desc.Text.strip():
                self._apply_btn.Content = u"Apply Revision to Selected Sheets"

    def _on_create_revision(self, sender, e):
        desc = self._new_desc.Text.strip()
        if not desc:
            self._set_status(u"\u26a0 Description is required to create a revision.", WARNING)
            return

        date_str = self._new_date.Text.strip()
        issued_by = self._new_issued_by.Text.strip()
        mark_issued = (self._issued_chk.IsChecked == True)

        selected_sheets = self._get_selected_sheets()

        t = Transaction(self._doc, u"Create Revision + Apply to Sheets")
        try:
            t.Start()
            rev = Revision.Create(self._doc)
            rev.Description = desc
            if date_str:
                rev.RevisionDate = date_str
            if issued_by:
                rev.IssuedBy = issued_by
            if _HAS_NUMBER_TYPE:
                try:
                    num_type = (RevisionNumberType.Alphanumeric
                                if self._num_alpha.IsChecked == True
                                else RevisionNumberType.Numeric)
                    # IronPython CLR sometimes requires explicit setter
                    try:
                        rev.NumberType = num_type
                    except Exception:
                        rev.set_NumberType(num_type)
                except Exception:
                    pass
            if mark_issued:
                rev.Issued = True

            applied = 0
            skipped = 0
            for sheet in selected_sheets:
                if sheet_has_revision(sheet, rev.Id):
                    skipped += 1
                    continue
                id_list = List[ElementId](sheet.GetAdditionalRevisionIds())
                id_list.Add(rev.Id)
                sheet.SetAdditionalRevisionIds(id_list)
                applied += 1

            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            self._set_status(u"\u274c Error: {0}".format(str(ex)), WARNING)
            return

        self._all_sheets = get_all_sheets(self._doc)
        self._populate_revisions()
        self._revision_combo.SelectedIndex = len(self._revisions) - 1
        self._show_all_sheets()
        self._new_desc.Text = u""

        if applied:
            msg = u"\u2713 Created revision + applied to {0} sheet(s).".format(applied)
            if skipped:
                msg += u" {0} already had it.".format(skipped)
        else:
            msg = u"\u2713 Revision created. Select sheets and apply to stamp them."
        self._set_status(msg, SUCCESS)

    def _on_apply(self, sender, e):
        sel = self._revision_combo.SelectedItem
        if sel is None or not hasattr(sel, "Tag") or sel.Tag is None:
            self._set_status(u"\u26a0 Please select a revision first.", WARNING)
            return

        rev = sel.Tag
        selected_sheets = self._get_selected_sheets()

        if not selected_sheets:
            self._set_status(u"\u26a0 No sheets selected.", WARNING)
            return

        if rev.Issued:
            result = MessageBox.Show(
                u"The selected revision is marked as ISSUED (locked).\n\n"
                u"Revit may prevent changes to issued revisions.\nContinue anyway?",
                u"Issued Revision",
                MessageBoxButton.YesNo
            )
            if result != MessageBoxResult.Yes:
                return

        t = Transaction(self._doc, u"Apply Revision to Sheets")
        try:
            t.Start()
            applied = 0
            skipped = 0
            for sheet in selected_sheets:
                if sheet_has_revision(sheet, rev.Id):
                    skipped += 1
                    continue
                id_list = List[ElementId](sheet.GetAdditionalRevisionIds())
                id_list.Add(rev.Id)
                sheet.SetAdditionalRevisionIds(id_list)
                applied += 1
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            self._set_status(u"\u274c Error: {0}".format(str(ex)), WARNING)
            return

        msg = u"\u2713 Applied to {0} sheet(s).".format(applied)
        if skipped:
            msg += u" {0} already had it.".format(skipped)
        self._set_status(msg, SUCCESS)
        self._refresh_sheet_indicators()

    def _on_remove(self, sender, e):
        sel = self._revision_combo.SelectedItem
        if sel is None or not hasattr(sel, "Tag") or sel.Tag is None:
            self._set_status(u"\u26a0 Please select a revision first.", WARNING)
            return

        rev = sel.Tag
        selected_sheets = self._get_selected_sheets()

        if not selected_sheets:
            self._set_status(u"\u26a0 No sheets selected.", WARNING)
            return

        if rev.Issued:
            result = MessageBox.Show(
                u"The selected revision is marked as ISSUED (locked).\n\n"
                u"Revit may prevent changes to issued revisions.\nContinue anyway?",
                u"Issued Revision",
                MessageBoxButton.YesNo
            )
            if result != MessageBoxResult.Yes:
                return

        t = Transaction(self._doc, u"Remove Revision from Sheets")
        try:
            t.Start()
            removed = 0
            skipped = 0
            for sheet in selected_sheets:
                if not sheet_has_revision(sheet, rev.Id):
                    skipped += 1
                    continue
                id_list = List[ElementId](sheet.GetAdditionalRevisionIds())
                id_list.Remove(rev.Id)
                sheet.SetAdditionalRevisionIds(id_list)
                removed += 1
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            self._set_status(u"\u274c Error: {0}".format(str(ex)), WARNING)
            return

        msg = u"\u2713 Removed from {0} sheet(s).".format(removed)
        if skipped:
            msg += u" {0} didn\u2019t have it.".format(skipped)
        self._set_status(msg, SUCCESS)
        self._refresh_sheet_indicators()

    def _on_desc_changed(self, sender, e):
        if not hasattr(self, "_apply_btn"):
            return
        if self._new_desc.Text.strip():
            self._apply_btn.Content = u"Create New Revision + Apply to Selected Sheets"
            self._apply_btn.Background = BLUE
        else:
            # Restore label and colour based on current mode
            if self._mode_remove_rb.IsChecked == True:
                self._apply_btn.Content = u"Remove Revision from Selected Sheets"
                self._apply_btn.Background = ACCENT
            else:
                self._apply_btn.Content = u"Apply Revision to Selected Sheets"
                self._apply_btn.Background = BLUE

    def _on_smart_apply(self, sender, e):
        if self._new_desc.Text.strip():
            self._on_create_revision(sender, e)
        elif self._mode_remove_rb.IsChecked == True:
            self._on_remove(sender, e)
        else:
            self._on_apply(sender, e)

    # -----------------------------------------------------------------------
    # UI helpers
    # -----------------------------------------------------------------------

    def _get_selected_revision_id(self):
        sel = self._revision_combo.SelectedItem
        if sel is None or not hasattr(sel, "Tag") or sel.Tag is None:
            return None
        return sel.Tag.Id

    def _get_selected_sheets(self):
        result = []
        for item in self._sheet_list.Items:
            if isinstance(item, ListBoxItem) and item.IsSelected and item.Tag is not None:
                result.append(item.Tag)
        return result

    def _set_status(self, msg, color=None):
        self._status_lbl.Text = msg
        self._status_lbl.Foreground = color if color else MUTED

    def _label(self, text):
        lbl = TextBlock()
        lbl.Text = text
        lbl.Foreground = TEXT
        lbl.FontSize = 12
        return lbl

    def _small_btn(self, text):
        btn = Button()
        btn.Content = text
        btn.Background = BTN_BG
        btn.Foreground = TEXT
        btn.BorderBrush = hex_brush("#444444")
        btn.Padding = Thickness(8, 3, 8, 3)
        btn.FontSize = 11
        return btn

    def _spacer(self, h):
        sp = StackPanel()
        sp.Height = h
        return sp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        from pyrevit import HOST_APP
        doc = HOST_APP.doc
        uidoc = HOST_APP.uidoc
    except Exception:
        raise RuntimeError("Could not get Revit document. Run from pyRevit.")

    win = RevisionManagerWindow(doc, uidoc)
    win.ShowDialog()


main()
