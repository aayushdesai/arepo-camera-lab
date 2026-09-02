# Native-volume preview quality work

## Problem observed, 2026-09-02

The first full-mesh previews expose the outside of cells passing a hard density
cut. Their flat polygon lighting and abrupt selection boundary look faceted,
and the opaque shell hides the remnant, disk interior, and diffuse outflows.
These images verify geometry availability; they do not establish movie quality.
The previous captures and their provenance are retained as the baseline.

## Implementation and first preview checks

The Metal prototype traces rays through native Voronoi cells and composites
each physical path segment with an explicit display transfer function. This
keeps the complete native connectivity and avoids replacing the mesh with a voxel grid or a sampled-point
tessellation. Native M4 Metal compute performs this preview rendering; the
VTK cell-face view remains available as a geometry diagnostic.

Rendering parameters must separate field colour, density support, and optical
visibility. This is an illustrative volume preview, not a calibrated radiation
transport calculation and not added simulation resolution. Record the actual
backend, traversal errors, camera, physical scale, snapshot/time, source/input
hashes, and workload timings. Compare native output images before choosing a
default. Scientific raw-input processing and production validation retain the
workspace execution-placement rules.

Timestamp, calibrated scale bar, field legend, camera/style controls, snapshot
binding, and a clearly defined ruler must stay attached to the displayed frame.
A transparent volume has no unique physical surface; its ruler depth convention
must be explicit. Browser visual QA remains blocked by the previously reported
admin-policy verification failure; native render checks do not substitute for it.

The first Metal preview ran on Apple M4 Pro against the existing checksum-bound
snapshot 721 scene. At 960 by 640 with four subpixel rays per pixel, disk and
outflow frames took about 0.12-0.26 s on the GPU, excluding field loading,
transfer updates, image encoding, browser delivery, and initial compilation.
Each frame traced 2,457,600 rays with zero reported traversal failures. Native
PNGs were inspected: the opaque polygon shell is removed. A low density floor
in the disk view initially made foreground emission obscure the disk; this
rejected preview is retained. Disk, remnant, and diffuse-outflow transparency
presets address different density/path-length scales.

The backend, capture command, browser controls, and preset persistence are
connected. The local software regression run passed **49 tests**, including
analytical chord lengths/compositing, periodic geometry, large absolute
coordinate offsets, invalid-neighbour rejection, existing derived fields,
geometry/transfer reuse, asynchronous renderer-switch guards, projected/surface
rulers, native-worker shutdown, and the existing camera/catalog/archive checks.
The first regression run found two fixture issues (a fake control without an
event method and a point budget below the server minimum); both were corrected
and the complete run passed. Native Metal checks create no OpenGL window.

Full-resolution comparison captures and their inspection are the remaining
image gate. No browser visual QA or production-movie quality claim follows from
these native previews.
