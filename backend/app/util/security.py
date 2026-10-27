"""HTTP security, same-origin writes and bounded public request bodies."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


class SecurityMiddleware:
    def __init__(self, app, *, frontend_dist: str, dev_origins=()):
        self.app = app
        self.frontend_dist = Path(frontend_dist)
        self.dev_origins = frozenset(dev_origins)
        self._script_hashes: tuple[str, ...] | None = None

    def script_hashes(self) -> tuple[str, ...]:
        if self._script_hashes is not None:
            return self._script_hashes
        index = self.frontend_dist / "index.html"
        if not index.is_file():
            return ()
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", index.read_text(), re.S)
        self._script_hashes = tuple(
            "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
            for s in scripts if s.strip()
        )
        return self._script_hashes

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", ())}
        is_http = scope["type"] == "http"

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                extra = {
                    "x-content-type-options": "nosniff",
                    "x-frame-options": "DENY",
                    "referrer-policy": "no-referrer",
                    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
                    "content-security-policy": "default-src 'self'; script-src 'self' "
                    + " ".join(self.script_hashes())
                    + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                    "font-src 'self'; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
                }
                if scope.get("path", "").startswith("/api/") and not (
                    scope.get("path") == "/api/demo/snapshot" and scope.get("method") in {"GET", "HEAD"}
                    and message.get("status") in {200, 304}
                ):
                    extra["cache-control"] = "no-store"
                if scope.get("scheme") == "https":
                    extra["strict-transport-security"] = "max-age=31536000"
                old = [(k, v) for k, v in message.get("headers", ()) if k.decode().lower() not in extra]
                message["headers"] = old + [(k.encode(), v.encode()) for k, v in extra.items()]
            await send(message)

        async def reject(status, code, message):
            if not is_http:
                return await send({"type": "websocket.close", "code": 1008})
            response = JSONResponse({"error": {"code": code, "message": message, "detail": {}}}, status_code=status)
            await response(scope, receive, secured_send)

        unsafe = not is_http or scope.get("method") not in ("GET", "HEAD", "OPTIONS")
        if unsafe:
            origin = headers.get("origin")
            try:
                parsed = urlsplit(origin or "")
                expected_scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
                same_origin = parsed.scheme == expected_scheme and parsed.netloc.lower() == headers.get("host", "").lower()
            except ValueError:
                same_origin = False
            if (origin and not same_origin and origin not in self.dev_origins) or (
                headers.get("sec-fetch-site") == "cross-site" and origin not in self.dev_origins
            ):
                return await reject(403, "cross_origin_request", "Cross-origin requests are not allowed")

        # Buffer only the two tiny, public JSON forms. Count actual bytes as well as
        # Content-Length, so chunked requests cannot bypass the limit.
        if is_http and scope.get("method") not in {"GET", "HEAD", "OPTIONS"} and scope.get("path", "").startswith("/api/"):
            path = scope.get("path", "").rstrip("/")
            limit = 256 if path == "/api/demo/presence" else 8192 if path in {"/api/auth/login", "/api/waitlist"} else 1024 * 1024
            try:
                length = int(headers.get("content-length", "0"))
                if length < 0:
                    return await reject(400, "bad_request", "Invalid content length")
                if length > limit:
                    return await reject(413, "payload_too_large", "Request body is too large")
            except ValueError:
                return await reject(400, "bad_request", "Invalid content length")
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > limit:
                    return await reject(413, "payload_too_large", "Request body is too large")
                chunks.append(message)
                if not message.get("more_body", False):
                    break
            async def buffered_receive():
                return chunks.pop(0) if chunks else await receive()
            return await self.app(scope, buffered_receive, secured_send)
        await self.app(scope, receive, secured_send)
