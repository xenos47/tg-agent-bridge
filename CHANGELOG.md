# Changelog

## 0.1.1 — Unreleased

- Allow an explicit MTProto port for restricted hosted environments.
- Protect Telethon session files before the first network operation.
- Add safe structured stderr diagnostics with `--verbose` and `--quiet`.
- Add human-only `tgq watchlist resolve` to bootstrap peer IDs without writing
  unwatched peers or messages to the SQLite mirror.
- Add optional XDG user settings for database, watchlist, session, and Telegram
  port paths while preserving existing CLI and environment overrides.
- Add oneshot systemd/launchd templates and a non-blocking sync flock so a
  background timer can refresh the mirror without agents calling `tgq sync`.
- Add `sync.interval` / `TGQ_SYNC_INTERVAL` precedence for foreground `--loop`.
- Wire `rescan` into `tgq sync` (at most once per hour per peer, skipped during
  FloodWait cooldown) so edits and deletions in the last ~200 messages per
  peer reach the mirror. Fixes a window/batch-size mismatch that could have
  soft-deleted messages that still exist remotely. Adds migration `002`
  (`sync_state.last_rescan_at`).

## 0.1.0 — 2026-09-16

- Add the SQLite mirror and numbered migrations.
- Add incremental sync, independent backfill, rescan, and deterministic rules.
- Add `tgq` format version 1 with JSONL, Markdown, table, and count output.
- Add moderated outbox, persistent rate limits, audit records, and separate sender.

CLI output contract: `format_version=1`.
