# Changelog

## 0.3.0

- Replaced the `nwg-displays` handoff with a native GTK 4 display manager.
- Added draggable, normalized display arrangements and per-output controls.
- Added Extend, Mirror, External-only, and Internal-only quick modes.
- Added 15-second display confirmation with automatic rollback.
- Added EDID- and dock-aware setup profiles that survive connector renaming.
- Added an event-driven background service for automatic hotplug switching.
- Added the `hypr-control-display` status, remember, apply, and watch commands.

## 0.2.0

- Added transactional multi-file configuration writes and bounded backup history.
- Added external-change detection before settings and raw-editor saves.
- Added a persistent pending-change bar with Apply and Discard actions.
- Added search across settings, descriptions, and underlying configuration keys.
- Corrected Hypridle lock timing and added a chronological idle timeline.
- Added keyboard shortcut capture and normalized conflict detection.
- Added appearance presets and richer live overview information.
- Added raw-editor diff confirmation and backup restoration.
- Expanded configuration-engine coverage from 8 to 14 tests.
