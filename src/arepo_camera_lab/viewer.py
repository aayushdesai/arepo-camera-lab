#!/usr/bin/env python3
"""Build a self-contained interactive WebGL camera lab from a v052 scene.

The generated HTML has no runtime dependencies and can be opened directly in a
browser. It supports orbit, pan, orthographic zoom, physical channel coloring,
key-pose capture, and playback of an optional v055 camera path.
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
            raise ValueError(f"logarithmic channel {label} has no positive values")
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
            density_cgs, label="density proxy", units="g cm^-3",
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
            channels["gas_pressure"] = _channel_payload(
                auxiliary["pressure_dyn_cm2"], label="gas pressure",
                units="dyn cm^-2", default_scale="log10", default_palette="magma")
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
#status { margin-top: 10px; color: #b7c9d5; }
#timeline { display: none; }
.hint { position: fixed; right: 14px; bottom: 46px; padding: 6px 8px; background: rgba(8,11,14,.78); color: #a9b6bf; font-size: 11px; }
#visibleSnapshot { position: fixed; right: 14px; bottom: 12px; z-index: 4; padding: 7px 10px; border: 1px solid #46535e; border-radius: 4px; background: rgba(8,11,14,.92); color: #e8f1f5; font-size: 12px; font-weight: 650; }
@media (max-width: 760px) { #panel { width: 290px; } #view { left: 290px; width: calc(100vw - 290px); } }
</style>
</head>
<body>
<canvas id="view"></canvas>
<aside id="panel">
  <h1>Stellar Camera Lab</h1>
  <div id="sceneMeta" class="meta"></div>
  <h2>Display</h2>
  <label for="channel">Physical channel</label><select id="channel"></select>
  <div id="channelMeta" class="meta"></div>
  <div id="fieldNotice" class="meta"></div>
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
  <label for="snapshot">Visible AREPO snapshot index</label><input id="snapshot" type="text" readonly>
  <div class="meta">This value comes from the cells currently loaded. It cannot be changed by editing camera metadata. A camera pose stores only the view, look-at point, roll, and zoom for this simulation output.</div>
  <button id="addPose">Save camera pose</button>
  <div class="row"><button id="copyPose">Copy current pose</button><button id="download">Download camera poses</button></div>
  <button id="clearPoses">Clear saved camera poses</button>
  <div id="poseCount" class="meta">0 camera poses</div>
  <div id="timeline">
    <h2>Spline Playback</h2>
    <input id="pathSlider" type="range" min="0" max="0" value="0" step="1">
    <div class="row"><button id="play">Play</button><button id="stop">Stop</button></div>
    <div id="pathStatus" class="meta"></div>
  </div>
  <div id="status" class="meta"></div>
</aside>
<div class="hint">Drag: orbit | Shift/right drag: pan | Wheel: zoom | Double-click: enter feature | K: save pose | Space: play</div>
<div id="visibleSnapshot">VISIBLE CELLS: AREPO SNAPSHOT UNKNOWN</div>
<script>
const DATA = __PAYLOAD__;
const KEYFRAME_STORAGE='arepo_camera_lab_camera_poses_v002';
const LEGACY_KEYFRAME_STORAGE='arepo_camera_lab_keyframes_v001';
const canvas = document.getElementById('view');
const gl = canvas.getContext('webgl', {antialias: true, alpha: false});
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
function storedKeyframes(){
  try {
    const current=JSON.parse(localStorage.getItem(KEYFRAME_STORAGE)||'null');
    if(Array.isArray(current))return current;
    const legacy=JSON.parse(localStorage.getItem(LEGACY_KEYFRAME_STORAGE)||'[]');
    return Array.isArray(legacy)?legacy:[];
  } catch(error) { return []; }
}
function persistKeyframes(){try{localStorage.setItem(KEYFRAME_STORAGE,JSON.stringify(keyframes));return true;}catch(error){return false;}}
let keyframes=storedKeyframes(), dragging=false, last=[0,0], panMode=false, playing=null;
function setCamera(entry) { camera.target=[...entry.target]; camera.scale=entry.scale; Object.assign(camera,cleanBasis(entry.forward,entry.up)); }
function resize(){const ratio=devicePixelRatio||1,w=Math.floor(canvas.clientWidth*ratio),h=Math.floor(canvas.clientHeight*ratio);if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;gl.viewport(0,0,w,h);} }
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
function updateChannelMeta(){const [low,high]=safeRange();channelMeta.textContent=`${currentChannel.label} [${currentChannel.units}] | data ${formattedNumber(currentChannel.data_min)} to ${formattedNumber(currentChannel.data_max)} | visible ${formattedNumber(low)} to ${formattedNumber(high)}`;}
function applyPreset(){if(rangePreset.value==='custom')return;let low,high;if(rangePreset.value==='symmetric'){const bound=Math.max(Math.abs(currentChannel.percentiles[1]),Math.abs(currentChannel.percentiles[99]));low=-bound;high=bound;}else{const [a,b]=rangePreset.value.split(',').map(Number);low=currentChannel.percentiles[a];high=currentChannel.percentiles[b];}if(scaleMode.value==='log10'&&low<=0)low=currentChannel.positive_min??1e-30;setNumericValue('low',rangeLow,low);setNumericValue('high',rangeHigh,high);updateChannelMeta();}
function render(){resize();const [low,high]=safeRange();gl.clearColor(0.015,0.022,0.03,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.uniform3fv(locations.target,camera.target);gl.uniform3fv(locations.right,camera.right);gl.uniform3fv(locations.up,camera.up);gl.uniform3fv(locations.forward,camera.forward);gl.uniform1f(locations.scale,camera.scale);gl.uniform1f(locations.aspect,canvas.width/canvas.height);gl.uniform1f(locations.depth,4.0);gl.uniform1f(locations.point,+pointSize.value*(devicePixelRatio||1));gl.uniform1f(locations.domain_low,transformValue(low));gl.uniform1f(locations.domain_high,transformValue(high));gl.uniform1f(locations.scale_mode,scaleIds[scaleMode.value]);gl.uniform1f(locations.linthresh,Math.max(rangeState.linthresh,1e-30));gl.uniform1f(locations.opacity,+opacity.value);gl.uniform1f(locations.palette,paletteIds[palette.value]);gl.uniform1f(locations.gamma,+gamma.value);gl.uniform1f(locations.invert,invert.checked?1:0);gl.uniform1f(locations.saturation,+saturation.value);gl.uniform1f(locations.brightness,+brightness.value);gl.drawArrays(gl.POINTS,0,DATA.point_count);zoomReadout.textContent=`screen half extent ${(camera.scale*DATA.scene.display_radius_cm).toExponential(3)} cm (${camera.scale.toExponential(3)} scene radii)`;requestAnimationFrame(render);}
function loadChannel(name){currentChannel=DATA.channels[name];gl.bindBuffer(gl.ARRAY_BUFFER,valueBuffer);gl.bufferData(gl.ARRAY_BUFFER,decodeFloat32(currentChannel.values),gl.STATIC_DRAW);gl.vertexAttribPointer(valueLocation,1,gl.FLOAT,false,0,0);scaleMode.value=currentChannel.default_scale;palette.value=currentChannel.default_palette;setNumericValue('linthresh',linthresh,currentChannel.linthresh);setNumericValue('low',rangeLow,currentChannel.default_low);setNumericValue('high',rangeHigh,currentChannel.default_high);rangePreset.value=currentChannel.diverging?'symmetric':'1,99';symlogControl.style.display=scaleMode.value==='symlog'?'block':'none';updateColorBar();updateChannelMeta();}
const channel=document.getElementById('channel'),channelMeta=document.getElementById('channelMeta'),fieldNotice=document.getElementById('fieldNotice'),pointSize=document.getElementById('pointSize'),opacity=document.getElementById('opacity'),scaleMode=document.getElementById('scaleMode'),palette=document.getElementById('palette'),rangePreset=document.getElementById('rangePreset'),rangeLow=document.getElementById('rangeLow'),rangeHigh=document.getElementById('rangeHigh'),linthresh=document.getElementById('linthresh'),numericPrecision=document.getElementById('numericPrecision'),symlogControl=document.getElementById('symlogControl'),gamma=document.getElementById('gamma'),saturation=document.getElementById('saturation'),brightness=document.getElementById('brightness'),invert=document.getElementById('invert'),colorBar=document.getElementById('colorBar'),gammaValue=document.getElementById('gammaValue'),saturationValue=document.getElementById('saturationValue'),brightnessValue=document.getElementById('brightnessValue');
const sceneMeta=document.getElementById('sceneMeta'),statusText=document.getElementById('status'),snapshotInput=document.getElementById('snapshot'),poseCountText=document.getElementById('poseCount'),visibleSnapshot=document.getElementById('visibleSnapshot');
const resetButton=document.getElementById('reset'),fitButton=document.getElementById('fit'),rollLeftButton=document.getElementById('rollLeft'),rollRightButton=document.getElementById('rollRight');
const zoomInButton=document.getElementById('zoomIn'),zoomOutButton=document.getElementById('zoomOut'),zoomReadout=document.getElementById('zoomReadout');
const addPoseButton=document.getElementById('addPose'),copyPoseButton=document.getElementById('copyPose'),downloadButton=document.getElementById('download'),clearPosesButton=document.getElementById('clearPoses');
const timelinePanel=document.getElementById('timeline'),pathSlider=document.getElementById('pathSlider'),pathStatus=document.getElementById('pathStatus'),playButton=document.getElementById('play'),stopButton=document.getElementById('stop');
let currentChannel=null;
for(const name of Object.keys(DATA.channels)){const option=document.createElement('option');option.value=name;option.textContent=name.replaceAll('_',' ');channel.appendChild(option);} channel.value='rotational_fraction';loadChannel(channel.value);channel.onchange=()=>loadChannel(channel.value);
fieldNotice.textContent=DATA.scene.auxiliary_fields?`Auxiliary fields loaded: ${DATA.scene.auxiliary_fields.fields.join(', ')} | ${DATA.scene.auxiliary_fields.sha256.slice(0,12)}`:'Magnetic field, pressure, and entropy are unavailable because this v052 scene has no auxiliary field sidecar.';
scaleMode.onchange=()=>{symlogControl.style.display=scaleMode.value==='symlog'?'block':'none';if(scaleMode.value==='log10'&&rangeState.low<=0)setNumericValue('low',rangeLow,currentChannel.positive_min??1e-30);updateChannelMeta();};
palette.onchange=updateColorBar;invert.onchange=updateColorBar;rangePreset.onchange=applyPreset;
numericPrecision.onchange=reformatNumericInputs;
for(const [input,key] of [[rangeLow,'low'],[rangeHigh,'high'],[linthresh,'linthresh']]){input.onfocus=()=>{input.value=String(rangeState[key]);};input.oninput=()=>{const value=Number(input.value);if(Number.isFinite(value))rangeState[key]=value;rangePreset.value='custom';updateChannelMeta();};input.onblur=()=>setNumericValue(key,input,rangeState[key]);}
for(const [input,output] of [[gamma,gammaValue],[saturation,saturationValue],[brightness,brightnessValue]]){const update=()=>output.textContent=(+input.value).toFixed(2);input.oninput=update;update();}
document.getElementById('sceneMeta').textContent=`AREPO snapshot index ${DATA.scene.snapshot ?? 'unknown'} | ${DATA.point_count.toLocaleString()} / ${DATA.scene.num_cells.toLocaleString()} cells | radius ${DATA.scene.display_radius_cm.toExponential(3)} cm | scene ${DATA.scene.sha256.slice(0,12)}`;
snapshotInput.value=DATA.scene.snapshot ?? 'unknown';
visibleSnapshot.textContent=`VISIBLE CELLS: AREPO SNAPSHOT ${DATA.scene.snapshot ?? 'UNKNOWN'}`;
canvas.oncontextmenu=e=>e.preventDefault();canvas.onpointerdown=e=>{dragging=true;canvas.classList.add('dragging');last=[e.clientX,e.clientY];panMode=e.shiftKey||e.button===2;canvas.setPointerCapture(e.pointerId);};canvas.onpointerup=e=>{dragging=false;canvas.classList.remove('dragging');canvas.releasePointerCapture(e.pointerId);};canvas.onpointermove=e=>{if(!dragging)return;const dx=e.clientX-last[0],dy=e.clientY-last[1];last=[e.clientX,e.clientY];if(panMode){camera.target=V.add(camera.target,V.add(V.scale(camera.right,-dx*camera.scale*0.0025),V.scale(camera.up,dy*camera.scale*0.0025)));}else{let f=rotate(camera.forward,camera.up,-dx*0.006),r=V.unit(V.cross(f,camera.up));f=rotate(f,r,-dy*0.006);let u=rotate(camera.up,r,-dy*0.006);Object.assign(camera,cleanBasis(f,u));}};
canvas.onwheel=e=>{e.preventDefault();camera.scale=Math.min(100,Math.max(1e-6,camera.scale*Math.exp(e.deltaY*0.0015)));};
canvas.ondblclick=e=>{const rect=canvas.getBoundingClientRect(),aspect=canvas.width/canvas.height,x=((e.clientX-rect.left)/rect.width*2-1)*camera.scale*aspect,y=(1-(e.clientY-rect.top)/rect.height*2)*camera.scale;camera.target=V.add(camera.target,V.add(V.scale(camera.right,x),V.scale(camera.up,y)));camera.scale=Math.max(1e-6,camera.scale*0.35);};
function roll(angle){camera.up=rotate(camera.up,camera.forward,angle);Object.assign(camera,cleanBasis(camera.forward,camera.up));}
resetButton.onclick=()=>setCamera(initial);fitButton.onclick=()=>{camera.target=[0,0,0];camera.scale=1.05;};rollLeftButton.onclick=()=>roll(-Math.PI/90);rollRightButton.onclick=()=>roll(Math.PI/90);
zoomInButton.onclick=()=>{camera.scale=Math.max(1e-6,camera.scale*0.5);};zoomOutButton.onclick=()=>{camera.scale=Math.min(100,camera.scale*2);};
function pose(){if(DATA.scene.snapshot===null||DATA.scene.snapshot===undefined)throw new Error('The loaded scene has no AREPO snapshot index. Reload it with an explicit index before saving a pose.');const radius=DATA.scene.display_radius_cm,center=DATA.scene.center_cm,look=V.add(center,V.scale(camera.target,radius)),half=camera.scale*radius,pos=V.sub(look,V.scale(camera.forward,4*half));return {snapshot:Number(DATA.scene.snapshot),position_cm:pos,look_at_cm:look,view_direction:[...camera.forward],up:[...camera.up],screen_half_extent_cm:half,scene_sha256:DATA.scene.sha256,scene_path:DATA.scene.path};}
function uniqueSnapshotCount(){return new Set(keyframes.map(entry=>entry.snapshot)).size;}
function updateCount(){const epochs=uniqueSnapshotCount();poseCountText.textContent=`${keyframes.length} saved camera pose${keyframes.length===1?'':'s'} across ${epochs} AREPO snapshot${epochs===1?'':'s'}`;}
function poseIdentifier(snapshot){if(globalThis.crypto&&crypto.randomUUID)return `snapshot-${snapshot}-${crypto.randomUUID()}`;return `snapshot-${snapshot}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,10)}`;}
addPoseButton.onclick=async()=>{try{const p=pose();p.pose_id=poseIdentifier(p.snapshot);p.saved_at=new Date().toISOString();keyframes.push(p);const persisted=persistKeyframes();updateCount();const alternatives=keyframes.filter(entry=>entry.snapshot===p.snapshot).length;let serverCopy='';try{const response=await fetch('/api/pose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}),result=await response.json();if(response.ok)serverCopy=` and ${result.path}`;else throw new Error(result.error||`HTTP ${response.status}`);}catch(error){serverCopy=`; browser copy kept, server copy unavailable (${error.message})`;}statusText.textContent=`Saved no-clobber camera alternative ${alternatives} for visible AREPO snapshot ${p.snapshot}${persisted?' in this browser':' in memory only'}${serverCopy}.`;}catch(error){statusText.textContent=error.message;}};
copyPoseButton.onclick=async()=>{await navigator.clipboard.writeText(JSON.stringify(pose(),null,2));statusText.textContent='Current pose copied.';};
downloadButton.onclick=()=>{const latestBySnapshot=new Map();for(const entry of keyframes)latestBySnapshot.set(Number(entry.snapshot),entry);const selected=[...latestBySnapshot.values()].sort((a,b)=>a.snapshot-b.snapshot);const payload={schema:'stellar_camera_keyframes_v001',scene:DATA.scene,keyframes:selected,alternatives:keyframes,selection:'keyframes contains the latest saved pose per AREPO snapshot; alternatives preserves every no-clobber save'};const blob=new Blob([JSON.stringify(payload,null,2)+'\n'],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='stellar_camera_poses.json';a.click();URL.revokeObjectURL(a.href);statusText.textContent=`Downloaded ${selected.length} spline knot${selected.length===1?'':'s'} plus ${keyframes.length} preserved alternative${keyframes.length===1?'':'s'}.`;};
clearPosesButton.onclick=()=>{if(keyframes.length&&confirm('Clear all saved camera poses from this browser?')){keyframes=[];persistKeyframes();updateCount();statusText.textContent='Saved camera poses cleared.';}};
updateCount();
timelinePanel.style.display='block';
if(DATA.camera_path.length){pathSlider.max=DATA.camera_path.length-1;const show=i=>{const entry=DATA.camera_path[i];setCamera(entry);pathStatus.textContent=`camera path row ${entry.snapshot} (${i+1}/${DATA.camera_path.length}); visible cells remain AREPO snapshot ${DATA.scene.snapshot??'unknown'}`;};pathSlider.oninput=()=>show(+pathSlider.value);playButton.onclick=()=>{if(playing)return;playing=setInterval(()=>{let i=(+pathSlider.value+1)%DATA.camera_path.length;pathSlider.value=i;show(i);},50);};stopButton.onclick=()=>{clearInterval(playing);playing=null;};show(0);}else{pathSlider.disabled=true;playButton.disabled=true;stopButton.disabled=true;pathStatus.textContent='No spline is loaded. Save one camera pose at each of at least two different snapshots, compile them, then rebuild this viewer with --camera-path.';}
document.onkeydown=e=>{if(e.key==='k'||e.key==='K')addPoseButton.click();if(e.code==='Space'){e.preventDefault();if(playing)stopButton.click();else if(DATA.camera_path.length)playButton.click();else statusText.textContent='Space plays a compiled path; this single-scene viewer has none loaded yet.';}if(e.key==='r'||e.key==='R')resetButton.click();};
render();
</script>
</body>
</html>
'''


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
