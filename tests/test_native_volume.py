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


def lattice(path: Path, invalid=False, translation=0.0, sites=(.5, 1.5, 2.5)):
    """27 unit Voronoi cubes, including explicit periodic ghost neighbours."""
    n = 27
    header = viewer.HEADER_STRUCT.pack(
        viewer.SCENE_MAGIC.ljust(16, b"\0"), 5, 0x01020304, 208, 52, 16,
        72, 1, 1, 1, 1, 1, viewer.REQUIRED_FLAGS, n, n*6, 0, 0, 0,
        3., 10., 0., 0., 0., 2., 1., 1., 1., 155.0146484375, bytes(24))
    xyz = list(itertools.product(range(3), repeat=3))
    lookup = {p: i for i, p in enumerate(xyz)}
    cells = np.zeros(n, dtype=viewer.CELL_DTYPE)
    cells["position"] = np.asarray(sites)[np.array(xyz)] + translation
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
                edges["delta"][j, axis] = sites[neighbor[axis]]-sites[p[axis]]+(3*sign if ghost else 0)
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
        with self.assertRaisesRegex(ValueError, "reconstruction"):
            volume_render.volume_options({"volume": {"reconstruction": "invented"}})
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
        for mode, samples in (("piecewise_constant", 1), ("continuous", 1), ("continuous", 2),
                               ("linear", 1), ("linear", 2)):
            for direction, chord in (((0, 0, 1), 3), ((1, 0, 1), 3*math.sqrt(2)), ((0, 0, -1), 3)):
                image, report = self.volume.trace(self.camera(direction), 1, 1, 1, mode, samples)
                transmission = math.exp(-.25*chord)
                np.testing.assert_allclose(image[0, 0, :3], (1-transmission)*color+transmission*background, atol=2e-6)
                self.assertEqual(report["traversal_failures"], 0)
        zoomed, _ = self.volume.trace(self.camera(scale=.01), 1, 1, 1)
        np.testing.assert_allclose(zoomed, image, atol=2e-6)
        self.assertIsNone(self.owner.plotter)  # Volume preview needs no OpenGL window.

    def test_limited_linear_reproduces_an_affine_field_and_bounds_native_extrema(self):
        positions = self.volume.positions[:, :3]
        slope = np.array([[.12, -.06, .07], [.05, .02, -.01]], np.float32)
        offset = np.array([.5, .3], np.float32)
        fields = offset + positions @ slope.T
        self.volume.upload_transfer(fields, np.ones((512, 4), np.float32))
        rng = np.random.default_rng(683)
        points = rng.uniform(-.45, .45, (512, 3)).astype(np.float32)
        expected = offset + points @ slope.T
        result = self.volume.sample_fields(points, "linear")
        np.testing.assert_allclose(result, expected, atol=5e-7, rtol=1e-6)
        # The old interpolator introduces plateaus even on a smooth ramp.
        self.assertGreater(float(np.max(np.abs(self.volume.sample_fields(points)-expected))), .01)
        np.testing.assert_array_equal(self.volume.sample_fields(positions, "linear"), fields)
        self.assertEqual(self.volume.gradient_fallbacks, 0)
        fields = rng.uniform(.01, 1, (27, 2)).astype(np.float32)
        self.volume.upload_transfer(fields, np.ones((512, 4), np.float32))
        points = rng.uniform(-4.5, 4.5, (1024, 3)).astype(np.float32)
        values = self.volume.sample_fields(points, "linear")
        relative = positions[None, :, :]-points[:, None, :]
        relative -= np.round(relative/3)*3
        nearest = np.argmin(np.sum(relative*relative, axis=2), axis=1)
        xyz = np.array(list(itertools.product(range(3), repeat=3)))
        lookup = {tuple(p): i for i, p in enumerate(xyz)}
        for row, parent in zip(values, nearest):
            neighbors = [parent]
            for axis in range(3):
                for sign in (-1, 1):
                    p = xyz[parent].copy(); p[axis] = (p[axis]+sign)%3
                    neighbors.append(lookup[tuple(p)])
            self.assertTrue(np.all(row >= fields[neighbors].min(axis=0)-1e-6))
            self.assertTrue(np.all(row <= fields[neighbors].max(axis=0)+1e-6))

    def test_linear_gradients_use_unequal_native_spacing(self):
        owner = mesh_render.NativeMeshRenderer(lattice(self.root/"uneven-linear.bin", sites=(.05, .07, 2.7)), self.root)
        volume = volume_render.MetalVolume(owner)
        try:
            positions = volume.positions[:, :3]
            slope = np.array([[.03, -.02, .04], [.01, .03, -.02]], np.float32)
            offset = np.array([.5, .3], np.float32)
            fields = offset + positions @ slope.T
            volume.upload_transfer(fields, np.ones((512, 4), np.float32))
            points = positions[13] + np.random.default_rng(599).uniform(.001, .3, (256, 3)).astype(np.float32)
            np.testing.assert_allclose(volume.sample_fields(points, "linear"), offset + points @ slope.T,
                                       atol=2e-6, rtol=2e-6)
            self.assertEqual(volume.gradient_fallbacks, 0)
        finally:
            volume.close(); owner.close()

    def test_linear_hides_nonfinite_fields_and_reports_unsupported_gradients(self):
        fields = np.zeros((27, 2), np.float32)
        fields[:, 0] = np.nan
        fields[13] = [.75, .5]
        self.volume.upload_transfer(fields, np.ones((512, 4), np.float32))
        self.assertGreater(self.volume.gradient_fallbacks, 0)
        values = self.volume.sample_fields(np.array([[.1, .05, -.05], [1., 0, 0]], np.float32), "linear")
        self.assertAlmostEqual(float(values[0, 0]), .75, places=6)
        self.assertGreater(float(values[0, 1]), 0)
        self.assertTrue(np.isnan(values[1, 0]))
        self.assertEqual(values[1, 1], 0)
        image, report = self.volume.trace(self.camera(), 3, 3, 4, "linear", 2)
        self.assertTrue(np.all(np.isfinite(image)))
        self.assertGreater(report["gradient_fallback_cells"], 0)
        with self.assertRaisesRegex(ValueError, "reconstruction"):
            self.volume.sample_fields(np.zeros((1, 3)), "invented")

    def test_continuous_field_matches_independent_periodic_neighbour_search(self):
        rng = np.random.default_rng(173)
        fields = rng.uniform(.05, 1., (27, 2)).astype(np.float32)
        self.volume.upload_transfer(fields, np.ones((512, 4), np.float32))
        positions = self.volume.positions[:, :3]
        # Includes box crossings and translations by several periodic lengths.
        points = rng.uniform(-4.5, 4.5, (512, 3)).astype(np.float32)
        expected = []
        for point in points:
            relative = positions.astype(float)-point.astype(float)
            relative -= np.round(relative/3)*3
            distances = np.linalg.norm(relative, axis=1)
            order = np.argsort(distances)[:9]
            d = distances[order]
            weights = (d[0]/d-d[0]/d[-1])**2
            expected.append(np.sum(fields[order]*weights[:, None], axis=0)/weights.sum())
        result = self.volume.sample_fields(points)
        np.testing.assert_allclose(result, expected, rtol=3e-5, atol=3e-6)
        self.assertTrue(np.all(result >= fields.min(axis=0)-1e-6))
        self.assertTrue(np.all(result <= fields.max(axis=0)+1e-6))
        np.testing.assert_array_equal(self.volume.sample_fields(positions), fields)

    def test_continuity_across_cell_faces_and_periodic_boundaries(self):
        rng = np.random.default_rng(319)
        fields = rng.uniform(.1, 1., (27, 2)).astype(np.float32)
        self.volume.upload_transfer(fields, np.ones((512, 4), np.float32))
        points = rng.uniform(-1.4, 1.4, (128, 3)).astype(np.float32)
        for boundary in (.5, 1.5):
            left, right = points.copy(), points.copy()
            left[:, 0], right[:, 0] = boundary-1e-5, boundary+1e-5
            difference = self.volume.sample_fields(left)-self.volume.sample_fields(right)
            self.assertLess(float(np.max(np.abs(difference))), 2e-4)

    def test_nonuniform_cell_spacing_uses_geometry_instead_of_a_density_size_law(self):
        owner = mesh_render.NativeMeshRenderer(lattice(self.root/"uneven.bin", sites=(.05, .07, 2.7)), self.root)
        volume = volume_render.MetalVolume(owner)
        try:
            rng = np.random.default_rng(589)
            fields = rng.uniform(.1, 1., (27, 2)).astype(np.float32)
            volume.upload_transfer(fields, np.ones((512, 4), np.float32))
            points = rng.uniform(-1.5, 1.5, (256, 3)).astype(np.float32)
            relative = volume.positions[None, :, :3].astype(float)-points[:, None, :].astype(float)
            relative -= np.round(relative/3)*3
            distances = np.linalg.norm(relative, axis=2)
            nearest = np.argsort(distances, axis=1)[:, :9]
            d = np.take_along_axis(distances, nearest, axis=1)
            weights = (d[:, :1]/d-d[:, :1]/d[:, -1:])**2
            expected = (fields[nearest]*weights[:, :, None]).sum(axis=1)/weights.sum(axis=1)[:, None]
            np.testing.assert_allclose(volume.sample_fields(points), expected, atol=4e-6, rtol=3e-5)
            np.testing.assert_array_equal(volume.sample_fields(volume.positions[:, :3]), fields)
        finally:
            volume.close(); owner.close()

    def test_nonfinite_and_hidden_colours_do_not_contaminate_interpolation(self):
        fields = np.zeros((27, 2), np.float32)
        fields[:, 0] = np.nan
        fields[13] = [.75, .5]
        self.volume.upload_transfer(fields, np.ones((512, 4), np.float32))
        sample = self.volume.sample_fields(np.array([[.1, .05, -.05]], np.float32))[0]
        self.assertAlmostEqual(float(sample[0]), .75, places=6)
        self.assertGreater(float(sample[1]), 0)
        fields[13, 1] = -1
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            self.volume.upload_transfer(fields, np.ones((512, 4), np.float32))

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
            self.assertEqual(report["reconstruction"], "linear")
            params["camera"] = self.camera((1, 0, 1))
            second, _ = self.owner.render(params)
            self.assertNotEqual(first, second)
            self.assertEqual(upload.call_count, 1)
            params["volume"]["reconstruction"] = "piecewise_constant"
            _, exact_report = self.owner.render(params)
            self.assertEqual(exact_report["reconstruction"], "piecewise_constant")
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
            with self.assertRaisesRegex(ValueError, "reconstruction failed"):
                volume.sample_fields(np.array([[.1, 0, 0]], np.float32))
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
            self.assertEqual(result["report"]["backend"], "native_metal_voronoi_volume_v003")
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
