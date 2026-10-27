# Security

Report vulnerabilities privately through [GitHub private vulnerability reporting](https://github.com/abdulloh5007/lv-copytrader-public/security/advisories/new). Do not post credentials, account details or session files in issues.

Private APIs fail closed when dashboard credentials are missing. `SP_ALLOW_UNAUTHENTICATED=true` is an explicit option for isolated local development, never a public deployment setting. Use a strong unique password and signing key, explicit allowed hostnames, HTTPS, and only trusted reverse proxies. Keep the sidecar RPC port private.

Logout revokes the current session. Sessions are unique, expire, and become invalid when credentials change. Authentication state and guessing limits persist in the private data volume; losing that state invalidates existing sessions. Proxy-supplied client IPs must be overwritten at the trusted edge.

Dependency audits and auth regression checks run from `.github/workflows/security.yml` when pushed. Rebuild and redeploy after updates: pushing source does not update a running container. Node is used to build static frontend assets; it is not the production backend server. React Server Components and Next.js are not used.

Known-vulnerability scans and passing tests do not guarantee the absence of vulnerabilities or protection against a large distributed denial-of-service attack. Re-run dependency and image scans regularly and apply upstream security updates.
