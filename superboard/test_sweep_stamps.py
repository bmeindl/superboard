"""Archive completion stamps: date-only and offset-free `@done-at` values must not crash the sweep."""
from datetime import datetime, timezone

import sweep


def test_done_stamp_timezones_and_invalid_fallback() -> None:
    for stamp in ("2020-01-01T12:00:00", "2020-01-01T12:00:00Z", "2020-01-01T14:00:00+02:00"):
        assert sweep.done_at({"done_at": stamp}) == datetime(2020, 1, 1, 12, tzinfo=timezone.utc)
    assert sweep.done_at({"done_at": "invalid", "date": "2020-01-01"}) == \
        datetime(2020, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
    assert sweep.done_at({"done_at": "2020-01-01T00:00:00"}) == datetime(2020, 1, 1, tzinfo=timezone.utc)
    # Date-only hand edits keep the full end-of-day retention.
    assert sweep.done_at({"done_at": "2020-01-02", "date": "2020-01-01"}) == \
        datetime(2020, 1, 2, 23, 59, 59, tzinfo=timezone.utc)
