from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

from arepo_camera_lab import mesh_capture, mesh_render, server, viewer


def adjacent_cubes(path: Path, periodic=False):
    header = viewer.HEADER_STRUCT.pack(
        viewer.SCENE_MAGIC.ljust(16, b"\0"), 5, 0x01020304, 208, 52, 16,
        72, 16, 9, 16, 9, 8, viewer.REQUIRED_FLAGS, 2, 12, 0, 0, 0,
        10., 10., 0., 0., 0., 2., 1., 1., 1., 155.0146484375, bytes(24))
    cells = np.zeros(2, dtype=viewer.CELL_DTYPE)
    cells["position"] = [[9.5, 0, 0], [.5, 0, 0]] if periodic else [[0, 0, 0], [1, 0, 0]]
    cells["density"] = [10, 11]  # v052 stores log10(rho_cgs) + 10.
    cells["temperature"] = [100, 200]
    cells["particle_id"] = [2**63 + 123, 2**63 + 456]
    edges = np.zeros(12, dtype=[("delta", "<f4", (3,)), ("neighbor", "<u4")])
    edges["delta"] = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0],
                      [0, 0, 1], [0, 0, -1]] * 2
    edges["neighbor"][0] = 2 | (0x80000000 if periodic else 0)
    edges["neighbor"][7] = 1 | (0x80000000 if periodic else 0)
    with path.open("xb") as handle:
        handle.write(header)
        handle.write(cells.tobytes())
        handle.write(np.array([0, 6, 12], dtype="<u8").tobytes())
        handle.write(edges.tobytes())


class NativeGeometryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (shutil.which("clang++") or shutil.which("c++")):
            raise unittest.SkipTest("native compiler unavailable")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.binary = mesh_render.compile_builder(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def build(self, path, mask, interior=False, **options):
        return mesh_render.build_geometry(self.binary, path, np.array(mask), self.root,
                                          [0, 0, 0], 2., interior=interior, **options)

    def test_closed_faces_winding_volume_and_visible_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cubes.bin"
            adjacent_cubes(path)
            (points, offsets, owners), stats = self.build(path, [1, 1])
            self.assertEqual(stats["faces"], 10)
            np.testing.assert_allclose(points.min(axis=0), [-.5, -.5, -.5])
            np.testing.assert_allclose(points.max(axis=0), [1.5, .5, .5])
            volume = 0
            for index, owner in enumerate(owners):
                polygon = points[offsets[index]:offsets[index + 1]]
                normal = np.cross(polygon[1] - polygon[0], polygon[2] - polygon[0])
                self.assertGreater(np.dot(normal, polygon.mean(axis=0) - [owner, 0, 0]), 0)
                for j in range(1, len(polygon) - 1):
                    volume += np.dot(polygon[0], np.cross(polygon[j], polygon[j + 1])) / 6
            self.assertAlmostEqual(float(volume), 2., places=6)
            (_, offsets, owners), stats = self.build(path, [1, 0])
            self.assertEqual(stats["faces"], 6)
            self.assertTrue(np.all(owners == 0))
            (_, offsets, owners), stats = self.build(path, [1, 1], interior=True)
            self.assertEqual(stats["faces"], 11)  # Shared face emitted once.

    def test_periodic_ghost_neighbor_uses_stored_plane_and_keeps_shared_face_internal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "periodic.bin"
            adjacent_cubes(path, periodic=True)
            (points, _, _), stats = self.build(path, [1, 1])
            self.assertEqual(stats["faces"], 10)
            np.testing.assert_allclose(points.min(axis=0), [-1, -.5, -.5])
            np.testing.assert_allclose(points.max(axis=0), [1, .5, .5])

    def test_bad_connectivity_and_vertex_budget_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cubes.bin"
            adjacent_cubes(path)
            with self.assertRaisesRegex(ValueError, "vertex budget"):
                self.build(path, [1, 1], max_vertices=8)
            with path.open("r+b") as handle:
                handle.truncate(path.stat().st_size - 1)
            with self.assertRaisesRegex(ValueError, "connectivity is truncated"):
                self.build(path, [1, 1])


class MeasurementAndStyleTest(unittest.TestCase):
    def test_physical_inspection_view_survives_snapshot_reference_changes(self):
        if not shutil.which("node"):
            self.skipTest("Node unavailable")
        subprocess.run(["node", str(Path(__file__).with_name("test_inspection_view.cjs"))], check=True)

    def test_snapshot_shell_carries_only_the_requested_inspection_view(self):
        if not shutil.which("node"):
            self.skipTest("Node unavailable")
        subprocess.run(["node", str(Path(__file__).with_name("test_snapshot_shell.cjs"))],
                       input=server.APP_HTML, text=True, check=True)

    def test_browser_controller_uses_current_frame_and_correct_ruler_kind(self):
        if not shutil.which("node"):
            self.skipTest("Node unavailable")
        subprocess.run(["node", str(Path(__file__).with_name("test_mesh_controls.cjs"))], check=True)

    def test_ruler_projection_and_zoom_device_pixel_independence(self):
        if not shutil.which("node"):
            self.skipTest("Node unavailable")
        module = Path(viewer.__file__).with_name("measurement_math.js")
        script = """
const assert=require('node:assert/strict'),M=require(process.argv[1]);
const camera={target:[0,0,0],right:[1,0,0],up:[0,1,0],forward:[0,0,-1],scale:2};
const p=M.pointOnPlane(.25,.75,camera,2,10,[100,200,300]);
assert.deepEqual(p,[80,190,300]);
assert.deepEqual(M.project(p,camera,2,10,[100,200,300],800,400),[200,300]);
assert.equal(M.distance([0,0,0],[3,4,12]),13);
assert.equal(M.scaleBar(20,2,800).cmPerPixel,.1);
assert.equal(M.scaleBar(10,2,800).cmPerPixel,.05);
assert.equal(M.scaleBar(20,2,1600).cmPerPixel,.05);
assert.equal(M.formatLength(1e9),'10,000 km');
console.log(JSON.stringify(M.scaleBar(20,2,800)));
"""
        result = subprocess.run(["node", "-e", script, str(module)], check=True,
                                capture_output=True, text=True)
        js = json.loads(result.stdout)
        py = mesh_capture.scale_bar(20, 2, 800)
        self.assertEqual(js["lengthCm"], py["length_cm"])
        self.assertEqual(js["pixels"], py["pixels"])

    def test_derived_fields_are_safe_and_share_existing_operators(self):
        fields = {"gas_pressure": np.array([8., 18., 0.]),
                  "magnetic_pressure": np.array([2., 3., 0.])}
        value = mesh_render.evaluate_formula("sqrt(gas_pressure / magnetic_pressure)^2", fields.__getitem__)
        np.testing.assert_allclose(value[:2], [4, 6])
        self.assertTrue(np.isnan(value[2]))
        np.testing.assert_allclose(mesh_render.evaluate_formula("clip(gas_pressure, 1, 10)", fields.__getitem__), [8, 10, 1])
        for expression in ("gas_pressure[0]", "__import__('os').system('x')", "(lambda: 1)()"):
            with self.assertRaises((ValueError, KeyError)):
                mesh_render.evaluate_formula(expression, fields.__getitem__)

    def test_log_symlog_and_color_map_direction(self):
        np.testing.assert_allclose(mesh_render.transform(np.array([-99, 0, 99]),
                                  {"scale_mode": "symlog", "linthresh": 1}), [-2, 0, 2])
        self.assertTrue(np.isnan(mesh_render.transform(np.array([-1]), {"scale_mode": "log10"})[0]))
        ordinary = mesh_render.transfer_colors({"palette": "copper_blue"})
        inverse = mesh_render.transfer_colors({"palette": "copper_blue", "invert": True})
        np.testing.assert_array_equal(ordinary[0], inverse[-1])
        np.testing.assert_array_equal(ordinary[-1], inverse[0])
        for palette in ("viridis", "plasma", "magma", "inferno", "turbo", "grayscale", "blue_red"):
            colors = mesh_render.transfer_colors({"palette": palette})
            self.assertEqual(colors.shape, (512, 4))

    def test_server_hash_guard_and_owned_renderer_teardown(self):
        state = server.ViewerState(payload={"scene": {"sha256": "a" * 64}})
        with self.assertRaisesRegex(ValueError, "does not match"):
            state.mesh_request({"scene_sha256": "b" * 64})
        bridge = mock.Mock()
        state.mesh_bridge = bridge
        state.close_mesh()
        state.close_mesh()
        bridge.close.assert_called_once()
        self.assertIsNone(state.mesh_bridge)


@unittest.skipUnless(os.environ.get("AREPO_TEST_NATIVE_GRAPHICS") == "1", "native graphics integration is opt-in")
class NativeGraphicsTest(unittest.TestCase):
    def test_server_native_worker_protocol_and_local_quit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scene = root / "cubes.bin"
            adjacent_cubes(scene)
            state = server.ViewerState()
            state.load(scene, 31, 3000)
            params = {"action": "render", "scene_sha256": viewer.sha256(scene),
                      "camera": {"target": [0, 0, 0], "forward": [0, 0, -1], "up": [0, 1, 0], "scale": 1.2},
                      "style": {"channel": "density", "scale_mode": "log10", "low": .5, "high": 20.,
                                "palette": "copper_blue", "opacity": 1}, "width": 640, "height": 480}
            try:
                result = state.mesh_request(params)
                self.assertEqual(result["kind"], "result")
                self.assertTrue(result["png"].startswith("iVBOR"))
                self.assertEqual(result["report"]["snapshot"], 31)
                self.assertEqual(result["report"]["faces"], 10)
                process = state.mesh_bridge.process
                self.assertIsNone(process.poll())
                with mock.patch.object(server.session_cleanup, "archive_and_cleanup") as archive:
                    state.start_quit()
                    archive.assert_not_called()
                self.assertIsNone(state.mesh_bridge)
                self.assertIsNotNone(process.poll())
            finally:
                state.close_mesh()

    def test_renderer_redraw_field_binding_pick_and_geometry_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scene = root / "cubes.bin"
            adjacent_cubes(scene)
            renderer = mesh_render.NativeMeshRenderer({"scene_path": str(scene), "scene_sha256": viewer.sha256(scene),
                "snapshot": 31, "scene_meta": {"center_cm": [0, 0, 0], "axis": [0, 0, 1], "display_radius_cm": 2}}, root)
            params = {"camera": {"target": [.5, 0, 0], "forward": [0, 0, -1], "up": [0, 1, 0], "scale": 1.2},
                      "style": {"channel": "density", "scale_mode": "log10", "low": .5, "high": 20.,
                                "palette": "copper_blue", "opacity": 1}, "width": 640, "height": 480}
            try:
                first, report = renderer.render(params)
                geometry = renderer.mesh
                native_values = renderer.channels["density"][renderer.rows]
                np.testing.assert_allclose(native_values, np.where(renderer.owners == 0, 1., 10.))
                for x, owner in ((.35, 0), (.65, 1)):
                    pick = renderer.pick({"camera": params["camera"], "x": x, "y": .5})
                    self.assertTrue(pick["hit"])
                    self.assertEqual(pick["native_cell_index"], owner)
                    self.assertEqual(pick["particle_id"], str(2**63 + (123 if owner == 0 else 456)))
                    self.assertAlmostEqual(pick["position_cm"][2], 1., places=5)
                params["camera"]["target"] = [0, 0, 0]
                second, _ = renderer.render(params)
                self.assertIs(renderer.mesh, geometry)
                self.assertNotEqual(first, second)  # Detect stale screenshots.
                params["style"]["palette"] = "plasma"
                third, _ = renderer.render(params)
                self.assertIs(renderer.mesh, geometry)
                self.assertNotEqual(second, third)
                self.assertEqual(report["snapshot_time_seconds"], 155.0146484375)
                params["style"]["opacity"] = .5
                transparent, transparent_report = renderer.render(params)
                self.assertNotEqual(third, transparent)
                self.assertTrue(transparent_report["depth_peeling"])
            finally:
                renderer.close()


if __name__ == "__main__":
    unittest.main()
