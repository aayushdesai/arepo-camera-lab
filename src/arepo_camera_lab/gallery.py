"""Capture exact saved camera alternatives across every physical channel."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

import numpy as np

from . import catalog, viewer
from .transfer import acquire_verified_file


POSE_SCHEMA = "stellar_camera_keyframes_v001"
CAPTURE_PLAN_SCHEMA = "arepo_camera_lab_capture_plan_v001"
RANGE_SCHEMA = "arepo_camera_lab_frozen_ranges_v001"
GALLERY_SCHEMA = "arepo_camera_lab_webgl_gallery_v001"
PHYSICAL_CHANNELS = (
    "density", "temperature", "speed", "radial_velocity",
    "azimuthal_velocity", "rotational_fraction",
    "angular_momentum_alignment", "outward_axial_velocity",
    "outward_mass_flux_proxy", "cylindrical_radius", "axial_position",
    "magnetic_field_strength", "magnetic_field_axial",
    "magnetic_field_azimuthal", "magnetic_pressure", "alfven_speed",
    "field_velocity_alignment", "toroidal_field_fraction",
    "poloidal_field_fraction", "plasma_beta", "gas_pressure",
    "entropy_proxy", "sound_speed", "mach_number",
)
LOG_CHANNELS = {
    "density", "temperature", "speed", "outward_mass_flux_proxy",
    "cylindrical_radius", "entropy_proxy", "magnetic_field_strength",
    "magnetic_pressure", "alfven_speed", "plasma_beta", "gas_pressure",
    "sound_speed", "mach_number",
}
SYMLOG_CHANNELS = {
    "radial_velocity", "azimuthal_velocity", "outward_axial_velocity",
    "axial_position", "magnetic_field_axial", "magnetic_field_azimuthal",
}
UNIT_INTERVAL_CHANNELS = {
    "rotational_fraction", "toroidal_field_fraction",
    "poloidal_field_fraction",
}
SIGNED_UNIT_CHANNELS = {
    "angular_momentum_alignment", "field_velocity_alignment",
}


def _sha256(path: Path) -> str:
    return viewer.sha256(path)


def _pose_bundle(path: Path) -> tuple[str, list[dict[str, Any]]]:
    path = path.expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != POSE_SCHEMA:
        raise ValueError(f"pose schema must be {POSE_SCHEMA}")
    poses = payload.get("alternatives")
    if not isinstance(poses, list) or not poses:
        raise ValueError("pose bundle has no alternatives")
    normalized = []
    seen_ids = set()
    for index, pose in enumerate(poses, 1):
        snapshot = int(pose["snapshot"])
        pose_id = str(pose.get("pose_id") or f"pose-{index:03d}")
        if pose_id in seen_ids:
            raise ValueError(f"duplicate pose_id {pose_id}")
        seen_ids.add(pose_id)
        for name in ("position_cm", "look_at_cm", "view_direction", "up"):
            values = np.asarray(pose.get(name), dtype=float)
            if values.shape != (3,) or not np.all(np.isfinite(values)):
                raise ValueError(f"pose {index} {name} must have three finite values")
        half_extent = float(pose["screen_half_extent_cm"])
        if not math.isfinite(half_extent) or half_extent <= 0.0:
            raise ValueError(f"pose {index} screen_half_extent_cm must be positive")
        record = dict(pose)
        record["snapshot"] = snapshot
        record["pose_id"] = pose_id
        normalized.append(record)
    return _sha256(path), normalized


def _inputs(scene_catalog: catalog.SceneCatalog, snapshots: set[int],
            cache_directory: Path) -> dict[int, tuple[Path, Path]]:
    missing = sorted(snapshots - set(scene_catalog.frames))
    if missing:
        raise ValueError(f"pose snapshots are absent from the catalog: {missing}")
    result = {}
    for snapshot in sorted(snapshots):
        frame = scene_catalog.frames[snapshot]
        scene, _ = acquire_verified_file(
            frame.scene_source, frame.scene_sha256, cache_directory / "scenes")
        sidecar, _ = acquire_verified_file(
            frame.field_sidecar_source, frame.field_sidecar_sha256,
            cache_directory / "fields")
        result[snapshot] = (scene, sidecar)
    return result


def _payload(snapshot: int, scene: Path, sidecar: Path, expected_scene_sha: str,
             max_points: int) -> dict[str, Any]:
    header = viewer.read_header(scene)
    cells = viewer.read_cells(scene, header)
    center, axis = viewer.infer_center_axis(cells, header, None, None)
    selected = viewer.sample_cells(cells, int(header["num_cells"])
                                   if max_points == 0 else max_points)
    fields = viewer.read_field_sidecar(sidecar)
    payload = viewer.build_payload(
        scene, header, cells, selected, center, axis, None,
        expected_scene_sha, snapshot, None, fields)
    actual = tuple(payload["channels"])
    if actual != PHYSICAL_CHANNELS:
        raise ValueError(
            f"snapshot {snapshot} physical channels differ from the 24-channel "
            f"contract; actual={actual}")
    return payload


def _range_settings(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    channels = {}
    for name in PHYSICAL_CHANNELS:
        records = [payload["channels"][name] for payload in payloads]
        if name in UNIT_INTERVAL_CHANNELS:
            mode, low, high, threshold = "linear", 0.0, 1.0, 1.0
        elif name in SIGNED_UNIT_CHANNELS:
            mode, low, high, threshold = "linear", -1.0, 1.0, 1.0
        elif name in SYMLOG_CHANNELS:
            mode = "symlog"
            bound = max(max(abs(float(row["default_low"])),
                            abs(float(row["default_high"]))) for row in records)
            low, high = -bound, bound
            threshold = float(np.median([float(row["linthresh"])
                                         for row in records]))
        else:
            mode = "log10" if name in LOG_CHANNELS else "linear"
            low = min(float(row["default_low"]) for row in records)
            high = max(float(row["default_high"]) for row in records)
            threshold = float(np.median([float(row["linthresh"])
                                         for row in records]))
        channels[name] = {
            "scale_mode": mode,
            "low": low,
            "high": high,
            "linthresh": max(threshold, 1.0e-30),
            "palette": "copper_blue",
            "point_size": 2.2,
            "opacity": 0.72,
            "gamma": 1.0,
            "saturation": 1.0,
            "brightness": 1.0,
            "invert": False,
        }
    return {
        "schema": RANGE_SCHEMA,
        "derivation": "minimum epoch p01 to maximum epoch p99; symmetric maxima for signed channels; exact physical bounds for fractions and alignments",
        "channels": channels,
    }


def _write_index(path: Path, records: list[dict[str, Any]]) -> None:
    rows = []
    for record in records:
        rows.append(
            f'<a href="{record["output"]}"><img loading="lazy" '
            f'src="{record["output"]}" alt="pose {record["pose_index"]} '
            f'{record["channel"]}"><span>pose {record["pose_index"]:03d} | '
            f'snapshot {record["snapshot"]:04d} | {record["channel"]}</span></a>')
    html = """<!doctype html><meta charset=\"utf-8\"><title>AREPO WebGL pose gallery</title>
<style>body{margin:0;background:#090c10;color:#e6edf3;font:12px ui-monospace,monospace}header{position:sticky;top:0;background:#111820;padding:12px 18px;z-index:2}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:8px;padding:8px}a{position:relative;color:inherit;text-decoration:none;background:#111820}img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover}span{display:block;padding:7px 9px}</style>
<header>AREPO camera lab | exact saved alternatives | 24 physical channels | copper-blue</header><main class=\"grid\">""" + "".join(rows) + "</main>\n"
    path.write_text(html, encoding="utf-8")


def capture_gallery(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_directory.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    pose_sha, poses = _pose_bundle(args.poses)
    if args.pose_limit:
        poses = poses[:args.pose_limit]
    scene_catalog = catalog.load_catalog(args.catalog)
    cache = args.cache_directory.expanduser().resolve()
    resolved = _inputs(scene_catalog, {int(pose["snapshot"]) for pose in poses}, cache)
    for index, pose in enumerate(poses, 1):
        frame = scene_catalog.frames[int(pose["snapshot"])]
        if str(pose.get("scene_sha256")) != frame.scene_sha256:
            raise ValueError(
                f"pose {index} scene SHA does not match catalog snapshot "
                f"{pose['snapshot']}")
    channels = list(PHYSICAL_CHANNELS[:args.channel_limit]
                    if args.channel_limit else PHYSICAL_CHANNELS)

    stats_payloads = []
    range_points = min(args.max_points or 250_000, args.range_points)
    for snapshot, (scene, sidecar) in resolved.items():
        payload = _payload(
            snapshot, scene, sidecar,
            scene_catalog.frames[snapshot].scene_sha256, range_points)
        stats_payloads.append({
            "channels": {
                name: {key: payload["channels"][name][key]
                       for key in ("default_low", "default_high", "linthresh")}
                for name in PHYSICAL_CHANNELS
            }
        })
    ranges = _range_settings(stats_payloads)
    range_path = output / "frozen_channel_ranges.json"
    range_path.write_text(json.dumps(ranges, indent=2) + "\n", encoding="utf-8")
    del stats_payloads

    chrome = args.chrome.expanduser().resolve()
    if not chrome.is_file():
        raise ValueError(f"Chrome executable does not exist: {chrome}")
    node = shutil.which(args.node)
    if node is None:
        raise ValueError(f"Node executable is unavailable: {args.node}")
    driver = Path(__file__).with_name("cdp_capture.js")
    if not driver.is_file():
        raise ValueError(f"capture driver is missing: {driver}")

    all_records = []
    with tempfile.TemporaryDirectory(prefix="arepo-camera-gallery-") as temporary:
        temporary_root = Path(temporary)
        for snapshot in sorted(resolved):
            matching = [(index, pose) for index, pose in enumerate(poses, 1)
                        if int(pose["snapshot"]) == snapshot]
            if not matching:
                continue
            scene, sidecar = resolved[snapshot]
            payload = _payload(
                snapshot, scene, sidecar,
                scene_catalog.frames[snapshot].scene_sha256, args.max_points)
            html = temporary_root / f"snapshot_{snapshot:04d}.html"
            viewer.write_html(html, payload)
            plan = {
                "schema": CAPTURE_PLAN_SCHEMA,
                "records_output": f"capture_records_snapshot_{snapshot:04d}.json",
                "captures": [],
            }
            for pose_index, pose in matching:
                for channel in channels:
                    relative = (f"pose_{pose_index:03d}_snapshot_{snapshot:04d}/"
                                f"{channel}.png")
                    plan["captures"].append({
                        "pose_index": pose_index,
                        "pose": pose,
                        "channel": channel,
                        "settings": ranges["channels"][channel],
                        "output": relative,
                    })
            plan_path = temporary_root / f"capture_plan_{snapshot:04d}.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            subprocess.run([
                node, str(driver), "--html", str(html), "--plan", str(plan_path),
                "--output", str(output), "--width", str(args.width),
                "--height", str(args.height), "--chrome", str(chrome),
            ], check=True)
            records_path = output / plan["records_output"]
            epoch_records = json.loads(records_path.read_text(encoding="utf-8"))
            all_records.extend(epoch_records["captures"])

    expected = len(poses) * len(channels)
    if len(all_records) != expected:
        raise ValueError(f"captured {len(all_records)} images; expected {expected}")
    _write_index(output / "index.html", all_records)
    products = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json", "manifest.sha256"}:
            products.append({
                "path": str(path.relative_to(output)),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
    manifest = {
        "schema": GALLERY_SCHEMA,
        "pose_bundle": str(args.poses.expanduser().resolve()),
        "pose_bundle_sha256": pose_sha,
        "pose_count": len(poses),
        "channel_count": len(channels),
        "image_count": expected,
        "channels": channels,
        "palette": "copper_blue",
        "width": args.width,
        "height": args.height,
        "max_points": args.max_points,
        "range_points": range_points,
        "catalog": str(args.catalog.expanduser().resolve()),
        "catalog_sha256": _sha256(args.catalog.expanduser().resolve()),
        "products": products,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.sha256").write_text(
        f"{_sha256(manifest_path)}  manifest.json\n", encoding="ascii")
    return manifest


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--poses", type=Path, required=True,
                        help="Downloaded camera-pose JSON; every alternative is captured")
    parser.add_argument("--catalog", type=Path, required=True,
                        help="Verified scene and physical-field catalog")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--cache-directory", type=Path,
                        default=Path.home() / ".cache/arepo-camera-lab")
    parser.add_argument("--max-points", type=int, default=1_000_000,
                        help="Points per WebGL image; zero uses every scene cell")
    parser.add_argument("--range-points", type=int, default=250_000,
                        help="Deterministic per-epoch sample used to freeze color ranges")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--chrome", type=Path,
                        default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    parser.add_argument("--node", default="node")
    parser.add_argument("--pose-limit", type=int, default=0,
                        help=argparse.SUPPRESS)
    parser.add_argument("--channel-limit", type=int, default=0,
                        help=argparse.SUPPRESS)


def run(args: argparse.Namespace) -> int:
    if args.max_points != 0 and args.max_points < 1000:
        raise ValueError("--max-points must be zero or at least 1000")
    if args.range_points < 1000:
        raise ValueError("--range-points must be at least 1000")
    if args.width < 320 or args.height < 180:
        raise ValueError("capture dimensions must be at least 320x180")
    manifest = capture_gallery(args)
    print(
        f"AREPO_CAMERA_LAB_WEBGL_GALLERY_OK poses={manifest['pose_count']} "
        f"channels={manifest['channel_count']} images={manifest['image_count']}")
    return 0
