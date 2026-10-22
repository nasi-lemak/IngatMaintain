"""Software versions: which version of each product is on each client computer."""

import sqlite3
from urllib.parse import urlencode

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..auth import admin_required, display_name
from ..db import get_db
from ..util import now_str, parse_date, today, version_key, version_status
from .jobs import get_job

bp = Blueprint("software", __name__, url_prefix="/versions")


# ---------------------------------------------------------------- queries

def releases_for(conn, software_id):
    """Releases, newest version first."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM software_releases WHERE software_id = ?", (software_id,))]
    return sorted(rows, key=lambda r: version_key(r["version"]), reverse=True)


def latest_versions(conn):
    """{software_id: latest version string}"""
    latest = {}
    for r in conn.execute("SELECT software_id, version FROM software_releases"):
        current = latest.get(r["software_id"])
        if current is None or version_key(r["version"]) > version_key(current):
            latest[r["software_id"]] = r["version"]
    return latest


def load_software(conn):
    latest = latest_versions(conn)
    items = []
    for row in conn.execute("SELECT * FROM software ORDER BY name"):
        sw = dict(row)
        sw["latest"] = latest.get(row["id"], "")
        statuses = [version_status(i["version"], sw["latest"]) for i in conn.execute(
            "SELECT version FROM installations WHERE software_id = ?", (row["id"],))]
        sw["installs"] = len(statuses)
        sw["behind"] = statuses.count("behind")
        sw["clients_behind"] = len(outdated(conn, software_id=row["id"], latest=latest))
        items.append(sw)
    return items


def get_software(software_id):
    sw = get_db().execute("SELECT * FROM software WHERE id = ?", (software_id,)).fetchone()
    if sw is None:
        abort(404)
    return sw


def outdated(conn, software_id=None, client_id=None, latest=None):
    """[(client, software, machines behind, versions in use)] for sites not on the latest version."""
    latest = latest if latest is not None else latest_versions(conn)
    where, params = ["1 = 1"], []
    if software_id:
        where.append("s.id = ?")
        params.append(software_id)
    if client_id:
        where.append("c.id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT c.id AS client_id, c.name AS client_name, s.id AS software_id, s.name AS software_name,
                   m.name AS machine_name, i.version
            FROM installations i
            JOIN machines m ON m.id = i.machine_id
            JOIN clients c ON c.id = m.client_id
            JOIN software s ON s.id = i.software_id
            WHERE {' AND '.join(where)}
            ORDER BY c.name, s.name, m.name""",
        params,
    ).fetchall()
    groups = {}
    for r in rows:
        target = latest.get(r["software_id"], "")
        if version_status(r["version"], target) != "behind":
            continue
        key = (r["client_id"], r["software_id"])
        g = groups.setdefault(key, {
            "client_id": r["client_id"], "client_name": r["client_name"],
            "software_id": r["software_id"], "software_name": r["software_name"],
            "latest": target, "machines": [], "versions": set(),
        })
        g["machines"].append(r["machine_name"])
        g["versions"].add(r["version"])
    result = list(groups.values())
    for g in result:
        g["versions"] = sorted(g["versions"], key=version_key)
    return result


def version_grid(conn, machines, extra_software_ids=()):
    """Columns (software present on these machines, plus any extra) and cells for a machines x software table."""
    machine_ids = [m["id"] for m in machines]
    latest = latest_versions(conn)
    installs = {}
    if machine_ids:
        marks = ",".join("?" * len(machine_ids))
        for r in conn.execute(f"SELECT * FROM installations WHERE machine_id IN ({marks})", machine_ids):
            installs[(r["machine_id"], r["software_id"])] = dict(r)
    software_ids = {sid for (_m, sid) in installs} | {int(s) for s in extra_software_ids}
    columns = []
    if software_ids:
        marks = ",".join("?" * len(software_ids))
        for s in conn.execute(f"SELECT * FROM software WHERE id IN ({marks}) ORDER BY name", list(software_ids)):
            columns.append({**dict(s), "latest": latest.get(s["id"], ""),
                            "releases": [r["version"] for r in releases_for(conn, s["id"])]})
    rows = []
    for m in machines:
        cells = []
        for col in columns:
            inst = installs.get((m["id"], col["id"]))
            cells.append({
                "software_id": col["id"],
                "version": inst["version"] if inst else "",
                "updated_on": inst["updated_on"] if inst else "",
                "status": version_status(inst["version"], col["latest"]) if inst else "none",
            })
        rows.append({"machine": m, "cells": cells})
    return columns, rows


def history(conn, where, params, limit=30):
    return conn.execute(
        f"""SELECT h.*, m.name AS machine_name, s.name AS software_name, c.name AS client_name, c.id AS client_id
            FROM version_history h
            JOIN machines m ON m.id = h.machine_id
            JOIN clients c ON c.id = m.client_id
            JOIN software s ON s.id = h.software_id
            WHERE {where}
            ORDER BY h.changed_on DESC, h.id DESC LIMIT {int(limit)}""",
        params,
    ).fetchall()


def record_versions(conn, changes, who, on_date, job_id=None, note=""):
    """Apply [(machine_id, software_id, new_version)] and log each real change. Returns the number changed."""
    changed = 0
    for machine_id, software_id, new_version in changes:
        new_version = (new_version or "").strip()
        row = conn.execute(
            "SELECT version FROM installations WHERE machine_id = ? AND software_id = ?", (machine_id, software_id)
        ).fetchone()
        old_version = row["version"] if row else ""
        if new_version == old_version:
            continue
        if new_version:
            conn.execute(
                """INSERT INTO installations (machine_id, software_id, version, updated_on, updated_by)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(machine_id, software_id) DO UPDATE SET
                     version = excluded.version, updated_on = excluded.updated_on, updated_by = excluded.updated_by""",
                (machine_id, software_id, new_version, on_date, who),
            )
        else:
            conn.execute("DELETE FROM installations WHERE machine_id = ? AND software_id = ?", (machine_id, software_id))
        conn.execute(
            """INSERT INTO version_history
               (machine_id, software_id, old_version, new_version, changed_on, changed_by, job_id, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (machine_id, software_id, old_version, new_version, on_date, who, job_id, note),
        )
        changed += 1
    return changed


def _changes_from_form(form, machines, columns):
    """Read the grid form: v_<machine>_<software>, with all_<software> overriding a whole column."""
    changes = []
    for col in columns:
        apply_all = form.get(f"all_{col['id']}", "").strip()
        for m in machines:
            name = f"v_{m['id']}_{col['id']}"
            if apply_all:
                changes.append((m["id"], col["id"], apply_all))
            elif name in form:
                changes.append((m["id"], col["id"], form.get(name, "")))
    return changes


def _form_date(form):
    try:
        d = parse_date(form.get("changed_on")) or today()
    except ValueError:
        return None
    return None if d > today() else d.isoformat()


def _extra_ids(conn):
    """Software columns the user asked to add to the grid (?add=<id>, repeatable)."""
    ids = set()
    for value in request.values.getlist("add"):
        if value.isdigit() and conn.execute("SELECT 1 FROM software WHERE id = ?", (int(value),)).fetchone():
            ids.add(int(value))
    return ids


def deployment_link(group):
    """Prefilled 'new maintenance item' link for planning an upgrade."""
    query = urlencode({
        "client": group["client_id"],
        "title": f"Upgrade {group['software_name']} to {group['latest']}",
        "category": "Software deployment",
    })
    return url_for("items.new") + "?" + query


# ---------------------------------------------------------------- catalogue

@bp.route("/")
def index():
    conn = get_db()
    groups = outdated(conn)
    return render_template("software/index.html", software=load_software(conn), outdated=groups,
                           deployment_link=deployment_link)


def _save_software(conn, software_id=None):
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    if not name:
        flash("Name is required.", "error")
        return None
    try:
        if software_id is None:
            software_id = conn.execute("INSERT INTO software (name, notes) VALUES (?, ?)", (name, notes)).lastrowid
        else:
            conn.execute("UPDATE software SET name = ?, notes = ? WHERE id = ?", (name, notes, software_id))
    except sqlite3.IntegrityError:
        flash("Software with that name already exists.", "error")
        return None
    conn.commit()
    return software_id


@bp.route("/new", methods=["GET", "POST"])
def new():
    conn = get_db()
    if request.method == "POST":
        software_id = _save_software(conn)
        if software_id:
            version = request.form.get("version", "").strip()
            if version:
                conn.execute("INSERT INTO software_releases (software_id, version, released_on) VALUES (?, ?, ?)",
                             (software_id, version, today().isoformat()))
                conn.commit()
            flash("Software added.", "success")
            return redirect(url_for("software.detail", software_id=software_id))
    return render_template("software/form.html", sw=request.form)


@bp.route("/<int:software_id>")
def detail(software_id):
    conn = get_db()
    sw = get_software(software_id)
    releases = releases_for(conn, software_id)
    latest = releases[0]["version"] if releases else ""
    installs = []
    for r in conn.execute(
        """SELECT i.*, m.name AS machine_name, c.id AS client_id, c.name AS client_name
           FROM installations i JOIN machines m ON m.id = i.machine_id JOIN clients c ON c.id = m.client_id
           WHERE i.software_id = ? ORDER BY c.name, m.name""",
        (software_id,),
    ):
        installs.append({**dict(r), "status": version_status(r["version"], latest)})
    by_client = {}
    for inst in installs:
        by_client.setdefault((inst["client_id"], inst["client_name"]), []).append(inst)
    behind = {g["client_id"]: g for g in outdated(conn, software_id=software_id)}
    return render_template(
        "software/detail.html", sw=sw, releases=releases, latest=latest, by_client=by_client, behind=behind,
        deployment_link=deployment_link, recent=history(conn, "h.software_id = ?", (software_id,)),
    )


@bp.route("/<int:software_id>/edit", methods=["GET", "POST"])
def edit(software_id):
    conn = get_db()
    sw = get_software(software_id)
    if request.method == "POST" and _save_software(conn, software_id):
        flash("Saved.", "success")
        return redirect(url_for("software.detail", software_id=software_id))
    return render_template("software/form.html", sw=request.form if request.method == "POST" else sw,
                           software_id=software_id)


@bp.route("/<int:software_id>/delete", methods=["POST"])
@admin_required
def delete(software_id):
    conn = get_db()
    sw = get_software(software_id)
    conn.execute("DELETE FROM software WHERE id = ?", (software_id,))
    conn.commit()
    flash(f"Deleted {sw['name']} and its version records.", "success")
    return redirect(url_for("software.index"))


@bp.route("/<int:software_id>/releases", methods=["POST"])
def add_release(software_id):
    conn = get_db()
    get_software(software_id)
    version = request.form.get("version", "").strip()
    try:
        released = parse_date(request.form.get("released_on")) or today()
    except ValueError:
        released = today()
    if not version:
        flash("Enter the version number.", "error")
    else:
        try:
            conn.execute(
                "INSERT INTO software_releases (software_id, version, released_on, notes) VALUES (?, ?, ?, ?)",
                (software_id, version, released.isoformat(), request.form.get("notes", "").strip()),
            )
            conn.commit()
            behind = outdated(conn, software_id=software_id)
            flash(f"Release {version} added." + (
                f" {len(behind)} site{'s' if len(behind) != 1 else ''} now need{'s' if len(behind) == 1 else ''} "
                "upgrading." if behind else ""), "success")
        except sqlite3.IntegrityError:
            flash(f"Version {version} is already recorded.", "error")
    return redirect(url_for("software.detail", software_id=software_id))


@bp.route("/releases/<int:release_id>/delete", methods=["POST"])
def delete_release(release_id):
    conn = get_db()
    rel = conn.execute("SELECT * FROM software_releases WHERE id = ?", (release_id,)).fetchone()
    if rel is None:
        abort(404)
    conn.execute("DELETE FROM software_releases WHERE id = ?", (release_id,))
    conn.commit()
    flash(f"Release {rel['version']} removed.", "success")
    return redirect(url_for("software.detail", software_id=rel["software_id"]))


# ---------------------------------------------------------------- recording versions

@bp.route("/client/<int:client_id>", methods=["GET", "POST"])
def update_client(client_id):
    """Grid of this client's computers x software to record what's installed."""
    conn = get_db()
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        abort(404)
    machines = [dict(m) for m in conn.execute("SELECT * FROM machines WHERE client_id = ? ORDER BY name",
                                              (client_id,))]
    columns, rows = version_grid(conn, machines, _extra_ids(conn))
    if request.method == "POST":
        changed_on = _form_date(request.form)
        if changed_on is None:
            flash("Date must be a valid date, not in the future.", "error")
        else:
            changed = record_versions(conn, _changes_from_form(request.form, machines, columns), display_name(),
                                      changed_on, note=request.form.get("note", "").strip())
            conn.commit()
            flash(f"{changed} version change{'s' if changed != 1 else ''} recorded." if changed
                  else "No versions changed.", "success" if changed else "info")
            return redirect(url_for("clients.detail", client_id=client_id) + "#versions")
    return render_template(
        "software/update.html", client=client, columns=columns, rows=rows,
        all_software=conn.execute("SELECT id, name FROM software ORDER BY name").fetchall(),
        action=url_for("software.update_client", client_id=client_id),
        back=url_for("clients.detail", client_id=client_id) + "#versions",
    )


@bp.route("/job/<int:job_id>", methods=["GET", "POST"])
def update_job(job_id):
    """Same grid, limited to the job's computers; changes are linked to the job."""
    conn = get_db()
    job = get_job(job_id)
    machines = [dict(m) for m in conn.execute(
        """SELECT m.* FROM job_machines jm JOIN machines m ON m.id = jm.machine_id
           WHERE jm.job_id = ? ORDER BY jm.position""",
        (job_id,),
    )]
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (job["client_id"],)).fetchone()
    columns, rows = version_grid(conn, machines, _extra_ids(conn))
    if request.method == "POST":
        changed_on = _form_date(request.form)
        if changed_on is None:
            flash("Date must be a valid date, not in the future.", "error")
        else:
            changes = _changes_from_form(request.form, machines, columns)
            changed = record_versions(conn, changes, display_name(), changed_on, job_id=job_id,
                                      note=request.form.get("note", "").strip())
            if changed:
                applied = [(c["name"], request.form.get(f"all_{c['id']}", "").strip()) for c in columns]
                summary = [f"{name} {version}" for name, version in applied if version]
                conn.execute(
                    "INSERT INTO job_notes (job_id, author, created_at, body) VALUES (?, ?, ?, ?)",
                    (job_id, display_name(), now_str(),
                     f"Recorded {changed} software version change{'s' if changed != 1 else ''}"
                     + (f": {', '.join(summary)} on all computers" if summary else "") + "."),
                )
            conn.commit()
            flash(f"{changed} version change{'s' if changed != 1 else ''} recorded." if changed
                  else "No versions changed.", "success" if changed else "info")
            return redirect(url_for("jobs.detail", job_id=job_id) + "#versions")
    return render_template(
        "software/update.html", client=client, job=job, columns=columns, rows=rows,
        all_software=conn.execute("SELECT id, name FROM software ORDER BY name").fetchall(),
        action=url_for("software.update_job", job_id=job_id),
        back=url_for("jobs.detail", job_id=job_id) + "#versions",
    )
