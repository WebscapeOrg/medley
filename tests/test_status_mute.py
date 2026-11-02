import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medley import core


class StatusMuteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config = root / 'herdr.toml'
        config.write_text('[ui.sound]\n')
        environment = patch.dict(os.environ, {
            'HERDR_PLUGIN_CONFIG_DIR': str(root / 'config'),
            'HERDR_PLUGIN_STATE_DIR': str(root / 'state'),
            'HERDR_CONFIG_PATH': str(config),
        })
        environment.start()
        self.addCleanup(environment.stop)
        audio = core.config_dir() / 'audio'
        audio.mkdir(parents=True)
        self.key = 'a' * 64
        (audio / (self.key + '.mp3')).write_bytes(b'synthetic sound')

    def test_status_mute_restores_custom_default_and_quiet_choices(self):
        for choice in (self.key, 'default', 'none'):
            with self.subTest(choice=choice):
                original = core.set_rule('w1', 'done', choice)
                original_path = core.resolve(original, 'w1', 'done')
                core.set_status_mute('w1', 'done', True)
                core.set_status_mute('w1', 'done', True)
                data = core.settings()
                self.assertEqual(data['rules'], original['rules'])
                self.assertIsNone(core.resolve(data, 'w1', 'done'))
                self.assertEqual(core.resolve(data, 'w1', 'blocked'), core.ROOT / 'assets/request.mp3')
                self.assertEqual(core.resolve(data, 'w2', 'done'), core.ROOT / 'assets/done.mp3')
                core.set_status_mute('w1', 'done', False)
                core.set_status_mute('w1', 'done', False)
                self.assertEqual(core.resolve(core.settings(), 'w1', 'done'), original_path)
                self.assertEqual(core.settings()['mutes'], {})

    def test_workspace_mute_and_individual_unmute_preserve_every_choice(self):
        for status, choice in zip(core.STATUSES, (self.key, self.key, 'none', 'default', self.key)):
            core.set_rule('w1', status, choice)
        original = core.settings()
        paths = [core.resolve(original, 'w1', status) for status in core.STATUSES]
        core.set_status_mute('w2', 'blocked', True)
        core.set_status_mute('w1', 'done', True)
        data = core.set_status_mute('w1', 'all', True)
        self.assertTrue(all(core.resolve(data, 'w1', status) is None for status in core.STATUSES))
        self.assertEqual(data['rules'], original['rules'])
        data = core.set_status_mute('w1', 'idle', False)
        self.assertEqual(core.resolve(data, 'w1', 'idle'), paths[0])
        self.assertIsNone(core.resolve(data, 'w1', 'done'))
        data = core.set_status_mute('w1', 'all', False)
        self.assertEqual([core.resolve(data, 'w1', status) for status in core.STATUSES], paths)
        self.assertEqual(data['mutes'], {'w2': ['blocked']})

    def test_global_mute_is_independent(self):
        core.set_status_mute('w1', 'done', True)
        core.mute(0)
        core.unmute()
        self.assertIsNone(core.resolve(core.settings(), 'w1', 'done'))
        core.mute(0)
        core.set_status_mute('w1', 'all', False)
        self.assertIsNotNone(core.muted())

    def test_reset_and_removal_do_not_resurrect_old_choices_on_unmute(self):
        core.set_rule('w1', 'done', self.key)
        core.set_status_mute('w1', 'done', True)
        core.remove_audio(self.key)
        self.assertIsNone(core.resolve(core.settings(), 'w1', 'done'))
        core.set_status_mute('w1', 'done', False)
        self.assertEqual(core.resolve(core.settings(), 'w1', 'done'), core.ROOT / 'assets/done.mp3')
        core.set_rule('w1', 'done', 'none')
        core.set_status_mute('w1', 'done', True)
        core.set_rule('w1', 'done', 'default')
        core.set_status_mute('w1', 'done', False)
        self.assertEqual(core.resolve(core.settings(), 'w1', 'done'), core.ROOT / 'assets/done.mp3')

    def test_bad_mute_inputs_do_not_change_settings(self):
        original = core.set_status_mute('w1', 'done', True)
        for workspace, status, muted in [('', 'done', True), (None, 'all', True),
                                          ('w' * 201, 'done', True), ('w1', 'invalid', True),
                                          ('w1', None, True), ('w1', 'all', 1), ('w1', 'done', 'false')]:
            with self.subTest(workspace=workspace, status=status, muted=muted):
                with self.assertRaises(ValueError):
                    core.set_status_mute(workspace, status, muted)
                self.assertEqual(core.settings(), original)

    def test_muted_events_are_tracked_and_do_not_replay_on_unmute(self):
        core.update(lambda data: data.update(enabled=True))
        payload = {'data': {'workspace_id': 'w1'}}
        with patch.object(core, 'workspaces') as workspaces, patch.object(core, 'play') as play:
            workspaces.return_value = [{'workspace_id': 'w1', 'agent_status': 'working'}]
            core.event(payload)
            core.set_status_mute('w1', 'all', True)
            workspaces.return_value = [{'workspace_id': 'w1', 'agent_status': 'done'}]
            core.event(payload)
            play.assert_called_once_with(None)
            core.set_status_mute('w1', 'all', False)
            play.reset_mock()
            core.event(payload)
            play.assert_not_called()
            workspaces.return_value = [{'workspace_id': 'w1', 'agent_status': 'blocked'}]
            core.event(payload)
            play.assert_called_once_with(core.ROOT / 'assets/request.mp3')

    def test_mute_during_workspace_query_takes_effect_before_playback(self):
        core.update(lambda data: data.update(enabled=True))
        core.write_json(core.state_dir() / 'statuses.json', {'w1': 'working'})
        def workspaces():
            core.set_status_mute('w1', 'done', True)
            return [{'workspace_id': 'w1', 'agent_status': 'done'}]
        with patch.object(core, 'workspaces', side_effect=workspaces), patch.object(core, 'play') as play:
            core.event({'data': {'workspace_id': 'w1'}})
            play.assert_called_once_with(None)
