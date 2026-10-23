import argparse
import base64
import errno
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import webbrowser

from . import core


DEFAULT_PORT = 43127


def settings_token():
    inherited = os.environ.pop("MEDLEY_RELAUNCH_TOKEN", None)
    if inherited:
        return inherited
    with core.locked(core.config_dir()):
        path = core.config_dir() / 'settings-access.json'
        access = core.read_json(path, None)
        if access is None:
            access = {'token': secrets.token_urlsafe(32)}
            core.write_json(path, access)
        if not isinstance(access, dict) or not isinstance(access.get('token'), str) or len(access['token']) < 32:
            raise ValueError('The saved settings access key is invalid. Restore settings-access.json from a backup.')
        path.chmod(0o600)
        return access['token']


def instance_proof(token, challenge):
    return hmac.digest(token.encode(), challenge.encode(), 'sha256').hex()


def matching_server(port, token):
    challenge = secrets.token_hex(16)
    connection = HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', '/instance?challenge=' + challenge)
        response = connection.getresponse()
        if response.status != 200:
            return False
        proof = json.loads(response.read(4096)).get('proof', '')
        return isinstance(proof, str) and secrets.compare_digest(proof, instance_proof(token, challenge))
    except (OSError, HTTPException, ValueError, AttributeError):
        return False
    finally:
        connection.close()


def serve(open_browser=True, port=DEFAULT_PORT):
    token = settings_token()
    relaunch = threading.Event()
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
            if self.path.startswith('/instance?challenge='):
                challenge = self.path.removeprefix('/instance?challenge=')
                if len(challenge) == 32 and all(c in '0123456789abcdef' for c in challenge):
                    self.reply(200, {'proof': instance_proof(token, challenge)})
                else:
                    self.reply(400, {'error': 'Invalid settings server check'})
                return
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
                                 'workspaces': core.workspaces(), 'statuses': core.STATUSES,
                                 'mute': core.muted()})
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
                if self.path == '/relaunch':
                    if not relaunch.is_set():
                        relaunch.set()
                        self.reply(200, {'ok': True})
                        threading.Thread(target=server.shutdown, daemon=True).start()
                    else:
                        self.reply(409, {'error': 'Medley is already relaunching. Wait a moment, then refresh.'})
                    return
                elif self.path == '/uninstall':
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
                    if 'id' in data:
                        if data['id'] not in {audio['id'] for audio in core.library()}:
                            raise ValueError('This sound is no longer in the library. Refresh and try again.')
                        path = core.config_dir() / 'audio' / (data['id'] + '.mp3')
                    else:
                        if data['status'] not in core.STATUSES:
                            raise ValueError('Choose a valid status')
                        path = core.resolve(core.settings(), data['workspace'], data['status'])
                    if path is None:
                        self.reply(200, {'quiet': True})
                    else:
                        self.reply(200, path.read_bytes(), 'audio/mpeg')
                    return
                elif self.path == '/mute':
                    minutes = data.get('minutes')
                    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
                        raise ValueError('Choose how long to mute for.')
                    core.mute(minutes * 60)
                elif self.path == '/unmute':
                    core.unmute()
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

    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        if not matching_server(port, token):
            raise ValueError(f'Port {port} is used by another server. Close that server or choose a different --port.') from None
        url = f'http://127.0.0.1:{port}/#{token}'
        print('Medley settings:', url, flush=True)
        print('Opened the existing Medley settings server.', flush=True)
        if open_browser:
            webbrowser.open(url)
        return
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
    if relaunch.is_set():
        environment = dict(os.environ, MEDLEY_RELAUNCH_TOKEN=token)
        os.execve(sys.executable, [sys.executable, str(core.ROOT / 'medley.py'),
                                 'settings', '--no-browser', '--port', str(server.server_port)], environment)


def mute_length(text):
    """Seconds from '45m', '1h', '1h30', '90' (minutes) or '0' (until unmuted)."""
    import re
    match = re.fullmatch(r'\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m?)?\s*', text or '')
    if not match or not (match.group(1) or match.group(2)):
        raise ValueError('Give the mute length as 45m, 1h, 1h30 or 0 for until unmuted.')
    return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60


def mute_summary(now=None):
    """What `status` and the Caboodle switch say: on means sounds play."""
    import time
    data = core.settings()
    record = core.muted(now)
    if not data.get('enabled'):
        on, summary = False, 'Medley is disabled, so herdr plays its own sounds.'
    elif record and record.get('until'):
        on = False
        summary = 'Muted until ' + time.strftime('%H:%M', time.localtime(record['until'])) + '.'
    elif record:
        on, summary = False, 'Muted until unmuted.'
    else:
        on, summary = True, 'Workspace sounds play.'
    return {'id': 'medley-sounds', 'on': on, 'summary': summary, 'enabled': bool(data.get('enabled')), 'mute': record}


def main():
    parser = argparse.ArgumentParser(description='Medley — workspace sounds for herdr')
    parser.add_argument('command', choices=['settings', 'event', 'enable', 'disable', 'uninstall', 'import',
                                            'mute', 'unmute', 'status'])
    parser.add_argument('path', nargs='?')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Settings port (default: {DEFAULT_PORT}; 0 chooses a temporary port)')
    parser.add_argument('--for', dest='length', metavar='LENGTH',
                        help='mute: how long, as 45m, 1h or 1h30 (default 1h); 0 means until unmuted')
    parser.add_argument('--toggle', action='store_true', help='mute: unmute instead when already muted')
    parser.add_argument('--json', action='store_true', help='status: machine-readable')
    args = parser.parse_args()
    try:
        if args.command == 'settings':
            serve(not args.no_browser, args.port)
        elif args.command == 'mute':
            if args.toggle and core.muted():
                core.unmute()
                print('Medley: unmuted; workspace sounds play again.')
            else:
                core.mute(mute_length(args.length) if args.length is not None else core.MUTE_DEFAULT_SECONDS)
                print('Medley:', mute_summary()['summary'])
        elif args.command == 'unmute':
            core.unmute()
            print('Medley: unmuted; workspace sounds play again.')
        elif args.command == 'status':
            summary = mute_summary()
            print(json.dumps(summary) if args.json else 'Medley: ' + summary['summary'])
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
