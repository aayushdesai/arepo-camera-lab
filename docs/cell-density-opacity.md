# Density opacity in the native cell view

Implemented and checked on 2026-09-03. The existing VTK face view assigns one opacity to
every polygon and normally emits only the boundary of selected cells. Making
that shell transparent cannot reveal omitted interior faces. VTK's
[depth peeling](https://vtk.org/doc/release/7.0/html/classvtkDepthPeelingPass.html)
uses repeated passes through translucent geometry. The density mode instead
uses the existing ray traversal to include interior cells without constructing
and sorting all of their polygons.

The new density mode uses the existing Metal native-cell traversal. Fields
remain constant within each original cell. Its opacity uses gas density and
the actual ray chord length, including every interior cell along the ray until
the view becomes opaque. No interpolation, voxel grid, retessellation, or
density-to-cell-size assumption is introduced.

Native edges are computed from intersections of the stored neighbour planes.
Their width is measured in screen pixels. Outlines decorate each cell's colour
contribution and inherit its density/path opacity; they do not form a second,
opaque wire cage. Rear edges are attenuated through their own cell. The VTK
surface mode remains available for ordinary face lighting and 3D surface picks.
Density mode also supports headlight shading of each cell's entry face.
Edges fade in when the native inscribed-cell diameter spans two to five pixels;
cell colour and density opacity remain unchanged by this edge-resolution rule.
This suppresses a dense wire texture when cells cannot be resolved individually.

The transfer is a display choice, not a calibrated radiative-opacity model:
`alpha = 1 - exp(-tau)` and
`tau = -log(1-opacity) * (rho/rho_reference)^power * chord/reference_length`,
with the existing density floor transition and optional dense-gas fade.
Native chord lengths keep uniform-medium opacity unchanged under refinement.

## Zoom and inspection

The point renderer uses a fixed pixel diameter and fixed alpha per marker.
Zooming spreads those markers apart, lowering their coverage of foreground
pixels. Filled polygons retain coverage at every zoom. The new optional
**Reveal on zoom** control multiplies foreground display optical depth by
`min(1, (current_screen_half_extent_cm/reference_half_extent_cm)^2)`.
Zooming in by two therefore reduces far-foreground optical depth by four. Zooming back out
restores it, capped at the user-set baseline. Density and colour data are
unchanged. This is explicitly an inspection aid, not a physical opacity law.

The fade applies in front of the look-at plane: full density opacity is retained
from minus one screen half-extent onward, with a smooth transition to the zoom
factor at minus two half-extents. The rest of the view and the cells at the
focus retain full opacity. The smoothstep integral is evaluated analytically
over every native chord, preserving consistency under cell subdivision. A
first prototype faded the entire column and dimmed the interior as well; its
images/source remain in `comparison-v001` as a rejected global-fade experiment.

The reference is a saved physical length, so keeping the physical viewport
between snapshots preserves the same zoom factor even if their scene radii
differ. **Fit visible mesh** resets the reference to the fitted overview.
Unchecking the option restores fixed density/path opacity. Zoom only changes a
render uniform; it does not rebuild geometry or re-upload all physical fields.

Density cells use projected rulers because several depths can contribute to a
pixel. The VTK surface option retains 3D surface picks; this avoids inventing
a unique interior surface behind a translucent image.

## Use and verification

Select **Native cells · faces and edges**, then **Density through cells ·
Metal**. Enable **Show cell edges** to see resolved boundaries. **Reveal on
zoom** starts enabled in new views; older saved surface presets retain their
VTK mode. Switching to density mode initially keeps opacity outside the colour
range, so changing the colour limits does not silently remove foreground or
interior material. The density threshold remains an explicit visibility cut.
Use **Diffuse outflow** to include lower-density cells. **Light cell faces**
is optional. Restart a running server after updating the code.

Verification used synthetic meshes and the existing checksum-bound snapshot
721 cache on an Apple M4 Pro. No raw snapshot was read or new scientific field
derived for this change.

- All 66 Python checks passed, including native Metal rendering. Analytic
  fixtures verify density/path opacity, native edge positions, unchanged alpha
  when edges are enabled, foreground attenuation with preserved focus opacity,
  and the integrated zoom fade under unequal cell subdivision.
- All three JavaScript controller checks passed, plus syntax checks of the
  assembled viewer and server shell. They cover density/uniform switching,
  saved styles, physical zoom-reference carry between snapshots, original-cell
  rendering, projected rulers, and existing point comparison and frame guards.
- Seven 960 by 640 captures compare the VTK shell with density cells, fixed
  opacity with foreground reveal at 16x and 64x zoom, and an outflow overview.
  They use the same snapshot, cameras, field limits and density transfers
  within each comparison. Every native ray finished without traversal errors.
  The six density-cell frames took 0.23–0.51 seconds of GPU time each, using
  four rays per pixel. This excludes mesh loading, field setup, and annotation.

At overview scale, many cells are smaller than a pixel and their outlines are
suppressed. At high zoom the native edges are resolved; several translucent
layers can still overlap. The matched zoom frames show changed interior colour
and edge contributions, rather than a new reconstruction of the field. These
are rendering checks, not validation of an identified astrophysical structure.
Browser automation was unavailable, so interactive browser behavior has not
been visually checked; the controls were exercised by the JavaScript harness.

The no-clobber preview package records frame configurations, source archives,
timings and hashes. It retains `comparison-v001` (rejected global fade) and
`comparison-v002` (foreground fade) alongside the release captures. The
[capture example](../examples/native-cell-opacity.example.json) shows the
portable configuration; set `zoom_opacity.enabled` to true and retain the same
physical reference length to compare zoom levels.
