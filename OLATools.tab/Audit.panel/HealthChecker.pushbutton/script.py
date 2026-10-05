# -*- coding: utf-8 -*-
"""
script.py  —  Model Health Checker  v1.0.0
pyRevit pushbutton entry point.

Responsibilities:
  - Load checks.py and snapshots.py from the same bundle folder
  - Show the WPF split-pane window
  - Run checks one at a time on the UI thread via the Dispatcher queue —
    never on a background thread, since the Revit API is not thread-safe —
    updating the progress bar between checks
  - Handle all in-tool fix actions (delete, purge, unbind) inside Revit transactions
  - Persist config (last preset, export folder, thresholds) via pyRevit config
  - Save / load snapshots via snapshots.py

IronPython 2 / Revit 2024.  No f-strings.  .format() throughout.
Known IronPython WPF constraints:
  - No Style.Triggers with IsMouseOver/IsFocused on TextBox (crashes)
  - No CharacterSpacing (UWP only)
  - Use System.Windows.MessageBox for dialogs, not forms.alert, so focus
    returns to the WPF owner window correctly.
"""

import os
import sys
import clr
import json
import datetime

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('System')
clr.AddReference('System.Windows.Forms')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from Autodesk.Revit.DB import (
    Transaction,
    TransactionGroup,
    ElementId,
    FilteredElementCollector,
    ViewSheet,
    View,
    FamilySymbol,
    FamilyInstance,
    ImportInstance,
)
from Autodesk.Revit.UI import UIApplication

import System
from System import Action, Threading
from System.Windows import (
    Window, MessageBox, MessageBoxButton, MessageBoxResult,
    Visibility, HorizontalAlignment, VerticalAlignment, Thickness,
    GridLength, GridUnitType,
)
from System.Windows.Controls import (
    Grid, ColumnDefinition, RowDefinition,
    StackPanel, ScrollViewer, Border,
    Label, TextBlock, TextBox, ComboBox, ComboBoxItem,
    Button, CheckBox, ProgressBar, ListBox, ListBoxItem,
    ScrollBarVisibility, Orientation, Separator,
)
from System.Windows.Media import (
    SolidColorBrush, Color,
)
from System.Windows.Threading import DispatcherPriority

# ---------------------------------------------------------------------------
# pyRevit context
# ---------------------------------------------------------------------------
from pyrevit import script as pyscript
from pyrevit import forms

doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
app   = __revit__.Application

TOOL_VERSION = 'v1.0.0'

# ---------------------------------------------------------------------------
# Bundle-relative imports
# ---------------------------------------------------------------------------
BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
if BUNDLE_DIR not in sys.path:
    sys.path.insert(0, BUNDLE_DIR)

import checks
import snapshots

# ---------------------------------------------------------------------------
# Config helpers  (pyRevit config — persisted per machine)
# ---------------------------------------------------------------------------
CONFIG_SECTION = 'HealthChecker'

def _cfg_get(key, default=''):
    try:
        cfg = pyscript.get_config(CONFIG_SECTION)
        return getattr(cfg, key, default)
    except Exception:
        return default

def _cfg_set(key, value):
    try:
        cfg = pyscript.get_config(CONFIG_SECTION)
        setattr(cfg, key, value)
        pyscript.save_config()
    except Exception:
        pass

def _load_custom_thresholds():
    """Return thresholds dict from config, or defaults."""
    raw = _cfg_get('thresholds', '{}')
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _save_custom_thresholds(d):
    _cfg_set('thresholds', json.dumps(d))

def _load_custom_presets():
    """Return {name: [key, ...]} dict from config."""
    raw = _cfg_get('custom_presets', '{}')
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _save_custom_presets(d):
    _cfg_set('custom_presets', json.dumps(d))

# ---------------------------------------------------------------------------
# Colour constants  (match Place RLS Views palette)
# ---------------------------------------------------------------------------
COL_BG       = Color.FromRgb(0x26, 0x26, 0x26)
COL_PANEL    = Color.FromRgb(0x1E, 0x1E, 0x1E)
COL_DARKER   = Color.FromRgb(0x18, 0x18, 0x18)
COL_BORDER   = Color.FromRgb(0x3A, 0x3A, 0x3A)
COL_ACTIVE   = Color.FromRgb(0x1E, 0x2A, 0x38)
COL_BLUE     = Color.FromRgb(0x4F, 0xC3, 0xF7)
COL_TEAL     = Color.FromRgb(0x4D, 0xB6, 0xAC)
COL_AMBER    = Color.FromRgb(0xFF, 0xB7, 0x4D)
COL_RED      = Color.FromRgb(0xEF, 0x53, 0x50)
COL_GREEN    = Color.FromRgb(0x4D, 0xB6, 0xAC)
COL_TEXT     = Color.FromRgb(0xCC, 0xCC, 0xCC)
COL_MUTED    = Color.FromRgb(0x88, 0x87, 0x80)
COL_DIMMED   = Color.FromRgb(0x55, 0x55, 0x55)
COL_WHITE    = Color.FromRgb(0xFF, 0xFF, 0xFF)

def _brush(col):
    return SolidColorBrush(col)

STATUS_COLOURS = {
    'OK':    COL_TEAL,
    'WARN':  COL_AMBER,
    'FAIL':  COL_RED,
    'INFO':  COL_MUTED,
    'ERROR': COL_RED,
}

# Checks whose flagged items can be selectively deleted from the UI.
# Button label shown on the action bar per check.
DELETE_ACTION_LABELS = {
    'imported_dwgs':        'Delete selected DWGs',
    'unused_families':      'Purge selected families',
    'raster_images':        'Delete selected images',
    'views_not_on_sheets':  'Delete selected views',
    'sheets_with_no_views': 'Delete selected sheets',
}
DELETE_ACTION_KEYS = set(DELETE_ACTION_LABELS.keys())

# Singular noun used in confirm / result dialogs.
DELETE_ITEM_LABELS = {
    'imported_dwgs':        'imported DWG',
    'unused_families':      'unused family type',
    'raster_images':        'raster image',
    'views_not_on_sheets':  'orphan view',
    'sheets_with_no_views': 'empty sheet',
}

SCORE_COLOUR_THRESHOLDS = [
    (80, COL_TEAL),
    (50, COL_AMBER),
    (0,  COL_RED),
]

def _score_colour(score):
    for threshold, col in SCORE_COLOUR_THRESHOLDS:
        if score >= threshold:
            return col
    return COL_RED

# ---------------------------------------------------------------------------
# WPF helper factories
# ---------------------------------------------------------------------------

def _tb(text, size=12, colour=None, bold=False, wrap=False):
    tb = TextBlock()
    tb.Text = text
    tb.FontSize = size
    tb.Foreground = _brush(colour or COL_TEXT)
    if bold:
        tb.FontWeight = System.Windows.FontWeights.SemiBold
    if wrap:
        tb.TextWrapping = System.Windows.TextWrapping.Wrap
    return tb

def _border(child=None, bg=None, border_col=None, thickness=1,
            padding=0, radius=3, margin=0):
    b = Border()
    b.Background   = _brush(bg or COL_PANEL)
    b.BorderBrush  = _brush(border_col or COL_BORDER)
    b.BorderThickness = Thickness(thickness)
    b.CornerRadius = System.Windows.CornerRadius(radius)
    b.Padding      = Thickness(padding)
    b.Margin       = Thickness(margin)
    if child:
        b.Child = child
    return b

def _btn(text, colour=None, fg=None, handler=None, margin=4):
    b = Button()
    b.Content    = text
    b.FontSize   = 11
    b.Background = _brush(colour or COL_PANEL)
    b.Foreground = _brush(fg or COL_BLUE)
    b.BorderBrush = _brush(fg or COL_BLUE)
    b.BorderThickness = Thickness(1)
    b.Padding    = Thickness(10, 4, 10, 4)
    b.Margin     = Thickness(margin, 0, 0, 0)
    b.Cursor     = System.Windows.Input.Cursors.Hand
    if handler:
        b.Click += handler
    return b

def _separator_h(margin_v=6):
    s = Separator()
    s.Margin = Thickness(0, margin_v, 0, margin_v)
    s.Background = _brush(COL_BORDER)
    return s

# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class HealthCheckerWindow(Window):

    def __init__(self):
        self.Title = 'Model Health Checker — {}'.format(TOOL_VERSION)
        self.Width  = 1000
        self.Height = 860
        self.MinWidth  = 860
        self.MinHeight = 700
        self.Background = _brush(COL_BG)
        self.WindowStartupLocation = \
            System.Windows.WindowStartupLocation.CenterScreen

        # state
        self._results       = {}
        self._running       = False
        self._selected_key  = None
        self._check_keys    = list(checks.PRESETS['Full audit'])
        self._thresholds    = _load_custom_thresholds()
        self._custom_presets = _load_custom_presets()
        self._last_export_folder = _cfg_get(
            'export_folder',
            os.path.join(os.path.expanduser('~'), 'Desktop'))
        self._check_item_map    = {}   # key → Border row
        self._check_row_widgets = {}   # key → {'name_tb', 'badge', 'badge_tb'}
        self._score         = 0
        self._counts        = {}

        # Per-item selection state for delete-capable checks.
        self._item_selection     = {}  # key → {item_id: bool}
        self._item_checkbox_refs = {}  # key → {item_id: CheckBox}
        self._action_bar_refs    = {}  # key → {'note': TextBlock, 'del_btn': Button}

        self._build_ui()
        self._populate_preset_dropdown()
        self._restore_preset_selection()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = Grid()
        root.RowDefinitions.Add(_row_def(GridUnitType.Auto))   # score bar
        root.RowDefinitions.Add(_row_def(GridUnitType.Star))   # main layout
        self.Content = root

        # Score bar
        self._score_bar = self._build_score_bar()
        Grid.SetRow(self._score_bar, 0)
        root.Children.Add(self._score_bar)

        # Main split layout
        split = Grid()
        split.ColumnDefinitions.Add(_col_def(300, GridUnitType.Pixel))
        split.ColumnDefinitions.Add(_col_def(1,   GridUnitType.Auto))
        split.ColumnDefinitions.Add(_col_def(1,   GridUnitType.Star))
        Grid.SetRow(split, 1)
        root.Children.Add(split)

        # Left panel
        left = self._build_left_panel()
        Grid.SetColumn(left, 0)
        split.Children.Add(left)

        # Splitter visual
        splitter_border = Border()
        splitter_border.Width = 1
        splitter_border.Background = _brush(COL_BORDER)
        Grid.SetColumn(splitter_border, 1)
        split.Children.Add(splitter_border)

        # Right panel
        right = self._build_right_panel()
        Grid.SetColumn(right, 2)
        split.Children.Add(right)

        # Now both panels exist — safe to select the first check row
        if getattr(self, '_first_check_key', None):
            self._select_check(self._first_check_key)

    def _build_score_bar(self):
        bar = Border()
        bar.Background = _brush(COL_DARKER)
        bar.BorderBrush = _brush(COL_BORDER)
        bar.BorderThickness = Thickness(0, 0, 0, 1)
        bar.Padding = Thickness(14, 8, 14, 8)

        g = Grid()
        g.ColumnDefinitions.Add(_col_def(60,  GridUnitType.Pixel))   # circle
        g.ColumnDefinitions.Add(_col_def(1,   GridUnitType.Star))    # meta
        g.ColumnDefinitions.Add(_col_def(220, GridUnitType.Pixel))   # preset
        g.ColumnDefinitions.Add(_col_def(180, GridUnitType.Pixel))   # progress
        bar.Child = g

        # Score circle
        circle = Border()
        circle.Width  = 52
        circle.Height = 52
        circle.CornerRadius = System.Windows.CornerRadius(26)
        circle.BorderThickness = Thickness(3)
        circle.BorderBrush = _brush(COL_AMBER)
        circle.HorizontalAlignment = HorizontalAlignment.Center
        circle.VerticalAlignment   = VerticalAlignment.Center
        self._score_circle_border  = circle

        self._score_label = TextBlock()
        self._score_label.Text = '--'
        self._score_label.FontSize = 18
        self._score_label.FontWeight = System.Windows.FontWeights.Bold
        self._score_label.Foreground = _brush(COL_AMBER)
        self._score_label.HorizontalAlignment = HorizontalAlignment.Center
        self._score_label.VerticalAlignment   = VerticalAlignment.Center
        circle.Child = self._score_label
        Grid.SetColumn(circle, 0)
        g.Children.Add(circle)

        # Meta (last run + counts)
        meta_stack = StackPanel()
        meta_stack.VerticalAlignment = VerticalAlignment.Center
        meta_stack.Margin = Thickness(12, 0, 0, 0)

        self._score_subtitle = _tb('Run health check to begin', 11, COL_MUTED)
        meta_stack.Children.Add(self._score_subtitle)

        self._counts_panel = StackPanel()
        self._counts_panel.Orientation = Orientation.Horizontal
        self._counts_panel.Margin = Thickness(0, 4, 0, 0)
        meta_stack.Children.Add(self._counts_panel)

        Grid.SetColumn(meta_stack, 1)
        g.Children.Add(meta_stack)

        # Preset + threshold
        preset_stack = StackPanel()
        preset_stack.VerticalAlignment = VerticalAlignment.Center
        preset_stack.Margin = Thickness(8, 0, 8, 0)

        preset_label = _tb('Preset', 10, COL_MUTED)
        preset_stack.Children.Add(preset_label)

        self._preset_combo = ComboBox()
        self._preset_combo.Margin = Thickness(0, 4, 0, 0)
        self._preset_combo.Background = _brush(COL_PANEL)
        self._preset_combo.Foreground = _brush(COL_TEXT)
        self._preset_combo.BorderBrush = _brush(COL_BORDER)
        self._preset_combo.FontSize = 11
        self._preset_combo.SelectionChanged += self._on_preset_changed
        preset_stack.Children.Add(self._preset_combo)

        thresh_btn = _btn('Edit thresholds...', COL_DARKER, COL_MUTED,
                          self._on_edit_thresholds)
        thresh_btn.Margin = Thickness(0, 4, 0, 0)
        thresh_btn.HorizontalAlignment = HorizontalAlignment.Left
        preset_stack.Children.Add(thresh_btn)

        Grid.SetColumn(preset_stack, 2)
        g.Children.Add(preset_stack)

        # Progress
        prog_stack = StackPanel()
        prog_stack.VerticalAlignment = VerticalAlignment.Center
        prog_stack.Margin = Thickness(8, 0, 0, 0)

        self._prog_label = _tb('', 10, COL_DIMMED)
        prog_stack.Children.Add(self._prog_label)

        self._prog_bar = ProgressBar()
        self._prog_bar.Minimum = 0
        self._prog_bar.Maximum = 100
        self._prog_bar.Value   = 0
        self._prog_bar.Height  = 6
        self._prog_bar.Margin  = Thickness(0, 4, 0, 0)
        self._prog_bar.Foreground = _brush(COL_BLUE)
        self._prog_bar.Background = _brush(COL_BORDER)
        prog_stack.Children.Add(self._prog_bar)

        self._prog_step = _tb('', 10, COL_BLUE)
        self._prog_step.Margin = Thickness(0, 3, 0, 0)
        prog_stack.Children.Add(self._prog_step)

        Grid.SetColumn(prog_stack, 3)
        g.Children.Add(prog_stack)

        return bar

    def _build_left_panel(self):
        outer = Grid()
        outer.RowDefinitions.Add(_row_def(GridUnitType.Star))
        outer.RowDefinitions.Add(_row_def(GridUnitType.Auto))

        # Scrollable check list
        scroll = ScrollViewer()
        scroll.VerticalScrollBarVisibility   = ScrollBarVisibility.Auto
        scroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        scroll.Background = _brush(COL_BG)

        self._check_listbox = StackPanel()
        scroll.Content = self._check_listbox
        Grid.SetRow(scroll, 0)
        outer.Children.Add(scroll)

        # Run button dock
        btn_dock = Border()
        btn_dock.Background = _brush(COL_DARKER)
        btn_dock.BorderBrush = _brush(COL_BORDER)
        btn_dock.BorderThickness = Thickness(0, 1, 0, 0)
        btn_dock.Padding = Thickness(10, 8, 10, 8)

        self._run_btn = Button()
        self._run_btn.Content  = 'Run health check'
        self._run_btn.FontSize = 12
        self._run_btn.FontWeight = System.Windows.FontWeights.SemiBold
        self._run_btn.Background = _brush(COL_BLUE)
        self._run_btn.Foreground = _brush(Color.FromRgb(0, 0, 0))
        self._run_btn.BorderThickness = Thickness(0)
        self._run_btn.Height  = 32
        self._run_btn.Cursor  = System.Windows.Input.Cursors.Hand
        self._run_btn.Click  += self._on_run
        btn_dock.Child = self._run_btn

        Grid.SetRow(btn_dock, 1)
        outer.Children.Add(btn_dock)

        self._build_check_list()
        return outer

    def _build_check_list(self):
        """Populate left panel with category headers and check rows."""
        self._check_listbox.Children.Clear()
        self._check_item_map = {}

        category_order = ['bloat', 'integrity', 'coordination', 'performance', 'parameters']
        category_labels = {
            'bloat':        'Model bloat',
            'integrity':    'Data integrity',
            'coordination': 'Coordination',
            'performance':  'Performance',
            'parameters':   'Parameters  (opt-in)',
        }

        # group checks by category maintaining order
        by_cat = {}
        for c in checks.CHECKS:
            key, fn, label, cat, weight, opt_in = c
            by_cat.setdefault(cat, []).append(c)

        first_key = None
        for cat in category_order:
            if cat not in by_cat:
                continue
            cat_checks = by_cat[cat]

            # Category header
            hdr = Border()
            hdr.Background = _brush(COL_DARKER)
            hdr.Padding = Thickness(12, 7, 12, 4)
            if cat == 'parameters':
                hdr.BorderBrush = _brush(COL_BORDER)
                hdr.BorderThickness = Thickness(0, 1, 0, 0)
                hdr.Margin = Thickness(0, 4, 0, 0)
            hdr_tb = _tb(category_labels[cat], 10, COL_MUTED)
            hdr.Child = hdr_tb
            self._check_listbox.Children.Add(hdr)

            for c in cat_checks:
                key, fn, label, cat2, weight, opt_in = c
                row = self._make_check_row(key, label)
                self._check_listbox.Children.Add(row)
                self._check_item_map[key] = row
                if first_key is None:
                    first_key = key

        # first_key selection deferred to _build_ui after right panel is built
        self._first_check_key = first_key

    def _make_check_row(self, key, label):
        """Create a single check row for the left panel."""
        row = Border()
        row.Background = _brush(COL_BG)
        row.BorderBrush = _brush(System.Windows.Media.Colors.Transparent)
        row.BorderThickness = Thickness(2, 0, 0, 0)
        row.Padding = Thickness(10, 6, 10, 6)
        row.Cursor = System.Windows.Input.Cursors.Hand
        row.Tag = key

        g = Grid()
        g.ColumnDefinitions.Add(_col_def(1, GridUnitType.Star))
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))

        name_tb = _tb(label, 12, COL_MUTED)
        name_tb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(name_tb, 0)
        g.Children.Add(name_tb)

        badge = Border()
        badge.Background = _brush(COL_DARKER)
        badge.CornerRadius = System.Windows.CornerRadius(2)
        badge.Padding = Thickness(6, 1, 6, 1)
        badge.VerticalAlignment = VerticalAlignment.Center
        badge_tb = _tb('—', 10, COL_DIMMED)
        badge.Child = badge_tb
        Grid.SetColumn(badge, 1)
        g.Children.Add(badge)

        row.Child = g
        row.MouseLeftButtonDown += self._on_check_row_click

        # store widget refs in the window dict — IronPython cannot set
        # arbitrary attributes on .NET WPF objects
        self._check_row_widgets[key] = {
            'name_tb':  name_tb,
            'badge':    badge,
            'badge_tb': badge_tb,
        }
        return row

    def _build_right_panel(self):
        outer = Grid()
        outer.RowDefinitions.Add(_row_def(GridUnitType.Auto))   # detail header
        outer.RowDefinitions.Add(_row_def(GridUnitType.Star))   # detail body
        outer.RowDefinitions.Add(_row_def(GridUnitType.Auto))   # export dock

        # Detail header
        self._detail_header = self._build_detail_header()
        Grid.SetRow(self._detail_header, 0)
        outer.Children.Add(self._detail_header)

        # Detail body (scrollable)
        detail_scroll = ScrollViewer()
        detail_scroll.VerticalScrollBarVisibility   = ScrollBarVisibility.Auto
        detail_scroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        detail_scroll.Background = _brush(COL_BG)
        detail_scroll.Padding    = Thickness(16, 12, 16, 12)

        self._detail_body = StackPanel()
        detail_scroll.Content = self._detail_body

        placeholder = _tb('Select a check from the left panel, then click Run health check.',
                          12, COL_DIMMED, wrap=True)
        self._detail_body.Children.Add(placeholder)

        Grid.SetRow(detail_scroll, 1)
        outer.Children.Add(detail_scroll)

        # Export dock
        export_dock = self._build_export_dock()
        Grid.SetRow(export_dock, 2)
        outer.Children.Add(export_dock)

        return outer

    def _build_detail_header(self):
        hdr = Border()
        hdr.Background = _brush(COL_PANEL)
        hdr.BorderBrush = _brush(COL_BORDER)
        hdr.BorderThickness = Thickness(0, 0, 0, 1)
        hdr.Padding = Thickness(16, 10, 16, 10)

        g = Grid()
        g.ColumnDefinitions.Add(_col_def(1, GridUnitType.Star))
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))

        left_stack = StackPanel()
        self._detail_title = _tb('', 13, COL_WHITE, bold=True)
        self._detail_meta  = _tb('', 11, COL_MUTED, wrap=True)
        self._detail_meta.Margin = Thickness(0, 2, 0, 0)
        left_stack.Children.Add(self._detail_title)
        left_stack.Children.Add(self._detail_meta)
        Grid.SetColumn(left_stack, 0)
        g.Children.Add(left_stack)

        self._detail_badge = Border()
        self._detail_badge.CornerRadius = System.Windows.CornerRadius(3)
        self._detail_badge.Padding = Thickness(10, 3, 10, 3)
        self._detail_badge.Background = _brush(COL_DARKER)
        self._detail_badge.VerticalAlignment = VerticalAlignment.Center
        self._detail_badge_tb = _tb('', 11, COL_MUTED, bold=True)
        self._detail_badge.Child = self._detail_badge_tb
        Grid.SetColumn(self._detail_badge, 1)
        g.Children.Add(self._detail_badge)

        hdr.Child = g
        return hdr

    def _build_export_dock(self):
        dock = Border()
        dock.Background = _brush(COL_DARKER)
        dock.BorderBrush = _brush(COL_BORDER)
        dock.BorderThickness = Thickness(0, 1, 0, 0)
        dock.Padding = Thickness(14, 8, 14, 8)

        g = Grid()
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))   # label
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))   # csv cb
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))   # xlsx cb
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))   # json cb
        g.ColumnDefinitions.Add(_col_def(1, GridUnitType.Star)) # path
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))   # browse
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))   # snapshot btn
        g.ColumnDefinitions.Add(_col_def(GridUnitType.Auto))   # export btn

        def _cb_col(text, col_idx, checked=True):
            cb = CheckBox()
            cb.Content = text
            cb.FontSize = 11
            cb.Foreground = _brush(COL_TEXT)
            cb.IsChecked = checked
            cb.VerticalAlignment = VerticalAlignment.Center
            cb.Margin = Thickness(8, 0, 0, 0)
            Grid.SetColumn(cb, col_idx)
            g.Children.Add(cb)
            return cb

        label = _tb('Export', 11, COL_MUTED)
        label.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(label, 0)
        g.Children.Add(label)

        self._cb_csv  = _cb_col('CSV',   1, True)
        self._cb_xlsx = _cb_col('Excel', 2, True)
        self._cb_json = _cb_col('JSON',  3, False)

        self._export_path_tb = TextBox()
        self._export_path_tb.Text = self._last_export_folder
        self._export_path_tb.FontSize = 11
        self._export_path_tb.Background = _brush(Color.FromRgb(0x11, 0x11, 0x11))
        self._export_path_tb.Foreground = _brush(COL_MUTED)
        self._export_path_tb.BorderBrush = _brush(COL_BORDER)
        self._export_path_tb.Margin = Thickness(8, 0, 0, 0)
        self._export_path_tb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(self._export_path_tb, 4)
        g.Children.Add(self._export_path_tb)

        browse_btn = _btn('Browse...', COL_PANEL, COL_MUTED,
                          self._on_browse_folder)
        browse_btn.Margin = Thickness(6, 0, 0, 0)
        browse_btn.FontSize = 11
        Grid.SetColumn(browse_btn, 5)
        g.Children.Add(browse_btn)

        snap_btn = _btn('Save snapshot', COL_DARKER, COL_AMBER,
                        self._on_save_snapshot)
        snap_btn.Margin = Thickness(10, 0, 0, 0)
        Grid.SetColumn(snap_btn, 6)
        g.Children.Add(snap_btn)

        export_btn = _btn('Export report', COL_DARKER, COL_TEAL,
                          self._on_export)
        export_btn.Margin = Thickness(6, 0, 0, 0)
        Grid.SetColumn(export_btn, 7)
        g.Children.Add(export_btn)

        dock.Child = g
        return dock

    # ------------------------------------------------------------------
    # Preset dropdown
    # ------------------------------------------------------------------

    def _populate_preset_dropdown(self):
        self._preset_combo.Items.Clear()
        all_presets = list(checks.PRESETS.keys())
        for name in all_presets:
            item = ComboBoxItem()
            item.Content = name
            item.Tag = name
            item.FontSize = 11
            item.Foreground = _brush(COL_TEXT)
            item.Background = _brush(COL_PANEL)
            self._preset_combo.Items.Add(item)

        custom = _load_custom_presets()
        for name in custom:
            item = ComboBoxItem()
            item.Content = name
            item.Tag = '__custom__' + name
            item.FontSize = 11
            item.Foreground = _brush(COL_BLUE)
            item.Background = _brush(COL_PANEL)
            self._preset_combo.Items.Add(item)

        # separator + new preset
        sep_item = ComboBoxItem()
        sep_item.Content = '──────────────'
        sep_item.IsEnabled = False
        sep_item.FontSize = 11
        sep_item.Foreground = _brush(COL_DIMMED)
        self._preset_combo.Items.Add(sep_item)

        new_item = ComboBoxItem()
        new_item.Content = '+ Save current as preset...'
        new_item.Tag = '__new__'
        new_item.FontSize = 11
        new_item.Foreground = _brush(COL_BLUE)
        new_item.Background = _brush(COL_PANEL)
        self._preset_combo.Items.Add(new_item)

    def _restore_preset_selection(self):
        saved = _cfg_get('last_preset', 'Full audit')
        for i in range(self._preset_combo.Items.Count):
            item = self._preset_combo.Items[i]
            if hasattr(item, 'Tag') and item.Tag == saved:
                self._preset_combo.SelectedIndex = i
                return
        if self._preset_combo.Items.Count > 0:
            self._preset_combo.SelectedIndex = 0

    def _on_preset_changed(self, sender, e):
        item = self._preset_combo.SelectedItem
        if item is None:
            return
        tag = getattr(item, 'Tag', None)
        if tag is None or not tag:
            return
        if tag == '__new__':
            self._save_new_preset()
            return
        if str(tag).startswith('__custom__'):
            name = str(tag)[len('__custom__'):]
            custom = _load_custom_presets()
            self._check_keys = custom.get(name, [])
        elif tag in checks.PRESETS:
            self._check_keys = list(checks.PRESETS[tag])
        _cfg_set('last_preset', str(tag))

    def _save_new_preset(self):
        """Prompt for a name and save current check_keys as a custom preset."""
        name = forms.ask_for_string(
            prompt='Enter a name for this preset:',
            title='Save preset')
        if not name:
            return
        custom = _load_custom_presets()
        custom[name] = self._check_keys
        _save_custom_presets(custom)
        self._populate_preset_dropdown()
        MessageBox.Show(
            'Preset "{}" saved.'.format(name),
            'Health Checker',
            MessageBoxButton.OK)

    # ------------------------------------------------------------------
    # Check row selection and detail rendering
    # ------------------------------------------------------------------

    def _on_check_row_click(self, sender, e):
        key = sender.Tag
        self._select_check(key)

    def _select_check(self, key):
        self._selected_key = key
        # update visual selection
        for k, row in self._check_item_map.items():
            w = self._check_row_widgets.get(k, {})
            if k == key:
                row.Background = _brush(COL_ACTIVE)
                row.BorderBrush = _brush(COL_BLUE)
                if w.get('name_tb'):
                    w['name_tb'].Foreground = _brush(COL_WHITE)
            else:
                row.Background = _brush(COL_BG)
                row.BorderBrush = _brush(System.Windows.Media.Colors.Transparent)
                if w.get('name_tb'):
                    w['name_tb'].Foreground = _brush(COL_MUTED)

        # render detail panel — guard against being called before right panel is built
        if not hasattr(self, '_detail_title'):
            return
        if key in self._results:
            self._render_detail(key, self._results[key])
        else:
            # no result yet — show placeholder
            self._detail_title.Text = self._label_for(key)
            self._detail_meta.Text  = 'Not yet run.'
            self._detail_badge_tb.Text = '—'
            self._detail_badge.Background = _brush(COL_DARKER)
            self._detail_body.Children.Clear()
            self._detail_body.Children.Add(
                _tb('Click Run health check to see results.', 12, COL_DIMMED))

    def _label_for(self, key):
        for c in checks.CHECKS:
            if c[0] == key:
                return c[2]
        return key

    def _render_detail(self, key, result):
        """Populate the right-panel detail body for a given result."""
        status  = result.get('status', 'ERROR')
        count   = result.get('count', 0)
        items   = result.get('items', [])
        message = result.get('message', '')
        label   = result.get('label', self._label_for(key))

        # header
        self._detail_title.Text = label
        self._detail_meta.Text  = message
        col = STATUS_COLOURS.get(status, COL_MUTED)
        self._detail_badge_tb.Text = '{} {}'.format(count if count else '', status)
        self._detail_badge_tb.Foreground = _brush(col)
        bg_map = {
            'OK':   Color.FromRgb(0x1A, 0x3A, 0x2A),
            'WARN': Color.FromRgb(0x3A, 0x2E, 0x10),
            'FAIL': Color.FromRgb(0x3A, 0x1A, 0x1A),
            'INFO': Color.FromRgb(0x22, 0x22, 0x22),
            'ERROR':Color.FromRgb(0x3A, 0x1A, 0x1A),
        }
        self._detail_badge.Background = _brush(bg_map.get(status, COL_DARKER))

        self._detail_body.Children.Clear()

        # diff strip
        diff = snapshots.get_diff_for_key(doc, key, count)
        if diff:
            self._detail_body.Children.Add(self._make_diff_strip(diff))

        if status == 'OK':
            ok_tb = _tb(message or 'No issues found.', 12, COL_TEAL)
            ok_tb.Margin = Thickness(0, 8, 0, 0)
            self._detail_body.Children.Add(ok_tb)
            return

        if status == 'INFO' and not items:
            info_tb = _tb(message, 12, COL_MUTED, wrap=True)
            info_tb.Margin = Thickness(0, 8, 0, 0)
            self._detail_body.Children.Add(info_tb)
            return

        if not items:
            self._detail_body.Children.Add(
                _tb('No detail available.', 12, COL_DIMMED))
            return

        # Route to specialist renderers or generic table
        specialist = {
            'revit_warnings':         self._render_warnings,
            'workset_usage':          self._render_guided_tip,
            'grid_level_extents':     self._render_guided_tip,
            'design_options':         self._render_guided_tip,
            'duplicate_parameters':   self._render_param_dupes,
            'unassociated_sp_params': self._render_sp_info,
            'link_status':            self._render_links,
            'duplicate_view_names':   self._render_duplicate_views,
        }
        if key in specialist:
            specialist[key](key, result)
        else:
            self._render_generic_table(key, result)

    # ------------------------------------------------------------------
    # Detail renderers
    # ------------------------------------------------------------------

    def _make_diff_strip(self, diff):
        strip = Border()
        strip.Background = _brush(Color.FromRgb(0x1A, 0x1E, 0x2A))
        strip.BorderBrush = _brush(Color.FromRgb(0x2A, 0x3A, 0x5A))
        strip.BorderThickness = Thickness(1)
        strip.CornerRadius = System.Windows.CornerRadius(3)
        strip.Padding = Thickness(10, 6, 10, 6)
        strip.Margin  = Thickness(0, 0, 0, 10)

        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal

        title = _tb('vs last run  ', 11, COL_BLUE, bold=True)
        sp.Children.Add(title)

        if diff.get('new', 0):
            t = _tb('+{} new   '.format(diff['new']), 11, COL_RED)
            sp.Children.Add(t)
        if diff.get('fixed', 0):
            t = _tb('{} fixed   '.format(diff['fixed']), 11, COL_TEAL)
            sp.Children.Add(t)
        if diff.get('same', 0):
            t = _tb('{} unchanged'.format(diff['same']), 11, COL_DIMMED)
            sp.Children.Add(t)

        strip.Child = sp
        return strip

    def _render_generic_table(self, key, result):
        """
        Generic render: shows items as a scrollable list with
        element ID, name/description, and a 'Show in view' navigate link.
        Adds Delete / Purge / Ungroup action buttons where applicable.
        """
        items  = result.get('items', [])
        status = result.get('status', 'WARN')

        # warn/info box for checks that can't be auto-fixed
        no_fix_keys = {'inplace_families', 'oversized_families',
                       'elements_not_in_phase', 'rooms_in_open_space',
                       'ceiling_height_anomalies', 'pinned_elements',
                       'untagged_elements'}
        if key in no_fix_keys:
            self._detail_body.Children.Add(
                self._make_notice_box(
                    'Cannot auto-fix — navigate to each element',
                    'Click Show to zoom Revit to the element.',
                    COL_AMBER))

        selectable = key in DELETE_ACTION_KEYS
        if selectable:
            self._get_selection(key, items)
            self._item_checkbox_refs[key] = {}

        # item rows
        for item in items[:50]:  # cap at 50 for UI performance
            self._detail_body.Children.Add(
                self._make_item_row(item, key))

        if len(items) > 50:
            hidden = len(items) - 50
            note_text = '... {} more item{} — export report for full list.'.format(
                hidden, 's' if hidden != 1 else '')
            if selectable:
                note_text += (' Hidden items stay selected for delete unless '
                              'you click "Select none" first.')
            more = _tb(note_text, 11, COL_DIMMED, wrap=True)
            more.Margin = Thickness(0, 4, 0, 0)
            self._detail_body.Children.Add(more)

        # action bar
        self._detail_body.Children.Add(
            self._make_action_bar(key, result))

    def _make_notice_box(self, title, body, col):
        bg_map = {
            COL_AMBER: Color.FromRgb(0x2A, 0x20, 0x10),
            COL_TEAL:  Color.FromRgb(0x1E, 0x2A, 0x1E),
            COL_MUTED: Color.FromRgb(0x1E, 0x1E, 0x1E),
        }
        border_map = {
            COL_AMBER: Color.FromRgb(0x4A, 0x3A, 0x18),
            COL_TEAL:  Color.FromRgb(0x2A, 0x4A, 0x2A),
            COL_MUTED: Color.FromRgb(0x2A, 0x2A, 0x3A),
        }
        box = Border()
        box.Background = _brush(bg_map.get(col, COL_DARKER))
        box.BorderBrush = _brush(border_map.get(col, COL_BORDER))
        box.BorderThickness = Thickness(1)
        box.CornerRadius = System.Windows.CornerRadius(3)
        box.Padding = Thickness(10, 8, 10, 8)
        box.Margin  = Thickness(0, 0, 0, 10)

        sp = StackPanel()
        title_tb = _tb(title, 11, col, bold=True)
        body_tb  = _tb(body,  11, COL_MUTED, wrap=True)
        body_tb.Margin = Thickness(0, 3, 0, 0)
        sp.Children.Add(title_tb)
        sp.Children.Add(body_tb)
        box.Child = sp
        return box

    def _get_selection(self, key, items):
        """
        Lazily init (or refresh, if the item set changed) the per-item
        selection dict for a delete-capable check. Everything defaults
        to selected, matching the old all-or-nothing delete behaviour.
        """
        ids = [item.get('id') for item in items]
        id_set = set(ids)
        sel = self._item_selection.get(key)
        if sel is None or set(sel.keys()) != id_set:
            sel = dict((i, True) for i in ids)
            self._item_selection[key] = sel
        return sel

    def _on_item_checkbox_changed(self, sender, e, key, item_id):
        sel = self._item_selection.setdefault(key, {})
        sel[item_id] = bool(sender.IsChecked)
        self._update_selection_summary(key)

    def _set_all_selected(self, key, items, value):
        """Select-all / select-none — covers items beyond the 50-row cap too."""
        refs = self._item_checkbox_refs.get(key, {})
        sel = self._item_selection.setdefault(key, {})
        for item in items:
            item_id = item.get('id')
            cb = refs.get(item_id)
            if cb is not None:
                cb.IsChecked = value  # fires Checked/Unchecked -> updates sel + summary
            else:
                sel[item_id] = value
        self._update_selection_summary(key)

    def _update_selection_summary(self, key):
        refs = self._action_bar_refs.get(key)
        if not refs:
            return
        sel = self._item_selection.get(key, {})
        selected = sum(1 for v in sel.values() if v)
        total = len(sel)
        note = refs.get('note')
        if note:
            note.Text = '{} of {} selected'.format(selected, total)
        del_btn = refs.get('del_btn')
        if del_btn:
            del_btn.IsEnabled = selected > 0

    def _make_item_row(self, item, key):
        row = Border()
        row.Background = _brush(COL_PANEL)
        row.BorderBrush = _brush(Color.FromRgb(0x22, 0x22, 0x22))
        row.BorderThickness = Thickness(0, 0, 0, 1)
        row.Padding = Thickness(6, 5, 6, 5)

        selectable = key in DELETE_ACTION_KEYS
        item_id    = item.get('id')

        g = Grid()
        if selectable:
            g.ColumnDefinitions.Add(_col_def(24, GridUnitType.Pixel))  # checkbox
        g.ColumnDefinitions.Add(_col_def(60,  GridUnitType.Pixel))  # ID
        g.ColumnDefinitions.Add(_col_def(1,   GridUnitType.Star))   # name
        g.ColumnDefinitions.Add(_col_def(100, GridUnitType.Pixel))  # detail
        g.ColumnDefinitions.Add(_col_def(80,  GridUnitType.Pixel))  # action

        col_offset = 1 if selectable else 0

        if selectable:
            sel = self._item_selection.get(key, {})
            cb = CheckBox()
            cb.IsChecked = sel.get(item_id, True)
            cb.VerticalAlignment = VerticalAlignment.Center
            cb.HorizontalAlignment = HorizontalAlignment.Center
            cb.Tag = item_id
            cb.Checked   += (lambda s, e, k=key, iid=item_id:
                              self._on_item_checkbox_changed(s, e, k, iid))
            cb.Unchecked += (lambda s, e, k=key, iid=item_id:
                              self._on_item_checkbox_changed(s, e, k, iid))
            Grid.SetColumn(cb, 0)
            g.Children.Add(cb)
            self._item_checkbox_refs.setdefault(key, {})[item_id] = cb

        elem_id = str(item.get('id', ''))
        name    = (item.get('name') or item.get('view_name') or
                   item.get('description') or item.get('param_name') or '')
        detail  = (item.get('category') or item.get('level') or
                   item.get('view_type') or item.get('status') or '')

        id_tb = _tb(elem_id, 11, COL_DIMMED)
        id_tb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(id_tb, col_offset + 0)
        g.Children.Add(id_tb)

        name_tb = _tb(name, 11, COL_TEXT)
        name_tb.VerticalAlignment = VerticalAlignment.Center
        name_tb.TextTrimming = System.Windows.TextTrimming.CharacterEllipsis
        Grid.SetColumn(name_tb, col_offset + 1)
        g.Children.Add(name_tb)

        detail_tb = _tb(detail, 11, COL_MUTED)
        detail_tb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(detail_tb, col_offset + 2)
        g.Children.Add(detail_tb)

        # Navigate button (Show in view / Open view)
        nav_keys = {'imported_dwgs', 'unused_families', 'raster_images',
                    'inplace_families', 'groups', 'scope_boxes',
                    'reference_planes', 'unplaced_rooms', 'elements_not_in_phase',
                    'design_options', 'untagged_elements', 'pinned_elements',
                    'sheets_with_no_views', 'duplicate_view_names',
                    'rooms_in_open_space', 'ceiling_height_anomalies',
                    'oversized_families', 'views_not_on_sheets',
                    'view_count_and_templates', 'detail_line_overload',
                    'unused_parameters'}
        if key in nav_keys and elem_id:
            nav_btn = _btn('Show', COL_DARKER, COL_BLUE)
            nav_btn.FontSize = 10
            nav_btn.Padding  = Thickness(6, 2, 6, 2)
            nav_btn.Tag      = elem_id
            nav_btn.Click   += self._on_navigate
            nav_btn.VerticalAlignment = VerticalAlignment.Center
            Grid.SetColumn(nav_btn, col_offset + 3)
            g.Children.Add(nav_btn)

        row.Child = g
        return row

    def _make_action_bar(self, key, result):
        """Action buttons docked below the item list."""
        bar = Border()
        bar.BorderBrush = _brush(COL_BORDER)
        bar.BorderThickness = Thickness(0, 1, 0, 0)
        bar.Padding = Thickness(0, 8, 0, 0)
        bar.Margin  = Thickness(0, 4, 0, 0)

        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal

        ungroup_keys = {'groups'}
        unbind_keys  = {'unused_parameters'}
        rename_keys  = {'duplicate_view_names'}

        count = result.get('count', 0)
        selectable = key in DELETE_ACTION_KEYS

        if selectable:
            sel = self._item_selection.get(key, {})
            selected = sum(1 for v in sel.values() if v)
            note = _tb('{} of {} selected'.format(selected, count), 11, COL_DIMMED)
        else:
            note = _tb('{} issue{} found'.format(
                count, 's' if count != 1 else ''), 11, COL_DIMMED)
        note.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(note)

        self._action_bar_refs[key] = {'note': note, 'del_btn': None}

        if selectable:
            all_items = self._results.get(key, {}).get('items', [])

            all_btn = _btn('Select all', COL_DARKER, COL_MUTED,
                           lambda s, e, k=key, its=all_items:
                               self._set_all_selected(k, its, True))
            all_btn.Margin = Thickness(10, 0, 0, 0)
            sp.Children.Add(all_btn)

            none_btn = _btn('Select none', COL_DARKER, COL_MUTED,
                            lambda s, e, k=key, its=all_items:
                                self._set_all_selected(k, its, False))
            none_btn.Margin = Thickness(6, 0, 0, 0)
            sp.Children.Add(none_btn)

            del_btn = _btn(DELETE_ACTION_LABELS[key],
                           Color.FromRgb(0x5A, 0x1E, 0x1E), COL_RED,
                           lambda s, e, k=key: self._on_delete_action(k))
            del_btn.Margin = Thickness(10, 0, 0, 0)
            del_btn.IsEnabled = selected > 0
            sp.Children.Add(del_btn)
            self._action_bar_refs[key]['del_btn'] = del_btn

        if key in rename_keys:
            rn_btn = _btn('Rename duplicates', Color.FromRgb(0x3A, 0x2E, 0x10), COL_AMBER,
                         lambda s, e: self._on_rename_duplicates())
            rn_btn.Margin = Thickness(10, 0, 0, 0)
            sp.Children.Add(rn_btn)

        if key in ungroup_keys:
            ug_btn = _btn('Ungroup selected', Color.FromRgb(0x3A, 0x2E, 0x10), COL_AMBER,
                          lambda s, e: self._on_ungroup())
            ug_btn.Margin = Thickness(10, 0, 0, 0)
            sp.Children.Add(ug_btn)

        if key in unbind_keys:
            ub_btn = _btn('Unbind selected parameters',
                          Color.FromRgb(0x5A, 0x1E, 0x1E), COL_RED,
                          lambda s, e: self._on_unbind_params())
            ub_btn.Margin = Thickness(10, 0, 0, 0)
            sp.Children.Add(ub_btn)

        # Open Revit warnings dialog
        if key == 'revit_warnings':
            warn_btn = _btn('Open warnings dialog', COL_DARKER, COL_BLUE,
                            self._on_open_warnings)
            warn_btn.Margin = Thickness(10, 0, 0, 0)
            sp.Children.Add(warn_btn)

        # Manage links
        if key == 'link_status':
            links_btn = _btn('Manage links...', COL_DARKER, COL_BLUE,
                             self._on_manage_links)
            links_btn.Margin = Thickness(10, 0, 0, 0)
            sp.Children.Add(links_btn)

        bar.Child = sp
        return bar

    def _render_warnings(self, key, result):
        items = result.get('items', [])
        for grp in items[:20]:
            box = Border()
            box.Background = _brush(COL_PANEL)
            box.BorderBrush = _brush(Color.FromRgb(0x22, 0x22, 0x22))
            box.BorderThickness = Thickness(0, 0, 0, 1)
            box.Padding = Thickness(8, 6, 8, 6)
            box.Margin  = Thickness(0, 0, 0, 2)

            sp = StackPanel()
            desc = _tb(grp.get('description', ''), 11, COL_TEXT, wrap=True)
            count_tb = _tb('{} instance{}'.format(
                grp['count'], 's' if grp['count'] != 1 else ''),
                10, COL_AMBER)
            count_tb.Margin = Thickness(0, 2, 0, 0)
            sp.Children.Add(desc)
            sp.Children.Add(count_tb)
            box.Child = sp
            self._detail_body.Children.Add(box)

        self._detail_body.Children.Add(self._make_action_bar(key, result))

    def _render_guided_tip(self, key, result):
        tips = {
            'workset_usage': (
                'How to fix — empty workset',
                '1. Open Collaborate > Worksets\n'
                '2. Select the empty workset from the list\n'
                '3. Move elements to it, or delete it if unused\n'
                '4. To move: select elements, set Workset in Properties'
            ),
            'grid_level_extents': (
                'How to fix — datum extents',
                '1. Select the grid or level\n'
                '2. Click the 3D extents toggle at the end of the datum\n'
                '3. Drag to extend across all relevant views\n'
                '4. Repeat for each affected grid or level'
            ),
            'design_options': (
                'How to fix — unresolved design options',
                '1. Open Manage > Design Options\n'
                '2. For each option set, accept the primary option\n'
                '   or delete unwanted secondary options\n'
                '3. Repeat until no option sets remain'
            ),
        }
        if key in tips:
            title, body = tips[key]
            self._detail_body.Children.Add(
                self._make_notice_box(title, body, COL_TEAL))
        self._render_generic_table(key, result)

    def _render_links(self, key, result):
        self._detail_body.Children.Add(
            self._make_notice_box(
                'Cannot auto-reload — path may have changed',
                'Use Manage Links to reload or repoint the file. '
                'Ensure the correct version is available on BIM 360.',
                COL_AMBER))
        self._render_generic_table(key, result)

    def _render_param_dupes(self, key, result):
        items = result.get('items', [])
        for item in items:
            box = Border()
            box.Background = _brush(COL_PANEL)
            box.BorderBrush = _brush(Color.FromRgb(0x22, 0x22, 0x22))
            box.BorderThickness = Thickness(0, 0, 0, 1)
            box.Padding = Thickness(8, 6, 8, 6)
            box.Margin  = Thickness(0, 0, 0, 2)

            sp = StackPanel()
            issue_tb = _tb(item.get('issue_type', ''), 10, COL_AMBER, bold=True)
            name_tb  = _tb(item.get('param_name', ''), 11, COL_TEXT)
            vars_tb  = _tb('Variants: ' + ', '.join(item.get('variants', [])),
                           10, COL_MUTED, wrap=True)
            sp.Children.Add(issue_tb)
            sp.Children.Add(name_tb)
            sp.Children.Add(vars_tb)
            box.Child = sp
            self._detail_body.Children.Add(box)

        self._detail_body.Children.Add(
            self._make_notice_box(
                'Cannot auto-fix — manual consolidation required',
                'Review each duplicate in the Shared Parameters file '
                'or via Manage > Project Parameters. '
                'Keep the original GUID; delete the duplicate binding.',
                COL_AMBER))
        self._detail_body.Children.Add(self._make_action_bar(key, result))

    def _render_sp_info(self, key, result):
        self._detail_body.Children.Add(
            self._make_notice_box(
                'Informational — no action required',
                'These parameters exist in the SP file but are not bound in '
                'this model. They may be used in other projects sharing '
                'the same file. No action needed unless cleaning up the SP file.',
                COL_MUTED))
        self._render_generic_table(key, result)

    def _render_duplicate_views(self, key, result):
        """
        Specialist renderer for duplicate_view_names.

        Items here are grouped by shared name AND shared view type —
        {'name': str, 'view_type': str, 'views': [{'id', 'view_type'}, ...]}
        — which doesn't fit the generic single-item table (no per-item
        'id' at the top level), so each group is rendered with its
        member views listed underneath, each with its own Show button.
        (A Floor Plan and a Ceiling Plan sharing a name, e.g. both named
        "Level 1", is normal Revit behaviour and never appears here —
        see the check's docstring in checks.py.)
        """
        items = result.get('items', [])

        self._detail_body.Children.Add(
            self._make_notice_box(
                'Auto-fix available — rename to make unique',
                '"Rename duplicates" keeps the first view in each group '
                'as-is and appends " (2)", " (3)", etc. to the rest. '
                'Use Show on a view first if you\'d rather rename it '
                'yourself with a more meaningful name.',
                COL_TEAL))

        for grp in items[:50]:
            box = Border()
            box.Background = _brush(COL_PANEL)
            box.BorderBrush = _brush(Color.FromRgb(0x22, 0x22, 0x22))
            box.BorderThickness = Thickness(0, 0, 0, 1)
            box.Padding = Thickness(8, 6, 8, 6)
            box.Margin  = Thickness(0, 0, 0, 4)

            sp = StackPanel()
            views = grp.get('views', [])
            name_tb = _tb('{}  ({})  —  {} views'.format(
                             grp.get('name', ''), grp.get('view_type', ''), len(views)),
                         11, COL_TEXT, bold=True)
            sp.Children.Add(name_tb)

            for i, v_info in enumerate(views):
                row = Grid()
                row.ColumnDefinitions.Add(_col_def(60, GridUnitType.Pixel))  # id
                row.ColumnDefinitions.Add(_col_def(1,  GridUnitType.Star))   # type
                row.ColumnDefinitions.Add(_col_def(70, GridUnitType.Pixel))  # show
                row.Margin = Thickness(0, 3, 0, 0)

                elem_id = str(v_info.get('id', ''))
                tag_tb = ('kept — unchanged' if i == 0
                          else v_info.get('view_type', ''))

                id_tb = _tb(elem_id, 10, COL_DIMMED)
                id_tb.VerticalAlignment = VerticalAlignment.Center
                Grid.SetColumn(id_tb, 0)
                row.Children.Add(id_tb)

                type_tb = _tb(tag_tb, 10, COL_TEAL if i == 0 else COL_MUTED)
                type_tb.VerticalAlignment = VerticalAlignment.Center
                Grid.SetColumn(type_tb, 1)
                row.Children.Add(type_tb)

                if elem_id:
                    nav_btn = _btn('Show', COL_DARKER, COL_BLUE)
                    nav_btn.FontSize = 10
                    nav_btn.Padding  = Thickness(6, 2, 6, 2)
                    nav_btn.Tag      = elem_id
                    nav_btn.Click   += self._on_navigate
                    nav_btn.VerticalAlignment = VerticalAlignment.Center
                    Grid.SetColumn(nav_btn, 2)
                    row.Children.Add(nav_btn)

                sp.Children.Add(row)

            box.Child = sp
            self._detail_body.Children.Add(box)

        if len(items) > 50:
            hidden = len(items) - 50
            more = _tb('... {} more duplicate name group{} — export report '
                      'for the full list.'.format(
                          hidden, 's' if hidden != 1 else ''),
                      11, COL_DIMMED, wrap=True)
            more.Margin = Thickness(0, 4, 0, 0)
            self._detail_body.Children.Add(more)

        self._detail_body.Children.Add(self._make_action_bar(key, result))

    # ------------------------------------------------------------------
    # Running checks
    # ------------------------------------------------------------------

    def _on_run(self, sender, e):
        if self._running:
            return
        self._running = True
        self._run_btn.IsEnabled = False
        self._run_btn.Content   = 'Running...'
        self._results = {}
        self._reset_badges()

        # IMPORTANT: checks must run on the main/UI thread. The Revit API
        # is not thread-safe — calling it from a background thread (as a
        # real threading.Thread previously did here) is unsupported and
        # can crash Revit outright, most easily on the heaviest checks
        # (e.g. the parameter checks, which touch every bound parameter
        # across every element in its categories). Instead we process one
        # check per Dispatcher tick at Background priority, which still
        # lets the progress bar repaint between checks without ever
        # leaving the UI thread.
        all_keys = {c[0] for c in checks.CHECKS}
        self._run_check_keys = [k for k in self._check_keys if k in all_keys]
        self._run_check_map  = {c[0]: c for c in checks.CHECKS}
        self._run_total      = len(self._run_check_keys)
        self._run_index      = 0
        self._run_results    = {}

        self.Dispatcher.BeginInvoke(
            DispatcherPriority.Background,
            Action(self._run_next_check))

    def _reset_badges(self):
        for key in self._check_item_map:
            w = self._check_row_widgets.get(key, {})
            if w.get('badge_tb'):
                w['badge_tb'].Text = '—'
                w['badge_tb'].Foreground = _brush(COL_DIMMED)
            if w.get('badge'):
                w['badge'].Background = _brush(COL_DARKER)

    def _run_next_check(self):
        """
        Run a single check on the UI thread, update progress, then
        re-queue itself at Background priority so the dispatcher can
        process pending render/input work (i.e. repaint the progress
        bar) before the next check starts. Runs entirely on the main
        thread — see the comment in _on_run for why.
        """
        if self._run_index >= self._run_total:
            results = self._run_results
            score, counts = checks.calculate_health_score(results)
            self._on_run_complete(results, score, counts)
            return

        key = self._run_check_keys[self._run_index]
        self._run_index += 1

        check_map = self._run_check_map
        if key in check_map:
            _, fn, label, category, weight, opt_in = check_map[key]
            try:
                result = fn(doc)
            except Exception as ex:
                result = {
                    'status':  'ERROR',
                    'count':   0,
                    'items':   [],
                    'message': 'Unhandled error in {}: {}'.format(key, str(ex)),
                }
            result['label']    = label
            result['category'] = category
            result['weight']   = weight
            self._run_results[key] = result

            pct = int((self._run_index / float(self._run_total)) * 100)
            self._update_progress(self._run_index, self._run_total, label, pct)

        self.Dispatcher.BeginInvoke(
            DispatcherPriority.Background,
            Action(self._run_next_check))

    def _update_progress(self, current, total, label, pct):
        self._prog_label.Text = '{} / {} complete'.format(current, total)
        self._prog_bar.Value  = pct
        self._prog_step.Text  = 'Checking {}...'.format(label)

    def _on_run_complete(self, results, score, counts):
        self._results = results
        self._running = False
        self._run_btn.IsEnabled = True
        self._run_btn.Content   = 'Run health check'
        self._prog_step.Text    = 'Complete'
        self._prog_bar.Value    = 100

        # update score bar
        self._score  = score
        self._counts = counts
        col = _score_colour(score)
        self._score_label.Text = str(score)
        self._score_label.Foreground = _brush(col)
        self._score_circle_border.BorderBrush = _brush(col)

        now = datetime.datetime.now().strftime('%H:%M')
        self._score_subtitle.Text = 'Health score — last run: today {}'.format(now)

        self._counts_panel.Children.Clear()
        count_colours = {'OK': COL_TEAL, 'WARN': COL_AMBER,
                         'FAIL': COL_RED, 'INFO': COL_MUTED, 'ERROR': COL_RED}
        for status in ('OK', 'WARN', 'FAIL', 'INFO'):
            n = counts.get(status, 0)
            if n:
                tb = _tb('{}  {}    '.format(n, status), 11,
                         count_colours.get(status, COL_MUTED))
                self._counts_panel.Children.Add(tb)

        # update left-panel badges
        bg_map = {
            'OK':    Color.FromRgb(0x1A, 0x3A, 0x2A),
            'WARN':  Color.FromRgb(0x3A, 0x2E, 0x10),
            'FAIL':  Color.FromRgb(0x3A, 0x1A, 0x1A),
            'INFO':  Color.FromRgb(0x22, 0x22, 0x22),
            'ERROR': Color.FromRgb(0x3A, 0x1A, 0x1A),
        }
        for key, result in results.items():
            if key not in self._check_item_map:
                continue
            w      = self._check_row_widgets.get(key, {})
            status = result.get('status', 'ERROR')
            count  = result.get('count', 0)
            col    = STATUS_COLOURS.get(status, COL_MUTED)
            badge_text = '{} {}'.format(count if count else '0', status)
            if w.get('badge_tb'):
                w['badge_tb'].Text = badge_text
                w['badge_tb'].Foreground = _brush(col)
            if w.get('badge'):
                w['badge'].Background = _brush(bg_map.get(status, COL_DARKER))

        # refresh selected detail panel
        if self._selected_key and self._selected_key in results:
            self._render_detail(self._selected_key,
                                results[self._selected_key])

    # ------------------------------------------------------------------
    # Fix actions
    # ------------------------------------------------------------------

    def _on_navigate(self, sender, e):
        """Zoom Revit to the element by ID."""
        try:
            elem_id_int = int(sender.Tag)
            elem_id = ElementId(elem_id_int)
            uidoc.ShowElements(elem_id)
        except Exception as ex:
            MessageBox.Show('Could not navigate to element:\n{}'.format(str(ex)),
                            'Health Checker', MessageBoxButton.OK)

    def _on_delete_action(self, key):
        """Delete only the checked elements for delete-capable checks."""
        result = self._results.get(key, {})
        items  = result.get('items', [])
        if not items:
            return

        sel = self._item_selection.get(key, {})
        selected_items = [item for item in items if sel.get(item.get('id'), True)]

        if not selected_items:
            MessageBox.Show(
                'No items are selected — check at least one item, '
                'or click "Select all", before deleting.',
                'Health Checker', MessageBoxButton.OK)
            return

        label = DELETE_ITEM_LABELS.get(key, 'element')
        count = len(selected_items)

        answer = MessageBox.Show(
            'Delete {} {}{}?\n\nThis cannot be undone via this tool. '
            'Use Revit Undo (Ctrl+Z) immediately if needed.'.format(
                count, label, 's' if count != 1 else ''),
            'Confirm delete',
            MessageBoxButton.YesNo)
        if answer != MessageBoxResult.Yes:
            return

        deleted = 0
        failed  = 0
        with Transaction(doc, 'Health Checker — delete {}'.format(label)) as t:
            t.Start()
            for item in selected_items:
                try:
                    elem_id = ElementId(item['id'])
                    doc.Delete(elem_id)
                    deleted += 1
                except Exception:
                    failed += 1
            t.Commit()

        MessageBox.Show(
            'Deleted: {}  |  Failed: {}'.format(deleted, failed),
            'Health Checker', MessageBoxButton.OK)
        # items are gone — drop stale selection state, then re-run to refresh
        self._item_selection.pop(key, None)
        self._item_checkbox_refs.pop(key, None)
        self._rerun_single(key)

    def _on_ungroup(self):
        result = self._results.get('groups', {})
        items  = result.get('items', [])
        if not items:
            return
        answer = MessageBox.Show(
            'Ungroup {} group instance{}?\n'
            'This cannot be undone via this tool.'.format(
                len(items), 's' if len(items) != 1 else ''),
            'Confirm ungroup', MessageBoxButton.YesNo)
        if answer != MessageBoxResult.Yes:
            return

        ungrouped = 0
        from Autodesk.Revit.DB import Group
        with Transaction(doc, 'Health Checker — ungroup') as t:
            t.Start()
            for item in items:
                try:
                    elem = doc.GetElement(ElementId(item['id']))
                    if isinstance(elem, Group):
                        elem.UngroupMembers()
                        ungrouped += 1
                except Exception:
                    pass
            t.Commit()

        MessageBox.Show('Ungrouped: {}'.format(ungrouped),
                        'Health Checker', MessageBoxButton.OK)
        self._rerun_single('groups')

    def _on_unbind_params(self):
        result = self._results.get('unused_parameters', {})
        items  = result.get('items', [])
        if not items:
            return
        answer = MessageBox.Show(
            'Unbind {} unused parameter{}?\n'
            'This removes the parameter binding from all '
            'associated categories.'.format(
                len(items), 's' if len(items) != 1 else ''),
            'Confirm unbind', MessageBoxButton.YesNo)
        if answer != MessageBoxResult.Yes:
            return

        unbound = 0
        with Transaction(doc, 'Health Checker — unbind parameters') as t:
            t.Start()
            for item in items:
                try:
                    it = doc.ParameterBindings.ForwardIterator()
                    while it.MoveNext():
                        if it.Key.Name == item['name']:
                            doc.ParameterBindings.Remove(it.Key)
                            unbound += 1
                            break
                except Exception:
                    pass
            t.Commit()

        MessageBox.Show('Unbound: {}'.format(unbound),
                        'Health Checker', MessageBoxButton.OK)
        self._rerun_single('unused_parameters')

    def _on_rename_duplicates(self):
        """
        Fix duplicate_view_names by renaming all but the first view in
        each duplicate-name group, appending ' (2)', ' (3)', etc. Renamed
        names are checked against every existing view name in the model
        (not just the duplicate ones) so a rename can't collide with an
        unrelated view.
        """
        result = self._results.get('duplicate_view_names', {})
        items  = result.get('items', [])
        if not items:
            return

        total_dupes = sum(max(0, len(grp.get('views', [])) - 1) for grp in items)
        if total_dupes == 0:
            return

        answer = MessageBox.Show(
            'Rename {} duplicate view{}?\n\n'
            'The first view in each group keeps its name; the others are '
            'renamed by appending " (2)", " (3)", etc. '
            'This cannot be undone via this tool. '
            'Use Revit Undo (Ctrl+Z) immediately if needed.'.format(
                total_dupes, 's' if total_dupes != 1 else ''),
            'Confirm rename', MessageBoxButton.YesNo)
        if answer != MessageBoxResult.Yes:
            return

        # every current view name, so a new name can't collide with a
        # view outside the duplicate groups either
        existing_names = set()
        try:
            all_views = FilteredElementCollector(doc) \
                .OfClass(View) \
                .WhereElementIsNotElementType()
            for v in all_views:
                try:
                    existing_names.add(v.Name)
                except Exception:
                    continue
        except Exception:
            pass

        renamed = 0
        failed  = 0
        with Transaction(doc, 'Health Checker — rename duplicate views') as t:
            t.Start()
            for grp in items:
                base_name = grp.get('name', '')
                views = grp.get('views', [])
                # first view in the group is left unchanged — matches
                # the "kept — unchanged" row shown in the detail panel
                for idx, v_info in enumerate(views[1:], start=2):
                    try:
                        elem = doc.GetElement(ElementId(v_info['id']))
                        if elem is None:
                            failed += 1
                            continue
                        candidate = '{} ({})'.format(base_name, idx)
                        bump = idx
                        while candidate in existing_names:
                            bump += 1
                            candidate = '{} ({})'.format(base_name, bump)
                        elem.Name = candidate
                        existing_names.add(candidate)
                        renamed += 1
                    except Exception:
                        failed += 1
            t.Commit()

        MessageBox.Show(
            'Renamed: {}  |  Failed: {}'.format(renamed, failed),
            'Health Checker', MessageBoxButton.OK)
        self._rerun_single('duplicate_view_names')

    def _on_open_warnings(self, sender, e):
        try:
            from Autodesk.Revit.UI import PostableCommand, RevitCommandId
            cmd_id = RevitCommandId.LookupPostableCommandId(
                PostableCommand.ReviewWarnings)
            __revit__.PostCommand(cmd_id)
        except Exception as ex:
            MessageBox.Show(
                'Could not open warnings dialog:\n{}\n\n'
                'Open manually via Manage > Review Warnings.'.format(str(ex)),
                'Health Checker', MessageBoxButton.OK)

    def _on_manage_links(self, sender, e):
        try:
            from Autodesk.Revit.UI import PostableCommand, RevitCommandId
            cmd_id = RevitCommandId.LookupPostableCommandId(
                PostableCommand.ManageLinks)
            __revit__.PostCommand(cmd_id)
        except Exception as ex:
            MessageBox.Show(
                'Could not open Manage Links:\n{}\n\n'
                'Open manually via Manage > Manage Links.'.format(str(ex)),
                'Health Checker', MessageBoxButton.OK)

    def _rerun_single(self, key):
        """Re-run one check and refresh its badge and detail panel."""
        check_map = {c[0]: c for c in checks.CHECKS}
        if key not in check_map:
            return
        _, fn, label, category, weight, opt_in = check_map[key]
        try:
            result = fn(doc)
            result['label']    = label
            result['category'] = category
            result['weight']   = weight
            self._results[key] = result
        except Exception:
            return
        # update badge
        if key in self._check_item_map:
            w      = self._check_row_widgets.get(key, {})
            status = result.get('status', 'ERROR')
            count  = result.get('count', 0)
            col    = STATUS_COLOURS.get(status, COL_MUTED)
            if w.get('badge_tb'):
                w['badge_tb'].Text = '{} {}'.format(count, status)
                w['badge_tb'].Foreground = _brush(col)
        # refresh detail if this check is selected
        if self._selected_key == key:
            self._render_detail(key, result)

    # ------------------------------------------------------------------
    # Threshold editor
    # ------------------------------------------------------------------

    def _on_edit_thresholds(self, sender, e):
        """Simple threshold editor dialog."""
        thresh = _load_custom_thresholds()
        defaults = {
            'revit_warnings_warn':      20,
            'revit_warnings_fail':      100,
            'inplace_families_fail':    10,
            'unplaced_rooms_fail':      5,
            'untagged_warn':            10,
            'untagged_fail':            50,
            'views_not_on_sheets_warn': 20,
            'ref_planes_warn':          20,
            'detail_lines_per_view':    500,
            'oversized_family_faces':   5000,
        }

        win = Window()
        win.Title  = 'Edit thresholds'
        win.Width  = 420
        win.Height = 480
        win.Background = _brush(COL_BG)
        win.WindowStartupLocation = \
            System.Windows.WindowStartupLocation.CenterOwner
        win.Owner = self

        sp = StackPanel()
        sp.Margin = Thickness(16)
        fields = {}

        heading = _tb('Threshold values', 13, COL_WHITE, bold=True)
        sp.Children.Add(heading)
        sub = _tb('Changes apply on the next run.', 11, COL_MUTED)
        sub.Margin = Thickness(0, 2, 0, 14)
        sp.Children.Add(sub)

        label_map = {
            'revit_warnings_warn':      'Revit warnings — WARN above',
            'revit_warnings_fail':      'Revit warnings — FAIL above',
            'inplace_families_fail':    'In-place families — FAIL above',
            'unplaced_rooms_fail':      'Unplaced rooms — FAIL above',
            'untagged_warn':            'Untagged elements — WARN above',
            'untagged_fail':            'Untagged elements — FAIL above',
            'views_not_on_sheets_warn': 'Views not on sheets — WARN above',
            'ref_planes_warn':          'Reference planes — WARN above',
            'detail_lines_per_view':    'Detail lines per view — WARN above',
            'oversized_family_faces':   'Oversized family — face count threshold',
        }
        for key, default in defaults.items():
            row = Grid()
            row.ColumnDefinitions.Add(_col_def(1, GridUnitType.Star))
            row.ColumnDefinitions.Add(_col_def(80, GridUnitType.Pixel))
            row.Margin = Thickness(0, 0, 0, 8)

            lbl = _tb(label_map.get(key, key), 11, COL_TEXT)
            lbl.VerticalAlignment = VerticalAlignment.Center
            Grid.SetColumn(lbl, 0)
            row.Children.Add(lbl)

            tb = TextBox()
            tb.Text = str(thresh.get(key, default))
            tb.FontSize = 11
            tb.Background = _brush(COL_PANEL)
            tb.Foreground = _brush(COL_TEXT)
            tb.BorderBrush = _brush(COL_BORDER)
            tb.Padding = Thickness(6, 3, 6, 3)
            tb.Width = 72
            tb.HorizontalAlignment = HorizontalAlignment.Right
            Grid.SetColumn(tb, 1)
            row.Children.Add(tb)
            fields[key] = tb
            sp.Children.Add(row)

        def _save_thresholds(s, e):
            new_thresh = {}
            for k, tb_field in fields.items():
                try:
                    new_thresh[k] = int(tb_field.Text)
                except Exception:
                    new_thresh[k] = defaults[k]
            _save_custom_thresholds(new_thresh)
            win.Close()

        save_btn = _btn('Save', COL_BLUE, Color.FromRgb(0, 0, 0), _save_thresholds, 0)
        save_btn.Margin = Thickness(0, 14, 0, 0)
        sp.Children.Add(save_btn)

        scroll = ScrollViewer()
        scroll.Content = sp
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        win.Content = scroll
        win.ShowDialog()

    # ------------------------------------------------------------------
    # Export and snapshot
    # ------------------------------------------------------------------

    def _on_browse_folder(self, sender, e):
        try:
            from System.Windows.Forms import FolderBrowserDialog, DialogResult
            dlg = FolderBrowserDialog()
            dlg.SelectedPath = self._export_path_tb.Text
            if dlg.ShowDialog() == DialogResult.OK:
                self._export_path_tb.Text = dlg.SelectedPath
                self._last_export_folder  = dlg.SelectedPath
                _cfg_set('export_folder', dlg.SelectedPath)
        except Exception as ex:
            MessageBox.Show('Browse error:\n{}'.format(str(ex)),
                            'Health Checker', MessageBoxButton.OK)

    def _on_save_snapshot(self, sender, e):
        if not self._results:
            MessageBox.Show('Run health check first.',
                            'Health Checker', MessageBoxButton.OK)
            return
        try:
            path = snapshots.save_snapshot(doc, self._results, self._score)
            MessageBox.Show('Snapshot saved:\n{}'.format(path),
                            'Health Checker', MessageBoxButton.OK)
        except Exception as ex:
            MessageBox.Show('Could not save snapshot:\n{}'.format(str(ex)),
                            'Health Checker', MessageBoxButton.OK)

    def _on_export(self, sender, e):
        if not self._results:
            MessageBox.Show('Run health check first.',
                            'Health Checker', MessageBoxButton.OK)
            return

        folder = self._export_path_tb.Text
        if not os.path.exists(folder):
            try:
                os.makedirs(folder)
            except Exception as ex:
                MessageBox.Show('Could not create folder:\n{}'.format(str(ex)),
                                'Health Checker', MessageBoxButton.OK)
                return

        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        proj_name = doc.Title.replace(' ', '_')
        base_name = 'HealthCheck_{}_{}'.format(proj_name, timestamp)

        exported = []
        try:
            import report
        except Exception as ex:
            # A broken/incompatible vendor install (e.g. a Python-3-only
            # openpyxl raising SyntaxError on import — see report.py's
            # own notes) must not take the whole tool down here. report.py
            # already isolates openpyxl-specific failures internally; this
            # is a second line of defence for anything else that goes
            # wrong while loading the module itself.
            MessageBox.Show(
                'Could not load the report module:\n{}: {}'.format(
                    type(ex).__name__, str(ex)),
                'Health Checker', MessageBoxButton.OK)
            return
        try:
            if self._cb_csv.IsChecked:
                p = report.export_csv(
                    self._results, self._score, self._counts,
                    folder, base_name)
                exported.append(p)
        except Exception as ex:
            MessageBox.Show('CSV export error:\n{}'.format(str(ex)),
                            'Health Checker', MessageBoxButton.OK)
        try:
            if self._cb_xlsx.IsChecked:
                p = report.export_xlsx(
                    self._results, self._score, self._counts,
                    folder, base_name)
                exported.append(p)
        except Exception as ex:
            MessageBox.Show('Excel export error:\n{}'.format(str(ex)),
                            'Health Checker', MessageBoxButton.OK)
        try:
            if self._cb_json.IsChecked:
                p = report.export_json(
                    self._results, self._score, self._counts,
                    folder, base_name)
                exported.append(p)
        except Exception as ex:
            MessageBox.Show('JSON export error:\n{}'.format(str(ex)),
                            'Health Checker', MessageBoxButton.OK)

        if exported:
            MessageBox.Show(
                'Exported:\n{}'.format('\n'.join(exported)),
                'Health Checker', MessageBoxButton.OK)
            try:
                import subprocess
                subprocess.Popen(['explorer', '/select,', exported[0]])
            except Exception:
                pass

# ---------------------------------------------------------------------------
# WPF grid helpers
# ---------------------------------------------------------------------------

def _row_def(unit_type, value=1):
    rd = RowDefinition()
    if unit_type == GridUnitType.Auto:
        rd.Height = GridLength(0, GridUnitType.Auto)
    elif unit_type == GridUnitType.Star:
        rd.Height = GridLength(value, GridUnitType.Star)
    else:
        rd.Height = GridLength(value, GridUnitType.Pixel)
    return rd

def _col_def(value_or_unit, unit_type=None):
    cd = ColumnDefinition()
    if unit_type is None:
        # called as _col_def(GridUnitType.Auto)
        cd.Width = GridLength(0, value_or_unit)
    elif unit_type == GridUnitType.Star:
        cd.Width = GridLength(value_or_unit, GridUnitType.Star)
    elif unit_type == GridUnitType.Pixel:
        cd.Width = GridLength(value_or_unit, GridUnitType.Pixel)
    else:
        cd.Width = GridLength(value_or_unit, unit_type)
    return cd

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

win = HealthCheckerWindow()
win.ShowDialog()
