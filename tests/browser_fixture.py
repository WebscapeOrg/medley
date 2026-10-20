"""Synthetic data only. Run from the repository root for browser checks."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from medley import core
from medley.app import serve

with tempfile.TemporaryDirectory() as directory:
    os.environ['HERDR_PLUGIN_CONFIG_DIR'] = directory
    os.environ['HERDR_CONFIG_PATH'] = str(Path(directory) / 'herdr.toml')
    Path(os.environ['HERDR_CONFIG_PATH']).write_text('[ui.sound]\n')
    core.workspaces = lambda: [
        {'workspace_id': 'w1', 'label': 'Example project', 'agent_status': 'idle'},
        {'workspace_id': 'w2', 'label': 'Research', 'agent_status': 'working'},
    ]
    core.uninstall = lambda: core.update(lambda data: data.update(enabled=False))
    serve(False)
