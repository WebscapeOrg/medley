# Architecture

Medley has three entrypoints: a herdr event hook, a localhost settings server, and command-line enable/disable/import operations. It uses Python's standard library, herdr's public CLI and an installed audio decoder/player. The optional Node tooling is only for browser tests.

## Event handling

The manifest subscribes to `pane.agent_status_changed`. The hook reads `HERDR_PLUGIN_EVENT_JSON.data.workspace_id`, asks herdr for the authoritative combined workspace status, and compares it with a persistent cache. An exclusive file lock serializes cache updates across hooks. Initial observations and identical repeated states do not play. The lock is released before playback. A separate nonblocking playback lock prevents overlapping clips or an unbounded playback queue; transitions during another clip are recorded but their sound is dropped.

This approach deliberately coalesces rapid intermediate states. It does not claim a lossless event history. Playback is on the server host, independent of a client's focus. Multiple independent herdr sessions need separate plugin config/state directories to prevent ID collisions.

## Configuration

`HERDR_PLUGIN_CONFIG_DIR` contains `settings.json`, `library.json`, imported audio and the first pre-activation herdr config backup. `HERDR_PLUGIN_STATE_DIR` contains the aggregate-status cache and locks. Standalone commands default to herdr's XDG paths. State writes use a private temporary file, fsync and atomic replacement. Audio identities are SHA-256 hashes; imports are deduplicated. Sound names are display text, never filesystem paths or shell commands. Renaming validates a 1–160 character label and atomically updates only library.json under the existing library lock; audio hashes, files and workspace rules remain unchanged.

Replacing a sound (`/replace`) validates and converts the upload exactly as an import does, but its file name is only used to pick MP3 or WAV validation and is never stored. Because identities are content hashes, the new audio has a new hash. Under the library lock Medley writes the display name for the new hash (invisible until its file exists), adds the new file, rewrites every rule that used the old hash, then removes the old name and file. A failure part way leaves at worst two entries with the same name, and repeating the replace finishes the swap. Mutes are keyed by workspace and status, so they are untouched. Uploading the sound's current audio changes nothing; audio already held by a differently named sound is refused rather than merged, so no name is overwritten. A hook that resolves a sound in the instant it is replaced can miss that one clip, as with Remove. After a replace the page rewrites the hashes in its pending "Undo reset" copy, which would otherwise name audio that no longer exists.

Rules are keyed by workspace ID and status. A missing cell means default, `none` means quiet, and a hash selects a library file. Defaults use herdr's current custom sound paths or the Apache-licensed bundled clips. No private user sounds ship in the public repo.

The optional `mutes` map in settings.json holds a list of muted statuses per workspace. It never overwrites sound rules. `/status-mute` sets or clears one status, or all five atomically, under the same settings lock. Resolution returns quiet before looking up the saved choice. Reset and sound removal still change the saved rule; unmuting uses that current rule. The event hook records status changes while muted and rereads settings after querying herdr, so unmuting never replays an old transition. Global timed mute remains separate in mute.json.

The settings page disables workspace and status mute buttons during a global mute. A workspace with all five statuses muted disables its status mute buttons until Unmute all. The existing `data-unavailable` state keeps these controls disabled after requests finish or fail. At a global mute's deadline, an authenticated read refreshes only the mute display and button availability; it preserves open editors and previews. A stale reply cannot replace a newer mute, and availability updates cannot enable controls while an action is pending. Read failures retry after five seconds. If muting the final status disables the focused button, focus moves to the workspace's Unmute all button.

## Sound ownership

Activation seeds the baseline and changes only `ui.sound.enabled` in herdr's config, validates TOML and asks herdr to reload. It stores the previous enabled value and restores it on disable. Validation/reload failures restore the original file. Other user config edits are preserved by targeted table editing. Disabling/unlinking a plugin through herdr bypasses its code, so users must disable Medley playback first; README includes manual recovery.

## Settings interface

The server binds only to 127.0.0.1, on port 43127 by default (port 0 explicitly requests an ephemeral port). The launch URL has a persistent random token in its fragment; JavaScript retains the fragment for bookmarks, stores the token in tab session storage, and supplies it in an API header. The access key is stored with owner-only permissions in settings-access.json. Relaunch re-executes only the settings process on the same port with the same key. All reads of configuration and all mutations require the token. No CORS permission is issued. Static assets contain no secrets. CSP limits scripts/styles to local assets and preview media to blobs. User-controlled labels use textContent, not HTML interpolation.

Uploads are limited to 20 MB and validated with ffprobe. WAV input is converted locally with ffmpeg to MP3, under a 30-second timeout and output-size limit, before entering the private library. Previews start native browser audio playback on request and retain the controls for stopping or replaying. Quiet selections stop playback; rejected playback requests show a recoverable error. Event playback invokes installed players with argument arrays and a 30-second timeout. There is no shell evaluation or external network request at runtime.

The Medley uninstall action restores the saved herdr sound setting first, then invokes herdr plugin uninstall medley. Failure to restore prevents unregistering. Standard herdr removal commands have no cleanup hook and bypass this action.
