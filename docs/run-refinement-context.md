# Refinement context for the native preview scenes

Checked 2026-09-02 by read-only inspection of small configuration/source files
and the first 64 KiB of selected run logs. No raw snapshot payload was read and
no new cell-size statistics were calculated.

## Run binding

The export configurations for snapshots 31 and 721 both name
`mass_frac/0.6+0.65/locked/run/merger/output/snapshot_NUMM`. The prepared scene
hashes are `effc78cfe58b5e6b8a224b103fcf9cd3195e715b4b1411cea9b282d90ec775e2`
and `dbbe15673ae7dc98057033408b13b832fb56855f0f7b0bd2bc4ab8a9b598c036`.
The exporter provenance records eta execution. Its `param_native.txt` is a
renderer parameter file; it must not be mistaken for the simulation settings.

The simulation's `param.txt`, `param.txt-usedvalues`, compiled
`arepo/build/arepoconfig.h`, and colocated refinement sources were inspected.
The startup headers in `logout_60681315.txt`, `logout_60857433.txt`, and
`logout_60963778.txt` corroborate the following refinement settings. The bounded
prefix of `logout_61221773.txt` contained no matching settings; no conclusion
was drawn from its absence.

| Setting | Recorded value |
| --- | --- |
| `REFINEMENT_SPLIT_CELLS` | enabled |
| `REFINEMENT_MERGE_CELLS` | enabled |
| `REFINEMENT_VOLUME_LIMIT` | enabled |
| `REGULARIZE_MESH_FACE_ANGLE` | enabled |
| `ReferenceGasPartMass` | 1.19e27 g |
| `TargetGasMassFactor` | 1 |
| `RefinementCriterion`, `DerefinementCriterion` | 1, 1 |
| `MaxVolumeDiff` | 4, a volume ratio |
| `MaxVolume` | 1e30 cm^3 |
| `MinVolume` | 0 |
| length and mass unit factors | 1 cm and 1 g |

## Consequence for cell size

In the colocated source, criterion 1 splits eligible cells above twice the
target mass and requests derefinement below half the target mass. However,
volume checks precede those mass rules. Eligible cells can also split above
`2 * MaxVolume` or above `MaxVolumeDiff * MinNgbVolume`. Derefinement is vetoed
above `0.5 * MaxVolume` or `0.3 * MaxVolumeDiff * MinNgbVolume`. Splitting also
requires the face-angle shape condition; these are decision thresholds, not a
claim that every instantaneous cell satisfies hard bounds.

Therefore a fixed-mass law, `cell width proportional to density**(-1/3)`, is
only a conditional heuristic here. Volume constraints can retain much smaller
masses in diffuse material and limit contrasts between neighbouring cells.
Distance from the remnant alone does not determine resolution. This check does
not measure the actual volume/mass distribution of any snapshot or establish
which visible patch is a resolved physical structure.

The viewer keeps the native generator positions, neighbour planes, and original
cell values. Smooth field is a separate, selectable display interpolation of
those values. Its support comes from actual spatial neighbours, with no
assumed density-to-cell-size conversion. It can soften sharp physical features
and does not conserve cell mass as a physical reconstruction. Keep original
cell images alongside it when diagnosing the movie.

## Evidence locations and hashes

The v003 comparison products retain copies under `refinement-evidence/` with
source paths, SHA-256 hashes, and bounded-log excerpt line numbers in
`manifest.json`. The copied source files establish what the colocated source
currently says; they are not a fresh binary/source equivalence audit of the
historical simulation.

- `param.txt-usedvalues`: `5e3e42d1a8e0954361eec4f6718088262f8903f7b05658658f033c59cc4fc9f1`
- `arepoconfig.h`: `dfdbbaa50d0ca6f61ea4a67f4ba8e9f5e5acc409056af25e294a9623b5c711f2`
- `criterion_refinement.cc`: `22d4dfec13cbfd592f74dda13dae38619581a14ea0faf7d00c76fcfaa001a771`
- `criterion_derefinement.cc`: `73efee3ed48c4bf316f7073b4ac69e8d719299168818f506dd932779bef03f5e`
