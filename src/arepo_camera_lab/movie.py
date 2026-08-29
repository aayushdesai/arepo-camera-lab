"""Render a local WebGL point-cloud preview along an accepted camera spline."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

import numpy as np

from . import catalog, gallery, review, viewer


MOVIE_SCHEMA = "arepo_camera_lab_spline_movie_v001"
CAPTURE_RECORDS_SCHEMA = "arepo_camera_lab_capture_records_v001"
VISIBLE_SCENE_BINDING_SCHEMA = "arepo_camera_lab_visible_scene_binding_v001"
INTERPOLATED_NUMERIC_STYLE_FIELDS = (
    "low", "high", "symlog_threshold", "gamma", "saturation",
    "brightness", "point_size", "opacity",
)
DISCRETE_STYLE_FIELDS = ("channel", "scale_mode", "palette", "inversion")


def read_physical_camera_path(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) != 21:
                raise ValueError(
                    f"{path}:{line_number}: expected 21 v055 columns")
            values = np.asarray([float(value) for value in fields], dtype=float)
            if not np.all(np.isfinite(values)):
                raise ValueError(f"{path}:{line_number}: non-finite camera row")
            position = values[2:5]
            look_at = values[5:8]
            delta = look_at - position
            length = float(np.linalg.norm(delta))
            if length <= 0.0 or values[11] <= 0.0:
                raise ValueError(f"{path}:{line_number}: invalid camera geometry")
            rows.append({
                "snapshot": int(values[0]),
                "time_seconds": float(values[1]),
                "position_cm": position.tolist(),
                "look_at_cm": look_at.tolist(),
                "view_direction": (delta / length).tolist(),
                "up": values[8:11].tolist(),
                "screen_half_extent_cm": float(values[11]),
            })
    if not rows:
        raise ValueError(f"camera path contains no rows: {path}")
    snapshots = [row["snapshot"] for row in rows]
    if snapshots != sorted(set(snapshots)):
        raise ValueError("camera path snapshots must be unique and increasing")
    return rows


def sample_path(rows: list[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    if step <= 0:
        raise ValueError("frame step must be positive")
    sampled = [dict(row) for index, row in enumerate(rows) if index % step == 0]
    if sampled[-1]["snapshot"] != rows[-1]["snapshot"]:
        sampled.append(dict(rows[-1]))
    return sampled


def visible_snapshot(camera_snapshot: int, available: list[int]) -> int:
    """Use the latest available simulation state, never a future snapshot."""
    if not available:
        raise ValueError("scene catalog is empty")
    eligible = [snapshot for snapshot in available if snapshot <= camera_snapshot]
    return max(eligible) if eligible else min(available)


def _latest_bindings(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for binding in bundle["pose_style_bindings"]:
        latest[str(binding["pose_id"])] = dict(binding["visual_state"])
    return latest


def _route_keyframes(path: Path, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    keyframes = payload.get("keyframes")
    if payload.get("schema") != review.LEGACY_POSE_SCHEMA or \
            not isinstance(keyframes, list) or len(keyframes) < 2:
        raise ValueError("route must contain at least two v001 keyframes")
    geometry = {
        str(pose["pose_id"]): pose
        for pose in bundle["geometry"]["alternatives"]
    }
    normalized = []
    for keyframe in keyframes:
        pose_id = str(keyframe.get("pose_id", ""))
        if pose_id not in geometry:
            raise ValueError(f"route references unknown reviewed pose {pose_id}")
        source = geometry[pose_id]
        if int(keyframe["snapshot"]) != int(source["snapshot"]):
            raise ValueError(f"route snapshot differs for pose {pose_id}")
        normalized.append({"snapshot": int(source["snapshot"]), "pose_id": pose_id})
    snapshots = [row["snapshot"] for row in normalized]
    if snapshots != sorted(set(snapshots)):
        raise ValueError("route keyframe snapshots must be unique and increasing")
    return normalized


def _smootherstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def interpolate_reviewed_style(camera_snapshot: int,
                               keyframes: list[dict[str, Any]],
                               bindings: dict[str, dict[str, Any]],
                               max_points: int) -> dict[str, Any]:
    missing = [row["pose_id"] for row in keyframes
               if row["pose_id"] not in bindings]
    if missing:
        raise ValueError(
            "route poses need reviewed style bindings: " + ", ".join(missing))
    right_index = next((index for index, row in enumerate(keyframes)
                        if row["snapshot"] >= camera_snapshot), len(keyframes) - 1)
    left_index = max(0, right_index - 1)
    if keyframes[right_index]["snapshot"] == camera_snapshot or right_index == 0:
        left_index = right_index
    left = bindings[keyframes[left_index]["pose_id"]]
    right = bindings[keyframes[right_index]["pose_id"]]
    for name in DISCRETE_STYLE_FIELDS:
        if left[name] != right[name]:
            raise ValueError(
                f"route style field {name} changes between adjacent poses")
    if left["channel"] != "rotational_fraction":
        raise ValueError("spline preview currently requires rotational_fraction")
    if left_index == right_index:
        fraction = 0.0
    else:
        start = keyframes[left_index]["snapshot"]
        stop = keyframes[right_index]["snapshot"]
        fraction = _smootherstep((camera_snapshot - start) / (stop - start))
    result = {
        "palette": left["palette"],
        "scale_mode": left["scale_mode"],
        "invert": bool(left["inversion"]),
        "point_budget": int(max_points),
    }
    for name in INTERPOLATED_NUMERIC_STYLE_FIELDS:
        result["linthresh" if name == "symlog_threshold" else name] = (
            float(left[name]) * (1.0 - fraction) + float(right[name]) * fraction)
    return result


def build_frame_plan(camera_rows: list[dict[str, Any]],
                     available_snapshots: list[int],
                     route_keyframes: list[dict[str, Any]],
                     bindings: dict[str, dict[str, Any]],
                     max_points: int,
                     input_bindings: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    frames = []
    for frame_index, camera in enumerate(camera_rows):
        camera_snapshot = int(camera["snapshot"])
        simulation_snapshot = visible_snapshot(
            camera_snapshot, available_snapshots)
        if simulation_snapshot not in input_bindings:
            raise ValueError(
                f"visible snapshot {simulation_snapshot} has no input binding")
        source = input_bindings[simulation_snapshot]
        scene_sha = str(source.get("scene_sha256", "")).lower()
        sidecar_sha = str(source.get("field_sidecar_sha256", "")).lower()
        for label, digest in (("scene", scene_sha), ("field sidecar", sidecar_sha)):
            if len(digest) != 64 or any(character not in "0123456789abcdef"
                                        for character in digest):
                raise ValueError(
                    f"visible snapshot {simulation_snapshot} has invalid {label} SHA-256")
        frames.append({
            "frame_index": frame_index,
            "camera_snapshot": camera_snapshot,
            "visible_snapshot": simulation_snapshot,
            "visible_scene_binding": {
                "schema": VISIBLE_SCENE_BINDING_SCHEMA,
                "camera_snapshot": camera_snapshot,
                "visible_snapshot": simulation_snapshot,
                "scene_sha256": scene_sha,
                "field_sidecar_sha256": sidecar_sha,
            },
            "pose": dict(camera, pose_id=f"spline-{camera_snapshot:04d}"),
            "settings": interpolate_reviewed_style(
                camera_snapshot, route_keyframes, bindings, max_points),
        })
    return frames


def direct_catalog_inputs(scene_catalog: catalog.SceneCatalog,
                          snapshots: set[int]) -> dict[int, tuple[Path, Path]]:
    missing = sorted(snapshots - set(scene_catalog.frames))
    if missing:
        raise ValueError(f"camera snapshots are absent from catalog: {missing}")
    result = {}
    for snapshot in sorted(snapshots):
        frame = scene_catalog.frames[snapshot]
        paths = []
        for label, source, expected in (
                ("scene", frame.scene_source, frame.scene_sha256),
                ("field sidecar", frame.field_sidecar_source,
                 frame.field_sidecar_sha256)):
            source_path = source.split(":", 1)[-1]
            path = Path(source_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(
                    f"direct catalog {label} is unavailable on this host: {path}")
            actual = viewer.sha256(path)
            if actual != expected:
                raise ValueError(
                    f"direct catalog {label} SHA mismatch for snapshot {snapshot}")
            paths.append(path)
        result[snapshot] = (paths[0], paths[1])
    return result


def _ffprobe(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = subprocess.run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=width,height,pix_fmt,avg_frame_rate,nb_read_frames",
        "-of", "json", str(path),
    ], check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)["streams"][0]


def capture_movie(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_directory.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    camera_path = args.camera_path.expanduser().resolve()
    reviewed_path = args.reviewed_bundle.expanduser().resolve()
    route_path = args.route.expanduser().resolve()
    scene_catalog = catalog.load_catalog(args.catalog)
    reviewed = review.load_bundle(reviewed_path, args.reviewed_bundle_sha256)
    review.validate_catalog_bindings(reviewed, scene_catalog)
    keyframes = _route_keyframes(route_path, reviewed)
    bindings = _latest_bindings(reviewed)
    camera_rows = sample_path(read_physical_camera_path(camera_path), args.frame_step)
    frames = build_frame_plan(
        camera_rows, sorted(scene_catalog.frames), keyframes, bindings,
        args.max_points, {
            snapshot: {
                "scene_sha256": frame.scene_sha256,
                "field_sidecar_sha256": frame.field_sidecar_sha256,
            }
            for snapshot, frame in scene_catalog.frames.items()
        })
    snapshots = sorted({frame["visible_snapshot"] for frame in frames})
    cache = args.cache_directory.expanduser().resolve()
    resolved = (direct_catalog_inputs(scene_catalog, set(snapshots))
                if args.direct_catalog_inputs else
                gallery._inputs(scene_catalog, set(snapshots), cache))

    chrome = args.chrome.expanduser().resolve()
    if not chrome.is_file():
        raise ValueError(f"Chrome executable does not exist: {chrome}")
    node = shutil.which(args.node)
    ffmpeg = shutil.which(args.ffmpeg)
    ffprobe = shutil.which(args.ffprobe)
    if node is None or ffmpeg is None or ffprobe is None:
        raise ValueError("node, ffmpeg, and ffprobe must be available")
    driver = Path(__file__).with_name("cdp_capture.js")
    frame_directory = output / "frames"
    frame_directory.mkdir()
    all_records = []
    with tempfile.TemporaryDirectory(prefix="arepo-camera-movie-") as temporary:
        temporary_root = Path(temporary)
        for snapshot in snapshots:
            scene, sidecar = resolved[snapshot]
            payload = gallery._payload(
                snapshot, scene, sidecar,
                scene_catalog.frames[snapshot].scene_sha256, args.max_points)
            html = temporary_root / f"snapshot_{snapshot:04d}.html"
            viewer.write_html(html, payload)
            selected = [frame for frame in frames
                        if frame["visible_snapshot"] == snapshot]
            plan = {
                "schema": gallery.CAPTURE_PLAN_SCHEMA,
                "records_output": f"capture_records_snapshot_{snapshot:04d}.json",
                "captures": [{
                    "pose_index": frame["frame_index"] + 1,
                    "pose": frame["pose"],
                    "visible_scene_binding": frame["visible_scene_binding"],
                    "channel": "rotational_fraction",
                    "settings": frame["settings"],
                    "output": f"frames/frame_{frame['frame_index']:06d}.png",
                } for frame in selected],
            }
            plan_path = temporary_root / f"plan_{snapshot:04d}.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            subprocess.run([
                node, str(driver), "--html", str(html), "--plan", str(plan_path),
                "--output", str(output), "--width", str(args.width),
                "--height", str(args.height), "--chrome", str(chrome),
            ], check=True)
            records_path = output / plan["records_output"]
            records = json.loads(records_path.read_text(encoding="utf-8"))
            if records.get("schema") != CAPTURE_RECORDS_SCHEMA:
                raise ValueError("unexpected capture-record schema")
            all_records.extend(records["captures"])

    if len(all_records) != len(frames):
        raise ValueError(f"captured {len(all_records)} frames; expected {len(frames)}")
    records_by_index = {
        int(record["pose_index"]) - 1: record for record in all_records
    }
    if len(records_by_index) != len(frames):
        raise ValueError("capture records have duplicate or missing frame indices")
    for frame in frames:
        record = records_by_index.get(frame["frame_index"])
        state = record.get("state", {}) if record else {}
        if record is None or state.get("visible_scene_binding") != \
                frame["visible_scene_binding"]:
            raise ValueError(
                f"frame {frame['frame_index']} visible-scene binding differs")
        if int(state.get("camera_snapshot", -1)) != frame["camera_snapshot"] or \
                int(state.get("snapshot", -1)) != frame["visible_snapshot"] or \
                str(state.get("scene_sha256", "")) != \
                frame["visible_scene_binding"]["scene_sha256"]:
            raise ValueError(
                f"frame {frame['frame_index']} capture provenance differs")
    expected_names = [f"frame_{index:06d}.png" for index in range(len(frames))]
    actual_names = sorted(path.name for path in frame_directory.glob("*.png"))
    if actual_names != expected_names:
        raise ValueError("captured frame sequence is incomplete")

    timeline_path = output / "timeline.tsv"
    with timeline_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "frame_index", "camera_snapshot", "visible_snapshot", "pose_id",
            "gamma", "brightness", "saturation", "point_size", "opacity"),
            delimiter="\t")
        writer.writeheader()
        for frame in frames:
            writer.writerow({
                "frame_index": frame["frame_index"],
                "camera_snapshot": frame["camera_snapshot"],
                "visible_snapshot": frame["visible_snapshot"],
                "pose_id": frame["pose"]["pose_id"],
                **{name: frame["settings"][name] for name in (
                    "gamma", "brightness", "saturation", "point_size", "opacity")},
            })

    movie = output / "rotational_fraction_spline_preview.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-framerate",
        str(args.fps), "-i", str(frame_directory / "frame_%06d.png"),
        "-c:v", "libx264", "-crf", str(args.crf), "-preset", "medium",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(movie),
    ], check=True)
    probe = _ffprobe(movie, ffprobe)
    if int(probe["width"]) != args.width or int(probe["height"]) != args.height or \
            int(probe["nb_read_frames"]) != len(frames):
        raise ValueError("encoded movie dimensions or frame count differ")

    products = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json", "manifest.sha256"}:
            products.append({
                "path": str(path.relative_to(output)),
                "bytes": path.stat().st_size,
                "sha256": viewer.sha256(path),
            })
    manifest = {
        "schema": MOVIE_SCHEMA,
        "camera_path": str(camera_path),
        "camera_path_sha256": viewer.sha256(camera_path),
        "route": str(route_path),
        "route_sha256": viewer.sha256(route_path),
        "reviewed_bundle": str(reviewed_path),
        "reviewed_bundle_sha256": viewer.sha256(reviewed_path),
        "catalog": str(args.catalog.expanduser().resolve()),
        "catalog_sha256": viewer.sha256(args.catalog.expanduser().resolve()),
        "channel": "rotational_fraction",
        "palette": "copper_blue",
        "input_mode": ("direct_verified_catalog_nfs" if args.direct_catalog_inputs
                       else "content_addressed_rsync_cache"),
        "simulation_sampling": "latest_available_catalog_snapshot_not_future",
        "source_camera_rows": len(read_physical_camera_path(camera_path)),
        "frame_step": args.frame_step,
        "rendered_frames": len(frames),
        "visible_snapshots": snapshots,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "max_points": args.max_points,
        "movie": str(movie.relative_to(output)),
        "movie_probe": probe,
        "products": products,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.sha256").write_text(
        f"{viewer.sha256(manifest_path)}  manifest.json\n", encoding="ascii")
    if args.cleanup_cache_after and not args.direct_catalog_inputs:
        for scene, sidecar in resolved.values():
            scene.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
    return manifest


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--camera-path", type=Path, required=True)
    parser.add_argument("--route", type=Path, required=True,
                        help="Exact route bundle whose pose IDs authored the spline")
    parser.add_argument("--reviewed-bundle", type=Path, required=True)
    parser.add_argument("--reviewed-bundle-sha256")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--cache-directory", type=Path,
                        default=Path.home() / ".cache/arepo-camera-lab")
    parser.add_argument("--frame-step", type=int, default=5,
                        help="Render every Nth camera row and always the final row")
    parser.add_argument("--max-points", type=int, default=500_000)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--chrome", type=Path,
                        default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    parser.add_argument("--node", default="node")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--cleanup-cache-after", action="store_true")
    parser.add_argument(
        "--direct-catalog-inputs", action="store_true",
        help="Use verified NFS paths from catalog sources without a local rsync cache")


def run(args: argparse.Namespace) -> int:
    if args.max_points < 1000 or args.width < 320 or args.height < 180 or \
            args.fps <= 0 or args.frame_step <= 0 or not 0 <= args.crf <= 51:
        raise ValueError("invalid spline-movie rendering parameter")
    if args.direct_catalog_inputs and args.cleanup_cache_after:
        raise ValueError(
            "--cleanup-cache-after does not apply to direct catalog inputs")
    manifest = capture_movie(args)
    print(
        "AREPO_CAMERA_LAB_SPLINE_MOVIE_OK "
        f"frames={manifest['rendered_frames']} movie={manifest['movie']}")
    return 0
