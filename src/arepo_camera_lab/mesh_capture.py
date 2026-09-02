"""Reproducible native volume/face PNGs with physical time, scale, and field legends."""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np

from . import viewer
from .mesh_render import NativeMeshRenderer, transfer_colors


def scale_bar(half_cm: float, aspect: float, width: int, max_pixels: int = 140):
    if not all(math.isfinite(x) and x > 0 for x in (half_cm, aspect, width)):
        raise ValueError("Invalid physical viewport scale")
    cm_per_pixel = 2 * half_cm * aspect / width
    desired = cm_per_pixel * min(max_pixels, width * .28)
    power = 10 ** math.floor(math.log10(desired))
    factor = next(x for x in (5, 2, 1) if x * power <= desired)
    length = factor * power
    return {"length_cm": length, "pixels": length / cm_per_pixel,
            "cm_per_pixel": cm_per_pixel}


def annotate(png: bytes, report: dict, meta: dict, channel_meta: dict,
             title: str = "Native Voronoi cells", subtitle: str = "") -> tuple[bytes, dict]:
    from PIL import Image, ImageDraw, ImageFont
    picture = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = picture.size
    draw = ImageDraw.Draw(picture)
    def font(size):
        for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
        return ImageFont.load_default(size=size)
    text, muted = "#e5edf3", "#a7b9c8"
    draw.text((28, 22), title, font=font(27), fill=text)
    if subtitle:
        draw.text((29, 59), subtitle, font=font(15), fill=muted)
    bar = scale_bar(report["camera"]["scale"] * meta["display_radius_cm"],
                    width / height, width)
    x, y, panel_width = width - 337, height - 174, 309
    draw.rounded_rectangle((x, y, x + panel_width, height - 24), radius=8,
                           fill="#101920", outline="#364650")
    draw.text((x + 15, y + 11), f't = {report["snapshot_time_seconds"]:,.3f} s',
              font=font(20), fill=text)
    draw.text((x + 15, y + 38), f'Snapshot {report["snapshot"]}', font=font(13), fill=muted)
    unit = "km" if bar["length_cm"] >= 1e5 else "cm"
    length = bar["length_cm"] / (1e5 if unit == "km" else 1)
    bx, by = x + 15, y + 78
    rounded = float(f"{length:.4g}")
    length_text = f"{rounded:,.0f}" if rounded.is_integer() else f"{rounded:,g}"
    draw.text((bx, by - 20), f"{length_text} {unit}", font=font(14), fill=text)
    draw.line((bx, by, bx + bar["pixels"], by), fill=text, width=2)
    for end in (bx, bx + bar["pixels"]):
        draw.line((end, by - 4, end, by + 4), fill=text, width=2)
    style = report["style"]
    name = "rotational fraction" if style["channel"] == "rotational_fraction" else channel_meta["label"]
    label = f'{name} [{channel_meta["units"]}]'
    draw.text((bx, by + 13), label, font=font(13), fill=muted)
    lut = transfer_colors(style, panel_width - 30)[:, :3]
    gradient = Image.fromarray(np.repeat(lut[None, :, :], 8, axis=0))
    picture.paste(gradient, (bx, by + 35))
    draw.text((bx, by + 45), f'{style["low"]:.2e}', font=font(12), fill=text)
    draw.text((x + panel_width - 15, by + 45), f'{style["high"]:.2e}  ({style["scale_mode"]})',
              font=font(12), fill=text, anchor="ra")
    detail = (f'{report["rays"]:,} rays through native cells' if report.get("representation") == "volume"
              else f'{report["faces"]:,} native faces')
    draw.text((28, height - 42), f'{report["selected_cells"]:,} visible cells  |  {detail}',
              font=font(14), fill=muted)
    output = io.BytesIO()
    picture.save(output, format="PNG")
    return output.getvalue(), {"timestamp_seconds": report["snapshot_time_seconds"],
                               "snapshot": report["snapshot"], "scale_bar": bar,
                               "legend": {"label": label, "style": style}}


def fit_camera(points, camera: dict, aspect: float) -> dict:
    """Create an explicitly recorded diagnostic framing; never edit saved poses."""
    forward = np.asarray(camera["forward"], dtype=float)
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, camera["up"])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    basis = np.column_stack((right, up, forward))
    projected = np.asarray(points, dtype=float) @ basis
    lower, upper = projected.min(axis=0), projected.max(axis=0)
    center = basis @ ((lower + upper) / 2)
    extent = (upper - lower) / 2
    scale = float(max(extent[1], extent[0] / aspect) * 1.45)
    center -= .10 * scale * up  # Leave room for the time/scale legend below.
    return {"target": center.tolist(), "forward": forward.tolist(), "up": up.tolist(), "scale": scale}


def capture(config_path: Path, output: Path) -> dict:
    config = json.loads(config_path.read_text())
    scene = Path(config["scene_path"]).expanduser().resolve()
    actual = viewer.sha256(scene)
    if actual != config["scene_sha256"]:
        raise ValueError("Scene checksum does not match the capture configuration")
    fields = config.get("field_sidecar_path")
    if fields and viewer.sha256(Path(fields)) != config.get("field_sidecar_sha256"):
        raise ValueError("Field sidecar checksum does not match the capture configuration")
    output.mkdir(parents=True, exist_ok=False)
    (output / "capture_config.json").write_text(json.dumps(config, indent=2))
    renderer = NativeMeshRenderer(config, output, lambda message: print(message, flush=True))
    rows, capabilities = [], {}
    try:
        for entry in config["frames"]:
            name = entry["name"]
            if not name or Path(name).name != name or not all(c.isalnum() or c in "-_" for c in name):
                raise ValueError("Frame names must be simple filenames")
            params = dict(entry["parameters"])
            if "physical_camera" in entry:
                pose = entry["physical_camera"]
                params["camera"] = {
                    "target": ((np.asarray(pose["look_at_cm"]) - renderer.meta["center_cm"]) /
                               renderer.meta["display_radius_cm"]).tolist(),
                    "scale": pose["screen_half_extent_cm"] / renderer.meta["display_radius_cm"],
                    "forward": pose["view_direction"], "up": pose["up"],
                }
            diagnostic_fit = bool(entry.get("fit_visible", params.get("fit_visible", False)))
            params["fit_visible"] = diagnostic_fit
            png, report = renderer.render(params)
            params["camera"] = report["camera"]
            params["fit_visible"] = False  # The recorded camera now reproduces the fit exactly.
            capabilities[report["backend"]] = renderer.capabilities
            channel = renderer.channel_meta.get(report["style"]["channel"],
                                                 {"label": report["style"]["channel"], "units": "derived"})
            png, annotations = annotate(png, report, renderer.meta, channel,
                                         entry.get("title", "Native Voronoi cells"), entry.get("subtitle", ""))
            path = output / f"{name}.png"
            with path.open("xb") as handle:
                handle.write(png)
            report.update({"annotations": annotations, "scene_metadata": renderer.meta,
                           "capture_parameters": params, "diagnostic_fit": diagnostic_fit,
                           "png": path.name, "png_sha256": hashlib.sha256(png).hexdigest()})
            with (output / f"{name}.json").open("x") as handle:
                json.dump(report, handle, indent=2, allow_nan=False)
            rows.append({"png": path.name, "sha256": report["png_sha256"], "report": f"{name}.json"})
            print(json.dumps({"frame": name, "backend": report["backend"],
                              "faces": report.get("faces"), "gpu_seconds": report.get("gpu_seconds"),
                              "render_seconds": report["render_seconds"]}), flush=True)
        (output / "graphics_capabilities.txt").write_text("\n\n".join(value or "" for value in capabilities.values()))
        manifest = {"schema": "arepo_camera_lab_native_mesh_capture_v002", "frames": rows,
                    "scene_sha256": actual, "field_sidecar_sha256": config.get("field_sidecar_sha256"),
                    "source_sha256": {name: viewer.sha256(Path(__file__).with_name(name)) for name in
                                      ["mesh_capture.py", "mesh_render.py", "native_mesh.cpp", "viewer.py",
                                       "volume_render.py", "native_volume.mm", "native_volume.metal"]},
                    "native_builder_sha256": viewer.sha256(renderer.binary) if renderer.binary else None,
                    "metal_bridge_sha256": viewer.sha256(renderer.volume.binary) if renderer.volume else None}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return manifest
    finally:
        renderer.close()


def add_arguments(parser):
    parser.add_argument("--config", required=True, type=Path,
                        help="Hash-bound scene, fields, cameras, and display settings")
    parser.add_argument("--output-directory", required=True, type=Path,
                        help="New directory for annotated PNGs and provenance")


def run(args):
    capture(args.config.expanduser().resolve(), args.output_directory.expanduser().resolve())
    return 0
