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
    raw = np.asarray(values, dtype="<f4").tobytes(order="C")
    return base64.b64encode(raw).decode("ascii")


def _normalize_channel(values: np.ndarray, diverging: bool) -> tuple[np.ndarray, dict]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.full(values.shape, 0.5, dtype=np.float32), {
            "low": 0.0, "high": 1.0, "diverging": diverging}
    if diverging:
        bound = float(np.quantile(np.abs(finite), 0.99))
        bound = max(bound, 1.0e-30)
        low, high = -bound, bound
    else:
        low, high = [float(value) for value in np.quantile(finite, [0.01, 0.99])]
        if not high > low:
            high = low + 1.0
    normalized = np.clip((values - low) / (high - low), 0.0, 1.0)
    normalized[~np.isfinite(normalized)] = 0.5
    return normalized.astype(np.float32), {
        "low": low, "high": high, "diverging": diverging}


def build_payload(scene: Path, header: dict, cells: np.memmap,
                  selected: np.ndarray, center_native: np.ndarray,
                  axis: np.ndarray, display_radius_cm: float | None,
                  scene_digest: str, snapshot: int | None,
                  camera_path: Path | None) -> dict:
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
    radial_velocity = np.sum(velocity * radial_unit, axis=1) / 1.0e8
    height = np.sum(relative_cm * axis[None, :], axis=1)
    cylindrical = relative_cm - height[:, None] * axis[None, :]
    cylindrical_radius = np.linalg.norm(cylindrical, axis=1)
    cylindrical_unit = np.divide(cylindrical, cylindrical_radius[:, None],
                                 out=np.zeros_like(cylindrical),
                                 where=cylindrical_radius[:, None] > 0.0)
    azimuthal_unit = np.cross(axis[None, :], cylindrical_unit)
    azimuthal_velocity = np.sum(velocity * azimuthal_unit, axis=1)
    rotational_fraction = np.abs(azimuthal_velocity) / np.maximum(speed, 1.0)
    outward_axial = np.sign(height) * np.sum(velocity * axis[None, :], axis=1) / 1.0e8
    angular_momentum = np.sum(np.cross(relative_cm, velocity) * axis[None, :], axis=1)
    angular_momentum_alignment = angular_momentum / np.maximum(radius * speed, 1.0)
    density_log10 = np.asarray(cells["density"][selected], dtype=float) - 10.0
    outward_mass_flux = density_log10 + np.log10(
        np.maximum(outward_axial * 1.0e8, 1.0))
    channels_raw = {
        "density": (density_log10, False, "log10 density proxy"),
        "temperature": (np.log10(np.maximum(
            np.asarray(cells["temperature"][selected], dtype=float), 1.0)),
                        False, "log10 temperature [K]"),
        "speed": (np.log10(np.maximum(speed, 1.0)), False,
                  "log10 speed [cm/s]"),
        "radial_velocity": (radial_velocity, True,
                            "radial velocity [1e8 cm/s]"),
        "azimuthal_velocity": (azimuthal_velocity / 1.0e8, True,
                               "signed azimuthal velocity [1e8 cm/s]"),
        "rotational_fraction": (rotational_fraction, False,
                                "absolute azimuthal speed / total speed"),
        "angular_momentum_alignment": (angular_momentum_alignment, True,
                                       "signed axial angular-momentum alignment"),
        "outward_axial_velocity": (outward_axial, True,
                                   "signed outward axial speed [1e8 cm/s]"),
        "outward_mass_flux_proxy": (outward_mass_flux, False,
                                    "log10 outward rho-v proxy"),
        "cylindrical_radius": (cylindrical_radius / display_radius_cm, False,
                               "cylindrical radius / display radius"),
        "axial_position": (height / display_radius_cm, True,
                           "signed axial position / display radius"),
    }
    channels = {}
    for name, (values, diverging, label) in channels_raw.items():
        normalized, metadata = _normalize_channel(values, diverging)
        metadata.update({"label": label, "values": _encode_float32(normalized)})
        channels[name] = metadata

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
#view { position: fixed; left: 320px; top: 0; width: calc(100vw - 320px); height: 100vh; cursor: grab; }
#view.dragging { cursor: grabbing; }
#panel { position: fixed; left: 0; top: 0; width: 320px; height: 100vh; overflow-y: auto; padding: 16px; background: #11151a; border-right: 1px solid #303741; }
h1 { margin: 0 0 14px; font-size: 17px; font-weight: 650; }
h2 { margin: 19px 0 8px; font-size: 12px; color: #9fb0be; text-transform: uppercase; }
label { display: block; margin: 8px 0 4px; font-size: 12px; color: #c5cdd3; }
select, input, button { width: 100%; min-height: 31px; border: 1px solid #3b4651; background: #171d23; color: #edf2f5; border-radius: 4px; }
input[type=range] { min-height: 24px; }
button { margin-top: 6px; cursor: pointer; }
button:hover { background: #222b33; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
.meta { font-size: 11px; line-height: 1.55; color: #8fa0ae; overflow-wrap: anywhere; }
#status { margin-top: 10px; color: #b7c9d5; }
#timeline { display: none; }
.hint { position: fixed; right: 14px; bottom: 12px; padding: 6px 8px; background: rgba(8,11,14,.78); color: #a9b6bf; font-size: 11px; }
@media (max-width: 760px) { #panel { width: 260px; } #view { left: 260px; width: calc(100vw - 260px); } }
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
  <label for="clipLow">Visible low percentile</label><input id="clipLow" type="range" min="0" max="0.95" value="0" step="0.01">
  <label for="clipHigh">Visible high percentile</label><input id="clipHigh" type="range" min="0.05" max="1" value="1" step="0.01">
  <div class="row">
    <div><label for="pointSize">Point size</label><input id="pointSize" type="range" min="1" max="8" value="2.2" step="0.1"></div>
    <div><label for="opacity">Opacity</label><input id="opacity" type="range" min="0.05" max="1" value="0.72" step="0.01"></div>
  </div>
  <h2>Camera</h2>
  <div class="row"><button id="reset">Reset</button><button id="rollLeft">Roll -2 deg</button></div>
  <div class="row"><button id="fit">Fit cloud</button><button id="rollRight">Roll +2 deg</button></div>
  <div class="row"><button id="zoomIn">Zoom in 2x</button><button id="zoomOut">Zoom out 2x</button></div>
  <div id="zoomReadout" class="meta"></div>
  <label for="snapshot">Key pose snapshot</label><input id="snapshot" type="number" min="0" step="1">
  <div class="meta">A key pose is one spline control point: this camera orientation, look-at point, roll, and zoom at the named simulation snapshot. One pose does not create animation.</div>
  <button id="addPose">Add current key pose</button>
  <div class="row"><button id="copyPose">Copy current pose</button><button id="download">Download key poses</button></div>
  <button id="clearPoses">Clear saved key poses</button>
  <div id="poseCount" class="meta">0 key poses</div>
  <div id="timeline">
    <h2>Spline Playback</h2>
    <input id="pathSlider" type="range" min="0" max="0" value="0" step="1">
    <div class="row"><button id="play">Play</button><button id="stop">Stop</button></div>
    <div id="pathStatus" class="meta"></div>
  </div>
  <div id="status" class="meta"></div>
</aside>
<div class="hint">Drag: orbit | Shift/right drag: pan | Wheel: zoom | Double-click: enter feature | K: save pose | Space: play</div>
<script>
const DATA = __PAYLOAD__;
const KEYFRAME_STORAGE='arepo_camera_lab_keyframes_v001';
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
attribute vec3 a_position; attribute float a_value;
uniform vec3 u_target, u_right, u_up, u_forward;
uniform float u_scale, u_aspect, u_depth, u_point, u_low, u_high;
varying float v_value, v_visible;
void main() {
  vec3 rel = a_position - u_target;
  float x = dot(rel, u_right) / (u_scale * u_aspect);
  float y = dot(rel, u_up) / u_scale;
  float z = clamp(dot(rel, u_forward) / u_depth, -0.999, 0.999);
  v_value = a_value; v_visible = step(u_low, a_value) * step(a_value, u_high);
  gl_Position = v_visible > 0.5 ? vec4(x, y, z, 1.0) : vec4(2.0, 2.0, 1.0, 1.0);
  gl_PointSize = u_point;
}`);
const fragment = shader(gl.FRAGMENT_SHADER, `
precision mediump float; varying float v_value, v_visible;
uniform float u_opacity, u_diverging;
vec3 sequential(float t) {
  vec3 a=vec3(0.025,0.055,0.10), b=vec3(0.16,0.38,0.55), c=vec3(0.72,0.34,0.12), d=vec3(1.0,0.88,0.62);
  return t<0.34 ? mix(a,b,t/0.34) : (t<0.72 ? mix(b,c,(t-0.34)/0.38) : mix(c,d,(t-0.72)/0.28));
}
vec3 diverging(float t) {
  vec3 blue=vec3(0.12,0.42,0.88), mid=vec3(0.82,0.85,0.84), red=vec3(0.88,0.30,0.10);
  return t<0.5 ? mix(blue,mid,t*2.0) : mix(mid,red,(t-0.5)*2.0);
}
void main() {
  if (v_visible < 0.5 || length(gl_PointCoord-vec2(0.5)) > 0.5) discard;
  vec3 color = u_diverging > 0.5 ? diverging(v_value) : sequential(v_value);
  gl_FragColor = vec4(color, u_opacity);
}`);
const program = gl.createProgram(); gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program);
if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
gl.useProgram(program);
const locations = {};
for (const name of ['target','right','up','forward','scale','aspect','depth','point','low','high','opacity','diverging'])
  locations[name] = gl.getUniformLocation(program, 'u_'+name);
const positionBuffer=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,positionBuffer); gl.bufferData(gl.ARRAY_BUFFER,decodeFloat32(DATA.positions),gl.STATIC_DRAW);
const positionLocation=gl.getAttribLocation(program,'a_position'); gl.enableVertexAttribArray(positionLocation); gl.vertexAttribPointer(positionLocation,3,gl.FLOAT,false,0,0);
const valueBuffer=gl.createBuffer(); const valueLocation=gl.getAttribLocation(program,'a_value'); gl.enableVertexAttribArray(valueLocation);

const V={add:(a,b)=>a.map((x,i)=>x+b[i]), sub:(a,b)=>a.map((x,i)=>x-b[i]), scale:(a,s)=>a.map(x=>x*s), dot:(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0), cross:(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]], norm:a=>Math.hypot(...a), unit:a=>{const n=Math.hypot(...a);return a.map(x=>x/n)}};
function rotate(vector, axis, angle) { const u=V.unit(axis), c=Math.cos(angle),s=Math.sin(angle); return V.add(V.add(V.scale(vector,c),V.scale(V.cross(u,vector),s)),V.scale(u,V.dot(u,vector)*(1-c))); }
function cleanBasis(forward,up) { forward=V.unit(forward); let right=V.unit(V.cross(forward,up)); up=V.unit(V.cross(right,forward)); return {forward,up,right}; }
const initial=DATA.initial_camera;
let camera={target:[...initial.target],scale:initial.scale,...cleanBasis(initial.forward,initial.up)};
function storedKeyframes(){try{const value=JSON.parse(localStorage.getItem(KEYFRAME_STORAGE)||'[]');return Array.isArray(value)?value:[];}catch(error){return [];}}
function persistKeyframes(){try{localStorage.setItem(KEYFRAME_STORAGE,JSON.stringify(keyframes));return true;}catch(error){return false;}}
let keyframes=storedKeyframes(), dragging=false, last=[0,0], panMode=false, playing=null;
function setCamera(entry) { camera.target=[...entry.target]; camera.scale=entry.scale; Object.assign(camera,cleanBasis(entry.forward,entry.up)); }
function resize(){const ratio=devicePixelRatio||1,w=Math.floor(canvas.clientWidth*ratio),h=Math.floor(canvas.clientHeight*ratio);if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;gl.viewport(0,0,w,h);} }
function render(){resize();gl.clearColor(0.015,0.022,0.03,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.uniform3fv(locations.target,camera.target);gl.uniform3fv(locations.right,camera.right);gl.uniform3fv(locations.up,camera.up);gl.uniform3fv(locations.forward,camera.forward);gl.uniform1f(locations.scale,camera.scale);gl.uniform1f(locations.aspect,canvas.width/canvas.height);gl.uniform1f(locations.depth,4.0);gl.uniform1f(locations.point,+pointSize.value*(devicePixelRatio||1));gl.uniform1f(locations.low,+clipLow.value);gl.uniform1f(locations.high,+clipHigh.value);gl.uniform1f(locations.opacity,+opacity.value);gl.drawArrays(gl.POINTS,0,DATA.point_count);zoomReadout.textContent=`screen half extent ${(camera.scale*DATA.scene.display_radius_cm).toExponential(3)} cm (${camera.scale.toExponential(3)} scene radii)`;requestAnimationFrame(render);}
function loadChannel(name){const item=DATA.channels[name];gl.bindBuffer(gl.ARRAY_BUFFER,valueBuffer);gl.bufferData(gl.ARRAY_BUFFER,decodeFloat32(item.values),gl.STATIC_DRAW);gl.vertexAttribPointer(valueLocation,1,gl.FLOAT,false,0,0);gl.uniform1f(locations.diverging,item.diverging?1:0);channelMeta.textContent=`${item.label}; display range ${item.low.toPrecision(4)} to ${item.high.toPrecision(4)}`;}
const channel=document.getElementById('channel'),channelMeta=document.getElementById('channelMeta'),pointSize=document.getElementById('pointSize'),opacity=document.getElementById('opacity'),clipLow=document.getElementById('clipLow'),clipHigh=document.getElementById('clipHigh');
const sceneMeta=document.getElementById('sceneMeta'),statusText=document.getElementById('status'),snapshotInput=document.getElementById('snapshot'),poseCountText=document.getElementById('poseCount');
const resetButton=document.getElementById('reset'),fitButton=document.getElementById('fit'),rollLeftButton=document.getElementById('rollLeft'),rollRightButton=document.getElementById('rollRight');
const zoomInButton=document.getElementById('zoomIn'),zoomOutButton=document.getElementById('zoomOut'),zoomReadout=document.getElementById('zoomReadout');
const addPoseButton=document.getElementById('addPose'),copyPoseButton=document.getElementById('copyPose'),downloadButton=document.getElementById('download'),clearPosesButton=document.getElementById('clearPoses');
const timelinePanel=document.getElementById('timeline'),pathSlider=document.getElementById('pathSlider'),pathStatus=document.getElementById('pathStatus'),playButton=document.getElementById('play'),stopButton=document.getElementById('stop');
for(const name of Object.keys(DATA.channels)){const option=document.createElement('option');option.value=name;option.textContent=name.replaceAll('_',' ');channel.appendChild(option);} channel.value='rotational_fraction';loadChannel(channel.value);channel.onchange=()=>loadChannel(channel.value);
document.getElementById('sceneMeta').textContent=`snapshot ${DATA.scene.snapshot ?? 'unknown'} | ${DATA.point_count.toLocaleString()} / ${DATA.scene.num_cells.toLocaleString()} cells | radius ${DATA.scene.display_radius_cm.toExponential(3)} cm | scene ${DATA.scene.sha256.slice(0,12)}`;
if(DATA.scene.snapshot !== null) snapshotInput.value=DATA.scene.snapshot;
canvas.oncontextmenu=e=>e.preventDefault();canvas.onpointerdown=e=>{dragging=true;canvas.classList.add('dragging');last=[e.clientX,e.clientY];panMode=e.shiftKey||e.button===2;canvas.setPointerCapture(e.pointerId);};canvas.onpointerup=e=>{dragging=false;canvas.classList.remove('dragging');canvas.releasePointerCapture(e.pointerId);};canvas.onpointermove=e=>{if(!dragging)return;const dx=e.clientX-last[0],dy=e.clientY-last[1];last=[e.clientX,e.clientY];if(panMode){camera.target=V.add(camera.target,V.add(V.scale(camera.right,-dx*camera.scale*0.0025),V.scale(camera.up,dy*camera.scale*0.0025)));}else{let f=rotate(camera.forward,camera.up,-dx*0.006),r=V.unit(V.cross(f,camera.up));f=rotate(f,r,-dy*0.006);let u=rotate(camera.up,r,-dy*0.006);Object.assign(camera,cleanBasis(f,u));}};
canvas.onwheel=e=>{e.preventDefault();camera.scale=Math.min(100,Math.max(1e-6,camera.scale*Math.exp(e.deltaY*0.0015)));};
canvas.ondblclick=e=>{const rect=canvas.getBoundingClientRect(),aspect=canvas.width/canvas.height,x=((e.clientX-rect.left)/rect.width*2-1)*camera.scale*aspect,y=(1-(e.clientY-rect.top)/rect.height*2)*camera.scale;camera.target=V.add(camera.target,V.add(V.scale(camera.right,x),V.scale(camera.up,y)));camera.scale=Math.max(1e-6,camera.scale*0.35);};
function roll(angle){camera.up=rotate(camera.up,camera.forward,angle);Object.assign(camera,cleanBasis(camera.forward,camera.up));}
resetButton.onclick=()=>setCamera(initial);fitButton.onclick=()=>{camera.target=[0,0,0];camera.scale=1.05;};rollLeftButton.onclick=()=>roll(-Math.PI/90);rollRightButton.onclick=()=>roll(Math.PI/90);
zoomInButton.onclick=()=>{camera.scale=Math.max(1e-6,camera.scale*0.5);};zoomOutButton.onclick=()=>{camera.scale=Math.min(100,camera.scale*2);};
function pose(){const radius=DATA.scene.display_radius_cm,center=DATA.scene.center_cm,look=V.add(center,V.scale(camera.target,radius)),half=camera.scale*radius,pos=V.sub(look,V.scale(camera.forward,4*half));return {snapshot:+snapshotInput.value,position_cm:pos,look_at_cm:look,view_direction:[...camera.forward],up:[...camera.up],screen_half_extent_cm:half,scene_sha256:DATA.scene.sha256,scene_path:DATA.scene.path};}
function updateCount(){poseCountText.textContent=`${keyframes.length} key pose${keyframes.length===1?'':'s'}`;}
addPoseButton.onclick=()=>{if(snapshotInput.value===''){statusText.textContent='Enter a snapshot number first.';return;}const p=pose();const old=keyframes.findIndex(k=>k.snapshot===p.snapshot);if(old>=0)keyframes[old]=p;else keyframes.push(p);keyframes.sort((a,b)=>a.snapshot-b.snapshot);const persisted=persistKeyframes();updateCount();statusText.textContent=`Stored pose at snapshot ${p.snapshot}${persisted?' for this browser session':' in memory only'}.`;};
copyPoseButton.onclick=async()=>{await navigator.clipboard.writeText(JSON.stringify(pose(),null,2));statusText.textContent='Current pose copied.';};
downloadButton.onclick=()=>{const payload={schema:'stellar_camera_keyframes_v001',scene:DATA.scene,keyframes};const blob=new Blob([JSON.stringify(payload,null,2)+'\n'],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='stellar_camera_keyframes.json';a.click();URL.revokeObjectURL(a.href);};
clearPosesButton.onclick=()=>{if(keyframes.length&&confirm('Clear all saved camera key poses from this browser?')){keyframes=[];persistKeyframes();updateCount();statusText.textContent='Saved key poses cleared.';}};
updateCount();
timelinePanel.style.display='block';
if(DATA.camera_path.length){pathSlider.max=DATA.camera_path.length-1;const show=i=>{const entry=DATA.camera_path[i];setCamera(entry);snapshotInput.value=entry.snapshot;pathStatus.textContent=`snapshot ${entry.snapshot} (${i+1}/${DATA.camera_path.length})`;};pathSlider.oninput=()=>show(+pathSlider.value);playButton.onclick=()=>{if(playing)return;playing=setInterval(()=>{let i=(+pathSlider.value+1)%DATA.camera_path.length;pathSlider.value=i;show(i);},50);};stopButton.onclick=()=>{clearInterval(playing);playing=null;};show(0);}else{pathSlider.disabled=true;playButton.disabled=true;stopButton.disabled=true;pathStatus.textContent='No spline is loaded. Save poses from at least two snapshots, compile them, then rebuild this viewer with --camera-path.';}
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
        digest = args.scene_sha256 or sha256(args.scene)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
            raise ValueError("--scene-sha256 must be a 64-character hexadecimal digest")
        payload = build_payload(args.scene, header, cells, selected, center_native,
                                inferred_axis, args.display_radius_cm,
                                digest.lower(), args.snapshot, args.camera_path)
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
