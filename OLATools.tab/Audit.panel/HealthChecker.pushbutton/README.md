# Model Health Checker  v1.0.0

pyRevit pushbutton tool — IronPython 2 / Revit 2024–2026.

---

## Folder structure

Deploy the entire `HealthChecker.pushbutton/` folder into your extension:

```
MyToolsExtension.extension/
  MyTools.tab/
    Audit.panel/
      HealthChecker.pushbutton/
        script.py        ← main entry point + WPF window
        checks.py        ← all 28 check functions
        snapshots.py     ← JSON snapshot store and diff engine
        report.py        ← CSV / Excel / JSON export engine
        bundle.yaml      ← pyRevit button config
        README.md        ← this file
        icon.png         ← 96×96 button icon (add your own)
        snapshots/       ← auto-created on first snapshot save
        vendor/          ← place openpyxl here (see Excel setup below)
```

If `Audit.panel/` does not yet exist in your tab, create the folder and
add a `panel.yaml` containing:

```yaml
name: Audit
```

---

## Excel export setup (openpyxl)

The Excel export requires `openpyxl`, a pure-Python library that runs
under IronPython 2.

Install it into the `vendor/` subfolder inside the bundle:

```
pip install openpyxl --target "path\to\HealthChecker.pushbutton\vendor"
```

The tool loads `vendor/` automatically at runtime. If `openpyxl` is
absent, CSV and JSON export still work; only the Excel button shows an
error.

Tested with openpyxl 3.1.x.

---

## First run

1. Open a Revit project (workshared or local).
2. Click **Health Checker** in the Audit panel.
3. Select a preset from the dropdown (default: **Full audit**).
4. Click **Run health check**.
5. Click any check row in the left panel to see its detail and fix options.

---

## Presets

| Preset | Checks | Notes |
|---|---|---|
| Full audit | 25 | All non-opt-in checks |
| Issue check | 6 | Pre-issue QA: DWGs, links, sheet nos., view names, rooms, options |
| Quick bloat | 8 | File size checks only |
| Parameter audit | 3 | Opt-in; slow on large models |

To save a custom preset: select the checks you want from the left panel,
then choose **+ Save current as preset...** from the dropdown.

---

## Fix actions

| Action | Checks | Notes |
|---|---|---|
| Delete selected | Imported DWGs, raster images, orphan views, empty sheets | Runs inside a named transaction. Revit Undo (Ctrl+Z) reverses it immediately after closing the dialog. |
| Purge selected | Unused families | Same transaction pattern. |
| Ungroup selected | Groups with issues | Calls `Group.UngroupMembers()`. |
| Unbind selected | Unused parameters | Calls `doc.ParameterBindings.Remove()`. |
| Show in view | Most element-level checks | Calls `uidoc.ShowElements()` to zoom Revit to the element. |
| Open warnings dialog | Revit warnings | Posts the built-in Review Warnings command. |
| Manage Links | Link status | Posts the built-in Manage Links command. |

---

## Snapshots

Clicking **Save snapshot** writes a JSON record of the current run to
`snapshots/{ProjectTitle}_health.json` inside the bundle folder.

Up to 20 snapshots are retained per project. On subsequent runs, the
detail panel for each check shows a diff strip:

```
vs last run (2d ago)   +1 new   2 unchanged
```

To clear all snapshots for a project, delete the corresponding
`snapshots/*.json` file.

---

## Health score

The score (0–100) is calculated from weighted check results:

| Status | Weight contribution |
|---|---|
| OK | Full weight |
| INFO | Full weight (no penalty) |
| WARN | Half weight |
| FAIL | Zero weight |
| ERROR | Zero weight |

Higher-impact checks carry more weight (e.g. link status = 10,
Revit warnings = 10, duplicate sheet numbers = 9).

Score thresholds:

| Score | Colour | Meaning |
|---|---|---|
| ≥ 80 | Teal | Good |
| 50–79 | Amber | Needs attention |
| < 50 | Red | Urgent issues |

---

## Thresholds

Default thresholds (configurable via **Edit thresholds...** in the UI):

| Check | WARN above | FAIL above |
|---|---|---|
| Revit warnings | 20 | 100 |
| In-place families | — | 10 |
| Unplaced rooms | — | 5 |
| Untagged elements | 10 | 50 |
| Views not on sheets | 20 | — |
| Unnamed reference planes | 20 | — |
| Detail lines per view | 500 | — |
| Oversized family (faces) | 5000 | — |

Thresholds are persisted per machine via pyRevit config under the
`HealthChecker` section.

---

## Parameters category (opt-in)

The three parameter checks are excluded from **Full audit** by default
because `check_unused_parameters` can take 30–60 seconds on large models
(it iterates every element in every bound category).

Run them via the **Parameter audit** preset or include them in a custom
preset.

`check_unassociated_sp_params` reads the shared parameter `.txt` file
directly via Python file I/O using `doc.Application.SharedParametersFilename`.
It compares GUIDs in the file against `doc.ParameterBindings` and
`SharedParameterElement` collector. The SP file path must be set in
Revit's Application Options for this check to run.

---

## strftime on Windows

`snapshots._format_date` uses `%-d` (Linux) for day-without-leading-zero.
On Windows IronPython this silently falls back to the raw ISO string.
To fix, change `'%-d'` to `'%#d'` in `snapshots.py` line ~280.

---

## Known API constraints (Revit 2024 / IronPython 2)

| Constraint | Detail |
|---|---|
| `DisableTemporaryViewPropertiesMode` | Missing on `ViewSection` — use `SubTransaction.RollBack()` instead |
| `Style.Triggers` with `IsMouseOver` on `TextBox` | Crashes IronPython WPF — not used |
| `CharacterSpacing` | UWP only — not used |
| `System.Windows.MessageBox` | Used for all dialogs (not `forms.alert`) so focus returns to WPF owner window correctly |
| Dispatcher threading | `self.Dispatcher.Invoke(DispatcherPriority.Normal, Action(lambda: ...))` — do not pass lambdas with captured variables directly; use named functions or default-arg capture |

---

## File sizes (v1.0.0)

| File | Lines |
|---|---|
| `script.py` | 1,667 |
| `checks.py` | 1,680 |
| `snapshots.py` | 302 |
| `report.py` | 548 |
| `bundle.yaml` | 55 |
| `README.md` | — |

---

## Adding a new check

1. Write `check_my_thing(doc)` in `checks.py` following the standard
   return schema `{status, count, items, message}`.
2. Add one entry to the `CHECKS` list at the bottom of `checks.py`:
   ```python
   ('my_thing', check_my_thing, 'My thing label', 'bloat', 3, False),
   ```
3. If the check needs a specialist detail renderer, add a branch in
   `script.py → _render_detail()` and a `_render_my_thing()` method.
4. If items have a delete/purge action, add the key to the relevant
   dict in `_make_action_bar()`.

No changes needed to `snapshots.py` or `report.py` — both consume
`results` generically.

---

## Roadmap

- [ ] BEP alignment check (naming convention validation)
- [ ] Project info completeness check
- [ ] Email / Teams report dispatch
- [ ] Run-on-open lightweight mode (status bar badge)
- [ ] Revit 2026 compatibility pass
