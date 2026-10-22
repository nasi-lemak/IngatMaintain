"""Date helpers and maintenance status rules."""

import calendar
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import current_app

DEFAULT_REMINDER_DAYS = "180,90,60,30,14,7,1"
URGENT_DAYS = 30

CATEGORIES = [
    "SSL certificate",
    "Software deployment",
    "Software licence",
    "OS / security patching",
    "Antivirus / definitions",
    "Backup check",
    "Hardware check",
    "Other",
]


def local_tz():
    return ZoneInfo(current_app.config["TIMEZONE"])


def today():
    return datetime.now(local_tz()).date()


def now_str():
    return datetime.now(local_tz()).strftime("%Y-%m-%d %H:%M")


def parse_date(value):
    """Parse YYYY-MM-DD; empty -> None, bad input -> ValueError."""
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


def parse_reminder_days(value):
    """'180, 90,30' -> [180, 90, 30] (unique, descending, non-negative)."""
    days = set()
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            days.add(int(part))
    return sorted(days, reverse=True)


def normalise_reminder_days(value):
    days = parse_reminder_days(value)
    if not days:
        raise ValueError("Enter at least one reminder, e.g. 90,30,7")
    return ",".join(str(d) for d in days)


def add_months(d, months):
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def describe_days(days_left):
    if days_left is None:
        return "No due date"
    if days_left < 0:
        n = -days_left
        return f"Overdue by {n} day{'s' if n != 1 else ''}"
    if days_left == 0:
        return "Due today"
    if days_left == 1:
        return "Due tomorrow"
    if days_left < 60:
        return f"Due in {days_left} days"
    if days_left < 730:
        return f"Due in {days_left} days (~{round(days_left / 30.44)} months)"
    return f"Due in {days_left} days (~{days_left / 365.25:.1f} years)"


def item_status(due_date, reminder_days, on_date=None):
    """Classify a maintenance item.

    overdue  - past the due date
    urgent   - within URGENT_DAYS (or the first reminder, if that is sooner)
    upcoming - inside the reminder window (first reminder has fired)
    ok       - nothing to do yet
    """
    on_date = on_date or today()
    due = parse_date(due_date) if isinstance(due_date, str) else due_date
    if due is None:
        return {"key": "none", "label": "No due date", "days_left": None}
    days_left = (due - on_date).days
    thresholds = parse_reminder_days(reminder_days) or [URGENT_DAYS]
    window = max(thresholds)
    if days_left < 0:
        key = "overdue"
    elif days_left <= min(URGENT_DAYS, window):
        key = "urgent"
    elif days_left <= window:
        key = "upcoming"
    else:
        key = "ok"
    return {"key": key, "label": describe_days(days_left), "days_left": days_left}


def reminder_bucket(days_left, thresholds):
    """Which reminder (if any) applies today.

    Before the due date: the tightest threshold already crossed, so a reminder
    is sent once per threshold. After the due date: one per overdue week
    (-1 = first week overdue, -2 = second week, ...).
    """
    if days_left < 0:
        return -1 - ((-days_left - 1) // 7)
    crossed = [t for t in thresholds if days_left <= t]
    return min(crossed) if crossed else None


def fmt_date(value):
    if not value:
        return ""
    try:
        d = parse_date(value) if isinstance(value, str) else value
    except ValueError:
        return value
    return f"{d.day} {d.strftime('%b %Y')}"


def version_key(version):
    """Sort key for version strings.

    '2.10.1' > '2.9', 'v1.2' == '1.2' == '1.2.0', and pre-releases sort before
    the release: '2.4.0-rc1' < '2.4.0' < '2.4.0.1'.
    """
    v = (version or "").strip().lower().lstrip("v")
    parts = [(2, int(p), "") if p.isdigit() else (0, 0, p) for p in re.findall(r"\d+|[a-z]+", v)]
    # Zeros not followed by another number don't count (1.2.0 == 1.2, 2.4.0-rc1 == 2.4-rc1).
    # Walk right to left so 1.0.0 collapses to 1.
    key, next_is_number = [], False
    for part in reversed(parts):
        if part == (2, 0, "") and not next_is_number:
            continue
        key.append(part)
        next_is_number = part[0] == 2
    end = (1, 0, "")  # sorts above pre-release tags, below any further number
    return tuple(reversed(key)) + (end,)


def version_status(installed, latest):
    """current / behind / ahead / unknown (no release recorded yet)."""
    if not latest:
        return "unknown"
    a, b = version_key(installed), version_key(latest)
    if a == b:
        return "current"
    return "behind" if a < b else "ahead"
