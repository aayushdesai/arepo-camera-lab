"""Generate a small deterministic disk-and-outflow v052 demonstration scene."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import viewer


def write_demo_scene(path: Path, count: int = 250_000) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    if count < 10_000:
        raise ValueError("demo requires at least 10000 cells")
    rng = np.random.default_rng(20260828)
    disk_count = int(count * 0.72)
    outflow_count = count - disk_count
    cells = np.zeros(count, dtype=viewer.CELL_DTYPE)
    center = np.array([2.0e12, 2.0e12, 2.0e12])

    angle = rng.uniform(0.0, 2.0 * np.pi, disk_count)
    radius = rng.lognormal(np.log(2.4e11), 0.48, disk_count)
    height = rng.normal(0.0, 2.2e10 + 0.05 * radius)
    cells["position"][:disk_count] = center + np.column_stack((
        radius * np.cos(angle), radius * np.sin(angle), height))
    cells["density"][:disk_count] = (11.2 - 1.15 * np.log10(
        np.maximum(radius, 2.0e9) / 2.0e10)).astype(np.float32)
    cells["temperature"][:disk_count] = rng.lognormal(
        np.log(1.2e6), 0.55, disk_count).astype(np.float32)
    rotation = 2.0e8 * np.sqrt(1.2e11 / np.maximum(radius, 1.2e10))
    cells["velocity"][:disk_count, 0] = (-np.sin(angle) * rotation).astype(np.float32)
    cells["velocity"][:disk_count, 1] = (np.cos(angle) * rotation).astype(np.float32)
    cells["velocity"][:disk_count, 2] = rng.normal(0.0, 1.2e7, disk_count)

    sign = rng.choice(np.array([-1.0, 1.0]), outflow_count)
    z = sign * rng.lognormal(np.log(4.0e11), 0.62, outflow_count)
    cone = np.maximum(np.abs(z) * rng.uniform(0.025, 0.22, outflow_count), 8.0e9)
    phi = rng.uniform(0.0, 2.0 * np.pi, outflow_count)
    radial = cone * np.sqrt(rng.uniform(0.0, 1.0, outflow_count))
    tail = slice(disk_count, count)
    cells["position"][tail] = center + np.column_stack((
        radial * np.cos(phi), radial * np.sin(phi), z))
    cells["density"][tail] = rng.normal(7.8, 0.55, outflow_count).astype(np.float32)
    cells["temperature"][tail] = rng.lognormal(
        np.log(8.0e7), 0.65, outflow_count).astype(np.float32)
    outward = sign * rng.uniform(1.2e8, 4.8e8, outflow_count)
    cells["velocity"][tail, 0] = (0.10 * outward * np.cos(phi)).astype(np.float32)
    cells["velocity"][tail, 1] = (0.10 * outward * np.sin(phi)).astype(np.float32)
    cells["velocity"][tail, 2] = outward.astype(np.float32)
    cells["particle_id"] = np.arange(count, dtype=np.uint64) + 1

    header = viewer.HEADER_STRUCT.pack(
        viewer.SCENE_MAGIC.ljust(16, b"\0"), viewer.SCENE_VERSION, 0x01020304,
        viewer.HEADER_BYTES, viewer.CELL_BYTES, 16, 72, 16, 9, 16, 9, 8,
        viewer.REQUIRED_FLAGS, count, 0, 0, 0, 0, 4.0e12, 4.0e12,
        0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, bytes(24))
    with path.open("xb") as handle:
        handle.write(header)
        handle.write(cells.tobytes())

