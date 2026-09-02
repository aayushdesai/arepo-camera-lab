#!/usr/bin/env python3
"""Build a self-contained interactive WebGL camera lab from a v052 scene.

The generated HTML has no runtime dependencies and can be opened directly in a
browser. It supports orbit, pan, orthographic zoom, physical channel coloring,
immutable camera-alternative review, and playback of an optional v055 path.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
from typing import Iterable

import numpy as np


SCENE_MAGIC = b"ARVTKSTARV052A"
SCENE_VERSION = 5
HEADER_BYTES = 208
CELL_BYTES = 52
REQUIRED_FLAGS = (1 << 0) | (1 << 1) | (1 << 6) | (1 << 7) | (1 << 8)
RAYS_ONLY_FLAG = 1 << 5
HEADER_STRUCT = struct.Struct("<16s10IiI5Q10d24s")
CELL_DTYPE = np.dtype([
    ("position", "<f8", (3,)),
    ("density", "<f4"),
    ("temperature", "<f4"),
    ("velocity", "<f4", (3,)),
    ("particle_id", "<u8"),
], align=False)
AUXILIARY_SCHEMA = "arepo_camera_lab_fields_v001"
AUXILIARY_FIELDS = {
    "magnetic_field_gauss": (3,),
    "pressure_dyn_cm2": (),
    "specific_entropy_cgs": (),
    "sound_speed_cm_s": (),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _vector(text: str | None, name: str) -> np.ndarray | None:
    if text is None:
        return None
    try:
        values = np.asarray([float(value) for value in text.split(",")])
    except ValueError as error:
        raise ValueError(f"{name} must be x,y,z") from error
    if values.shape != (3,) or not np.all(np.isfinite(values)):
        raise ValueError(f"{name} must be three finite values")
    return values


def _normalize(vector: np.ndarray, fallback: np.ndarray | None = None) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if math.isfinite(norm) and norm > 1.0e-30:
        return vector / norm
    if fallback is None:
        raise ValueError("cannot normalize zero vector")
    return fallback.copy()


def read_header(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(HEADER_BYTES)
    if len(raw) != HEADER_BYTES:
        raise ValueError("scene is shorter than the v052 header")
    fields = HEADER_STRUCT.unpack(raw)
    names = [
        "magic", "version", "endian_marker", "header_bytes", "cell_bytes",
        "edge_bytes", "ray_bytes", "sample_width", "sample_height",
        "source_width", "source_height", "samples_per_cell", "flags",
        "num_cells", "num_edges", "num_rays", "invalid_neighbor_edges",
        "inactive_rays", "box_size", "ray_max_t", "camera_origin_x",
        "camera_origin_y", "camera_origin_z", "position_unit_cm",
        "density_unit_cgs", "velocity_unit_cm_per_s",
        "temperature_unit_kelvin", "snapshot_time_seconds", "reserved",
    ]
    header = dict(zip(names, fields))
    if header["magic"].rstrip(b"\0") != SCENE_MAGIC:
        raise ValueError("not an ARVTKSTARV052A scene")
    if header["version"] != SCENE_VERSION:
        raise ValueError(f"unsupported scene version {header['version']}")
    if header["endian_marker"] != 0x01020304:
        raise ValueError("unsupported scene endianness")
    if header["header_bytes"] != HEADER_BYTES or header["cell_bytes"] != CELL_BYTES:
        raise ValueError("unexpected v052 record size")
    if header["flags"] & REQUIRED_FLAGS != REQUIRED_FLAGS:
        raise ValueError("scene lacks required position/density/temperature/velocity/ID fields")
    if header["flags"] & RAYS_ONLY_FLAG:
        raise ValueError("rays-only scene has no reusable cell cloud")
    expected = HEADER_BYTES + header["num_cells"] * CELL_BYTES
    if path.stat().st_size < expected:
        raise ValueError("scene is truncated before the cell payload ends")
    return header


def read_cells(path: Path, header: dict) -> np.memmap:
    return np.memmap(path, dtype=CELL_DTYPE, mode="r", offset=HEADER_BYTES,
                     shape=(int(header["num_cells"]),))


def read_field_sidecar(path: Path) -> dict:
    """Read explicit, particle-ID-bound fields not present in scene v052."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"field sidecar does not exist: {path}")
    with np.load(path, allow_pickle=False) as source:
        if "schema" not in source or str(source["schema"].item()) != AUXILIARY_SCHEMA:
            raise ValueError(f"field sidecar schema must be {AUXILIARY_SCHEMA}")
        if "particle_id" not in source:
            raise ValueError("field sidecar lacks particle_id")
        particle_id = np.asarray(source["particle_id"], dtype=np.uint64)
        if particle_id.ndim != 1 or particle_id.size == 0:
            raise ValueError("field sidecar particle_id must be a nonempty vector")
        if np.unique(particle_id).size != particle_id.size:
            raise ValueError("field sidecar particle_id values must be unique")
        fields = {}
        for name, trailing_shape in AUXILIARY_FIELDS.items():
            if name not in source:
                continue
            values = np.asarray(source[name])
            expected = (particle_id.size, *trailing_shape)
            if values.shape != expected:
                raise ValueError(
                    f"field sidecar {name} has shape {values.shape}; expected {expected}")
            if not np.all(np.isfinite(values)):
                raise ValueError(f"field sidecar {name} contains non-finite values")
            fields[name] = np.asarray(values, dtype=np.float64)
        if not fields:
            raise ValueError("field sidecar contains no supported physical fields")
    order = np.argsort(particle_id)
    return {
        "path": path,
        "sha256": sha256(path),
        "particle_id": particle_id[order],
        "fields": {name: values[order] for name, values in fields.items()},
    }


def align_field_sidecar(sidecar: dict, selected_ids: np.ndarray) -> dict[str, np.ndarray]:
    source_ids = sidecar["particle_id"]
    selected_ids = np.asarray(selected_ids, dtype=np.uint64)
    locations = np.searchsorted(source_ids, selected_ids)
    in_range = locations < source_ids.size
    matched = np.zeros(selected_ids.shape, dtype=bool)
    matched[in_range] = source_ids[locations[in_range]] == selected_ids[in_range]
    if not np.all(matched):
        missing = int(np.count_nonzero(~matched))
        raise ValueError(
            f"field sidecar does not contain {missing} selected scene particle IDs")
    return {name: values[locations] for name, values in sidecar["fields"].items()}


def periodic_delta(position: np.ndarray, origin: np.ndarray,
                   box_size: float) -> np.ndarray:
    delta = position - origin
    if math.isfinite(box_size) and box_size > 0.0:
        delta -= np.rint(delta / box_size) * box_size
    return delta


def infer_center_axis(cells: np.memmap, header: dict,
                      center_override: np.ndarray | None,
                      axis_override: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    position = cells["position"]
    density = cells["density"].astype(np.float64)
    finite = np.isfinite(density) & np.all(np.isfinite(position), axis=1)
    candidates = np.flatnonzero(finite)
    if candidates.size == 0:
        raise ValueError("scene has no finite cells")
    anchor_count = min(8192, candidates.size)
    density_values = density[candidates]
    local = np.argpartition(density_values, -anchor_count)[-anchor_count:]
    anchors = candidates[local]
    reference = np.asarray(position[anchors[np.argmax(density[anchors])]], dtype=float)
    box_size = float(header["box_size"])
    anchor_delta = periodic_delta(np.asarray(position[anchors], dtype=float),
                                  reference, box_size)
    weights = np.power(10.0, np.clip(density[anchors] - np.max(density[anchors]),
                                    -12.0, 0.0))
    inferred_center = reference + np.average(anchor_delta, axis=0, weights=weights)
    if box_size > 0.0:
        inferred_center = np.mod(inferred_center, box_size)
    center = center_override if center_override is not None else inferred_center

    if axis_override is not None:
        axis = _normalize(axis_override)
    else:
        axis_count = min(200000, candidates.size)
        local = np.argpartition(density_values, -axis_count)[-axis_count:]
        selected = candidates[local]
        relative = periodic_delta(np.asarray(position[selected], dtype=float),
                                  center, box_size)
        relative *= float(header["position_unit_cm"])
        velocity = np.asarray(cells["velocity"][selected], dtype=float)
        velocity *= float(header["velocity_unit_cm_per_s"])
        angular = np.cross(relative, velocity)
        angular_weights = np.power(
            10.0, np.clip(density[selected] - np.max(density[selected]), -10.0, 0.0))
        momentum = np.sum(angular * angular_weights[:, None], axis=0)
        axis = _normalize(momentum, np.array([0.0, 0.0, 1.0]))
    return np.asarray(center, dtype=float), axis


def _splitmix64(values: np.ndarray) -> np.ndarray:
    mask = np.uint64(0xFFFFFFFFFFFFFFFF)
    z = (values.astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15)) & mask
    z = ((z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)) & mask
    z = ((z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)) & mask
    return z ^ (z >> np.uint64(31))


def _smallest_hash(cells: np.memmap, candidates: np.ndarray, count: int) -> np.ndarray:
    if count >= candidates.size:
        return candidates
    hashes = _splitmix64(np.asarray(cells["particle_id"][candidates]))
    return candidates[np.argpartition(hashes, count)[:count]]


def _top(candidates: np.ndarray, values: np.ndarray, count: int) -> np.ndarray:
    if count >= candidates.size:
        return candidates
    return candidates[np.argpartition(values[candidates], -count)[-count:]]


def sample_cells(cells: np.memmap, max_points: int) -> np.ndarray:
    position = cells["position"]
    velocity = cells["velocity"]
    density = np.asarray(cells["density"])
    temperature = np.asarray(cells["temperature"])
    speed2 = np.sum(np.asarray(velocity, dtype=np.float64) ** 2, axis=1)
    finite = (np.all(np.isfinite(position), axis=1) &
              np.all(np.isfinite(velocity), axis=1) &
              np.isfinite(density) & np.isfinite(temperature) &
              (temperature > 0.0) & np.isfinite(speed2))
    candidates = np.flatnonzero(finite)
    if candidates.size <= max_points:
        return candidates
    counts = [int(max_points * 0.55), int(max_points * 0.15),
              int(max_points * 0.15)]
    counts.append(max_points - sum(counts))
    pieces = [
        _smallest_hash(cells, candidates, counts[0]),
        _top(candidates, density, counts[1]),
        _top(candidates, np.log10(np.maximum(temperature, 1.0)), counts[2]),
        _top(candidates, speed2, counts[3]),
    ]
    selected = np.unique(np.concatenate(pieces))
    if selected.size < max_points:
        remaining = np.setdiff1d(candidates, selected, assume_unique=False)
        fill = _smallest_hash(cells, remaining, max_points - selected.size)
        selected = np.concatenate((selected, fill))
    return np.sort(selected[:max_points])


def _encode_float32(values: np.ndarray) -> str:
    limit = np.finfo(np.float32).max
    bounded = np.clip(np.asarray(values, dtype=np.float64), -limit, limit)
    raw = np.asarray(bounded, dtype="<f4").tobytes(order="C")
    return base64.b64encode(raw).decode("ascii")


def _channel_payload(values: np.ndarray, *, label: str, units: str,
                     diverging: bool = False, default_scale: str = "linear",
                     default_palette: str | None = None) -> dict:
    values = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(values)):
        raise ValueError(f"channel {label} contains non-finite values")
    finite = values.reshape(-1)
    percentiles = np.quantile(finite, np.linspace(0.0, 1.0, 101))
    if diverging:
        bound = max(float(np.quantile(np.abs(finite), 0.99)), 1.0e-30)
        low, high = -bound, bound
    elif default_scale == "log10":
        positive = finite[finite > 0.0]
        if positive.size == 0:
            # A stationary or unmagnetised snapshot still has valid zero-valued
            # fields. Keep their values and offer a linear display by default.
            default_scale = "linear"
            low, high = float(np.min(finite)), float(np.max(finite))
        else:
            low, high = [float(value) for value in np.quantile(positive, [0.01, 0.99])]
    else:
        low, high = [float(value) for value in np.quantile(finite, [0.01, 0.99])]
    if not high > low:
        high = low + max(abs(low) * 1.0e-6, 1.0e-30)
    nonzero = np.abs(finite[np.nonzero(finite)])
    linthresh = float(np.quantile(nonzero, 0.10)) if nonzero.size else 1.0
    return {
        "label": label,
        "units": units,
        "diverging": diverging,
        "default_scale": default_scale,
        "default_palette": default_palette or ("blue_red" if diverging else "copper_blue"),
        "default_low": low,
        "default_high": high,
        "data_min": float(np.min(finite)),
        "data_max": float(np.max(finite)),
        "positive_min": float(np.min(finite[finite > 0.0])) if np.any(finite > 0.0) else None,
        "linthresh": max(linthresh, 1.0e-30),
        "percentiles": [float(value) for value in percentiles],
        "values": _encode_float32(values),
    }


def build_payload(scene: Path, header: dict, cells: np.memmap,
                  selected: np.ndarray, center_native: np.ndarray,
                  axis: np.ndarray, display_radius_cm: float | None,
                  scene_digest: str, snapshot: int | None,
                  camera_path: Path | None,
                  field_sidecar: dict | None = None) -> dict:
    position_unit = float(header["position_unit_cm"])
    velocity_unit = float(header["velocity_unit_cm_per_s"])
    relative_cm = periodic_delta(np.asarray(cells["position"][selected], dtype=float),
                                 center_native, float(header["box_size"]))
    relative_cm *= position_unit
    radius = np.linalg.norm(relative_cm, axis=1)
    if display_radius_cm is None:
        display_radius_cm = float(np.quantile(radius, 0.995)) * 1.05
    if not math.isfinite(display_radius_cm) or display_radius_cm <= 0.0:
        raise ValueError("display radius must be positive")
    normalized_position = relative_cm / display_radius_cm

    velocity = np.asarray(cells["velocity"][selected], dtype=float) * velocity_unit
    speed = np.linalg.norm(velocity, axis=1)
    radial_unit = np.divide(relative_cm, radius[:, None],
                            out=np.zeros_like(relative_cm),
                            where=radius[:, None] > 0.0)
    radial_velocity = np.sum(velocity * radial_unit, axis=1)
    height = np.sum(relative_cm * axis[None, :], axis=1)
    cylindrical = relative_cm - height[:, None] * axis[None, :]
    cylindrical_radius = np.linalg.norm(cylindrical, axis=1)
    cylindrical_unit = np.divide(cylindrical, cylindrical_radius[:, None],
                                 out=np.zeros_like(cylindrical),
                                 where=cylindrical_radius[:, None] > 0.0)
    azimuthal_unit = np.cross(axis[None, :], cylindrical_unit)
    azimuthal_velocity = np.sum(velocity * azimuthal_unit, axis=1)
    rotational_fraction = np.abs(azimuthal_velocity) / np.maximum(speed, 1.0)
    outward_axial = np.sign(height) * np.sum(velocity * axis[None, :], axis=1)
    angular_momentum = np.sum(np.cross(relative_cm, velocity) * axis[None, :], axis=1)
    angular_momentum_alignment = angular_momentum / np.maximum(radius * speed, 1.0)
    density_log10 = np.asarray(cells["density"][selected], dtype=float) - 10.0
    density_cgs = np.power(10.0, density_log10)
    outward_mass_flux = density_cgs * np.maximum(outward_axial, 0.0)
    channels = {
        "density": _channel_payload(
            density_cgs, label="gas density", units="g cm^-3",
            default_scale="log10"),
        "temperature": _channel_payload(
            np.asarray(cells["temperature"][selected], dtype=float),
            label="temperature", units="K", default_scale="log10",
            default_palette="inferno"),
        "speed": _channel_payload(
            speed, label="speed", units="cm s^-1", default_scale="log10",
            default_palette="viridis"),
        "radial_velocity": _channel_payload(
            radial_velocity, label="signed radial velocity", units="cm s^-1",
            diverging=True, default_scale="symlog"),
        "azimuthal_velocity": _channel_payload(
            azimuthal_velocity, label="signed azimuthal velocity", units="cm s^-1",
            diverging=True, default_scale="symlog"),
        "rotational_fraction": _channel_payload(
            rotational_fraction, label="absolute azimuthal speed / total speed",
            units="dimensionless", default_palette="viridis"),
        "angular_momentum_alignment": _channel_payload(
            angular_momentum_alignment,
            label="signed axial angular-momentum alignment", units="dimensionless",
            diverging=True),
        "outward_axial_velocity": _channel_payload(
            outward_axial, label="signed outward axial speed", units="cm s^-1",
            diverging=True, default_scale="symlog"),
        "outward_mass_flux_proxy": _channel_payload(
            outward_mass_flux, label="outward rho-v proxy",
            units="g cm^-2 s^-1", default_scale="log10", default_palette="plasma"),
        "cylindrical_radius": _channel_payload(
            cylindrical_radius, label="cylindrical radius", units="cm",
            default_scale="log10", default_palette="viridis"),
        "axial_position": _channel_payload(
            height, label="signed axial position", units="cm", diverging=True,
            default_scale="symlog"),
    }

    auxiliary_metadata = None
    if field_sidecar is not None:
        auxiliary = align_field_sidecar(
            field_sidecar, np.asarray(cells["particle_id"][selected]))
        auxiliary_metadata = {
            "schema": AUXILIARY_SCHEMA,
            "path": str(field_sidecar["path"]),
            "sha256": field_sidecar["sha256"],
            "fields": sorted(auxiliary),
        }
        magnetic = auxiliary.get("magnetic_field_gauss")
        if magnetic is not None:
            field_strength = np.linalg.norm(magnetic, axis=1)
            field_axial = np.sum(magnetic * axis[None, :], axis=1)
            field_cylindrical = np.sum(magnetic * cylindrical_unit, axis=1)
            field_azimuthal = np.sum(magnetic * azimuthal_unit, axis=1)
            field_poloidal = np.hypot(field_axial, field_cylindrical)
            safe_field = np.maximum(field_strength, 1.0e-30)
            magnetic_pressure = field_strength ** 2 / (8.0 * math.pi)
            alfven_speed = field_strength / np.sqrt(
                4.0 * math.pi * np.maximum(density_cgs, 1.0e-99))
            field_velocity_alignment = np.sum(magnetic * velocity, axis=1) / np.maximum(
                field_strength * speed, 1.0e-30)
            channels.update({
                "magnetic_field_strength": _channel_payload(
                    field_strength, label="magnetic field strength", units="G",
                    default_scale="log10", default_palette="plasma"),
                "magnetic_field_axial": _channel_payload(
                    field_axial, label="signed axial magnetic field", units="G",
                    diverging=True, default_scale="symlog"),
                "magnetic_field_azimuthal": _channel_payload(
                    field_azimuthal, label="signed azimuthal magnetic field", units="G",
                    diverging=True, default_scale="symlog"),
                "magnetic_pressure": _channel_payload(
                    magnetic_pressure, label="magnetic pressure B^2 / 8pi",
                    units="dyn cm^-2", default_scale="log10", default_palette="magma"),
                "alfven_speed": _channel_payload(
                    alfven_speed, label="Alfven speed", units="cm s^-1",
                    default_scale="log10", default_palette="viridis"),
                "field_velocity_alignment": _channel_payload(
                    field_velocity_alignment, label="B-velocity alignment",
                    units="dimensionless", diverging=True),
                "toroidal_field_fraction": _channel_payload(
                    np.abs(field_azimuthal) / safe_field,
                    label="absolute toroidal B / |B|", units="dimensionless",
                    default_palette="viridis"),
                "poloidal_field_fraction": _channel_payload(
                    field_poloidal / safe_field, label="poloidal B / |B|",
                    units="dimensionless", default_palette="viridis"),
            })
            pressure = auxiliary.get("pressure_dyn_cm2")
            if pressure is not None:
                channels["plasma_beta"] = _channel_payload(
                    pressure / np.maximum(magnetic_pressure, 1.0e-99),
                    label="plasma beta Pgas / Pmag", units="dimensionless",
                    default_scale="log10", default_palette="turbo")
        if "pressure_dyn_cm2" in auxiliary:
            pressure = auxiliary["pressure_dyn_cm2"]
            channels["gas_pressure"] = _channel_payload(
                pressure, label="gas pressure",
                units="dyn cm^-2", default_scale="log10", default_palette="magma")
            channels["entropy_proxy"] = _channel_payload(
                pressure / np.power(np.maximum(density_cgs, 1.0e-99), 5.0 / 3.0),
                label="entropy proxy P / rho^(5/3)", units="cgs proxy",
                default_scale="log10", default_palette="viridis")
        if "specific_entropy_cgs" in auxiliary:
            channels["specific_entropy"] = _channel_payload(
                auxiliary["specific_entropy_cgs"], label="specific entropy",
                units="declared cgs", default_scale="symlog", default_palette="viridis")
        if "sound_speed_cm_s" in auxiliary:
            sound_speed = auxiliary["sound_speed_cm_s"]
            channels["sound_speed"] = _channel_payload(
                sound_speed, label="sound speed", units="cm s^-1",
                default_scale="log10", default_palette="viridis")
            channels["mach_number"] = _channel_payload(
                speed / np.maximum(sound_speed, 1.0e-30), label="Mach number",
                units="dimensionless", default_scale="log10", default_palette="turbo")

    side = np.cross(axis, np.array([0.0, 0.0, 1.0]))
    if np.linalg.norm(side) < 1.0e-8:
        side = np.cross(axis, np.array([0.0, 1.0, 0.0]))
    side = _normalize(side)
    forward = _normalize(0.82 * side + 0.57 * axis)
    up = _normalize(axis - np.dot(axis, forward) * forward)
    initial = {
        "target": [0.0, 0.0, 0.0],
        "forward": forward.tolist(),
        "up": up.tolist(),
        "scale": 0.72,
    }
    payload = {
        "schema": "stellar_scene_camera_lab_v001",
        "point_count": int(selected.size),
        "positions": _encode_float32(normalized_position.reshape(-1)),
        "channels": channels,
        "scene": {
            "path": str(scene.resolve()),
            "sha256": scene_digest,
            "snapshot": snapshot,
            "snapshot_time_seconds": float(header["snapshot_time_seconds"]),
            "num_cells": int(header["num_cells"]),
            "sample_width": int(header["sample_width"]),
            "sample_height": int(header["sample_height"]),
            "center_cm": (center_native * position_unit).tolist(),
            "axis": axis.tolist(),
            "display_radius_cm": display_radius_cm,
            "auxiliary_fields": auxiliary_metadata,
        },
        "initial_camera": initial,
        "camera_path": read_camera_path(camera_path, center_native * position_unit,
                                        display_radius_cm) if camera_path else [],
    }
    return payload


def read_camera_path(path: Path, center_cm: np.ndarray,
                     display_radius_cm: float) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) != 21:
                raise ValueError(f"{path}:{line_number}: expected 21 v055 columns")
            values = np.asarray([float(value) for value in fields], dtype=float)
            position = values[2:5]
            look_at = values[5:8]
            up = _normalize(values[8:11])
            forward = _normalize(look_at - position)
            rows.append({
                "snapshot": int(values[0]),
                "target": ((look_at - center_cm) / display_radius_cm).tolist(),
                "forward": forward.tolist(),
                "up": up.tolist(),
                "scale": float(values[11] / display_radius_cm),
            })
    return rows


HTML_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stellar Camera Lab</title>
<style>
:root { color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
* { box-sizing: border-box; }
body { margin: 0; overflow: hidden; background: #07090c; color: #e8edf1; }
#view { position: fixed; left: 360px; top: 0; width: calc(100vw - 360px); height: 100vh; cursor: grab; }
#meshImage { position: fixed; left: 360px; top: 0; width: calc(100vw - 360px); height: 100vh; display: none; pointer-events: none; z-index: 1; }
#measurementHud { position: fixed; right: 16px; bottom: 16px; z-index: 5; width: 235px; background: rgba(7,11,15,.85); border: 1px solid #354550; border-radius: 5px; padding: 10px 12px; pointer-events: none; font-size: 11px; }
#timeLabel { font-size: 12px; color: #eaf1f5; margin-bottom: 9px; }
#scaleLine { height: 7px; border: 1.5px solid #eaf1f5; border-top: 0; margin: 4px 0 9px auto; }
#scaleLabel { text-align: right; }
#legendTitle { font-size: 10px; color: #c2d0d8; margin-top: 6px; }
#legendGradient { height: 10px; margin-top: 4px; }
.legendLimits { display: flex; justify-content: space-between; margin-top: 2px; font-size: 10px; }
#rulerOverlay { position: fixed; z-index: 4; pointer-events: none; overflow: hidden; }

#view.dragging { cursor: grabbing; }
#panel { position: fixed; left: 0; top: 0; width: 360px; height: 100vh; overflow-y: auto; padding: 16px; background: #11151a; border-right: 1px solid #303741; }
h1 { margin: 0 0 14px; font-size: 17px; font-weight: 650; }
h2 { margin: 19px 0 8px; font-size: 12px; color: #9fb0be; text-transform: uppercase; }
label { display: block; margin: 8px 0 4px; font-size: 12px; color: #c5cdd3; }
select, input, button { width: 100%; min-height: 31px; border: 1px solid #3b4651; background: #171d23; color: #edf2f5; border-radius: 4px; }
input[type=range] { min-height: 24px; }
button { margin-top: 6px; cursor: pointer; }
button:hover { background: #222b33; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
.row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 7px; }
.check { display: flex; align-items: center; gap: 7px; min-height: 31px; }
.check input { width: 16px; min-height: 16px; }
.check label { display: inline; margin: 0; }
.value { float: right; color: #edf2f5; }
#colorBar { height: 15px; margin-top: 7px; border: 1px solid #3b4651; background: linear-gradient(90deg,#07101c,#29618c,#b8561f,#ffe09e); }
.meta { font-size: 11px; line-height: 1.55; color: #8fa0ae; overflow-wrap: anywhere; }
#snapshot { display: block; width: 100%; min-height: 31px; padding: 7px 9px; border-left: 2px solid #647786; background: #0c1116; color: #edf2f5; font-size: 13px; }
	#status { margin-top: 10px; color: #b7c9d5; }
	#reviewDirty { margin-top: 7px; padding: 6px 8px; border-left: 3px solid #627482; background: #0c1116; }
	#reviewDirty.unsaved { border-color: #e6a64b; color: #ffd79b; }
	#reviewDirty.saved { border-color: #5fa77a; color: #bde7ca; }
	#reviewVerification { margin: 8px 0 0; padding: 8px; border: 1px solid #303a43; background: #0b1015; color: #aebdc7; white-space: pre-wrap; font-size: 10px; line-height: 1.45; }
#timeline { display: none; }
.hint { position: fixed; right: 14px; top: 12px; padding: 6px 8px; background: rgba(8,11,14,.78); color: #a9b6bf; font-size: 11px; }
#visibleSnapshot { position: fixed; right: 14px; top: 42px; z-index: 4; padding: 7px 10px; border: 1px solid #46535e; border-radius: 4px; background: rgba(8,11,14,.92); color: #e8f1f5; font-size: 12px; font-weight: 650; }
body.capture-mode #panel, body.capture-mode .hint, body.capture-mode #visibleSnapshot { display: none; }
body.capture-mode #view, body.capture-mode #meshImage { left: 0; width: 100vw; height: 100vh; }
@media (max-width: 760px) { #panel { width: 290px; } #view, #meshImage { left: 290px; width: calc(100vw - 290px); } }
</style>
</head>
<body>
<canvas id="view"></canvas>
<img id="meshImage" alt="Native 3D Voronoi cell view">
<svg id="rulerOverlay" aria-label="Physical ruler"></svg>
<div id="measurementHud"><div id="timeLabel"></div><div id="scaleLabel"></div><div id="scaleLine"></div><div id="legendTitle"></div><div id="legendGradient"></div><div class="legendLimits"><span id="legendLow"></span><span id="legendHigh"></span></div></div>
<aside id="panel">
  <h1>Stellar Camera Lab</h1>
  <div id="sceneMeta" class="meta"></div>
  <h2>3D view</h2>
  <label for="renderMode">Renderer</label><select id="renderMode"><option value="volume">Native cell volume · Metal</option><option value="mesh">Native cell faces · VTK</option><option value="points">Point preview</option></select>
  <label for="meshDensityFloor">Hide cells below density (g cm⁻³)</label><input id="meshDensityFloor" type="number" value="100" min="0" step="any">
  <div class="meta">Lower this to reveal diffuse material. Colour and transparency are controlled separately.</div>
  <div id="volumeControls">
    <label for="volumeProfile">Volume transparency</label><select id="volumeProfile"><option value="disk">Disk</option><option value="remnant">Through to the remnant</option><option value="outflow">Diffuse outflow</option><option value="custom">Custom</option></select>
    <details><summary>Adjust transparency</summary>
      <label for="volumeDensityReference">Reference density (g cm⁻³)</label><input id="volumeDensityReference" type="number" value="10000" min="1e-30" step="any">
      <label for="volumeOpacityLength">Reference path length (km)</label><input id="volumeOpacityLength" type="number" value="10000" min="1e-30" step="any">
      <label for="volumeDensityPower">Density weighting</label><input id="volumeDensityPower" type="number" value="0.5" min="0" max="2" step="0.1">
      <label for="volumeFloorSoftening">Density threshold transition (dex)</label><input id="volumeFloorSoftening" type="number" value="1" min="0" max="4" step="0.1">
      <div class="meta">Opacity applies at this density over this path length. Longer paths make the view more transparent. These are display settings.</div>
    </details>
    <label for="volumeReconstruction">Field reconstruction</label><select id="volumeReconstruction"><option value="linear">Linear field</option><option value="piecewise_constant">Original cell values</option><option value="continuous">Legacy smoothing · slow</option></select>
    <div class="meta">Smooth field interpolates between native cells. It does not add simulation resolution.</div>
    <label for="volumeQuality">Image quality</label><select id="volumeQuality"><option value="4">High · antialiased</option><option value="1">Fast</option></select>
  </div>
  <button id="meshFit">Fit visible mesh</button>
  <div class="check"><input id="meshEdges" type="checkbox"><label for="meshEdges">Show cell edges</label></div>
  <div class="check"><input id="meshInterior" type="checkbox"><label for="meshInterior">Show interior cell faces</label></div>
  <div class="check"><input id="meshLighting" type="checkbox" checked><label for="meshLighting">Light cell faces</label></div>
  <div id="meshNotice" class="meta"></div><button id="meshRetry" type="button">Refresh native view</button>
  <h2>Time and scale</h2>
  <div class="check"><input id="showAnnotations" type="checkbox" checked><label for="showAnnotations">Show time, scale, and field legend</label></div>
  <label for="measureUnit">Distance units</label><select id="measureUnit"><option value="auto">Automatic</option><option value="cm">Centimeters</option><option value="km">Kilometers</option></select>
  <div class="check"><input id="rulerToggle" type="checkbox"><label for="rulerToggle">Two-point ruler</label></div>
  <button id="clearRuler" type="button">Clear measurement</button><div id="rulerStatus" class="meta">Face picks measure 3D surface distance. Volume and point views measure projected distance.</div>
  <h2>Display</h2>
  <label for="channel">Physical channel</label><select id="channel"></select>
  <div id="channelMeta" class="meta"></div>
  <div id="fieldNotice" class="meta"></div>
  <h2>Derived Channel</h2>
  <label for="derivedSaved">Saved formula</label><select id="derivedSaved"><option value="">New formula</option></select>
  <div class="row">
    <div><label for="derivedName">Channel name</label><input id="derivedName" type="text" spellcheck="false" value="gas_to_magnetic_pressure"></div>
    <div><label for="derivedUnits">Units</label><input id="derivedUnits" type="text" spellcheck="false" value="dimensionless"></div>
  </div>
  <label for="derivedExpression">Formula</label><input id="derivedExpression" type="text" spellcheck="false" value="gas_pressure / magnetic_pressure">
  <div class="row">
    <select id="derivedOperand" aria-label="Formula field or function"></select>
    <button id="insertOperand" type="button">Insert</button>
  </div>
  <div class="row"><button id="applyDerived" type="button">Add / update</button><button id="removeDerived" type="button">Remove</button></div>
  <div id="derivedStatus" class="meta"></div>
  <div class="row">
    <div><label for="scaleMode">Scale</label><select id="scaleMode"><option value="linear">Linear</option><option value="log10">Log10</option><option value="symlog">Symmetric log</option></select></div>
    <div><label for="palette">Color map</label><select id="palette"><option value="copper_blue">Copper-blue</option><option value="viridis">Viridis</option><option value="plasma">Plasma</option><option value="magma">Magma</option><option value="inferno">Inferno</option><option value="turbo">Turbo</option><option value="blue_red">Blue-white-red</option><option value="grayscale">Grayscale</option></select></div>
  </div>
  <label for="rangePreset">Visible range preset</label><select id="rangePreset"><option value="1,99">1-99 percentile</option><option value="5,95">5-95 percentile</option><option value="0,100">Full range</option><option value="symmetric">Symmetric about zero</option><option value="custom">Custom</option></select>
  <div class="row">
    <div><label for="rangeLow">Minimum</label><input id="rangeLow" type="number" step="any"></div>
    <div><label for="rangeHigh">Maximum</label><input id="rangeHigh" type="number" step="any"></div>
  </div>
  <div class="row">
    <div id="symlogControl"><label for="linthresh">Symmetric-log linear threshold</label><input id="linthresh" type="number" min="1e-30" step="any"></div>
    <div><label for="numericPrecision">Floating-point display precision</label><select id="numericPrecision"><option value="4">4 significant digits</option><option value="6" selected>6 significant digits</option><option value="8">8 significant digits</option><option value="12">12 significant digits</option></select></div>
  </div>
  <div id="colorBar"></div>
  <div class="row3">
    <div><label for="gamma">Gamma <span id="gammaValue" class="value"></span></label><input id="gamma" type="range" min="0.2" max="3" value="1" step="0.05"></div>
    <div><label for="saturation">Saturation <span id="saturationValue" class="value"></span></label><input id="saturation" type="range" min="0" max="2" value="1" step="0.05"></div>
    <div><label for="brightness">Brightness <span id="brightnessValue" class="value"></span></label><input id="brightness" type="range" min="0.1" max="3" value="1" step="0.05"></div>
  </div>
  <div class="check"><input id="invert" type="checkbox"><label for="invert">Invert color map</label></div>
  <div class="row">
    <div><label for="pointSize">Point size</label><input id="pointSize" type="range" min="1" max="8" value="2.2" step="0.1"></div>
    <div><label for="opacity">Opacity</label><input id="opacity" type="range" min="0.05" max="1" value="0.72" step="0.01"></div>
  </div>
  <h2>Camera</h2>
  <div class="row"><button id="reset">Reset</button><button id="rollLeft">Roll -2 deg</button></div>
  <div class="row"><button id="fit">Fit cloud</button><button id="rollRight">Roll +2 deg</button></div>
  <div class="row"><button id="zoomIn">Zoom in 2x</button><button id="zoomOut">Zoom out 2x</button></div>
  <div id="zoomReadout" class="meta"></div>
  <label for="snapshot">Visible AREPO snapshot index</label><output id="snapshot" aria-label="Visible AREPO snapshot index">unknown</output>
  <div class="meta">This value comes from the cells currently loaded. It cannot be changed by editing camera metadata. A camera pose stores only the view, look-at point, roll, and zoom for this simulation output.</div>
	  <h2>Reviewed Alternatives</h2>
	  <div class="row"><button id="previousPose" type="button">Previous pose</button><button id="nextPose" type="button">Next pose</button></div>
	  <label for="reviewPose">Snapshot / camera alternative</label><select id="reviewPose"><option value="">No pose bundle loaded</option></select>
	  <div id="poseCount" class="meta">No immutable alternatives loaded</div>
	  <label for="presetName">Named per-channel style preset</label><input id="presetName" type="text" spellcheck="false" value="copper_blue_high_gamma">
	  <label for="stylePreset">Saved preset revision</label><select id="stylePreset"><option value="">No preset revisions</option></select>
	  <button id="copyStyle" type="button">Copy current style as preset revision</button>
	  <div class="row"><button id="bindStyleSelected" type="button">Apply preset to this pose</button><button id="bindStyleAll" type="button">Apply preset to all poses</button></div>
	  <button id="savePoseOverride" type="button">Save current style as pose override</button>
	  <div class="row"><button id="copyPose" type="button">Copy exact camera</button><button id="download" type="button">Download reviewed bundle</button></div>
	  <button id="saveReviewServer" type="button">Save reviewed bundle no-clobber</button>
	  <div id="reviewDirty">No reviewed pose is active.</div>
	  <pre id="reviewVerification">Load a pose bundle to verify camera and display state.</pre>
  <div id="timeline">
    <h2>Spline Playback</h2>
    <input id="pathSlider" type="range" min="0" max="0" value="0" step="1">
    <div class="row"><button id="play">Play</button><button id="stop">Stop</button></div>
    <div id="pathStatus" class="meta"></div>
  </div>
  <div id="status" class="meta"></div>
</aside>
<div class="hint">Drag: orbit | Shift/right drag: pan | Wheel: zoom | Double-click: enter feature | K: save style override | Space: play</div>
<div id="visibleSnapshot">VISIBLE CELLS: AREPO SNAPSHOT UNKNOWN</div>
<script>
const DATA = __PAYLOAD__;
	const REVIEW_STORAGE='arepo_camera_lab_review_bundle_v002';
	const REVIEW_DRAFT_STORAGE='arepo_camera_lab_review_drafts_v001';
	const REVIEW_PENDING_POSE='arepo_camera_lab_pending_pose_v001';
const DERIVED_CHANNEL_STORAGE='arepo_camera_lab_derived_channels_v001';
const SELECTED_CHANNEL_STORAGE='arepo_camera_lab_selected_channel_v001';
const BASE_CHANNEL_NAMES=Object.keys(DATA.channels);
const canvas = document.getElementById('view');
const gl = canvas.getContext('webgl', {antialias: true, alpha: false, preserveDrawingBuffer: true});
if (!gl) throw new Error('WebGL is required');

function decodeFloat32(text) {
  const raw = atob(text), bytes = new Uint8Array(raw.length);
  for (let i=0; i<raw.length; ++i) bytes[i] = raw.charCodeAt(i);
  return new Float32Array(bytes.buffer);
}
function shader(type, source) {
  const value = gl.createShader(type); gl.shaderSource(value, source); gl.compileShader(value);
  if (!gl.getShaderParameter(value, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(value));
  return value;
}
const vertex = shader(gl.VERTEX_SHADER, `
precision highp float;
attribute vec3 a_position; attribute float a_value;
uniform vec3 u_target, u_right, u_up, u_forward;
uniform float u_scale, u_aspect, u_depth, u_point, u_domain_low, u_domain_high, u_scale_mode, u_linthresh;
varying float v_value, v_visible;
float symlog(float x) { return sign(x) * log(1.0 + abs(x) / u_linthresh) / log(10.0); }
float transformValue(float x) {
  if (u_scale_mode < 0.5) return x;
  if (u_scale_mode < 1.5) return log(max(x, 1.0e-30)) / log(10.0);
  return symlog(x);
}
void main() {
  if (!(a_value == a_value) || abs(a_value) > 3.402823e38) {
    v_value = 0.0; v_visible = 0.0;
    gl_Position = vec4(2.0, 2.0, 1.0, 1.0);
    gl_PointSize = u_point;
    return;
  }
  vec3 rel = a_position - u_target;
  float x = dot(rel, u_right) / (u_scale * u_aspect);
  float y = dot(rel, u_up) / u_scale;
  float z = clamp(dot(rel, u_forward) / u_depth, -0.999, 0.999);
  float transformed = transformValue(a_value);
  float valid = u_scale_mode > 0.5 && u_scale_mode < 1.5 ? step(1.0e-30, a_value) : 1.0;
  v_value = clamp((transformed-u_domain_low)/max(u_domain_high-u_domain_low,1.0e-30),0.0,1.0);
  v_visible = valid * step(u_domain_low, transformed) * step(transformed, u_domain_high);
  gl_Position = v_visible > 0.5 ? vec4(x, y, z, 1.0) : vec4(2.0, 2.0, 1.0, 1.0);
  gl_PointSize = u_point;
}`);
const fragment = shader(gl.FRAGMENT_SHADER, `
precision mediump float; varying float v_value, v_visible;
uniform float u_opacity, u_palette, u_gamma, u_invert, u_saturation, u_brightness;
vec3 copperBlue(float t) {
  vec3 a=vec3(0.025,0.055,0.10), b=vec3(0.16,0.38,0.55), c=vec3(0.72,0.34,0.12), d=vec3(1.0,0.88,0.62);
  return t<0.34 ? mix(a,b,t/0.34) : (t<0.72 ? mix(b,c,(t-0.34)/0.38) : mix(c,d,(t-0.72)/0.28));
}
vec3 blueRed(float t) {
  vec3 blue=vec3(0.12,0.42,0.88), mid=vec3(0.82,0.85,0.84), red=vec3(0.88,0.30,0.10);
  return t<0.5 ? mix(blue,mid,t*2.0) : mix(mid,red,(t-0.5)*2.0);
}
vec3 stops5(float t, vec3 a, vec3 b, vec3 c, vec3 d, vec3 e) {
  return t<0.25?mix(a,b,t*4.0):(t<0.5?mix(b,c,(t-.25)*4.0):(t<0.75?mix(c,d,(t-.5)*4.0):mix(d,e,(t-.75)*4.0)));
}
vec3 palette(float t) {
  if (u_palette < 0.5) return copperBlue(t);
  if (u_palette < 1.5) return stops5(t,vec3(.267,.005,.329),vec3(.230,.322,.546),vec3(.128,.567,.551),vec3(.369,.789,.383),vec3(.993,.906,.144));
  if (u_palette < 2.5) return stops5(t,vec3(.050,.030,.528),vec3(.494,.012,.658),vec3(.798,.280,.470),vec3(.973,.586,.252),vec3(.940,.975,.131));
  if (u_palette < 3.5) return stops5(t,vec3(.001,.000,.014),vec3(.251,.038,.403),vec3(.550,.161,.506),vec3(.868,.288,.409),vec3(.987,.991,.750));
  if (u_palette < 4.5) return stops5(t,vec3(.002,.001,.014),vec3(.258,.039,.406),vec3(.578,.148,.404),vec3(.865,.317,.226),vec3(.988,.998,.645));
  if (u_palette < 5.5) return stops5(t,vec3(.190,.072,.232),vec3(.160,.733,.925),vec3(.638,.991,.236),vec3(.976,.588,.093),vec3(.480,.016,.010));
  if (u_palette < 6.5) return blueRed(t);
  return vec3(t);
}
void main() {
  if (v_visible < 0.5 || length(gl_PointCoord-vec2(0.5)) > 0.5) discard;
  float t = u_invert > 0.5 ? 1.0-v_value : v_value;
  t = pow(clamp(t,0.0,1.0),1.0/max(u_gamma,0.01));
  vec3 color = palette(t);
  float luma = dot(color,vec3(.2126,.7152,.0722));
  color = clamp(mix(vec3(luma),color,u_saturation)*u_brightness,0.0,1.0);
  gl_FragColor = vec4(color, u_opacity);
}`);
const program = gl.createProgram(); gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program);
if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
gl.useProgram(program);
const locations = {};
for (const name of ['target','right','up','forward','scale','aspect','depth','point','domain_low','domain_high','scale_mode','linthresh','opacity','palette','gamma','invert','saturation','brightness'])
  locations[name] = gl.getUniformLocation(program, 'u_'+name);
const positionBuffer=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,positionBuffer); gl.bufferData(gl.ARRAY_BUFFER,decodeFloat32(DATA.positions),gl.STATIC_DRAW);
const positionLocation=gl.getAttribLocation(program,'a_position'); gl.enableVertexAttribArray(positionLocation); gl.vertexAttribPointer(positionLocation,3,gl.FLOAT,false,0,0);
const valueBuffer=gl.createBuffer(); const valueLocation=gl.getAttribLocation(program,'a_value'); gl.enableVertexAttribArray(valueLocation);

const V={add:(a,b)=>a.map((x,i)=>x+b[i]), sub:(a,b)=>a.map((x,i)=>x-b[i]), scale:(a,s)=>a.map(x=>x*s), dot:(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0), cross:(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]], norm:a=>Math.hypot(...a), unit:a=>{const n=Math.hypot(...a);return a.map(x=>x/n)}};
function rotate(vector, axis, angle) { const u=V.unit(axis), c=Math.cos(angle),s=Math.sin(angle); return V.add(V.add(V.scale(vector,c),V.scale(V.cross(u,vector),s)),V.scale(u,V.dot(u,vector)*(1-c))); }
function cleanBasis(forward,up) { forward=V.unit(forward); let right=V.unit(V.cross(forward,up)); up=V.unit(V.cross(right,forward)); return {forward,up,right}; }
const initial=DATA.initial_camera;
let camera={target:[...initial.target],scale:initial.scale,...cleanBasis(initial.forward,initial.up)};
	let dragging=false,last=[0,0],panMode=false,playing=null,batchCapture=false;
function setCamera(entry) { camera.target=[...entry.target]; camera.scale=entry.scale; Object.assign(camera,cleanBasis(entry.forward,entry.up)); }
let restoredCanvasSize=null;
function resize(){const ratio=devicePixelRatio||1,w=restoredCanvasSize?.width??Math.floor(canvas.clientWidth*ratio),h=restoredCanvasSize?.height??Math.floor(canvas.clientHeight*ratio);if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;gl.viewport(0,0,w,h);} }
const paletteIds={copper_blue:0,viridis:1,plasma:2,magma:3,inferno:4,turbo:5,blue_red:6,grayscale:7};
const paletteGradients={copper_blue:'#07101c,#29618c,#b8561f,#ffe09e',viridis:'#440154,#3b528b,#21918c,#5ec962,#fde725',plasma:'#0d0887,#7e03a8,#cc4778,#f89540,#f0f921',magma:'#000004,#3b0f70,#8c2981,#de4968,#fcfdbf',inferno:'#000004,#420a68,#932667,#dd513a,#fcffa4',turbo:'#30123b,#28bbec,#a4fc3c,#f9a31b,#7a0403',blue_red:'#1f6be0,#d1d9d6,#e04c1a',grayscale:'#000,#fff'};
const scaleIds={linear:0,log10:1,symlog:2};
const rangeState={low:0,high:1,linthresh:1};
function precisionDigits(){return Math.min(12,Math.max(4,+numericPrecision.value||6));}
function formattedNumber(value){const number=Number(value);if(!Number.isFinite(number))return String(number);return Number(number.toPrecision(precisionDigits())).toString();}
function setNumericValue(key,input,value){const number=Number(value);rangeState[key]=number;input.value=formattedNumber(number);input.title=`Exact internal value: ${number}`;}
function reformatNumericInputs(){setNumericValue('low',rangeLow,rangeState.low);setNumericValue('high',rangeHigh,rangeState.high);setNumericValue('linthresh',linthresh,rangeState.linthresh);updateChannelMeta();}
function transformValue(value){if(scaleMode.value==='log10')return Math.log10(Math.max(value,1e-30));if(scaleMode.value==='symlog')return Math.sign(value)*Math.log10(1+Math.abs(value)/Math.max(rangeState.linthresh,1e-30));return value;}
function safeRange(){let low=rangeState.low,high=rangeState.high;if(scaleMode.value==='log10'){low=Math.max(low,currentChannel.positive_min??1e-30);high=Math.max(high,low*(1+1e-6));}if(!(high>low))high=low+Math.max(Math.abs(low)*1e-6,1e-30);return [low,high];}
function updateColorBar(){const gradient=paletteGradients[palette.value];colorBar.style.background=`linear-gradient(90deg,${invert.checked?gradient.split(',').reverse().join(','):gradient})`;}
function updateChannelMeta(){const [low,high]=safeRange(),invalid=currentChannel.nonfinite_count?` | ${currentChannel.nonfinite_count.toLocaleString()} invalid hidden`:'';channelMeta.textContent=`${currentChannel.label} [${currentChannel.units}] | data ${formattedNumber(currentChannel.data_min)} to ${formattedNumber(currentChannel.data_max)} | visible ${formattedNumber(low)} to ${formattedNumber(high)}${invalid}`;}
function applyPreset(){if(rangePreset.value==='custom')return;let low,high;if(rangePreset.value==='symmetric'){const bound=Math.max(Math.abs(currentChannel.percentiles[1]),Math.abs(currentChannel.percentiles[99]));low=-bound;high=bound;}else{const [a,b]=rangePreset.value.split(',').map(Number);low=currentChannel.percentiles[a];high=currentChannel.percentiles[b];}if(scaleMode.value==='log10'&&low<=0)low=currentChannel.positive_min??1e-30;setNumericValue('low',rangeLow,low);setNumericValue('high',rangeHigh,high);updateChannelMeta();}
function render(){resize();updateMeasurements();if(nativeMode()){requestNativeFrame();zoomReadout.textContent=`screen half extent ${(camera.scale*DATA.scene.display_radius_cm).toExponential(3)} cm`;if(!batchCapture)requestAnimationFrame(render);return;}meshImage.style.display='none';const [low,high]=safeRange();gl.clearColor(0.015,0.022,0.03,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.uniform3fv(locations.target,camera.target);gl.uniform3fv(locations.right,camera.right);gl.uniform3fv(locations.up,camera.up);gl.uniform3fv(locations.forward,camera.forward);gl.uniform1f(locations.scale,camera.scale);gl.uniform1f(locations.aspect,canvas.width/canvas.height);gl.uniform1f(locations.depth,4.0);gl.uniform1f(locations.point,+pointSize.value*(devicePixelRatio||1));gl.uniform1f(locations.domain_low,transformValue(low));gl.uniform1f(locations.domain_high,transformValue(high));gl.uniform1f(locations.scale_mode,scaleIds[scaleMode.value]);gl.uniform1f(locations.linthresh,Math.max(rangeState.linthresh,1e-30));gl.uniform1f(locations.opacity,+opacity.value);gl.uniform1f(locations.palette,paletteIds[palette.value]);gl.uniform1f(locations.gamma,+gamma.value);gl.uniform1f(locations.invert,invert.checked?1:0);gl.uniform1f(locations.saturation,+saturation.value);gl.uniform1f(locations.brightness,+brightness.value);gl.drawArrays(gl.POINTS,0,DATA.point_count);zoomReadout.textContent=`screen half extent ${(camera.scale*DATA.scene.display_radius_cm).toExponential(3)} cm (${camera.scale.toExponential(3)} scene radii)`;if(!batchCapture)requestAnimationFrame(render);}
const SAFE_FUNCTION_ARITY={abs:1,sqrt:1,log10:1,ln:1,exp:1,min:2,max:2,pow:2,clip:3};
function tokenizeDerivedExpression(source){
  const tokens=[];let index=0;
  while(index<source.length){
    const rest=source.slice(index),space=rest.match(/^\s+/);if(space){index+=space[0].length;continue;}
    const number=rest.match(/^(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)/);if(number){tokens.push({type:'number',value:Number(number[0])});index+=number[0].length;continue;}
    const identifier=rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);if(identifier){tokens.push({type:'identifier',value:identifier[0]});index+=identifier[0].length;continue;}
    const symbol=source[index];if('+-*/^(),'.includes(symbol)){tokens.push({type:symbol,value:symbol});index+=1;continue;}
    throw new Error(`Unexpected character "${symbol}" at position ${index+1}.`);
  }
  tokens.push({type:'eof'});return tokens;
}
function parseDerivedExpression(source){
  if(!source.trim())throw new Error('Formula is empty.');
  const tokens=tokenizeDerivedExpression(source),dependencies=new Set();let at=0;
  const peek=()=>tokens[at],take=type=>{const token=peek();if(token.type!==type)throw new Error(`Expected ${type}, found ${token.type}.`);at+=1;return token;};
  function primary(){
    const token=peek();
    if(token.type==='number'){at+=1;return {kind:'number',value:token.value};}
    if(token.type==='('){at+=1;const node=addition();take(')');return node;}
    if(token.type==='identifier'){
      at+=1;const name=token.value;
      if(peek().type==='('){
        at+=1;const args=[];if(peek().type!==')'){while(true){args.push(addition());if(peek().type!==',')break;at+=1;}}take(')');
        if(!(name in SAFE_FUNCTION_ARITY))throw new Error(`Unknown function ${name}.`);
        if(args.length!==SAFE_FUNCTION_ARITY[name])throw new Error(`${name} expects ${SAFE_FUNCTION_ARITY[name]} argument(s).`);
        return {kind:'call',name,args};
      }
      if(name==='pi')return {kind:'number',value:Math.PI};if(name==='e')return {kind:'number',value:Math.E};
      dependencies.add(name);return {kind:'variable',name};
    }
    throw new Error(`Expected a number, field, function, or parenthesis; found ${token.type}.`);
  }
  function power(){let left=primary();if(peek().type==='^'){at+=1;left={kind:'binary',operator:'^',left,right:unary()};}return left;}
  function unary(){if(peek().type==='+'||peek().type==='-'){const operator=take(peek().type).type;return {kind:'unary',operator,operand:unary()};}return power();}
  function multiply(){let left=unary();while(peek().type==='*'||peek().type==='/'){const operator=take(peek().type).type;left={kind:'binary',operator,left,right:unary()};}return left;}
  function addition(){let left=multiply();while(peek().type==='+'||peek().type==='-'){const operator=take(peek().type).type;left={kind:'binary',operator,left,right:multiply()};}return left;}
  const ast=addition();if(peek().type!=='eof')throw new Error(`Unexpected token ${peek().type}.`);return {ast,dependencies:[...dependencies]};
}
function evaluateDerivedAst(node,index,arrays){
  if(node.kind==='number')return node.value;if(node.kind==='variable')return arrays[node.name][index];
  if(node.kind==='unary'){const value=evaluateDerivedAst(node.operand,index,arrays);return node.operator==='-'?-value:value;}
  if(node.kind==='binary'){const left=evaluateDerivedAst(node.left,index,arrays),right=evaluateDerivedAst(node.right,index,arrays);if(node.operator==='+')return left+right;if(node.operator==='-')return left-right;if(node.operator==='*')return left*right;if(node.operator==='/')return left/right;return Math.pow(left,right);}
  const args=node.args.map(argument=>evaluateDerivedAst(argument,index,arrays));
  if(node.name==='abs')return Math.abs(args[0]);if(node.name==='sqrt')return Math.sqrt(args[0]);if(node.name==='log10')return Math.log10(args[0]);if(node.name==='ln')return Math.log(args[0]);if(node.name==='exp')return Math.exp(args[0]);if(node.name==='min')return Math.min(args[0],args[1]);if(node.name==='max')return Math.max(args[0],args[1]);if(node.name==='pow')return Math.pow(args[0],args[1]);return Math.min(Math.max(args[0],args[1]),args[2]);
}
function quantileSorted(values,fraction){if(values.length===1)return values[0];const position=(values.length-1)*fraction,low=Math.floor(position),high=Math.ceil(position),weight=position-low;return values[low]*(1-weight)+values[high]*weight;}
function derivedChannelPayload(definition,values){
  const finite=[];for(const value of values)if(Number.isFinite(value))finite.push(value);if(!finite.length)throw new Error(`${definition.name} has no finite values.`);finite.sort((a,b)=>a-b);
  const percentiles=Array.from({length:101},(_,index)=>quantileSorted(finite,index/100)),positive=finite.filter(value=>value>0),nonzero=finite.filter(value=>value!==0).map(Math.abs).sort((a,b)=>a-b),diverging=finite[0]<0&&finite[finite.length-1]>0;
  const broadPositive=finite[0]>=0&&positive.length&&positive[positive.length-1]/positive[0]>=100,defaultScale=diverging?'symlog':(broadPositive?'log10':'linear');
  let low,high;if(diverging){const bound=Math.max(quantileSorted(finite.map(Math.abs).sort((a,b)=>a-b),.99),1e-30);low=-bound;high=bound;}else if(defaultScale==='log10'){low=quantileSorted(positive,.01);high=quantileSorted(positive,.99);}else{low=percentiles[1];high=percentiles[99];}
  if(!(high>low))high=low+Math.max(Math.abs(low)*1e-6,1e-30);
  return {label:definition.name.replaceAll('_',' '),units:definition.units||'derived',diverging,default_scale:defaultScale,default_palette:'copper_blue',default_low:low,default_high:high,data_min:finite[0],data_max:finite[finite.length-1],positive_min:positive.length?positive[0]:null,linthresh:nonzero.length?Math.max(quantileSorted(nonzero,.10),1e-30):1,percentiles,array:values,derived:true,source_expression:definition.expression,finite_count:finite.length,nonfinite_count:values.length-finite.length};
}
function readDerivedDefinitions(){
  try{const stored=JSON.parse(localStorage.getItem(DERIVED_CHANNEL_STORAGE)||'null'),rows=Array.isArray(stored)?stored:(stored&&Array.isArray(stored.definitions)?stored.definitions:[]),byName=new Map();for(const row of rows)if(row&&typeof row.name==='string'&&typeof row.expression==='string')byName.set(row.name,{name:row.name,expression:row.expression,units:typeof row.units==='string'?row.units:'derived'});return [...byName.values()];}catch(error){return [];}
}
function persistDerivedDefinitions(){localStorage.setItem(DERIVED_CHANNEL_STORAGE,JSON.stringify({schema:'arepo_camera_lab_derived_channels_v001',definitions:derivedDefinitions}));}
const channelArrays=new Map();
function valuesForChannel(name){const payload=DATA.channels[name];if(!payload)throw new Error(`Unknown physical channel ${name}.`);if(payload.array)return payload.array;if(!channelArrays.has(name))channelArrays.set(name,decodeFloat32(payload.values));return channelArrays.get(name);}
function materializeDerivedDefinitions(){
  for(const name of Object.keys(DATA.channels))if(DATA.channels[name].derived)delete DATA.channels[name];
  for(const name of [...channelArrays.keys()])if(!BASE_CHANNEL_NAMES.includes(name))channelArrays.delete(name);
  const definitions=new Map(derivedDefinitions.map(row=>[row.name,row])),resolved=new Map(),active=new Set(),errors=[];
  function resolve(name){
    if(BASE_CHANNEL_NAMES.includes(name))return valuesForChannel(name);if(resolved.has(name))return resolved.get(name);const definition=definitions.get(name);if(!definition)throw new Error(`Unknown field ${name}.`);if(active.has(name))throw new Error(`Cyclic derived-channel dependency at ${name}.`);active.add(name);
    try{const parsed=parseDerivedExpression(definition.expression),arrays={};for(const dependency of parsed.dependencies)arrays[dependency]=resolve(dependency);const values=new Float32Array(DATA.point_count),limit=3.4028234663852886e38;for(let index=0;index<values.length;++index){const value=evaluateDerivedAst(parsed.ast,index,arrays);values[index]=Number.isFinite(value)&&Math.abs(value)<=limit?value:NaN;}DATA.channels[name]=derivedChannelPayload(definition,values);channelArrays.set(name,values);resolved.set(name,values);return values;}finally{active.delete(name);}
  }
  for(const definition of derivedDefinitions){try{resolve(definition.name);}catch(error){errors.push(`${definition.name}: ${error.message}`);}}
  return errors;
}
function rebuildChannelOptions(selected){
  channel.replaceChildren();const physical=document.createElement('optgroup');physical.label='Physical';for(const name of BASE_CHANNEL_NAMES){const option=document.createElement('option');option.value=name;option.textContent=name.replaceAll('_',' ');physical.appendChild(option);}channel.appendChild(physical);
  const names=derivedDefinitions.map(row=>row.name).filter(name=>DATA.channels[name]);if(names.length){const derived=document.createElement('optgroup');derived.label='Derived';for(const name of names){const option=document.createElement('option');option.value=name;option.textContent=name.replaceAll('_',' ');derived.appendChild(option);}channel.appendChild(derived);}channel.value=DATA.channels[selected]?selected:'rotational_fraction';
}
function rebuildDerivedEditor(){
  const selected=derivedSaved.value;derivedSaved.replaceChildren(new Option('New formula',''));for(const definition of derivedDefinitions)derivedSaved.appendChild(new Option(definition.name.replaceAll('_',' '),definition.name));derivedSaved.value=derivedDefinitions.some(row=>row.name===selected)?selected:'';
  derivedOperand.replaceChildren();const fields=document.createElement('optgroup');fields.label='Physical channels';for(const name of [...BASE_CHANNEL_NAMES,...derivedDefinitions.map(row=>row.name).filter(name=>DATA.channels[name])])fields.appendChild(new Option(name.replaceAll('_',' '),name));derivedOperand.appendChild(fields);const functions=document.createElement('optgroup');functions.label='Functions';for(const entry of ['abs()','sqrt()','log10()','ln()','exp()','min(, )','max(, )','pow(, )','clip(, , )'])functions.appendChild(new Option(entry,entry));derivedOperand.appendChild(functions);
}
function loadChannel(name){currentChannel=DATA.channels[name];if(!currentChannel)return;gl.bindBuffer(gl.ARRAY_BUFFER,valueBuffer);gl.bufferData(gl.ARRAY_BUFFER,valuesForChannel(name),gl.STATIC_DRAW);gl.vertexAttribPointer(valueLocation,1,gl.FLOAT,false,0,0);scaleMode.value=currentChannel.default_scale;palette.value=currentChannel.default_palette;setNumericValue('linthresh',linthresh,currentChannel.linthresh);setNumericValue('low',rangeLow,currentChannel.default_low);setNumericValue('high',rangeHigh,currentChannel.default_high);rangePreset.value=currentChannel.diverging?'symmetric':'1,99';symlogControl.style.display=scaleMode.value==='symlog'?'block':'none';updateColorBar();updateChannelMeta();localStorage.setItem(SELECTED_CHANNEL_STORAGE,name);if(currentChannel.derived){derivedSaved.value=name;const definition=derivedDefinitions.find(row=>row.name===name);if(definition){derivedName.value=definition.name;derivedUnits.value=definition.units;derivedExpression.value=definition.expression;}}}
const channel=document.getElementById('channel'),channelMeta=document.getElementById('channelMeta'),fieldNotice=document.getElementById('fieldNotice'),pointSize=document.getElementById('pointSize'),opacity=document.getElementById('opacity'),scaleMode=document.getElementById('scaleMode'),palette=document.getElementById('palette'),rangePreset=document.getElementById('rangePreset'),rangeLow=document.getElementById('rangeLow'),rangeHigh=document.getElementById('rangeHigh'),linthresh=document.getElementById('linthresh'),numericPrecision=document.getElementById('numericPrecision'),symlogControl=document.getElementById('symlogControl'),gamma=document.getElementById('gamma'),saturation=document.getElementById('saturation'),brightness=document.getElementById('brightness'),invert=document.getElementById('invert'),colorBar=document.getElementById('colorBar'),gammaValue=document.getElementById('gammaValue'),saturationValue=document.getElementById('saturationValue'),brightnessValue=document.getElementById('brightnessValue');
const derivedSaved=document.getElementById('derivedSaved'),derivedName=document.getElementById('derivedName'),derivedUnits=document.getElementById('derivedUnits'),derivedExpression=document.getElementById('derivedExpression'),derivedOperand=document.getElementById('derivedOperand'),insertOperand=document.getElementById('insertOperand'),applyDerived=document.getElementById('applyDerived'),removeDerived=document.getElementById('removeDerived'),derivedStatus=document.getElementById('derivedStatus');
const sceneMeta=document.getElementById('sceneMeta'),statusText=document.getElementById('status'),snapshotReadout=document.getElementById('snapshot'),poseCountText=document.getElementById('poseCount'),visibleSnapshot=document.getElementById('visibleSnapshot');
const resetButton=document.getElementById('reset'),fitButton=document.getElementById('fit'),rollLeftButton=document.getElementById('rollLeft'),rollRightButton=document.getElementById('rollRight');
const zoomInButton=document.getElementById('zoomIn'),zoomOutButton=document.getElementById('zoomOut'),zoomReadout=document.getElementById('zoomReadout');
const copyPoseButton=document.getElementById('copyPose'),downloadButton=document.getElementById('download'),previousPoseButton=document.getElementById('previousPose'),nextPoseButton=document.getElementById('nextPose'),reviewPose=document.getElementById('reviewPose'),presetName=document.getElementById('presetName'),stylePreset=document.getElementById('stylePreset'),copyStyleButton=document.getElementById('copyStyle'),bindStyleSelectedButton=document.getElementById('bindStyleSelected'),bindStyleAllButton=document.getElementById('bindStyleAll'),savePoseOverrideButton=document.getElementById('savePoseOverride'),saveReviewServerButton=document.getElementById('saveReviewServer'),reviewDirty=document.getElementById('reviewDirty'),reviewVerification=document.getElementById('reviewVerification');
const timelinePanel=document.getElementById('timeline'),pathSlider=document.getElementById('pathSlider'),pathStatus=document.getElementById('pathStatus'),playButton=document.getElementById('play'),stopButton=document.getElementById('stop');
let currentChannel=null,derivedDefinitions=readDerivedDefinitions();
const initialDerivedErrors=materializeDerivedDefinitions();rebuildChannelOptions(localStorage.getItem(SELECTED_CHANNEL_STORAGE)||'rotational_fraction');rebuildDerivedEditor();loadChannel(channel.value);channel.onchange=()=>loadChannel(channel.value);
derivedStatus.textContent=initialDerivedErrors.length?initialDerivedErrors.join(' | '):'Built-in plasma beta is gas_pressure / magnetic_pressure.';
derivedSaved.onchange=()=>{const definition=derivedDefinitions.find(row=>row.name===derivedSaved.value);if(definition){derivedName.value=definition.name;derivedUnits.value=definition.units;derivedExpression.value=definition.expression;}else{derivedName.value='';derivedUnits.value='dimensionless';derivedExpression.value='';}};
insertOperand.onclick=()=>{const insertion=derivedOperand.value,start=derivedExpression.selectionStart??derivedExpression.value.length,end=derivedExpression.selectionEnd??start;derivedExpression.setRangeText(insertion,start,end,'end');if(insertion.endsWith(')'))derivedExpression.selectionStart=derivedExpression.selectionEnd=derivedExpression.selectionStart-1;derivedExpression.focus();};
applyDerived.onclick=()=>{const name=derivedName.value.trim(),expression=derivedExpression.value.trim(),units=derivedUnits.value.trim()||'derived';if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)){derivedStatus.textContent='Channel name must contain only letters, digits, and underscores and cannot start with a digit.';return;}if(BASE_CHANNEL_NAMES.includes(name)){derivedStatus.textContent=`${name} is a built-in physical channel and cannot be replaced.`;return;}const previous=derivedDefinitions.map(row=>({...row})),index=derivedDefinitions.findIndex(row=>row.name===name);if(index>=0)derivedDefinitions[index]={name,expression,units};else derivedDefinitions.push({name,expression,units});const errors=materializeDerivedDefinitions(),ownError=errors.find(message=>message.startsWith(`${name}:`));if(ownError){derivedDefinitions=previous;materializeDerivedDefinitions();rebuildChannelOptions(channel.value);rebuildDerivedEditor();derivedStatus.textContent=ownError;return;}try{persistDerivedDefinitions();}catch(error){derivedStatus.textContent=`Formula works but could not be saved: ${error.message}`;return;}rebuildChannelOptions(name);rebuildDerivedEditor();derivedSaved.value=name;loadChannel(name);const invalid=DATA.channels[name].nonfinite_count;derivedStatus.textContent=`Saved ${name}${invalid?`; ${invalid.toLocaleString()} non-finite values are hidden`:''}.${errors.length?` Other formula errors: ${errors.join(' | ')}`:''}`;};
removeDerived.onclick=()=>{const name=derivedSaved.value||derivedName.value.trim();if(!derivedDefinitions.some(row=>row.name===name)){derivedStatus.textContent='Select a saved derived channel to remove.';return;}derivedDefinitions=derivedDefinitions.filter(row=>row.name!==name);materializeDerivedDefinitions();persistDerivedDefinitions();rebuildChannelOptions('rotational_fraction');rebuildDerivedEditor();derivedName.value='';derivedUnits.value='dimensionless';derivedExpression.value='';loadChannel(channel.value);derivedStatus.textContent=`Removed ${name}.`;};
fieldNotice.textContent=DATA.scene.auxiliary_fields?`Auxiliary fields loaded: ${DATA.scene.auxiliary_fields.fields.join(', ')} | ${DATA.scene.auxiliary_fields.sha256.slice(0,12)}`:'Magnetic field, pressure, and entropy are unavailable because this v052 scene has no auxiliary field sidecar.';
scaleMode.onchange=()=>{symlogControl.style.display=scaleMode.value==='symlog'?'block':'none';if(scaleMode.value==='log10'&&rangeState.low<=0)setNumericValue('low',rangeLow,currentChannel.positive_min??1e-30);updateChannelMeta();};
palette.onchange=updateColorBar;invert.onchange=updateColorBar;rangePreset.onchange=applyPreset;
numericPrecision.onchange=reformatNumericInputs;
for(const [input,key] of [[rangeLow,'low'],[rangeHigh,'high'],[linthresh,'linthresh']]){input.onfocus=()=>{input.value=String(rangeState[key]);};input.oninput=()=>{const value=Number(input.value);if(Number.isFinite(value))rangeState[key]=value;rangePreset.value='custom';updateChannelMeta();};input.onblur=()=>setNumericValue(key,input,rangeState[key]);}
for(const [input,output] of [[gamma,gammaValue],[saturation,saturationValue],[brightness,brightnessValue]]){const update=()=>output.textContent=(+input.value).toFixed(2);input.oninput=update;update();}
document.getElementById('sceneMeta').textContent=`AREPO snapshot index ${DATA.scene.snapshot ?? 'unknown'} | ${DATA.point_count.toLocaleString()} / ${DATA.scene.num_cells.toLocaleString()} cells | radius ${DATA.scene.display_radius_cm.toExponential(3)} cm | scene ${DATA.scene.sha256.slice(0,12)}`;
snapshotReadout.textContent=DATA.scene.snapshot ?? 'unknown';
visibleSnapshot.textContent=`VISIBLE CELLS: AREPO SNAPSHOT ${DATA.scene.snapshot ?? 'UNKNOWN'}`;
canvas.oncontextmenu=e=>e.preventDefault();canvas.onpointerdown=e=>{dragging=true;canvas.classList.add('dragging');last=[e.clientX,e.clientY];panMode=e.shiftKey||e.button===2;canvas.setPointerCapture(e.pointerId);};canvas.onpointerup=e=>{dragging=false;canvas.classList.remove('dragging');canvas.releasePointerCapture(e.pointerId);};canvas.onpointermove=e=>{if(!dragging)return;const dx=e.clientX-last[0],dy=e.clientY-last[1];last=[e.clientX,e.clientY];if(dx||dy)markCameraModified();if(panMode){camera.target=V.add(camera.target,V.add(V.scale(camera.right,-dx*camera.scale*0.0025),V.scale(camera.up,dy*camera.scale*0.0025)));}else{let f=rotate(camera.forward,camera.up,-dx*0.006),r=V.unit(V.cross(f,camera.up));f=rotate(f,r,-dy*0.006);let u=rotate(camera.up,r,-dy*0.006);Object.assign(camera,cleanBasis(f,u));}};
canvas.onwheel=e=>{e.preventDefault();markCameraModified();camera.scale=Math.min(100,Math.max(1e-6,camera.scale*Math.exp(e.deltaY*0.0015)));};
canvas.ondblclick=e=>{markCameraModified();const rect=canvas.getBoundingClientRect(),aspect=canvas.width/canvas.height,x=((e.clientX-rect.left)/rect.width*2-1)*camera.scale*aspect,y=(1-(e.clientY-rect.top)/rect.height*2)*camera.scale;camera.target=V.add(camera.target,V.add(V.scale(camera.right,x),V.scale(camera.up,y)));camera.scale=Math.max(1e-6,camera.scale*0.35);};
function roll(angle){camera.up=rotate(camera.up,camera.forward,angle);Object.assign(camera,cleanBasis(camera.forward,camera.up));}
resetButton.onclick=()=>{markCameraModified();setCamera(initial);};fitButton.onclick=()=>{markCameraModified();camera.target=[0,0,0];camera.scale=1.05;};rollLeftButton.onclick=()=>{markCameraModified();roll(-Math.PI/90);};rollRightButton.onclick=()=>{markCameraModified();roll(Math.PI/90);};
zoomInButton.onclick=()=>{markCameraModified();camera.scale=Math.max(1e-6,camera.scale*0.5);};zoomOutButton.onclick=()=>{markCameraModified();camera.scale=Math.min(100,camera.scale*2);};
function pose(){if(DATA.scene.snapshot===null||DATA.scene.snapshot===undefined)throw new Error('The loaded scene has no AREPO snapshot index. Reload it with an explicit index before saving a pose.');const radius=DATA.scene.display_radius_cm,center=DATA.scene.center_cm,look=V.add(center,V.scale(camera.target,radius)),half=camera.scale*radius,pos=V.sub(look,V.scale(camera.forward,4*half));return {snapshot:Number(DATA.scene.snapshot),position_cm:pos,look_at_cm:look,view_direction:[...camera.forward],up:[...camera.up],screen_half_extent_cm:half,scene_sha256:DATA.scene.sha256,scene_path:DATA.scene.path};}
const embeddedReview=DATA.review_workspace?.bundle??null;
const reviewSidecars=DATA.review_workspace?.field_sidecar_sha256_by_snapshot??{};
function cloned(value){return JSON.parse(JSON.stringify(value));}
function storedReview(){try{const value=JSON.parse(localStorage.getItem(REVIEW_STORAGE)||'null');if(value&&embeddedReview&&value.schema==='stellar_camera_review_bundle_v002'&&value.geometry_fingerprint_sha256===embeddedReview.geometry_fingerprint_sha256)return value;}catch(error){}return embeddedReview?cloned(embeddedReview):null;}
function storedDrafts(){try{const value=JSON.parse(localStorage.getItem(REVIEW_DRAFT_STORAGE)||'{}');return value&&typeof value==='object'?value:{};}catch(error){return {};}}
let reviewBundle=storedReview(),reviewDrafts=storedDrafts(),activePose=null,styleDirty=false,cameraExact=false,suppressStyleDirty=false;
const alternatives=reviewBundle?.geometry?.alternatives??[];
function uniqueId(prefix){if(globalThis.crypto&&crypto.randomUUID)return `${prefix}-${crypto.randomUUID()}`;return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,10)}`;}
function persistReview(){if(!reviewBundle)return false;try{localStorage.setItem(REVIEW_STORAGE,JSON.stringify(reviewBundle));localStorage.setItem(REVIEW_DRAFT_STORAGE,JSON.stringify(reviewDrafts));return true;}catch(error){statusText.textContent=`Browser review storage failed: ${error.message}`;return false;}}
function visualState(forPose=activePose){if(!forPose)throw new Error('Select an immutable pose first.');resize();return {schema:'stellar_camera_visual_state_v001',channel:channel.value,scale_mode:scaleMode.value,low:rangeState.low,high:rangeState.high,symlog_threshold:rangeState.linthresh,palette:palette.value,inversion:invert.checked,gamma:Number(gamma.value),saturation:Number(saturation.value),brightness:Number(brightness.value),point_size:Number(pointSize.value),opacity:Number(opacity.value),point_budget:Number(DATA.scene.point_budget??DATA.point_count),canvas_size:{width:canvas.width,height:canvas.height},scene_sha256:forPose.scene_sha256,field_sidecar_sha256:reviewSidecars[String(forPose.snapshot)]??null,mesh_view:meshViewState()};}
function syncStyleReadouts(){gammaValue.textContent=Number(gamma.value).toFixed(2);saturationValue.textContent=Number(saturation.value).toFixed(2);brightnessValue.textContent=Number(brightness.value).toFixed(2);updateColorBar();updateChannelMeta();}
function applyVisualState(state){if(!state)return;suppressStyleDirty=true;try{if(DATA.channels[state.channel]){channel.value=state.channel;loadChannel(state.channel);}scaleMode.value=state.scale_mode;palette.value=state.palette;setNumericValue('low',rangeLow,Number(state.low));setNumericValue('high',rangeHigh,Number(state.high));setNumericValue('linthresh',linthresh,Number(state.symlog_threshold));rangePreset.value='custom';invert.checked=Boolean(state.inversion);gamma.value=String(state.gamma);saturation.value=String(state.saturation);brightness.value=String(state.brightness);pointSize.value=String(state.point_size);opacity.value=String(state.opacity);restoredCanvasSize={width:Number(state.canvas_size.width),height:Number(state.canvas_size.height)};resize();symlogControl.style.display=scaleMode.value==='symlog'?'block':'none';syncStyleReadouts();if(state.mesh_view)applyMeshViewState(state.mesh_view);}finally{suppressStyleDirty=false;}}
function latestBinding(poseId){if(!reviewBundle)return null;for(let index=reviewBundle.pose_style_bindings.length-1;index>=0;--index){const binding=reviewBundle.pose_style_bindings[index];if(binding.pose_id===poseId)return binding;}return null;}
function cameraSummary(){if(!activePose)return 'camera: no active pose';return `camera: ${cameraExact?'exact immutable geometry':'interactive camera differs from immutable geometry'}\nposition_cm: ${activePose.position_cm.join(', ')}\nlook_at_cm: ${activePose.look_at_cm.join(', ')}\nview_direction: ${activePose.view_direction.join(', ')}\nup: ${activePose.up.join(', ')}\nscreen_half_extent_cm: ${activePose.screen_half_extent_cm}`;}
function updateReviewReadout(){if(!activePose){reviewDirty.className='';reviewDirty.textContent='No reviewed pose is active.';reviewVerification.textContent='Load a pose bundle to verify camera and display state.';return;}reviewDirty.className=styleDirty?'unsaved':'saved';reviewDirty.textContent=styleDirty?'UNSAVED STYLE DRAFT retained in this browser':'STYLE BINDING SAVED (camera geometry unchanged)';reviewVerification.textContent=`pose_id: ${activePose.pose_id}\nsnapshot: ${activePose.snapshot}\nscene_sha256: ${activePose.scene_sha256}\n${cameraSummary()}\nchannel: ${channel.value}\nscale/range: ${scaleMode.value} [${rangeState.low}, ${rangeState.high}]\nsymlog_threshold: ${rangeState.linthresh}\npalette/invert: ${palette.value} / ${invert.checked}\ngamma/saturation/brightness: ${gamma.value} / ${saturation.value} / ${brightness.value}\npoint_size/opacity/budget: ${pointSize.value} / ${opacity.value} / ${DATA.scene.point_budget??DATA.point_count}\ncanvas: ${canvas.width}x${canvas.height}`;}
function markStyleDirty(){if(suppressStyleDirty||!activePose)return;styleDirty=true;reviewDrafts[activePose.pose_id]=visualState(activePose);persistReview();updateReviewReadout();}
function markCameraModified(){if(!activePose)return;cameraExact=false;updateReviewReadout();}
function rebuildPoseMenu(){reviewPose.replaceChildren();if(!alternatives.length){reviewPose.appendChild(new Option('No pose bundle loaded',''));reviewPose.disabled=true;previousPoseButton.disabled=true;nextPoseButton.disabled=true;poseCountText.textContent='No immutable alternatives loaded';return;}const grouped=new Map();for(const item of alternatives){const snapshot=Number(item.snapshot);if(!grouped.has(snapshot))grouped.set(snapshot,[]);grouped.get(snapshot).push(item);}for(const [snapshot,poses] of grouped){const group=document.createElement('optgroup');group.label=`AREPO snapshot ${snapshot} (${poses.length} alternative${poses.length===1?'':'s'})`;poses.forEach((item,index)=>group.appendChild(new Option(`alternative ${index+1} | ${item.pose_id}`,item.pose_id)));reviewPose.appendChild(group);}poseCountText.textContent=`${alternatives.length} immutable camera alternatives across ${grouped.size} AREPO snapshots | source ${reviewBundle.source_pose_bundle?.sha256?.slice(0,12)??'embedded'}`;}
function rebuildPresetMenu(selected){stylePreset.replaceChildren();const presets=reviewBundle?.style_presets??[];if(!presets.length){stylePreset.appendChild(new Option('No preset revisions',''));stylePreset.disabled=true;return;}stylePreset.disabled=false;for(const preset of presets){stylePreset.appendChild(new Option(`${preset.name} | ${preset.channel} | ${preset.preset_id.slice(-8)}`,preset.preset_id));}stylePreset.value=presets.some(row=>row.preset_id===selected)?selected:presets[presets.length-1].preset_id;}
function selectedPreset(){return reviewBundle?.style_presets.find(row=>row.preset_id===stylePreset.value)??null;}
function defaultVisualState(){restoredCanvasSize=null;loadChannel('rotational_fraction');scaleMode.value=currentChannel.default_scale;palette.value='copper_blue';setNumericValue('low',rangeLow,currentChannel.default_low);setNumericValue('high',rangeHigh,currentChannel.default_high);setNumericValue('linthresh',linthresh,currentChannel.linthresh);invert.checked=false;gamma.value='1';saturation.value='1';brightness.value='1';pointSize.value='2.2';opacity.value='0.72';syncStyleReadouts();}
async function activatePoseId(poseId){const target=alternatives.find(row=>row.pose_id===poseId);if(!target)throw new Error(`Unknown pose ID ${poseId}.`);reviewPose.value=poseId;const draft=reviewDrafts[target.pose_id],binding=latestBinding(target.pose_id),restoredStyle=draft??binding?.visual_state??null,desiredBudget=Number(restoredStyle?.point_budget??DATA.scene.point_budget??DATA.point_count),requiresReload=Number(target.snapshot)!==Number(DATA.scene.snapshot)||desiredBudget!==Number(DATA.scene.point_budget??DATA.point_count);if(requiresReload){localStorage.setItem(REVIEW_PENDING_POSE,poseId);statusText.textContent=`Loading verified AREPO snapshot ${target.snapshot} for ${poseId} at point budget ${desiredBudget.toLocaleString()}...`;const response=await fetch('/api/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({snapshot:Number(target.snapshot),max_points:desiredBudget,pose_id:poseId})}),result=await response.json();if(!response.ok)throw new Error(result.error||`HTTP ${response.status}`);return;}setPhysicalPose(target);activePose=target;cameraExact=true;localStorage.removeItem(REVIEW_PENDING_POSE);if(draft){applyVisualState(draft);styleDirty=true;}else if(binding){applyVisualState(binding.visual_state);styleDirty=false;}else{suppressStyleDirty=true;try{defaultVisualState();}finally{suppressStyleDirty=false;}styleDirty=false;statusText.textContent='Legacy v001 pose restored exactly. Historical styling was not recorded; explicit camera-lab runtime defaults are shown.';}updateReviewReadout();}
function savePresetRevision(){if(!reviewBundle||!activePose)throw new Error('Load an immutable pose bundle and select a pose first.');const name=presetName.value.trim();if(!name)throw new Error('Preset name cannot be empty.');const state=visualState(activePose),record={schema:'stellar_camera_style_preset_v001',preset_id:uniqueId('style'),name,channel:state.channel,created_at:new Date().toISOString(),visual_state:state};reviewBundle.style_presets.push(record);persistReview();rebuildPresetMenu(record.preset_id);return record;}
function addBinding(target,preset,state){const visual=cloned(state);visual.scene_sha256=target.scene_sha256;visual.field_sidecar_sha256=reviewSidecars[String(target.snapshot)]??null;const binding={schema:'stellar_camera_pose_style_binding_v001',binding_id:uniqueId('binding'),pose_id:target.pose_id,preset_id:preset.preset_id,channel:visual.channel,created_at:new Date().toISOString(),visual_state:visual};reviewBundle.pose_style_bindings.push(binding);delete reviewDrafts[target.pose_id];return binding;}
function bindPreset(targets,useCurrentOverride=false){if(!activePose||targets.some(target=>!target))throw new Error('Select an immutable pose first.');const preset=selectedPreset();if(!preset)throw new Error('Copy the current style as a named preset revision first.');const current=useCurrentOverride?visualState(activePose):preset.visual_state;for(const target of targets)addBinding(target,preset,current);persistReview();styleDirty=Boolean(reviewDrafts[activePose?.pose_id]);updateReviewReadout();return targets.length;}
function downloadReview(){if(!reviewBundle)throw new Error('No reviewed bundle is loaded.');const blob=new Blob([JSON.stringify(reviewBundle,null,2)+'\n'],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='stellar_camera_review_bundle_v002.json';a.click();URL.revokeObjectURL(a.href);}
// Snapshot browser review/draft state before the archive freezes edits.
window.cameraLabSessionState=()=>({
  schema:'arepo_camera_lab_browser_session_v001',
  visible_scene:{snapshot:DATA.scene.snapshot,sha256:DATA.scene.sha256},
  review_bundle:cloned(reviewBundle),review_drafts:cloned(reviewDrafts),
  derived_channels:cloned(derivedDefinitions),active_pose_id:activePose?.pose_id??null,
  current_camera:activePose&&cameraExact?cloned(activePose):pose(),
  current_visual_state:activePose?visualState(activePose):null,
  renderer_mode:renderMode.value,mesh_view:meshViewState(),measurements:window.cameraLabMeasurements()
});
rebuildPoseMenu();rebuildPresetMenu('');
reviewPose.onchange=()=>activatePoseId(reviewPose.value).catch(error=>statusText.textContent=error.message);
previousPoseButton.onclick=()=>{if(!activePose&&alternatives.length)return activatePoseId(alternatives[0].pose_id);const index=alternatives.findIndex(row=>row.pose_id===activePose.pose_id);activatePoseId(alternatives[(index-1+alternatives.length)%alternatives.length].pose_id).catch(error=>statusText.textContent=error.message);};
nextPoseButton.onclick=()=>{if(!activePose&&alternatives.length)return activatePoseId(alternatives[0].pose_id);const index=alternatives.findIndex(row=>row.pose_id===activePose.pose_id);activatePoseId(alternatives[(index+1)%alternatives.length].pose_id).catch(error=>statusText.textContent=error.message);};
copyStyleButton.onclick=()=>{try{const preset=savePresetRevision();statusText.textContent=`Saved immutable preset revision ${preset.preset_id}; no pose binding changed.`;}catch(error){statusText.textContent=error.message;}};
bindStyleSelectedButton.onclick=()=>{try{const count=bindPreset([activePose]);statusText.textContent=`Appended ${count} pose-style binding; camera geometry unchanged.`;}catch(error){statusText.textContent=error.message;}};
bindStyleAllButton.onclick=()=>{try{const count=bindPreset(alternatives);statusText.textContent=`Appended ${count} pose-style bindings from one preset; all immutable camera alternatives retained.`;}catch(error){statusText.textContent=error.message;}};
savePoseOverrideButton.onclick=()=>{try{const count=bindPreset([activePose],true);statusText.textContent=`Appended ${count} per-pose style override; camera geometry unchanged.`;}catch(error){statusText.textContent=error.message;}};
copyPoseButton.onclick=async()=>{if(!activePose)throw new Error('No immutable camera alternative is active.');await navigator.clipboard.writeText(JSON.stringify(activePose,null,2));statusText.textContent=`Copied exact immutable camera ${activePose.pose_id}.`;};
downloadButton.onclick=()=>{try{downloadReview();statusText.textContent=`Downloaded ${alternatives.length} unchanged alternatives, ${reviewBundle.style_presets.length} preset revisions, and ${reviewBundle.pose_style_bindings.length} append-only bindings.`;}catch(error){statusText.textContent=error.message;}};
saveReviewServerButton.onclick=async()=>{try{if(!reviewBundle)throw new Error('No reviewed bundle is loaded.');const response=await fetch('/api/review-bundle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(reviewBundle)}),result=await response.json();if(!response.ok)throw new Error(result.error||`HTTP ${response.status}`);statusText.textContent=`Saved reviewed bundle no-clobber at ${result.path}.`;}catch(error){statusText.textContent=error.message;}};
for(const control of [channel,scaleMode,palette,rangeLow,rangeHigh,linthresh,invert,gamma,saturation,brightness,pointSize,opacity]){control.addEventListener('input',markStyleDirty);control.addEventListener('change',markStyleDirty);}
timelinePanel.style.display='block';
if(DATA.camera_path.length){pathSlider.max=DATA.camera_path.length-1;const show=i=>{const entry=DATA.camera_path[i];setCamera(entry);pathStatus.textContent=`camera path row ${entry.snapshot} (${i+1}/${DATA.camera_path.length}); visible cells remain AREPO snapshot ${DATA.scene.snapshot??'unknown'}`;};pathSlider.oninput=()=>show(+pathSlider.value);playButton.onclick=()=>{if(playing)return;playing=setInterval(()=>{let i=(+pathSlider.value+1)%DATA.camera_path.length;pathSlider.value=i;show(i);},50);};stopButton.onclick=()=>{clearInterval(playing);playing=null;};show(0);}else{pathSlider.disabled=true;playButton.disabled=true;stopButton.disabled=true;pathStatus.textContent='No spline is loaded. Save one camera pose at each of at least two different snapshots, compile them, then rebuild this viewer with --camera-path.';}
document.onkeydown=e=>{if(e.target.closest('input,textarea,select,[contenteditable=true]'))return;if(e.key==='k'||e.key==='K')savePoseOverrideButton.click();if(e.code==='Space'){e.preventDefault();if(playing)stopButton.click();else if(DATA.camera_path.length)playButton.click();else statusText.textContent='Space plays a compiled path; this single-scene viewer has none loaded yet.';}if(e.key==='r'||e.key==='R')resetButton.click();};
function setPhysicalPose(entry,visibleSceneBinding=null){
  const expectedSnapshot=visibleSceneBinding===null?Number(entry.snapshot):Number(visibleSceneBinding.visible_snapshot);
  const expectedSceneSha=visibleSceneBinding===null?String(entry.scene_sha256):String(visibleSceneBinding.scene_sha256);
  if(visibleSceneBinding!==null){
    if(visibleSceneBinding.schema!=='arepo_camera_lab_visible_scene_binding_v001')throw new Error('Capture visible-scene binding schema is invalid.');
    if(Number(visibleSceneBinding.camera_snapshot)!==Number(entry.snapshot))throw new Error('Capture camera snapshot does not match the interpolated camera row.');
  }
  if(expectedSnapshot!==Number(DATA.scene.snapshot))throw new Error(`Expected visible snapshot ${expectedSnapshot} does not match loaded snapshot ${DATA.scene.snapshot}.`);
  if(expectedSceneSha!==String(DATA.scene.sha256))throw new Error('Expected scene SHA-256 does not match the visible scene.');
  const expectedSidecar=visibleSceneBinding?.field_sidecar_sha256;
  const loadedSidecar=DATA.scene.auxiliary_fields?.sha256;
  if(expectedSidecar!==undefined&&expectedSidecar!==null&&String(expectedSidecar)!==String(loadedSidecar))throw new Error('Expected field-sidecar SHA-256 does not match the visible scene.');
  const center=DATA.scene.center_cm,radius=DATA.scene.display_radius_cm;
  const look=entry.look_at_cm.map(Number),forward=entry.view_direction.map(Number),up=entry.up.map(Number);
  const target=look.map((value,index)=>(value-center[index])/radius),scale=Number(entry.screen_half_extent_cm)/radius;
  if(!Number.isFinite(scale)||scale<=0)throw new Error('Pose screen half extent must be positive and finite.');
  setCamera({target,forward,up,scale});
}
__MEASUREMENT_MATH__
__MESH_VIEWER__
if(alternatives.length){const pending=DATA.review_workspace?.requested_pose_id||localStorage.getItem(REVIEW_PENDING_POSE),initialPose=alternatives.some(row=>row.pose_id===pending)?pending:(alternatives.find(row=>Number(row.snapshot)===Number(DATA.scene.snapshot))?.pose_id);if(initialPose)activatePoseId(initialPose).catch(error=>statusText.textContent=error.message);}
function setCaptureMode(enabled){
  batchCapture=Boolean(enabled);document.body.classList.toggle('capture-mode',batchCapture);
  resize();
}
async function prepareCapture(entry,name,settings={},visibleSceneBinding=null){
  if(!DATA.channels[name])throw new Error(`Unknown physical channel ${name}.`);
  renderMode.value=settings.renderer==='volume'&&volumeLive?'volume':settings.renderer==='mesh'&&meshLive?'mesh':'points';nativeInteractiveUntil=0;syncNativeControls();
  if(settings.mesh_view)applyMeshViewState(settings.mesh_view);
  setCaptureMode(true);setPhysicalPose(entry,visibleSceneBinding);channel.value=name;loadChannel(name);
  palette.value=settings.palette||'copper_blue';scaleMode.value=settings.scale_mode||currentChannel.default_scale;
  if(Number.isFinite(Number(settings.low)))setNumericValue('low',rangeLow,Number(settings.low));
  if(Number.isFinite(Number(settings.high)))setNumericValue('high',rangeHigh,Number(settings.high));
  if(Number.isFinite(Number(settings.linthresh)))setNumericValue('linthresh',linthresh,Number(settings.linthresh));
  if(Number.isFinite(Number(settings.point_size)))pointSize.value=String(settings.point_size);
  if(Number.isFinite(Number(settings.opacity)))opacity.value=String(settings.opacity);
  if(Number.isFinite(Number(settings.gamma)))gamma.value=String(settings.gamma);
  if(Number.isFinite(Number(settings.saturation)))saturation.value=String(settings.saturation);
  if(Number.isFinite(Number(settings.brightness)))brightness.value=String(settings.brightness);
  invert.checked=Boolean(settings.invert);symlogControl.style.display=scaleMode.value==='symlog'?'block':'none';updateColorBar();updateChannelMeta();
  resize();render();if(nativeMode())await awaitNativeFrame();else gl.finish();updateMeasurements();await new Promise(resolve=>requestAnimationFrame(resolve));
  return {schema:'arepo_camera_lab_capture_state_v001',camera_snapshot:Number(entry.snapshot),snapshot:Number(DATA.scene.snapshot),scene_sha256:DATA.scene.sha256,visible_scene_binding:visibleSceneBinding,pose_id:entry.pose_id??null,channel:name,palette:palette.value,scale_mode:scaleMode.value,low:rangeState.low,high:rangeState.high,linthresh:rangeState.linthresh,point_size:Number(pointSize.value),opacity:Number(opacity.value),gamma:Number(gamma.value),saturation:Number(saturation.value),brightness:Number(brightness.value),invert:invert.checked,point_count:DATA.point_count,renderer:renderMode.value,mesh_report:meshLastReport,measurements:window.cameraLabMeasurements(),camera_pose:pose(),canvas:{width:canvas.width,height:canvas.height}};
}
window.AREPO_CAMERA_LAB_CAPTURE={schema:'arepo_camera_lab_capture_api_v001',channels:[...BASE_CHANNEL_NAMES],scene:{...DATA.scene},prepare:prepareCapture,setCaptureMode};
render();
</script>
</body>
</html>
'''


HTML_TEMPLATE = HTML_TEMPLATE.replace("__MEASUREMENT_MATH__", Path(__file__).with_name("measurement_math.js").read_text()).replace("__MESH_VIEWER__", Path(__file__).with_name("mesh_viewer.js").read_text())


def write_html(path: Path, payload: dict) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(HTML_TEMPLATE.replace("__PAYLOAD__", encoded))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot", type=int)
    parser.add_argument("--max-points", type=int, default=140000)
    parser.add_argument("--center", help="Override physical scene center as native x,y,z")
    parser.add_argument("--axis", help="Override physical axis as x,y,z")
    parser.add_argument("--display-radius-cm", type=float)
    parser.add_argument("--scene-sha256",
                        help="Trusted manifest hash; otherwise the scene is hashed")
    parser.add_argument("--camera-path", type=Path,
                        help="Optional v055 path to expose on the playback timeline")
    parser.add_argument("--field-sidecar", type=Path,
                        help="Optional particle-ID-bound magnetic/thermodynamic NPZ")
    args = parser.parse_args(argv)
    if args.max_points < 1000:
        parser.error("--max-points must be at least 1000")
    try:
        center = _vector(args.center, "--center")
        axis = _vector(args.axis, "--axis")
        header = read_header(args.scene)
        cells = read_cells(args.scene, header)
        center_native, inferred_axis = infer_center_axis(cells, header, center, axis)
        selected = sample_cells(cells, args.max_points)
        field_sidecar = read_field_sidecar(args.field_sidecar) \
            if args.field_sidecar is not None else None
        digest = args.scene_sha256 or sha256(args.scene)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
            raise ValueError("--scene-sha256 must be a 64-character hexadecimal digest")
        payload = build_payload(args.scene, header, cells, selected, center_native,
                                inferred_axis, args.display_radius_cm,
                                digest.lower(), args.snapshot, args.camera_path,
                                field_sidecar)
        write_html(args.output, payload)
    except (ValueError, OSError) as error:
        print(f"stellar_scene_camera_lab: {error}", file=sys.stderr)
        return 1
    print("STELLAR_SCENE_CAMERA_LAB_OK "
          f"points={payload['point_count']} center_cm={payload['scene']['center_cm']} "
          f"axis={payload['scene']['axis']} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
