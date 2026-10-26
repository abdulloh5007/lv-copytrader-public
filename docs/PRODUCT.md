# LV Copytrader · product update

The deployed source is `/opt/signalpilot`. The `signalpilot` directory and database
name are retained for compatibility; the product name is **LV Copytrader**.

## Source layout

```text
frontend/src/
  app/                   Session gate and the single health poll
  api/                   REST, WebSocket transport and event normalization
  components/            Shared controls, tables, navigation and public layout
  features/settings/     Telegram, MT5, provider, notifications, limits, general
  features/waitlist/     Signup API client
  pages/                 Lazy-loaded routes
  styles/                Tokens, base, primitives, app, product and public pages
  i18n/                  Complete English and Russian dictionaries
frontend/tests/          Live/replayed event contract regression tests
backend/app/
  api/waitlist.py        Public signup and authenticated count
  cli/waitlist.py        Operator-only export and deletion
  util/security.py       Same-origin checks, CSP, response headers, body limits
  util/auth.py           Cookie/Basic authentication and shared login throttle
  models.py              Existing trading schema plus isolated waitlist tables
backend/requirements.lock  Exact runtime dependency constraints
```

The existing AI, Telegram and trading packages retain their boundaries. The UI
does not send trading actions merely by navigating between pages.

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Authenticated overview; login when signed out |
| `/positions`, `/history`, `/settings` | Authenticated workspace |
| `/login` | Login; signed-in operators return to the overview |
| `/waitlist` | Public early-access signup, independently of dashboard auth |
| `/privacy` | Public explanation of waitlist data use |
| `POST /api/waitlist` | Public JSON `{email, consent: true, website: ""}` |
| `GET /api/waitlist` | Authenticated signup count; never returns email addresses |

Signup responds identically for new and existing addresses. Email is normalized,
Fernet-encrypted with the existing `SECRET_KEY`, and deduplicated using an HMAC.
The consent time and notice version are recorded. The SQLite throttle survives
restarts and caps requests at 20 per minute per peer; behind a proxy this depends
on correctly configured trusted proxy addresses. No email is sent automatically.

## Operator commands

Run in `/opt/signalpilot`. These commands require local Docker access.

```bash
docker compose exec -T app python -m app.cli.waitlist count
# The CSV contains personal data. Store it privately and delete unneeded copies.
umask 077
docker compose exec -T app python -m app.cli.waitlist export > /private/path/waitlist.csv
docker compose exec -T app python -m app.cli.waitlist delete --email person@example.com
```

The encryption key must remain stable and backed up with the database. Rotating it
without migrating stored data makes encrypted signup addresses and existing
broker/provider/Telegram secrets unreadable. Export escapes spreadsheet formulas.

## Verification and build

```bash
cd frontend
npm ci
npm run typecheck
npm test
npm run build
npm audit

cd ../backend
python -m pip install -c requirements.lock '.[dev]'
python -m pytest -q
```

Run tests from a clean environment with no operator secrets. `tests/conftest.py`
uses temporary databases and fake MT5 services; a container with `--network none`
provides additional protection. Mount the whole repository so sidecar source
tests can locate `mt5-sidecar/` alongside `backend/`.

`backend/Dockerfile` runs typechecking before Vite and uses `npm ci`; Python
dependencies use the runtime constraints. No secrets, backups, runtime snapshots,
browser artifacts or local data enter the Docker build context.

## Deployment

Build and replace **only** the application; preserve MT5, the tunnel, all named
volumes and `.env`. Use one uvicorn worker, no reload. Before replacement, take a
SQLite online backup and retain the previous image for rollback. Never start a
second production runtime against the same Telegram session or broker account.

```bash
docker compose build app
docker compose up -d --no-deps app
docker compose ps app
```

`SP_TRUSTED_PROXY_IPS` lists only the immediate trusted proxy IPs for uvicorn.
This makes the public HTTPS scheme and visitor address available to session
cookies and request throttles. Include loopback for local administration. If the
Cloudflare tunnel container is recreated with a new IP, update this setting and
recreate only the app. Do not use `*` on a reachable service.

The current hosting remains a Cloudflare Quick Tunnel. Obtain its current address
with `curl -s http://127.0.0.1:2000/quicktunnel`. Its hostname may change when the
tunnel restarts; a permanent domain is a separate hosting configuration.

## Security and operational boundaries

- Dashboard APIs, OpenAPI and WebSockets require existing operator authentication.
- Public signup is the only new authentication exception, for POST only.
- HTTP writes and WebSockets validate Origin; public JSON is capped at 8 KiB.
- Cookies reject future timestamps and malformed values; key bootstrap is observed
  dynamically, and Basic authentication shares the login throttle.
- CSP allows only local scripts plus the hash of the pre-paint theme bootstrap.
  There are no external font or advertising requests. Inline styles remain
  allowed for the existing React component styling.
- Input values are excluded from validation errors. API responses are not cached.
- Themes and locales persist before paint; reduced motion is respected.
- The waitlist is a collection mechanism, not a mail-delivery integration.

The original runbook and trading behavior remain relevant. A passing test suite
does not establish future trading performance; this release does not change
position sizing, stop-loss logic, provider keys or the operator's arm state.
