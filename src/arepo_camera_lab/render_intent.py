"""Compile reviewed camera poses into backend-neutral render intents."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import review


INTENT_SCHEMA = "arepo_stellar_render_intent_bundle_v001"
ROW_SCHEMA = "arepo_stellar_render_intent_v001"
NATIVE_OVERLAY_SCHEMA = "arepo_stellar_native_config_overlay_v001"
DEFAULT_OPTICAL_PROFILE = "separated_support_v075"


def _latest_bindings(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for binding in bundle["pose_style_bindings"]:
        latest[str(binding["pose_id"])] = binding
    return latest


def _safe_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not result:
        raise ValueError("pose ID cannot be represented as a filename")
    return result


def _vector(values: list[float]) -> str:
    return " ".join(format(float(value), ".17g") for value in values)


def _native_overlay(row: dict[str, Any]) -> str:
    camera = row["camera"]
    display = row["display"]
    optical = row["optical"]
    lines = [
        f"% schema = {NATIVE_OVERLAY_SCHEMA}",
        f"% pose_id = {row['pose_id']}",
        f"% review_bundle_sha256 = {row['provenance']['review_bundle_sha256']}",
        f"% style_binding_id = {row['provenance']['style_binding_id']}",
        "cameraType = orthographic",
        f"cameraPosition = {_vector(camera['position_cm'])}",
        f"cameraLookAt = {_vector(camera['look_at_cm'])}",
        f"cameraUp = {_vector(camera['up'])}",
        f"swScale = {format(camera['screen_half_extent_cm'], '.17g')}",
        f"stellarPhysicalChannel = {display['channel']}",
        f"stellarPhysicalScale = {display['scale_mode']}",
        f"stellarPhysicalRangeMin = {format(display['low'], '.17g')}",
        f"stellarPhysicalRangeMax = {format(display['high'], '.17g')}",
        f"stellarPhysicalSymlogLinthresh = {format(display['symlog_threshold'], '.17g')}",
        f"stellarPhysicalOpticalProfile = {optical['profile']}",
        f"stellarPhysicalTargetOpticalDepth = {format(optical['target_optical_depth'], '.17g')}",
        f"stellarPhysicalTargetEmission = {format(optical['target_emission'], '.17g')}",
        f"stellarPhysicalReferencePathCm = {format(optical['reference_path_cm'], '.17g')}",
        f"stellarPhysicalOpacitySignalThreshold = {format(optical['opacity_signal_threshold'], '.17g')}",
        f"stellarPhysicalColorGamma = {format(display['gamma'], '.17g')}",
        f"stellarPhysicalColorInvert = {1 if display['inversion'] else 0}",
        f"stellarSaturation = {format(display['saturation'], '.17g')}",
        f"stellarDisplayBrightness = {format(display['brightness'], '.17g')}",
    ]
    return "\n".join(lines) + "\n"


def compile_bundle(
    review_bundle: Path,
    output_directory: Path,
    expected_sha256: str | None = None,
    optical_profile: str = DEFAULT_OPTICAL_PROFILE,
    target_optical_depth: float = 1.0,
    target_emission: float = 1.0,
    reference_path_cm: float = 0.0,
    opacity_signal_threshold: float = 0.02,
) -> dict[str, Any]:
    review_bundle = review_bundle.expanduser().resolve()
    output_directory = output_directory.expanduser().resolve()
    if output_directory.exists():
        raise FileExistsError(f"output directory already exists: {output_directory}")
    if target_optical_depth <= 0.0 or target_emission <= 0.0 or \
            reference_path_cm < 0.0 or not 0.0 < opacity_signal_threshold <= 1.0:
        raise ValueError("invalid optical transfer parameters")
    if optical_profile not in {
            "legacy_v072", "material_support_v074", "separated_support_v075"}:
        raise ValueError(f"unsupported native optical profile: {optical_profile}")

    source_sha = review.sha256(review_bundle)
    if expected_sha256 is not None and source_sha != expected_sha256.lower():
        raise ValueError(
            f"review bundle SHA-256 mismatch: expected {expected_sha256}, got {source_sha}")
    bundle = review.load_bundle(review_bundle, source_sha)
    latest = _latest_bindings(bundle)
    alternatives = bundle["geometry"]["alternatives"]
    missing = [pose["pose_id"] for pose in alternatives
               if pose["pose_id"] not in latest]
    if missing:
        raise ValueError(
            "every pose needs a reviewed style binding; missing: " + ", ".join(missing))

    output_directory.mkdir(parents=True)
    overlays = output_directory / "native_overlays"
    overlays.mkdir()
    rows: list[dict[str, Any]] = []
    for pose in alternatives:
        binding = latest[pose["pose_id"]]
        style = review._validate_style(dict(binding["visual_state"]))
        if style["scene_sha256"] != pose["scene_sha256"]:
            raise ValueError(
                f"pose {pose['pose_id']} style scene SHA does not match geometry")
        if style["palette"] != "copper_blue":
            raise ValueError(
                f"pose {pose['pose_id']} uses unsupported native palette "
                f"{style['palette']}; only copper_blue has a shared contract")
        row = {
            "schema": ROW_SCHEMA,
            "pose_id": pose["pose_id"],
            "snapshot": int(pose["snapshot"]),
            "camera": {
                "position_cm": pose["position_cm"],
                "look_at_cm": pose["look_at_cm"],
                "view_direction": pose["view_direction"],
                "up": pose["up"],
                "screen_half_extent_cm": pose["screen_half_extent_cm"],
            },
            "display": {
                "channel": style["channel"],
                "scale_mode": style["scale_mode"],
                "low": style["low"],
                "high": style["high"],
                "symlog_threshold": style["symlog_threshold"],
                "palette": style["palette"],
                "inversion": style["inversion"],
                "gamma": style["gamma"],
                "saturation": style["saturation"],
                "brightness": style["brightness"],
            },
            "webgl_only": {
                "point_size": style["point_size"],
                "opacity": style["opacity"],
                "point_budget": style["point_budget"],
                "canvas_size": style["canvas_size"],
            },
            "optical": {
                "profile": optical_profile,
                "target_optical_depth": target_optical_depth,
                "target_emission": target_emission,
                "reference_path_cm": reference_path_cm,
                "opacity_signal_threshold": opacity_signal_threshold,
            },
            "provenance": {
                "review_bundle_sha256": source_sha,
                "geometry_fingerprint_sha256": bundle["geometry_fingerprint_sha256"],
                "scene_sha256": pose["scene_sha256"],
                "field_sidecar_sha256": style["field_sidecar_sha256"],
                "style_binding_id": binding["binding_id"],
                "style_preset_id": binding["preset_id"],
            },
        }
        overlay_name = _safe_name(pose["pose_id"]) + ".cfg"
        row["native_overlay"] = f"native_overlays/{overlay_name}"
        (overlays / overlay_name).write_text(_native_overlay(row), encoding="utf-8")
        rows.append(row)

    payload = {
        "schema": INTENT_SCHEMA,
        "source": {
            "review_bundle_path": str(review_bundle),
            "review_bundle_sha256": source_sha,
            "geometry_fingerprint_sha256": bundle["geometry_fingerprint_sha256"],
        },
        "pose_count": len(rows),
        "snapshot_count": len({row["snapshot"] for row in rows}),
        "rows": rows,
    }
    intent_path = output_directory / "render_intents.json"
    intent_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8")

    manifest_lines = []
    for path in sorted(output_directory.rglob("*")):
        if path.is_file() and path.name != "manifest.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_lines.append(f"{digest}  {path.relative_to(output_directory)}")
    (output_directory / "manifest.sha256").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8")
    return payload


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--review-bundle", type=Path, required=True)
    parser.add_argument("--review-bundle-sha256")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--optical-profile", default=DEFAULT_OPTICAL_PROFILE)
    parser.add_argument("--target-optical-depth", type=float, default=1.0)
    parser.add_argument("--target-emission", type=float, default=1.0)
    parser.add_argument("--reference-path-cm", type=float, default=0.0)
    parser.add_argument("--opacity-signal-threshold", type=float, default=0.02)


def run(args: argparse.Namespace) -> int:
    payload = compile_bundle(
        args.review_bundle, args.output_directory, args.review_bundle_sha256,
        args.optical_profile, args.target_optical_depth, args.target_emission,
        args.reference_path_cm, args.opacity_signal_threshold)
    print(json.dumps({
        "schema": payload["schema"],
        "pose_count": payload["pose_count"],
        "snapshot_count": payload["snapshot_count"],
        "output_directory": str(args.output_directory.expanduser().resolve()),
    }, sort_keys=True))
    return 0
