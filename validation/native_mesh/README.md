# Native mesh fidelity audit

`audit_mesh.py` compares a complete, checksum-bound v052 scene with one raw
AREPO HDF5 snapshot. It checks exact uint64 IDs, generator positions, density,
all native neighbour vectors, and a deterministic sample of cell volumes using
both an independent halfspace solver and the production face builder.

This is a scientific validation job. It refuses to run outside an eta Slurm
allocation or without `Arepo_Env`. The supplied wrapper requests eta explicitly
and uses the workspace's pinned batch environment. It reads the snapshot once;
do not run it on a login node or the local Mac. No simulation files are changed.

Prepare a fresh, no-clobber directory on the cluster containing:

- `audit_mesh.py` and `audit.sbatch` from this directory;
- the exact `src/arepo_camera_lab/native_mesh.cpp` used by the preview;
- a `config.json` with the following fields.

```json
{
  "snapshot": "/absolute/path/to/snapshot_721.hdf5",
  "scene": "/absolute/path/to/scene_v052.bin",
  "scene_sha256": "trusted SHA-256 from the preview catalog",
  "native_builder_source_sha256": "SHA-256 of the supplied native_mesh.cpp",
  "feature_commit": "source commit used by the preview",
  "center_cm": [501088165136.1037, 499042820391.7337, 479774439428.8581],
  "radius_cm": 756392444536.8658
}
```

Use the actual preview's centre and normalization radius; they affect the
float32 face-output precision being tested. The script expects the v052 density
encoding `log10(snapshot density) + 10`, as used by these cgs gas scenes. It
supports a single-file gas snapshot, with either per-cell `Masses` or a constant
header mass, and optionally checks a saved `Volume` dataset against M/rho.
It does not silently support split snapshots or unrelated unit conventions.

Submit from the staged directory:

```bash
sbatch --chdir="$PWD" --output="$PWD/allocation-%j.log" audit.sbatch
```

The script creates `results` exclusively and refuses to overwrite it. Preserve
failed runs; stage a new directory for any retry. Keep Slurm accounting and
input/output hashes alongside `results/report.json`, `cell_volume_audit.csv`,
the builder/compiler logs, and the sampled geometry. The report records raw
dataset hashes and snapshot stat information; it does not hash the whole HDF5
file a second time.

The real snapshot 721 execution passed on eta295 in allocation 64279567. See
the [result and limitations](../../docs/mesh-fidelity-audit.md). Its reported
success does not certify every cell volume, the Metal integrator, interpolation
quality, or the physical interpretation of rendered texture.
