"""Reminder engine: works out what is due and sends email / Telegram / webhook digests.

Safe to run as often as you like - every (item, due date, threshold) is only
sent once, and nothing is marked as sent unless at least one channel worked.
"""

import json
import logging
import smtplib
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from .db import connect, set_setting
from .util import describe_days, fmt_date, parse_date, parse_reminder_days, reminder_bucket

log = logging.getLogger(__name__)


def _today(config):
    return datetime.now(ZoneInfo(config["TIMEZONE"])).date()


def open_job_summary(conn, item_id):
    job = conn.execute(
        "SELECT * FROM jobs WHERE item_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (item_id,)
    ).fetchone()
    if job is None:
        return None
    from .views.jobs import job_progress

    done, total = job_progress(conn, job["id"])
    return {"id": job["id"], "done": done, "total": total, "scheduled_date": job["scheduled_date"]}


def collect_due(conn, on_date):
    """Items whose next reminder has not been sent yet."""
    rows = conn.execute(
        """
        SELECT i.*, c.name AS client_name, p.name AS procedure_name
        FROM maintenance_items i
        JOIN clients c ON c.id = i.client_id
        LEFT JOIN procedures p ON p.id = i.procedure_id
        WHERE i.archived = 0 AND i.due_date IS NOT NULL AND i.due_date != ''
        ORDER BY i.due_date
        """
    ).fetchall()
    due = []
    for item in rows:
        try:
            days_left = (parse_date(item["due_date"]) - on_date).days
        except ValueError:
            continue
        bucket = reminder_bucket(days_left, parse_reminder_days(item["reminder_days"]))
        if bucket is None:
            continue
        already = conn.execute(
            "SELECT 1 FROM reminder_log WHERE item_id = ? AND due_date = ? AND threshold = ?",
            (item["id"], item["due_date"], bucket),
        ).fetchone()
        if already:
            continue
        due.append({"item": item, "days_left": days_left, "bucket": bucket, "job": open_job_summary(conn, item["id"])})
    return due


def format_line(entry, base_url):
    item = entry["item"]
    tag = "OVERDUE" if entry["days_left"] < 0 else ("DUE" if entry["days_left"] <= 30 else "UPCOMING")
    line = (
        f"[{tag}] {item['client_name']} - {item['title']}: due {fmt_date(item['due_date'])} "
        f"({describe_days(entry['days_left'])})"
    )
    job = entry["job"]
    if job:
        when = f", visit {fmt_date(job['scheduled_date'])}" if job["scheduled_date"] else ""
        line += f"\n    Job #{job['id']} in progress: {job['done']}/{job['total']} ticked{when} - {base_url}/jobs/{job['id']}"
    else:
        line += "\n    No job started yet"
        if item["procedure_name"]:
            line += f" (checklist: {item['procedure_name']})"
        line += f" - {base_url}/items/{item['id']}"
    return line


def build_message(due, on_date, base_url):
    overdue = sum(1 for d in due if d["days_left"] < 0)
    subject = f"IngatMaintain: {len(due)} maintenance reminder{'s' if len(due) != 1 else ''}"
    if overdue:
        subject += f" ({overdue} overdue)"
    lines = [format_line(d, base_url) for d in due]
    body = (
        f"Maintenance reminders for {fmt_date(on_date)}\n\n"
        + "\n\n".join(lines)
        + f"\n\nDashboard: {base_url}/\n"
    )
    return subject, body, lines


def notify_recipients(conn):
    rows = conn.execute(
        "SELECT email FROM users WHERE active = 1 AND notify = 1 AND email != '' ORDER BY id"
    ).fetchall()
    return [r["email"] for r in rows]


def configured_channels(config, recipients):
    channels = []
    if config.get("SMTP_HOST") and recipients:
        channels.append("email")
    if config.get("TELEGRAM_BOT_TOKEN") and config.get("TELEGRAM_CHAT_ID"):
        channels.append("telegram")
    if config.get("WEBHOOK_URL"):
        channels.append("webhook")
    return channels


def send_email(config, recipients, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.get("SMTP_FROM") or config.get("SMTP_USER") or "ingatmaintain@localhost"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    smtp_cls = smtplib.SMTP_SSL if config.get("SMTP_SSL") else smtplib.SMTP
    with smtp_cls(config["SMTP_HOST"], config["SMTP_PORT"], timeout=30) as smtp:
        if config.get("SMTP_STARTTLS") and not config.get("SMTP_SSL"):
            smtp.starttls()
        if config.get("SMTP_USER"):
            smtp.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        smtp.send_message(msg)


def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def send_telegram(config, text):
    url = f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage"
    # Telegram caps messages at 4096 characters.
    _post_json(url, {"chat_id": config["TELEGRAM_CHAT_ID"], "text": text[:4000], "disable_web_page_preview": True})


def send_webhook(config, subject, body, due):
    # "text" works for Slack, Google Chat and Teams incoming webhooks.
    _post_json(
        config["WEBHOOK_URL"],
        {
            "text": f"*{subject}*\n{body}",
            "items": [
                {
                    "item_id": d["item"]["id"],
                    "client": d["item"]["client_name"],
                    "title": d["item"]["title"],
                    "due_date": d["item"]["due_date"],
                    "days_left": d["days_left"],
                }
                for d in due
            ],
        },
    )


def deliver(config, conn, subject, body, due=()):
    """Send on every configured channel. Returns (succeeded, errors)."""
    recipients = notify_recipients(conn)
    succeeded, errors = [], []
    for channel in configured_channels(config, recipients):
        try:
            if channel == "email":
                send_email(config, recipients, subject, body)
            elif channel == "telegram":
                send_telegram(config, f"{subject}\n\n{body}")
            elif channel == "webhook":
                send_webhook(config, subject, body, list(due))
            succeeded.append(channel)
        except Exception as exc:  # report and carry on with the other channels
            log.exception("Reminder channel %s failed", channel)
            errors.append(f"{channel}: {exc}")
    return succeeded, errors


def run_reminders(conn, config, on_date=None, dry_run=False):
    result = _run_reminders(conn, config, on_date, dry_run)
    if not dry_run:
        stamp = datetime.now(ZoneInfo(config["TIMEZONE"])).strftime("%Y-%m-%d %H:%M")
        set_setting(conn, "last_reminder_run", f"{stamp} - {result['message']}")
    return result


def _run_reminders(conn, config, on_date, dry_run):
    on_date = on_date or _today(config)
    due = collect_due(conn, on_date)
    if not due:
        return {"sent": 0, "message": "No reminders due today.", "lines": []}

    subject, body, lines = build_message(due, on_date, config["BASE_URL"])
    if dry_run:
        return {"sent": 0, "message": f"{len(due)} reminder(s) would be sent (dry run).", "lines": lines}

    if not configured_channels(config, notify_recipients(conn)):
        return {
            "sent": 0,
            "message": f"{len(due)} reminder(s) due but no notification channel is configured "
            "(set SMTP_* with user emails, TELEGRAM_* or WEBHOOK_URL). They are still shown on the dashboard.",
            "lines": lines,
        }

    succeeded, errors = deliver(config, conn, subject, body, due)
    if not succeeded:
        return {"sent": 0, "message": "Sending failed: " + "; ".join(errors), "lines": lines}

    sent_at = datetime.now(ZoneInfo(config["TIMEZONE"])).strftime("%Y-%m-%d %H:%M")
    conn.executemany(
        "INSERT OR IGNORE INTO reminder_log (item_id, due_date, threshold, sent_at, channels) VALUES (?, ?, ?, ?, ?)",
        [(d["item"]["id"], d["item"]["due_date"], d["bucket"], sent_at, ",".join(succeeded)) for d in due],
    )
    conn.commit()
    message = f"Sent {len(due)} reminder(s) via {', '.join(succeeded)}."
    if errors:
        message += " Some channels failed: " + "; ".join(errors)
    return {"sent": len(due), "message": message, "lines": lines}


def start_scheduler(app):
    """Background thread: check once at start-up, then daily at REMINDER_HOUR."""

    def seconds_until_next_run():
        tz = ZoneInfo(app.config["TIMEZONE"])
        now = datetime.now(tz)
        run_at = now.replace(hour=app.config["REMINDER_HOUR"], minute=0, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        return (run_at - now).total_seconds()

    def run_once():
        conn = connect(app.config["DATABASE"])
        try:
            with app.app_context():
                result = run_reminders(conn, app.config)
            log.info("Reminder check: %s", result["message"])
        except Exception:
            log.exception("Reminder check failed")
        finally:
            conn.close()

    def loop():
        run_once()
        while True:
            time.sleep(seconds_until_next_run())
            run_once()

    thread = threading.Thread(target=loop, name="reminder-scheduler", daemon=True)
    thread.start()
    return thread
