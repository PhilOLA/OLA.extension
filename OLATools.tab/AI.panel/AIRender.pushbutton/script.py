# -*- coding: utf-8 -*-
"""
AI Render
---------
Exports the active 3D view to an image, sends it to an AI image-generation
API (default: Gemini 2.5 Flash Image, aka "Nano Banana") along with a
user-built prompt, then places BEFORE (source) and AFTER (AI result) images
side by side on a new sheet.

Runs on the IronPython2 engine (see bundle.yaml) because pyrevit.forms/WPF
does not work under pyRevit's CPython3 engine. The HTTP call uses .NET's
System.Net.WebClient instead of `requests`; IronPython's built-in json and
base64 modules handle encoding.

Setup:
  1. Get an API key from Google AI Studio (https://aistudio.google.com/apikey).
  2. Set it as a Windows environment variable named GEMINI_API_KEY, then
     restart Revit (env vars are read at process start).
  3. Activate a 3D view and click this button. Pick a title block from the
     dropdown in the dialog (populated from what's loaded in the project).
"""

import os
import json
import time
import base64
from datetime import datetime

import clr
clr.AddReference("System")
clr.AddReference("System.Windows.Forms")
from System.Net import WebClient, WebException, ServicePointManager
from System.IO import StreamReader
from System.Text import Encoding
clr.AddReference("PresentationCore")
from System.Windows import Visibility
from System.Windows.Media.Imaging import BitmapImage
from System import Uri, Action
from System.Threading import Thread, ThreadStart
from System.Diagnostics import Process

# Some networks/proxies stall on the "Expect: 100-continue" handshake .NET
# sends before larger POST bodies (like our base64 image). Disabling it is
# the standard fix for a WebClient POST that hangs only on bigger payloads.
ServicePointManager.Expect100Continue = False

from pyrevit import revit, DB, forms, script

logger = script.get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# (label, model id) pairs - shown as a "Render quality" dropdown in the
# dialog. Draft is cheapest/fastest for testing the pipeline; Final uses
# Nano Banana Pro for the best spatial/lighting reasoning once you're happy
# with a prompt and ready for a real deliverable.
QUALITY_PRESETS = [
    ("Draft (fastest, cheapest)", "gemini-3.1-flash-lite-image"),
    ("Standard", "gemini-3.1-flash-image"),
    ("Final (Pro quality, slower)", "gemini-3-pro-image"),
]
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
API_KEY_ENV_VAR = "GEMINI_API_KEY"

EXPORT_FOLDER = os.path.join(
    os.environ.get("USERPROFILE", os.getcwd()), "Documents", "AIRender"
)  # Documents, not Temp - Temp can be cleared by Windows/IT policy between sessions

STYLE_PRESETS = [
    "Photorealistic, bright daylight",
    "Photorealistic, early morning light",
    "Photorealistic, warm dusk / interior lighting",
    "Photorealistic, dusk with exterior/landscape lighting",
    "Concept sketch, loose linework",
    "Soft watercolor illustration",
    "Clean line-art / technical illustration",
    "Sci-fi space station",
    "Cyberpunk neon night",
    "Fantasy castle interior",
    "Underwater research station",
    "Retro-futuristic 1960s",
]


# ---------------------------------------------------------------------------
# WPF prompt window
# ---------------------------------------------------------------------------
MAX_RECENT_EXTRAS = 10
RECENT_PLACEHOLDER = "Select a recent prompt..."
RECENT_EMPTY_PLACEHOLDER = "No recent prompts yet"


def load_last_prompt():
    config = script.get_config()
    last_extra = getattr(config, "last_extra", "")
    last_style_index = getattr(config, "last_style_index", 0)
    if not isinstance(last_style_index, int) or not (0 <= last_style_index < len(STYLE_PRESETS)):
        last_style_index = 0
    last_titleblock = getattr(config, "last_titleblock", None)
    last_quality_index = getattr(config, "last_quality_index", 0)
    if not isinstance(last_quality_index, int) or not (0 <= last_quality_index < len(QUALITY_PRESETS)):
        last_quality_index = 0
    return last_extra, last_style_index, last_titleblock, 0.0, last_quality_index


def get_recent_extras():
    config = script.get_config()
    recent = getattr(config, "recent_extras", None)
    if not isinstance(recent, list):
        recent = []
    return recent


def push_recent_extra(recent_extras, new_extra):
    new_extra = (new_extra or "").strip()
    if not new_extra:
        return recent_extras
    updated = [new_extra] + [e for e in recent_extras if e != new_extra]
    return updated[:MAX_RECENT_EXTRAS]


def get_recent_edit_instructions():
    config = script.get_config()
    recent = getattr(config, "recent_edit_instructions", None)
    if not isinstance(recent, list):
        recent = []
    return recent


def save_recent_edit_instructions(recent):
    config = script.get_config()
    config.recent_edit_instructions = recent
    script.save_config()


def save_last_prompt(extra, style_index, titleblock_name, recent_extras, h_offset, quality_index):
    config = script.get_config()
    config.last_extra = extra
    config.last_style_index = style_index
    if titleblock_name:
        config.last_titleblock = titleblock_name
    config.recent_extras = recent_extras
    config.last_h_offset = h_offset
    config.last_quality_index = quality_index
    script.save_config()


def _ascii_safe(text):
    """Sanitize user-entered text to ASCII - IronPython 2 crashes when
    non-ASCII chars (smart quotes, em dashes etc. pasted from Word) get
    mixed with byte strings in the same json.dumps call."""
    if not text:
        return ""
    replacements = {
        u"\u2014": "-", u"\u2013": "-",
        u"\u2018": "'", u"\u2019": "'",
        u"\u201c": '"', u"\u201d": '"',
        u"\u2026": "...",
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    return text.encode("ascii", errors="ignore").decode("ascii")


class PromptForm(forms.WPFWindow):
    """Single window covering the whole flow: settings -> progress ->
    result preview, swapped via panel visibility rather than a separate
    completion dialog. The image-generation call runs on a background
    thread; placing the result on a sheet is a Revit API call, so that
    step is marshaled back to the main/UI thread once the network call
    finishes (Revit API calls aren't safe off-thread)."""

    def __init__(self, xaml_file, doc, view, api_key, view_name,
                 last_extra="", last_style_index=0,
                 titleblock_names=None, last_titleblock=None, recent_extras=None,
                 last_h_offset=0.0, last_quality_index=0):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self.view = view
        self.api_key = api_key
        self.view_name = view_name
        self.recent_extras = recent_extras or []
        self.result_image_path = None
        self._closed = False
        self.Closed += self.on_window_closed
        self.style_combo.ItemsSource = STYLE_PRESETS
        self.style_combo.SelectedIndex = last_style_index
        self.extra_box.Text = last_extra

        self.quality_combo.ItemsSource = [label for label, _ in QUALITY_PRESETS]
        self.quality_combo.SelectedIndex = last_quality_index

        if self.recent_extras:
            self.recent_combo.ItemsSource = [RECENT_PLACEHOLDER] + self.recent_extras
            self.recent_combo.IsEnabled = True
        else:
            self.recent_combo.ItemsSource = [RECENT_EMPTY_PLACEHOLDER]
            self.recent_combo.IsEnabled = False
        self.recent_combo.SelectedIndex = 0

        titleblock_names = titleblock_names or []
        self.titleblock_combo.ItemsSource = titleblock_names
        if titleblock_names:
            if last_titleblock in titleblock_names:
                self.titleblock_combo.SelectedItem = last_titleblock
            else:
                self.titleblock_combo.SelectedIndex = 0
        else:
            # No title blocks loaded in the project - disable both controls
            # rather than letting the user check a box that can't work.
            self.titleblock_combo.IsEnabled = False
            self.sheet_check.IsChecked = False
            self.sheet_check.IsEnabled = False

        try:
            logo_path = script.get_bundle_file("logo.png")
            self.logo_image.Source = BitmapImage(Uri(logo_path))
        except Exception:
            pass  # missing/unreadable logo shouldn't block the tool

    # -- settings panel handlers --------------------------------------
    def recent_combo_changed(self, sender, args):
        if self.recent_combo.SelectedIndex <= 0:
            return
        self.extra_box.Text = self.recent_combo.SelectedItem
        self.recent_combo.SelectedIndex = 0

    def render_click(self, sender, args):
        h_offset = 0.0

        style = self.style_combo.SelectedItem
        extra = self.extra_box.Text or ""
        build_sheet = bool(self.sheet_check.IsChecked)
        titleblock = self.titleblock_combo.SelectedItem
        quality_index = self.quality_combo.SelectedIndex

        if build_sheet and not titleblock:
            forms.alert(
                "No title block is loaded in this project.\n\n"
                "Load one (Insert tab → Load Family) or uncheck "
                "'Build a before/after comparison sheet', then run again.",
            )
            return

        updated_recent = push_recent_extra(self.recent_extras, extra)
        save_last_prompt(extra, STYLE_PRESETS.index(style), titleblock, updated_recent, h_offset, quality_index)
        self.recent_extras = updated_recent

        model_id = QUALITY_PRESETS[quality_index][1]
        self._last_quality_index = quality_index
        prompt = (
            "Render this architectural 3D view as a {style} image. "
            "Preserve the exact geometry, proportions, and camera framing shown "
            "in the source image, only change materials, lighting, and "
            "atmosphere. {extra}"
        ).format(style=style[0].lower() + style[1:], extra=_ascii_safe(extra))

        self.settings_panel.Visibility = Visibility.Collapsed
        self.progress_panel.Visibility = Visibility.Visible
        self.progress_status_text.Text = "Exporting the active 3D view..."

        # Revit API access must stay on the main thread - do this one step
        # synchronously before handing off to the background worker.
        try:
            source_image = export_view_to_image(self.doc, self.view)
        except Exception as ex:
            self._show_error("Couldn't export the view: {}".format(ex))
            return

        worker = ThreadStart(
            lambda: self._generate_worker(source_image, prompt, model_id, build_sheet, titleblock, h_offset)
        )
        thread = Thread(worker)
        thread.IsBackground = True
        thread.Start()

    def cancel_click(self, sender, args):
        self.Close()

    def on_window_closed(self, sender, args):
        self._closed = True

    # -- background worker (network call only - no Revit API here) ------
    def _generate_worker(self, source_image, prompt, model_id, build_sheet, titleblock, h_offset):
        try:
            self._set_status("Generating your AI render... ☕ grab a coffee, back in a moment")
            image_bytes = call_image_api(self.api_key, source_image, prompt, model_id)
            self.Dispatcher.BeginInvoke(Action(
                lambda: self._finish_on_ui_thread(image_bytes, source_image, prompt, build_sheet, titleblock, h_offset)
            ))
        except Exception as ex:
            self._show_error(str(ex))

    # -- finish-up on the main/UI thread (Revit API calls happen here) --
    def _finish_on_ui_thread(self, image_bytes, source_image, prompt, build_sheet, titleblock, h_offset):
        if self._closed:
            return
        try:
            self.progress_status_text.Text = "Saving image..."
            # Store state so accept_click and edit handlers can use it later
            self._last_source_image = source_image
            self._last_prompt = prompt
            self._last_build_sheet = build_sheet
            self._last_titleblock = titleblock
            self._last_h_offset = h_offset
            result_image = save_result_image(image_bytes, source_image)
            self.result_image_path = result_image
            self._show_review_ui(result_image)
        except Exception as ex:
            self._show_error_ui(str(ex))

    # -- thread-safe status/error updates --------------------------------
    def _set_status(self, text):
        try:
            self.Dispatcher.BeginInvoke(Action(lambda: self._set_status_ui(text)))
        except Exception:
            pass  # window likely closed - nothing to update

    def _set_status_ui(self, text):
        if self._closed:
            return
        self.progress_status_text.Text = text

    def _show_error(self, message):
        try:
            self.Dispatcher.BeginInvoke(Action(lambda: self._show_error_ui(message)))
        except Exception:
            pass

    def _show_error_ui(self, message):
        if self._closed:
            return
        self.progress_status_text.Text = "Something went wrong:\n{}".format(message)

    def _show_review_ui(self, result_image):
        if self._closed:
            return
        self.progress_panel.Visibility = Visibility.Collapsed
        self.review_panel.Visibility = Visibility.Visible
        self.review_status_text.Text = result_image
        self.edit_status_text.Text = ""
        self.edit_instruction_box.Text = (
            "Place this logo on the wall above the reception counter as a "
            "printed sign, maintaining the wall's perspective and lighting"
        )
        self.edit_progress_bar.Visibility = Visibility.Collapsed
        self._attached_logo_path = None
        self.attached_logo_text.Text = "No file attached"
        self.clear_logo_btn.Visibility = Visibility.Collapsed
        # Populate recent edit instructions dropdown
        recent_edits = get_recent_edit_instructions()
        if recent_edits:
            self.recent_edit_combo.ItemsSource = [RECENT_PLACEHOLDER] + recent_edits
            self.recent_edit_combo.IsEnabled = True
        else:
            self.recent_edit_combo.ItemsSource = [RECENT_EMPTY_PLACEHOLDER]
            self.recent_edit_combo.IsEnabled = False
        self.recent_edit_combo.SelectedIndex = 0
        try:
            self.result_image_view.Source = BitmapImage(Uri(result_image))
        except Exception as ex:
            self.review_status_text.Text = (
                "Couldn't preview the image ({}). File: {}"
            ).format(ex, result_image)

    # -- review panel handlers -----------------------------------------
    def open_folder_click(self, sender, args):
        if self.result_image_path:
            open_containing_folder(self.result_image_path)

    def rerender_click(self, sender, args):
        self.review_panel.Visibility = Visibility.Collapsed
        self.settings_panel.Visibility = Visibility.Visible

    def accept_click(self, sender, args):
        """Place the current result image on a sheet/drafting view."""
        if not self.result_image_path:
            return
        self.review_panel.Visibility = Visibility.Collapsed
        self.progress_panel.Visibility = Visibility.Visible
        self.progress_status_text.Text = "Placing on sheet..."
        try:
            build_sheet = self._last_build_sheet
            titleblock = self._last_titleblock
            h_offset = self._last_h_offset
            prompt = self._last_prompt

            if build_sheet:
                target = create_before_after_sheet(
                    self.doc, self._last_source_image, self.result_image_path,
                    self.view_name, prompt, titleblock, h_offset
                )
                target_desc = "sheet '{}'".format(target.SheetNumber)
            else:
                target = place_image_on_drafting_view(
                    self.doc, self.result_image_path,
                    "AI Render - {}".format(self.view_name)
                )
                target_desc = "drafting view '{}'".format(_element_name(target))

            self._show_placed_ui(target_desc)
        except Exception as ex:
            self._show_placed_ui(None, error=str(ex))

    def attach_logo_click(self, sender, args):
        from System.Windows.Forms import OpenFileDialog, DialogResult
        dlg = OpenFileDialog()
        dlg.Title = "Select logo or reference image"
        dlg.Filter = "Image files (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg|All files (*.*)|*.*"
        dlg.FilterIndex = 1
        if dlg.ShowDialog() == DialogResult.OK:
            self._attached_logo_path = dlg.FileName
            import os as _os
            self.attached_logo_text.Text = _os.path.basename(dlg.FileName)
            self.clear_logo_btn.Visibility = Visibility.Visible
            self.edit_status_text.Text = "Logo attached - will be included in your render as instructed."

    def clear_logo_click(self, sender, args):
        self._attached_logo_path = None
        self.attached_logo_text.Text = "No file attached"
        self.clear_logo_btn.Visibility = Visibility.Collapsed
        self.edit_status_text.Text = ""

    def recent_edit_combo_changed(self, sender, args):
        if self.recent_edit_combo.SelectedIndex <= 0:
            return
        self.edit_instruction_box.Text = self.recent_edit_combo.SelectedItem
        self.recent_edit_combo.SelectedIndex = 0

    def edit_click(self, sender, args):
        """Send the current rendered image back to Gemini with an edit instruction,
        optionally including an attached logo/reference image."""
        instruction = self.edit_instruction_box.Text.strip()
        if not instruction:
            self.edit_status_text.Text = "Please type an edit instruction first."
            return
        if not self.result_image_path:
            return
        self.edit_btn.IsEnabled = False
        self.accept_btn.IsEnabled = False
        self.rerender_btn.IsEnabled = False
        self.attach_logo_btn.IsEnabled = False
        self.edit_progress_bar.Visibility = Visibility.Visible
        logo_path = getattr(self, "_attached_logo_path", None)
        self.edit_status_text.Text = "Applying edit{}...".format(
            " with attached image" if logo_path else ""
        )
        image_to_edit = self.result_image_path
        model_id = QUALITY_PRESETS[self._last_quality_index][1]

        worker = ThreadStart(
            lambda: self._edit_worker(image_to_edit, instruction, model_id, logo_path)
        )
        thread = Thread(worker)
        thread.IsBackground = True
        thread.Start()

    def _edit_worker(self, image_path, instruction, model_id, logo_path=None):
        try:
            image_bytes = call_edit_api(self.api_key, image_path, instruction, model_id, logo_path)
            edited_path = save_edited_image(image_bytes, image_path)
            self.Dispatcher.BeginInvoke(Action(lambda: self._finish_edit_ui(edited_path)))
        except Exception as ex:
            self.Dispatcher.BeginInvoke(Action(
                lambda: self._finish_edit_ui(None, error=str(ex))
            ))

    def _finish_edit_ui(self, edited_path, error=None):
        if self._closed:
            return
        self.edit_btn.IsEnabled = True
        self.accept_btn.IsEnabled = True
        self.rerender_btn.IsEnabled = True
        self.attach_logo_btn.IsEnabled = True
        self.edit_progress_bar.Visibility = Visibility.Collapsed
        if error:
            self.edit_status_text.Text = "Edit failed: {}".format(error[:200])
            return
        # Save instruction to recents
        instruction = self.edit_instruction_box.Text.strip()
        if instruction:
            recent = get_recent_edit_instructions()
            updated = push_recent_extra(recent, instruction)
            save_recent_edit_instructions(updated)
            # Refresh combo
            self.recent_edit_combo.ItemsSource = [RECENT_PLACEHOLDER] + updated
            self.recent_edit_combo.IsEnabled = True
            self.recent_edit_combo.SelectedIndex = 0
        self.result_image_path = edited_path
        self.edit_status_text.Text = "Edit applied. Review the result, edit again, or Accept."
        try:
            self.result_image_view.Source = BitmapImage(Uri(edited_path))
        except Exception:
            pass

    def _show_placed_ui(self, target_desc, error=None):
        if self._closed:
            return
        self.progress_panel.Visibility = Visibility.Collapsed
        self.placed_panel.Visibility = Visibility.Visible
        if error:
            self.placed_status_text.Text = "Couldn't place the image:\n{}".format(error)
        else:
            self.placed_status_text.Text = "Done — placed on {}.".format(target_desc)
        try:
            logo_path = script.get_bundle_file("logo.png")
            self.placed_logo_image.Source = BitmapImage(Uri(logo_path))
        except Exception:
            pass

    def new_render_click(self, sender, args):
        try:
            self.review_panel.Visibility = Visibility.Collapsed
            self.placed_panel.Visibility = Visibility.Collapsed
        except Exception:
            pass
        self.settings_panel.Visibility = Visibility.Visible

    def close_click(self, sender, args):
        self.Close()


def get_titleblock_names(doc):
    collector = (
        DB.FilteredElementCollector(doc)
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
        .WhereElementIsElementType()
    )
    return sorted(set(_element_name(s) for s in collector))


def open_containing_folder(file_path):
    try:
        Process.Start("explorer.exe", '/select,"{}"'.format(file_path))
    except Exception:
        pass  # non-critical


# ---------------------------------------------------------------------------
# Revit-side helpers
# ---------------------------------------------------------------------------
def get_api_key():
    key = os.environ.get(API_KEY_ENV_VAR)
    if not key:
        forms.alert(
            "No API key found.\n\n"
            "Set a Windows environment variable named '{}' with your "
            "Gemini API key, then restart Revit.".format(API_KEY_ENV_VAR),
            exitscript=True,
        )
    return key


def get_active_3d_view():
    view = revit.active_view
    if not isinstance(view, DB.View3D):
        forms.alert("Activate a 3D view before running AI Render.", exitscript=True)
    return view


def export_view_to_image(doc, view):
    if not os.path.exists(EXPORT_FOLDER):
        os.makedirs(EXPORT_FOLDER)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    basename = "AIRenderSource_{}".format(stamp)
    filepath = os.path.join(EXPORT_FOLDER, basename)

    options = DB.ImageExportOptions()
    options.FilePath = filepath
    options.ExportRange = DB.ExportRange.CurrentView
    options.ZoomType = DB.ZoomFitType.FitToPage
    options.PixelSize = 1000
    options.ImageResolution = DB.ImageResolution.DPI_300
    options.HLRandWFViewsFileType = DB.ImageFileType.PNG
    options.ShadowViewsFileType = DB.ImageFileType.PNG

    doc.ExportImage(options)

    # Find the produced file by our timestamp prefix — never use GetFileName
    # since it can return a stale cached path inconsistent with our FilePath.
    candidates = sorted(
        [f for f in os.listdir(EXPORT_FOLDER) if f.startswith(basename)],
        reverse=True,
    )
    if candidates:
        return os.path.join(EXPORT_FOLDER, candidates[0])
    if os.path.exists(filepath + ".png"):
        return filepath + ".png"
    raise RuntimeError(
        "Could not find the exported image. Expected a file starting with "
        "'{}' in '{}'.".format(basename, EXPORT_FOLDER)
    )


# ---------------------------------------------------------------------------
# AI image API call (System.Net.WebClient, no `requests` dependency)
# ---------------------------------------------------------------------------
class _TimeoutWebClient(WebClient):
    """WebClient with a longer timeout for slow image-generation responses,
    and keep-alive disabled - Google's servers occasionally close a pooled
    keep-alive connection, which .NET's WebClient doesn't always recover
    from gracefully ("connection expected to be kept alive was closed").
    Forcing a fresh connection per request avoids that."""
    def GetWebRequest(self, address):
        request = WebClient.GetWebRequest(self, address)
        request.Timeout = 300000  # 5 minutes - was 3, gave too little headroom on this network
        request.KeepAlive = False
        return request


UPLOAD_MAX_ATTEMPTS = 3        # retries for the image-generation POST
UPLOAD_RETRY_DELAY_SECONDS = 5


def _upload_with_retry(url, json_body):
    """POST json_body to url, retrying a few times on transient network
    failures. This network has shown occasional mid-upload connection
    drops on larger payloads (base64 images)."""
    last_error = None
    for attempt in range(UPLOAD_MAX_ATTEMPTS):
        client = _TimeoutWebClient()
        client.Encoding = Encoding.UTF8
        client.Headers.Add("Content-Type", "application/json")
        try:
            return client.UploadString(url, "POST", json_body)
        except WebException as ex:
            last_error = ex
            if attempt < UPLOAD_MAX_ATTEMPTS - 1:
                time.sleep(UPLOAD_RETRY_DELAY_SECONDS)

    details = ""
    if last_error.Response:
        reader = StreamReader(last_error.Response.GetResponseStream())
        details = reader.ReadToEnd()
    raise RuntimeError(
        "AI image API call failed: {} {}".format(last_error.Message, details[:500])
    )


def call_image_api(api_key, image_path, prompt, model_id):
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            ]
        }]
    }
    url = "{}?key={}".format(GEMINI_ENDPOINT.format(model=model_id), api_key)
    response_text = _upload_with_retry(url, json.dumps(body, ensure_ascii=True))
    data = json.loads(response_text)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise RuntimeError(
        "No image returned by the API. Response: {}".format(response_text[:500])
    )



    result_path = source_path.replace("AIRenderSource", "AIRenderResult")
    if not result_path.lower().endswith(".png"):
        result_path += ".png"
    with open(result_path, "wb") as f:
        f.write(image_bytes)
    return result_path


def save_result_image(image_bytes, source_path):
    result_path = source_path.replace("AIRenderSource", "AIRenderResult")
    if not result_path.lower().endswith(".png"):
        result_path += ".png"
    with open(result_path, "wb") as f:
        f.write(image_bytes)
    return result_path


def call_edit_api(api_key, image_path, instruction, model_id, logo_path=None):
    """Send an already-rendered image back to Gemini with a text edit
    instruction, optionally including a second reference image (logo/branding).
    When a logo is attached, both images are sent as parts — the rendered
    image first, then the logo — with the instruction telling Gemini what
    to do with the logo in the context of the render."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")

    if logo_path:
        # Detect mime type from extension
        ext = os.path.splitext(logo_path)[1].lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        with open(logo_path, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode("ascii")
        prompt_text = (
            "I am providing two images: the first is an architectural render, "
            "the second is a logo or reference image. "
            "Edit the architectural render as follows: {}. "
            "Use the second image (the logo/reference) as specified in the instruction. "
            "Preserve the overall composition, geometry, and camera angle of the render "
            "- only modify what the instruction asks for."
        ).format(_ascii_safe(instruction))
        parts = [
            {"text": prompt_text},
            {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            {"inline_data": {"mime_type": mime, "data": logo_b64}},
        ]
    else:
        prompt_text = (
            "Edit this architectural render image: {}. "
            "Preserve the overall composition, geometry, and camera angle "
            "- only modify what the instruction asks for."
        ).format(_ascii_safe(instruction))
        parts = [
            {"text": prompt_text},
            {"inline_data": {"mime_type": "image/png", "data": image_b64}},
        ]

    body = {"contents": [{"parts": parts}]}
    url = "{}?key={}".format(GEMINI_ENDPOINT.format(model=model_id), api_key)
    response_text = _upload_with_retry(url, json.dumps(body, ensure_ascii=True))
    data = json.loads(response_text)
    parts_out = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts_out:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise RuntimeError(
        "No image returned by the edit API. Response: {}".format(response_text[:500])
    )
    """Send an already-rendered image back to Gemini with a text edit
    instruction. Uses the same generateContent endpoint as the initial
    render — per Google's docs, image editing is just generateContent
    with the existing image + an edit instruction as the prompt.
    The edited image is returned as inlineData in the response."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    body = {
        "contents": [{
            "parts": [
                {"text": "Edit this architectural render image: {}. "
                         "Preserve the overall composition, geometry, and "
                         "camera angle — only modify what the instruction asks for.".format(instruction)},
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            ]
        }]
    }
    url = "{}?key={}".format(GEMINI_ENDPOINT.format(model=model_id), api_key)
    response_text = _upload_with_retry(url, json.dumps(body))
    data = json.loads(response_text)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise RuntimeError(
        "No image returned by the edit API. Response: {}".format(response_text[:500])
    )


def save_edited_image(image_bytes, previous_path):
    """Save edited image with a numbered suffix so each edit is kept."""
    from datetime import datetime
    stamp = datetime.now().strftime("%H%M%S")
    if previous_path.lower().endswith(".png"):
        edited_path = previous_path[:-4] + "_edit_{}.png".format(stamp)
    else:
        edited_path = previous_path + "_edit_{}.png".format(stamp)
    with open(edited_path, "wb") as f:
        f.write(image_bytes)
    return edited_path


# ---------------------------------------------------------------------------
# Before / after sheet
# ---------------------------------------------------------------------------
def _element_name(element):
    """element.Name can raise AttributeError under IronPython for some
    element types (a known Revit API/IronPython interop quirk, seen on
    some title block symbols). Binding to the base Element.Name property
    descriptor explicitly works around it."""
    return DB.Element.Name.__get__(element)


def get_titleblock_type(doc, titleblock_name):
    collector = (
        DB.FilteredElementCollector(doc)
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
        .WhereElementIsElementType()
    )
    for symbol in collector:
        if _element_name(symbol) == titleblock_name:
            return symbol
    return None


def next_sheet_number(doc, prefix="AI-"):
    existing = set(s.SheetNumber for s in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet))
    i = 1
    while "{}{:03d}".format(prefix, i) in existing:
        i += 1
    return "{}{:03d}".format(prefix, i)


MM_PER_FOOT = 304.8


def mm_to_ft(mm):
    """The Revit API always works in decimal feet internally, regardless of
    the project's display units - this converts our millimeter-based
    tuning constants (and the UI's mm offset field) to feet at the point
    of use."""
    return mm / MM_PER_FOOT


# Sheet layout constants, in millimeters (converted to feet where they're
# actually used, since the Revit API requires feet regardless of the
# project's units). The margins below are the *confirmed real* print
# margins for this A3 title block (measured directly, not estimated) -
# 15mm top/left/right, 25mm bottom.
SHEET_MARGIN_LEFT_MM = 15
SHEET_MARGIN_RIGHT_MM = 15
SHEET_MARGIN_TOP_MM = 15
SHEET_MARGIN_BOTTOM_MM = 25
IMAGE_GAP_MM = 45                 # gap between the two images
LABEL_GAP_MM = 10                 # space reserved above each image for its BEFORE/AFTER label
PROMPT_HEIGHT_MM = 20             # space reserved below the images for the prompt text block
ROW_GAP_MM = 25                   # breathing room between the prompt block and the images


def _fit_image_width(img, max_width, max_height):
    """Return the Width to set on img (with LockProportions on) so it fits
    within max_width x max_height while keeping its original aspect ratio."""
    aspect = float(img.Width) / float(img.Height)
    width = max_width
    height = width / aspect
    if height > max_height:
        height = max_height
        width = height * aspect
    return width


def create_before_after_sheet(doc, source_path, result_path, view_name, prompt_text, titleblock_name, h_offset=0.0):
    titleblock = get_titleblock_type(doc, titleblock_name)
    if titleblock is None:
        forms.alert(
            "Title block '{}' could not be found (it may have been removed "
            "from the project since the dialog opened). Try again.".format(
                titleblock_name
            ),
            exitscript=True,
        )

    text_type_id = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)

    with revit.Transaction("Create AI Render Comparison Sheet"):
        sheet = DB.ViewSheet.Create(doc, titleblock.Id)
        sheet.Name = "AI Render - {}".format(view_name)
        sheet.SheetNumber = next_sheet_number(doc)

        before_type = DB.ImageType.Create(
            doc, DB.ImageTypeOptions(source_path, False, DB.ImageTypeSource.Import)
        )
        after_type = DB.ImageType.Create(
            doc, DB.ImageTypeOptions(result_path, False, DB.ImageTypeSource.Import)
        )

        before_img = DB.ImageInstance.Create(
            doc, sheet, before_type.Id, DB.ImagePlacementOptions()
        )
        after_img = DB.ImageInstance.Create(
            doc, sheet, after_type.Id, DB.ImagePlacementOptions()
        )

        # Find the title block instance Revit auto-placed on this sheet, so
        # we can size/position against its *actual* printable area instead
        # of guessing fixed coordinates (which don't generalize across
        # title block families, and don't account for the two images
        # coming in at different native pixel sizes/aspect ratios).
        tb_instance = (
            DB.FilteredElementCollector(doc, sheet.Id)
            .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
            .WhereElementIsNotElementType()
            .FirstElement()
        )
        tb_bbox = tb_instance.get_BoundingBox(sheet) if tb_instance else None

        # Convert the mm-based tuning constants to feet here, once, since
        # the Revit API calls below all require feet.
        margin_left = mm_to_ft(SHEET_MARGIN_LEFT_MM)
        margin_right = mm_to_ft(SHEET_MARGIN_RIGHT_MM)
        margin_top = mm_to_ft(SHEET_MARGIN_TOP_MM)
        margin_bottom = mm_to_ft(SHEET_MARGIN_BOTTOM_MM)
        image_gap = mm_to_ft(IMAGE_GAP_MM)
        label_gap = mm_to_ft(LABEL_GAP_MM)
        prompt_height = mm_to_ft(PROMPT_HEIGHT_MM)
        row_gap = mm_to_ft(ROW_GAP_MM)
        h_offset_ft = mm_to_ft(h_offset)

        # Both axes from the title block instance's bounding box, using the
        # confirmed real print margins (15mm top/left/right, 25mm bottom -
        # measured directly, not estimated).
        if tb_bbox:
            area_min_x = tb_bbox.Min.X + margin_left
            area_max_x = tb_bbox.Max.X - margin_right
            area_min_y = tb_bbox.Min.Y + margin_bottom
            area_max_y = tb_bbox.Max.Y - margin_top
            # True horizontal midpoint of the whole title block (not the
            # margin-adjusted area) - this is what centering should be
            # measured against, independent of image size.
            page_center_x = (tb_bbox.Min.X + tb_bbox.Max.X) / 2.0
        else:
            area_min_x, area_max_x = 0.3, 1.1
            area_min_y, area_max_y = 0.5, 0.9
            page_center_x = (area_min_x + area_max_x) / 2.0

        available_width = max(area_max_x - area_min_x, 0.5)
        available_height = max(area_max_y - area_min_y, 0.3)

        # No inflation multiplier needed now that the margins are the
        # confirmed real values rather than conservative guesses - this
        # already fills the true usable area.
        max_image_height = max(available_height - label_gap - row_gap - prompt_height, 0.2)
        max_image_width = max((available_width - image_gap) / 2.0, 0.3)

        before_img.LockProportions = True
        before_img.Width = _fit_image_width(before_img, max_image_width, max_image_height)

        after_img.LockProportions = True
        after_img.Width = _fit_image_width(after_img, max_image_width, max_image_height)

        images_total_width = before_img.Width + image_gap + after_img.Width
        # Center on the title block's true midpoint directly, rather than
        # on the margin-constrained available_width, so this stays correct
        # regardless of how big the fitted images end up being.
        start_x = page_center_x - images_total_width / 2.0 + h_offset_ft
        # area_min_y is now the TRUE bottom-margin edge (25mm, confirmed),
        # so the image block can anchor directly above it - no empirical
        # offset needed.
        images_bottom_y = area_min_y + prompt_height + row_gap

        before_pt = DB.XYZ(start_x, images_bottom_y, 0)
        before_img.SetLocation(before_pt, DB.BoxPlacement.BottomLeft)

        after_pt = DB.XYZ(start_x + before_img.Width + image_gap, images_bottom_y, 0)
        after_img.SetLocation(after_pt, DB.BoxPlacement.BottomLeft)

        DB.TextNote.Create(
            doc, sheet.Id,
            DB.XYZ(before_pt.X, before_pt.Y + before_img.Height + label_gap, 0),
            before_img.Width, "BEFORE", DB.TextNoteOptions(text_type_id),
        )
        DB.TextNote.Create(
            doc, sheet.Id,
            DB.XYZ(after_pt.X, after_pt.Y + after_img.Height + label_gap, 0),
            after_img.Width, "AFTER", DB.TextNoteOptions(text_type_id),
        )
        DB.TextNote.Create(
            doc, sheet.Id,
            DB.XYZ(start_x, images_bottom_y - row_gap, 0),
            images_total_width,
            "Prompt: {}".format(prompt_text),
            DB.TextNoteOptions(text_type_id),
        )

    return sheet


def place_image_on_drafting_view(doc, image_path, view_name):
    """Fallback used when the before/after sheet is skipped."""
    existing = list(DB.FilteredElementCollector(doc).OfClass(DB.ViewDrafting))
    drafting_view = next((v for v in existing if _element_name(v) == view_name), None)

    with revit.Transaction("Place AI Render"):
        if drafting_view is None:
            view_family_type = next(
                vft for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
                if vft.ViewFamily == DB.ViewFamily.Drafting
            )
            drafting_view = DB.ViewDrafting.Create(doc, view_family_type.Id)
            drafting_view.Name = view_name

        image_type = DB.ImageType.Create(
            doc, DB.ImageTypeOptions(image_path, False, DB.ImageTypeSource.Import)
        )
        DB.ImageInstance.Create(doc, drafting_view, image_type.Id, DB.ImagePlacementOptions())

    return drafting_view


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    doc = revit.doc
    api_key = get_api_key()
    view = get_active_3d_view()
    view_name = _element_name(view)

    xaml_path = script.get_bundle_file("ui.xaml")
    last_extra, last_style_index, last_titleblock, last_h_offset, last_quality_index = load_last_prompt()
    recent_extras = get_recent_extras()
    titleblock_names = get_titleblock_names(doc)

    prompt_form = PromptForm(
        xaml_path, doc, view, api_key, view_name,
        last_extra, last_style_index, titleblock_names,
        last_titleblock, recent_extras, last_h_offset, last_quality_index
    )
    prompt_form.ShowDialog()


if __name__ == "__main__":
    main()
