# AI Walkthrough (pyRevit pushbutton)

Renders the active 3D view with AI (same still-image pipeline as AI Render),
then animates that still into a short video using Veo 3.1's image-to-video
generation.

## Install

1. Copy this `AIWalkthrough.pushbutton` folder (all files: `bundle.yaml`,
   `script.py`, `ui.xaml`) alongside `AIRender.pushbutton`, e.g.:
   `...\MyToolsExtension.extension\MyTools.tab\AI.panel\AIWalkthrough.pushbutton`
2. In pyRevit, click **Reload**.

## Configure

No extra setup if AI Render is already configured — this uses the same
`GEMINI_API_KEY` environment variable. Video generation just needs to be
enabled on your Google AI Studio project the same way image generation was
(billing enabled; there's no free tier for video at all, not even a
zero-quota trip-up like images had).

## Use

1. Open/activate a 3D view.
2. Click **AI Walkthrough**.
3. Pick a style for the still render, optionally add extra direction
   (materials, mood, context — same as AI Render), an image quality tier,
   and a video quality tier. Optionally describe the camera/environment
   motion you want (leave blank for a sensible default: slow, subtle
   exploration of the space). Both text fields have their own "recent
   prompts" dropdown, same as AI Render.
4. Click Generate. The same window switches to a progress view — this
   takes noticeably longer than AI Render alone, since video generation
   typically adds 1–4 minutes on top of the still image step, sometimes
   longer at peak times (Google's documented max is 6 minutes).
5. When done, that same window switches again to a preview view: the
   video plays inline (auto-looping), with **Open Folder**, **New
   Render** (goes back to the settings view without closing the window,
   so you can try another prompt), and **Close** buttons.

## Why the video can't go into Revit

Revit has no video element type — nothing to place on a sheet or in a
view. This tool saves the result as a plain `.mp4` file in the same
`Documents\AIRender` folder AI Render uses, alongside the still image it was
generated from, and shows it in an in-app preview panel (WPF's
`MediaElement` control) within the same window you started from — no
separate popup. From there, use the file like any other video — attach it
to an email, drop it into a presentation, etc.

`MediaElement` relies on Windows Media Foundation components normally
present on any standard Windows install. If they're missing (rare, but
possible on a locked-down corporate image), the preview window shows a
plain message with the file path instead of erroring out — the video file
itself is unaffected either way.

## Cost — read this before generating a lot of these

Video generation is meaningfully more expensive than stills:

| Video quality | Approx. cost per 8s clip |
|---|---|
| Lite | ~$0.40 |
| Fast | ~$1.20 |
| Standard | ~$3.20 |

There's no free tier at all for video on the Gemini API, unlike images.
Budget accordingly, especially if testing several motion prompts on the
same view.

## Things you'll likely want to adjust

- **`VIDEO_RESOLUTION`** (currently `"720p"`) — raise to `"1080p"` or
  `"4k"` for higher quality at higher cost. 4K forces 8-second duration
  and isn't available on the Lite tier.
- **`VIDEO_DURATION_SECONDS`** (currently `"8"`, the maximum per
  generation) — Veo also accepts `"4"` or `"6"` for shorter/cheaper clips.
- **`MAX_POLL_ATTEMPTS`** / **`POLL_INTERVAL_SECONDS`** — control how long
  the script waits before giving up on a slow generation (~6-7 minutes by
  default, matching Google's documented worst case).
- **Default motion prompt** in `ask_for_settings()` — used whenever the
  motion field is left blank; adjust the wording to match the kind of
  movement you generally want.

## Known interop quirks (same as AI Render, reused here)

- IronPython2 engine (not CPython3) for WPF support.
- `System.Net.WebClient` instead of `requests`, with `KeepAlive = False`
  and `Expect100Continue = False` to avoid connection issues on larger
  payloads.
- `ImageExportOptions.GetFileName()` is a static method, not an instance
  call.

## Threading (new in this version)

Settings, progress, and video preview all live in one window (three
panels toggled by visibility) rather than separate popups. Since
generation takes minutes, the actual API calls run on a background
`System.Threading.Thread`, with UI updates marshaled back via
`Dispatcher.BeginInvoke`. The one Revit API touch — exporting the active
view — happens synchronously on the main thread *before* the background
thread starts, since Revit API calls aren't safe to make off the main
thread. This is the first threaded code in this build; if the window
seems to hang or fails to update after clicking Generate, that's the
first place to look.
