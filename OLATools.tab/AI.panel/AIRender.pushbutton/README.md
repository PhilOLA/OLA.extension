# AI Render (pyRevit pushbutton)

Sends the active 3D view to a Gemini image model with a prompt built through
a WPF dialog, then places a before/after comparison on a new sheet using a
title block you pick from a live dropdown. The whole flow — settings,
progress, and result preview — lives in one window.

## Install

1. Copy this `AIRender.pushbutton` folder (all files: `bundle.yaml`,
   `script.py`, `ui.xaml`, `logo.png`) into an existing panel, e.g.:
   `...\MyToolsExtension.extension\MyTools.tab\AI.panel\AIRender.pushbutton`
   (create the `AI.panel` folder if it doesn't exist — pyRevit picks up any
   `<name>.panel` folder automatically).
2. In pyRevit, click **Reload** (or restart Revit).

## Configure

1. Get an API key from Google AI Studio: https://aistudio.google.com/apikey
2. Enable billing on that key's project (image models have no free-tier
   quota — see https://ai.google.dev/gemini-api/docs/billing).
3. Set a Windows environment variable:
   - Name: `GEMINI_API_KEY`
   - Value: your key
   - **Fully close and relaunch Revit** afterward — env vars are only read
     once at process start, so a running Revit instance won't pick up a
     new or changed key.

No title block configuration needed — the dialog's dropdown is populated
live from whatever title block families/types are loaded in the current
project.

## Use

1. Open/activate a 3D view.
2. Click **AI Render**.
3. In the settings view:
   - Pick a **style** (photorealistic variants, sketch/illustration styles,
     and a few just-for-fun ones like sci-fi space station or cyberpunk).
   - Pick a **render quality** tier — Draft (fastest/cheapest, good for
     testing a prompt), Standard, or Final (Nano Banana Pro, best
     spatial/lighting reasoning, use once you're happy with a prompt).
   - Pick a **title block** for the comparison sheet.
   - Optionally pick from **Recent prompts** — your last 10 "extra
     direction" entries, most recent first, deduplicated. Selecting one
     fills the text box below with it (still editable afterward).
   - Type or edit **extra direction** (materials, mood, context).
   - Leave "Build a before/after comparison sheet" checked, or uncheck it
     for the drafting-view fallback instead.
   - **Horizontal offset (mm)** — nudge left/right if the sheet layout
     isn't centered for your specific title block (see "Sheet layout"
     below for how this is calculated).
4. Click **Render**. The same window switches to a progress view showing
   status as it exports, generates, and places the result — no separate
   popup.
5. When done, that same window switches again to a preview view: the
   rendered image is shown inline, with **Open Folder**, **New Render**
   (goes back to the settings view without closing the window), and
   **Close** buttons.

Result: a new sheet numbered `AI-001` (incrementing) named
`AI Render - <view name>`, with the source view on the left labeled
BEFORE, the AI result on the right labeled AFTER, and the prompt text
underneath.

Style, quality, title block, and extra-direction text are all remembered
between runs (and between Revit sessions) via pyRevit's own settings store.

If no title block is loaded in the project at all, the dropdown and
checkbox disable themselves automatically — you'll only get the drafting
view fallback until one's loaded.

## Why IronPython2, not CPython3

pyRevit's `pyrevit.forms` (which the WPF dialog depends on) only works on
the IronPython2 engine — it hard-fails on import under CPython3. So this
version uses IronPython2 throughout and calls the API with .NET's
`System.Net.WebClient` instead of the `requests` library; IronPython's
built-in `json` and `base64` modules cover the encoding side.

## Threading

Settings, progress, and result preview all live in one window (three
panels toggled by visibility) rather than separate popups. Generation
happens on a background `System.Threading.Thread` (the network call
only), with status updates marshaled back via `Dispatcher.BeginInvoke`.
Placing the result on a sheet is a Revit API call, so that step — along
with saving the file — happens back on the main/UI thread once the
network call finishes, not on the background thread itself.

## Known interop quirks handled in this script

- **`Element.Name` AttributeError**: under IronPython, plain `element.Name`
  access can raise `AttributeError: Name` for some element types (title
  block symbols hit this in testing). Worked around with a helper
  (`_element_name()`) that binds to the base `Element.Name` property
  descriptor explicitly.
- **`WebClient` POST hangs/drops on larger bodies**: some networks stall on
  the `Expect: 100-continue` handshake .NET sends before bigger POST
  payloads, or drop the connection mid-upload. Handled with
  `ServicePointManager.Expect100Continue = False`, `KeepAlive = False`, a
  5-minute timeout, and up to 3 retries via `_upload_with_retry()`.
- **`ImageExportOptions.GetFileName()`**: this is a *static* method —
  `GetFileName(doc, viewId)` — not an instance call.
- **Unicode escapes**: IronPython2 follows Python 2 rules, where `\uXXXX`
  escapes only work inside `u"..."`-prefixed strings — in a plain string
  literal they render as literal backslash-text instead of the character.
  All em dashes/arrows/etc. in this script are written as literal UTF-8
  characters in the source (the file is saved as UTF-8) rather than escape
  sequences, to sidestep this entirely.

## Dialog styling

The dialog keeps Revit's native title bar (for reliability inside pyRevit's
window hosting) but restyles everything below it: a white rounded "card"
with a soft shadow sits on a light grey background, section labels are
small-caps grey, and the dropdowns/text box use rounded corners with a
custom flat style. All of this lives in `ui.xaml` under `Window.Resources`
— tweak colors/corner radius there if you want a different look.

## Things you'll likely want to adjust

- **Sheet layout margins**: `SHEET_MARGIN_LEFT_MM` / `_RIGHT_MM` /
  `_TOP_MM` / `_BOTTOM_MM`, `IMAGE_GAP_MM`, `LABEL_GAP_MM`,
  `PROMPT_HEIGHT_MM`, `ROW_GAP_MM` near the top of `script.py` — all in
  millimeters (converted to feet internally, since the Revit API always
  works in feet regardless of project units). These were calibrated
  against one specific title block's real printable area; a different
  title block may need different values.
- **Sheet numbering**: auto-increments `AI-001`, `AI-002`, etc. — change
  the prefix in `next_sheet_number()` to match your project's convention.
- **Recent-prompts limit**: `MAX_RECENT_EXTRAS` near the top of
  `script.py` (currently 10).
- **The result is still a picture, not BIM geometry** — the AI restyles
  materials/lighting/atmosphere but can't hand back editable Revit
  elements.
- **Export resolution**: `PixelSize` in `export_view_to_image()` controls
  source image detail — currently 1000 for faster uploads; raise it for
  higher-quality final renders.
- **Quality tiers**: `QUALITY_PRESETS` near the top of `script.py` maps
  the dropdown labels to actual Gemini model IDs — update here if Google
  renames or retires a model.
- **Output folder**: `EXPORT_FOLDER` — currently `Documents\AIRender`
  (deliberately not Temp, which Windows/IT policy can clear between
  sessions).
