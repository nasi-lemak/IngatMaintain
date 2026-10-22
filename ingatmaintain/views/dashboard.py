from flask import Blueprint, current_app, render_template

from ..db import get_db, get_setting
from ..reminders import configured_channels, notify_recipients
from .items import STATUS_ORDER, load_items
from .jobs import load_jobs

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    conn = get_db()
    items = load_items(conn, "i.archived = 0")
    attention = sorted(
        (i for i in items if i["status"]["key"] in ("overdue", "urgent", "upcoming")),
        key=lambda i: (STATUS_ORDER[i["status"]["key"]], i["due_date"] or ""),
    )
    later = [i for i in items if i["status"]["key"] in ("ok", "none")]
    counts = {key: sum(1 for i in items if i["status"]["key"] == key) for key in STATUS_ORDER}
    open_jobs = load_jobs(conn, "j.status = 'open'")
    has_clients = conn.execute("SELECT 1 FROM clients LIMIT 1").fetchone() is not None
    channels = configured_channels(current_app.config, notify_recipients(conn))
    return render_template(
        "dashboard.html",
        attention=attention,
        later=later,
        counts=counts,
        open_jobs=open_jobs,
        has_clients=has_clients,
        channels=channels,
        last_run=get_setting(conn, "last_reminder_run"),
    )
