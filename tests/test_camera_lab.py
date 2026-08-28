from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from arepo_camera_lab import catalog, cleanup, demo, fields, server, spline, viewer, vtk_backend


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
            self.assertIn("Symmetric log", html)
            self.assertIn("Floating-point display precision", html)
            self.assertIn("6 significant digits", html)
            self.assertIn("const rangeState=", html)
            self.assertIn("Exact internal value", html)
            self.assertIn("Math.max(rangeState.linthresh", html)
            self.assertIn("Color map", html)
            self.assertIn("Gamma", html)
            self.assertIn("Magnetic field, pressure, and entropy are unavailable", html)
            self.assertIn("localStorage.setItem(KEYFRAME_STORAGE", html)
            self.assertIn("scene_sha256:DATA.scene.sha256", html)
            self.assertIn("VISIBLE CELLS: AREPO SNAPSHOT", html)
            self.assertIn('id="snapshot" type="text" readonly', html)
            self.assertIn("keyframes.push(p)", html)
            self.assertNotIn("keyframes[old]=p", html)
            self.assertIn("visible cells remain AREPO snapshot", html)
            self.assertIn("/api/pose", html)
            self.assertEqual(status["scene_sha256"], digest)
            state.session_directory = Path(temporary) / "server-poses"
            saved_pose = state.save_pose({
                "snapshot": 721,
                "position_cm": [1.0, 2.0, 3.0],
                "look_at_cm": [0.0, 0.0, 0.0],
                "view_direction": [-1.0, 0.0, 0.0],
                "up": [0.0, 0.0, 1.0],
                "screen_half_extent_cm": 4.0,
                "scene_sha256": digest,
                "scene_path": str(scene),
            })
            self.assertTrue(saved_pose.is_file())
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
            self.assertIn('id="snapshot" aria-label="Available AREPO snapshots"',
                          server.APP_HTML)
            self.assertIn("/api/catalog", server.APP_HTML)
            self.assertIn("Local camera-lab server unavailable", server.APP_HTML)
            self.assertIn("Archive &amp; close", server.APP_HTML)
            self.assertIn("/api/shutdown", server.APP_HTML)

    def test_auxiliary_magnetic_channels_and_id_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scene = root / "scene.bin"
            sidecar = root / "fields.npz"
            self.make_scene(scene, 5000)
            ids = np.arange(5000, dtype=np.uint64) + 100
            magnetic = np.column_stack((
                np.linspace(1.0e5, 2.0e7, 5000),
                np.linspace(-2.0e6, 3.0e6, 5000),
                np.linspace(4.0e5, 8.0e6, 5000),
            )).astype(np.float32)
            np.savez_compressed(
                sidecar, schema=np.asarray(viewer.AUXILIARY_SCHEMA),
                particle_id=ids, magnetic_field_gauss=magnetic,
                pressure_dyn_cm2=np.geomspace(1.0e14, 1.0e20, 5000).astype(np.float32),
                specific_entropy_cgs=np.linspace(1.0, 3.0, 5000).astype(np.float32),
                sound_speed_cm_s=np.geomspace(1.0e6, 1.0e8, 5000).astype(np.float32))
            state = server.ViewerState()
            ready = state.load(scene, 721, 3000, viewer.sha256(scene), sidecar)
            self.assertTrue(ready["loaded"])
            channels = state.payload["channels"]
            for name in ("magnetic_field_strength", "magnetic_pressure",
                         "alfven_speed", "plasma_beta", "specific_entropy",
                         "sound_speed", "mach_number", "toroidal_field_fraction"):
                self.assertIn(name, channels)
            self.assertEqual(
                state.payload["scene"]["auxiliary_fields"]["schema"],
                viewer.AUXILIARY_SCHEMA)
            self.assertIn("Auxiliary fields loaded", state.viewer_html())

    def test_hdf5_field_sidecar_builder(self) -> None:
        import h5py
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot.hdf5"
            output = root / "snapshot.fields.npz"
            with h5py.File(snapshot, "w") as handle:
                gas = handle.create_group("PartType0")
                gas.create_dataset("ParticleIDs", data=np.arange(8, dtype=np.uint64) + 7)
                gas.create_dataset("MagneticField", data=np.ones((8, 3)))
                gas.create_dataset("Pressure", data=np.arange(8, dtype=float) + 1.0)
            result = fields.build_sidecar(
                snapshot, output, ids_dataset="PartType0/ParticleIDs",
                field_specs={
                    "magnetic_field_gauss": ("PartType0/MagneticField", 2.0),
                    "pressure_dyn_cm2": ("PartType0/Pressure", 3.0),
                })
            self.assertEqual(result["particle_count"], 8)
            loaded = viewer.read_field_sidecar(output)
            self.assertTrue(np.allclose(
                loaded["fields"]["magnetic_field_gauss"], 2.0))
            with self.assertRaises(FileExistsError):
                fields.build_sidecar(
                    snapshot, output, ids_dataset="PartType0/ParticleIDs",
                    field_specs={"magnetic_field_gauss":
                                 ("PartType0/MagneticField", 2.0)})

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

    def test_native_vtk_data_and_pose_contract(self) -> None:
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scene_path = root / "scene.bin"
            sidecar_path = root / "fields.npz"
            self.make_scene(scene_path, 5000)
            ids = np.arange(5000, dtype=np.uint64) + 100
            magnetic = np.column_stack((
                np.linspace(1.0, 2.0, 5000),
                np.linspace(-1.0, 1.0, 5000),
                np.linspace(0.5, 1.5, 5000),
            )).astype(np.float32)
            np.savez_compressed(
                sidecar_path, schema=np.asarray(viewer.AUXILIARY_SCHEMA),
                particle_id=ids, magnetic_field_gauss=magnetic,
                pressure_dyn_cm2=np.geomspace(1.0e14, 1.0e20, 5000).astype(np.float32),
                sound_speed_cm_s=np.geomspace(1.0e6, 1.0e8, 5000).astype(np.float32))
            native = vtk_backend.load_native_scene(
                scene_path, snapshot=721, max_points=3000,
                scene_sha256=viewer.sha256(scene_path), field_sidecar=sidecar_path)
            self.assertEqual(native.points.shape, (3000, 3))
            self.assertEqual(native.magnetic_vectors.shape, (3000, 3))
            self.assertIn("plasma_beta", native.channels)
            transformed = vtk_backend.transform_values(
                native.channels["radial_velocity"], "symlog", 1.0e5)
            self.assertTrue(np.all(np.isfinite(transformed)))
            camera = SimpleNamespace(
                position=(0.0, 0.0, -2.0), focal_point=(0.0, 0.0, 0.0),
                up=(0.0, 1.0, 0.0), parallel_scale=0.25)
            pose = vtk_backend.camera_pose(native, camera)
            self.assertEqual(pose["snapshot"], 721)
            self.assertAlmostEqual(
                pose["screen_half_extent_cm"], native.display_radius_cm * 0.25)
            output = vtk_backend.write_pose(native, camera, root / "poses")
            alternative = vtk_backend.write_pose(native, camera, root / "poses")
            self.assertNotEqual(output, alternative)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["keyframes"][0]["snapshot"], 721)
            self.assertIn("native_vtk", saved["keyframes"][0]["backend"])

    def test_native_vtk_content_addressed_scene_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scene = root / "scene.bin"
            self.make_scene(scene, 1000)
            digest = viewer.sha256(scene)
            cached, actual = vtk_backend.cache_scene_file(
                scene, digest, root / "cache")
            self.assertEqual(actual, digest)
            self.assertEqual(viewer.sha256(cached), digest)
            repeated, repeated_digest = vtk_backend.cache_scene_file(
                scene, digest, root / "cache")
            self.assertEqual(repeated, cached)
            self.assertEqual(repeated_digest, digest)

    def test_verified_snapshot_catalog_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "catalog.json"
            digest_a = "a" * 64
            digest_b = "b" * 64
            path.write_text(json.dumps({
                "schema": catalog.CATALOG_SCHEMA,
                "required_auxiliary_fields": [
                    "magnetic_field_gauss", "pressure_dyn_cm2", "sound_speed_cm_s"],
                "frames": [{
                    "snapshot": 721,
                    "label": "disk formation",
                    "scene_source": "user@host:/data/snapshot_0721/scene_v052.bin",
                    "scene_sha256": digest_a,
                    "field_sidecar_source": "user@host:/data/snapshot_0721.fields.npz",
                    "field_sidecar_sha256": digest_b,
                }],
            }), encoding="utf-8")
            loaded = catalog.load_catalog(path)
            self.assertEqual(list(loaded.frames), [721])
            state = server.ViewerState(catalog=loaded)
            public = state.catalog_payload()
            self.assertEqual(public["frames"][0]["snapshot"], 721)
            self.assertEqual(len(public["required_auxiliary_fields"]), 3)

    def test_cleanup_deletes_cache_only_after_verified_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = root / "poses"
            outputs.mkdir()
            (outputs / "pose.json").write_text("{}\n", encoding="utf-8")
            cached = root / "scene.bin"
            cached.write_bytes(b"verified scene")
            digest = viewer.sha256(cached)
            item = cleanup.CachedInput(
                cached, "user@host:/cluster/scene.bin", digest)
            with mock.patch.object(cleanup, "remote_sha256", return_value=digest), \
                    mock.patch.object(cleanup, "sync_directory_no_clobber") as sync:
                receipt = cleanup.archive_and_cleanup(
                    outputs, "user@host:/cluster/sessions/unique", [item])
            sync.assert_called_once()
            self.assertFalse(cached.exists())
            self.assertTrue(receipt.is_file())

            failed = root / "failed.bin"
            failed.write_bytes(b"keep me")
            failed_digest = viewer.sha256(failed)
            with mock.patch.object(cleanup, "remote_sha256", return_value="0" * 64):
                with self.assertRaises(ValueError):
                    cleanup.archive_and_cleanup(
                        outputs, "user@host:/cluster/sessions/another",
                        [cleanup.CachedInput(
                            failed, "user@host:/cluster/failed.bin", failed_digest)])
            self.assertTrue(failed.exists())

    def test_webgl_float32_encoding_bounds_extreme_derived_values(self) -> None:
        encoded = viewer._encode_float32(np.asarray([1.0, 1.0e400, -1.0e400]))
        decoded = vtk_backend._decode_float32(encoded, (3,))
        self.assertTrue(np.all(np.isfinite(decoded)))
        self.assertEqual(decoded[0], 1.0)


if __name__ == "__main__":
    unittest.main()
