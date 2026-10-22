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


def open_job_summary(conn, item_id, on_date):
    job = conn.execute(
        "SELECT * FROM jobs WHERE item_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (item_id,)
    ).fetchone()
    if job is None:
        return None
    from .views.jobs import job_progress

    done, total = job_progress(conn, job["id"])
    upcoming = conn.execute(
        "SELECT MIN(visit_date) FROM job_visits WHERE job_id = ? AND visit_date >= ?",
        (job["id"], on_date.isoformat()),
    ).fetchone()[0]
    return {"id": job["id"], "done": done, "total": total, "scheduled_date": upcoming}


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
        due.append({"item": item, "days_left": days_left, "bucket": bucket, "job": open_job_summary(conn, item["id"], on_date)})
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


def collect_visits(conn, on_date):
    """Site visits today or tomorrow whose reminder hasn't gone out for that date yet.

    Normally sent the day before; "today" catches a visit booked late or a missed run.
    """
    from .views.jobs import job_progress, load_visits

    days = (on_date.isoformat(), (on_date + timedelta(days=1)).isoformat())
    rows = conn.execute(
        """
        SELECT v.*, j.title AS job_title, i.title AS item_title,
               c.name AS client_name, c.site_address, c.access_notes, c.contact_name, c.contact_phone
        FROM job_visits v
        JOIN jobs j ON j.id = v.job_id
        JOIN maintenance_items i ON i.id = j.item_id
        JOIN clients c ON c.id = i.client_id
        WHERE j.status = 'open' AND v.visit_date IN (?, ?)
          AND (v.reminded_for IS NULL OR v.reminded_for != v.visit_date)
        ORDER BY v.visit_date, c.name
        """,
        days,
    ).fetchall()
    visits = []
    for row in rows:
        visit = dict(row)
        siblings = load_visits(conn, row["job_id"])
        visit["number"] = next(v["number"] for v in siblings if v["id"] == row["id"])
        visit["visit_count"] = len(siblings)
        visit["done"], visit["total"] = job_progress(conn, row["job_id"])
        unticked = conn.execute(
            """SELECT r.kind, r.name, r.quantity FROM job_requirements r
               LEFT JOIN job_checks k ON k.kind = 'req' AND k.ref_id = r.id
               WHERE r.job_id = ? AND k.id IS NULL ORDER BY r.position""",
            (row["job_id"],),
        ).fetchall()
        visit["to_pack"] = [r["name"] + (f" x{r['quantity']}" if r["quantity"] else "")
                            for r in unticked if r["kind"] == "equipment"]
        visit["software"] = [r["name"] for r in unticked if r["kind"] == "software"]
        visit["when"] = "TODAY" if row["visit_date"] == days[0] else "TOMORROW"
        visits.append(visit)
    return visits


def collect_upgrades(conn, on_date, every_days):
    """Sites behind on software: a first notice, then one every `every_days` (0 = first notice only)."""
    from .views.software import outdated

    upgrades = []
    for g in outdated(conn):
        key = (g["client_id"], g["software_id"], g["latest"])
        first = conn.execute(
            "SELECT MIN(sent_on) FROM upgrade_reminder_log WHERE client_id = ? AND software_id = ? AND target = ?", key
        ).fetchone()[0]
        if first is None:
            bucket = 0
        else:
            days = (on_date - parse_date(first)).days
            if not every_days or days < every_days:
                continue
            bucket = days // every_days
        if conn.execute(
            "SELECT 1 FROM upgrade_reminder_log WHERE client_id = ? AND software_id = ? AND target = ? AND bucket = ?",
            key + (bucket,),
        ).fetchone():
            continue
        planned = conn.execute(
            """SELECT id, title FROM maintenance_items
               WHERE client_id = ? AND archived = 0 AND title LIKE '%' || ? || '%' ORDER BY id DESC LIMIT 1""",
            (g["client_id"], g["software_name"]),
        ).fetchone()
        upgrades.append({**g, "bucket": bucket, "first": first, "planned": dict(planned) if planned else None})
    return upgrades


def format_upgrade(u, base_url):
    n = len(u["machines"])
    target = f"held at {u['latest']}" if u["hold"] else f"latest {u['latest']}"
    line = (f"[UPGRADE] {u['client_name']} - {u['software_name']}: {n} computer{'s' if n != 1 else ''} "
            f"on {', '.join(u['versions'])}, {target}")
    if u["first"]:
        line += f" (behind since {fmt_date(u['first'])})"
    if u["planned"]:
        line += f"\n    Planned: {u['planned']['title']} - {base_url}/items/{u['planned']['id']}"
    else:
        line += f"\n    No upgrade planned yet - {base_url}/clients/{u['client_id']}#versions"
    return line


def _day(iso):
    d = parse_date(iso)
    return f"{d.strftime('%a')} {fmt_date(d)}"


def format_visit(visit, base_url):
    which = ""
    if visit["visit_count"] > 1:
        which = f" (visit {visit['number']} of {visit['visit_count']}"
        which += f": {visit['purpose']})" if visit["purpose"] else ")"
    elif visit["purpose"]:
        which = f" ({visit['purpose']})"
    lines = [
        f"[VISIT {visit['when']}] {_day(visit['visit_date'])} - {visit['client_name']} - {visit['item_title']}{which}",
        f"    Team: {visit['assigned_to'] or 'nobody assigned yet!'}",
    ]
    if visit["site_address"]:
        lines.append(f"    Site: {visit['site_address']}")
    if visit["contact_name"] or visit["contact_phone"]:
        lines.append(f"    Contact: {visit['contact_name']} {visit['contact_phone']}".rstrip())
    if visit["access_notes"]:
        lines.append("    Access: " + " / ".join(visit["access_notes"].splitlines()))
    if visit["to_pack"]:
        lines.append("    Still to pack: " + ", ".join(visit["to_pack"]))
    if visit["software"]:
        lines.append("    Check laptop has: " + ", ".join(visit["software"]))
    lines.append(f"    Checklist {visit['done']}/{visit['total']} ticked - {base_url}/jobs/{visit['job_id']}")
    return "\n".join(lines)


def build_message(due, on_date, base_url, visits=(), upgrades=()):
    parts, sections, lines = [], [], []
    if visits:
        n = len(visits)
        parts.append(f"{n} site visit{'s' if n != 1 else ''} coming up")
        visit_lines = [format_visit(v, base_url) for v in visits]
        sections.append("SITE VISITS\n\n" + "\n\n".join(visit_lines))
        lines += visit_lines
    if due:
        overdue = sum(1 for d in due if d["days_left"] < 0)
        part = f"{len(due)} maintenance reminder{'s' if len(due) != 1 else ''}"
        if overdue:
            part += f" ({overdue} overdue)"
        parts.append(part)
        due_lines = [format_line(d, base_url) for d in due]
        sections.append("MAINTENANCE REMINDERS\n\n" + "\n\n".join(due_lines))
        lines += due_lines
    if upgrades:
        n = len(upgrades)
        parts.append(f"{n} site{'s' if n != 1 else ''} behind on software")
        upgrade_lines = [format_upgrade(u, base_url) for u in upgrades]
        sections.append("SOFTWARE UPGRADES\n\n" + "\n\n".join(upgrade_lines))
        lines += upgrade_lines
    subject = "IngatMaintain: " + ", ".join(parts)
    body = (
        f"IngatMaintain - {fmt_date(on_date)}\n\n"
        + "\n\n\n".join(sections)
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


def send_webhook(config, subject, body, due, visits=(), upgrades=()):
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
            "visits": [
                {
                    "job_id": v["job_id"],
                    "client": v["client_name"],
                    "visit_date": v["visit_date"],
                    "team": v["assigned_to"],
                }
                for v in visits
            ],
            "upgrades": [
                {
                    "client": u["client_name"],
                    "software": u["software_name"],
                    "running": u["versions"],
                    "target": u["latest"],
                    "computers": len(u["machines"]),
                }
                for u in upgrades
            ],
        },
    )


def deliver(config, conn, subject, body, due=(), visits=(), upgrades=()):
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
                send_webhook(config, subject, body, list(due), list(visits), list(upgrades))
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
    visits = collect_visits(conn, on_date)
    upgrades = collect_upgrades(conn, on_date, config.get("OUTDATED_REMINDER_DAYS", 30))
    if not due and not visits and not upgrades:
        return {"sent": 0, "message": "No reminders due today.", "lines": []}

    count = len(due) + len(visits) + len(upgrades)
    subject, body, lines = build_message(due, on_date, config["BASE_URL"], visits, upgrades)
    if dry_run:
        return {"sent": 0, "message": f"{count} reminder(s) would be sent (dry run).", "lines": lines}

    if not configured_channels(config, notify_recipients(conn)):
        return {
            "sent": 0,
            "message": f"{count} reminder(s) due but no notification channel is configured "
            "(set SMTP_* with user emails, TELEGRAM_* or WEBHOOK_URL). They are still shown on the dashboard.",
            "lines": lines,
        }

    succeeded, errors = deliver(config, conn, subject, body, due, visits, upgrades)
    if not succeeded:
        return {"sent": 0, "message": "Sending failed: " + "; ".join(errors), "lines": lines}

    sent_at = datetime.now(ZoneInfo(config["TIMEZONE"])).strftime("%Y-%m-%d %H:%M")
    conn.executemany(
        "INSERT OR IGNORE INTO reminder_log (item_id, due_date, threshold, sent_at, channels) VALUES (?, ?, ?, ?, ?)",
        [(d["item"]["id"], d["item"]["due_date"], d["bucket"], sent_at, ",".join(succeeded)) for d in due],
    )
    conn.executemany(
        "UPDATE job_visits SET reminded_for = visit_date WHERE id = ?", [(v["id"],) for v in visits]
    )
    conn.executemany(
        "INSERT OR IGNORE INTO upgrade_reminder_log (client_id, software_id, target, bucket, sent_on) "
        "VALUES (?, ?, ?, ?, ?)",
        [(u["client_id"], u["software_id"], u["latest"], u["bucket"], on_date.isoformat()) for u in upgrades],
    )
    conn.commit()
    message = f"Sent {count} reminder(s) via {', '.join(succeeded)}."
    if errors:
        message += " Some channels failed: " + "; ".join(errors)
    return {"sent": count, "message": message, "lines": lines}


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
