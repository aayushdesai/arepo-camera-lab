"""Create explicit auxiliary field sidecars from one AREPO HDF5 snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

from .viewer import AUXILIARY_SCHEMA, sha256


def _dataset(handle, path: str, expected_tail: tuple[int, ...]) -> np.ndarray:
    if path not in handle:
        raise ValueError(f"HDF5 dataset does not exist: {path}")
    values = np.asarray(handle[path])
    if values.ndim != 1 + len(expected_tail) or values.shape[1:] != expected_tail:
        raise ValueError(
            f"HDF5 dataset {path} has shape {values.shape}; expected (N,{','.join(map(str, expected_tail))})"
            if expected_tail else
            f"HDF5 dataset {path} has shape {values.shape}; expected (N,)")
    return values


def build_sidecar(snapshot: Path, output: Path, *, ids_dataset: str,
                  field_specs: dict[str, tuple[str, float]],
                  snapshot_sha256: str | None = None) -> dict:
    try:
        import h5py
    except ImportError as error:
        raise ValueError("h5py is required; recreate the Conda environment") from error

    snapshot = snapshot.expanduser().resolve()
    output = output.expanduser().resolve()
    if not snapshot.is_file():
        raise ValueError(f"snapshot does not exist: {snapshot}")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    with h5py.File(snapshot, "r") as source:
        particle_id = _dataset(source, ids_dataset, ()).astype(np.uint64, copy=False)
        if np.unique(particle_id).size != particle_id.size:
            raise ValueError("snapshot particle IDs are not unique")
        arrays: dict[str, np.ndarray] = {
            "schema": np.asarray(AUXILIARY_SCHEMA),
            "particle_id": particle_id,
        }
        recorded = {}
        for output_name, (dataset_path, scale) in field_specs.items():
            tail = (3,) if output_name == "magnetic_field_gauss" else ()
            values = _dataset(source, dataset_path, tail).astype(np.float64)
            if values.shape[0] != particle_id.size:
                raise ValueError(
                    f"{dataset_path} has {values.shape[0]} rows; IDs have {particle_id.size}")
            values *= scale
            if not np.all(np.isfinite(values)):
                raise ValueError(f"scaled field {dataset_path} contains non-finite values")
            arrays[output_name] = values.astype(np.float32)
            recorded[output_name] = {"dataset": dataset_path, "scale_to_declared_unit": scale}
    digest = (snapshot_sha256 or sha256(snapshot)).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("snapshot SHA-256 must contain 64 hexadecimal characters")
    provenance = {
        "schema": AUXILIARY_SCHEMA,
        "source_snapshot": str(snapshot),
        "source_snapshot_sha256": digest,
        "particle_ids_dataset": ids_dataset,
        "fields": recorded,
    }
    arrays["provenance_json"] = np.asarray(
        json.dumps(provenance, sort_keys=True, separators=(",", ":")))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        np.savez_compressed(destination, **arrays)
    return {"output": str(output), "sha256": sha256(output),
            "particle_count": int(particle_id.size), "fields": sorted(recorded)}


def add_arguments(result: argparse.ArgumentParser) -> None:
    result.add_argument("--snapshot", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--ids-dataset", default="PartType0/ParticleIDs")
    result.add_argument("--snapshot-sha256")
    result.add_argument("--magnetic-dataset")
    result.add_argument("--magnetic-unit-gauss", type=float)
    result.add_argument("--pressure-dataset")
    result.add_argument("--pressure-unit-dyn-cm2", type=float)
    result.add_argument("--entropy-dataset")
    result.add_argument("--entropy-unit-cgs", type=float)
    result.add_argument("--sound-speed-dataset")
    result.add_argument("--sound-speed-unit-cm-s", type=float)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Build an ID-bound magnetic/thermodynamic NPZ sidecar.")
    add_arguments(result)
    return result


def run(args: argparse.Namespace) -> int:
    options = [
        ("magnetic_field_gauss", args.magnetic_dataset, args.magnetic_unit_gauss),
        ("pressure_dyn_cm2", args.pressure_dataset, args.pressure_unit_dyn_cm2),
        ("specific_entropy_cgs", args.entropy_dataset, args.entropy_unit_cgs),
        ("sound_speed_cm_s", args.sound_speed_dataset, args.sound_speed_unit_cm_s),
    ]
    fields = {}
    for name, dataset, scale in options:
        if (dataset is None) != (scale is None):
            parser().error(f"{name} requires both a dataset and a unit scale")
        if dataset is not None:
            if not np.isfinite(scale) or scale == 0.0:
                parser().error(f"{name} unit scale must be finite and nonzero")
            fields[name] = (dataset, float(scale))
    if not fields:
        parser().error("specify at least one magnetic, pressure, entropy, or sound-speed field")
    try:
        result = build_sidecar(
            args.snapshot, args.output, ids_dataset=args.ids_dataset,
            field_specs=fields, snapshot_sha256=args.snapshot_sha256)
    except (FileExistsError, OSError, ValueError) as error:
        print(f"arepo-camera-lab fields: {error}", file=sys.stderr)
        return 1
    print("AREPO_CAMERA_FIELD_SIDECAR_OK " + json.dumps(result, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
