"""Two bounded public routes; no arbitrary filters, account selector or broker calls."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.public_demo.service import PublicDemo

router = APIRouter(prefix="/api/demo", tags=["public-demo"])


def service(request: Request) -> PublicDemo:
    return request.app.state.public_demo


@router.api_route("/snapshot", methods=["GET", "HEAD"])
async def snapshot(request: Request) -> Response:
    if request.url.query:
        raise HTTPException(400, "This endpoint does not accept query parameters")
    demo = service(request)
    demo.demand()
    encodings = {}
    for item in request.headers.get("accept-encoding", "").lower().split(","):
        parts = [p.strip() for p in item.split(";")]
        try:
            quality = next((float(p[2:]) for p in parts[1:] if p.startswith("q=")), 1.0)
        except ValueError:
            quality = 0
        encodings[parts[0]] = quality
    use_gzip = encodings.get("gzip", encodings.get("*", 0)) > 0
    etag = '"' + demo.etag + ("-gzip" if use_gzip else "") + '"'
    headers = {
        "ETag": etag,
        "Vary": "Accept-Encoding",
        "Cache-Control": (
            "public, max-age=2, s-maxage=3, stale-while-revalidate=5"
            if demo.account_at is not None
            else "no-store"
        ),
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if use_gzip:
        headers["Content-Encoding"] = "gzip"
    if request.method == "HEAD":
        headers["Content-Length"] = str(len(demo.compressed if use_gzip else demo.raw))
    return Response(
        content=b"" if request.method == "HEAD" else (demo.compressed if use_gzip else demo.raw),
        media_type="application/json",
        headers=headers,
    )


@router.post("/presence")
async def presence(request: Request) -> JSONResponse:
    if request.url.query or await request.body():
        raise HTTPException(400, "This endpoint does not accept a request body or query parameters")
    demo = service(request)
    peer = demo.limits.peer(request.client.host if request.client else "unknown")
    token = request.cookies.get(demo.viewers.COOKIE)
    fresh = not demo.viewers.valid(token)
    if fresh:
        wait = demo.limits.issuance.admit(peer)
        if wait:
            raise HTTPException(429, "Try again later", headers={"Retry-After": str(wait)})
        token = demo.viewers.mint()
    if not demo.viewers.touch(token):
        raise HTTPException(503, "Viewer capacity reached", headers={"Retry-After": "30"})
    demo.demand()
    response = JSONResponse(
        {"viewers": demo.viewers.count, "lease_seconds": int(demo.viewers.LEASE)},
        headers={"Cache-Control": "no-store"},
    )
    if fresh:
        response.set_cookie(
            demo.viewers.COOKIE,
            token,
            path="/api/demo",
            max_age=demo.viewers.COOKIE_TTL,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
    return response
