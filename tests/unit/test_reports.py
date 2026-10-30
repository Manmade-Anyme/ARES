from datetime import datetime, timezone

import pytest

from reports import is_performance_report_window


@pytest.mark.parametrize(
    ("now_ist", "expected"),
    [
        (datetime(2026, 10, 30, 15, 27, 59), False),
        (datetime(2026, 10, 30, 15, 28), True),
        (datetime(2026, 10, 30, 15, 29, 59), True),
        (datetime(2026, 10, 30, 15, 30), False),
        (datetime(2026, 10, 30, 9, 58, tzinfo=timezone.utc), True),
    ],
)
def test_performance_report_window_starts_at_1528_ist_and_ends_before_1530(now_ist, expected):
    assert is_performance_report_window(now_ist) is expected
