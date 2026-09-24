# tg-agent-bridge

Local bridge between a Telegram user account and AI agents. The agent never talks to Telegram — it talks to a SQLite mirror through the CLI `tgq`.

## Status

v0.1 is implemented for Python 3.12 and 3.13 on macOS and Linux. It includes
the SQLite mirror, offline-tested incremental sync, deterministic read CLI,
and moderated outbox.

Local agent instructions and workflows are intentionally excluded from version control.

## Why

Three properties drive the design:

1. **Cheap local search** — queries hit SQLite, not the Telegram API or an LLM context window on every lookup.
2. **Human control of egress** — nothing leaves the machine as a Telegram message without an explicit human approve step.
3. **Every write goes through outbox** — including retries and “utility” messages. There is no side path that calls `send_message` directly.

## How it works

Dependencies are one-way. Telethon exists only in the sync layer; the CLI must not import it.

```mermaid
flowchart TD
  Telegram[Telegram MTProto]
  Sync["tgbridge/sync Telethon only"]
  DB[SQLite mirror]
  CLI["tgq CLI no Telethon"]
  Agent[Agent or human]
  Telegram --> Sync
  Sync -->|"writes"| DB
  DB -->|"reads"| CLI
  Agent --> CLI
```

- **`tgbridge/sync/`** — the only code that knows about Telegram. Timer-based incremental sync into SQLite.
- **`tgbridge/db/`** — schema and migrations; the contract between layers.
- **`tgbridge/cli/`** — `tgq`. Reads the mirror, never MTProto. Fast startup, tests against fixture DBs with no network.

## Install and configure

```bash
uv sync --dev
mkdir -p "$HOME/.config/tgq" "$HOME/.local/share/tgq"
cp config.example.yaml "$HOME/.config/tgq/config.yaml"
cp watchlist.example.yaml "$HOME/.config/tgq/watchlist.yaml"
export TGQ_API_ID="..."
export TGQ_API_HASH="..."
```

Credentials may instead be stored in `~/.config/tgq/secrets.env`, which must
have mode `0600`. They are never accepted in `config.yaml`. The example settings
file keeps the unencrypted mirror and Telethon session under
`~/.local/share/tgq/`; keep both local and out of cloud-synced folders.

The optional non-secret settings file is discovered at
`${XDG_CONFIG_HOME:-~/.config}/tgq/config.yaml`:

```yaml
paths:
  db: ~/.local/share/tgq/messages.sqlite
  watchlist: ~/.config/tgq/watchlist.yaml
  session: ~/.local/share/tgq/tgq.session

telegram:
  port: 443

sync:
  interval: 60
```

Paths in this file expand `~` and environment variables. Relative paths are
resolved from the settings file directory. Override the file itself with
`--settings PATH` or `TGQ_SETTINGS`. Value precedence is CLI option, existing
`TGQ_*` environment variable, user settings, then the previous default.
Existing path exports and repository-local `watchlist.yaml` / `tgq.session`
work unchanged when no settings file exists. For foreground `tgq sync --loop`,
`--interval` / `TGQ_SYNC_INTERVAL` / `sync.interval` share that precedence
(default 60; the engine still floors at once per minute per peer).

Resolve a peer named by the human before the first sync. This command reads only
peer metadata, does not require `--db`, and does not add anything to the mirror:

```bash
uv run tgq watchlist resolve @username
uv run tgq watchlist resolve "Work chat" --write --dry-run
uv run tgq watchlist resolve "Work chat" --write
```

Queries may be an `@username`, `t.me` link, numeric peer id, or title substring.
An ambiguous title prints candidates and leaves the config unchanged. `--write`
is a human-only bootstrap action that appends the resolved peer with sending
disabled; the peer enters SQLite only after a later `sync`.

The default MTProto port is `443`. Some hosted environments classify ports 80
and 443 as HTTP and intercept raw MTProto. Cursor Grok Bot computers currently
allow the same Telegram transport on port 5222:

```bash
TGQ_TELEGRAM_PORT=5222 uv run tgq sync
```

The environment variable overrides user settings and the legacy
`telegram.port` field in existing `watchlist.yaml` files.
HTTP(S) proxy variables do not carry raw MTProto; the port override is explicit
and never triggers automatic port cycling.

## Background sync (timer)

Agents should read the mirror (`search`, `digest`, `tail`, `peers`, `doctor`)
and must not call `tgq sync` — that wastes tokens and races the Telethon
session. Populate the mirror with an OS oneshot timer that runs `tgq sync`
every five minutes. Templates live under `contrib/`; edit the `tgq` path, then
enable locally if you want (this repo does not enable them for you).

**systemd (user):**

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/tgq-sync.service contrib/systemd/tgq-sync.timer \
  ~/.config/systemd/user/
# Edit ExecStart to an absolute tgq, or:
#   ExecStart=/usr/bin/uv run --directory /path/to/tg-agent-bridge tgq sync
systemctl --user daemon-reload
systemctl --user enable --now tgq-sync.timer
systemctl --user status tgq-sync.timer
```

**launchd (macOS):**

```bash
cp contrib/launchd/com.tgq.sync.plist ~/Library/LaunchAgents/
# Edit ProgramArguments to an absolute tgq or uv run --directory ...
launchctl load ~/Library/LaunchAgents/com.tgq.sync.plist
launchctl list com.tgq.sync
```

Overlapping runs take a non-blocking flock next to the session file
(`sync.lock`). If another sync holds the lock, the new process logs and exits 0
so the timer stays green. Prefer oneshot ticks over a long-lived
`tgq sync --loop` so the session is released between runs (resolve / sender can
reuse it). `--loop` remains a foreground helper for manual debugging.

`tgq sync` also rescans the most recent ~200 messages of each peer, at most
once per hour per peer and never during a FloodWait/error cooldown, to pick up
edits and deletions that happened after the original fetch. Rescan rewrites
only messages already in the mirror that actually changed; new messages are
left to the incremental cursor. Edits and deletions older than that window are
not detected, and changes inside it can take up to an hour to appear — this
trades completeness for a bounded number of extra API calls.

## CLI

```bash
# Human / daemon: populate the mirror (watchlist.yaml only). Prefer a timer.
uv run tgq sync

# Agent: search the local mirror — do not run sync
uv run tgq --format jsonl search --tag urgent --since 24h
uv run tgq search --peer work-chat --q "release OR deploy" --limit 20

# Propose a send — writes outbox only; does not transmit
uv run tgq send --peer lena --body "..."

# Human approval; a separate sender process transmits only status=approved
uv run tgq outbox approve 17
uv run tgq outbox send
```

Other commands: `thread`, `tail`, `digest`, `peers`, `tag`, `retag`, `doctor`,
and `outbox list|reject`.

Diagnostics are structured JSON lines on stderr and never change command data
on stdout. Use `--verbose` before the command for transport, sync, and sender
events, or `--quiet` to suppress project diagnostics:

```bash
uv run tgq --verbose sync
uv run tgq --quiet search --peer work-chat
```

Writes support `--dry-run`. Secrets belong in the environment or `~/.config/tgq/secrets.env` (mode `0600`), never in the repo. Sync scope is `watchlist.yaml`; send requires `allow_send` in config **and** `peers.sendable`.

## Data model (summary)

| Rule | Why |
|------|-----|
| Message PK is `(peer_id, msg_id)` | `msg_id` is unique only within a dialog |
| Soft delete via `deleted_at`, set by rescan | Agents that already cited a handle still get a clear answer |
| Tags in a separate table with `source` | Distinguish rule / manual / agent tags; never a CSV field |
| Always store `raw_json` | Retag and new fields without re-downloading history |
| Time is UTC unix int | Keeps `--since` comparisons and indexes honest |
| `sync_state` moves in the same transaction as the batch | Avoid holes or infinite replays after a crash |
| Only path out is `outbox` with `status=approved` | No direct Telethon send |
| Peer `slug` is immutable after insert | `slug#msg_id` citations must not break |

The database is **unencrypted** and holds private chat history. Do not put it in cloud-synced folders; treat the file like sensitive mail.

## Safety and ToS

This is a userbot acting as a real person. Defaults and hard boundaries:

- Sync **only** peers listed in `watchlist.yaml`. Absence from the DB is the privacy boundary — not a read-time filter.
- Account is **read-only** until send is enabled in config and each message is approved. `approve` and `reject` are human-only by contract and agent instructions, not an OS sandbox: a same-UID process can still invoke those commands. The technical boundary is process split — enqueue cannot transmit, and the sender reads only `status=approved`.
- Telethon session file: mode `0600`, gitignored, never logged. Log formatters redact secrets centrally.
- Every write action is recorded in `audit`.

**Will not implement** (even “just for a test”):

- Mass sends or send-to-list
- Unattended auto-replies
- Syncing or scraping peers outside the watchlist
- Aggressive polling faster than about once per minute per peer
- FloodWait evasion via session/account rotation

Telegram limits accounts by behavior. Convenience is not worth a locked human account.

## Development notes

- Tests never hit the network. Sync uses a mocked client and fixture messages; CLI uses a temporary DB filled by factories.
- Sync bugs: put the problematic `raw_json` in a fixture and write a failing test before changing the daemon. Live-API debug loops invite FloodWait.
- CLI stdout is a public API consumed by agents. Adding fields is fine; renaming or removing requires a `--format-version` bump and a CHANGELOG entry.
- Any write path (`sync`, `send`, bulk tag) must support `--dry-run`.
- No ORM. Open connections with `journal_mode=WAL` and `busy_timeout=5000` so the daemon can write while agents read.
- FTS5 is external-content over `messages.text`, kept in sync with triggers.

Run all checks:

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run basedpyright
```

## Intentionally not in v1

Closed decisions — do not “improve” them without an explicit product change:

- **No MCP server in the core.** Interface is CLI only; MCP can sit on top later.
- **No real-time update stream.** Timer sync trades minutes of lag for a simple, restartable daemon.
- **No LLM on the hot path.** Auto-tagging is rule-based at write time. Optional selective LLM tagging is an explicit command, not the message pipeline.
- **No second store.** SQLite until measured pain says otherwise.
- **No OS-level human-presence for approve.** Touch ID / polkit are out of v0.1. Approval is a confused-deputy control (process split + human workflow), not a sandbox.

## License

MIT. Copyright (c) 2026 Igor Kropochev. Contributions are inbound=outbound under the same license; there is no CLA.
