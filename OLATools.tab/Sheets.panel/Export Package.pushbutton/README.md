# Export Package Builder — pyRevit Pushbutton

A one-click Revit export tool for PDF and DWG batch output with custom naming,
print set selection, and run history. PDFs are generated through the
**Bullzip PDF Printer** by default (smaller, higher-quality files than
Revit's native export), with automatic fallback to Revit's native exporter
on any machine where Bullzip isn't installed — see "PDF Generation Engine"
below.

---

## Deployment

Place the folder as a pushbutton inside your pyRevit extension:

```
MyToolsExtension.extension/
  MyTools.tab/
    Sheets.panel/
      ExportPackage.pushbutton/
        script.py
        bundle.yaml
        README.md
```

After placing it, reload pyRevit (pyRevit tab → Reload).

---

## File Naming Convention

```
{Prefix} - {OLA Building Name}-{SheetNo} - {SheetName}{ Title2}{ Title3}{ - Rev X}
```

| Segment | Source |
|---|---|
| `Prefix` | Manual input (e.g. `2401-AR-`), pre-filled from Project Number + `-AR-` |
| `OLA Building Name` | Revit Shared Parameter on the sheet |
| `SheetNo` | Revit built-in Sheet Number |
| `SheetName` | Revit built-in Sheet Name |
| `Title2` | Revit parameter `OLA Sheet Title 2` (appended if not empty) |
| `Title3` | Revit parameter `OLA Sheet Title 3` (appended if not empty) |
| `Rev X` | Revit current revision (optional toggle; `REV –` if no revision) |

Example output:
```
2401-AR- - CLINIC-A-100 - Ground Floor Plan East Wing - Rev B.pdf
```

---

## Shared Parameters Required

The following Revit parameters must exist on sheets (they are read via
`LookupParameter()` — if absent on a sheet, that segment is simply omitted):

| Parameter Name | Type |
|---|---|
| `OLA Building Name` | Shared Parameter (text) |
| `OLA Sheet Title 2` | Shared or Project Parameter (text) |
| `OLA Sheet Title 3` | Shared or Project Parameter (text) |

---

## PDF Generation Engine — Bullzip (default) vs Native

The **General** tab has a "PDF ENGINE" choice under PDF OUTPUT FOLDER:

- **Bullzip (recommended, default when installed)** — each sheet is printed
  through the **Bullzip PDF Printer** virtual printer via Revit's
  `PrintManager` API, instead of Revit's native PDF export. Bullzip produces
  noticeably smaller files and better print quality than Revit's native
  exporter. This is now the default output method.
- **Native (Revit Export)** — Revit's built-in `doc.Export()` /
  `PDFExportOptions` API. Always available, no external dependency.

**Per-machine auto-detection.** Every time the tool opens, it checks the
current PC's installed Windows printers for one whose name contains
"Bullzip" (`System.Drawing.Printing.PrinterSettings.InstalledPrinters`).

- If found, that printer's exact name is used and "Bullzip" is offered
  (and selected by default unless you last chose Native yourself).
- If **not** found — e.g. a colleague opens this tool on a machine that
  doesn't have Bullzip PDF Printer installed — the Bullzip option is
  greyed out, a warning is shown in the panel, and the tool automatically
  uses Native export for that user. Nobody has to configure anything for
  the tool to keep working; they just don't get the Bullzip quality/size
  benefit until it's installed on their PC.
- The engine choice is saved to `export_config.json` (which is per-machine
  by default — see "Config and History" below), but is always re-validated
  against what's actually installed before being applied.

### Installing Bullzip PDF Printer

Download and install the free **Bullzip PDF Printer** from
[bullzip.com/pdf-printer](https://www.bullzip.com/products/pdf/info.php)
on each machine that should use it. It registers itself as a normal
Windows printer (default name **"Bullzip PDF Printer"**).

### How the silent printing actually works

Revit's own `PrintManager.PrintToFileName` only tells **Revit** where to
route output — it has no effect on Bullzip's own UI. Bullzip has to be told
its output path and told to suppress its dialogs *through Bullzip's own
settings*, or its "Create File" window pops up and blocks the export
waiting for someone to click Save.

Before printing each sheet, the tool sets these keys — `Output` (the exact
file path), `ShowSaveAS=never`, `ShowSettings=never`, `ShowPDF=no`,
`ShowProgress=no`, `ShowProgressFinished=no`, `ConfirmOverwrite=yes` — via
two methods, in order:

1. **Bullzip's COM automation object** (`Bullzip.PDFPrinterSettings`,
   `SetValue(key, value)` / `WriteSettings(True)`) — Bullzip's officially
   documented automation interface. Not registered on every
   install/edition (the free edition, in particular, may not register it —
   the log will say `COM automation not available` if so).
2. **Falls back to writing the same keys directly into Bullzip's own
   settings file**, `%APPDATA%\PDF Writer\Bullzip PDF Printer\settings.ini`
   under `[PDF Printer]` — the same file the COM object and the Options
   dialog both read/write, so the effect is identical. Only those specific
   keys are touched; every other line/setting in the file is preserved.

On the Revit side, printing one sheet to an exact filename against a
virtual printer needs a specific combination the Revit API is picky
about: `PrintRange.Select` with a one-sheet `ViewSet` saved as a named
print set (`Export Package Builder - Temp` — recreated fresh each sheet,
so it doesn't accumulate), `CombinedFile = True`, and `PrintToFile = True`
with `PrintToFileName` set. (`PrintToFile` **cannot** be `False` for a
virtual printer, and `CombinedFile` **cannot** be touched at all when
printing the "current" view directly via `SubmitPrint(view)` — both throw;
this only works via the Select/ViewSet/CombinedFile=True path.)

**If it still doesn't work** — e.g. Bullzip's own dialog still appears —
open **Bullzip PDF Printer → Properties → Options** (or the tray icon →
Settings) once on that machine and manually confirm "Show Save As dialog",
"Show progress dialog"/"Show progress finished dialog", and "Show PDF after
creation" are off, and "Overwrite existing file without confirmation" is
on — this covers any Bullzip version/edition whose settings.ini uses a
different section name than `[PDF Printer]`.

If Bullzip is still waiting on a dialog Revit can't see, a batch export can
appear to "hang" — the tool times out after 45 seconds per sheet and logs
it as an error rather than freezing indefinitely.

### Known things to verify in your environment

This integration talks to two APIs (Bullzip's COM automation object and
Revit's `PrintManager`) that aren't as tightly documented as Revit's native
export API, so a couple of details are worth confirming after installing:

- **Paper size names.** The tool reads the currently-selected driver's
  sizes from `PrintManager.PaperSizes` (not `PrintSetup`, and not via a
  `FilteredElementCollector` — that path hands back pyRevit-proxy-wrapped
  objects Revit then rejects) and looks for one named "A3", "ISO A3",
  etc., falling back to whatever size is first in the list (logged) if
  none match — check the log/output after a test run to confirm the
  page size came out correctly.
- **Printer name.** If Bullzip is ever reinstalled or renamed (e.g.
  "Bullzip PDF Printer (Copy 1)"), detection still works (it matches any
  installed printer whose name *contains* "bullzip"), but if two Bullzip
  instances are installed the first match found is used.

---

## PDF Merge — How It Works

Revit's native PDF export API can only write one PDF per sheet.
To merge them into a single package PDF, the tool uses a two-stage approach:

### Stage 1 — pypdf2 via CPython3 helper

pyRevit runs on IronPython 2.7, which cannot import `pypdf2` directly.
The script writes a small helper file (`_merge_helper.py`) next to itself and
calls it using a separate **CPython 3** interpreter via `subprocess`.

The helper script tries:
1. `from pypdf import PdfMerger`   ← newer `pypdf` package
2. `from PyPDF2 import PdfMerger`  ← older `PyPDF2` fallback

pyRevit ships a bundled CPython 3 at:
```
%AppData%\pyRevit\bin\cpython\python.exe
```
The script searches several common locations. If found and the merge succeeds,
a `_MERGED.pdf` file is written to the PDF output folder.

### Stage 2 — Bullzip PDF Printer fallback

If the CPython3 approach fails (interpreter not found, pypdf not installed),
the script falls back to **Bullzip's `pdfutil.exe`** command-line tool:

```
pdfutil.exe /Merge sheet1.pdf sheet2.pdf ... /Output merged.pdf
```

Bullzip is searched at:
```
C:\Program Files\Bullzip\PDF Printer\pdfutil.exe
C:\Program Files (x86)\Bullzip\PDF Printer\pdfutil.exe
```

If both methods fail, the individual PDFs are retained and a note is written
to the log. No sheets are lost.

---

## DWG Export and Renaming

Revit's `doc.Export()` API exports all selected sheets in a single call
but names the output files using its own convention:
```
SheetNumber - SheetName.dwg
```
Immediately after export, the script renames every file to match the
project naming convention. This is the only way to control DWG output names
when using the native API.

DWG export setups (line weights, layers, units, etc.) are pulled directly
from setups already saved in the Revit document. Select from the dropdown
in the DWG Settings tab.

---

## Config and History

Run history and previous inputs are saved to:
```
ExportPackage.pushbutton/export_config.json
```

This file stores:
- Up to 20 previous prefixes
- Up to 20 previous PDF output folders
- Up to 20 previous DWG output folders
- Up to 100 previous export runs (for the History tab)

To share history across a team, move this file to a network location and
update `CONFIG_PATH` in `script.py`.

---

## Log Files

A timestamped log file is written to the output folder(s) after each run:
```
ExportLog_20260901_143200.txt
```

The log records: settings used, each sheet exported (✓ success / ✗ error),
merge status, and total counts.

---

## Revit Version Compatibility

| Version | PDF API | DWG API | Status |
|---|---|---|---|
| Revit 2022 | Native ExportPDF ✓ | doc.Export ✓ | Supported |
| Revit 2023 | Native ExportPDF ✓ | doc.Export ✓ | Supported |
| Revit 2024 | Native ExportPDF ✓ | doc.Export ✓ | **Current** |
| Revit 2025 | Native ExportPDF ✓ | doc.Export ✓ | Supported |
| Revit 2026 | Native ExportPDF ✓ | doc.Export ✓ | Ready |

No virtual print driver required for PDF export on any of these versions.

---

## Workshared Models

The script reads and exports only — it makes no changes to the model,
borrows no elements, and does not trigger a sync. Safe for use in
workshared (central file) environments.

---

## Next Iteration Ideas

- Manual sheet reordering within a run (drag-and-drop)
- Per-sheet paper size support
- Overwrite prompt (currently: silently overwrites)
- Upload to BIM360 / ACC after export
- Watermark option
- Email package on completion
