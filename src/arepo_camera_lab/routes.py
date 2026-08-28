"""Select reviewable camera routes from a multi-pose camera-lab bundle.

The browser preserves every no-clobber camera save in ``alternatives`` while
``keyframes`` contains the latest pose at each simulation snapshot. This tool
never modifies that source bundle. It writes an exact latest-pose diagnostic
and two smoothest routes that explicitly omit either member of a requested
adjacent-snapshot conflict.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Iterable

import numpy as np

from .spline import SCHEMA, _matrix_to_quat, _orthonormal_basis


ROUTE_SCHEMA = "arepo_camera_routes_v001"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pose_quaternion(pose: dict) -> np.ndarray:
    return _matrix_to_quat(_orthonormal_basis(
        pose["view_direction"], pose["up"]))


def _transition(left: dict, right: dict) -> dict:
    left_snapshot = int(left["snapshot"])
    right_snapshot = int(right["snapshot"])
    span = right_snapshot - left_snapshot
    if span <= 0:
        raise ValueError("route snapshots must increase")
    left_q = _pose_quaternion(left)
    right_q = _pose_quaternion(right)
    dot = min(1.0, abs(float(np.dot(left_q, right_q))))
    orientation = math.degrees(2.0 * math.acos(dot))
    left_scale = float(left["screen_half_extent_cm"])
    right_scale = float(right["screen_half_extent_cm"])
    if not left_scale > 0.0 or not right_scale > 0.0:
        raise ValueError("pose half extents must be positive")
    log_scale_change = abs(math.log(right_scale / left_scale))
    look_delta = float(np.linalg.norm(
        np.asarray(right["look_at_cm"], dtype=float) -
        np.asarray(left["look_at_cm"], dtype=float)))
    reference_scale = math.sqrt(left_scale * right_scale)
    return {
        "left_snapshot": left_snapshot,
        "right_snapshot": right_snapshot,
        "snapshot_span": span,
        "orientation_change_deg": orientation,
        "orientation_deg_per_snapshot": orientation / span,
        "scale_ratio": right_scale / left_scale,
        "log_scale_change": log_scale_change,
        "log_scale_percent_per_snapshot": 100.0 * math.expm1(log_scale_change / span),
        "look_at_shift_fraction": look_delta / reference_scale,
        "look_at_shift_fraction_per_snapshot": look_delta / reference_scale / span,
    }


def _edge_cost(left: dict, right: dict) -> float:
    row = _transition(left, right)
    # Normalize by the camera-lab motion budgets. Squaring rejects a short,
    # violent spin while permitting a large move distributed over time.
    return (
        (row["orientation_deg_per_snapshot"] / 0.25) ** 2 +
        (row["log_scale_percent_per_snapshot"] / 1.0) ** 2 +
        (row["look_at_shift_fraction_per_snapshot"] / 0.01) ** 2
    ) * row["snapshot_span"]


def _group_poses(payload: dict) -> dict[int, list[dict]]:
    raw = payload.get("alternatives") or payload.get("keyframes")
    if not isinstance(raw, list) or not raw:
        raise ValueError("pose bundle has no alternatives or keyframes")
    grouped: dict[int, list[dict]] = {}
    identities: set[str] = set()
    for pose in raw:
        snapshot = int(pose["snapshot"])
        identity = str(pose.get("pose_id") or json.dumps(
            pose, sort_keys=True, separators=(",", ":")))
        if identity in identities:
            continue
        identities.add(identity)
        grouped.setdefault(snapshot, []).append(pose)
    for poses in grouped.values():
        poses.sort(key=lambda pose: (
            str(pose.get("saved_at", "")), str(pose.get("pose_id", ""))))
    return dict(sorted(grouped.items()))


def _validate_latest(payload: dict, grouped: dict[int, list[dict]]) -> list[dict]:
    latest = payload.get("keyframes")
    if not isinstance(latest, list) or len(latest) < 2:
        raise ValueError("pose bundle needs at least two latest keyframes")
    snapshots = [int(pose["snapshot"]) for pose in latest]
    if snapshots != sorted(grouped):
        raise ValueError(
            "latest keyframes must cover exactly the alternative snapshots")
    for pose in latest:
        pose_id = pose.get("pose_id")
        candidates = grouped[int(pose["snapshot"])]
        if pose_id is not None and not any(
                candidate.get("pose_id") == pose_id for candidate in candidates):
            raise ValueError("latest keyframe is absent from alternatives")
    return latest


def smoothest_route(grouped: dict[int, list[dict]],
                    omitted: Iterable[int] = ()) -> list[dict]:
    omitted_set = set(omitted)
    snapshots = [snapshot for snapshot in grouped if snapshot not in omitted_set]
    if len(snapshots) < 2:
        raise ValueError("route must retain at least two snapshots")
    costs = [0.0] * len(grouped[snapshots[0]])
    parents: list[list[int]] = []
    for left_snapshot, right_snapshot in zip(snapshots, snapshots[1:]):
        left_poses = grouped[left_snapshot]
        right_poses = grouped[right_snapshot]
        next_costs: list[float] = []
        next_parents: list[int] = []
        for right in right_poses:
            candidates = [
                costs[index] + _edge_cost(left, right)
                for index, left in enumerate(left_poses)
            ]
            parent = int(np.argmin(candidates))
            next_costs.append(candidates[parent])
            next_parents.append(parent)
        costs = next_costs
        parents.append(next_parents)
    index = int(np.argmin(costs))
    selected = [grouped[snapshots[-1]][index]]
    for level in range(len(parents) - 1, -1, -1):
        index = parents[level][index]
        selected.append(grouped[snapshots[level]][index])
    selected.reverse()
    return selected


def route_diagnostics(poses: list[dict]) -> list[dict]:
    return [_transition(left, right) for left, right in zip(poses, poses[1:])]


def _route_payload(source: Path, source_sha256: str, name: str,
                   poses: list[dict], omitted: list[int]) -> dict:
    return {
        "schema": SCHEMA,
        "keyframes": poses,
        "route": {
            "schema": ROUTE_SCHEMA,
            "name": name,
            "source_pose_bundle": str(source),
            "source_pose_bundle_sha256": source_sha256,
            "omitted_snapshots": omitted,
            "pose_ids": [pose.get("pose_id") for pose in poses],
        },
    }


def _write_json(path: Path, payload: dict) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, allow_nan=False)
        handle.write("\n")


def write_routes(source: Path, output_directory: Path,
                 conflict: tuple[int, int]) -> dict:
    source = source.expanduser().resolve()
    output_directory = output_directory.expanduser().resolve()
    if output_directory.exists():
        raise FileExistsError(f"refusing to overwrite {output_directory}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"expected pose schema {SCHEMA}")
    grouped = _group_poses(payload)
    latest = _validate_latest(payload, grouped)
    for snapshot in conflict:
        if snapshot not in grouped:
            raise ValueError(f"conflict snapshot {snapshot} is absent")
    routes = {
        "exact_latest_all_knots": (latest, []),
        f"continuous_drop_{conflict[0]}": (
            smoothest_route(grouped, [conflict[0]]), [conflict[0]]),
        f"continuous_drop_{conflict[1]}": (
            smoothest_route(grouped, [conflict[1]]), [conflict[1]]),
    }
    source_digest = sha256(source)
    output_directory.mkdir(parents=True)
    report = {
        "schema": ROUTE_SCHEMA,
        "source_pose_bundle": str(source),
        "source_pose_bundle_sha256": source_digest,
        "alternative_pose_count": sum(len(poses) for poses in grouped.values()),
        "snapshot_count": len(grouped),
        "conflict_snapshots": list(conflict),
        "routes": {},
    }
    diagnostics_rows: list[dict] = []
    for name, (poses, omitted) in routes.items():
        route_path = output_directory / f"{name}.json"
        _write_json(
            route_path,
            _route_payload(source, source_digest, name, poses, omitted))
        diagnostics = route_diagnostics(poses)
        for row in diagnostics:
            diagnostics_rows.append({"route": name, **row})
        report["routes"][name] = {
            "path": str(route_path),
            "sha256": sha256(route_path),
            "snapshots": [int(pose["snapshot"]) for pose in poses],
            "pose_ids": [pose.get("pose_id") for pose in poses],
            "omitted_snapshots": omitted,
            "worst_orientation_deg_per_snapshot": max(
                row["orientation_deg_per_snapshot"] for row in diagnostics),
            "worst_log_scale_percent_per_snapshot": max(
                row["log_scale_percent_per_snapshot"] for row in diagnostics),
            "worst_look_at_shift_fraction_per_snapshot": max(
                row["look_at_shift_fraction_per_snapshot"] for row in diagnostics),
        }
    diagnostics_path = output_directory / "route_transition_diagnostics.tsv"
    with diagnostics_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(diagnostics_rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(diagnostics_rows)
    report["diagnostics"] = {
        "path": str(diagnostics_path),
        "sha256": sha256(diagnostics_path),
    }
    report_path = output_directory / "route_selection_report.json"
    _write_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poses", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--conflict", type=int, nargs=2, default=(820, 821),
        metavar=("LEFT_SNAPSHOT", "RIGHT_SNAPSHOT"))
    args = parser.parse_args(argv)
    try:
        report = write_routes(
            args.poses, args.output_directory, tuple(args.conflict))
    except (FileExistsError, OSError, ValueError, KeyError) as error:
        print(f"arepo-camera-routes: {error}", file=sys.stderr)
        return 1
    print(
        "AREPO_CAMERA_ROUTES_OK "
        f"alternatives={report['alternative_pose_count']} "
        f"snapshots={report['snapshot_count']} routes={len(report['routes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
