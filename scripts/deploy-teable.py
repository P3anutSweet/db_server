#!/usr/bin/env python3
"""Install the LAN Teable community stack on the already-provisioned server."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
DEST = Path('/etc/teable')
STATUS = Path('/var/lib/db-server/teable-status.json')
IP = '192.168.32.84'
IMAGES = {'TEABLE_IMAGE': 'ghcr.io/teableio/teable-community:latest',
          'POSTGRES_IMAGE': 'postgres:15-bookworm', 'REDIS_IMAGE': 'redis:7.4-bookworm'}
ENCRYPTION_VALUES = ('STORAGE_KEY', 'STORAGE_IV', 'MAIL_KEY', 'MAIL_IV',
                     'ACCESS_TOKEN_KEY', 'ACCESS_TOKEN_IV', 'DATA_DB_KEY', 'DATA_DB_IV')
EXPECTED_UUIDS = {'db_uuid': '8500b26b-de65-4291-a5eb-c1874801abf9',
                  'backup_uuid': 'bd58d26a-ec6b-47e5-91d4-69169e4738fb'}


def run(*args, capture=False):
    # Never print the generated environment or a rendered compose configuration.
    return subprocess.run(args, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.partial')
    with temporary.open('w', encoding='utf-8') as handle:
        os.chmod(temporary, mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def compose(*args, capture=False):
    return run('docker', 'compose', '--project-directory', str(DEST), '--env-file', str(DEST / '.env'),
               '-f', str(DEST / 'compose.yaml'), *args, capture=capture)


def preflight():
    if os.geteuid() != 0:
        raise RuntimeError('Run this installer using sudo at the Ubuntu terminal')
    if socket.gethostname() != 'thunderobotdb':
        raise RuntimeError('Installer is restricted to thunderobotdb')
    storage = json.loads(Path('/etc/db-server/storage.json').read_text())
    if any(storage.get(key) != value for key, value in EXPECTED_UUIDS.items()):
        raise RuntimeError('Storage configuration does not match the approved database and WDC UUIDs')
    for mount, key in [('/srv/db', 'db_uuid'), ('/srv/backups', 'backup_uuid')]:
        actual = run('findmnt', '-rn', '--mountpoint', mount, '-o', 'UUID', capture=True).strip()
        if not actual or actual != storage[key]:
            raise RuntimeError(f'Expected filesystem missing at {mount}')
    interfaces = json.loads(run('ip', '-j', '-4', 'addr', capture=True))
    if not any(a.get('local') == IP for interface in interfaces for a in interface.get('addr_info', [])):
        raise RuntimeError('Expected LAN IP is absent')
    for path in [DEST, Path('/srv/db/teable')]:
        if path.is_symlink():
            raise RuntimeError(f'Refusing symlink target: {path}')
    default_site = Path('/etc/nginx/sites-enabled/default')
    if (default_site.exists() or default_site.is_symlink()) and not (
            default_site.is_symlink() and default_site.resolve() == Path('/etc/nginx/sites-available/default')):
        raise RuntimeError('Unmanaged Nginx default site exists; inspect before deployment')
    existing = DEST / '.env'
    if not existing.exists():
        # Bind without listening to reject another service before package/config changes.
        for address, port in [(IP, 80), ('127.0.0.1', 3000)]:
            with socket.socket() as sock:
                sock.bind((address, port))
        if Path('/srv/db/teable').exists():
            raise RuntimeError('Unmanaged Teable data directory exists; inspect before deployment')
    return storage


def environment():
    path = DEST / '.env'
    if path.exists():
        values = dict(line.split('=', 1) for line in path.read_text().splitlines() if line and not line.startswith('#'))
        for name in [*IMAGES, 'POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'SECRET_KEY', *ENCRYPTION_VALUES]:
            if not values.get(name):
                raise RuntimeError(f'Existing environment is missing {name}; refusing secret rotation')
        return values
    return {**IMAGES, 'POSTGRES_PASSWORD': secrets.token_hex(32), 'REDIS_PASSWORD': secrets.token_hex(32),
            'SECRET_KEY': secrets.token_hex(32),
            **{name: secrets.token_hex(8) for name in ENCRYPTION_VALUES}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Install packages and deploy the stack')
    args = parser.parse_args()
    if not args.apply:
        print('Prepared target: thunderobotdb, http://192.168.32.84, data /srv/db/teable')
        print('Use sudo python3 scripts/deploy-teable.py --apply to deploy')
        return
    os.umask(0o077)
    storage = preflight()
    print('Preflight passed; installing Docker, Compose and the LAN reverse proxy.', flush=True)
    run('apt-get', 'update')
    run('env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y',
        'docker.io', 'docker-compose-v2', 'nginx', 'ca-certificates')
    default_site = Path('/etc/nginx/sites-enabled/default')
    if default_site.is_symlink() and default_site.resolve() == Path('/etc/nginx/sites-available/default'):
        # Preserve the packaged source; disable its wildcard port-80 listener only.
        default_site.unlink()
    for filename, destination in [
        ('teable-mount-check.py', '/usr/local/sbin/teable-mount-check'),
        ('teable-export.py', '/usr/local/sbin/teable-export'),
        ('backup-server.py', '/usr/local/sbin/db-server-backup')]:
        write(Path(destination), (SOURCE / 'scripts' / filename).read_text(), 0o750)
    # Docker's restart policies run before teable.service at boot, so guard Docker too.
    write(Path('/etc/systemd/system/docker.service.d/db-storage.conf'),
          '[Unit]\nRequiresMountsFor=/srv/db\n\n[Service]\nExecStartPre=/usr/bin/python3 /usr/local/sbin/teable-mount-check\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'docker')
    DEST.mkdir(mode=0o700, exist_ok=True)
    values = environment()
    for name in IMAGES:
        print(f'Pulling {name} and locking its immutable digest.', flush=True)
        run('docker', 'pull', values[name])
        digests = json.loads(run('docker', 'image', 'inspect', values[name], '--format', '{{json .RepoDigests}}', capture=True))
        if not digests:
            raise RuntimeError(f'Cannot resolve immutable digest for {name}')
        values[name] = digests[0]
    write(DEST / '.env', ''.join(f'{key}={value}\n' for key, value in values.items()))
    write(DEST / 'compose.yaml', (SOURCE / 'deploy/teable/compose.yaml').read_text())
    base = Path('/srv/db/teable')
    base.mkdir(mode=0o711, exist_ok=True)
    for folder, uid, gid in [('postgres', 999, 999), ('redis', 999, 999), ('assets', 1001, 1001)]:
        path = base / folder
        if path.is_symlink():
            raise RuntimeError(f'Refusing symlink data directory: {path}')
        if not path.exists():
            path.mkdir(mode=0o700)
            os.chown(path, uid, gid)
    compose('config', '--quiet')
    write(Path('/etc/systemd/system/teable.service'), (SOURCE / 'systemd/teable.service').read_text())
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'teable.service')
    run('systemctl', 'restart', 'teable.service')
    write(Path('/etc/nginx/conf.d/teable.conf'), (SOURCE / 'deploy/teable/nginx.conf').read_text(), 0o644)
    run('nginx', '-t')
    run('systemctl', 'enable', '--now', 'nginx')
    # Releasing the packaged wildcard listener needs a restart: reload can retain
    # :80 and fail to bind the new LAN-specific listener while reporting success.
    run('systemctl', 'restart', 'nginx')
    run('ufw', 'allow', 'from', '192.168.32.0/24', 'to', IP, 'port', '80', 'proto', 'tcp', 'comment', 'Teable LAN')
    write(Path('/etc/systemd/system/db-server-backup.service.d/teable.conf'),
          '[Unit]\nRequires=teable.service\nAfter=teable.service\n\n[Service]\nExecStartPre=/usr/bin/python3 /usr/local/sbin/teable-export\n')
    storage['database_engine'] = 'PostgreSQL 15 (Teable community)'
    storage['database_recovery_status'] = 'Consistent Teable export with isolated pg_restore validation; full application recovery rehearsal pending'
    write(Path('/etc/db-server/storage.json'), json.dumps(storage, indent=2) + '\n')
    run('systemctl', 'daemon-reload')
    print('Running the first consistent export, isolated restore test and WDC backup.', flush=True)
    run('systemctl', 'start', 'db-server-backup.service')
    health = urllib.request.urlopen(f'http://{IP}/health', timeout=15).status
    if health != 200:
        raise RuntimeError('LAN HTTP health check failed')
    report = {'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'url': f'http://{IP}', 'edition': 'Community', 'images': {k: values[k] for k in IMAGES},
              'data': '/srv/db/teable', 'health_http_status': health,
              'backup': 'db-server-backup.timer; Teable export and isolated pg_restore test before every backup',
              'schema_import': 'Windows chat is drafting the schema from the new Excel inputs; no business schema imported',
              'account': 'User must register their own first account in the browser',
              'limitations': 'LAN HTTP; browser secure-context clipboard features may require HTTPS; full application recovery rehearsal pending'}
    write(STATUS, json.dumps(report, indent=2, ensure_ascii=False) + '\n', 0o644)
    print(f'Teable deployment verified: http://{IP}', flush=True)
    print('Register your own first account. Application passwords are never stored in Git or printed here.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Deployment stopped: {error}', file=sys.stderr)
        sys.exit(1)
