import fcntl
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

from medley import core

FAKE_HERDR = '''#!%s
import json, os, sys
root = os.environ['FAKE_HERDR_DIR']
args = sys.argv[1:]
with open(root + '/calls.log', 'a') as log:
    log.write(' '.join(args) + '\\n')
if args[:3] == ['machine', 'list', '--json']:
    text = open(root + '/machines.json').read()
    if text.startswith('FAIL'):
        sys.exit(1)
    print(text)
elif args[:1] == ['--machine'] and args[2:] == ['workspace', 'list']:
    text = open(root + '/' + args[1] + '.json').read()
    if text.startswith('SLOW'):
        import time
        time.sleep(float(text.split()[1]))
        sys.exit(1)
    if text.startswith('FAIL'):
        print(text[5:], file=sys.stderr)
        sys.exit(1)
    print(text)
elif args == ['workspace', 'list']:
    print(json.dumps({'result': {'workspaces': [{'workspace_id': 'w1', 'label': 'Local project', 'agent_status': 'idle'}]}}))
else:
    sys.exit(2)
'''


class RemoteFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake_dir = self.root / 'fake'
        self.fake_dir.mkdir()
        fake = self.root / 'herdr'
        fake.write_text(FAKE_HERDR % sys.executable)
        fake.chmod(0o700)
        config = self.root / 'herdr.toml'
        config.write_text('[ui.sound]\n')
        environment = patch.dict(os.environ, {
            'HERDR_PLUGIN_CONFIG_DIR': str(self.root / 'config'), 'HERDR_PLUGIN_STATE_DIR': str(self.root / 'state'),
            'HERDR_CONFIG_PATH': str(config), 'HERDR_BIN_PATH': str(fake), 'FAKE_HERDR_DIR': str(self.fake_dir)})
        environment.start()
        self.addCleanup(environment.stop)

    def machines(self, *labels, disabled=()):
        listed = [{'id': 'id-' + label.lower(), 'label': label, 'target': label.lower(), 'session': 'default',
                   'enabled': label not in disabled, 'selected': False} for label in labels]
        (self.fake_dir / 'machines.json').write_text(json.dumps(listed))

    def workspaces(self, label, *rows):
        listed = [{'workspace_id': wid, 'label': name, 'agent_status': status} for wid, name, status in rows]
        (self.fake_dir / ('id-' + label.lower() + '.json')).write_text(json.dumps({'result': {'workspaces': listed}}))

    def fail(self, label, reason='ssh: Could not resolve hostname harbor.local: Name or service not known'):
        (self.fake_dir / ('id-' + label.lower() + '.json')).write_text('FAIL ' + reason)

    def calls(self):
        log = self.fake_dir / 'calls.log'
        return log.read_text().splitlines() if log.exists() else []

    @staticmethod
    def new_state():
        return core.new_poll_state()


class RemotePollTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.sound = core.import_audio('chime.mp3', (core.ROOT / 'assets/done.mp3').read_bytes())
        core.update(lambda data: data.update(enabled=True))
        core.set_rule('Harbor/w4', 'done', self.sound)
        self.machines('Harbor')

    def poll(self, state, *statuses):
        self.workspaces('Harbor', ('w1', 'Harbor', 'idle'), ('w4', 'Tidepool', statuses[0]))
        with patch.object(core, 'play') as play:
            core.poll_remote(state)
        return play

    def test_lists_a_machines_workspaces_under_its_label_with_composite_keys(self):
        self.poll(self.new_state(), 'working')
        self.assertEqual(core.remote_workspaces(), [
            {'workspace_id': 'Harbor/w1', 'label': 'Harbor → Harbor', 'agent_status': 'idle', 'machine': 'Harbor'},
            {'workspace_id': 'Harbor/w4', 'label': 'Harbor → Tidepool', 'agent_status': 'working', 'machine': 'Harbor'}])
        self.assertEqual(core.remote_problems(), [])
        self.assertIn('--machine id-harbor workspace list', self.calls())

    def test_the_first_pass_is_silent_and_a_later_change_plays_the_rule_for_that_workspace(self):
        state = self.new_state()
        self.poll(state, 'working').assert_not_called()
        self.poll(state, 'working').assert_not_called()
        play = self.poll(state, 'done')
        play.assert_called_once_with(core.config_dir() / 'audio' / (self.sound + '.mp3'))
        self.poll(state, 'done').assert_not_called()

    def test_a_restart_does_not_play_what_changed_while_it_was_stopped(self):
        self.poll(self.new_state(), 'working')
        self.poll(self.new_state(), 'done').assert_not_called()

    def test_unrecognised_statuses_are_ignored(self):
        state = self.new_state()
        self.poll(state, 'working')
        self.poll(state, 'sleeping').assert_not_called()
        self.assertNotIn('sleeping', json.dumps(json.loads((core.state_dir() / 'statuses.json').read_text())))

    def test_mute_and_a_disabled_medley_play_nothing_and_leave_nothing_to_play_late(self):
        state = self.new_state()
        self.poll(state, 'working')
        core.mute(600)
        self.poll(state, 'done').assert_not_called()
        core.unmute()
        self.poll(state, 'done').assert_not_called()
        self.poll(state, 'working').assert_not_called()
        core.update(lambda data: data.update(enabled=False))
        self.poll(state, 'done').assert_not_called()

    def test_a_status_mute_on_a_remote_workspace_silences_it(self):
        state = self.new_state()
        self.poll(state, 'working')
        core.set_status_mute('Harbor/w4', 'done', True)
        self.poll(state, 'done').assert_not_called()

    def test_an_unreachable_machine_keeps_its_last_workspaces_and_says_why(self):
        state = self.new_state()
        self.poll(state, 'working')
        self.fail('Harbor')
        with patch.object(core, 'play') as play:
            core.poll_remote(state)
        play.assert_not_called()
        self.assertEqual([w['label'] for w in core.remote_workspaces()], ['Harbor → Harbor (last seen)', 'Harbor → Tidepool (last seen)'])
        self.assertEqual(core.remote_problems(), [{'machine': 'Harbor', 'error': 'ssh: Could not resolve hostname harbor.local: Name or service not known'}])

    def test_odd_data_from_a_machine_cannot_break_the_page_or_the_other_machines(self):
        self.machines('Odd', 'Broken', 'Deep', 'Good')
        (self.fake_dir / 'id-odd.json').write_text(json.dumps({'result': {'workspaces': [
            {'workspace_id': 5, 'label': 'skipped', 'agent_status': 'idle'},
            {'workspace_id': 'w9', 'label': 7, 'agent_status': 'weird'},
            {'workspace_id': 'x' * 61, 'label': 'too long', 'agent_status': 'idle'}]}}))
        (self.fake_dir / 'id-broken.json').write_text(json.dumps({'result': {'workspaces': 'nope'}}))
        (self.fake_dir / 'id-deep.json').write_text('[' * 200000)
        self.workspaces('Good', ('w1', 'Fine', 'idle'))
        core.poll_remote(self.new_state())
        self.assertEqual([(w['workspace_id'], w['label'], w['agent_status']) for w in core.remote_workspaces()],
                         [('Odd/w9', 'Odd \u2192 w9', None), ('Good/w1', 'Good \u2192 Fine', 'idle')])
        self.assertEqual({p['machine'] for p in core.remote_problems()}, {'Broken', 'Deep'})
        self.assertEqual(next(p['error'] for p in core.remote_problems() if p['machine'] == 'Broken'), 'herdr returned no list of workspaces.')

    def test_a_damaged_cache_never_breaks_the_listing(self):
        core.state_dir().mkdir(parents=True)
        (core.state_dir() / 'remote.json').write_text(json.dumps({
            'Harbor': {'ok': True, 'workspaces': [{'workspace_id': 5, 'label': 'x'}, 'junk', {'workspace_id': 'w1'}, {'workspace_id': 'w2', 'label': 'Fine', 'agent_status': 'idle'}]},
            'Bad': 5, 'Worse': {'ok': False, 'error': 5, 'workspaces': 'nope'}}))
        self.assertEqual([w['workspace_id'] for w in core.remote_workspaces()], ['Harbor/w2'])
        self.assertEqual(core.remote_problems(), [{'machine': 'Worse', 'error': '5'}])
        (core.state_dir() / 'remote.json').write_text('[1, 2')
        self.assertEqual((core.remote_workspaces(), core.remote_problems()), ([], []))

    def test_a_hung_machine_does_not_hold_up_another(self):
        self.machines('Slow', 'Fast')
        (self.fake_dir / 'id-slow.json').write_text('SLOW 8')
        self.workspaces('Fast', ('w1', 'Here', 'idle'))
        state = self.new_state()
        with patch.object(core, 'REMOTE_TIMEOUT', 1), patch.object(core, 'REMOTE_WAIT', 0.3):
            started = time.monotonic()
            core.poll_remote(state)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual([w['workspace_id'] for w in core.remote_workspaces()], ['Fast/w1'])
            self.assertEqual(core.remote_problems(), [])
            time.sleep(1.4)
            core.poll_remote(state)
        self.assertEqual(core.remote_problems(), [{'machine': 'Slow', 'error': 'No answer within 1 seconds.'}])

    def test_duplicate_machine_labels_keep_the_first(self):
        (self.fake_dir / 'machines.json').write_text(json.dumps([
            {'id': 'a', 'label': 'Dup', 'enabled': True}, {'id': 'b', 'label': ' Dup ', 'enabled': True},
            {'id': 'c', 'label': 'Off', 'enabled': False}, {'id': 'd', 'label': '', 'enabled': True}, {'id': 5, 'label': 'Num', 'enabled': True}]))
        self.assertEqual(core.machines(), [{'id': 'a', 'label': 'Dup'}])

    def test_only_one_process_watches_at_a_time(self):
        self.machines('Harbor')
        self.workspaces('Harbor', ('w1', 'Harbor', 'idle'))
        core.state_dir().mkdir(parents=True)
        holder = (core.state_dir() / 'remote.lock').open('a')
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        stop = threading.Event()
        with patch.object(core, 'REMOTE_LOCK_RETRY_SECONDS', 0.1):
            thread = threading.Thread(target=core.poll_remote_forever, args=(stop,), daemon=True)
            self.addCleanup(stop.set)
            thread.start()
            time.sleep(0.8)
            self.assertEqual(core.remote_workspaces(), [])
            self.assertFalse(any('--machine' in call for call in self.calls()))
            fcntl.flock(holder, fcntl.LOCK_UN)
            deadline = time.time() + 5
            while not core.remote_workspaces() and time.time() < deadline:
                time.sleep(0.05)
            stop.set()
            thread.join(timeout=5)
        self.assertEqual(len(core.remote_workspaces()), 1)

    def test_the_loop_slows_down_when_there_are_no_machines(self):
        (self.fake_dir / 'machines.json').write_text('[]')
        self.assertEqual(core.poll_remote(self.new_state()), 0)
        self.machines('Harbor')
        self.workspaces('Harbor', ('w1', 'Harbor', 'idle'))
        self.assertEqual(core.poll_remote(self.new_state()), 1)
        (self.fake_dir / 'machines.json').write_text('FAIL')
        self.assertEqual(core.poll_remote(self.new_state()), 0)

    def test_an_idle_pass_does_not_rewrite_the_status_cache(self):
        state = self.new_state()
        self.poll(state, 'working')
        marker = (core.state_dir() / 'statuses.json').stat().st_ino
        self.poll(state, 'working')
        self.assertEqual((core.state_dir() / 'statuses.json').stat().st_ino, marker)

    def test_herdrs_wrapped_error_text_is_reduced_to_the_reason(self):
        self.fail('Harbor', 'Error: Custom { kind: ConnectionAborted, error: "machine \'Harbor\' (session default): remote SSH connection failed: ssh: Could not resolve hostname harbor.local" }')
        core.poll_remote(self.new_state())
        self.assertEqual(core.remote_problems()[0]['error'], "machine 'Harbor' (session default): remote SSH connection failed: ssh: Could not resolve hostname harbor.local")
        self.assertEqual(core.short_reason('first line\n\nsecond'), 'first line')
        self.assertEqual(core.short_reason(''), 'The machine did not answer.')
        self.assertEqual(len(core.short_reason('x' * 1000)), 300)

    def test_an_unreachable_machine_is_retried_later_not_every_pass(self):
        state = self.new_state()
        self.fail('Harbor')
        core.poll_remote(state)
        core.poll_remote(state)
        self.assertEqual(self.calls().count('--machine id-harbor workspace list'), 1)
        state['retry']['Harbor'] = 0
        core.poll_remote(state)
        self.assertEqual(self.calls().count('--machine id-harbor workspace list'), 2)

    def test_coming_back_resyncs_silently_then_plays_changes(self):
        state = self.new_state()
        self.poll(state, 'working')
        self.fail('Harbor')
        core.poll_remote(state)
        state['retry'].clear()
        self.poll(state, 'done').assert_not_called()
        self.assertEqual(core.remote_problems(), [])
        self.assertEqual(core.remote_workspaces()[1]['label'], 'Harbor → Tidepool')
        self.poll(state, 'working').assert_not_called()
        self.poll(state, 'done').assert_called_once()

    def test_one_unreachable_machine_does_not_stop_another(self):
        self.machines('Harbor', 'Tidepool')
        self.fail('Harbor')
        self.workspaces('Tidepool', ('w1', 'Elsewhere', 'idle'))
        core.poll_remote(self.new_state())
        self.assertEqual([w['workspace_id'] for w in core.remote_workspaces()], ['Tidepool/w1'])
        self.assertEqual([p['machine'] for p in core.remote_problems()], ['Harbor'])

    def test_removed_and_disabled_machines_leave_the_list_but_keep_their_choices(self):
        state = self.new_state()
        self.poll(state, 'working')
        rules = core.settings()['rules']
        self.machines('Harbor', disabled=('Harbor',))
        core.poll_remote(state)
        self.assertEqual(core.remote_workspaces(), [])
        self.assertEqual(core.settings()['rules'], rules)

    def test_when_herdr_cannot_list_machines_nothing_changes(self):
        self.poll(self.new_state(), 'working')
        before = (core.state_dir() / 'remote.json').read_bytes()
        (self.fake_dir / 'machines.json').write_text('FAIL')
        self.assertIsNone(core.machines())
        core.poll_remote(self.new_state())
        self.assertEqual((core.state_dir() / 'remote.json').read_bytes(), before)

    def test_an_idle_pass_writes_nothing(self):
        state = self.new_state()
        self.poll(state, 'working')
        marker = (core.state_dir() / 'remote.json').stat().st_mtime_ns
        time.sleep(0.01)
        self.poll(state, 'working')
        self.assertEqual((core.state_dir() / 'remote.json').stat().st_mtime_ns, marker)

    def test_a_failing_sound_does_not_stop_the_loop(self):
        state = self.new_state()
        self.poll(state, 'working')
        self.workspaces('Harbor', ('w1', 'Harbor', 'idle'), ('w4', 'Tidepool', 'done'))
        with patch.object(core, 'play', side_effect=ValueError('no player')):
            core.poll_remote(state)
        self.assertEqual(core.remote_workspaces()[1]['agent_status'], 'done')

    def test_poller_thread_stops_when_asked(self):
        stop = threading.Event()
        thread = threading.Thread(target=core.poll_remote_forever, args=(stop,), daemon=True)
        self.addCleanup(stop.set)
        self.workspaces('Harbor', ('w1', 'Harbor', 'idle'))
        thread.start()
        deadline = time.time() + 5
        while not core.remote_workspaces() and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(core.remote_workspaces()), 1)


class RemoteServerTests(RemoteFixture):
    def serve(self, *extra):
        environment = dict(os.environ)
        environment.pop('MEDLEY_RELAUNCH_TOKEN', None)
        process = subprocess.Popen([sys.executable, str(core.ROOT / 'medley.py'), 'settings', '--no-browser', '--port', '0', *extra],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (process.terminate(), process.communicate(timeout=5)))
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line.strip().split('Medley settings: ', 1)[1]) for line in process.stdout
                                         if line.startswith('Medley settings: ')], daemon=True).start()
        base, token = lines.get(timeout=10).split('/#')

        def request(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers={'X-Medley-Token': token}), timeout=5) as response:
                return json.load(response)
        return request

    def wait_for(self, request, condition):
        deadline = time.time() + 10
        while time.time() < deadline:
            api = request('/api')
            if condition(api):
                return api
            time.sleep(0.1)
        self.fail('the settings page never showed the expected workspaces: %r' % request('/api')['workspaces'])

    def test_the_settings_server_watches_saved_machines_and_serves_their_workspaces(self):
        self.machines('Harbor')
        self.workspaces('Harbor', ('w4', 'Tidepool', 'working'))
        request = self.serve()
        api = self.wait_for(request, lambda api: len(api['workspaces']) == 2)
        self.assertEqual([w['workspace_id'] for w in api['workspaces']], ['w1', 'Harbor/w4'])
        self.assertEqual(api['workspaces'][1]['label'], 'Harbor → Tidepool')
        self.assertEqual(api['machines'], [])
        request('/rule', {'workspace': 'Harbor/w4', 'status': 'done', 'choice': 'none'})
        request('/status-mute', {'workspace': 'Harbor/w4', 'status': 'blocked', 'muted': True})
        settings = request('/api')['settings']
        self.assertEqual(settings['rules'], {'Harbor/w4': {'done': 'none'}})
        self.assertEqual(settings['mutes'], {'Harbor/w4': ['blocked']})
        self.fail('Harbor')
        api = self.wait_for(request, lambda api: bool(api['machines']))
        self.assertEqual(api['workspaces'][1]['label'], 'Harbor → Tidepool (last seen)')

    def test_no_remote_leaves_the_machines_alone(self):
        self.machines('Harbor')
        self.workspaces('Harbor', ('w4', 'Tidepool', 'working'))
        request = self.serve('--no-remote')
        time.sleep(1.5)
        self.assertEqual([w['workspace_id'] for w in request('/api')['workspaces']], ['w1'])
        self.assertFalse(any('--machine' in call for call in self.calls()))


if __name__ == '__main__':
    unittest.main()
