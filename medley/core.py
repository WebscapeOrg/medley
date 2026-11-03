import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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


MUTE_DEFAULT_SECONDS = 60 * 60


def mute(seconds=MUTE_DEFAULT_SECONDS, now=None):
    """Silence every workspace sound for `seconds` (0: until unmuted). Returns the mute record."""
    now = now or time.time()
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0 or seconds > 24 * 3600:
        raise ValueError('Choose a mute length between 1 minute and 24 hours, or 0 for until unmuted.')
    if 0 < seconds < 60:
        raise ValueError('Choose a mute length between 1 minute and 24 hours, or 0 for until unmuted.')
    record = {'since': int(now), 'until': int(now + seconds) if seconds else None}
    with locked(config_dir()):
        write_json(config_dir() / 'mute.json', record)
    return record


def unmute():
    with locked(config_dir()):
        (config_dir() / 'mute.json').unlink(missing_ok=True)


def muted(now=None):
    """The mute record while it is in force, else None; a mute that has run out is forgotten."""
    now = now or time.time()
    record = read_json(config_dir() / 'mute.json', None)
    if not isinstance(record, dict):
        return None
    until = record.get('until')
    if until is None:
        return record
    if isinstance(until, (int, float)) and until > now:
        return record
    unmute()
    return None


def workspaces():
    result = subprocess.run([os.environ.get('HERDR_BIN_PATH', 'herdr'), 'workspace', 'list'],
                            capture_output=True, text=True, timeout=8, check=True)
    return json.loads(result.stdout)['result']['workspaces']


REMOTE_POLL_SECONDS = 5
REMOTE_RETRY_SECONDS = 30
REMOTE_TIMEOUT = 12
REMOTE_WAIT = 3
REMOTE_LOCK_RETRY_SECONDS = 30
REMOTE_IDLE_SECONDS = 30
REMOTE_MAX_WORKSPACES = 500


def herdr(*args, timeout=8):
    """Run herdr and parse its JSON. It runs in its own session, so a timeout stops its ssh too and nothing can prompt on a terminal."""
    process = subprocess.Popen([os.environ.get('HERDR_BIN_PATH', 'herdr'), *args], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, args, output, errors)
    return json.loads(output)


def machines():
    """The enabled saved SSH machines as [{'id', 'label'}], or None when herdr cannot say."""
    try:
        listed = herdr('machine', 'list', '--json')
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(listed, list):
        return None
    found, seen = [], set()
    for machine in listed:
        if not (isinstance(machine, dict) and machine.get('enabled') is True
                and isinstance(machine.get('id'), str) and isinstance(machine.get('label'), str)):
            continue
        label = machine['label'].strip()[:100]
        if label and label not in seen:
            seen.add(label)
            found.append({'id': machine['id'], 'label': label})
    return found


def remote_key(label, workspace_id):
    """Rules, mutes and statuses for a saved machine's workspace; local workspaces keep their bare id."""
    return label + '/' + workspace_id


def short_reason(text):
    """One readable line from herdr's error text, which can wrap the reason in a Rust debug string."""
    text = str(text)
    wrapped = re.search(r'error: "(.*)" \}', text)
    if wrapped:
        text = wrapped.group(1).replace('\\"', '"')
    lines = [' '.join(line.split()) for line in text.splitlines() if line.strip()]
    return (lines[0] if lines else 'The machine did not answer.')[:300]


def machine_workspaces(machine):
    """(workspaces, None) from one saved machine, or (None, why it could not be read)."""
    try:
        listed = herdr('--machine', machine['id'], 'workspace', 'list', timeout=REMOTE_TIMEOUT)['result']['workspaces']
        if not isinstance(listed, list):
            raise ValueError('herdr returned no list of workspaces.')
        found = []
        for workspace in listed[:REMOTE_MAX_WORKSPACES]:
            identity, name, status = workspace.get('workspace_id'), workspace.get('label'), workspace.get('agent_status')
            if not isinstance(identity, str) or not identity or len(identity) > 60:
                continue
            found.append({'workspace_id': identity, 'agent_status': status if status in STATUSES else None,
                          'label': (name if isinstance(name, str) and name.strip() else identity)[:200]})
        return found, None
    except subprocess.CalledProcessError as error:
        return None, short_reason((error.stderr or '').strip() or (error.stdout or '').strip() or error)
    except subprocess.TimeoutExpired:
        return None, 'No answer within %d seconds.' % REMOTE_TIMEOUT
    except Exception as error:
        return None, short_reason(error)


def remote_cache():
    try:
        cache = read_json(state_dir() / 'remote.json', {})
    except ValueError:
        return {}
    return cache if isinstance(cache, dict) else {}


def remote_workspaces():
    """Saved machines' workspaces as the poller last saw them, shaped like workspaces() with composite keys."""
    found = []
    for label, entry in remote_cache().items():
        if not isinstance(label, str) or not isinstance(entry, dict) or not isinstance(entry.get('workspaces'), list):
            continue
        for workspace in entry['workspaces']:
            if not (isinstance(workspace, dict) and isinstance(workspace.get('workspace_id'), str) and isinstance(workspace.get('label'), str)):
                continue
            found.append({'workspace_id': remote_key(label, workspace['workspace_id']),
                          'label': label + ' \u2192 ' + workspace['label'] + ('' if entry.get('ok') else ' (last seen)'),
                          'agent_status': workspace.get('agent_status'), 'machine': label})
    return found


def remote_problems():
    return [{'machine': label, 'error': str(entry.get('error') or '')} for label, entry in remote_cache().items()
            if isinstance(label, str) and isinstance(entry, dict) and not entry.get('ok')]


def record_remote_statuses(label, found, silent):
    """Remember each workspace's status; return the changes worth a sound unless `silent`."""
    changes = []
    with locked(state_dir()):
        statuses = read_json(state_dir() / 'statuses.json', {})
        before = dict(statuses)
        for workspace in found:
            status = workspace['agent_status']
            if status not in STATUSES:
                continue
            key = remote_key(label, workspace['workspace_id'])
            previous = statuses.get(key)
            statuses[key] = status
            if not silent and previous is not None and previous != status:
                changes.append((key, status))
        if statuses != before:
            write_json(state_dir() / 'statuses.json', statuses)
    return changes


def play_remote_change(key, status):
    data = settings()
    if not data.get('enabled') or muted():
        return
    path = resolve(data, key, status)
    if path is not None:
        play(path)


def new_poll_state():
    return {'synced': set(), 'retry': {}, 'pending': {}}


def start_lookup(machine):
    """Ask one machine for its workspaces on a daemon thread, so a slow machine holds nobody else up."""
    lookup = {'machine': machine, 'done': threading.Event(), 'answer': None}

    def run():
        try:
            lookup['answer'] = machine_workspaces(machine)
        except BaseException as error:
            lookup['answer'] = (None, short_reason(error))
        lookup['done'].set()

    threading.Thread(target=run, daemon=True).start()
    return lookup


def poll_remote(state):
    """One pass over the enabled saved machines: refresh what the page lists and play sounds for status changes.
    Returns how many machines there are (0 when herdr has none or cannot say).

    `state` (from new_poll_state) belongs to the polling loop. A machine that has not answered within
    REMOTE_WAIT is left running and read on a later pass. The first pass after a start, and the first
    after a machine was unreachable, only record statuses, so nothing plays late."""
    listed = machines()
    if listed is None:
        return 0
    now = time.monotonic()
    pending = state['pending']
    for machine in listed:
        label = machine['label']
        if label not in pending and state['retry'].get(label, 0) <= now:
            pending[label] = start_lookup(machine)
    deadline = time.monotonic() + REMOTE_WAIT
    for lookup in list(pending.values()):
        lookup['done'].wait(max(0, deadline - time.monotonic()))
    cache = remote_cache()
    before = copy.deepcopy(cache)
    changes = []
    for label in [label for label, lookup in pending.items() if lookup['done'].is_set()]:
        lookup = pending.pop(label)
        found, error = lookup['answer']
        known = cache.get(label) if isinstance(cache.get(label), dict) else {}
        try:
            if found is None:
                cache[label] = {**known, 'id': lookup['machine']['id'], 'ok': False, 'error': error}
                state['retry'][label] = time.monotonic() + REMOTE_RETRY_SECONDS
                state['synced'].discard(label)
                continue
            cache[label] = {'id': lookup['machine']['id'], 'ok': True, 'error': None, 'workspaces': found}
            state['retry'].pop(label, None)
            changes += record_remote_statuses(label, found, silent=label not in state['synced'])
            state['synced'].add(label)
        except Exception as problem:
            print('Medley: could not record', label + ':', problem, file=sys.stderr)
    current = {machine['label'] for machine in listed}
    for label in [label for label in cache if label not in current]:
        del cache[label]
        for part in (state['synced'], state['retry'], pending):
            part.discard(label) if isinstance(part, set) else part.pop(label, None)
    if cache != before:
        write_json(state_dir() / 'remote.json', cache)
    for key, status in changes:
        try:
            play_remote_change(key, status)
        except Exception as error:
            print('Medley: could not play a sound for', key + ':', error, file=sys.stderr)
    return len(listed)


def poll_remote_forever(stop):
    """Watch saved machines until `stop` is set. One process per state directory watches; others wait their turn."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'remote.lock').open('a') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if stop.wait(REMOTE_LOCK_RETRY_SECONDS):
                    return
        state = new_poll_state()
        while True:
            count = 0
            try:
                count = poll_remote(state)
            except Exception as error:
                print('Medley: remote machines:', error, file=sys.stderr)
            if stop.wait(REMOTE_POLL_SECONDS if count else REMOTE_IDLE_SECONDS):
                return


def library():
    directory = config_dir() / 'audio'
    if not directory.exists():
        return []
    names = read_json(config_dir() / 'library.json', {})
    return [{'id': p.stem, 'name': names.get(p.stem, p.name)}
            for p in sorted(directory.glob('*.mp3'))]


@contextlib.contextmanager
def staged_audio(name, content):
    """Validate an MP3 or WAV and yield (temporary MP3 path, SHA-256 key, MP3 file name); the temporary file is removed on exit."""
    if not isinstance(name, str) or not name.lower().endswith(('.mp3', '.wav')) or not content or len(content) > MAX_AUDIO:
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
        yield temporary, hashlib.sha256(content).hexdigest(), name
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def add_audio(name, content):
    """Import audio and return (key, added). Audio already in the library keeps the name it has."""
    with staged_audio(name, content) as (temporary, key, name):
        with locked(config_dir()):
            added = key not in {audio['id'] for audio in library()}
            os.replace(temporary, config_dir() / 'audio' / (key + '.mp3'))
            names = read_json(config_dir() / 'library.json', {})
            if added or key not in names:
                names[key] = Path(name).name[:160]
                write_json(config_dir() / 'library.json', names)
    return key, added


def import_audio(name, content):
    return add_audio(name, content)[0]


def replace_audio(key, name, content):
    """Swap the audio behind a library entry. Its name and every workspace choice using it are kept.

    Audio identities are content hashes, so the new audio gets a new key; `name` only says whether
    the upload is an MP3 or a WAV and is never stored. The steps are ordered so that a failure part
    way leaves nothing misleading (a name with no file is invisible; two entries with one name are
    harmless) and repeating the replace finishes the swap."""
    if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('Choose an audio file from the library.')
    with staged_audio(name, content) as (temporary, new_key, _):
        with locked(config_dir()):
            current = {audio['id']: audio['name'] for audio in library()}
            if key not in current:
                raise ValueError('This sound is no longer in the library. Refresh and try again.')
            if new_key == key:
                return key
            if new_key in current and current[new_key] != current[key]:
                other = current[new_key][:-4] if current[new_key].lower().endswith('.mp3') else current[new_key]
                raise ValueError('This audio is already in My Sounds as “' + other + '”. Choose it in a workspace, or remove one of the two sounds.')
            directory = config_dir() / 'audio'
            names = read_json(config_dir() / 'library.json', {})
            names[new_key] = current[key]
            write_json(config_dir() / 'library.json', names)
            os.replace(temporary, directory / (new_key + '.mp3'))
            data = settings()
            changed = False
            for rules in data.get('rules', {}).values():
                for status, choice in rules.items():
                    if choice == key:
                        rules[status] = new_key
                        changed = True
            if changed:
                write_json(config_dir() / 'settings.json', data)
            names.pop(key, None)
            write_json(config_dir() / 'library.json', names)
            (directory / (key + '.mp3')).unlink()
    return new_key


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


def set_status_mute(workspace, status, muted):
    if not isinstance(workspace, str) or not workspace or len(workspace) > 200:
        raise ValueError('Choose a valid workspace.')
    if status not in (*STATUSES, 'all') or not isinstance(muted, bool):
        raise ValueError('Choose a valid status and whether to mute it.')
    def change(data):
        mutes = data.setdefault('mutes', {})
        statuses = set(mutes.get(workspace, []))
        affected = STATUSES if status == 'all' else (status,)
        if muted:
            statuses.update(affected)
        else:
            statuses.difference_update(affected)
        if statuses:
            mutes[workspace] = [value for value in STATUSES if value in statuses]
        else:
            mutes.pop(workspace, None)
    return update(change)


def herdr_config():
    path = Path(os.environ.get('HERDR_CONFIG_PATH', Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'herdr/config.toml'))
    return path, tomllib.loads(path.read_text())


def resolve(data, workspace, status):
    if status in data.get('mutes', {}).get(workspace, []):
        return None
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
    if muted():
        return
    play(resolve(settings(), workspace, status))


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
