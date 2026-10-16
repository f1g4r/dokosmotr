"""Installer network modes preserve data and avoid executing dotenv contents."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ['install.sh', '.env.example']:
            shutil.copy(ROOT / name, self.root / name)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        docker.write_text('''#!/bin/sh
case " $* " in
  *" port "*)
    bind=$(awk -F= '$1 == "BIND_ADDRESS" {print $2}' .env)
    port=$(awk -F= '$1 == "PORT" {print $2}' .env)
    printf '%s:%s\\n' "$bind" "$port" ;;
esac
exit 0
''')
        docker.chmod(0o755)
        # A fresh-install group lookup must not require a real Docker socket in unit tests.
        stat = bin_dir / 'stat'
        stat.write_text('#!/bin/sh\necho 999\n')
        stat.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}

    def run_install(self, *args):
        return subprocess.run(['bash', 'install.sh', *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, check=True).stdout

    def test_new_install_defaults_to_network(self):
        output = self.run_install()
        config = (self.root / '.env').read_text()
        self.assertIn('BIND_ADDRESS=0.0.0.0', config)
        self.assertIn('PUBLIC_ORIGIN=\n', config)
        self.assertIn('IP-СЕРВЕРА:5080', output)

    def test_lan_migration_keeps_custom_port_and_other_settings(self):
        (self.root / '.env').write_text('BIND_ADDRESS=127.0.0.1\nPUBLIC_ORIGIN=http://localhost:9000\nCOOKIE_SECURE=true\nPORT=9000\nREAD_ONLY=false\nSERVER_LABEL=$(touch should-not-exist)\n')
        self.run_install('--lan')
        config = (self.root / '.env').read_text()
        for expected in ['PORT=9000', 'READ_ONLY=false', 'BIND_ADDRESS=0.0.0.0', 'PUBLIC_ORIGIN=\n', 'COOKIE_SECURE=false']:
            self.assertIn(expected, config)
        self.assertFalse((self.root / 'should-not-exist').exists())

    def test_local_opt_in_and_plain_update_preserves_it(self):
        self.run_install('--local')
        before = (self.root / '.env').read_text()
        self.assertIn('BIND_ADDRESS=127.0.0.1', before)
        self.run_install()
        self.assertEqual((self.root / '.env').read_text(), before)
