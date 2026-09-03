# Field reconstruction and opacity, 2026-09-03

The goal is scientific inspection: see structures, distinguish the disk,
remnant and outflows, and follow their formation through snapshots. The point
cloud already supports useful exploration. A Voronoi replacement needs to
retain that usefulness while supplying the actual cell geometry and fields.
Reduced grain or a polished movie is not sufficient evidence of improvement.

The snapshot 721 geometry audit passed. This change targets field reconstruction,
foreground opacity and the controls needed for a fair structural comparison,
keeping the native generators, neighbour planes and input values intact.

## Reconstruction and transfer

The prototype adds a continuous blend of local, limited linear polynomials.
It reuses the Metal gradient prepass and the existing native-neighbour search;
eight nearby sites receive positive compact weights, with the ninth setting
the zero-weight support boundary. Predictions from the local gradients are
blended before clipping to the full input-field range. The clipping bounds
are fixed for a field upload, so a change of interpolation support cannot
change them. This removes the old per-cell reconstruction seam and the flat
slopes of plain inverse-distance interpolation. Local extrema can still be
softened, and the result is a display interpolation rather than a conservative
hydrodynamic reconstruction or exact Sibson natural-neighbour interpolation.

The method follows the modified-Shepard idea of blending local approximants;
see [SHEPPACK, Algorithm 905](https://doi.org/10.1145/1824801.1824812).
The implementation uses the existing native geometry search and custom
limited gradients. It is not a port of AREPO-VTK's natural-neighbour code.

A separate transfer setting uploads scaled physical channel values and gas
density. Colour transforms, density powers and the density visibility taper
are then applied at each interpolated ray sample. This differs from fitting
already transformed colours and extinction coefficients. Colour limits can
either hide outside-range values (legacy behaviour) or clamp their palette
colour while preserving the gas's optical contribution. Physical coefficients
still integrate along actual native-cell chord lengths.

Existing modes and transfer order remain available for matched comparisons.
The optional dense-gas fade reduces the computed extinction over one decade
in density to the selected fraction. It leaves the density values unchanged;
it is an explicit visibility choice, not a classifier or calibrated radiative
model. The fade is disabled by default.

All rendering uses cached, checksum-bound prepared scenes on the M4. No raw
snapshot reads or new scientific fields are required by this prototype.

## Scientific inspection in the GUI

The fresh browser chooses **Linear field · fast** with four antialiased rays
per pixel and field-before-transfer mapping. **Continuous field · slower** is
an explicit choice. Camera interaction uses a smaller linear preview and then
restores the chosen reconstruction. Existing saved profiles retain their
recorded reconstruction, transfer order and colour-range behaviour.

**Compare with point cloud** switches at the same snapshot, camera and colour
settings. Returning reuses the last decoded native image only if the full
request key and image source still match. Camera, opacity, field, snapshot or
resolution changes prevent reuse. The point renderer has different selection
and opacity rules: it uses sampled round markers, while native volumes include
the full mesh and their own density visibility settings. This is a useful
structural reference, not a claim of identical projected intensities.

**Keep view on snapshot change** preserves the latest physical viewport,
selected field, fixed colour limits, opacity, reconstruction and native-view
settings across manual snapshot loads. The new scene's centre and radius only
convert the same physical viewport into its local display coordinates. The
new snapshot/time/hash remain the binding; an unavailable field or changed
units prevents restoration and produces a visible notice. The lock is enabled
initially, can be disabled, and does not override explicit saved-pose navigation.

These controls address inspection friction. They do not yet establish that
the native view reveals evolving structures better than the point cloud.

## Verification and retained captures

All 61 software checks passed on M4 Pro in 8.082 s. They include constant/affine
fields, continuity across faces and periodic boundaries, original generator
values, full-field bounds, field-before-transfer order, signed/log colours,
dense-gas fading, optical path integration and worker shutdown. GUI controller
checks cover unchanged-frame comparison, cache invalidation, preservation of
physical coordinates across two synthetic snapshots with different centres
and radii, field/unit/hash guards, failed loads, saved settings, fit refresh
and measurements. They use an in-memory DOM/transport. Browser visual QA is
still blocked by an admin-policy verification failure.

Nine initial matched frames use snapshot 721 at 960 by 640, one ray per pixel
and two cell samples. Physical-field linear GPU times were 0.108 s (disk),
0.114 s (remnant) and 0.055 s (outflow); the continuous blend took 3.618, 4.324
and 1.826 s. All frames reported zero traversal failures and gradient fallbacks.
Cameras and display-transfer parameters were fixed. The three modes separate
transformed-field linear, physical-field linear and continuous physical-field
reconstruction.

The seven final images use 1440 by 960, four deterministic rays per pixel and
two samples per crossed cell. Each baseline/continuous pair keeps the same
scene, camera, field, colour limits, density support and opacity parameters.
The pair changes both the reconstruction and transfer order; the intermediate
physical-linear probe separates those effects.

| View | Previous linear GPU time | Continuous physical-field GPU time |
| --- | ---: | ---: |
| Disk | 0.631 s | 24.218 s |
| Remnant | 0.762 s | 28.898 s |
| Outflow | 0.289 s | 10.986 s |

The additional continuous disk view with dense-gas fading took 27.022 s.
GPU times exclude initial loading and CPU/image delivery. The continuous
cost is too high to assume an improvement in interactive exploration.

Visual inspection shows softer outer cell boundaries but only a modest overall
improvement with interpolation alone. The dense foreground still dominates
the disk view. A separate opacity comparison used a fade starting at 1e4
g cm^-3, reaching a fraction of 0.08 at 1e5 g cm^-3, with colour-range clamping;
this exposed more interior contrast. Remnant/outflow opacity variants became fainter without a clear gain
in structure, so their standard profiles are retained. A density-coloured
alternative made the surrounding disk too dark and is retained as a rejected
variant. All seven full-size images have been visually inspected. Dense
foreground saturation and lost interior contrast remain scientific usability
problems, independent of movie polish.

The package is `~/Movies/AREPO/voronoi-m4-preview-20260903-v005`. It retains all
comparisons, rejected variants, configs, reports, checks and the exact capture
source at commit `0806c29a4bbd8fe190c703f691b2338d261b111d`. The subsequent GUI
and default changes leave the numerical renderer used for these images unchanged.

The next acceptance comparison must identify the same structures in points,
original-cell values and reconstructed fields across neighbouring snapshots,
with fixed physical camera, field definition, colour limits and stated
visibility settings. It must check that interpolation preserves boundaries
and contrast, and that foreground opacity does not make a forming component
disappear. A matched AREPO-VTK reference reconstruction and a real temporal
comparison remain outstanding; these single-epoch captures do not replace them.
