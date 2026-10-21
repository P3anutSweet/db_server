import runpy
from pathlib import Path
import unittest

INSTALL = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/install-lan-reader.py'))
FIXTURE = '''server {
    listen 192.168.32.84:443 ssl;
    ssl_certificate /etc/teable/tls/server.crt;
    allow 192.168.32.0/24;
    allow 192.168.35.131;
    deny all;
    client_max_body_size 100m;
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header X-Forwarded-Proto https;
    }
}
'''


class LanReaderTests(unittest.TestCase):
    def test_known_subnets_only_and_deny_retained(self):
        result = INSTALL['candidate'](FIXTURE)
        self.assertEqual(result.count('allow 192.168.35.0/24;'), 1)
        self.assertIn('allow 192.168.32.0/24;', result)
        self.assertEqual(result.count('deny all;'), 1)
        self.assertNotIn('allow all;', result)

    def test_rerun_has_no_duplicate_routes(self):
        result = INSTALL['candidate'](FIXTURE)
        self.assertEqual(INSTALL['candidate'](result), result)

    def test_tls_and_proxy_preserved(self):
        result = INSTALL['candidate'](FIXTURE)
        for line in FIXTURE.splitlines():
            if any(word in line for word in ('ssl_', 'proxy_', 'listen ')):
                self.assertIn(line, result)

    def test_missing_access_boundary_rejected(self):
        with self.assertRaises(ValueError):
            INSTALL['candidate'](FIXTURE.replace('    deny all;\n', ''))

    def test_existing_route_conflict_rejected(self):
        with self.assertRaises(ValueError):
            INSTALL['candidate'](FIXTURE.replace('    client_max_body_size',
                '    location = /read { return 403; }\n    client_max_body_size'))


if __name__ == '__main__':
    unittest.main()
