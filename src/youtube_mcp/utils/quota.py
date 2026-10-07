"""Client-side YouTube API quota tracking.

YouTube Data API v3 has a default daily quota of 10,000 units.
videos.insert does not draw from that pool: it costs 1 unit in a
separate "Video Uploads" bucket limited to 100 uploads per day.
This module tracks both and hard-fails when either is exhausted.
"""

from datetime import date


class QuotaExhaustedError(Exception):
    def __init__(self, used: int, limit: int):
        self.used = used
        self.limit = limit
        super().__init__(
            f"YouTube API quota exhausted: {used}/{limit} units used today. "
            f"Resets at midnight Pacific Time."
        )


# Quota costs per operation type (from Google's documentation)
QUOTA_COSTS = {
    "list": 1,
    "insert": 50,
    "update": 50,
    "delete": 50,
    "search": 100,
    "video_insert": 0,  # counted in the separate uploads bucket
    "caption_insert": 400,
    "caption_update": 450,
    "thumbnail_set": 50,
}


UPLOAD_OPERATIONS = {"video_insert"}


class QuotaTracker:
    def __init__(self, daily_limit: int = 10_000, daily_upload_limit: int = 100):
        self.daily_limit = daily_limit
        self.daily_upload_limit = daily_upload_limit
        self._used = 0
        self._uploads = 0
        self._date = date.today()

    def _reset_if_new_day(self):
        today = date.today()
        if today != self._date:
            self._used = 0
            self._uploads = 0
            self._date = today

    def consume(self, operation: str, count: int = 1):
        """Consume quota units for an operation. Raises QuotaExhaustedError if exhausted."""
        self._reset_if_new_day()
        if operation in UPLOAD_OPERATIONS:
            if self._uploads + count > self.daily_upload_limit:
                raise QuotaExhaustedError(self._uploads, self.daily_upload_limit)
            self._uploads += count
        cost = QUOTA_COSTS.get(operation, 1) * count
        if self._used + cost > self.daily_limit:
            raise QuotaExhaustedError(self._used, self.daily_limit)
        self._used += cost

    @property
    def used(self) -> int:
        self._reset_if_new_day()
        return self._used

    @property
    def remaining(self) -> int:
        self._reset_if_new_day()
        return self.daily_limit - self._used

    def status(self) -> dict:
        self._reset_if_new_day()
        return {
            "used": self._used,
            "remaining": self.remaining,
            "limit": self.daily_limit,
            "uploads_used": self._uploads,
            "uploads_limit": self.daily_upload_limit,
            "date": str(self._date),
        }
