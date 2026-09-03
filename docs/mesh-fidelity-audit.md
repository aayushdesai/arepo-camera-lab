# Mesh fidelity and the remaining grain

## Result, 2026-09-03

The exported geometry for snapshot 721 agrees with the saved AREPO cells.
The image-quality problem is not evidence of an incorrect refinement scheme.
The viewer reconstructs the snapshot's existing tessellation; it does not
split, merge, regularize, or reposition cells for display.

This conclusion is stronger than the earlier 56 software/display checks:
those checks did not compare real cell volumes with the original snapshot.
The new audit ran in eta allocation **64279567**, on **eta295**, with
`Arepo_Env` active. Slurm recorded `COMPLETED`, exit `0:0`, elapsed **3m46s**;
the audit itself took **144.860 s** after environment initialization.
It opened one raw HDF5 snapshot once. Subsequent camera changes continue to
use the existing local cache.

| Check on snapshot 721 | Result |
| --- | --- |
| Snapshot time | 3604.976133108139 s |
| Exact uint64 IDs | All 2,582,677 match; no duplicates or missing cells |
| Mesh-generating positions | Exact match for every cell, modulo periodic box |
| Density values | Maximum relative difference 3.035e-6 from float32 log encoding |
| Native neighbour vectors | All 39,569,906 agree with the raw generator displacements within float32 tolerance; no invalid indices or zero vectors |
| Independent polyhedral volumes | 848 sampled cells; maximum relative error 1.375e-7 against snapshot `Masses / Density` |
| Production face-builder volumes | Same 848 cells; maximum relative error 2.582e-6, or 0.0002582% |
| Geometry audit status | PASS |

The sample spans 16 equal-population density strata, 16 radius strata, and
extreme density, volume, and neighbour-degree cells. Adjacent selections are
excluded so the unchanged face builder emits complete individual polyhedra
instead of suppressing a shared face. Every sampled cell closes successfully.

The independent calculation intersects the exported bisector halfspaces with
SciPy/Qhull and obtains their convex-hull volume. The production
`native_mesh.cpp` is compiled unchanged on eta; its actual normalized float32
output polygons are triangulated about the owning generator to recover volume.
The common acceptance tolerance was 0.1%; measured errors are much smaller.
No image blur or altered refinement enters either calculation.

## Refinement is present in the saved cells

The snapshot's own `Parameters` and `Config` attributes independently confirm
the [recorded refinement settings](run-refinement-context.md): target mass
1.19e27 g, criterion 1 for refinement/derefinement, split/merge enabled,
volume limits enabled, `MaxVolumeDiff=4`, `MaxVolume=1e30 cm^3`, `MinVolume=0`,
and face-angle regularization. The source defines decision thresholds rather
than hard bounds guaranteed at every snapshot.

Actual cell sizes do grow towards lower density in this snapshot, while the
diffuse material approaches the volume-limited regime. These are density bins,
not disk/core/outflow classifications or a radial resolution law:

| log10(density / g cm^-3) interval | Cells | Median equivalent cell width, V^(1/3) |
| --- | ---: | ---: |
| 5.58 to 6.59 | 881,260 | 103 km |
| 1.55 to 2.56 | 75,028 | 2,736 km |
| -2.48 to -1.47 | 108,524 | 42,208 km |
| -5.50 to -4.50 | 578,382 | 111,540 km |

These summaries were computed on eta from the complete snapshot, not from the
848-cell geometry sample. The lowest-density bin has median mass 1.385e25 g,
well below the target mass, and median volume 1.388e30 cm^3. A fixed-mass
density-to-size assumption therefore cannot describe the whole scene.

## What still needs work in the image

The point preview draws round markers of fixed screen-pixel size with uniform
per-marker opacity. It does not integrate density through each physical cell.
Its smoother appearance is therefore not a matched test of mesh geometry or
physical resolution; the WebGL shader does not use a Gaussian kernel.

At the time of this audit, the Metal default fitted limited gradients to
transformed colour and display extinction independently. These are not AREPO's hydrodynamic gradients,
and adjacent fitted cells can still disagree at their shared boundary.
Colour mapping, density support, and opacity also determine which physical
cell structure is visible. This makes the display reconstruction and transfer
the next things to investigate, rather than changing the saved cell refinement.
The audit does not prove that every visible patch comes from interpolation,
nor does it validate the scientific interpretation or polish of the image.

A useful next comparison must use the same native scene, field, camera, and
explicit transfer, with an AREPO-VTK reference reconstruction. It should test
the field before colour/opacity mapping and distinguish interpolation error
from real cell-average structure. The v004 images remain rejected quality
candidates. No new rendering or simulation resolution claim accompanies this
geometry result. Subsequent [field and inspection work](field-reconstruction-work.md)
adds field-before-transfer interpolation and matched-view controls. Scientific
usefulness means retaining visible structures and following their evolution,
not merely making these images smoother.

## Reproduction and evidence

The [audit program and eta wrapper](../validation/native_mesh/README.md) are
retained with the implementation. The immutable cluster run is under
`camera_lab_validation/mesh_fidelity_20260903_v001` relative to the AREPO root.
The local compact receipt is under
`generated/mesh-fidelity-20260903-v001/verified_receipt`. It contains the report,
per-cell CSV, builder/compiler logs, Slurm accounting, and SHA-256 manifest.
Raw snapshots were not transferred to the Mac.

- Prepared scene SHA-256: `dbbe15673ae7dc98057033408b13b832fb56855f0f7b0bd2bc4ab8a9b598c036`
- Production face-builder source SHA-256: `8586572e901c01477472c275996b8a35ff264e08ec6a81a47ff658b4a5046466`
- Executed audit source SHA-256: `f16a8ffa52fb70123991223353908c0242680b4e309313d565770708d95d3e06`
- Report SHA-256: `cbc2e75b7a6cb36025796f33ebb6faa53a6ec77bba4406a39feaeba7b500f28d`
- Per-cell CSV SHA-256: `526fab0f0dc157e9b3e1eebe2f143f968d6ce8e334faa351cfc3042d7ab41b2a`

The snapshot payload arrays have individual hashes in the report, and its
size/mtime are unchanged across the read; there is no claim of a whole-HDF5
file checksum. This checks one real epoch, not every catalog snapshot, and
cell-volume validation is sampled rather than exhaustive. It does not replace
a reference check of Metal ray integration or browser visual QA.

For the snapshot conventions, see AREPO's
[snapshot format](https://arepo-code.org/wp-content/userguide/snapshotformat.html):
`Coordinates` stores mesh generators, while `CenterOfMass` is a separate field;
cell volume is mass divided by density. The snapshot does not contain an
explicit `Volume` dataset in this run.
