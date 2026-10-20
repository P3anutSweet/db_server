"""Safety checks run entirely against temporary directories and mocked inventory."""
import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


setup = module("storage_setup", "setup-storage.py")
backup = module("server_backup", "backup-server.py")


class StorageSafety(unittest.TestCase):
    def test_fstab_preserves_root_and_swap_and_is_repeatable(self):
        original = "# original\nUUID=root / ext4 defaults 0 1\n/swap.img none swap sw 0 0\n"
        first = setup.render_fstab(original, "database", "backup")
        self.assertIn("UUID=root / ext4 defaults 0 1", first)
        self.assertIn("/swap.img none swap sw 0 0", first)
        self.assertEqual(first, setup.render_fstab(first, "database", "backup"))
        self.assertNotIn("nofail", next(s for s in first.splitlines() if " /srv/db " in s))

    def test_unmanaged_mount_and_damaged_markers_are_rejected(self):
        for original in ("UUID=other /srv/db ext4 defaults 0 2\n", setup.BEGIN,
                         setup.END + "\n" + setup.BEGIN):
            with self.subTest(original=original), self.assertRaises(RuntimeError):
                setup.render_fstab(original, "database", "backup")

    def test_mountpoint_with_existing_files_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "keep-me").write_text("data")
            with mock.patch.object(setup, "mount_uuid", return_value=None):
                with self.assertRaises(RuntimeError):
                    setup.check_mount_target(directory, None)
            self.assertEqual(Path(directory, "keep-me").read_text(), "data")

    def test_wrong_mount_uuid_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(setup, "mount_uuid", return_value="wrong"):
                with self.assertRaises(RuntimeError):
                    setup.check_mount_target(directory, "expected")

    def test_wrong_backup_disk_aborts_before_any_lvm_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            disk = {"path": "/dev/sda", "type": "disk", "serial": "ANOTHER-DISK", "size": setup.BACKUP_BYTES}
            with (mock.patch.object(setup.os, "geteuid", return_value=0, create=True),
                  mock.patch.object(setup.socket, "gethostname", return_value="thunderobotdb"),
                  mock.patch.object(setup.shutil, "which", return_value="/usr/bin/fixture"),
                  mock.patch.object(setup, "CONFIG", Path(directory) / "absent.json"),
                  mock.patch.object(setup, "inventory", return_value={"blockdevices": [disk]}),
                  mock.patch.object(setup, "lvm") as lvm,
                  mock.patch.object(setup, "run") as run):
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    setup.preflight()
                lvm.assert_not_called()
                run.assert_not_called()

    def test_rejects_apply_without_exact_confirmation_before_preflight(self):
        for args in (["setup-storage.py", "--apply"],
                     ["setup-storage.py", "--apply", "--erase-backup", "WRONG"]):
            with mock.patch.object(setup.sys, "argv", args), mock.patch.object(setup, "preflight") as preflight:
                with self.assertRaises(RuntimeError):
                    setup.main()
                preflight.assert_not_called()


class BackupSafety(unittest.TestCase):
    def test_wrong_or_absent_backup_mount_fails_closed(self):
        with mock.patch.object(backup, "run", return_value="WRONG"):
            with self.assertRaises(RuntimeError):
                backup.require_mount("/srv/backups", "EXPECTED")

    def test_retention_preserves_partial_foreign_and_newest_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = []
            for index in range(1, 5):
                path = root / f"backup-2026102{index}T000000.000000Z"
                path.mkdir()
                (path / "COMPLETE").write_text("done")
                completed.append(path)
            partial = root / ".partial-important"
            foreign = root / "keep-other-data"
            unmarked = root / "backup-20260101T000000.000000Z"
            for path in (partial, foreign, unmarked):
                path.mkdir()
            backup.prune(root, 2)
            self.assertFalse(completed[0].exists())
            self.assertFalse(completed[1].exists())
            self.assertTrue(all(p.exists() for p in (*completed[2:], partial, foreign, unmarked)))

    def test_invalid_retention_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "backup-20261020T000000.000000Z"
            path.mkdir()
            (path / "COMPLETE").write_text("done")
            with self.assertRaises(RuntimeError):
                backup.prune(root, 0)
            self.assertTrue(path.exists())

    def test_failed_archive_keeps_previous_backups_and_removes_partial(self):
        # main() is Linux-specific (flock); this test runs on the Ubuntu server/CI.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "storage.json"
            config.write_text(json.dumps({"db_uuid": "db", "backup_uuid": "backup"}))
            destination = root / "backups"
            destination.mkdir()
            previous = destination / "backup-20261019T000000.000000Z"
            previous.mkdir()
            (previous / "COMPLETE").write_text("done")
            previous_umask = backup.os.umask(0o077)
            try:
                with (mock.patch.object(backup.os, "geteuid", return_value=0, create=True),
                      mock.patch.object(backup, "CONFIG", config),
                      mock.patch.object(backup, "DEST", destination),
                      mock.patch.object(backup, "LOCK", root / "backup.lock"),
                      mock.patch.object(backup, "require_mount"),
                      mock.patch.object(backup.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024 ** 3)),
                      mock.patch.object(backup, "archive", side_effect=RuntimeError("archive failed")),
                      mock.patch.object(backup, "prune") as prune):
                    with self.assertRaises(RuntimeError):
                        backup.main()
                    prune.assert_not_called()
                    self.assertTrue(previous.exists())
                    self.assertEqual(list(destination.glob(".partial-*")), [])
            finally:
                backup.os.umask(previous_umask)

    def test_successful_backup_contains_only_completed_exports_and_valid_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "storage.json"
            config.write_text(json.dumps({"db_uuid": "db", "backup_uuid": "backup"}))
            destination = root / "backups"
            destination.mkdir()
            exports = root / "exports"
            exports.mkdir()
            (exports / "complete.sql").write_text("SELECT 1;")
            (exports / "in-progress.sql.partial").write_text("unfinished")
            (exports / ".hidden.sql").write_text("hidden")
            etc = root / "etc"
            etc.mkdir()
            (etc / "hostname").write_text("fixture-server")
            real_run = backup.run
            real_subprocess_run = backup.subprocess.run

            def fake_run(*args):
                return real_run(*args) if args[0] == "tar" else "fixture inventory\n"

            def fake_subprocess(args, **kwargs):
                if args[0] == "journalctl":
                    return backup.subprocess.CompletedProcess(args, 0, "fixture journal\n", "")
                return real_subprocess_run(args, **kwargs)

            previous_umask = backup.os.umask(0o077)
            try:
                with (mock.patch.object(backup.os, "geteuid", return_value=0, create=True),
                      mock.patch.object(backup.os, "sync", create=True),
                      mock.patch.object(backup, "CONFIG", config),
                      mock.patch.object(backup, "CONFIG_PATHS", [etc]),
                      mock.patch.object(backup, "EXPORTS", exports),
                      mock.patch.object(backup, "DEST", destination),
                      mock.patch.object(backup, "LOCK", root / "backup.lock"),
                      mock.patch.object(backup, "require_mount"),
                      mock.patch.object(backup.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024 ** 3)),
                      mock.patch.object(backup, "run", side_effect=fake_run),
                      mock.patch.object(backup.subprocess, "run", side_effect=fake_subprocess)):
                    backup.main()
                completed = backup.completed_backups(destination)
                self.assertEqual(len(completed), 1)
                with tarfile.open(completed[0] / "database-exports.tar.gz") as archive:
                    names = archive.getnames()
                self.assertTrue(any(name.endswith("/complete.sql") for name in names))
                self.assertFalse(any("partial" in name or ".hidden.sql" in name for name in names))
                for line in (completed[0] / "SHA256SUMS").read_text().splitlines():
                    digest, name = line.split("  ", 1)
                    self.assertEqual(hashlib.sha256((completed[0] / name).read_bytes()).hexdigest(), digest)
                manifest = json.loads((completed[0] / "manifest.json").read_text())
                self.assertEqual(manifest["completed_export_files"], ["complete.sql"])
                self.assertEqual(list(destination.glob(".partial-*")), [])
            finally:
                backup.os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
