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

# Minimum spacing (feet) enforced between interior drape points via grid
# thinning. Tight bends/branches in a complex footprint get densely
# triangulated by Revit, and that local density is what tends to produce
# degenerate/near-zero-area shape-edit triangles. ~1 ft (~300mm) spacing
# keeps the drape's accuracy while cutting that risk down a lot.
MIN_POINT_SPACING = 1.0

# Hard cap as a final safety net after grid thinning.
MAX_INTERIOR_POINTS = 400


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


def pick_level():
    levels = list(FilteredElementCollector(doc).OfClass(Level))
    if not levels:
        forms.alert("No Levels found in this project.", exitscript=True)

    name_to_level = {get_name(lvl): lvl for lvl in levels}
    chosen = forms.SelectFromList.show(
        sorted(name_to_level.keys()),
        title="Select Level",
        button_name="Next",
    )
    if not chosen:
        script.exit()
    return name_to_level[chosen]


def pick_floor_type():
    floor_types = list(FilteredElementCollector(doc).OfClass(FloorType))
    if not floor_types:
        forms.alert("No Floor Types found in this project.", exitscript=True)

    name_to_type = {
        ft.FamilyName + " : " + get_name(ft): ft for ft in floor_types
    }
    chosen = forms.SelectFromList.show(
        sorted(name_to_type.keys()),
        title="Select Floor Type",
        button_name="Create Floor",
    )
    if not chosen:
        script.exit()
    return name_to_type[chosen]


def drape_floor(new_floor, surface_points, base_z):
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

    interior = thin_points_by_grid(interior, MIN_POINT_SPACING)

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
        # Covers Escape/right-click cancel during PickObject.
        return

    element = doc.GetElement(ref.ElementId)

    curve_loops, base_z, used_sketch = get_boundary_loops_and_base_z(element)
    if curve_loops is None:
        forms.alert(
            "Couldn't determine a boundary for that element - no sketch "
            "profile and no usable upward-facing face found.",
            exitscript=True,
        )

    raw_points = collect_surface_points(element)
    if not raw_points:
        forms.alert("Couldn't sample any surface points from that element.", exitscript=True)

    if len(raw_points) > 6000:
        raw_points = raw_points[::int(round(len(raw_points) / 6000.0))]
    surface_points = dedupe_points(raw_points)
    surface_points = filter_points_to_boundary(surface_points, curve_loops)
    if not surface_points:
        forms.alert(
            "No sampled surface points fell within the boundary - the "
            "floor was not draped.",
            exitscript=True,
        )

    level = pick_level()
    floor_type = pick_floor_type()

    loops_net = List[CurveLoop](curve_loops)

    t = Transaction(doc, "Create Floor")
    t.Start()
    try:
        new_floor = Floor.Create(doc, loops_net, floor_type.Id, level.Id)
        t.Commit()
    except Exception as ex:
        t.RollBack()
        forms.alert("Failed to create floor:\n{}".format(ex), exitscript=True)
        return

    try:
        corners_set, interior_added, interior_skipped = drape_floor(
            new_floor, surface_points, base_z
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
        "Boundary source: **{}**. Draped {} corner point(s) and {} of {} "
        "interior point(s) ({} skipped as invalid geometry) from {} "
        "sampled surface points.".format(
            new_floor.Id,
            get_name(level),
            floor_type.FamilyName,
            get_name(floor_type),
            "sketch profile" if used_sketch else "derived from top face",
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
