"""OAuth 2.0 authentication for YouTube APIs.

Users must provide their own OAuth client from their Google Cloud project, either as a
client_secret.json file or via the GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars.

Local (stdio) mode: on first use, a browser-based OAuth consent flow runs on localhost
and stores the token in the config dir.

Remote (HTTP) mode: the server exposes /oauth/start and /oauth/callback (see http_app.py).
The owner opens /oauth/start once, consents, and the refresh token is stored in the config
dir (mount a persistent volume there, e.g. /data on Railway). YOUTUBE_REFRESH_TOKEN can be
used instead of the volume.
"""

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

# All scopes we need across all phases
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    # Required by the comment endpoints (commentThreads.list/insert,
    # comments.insert). Without it, youtube_list_comments / youtube_post_comment
    # / youtube_reply_to_comment fail with HTTP 403 insufficientPermissions.
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]

DEFAULT_CONFIG_DIR = Path.home() / ".youtube-mcp"
TOKEN_FILE = "token.json"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


class AuthError(Exception):
    pass


class YouTubeAuth:
    """Manages OAuth 2.0 credentials and builds API service clients."""

    def __init__(
        self,
        client_secret_path: str | Path | None = None,
        config_dir: str | Path | None = None,
        api_key: str | None = None,
        remote: bool = False,
        auth_url_hint: str | None = None,
    ):
        self.remote = remote
        # Where the owner should go to (re)authorize in remote mode; used in errors.
        self.auth_url_hint = auth_url_hint
        self.client_id = os.environ.get("GOOGLE_CLIENT_ID")
        self.client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        self._pending_flows: dict[str, str | None] = {}  # state -> PKCE code_verifier
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.token_path = self.config_dir / TOKEN_FILE
        self._credentials: Credentials | None = None

        # Resolve client_secret.json path
        if client_secret_path:
            self.client_secret_path = Path(client_secret_path)
        else:
            env_path = os.environ.get("YOUTUBE_MCP_CLIENT_SECRET")
            if env_path:
                self.client_secret_path = Path(env_path)
            else:
                self.client_secret_path = self.config_dir / "client_secret.json"

        # API key fallback for public-only operations
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")

    def _load_token(self) -> Credentials | None:
        """Load saved credentials from token file."""
        if not self.token_path.exists():
            return self._token_from_env()
        try:
            # Deliberately omit `scopes=SCOPES` here: passing a non-None
            # scopes argument makes from_authorized_user_info() ignore the
            # file's own "scopes" field entirely (it only falls back to the
            # file when scopes is None), which would make creds.scopes always
            # report today's SCOPES regardless of what was actually granted.
            creds = Credentials.from_authorized_user_file(str(self.token_path))
            return creds
        except Exception:
            return None

    def _token_from_env(self) -> Credentials | None:
        """Build credentials from YOUTUBE_REFRESH_TOKEN + client id/secret, if set."""
        refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")
        config = self._client_config()
        if not refresh_token or not config:
            return None
        info = next(iter(config.values()))
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=info.get("token_uri", TOKEN_URI),
            client_id=info["client_id"],
            client_secret=info["client_secret"],
            scopes=SCOPES,
        )

    def _client_config(self) -> dict | None:
        """OAuth client config from env vars, or from client_secret.json."""
        if self.client_id and self.client_secret:
            return {
                "web": {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "auth_uri": AUTH_URI,
                    "token_uri": TOKEN_URI,
                }
            }
        if self.client_secret_path.exists():
            try:
                return json.loads(self.client_secret_path.read_text())
            except (OSError, ValueError):
                return None
        return None

    # --- Web (remote) OAuth flow -------------------------------------------

    def _web_flow(self, redirect_uri: str, state: str | None = None) -> Flow:
        config = self._client_config()
        if not config:
            raise AuthError(
                "No OAuth client configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
            )
        return Flow.from_client_config(
            config, scopes=SCOPES, redirect_uri=redirect_uri, state=state
        )

    def authorization_url(self, redirect_uri: str) -> str:
        """Start the web OAuth flow. Returns the Google consent URL."""
        flow = self._web_flow(redirect_uri)
        url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",  # always return a refresh token
            include_granted_scopes="true",
        )
        self._pending_flows[state] = getattr(flow, "code_verifier", None)
        return url

    def exchange_code(self, code: str, state: str, redirect_uri: str) -> Credentials:
        """Finish the web OAuth flow and persist the token."""
        if state not in self._pending_flows:
            raise AuthError("Unknown or expired OAuth state. Start again from /oauth/start.")
        verifier = self._pending_flows.pop(state)
        flow = self._web_flow(redirect_uri, state=state)
        if verifier:
            flow.code_verifier = verifier
        flow.fetch_token(code=code)
        creds = flow.credentials
        if not creds.refresh_token:
            raise AuthError(
                "Google did not return a refresh token. Remove the app's access at "
                "https://myaccount.google.com/permissions and authorize again."
            )
        self._save_token(creds)
        self._credentials = creds
        return creds

    def _save_token(self, creds: Credentials):
        """Save credentials to token file (best effort if the dir is read-only)."""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(creds.to_json())
        except OSError:
            pass

    @staticmethod
    def _has_required_scopes(creds: Credentials | None) -> bool:
        """Return True if the cached credential covers every scope in SCOPES.

        google-auth's `creds.valid` only reflects token expiry, not scope
        coverage. When SCOPES is expanded, a previously saved token still
        reports valid but will 403 on any newly-required API. We must detect
        the mismatch and drop back to the OAuth flow.
        """
        if creds is None:
            return False
        granted = set(creds.scopes or [])
        return set(SCOPES).issubset(granted)

    def _invalidate_token_file(self):
        """Remove stale token so a fresh OAuth flow can overwrite it."""
        try:
            self.token_path.unlink(missing_ok=True)
        except OSError:
            pass

    def authenticate(self) -> Credentials:
        """Get valid credentials, running OAuth flow if needed.

        Returns valid credentials. Raises AuthError if client_secret.json
        is missing or the flow fails.
        """
        creds = self._load_token()

        if creds and creds.valid and self._has_required_scopes(creds):
            self._credentials = creds
            return creds

        refresh_error: Exception | None = None
        needs_refresh = creds is not None and (creds.expired or not creds.token)
        if needs_refresh and creds.refresh_token and self._has_required_scopes(creds):
            try:
                creds.refresh(Request())
                self._save_token(creds)
                self._credentials = creds
                return creds
            except Exception as e:
                # Refresh failed (revoked, or 7-day expiry of apps in "Testing"), re-auth
                refresh_error = e

        # Either no token, expired without refresh, or scopes insufficient.
        # Drop the stale token so we do not keep reusing it.
        if creds is not None and not self._has_required_scopes(creds):
            self._invalidate_token_file()

        # Need to run the OAuth flow
        if self.remote:
            where = self.auth_url_hint or "/oauth/start on this server"
            detail = f" Refresh failed: {refresh_error}." if refresh_error else ""
            raise AuthError(
                f"YouTube account not authorized (or token revoked/expired).{detail} "
                f"The channel owner must open {where} and grant access."
            )
        if not self.client_secret_path.exists():
            raise AuthError(
                f"client_secret.json not found at {self.client_secret_path}. "
                f"Download it from your Google Cloud Console "
                f"(APIs & Services > Credentials > OAuth 2.0 Client IDs) "
                f"and place it at this path, or set YOUTUBE_MCP_CLIENT_SECRET env var."
            )

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secret_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
            self._save_token(creds)
            self._credentials = creds
            return creds
        except Exception as e:
            raise AuthError(f"OAuth flow failed: {e}") from e

    @property
    def credentials(self) -> Credentials:
        """Get current credentials, authenticating if needed."""
        if self._credentials and self._credentials.valid:
            return self._credentials
        return self.authenticate()

    def build_youtube_service(self):
        """Build a YouTube Data API v3 service client."""
        return build("youtube", "v3", credentials=self.credentials)

    def build_youtube_analytics_service(self):
        """Build a YouTube Analytics API service client."""
        return build("youtubeAnalytics", "v2", credentials=self.credentials)

    def build_youtube_reporting_service(self):
        """Build a YouTube Reporting API service client."""
        return build("youtubereporting", "v1", credentials=self.credentials)

    def build_public_youtube_service(self):
        """Build a YouTube Data API client using API key only (public data)."""
        if not self.api_key:
            raise AuthError(
                "No API key available. Set YOUTUBE_API_KEY env var for public-only access."
            )
        return build("youtube", "v3", developerKey=self.api_key)

    def status(self) -> dict:
        """Return current auth status."""
        creds = self._load_token()
        if creds and creds.valid:
            return {
                "authenticated": True,
                "scopes": creds.scopes or [],
                "token_path": str(self.token_path),
                "expired": False,
            }
        if creds and creds.expired:
            # An expired access token is refreshed on the next call if a refresh token exists.
            return {
                "authenticated": bool(creds.refresh_token),
                "expired": True,
                "has_refresh_token": bool(creds.refresh_token),
                "token_path": str(self.token_path),
            }
        if creds and creds.refresh_token and not creds.token:
            # Env-provided refresh token, not yet exchanged for an access token.
            return {"authenticated": True, "source": "YOUTUBE_REFRESH_TOKEN"}
        return {
            "authenticated": False,
            "remote": self.remote,
            "authorize_at": self.auth_url_hint if self.remote else None,
            "oauth_client_configured": self._client_config() is not None,
            "token_exists": self.token_path.exists(),
            "client_secret_exists": self.client_secret_path.exists(),
            "client_secret_path": str(self.client_secret_path),
        }
