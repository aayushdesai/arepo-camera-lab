"""Native VTK/PyVista explorer for portable AREPO full-cell scenes.

This backend renders sampled cell centers and physical scalars with native VTK.
It is an interactive exploration backend, not the exact ArepoRT Voronoi
ray-traversal renderer used for scientific movie frames.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any

import numpy as np

from . import cleanup as session_cleanup
from . import viewer
from .transfer import acquire_verified_file


PALETTE_STOPS = {
    "copper_blue": ["#07101c", "#29618c", "#b8561f", "#ffe09e"],
    "viridis": "viridis",
    "plasma": "plasma",
    "magma": "magma",
    "inferno": "inferno",
    "turbo": "turbo",
    "blue_red": ["#1f6be0", "#d1d9d6", "#e04c1a"],
    "grayscale": "gray",
}
SCALE_MODES = ("linear", "log10", "symlog")


def _pyvista():
    try:
        import pyvista as pv
    except ImportError as error:
        raise ValueError(
            "native VTK support is not installed; update the Conda environment "
            "with: conda env update -f environment.yml --prune") from error
    return pv


def _decode_float32(encoded: str, shape: tuple[int, ...]) -> np.ndarray:
    values = np.frombuffer(base64.b64decode(encoded), dtype="<f4")
    if values.size != int(np.prod(shape)):
        raise ValueError("decoded scene array has the wrong size")
    return np.asarray(values.reshape(shape), dtype=np.float64)


def cache_scene_file(scene: Path, expected_sha256: str | None,
                     directory: Path) -> tuple[Path, str]:
    """Copy one requested scene into a content-addressed local cache."""
    scene = scene.expanduser().resolve()
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    expected = expected_sha256.lower() if expected_sha256 else None
    if expected is not None and (
            len(expected) != 64 or any(character not in "0123456789abcdef"
                                       for character in expected)):
        raise ValueError("scene SHA-256 must be 64 hexadecimal characters")
    if expected is not None:
        cached = directory / f"{scene.stem}_{expected[:16]}{scene.suffix}"
        if cached.is_file():
            actual = viewer.sha256(cached)
            if actual != expected:
                raise ValueError(f"cached scene digest mismatch: {cached}")
            print(f"[cache] Reusing {cached}", flush=True)
            return cached, expected

    temporary = directory / f".{scene.name}.{os.getpid()}.partial"
    if temporary.exists():
        raise FileExistsError(f"refusing to overwrite cache staging file {temporary}")
    import hashlib
    digest = hashlib.sha256()
    copied = 0
    total = scene.stat().st_size
    print(f"[cache] Copying requested scene ({total / 1024**2:.1f} MiB)", flush=True)
    try:
        with scene.open("rb") as source, temporary.open("xb") as target:
            while True:
                chunk = source.read(16 * 1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
                print(f"[cache] {100.0 * copied / total:5.1f}%", flush=True)
        actual = digest.hexdigest()
        if expected is not None and actual != expected:
            raise ValueError(
                f"source scene digest {actual} does not match expected {expected}")
        cached = directory / f"{scene.stem}_{actual[:16]}{scene.suffix}"
        if cached.exists():
            if viewer.sha256(cached) != actual:
                raise ValueError(f"existing cached scene digest mismatch: {cached}")
            temporary.unlink()
        else:
            shutil.move(str(temporary), str(cached))
        print(f"[cache] Ready: {cached}", flush=True)
        return cached, actual
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def rsync_scene_file(source: str, expected_sha256: str,
                     directory: Path) -> tuple[Path, str]:
    """Fetch one scene with resumable rsync and verify its trusted digest."""
    try:
        return acquire_verified_file(source, expected_sha256, directory)
    except ValueError as error:
        if "SHA-256" in str(error):
            raise ValueError(
                "--rsync-scene requires a 64-character --scene-sha256") from error
        raise


def _colormap(name: str):
    definition = PALETTE_STOPS[name]
    if isinstance(definition, str):
        return definition
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list(f"arepo_{name}", definition)


def transform_values(values: np.ndarray, mode: str, linthresh: float) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if mode == "linear":
        return values.copy()
    if mode == "log10":
        result = np.full(values.shape, np.nan, dtype=np.float64)
        positive = values > 0.0
        result[positive] = np.log10(values[positive])
        return result
    if mode == "symlog":
        threshold = max(float(linthresh), 1.0e-300)
        return np.sign(values) * np.log10(1.0 + np.abs(values) / threshold)
    raise ValueError(f"unknown scale mode: {mode}")


@dataclass
class NativeScene:
    scene_path: Path
    scene_sha256: str
    snapshot: int | None
    points: np.ndarray
    channels: dict[str, np.ndarray]
    channel_metadata: dict[str, dict[str, Any]]
    center_cm: np.ndarray
    display_radius_cm: float
    initial_camera: dict[str, Any]
    magnetic_vectors: np.ndarray | None = None
    auxiliary_sha256: str | None = None


def load_native_scene(scene: Path, *, snapshot: int | None,
                      max_points: int, scene_sha256: str | None = None,
                      field_sidecar: Path | None = None) -> NativeScene:
    scene = scene.expanduser().resolve()
    if not scene.is_file():
        raise ValueError(f"scene does not exist: {scene}")
    print("[load] Reading portable-scene header", flush=True)
    header = viewer.read_header(scene)
    print("[load] Mapping full-cell records", flush=True)
    cells = viewer.read_cells(scene, header)
    print("[load] Inferring center and rotation axis", flush=True)
    center, axis = viewer.infer_center_axis(cells, header, None, None)
    point_budget = int(header["num_cells"]) if max_points == 0 else max_points
    print(f"[load] Selecting {point_budget:,} deterministic cell centers", flush=True)
    selected = viewer.sample_cells(cells, point_budget)
    digest = scene_sha256.lower() if scene_sha256 else viewer.sha256(scene)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("scene SHA-256 must be 64 hexadecimal characters")

    print("[load] Reading and joining auxiliary physical fields", flush=True)
    sidecar = viewer.read_field_sidecar(field_sidecar) if field_sidecar else None
    print("[load] Deriving physical channels", flush=True)
    payload = viewer.build_payload(
        scene, header, cells, selected, center, axis, None, digest, snapshot,
        None, sidecar)
    count = int(payload["point_count"])
    points = _decode_float32(payload["positions"], (count, 3))
    channels: dict[str, np.ndarray] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for name, channel in payload["channels"].items():
        channels[name] = _decode_float32(channel["values"], (count,))
        metadata[name] = {key: value for key, value in channel.items()
                          if key != "values"}
    print(f"[load] Ready: {count:,} cells, {len(channels)} channels", flush=True)

    magnetic_vectors = None
    auxiliary_sha256 = None
    if sidecar is not None:
        auxiliary_sha256 = sidecar["sha256"]
        auxiliary = viewer.align_field_sidecar(
            sidecar, np.asarray(cells["particle_id"][selected]))
        if "magnetic_field_gauss" in auxiliary:
            magnetic_vectors = np.asarray(
                auxiliary["magnetic_field_gauss"], dtype=np.float64)

    return NativeScene(
        scene_path=scene,
        scene_sha256=digest,
        snapshot=snapshot,
        points=points,
        channels=channels,
        channel_metadata=metadata,
        center_cm=np.asarray(payload["scene"]["center_cm"], dtype=np.float64),
        display_radius_cm=float(payload["scene"]["display_radius_cm"]),
        initial_camera=payload["initial_camera"],
        magnetic_vectors=magnetic_vectors,
        auxiliary_sha256=auxiliary_sha256,
    )


def camera_pose(scene: NativeScene, camera: Any) -> dict[str, Any]:
    position = np.asarray(camera.position, dtype=np.float64)
    focal = np.asarray(camera.focal_point, dtype=np.float64)
    direction = focal - position
    norm = float(np.linalg.norm(direction))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("VTK camera position and focal point coincide")
    return {
        "snapshot": scene.snapshot,
        "position_cm": (scene.center_cm + position * scene.display_radius_cm).tolist(),
        "look_at_cm": (scene.center_cm + focal * scene.display_radius_cm).tolist(),
        "view_direction": (direction / norm).tolist(),
        "up": np.asarray(camera.up, dtype=np.float64).tolist(),
        "screen_half_extent_cm": float(camera.parallel_scale) * scene.display_radius_cm,
        "scene_sha256": scene.scene_sha256,
        "scene_path": str(scene.scene_path),
        "backend": "native_vtk_point_explorer_v001",
    }


def write_pose(scene: NativeScene, camera: Any, directory: Path) -> Path:
    if scene.snapshot is None:
        raise ValueError("--snapshot is required before saving a camera pose")
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, 10000):
        path = directory / f"camera_pose_snapshot_{scene.snapshot:04d}_{index:03d}.json"
        if path.exists():
            continue
        payload = {
            "schema": "stellar_camera_keyframes_v001",
            "keyframes": [camera_pose(scene, camera)],
        }
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.write("\n")
        return path
    raise FileExistsError("camera-pose numbering is exhausted")


@dataclass
class NativeExplorer:
    scene: NativeScene
    plotter: Any
    channel_name: str
    palette_name: str
    scale_mode: str
    point_size: float
    opacity: float
    low_percentile: float
    high_percentile: float
    clip_outside: bool
    poses_directory: Path
    glyph_count: int
    glyph_factor: float
    glyphs_visible: bool = False
    channels: list[str] = field(init=False)
    palettes: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.channels = list(self.scene.channels)
        self.palettes = list(PALETTE_STOPS)
        if self.channel_name not in self.channels:
            raise ValueError(
                f"unknown channel {self.channel_name}; choose from {', '.join(self.channels)}")
        if self.palette_name not in self.palettes:
            raise ValueError(
                f"unknown palette {self.palette_name}; choose from {', '.join(self.palettes)}")
        if self.scale_mode not in SCALE_MODES:
            raise ValueError(f"unknown scale mode {self.scale_mode}")
        if not 0.0 <= self.low_percentile < self.high_percentile <= 100.0:
            raise ValueError("percentiles must satisfy 0 <= low < high <= 100")

    def _display_values(self) -> tuple[np.ndarray, tuple[float, float]]:
        raw = self.scene.channels[self.channel_name]
        metadata = self.scene.channel_metadata[self.channel_name]
        transformed = transform_values(raw, self.scale_mode, metadata["linthresh"])
        finite = transformed[np.isfinite(transformed)]
        if finite.size == 0:
            raise ValueError(
                f"channel {self.channel_name} has no finite values in {self.scale_mode} mode")
        low, high = np.percentile(
            finite, [self.low_percentile, self.high_percentile])
        if not high > low:
            high = low + max(abs(low) * 1.0e-6, 1.0e-30)
        if self.clip_outside:
            transformed = transformed.copy()
            transformed[(transformed < low) | (transformed > high)] = np.nan
        return transformed, (float(low), float(high))

    def _status(self, clim: tuple[float, float]) -> str:
        snapshot = "unknown" if self.scene.snapshot is None else str(self.scene.snapshot)
        return (
            f"AREPO snapshot index {snapshot}\n"
            f"{self.channel_name} | {self.scale_mode} | {self.palette_name}\n"
            f"percentiles {self.low_percentile:.0f}-{self.high_percentile:.0f} | "
            f"clip {'on' if self.clip_outside else 'off'}\n"
            f"display range {clim[0]:.4g} .. {clim[1]:.4g}")

    def redraw(self) -> None:
        values, clim = self._display_values()
        cloud = _pyvista().PolyData(self.scene.points)
        cloud["display_scalar"] = values
        self.plotter.remove_actor("cell-cloud", render=False)
        try:
            self.plotter.remove_scalar_bar(render=False)
        except (KeyError, ValueError, StopIteration):
            pass
        label = self.scene.channel_metadata[self.channel_name]["label"]
        units = self.scene.channel_metadata[self.channel_name]["units"]
        title = f"{label} [{units}] ({self.scale_mode})"
        self.plotter.add_points(
            cloud, scalars="display_scalar", name="cell-cloud",
            cmap=_colormap(self.palette_name), clim=clim,
            point_size=self.point_size, opacity=self.opacity,
            render_points_as_spheres=True, nan_opacity=0.0,
            scalar_bar_args={
                "title": title, "vertical": True, "color": "white",
                "title_font_size": 12, "label_font_size": 10,
                "position_x": 0.88, "position_y": 0.18,
                "width": 0.07, "height": 0.62})
        self.plotter.add_text(
            self._status(clim), name="arepo-status", position="upper_left",
            font_size=10, color="white")
        snapshot = "UNKNOWN" if self.scene.snapshot is None else str(self.scene.snapshot)
        self.plotter.add_text(
            f"VISIBLE CELLS: AREPO SNAPSHOT {snapshot}",
            name="visible-snapshot", position="lower_right",
            font_size=10, color="white")
        self.plotter.render()

    def cycle_channel(self, step: int) -> None:
        index = (self.channels.index(self.channel_name) + step) % len(self.channels)
        self.channel_name = self.channels[index]
        metadata = self.scene.channel_metadata[self.channel_name]
        self.scale_mode = str(metadata["default_scale"])
        self.palette_name = str(metadata["default_palette"])
        self.redraw()

    def cycle_scale(self) -> None:
        index = (SCALE_MODES.index(self.scale_mode) + 1) % len(SCALE_MODES)
        self.scale_mode = SCALE_MODES[index]
        self.redraw()

    def cycle_palette(self) -> None:
        index = (self.palettes.index(self.palette_name) + 1) % len(self.palettes)
        self.palette_name = self.palettes[index]
        self.redraw()

    def set_percentiles(self, low: float, high: float) -> None:
        self.low_percentile = low
        self.high_percentile = high
        self.redraw()

    def toggle_clipping(self) -> None:
        self.clip_outside = not self.clip_outside
        self.redraw()

    def toggle_glyphs(self) -> None:
        if self.scene.magnetic_vectors is None:
            print("No magnetic vectors are loaded. Supply --field-sidecar.")
            return
        if self.glyphs_visible:
            self.plotter.remove_actor("magnetic-vectors")
            self.glyphs_visible = False
            return
        count = min(self.glyph_count, self.scene.points.shape[0])
        indices = np.linspace(0, self.scene.points.shape[0] - 1, count, dtype=np.int64)
        vectors = self.scene.magnetic_vectors[indices]
        magnitude = np.linalg.norm(vectors, axis=1)
        direction = np.divide(
            vectors, magnitude[:, None], out=np.zeros_like(vectors),
            where=magnitude[:, None] > 0.0)
        source = _pyvista().PolyData(self.scene.points[indices])
        source["magnetic_direction"] = direction
        source["magnetic_strength"] = magnitude
        glyphs = source.glyph(
            orient="magnetic_direction", scale=False, factor=self.glyph_factor)
        self.plotter.add_mesh(
            glyphs, scalars="magnetic_strength", cmap="cool", name="magnetic-vectors",
            show_scalar_bar=False, opacity=0.82)
        self.glyphs_visible = True

    def save_pose(self) -> None:
        path = write_pose(self.scene, self.plotter.camera, self.poses_directory)
        print(f"Saved camera pose: {path}")
        self.plotter.add_text(
            f"Saved {path.name}", name="pose-saved", position="lower_left",
            font_size=10, color="white")

    def add_controls(self) -> None:
        self.plotter.add_key_event("bracketleft", lambda: self.cycle_channel(-1))
        self.plotter.add_key_event("bracketright", lambda: self.cycle_channel(1))
        self.plotter.add_key_event("l", self.cycle_scale)
        self.plotter.add_key_event("p", self.cycle_palette)
        self.plotter.add_key_event("c", self.toggle_clipping)
        self.plotter.add_key_event("b", self.toggle_glyphs)
        self.plotter.add_key_event("k", self.save_pose)
        self.plotter.add_key_event("1", lambda: self.set_percentiles(1.0, 99.0))
        self.plotter.add_key_event("2", lambda: self.set_percentiles(5.0, 95.0))
        self.plotter.add_key_event("3", lambda: self.set_percentiles(0.0, 100.0))
        help_text = (
            "[ ] channel   L scale   P palette   C clip\n"
            "1/2/3 range   B magnetic vectors   K save camera pose\n"
            "Mouse: orbit/pan/zoom   R reset camera")
        self.plotter.add_text(
            help_text, name="arepo-help", position="lower_left",
            font_size=9, color="#bdc9d2")


def create_explorer(scene: NativeScene, *, channel: str,
                    palette: str | None, scale: str | None,
                    point_size: float, opacity: float,
                    percentile_low: float, percentile_high: float,
                    clip_outside: bool, poses_directory: Path,
                    glyph_count: int, glyph_factor: float,
                    off_screen: bool = False) -> NativeExplorer:
    metadata = scene.channel_metadata.get(channel)
    if metadata is None:
        raise ValueError(f"unknown channel {channel}; choose from {', '.join(scene.channels)}")
    plotter = _pyvista().Plotter(
        off_screen=off_screen, window_size=(1440, 900),
        title="AREPO Camera Lab - native VTK")
    plotter.set_background("#070b0f")
    initial = scene.initial_camera
    target = np.asarray(initial["target"], dtype=float)
    forward = np.asarray(initial["forward"], dtype=float)
    position = target - 4.0 * float(initial["scale"]) * forward
    plotter.camera_position = [position, target, initial["up"]]
    plotter.enable_parallel_projection()
    plotter.camera.parallel_scale = float(initial["scale"])
    explorer = NativeExplorer(
        scene=scene,
        plotter=plotter,
        channel_name=channel,
        palette_name=palette or str(metadata["default_palette"]),
        scale_mode=scale or str(metadata["default_scale"]),
        point_size=point_size,
        opacity=opacity,
        low_percentile=percentile_low,
        high_percentile=percentile_high,
        clip_outside=clip_outside,
        poses_directory=poses_directory,
        glyph_count=glyph_count,
        glyph_factor=glyph_factor,
    )
    explorer.redraw()
    explorer.add_controls()
    return explorer


def add_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--scene", type=Path,
                        help="Portable scene already on a local filesystem")
    source.add_argument("--rsync-scene",
                        help="Remote rsync source, for example user@host:/path/scene_v052.bin")
    parser.add_argument("--snapshot", type=int,
                        help="AREPO output index, for example 721 for snapshot_0721.hdf5")
    parser.add_argument("--max-points", type=int, default=400_000,
                        help="Cell centers to display; zero loads every cell")
    parser.add_argument("--scene-sha256")
    field_source = parser.add_mutually_exclusive_group()
    field_source.add_argument("--field-sidecar", type=Path)
    field_source.add_argument(
        "--rsync-field-sidecar",
        help="Remote rsync source for the particle-ID-bound physical-field sidecar")
    parser.add_argument("--field-sidecar-sha256")
    parser.add_argument(
        "--cache-directory", type=Path,
        help="Content-addressed local cache; defaults under ~/.cache for rsync")
    parser.add_argument("--channel", default="rotational_fraction")
    parser.add_argument("--scale", choices=SCALE_MODES)
    parser.add_argument("--palette", choices=tuple(PALETTE_STOPS))
    parser.add_argument("--percentile-low", type=float, default=1.0)
    parser.add_argument("--percentile-high", type=float, default=99.0)
    parser.add_argument("--clip-outside", action="store_true")
    parser.add_argument("--point-size", type=float, default=3.0)
    parser.add_argument("--opacity", type=float, default=0.72)
    parser.add_argument("--poses-directory", type=Path, default=Path("camera-poses"))
    parser.add_argument("--glyph-count", type=int, default=2500)
    parser.add_argument("--glyph-factor", type=float, default=0.035)
    parser.add_argument("--show-magnetic-glyphs", action="store_true")
    parser.add_argument("--off-screen", action="store_true",
                        help="Build one frame without opening a desktop window")
    parser.add_argument("--screenshot", type=Path,
                        help="Write one PNG; implies off-screen mode")
    parser.add_argument("--cleanup-on-close", action="store_true",
                        help="Archive poses by rsync and remove verified scene/sidecar caches")
    parser.add_argument("--sync-back-destination",
                        help="Unique no-clobber host:/path for pose/session outputs")


def run(args: argparse.Namespace) -> int:
    if args.max_points != 0 and args.max_points < 1000:
        raise ValueError("--max-points must be zero or at least 1000")
    if not 0.0 < args.opacity <= 1.0:
        raise ValueError("--opacity must be in (0, 1]")
    if args.point_size <= 0.0 or args.glyph_count <= 0 or args.glyph_factor <= 0.0:
        raise ValueError("point size and magnetic-glyph controls must be positive")
    scene_path = args.scene
    scene_digest = args.scene_sha256
    if args.rsync_scene is not None:
        cache_directory = args.cache_directory or Path.home() / ".cache/arepo-camera-lab/scenes"
        if args.scene_sha256 is None:
            raise ValueError("--rsync-scene requires --scene-sha256")
        scene_path, scene_digest = rsync_scene_file(
            args.rsync_scene, args.scene_sha256, cache_directory)
    elif args.cache_directory is not None:
        scene_path, scene_digest = cache_scene_file(
            args.scene, args.scene_sha256, args.cache_directory)
    field_sidecar = args.field_sidecar
    if args.rsync_field_sidecar is not None:
        if args.field_sidecar_sha256 is None:
            raise ValueError(
                "--rsync-field-sidecar requires --field-sidecar-sha256")
        cache_directory = (
            scene_path.parent.parent / "fields" if args.rsync_scene is not None
            else Path.home() / ".cache/arepo-camera-lab/fields")
        field_sidecar, _ = acquire_verified_file(
            args.rsync_field_sidecar, args.field_sidecar_sha256,
            cache_directory)
    if args.cleanup_on_close:
        if args.rsync_scene is None or args.sync_back_destination is None:
            raise ValueError(
                "--cleanup-on-close requires --rsync-scene and "
                "--sync-back-destination")
        args.poses_directory.expanduser().resolve().mkdir(parents=True, exist_ok=True)
    scene = load_native_scene(
        scene_path, snapshot=args.snapshot, max_points=args.max_points,
        scene_sha256=scene_digest, field_sidecar=field_sidecar)
    explorer = create_explorer(
        scene, channel=args.channel, palette=args.palette, scale=args.scale,
        point_size=args.point_size, opacity=args.opacity,
        percentile_low=args.percentile_low, percentile_high=args.percentile_high,
        clip_outside=args.clip_outside, poses_directory=args.poses_directory,
        glyph_count=args.glyph_count, glyph_factor=args.glyph_factor,
        off_screen=args.off_screen or args.screenshot is not None)
    if args.show_magnetic_glyphs:
        explorer.toggle_glyphs()
    print(
        f"Loaded AREPO snapshot index {args.snapshot}: "
        f"{scene.points.shape[0]:,} / {viewer.read_header(scene_path)['num_cells']:,} cells")
    if scene.auxiliary_sha256:
        print(f"Auxiliary physical fields: {scene.auxiliary_sha256}")
    if args.screenshot is not None:
        path = args.screenshot.expanduser().resolve()
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        explorer.plotter.show(auto_close=True, screenshot=str(path))
        print(f"Wrote native VTK screenshot: {path}")
    elif args.off_screen:
        explorer.plotter.show(auto_close=True)
    else:
        explorer.plotter.show()
    if args.cleanup_on_close:
        cached_inputs = [session_cleanup.CachedInput(
            scene_path, args.rsync_scene, args.scene_sha256)]
        if args.rsync_field_sidecar is not None:
            cached_inputs.append(session_cleanup.CachedInput(
                field_sidecar, args.rsync_field_sidecar,
                args.field_sidecar_sha256))
        session_cleanup.archive_and_cleanup(
            args.poses_directory, args.sync_back_destination, cached_inputs)
    return 0
