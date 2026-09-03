# Field reconstruction and opacity, 2026-09-03

The snapshot 721 geometry audit passed. This change targets how fields become
an image, keeping the native generators, neighbour planes, cameras and input
values intact.

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
This is initially a prototype: analytic affine-field, face-continuity, nonlinear
transfer-order, periodicity and optical-integration checks precede matched
real-scene captures. A first compatibility run passed all numerical checks;
its worker-protocol assertion expected the previous backend version string.
That assertion was updated for v004. No image-quality success is established
by that compatibility run.

All rendering uses cached, checksum-bound prepared scenes on the M4. No raw
snapshot reads or new scientific fields are required by this prototype.

## First real-scene comparisons

The 18 focused M4 checks passed after correcting a variable-name collision in
an expanded synthetic lattice fixture. They cover the new continuous mode on
constant/affine fields, face and periodic continuity, original generator
values, full-field bounds, field-before-transfer order, signed/log colours,
dense-gas fading, and the existing optical/path and worker-shutdown checks.
The controller check also passes the fast-to-continuous handoff, saved new
settings, legacy restoration, stale responses, fit refresh, and measurements.

Nine initial matched frames use snapshot 721 at 960 by 640, one ray per pixel
and two cell samples. Physical-field linear GPU times were 0.108 s (disk),
0.114 s (remnant) and 0.055 s (outflow); the continuous blend took 3.618, 4.324
and 1.826 s. All frames reported zero traversal failures and gradient fallbacks.
The field transfer and camera were held fixed within each comparison.

Visual inspection shows softer outer cell boundaries but only a modest overall
improvement with interpolation alone. The dense foreground still dominates
the disk view. A separate opacity comparison used a fade starting at 1e4
g cm^-3, reaching a fraction of 0.08 at 1e5 g cm^-3; this exposed more interior
contrast. Remnant/outflow opacity variants became fainter without a clear gain
in structure, so their standard profiles are retained. A density-coloured
alternative made the surrounding disk too dark and is retained as a rejected
variant. These are preview-quality changes, not an accepted production movie.

The fresh browser chooses the continuous physical-field mode at Standard
quality. Camera interaction uses a fast linear preview, preserving the same
field/opacity order, then requests the selected continuous result. High quality
is slower and remains explicit. Existing saved profiles restore their old
transfer order, colour-range masking, and reconstruction; new settings are
serialized. The optional dense fade is disabled by default.
