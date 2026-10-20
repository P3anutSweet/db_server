#!/usr/bin/env python3
"""Archive configuration, recent journal and completed database exports to the WDC disk."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

CONFIG = Path("/etc/db-server/storage.json")
DEST = Path("/srv/backups/db-server")
EXPORTS = Path("/srv/db/exports")
LOCK = Path("/run/lock/db-server-backup.lock")
CONFIG_PATHS = ["/etc", "/usr/local/sbin/db-server-backup"]
NAME = re.compile(r"^backup-\d{8}T\d{6}\.\d{6}Z$")


def run(*args):
    return subprocess.run(args, text=True, check=True, capture_output=True).stdout


def require_mount(path, uuid):
    actual = run("findmnt", "-rn", "--mountpoint", path, "-o", "UUID").strip()
    if not uuid or actual != uuid:
        raise RuntimeError(f"Expected filesystem is not mounted at {path}; refusing backup")


def archive(destination, paths, exclude=()):
    args = ["tar", "--create", "--gzip", "--file", str(destination), "--directory", "/"]
    args += [f"--exclude={pattern}" for pattern in exclude]
    args += [str(path).lstrip("/") for path in paths]
    run(*args)
    # Read the entire compressed archive to detect truncation and invalid tar headers.
    run("tar", "--list", "--gzip", "--file", str(destination))


def completed_backups(root):
    return sorted(p for p in root.iterdir() if NAME.fullmatch(p.name) and not p.is_symlink()
                  and p.is_dir() and (p / "COMPLETE").is_file())


def prune(root, keep):
    if keep < 1:
        raise RuntimeError("Retention must keep at least one completed backup")
    for old in completed_backups(root)[:-keep]:
        # Only our named, marked, completed directories directly inside this verified root.
        if old.parent != root or old.resolve().parent != root.resolve():
            raise RuntimeError("Unexpected retention path")
        shutil.rmtree(old)


def main():
    import fcntl
    if os.geteuid() != 0:
        raise RuntimeError("Run as root")
    os.umask(0o077)
    with LOCK.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads(CONFIG.read_text())
        require_mount("/srv/db", config["db_uuid"])
        require_mount("/srv/backups", config["backup_uuid"])
        if DEST.is_symlink() or not DEST.is_dir():
            raise RuntimeError("Backup destination must be the managed directory")
        if shutil.disk_usage(DEST).free < 5 * 1024 ** 3:
            raise RuntimeError("Backup disk has less than 5GiB free; previous backups were preserved")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        stage = DEST / (".partial-" + stamp)
        stage.mkdir(mode=0o700)
        try:
            archive(stage / "configuration.tar.gz", CONFIG_PATHS)
            exports = EXPORTS
            archive(stage / "database-exports.tar.gz", [exports],
                    exclude=("*.partial", "*.tmp", ".*"))
            journal = subprocess.run(["journalctl", "--since", "24 hours ago", "--no-pager", "-o", "short-iso"],
                                     text=True, check=True, capture_output=True).stdout
            (stage / "journal-last-24h.txt").write_text(journal, encoding="utf-8")
            (stage / "packages.txt").write_text(run("dpkg-query", "-W"), encoding="utf-8")
            (stage / "storage.txt").write_text(run("lsblk", "-e", "7", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS")
                                               + run("df", "-hT", "/srv/db", "/srv/backups"), encoding="utf-8")
            exported = [p.name for p in exports.iterdir() if p.is_file() and not p.is_symlink()
                        and not p.name.startswith(".") and not p.name.endswith((".partial", ".tmp"))]
            manifest = {"created_at": stamp, "database_engine": config.get("database_engine", "not_configured"),
                        "completed_export_files": exported,
                        "database_recovery_status": config.get("database_recovery_status", "No database dump producer or restore test is configured yet"),
                        "scope": "System configuration, package list, last 24h journal, and completed files in /srv/db/exports"}
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            hashes = []
            for path in sorted(stage.iterdir()):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 ** 2), b""):
                        digest.update(block)
                hashes.append(f"{digest.hexdigest()}  {path.name}\n")
            (stage / "SHA256SUMS").write_text("".join(hashes), encoding="utf-8")
            (stage / "COMPLETE").write_text(stamp + "\n", encoding="utf-8")
            os.sync()
            completed = DEST / ("backup-" + stamp)
            stage.rename(completed)
            prune(DEST, int(config.get("retain_backups", 7)))
            print(f"Backup completed: {completed}")
            print(f"Completed database export files: {len(exported)}")
            print(manifest["database_recovery_status"])
        except BaseException:
            # A failed run cannot become a completed backup or trigger retention deletion.
            if stage.exists() and stage.parent == DEST and stage.name == ".partial-" + stamp:
                shutil.rmtree(stage)
            raise


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Backup failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
