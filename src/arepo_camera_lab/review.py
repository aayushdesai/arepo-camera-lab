"""Versioned camera-alternative review bundles.

Camera geometry is immutable source material.  Display presets and per-pose
bindings are append-only review records so a visual review cannot silently
rewrite an authored camera alternative.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


LEGACY_POSE_SCHEMA = "stellar_camera_keyframes_v001"
REVIEW_BUNDLE_SCHEMA = "stellar_camera_review_bundle_v002"
STYLE_SCHEMA = "stellar_camera_visual_state_v001"
PRESET_SCHEMA = "stellar_camera_style_preset_v001"
BINDING_SCHEMA = "stellar_camera_pose_style_binding_v001"

STYLE_FIELDS = (
    "channel", "scale_mode", "low", "high", "symlog_threshold",
    "palette", "inversion", "gamma", "saturation", "brightness",
    "point_size", "opacity", "point_budget", "canvas_size",
    "scene_sha256", "field_sidecar_sha256",
)

EXPLICIT_LEGACY_DEFAULTS = {
    "policy": "camera_lab_runtime_defaults_v001",
    "historical_style_available": False,
    "channel": "rotational_fraction",
    "scale_mode": "linear",
    "range": "channel_default",
    "symlog_threshold": "channel_default",
    "palette": "copper_blue",
    "inversion": False,
    "gamma": 1.0,
    "saturation": 1.0,
    "brightness": 1.0,
    "point_size": 2.2,
    "opacity": 0.72,
    "point_budget": "server_request",
    "canvas_size": "current_canvas",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_vector(pose: dict[str, Any], name: str) -> list[float]:
    value = pose.get(name)
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"pose {name} must contain three values")
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"pose {name} must be finite")
    return result


def _validate_pose(pose: dict[str, Any]) -> dict[str, Any]:
    result = dict(pose)
    result["snapshot"] = int(result["snapshot"])
    pose_id = result.get("pose_id")
    if not isinstance(pose_id, str) or not pose_id:
        raise ValueError("every camera alternative needs a nonempty pose_id")
    for name in ("position_cm", "look_at_cm", "view_direction", "up"):
        result[name] = _finite_vector(result, name)
    half_extent = float(result.get("screen_half_extent_cm", 0.0))
    if not math.isfinite(half_extent) or half_extent <= 0.0:
        raise ValueError("pose screen_half_extent_cm must be positive and finite")
    result["screen_half_extent_cm"] = half_extent
    scene_sha = str(result.get("scene_sha256", "")).lower()
    if len(scene_sha) != 64 or any(c not in "0123456789abcdef" for c in scene_sha):
        raise ValueError(f"pose {pose_id} has an invalid scene_sha256")
    result["scene_sha256"] = scene_sha
    return result


def _validate_style(style: dict[str, Any]) -> dict[str, Any]:
    missing = [name for name in STYLE_FIELDS if name not in style]
    if missing:
        raise ValueError(f"visual state is missing fields: {', '.join(missing)}")
    result = dict(style)
    result["schema"] = STYLE_SCHEMA
    for name in ("low", "high", "symlog_threshold", "gamma", "saturation",
                 "brightness", "point_size", "opacity"):
        result[name] = float(result[name])
        if not math.isfinite(result[name]):
            raise ValueError(f"visual state {name} must be finite")
    if not result["high"] > result["low"]:
        raise ValueError("visual state high must exceed low")
    if result["symlog_threshold"] <= 0.0 or result["gamma"] <= 0.0 or \
            result["brightness"] <= 0.0 or result["point_size"] <= 0.0 or \
            not 0.0 <= result["opacity"] <= 1.0 or result["saturation"] < 0.0:
        raise ValueError("visual state contains an invalid display parameter")
    result["point_budget"] = int(result["point_budget"])
    if result["point_budget"] < 0:
        raise ValueError("visual state point_budget cannot be negative")
    canvas = result["canvas_size"]
    if not isinstance(canvas, dict) or int(canvas.get("width", 0)) <= 0 or \
            int(canvas.get("height", 0)) <= 0:
        raise ValueError("visual state canvas_size needs positive width and height")
    result["canvas_size"] = {
        "width": int(canvas["width"]), "height": int(canvas["height"])}
    result["inversion"] = bool(result["inversion"])
    scene_sha = str(result["scene_sha256"]).lower()
    if len(scene_sha) != 64 or any(c not in "0123456789abcdef" for c in scene_sha):
        raise ValueError("visual state scene_sha256 must be a SHA-256 digest")
    result["scene_sha256"] = scene_sha
    sidecar_sha = result["field_sidecar_sha256"]
    if sidecar_sha is not None:
        sidecar_sha = str(sidecar_sha).lower()
        if len(sidecar_sha) != 64 or any(
                c not in "0123456789abcdef" for c in sidecar_sha):
            raise ValueError(
                "visual state field_sidecar_sha256 must be null or a SHA-256 digest")
        result["field_sidecar_sha256"] = sidecar_sha
    for name in ("channel", "scale_mode", "palette"):
        if not isinstance(result[name], str) or not result[name]:
            raise ValueError(f"visual state {name} must be a nonempty string")
    return result


def geometry_fingerprint(bundle: dict[str, Any]) -> str:
    geometry = bundle.get("geometry", bundle)
    alternatives = geometry.get("alternatives") or geometry.get("keyframes")
    encoded = json.dumps(alternatives, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_bundle(payload: dict[str, Any], source_sha256: str | None = None) -> dict[str, Any]:
    schema = payload.get("schema")
    if schema == REVIEW_BUNDLE_SCHEMA:
        geometry = payload.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("schema") != LEGACY_POSE_SCHEMA:
            raise ValueError("review bundle geometry must use stellar_camera_keyframes_v001")
        result = dict(payload)
    elif schema == LEGACY_POSE_SCHEMA:
        geometry = dict(payload)
        result = {
            "schema": REVIEW_BUNDLE_SCHEMA,
            "source_pose_bundle": {
                "schema": LEGACY_POSE_SCHEMA,
                "sha256": source_sha256,
            },
            "geometry": geometry,
            "style_presets": [],
            "pose_style_bindings": [],
            "legacy_style_defaults": dict(EXPLICIT_LEGACY_DEFAULTS),
        }
    else:
        raise ValueError(f"unsupported camera review schema: {schema}")

    alternatives = geometry.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        alternatives = geometry.get("keyframes")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("camera bundle has no alternatives")
    validated = [_validate_pose(dict(pose)) for pose in alternatives]
    ids = [pose["pose_id"] for pose in validated]
    if len(set(ids)) != len(ids):
        raise ValueError("camera alternative pose_id values must be unique")
    geometry = dict(geometry)
    geometry["alternatives"] = validated
    result["geometry"] = geometry
    result.setdefault("style_presets", [])
    result.setdefault("pose_style_bindings", [])
    result.setdefault("legacy_style_defaults", dict(EXPLICIT_LEGACY_DEFAULTS))

    preset_ids: set[str] = set()
    for preset in result["style_presets"]:
        if preset.get("schema") != PRESET_SCHEMA:
            raise ValueError("invalid style preset schema")
        preset_id = str(preset.get("preset_id", ""))
        if not preset_id or preset_id in preset_ids:
            raise ValueError("style preset IDs must be unique and nonempty")
        preset_ids.add(preset_id)
        _validate_style(dict(preset["visual_state"]))
    binding_ids: set[str] = set()
    for binding in result["pose_style_bindings"]:
        if binding.get("schema") != BINDING_SCHEMA:
            raise ValueError("invalid pose-style binding schema")
        binding_id = str(binding.get("binding_id", ""))
        if not binding_id or binding_id in binding_ids:
            raise ValueError("pose-style binding IDs must be unique and nonempty")
        binding_ids.add(binding_id)
        if binding.get("pose_id") not in ids:
            raise ValueError("pose-style binding references an unknown pose_id")
        if binding.get("preset_id") not in preset_ids:
            raise ValueError("pose-style binding references an unknown preset_id")
        _validate_style(dict(binding["visual_state"]))
    result["geometry_fingerprint_sha256"] = geometry_fingerprint(result)
    return result


def load_bundle(path: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    path = path.expanduser().resolve()
    actual = sha256(path)
    if expected_sha256 is not None and actual != expected_sha256.lower():
        raise ValueError(
            f"pose bundle SHA-256 mismatch: expected {expected_sha256}, got {actual}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    result = normalize_bundle(payload, actual)
    result["source_pose_bundle"]["path"] = str(path)
    result["source_pose_bundle"]["sha256"] = actual
    return result


def validate_catalog_bindings(bundle: dict[str, Any], catalog: Any) -> None:
    for pose in bundle["geometry"]["alternatives"]:
        try:
            frame = catalog.frames[int(pose["snapshot"])]
        except KeyError as error:
            raise ValueError(
                f"pose {pose['pose_id']} snapshot {pose['snapshot']} is absent from catalog") from error
        if pose["scene_sha256"] != frame.scene_sha256:
            raise ValueError(
                f"pose {pose['pose_id']} scene SHA does not match catalog snapshot "
                f"{pose['snapshot']}")


def public_workspace(bundle: dict[str, Any], catalog: Any | None,
                     requested_pose_id: str | None) -> dict[str, Any]:
    result = json.loads(json.dumps(bundle, allow_nan=False))
    sidecars: dict[str, str] = {}
    if catalog is not None:
        for snapshot, frame in catalog.frames.items():
            sidecars[str(snapshot)] = frame.field_sidecar_sha256
    return {
        "schema": "arepo_camera_review_workspace_v001",
        "bundle": result,
        "field_sidecar_sha256_by_snapshot": sidecars,
        "requested_pose_id": requested_pose_id,
    }
