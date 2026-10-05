# -*- coding: utf-8 -*-
"""
snapshots.py  —  Model Health Checker
Snapshot store and diff engine.

Snapshots are JSON files written to a folder next to the bundle script.
One file per project, named by the Revit document title (sanitised).
The file holds a list of run records, newest first, capped at MAX_SNAPSHOTS.

File schema
-----------
{
  "project":   "My Project Name",
  "snapshots": [
    {
      "timestamp": "2026-09-18T10:42:01",
      "score":     61,
      "counts":    {"OK": 7, "WARN": 13, "FAIL": 4, "INFO": 3, "ERROR": 0},
      "checks": {
        "imported_dwgs":   {"status": "FAIL", "count": 3},
        "unused_families": {"status": "WARN", "count": 12},
        ...
      }
    },
    ...
  ]
}

Public API used by script.py
-----------------------------
  save_snapshot(doc, results, score)  →  str (path written)
  get_diff_for_key(doc, key, current_count)  →  dict | None
      {"new": int, "fixed": int, "same": int, "prev_count": int}
      Returns None when no previous snapshot exists for this project.

Internal helpers
----------------
  _snapshot_path(doc)   →  str
  _load_store(path)     →  dict
  _save_store(path, store)
  _serialise_results(results, score)  →  dict  (one snapshot record)
"""

import os
import sys
import json
import datetime
import re

# Max number of snapshots retained per project.
MAX_SNAPSHOTS = 20

# Snapshot folder — sibling of this file (inside the bundle).
_BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
_SNAPSHOT_DIR = os.path.join(_BUNDLE_DIR, 'snapshots')


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _sanitise(name):
    """Strip characters that are unsafe in filenames."""
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.strip('. ')
    return name[:80] or 'unnamed_project'


def _snapshot_path(doc):
    """
    Return the path to the JSON snapshot file for this project.
    Keyed by doc.Title so it survives the file moving between machines.
    """
    if not os.path.exists(_SNAPSHOT_DIR):
        os.makedirs(_SNAPSHOT_DIR)
    fname = _sanitise(doc.Title) + '_health.json'
    return os.path.join(_SNAPSHOT_DIR, fname)


# ---------------------------------------------------------------------------
# Store I/O
# ---------------------------------------------------------------------------

def _load_store(path):
    """
    Load the snapshot store from disk.
    Returns a valid store dict even if the file is missing or corrupt.
    """
    if not os.path.exists(path):
        return {'project': '', 'snapshots': []}
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        # validate basic shape
        if not isinstance(data, dict):
            return {'project': '', 'snapshots': []}
        if 'snapshots' not in data or not isinstance(data['snapshots'], list):
            data['snapshots'] = []
        return data
    except Exception:
        return {'project': '', 'snapshots': []}


def _save_store(path, store):
    """Write the snapshot store to disk, pretty-printed."""
    try:
        with open(path, 'w') as f:
            json.dump(store, f, indent=2)
    except Exception as ex:
        raise IOError('Could not write snapshot file {}: {}'.format(path, str(ex)))


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _serialise_results(results, score):
    """
    Convert a results dict (from checks.run_checks) into a compact
    snapshot record — just status and count per check key.
    Full item lists are not stored (too large; use export for that).
    """
    checks_summary = {}
    counts = {'OK': 0, 'WARN': 0, 'FAIL': 0, 'INFO': 0, 'ERROR': 0}

    for key, r in results.items():
        status = r.get('status', 'ERROR')
        count  = r.get('count', 0)
        checks_summary[key] = {
            'status': status,
            'count':  count,
        }
        counts[status] = counts.get(status, 0) + 1

    return {
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'score':     score,
        'counts':    counts,
        'checks':    checks_summary,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_snapshot(doc, results, score):
    """
    Serialise results and append to this project's snapshot store.
    Trims to MAX_SNAPSHOTS (newest first).
    Returns the path of the file written.

    Raises IOError if the file cannot be written.
    """
    path  = _snapshot_path(doc)
    store = _load_store(path)
    store['project'] = doc.Title

    record = _serialise_results(results, score)

    # prepend (newest first)
    store['snapshots'].insert(0, record)

    # trim
    if len(store['snapshots']) > MAX_SNAPSHOTS:
        store['snapshots'] = store['snapshots'][:MAX_SNAPSHOTS]

    _save_store(path, store)
    return path


def get_diff_for_key(doc, key, current_count):
    """
    Compare current_count for a check key against the most recent snapshot.

    Returns a dict:
        {
            'new':        int,   # issues that appeared since last run
            'fixed':      int,   # issues that were resolved
            'same':       int,   # unchanged count
            'prev_count': int,   # count in the previous snapshot
            'prev_date':  str,   # timestamp of the previous snapshot
        }

    Returns None if:
        - No snapshot file exists for this project.
        - The previous snapshot does not contain this key.
        - The snapshot file is corrupt.
    """
    try:
        path  = _snapshot_path(doc)
        store = _load_store(path)
        snaps = store.get('snapshots', [])

        if not snaps:
            return None

        # most recent snapshot
        prev = snaps[0]
        prev_checks = prev.get('checks', {})

        if key not in prev_checks:
            return None

        prev_count = prev_checks[key].get('count', 0)
        prev_date  = prev.get('timestamp', '')

        new_issues = max(0, current_count - prev_count)
        fixed      = max(0, prev_count - current_count)
        same       = max(0, min(current_count, prev_count))

        # only return a diff strip when something actually changed
        # or when there is a meaningful previous count to show
        if prev_count == 0 and current_count == 0:
            return None

        return {
            'new':        new_issues,
            'fixed':      fixed,
            'same':       same,
            'prev_count': prev_count,
            'prev_date':  _format_date(prev_date),
        }

    except Exception:
        return None


def get_score_history(doc, limit=10):
    """
    Return a list of (timestamp, score) tuples, newest first, up to limit.
    Used by report.py for the trend section.
    Returns [] if no snapshots exist.
    """
    try:
        path  = _snapshot_path(doc)
        store = _load_store(path)
        snaps = store.get('snapshots', [])
        result = []
        for snap in snaps[:limit]:
            ts    = snap.get('timestamp', '')
            score = snap.get('score', 0)
            result.append((ts, score))
        return result
    except Exception:
        return []


def get_all_snapshots(doc):
    """
    Return the full list of snapshot records for this project.
    Each record: {timestamp, score, counts, checks}.
    Returns [] if none exist.
    """
    try:
        path  = _snapshot_path(doc)
        store = _load_store(path)
        return store.get('snapshots', [])
    except Exception:
        return []


def delete_snapshots(doc):
    """
    Delete all snapshots for this project.
    Returns True on success, False on failure.
    """
    try:
        path = _snapshot_path(doc)
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Internal date formatter
# ---------------------------------------------------------------------------

def _format_date(timestamp_str):
    """
    Convert an ISO timestamp to a short human-readable string.
    e.g. '2026-09-16T10:42:01'  →  '2d ago'  |  'today 10:42'  |  '16 Sep'
    """
    try:
        dt = datetime.datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S')
        now  = datetime.datetime.now()
        diff = now - dt

        if diff.days == 0:
            return 'today {}'.format(dt.strftime('%H:%M'))
        elif diff.days == 1:
            return 'yesterday'
        elif diff.days < 7:
            return '{}d ago'.format(diff.days)
        elif diff.days < 365:
            return dt.strftime('%-d %b')
        else:
            return dt.strftime('%-d %b %Y')
    except Exception:
        return timestamp_str
