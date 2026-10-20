# Architecture

Medley has three entrypoints: a herdr event hook, a localhost settings server, and command-line enable/disable/import operations. It uses Python's standard library, herdr's public CLI and an installed audio decoder/player. The optional Node tooling is only for browser tests.

## Event handling

The manifest subscribes to `pane.agent_status_changed`. The hook reads `HERDR_PLUGIN_EVENT_JSON.data.workspace_id`, asks herdr for the authoritative combined workspace status, and compares it with a persistent cache. An exclusive file lock serializes cache updates across hooks. Initial observations and identical repeated states do not play. The lock is released before playback. A separate nonblocking playback lock prevents overlapping clips or an unbounded playback queue; transitions during another clip are recorded but their sound is dropped.

This approach deliberately coalesces rapid intermediate states. It does not claim a lossless event history. Playback is on the server host, independent of a client's focus. Multiple independent herdr sessions need separate plugin config/state directories to prevent ID collisions.

## Configuration

`HERDR_PLUGIN_CONFIG_DIR` contains `settings.json`, `library.json`, imported audio and the first pre-activation herdr config backup. `HERDR_PLUGIN_STATE_DIR` contains the aggregate-status cache and locks. Standalone commands default to herdr's XDG paths. State writes use a private temporary file, fsync and atomic replacement. Audio identities are SHA-256 hashes; imports are deduplicated. The original file name is display text, never a filesystem path or shell command.

Rules are keyed by workspace ID and status. A missing cell means default, `none` means quiet, and a hash selects a library file. Defaults use herdr's current custom sound paths or the Apache-licensed bundled clips. No private user sounds ship in the public repo.

## Sound ownership

Activation seeds the baseline and changes only `ui.sound.enabled` in herdr's config, validates TOML and asks herdr to reload. It stores the previous enabled value and restores it on disable. Validation/reload failures restore the original file. Other user config edits are preserved by targeted table editing. Disabling/unlinking a plugin through herdr bypasses its code, so users must disable Medley playback first; README includes manual recovery.

## Settings interface

The server binds only to 127.0.0.1 on an ephemeral port. The launch URL has a random token in its fragment; JavaScript stores it in tab session storage, removes the fragment, and supplies it in an API header. All reads of configuration and all mutations require the token. No CORS permission is issued. Static assets contain no secrets. CSP limits scripts/styles to local assets and preview media to blobs. User-controlled labels use textContent, not HTML interpolation.

Uploads are limited to 20 MB and decoded with ffprobe before being copied into the private library. Previews start native browser audio playback on request and retain the controls for stopping or replaying. Quiet selections stop playback; rejected playback requests show a recoverable error. Event playback invokes installed players with argument arrays and a 30-second timeout. There is no shell evaluation or external network request at runtime.
