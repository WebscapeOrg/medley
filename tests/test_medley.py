import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from medley import core


class MedleyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'herdr.toml'
        self.config.write_text('[ui.sound]\npath = "custom.mp3"\n')
        self.env = patch.dict(os.environ, {'HERDR_PLUGIN_CONFIG_DIR': str(self.root / 'config'),
                                          'HERDR_PLUGIN_STATE_DIR': str(self.root / 'state'),
                                          'HERDR_CONFIG_PATH': str(self.config)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_defaults(self):
        self.assertFalse(core.settings()['enabled'])
        self.assertEqual(core.resolve({}, 'w1', 'done'), core.ROOT / 'assets/done.mp3')
        self.assertEqual(core.resolve({}, 'w1', 'blocked'), core.ROOT / 'assets/request.mp3')
        self.assertIsNone(core.resolve({}, 'w1', 'working'))
        self.assertIsNone(core.resolve({}, 'w1', 'unknown'))

    def test_configured_defaults_and_specific_status(self):
        (self.root / 'custom.mp3').write_bytes(b'audio')
        self.assertEqual(core.resolve({}, 'w1', 'idle'), self.root / 'custom.mp3')
        self.config.write_text('[ui.sound]\npath="custom.mp3"\nrequest_path="attention.mp3"\n')
        (self.root / 'attention.mp3').write_bytes(b'attention')
        self.assertEqual(core.resolve({}, 'w1', 'blocked'), self.root / 'attention.mp3')

    def test_rule_reset_and_workspace_isolation(self):
        data = core.set_rule('w1', 'done', 'none')
        self.assertIsNone(core.resolve(data, 'w1', 'done'))
        self.assertIsNotNone(core.resolve(data, 'w2', 'done'))
        self.assertEqual(core.set_rule('w1', 'done', 'default')['rules'], {})

    def test_invalid_rules(self):
        for status, choice in [('invalid', 'none'), ('done', '../../file'), ('done', 'missing')]:
            with self.assertRaises(ValueError):
                core.set_rule('w1', status, choice)

    def test_rename_preserves_audio_and_all_assignments(self):
        content = (core.ROOT / 'assets/done.mp3').read_bytes()
        key = core.import_audio('old.mp3', content)
        core.set_rule('w1', 'done', key)
        core.set_rule('w2', 'idle', key)
        before = core.settings()
        core.rename_audio(key, '  Bell & <chime>.MP3  ')
        self.assertEqual(core.library(), [{'id': key, 'name': 'Bell & <chime>'}])
        self.assertEqual(core.settings(), before)
        self.assertEqual(core.resolve(before, 'w2', 'idle').read_bytes(), content)
        for invalid in ['', '  ', '.mp3', 'x' * 161, 'a\nb', None]:
            with self.assertRaises(ValueError):
                core.rename_audio(key, invalid)
        for invalid in ['../escape', '0' * 64, None]:
            with self.assertRaises(ValueError):
                core.rename_audio(invalid, 'New name')
        self.assertEqual(core.library()[0]['name'], 'Bell & <chime>')

    def test_remove_resets_all_uses_and_preserves_other_rules(self):
        key = core.import_audio('remove.mp3', (core.ROOT / 'assets/done.mp3').read_bytes())
        core.set_rule('w1', 'done', key)
        core.set_rule('w2', 'idle', key)
        core.set_rule('w2', 'working', 'none')
        core.remove_audio(key)
        self.assertEqual(core.settings()['rules'], {'w2': {'working': 'none'}})
        self.assertEqual(core.library(), [])
        self.assertEqual(core.resolve(core.settings(), 'w1', 'done'), core.ROOT / 'assets/done.mp3')
        with self.assertRaises(ValueError):
            core.set_rule('w1', 'done', key)
        for invalid in ['../../file', key]:
            with self.assertRaises(ValueError):
                core.remove_audio(invalid)

    def test_wav_import_converts_to_mp3(self):
        import io
        import wave
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\x00\x00' * 1600)
        key = core.import_audio('example.WAV', stream.getvalue())
        audio = core.config_dir() / 'audio' / (key + '.mp3')
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name', '-of', 'json', str(audio)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['streams'][0]['codec_name'], 'mp3')
        self.assertEqual(core.library()[0]['name'], 'example.mp3')

    def test_uninstall_restores_before_unregistering(self):
        calls = []
        with patch.object(core, 'activate', side_effect=lambda value: calls.append(('enabled', value))), patch.object(core.subprocess, 'run', side_effect=lambda *args, **kwargs: calls.append(args[0])):
            core.uninstall()
        self.assertEqual(calls[0], ('enabled', False))
        self.assertEqual(calls[1][-3:], ['plugin', 'uninstall', 'medley'])

    def test_failed_restore_prevents_uninstall(self):
        with patch.object(core, 'activate', side_effect=ValueError('restore failed')), patch.object(core.subprocess, 'run') as command:
            with self.assertRaises(ValueError):
                core.uninstall()
            command.assert_not_called()

    def test_invalid_import(self):
        for name, content in [('file.wav', b'x'), ('file.mp3', b''), ('file.mp3', b'invalid audio')]:
            with self.assertRaises(ValueError):
                core.import_audio(name, content)
        self.assertEqual(core.library(), [])

    def test_import_deduplicates_and_copies(self):
        content = (core.ROOT / 'assets/done.mp3').read_bytes()
        key = core.import_audio('friendly name.mp3', content)
        self.assertEqual(key, core.import_audio('friendly name.mp3', content))
        self.assertEqual(len(core.library()), 1)
        data = core.set_rule('w1', 'working', key)
        self.assertEqual(core.resolve(data, 'w1', 'working').read_bytes(), content)

    def test_event_dedup_and_initial_silence(self):
        core.update(lambda data: data.update(enabled=True))
        payload = {'event':'pane_agent_status_changed','data':{'workspace_id':'w1'}}
        with patch.object(core, 'workspaces') as workspaces, patch.object(core, 'play') as play:
            workspaces.return_value = [{'workspace_id':'w1','agent_status':'working'}]
            core.event(payload)
            play.assert_not_called()
            workspaces.return_value = [{'workspace_id':'w1','agent_status':'done'}]
            core.event(payload)
            core.event(payload)
            self.assertEqual(play.call_count, 1)

    def test_disabled_does_not_query_or_play(self):
        with patch.object(core, 'workspaces') as workspaces:
            core.event({'data':{'workspace_id':'w1'}})
            workspaces.assert_not_called()

    def test_enable_disable_restores_sound_setting_and_preserves_other_config(self):
        self.config.write_text('[theme]\nname="terminal"\n[ui.sound]\npath="custom.mp3"\n[ui.toast]\ndelivery="off"\n')
        with patch.object(core.subprocess, 'run') as command, patch.object(core, 'workspaces', return_value=[]):
            core.activate(True)
            self.assertFalse(core.herdr_config()[1]['ui']['sound']['enabled'])
            self.assertTrue(core.settings()['enabled'])
            core.activate(False)
            data = core.herdr_config()[1]
            self.assertTrue(data['ui']['sound']['enabled'])
            self.assertEqual(data['ui']['toast']['delivery'], 'off')
            self.assertEqual(data['theme']['name'], 'terminal')

    def test_activation_failure_rolls_back(self):
        before = self.config.read_bytes()
        with patch.object(core.subprocess,'run',side_effect=[subprocess.CalledProcessError(1,'herdr'),None]):
            with self.assertRaises(subprocess.CalledProcessError):
                core.activate(True)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(core.settings()['enabled'])

    def test_atomic_json(self):
        core.update(lambda data: data.update(enabled=True))
        self.assertEqual(core.settings()['rules'], {})
        self.assertEqual((core.config_dir()/'settings.json').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
