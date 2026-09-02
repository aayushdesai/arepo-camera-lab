from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from arepo_camera_lab import cleanup, cli, review, server, transfer


class ArchiveCloseTest(unittest.TestCase):
    def fixture(self, root):
        outputs = root / 'session'
        outputs.mkdir()
        pose = outputs / 'camera_pose_snapshot_0001_001.json'
        pose.write_text(json.dumps({'schema': 'stellar_camera_keyframes_v001', 'keyframes': []}))
        unrelated = outputs / 'unrelated.txt'
        unrelated.write_text('keep outside archive')
        body = b'known cached bytes'
        digest = hashlib.sha256(body).hexdigest()
        source = 'fixture:/cluster/scene.bin'
        cached = transfer.verified_cache_path(source, digest, root)
        cached.write_bytes(body)
        return outputs, pose, unrelated, cleanup.CachedInput(cached, source, digest)

    def test_success_scopes_archive_and_retry_uses_fresh_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs, pose, unrelated, item = self.fixture(Path(tmp))
            attempts = []
            def sync(staging, destination):
                attempts.append(destination)
                self.assertTrue((staging / pose.name).is_file())
                self.assertFalse((staging / unrelated.name).exists())
                self.assertTrue(item.local_path.exists())
                if len(attempts) == 1:
                    raise OSError('transfer interrupted')
            with mock.patch.object(cleanup, 'remote_sha256', return_value=item.sha256), mock.patch.object(cleanup, 'sync_directory_no_clobber', side_effect=sync):
                with self.assertRaisesRegex(OSError, 'transfer interrupted'):
                    cleanup.archive_and_cleanup(outputs, 'fixture:/archives', [item])
                self.assertTrue(item.local_path.exists())
                receipt = cleanup.archive_and_cleanup(outputs, 'fixture:/archives', [item, item])
            self.assertNotEqual(attempts[0], attempts[1])
            self.assertFalse(item.local_path.exists())
            self.assertTrue(pose.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(json.loads(receipt.read_text())['removed_cache_files'], 1)

    def test_verification_and_cache_replacement_failures_retain_local_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs, pose, unrelated, item = self.fixture(Path(tmp))
            with mock.patch.object(cleanup, 'remote_sha256', return_value='0'*64), mock.patch.object(cleanup, 'sync_directory_no_clobber') as sync:
                with self.assertRaisesRegex(ValueError, 'cluster source digest mismatch'):
                    cleanup.archive_and_cleanup(outputs, 'fixture:/archives', [item])
                sync.assert_not_called()
            self.assertTrue(item.local_path.exists())
            with mock.patch.object(cleanup, 'remote_sha256', return_value=item.sha256), mock.patch.object(cleanup, 'sync_directory_no_clobber', side_effect=lambda *_: item.local_path.write_bytes(b'new cache content')):
                with self.assertRaisesRegex(ValueError, 'cache changed before cleanup'):
                    cleanup.archive_and_cleanup(outputs, 'fixture:/archives', [item])
            self.assertEqual(item.local_path.read_bytes(), b'new cache content')

    def test_original_inputs_and_symlink_targets_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs, pose, unrelated, item = self.fixture(root)
            original = root / 'scene.bin'
            original.write_bytes(item.local_path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'not a managed content-addressed cache'):
                cleanup.archive_and_cleanup(outputs, 'fixture:/archives', [cleanup.CachedInput(original, item.remote_source, item.sha256)])
            item.local_path.unlink()
            item.local_path.symlink_to(original)
            with self.assertRaisesRegex(ValueError, 'symlink cache'):
                cleanup.archive_and_cleanup(outputs, 'fixture:/archives', [item])
            self.assertTrue(original.exists())

    def test_completion_is_observable_until_ack_and_mutations_are_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'receipt.json'
            state = server.ViewerState(cleanup_configured=True, cleanup_destination='fixture:/archive', session_directory=Path(tmp))
            http = mock.Mock()
            with mock.patch.object(cleanup, 'archive_and_cleanup', return_value=receipt) as archive:
                queued = state.start_cleanup(http)
                for _ in range(100):
                    if state.status()['cleanup_state'] == 'complete':
                        break
                    time.sleep(0.001)
                self.assertEqual(state.status()['cleanup_state'], 'complete')
                self.assertFalse(state.status()['cleanup_running'])
                http.shutdown.assert_not_called()
                with self.assertRaisesRegex(ValueError, 'session is closing'):
                    state.save_review_bundle({})
                self.assertEqual(state.start_cleanup(http)['archive_id'], queued['archive_id'])
                with self.assertRaisesRegex(ValueError, 'matching completed archive'):
                    state.acknowledge_cleanup('wrong-id')
                state.acknowledge_cleanup(queued['archive_id'])
                state.cleanup_worker.join(2)
                archive.assert_called_once()
                http.shutdown.assert_called_once()

    def test_closed_browser_does_not_leave_server_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = server.ViewerState(cleanup_configured=True, cleanup_destination='fixture:/archive', session_directory=Path(tmp), cleanup_grace_seconds=0.01)
            http = mock.Mock()
            with mock.patch.object(cleanup, 'archive_and_cleanup', return_value=Path(tmp)/'receipt.json'):
                state.start_cleanup(http)
                state.cleanup_worker.join(2)
            http.shutdown.assert_called_once()

    def test_discovery_failure_stays_visible_and_quit_never_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = server.ViewerState(cleanup_configured=True, cleanup_destination='fixture:/archive', catalog_path=root/'missing.json', session_directory=root)
            http = mock.Mock()
            state.start_cleanup(http)
            state.cleanup_worker.join(2)
            self.assertEqual(state.status()['cleanup_state'], 'failed')
            self.assertIn('missing.json', state.status()['cleanup_error'])
            http.shutdown.assert_not_called()
            with mock.patch.object(cleanup, 'archive_and_cleanup') as archive:
                self.assertEqual(state.start_quit()['status'], 'server_stopping')
                archive.assert_not_called()

    def test_destination_alone_enables_archive_and_ctrl_c_does_not_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = {'schema':'arepo_camera_lab_archive_settings_v001','destination':'fixture:/saved'}
            (root/'archive_settings.json').write_text(json.dumps(settings))
            for explicit in (False, True):
                command = ['serve','--scene',str(root/'synthetic.bin'),'--session-directory',str(root),'--no-browser']
                if explicit:
                    command += ['--sync-back-destination','fixture:/explicit']
                args=cli.parser().parse_args(command)
                def run(state, port):
                    self.assertTrue(state.cleanup_configured)
                    self.assertEqual(state.cleanup_destination, 'fixture:/explicit' if explicit else 'fixture:/saved')
                with mock.patch.object(server.ViewerState,'start_load'), mock.patch.object(server,'run_server',side_effect=run), mock.patch.object(cleanup,'archive_and_cleanup') as archive:
                    self.assertEqual(cli._serve(args),0)
                    archive.assert_not_called()

    def test_browser_session_preserves_all_43_geometries_and_separate_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            poses = [{'pose_id':f'pose-{i}','snapshot':31,'position_cm':[1.,2.,3.],
                      'look_at_cm':[0.,0.,0.],'view_direction':[0.,0.,1.],'up':[0.,1.,0.],
                      'screen_half_extent_cm':12.3456789012345,'scene_sha256':'a'*64} for i in range(43)]
            bundle=review.normalize_bundle({'schema':'stellar_camera_keyframes_v001','keyframes':poses})
            payload={'schema':'arepo_camera_lab_browser_session_v001','visible_scene':{'snapshot':31,'sha256':'a'*64},
                     'review_bundle':bundle,'review_drafts':{'pose-2':{'gamma':3.,'low':0.,'high':10000.}},
                     'derived_channels':[{'name':'ratio','expression':'gas_pressure / magnetic_pressure'}]}
            state=server.ViewerState(payload={'scene':{'snapshot':31,'sha256':'a'*64}}, review_bundle=bundle, session_directory=Path(tmp))
            path=state.save_browser_session(payload)
            saved=json.loads(path.read_text())
            self.assertEqual(saved,payload)
            self.assertEqual(len(saved['review_bundle']['geometry']['alternatives']),43)
            self.assertNotEqual(state.save_browser_session(payload),path)
            payload['review_bundle']['geometry']['alternatives'][0]['up']=[1.,0.,0.]
            with self.assertRaisesRegex(ValueError,'immutable camera geometry'):
                state.save_browser_session(payload)


if __name__ == '__main__':
    unittest.main()
