# mailattachments2dropbox

A lightweight headless worker that ingests email attachments from an IMAP mailbox
and stores them in Dropbox under a configurable folder taxonomy.

The intended use case is accounting: a single mailbox receives invoices (either
sent directly to it or forwarded with `Forward as attachment` from Gmail) and
this worker routes the PDF attachments into the correct Dropbox subfolder of a
bookkeeping app. Routing is driven by a short token in the mail body, not the
subject, so the original subject stays intact.

## Features

- Connects to any IMAP mailbox (TLS, IDLE plus polling fallback).
- Parses MIME bodies recursively, so attachments nested inside forwarded
  `message/rfc822` parts are surfaced and uploaded.
- Routes attachments by a body sentinel (`:::in:::paypal`, `:::out`, ...).
  Branches and subfolders are defined in `mapping.yaml` and resolved at runtime.
- Sender allow-list with glob patterns (`*@example.com`); unknown senders are
  deleted (configurable) and the rejection is logged for the audit trail.
- Attachment whitelist by file extension. Default: PDF only.
- Dropbox uploads with autorename, so duplicate filenames never overwrite an
  existing invoice.
- Idempotent mail lifecycle: keep, move to a `Processed` folder, or delete on
  success. Operator-friendly rejection behaviour on routing errors (mail stays
  in the inbox).
- Structured JSON logging to stdout and a rotating audit logfile
  (10 MB per file, 10 backups, files older than 30 days are pruned at startup).
- Single container, `restart: always`, no extra dependencies.

## Quickstart

```bash
git clone https://github.com/<your-org>/mailattachments2dropbox.git
cd mailattachments2dropbox

cp .env.example .env
cp mapping.yaml.example mapping.yaml

# 1. Create a Dropbox app and obtain a refresh token (see below).
uv sync --extra dev
uv run python scripts/setup_dropbox.py
# Paste APP_KEY, APP_SECRET into the prompt, follow the browser, then copy
# the three lines the script prints into your .env.

# 2. Edit .env (IMAP credentials, allow-list, sentinel defaults) and mapping.yaml
#    so that your Dropbox folder taxonomy is mirrored.

# 3. Run.
docker compose up -d --build
docker compose logs -f
```

## Configuration

### `.env` (process settings)

See [`.env.example`](.env.example) for the full list. The interesting knobs:

| Variable | Default | Notes |
|---|---|---|
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD` | required | Standard IMAP credentials. For Gmail or any provider with 2FA use an app-specific password. |
| `IMAP_USE_TLS` | `true` | Disable only when the server explicitly requires plain IMAP. |
| `IMAP_FOLDER` | `INBOX` | Which folder to watch. |
| `IMAP_USE_IDLE` | `true` | IDLE plus polling fallback. Set to `false` to use pure polling. |
| `IMAP_POLL_INTERVAL_SECONDS` | `300` | Polling cadence when IDLE is off or after a disconnect. |
| `MAIL_ON_SUCCESS` | `delete` | `keep`, `move_processed`, or `delete`. Successful uploads trigger this. |
| `MAIL_ON_REJECT` | `delete` | What to do with mails from disallowed senders. |
| `PROCESSED_FOLDER` | `Processed` | Target folder name for `move_processed`. Created on demand. |
| `ALLOWED_SENDERS` | empty | CSV of addresses or glob patterns. Empty disables the check. |
| `ALLOWED_EXTENSIONS` | `pdf` | CSV of file extensions (no leading dot). Empty allows everything. |
| `SENTINEL_PREFIX`, `SENTINEL_SEPARATOR` | `:::` | Customise if `:::` collides with your normal correspondence. |
| `DEFAULT_BRANCH`, `DEFAULT_SUBFOLDER_KEY` | `in`, `auto` | Used when the mail body contains no sentinel. |
| `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` | required | From `scripts/setup_dropbox.py`. |
| `DROPBOX_ROOT_PATH` | `/` | Base path for every upload. Typical pattern: `/Apps/<your-app>/<workspace>`. |
| `MAPPING_FILE` | `/app/mapping.yaml` | Bind-mount your mapping here. |
| `LOG_DIR`, `LOG_LEVEL` | `/app/logs`, `INFO` | Logging knobs. |

### `mapping.yaml` (routing taxonomy)

```yaml
branches:
  in:
    folder: Inbox
    default_subfolder: auto
    subfolders:
      auto:   "Auto-Assignment"
      paypal: "PayPal"
  out:
    folder: Outbox
    default_subfolder: ""
    subfolders: {}
```

Add or rename branches and subfolders as you like. Body sentinels reference the
keys, not the human-readable folder names, so umlauts and spaces never reach
the user-typed token. For example, putting `:::in:::paypal` in the body uploads
into `Inbox/PayPal`.

### Sentinel syntax

The worker scans every incoming mail body for the first occurrence of:

```
<prefix><branch>(<separator><subfolder>)?
```

With defaults that becomes:

| Body contains | Resolved Dropbox path |
|---|---|
| `:::in:::paypal` | `<root>/Inbox/PayPal` |
| `:::in` | `<root>/Inbox/Auto-Assignment` (branch default) |
| `:::out` | `<root>/Outbox` (no subfolders configured) |
| (no sentinel) | `<root>/Inbox/Auto-Assignment` (env defaults) |
| `:::unknown` or `:::in:::nonsense` | rejected with `ROUTING_REJECTED`, mail stays in INBOX |

The sentinel can be anywhere in the body, including inside a forwarded quote.
HTML-only mails are stripped to text before the search, so writing `:::in:::pp`
into an HTML composer works fine.

### Forwarded mail handling

When you forward a mail using Gmail's `Forward as attachment` option (or any
client that wraps a mail as a `.eml` part), `mailattachments2dropbox` walks the
MIME tree and pulls every attachment out, no matter how deeply nested. The
sentinel is always read from the outermost mail body (the wrapper you write
yourself), so a single forwarded mail can carry several inner messages each
with their own attachments and all of them land in the same target folder.

The audit log records the ancestor trail of every attachment, for example:

```
ATTACHMENT_FOUND uid=42 idx=2 filename=invoice.pdf
                 source_path=["outer", "forwarded.eml", "inner.eml"]
```

## Dropbox: one-time setup

1. Go to <https://www.dropbox.com/developers/apps> and click **Create app**.
2. Choose:
   - API: **Scoped access**
   - Access: **App folder** (or **Full Dropbox** if you want to write outside
     the auto-created app folder) - whichever matches the `DROPBOX_ROOT_PATH`
     you intend to use.
   - Name: anything unique to your account.
3. Under **Permissions**, enable: `files.metadata.write`, `files.content.write`,
   `files.content.read`. Click **Submit**.
4. From the app's **Settings** tab note the **App key** and **App secret**.
5. Locally run:

   ```bash
   uv sync --extra dev
   uv run python scripts/setup_dropbox.py
   ```

   The script asks for `APP_KEY` and `APP_SECRET`, opens the consent page in
   your browser, then waits for you to paste the authorisation code Dropbox
   prints back. It exchanges the code for a refresh token and prints the three
   lines you need in `.env`:

   ```
   DROPBOX_APP_KEY=...
   DROPBOX_APP_SECRET=...
   DROPBOX_REFRESH_TOKEN=...
   ```

   The refresh token is long-lived; the SDK mints short-lived access tokens
   from it transparently. You never need to redo this flow unless you revoke
   the app.

## Development

```bash
# Install runtime and dev dependencies.
uv sync --extra dev

# Lint, format, type-check, test (the four gates that CI should run).
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Project layout:

```
src/mailattachments2dropbox/
  config.py         pydantic settings + mapping.yaml loader
  logging_setup.py  structlog over stdlib, JSON to stdout + rotating file
  mail_parser.py    MIME parser, recursive for nested message/rfc822
  sentinel.py       body sentinel parser + routing + allow-list checks
  dropbox_client.py SDK wrapper with refresh-token auth and autorename
  mail_client.py    aioimaplib IDLE/polling client + lifecycle ops
  pipeline.py       per-mail orchestration and audit trail
  __main__.py       entrypoint: wires everything and runs the loop
scripts/
  setup_dropbox.py  one-time OAuth helper
tests/
  fixtures, unit tests for parser/sentinel/router, async pipeline integration
```

## Audit log

Every mail produces a deterministic sequence of structured events; an auditor
can follow a single mail by filtering on its `uid`. Representative events:

```
MAIL_RECEIVED        uid=42 sender=... subject="..." attachments_total=3
SENDER_CHECK         uid=42 sender=... matched_rule="*@example.com"
SENTINEL_PARSED      uid=42 branch=in subfolder=paypal matched_token=":::in:::paypal"
ROUTE_RESOLVED       uid=42 target=".../Inbox/PayPal"
ATTACHMENT_FOUND     uid=42 idx=0 filename=invoice.pdf source_path=["outer"]
ATTACHMENT_SKIPPED   uid=42 idx=1 filename=signature.png reason=extension_not_allowed
DROPBOX_UPLOAD       uid=42 idx=0 final_path=".../invoice.pdf" renamed=false size=12345
MAIL_LIFECYCLE       uid=42 action=delete uploaded=1 skipped=1
```

Rejection paths are equally explicit:

```
REJECT_SENDER          uid=7 sender=attacker@... allow_list=["*@example.com"]
ROUTING_REJECTED       uid=22 reason="subfolder key 'foo' is not defined..."
DROPBOX_UPLOAD_FAILED  uid=99 error="..."
```

Logs are written to stdout (consumed by the Docker logging driver) and to
`<LOG_DIR>/mailattachments2dropbox.log`, which is bind-mounted to `./logs/` by
the default `compose.yaml`.

## Linux host: log directory ownership

The container writes to `/app/logs`, which is bind-mounted to `./logs/` on
the host. On Linux that directory must be writable by the UID the container
runs as. By default the container uses UID 1000 (baked into the image as the
`app` user); if your host directory is owned by a different UID (for example
because you created `./logs/` while logged in as a non-1000 user), the worker
fails on startup with `PermissionError: '/app/logs/mailattachments2dropbox.log'`.

Two ways to fix this:

1. **Run the container as your host user** (recommended). Add `PUID` / `PGID`
   to `.env` so they propagate into compose's `user:` directive:

   ```bash
   echo "PUID=$(id -u)" >> .env
   echo "PGID=$(id -g)" >> .env
   docker compose up -d
   ```

2. **Or chown the logs directory to UID 1000:**

   ```bash
   sudo chown -R 1000:1000 logs
   docker compose up -d
   ```

On Docker Desktop for macOS and Windows the UIDs are transparently mapped, so
you can leave the defaults.

## Operational notes

- A mail that fails routing (unknown branch or subfolder) is left untouched so
  you can inspect or correct the mapping. The next run retries.
- A Dropbox upload error aborts the current mail; the next run retries.
- The container exits non-zero only on startup failure (bad config, broken
  Dropbox credentials, no IMAP connection). All in-flight errors are logged
  and the worker keeps running.
- IMAP IDLE refreshes itself every 25 minutes (RFC 2177 recommendation). On
  socket errors the client reconnects with a 5-second backoff.
- The compose file caps Docker's own JSON-file log driver at 10 MB times 5
  rotations to keep the host disk bounded even if you do not consume stdout.

## License

MIT. See [LICENSE](LICENSE).
