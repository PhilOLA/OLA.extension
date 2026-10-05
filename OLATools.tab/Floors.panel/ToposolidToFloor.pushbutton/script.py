"""
Toposolid to Floor
-------------------
Select a Toposolid (or a Toposolid sub-region) and create a new Floor that
follows the SAME sloped/undulating top surface - not just a flat floor
matching the footprint.

How it works:
1. Boundary comes from the toposolid's own sketch profile (flat, planar -
   this is what you originally drew, so it's always valid for Floor.Create).
2. A flat Floor is created on that boundary, on the Level and Floor Type
   you choose.
3. Real 3D points are sampled off the toposolid's actual top faces, and
   pushed onto the new floor via Revit's shape-editing API (the same
   mechanism behind Floor > Modify Sub Elements > Add Point), so the
   floor's top surface is draped to match the toposolid's slope.

Revit 2024 (Toposolid API). This uses a more obscure part of the API
(SlabShapeEditor) than a typical pyRevit script, so if something looks
off, the traceback printed at the bottom will show exactly where.
"""

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from System.Collections.Generic import List

from Autodesk.Revit.DB import (
    Element,
    Options,
    ViewDetailLevel,
    Solid,
    PlanarFace,
    CurveLoop,
    Line,
    XYZ,
    Floor,
    FloorType,
    FilteredElementCollector,
    Transaction,
    TransactionGroup,
    IFailuresPreprocessor,
    FailureProcessingResult,
    FailureSeverity,
    Level,
)
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

from pyrevit import revit, forms, script
import traceback

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
config = script.get_config()

LAST_LEVEL_KEY = "last_level_name"
LAST_FLOOR_TYPE_KEY = "last_floor_type_name"
LAST_SPACING_MM_KEY = "last_point_spacing_mm"
LAST_DRAPE_KEY = "last_drape_enabled"

MM_PER_FOOT = 304.8
DEFAULT_SPACING_MM = 300

# Hard cap as a final safety net after grid thinning, regardless of the
# spacing value chosen in the dialog.
MAX_INTERIOR_POINTS = 800


class DrapeFailureHandler(IFailuresPreprocessor):
    """Auto-dismiss harmless warnings (off-axis sketch lines, "extreme
    shape editing" thickness notices) so they never pop an interactive
    dialog. A genuine Error still rolls back - but since every point gets
    its own tiny Transaction, that only discards the one point causing it,
    not the whole floor."""

    def PreprocessFailures(self, failures_accessor):
        had_error = False
        for f in list(failures_accessor.GetFailureMessages()):
            if f.GetSeverity() == FailureSeverity.Error:
                had_error = True
            else:
                try:
                    failures_accessor.DeleteWarning(f)
                except Exception:
                    pass
        if had_error:
            return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


def run_isolated(doc, name, action):
    """Run `action()` in its own Transaction with warnings auto-dismissed.
    Returns True if it committed, False if it was rolled back (silently -
    no dialog is shown either way)."""
    t = Transaction(doc, name)
    t.Start()
    try:
        opts = t.GetFailureHandlingOptions()
        opts.SetFailuresPreprocessor(DrapeFailureHandler())
        t.SetFailureHandlingOptions(opts)
        action()
        t.Commit()
        return True
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        return False


class ToposolidSelectionFilter(ISelectionFilter):
    """Only allow picking Toposolid elements or Toposolid sub-regions."""

    def AllowElement(self, element):
        cat = element.Category
        if cat is None:
            return False
        return "Toposolid" in cat.Name

    def AllowReference(self, reference, position):
        return True


def get_name(element):
    """ElementType.Name is an explicit interface property under IronPython,
    so plain `.Name` raises AttributeError on types like FloorType (though
    it works fine on instances like Level)."""
    try:
        return element.Name
    except AttributeError:
        return Element.Name.__get__(element)


def get_top_planar_face(element):
    """Return the largest upward-facing planar face on the element, or None.
    Used only as a boundary fallback when the element has no usable sketch.
    """
    opt = Options()
    opt.ComputeReferences = True
    opt.DetailLevel = ViewDetailLevel.Fine

    geom = element.get_Geometry(opt)
    if geom is None:
        return None

    best_face = None
    best_area = 0.0

    for geo_obj in geom:
        solid = geo_obj if isinstance(geo_obj, Solid) else None
        if solid is None or solid.Faces.IsEmpty:
            continue
        for face in solid.Faces:
            if isinstance(face, PlanarFace) and face.FaceNormal.Z > 0.9:
                if face.Area > best_area:
                    best_area = face.Area
                    best_face = face

    return best_face


def flatten_curve_loop(loop, target_z):
    """Rebuild a CurveLoop with every point pinned to target_z (fallback
    boundary path only - see flatten note in get_top_planar_face callers)."""
    points = []
    for curve in loop:
        pts = list(curve.Tessellate())
        if points and points[-1].IsAlmostEqualTo(pts[0]):
            pts = pts[1:]
        points.extend(pts)

    flat_points = [XYZ(p.X, p.Y, target_z) for p in points]
    if not flat_points[0].IsAlmostEqualTo(flat_points[-1]):
        flat_points.append(flat_points[0])

    new_loop = CurveLoop()
    for i in range(len(flat_points) - 1):
        if flat_points[i].DistanceTo(flat_points[i + 1]) < 0.0007:  # ~0.2mm
            continue
        new_loop.Append(Line.CreateBound(flat_points[i], flat_points[i + 1]))
    return new_loop


def get_boundary_loops_and_base_z(element):
    """Prefer the element's own sketch profile (already flat & planar - this
    is literally the boundary the user drew). Falls back to deriving one
    from geometry if no sketch is found (e.g. on some sub-region elements).

    Returns (curve_loops, base_z, used_sketch_bool).
    """
    try:
        sketch_id = element.SketchId
    except AttributeError:
        sketch_id = None

    if sketch_id is not None and sketch_id.IntegerValue > 0:
        sketch = doc.GetElement(sketch_id)
        if sketch is not None and sketch.Profile is not None:
            loops = []
            for curve_array in sketch.Profile:
                loop = CurveLoop()
                for curve in curve_array:
                    loop.Append(curve)
                loops.append(loop)
            if loops:
                first_curve = list(loops[0])[0]
                base_z = first_curve.GetEndPoint(0).Z
                return loops, base_z, True

    # Fallback: derive a flat boundary from the largest upward-facing face.
    top_face = get_top_planar_face(element)
    if top_face is None:
        return None, None, False
    raw_loops = list(top_face.GetEdgesAsCurveLoops())
    if not raw_loops:
        return None, None, False
    base_z = top_face.Origin.Z
    loops = [flatten_curve_loop(loop, base_z) for loop in raw_loops]
    return loops, base_z, False


def collect_surface_points(element, min_normal_z=0.05):
    """Sample real 3D points off every upward-facing planar face of the
    element - these define the actual sloped/undulating shape."""
    opt = Options()
    opt.DetailLevel = ViewDetailLevel.Fine
    geom = element.get_Geometry(opt)
    points = []
    if geom is None:
        return points

    for geo_obj in geom:
        solid = geo_obj if isinstance(geo_obj, Solid) else None
        if solid is None or solid.Faces.IsEmpty:
            continue
        for face in solid.Faces:
            if not (isinstance(face, PlanarFace) and face.FaceNormal.Z > min_normal_z):
                continue
            mesh = face.Triangulate()
            for i in range(mesh.NumTriangles):
                tri = mesh.get_Triangle(i)
                for j in range(3):
                    points.append(tri.get_Vertex(j))

    return points


def loop_to_2d_points(loop):
    """Tessellate a CurveLoop into a flat (x, y) polygon (Z dropped)."""
    pts = []
    for curve in loop:
        tpts = list(curve.Tessellate())
        if pts:
            last = pts[-1]
            first = (tpts[0].X, tpts[0].Y)
            if abs(last[0] - first[0]) < 1e-6 and abs(last[1] - first[1]) < 1e-6:
                tpts = tpts[1:]
        pts.extend([(p.X, p.Y) for p in tpts])
    return pts


def polygon_area(poly):
    """Shoelace formula. For a multi-loop boundary this overcounts holes
    slightly (it doesn't subtract them), which is fine for a rough sizing
    heuristic."""
    area = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def recommend_spacing_mm(curve_loops, target_point_count=150):
    """Suggest a starting point spacing based on the shape's size and
    complexity. Larger/simpler shapes can afford finer (smaller) spacing;
    smaller or more complex (many boundary segments - a proxy for tight
    bends/branches, per what actually caused the aggregate shape-edit
    failure on a branching footprint) shapes get a coarser recommendation,
    biased toward reliability over surface detail."""
    polygons = [loop_to_2d_points(loop) for loop in curve_loops]
    total_area_sqft = sum(polygon_area(p) for p in polygons)
    segment_count = sum(len(p) for p in polygons)

    if total_area_sqft <= 0:
        return DEFAULT_SPACING_MM

    spacing_ft = (total_area_sqft / float(target_point_count)) ** 0.5

    # Bump up the recommendation for shapes with lots of boundary segments
    # relative to their area (narrow/branching/notched geometry).
    if segment_count > 60:
        spacing_ft *= 1.5
    elif segment_count > 25:
        spacing_ft *= 1.2

    spacing_ft = max(0.5, min(spacing_ft, 2.0))  # clamp to 150mm-600mm
    spacing_mm = spacing_ft * MM_PER_FOOT
    return int(round(spacing_mm / 10.0) * 10)  # round to nearest 10mm


def point_in_polygon(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def filter_points_to_boundary(points, curve_loops, margin=0.05):
    """Keep only points that fall within the boundary loops (even-odd rule,
    so holes/nested loops are handled correctly), with a small outward
    margin so legitimate boundary/corner points aren't clipped off by
    floating-point noise."""
    polygons = [loop_to_2d_points(loop) for loop in curve_loops]

    def is_inside(p):
        count = 0
        for poly in polygons:
            if point_in_polygon(p.X, p.Y, poly):
                count += 1
        if count % 2 == 1:
            return True
        # Not strictly inside - allow it if it's very close to an edge.
        for poly in polygons:
            n = len(poly)
            for i in range(n):
                xi, yi = poly[i]
                xj, yj = poly[(i + 1) % n]
                # crude point-to-segment distance check
                dx, dy = xj - xi, yj - yi
                length_sq = dx * dx + dy * dy
                if length_sq == 0:
                    continue
                t = max(0.0, min(1.0, ((p.X - xi) * dx + (p.Y - yi) * dy) / length_sq))
                cx, cy = xi + t * dx, yi + t * dy
                if ((p.X - cx) ** 2 + (p.Y - cy) ** 2) ** 0.5 < margin:
                    return True
        return False

    return [p for p in points if is_inside(p)]


def thin_points_by_grid(points, cell_size):
    """Keep at most one point per (cell_size x cell_size) XY grid cell, for
    an even spatial spread rather than whatever density the mesh happened
    to triangulate at (which tends to be much denser at tight bends)."""
    buckets = {}
    for p in points:
        key = (int(p.X // cell_size), int(p.Y // cell_size))
        if key not in buckets:
            buckets[key] = p
    return list(buckets.values())


def dedupe_points(points, tol=0.01):
    """O(n^2) dedupe - fine up to a few thousand points, which is why the
    caller pre-limits how many raw points get this far."""
    unique = []
    for p in points:
        is_dupe = False
        for u in unique:
            if p.DistanceTo(u) < tol:
                is_dupe = True
                break
        if not is_dupe:
            unique.append(p)
    return unique


FORM_XAML = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Toposolid to Floor"
    Height="620" Width="460"
    MinHeight="560" MinWidth="420"
    WindowStartupLocation="CenterScreen"
    ResizeMode="CanResize"
    Background="#FF262626"
    FontFamily="Segoe UI">
    <Window.Resources>
        <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="#FFE6E6E6"/>
        </Style>
        <Style x:Key="SectionLabel" TargetType="TextBlock">
            <Setter Property="Foreground" Value="#FFC8C8C8"/>
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Margin" Value="0,14,0,4"/>
        </Style>
        <Style x:Key="RequiredLabel" TargetType="TextBlock" BasedOn="{StaticResource SectionLabel}">
            <Setter Property="Foreground" Value="#FF4FC3F7"/>
            <Setter Property="FontSize" Value="12"/>
        </Style>
        <Style TargetType="ComboBox">
            <Setter Property="Background" Value="#FF3A3A3A"/>
            <Setter Property="Foreground" Value="Black"/>
            <Setter Property="Padding" Value="6,4"/>
            <Setter Property="Height" Value="28"/>
            <Setter Property="BorderBrush" Value="#FF4F4F4F"/>
        </Style>
        <Style TargetType="TextBox">
            <Setter Property="Background" Value="#FF3A3A3A"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Padding" Value="6,5"/>
            <Setter Property="BorderBrush" Value="#FF4F4F4F"/>
            <Setter Property="CaretBrush" Value="White"/>
            <Style.Triggers>
                <Trigger Property="IsEnabled" Value="False">
                    <Setter Property="Background" Value="#FF2C2C2C"/>
                    <Setter Property="Foreground" Value="#FF707070"/>
                    <Setter Property="BorderBrush" Value="#FF3A3A3A"/>
                </Trigger>
            </Style.Triggers>
        </Style>
        <Style x:Key="RequiredComboBox" TargetType="ComboBox" BasedOn="{StaticResource {x:Type ComboBox}}">
            <Setter Property="BorderBrush" Value="#FF4FC3F7"/>
            <Setter Property="BorderThickness" Value="1.5"/>
        </Style>
        <Style TargetType="CheckBox">
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Margin" Value="2"/>
        </Style>
        <Style TargetType="Button">
            <Setter Property="Background" Value="#FF0A84D8"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Padding" Value="12,6"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Margin" Value="6,0,0,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="FontSize" Value="12"/>
        </Style>
        <Style x:Key="SecondaryButton" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
            <Setter Property="Background" Value="#FF4A4A4A"/>
        </Style>
    </Window.Resources>

    <ScrollViewer VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Disabled">
    <Grid Margin="18">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <StackPanel Grid.Row="0">
            <TextBlock Text="Toposolid to Floor" FontSize="17" FontWeight="Bold"/>
            <TextBlock Text="Pick a Toposolid or sub-region, then set the options below."
                       Foreground="#FF9B9B9B" FontSize="11" Margin="0,3,0,0"/>
        </StackPanel>

        <StackPanel Grid.Row="1" Margin="0,16,0,0">
            <TextBlock Text="Toposolid / Sub-region" Style="{StaticResource RequiredLabel}"/>
            <TextBlock x:Name="txt_selection_summary"
                       Text="No element selected yet."
                       Foreground="#FF9B9B9B" FontSize="11" Margin="0,4,0,0"
                       TextWrapping="Wrap"/>
        </StackPanel>

        <StackPanel Grid.Row="2">
            <TextBlock Text="Level" Style="{StaticResource RequiredLabel}"/>
            <ComboBox x:Name="cmb_level" Style="{StaticResource RequiredComboBox}"/>
        </StackPanel>

        <StackPanel Grid.Row="3">
            <TextBlock Text="Floor Type" Style="{StaticResource RequiredLabel}"/>
            <ComboBox x:Name="cmb_floor_type" Style="{StaticResource RequiredComboBox}"/>
        </StackPanel>

        <StackPanel Grid.Row="4" Orientation="Horizontal" Margin="0,16,0,0">
            <CheckBox x:Name="chk_drape" Content="Drape to match toposolid slope"
                      IsChecked="True" VerticalAlignment="Center"
                      Checked="chk_drape_toggled" Unchecked="chk_drape_toggled"/>
        </StackPanel>

        <StackPanel Grid.Row="5">
            <TextBlock Text="Point spacing (mm) - smaller = more detail, larger = more reliable on complex shapes"
                       Style="{StaticResource SectionLabel}" TextWrapping="Wrap"/>
            <StackPanel Orientation="Horizontal">
                <TextBox x:Name="txt_spacing" Width="100" HorizontalAlignment="Left"/>
                <TextBlock x:Name="txt_recommended"
                           Text="" Foreground="#FF9B9B9B" FontSize="11"
                           VerticalAlignment="Center" Margin="10,0,0,0"/>
                <Button x:Name="btn_use_recommended" Content="Use Recommended"
                        Click="btn_use_recommended_click" Style="{StaticResource SecondaryButton}"
                        Padding="8,3" FontSize="10" Margin="10,0,0,0"/>
            </StackPanel>
        </StackPanel>

        <StackPanel Grid.Row="6">
            <TextBlock x:Name="txt_point_estimate"
                       Text="" Foreground="#FF9B9B9B" FontSize="11" Margin="0,8,0,0"
                       TextWrapping="Wrap"/>
        </StackPanel>

        <StackPanel Grid.Row="7"/>

        <StackPanel Grid.Row="8" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,14,0,0">
            <Button x:Name="btn_cancel" Content="Cancel" Click="btn_cancel_click" Style="{StaticResource SecondaryButton}"/>
            <Button x:Name="btn_ok" Content="Create Floor" Click="btn_ok_click"/>
        </StackPanel>
    </Grid>
    </ScrollViewer>
</Window>
"""


class ToposolidToFloorForm(forms.WPFWindow):
    def __init__(self, xaml_source, levels, floor_types, element, curve_loops,
                 base_z, used_sketch, surface_points, recommended_spacing_mm,
                 selection_summary):
        forms.WPFWindow.__init__(self, xaml_source, literal_string=True)

        self.levels = levels
        self.floor_types = floor_types
        self.result = None
        self._ok_clicked = False

        # The element and its derived geometry are picked and computed BEFORE
        # this dialog is ever shown (see main()) - selection must not happen
        # while this WPF window is open/hidden, since Hide()-then-PickObject
        # has been observed to crash Revit outright.
        self.element = element
        self.curve_loops = curve_loops
        self.base_z = base_z
        self.used_sketch = used_sketch
        self.surface_points = surface_points
        self.recommended_spacing_mm = recommended_spacing_mm

        self.txt_selection_summary.Text = selection_summary

        level_names = [get_name(lvl) for lvl in levels]
        self.cmb_level.ItemsSource = level_names
        last_level = config.get_option(LAST_LEVEL_KEY, "")
        self.cmb_level.SelectedIndex = (
            level_names.index(last_level) if last_level in level_names else 0
        )

        type_names = [ft.FamilyName + " : " + get_name(ft) for ft in floor_types]
        self.cmb_floor_type.ItemsSource = type_names
        last_type = config.get_option(LAST_FLOOR_TYPE_KEY, "")
        self.cmb_floor_type.SelectedIndex = (
            type_names.index(last_type) if last_type in type_names else 0
        )

        self.chk_drape.IsChecked = config.get_option(LAST_DRAPE_KEY, "True") == "True"
        self.txt_spacing.Text = config.get_option(LAST_SPACING_MM_KEY, str(DEFAULT_SPACING_MM))

        # Only overwrite the spacing box with the recommendation if the user
        # hasn't already got a remembered value from a previous run.
        if not config.get_option(LAST_SPACING_MM_KEY, ""):
            self.txt_spacing.Text = str(self.recommended_spacing_mm)
        self.txt_recommended.Text = "Recommended for this shape: {}mm".format(
            self.recommended_spacing_mm
        )

        self._sync_spacing_enabled()
        self._update_point_estimate()

    def btn_use_recommended_click(self, sender, args):
        self.txt_spacing.Text = str(self.recommended_spacing_mm)
        self._update_point_estimate()

    def _sync_spacing_enabled(self):
        self.txt_spacing.IsEnabled = bool(self.chk_drape.IsChecked)

    def _update_point_estimate(self):
        if self.element is None:
            self.txt_point_estimate.Text = ""
            return
        try:
            spacing_mm = float(self.txt_spacing.Text)
        except Exception:
            self.txt_point_estimate.Text = ""
            return
        if not self.chk_drape.IsChecked or spacing_mm <= 0:
            self.txt_point_estimate.Text = "Floor will be created flat (no draping)."
            return
        self.txt_point_estimate.Text = (
            "Sampled {} surface point(s) within the boundary; will thin to "
            "~{}mm spacing (capped at {} interior points).".format(
                len(self.surface_points), int(spacing_mm), MAX_INTERIOR_POINTS
            )
        )

    def chk_drape_toggled(self, sender, args):
        self._sync_spacing_enabled()
        self._update_point_estimate()

    def validate_inputs(self):
        if self.element is None:
            forms.alert("Please select a Toposolid or sub-region first.")
            return None
        if self.cmb_level.SelectedIndex < 0:
            forms.alert("Please select a Level.")
            return None
        if self.cmb_floor_type.SelectedIndex < 0:
            forms.alert("Please select a Floor Type.")
            return None

        drape = bool(self.chk_drape.IsChecked)
        spacing_mm = DEFAULT_SPACING_MM
        if drape:
            try:
                spacing_mm = float(self.txt_spacing.Text)
                if spacing_mm <= 0:
                    raise ValueError()
            except Exception:
                forms.alert("Point spacing must be a positive number (mm).")
                return None

        return {
            "element": self.element,
            "curve_loops": self.curve_loops,
            "base_z": self.base_z,
            "used_sketch": self.used_sketch,
            "surface_points": self.surface_points,
            "level": self.levels[self.cmb_level.SelectedIndex],
            "floor_type": self.floor_types[self.cmb_floor_type.SelectedIndex],
            "drape": drape,
            "spacing_mm": spacing_mm,
        }

    def btn_cancel_click(self, sender, args):
        self._ok_clicked = False
        self.Close()

    def btn_ok_click(self, sender, args):
        validated = self.validate_inputs()
        if validated is None:
            return
        self.result = validated
        self._ok_clicked = True
        self.Close()


def show_options_dialog(element, curve_loops, base_z, used_sketch,
                         surface_points, recommended_spacing_mm,
                         selection_summary):
    levels = sorted(
        FilteredElementCollector(doc).OfClass(Level).ToElements(),
        key=get_name,
    )
    if not levels:
        forms.alert("No Levels found in this project.", exitscript=True)

    floor_types = sorted(
        FilteredElementCollector(doc).OfClass(FloorType).ToElements(),
        key=lambda ft: ft.FamilyName + " : " + get_name(ft),
    )
    if not floor_types:
        forms.alert("No Floor Types found in this project.", exitscript=True)

    dlg = ToposolidToFloorForm(
        FORM_XAML, levels, floor_types, element, curve_loops, base_z,
        used_sketch, surface_points, recommended_spacing_mm, selection_summary,
    )
    dlg.ShowDialog()

    if not dlg._ok_clicked:
        return None

    config.set_option(LAST_LEVEL_KEY, get_name(dlg.result["level"]))
    config.set_option(
        LAST_FLOOR_TYPE_KEY,
        dlg.result["floor_type"].FamilyName + " : " + get_name(dlg.result["floor_type"]),
    )
    config.set_option(LAST_DRAPE_KEY, str(dlg.result["drape"]))
    config.set_option(LAST_SPACING_MM_KEY, str(dlg.result["spacing_mm"]))
    script.save_config()

    return dlg.result


def drape_floor(new_floor, surface_points, base_z, spacing_ft):
    # Enabling shape editing, regenerating, and reading the resulting
    # default corner vertices all have to happen inside the SAME open
    # transaction - the vertices are lazily created on first access, which
    # counts as a document modification just like Enable() itself.
    state = {"vertices": []}

    def do_enable():
        editor = new_floor.SlabShapeEditor
        if not editor.IsEnabled:
            editor.Enable()
        doc.Regenerate()
        state["vertices"] = list(editor.SlabShapeVertices)

    run_isolated(doc, "Enable shape editing", do_enable)

    editor = new_floor.SlabShapeEditor
    existing_vertices = state["vertices"]

    # Snap the floor's own corner vertices to the nearest real surface
    # point, so the boundary itself follows the terrain too. Each is its
    # own transaction, so one bad corner can't block the rest.
    used_points = []
    corners_set = 0
    for vertex in existing_vertices:
        vpos = vertex.Position
        nearest = None
        nearest_dist = None
        for p in surface_points:
            d = ((p.X - vpos.X) ** 2 + (p.Y - vpos.Y) ** 2) ** 0.5
            if nearest_dist is None or d < nearest_dist:
                nearest_dist = d
                nearest = p
        if nearest is None:
            continue

        target = nearest  # captured for the closure below

        def do_modify(vertex=vertex, target=target):
            editor.ModifySubElement(vertex, target.Z - base_z)

        if run_isolated(doc, "Set corner elevation", do_modify):
            corners_set += 1
            used_points.append(nearest)

    # Add interior points for everything else, skipping points that sit
    # right on a corner we already handled.
    interior = []
    for p in surface_points:
        too_close = False
        for u in used_points:
            if p.DistanceTo(u) < 0.05:  # ~15mm
                too_close = True
                break
        if not too_close:
            interior.append(p)

    interior = thin_points_by_grid(interior, spacing_ft)

    if len(interior) > MAX_INTERIOR_POINTS:
        stride = int(round(len(interior) / float(MAX_INTERIOR_POINTS)))
        interior = interior[::max(stride, 1)]

    interior_added = 0
    for p in interior:
        def do_draw(p=p):
            editor.DrawPoint(XYZ(p.X, p.Y, p.Z - base_z))

        if run_isolated(doc, "Add drape point", do_draw):
            interior_added += 1

    return corners_set, interior_added, len(interior) - interior_added


def main():
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            ToposolidSelectionFilter(),
            "Select a Toposolid or a Toposolid sub-region",
        )
    except Exception:
        return  # Escape/right-click cancel

    element = doc.GetElement(ref.ElementId)

    curve_loops, base_z, used_sketch = get_boundary_loops_and_base_z(element)
    if curve_loops is None:
        forms.alert(
            "Couldn't determine a boundary for that element - no sketch "
            "profile and no usable upward-facing face found.",
            exitscript=True,
        )
        return

    raw_points = collect_surface_points(element)
    if not raw_points:
        forms.alert("Couldn't sample any surface points from that element.", exitscript=True)
        return
    if len(raw_points) > 6000:
        raw_points = raw_points[::int(round(len(raw_points) / 6000.0))]
    surface_points = dedupe_points(raw_points)
    surface_points = filter_points_to_boundary(surface_points, curve_loops)
    if not surface_points:
        forms.alert("No sampled surface points fell within that boundary.", exitscript=True)
        return

    recommended_spacing_mm = recommend_spacing_mm(curve_loops)

    try:
        cat_name = element.Category.Name
    except Exception:
        cat_name = "element"
    selection_summary = "Selected: {} (id {})".format(cat_name, element.Id)

    options = show_options_dialog(
        element, curve_loops, base_z, used_sketch, surface_points,
        recommended_spacing_mm, selection_summary,
    )
    if options is None:
        return  # user cancelled the dialog

    curve_loops = options["curve_loops"]
    base_z = options["base_z"]
    used_sketch = options["used_sketch"]
    surface_points = options["surface_points"]
    level = options["level"]
    floor_type = options["floor_type"]
    drape = options["drape"]
    spacing_ft = options["spacing_mm"] / MM_PER_FOOT

    loops_net = List[CurveLoop](curve_loops)

    # The toposolid's own sketch often has many non-axis-aligned segments,
    # which Revit flags as "Line in Sketch is slightly off axis" warnings -
    # harmless, but there can be dozens of them, so this goes through the
    # same warning-suppressing transaction helper as the drape points.
    state = {"floor": None}

    def do_create():
        state["floor"] = Floor.Create(doc, loops_net, floor_type.Id, level.Id)

    if not run_isolated(doc, "Create Floor", do_create):
        forms.alert("Failed to create floor.", exitscript=True)
        return

    new_floor = state["floor"]
    if new_floor is None:
        return

    if not drape:
        output.print_md(
            "**Floor created** (Id `{}`) on level **{}** using type **{} : {}**. "
            "Boundary source: **{}**. Created flat (draping skipped).".format(
                new_floor.Id,
                get_name(level),
                floor_type.FamilyName,
                get_name(floor_type),
                "sketch profile" if used_sketch else "derived from top face",
            )
        )
        return

    try:
        corners_set, interior_added, interior_skipped = drape_floor(
            new_floor, surface_points, base_z, spacing_ft
        )
    except Exception as ex:
        forms.alert(
            "Floor was created (Id `{}`) but draping failed:\n{}".format(
                new_floor.Id, ex
            ),
            exitscript=True,
        )
        return

    output.print_md(
        "**Floor created** (Id `{}`) on level **{}** using type **{} : {}**. "
        "Boundary source: **{}**. Point spacing: **{:.0f}mm**. Draped {} "
        "corner point(s) and {} of {} interior point(s) ({} skipped as "
        "invalid geometry) from {} sampled surface points.".format(
            new_floor.Id,
            get_name(level),
            floor_type.FamilyName,
            get_name(floor_type),
            "sketch profile" if used_sketch else "derived from top face",
            options["spacing_mm"],
            corners_set,
            interior_added,
            interior_added + interior_skipped,
            interior_skipped,
            len(surface_points),
        )
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        # Raised by script.exit()/forms.alert(exitscript=True) on a
        # cancelled pick or dismissed dropdown - not an actual error.
        pass
    except Exception:
        # Surface the real error in pyRevit's own output window instead
        # of Revit's generic "Command Failure" dialog with no details.
        output.print_md("### Toposolid to Floor - unexpected error")
        output.print_code(traceback.format_exc())
