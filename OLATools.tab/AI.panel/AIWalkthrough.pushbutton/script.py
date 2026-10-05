# -*- coding: utf-8 -*-
"""
AI Walkthrough
--------------
Exports the active 3D view, renders it as a still (same pipeline as AI
Render), then animates that still into a short video using Veo 3.1
(image-to-video). Since Revit sheets/views cannot embed or play video, the
result is saved as an .mp4 next to your AI Render exports, and the
containing folder is opened automatically when it's done.

Runs on the IronPython2 engine (see bundle.yaml) - same reasoning as AI
Render: pyrevit.forms/WPF needs IronPython2, and the HTTP calls use .NET's
System.Net.WebClient instead of `requests`.

Setup: uses the same GEMINI_API_KEY environment variable as AI Render - no
extra setup needed if you've already configured that tool.
"""

import os
import json
import time
import base64
from datetime import datetime

import clr
clr.AddReference("System")
from System.Net import WebClient, WebException, ServicePointManager
from System.IO import StreamReader
from System.IO import File as DotNetFile
from System.Text import Encoding
from System.Diagnostics import Process
from System import Uri, TimeSpan, Action
from System.Threading import Thread, ThreadStart
clr.AddReference("PresentationCore")
from System.Windows import Visibility
from System.Windows.Media.Imaging import BitmapImage

# Some networks/proxies stall on the "Expect: 100-continue" handshake .NET
# sends before larger POST bodies (like our base64 image). Disabling it is
# the standard fix for a WebClient POST that hangs only on bigger payloads.
ServicePointManager.Expect100Continue = False

from pyrevit import revit, DB, forms, script

logger = script.get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IMAGE_QUALITY_PRESETS = [
    ("Draft (fastest, cheapest)", "gemini-3.1-flash-lite-image"),
    ("Standard", "gemini-3.1-flash-image"),
    ("Final (Pro quality, slower)", "gemini-3-pro-image"),
]
VIDEO_QUALITY_PRESETS = [
    ("Lite (cheapest, ~$0.40/clip)", "veo-3.1-lite-generate-preview"),
    ("Fast (~$1.20/clip)", "veo-3.1-fast-generate-preview"),
    ("Standard (best quality, ~$3.20/clip)", "veo-3.1-generate-preview"),
]
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

GEMINI_IMAGE_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
VEO_START_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:predictLongRunning"
)
VEO_OPERATION_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/{operation_name}"

API_KEY_ENV_VAR = "GEMINI_API_KEY"
# Same folder AI Render uses, so everything AI-generated lands in one place.
EXPORT_FOLDER = os.path.join(
    os.environ.get("USERPROFILE", os.getcwd()), "Documents", "AIRender"
)  # Documents, not Temp - Temp can be cleared by Windows/IT policy between sessions

VIDEO_DURATION_SECONDS = 8     # Veo 3.1's max per generation - must be a JSON
                                # number, not the quoted string Google's own
                                # docs show (API rejects the string form)
VIDEO_ASPECT_RATIO = "16:9"
VIDEO_RESOLUTION = "720p"      # cheapest tier; raise to "1080p"/"4k" if needed
POLL_INTERVAL_SECONDS = 10
MAX_POLL_ATTEMPTS = 40         # ~6-7 min, matching Google's documented max latency
UPLOAD_MAX_ATTEMPTS = 3        # retries for any large POST (image or video-job submission)
UPLOAD_RETRY_DELAY_SECONDS = 5

MAX_RECENT_EXTRAS = 10
MAX_RECENT_MOTIONS = 10
RECENT_PLACEHOLDER = "Select a recent motion prompt..."
RECENT_EMPTY_PLACEHOLDER = "No recent motion prompts yet"


# ---------------------------------------------------------------------------
# Remembered settings
# ---------------------------------------------------------------------------
def load_last_settings():
    config = script.get_config()
    last_style_index = getattr(config, "walkthrough_style_index", 0)
    if not isinstance(last_style_index, int) or not (0 <= last_style_index < len(STYLE_PRESETS)):
        last_style_index = 0
    last_image_quality_index = getattr(config, "walkthrough_image_quality_index", 0)
    if not isinstance(last_image_quality_index, int) or not (
        0 <= last_image_quality_index < len(IMAGE_QUALITY_PRESETS)
    ):
        last_image_quality_index = 0
    last_video_quality_index = getattr(config, "walkthrough_video_quality_index", 0)
    if not isinstance(last_video_quality_index, int) or not (
        0 <= last_video_quality_index < len(VIDEO_QUALITY_PRESETS)
    ):
        last_video_quality_index = 0
    last_extra = getattr(config, "walkthrough_last_extra", "")
    last_motion = getattr(config, "walkthrough_last_motion", "")
    return last_style_index, last_image_quality_index, last_video_quality_index, last_extra, last_motion


def get_recent_extras():
    config = script.get_config()
    recent = getattr(config, "walkthrough_recent_extras", None)
    if not isinstance(recent, list):
        recent = []
    return recent


def push_recent_extra(recent, new_extra):
    new_extra = (new_extra or "").strip()
    if not new_extra:
        return recent
    updated = [new_extra] + [e for e in recent if e != new_extra]
    return updated[:MAX_RECENT_EXTRAS]


def get_recent_motions():
    config = script.get_config()
    recent = getattr(config, "walkthrough_recent_motions", None)
    if not isinstance(recent, list):
        recent = []
    return recent


def push_recent_motion(recent, new_motion):
    new_motion = (new_motion or "").strip()
    if not new_motion:
        return recent
    updated = [new_motion] + [m for m in recent if m != new_motion]
    return updated[:MAX_RECENT_MOTIONS]


def save_last_settings(style_index, image_quality_index, video_quality_index,
                        extra, recent_extras, motion, recent_motions):
    config = script.get_config()
    config.walkthrough_style_index = style_index
    config.walkthrough_image_quality_index = image_quality_index
    config.walkthrough_video_quality_index = video_quality_index
    config.walkthrough_last_extra = extra
    config.walkthrough_recent_extras = recent_extras
    config.walkthrough_last_motion = motion
    config.walkthrough_recent_motions = recent_motions
    script.save_config()


# ---------------------------------------------------------------------------
# WPF prompt window
# ---------------------------------------------------------------------------
def build_prompts(style, extra, motion):
    still_prompt = (
        "Render this architectural 3D view as a {style} image. "
        "Preserve the exact geometry, proportions, and camera framing shown "
        "in the source image — only change materials, lighting, and "
        "atmosphere. {extra}"
    ).format(style=style[0].lower() + style[1:], extra=extra)

    motion_prompt = motion.strip() or (
        "Subtle, slow camera movement exploring the space. Gentle, natural "
        "motion in the environment (light shifting, foliage swaying, people "
        "moving naturally) while the architecture stays the clear focus. No "
        "dialogue."
    )
    return still_prompt, motion_prompt


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
    """Single window covering the whole flow: settings -> progress -> video
    preview, swapped via panel visibility rather than separate windows.
    Generation runs on a background thread (HTTP calls only - the one
    Revit API touch, exporting the view, happens synchronously first on
    the main thread, since Revit API calls aren't safe off-thread)."""

    def __init__(self, xaml_file, doc, view, api_key,
                 last_style_index, last_image_quality_index, last_video_quality_index,
                 last_extra, recent_extras, last_motion, recent_motions):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self.view = view
        self.api_key = api_key
        self.recent_extras = recent_extras or []
        self.recent_motions = recent_motions or []
        self.video_path = None
        self._closed = False
        self.Closed += self.on_window_closed

        self.style_combo.ItemsSource = STYLE_PRESETS
        self.style_combo.SelectedIndex = last_style_index

        self.extra_box.Text = last_extra
        if self.recent_extras:
            self.recent_extra_combo.ItemsSource = [RECENT_PLACEHOLDER] + self.recent_extras
            self.recent_extra_combo.IsEnabled = True
        else:
            self.recent_extra_combo.ItemsSource = [RECENT_EMPTY_PLACEHOLDER]
            self.recent_extra_combo.IsEnabled = False
        self.recent_extra_combo.SelectedIndex = 0

        self.image_quality_combo.ItemsSource = [label for label, _ in IMAGE_QUALITY_PRESETS]
        self.image_quality_combo.SelectedIndex = last_image_quality_index

        self.video_quality_combo.ItemsSource = [label for label, _ in VIDEO_QUALITY_PRESETS]
        self.video_quality_combo.SelectedIndex = last_video_quality_index

        self.motion_box.Text = last_motion
        if self.recent_motions:
            self.recent_motion_combo.ItemsSource = [RECENT_PLACEHOLDER] + self.recent_motions
            self.recent_motion_combo.IsEnabled = True
        else:
            self.recent_motion_combo.ItemsSource = [RECENT_EMPTY_PLACEHOLDER]
            self.recent_motion_combo.IsEnabled = False
        self.recent_motion_combo.SelectedIndex = 0

        try:
            logo_path = script.get_bundle_file("logo.png")
            self.logo_image.Source = BitmapImage(Uri(logo_path))
        except Exception:
            pass  # missing/unreadable logo shouldn't block the tool

    # -- settings panel handlers --------------------------------------
    def recent_extra_combo_changed(self, sender, args):
        if self.recent_extra_combo.SelectedIndex <= 0:
            return
        self.extra_box.Text = self.recent_extra_combo.SelectedItem
        self.recent_extra_combo.SelectedIndex = 0

    def recent_motion_combo_changed(self, sender, args):
        if self.recent_motion_combo.SelectedIndex <= 0:
            return
        self.motion_box.Text = self.recent_motion_combo.SelectedItem
        self.recent_motion_combo.SelectedIndex = 0

    def render_click(self, sender, args):
        style = self.style_combo.SelectedItem
        extra = self.extra_box.Text or ""
        image_quality_index = self.image_quality_combo.SelectedIndex
        video_quality_index = self.video_quality_combo.SelectedIndex
        motion = self.motion_box.Text or ""

        updated_recent_extras = push_recent_extra(self.recent_extras, extra)
        updated_recent_motions = push_recent_motion(self.recent_motions, motion)
        save_last_settings(
            STYLE_PRESETS.index(style), image_quality_index, video_quality_index,
            extra, updated_recent_extras, motion, updated_recent_motions
        )
        self.recent_extras = updated_recent_extras
        self.recent_motions = updated_recent_motions

        image_model_id = IMAGE_QUALITY_PRESETS[image_quality_index][1]
        video_model_id = VIDEO_QUALITY_PRESETS[video_quality_index][1]
        still_prompt, motion_prompt = build_prompts(style, extra, motion)

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
            lambda: self._generate_worker(source_image, still_prompt, motion_prompt,
                                           image_model_id, video_model_id)
        )
        thread = Thread(worker)
        thread.IsBackground = True
        thread.Start()

    def cancel_click(self, sender, args):
        self.Close()

    def on_window_closed(self, sender, args):
        self._closed = True

    # -- background worker (runs off the UI thread) --------------------
    def _generate_worker(self, source_image, still_prompt, motion_prompt,
                          image_model_id, video_model_id):
        try:
            self._set_status("Generating still image... ☕ go grab a coffee, this'll take a few minutes")
            image_bytes = call_image_api(self.api_key, source_image, still_prompt, image_model_id)

            still_path = save_still_image(image_bytes, source_image)

            self._set_status(
                "Generating video — this can take a few minutes..."
            )

            def on_poll_progress(attempt, max_attempts):
                elapsed_min = (attempt * POLL_INTERVAL_SECONDS) // 60
                self._set_status(
                    "Generating video — this can take a few minutes... "
                    "(~{} min elapsed) — have some more coffee ☕".format(elapsed_min)
                )

            video_bytes = call_veo_api(
                self.api_key, still_path, motion_prompt, video_model_id, on_poll_progress
            )

            self._set_status("Saving video...")
            video_path = save_video(video_bytes, still_path)

            self._show_preview(video_path)
        except Exception as ex:
            self._show_error(str(ex))

    # -- thread-safe UI updates (marshaled via Dispatcher) --------------
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

    def _show_preview(self, video_path):
        try:
            self.Dispatcher.BeginInvoke(Action(lambda: self._show_preview_ui(video_path)))
        except Exception:
            pass

    def _show_preview_ui(self, video_path):
        if self._closed:
            return
        self.video_path = video_path
        self.progress_panel.Visibility = Visibility.Collapsed
        self.preview_panel.Visibility = Visibility.Visible
        self.preview_status_text.Text = video_path
        try:
            self.video_player.Source = Uri(video_path)
            self.video_player.Play()
        except Exception as ex:
            self.preview_status_text.Text = (
                "Couldn't start the preview ({}). The video itself is fine "
                "— saved to:\n{}"
            ).format(ex, video_path)

    # -- preview panel handlers -----------------------------------------
    def video_ended(self, sender, args):
        self.video_player.Position = TimeSpan.Zero
        self.video_player.Play()

    def video_failed(self, sender, args):
        self.preview_status_text.Text = (
            "Couldn't preview the video in this window (a codec or media "
            "component may be missing on this machine). The file itself is "
            "fine — saved to:\n{}"
        ).format(self.video_path)

    def open_folder_click(self, sender, args):
        if self.video_path:
            open_containing_folder(self.video_path)

    def new_render_click(self, sender, args):
        try:
            self.video_player.Stop()
        except Exception:
            pass
        self.preview_panel.Visibility = Visibility.Collapsed
        self.settings_panel.Visibility = Visibility.Visible

    def close_click(self, sender, args):
        try:
            self.video_player.Stop()
        except Exception:
            pass
        self.Close()


# ---------------------------------------------------------------------------
# Revit-side helpers (same pattern as AI Render)
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
        forms.alert("Activate a 3D view before running AI Walkthrough.", exitscript=True)
    return view


def export_view_to_image(doc, view):
    if not os.path.exists(EXPORT_FOLDER):
        os.makedirs(EXPORT_FOLDER)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    basename = "AIWalkthroughSource_{}".format(stamp)
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
# HTTP client helpers (shared shape for image + video calls)
# ---------------------------------------------------------------------------
class _TimeoutWebClient(WebClient):
    """WebClient with a longer timeout and keep-alive disabled - same fixes
    as AI Render needed for larger payloads and Google's connection
    handling."""
    def GetWebRequest(self, address):
        request = WebClient.GetWebRequest(self, address)
        request.Timeout = 300000  # 5 minutes - was 3, gave too little headroom on this network
        request.KeepAlive = False
        return request


def _new_client(api_key):
    client = _TimeoutWebClient()
    client.Encoding = Encoding.UTF8
    client.Headers.Add("x-goog-api-key", api_key)
    client.Headers.Add("Content-Type", "application/json")
    return client


def _raise_web_error(ex, context):
    details = ""
    if ex.Response:
        reader = StreamReader(ex.Response.GetResponseStream())
        details = reader.ReadToEnd()
    raise RuntimeError("{} failed: {} {}".format(context, ex.Message, details[:500]))


def _upload_with_retry(api_key, url, json_body, context):
    """POST json_body to url, retrying a few times on transient network
    failures. This network has shown occasional mid-upload connection
    drops on larger payloads (base64 images) - seen on both the still
    image call and the video job submission, so this is shared rather
    than fixed in just one place."""
    last_error = None
    for attempt in range(UPLOAD_MAX_ATTEMPTS):
        client = _new_client(api_key)
        try:
            return client.UploadString(url, "POST", json_body)
        except WebException as ex:
            last_error = ex
            if attempt < UPLOAD_MAX_ATTEMPTS - 1:
                time.sleep(UPLOAD_RETRY_DELAY_SECONDS)
    _raise_web_error(last_error, context)


# ---------------------------------------------------------------------------
# Still image generation (same call pattern as AI Render)
# ---------------------------------------------------------------------------
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
    json_body = json.dumps(body)
    url = GEMINI_IMAGE_ENDPOINT.format(model=model_id)

    response_text = _upload_with_retry(api_key, url, json_body, "Still image generation")

    data = json.loads(response_text)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])

    raise RuntimeError(
        "No image returned by the API. Response snippet: {}".format(response_text[:500])
    )


def save_still_image(image_bytes, source_path):
    result_path = source_path.replace("AIWalkthroughSource", "AIWalkthroughStill")
    if not result_path.lower().endswith(".png"):
        result_path += ".png"
    with open(result_path, "wb") as f:
        f.write(image_bytes)
    return result_path


# ---------------------------------------------------------------------------
# Video generation (Veo 3.1, image-to-video, async: submit -> poll -> download)
# ---------------------------------------------------------------------------
def call_veo_api(api_key, still_image_path, motion_prompt, video_model_id, progress_callback=None):
    with open(still_image_path, "rb") as f:
        image_bytes = f.read()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    body = {
        "instances": [{
            "prompt": motion_prompt,
            "image": {"bytesBase64Encoded": image_b64, "mimeType": "image/png"},
        }],
        "parameters": {
            "aspectRatio": VIDEO_ASPECT_RATIO,
            "resolution": VIDEO_RESOLUTION,
            "durationSeconds": VIDEO_DURATION_SECONDS,
            # Image-to-video only accepts "allow_adult" - not a workflow choice.
            "personGeneration": "allow_adult",
        },
    }
    json_body = json.dumps(body)
    start_url = VEO_START_ENDPOINT.format(model=video_model_id)

    response_text = _upload_with_retry(api_key, start_url, json_body, "Video generation request")

    operation = json.loads(response_text)
    operation_name = operation.get("name")
    if not operation_name:
        raise RuntimeError(
            "No operation returned by the video API. Response: {}".format(response_text[:500])
        )

    poll_url = VEO_OPERATION_ENDPOINT.format(operation_name=operation_name)

    for attempt in range(MAX_POLL_ATTEMPTS):
        if progress_callback:
            progress_callback(attempt, MAX_POLL_ATTEMPTS)
        time.sleep(POLL_INTERVAL_SECONDS)

        poll_client = _new_client(api_key)
        try:
            status_text = poll_client.DownloadString(poll_url)
        except WebException as ex:
            _raise_web_error(ex, "Video status check")

        status = json.loads(status_text)
        if status.get("done"):
            video_info = (
                status.get("response", {})
                .get("generateVideoResponse", {})
                .get("generatedSamples", [{}])[0]
                .get("video", {})
            )
            video_uri = video_info.get("uri")
            if not video_uri:
                raise RuntimeError(
                    "Video generation finished but no video URI was returned. "
                    "Response: {}".format(status_text[:500])
                )
            download_client = _new_client(api_key)
            try:
                video_bytes = download_client.DownloadData(video_uri)
            except WebException as ex:
                _raise_web_error(ex, "Video download")
            return video_bytes

    raise RuntimeError(
        "Video generation did not finish within {} attempts ({}s each, ~{} "
        "minutes total). It may still complete on Google's end - check "
        "https://aistudio.google.com or try again shortly.".format(
            MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS,
            (MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS) // 60,
        )
    )


def save_video(video_bytes, still_path):
    video_path = still_path.replace("AIWalkthroughStill", "AIWalkthrough")
    if video_path.lower().endswith(".png"):
        video_path = video_path[:-4] + ".mp4"
    else:
        video_path += ".mp4"
    DotNetFile.WriteAllBytes(video_path, video_bytes)
    return video_path


def open_containing_folder(file_path):
    try:
        Process.Start("explorer.exe", '/select,"{}"'.format(file_path))
    except Exception:
        pass  # non-critical - the path is still shown in the completion dialog


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    doc = revit.doc
    api_key = get_api_key()
    view = get_active_3d_view()

    xaml_path = script.get_bundle_file("ui.xaml")
    (last_style_index, last_image_quality_index, last_video_quality_index,
     last_extra, last_motion) = load_last_settings()
    recent_extras = get_recent_extras()
    recent_motions = get_recent_motions()

    prompt_form = PromptForm(
        xaml_path, doc, view, api_key,
        last_style_index, last_image_quality_index, last_video_quality_index,
        last_extra, recent_extras, last_motion, recent_motions
    )
    prompt_form.ShowDialog()


if __name__ == "__main__":
    main()
