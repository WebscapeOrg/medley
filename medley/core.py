import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parent.parent
STATUSES = ('idle', 'working', 'blocked', 'done', 'unknown')
MAX_AUDIO = 20 * 1024 * 1024


def config_dir():
    return Path(os.environ.get('HERDR_PLUGIN_CONFIG_DIR', Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'herdr/plugins/config/medley'))


def state_dir():
    return Path(os.environ.get('HERDR_PLUGIN_STATE_DIR', Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'herdr/plugins/medley'))


@contextlib.contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read_json(path, fallback):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return fallback


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def settings():
    return read_json(config_dir() / 'settings.json', {'rules': {}, 'enabled': False})


def update(change):
    with locked(config_dir()):
        data = settings()
        change(data)
        write_json(config_dir() / 'settings.json', data)
    return data


def workspaces():
    result = subprocess.run([os.environ.get('HERDR_BIN_PATH', 'herdr'), 'workspace', 'list'],
                            capture_output=True, text=True, timeout=8, check=True)
    return json.loads(result.stdout)['result']['workspaces']


def library():
    directory = config_dir() / 'audio'
    if not directory.exists():
        return []
    names = read_json(config_dir() / 'library.json', {})
    return [{'id': p.stem, 'name': names.get(p.stem, p.name)}
            for p in sorted(directory.glob('*.mp3'))]


def import_audio(name, content):
    if not name.lower().endswith(('.mp3', '.wav')) or not content or len(content) > MAX_AUDIO:
        raise ValueError('Choose an MP3 or WAV file between 1 byte and 20 MB.')
    if not shutil.which('ffprobe'):
        raise ValueError('Install ffmpeg to validate imported audio.')
    directory = config_dir() / 'audio'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(suffix=Path(name).suffix.lower(), dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,codec_type:format=format_name',
                                '-of', 'json', temporary], capture_output=True, text=True, timeout=10)
        metadata = json.loads(probe.stdout or '{}')
        streams = metadata.get('streams', [])
        if probe.returncode or not any(s.get('codec_type') == 'audio' and (s.get('codec_name') == 'mp3' if name.lower().endswith('.mp3') else metadata.get('format', {}).get('format_name') == 'wav') for s in streams):
            raise ValueError('This file does not contain valid MP3 or WAV audio.')
        if name.lower().endswith('.wav'):
            if not streams or not any(stream.get('codec_type') == 'audio' for stream in streams):
                raise ValueError('This file does not contain valid WAV audio.')
            converted = temporary + '.mp3'
            try:
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', temporary,
                                '-map', '0:a:0', '-vn', '-codec:a', 'libmp3lame', '-q:a', '2',
                                '-fs', str(MAX_AUDIO + 1), '-y', converted],
                               check=True, capture_output=True, timeout=30)
                content = Path(converted).read_bytes()
                if not content or len(content) > MAX_AUDIO:
                    raise ValueError('Converted audio exceeds the 20 MB limit.')
                os.replace(converted, temporary)
            finally:
                Path(converted).unlink(missing_ok=True)
            name = str(Path(name).with_suffix('.mp3'))
        key = hashlib.sha256(content).hexdigest()
        with locked(config_dir()):
            os.replace(temporary, directory / (key + '.mp3'))
            names = read_json(config_dir() / 'library.json', {})
            names[key] = Path(name).name[:160]
            write_json(config_dir() / 'library.json', names)
        return key
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rename_audio(key, name):
    if not isinstance(name, str):
        raise ValueError('Enter a sound name between 1 and 160 characters.')
    name = name.strip()
    if name.lower().endswith('.mp3'):
        name = name[:-4].rstrip()
    if not name or len(name) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('Enter a sound name between 1 and 160 characters, without line breaks.')
    with locked(config_dir()):
        if not isinstance(key, str) or key not in {audio['id'] for audio in library()}:
            raise ValueError('This sound is no longer in the library. Refresh and try again.')
        names = read_json(config_dir() / 'library.json', {})
        names[key] = name
        write_json(config_dir() / 'library.json', names)


def remove_audio(key):
    if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('Choose an audio file from the library.')
    with locked(config_dir()):
        if key not in {audio['id'] for audio in library()}:
            raise ValueError('This sound is no longer in the library. Refresh and try again.')
        data = settings()
        for workspace in list(data['rules']):
            rules = data['rules'][workspace]
            for status in list(rules):
                if rules[status] == key:
                    del rules[status]
            if not rules:
                del data['rules'][workspace]
        write_json(config_dir() / 'settings.json', data)
        names = read_json(config_dir() / 'library.json', {})
        names.pop(key, None)
        write_json(config_dir() / 'library.json', names)
        (config_dir() / 'audio' / (key + '.mp3')).unlink()


def set_rule(workspace, status, choice):
    if status not in STATUSES or not isinstance(workspace, str) or len(workspace) > 200:
        raise ValueError('Choose a valid workspace and status.')
    def change(data):
        if choice not in ('default', 'none') and choice not in {a['id'] for a in library()}:
            raise ValueError('Choose an audio file from the library.')
        rules = data['rules'].setdefault(workspace, {})
        if choice == 'default':
            rules.pop(status, None)
        else:
            rules[status] = choice
        if not rules:
            data['rules'].pop(workspace, None)
    return update(change)


def herdr_config():
    path = Path(os.environ.get('HERDR_CONFIG_PATH', Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'herdr/config.toml'))
    return path, tomllib.loads(path.read_text())


def resolve(data, workspace, status):
    choice = data.get('rules', {}).get(workspace, {}).get(status, 'default')
    if choice == 'none':
        return None
    if choice != 'default':
        if choice not in {a['id'] for a in library()}:
            raise ValueError('The selected audio file is missing.')
        return config_dir() / 'audio' / (choice + '.mp3')
    if status not in ('idle', 'done', 'blocked'):
        return None
    category = 'request' if status == 'blocked' else 'done'
    path, config = herdr_config()
    sound = config.get('ui', {}).get('sound', {})
    selected = sound.get(category + '_path') or sound.get('path')
    if selected:
        custom = Path(selected).expanduser()
        if not custom.is_absolute():
            custom = path.parent / custom
        if custom.is_file():
            return custom
    return ROOT / 'assets' / (category + '.mp3')


def play(path):
    if path is None:
        return
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'playback.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        players = [('afplay', [str(path)]), ('ffplay', ['-nodisp', '-autoexit', '-loglevel', 'error', str(path)]),
                   ('mpg123', ['-q', str(path)]), ('mpv', ['--no-video', '--really-quiet', str(path)])]
        for player, args in players:
            if shutil.which(player):
                subprocess.run([player, *args], timeout=30, check=True, stdout=subprocess.DEVNULL)
                return
        raise ValueError('Install ffmpeg, mpg123 or mpv to play audio.')


def event(payload):
    data = settings()
    if not data.get('enabled'):
        return
    event_data = payload.get('data', payload)
    workspace = event_data.get('workspace_id')
    if not workspace:
        return
    with locked(state_dir()):
        current = next((w for w in workspaces() if w['workspace_id'] == workspace), None)
        if not current:
            return
        status = current['agent_status']
        if status not in STATUSES:
            return
        cache = read_json(state_dir() / 'statuses.json', {})
        previous = cache.get(workspace)
        cache[workspace] = status
        write_json(state_dir() / 'statuses.json', cache)
        if previous is None or previous == status:
            return
    play(resolve(data, workspace, status))


def activate(enabled):
    path, config = herdr_config()
    old = config.get('ui', {}).get('sound', {}).get('enabled', True)
    data = settings()
    if enabled and data.get('enabled'):
        return data
    if not enabled and not data.get('enabled'):
        return data
    target = False if enabled else data.get('previous_sound_enabled', True)
    text = path.read_text()
    import re
    match = re.search(r'(?m)^\[ui\.sound\]\s*$', text)
    line = 'enabled = ' + str(target).lower() + '\n'
    if match:
        end = re.search(r'(?m)^\[', text[match.end():])
        stop = match.end() + end.start() if end else len(text)
        block = text[match.end():stop]
        block = re.sub(r'(?m)^enabled\s*=.*$', line.rstrip(), block) if re.search(r'(?m)^enabled\s*=', block) else '\n' + line + block
        text = text[:match.end()] + block + text[stop:]
    else:
        text += '\n[ui.sound]\n' + line
    tomllib.loads(text)
    backup = config_dir() / 'herdr-config-before-medley.toml'
    if enabled:
        config_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
            backup.chmod(0o600)
    original = path.read_text()
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    try:
        subprocess.run([os.environ.get('HERDR_BIN_PATH', 'herdr'), 'config', 'check'], check=True, capture_output=True, timeout=8)
        subprocess.run([os.environ.get('HERDR_BIN_PATH', 'herdr'), 'server', 'reload-config'], check=True, capture_output=True, timeout=8)
        if enabled:
            with locked(state_dir()):
                write_json(state_dir() / 'statuses.json', {w['workspace_id']: w['agent_status'] for w in workspaces()})
        def change(data):
            data['enabled'] = enabled
            if enabled:
                data['previous_sound_enabled'] = old
        return update(change)
    except Exception:
        path.write_text(original)
        subprocess.run([os.environ.get('HERDR_BIN_PATH', 'herdr'), 'server', 'reload-config'], capture_output=True, timeout=8)
        raise


def uninstall():
    activate(False)
    try:
        subprocess.run([os.environ.get('HERDR_BIN_PATH', 'herdr'), 'plugin', 'uninstall', 'medley'],
                       check=True, capture_output=True, timeout=15)
    except Exception as error:
        raise ValueError('Medley is disabled, but uninstall failed. Retry Uninstall or run herdr plugin uninstall medley.') from error
