"""Share links: tick one job's checklist without an account.

A link is a long random token tied to one open job. It only shows that job's
checklist and packing list (no other clients, no hostnames / IPs), and stops
working when the job is completed or cancelled, the link is revoked, or it
passes its expiry date.
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from ..auth import display_name
from ..db import get_db
from ..util import now_str, today
from .jobs import get_job, handle_toggle, load_job_detail

bp = Blueprint("shared", __name__, url_prefix="/s")


def get_shared_job(token):
    row = get_db().execute(
        "SELECT id, share_expires_at FROM jobs WHERE share_token = ? AND status = 'open'", (token,)
    ).fetchone()
    if row is None or (row["share_expires_at"] and row["share_expires_at"] < today().isoformat()):
        abort(404)
    return get_job(row["id"])


def visitor_name():
    """Who ticks are stamped with: the account name, or the name a guest typed in."""
    if g.user is not None:
        return display_name()
    name = session.get("guest_name")
    return f"{name} (shared link)" if name else None


@bp.route("/<token>")
def view(token):
    conn = get_db()
    job = get_shared_job(token)
    return render_template(
        "shared/job.html", job=job, token=token, visitor=visitor_name(), **load_job_detail(conn, job)
    )


@bp.route("/<token>/name", methods=["POST"])
def set_name(token):
    get_shared_job(token)
    name = " ".join(request.form.get("name", "").split())[:60]
    if name:
        session.permanent = True
        session["guest_name"] = name
    else:
        flash("Please enter your name so the team knows who ticked what.", "error")
    return redirect(url_for("shared.view", token=token))


@bp.route("/<token>/toggle", methods=["POST"])
def toggle(token):
    job = get_shared_job(token)
    who = visitor_name()
    if who is None:
        abort(400, "Enter your name first.")
    return handle_toggle(get_db(), job, who, url_for("shared.view", token=token))


@bp.route("/<token>/notes", methods=["POST"])
def add_note(token):
    conn = get_db()
    job = get_shared_job(token)
    who = visitor_name()
    body = request.form.get("body", "").strip()
    if who and body:
        conn.execute(
            "INSERT INTO job_notes (job_id, author, created_at, body) VALUES (?, ?, ?, ?)",
            (job["id"], who, now_str(), body),
        )
        conn.commit()
    return redirect(url_for("shared.view", token=token) + "#notes")


@bp.errorhandler(404)
def link_not_found(_error):
    return render_template("shared/expired.html"), 404
