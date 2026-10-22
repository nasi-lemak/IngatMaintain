from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..auth import admin_required
from ..db import get_db
from ..util import (
    CATEGORIES,
    DEFAULT_REMINDER_DAYS,
    item_status,
    normalise_reminder_days,
    parse_date,
    today,
)
from .jobs import load_jobs

bp = Blueprint("items", __name__, url_prefix="/items")

STATUS_ORDER = {"overdue": 0, "urgent": 1, "upcoming": 2, "ok": 3, "none": 4}


def load_items(conn, where="1 = 1", params=()):
    """Maintenance items with client/procedure names, open job and computed status."""
    rows = conn.execute(
        f"""
        SELECT i.*, c.name AS client_name, p.name AS procedure_name,
               (SELECT j.id FROM jobs j WHERE j.item_id = i.id AND j.status = 'open'
                ORDER BY j.id DESC LIMIT 1) AS open_job_id
        FROM maintenance_items i
        JOIN clients c ON c.id = i.client_id
        LEFT JOIN procedures p ON p.id = i.procedure_id
        WHERE {where}
        """,
        params,
    ).fetchall()
    on_date = today()
    items = []
    for row in rows:
        item = dict(row)
        item["status"] = item_status(row["due_date"], row["reminder_days"], on_date)
        items.append(item)
    items.sort(key=lambda i: (i["due_date"] is None, i["due_date"] or "", i["client_name"].lower()))
    return items


def get_item(item_id):
    items = load_items(get_db(), "i.id = ?", (item_id,))
    if not items:
        abort(404)
    return items[0]


def _form_data(form):
    errors = []
    data = {
        "client_id": form.get("client_id", type=int),
        "procedure_id": form.get("procedure_id", type=int) or None,
        "title": form.get("title", "").strip(),
        "category": form.get("category", "").strip(),
        "notes": form.get("notes", "").strip(),
        "recurrence_months": form.get("recurrence_months", type=int) or None,
    }
    if not data["client_id"]:
        errors.append("Choose a client.")
    if not data["title"]:
        errors.append("Give the item a name, e.g. 'SSL certificate (MQTT)'.")
    try:
        due = parse_date(form.get("due_date"))
        data["due_date"] = due.isoformat() if due else None
    except ValueError:
        errors.append("Due date must be a valid date.")
        data["due_date"] = form.get("due_date")
    try:
        data["reminder_days"] = normalise_reminder_days(form.get("reminder_days") or DEFAULT_REMINDER_DAYS)
    except ValueError as exc:
        errors.append(str(exc))
        data["reminder_days"] = form.get("reminder_days", "")
    return data, errors


def _form_context(conn):
    return {
        "clients": conn.execute("SELECT id, name FROM clients ORDER BY name").fetchall(),
        "procedures": conn.execute("SELECT id, name FROM procedures ORDER BY name").fetchall(),
        "categories": CATEGORIES,
    }


@bp.route("/")
def index():
    conn = get_db()
    show = request.args.get("show", "active")
    client_id = request.args.get("client", type=int)
    where, params = ["i.archived = ?"], [1 if show == "archived" else 0]
    if client_id:
        where.append("i.client_id = ?")
        params.append(client_id)
    items = load_items(conn, " AND ".join(where), params)
    status = request.args.get("status")
    if status:
        items = [i for i in items if i["status"]["key"] == status]
    clients = conn.execute("SELECT id, name FROM clients ORDER BY name").fetchall()
    return render_template(
        "items/index.html", items=items, clients=clients, show=show, client_id=client_id, status=status
    )


@bp.route("/new", methods=["GET", "POST"])
def new():
    conn = get_db()
    if request.method == "POST":
        data, errors = _form_data(request.form)
        if not errors:
            cur = conn.execute(
                """INSERT INTO maintenance_items
                   (client_id, procedure_id, title, category, due_date, reminder_days, recurrence_months, notes)
                   VALUES (:client_id, :procedure_id, :title, :category, :due_date, :reminder_days,
                           :recurrence_months, :notes)""",
                data,
            )
            conn.commit()
            flash("Maintenance item added.", "success")
            return redirect(url_for("items.detail", item_id=cur.lastrowid))
        for e in errors:
            flash(e, "error")
        item = data
    else:
        item = {
            "client_id": request.args.get("client", type=int),
            "title": request.args.get("title", ""),
            "category": request.args.get("category", ""),
            "reminder_days": DEFAULT_REMINDER_DAYS,
            "procedure_id": request.args.get("procedure", type=int),
        }
    return render_template("items/form.html", item=item, **_form_context(conn))


@bp.route("/<int:item_id>")
def detail(item_id):
    conn = get_db()
    item = get_item(item_id)
    jobs = load_jobs(conn, "j.item_id = ?", (item_id,))
    reminders = conn.execute(
        "SELECT * FROM reminder_log WHERE item_id = ? ORDER BY sent_at DESC LIMIT 20", (item_id,)
    ).fetchall()
    return render_template("items/detail.html", item=item, jobs=jobs, reminders=reminders)


@bp.route("/<int:item_id>/edit", methods=["GET", "POST"])
def edit(item_id):
    conn = get_db()
    item = get_item(item_id)
    if request.method == "POST":
        data, errors = _form_data(request.form)
        if not errors:
            data["id"] = item_id
            conn.execute(
                """UPDATE maintenance_items SET client_id = :client_id, procedure_id = :procedure_id,
                   title = :title, category = :category, due_date = :due_date, reminder_days = :reminder_days,
                   recurrence_months = :recurrence_months, notes = :notes, updated_at = CURRENT_TIMESTAMP
                   WHERE id = :id""",
                data,
            )
            conn.commit()
            flash("Saved.", "success")
            return redirect(url_for("items.detail", item_id=item_id))
        for e in errors:
            flash(e, "error")
        item = {**item, **data}
    return render_template("items/form.html", item=item, **_form_context(conn))


@bp.route("/<int:item_id>/archive", methods=["POST"])
def archive(item_id):
    conn = get_db()
    get_item(item_id)
    archived = 1 if request.form.get("archived") == "1" else 0
    conn.execute("UPDATE maintenance_items SET archived = ? WHERE id = ?", (archived, item_id))
    conn.commit()
    flash("Item archived - no more reminders." if archived else "Item restored.", "success")
    return redirect(url_for("items.detail", item_id=item_id))


@bp.route("/<int:item_id>/delete", methods=["POST"])
@admin_required
def delete(item_id):
    conn = get_db()
    item = get_item(item_id)
    conn.execute("DELETE FROM maintenance_items WHERE id = ?", (item_id,))
    conn.commit()
    flash(f"Deleted '{item['title']}' and its job history.", "success")
    return redirect(url_for("clients.detail", client_id=item["client_id"]))
