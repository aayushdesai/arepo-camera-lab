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

Periodic generator positions and ghost-neighbour planes remain available.
The Metal locator indexes every generator and starts each ray at the periodic
display-box entrance. All traversed cells are available, including ones that
make no visible contribution. Cell values are piecewise constant; no neighbour
interpolation has been added. Four deterministic subpixel rays provide the
high-quality image. No time-varying sampling noise is introduced.

Fields use the existing 24-channel implementation and restricted formula
language. Sidecars join by uint64 particle ID. Non-finite formula values are
hidden. The point budget never subsamples the native mesh. Face picks preserve
the native cell index and serialize particle IDs as strings for JavaScript.

## Volume transparency

The transfer is an illustrative display model, not a calibrated synthetic
observation or the production ArepoRT optical model. Field colour and density
transparency are independent. Values outside the chosen field range contribute
no emission or extinction. For each visible native-cell segment:

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
`subpixel_samples` is 1 or 4. See the [volume example](../examples/native-volume-capture.example.json)
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

Browser visual QA remains blocked by an admin-policy verification failure.
Controller tests use a fake DOM/transport, and native output PNGs are inspected
directly. These are local preview/software checks, not scientific promotion.
No raw snapshot reads or scientific production jobs were moved onto the Mac.

## Relationship to AREPO-VTK

[AREPO-VTK](https://github.com/dnelson/ArepoVTK) describes ray integration through
unstructured Voronoi fields and offers reconstruction modes beyond the
piecewise-constant mode used here. This companion consumes the complete scenes
exported by this project's AREPO-VTK workflow; it does not port its CUDA optical
profiles or claim the same reconstruction. VTK continues to supply the explicit
geometry/camera/picking tools. The Metal shader is compiled from source through
[Apple's native library API](https://developer.apple.com/documentation/metal/shader-libraries).

Arbitrary raw-HDF5 loading still needs the compatible AREPO-VTK snapshot/mesh/EOS
reader and its field/unit contract, with the workspace's required execution
placement. The current live selector acquires only the requested verified
scene/sidecar pair from the existing catalog.
