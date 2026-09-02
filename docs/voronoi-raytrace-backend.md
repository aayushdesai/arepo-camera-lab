# Native 3D Voronoi viewer

Implemented on `feature/arepo-vtk-voronoi-m4`: full native connectivity, filled
3D cell faces, existing browser controls, simulation time, physical scale bar,
field legend, and a two-point surface ruler. Direct raw-HDF5 loading and an M4
emission/absorption ray tracer remain unimplemented.

## Geometry and fields

The adapter reads complete v052 cell records, neighbour offsets, and native
neighbour displacement vectors exported by ArepoVTK-stellar. Each displacement
defines a Voronoi bisector. A C++17 worker intersects those planes in double
precision to recover convex cell faces. It never tessellates the sampled point
cloud or substitutes a slice. Displacement vectors are float32 in v052, so this
reconstructs the exported planes; it does not claim bit-identical original
AREPO double-precision vertices.

The ordinary view is the exterior boundary of selected native cells. Hidden
interfaces are omitted while full native connectivity remains available. The
optional interior-face view emits shared faces once, with the lower-index
selected cell as owner. Neighbour planes, including periodic ghost planes,
clip the geometry. Periodic cell centres are unwrapped relative to the same
centre as the point preview. Invalid, open, or oversized geometry fails
explicitly; there is no silent point fallback. The current vertex limit is
30 million per request.

Each polygon retains its native cell index. Physical sidecars are joined by
uint64 particle ID; picked IDs are serialized as strings to avoid JavaScript
integer rounding. The existing 24-channel and formula language is shared with
the point view. Non-finite formula results are hidden. Zero-valued fields can
load even when their usual display default is logarithmic.

Visibility combines the selected channel range with an independent density
floor. Lower that floor to expose diffuse material. Surface opacity controls
polygon compositing, not optical depth; it does not integrate all hidden
interior cells. Depth peeling was exercised on the M4.

## Cameras, measurements, and existing functionality

The renderer uses the existing orthographic camera basis, centre, and physical
screen half extent. Camera and colour changes reuse geometry; visibility
changes reconstruct surfaces. One native request runs at a time, with a smaller
viewport during dragging. Failed requests retain the preceding visible frame
and show an error. Scene hashes prevent stale frames/picks being attributed to
a different snapshot.

The bottom-right overlay is tied to the displayed frame. Time comes from the
loaded scene's `snapshot_time_seconds`, never a camera-path row. For vertical
half extent H, viewport aspect A, and displayed width W, the horizontal scale
is `2 H A / W` cm per CSS pixel. A 1/2/5 scale bar updates with zoom. The ruler
picks two native surfaces and reports their Euclidean separation in physical
coordinates. The point renderer uses a camera-parallel plane and labels the
result **projected**.

Imported camera alternatives remain immutable. Physical channels, formulas,
style presets/bindings, camera controls, point-mode captures, native VTK
point/glyph mode, and verified snapshot selection remain supported. New presets
also retain mesh visibility/edge/lighting and measurement display settings.
Mesh preview settings do not redefine the production optical model.

The worker owns its compiler/face-builder process group. Local Quit and Ctrl-C
terminate it without SSH. Archive & close freezes requests before stopping it
and performs the existing verified archival workflow. Changing snapshot closes
the old worker when the replacement scene is ready.

## Verified development results, 2026-09-02

On the M4 Pro, installed VTK 9.6.2 reported:

```text
OpenGL vendor string: Apple
OpenGL renderer string: Apple M4 Pro
OpenGL version string: 4.1 Metal - 90.5
hardware acceleration: Yes
```

This is VTK's OpenGL path through the macOS graphics driver. No CUDA kernels or
new direct Metal ray tracer are used. The native C++ face builder runs on the
CPU with eight workers by default.

Real prepared scenes were checksum-verified for snapshots 31 (1,696,613 cells,
155.0146484375 s) and 721 (2,582,677 cells, 3604.976133108139 s). Example
selections produced 22,693, 38,747, and 127,691 native faces. Cached camera
redraws at 1440 by 960 took about 0.04-0.09 s. These renderer timings exclude
network transfer, first-load work, and browser latency.

The 41-test local software regression run passed, including native geometry
closure/winding/volume, shared interfaces, periodic ghost neighbours,
ID/field binding, camera/palette redraw, transparent rendering, 3D picks,
projected-ruler calibration, snapshot hash guards, native worker shutdown,
and existing camera/review/catalog/movie/archive tests. Browser controller
tests used a fake DOM/transport. Browser visual inspection was blocked by an
admin-policy verification failure; native PNGs were visually inspected. These
are local preview checks, not scientific promotion.

## Capture contract

`capture-mesh` consumes a JSON config with `scene_path`, `scene_sha256`,
`snapshot`, optional `field_sidecar_path` plus `field_sidecar_sha256`, and
`frames`. Each frame has a simple `name`, display `parameters`, and either a
normalized `parameters.camera` or an existing `physical_camera` pose.
`fit_visible: true` records a diagnostic framing without changing the source
pose. See [the example](../examples/native-capture.example.json).

The command creates a new output directory and refuses to overwrite one. It
writes annotated PNGs, camera/style/scale/time JSON records, input/output
hashes, adapter source/binary hashes, and graphics capabilities. Keep these
records with each preview. Density-threshold surfaces help inspect geometry;
they add no spatial resolution and do not replace validated optical rendering.

## Snapshot-loading gap

The viewer currently selects complete, hash-bound v052 scene/sidecar pairs
from the verified catalog and transfers only the requested pair. It does not
yet read arbitrary raw HDF5 snapshots on demand. That path needs the compatible
AREPO-VTK snapshot/mesh/EOS reader and its field/unit contracts, plus the
workspace's required execution placement. No raw scientific reads or new
production jobs were moved onto the Mac for these preview checks.
