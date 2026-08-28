#!/usr/bin/env python3
"""Compile interactive camera keyframes into a smooth v055 camera path.

This utility deliberately depends only on NumPy. It interpolates look-at points
with a cubic Hermite spline, orientation with quaternion SQUAD, and orthographic
half extent with a shape-preserving cubic spline in log space. Simulation time,
physical center/axis, and landmark extents are copied from an existing v055
template path.
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


SCHEMA = "stellar_camera_keyframes_v001"
PATH_COLUMNS = 21


def _normalize(vector: np.ndarray, name: str) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError(f"{name} must be a finite nonzero vector")
    return vector / norm


def _orthonormal_basis(view: Iterable[float], up: Iterable[float]) -> np.ndarray:
    forward = _normalize(np.asarray(view, dtype=float), "view direction")
    supplied_up = _normalize(np.asarray(up, dtype=float), "up")
    right = _normalize(np.cross(forward, supplied_up), "camera right")
    corrected_up = _normalize(np.cross(right, forward), "camera up")
    # Columns map camera-local right, up, back into world coordinates.
    return np.column_stack((right, corrected_up, -forward))


def _quat_normalize(q: np.ndarray) -> np.ndarray:
    return _normalize(np.asarray(q, dtype=float), "quaternion")


def _matrix_to_quat(matrix: np.ndarray) -> np.ndarray:
    m = np.asarray(matrix, dtype=float)
    trace = float(np.trace(m))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        q = np.array([
            0.25 * s,
            (m[2, 1] - m[1, 2]) / s,
            (m[0, 2] - m[2, 0]) / s,
            (m[1, 0] - m[0, 1]) / s,
        ])
    else:
        index = int(np.argmax(np.diag(m)))
        if index == 0:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
            q = np.array([
                (m[2, 1] - m[1, 2]) / s,
                0.25 * s,
                (m[0, 1] + m[1, 0]) / s,
                (m[0, 2] + m[2, 0]) / s,
            ])
        elif index == 1:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
            q = np.array([
                (m[0, 2] - m[2, 0]) / s,
                (m[0, 1] + m[1, 0]) / s,
                0.25 * s,
                (m[1, 2] + m[2, 1]) / s,
            ])
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
            q = np.array([
                (m[1, 0] - m[0, 1]) / s,
                (m[0, 2] + m[2, 0]) / s,
                (m[1, 2] + m[2, 1]) / s,
                0.25 * s,
            ])
    return _quat_normalize(q)


def _quat_to_matrix(q: np.ndarray) -> np.ndarray:
    w, x, y, z = _quat_normalize(q)
    return np.array([
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w),
         2.0 * (x * z + y * w)],
        [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z),
         2.0 * (y * z - x * w)],
        [2.0 * (x * z - y * w), 2.0 * (y * z + x * w),
         1.0 - 2.0 * (x * x + y * y)],
    ])


def _quat_conjugate(q: np.ndarray) -> np.ndarray:
    return np.array([q[0], -q[1], -q[2], -q[3]], dtype=float)


def _quat_multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ])


def _quat_log(q: np.ndarray) -> np.ndarray:
    q = _quat_normalize(q)
    vector = q[1:]
    length = float(np.linalg.norm(vector))
    if length < 1.0e-15:
        return np.zeros(3)
    angle = math.atan2(length, float(q[0]))
    return vector * (angle / length)


def _quat_exp(vector: np.ndarray) -> np.ndarray:
    angle = float(np.linalg.norm(vector))
    if angle < 1.0e-15:
        return np.array([1.0, 0.0, 0.0, 0.0])
    return np.concatenate(([math.cos(angle)], vector * (math.sin(angle) / angle)))


def _slerp(a: np.ndarray, b: np.ndarray, fraction: float) -> np.ndarray:
    a = _quat_normalize(a)
    b = _quat_normalize(b)
    dot = float(np.dot(a, b))
    if dot < 0.0:
        b = -b
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        return _quat_normalize(a + fraction * (b - a))
    angle = math.acos(dot)
    denominator = math.sin(angle)
    return _quat_normalize(
        math.sin((1.0 - fraction) * angle) / denominator * a +
        math.sin(fraction * angle) / denominator * b)


def _squad_controls(quaternions: list[np.ndarray]) -> list[np.ndarray]:
    controls = [quaternions[0].copy()]
    for index in range(1, len(quaternions) - 1):
        current = quaternions[index]
        inverse = _quat_conjugate(current)
        left = _quat_log(_quat_multiply(inverse, quaternions[index - 1]))
        right = _quat_log(_quat_multiply(inverse, quaternions[index + 1]))
        controls.append(_quat_normalize(_quat_multiply(
            current, _quat_exp(-0.25 * (left + right)))))
    controls.append(quaternions[-1].copy())
    return controls


def _squad(a: np.ndarray, b: np.ndarray, sa: np.ndarray, sb: np.ndarray,
           fraction: float) -> np.ndarray:
    return _slerp(_slerp(a, b, fraction), _slerp(sa, sb, fraction),
                  2.0 * fraction * (1.0 - fraction))


def _catmull_tangents(x: np.ndarray, values: np.ndarray,
                      tension: float) -> np.ndarray:
    tangents = np.empty_like(values)
    tangents[0] = (values[1] - values[0]) / (x[1] - x[0])
    tangents[-1] = (values[-1] - values[-2]) / (x[-1] - x[-2])
    for index in range(1, len(x) - 1):
        tangents[index] = ((values[index + 1] - values[index - 1]) /
                           (x[index + 1] - x[index - 1]))
    return tangents * (1.0 - tension)


def _pchip_tangents(x: np.ndarray, values: np.ndarray) -> np.ndarray:
    count = len(x)
    if count == 2:
        slope = (values[1] - values[0]) / (x[1] - x[0])
        return np.array([slope, slope])
    widths = np.diff(x)
    slopes = np.diff(values) / widths
    tangents = np.zeros(count)
    for index in range(1, count - 1):
        if slopes[index - 1] * slopes[index] <= 0.0:
            tangents[index] = 0.0
        else:
            w1 = 2.0 * widths[index] + widths[index - 1]
            w2 = widths[index] + 2.0 * widths[index - 1]
            tangents[index] = (w1 + w2) / (w1 / slopes[index - 1] +
                                            w2 / slopes[index])

    def endpoint(h0: float, h1: float, d0: float, d1: float) -> float:
        value = ((2.0 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if value * d0 <= 0.0:
            return 0.0
        if d0 * d1 < 0.0 and abs(value) > abs(3.0 * d0):
            return 3.0 * d0
        return value

    tangents[0] = endpoint(widths[0], widths[1], slopes[0], slopes[1])
    tangents[-1] = endpoint(widths[-1], widths[-2], slopes[-1], slopes[-2])
    return tangents


def _hermite(x0: float, x1: float, y0: np.ndarray, y1: np.ndarray,
             m0: np.ndarray, m1: np.ndarray, x: float) -> np.ndarray:
    width = x1 - x0
    t = (x - x0) / width
    t2 = t * t
    t3 = t2 * t
    return ((2.0 * t3 - 3.0 * t2 + 1.0) * y0 +
            (t3 - 2.0 * t2 + t) * width * m0 +
            (-2.0 * t3 + 3.0 * t2) * y1 +
            (t3 - t2) * width * m1)


def _segment(x: np.ndarray, value: float) -> int:
    index = int(np.searchsorted(x, value, side="right") - 1)
    return min(max(index, 0), len(x) - 2)


def read_template(path: Path) -> list[list[float]]:
    rows: list[list[float]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) != PATH_COLUMNS:
                raise ValueError(
                    f"{path}:{line_number}: expected {PATH_COLUMNS} columns, "
                    f"found {len(fields)}")
            row = [float(field) for field in fields]
            if not row[0].is_integer():
                raise ValueError(f"{path}:{line_number}: noninteger snapshot")
            rows.append(row)
    if not rows:
        raise ValueError("template camera path is empty")
    snapshots = [int(row[0]) for row in rows]
    if any(right <= left for left, right in zip(snapshots, snapshots[1:])):
        raise ValueError("template snapshots must increase")
    return rows


def read_keyframes(path: Path) -> tuple[dict, list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"expected keyframe schema {SCHEMA}")
    keyframes = payload.get("keyframes")
    if not isinstance(keyframes, list) or not keyframes:
        raise ValueError(f"{path}: at least one camera keyframe is required")
    snapshots = [int(keyframe["snapshot"]) for keyframe in keyframes]
    if any(right <= left for left, right in zip(snapshots, snapshots[1:])):
        raise ValueError(f"{path}: keyframe snapshots must increase")
    return payload, keyframes


def read_keyframe_files(paths: list[Path]) -> list[dict]:
    combined: list[dict] = []
    for path in paths:
        _, keyframes = read_keyframes(path)
        combined.extend(keyframes)
    combined.sort(key=lambda keyframe: int(keyframe["snapshot"]))
    if len(combined) < 2:
        raise ValueError("at least two camera keyframes are required in total")
    snapshots = [int(keyframe["snapshot"]) for keyframe in combined]
    if any(right <= left for left, right in zip(snapshots, snapshots[1:])):
        raise ValueError("combined keyframe snapshots must be unique")
    return combined


def compile_spline(template_rows: list[list[float]], keyframes: list[dict],
                   tension: float) -> tuple[list[list[float]], list[dict]]:
    x = np.asarray([float(keyframe["snapshot"]) for keyframe in keyframes])
    look = np.asarray([keyframe["look_at_cm"] for keyframe in keyframes],
                      dtype=float)
    scale = np.asarray([float(keyframe["screen_half_extent_cm"])
                        for keyframe in keyframes])
    if not np.all(np.isfinite(look)) or not np.all(np.isfinite(scale)) or \
       np.any(scale <= 0.0):
        raise ValueError("keyframe look-at and scale values must be finite")
    quaternions: list[np.ndarray] = []
    for keyframe in keyframes:
        quaternion = _matrix_to_quat(_orthonormal_basis(
            keyframe["view_direction"], keyframe["up"]))
        if quaternions and np.dot(quaternions[-1], quaternion) < 0.0:
            quaternion = -quaternion
        quaternions.append(quaternion)
    controls = _squad_controls(quaternions)
    look_tangents = _catmull_tangents(x, look, tension)
    log_scale = np.log(scale)
    scale_tangents = _pchip_tangents(x, log_scale)

    template_snapshots = np.asarray([row[0] for row in template_rows])
    if template_snapshots[0] < x[0] or template_snapshots[-1] > x[-1]:
        raise ValueError(
            "keyframes must span the complete template snapshot range")

    output: list[list[float]] = []
    diagnostics: list[dict] = []
    previous_look = None
    previous_scale = None
    previous_quaternion = None
    for template in template_rows:
        snapshot = float(template[0])
        index = _segment(x, snapshot)
        fraction = (snapshot - x[index]) / (x[index + 1] - x[index])
        target = _hermite(x[index], x[index + 1], look[index],
                          look[index + 1], look_tangents[index],
                          look_tangents[index + 1], snapshot)
        log_half_extent = float(_hermite(
            x[index], x[index + 1], np.asarray(log_scale[index]),
            np.asarray(log_scale[index + 1]),
            np.asarray(scale_tangents[index]),
            np.asarray(scale_tangents[index + 1]), snapshot))
        half_extent = math.exp(log_half_extent)
        quaternion = _squad(quaternions[index], quaternions[index + 1],
                            controls[index], controls[index + 1], fraction)
        basis = _quat_to_matrix(quaternion)
        forward = _normalize(-basis[:, 2], "interpolated view")
        up = _normalize(basis[:, 1], "interpolated up")
        right = _normalize(np.cross(forward, up), "interpolated right")
        up = _normalize(np.cross(right, forward), "interpolated up")
        position = target - forward * (4.0 * half_extent)

        row = template.copy()
        row[2:5] = position.tolist()
        row[5:8] = target.tolist()
        row[8:11] = up.tolist()
        row[11] = half_extent
        output.append(row)

        if previous_look is None:
            pan = 0.0
            zoom = 0.0
            orientation = 0.0
        else:
            pan = float(np.linalg.norm(target - previous_look) / previous_scale)
            zoom = abs(half_extent / previous_scale - 1.0) * 100.0
            orientation = math.degrees(2.0 * math.acos(min(
                1.0, abs(float(np.dot(previous_quaternion, quaternion))))))
        diagnostics.append({
            "snapshot": int(snapshot),
            "segment_left": int(x[index]),
            "segment_right": int(x[index + 1]),
            "segment_fraction": fraction,
            "pan_fraction_per_frame": pan,
            "zoom_percent_per_frame": zoom,
            "orientation_deg_per_frame": orientation,
            "screen_half_extent_cm": half_extent,
        })
        previous_look = target
        previous_scale = half_extent
        previous_quaternion = quaternion
    return output, diagnostics


def write_path(path: Path, rows: list[list[float]], provenance: dict) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    with path.open("x", encoding="utf-8") as handle:
        handle.write("# schema=stellar_camera_path_v055\n")
        handle.write("# generated_by=stellar_camera_spline.py\n")
        handle.write("# interpolation=catmull_hermite_target,squad_orientation,pchip_log_scale\n")
        handle.write(f"# keyframes_sha256={provenance['keyframes_sha256']}\n")
        handle.write(f"# template_sha256={provenance['template_sha256']}\n")
        for row in rows:
            fields = [str(int(row[0]))] + [format(value, ".17g") for value in row[1:]]
            handle.write(" ".join(fields) + "\n")


def write_diagnostics(path: Path, rows: list[dict]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    columns = list(rows[0].keys())
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def keyframe_bundle_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyframes", type=Path, required=True, nargs="+",
                        help="One or more downloaded camera-keyframe JSON files")
    parser.add_argument("--template", type=Path, required=True,
                        help="Existing 21-column v055 path providing timeline and physical fields")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--tension", type=float, default=0.25,
                        help="Look-at spline tension in [0,1]; larger is more restrained")
    args = parser.parse_args(argv)
    if not 0.0 <= args.tension <= 1.0:
        parser.error("--tension must lie in [0,1]")
    try:
        keyframes = read_keyframe_files(args.keyframes)
        template = read_template(args.template)
        rows, diagnostics = compile_spline(template, keyframes, args.tension)
        provenance = {
            "keyframes_sha256": keyframe_bundle_sha256(args.keyframes),
            "template_sha256": sha256(args.template),
        }
        write_path(args.output, rows, provenance)
        write_diagnostics(args.diagnostics, diagnostics)
    except (ValueError, OSError) as error:
        print(f"stellar_camera_spline: {error}", file=sys.stderr)
        return 1
    worst_pan = max(row["pan_fraction_per_frame"] for row in diagnostics)
    worst_zoom = max(row["zoom_percent_per_frame"] for row in diagnostics)
    worst_angle = max(row["orientation_deg_per_frame"] for row in diagnostics)
    print("STELLAR_CAMERA_SPLINE_OK "
          f"rows={len(rows)} max_pan={worst_pan:.9g} "
          f"max_zoom_percent={worst_zoom:.9g} "
          f"max_orientation_deg={worst_angle:.9g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
