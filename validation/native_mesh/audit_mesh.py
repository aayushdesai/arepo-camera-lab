#!/usr/bin/env python3
"""Eta-only audit of a frozen v052 mesh against one AREPO snapshot.

No re-export, refinement, renderer transfer changes, or snapshot writes.
The independent halfspace solver and unchanged production face builder both
check a deterministic, stratified, non-adjacent sample of complete cells.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time


def guard():
    if not os.environ.get("SLURM_JOB_ID") or not socket.gethostname().split(".")[0].startswith("eta"):
        raise RuntimeError("Scientific validation requires an eta Slurm allocation")
    if os.environ.get("CONDA_DEFAULT_ENV") != "Arepo_Env":
        raise RuntimeError("Arepo_Env is not active")


guard()
import h5py
import numpy as np
import scipy
from scipy.spatial import ConvexHull, HalfspaceIntersection


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def json_value(x):
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, bytes):
        return x.decode(errors="replace")
    return str(x)


def stat(path):
    s = Path(path).stat()
    return dict(bytes=s.st_size, mtime_ns=s.st_mtime_ns)


def stats(x):
    x = np.asarray(x, dtype=np.float64)
    good = x[np.isfinite(x)]
    result = dict(count=int(x.size), nonfinite=int(x.size-good.size))
    if good.size:
        result["quantiles"] = dict(zip(["min", "p01", "p50", "p95", "p99", "max"],
                                       np.quantile(good, [0, .01, .5, .95, .99, 1]).tolist()))
    return result


def independent_volume(delta):
    lengths = np.linalg.norm(delta, axis=1)
    if len(delta) < 4 or not np.all(np.isfinite(delta)) or np.any(lengths <= 0):
        raise ValueError("Invalid native halfspaces")
    scale = np.median(lengths)
    d = delta / scale
    halfspaces = np.column_stack((d, -.5 * np.einsum("ij,ij->i", d, d)))
    vertices = HalfspaceIntersection(halfspaces, np.zeros(3)).intersections
    return ConvexHull(vertices).volume * scale**3, len(vertices)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    output = Path("results")
    output.mkdir(exist_ok=False)
    start = time.monotonic()
    report = dict(schema="arepo_mesh_fidelity_audit_v001", config=config,
                  host=socket.gethostname(), job_id=os.environ["SLURM_JOB_ID"],
                  environment=os.environ["CONDA_DEFAULT_ENV"], python=sys.version,
                  numpy=np.__version__, scipy=scipy.__version__, h5py=h5py.__version__,
                  started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  audit_source_sha256=digest(__file__))
    try:
        scene = Path(config["scene"])
        raw = Path(config["snapshot"])
        report["scene_sha256"] = digest(scene)
        if report["scene_sha256"] != config["scene_sha256"]:
            raise RuntimeError("Scene checksum differs from the actual Mac preview")
        names = ["magic", "version", "endian", "header_bytes", "cell_bytes", "edge_bytes", "ray_bytes",
                 "sample_width", "sample_height", "source_width", "source_height", "samples", "flags",
                 "num_cells", "num_edges", "num_rays", "invalid_edges", "inactive_rays", "box", "ray_max",
                 "origin_x", "origin_y", "origin_z", "position_unit", "density_unit", "velocity_unit", "temperature_unit", "time", "reserved"]
        with scene.open("rb") as f:
            header = dict(zip(names, struct.unpack("<16s10IiI5Q10d24s", f.read(208))))
        if header["magic"].rstrip(b"\0") != b"ARVTKSTARV052A" or header["version"] != 5 or header["endian"] != 0x01020304:
            raise ValueError("Unexpected scene layout")
        if (header["header_bytes"], header["cell_bytes"], header["edge_bytes"]) != (208, 52, 16) or header["invalid_edges"]:
            raise ValueError("Incomplete native scene")
        report["header"] = {k: v for k, v in header.items() if k not in ("magic", "reserved")}
        n, ne = header["num_cells"], header["num_edges"]
        cell_dtype = np.dtype([("position", "<f8", (3,)), ("density", "<f4"), ("temperature", "<f4"), ("velocity", "<f4", (3,)), ("id", "<u8")])
        cells = np.memmap(scene, mode="r", dtype=cell_dtype, offset=208, shape=(n,))
        offsets = np.memmap(scene, mode="r", dtype="<u8", offset=208 + n * 52, shape=(n+1,))
        edges = np.memmap(scene, mode="r", dtype=np.dtype([("delta", "<f4", (3,)), ("neighbor", "<u4")]), offset=208+n*52+(n+1)*8, shape=(ne,))
        if offsets[0] or offsets[-1] != ne or np.any(offsets[1:] < offsets[:-1]):
            raise ValueError("Broken native offsets")
        report["snapshot_stat_before"] = stat(raw)
        arrays = {}
        with h5py.File(raw, "r") as f:
            report["snapshot_metadata"] = {key: dict(f[key].attrs) for key in ("Header", "Parameters", "Config") if key in f}
            gas = f["PartType0"]
            report["gas_datasets"] = {k: dict(shape=list(v.shape), dtype=str(v.dtype)) for k, v in gas.items() if isinstance(v, h5py.Dataset)}
            for key in ("ParticleIDs", "Coordinates", "Density", "Masses", "Volume"):
                if key in gas:
                    arrays[key] = gas[key][:]
            if "Masses" not in arrays:
                arrays["Masses"] = np.full(len(arrays["ParticleIDs"]), f["Header"].attrs["MassTable"][0], dtype=np.float64)
        report["snapshot_stat_after"] = stat(raw)
        report["snapshot_hdf5_opens"] = 1
        report["snapshot_payload_sha256"] = {k: hashlib.sha256(np.ascontiguousarray(v).view(np.uint8)).hexdigest() for k, v in arrays.items()}
        if report["snapshot_stat_before"] != report["snapshot_stat_after"]:
            raise RuntimeError("Snapshot changed while it was being read")
        raw_ids = arrays["ParticleIDs"].astype(np.uint64, copy=False)
        scene_ids = np.asarray(cells["id"])
        order = np.argsort(raw_ids)
        sorted_ids = raw_ids[order]
        duplicates_raw = int(np.count_nonzero(sorted_ids[1:] == sorted_ids[:-1]))
        scene_sorted = np.sort(scene_ids)
        duplicates_scene = int(np.count_nonzero(scene_sorted[1:] == scene_sorted[:-1]))
        slots = np.searchsorted(sorted_ids, scene_ids)
        valid = slots < len(sorted_ids)
        valid[valid] &= sorted_ids[slots[valid]] == scene_ids[valid]
        report["id_match"] = dict(snapshot_cells=len(raw_ids), scene_cells=n,
                                  raw_duplicates=duplicates_raw, scene_duplicates=duplicates_scene,
                                  missing_scene_ids=int(np.count_nonzero(~valid)))
        if not np.all(valid) or duplicates_raw or duplicates_scene or len(raw_ids) != n:
            raise RuntimeError("Full exact uint64 cell-ID match failed")
        match = order[slots]
        box = float(header["box"])
        coords = np.asarray(arrays["Coordinates"][match], dtype=np.float64)
        position_error = np.asarray(cells["position"]) - coords
        position_error -= np.rint(position_error/box)*box
        report["generator_error_cm"] = stats(np.linalg.norm(position_error, axis=1)*header["position_unit"])
        rho = np.asarray(arrays["Density"][match], dtype=np.float64)
        masses = np.asarray(arrays["Masses"][match], dtype=np.float64)
        if np.any(rho <= 0) or np.any(masses <= 0) or not np.all(np.isfinite(rho)) or not np.all(np.isfinite(masses)):
            raise RuntimeError("Nonpositive or nonfinite raw gas density/mass")
        expected_stored = np.log10(rho) + 10
        stored_error = np.asarray(cells["density"], dtype=np.float64) - expected_stored
        report["density_log10_error"] = stats(stored_error)
        report["density_relative_error"] = stats(np.expm1(stored_error*np.log(10)))
        volume = masses/rho
        report["volume_reference"] = "Masses / Density in snapshot code units"
        report["snapshot_volume_code_units"] = stats(volume)
        report["snapshot_mass_code_units"] = stats(masses)
        if "Volume" in arrays:
            report["stored_volume_over_mass_density"] = stats(arrays["Volume"][match]/volume)
        degree = np.diff(offsets).astype(np.int64)
        report["native_degree"] = stats(degree)
        report["stages"] = ["scene checksum and raw cell matching complete"]
        print(json.dumps(dict(stage="cell_match", ids=report["id_match"], generator_error_cm=report["generator_error_cm"], density_relative_error=report["density_relative_error"])), flush=True)

        # Every exported neighbour is checked against the corresponding raw
        # generator. This tests index binding and float32 periodic displacement.
        max_error = max_relative_error = max_edge_extent = 0.0
        bad_edges = zero_edges = invalid_targets = 0
        for lo in range(0, ne, 250000):
            hi = min(ne, lo+250000)
            owners = np.searchsorted(offsets, np.arange(lo, hi, dtype=np.uint64), side="right")-1
            target = (np.asarray(edges["neighbor"][lo:hi]) & np.uint32(0x7fffffff)).astype(np.int64)-1
            valid_edge = (target >= 0) & (target < n)
            invalid_targets += int(np.count_nonzero(~valid_edge))
            actual = np.asarray(edges["delta"][lo:hi], dtype=np.float64)[valid_edge]
            expected = coords[target[valid_edge]]-coords[owners[valid_edge]]
            expected -= np.rint(expected/box)*box
            error = np.abs(actual-expected)
            # Two float32 ULPs plus raw double precision subtraction roundoff.
            tolerance = 2*np.abs(np.spacing(expected.astype(np.float32))).astype(np.float64)+16*np.finfo(float).eps*box
            bad_edges += int(np.count_nonzero(np.any(error > tolerance, axis=1)))
            length = np.linalg.norm(actual, axis=1)
            zero_edges += int(np.count_nonzero(length == 0))
            max_error = max(max_error, float(error.max(initial=0)))
            max_relative_error = max(max_relative_error, float((np.linalg.norm(error, axis=1)/np.maximum(np.linalg.norm(expected, axis=1), 1e-300)).max(initial=0)))
            max_edge_extent = max(max_edge_extent, float(np.abs(actual).max(initial=0)/box))
        report["all_native_edges"] = dict(count=ne, invalid_targets=invalid_targets, zero_length=zero_edges,
                                          outside_float32_tolerance=bad_edges, max_component_error_code_units=max_error,
                                          max_relative_vector_error=max_relative_error, max_component_fraction_of_box=max_edge_extent)
        print(json.dumps(dict(stage="edge_check", **report["all_native_edges"])), flush=True)

        # Equal-population density and radius strata, with independent cells so
        # the unchanged builder does not suppress a face shared by two selections.
        rng = np.random.default_rng(20260903)
        log_rho = np.log10(rho)
        center = np.asarray(config["center_cm"], dtype=float)
        relative = coords*header["position_unit"]-center
        relative -= np.rint(relative/(box*header["position_unit"]))*box*header["position_unit"]
        radius = np.linalg.norm(relative, axis=1)
        candidates = []
        for label, values in (("density", log_rho), ("radius", radius)):
            limits = np.quantile(values, np.linspace(0, 1, 17))
            bins = np.searchsorted(limits[1:-1], values, side="right")
            for b in range(16):
                pool = np.flatnonzero(bins == b)
                if len(pool):
                    for cell in rng.choice(pool, min(24, len(pool)), replace=False):
                        candidates.append((int(cell), f"{label}_quantile_{b:02d}"))
        for label, values in (("density", rho), ("volume", volume), ("degree", degree)):
            rank = np.argsort(values)
            candidates.extend((int(cell), f"extreme_{label}") for cell in np.r_[rank[:16], rank[-16:]])
        blocked = np.zeros(n, dtype=bool)
        selected = []
        for cell, label in candidates:
            if blocked[cell]:
                continue
            neighbors = (edges["neighbor"][int(offsets[cell]):int(offsets[cell+1])] & np.uint32(0x7fffffff)).astype(np.int64)-1
            if np.any((neighbors < 0) | (neighbors >= n)):
                continue
            selected.append((cell, label))
            blocked[cell] = True
            blocked[neighbors] = True
        selected_ids = np.asarray([cell for cell, _ in selected], dtype=np.int64)
        mask = np.zeros(n, dtype=np.uint8)
        mask[selected_ids] = 1
        for cell in selected_ids:
            neighbors = (edges["neighbor"][int(offsets[cell]):int(offsets[cell+1])] & np.uint32(0x7fffffff)).astype(np.int64)-1
            if np.any(mask[neighbors]):
                raise RuntimeError("Sample includes adjacent cells; cannot compare incomplete face sets")
        mask.tofile(output/"selected_cells.u8")
        rows = []
        for cell, label in selected:
            row = dict(cell_index=cell, particle_id=str(int(scene_ids[cell])), stratum=label,
                       density=float(rho[cell]), mass=float(masses[cell]), reference_volume=float(volume[cell]),
                       radius_cm=float(radius[cell]), neighbors=int(degree[cell]))
            try:
                delta = np.asarray(edges["delta"][int(offsets[cell]):int(offsets[cell+1])], dtype=np.float64)
                actual, vertices = independent_volume(delta)
                row.update(halfspace_volume=actual, halfspace_over_reference=actual/volume[cell], halfspace_vertices=vertices)
            except Exception as e:
                row["halfspace_error"] = repr(e)
            rows.append(row)
        source = Path("native_mesh.cpp")
        report["native_builder_source_sha256"] = digest(source)
        if report["native_builder_source_sha256"] != config["native_builder_source_sha256"]:
            raise RuntimeError("Production native builder source hash changed")
        compiler = os.environ.get("CXX", "g++")
        report["compiler"] = subprocess.check_output([compiler, "--version"], text=True).splitlines()[0]
        build = subprocess.run([compiler, "-O3", "-std=c++17", "-pthread", str(source), "-o", str(output/"native_mesh")], capture_output=True, text=True, timeout=90)
        (output/"compile.log").write_text(build.stdout+build.stderr)
        build.check_returncode()
        report["native_builder_binary_sha256"] = digest(output/"native_mesh")
        mesh_path = output/"sample_mesh.bin"
        command = [str(output/"native_mesh"), str(scene), str(output/"selected_cells.u8"), str(mesh_path), *map(str, config["center_cm"]), str(config["radius_cm"]), os.environ.get("SLURM_CPUS_PER_TASK", "4"), "1", "3000000"]
        build = subprocess.run(command, capture_output=True, text=True, timeout=120)
        (output/"native_builder.log").write_text(build.stdout+build.stderr)
        build.check_returncode()
        report["native_builder"] = json.loads(build.stdout)
        with mesh_path.open("rb") as f:
            magic, nf, nv = struct.unpack("<16sQQ", f.read(32))
            if magic.rstrip(b"\0") != b"ACLMESH0001":
                raise ValueError("Unknown native mesh output")
            points = np.fromfile(f, dtype="<f4", count=3*nv).reshape(-1, 3).astype(float)
            face_offsets = np.fromfile(f, dtype="<u8", count=nf+1)
            owners = np.fromfile(f, dtype="<u4", count=nf)
        native_volumes = dict.fromkeys(map(int, selected_ids), 0.0)
        for face in range(nf):
            cell = int(owners[face])
            vertices = points[int(face_offsets[face]):int(face_offsets[face+1])]*config["radius_cm"]-relative[cell]
            a, b, c = vertices[0], vertices[1:-1], vertices[2:]
            native_volumes[cell] += float(np.sum(np.abs(np.einsum("j,ij->i", a, np.cross(b, c))))/6)/header["position_unit"]**3
        for row in rows:
            actual = native_volumes[row["cell_index"]]
            row["native_volume"] = actual
            row["native_over_reference"] = actual/row["reference_volume"]
            if "halfspace_volume" in row:
                row["native_over_halfspace"] = actual/row["halfspace_volume"]
        fields = sorted(set().union(*(row.keys() for row in rows)))
        with (output/"cell_volume_audit.csv").open("x", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        ratios = np.array([row["halfspace_over_reference"] for row in rows if "halfspace_over_reference" in row])
        report["volume_sample"] = dict(method="deterministic 16 density quantiles and 16 radius quantiles plus density/volume/degree extremes; adjacent cells excluded", cells=len(rows), errors=[row for row in rows if "halfspace_error" in row], halfspace_over_reference=stats(ratios), native_over_reference=stats([row["native_over_reference"] for row in rows]), native_over_halfspace=stats([row["native_over_halfspace"] for row in rows if "native_over_halfspace" in row]), halfspace_relative_error_over_0p001=int(np.count_nonzero(np.abs(ratios-1) > .001)), halfspace_relative_error_over_0p01=int(np.count_nonzero(np.abs(ratios-1) > .01)))
        # Compact actual refinement context, not a claim of radial uniformity.
        density_edges = np.linspace(log_rho.min(), log_rho.max(), 13)
        distribution = []
        for i, (left, right) in enumerate(zip(density_edges[:-1], density_edges[1:])):
            sel = (log_rho >= left) & ((log_rho <= right) if i == 11 else (log_rho < right))
            if np.any(sel):
                distribution.append(dict(log10_density_interval=[float(left), float(right)], cells=int(sel.sum()), mass=stats(masses[sel]), volume=stats(volume[sel]), equivalent_cell_width_cm=stats(np.cbrt(volume[sel])*header["position_unit"])))
        report["density_size_distribution"] = distribution
        report["checks"] = dict(full_ids=True,
            all_generators=np.max(np.abs(position_error)) <= 32*np.finfo(float).eps*box,
            density=np.max(np.abs(stored_error)) <= 2e-6,
            neighbor_binding=(invalid_targets == bad_edges == zero_edges == 0),
            sample_halfspace_volume=len(ratios) == len(rows) and bool(np.all(np.abs(ratios-1) <= .001)),
            sample_native_face_volume=all(abs(row["native_over_reference"]-1) <= .001 for row in rows))
        report["status"] = "PASS" if all(report["checks"].values()) else "FAIL"
    except Exception as e:
        report["status"] = "ERROR"
        report["exception"] = repr(e)
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic()-start
        (output/"report.json").write_text(json.dumps(report, indent=2, default=json_value)+"\n")
        print(json.dumps({key: report[key] for key in ("status", "checks", "volume_sample", "elapsed_seconds", "exception") if key in report}, default=json_value), flush=True)
    if report["status"] != "PASS":
        sys.exit(2)


if __name__ == "__main__":
    main()
