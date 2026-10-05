# -*- coding: utf-8 -*-
"""
report.py  —  Model Health Checker
Export engine: CSV, Excel (.xlsx, hand-written), and JSON.

Public API (called from script.py)
-----------------------------------
  export_csv (results, score, counts, folder, base_name)  →  str (path)
  export_xlsx(results, score, counts, folder, base_name)  →  str (path)
  export_json(results, score, counts, folder, base_name)  →  str (path)

Each function returns the absolute path of the file written.
Raises on failure — callers in script.py already catch and surface errors.

CSV format
----------
One sheet — flat table, one row per flagged item:
  Category | Check | Status | Count | Element ID | Name | Detail | Issue

Excel format
------------
Sheet 1 — Summary    : score, counts, one row per check
Sheet 2 — All items  : same flat table as CSV
Sheet 3 — Trend      : score history from snapshots (if any)
Plain, unstyled workbook — no colours, no chart. See note below.

JSON format
-----------
Full results dict serialised, with metadata header.

Excel export notes
-------------------
This used to be built with openpyxl, but openpyxl's internal XML
serialisation machinery (Serialisable/Typed/ColorDescriptor descriptors)
turned out to be genuinely incompatible with IronPython 2712's descriptor
protocol ('ColorDescriptor' object has no attribute 'to_tree') — a deeper
problem than the earlier Python-2-vs-3 version-pinning issues with
openpyxl/et_xmlfile, and not something worth chasing further.

Excel export is now written by hand: a .xlsx is just a zip archive of a
few small XML parts, so this module builds those directly with `zipfile`
and `xml.sax.saxutils.escape` — both in the Python 2.7 standard library,
so no vendor/ folder or pip install is needed for Excel export anymore.
The trade-off is no cell colours/styling and no embedded trend line
chart — the Trend sheet still has the score-history data, just as a
plain table instead of a chart.
"""

import os
import csv
import json
import io
import datetime
import zipfile

try:
    from xml.sax.saxutils import escape as _xml_escape
except ImportError:
    def _xml_escape(s):
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# snapshots imported lazily inside _trend_rows to avoid circular issues
# (snapshots imports nothing from report)

# Category display order and labels
_CATEGORY_ORDER = ['bloat', 'integrity', 'coordination', 'performance', 'parameters']
_CATEGORY_LABELS = {
    'bloat':        'Model bloat',
    'integrity':    'Data integrity',
    'coordination': 'Coordination',
    'performance':  'Performance',
    'parameters':   'Parameters',
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _timestamp():
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def _flatten_items(results):
    """
    Yield flat rows from all check results, ordered by category then check.
    Each row: (category_label, check_label, status, total_count,
               elem_id, name, detail, issue_note)
    """
    # build ordered list of (key, result) by category
    import checks as chk
    check_meta = {c[0]: c for c in chk.CHECKS}
    ordered = []
    for cat in _CATEGORY_ORDER:
        for c in chk.CHECKS:
            key = c[0]
            if c[3] == cat and key in results:
                ordered.append((key, results[key]))

    for key, result in ordered:
        meta       = check_meta.get(key)
        cat_label  = _CATEGORY_LABELS.get(meta[3], meta[3]) if meta else ''
        chk_label  = result.get('label', key)
        status     = result.get('status', 'ERROR')
        total      = result.get('count', 0)
        items      = result.get('items', [])

        if not items:
            # emit one summary row even for OK checks
            yield (cat_label, chk_label, status, total,
                   '', '', '', result.get('message', ''))
            continue

        for item in items:
            elem_id = str(item.get('id', ''))
            name = (item.get('name') or item.get('view_name') or
                    item.get('description') or item.get('param_name') or
                    item.get('filename') or '')
            detail = (item.get('category') or item.get('level') or
                      item.get('view_type') or item.get('group_type') or
                      item.get('datatype') or item.get('link_type') or '')
            issue = (item.get('issue_type') or item.get('status') or
                     item.get('issue') or '')
            yield (cat_label, chk_label, status, total,
                   elem_id, name, detail, issue)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(results, score, counts, folder, base_name):
    """
    Write a flat CSV with one row per flagged item.
    Returns the path written.
    """
    path = os.path.join(folder, base_name + '.csv')

    rows = list(_flatten_items(results))

    # Opened via io.open with an explicit encoding — the plain built-in
    # open() defaults to ASCII on this platform, and check messages
    # routinely contain non-ASCII characters (em dashes, etc.), which
    # crashes Python 2's csv module with a UnicodeEncodeError. utf-8-sig
    # (UTF-8 + BOM) also makes Excel open the file correctly instead of
    # mangling anything past ASCII. newline='' stops io.open's own
    # newline translation from doubling up with csv's explicit '\r\n'.
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f, lineterminator='\r\n')

        # Metadata header block
        writer.writerow(['Model Health Checker — Export'])
        writer.writerow(['Generated', _timestamp()])
        writer.writerow(['Health score', score])
        writer.writerow(['OK',    counts.get('OK',    0)])
        writer.writerow(['WARN',  counts.get('WARN',  0)])
        writer.writerow(['FAIL',  counts.get('FAIL',  0)])
        writer.writerow(['INFO',  counts.get('INFO',  0)])
        writer.writerow([])

        # Column headers
        writer.writerow([
            'Category', 'Check', 'Status', 'Total count',
            'Element ID', 'Name / description', 'Detail', 'Issue note',
        ])

        for row in rows:
            writer.writerow(list(row))

    return path


# ---------------------------------------------------------------------------
# Excel export — hand-written .xlsx (zipfile + raw OOXML XML)
# ---------------------------------------------------------------------------
# No external dependencies: a .xlsx is a zip of small XML parts, built
# directly here with the standard-library zipfile and xml.sax.saxutils.
# Plain/unstyled on purpose — see the module docstring for why.

def _col_letter(idx):
    """1-based column index -> Excel column letter(s), e.g. 1 -> 'A', 28 -> 'AB'."""
    letters = ''
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _xml_cell(col_idx, row_idx, value):
    ref = '{0}{1}'.format(_col_letter(col_idx), row_idx)
    if value is None or value == '':
        return ''
    if isinstance(value, bool):
        return '<c r="{0}" t="inlineStr"><is><t>{1}</t></is></c>'.format(
            ref, 'TRUE' if value else 'FALSE')
    if isinstance(value, (int, float)):
        return '<c r="{0}"><v>{1}</v></c>'.format(ref, value)
    try:
        text = unicode(value)
    except NameError:
        text = str(value)
    except Exception:
        text = unicode(str(value), 'utf-8', 'replace')
    text = _xml_escape(text)
    return '<c r="{0}" t="inlineStr"><is><t xml:space="preserve">{1}</t></is></c>'.format(ref, text)


def _xml_row(row_idx, values):
    cells = ''.join(_xml_cell(i, row_idx, v) for i, v in enumerate(values, 1))
    return '<row r="{0}">{1}</row>'.format(row_idx, cells)


def _sheet_xml(rows):
    body = ''.join(_xml_row(i, row) for i, row in enumerate(rows, 1))
    return (
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        u'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        u'<sheetData>{0}</sheetData></worksheet>'
    ).format(body)


def _workbook_xml(sheet_names):
    items = []
    for i, name in enumerate(sheet_names, 1):
        items.append(u'<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'.format(
            _xml_escape(name), i))
    return (
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        u'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        u'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        u'<sheets>{0}</sheets></workbook>'
    ).format(u''.join(items))


def _workbook_rels_xml(sheet_count):
    items = []
    for i in range(1, sheet_count + 1):
        items.append(
            u'<Relationship Id="rId{0}" '
            u'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            u'Target="worksheets/sheet{0}.xml"/>'.format(i))
    items.append(
        u'<Relationship Id="rId{0}" '
        u'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        u'Target="styles.xml"/>'.format(sheet_count + 1))
    return (
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        u'{0}</Relationships>'
    ).format(u''.join(items))


def _content_types_xml(sheet_count):
    overrides = [
        u'<Override PartName="/xl/workbook.xml" '
        u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        u'<Override PartName="/xl/styles.xml" '
        u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    for i in range(1, sheet_count + 1):
        overrides.append(
            u'<Override PartName="/xl/worksheets/sheet{0}.xml" '
            u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i))
    return (
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        u'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        u'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        u'<Default Extension="xml" ContentType="application/xml"/>'
        u'{0}</Types>'
    ).format(u''.join(overrides))


_ROOT_RELS_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    u'<Relationship Id="rId1" '
    u'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    u'Target="xl/workbook.xml"/>'
    u'</Relationships>'
)

_STYLES_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    u'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    u'<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    u'<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    u'<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    u'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    u'<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    u'</styleSheet>'
)


def export_xlsx(results, score, counts, folder, base_name):
    """
    Write a plain, unstyled .xlsx workbook with Summary, All items, and
    Trend sheets — built by hand (zipfile + raw OOXML), not openpyxl.
    Returns the path written.
    """
    path = os.path.join(folder, base_name + '.xlsx')

    sheets = [
        (u'Summary', _summary_rows(results, score, counts)),
        (u'All items', _items_rows(results)),
    ]
    trend_rows = _trend_rows()
    if trend_rows:
        sheets.append((u'Trend', trend_rows))

    sheet_names = [name for name, _ in sheets]

    zf = zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED)
    try:
        zf.writestr('[Content_Types].xml', _content_types_xml(len(sheets)).encode('utf-8'))
        zf.writestr('_rels/.rels', _ROOT_RELS_XML.encode('utf-8'))
        zf.writestr('xl/workbook.xml', _workbook_xml(sheet_names).encode('utf-8'))
        zf.writestr('xl/_rels/workbook.xml.rels', _workbook_rels_xml(len(sheets)).encode('utf-8'))
        zf.writestr('xl/styles.xml', _STYLES_XML.encode('utf-8'))
        for i, (name, rows) in enumerate(sheets, 1):
            zf.writestr('xl/worksheets/sheet{0}.xml'.format(i), _sheet_xml(rows).encode('utf-8'))
    finally:
        zf.close()

    return path


def _summary_rows(results, score, counts):
    """Row data for the Summary sheet: score block + one row per check."""
    rows = []
    rows.append([u'Model Health Checker — Report'])
    rows.append([u'Generated', _timestamp()])
    rows.append([u'Health score', score])
    rows.append([u'OK checks', counts.get('OK', 0)])
    rows.append([u'WARN checks', counts.get('WARN', 0)])
    rows.append([u'FAIL checks', counts.get('FAIL', 0)])
    rows.append([])
    rows.append([u'Category', u'Check', u'Status', u'Count', u'Message'])

    import checks as chk
    prev_cat = None
    for cat in _CATEGORY_ORDER:
        cat_checks = [c for c in chk.CHECKS if c[3] == cat]
        for c in cat_checks:
            key = c[0]
            if key not in results:
                continue
            result  = results[key]
            status  = result.get('status', 'ERROR')
            count   = result.get('count', 0)
            message = result.get('message', '')
            label   = result.get('label', key)
            cat_label = _CATEGORY_LABELS.get(cat, cat)
            if cat != prev_cat:
                rows.append([cat_label.upper()])
                prev_cat = cat
            rows.append([cat_label, label, status, count, message])
    return rows


def _items_rows(results):
    """Row data for the All items sheet — same flat table as the CSV."""
    rows = [[u'Category', u'Check', u'Status', u'Total',
             u'Element ID', u'Name / description', u'Detail', u'Issue note']]
    for row in _flatten_items(results):
        rows.append(list(row))
    return rows


def _trend_rows():
    """Row data for the Trend sheet — score history, oldest first. [] if none."""
    try:
        import snapshots
        try:
            doc = __revit__.ActiveUIDocument.Document
        except Exception:
            return []
        history = snapshots.get_score_history(doc, limit=20)
        if not history:
            return []
        history_asc = list(reversed(history))
        rows = [[u'Date / time', u'Health score']]
        for ts, sc in history_asc:
            rows.append([ts, sc])
        return rows
    except Exception:
        return []


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_json(results, score, counts, folder, base_name):
    """
    Write the full results dict as JSON with a metadata header.
    Returns the path written.
    """
    path = os.path.join(folder, base_name + '.json')

    # Build a fully serialisable copy of results
    # (items lists may contain int ElementIds which are already Python ints)
    serialisable = {}
    for key, result in results.items():
        serialisable[key] = {
            'label':    result.get('label', key),
            'category': result.get('category', ''),
            'status':   result.get('status', 'ERROR'),
            'count':    result.get('count', 0),
            'message':  result.get('message', ''),
            'items':    _serialise_items(result.get('items', [])),
        }

    payload = {
        'meta': {
            'tool':      'Model Health Checker',
            'version':   '1.0.0',
            'generated': _timestamp(),
            'score':     score,
            'counts':    counts,
        },
        'results': serialisable,
    }

    with open(path, 'w') as f:
        json.dump(payload, f, indent=2, default=_json_default)

    return path


def _serialise_items(items):
    """Return a JSON-safe copy of an items list."""
    out = []
    for item in items:
        clean = {}
        for k, v in item.items():
            clean[k] = _json_default(v) if not _is_json_native(v) else v
        out.append(clean)
    return out


def _is_json_native(v):
    return isinstance(v, (bool, int, float, str, list, dict, type(None)))


def _json_default(obj):
    """Fallback JSON serialiser for non-native types."""
    try:
        return int(obj)
    except (TypeError, ValueError):
        pass
    try:
        return str(obj)
    except Exception:
        return None
