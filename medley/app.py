import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import webbrowser

from . import core


def serve(open_browser=True, port=0):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, data, mime='application/json'):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src blob:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            return secrets.compare_digest(self.headers.get('X-Medley-Token', ''), token)

        def do_GET(self):
            if self.path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[self.path]
                mime = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript', '/style.css': 'text/css'}[self.path]
                self.reply(200, (core.ROOT / 'assets' / name).read_bytes(), mime)
                return
            if not self.authorized():
                self.reply(403, {'error': 'Open Medley using its settings command.'})
                return
            try:
                if self.path != '/api':
                    self.reply(404, {'error': 'Not found'})
                    return
                self.reply(200, {'settings': core.settings(), 'audio': core.library(),
                                 'workspaces': core.workspaces(), 'statuses': core.STATUSES})
            except Exception as error:
                self.reply(503, {'error': str(error)})

        def do_POST(self):
            if not self.authorized():
                self.reply(403, {'error': 'Open Medley using its settings command.'})
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= core.MAX_AUDIO * 2:
                    raise ValueError('Request is empty or too large. Maximum audio size is 20 MB.')
                data = json.loads(self.rfile.read(length))
                if self.path == '/uninstall':
                    core.uninstall()
                elif self.path == '/rename':
                    core.rename_audio(data['id'], data['name'])
                elif self.path == '/remove':
                    core.remove_audio(data['id'])
                elif self.path == '/rule':
                    core.set_rule(data['workspace'], data['status'], data['choice'])
                elif self.path == '/reset':
                    core.update(lambda settings: settings.update(rules={}))
                elif self.path == '/restore':
                    rules = data['rules']
                    if not isinstance(rules, dict):
                        raise ValueError('Invalid restore data')
                    for workspace, choices in rules.items():
                        for status, choice in choices.items():
                            if status not in core.STATUSES or choice not in ('none', 'default', *[a['id'] for a in core.library()]):
                                raise ValueError('Invalid restore data')
                    core.update(lambda settings: settings.update(rules=rules))
                elif self.path == '/upload':
                    core.import_audio(data['name'], base64.b64decode(data['content'], validate=True))
                elif self.path == '/preview':
                    if data['status'] not in core.STATUSES:
                        raise ValueError('Choose a valid status')
                    path = core.resolve(core.settings(), data['workspace'], data['status'])
                    if path is None:
                        self.reply(200, {'quiet': True})
                    else:
                        self.reply(200, path.read_bytes(), 'audio/mpeg')
                    return
                elif self.path == '/enabled':
                    if not isinstance(data.get('enabled'), bool):
                        raise ValueError('Choose whether to enable Medley')
                    core.activate(data['enabled'])
                else:
                    self.reply(404, {'error': 'Not found'})
                    return
                self.reply(200, {'ok': True})
            except (ValueError, KeyError, TypeError) as error:
                self.reply(400, {'error': str(error)})
            except Exception as error:
                self.reply(503, {'error': str(error)})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_port}/#{token}'
    print('Medley settings:', url, flush=True)
    print('Press Ctrl+C to close settings. Sound rules continue through herdr event hooks.', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description='Medley — workspace sounds for herdr')
    parser.add_argument('command', choices=['settings', 'event', 'enable', 'disable', 'uninstall', 'import'])
    parser.add_argument('path', nargs='?')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == 'settings':
            serve(not args.no_browser, args.port)
        elif args.command == 'event':
            core.event(json.loads(os.environ.get('HERDR_PLUGIN_EVENT_JSON', '{}')))
        elif args.command == 'enable':
            core.activate(True)
        elif args.command == 'disable':
            core.activate(False)
        elif args.command == 'uninstall':
            core.uninstall()
        elif args.command == 'import':
            if not args.path:
                parser.error('import requires a file path')
            path = Path(args.path)
            if path.stat().st_size > core.MAX_AUDIO:
                raise ValueError('Maximum audio size is 20 MB')
            print(core.import_audio(path.name, path.read_bytes()))
    except Exception as error:
        print('Medley:', error, file=sys.stderr)
        sys.exit(1)
