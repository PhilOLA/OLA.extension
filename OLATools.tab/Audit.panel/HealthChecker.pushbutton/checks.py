# -*- coding: utf-8 -*-
"""
checks.py  —  Model Health Checker
All 28 check functions for the pyRevit Health Checker tool.
IronPython 2 / Revit 2024 compatible.

Each check returns a CheckResult dict:
{
    'status':  'OK' | 'WARN' | 'FAIL' | 'INFO' | 'ERROR',
    'count':   int,
    'items':   list of dicts (schema varies per check — see each fn),
    'message': str  (short summary for the detail panel header),
}

Import pattern in script.py:
    import checks
    result = checks.check_imported_dwgs(doc)
"""

import os
import re
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    FilteredWorksetCollector,
    BuiltInCategory,
    BuiltInParameter,
    WorksetKind,
    ImportInstance,
    FamilyInstance,
    SpatialElement,
    IndependentTag,
    RevitLinkInstance,
    CADLinkType,
    ExternalFileReference,
    LinkedFileStatus,
    ElementId,
    ViewType,
    ViewSheet,
    View,
    Group,
    ReferencePlane,
    DesignOption,
    SharedParameterElement,
)

from collections import defaultdict
from System import Exception as SysException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_param_str(elem, bip):
    """Return string value of a BuiltInParameter, or '' on failure."""
    try:
        p = elem.get_Parameter(bip)
        return p.AsString() or '' if p else ''
    except Exception:
        return ''


def _safe_param_id(elem, bip):
    """Return ElementId value of a BuiltInParameter, or InvalidElementId."""
    try:
        p = elem.get_Parameter(bip)
        return p.AsElementId() if p else ElementId.InvalidElementId
    except Exception:
        return ElementId.InvalidElementId


def _elem_level_name(doc, elem):
    """Return level name for an element, or '' if not applicable."""
    try:
        lvl_id = elem.LevelId
        if lvl_id and lvl_id != ElementId.InvalidElementId:
            lvl = doc.GetElement(lvl_id)
            return lvl.Name if lvl else ''
    except Exception:
        pass
    return ''


def _view_type_name(view):
    try:
        return str(view.ViewType)
    except Exception:
        return 'Unknown'


# ---------------------------------------------------------------------------
# Return schema factories
# ---------------------------------------------------------------------------

def _result(status, count, items, message):
    return {
        'status':  status,
        'count':   count,
        'items':   items,
        'message': message,
    }


def _ok(message='No issues found.'):
    return _result('OK', 0, [], message)


# ---------------------------------------------------------------------------
# CATEGORY 1 — MODEL BLOAT (8 checks)
# ---------------------------------------------------------------------------

def check_imported_dwgs(doc):
    """
    Finds ImportInstance elements where IsLinked == False.
    Items: [{id, name, view_name, in_3d}]
    FAIL if any found.
    """
    items = []
    try:
        collector = FilteredElementCollector(doc) \
            .OfClass(ImportInstance) \
            .WhereElementIsNotElementType()
        for imp in collector:
            try:
                if imp.IsLinked:
                    continue
                name = ''
                try:
                    cat = imp.Category
                    name = cat.Name if cat else 'Unknown'
                except Exception:
                    pass
                # get owning view
                view_name = 'All views'
                owner_id = imp.OwnerViewId
                if owner_id and owner_id != ElementId.InvalidElementId:
                    v = doc.GetElement(owner_id)
                    view_name = v.Name if v else 'Unknown view'
                in_3d = False
                if owner_id and owner_id != ElementId.InvalidElementId:
                    v = doc.GetElement(owner_id)
                    try:
                        in_3d = v.ViewType == ViewType.ThreeD
                    except Exception:
                        pass
                # try to get filename from parameters
                fname = _safe_param_str(imp, BuiltInParameter.IMPORT_SYMBOL_NAME)
                if not fname:
                    fname = name
                items.append({
                    'id':        imp.Id.IntegerValue,
                    'name':      fname,
                    'view_name': view_name,
                    'in_3d':     in_3d,
                })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error running check: {}'.format(str(ex)))

    if not items:
        return _ok('No imported DWGs found.')
    return _result('FAIL', len(items), items,
                   '{} imported DWG{} found — imports should always be links.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_unused_families(doc):
    """
    Family types with zero placed instances.
    Items: [{family_name, type_name, category, instance_count}]
    WARN if any found.
    """
    items = []
    try:
        # count placed instances per family type
        type_counts = defaultdict(int)
        instances = FilteredElementCollector(doc) \
            .OfClass(FamilyInstance) \
            .WhereElementIsNotElementType()
        for inst in instances:
            try:
                type_counts[inst.GetTypeId().IntegerValue] += 1
            except Exception:
                continue

        # iterate all family symbols (types)
        from Autodesk.Revit.DB import FamilySymbol, Family
        symbols = FilteredElementCollector(doc) \
            .OfClass(FamilySymbol) \
            .WhereElementIsElementType()
        for sym in symbols:
            try:
                if type_counts[sym.Id.IntegerValue] == 0:
                    fam = sym.Family
                    cat_name = ''
                    try:
                        cat_name = fam.FamilyCategory.Name
                    except Exception:
                        pass
                    items.append({
                        'family_name': fam.Name,
                        'type_name':   sym.Name,
                        'category':    cat_name,
                        'instance_count': 0,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No unused families found.')
    return _result('WARN', len(items), items,
                   '{} unused family type{} loaded with 0 instances.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_raster_images(doc):
    """
    Finds raster ImageInstance elements embedded in the model.
    Items: [{id, view_name}]
    FAIL if any found.
    """
    items = []
    try:
        from Autodesk.Revit.DB import ImageInstance
        imgs = FilteredElementCollector(doc) \
            .OfClass(ImageInstance) \
            .WhereElementIsNotElementType()
        for img in imgs:
            try:
                view_name = 'Unknown'
                owner_id = img.OwnerViewId
                if owner_id and owner_id != ElementId.InvalidElementId:
                    v = doc.GetElement(owner_id)
                    view_name = v.Name if v else 'Unknown'
                items.append({
                    'id':        img.Id.IntegerValue,
                    'view_name': view_name,
                })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No raster images found.')
    return _result('FAIL', len(items), items,
                   '{} embedded raster image{} found — embed as links instead.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_oversized_families(doc, face_threshold=5000):
    """
    Families with high face/edge counts (geometry complexity).
    Items: [{family_name, type_name, category, face_count}]
    WARN if face_count > face_threshold.

    Opt-in only — excluded from Full Audit and Quick bloat by default.
    Walking every family symbol's actual solid geometry via get_Geometry()
    is the one operation in this whole tool that can trigger a native
    crash in Revit's geometry kernel on certain corrupt/complex families —
    that kind of fault happens below the .NET/Python exception layer, so
    no try/except here can catch or recover from it. Mitigations below
    reduce (but cannot eliminate) the risk:
      - Options.DetailLevel = Coarse — cheaper tessellation than default
      - in-place families are skipped entirely — they're already covered
        by check_inplace_families and are the most likely to carry
        unstable/parametric geometry
      - hard cap on families processed, to bound worst-case runtime
    Run this one deliberately (it has its own 'Geometry check (slow)'
    preset) rather than as part of a routine audit.
    """
    items = []
    try:
        from Autodesk.Revit.DB import FamilySymbol, Options, ViewDetailLevel

        opts = Options()
        opts.ComputeReferences = False
        opts.IncludeNonVisibleObjects = False
        opts.DetailLevel = ViewDetailLevel.Coarse

        symbols = FilteredElementCollector(doc) \
            .OfClass(FamilySymbol) \
            .WhereElementIsElementType()

        seen_families = set()
        processed = 0
        MAX_FAMILIES = 3000  # hard cap — bounds worst-case runtime on huge libraries

        for sym in symbols:
            if processed >= MAX_FAMILIES:
                break
            try:
                fam = sym.Family
                if fam.IsInPlace:
                    # covered by check_inplace_families; also the most
                    # likely source of unstable parametric geometry here
                    continue
                fam_name = fam.Name
                if fam_name in seen_families:
                    continue
                seen_families.add(fam_name)
                processed += 1

                geom = sym.get_Geometry(opts)
                if geom is None:
                    continue
                face_count = 0
                for geom_obj in geom:
                    try:
                        solid = geom_obj
                        face_count += solid.Faces.Size
                    except Exception:
                        continue

                if face_count > face_threshold:
                    cat_name = ''
                    try:
                        cat_name = fam.FamilyCategory.Name
                    except Exception:
                        pass
                    items.append({
                        'family_name': fam_name,
                        'type_name':   sym.Name,
                        'category':    cat_name,
                        'face_count':  face_count,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No oversized families found (threshold: {} faces).'.format(face_threshold))
    return _result('WARN', len(items), items,
                   '{} family/families exceed {} faces — may impact performance.'.format(
                       len(items), face_threshold))


def check_inplace_families(doc, fail_threshold=10):
    """
    FamilyInstance elements where Family.IsInPlace == True.
    Items: [{id, name, category, level}]
    WARN > 0, FAIL > fail_threshold.
    """
    items = []
    try:
        instances = FilteredElementCollector(doc) \
            .OfClass(FamilyInstance) \
            .WhereElementIsNotElementType()
        for inst in instances:
            try:
                if not inst.Symbol.Family.IsInPlace:
                    continue
                cat_name = ''
                try:
                    cat_name = inst.Category.Name
                except Exception:
                    pass
                items.append({
                    'id':       inst.Id.IntegerValue,
                    'name':     inst.Name,
                    'category': cat_name,
                    'level':    _elem_level_name(doc, inst),
                })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No in-place families found.')
    status = 'FAIL' if len(items) > fail_threshold else 'WARN'
    return _result(status, len(items), items,
                   '{} in-place famil{} found — replace with loadable families.'.format(
                       len(items), 'ies' if len(items) != 1 else 'y'))


def check_groups(doc):
    """
    Model and detail groups — flags nested groups and groups with excluded members.
    Items: [{id, name, group_type, instance_count, nested, excluded_members, single_instance}]
    WARN if nested / excluded / single-instance.
    """
    items = []
    try:
        groups = FilteredElementCollector(doc) \
            .OfClass(Group) \
            .WhereElementIsNotElementType()

        # count instances per group type
        type_instance_counts = defaultdict(int)
        for g in groups:
            try:
                type_instance_counts[g.GetTypeId().IntegerValue] += 1
            except Exception:
                continue

        seen_types = set()
        # re-collect (iterator consumed)
        groups = FilteredElementCollector(doc) \
            .OfClass(Group) \
            .WhereElementIsNotElementType()

        for g in groups:
            try:
                type_id = g.GetTypeId().IntegerValue
                if type_id in seen_types:
                    continue
                seen_types.add(type_id)

                gt = doc.GetElement(g.GetTypeId())
                group_type_name = gt.Name if gt else 'Unknown'

                # model vs detail
                cat_name = ''
                try:
                    cat_name = g.Category.Name
                except Exception:
                    pass

                # nested: check if any member is itself a Group
                nested = False
                excluded = False
                try:
                    member_ids = g.GetMemberIds()
                    for mid in member_ids:
                        member = doc.GetElement(mid)
                        if isinstance(member, Group):
                            nested = True
                            break
                    # excluded members via GetExcludedMemberIds if available
                    try:
                        excl = g.GetExcludedMemberIds()
                        excluded = len(list(excl)) > 0
                    except Exception:
                        pass
                except Exception:
                    pass

                inst_count = type_instance_counts[type_id]
                single = inst_count == 1

                if nested or excluded or single:
                    items.append({
                        'id':              g.Id.IntegerValue,
                        'name':            group_type_name,
                        'group_type':      cat_name,
                        'instance_count':  inst_count,
                        'nested':          nested,
                        'excluded_members': excluded,
                        'single_instance': single,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No group issues found.')
    return _result('WARN', len(items), items,
                   '{} group type{} with issues (nested / excluded / single instance).'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_scope_boxes(doc):
    """
    Scope boxes not referenced by any view.
    Items: [{id, name}]
    WARN if any orphaned scope boxes found.
    """
    items = []
    try:
        # collect all scope box element IDs referenced by views
        used_ids = set()
        views = FilteredElementCollector(doc) \
            .OfClass(View) \
            .WhereElementIsNotElementType()
        for v in views:
            try:
                sb_id = _safe_param_id(v, BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
                if sb_id != ElementId.InvalidElementId:
                    used_ids.add(sb_id.IntegerValue)
            except Exception:
                continue

        scope_boxes = FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_VolumeOfInterest) \
            .WhereElementIsNotElementType()
        for sb in scope_boxes:
            try:
                if sb.Id.IntegerValue not in used_ids:
                    items.append({
                        'id':   sb.Id.IntegerValue,
                        'name': sb.Name,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No orphaned scope boxes found.')
    return _result('WARN', len(items), items,
                   '{} unused scope box{} found.'.format(
                       len(items), 'es' if len(items) != 1 else ''))


def check_reference_planes(doc, unnamed_threshold=20):
    """
    Reference planes with default name ('Reference Plane').
    Items: [{id, name, view_name}]
    WARN if unnamed count > unnamed_threshold.
    """
    items = []
    try:
        ref_planes = FilteredElementCollector(doc) \
            .OfClass(ReferencePlane) \
            .WhereElementIsNotElementType()
        for rp in ref_planes:
            try:
                name = rp.Name or ''
                if name.strip().lower() in ('reference plane', ''):
                    view_name = ''
                    owner_id = rp.OwnerViewId
                    if owner_id and owner_id != ElementId.InvalidElementId:
                        v = doc.GetElement(owner_id)
                        view_name = v.Name if v else ''
                    items.append({
                        'id':        rp.Id.IntegerValue,
                        'name':      name or '(unnamed)',
                        'view_name': view_name or 'All views',
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No unnamed reference planes found.')
    status = 'WARN' if len(items) > unnamed_threshold else 'OK'
    return _result(status, len(items), items,
                   '{} unnamed reference plane{} found.'.format(
                       len(items), 's' if len(items) != 1 else ''))


# ---------------------------------------------------------------------------
# CATEGORY 2 — DATA INTEGRITY (9 checks)
# ---------------------------------------------------------------------------

def check_revit_warnings(doc, warn_threshold=20, fail_threshold=100):
    """
    doc.GetWarnings() — groups by failure description.
    Items: [{description, count, element_ids}]
    WARN > warn_threshold, FAIL > fail_threshold.
    """
    items = []
    total = 0
    try:
        warnings = doc.GetWarnings()
        by_desc = defaultdict(list)
        for w in warnings:
            try:
                desc = w.GetDescriptionText()
                ids = [eid.IntegerValue for eid in w.GetFailingElements()]
                by_desc[desc].extend(ids)
            except Exception:
                continue
        total = sum(len(v) for v in by_desc.values())
        for desc, eids in sorted(by_desc.items(), key=lambda x: -len(x[1])):
            items.append({
                'description': desc,
                'count':       len(eids),
                'element_ids': eids[:20],  # cap at 20 IDs per type for UI
            })
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if total == 0:
        return _ok('No Revit warnings.')
    status = 'FAIL' if total > fail_threshold else 'WARN'
    return _result(status, total, items,
                   '{} warning{} across {} type{}.'.format(
                       total, 's' if total != 1 else '',
                       len(items), 's' if len(items) != 1 else ''))


def check_unplaced_rooms(doc, fail_threshold=5):
    """
    SpatialElement (rooms) with Area == 0 or no location.
    Items: [{id, name, number, level}]
    WARN > 0, FAIL > fail_threshold.
    """
    items = []
    try:
        rooms = FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_Rooms) \
            .WhereElementIsNotElementType()
        for r in rooms:
            try:
                area = r.Area
                loc = r.Location
                if area == 0 or loc is None:
                    number = _safe_param_str(r, BuiltInParameter.ROOM_NUMBER)
                    name   = _safe_param_str(r, BuiltInParameter.ROOM_NAME)
                    level  = ''
                    try:
                        level = r.Level.Name if r.Level else ''
                    except Exception:
                        pass
                    items.append({
                        'id':     r.Id.IntegerValue,
                        'name':   name,
                        'number': number,
                        'level':  level,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No unplaced rooms found.')
    status = 'FAIL' if len(items) > fail_threshold else 'WARN'
    return _result(status, len(items), items,
                   '{} unplaced or redundant room{}.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_elements_not_in_phase(doc):
    """
    Elements whose Phase Created parameter is invalid / missing.
    Focuses on model elements (walls, floors, doors, windows, rooms).
    Items: [{id, category, phase_created}]
    WARN if any found.
    """
    items = []
    check_bics = [
        BuiltInCategory.OST_Walls,
        BuiltInCategory.OST_Floors,
        BuiltInCategory.OST_Doors,
        BuiltInCategory.OST_Windows,
        BuiltInCategory.OST_Rooms,
    ]
    try:
        for bic in check_bics:
            try:
                elems = FilteredElementCollector(doc) \
                    .OfCategory(bic) \
                    .WhereElementIsNotElementType()
                for e in elems:
                    try:
                        p = e.get_Parameter(BuiltInParameter.PHASE_CREATED)
                        if p is None or p.AsElementId() == ElementId.InvalidElementId:
                            cat_name = ''
                            try:
                                cat_name = e.Category.Name
                            except Exception:
                                pass
                            items.append({
                                'id':            e.Id.IntegerValue,
                                'category':      cat_name,
                                'phase_created': '(none)',
                            })
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All checked elements have a phase assigned.')
    return _result('WARN', len(items), items,
                   '{} element{} with no phase assigned.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_duplicate_sheet_numbers(doc):
    """
    ViewSheet elements sharing the same sheet number.
    Items: [{sheet_number, sheet_names, ids}]
    FAIL if any duplicates found.
    """
    items = []
    try:
        sheets = FilteredElementCollector(doc) \
            .OfClass(ViewSheet) \
            .WhereElementIsNotElementType()
        by_number = defaultdict(list)
        for s in sheets:
            try:
                num = s.SheetNumber
                by_number[num].append({
                    'id':   s.Id.IntegerValue,
                    'name': s.Name,
                })
            except Exception:
                continue
        for num, sheet_list in by_number.items():
            if len(sheet_list) > 1:
                items.append({
                    'sheet_number': num,
                    'sheets':       sheet_list,
                })
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No duplicate sheet numbers found.')
    return _result('FAIL', len(items), items,
                   '{} duplicate sheet number{} found.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_design_options(doc):
    """
    Live (unresolved) design options still in the model.
    Items: [{id, name, option_set, is_primary}]
    WARN if any found.
    """
    items = []
    try:
        options = FilteredElementCollector(doc) \
            .OfClass(DesignOption) \
            .WhereElementIsNotElementType()
        for opt in options:
            try:
                items.append({
                    'id':         opt.Id.IntegerValue,
                    'name':       opt.Name,
                    'is_primary': opt.IsPrimary,
                })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No unresolved design options found.')
    return _result('WARN', len(items), items,
                   '{} design option{} still active — accept or reject before issuing.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_untagged_elements(doc, warn_threshold=10, fail_threshold=50):
    """
    Doors, windows and rooms with no IndependentTag in any view.
    Items: [{id, name, category, level}]
    WARN > warn_threshold, FAIL > fail_threshold.
    """
    items = []
    try:
        # collect tagged element IDs from all independent tags
        tagged_ids = set()
        tags = FilteredElementCollector(doc) \
            .OfClass(IndependentTag) \
            .WhereElementIsNotElementType()
        for tag in tags:
            try:
                tid = tag.TaggedElementId
                if hasattr(tid, 'HostElementId'):
                    tagged_ids.add(tid.HostElementId.IntegerValue)
                else:
                    tagged_ids.add(tid.IntegerValue)
            except Exception:
                continue

        check_bics = [
            (BuiltInCategory.OST_Doors,   'Doors'),
            (BuiltInCategory.OST_Windows, 'Windows'),
            (BuiltInCategory.OST_Rooms,   'Rooms'),
        ]
        for bic, label in check_bics:
            try:
                elems = FilteredElementCollector(doc) \
                    .OfCategory(bic) \
                    .WhereElementIsNotElementType()
                for e in elems:
                    try:
                        if e.Id.IntegerValue not in tagged_ids:
                            items.append({
                                'id':       e.Id.IntegerValue,
                                'name':     e.Name,
                                'category': label,
                                'level':    _elem_level_name(doc, e),
                            })
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All checked elements are tagged.')
    status = 'FAIL' if len(items) > fail_threshold else 'WARN'
    return _result(status, len(items), items,
                   '{} untagged element{} (doors, windows, rooms).'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_pinned_elements(doc):
    """
    Elements that are pinned but belong to categories that shouldn't typically be.
    Checks: Furniture, Furniture Systems, Specialty Equipment, Generic Models.
    Items: [{id, name, category, level}]
    WARN if any found.
    """
    items = []
    check_bics = [
        BuiltInCategory.OST_Furniture,
        BuiltInCategory.OST_FurnitureSystems,
        BuiltInCategory.OST_SpecialityEquipment,
        BuiltInCategory.OST_GenericModel,
    ]
    try:
        for bic in check_bics:
            try:
                elems = FilteredElementCollector(doc) \
                    .OfCategory(bic) \
                    .WhereElementIsNotElementType()
                for e in elems:
                    try:
                        if e.Pinned:
                            cat_name = ''
                            try:
                                cat_name = e.Category.Name
                            except Exception:
                                pass
                            items.append({
                                'id':       e.Id.IntegerValue,
                                'name':     e.Name,
                                'category': cat_name,
                                'level':    _elem_level_name(doc, e),
                            })
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No unexpectedly pinned elements found.')
    return _result('WARN', len(items), items,
                   '{} pinned element{} in categories that should not typically be pinned.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_sheets_with_no_views(doc):
    """
    ViewSheet elements that have no viewports placed on them.
    Items: [{id, sheet_number, name}]
    WARN if any found.
    """
    items = []
    try:
        from Autodesk.Revit.DB import Viewport
        # build set of sheet IDs that have at least one viewport
        sheets_with_viewports = set()
        viewports = FilteredElementCollector(doc) \
            .OfClass(Viewport) \
            .WhereElementIsNotElementType()
        for vp in viewports:
            try:
                sheets_with_viewports.add(vp.SheetId.IntegerValue)
            except Exception:
                continue

        sheets = FilteredElementCollector(doc) \
            .OfClass(ViewSheet) \
            .WhereElementIsNotElementType()
        for s in sheets:
            try:
                if s.Id.IntegerValue not in sheets_with_viewports:
                    items.append({
                        'id':           s.Id.IntegerValue,
                        'sheet_number': s.SheetNumber,
                        'name':         s.Name,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All sheets have at least one viewport.')
    return _result('WARN', len(items), items,
                   '{} sheet{} with no views placed.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_duplicate_view_names(doc):
    """
    Views (excluding sheets and templates) sharing the same name AND the
    same view type.
    Items: [{name, view_type, views: [{id, view_type}]}]
    FAIL if any found.

    Grouped by (name, view type) rather than name alone — Revit's own
    name-uniqueness rule is scoped per view type, not global. A Floor
    Plan and a Reflected Ceiling Plan both named "Level 1" is completely
    normal (it's what every out-of-the-box Revit template ships with);
    only two views of the SAME type sharing a name is an actual
    conflict/duplicate as Revit itself defines it.
    """
    items = []
    try:
        views = FilteredElementCollector(doc) \
            .OfClass(View) \
            .WhereElementIsNotElementType()
        by_name_type = defaultdict(list)
        for v in views:
            try:
                if v.IsTemplate:
                    continue
                if isinstance(v, ViewSheet):
                    continue
                vtype = _view_type_name(v)
                by_name_type[(v.Name, vtype)].append({
                    'id':        v.Id.IntegerValue,
                    'view_type': vtype,
                })
            except Exception:
                continue
        for (name, vtype), view_list in by_name_type.items():
            if len(view_list) > 1:
                items.append({
                    'name':      name,
                    'view_type': vtype,
                    'views':     view_list,
                })
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No duplicate view names found.')
    return _result('FAIL', len(items), items,
                   '{} duplicate view name{} found (same name, same view type).'.format(
                       len(items), 's' if len(items) != 1 else ''))


# ---------------------------------------------------------------------------
# CATEGORY 3 — COORDINATION (4 checks)
# ---------------------------------------------------------------------------

def check_link_status(doc):
    """
    RevitLinkInstance and CAD links not in Loaded state.
    Items: [{id, name, link_type, status, last_path}]
    FAIL if any not loaded.
    """
    items = []
    try:
        # Revit links
        rvt_links = FilteredElementCollector(doc) \
            .OfClass(RevitLinkInstance) \
            .WhereElementIsNotElementType()
        for lnk in rvt_links:
            try:
                ref = lnk.GetExternalFileReference()
                status = str(ref.GetLinkedFileStatus())
                if ref.GetLinkedFileStatus() != LinkedFileStatus.Loaded:
                    path = ''
                    try:
                        path = ref.GetAbsolutePath().CentralServerPath
                    except Exception:
                        try:
                            path = str(ref.GetPath())
                        except Exception:
                            pass
                    items.append({
                        'id':        lnk.Id.IntegerValue,
                        'name':      lnk.Name,
                        'link_type': 'Revit',
                        'status':    status,
                        'last_path': path,
                    })
            except Exception:
                continue

        # CAD links
        cad_links = FilteredElementCollector(doc) \
            .OfClass(CADLinkType) \
            .WhereElementIsElementType()
        for lnk in cad_links:
            try:
                ref = lnk.GetExternalFileReference()
                if ref.GetLinkedFileStatus() != LinkedFileStatus.Loaded:
                    path = ''
                    try:
                        path = str(ref.GetPath())
                    except Exception:
                        pass
                    items.append({
                        'id':        lnk.Id.IntegerValue,
                        'name':      lnk.Name,
                        'link_type': 'CAD',
                        'status':    str(ref.GetLinkedFileStatus()),
                        'last_path': path,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All links loaded.')
    return _result('FAIL', len(items), items,
                   '{} link{} not loaded.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_grid_level_extents(doc):
    """
    Grids and levels whose 3D extents are not enabled.
    Items: [{id, name, element_type}]
    WARN if any found.
    """
    items = []
    try:
        from Autodesk.Revit.DB import Grid, Level, DatumExtentType
        for cls, label in [(Grid, 'Grid'), (Level, 'Level')]:
            elems = FilteredElementCollector(doc) \
                .OfClass(cls) \
                .WhereElementIsNotElementType()
            for e in elems:
                try:
                    ext_3d = e.GetDatumExtentTypeInView(
                        DatumExtentType.Model,
                        doc.ActiveView if doc.ActiveView else None
                    )
                    # if we can check, skip — not all API versions expose this easily
                    # so we check the MaximumExtent vs BoundingBox
                    bb = e.get_BoundingBox(None)
                    if bb is None:
                        items.append({
                            'id':           e.Id.IntegerValue,
                            'name':         e.Name,
                            'element_type': label,
                        })
                except Exception:
                    continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('Grid and level extents appear set.')
    return _result('WARN', len(items), items,
                   '{} grid/level element{} may have limited extents.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_rooms_in_open_space(doc):
    """
    Placed rooms (Area > 0) that are not properly enclosed —
    detected by checking if room perimeter is 0 or room has no boundary segments.
    Items: [{id, name, number, level, area, perimeter}]
    WARN if any found.
    """
    items = []
    try:
        from Autodesk.Revit.DB import SpatialElementBoundaryOptions
        opts = SpatialElementBoundaryOptions()
        rooms = FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_Rooms) \
            .WhereElementIsNotElementType()
        for r in rooms:
            try:
                area = r.Area
                if area == 0:
                    continue  # unplaced — handled by check_unplaced_rooms
                perimeter = r.Perimeter
                segs = r.GetBoundarySegments(opts)
                if perimeter == 0 or not segs or len(list(segs)) == 0:
                    number = _safe_param_str(r, BuiltInParameter.ROOM_NUMBER)
                    name   = _safe_param_str(r, BuiltInParameter.ROOM_NAME)
                    level  = ''
                    try:
                        level = r.Level.Name if r.Level else ''
                    except Exception:
                        pass
                    items.append({
                        'id':        r.Id.IntegerValue,
                        'name':      name,
                        'number':    number,
                        'level':     level,
                        'area':      round(area, 2),
                        'perimeter': round(perimeter, 2),
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All placed rooms have enclosing boundaries.')
    return _result('WARN', len(items), items,
                   '{} room{} placed in open space (no enclosing walls).'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_workset_usage(doc):
    """
    User worksets with 0 elements assigned to them.
    Items: [{id, name, element_count}]
    WARN if any empty worksets found.
    Skipped gracefully if model is not workshared.
    """
    items = []
    try:
        if not doc.IsWorkshared:
            return _result('INFO', 0, [], 'Model is not workshared — workset check skipped.')

        worksets = FilteredWorksetCollector(doc) \
            .OfKind(WorksetKind.UserWorkset)

        # count elements per workset
        from Autodesk.Revit.DB import WorksetId
        workset_counts = defaultdict(int)
        all_elems = FilteredElementCollector(doc) \
            .WhereElementIsNotElementType()
        for e in all_elems:
            try:
                ws_id = e.WorksetId
                if ws_id:
                    workset_counts[ws_id.IntegerValue] += 1
            except Exception:
                continue

        for ws in worksets:
            try:
                count = workset_counts.get(ws.Id.IntegerValue, 0)
                if count == 0:
                    items.append({
                        'id':            ws.Id.IntegerValue,
                        'name':          ws.Name,
                        'element_count': 0,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All worksets have elements assigned.')
    return _result('WARN', len(items), items,
                   '{} empty workset{} found.'.format(
                       len(items), 's' if len(items) != 1 else ''))


# ---------------------------------------------------------------------------
# CATEGORY 4 — PERFORMANCE (4 checks)
# ---------------------------------------------------------------------------

def check_views_not_on_sheets(doc, warn_threshold=20):
    """
    Non-template, non-sheet views not placed on any sheet.
    Items: [{id, name, view_type}]
    WARN if count > warn_threshold.
    """
    items = []
    try:
        from Autodesk.Revit.DB import Viewport
        # views that ARE on sheets
        views_on_sheets = set()
        viewports = FilteredElementCollector(doc) \
            .OfClass(Viewport) \
            .WhereElementIsNotElementType()
        for vp in viewports:
            try:
                views_on_sheets.add(vp.ViewId.IntegerValue)
            except Exception:
                continue

        views = FilteredElementCollector(doc) \
            .OfClass(View) \
            .WhereElementIsNotElementType()
        for v in views:
            try:
                if v.IsTemplate:
                    continue
                if isinstance(v, ViewSheet):
                    continue
                # skip schedules — they go on sheets differently
                if v.ViewType == ViewType.Schedule:
                    continue
                if v.Id.IntegerValue not in views_on_sheets:
                    items.append({
                        'id':        v.Id.IntegerValue,
                        'name':      v.Name,
                        'view_type': _view_type_name(v),
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All views are placed on sheets.')
    status = 'WARN' if len(items) > warn_threshold else 'OK'
    return _result(status, len(items), items,
                   '{} view{} not placed on any sheet.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_view_count_and_templates(doc, unmanaged_threshold=10):
    """
    Views not using any view template.
    Items: [{id, name, view_type}]
    WARN if count > unmanaged_threshold.
    """
    items = []
    try:
        from Autodesk.Revit.DB import ElementId
        views = FilteredElementCollector(doc) \
            .OfClass(View) \
            .WhereElementIsNotElementType()
        for v in views:
            try:
                if v.IsTemplate:
                    continue
                if isinstance(v, ViewSheet):
                    continue
                if v.ViewType in (ViewType.Schedule, ViewType.DrawingSheet):
                    continue
                tmpl_id = v.ViewTemplateId
                if tmpl_id == ElementId.InvalidElementId:
                    items.append({
                        'id':        v.Id.IntegerValue,
                        'name':      v.Name,
                        'view_type': _view_type_name(v),
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All views use a view template.')
    status = 'WARN' if len(items) > unmanaged_threshold else 'OK'
    return _result(status, len(items), items,
                   '{} view{} without a view template.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_detail_line_overload(doc, per_view_threshold=500):
    """
    Views with excessive detail lines (CurveElement of category Detail Lines).
    Items: [{id, view_name, line_count}]
    WARN if any view exceeds per_view_threshold.
    """
    items = []
    try:
        from Autodesk.Revit.DB import CurveElement
        # count detail lines per view
        view_counts = defaultdict(int)
        lines = FilteredElementCollector(doc) \
            .OfClass(CurveElement) \
            .WhereElementIsNotElementType()
        for ln in lines:
            try:
                cat = ln.Category
                if cat and cat.Id.IntegerValue == int(BuiltInCategory.OST_Lines):
                    owner_id = ln.OwnerViewId
                    if owner_id and owner_id != ElementId.InvalidElementId:
                        view_counts[owner_id.IntegerValue] += 1
            except Exception:
                continue

        for view_id_int, count in view_counts.items():
            if count > per_view_threshold:
                v = doc.GetElement(ElementId(view_id_int))
                view_name = v.Name if v else 'Unknown'
                items.append({
                    'id':         view_id_int,
                    'view_name':  view_name,
                    'line_count': count,
                })
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No views exceed the detail line threshold ({}).'.format(per_view_threshold))
    return _result('WARN', len(items), items,
                   '{} view{} with excessive detail lines (>{} per view).'.format(
                       len(items), 's' if len(items) != 1 else '', per_view_threshold))


def check_ceiling_height_anomalies(doc):
    """
    Placed rooms with UnboundedHeight == 0 or missing ceiling height parameter.
    Items: [{id, name, number, level, height}]
    WARN if any found.
    """
    items = []
    try:
        rooms = FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_Rooms) \
            .WhereElementIsNotElementType()
        for r in rooms:
            try:
                if r.Area == 0:
                    continue  # skip unplaced
                p = r.get_Parameter(BuiltInParameter.ROOM_HEIGHT)
                height = p.AsDouble() if p else 0
                if height == 0:
                    number = _safe_param_str(r, BuiltInParameter.ROOM_NUMBER)
                    name   = _safe_param_str(r, BuiltInParameter.ROOM_NAME)
                    level  = ''
                    try:
                        level = r.Level.Name if r.Level else ''
                    except Exception:
                        pass
                    items.append({
                        'id':     r.Id.IntegerValue,
                        'name':   name,
                        'number': number,
                        'level':  level,
                        'height': height,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All rooms have ceiling heights set.')
    return _result('WARN', len(items), items,
                   '{} room{} with missing or zero ceiling height.'.format(
                       len(items), 's' if len(items) != 1 else ''))


# ---------------------------------------------------------------------------
# CATEGORY 5 — PARAMETERS (3 checks — opt-in)
# ---------------------------------------------------------------------------

def _read_sp_file(doc):
    """
    Read the shared parameter .txt file set in Revit Application Options.
    Returns (sp_path, params_list) where params_list is a list of dicts,
    or (sp_path, None) if file not found, or (None, None) if no SP file set.
    """
    try:
        sp_path = doc.Application.SharedParametersFilename
        if not sp_path:
            return None, None
        if not os.path.exists(sp_path):
            return sp_path, None

        params = []
        with open(sp_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('*'):
                    continue
                cols = line.split('\t')
                if len(cols) >= 6 and cols[0] == 'PARAM':
                    params.append({
                        'guid':     cols[1].strip(),
                        'name':     cols[2].strip(),
                        'datatype': cols[3].strip() if len(cols) > 3 else '',
                        'group_id': cols[5].strip() if len(cols) > 5 else '',
                    })
        return sp_path, params
    except Exception:
        return None, None


def check_duplicate_parameters(doc):
    """
    Detects:
    (a) Duplicate GUIDs — same parameter added twice in SP file.
    (b) Duplicate name variants — e.g. Room_Number vs RoomNumber vs Room Number.
    Items: [{issue_type, param_name, guids, variants}]
    WARN if any found.
    Reads SP file directly + doc.ParameterBindings.
    """
    items = []
    try:
        sp_path, sp_params = _read_sp_file(doc)

        if sp_params is not None:
            # (a) duplicate GUIDs in SP file
            by_guid = defaultdict(list)
            for p in sp_params:
                by_guid[p['guid']].append(p)
            for guid, plist in by_guid.items():
                if len(plist) > 1:
                    items.append({
                        'issue_type': 'Duplicate GUID in SP file',
                        'param_name': ' / '.join(p['name'] for p in plist),
                        'guids':      [guid],
                        'variants':   [p['name'] for p in plist],
                    })

            # (b) duplicate name variants — normalise
            def normalise(name):
                return re.sub(r'[\s_\-]', '', name).lower()

            by_norm = defaultdict(list)
            for p in sp_params:
                by_norm[normalise(p['name'])].append(p)
            for norm, plist in by_norm.items():
                if len(plist) > 1:
                    # don't double-report if already caught by GUID check
                    guids = [p['guid'] for p in plist]
                    if len(set(guids)) == len(guids):  # all different GUIDs
                        items.append({
                            'issue_type': 'Duplicate name variants',
                            'param_name': plist[0]['name'],
                            'guids':      guids,
                            'variants':   [p['name'] for p in plist],
                        })

        # also check project parameters for name collisions with SP params
        it = doc.ParameterBindings.ForwardIterator()
        project_param_names = defaultdict(list)
        while it.MoveNext():
            defn = it.Key
            try:
                project_param_names[defn.Name.strip()].append({
                    'name': defn.Name,
                    'is_shared': hasattr(defn, 'GUID'),
                })
            except Exception:
                continue
        for name, plist in project_param_names.items():
            if len(plist) > 1:
                items.append({
                    'issue_type': 'Duplicate name in project bindings',
                    'param_name': name,
                    'guids':      [],
                    'variants':   [p['name'] for p in plist],
                })

    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('No duplicate parameters found.')
    return _result('WARN', len(items), items,
                   '{} duplicate parameter issue{} found.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_unused_parameters(doc):
    """
    Shared and project parameters bound to categories but with no values
    set on any element in those categories.
    SLOW — iterates all elements in all bound categories.
    Opt-in only — excluded from Full Audit preset.
    Items: [{name, param_type, bound_categories, values_found}]
    WARN if any found.
    """
    items = []
    try:
        from Autodesk.Revit.DB import ElementMulticategoryFilter, Category
        it = doc.ParameterBindings.ForwardIterator()
        param_defs = []
        while it.MoveNext():
            try:
                defn = it.Key
                binding = it.Current
                # get bound category ids
                cat_set = binding.Categories
                bic_list = []
                for cat in cat_set:
                    try:
                        bic_list.append(cat.Id)
                    except Exception:
                        continue
                if bic_list:
                    param_defs.append({
                        'defn':       defn,
                        'name':       defn.Name,
                        'is_shared':  hasattr(defn, 'GUID'),
                        'cat_ids':    bic_list,
                        'cat_names':  [cat.Name for cat in cat_set],
                    })
            except Exception:
                continue

        for pd in param_defs:
            try:
                # collect elements in bound categories
                cat_filter = ElementMulticategoryFilter(pd['cat_ids'])
                elems = FilteredElementCollector(doc) \
                    .WherePasses(cat_filter) \
                    .WhereElementIsNotElementType()
                has_value = False
                for e in elems:
                    try:
                        p = e.LookupParameter(pd['name'])
                        if p is not None:
                            val = p.AsString() or p.AsValueString() or ''
                            if val.strip():
                                has_value = True
                                break
                            # check numeric
                            try:
                                if p.AsDouble() != 0:
                                    has_value = True
                                    break
                            except Exception:
                                pass
                    except Exception:
                        continue
                if not has_value:
                    items.append({
                        'name':             pd['name'],
                        'param_type':       'Shared' if pd['is_shared'] else 'Project',
                        'bound_categories': ', '.join(pd['cat_names']),
                        'values_found':     0,
                    })
            except Exception:
                continue
    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _ok('All bound parameters have at least one value set.')
    return _result('WARN', len(items), items,
                   '{} parameter{} bound to categories but no values set on any element.'.format(
                       len(items), 's' if len(items) != 1 else ''))


def check_unassociated_sp_params(doc):
    """
    Parameters in the shared parameter .txt file that are not bound
    to any category in this model.
    Reads the SP file directly via Python file I/O.
    Items: [{name, guid, datatype, group_id}]
    INFO status — informational only, does not affect health score.
    """
    items = []
    try:
        sp_path, sp_params = _read_sp_file(doc)

        if sp_path is None:
            return _result('INFO', 0, [],
                           'No shared parameter file set in Application Options.')
        if sp_params is None:
            return _result('WARN', 0, [],
                           'Shared parameter file set but not found at: {}'.format(sp_path))

        # collect GUIDs of all shared params bound in this model
        bound_guids = set()
        it = doc.ParameterBindings.ForwardIterator()
        while it.MoveNext():
            defn = it.Key
            try:
                if hasattr(defn, 'GUID'):
                    bound_guids.add(str(defn.GUID).lower())
            except Exception:
                continue

        # also check SharedParameterElement collector
        sp_elems = FilteredElementCollector(doc) \
            .OfClass(SharedParameterElement) \
            .WhereElementIsNotElementType()
        for spe in sp_elems:
            try:
                bound_guids.add(str(spe.GuidValue).lower())
            except Exception:
                continue

        for p in sp_params:
            if p['guid'].lower() not in bound_guids:
                items.append({
                    'name':     p['name'],
                    'guid':     p['guid'],
                    'datatype': p['datatype'],
                    'group_id': p['group_id'],
                })

    except Exception as ex:
        return _result('ERROR', 0, [], 'Error: {}'.format(str(ex)))

    if not items:
        return _result('INFO', 0, [],
                       'All SP file parameters are bound in this model.')
    return _result('INFO', len(items), items,
                   '{} SP file parameter{} not bound to any category in this model.'.format(
                       len(items), 's' if len(items) != 1 else ''))


# ---------------------------------------------------------------------------
# Master runner
# ---------------------------------------------------------------------------

# Maps check key → (function, label, category, weight, opt_in)
# weight = contribution to health score (higher = more impact)
# opt_in = True means excluded from Full Audit preset by default

CHECKS = [
    # key                          fn                               label                         category        weight  opt_in
    ('imported_dwgs',              check_imported_dwgs,             'Imported DWGs',              'bloat',        8,      False),
    ('unused_families',            check_unused_families,           'Unused families',             'bloat',        5,      False),
    ('raster_images',              check_raster_images,             'Raster images',               'bloat',        6,      False),
    ('oversized_families',         check_oversized_families,        'Oversized families (opt-in, slow)', 'bloat',  3,      True),
    ('inplace_families',           check_inplace_families,          'In-place families',           'bloat',        7,      False),
    ('groups',                     check_groups,                    'Groups',                      'bloat',        4,      False),
    ('scope_boxes',                check_scope_boxes,               'Scope boxes',                 'bloat',        2,      False),
    ('reference_planes',           check_reference_planes,          'Reference planes',            'bloat',        2,      False),
    ('revit_warnings',             check_revit_warnings,            'Revit warnings',              'integrity',    10,     False),
    ('unplaced_rooms',             check_unplaced_rooms,            'Unplaced rooms',              'integrity',    8,      False),
    ('elements_not_in_phase',      check_elements_not_in_phase,     'Elements not in phase',       'integrity',    6,      False),
    ('duplicate_sheet_numbers',    check_duplicate_sheet_numbers,   'Duplicate sheet numbers',     'integrity',    9,      False),
    ('design_options',             check_design_options,            'Design options',              'integrity',    5,      False),
    ('untagged_elements',          check_untagged_elements,         'Untagged elements',           'integrity',    5,      False),
    ('pinned_elements',            check_pinned_elements,           'Pinned elements',             'integrity',    3,      False),
    ('sheets_with_no_views',       check_sheets_with_no_views,      'Sheets with no views',        'integrity',    4,      False),
    ('duplicate_view_names',       check_duplicate_view_names,      'Duplicate view names',        'integrity',    6,      False),
    ('link_status',                check_link_status,               'Link status',                 'coordination', 10,     False),
    ('grid_level_extents',         check_grid_level_extents,        'Grid / level extents',        'coordination', 5,      False),
    ('rooms_in_open_space',        check_rooms_in_open_space,       'Rooms in open space',         'coordination', 6,      False),
    ('workset_usage',              check_workset_usage,             'Workset usage',               'coordination', 4,      False),
    ('views_not_on_sheets',        check_views_not_on_sheets,       'Views not on sheets',         'performance',  5,      False),
    ('view_count_and_templates',   check_view_count_and_templates,  'View count / templates',      'performance',  4,      False),
    ('detail_line_overload',       check_detail_line_overload,      'Detail line overload',        'performance',  3,      False),
    ('ceiling_height_anomalies',   check_ceiling_height_anomalies,  'Ceiling height anomalies',    'performance',  3,      False),
    ('duplicate_parameters',       check_duplicate_parameters,      'Duplicate parameters',        'parameters',   3,      True),
    ('unused_parameters',          check_unused_parameters,         'Unused parameters',           'parameters',   2,      True),
    ('unassociated_sp_params',     check_unassociated_sp_params,    'Unassociated SP params',      'parameters',   1,      True),
]

# Preset definitions — list of check keys included
PRESETS = {
    'Full audit':       [c[0] for c in CHECKS if not c[5]],      # all non-opt-in
    'Issue check':      ['imported_dwgs', 'link_status',
                         'duplicate_sheet_numbers', 'duplicate_view_names',
                         'unplaced_rooms', 'design_options'],
    'Quick bloat':      [c[0] for c in CHECKS if c[3] == 'bloat' and not c[5]],
    'Parameter audit':  ['duplicate_parameters',
                         'unused_parameters',
                         'unassociated_sp_params'],
    # oversized_families is opt-in and excluded from the presets above —
    # it's the one check that walks every family's solid geometry via the
    # Revit API, which can crash the geometry kernel on some models. Run
    # it deliberately, on its own, via this preset.
    'Geometry check (slow)': ['oversized_families'],
}


def run_checks(doc, check_keys=None, progress_callback=None):
    """
    Run a set of checks and return results dict keyed by check key.
    progress_callback(current, total, label) called after each check.

    Usage in script.py:
        results = checks.run_checks(doc, checks.PRESETS['Full audit'],
                                    progress_callback=update_progress_bar)
    """
    if check_keys is None:
        check_keys = PRESETS['Full audit']

    # build lookup
    check_map = {c[0]: c for c in CHECKS}
    results = {}
    total = len(check_keys)

    for i, key in enumerate(check_keys):
        if key not in check_map:
            continue
        _, fn, label, category, weight, opt_in = check_map[key]
        try:
            result = fn(doc)
        except Exception as ex:
            result = _result('ERROR', 0, [],
                             'Unhandled error in {}: {}'.format(key, str(ex)))
        result['label']    = label
        result['category'] = category
        result['weight']   = weight
        results[key] = result

        if progress_callback:
            try:
                progress_callback(i + 1, total, label)
            except Exception:
                pass

    return results


def calculate_health_score(results):
    """
    Weighted health score 0–100.
    OK   → full weight
    INFO → full weight (informational, no penalty)
    WARN → half weight
    FAIL → zero weight
    ERROR → zero weight

    Returns int score and breakdown dict.
    """
    total_weight = 0
    earned_weight = 0
    counts = {'OK': 0, 'WARN': 0, 'FAIL': 0, 'INFO': 0, 'ERROR': 0}

    for key, r in results.items():
        status = r.get('status', 'ERROR')
        weight = r.get('weight', 1)
        total_weight += weight
        if status in ('OK', 'INFO'):
            earned_weight += weight
        elif status == 'WARN':
            earned_weight += weight * 0.5
        counts[status] = counts.get(status, 0) + 1

    score = int(round((earned_weight / total_weight) * 100)) if total_weight > 0 else 0
    return score, counts
