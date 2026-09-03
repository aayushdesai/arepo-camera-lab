# Native 3D Voronoi viewer

The feature branch provides a native-cell **volume view on Metal**, an explicit
**face view on VTK**, and the existing point previews. Complete v052 scene and
field-sidecar pairs remain the inputs. Direct raw-HDF5 loading and a volume
movie-export command are not implemented.

## Geometry and fields

Both native views read complete AREPO-VTK cell records, neighbour offsets, and
native neighbour displacement vectors. Each displacement defines a Voronoi
bisector. The VTK adapter intersects these planes to recover convex cell faces;
the Metal adapter intersects rays with the same planes and follows their native
neighbour indices. Neither rebuilds a tessellation from sampled points, creates
a voxel grid, substitutes a slice, or blurs the rendered image.

The v052 displacement vectors and physical fields are float32. The face builder
intersects planes in double precision. The Metal renderer converts positions
to camera-lab scene coordinates in double precision before storing float32 GPU
positions. Rays originate in the focus plane and use compensated distance
accumulation to retain short steps after crossing the much larger simulation
box. This reconstructs the exported geometry; it is not a bit-identical recovery
of the original AREPO double-precision vertices.

The [snapshot 721 fidelity audit](mesh-fidelity-audit.md) independently matches
all exported generators to the raw snapshot and compares 848 reconstructed
cell volumes with M/rho. It verifies geometry at that epoch; the Metal field
reconstruction and image appearance have separate validation requirements.

Periodic generator positions and ghost-neighbour planes remain available.
The Metal locator indexes every generator and starts each ray at the periodic
display-box entrance. All traversed cells are available, including ones that
make no visible contribution. The **Original cell values** mode holds each
exported field constant within its native cell.

The optional **Continuous field · slower** mode blends the local linear
predictions described below with compact spatial weights. Original generator
values are retained; the blend stays continuous across the old cell boundaries
and is clipped to the full uploaded field range. Its slopes are limited locally,
but its final values are not guaranteed to stay within each cell's immediate
neighbour range. It is a display reconstruction, not a conservative AREPO
reconstruction. The [field-reconstruction notes](field-reconstruction-work.md)
describe the algorithm and validation.

The fresh-viewer default **Linear field · fast** uses inverse-distance-squared weighted
least-squares gradients from the stored native neighbour displacements. It fits
the physical channel and gas density with fresh settings, or transformed colour
and display extinction when the legacy transfer order is selected. A slope
limiter constrains extrapolation towards neighbour generators; each sampled
value is also bounded by the parent and its neighbours. Degenerate or
unsupported fits use a zero gradient and are counted in the render report.
Undefined parent colour stays hidden. Periodic displacements come from the
native graph, and no density-to-cell-size assumption enters the fit.

Gradients are computed in a Metal pass when the transfer changes and remain on
the GPU across camera changes. This removes the flat plateaus around generators
introduced by the old distance-weighted interpolator. A synthetic smooth affine
field is reproduced to float32 accuracy in a region where the limiter is inactive.
The field can still jump between independently limited cells; it is not strictly
continuous, nor is it AREPO's own gradient or a conservative mass reconstruction.

The selectable **Legacy smoothing** mode interpolates the uploaded fields
(physical values or legacy transfer coefficients) using a compact Shepard
kernel. Up to eight nearby generators have positive weights; the ninth nearest
generator sets the zero-weight support radius. Distances use periodic minimum
images, and neighbours are deduplicated by native cell index. A best-first walk
along the native Voronoi graph replaces a global tree search at each sample.
The global locator is used only to enter the mesh or sample a diagnostic point.

For distances d_i, nearest distance d_min, and support radius h, weights are
`(d_min / d_i - d_min / h)^2`, normalized by their sum. At a generator, the
original value is returned. The kernel has nonnegative weights and does not
overshoot the participating values. Sites enter and leave its support with zero
weight, avoiding the ordinary nearest-cell jumps. Non-emitting or undefined
colour samples do not contribute to colour interpolation; their zero extinction
still participates in the transparency interpolation.

These are display interpolations, not AREPO's own gradient reconstruction, exact
Sibson natural-neighbour interpolation, or a conservative reconstruction of gas
mass. None is guaranteed to preserve a sharp shock. Original cell values remain
available, and older saved volume presets keep that original mode. A smoother
image is not evidence of better physical resolution. Cell sizes must be
interpreted using the particular simulation's actual refinement/derefinement
rules, not an assumed uniform mass per cell or a density-only size law. See the
[checked refinement context for these two scenes](run-refinement-context.md).

Continuous reconstruction uses two ordered quadrature samples per crossed
cell. Standard quality uses one deterministic ray per pixel; high quality uses
four. Fast interaction uses linear reconstruction on a smaller frame with one
ray and one cell sample, then restores the selected reconstruction. No
time-varying sampling noise or post-render image blur is introduced.

Gas density is the exported mass density in g cm^-3, decoded as
`10**(stored_density - 10)` for these cgs v052 scenes. The previous "density
proxy" label was incorrect; density-based display opacity is a separate choice.

Fields use the existing 24-channel implementation and restricted formula
language. Sidecars join by uint64 particle ID. Non-finite formula values are
hidden. The point budget never subsamples the native mesh. Face picks preserve
the native cell index and serialize particle IDs as strings for JavaScript.

## Volume transparency

Fresh viewers upload the physical channel and density, scaled to float32
ranges. The chosen interpolation precedes the colour transform, density power,
and visibility taper. `transfer_stage: before_reconstruction` preserves the
legacy transfer-coefficient interpolation; saved states missing the setting
retain that behaviour. `range_behavior: clamp` retains gas outside the colour
range and clamps the palette; `hide` preserves the original range filter.

An optional `dense_fade_start` reduces opacity smoothly over the next density
decade to `dense_opacity_fraction` times the otherwise chosen coefficient. The
default start is 0 (disabled), and the default fraction is 1. This is an
explicit display selection, not a disk/core classifier or modified density.

The transfer is an illustrative display model, not a calibrated synthetic
observation or the production ArepoRT optical model. Field colour and density
transparency are independent. Values outside the chosen field range contribute
no emission or extinction at their generators. The original-cell mode uses
this transfer for each segment. The reconstructed modes vary the resulting
coefficient and transformed colour scalar within the volume:

```text
tau = -ln(max(1 - opacity, 0.001))
      * (rho / reference_density)^density_power
      * density_support(rho)
      * segment_length_cm / reference_path_length_cm

alpha = 1 - exp(-tau)
image += transmission * alpha * field_colour_in_linear_light
transmission *= 1 - alpha
```

The density support rises smoothly from zero at the density floor to one a
decade above it. A zero floor disables this taper. The API's
`floor_softening_dex` can change its width or use a hard cutoff with zero.
The taper changes display opacity, not geometry or scientific field values.
Opacity 1 uses finite reference optical depth `-ln(0.001)`. Physical path-length
normalization keeps transparency independent of zoom and pixel size.

The browser offers three starting settings:

| Transparency profile | Density floor (g cm^-3) | Reference density (g cm^-3) | Density power | Reference path |
| --- | ---: | ---: | ---: | ---: |
| Disk | 100 | 10,000 | 0.5 | 10,000 km |
| Through to the remnant | 100 | 1,000,000 | 0.7 | 10,000 km |
| Diffuse outflow | 0.01 | 10,000 | 0.5 | 10,000 km |

These profiles expose different density/path-length scales. They do not change
the selected colour field or camera. The remnant setting can make the disk
faint; use a density close-up for a separate look at the dense material. A low
floor with the disk transfer can obscure the close-up with foreground material.
The first such rejected preview is retained in the development products.

The GPU integrates front to back in linear light and converts to sRGB once at
the end. The legend describes the per-cell colour mapping; a volume pixel is
a composite along the line of sight. Integration stops when transmission falls
below 0.001 or the ray exits the display box. A ray exceeding 8,192 cells or
encountering an invalid neighbour, non-finite value, missing exit face, or
persistent zero-length cycle fails the frame explicitly. There is no silent
point or software-rendering fallback. Per-frame records include failure counts,
cell visits, zero-length transitions, GPU device, timings, and source hashes.

## Face view

The ordinary VTK view shows the boundary of selected native cells. Hidden
interfaces are omitted; optional interior faces emit shared faces once with
the lower-index selected cell as owner. Open or oversized geometry fails
explicitly. The current vertex budget is 30 million per request. Density
visibility is independent of field colour. Surface opacity and depth peeling
composite polygons; they do not integrate hidden interior cells.

## Controls, measurements, and lifetime

On the Mac, the live viewer defaults to Metal volume rendering unless a
previous renderer choice was saved. The face and point views remain selectable.
During orbit, zoom, or slider interaction, a smaller image renders with one ray
per pixel; after interaction settles, the requested quality replaces it. One
request runs at a time. Scene hashes and renderer-change guards reject stale
results. Camera changes reuse resident GPU geometry and transfer buffers.

The bottom-right overlay follows the displayed frame. Time is read from the
loaded scene, not a camera-path row. For vertical half extent H, image aspect A,
and displayed width W, horizontal scale is `2 H A / W` cm per CSS pixel. The
1/2/5 scale bar updates with zoom and retains correct units on Retina displays.
Its field legend uses the displayed frame's style while an update is pending.

The face ruler measures the Euclidean distance between two picked native
surfaces. Volume and point rulers measure projected distance in the camera
plane and are labelled **projected**. Their recorded camera plane makes this
convention inspectable; starting a second pick from a different plane restarts
the projected measurement. A volume has no unique surface, so it does not reuse
stale VTK surface picks.

Existing channels, formulas, palettes, numeric ranges, camera alternatives,
style presets/bindings, camera paths, point captures, and VTK magnetic glyphs
remain available. Native view and transparency settings are included in style
presets and session archives. Original imported camera poses remain immutable.

The local server owns the renderer process group, including compiler children.
Quit and Ctrl-C stop it locally without SSH. Archive & close first freezes
requests and stops the worker, then performs the existing verified archival
workflow. Changing snapshot closes the old worker once the replacement scene
is ready. Volume-only use creates no VTK/OpenGL window.

## Capture and verification

`capture-mesh` consumes a hash-bound JSON config. Set each frame's
`parameters.representation` to `volume` or `faces` (the API default remains
`faces` for existing callers). Volume settings live under `parameters.volume`;
`volume.reconstruction` accepts `continuous_linear`, `linear`,
`piecewise_constant`, or `continuous` (legacy compact Shepard). Omitted API
settings and fresh browsers retain `linear`; `continuous_linear` is an explicit
quality option. Fresh browsers use four antialiased rays per pixel.
`subpixel_samples` is 1 or 4 and `cell_samples` is 1 or 2 for interpolation. See the [volume example](../examples/native-volume-capture.example.json)
and [face example](../examples/native-capture.example.json).

Each frame can use a normalized camera or an existing physical camera pose.
`fit_visible: true` records an explicit diagnostic framing: native face bounds
for VTK, visible generator bounds with a 45 percent margin for Metal. The output
records the resulting camera, so the fit can be reproduced without altering a
source pose.

The command refuses to overwrite an output directory. It writes annotated PNGs,
camera/style/scale/time records, input/output hashes, C++/Metal/Python source
hashes, native library hashes, and graphics capabilities. Initial scene loading,
geometry setup, transfer upload, GPU work, PNG encoding, and browser delivery
are distinct costs; GPU time is not a browser frame-rate claim.

The first 960 by 640 Metal previews on Apple M4 Pro traced 2,457,600 rays per
frame with zero reported traversal failures. GPU work took approximately
0.12-0.26 s, excluding setup and CPU/browser costs. Analytical local software
checks cover physical chord lengths, front-to-back integration, periodic faces,
large absolute coordinate offsets, field/formula binding, and failure handling.
The final regression and image-inspection record is maintained in
[the quality notes](volume-preview-quality.md).

The newer [field and inspection work](field-reconstruction-work.md) separates
structural usefulness from image smoothness. The comparison button preserves
camera/colour controls and reuses the last decoded native frame only when its
entire request key and image source still match. A snapshot, camera, field,
opacity or image-source change prevents that reuse.

Manual snapshot loads can carry the physical viewport and display settings.
The shell captures the latest view before replacing the iframe and binds the
restore to the requested snapshot and scene hash. The new viewport is converted
from physical centimetres using the new scene's centre/radius; the old snapshot
binding and time are never copied. This path skips automatic pose activation,
which could otherwise reset the view or request a different point budget.
Explicit saved-pose navigation retains its separate hash-checked behaviour.

Browser visual QA remains blocked by an admin-policy verification failure.
Controller tests use a fake DOM/transport, and native output PNGs are inspected
directly. These are local preview/software checks, not scientific promotion.
No raw snapshot reads or scientific production jobs were moved onto the Mac.

## Relationship to AREPO-VTK

[AREPO-VTK](https://github.com/dnelson/ArepoVTK) describes ray integration through
unstructured Voronoi fields and offers several reconstruction modes. The
display interpolators here are separate implementations. This companion consumes the complete scenes
exported by this project's AREPO-VTK workflow; it does not port its CUDA optical
profiles or claim the same reconstruction. VTK continues to supply the explicit
geometry/camera/picking tools. The Metal shader is compiled from source through
[Apple's native library API](https://developer.apple.com/documentation/metal/shader-libraries).

Arbitrary raw-HDF5 loading still needs the compatible AREPO-VTK snapshot/mesh/EOS
reader and its field/unit contract, with the workspace's required execution
placement. The current live selector acquires only the requested verified
scene/sidecar pair from the existing catalog.

## Native constructor and local cache

The inspected AREPO-VTK source calls `read_ic()` and then native `init()`
(including `create_mesh()`) on its mesh path. The v052 exporter records the
resulting native connectivity and neighbour displacements. Camera Lab uses
these prepared planes; it does not call AREPO's constructor on every frame.
This source-path check is not a bit-for-bit audit of the historical simulation
binary or exported mesh vertices.

The verified scene and sidecar cache is on the Mac's local APFS volume. A cache
hit in `acquire_verified_file()` verifies the checksum and returns before rsync.
Camera and display changes use the resident worker, and revisiting a cached
snapshot reloads local arrays. A missing prepared pair is transferred once and
then reused. The reconstruction comparisons used these local files; the only
recent cluster reads were small configuration/source files and bounded log
prefixes for the refinement-context check. No raw snapshot was fetched for them.

Ordinary Quit stops the local worker/server without needing SSH. Explicit
archival may delete a verified local cache after backup; a later visit would
then acquire that prepared scene again. Arbitrary raw-snapshot loading remains
separate work: construct once in the eta scientific lane and cache the prepared
result, rather than repeatedly reading HDF5 for camera changes.
