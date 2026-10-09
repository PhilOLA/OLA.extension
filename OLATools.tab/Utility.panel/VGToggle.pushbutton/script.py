# -*- coding: utf-8 -*-
"""
VG Subcategory Toggle  v1.1.0
Toggle visibility of named subcategories in Visibility/Graphic Overrides.
Applies to the active view or a selected view template.
v1.1.0 — adds Revit Links datum-category section (Grids, Levels, Scope Boxes).
"""
import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, Transaction,
    RevitLinkInstance, RevitLinkGraphicsSettings,
    BuiltInCategory, ElementId,
)

from System.Windows import (
    Window, Thickness, WindowStartupLocation, ResizeMode,
    HorizontalAlignment, VerticalAlignment, FontWeights, TextWrapping,
)
from System.Windows import Visibility as WpfVisibility
from System.Windows.Controls import (
    StackPanel, ScrollViewer, ListBox, ListBoxItem,
    CheckBox, RadioButton, Button, GroupBox, TextBlock,
    Orientation, ScrollBarVisibility, WrapPanel,
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

# ── Datum categories to control in linked models ───────────────────────────────
# (display_name, BuiltInCategory)
LINK_DATUM_CATS = [
    ("Grids",       BuiltInCategory.OST_Grids),
    ("Levels",      BuiltInCategory.OST_Levels),
    ("Scope Boxes", BuiltInCategory.OST_VolumeOfInterest),
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


def get_link_instances():
    """Return all RevitLinkInstance elements present in the document, sorted by name."""
    links = list(
        FilteredElementCollector(doc).OfClass(RevitLinkInstance).ToElements()
    )
    return sorted(links, key=lambda l: l.Name)


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
        self.Title = "VG Subcategory Toggle  v1.1.0"
        self.Width    = 660
        self.Height   = 980
        self.MinWidth  = 500
        self.MinHeight = 600
        self.Background = BG
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.CanResizeWithGrip

        self._templates     = get_view_templates()
        self._link_instances = get_link_instances()
        self._target        = doc.ActiveView
        self._matches       = []

        # link-section state lists — populated in _build_ui
        self._link_cat_cbs  = []   # [(display_name, BIC, CheckBox), ...]
        self._link_inst_cbs = []   # [(RevitLinkInstance, CheckBox), ...]

        self._build_ui()
        self._refresh()

    # ── UI helpers ─────────────────────────────────────────────────────────────
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

    def _lbl(self, text, fg=None, bold=False):
        tb = TextBlock()
        tb.Text = text
        tb.Foreground = fg or MUTED
        if bold:
            tb.FontWeight = FontWeights.SemiBold
        tb.Margin = Thickness(0, 0, 0, 4)
        return tb

    # ── Build UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Outer scroll viewer so the window is usable at any height
        outer_sv = ScrollViewer()
        outer_sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        outer_sv.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        self.Content = outer_sv

        root = StackPanel()
        root.Margin = Thickness(14, 14, 14, 10)
        outer_sv.Content = root

        # Title
        ttl = TextBlock()
        ttl.Text = "VG Subcategory Toggle"
        ttl.Foreground = BLUE
        ttl.FontSize = 15
        ttl.FontWeight = FontWeights.SemiBold
        ttl.Margin = Thickness(0, 0, 0, 12)
        root.Children.Add(ttl)

        # ══════════════════════════════════════════════════════════════════════
        # SECTION A — Target
        # ══════════════════════════════════════════════════════════════════════
        g_tgt = self._grp("Target  (applies to both sections below)")
        root.Children.Add(g_tgt)

        tgt_panel = StackPanel()
        g_tgt.Content = tgt_panel

        self._rb_active = RadioButton()
        self._rb_active.Content = u"Active View — {}".format(doc.ActiveView.Name)
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
        self._tpl_lb.Height = 110
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

        # ══════════════════════════════════════════════════════════════════════
        # SECTION B — Subcategory Toggle
        # ══════════════════════════════════════════════════════════════════════
        g_flt = self._grp("A — Subcategory Name Filters")
        root.Children.Add(g_flt)

        sv_flt = ScrollViewer()
        sv_flt.MaxHeight = 140
        sv_flt.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        g_flt.Content = sv_flt

        flt_panel = StackPanel()
        flt_panel.Margin = Thickness(2)
        sv_flt.Content = flt_panel

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

        # Match preview
        g_prv = self._grp("A — Match Preview")
        root.Children.Add(g_prv)

        prv_panel = StackPanel()
        g_prv.Content = prv_panel

        self._summary = TextBlock()
        self._summary.Foreground = MUTED
        self._summary.FontSize = 12
        self._summary.Margin = Thickness(0, 0, 0, 6)
        prv_panel.Children.Add(self._summary)

        self._prv_lb = ListBox()
        self._prv_lb.Height = 200
        self._prv_lb.Background = DARK
        self._prv_lb.Foreground = TEXT
        self._prv_lb.BorderBrush = hex_brush("#404040")
        self._prv_lb.BorderThickness = Thickness(1)
        prv_panel.Children.Add(self._prv_lb)

        # Apply Toggle button
        row_a = StackPanel()
        row_a.Orientation = Orientation.Horizontal
        row_a.HorizontalAlignment = HorizontalAlignment.Right
        row_a.Margin = Thickness(0, 6, 0, 12)
        root.Children.Add(row_a)

        self._apply_btn = self._btn("A — Apply Toggle", self._on_apply, width=160)
        row_a.Children.Add(self._apply_btn)

        # ══════════════════════════════════════════════════════════════════════
        # SECTION C — Revit Links datum visibility
        # ══════════════════════════════════════════════════════════════════════
        g_links = self._grp("B — Revit Links — Datum Category Visibility")
        root.Children.Add(g_links)

        links_panel = StackPanel()
        g_links.Content = links_panel

        # Category checkboxes
        links_panel.Children.Add(self._lbl("Categories to hide / show in selected links:", TEXT))

        cat_row = WrapPanel()
        cat_row.Margin = Thickness(0, 0, 0, 8)
        links_panel.Children.Add(cat_row)

        self._link_cat_cbs = []
        for (name, bic) in LINK_DATUM_CATS:
            cb = CheckBox()
            cb.Content = name
            cb.IsChecked = True
            cb.Foreground = TEXT
            cb.Margin = Thickness(0, 2, 16, 2)
            cat_row.Children.Add(cb)
            self._link_cat_cbs.append((name, bic, cb))

        # Link instance list
        links_panel.Children.Add(self._lbl("Loaded linked models:", TEXT))

        self._link_inst_panel = StackPanel()
        self._link_inst_panel.Margin = Thickness(0, 0, 0, 8)

        sv_links = ScrollViewer()
        sv_links.MaxHeight = 130
        sv_links.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        sv_links.Content = self._link_inst_panel
        links_panel.Children.Add(sv_links)

        self._link_inst_cbs = []
        if self._link_instances:
            for link in self._link_instances:
                cb = CheckBox()
                cb.Content = link.Name
                cb.IsChecked = True
                cb.Foreground = TEXT
                cb.Margin = Thickness(2, 3, 2, 3)
                self._link_inst_panel.Children.Add(cb)
                self._link_inst_cbs.append((link, cb))
        else:
            no_links = TextBlock()
            no_links.Text = "No linked models found in this document."
            no_links.Foreground = MUTED
            self._link_inst_panel.Children.Add(no_links)

        # Select-all / None helpers
        sel_row = StackPanel()
        sel_row.Orientation = Orientation.Horizontal
        sel_row.Margin = Thickness(0, 0, 0, 8)
        links_panel.Children.Add(sel_row)

        def _all(s, e):
            for (_, cb) in self._link_inst_cbs:
                cb.IsChecked = True
        def _none(s, e):
            for (_, cb) in self._link_inst_cbs:
                cb.IsChecked = False

        all_btn = self._btn("Select All", _all, width=90)
        all_btn.Margin = Thickness(0, 0, 6, 0)
        sel_row.Children.Add(all_btn)
        none_btn = self._btn("Select None", _none, width=90)
        sel_row.Children.Add(none_btn)

        # Result label
        self._link_result = TextBlock()
        self._link_result.Foreground = MUTED
        self._link_result.FontSize = 12
        self._link_result.TextWrapping = TextWrapping.Wrap  # Wrap
        self._link_result.Margin = Thickness(0, 0, 0, 6)
        links_panel.Children.Add(self._link_result)

        # Result detail list
        self._link_result_lb = ListBox()
        self._link_result_lb.MaxHeight = 120
        self._link_result_lb.Background = DARK
        self._link_result_lb.Foreground = TEXT
        self._link_result_lb.BorderBrush = hex_brush("#404040")
        self._link_result_lb.BorderThickness = Thickness(1)
        self._link_result_lb.Visibility = WpfVisibility.Collapsed
        links_panel.Children.Add(self._link_result_lb)

        # Hide / Show buttons
        row_b = StackPanel()
        row_b.Orientation = Orientation.Horizontal
        row_b.HorizontalAlignment = HorizontalAlignment.Right
        row_b.Margin = Thickness(0, 8, 0, 0)
        links_panel.Children.Add(row_b)

        self._link_hide_btn = self._btn("B — Hide in Links", self._on_links_hide, width=150)
        row_b.Children.Add(self._link_hide_btn)

        self._link_show_btn = self._btn("B — Show in Links", self._on_links_show, width=150)
        self._link_show_btn.Margin = Thickness(8, 0, 0, 0)
        row_b.Children.Add(self._link_show_btn)

        # Update enabled state
        has_links = bool(self._link_instances)
        self._link_hide_btn.IsEnabled = has_links
        self._link_show_btn.IsEnabled = has_links

        # ── Close ─────────────────────────────────────────────────────────────
        close_row = StackPanel()
        close_row.Orientation = Orientation.Horizontal
        close_row.HorizontalAlignment = HorizontalAlignment.Right
        close_row.Margin = Thickness(0, 12, 0, 4)
        root.Children.Add(close_row)

        close_btn = self._btn("Close", lambda s, e: self.Close(), width=80)
        close_row.Children.Add(close_btn)

    # ── Event handlers — Target ────────────────────────────────────────────────
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

    # ── Event handlers — Section A toggle ─────────────────────────────────────
    def _on_apply(self, sender, e):
        if not self._matches or self._target is None:
            return

        t = Transaction(doc, "VG Subcategory Toggle")
        toggled   = []
        failed    = []
        no_effect = []
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

        parts = []
        if toggled:
            parts.append(u"✓ {} toggled".format(len(toggled)))
        if no_effect:
            parts.append(u"⚠ {} not applied".format(len(no_effect)))
        if failed:
            parts.append(u"✗ {} errored".format(len(failed)))
        self._summary.Text = u"  |  ".join(parts)

        if no_effect or failed:
            self._summary.Foreground = WARNING
            self._prv_lb.Items.Clear()
            if no_effect:
                hdr = ListBoxItem()
                hdr.Content = (u"⚠  Not applied — view template may not "
                               u"control these categories (check its V/G ‘Include’ checkboxes)")
                hdr.Foreground = WARNING
                hdr.IsHitTestVisible = False
                hdr.Background = hex_brush("#2D2200")
                hdr.Padding = Thickness(6, 3, 6, 3)
                self._prv_lb.Items.Add(hdr)
                for (name, parent) in no_effect:
                    li = ListBoxItem()
                    li.Content = u"    {} → {}".format(parent, name)
                    li.Foreground = WARNING
                    li.IsHitTestVisible = False
                    self._prv_lb.Items.Add(li)
            if failed:
                hdr2 = ListBoxItem()
                hdr2.Content = u"✗  Errors"
                hdr2.Foreground = ERROR
                hdr2.IsHitTestVisible = False
                self._prv_lb.Items.Add(hdr2)
                for (name, parent, msg) in failed:
                    li = ListBoxItem()
                    li.Content = u"    {} → {}: {}".format(parent, name, msg)
                    li.Foreground = ERROR
                    li.IsHitTestVisible = False
                    self._prv_lb.Items.Add(li)
        else:
            self._summary.Foreground = SUCCESS

    # ── Event handlers — Section B links ──────────────────────────────────────
    def _on_links_hide(self, sender, e):
        self._apply_link_visibility(hide=True)

    def _on_links_show(self, sender, e):
        self._apply_link_visibility(hide=False)

    def _apply_link_visibility(self, hide):
        target = self._target
        if target is None:
            self._link_result.Text = "No target view selected."
            self._link_result.Foreground = WARNING
            return

        active_cats = [(name, bic) for (name, bic, cb) in self._link_cat_cbs
                       if cb.IsChecked]
        selected_links = [(link, cb) for (link, cb) in self._link_inst_cbs
                          if cb.IsChecked]

        if not active_cats:
            self._link_result.Text = "Check at least one category."
            self._link_result.Foreground = WARNING
            return
        if not selected_links:
            self._link_result.Text = "Select at least one linked model."
            self._link_result.Foreground = WARNING
            return

        ok_list      = []   # (link_name, cat_name) — confirmed
        no_eff_list  = []   # write accepted but read-back unchanged
        err_list     = []   # exception

        t = Transaction(doc, "VG Link Datum Visibility")
        try:
            t.Start()
            for (link, _) in selected_links:
                try:
                    settings = target.GetLinkOverrides(link.Id)
                    # GetLinkOverrides returns None when no overrides exist yet
                    if settings is None:
                        settings = RevitLinkGraphicsSettings()
                except Exception as ex:
                    err_list.append(u"{}: cannot get overrides — {}".format(
                        link.Name, str(ex)))
                    continue

                link_changed = False
                for (cat_name, bic) in active_cats:
                    cat_id = ElementId(bic)
                    try:
                        settings.SetCategoryHidden(cat_id, hide)
                        link_changed = True
                    except Exception as ex:
                        err_list.append(u"{} / {}: {}".format(
                            link.Name, cat_name, str(ex)))

                if link_changed:
                    try:
                        target.SetLinkOverrides(link.Id, settings)
                        # Read-back verification: re-fetch and check
                        settings2 = target.GetLinkOverrides(link.Id)
                        for (cat_name, bic) in active_cats:
                            cat_id = ElementId(bic)
                            try:
                                if settings2 is None:
                                    ok_list.append((link.Name, cat_name))
                                    continue
                                actual = settings2.GetCategoryHidden(cat_id)
                                if actual == hide:
                                    ok_list.append((link.Name, cat_name))
                                else:
                                    no_eff_list.append((link.Name, cat_name))
                            except Exception:
                                # GetCategoryHidden unavailable — assume ok
                                ok_list.append((link.Name, cat_name))
                    except Exception as ex:
                        err_list.append(u"{}: SetLinkOverrides failed — {}".format(
                            link.Name, str(ex)))
            t.Commit()
        except Exception as ex:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            self._link_result.Text = u"Transaction error: {}".format(str(ex))
            self._link_result.Foreground = ERROR
            return

        # ── Summary ───────────────────────────────────────────────────────────
        action = "hidden" if hide else "shown"
        parts = []
        if ok_list:
            parts.append(u"✓ {} category-link pair{} {}".format(
                len(ok_list), "s" if len(ok_list) != 1 else "", action))
        if no_eff_list:
            parts.append(u"⚠ {} not applied (read-back mismatch)".format(len(no_eff_list)))
        if err_list:
            parts.append(u"✗ {} error{}".format(
                len(err_list), "s" if len(err_list) != 1 else ""))

        self._link_result.Text = u"  |  ".join(parts) if parts else "Nothing to report."
        self._link_result.Foreground = SUCCESS if (not no_eff_list and not err_list) else WARNING

        # Detail list
        self._link_result_lb.Items.Clear()
        detail_items = []
        for (lname, cname) in no_eff_list:
            detail_items.append((
                u"⚠  {}: {} — write accepted but state unchanged".format(lname, cname),
                WARNING))
        for msg in err_list:
            detail_items.append((u"✗  {}".format(msg), ERROR))

        if detail_items:
            self._link_result_lb.Visibility = WpfVisibility.Visible
            for (txt, fg) in detail_items:
                li = ListBoxItem()
                li.Content = txt
                li.Foreground = fg
                li.IsHitTestVisible = False
                li.Padding = Thickness(4, 2, 4, 2)
                self._link_result_lb.Items.Add(li)
        else:
            self._link_result_lb.Visibility = WpfVisibility.Collapsed

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
                hdr.Content = u"▸  {}".format(parent)
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
            arrow_tb.Text = u"→ will turn {}".format("ON" if is_hidden else "OFF")
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
