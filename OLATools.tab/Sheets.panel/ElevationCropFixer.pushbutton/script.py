# -*- coding: utf-8 -*-
"""
Elevation Crop Fixer
---------------------
Batch-fixes interior elevation crop regions for a chosen set of rooms:

  * Extends the crop's left/right edges past the FAR face of each side
    wall (found geometrically - never trusts Revit's wall
    interior/exterior flag, since that can be backwards on interior
    partitions) by a user-set offset (default 75mm).
  * Extends the crop's top edge above the room's topmost ceiling by a
    user-set offset (default 100mm).
  * Optionally hides the right-hand bubble on any Level visible in the
    view, and extends that level's right end a user-set distance
    (default 100mm) past the new crop edge.
  * Optionally extends any Grid's top and bottom ends the same distance
    past the new top/bottom crop edges.
  * Skips (and reports) any elevation whose crop has already been
    manually resized, unless the user unchecks that option.

FIRST DRAFT - this has not been run inside Revit yet. The geometry and
DatumPlane API calls are believed correct for Revit 2022+ but should be
tested on a non-critical model first. See README.md for known
limitations / assumptions.

Author: Phil May (with Claude)
"""
__title__ = "Elevation\nCrop Fixer"
__author__ = "Phil May"
__doc__ = "Batch-fix interior elevation crop regions for selected rooms."

import os
import re
import System
import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    FilteredElementCollector,
    Wall,
    XYZ,
    UV,
    Line,
    Transaction,
    TransactionGroup,
    SpatialElementBoundaryOptions,
    DatumEnds,
    DatumExtentType,
    UnitUtils,
    BoundingBoxXYZ,
    Options,
    Solid,
    GeometryInstance,
    ElementId,
    ElevationMarker,
    ViewPlan,
    View,
    ViewType,
    FamilySymbol,
    Level,
    Category,
)

try:
    from Autodesk.Revit.DB import PlanViewPlane
except ImportError:
    PlanViewPlane = None

try:
    from Autodesk.Revit.DB import UnitTypeId

    def mm_to_ft(mm):
        return UnitUtils.ConvertToInternalUnits(mm, UnitTypeId.Millimeters)
except ImportError:
    from Autodesk.Revit.DB import DisplayUnitType

    def mm_to_ft(mm):
        return UnitUtils.ConvertToInternalUnits(mm, DisplayUnitType.DUT_MILLIMETERS)

try:
    from Autodesk.Revit.DB import ViewDetailLevel
except ImportError:
    ViewDetailLevel = None

from Autodesk.Revit.DB.ExtensibleStorage import (
    Schema,
    SchemaBuilder,
    Entity,
    AccessLevel,
)

from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
logger = script.get_logger()

XAML_PATH = os.path.join(os.path.dirname(__file__), "ui.xaml")

TOOL_VERSION = "1.6.8"

# ---------------------------------------------------------------------------
# Extensible storage - marks a view we have adjusted, so a later run can
# tell "adjusted by this tool, still matches" from "someone changed it
# since". This is what backs the "skip already-modified" option.
# ---------------------------------------------------------------------------

SCHEMA_GUID = System.Guid("6E7B2B3B-6C1E-4E9E-9C2A-2C9D8F2C7A12")  # v2: adds MinY
# (bumped from ...A11 -> ...A12 because this now also stores the crop's
# bottom edge, which v1 never touched; a document that already has ...A11
# entities from earlier runs just looks "never touched" under the new
# schema and gets freshly reprocessed once - harmless.)


def _set_length_spec(field):
    """Extensible Storage requires a unit/spec on any Double field that
    represents a physical quantity - our values are in feet (length)."""
    try:
        from Autodesk.Revit.DB import SpecTypeId
        field.SetSpec(SpecTypeId.Length)
    except ImportError:
        from Autodesk.Revit.DB import UnitType
        field.SetUnitType(UnitType.UT_Length)


def get_or_create_schema():
    existing = Schema.Lookup(SCHEMA_GUID)
    if existing is not None:
        return existing
    builder = SchemaBuilder(SCHEMA_GUID)
    builder.SetSchemaName("ElevationCropFixerMarker")
    builder.SetReadAccessLevel(AccessLevel.Public)
    builder.SetWriteAccessLevel(AccessLevel.Public)
    _set_length_spec(builder.AddSimpleField("MinX", System.Double))
    _set_length_spec(builder.AddSimpleField("MaxX", System.Double))
    _set_length_spec(builder.AddSimpleField("MinY", System.Double))
    _set_length_spec(builder.AddSimpleField("MaxY", System.Double))
    return builder.Finish()


def read_marker(view):
    schema = Schema.Lookup(SCHEMA_GUID)
    if schema is None:
        return None
    entity = view.GetEntity(schema)
    if entity is None or not entity.IsValid():
        return None
    try:
        return (
            entity.Get[System.Double]("MinX", UnitTypeId.Feet),
            entity.Get[System.Double]("MaxX", UnitTypeId.Feet),
            entity.Get[System.Double]("MaxY", UnitTypeId.Feet),
            entity.Get[System.Double]("MinY", UnitTypeId.Feet),
        )
    except Exception:
        return None


def write_marker(view, min_x, max_x, max_y, min_y):
    schema = get_or_create_schema()
    entity = Entity(schema)
    entity.Set[System.Double]("MinX", min_x, UnitTypeId.Feet)
    entity.Set[System.Double]("MaxX", max_x, UnitTypeId.Feet)
    entity.Set[System.Double]("MaxY", max_y, UnitTypeId.Feet)
    entity.Set[System.Double]("MinY", min_y, UnitTypeId.Feet)
    view.SetEntity(entity)


TOL = 0.01  # feet, ~3mm - tolerance when comparing stored vs current crop


def crop_matches_marker(view, marker):
    crop = view.CropBox
    return (
        abs(crop.Min.X - marker[0]) < TOL
        and abs(crop.Max.X - marker[1]) < TOL
        and abs(crop.Max.Y - marker[2]) < TOL
        and abs(crop.Min.Y - marker[3]) < TOL
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def get_room_center(room):
    loc = room.Location
    if loc is not None and hasattr(loc, "Point"):
        return loc.Point
    bb = room.get_BoundingBox(None)
    return (bb.Min + bb.Max) / 2.0


def get_boundary_walls(room):
    """All (wall, room-clipped boundary curve) pairs that bound this
    room, deduped by wall id. The curve is the portion of the wall's
    boundary specific to THIS room - important for a wall that spans
    past this room (a party wall shared with other rooms, or a long
    perimeter wall), whose own full Location curve would give a
    midpoint far outside this room."""
    opts = SpatialElementBoundaryOptions()
    walls = {}
    loops = room.GetBoundarySegments(opts)
    if not loops:
        return []
    for loop in loops:
        for seg in loop:
            el = doc.GetElement(seg.ElementId)
            if isinstance(el, Wall):
                curve = None
                try:
                    curve = seg.GetCurve()
                except Exception:
                    curve = None
                if el.Id.IntegerValue not in walls:
                    walls[el.Id.IntegerValue] = (el, curve)
    return list(walls.values())


def _get_wall_side_faces(wall, axis, room_center):
    """All (position, distance-from-room-center) pairs, along `axis`,
    for this wall's faces that are roughly (anti)parallel to `axis`
    (i.e. its two long side faces, not the end caps or top/bottom)."""
    opt = Options()
    opt.ComputeReferences = False
    opt.IncludeNonVisibleObjects = False
    if ViewDetailLevel is not None:
        opt.DetailLevel = ViewDetailLevel.Fine

    geom = wall.get_Geometry(opt)
    if geom is None:
        return []

    solids = []
    for obj in geom:
        if isinstance(obj, Solid) and obj.Faces.Size > 0:
            solids.append(obj)
        elif isinstance(obj, GeometryInstance):
            try:
                inst_geom = obj.GetInstanceGeometry()
            except Exception:
                inst_geom = None
            if inst_geom is not None:
                for o2 in inst_geom:
                    if isinstance(o2, Solid) and o2.Faces.Size > 0:
                        solids.append(o2)

    near_val = axis.DotProduct(room_center)
    results = []
    for solid in solids:
        for face in solid.Faces:
            try:
                bbox = face.GetBoundingBox()
                uv_mid = UV(
                    (bbox.Min.U + bbox.Max.U) / 2.0, (bbox.Min.V + bbox.Max.V) / 2.0
                )
                normal = face.ComputeNormal(uv_mid)
                pt = face.Evaluate(uv_mid)
            except Exception:
                continue
            align = abs(normal.DotProduct(axis))
            if align < 0.95:
                continue  # not (anti)parallel to axis - not a side face
            val = axis.DotProduct(pt)
            results.append((val, abs(val - near_val)))
    return results


def get_analytic_face_positions(wall, axis, room_center):
    """
    Compute a straight wall's two long side-face positions directly
    from its centerline (Location.Curve) and Width, WITHOUT looking at
    solid geometry at all. This sidesteps geometry artifacts that can
    fool a solid-face scan into picking the wrong face: small "join"
    cap faces Revit adds where this wall meets another wall at a
    corner, return faces at a notch, or a face interrupted by an
    opening - any of which can occasionally end up with a sampled
    midpoint that looks like a valid, and even farther, side face.

    Returns (val_near, val_far) along axis (near/far from room_center),
    or None if this isn't a simple straight wall we can compute this
    for (curved walls, or anything without a normal Location.Curve).
    """
    try:
        loc = wall.Location
        curve = loc.Curve if loc is not None and hasattr(loc, "Curve") else None
        width = wall.Width
    except Exception:
        return None
    if curve is None or not isinstance(curve, Line):
        return None
    if width is None or width <= 0:
        return None

    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    tangent = p1 - p0
    if tangent.GetLength() < 1e-6:
        return None
    tangent = tangent.Normalize()
    perp = XYZ(-tangent.Y, tangent.X, 0.0)
    if perp.GetLength() < 1e-6:
        return None
    perp = perp.Normalize()
    mid = (p0 + p1) / 2.0
    face_a = mid + perp.Multiply(width / 2.0)
    face_b = mid - perp.Multiply(width / 2.0)
    val_a = axis.DotProduct(face_a)
    val_b = axis.DotProduct(face_b)

    near_val = axis.DotProduct(room_center)
    if abs(val_a - near_val) >= abs(val_b - near_val):
        return val_b, val_a
    return val_a, val_b


def get_face_method_diag(wall):
    """Troubleshooting only: explain, in one short phrase, whether
    get_far_face_position will use the analytic centerline+width method
    for this wall or fall back to scanning solid geometry - and if it
    falls back, why the analytic method couldn't be used."""
    try:
        wtype = type(wall).__name__
    except Exception:
        wtype = "?"
    try:
        loc = wall.Location
        curve = loc.Curve if loc is not None and hasattr(loc, "Curve") else None
    except Exception as ex:
        return "analytic: FAILED - Location error: {} ({})".format(ex, wtype)
    if curve is None:
        return "analytic: FAILED - no Location.Curve ({})".format(wtype)
    if not isinstance(curve, Line):
        return "analytic: FAILED - Location.Curve is {} not Line ({})".format(type(curve).__name__, wtype)
    try:
        width = wall.Width
    except Exception as ex:
        return "analytic: FAILED - Width error: {} ({})".format(ex, wtype)
    if width is None or width <= 0:
        return "analytic: FAILED - Width={} ({})".format(width, wtype)
    return "analytic: OK, width={:.3f}ft ({})".format(width, wtype)


def get_far_face_position(wall, axis, room_center):
    """
    Find the wall face most nearly parallel to `axis` that sits FARTHEST
    from room_center along axis - this deliberately ignores Wall.Flipped
    / interior-exterior side, since that can be backwards on interior
    partitions.

    Tries the analytic centerline+width method first (robust against
    join/return/opening artifacts); only falls back to scanning solid
    geometry for faces if that's not available (e.g. a curved wall).

    Returns the signed scalar position (dot(point, axis)) of that face,
    or None if no suitable face was found.
    """
    analytic = get_analytic_face_positions(wall, axis, room_center)
    if analytic is not None:
        return analytic[1]

    best_val = None
    best_dist = -1.0
    for val, dist in _get_wall_side_faces(wall, axis, room_center):
        if dist > best_dist:
            best_dist = dist
            best_val = val
    return best_val


def get_boundary_loops(room):
    """Room boundary segments as ordered loops of (wall_or_None, curve)
    pairs, preserving adjacency - needed to find the walls neighboring
    a specific segment (used by the loop-adjacency side-wall method,
    which handles L-shaped rooms correctly where a simple
    nearest-wall-by-distance pick can grab the wrong one)."""
    opts = SpatialElementBoundaryOptions()
    loops = room.GetBoundarySegments(opts)
    result = []
    if not loops:
        return result
    for loop in loops:
        entries = []
        for seg in loop:
            el = doc.GetElement(seg.ElementId)
            wall = el if isinstance(el, Wall) else None
            try:
                curve = seg.GetCurve()
            except Exception:
                curve = None
            entries.append((wall, curve))
        result.append(entries)
    return result


def find_side_walls_by_scan(room, scan_origin, right_dir):
    """
    Find the LEFT and RIGHT side walls with a straight scan line
    through `scan_origin`, running along `right_dir` (the view's
    left-right axis) - i.e. find where that line first crosses the
    room's boundary wall on each side. Those crossings are, by
    construction, the two walls immediately flanking the sightline,
    regardless of how many corners the room's boundary has - so this
    works correctly on an L-shaped room without needing to reason
    about loop adjacency at all.

    `scan_origin` should be the specific ELEVATION MARKER's own plan
    location, not just "the room's centre" - once a room has more than
    one notch/wing, its width at the centroid's row can differ from
    its width at a particular marker's own row, so scanning through
    the centroid can cross the wrong walls for a marker that isn't
    near it. (The caller falls back to the room centre only if the
    marker's location couldn't be resolved.)

    Crucially, this ALSO doesn't depend on the view's existing crop
    box in any way - unlike matching an existing crop edge to a wall
    face, which silently assumes the crop was already sitting in the
    right place. That assumption breaks whenever an elevation marker
    was copied/pasted from a different room (a normal workflow here to
    speed up placing markers): a copied marker's crop reflects the
    ROOM IT WAS COPIED FROM, not the one it now sits in, so there is no
    "original correct edge" to trust. A pure room-geometry scan sidesteps
    that entirely.

    Returns (left_wall, right_wall, note, diag).
    """
    loops = get_boundary_loops(room)
    if not loops:
        return None, None, "no boundary loops", ""

    Ox, Oy = scan_origin.X, scan_origin.Y
    Dx, Dy = right_dir.X, right_dir.Y

    left_best = None  # (wall, s, t, A, B) - s is signed offset along right_dir, s < 0
    right_best = None  # s >= 0

    for loop in loops:
        for wall, curve in loop:
            if wall is None or curve is None:
                continue
            A = curve.GetEndPoint(0)
            B = curve.GetEndPoint(1)
            Ex = B.X - A.X
            Ey = B.Y - A.Y
            denom = Ex * Dy - Ey * Dx
            if abs(denom) < 1e-9:
                continue  # this wall runs parallel to the scan line - no clean crossing
            diffX = A.X - Ox
            diffY = A.Y - Oy
            s = (Ex * diffY - Ey * diffX) / denom  # offset along right_dir, from room_center
            t = (Dx * diffY - Dy * diffX) / denom  # 0..1 = within the wall segment
            if t < -0.001 or t > 1.001:
                continue  # the scan line passes this wall's line, but outside the segment itself
            if s < 0:
                if left_best is None or s > left_best[1]:
                    left_best = (wall, s, t, A, B)
            else:
                if right_best is None or s < right_best[1]:
                    right_best = (wall, s, t, A, B)

    left_wall = left_best[0] if left_best else None
    right_wall = right_best[0] if right_best else None

    def seg_diag(best):
        if best is None:
            return "none"
        wall, s, t, A, B = best
        return "#{} s={:.2f}ft t={:.3f} seg=({:.2f},{:.2f})->({:.2f},{:.2f})".format(
            wall.Id, s, t, A.X, A.Y, B.X, B.Y,
        )

    diag = "scan through ({:.2f},{:.2f}): left={} | right={}".format(
        Ox, Oy, seg_diag(left_best), seg_diag(right_best),
    )

    if left_wall is None or right_wall is None:
        return None, None, "scan line didn't cross a wall on both sides - {}".format(diag), diag
    return left_wall, right_wall, None, diag


def find_side_walls_via_loop(view, room, room_center):
    """
    Find the LEFT and RIGHT side walls using the room's own boundary
    segments (its true geometric edges - including shared/party walls
    with neighbouring rooms, since those are still part of THIS room's
    boundary loop):

      1. Locate the 'back wall' - the boundary segment running
         parallel to the crop's right axis, on the far side of the
         room in the camera's looking direction (largest depth along
         view_dir from the room centre, among segments aligned with
         right_dir).
      2. Scan every OTHER boundary segment of the room (any loop) that
         is genuinely side-aligned (runs more along view_dir than
         along right_dir) and whose depth doesn't run past the back
         wall (depth <= back_depth, with a little slack) - i.e. a
         segment that could plausibly flank the sightline from the
         viewer up to the back wall, rather than a wall belonging to
         some other, unrelated part of the room or a neighbour beyond
         the back wall.
      3. Among those, pick the one nearest the room's centreline on
         each side (smallest |x_off|) - the wall immediately flanking
         the view, not the outermost thing that happens to qualify.

    This replaced an earlier 'walk to the immediate loop neighbour'
    approach, which broke on an L-shaped room: at a concave corner
    (where this room notches around a neighbour) there can be several
    corners between the back wall and the room's true side wall, and
    simply stepping to the nearest side-aligned segment could land on
    a short connector or even a neighbouring room's own wall instead.
    Restricting candidates by depth (can't run past the back wall) and
    picking the nearest one by offset (not the first one encountered)
    is robust to how many corners lie in between.

    Returns (left_wall, right_wall, note, diag) - note is None on
    success, or a short reason string if this approach didn't find a
    clean answer (caller should fall back to the distance-based
    method). diag is always a short human-readable string describing
    which wall was treated as the back wall and which as each side,
    for troubleshooting - included in the report even on success.
    """
    view_dir = view.ViewDirection.Normalize()
    right_dir = view.RightDirection.Normalize()

    loops = get_boundary_loops(room)
    if not loops:
        return None, None, "no boundary loops", ""

    all_segments = []  # (wall, curve) flattened across every loop
    best = None  # (seg_index_into_all_segments, depth) - the back wall
    for loop in loops:
        for wall, curve in loop:
            if wall is None or curve is None:
                continue
            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            d = p1 - p0
            if d.GetLength() < 1e-6:
                continue
            d = d.Normalize()
            idx = len(all_segments)
            all_segments.append((wall, curve))
            align = abs(d.DotProduct(right_dir))
            if align < 0.85:
                continue  # not roughly perpendicular to the view direction
            mid = (p0 + p1) / 2.0
            depth = view_dir.DotProduct(mid - room_center)
            if depth <= 0:
                continue  # behind the camera - not the wall being looked at
            if best is None or depth > best[1]:
                best = (idx, depth)

    if best is None:
        return None, None, "no back wall found via loop", ""

    back_idx, back_depth = best
    back_wall, back_curve = all_segments[back_idx]
    BACK_DEPTH_SLACK_FT = 0.5  # ~150mm - allow a side wall's midpoint to sit
    # very slightly past the back wall's own depth (corner overlap) without
    # disqualifying it, but nothing further than that.

    def mid_of(curve):
        return (curve.GetEndPoint(0) + curve.GetEndPoint(1)) / 2.0

    left_best = None  # (wall, curve, abs_x_off)
    right_best = None
    for i, (wall, curve) in enumerate(all_segments):
        if i == back_idx:
            continue
        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        d = (p1 - p0)
        if d.GetLength() < 1e-6:
            continue
        d = d.Normalize()
        side_align = abs(d.DotProduct(view_dir))
        back_align = abs(d.DotProduct(right_dir))
        if side_align <= back_align:
            continue  # this segment runs across the view, not along it
        mid = (p0 + p1) / 2.0
        depth = view_dir.DotProduct(mid - room_center)
        if depth > back_depth + BACK_DEPTH_SLACK_FT:
            continue  # runs past the back wall - not part of this bay
        x_off = right_dir.DotProduct(mid - room_center)
        if x_off < 0:
            if left_best is None or abs(x_off) < left_best[2]:
                left_best = (wall, curve, abs(x_off))
        else:
            if right_best is None or abs(x_off) < right_best[2]:
                right_best = (wall, curve, abs(x_off))

    left_wall = left_best[0] if left_best else None
    right_wall = right_best[0] if right_best else None

    def diag_str():
        return (
            "back wall #{} (depth {:.2f}); picked left=#{} right=#{}"
        ).format(
            back_wall.Id if back_wall else "?", back_depth,
            left_wall.Id if left_wall else "none",
            right_wall.Id if right_wall else "none",
        )

    if left_wall is None or right_wall is None:
        return None, None, "no side wall found within the back wall's depth band", diag_str()
    return left_wall, right_wall, None, diag_str()


def classify_side_walls(view, boundary_walls, crop_transform, room_center):
    """
    From the room's boundary walls, pick the ones bounding the LEFT and
    RIGHT edges of this elevation crop. A side wall is one whose
    location line runs roughly parallel to the view direction (i.e. it
    runs "into" the view, away from the viewer) rather than parallel to
    the view's right axis (which would make it the back/viewed wall).

    Left vs right is decided relative to the ROOM'S OWN CENTER, not the
    crop box's transform origin - a freshly-created elevation view's
    CropBox can apparently sit off-center (only the first of a
    marker's 4 views seems reliably centered until Revit re-fits it),
    which otherwise put both side walls on the same side of "origin".
    The room center is always safely between the two walls.

    Returns (left_wall, right_wall, warning) - warning is None on a
    clean single match per side, otherwise a short reason string and
    the walls picked are the closest match (caller should treat as
    "flagged for review" when warning is set).
    """
    view_dir = view.ViewDirection.Normalize()
    right_dir = crop_transform.BasisX.Normalize()
    origin = room_center

    candidates = []
    examined = []  # diagnostics: (wall_id, align, x_off_or_None, note)
    for wall, seg_curve in boundary_walls:
        curve = seg_curve
        if curve is None:
            # fall back to the wall's own (possibly un-clipped) location
            # curve if we couldn't get the room-specific boundary curve
            loc = wall.Location
            curve = loc.Curve if loc is not None and hasattr(loc, "Curve") else None
        if curve is None:
            examined.append((wall.Id, None, None, "no curve"))
            continue
        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        d = p1 - p0
        if d.GetLength() < 1e-6:
            examined.append((wall.Id, None, None, "zero-length curve"))
            continue
        d = d.Normalize()
        align = abs(d.DotProduct(view_dir))
        mid = (p0 + p1) / 2.0
        x_off = right_dir.DotProduct(mid - origin)
        if align < 0.85:
            examined.append((wall.Id, align, x_off, "below align threshold"))
            continue
        candidates.append((wall, x_off))
        examined.append((wall.Id, align, x_off, "candidate"))

    def fmt_examined():
        parts = []
        for wid, align, x_off, note in examined:
            a_str = "{:.2f}".format(align) if align is not None else "-"
            x_str = "{:.2f}".format(x_off) if x_off is not None else "-"
            parts.append("#{} align={} x_off={} ({})".format(wid, a_str, x_str, note))
        vd = "({:.2f},{:.2f},{:.2f})".format(view_dir.X, view_dir.Y, view_dir.Z)
        rd = "({:.2f},{:.2f},{:.2f})".format(right_dir.X, right_dir.Y, right_dir.Z)
        return "view_dir={} right_dir={} walls: {}".format(vd, rd, "; ".join(parts))

    if not candidates:
        return None, None, "no side walls found - {}".format(fmt_examined())

    left_candidates = sorted([c for c in candidates if c[1] < 0], key=lambda c: -c[1])
    right_candidates = sorted([c for c in candidates if c[1] >= 0], key=lambda c: c[1])

    warnings = []

    def ambiguous(cands):
        return len(cands) >= 2 and abs(cands[0][1] - cands[1][1]) < 0.5  # ~150mm

    if not left_candidates:
        warnings.append("no left-side wall found")
    elif ambiguous(left_candidates):
        warnings.append("multiple walls on the left (possible L-shaped room)")

    if not right_candidates:
        warnings.append("no right-side wall found")
    elif ambiguous(right_candidates):
        warnings.append("multiple walls on the right (possible L-shaped room)")

    left_wall = left_candidates[0][0] if left_candidates else None
    right_wall = right_candidates[0][0] if right_candidates else None

    if warnings:
        return left_wall, right_wall, "; ".join(warnings) + " - " + fmt_examined()
    return left_wall, right_wall, None


_LEVELS_CACHE = None  # sorted list of (elevation, Level) tuples


def _get_levels_cache():
    global _LEVELS_CACHE
    if _LEVELS_CACHE is None:
        levels = FilteredElementCollector(doc).OfClass(Level).ToElements()
        _LEVELS_CACHE = sorted((lvl.Elevation, lvl) for lvl in levels)
    return _LEVELS_CACHE


def _get_next_level_above(z):
    """The Level element immediately above `z` in the whole project (by
    elevation), or None if `z` is at/above the topmost level."""
    above = [(e, lvl) for e, lvl in _get_levels_cache() if e > z + 0.1]
    return min(above)[1] if above else None


def _get_element_level_id(elem):
    """The Level this element is hosted to, how ever the category
    happens to expose it (a direct LevelId property on most host
    objects; an instance parameter on some families) - or
    ElementId.InvalidElementId if neither resolves."""
    try:
        lid = elem.LevelId
        if lid is not None and lid != ElementId.InvalidElementId:
            return lid
    except Exception:
        pass
    try:
        p = elem.get_Parameter(BuiltInParameter.LEVEL_PARAM)
        if p is not None:
            return p.AsElementId()
    except Exception:
        pass
    return ElementId.InvalidElementId


MAX_ROOM_HEIGHT_FT = 26.0  # ~8m - generous fallback cap only used when there
# is no level above this room's own (e.g. the top floor / roof), so a
# ceiling search still can't run away indefinitely upward.


_CEILINGS_CACHE = None  # all OST_Ceilings elements in the doc, collected once per session
_CEILING_TOP_Z_CACHE = {}  # room.Id.IntegerValue -> (top_z, diag) - a view's own 4 elevations
                            # were each re-collecting + re-testing every ceiling in the model
                            # for the SAME room; that's identical work every time, so memoize it.


def _get_ceilings_cache():
    global _CEILINGS_CACHE
    if _CEILINGS_CACHE is None:
        _CEILINGS_CACHE = (
            FilteredElementCollector(doc)
            .OfCategory(BuiltInCategory.OST_Ceilings)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    return _CEILINGS_CACHE


def get_room_top_ceiling_z(room):
    """World Z of the top face of the highest ceiling associated with
    this room, or None if none is found. Also returns a diagnostic
    string explaining exactly what was considered and why, so a wrong
    pick can be read straight off the results table instead of guessed
    at blind.

    Primary method: among ceilings that geometrically overlap this room
    in plan (bbox overlap + the room's own IsPointInRoom test), trust the
    ceiling's OWN hosting Level directly - it counts only if that Level
    is the room's own Level or the next Level up (the common "ceiling
    hosted to the underside of the slab above" convention). This is the
    most direct, Revit-native way to tie a ceiling to a floor and isn't
    fooled by a project with more than one interleaved level set (e.g. a
    building-within-a-building with its own separate level stack sharing
    similar elevations) the way a pure Z-height guess can be.

    Falls back to the previous Z-band heuristic (bounded to the gap
    between this room's own floor and the next level up, or a generous
    cap on the topmost floor) only if nothing passes the Level check -
    e.g. if a ceiling has no resolvable LevelId at all.

    Result is cached per room (a room's 4+ elevation views all ask this
    same question and got an identical answer every time - no reason to
    redo it once per view).

    Returns (best_z, diag).
    """
    cache_key = room.Id.IntegerValue
    if cache_key in _CEILING_TOP_Z_CACHE:
        return _CEILING_TOP_Z_CACHE[cache_key]

    room_bbox = room.get_BoundingBox(None)
    if room_bbox is None:
        result = None, "CEILING: room has no bounding box"
        _CEILING_TOP_Z_CACHE[cache_key] = result
        return result

    room_level_id = getattr(room, "LevelId", ElementId.InvalidElementId)
    next_level = _get_next_level_above(room_bbox.Min.Z)
    next_level_id = next_level.Id if next_level is not None else None
    allowed_level_ids = set(i for i in (room_level_id, next_level_id) if i is not None)

    if next_level is not None:
        max_allowed_z = next_level.Elevation
        cap_note = "next level above @ {:.2f}ft".format(next_level.Elevation)
    else:
        max_allowed_z = room_bbox.Min.Z + MAX_ROOM_HEIGHT_FT
        cap_note = "no level found above room base - fallback cap {:.1f}ft above room base".format(
            MAX_ROOM_HEIGHT_FT
        )
    min_allowed_z = room_bbox.Min.Z - 1.0  # a little slack below the room's own base

    ceilings = _get_ceilings_cache()

    own_level_matched = []  # (top_z, ceiling_id) - hosted on THIS room's own Level
    next_level_matched = []  # (top_z, ceiling_id) - hosted on the next Level up
    height_banded = []  # (top_z, ceiling_id) - fallback, Z-window guess only
    in_plan_count = 0
    clamped_ids = []  # ceilings whose raw Max.Z exceeded max_allowed_z and got capped
    for c in ceilings:
        c_bbox = c.get_BoundingBox(None)
        if c_bbox is None:
            continue
        if c_bbox.Max.X < room_bbox.Min.X or c_bbox.Min.X > room_bbox.Max.X:
            continue
        if c_bbox.Max.Y < room_bbox.Min.Y or c_bbox.Min.Y > room_bbox.Max.Y:
            continue
        cx = (c_bbox.Min.X + c_bbox.Max.X) / 2.0
        cy = (c_bbox.Min.Y + c_bbox.Max.Y) / 2.0
        test_pt = XYZ(cx, cy, room_bbox.Min.Z + 0.1)
        try:
            if not room.IsPointInRoom(test_pt):
                continue
        except Exception:
            pass  # fall back to bbox-overlap only if API unavailable
        in_plan_count += 1

        ceiling_level_id = _get_element_level_id(c)
        if ceiling_level_id in allowed_level_ids:
            # (v1.6.4 fix) This used to store c_bbox.Max.Z uncapped, on the
            # assumption that a Level match alone was proof enough this
            # ceiling belongs to this room. It isn't: a ceiling genuinely
            # hosted on the next level up (the room ABOVE's own ceiling,
            # still present even after that room was deleted, since
            # ceilings aren't tied to Room elements) can have a bulkhead,
            # offset, or coffer that puts its real top well past
            # max_allowed_z - past the next level up entirely. The
            # height-banded fallback below already clamped to
            # max_allowed_z; a Level match was wrongly exempt from that
            # same clamp, letting a tall ceiling from the room above leak
            # into this room's own crop top. Clamp here too.
            if c_bbox.Max.Z > max_allowed_z:
                clamped_ids.append((c.Id, c_bbox.Max.Z))
            capped_top_z = min(c_bbox.Max.Z, max_allowed_z)
            # (v1.6.5 fix) Clamping alone wasn't enough: when a room has
            # BOTH its own real ceiling (hosted on its own Level) AND a
            # same-footprint ceiling belonging to the room above (hosted
            # on the next Level up, now clamped to that level's
            # elevation), picking max() of the two always preferred the
            # next-level one, since a clamped "next level up" ceiling
            # sits exactly AT that level's elevation while this room's own
            # ceiling normally sits well BELOW it. That's backwards - the
            # room's own hosting Level is the strong, intended match; the
            # next-level-up allowance is only meant as a fallback for
            # projects that host ceilings to the underside of the slab
            # above. So: prefer own-Level matches outright, and only look
            # at next-Level matches if this room has no ceiling of its
            # own at all.
            if ceiling_level_id == room_level_id:
                own_level_matched.append((capped_top_z, c.Id))
            else:
                next_level_matched.append((capped_top_z, c.Id))
        elif min_allowed_z <= c_bbox.Min.Z <= max_allowed_z:
            height_banded.append((min(c_bbox.Max.Z, max_allowed_z), c.Id))

    if own_level_matched:
        top_z, best_id = max(own_level_matched)
        method = "matched by ceiling's own hosting Level (this room's own Level)"
    elif next_level_matched:
        top_z, best_id = max(next_level_matched)
        method = "matched by ceiling's own hosting Level (next Level up - no ceiling on this room's own Level)"
    elif height_banded:
        top_z, best_id = max(height_banded)
        method = "matched by height band only (no ceiling had a Level match)"
    else:
        top_z, best_id = None, None
        method = "no candidate found"
    level_matched = own_level_matched + next_level_matched

    diag = (
        "CEILING: room base={:.2f}ft, room LevelId={}, next LevelId={}, cap={} "
        "(max_allowed={:.2f}ft) | {} in-plan/in-room candidate(s), {} Level-matched, "
        "{} height-band-only | {} | picked #{} -> top_z={}{}"
    ).format(
        room_bbox.Min.Z,
        room_level_id.IntegerValue if room_level_id else "?",
        next_level_id.IntegerValue if next_level_id else "none",
        cap_note, max_allowed_z,
        in_plan_count, len(level_matched), len(height_banded),
        method,
        best_id if best_id is not None else "none",
        "{:.2f}ft".format(top_z) if top_z is not None else "n/a",
        (
            " [note: {} Level-matched ceiling(s) exceeded the {:.2f}ft cap and were clamped "
            "down to it - raw Max.Z was: {}]".format(
                len(clamped_ids),
                max_allowed_z,
                ", ".join("#{}={:.2f}ft".format(cid, z) for cid, z in clamped_ids),
            )
            if clamped_ids
            else ""
        ),
    )
    result = top_z, diag
    _CEILING_TOP_Z_CACHE[cache_key] = result
    return result


def get_room_floor_z(room):
    """World Z of this room's own floor (its bounding box base), or None
    if it can't be resolved.

    Matters for the same reason the ceiling lookup does: elevation
    markers here are routinely copy/pasted between rooms, so an
    existing crop's BOTTOM edge can't be trusted either - it may still
    reflect whatever room the marker was originally copied from (e.g. a
    different floor-to-floor height). Recomputing it from THIS room's
    own geometry, the same way the top edge is recomputed from THIS
    room's own ceiling, makes the tool self-correcting on both edges
    instead of just the top one.
    """
    room_bbox = room.get_BoundingBox(None)
    if room_bbox is None:
        return None
    return room_bbox.Min.Z


# ---------------------------------------------------------------------------
# Datum (level / grid) helpers
# ---------------------------------------------------------------------------


def get_datum_end_points(datum, view):
    """Return (p_end0, p_end1) world points for this datum's line in
    this view, or None if it can't be resolved."""
    curves = None
    try:
        curves = datum.GetCurvesInView(DatumExtentType.Model, view)
    except Exception:
        curves = None
    if not curves or len(list(curves)) == 0:
        try:
            curves = datum.GetCurvesInView(DatumExtentType.ViewSpecific, view)
        except Exception:
            curves = None
    if not curves or len(list(curves)) == 0:
        return None
    curve = list(curves)[0]
    return curve.GetEndPoint(0), curve.GetEndPoint(1)


def fix_level(level, view, crop_transform, target_right_offset, target_left_offset, hide_bubble):
    """
    Sets both ends of a level line to explicit ViewSpecific extents,
    placing the right end at target_right_offset and the left end at
    target_left_offset (both in crop-local X, i.e. dot(point, right_dir)
    relative to crop_transform.Origin).

    Both ends are set explicitly so that the left end is never left at
    whatever position the pre-existing Model-extent curve happened to
    have - a level in Model-extent mode can span the whole project width,
    and using its left_pt as-is for the new curve would place the left
    bubble (and the level line's left terminus) far outside the view.

    Returns (success, debug_string).
    """
    pts = get_datum_end_points(level, view)
    if pts is None:
        return False, "no curve returned by GetCurvesInView"
    p0, p1 = pts
    right_dir = crop_transform.BasisX.Normalize()
    origin = crop_transform.Origin
    x0 = right_dir.DotProduct(p0 - origin)
    x1 = right_dir.DotProduct(p1 - origin)

    if x0 >= x1:
        right_end, left_end = DatumEnds.End0, DatumEnds.End1
        right_pt, left_pt = p0, p1
    else:
        right_end, left_end = DatumEnds.End1, DatumEnds.End0
        right_pt, left_pt = p1, p0

    current_right_offset = right_dir.DotProduct(right_pt - origin)
    current_left_offset = right_dir.DotProduct(left_pt - origin)
    right_delta = target_right_offset - current_right_offset
    left_delta = target_left_offset - current_left_offset
    new_right_pt = right_pt + right_dir.Multiply(right_delta)
    new_left_pt = left_pt + right_dir.Multiply(left_delta)

    debug = (
        "p0=({:.2f},{:.2f},{:.2f}) p1=({:.2f},{:.2f},{:.2f}) "
        "right_end={} "
        "target_right={:.2f} current_right={:.2f} right_delta={:.2f} "
        "target_left={:.2f} current_left={:.2f} left_delta={:.2f} "
        "new_left_pt=({:.2f},{:.2f},{:.2f}) new_right_pt=({:.2f},{:.2f},{:.2f})"
    ).format(
        p0.X, p0.Y, p0.Z, p1.X, p1.Y, p1.Z,
        right_end,
        target_right_offset, current_right_offset, right_delta,
        target_left_offset, current_left_offset, left_delta,
        new_left_pt.X, new_left_pt.Y, new_left_pt.Z,
        new_right_pt.X, new_right_pt.Y, new_right_pt.Z,
    )

    try:
        # Switch BOTH ends to ViewSpecific before setting the curve so
        # Revit doesn't snap either endpoint back to a model-extent
        # position after we write the new line.
        level.SetDatumExtentType(right_end, view, DatumExtentType.ViewSpecific)
        level.SetDatumExtentType(left_end, view, DatumExtentType.ViewSpecific)
        if right_end == DatumEnds.End1:
            new_curve = Line.CreateBound(new_left_pt, new_right_pt)
        else:
            new_curve = Line.CreateBound(new_right_pt, new_left_pt)
        level.SetCurveInView(DatumExtentType.ViewSpecific, view, new_curve)
        if hide_bubble:
            level.HideBubbleInView(right_end, view)
        # Revit appears to reset bubble visibility on BOTH ends as a
        # side effect of the calls above, even though only the right
        # end's extent type changed - explicitly force the left end's
        # bubble back on rather than relying on it being left alone.
        level.ShowBubbleInView(left_end, view)
    except Exception as ex:
        logger.debug("level fix failed for {}: {}".format(level.Id, ex))
        return False, debug + " - EXCEPTION: {}".format(ex)
    return True, debug


def fix_grid(grid, view, crop_transform, target_bottom_offset, target_top_offset):
    pts = get_datum_end_points(grid, view)
    if pts is None:
        return False
    p0, p1 = pts
    up_dir = crop_transform.BasisY.Normalize()
    origin = crop_transform.Origin
    y0 = up_dir.DotProduct(p0 - origin)
    y1 = up_dir.DotProduct(p1 - origin)

    if y0 >= y1:
        top_end, bottom_end = DatumEnds.End0, DatumEnds.End1
        top_pt, bottom_pt = p0, p1
    else:
        top_end, bottom_end = DatumEnds.End1, DatumEnds.End0
        top_pt, bottom_pt = p1, p0

    top_delta = target_top_offset - up_dir.DotProduct(top_pt - origin)
    bottom_delta = target_bottom_offset - up_dir.DotProduct(bottom_pt - origin)
    new_top_pt = top_pt + up_dir.Multiply(top_delta)
    new_bottom_pt = bottom_pt + up_dir.Multiply(bottom_delta)

    try:
        grid.SetDatumExtentType(top_end, view, DatumExtentType.ViewSpecific)
        grid.SetDatumExtentType(bottom_end, view, DatumExtentType.ViewSpecific)
        if top_end == DatumEnds.End1:
            new_curve = Line.CreateBound(new_bottom_pt, new_top_pt)
        else:
            new_curve = Line.CreateBound(new_top_pt, new_bottom_pt)
        grid.SetCurveInView(DatumExtentType.ViewSpecific, view, new_curve)
    except Exception as ex:
        logger.debug("grid fix failed for {}: {}".format(grid.Id, ex))
        return False
    return True


def get_elevation_view_templates():
    """Every view template in the model, sorted with Elevation-type
    templates first (most likely to be relevant here), then everything
    else alphabetically - for the "Elevation view template" dropdown.
    We don't filter down to ViewType.Elevation only any more: some
    offices' "elevation" templates are saved under a different ViewType,
    and hiding them was why templates were going missing from the list -
    the dropdown's new filter box is there to make the fuller list easy
    to search instead."""
    views = FilteredElementCollector(doc).OfClass(View).WhereElementIsNotElementType().ToElements()
    items = []
    for v in views:
        try:
            if not v.IsTemplate:
                continue
        except Exception:
            continue
        try:
            name = v.Name
        except Exception:
            name = ""
        try:
            is_elevation = v.ViewType == ViewType.Elevation
        except Exception:
            is_elevation = False
        items.append((not is_elevation, name, v))
    items.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in items]


def apply_view_template(view, template_id, override):
    """Assigns template_id to view, unless it already has a different
    template and override is False (matches Place RLS Views' default
    behaviour: only fill in views with no template, unless told to
    force it). template_id may be None, meaning "leave templates alone"."""
    if template_id is None:
        return None
    try:
        current = view.ViewTemplateId
    except Exception as ex:
        return "could not read current view template: {}".format(ex)
    if current is not None and current != ElementId.InvalidElementId and not override:
        return "already has a view template - left alone"
    try:
        view.ViewTemplateId = template_id
        return "view template applied"
    except Exception as ex:
        return "failed to apply view template: {}".format(ex)


def fix_far_clip(view, far_clip_offset_ft):
    """Sets the view's Far Clip Offset to an absolute, user-entered
    distance (not additive - safe to re-run any number of times without
    drifting). Turns Far Clipping on (without a line) if it was off,
    since a target distance with clipping still inactive would have no
    visible effect and just look like the tool "isn't working"."""
    try:
        active_param = view.get_Parameter(BuiltInParameter.VIEWER_BOUND_ACTIVE_FAR)
        offset_param = view.get_Parameter(BuiltInParameter.VIEWER_BOUND_OFFSET_FAR)
    except Exception as ex:
        return False, "far clip parameters not available: {}".format(ex)
    if active_param is None or offset_param is None:
        return False, "far clip parameters not found on this view"
    try:
        was_active = active_param.AsInteger() != 0
        if not was_active:
            active_param.Set(1)  # 1 = Clip without line
    except Exception as ex:
        return False, "failed to activate far clipping: {}".format(ex)
    try:
        current = offset_param.AsDouble()
        offset_param.Set(far_clip_offset_ft)
        note = "far clip offset {:.3f}ft -> {:.3f}ft".format(current, far_clip_offset_ft)
        if not was_active:
            note += " (far clipping was off, turned on)"
        return True, note
    except Exception as ex:
        return False, "failed to set far clip offset: {}".format(ex)


def get_room_tag_types():
    """Every Room Tag family type in the model, sorted by family then
    type name - for the "Room tag type" dropdown. Names are resolved up
    front (not inside the sort key lambda) so one odd/dangling symbol
    can't take the whole sort down with an AttributeError."""
    symbols = (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_RoomTags)
        .OfClass(FamilySymbol)
        .ToElements()
    )
    items = []
    for s in symbols:
        try:
            fam_name = s.Family.Name
        except Exception:
            fam_name = ""
        try:
            type_name = s.Name
        except Exception:
            type_name = ""
        items.append((fam_name, type_name, s))
    items.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in items]


def room_already_tagged(view, room):
    """True if a Room Tag in this view already references this room -
    keeps re-runs from piling up duplicate tags."""
    try:
        tags = (
            FilteredElementCollector(doc, view.Id)
            .OfCategory(BuiltInCategory.OST_RoomTags)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        return False
    for tag in tags:
        try:
            tagged_room = tag.Room
        except Exception:
            tagged_room = None
        if tagged_room is None:
            try:
                tagged_id = tag.GetTaggedLocalElement().Id
            except Exception:
                tagged_id = None
        else:
            tagged_id = tagged_room.Id
        if tagged_id == room.Id:
            return True
    return False


def place_room_tag(view, room, tag_type_id, local_x, local_y):
    """Places a Room Tag for `room` in `view` at (local_x, local_y) -
    coordinates in the view's own right/up axes (same space as the crop
    box Min/Max we already compute), centred left-right on the crop and
    a quarter of the way down from the ceiling line, per how this is
    done by hand. Skips it if this room is already tagged in this view."""
    if tag_type_id is None:
        return None
    if room_already_tagged(view, room):
        return "already tagged in this view - left alone"
    try:
        tag_type = doc.GetElement(tag_type_id)
        if tag_type is not None and not tag_type.IsActive:
            tag_type.Activate()
    except Exception as ex:
        return "failed to activate tag type: {}".format(ex)
    uv = UV(local_x, local_y)
    tag = None
    try:
        # Modern API signature - room passed via LinkElementId (works for
        # a normal, non-linked room too; only its host ElementId is set).
        from Autodesk.Revit.DB import LinkElementId

        tag = doc.Create.NewRoomTag(LinkElementId(room.Id), uv, view.Id)
    except Exception:
        try:
            # Older API signature - room passed directly.
            tag = doc.Create.NewRoomTag(room, uv, view.Id)
        except Exception as ex:
            return "failed to place room tag: {}".format(ex)
    if tag is None:
        return "room tag creation returned nothing"
    try:
        tag.ChangeTypeId(tag_type_id)
    except Exception as ex:
        return "tag placed, but could not set its type: {}".format(ex)
    try:
        # (v1.6.2) Revit can force a room tag's leader back on once it
        # regenerates and decides the tag's position doesn't cleanly read
        # as "inside" the room from this view's projection - which a
        # freshly-placed tag hasn't been evaluated against yet. Setting
        # HasLeader immediately after creation was seeing that get
        # silently overridden on regenerate for some elevation views.
        # Regenerating FIRST, so Revit's own leader-forcing logic has
        # already run once, then explicitly forcing it off afterwards
        # (unconditionally, not gated behind "if tag.HasLeader" - the
        # getter right after creation isn't reliable either) fixes that.
        doc.Regenerate()
        tag.HasLeader = False
        doc.Regenerate()
        if tag.HasLeader:
            # Belt and braces: still on after two regenerates - try once
            # more, and report the true end state either way rather than
            # assuming success.
            tag.HasLeader = False
            doc.Regenerate()
    except Exception as ex:
        return "tag placed, but could not disable its leader: {}".format(ex)
    try:
        final_leader = tag.HasLeader
    except Exception:
        final_leader = None
    if final_leader:
        return "room tag placed, but leader is still ON after repeated attempts to disable it"
    return "room tag placed"


# ---------------------------------------------------------------------------
# Core per-view routine
# ---------------------------------------------------------------------------


def process_view(
    view,
    room,
    wall_offset_ft,
    ceiling_offset_ft,
    floor_offset_ft,
    datum_ext_ft,
    skip_modified,
    hide_level_bubble,
    extend_grids,
    grid_bubble_offset_ft=None,
    marker_loc=None,
    template_id=None,
    override_template=False,
    extend_far_clip=False,
    far_clip_offset_ft=None,
    room_tag_type_id=None,
    level_left_offset_ft=None,
):
    """Returns (status, message) where status in
    'adjusted' | 'skipped' | 'flagged' | 'error'."""

    crop = view.CropBox
    if crop is None or not view.CropBoxActive:
        return "flagged", "view has no active crop box"

    marker = read_marker(view)
    if skip_modified:
        if marker is None:
            # never touched by this tool - only skip if it looks like it
            # has already been resized away from a fresh default (we
            # can't know the exact original default without recomputing
            # it, so we compute it below and compare after the fact).
            pass
        elif not crop_matches_marker(view, marker):
            return "skipped", "crop modified since last run"

    room_center = get_room_center(room)
    right_dir = crop.Transform.BasisX.Normalize()
    up_dir = crop.Transform.BasisY.Normalize()
    origin = crop.Transform.Origin
    origin_x = right_dir.DotProduct(origin)

    # Primary method: scan a straight line through the SPECIFIC MARKER'S
    # OWN plan location (falling back to the room's centre only if that
    # location couldn't be resolved), along this view's left-right axis,
    # and find the walls it crosses on each side (find_side_walls_by_scan).
    # Using the marker's own position - not the room's centroid - matters
    # once a room has more than one notch/wing (e.g. it wraps around one
    # neighbour AND sits beside another): the room's width at the
    # centroid's row can differ from its width at a particular marker's
    # own row, so a scan through the centroid can cross the wrong walls
    # for a marker that isn't near it. This still depends only on the
    # room's own geometry - never on the view's existing crop - which
    # matters because elevation markers here are routinely copy/pasted
    # between rooms to speed up placement, so an existing crop can't be
    # trusted as "the room's own default"; it may well reflect whatever
    # room the marker was copied FROM. Falls back to the loop-based
    # back-wall-plus-neighbours method only if the scan can't find a
    # clean crossing on both sides.
    scan_origin = marker_loc if marker_loc is not None else room_center
    warn = None
    left_wall, right_wall, warn, loop_diag = find_side_walls_by_scan(room, scan_origin, right_dir)
    if left_wall is None or right_wall is None:
        lw2, rw2, warn2, loop_diag2 = find_side_walls_via_loop(view, room, room_center)
        if lw2 is None or rw2 is None:
            try:
                inside, method = point_in_room(room, scan_origin)
            except Exception:
                inside, method = None, "check failed"
            inside_note = (
                " || marker plan location ({:.2f},{:.2f}) is {} this room's boundary ({});"
                " a marker copy/pasted from another room can land right on a wall or just"
                " outside the room it's now meant to belong to, which stops a clean"
                " both-sides scan even though the elevation view itself still resolves -"
                " if so, the fix is to move that marker to sit properly inside this room,"
                " not a code change here.".format(
                    scan_origin.X, scan_origin.Y,
                    "INSIDE" if inside else "OUTSIDE" if inside is not None else "unknown -",
                    method,
                )
            )
            combined = "scan method: {}; loop fallback: {}{}".format(warn, warn2, inside_note)
            return "flagged", combined
        left_wall, right_wall = lw2, rw2
        warn = "matched by loop fallback, not by centre-line scan: {}".format(loop_diag2)
        loop_diag = loop_diag2

    left_face_val = get_far_face_position(left_wall, right_dir, room_center)
    right_face_val = get_far_face_position(right_wall, right_dir, room_center)
    if left_face_val is None or right_face_val is None:
        return "flagged", "could not resolve wall face geometry"

    face_method_diag = (
        "FACE METHOD left #{}: {} (face_val={:.2f}) | right #{}: {} (face_val={:.2f}) "
        "| room_center=({:.2f},{:.2f}) crop_origin=({:.2f},{:.2f}) right_dir=({:.2f},{:.2f})"
    ).format(
        left_wall.Id, get_face_method_diag(left_wall), left_face_val,
        right_wall.Id, get_face_method_diag(right_wall), right_face_val,
        room_center.X, room_center.Y, origin.X, origin.Y, right_dir.X, right_dir.Y,
    )

    origin_x = right_dir.DotProduct(origin)
    new_min_x = (left_face_val - origin_x) - wall_offset_ft
    new_max_x = (right_face_val - origin_x) + wall_offset_ft
    if new_min_x > new_max_x:
        new_min_x, new_max_x = new_max_x, new_min_x

    origin_y = up_dir.DotProduct(origin)

    ceiling_note = ""
    ceiling_top_z, ceiling_diag = get_room_top_ceiling_z(room)
    if ceiling_top_z is not None:
        ceiling_val = up_dir.DotProduct(XYZ(0, 0, ceiling_top_z))
        ceiling_local_y = ceiling_val - origin_y
        new_max_y = ceiling_local_y + ceiling_offset_ft
    else:
        new_max_y = crop.Max.Y
        ceiling_local_y = new_max_y
        ceiling_note = " (no ceiling found - top left unchanged)"

    # Bottom edge: recompute from THIS room's own floor, the same way the
    # top edge is recomputed from this room's own ceiling (see
    # get_room_floor_z's docstring for why - copy/pasted markers can
    # leave a crop's bottom edge reflecting a different room entirely).
    floor_note = ""
    floor_z = get_room_floor_z(room)
    if floor_z is not None:
        floor_val = up_dir.DotProduct(XYZ(0, 0, floor_z))
        floor_local_y = floor_val - origin_y
        new_min_y = floor_local_y - floor_offset_ft
    else:
        new_min_y = crop.Min.Y
        floor_note = " (no floor found - bottom left unchanged)"

    if skip_modified and marker is None:
        # Heuristic default check: a never-touched crop whose X span is
        # already noticeably wider than the interior-face span (i.e. the
        # un-offset span) suggests someone already dragged it by hand.
        interior_span = abs(right_face_val - left_face_val)
        current_span = crop.Max.X - crop.Min.X
        if current_span > interior_span + wall_offset_ft * 0.5:
            return "skipped", "crop appears already manually resized"

    new_box = BoundingBoxXYZ()
    new_box.Transform = crop.Transform
    new_box.Min = XYZ(new_min_x, new_min_y, crop.Min.Z)
    new_box.Max = XYZ(new_max_x, new_max_y, crop.Max.Z)
    view.CropBox = new_box

    # --- readback diagnostic: what did Revit actually store? ---
    try:
        rb = view.CropBox
        rb_origin = rb.Transform.Origin
        rb_basis_x = rb.Transform.BasisX
        intended_world_min = origin_x + new_min_x
        intended_world_max = origin_x + new_max_x
        rb_origin_x = rb_basis_x.DotProduct(rb_origin)
        rb_world_min = rb_origin_x + rb.Min.X
        rb_world_max = rb_origin_x + rb.Max.X
        readback_diag = (
            "READBACK: set Min.X={:.3f} Max.X={:.3f} (local) | "
            "intended world X=[{:.3f},{:.3f}] | "
            "after-set Min.X={:.3f} Max.X={:.3f} (local), origin=({:.3f},{:.3f}), "
            "basisX=({:.3f},{:.3f}) | after-set world X=[{:.3f},{:.3f}] | "
            "origin_shift={:.3f}"
        ).format(
            new_min_x, new_max_x,
            intended_world_min, intended_world_max,
            rb.Min.X, rb.Max.X, rb_origin.X, rb_origin.Y,
            rb_basis_x.X, rb_basis_x.Y,
            rb_world_min, rb_world_max,
            rb_origin_x - origin_x,
        )
    except Exception as ex:
        readback_diag = "READBACK: failed to read back ({})".format(ex)

    write_marker(view, new_min_x, new_max_x, new_max_y, new_min_y)

    # --- levels ---
    level_count = 0
    level_debug = []
    if hide_level_bubble is not None:
        levels = (
            FilteredElementCollector(doc, view.Id)
            .OfCategory(BuiltInCategory.OST_Levels)
            .WhereElementIsNotElementType()
            .ToElements()
        )
        # (v1.6.7 fix) Both target offsets are set explicitly so fix_level
        # never uses the left endpoint from a Model-extent curve as-is.
        # A level in Model-extent mode spans the whole project; its left
        # endpoint could be hundreds of feet outside the view, and using
        # it as the new line's left end placed the left bubble (and all
        # level markers in the view) far off to the left.
        # (v1.6.8) level_left_offset_ft is independent of datum_ext_ft so
        # the left end can be set to a different distance than the right end.
        target_right = new_max_x + datum_ext_ft
        _level_left_ext = level_left_offset_ft if level_left_offset_ft is not None else datum_ext_ft
        target_left = new_min_x - _level_left_ext
        for lvl in levels:
            try:
                in_group = lvl.GroupId is not None and lvl.GroupId != ElementId.InvalidElementId
            except Exception:
                in_group = False
            if in_group:
                level_debug.append("Level {} is in a model group - left alone".format(lvl.Id))
                continue
            ok, dbg = fix_level(lvl, view, new_box.Transform, target_right, target_left, hide_level_bubble)
            if ok:
                level_count += 1
            level_debug.append("{}{}".format("" if ok else "FAILED ", dbg))

    # --- grids ---
    grid_count = 0
    if extend_grids:
        grids = (
            FilteredElementCollector(doc, view.Id)
            .OfCategory(BuiltInCategory.OST_Grids)
            .WhereElementIsNotElementType()
            .ToElements()
        )
        target_bottom = new_min_y - datum_ext_ft
        # Top end (and its bubble) deliberately does NOT extend past the
        # crop like the bottom end does - it's pulled back down to sit
        # just below the ceiling line instead, so the bubble reads inside
        # the room rather than poking up above the ceiling.
        _grid_top_offset = grid_bubble_offset_ft if grid_bubble_offset_ft is not None else 0.0
        target_top = ceiling_local_y - _grid_top_offset
        for gr in grids:
            if fix_grid(gr, view, new_box.Transform, target_bottom, target_top):
                grid_count += 1

    # --- view template ---
    template_note = apply_view_template(view, template_id, override_template)

    # --- far clip offset ---
    far_clip_note = None
    if extend_far_clip and far_clip_offset_ft is not None:
        _, far_clip_note = fix_far_clip(view, far_clip_offset_ft)

    # --- room tag: centred left-right on the crop, a quarter of the
    # way down from the ceiling line ---
    tag_local_x = (new_min_x + new_max_x) / 2.0
    tag_local_y = ceiling_local_y - 0.25 * (ceiling_local_y - new_min_y)
    tag_note = place_room_tag(view, room, room_tag_type_id, tag_local_x, tag_local_y)

    # The crop REGION (the visible dashed rectangle + drag handles) is
    # separate from the crop BOX (the extents we just set) and from
    # CropBoxActive (on/off) - this just makes sure the outline isn't
    # left visible/clickable in the view once the tool has finished with it.
    crop_vis_note = ""
    try:
        if view.CropBoxVisible:
            view.CropBoxVisible = False
    except Exception as ex:
        crop_vis_note = " (could not hide crop region: {})".format(ex)

    msg = "cropped OK, {} level(s), {} grid(s)".format(level_count, grid_count)
    msg += crop_vis_note
    msg += ceiling_note
    msg += floor_note
    if warn:
        msg += " [note: {}]".format(warn)
    if template_note:
        msg += " || TEMPLATE: " + template_note
    if far_clip_note:
        msg += " || FAR CLIP: " + far_clip_note
    if tag_note:
        msg += " || ROOM TAG: " + tag_note
    if loop_diag:
        msg += " || WALL PICK: " + loop_diag
    msg += " || " + ceiling_diag
    msg += " || " + face_method_diag
    msg += " || " + readback_diag
    if level_debug:
        msg += " || LEVEL DEBUG: " + " ;; ".join(level_debug)
    return "adjusted", msg


# ---------------------------------------------------------------------------
# Room / elevation discovery
# ---------------------------------------------------------------------------


def get_all_rooms(scope):
    collector = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType()
    rooms = [r for r in collector.ToElements() if r.Area > 0]

    if scope == "Active view":
        av = doc.ActiveView
        rooms = [r for r in rooms if r.Id in [e.Id for e in FilteredElementCollector(doc, av.Id).OfCategory(BuiltInCategory.OST_Rooms).ToElements()]]
    elif scope == "Active level":
        av = doc.ActiveView
        level_id = getattr(av, "GenLevel", None)
        if level_id is not None:
            rooms = [r for r in rooms if r.LevelId == level_id.Id]
    return rooms


def get_room_test_point(room):
    """A point safely inside the room's plan area, at a Z height within
    the room's vertical range - used for point-in-room tests."""
    bb = room.get_BoundingBox(None)
    z = bb.Min.Z + 0.1 if bb is not None else 0.0
    center = get_room_center(room)
    return XYZ(center.X, center.Y, z)


def point_in_room(room, point):
    """Best-effort point-in-room test: tries Room.IsPointInRoom (needs a
    point within the room's own vertical range), falls back to a plan
    bounding-box overlap check."""
    try:
        test_pt = XYZ(point.X, point.Y, get_room_test_point(room).Z)
        result = room.IsPointInRoom(test_pt)
        return result, "IsPointInRoom"
    except Exception:
        pass
    rb = room.get_BoundingBox(None)
    inside = (
        rb is not None
        and rb.Min.X <= point.X <= rb.Max.X
        and rb.Min.Y <= point.Y <= rb.Max.Y
    )
    return inside, "bbox fallback"


_REF_PLAN_VIEWS_CACHE = {}
_MARKER_LOCATION_CACHE = {}  # (marker.Id.IntegerValue, level_key) -> (test_pt, resolved_view, fail_diag)


def find_reference_plan_views(room):
    """Ordered candidate ViewPlans we can use to ask Revit for an
    ElevationMarker's real bounding box (an ElevationMarker has no
    bounding box outside of a specific view - passing None always
    comes back empty).

    We deliberately don't pick a single "best" view up front: besides
    crop region (Element.get_BoundingBox(view) comes back None for
    anything outside that view's crop, not just its model extents), a
    view can also hide Elevation Markers entirely for reasons we can't
    detect in advance from the API - a phase filter (e.g. an "EXISTING"
    plan not showing anything from a later phase), a view template,
    Visibility/Graphics overrides, worksets, and so on. So this returns
    several candidates in a sensible order (uncropped plans on the
    room's own level first, cropped ones after) and get_marker_location()
    tries each in turn until one actually resolves a bounding box,
    instead of committing to one guess that might silently fail for
    every marker.

    Deliberately restricted to the room's OWN level, never any other
    level or an active view on a different level: a plan on a different
    level/building can genuinely report a marker's bounding box just
    fine while still being the wrong reference for THIS room (its own
    transform, phase, or a repeated floor layout elsewhere in the
    building can put the resolved point at coordinates that happen to
    land inside this room's 2D footprint even though the marker belongs
    to a completely different room). That showed up as an unrelated
    room's elevation views being pulled into this room's results and
    resized using this room's walls - worse than just failing to resolve
    a few markers, so cross-level fallback is not worth the risk. Cached
    per level."""
    key = room.LevelId.IntegerValue if room.LevelId else -1
    if key in _REF_PLAN_VIEWS_CACHE:
        return _REF_PLAN_VIEWS_CACHE[key]

    def is_uncropped(v):
        try:
            return not v.CropBoxActive
        except Exception:
            return False

    def same_level(v):
        try:
            return v.GenLevel is not None and v.GenLevel.Id == room.LevelId
        except Exception:
            return False

    active = doc.ActiveView
    plans = [
        v
        for v in FilteredElementCollector(doc).OfClass(ViewPlan).WhereElementIsNotElementType().ToElements()
        if not v.IsTemplate and same_level(v)
    ]

    ordered = []
    seen_ids = set()

    def add(v):
        if v is None:
            return
        vid = v.Id.IntegerValue
        if vid in seen_ids:
            return
        seen_ids.add(vid)
        ordered.append(v)

    if isinstance(active, ViewPlan) and not active.IsTemplate and same_level(active):
        add(active)

    for v in sorted(plans, key=lambda v: not is_uncropped(v)):
        add(v)

    _REF_PLAN_VIEWS_CACHE[key] = ordered
    return ordered


def get_marker_location(marker, ref_views, fail_diag=None):
    """The marker's real plan position. ElevationMarker.Location and
    get_BoundingBox(None) both come back empty (it's a 2D annotation
    symbol with no model-space geometry), so we ask for its bounding
    box in an actual plan view instead, which does work - trying each
    view in ref_views (see find_reference_plan_views) in turn, since a
    single view can fail to show the marker for several unrelated
    reasons (crop region, phase filter, visibility overrides...) and we
    have no way to know which in advance.

    Gotcha: that bounding box also includes the on-plan crop-width
    indicator line Revit draws for each of the marker's 4 views when
    that view's "Crop Region Visible" is on - and that line is exactly
    as wide as whatever crop the view currently has, correct or not
    (e.g. a copy/pasted marker that inherited an unrelated room's much
    wider crop). A lopsided crop then drags the bounding box's midpoint
    off to one side, away from the marker's true centre - which throws
    off the side-wall scan that uses this point as its origin. To avoid
    that, we temporarily switch Crop Region Visible off on the marker's
    own 4 views (a plain, symmetric fixed-size symbol has no width
    indicator to distort the box), read the bounding box, then restore
    the original visibility. This needs an active transaction - callers
    must ensure one is open.

    Returns (point, used_view) - used_view is whichever ref_views entry
    actually resolved the bounding box (or None, if it fell back to
    marker.Location instead), kept purely for diagnostics: which
    specific view a marker's Z came from turned out to matter - the
    same marker can apparently resolve a different Z depending on which
    reference view answers, not just which room is asking."""
    if ref_views is None:
        ref_views = []
    elif not isinstance(ref_views, list):
        ref_views = [ref_views]

    if ref_views:
        toggled_off = []
        try:
            for i in range(4):
                try:
                    if marker.IsAvailableIndex(i):
                        continue
                    view_id = marker.GetViewId(i)
                except Exception:
                    continue
                if view_id is None or view_id == ElementId.InvalidElementId:
                    continue
                v = doc.GetElement(view_id)
                if v is None:
                    continue
                try:
                    if v.CropBoxVisible:
                        v.CropBoxVisible = False
                        toggled_off.append(v)
                except Exception:
                    pass
            if fail_diag is not None:
                try:
                    created_param = marker.get_Parameter(BuiltInParameter.PHASE_CREATED)
                    demo_param = marker.get_Parameter(BuiltInParameter.PHASE_DEMOLISHED)
                    created = doc.GetElement(created_param.AsElementId()) if created_param else None
                    demo_id = demo_param.AsElementId() if demo_param else None
                    demo = doc.GetElement(demo_id) if demo_id and demo_id != ElementId.InvalidElementId else None
                    fail_diag.append(
                        "(marker's own phase created: '{}', phase demolished: '{}')".format(
                            created.Name if created else "?",
                            demo.Name if demo else "none",
                        )
                    )
                except Exception as ex:
                    fail_diag.append("(marker phase check failed: {})".format(ex))
            try:
                for rv in ref_views:
                    try:
                        bbox = marker.get_BoundingBox(rv)
                    except Exception as ex:
                        bbox = None
                        if fail_diag is not None:
                            fail_diag.append("'{}': exception - {}".format(rv.Name, ex))
                    else:
                        if bbox is None and fail_diag is not None:
                            fail_diag.append("'{}': get_BoundingBox returned None (not visible/scoped in this view)".format(rv.Name))
                    if bbox is not None:
                        return (bbox.Min + bbox.Max) / 2.0, rv
            finally:
                for v in toggled_off:
                    try:
                        v.CropBoxVisible = True
                    except Exception:
                        pass
        except Exception:
            pass
    loc = marker.Location
    if loc is not None and hasattr(loc, "Point"):
        return loc.Point, None
    return None, None


def _diagnose_ref_view_visibility(view, room_bbox=None):
    """Best-effort explanation of why a reference plan view might hide
    EVERY ElevationMarker in the model (not just one room's), so a
    whole floor's worth of views can fail get_BoundingBox() for a
    reason that has nothing to do with any individual marker: the
    Elevation Marks category turned off (directly or via a view
    template), the view's workset itself not visible, or a phase
    filter. Never raises - returns a short diagnostic string.

    `room_bbox`, if given (the calling room's own get_BoundingBox(None)),
    is cross-checked against this view's CropBox - see the comment
    further down for why that, and not Level.Elevation, is used for
    this comparison."""
    bits = []
    try:
        cat = Category.GetCategory(doc, BuiltInCategory.OST_Elev)
        if cat is not None:
            hidden = view.GetCategoryHidden(cat.Id)
            bits.append("Elevation Marks category hidden in view: {}".format(hidden))
        else:
            bits.append("Elevation Marks category: <not found in doc>")
    except Exception as ex:
        bits.append("category-hidden check failed: {}".format(ex))

    try:
        tmpl_id = view.ViewTemplateId
        if tmpl_id is not None and tmpl_id != ElementId.InvalidElementId:
            tmpl = doc.GetElement(tmpl_id)
            bits.append("view template: '{}'".format(tmpl.Name if tmpl else tmpl_id))
        else:
            bits.append("view template: none")
    except Exception as ex:
        bits.append("view-template check failed: {}".format(ex))

    try:
        phase_param = view.get_Parameter(BuiltInParameter.VIEW_PHASE)
        if phase_param is not None:
            phase_id = phase_param.AsElementId()
            phase = doc.GetElement(phase_id) if phase_id else None
            bits.append("view phase: '{}'".format(phase.Name if phase else phase_id))
    except Exception as ex:
        bits.append("view-phase check failed: {}".format(ex))

    # The Phase itself (above) only says which phase the view is "as of" -
    # it's the separate Phase Filter that actually decides whether a
    # New/Existing/Demolished/Temporary element is shown, not-shown, or
    # shown-with-overrides for that phase. A filter set to hide "New"
    # elements would make get_BoundingBox() return a clean None for any
    # marker placed in a later phase than the rooms around it, with no
    # exception raised - indistinguishable from a view-range problem
    # without checking this directly.
    try:
        filter_param = view.get_Parameter(BuiltInParameter.VIEW_PHASE_FILTER)
        if filter_param is not None:
            filter_id = filter_param.AsElementId()
            pfilter = doc.GetElement(filter_id) if filter_id else None
            bits.append("view phase filter: '{}'".format(pfilter.Name if pfilter else filter_id))
    except Exception as ex:
        bits.append("view-phase-filter check failed: {}".format(ex))

    try:
        if doc.IsWorkshared:
            ws_id = view.WorksetId
            ws_table = doc.GetWorksetTable()
            visibility = ws_table.GetWorksetVisibility(ws_id, view.Id) if ws_table else None
            bits.append("view's own workset visibility: {}".format(visibility))
    except Exception as ex:
        bits.append("workset-visibility check failed: {}".format(ex))

    try:
        bits.append("crop active: {}, crop box visible: {}".format(view.CropBoxActive, view.CropBoxVisible))
    except Exception:
        pass

    # (v1.5.4->v1.5.6 correction) An earlier version of this diagnostic
    # reported each View Range plane as Level.Elevation + offset, and
    # compared that absolute figure against the room's bounding-box Z.
    # That comparison is invalid in THIS project specifically: this
    # document's Levels do not report Elevation on a consistent basis
    # relative to their own hosted geometry (confirmed: some Levels read
    # within a few ft of the rooms/views on them, others - including the
    # Level this room's own reference views sit on - read thousands of ft
    # away from their own hosted room's real Z). That earlier version's
    # "5438ft vs 44ft mismatch" finding was this bug, not a real problem
    # with the room or the view - find_reference_plan_views() already
    # guarantees every candidate view's GenLevel.Id equals the room's own
    # LevelId, so the view IS on the room's correct level regardless of
    # what number Level.Elevation happens to report for it.
    #
    # So: report View Range as a plain offset from each plane's own level
    # (no absolute-elevation arithmetic), and separately do a real
    # apples-to-apples check using CropBox, which - unlike Level.Elevation
    # - is always in the same model/project coordinate system as
    # Room.get_BoundingBox().
    try:
        if PlanViewPlane is not None and isinstance(view, ViewPlan):
            vr = view.GetViewRange()
            gen_level = view.GenLevel

            def _plane_offset(plane):
                try:
                    offset = vr.GetOffset(plane)
                    level_id = vr.GetLevelId(plane)
                    if level_id is None or level_id == ElementId.InvalidElementId:
                        return "offset {:.2f}ft (level not set)".format(offset)
                    lvl = doc.GetElement(level_id)
                    if lvl is None:
                        return "offset {:.2f}ft (unknown level)".format(offset)
                    is_gen_level = gen_level is not None and lvl.Id == gen_level.Id
                    return "offset {:.2f}ft from '{}'{}".format(
                        offset, lvl.Name, " (view's own level)" if is_gen_level else ""
                    )
                except Exception as ex:
                    return "<error: {}>".format(ex)

            bits.append(
                "view range - top: {}; cut: {}; bottom: {}".format(
                    _plane_offset(PlanViewPlane.TopClipPlane),
                    _plane_offset(PlanViewPlane.CutPlane),
                    _plane_offset(PlanViewPlane.BottomClipPlane),
                )
            )
    except Exception as ex:
        bits.append("view-range check failed: {}".format(ex))

    if room_bbox is not None:
        try:
            if view.CropBoxActive:
                cb = view.CropBox
                tf = cb.Transform
                inv = tf.Inverse
                corners = [
                    XYZ(x, y, z)
                    for x in (room_bbox.Min.X, room_bbox.Max.X)
                    for y in (room_bbox.Min.Y, room_bbox.Max.Y)
                    for z in (room_bbox.Min.Z, room_bbox.Max.Z)
                ]
                local = [inv.OfPoint(p) for p in corners]
                room_min_x = min(p.X for p in local)
                room_max_x = max(p.X for p in local)
                room_min_y = min(p.Y for p in local)
                room_max_y = max(p.Y for p in local)
                room_min_z = min(p.Z for p in local)
                room_max_z = max(p.Z for p in local)
                overlaps_xy = (
                    room_min_x <= cb.Max.X
                    and room_max_x >= cb.Min.X
                    and room_min_y <= cb.Max.Y
                    and room_max_y >= cb.Min.Y
                )
                overlaps_z = room_min_z <= cb.Max.Z and room_max_z >= cb.Min.Z
                bits.append(
                    "room bbox vs this view's crop box (ground truth, same coord system as Room.get_BoundingBox): "
                    "XY overlaps={}, Z overlaps={} (room Z {:.2f}ft to {:.2f}ft in crop-local coords, crop Z {:.2f}ft to {:.2f}ft)".format(
                        overlaps_xy, overlaps_z, room_min_z, room_max_z, cb.Min.Z, cb.Max.Z
                    )
                )
            else:
                bits.append("room bbox vs crop box check skipped: view.CropBoxActive is False")
        except Exception as ex:
            bits.append("room-vs-cropbox check failed: {}".format(ex))

    return "; ".join(bits)


def diagnose_single_marker(marker):
    """(v1.6.0) Deep-dive on ONE pre-selected ElevationMarker: test its
    get_BoundingBox() against EVERY view in the entire document (not just
    the handful of room-specific reference plan views), so a marker that
    won't resolve for room-search purposes can be checked on its own,
    directly, without the room-matching heuristic in between.

    Returns a list of (label, detail) rows suitable for output.print_table,
    same shape as the diag lists used elsewhere in this file.
    """
    rows = []
    rows.append(("Marker Id", str(marker.Id)))

    try:
        loc = marker.Location
        if hasattr(loc, "Point") and loc.Point is not None:
            p = loc.Point
            rows.append(
                ("Marker.Location.Point", "({:.2f}, {:.2f}, {:.2f})".format(p.X, p.Y, p.Z))
            )
        else:
            rows.append(("Marker.Location.Point", "not a LocationPoint / None"))
    except Exception as ex:
        rows.append(("Marker.Location.Point", "failed: {}".format(ex)))

    # The marker's own 4 (or fewer) elevation-direction views - these are
    # the actual ViewSection elevation views this marker owns, separate
    # from the plan views we're about to probe get_BoundingBox() against.
    try:
        own_view_ids = []
        for i in range(4):
            try:
                if marker.IsAvailableIndex(i):
                    continue
                vid = marker.GetViewId(i)
                if vid is not None and vid != ElementId.InvalidElementId:
                    own_view_ids.append(vid)
            except Exception:
                pass
        own_views = [doc.GetElement(vid) for vid in own_view_ids]
        own_view_names = [v.Name for v in own_views if v is not None]
        rows.append(
            (
                "Marker's own elevation views",
                ", ".join(own_view_names) if own_view_names else "(none found)",
            )
        )
    except Exception as ex:
        rows.append(("Marker's own elevation views", "failed to enumerate: {}".format(ex)))

    # Every view in the whole document, plan or otherwise - a much wider
    # net than find_reference_plan_views() ever casts, on purpose.
    all_views = (
        FilteredElementCollector(doc)
        .OfClass(View)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    all_views = [v for v in all_views if not v.IsTemplate]
    rows.append(("Total views in document (non-template)", str(len(all_views))))

    resolved = []
    failed_count = 0
    for v in all_views:
        try:
            bbox = marker.get_BoundingBox(v)
        except Exception:
            bbox = None
        if bbox is not None:
            try:
                ctr = (bbox.Min + bbox.Max) * 0.5
                resolved.append((v, ctr))
            except Exception:
                resolved.append((v, None))
        else:
            failed_count += 1

    rows.append(
        (
            "get_BoundingBox() resolves in",
            "{} of {} views ({} return None)".format(
                len(resolved), len(all_views), failed_count
            ),
        )
    )
    if resolved:
        for v, ctr in resolved:
            vtype = v.ViewType.ToString() if hasattr(v, "ViewType") else "?"
            ctr_str = (
                "({:.2f}, {:.2f}, {:.2f})".format(ctr.X, ctr.Y, ctr.Z)
                if ctr is not None
                else "(bbox present, center calc failed)"
            )
            rows.append(("  resolves in -> '{}' ({})".format(v.Name, vtype), "center {}".format(ctr_str)))
    else:
        rows.append(
            (
                "(none)",
                "This marker's get_BoundingBox() returns None in EVERY view in the "
                "document, including its own elevation views. That points at the "
                "marker itself (corrupt/orphaned annotation, or genuinely not "
                "placed in any active view) rather than at any particular room's "
                "reference-view choice.",
            )
        )

    # (v1.6.1) Cross-check against candidate rooms by NAME, not just by the
    # Z-band math: a "wrong floor" verdict from get_elevation_views_for_room
    # is only ever inferred (marker Z outside room's own bbox-derived
    # band) - it never actually confirms a second, distinct marker exists
    # elsewhere. So when the user reports there is no other marker sitting
    # in the room's own plan view, that inference needs checking directly:
    # guess the room name/number from this marker's own elevation view
    # names (they're auto-named after the room they were placed in), find
    # every room in the model with a matching name, and report - for each
    # one - whether this marker's XY genuinely falls inside that room's
    # footprint (Room.IsPointInRoom, Z-independent) alongside the room's
    # own floor band vs this marker's resolved Z. That tells us whether
    # the floor-band check is correctly rejecting a genuine duplicate, or
    # incorrectly rejecting this room's only real marker over a Z
    # mismatch that isn't actually about a different floor.
    room_name_guess = None
    if own_view_names:
        m = re.match(r"^(.*?)\s+ELEVATION\s+\d+\s*$", own_view_names[0], re.IGNORECASE)
        room_name_guess = m.group(1).strip() if m else own_view_names[0].strip()

    if room_name_guess and resolved:
        # Prefer the center point from a resolved view whose own name
        # contains the room-name guess (the most specific / least likely
        # to be some unrelated generic plan) over just the first resolved
        # view, for the XY/Z test point.
        test_view, test_ctr = None, None
        for v, ctr in resolved:
            if ctr is not None and room_name_guess.lower() in v.Name.lower():
                test_view, test_ctr = v, ctr
                break
        if test_ctr is None:
            for v, ctr in resolved:
                if ctr is not None:
                    test_view, test_ctr = v, ctr
                    break

        rows.append(("Room-name guess (from own elevation views)", room_name_guess))

        if test_ctr is not None:
            rows.append(
                (
                    "Test point used for room cross-check",
                    "({:.2f}, {:.2f}, {:.2f}) via '{}'".format(
                        test_ctr.X, test_ctr.Y, test_ctr.Z, test_view.Name
                    ),
                )
            )
            all_rooms = list(
                FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_Rooms)
                .WhereElementIsNotElementType()
                .ToElements()
            )
            matches = [r for r in all_rooms if room_name_guess.lower() in RoomListItem(r).name.lower()]
            if not matches:
                rows.append(
                    (
                        "Rooms matching '{}'".format(room_name_guess),
                        "none found in the model by that name/number",
                    )
                )
            for r in matches:
                try:
                    rb = r.get_BoundingBox(None)
                    band_lo, band_hi = (rb.Min.Z - 2.0, rb.Min.Z + 2.0) if rb is not None else (None, None)
                    inside, method = point_in_room(r, test_ctr)
                    level = doc.GetElement(r.LevelId) if r.LevelId else None
                    z_gap = (
                        "n/a"
                        if band_lo is None
                        else (
                            "0 (within band)"
                            if band_lo <= test_ctr.Z <= band_hi
                            else "{:.2f}ft".format(min(abs(test_ctr.Z - band_lo), abs(test_ctr.Z - band_hi)))
                        )
                    )
                    rows.append(
                        (
                            "Room '{}' (Level '{}')".format(
                                RoomListItem(r).name, level.Name if level else "?"
                            ),
                            "XY inside room ({}): {} | room floor band [{:.2f}, {:.2f}] vs marker Z {:.2f} "
                            "-> gap {}".format(
                                method, inside, band_lo, band_hi, test_ctr.Z, z_gap
                            ),
                        )
                    )
                except Exception as ex:
                    rows.append(("Room '{}'".format(RoomListItem(r).name), "cross-check failed: {}".format(ex)))
        else:
            rows.append(("Room cross-check", "skipped - no resolved center point available"))

    return rows


def get_elevation_views_for_room(room, diag=None):
    """All (view, marker_location) pairs for ViewSection elevation views
    (from any ElevationMarker in the model) whose MARKER sits inside
    this room's boundary. marker_location is the marker's own plan
    position (not the room's centre) - needed so the side-wall scan
    for each view can be run through where that specific view actually
    is, not through the room's centroid, since a room with more than
    one notch/wing can have a different width at different points and
    a marker isn't necessarily anywhere near the centroid. If `diag`
    (a list) is passed, appends one row per marker/view describing
    what happened, for troubleshooting."""
    markers = list(FilteredElementCollector(doc).OfClass(ElevationMarker).ToElements())
    result = []
    ref_views = find_reference_plan_views(room)

    # Stacked rooms (the same STAFF REST layout repeated floor after
    # floor, say) share the same XY footprint, so an XY-only "is this
    # marker's point inside this room's polygon" test - which is all
    # point_in_room does, since it substitutes in THIS room's own Z -
    # will happily say yes for every floor's copy of that room, not just
    # the one the marker actually sits on.
    #
    # What get_marker_location() actually resolves for a marker's Z
    # (via a plan view's bounding box) isn't the marker's true placement
    # height - real testing showed it comes back as exactly that
    # reference plan VIEW's own cut-plane elevation, which itself sits
    # at that floor's own base. A B-144 marker queried through a
    # B-144-level view reads 14.18ft (== B-144's own floor base, exactly);
    # a B-231 marker queried through a B-231-level view reads 24.55ft
    # (== B-231's own floor base, exactly). So it's still a solid floor
    # discriminator, just a precise one, not a "somewhere between floor
    # and ceiling" one - hence a TIGHT band right around this room's own
    # floor base, not a wide one up to the ceiling (that was tried and
    # was too generous: with only ~10ft between floors here, a
    # ceiling+10ft margin comfortably covered the floor above too).
    #
    # (Level.Elevation itself was tried first for the upper bound and
    # abandoned - this project's document-wide Level collection sits on
    # a totally different numeric scale, e.g. "200-Basement" at 5380+ft
    # against a 14ft room base, so any comparison against it is
    # meaningless here.)
    room_bbox = room.get_BoundingBox(None)
    if room_bbox is not None:
        floor_min_z = room_bbox.Min.Z - 2.0
        floor_max_z = room_bbox.Min.Z + 2.0
    else:
        floor_min_z = floor_max_z = None

    if diag is not None:
        names = ", ".join(v.Name for v in ref_views[:4])
        if len(ref_views) > 4:
            names += ", ... ({} more)".format(len(ref_views) - 4)
        diag.append(
            (
                "(room)",
                "total markers in model: {} (reference plan views tried in order: {})".format(
                    len(markers), names if ref_views else "none found"
                ),
            )
        )
        # Report room bbox Z alongside room.Level.Elevation directly, so any
        # gap between them (this project's Levels are known NOT to sit on a
        # consistent elevation basis - see comment above) is visible up
        # front instead of being silently baked into other numbers below.
        try:
            room_level = doc.GetElement(room.LevelId) if room.LevelId else None
            if room_bbox is not None and room_level is not None:
                diag.append(
                    (
                        "(room)",
                        "room bbox Z: {:.2f}ft to {:.2f}ft; room's own Level '{}'.Elevation: {:.2f}ft "
                        "(a large gap here is expected/known in this project and is NOT itself the bug - "
                        "see room-vs-cropbox line below for the real check)".format(
                            room_bbox.Min.Z, room_bbox.Max.Z, room_level.Name, room_level.Elevation
                        ),
                    )
                )
        except Exception as ex:
            diag.append(("(room)", "room/level Z comparison failed: {}".format(ex)))

        for v in ref_views[:4]:
            diag.append(
                (
                    "(ref view)",
                    "'{}': {}".format(v.Name, _diagnose_ref_view_visibility(v, room_bbox)),
                )
            )

        # (v1.5.8-v1.5.9 had an "all markers vs ref view" probe here - it
        # ran get_BoundingBox() for every marker in the model against up
        # to 11 reference views, PER ROOM, PER RUN, to help root-cause the
        # B-403 lookup failure. That's resolved now (recreating B-403's
        # elevations fixed it) and the probe was pure overhead on every
        # normal run since it wasn't gated to failures only, so it's been
        # removed as of v1.6.3. If a similar "no elevations matched"
        # mystery shows up again for some other room: that probe's full
        # code is preserved in the v1.5.9 delivered copy of this script,
        # or select the room's marker directly in Revit and use the
        # diagnose_single_marker() path below (triggered automatically by
        # pre-selecting an ElevationMarker before clicking Run) - it does
        # the same kind of whole-document get_BoundingBox() sweep, just
        # scoped to one marker instead of every marker in the model, so
        # it's cheap enough to leave in permanently.

    # Getting a marker's real Z has turned out harder than expected: two
    # "should be reliable" sources (ElevationMarker.Location / a
    # model-space bounding box, then a per-marker elevation view's own
    # .Origin/CropBox.Transform.Origin) all came back empty or a flat
    # 0.00ft on a real run. What DID turn out reliable - confirmed by a
    # side-by-side probe against real Revit data - is the Z component of
    # the point get_marker_location() already resolves for the XY test:
    # it's read from the marker's bounding box in one of THIS room's own
    # reference plan views, and empirically it tracks the marker's real
    # placement, not the query view's cut plane (a wrongly-matched
    # marker from a floor below came back at a visibly different,
    # correct-for-its-own-floor Z, even though it was queried through a
    # view on THIS room's level). So no extra API calls needed - just use
    # test_pt.Z, bounded to this room's own floor-to-next-level band (the
    # same technique already used for ceiling matching).
    #
    # (v1.6.6) get_marker_location() is by far the most expensive call in
    # this whole loop - it can walk through several reference plan views
    # per marker, and the FIRST time any given view is touched this
    # session, Revit has to generate that view's graphics from scratch to
    # answer a bounding-box query (that's the "generating graphics"
    # slowdown). This loop runs once per ROOM and tests EVERY marker in
    # the whole model each time (most belong to other rooms entirely), so
    # without caching, running the tool on N rooms one at a time redoes
    # this full-model sweep N times over. A marker's resolved location
    # only depends on which set of reference views was used (i.e. which
    # Level the querying room is on - see find_reference_plan_views), not
    # on which room asked, so it's safe to memoize per (marker, level) and
    # reuse across every room and every run for the rest of this Revit
    # session (this module's globals persist across button presses -
    # engine: clean: false in bundle.yaml).
    level_key = room.LevelId.IntegerValue if room.LevelId else -1
    for marker in markers:
        cache_key = (marker.Id.IntegerValue, level_key)
        cached = _MARKER_LOCATION_CACHE.get(cache_key)
        if cached is not None:
            test_pt, resolved_view, fail_diag = cached
        else:
            fail_diag = []
            test_pt, resolved_view = get_marker_location(marker, ref_views, fail_diag)
            _MARKER_LOCATION_CACHE[cache_key] = (test_pt, resolved_view, fail_diag)
        resolved_view_name = resolved_view.Name if resolved_view is not None else "(Location.Point fallback)"
        if test_pt is None:
            if diag is not None:
                fail_summary = "; ".join(fail_diag) if fail_diag else "(no ref views to try at all)"
                diag.append(
                    (
                        marker.Id,
                        "could not resolve marker location at all - tried {} view(s): {}".format(
                            len(ref_views), fail_summary
                        ),
                    )
                )
            continue

        if floor_min_z is not None and (test_pt.Z < floor_min_z or test_pt.Z > floor_max_z):
            if diag is not None:
                diag.append(
                    (
                        marker.Id,
                        "own Z ({:.2f}ft, via plan-view bounding box in '{}') is outside this "
                        "room's floor band [{:.2f}, {:.2f}] - belongs to a different (stacked) "
                        "floor even though its XY may sit inside this room's footprint".format(
                            test_pt.Z, resolved_view_name, floor_min_z, floor_max_z
                        ),
                    )
                )
            continue

        inside, method = point_in_room(room, test_pt)
        if not inside:
            if diag is not None:
                # (v1.5.7 addition) How far outside the room's own plan
                # bbox this point actually is, in mm - IsPointInRoom uses
                # the room's true (possibly non-rectangular) polygon, so a
                # point can fail that test while sitting only a hair
                # outside the room's footprint (e.g. a marker nudged just
                # past a doorway/wall edge). A near-miss of a few mm is a
                # very different situation from one that's metres away -
                # the former is very likely this room's real marker,
                # mis-rejected by a too-strict boundary test.
                try:
                    dx = max(room_bbox.Min.X - test_pt.X, 0.0, test_pt.X - room_bbox.Max.X)
                    dy = max(room_bbox.Min.Y - test_pt.Y, 0.0, test_pt.Y - room_bbox.Max.Y)
                    dist_mm = ((dx ** 2 + dy ** 2) ** 0.5) * 304.8
                    dist_str = ", {:.0f}mm outside room's plan bbox".format(dist_mm)
                except Exception:
                    dist_str = ""
                diag.append(
                    (
                        marker.Id,
                        "outside this room (via {}) at ({:.2f}, {:.2f}){}".format(
                            method, test_pt.X, test_pt.Y, dist_str
                        ),
                    )
                )
            continue

        z_probe_str = "marker Z={:.2f}ft (via '{}'), room floor band [{:.2f}, {:.2f}]".format(
            test_pt.Z,
            resolved_view_name,
            floor_min_z if floor_min_z is not None else -1,
            floor_max_z if floor_max_z is not None else -1,
        )

        found_any = False
        for i in range(4):
            try:
                # IsAvailableIndex is True when that slot is EMPTY (no
                # view placed there yet) - we want the opposite: slots
                # that already hold a view.
                if marker.IsAvailableIndex(i):
                    continue
                view_id = marker.GetViewId(i)
            except Exception as ex:
                if diag is not None:
                    diag.append((marker.Id, "index {} check failed: {}".format(i, ex)))
                continue
            if view_id is None or view_id == ElementId.InvalidElementId:
                continue
            view = doc.GetElement(view_id)
            if view is None:
                continue
            result.append((view, test_pt, method, marker.Id, z_probe_str))
            found_any = True
            if diag is not None:
                diag.append((marker.Id, "view '{}': inside this room (via {})".format(view.Name, method)))
        if diag is not None and not found_any:
            diag.append((marker.Id, "inside this room but no view found on any of its 4 indices"))
    return result


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class RoomListItem(object):
    def __init__(self, room):
        self.room = room
        try:
            self.name = "{} {}".format(
                room.Number,
                room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString(),
            )
        except Exception:
            self.name = "Room {}".format(room.Id)

    def __str__(self):
        return self.name


class CropFixerWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
        try:
            self.Title = "Elevation Crop Fixer - v{}".format(TOOL_VERSION)
        except Exception:
            pass
        self.cbScope.SelectionChanged += self.on_scope_changed
        self.tbSearch.TextChanged += self.on_search_changed
        self.btnAll.Click += self.on_select_all
        self.btnNone.Click += self.on_select_none
        self.tbTemplateFilter.TextChanged += self.on_template_filter_changed
        self.tbRoomTagFilter.TextChanged += self.on_room_tag_filter_changed
        self.btnRun.Click += self.on_run
        self.btnClose.Click += self.on_close
        self.Closing += self.on_closing
        self.all_items = []
        self.all_templates = []
        self.all_room_tag_types = []
        self.load_templates()
        self.load_room_tag_types()
        self.load_settings()
        self.refresh_rooms()

    @staticmethod
    def template_display_name(tmpl):
        try:
            name = tmpl.Name
        except Exception:
            try:
                name = "Template {}".format(tmpl.Id)
            except Exception:
                return "(unreadable template)"
        try:
            is_elevation = tmpl.ViewType == ViewType.Elevation
        except Exception:
            is_elevation = True
        if is_elevation:
            return name
        try:
            return "{} [{}]".format(name, tmpl.ViewType)
        except Exception:
            return name

    def load_templates(self):
        """Caches every view template in the model (see
        get_elevation_view_templates), then builds the dropdown from
        that cache - apply_template_filter() rebuilds the dropdown's
        items whenever the filter box changes, without re-querying
        Revit each time."""
        self.all_templates = get_elevation_view_templates()
        self.apply_template_filter()

    def apply_template_filter(self):
        from System.Windows.Controls import ComboBoxItem

        term = (self.tbTemplateFilter.Text or "").strip().lower()
        previously_selected = self.selected_template_id()

        self.cbElevTemplate.Items.Clear()
        none_item = ComboBoxItem()
        none_item.Content = "(none)"
        none_item.Tag = None
        self.cbElevTemplate.Items.Add(none_item)

        select_item = none_item
        for tmpl in self.all_templates:
            display = self.template_display_name(tmpl)
            if term and term not in display.lower():
                continue
            item = ComboBoxItem()
            item.Content = display
            item.Tag = tmpl.Id
            self.cbElevTemplate.Items.Add(item)
            if previously_selected is not None and tmpl.Id == previously_selected:
                select_item = item
        self.cbElevTemplate.SelectedItem = select_item

    def on_template_filter_changed(self, sender, args):
        self.apply_template_filter()

    @staticmethod
    def room_tag_display_name(symbol):
        fam_name = None
        type_name = None
        try:
            fam_name = symbol.Family.Name
        except Exception:
            pass
        try:
            type_name = symbol.Name
        except Exception:
            pass
        if fam_name and type_name:
            return "{} : {}".format(fam_name, type_name)
        if type_name:
            return type_name
        if fam_name:
            return fam_name
        try:
            return "Room Tag Type {}".format(symbol.Id)
        except Exception:
            return "(unreadable room tag type)"

    def selected_room_tag_type_id(self):
        item = self.cbRoomTagType.SelectedItem
        return item.Tag if item is not None else None

    def load_room_tag_types(self):
        self.all_room_tag_types = get_room_tag_types()
        self.apply_room_tag_filter()

    def apply_room_tag_filter(self):
        from System.Windows.Controls import ComboBoxItem

        term = (self.tbRoomTagFilter.Text or "").strip().lower()
        previously_selected = self.selected_room_tag_type_id()

        self.cbRoomTagType.Items.Clear()
        none_item = ComboBoxItem()
        none_item.Content = "(none)"
        none_item.Tag = None
        self.cbRoomTagType.Items.Add(none_item)

        select_item = none_item
        for symbol in self.all_room_tag_types:
            display = self.room_tag_display_name(symbol)
            if term and term not in display.lower():
                continue
            item = ComboBoxItem()
            item.Content = display
            item.Tag = symbol.Id
            self.cbRoomTagType.Items.Add(item)
            if previously_selected is not None and symbol.Id == previously_selected:
                select_item = item
        self.cbRoomTagType.SelectedItem = select_item

    def on_room_tag_filter_changed(self, sender, args):
        self.apply_room_tag_filter()

    def selected_template_id(self):
        item = self.cbElevTemplate.SelectedItem
        return item.Tag if item is not None else None

    # --- persisted settings (per-user, via pyRevit config - same
    # mechanism as the Place RLS Views tool) ---
    def load_settings(self):
        cfg = script.get_config()
        try:
            self.txtWallOffset.Text = str(cfg.get_option("wall_offset_mm", "75"))
            self.txtCeilingOffset.Text = str(cfg.get_option("ceiling_offset_mm", "100"))
            if hasattr(self, "txtFloorOffset"):
                self.txtFloorOffset.Text = str(cfg.get_option("floor_offset_mm", "0"))
            self.txtDatumExt.Text = str(cfg.get_option("datum_ext_mm", "100"))
            if hasattr(self, "txtLevelLeftOffset"):
                self.txtLevelLeftOffset.Text = str(cfg.get_option("level_left_offset_mm", "600"))
            if hasattr(self, "txtGridBubbleOffset"):
                self.txtGridBubbleOffset.Text = str(cfg.get_option("grid_bubble_offset_mm", "25"))
            self.txtFarClipOffset.Text = str(cfg.get_option("far_clip_offset_mm", "5000"))
            self.chkSkipModified.IsChecked = bool(cfg.get_option("skip_modified", True))
            self.chkHideLevelBubble.IsChecked = bool(cfg.get_option("hide_level_bubble", True))
            self.chkExtendGrids.IsChecked = bool(cfg.get_option("extend_grids", True))
            self.chkFarClip.IsChecked = bool(cfg.get_option("extend_far_clip", True))
            self.chkOverrideTemplate.IsChecked = bool(cfg.get_option("override_template", False))
            scope_index = cfg.get_option("scope_index", 0)
            try:
                scope_index = int(scope_index)
            except Exception:
                scope_index = 0
            if 0 <= scope_index < self.cbScope.Items.Count:
                self.cbScope.SelectedIndex = scope_index
            template_name = cfg.get_option("elev_template_name", "")
            if template_name:
                for item in self.cbElevTemplate.Items:
                    if item.Content == template_name:
                        self.cbElevTemplate.SelectedItem = item
                        break
            room_tag_name = cfg.get_option("room_tag_type_name", "")
            if room_tag_name:
                for item in self.cbRoomTagType.Items:
                    if item.Content == room_tag_name:
                        self.cbRoomTagType.SelectedItem = item
                        break
        except Exception as ex:
            logger.debug("load_settings failed: {}".format(ex))

    def save_settings(self):
        try:
            cfg = script.get_config()
            cfg.wall_offset_mm = self.txtWallOffset.Text
            cfg.ceiling_offset_mm = self.txtCeilingOffset.Text
            if hasattr(self, "txtFloorOffset"):
                cfg.floor_offset_mm = self.txtFloorOffset.Text
            cfg.datum_ext_mm = self.txtDatumExt.Text
            if hasattr(self, "txtLevelLeftOffset"):
                cfg.level_left_offset_mm = self.txtLevelLeftOffset.Text
            if hasattr(self, "txtGridBubbleOffset"):
                cfg.grid_bubble_offset_mm = self.txtGridBubbleOffset.Text
            cfg.far_clip_offset_mm = self.txtFarClipOffset.Text
            cfg.skip_modified = bool(self.chkSkipModified.IsChecked)
            cfg.hide_level_bubble = bool(self.chkHideLevelBubble.IsChecked)
            cfg.extend_grids = bool(self.chkExtendGrids.IsChecked)
            cfg.extend_far_clip = bool(self.chkFarClip.IsChecked)
            cfg.override_template = bool(self.chkOverrideTemplate.IsChecked)
            cfg.scope_index = self.cbScope.SelectedIndex
            selected = self.cbElevTemplate.SelectedItem
            cfg.elev_template_name = selected.Content if selected is not None else ""
            selected_tag = self.cbRoomTagType.SelectedItem
            cfg.room_tag_type_name = selected_tag.Content if selected_tag is not None else ""
            script.save_config()
        except Exception as ex:
            logger.debug("save_settings failed: {}".format(ex))

    def on_closing(self, sender, args):
        self.save_settings()

    def current_scope(self):
        item = self.cbScope.SelectedItem
        return item.Content if item is not None else "Active view"

    def refresh_rooms(self):
        rooms = get_all_rooms(self.current_scope())
        self.all_items = sorted([RoomListItem(r) for r in rooms], key=lambda i: i.name)
        self.apply_filter()

    def apply_filter(self):
        term = (self.tbSearch.Text or "").strip().lower()
        self.lstRooms.Items.Clear()
        from System.Windows.Controls import CheckBox

        for item in self.all_items:
            if term and term not in item.name.lower():
                continue
            cb = CheckBox()
            cb.Content = item.name
            cb.Tag = item
            cb.Foreground = System.Windows.Media.Brushes.White
            cb.Margin = System.Windows.Thickness(2)
            cb.Checked += self.on_room_check_changed
            cb.Unchecked += self.on_room_check_changed
            self.lstRooms.Items.Add(cb)
        self.lblCount.Text = "{} room(s)".format(self.lstRooms.Items.Count)
        self.update_selected_count()

    def update_selected_count(self):
        if not hasattr(self, "lblSelectedCount"):
            return
        try:
            selected = sum(1 for item in self.lstRooms.Items if item.IsChecked)
            self.lblSelectedCount.Text = "{} selected".format(selected)
        except Exception:
            pass

    def on_room_check_changed(self, sender, args):
        self.update_selected_count()

    def on_scope_changed(self, sender, args):
        self.refresh_rooms()

    def on_search_changed(self, sender, args):
        self.apply_filter()

    def on_select_all(self, sender, args):
        for item in self.lstRooms.Items:
            item.IsChecked = True
        self.update_selected_count()

    def on_select_none(self, sender, args):
        for item in self.lstRooms.Items:
            item.IsChecked = False
        self.update_selected_count()

    def on_close(self, sender, args):
        self.Close()

    def get_selected_rooms(self):
        return [cb.Tag.room for cb in self.lstRooms.Items if cb.IsChecked]

    def on_run(self, sender, args):
        # (v1.6.0) If the user pre-selected an ElevationMarker in Revit
        # before clicking Run, sidestep the whole room-search heuristic
        # and just deep-dive that one marker: test its get_BoundingBox()
        # against every view in the document and report where (if
        # anywhere) it resolves. This is a diagnostic-only path - it
        # never touches the model and returns before any room processing.
        try:
            sel_ids = list(uidoc.Selection.GetElementIds())
        except Exception:
            sel_ids = []
        selected_markers = []
        for eid in sel_ids:
            el = doc.GetElement(eid)
            if isinstance(el, ElevationMarker):
                selected_markers.append(el)

        if selected_markers:
            for marker in selected_markers:
                rows = diagnose_single_marker(marker)
                output.print_md(
                    "## Elevation Crop Fixer v{} - single marker diagnostic (Id {})".format(
                        TOOL_VERSION, marker.Id
                    )
                )
                output.print_table(
                    table_data=[[str(a), str(b)] for a, b in rows],
                    columns=["Field", "Value"],
                )
            forms.alert(
                "Marker diagnostic complete for {} selected marker(s). See the "
                "pyRevit output window. Deselect the marker(s) and select rooms "
                "instead to run the normal crop-fixing pass.".format(len(selected_markers))
            )
            return

        rooms = self.get_selected_rooms()
        if not rooms:
            forms.alert("Select at least one room first.")
            return

        try:
            wall_offset_ft = mm_to_ft(float(self.txtWallOffset.Text))
            ceiling_offset_ft = mm_to_ft(float(self.txtCeilingOffset.Text))
            # txtFloorOffset is only present once ui.xaml has been updated
            # alongside this script - fall back to "0" (flush to floor,
            # same as the old un-recomputed behaviour's typical result)
            # rather than crashing if only script.py was swapped in.
            if hasattr(self, "txtFloorOffset"):
                floor_offset_ft = mm_to_ft(float(self.txtFloorOffset.Text))
            else:
                floor_offset_ft = 0.0
            datum_ext_ft = mm_to_ft(float(self.txtDatumExt.Text))
            if hasattr(self, "txtGridBubbleOffset"):
                grid_bubble_offset_ft = mm_to_ft(float(self.txtGridBubbleOffset.Text))
            else:
                grid_bubble_offset_ft = mm_to_ft(25.0)
            if hasattr(self, "txtLevelLeftOffset"):
                level_left_offset_ft = mm_to_ft(float(self.txtLevelLeftOffset.Text))
            else:
                level_left_offset_ft = mm_to_ft(600.0)
            far_clip_offset_ft = mm_to_ft(float(self.txtFarClipOffset.Text))
        except ValueError:
            forms.alert("Offsets must be numbers (millimetres).")
            return

        self.save_settings()

        skip_modified = bool(self.chkSkipModified.IsChecked)
        hide_bubble = bool(self.chkHideLevelBubble.IsChecked)
        extend_grids = bool(self.chkExtendGrids.IsChecked)
        extend_far_clip = bool(self.chkFarClip.IsChecked)
        override_template = bool(self.chkOverrideTemplate.IsChecked)
        template_id = self.selected_template_id()
        room_tag_type_id = self.selected_room_tag_type_id()

        results = []  # (room_name, view_name, status, message)
        tg = TransactionGroup(doc, "Elevation Crop Fixer")
        tg.Start()
        try:
            t = Transaction(doc, "Fix elevation crops")
            t.Start()

            # gather (view, room, marker_location) triples first so the
            # progress bar is accurate. Must happen inside the transaction:
            # get_marker_location() temporarily toggles Crop Region
            # Visible on each marker's views to get an accurate reading.
            jobs = []
            diag = []
            zero_match_rooms = []  # (room_name, this room's own diag slice)
            for room in rooms:
                room_name = RoomListItem(room).name
                diag.append(("=== {} ===".format(room_name), ""))
                room_diag_start = len(diag)
                jobs_before = len(jobs)
                for view, marker_loc, match_method, marker_id, z_probe_str in get_elevation_views_for_room(room, diag):
                    jobs.append((view, room, marker_loc, match_method, marker_id, z_probe_str))
                if len(jobs) == jobs_before:
                    # A room with zero matches used to just silently vanish
                    # from the results table with no explanation at all -
                    # the "no elevations matched" diagnostic dump only ever
                    # fired when EVERY selected room came back empty, so a
                    # room that failed alongside others that succeeded left
                    # no trace anywhere. Keep this room's own diag slice so
                    # it can be reported explicitly below.
                    zero_match_rooms.append((room_name, diag[room_diag_start:]))

            if zero_match_rooms:
                for zr_name, zr_diag in zero_match_rooms:
                    output.print_md("### No elevations matched for room: {}".format(zr_name))
                    output.print_table(
                        table_data=[[str(a), b] for a, b in zr_diag],
                        columns=["Marker / room", "What happened"],
                    )

            if not jobs:
                t.RollBack()
                tg.RollBack()
                output.print_md("## Elevation Crop Fixer - diagnostics")
                output.print_md(
                    "No elevations matched. Details below (one line per marker "
                    "found in the model):"
                )
                output.print_table(
                    table_data=[[str(a), b] for a, b in diag],
                    columns=["Marker / room", "What happened"],
                )
                forms.alert(
                    "No interior elevations found for the selected rooms. "
                    "See the pyRevit output window for details."
                )
                return

            self.pb.Maximum = len(jobs)
            self.pb.Value = 0

            for view, room, marker_loc, match_method, marker_id, z_probe_str in jobs:
                room_name = RoomListItem(room).name
                try:
                    status, msg = process_view(
                        view,
                        room,
                        wall_offset_ft,
                        ceiling_offset_ft,
                        floor_offset_ft,
                        datum_ext_ft,
                        skip_modified,
                        hide_bubble if hide_bubble else None,
                        extend_grids,
                        grid_bubble_offset_ft,
                        marker_loc,
                        template_id,
                        override_template,
                        extend_far_clip,
                        far_clip_offset_ft,
                        room_tag_type_id,
                        level_left_offset_ft,
                    )
                except Exception as ex:
                    status, msg = "error", str(ex)
                    logger.debug("process_view failed: {}".format(ex))
                match_diag = "ROOM MATCH: marker #{} @ ({:.2f},{:.2f}) matched via {}".format(
                    marker_id, marker_loc.X, marker_loc.Y, match_method
                )
                # The actual status reason goes FIRST and the room-match
                # diagnostic LAST: the pyRevit output table appears to
                # silently cut a cell off past some length rather than
                # wrapping or showing an ellipsis, and the status reason
                # matters far more than the room-match confirmation once
                # a run is otherwise working.
                msg = msg + " || " + match_diag + " || " + z_probe_str
                results.append((room_name, view.Name, status, msg))
                self.pb.Value += 1
            for zr_name, _ in zero_match_rooms:
                results.append(
                    (
                        zr_name,
                        "(none)",
                        "flagged",
                        "no elevation markers matched this room at all - see the "
                        "'No elevations matched for room' detail printed above the "
                        "results table",
                    )
                )
            t.Commit()
            tg.Assimilate()
        except Exception:
            tg.RollBack()
            raise

        self.print_report(results)

    STATUS_COLORS = {
        "adjusted": "#66BB6A",  # green - done, no action needed
        "flagged": "#FFB74D",  # amber - needs a look
        "skipped": "#4DB6AC",  # teal - deliberately left alone
        "error": "#EF5350",  # red - failed
    }

    @classmethod
    def _color_status(cls, status):
        color = cls.STATUS_COLORS.get(status, "#FFFFFF")
        return "<span style='color:{}; font-weight:bold'>{}</span>".format(color, status.upper())

    @staticmethod
    def _highlight_notes(msg):
        # Draw the eye to FAILED / [note: ...] fragments within the long
        # diagnostic message, the same way status is coloured, without
        # needing to parse or restructure the message itself.
        msg = msg.replace(
            "FAILED", "<span style='color:#EF5350; font-weight:bold'>FAILED</span>"
        )
        msg = re.sub(
            r"\[note:.*?\]",
            lambda m: "<span style='color:#FFB74D'>{}</span>".format(m.group(0)),
            msg,
        )
        return msg

    def print_report(self, results):
        output.print_md("## Elevation Crop Fixer v{} - results".format(TOOL_VERSION))
        counts = {}
        for _, _, status, _ in results:
            counts[status] = counts.get(status, 0) + 1
        summary = ", ".join(
            "<span style='color:{}'>{}: {}</span>".format(
                self.STATUS_COLORS.get(k, "#FFFFFF"), k, v
            )
            for k, v in counts.items()
        )
        summary_line = "<b>{}</b> views processed - {}".format(len(results), summary)
        try:
            output.print_html(summary_line)
        except Exception:
            output.print_md(summary_line)

        rows = [
            [r, v, self._color_status(s), self._highlight_notes(m)]
            for r, v, s, m in results
        ]
        output.print_table(
            table_data=rows,
            title="Details",
            columns=["Room", "View", "Status", "Notes"],
        )

        # The table cell above visibly clips long Notes text after a
        # handful of wrapped lines (no ellipsis, just silently gone) -
        # pyRevit's own output styling, not something this script
        # controls. So anything worth reading in full (flagged/error)
        # also gets printed again below as its own block, outside the
        # table, where it can't get cut off.
        needs_detail = [(r, v, s, m) for r, v, s, m in results if s in ("flagged", "error")]
        if needs_detail:
            output.print_md("### Flagged / error detail (full text, not clipped)")
            for r, v, s, m in needs_detail:
                output.print_md(
                    "**{} — {}** ({})\n\n```\n{}\n```".format(r, v, s.upper(), m)
                )


if __name__ == "__main__":
    CropFixerWindow(XAML_PATH).ShowDialog()
