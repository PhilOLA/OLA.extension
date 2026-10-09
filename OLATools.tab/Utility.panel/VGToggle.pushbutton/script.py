# -*- coding: utf-8 -*-
"""
VG Subcategory Toggle  v1.0.0
Toggle visibility of named subcategories in Visibility/Graphic Overrides.
Applies to the active view or a selected view template.
"""
import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from Autodesk.Revit.DB import FilteredElementCollector, View, Transaction

from System.Windows import (
    Window, Thickness, WindowStartupLocation, ResizeMode,
    HorizontalAlignment, FontWeights,
)
from System.Windows import Visibility as WpfVisibility
from System.Windows.Controls import (
    StackPanel, ScrollViewer, ListBox, ListBoxItem,
    CheckBox, RadioButton, Button, GroupBox, TextBlock,
    Orientation, ScrollBarVisibility,
)
from System.Windows.Media import SolidColorBrush, Color


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PREDEFINED SUBCATEGORY NAME FRAGMENTS                                      ║
# ║  Add names here to extend the list. Case-insensitive contains-match.        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
SUBCATEGORY_NAMES = [
    "Center Line",
    "Drop",
    "Rise",
    "Hidden Lines",
    "Above Cut Line",
    "Below Cut Line",
]
# ─────────────────────────────────────────────────────────────────────────────

doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument


# ── Colour helpers ─────────────────────────────────────────────────────────────
def hex_brush(hex_str):
    h = hex_str.lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return SolidColorBrush(Color.FromArgb(255, r, g, b))

BG      = hex_brush("#1E1E1E")
PANEL   = hex_brush("#262626")
DARK    = hex_brush("#181818")
BTN_BG  = hex_brush("#2D2D2D")
TEXT    = hex_brush("#E0E0E0")
MUTED   = hex_brush("#888780")
ACCENT  = hex_brush("#F4723A")
BLUE    = hex_brush("#4FC3F7")
SUCCESS = hex_brush("#4DB6AC")
WARNING = hex_brush("#FFB74D")
ERROR   = hex_brush("#FF6B6B")


# ── Revit helpers ──────────────────────────────────────────────────────────────
def get_view_templates():
    vts = [
        v for v in FilteredElementCollector(doc).OfClass(View).ToElements()
        if v.IsTemplate
    ]
    return sorted(vts, key=lambda v: v.Name)


def get_matches(target_view, active_frags):
    """
    Scan all parent categories for subcategories whose names contain any
    fragment in active_frags (case-insensitive). Returns a sorted list of
    (parent_cat_name, subcat_category, is_hidden).
    Skips any subcategory the view cannot control.
    """
    results = []
    frags = [f.lower() for f in active_frags]
    for cat in doc.Settings.Categories:
        try:
            subcats = cat.SubCategories
        except Exception:
            continue
        for sc in subcats:
            if not any(frag in sc.Name.lower() for frag in frags):
                continue
            try:
                if not target_view.CanCategoryBeHidden(sc.Id):
                    continue
                is_hidden = target_view.GetCategoryHidden(sc.Id)
            except Exception:
                continue
            results.append((cat.Name, sc, is_hidden))
    results.sort(key=lambda x: (x[0], x[1].Name))
    return results


# ── Dialog ─────────────────────────────────────────────────────────────────────
class VGToggleDialog(Window):

    def __init__(self):
        self.Title = "VG Subcategory Toggle  v1.0.0"
        self.Width  = 640
        self.Height = 860
        self.MinWidth  = 480
        self.MinHeight = 500
        self.Background = BG
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.CanResizeWithGrip

        self._templates = get_view_templates()
        self._target    = doc.ActiveView
        self._matches   = []

        self._build_ui()
        self._refresh()

    # ── UI builders ────────────────────────────────────────────────────────────
    def _grp(self, title):
        gb = GroupBox()
        gb.Header = title
        gb.Foreground = ACCENT
        gb.BorderBrush = ACCENT
        gb.BorderThickness = Thickness(1)
        gb.Background = PANEL
        gb.Padding = Thickness(6, 4, 6, 8)
        gb.Margin = Thickness(0, 0, 0, 8)
        return gb

    def _btn(self, text, handler, width=None):
        b = Button()
        b.Content = text
        b.Background = BTN_BG
        b.Foreground = TEXT
        b.BorderBrush = ACCENT
        b.BorderThickness = Thickness(1)
        b.Padding = Thickness(10, 5, 10, 5)
        if width:
            b.Width = width
        b.Click += handler
        return b

    def _build_ui(self):
        root = StackPanel()
        root.Margin = Thickness(14, 14, 14, 10)
        self.Content = root

        # Title
        ttl = TextBlock()
        ttl.Text = "VG Subcategory Toggle"
        ttl.Foreground = BLUE
        ttl.FontSize = 15
        ttl.FontWeight = FontWeights.SemiBold
        ttl.Margin = Thickness(0, 0, 0, 12)
        root.Children.Add(ttl)

        # ── Target ─────────────────────────────────────────────────────────────
        g_tgt = self._grp("Target")
        root.Children.Add(g_tgt)

        tgt_panel = StackPanel()
        g_tgt.Content = tgt_panel

        self._rb_active = RadioButton()
        self._rb_active.Content = u"Active View \u2014 {}".format(doc.ActiveView.Name)
        self._rb_active.IsChecked = True
        self._rb_active.Foreground = TEXT
        self._rb_active.Margin = Thickness(0, 0, 0, 6)
        self._rb_active.Checked += self._on_rb
        tgt_panel.Children.Add(self._rb_active)

        self._rb_tpl = RadioButton()
        self._rb_tpl.Content = "View Template:"
        self._rb_tpl.Foreground = TEXT
        self._rb_tpl.Margin = Thickness(0, 0, 0, 4)
        self._rb_tpl.Checked += self._on_rb
        tgt_panel.Children.Add(self._rb_tpl)

        self._tpl_lb = ListBox()
        self._tpl_lb.Height = 120
        self._tpl_lb.Background = DARK
        self._tpl_lb.Foreground = TEXT
        self._tpl_lb.BorderBrush = hex_brush("#404040")
        self._tpl_lb.BorderThickness = Thickness(1)
        self._tpl_lb.Margin = Thickness(18, 0, 0, 0)
        self._tpl_lb.Visibility = WpfVisibility.Collapsed
        for vt in self._templates:
            li = ListBoxItem()
            li.Content = vt.Name
            li.Tag = vt
            li.Foreground = TEXT
            self._tpl_lb.Items.Add(li)
        self._tpl_lb.SelectionChanged += self._on_tpl_sel
        tgt_panel.Children.Add(self._tpl_lb)

        # ── Name filters ───────────────────────────────────────────────────────
        g_flt = self._grp("Subcategory Name Filters")
        root.Children.Add(g_flt)

        sv = ScrollViewer()
        sv.MaxHeight = 150
        sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        g_flt.Content = sv

        flt_panel = StackPanel()
        flt_panel.Margin = Thickness(2)
        sv.Content = flt_panel

        self._cbs = []
        for name in SUBCATEGORY_NAMES:
            cb = CheckBox()
            cb.Content = name
            cb.IsChecked = True
            cb.Foreground = TEXT
            cb.Margin = Thickness(2, 3, 2, 3)
            cb.Checked   += self._on_filter
            cb.Unchecked += self._on_filter
            flt_panel.Children.Add(cb)
            self._cbs.append(cb)

        # ── Match preview ──────────────────────────────────────────────────────
        g_prv = self._grp("Match Preview")
        root.Children.Add(g_prv)

        prv_panel = StackPanel()
        g_prv.Content = prv_panel

        self._summary = TextBlock()
        self._summary.Foreground = MUTED
        self._summary.FontSize = 12
        self._summary.Margin = Thickness(0, 0, 0, 6)
        prv_panel.Children.Add(self._summary)

        self._prv_lb = ListBox()
        self._prv_lb.Height = 280
        self._prv_lb.Background = DARK
        self._prv_lb.Foreground = TEXT
        self._prv_lb.BorderBrush = hex_brush("#404040")
        self._prv_lb.BorderThickness = Thickness(1)
        # IsHitTestVisible must stay True so the scrollbar captures mouse events.
        # Individual items are set non-interactive below to prevent selection.
        prv_panel.Children.Add(self._prv_lb)

        # ── Buttons ────────────────────────────────────────────────────────────
        btns = StackPanel()
        btns.Orientation = Orientation.Horizontal
        btns.HorizontalAlignment = HorizontalAlignment.Right
        btns.Margin = Thickness(0, 10, 0, 0)
        root.Children.Add(btns)

        self._apply_btn = self._btn("Apply Toggle", self._on_apply, width=130)
        btns.Children.Add(self._apply_btn)

        close_btn = self._btn("Close", lambda s, e: self.Close(), width=80)
        close_btn.Margin = Thickness(8, 0, 0, 0)
        btns.Children.Add(close_btn)

    # ── Event handlers ─────────────────────────────────────────────────────────
    def _on_rb(self, sender, e):
        if self._rb_active.IsChecked:
            self._tpl_lb.Visibility = WpfVisibility.Collapsed
            self._target = doc.ActiveView
        else:
            self._tpl_lb.Visibility = WpfVisibility.Visible
            sel = self._tpl_lb.SelectedItem
            self._target = sel.Tag if sel else None
        self._refresh()

    def _on_tpl_sel(self, sender, e):
        sel = self._tpl_lb.SelectedItem
        if sel:
            self._target = sel.Tag
            self._refresh()

    def _on_filter(self, sender, e):
        self._refresh()

    def _on_apply(self, sender, e):
        if not self._matches or self._target is None:
            return

        # For view templates, SetCategoryHidden can succeed silently but have
        # no effect when the template's "Model Categories" parameter is not
        # included (controlled). We verify each write by reading back the state
        # inside the same transaction, before committing.
        t = Transaction(doc, "VG Subcategory Toggle")
        toggled  = []   # (sc.Name, parent) \u2014 confirmed changed
        failed   = []   # write threw an exception
        no_effect = []  # write accepted but read-back shows unchanged state
        try:
            t.Start()
            for (parent, sc, is_hidden) in self._matches:
                want = not is_hidden
                try:
                    self._target.SetCategoryHidden(sc.Id, want)
                    actual = self._target.GetCategoryHidden(sc.Id)
                    if actual == want:
                        toggled.append((sc.Name, parent))
                    else:
                        no_effect.append((sc.Name, parent))
                except Exception as ex:
                    failed.append((sc.Name, parent, str(ex)))
            t.Commit()
        except Exception as ex:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            self._summary.Text = u"Transaction error: {}".format(str(ex))
            self._summary.Foreground = ERROR
            return

        self._refresh()

        # \u2500\u2500 Build result message \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        parts = []
        if toggled:
            parts.append(u"\u2713 {} toggled".format(len(toggled)))
        if no_effect:
            parts.append(u"\u26a0 {} not applied".format(len(no_effect)))
        if failed:
            parts.append(u"\u2717 {} errored".format(len(failed)))
        self._summary.Text = u"  |  ".join(parts)

        if no_effect or failed:
            self._summary.Foreground = WARNING
            # Surface detail in the preview list so the user can see which failed
            self._prv_lb.Items.Clear()
            if no_effect:
                hdr = ListBoxItem()
                hdr.Content = (u"\u26a0  Not applied \u2014 view template may not "
                               u"control these categories (check its V/G \u2018Include\u2019 checkboxes)")
                hdr.Foreground = WARNING
                hdr.IsHitTestVisible = False
                hdr.Background = hex_brush("#2D2200")
                hdr.Padding = Thickness(6, 3, 6, 3)
                self._prv_lb.Items.Add(hdr)
                for (name, parent) in no_effect:
                    li = ListBoxItem()
                    li.Content = u"    {} \u2192 {}".format(parent, name)
                    li.Foreground = WARNING
                    li.IsHitTestVisible = False
                    self._prv_lb.Items.Add(li)
            if failed:
                hdr2 = ListBoxItem()
                hdr2.Content = u"\u2717  Errors"
                hdr2.Foreground = hex_brush("#FF6B6B")
                hdr2.IsHitTestVisible = False
                self._prv_lb.Items.Add(hdr2)
                for (name, parent, msg) in failed:
                    li = ListBoxItem()
                    li.Content = u"    {} \u2192 {}: {}".format(parent, name, msg)
                    li.Foreground = hex_brush("#FF6B6B")
                    li.IsHitTestVisible = False
                    self._prv_lb.Items.Add(li)
        else:
            self._summary.Foreground = hex_brush("#4DB6AC")

    # ── Preview refresh ────────────────────────────────────────────────────────
    def _refresh(self):
        self._prv_lb.Items.Clear()
        self._apply_btn.IsEnabled = False

        if self._target is None:
            self._summary.Text = "Pick a view template from the list above."
            self._summary.Foreground = WARNING
            return

        active = [cb.Content for cb in self._cbs if cb.IsChecked]
        if not active:
            self._summary.Text = "Check at least one name filter."
            self._summary.Foreground = WARNING
            return

        self._matches = get_matches(self._target, active)

        if not self._matches:
            self._summary.Text = "No matching subcategories found in this view."
            self._summary.Foreground = WARNING
            return

        n_parents = len(set(m[0] for m in self._matches))
        self._summary.Text = u"Found {} subcategor{} across {} parent categor{}.".format(
            len(self._matches), "y" if len(self._matches) == 1 else "ies",
            n_parents, "y" if n_parents == 1 else "ies",
        )
        self._summary.Foreground = BLUE
        self._apply_btn.IsEnabled = True

        last_parent = None
        for (parent, sc, is_hidden) in self._matches:
            if parent != last_parent:
                hdr = ListBoxItem()
                hdr.Content = u"\u25b8  {}".format(parent)
                hdr.Foreground = ACCENT
                hdr.FontWeight = FontWeights.SemiBold
                hdr.IsHitTestVisible = False
                hdr.Background = PANEL
                self._prv_lb.Items.Add(hdr)
                last_parent = parent

            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin = Thickness(14, 1, 4, 1)

            name_tb = TextBlock()
            name_tb.Text = sc.Name
            name_tb.Foreground = TEXT
            name_tb.Width = 200
            row.Children.Add(name_tb)

            state_tb = TextBlock()
            state_tb.Width = 70
            if is_hidden:
                state_tb.Text = "HIDDEN"
                state_tb.Foreground = WARNING
            else:
                state_tb.Text = "VISIBLE"
                state_tb.Foreground = SUCCESS
            row.Children.Add(state_tb)

            arrow_tb = TextBlock()
            arrow_tb.Text = u"\u2192 will turn {}".format("ON" if is_hidden else "OFF")
            arrow_tb.Foreground = MUTED
            row.Children.Add(arrow_tb)

            li = ListBoxItem()
            li.Content = row
            li.Background = DARK
            li.IsHitTestVisible = False
            self._prv_lb.Items.Add(li)


# ── Launch ─────────────────────────────────────────────────────────────────────
dlg = VGToggleDialog()
dlg.ShowDialog()
