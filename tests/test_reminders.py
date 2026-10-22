from datetime import date

from ingatmaintain import reminders
from ingatmaintain.db import create_user
from ingatmaintain.seed import seed_example


def _setup(conn):
    seed_example(conn)
    create_user(conn, "tech", "password123", "Tech One", "tech@example.com")


def test_no_reminder_while_far_away(app, conn):
    _setup(conn)
    with app.app_context():
        result = reminders.run_reminders(conn, app.config, on_date=date(2026, 10, 22))
    assert result["sent"] == 0
    assert "No reminders" in result["message"]


def test_reminders_sent_once_per_threshold(app, conn, monkeypatch):
    _setup(conn)
    app.config["SMTP_HOST"] = "smtp.example.com"
    sent = []
    monkeypatch.setattr(reminders, "send_email", lambda cfg, to, subject, body: sent.append((to, subject, body)))

    with app.app_context():
        # 21 Oct 2028 minus 180 days = 24 Apr 2028: first reminder.
        r1 = reminders.run_reminders(conn, app.config, on_date=date(2028, 4, 24))
        r2 = reminders.run_reminders(conn, app.config, on_date=date(2028, 4, 25))
        r3 = reminders.run_reminders(conn, app.config, on_date=date(2028, 6, 23))  # 120 days out
    assert (r1["sent"], r2["sent"], r3["sent"]) == (1, 0, 1)
    to, subject, body = sent[0]
    assert to == ["tech@example.com"]
    assert "NHG Khatib Polyclinic" in body and "21 Oct 2028" in body
    assert "No job started yet" in body
    thresholds = [r["threshold"] for r in conn.execute("SELECT threshold FROM reminder_log ORDER BY id")]
    assert thresholds == [180, 120]


def test_overdue_reminds_weekly(app, conn, monkeypatch):
    _setup(conn)
    app.config["WEBHOOK_URL"] = "https://hooks.example.com/x"
    calls = []
    monkeypatch.setattr(reminders, "send_webhook", lambda cfg, s, b, due, visits=(): calls.append(due))
    with app.app_context():
        for day in (22, 23, 28, 29):
            reminders.run_reminders(conn, app.config, on_date=date(2028, 10, day))
    # 22 Oct = 1 day overdue (week 1), 29 Oct = 8 days overdue (week 2)
    assert len(calls) == 2


def test_failed_send_is_retried(app, conn, monkeypatch):
    _setup(conn)
    app.config["WEBHOOK_URL"] = "https://hooks.example.com/x"

    def boom(*a):
        raise OSError("network down")

    monkeypatch.setattr(reminders, "send_webhook", boom)
    with app.app_context():
        r = reminders.run_reminders(conn, app.config, on_date=date(2028, 10, 1))
    assert r["sent"] == 0 and "network down" in r["message"]
    assert conn.execute("SELECT COUNT(*) FROM reminder_log").fetchone()[0] == 0


def test_no_channel_does_not_mark_sent(app, conn):
    _setup(conn)
    with app.app_context():
        r = reminders.run_reminders(conn, app.config, on_date=date(2028, 10, 1))
    assert r["sent"] == 0 and "no notification channel" in r["message"]
    assert conn.execute("SELECT COUNT(*) FROM reminder_log").fetchone()[0] == 0


def test_dry_run_lists_without_sending(app, conn):
    _setup(conn)
    with app.app_context():
        r = reminders.run_reminders(conn, app.config, on_date=date(2028, 10, 1), dry_run=True)
    assert len(r["lines"]) == 1 and "DUE" in r["lines"][0]
