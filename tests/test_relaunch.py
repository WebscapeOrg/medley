import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from medley import core


class RelaunchTests(unittest.TestCase):
    def test_authenticated_relaunch_reloads_server_and_preserves_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_herdr = root / 'herdr'
            fake_herdr.write_text('#!' + sys.executable + '\nprint(\'{"result":{"workspaces":[]}}\')\n')
            fake_herdr.chmod(0o700)
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'enabled': True, 'rules': {'example': {'done': 'none'}}}))
            original = settings.read_bytes()
            config = root / 'herdr.toml'
            config.write_text('[ui.sound]\nenabled=false\n')
            environment = dict(os.environ, HERDR_PLUGIN_CONFIG_DIR=directory,
                               HERDR_CONFIG_PATH=str(config), HERDR_BIN_PATH=str(fake_herdr))
            environment.pop('MEDLEY_RELAUNCH_TOKEN', None)
            process = subprocess.Popen([sys.executable, str(core.ROOT / 'medley.py'), 'settings', '--no-browser', '--port', '0'],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = queue.Queue()
            def collect():
                for line in process.stdout:
                    if line.startswith('Medley settings: '):
                        lines.put(line.strip().split('Medley settings: ', 1)[1])
            threading.Thread(target=collect, daemon=True).start()
            try:
                url = lines.get(timeout=10)
                base, token = url.split('/#')
                def request(path, authorized=True, body=None):
                    headers = {'X-Medley-Token': token} if authorized else {}
                    data = json.dumps(body).encode() if body is not None else None
                    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=3) as response:
                        return json.load(response)
                for path in ['/api', '/relaunch']:
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        request(path, False, {} if path == '/relaunch' else None)
                    self.assertEqual(failure.exception.code, 403)
                    failure.exception.close()
                before = request('/api')
                self.assertEqual(request('/relaunch', body={}), {'ok': True})
                self.assertEqual(lines.get(timeout=10), url)
                self.assertIsNone(process.poll())
                self.assertEqual(request('/api'), before)
                self.assertEqual(settings.read_bytes(), original)
                self.assertEqual(config.read_text(), '[ui.sound]\nenabled=false\n')
                self.assertEqual(request('/relaunch', body={}), {'ok': True})
                self.assertEqual(lines.get(timeout=10), url)
                self.assertEqual(request('/api'), before)
            finally:
                process.terminate()
                process.communicate(timeout=5)
