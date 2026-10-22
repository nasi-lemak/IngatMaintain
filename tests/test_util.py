from datetime import date

from ingatmaintain.util import (
    add_months,
    item_status,
    normalise_reminder_days,
    parse_reminder_days,
    reminder_bucket,
)

TODAY = date(2026, 10, 22)


def test_parse_reminder_days_sorts_and_dedupes():
    assert parse_reminder_days("30, 180,7;30, x, -1") == [180, 30, 7]
    assert normalise_reminder_days(" 7,90 ") == "90,7"


def test_add_months_clamps_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_item_status_bands():
    days = "180,90,30"
    assert item_status("2028-10-21", days, TODAY)["key"] == "ok"
    assert item_status("2027-03-01", days, TODAY)["key"] == "upcoming"   # 130 days
    assert item_status("2026-11-10", days, TODAY)["key"] == "urgent"     # 19 days
    assert item_status("2026-10-21", days, TODAY)["key"] == "overdue"
    assert item_status(None, days, TODAY)["key"] == "none"


def test_reminder_bucket():
    thresholds = [180, 90, 30, 7]
    assert reminder_bucket(200, thresholds) is None
    assert reminder_bucket(180, thresholds) == 180
    assert reminder_bucket(100, thresholds) == 180
    assert reminder_bucket(89, thresholds) == 90
    assert reminder_bucket(0, thresholds) == 7
    assert reminder_bucket(-1, thresholds) == -1
    assert reminder_bucket(-7, thresholds) == -1
    assert reminder_bucket(-8, thresholds) == -2
