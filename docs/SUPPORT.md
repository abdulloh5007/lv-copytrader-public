# Support reports

Users send reports from their own local installation:

```sh
lvct report --note "MT5 has not connected after startup"
# Read the JSON file printed by the command, then:
lvct report --send --file PATH_TO_REPORT
```

An optional `--contact EMAIL` adds a reply address to the saved report. It does not
create an account or subscribe the user to marketing mail. Reports are never sent
on startup, errors or a schedule. Configured secrets and common credentials, account
IDs, URLs, email/IP addresses and local paths are scrubbed from error text. Redaction
is best effort: review the file before sending it. Contact is intentionally retained
if supplied. No environment files, databases, broker files, trades, sessions or full
settings are attached. At most 50 recent error records (1,000 characters each) and
10 CLI events are included, along with platform/version and readiness categories.

## Receiver

The official endpoint is `https://lvenc.site/api/support`. It accepts strict schema-1
JSON, up to 64 KiB with a 10-second body-read limit, using an expiring IP-bound SHA-256 challenge. It is separate from
the trading application. The default limits are 3 reports/hour per hashed IP,
20/day globally and at most 500 retained reports. Shared networks share the IP limit.
Challenge issuance also has per-IP/global limits. A proof-of-work challenge reduces
cheap spam; it is not an identity check or a guarantee against denial of service.

Reports are Fernet-encrypted in a dedicated SQLite volume with a separate server key.
IP addresses are retained only as keyed hashes. Reports expire after 14 days; keys
and SMTP/Telegram credentials stay on the support server and are never shipped to
users. API responses are not cached; there is no public report-reading endpoint.
An independent outbox alerts the configured owner by Telegram and TLS SMTP. Each
channel retries failures up to five times with backoff, with at most 20 attempts per
UTC day per channel. Acceptance means the report is saved, not that alerts arrived.
Delivery after an ambiguous network failure can duplicate an alert; the report ID and
email Message-ID help correlate it. Optional contact is available in the full inbox.

## Self-hosting the receiver

This service is optional for operating a copytrader. It does not mount application
or MT5 volumes. Its Dockerfile uses Python 3.12 and pinned dependencies. Build from
the repository root: `docker build -f support_service/Dockerfile -t lvct-support:local .`.
Mount a protected data directory writable by UID 10001 at `/data`; bind the container's
19473 port to `127.0.0.1:19473`, and use a dedicated reverse proxy HTTPS route for
`/api/support/*`. Run one worker, no reload. Set these only in a protected server env:

| Setting | Purpose |
| --- | --- |
| `SUPPORT_KEY` | Stable, separate Fernet key; back up with support storage |
| `SUPPORT_DATA_DIR` | Defaults to `/data` |
| `SUPPORT_TG_TOKEN`, `SUPPORT_TG_CHAT` | Fixed owner notification destination |
| `SUPPORT_SMTP_HOST`, `SUPPORT_SMTP_PORT` | STARTTLS SMTP, default port 587 |
| `SUPPORT_SMTP_USER`, `SUPPORT_SMTP_PASSWORD` | SMTP credentials |
| `SUPPORT_SENDER`, `SUPPORT_RECIPIENT` | Fixed sender and owner recipient |
| `SUPPORT_TRUSTED_PROXIES` | Exact immediate proxy IPs; never `*` |
| `SUPPORT_NOTIFICATIONS` | `0` disables the outbox for isolated tests |

The proxy must **overwrite** `X-LVCT-Client` with the real peer IP. The receiver uses
it only from exact trusted peers; untrusted forwarded headers are ignored. Keep the
loopback port private, reject bodies above 64 KiB, cap proxy concurrency, and do not
log request bodies or notification URLs containing Telegram bot tokens. Keep access
logs disabled for this route if raw visitor IP retention is unwanted. The default
Host allowlist is the official host and localhost; a fork must deliberately change
its host allowlist and the CLI endpoint together.

Owner-only inbox inspection via local Docker privileges:

```sh
docker exec lvct-support python -m support_service.admin list
docker exec lvct-support python -m support_service.admin show REPORT_UUID
```

`show` prints potentially personal report text; use a private terminal. `GET
/api/support/health` checks process readiness. Tests use temporary encrypted stores
and mocked mail/Telegram transports; no real notification or trade is needed.
