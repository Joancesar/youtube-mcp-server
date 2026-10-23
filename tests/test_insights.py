"""Tests for higher-level analytics (insights) tools with mocked APIs."""

from unittest.mock import MagicMock, patch

from youtube_mcp.tools import insights


def _meta(ids, dur="PT10M"):
    return {v: {"title": f"Title {v}", "published_at": "2026-10-01T00:00:00Z", "duration_s": insights._iso_seconds(dur)} for v in ids}


class TestHelpers:
    def test_iso_seconds(self):
        assert insights._iso_seconds("PT1H2M3S") == 3723
        assert insights._iso_seconds("PT45S") == 45
        assert insights._iso_seconds("P1DT1S") == 86401
        assert insights._iso_seconds(None) is None

    def test_clock(self):
        assert insights._clock(65) == "1:05"
        assert insights._clock(3723) == "1:02:03"
        assert insights._clock(None) is None

    def test_interp(self):
        pts = [(0, 1.0), (10, 0.5), (20, 0.25)]
        assert insights._interp(pts, 5) == 0.75
        assert insights._interp(pts, 30) == 0.25
        assert insights._interp([], 3) is None

    def test_parse_reach_csv(self):
        text = (
            "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
            "20261020,UC1,v1,1000,0.05\n"
            "20261020,UC1,v2,200,4.5\n"
        )
        rows = insights._parse_reach_csv(text)
        assert rows[0] == {"date": "20261020", "video_id": "v1", "impressions": 1000, "ctr": 0.05}
        assert rows[1]["ctr"] == 0.045


class TestContentTable:
    @patch("youtube_mcp.tools.insights._video_meta")
    @patch("youtube_mcp.tools.insights._run_analytics_query")
    def test_table(self, mock_q, mock_meta):
        def q(**kw):
            if kw["filters"].endswith("shorts"):
                return {"results": [{"video": "s1", "views": 1000, "engagedViews": 900, "estimatedMinutesWatched": 120,
                                     "averageViewDuration": 7.2, "averageViewPercentage": 80.0, "likes": 50,
                                     "comments": 2, "shares": 3, "subscribersGained": 5}]}
            if kw["filters"].endswith("video_on_demand"):
                return {"results": [{"video": "l1", "views": 20, "engagedViews": 20, "estimatedMinutesWatched": 60,
                                     "averageViewDuration": 180, "averageViewPercentage": 10.7, "likes": 1,
                                     "comments": 0, "shares": 0, "subscribersGained": 1}]}
            raise RuntimeError("live not supported")

        mock_q.side_effect = q
        mock_meta.side_effect = lambda ids: _meta(ids)
        out = insights.youtube_content_table("2026-10-01", "2026-10-21")
        assert [v["video_id"] for v in out["videos"]] == ["s1", "l1"]
        assert out["videos"][0]["type"] == "shorts"
        assert out["videos"][0]["subs_per_1000_views"] == 5.0
        assert out["videos"][1]["avg_view_duration"] == "3:00"
        assert out["totals"]["views"] == 1020

    def test_bad_type(self):
        assert "error" in insights.youtube_content_table("2026-10-01", "2026-10-21", content_type="foo")


class TestRetention:
    @patch("youtube_mcp.tools.insights._video_meta")
    @patch("youtube_mcp.tools.insights._run_analytics_query")
    def test_insights(self, mock_q, mock_meta):
        mock_meta.return_value = _meta(["v1"], "PT10M")
        curve = [{"elapsedVideoTimeRatio": i / 100, "audienceWatchRatio": 1 - i / 100,
                  "relativeRetentionPerformance": 0.4} for i in range(0, 100)]
        curve[10]["audienceWatchRatio"] = 0.5  # sharp drop at 10%
        mock_q.side_effect = [
            {"results": curve},
            {"results": [{"views": 300, "averageViewDuration": 180, "averageViewPercentage": 30.0}]},
        ]
        out = insights.youtube_retention_insights("v1")
        assert out["duration"] == "10:00"
        assert out["biggest_drops"][0]["from"] == "0:54"
        assert "1:00" in out["percent_still_watching"]
        assert out["relative_retention_avg"] == 0.4
        assert mock_q.call_args_list[0].kwargs["start_date"] == "2026-10-01"

    @patch("youtube_mcp.tools.insights._video_meta", return_value={})
    def test_not_found(self, _):
        assert "error" in insights.youtube_retention_insights("nope")


class TestTrafficDetail:
    @patch("youtube_mcp.tools.insights._video_meta")
    @patch("youtube_mcp.tools.insights._run_analytics_query")
    def test_related(self, mock_q, mock_meta):
        mock_q.return_value = {"start_date": "a", "end_date": "b", "results": [
            {"insightTrafficSourceDetail": "x1", "views": 10, "estimatedMinutesWatched": 5}]}
        mock_meta.side_effect = lambda ids: _meta(ids)
        out = insights.youtube_traffic_source_detail("RELATED_VIDEO", video_id="v1")
        assert out["rows"][0]["title"] == "Title x1"
        assert out["rows"][0]["avg_seconds_per_view"] == 30.0
        assert mock_q.call_args.kwargs["filters"] == "insightTrafficSourceType==RELATED_VIDEO;video==v1"


class TestReach:
    @patch("youtube_mcp.tools.insights.auth")
    def test_creates_job(self, mock_auth):
        rep = MagicMock()
        mock_auth.build_youtube_reporting_service.return_value = rep
        rep.jobs().list().execute.return_value = {"jobs": []}
        rep.jobs().create().execute.return_value = {"id": "j1"}
        out = insights.youtube_reach()
        assert out["status"] == "job_created"

    @patch("youtube_mcp.tools.insights._video_meta")
    @patch("youtube_mcp.tools.insights.httpx")
    @patch("youtube_mcp.tools.insights.auth")
    def test_aggregates(self, mock_auth, mock_httpx, mock_meta):
        rep = MagicMock()
        mock_auth.build_youtube_reporting_service.return_value = rep
        rep.jobs().list().execute.return_value = {"jobs": [{"id": "j1", "reportTypeId": "channel_reach_basic_a1"}]}
        rep.jobs().reports().list().execute.return_value = {"reports": [
            {"startTime": "2026-10-20T07:00:00Z", "createTime": "2026-10-21T00:00:00Z", "downloadUrl": "u1"},
            {"startTime": "2026-10-20T07:00:00Z", "createTime": "2026-10-22T00:00:00Z", "downloadUrl": "u2"},
            {"startTime": "2026-10-21T07:00:00Z", "createTime": "2026-10-22T00:00:00Z", "downloadUrl": "u3"},
        ]}
        files = {
            "u2": "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n20261020,UC,v1,1000,0.04\n",
            "u3": "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n20261021,UC,v1,1000,0.06\n20261021,UC,v2,100,0.10\n",
        }
        http = mock_httpx.Client.return_value.__enter__.return_value
        http.get.side_effect = lambda url, headers: MagicMock(text=files[url])
        mock_meta.side_effect = lambda ids: _meta(ids)
        out = insights.youtube_reach(days=7)
        assert [c.args[0] for c in http.get.call_args_list] == ["u2", "u3"]  # latest file per day
        assert out["total_impressions"] == 2100
        assert out["videos"][0] == {"video_id": "v1", "title": "Title v1", "impressions": 2000, "ctr_percent": 5.0}
        assert out["channel_ctr_percent"] == round(100 * 110 / 2100, 2)


class TestChannelReport:
    @patch("youtube_mcp.tools.insights.youtube_content_table")
    @patch("youtube_mcp.tools.insights._run_analytics_query")
    def test_report(self, mock_q, mock_table):
        cur = {"views": 200, "estimatedMinutesWatched": 600, "averageViewDuration": 30,
               "subscribersGained": 5, "subscribersLost": 1}
        prev = {"views": 100, "estimatedMinutesWatched": 300, "averageViewDuration": 30,
                "subscribersGained": 1, "subscribersLost": 0}

        def q(**kw):
            if kw.get("dimensions") == "insightTrafficSourceType":
                return {"results": [{"insightTrafficSourceType": "SHORTS", "views": 150},
                                    {"insightTrafficSourceType": "YT_SEARCH", "views": 50}]}
            return {"results": [cur if kw["end_date"] == insights.date.today().isoformat() else prev]}

        mock_q.side_effect = q
        mock_table.return_value = {"videos": [{"video_id": "v1"}]}
        out = insights.youtube_channel_report(days=7)
        assert out["totals"]["all"]["views_change_pct"] == 100.0
        assert out["totals"]["all"]["net_subscribers"] == 4
        assert out["traffic_sources"][0]["share_pct"] == 75.0
