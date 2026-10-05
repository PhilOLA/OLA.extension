# Elevation Crop Fixer (pyRevit pushbutton)

Batch-fixes interior elevation crop regions for a chosen set of rooms.
Drop the `ElevationCropFixer.pushbutton` folder into your pyRevit
extension (next to your other tools, e.g. in the same `.tab\.panel` as
Model Health Checker / Export Package Builder) and reload pyRevit.

## What it does

For every interior elevation view belonging to each selected room:

1. **Side walls** - finds the two walls bounding the elevation's left
   and right crop edges, and extends the crop past each wall's *far*
   face (the face farther from the room's centre point) by your Wall
   offset (default 75mm). The far face is found by measuring the
   wall's actual solid geometry, not by trusting Revit's
   interior/exterior flip flag - so it still works correctly on
   partitions where that flag is backwards.
2. **Ceiling** - finds the highest ceiling associated with the room and
   extends the crop's top edge above the ceiling's top face by your
   Ceiling offset (default 100mm). If no ceiling is found, the top
   edge is left as Revit's default and this is noted in the report.
3. **Levels** - for each Level visible in the view, hides its
   right-hand bubble and extends its right end to sit your Datum
   extension (default 100mm) past the new right crop edge. The left
   end is left untouched.
4. **Grids** - for each Grid visible in the view, extends both the top
   (bubble) end and bottom end the same Datum extension distance past
   the new top/bottom crop edges.
5. **Skip already-modified crops** - on, by default. The tool stamps
   an Extensible Storage marker on every view it adjusts. On a later
   run: if the marker matches the current crop, the view is untouched
   history and gets reprocessed normally; if the crop has since moved
   (someone manually resized it), the view is skipped and reported. A
   view the tool has never touched is skipped only if its current crop
   is already noticeably wider than the room's raw wall-to-wall span
   (a heuristic - a genuinely default, freshly-placed crop won't
   trigger this).

Any room side where more than one wall could plausibly be "the" side
wall (an L-shaped room, for instance) is **flagged, not guessed** -
it's left alone and reported so you can handle it by hand.

## Known limitations / things to test first

- Written against the Revit 2022+ API (`Room.IsPointInRoom`,
  `DatumPlane.GetCurvesInView` / `SetCurveInView` /
  `SetDatumExtentType` / `HideBubbleInView`). Not yet run inside Revit
  - please try it on a throwaway/test model first.
- Side-wall detection assumes the elevation looks roughly
  perpendicular at a wall with two side walls running back into the
  room parallel to the view direction (the normal case for a
  room-generated interior elevation). Curved walls or very irregular
  rooms may need the manual fallback.
- Ceiling detection uses each ceiling's plan bounding box plus a
  point-in-room test at the room's floor level +100mm - sloped or
  multi-tier ceilings use the highest point found this way.
- "Active view" / "Active level" room scope depends on having a plan
  or similar view active; if nothing sensible is active, use "Entire
  model" and the search box to narrow down.

## Suggested next steps once tested

- Wire up a keyboard shortcut / add to your existing toolbar panel.
- If you want this to also run automatically the moment a new
  elevation marker is placed (rather than only as a manual batch
  tool), that needs a separate pyRevit event hook - happy to build
  that as a follow-on once this version is confirmed working the way
  you want.
