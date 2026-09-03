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
spatial interpolation independently of a density-to-cell-size law.

The paired v003 captures finished at 1440 by 960, four rays per pixel and
two samples per cell for interpolation. Original-cell GPU times were 0.27 s
(binary), 0.70 s (disk), 0.68 s (remnant), and 0.27 s (outflow). The corresponding
Shepard times were 12.26, 23.87, 27.76, and 11.79 s. All eight frames reported
zero traversal failures. The user rejected the grain again. These images are
retained as failed quality candidates; passing traversal and software checks
does not make their appearance acceptable.

## Smooth-field regression and limited gradients

A synthetic 27-cell native periodic lattice was assigned known affine colour
and extinction fields. At 512 points inside its central cell, the Shepard
interpolator deviated by up to 0.04489 in normalized field units. Its vanishing
slope near generators creates plateaus even when the input field is smooth.
The limited least-squares prototype reproduced the same field with maximum
error 5.96e-8, retained generator values exactly, and stayed within native
neighbour bounds. This isolates one source of artificial texture; it does not
identify every feature in the real snapshot images.

Native M4 previews of the same four cached scenes/cameras completed at 1440 by
960 with four subpixel rays and two cell samples in 0.28, 0.61, 0.72, and 0.28 s
of GPU time (binary, disk, remnant, outflow respectively). All reported zero
traversal failures. The gradients are a fast preview improvement, but the disk
still contains visible texture and the colour/opacity mapping hides some inner
structure. They are not being accepted as polished movie frames.

A bounded moving least-squares prototype also passed the affine check
(maximum error 1.79e-7). At 960 by 640 and one ray per pixel, it cost 3.43 s for
the disk, 4.14 s for the remnant, and 1.64 s for the outflow. It softened texture
but did not establish the desired visual quality and is retained as a separate
experiment, outside the live viewer. The limited linear mode replaces Shepard
as the interactive default; original values and legacy presets remain usable.

The release checks add affine reproduction, strongly unequal native spacing,
local no-overshoot bounds, hidden non-finite fields, and reported gradient
fallbacks. All 56 software regression checks passed on the M4 Pro in 6.471 s.
The v004 package is complete at
`~/Movies/AREPO/voronoi-m4-preview-20260902-v004`, with four matched-camera
PNGs, per-frame reports, configuration files, source/binary/input/output hashes,
a comparison index, regression logs, and retained rejected experiments.
The capture source commit is `a9cef721dba48f35db8c25224074bdafde33b456`.

All four final PNGs decoded successfully at 1440 by 960. The 22,118,400 total
rays reported no traversal failures and no gradient fallback cells. Camera,
field, opacity, density support, timestamp, scale, source hashes and binary
hashes passed the local preview checks. Final GPU times were 0.277, 0.607,
0.719 and 0.281 s for binary, disk, remnant and outflow respectively. Total
renderer times were 0.462, 0.805, 0.905 and 0.455 s, excluding initial scene
loading and browser delivery. All four final images were visually inspected;
remaining texture and loss of visible inner detail still prevent movie-quality
acceptance. These are software/display checks, not scientific promotion.

The controller also had a separate resolution bug: a 480-pixel fit request
finishing after interaction ended stored the *current* high-quality request key
against the low-resolution image. That suppressed the subsequent sharper frame.
A held-response regression reproduced the failure before the fix and passed after it. The cache key
now retains the completed request's resolution, quality, field and style settings,
changing only its fitted camera and consumed fit flag. This fix addresses a
stretched preview; it does not explain texture in the full-size capture PNGs.

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

## Real geometry audit, 2026-09-03

The user again questioned whether the grain came from incorrect mesh refinement.
An eta audit now closes the missing raw-snapshot comparison for epoch 721:
2,582,677 exact IDs and generator positions match, all 39,569,906 neighbour
vectors pass the float32 consistency check, and 848 independent/production
polyhedral volumes agree with raw M/rho. The maximum production relative
volume error is 2.582e-6. The snapshot metadata also confirms the recorded
refinement rules. [Methods, cell-size summaries, and evidence](mesh-fidelity-audit.md)
are retained with the reproducible audit.

This does not make the images acceptable. At the time of the audit, the default fitted
transformed colour and display opacity independently and can jump at cell
faces. A comparison with an AREPO-VTK reference field reconstruction is the
next diagnostic; the saved cell refinement should be retained. The point
preview uses fixed-pixel round markers and per-marker opacity, so its
appearance is not a matched physical-volume reference. No additional render
or image-quality success is claimed by this audit.

## Field reconstruction and scientific inspection, 2026-09-03

The [next iteration](field-reconstruction-work.md) adds a continuous blend of
local gradients, optional field-before-transfer mapping, dense-gas fading,
quick comparison with the point cloud, and a physical view lock across manual
snapshot loads. Fast linear reconstruction remains the initial interactive
mode. The point-cloud workflow remains a reference for finding structures and
following formation; a smoother native image alone does not meet that goal.

Seven new matched native PNGs are retained at
`~/Movies/AREPO/voronoi-m4-preview-20260903-v005`, with source commit
`0806c29a4bbd8fe190c703f691b2338d261b111d`, configurations and per-frame reports.
The subsequent GUI/default changes leave the captured numerical renderer
unchanged. The continuous images reduce some boundary texture; the optional
dense fade reveals more inner contrast. The images do not yet demonstrate
better structural visibility than the point cloud or continuity of identified
structures through adjacent snapshots. Those remain separate requirements
from software correctness and presentation quality.
