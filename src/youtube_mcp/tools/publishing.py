"""Video publishing tools — upload, update metadata, thumbnails, delete."""

import os

from googleapiclient.http import MediaFileUpload

from youtube_mcp.server import auth, mcp, quota
from youtube_mcp.utils.fetch import download_to_temp


@mcp.tool()
def youtube_upload_video(
    file_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "22",
    privacy_status: str = "private",
    publish_at: str | None = None,
) -> dict:
    """Upload a video to YouTube.

    Uses 1 of the 100 daily uploads (separate quota bucket). Video is uploaded as private
    by default. For files that live on Google Drive or any URL (the usual case when this
    server runs remotely), use youtube_upload_from_url instead.

    Args:
        file_path: Absolute path to the video file
        title: Video title (max 100 characters)
        description: Video description (max 5,000 characters)
        tags: List of tags
        category_id: YouTube category ID (default "22" = People & Blogs)
        privacy_status: "private", "public", or "unlisted"
        publish_at: ISO 8601 datetime to schedule publishing (requires privacy_status="private")
    """
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    quota.consume("video_insert")
    youtube = auth.build_youtube_service()

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    if publish_at and privacy_status == "private":
        body["status"]["publishAt"] = publish_at

    media = MediaFileUpload(file_path, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = request.execute()

    return {
        "id": response["id"],
        "title": response["snippet"]["title"],
        "privacy": response["status"]["privacyStatus"],
        "publish_at": response["status"].get("publishAt"),
        "url": f"https://www.youtube.com/watch?v={response['id']}",
        "quota_cost": {"uploads_bucket": 1},
    }


@mcp.tool()
def youtube_update_video(
    video_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    category_id: str | None = None,
    privacy_status: str | None = None,
    publish_at: str | None = None,
    made_for_kids: bool | None = None,
) -> dict:
    """Update metadata for an existing video.

    Only provided fields are updated; others remain unchanged.

    Args:
        video_id: YouTube video ID
        title: New title (max 100 characters)
        description: New description (max 5,000 characters)
        tags: New tags (replaces existing tags)
        category_id: New category ID
        privacy_status: "private", "public", or "unlisted"
        publish_at: ISO 8601 datetime to schedule publishing (requires privacy_status="private" — either passed explicitly here or already set on the video).
        made_for_kids: COPPA self-declaration. Pass True/False to set selfDeclaredMadeForKids. Videos that were uploaded without this declaration cannot be published until it is set.
    """
    quota.consume("list")
    youtube = auth.build_youtube_service()

    # Fetch current video data first
    current = youtube.videos().list(part="snippet,status", id=video_id).execute()
    items = current.get("items", [])
    if not items:
        return {"error": f"Video not found: {video_id}"}

    video = items[0]
    snippet = video["snippet"]
    status = video["status"]

    # Update only provided fields
    if title is not None:
        snippet["title"] = title[:100]
    if description is not None:
        snippet["description"] = description[:5000]
    if tags is not None:
        snippet["tags"] = tags
    if category_id is not None:
        snippet["categoryId"] = category_id

    body = {"id": video_id, "snippet": snippet}

    if privacy_status is not None or publish_at is not None or made_for_kids is not None:
        effective_privacy = privacy_status or status.get("privacyStatus", "private")
        new_status = {"privacyStatus": effective_privacy}
        if publish_at:
            if effective_privacy != "private":
                return {"error": "publish_at requires privacy_status='private'"}
            new_status["publishAt"] = publish_at
        if made_for_kids is not None:
            new_status["selfDeclaredMadeForKids"] = made_for_kids
        body["status"] = new_status
        parts = "snippet,status"
    else:
        parts = "snippet"

    quota.consume("update")
    response = youtube.videos().update(part=parts, body=body).execute()

    # part='snippet' responses omit the 'status' block entirely, so access it
    # defensively. Only surface these fields when the server actually
    # returned them.
    response_status = response.get("status") or {}
    return {
        "id": response["id"],
        "title": response["snippet"]["title"],
        "privacy": response_status.get("privacyStatus"),
        "publish_at": response_status.get("publishAt"),
        "updated": True,
    }


def _image_mimetype(path: str) -> str | None:
    """Sniff the image type (downloaded files have no extension)."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    return None


@mcp.tool()
def youtube_set_thumbnail(
    video_id: str,
    file_path: str | None = None,
    image_url: str | None = None,
) -> dict:
    """Upload a custom thumbnail for a video.

    Provide either a local file path or a URL (a Google Drive share link works if the
    file is shared as "Anyone with the link").

    Args:
        video_id: YouTube video ID
        file_path: Absolute path to the thumbnail image (JPEG, PNG, GIF, BMP; max 2MB)
        image_url: URL of the thumbnail image (alternative to file_path)
    """
    tmp = None
    if image_url:
        try:
            tmp = download_to_temp(image_url, max_bytes=2 * 1024 * 1024)
        except Exception as e:
            return {"error": f"Could not download thumbnail: {e}"}
        file_path = str(tmp)
    if not file_path or not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    try:
        quota.consume("thumbnail_set")
        youtube = auth.build_youtube_service()

        mimetype = _image_mimetype(file_path) if tmp else None  # local: guess by extension
        media = MediaFileUpload(file_path, mimetype=mimetype)
        response = youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)

    items = response.get("items", [])
    if items:
        return {
            "video_id": video_id,
            "thumbnail_url": items[0].get("default", {}).get("url"),
            "updated": True,
        }

    return {"video_id": video_id, "updated": True}


@mcp.tool()
def youtube_delete_video(video_id: str) -> dict:
    """Delete a video. This action is irreversible.

    Args:
        video_id: YouTube video ID to delete
    """
    quota.consume("delete")
    youtube = auth.build_youtube_service()

    youtube.videos().delete(id=video_id).execute()

    return {"video_id": video_id, "deleted": True}
