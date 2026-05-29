"""
Integration tests for the new client-managed job resources in fslink mode.
Requires a running Docker daemon (containers are actually scheduled).

Tests the full lifecycle:
  POST /copyjobs/    -> schedule copy container
  GET  /copyjobs/id/ -> poll copy status until finished
  POST /pluginjobs/  -> schedule plugin container
  GET  /pluginjobs/id/ -> poll plugin status until finished
  GET  /pluginjobs/id/file/ -> retrieve output metadata
  POST /deletejobs/  -> schedule delete container
  GET  /deletejobs/id/ -> poll delete status until finished
  DELETE /copyjobs/id/, /pluginjobs/id/, /deletejobs/id/ -> cleanup
"""

import logging
import shutil
import os
import tempfile
import time
import json
from unittest import TestCase, mock

from flask import url_for
from werkzeug.exceptions import NotFound

import pfcon.copy_worker as cw
from pfcon.app import create_app
from pfcon.compute.container_user import ContainerUser
from pfcon.compute.abstractmgr import ManagerException
from pfcon.base_resources import get_compute_mgr
from pfcon.storage.fslink_storage import FSLinkStorage


class NewResourcesFsLinkTests(TestCase):
    """Base class for the new resources integration tests in fslink mode."""

    def setUp(self):
        logging.disable(logging.WARNING)

        self.app = create_app({'PFCON_INNETWORK': True,
                               'STORAGE_ENV': 'fslink'})
        self.client = self.app.test_client()

        with self.app.test_request_context():
            url = url_for('api.auth')
            creds = {
                'pfcon_user': self.app.config.get('PFCON_USER'),
                'pfcon_password': self.app.config.get('PFCON_PASSWORD')
            }
            response = self.client.post(url, data=json.dumps(creds),
                                        content_type='application/json')
            self.headers = {'Authorization': 'Bearer ' + response.json['token']}

            self.storebase_mount = self.app.config.get('STOREBASE_MOUNT')
            self.storebase = self.app.config.get('STOREBASE')
            self.container_env = self.app.config.get('CONTAINER_ENV')
            self.user = ContainerUser.parse(
                self.app.config.get('CONTAINER_USER'))

            self.user_dir = os.path.join(self.storebase_mount, 'home/foo')
            self.fs_input_path = os.path.join(self.user_dir, 'feed/input')
            self.fs_output_path = os.path.join(self.user_dir, 'feed/output')

            os.makedirs(self.fs_input_path, exist_ok=True)
            os.makedirs(self.fs_output_path, exist_ok=True)

            with open(self.fs_input_path + '/test.txt', 'w') as f:
                f.write('Test file')

    def tearDown(self):
        if os.path.isdir(self.user_dir):
            shutil.rmtree(self.user_dir)
        pipeline_dir = os.path.join(self.storebase_mount, 'PIPELINES')
        if os.path.isdir(pipeline_dir):
            shutil.rmtree(pipeline_dir)
        logging.disable(logging.NOTSET)

    def _remove_container(self, name):
        with self.app.test_request_context():
            compute_mgr = get_compute_mgr(self.container_env)
            try:
                job = compute_mgr.get_job(name)
                compute_mgr.remove_job(job)
            except ManagerException:
                pass


class TestFullLifecycleFsLink(NewResourcesFsLinkTests):
    """Full end-to-end test of the new client-managed job flow."""

    def test_full_lifecycle(self):
        job_id = 'new-fslink-e2e-1'
        input_rel = os.path.relpath(self.fs_input_path, self.storebase_mount)
        output_rel = os.path.relpath(self.fs_output_path, self.storebase_mount)

        try:
            # 1. Schedule copy job
            with self.app.test_request_context():
                copy_list_url = url_for('api.copyjoblist')
                copy_url = url_for('api.copyjob', job_id=job_id)

            copy_data = {
                'jid': job_id,
                'input_dirs': [input_rel],
                'output_dir': output_rel,
            }
            response = self.client.post(copy_list_url, data=copy_data,
                                        headers=self.headers)
            self.assertEqual(response.status_code, 201)

            # 2. Poll copy until finished
            for _ in range(30):
                time.sleep(3)
                response = self.client.get(copy_url, headers=self.headers)
                status = response.json['compute']['status']
                if status == 'finishedSuccessfully':
                    break
            self.assertEqual(response.json['compute']['status'],
                             'finishedSuccessfully')

            # Verify files were copied
            incoming = os.path.join(self.storebase_mount, 'key-' + job_id,
                                    'incoming')
            self.assertTrue(os.path.isfile(
                os.path.join(incoming, 'test.txt')))

            # 3. Schedule plugin job
            with self.app.test_request_context():
                plugin_list_url = url_for('api.pluginjoblist')
                plugin_url = url_for('api.pluginjob', job_id=job_id)

            plugin_data = {
                'jid': job_id,
                'entrypoint': ['python3', '/usr/local/bin/simplefsapp'],
                'args': ['--dir', '/share/incoming'],
                'auid': 'cube',
                'number_of_workers': '1',
                'cpu_limit': '1000',
                'memory_limit': '200',
                'gpu_limit': '0',
                'image': 'fnndsc/pl-simplefsapp',
                'type': 'fs',
                'input_dirs': [input_rel],
                'output_dir': output_rel,
            }
            response = self.client.post(plugin_list_url, data=plugin_data,
                                        headers=self.headers)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json['data'], {})

            # 4. Poll plugin until finished
            for _ in range(30):
                time.sleep(3)
                response = self.client.get(plugin_url, headers=self.headers)
                status = response.json['compute']['status']
                if status == 'finishedSuccessfully':
                    break
            self.assertEqual(response.json['compute']['status'],
                             'finishedSuccessfully')

            # 5. Get output file metadata
            with self.app.test_request_context():
                file_url = url_for('api.pluginjobfile', job_id=job_id)

            response = self.client.get(
                file_url,
                query_string={'job_output_path': output_rel},
                headers=self.headers)
            self.assertEqual(response.status_code, 200)
            content = json.loads(response.data.decode())
            self.assertEqual(content['job_output_path'], output_rel)

            # 6. Schedule delete job
            with self.app.test_request_context():
                delete_list_url = url_for('api.deletejoblist')
                delete_url = url_for('api.deletejob', job_id=job_id)

            delete_data = {'jid': job_id}
            response = self.client.post(delete_list_url, data=delete_data,
                                        headers=self.headers)
            self.assertEqual(response.status_code, 201)

            # 7. Poll delete until finished
            for _ in range(30):
                time.sleep(3)
                response = self.client.get(delete_url, headers=self.headers)
                status = response.json['compute']['status']
                if status == 'finishedSuccessfully':
                    break
            self.assertEqual(response.json['compute']['status'],
                             'finishedSuccessfully')

            # Verify data was deleted
            self.assertFalse(os.path.isdir(incoming))

            # 8. Delete all containers
            response = self.client.delete(copy_url, headers=self.headers)
            self.assertEqual(response.status_code, 204)

            response = self.client.delete(plugin_url, headers=self.headers)
            self.assertEqual(response.status_code, 204)

            response = self.client.delete(delete_url, headers=self.headers)
            self.assertEqual(response.status_code, 204)

        except Exception:
            # Force-cleanup on failure
            for suffix in ('-copy', '', '-delete'):
                self._remove_container(job_id + suffix)
            key_dir = os.path.join(self.storebase_mount, 'key-' + job_id)
            if os.path.isdir(key_dir):
                shutil.rmtree(key_dir)
            raise

    def test_copy_with_chris_links(self):
        """Copy job should recursively follow .chrislink files."""
        job_id = 'new-fslink-links-1'
        input_rel = os.path.relpath(self.fs_input_path, self.storebase_mount)
        output_rel = os.path.relpath(self.fs_output_path, self.storebase_mount)

        # Set up ChRIS link structure
        pipeline_dir = os.path.join(self.storebase_mount, 'PIPELINES/bob')
        os.makedirs(pipeline_dir, exist_ok=True)
        link_dir = os.path.join(self.storebase_mount, 'home/bob')
        os.makedirs(link_dir, exist_ok=True)

        with open(pipeline_dir + '/pipeline.yml', 'w') as f:
            f.write('Test pipeline')
        with open(link_dir + '/PIPELINES_bob.chrislink', 'w') as f:
            f.write('PIPELINES/bob')
        with open(self.fs_input_path + '/home_bob.chrislink', 'w') as f:
            f.write('home/bob')

        try:
            with self.app.test_request_context():
                copy_list_url = url_for('api.copyjoblist')
                copy_url = url_for('api.copyjob', job_id=job_id)

            copy_data = {
                'jid': job_id,
                'input_dirs': [input_rel],
                'output_dir': output_rel,
            }
            response = self.client.post(copy_list_url, data=copy_data,
                                        headers=self.headers)
            self.assertEqual(response.status_code, 201)

            for _ in range(30):
                time.sleep(3)
                response = self.client.get(copy_url, headers=self.headers)
                status = response.json['compute']['status']
                if status == 'finishedSuccessfully':
                    break

            self.assertEqual(response.json['compute']['status'],
                             'finishedSuccessfully')

            # Verify ChRIS links were followed
            incoming = os.path.join(self.storebase_mount, 'key-' + job_id,
                                    'incoming')
            self.assertTrue(os.path.isfile(
                os.path.join(incoming, 'home_bob', 'PIPELINES_bob',
                             'pipeline.yml')))

        finally:
            self._remove_container(job_id + '-copy')
            key_dir = os.path.join(self.storebase_mount, 'key-' + job_id)
            if os.path.isdir(key_dir):
                shutil.rmtree(key_dir)
            link_parent = os.path.join(self.storebase_mount, 'home/bob')
            if os.path.isdir(link_parent):
                shutil.rmtree(link_parent)


class TestCopyJobListFsLink(NewResourcesFsLinkTests):
    """Test CopyJobList resource specifically."""

    def test_get_returns_server_info(self):
        with self.app.test_request_context():
            url = url_for('api.copyjoblist')
        response = self.client.get(url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['pfcon_innetwork'])
        self.assertEqual(response.json['storage_env'], 'fslink')


class TestPluginJobListFsLink(NewResourcesFsLinkTests):
    """Test PluginJobList resource specifically."""

    def test_get_returns_server_info(self):
        with self.app.test_request_context():
            url = url_for('api.pluginjoblist')
        response = self.client.get(url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['pfcon_innetwork'])


class TestDeleteJobNoDataFsLink(NewResourcesFsLinkTests):
    """Test DeleteJob when no data directory exists."""

    def test_post_noop_when_no_key_dir(self):
        with self.app.test_request_context():
            url = url_for('api.deletejoblist')
        data = {'jid': 'nonexistent-job'}
        response = self.client.post(url, data=data, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['compute']['status'],
                         'finishedSuccessfully')
        self.assertEqual(response.json['compute']['message'],
                         'deleteSkipped')


class TestFSLinkStorageGetDataSanitization(TestCase):
    """
    Unit tests for FSLinkStorage.get_data() output sanitization:
    plugin-produced .chrislink files and symlinks must be removed,
    and any failure to remove them must propagate (treated as fatal,
    matching CUBE's _register_output_files() behavior).
    """

    def setUp(self):
        logging.disable(logging.WARNING)
        self.tmpdir = tempfile.mkdtemp()
        self.job_output_rel = 'job-out'
        self.outgoing_dir = os.path.join(self.tmpdir, self.job_output_rel)
        os.makedirs(self.outgoing_dir)
        self.storage = FSLinkStorage({'STOREBASE_MOUNT': self.tmpdir})

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def _get_data(self):
        content = self.storage.get_data('job-1', self.outgoing_dir,
                                         job_output_path=self.job_output_rel)
        return json.loads(content.getvalue().decode())

    def test_removes_chrislink_files(self):
        regular_path = os.path.join(self.outgoing_dir, 'out.txt')
        link_path = os.path.join(self.outgoing_dir, 'evil.chrislink')
        with open(regular_path, 'w') as f:
            f.write('legit output')
        with open(link_path, 'w') as f:
            f.write('home/victim')

        data = self._get_data()

        self.assertFalse(os.path.exists(link_path))
        self.assertTrue(os.path.exists(regular_path))
        self.assertIn('out.txt', data['rel_file_paths'])
        self.assertNotIn('evil.chrislink', data['rel_file_paths'])

    def test_removes_symlinks(self):
        target_path = os.path.join(self.tmpdir, 'secret.txt')
        with open(target_path, 'w') as f:
            f.write('secret data')
        regular_path = os.path.join(self.outgoing_dir, 'out.txt')
        with open(regular_path, 'w') as f:
            f.write('legit output')
        symlink_path = os.path.join(self.outgoing_dir, 'leak')
        os.symlink(target_path, symlink_path)

        data = self._get_data()

        self.assertFalse(os.path.lexists(symlink_path))
        self.assertTrue(os.path.exists(target_path))  # target untouched
        self.assertTrue(os.path.exists(regular_path))
        self.assertIn('out.txt', data['rel_file_paths'])
        self.assertNotIn('leak', data['rel_file_paths'])

    def test_chrislink_symlink_takes_chrislink_path(self):
        # a *.chrislink symlink must be removed via os.remove (which unlinks
        # the symlink itself, not its target) and logged through the
        # .chrislink branch
        target_path = os.path.join(self.tmpdir, 'target.txt')
        with open(target_path, 'w') as f:
            f.write('linked content')
        link_path = os.path.join(self.outgoing_dir, 'evil.chrislink')
        os.symlink(target_path, link_path)

        data = self._get_data()

        self.assertFalse(os.path.lexists(link_path))
        self.assertTrue(os.path.exists(target_path))
        self.assertNotIn('evil.chrislink', data['rel_file_paths'])

    def test_raises_on_unremovable_chrislink(self):
        link_path = os.path.join(self.outgoing_dir, 'evil.chrislink')
        with open(link_path, 'w') as f:
            f.write('home/victim')

        with (
            mock.patch('pfcon.storage.fslink_storage.os.remove',
                       side_effect=OSError('permission denied')),
            self.assertRaises(OSError),
        ):
            self.storage.get_data('job-1', self.outgoing_dir,
                                   job_output_path=self.job_output_rel)
        # link must still exist on disk: removal failed and the exception
        # propagated, so the job is fatally failed by the caller
        self.assertTrue(os.path.exists(link_path))

    def test_raises_on_unremovable_symlink(self):
        target_path = os.path.join(self.tmpdir, 'secret.txt')
        with open(target_path, 'w') as f:
            f.write('secret')
        symlink_path = os.path.join(self.outgoing_dir, 'leak')
        os.symlink(target_path, symlink_path)

        with (
            mock.patch('pfcon.storage.fslink_storage.os.remove',
                       side_effect=OSError('permission denied')),
            self.assertRaises(OSError),
        ):
            self.storage.get_data('job-1', self.outgoing_dir,
                                   job_output_path=self.job_output_rel)
        self.assertTrue(os.path.lexists(symlink_path))

    def test_removes_symlink_to_directory(self):
        # os.walk classifies symlink-to-dir as a directory (entry.is_dir()
        # follows symlinks), so it lands in `dirs`, not `files`. The
        # sanitizer must check dirs too.
        target_dir = os.path.join(self.tmpdir, 'secret_dir')
        os.makedirs(target_dir)
        with open(os.path.join(target_dir, 'inside.txt'), 'w') as f:
            f.write('private')
        regular_path = os.path.join(self.outgoing_dir, 'out.txt')
        with open(regular_path, 'w') as f:
            f.write('legit output')
        symlink_path = os.path.join(self.outgoing_dir, 'leak_dir')
        os.symlink(target_dir, symlink_path)

        data = self._get_data()

        self.assertFalse(os.path.lexists(symlink_path))
        # target directory and its contents are untouched
        self.assertTrue(os.path.isdir(target_dir))
        self.assertTrue(os.path.isfile(
            os.path.join(target_dir, 'inside.txt')))
        self.assertIn('out.txt', data['rel_file_paths'])
        # nothing from the symlink target should leak into the metadata
        self.assertFalse(any('leak_dir' in p or 'inside.txt' in p
                             for p in data['rel_file_paths']))

    def test_raises_on_unremovable_symlink_to_directory(self):
        target_dir = os.path.join(self.tmpdir, 'secret_dir')
        os.makedirs(target_dir)
        symlink_path = os.path.join(self.outgoing_dir, 'leak_dir')
        os.symlink(target_dir, symlink_path)

        with (
            mock.patch('pfcon.storage.fslink_storage.os.remove',
                       side_effect=OSError('permission denied')),
            self.assertRaises(OSError),
        ):
            self.storage.get_data('job-1', self.outgoing_dir,
                                   job_output_path=self.job_output_rel)
        self.assertTrue(os.path.lexists(symlink_path))

    def test_dirs_mutation_blocks_descent_when_followlinks_true(self):
        # The dirs[:] mutation is the second line of defense: even with
        # followlinks=True (a future-proofing concern), os.walk must not
        # descend into a removed symlink. Make os.remove a no-op so the
        # symlink stays on disk and force followlinks=True; if dirs[:]
        # mutation were removed, os.walk would descend into the symlinked
        # dir and `inside.txt` would leak into rel_file_paths.
        target_dir = os.path.join(self.tmpdir, 'secret_dir')
        os.makedirs(target_dir)
        with open(os.path.join(target_dir, 'inside.txt'), 'w') as f:
            f.write('private')
        os.symlink(target_dir,
                   os.path.join(self.outgoing_dir, 'leak_dir'))

        original_walk = os.walk

        def walk_with_followlinks(*args, **kwargs):
            kwargs['followlinks'] = True
            return original_walk(*args, **kwargs)

        with (
            mock.patch('pfcon.storage.fslink_storage.os.remove'),
            mock.patch('pfcon.storage.fslink_storage.os.walk',
                       side_effect=walk_with_followlinks),
        ):
            data = self._get_data()

        self.assertFalse(any('inside.txt' in p
                             for p in data['rel_file_paths']),
                         f'symlink target leaked: {data["rel_file_paths"]}')

    def test_sanitizes_nested_directories(self):
        # Sanitization applies through subdirectories — a .chrislink or
        # symlink several levels deep must still be removed and excluded
        # from rel_file_paths.
        subdir = os.path.join(self.outgoing_dir, 'sub1', 'sub2')
        os.makedirs(subdir)
        with open(os.path.join(subdir, 'evil.chrislink'), 'w') as f:
            f.write('home/x')
        with open(os.path.join(subdir, 'real.txt'), 'w') as f:
            f.write('ok')
        target_path = os.path.join(self.tmpdir, 'secret.txt')
        with open(target_path, 'w') as f:
            f.write('s')
        os.symlink(target_path, os.path.join(subdir, 'leak'))

        data = self._get_data()

        self.assertFalse(os.path.exists(
            os.path.join(subdir, 'evil.chrislink')))
        self.assertFalse(os.path.lexists(os.path.join(subdir, 'leak')))
        rel_real = os.path.join('sub1', 'sub2', 'real.txt')
        self.assertIn(rel_real, data['rel_file_paths'])
        self.assertFalse(any(p.endswith('.chrislink') or p.endswith('leak')
                             for p in data['rel_file_paths']))

    def test_removes_broken_symlink(self):
        # A dangling symlink (target does not exist) lands in `files`
        # because os.walk's is_dir(follow=True) returns False. The
        # sanitizer must still remove it — both as defense-in-depth and
        # because a downstream consumer could resolve the target.
        symlink_path = os.path.join(self.outgoing_dir, 'broken')
        os.symlink(os.path.join(self.tmpdir, 'does_not_exist'),
                   symlink_path)
        with open(os.path.join(self.outgoing_dir, 'real.txt'), 'w') as f:
            f.write('ok')

        data = self._get_data()

        self.assertFalse(os.path.lexists(symlink_path))
        self.assertIn('real.txt', data['rel_file_paths'])
        self.assertNotIn('broken', data['rel_file_paths'])


class TestPluginJobFileFsLinkSanitizationFailure(NewResourcesFsLinkTests):
    """
    Lock in the resources.py outer wrapper: when FSLinkStorage.get_data
    raises during output sanitization, PluginJobFile must convert the
    exception into an HTTP 500 rather than letting it become a 200 with
    incomplete or tainted metadata.
    """

    def test_returns_500_when_get_data_raises(self):
        job_id = 'fslink-sanitization-fail-1'
        output_rel = os.path.relpath(self.fs_output_path,
                                     self.storebase_mount)

        # Drop a .chrislink file in the outgoing dir so the sanitizer has
        # a removal call to make.
        with open(os.path.join(self.fs_output_path, 'evil.chrislink'),
                  'w') as f:
            f.write('home/x')

        with self.app.test_request_context():
            file_url = url_for('api.pluginjobfile', job_id=job_id)

        # Force the .chrislink removal to fail; this exercises the
        # OSError re-raise in get_data and the abort(500) wrapper in
        # resources.PluginJobFile.get().
        with mock.patch('pfcon.storage.fslink_storage.os.remove',
                        side_effect=OSError('permission denied')):
            response = self.client.get(
                file_url,
                query_string={'job_output_path': output_rel},
                headers=self.headers)

        self.assertEqual(response.status_code, 500)


class TestPluginJobFileFsLinkHTTPExceptionPassthrough(NewResourcesFsLinkTests):
    """
    The abort-500 wrappers in PluginJobFile.get() must let
    werkzeug HTTPExceptions propagate unchanged. Without an explicit
    `except HTTPException: raise` ahead of the broad `except Exception`,
    a future storage method that calls flask_restful.abort(404) (or
    similar) would have its intended status silently downgraded to 500.
    """

    def test_http_exception_from_storage_propagates_unchanged(self):
        job_id = 'fslink-httpexc-1'
        output_rel = os.path.relpath(self.fs_output_path,
                                     self.storebase_mount)

        with self.app.test_request_context():
            file_url = url_for('api.pluginjobfile', job_id=job_id)

        # Storage method raises NotFound (a 404 HTTPException). The
        # wrapper must let it pass through, not catch and rewrite as 500.
        with mock.patch(
                'pfcon.storage.fslink_storage.FSLinkStorage.get_data',
                side_effect=NotFound('not found')):
            response = self.client.get(
                file_url,
                query_string={'job_output_path': output_rel},
                headers=self.headers)

        self.assertEqual(response.status_code, 404)


class TestCopyWorkerExitOnFailure(TestCase):
    """
    Lock in the copy_worker.main() outer wrapper: when do_copy raises,
    the worker must exit with status 1 so the compute manager records
    finishedWithError and CUBE refuses to schedule the plugin job.
    Also confirm the upload/delete subcommands were intentionally not
    wrapped, so anyone who later 'fixes' them by adding the same wrapper
    has to update this test and consciously change the contract.
    """

    def test_copy_subcommand_exits_1_on_exception(self):
        with (
            mock.patch.object(cw, 'do_copy',
                              side_effect=RuntimeError('boom')),
            mock.patch('sys.argv',
                       ['copy_worker', 'copy', '/tmp/nonexistent']),
            self.assertRaises(SystemExit) as cm,
        ):
            cw.main()

        self.assertEqual(cm.exception.code, 1)

    def test_upload_subcommand_propagates_exception(self):
        # No try/except around upload; an unhandled exception bubbles up
        # as a plain Python error and produces a non-zero exit via the
        # interpreter's default behavior. If anyone wraps it later,
        # this test will fail and force the contract change to be
        # explicit.
        with (
            mock.patch.object(cw, 'do_upload',
                              side_effect=RuntimeError('boom')),
            mock.patch('sys.argv',
                       ['copy_worker', 'upload', '/tmp/nonexistent']),
            self.assertRaises(RuntimeError),
        ):
            cw.main()

    def test_delete_subcommand_propagates_exception(self):
        with (
            mock.patch.object(cw, 'do_delete',
                              side_effect=RuntimeError('boom')),
            mock.patch('sys.argv',
                       ['copy_worker', 'delete', '/tmp/nonexistent']),
            self.assertRaises(RuntimeError),
        ):
            cw.main()
