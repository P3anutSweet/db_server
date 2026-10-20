#!/usr/bin/env python3
"""Prevent starting persistent containers against the root disk after a lost mount."""
import json
from pathlib import Path
import subprocess


def require_mount():
    config = json.loads(Path('/etc/db-server/storage.json').read_text())
    uuid = subprocess.check_output(
        ['findmnt', '-rn', '--mountpoint', '/srv/db', '-o', 'UUID'], text=True).strip()
    if not uuid or uuid != config['db_uuid']:
        raise RuntimeError('Expected database filesystem is not mounted; refusing Teable start')


if __name__ == '__main__':
    require_mount()
