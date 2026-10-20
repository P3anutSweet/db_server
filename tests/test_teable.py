"""Deployment guards and export failure recovery; never touch live services."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load('teable_deploy', 'deploy-teable.py')
export = load('teable_export', 'teable-export.py')
guard = load('teable_guard', 'teable-mount-check.py')


class DeploymentSafety(unittest.TestCase):
    def test_rejects_wrong_host_before_commands(self):
        with mock.patch.object(deploy.os, 'geteuid', return_value=0), \
                mock.patch.object(deploy.socket, 'gethostname', return_value='different'), \
                mock.patch.object(deploy, 'run') as run:
            with self.assertRaises(RuntimeError):
                deploy.preflight()
            run.assert_not_called()

    def test_mount_guard_rejects_wrong_disk(self):
        with mock.patch.object(Path, 'read_text', return_value='{"db_uuid":"expected"}'), \
                mock.patch.object(guard.subprocess, 'check_output', return_value='other\n'):
            with self.assertRaises(RuntimeError):
                guard.require_mount()

    def test_secrets_are_correct_length_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(deploy, 'DEST', Path(temporary)):
            first = deploy.environment()
            self.assertEqual(len(first['STORAGE_KEY'].encode()), 16)
            self.assertEqual(len(first['STORAGE_IV'].encode()), 16)
            for name in deploy.ENCRYPTION_VALUES:
                self.assertEqual(len(first[name].encode()), 16)
            self.assertGreaterEqual(len(first['SECRET_KEY']), 32)
            deploy.write(Path(temporary) / '.env', ''.join(f'{k}={v}\n' for k, v in first.items()))
            self.assertEqual(deploy.environment(), first)

    def test_incomplete_existing_env_cannot_rotate_credentials(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(deploy, 'DEST', Path(temporary)):
            (Path(temporary) / '.env').write_text('POSTGRES_PASSWORD=keep\n')
            with self.assertRaises(RuntimeError):
                deploy.environment()
            self.assertEqual((Path(temporary) / '.env').read_text(), 'POSTGRES_PASSWORD=keep\n')


class ExportRecovery(unittest.TestCase):
    def test_partial_stop_failure_still_restarts_app(self):
        calls = []

        def command(*args, **kwargs):
            calls.append(args)
            if 'stop' in args:
                raise subprocess.CalledProcessError(1, ['docker', 'compose', 'stop'])
            return mock.Mock()

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(export, 'sql', return_value=''), mock.patch.object(export, 'run', side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError):
                export.create_bundle(Path(temporary))
        self.assertEqual(len(calls), 2)
        self.assertIn('up', calls[-1])
        self.assertIn('--wait', calls[-1])

    def test_foreign_restore_check_database_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(export, 'sql', return_value='1'), mock.patch.object(export, 'run') as run:
            with self.assertRaises(RuntimeError):
                export.create_bundle(Path(temporary))
            run.assert_not_called()

    def test_restore_failure_drops_temporary_database_and_restarts_app(self):
        calls = []

        def command(*args, **kwargs):
            calls.append(args)
            if 'pg_restore' in args:
                raise subprocess.CalledProcessError(1, ['pg_restore'])
            return mock.Mock()

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(export, 'sql', return_value=''), mock.patch.object(export, 'run', side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError):
                export.create_bundle(Path(temporary))
        self.assertTrue(any('dropdb' in args for args in calls))
        self.assertIn('up', calls[-1])
        self.assertIn('--wait', calls[-1])

    def test_success_records_actual_restore_and_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config'
            config.mkdir()
            (config / '.env').write_text('fixture=true\n')
            stage = root / 'stage'
            stage.mkdir()
            with mock.patch.object(export, 'ROOT', config), \
                    mock.patch.object(export, 'sql', side_effect=['', '42', '42']), \
                    mock.patch.object(export, 'run'):
                manifest = export.create_bundle(stage)
            self.assertEqual(manifest['source_tables'], 42)
            self.assertEqual(manifest['restored_tables'], 42)
            self.assertIn('succeeded', manifest['restore_test'])
            self.assertTrue((stage / 'deployment/.env').is_file())


if __name__ == '__main__':
    unittest.main()
