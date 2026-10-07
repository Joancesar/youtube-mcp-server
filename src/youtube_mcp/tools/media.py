"""Remote-friendly media tools.

- youtube_upload_from_url: stream a video from a URL (e.g. a Google Drive share link)
  straight into a YouTube resumable upload, in a background thread. Nothing is buffered
  on disk, so multi-GB files work on small containers.
- youtube_upload_status: poll background upload jobs.
- youtube_upload_caption: upload a subtitle track (.srt/.vtt) from text or a URL.
"""

import io
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import httpx
from googleapiclient.http import MediaIoBaseUpload

from youtube_mcp.server import auth, mcp, quota
from youtube_mcp.utils.fetch import USER_AGENT, check_not_html, fetch_text, normalize_url

log = logging.getLogger(__name__)

UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK_SIZE = 32 * 256 * 1024  # 8 MiB; YouTube requires multiples of 256 KiB
MAX_RETRIES = 6

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _token() -> str:
    # auth.credentials refreshes the access token when it has expired.
    return auth.credentials.token


# --- Resumable upload protocol -------------------------------------------------


def _start_session(http: httpx.Client, body: dict, total: int | None, ctype: str) -> str:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": ctype,
    }
    if total:
        headers["X-Upload-Content-Length"] = str(total)
    r = http.post(
        UPLOAD_ENDPOINT,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers=headers,
        json=body,
    )
    if r.status_code != 200 or "location" not in r.headers:
        raise RuntimeError(f"Could not start upload session: HTTP {r.status_code} {r.text[:500]}")
    return r.headers["location"]


def _put_chunk(
    http: httpx.Client, session: str, data: bytes, start: int, total_str: str
) -> httpx.Response:
    """PUT one chunk. On transient errors, ask the server how much it has and return that
    308 response so the caller can resume from the right offset."""
    for attempt in range(MAX_RETRIES):
        try:
            if data:
                content_range = f"bytes {start}-{start + len(data) - 1}/{total_str}"
            else:
                content_range = f"bytes */{total_str}"
            r = http.put(
                session,
                content=data,
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "Content-Range": content_range,
                },
            )
            if r.status_code < 500 and r.status_code != 429:
                return r
            log.warning("Upload chunk got HTTP %s, retrying", r.status_code)
        except httpx.TransportError as e:
            log.warning("Upload chunk transport error: %s, retrying", e)
        time.sleep(min(2**attempt, 60))
        try:
            r = http.put(
                session,
                content=b"",
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "Content-Range": f"bytes */{total_str}",
                },
            )
            if r.status_code in (200, 201, 308):
                return r
        except httpx.TransportError:
            pass
    raise RuntimeError("Upload failed after several retries")


def _stream_upload(http, session: str, source, total: int | None, job_id: str) -> dict:
    offset = 0  # bytes YouTube has committed
    buf = bytearray()
    it = iter(source)
    eof = False
    while True:
        while not eof and len(buf) < CHUNK_SIZE:
            try:
                buf += next(it)
            except StopIteration:
                eof = True
        last = eof and len(buf) <= CHUNK_SIZE
        data = bytes(buf) if last else bytes(buf[:CHUNK_SIZE])
        if last:
            total_str = str(offset + len(data))
        else:
            total_str = str(total) if total else "*"
        r = _put_chunk(http, session, data, offset, total_str)
        if r.status_code in (200, 201):
            _update(job_id, bytes_sent=offset + len(data))
            return r.json()
        if r.status_code != 308:
            raise RuntimeError(f"Upload rejected: HTTP {r.status_code} {r.text[:500]}")
        rng = r.headers.get("range")  # e.g. "bytes=0-8388607"
        committed = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
        if committed < offset:
            raise RuntimeError("YouTube lost already-sent data; restart the upload")
        del buf[: committed - offset]
        offset = committed
        _update(job_id, bytes_sent=offset)


def _run_upload(job_id: str, source_url: str, body: dict, thumbnail_url, playlist_id) -> None:
    try:
        url = normalize_url(source_url)
        timeout = httpx.Timeout(60.0, read=600.0, write=600.0)
        with httpx.Client(follow_redirects=True, timeout=timeout) as http:
            with http.stream("GET", url, headers={"User-Agent": USER_AGENT}) as src:
                src.raise_for_status()
                check_not_html(src, source_url)
                total = int(src.headers.get("content-length") or 0) or None
                ctype = src.headers.get("content-type", "")
                if not ctype.startswith("video/"):
                    ctype = "video/*"  # Drive serves application/octet-stream
                _update(job_id, status="uploading", bytes_total=total)
                session = _start_session(http, body, total, ctype)
                video = _stream_upload(http, session, src.iter_bytes(1 << 20), total, job_id)

        video_id = video["id"]
        status = video.get("status", {})
        result = {
            "status": "done",
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "privacy": status.get("privacyStatus"),
            "upload_status": status.get("uploadStatus"),
            "notes": [],
        }
        if thumbnail_url:
            from youtube_mcp.tools.publishing import youtube_set_thumbnail

            t = youtube_set_thumbnail(video_id, image_url=thumbnail_url)
            result["notes"].append(f"thumbnail: {t.get('error') or 'set'}")
        if playlist_id:
            quota.consume("insert")
            auth.build_youtube_service().playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
            result["notes"].append(f"added to playlist {playlist_id}")
        _update(job_id, finished_at=_now(), **result)
    except Exception as e:  # noqa: BLE001 - surface any failure in the job record
        log.exception("Upload job %s failed", job_id)
        _update(job_id, status="error", error=str(e), finished_at=_now())


# --- Tools -----------------------------------------------------------------------


@mcp.tool()
def youtube_upload_from_url(
    source_url: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "24",
    privacy_status: str = "private",
    publish_at: str | None = None,
    made_for_kids: bool = False,
    language: str = "es",
    thumbnail_url: str | None = None,
    playlist_id: str | None = None,
) -> dict:
    """Upload a video to YouTube from a URL, in the background. Returns a job_id at once.

    The file is streamed from the URL into YouTube without touching disk, so large
    files (several GB) are fine. Google Drive share links work if the file is shared as
    "Anyone with the link". Poll progress with youtube_upload_status(job_id).

    Uses 1 of the 100 daily uploads. NOTE: while the Google Cloud project has not passed
    YouTube's API compliance audit, YouTube locks API uploads as private.

    Args:
        source_url: Direct URL or Google Drive share link of the video file
        title: Video title (max 100 characters)
        description: Video description (max 5,000 characters); chapters go here as
            "00:00 Intro" lines
        tags: List of tags
        category_id: YouTube category ID (default "24" = Entertainment; "1" = Film &
            Animation)
        privacy_status: "private", "public", or "unlisted"
        publish_at: ISO 8601 UTC datetime to schedule publishing (forces private until then)
        made_for_kids: COPPA self-declaration (default False)
        language: Language of title/description and audio (default "es")
        thumbnail_url: Optional thumbnail image URL to set once the upload finishes
        playlist_id: Optional playlist to add the video to once uploaded
    """
    status = {
        "privacyStatus": "private" if publish_at else privacy_status,
        "selfDeclaredMadeForKids": made_for_kids,
    }
    if publish_at:
        status["publishAt"] = publish_at
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": category_id,
            "defaultLanguage": language,
            "defaultAudioLanguage": language,
        },
        "status": status,
    }

    # Fail fast on auth and quota before starting the thread.
    try:
        auth.credentials
    except Exception as e:
        return {"error": str(e)}
    quota.consume("video_insert")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "downloading",
            "title": title[:100],
            "source_url": source_url,
            "started_at": _now(),
            "bytes_total": None,
            "bytes_sent": 0,
        }
    threading.Thread(
        target=_run_upload,
        args=(job_id, source_url, body, thumbnail_url, playlist_id),
        daemon=True,
        name=f"upload-{job_id}",
    ).start()
    return {"job_id": job_id, "status": "started", "next": "youtube_upload_status(job_id)"}


@mcp.tool()
def youtube_upload_status(job_id: str | None = None) -> dict:
    """Check background upload jobs started with youtube_upload_from_url.

    Jobs live in memory and are lost if the server restarts.

    Args:
        job_id: Job to check. Omit to list all jobs since the server started.
    """
    with _jobs_lock:
        if job_id:
            job = _jobs.get(job_id)
            if not job:
                return {"error": f"Unknown job_id {job_id} (the server may have restarted)"}
            jobs = [dict(job)]
        else:
            jobs = [dict(j) for j in _jobs.values()]
    for j in jobs:
        if j.get("bytes_total"):
            j["progress_pct"] = round(100 * j["bytes_sent"] / j["bytes_total"], 1)
    return {"jobs": jobs} if not job_id else jobs[0]


@mcp.tool()
def youtube_upload_caption(
    video_id: str,
    language: str = "es",
    name: str = "",
    srt_url: str | None = None,
    srt_text: str | None = None,
    is_draft: bool = False,
) -> dict:
    """Upload a subtitle/caption track (.srt or .vtt) to one of your videos.

    Costs 400 quota units. Provide the file as a URL (Drive share links work) or as
    raw text.

    Args:
        video_id: YouTube video ID (must be on the authorized channel)
        language: BCP-47 language of the track (default "es")
        name: Track name shown to viewers (empty = default track for that language)
        srt_url: URL of the .srt/.vtt file
        srt_text: Raw .srt/.vtt content (alternative to srt_url)
        is_draft: Upload as draft (not visible to viewers)
    """
    if not srt_text and not srt_url:
        return {"error": "Provide srt_url or srt_text"}
    if not srt_text:
        try:
            srt_text = fetch_text(srt_url)
        except Exception as e:
            return {"error": f"Could not download subtitles: {e}"}

    quota.consume("caption_insert")
    youtube = auth.build_youtube_service()
    media = MediaIoBaseUpload(
        io.BytesIO(srt_text.encode("utf-8")), mimetype="application/octet-stream"
    )
    response = youtube.captions().insert(
        part="snippet",
        body={
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": name,
                "isDraft": is_draft,
            }
        },
        media_body=media,
    ).execute()
    snippet = response.get("snippet", {})
    return {
        "caption_id": response.get("id"),
        "video_id": video_id,
        "language": snippet.get("language"),
        "name": snippet.get("name"),
        "status": snippet.get("status"),
        "quota_cost": 400,
    }
