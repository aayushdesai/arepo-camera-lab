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
                  "opacity_length_cm": 1e9, "floor_softening_dex": 1.0,
                  "dense_fade_start": 0.0, "dense_opacity_fraction": 1.0}
RECONSTRUCTIONS = {"piecewise_constant": 0, "continuous": 1, "linear": 2,
                   "continuous_linear": 3}


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
    if result["dense_fade_start"] < 0 or not 0 <= result["dense_opacity_fraction"] <= 1:
        raise ValueError("Dense gas fade density must be nonnegative and opacity fraction in [0,1]")
    result["reconstruction"] = params.get("volume", {}).get("reconstruction", "linear")
    if result["reconstruction"] not in RECONSTRUCTIONS:
        raise ValueError("Unknown volume reconstruction")
    result["transfer_stage"] = params.get("volume", {}).get("transfer_stage", "before_reconstruction")
    result["range_behavior"] = params.get("volume", {}).get("range_behavior", "hide")
    if result["transfer_stage"] not in ("before_reconstruction", "after_reconstruction"):
        raise ValueError("Invalid volume transfer stage")
    if result["range_behavior"] not in ("hide", "clamp"):
        raise ValueError("Invalid volume colour range behavior")
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
        if options.get("dense_fade_start", 0) > 0:
            ramp = np.clip(np.log10(rho/options["dense_fade_start"]), 0, 1)
            support *= 1-(1-options.get("dense_opacity_fraction", 1))*ramp*ramp*(3-2*ramp)
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
        self.library.av_fields.argtypes = [pointer, pointer, pointer, C.c_float, pointer, C.c_char_p, C.c_size_t]
        self.library.av_gradient_seconds.argtypes = [pointer]
        self.library.av_gradient_seconds.restype = C.c_double
        self.library.av_gradient_fallbacks.argtypes = [pointer]
        self.library.av_gradient_fallbacks.restype = C.c_uint32
        self.library.av_render.argtypes = [pointer, C.c_uint32, C.c_uint32, C.c_uint32,
                                           C.c_uint32, C.c_uint32, C.c_uint32,
                                           pointer, pointer, pointer, pointer,
                                           C.c_char_p, C.c_size_t]
        self.library.av_sample_fields.argtypes = [pointer, pointer, C.c_uint32, C.c_float, C.c_uint32,
                                                  C.c_uint32, pointer, C.c_char_p, C.c_size_t]
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
                             "Reconstruction: continuous limited-gradient blend, limited linear field, original cells, or legacy compact Shepard\n"
                             "Transfer: physical fields first or legacy coefficient interpolation\n"
                             "Compositing: physical segment lengths, linear-light display transfer\n"
                             "Renderer fallback: none\nGradient fallback: zero slope (reported)\n")

    def upload_transfer(self, fields: np.ndarray, palette: np.ndarray, transfer=None):
        fields = np.ascontiguousarray(fields, dtype=np.float32)
        palette = np.ascontiguousarray(palette, dtype=np.float32)
        if fields.shape != (len(self.positions), 2) or palette.shape != (512, 4):
            raise ValueError("Native volume transfer buffer shape mismatch")
        if not np.all(np.isfinite(fields[:, 1])) or np.any(fields[:, 1] < 0):
            raise ValueError("Native volume extinction must be finite and nonnegative")
        transfer = np.zeros(12, np.float32) if transfer is None else np.ascontiguousarray(transfer, dtype=np.float32)
        if transfer.shape != (12,) or not np.all(np.isfinite(transfer)):
            raise ValueError("Native volume transfer parameters must be twelve finite values")
        if transfer[8] and not (transfer[1] > transfer[0] and transfer[2] > 0 and
                                transfer[4] >= 0 and transfer[5] >= 0 and transfer[6] >= 0 and transfer[7] >= 0):
            raise ValueError("Invalid field-first transfer domain")
        finite_color = fields[np.isfinite(fields[:, 0]), 0]
        bounds = [finite_color.min() if finite_color.size else 0,
                  finite_color.max() if finite_color.size else 0,
                  fields[:, 1].min(), fields[:, 1].max()]
        transfer = np.concatenate((transfer, np.asarray(bounds, dtype=np.float32)))
        if self.library.av_fields(self.handle, fields.ctypes.data, palette.ctypes.data,
                                  self.owner.header["position_unit_cm"]/self.owner.meta["display_radius_cm"],
                                  transfer.ctypes.data,
                                  self.error, len(self.error)):
            raise ValueError(self.error.value.decode())
        self.gradient_seconds = self.library.av_gradient_seconds(self.handle)
        self.gradient_fallbacks = self.library.av_gradient_fallbacks(self.handle)

    def sample_fields(self, points: np.ndarray, reconstruction="continuous", *, apply_transfer=False) -> np.ndarray:
        """Sample uploaded fields at normalized scene coordinates, optionally mapping them to colour/extinction."""
        if reconstruction not in RECONSTRUCTIONS:
            raise ValueError("Invalid volume reconstruction")
        points = np.asarray(points, dtype=np.float32)
        if points.ndim != 2 or points.shape[1] != 3 or not 0 < len(points) <= 1000000:
            raise ValueError("Expected between 1 and 1000000 three-dimensional sample points")
        if not np.all(np.isfinite(points)):
            raise ValueError("Native field sample points must be finite")
        queries = np.zeros((len(points), 4), np.float32)
        queries[:, :3] = points
        result = np.empty((len(points), 2), np.float32)
        if self.library.av_sample_fields(self.handle, queries.ctypes.data, len(points),
                     self.box_cm/self.owner.meta["display_radius_cm"],
                     RECONSTRUCTIONS[reconstruction], int(apply_transfer), result.ctypes.data,
                     self.error, len(self.error)):
            raise ValueError(self.error.value.decode())
        if not np.all(np.isfinite(result[:, 1])) or np.any(result[:, 1] < 0):
            raise ValueError("Native field reconstruction failed")
        return result

    def trace(self, camera: dict, width: int, height: int, subpixels: int,
              reconstruction: str = "piecewise_constant", cell_samples: int = 1):
        if width <= 0 or height <= 0 or width > 1920 or height > 1200 or subpixels not in (1, 4):
            raise ValueError("Volume viewport must be at most 1920 by 1200 with 1 or 4 rays per pixel")
        if reconstruction not in RECONSTRUCTIONS or cell_samples not in (1, 2):
            raise ValueError("Invalid volume reconstruction or cell sampling")
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
        if self.library.av_render(self.handle, width, height, subpixels, 8192,
                                  RECONSTRUCTIONS[reconstruction], cell_samples, uniform.ctypes.data,
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
                  "subpixel_samples": subpixels, "reconstruction": reconstruction,
                  "cell_samples": cell_samples if reconstruction != "piecewise_constant" else 1,
                  "interpolation": {"continuous": "compact_shepard_8", "linear": "limited_least_squares", "piecewise_constant": "none", "continuous_linear": "blended_limited_gradients_8"}[reconstruction],
                  "gradient_setup_gpu_seconds": self.gradient_seconds,
                  "gradient_fallback_cells": self.gradient_fallbacks}
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
        transfer_options = {key: value for key, value in options.items() if key != "reconstruction"}
        key = json.dumps([style, params.get("derived_channels", []), floor, transfer_options], sort_keys=True)
        if key != self.transfer_key:
            owner.progress("Updating field colours and density transparency")
            values = owner.values(style["channel"], params.get("derived_channels", []))
            scalar = transform(values, style)
            visible = np.isfinite(scalar)
            if options["range_behavior"] == "hide":
                visible &= (values >= low) & (values <= high)
            fields = np.zeros((len(self.positions), 2), dtype=np.float32)
            fields[:, 0] = np.nan
            coefficient = extinction(owner.channels["density"], visible, floor,
                                      opacity, owner.meta["display_radius_cm"], options)
            self.visible_indices = owner.selected[coefficient > 0]
            field_transfer = None
            if options["transfer_stage"] == "after_reconstruction":
                channel_scale = max(abs(low), abs(high), float(style.get("linthresh", 1)), 1e-30)
                scaled_values = np.asarray(values, dtype=np.float64) / channel_scale
                finite = np.isfinite(scaled_values)
                if np.any(np.abs(scaled_values[finite]) > np.finfo(np.float32).max):
                    raise ValueError("Physical field exceeds the float32 reconstruction range")
                fields[owner.selected, 0] = np.where(finite, scaled_values, np.nan)
                fields[owner.selected, 1] = owner.channels["density"] / options["density_reference"]
                field_transfer = np.array([
                    low/channel_scale, high/channel_scale, float(style.get("linthresh", 1))/channel_scale,
                    {"linear": 0, "log10": 1, "symlog": 2}[style.get("scale_mode", "linear")],
                    floor/options["density_reference"], options["floor_softening_dex"], options["density_power"],
                    -math.log(max(1-opacity, 1e-3))*owner.meta["display_radius_cm"]/options["opacity_length_cm"],
                    1, options["range_behavior"] == "hide",
                    options["dense_fade_start"]/options["density_reference"],
                    options["dense_opacity_fraction"]], dtype=np.float32)
            else:
                fields[owner.selected, 0] = (scalar-bounds[0]) / (bounds[1]-bounds[0])
                fields[owner.selected, 1] = coefficient
            colors = transfer_colors(style).astype(np.float32) / 255
            colors[:, :3] = np.where(colors[:, :3] <= .04045, colors[:, :3] / 12.92,
                                     ((colors[:, :3] + .055) / 1.055) ** 2.4)
            self.upload_transfer(fields, colors, field_transfer)
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
        subpixels = int(params.get("subpixel_samples", 4))
        pixels, stats = self.trace(owner.camera, width, height, subpixels,
                                  options["reconstruction"],
                                  int(params.get("cell_samples", 2 if subpixels == 4 else 1)))
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
                  "backend": "native_metal_voronoi_volume_v004", "device": self.device,
                  "representation": "volume", "density_floor": floor, "volume": options,
                  "reconstruction_fields": (["physical_channel_scaled", "gas_density_scaled"]
                                             if options["transfer_stage"] == "after_reconstruction"
                                             else ["transformed_colour_scalar", "display_extinction"]),
                  "ruler_kind": "projected",
                  "early_termination_transmittance": .001, "camera": owner.camera, "style": style}
        return output.getvalue(), report

    def close(self):
        if self.handle:
            self.library.av_close(self.handle)
            self.handle = None
