# -*- coding: utf-8 -*-
"""
RLS Sheet Renumber  v1.0.0
Renumbers the numeric base of RLS sheet drawing numbers by block/floor group.
Only the base number changes (e.g. 6516 in CN06/1053-AR-BN 6516-0).
The project prefix and plan/elevation suffix are preserved.
"""
import clr
import os
import re
import json
import codecs
import datetime
import csv as csv_mod

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')
clr.AddReference('System')
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Xml')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInParameter, Transaction, ViewSheet,
    StorageType
)
import System
import System.Windows as SW
import System.Windows.Controls as SWC
import System.Windows.Media as SWM
import System.Windows.Forms as WinForms
from System.Windows.Markup import XamlReader
from System.Xml import XmlReader
from System.IO import StringReader
from System.Collections.ObjectModel import ObservableCollection
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import script as pyscript
pyrevit_config = pyscript.get_config()

uidoc = __revit__.ActiveUIDocument
doc   = uidoc.Document

# ─────────────────────────────────────────────────────────────────────────────
# COLOUR PALETTE  (matches office standard)
# ─────────────────────────────────────────────────────────────────────────────
def hex_brush(h):
    h = h.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return SolidColorBrush(Color.FromArgb(255, r, g, b))

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
VERSION    = "1.0.0"
RLS_FILTER = "FLOOR- RLS"   # sheet Name must contain this (case-insensitive)
P_NUM      = BuiltInParameter.SHEET_NUMBER
P_NAME     = BuiltInParameter.SHEET_NAME
T2_PARAM   = "OLA Sheet Title 2"

# Regex: captures prefix, 4+-digit base, and optional dash-suffix
# e.g. "CN06/1053-AR-BN 6516-0" -> ("CN06/1053-AR-BN ", 6516, 0)
# e.g. "6516-0"                 -> ("", 6516, 0)
# e.g. "6516"                   -> ("", 6516, 0)  [fallback, suffix=0]
NUM_RE   = re.compile(r'^(.*?)(\d{4,})-(\d+)$')   # with explicit suffix
NUM_RE_B = re.compile(r'^(.*?)(\d{4,})$')          # without suffix → treat as 0

DEFAULT_GROUPS = [
    {"name": "BLOCK M BASEMENT",  "start": 6001, "end": 6099},
    {"name": "BLOCK M GROUND",    "start": 6101, "end": 6199},
    {"name": "BLOCK M FIRST",     "start": 6201, "end": 6299},
    {"name": "BLOCK M SECOND",    "start": 6301, "end": 6399},
    {"name": "BLOCK N-1 GROUND",  "start": 6401, "end": 6499},
    {"name": "BLOCK N-2 GROUND",  "start": 6501, "end": 6599},
    {"name": "BLOCK N-1 FIRST",   "start": 6601, "end": 6699},
    {"name": "BLOCK N-2 FIRST",   "start": 6701, "end": 6799},
    {"name": "BLOCK N-1 SECOND",  "start": 6801, "end": 6899},
    {"name": "BLOCK N-2 SECOND",  "start": 6901, "end": 6999},
]

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (per-project, stored in renumber_config.json beside the script)
# ─────────────────────────────────────────────────────────────────────────────
def project_key():
    return re.sub(r'[^\w\-]', '_', doc.Title or 'UNKNOWN')[:40]

def config_path():
    return os.path.join(os.path.dirname(__file__), 'renumber_config.json')

def load_cfg():
    p, k = config_path(), project_key()
    if os.path.exists(p):
        try:
            with open(p, 'r') as f:
                all_cfg = json.load(f)
            if k in all_cfg:
                return all_cfg[k]
        except Exception:
            pass
    return {"groups": [dict(g) for g in DEFAULT_GROUPS],
            "log_folder":   "",
            "sheet_filter": RLS_FILTER,
            "t2_param":     T2_PARAM}

def save_cfg(data):
    p, k = config_path(), project_key()
    all_cfg = {}
    if os.path.exists(p):
        try:
            with open(p, 'r') as f:
                all_cfg = json.load(f)
        except Exception:
            pass
    all_cfg[k] = data
    with open(p, 'w') as f:
        json.dump(all_cfg, f, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
# SHEET PARSING HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def parse_num(s):
    """
    Parse a drawing number string into (prefix, base_int, suffix).
    Works with or without a space-prefixed format and with or without a dash suffix.
      'CN06/1053-AR-BN 6516-0' -> ('CN06/1053-AR-BN ', 6516, 0)
      '6516-0'                 -> ('', 6516, 0)
      '6516'                   -> ('', 6516, None)   ← None = no explicit suffix
    Returning None for suffix-less sheets prevents them colliding with genuine
    '-0' sheets when both land on the same room base during renumbering.
    Returns None (the whole tuple) if no 4+-digit sequence is found.
    """
    s = (s or '').strip()
    m = NUM_RE.match(s)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    m = NUM_RE_B.match(s)
    if m:
        return m.group(1), int(m.group(2)), None   # no dash-suffix in original
    return None

def norm_sheet_name(name):
    """
    Reduce a sheet Name to just the block/floor tokens for group matching.
    'BLOCK N-1 GROUND FLOOR- RLS'  ->  'BLOCK N-1 GROUND'
    """
    s = (name or '').upper()
    # Strip everything from 'RLS' onwards (including preceding dash/space)
    s = re.sub(r'\s*[-–]?\s*RLS.*$', '', s)
    # Remove FLOOR keyword
    s = re.sub(r'\bFLOOR\b', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def find_group(sheet_name, groups):
    """Return the first group whose name is contained in the normalised sheet name."""
    n = norm_sheet_name(sheet_name)
    for g in groups:
        if g['name'].upper() in n:
            return g
    return None

def room_sort_key(room_num):
    """Sort rooms by numeric suffix: 'N1-028' -> ('N1', 28)."""
    m = re.search(r'(\d+)\s*$', room_num or '')
    return (room_num or '', int(m.group(1)) if m else 0)

def get_rls_sheets(flt=None):
    """Return sheets whose Name contains flt (case-insensitive). Falls back to RLS_FILTER."""
    needle = (flt or RLS_FILTER).upper()
    result = []
    for sh in FilteredElementCollector(doc).OfClass(ViewSheet):
        sname = sh.get_Parameter(P_NAME).AsString() or ''
        if needle in sname.upper():
            result.append(sh)
    return result

def get_sheet_string_params():
    """Return sorted list of string-parameter names found on sheets in this project."""
    names = set()
    sheets = list(FilteredElementCollector(doc).OfClass(ViewSheet))
    for sh in sheets[:15]:   # sample first 15 sheets — fast enough on any model
        try:
            for p in sh.Parameters:
                if p.StorageType == StorageType.String and p.Definition is not None:
                    n = p.Definition.Name
                    if n:
                        names.add(n)
        except Exception:
            pass
    return sorted(names)

# ─────────────────────────────────────────────────────────────────────────────
# SHEET RECORD
# ─────────────────────────────────────────────────────────────────────────────
class SheetRec(object):
    __slots__ = ['sheet', 'element_id', 'sheet_name', 'group_name',
                 'room_num', 'prefix', 'old_base', 'suffix',
                 'cur_num', 'new_num', 'status']

    def __init__(self, sh, gname, room, pfx, old_base, sfx, cur):
        self.sheet      = sh
        self.element_id = sh.Id.IntegerValue
        self.sheet_name = sh.get_Parameter(P_NAME).AsString() or ''
        self.group_name = gname
        self.room_num   = room
        self.prefix     = pfx
        self.old_base   = old_base
        self.suffix     = sfx
        self.cur_num    = cur
        self.new_num    = cur        # will be updated by build_plan
        self.status     = 'PENDING'

# ─────────────────────────────────────────────────────────────────────────────
# BUILD RENUMBER PLAN
# ─────────────────────────────────────────────────────────────────────────────
def do_strip_prefix(staging_prefix, flt=None):
    """
    Find all RLS sheets whose number has <staging_prefix> immediately before
    the 4+-digit base (e.g. 'CN06/1053-AR-BN X6601-0' with prefix 'X'),
    and remove that prefix.  Uses the same two-pass technique as do_renumber
    to avoid conflicts.
    Returns (count_changed, list[str] errors, list[(old,new)] preview).
    """
    if not staging_prefix:
        return 0, ['No prefix specified.'], []

    strip_re = re.compile(
        r'^(.*?)' + re.escape(staging_prefix) + r'(\d{4,})-(\d+)$')

    to_strip = []
    for sh in get_rls_sheets(flt):
        num = sh.get_Parameter(P_NUM).AsString() or ''
        m   = strip_re.match(num)
        if m:
            stripped = '{}{}-{}'.format(m.group(1), m.group(2), m.group(3))
            to_strip.append((sh, num, stripped))

    if not to_strip:
        return 0, [], []

    preview = [(old, new) for (_, old, new) in to_strip]
    errors  = []

    t = Transaction(doc, 'RLS Strip Prefix  v{}'.format(VERSION))
    t.Start()

    # Pass 1 — temp names
    pass1_ok = []
    for i, (sh, old, new) in enumerate(to_strip):
        tmp = '__SP{}__'.format(i)
        try:
            sh.get_Parameter(P_NUM).Set(tmp)
            pass1_ok.append((sh, old, new))
        except Exception as ex:
            errors.append('[P1] {} -> temp: {}'.format(old, str(ex)))

    # Pass 2 — real target names
    count = 0
    for sh, old, new in pass1_ok:
        try:
            sh.get_Parameter(P_NUM).Set(new)
            count += 1
        except Exception as ex:
            errors.append('{} -> {}: {}'.format(old, new, str(ex)))
            try:
                sh.get_Parameter(P_NUM).Set(old)
            except Exception:
                pass

    t.Commit()
    return count, errors, preview


def build_plan(groups, scope=None, flt=None, t2_param=None, staging_prefix=''):
    """
    Collect all RLS sheets, match each to a group, sort rooms, assign
    sequential base numbers within each group range.

    scope          = set of group names to process; None means all groups.
    flt            = sheet name filter string (falls back to RLS_FILTER).
    t2_param       = name of the room-number parameter (falls back to T2_PARAM).
    staging_prefix = string inserted before the 4-digit base in new_num
                     (e.g. 'X' → 'CN06/1053-AR-BN X6601-0').
    Returns (list[SheetRec], list[str] unrecognised_msgs).
    """
    t2 = (t2_param or T2_PARAM).strip() or T2_PARAM
    recs, unrec = [], []

    for sh in get_rls_sheets(flt):
        num   = sh.get_Parameter(P_NUM).AsString() or ''
        sname = sh.get_Parameter(P_NAME).AsString() or ''

        # Scope check first: match group by sheet name, skip out-of-scope sheets
        # entirely so they never appear in unrec when a narrow scope is selected.
        grp   = find_group(sname, groups)
        gname = grp['name'] if grp else None
        if scope and (gname is None or gname not in scope):
            continue

        p = parse_num(num)
        if not p:
            unrec.append('Cannot parse number [{}] on sheet: {}'.format(num, sname))
            continue

        pfx, old_base, sfx = p

        if not grp:
            unrec.append('No group matched for sheet: {} ({})'.format(sname, num))
            continue

        # Room number from the configured room-number parameter — first token
        t2p   = sh.LookupParameter(t2)
        t2v   = (t2p.AsString() if t2p else '') or ''
        parts = t2v.strip().split()
        room  = parts[0] if parts else 'ROOM_{}'.format(old_base)

        recs.append(SheetRec(sh, gname, room, pfx, old_base, sfx, num))

    # Assign new base numbers group by group
    by_grp = {}
    for r in recs:
        by_grp.setdefault(r.group_name, []).append(r)

    for g in groups:
        gname = g['name']
        if gname not in by_grp:
            continue
        if scope and gname not in scope:
            continue

        rng_start = int(g['start'])
        rng_end   = int(g['end'])

        # Group sheets by room, then sort rooms
        by_room = {}
        for r in by_grp[gname]:
            by_room.setdefault(r.room_num, []).append(r)

        cur_base = rng_start
        for room in sorted(by_room.keys(), key=room_sort_key):
            # Sort: None-suffix sheets first (they had no dash), then by int suffix
            room_sheets = sorted(by_room[room],
                                 key=lambda r: (-1 if r.suffix is None else r.suffix))

            if cur_base > rng_end:
                for r in room_sheets:
                    r.status = 'OUT OF RANGE'
                continue

            has_suffixed = False
            for r in room_sheets:
                if r.suffix is None:
                    # No -0/-1/-2/-3 suffix → exclude from renumbering entirely
                    r.new_num = r.cur_num
                    r.status  = 'UNCHANGED'
                    continue
                new = '{}{}{}-{}'.format(r.prefix, staging_prefix, cur_base, r.suffix)
                r.new_num = new
                r.status  = 'CHANGED' if new != r.cur_num else 'UNCHANGED'
                has_suffixed = True

            if has_suffixed:
                cur_base += 1

    # ── Mark intra-batch duplicate targets so the preview flags them visually ──
    from collections import Counter
    counts = Counter(r.new_num for r in recs if r.status == 'CHANGED')
    for r in recs:
        if r.status == 'CHANGED' and counts[r.new_num] > 1:
            r.status = 'DUPLICATE'

    return recs, unrec

# ─────────────────────────────────────────────────────────────────────────────
# APPLY RENUMBER
# ─────────────────────────────────────────────────────────────────────────────
def _all_sheet_numbers(exclude_ids=None):
    """Return {sheet_number: sheet_name} for all sheets not in exclude_ids."""
    result = {}
    for sh in FilteredElementCollector(doc).OfClass(ViewSheet):
        if exclude_ids and sh.Id.IntegerValue in exclude_ids:
            continue
        num   = sh.get_Parameter(P_NUM).AsString() or ''
        sname = sh.get_Parameter(P_NAME).AsString() or ''
        if num:
            result[num] = sname
    return result


def check_conflicts(recs):
    """
    Return (external_conflicts, internal_duplicates).

    external_conflicts : list of (proposed_num, blocking_sheet_name)
        Proposed numbers already used by sheets OUTSIDE the batch.

    internal_duplicates : list of (proposed_num, [sheet_names_in_batch])
        Proposed numbers assigned to MORE THAN ONE sheet WITHIN the batch
        (build_plan collision — Revit will reject all but the first).
    """
    to_do     = [r for r in recs if r.status == 'CHANGED']
    batch_ids = {r.element_id for r in to_do}

    # ── internal: detect duplicate targets within the batch ─────────────────
    from collections import defaultdict
    target_groups = defaultdict(list)
    for r in to_do:
        target_groups[r.new_num].append(r.sheet_name)
    internal_duplicates = [
        (num, names) for num, names in target_groups.items() if len(names) > 1
    ]

    # ── external: targets that clash with non-batch sheets ──────────────────
    target_set = set(target_groups.keys())
    existing   = _all_sheet_numbers(exclude_ids=batch_ids)
    external_conflicts = [
        (num, sname) for num, sname in existing.items() if num in target_set
    ]

    return external_conflicts, internal_duplicates


def do_renumber(recs):
    """
    Apply renumber plan.
    Two-pass inside a single transaction eliminates within-batch conflicts.
    Revit's Sheet Number Set() can fail *silently* (no exception), so we
    verify success by reading back the value after every Set(), and restore
    failures to their original number rather than leaving them with a temp name.
    Returns (count_changed, list[str] errors).
    """
    to_do = [r for r in recs if r.status == 'CHANGED']
    if not to_do:
        return 0, []

    errors = []
    t = Transaction(doc, 'RLS Sheet Renumber  v{}'.format(VERSION))
    t.Start()

    p_num = lambda sh: sh.get_Parameter(P_NUM)

    # ── Pass 1: set guaranteed-unique temp names ──────────────────────────────
    # Track original number alongside each record so we can restore on failure.
    pass1_ok = []   # list of (rec, original_num, temp_name)
    for i, r in enumerate(to_do):
        tmp = '__RN{}__'.format(i)
        p_num(r.sheet).Set(tmp)
        actual = p_num(r.sheet).AsString()
        if actual == tmp:
            pass1_ok.append((r, r.cur_num, tmp))
        else:
            # Set() rejected silently — leave sheet with its original number
            errors.append('[P1] Could not set temp name for: {} (kept {})'.format(
                r.sheet_name, r.cur_num))

    # ── Pass 2: set real target numbers ──────────────────────────────────────
    count = 0
    for rec, orig_num, tmp in pass1_ok:
        p_num(rec.sheet).Set(rec.new_num)
        actual = p_num(rec.sheet).AsString()
        if actual == rec.new_num:
            count += 1
        else:
            # Target rejected (conflict with non-batch sheet) — restore original
            p_num(rec.sheet).Set(orig_num)
            actual2 = p_num(rec.sheet).AsString()
            if actual2 == orig_num:
                errors.append('[CONFLICT] {} → {} blocked by another sheet '
                              '(restored to {})'.format(
                                  orig_num, rec.new_num, orig_num))
            else:
                # Restore also failed — last resort: leave with temp name
                errors.append('[STUCK] {} → {} blocked; restore to {} also '
                              'failed — sheet left as {}'.format(
                                  orig_num, rec.new_num, orig_num, tmp))

    t.Commit()
    return count, errors

# ─────────────────────────────────────────────────────────────────────────────
# BACKUP  AND  RUN LOGGING
# ─────────────────────────────────────────────────────────────────────────────
def _ts():
    return datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

def _safe_key():
    return re.sub(r'[^\w]', '_', project_key())

def save_backup(recs, folder):
    """
    Write ElementID,SheetName,CurrentNumber CSV before any changes.
    Returns (full_path, filename).
    """
    fname = 'RLS_Renumber_BACKUP_{}_{}.csv'.format(_safe_key(), _ts())
    fpath = os.path.join(folder, fname)
    lines = ['ElementID,SheetName,CurrentNumber']
    for r in recs:
        lines.append('{},{},{}'.format(
            r.element_id,
            r.sheet_name.replace(',', ';'),
            r.cur_num))
    with open(fpath, 'w') as f:
        f.write('\n'.join(lines))
    return fpath, fname

def write_run_log(recs, unrec, folder):
    """Write .txt narrative + .csv full record after a renumber run."""
    base     = 'RLS_Renumber_{}_{}'.format(_safe_key(), _ts())
    txt_path = os.path.join(folder, base + '.txt')
    csv_path = os.path.join(folder, base + '.csv')

    changed   = [r for r in recs if r.status == 'CHANGED']
    unchanged = [r for r in recs if r.status == 'UNCHANGED']
    oor       = [r for r in recs if r.status == 'OUT OF RANGE']
    skipped   = [r for r in recs if r.status == 'SKIPPED']
    now_str   = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        '=' * 68,
        'RLS SHEET RENUMBER  v{}  --  RUN LOG'.format(VERSION),
        '=' * 68,
        'Project  : {}'.format(doc.Title),
        'Run at   : {}'.format(now_str),
        '',
        'Changed  : {:>4}'.format(len(changed)),
        'Unchanged: {:>4}'.format(len(unchanged)),
        'Skipped  : {:>4}'.format(len(skipped)),
        'OOR      : {:>4}'.format(len(oor)),
        'Unrecog  : {:>4}'.format(len(unrec)),
        '',
        '-' * 68,
        '{:<32}  {:>22}    {}'.format('SHEET NAME', 'OLD NUMBER', 'NEW NUMBER'),
        '-' * 68,
    ]
    for r in sorted(changed, key=lambda x: (x.group_name, room_sort_key(x.room_num))):
        lines.append('{:<32}  {:>22} -> {}'.format(
            r.sheet_name[:32], r.cur_num, r.new_num))

    if oor:
        lines += ['', '-' * 68, 'OUT OF RANGE (not renumbered)', '-' * 68]
        for r in oor:
            lines.append('  {:<32}  Room: {}'.format(r.sheet_name[:32], r.room_num))

    if skipped:
        lines += ['', '-' * 68, 'SKIPPED (excluded in preview — not changed)', '-' * 68]
        for r in sorted(skipped,
                        key=lambda x: (x.group_name, room_sort_key(x.room_num))):
            lines.append('  {:<32}  Would have been: {}'.format(
                r.sheet_name[:32], r.new_num))

    if unrec:
        lines += ['', '-' * 68, 'UNRECOGNIZED (skipped)', '-' * 68]
        for msg in unrec:
            lines.append('  ' + msg)

    lines += ['', '=' * 68, 'END OF LOG', '=' * 68]

    with codecs.open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    csv_lines = ['Group,Room,SheetName,OldNumber,NewNumber,Status']
    for r in sorted(recs, key=lambda x: (x.group_name, room_sort_key(x.room_num), x.suffix)):
        csv_lines.append('{},{},{},{},{},{}'.format(
            r.group_name, r.room_num,
            r.sheet_name.replace(',', ';'),
            r.cur_num, r.new_num, r.status))

    with open(csv_path, 'w') as f:
        f.write('\n'.join(csv_lines))

    return txt_path, csv_path

# ─────────────────────────────────────────────────────────────────────────────
# RESTORE FROM BACKUP
# ─────────────────────────────────────────────────────────────────────────────
def parse_backup(csv_path):
    """
    Read backup CSV, match by Element ID.
    Returns (list[dict], error_str_or_None).
    """
    if not os.path.exists(csv_path):
        return None, 'File not found: {}'.format(csv_path)

    by_id = {sh.Id.IntegerValue: sh
             for sh in FilteredElementCollector(doc).OfClass(ViewSheet)}
    results = []
    try:
        with open(csv_path, 'r') as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                eid  = int(row['ElementID'])
                old  = (row['CurrentNumber'] or '').strip()
                sh   = by_id.get(eid)
                curr = sh.get_Parameter(P_NUM).AsString() if sh else '(not found)'
                results.append({
                    'eid':        eid,
                    'sheet':      sh,
                    'name':       (row.get('SheetName') or '').strip(),
                    'restore_to': old,
                    'current':    curr,
                    'will_change': sh is not None and curr != old,
                })
    except Exception as ex:
        return None, str(ex)
    return results, None

def do_restore(restore_recs, folder=''):
    to_do  = [r for r in restore_recs if r['will_change']]
    errors = []
    if not to_do:
        return 0, []

    t = Transaction(doc, 'RLS Sheet Renumber - Restore from Backup')
    t.Start()
    count = 0
    for r in to_do:
        try:
            r['sheet'].get_Parameter(P_NUM).Set(r['restore_to'])
            count += 1
        except Exception as ex:
            errors.append('EID {}: {}'.format(r['eid'], str(ex)))
    t.Commit()

    if folder and os.path.isdir(folder):
        log_path = os.path.join(
            folder,
            'RLS_Renumber_RESTORE_{}_{}.txt'.format(_safe_key(), _ts()))
        lines = [
            '=' * 68,
            'RLS SHEET RENUMBER  v{}  --  RESTORE LOG'.format(VERSION),
            '=' * 68,
            'Project: {}'.format(doc.Title),
            'Run at : {}'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
            'Restored: {}'.format(count), '',
            '-' * 68,
        ]
        for r in to_do:
            lines.append('  EID {:>8}  {:>22} -> {}'.format(
                r['eid'], r['current'], r['restore_to']))
        lines += ['', '=' * 68]
        with codecs.open(log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

    return count, errors

# ─────────────────────────────────────────────────────────────────────────────
# WPF DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────
class GroupRow(object):
    """One editable row in the groups DataGrid."""
    def __init__(self, name, start, end):
        self.Name  = str(name)
        self.Start = str(start)
        self.End   = str(end)

    def to_dict(self):
        try:
            return {'name': self.Name.strip(),
                    'start': int(self.Start),
                    'end':   int(self.End)}
        except (ValueError, TypeError):
            return None

class PreviewRow(System.ComponentModel.INotifyPropertyChanged):
    """One row in the preview DataGrid.
    Implements INotifyPropertyChanged so the RowStyle DataTrigger on
    Excluded reacts immediately when the Skip checkbox is ticked."""

    PropertyChanged = None   # INotifyPropertyChanged event placeholder

    def __init__(self, group, room, sheet_name, current, proposed, status,
                 element_id=None):
        self.Group     = group
        self.Room      = room
        self.SheetName = sheet_name
        self.Current   = current
        self.Proposed  = proposed
        self.Status    = status
        self._excluded = False          # backing field
        self.ElementId = element_id     # links back to SheetRec for exclusion

    # ── INotifyPropertyChanged wiring ──────────────────────────────────────
    def add_PropertyChanged(self, handler):
        self.PropertyChanged = System.Delegate.Combine(self.PropertyChanged, handler)

    def remove_PropertyChanged(self, handler):
        self.PropertyChanged = System.Delegate.Remove(self.PropertyChanged, handler)

    def _raise(self, name):
        if self.PropertyChanged:
            self.PropertyChanged(
                self,
                System.ComponentModel.PropertyChangedEventArgs(name)
            )

    # ── Excluded property ───────────────────────────────────────────────────
    def _get_excluded(self):
        return self._excluded

    def _set_excluded(self, value):
        if self._excluded != value:
            self._excluded = value
            self._raise('Excluded')

    Excluded = property(_get_excluded, _set_excluded)

# ─────────────────────────────────────────────────────────────────────────────
# XAML
# ─────────────────────────────────────────────────────────────────────────────
XAML = r"""<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="RLS Sheet Renumber  v{ver}"
    Height="1100" Width="760"
    MinHeight="720" MinWidth="640"
    Background="#FF1E1E1E"
    Foreground="#FFE0E0E0"
    FontFamily="Segoe UI"
    FontSize="12"
    WindowStartupLocation="CenterScreen">

  <Window.Resources>

    <!-- TextBox -->
    <Style TargetType="TextBox">
      <Setter Property="Background"             Value="#FF2D2D2D"/>
      <Setter Property="Foreground"             Value="#FFE0E0E0"/>
      <Setter Property="BorderBrush"            Value="#FF444444"/>
      <Setter Property="BorderThickness"        Value="1"/>
      <Setter Property="Padding"                Value="6,3"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="CaretBrush"             Value="#FFE0E0E0"/>
    </Style>

    <!-- CheckBox -->
    <Style TargetType="CheckBox">
      <Setter Property="Foreground" Value="#FFE0E0E0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>

    <!-- ComboBox — full ControlTemplate so the Popup gets a dark background -->
    <Style TargetType="ComboBoxItem">
      <Setter Property="Background"  Value="#FF2D2D2D"/>
      <Setter Property="Foreground"  Value="#FFE0E0E0"/>
      <Setter Property="Padding"     Value="6,3"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ComboBoxItem">
            <Border Background="{TemplateBinding Background}"
                    Padding="{TemplateBinding Padding}">
              <ContentPresenter/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="Background" Value="#FF3D3D3D"/>
              </Trigger>
              <Trigger Property="IsSelected" Value="True">
                <Setter Property="Background" Value="#FF4FC3F7"/>
                <Setter Property="Foreground" Value="#FF1A1A1A"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Background"      Value="#FF2D2D2D"/>
      <Setter Property="Foreground"      Value="#FFE0E0E0"/>
      <Setter Property="BorderBrush"     Value="#FF444444"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="4,2"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ComboBox">
            <Grid>
              <!-- Closed-state box -->
              <Border x:Name="Border"
                      Background="{TemplateBinding Background}"
                      BorderBrush="{TemplateBinding BorderBrush}"
                      BorderThickness="{TemplateBinding BorderThickness}"
                      CornerRadius="2">
                <Grid>
                  <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="22"/>
                  </Grid.ColumnDefinitions>
                  <!-- non-editable display -->
                  <ContentPresenter x:Name="ContentSite"
                      Grid.Column="0"
                      Content="{TemplateBinding SelectionBoxItem}"
                      ContentTemplate="{TemplateBinding SelectionBoxItemTemplate}"
                      Margin="4,2,0,2"
                      VerticalAlignment="Center"/>
                  <!-- editable text box (visible when IsEditable=True) -->
                  <TextBox x:Name="PART_EditableTextBox"
                      Grid.Column="0"
                      Background="Transparent"
                      Foreground="{TemplateBinding Foreground}"
                      BorderThickness="0"
                      Padding="4,2,0,2"
                      Visibility="Collapsed"
                      VerticalAlignment="Center"/>
                  <!-- chevron toggle -->
                  <ToggleButton Grid.Column="1"
                      IsChecked="{Binding Path=IsDropDownOpen, Mode=TwoWay,
                                          RelativeSource={RelativeSource TemplatedParent}}"
                      Background="Transparent" BorderThickness="0"
                      Focusable="False" Cursor="Hand">
                    <Path Data="M 0 0 L 4 4 L 8 0 Z"
                          Fill="#FFE0E0E0" HorizontalAlignment="Center"
                          VerticalAlignment="Center"/>
                  </ToggleButton>
                </Grid>
              </Border>
              <!-- Dropdown popup with explicit dark background -->
              <Popup x:Name="PART_Popup"
                     IsOpen="{TemplateBinding IsDropDownOpen}"
                     Placement="Bottom"
                     AllowsTransparency="True"
                     Focusable="False"
                     PopupAnimation="Slide">
                <Border Background="#FF2D2D2D"
                        BorderBrush="#FF555555"
                        BorderThickness="1"
                        MinWidth="{TemplateBinding ActualWidth}"
                        MaxHeight="{TemplateBinding MaxDropDownHeight}"
                        SnapsToDevicePixels="True">
                  <ScrollViewer>
                    <ItemsPresenter/>
                  </ScrollViewer>
                </Border>
              </Popup>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsEditable" Value="True">
                <Setter TargetName="ContentSite"        Property="Visibility" Value="Collapsed"/>
                <Setter TargetName="PART_EditableTextBox" Property="Visibility" Value="Visible"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="Border" Property="Opacity" Value="0.5"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- Buttons -->
    <Style x:Key="GrayBtn" TargetType="Button">
      <Setter Property="Background"      Value="#FF2D2D2D"/>
      <Setter Property="Foreground"      Value="#FFE0E0E0"/>
      <Setter Property="BorderBrush"     Value="#FF555555"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="8,4"/>
      <Setter Property="Cursor"          Value="Hand"/>
    </Style>
    <Style x:Key="BlueBtn" TargetType="Button">
      <Setter Property="Background"      Value="#FF4FC3F7"/>
      <Setter Property="Foreground"      Value="#FF1A1A1A"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding"         Value="12,6"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="FontWeight"      Value="SemiBold"/>
    </Style>
    <Style x:Key="TealBtn" TargetType="Button">
      <Setter Property="Background"      Value="#FF4DB6AC"/>
      <Setter Property="Foreground"      Value="#FF1A1A1A"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding"         Value="12,6"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="FontWeight"      Value="Bold"/>
    </Style>
    <Style x:Key="AmberBtn" TargetType="Button">
      <Setter Property="Background"      Value="#FFFFB74D"/>
      <Setter Property="Foreground"      Value="#FF1A1A1A"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding"         Value="12,6"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="FontWeight"      Value="SemiBold"/>
    </Style>

    <!-- Labels -->
    <Style x:Key="SectionLabel" TargetType="TextBlock">
      <Setter Property="Foreground"  Value="#FFF4723A"/>
      <Setter Property="FontWeight"  Value="SemiBold"/>
      <Setter Property="FontSize"    Value="10.5"/>
    </Style>
    <Style x:Key="HintLabel" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#FF888780"/>
      <Setter Property="FontSize"   Value="10"/>
    </Style>

    <!-- DataGrid -->
    <Style TargetType="DataGridColumnHeader">
      <Setter Property="Background"      Value="#FF181818"/>
      <Setter Property="Foreground"      Value="#FFCCCCCC"/>
      <Setter Property="Padding"         Value="8,5"/>
      <Setter Property="BorderBrush"     Value="#FF404040"/>
      <Setter Property="BorderThickness" Value="0,0,1,1"/>
      <Setter Property="FontWeight"      Value="SemiBold"/>
    </Style>
    <Style TargetType="DataGridCell">
      <Setter Property="Background"      Value="Transparent"/>
      <Setter Property="Foreground"      Value="#FFE0E0E0"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding"         Value="4,2"/>
    </Style>
    <Style TargetType="DataGrid">
      <Setter Property="Background"           Value="#FF262626"/>
      <Setter Property="Foreground"           Value="#FFE0E0E0"/>
      <Setter Property="BorderBrush"          Value="#FF444444"/>
      <Setter Property="BorderThickness"      Value="1"/>
      <Setter Property="RowBackground"        Value="#FF262626"/>
      <Setter Property="AlternatingRowBackground" Value="#FF2E2E2E"/>
      <Setter Property="GridLinesVisibility"  Value="Horizontal"/>
      <Setter Property="HorizontalGridLinesBrush" Value="#FF3A3A3A"/>
      <Setter Property="ColumnHeaderHeight"   Value="28"/>
      <Setter Property="RowHeight"            Value="26"/>
      <Setter Property="SelectionMode"        Value="Single"/>
    </Style>

  </Window.Resources>

  <ScrollViewer x:Name="OuterScroll"
                VerticalScrollBarVisibility="Auto"
                HorizontalScrollBarVisibility="Disabled"
                Background="#FF1E1E1E">
    <StackPanel Margin="20,16,20,28">

      <!-- ══ HEADER ══════════════════════════════════════════════════════ -->
      <TextBlock Text="RLS Sheet Renumber"
                 FontSize="20" FontWeight="Bold" Foreground="#FF4FC3F7"/>
      <TextBlock x:Name="SubtitleBlock"
                 Foreground="#FF888780" FontSize="10.5" Margin="0,3,0,20"/>

      <!-- ══ CONFIGURATION ═══════════════════════════════════════════════ -->
      <TextBlock Text="CONFIGURATION" Style="{StaticResource SectionLabel}" Margin="0,0,0,8"/>

      <!-- Log Folder -->
      <Grid Margin="0,0,0,6">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="88"/>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="72"/>
          <ColumnDefinition Width="52"/>
        </Grid.ColumnDefinitions>
        <TextBlock Text="Log Folder:" Foreground="#FFE0E0E0" VerticalAlignment="Center"/>
        <TextBox x:Name="LogFolderBox" Grid.Column="1" Margin="0,0,6,0" Height="28"/>
        <Button x:Name="BrowseBtn"     Grid.Column="2" Content="Browse…"
                Style="{StaticResource GrayBtn}" Height="28" Margin="0,0,6,0"/>
        <Button x:Name="OpenFolderBtn" Grid.Column="3" Content="Open"
                Style="{StaticResource GrayBtn}" Height="28"/>
      </Grid>

      <!-- Sheet Filter -->
      <Grid Margin="0,0,0,6">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="88"/>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBlock Text="Sheet Filter:" Foreground="#FFE0E0E0" VerticalAlignment="Center"/>
        <TextBox x:Name="SheetFilterBox" Grid.Column="1" Margin="0,0,8,0" Height="28"/>
        <TextBlock x:Name="MatchCountLabel" Grid.Column="2"
                   Foreground="#FF4DB6AC" FontSize="10"
                   VerticalAlignment="Center" MinWidth="110"/>
      </Grid>

      <!-- Room Parameter -->
      <Grid Margin="0,0,0,3">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="88"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>
        <TextBlock Text="Room Param:" Foreground="#FFE0E0E0" VerticalAlignment="Center"/>
        <ComboBox x:Name="T2ParamBox" Grid.Column="1" Height="28" IsEditable="True"/>
      </Grid>

      <TextBlock x:Name="ProjectLabel" Style="{StaticResource HintLabel}"
                 Margin="88,3,0,0"/>

      <Border Height="1" Background="#FF333333" Margin="0,14,0,14"/>

      <!-- ══ NUMBER RANGES ═══════════════════════════════════════════════ -->
      <Grid Margin="0,0,0,8">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBlock Text="NUMBER RANGES" Style="{StaticResource SectionLabel}"
                   VerticalAlignment="Center"/>
        <StackPanel Grid.Column="1" Orientation="Horizontal">
          <Button x:Name="AddGroupBtn"    Content="＋ Add"    Width="62"
                  Style="{StaticResource GrayBtn}" Margin="0,0,6,0" Height="26"/>
          <Button x:Name="RemGroupBtn"    Content="－ Remove" Width="74"
                  Style="{StaticResource GrayBtn}" Margin="0,0,6,0" Height="26"/>
          <Button x:Name="ResetGroupsBtn" Content="Defaults"  Width="68"
                  Style="{StaticResource GrayBtn}" Height="26"/>
        </StackPanel>
      </Grid>

      <DataGrid x:Name="GroupsGrid"
                AutoGenerateColumns="False"
                CanUserAddRows="False"
                CanUserDeleteRows="False"
                CanUserReorderColumns="False"
                CanUserResizeRows="False"
                Height="158" Margin="0,0,0,4">
        <DataGrid.Columns>
          <DataGridTextColumn Header="Group Name"
              Binding="{Binding Name,  Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
              Width="*" MinWidth="160"/>
          <DataGridTextColumn Header="Start"
              Binding="{Binding Start, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
              Width="68"/>
          <DataGridTextColumn Header="End"
              Binding="{Binding End,   Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
              Width="68"/>
        </DataGrid.Columns>
      </DataGrid>
      <TextBlock Style="{StaticResource HintLabel}"
          Text="Edit cells directly. Group Name must contain the same keywords as the Revit sheet Name (e.g. BLOCK N-1 GROUND)."/>

      <Border Height="1" Background="#FF333333" Margin="0,14,0,14"/>

      <!-- ══ SCOPE FILTER ════════════════════════════════════════════════ -->
      <Grid Margin="0,0,0,8">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBlock Text="SCOPE FILTER" Style="{StaticResource SectionLabel}"
                   VerticalAlignment="Center"/>
        <StackPanel Grid.Column="1" Orientation="Horizontal">
          <Button x:Name="SelectAllBtn"  Content="All"  Width="46"
                  Style="{StaticResource GrayBtn}" Margin="0,0,6,0" Height="26"/>
          <Button x:Name="SelectNoneBtn" Content="None" Width="46"
                  Style="{StaticResource GrayBtn}" Height="26"/>
        </StackPanel>
      </Grid>

      <Border BorderBrush="#FF444444" BorderThickness="1" Margin="0,0,0,4">
        <ScrollViewer x:Name="ScopeScroll" Height="116"
                      VerticalScrollBarVisibility="Auto"
                      Background="#FF262626">
          <StackPanel x:Name="ScopePanel" Margin="12,8,12,8"/>
        </ScrollViewer>
      </Border>
      <TextBlock Style="{StaticResource HintLabel}"
          Text="Uncheck groups to exclude from this run. Click Dry Run after editing ranges to refresh scope."/>

      <Border Height="1" Background="#FF333333" Margin="0,14,0,14"/>

      <!-- ══ ACTION BUTTONS ══════════════════════════════════════════════ -->
      <Grid Margin="0,0,0,14">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>
        <Button x:Name="DryRunBtn"  Grid.Column="0"
                Content="Dry Run / Preview"
                Style="{StaticResource BlueBtn}"  Height="38" Margin="0,0,7,0"/>
        <Button x:Name="CommitBtn"  Grid.Column="1"
                Content="✔  Renumber Sheets"
                Style="{StaticResource TealBtn}"  Height="38" Margin="0,0,7,0"/>
        <Button x:Name="RestoreBtn" Grid.Column="2"
                Content="↩  Restore from Backup"
                Style="{StaticResource AmberBtn}" Height="38"/>
      </Grid>

      <!-- ══ STAGING OPTIONS ══════════════════════════════════════════ -->
      <Border Background="#FF262626" BorderBrush="#FF383838" BorderThickness="1"
              CornerRadius="3" Padding="12,8" Margin="0,0,0,14">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="54"/>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <CheckBox x:Name="PrefixChk" Grid.Column="0"
                    VerticalAlignment="Center" Margin="0,0,10,0">
            <TextBlock Text="Stage with prefix:" Foreground="#FFE0E0E0"
                       VerticalAlignment="Center"/>
          </CheckBox>
          <TextBox  x:Name="PrefixBox" Grid.Column="1" Text="X"
                    Height="24" VerticalContentAlignment="Center"
                    Padding="6,0" IsEnabled="False"/>
          <TextBlock Grid.Column="2" VerticalAlignment="Center"
                     Foreground="#FF666666" FontSize="11" Margin="10,0,0,0"
                     TextWrapping="Wrap"
                     Text="Inserts prefix before base digits  (e.g. X6516). Sheets stay visible as 'halfway done'. Use Strip Prefix when ready to finalise."/>
          <Button x:Name="StripPrefixBtn" Grid.Column="3"
                  Content="Strip Prefix"
                  Style="{StaticResource AmberBtn}"
                  Height="28" Padding="12,3" Margin="10,0,0,0"/>
        </Grid>
      </Border>

      <!-- ══ PREVIEW ════════════════════════════════════════════════════ -->
      <TextBlock x:Name="PreviewHeader"
                 Style="{StaticResource SectionLabel}"
                 Margin="0,0,0,8"
                 Text="PREVIEW  —  run Dry Run first"/>

      <DataGrid x:Name="PreviewGrid"
                AutoGenerateColumns="False"
                CanUserAddRows="False"
                CanUserDeleteRows="False"
                IsReadOnly="False"
                HeadersVisibility="Column"
                Height="312" Margin="0,0,0,4">
        <!-- Row goes red-tinted + dimmed when its Skip checkbox is ticked.
             Background (not Foreground) is used because per-column CellStyle
             Foreground setters win over a RowStyle Foreground trigger. -->
        <DataGrid.RowStyle>
          <Style TargetType="DataGridRow">
            <Style.Triggers>
              <DataTrigger Binding="{Binding Excluded}" Value="True">
                <Setter Property="Background" Value="#55EF5350"/>
                <Setter Property="Opacity"    Value="0.65"/>
              </DataTrigger>
            </Style.Triggers>
          </Style>
        </DataGrid.RowStyle>
        <DataGrid.Columns>

          <!-- Skip checkbox — directly clickable, one-time per dry run -->
          <DataGridTemplateColumn Header="Skip" Width="54"
                                  CanUserSort="False" CanUserResize="False">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <CheckBox IsChecked="{Binding Excluded, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                          HorizontalAlignment="Center" VerticalAlignment="Center"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>

          <DataGridTextColumn Header="Group"
              Binding="{Binding Group}"     Width="130" MinWidth="80" IsReadOnly="True"/>
          <DataGridTextColumn Header="Room"
              Binding="{Binding Room}"      Width="66" IsReadOnly="True"/>
          <DataGridTextColumn Header="Sheet Name"
              Binding="{Binding SheetName}" Width="*"   MinWidth="100" IsReadOnly="True"/>
          <DataGridTextColumn Header="Current"
              Binding="{Binding Current}"   Width="140" IsReadOnly="True"/>

          <!-- Proposed — blue so it reads distinctly from Current -->
          <DataGridTextColumn Header="Proposed"
              Binding="{Binding Proposed}"  Width="140" IsReadOnly="True">
            <DataGridTextColumn.CellStyle>
              <Style TargetType="DataGridCell">
                <Setter Property="Foreground"      Value="#FF4FC3F7"/>
                <Setter Property="BorderThickness" Value="0"/>
                <Style.Triggers>
                  <DataTrigger Binding="{Binding Status}" Value="UNCHANGED">
                    <Setter Property="Foreground" Value="#FF666666"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="OUT OF RANGE">
                    <Setter Property="Foreground" Value="#FFEF5350"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="SKIPPED">
                    <Setter Property="Foreground" Value="#FF555555"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="DUPLICATE">
                    <Setter Property="Foreground" Value="#FFBA68C8"/>
                  </DataTrigger>
                </Style.Triggers>
              </Style>
            </DataGridTextColumn.CellStyle>
          </DataGridTextColumn>

          <DataGridTextColumn Header="Status"
              Binding="{Binding Status}"    Width="90" IsReadOnly="True">
            <DataGridTextColumn.CellStyle>
              <Style TargetType="DataGridCell">
                <Setter Property="Foreground"      Value="#FFE0E0E0"/>
                <Setter Property="BorderThickness" Value="0"/>
                <Style.Triggers>
                  <DataTrigger Binding="{Binding Status}" Value="CHANGED">
                    <Setter Property="Foreground" Value="#FF81C784"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="UNCHANGED">
                    <Setter Property="Foreground" Value="#FF666666"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="OUT OF RANGE">
                    <Setter Property="Foreground" Value="#FFEF5350"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="SKIPPED">
                    <Setter Property="Foreground" Value="#FF888780"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="DUPLICATE">
                    <Setter Property="Foreground" Value="#FFBA68C8"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="RESTORE">
                    <Setter Property="Foreground" Value="#FFFFB74D"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="NOT FOUND">
                    <Setter Property="Foreground" Value="#FFEF5350"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Status}" Value="NO CHANGE">
                    <Setter Property="Foreground" Value="#FF666666"/>
                  </DataTrigger>
                </Style.Triggers>
              </Style>
            </DataGridTextColumn.CellStyle>
          </DataGridTextColumn>
        </DataGrid.Columns>
      </DataGrid>

      <TextBlock x:Name="PreviewSummary"
                 Style="{StaticResource HintLabel}" Margin="0,3,0,0"/>

      <Border Height="1" Background="#FF333333" Margin="0,14,0,14"/>

      <!-- ══ LOG ════════════════════════════════════════════════════════ -->
      <TextBlock Text="LOG" Foreground="#FF4DB6AC"
                 FontWeight="SemiBold" FontSize="10.5" Margin="0,0,0,6"/>
      <TextBox x:Name="LogBox"
               Height="150"
               Background="#FF181818"
               Foreground="#FFD0D0D0"
               FontFamily="Consolas"
               FontSize="10.5"
               IsReadOnly="True"
               VerticalScrollBarVisibility="Auto"
               HorizontalScrollBarVisibility="Auto"
               TextWrapping="NoWrap"/>

    </StackPanel>
  </ScrollViewer>
</Window>""".replace('{ver}', VERSION)

# ─────────────────────────────────────────────────────────────────────────────
# DARK DIALOG HELPERS  (replace system MessageBox so theme stays consistent)
# ─────────────────────────────────────────────────────────────────────────────
_DARK_DLG_XAML = u"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="{title}" SizeToContent="WidthAndHeight"
        MinWidth="320" MaxWidth="520" MinHeight="150"
        WindowStartupLocation="CenterScreen"
        ResizeMode="NoResize"
        Background="#FF1E1E1E"
        Foreground="#FFE0E0E0"
        FontFamily="Segoe UI" FontSize="13">
  <Window.Resources>
    <Style TargetType="Button">
      <Setter Property="Background"      Value="#FF2D2D2D"/>
      <Setter Property="Foreground"      Value="#FFE0E0E0"/>
      <Setter Property="BorderBrush"     Value="#FF555555"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="22,6"/>
      <Setter Property="Margin"          Value="6,0"/>
      <Setter Property="MinWidth"        Value="80"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}"
                    CornerRadius="3"
                    Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="Background" Value="#FF3A3A3A"/>
                <Setter Property="BorderBrush" Value="#FF888888"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter Property="Background" Value="#FF222222"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>
  <StackPanel Margin="28,24,28,20">
    <TextBlock x:Name="MsgTxt" TextWrapping="Wrap" MaxWidth="460"
               Foreground="#FFE0E0E0" LineHeight="20" Margin="0,0,0,20"
               Text="{msg}"/>
    <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
      {buttons}
    </StackPanel>
  </StackPanel>
</Window>"""


def _build_dark_dlg(title, msg, buttons_xml):
    xaml = (_DARK_DLG_XAML
            .replace('{title}', title)
            .replace('{msg}',   msg)
            .replace('{buttons}', buttons_xml))
    win = XamlReader.Load(XmlReader.Create(StringReader(xaml)))
    return win


def dark_confirm(title, msg):
    """Show a dark Yes/No dialog.  Returns True if Yes was clicked."""
    btns = (u'<Button x:Name="YesBtn" Content="Yes"/>'
            u'<Button x:Name="NoBtn"  Content="No"/>')
    win  = _build_dark_dlg(title, msg, btns)
    result = [False]
    yes_btn = win.FindName('YesBtn')
    no_btn  = win.FindName('NoBtn')
    # Style Yes button with accent colour
    yes_btn.Background  = SWM.SolidColorBrush(SWM.Color.FromArgb(255, 79, 195, 247))
    yes_btn.Foreground  = SWM.SolidColorBrush(SWM.Color.FromArgb(255, 20, 20, 20))
    yes_btn.BorderBrush = SWM.SolidColorBrush(SWM.Color.FromArgb(255, 79, 195, 247))
    def _yes(s, e):
        result[0] = True
        win.Close()
    def _no(s, e):
        win.Close()
    yes_btn.Click += _yes
    no_btn.Click  += _no
    win.ShowDialog()
    return result[0]


def dark_msgbox(title, msg):
    """Show a dark informational / warning dialog with an OK button."""
    btns = u'<Button x:Name="OkBtn" Content="OK"/>'
    win  = _build_dark_dlg(title, msg, btns)
    ok_btn = win.FindName('OkBtn')
    ok_btn.Background  = SWM.SolidColorBrush(SWM.Color.FromArgb(255, 79, 195, 247))
    ok_btn.Foreground  = SWM.SolidColorBrush(SWM.Color.FromArgb(255, 20, 20, 20))
    ok_btn.BorderBrush = SWM.SolidColorBrush(SWM.Color.FromArgb(255, 79, 195, 247))
    def _ok(s, e):
        win.Close()
    ok_btn.Click += _ok
    win.ShowDialog()


# ─────────────────────────────────────────────────────────────────────────────
# WPF DIALOG
# ─────────────────────────────────────────────────────────────────────────────
class RenumberDialog(object):

    def __init__(self):
        self._cfg       = load_cfg()
        self._grp_col   = ObservableCollection[object](
            [GroupRow(g['name'], g['start'], g['end'])
             for g in self._cfg.get('groups', DEFAULT_GROUPS)])
        self._prev_col  = ObservableCollection[object]([])
        self._last_recs = []
        self._last_unr  = []
        self._dry_done  = False
        self._scope_cbs = {}   # group_name -> CheckBox

        self._win = XamlReader.Load(XmlReader.Create(StringReader(XAML)))

        def _f(n):
            return self._win.FindName(n)

        self._log_box    = _f('LogBox')
        self._log_fbox   = _f('LogFolderBox')
        self._browse_btn = _f('BrowseBtn')
        self._open_btn   = _f('OpenFolderBtn')
        self._filter_box = _f('SheetFilterBox')
        self._match_lbl  = _f('MatchCountLabel')
        self._t2_box     = _f('T2ParamBox')
        self._proj_lbl   = _f('ProjectLabel')
        self._subtitle   = _f('SubtitleBlock')
        self._grp_grid   = _f('GroupsGrid')
        self._add_btn    = _f('AddGroupBtn')
        self._rem_btn    = _f('RemGroupBtn')
        self._rst_btn    = _f('ResetGroupsBtn')
        self._scope_pan  = _f('ScopePanel')
        self._all_btn    = _f('SelectAllBtn')
        self._none_btn   = _f('SelectNoneBtn')
        self._dry_btn       = _f('DryRunBtn')
        self._cmt_btn       = _f('CommitBtn')
        self._res_btn       = _f('RestoreBtn')
        self._prefix_chk    = _f('PrefixChk')
        self._prefix_box    = _f('PrefixBox')
        self._strip_btn     = _f('StripPrefixBtn')
        self._prev_grid     = _f('PreviewGrid')
        self._prev_hdr      = _f('PreviewHeader')
        self._prev_sum      = _f('PreviewSummary')
        self._outer_sc      = _f('OuterScroll')

        # Initial state
        self._subtitle.Text  = 'Project: {}  |  v{}'.format(doc.Title[:52], VERSION)
        self._proj_lbl.Text  = 'Config key: {}'.format(project_key())
        self._log_fbox.Text  = self._cfg.get('log_folder', '')
        self._filter_box.Text = self._cfg.get('sheet_filter', RLS_FILTER)
        # Populate T2 ComboBox with all string parameters found in the project
        _saved_t2 = self._cfg.get('t2_param', T2_PARAM)
        try:
            for _pname in get_sheet_string_params():
                self._t2_box.Items.Add(_pname)
        except Exception:
            pass
        self._t2_box.Text = _saved_t2
        self._grp_grid.ItemsSource  = self._grp_col
        self._prev_grid.ItemsSource = self._prev_col
        self._rebuild_scope()
        self._refresh_match_count()

        # Wire events
        self._browse_btn.Click          += self._on_browse
        self._open_btn.Click            += self._on_open_folder
        self._filter_box.LostFocus      += self._on_filter_changed
        self._add_btn.Click         += self._on_add_grp
        self._rem_btn.Click         += self._on_rem_grp
        self._rst_btn.Click         += self._on_reset_grps
        self._all_btn.Click         += self._on_sel_all
        self._none_btn.Click        += self._on_sel_none
        self._dry_btn.Click              += self._on_dry_run
        self._cmt_btn.Click              += self._on_commit
        self._res_btn.Click              += self._on_restore
        self._prefix_chk.Checked         += self._on_prefix_toggle
        self._prefix_chk.Unchecked       += self._on_prefix_toggle
        self._strip_btn.Click            += self._on_strip_prefix
        self._outer_sc.PreviewMouseWheel += self._on_scroll

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _log(self, msg):
        self._log_box.Text += msg + '\n'
        self._log_box.ScrollToEnd()

    def _clear_log(self):
        self._log_box.Text = ''

    def _scope_names(self):
        """Return set of checked group names (empty set = nothing selected)."""
        return {name for name, cb in self._scope_cbs.items()
                if cb.IsChecked is True or cb.IsChecked == True}  # noqa

    def _current_groups(self):
        """Commit any pending DataGrid edit and return valid group dicts."""
        try:
            self._grp_grid.CommitEdit()
        except Exception:
            pass
        groups = []
        for item in self._grp_col:
            d = item.to_dict()
            if d:
                groups.append(d)
        return groups

    def _rebuild_scope(self):
        """Rebuild the scope CheckBox panel from current group rows."""
        self._scope_pan.Children.Clear()
        self._scope_cbs = {}
        fg_white = SWM.SolidColorBrush(SWM.Color.FromArgb(255, 224, 224, 224))
        fg_teal  = SWM.SolidColorBrush(SWM.Color.FromArgb(255,  77, 182, 172))
        for row in self._grp_col:
            # Label: "RLS Plans — Block N-1 First Floor"  +  teal "(6601 – 6699)"
            sp = SWC.StackPanel()
            sp.Orientation = SWC.Orientation.Horizontal

            lbl = SWC.TextBlock()
            lbl.Text       = u'RLS Plans — ' + row.Name.title() + ' Floor'
            lbl.Foreground = fg_white

            rng = SWC.TextBlock()
            rng.Text       = u'   {} – {}'.format(row.Start, row.End)
            rng.Foreground = fg_teal
            rng.FontSize   = 10.0

            sp.Children.Add(lbl)
            sp.Children.Add(rng)

            cb = SWC.CheckBox()
            cb.Content   = sp
            cb.IsChecked = False   # default: all groups unselected
            cb.Margin    = SW.Thickness(0, 3, 0, 3)
            self._scope_pan.Children.Add(cb)
            self._scope_cbs[row.Name] = cb

    def _persist_cfg(self):
        groups = self._current_groups()
        if groups:
            self._cfg['groups']        = groups
            self._cfg['log_folder']    = self._log_fbox.Text.strip()
            self._cfg['sheet_filter']  = self._filter_box.Text.strip()
            self._cfg['t2_param']      = self._t2_box.Text.strip()
            save_cfg(self._cfg)

    def _require_log_folder(self):
        folder = self._log_fbox.Text.strip()
        if not folder or not os.path.isdir(folder):
            dark_msgbox('Log Folder Required',
                        'Please set a valid log folder before continuing.')
            return None
        return folder

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_scroll(self, sender, e):
        # Walk up from the event's originating element.
        # If we reach an inner DataGrid or ScrollViewer before the outer
        # ScrollViewer, bail out so those controls scroll themselves.
        el = e.OriginalSource
        while el is not None:
            if el is self._outer_sc:
                break
            if isinstance(el, SWC.DataGrid) or isinstance(el, SWC.ScrollViewer):
                return   # let inner control handle the wheel
            try:
                el = SWM.VisualTreeHelper.GetParent(el)
            except Exception:
                break
        self._outer_sc.ScrollToVerticalOffset(
            self._outer_sc.VerticalOffset - e.Delta / 3.0)
        e.Handled = True

    def _on_browse(self, sender, e):
        dlg = WinForms.FolderBrowserDialog()
        dlg.Description = 'Select folder for logs and backups'
        cur = self._log_fbox.Text.strip()
        if cur and os.path.isdir(cur):
            dlg.SelectedPath = cur
        if dlg.ShowDialog() == WinForms.DialogResult.OK:
            self._log_fbox.Text = dlg.SelectedPath

    def _on_open_folder(self, sender, e):
        folder = self._log_fbox.Text.strip()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            dark_msgbox('Open Folder', 'No valid log folder set.')

    def _refresh_match_count(self):
        flt = self._filter_box.Text.strip()
        try:
            count = len(get_rls_sheets(flt))
            self._match_lbl.Text = '{} sheet(s) matched'.format(count)
        except Exception:
            self._match_lbl.Text = '(error counting)'

    def _on_filter_changed(self, sender, e):
        self._refresh_match_count()

    def _on_add_grp(self, sender, e):
        self._grp_col.Add(GroupRow('NEW GROUP', 7001, 7099))
        self._rebuild_scope()

    def _on_rem_grp(self, sender, e):
        sel = self._grp_grid.SelectedItem
        if sel and sel in self._grp_col:
            self._grp_col.Remove(sel)
            self._rebuild_scope()

    def _on_reset_grps(self, sender, e):
        if dark_confirm('Confirm Reset',
                        'Reset to default ranges?\nCustom groups will be lost.'):
            self._grp_col.Clear()
            for g in DEFAULT_GROUPS:
                self._grp_col.Add(GroupRow(g['name'], g['start'], g['end']))
            self._rebuild_scope()
            self._log('[INFO] Ranges reset to defaults.')

    def _on_sel_all(self, sender, e):
        for cb in self._scope_cbs.values():
            cb.IsChecked = True
        self._clear_preview()

    def _on_sel_none(self, sender, e):
        for cb in self._scope_cbs.values():
            cb.IsChecked = False
        self._clear_preview()

    def _clear_preview(self):
        """Discard the current dry-run preview — scope or settings have changed."""
        self._dry_done = False
        self._prev_col.Clear()
        self._prev_hdr.Text = u'PREVIEW  —  run Dry Run first'
        self._prev_sum.Text = u''

    def _on_prefix_toggle(self, sender, e):
        """Enable/disable the prefix TextBox when the checkbox is toggled."""
        self._prefix_box.IsEnabled = bool(self._prefix_chk.IsChecked)
        self._clear_preview()

    def _on_strip_prefix(self, sender, e):
        """Remove staging prefix from all matching RLS sheets."""
        pfx = self._prefix_box.Text.strip()
        if not pfx:
            dark_msgbox('Strip Prefix', 'Enter a prefix in the prefix box first.')
            return

        # Preview what will change (dry run of the strip)
        strip_re = re.compile(
            r'^(.*?)' + re.escape(pfx) + r'(\d{4,})-(\d+)$')
        matches = []
        for sh in get_rls_sheets(self._filter_box.Text.strip()):
            num = sh.get_Parameter(P_NUM).AsString() or ''
            m   = strip_re.match(num)
            if m:
                matches.append((num, '{}{}-{}'.format(m.group(1), m.group(2), m.group(3))))

        if not matches:
            dark_msgbox('Strip Prefix',
                        u'No RLS sheets found whose number contains the prefix “{}” '
                        'before the base digits.'.format(pfx))
            return

        sample = '\n'.join(u'  {} → {}'.format(a, b) for a, b in matches[:6])
        if len(matches) > 6:
            sample += '\n  … and {} more'.format(len(matches) - 6)

        if not dark_confirm('Strip Prefix',
                            u'Remove prefix “{}” from {} sheet(s)?\n\n{}'.format(
                                pfx, len(matches), sample)):
            return

        self._clear_log()
        self._log('─' * 56)
        self._log('STRIP PREFIX  [{}]'.format(pfx))
        self._log('─' * 56)

        try:
            count, errors, _ = do_strip_prefix(
                pfx, flt=self._filter_box.Text.strip())
        except Exception as ex:
            self._log('[ERR] {}'.format(str(ex)))
            return

        for err in errors:
            self._log('[WARN] ' + err)

        self._log('[OK] Done — prefix stripped from {} sheet(s).'.format(count))
        self._log('[INFO] Run Dry Run again to verify the final numbers.')
        self._clear_preview()

    def _on_dry_run(self, sender, e):
        self._clear_log()
        groups = self._current_groups()
        if not groups:
            self._log('[ERR] No valid groups configured.')
            return

        scope = self._scope_names()
        self._log('─' * 56)
        self._log('DRY RUN')
        self._log('─' * 56)
        self._log('Scope: {}'.format(
            ', '.join(sorted(scope)) if scope else '(all groups)'))

        pfx = self._prefix_box.Text.strip() if self._prefix_chk.IsChecked else ''
        if pfx:
            self._log('Staging prefix: [{}]'.format(pfx))
        try:
            recs, unrec = build_plan(groups, scope if scope else None,
                                     flt=self._filter_box.Text.strip(),
                                     t2_param=self._t2_box.Text.strip(),
                                     staging_prefix=pfx)
        except Exception as ex:
            self._log('[ERR] {}'.format(str(ex)))
            return

        self._last_recs = recs
        self._last_unr  = unrec
        self._dry_done  = True

        # Populate preview
        self._prev_col.Clear()
        for r in sorted(recs,
                        key=lambda x: (x.group_name,
                                       room_sort_key(x.room_num),
                                       x.suffix)):
            self._prev_col.Add(PreviewRow(
                r.group_name, r.room_num,
                r.sheet_name, r.cur_num,
                r.new_num, r.status,
                element_id=r.element_id))

        changed   = sum(1 for r in recs if r.status == 'CHANGED')
        unchanged = sum(1 for r in recs if r.status == 'UNCHANGED')
        oor       = sum(1 for r in recs if r.status == 'OUT OF RANGE')
        dupes     = sum(1 for r in recs if r.status == 'DUPLICATE')

        self._log('[OK]   Sheets found    : {}'.format(len(recs)))
        self._log('[OK]   Will change     : {}'.format(changed))
        self._log('[INFO] Already correct : {}'.format(unchanged))
        if oor:
            self._log('[WARN] Out of range    : {}  (range too small)'.format(oor))
        if dupes:
            self._log('[WARN] Duplicate target: {}  (two+ sheets share the same proposed number — adjust ranges)'.format(dupes))
        if unrec:
            self._log('[WARN] Unrecognized    : {}'.format(len(unrec)))
            for msg in unrec:
                self._log('         ' + msg)

        self._log('')
        self._log('[OK] Dry run complete.')
        if dupes:
            self._log('[WARN] Fix duplicate targets (shown in amber) before committing.')
        elif changed:
            self._log('[OK] Review preview above, then click  ✔ Renumber Sheets  to commit.')

        self._prev_hdr.Text = (
            'PREVIEW  —  {} sheet(s) will be renumbered'.format(changed)
            + ('   ⚠ {} DUPLICATE target(s)'.format(dupes) if dupes else ''))
        self._prev_sum.Text = (
            'Changed: {}    Unchanged: {}    Out of range: {}    Unrecognized: {}{}'.format(
                changed, unchanged, oor, len(unrec),
                '    Duplicates: {}'.format(dupes) if dupes else ''))

        # Sync scope dict in case groups were edited
        existing_names = {row.Name for row in self._grp_col}
        for name in list(self._scope_cbs.keys()):
            if name not in existing_names:
                del self._scope_cbs[name]

    def _on_commit(self, sender, e):
        if not self._dry_done or not self._last_recs:
            dark_msgbox('Preview Required',
                        'Run Dry Run / Preview first to generate the renumber plan.')
            return

        # Collect element IDs ticked as Skip in the preview grid
        excluded_ids = set()
        for row in self._prev_col:
            if getattr(row, 'Excluded', False) and getattr(row, 'ElementId', None):
                excluded_ids.add(row.ElementId)

        # Mark excluded CHANGED records as SKIPPED so do_renumber leaves them alone
        for r in self._last_recs:
            if r.status in ('CHANGED', 'DUPLICATE') and r.element_id in excluded_ids:
                r.status = 'SKIPPED'

        to_change  = [r for r in self._last_recs if r.status == 'CHANGED']
        n_skipped  = len([r for r in self._last_recs if r.status == 'SKIPPED'])
        n_dupes    = len([r for r in self._last_recs if r.status == 'DUPLICATE'])

        if n_dupes:
            dark_msgbox(u'Duplicate Targets — Cannot Proceed',
                        u'{} sheet(s) have duplicate proposed numbers (shown in amber in the '
                        u'preview).\n\nRevit requires unique sheet numbers. Please adjust the '
                        u'group ranges so each sheet gets a unique number, then run Dry Run '
                        u'again.'.format(n_dupes))
            return

        if not to_change:
            msg = '[INFO] Nothing to renumber'
            if n_skipped:
                msg += u' — {} sheet(s) are excluded (un-tick Skip to include them)'.format(n_skipped)
            else:
                msg += ' — all sheets already match the proposed numbers'
            self._log(msg + '.')
            return

        folder = self._require_log_folder()
        if not folder:
            return

        # ── Pre-flight conflict check ─────────────────────────────────────────
        # Two kinds of collision that Revit will reject at commit time:
        #   1. External — proposed number already used by a non-batch sheet.
        #   2. Internal — two sheets in this batch assigned the SAME target number.
        # Both are surfaced here so the user can cancel rather than getting
        # Revit's own cryptic error dialog after the transaction.
        try:
            ext_conflicts, int_duplicates = check_conflicts(self._last_recs)
        except Exception:
            ext_conflicts, int_duplicates = [], []

        abort = False

        if int_duplicates:
            lines = []
            for num, names in int_duplicates[:6]:
                lines.append(u'  {} ← assigned to {} sheets: {}'.format(
                    num, len(names), u', '.join(n[:30] for n in names[:3])))
            if len(int_duplicates) > 6:
                lines.append(u'  … and {} more'.format(len(int_duplicates) - 6))
            dup_msg = (u'{} proposed number(s) are assigned to more than one sheet '
                       u'in this batch — Revit requires unique sheet numbers.\n\n'
                       u'{}\n\nThis usually means the group ranges overlap or '
                       u'several sheets share the same room/suffix combination. '
                       u'Please adjust the group ranges and run Dry Run again.'.format(
                           len(int_duplicates), u'\n'.join(lines)))
            dark_msgbox(u'Duplicate Targets — Cannot Proceed', dup_msg)
            abort = True

        if not abort and ext_conflicts:
            sample = '\n'.join(
                u'  {} ← used by: {}'.format(n, s[:40])
                for n, s in ext_conflicts[:8])
            if len(ext_conflicts) > 8:
                sample += u'\n  … and {} more'.format(len(ext_conflicts) - 8)
            warn_msg = (u'{} proposed number(s) are already used by other sheets '
                        u'in this project and cannot be assigned:\n\n{}\n\n'
                        u'These sheets will be skipped (restored to original). '
                        u'Continue with the rest?'.format(len(ext_conflicts), sample))
            if not dark_confirm(u'Number Conflicts Detected', warn_msg):
                abort = True

        if abort:
            return

        excl_note = u'\n({} sheet(s) skipped — excluded in preview)'.format(n_skipped) if n_skipped else ''
        confirm_msg = (u'Renumber {} sheet(s) in the Revit model?{}\n\n'
                       u'A backup CSV will be saved before any changes are made.'.format(
                           len(to_change), excl_note))
        if not dark_confirm('Confirm Renumber', confirm_msg):
            return

        self._clear_log()
        self._log('─' * 56)
        self._log('RENUMBER SHEETS')
        self._log('─' * 56)

        self._persist_cfg()

        # 1 — backup FIRST
        try:
            _bk_path, bk_name = save_backup(self._last_recs, folder)
            self._log('[OK] Backup saved  : {}'.format(bk_name))
        except Exception as ex:
            self._log('[ERR] Backup failed — aborting: {}'.format(str(ex)))
            return

        # 2 — apply changes
        try:
            count, errors = do_renumber(self._last_recs)
        except Exception as ex:
            self._log('[ERR] Transaction failed: {}'.format(str(ex)))
            return

        for err in errors:
            self._log('[WARN] ' + err)

        # 3 — write run log
        try:
            txt_p, csv_p = write_run_log(self._last_recs, self._last_unr, folder)
            self._log('[OK] Run log saved : {}'.format(os.path.basename(txt_p)))
            self._log('[OK] CSV saved     : {}'.format(os.path.basename(csv_p)))
        except Exception as ex:
            self._log('[WARN] Log write failed: {}'.format(str(ex)))

        self._log('')
        if n_skipped:
            self._log('[INFO] Skipped (excluded) : {}'.format(n_skipped))
        self._log('[OK] Done — {} sheet(s) renumbered.'.format(count))
        self._log('[INFO] Run Dry Run again to verify the updated numbers.')
        self._dry_done = False

        # 4 — success popup
        summary_lines = [u'✔  {} sheet(s) renumbered successfully.'.format(count)]
        if n_skipped:
            summary_lines.append(u'↷  {} sheet(s) skipped.'.format(n_skipped))
        if errors:
            summary_lines.append(u'⚠  {} warning(s) — see log for details.'.format(len(errors)))
        try:
            summary_lines.append(u'')
            summary_lines.append(u'Log saved to:')
            summary_lines.append(u'  {}'.format(os.path.basename(txt_p)))
        except Exception:
            pass
        dark_msgbox(u'Renumber Complete', u'\n'.join(summary_lines))

    def _on_restore(self, sender, e):
        folder = self._log_fbox.Text.strip()
        dlg = WinForms.OpenFileDialog()
        dlg.Title  = 'Select a backup CSV to restore from'
        dlg.Filter = 'Backup CSV (*BACKUP*.csv)|*BACKUP*.csv|All CSV (*.csv)|*.csv'
        if folder and os.path.isdir(folder):
            dlg.InitialDirectory = folder
        if dlg.ShowDialog() != WinForms.DialogResult.OK:
            return

        self._clear_log()
        self._log('─' * 56)
        self._log('RESTORE FROM BACKUP')
        self._log('─' * 56)
        self._log('File: {}'.format(os.path.basename(dlg.FileName)))

        restore_recs, err = parse_backup(dlg.FileName)
        if err:
            self._log('[ERR] ' + err)
            return

        will_change = [r for r in restore_recs if r['will_change']]
        not_found   = [r for r in restore_recs if r['sheet'] is None]
        no_change   = [r for r in restore_recs
                       if not r['will_change'] and r['sheet'] is not None]

        # Show restore preview
        self._prev_col.Clear()
        for r in restore_recs:
            if r['sheet'] is None:
                status = 'NOT FOUND'
            elif r['will_change']:
                status = 'RESTORE'
            else:
                status = 'NO CHANGE'
            self._prev_col.Add(PreviewRow(
                '', '', r['name'], r['current'], r['restore_to'], status))

        self._prev_hdr.Text = (
            'RESTORE PREVIEW  —  {} sheet(s) will be restored'.format(len(will_change)))
        self._prev_sum.Text = (
            'Will restore: {}    No change needed: {}    Not found (skipped): {}'.format(
                len(will_change), len(no_change), len(not_found)))

        self._log('[INFO] Will restore  : {}'.format(len(will_change)))
        self._log('[INFO] No change     : {}'.format(len(no_change)))
        if not_found:
            self._log('[WARN] Not found     : {} (skipped)'.format(len(not_found)))

        if not will_change:
            self._log('[INFO] Nothing to restore — all numbers already match the backup.')
            return

        if not dark_confirm('Confirm Restore',
                            'Restore {} sheet number(s) from backup?\n'
                            'This cannot be undone.'.format(len(will_change))):
            self._log('[INFO] Restore cancelled.')
            return

        log_folder = folder if folder and os.path.isdir(folder) else ''
        count, errors = do_restore(restore_recs, log_folder)

        for err_msg in errors:
            self._log('[WARN] ' + err_msg)

        self._log('[OK] Restored {} sheet(s).'.format(count))
        if log_folder:
            self._log('[OK] Restore log saved to log folder.')

    def show(self):
        self._win.ShowDialog()

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    RenumberDialog().show()
