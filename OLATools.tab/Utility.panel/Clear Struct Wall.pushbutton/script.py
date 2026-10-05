# -*- coding: utf-8 -*-
"""
Set Structural Walls
pyRevit pushbutton tool
Enables the built-in Structural checkbox (instance parameter) on all
host-model walls in the active document.
Linked-model walls are silently skipped.
Walls in groups or with read-only access are reported but not modified.
"""

__title__   = "Clear\nStructural"
__author__  = "Nano Banana"
__version__ = "1.0.0"

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("System")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    BuiltInCategory,
    BuiltInParameter,
    Wall,
    Transaction,
)
from Autodesk.Revit.DB.Structure import StructuralWallUsage

import System
from System.Windows import (
    Window, WindowStartupLocation, SizeToContent,
    Thickness, HorizontalAlignment, VerticalAlignment,
    TextWrapping,
)
from System.Windows.Controls import (
    StackPanel, TextBlock, Border, ScrollViewer,
    Button, Grid, ColumnDefinition,
    Orientation, Separator,
)
from System.Windows.Media import (
    SolidColorBrush, Color,
)
from System.Windows import GridLength, GridUnitType

# ── pyRevit helpers ─────────────────────────────────────────────────────────
from pyrevit import revit, script

doc   = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

TOOL_VERSION = "1.0.0"

# ── Colours (matching existing tool family) ──────────────────────────────────
CLR_BG          = Color.FromRgb(30,  30,  30)
CLR_SURFACE     = Color.FromRgb(45,  45,  45)
CLR_BORDER      = Color.FromRgb(60,  60,  60)
CLR_ACCENT      = Color.FromRgb(76,  175, 80)   # green
CLR_WARN        = Color.FromRgb(255, 152,  0)   # amber
CLR_TEXT        = Color.FromRgb(220, 220, 220)
CLR_TEXT_DIM    = Color.FromRgb(140, 140, 140)
CLR_BTN_OK      = Color.FromRgb(50,  130,  55)
CLR_BTN_OK_H    = Color.FromRgb(60,  160,  65)
CLR_BTN_CANCEL  = Color.FromRgb(70,   70,  70)


def brush(c):
    return SolidColorBrush(c)


# ── Scan phase ───────────────────────────────────────────────────────────────
def collect_walls():
    """
    Returns:
        to_fix   – list of Wall elements that need Structural ticked ON
        skipped  – list of (Wall, reason_str) for walls that can't be changed
    """
    all_walls = (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Walls)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    to_fix  = []
    skipped = []

    for wall in all_walls:
        if not isinstance(wall, Wall):
            continue

        # Skip walls that live in a linked model (they have no direct owner doc)
        # Linked elements appear via FilteredElementCollector only when you
        # include link instances; since we do NOT include them here the
        # collector already excludes them.  This guard is belt-and-braces.
        if wall.Document.Title != doc.Title:
            continue  # silently skip linked

        # Check if already structural
        param = wall.get_Parameter(BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT)
        if param is None:
            skipped.append((wall, "No Structural parameter found"))
            continue

        if param.IsReadOnly:
            skipped.append((wall, "Parameter is read-only"))
            continue

        if param.AsInteger() == 0:
            # Already un-ticked – nothing to do
            continue

        to_fix.append(wall)

    return to_fix, skipped


# ── WPF Confirmation Dialog ──────────────────────────────────────────────────
class ConfirmDialog(Window):
    def __init__(self, to_fix, skipped):
        self.to_fix   = to_fix
        self.skipped  = skipped
        self.confirmed = False
        self._build_ui()

    def _build_ui(self):
        self.Title                  = "Clear Structural Walls  v{}".format(TOOL_VERSION)
        self.Background             = brush(CLR_BG)
        self.Width                  = 480
        self.SizeToContent          = SizeToContent.Height
        self.WindowStartupLocation  = WindowStartupLocation.CenterScreen
        self.ResizeMode             = System.Windows.ResizeMode.NoResize

        root = StackPanel()
        root.Margin = Thickness(18, 18, 18, 14)
        self.Content = root

        # ── Header ──────────────────────────────────────────────────────────
        hdr = TextBlock()
        hdr.Text       = "Clear Structural Walls"
        hdr.Foreground = brush(CLR_TEXT)
        hdr.FontSize   = 16
        hdr.FontWeight = System.Windows.FontWeights.SemiBold
        hdr.Margin     = Thickness(0, 0, 0, 4)
        root.Children.Add(hdr)

        sub = TextBlock()
        sub.Text       = "Disables the built-in Structural parameter on all host-model walls."
        sub.Foreground = brush(CLR_TEXT_DIM)
        sub.FontSize   = 12
        sub.TextWrapping = TextWrapping.Wrap
        sub.Margin     = Thickness(0, 0, 0, 14)
        root.Children.Add(sub)

        # ── Stats row ───────────────────────────────────────────────────────
        stats_border = Border()
        stats_border.Background    = brush(CLR_SURFACE)
        stats_border.BorderBrush   = brush(CLR_BORDER)
        stats_border.BorderThickness = Thickness(1)
        stats_border.CornerRadius  = System.Windows.CornerRadius(4)
        stats_border.Padding       = Thickness(14, 10, 14, 10)
        stats_border.Margin        = Thickness(0, 0, 0, 14)

        stats_grid = Grid()
        for _ in range(4):
            col = ColumnDefinition()
            col.Width = GridLength(1, GridUnitType.Star)
            stats_grid.ColumnDefinitions.Add(col)

        def stat_col(label, value, col_idx, accent=CLR_ACCENT):
            sp = StackPanel()
            sp.HorizontalAlignment = HorizontalAlignment.Center

            val_tb = TextBlock()
            val_tb.Text       = str(value)
            val_tb.Foreground = brush(accent)
            val_tb.FontSize   = 22
            val_tb.FontWeight = System.Windows.FontWeights.Bold
            val_tb.HorizontalAlignment = HorizontalAlignment.Center
            sp.Children.Add(val_tb)

            lbl_tb = TextBlock()
            lbl_tb.Text       = label
            lbl_tb.Foreground = brush(CLR_TEXT_DIM)
            lbl_tb.FontSize   = 10
            lbl_tb.HorizontalAlignment = HorizontalAlignment.Center
            sp.Children.Add(lbl_tb)

            Grid.SetColumn(sp, col_idx)
            stats_grid.Children.Add(sp)

        already_structural = 0
        all_host = (
            FilteredElementCollector(doc)
            .OfCategory(BuiltInCategory.OST_Walls)
            .WhereElementIsNotElementType()
            .ToElements()
        )
        for w in all_host:
            if isinstance(w, Wall):
                p = w.get_Parameter(BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT)
                if p and p.AsInteger() == 0:
                    already_structural += 1

        stat_col("WILL BE UPDATED",  len(self.to_fix),          0, CLR_ACCENT)
        stat_col("ALREADY NON-STRUCTURAL", already_structural,  1, CLR_TEXT_DIM)
        stat_col("CANNOT UPDATE",    len(self.skipped),          2, CLR_WARN if self.skipped else CLR_TEXT_DIM)
        stat_col("TOTAL HOST WALLS", len(self.to_fix) + already_structural + len(self.skipped), 3, CLR_TEXT_DIM)

        stats_border.Child = stats_grid
        root.Children.Add(stats_border)

        # ── Skipped list (only if there are any) ────────────────────────────
        if self.skipped:
            warn_hdr = TextBlock()
            warn_hdr.Text       = "⚠  Walls that cannot be updated:"
            warn_hdr.Foreground = brush(CLR_WARN)
            warn_hdr.FontSize   = 12
            warn_hdr.Margin     = Thickness(0, 0, 0, 6)
            root.Children.Add(warn_hdr)

            sv = ScrollViewer()
            sv.MaxHeight           = 160
            sv.VerticalScrollBarVisibilityProperty
            sv.SetValue(
                ScrollViewer.VerticalScrollBarVisibilityProperty,
                System.Windows.Controls.ScrollBarVisibility.Auto,
            )
            sv.Margin = Thickness(0, 0, 0, 14)

            list_panel = StackPanel()
            list_panel.Background = brush(CLR_SURFACE)

            for wall, reason in self.skipped:
                try:
                    wall_name = "ID {} – {}".format(
                        wall.Id.IntegerValue,
                        wall.Name if wall.Name else "(unnamed)"
                    )
                except Exception:
                    wall_name = "ID {}".format(wall.Id.IntegerValue)

                row = Border()
                row.BorderBrush      = brush(CLR_BORDER)
                row.BorderThickness  = Thickness(0, 0, 0, 1)
                row.Padding          = Thickness(10, 5, 10, 5)

                row_sp = StackPanel()
                row_sp.Orientation = Orientation.Horizontal

                name_tb = TextBlock()
                name_tb.Text       = wall_name
                name_tb.Foreground = brush(CLR_TEXT)
                name_tb.FontSize   = 11
                name_tb.Width      = 250
                row_sp.Children.Add(name_tb)

                reason_tb = TextBlock()
                reason_tb.Text       = reason
                reason_tb.Foreground = brush(CLR_WARN)
                reason_tb.FontSize   = 11
                reason_tb.TextWrapping = TextWrapping.Wrap
                row_sp.Children.Add(reason_tb)

                row.Child = row_sp
                list_panel.Children.Add(row)

            sv.Content = list_panel
            root.Children.Add(sv)

        # ── Separator ───────────────────────────────────────────────────────
        sep = Separator()
        sep.Background = brush(CLR_BORDER)
        sep.Margin     = Thickness(0, 0, 0, 12)
        root.Children.Add(sep)

        # ── Buttons ─────────────────────────────────────────────────────────
        btn_row = Grid()
        btn_row.ColumnDefinitions.Add(ColumnDefinition())
        btn_row.ColumnDefinitions.Add(ColumnDefinition())
        col_gap = ColumnDefinition()
        col_gap.Width = GridLength(10)
        # insert gap between columns by using margin instead

        cancel_btn = Button()
        cancel_btn.Content    = "Cancel"
        cancel_btn.Background = brush(CLR_BTN_CANCEL)
        cancel_btn.Foreground = brush(CLR_TEXT)
        cancel_btn.BorderThickness = Thickness(0)
        cancel_btn.Height     = 34
        cancel_btn.FontSize   = 13
        cancel_btn.Margin     = Thickness(0, 0, 6, 0)
        cancel_btn.Cursor     = System.Windows.Input.Cursors.Hand
        cancel_btn.Click     += self._on_cancel
        Grid.SetColumn(cancel_btn, 0)
        btn_row.Children.Add(cancel_btn)

        label = "Clear {} Wall{}".format(
            len(self.to_fix), "s" if len(self.to_fix) != 1 else ""
        )
        ok_btn = Button()
        ok_btn.Content    = label
        ok_btn.Background = brush(CLR_BTN_OK)
        ok_btn.Foreground = brush(CLR_TEXT)
        ok_btn.BorderThickness = Thickness(0)
        ok_btn.Height     = 34
        ok_btn.FontSize   = 13
        ok_btn.FontWeight = System.Windows.FontWeights.SemiBold
        ok_btn.Margin     = Thickness(6, 0, 0, 0)
        ok_btn.Cursor     = System.Windows.Input.Cursors.Hand
        ok_btn.IsEnabled  = len(self.to_fix) > 0
        ok_btn.Click     += self._on_ok
        Grid.SetColumn(ok_btn, 1)
        btn_row.Children.Add(ok_btn)

        root.Children.Add(btn_row)

    def _on_ok(self, sender, e):
        self.confirmed = True
        self.Close()

    def _on_cancel(self, sender, e):
        self.confirmed = False
        self.Close()


# ── Results Dialog ───────────────────────────────────────────────────────────
class ResultsDialog(Window):
    def __init__(self, updated, failed):
        self.updated = updated
        self.failed  = failed
        self._build_ui()

    def _build_ui(self):
        self.Title                 = "Clear Structural Walls – Results"
        self.Background            = brush(CLR_BG)
        self.Width                 = 420
        self.SizeToContent         = SizeToContent.Height
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode            = System.Windows.ResizeMode.NoResize

        root = StackPanel()
        root.Margin = Thickness(18, 18, 18, 14)
        self.Content = root

        success = len(self.updated)
        fail    = len(self.failed)

        # Header
        hdr = TextBlock()
        hdr.Text       = "✓  Done" if fail == 0 else "⚠  Completed with issues"
        hdr.Foreground = brush(CLR_ACCENT if fail == 0 else CLR_WARN)
        hdr.FontSize   = 16
        hdr.FontWeight = System.Windows.FontWeights.SemiBold
        hdr.Margin     = Thickness(0, 0, 0, 12)
        root.Children.Add(hdr)

        # Stats
        stats_border = Border()
        stats_border.Background      = brush(CLR_SURFACE)
        stats_border.BorderBrush     = brush(CLR_BORDER)
        stats_border.BorderThickness = Thickness(1)
        stats_border.CornerRadius    = System.Windows.CornerRadius(4)
        stats_border.Padding         = Thickness(14, 10, 14, 10)
        stats_border.Margin          = Thickness(0, 0, 0, 14)

        stats_grid = Grid()
        for _ in range(2):
            col = ColumnDefinition()
            col.Width = GridLength(1, GridUnitType.Star)
            stats_grid.ColumnDefinitions.Add(col)

        def stat_col(label, value, col_idx, accent=CLR_ACCENT):
            sp = StackPanel()
            sp.HorizontalAlignment = HorizontalAlignment.Center
            val_tb = TextBlock()
            val_tb.Text       = str(value)
            val_tb.Foreground = brush(accent)
            val_tb.FontSize   = 22
            val_tb.FontWeight = System.Windows.FontWeights.Bold
            val_tb.HorizontalAlignment = HorizontalAlignment.Center
            sp.Children.Add(val_tb)
            lbl_tb = TextBlock()
            lbl_tb.Text       = label
            lbl_tb.Foreground = brush(CLR_TEXT_DIM)
            lbl_tb.FontSize   = 10
            lbl_tb.HorizontalAlignment = HorizontalAlignment.Center
            sp.Children.Add(lbl_tb)
            Grid.SetColumn(sp, col_idx)
            stats_grid.Children.Add(sp)

        stat_col("UPDATED",     success, 0, CLR_ACCENT)
        stat_col("FAILED",      fail,    1, CLR_WARN if fail else CLR_TEXT_DIM)

        stats_border.Child = stats_grid
        root.Children.Add(stats_border)

        # Failed list
        if self.failed:
            fail_hdr = TextBlock()
            fail_hdr.Text       = "Walls that failed during transaction:"
            fail_hdr.Foreground = brush(CLR_WARN)
            fail_hdr.FontSize   = 12
            fail_hdr.Margin     = Thickness(0, 0, 0, 6)
            root.Children.Add(fail_hdr)

            sv = ScrollViewer()
            sv.MaxHeight = 140
            sv.SetValue(
                ScrollViewer.VerticalScrollBarVisibilityProperty,
                System.Windows.Controls.ScrollBarVisibility.Auto,
            )
            sv.Margin = Thickness(0, 0, 0, 14)

            lp = StackPanel()
            lp.Background = brush(CLR_SURFACE)
            for wall, err in self.failed:
                row = Border()
                row.BorderBrush     = brush(CLR_BORDER)
                row.BorderThickness = Thickness(0, 0, 0, 1)
                row.Padding         = Thickness(10, 5, 10, 5)
                tb = TextBlock()
                tb.Text        = "ID {} – {}".format(wall.Id.IntegerValue, str(err)[:80])
                tb.Foreground  = brush(CLR_WARN)
                tb.FontSize    = 11
                tb.TextWrapping = TextWrapping.Wrap
                row.Child = tb
                lp.Children.Add(row)
            sv.Content = lp
            root.Children.Add(sv)

        sep = Separator()
        sep.Background = brush(CLR_BORDER)
        sep.Margin     = Thickness(0, 0, 0, 12)
        root.Children.Add(sep)

        close_btn = Button()
        close_btn.Content         = "Close"
        close_btn.Background      = brush(CLR_BTN_OK)
        close_btn.Foreground      = brush(CLR_TEXT)
        close_btn.BorderThickness = Thickness(0)
        close_btn.Height          = 34
        close_btn.FontSize        = 13
        close_btn.HorizontalAlignment = HorizontalAlignment.Right
        close_btn.Width           = 120
        close_btn.Cursor          = System.Windows.Input.Cursors.Hand
        close_btn.Click          += lambda s, e: self.Close()
        root.Children.Add(close_btn)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    to_fix, skipped = collect_walls()

    dlg = ConfirmDialog(to_fix, skipped)
    dlg.ShowDialog()

    if not dlg.confirmed:
        logger.info("User cancelled.")
        return

    updated = []
    failed  = []

    with Transaction(doc, "Clear Structural Walls") as t:
        t.Start()
        for wall in to_fix:
            try:
                param = wall.get_Parameter(BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT)
                if param and not param.IsReadOnly:
                    param.Set(0)
                    updated.append(wall)
                else:
                    failed.append((wall, "Param became read-only inside transaction"))
            except Exception as ex:
                failed.append((wall, ex))
        t.Commit()

    results = ResultsDialog(updated, failed)
    results.ShowDialog()


main()
