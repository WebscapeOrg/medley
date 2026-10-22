import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from medley import app, core


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = dict(os.environ, HERDR_PLUGIN_CONFIG_DIR=self.directory.name)
        self.environment.pop('MEDLEY_RELAUNCH_TOKEN', None)
        fake = self.root / 'herdr'
        fake.write_text('#!' + sys.executable + '\nprint(\'{"result":{"workspaces":[]}}\')\n')
        fake.chmod(0o700)
        self.environment['HERDR_BIN_PATH'] = str(fake)

    @contextlib.contextmanager
    def server(self, port=0):
        process = subprocess.Popen(
            [sys.executable, str(core.ROOT / 'medley.py'), 'settings', '--no-browser', '--port', str(port)],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()

        def collect():
            for line in process.stdout:
                if line.startswith('Medley settings: '):
                    lines.put(line.strip().split('Medley settings: ', 1)[1])

        threading.Thread(target=collect, daemon=True).start()
        try:
            yield process, lines.get(timeout=10)
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_bookmarked_url_survives_full_stop_and_restart(self):
        settings = self.root / 'settings.json'
        settings.write_text('{"enabled":true,"rules":{"example":{"done":"none"}}}')
        original = settings.read_bytes()
        with self.server() as (_, bookmark):
            port = urllib.parse.urlsplit(bookmark).port
        with self.server(port) as (_, reopened):
            self.assertEqual(reopened, bookmark)
            base, token = bookmark.split('/#')
            request = urllib.request.Request(base + '/api', headers={'X-Medley-Token': token})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertTrue(json.load(response)['settings']['enabled'])
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(base + '/api', timeout=3)
            self.assertEqual(failure.exception.code, 403)
            failure.exception.close()
        self.assertEqual(settings.read_bytes(), original)
        access = self.root / 'settings-access.json'
        self.assertEqual(stat.S_IMODE(access.stat().st_mode), 0o600)

    def test_second_launch_reuses_server_without_interrupting_it(self):
        with self.server() as (process, bookmark):
            port = urllib.parse.urlsplit(bookmark).port
            second = subprocess.run(
                [sys.executable, str(core.ROOT / 'medley.py'), 'settings', '--no-browser', '--port', str(port)],
                env=self.environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn(bookmark, second.stdout)
            self.assertIn('existing Medley settings server', second.stdout)
            self.assertIsNone(process.poll())

    def test_foreign_listener_is_not_given_the_token_or_opened(self):
        requests = []

        class ForeignHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                body = b'{"proof":"not-medley"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), ForeignHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.dict(os.environ, self.environment, clear=True), patch.object(app.webbrowser, 'open') as browser:
                with self.assertRaisesRegex(ValueError, 'used by another server'):
                    app.serve(port=server.server_port)
                browser.assert_not_called()
            token = json.loads((self.root / 'settings-access.json').read_text())['token']
            self.assertEqual(len(requests), 1)
            self.assertNotIn('X-Medley-Token', requests[0][1])
            self.assertNotIn(token, repr(requests))
        finally:
            server.shutdown()
            server.server_close()

    def test_existing_saved_token_permissions_are_restricted(self):
        access = self.root / 'settings-access.json'
        access.write_text(json.dumps({'token': 'a' * 43}))
        access.chmod(0o644)
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(app.settings_token(), 'a' * 43)
        self.assertEqual(stat.S_IMODE(access.stat().st_mode), 0o600)

    def test_invalid_saved_key_is_not_silently_replaced(self):
        access = self.root / 'settings-access.json'
        access.write_text('{"token":""}')
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'saved settings access key is invalid'):
                app.settings_token()
        self.assertEqual(access.read_text(), '{"token":""}')

    def test_command_defaults_to_fixed_port(self):
        with patch.object(sys, 'argv', ['medley.py', 'settings', '--no-browser']), patch.object(app, 'serve') as serve:
            app.main()
        serve.assert_called_once_with(False, app.DEFAULT_PORT)
