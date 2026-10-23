"""Higher-level analytics built on the Analytics, Data and Reporting APIs.

- youtube_content_table: the YouTube Studio "Content" table (one row per video, with
  titles, type, duration, views, engaged views, average view duration, % viewed...).
- youtube_retention_insights: retention curve turned into checkpoints and drop-off moments.
- youtube_traffic_source_detail: search terms, suggesting videos, external sites...
- youtube_reach: thumbnail impressions and impressions CTR (Reporting API reach report).
- youtube_channel_report: one-call period report with comparison against the previous period.
"""

import csv
import io
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from youtube_mcp.server import auth, mcp, quota
from youtube_mcp.tools.analytics import _default_date_range, _run_analytics_query

REACH_REPORT = "channel_reach_basic_a1"
CONTENT_FILTERS = {
    "shorts": "creatorContentType==shorts",
    "long": "creatorContentType==video_on_demand",
    "live": "creatorContentType==live_stream",
}
TABLE_METRICS = (
    "views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained"
)


# --- helpers -----------------------------------------------------------------------


def _iso_seconds(duration: str | None) -> int | None:
    """'PT1H2M3S' -> 3723."""
    if not duration:
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _clock(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _video_meta(video_ids: list[str]) -> dict[str, dict]:
    """Title, publish date and duration for up to N videos (Data API, 1 unit per 50 ids)."""
    meta: dict[str, dict] = {}
    ids = [v for v in dict.fromkeys(video_ids) if v]
    if not ids:
        return meta
    youtube = auth.build_youtube_service()
    for i in range(0, len(ids), 50):
        quota.consume("list")
        chunk = ids[i : i + 50]
        resp = youtube.videos().list(part="snippet,contentDetails", id=",".join(chunk)).execute()
        for item in resp.get("items", []):
            meta[item["id"]] = {
                "title": item.get("snippet", {}).get("title"),
                "published_at": item.get("snippet", {}).get("publishedAt"),
                "duration_s": _iso_seconds(item.get("contentDetails", {}).get("duration")),
            }
    return meta


def _query_videos(filters: str | None, start_date, end_date, max_results: int) -> list[dict]:
    """Per-video metrics; falls back to a metric set without engagedViews if needed."""
    for metrics in (TABLE_METRICS, TABLE_METRICS.replace("engagedViews,", "")):
        try:
            return _run_analytics_query(
                metrics=metrics,
                dimensions="video",
                filters=filters,
                sort="-views",
                max_results=min(max_results, 200),
                start_date=start_date,
                end_date=end_date,
            )["results"]
        except Exception as e:  # noqa: BLE001
            last = e
            if "engagedViews" not in str(e) and "metric" not in str(e).lower():
                raise
    raise last


def _interp(points: list[tuple[float, float]], x: float) -> float | None:
    """Linear interpolation over sorted (x, y) points."""
    if not points:
        return None
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


# --- tools -------------------------------------------------------------------------


@mcp.tool()
def youtube_content_table(
    start_date: str | None = None,
    end_date: str | None = None,
    content_type: str = "all",
    max_results: int = 50,
) -> dict:
    """The YouTube Studio "Content" table: one row per video for a date range.

    Each row has title, type (short/long/live), publish date, duration, views, engaged
    views, watch time, average view duration, average % viewed, likes, comments, shares
    and subscribers gained. Use it to compare formats (e.g. narrated Shorts vs raw clips).

    Args:
        start_date: Start date (YYYY-MM-DD). Defaults to 28 days ago.
        end_date: End date (YYYY-MM-DD). Defaults to today.
        content_type: "all", "shorts", "long" or "live".
        max_results: Max rows per content type (max 200).
    """
    if not start_date or not end_date:
        start_date, end_date = _default_date_range()
    types = list(CONTENT_FILTERS) if content_type == "all" else [content_type]
    rows = []
    for t in types:
        if t not in CONTENT_FILTERS:
            return {"error": f"content_type must be all, shorts, long or live (got {t})"}
        try:
            for r in _query_videos(CONTENT_FILTERS[t], start_date, end_date, max_results):
                r["type"] = t
                rows.append(r)
        except Exception as e:  # noqa: BLE001
            if t == "live":
                continue  # channels without lives may reject the filter
            raise
    meta = _video_meta([r["video"] for r in rows])
    table = []
    for r in sorted(rows, key=lambda x: -x.get("views", 0)):
        m = meta.get(r["video"], {})
        views = r.get("views", 0)
        table.append({
            "video_id": r["video"],
            "title": m.get("title"),
            "type": r["type"],
            "published_at": m.get("published_at"),
            "duration": _clock(m.get("duration_s")),
            "views": views,
            "engaged_views": r.get("engagedViews"),
            "watch_hours": round(r.get("estimatedMinutesWatched", 0) / 60, 1),
            "avg_view_duration": _clock(r.get("averageViewDuration")),
            "avg_percent_viewed": round(r.get("averageViewPercentage", 0), 1),
            "likes": r.get("likes"),
            "comments": r.get("comments"),
            "shares": r.get("shares"),
            "subscribers_gained": r.get("subscribersGained"),
            "subs_per_1000_views": round(1000 * r.get("subscribersGained", 0) / views, 2) if views else None,
        })
    totals = {"views": sum(r["views"] for r in table), "watch_hours": round(sum(r["watch_hours"] for r in table), 1)}
    return {"start_date": start_date, "end_date": end_date, "totals": totals, "videos": table}


@mcp.tool()
def youtube_retention_insights(
    video_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Audience retention turned into answers: where viewers leave and how many stay.

    Returns the share of viewers still watching at 0:30, 1:00, 3:00, 25/50/75% and the end,
    the 5 biggest drop-offs as timestamps, spikes (rewatched or skipped-to moments) and
    the relative retention vs YouTube videos of similar length (0.5 = median).
    YouTube needs ~100 views before it returns a retention curve.

    Args:
        video_id: YouTube video ID.
        start_date: Start date (YYYY-MM-DD). Defaults to the video's publish date.
        end_date: End date (YYYY-MM-DD). Defaults to today.
    """
    meta = _video_meta([video_id]).get(video_id)
    if not meta:
        return {"error": f"Video not found: {video_id}"}
    dur = meta["duration_s"] or 0
    start_date = start_date or (meta["published_at"] or "2020-01-01")[:10]
    end_date = end_date or date.today().isoformat()
    curve = _run_analytics_query(
        metrics="audienceWatchRatio,relativeRetentionPerformance",
        dimensions="elapsedVideoTimeRatio",
        filters=f"video=={video_id}",
        sort="elapsedVideoTimeRatio",
        start_date=start_date,
        end_date=end_date,
    )["results"]
    if not curve:
        return {"video_id": video_id, "title": meta["title"], "error": "No retention data yet (YouTube needs more views)."}
    pts = [(c["elapsedVideoTimeRatio"] * dur, c["audienceWatchRatio"]) for c in curve]
    rel = [c.get("relativeRetentionPerformance") for c in curve if c.get("relativeRetentionPerformance") is not None]
    checkpoints = {}
    for label, sec in (("0:30", 30), ("1:00", 60), ("3:00", 180)):
        if sec < dur:
            checkpoints[label] = round(100 * _interp(pts, sec), 1)
    for label, frac in (("25%", 0.25), ("50%", 0.5), ("75%", 0.75), ("end", 0.99)):
        checkpoints[f"{label} ({_clock(frac * dur)})"] = round(100 * _interp(pts, frac * dur), 1)
    deltas = [(pts[i][0], pts[i + 1][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1)]
    drops = sorted((d for d in deltas if d[2] < 0), key=lambda d: d[2])[:5]
    spikes = sorted((d for d in deltas if d[2] > 0.01), key=lambda d: -d[2])[:3]
    summary = _run_analytics_query(
        metrics="views,averageViewDuration,averageViewPercentage",
        filters=f"video=={video_id}",
        start_date=start_date,
        end_date=end_date,
    )["results"]
    s = summary[0] if summary else {}
    return {
        "video_id": video_id,
        "title": meta["title"],
        "duration": _clock(dur),
        "views": s.get("views"),
        "avg_view_duration": _clock(s.get("averageViewDuration")),
        "avg_percent_viewed": s.get("averageViewPercentage"),
        "percent_still_watching": checkpoints,
        "biggest_drops": [
            {"from": _clock(a), "to": _clock(b), "viewers_lost_pct": round(-100 * d, 1)} for a, b, d in drops
        ],
        "spikes_rewatched_or_skipped_to": [
            {"from": _clock(a), "to": _clock(b), "gain_pct": round(100 * d, 1)} for a, b, d in spikes
        ],
        "relative_retention_avg": round(sum(rel) / len(rel), 2) if rel else None,
        "relative_retention_first_10pct": round(sum(rel[:10]) / len(rel[:10]), 2) if rel else None,
        "note": "relative retention: 0.5 = median of YouTube videos of similar length; audienceWatchRatio > 1 means rewatches.",
    }


@mcp.tool()
def youtube_traffic_source_detail(
    source_type: str = "YT_SEARCH",
    video_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_results: int = 25,
) -> dict:
    """Detail inside one traffic source: search terms, suggesting videos, external sites...

    source_type values: YT_SEARCH (search terms), RELATED_VIDEO (videos that suggested
    yours), EXT_URL (external websites), PLAYLIST, YT_CHANNEL, NOTIFICATION, SHORTS.
    Each row includes views, watch time and average seconds watched per view, which shows
    whether that traffic is real interest or accidental clicks.

    Args:
        source_type: Traffic source type (see above).
        video_id: Optional video ID to restrict to one video.
        start_date: Start date (YYYY-MM-DD). Defaults to 28 days ago.
        end_date: End date (YYYY-MM-DD). Defaults to today.
        max_results: Rows to return (max 25, an API limit for this dimension).
    """
    filters = f"insightTrafficSourceType=={source_type}"
    if video_id:
        filters += f";video=={video_id}"
    data = _run_analytics_query(
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceDetail",
        filters=filters,
        sort="-views",
        max_results=min(max_results, 25),
        start_date=start_date,
        end_date=end_date,
    )
    rows = data["results"]
    titles = _video_meta([r["insightTrafficSourceDetail"] for r in rows]) if source_type == "RELATED_VIDEO" else {}
    out = []
    for r in rows:
        detail = r["insightTrafficSourceDetail"]
        views = r.get("views", 0)
        item = {
            "detail": detail,
            "views": views,
            "watch_minutes": r.get("estimatedMinutesWatched"),
            "avg_seconds_per_view": round(60 * r.get("estimatedMinutesWatched", 0) / views, 1) if views else None,
        }
        if detail in titles:
            item["title"] = titles[detail]["title"]
        out.append(item)
    return {"source_type": source_type, "video_id": video_id, "start_date": data["start_date"], "end_date": data["end_date"], "rows": out}


def _reach_job(create: bool = True) -> tuple[dict | None, bool]:
    reporting = auth.build_youtube_reporting_service()
    jobs = reporting.jobs().list().execute().get("jobs", [])
    job = next((j for j in jobs if j.get("reportTypeId") == REACH_REPORT), None)
    if job or not create:
        return job, False
    job = reporting.jobs().create(body={"reportTypeId": REACH_REPORT, "name": "reach (mcp)"}).execute()
    return job, True


def _parse_reach_csv(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            impressions = int(float(r.get("video_thumbnail_impressions") or 0))
            ctr = float(r.get("video_thumbnail_impressions_ctr") or 0)
        except ValueError:
            continue
        if ctr > 1:  # tolerate percentages
            ctr /= 100
        rows.append({"date": r.get("date"), "video_id": r.get("video_id"), "impressions": impressions, "ctr": ctr})
    return rows


@mcp.tool()
def youtube_reach(
    days: int = 28,
    video_id: str | None = None,
    max_results: int = 30,
) -> dict:
    """Thumbnail impressions and impressions click-through rate (CTR) per video.

    The Analytics API does not expose impressions/CTR, so this uses the Reporting API
    report "channel_reach_basic_a1". The first call schedules the daily report job if it
    does not exist yet; YouTube then needs about 24-48 hours to produce the first files.
    Later calls download the daily files for the last `days` days and aggregate them.

    Args:
        days: Number of days to cover (Reporting API files are kept ~60 days).
        video_id: Optional video ID to restrict the output.
        max_results: Number of videos to return, sorted by impressions.
    """
    job, created = _reach_job(create=True)
    if created:
        return {
            "status": "job_created",
            "job_id": job.get("id"),
            "message": "Reach report scheduled. YouTube generates the first daily files in ~24-48 h; call again then.",
        }
    reporting = auth.build_youtube_reporting_service()
    since = (datetime.now(timezone.utc) - timedelta(days=days + 2)).strftime("%Y-%m-%dT00:00:00Z")
    reports, page = [], None
    while True:
        resp = reporting.jobs().reports().list(jobId=job["id"], startTimeAtOrAfter=since, pageToken=page).execute()
        reports += resp.get("reports", [])
        page = resp.get("nextPageToken")
        if not page:
            break
    if not reports:
        return {"status": "waiting", "job_id": job["id"], "message": "No reach files yet. YouTube needs ~24-48 h after the job is created."}
    # keep the latest file for each day (YouTube may regenerate a day with corrected data)
    by_day: dict[str, dict] = {}
    for r in reports:
        day = r.get("startTime", "")[:10]
        if day not in by_day or r.get("createTime", "") > by_day[day].get("createTime", ""):
            by_day[day] = r
    headers = {"Authorization": f"Bearer {auth.credentials.token}"}
    agg: dict[str, dict] = {}
    with httpx.Client(follow_redirects=True, timeout=60) as http:
        for day, r in sorted(by_day.items()):
            resp = http.get(r["downloadUrl"], headers=headers)
            resp.raise_for_status()
            for row in _parse_reach_csv(resp.text):
                if video_id and row["video_id"] != video_id:
                    continue
                a = agg.setdefault(row["video_id"], {"impressions": 0, "clicks": 0.0})
                a["impressions"] += row["impressions"]
                a["clicks"] += row["impressions"] * row["ctr"]
    top = sorted(agg.items(), key=lambda kv: -kv[1]["impressions"])[:max_results]
    meta = _video_meta([v for v, _ in top])
    videos = [{
        "video_id": v,
        "title": meta.get(v, {}).get("title"),
        "impressions": a["impressions"],
        "ctr_percent": round(100 * a["clicks"] / a["impressions"], 2) if a["impressions"] else None,
    } for v, a in top]
    tot_i = sum(a["impressions"] for a in agg.values())
    tot_c = sum(a["clicks"] for a in agg.values())
    return {
        "days_covered": sorted(by_day)[0] + " .. " + sorted(by_day)[-1],
        "total_impressions": tot_i,
        "channel_ctr_percent": round(100 * tot_c / tot_i, 2) if tot_i else None,
        "videos": videos,
        "note": "Impressions/CTR mostly reflect long-form thumbnails (home, suggested, search). Shorts feed views are not thumbnail impressions.",
    }


@mcp.tool()
def youtube_channel_report(days: int = 7) -> dict:
    """One-call report for the last N days compared with the previous N days.

    Includes totals (views, watch hours, net subscribers, average view duration) split
    into Shorts and long-form, the change vs the previous period, the top videos of the
    period and where views came from. Analytics data usually lags 1-3 days.

    Args:
        days: Length of the period in days (e.g. 7 for a weekly report, 28 for monthly).
    """
    end = date.today()
    cur = ((end - timedelta(days=days)).isoformat(), end.isoformat())
    prev = ((end - timedelta(days=2 * days)).isoformat(), (end - timedelta(days=days + 1)).isoformat())
    metrics = "views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost"

    def totals(period, filters=None):
        r = _run_analytics_query(metrics=metrics, filters=filters, start_date=period[0], end_date=period[1])["results"]
        r = r[0] if r else {}
        return {
            "views": r.get("views", 0),
            "watch_hours": round(r.get("estimatedMinutesWatched", 0) / 60, 1),
            "net_subscribers": r.get("subscribersGained", 0) - r.get("subscribersLost", 0),
            "avg_view_duration": _clock(r.get("averageViewDuration")),
        }

    def change(a, b):
        return None if not b else round(100 * (a - b) / b, 1)

    report = {"period": {"current": cur, "previous": prev}, "totals": {}}
    for label, f in (("all", None), ("shorts", CONTENT_FILTERS["shorts"]), ("long", CONTENT_FILTERS["long"])):
        c, p = totals(cur, f), totals(prev, f)
        report["totals"][label] = {
            **c,
            "views_change_pct": change(c["views"], p["views"]),
            "watch_hours_change_pct": change(c["watch_hours"], p["watch_hours"]),
            "previous_views": p["views"],
        }
    table = youtube_content_table(start_date=cur[0], end_date=cur[1], content_type="all", max_results=10)
    report["top_videos"] = table["videos"][:8]
    src = _run_analytics_query(
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        sort="-views",
        start_date=cur[0],
        end_date=cur[1],
    )["results"]
    total_views = sum(s.get("views", 0) for s in src) or 1
    report["traffic_sources"] = [
        {"source": s["insightTrafficSourceType"], "views": s["views"], "share_pct": round(100 * s["views"] / total_views, 1)}
        for s in src
    ]
    return report
