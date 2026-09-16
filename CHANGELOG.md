# Changelog

## 0.1.1 — Unreleased

- Allow an explicit MTProto port for restricted hosted environments.
- Protect Telethon session files before the first network operation.

## 0.1.0 — 2026-09-16

- Add the SQLite mirror and numbered migrations.
- Add incremental sync, independent backfill, rescan, and deterministic rules.
- Add `tgq` format version 1 with JSONL, Markdown, table, and count output.
- Add moderated outbox, persistent rate limits, audit records, and separate sender.

CLI output contract: `format_version=1`.
