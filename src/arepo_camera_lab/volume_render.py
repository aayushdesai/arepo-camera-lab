"""Native Metal preview integration through the prepared Voronoi cells.

This is a display transfer, not calibrated radiation transport. Every ray uses
the stored neighbour planes and physical path lengths; no voxel grid or image
blur is introduced. The VTK face renderer remains a separate diagnostic view.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import io
import json
import math
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

import numpy as np

from .mesh_render import transfer_colors, transform


DEFAULT_VOLUME = {"density_reference": 1e4, "density_power": .5,
                  "opacity_length_cm": 1e9, "floor_softening_dex": 1.0}


def volume_options(params: dict) -> dict:
    result = dict(DEFAULT_VOLUME)
    for name in result:
        result[name] = float(params.get("volume", {}).get(name, result[name]))
        if not math.isfinite(result[name]):
            raise ValueError("Volume transparency parameters must be finite")
    if result["density_reference"] <= 0 or result["opacity_length_cm"] <= 0:
        raise ValueError("Volume reference density and path length must be positive")
    if not 0 <= result["density_power"] <= 2 or not 0 <= result["floor_softening_dex"] <= 4:
        raise ValueError("Volume density power must be in [0,2] and floor softening in [0,4] dex")
    return result


def extinction(density, visible, density_floor: float, opacity: float,
               radius_cm: float, options: dict) -> np.ndarray:
    """Return extinction per normalized scene length; independent of camera zoom."""
    rho = np.asarray(density, dtype=np.float64)
    with np.errstate(all="ignore"):
        support = (rho >= density_floor).astype(float)
        if density_floor > 0 and options["floor_softening_dex"] > 0:
            ramp = np.clip(np.log10(rho / density_floor) / options["floor_softening_dex"], 0, 1)
            support = ramp * ramp * (3 - 2 * ramp)
        optical_depth = -math.log(max(1-opacity, 1e-3))
        value = optical_depth * (rho / options["density_reference"]) ** options["density_power"]
        value *= radius_cm / options["opacity_length_cm"] * support
    value = np.where(visible & (rho > 0), value, 0)
    if not np.all(np.isfinite(value)) or np.any(value > np.finfo(np.float32).max):
        raise ValueError("Volume transparency parameters overflow the supported numerical range")
    return value.astype(np.float32)


def compile_volume(directory: Path) -> Path:
    if sys.platform != "darwin":
        raise ValueError("The native Metal volume view requires macOS; select VTK cell faces on this host")
    source = Path(__file__).with_name("native_volume.mm")
    shader = source.with_suffix(".metal")
    digest = hashlib.sha256(source.read_bytes() + shader.read_bytes()).hexdigest()[:16]
    binary = directory / f"native_volume_{digest}.dylib"
    if binary.is_file():
        return binary
    compiler = shutil.which("clang++")
    if compiler is None:
        raise ValueError("The native Metal volume view needs the Xcode Command Line Tools")
    result = subprocess.run([compiler, "-std=c++17", "-O3", "-dynamiclib", "-fobjc-arc",
                             "-framework", "Metal", "-framework", "Foundation",
                             str(source), "-o", str(binary)], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise ValueError("Native Metal bridge compilation failed: " + result.stderr[-3000:])
    return binary


class MetalVolume:
    """Persistent Metal geometry, transfer buffers, and a native-cell locator."""
    def __init__(self, owner):
        self.owner, self.handle, self.transfer_key = owner, None, None
        self.setup_started = time.monotonic()
        owner.progress("Preparing the full native mesh for Metal volume rendering")
        self.binary = compile_volume(owner.directory)
        self.library = C.CDLL(str(self.binary))
        pointer = C.c_void_p
        self.library.av_create.argtypes = [C.c_char_p, pointer, pointer, pointer,
                                           C.c_uint32, C.c_uint32, C.c_char_p, C.c_size_t]
        self.library.av_create.restype = pointer
        self.library.av_device.argtypes = [pointer]
        self.library.av_device.restype = C.c_char_p
        self.library.av_fields.argtypes = [pointer, pointer, pointer, C.c_char_p, C.c_size_t]
        self.library.av_render.argtypes = [pointer, C.c_uint32, C.c_uint32, C.c_uint32,
                                           C.c_uint32, pointer, pointer, pointer, pointer,
                                           C.c_char_p, C.c_size_t]
        self.library.av_close.argtypes = [pointer]
        self.error = C.create_string_buffer(4096)
        n, edges = int(owner.header["num_cells"]), int(owner.header["num_edges"])
        if n >= 0x7fffffff or edges >= 0xffffffff:
            raise ValueError("Native volume connectivity exceeds 32-bit indexing")
        self.positions = np.zeros((n, 4), dtype=np.float32)
        relative = np.asarray(owner.cells["position"], dtype=np.float64) * owner.header["position_unit_cm"]
        relative -= np.asarray(owner.meta["center_cm"])
        self.box_cm = owner.header["box_size"] * owner.header["position_unit_cm"]
        if not math.isfinite(self.box_cm) or self.box_cm <= 0 or not np.all(np.isfinite(relative)):
            raise ValueError("Native volume requires finite generators and a positive periodic box size")
        relative -= np.round(relative / self.box_cm) * self.box_cm
        self.positions[:, :3] = relative / owner.meta["display_radius_cm"]
        offsets64 = np.memmap(owner.scene, mode="r", dtype="<u8", offset=208+n*52, shape=(n+1,))
        if offsets64[0] != 0 or offsets64[-1] != edges or np.any(offsets64[1:] < offsets64[:-1]):
            raise ValueError("Invalid native neighbour offsets")
        offsets = offsets64.astype(np.uint32)
        edge_bytes = np.memmap(owner.scene, mode="r", dtype=np.uint8,
                              offset=208+n*52+(n+1)*8, shape=(edges*16,))
        self.handle = self.library.av_create(str(Path(__file__).with_name("native_volume.metal")).encode(),
                         self.positions.ctypes.data, offsets.ctypes.data, edge_bytes.ctypes.data,
                         n, edges, self.error, len(self.error))
        if not self.handle:
            raise ValueError(self.error.value.decode())
        self.device = self.library.av_device(self.handle).decode()
        self.setup_seconds = time.monotonic() - self.setup_started
        self.capabilities = (f"Native Metal compute\nDevice: {self.device}\n"
                             f"macOS: {platform.mac_ver()[0]}\nArchitecture: {platform.machine()}\n"
                             "Geometry: full native v052 neighbour planes\n"
                             "Reconstruction: piecewise constant per native cell\n"
                             "Compositing: physical segment lengths, linear-light display transfer\n"
                             "Fallback: none\n")

    def upload_transfer(self, fields: np.ndarray, palette: np.ndarray):
        fields = np.ascontiguousarray(fields, dtype=np.float32)
        palette = np.ascontiguousarray(palette, dtype=np.float32)
        if fields.shape != (len(self.positions), 2) or palette.shape != (512, 4):
            raise ValueError("Native volume transfer buffer shape mismatch")
        if self.library.av_fields(self.handle, fields.ctypes.data, palette.ctypes.data,
                                  self.error, len(self.error)):
            raise ValueError(self.error.value.decode())

    def trace(self, camera: dict, width: int, height: int, subpixels: int):
        if width <= 0 or height <= 0 or width > 1920 or height > 1200 or subpixels not in (1, 4):
            raise ValueError("Volume viewport must be at most 1920 by 1200 with 1 or 4 rays per pixel")
        forward, up = np.asarray(camera["forward"]), np.asarray(camera["up"])
        right = np.cross(forward, up)
        radius = self.owner.meta["display_radius_cm"]
        uniform = np.array([*camera["target"], camera["scale"], *forward, width/height,
                            *right, self.box_cm/radius, *up,
                            self.owner.header["position_unit_cm"]/radius], dtype=np.float32)
        if not np.all(np.isfinite(uniform)):
            raise ValueError("Camera is outside the native volume numerical range")
        pixels = np.empty((height, width, subpixels, 4), dtype=np.float32)
        stats = np.empty((height, width, subpixels, 4), dtype=np.uint32)
        gpu_seconds = C.c_double()
        if self.library.av_render(self.handle, width, height, subpixels, 8192, uniform.ctypes.data,
                                  pixels.ctypes.data, stats.ctypes.data, C.byref(gpu_seconds),
                                  self.error, len(self.error)):
            raise ValueError(self.error.value.decode())
        failures = stats[..., 0] != 0
        if np.any(failures):
            values, counts = np.unique(stats[..., 0][failures], return_counts=True)
            errors = dict(zip(map(str, values), map(int, counts)))
            raise ValueError(f"Native volume traversal failed for {int(failures.sum())} rays: {errors}")
        if not np.all(np.isfinite(pixels)):
            raise ValueError("Native volume returned non-finite pixels")
        report = {"gpu_seconds": gpu_seconds.value, "rays": int(stats[..., 0].size),
                  "traversal_failures": 0, "max_cells_per_ray": int(stats[..., 1].max()),
                  "mean_cells_per_ray": float(stats[..., 1].mean()),
                  "zero_length_transitions": int(stats[..., 2].sum()),
                  "subpixel_samples": subpixels}
        return pixels.mean(axis=2), report

    def render(self, params: dict):
        from PIL import Image
        from .mesh_capture import fit_camera
        started = time.monotonic()
        owner, style = self.owner, dict(params["style"])
        low, high = float(style["low"]), float(style["high"])
        if not (math.isfinite(low) and math.isfinite(high) and high > low):
            raise ValueError("Visible field limits must be finite and increasing")
        bounds = transform(np.array([low, high]), style)
        if not np.all(np.isfinite(bounds)) or bounds[1] <= bounds[0]:
            raise ValueError("Field limits are invalid for the selected scale")
        floor = float(params.get("density_floor", 100.0))
        opacity = float(style.get("opacity", .72))
        if not math.isfinite(floor) or floor < 0 or not 0 < opacity <= 1:
            raise ValueError("Density floor must be nonnegative and opacity in (0,1]")
        options = volume_options(params)
        key = json.dumps([style, params.get("derived_channels", []), floor, options], sort_keys=True)
        if key != self.transfer_key:
            owner.progress("Updating field colours and density transparency")
            values = owner.values(style["channel"], params.get("derived_channels", []))
            scalar = transform(values, style)
            visible = np.isfinite(scalar) & (values >= low) & (values <= high)
            fields = np.zeros((len(self.positions), 2), dtype=np.float32)
            fields[owner.selected, 0] = (scalar-bounds[0]) / (bounds[1]-bounds[0])
            fields[owner.selected, 1] = extinction(owner.channels["density"], visible, floor,
                                                  opacity, owner.meta["display_radius_cm"], options)
            self.visible_indices = np.flatnonzero(fields[:, 1] > 0)
            colors = transfer_colors(style).astype(np.float32) / 255
            colors[:, :3] = np.where(colors[:, :3] <= .04045, colors[:, :3] / 12.92,
                                     ((colors[:, :3] + .055) / 1.055) ** 2.4)
            self.upload_transfer(fields, colors)
            self.transfer_key = key
        width, height = int(params.get("width", 960)), int(params.get("height", 600))
        if width <= 0 or height <= 0:
            raise ValueError("Viewport dimensions must be positive")
        resize = min(1., 1920/width, 1200/height)
        width, height = max(1, round(width*resize)), max(1, round(height*resize))
        camera = params["camera"]
        if params.get("fit_visible"):
            if not len(self.visible_indices):
                raise ValueError("No visible native cells to fit")
            camera = fit_camera(self.positions[self.visible_indices, :3], camera, width/height)
        owner.set_camera(camera)
        owner.progress("Integrating rays through native Voronoi cells on Metal")
        pixels, stats = self.trace(owner.camera, width, height, int(params.get("subpixel_samples", 4)))
        linear = np.clip(pixels[:, :, :3], 0, 1)
        srgb = np.where(linear <= .0031308, 12.92*linear, 1.055*linear**(1/2.4)-.055)
        picture = Image.fromarray(np.rint(srgb*255).astype(np.uint8))
        output = io.BytesIO()
        picture.save(output, format="PNG")
        report = {**stats, "snapshot": owner.meta["snapshot"],
                  "snapshot_time_seconds": owner.meta["snapshot_time_seconds"],
                  "scene_sha256": owner.meta["sha256"], "native_cell_count": int(owner.header["num_cells"]),
                  "selected_cells": int(len(self.visible_indices)), "width": width, "height": height,
                  "render_seconds": time.monotonic()-started, "setup_seconds": self.setup_seconds,
                  "backend": "native_metal_voronoi_volume_v001", "device": self.device,
                  "representation": "volume", "density_floor": floor, "volume": options,
                  "reconstruction": "piecewise_constant", "ruler_kind": "projected",
                  "early_termination_transmittance": .001, "camera": owner.camera, "style": style}
        return output.getvalue(), report

    def close(self):
        if self.handle:
            self.library.av_close(self.handle)
            self.handle = None
