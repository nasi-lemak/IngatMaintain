"""Settings: users, notifications, calendar feed, own account."""

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..auth import admin_required
from ..db import calendar_token, create_user, get_db, get_setting, set_setting
from ..reminders import configured_channels, deliver, notify_recipients, run_reminders
from ..util import parse_date

bp = Blueprint("admin", __name__)


@bp.route("/settings")
@admin_required
def settings():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY active DESC, username").fetchall()
    recipients = notify_recipients(conn)
    cfg = current_app.config
    return render_template(
        "admin/settings.html",
        users=users,
        recipients=recipients,
        channels=configured_channels(cfg, recipients),
        smtp_host=cfg["SMTP_HOST"],
        telegram=bool(cfg["TELEGRAM_BOT_TOKEN"] and cfg["TELEGRAM_CHAT_ID"]),
        webhook=bool(cfg["WEBHOOK_URL"]),
        calendar_url=f"{cfg['BASE_URL']}{url_for('admin.calendar_feed', token=calendar_token(conn))}",
        last_run=get_setting(conn, "last_reminder_run"),
        reminder_hour=cfg["REMINDER_HOUR"],
        timezone=cfg["TIMEZONE"],
    )


@bp.route("/settings/test-notification", methods=["POST"])
@admin_required
def test_notification():
    conn = get_db()
    if not configured_channels(current_app.config, notify_recipients(conn)):
        flash("No notification channel is configured yet - see the setup notes below.", "error")
    else:
        ok, errors = deliver(
            current_app.config,
            conn,
            "IngatMaintain test notification",
            "If you can read this, maintenance reminders will reach you here.",
        )
        if ok:
            flash(f"Test sent via {', '.join(ok)}.", "success")
        for e in errors:
            flash(f"Failed - {e}", "error")
    return redirect(url_for("admin.settings"))


@bp.route("/settings/run-reminders", methods=["POST"])
@admin_required
def run_reminders_now():
    result = run_reminders(get_db(), current_app.config)
    flash(result["message"], "success" if result["sent"] else "info")
    return redirect(url_for("admin.settings"))


@bp.route("/settings/calendar-token", methods=["POST"])
@admin_required
def reset_calendar_token():
    set_setting(get_db(), "calendar_token", secrets.token_urlsafe(24))
    flash("New calendar link created - the old one no longer works.", "success")
    return redirect(url_for("admin.settings"))


@bp.route("/settings/seed-example", methods=["POST"])
@admin_required
def seed_example():
    from ..seed import seed_example as load

    flash(load(get_db()), "success")
    return redirect(url_for("dashboard.index"))


@bp.route("/settings/users", methods=["POST"])
@admin_required
def add_user():
    form = request.form
    username, password = form.get("username", "").strip(), form.get("password", "")
    if not username or len(password) < 8:
        flash("Username and a password of at least 8 characters are required.", "error")
    else:
        try:
            create_user(get_db(), username, password, form.get("full_name", ""), form.get("email", ""),
                        form.get("is_admin") == "1")
            flash(f"Added {username}.", "success")
        except sqlite3.IntegrityError:
            flash("That username is taken.", "error")
    return redirect(url_for("admin.settings") + "#users")


@bp.route("/settings/users/<int:user_id>", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    if request.method == "POST":
        form = request.form
        is_admin = 1 if form.get("is_admin") == "1" else 0
        active = 1 if form.get("active") == "1" else 0
        if user_id == g.user["id"] and (not is_admin or not active):
            flash("You can't remove your own admin rights or deactivate yourself.", "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        conn.execute(
            "UPDATE users SET full_name = ?, email = ?, is_admin = ?, notify = ?, active = ? WHERE id = ?",
            (form.get("full_name", "").strip(), form.get("email", "").strip(), is_admin,
             1 if form.get("notify") == "1" else 0, active, user_id),
        )
        if form.get("password"):
            if len(form["password"]) < 8:
                flash("Password must be at least 8 characters - not changed.", "error")
            else:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(form["password"]), user_id))
        conn.commit()
        flash("User saved.", "success")
        return redirect(url_for("admin.settings") + "#users")
    return render_template("admin/user_form.html", user=user)


@bp.route("/account", methods=["GET", "POST"])
def account():
    conn = get_db()
    if request.method == "POST":
        form = request.form
        conn.execute(
            "UPDATE users SET full_name = ?, email = ?, notify = ? WHERE id = ?",
            (form.get("full_name", "").strip(), form.get("email", "").strip(),
             1 if form.get("notify") == "1" else 0, g.user["id"]),
        )
        if form.get("new_password"):
            if not check_password_hash(g.user["password_hash"], form.get("current_password", "")):
                flash("Current password is wrong - password not changed.", "error")
            elif len(form["new_password"]) < 8:
                flash("New password must be at least 8 characters.", "error")
            else:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(form["new_password"]), g.user["id"]))
                flash("Password changed.", "success")
        conn.commit()
        flash("Account saved.", "success")
        return redirect(url_for("admin.account"))
    return render_template("admin/account.html")


def _ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line):
    # RFC 5545: lines longer than 75 octets are folded with CRLF + space.
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        b = ch.encode()
        if len(current) + len(b) > (75 if not parts else 74):
            parts.append(current.decode())
            current = b""
        current += b
    parts.append(current.decode())
    return "\r\n ".join(parts)


def _all_day_event(uid, day, summary, description, url, stamp):
    start = parse_date(day)
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
        f"DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"URL:{url}",
        "END:VEVENT",
    ]


@bp.route("/calendar/<token>.ics")
def calendar_feed(token):
    """Subscribe from Google / Outlook calendar. Secret link instead of a login."""
    conn = get_db()
    if not secrets.compare_digest(token, calendar_token(conn)):
        abort(404)
    base = current_app.config["BASE_URL"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//IngatMaintain//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:IngatMaintain",
    ]
    items = conn.execute(
        "SELECT i.*, c.name AS client_name FROM maintenance_items i JOIN clients c ON c.id = i.client_id "
        "WHERE i.archived = 0 AND i.due_date IS NOT NULL"
    ).fetchall()
    for i in items:
        lines += _all_day_event(
            f"item-{i['id']}-{i['due_date']}@ingatmaintain",
            i["due_date"],
            f"DUE: {i['client_name']} - {i['title']}",
            f"{i['category']}\n{i['notes']}".strip(),
            f"{base}/items/{i['id']}",
            stamp,
        )
    jobs = conn.execute(
        "SELECT j.*, c.name AS client_name FROM jobs j JOIN maintenance_items i ON i.id = j.item_id "
        "JOIN clients c ON c.id = i.client_id WHERE j.status = 'open' AND j.scheduled_date IS NOT NULL"
    ).fetchall()
    for j in jobs:
        lines += _all_day_event(
            f"job-{j['id']}@ingatmaintain",
            j["scheduled_date"],
            f"Site visit: {j['client_name']} - {j['title']}",
            f"Assigned: {j['assigned_to'] or 'unassigned'}",
            f"{base}/jobs/{j['id']}",
            stamp,
        )
    lines.append("END:VCALENDAR")
    body = "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": "inline; filename=ingatmaintain.ics"})
