import base64
import hashlib
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


class ReplaceRequestTests(unittest.TestCase):
    def test_replace_and_upload_need_the_token_and_answer_with_the_sound_id(self):
        first = (core.ROOT / 'assets/done.mp3').read_bytes()
        second = (core.ROOT / 'assets/request.mp3').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_herdr = root / 'herdr'
            fake_herdr.write_text('#!' + sys.executable + '\nprint(\'{"result":{"workspaces":[]}}\')\n')
            fake_herdr.chmod(0o700)
            config = root / 'herdr.toml'
            config.write_text('[ui.sound]\n')
            environment = dict(os.environ, HERDR_PLUGIN_CONFIG_DIR=directory, HERDR_CONFIG_PATH=str(config),
                               HERDR_BIN_PATH=str(fake_herdr))
            environment.pop('MEDLEY_RELAUNCH_TOKEN', None)
            key = hashlib.sha256(first).hexdigest()
            (root / 'audio').mkdir()
            (root / 'audio' / (key + '.mp3')).write_bytes(first)
            (root / 'library.json').write_text(json.dumps({key: 'My chime'}))
            (root / 'settings.json').write_text(json.dumps({'enabled': True, 'rules': {'w1': {'done': key}}}))
            process = subprocess.Popen([sys.executable, str(core.ROOT / 'medley.py'), 'settings', '--no-browser', '--port', '0'],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = queue.Queue()
            def collect():
                for line in process.stdout:
                    if line.startswith('Medley settings: '):
                        lines.put(line.strip().split('Medley settings: ', 1)[1])
            threading.Thread(target=collect, daemon=True).start()
            try:
                base, token = lines.get(timeout=10).split('/#')
                def request(path, body=None, authorized=True):
                    headers = {'X-Medley-Token': token} if authorized else {}
                    data = json.dumps(body).encode() if body is not None else None
                    try:
                        with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=5) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as failure:
                        with failure:
                            return failure.code, json.load(failure)
                encoded = base64.b64encode(second).decode()
                before = (root / 'settings.json').read_bytes()
                self.assertEqual(request('/replace', {'id': key, 'name': 'x.mp3', 'content': encoded}, authorized=False)[0], 403)
                self.assertEqual((root / 'settings.json').read_bytes(), before)
                for body in [{'id': key, 'name': 'x.mp3', 'content': '***'}, {'id': key, 'name': 'x.mp3'},
                             {'id': '0' * 64, 'name': 'x.mp3', 'content': encoded},
                             {'id': key, 'name': 'x.txt', 'content': encoded}]:
                    status, _ = request('/replace', body)
                    self.assertEqual(status, 400, body)
                self.assertEqual((root / 'settings.json').read_bytes(), before)
                status, result = request('/replace', {'id': key, 'name': 'elsewhere.mp3', 'content': encoded})
                new_key = hashlib.sha256(second).hexdigest()
                self.assertEqual((status, result), (200, {'ok': True, 'id': new_key}))
                api = request('/api')[1]
                self.assertEqual(api['audio'], [{'id': new_key, 'name': 'My chime'}])
                self.assertEqual(api['settings']['rules'], {'w1': {'done': new_key}})
                self.assertEqual((root / 'audio' / (new_key + '.mp3')).read_bytes(), second)
                self.assertFalse((root / 'audio' / (key + '.mp3')).exists())
                self.assertEqual(request('/upload', {'name': 'fresh.mp3', 'content': base64.b64encode(first).decode()}, authorized=False)[0], 403)
                self.assertEqual(request('/upload', {'name': 'fresh.mp3', 'content': base64.b64encode(first).decode()}), (200, {'ok': True, 'id': key, 'added': True}))
                self.assertEqual({audio['id']: audio['name'] for audio in request('/api')[1]['audio']}, {new_key: 'My chime', key: 'fresh.mp3'})
                self.assertEqual(request('/upload', {'name': 'again.mp3', 'content': base64.b64encode(first).decode()}), (200, {'ok': True, 'id': key, 'added': False}))
                self.assertEqual({audio['id']: audio['name'] for audio in request('/api')[1]['audio']}[key], 'fresh.mp3')
            finally:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
