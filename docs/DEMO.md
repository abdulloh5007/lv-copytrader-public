# Public demo

`/demo` is public. It never initializes the operator UI, authenticates visitors, subscribes to private WebSockets or accepts trading commands. The waitlist links to it.

## Published data

`GET /api/demo/snapshot` returns an explicit projection: account balance, equity, floating profit, currency and demo/real mode; broker/Telegram availability and bot running state; bot aggregate results, a cumulative realized P/L curve and the latest 30 closed positions. It excludes account login, owner, server, credentials, settings, Telegram identities/messages, tickets and internal identifiers. Account-wide figures and bot-only results are separate. An account being a broker demo account is labeled accurately.

Bot statistics include only non-external positions with the configured magic number and a closed timestamp. Null profit is unknown, never counted as a loss; wins divided by positions with known profit includes flat outcomes in the denominator. The curve covers up to 90 trading days with the prior-history baseline retained. It is cumulative bot P/L, not a reconstructed account balance or return percentage. The existing reconciliation process remains responsible for stored execution history.

## Freshness and traffic

A single background task refreshes the account every approximately five seconds while visitors are active. Database aggregates/history refresh every 15 seconds, supported by a composite ownership/status/time index. After 70 seconds without demand, polling stops. Broker failures preserve the last successful values and their original timestamp; the UI marks stale data rather than presenting it as live.

Visitor requests read pre-serialized JSON/gzip bytes; they do not query MT5 or the database. Representation-specific ETags support 304 and HEAD. Successful snapshots can be cached briefly by a shared proxy; errors and presence responses are never cached. The browser pauses on hidden tabs, cancels obsolete requests and backs off after failures/rate limits.

`POST /api/demo/presence` uses a signed, HttpOnly, SameSite=Strict cookie scoped to `/api/demo`, Secure over HTTPS. A 50-second lease counts active browser sessions approximately; tabs sharing a cookie count once. This is not a verified-human count. Heartbeats are about 20 seconds apart. The cookie grants no authentication permissions and is described on the privacy page.

## Bounded resources and security

Only the exact two public route/method combinations bypass authentication. Private REST and all WebSocket endpoints retain authentication. Both public routes reject query parameters; presence accepts no request body. Existing origin checks, CSP and other security headers apply. Actual streamed body size is bounded before route processing.

Token buckets cap global traffic (burst 3000, refill 1500/second), each peer (burst 120, refill 12/second), and new viewer-cookie issuance per peer (burst 20, refill 0.5/second). Peer identities use process-salted HMAC hashes. Limiter state is bounded to 8192 active peers; presence to 10000 active leases. Full state rejects new admissions rather than allocating without bound. These are process-level application controls; the current single-worker deployment is intentional because trading/Telegram lifecycle must not be duplicated. Viewer leases reset when the process restarts. Infrastructure/network flood capacity remains dependent on the hosting edge.

No trading settings, account selection, credentials or execution functions are added to the public surface.

## Validation

- Full backend suite: 629 passed, including 30 public-demo cases covering allowlists, accounting, cached concurrency, signed leases, replay/expiry/capacity, rate limits, method/query boundaries, cross-origin and streamed-body rejection, private API/WebSocket access, stale broker data, gzip/ETag/HEAD and cache isolation.
- Frontend: TypeScript check, production build and nine tests passed. Chart tests cover empty/non-finite data, flat results and actual losses/recoveries without fabricating a curve.
- Isolated browser fixture: light/dark persistence, 320/390/768/1440 widths without overflow, keyboard chart navigation, expandable/paginated history, shared-tab counting and HTML escaping. Axe WCAG A/AA: zero violations; no private API calls or browser/CSP errors.
- Isolated in-process ASGI load test: 1000 virtual visitors, 3000 requests (2000 successful responses, 1000 conditional 304s), 2.947 seconds, roughly 1018 requests/second; p50 0.738 ms, p95 1.579 ms. Request handlers caused zero broker and zero database calls. Separate one-peer overload returned 874 rate limits out of 1000 requests, with no HTTP 500s. This is an application benchmark, not a measurement of public-network capacity.

Fixtures and load scripts use isolated fake accounts/databases and never submit real trades. Deployment backs up SQLite and the prior source/image, changes only the app container, and preserves account/trading configuration.

## Published verification — 2026-10-21 UTC

The public HTTPS page and read-only snapshot were verified after deployment. Balance/currency/account mode matched a separate MT5 read; bot count, settled P/L and win rate matched read-only SQL aggregates. The database contains 84 qualifying closed bot positions; the page returns the latest 30. The connected account reports broker demo mode. Broker and Telegram are connected; automation reports paused accurately.

Production checks passed: secure presence cookie, conditional/gzip/HEAD responses, query/body/origin rejection, private endpoints returning 401, unchanged settings/source hashes and retained trade history. App container is healthy. MT5 and tunnel container identities and start times are unchanged.

A browser on the public URL confirmed real snapshot timestamp advancement, both themes, 320/390/768/1440 layouts, history interaction, shared-tab viewer counting, the waitlist demo link, zero WCAG A/AA violations and zero console/page/request errors. No private API was requested by `/demo`.
