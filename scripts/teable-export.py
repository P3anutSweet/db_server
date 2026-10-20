#!/usr/bin/env python3
"""Quiesce Teable, dump PostgreSQL, test restore, and atomically publish its backup bundle."""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path('/etc/teable')
EXPORT = Path('/srv/db/exports/teable-latest.tar.gz')
COMPOSE = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(ROOT / '.env'),
           '-f', str(ROOT / 'compose.yaml')]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sql(database, statement):
    return run(*COMPOSE, 'exec', '-T', 'db', 'psql', '-U', 'teable', '-d', database,
               '-At', '-v', 'ON_ERROR_STOP=1', '-c', statement,
               capture_output=True, text=True).stdout.strip()


def mount_check():
    config = json.loads(Path('/etc/db-server/storage.json').read_text())
    for mount, key in [('/srv/db', 'db_uuid'), ('/srv/backups', 'backup_uuid')]:
        actual = subprocess.check_output(['findmnt', '-rn', '--mountpoint', mount, '-o', 'UUID'], text=True).strip()
        if not actual or actual != config[key]:
            raise RuntimeError(f'Expected filesystem is not mounted at {mount}')


def create_bundle(stage):
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    database = 'teable_restore_check'
    table_query = "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"
    # This database name is reserved for our isolated restore validation.
    if sql('postgres', f"SELECT 1 FROM pg_database WHERE datname='{database}'"):
        raise RuntimeError('Restore-check database already exists; inspect it before retrying')
    created = False
    try:
        # Even a failed/partially completed stop must reach the restart in finally.
        run(*COMPOSE, 'stop', '-t', '45', 'app')
        with (stage / 'teable.dump').open('wb') as dump:
            run(*COMPOSE, 'exec', '-T', 'db', 'pg_dump', '-U', 'teable', '-d', 'teable', '-Fc', stdout=dump)
        with (stage / 'globals.sql').open('wb') as globals_file:
            run(*COMPOSE, 'exec', '-T', 'db', 'pg_dumpall', '-U', 'teable', '--globals-only', stdout=globals_file)
        run(*COMPOSE, 'exec', '-T', 'db', 'createdb', '-U', 'teable', database)
        created = True
        with (stage / 'teable.dump').open('rb') as dump:
            run(*COMPOSE, 'exec', '-T', 'db', 'pg_restore', '-U', 'teable', '-d', database,
                '--exit-on-error', '--no-owner', '--no-privileges', stdin=dump)
        source_tables = int(sql('teable', table_query))
        restored_tables = int(sql(database, table_query))
        if source_tables < 1 or source_tables != restored_tables:
            raise RuntimeError('Restored database table count does not match the source')
        run('tar', '-czf', str(stage / 'assets.tar.gz'), '-C', '/srv/db/teable/assets', '.')
        shutil.copytree(ROOT, stage / 'deployment')
        manifest = {'created_at': stamp, 'database': 'teable', 'format': 'pg_dump custom',
                    'restore_test': 'pg_restore into isolated temporary database succeeded',
                    'source_tables': source_tables, 'restored_tables': restored_tables,
                    'consistency': 'App stopped during dump and attachment archive; database ports are not published',
                    'scope': 'PostgreSQL dump, global roles, local attachments, compose and secret configuration',
                    'limitations': 'Not a full application-level disaster recovery rehearsal; Redis sessions/cache and queued jobs are not backed up'}
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    finally:
        try:
            if created:
                run(*COMPOSE, 'exec', '-T', 'db', 'dropdb', '-U', 'teable', database)
        finally:
            run(*COMPOSE, 'up', '-d', '--wait', '--wait-timeout', '300', 'app')
    return manifest


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run as root')
    os.umask(0o077)
    with open('/run/lock/teable-export.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mount_check()
        if shutil.disk_usage(EXPORT.parent).free < 10 * 1024 ** 3:
            raise RuntimeError('Less than 10GiB database disk free; previous export preserved')
        with tempfile.TemporaryDirectory(prefix='.teable-export-', dir=EXPORT.parent) as temporary:
            stage = Path(temporary)
            manifest = create_bundle(stage)
            bundle = stage / 'bundle.partial'
            contents = [p.name for p in stage.iterdir()]
            run('tar', '-czf', str(bundle), '-C', str(stage), *contents)
            run('tar', '-tzf', str(bundle), stdout=subprocess.DEVNULL)
            with bundle.open('rb') as handle:
                os.fsync(handle.fileno())
                manifest['sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
            bundle.replace(EXPORT)
            # The existing root-only backup service archives this completed file next.
            report = Path('/var/lib/db-server/teable-export-status.json')
            report.write_text(json.dumps(manifest, indent=2) + '\n')
            report.chmod(0o644)
            print('Teable export and isolated database restore test succeeded')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'Teable export failed: {error}', file=sys.stderr)
        sys.exit(1)
