"""Public rate protection before authentication and route processing."""

from starlette.responses import JSONResponse

from app.public_demo.limits import PublicLimits


class DemoGuard:
    def __init__(self, app, *, limits: PublicLimits):
        self.app, self.limits = app, limits

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith("/api/demo/"):
            peer = str((scope.get("client") or ("unknown", 0))[0])
            wait = self.limits.admit(self.limits.peer(peer))
            if wait:
                response = JSONResponse(
                    {"error": {"code": "rate_limit", "message": "Try again later", "detail": {}}},
                    status_code=429,
                    headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
                )
                return await response(scope, receive, send)
        await self.app(scope, receive, send)
