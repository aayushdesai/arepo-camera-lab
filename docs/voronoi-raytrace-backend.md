# Native 3D Voronoi viewer

Status: design for `feature/arepo-vtk-voronoi-m4`. The native mesh bridge and
M4 rendering integration are not implemented yet.

## Primary view

Open an interactive, rotatable 3D view of AREPO's actual Voronoi polyhedra.
Filled cell faces are the default representation. Keep the full cell geometry
and native connectivity available while orbiting, panning, and zooming into the
white dwarf, disk, and outflows. Cell-edge visibility is an optional overlay;
a movable slice is not the primary view.

Color cells by their physical fields. Provide adjustable opacity and field
visibility controls to expose interior structures without making a sliced
cross-section the viewing model. Preserve cell-centered values and stable
particle IDs through the renderer adapter. Opaque faces naturally occlude
interior cells; transparent rendering must handle depth ordering correctly.
Surface opacity is a visualization control, not a physical optical depth.

## Data and mesh ownership

Use the existing AREPO-VTK snapshot reader and AREPO mesh builder. Its
`Arepo::LoadSnapshot()` path initializes the native mesh, and `ArepoMesh` exposes
Delaunay connectivity, circumcenters, and Voronoi faces. Adapt that topology to
VTK polyhedral cells (`vtkUnstructuredGrid` / `vtkPolyhedron`), with physical
fields attached to cells. VTK supplies interactive rendering, picking,
measurement widgets, and overlays.

Preserve face winding, cell ownership, units, and periodic/ghost-cell semantics
in the adapter. Do not substitute a tessellation of the sampled point cloud
for the simulation mesh. Camera movement and display changes reuse the loaded
mesh and do not trigger reconstruction.

## Snapshot selection

Select any available simulation snapshot through the viewer. The application
reads that snapshot and constructs its mesh on demand, in memory, without a
separate user-run scene-export or sidecar-preparation step. The first load still
requires data transfer or disk reading plus reconstruction. Retain a bounded
cache for revisiting snapshots and make load progress and cancellation visible.
Switch the visible snapshot and its metadata together only after a successful
load; a failed load leaves the previous view intact.

The snapshot reader must use the simulation's explicit field and unit
conventions, including its temperature/EOS handling. Missing physical fields
must not be synthesized from an assumed ideal-gas conversion.

## Time, scale, and measurements

Place the loaded snapshot's simulation time and index at the bottom right,
alongside an adaptive physical scale bar and the active field's color legend.
Read time from snapshot metadata with its declared unit conversion, independently
of camera-path time.

Provide a two-point ruler. Points picked on the mesh have world coordinates,
so the ruler reports their 3D physical separation. Any screen-plane measurement
must be labeled as a projected distance. In orthographic mode, compute the scale
bar from the camera's physical screen extent. In perspective mode, identify the
reference depth used for the bar. Update these overlays with zoom and snapshot
changes and include them in captures when enabled.

## Apple silicon execution

Build AREPO-VTK and its dependencies natively for arm64. Use the M4 CPU for the
existing mesh-construction code and a supported GPU rendering backend for the
interactive view. VTK/Metal support must be checked in the actual installed
build; CUDA kernels do not run on Apple GPUs. Preserve robust geometry and
required double precision when optimizing construction.

The inspected development machine is an M4 Pro with 48 GiB unified memory.
Its current VTK 9.6.2 build has the OpenGL backend and lacks the WebGPU backend.
These are development observations, not runtime capability guarantees.

Keep direct local preview work separate from production scientific acceptance.
Existing eta placement and provenance requirements continue to govern production
analysis and validation.

## Geometry inspection and movie rendering

The full 3D mesh view is the primary geometry-debugging tool. It should expose
cell boundaries and structures hidden by the sampled point display. It does not
increase the simulation's spatial resolution.

A movie with physically meaningful emission/absorption still needs integration
along rays through those cells. Reuse AREPO-VTK's transfer and traversal semantics
for that backend. An M4 GPU implementation requires a Metal port and comparisons
with the native renderer before its output can be accepted as equivalent.

## Implementation checks

- Verify closed polyhedra, shared faces, orientation, cell IDs, periodic
  boundaries, and field bindings on small known meshes before a real snapshot.
- Compare the adapter against the native AREPO mesh; never silently repair or
  drop invalid cells.
- Confirm orbit, zoom, field changes, and opacity changes reuse the resident mesh.
- Check time labels and rulers against known coordinates, camera scales, and
  independently verified snapshot metadata.
- Measure snapshot-load time, peak memory, and interaction latency on the M4
  before making performance claims.

References: [AREPO-VTK](https://github.com/dnelson/ArepoVTK),
[AREPO snapshot format](https://arepo-code.org/wp-content/userguide/snapshotformat.html),
[VTK graphics backends](https://docs.vtk.org/en/latest/release_details/9.7/hardware-windows-and-wayland.html).
