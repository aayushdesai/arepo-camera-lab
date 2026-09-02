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

The full-resolution first-order captures completed at 1440 by 960 with four
subpixel rays per pixel and zero reported traversal failures. GPU times were
0.25 s (snapshot 31 binary), 0.54 s (snapshot 721 disk), 0.64 s (remnant close-up),
and 0.24 s (outflow), excluding scene loading and image delivery. The images
still show cell-scale grain. Antialiasing addresses pixel edges; it cannot remove
jumps in the piecewise-constant field inside the volume. These captures remain
an intermediate baseline, not an accepted movie-quality result.

Continuous display-field interpolation is now being prototyped against the same
native mesh, cameras, and transfer settings. The first global-nearest-neighbour
prototype completed without traversal failures but took 1.24 s on M4 Pro for
only 480 by 320 pixels and one ray per pixel. Its search cost is too high for
the default interactive view. The native-neighbour prototype reduced that workload to 0.23 s, then rendered
1440 by 960 at four rays per pixel in 5.24 s with zero reported traversal errors.
These timings use one sample per cell and exclude field loading and browser IO.
The image softens cell boundaries but retains texture. The selectable
reconstruction, periodic-neighbour checks, and two-sample quadrature are now
implemented. The 52-test regression passed, followed by all 12 native-volume
checks after adding a strongly unequal-spacing fixture. That fixture verifies
spatial interpolation independently of a density-to-cell-size law. Final
paired image captures are pending.

The user's refinement caveat is an acceptance condition: do not attribute
remaining texture to coarse low-density cells from a constant-mass assumption.
Trace the exported scene back to its specific run, inspect its refinement and
derefinement configuration, and keep original-cell captures alongside the
interpolated ones. Neither a prettier image nor smoothing proves that a feature
is physically resolved.

The density channel is the exported simulation gas mass density in g cm^-3,
decoded as 10**(stored_density - 10). The old "density proxy" UI label was
misleading and has been corrected to "gas density". Only the density-dependent
transparency is an illustrative display choice; the density values are not a
proxy. No browser visual QA or production-movie quality claim follows from
these native previews.

The checked [run-specific refinement context](run-refinement-context.md) records
target-mass refinement plus volume and neighbour-volume limits. Interpret the
remaining grain using those rules and actual geometry; the image alone cannot
separate physical structure from finite-resolution texture.
