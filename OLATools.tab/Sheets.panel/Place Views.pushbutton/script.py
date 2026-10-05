# -*- coding: utf-8 -*-
"""Place a plan view on one sheet, and its 4 interior elevation views
(generated from an ElevationMarker inside that plan) on a second sheet
in a configurable grid layout.

Usage:
    Drop this file as script.py inside a pyRevit pushbutton folder
    (e.g. MyTools.tab/Sheets.panel/PlaceViews.pushbutton/script.py),
    reload pyRevit, and click it. All inputs are gathered from the
    dialog window that appears - nothing to edit in this file.
"""

import re
import json

from pyrevit import revit, DB
from pyrevit import script
from pyrevit import forms

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("System.Windows.Forms")
import System
from System.Windows.Forms import FolderBrowserDialog, DialogResult
from System.Windows import Visibility

doc = revit.doc
output = script.get_output()
config = script.get_config()

SAVED_SETS_CONFIG_KEY = "saved_plan_view_sets"
LAST_TITLE_BLOCK_KEY = "last_title_block_name"
LAST_OFFSET_KEY = "last_offset_mm"
LAST_H_OFFSET_KEY = "last_h_offset_mm"
LAST_START_NUMBER_KEY = "last_start_sheet_number"
LAST_LAYOUT_KEY = "last_layout_index"
LAST_MARGIN_KEY = "last_margin_mm"
LAST_VIEWPORT_TYPE_KEY = "last_viewport_type_name"
LAST_PLAN_TITLE_KEY = "last_plan_title_suffix"
LAST_ELEV_TITLE_KEY = "last_elev_title_suffix"
LAST_TITLE_CHAR_LIMIT_KEY = "last_title_char_limit"
LAST_TEMP_PREFIX_KEY = "last_temp_prefix"
LAST_SHEET_NAME_KEY = "last_sheet_name"
LAST_PLAN_TEMPLATE_KEY = "last_plan_template_name"
LAST_ELEV_TEMPLATE_KEY = "last_elev_template_name"

TOOL_VERSION = "1.0.1"

# ---- DEFAULTS (shown pre-filled in the dialog, editable there) ----
GAP = 0.05  # feet between viewports in the grid (~0.6")
DEFAULT_ELEV_GRID_VERTICAL_OFFSET_MM = 30
DEFAULT_ELEV_GRID_HORIZONTAL_OFFSET_MM = 0
DEFAULT_TITLE_BLOCK_MARGIN_MM = 30  # reserved border/label space around the title block edge, per side
DEFAULT_PLAN_TITLE_SUFFIX = "PLAN"
DEFAULT_ELEV_TITLE_SUFFIX = "ELEVATIONS"
DEFAULT_TITLE_CHAR_LIMIT = 50
DEFAULT_TEMP_PREFIX = "TEMP"
DEFAULT_SHEET_NAME = "SHEET NAME"
MM_PER_FOOT = 304.8
DEFAULT_START_SHEET_NUMBER = "A-100"
NONE_SET_LABEL = "<none>"
LAYOUT_OPTIONS = ["Auto (best fit)", "2x2 Grid", "1x4 Horizontal (row)", "4x1 Vertical (column)"]
LAYOUT_CODES = ["auto", "2x2", "1x4", "4x1"]
# --------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------

def viewport_type_display_name(t):
    """Get a meaningful display name for a Viewport type. Viewport types are
    system family types in Revit - they don't expose a Family property like
    loadable families do. We try several approaches to get a useful name."""
    # Try the built-in SYMBOL_NAME_PARAM (type name in the family)
    for param_id in (DB.BuiltInParameter.SYMBOL_NAME_PARAM,
                     DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,
                     DB.BuiltInParameter.DATUM_TEXT):
        try:
            p = t.get_Parameter(param_id)
            if p is not None and p.HasValue and p.AsString():
                name = p.AsString().strip()
                if name:
                    return name
        except Exception:
            pass

    # Fall back to t.Name which may be set even when the above aren't
    try:
        name = t.Name
        if name and name.strip():
            return name.strip()
    except Exception:
        pass

    return "Viewport Type [id:{}]".format(t.Id.IntegerValue)


def get_selectable_viewport_types():
    """All Viewport types available in the project (e.g. 'No Title', 'With
    Line'), sorted by name. Note: Revit only creates named Viewport types
    once at least one viewport has actually been placed and typed in the
    project - an empty result here just means none exist yet."""
    found = {}

    try:
        types = DB.FilteredElementCollector(doc)\
            .OfCategory(DB.BuiltInCategory.OST_Viewports)\
            .WhereElementIsElementType()\
            .ToElements()
        for t in types:
            found[t.Id.IntegerValue] = t
    except Exception:
        pass

    try:
        instances = DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements()
        for vp in instances:
            type_id = vp.GetTypeId()
            if type_id != DB.ElementId.InvalidElementId and type_id.IntegerValue not in found:
                t = doc.GetElement(type_id)
                if t is not None:
                    found[type_id.IntegerValue] = t
    except Exception:
        pass

    result = list(found.values())
    result.sort(key=viewport_type_display_name)
    return result


def default_viewport_type_index(viewport_types):
    """Prefer a type with 'no title' in its name (common for compact
    elevation grids); otherwise default to the first type."""
    for i, t in enumerate(viewport_types):
        try:
            if "no title" in t.Name.lower():
                return i
        except Exception:
            pass
    return 0 if viewport_types else -1


def get_selectable_title_blocks():
    """All title block types loaded in the project, sorted by display name."""
    symbols = DB.FilteredElementCollector(doc)\
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)\
        .WhereElementIsElementType()\
        .ToElements()
    result = list(symbols)
    result.sort(key=lambda tb: title_block_display_name(tb))
    return result


def title_block_display_name(tb):
    try:
        family_name = tb.Family.Name if tb.Family is not None else "Unknown Family"
    except Exception:
        family_name = "Unknown Family"
    try:
        type_name = tb.Name if tb.Name is not None else "Unknown Type"
    except Exception:
        type_name = "Unknown Type"
    return "{}: {}".format(family_name, type_name)


def get_selectable_plan_views():
    """All non-template floor plan views, sorted by name."""
    plans = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
    result = [
        v for v in plans
        if not v.IsTemplate and v.ViewType == DB.ViewType.FloorPlan
    ]
    result.sort(key=lambda v: v.Name)
    return result


def load_saved_view_sets():
    """Return {set_name: [{"id": int, "name": str}, ...]} loaded from persistent script config.
    Automatically migrates old-format {set_name: [str, ...]} to the new format on first load."""
    raw = config.get_option(SAVED_SETS_CONFIG_KEY, "{}")
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    # Migrate old format (list of strings) to new format (list of {id, name} dicts)
    # Do migration in-memory only; caller must call save_view_sets() to persist
    migrated = False
    plan_views = get_selectable_plan_views()
    for set_name, entries in list(data.items()):
        if entries and isinstance(entries[0], basestring):
            data[set_name] = migrate_set_entries_to_id_format(entries, plan_views)
            migrated = True
    if migrated:
        save_view_sets(data)
    return data


def save_view_sets(sets_dict):
    config.set_option(SAVED_SETS_CONFIG_KEY, json.dumps(sets_dict))
    script.save_config()


def migrate_set_entries_to_id_format(raw_names_list, all_plan_views):
    """Convert old-format [str, ...] to new-format [{"id": int, "name": str}, ...]."""
    name_to_view = {v.Name.strip().lower(): v for v in all_plan_views}
    result = []
    for name in raw_names_list:
        v = name_to_view.get(name.strip().lower())
        entry = {"id": int(v.Id.IntegerValue) if v is not None else -1, "name": name}
        result.append(entry)
    return result


def resolve_saved_set(set_entries, all_plan_views):
    """Resolve a list of {"id": int, "name": str} entries to View objects.
    Tries ElementId first, falls back to name match.
    Returns (resolved_views, corrected_names, missing_names) where:
      resolved_views  = [View, ...] in order
      corrected_names = [(old_stored_name, current_view_name), ...] where name was stale
      missing_names   = [stored_name, ...] that could not be resolved at all"""
    id_to_view = {}
    name_to_view = {}
    for v in all_plan_views:
        try:
            id_to_view[int(v.Id.IntegerValue)] = v
        except Exception:
            pass
        name_to_view[v.Name.strip().lower()] = v

    resolved_views = []
    corrected_names = []
    missing_names = []

    for entry in set_entries:
        stored_id = entry.get("id", -1)
        stored_name = entry.get("name", "")
        found = None

        # Try by ElementId first
        if stored_id != -1:
            found = id_to_view.get(int(stored_id))

        # Fall back to name match
        if found is None:
            found = name_to_view.get(stored_name.strip().lower())

        if found is None:
            missing_names.append(stored_name)
        else:
            resolved_views.append(found)
            if found.Name != stored_name:
                corrected_names.append((stored_name, found.Name))

    return resolved_views, corrected_names, missing_names


# (pref_name -> (config_key, default, is_int)) - single source of truth for
# load_last_used_prefs/save_last_used_prefs, so adding a new remembered
# field is one line instead of touching multiple positional-arg call sites.
PREF_FIELDS = [
    ("title_block_name", LAST_TITLE_BLOCK_KEY, "", False),
    ("offset_mm", LAST_OFFSET_KEY, str(DEFAULT_ELEV_GRID_VERTICAL_OFFSET_MM), False),
    ("h_offset_mm", LAST_H_OFFSET_KEY, str(DEFAULT_ELEV_GRID_HORIZONTAL_OFFSET_MM), False),
    ("start_number", LAST_START_NUMBER_KEY, DEFAULT_START_SHEET_NUMBER, False),
    ("layout_index", LAST_LAYOUT_KEY, "0", True),
    ("margin_mm", LAST_MARGIN_KEY, str(DEFAULT_TITLE_BLOCK_MARGIN_MM), False),
    ("viewport_type_name", LAST_VIEWPORT_TYPE_KEY, "", False),
    ("plan_title_suffix", LAST_PLAN_TITLE_KEY, DEFAULT_PLAN_TITLE_SUFFIX, False),
    ("elev_title_suffix", LAST_ELEV_TITLE_KEY, DEFAULT_ELEV_TITLE_SUFFIX, False),
    ("title_char_limit", LAST_TITLE_CHAR_LIMIT_KEY, str(DEFAULT_TITLE_CHAR_LIMIT), False),
    ("sheet_name", LAST_SHEET_NAME_KEY, DEFAULT_SHEET_NAME, False),
    ("temp_prefix", LAST_TEMP_PREFIX_KEY, DEFAULT_TEMP_PREFIX, False),
    ("plan_template_name", LAST_PLAN_TEMPLATE_KEY, "", False),
    ("elev_template_name", LAST_ELEV_TEMPLATE_KEY, "", False),
]


def load_last_used_prefs():
    """Return a dict of last-used dialog values, with sensible fallbacks."""
    result = {}
    for name, key, default, is_int in PREF_FIELDS:
        value = config.get_option(key, default)
        result[name] = int(value) if is_int else value
    return result


def save_last_used_prefs(prefs):
    """Persist a dict of dialog values (same keys as load_last_used_prefs)."""
    for name, key, default, is_int in PREF_FIELDS:
        if name in prefs:
            config.set_option(key, str(prefs[name]))
    script.save_config()


AUDIT_LOG_FOLDERS_CONFIG_KEY = "audit_log_folders_by_project"


def get_current_project_key():
    """A stable identifier for the current Revit project, used to remember
    per-project settings (like the audit log folder) separately. Falls back
    to the document title if the project hasn't been saved to disk yet."""
    try:
        if doc.PathName:
            return doc.PathName
    except Exception:
        pass
    try:
        return doc.Title
    except Exception:
        return "unknown_project"


def load_last_audit_log_folder():
    """The audit log folder last used for THIS project, or '' if none."""
    raw = config.get_option(AUDIT_LOG_FOLDERS_CONFIG_KEY, "{}")
    try:
        folders_by_project = json.loads(raw)
    except Exception:
        folders_by_project = {}
    return folders_by_project.get(get_current_project_key(), "")


def save_last_audit_log_folder(folder_path):
    """Remember folder_path as this project's audit log folder for next time."""
    raw = config.get_option(AUDIT_LOG_FOLDERS_CONFIG_KEY, "{}")
    try:
        folders_by_project = json.loads(raw)
    except Exception:
        folders_by_project = {}
    folders_by_project[get_current_project_key()] = folder_path
    config.set_option(AUDIT_LOG_FOLDERS_CONFIG_KEY, json.dumps(folders_by_project))
    script.save_config()


# ---------------------------------------------------------------------------
# Core geometry / Revit-data logic
# ---------------------------------------------------------------------------

def marker_has_views(marker):
    for i in range(4):
        if marker.GetViewId(i) != DB.ElementId.InvalidElementId:
            return True
    return False


def get_marker_point(marker, view):
    """Return the marker's approximate model-space location as an XYZ,
    using its bounding box center in the given view (ElevationMarker has
    no Location/Position, and its model-space bbox can be None)."""
    bbox = marker.get_BoundingBox(view)
    if bbox is None:
        return None
    return DB.XYZ(
        (bbox.Min.X + bbox.Max.X) / 2.0,
        (bbox.Min.Y + bbox.Max.Y) / 2.0,
        (bbox.Min.Z + bbox.Max.Z) / 2.0,
    )


def marker_inside_plan_crop(marker, plan_view):
    point = get_marker_point(marker, plan_view)
    if point is None:
        return False

    bbox = plan_view.CropBox
    if bbox is None:
        return True  # no crop set, accept as fallback

    tf = bbox.Transform
    p = tf.Inverse.OfPoint(point)

    return (bbox.Min.X <= p.X <= bbox.Max.X) and (bbox.Min.Y <= p.Y <= bbox.Max.Y)


def find_elevation_marker_for_plan(plan_view, verbose=True):
    """Find the ElevationMarker whose location falls inside this plan view's
    crop region. If more than one qualifying marker is found, the first
    (by element id order) is used and a warning is printed."""
    markers = DB.FilteredElementCollector(doc).OfClass(DB.ElevationMarker).ToElements()

    log_lines = []
    log_lines.append("Plan view = '{}' (id {}), crop box = {}".format(
        plan_view.Name, plan_view.Id, "None" if plan_view.CropBox is None else "present"))
    log_lines.append("Found {} elevation marker(s) in the model.".format(len(markers)))

    qualifying = []
    for m in markers:
        has_views = marker_has_views(m)
        point = get_marker_point(m, plan_view)
        loc_str = "n/a" if point is None else "({:.2f}, {:.2f}, {:.2f})".format(point.X, point.Y, point.Z)
        inside = marker_inside_plan_crop(m, plan_view)
        log_lines.append("- Marker id {}: has_views={}, location={}, inside_crop={}".format(
            m.Id, has_views, loc_str, inside))
        if has_views and inside:
            qualifying.append(m)

    log_text = "\n".join(log_lines)

    log_path = r"C:\Temp\pyrevit_placeviews_debug_{}.txt".format(plan_view.Id)
    try:
        import os
        if not os.path.isdir(r"C:\Temp"):
            os.makedirs(r"C:\Temp")
        with open(log_path, "w") as f:
            f.write(log_text)
    except Exception as e:
        if verbose:
            output.print_md("**Warning:** could not write debug log file: {}".format(e))

    if not qualifying:
        return None

    if len(qualifying) > 1 and verbose:
        ids = ", ".join(str(m.Id) for m in qualifying)
        output.print_md("**Warning for '{}':** {} elevation markers qualify inside this plan's crop "
                         "(ids: {}). Using the first one found - if that's wrong, this plan's crop "
                         "may overlap another room's marker.".format(plan_view.Name, len(qualifying), ids))

    return qualifying[0]


def get_elevation_views(marker):
    views = []
    for i in range(4):
        vid = marker.GetViewId(i)
        if vid != DB.ElementId.InvalidElementId:
            v = doc.GetElement(vid)
            if v:
                views.append(v)
    return views


def expected_plan_view_name(base_name, number):
    """The correct Revit view name for a plan view:
    '{Room Number} {Room Name} PLAN'."""
    if number:
        return "{} {} PLAN".format(number, base_name)
    return "{} PLAN".format(base_name)


def expected_elevation_view_name(base_name, number, index_1based):
    """The correct Revit view name for an interior elevation view:
    '{Room Number} {Room Name} ELEVATION {N}'."""
    if number:
        return "{} {} ELEVATION {}".format(number, base_name, index_1based)
    return "{} ELEVATION {}".format(base_name, index_1based)


def sync_view_names(plan_view, elevation_views, base_name, number):
    """Rename plan view and its elevation views to match the naming convention
    if they don't already match. Returns a list of (old_name, new_name) pairs
    for any views that were actually renamed."""
    renames = []

    expected_plan = expected_plan_view_name(base_name, number)
    if plan_view.Name != expected_plan:
        old = plan_view.Name
        try:
            plan_view.Name = expected_plan
            renames.append((old, expected_plan))
            output.print_md("**Renamed plan view:** '{}' -> '{}'".format(old, expected_plan))
        except Exception as e:
            output.print_md("**Warning:** could not rename plan view '{}': {}".format(old, e))

    for idx, v in enumerate(elevation_views):
        expected_elev = expected_elevation_view_name(base_name, number, idx + 1)
        if v.Name != expected_elev:
            old = v.Name
            try:
                v.Name = expected_elev
                renames.append((old, expected_elev))
                output.print_md("**Renamed elevation view:** '{}' -> '{}'".format(old, expected_elev))
            except Exception as e:
                output.print_md("**Warning:** could not rename elevation view '{}': {}".format(old, e))

    return renames


def is_sheet_empty(sheet):
    """True if a sheet contains nothing but its own title block - no
    viewports, schedules, legends, text, revision clouds, or anything else."""
    elems = DB.FilteredElementCollector(doc, sheet.Id).WhereElementIsNotElementType().ToElements()
    for e in elems:
        try:
            cat = e.Category
        except Exception:
            cat = None
        if cat is not None and cat.Id.IntegerValue == int(DB.BuiltInCategory.OST_TitleBlocks):
            continue  # the title block itself doesn't count
        return False
    return True


def get_selectable_view_templates():
    """All view templates in the project that could be applied to plan or
    elevation views, sorted by name. Returns a list of View elements that
    have IsTemplate=True."""
    all_views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    templates = [v for v in all_views if v.IsTemplate]
    templates.sort(key=lambda v: v.Name)
    return templates


def apply_view_template(view, template_id, log_label):
    """Apply a view template to a view by ElementId. No-op if template_id
    is None or InvalidElementId. Returns True if applied, False otherwise."""
    if template_id is None or template_id == DB.ElementId.InvalidElementId:
        return False
    try:
        if view.ViewTemplateId != template_id:
            view.ViewTemplateId = template_id
            output.print_md("**Applied template to '{}':** {}".format(
                log_label, doc.GetElement(template_id).Name))
        return True
    except Exception as e:
        output.print_md("**Warning:** could not apply view template to '{}': {}".format(log_label, e))
        return False


def apply_view_templates_to_room(plan_view, elevation_views, plan_template_id, elev_template_id):
    """Apply view templates to the plan view and all its elevation views.
    Each template_id may be None/InvalidElementId to skip that view type."""
    apply_view_template(plan_view, plan_template_id, plan_view.Name)
    for v in elevation_views:
        apply_view_template(v, elev_template_id, v.Name)


def apply_viewport_type_to_placed_views(elevation_views, elev_sheets_list, viewport_type_id):
    """Update the Viewport type on any already-placed elevation viewports.
    Finds each elevation view's Viewport element on its sheet and calls
    ChangeTypeId if the type differs. No-op if viewport_type_id is None or
    InvalidElementId."""
    if viewport_type_id is None or viewport_type_id == DB.ElementId.InvalidElementId:
        return
    # Build a map of ViewId -> Viewport for elevation sheets
    sheet_ids = [s.Id for s in elev_sheets_list]
    for sheet_id in sheet_ids:
        vps = DB.FilteredElementCollector(doc, sheet_id).OfClass(DB.Viewport).ToElements()
        for vp in vps:
            if vp.ViewId in [v.Id for v in elevation_views]:
                try:
                    if vp.GetTypeId() != viewport_type_id:
                        vp.ChangeTypeId(viewport_type_id)
                        output.print_md("**Updated viewport type** on sheet {} for view '{}'".format(
                            doc.GetElement(sheet_id).SheetNumber,
                            doc.GetElement(vp.ViewId).Name))
                except Exception as e:
                    output.print_md("**Warning:** could not update viewport type: {}".format(e))


def get_existing_sheet_numbers_map():
    """{sheet_number.lower(): ViewSheet} for every sheet currently in the project."""
    m = {}
    for s in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet).ToElements():
        m[s.SheetNumber.lower()] = s
    return m


def get_sheet_center(sheet):
    title_block_instances = DB.FilteredElementCollector(doc, sheet.Id)\
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)\
        .WhereElementIsNotElementType()\
        .ToElements()

    if title_block_instances:
        bbox = title_block_instances[0].get_BoundingBox(sheet)
        if bbox:
            return DB.XYZ(
                (bbox.Min.X + bbox.Max.X) / 2.0,
                (bbox.Min.Y + bbox.Max.Y) / 2.0,
                0,
            )

    return DB.XYZ(1.0, 0.75, 0)  # rough A3-sized fallback


def get_title_block_interior_size(sheet, margin_ft):
    """Return (width, height) in feet of the usable area inside the sheet's
    title block (its bounding box minus margin_ft on each side), or None if
    no title block instance is found."""
    title_block_instances = DB.FilteredElementCollector(doc, sheet.Id)\
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)\
        .WhereElementIsNotElementType()\
        .ToElements()
    if not title_block_instances:
        return None
    bbox = title_block_instances[0].get_BoundingBox(sheet)
    if not bbox:
        return None
    width = (bbox.Max.X - bbox.Min.X) - 2 * margin_ft
    height = (bbox.Max.Y - bbox.Min.Y) - 2 * margin_ft
    return (max(width, 0), max(height, 0))


def compute_layout_footprint(sizes, layout_code):
    """Pure computation (no Revit calls): given a list of (width, height)
    sizes, return (offsets, needed_width, needed_height) for the given
    layout_code ('2x2', '1x4', or '4x1')."""
    n = len(sizes)
    if layout_code == "1x4":
        widths = [s[0] for s in sizes]
        needed_h = max(s[1] for s in sizes)
        needed_w = sum(widths) + (n - 1) * GAP
        offsets = []
        x = -needed_w / 2.0
        for w in widths:
            offsets.append(DB.XYZ(x + w / 2.0, 0, 0))
            x += w + GAP
        return offsets, needed_w, needed_h
    elif layout_code == "4x1":
        heights = [s[1] for s in sizes]
        needed_w = max(s[0] for s in sizes)
        needed_h = sum(heights) + (n - 1) * GAP
        offsets = []
        y = needed_h / 2.0
        for h in heights:
            offsets.append(DB.XYZ(0, y - h / 2.0, 0))
            y -= h + GAP
        return offsets, needed_w, needed_h
    else:  # "2x2" default
        return pack_2x2_offsets(sizes)


def choose_best_layout(sizes, interior=None):
    """For 'Auto' mode: try 2x2/1x4/4x1 and pick the best. If interior
    (interior_w, interior_h) is given, prefer the first candidate that
    actually fits; if none fit, fall back to whichever overflows least.
    Without an interior to check against, just picks the smallest area.
    Returns (chosen_code, offsets, needed_w, needed_h)."""
    candidates = []
    for code in ("2x2", "1x4", "4x1"):
        offsets, needed_w, needed_h = compute_layout_footprint(sizes, code)
        candidates.append((code, offsets, needed_w, needed_h))

    if interior is not None:
        interior_w, interior_h = interior
        for code, offsets, needed_w, needed_h in candidates:
            if needed_w <= interior_w and needed_h <= interior_h:
                return code, offsets, needed_w, needed_h
        # nothing fits - pick whichever overflows the least
        def overflow_amount(c):
            _, _, w, h = c
            return max(w - interior_w, 0) + max(h - interior_h, 0)
        best = min(candidates, key=overflow_amount)
        return best
    else:
        best = min(candidates, key=lambda c: c[2] * c[3])
        return best


def pack_2x2_offsets(sizes):
    """Pack up to 4 (width, height) sizes into a 2-column grid where each
    column/row is sized to its own contents (not a uniform cell size), so a
    single oversized view doesn't waste space in the other cells. Returns
    (offsets, total_width, total_height)."""
    n = len(sizes)
    rows = [list(range(i, min(i + 2, n))) for i in range(0, n, 2)]
    num_cols = max(len(r) for r in rows)

    col_w = [0] * num_cols
    row_h = [0] * len(rows)
    for r_idx, row in enumerate(rows):
        for c_idx, item_idx in enumerate(row):
            w, h = sizes[item_idx]
            col_w[c_idx] = max(col_w[c_idx], w)
            row_h[r_idx] = max(row_h[r_idx], h)

    total_w = sum(col_w) + (num_cols - 1) * GAP
    total_h = sum(row_h) + (len(rows) - 1) * GAP

    col_x_centers = []
    x = -total_w / 2.0
    for w in col_w:
        col_x_centers.append(x + w / 2.0)
        x += w + GAP

    row_y_centers = []
    y = total_h / 2.0
    for h in row_h:
        row_y_centers.append(y - h / 2.0)
        y -= h + GAP

    offsets = [None] * n
    for r_idx, row in enumerate(rows):
        for c_idx, item_idx in enumerate(row):
            offsets[item_idx] = DB.XYZ(col_x_centers[c_idx], row_y_centers[r_idx], 0)

    return offsets, total_w, total_h


def create_viewports(sheet, views, center, viewport_type_id=None):
    """Create viewports for each view at center (no arrangement). If
    viewport_type_id is given, applies that Viewport type to each one
    (e.g. a 'No Title' type, to reduce the footprint used in fit checks).
    Returns the list of created Viewport elements."""
    placed = []
    for v in views:
        if DB.Viewport.CanAddViewToSheet(doc, sheet.Id, v.Id):
            vp = DB.Viewport.Create(doc, sheet.Id, v.Id, center)
            if viewport_type_id is not None:
                try:
                    vp.ChangeTypeId(viewport_type_id)
                except Exception as e:
                    output.print_md("**Warning:** could not apply viewport type: {}".format(e))
            placed.append(vp)
    return placed


def arrange_viewports(placed, center, layout_code, interior=None):
    """Arrange already-created viewports into the given layout ('2x2',
    '1x4', '4x1', or 'auto' to pick the best fit), centered on `center`.
    Uses GetBoxOutline() for accurate placement, but temporarily enables
    TemporaryViewPropertiesMode on each view before measuring so levels and
    grids can be hidden regardless of any applied view template - then
    restores everything before moving viewports. Returns
    (needed_width, needed_height, chosen_code)."""
    if not placed:
        return (0, 0, layout_code)

    doc.Regenerate()

    suppress_cats = [DB.BuiltInCategory.OST_Levels, DB.BuiltInCategory.OST_Grids]
    views_enabled = []

    # Wrap the enable/measure step in a SubTransaction that we always roll back.
    # Rolling back is the correct cleanup for TemporaryViewPropertiesMode on ALL
    # view types including ViewSection (interior elevations) — calling
    # DisableTemporaryViewPropertiesMode() on a ViewSection throws in this API
    # version and the exception gets swallowed, leaving the "Temporary View
    # Properties" badge permanently on the view after the outer transaction commits.
    # A rolled-back SubTransaction discards every change made inside it, including
    # the EnableTemporaryViewPropertiesMode state, with no per-view cleanup needed.
    sub_t = DB.SubTransaction(doc)
    try:
        sub_t.Start()

        for vp in placed:
            v = doc.GetElement(vp.ViewId)
            if v is None:
                continue
            try:
                base_id = (v.ViewTemplateId
                           if v.ViewTemplateId != DB.ElementId.InvalidElementId
                           else DB.ElementId.InvalidElementId)
                v.EnableTemporaryViewPropertiesMode(base_id)
                for cat_bic in suppress_cats:
                    try:
                        cat = DB.Category.GetCategory(doc, cat_bic)
                        if cat is not None and not v.GetCategoryHidden(cat.Id):
                            v.SetCategoryHidden(cat.Id, True)
                    except Exception:
                        pass
            except Exception:
                pass  # view doesn't support temp properties - skip

        doc.Regenerate()

        sizes = [
            (vp.GetBoxOutline().MaximumPoint.X - vp.GetBoxOutline().MinimumPoint.X,
             vp.GetBoxOutline().MaximumPoint.Y - vp.GetBoxOutline().MinimumPoint.Y)
            for vp in placed
        ]

    finally:
        # Always roll back — this discards EnableTemporaryViewPropertiesMode on
        # every view type (including ViewSection) without calling Disable explicitly.
        try:
            sub_t.RollBack()
        except Exception:
            pass
        doc.Regenerate()

    if layout_code == "auto":
        chosen_code, offsets, needed_w, needed_h = choose_best_layout(sizes, interior)
    else:
        chosen_code = layout_code
        offsets, needed_w, needed_h = compute_layout_footprint(sizes, layout_code)

    for i, vp in enumerate(placed):
        outline = vp.GetBoxOutline()
        current_center = DB.XYZ(
            (outline.MinimumPoint.X + outline.MaximumPoint.X) / 2.0,
            (outline.MinimumPoint.Y + outline.MaximumPoint.Y) / 2.0,
            0,
        )
        target_center = center + offsets[i]
        DB.ElementTransformUtils.MoveElement(doc, vp.Id, target_center - current_center)

    return (needed_w, needed_h, chosen_code)


def place_views_in_grid_with_overflow(sheet, views, vertical_offset, horizontal_offset, layout_code,
                                       title_block, base, margin_ft,
                                       sheet_name_value, viewport_type_id=None):
    """Place views on `sheet` (numbered {base}-1) in the given layout
    ('2x2', '1x4', '4x1', or 'auto' to pick the best fit). If they don't
    fit inside the title block's interior area, split the last half onto a
    second new sheet numbered {base}-2 in the same layout instead. Returns
    a list of the ViewSheets used (1 or 2 entries)."""
    center = get_sheet_center(sheet)
    center = DB.XYZ(center.X + horizontal_offset, center.Y + vertical_offset, center.Z)

    placed = create_viewports(sheet, views, center, viewport_type_id)
    if not placed:
        return [sheet]

    interior = get_title_block_interior_size(sheet, margin_ft)

    needed_w, needed_h, chosen_code = arrange_viewports(placed, center, layout_code, interior)

    if interior is None:
        return [sheet]  # can't check fit, accept as-is

    interior_w, interior_h = interior

    if needed_w <= interior_w and needed_h <= interior_h:
        return [sheet]  # fits fine on one sheet

    if len(placed) < 2:
        return [sheet]  # nothing sensible to split

    output.print_md("**Note for '{}':** all {} elevations don't fit on one sheet "
                     "(need {:.0f}mm x {:.0f}mm, have {:.0f}mm x {:.0f}mm available) - "
                     "splitting across 2 sheets.".format(
                         base, len(placed),
                         needed_w * MM_PER_FOOT, needed_h * MM_PER_FOOT,
                         interior_w * MM_PER_FOOT, interior_h * MM_PER_FOOT))

    mid = len(placed) // 2
    first_half, second_half = placed[:mid], placed[mid:]
    second_half_views = [v for v in views if v.Id in [vp.ViewId for vp in second_half]]

    # Re-arrange the first half alone on the original sheet
    arrange_viewports(first_half, center, layout_code, interior)

    # Remove the second half's viewports from the original sheet and
    # re-create them on a brand new sheet with the same title block.
    for vp in second_half:
        doc.Delete(vp.Id)

    sheet2 = DB.ViewSheet.Create(doc, title_block.Id)
    sheet2.Name = sheet_name_value
    sheet2.SheetNumber = "{}-2".format(base)
    doc.Regenerate()

    center2 = get_sheet_center(sheet2)
    center2 = DB.XYZ(center2.X + horizontal_offset, center2.Y + vertical_offset, center2.Z)
    placed2 = create_viewports(sheet2, second_half_views, center2, viewport_type_id)
    interior2 = get_title_block_interior_size(sheet2, margin_ft)
    arrange_viewports(placed2, center2, layout_code, interior2)

    return [sheet, sheet2]


def split_trailing_number(s):
    """Split a string into (prefix, digits) based on its trailing numeric run.
    Returns (s, None) if there's no trailing number."""
    m = re.search(r'(\d+)$', s)
    if not m:
        return s, None
    return s[:m.start()], m.group(1)


def increment_sheet_number(s):
    """Increment the trailing number in a sheet number string, preserving
    zero-padding width (e.g. 'A-100' -> 'A-101', '6501' -> '6502').
    If the string ends in a -0, -1, or -2 suffix (from the base-0/-1/-2
    scheme), strips it first so the base number gets incremented, not the
    suffix (e.g. '6501-0' -> '6502', '6501-1' -> '6502')."""
    # Only strip a trailing single-digit -N suffix (our scheme uses -0, -1, -2)
    # to avoid misidentifying things like 'A-100' as base 'A' with suffix '-100'
    m_suffix = re.search(r'-(\d)$', s)
    if m_suffix:
        s = s[:m_suffix.start()]

    prefix, digits = split_trailing_number(s)
    if digits is None:
        return s  # nothing numeric to increment
    width = len(digits)
    incremented = str(int(digits) + 1).zfill(width)
    return prefix + incremented


class SheetNumberAllocator(object):
    """Hands out base numbers for the {base}-0 / {base}-1 / {base}-2 sheet
    trio scheme, starting from a given value and scanning forward for the
    next usable base. A base is usable if none of its 3 slots exist yet, or
    every slot that DOES exist is an empty sheet (nothing but its title
    block) - those get reclaimed by renaming them to TEMP-NNN, freeing the
    base for use. If any existing slot has real content, the whole base is
    skipped and the next one is tried.

    Pass dry_run=True to compute what WOULD happen (for previews / the
    dialog's "Find Next Available" detection) without actually renaming
    anything in the document."""

    SUFFIXES = ("0", "1", "2")

    def __init__(self, start_base, temp_prefix="TEMP"):
        self.next_candidate = start_base
        self.temp_counter = 1
        self.temp_prefix = temp_prefix or "TEMP"
        self.existing_map = get_existing_sheet_numbers_map()

    def _check_base(self, base):
        """Read-only: is this base usable, and which of its existing slots
        (if any) would need reclaiming? Returns (usable, [sheets_to_reclaim]).
        Also checks for any sheet matching {base}-N pattern beyond the three
        standard suffixes, so a partial set (e.g. -1 and -2 exist but -0
        doesn't) is correctly treated as that base being in use."""
        # First check for any sheet using this base with ANY suffix pattern
        # (catches cases where -1/-2 exist without a corresponding -0)
        base_lower = base.lower() + "-"
        for key in list(self.existing_map.keys()):
            if key.startswith(base_lower) and self.existing_map[key] != "RESERVED":
                sheet = self.existing_map[key]
                if not is_sheet_empty(sheet):
                    return False, []  # base is in use with a non-empty sheet

        reclaim_sheets = []
        for suffix in self.SUFFIXES:
            number = "{}-{}".format(base, suffix)
            entry = self.existing_map.get(number.lower())
            if entry is not None:
                if entry == "RESERVED":
                    return False, []  # already claimed earlier in this run
                if is_sheet_empty(entry):
                    reclaim_sheets.append(entry)
                else:
                    return False, []
        return True, reclaim_sheets

    def refresh(self):
        """Rebuild the existing sheets map from the current document state.
        Call this if sheets may have been created since __init__."""
        self.existing_map = get_existing_sheet_numbers_map()

    def claim_base(self, dry_run=False, max_tries=500):
        """Find and reserve the next usable base starting from
        self.next_candidate. Returns (base, reclaims) where reclaims is a
        list of (old_number, new_temp_number) pairs that were (or, in a dry
        run, would be) renamed to free up this base."""
        candidate = self.next_candidate
        for _ in range(max_tries):
            usable, reclaim_sheets = self._check_base(candidate)
            if usable:
                reclaims = []
                for sheet in reclaim_sheets:
                    old_number = sheet.SheetNumber
                    new_number = "{}-{:03d}".format(self.temp_prefix, self.temp_counter)
                    self.temp_counter += 1
                    if not dry_run:
                        sheet.SheetNumber = new_number
                    del self.existing_map[old_number.lower()]
                    self.existing_map[new_number.lower()] = sheet
                    reclaims.append((old_number, new_number))

                for suffix in self.SUFFIXES:
                    self.existing_map["{}-{}".format(candidate, suffix).lower()] = "RESERVED"

                self.next_candidate = increment_sheet_number(candidate)
                return candidate, reclaims

            candidate = increment_sheet_number(candidate)

        raise Exception("Could not find an available sheet number base after {} attempts starting from '{}'.".format(
            max_tries, self.next_candidate))


def find_next_available_base(start_base, temp_prefix="TEMP"):
    """Read-only lookup of the next usable base from start_base, for
    prefilling/detecting the Starting Sheet Number field. Does not touch
    the document."""
    allocator = SheetNumberAllocator(start_base, temp_prefix)
    base, _ = allocator.claim_base(dry_run=True)
    return base


def get_room_point(room):
    loc = room.Location
    if isinstance(loc, DB.LocationPoint):
        return loc.Point
    return None


def room_level_id(room):
    """The ElementId of the Level a Room is associated with, or None."""
    try:
        return room.Level.Id if room.Level is not None else None
    except Exception:
        return None


def plan_view_level_id(plan_view):
    """The ElementId of the Level a plan view is generated from, or None."""
    try:
        return plan_view.GenLevel.Id if plan_view.GenLevel is not None else None
    except Exception:
        return None


def plan_view_phase_id(plan_view):
    """The ElementId of the Phase a plan view is set to, or None."""
    try:
        p = plan_view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE)
        if p is not None and p.HasValue:
            return p.AsElementId()
    except Exception:
        pass
    return None


def room_phase_id(room):
    """The ElementId of the Phase a Room belongs to, or None."""
    try:
        p = room.get_Parameter(DB.BuiltInParameter.ROOM_PHASE)
        if p is not None and p.HasValue:
            return p.AsElementId()
    except Exception:
        pass
    return None


def find_rooms_for_plan(plan_view):
    """Find Rooms whose location point falls inside this plan view's crop
    region AND whose own Level and Phase match the plan view's level and
    phase (this excludes rooms from wrong phases and rooms stacked on
    floors above/below). Falls back gracefully if phase/level info is
    unavailable. Returns a list (possibly empty)."""
    bbox = plan_view.CropBox
    if bbox is None:
        return []

    tf = bbox.Transform
    rooms = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Rooms)\
        .WhereElementIsNotElementType().ToElements()

    matches = []
    for r in rooms:
        point = get_room_point(r)
        if point is None:
            continue
        p = tf.Inverse.OfPoint(point)
        if (bbox.Min.X <= p.X <= bbox.Max.X) and (bbox.Min.Y <= p.Y <= bbox.Max.Y):
            matches.append(r)

    # Filter by phase first (most important — rules out wrong-phase rooms)
    view_phase_id = plan_view_phase_id(plan_view)
    if view_phase_id is not None and view_phase_id != DB.ElementId.InvalidElementId:
        same_phase = [r for r in matches if room_phase_id(r) == view_phase_id]
        if same_phase:
            matches = same_phase
        # if no rooms match the phase, fall through to the unfiltered set

    # Filter by level (rules out same-phase rooms on floors above/below)
    level_id = plan_view_level_id(plan_view)
    if level_id is not None:
        same_level = [r for r in matches if room_level_id(r) == level_id]
        if same_level:
            return same_level
        # if no rooms match the level, fall back to the phase-filtered set

    return matches


def room_name(room):
    try:
        name_param = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
        if name_param and name_param.HasValue and name_param.AsString():
            return name_param.AsString()
    except Exception:
        pass
    return None


def room_number(room):
    try:
        number_param = room.get_Parameter(DB.BuiltInParameter.ROOM_NUMBER)
        if number_param and number_param.HasValue and number_param.AsString():
            return number_param.AsString()
    except Exception:
        pass
    return ""


def get_room_for_marker(marker, plan_view):
    """The precise, unambiguous way to find 'the room this plan is about':
    ask Revit which Room actually contains the elevation marker's point
    (Document.GetRoomAtPoint), rather than which rooms' center points
    happen to fall inside the plan's rectangular crop. The marker's Z is
    replaced with a point partway up the plan's level, since GetRoomAtPoint
    needs a Z that actually falls within the room's vertical extent. As a
    safety net, the found room's Level and Phase are both checked against
    the plan view's Level and Phase to reject stacked rooms or wrong-phase
    rooms."""
    if marker is None:
        return None
    point = get_marker_point(marker, plan_view)
    if point is None:
        return None
    try:
        level = plan_view.GenLevel
        z = (level.Elevation + 4.0) if level is not None else point.Z  # ~1.2m above level
        test_point = DB.XYZ(point.X, point.Y, z)
        room = doc.GetRoomAtPoint(test_point)
    except Exception:
        return None

    if room is not None:
        # Reject if on the wrong level
        plan_level_id = plan_view_level_id(plan_view)
        found_level_id = room_level_id(room)
        if plan_level_id is not None and found_level_id is not None and plan_level_id != found_level_id:
            return None

        # Reject if from the wrong phase
        view_phase_id = plan_view_phase_id(plan_view)
        found_phase_id = room_phase_id(room)
        if (view_phase_id is not None and view_phase_id != DB.ElementId.InvalidElementId
                and found_phase_id is not None and found_phase_id != view_phase_id):
            return None

    return room


def get_primary_room(plan_view, verbose=True):
    """Fallback room lookup when there's no usable marker: the first Room
    found inside the plan's crop region. Warns if more than one qualifies,
    since this method is inherently ambiguous."""
    rooms = find_rooms_for_plan(plan_view)
    if not rooms:
        return None
    if len(rooms) > 1 and verbose:
        names = ", ".join(room_name(r) or "(unnamed)" for r in rooms)
        output.print_md("**Warning for '{}':** {} rooms fall inside this plan's crop region "
                         "({}) and no elevation marker could pin down the exact room. "
                         "Using the first one found for sheet titling.".format(
                             plan_view.Name, len(rooms), names))
    return rooms[0]


def resolve_marker_and_room(plan_view, verbose=True):
    """The single source of truth for 'which elevation marker, and which
    room, does this plan view correspond to'. Room is resolved via the
    marker's exact point when possible (precise); only falls back to the
    looser crop-region match if that fails. Returns (marker, room) - either
    may be None."""
    marker = find_elevation_marker_for_plan(plan_view, verbose=verbose)
    room = get_room_for_marker(marker, plan_view) if marker is not None else None
    if room is None:
        room = get_primary_room(plan_view, verbose=verbose)
    return marker, room


def base_name_from_room(room, plan_view):
    """Room name (upper-cased) if given, else the plan view's own name
    (upper-cased) as a fallback."""
    if room is not None:
        name = room_name(room)
        if name:
            return name.upper()
    return plan_view.Name.upper()


def sheet_base_name(plan_view, verbose=True):
    """Room name (upper-cased) if a Room can be resolved for this plan view,
    else the plan view's own name (upper-cased) as a fallback. Prefer
    resolve_marker_and_room() directly when you also need the marker, to
    avoid resolving the room twice."""
    _, room = resolve_marker_and_room(plan_view, verbose=verbose)
    return base_name_from_room(room, plan_view)


def is_view_already_placed(view):
    """True if this view already has a Viewport on some sheet."""
    viewports = DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements()
    for vp in viewports:
        if vp.ViewId == view.Id:
            return True
    return False


def get_sheet_for_view(view):
    """Return the ViewSheet a view is placed on, or None if it isn't placed."""
    viewports = DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements()
    for vp in viewports:
        if vp.ViewId == view.Id:
            return doc.GetElement(vp.SheetId)
    return None


TITLE_PARAM_2_NAME = "OLA Sheet Title 2"
TITLE_PARAM_3_NAME = "OLA Sheet Title 3"


def split_title_text(text, max_len):
    """Split text into (part1, part2) so part1 fits within max_len chars.
    Breaks at the last space within the limit when possible, to avoid
    cutting a word in half; falls back to a hard cut if there's no space
    (e.g. one very long word)."""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text, ""
    cut = text.rfind(" ", 0, max_len + 1)
    if cut <= 0:
        cut = max_len  # no space found - hard cut
    part1 = text[:cut].rstrip()
    part2 = text[cut:].strip()
    return part1, part2


def build_title2_title3(base_name, number, suffix, max_len, sheet_label=None):
    """Compose the OLA Sheet Title 2 / 3 text: '{number} {name} {suffix}'.
    The suffix (PLAN/ELEVATIONS) always stays on line 2 - only the room
    number/name portion is truncated (word-aware) if the combined text
    would exceed max_len chars. Overflowed room text, plus sheet_label
    ('SHEET 1'/'SHEET 2', or None) when elevations split across 2 sheets,
    goes on line 3."""
    number = (number or "").strip()
    base_text = "{} {}".format(number, base_name).strip() if number else base_name.strip()
    suffix = (suffix or "").strip()

    full_line = "{} {}".format(base_text, suffix).strip()
    if len(full_line) <= max_len:
        return full_line, (sheet_label or "")

    # Keep the suffix intact; truncate the room number/name portion instead
    reserve = len(suffix) + 1 if suffix else 0
    room_for_base = max_len - reserve

    if room_for_base < 1:
        # Suffix alone doesn't fit within max_len - fall back to a plain
        # whole-line split rather than producing an empty/garbled line 2.
        title2, overflow = split_title_text(full_line, max_len)
        title3 = "{} {}".format(overflow, sheet_label).strip() if sheet_label else overflow
        return title2, title3

    base_part, base_overflow = split_title_text(base_text, room_for_base)
    title2 = "{} {}".format(base_part, suffix).strip()

    title3_parts = []
    if base_overflow:
        title3_parts.append(base_overflow)
    if sheet_label:
        title3_parts.append(sheet_label)
    title3 = " ".join(title3_parts)

    return title2, title3


def set_sheet_title_params(sheet, title2_text, title3_text, log_label):
    """Write title2_text/title3_text into the OLA Sheet Title 2/3 shared
    parameters. Warns (without raising) if either parameter isn't found on
    the sheet."""
    p2 = sheet.LookupParameter(TITLE_PARAM_2_NAME)
    if p2 is None:
        output.print_md("**Warning for '{}':** shared parameter '{}' not found on sheet {} - "
                         "make sure it's bound to the Sheets category in this project.".format(
                             log_label, TITLE_PARAM_2_NAME, sheet.SheetNumber))
    else:
        p2.Set(title2_text or "")

    p3 = sheet.LookupParameter(TITLE_PARAM_3_NAME)
    if p3 is None:
        if title3_text:
            output.print_md("**Warning for '{}':** '{}' overflows into '{}' but that shared parameter "
                             "wasn't found on sheet {}.".format(
                                 log_label, title2_text, TITLE_PARAM_3_NAME, sheet.SheetNumber))
    else:
        p3.Set(title3_text or "")


# ---------------------------------------------------------------------------
# Preview computation (dry run, no document changes)
# ---------------------------------------------------------------------------

def compute_preview(plan_views, start_number, temp_prefix="TEMP"):
    """Return a list of (plan_view, action_text) describing what would happen
    for each plan view, without changing the document. Uses a scratch,
    dry-run SheetNumberAllocator so the numbers (and any reclaims) shown
    match what a real run would do, without touching the document. Also
    flags likely data issues (missing room number/name, partial elevation
    sets, duplicate room numbers across the batch) as WARN lines."""
    preview_allocator = SheetNumberAllocator(start_number, temp_prefix)
    lines = []
    seen_numbers = {}  # room_number.lower() -> [plan_view.Name, ...]

    for plan_view in plan_views:
        marker, room = resolve_marker_and_room(plan_view, verbose=False)
        base_name = base_name_from_room(room, plan_view)
        room_num = room_number(room) if room is not None else ""
        plan_placed = is_view_already_placed(plan_view)
        elevation_views = get_elevation_views(marker) if marker else []
        elev_placed = any(is_view_already_placed(v) for v in elevation_views) if elevation_views else False
        elev_names = ", ".join(v.Name for v in elevation_views)

        warnings = []
        if room is None:
            warnings.append("no room found - using plan view's own name")
        else:
            if not room_name(room):
                warnings.append("room has no Name")
            if not room_num:
                warnings.append("room has no Number")
        if marker and 0 < len(elevation_views) < 4:
            warnings.append("only {} of 4 elevations found".format(len(elevation_views)))
        if room_num:
            key = room_num.strip().lower()
            seen_numbers.setdefault(key, []).append(plan_view.Name)

        warn_note = " | WARN: " + "; ".join(warnings) if warnings else ""

        if not marker:
            lines.append((plan_view, "SKIP - no elevation marker found in crop" + warn_note))
        elif not elevation_views:
            lines.append((plan_view, "SKIP - marker has no elevation views" + warn_note))
        elif plan_placed and elev_placed:
            lines.append((plan_view, "SYNC NAME ONLY - all views already placed (room: {}) | elevations: {}{}".format(
                base_name, elev_names, warn_note)))
        else:
            base, reclaims = preview_allocator.claim_base(dry_run=True)
            reclaim_note = ""
            if reclaims:
                reclaim_note = " | reclaiming empty sheet(s): " + ", ".join(
                    "{} -> {}".format(old, new) for old, new in reclaims)
            partial = ""
            if plan_placed:
                partial = " (plan already placed - will only create elevation sheet)"
            elif elev_placed:
                partial = " (elevations already placed - will only create plan sheet)"
            lines.append((plan_view, "CREATE{} - room: {} -> sheets {}-0 / {}-1 "
                          "(elevations may add a {}-2 sheet if they don't fit) "
                          "| elevations: {}{}{}".format(partial, base_name, base, base, base, elev_names, reclaim_note, warn_note)))

    # Flag duplicate room numbers across the batch (append as extra lines)
    for key, names in seen_numbers.items():
        if len(names) > 1:
            lines.append((None, "WARN: room number '{}' is used by {} plan views in this batch: {}".format(
                key.upper(), len(names), ", ".join(names))))

    return lines


# ---------------------------------------------------------------------------
# "Sync all sheet names" - standalone action across every placed view
# ---------------------------------------------------------------------------

def sheet_already_migrated(plan_sheet, elev_sheets_list):
    """Heuristic: does this room's sheet set already look like it's on the
    new base-0/-1/-2 scheme? ALL three sheets (plan and elevations) must
    share the same base and have the correct suffix. If only some do, the
    set is considered not yet migrated so we attempt a full renumber."""
    if plan_sheet is None and not elev_sheets_list:
        return False

    # Extract base from plan sheet — it must end in -0
    if plan_sheet is None:
        return False

    plan_num = plan_sheet.SheetNumber
    m = re.search(r'^(.*)-0$', plan_num)
    if not m:
        return False

    base = m.group(1)

    # Check all elevation sheets also share the same base
    for idx, s in enumerate(elev_sheets_list):
        expected = "{}-{}".format(base, idx + 1)
        if s.SheetNumber != expected:
            return False

    return True


def sync_sheet_names_for_views(plan_views, sheet_name_value, plan_title_suffix, elev_title_suffix, title_char_limit,
                                audit_sheet_name=True, renumber_existing=False, allocator=None,
                                plan_template_id=None, elev_template_id=None, viewport_type_id=None):
    """Walk the given plan views; for any already-placed plan or elevation
    views among them, sync the OLA Sheet Title 2/3 shared parameters (Lines
    2/3) to the current room info. If audit_sheet_name is True, also sync
    Sheet Name (Line 1) to sheet_name_value. If renumber_existing is True
    (a deliberate one-off migration), also renumber any room whose sheets
    aren't already on the base-0/-1/-2 scheme - claiming one base per room
    from `allocator` and applying it to the plan sheet and all of its
    elevation sheet(s) together. Runs in its own transaction. Returns
    (updated_count, renumbered_count, detail_lines)."""
    updated = 0
    renumbered = 0
    detail_lines = []

    def fmt_plan(s):
        return s.SheetNumber if s else "-"

    def fmt_elevs(sheets):
        return ", ".join(s.SheetNumber for s in sheets) if sheets else "-"

    with revit.Transaction("Sync Sheet Titles to Current Room Info"):
        for plan_view in plan_views:
            marker, room = resolve_marker_and_room(plan_view, verbose=False)
            base_name = base_name_from_room(room, plan_view)
            number = room_number(room) if room is not None else ""

            plan_sheet = get_sheet_for_view(plan_view) if is_view_already_placed(plan_view) else None

            elevation_views = get_elevation_views(marker) if marker else []
            placed_elev = [v for v in elevation_views if is_view_already_placed(v)]
            sheets_used = {}
            for v in placed_elev:
                s = get_sheet_for_view(v)
                if s is not None:
                    sheets_used[s.Id.IntegerValue] = s
            elev_sheets_list = sorted(sheets_used.values(), key=lambda s: s.SheetNumber)

            # Rename views to match the naming convention
            sync_view_names(plan_view, elevation_views, base_name, number)
            # Apply view templates
            apply_view_templates_to_room(plan_view, elevation_views, plan_template_id, elev_template_id)
            # Update viewport types on already-placed elevation viewports
            apply_viewport_type_to_placed_views(elevation_views, elev_sheets_list, viewport_type_id)

            old_plan_num = fmt_plan(plan_sheet)
            old_elev_nums = fmt_elevs(elev_sheets_list)

            if renumber_existing and allocator is not None and (plan_sheet is not None or elev_sheets_list):
                if not sheet_already_migrated(plan_sheet, elev_sheets_list):
                    try:
                        new_base, reclaims = allocator.claim_base()
                    except Exception as e:
                        msg = "ERROR  | {:<30} | room: {} {} | could not claim base: {}".format(
                            plan_view.Name, number, base_name, e)
                        detail_lines.append(msg)
                        output.print_md("**Error claiming base for '{}':** {}".format(plan_view.Name, e))
                        continue

                    for old_number, new_number in reclaims:
                        output.print_md("**Reclaimed empty sheet:** '{}' -> '{}' to free up base '{}' for '{}'.".format(
                            old_number, new_number, new_base, plan_view.Name))

                    if plan_sheet is not None:
                        old_num = plan_sheet.SheetNumber
                        try:
                            plan_sheet.SheetNumber = "{}-0".format(new_base)
                            output.print_md("**Renumbered (plan) '{}':** '{}' -> '{}'".format(
                                plan_view.Name, old_num, plan_sheet.SheetNumber))
                            renumbered += 1
                        except Exception as e:
                            output.print_md("**Failed to renumber plan sheet '{}' from '{}': {}**".format(
                                plan_view.Name, old_num, e))

                    for idx, s in enumerate(elev_sheets_list):
                        old_num = s.SheetNumber
                        try:
                            s.SheetNumber = "{}-{}".format(new_base, idx + 1)
                            output.print_md("**Renumbered (elevations) '{}':** '{}' -> '{}'".format(
                                plan_view.Name, old_num, s.SheetNumber))
                            renumbered += 1
                        except Exception as e:
                            output.print_md("**Failed to renumber elevation sheet '{}' from '{}': {}**".format(
                                plan_view.Name, old_num, e))
                else:
                    output.print_md("**Note for '{}':** already on the new numbering scheme (plan: {}) - skipped renumbering.".format(
                        plan_view.Name, plan_sheet.SheetNumber if plan_sheet else "not placed"))

            if plan_sheet is not None:
                if audit_sheet_name:
                    plan_sheet.Name = sheet_name_value
                title2, title3 = build_title2_title3(base_name, number, plan_title_suffix, title_char_limit)
                set_sheet_title_params(plan_sheet, title2, title3, plan_view.Name)
                updated += 1
            else:
                output.print_md("**Note for '{}':** plan view is not placed on any sheet - nothing to sync there.".format(plan_view.Name))

            if not marker:
                output.print_md("**Note for '{}':** no elevation marker found for this plan - elevations not checked.".format(plan_view.Name))
                detail_lines.append("NO_MARKER| {:<30} | room: {} {}".format(plan_view.Name, number, base_name))
                continue

            if not elev_sheets_list:
                output.print_md("**Note for '{}':** none of its interior elevations are placed on any sheet - nothing to sync there.".format(plan_view.Name))
            else:
                multi = len(elev_sheets_list) > 1
                for idx, s in enumerate(elev_sheets_list):
                    if audit_sheet_name:
                        s.Name = sheet_name_value
                    sheet_label = "SHEET {}".format(idx + 1) if multi else None
                    title2, title3 = build_title2_title3(base_name, number, elev_title_suffix, title_char_limit, sheet_label)
                    set_sheet_title_params(s, title2, title3, plan_view.Name)
                    updated += 1

            # Record the final state (after any renumbering) for the log
            new_plan_num = fmt_plan(plan_sheet)
            new_elev_nums = fmt_elevs(elev_sheets_list)

            if old_plan_num != new_plan_num or old_elev_nums != new_elev_nums:
                action = "RENUMBERED"
            elif plan_sheet is not None or elev_sheets_list:
                action = "SYNCED"
            else:
                action = "NOT PLACED"

            detail_lines.append("{:<10} | {:<30} | room: {} {} | plan: {} -> {} | elevations: {} -> {}".format(
                action, plan_view.Name, number, base_name, old_plan_num, new_plan_num, old_elev_nums, new_elev_nums))

    return updated, renumbered, detail_lines




# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------

def process_plan_view(plan_view, title_block, vertical_offset, horizontal_offset, layout_code, allocator,
                       margin_ft, sheet_name_value, plan_title_suffix, elev_title_suffix, title_char_limit,
                       viewport_type_id=None, plan_template_id=None, elev_template_id=None):
    """Create plan + elevation sheets for one plan view, numbered as a
    {base}-0 (plan) / {base}-1 (elevations) / {base}-2 (elevations overflow,
    if needed) trio from `allocator`. Sheet Name (Line 1) is the same
    batch-wide value for every sheet. OLA Sheet Title 2 (Line 2) is
    '{room number} {room name} - PLAN/ELEVATIONS'; OLA Sheet Title 3 (Line 3)
    is any overflow beyond the char limit, plus 'SHEET 1'/'SHEET 2' if
    elevations split across 2 sheets. If a view is already placed, only the
    title parameters are re-synced (Sheet Name is left alone, since it's no
    longer room-derived) - no base is consumed in that case.

    Returns a result dict: {plan_view_name, room_name, room_number, status
    ('created'/'synced'/'skipped'), plan_sheet_number, elev_sheet_numbers,
    reclaims, reason (for skipped)}."""
    result = {
        "plan_view_name": plan_view.Name,
        "room_name": None,
        "room_number": None,
        "status": "skipped",
        "plan_sheet_number": None,
        "elev_sheet_numbers": [],
        "reclaims": [],
        "reason": "",
    }

    marker, room = resolve_marker_and_room(plan_view)
    base_name = base_name_from_room(room, plan_view)
    number = room_number(room) if room is not None else ""
    result["room_name"] = base_name
    result["room_number"] = number

    if not marker:
        output.print_md("**Skipped '{}':** no elevation marker with interior elevations was found inside its crop region.".format(plan_view.Name))
        result["reason"] = "no elevation marker found in crop"
        return result

    elevation_views = get_elevation_views(marker)
    if not elevation_views:
        output.print_md("**Skipped '{}':** the elevation marker found has no associated elevation views.".format(plan_view.Name))
        result["reason"] = "marker has no elevation views"
        return result

    plan_already_placed = is_view_already_placed(plan_view)
    already_placed_elevations = [v for v in elevation_views if is_view_already_placed(v)]
    unplaced_elevations = [v for v in elevation_views if not is_view_already_placed(v)]

    # Rename views to match the naming convention regardless of placement status
    sync_view_names(plan_view, elevation_views, base_name, number)
    # Apply view templates (always, for consistency)
    apply_view_templates_to_room(plan_view, elevation_views, plan_template_id, elev_template_id)

    # Sync title parameters on any already-placed plan sheet
    if plan_already_placed:
        existing_plan_sheet = get_sheet_for_view(plan_view)
        if existing_plan_sheet is not None:
            title2, title3 = build_title2_title3(base_name, number, plan_title_suffix, title_char_limit)
            set_sheet_title_params(existing_plan_sheet, title2, title3, plan_view.Name)
            result["plan_sheet_number"] = existing_plan_sheet.SheetNumber

    # Sync title parameters on any already-placed elevation sheet(s)
    sheets_list = []
    if already_placed_elevations:
        sheets_used = {}
        for v in already_placed_elevations:
            s = get_sheet_for_view(v)
            if s is not None:
                sheets_used[s.Id.IntegerValue] = s
        sheets_list = sorted(sheets_used.values(), key=lambda s: s.SheetNumber)
        multi = len(sheets_list) > 1
        for idx, s in enumerate(sheets_list):
            sheet_label = "SHEET {}".format(idx + 1) if multi else None
            title2, title3 = build_title2_title3(base_name, number, elev_title_suffix, title_char_limit, sheet_label)
            set_sheet_title_params(s, title2, title3, plan_view.Name)
        result["elev_sheet_numbers"] = [s.SheetNumber for s in sheets_list]

    # If everything is already placed, nothing to create
    if plan_already_placed and not unplaced_elevations:
        output.print_md("**Synced '{}':** all views already placed - title parameters updated.".format(plan_view.Name))
        result["status"] = "synced"
        result["reason"] = "already placed - title parameters re-synced"
        return result

    # Determine the base number to use for this room's sheets.
    # If the plan is already placed on a sheet ending in -0, derive the base
    # from that sheet's number so elevation sheets share the same base.
    # Otherwise claim a fresh base from the allocator.
    base = None
    reclaims = []

    if plan_already_placed and result["plan_sheet_number"]:
        existing_num = result["plan_sheet_number"]
        m = re.search(r'^(.*)-0$', existing_num)
        if m:
            base = m.group(1)
            output.print_md("**Note for '{}':** plan already on sheet {} - deriving base '{}' for elevation sheets.".format(
                plan_view.Name, existing_num, base))

    if base is None:
        base, reclaims = allocator.claim_base()
        result["reclaims"] = reclaims
        for old_number, new_number in reclaims:
            output.print_md("**Reclaimed empty sheet:** '{}' -> '{}' to free up base '{}' for '{}'.".format(
                old_number, new_number, base, plan_view.Name))

    # Sheet 1: plan view (only if not already placed)
    if not plan_already_placed:
        plan_sheet = DB.ViewSheet.Create(doc, title_block.Id)
        plan_sheet.Name = sheet_name_value
        plan_sheet.SheetNumber = "{}-0".format(base)
        doc.Regenerate()
        plan_title2, plan_title3 = build_title2_title3(base_name, number, plan_title_suffix, title_char_limit)
        set_sheet_title_params(plan_sheet, plan_title2, plan_title3, plan_view.Name)
        result["plan_sheet_number"] = plan_sheet.SheetNumber

        if DB.Viewport.CanAddViewToSheet(doc, plan_sheet.Id, plan_view.Id):
            DB.Viewport.Create(doc, plan_sheet.Id, plan_view.Id, get_sheet_center(doc.GetElement(plan_sheet.Id)))
        else:
            output.print_md("**Warning:** plan view '{}' could not be placed (may already be on another sheet).".format(plan_view.Name))
    else:
        output.print_md("**Note for '{}':** plan view already placed on sheet {} - skipping plan sheet creation.".format(
            plan_view.Name, result["plan_sheet_number"]))

    # Sheet 2: interior elevations (only unplaced ones, may split into {base}-2)
    if unplaced_elevations:
        elev_sheet = DB.ViewSheet.Create(doc, title_block.Id)
        elev_sheet.Name = sheet_name_value
        elev_sheet.SheetNumber = "{}-1".format(base)
        doc.Regenerate()

        elev_sheets = place_views_in_grid_with_overflow(
            elev_sheet, unplaced_elevations, vertical_offset, horizontal_offset, layout_code,
            title_block, base, margin_ft, sheet_name_value, viewport_type_id)

        multi = len(elev_sheets) > 1
        for idx, s in enumerate(elev_sheets):
            sheet_label = "SHEET {}".format(idx + 1) if multi else None
            title2, title3 = build_title2_title3(base_name, number, elev_title_suffix, title_char_limit, sheet_label)
            set_sheet_title_params(s, title2, title3, plan_view.Name)
        result["elev_sheet_numbers"] += [s.SheetNumber for s in elev_sheets]
    else:
        output.print_md("**Note for '{}':** all elevation views already placed - skipping elevation sheet creation.".format(plan_view.Name))

    elev_sheet_numbers = " / ".join(result["elev_sheet_numbers"])
    output.print_md("**Done '{}':** plan on sheet **{}**, elevations on sheet(s) **{}**.".format(
        plan_view.Name, result["plan_sheet_number"] or "(already placed)", elev_sheet_numbers or "(already placed)"))

    result["status"] = "created"
    return result


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class SelectableItem(object):
    """A checkbox-bindable wrapper around a plan view name."""
    def __init__(self, name, checked=False):
        self.Name = name
        self.IsChecked = checked


PREVIEW_XAML = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Preview"
    Height="480" Width="520"
    MinHeight="360" MinWidth="420"
    WindowStartupLocation="CenterScreen"
    ResizeMode="CanResize"
    Background="#FF262626"
    FontFamily="Segoe UI">
    <Window.Resources>
        <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="#FFE6E6E6"/>
        </Style>
        <Style TargetType="ListBox">
            <Setter Property="Background" Value="#FF303030"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="BorderBrush" Value="#FF4F4F4F"/>
            <Setter Property="Padding" Value="2"/>
        </Style>
        <Style TargetType="ListBoxItem">
            <Setter Property="Padding" Value="4,4"/>
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
    <Grid Margin="18">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <TextBlock Grid.Row="0" Text="Preview - review before creating sheets" FontSize="15" FontWeight="Bold" Margin="0,0,0,10"/>
        <ListBox x:Name="lst_preview" Grid.Row="1">
            <ListBox.ItemTemplate>
                <DataTemplate>
                    <TextBlock Text="{Binding}" TextWrapping="Wrap" Margin="0,2"/>
                </DataTemplate>
            </ListBox.ItemTemplate>
        </ListBox>
        <StackPanel Grid.Row="2" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,14,0,0">
            <Button x:Name="btn_back" Content="Back" Click="btn_back_click" Style="{StaticResource SecondaryButton}"/>
            <Button x:Name="btn_confirm" Content="Confirm and Run" Click="btn_confirm_click"/>
        </StackPanel>
    </Grid>
</Window>
"""


MESSAGE_DIALOG_XAML = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Notice"
    SizeToContent="Height" Width="440" MaxHeight="640"
    WindowStartupLocation="CenterScreen"
    ResizeMode="NoResize"
    Background="#FF262626"
    FontFamily="Segoe UI">
    <Window.Resources>
        <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="#FFE6E6E6"/>
        </Style>
        <Style TargetType="Button">
            <Setter Property="Background" Value="#FF0A84D8"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Padding" Value="14,7"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Margin" Value="6,0,0,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="MinWidth" Value="70"/>
        </Style>
        <Style x:Key="SecondaryButton" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
            <Setter Property="Background" Value="#FF4A4A4A"/>
        </Style>
    </Window.Resources>
    <Grid Margin="20">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <ScrollViewer Grid.Row="0" VerticalScrollBarVisibility="Auto" MaxHeight="500">
            <TextBlock x:Name="txt_message" Text="" FontSize="13" TextWrapping="Wrap" Margin="0,0,0,20"/>
        </ScrollViewer>
        <StackPanel Grid.Row="1" Orientation="Horizontal" HorizontalAlignment="Right">
            <Button x:Name="btn_secondary" Content="No" Click="btn_secondary_click" Style="{StaticResource SecondaryButton}" Visibility="Collapsed"/>
            <Button x:Name="btn_primary" Content="OK" Click="btn_primary_click"/>
        </StackPanel>
    </Grid>
</Window>
"""


class MessageDialog(forms.WPFWindow):
    """A sleeker, dark-themed replacement for pyRevit's default forms.alert,
    for both simple info messages (single OK) and Yes/No confirmations."""
    def __init__(self, xaml_source, message, title="Notice", primary_text="OK", secondary_text=None):
        forms.WPFWindow.__init__(self, xaml_source, literal_string=True)
        self.Title = title
        self.txt_message.Text = message
        self.btn_primary.Content = primary_text
        self.result = None

        if secondary_text:
            self.btn_secondary.Content = secondary_text
            self.btn_secondary.Visibility = Visibility.Visible

    def btn_primary_click(self, sender, args):
        self.result = True
        self.Close()

    def btn_secondary_click(self, sender, args):
        self.result = False
        self.Close()


def show_info(message, title="Notice"):
    """Sleek info popup with a single OK button."""
    dlg = MessageDialog(MESSAGE_DIALOG_XAML, message, title=title, primary_text="OK")
    dlg.ShowDialog()


def show_confirm(message, title="Confirm", yes_text="Yes", no_text="No"):
    """Sleek Yes/No confirmation popup. Returns True/False (False if closed
    via the window's X button, same as clicking No)."""
    dlg = MessageDialog(MESSAGE_DIALOG_XAML, message, title=title, primary_text=yes_text, secondary_text=no_text)
    dlg.ShowDialog()
    return bool(dlg.result)


class PreviewForm(forms.WPFWindow):
    def __init__(self, xaml_source, preview_lines):
        forms.WPFWindow.__init__(self, xaml_source, literal_string=True)
        self.confirmed = False
        self.lst_preview.ItemsSource = [
            action if plan_view is None else "{}  ->  {}".format(plan_view.Name, action)
            for plan_view, action in preview_lines
        ]

    def btn_back_click(self, sender, args):
        self.confirmed = False
        self.Close()

    def btn_confirm_click(self, sender, args):
        self.confirmed = True
        self.Close()


FORM_XAML = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Place RLS Views"
    Height="1000" Width="760"
    MinHeight="600" MinWidth="660"
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
        <Style x:Key="RequiredTextBox" TargetType="TextBox" BasedOn="{StaticResource {x:Type TextBox}}">
            <Setter Property="BorderBrush" Value="#FF4FC3F7"/>
            <Setter Property="BorderThickness" Value="1.5"/>
        </Style>
        <Style x:Key="RequiredComboBox" TargetType="ComboBox" BasedOn="{StaticResource {x:Type ComboBox}}">
            <Setter Property="BorderBrush" Value="#FF4FC3F7"/>
            <Setter Property="BorderThickness" Value="1.5"/>
        </Style>
        <Style TargetType="ListBox">
            <Setter Property="Background" Value="#FF303030"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="BorderBrush" Value="#FF4F4F4F"/>
            <Setter Property="Padding" Value="2"/>
        </Style>
        <Style TargetType="ListBoxItem">
            <Setter Property="Padding" Value="4,3"/>
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
        <Style x:Key="DangerButton" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
            <Setter Property="Background" Value="#FFB0242E"/>
        </Style>
        <Style x:Key="MaintenanceLabel" TargetType="TextBlock" BasedOn="{StaticResource SectionLabel}">
            <Setter Property="Foreground" Value="#FFFFB74D"/>
            <Setter Property="FontSize" Value="12"/>
        </Style>
        <Style x:Key="AuditLabel" TargetType="TextBlock" BasedOn="{StaticResource SectionLabel}">
            <Setter Property="Foreground" Value="#FF4DB6AC"/>
            <Setter Property="FontSize" Value="12"/>
        </Style>
        <Style x:Key="MaintenanceButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
            <Setter Property="Background" Value="#FF5D4037"/>
            <Setter Property="Foreground" Value="#FFFFB74D"/>
        </Style>
        <Style x:Key="SmallButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
            <Setter Property="Padding" Value="8,3"/>
            <Setter Property="FontSize" Value="10"/>
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
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <StackPanel Grid.Row="0">
            <TextBlock Text="Place RLS Plan &amp; Elevations on Sheets" FontSize="17" FontWeight="Bold"/>
            <TextBlock Text="Select a title block, tick one or more plan views, and set sheet options. Saved plan view sets are optional."
                       Foreground="#FF9B9B9B" FontSize="11" Margin="0,3,0,0"/>
        </StackPanel>

        <StackPanel Grid.Row="1">
            <TextBlock Text="TITLE BLOCK (required)" Style="{StaticResource RequiredLabel}"/>
            <ComboBox x:Name="cmb_title_block" Style="{StaticResource RequiredComboBox}"/>
        </StackPanel>

        <StackPanel Grid.Row="2">
            <TextBlock Text="SAVED PLAN VIEW SETS" Style="{StaticResource SectionLabel}"/>
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <ComboBox x:Name="cmb_saved_sets" Grid.Column="0" SelectionChanged="cmb_saved_sets_selection_changed"/>
                <Button x:Name="btn_load_set" Grid.Column="1" Content="Reload" Click="btn_load_set_click" Style="{StaticResource SecondaryButton}"/>
                <Button x:Name="btn_update_set" Grid.Column="2" Content="Update" Click="btn_update_set_click" Style="{StaticResource SecondaryButton}"/>
                <Button x:Name="btn_save_set" Grid.Column="3" Content="Save As..." Click="btn_save_set_click" Style="{StaticResource SecondaryButton}"/>
                <Button x:Name="btn_delete_set" Grid.Column="4" Content="Delete" Click="btn_delete_set_click" Style="{StaticResource DangerButton}"/>
            </Grid>
        </StackPanel>

        <StackPanel Grid.Row="3">
            <TextBlock Text="MAINTENANCE &amp; SYNC SHEET NAMES" Style="{StaticResource MaintenanceLabel}"/>
            <Button x:Name="btn_sync_all" Content="Sync Sheet Names for Selected Set Above"
                    Click="btn_sync_all_click" Style="{StaticResource MaintenanceButton}"
                    HorizontalAlignment="Left"/>
            <CheckBox x:Name="chk_audit_sheet_name" Content="Also audit / update Sheet Name (Line 1) during sync"
                      Checked="chk_audit_sheet_name_changed" Unchecked="chk_audit_sheet_name_changed"
                      Margin="0,6,0,0"/>
            <CheckBox x:Name="chk_renumber_existing"
                      Content="Also renumber already-placed sheets to the new base-0/-1/-2 scheme (one-off migration, uses Starting Sheet Number below)"
                      Foreground="#FF4FC3F7" Margin="0,6,0,0"/>
        </StackPanel>

        <StackPanel Grid.Row="4">
            <TextBlock Text="PLAN VIEWS (search, then tick the checkboxes)" Style="{StaticResource SectionLabel}"/>
            <TextBox x:Name="txt_filter" TextChanged="txt_filter_changed"/>
            <CheckBox x:Name="chk_show_selected_only" Content="Show only selected views"
                      Checked="chk_show_selected_only_changed" Unchecked="chk_show_selected_only_changed"
                      Margin="0,6,0,0"/>
            <Grid Margin="0,6,0,0">
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <Button x:Name="btn_select_all" Grid.Column="0" Content="Select All (visible)" Click="btn_select_all_click" Style="{StaticResource SmallButton}"/>
                <Button x:Name="btn_select_none" Grid.Column="1" Content="Select None (visible)" Click="btn_select_none_click" Style="{StaticResource SmallButton}"/>
                <TextBlock Grid.Column="2" x:Name="txt_selection_count" Foreground="#FF9B9B9B" FontSize="11"
                           HorizontalAlignment="Right" VerticalAlignment="Center" Text="0 view(s) selected"/>
            </Grid>
        </StackPanel>

        <ListBox x:Name="lst_plan_views" Grid.Row="6" Margin="0,4,0,0" MinHeight="240"
                 SelectionMode="Extended"
                 ScrollViewer.CanContentScroll="False"
                 PreviewMouseWheel="lst_plan_views_preview_mouse_wheel"
                 PreviewMouseUp="lst_plan_views_mouse_up">
            <ListBox.ItemTemplate>
                <DataTemplate>
                    <CheckBox Content="{Binding Name}" IsChecked="{Binding IsChecked, Mode=TwoWay}"/>
                </DataTemplate>
            </ListBox.ItemTemplate>
        </ListBox>

        <Grid Grid.Row="7" Margin="0,10,0,0">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0">
                <TextBlock Text="PLAN VIEW TEMPLATE" Style="{StaticResource RequiredLabel}"/>
                <ComboBox x:Name="cmb_plan_template"/>
            </StackPanel>
            <StackPanel Grid.Column="2">
                <TextBlock Text="ELEVATION VIEW TEMPLATE" Style="{StaticResource RequiredLabel}"/>
                <ComboBox x:Name="cmb_elev_template"/>
            </StackPanel>
        </Grid>

        <StackPanel Grid.Row="8" Margin="0,10,0,0">
            <TextBlock Text="SHEET NAME (required - Revit's built-in Sheet Name, applied to every sheet in this batch)" Style="{StaticResource RequiredLabel}"/>
            <TextBox x:Name="txt_sheet_name" Style="{StaticResource RequiredTextBox}"/>
        </StackPanel>

        <Grid Grid.Row="10" Margin="0,14,0,0">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="12"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0">
                <TextBlock Text="STARTING SHEET NUMBER (required)" Style="{StaticResource RequiredLabel}"/>
                <Grid>
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>
                    <TextBox x:Name="txt_start_number" Grid.Column="0" Style="{StaticResource RequiredTextBox}"/>
                    <Button x:Name="btn_find_next" Grid.Column="1" Content="Find Next"
                            Click="btn_find_next_click" Style="{StaticResource SmallButton}"
                            VerticalAlignment="Center" Margin="6,0,0,0"/>
                </Grid>
                <TextBlock x:Name="txt_base_status" Text="" Foreground="#FF7A7A7A" FontSize="9"
                           Margin="0,3,0,0" TextWrapping="Wrap"/>
            </StackPanel>
            <StackPanel Grid.Column="2">
                <TextBlock Text="ELEVATION GRID LAYOUT" Style="{StaticResource SectionLabel}"/>
                <ComboBox x:Name="cmb_layout"/>
            </StackPanel>
        </Grid>

        <Grid Grid.Row="11" Margin="0,0,0,0">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="12"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0">
                <TextBlock Text="HORIZONTAL OFFSET (mm)" Style="{StaticResource SectionLabel}"/>
                <TextBox x:Name="txt_h_offset"/>
            </StackPanel>
            <StackPanel Grid.Column="2">
                <TextBlock Text="VERTICAL OFFSET (mm)" Style="{StaticResource SectionLabel}"/>
                <TextBox x:Name="txt_offset"/>
            </StackPanel>
        </Grid>

        <Grid Grid.Row="12" Margin="0,0,0,0">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="0.7*"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0">
                <TextBlock Text="SHEET MARGIN (mm)" Style="{StaticResource SectionLabel}"/>
                <TextBox x:Name="txt_margin"/>
                <TextBlock Text="Reserved border space when checking if elevations fit on one sheet."
                           Foreground="#FF7A7A7A" FontSize="9" Margin="0,3,0,0" TextWrapping="Wrap"/>
            </StackPanel>
            <StackPanel Grid.Column="2">
                <TextBlock Text="ELEVATION VIEWPORT TYPE" Style="{StaticResource SectionLabel}"/>
                <ComboBox x:Name="cmb_viewport_type"/>
                <TextBlock Text="A 'No Title' type shrinks each viewport's footprint, helping 4 fit on one sheet."
                           Foreground="#FF7A7A7A" FontSize="9" Margin="0,3,0,0" TextWrapping="Wrap"/>
            </StackPanel>
            <StackPanel Grid.Column="4">
                <TextBlock Text="RECLAIM EMPTY SHEETS PREFIX" Style="{StaticResource SectionLabel}"/>
                <TextBox x:Name="txt_temp_prefix"/>
                <TextBlock Text="Used to rename empty sheets freed up for reuse (e.g. TEMP-001)."
                           Foreground="#FF7A7A7A" FontSize="9" Margin="0,3,0,0" TextWrapping="Wrap"/>
            </StackPanel>
        </Grid>

        <Grid Grid.Row="13" Margin="0,0,0,0">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0">
                <TextBlock Text="ROOM NUMBER &amp; NAME CHARACTER LIMIT" Style="{StaticResource SectionLabel}"/>
                <TextBox x:Name="txt_title_char_limit"/>
                <TextBlock Text="Room number + name is written to 'OLA Sheet Title 2'; overflow (and SHEET 1/2 if elevations split) spills into 'OLA Sheet Title 3'."
                           Foreground="#FF7A7A7A" FontSize="9" Margin="0,8,0,0" TextWrapping="Wrap"/>
            </StackPanel>
        </Grid>
        <TextBlock Grid.Row="13" Text="" Visibility="Collapsed"/>

        <CheckBox Grid.Row="14" x:Name="chk_separate_transactions"
                   Content="Separate undo step per plan view (unchecked = one undo step for the whole batch)"
                   Margin="0,12,0,0"/>

        <StackPanel Grid.Row="16" Margin="0,10,0,0">
            <TextBlock Text="AUDIT LOG FOLDER (recommended - a run history is appended here)" Style="{StaticResource AuditLabel}"/>
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <TextBox x:Name="txt_audit_log_folder" Grid.Column="0" Style="{StaticResource RequiredTextBox}"/>
                <Button x:Name="btn_browse_log_folder" Grid.Column="1" Content="Browse..."
                        Click="btn_browse_log_folder_click" Style="{StaticResource SmallButton}"
                        VerticalAlignment="Center" Margin="6,0,0,0"/>
            </Grid>
            <TextBlock Text="Leave blank to skip logging. Remembered per-project for next time."
                       Foreground="#FF7A7A7A" FontSize="9" Margin="0,3,0,0" TextWrapping="Wrap"/>
        </StackPanel>

        <TextBlock Grid.Row="17" x:Name="txt_error" Foreground="#FFFF6B6B" FontSize="11" Margin="0,10,0,0"
                   TextWrapping="Wrap" Visibility="Collapsed"/>

        <StackPanel Grid.Row="18" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,14,0,0">
            <Button x:Name="btn_cancel" Content="Close" Click="btn_cancel_click" Style="{StaticResource SecondaryButton}"/>
            <Button x:Name="btn_ok" Content="Place Views..." Click="btn_ok_click"/>
        </StackPanel>
    </Grid>
    </ScrollViewer>
</Window>
"""


class PlaceViewsForm(forms.WPFWindow):
    def __init__(self, xaml_source, title_blocks, plan_views, saved_sets, prefs, preselected_names=None):
        forms.WPFWindow.__init__(self, xaml_source, literal_string=True)
        self.Title = "Place RLS Views — v{}".format(TOOL_VERSION)

        self.title_blocks = title_blocks
        self.all_plan_views = plan_views
        self.saved_sets = saved_sets
        self.prefs = prefs
        self.result = None  # populated on successful OK
        self._place_clicked = False
        self._validated = None

        title_block_names = [title_block_display_name(tb) for tb in title_blocks]
        self.cmb_title_block.ItemsSource = title_block_names
        if prefs.get("title_block_name") in title_block_names:
            self.cmb_title_block.SelectedIndex = title_block_names.index(prefs["title_block_name"])
        elif title_blocks:
            self.cmb_title_block.SelectedIndex = 0

        self.txt_offset.Text = str(prefs.get("offset_mm", DEFAULT_ELEV_GRID_VERTICAL_OFFSET_MM))
        self.txt_h_offset.Text = str(prefs.get("h_offset_mm", DEFAULT_ELEV_GRID_HORIZONTAL_OFFSET_MM))
        self.txt_start_number.Text = prefs.get("start_number", DEFAULT_START_SHEET_NUMBER)
        self.txt_temp_prefix.Text = prefs.get("temp_prefix", DEFAULT_TEMP_PREFIX)
        self._auto_detect_next_base(initial=True)
        self.txt_margin.Text = str(prefs.get("margin_mm", DEFAULT_TITLE_BLOCK_MARGIN_MM))
        self.txt_title_char_limit.Text = str(prefs.get("title_char_limit", DEFAULT_TITLE_CHAR_LIMIT))
        self.txt_sheet_name.Text = prefs.get("sheet_name", DEFAULT_SHEET_NAME)
        self.txt_audit_log_folder.Text = load_last_audit_log_folder()

        self.cmb_layout.ItemsSource = LAYOUT_OPTIONS
        layout_index = prefs.get("layout_index", 0)
        if not (0 <= layout_index < len(LAYOUT_OPTIONS)):
            layout_index = 0
        self.cmb_layout.SelectedIndex = layout_index

        self.viewport_types = get_selectable_viewport_types()
        viewport_type_names = [viewport_type_display_name(t) for t in self.viewport_types]
        self.cmb_viewport_type.ItemsSource = ["(default)"] + viewport_type_names
        pref_vp_name = prefs.get("viewport_type_name", "")
        if pref_vp_name and pref_vp_name in viewport_type_names:
            self.cmb_viewport_type.SelectedIndex = viewport_type_names.index(pref_vp_name) + 1
        elif self.viewport_types:
            self.cmb_viewport_type.SelectedIndex = default_viewport_type_index(self.viewport_types) + 1
        else:
            self.cmb_viewport_type.SelectedIndex = 0

        # View templates
        self.view_templates = get_selectable_view_templates()
        template_names = ["(none)"] + [t.Name for t in self.view_templates]
        self.cmb_plan_template.ItemsSource = template_names
        self.cmb_elev_template.ItemsSource = template_names
        pref_plan_tmpl = prefs.get("plan_template_name", "")
        pref_elev_tmpl = prefs.get("elev_template_name", "")
        self.cmb_plan_template.SelectedIndex = (template_names.index(pref_plan_tmpl)
                                                 if pref_plan_tmpl in template_names else 0)
        self.cmb_elev_template.SelectedIndex = (template_names.index(pref_elev_tmpl)
                                                 if pref_elev_tmpl in template_names else 0)

        self.chk_separate_transactions.IsChecked = True
        self.chk_audit_sheet_name.IsChecked = False
        self.txt_sheet_name.IsEnabled = False
        self.chk_renumber_existing.IsChecked = False
        self.chk_show_selected_only.IsChecked = False
        self.txt_sheet_name.IsEnabled = True

        # One persistent SelectableItem per plan view - checked state survives
        # filtering since we always reuse these same objects.
        preselected = set(preselected_names or [])
        self.plan_view_items = [SelectableItem(v.Name, checked=(v.Name in preselected)) for v in plan_views]

        self.refresh_saved_sets_combo()
        self.refresh_plan_views_list()

    # -- population helpers --------------------------------------------

    def refresh_saved_sets_combo(self):
        names = [NONE_SET_LABEL] + sorted(self.saved_sets.keys())
        self.cmb_saved_sets.ItemsSource = names
        self.cmb_saved_sets.SelectedIndex = 0

    def refresh_plan_views_list(self, filter_text=""):
        filter_text = (filter_text or "").lower()
        show_selected_only = bool(self.chk_show_selected_only.IsChecked)
        visible = [
            item for item in self.plan_view_items
            if filter_text in item.Name.lower() and (not show_selected_only or item.IsChecked)
        ]
        self.lst_plan_views.ItemsSource = visible
        self.update_selection_count()

    def update_selection_count(self):
        count = sum(1 for item in self.plan_view_items if item.IsChecked)
        self.txt_selection_count.Text = "{} view(s) selected".format(count)

    def show_error(self, message):
        self.txt_error.Text = message
        self.txt_error.Visibility = Visibility.Visible

    def clear_error(self):
        self.txt_error.Text = ""
        self.txt_error.Visibility = Visibility.Collapsed

    # -- event handlers ---------------------------------------------------

    def txt_filter_changed(self, sender, args):
        self.refresh_plan_views_list(self.txt_filter.Text)

    def chk_show_selected_only_changed(self, sender, args):
        self.refresh_plan_views_list(self.txt_filter.Text)

    def lst_plan_views_preview_mouse_wheel(self, sender, args):
        """Forward mouse wheel events from the ListBox up to the outer
        ScrollViewer so the window scrolls normally when hovering the list."""
        if not args.Handled:
            args.Handled = True
            from System.Windows import RoutedEventArgs
            from System.Windows.Controls import ScrollViewer
            # Walk up the visual tree to find the outer ScrollViewer
            parent = sender
            try:
                while parent is not None:
                    parent = System.Windows.Media.VisualTreeHelper.GetParent(parent)
                    if isinstance(parent, ScrollViewer):
                        parent.ScrollToVerticalOffset(parent.VerticalOffset - args.Delta / 3.0)
                        break
            except Exception:
                pass

    def lst_plan_views_mouse_up(self, sender, args):
        # Checkbox clicks bubble up to the ListBox; recompute count afterward.
        # When only showing selected views, unticking one should drop it
        # from view immediately, so re-filter rather than just recounting.
        if bool(self.chk_show_selected_only.IsChecked):
            self.refresh_plan_views_list(self.txt_filter.Text)
        else:
            self.update_selection_count()

    def _get_template_id(self, cmb):
        idx = cmb.SelectedIndex
        if idx <= 0 or not self.view_templates:
            return DB.ElementId.InvalidElementId
        try:
            return self.view_templates[idx - 1].Id
        except Exception:
            return DB.ElementId.InvalidElementId

    def _get_template_name(self, cmb):
        idx = cmb.SelectedIndex
        if idx <= 0 or not self.view_templates:
            return ""
        try:
            return self.view_templates[idx - 1].Name
        except Exception:
            return ""

    def chk_audit_sheet_name_changed(self, sender, args):
        self.txt_sheet_name.IsEnabled = bool(self.chk_audit_sheet_name.IsChecked)

    def _auto_detect_next_base(self, initial=False):
        """Check whether the current Starting Sheet Number is already a
        complete tool-generated base; if so, silently advance the field to
        the next available one (only meant to run once, on dialog load)."""
        typed = (self.txt_start_number.Text or "").strip()
        if not typed:
            return
        temp_prefix = (self.txt_temp_prefix.Text or "").strip() or DEFAULT_TEMP_PREFIX
        try:
            suggested = find_next_available_base(typed, temp_prefix)
        except Exception as e:
            self.txt_base_status.Text = "Could not check sheet number availability: {}".format(e)
            return

        if suggested != typed:
            if initial:
                self.txt_start_number.Text = suggested
                self.txt_base_status.Text = "'{}' is already in use - auto-advanced to next available base: '{}'.".format(typed, suggested)
            else:
                self.txt_base_status.Text = "Note: '{}' is already in use - next available is '{}'.".format(typed, suggested)
        else:
            self.txt_base_status.Text = "Base '{}' is available.".format(typed)

    def btn_find_next_click(self, sender, args):
        typed = (self.txt_start_number.Text or "").strip() or DEFAULT_START_SHEET_NUMBER
        temp_prefix = (self.txt_temp_prefix.Text or "").strip() or DEFAULT_TEMP_PREFIX
        try:
            suggested = find_next_available_base(typed, temp_prefix)
        except Exception as e:
            self.show_error("Could not detect the next available sheet number: {}".format(e))
            return
        self.txt_start_number.Text = suggested
        self.txt_base_status.Text = "Set to next available base: '{}'.".format(suggested)
        self.clear_error()

    def btn_browse_log_folder_click(self, sender, args):
        dlg = FolderBrowserDialog()
        dlg.Description = "Choose a folder for the Place Views audit log"
        current = (self.txt_audit_log_folder.Text or "").strip()
        if current:
            try:
                dlg.SelectedPath = current
            except Exception:
                pass
        if dlg.ShowDialog() == DialogResult.OK:
            self.txt_audit_log_folder.Text = dlg.SelectedPath

    def btn_select_all_click(self, sender, args):
        for item in list(self.lst_plan_views.ItemsSource):
            item.IsChecked = True
        self.refresh_plan_views_list(self.txt_filter.Text)

    def btn_select_none_click(self, sender, args):
        for item in list(self.lst_plan_views.ItemsSource):
            item.IsChecked = False
        self.refresh_plan_views_list(self.txt_filter.Text)

    def cmb_saved_sets_selection_changed(self, sender, args):
        self.load_selected_set()

    def btn_load_set_click(self, sender, args):
        self.load_selected_set()

    def load_selected_set(self):
        set_name = self.cmb_saved_sets.SelectedItem
        if not set_name or set_name == NONE_SET_LABEL:
            return

        set_entries = self.saved_sets.get(set_name, [])
        # Handle old-format (plain strings) that slipped through migration
        if set_entries and isinstance(set_entries[0], basestring):
            set_entries = migrate_set_entries_to_id_format(set_entries, self.all_plan_views)
            self.saved_sets[set_name] = set_entries
            save_view_sets(self.saved_sets)

        resolved_views, corrected_names, missing_names = resolve_saved_set(set_entries, self.all_plan_views)

        # Auto-write corrected names back to the stored set so it self-heals
        if corrected_names:
            id_to_view_heal = {int(v.Id.IntegerValue): v for v in self.all_plan_views}
            name_to_view_heal = {v.Name.strip().lower(): v for v in self.all_plan_views}
            updated_entries = []
            for entry in set_entries:
                stored_id = entry.get("id", -1)
                stored_name = entry.get("name", "")
                found = id_to_view_heal.get(int(stored_id)) if stored_id != -1 else None
                if found is None:
                    found = name_to_view_heal.get(stored_name.strip().lower())
                if found is not None:
                    updated_entries.append({"id": int(found.Id.IntegerValue), "name": found.Name})
                else:
                    updated_entries.append(entry)  # keep as-is for missing ones
            self.saved_sets[set_name] = updated_entries
            save_view_sets(self.saved_sets)

        resolved_names = set(v.Name for v in resolved_views)
        for item in self.plan_view_items:
            item.IsChecked = item.Name in resolved_names

        self.txt_filter.Text = ""
        self.refresh_plan_views_list()

        if corrected_names and missing_names:
            self.show_error(
                u"⚠ {} room name(s) auto-updated in this set; {} view(s) no longer exist: {}".format(
                    len(corrected_names), len(missing_names), u", ".join(missing_names)))
        elif corrected_names:
            self.show_error(
                u"⚠ {} room name(s) were auto-updated in this set (room renamed since last save).".format(
                    len(corrected_names)))
        elif missing_names:
            self.show_error("Note: {} saved view(s) from this set no longer exist: {}".format(
                len(missing_names), ", ".join(missing_names)))
        else:
            self.clear_error()

    def btn_update_set_click(self, sender, args):
        set_name = self.cmb_saved_sets.SelectedItem
        if not set_name or set_name == NONE_SET_LABEL:
            self.show_error("Select a saved set from the dropdown first, then click Update.")
            return

        selected_names = [item.Name for item in self.plan_view_items if item.IsChecked]
        if not selected_names:
            self.show_error("Tick at least one plan view before updating the set.")
            return

        id_map = {v.Name: v for v in self.all_plan_views}
        self.saved_sets[set_name] = [
            {"id": int(id_map[n].Id.IntegerValue) if n in id_map else -1, "name": n}
            for n in selected_names
        ]
        save_view_sets(self.saved_sets)
        self.clear_error()

    def btn_save_set_click(self, sender, args):
        selected_names = [item.Name for item in self.plan_view_items if item.IsChecked]
        if not selected_names:
            self.show_error("Tick one or more plan views before saving a set.")
            return
        set_name = forms.ask_for_string(default="", prompt="Name for this set:", title="Save Plan View Set")
        if set_name:
            id_map = {v.Name: v for v in self.all_plan_views}
            self.saved_sets[set_name] = [
                {"id": int(id_map[n].Id.IntegerValue) if n in id_map else -1, "name": n}
                for n in selected_names
            ]
            save_view_sets(self.saved_sets)
            self.refresh_saved_sets_combo()
            self.cmb_saved_sets.SelectedItem = set_name
            self.clear_error()

    def btn_delete_set_click(self, sender, args):
        set_name = self.cmb_saved_sets.SelectedItem
        if not set_name or set_name == NONE_SET_LABEL:
            return
        confirm = show_confirm("Delete saved set '{}'?".format(set_name), title="Delete Set")
        if confirm:
            self.saved_sets.pop(set_name, None)
            save_view_sets(self.saved_sets)
            self.refresh_saved_sets_combo()

    def btn_sync_all_click(self, sender, args):
        set_name = self.cmb_saved_sets.SelectedItem
        if set_name and set_name != NONE_SET_LABEL:
            # Use the saved set
            set_entries = self.saved_sets.get(set_name, [])
            if set_entries and isinstance(set_entries[0], basestring):
                set_entries = migrate_set_entries_to_id_format(set_entries, self.all_plan_views)
            plan_views, _corrected, missing = resolve_saved_set(set_entries, self.all_plan_views)
            source_label = "set '{}'".format(set_name)
        else:
            # No saved set — use currently checked views
            name_to_view = {v.Name: v for v in self.all_plan_views}
            plan_views = [name_to_view[item.Name] for item in self.plan_view_items
                          if item.IsChecked and item.Name in name_to_view]
            missing = []
            set_name = None
            source_label = "current selection"

        if not plan_views:
            if set_name:
                self.show_error("None of the views in set '{}' could be found in this project.".format(set_name))
            else:
                self.show_error("No plan views are currently checked. Tick some views in the list first.")
            return

        audit_sheet_name = bool(self.chk_audit_sheet_name.IsChecked)
        renumber_existing = bool(self.chk_renumber_existing.IsChecked)

        confirm_msg = "This will check {} plan view(s) from {} and update the OLA Sheet Title 2/3 " \
                      "shared parameters on any already-placed sheets to match current room info".format(
                          len(plan_views), source_label)
        if audit_sheet_name:
            confirm_msg += ", and also update Sheet Name (Line 1) to match the dialog's current value"
        if renumber_existing:
            confirm_msg += (
                ". IMPORTANT: renumbering is also ON - any of these sheets not already on the "
                "base-0/-1/-2 scheme will have their Sheet Number CHANGED, starting from the "
                "Starting Sheet Number field below. This affects real, already-placed sheets - "
                "double check that number is correct before continuing")
        confirm_msg += ". Continue?"

        confirm = show_confirm(confirm_msg, title="Sync Sheet Titles")
        if not confirm:
            return

        sheet_name_value = (self.txt_sheet_name.Text or "").strip() or DEFAULT_SHEET_NAME
        plan_title_suffix = DEFAULT_PLAN_TITLE_SUFFIX
        elev_title_suffix = DEFAULT_ELEV_TITLE_SUFFIX
        try:
            title_char_limit = int(float(self.txt_title_char_limit.Text))
            if title_char_limit <= 0:
                raise ValueError
        except ValueError:
            title_char_limit = DEFAULT_TITLE_CHAR_LIMIT

        # Persist these two fields so future sessions default to what was
        # actually used here, regardless of which action (Place Views or
        # this Maintenance sync) last set them.
        config.set_option(LAST_SHEET_NAME_KEY, sheet_name_value)
        config.set_option(LAST_TITLE_CHAR_LIMIT_KEY, str(title_char_limit))
        script.save_config()

        allocator = None
        if renumber_existing:
            start_number = (self.txt_start_number.Text or "").strip() or DEFAULT_START_SHEET_NUMBER
            temp_prefix = (self.txt_temp_prefix.Text or "").strip() or DEFAULT_TEMP_PREFIX
            allocator = SheetNumberAllocator(start_number, temp_prefix)
            allocator.refresh()  # ensure map reflects current document state

        maint_viewport_type_id = None
        vp_idx = self.cmb_viewport_type.SelectedIndex
        if vp_idx > 0 and self.viewport_types:
            try:
                maint_viewport_type_id = self.viewport_types[vp_idx - 1].Id
            except Exception:
                pass

        updated, renumbered, detail_lines = sync_sheet_names_for_views(
            plan_views, sheet_name_value, plan_title_suffix, elev_title_suffix, title_char_limit,
            audit_sheet_name, renumber_existing, allocator,
            self._get_template_id(self.cmb_plan_template),
            self._get_template_id(self.cmb_elev_template),
            maint_viewport_type_id)

        # Write to audit log if folder is set
        audit_log_folder = (self.txt_audit_log_folder.Text or "").strip()
        if audit_log_folder:
            try:
                import os, datetime
                if not os.path.isdir(audit_log_folder):
                    os.makedirs(audit_log_folder)
                log_path = os.path.join(audit_log_folder, "PlaceViewsAuditLog.txt")
                with open(log_path, "a") as f:
                    f.write("=" * 70 + "\n")
                    f.write("MAINTENANCE RUN: {}\n".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    f.write("Project: {}\n".format(get_current_project_key()))
                    f.write("Source: {}\n".format(source_label))
                    f.write("Sheet Name updated: {} | Renumber: {}\n".format(audit_sheet_name, renumber_existing))
                    f.write("-" * 70 + "\n")
                    for line in detail_lines:
                        f.write(line + "\n")
                    f.write("Sheets updated: {} | Sheets renumbered: {}\n".format(updated, renumbered))
                    f.write("\n")
                save_last_audit_log_folder(audit_log_folder)
            except Exception as e:
                output.print_md("**Warning:** could not write audit log: {}".format(e))

        # --- Colour-coded maintenance report in the pyRevit output pane ---
        output.print_md("---")
        output.print_md("### Place RLS Views — v{} &nbsp; Maintenance Report".format(TOOL_VERSION))
        output.print_md(
            "<span style='color:#4DB6AC'>&#9632; Updated: {}</span> &nbsp;&nbsp; "
            "<span style='color:#4FC3F7'>&#9632; Renumbered: {}</span>".format(updated, renumbered))
        output.print_md("")
        for line in detail_lines:
            if line.startswith("RENUMBERED"):
                output.print_md(
                    "<span style='color:#4FC3F7'>**RENUMBERED**</span> &nbsp; `{}`".format(
                        line[len("RENUMBERED"):].lstrip(" |")))
            elif line.startswith("SYNCED"):
                output.print_md(
                    "<span style='color:#4DB6AC'>**SYNCED**</span> &nbsp; `{}`".format(
                        line[len("SYNCED"):].lstrip(" |")))
            elif line.startswith("NOT PLACED"):
                output.print_md(
                    "<span style='color:#9E9E9E'>**NOT PLACED**</span> &nbsp; `{}`".format(
                        line[len("NOT PLACED"):].lstrip(" |")))
            elif line.startswith("ERROR"):
                output.print_md(
                    "<span style='color:#EF5350'>**ERROR**</span> &nbsp; `{}`".format(
                        line[len("ERROR"):].lstrip(" |")))
            elif line.startswith("NO_MARKER"):
                output.print_md(
                    "<span style='color:#FFB74D'>**NO MARKER**</span> &nbsp; `{}`".format(
                        line[len("NO_MARKER"):].lstrip(" |")))
            else:
                output.print_md("`{}`".format(line))
        output.print_md("---")

        message = "Done. {} sheet(s) updated.".format(updated)
        if renumber_existing:
            message += " {} sheet(s) renumbered.".format(renumbered)
        if missing:
            message += " Note: {} saved view(s) no longer exist: {}".format(len(missing), ", ".join(missing))
        show_info(message, title="Sync Complete")

        # Refresh plan view list from the model — maintenance may have renamed views,
        # so all_plan_views and plan_view_items must be rebuilt to show new names.
        # Preserve checked state by ElementId so ticked items stay ticked.
        checked_ids = set()
        old_id_map = {v.Name: v for v in self.all_plan_views}
        for item in self.plan_view_items:
            if item.IsChecked:
                v = old_id_map.get(item.Name)
                if v is not None:
                    checked_ids.add(int(v.Id.IntegerValue))
        self.all_plan_views = get_selectable_plan_views()
        new_id_map = {int(v.Id.IntegerValue): v for v in self.all_plan_views}
        self.plan_view_items = [
            SelectableItem(v.Name, checked=(int(v.Id.IntegerValue) in checked_ids))
            for v in self.all_plan_views
        ]
        self.refresh_plan_views_list()
        self.clear_error()

    def validate_inputs(self):
        if self.cmb_title_block.SelectedIndex < 0:
            self.show_error("Please select a title block.")
            return None

        selected_names = [item.Name for item in self.plan_view_items if item.IsChecked]
        if not selected_names:
            self.show_error("Please tick at least one plan view.")
            return None

        try:
            offset_mm = float(self.txt_offset.Text)
        except ValueError:
            self.show_error("Vertical offset must be a number.")
            return None

        try:
            h_offset_mm = float(self.txt_h_offset.Text)
        except ValueError:
            self.show_error("Horizontal offset must be a number.")
            return None

        start_number = (self.txt_start_number.Text or "").strip()
        if not start_number:
            self.show_error("Please enter a starting sheet number.")
            return None

        try:
            margin_mm = float(self.txt_margin.Text)
            if margin_mm < 0:
                raise ValueError
        except ValueError:
            self.show_error("Sheet margin must be a non-negative number.")
            return None

        layout_index = self.cmb_layout.SelectedIndex
        if layout_index < 0:
            layout_index = 0

        vp_type_index = self.cmb_viewport_type.SelectedIndex
        viewport_type = None
        viewport_type_name = ""
        if vp_type_index > 0 and self.viewport_types:
            viewport_type = self.viewport_types[vp_type_index - 1]
            viewport_type_name = viewport_type_display_name(viewport_type)

        plan_title_suffix = DEFAULT_PLAN_TITLE_SUFFIX
        elev_title_suffix = DEFAULT_ELEV_TITLE_SUFFIX

        try:
            title_char_limit = int(float(self.txt_title_char_limit.Text))
            if title_char_limit <= 0:
                raise ValueError
        except ValueError:
            self.show_error("Title 2 char limit must be a positive whole number.")
            return None

        sheet_name_value = (self.txt_sheet_name.Text or "").strip()
        if not sheet_name_value:
            self.show_error("Please enter a Sheet Name.")
            return None

        temp_prefix = (self.txt_temp_prefix.Text or "").strip()
        if not temp_prefix:
            self.show_error("Please enter a Reclaim Empty Sheets Prefix.")
            return None

        return {
            "title_block": self.title_blocks[self.cmb_title_block.SelectedIndex],
            "title_block_name": title_block_display_name(self.title_blocks[self.cmb_title_block.SelectedIndex]),
            "plan_view_names": selected_names,
            "offset_mm": offset_mm,
            "h_offset_mm": h_offset_mm,
            "start_number": start_number,
            "margin_mm": margin_mm,
            "layout_index": layout_index,
            "layout_code": LAYOUT_CODES[layout_index],
            "viewport_type": viewport_type,
            "viewport_type_name": viewport_type_name,
            "plan_template_id": self._get_template_id(self.cmb_plan_template),
            "plan_template_name": self._get_template_name(self.cmb_plan_template),
            "elev_template_id": self._get_template_id(self.cmb_elev_template),
            "elev_template_name": self._get_template_name(self.cmb_elev_template),
            "plan_title_suffix": plan_title_suffix,
            "elev_title_suffix": elev_title_suffix,
            "title_char_limit": title_char_limit,
            "sheet_name": sheet_name_value,
            "temp_prefix": temp_prefix,
            "audit_log_folder": (self.txt_audit_log_folder.Text or "").strip(),
            "separate_transactions": bool(self.chk_separate_transactions.IsChecked),
        }

    def btn_cancel_click(self, sender, args):
        self._place_clicked = False
        self.Close()

    def btn_ok_click(self, sender, args):
        validated = self.validate_inputs()
        if validated is None:
            return  # inline error already shown, keep dialog open
        self._validated = validated
        self._place_clicked = True
        self.Close()


def show_place_views_dialog():
    title_blocks = get_selectable_title_blocks()
    if not title_blocks:
        forms.alert("No title block types were found loaded in this project.", exitscript=True)

    plan_views = get_selectable_plan_views()
    if not plan_views:
        forms.alert("No floor plan views were found in this project.", exitscript=True)

    saved_sets = load_saved_view_sets()
    prefs = load_last_used_prefs()
    preselected_names = None

    while True:
        dlg = PlaceViewsForm(FORM_XAML, title_blocks, plan_views, saved_sets, prefs, preselected_names)
        dlg.ShowDialog()

        if not dlg._place_clicked:
            return None, None  # user cancelled

        validated = dlg._validated
        name_to_view = {v.Name: v for v in plan_views}
        selected_views = [name_to_view[n] for n in validated["plan_view_names"] if n in name_to_view]

        preview_lines = compute_preview(selected_views, validated["start_number"], validated["temp_prefix"])
        preview_dlg = PreviewForm(PREVIEW_XAML, preview_lines)
        preview_dlg.ShowDialog()

        if preview_dlg.confirmed:
            save_last_used_prefs(validated)
            return validated, selected_views

        # user clicked "Back" in preview - reopen the main dialog, carrying
        # forward what they had ticked and typed so nothing is lost
        saved_sets = load_saved_view_sets()
        preselected_names = validated["plan_view_names"]
        prefs = validated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def write_audit_log(folder, validated, results):
    """Append a timestamped run block to '{folder}\\PlaceViewsAuditLog.txt'
    summarizing what this batch did. Returns (success, message)."""
    if not folder:
        return False, "no folder specified"

    try:
        import os
        import datetime
        if not os.path.isdir(folder):
            os.makedirs(folder)
        log_path = os.path.join(folder, "PlaceViewsAuditLog.txt")

        lines = []
        lines.append("=" * 70)
        lines.append("RUN: {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        lines.append("Project: {}".format(get_current_project_key()))
        lines.append("Sheet Name (Line 1): {}".format(validated.get("sheet_name", "")))
        lines.append("Title Block: {}".format(validated.get("title_block_name", "")))
        lines.append("Starting base: {}".format(validated.get("start_number", "")))
        lines.append("-" * 70)

        for r in results:
            if r["status"] == "created":
                lines.append("CREATED  | {:<30} | room: {} {} | plan: {} | elevations: {}".format(
                    r["plan_view_name"], r["room_number"], r["room_name"],
                    r["plan_sheet_number"], ", ".join(r["elev_sheet_numbers"])))
                for old, new in r["reclaims"]:
                    lines.append("           reclaimed: {} -> {}".format(old, new))
            elif r["status"] == "synced":
                lines.append("SYNCED   | {:<30} | room: {} {} | plan: {} | elevations: {}".format(
                    r["plan_view_name"], r["room_number"], r["room_name"],
                    r["plan_sheet_number"] or "-", ", ".join(r["elev_sheet_numbers"]) or "-"))
            else:
                lines.append("SKIPPED  | {:<30} | {}".format(r["plan_view_name"], r["reason"]))

        lines.append("")

        with open(log_path, "a") as f:
            f.write("\n".join(lines) + "\n")

        return True, log_path
    except Exception as e:
        return False, str(e)


def show_batch_summary(results, log_written, log_path_or_error, audit_log_folder):
    """Build and show the sleek end-of-batch summary popup."""
    created = [r for r in results if r["status"] == "created"]
    synced = [r for r in results if r["status"] == "synced"]
    skipped = [r for r in results if r["status"] == "skipped"]
    total_reclaims = sum(len(r["reclaims"]) for r in results)

    lines = []
    lines.append("Batch complete: {} of {} plan view(s) had new sheets created.".format(
        len(created), len(results)))
    lines.append("")
    lines.append("Created: {}".format(len(created)))
    lines.append("Synced (already placed): {}".format(len(synced)))
    lines.append("Skipped: {}".format(len(skipped)))
    if total_reclaims:
        lines.append("Empty sheets reclaimed: {}".format(total_reclaims))

    if skipped:
        lines.append("")
        lines.append("Skipped views:")
        for r in skipped:
            lines.append("- {}: {}".format(r["plan_view_name"], r["reason"]))

    lines.append("")
    if audit_log_folder:
        if log_written:
            lines.append("Audit log updated: {}".format(log_path_or_error))
        else:
            lines.append("Audit log NOT written ({}).".format(log_path_or_error))
    else:
        lines.append("No audit log folder was set - this run wasn't logged.")

    show_info("\n".join(lines), title="Batch Complete")


def main():
    validated, plan_views = show_place_views_dialog()
    if not validated:
        return  # user cancelled

    title_block = validated["title_block"]
    vertical_offset = validated["offset_mm"] / MM_PER_FOOT
    horizontal_offset = validated["h_offset_mm"] / MM_PER_FOOT
    layout_code = validated["layout_code"]
    margin_ft = validated["margin_mm"] / MM_PER_FOOT
    viewport_type = validated.get("viewport_type")
    viewport_type_id = viewport_type.Id if viewport_type is not None else None
    plan_template_id = validated.get("plan_template_id", DB.ElementId.InvalidElementId)
    elev_template_id = validated.get("elev_template_id", DB.ElementId.InvalidElementId)
    sheet_name_value = validated["sheet_name"]
    plan_title_suffix = validated["plan_title_suffix"]
    elev_title_suffix = validated["elev_title_suffix"]
    title_char_limit = validated["title_char_limit"]
    separate_transactions = validated["separate_transactions"]
    audit_log_folder = validated.get("audit_log_folder", "")
    allocator = SheetNumberAllocator(validated["start_number"], validated["temp_prefix"])

    results = []

    if separate_transactions:
        for plan_view in plan_views:
            with revit.Transaction("Place Views for '{}'".format(plan_view.Name)):
                if not title_block.IsActive:
                    title_block.Activate()
                results.append(process_plan_view(plan_view, title_block, vertical_offset, horizontal_offset,
                                                   layout_code, allocator, margin_ft, sheet_name_value,
                                                   plan_title_suffix, elev_title_suffix, title_char_limit,
                                                   viewport_type_id, plan_template_id, elev_template_id))
    else:
        with revit.Transaction("Place Plan and Elevations on Sheets"):
            if not title_block.IsActive:
                title_block.Activate()
            for plan_view in plan_views:
                results.append(process_plan_view(plan_view, title_block, vertical_offset, horizontal_offset,
                                                   layout_code, allocator, margin_ft, sheet_name_value,
                                                   plan_title_suffix, elev_title_suffix, title_char_limit,
                                                   viewport_type_id, plan_template_id, elev_template_id))

    succeeded = sum(1 for r in results if r["status"] == "created")
    synced_n  = sum(1 for r in results if r["status"] == "synced")
    skipped_n = sum(1 for r in results if r["status"] == "skipped")

    # --- Colour-coded per-room result table in the pyRevit output pane ---
    # Colours: green = created, teal = synced, amber = skipped, red = error
    output.print_md("---")
    output.print_md("### Place RLS Views — v{} &nbsp; Batch Report".format(TOOL_VERSION))
    output.print_md(
        "<span style='color:#4CAF50'>&#9632; CREATED: {}</span> &nbsp;&nbsp; "
        "<span style='color:#4DB6AC'>&#9632; SYNCED: {}</span> &nbsp;&nbsp; "
        "<span style='color:#FFB74D'>&#9632; SKIPPED: {}</span>".format(
            succeeded, synced_n, skipped_n))
    output.print_md("")

    for r in results:
        if r["status"] == "created":
            output.print_md(
                "<span style='color:#4CAF50'>**CREATED**</span> &nbsp; "
                "`{view}` &nbsp;|&nbsp; room: **{rn} {rm}** &nbsp;|&nbsp; "
                "plan: `{ps}` &nbsp;|&nbsp; elevations: `{es}`".format(
                    view=r["plan_view_name"],
                    rn=r["room_number"], rm=r["room_name"],
                    ps=r["plan_sheet_number"],
                    es=", ".join(r["elev_sheet_numbers"])))
            for old, new in r["reclaims"]:
                output.print_md(
                    "&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#9E9E9E'>"
                    "&#8627; reclaimed: `{}` &rarr; `{}`</span>".format(old, new))
        elif r["status"] == "synced":
            output.print_md(
                "<span style='color:#4DB6AC'>**SYNCED**</span> &nbsp; "
                "`{view}` &nbsp;|&nbsp; room: **{rn} {rm}** &nbsp;|&nbsp; "
                "plan: `{ps}` &nbsp;|&nbsp; elevations: `{es}`".format(
                    view=r["plan_view_name"],
                    rn=r["room_number"], rm=r["room_name"],
                    ps=r["plan_sheet_number"] or "-",
                    es=", ".join(r["elev_sheet_numbers"]) or "-"))
        else:
            output.print_md(
                "<span style='color:#FFB74D'>**SKIPPED**</span> &nbsp; "
                "`{view}` &nbsp;|&nbsp; <span style='color:#FF7043'>{reason}</span>".format(
                    view=r["plan_view_name"], reason=r["reason"]))

    output.print_md("---")

    log_written, log_path_or_error = write_audit_log(audit_log_folder, validated, results)
    if audit_log_folder:
        save_last_audit_log_folder(audit_log_folder)

    show_batch_summary(results, log_written, log_path_or_error, audit_log_folder)


main()
