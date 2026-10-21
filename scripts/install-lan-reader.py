#!/usr/bin/env python3
"""Install the prepared LAN reader and allow the two known company subnets.

Default validates and prints the Nginx diff. --apply requires sudo; backs up the
configuration and restores it if validation/reload fails. Never changes TLS,
Teable roles, Docker ports, or the business database.
"""
import argparse
import datetime
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

CONFIG = Path('/etc/nginx/conf.d/teable.conf')
SOURCE = Path('/home/jjj/db_server_setup/lan-reader-20261021/index.html')
TARGET = Path('/var/www/teable-read/index.html')
IP = '192.168.32.84'
MARKER = '    # Company LAN read-only entry\n'
BLOCK = MARKER + '''    location = / { return 302 /read; }
    location = /read {
        alias /var/www/teable-read/index.html;
        default_type text/html;
        add_header Cache-Control "no-store" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
    }
    location = /read/ { return 302 /read; }
'''


def candidate(before):
    if 'listen 192.168.32.84:443 ssl;' not in before or 'ssl_certificate ' not in before:
        raise ValueError('Expected the existing LAN HTTPS configuration.')
    if before.count('    deny all;\n') != 1 or '    allow 192.168.32.0/24;\n' not in before:
        raise ValueError('Unexpected access rules; refusing to guess.')
    after = before.replace('    allow 192.168.35.131;\n', '')
    if '    allow 192.168.35.0/24;\n' not in after:
        after = after.replace('    deny all;\n', '    allow 192.168.35.0/24;\n    deny all;\n')
    if MARKER not in after:
        anchor = '    client_max_body_size 100m;\n'
        if after.count(anchor) != 1 or re.search(r'location\s*=\s*/(?:read/?)?\s*\{', after):
            raise ValueError('Unexpected exact root/read route; review before changing.')
        after = after.replace(anchor, anchor + BLOCK)
    elif BLOCK not in after:
        raise ValueError('Existing LAN reader block differs; no changes made.')
    return after


def run(*args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if CONFIG.is_symlink() or TARGET.is_symlink() or TARGET.parent.is_symlink():
        raise SystemExit('Refusing symlink configuration or output.')
    before = CONFIG.read_text()
    after = candidate(before)
    source = SOURCE.read_bytes()
    if b'<iframe ' not in source or b'Bearer ' in source or b'pat_' in source:
        raise SystemExit('Missing or unexpected reader HTML; no changes made.')
    state = json.loads(SOURCE.with_name('reader-state-private.json').read_text())
    if len(state['views']) != 7 or not all(v['shareId'].startswith('shr') for v in state['views']):
        raise SystemExit('Expected seven verified read-only views.')
    if not args.apply:
        print(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                         fromfile=str(CONFIG), tofile='proposed')))
        print('Apply also installs the token-free HTML and adds UFW TCP80/443 rules for 192.168.35.0/24.')
        return
    if os.geteuid() != 0:
        raise SystemExit('Run sudo python3 /home/jjj/db_server_setup/install-lan-reader.py --apply')
    backup = Path('/etc/teable/config-backups') / ('lan-reader-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(mode=0o700, parents=True)
    shutil.copy2(CONFIG, backup / 'teable.conf')
    old_html = TARGET.read_bytes() if TARGET.exists() else None
    if old_html is not None:
        (backup / 'index.html').write_bytes(old_html)
    try:
        TARGET.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        TARGET.write_bytes(source)
        TARGET.chmod(0o644)
        CONFIG.write_text(after)
        run('/usr/sbin/nginx', '-t')
        run('systemctl', 'reload', 'nginx')
    except BaseException:
        CONFIG.write_text(before)
        if old_html is None:
            TARGET.unlink(missing_ok=True)
        else:
            TARGET.write_bytes(old_html)
        subprocess.run(['/usr/sbin/nginx', '-t'], check=False)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=False)
        raise
    # Keep these rules narrow; loopback-only Docker publication stays untouched.
    # A firewall failure leaves the validated HTTPS site intact and is reported.
    for port in ('80', '443'):
        run('/usr/sbin/ufw', 'allow', 'from', '192.168.35.0/24', 'to', IP,
            'port', port, 'proto', 'tcp', 'comment', 'Teable company LAN')
    print('Installed: https://192.168.32.84/read ; anonymous read-only, company subnets 32/24 and35/24.')
    print('Nginx backup: ' + str(backup))


if __name__ == '__main__':
    main()
