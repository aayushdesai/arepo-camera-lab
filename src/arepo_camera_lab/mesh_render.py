"""Render native AREPO-VTK cells on the local graphics device.

The v052 mesh connectivity is retained for every cell. Display geometry is the
boundary of the cells passing the user's field range, or all their faces when
interior faces are enabled. Metal volume mode integrates inside these cells.
Both are display previews, not calibrated radiation-transport calculations.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable

import numpy as np

from . import viewer


def compile_builder(directory: Path) -> Path:
    source = Path(__file__).with_name("native_mesh.cpp")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    binary = directory / f"native_mesh_{digest}"
    if binary.is_file():
        return binary
    compiler = shutil.which("clang++") or shutil.which("c++")
    if compiler is None:
        raise ValueError("A native C++ compiler is needed to build the cell-face adapter")
    result = subprocess.run(
        [compiler, "-std=c++17", "-O3", "-pthread", str(source), "-o", str(binary)],
        capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise ValueError("Native face adapter failed to compile: " + result.stderr[-3000:])
    return binary


def read_geometry(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with path.open("rb") as handle:
        if handle.read(16).rstrip(b"\0") != b"ACLMESH0001":
            raise ValueError("Unknown native mesh buffer")
        counts = np.fromfile(handle, dtype="<u8", count=2)
        if counts.size != 2:
            raise ValueError("Truncated native mesh buffer")
        faces, vertices = (int(x) for x in counts)
        expected = 32 + vertices * 12 + (faces + 1) * 8 + faces * 4
        if path.stat().st_size != expected:
            raise ValueError("Native mesh buffer length does not match its counts")
        points = np.fromfile(handle, dtype="<f4", count=vertices * 3).reshape(-1, 3)
        offsets = np.fromfile(handle, dtype="<i8", count=faces + 1)
        owners = np.fromfile(handle, dtype="<u4", count=faces)
    if (offsets[0] != 0 or offsets[-1] != vertices or
            np.any(np.diff(offsets) < 3) or not np.all(np.isfinite(points))):
        raise ValueError("Invalid native polygons")
    return points, offsets, owners


def build_geometry(binary: Path, scene: Path, mask: np.ndarray, directory: Path,
                   center_cm, radius_cm: float, *, interior: bool = False,
                   threads: int = 8, max_vertices: int = 30_000_000):
    import tempfile
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="faces-", dir=directory) as work:
        folder = Path(work)
        np.asarray(mask, dtype=np.uint8).tofile(folder / "mask.bin")
        command = [str(binary), str(scene), str(folder / "mask.bin"),
                   str(folder / "mesh.bin"), *(repr(float(x)) for x in center_cm),
                   repr(float(radius_cm)), str(threads), str(int(interior)),
                   str(max_vertices)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise ValueError("Native mesh reconstruction failed: " + result.stderr.strip())
        stats = json.loads(result.stdout)
        geometry = read_geometry(folder / "mesh.bin")
    stats["construction_seconds"] = time.monotonic() - started
    return geometry, stats


def evaluate_formula(expression: str, resolve: Callable[[str], np.ndarray]):
    """Same small arithmetic language as the browser, without Python execution."""
    if len(expression) > 4096:
        raise ValueError("Formula is too long")
    tree = ast.parse(expression.replace("^", "**"), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 512:
        raise ValueError("Formula is too complex")
    functions = {"abs": (np.abs, 1), "sqrt": (np.sqrt, 1), "log10": (np.log10, 1),
                 "ln": (np.log, 1), "exp": (np.exp, 1), "min": (np.minimum, 2),
                 "max": (np.maximum, 2), "pow": (np.power, 2), "clip": (np.clip, 3)}
    binary = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
              ast.Div: np.divide, ast.Pow: np.power}
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if isinstance(node, ast.Name):
            return ({"pi": math.pi, "e": math.e}[node.id] if node.id in ("pi", "e")
                    else np.asarray(resolve(node.id), dtype=np.float64))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            return binary[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            function, count = functions[node.func.id]
            if node.keywords or len(node.args) != count:
                raise ValueError("Invalid formula function arguments")
            return function(*(walk(argument) for argument in node.args))
        raise ValueError("Unsupported formula syntax")
    with np.errstate(all="ignore"):
        return walk(tree)


def transfer_colors(style: dict, count: int = 512) -> np.ndarray:
    t = np.linspace(0.0, 1.0, count)
    if style.get("invert"):
        t = 1.0 - t
    t = t ** (1.0 / max(float(style.get("gamma", 1.0)), 0.01))
    name = style.get("palette", "copper_blue")
    if name == "copper_blue":
        stops = np.array([[.025, .055, .10], [.16, .38, .55], [.72, .34, .12], [1., .88, .62]])
        color = np.column_stack([np.interp(t, [0, .34, .72, 1], stops[:, k]) for k in range(3)])
    elif name == "blue_red":
        stops = np.array([[.12, .42, .88], [.82, .85, .84], [.88, .30, .10]])
        color = np.column_stack([np.interp(t, [0, .5, 1], stops[:, k]) for k in range(3)])
    else:
        # The same five-stop maps as the browser shader, not a different
        # implementation of the similarly named Matplotlib colour maps.
        maps = {
            "viridis": [[.267,.005,.329],[.230,.322,.546],[.128,.567,.551],[.369,.789,.383],[.993,.906,.144]],
            "plasma": [[.050,.030,.528],[.494,.012,.658],[.798,.280,.470],[.973,.586,.252],[.940,.975,.131]],
            "magma": [[.001,.000,.014],[.251,.038,.403],[.550,.161,.506],[.868,.288,.409],[.987,.991,.750]],
            "inferno": [[.002,.001,.014],[.258,.039,.406],[.578,.148,.404],[.865,.317,.226],[.988,.998,.645]],
            "turbo": [[.190,.072,.232],[.160,.733,.925],[.638,.991,.236],[.976,.588,.093],[.480,.016,.010]],
            "grayscale": [[0,0,0],[1,1,1]],
        }
        if name not in maps:
            raise ValueError("Unknown colour map: " + name)
        stops = np.asarray(maps[name])
        color = np.column_stack([np.interp(t, np.linspace(0, 1, len(stops)), stops[:, k]) for k in range(3)])
    luma = color @ np.array([.2126, .7152, .0722])
    color = np.clip((luma[:, None] + (color - luma[:, None]) * float(style.get("saturation", 1))) *
                    float(style.get("brightness", 1)), 0, 1)
    return np.column_stack((np.rint(color * 255).astype(np.uint8), np.full(count, 255, dtype=np.uint8)))


def transform(values, style: dict):
    mode = style.get("scale_mode", "linear")
    with np.errstate(all="ignore"):
        if mode == "log10":
            return np.where(np.asarray(values) > 0, np.log10(values), np.nan)
        if mode == "symlog":
            return np.sign(values) * np.log10(1 + np.abs(values) / max(float(style.get("linthresh", 1)), 1e-30))
        if mode != "linear":
            raise ValueError("Unknown scale mode")
        return np.asarray(values)


class NativeMeshRenderer:
    def __init__(self, config: dict, directory: Path, progress=lambda message: None):
        self.directory, self.progress = directory, progress
        self.scene = Path(config["scene_path"])
        self.header = viewer.read_header(self.scene)
        if not self.header["num_edges"] or self.header["invalid_neighbor_edges"]:
            raise ValueError("This scene has no complete native Voronoi connectivity")
        progress("Loading native cell records and physical fields")
        self.cells = viewer.read_cells(self.scene, self.header)
        self.selected = viewer.sample_cells(self.cells, int(self.header["num_cells"]))
        scene_meta = config.get("scene_meta")
        if scene_meta:
            center = np.asarray(scene_meta["center_cm"]) / self.header["position_unit_cm"]
            axis = np.asarray(scene_meta["axis"])
            radius = float(scene_meta["display_radius_cm"])
        else:
            center, axis = viewer.infer_center_axis(self.cells, self.header, None, None)
            radius = None
        sidecar = viewer.read_field_sidecar(Path(config["field_sidecar_path"])) if config.get("field_sidecar_path") else None
        payload = viewer.build_payload(self.scene, self.header, self.cells, self.selected,
                                       center, axis, radius, config["scene_sha256"],
                                       config["snapshot"], None, sidecar)
        self.meta = payload["scene"]
        self.initial_camera = payload["initial_camera"]
        self.channels = {name: np.frombuffer(base64.b64decode(item["values"]), dtype="<f4")
                         for name, item in payload["channels"].items()}
        self.channel_meta = {name: {k: v for k, v in item.items() if k != "values"}
                             for name, item in payload["channels"].items()}
        del payload, sidecar
        self.binary = self.plotter = self.volume = None
        self.active_representation = None
        self.face_capabilities = None
        self.mesh = None
        self.mask_key = None
        self.geometry_stats = {}
        self.capabilities = None
        self.camera = None
        self.actor = None
        self.actor_key = None

    def _prepare_faces(self):
        if self.plotter is not None:
            return
        import pyvista as pv
        self.pv = pv
        self.progress("Preparing the native VTK cell-face view")
        self.binary = compile_builder(self.directory)
        self.plotter = pv.Plotter(off_screen=True, window_size=(960, 600))
        self.plotter.set_background("#070b0f")
        self.plotter.ren_win.SetMultiSamples(0)
        self.plotter.enable_parallel_projection()
        self.plotter.enable_depth_peeling(number_of_peels=12, occlusion_ratio=0.0)

    def values(self, name: str, definitions: list[dict]):
        formulas = {item["name"]: item["expression"] for item in definitions}
        cache = dict(self.channels)
        active = set()
        def resolve(key):
            if key in cache:
                return cache[key]
            if key not in formulas or key in active:
                raise ValueError("Unknown or cyclic derived channel: " + key)
            active.add(key)
            value = evaluate_formula(formulas[key], resolve)
            value = np.broadcast_to(value, (self.selected.size,))
            valid = np.isfinite(value) & (np.abs(value) <= np.finfo(np.float32).max)
            value = np.where(valid, value, np.nan).astype(np.float32)
            active.remove(key)
            cache[key] = value
            return value
        return resolve(name)

    def set_camera(self, camera: dict):
        target = np.asarray(camera["target"], dtype=float)
        forward = np.asarray(camera["forward"], dtype=float)
        up = np.asarray(camera["up"], dtype=float)
        scale = float(camera["scale"])
        if any(a.shape != (3,) or not np.all(np.isfinite(a)) for a in (target, forward, up)) or not 0 < scale <= 100:
            raise ValueError("Invalid camera")
        length = np.linalg.norm(forward)
        if length < 1e-12:
            raise ValueError("Camera direction is zero")
        forward /= length
        right = np.cross(forward, up)
        if np.linalg.norm(right) < 1e-12:
            raise ValueError("Camera basis is degenerate")
        up = np.cross(right / np.linalg.norm(right), forward)
        # Keep the eye beyond the full scene at every zoom, avoiding near clipping
        # when the orthographic camera moves inside the white dwarf.
        distance = max(8.0, scale * 4.0)
        if self.plotter is not None:
            self.plotter.camera_position = [target - distance * forward, target, up]
            self.plotter.camera.parallel_scale = scale
            self.plotter.camera.clipping_range = (0.001, distance + 30.0)
        self.camera = {"target": target.tolist(), "forward": forward.tolist(), "up": up.tolist(), "scale": scale}

    def render(self, params: dict) -> tuple[bytes, dict]:
        representation = params.get("representation", "faces")
        if representation == "volume":
            from .volume_render import MetalVolume
            if self.volume is None:
                self.volume = MetalVolume(self)
            png, report = self.volume.render(params)
            self.active_representation = "volume"
            self.capabilities = self.volume.capabilities
            return png, report
        if representation != "faces":
            raise ValueError("Unknown native representation: " + str(representation))
        self._prepare_faces()
        from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
        from vtkmodules.vtkCommonDataModel import vtkCellArray
        started = time.monotonic()
        style = dict(params["style"])
        values = self.values(style["channel"], params.get("derived_channels", []))
        low, high = float(style["low"]), float(style["high"])
        if not (math.isfinite(low) and math.isfinite(high) and high > low):
            raise ValueError("Visible field limits must be finite and increasing")
        mask = np.zeros(int(self.header["num_cells"]), dtype=np.uint8)
        visible = np.isfinite(values) & (values >= low) & (values <= high)
        if style.get("scale_mode") == "log10":
            visible &= values > 0
        density_floor = float(params.get("density_floor", 0))
        if not math.isfinite(density_floor) or density_floor < 0:
            raise ValueError("Density floor must be finite and nonnegative")
        visible &= self.channels["density"] >= density_floor
        mask[self.selected[visible]] = 1
        interior = bool(params.get("interior_faces", False))
        key = hashlib.sha256(mask.tobytes() + bytes([interior])).hexdigest()
        if key != self.mask_key:
            self.progress("Building the visible native Voronoi faces")
            geometry, stats = build_geometry(self.binary, self.scene, mask, self.directory,
                                             self.meta["center_cm"], self.meta["display_radius_cm"],
                                             interior=interior)
            points, offsets, owners = geometry
            if not owners.size:
                raise ValueError("This field range exposes no cell faces. Narrow the range or enable interior faces.")
            mesh = self.pv.PolyData()
            mesh.points = points
            vtk_faces = vtkCellArray()
            vtk_faces.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                              numpy_to_vtkIdTypeArray(np.arange(len(points), dtype=np.int64), deep=True))
            mesh.SetPolys(vtk_faces)
            rows = np.searchsorted(self.selected, owners)
            if np.any(rows >= self.selected.size) or np.any(self.selected[rows] != owners):
                raise ValueError("Native face owners are not bound to physical-field rows")
            mesh.cell_data["native_cell_index"] = owners
            self.mesh, self.rows, self.owners = mesh, rows, owners
            self.mask_key, self.geometry_stats = key, stats
        bounds = transform(np.array([low, high]), style)
        if not np.all(np.isfinite(bounds)) or not bounds[1] > bounds[0]:
            raise ValueError("Field limits are invalid for the selected scale")
        opacity = float(style.get("opacity", .72))
        if not 0 < opacity <= 1:
            raise ValueError("Opacity must be in (0,1]")
        actor_key = json.dumps([key, style, params.get("derived_channels", []),
                                bool(params.get("edges")), bool(params.get("lighting", True))], sort_keys=True)
        if actor_key != self.actor_key:
            self.mesh.cell_data["display_scalar"] = transform(values[self.rows], style)
            lut = self.pv.LookupTable(n_values=512)
            lut.values = transfer_colors(style)
            lut.scalar_range = tuple(bounds)
            self.actor = self.plotter.add_mesh(self.mesh, name="native-cells", scalars="display_scalar",
                              preference="cell", cmap=lut, clim=tuple(bounds),
                              show_scalar_bar=False, show_edges=bool(params.get("edges", False)),
                              edge_color="#293743", line_width=.6,
                              opacity=opacity, smooth_shading=False,
                              lighting=bool(params.get("lighting", True)),
                              ambient=.38, diffuse=.60, specular=.08,
                              reset_camera=False, render=False)
            self.actor_key = actor_key
        width, height = int(params.get("width", 960)), int(params.get("height", 600))
        if width <= 0 or height <= 0:
            raise ValueError("Viewport dimensions must be positive")
        resize = min(1., 1920 / width, 1200 / height)
        width, height = max(1, round(width * resize)), max(1, round(height * resize))
        self.plotter.window_size = (width, height)
        camera = params["camera"]
        if params.get("fit_visible"):
            from .mesh_capture import fit_camera
            camera = fit_camera(self.mesh.points, camera, width / height)
        self.set_camera(camera)
        self.progress("Rendering native 3D cells on the graphics device")
        # screenshot() only initialises the first frame; later camera/style
        # requests must explicitly draw before reading back the framebuffer.
        self.plotter.render()
        picture = self.plotter.screenshot(return_img=True)
        if self.face_capabilities is None:
            self.face_capabilities = self.plotter.ren_win.ReportCapabilities()
        self.capabilities = self.face_capabilities
        self.active_representation = "faces"
        from PIL import Image
        image = Image.fromarray(picture)
        output = io.BytesIO()
        image.save(output, format="PNG")
        report = {**self.geometry_stats, "snapshot": self.meta["snapshot"],
                  "snapshot_time_seconds": self.meta["snapshot_time_seconds"],
                  "scene_sha256": self.meta["sha256"], "native_cell_count": int(self.header["num_cells"]),
                  "render_seconds": time.monotonic() - started, "width": width, "height": height,
                  "depth_peeling": bool(self.plotter.renderer.GetLastRenderingUsedDepthPeeling()),
                  "backend": "native_vtk_voronoi_faces_v001", "interior_faces": interior,
                  "representation": "faces", "ruler_kind": "3d",
                  "density_floor": density_floor,
                  "camera": self.camera, "style": style}
        return output.getvalue(), report

    def pick(self, params: dict) -> dict:
        if self.active_representation == "volume":
            raise ValueError("Volume rulers measure projected distance; use the face view for 3D surface picks")
        from vtkmodules.vtkRenderingCore import vtkCellPicker
        self.set_camera(params["camera"])
        if self.mesh is None:
            raise ValueError("Render the mesh before picking a point")
        width, height = self.plotter.window_size
        x, y = float(params["x"]), float(params["y"])
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("Pick coordinates must lie in the viewport")
        picker = vtkCellPicker()
        picker.SetTolerance(.001)
        hit = picker.Pick(x * (width-1), (1-y) * (height-1), 0, self.plotter.renderer)
        if not hit or picker.GetCellId() < 0:
            return {"hit": False}
        point = np.asarray(picker.GetPickPosition()) * self.meta["display_radius_cm"] + self.meta["center_cm"]
        owner = int(self.owners[picker.GetCellId()])
        return {"hit": True, "position_cm": point.tolist(), "native_cell_index": owner,
                "particle_id": str(int(self.cells["particle_id"][owner])),
                "snapshot": self.meta["snapshot"], "scene_sha256": self.meta["sha256"]}

    def close(self):
        if self.volume is not None:
            self.volume.close()
        if self.plotter is not None:
            self.plotter.close()
