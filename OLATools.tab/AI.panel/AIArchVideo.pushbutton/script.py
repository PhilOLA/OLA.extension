# -*- coding: utf-8 -*-
"""
AI Arch Video
-------------
Select one or more 3D views, assign each as Interior or Exterior (auto-
detected from name — "INT" anywhere = Interior, otherwise Exterior), then
generate an AI still render and a Luma Ray 3.2 video clip for each view.
Multiple views can be kept as separate clips or stitched into one video
with hard cut or crossfade transitions (ffmpeg required for stitching).

Interior and exterior views each have their own style, extra direction,
motion prompt, and quality settings.

Setup:
  1. GEMINI_API_KEY  — set as a Windows environment variable, restart Revit.
  2. LUMA_AGENTS_API_KEY — set as a Windows environment variable, restart Revit.
     Get a Luma key from https://platform.lumalabs.ai/
  3. ffmpeg must be on the system PATH for stitching (ffmpeg.org or
     `winget install ffmpeg`). Clip generation works fine without ffmpeg;
     only stitching needs it.
"""

import os
import json
import time
import base64
import subprocess
from datetime import datetime

import clr
clr.AddReference("System")
from System.Net import WebClient, WebException, ServicePointManager
from System.IO import StreamReader, File as DotNetFile
from System.Text import Encoding
clr.AddReference("PresentationCore")
from System.Windows import Visibility
from System.Windows.Media.Imaging import BitmapImage
from System import Uri, Action
from System.Threading import Thread, ThreadStart
from System.Diagnostics import Process
clr.AddReference("PresentationFramework")
from System.Windows.Controls import ListBoxItem, StackPanel, TextBlock, Border
from System.Windows.Media import SolidColorBrush, Color
from System.Windows import HorizontalAlignment, VerticalAlignment, Thickness

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
    ("720p  10s - SDR (default, longest clip)", "720p", "10s", False),
    ("1080p 10s - SDR (full HD, longest clip)", "1080p", "10s", False),
    ("720p  5s  - HDR (best colour accuracy)", "720p", "5s", True),
    ("1080p 5s  - HDR (full HD + best colour)", "1080p", "5s", True),
    ("720p  5s  - SDR (fastest, cheapest)", "720p", "5s", False),
    ("1080p 5s  - SDR", "1080p", "5s", False),
]

# Styles shown for exterior views (excludes interior-only ones)
EXTERIOR_STYLES = [
    "Photorealistic, bright daylight",
    "Photorealistic, early morning light",
    "Photorealistic, dusk with exterior/landscape lighting",
    "Concept sketch, loose linework",
    "Soft watercolor illustration",
    "Clean line-art / technical illustration",
    "Sci-fi space station",
    "Cyberpunk neon night",
    "Underwater research station",
    "Retro-futuristic 1960s",
]

# Styles shown for interior views (excludes exterior-only ones)
INTERIOR_STYLES = [
    "Photorealistic, bright daylight",
    "Photorealistic, early morning light",
    "Photorealistic, warm dusk / interior lighting",
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
LUMA_SUBMIT_ENDPOINT = "https://agents.lumalabs.ai/v1/generations"
LUMA_POLL_ENDPOINT = "https://agents.lumalabs.ai/v1/generations/{id}"

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
LUMA_API_KEY_ENV = "LUMA_AGENTS_API_KEY"

EXPORT_FOLDER = os.path.join(
    os.environ.get("USERPROFILE", os.getcwd()), "Documents", "AIRender"
)

UPLOAD_MAX_ATTEMPTS = 3
UPLOAD_RETRY_DELAY_SECONDS = 5
POLL_INTERVAL_SECONDS = 10
MAX_POLL_ATTEMPTS = 60
INITIAL_POLL_WAIT_SECONDS = 15

MAX_RECENT = 10
RECENT_PLACEHOLDER = "Select a recent prompt..."
RECENT_EMPTY_PLACEHOLDER = "No recent prompts yet"

# Interior detection: "INT" anywhere in the view name (case-insensitive)
INTERIOR_KEYWORD = "int"

# Default prompts
EXT_DEFAULT_EXTRA = (
    "Two-storey healthcare building, warm sand-beige render walls contrasted "
    "with red brick accent elements. Flat overhanging roofline with exposed "
    "structural elements. Lush landscaping, manicured lawn, paved pathways, "
    "natural boulders as landscape accents. Grounded in Bujumbura, Burundi, "
    "warm East African context, mature trees, welcoming and calm."
)
EXT_DEFAULT_MOTION = (
    "Slow, cinematic lateral camera drift revealing the full facade. Gentle "
    "parallax between foreground landscaping and the building. Natural "
    "environmental motion, grass swaying, light shifting across the "
    "walls. Architecture stays the clear focus throughout."
)
INT_DEFAULT_EXTRA = (
    "Modern clinic interior, grey porcelain tile floor with a matte "
    "finish, warm white walls, recessed ceiling lighting. Clean, welcoming, "
    "contemporary healthcare design. Reflect the setting of Bujumbura, "
    "Burundi, warm natural light and materials suited to an East "
    "African tropical climate."
)
INT_DEFAULT_MOTION = (
    "Slow, subtle push forward through the space. Soft natural light streaming "
    "through windows. Gentle environmental life, people moving naturally, "
    "ambient warmth. Architecture stays the clear focus throughout. No "
    "dialogue."
)


# ---------------------------------------------------------------------------
# Remembered settings (separate keys for ext/int so they don't collide)
# ---------------------------------------------------------------------------
def _cfg():
    return script.get_config()


def _safe_int(val, lo, hi, default=0):
    return val if isinstance(val, int) and lo <= val <= hi else default


def load_last_settings():
    cfg = _cfg()
    ext = {
        "style_index": _safe_int(getattr(cfg, "av_ext_style_index", 0), 0, len(EXTERIOR_STYLES) - 1),
        "image_quality_index": _safe_int(getattr(cfg, "av_ext_img_qi", 0), 0, len(IMAGE_QUALITY_PRESETS) - 1),
        "video_quality_index": _safe_int(getattr(cfg, "av_ext_vid_qi", 0), 0, len(VIDEO_QUALITY_PRESETS) - 1),
        "extra": getattr(cfg, "av_ext_extra", EXT_DEFAULT_EXTRA),
        "motion": getattr(cfg, "av_ext_motion", EXT_DEFAULT_MOTION),
        "recent_extras": getattr(cfg, "av_ext_recent_extras", []) or [],
        "recent_motions": getattr(cfg, "av_ext_recent_motions", []) or [],
    }
    int_ = {
        "style_index": _safe_int(getattr(cfg, "av_int_style_index", 0), 0, len(INTERIOR_STYLES) - 1),
        "image_quality_index": _safe_int(getattr(cfg, "av_int_img_qi", 0), 0, len(IMAGE_QUALITY_PRESETS) - 1),
        "video_quality_index": _safe_int(getattr(cfg, "av_int_vid_qi", 4), 0, len(VIDEO_QUALITY_PRESETS) - 1),
        "extra": getattr(cfg, "av_int_extra", INT_DEFAULT_EXTRA),
        "motion": getattr(cfg, "av_int_motion", INT_DEFAULT_MOTION),
        "recent_extras": getattr(cfg, "av_int_recent_extras", []) or [],
        "recent_motions": getattr(cfg, "av_int_recent_motions", []) or [],
    }
    return ext, int_


def save_settings(ext, int_):
    cfg = _cfg()
    cfg.av_ext_style_index = ext["style_index"]
    cfg.av_ext_img_qi = ext["image_quality_index"]
    cfg.av_ext_vid_qi = ext["video_quality_index"]
    cfg.av_ext_extra = ext["extra"]
    cfg.av_ext_motion = ext["motion"]
    cfg.av_ext_recent_extras = ext["recent_extras"]
    cfg.av_ext_recent_motions = ext["recent_motions"]
    cfg.av_int_style_index = int_["style_index"]
    cfg.av_int_img_qi = int_["image_quality_index"]
    cfg.av_int_vid_qi = int_["video_quality_index"]
    cfg.av_int_extra = int_["extra"]
    cfg.av_int_motion = int_["motion"]
    cfg.av_int_recent_extras = int_["recent_extras"]
    cfg.av_int_recent_motions = int_["recent_motions"]
    script.save_config()


def push_recent(recent, new_item):
    new_item = (new_item or "").strip()
    if not new_item:
        return recent
    updated = [new_item] + [e for e in recent if e != new_item]
    return updated[:MAX_RECENT]


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------
def _element_name_safe(element):
    try:
        return DB.Element.Name.__get__(element)
    except Exception:
        return ""


def _element_name(element):
    try:
        return DB.Element.Name.__get__(element)
    except Exception:
        return ""


def is_interior(view_name):
    return INTERIOR_KEYWORD in view_name.lower()


def get_all_3d_views(doc):
    collector = DB.FilteredElementCollector(doc).OfClass(DB.View3D)
    views = [v for v in collector if not v.IsTemplate]
    views.sort(key=_element_name_safe)
    return views


# ---------------------------------------------------------------------------
# WPF prompt window
# ---------------------------------------------------------------------------
# Badge colours
EXT_BG = Color.FromRgb(47, 111, 237)    # blue
INT_BG = Color.FromRgb(39, 174, 96)    # green


class ViewItem(object):
    """Wraps a Revit View3D with its display state for the list."""
    def __init__(self, view):
        self.view = view
        self.name = _element_name_safe(view)
        self.interior = is_interior(self.name)

    def toggle_type(self):
        self.interior = not self.interior


def _make_badge(text, bg_color):
    b = Border()
    b.CornerRadius = System_Windows_CornerRadius(3)
    b.Padding = Thickness(4, 1, 4, 1)
    b.Background = SolidColorBrush(bg_color)
    t = TextBlock()
    t.Text = text
    t.FontSize = 9
    t.FontWeight = System_Windows_FontWeights_Bold()
    t.Foreground = SolidColorBrush(Color.FromRgb(255, 255, 255))
    b.Child = t
    return b


# We need a few WPF types that require explicit import under IronPython
from System.Windows import CornerRadius as System_Windows_CornerRadius
from System.Windows import FontWeights as _FW


def System_Windows_FontWeights_Bold():
    return _FW.Bold


class PromptForm(forms.WPFWindow):
    def __init__(self, xaml_file, doc, all_views, active_view, gemini_key, luma_key):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self.gemini_key = gemini_key
        self.luma_key = luma_key
        self.video_path = None
        self._closed = False
        self._ext_collapsed = False
        self._int_collapsed = False
        self.Closed += self.on_window_closed

        # Build ViewItem list
        self._view_items = [ViewItem(v) for v in all_views]
        self._list_item_panels = {}  # ViewItem -> ListBoxItem, for badge refresh

        # Populate view list with custom rows
        self._populate_view_list(active_view)

        # Load saved settings
        ext, int_ = load_last_settings()

        # Wire exterior settings
        self.ext_style_combo.ItemsSource = EXTERIOR_STYLES
        self.ext_style_combo.SelectedIndex = ext["style_index"]
        self.ext_image_quality_combo.ItemsSource = [l for l, _ in IMAGE_QUALITY_PRESETS]
        self.ext_image_quality_combo.SelectedIndex = ext["image_quality_index"]
        self.ext_video_quality_combo.ItemsSource = [l for l, _, _, _ in VIDEO_QUALITY_PRESETS]
        self.ext_video_quality_combo.SelectedIndex = ext["video_quality_index"]
        self.ext_extra_box.Text = ext["extra"]
        self.ext_motion_box.Text = ext["motion"]
        self._populate_recent(self.ext_recent_extra_combo, ext["recent_extras"])
        self._populate_recent(self.ext_recent_motion_combo, ext["recent_motions"])

        # Wire interior settings
        self.int_style_combo.ItemsSource = INTERIOR_STYLES
        self.int_style_combo.SelectedIndex = int_["style_index"]
        self.int_image_quality_combo.ItemsSource = [l for l, _ in IMAGE_QUALITY_PRESETS]
        self.int_image_quality_combo.SelectedIndex = int_["image_quality_index"]
        self.int_video_quality_combo.ItemsSource = [l for l, _, _, _ in VIDEO_QUALITY_PRESETS]
        self.int_video_quality_combo.SelectedIndex = int_["video_quality_index"]
        self.int_extra_box.Text = int_["extra"]
        self.int_motion_box.Text = int_["motion"]
        self._populate_recent(self.int_recent_extra_combo, int_["recent_extras"])
        self._populate_recent(self.int_recent_motion_combo, int_["recent_motions"])

        # Load logo
        try:
            logo_path = script.get_bundle_file("logo.png")
            self.logo_image.Source = BitmapImage(Uri(logo_path))
        except Exception:
            pass

    # -- View list population -------------------------------------------
    def _populate_view_list(self, active_view):
        active_name = _element_name_safe(active_view) if active_view else ""
        from System.Windows.Controls import CheckBox as WpfCheckBox
        for item in self._view_items:
            row = StackPanel()
            row.Orientation = System_Windows_Controls_Orientation_Horizontal()
            row.Margin = Thickness(2, 2, 2, 2)

            # Checkbox
            cb = WpfCheckBox()
            cb.VerticalAlignment = VerticalAlignment.Center
            cb.Margin = Thickness(0, 0, 8, 0)
            cb.Tag = item
            cb.Checked += self._on_check_changed
            cb.Unchecked += self._on_check_changed
            # Pre-check the active view
            if item.name == active_name:
                cb.IsChecked = True

            # View name
            name_tb = TextBlock()
            name_tb.Text = item.name
            name_tb.FontSize = 12
            name_tb.VerticalAlignment = VerticalAlignment.Center
            name_tb.Margin = Thickness(0, 0, 8, 0)
            name_tb.MaxWidth = 350
            name_tb.TextTrimming = System_Windows_TextTrimming_CharacterEllipsis()

            # Badge (EXT or INT) — click to toggle
            badge = self._make_view_badge(item)

            row.Children.Add(cb)
            row.Children.Add(name_tb)
            row.Children.Add(badge)

            lbi = ListBoxItem()
            lbi.Content = row
            lbi.Tag = item
            # Store reference to checkbox for later reads
            lbi.DataContext = cb
            self.view_list.Items.Add(lbi)
            self._list_item_panels[id(item)] = (lbi, badge)

    def _make_view_badge(self, item):
        """Create a clickable EXT/INT badge for a view item."""
        from System.Windows.Controls import Button as WpfButton
        btn = WpfButton()
        btn.Width = 36
        btn.Height = 18
        btn.Margin = Thickness(0, 0, 0, 0)
        btn.BorderThickness = Thickness(0)
        btn.Cursor = System_Windows_Input_Cursors_Hand()
        self._update_badge_style(btn, item)
        btn.Tag = item
        btn.Click += self._on_badge_click
        return btn

    def _update_badge_style(self, btn, item):
        clr_val = INT_BG if item.interior else EXT_BG
        btn.Background = SolidColorBrush(clr_val)
        tb = TextBlock()
        tb.Text = "INT" if item.interior else "EXT"
        tb.FontSize = 9
        tb.FontWeight = _FW.Bold
        tb.Foreground = SolidColorBrush(Color.FromRgb(255, 255, 255))
        tb.HorizontalAlignment = HorizontalAlignment.Center
        tb.VerticalAlignment = VerticalAlignment.Center
        btn.Content = tb

    def _on_badge_click(self, sender, args):
        item = sender.Tag
        item.toggle_type()
        self._update_badge_style(sender, item)

    # -- Helpers --------------------------------------------------------
    def _populate_recent(self, combo, recent):
        if recent:
            combo.ItemsSource = [RECENT_PLACEHOLDER] + recent
            combo.IsEnabled = True
        else:
            combo.ItemsSource = [RECENT_EMPTY_PLACEHOLDER]
            combo.IsEnabled = False
        combo.SelectedIndex = 0

    def _on_check_changed(self, sender, args):
        """Update multi-clip panel visibility when any checkbox changes."""
        selected = self._get_selected_view_items()
        self.multiclip_panel.Visibility = (
            Visibility.Visible if len(selected) > 1 else Visibility.Collapsed
        )

    def _get_selected_view_items(self):
        """Return ViewItems whose checkbox is checked, in list order."""
        result = []
        for lbi in self.view_list.Items:
            cb = lbi.DataContext
            if cb is not None and cb.IsChecked:
                result.append(lbi.Tag)
        return result

    def _current_ext(self):
        qi_ext = self.ext_video_quality_combo.SelectedIndex
        _, res, dur, hdr = VIDEO_QUALITY_PRESETS[qi_ext]
        return {
            "style_index": self.ext_style_combo.SelectedIndex,
            "image_quality_index": self.ext_image_quality_combo.SelectedIndex,
            "video_quality_index": qi_ext,
            "extra": self.ext_extra_box.Text or "",
            "motion": self.ext_motion_box.Text or "",
            "image_model": IMAGE_QUALITY_PRESETS[self.ext_image_quality_combo.SelectedIndex][1],
            "vid_resolution": res, "vid_duration": dur, "vid_hdr": hdr,
        }

    def _current_int(self):
        qi_int = self.int_video_quality_combo.SelectedIndex
        _, res, dur, hdr = VIDEO_QUALITY_PRESETS[qi_int]
        return {
            "style_index": self.int_style_combo.SelectedIndex,
            "image_quality_index": self.int_image_quality_combo.SelectedIndex,
            "video_quality_index": qi_int,
            "extra": self.int_extra_box.Text or "",
            "motion": self.int_motion_box.Text or "",
            "image_model": IMAGE_QUALITY_PRESETS[self.int_image_quality_combo.SelectedIndex][1],
            "vid_resolution": res, "vid_duration": dur, "vid_hdr": hdr,
        }

    # -- Settings panel handlers ----------------------------------------
    def toggle_ext_click(self, sender, args):
        self._ext_collapsed = not self._ext_collapsed
        self.ext_body.Visibility = Visibility.Collapsed if self._ext_collapsed else Visibility.Visible
        self.ext_arrow.Text = ">" if self._ext_collapsed else "v"

    def toggle_int_click(self, sender, args):
        self._int_collapsed = not self._int_collapsed
        self.int_body.Visibility = Visibility.Collapsed if self._int_collapsed else Visibility.Visible
        self.int_arrow.Text = ">" if self._int_collapsed else "v"

    def output_mode_changed(self, sender, args):
        is_stitch = bool(self.rb_stitch.IsChecked)
        self.transition_panel.Visibility = (
            Visibility.Visible if is_stitch else Visibility.Collapsed
        )

    def move_up_click(self, sender, args):
        items = list(self.view_list.Items)
        checked = [lbi for lbi in items if lbi.DataContext is not None and lbi.DataContext.IsChecked]
        if not checked or items.index(checked[0]) == 0:
            return
        for lbi in checked:
            idx = list(self.view_list.Items).index(lbi)
            self.view_list.Items.Remove(lbi)
            self.view_list.Items.Insert(idx - 1, lbi)

    def move_down_click(self, sender, args):
        items = list(self.view_list.Items)
        checked = [lbi for lbi in items if lbi.DataContext is not None and lbi.DataContext.IsChecked]
        if not checked or list(self.view_list.Items).index(checked[-1]) == len(items) - 1:
            return
        for lbi in reversed(checked):
            idx = list(self.view_list.Items).index(lbi)
            self.view_list.Items.Remove(lbi)
            self.view_list.Items.Insert(idx + 1, lbi)

    # Recent prompt handlers
    def ext_recent_extra_changed(self, sender, args):
        if self.ext_recent_extra_combo.SelectedIndex > 0:
            self.ext_extra_box.Text = self.ext_recent_extra_combo.SelectedItem
            self.ext_recent_extra_combo.SelectedIndex = 0

    def ext_recent_motion_changed(self, sender, args):
        if self.ext_recent_motion_combo.SelectedIndex > 0:
            self.ext_motion_box.Text = self.ext_recent_motion_combo.SelectedItem
            self.ext_recent_motion_combo.SelectedIndex = 0

    def int_recent_extra_changed(self, sender, args):
        if self.int_recent_extra_combo.SelectedIndex > 0:
            self.int_extra_box.Text = self.int_recent_extra_combo.SelectedItem
            self.int_recent_extra_combo.SelectedIndex = 0

    def int_recent_motion_changed(self, sender, args):
        if self.int_recent_motion_combo.SelectedIndex > 0:
            self.int_motion_box.Text = self.int_recent_motion_combo.SelectedItem
            self.int_recent_motion_combo.SelectedIndex = 0

    def render_click(self, sender, args):
        selected_items = self._get_selected_view_items()
        if not selected_items:
            forms.alert("Select at least one 3D view from the list.")
            return

        ext = self._current_ext()
        int_ = self._current_int()

        # Save settings with updated recents
        ext_s = dict(ext)
        int_s = dict(int_)
        ext_s["recent_extras"] = push_recent(
            getattr(_cfg(), "av_ext_recent_extras", []) or [], ext["extra"])
        ext_s["recent_motions"] = push_recent(
            getattr(_cfg(), "av_ext_recent_motions", []) or [], ext["motion"])
        int_s["recent_extras"] = push_recent(
            getattr(_cfg(), "av_int_recent_extras", []) or [], int_["extra"])
        int_s["recent_motions"] = push_recent(
            getattr(_cfg(), "av_int_recent_motions", []) or [], int_["motion"])
        save_settings(ext_s, int_s)

        multi = len(selected_items) > 1
        stitch = multi and bool(self.rb_stitch.IsChecked)
        crossfade = stitch and bool(self.rb_crossfade.IsChecked)

        self.settings_panel.Visibility = Visibility.Collapsed
        self.progress_panel.Visibility = Visibility.Visible
        self.thumbnail_scroll.Visibility = Visibility.Collapsed
        self.thumbnail_stack.Children.Clear()

        # Export all views now (Revit API — main thread)
        source_images = []
        view_names = []
        view_is_interior = []
        try:
            for i, item in enumerate(selected_items):
                self.progress_status_text.Text = "Exporting view {}/{}: {}...".format(
                    i + 1, len(selected_items), item.name
                )
                path = export_view_to_image(self.doc, item.view)
                source_images.append(path)
                view_names.append(item.name)
                view_is_interior.append(item.interior)
        except Exception as ex:
            self._show_error("Export failed: {}".format(ex))
            return

        worker = ThreadStart(
            lambda: self._generate_worker(
                source_images, view_names, view_is_interior,
                ext, int_, stitch, crossfade
            )
        )
        thread = Thread(worker)
        thread.IsBackground = True
        thread.Start()

    def cancel_click(self, sender, args):
        self.Close()

    def on_window_closed(self, sender, args):
        self._closed = True

    # -- Background worker ----------------------------------------------
    def _generate_worker(self, source_images, view_names, view_is_interior,
                          ext, int_, stitch, crossfade):
        try:
            total = len(source_images)
            video_paths = []

            for i, (source_image, view_name, interior) in enumerate(
                zip(source_images, view_names, view_is_interior)
            ):
                settings = int_ if interior else ext
                vtype = "INT" if interior else "EXT"
                prefix = "View {}/{} [{}] {}".format(i + 1, total, vtype, view_name)

                style = INTERIOR_STYLES[settings["style_index"]] if interior else EXTERIOR_STYLES[settings["style_index"]]
                still_prompt = build_still_prompt(style, settings["extra"])
                motion_prompt = build_motion_prompt(settings["motion"])
                image_model = settings["image_model"]
                vid_res = settings["vid_resolution"]
                vid_dur = settings["vid_duration"]
                vid_hdr = settings["vid_hdr"]

                self._set_status("{}: Generating still image... grab a coffee, back in a moment".format(prefix))
                image_bytes = call_image_api(self.gemini_key, source_image, still_prompt, image_model)
                still_path = save_still_image(image_bytes, source_image)

                # Show thumbnail in progress panel
                self._show_thumbnail(still_path, view_name)

                self._set_status("{}: Generating video with Luma Ray 3.2... have some more coffee".format(prefix))

                def on_poll(attempt, max_attempts, idx=i, name=view_name, tot=total, vt=vtype):
                    elapsed = (attempt * POLL_INTERVAL_SECONDS) // 60
                    self._set_status(
                        "View {}/{} [{}] {}: video generating (~{} min elapsed)".format(
                            idx + 1, tot, vt, name, elapsed
                        )
                    )

                video_bytes = call_luma_api(
                    self.luma_key, still_path, motion_prompt,
                    vid_res, vid_dur, vid_hdr, on_poll
                )

                self._set_status("{}: Saving clip...".format(prefix))
                video_path = save_video(video_bytes, still_path)
                video_paths.append(video_path)

            if stitch and len(video_paths) > 1:
                self._set_status("Stitching clips together with ffmpeg...")
                final_path = stitch_videos(video_paths, crossfade)
            else:
                final_path = video_paths[-1]

            self._show_preview(final_path, video_paths, stitch)

        except Exception as ex:
            self._show_error(str(ex))

    # -- Thread-safe UI updates -----------------------------------------
    def _set_status(self, text):
        try:
            self.Dispatcher.BeginInvoke(Action(lambda: self._set_status_ui(text)))
        except Exception:
            pass

    def _set_status_ui(self, text):
        if not self._closed:
            self.progress_status_text.Text = text

    def _show_error(self, message):
        try:
            self.Dispatcher.BeginInvoke(Action(lambda: self._show_error_ui(message)))
        except Exception:
            pass

    def _show_error_ui(self, message):
        if not self._closed:
            self.progress_status_text.Text = "Something went wrong:\n" + message

    def _show_thumbnail(self, image_path, view_name):
        try:
            self.Dispatcher.BeginInvoke(Action(lambda: self._show_thumbnail_ui(image_path, view_name)))
        except Exception:
            pass

    def _show_thumbnail_ui(self, image_path, view_name):
        if self._closed:
            return
        try:
            # Label
            label = TextBlock()
            label.Text = view_name
            label.FontSize = 10
            label.Foreground = SolidColorBrush(Color.FromRgb(138, 138, 148))
            label.Margin = Thickness(0, 8, 0, 2)

            # Image
            border = Border()
            border.BorderBrush = SolidColorBrush(Color.FromRgb(220, 220, 227))
            border.BorderThickness = Thickness(1)
            border.CornerRadius = System_Windows_CornerRadius(6)
            border.ClipToBounds = True
            border.Margin = Thickness(0, 0, 0, 0)
            img = System_Windows_Controls_Image()
            img.Stretch = System_Windows_Media_Stretch_Uniform()
            img.MaxHeight = 160
            img.Source = BitmapImage(Uri(image_path))
            border.Child = img

            self.thumbnail_stack.Children.Add(label)
            self.thumbnail_stack.Children.Add(border)
            self.thumbnail_scroll.Visibility = Visibility.Visible
            # Scroll to bottom so the latest thumbnail is always visible
            self.thumbnail_scroll.ScrollToBottom()
        except Exception:
            pass

    def _show_preview(self, final_path, all_paths, stitched):
        try:
            self.Dispatcher.BeginInvoke(Action(
                lambda: self._show_preview_ui(final_path, all_paths, stitched)
            ))
        except Exception:
            pass

    def _show_preview_ui(self, final_path, all_paths, stitched):
        if self._closed:
            return
        self.video_path = final_path
        self.progress_panel.Visibility = Visibility.Collapsed
        self.preview_panel.Visibility = Visibility.Visible
        if stitched:
            status = "Stitched video ({} clips):\n{}".format(len(all_paths), final_path)
        elif len(all_paths) > 1:
            status = "{} separate clips saved to:\n{}".format(
                len(all_paths), os.path.dirname(final_path)
            )
        else:
            status = final_path
        self.preview_status_text.Text = status
        try:
            self.video_player.Source = Uri(final_path)
            self.video_player.Play()
        except Exception as ex:
            self.preview_status_text.Text = "Preview unavailable ({}). File: {}".format(ex, final_path)

    # -- Preview panel handlers -----------------------------------------
    def video_ended(self, sender, args):
        from System import TimeSpan
        self.video_player.Position = TimeSpan.Zero
        self.video_player.Play()

    def video_failed(self, sender, args):
        self.preview_status_text.Text = "Preview unavailable. File:\n" + str(self.video_path)

    def open_folder_click(self, sender, args):
        if self.video_path:
            open_containing_folder(self.video_path)

    def new_render_click(self, sender, args):
        try:
            self.video_player.Stop()
        except Exception:
            pass
        # Clear thumbnails from previous run
        try:
            self.thumbnail_stack.Children.Clear()
            self.thumbnail_scroll.Visibility = Visibility.Collapsed
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
# WPF type shims (IronPython needs these resolved at runtime)
# ---------------------------------------------------------------------------
def System_Windows_Controls_Orientation_Horizontal():
    from System.Windows.Controls import Orientation
    return Orientation.Horizontal


def System_Windows_TextTrimming_CharacterEllipsis():
    from System.Windows import TextTrimming
    return TextTrimming.CharacterEllipsis


def System_Windows_Input_Cursors_Hand():
    from System.Windows.Input import Cursors
    return Cursors.Hand


def System_Windows_Controls_Image():
    from System.Windows.Controls import Image as WpfImage
    return WpfImage()


def System_Windows_Media_Stretch_Uniform():
    from System.Windows.Media import Stretch
    return Stretch.Uniform


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------
def _ascii_safe(text):
    """Sanitize user-entered text to ASCII before sending to the API.
    IronPython 2 uses Python 2 string semantics - mixing unicode strings
    containing non-ASCII chars with byte strings (like base64 output) causes
    implicit ASCII decode failures. Smart quotes, em dashes etc. pasted from
    Word are the most common source of this in user-entered prompts."""
    if not text:
        return ""
    replacements = {
        u"\u2014": "-",   # em dash
        u"\u2013": "-",   # en dash
        u"\u2018": "'",   # left single quote
        u"\u2019": "'",   # right single quote (the current culprit)
        u"\u201c": '"',   # left double quote
        u"\u201d": '"',   # right double quote
        u"\u2026": "...", # ellipsis
        u"\u00e9": "e",   # e acute
        u"\u00e8": "e",   # e grave
        u"\u00ea": "e",   # e circumflex
        u"\u00e0": "a",   # a grave
        u"\u00e2": "a",   # a circumflex
        u"\u00f4": "o",   # o circumflex
        u"\u00fb": "u",   # u circumflex
        u"\u00fc": "u",   # u umlaut
        u"\u00e4": "a",   # a umlaut
        u"\u00f6": "o",   # o umlaut
        u"\u00df": "ss",  # sharp s
        u"\u00e7": "c",   # c cedilla
        u"\u00f1": "n",   # n tilde
        u"\u2122": "(TM)", # trademark
        u"\u00ae": "(R)",  # registered
        u"\u00a9": "(C)",  # copyright
        u"\u00b0": " degrees", # degree symbol
        u"\u2605": "*",   # star
        u"\u2022": "-",   # bullet
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    # Final pass: strip any remaining non-ASCII chars rather than crashing
    return text.encode("ascii", errors="ignore").decode("ascii")


def build_still_prompt(style, extra):
    return (
        "Render this architectural 3D view as a {style} image. "
        "Preserve the exact geometry, proportions, and camera framing shown "
        "in the source image, only change materials, lighting, and "
        "atmosphere. {extra}"
    ).format(style=style[0].lower() + style[1:], extra=_ascii_safe(extra))


def build_motion_prompt(motion):
    motion = _ascii_safe(motion)
    return motion.strip() or (
        "Slow, cinematic camera movement exploring the architecture. "
        "Gentle natural environmental motion. Architecture stays the "
        "clear focus throughout. No dialogue."
    )


# ---------------------------------------------------------------------------
# Revit-side helpers
# ---------------------------------------------------------------------------
def get_gemini_key():
    key = os.environ.get(GEMINI_API_KEY_ENV)
    if not key:
        forms.alert(
            "No Gemini API key found.\n\nSet '{}' as a Windows environment "
            "variable and restart Revit.".format(GEMINI_API_KEY_ENV),
            exitscript=True,
        )
    return key


def get_luma_key():
    key = os.environ.get(LUMA_API_KEY_ENV)
    if not key:
        forms.alert(
            "No Luma API key found.\n\nGet one at https://platform.lumalabs.ai/, "
            "set '{}' as a Windows environment variable, and restart "
            "Revit.".format(LUMA_API_KEY_ENV),
            exitscript=True,
        )
    return key


def export_view_to_image(doc, view):
    if not os.path.exists(EXPORT_FOLDER):
        os.makedirs(EXPORT_FOLDER)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    basename = "AIArchVideoSource_{}".format(stamp)
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
    # Find the produced file by our timestamp prefix - never use GetFileName
    # since it returns a path based on the view ID and can return the same
    # cached path for different views when called in quick succession.
    candidates = sorted(
        [f for f in os.listdir(EXPORT_FOLDER) if f.startswith(basename)],
        reverse=True,
    )
    if candidates:
        return os.path.join(EXPORT_FOLDER, candidates[0])
    # Last resort - check the direct path
    if os.path.exists(filepath + ".png"):
        return filepath + ".png"
    raise RuntimeError(
        "Could not find the exported image for view '{}'. "
        "Expected a file starting with '{}' in '{}'.".format(
            _element_name_safe(view), basename, EXPORT_FOLDER
        )
    )


def open_containing_folder(file_path):
    try:
        Process.Start("explorer.exe", '/select,"{}"'.format(file_path))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
class _TimeoutWebClient(WebClient):
    def GetWebRequest(self, address):
        request = WebClient.GetWebRequest(self, address)
        request.Timeout = 300000
        request.KeepAlive = False
        return request


def _upload_with_retry(url, json_body, auth_header=""):
    last_error = None
    for attempt in range(UPLOAD_MAX_ATTEMPTS):
        client = _TimeoutWebClient()
        client.Encoding = Encoding.UTF8
        client.Headers.Add("Content-Type", "application/json")
        if auth_header:
            client.Headers.Add("Authorization", auth_header)
        try:
            return client.UploadString(url, "POST", json_body)
        except WebException as ex:
            last_error = ex
            if attempt < UPLOAD_MAX_ATTEMPTS - 1:
                time.sleep(UPLOAD_RETRY_DELAY_SECONDS)
    details = ""
    if last_error and last_error.Response:
        reader = StreamReader(last_error.Response.GetResponseStream())
        details = reader.ReadToEnd()
    raise RuntimeError("Request failed: {} {}".format(last_error.Message, details[:500]))


def _get_json(url, auth_header):
    client = _TimeoutWebClient()
    if auth_header:
        client.Headers.Add("Authorization", auth_header)
    try:
        return json.loads(client.DownloadString(url))
    except WebException as ex:
        details = ""
        if ex.Response:
            reader = StreamReader(ex.Response.GetResponseStream())
            details = reader.ReadToEnd()
        raise RuntimeError("GET failed: {} {}".format(ex.Message, details[:500]))


def _download_bytes(url):
    client = _TimeoutWebClient()
    try:
        return client.DownloadData(url)
    except WebException as ex:
        raise RuntimeError("Download failed: {}".format(ex.Message))


# ---------------------------------------------------------------------------
# Gemini still-image generation
# ---------------------------------------------------------------------------
def call_image_api(api_key, image_path, prompt, model_id):
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            ]
        }]
    }
    url = "{}?key={}".format(GEMINI_IMAGE_ENDPOINT.format(model=model_id), api_key)
    response_text = _upload_with_retry(url, json.dumps(body, ensure_ascii=True))
    data = json.loads(response_text)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise RuntimeError("No image returned. Response: {}".format(response_text[:500]))


def save_still_image(image_bytes, source_path):
    result_path = source_path.replace("AIArchVideoSource", "AIArchVideoStill")
    if not result_path.lower().endswith(".png"):
        result_path += ".png"
    with open(result_path, "wb") as f:
        f.write(image_bytes)
    return result_path


# ---------------------------------------------------------------------------
# Luma Ray 3.2 video generation
# ---------------------------------------------------------------------------
def call_luma_api(luma_key, still_image_path, motion_prompt,
                  resolution, duration, hdr, progress_callback=None):
    with open(still_image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")
    body = {
        "model": "ray-3.2",
        "type": "video",
        "prompt": motion_prompt,
        "aspect_ratio": "16:9",
        "video": {
            "resolution": resolution,
            "duration": duration,
            "hdr": hdr,
            "keyframes": [{"data": image_b64, "media_type": "image/png"}],
            "keyframe_indexes": [0],
        },
    }
    auth = "Bearer {}".format(luma_key)
    response_text = _upload_with_retry(LUMA_SUBMIT_ENDPOINT, json.dumps(body, ensure_ascii=True), auth)
    submit_data = json.loads(response_text)
    generation_id = submit_data.get("id")
    if not generation_id:
        raise RuntimeError("Luma API returned no generation ID. Response: {}".format(response_text[:500]))

    poll_url = LUMA_POLL_ENDPOINT.format(id=generation_id)
    time.sleep(INITIAL_POLL_WAIT_SECONDS)

    for attempt in range(MAX_POLL_ATTEMPTS):
        if progress_callback:
            progress_callback(attempt, MAX_POLL_ATTEMPTS)
        status_data = _get_json(poll_url, auth)
        state = status_data.get("state", "")
        if state == "completed":
            outputs = status_data.get("output", [])
            if not outputs:
                raise RuntimeError("Generation completed but no output returned.")
            video_url = outputs[0].get("url")
            if not video_url:
                raise RuntimeError("Generation completed but no video URL in output.")
            return _download_bytes(video_url)
        if state == "failed":
            reason = status_data.get("failure_reason") or "unknown"
            raise RuntimeError("Luma generation failed: {}".format(reason))
        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        "Video generation timed out after ~{} minutes. Check "
        "https://platform.lumalabs.ai/ for status.".format(
            (INITIAL_POLL_WAIT_SECONDS + MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS) // 60
        )
    )


def save_video(video_bytes, still_path):
    video_path = still_path.replace("AIArchVideoStill", "AIArchVideo")
    if video_path.lower().endswith(".png"):
        video_path = video_path[:-4] + ".mp4"
    else:
        video_path += ".mp4"
    DotNetFile.WriteAllBytes(video_path, video_bytes)
    return video_path


def stitch_videos(video_paths, crossfade=False):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = os.path.join(EXPORT_FOLDER, "AIArchVideo_stitched_{}.mp4".format(stamp))
    # ffmpeg prefers forward slashes on Windows - backslashes can cause -22 EINVAL
    output_path_ff = output_path.replace("\\", "/")

    if not crossfade:
        list_path = os.path.join(EXPORT_FOLDER, "concat_{}.txt".format(stamp))
        list_path_ff = list_path.replace("\\", "/")
        import io as _io
        with _io.open(list_path, "w", encoding="utf-8") as f:
            for p in video_paths:
                f.write("file '{}'\n".format(p.replace("\\", "/")))
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", list_path_ff, "-c", "copy", output_path_ff]
    else:
        n = len(video_paths)
        fade = 0.5
        clip_secs = 5
        inputs = []
        for p in video_paths:
            inputs += ["-i", p]
        parts = []
        prev = "[0:v]"
        for i in range(1, n):
            label = "[v{}]".format(i) if i < n - 1 else "[vout]"
            parts.append("{}[{}:v]xfade=transition=fade:duration={}:offset={}{}".format(
                prev, i, fade, clip_secs * i - fade * i, label
            ))
            prev = label
        prev_a = "[0:a]"
        for i in range(1, n):
            label = "[a{}]".format(i) if i < n - 1 else "[aout]"
            parts.append("{}[{}:a]acrossfade=d={}{}".format(prev_a, i, fade, label))
            prev_a = label
        cmd = (["ffmpeg"] + inputs + ["-y", "-filter_complex", ";".join(parts),
                                       "-map", "[vout]", "-map", "[aout]", output_path_ff])

    result = subprocess.call(cmd, stderr=subprocess.PIPE)
    if result != 0:
        raise RuntimeError(
            "ffmpeg stitching failed (exit {}). "
            "Clips are still saved individually in {}.".format(result, EXPORT_FOLDER)
        )
    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    doc = revit.doc
    gemini_key = get_gemini_key()
    luma_key = get_luma_key()
    active_view = revit.active_view if isinstance(revit.active_view, DB.View3D) else None
    all_views = get_all_3d_views(doc)
    if not all_views:
        forms.alert("No 3D views found in this project.", exitscript=True)

    xaml_path = script.get_bundle_file("ui.xaml")
    prompt_form = PromptForm(
        xaml_path, doc, all_views, active_view, gemini_key, luma_key
    )
    prompt_form.ShowDialog()


if __name__ == "__main__":
    main()
