from __future__ import annotations

import itertools
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

from arepo_camera_lab import mesh_render, server, viewer, volume_render


def lattice(path: Path, invalid=False, translation=0.0):
    """27 unit Voronoi cubes, including explicit periodic ghost neighbours."""
    n = 27
    header = viewer.HEADER_STRUCT.pack(
        viewer.SCENE_MAGIC.ljust(16, b"\0"), 5, 0x01020304, 208, 52, 16,
        72, 1, 1, 1, 1, 1, viewer.REQUIRED_FLAGS, n, n*6, 0, 0, 0,
        3., 10., 0., 0., 0., 2., 1., 1., 1., 155.0146484375, bytes(24))
    xyz = list(itertools.product(range(3), repeat=3))
    lookup = {p: i for i, p in enumerate(xyz)}
    cells = np.zeros(n, dtype=viewer.CELL_DTYPE)
    cells["position"] = np.array(xyz) + .5 + translation
    cells["density"], cells["temperature"] = 10, 100
    cells["particle_id"] = np.arange(n, dtype=np.uint64) + np.uint64(2**63+37)
    edges = np.zeros(n*6, dtype=[("delta", "<f4", (3,)), ("neighbor", "<u4")])
    for i, p in enumerate(xyz):
        for axis in range(3):
            for side, sign in enumerate((1, -1)):
                j = i*6+axis*2+side
                neighbor = list(p)
                neighbor[axis] += sign
                ghost = not 0 <= neighbor[axis] < 3
                neighbor[axis] %= 3
                edges["delta"][j, axis] = sign
                edges["neighbor"][j] = (lookup[tuple(neighbor)]+1) | (0x80000000 if ghost else 0)
    if invalid:
        edges["neighbor"][lookup[(1, 1, 1)]*6+4] = 0
    with path.open("xb") as handle:
        handle.write(header)
        handle.write(cells.tobytes())
        handle.write((np.arange(n+1, dtype="<u8")*6).tobytes())
        handle.write(edges.tobytes())
    return {"scene_path": str(path), "scene_sha256": viewer.sha256(path), "snapshot": 31,
            "scene_meta": {"center_cm": [2*(translation+1.5)]*3, "axis": [0, 0, 1],
                           "display_radius_cm": 2}}


class VolumeTransferTest(unittest.TestCase):
    def test_physical_path_normalization_and_soft_floor(self):
        options = volume_render.volume_options({"volume": {"density_reference": 100,
                                            "density_power": .5, "opacity_length_cm": 200}})
        rho = np.array([1, 10, 100, 1e4])
        coefficient = volume_render.extinction(rho, np.ones(4, dtype=bool), 10, .5, 20, options)
        self.assertEqual(coefficient[0], 0)
        self.assertEqual(coefficient[1], 0)
        self.assertAlmostEqual(coefficient[2]*200/20, math.log(2), places=6)
        other = volume_render.extinction(rho, np.ones(4, dtype=bool), 10, .5, 2, options)
        np.testing.assert_allclose(coefficient/20, other/2, rtol=1e-6)
        ramp = volume_render.extinction(np.array([10., math.sqrt(1000.), 100.]),
                                       np.ones(3, dtype=bool), 10, .5, 20, options)
        self.assertTrue(np.all(np.diff(ramp) > 0))

    def test_bad_transparency_options_and_nonfinite_fields(self):
        for key, value in (("density_reference", 0), ("density_power", -1),
                           ("opacity_length_cm", float("nan")), ("floor_softening_dex", 5)):
            with self.assertRaises(ValueError):
                volume_render.volume_options({"volume": {key: value}})
        options = volume_render.volume_options({})
        result = volume_render.extinction([float("nan"), 1.], np.array([False, True]), 0, .5, 2., options)
        self.assertEqual(result[0], 0)
        self.assertGreater(result[1], 0)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("AREPO_TEST_NATIVE_GRAPHICS") == "1",
                     "native Metal integration is opt-in on macOS")
class NativeMetalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.owner = mesh_render.NativeMeshRenderer(lattice(cls.root/"lattice.bin"), cls.root)
        cls.volume = volume_render.MetalVolume(cls.owner)
        cls.owner.volume = cls.volume

    @classmethod
    def tearDownClass(cls):
        cls.owner.close()
        cls.temporary.cleanup()

    def camera(self, direction=(0, 0, 1), scale=.1):
        self.owner.set_camera({"target": [0, 0, 0], "forward": list(direction), "up": [0, 1, 0], "scale": scale})
        return self.owner.camera

    def test_uniform_medium_matches_analytic_chords_and_is_zoom_independent(self):
        fields = np.tile([.5, .25], (27, 1)).astype(np.float32)
        color = np.array([.3, .5, .1])
        palette = np.tile([*color, 1], (512, 1)).astype(np.float32)
        self.volume.upload_transfer(fields, palette)
        background = np.array([.00212469, .00334654, .00477695])
        for direction, chord in (((0, 0, 1), 3), ((1, 0, 1), 3*math.sqrt(2)), ((0, 0, -1), 3)):
            image, report = self.volume.trace(self.camera(direction), 1, 1, 1)
            transmission = math.exp(-.25*chord)
            np.testing.assert_allclose(image[0, 0, :3], (1-transmission)*color+transmission*background, atol=2e-6)
            self.assertEqual(report["traversal_failures"], 0)
        zoomed, _ = self.volume.trace(self.camera(scale=.01), 1, 1, 1)
        np.testing.assert_allclose(zoomed, image, atol=2e-6)
        self.assertIsNone(self.owner.plotter)  # Volume preview needs no OpenGL window.

    def test_front_to_back_order_and_antialias_sample_counts(self):
        fields = np.empty((27, 2), np.float32)
        fields[:, 0] = (self.owner.cells["position"][:, 2]-.5)/2
        fields[:, 1] = .3
        palette = np.zeros((512, 4), np.float32)
        palette[:100, 0] = 1; palette[100:400, 1] = 1; palette[400:, 2] = 1
        self.volume.upload_transfer(fields, palette)
        transmission = math.exp(-.3)
        background = np.array([.00212469, .00334654, .00477695])
        expected = (1-transmission)*np.array([1., transmission, transmission**2]) + transmission**3*background
        image, report = self.volume.trace(self.camera(), 3, 3, 4)
        np.testing.assert_allclose(image[1, 1, :3], expected, atol=2e-6)
        self.assertEqual(report["rays"], 36)
        self.assertEqual(report["subpixel_samples"], 4)
        reverse, _ = self.volume.trace(self.camera((0, 0, -1)), 1, 1, 1)
        np.testing.assert_allclose(reverse[0, 0, :3],
                                   (1-transmission)*np.array([transmission**2, transmission, 1.]) + transmission**3*background,
                                   atol=2e-6)

    def test_renderer_keeps_geometry_resident_and_uses_existing_formula_fields(self):
        params = {"representation": "volume", "camera": self.camera(), "width": 48, "height": 32,
                  "density_floor": 0, "subpixel_samples": 1,
                  "derived_channels": [{"name": "twice_density", "expression": "density*2"}],
                  "style": {"channel": "twice_density", "scale_mode": "linear", "low": 1., "high": 3.,
                            "palette": "copper_blue", "opacity": .5},
                  "volume": {"density_reference": 1., "opacity_length_cm": 2.}}
        self.volume.transfer_key = None
        with mock.patch.object(self.volume, "upload_transfer", wraps=self.volume.upload_transfer) as upload:
            first, report = self.owner.render(params)
            self.assertTrue(first.startswith(b"\x89PNG"))
            self.assertEqual(report["selected_cells"], 27)
            self.assertEqual(report["snapshot_time_seconds"], 155.0146484375)
            self.assertEqual(report["scene_sha256"], self.owner.meta["sha256"])
            self.assertEqual(report["ruler_kind"], "projected")
            params["camera"] = self.camera((1, 0, 1))
            second, _ = self.owner.render(params)
            self.assertNotEqual(first, second)
            self.assertEqual(upload.call_count, 1)
            params["style"]["palette"] = "blue_red"
            self.owner.render(params)
            self.assertEqual(upload.call_count, 2)
        with self.assertRaisesRegex(ValueError, "projected"):
            self.owner.pick({"camera": params["camera"], "x": .5, "y": .5})

    def test_large_absolute_coordinate_offset_preserves_local_cell_geometry(self):
        owner = mesh_render.NativeMeshRenderer(lattice(self.root/"translated.bin", translation=5e11), self.root)
        volume = volume_render.MetalVolume(owner)
        try:
            fields = np.tile([0., .25], (27, 1)).astype(np.float32)
            palette = np.tile([.3, .5, .1, 1.], (512, 1)).astype(np.float32)
            volume.upload_transfer(fields, palette)
            image, stats = volume.trace(self.camera(), 1, 1, 1)
            expected = (1-math.exp(-.75))*palette[0, :3] + math.exp(-.75)*np.array([.00212469, .00334654, .00477695])
            np.testing.assert_allclose(image[0, 0, :3], expected, atol=2e-6)
            self.assertEqual(stats["traversal_failures"], 0)
        finally:
            volume.close(); owner.close()

    def test_invalid_native_neighbor_is_rejected_instead_of_returning_a_hole(self):
        owner = mesh_render.NativeMeshRenderer(lattice(self.root/"invalid.bin", invalid=True), self.root)
        volume = volume_render.MetalVolume(owner)
        try:
            volume.upload_transfer(np.zeros((27, 2), np.float32), np.ones((512, 4), np.float32))
            with self.assertRaisesRegex(ValueError, "traversal failed.*2"):
                volume.trace(self.camera(), 1, 1, 1)
        finally:
            volume.close(); owner.close()

    def test_worker_volume_protocol_and_local_quit(self):
        state = server.ViewerState()
        state.load(Path(self.owner.meta["path"]), 31, 0)
        params = {"representation": "volume", "scene_sha256": self.owner.meta["sha256"],
                  "camera": self.camera(), "width": 48, "height": 32, "subpixel_samples": 1,
                  "density_floor": 0, "style": {"channel": "density", "scale_mode": "linear",
                  "low": .5, "high": 1.5, "palette": "copper_blue", "opacity": .5}}
        try:
            result = state.mesh_request(params)
            self.assertEqual(result["report"]["backend"], "native_metal_voronoi_volume_v001")
            self.assertEqual(result["report"]["traversal_failures"], 0)
            process = state.mesh_bridge.process
            with mock.patch.object(server.session_cleanup, "archive_and_cleanup") as archive:
                state.start_quit()
                archive.assert_not_called()
            self.assertIsNotNone(process.poll())
        finally:
            state.close_mesh()


if __name__ == "__main__":
    unittest.main()
