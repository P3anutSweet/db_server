#!/usr/bin/env python3
"""Validate the deployment with dummy values; never read the live secret environment."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-binary', help='Standalone Compose executable, if Docker is not installed')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(TEABLE_IMAGE='ghcr.io/teableio/teable-community:latest',
               POSTGRES_IMAGE='postgres:15-bookworm', REDIS_IMAGE='redis:7.4-bookworm',
               POSTGRES_PASSWORD='validation-only', REDIS_PASSWORD='validation-only', SECRET_KEY='validation-only')
    for key in ('STORAGE_KEY', 'STORAGE_IV', 'MAIL_KEY', 'MAIL_IV', 'ACCESS_TOKEN_KEY',
                'ACCESS_TOKEN_IV', 'DATA_DB_KEY', 'DATA_DB_IV'):
        env[key] = '0123456789abcdef'
    command = [args.compose_binary] if args.compose_binary else ['docker', 'compose']
    # Disable implicit .env discovery; all interpolation uses the explicit dummy environment.
    command += ['--env-file', '/dev/null', '-f', str(root / 'deploy/teable/compose.yaml'), 'config']
    subprocess.run([*command, '--quiet'], env=env, check=True)
    result = subprocess.run([*command, '--format', 'json'], env=env, check=True, text=True, capture_output=True)
    services = json.loads(result.stdout)['services']
    if set(services) != {'app', 'db', 'cache'}:
        raise RuntimeError('Unexpected services')
    ports = services['app'].get('ports', [])
    if len(ports) != 1 or ports[0]['host_ip'] != '127.0.0.1' or str(ports[0]['published']) != '3000' or ports[0]['target'] != 3000:
        raise RuntimeError('App must publish only 127.0.0.1:3000')
    for name, directory, target in [('app', 'assets', '/app/.assets'),
                                     ('db', 'postgres', '/var/lib/postgresql/data'), ('cache', 'redis', '/data')]:
        service = services[name]
        if name != 'app' and service.get('ports'):
            raise RuntimeError('Database/cache ports must not be published')
        volumes = service.get('volumes', [])
        if len(volumes) != 1 or volumes[0].get('type') != 'bind' or volumes[0]['source'] != f'/srv/db/teable/{directory}' or volumes[0]['target'] != target:
            raise RuntimeError('Unexpected persistent storage path')
        # Compose omits false-valued defaults from canonical JSON.
        if volumes[0].get('bind', {}).get('create_host_path', False):
            raise RuntimeError('Persistent mount paths must not be auto-created')
        if not service.get('healthcheck'):
            raise RuntimeError('Every service requires a health check')
    print('Compose validation passed: Community + PG15 + Redis7.4; loopback app only; DB/cache unpublished; guarded bind mounts.')


if __name__ == '__main__':
    main()
