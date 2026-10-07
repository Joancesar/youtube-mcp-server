"""YouTube MCP Server — FastMCP entry point.

Transports (MCP_TRANSPORT env var):
- stdio (default): local use from Claude Desktop / Claude Code.
- streamable-http: remote deployment (e.g. Railway). The MCP endpoint is served at
  /mcp/<MCP_SECRET_PATH>, and /oauth/start?key=<MCP_SECRET_PATH> authorizes the channel.
"""

import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from youtube_mcp.auth import YouTubeAuth
from youtube_mcp.utils.quota import QuotaTracker

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
REMOTE = TRANSPORT != "stdio"
SECRET = os.environ.get("MCP_SECRET_PATH", "").strip().strip("/")


def public_base_url() -> str:
    """Public https base URL of this server (PUBLIC_BASE_URL or Railway's domain)."""
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        base = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    return base


def oauth_start_url() -> str:
    query = f"?key={SECRET}" if SECRET else ""
    return f"{public_base_url()}/oauth/start{query}"


mcp = FastMCP(
    "YouTube MCP Server",
    instructions=(
        "Manage the authorized YouTube channel: videos, metadata, thumbnails, playlists, "
        "comments, captions, analytics and reports. When running remotely, upload videos "
        "with youtube_upload_from_url (Google Drive share links work) and poll "
        "youtube_upload_status. If a tool reports the account is not authorized, call "
        "youtube_auth to get the authorization link for the channel owner."
    ),
    # Remote mode: stateless JSON responses behind a trusted proxy (Railway).
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Shared state
auth = YouTubeAuth(
    client_secret_path=os.environ.get("YOUTUBE_MCP_CLIENT_SECRET"),
    config_dir=os.environ.get("YOUTUBE_MCP_CONFIG_DIR"),
    api_key=os.environ.get("YOUTUBE_API_KEY"),
    remote=REMOTE,
    auth_url_hint=oauth_start_url() if REMOTE else None,
)
quota = QuotaTracker()


# --- Auth tools ---


@mcp.tool()
def youtube_auth() -> dict:
    """Authenticate with YouTube (OAuth 2.0).

    Local mode: opens a browser window for Google consent.
    Remote mode: returns a link the channel owner must open once to grant access.
    Required before using tools that access private channel data or analytics.
    """
    try:
        auth.authenticate()
        return {"status": "authenticated", "detail": auth.status()}
    except Exception as e:
        if REMOTE:
            return {
                "status": "action_required",
                "open_this_url": oauth_start_url(),
                "detail": (
                    "Open the link signed in with the Google account that owns or manages "
                    "the channel, pick the channel if asked, and accept. " + str(e)
                ),
            }
        return {"status": "error", "detail": str(e)}


@mcp.tool()
def youtube_auth_status() -> dict:
    """Check current authentication status and quota usage."""
    return {
        "auth": auth.status(),
        "quota": quota.status(),
    }


# --- Register tool modules ---
# Import tool modules so their @mcp.tool() decorators run

from youtube_mcp.tools import channel  # noqa: E402, F401
from youtube_mcp.tools import search  # noqa: E402, F401
from youtube_mcp.tools import transcripts  # noqa: E402, F401
from youtube_mcp.tools import analytics  # noqa: E402, F401
from youtube_mcp.tools import publishing  # noqa: E402, F401
from youtube_mcp.tools import playlists  # noqa: E402, F401
from youtube_mcp.tools import comments  # noqa: E402, F401
from youtube_mcp.tools import reporting  # noqa: E402, F401
from youtube_mcp.tools import media  # noqa: E402, F401


def main():
    if not REMOTE:
        mcp.run()
        return

    import uvicorn

    from youtube_mcp.http_app import build_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("PORT", "8000"))
    # "::" is dual-stack on Linux: works for Railway's public (IPv4) and private (IPv6) nets.
    uvicorn.run(
        build_app(),
        host=os.environ.get("HOST", "::"),
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
