# Security audit — 27 October 2026

Scope: application source, authentication and WebSockets, public forms, image handling,
locked npm/Python dependencies, backend and Wine images, reverse proxy and deployment
permissions/network exposure. This is a point-in-time audit, not a certification.

## Applied protections

- Private API access fails closed without configured credentials. Development bypass must
  be explicitly enabled. Sessions contain random nonces, expire after 12 hours, are bound
  to credentials, and are revoked on logout. Revocation also stops private WebSocket frames.
- Per-IP login limit: eight failures per 60 seconds; global budget: 120 failures per
  60 seconds. Hashed peer identifiers and revocation state persist atomically with mode
  0600. Trusted proxies overwrite forwarded IP headers; arbitrary client headers cannot
  create new limiter identities. SSH new-connection throttling is separate.
- Host allowlisting, same-origin writes including scheme matching, CSP, secure cookies,
  no-store private API responses, bounded request bodies and proxy timeouts.
- Telegram images: bounded download size and time, bounded decoded pixel count, safe
  suffixes. Logs and public export checks recognize additional cloud credential formats.
- Non-root backend, read-only root filesystem, no capabilities or privilege escalation,
  process/concurrency/log limits. MT5 receives only terminal/RPC configuration and its RPC
  is isolated from the public/tunnel service. VNC remains debug-only and localhost-bound.

## Dependency and runtime results

- Node 24.22.1, matching the official October security release. Frontend assets are static;
  production does not run a Node web server or React Server Components.
- Caddy 2.11.7 rebuilt with Go 1.27.3, x/net 0.60.0 and chi 5.4.0. Build provenance is
  recorded in deployment metadata. The OpenPGP retirement advisory GO-2026-5932 names a
  package that is not linked into this binary, verified with the compiler dependency list.
- Locked Python and npm audits: zero known vulnerabilities. Deployed Alpine backend
  image: zero findings at all scanner severities, including OS and Python packages.
- Wine/GUI dependencies are audited separately. Current vendor security updates and an
  upstream Pillow 13 replacement are applied without upgrading Wine. Unfixed vendor
  findings remain tracked; they are not hidden by a global ignore file. The final image
  contains 4 Critical and 172 High package findings, all without a vendor fixed version.
  Critical IDs: liblmdb0 CVE-2019-16224, CVE-2019-16225, CVE-2019-16227 and libxml2
  CVE-2026-6653. Package matching does not establish remote reachability; the RPC network
  is restricted and does not accept arbitrary database or XML documents.
- Host OS package scanning distinguishes the running kernel from old kernel tools/headers:
  package-level matches on those files are not proof that the running kernel is affected.
  An available host Brotli fix is applied. Vendor-unfixed userspace/kernel-tool findings
  remain outside the clean backend-image result.

## Validation and limits

671 isolated backend tests passed, including 38 sidecar tests. Frontend typecheck,
production build and nine tests passed; 13 publisher/snapshot tests passed. Live checks
confirmed logout replay denial, unique secure cookies, request limits, Host/Origin rejection,
proxy-IP sanitization and backend-only RPC connectivity. No broker orders were used as tests.

The private operator archive retains raw scanner output and deployment evidence. Closed
source MT5/Wine Windows components, the provider infrastructure and unrelated hosted apps
were not certified. No volumetric denial-of-service or destructive exploitation was run
against live trading. Strong credentials and off-host backups remain required.

GitHub Actions and Dependabot configuration are prepared in source. They become effective
only after publication; account-side alerts require authenticated repository administration.

## Upstream references

- [Node October security release](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases)
- [Caddy 2.11.7](https://github.com/caddyserver/caddy/releases/tag/v2.11.7)
- [Go HTTP/2 advisory](https://pkg.go.dev/vuln/GO-2026-6611)
- [Chi proxy IP advisory](https://github.com/advisories/GHSA-j6qf-3m9c-vwj3)
- [Debian libxml2 status](https://security-tracker.debian.org/tracker/CVE-2026-6653)
- [Pillow release notes](https://pillow.readthedocs.io/en/stable/releasenotes/)
