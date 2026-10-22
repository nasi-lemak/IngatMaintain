"""Jobs: a live checklist for one site visit (or series of visits)."""

import secrets
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from ..auth import display_name
from ..db import get_db
from ..util import add_months, fmt_date, now_str, parse_date, today
from .procedures import group_sections, load_parts

bp = Blueprint("jobs", __name__, url_prefix="/jobs")


def job_progress(conn, job_id):
    """(ticked, total) boxes for a job. Per-computer steps count once per computer."""
    machines = conn.execute("SELECT COUNT(*) FROM job_machines WHERE job_id = ?", (job_id,)).fetchone()[0]
    total = 0
    for step in conn.execute("SELECT per_machine FROM job_steps WHERE job_id = ?", (job_id,)):
        total += machines if (step["per_machine"] and machines) else 1
    total += conn.execute("SELECT COUNT(*) FROM job_requirements WHERE job_id = ?", (job_id,)).fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM job_checks WHERE job_id = ?", (job_id,)).fetchone()[0]
    return done, total


def load_jobs(conn, where="1 = 1", params=(), limit=None):
    rows = conn.execute(
        f"""
        SELECT j.*, i.title AS item_title, i.client_id, c.name AS client_name
        FROM jobs j
        JOIN maintenance_items i ON i.id = j.item_id
        JOIN clients c ON c.id = i.client_id
        WHERE {where}
        ORDER BY CASE j.status WHEN 'open' THEN 0 ELSE 1 END, j.id DESC
        {f'LIMIT {int(limit)}' if limit else ''}
        """,
        params,
    ).fetchall()
    on_date = today().isoformat()
    jobs = []
    for row in rows:
        job = dict(row)
        job["done"], job["total"] = job_progress(conn, row["id"])
        job["visits"] = load_visits(conn, row["id"])
        job["next_visit"] = next_visit(job["visits"], on_date)
        # Shorthands used by tables, calendar and reminders.
        job["scheduled_date"] = job["next_visit"]["visit_date"] if job["next_visit"] else None
        job["assigned_to"] = job["next_visit"]["assigned_to"] if job["next_visit"] else ""
        jobs.append(job)
    # Open jobs first, soonest visit first (stable sort keeps newest-first for ties).
    jobs.sort(key=lambda j: (j["status"] != "open", j["scheduled_date"] or "9999"))
    return jobs


def load_visits(conn, job_id):
    visits = [dict(v) for v in conn.execute(
        "SELECT * FROM job_visits WHERE job_id = ? ORDER BY position, id", (job_id,)
    )]
    for number, visit in enumerate(visits, 1):
        visit["number"] = number
    return visits


def next_visit(visits, on_date):
    """The visit to show for a job: the next upcoming one, else the latest one, else the first."""
    upcoming = sorted((v for v in visits if v["visit_date"] and v["visit_date"] >= on_date),
                      key=lambda v: v["visit_date"])
    if upcoming:
        return upcoming[0]
    past = sorted((v for v in visits if v["visit_date"]), key=lambda v: v["visit_date"])
    if past:
        return past[-1]
    return visits[0] if visits else None


def visit_label(visit, total):
    label = f"Visit {visit['number']}" if total > 1 else "Visit"
    return label + (f" ({visit['purpose']})" if visit.get("purpose") else "")


def get_job(job_id):
    jobs = load_jobs(get_db(), "j.id = ?", (job_id,))
    if not jobs:
        abort(404)
    return jobs[0]


def load_job_detail(conn, job):
    """Everything the checklist and print views need."""
    job_id = job["id"]
    machines = [dict(m) for m in conn.execute(
        "SELECT * FROM job_machines WHERE job_id = ? ORDER BY position", (job_id,)
    )]
    checks = {
        (c["kind"], c["ref_id"], c["machine_id"]): c
        for c in conn.execute("SELECT * FROM job_checks WHERE job_id = ?", (job_id,))
    }
    steps = [dict(s) for s in conn.execute("SELECT * FROM job_steps WHERE job_id = ? ORDER BY position", (job_id,))]
    for step in steps:
        targets = machines if (step["per_machine"] and machines) else [{"id": 0, "name": ""}]
        step["boxes"] = [
            {"machine_id": m["id"], "machine_name": m["name"], "check": checks.get(("step", step["id"], m["id"]))}
            for m in targets
        ]
        step["done"] = all(b["check"] for b in step["boxes"])
    reqs = []
    for r in conn.execute("SELECT * FROM job_requirements WHERE job_id = ? ORDER BY position", (job_id,)):
        req = dict(r)
        req["check"] = checks.get(("req", r["id"], 0))
        reqs.append(req)
    client = conn.execute(
        "SELECT c.* FROM clients c JOIN maintenance_items i ON i.client_id = c.id WHERE i.id = ?",
        (job["item_id"],),
    ).fetchone()
    item = conn.execute("SELECT * FROM maintenance_items WHERE id = ?", (job["item_id"],)).fetchone()
    notes = conn.execute("SELECT * FROM job_notes WHERE job_id = ? ORDER BY id DESC", (job_id,)).fetchall()
    return {
        "machines": machines,
        "groups": group_sections(steps),
        "software": [r for r in reqs if r["kind"] == "software"],
        "equipment": [r for r in reqs if r["kind"] == "equipment"],
        "client": client,
        "item": item,
        "notes": notes,
        "client_machines": {
            m["id"]: m for m in conn.execute("SELECT * FROM machines WHERE client_id = ?", (client["id"],))
        },
    }


def _users(conn):
    return conn.execute("SELECT id, username, full_name FROM users WHERE active = 1 ORDER BY full_name, username")


def _assigned(form):
    return ", ".join(n.strip() for n in form.getlist("assigned_to") if n.strip())


@bp.route("/")
def index():
    conn = get_db()
    show = request.args.get("show", "open")
    if show == "all":
        jobs = load_jobs(conn, limit=200)
    else:
        jobs = load_jobs(conn, "j.status = 'open'")
    return render_template("jobs/index.html", jobs=jobs, show=show)


@bp.route("/start/<int:item_id>", methods=["GET", "POST"])
def start(item_id):
    conn = get_db()
    item = conn.execute(
        "SELECT i.*, c.name AS client_name FROM maintenance_items i JOIN clients c ON c.id = i.client_id "
        "WHERE i.id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        abort(404)
    machines = conn.execute("SELECT * FROM machines WHERE client_id = ? ORDER BY name", (item["client_id"],)).fetchall()
    procedures = conn.execute("SELECT id, name FROM procedures ORDER BY name").fetchall()

    if request.method == "POST":
        procedure_id = request.form.get("procedure_id", type=int)
        proc = conn.execute("SELECT * FROM procedures WHERE id = ?", (procedure_id,)).fetchone()
        try:
            scheduled = parse_date(request.form.get("scheduled_date"))
        except ValueError:
            scheduled = None
            flash("Ignored an invalid visit date.", "error")
        if proc is None:
            flash("Choose a checklist for this job (create one under Checklists first).", "error")
        else:
            chosen = set(request.form.getlist("machine_ids", type=int))
            job_id = conn.execute(
                "INSERT INTO jobs (item_id, title, procedure_name, created_by, created_at, previous_due_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    request.form.get("title", "").strip() or f"{item['title']} - {item['client_name']}",
                    proc["name"],
                    display_name(),
                    now_str(),
                    item["due_date"],
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO job_visits (job_id, position, visit_date, assigned_to) VALUES (?, 0, ?, ?)",
                (job_id, scheduled.isoformat() if scheduled else None, _assigned(request.form)),
            )
            conn.executemany(
                "INSERT INTO job_machines (job_id, machine_id, position, name) VALUES (?, ?, ?, ?)",
                [(job_id, m["id"], i, m["name"]) for i, m in enumerate(m for m in machines if m["id"] in chosen)],
            )
            steps, software, equipment = load_parts(conn, proc["id"])
            conn.executemany(
                "INSERT INTO job_steps (job_id, position, section, title, details, per_machine) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(job_id, s["position"], s["section"], s["title"], s["details"], s["per_machine"]) for s in steps],
            )
            conn.executemany(
                "INSERT INTO job_requirements (job_id, position, kind, name, quantity, details) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(job_id, r["position"], r["kind"], r["name"], r["quantity"], r["details"]) for r in software + equipment],
            )
            conn.commit()
            flash("Job started. Tick items off as you go - progress saves instantly.", "success")
            return redirect(url_for("jobs.detail", job_id=job_id))

    return render_template(
        "jobs/start.html", item=item, machines=machines, procedures=procedures, users=_users(conn).fetchall()
    )


@bp.route("/<int:job_id>")
def detail(job_id):
    conn = get_db()
    job = get_job(job_id)
    from .software import version_grid  # software imports this module

    machines = [dict(m) for m in conn.execute(
        """SELECT m.* FROM job_machines jm JOIN machines m ON m.id = jm.machine_id
           WHERE jm.job_id = ? ORDER BY jm.position""",
        (job_id,),
    )]
    version_columns, version_rows = version_grid(conn, machines)
    return render_template("jobs/detail.html", job=job, users=_users(conn).fetchall(),
                           version_columns=version_columns, version_rows=version_rows,
                           **load_job_detail(conn, job))


@bp.route("/<int:job_id>/print")
def print_view(job_id):
    conn = get_db()
    job = get_job(job_id)
    return render_template("jobs/print.html", job=job, **load_job_detail(conn, job))


def handle_toggle(conn, job, who, back_url):
    """Tick / untick one box. Answers JSON for the page script, or redirects to back_url without JS.

    Shared by the logged-in job page and the share-link page.
    """
    job_id = job["id"]
    wants_json = request.headers.get("Accept", "").startswith("application/json")

    def fail(message, code=400):
        if wants_json:
            return jsonify({"error": message}), code
        flash(message, "error")
        return redirect(back_url)

    if job["status"] != "open":
        return fail("This job is closed - reopen it to change the checklist.", 409)
    kind = request.form.get("kind")
    ref_id = request.form.get("ref_id", type=int)
    machine_id = request.form.get("machine_id", default=0, type=int)

    machine_ids = {r["id"] for r in conn.execute("SELECT id FROM job_machines WHERE job_id = ?", (job_id,))}
    if kind == "step":
        ref = conn.execute("SELECT * FROM job_steps WHERE id = ? AND job_id = ?", (ref_id, job_id)).fetchone()
        valid = ref is not None and (
            machine_id in machine_ids if (ref["per_machine"] and machine_ids) else machine_id == 0
        )
    elif kind == "req":
        ref = conn.execute("SELECT * FROM job_requirements WHERE id = ? AND job_id = ?", (ref_id, job_id)).fetchone()
        valid = ref is not None and machine_id == 0
    else:
        valid = False
    if not valid:
        return fail("That checklist item does not belong to this job.")

    key = (kind, ref_id, machine_id)
    existing = conn.execute(
        "SELECT id FROM job_checks WHERE kind = ? AND ref_id = ? AND machine_id = ?", key
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM job_checks WHERE id = ?", (existing["id"],))
        checked, by, at = False, None, None
    else:
        by, at = who, now_str()
        conn.execute(
            "INSERT INTO job_checks (job_id, kind, ref_id, machine_id, checked_by, checked_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, *key, by, at),
        )
        checked = True
    conn.commit()

    if wants_json:
        done, total = job_progress(conn, job_id)
        return jsonify({"checked": checked, "by": by, "at": at, "done": done, "total": total})
    return redirect(f"{back_url}#{kind}-{ref_id}")


@bp.route("/<int:job_id>/toggle", methods=["POST"])
def toggle(job_id):
    job = get_job(job_id)
    return handle_toggle(get_db(), job, display_name(), url_for("jobs.detail", job_id=job_id))


def _log(conn, job_id, body):
    conn.execute(
        "INSERT INTO job_notes (job_id, author, created_at, body) VALUES (?, ?, ?, ?)",
        (job_id, display_name(), now_str(), body),
    )


def _visit_form(form):
    """(date or None, team, purpose) from a visit form; raises ValueError on a bad date."""
    visit_date = parse_date(form.get("visit_date"))
    return (visit_date.isoformat() if visit_date else None, _assigned(form),
            form.get("purpose", "").strip()[:80])


def _get_open_job_for_visits(job_id):
    job = get_job(job_id)
    if job["status"] != "open":
        flash("This job is closed - reopen it to change visits.", "error")
        return None
    return job


@bp.route("/<int:job_id>/visits", methods=["POST"])
def add_visit(job_id):
    """Book another site visit, e.g. when Synapxe can't return the certificates on the day."""
    conn = get_db()
    job = _get_open_job_for_visits(job_id)
    if job is None:
        return redirect(url_for("jobs.detail", job_id=job_id))
    try:
        visit_date, team, purpose = _visit_form(request.form)
    except ValueError:
        flash("Visit date must be a valid date.", "error")
        return redirect(url_for("jobs.detail", job_id=job_id) + "#visits")
    position = max((v["position"] for v in job["visits"]), default=-1) + 1
    conn.execute(
        "INSERT INTO job_visits (job_id, position, visit_date, assigned_to, purpose) VALUES (?, ?, ?, ?, ?)",
        (job_id, position, visit_date, team, purpose),
    )
    number = len(job["visits"]) + 1
    _log(conn, job_id, f"Visit {number} added" + (f" ({purpose})" if purpose else "")
         + (f" for {fmt_date(visit_date)}" if visit_date else ", date to be confirmed")
         + (f". Team: {team}" if team else "") + ".")
    conn.commit()
    flash(f"Visit {number} added.", "success")
    return redirect(url_for("jobs.detail", job_id=job_id) + "#visits")


@bp.route("/<int:job_id>/visits/<int:visit_id>", methods=["POST"])
def update_visit(job_id, visit_id):
    """Reschedule a visit or change who's going. Every change is written to the job log."""
    conn = get_db()
    job = _get_open_job_for_visits(job_id)
    if job is None:
        return redirect(url_for("jobs.detail", job_id=job_id))
    visit = next((v for v in job["visits"] if v["id"] == visit_id), None)
    if visit is None:
        abort(404)
    try:
        visit_date, team, purpose = _visit_form(request.form)
    except ValueError:
        flash("Visit date must be a valid date.", "error")
        return redirect(url_for("jobs.detail", job_id=job_id) + "#visits")

    label = visit_label(visit, len(job["visits"]))
    reason = request.form.get("reason", "").strip()
    changes = []
    if visit_date != visit["visit_date"]:
        if visit["visit_date"] and visit_date:
            changes.append(f"{label} moved from {fmt_date(visit['visit_date'])} to {fmt_date(visit_date)}")
        elif visit_date:
            changes.append(f"{label} booked for {fmt_date(visit_date)}")
        else:
            changes.append(f"{label} date cleared (was {fmt_date(visit['visit_date'])})")
    if team != visit["assigned_to"]:
        changes.append(f"{label} team: {team or 'nobody assigned'}")
    if purpose != visit["purpose"]:
        if not purpose:
            changes.append(f"{label} description removed")
        else:
            changes.append(f"{label} {'renamed' if visit['purpose'] else 'described'} as '{purpose}'")
    if not changes:
        flash("Nothing changed.", "info")
        return redirect(url_for("jobs.detail", job_id=job_id) + "#visits")

    conn.execute(
        "UPDATE job_visits SET visit_date = ?, assigned_to = ?, purpose = ? WHERE id = ?",
        (visit_date, team, purpose, visit_id),
    )
    _log(conn, job_id, ". ".join(changes) + "." + (f"\nReason: {reason}" if reason else ""))
    conn.commit()
    flash(changes[0] + "." + (" The calendar and visit reminder follow the new date." if visit_date != visit["visit_date"] else ""),
          "success")
    return redirect(url_for("jobs.detail", job_id=job_id) + "#visits")


@bp.route("/<int:job_id>/visits/<int:visit_id>/delete", methods=["POST"])
def delete_visit(job_id, visit_id):
    conn = get_db()
    job = _get_open_job_for_visits(job_id)
    if job is None:
        return redirect(url_for("jobs.detail", job_id=job_id))
    visit = next((v for v in job["visits"] if v["id"] == visit_id), None)
    if visit is None:
        abort(404)
    if len(job["visits"]) == 1:
        flash("A job always has at least one visit - change its date instead.", "error")
        return redirect(url_for("jobs.detail", job_id=job_id) + "#visits")
    conn.execute("DELETE FROM job_visits WHERE id = ?", (visit_id,))
    reason = request.form.get("reason", "").strip()
    _log(conn, job_id, f"{visit_label(visit, len(job['visits']))} removed"
         + (f" (was {fmt_date(visit['visit_date'])})" if visit["visit_date"] else "") + "."
         + (f"\nReason: {reason}" if reason else ""))
    conn.commit()
    flash("Visit removed.", "success")
    return redirect(url_for("jobs.detail", job_id=job_id) + "#visits")


@bp.route("/<int:job_id>/notes", methods=["POST"])
def add_note(job_id):
    conn = get_db()
    get_job(job_id)
    body = request.form.get("body", "").strip()
    if body:
        conn.execute(
            "INSERT INTO job_notes (job_id, author, created_at, body) VALUES (?, ?, ?, ?)",
            (job_id, display_name(), now_str(), body),
        )
        conn.commit()
    return redirect(url_for("jobs.detail", job_id=job_id) + "#notes")


@bp.route("/<int:job_id>/complete", methods=["GET", "POST"])
def complete(job_id):
    conn = get_db()
    job = get_job(job_id)
    if job["status"] != "open":
        flash("This job is already closed.", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    item = conn.execute("SELECT * FROM maintenance_items WHERE id = ?", (job["item_id"],)).fetchone()

    if request.method == "POST":
        retire = request.form.get("retire") == "1"
        try:
            next_due = parse_date(request.form.get("next_due_date"))
        except ValueError:
            next_due = None
        if not retire and next_due is None:
            flash("Enter the new expiry / next due date so we can remind you next time.", "error")
        elif not retire and next_due <= today():
            flash("The next due date should be in the future.", "error")
        elif job["done"] < job["total"] and request.form.get("force") != "1":
            flash(
                f"Only {job['done']} of {job['total']} boxes are ticked. "
                "Tick 'Complete anyway' if the rest really are not needed.",
                "error",
            )
        else:
            new_due = None if retire else next_due.isoformat()
            conn.execute(
                "UPDATE jobs SET status = 'completed', share_token = NULL, completed_at = ?, completed_by = ?, "
                "previous_due_date = ?, new_due_date = ? WHERE id = ?",
                (now_str(), display_name(), item["due_date"], new_due, job_id),
            )
            if retire:
                conn.execute("UPDATE maintenance_items SET archived = 1 WHERE id = ?", (item["id"],))
            else:
                conn.execute(
                    "UPDATE maintenance_items SET due_date = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_due, item["id"]),
                )
            summary = request.form.get("summary", "").strip()
            note = "Job completed." + (f" Next due {new_due}." if new_due else " Item archived.")
            conn.execute(
                "INSERT INTO job_notes (job_id, author, created_at, body) VALUES (?, ?, ?, ?)",
                (job_id, display_name(), now_str(), note + (f"\n{summary}" if summary else "")),
            )
            conn.commit()
            flash("Job completed. Reminders now count down to " + (new_due or "nothing (item archived)") + ".",
                  "success")
            return redirect(url_for("items.detail", item_id=item["id"]))

    suggested = None
    if item["recurrence_months"]:
        suggested = add_months(today(), item["recurrence_months"]).isoformat()
    return render_template("jobs/complete.html", job=job, item=item, suggested=suggested)


@bp.route("/<int:job_id>/status", methods=["POST"])
def set_status(job_id):
    """Cancel or reopen a job."""
    conn = get_db()
    job = get_job(job_id)
    action = request.form.get("action")
    if action == "cancel" and job["status"] == "open":
        conn.execute("UPDATE jobs SET status = 'cancelled', share_token = NULL WHERE id = ?", (job_id,))
        message = "Job cancelled."
    elif action == "reopen" and job["status"] != "open":
        # Reopening a completed job does not roll the item's due date back - edit the item if needed.
        conn.execute(
            "UPDATE jobs SET status = 'open', completed_at = NULL, completed_by = NULL WHERE id = ?", (job_id,)
        )
        message = "Job reopened."
    else:
        abort(400)
    conn.execute(
        "INSERT INTO job_notes (job_id, author, created_at, body) VALUES (?, ?, ?, ?)",
        (job_id, display_name(), now_str(), message),
    )
    conn.commit()
    flash(message, "success")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/<int:job_id>/share", methods=["POST"])
def share(job_id):
    """Create (or renew) and revoke the job's share link."""
    conn = get_db()
    job = get_job(job_id)
    if request.form.get("action") == "revoke":
        conn.execute("UPDATE jobs SET share_token = NULL, share_expires_at = NULL WHERE id = ?", (job_id,))
        log = message = "Share link turned off."
    elif job["status"] == "open":
        expires = today() + timedelta(days=current_app.config["SHARE_LINK_DAYS"])
        conn.execute(
            "UPDATE jobs SET share_token = ?, share_expires_at = ? WHERE id = ?",
            (secrets.token_urlsafe(24), expires.isoformat(), job_id),
        )
        log = f"Share link created, valid until {fmt_date(expires)}."
        message = log + " It also stops working when the job is closed. Any older link no longer works."
    else:
        abort(400)
    conn.execute(
        "INSERT INTO job_notes (job_id, author, created_at, body) VALUES (?, ?, ?, ?)",
        (job_id, display_name(), now_str(), log),
    )
    conn.commit()
    flash(message, "success")
    return redirect(url_for("jobs.detail", job_id=job_id) + "#share")
