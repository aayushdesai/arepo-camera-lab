"""Select and compile a camera path with one continuous orbital direction.

Saved camera alternatives are exact views, but they are not an ordered orbit.
This module chooses one alternative per simulation epoch and parameterizes each
view around the physical axis carried by the v055 template.  Orbital phase is
then unwrapped in one requested direction and interpolated monotonically.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
from pathlib import Path
import sys

import numpy as np

from . import routes, spline


ORBIT_SCHEMA = "arepo_camera_orbit_v001"
DIRECTIONS = ("positive", "negative")


def _rotation_between(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Return the deterministic shortest rotation mapping left onto right."""
    left = spline._normalize(left, "left physical axis")
    right = spline._normalize(right, "right physical axis")
    cross = np.cross(left, right)
    sine = float(np.linalg.norm(cross))
    cosine = float(np.clip(np.dot(left, right), -1.0, 1.0))
    if sine < 1.0e-12:
        if cosine > 0.0:
            return np.eye(3)
        reference = np.zeros(3)
        reference[int(np.argmin(np.abs(left)))] = 1.0
        axis = spline._normalize(
            reference - np.dot(reference, left) * left,
            "antiparallel transport axis")
        return 2.0 * np.outer(axis, axis) - np.eye(3)
    skew = np.array([
        [0.0, -cross[2], cross[1]],
        [cross[2], 0.0, -cross[0]],
        [-cross[1], cross[0], 0.0],
    ])
    return np.eye(3) + skew + skew @ skew * ((1.0 - cosine) / (sine * sine))


def transported_axis_frames(
        template_rows: list[list[float]]) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Build a twist-minimizing equatorial basis along the physical axis."""
    snapshots = [int(row[0]) for row in template_rows]
    axes = [spline._normalize(np.asarray(row[15:18], dtype=float),
                              "physical axis") for row in template_rows]
    reference = np.zeros(3)
    reference[int(np.argmin(np.abs(axes[0])))] = 1.0
    first = spline._normalize(
        reference - np.dot(reference, axes[0]) * axes[0],
        "initial orbit reference")
    frames: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    tangent = first
    previous_axis = axes[0]
    for snapshot, axis in zip(snapshots, axes):
        if snapshot != snapshots[0]:
            tangent = _rotation_between(previous_axis, axis) @ tangent
            tangent = spline._normalize(
                tangent - np.dot(tangent, axis) * axis,
                "transported orbit reference")
        bitangent = spline._normalize(np.cross(axis, tangent),
                                      "orbit bitangent")
        frames[snapshot] = (axis, tangent.copy(), bitangent)
        previous_axis = axis
    return frames


def _pose_coordinates(
        pose: dict,
        frame: tuple[np.ndarray, np.ndarray, np.ndarray]) -> tuple[float, float]:
    axis, tangent, bitangent = frame
    radial = -spline._normalize(
        np.asarray(pose["view_direction"], dtype=float), "pose view")
    axial = float(np.clip(np.dot(radial, axis), -1.0, 1.0))
    planar = radial - axial * axis
    planar_length = float(np.linalg.norm(planar))
    if planar_length < 1.0e-6:
        raise ValueError(
            f"pose {pose.get('pose_id', '<unknown>')} is too close to the "
            "physical axis for a stable orbital phase")
    planar /= planar_length
    phase = math.atan2(float(np.dot(planar, bitangent)),
                       float(np.dot(planar, tangent))) % (2.0 * math.pi)
    return phase, math.asin(axial)


def _directional_delta(left: float, right: float, direction: str) -> float:
    if direction == "positive":
        return (right - left) % (2.0 * math.pi)
    if direction == "negative":
        return -((left - right) % (2.0 * math.pi))
    raise ValueError(f"unsupported orbit direction: {direction}")


def _orbit_edge_cost(left: dict, right: dict, direction: str,
                     total_span: int, target_turns: float) -> tuple[float, float]:
    span = int(right["snapshot"]) - int(left["snapshot"])
    delta = _directional_delta(
        float(left["orbit_phase_wrapped_radians"]),
        float(right["orbit_phase_wrapped_radians"]), direction)
    target = ((1.0 if direction == "positive" else -1.0) *
              target_turns * 2.0 * math.pi * span / total_span)
    phase_cost = ((delta - target) / span) ** 2 * span
    long_arc = max(0.0, abs(delta) - math.pi) / math.pi
    scale_cost = (
        math.log(float(right["screen_half_extent_cm"]) /
                 float(left["screen_half_extent_cm"])) / span) ** 2 * span * 25.0
    reference_scale = math.sqrt(
        float(left["screen_half_extent_cm"]) *
        float(right["screen_half_extent_cm"]))
    look_shift = float(np.linalg.norm(
        np.asarray(right["look_at_cm"], dtype=float) -
        np.asarray(left["look_at_cm"], dtype=float))) / reference_scale
    elevation_rate = (
        (float(right["orbit_elevation_radians"]) -
         float(left["orbit_elevation_radians"])) / span)
    return (phase_cost + 10.0 * long_arc * long_arc + scale_cost +
            look_shift * look_shift / span +
            elevation_rate * elevation_rate * span), delta


def _reference_composition_cost(pose: dict, reference: list[float]) -> float:
    """Keep the accepted target and framing while selecting orbit alternatives."""
    reference_scale = float(reference[11])
    scale_error = math.log(
        float(pose["screen_half_extent_cm"]) / reference_scale)
    look_error = float(np.linalg.norm(
        np.asarray(pose["look_at_cm"], dtype=float) -
        np.asarray(reference[5:8], dtype=float))) / reference_scale
    return scale_error * scale_error + look_error * look_error


def select_orbit_route(
        grouped: dict[int, list[dict]],
        frames: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]],
        omitted: set[int], direction: str,
        target_turns: float,
        reference_rows: dict[int, list[float]] | None = None
        ) -> tuple[list[dict], list[dict]]:
    """Select exact alternatives with monotonic, low-acceleration orbital phase."""
    if direction not in DIRECTIONS:
        raise ValueError(f"orbit direction must be one of {', '.join(DIRECTIONS)}")
    if not math.isfinite(target_turns) or target_turns <= 0.0:
        raise ValueError("target orbit turns must be finite and positive")
    snapshots = [snapshot for snapshot in grouped if snapshot not in omitted]
    if len(snapshots) < 2:
        raise ValueError("orbit route must retain at least two snapshots")
    if any(snapshot not in frames for snapshot in snapshots):
        raise ValueError("orbit pose snapshot is absent from the v055 template")
    candidates: dict[int, list[dict]] = {}
    for snapshot in snapshots:
        candidates[snapshot] = []
        for source in grouped[snapshot]:
            pose = copy.deepcopy(source)
            phase, elevation = _pose_coordinates(pose, frames[snapshot])
            pose["orbit_phase_wrapped_radians"] = phase
            pose["orbit_elevation_radians"] = elevation
            candidates[snapshot].append(pose)

    total_span = snapshots[-1] - snapshots[0]
    if reference_rows is not None and any(
            snapshot not in reference_rows for snapshot in snapshots):
        raise ValueError("orbit reference is missing a selected snapshot")
    costs = [
        _reference_composition_cost(pose, reference_rows[snapshots[0]])
        if reference_rows is not None else 0.0
        for pose in candidates[snapshots[0]]
    ]
    parents: list[list[int]] = []
    increments: list[list[float]] = []
    for left_snapshot, right_snapshot in zip(snapshots, snapshots[1:]):
        next_costs: list[float] = []
        next_parents: list[int] = []
        next_increments: list[float] = []
        for right in candidates[right_snapshot]:
            options = []
            for index, left in enumerate(candidates[left_snapshot]):
                edge_cost, increment = _orbit_edge_cost(
                    left, right, direction, total_span, target_turns)
                composition_cost = (
                    _reference_composition_cost(
                        right, reference_rows[right_snapshot])
                    if reference_rows is not None else 0.0)
                options.append((costs[index] + edge_cost + composition_cost,
                                index, increment))
            cost, parent, increment = min(options)
            next_costs.append(cost)
            next_parents.append(parent)
            next_increments.append(increment)
        costs = next_costs
        parents.append(next_parents)
        increments.append(next_increments)

    index = int(np.argmin(costs))
    selected = [candidates[snapshots[-1]][index]]
    selected_increments: list[float] = []
    for level in range(len(parents) - 1, -1, -1):
        selected_increments.append(increments[level][index])
        index = parents[level][index]
        selected.append(candidates[snapshots[level]][index])
    selected.reverse()
    selected_increments.reverse()

    phase = float(selected[0]["orbit_phase_wrapped_radians"])
    selected[0]["orbit_phase_radians"] = phase
    diagnostics: list[dict] = []
    for left, right, increment in zip(
            selected, selected[1:], selected_increments):
        phase += increment
        right["orbit_phase_radians"] = phase
        diagnostics.append({
            "left_snapshot": int(left["snapshot"]),
            "right_snapshot": int(right["snapshot"]),
            "left_pose_id": left.get("pose_id"),
            "right_pose_id": right.get("pose_id"),
            "orbit_direction": direction,
            "orbit_phase_increment_degrees": math.degrees(increment),
            "orbit_turns_cumulative": (
                (phase - float(selected[0]["orbit_phase_radians"])) /
                (2.0 * math.pi)),
        })
    return selected, diagnostics


def _natural_up(forward: np.ndarray, axis: np.ndarray,
                tangent: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    candidate = axis - np.dot(axis, forward) * forward
    if float(np.linalg.norm(candidate)) < 1.0e-8:
        candidate = tangent - np.dot(tangent, forward) * forward
    up = spline._normalize(candidate, "orbit natural up")
    right = spline._normalize(np.cross(forward, up), "orbit natural right")
    up = spline._normalize(np.cross(right, forward), "orbit natural up")
    return up, right


def compile_orbit_spline(
        template_rows: list[list[float]], keyframes: list[dict],
        tension: float, direction: str) -> tuple[list[list[float]], list[dict]]:
    """Compile a physical-axis orbit while exactly retaining selected knots."""
    frames = transported_axis_frames(template_rows)
    x = np.asarray([float(pose["snapshot"]) for pose in keyframes])
    look = np.asarray([pose["look_at_cm"] for pose in keyframes], dtype=float)
    scale = np.asarray([float(pose["screen_half_extent_cm"])
                        for pose in keyframes])
    phase = np.asarray([float(pose["orbit_phase_radians"])
                        for pose in keyframes])
    elevation = np.asarray([float(pose["orbit_elevation_radians"])
                            for pose in keyframes])
    sign = 1.0 if direction == "positive" else -1.0
    if np.any(np.diff(phase) * sign < -1.0e-12):
        raise ValueError("selected orbit phases reverse direction")
    template_snapshots = np.asarray([row[0] for row in template_rows])
    if template_snapshots[0] < x[0] or template_snapshots[-1] > x[-1]:
        raise ValueError("orbit keyframes must span the complete template")

    look_tangents = spline._catmull_tangents(x, look, tension)
    log_scale = np.log(scale)
    scale_tangents = spline._pchip_tangents(x, log_scale)
    phase_tangents = spline._pchip_tangents(x, phase)
    elevation_tangents = spline._pchip_tangents(x, elevation)
    output: list[list[float]] = []
    diagnostics: list[dict] = []
    previous_target = None
    previous_scale = None
    previous_quaternion = None
    previous_phase = None
    for template in template_rows:
        snapshot = float(template[0])
        index = spline._segment(x, snapshot)
        target = spline._hermite(
            x[index], x[index + 1], look[index], look[index + 1],
            look_tangents[index], look_tangents[index + 1], snapshot)
        log_half_extent = float(spline._hermite(
            x[index], x[index + 1], np.asarray(log_scale[index]),
            np.asarray(log_scale[index + 1]),
            np.asarray(scale_tangents[index]),
            np.asarray(scale_tangents[index + 1]), snapshot))
        half_extent = math.exp(log_half_extent)
        orbit_phase = float(spline._hermite(
            x[index], x[index + 1], np.asarray(phase[index]),
            np.asarray(phase[index + 1]), np.asarray(phase_tangents[index]),
            np.asarray(phase_tangents[index + 1]), snapshot))
        orbit_elevation = float(spline._hermite(
            x[index], x[index + 1], np.asarray(elevation[index]),
            np.asarray(elevation[index + 1]),
            np.asarray(elevation_tangents[index]),
            np.asarray(elevation_tangents[index + 1]), snapshot))
        axis, tangent, bitangent = frames[int(snapshot)]
        planar = math.cos(orbit_phase) * tangent + math.sin(orbit_phase) * bitangent
        radial = spline._normalize(
            math.cos(orbit_elevation) * planar +
            math.sin(orbit_elevation) * axis, "orbit radial")
        forward = -radial
        # Saved alternatives are independent compositions, so their roll
        # values are not an ordered trajectory.  Align the horizon with the
        # transported physical axis to avoid interpolating arbitrary roll
        # reversals into an otherwise coherent orbit.
        up, _ = _natural_up(forward, axis, tangent)
        position = target - forward * (4.0 * half_extent)
        quaternion = spline._matrix_to_quat(
            spline._orthonormal_basis(forward, up))

        row = template.copy()
        row[2:5] = position.tolist()
        row[5:8] = target.tolist()
        row[8:11] = up.tolist()
        row[11] = half_extent
        output.append(row)
        if previous_target is None:
            pan = zoom = orientation = phase_delta = 0.0
        else:
            pan = float(np.linalg.norm(target - previous_target) / previous_scale)
            zoom = abs(half_extent / previous_scale - 1.0) * 100.0
            orientation = math.degrees(2.0 * math.acos(min(
                1.0, abs(float(np.dot(previous_quaternion, quaternion))))))
            phase_delta = math.degrees(orbit_phase - previous_phase)
            if phase_delta * sign < -1.0e-7:
                raise ValueError(
                    f"compiled orbit reverses at snapshot {int(snapshot)}")
        diagnostics.append({
            "snapshot": int(snapshot),
            "segment_left": int(x[index]),
            "segment_right": int(x[index + 1]),
            "pan_fraction_per_frame": pan,
            "zoom_percent_per_frame": zoom,
            "orientation_deg_per_frame": orientation,
            "orbit_phase_degrees": math.degrees(orbit_phase),
            "orbit_phase_delta_degrees": phase_delta,
            "orbit_elevation_degrees": math.degrees(orbit_elevation),
            "screen_half_extent_cm": half_extent,
        })
        previous_target = target
        previous_scale = half_extent
        previous_quaternion = quaternion
        previous_phase = orbit_phase
    return output, diagnostics


def _write_route(path: Path, source: Path, template: Path, poses: list[dict],
                 diagnostics: list[dict], omitted: set[int], direction: str,
                 target_turns: float) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    payload = {
        "schema": spline.SCHEMA,
        "keyframes": poses,
        "orbit": {
            "schema": ORBIT_SCHEMA,
            "direction": direction,
            "target_turns": target_turns,
            "actual_turns": diagnostics[-1]["orbit_turns_cumulative"],
            "omitted_snapshots": sorted(omitted),
            "source_pose_bundle": str(source.resolve()),
            "source_pose_bundle_sha256": routes.sha256(source.resolve()),
            "template": str(template.resolve()),
            "template_sha256": routes.sha256(template.resolve()),
            "pose_ids": [pose.get("pose_id") for pose in poses],
        },
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, allow_nan=False)
        handle.write("\n")


def write_camera_path(path: Path, rows: list[list[float]], route_sha: str,
                      template_sha: str) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    with path.open("x", encoding="utf-8") as handle:
        handle.write("# schema=stellar_camera_path_v055\n")
        handle.write("# generated_by=arepo_camera_lab.orbit\n")
        handle.write("# interpolation=physical_axis_monotonic_orbit_v001,"
                     "catmull_hermite_target,pchip_log_scale\n")
        handle.write(f"# orbit_route_sha256={route_sha}\n")
        handle.write(f"# template_sha256={template_sha}\n")
        for row in rows:
            fields = [str(int(row[0]))] + [format(value, ".17g")
                                           for value in row[1:]]
            handle.write(" ".join(fields) + "\n")


def _write_diagnostics(path: Path, rows: list[dict]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> int:
    source = args.poses.expanduser().resolve()
    template_path = args.template.expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != spline.SCHEMA:
        raise ValueError(f"expected pose schema {spline.SCHEMA}")
    grouped = routes._group_poses(payload)
    template = spline.read_template(template_path)
    frames = transported_axis_frames(template)
    omitted = set(args.omit)
    poses, route_diagnostics = select_orbit_route(
        grouped, frames, omitted, args.direction, args.target_turns,
        {int(row[0]): row for row in template})
    _write_route(args.output_route, source, template_path, poses,
                 route_diagnostics, omitted, args.direction,
                 args.target_turns)
    rows, diagnostics = compile_orbit_spline(
        template, poses, args.tension, args.direction)
    write_camera_path(
        args.output, rows, routes.sha256(args.output_route),
        routes.sha256(template_path))
    _write_diagnostics(args.diagnostics, diagnostics)
    maximum = max(abs(row["orbit_phase_delta_degrees"])
                  for row in diagnostics)
    print(
        "AREPO_CAMERA_MONOTONIC_ORBIT_OK "
        f"rows={len(rows)} poses={len(poses)} direction={args.direction} "
        f"turns={route_diagnostics[-1]['orbit_turns_cumulative']:.9g} "
        f"max_phase_step_deg={maximum:.9g}")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--poses", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-route", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--omit", type=int, action="append", default=[])
    parser.add_argument("--direction", choices=DIRECTIONS, default="positive")
    parser.add_argument("--target-turns", type=float, default=1.0)
    parser.add_argument("--tension", type=float, default=0.25)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    if not 0.0 <= args.tension <= 1.0:
        parser.error("--tension must lie in [0,1]")
    try:
        return run(args)
    except (FileExistsError, OSError, ValueError, KeyError) as error:
        print(f"arepo-camera-orbit: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
