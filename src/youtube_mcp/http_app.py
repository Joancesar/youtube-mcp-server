"""HTTP app for remote deployment.

Routes:
- /mcp/<MCP_SECRET_PATH>  MCP streamable-HTTP endpoint (rewritten internally to /mcp)
- /oauth/start?key=<MCP_SECRET_PATH>  starts Google OAuth for the channel owner
- /oauth/callback  Google redirects here; the token is stored in YOUTUBE_MCP_CONFIG_DIR
- /health  liveness probe

The secret path is the access control: claude.ai custom connectors can't send custom
headers, so a long random path segment plays the role of a bearer token.
"""

import html
import logging
import os
import secrets

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from youtube_mcp.server import SECRET, auth, mcp, public_base_url

log = logging.getLogger(__name__)


class SecretPathMiddleware:
    """Pure ASGI middleware: /mcp only answers under /mcp/<secret>, rewritten to /mcp."""

    def __init__(self, app, secret: str):
        self.app = app
        self.prefix = f"/mcp/{secret}"

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                if not (path == self.prefix or path.startswith(self.prefix + "/")):
                    await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
                    return
                new_path = "/mcp" + path[len(self.prefix):]
                scope = dict(scope, path=new_path, raw_path=new_path.encode())
        await self.app(scope, receive, send)


def _redirect_uri(request: Request) -> str:
    base = public_base_url() or str(request.base_url).rstrip("/")
    return f"{base}/oauth/callback"


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui;max-width:560px;margin:15vh auto;padding:0 16px'>"
        f"<h2>{html.escape(title)}</h2><p>{body}</p></body>",
        status_code=status,
    )


async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def oauth_start(request: Request):
    key = request.query_params.get("key", "")
    if SECRET and not secrets.compare_digest(key, SECRET):
        return PlainTextResponse("Not Found", status_code=404)
    try:
        url = auth.authorization_url(_redirect_uri(request))
    except Exception as e:
        return _page("OAuth not configured", html.escape(str(e)), 500)
    return RedirectResponse(url)


async def oauth_callback(request: Request):
    params = request.query_params
    if params.get("error"):
        return _page("Authorization cancelled", html.escape(params["error"]), 400)
    code, state = params.get("code"), params.get("state")
    if not code or not state:
        return PlainTextResponse("Missing code/state", status_code=400)
    try:
        await run_in_threadpool(auth.exchange_code, code, state, _redirect_uri(request))
        channels = await run_in_threadpool(
            lambda: auth.build_youtube_service()
            .channels()
            .list(part="snippet", mine=True)
            .execute()
        )
    except Exception as e:
        log.exception("OAuth callback failed")
        return _page("Authorization failed", html.escape(str(e)), 400)
    items = channels.get("items", [])
    name = items[0]["snippet"]["title"] if items else "(no channel on this account)"
    return _page(
        "YouTube connected",
        f"Authorized channel: <b>{html.escape(name)}</b>. You can close this tab.",
    )


def build_app():
    if not SECRET and os.environ.get("ALLOW_NO_SECRET") != "1":
        raise SystemExit(
            "MCP_SECRET_PATH is required in remote mode (anyone with the URL could manage "
            "the channel). Set it to a long random string, or ALLOW_NO_SECRET=1 to override."
        )
    app = mcp.streamable_http_app()
    app.routes.append(Route("/health", endpoint=health, methods=["GET"]))
    app.routes.append(Route("/oauth/start", endpoint=oauth_start, methods=["GET"]))
    app.routes.append(Route("/oauth/callback", endpoint=oauth_callback, methods=["GET"]))
    return SecretPathMiddleware(app, SECRET) if SECRET else app
