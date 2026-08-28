from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from arepo_camera_lab import demo, server, spline, viewer


class CameraLabTest(unittest.TestCase):
    def make_scene(self, path: Path, count: int = 5000) -> None:
        header = viewer.HEADER_STRUCT.pack(
            viewer.SCENE_MAGIC.ljust(16, b"\0"), viewer.SCENE_VERSION,
            0x01020304, viewer.HEADER_BYTES, viewer.CELL_BYTES, 16, 72, 16,
            9, 16, 9, 8, viewer.REQUIRED_FLAGS, count, 0, 0, 0, 0,
            1.0e12, 1.0e12, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0,
            760.0, bytes(24))
        rng = np.random.default_rng(42)
        cells = np.zeros(count, dtype=viewer.CELL_DTYPE)
        angle = rng.uniform(0.0, 2.0 * np.pi, count)
        radius = rng.lognormal(np.log(7.0e10), 0.35, count)
        cells["position"][:, 0] = 5.0e11 + radius * np.cos(angle)
        cells["position"][:, 1] = 5.0e11 + radius * np.sin(angle)
        cells["position"][:, 2] = 5.0e11 + rng.normal(0.0, 7.0e9, count)
        cells["density"] = (10.0 - np.log10(radius / 1.0e10)).astype(np.float32)
        cells["temperature"] = rng.lognormal(np.log(2.0e6), 0.7, count)
        cells["velocity"][:, 0] = (-np.sin(angle) * 1.5e8).astype(np.float32)
        cells["velocity"][:, 1] = (np.cos(angle) * 1.5e8).astype(np.float32)
        cells["velocity"][:, 2] = rng.normal(0.0, 2.0e7, count)
        cells["particle_id"] = np.arange(count, dtype=np.uint64) + 100
        with path.open("xb") as handle:
            handle.write(header)
            handle.write(cells.tobytes())

    def test_state_load_and_deep_zoom_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scene = Path(temporary) / "scene.bin"
            self.make_scene(scene)
            state = server.ViewerState()
            digest = viewer.sha256(scene)
            status = state.load(scene, 721, 3000, digest)
            self.assertTrue(status["loaded"])
            self.assertEqual(status["point_count"], 3000)
            html = state.viewer_html()
            self.assertIn("Double-click: enter feature", html)
            self.assertIn("Math.max(1e-6", html)
            self.assertIn("screen half extent", html)
            self.assertIn("outward_mass_flux_proxy", html)
            self.assertIn("localStorage.setItem(KEYFRAME_STORAGE", html)
            self.assertIn("scene_sha256:DATA.scene.sha256", html)
            self.assertEqual(status["scene_sha256"], digest)
            with self.assertRaises(ValueError):
                state.load(scene, 721, 3000, "not-a-digest")

    def test_demo_scene_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scene = Path(temporary) / "demo.bin"
            demo.write_demo_scene(scene, 20_000)
            header = viewer.read_header(scene)
            self.assertEqual(header["num_cells"], 20_000)
            self.assertEqual(viewer.read_cells(scene, header).shape, (20_000,))
            with self.assertRaises(FileExistsError):
                demo.write_demo_scene(scene, 20_000)

    def test_all_cells_mode_and_async_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scene = Path(temporary) / "scene.bin"
            self.make_scene(scene, 5000)
            state = server.ViewerState()
            queued = state.start_load(scene, 31, 0, viewer.sha256(scene))
            self.assertTrue(queued["loading"])
            self.assertEqual(queued["requested_points"], 0)
            self.assertIsNotNone(state.worker)
            state.worker.join(timeout=10.0)
            self.assertFalse(state.worker.is_alive())
            ready = state.status()
            self.assertFalse(ready["loading"])
            self.assertEqual(ready["progress"], 1.0)
            self.assertEqual(ready["point_count"], 5000)
            self.assertEqual(ready["requested_points"], 0)
            self.assertIn('id="progress"', server.APP_HTML)

    def test_merge_key_pose_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            for index, snapshot in enumerate((31, 721)):
                payload = {"schema": spline.SCHEMA, "keyframes": [{
                    "snapshot": snapshot,
                    "look_at_cm": [float(index), 0.0, 0.0],
                    "view_direction": [0.0, 0.0, -1.0],
                    "up": [0.0, 1.0, 0.0],
                    "screen_half_extent_cm": float(index + 1),
                }]}
                path = root / f"pose_{snapshot}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                paths.append(path)
            merged = spline.read_keyframe_files(paths)
            self.assertEqual([row["snapshot"] for row in merged], [31, 721])


if __name__ == "__main__":
    unittest.main()
