# Medley

A public herdr plugin. Keep private audio, workspace names, tokens and machine settings out of this repository.

Run `python3 -m unittest discover -s tests -v` before committing. Use synthetic workspace fixtures in screenshots. Do not stop herdr or its agents. The plugin runs on the herdr server; do not promise remote-client playback. Keep the settings API loopback-only and require its random token for all data and mutations. Use argv subprocess calls, never shell interpolation.

The plugin owns its audio library and settings in HERDR_PLUGIN_CONFIG_DIR. Herdr owns its own configuration; Medley may change only ui.sound.enabled to take over or restore playback. Preserve unrelated settings. Keep README behavior and limitations accurate.
