# -*- coding: utf-8 -*-
"""
Shared Parameter Extractor
pyRevit pushbutton script — IronPython 2.7

Extracts every shared parameter visible in the current Revit document
(from the ParameterBindings map) and writes them to a CSV file.

Output columns:
    Name | GUID | Group | Type | Categories | Binding | Instance/Type
"""

import os
import csv
import datetime
import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    ParameterElement,
    SharedParameterElement,
    BuiltInParameterGroup,
    StorageType,
    BindingMap,
    InstanceBinding,
    TypeBinding,
)
from pyrevit import revit, DB, forms, script

doc = revit.doc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STORAGE_TYPE_MAP = {
    StorageType.Double:      "Number / Length / Area / etc.",
    StorageType.Integer:     "Integer / Yes-No",
    StorageType.String:      "Text",
    StorageType.ElementId:   "Element",
    StorageType.None:        "(none)",
}

def get_group_name(param_group):
    """Return a readable name for a BuiltInParameterGroup."""
    try:
        return DB.LabelUtils.GetLabelFor(param_group)
    except Exception:
        return str(param_group)

def get_categories(binding):
    """Return a sorted, semicolon-separated list of category names from a binding."""
    cats = []
    try:
        for cat in binding.Categories:
            cats.append(cat.Name)
    except Exception:
        pass
    return "; ".join(sorted(cats))

# ---------------------------------------------------------------------------
# Extract shared parameters from ParameterBindings
# ---------------------------------------------------------------------------

rows = []

binding_map = doc.ParameterBindings
it = binding_map.ForwardIterator()
it.Reset()

while it.MoveNext():
    definition = it.Key
    binding    = it.Current

    # Only shared parameters have a GUID
    try:
        guid = definition.GUID.ToString()
    except Exception:
        continue   # built-in or project param — skip

    name         = definition.Name
    param_group  = definition.ParameterGroup
    group_name   = get_group_name(param_group)
    storage      = STORAGE_TYPE_MAP.get(definition.ParameterType, str(definition.ParameterType)) \
                   if hasattr(definition, "ParameterType") else ""

    # Revit 2024+ uses SpecTypeId instead of ParameterType
    # Try both gracefully
    if not storage:
        try:
            storage = str(definition.GetDataType())
        except Exception:
            storage = ""

    binding_type = "Instance" if isinstance(binding, InstanceBinding) else "Type"
    categories   = get_categories(binding)

    rows.append({
        "Name":         name,
        "GUID":         guid,
        "Group":        group_name,
        "Storage Type": storage,
        "Binding":      binding_type,
        "Categories":   categories,
    })

# Sort by Group then Name
rows.sort(key=lambda r: (r["Group"], r["Name"]))

# ---------------------------------------------------------------------------
# Also sweep SharedParameterElements to catch any not in ParameterBindings
# (e.g. parameters added to the file but not yet bound to any category)
# ---------------------------------------------------------------------------

bound_guids = {r["GUID"] for r in rows}

for spe in FilteredElementCollector(doc).OfClass(SharedParameterElement):
    guid = spe.GuidValue.ToString()
    if guid in bound_guids:
        continue
    defn = spe.GetDefinition()
    name = spe.Name
    try:
        group_name = get_group_name(defn.ParameterGroup)
    except Exception:
        group_name = ""
    rows.append({
        "Name":         name,
        "GUID":         guid,
        "Group":        group_name,
        "Storage Type": "(unbound)",
        "Binding":      "(unbound)",
        "Categories":   "(not bound to any category)",
    })

# ---------------------------------------------------------------------------
# Pick output folder
# ---------------------------------------------------------------------------

if not rows:
    forms.alert(
        "No shared parameters found in this document.",
        title="Shared Parameter Extractor"
    )
    import sys; sys.exit()

output_folder = forms.pick_folder(title="Choose where to save the shared parameter CSV")
if not output_folder:
    import sys; sys.exit()
timestamp     = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
csv_path      = os.path.join(output_folder, "SharedParams_{}.csv".format(timestamp))

# ---------------------------------------------------------------------------
# Write CSV
# ---------------------------------------------------------------------------

fieldnames = ["Name", "GUID", "Group", "Storage Type", "Binding", "Categories"]

with open(csv_path, "wb") as f:          # IronPython2: open in binary mode
    f.write(u"\ufeff".encode("utf-8"))   # BOM so Excel opens it correctly
    writer = csv.DictWriter(
        f,
        fieldnames=fieldnames,
        lineterminator="\r\n"
    )
    writer.writeheader()
    for row in rows:
        encoded = {k: v.encode("utf-8") if isinstance(v, unicode) else str(v)
                   for k, v in row.items()}
        writer.writerow(encoded)

# ---------------------------------------------------------------------------
# Summary alert
# ---------------------------------------------------------------------------

forms.alert(
    "Extracted {} shared parameters.\n\nSaved to:\n{}".format(len(rows), csv_path),
    title="Shared Parameter Extractor",
    ok=True
)

# Open the folder in Explorer
import subprocess
subprocess.Popen('explorer "{}"'.format(output_folder), shell=True)

# ---------------------------------------------------------------------------
# Bonus: locate CPython3 for PDF merge (helps diagnose merge issues)
# ---------------------------------------------------------------------------
cpython_candidates = [
    # Confirmed install on this machine
    r"C:\Users\pmay\AppData\Local\Python\bin\python3.14.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Python", "bin", "python3.14.exe"),
    # pyRevit bundled / other standard locations
    os.path.join(os.environ.get("APPDATA", ""), "pyRevit", "bin", "cpython", "python.exe"),
    os.path.join(os.environ.get("APPDATA", ""), "pyRevit-Master", "bin", "cpython", "python.exe"),
    r"C:\Program Files\pyRevit\bin\cpython\python.exe",
    r"C:\Program Files (x86)\pyRevit\bin\cpython\python.exe",
    r"C:\Python310\python.exe",
    r"C:\Python311\python.exe",
    r"C:\Python312\python.exe",
    r"C:\Python314\python.exe",
]
found_python = [p for p in cpython_candidates if os.path.exists(p)]
if found_python:
    python_msg = "CPython3 found at:\n" + "\n".join(found_python)
else:
    python_msg = (
        "CPython3 NOT found at any standard location.\n\n"
        "To enable PDF merging, either:\n"
        "  1. Install Python 3 from python.org\n"
        "  2. Or locate python.exe on your machine and update\n"
        "     _find_cpython3() in script.py with the correct path.\n\n"
        "Tip: open a command prompt and type 'where python' to find it."
    )
forms.alert(python_msg, title="CPython3 Location Check")
