# Remote Deployment (Railway / any container host)

This fork adds a streamable-HTTP mode so the server can run remotely and be added to
claude.ai as a custom connector.

1. Deploy the repo (the `Dockerfile` sets `MCP_TRANSPORT=streamable-http` and stores the
   OAuth token in `/data`; mount a persistent volume there).
2. Set the variables:
   - `MCP_SECRET_PATH`: long random string. The MCP endpoint is `https://<host>/mcp/<secret>`
     (claude.ai connectors can't send headers, so the secret path is the access control).
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`: an OAuth client of type **Web application**
     with redirect URI `https://<host>/oauth/callback`.
   - `PUBLIC_BASE_URL` (optional on Railway, which provides `RAILWAY_PUBLIC_DOMAIN`).
   - `YOUTUBE_REFRESH_TOKEN` (optional): use instead of the volume.
3. Open `https://<host>/oauth/start?key=<secret>` signed in as the channel owner/manager and
   accept. The page confirms which channel was authorized.
4. Add `https://<host>/mcp/<secret>` as a custom connector in Claude.

## Google Cloud setup

1. Create a project and enable **YouTube Data API v3**, **YouTube Analytics API** and
   **YouTube Reporting API**.
2. OAuth consent screen: External. Then **publish the app** ("In production"). In "Testing"
   mode Google expires refresh tokens after 7 days. An unverified app shows a warning
   screen during consent; the owner can continue past it.
3. Credentials > Create OAuth client ID > **Web application**, redirect URI
   `https://<host>/oauth/callback`.

## Remote-friendly tools added in this fork

| Tool | Description |
|------|-------------|
| `youtube_upload_from_url` | Background upload streamed from a URL or Google Drive share link (no disk buffering) |
| `youtube_upload_status` | Progress of background uploads |
| `youtube_upload_caption` | Upload an .srt/.vtt subtitle track from a URL or raw text (400 units) |
| `youtube_set_thumbnail` | Now also accepts `image_url` |

## Notes

- Videos uploaded through the API by projects that have not passed YouTube's API
  compliance audit are locked as private.
- `videos.insert` does not use the 10,000-unit pool: it costs 1 unit in a separate
  "Video Uploads" bucket limited to 100 uploads/day.
- Upload jobs are kept in memory; a redeploy during an upload loses the job.
