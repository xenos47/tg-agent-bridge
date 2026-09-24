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
  soft-deleted messages that still exist remotely. Rescan rewrites only
  mirrored rows that changed, never inserts ahead of the incremental cursor,
  and `--dry-run` logs the edits and deletions it would apply. Adds migration
  `002` (`sync_state.last_rescan_at`).
- Add per-peer sync policies in `watchlist.yaml` (`policies`, `default_policy`,
  `peers[].policy`): `history` limits backfill depth and `retention`
  hard-deletes older messages. Watchlists without policies now backfill 14
  days instead of the whole history and never prune. Adds migration `003`
  (`sync_state.backfill_cutoff_ts`).
- Fix a fresh peer starting incremental sync from its oldest message, which
  delayed new messages until the cursor crawled through the whole history.

## 0.1.0 — 2026-09-16

- Add the SQLite mirror and numbered migrations.
- Add incremental sync, independent backfill, rescan, and deterministic rules.
- Add `tgq` format version 1 with JSONL, Markdown, table, and count output.
- Add moderated outbox, persistent rate limits, audit records, and separate sender.

CLI output contract: `format_version=1`.
