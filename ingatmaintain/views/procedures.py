"""Checklist templates: ordered steps, laptop software and items to bring."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import get_db

bp = Blueprint("procedures", __name__, url_prefix="/checklists")

SECTION_SUGGESTIONS = ["Preparation", "On site", "Third party / vendor", "After visit", "Wrap up"]


def get_procedure(procedure_id):
    conn = get_db()
    proc = conn.execute("SELECT * FROM procedures WHERE id = ?", (procedure_id,)).fetchone()
    if proc is None:
        abort(404)
    return proc


def load_parts(conn, procedure_id):
    steps = conn.execute(
        "SELECT * FROM procedure_steps WHERE procedure_id = ? ORDER BY position", (procedure_id,)
    ).fetchall()
    reqs = conn.execute(
        "SELECT * FROM procedure_requirements WHERE procedure_id = ? ORDER BY position", (procedure_id,)
    ).fetchall()
    return (
        [dict(s) for s in steps],
        [dict(r) for r in reqs if r["kind"] == "software"],
        [dict(r) for r in reqs if r["kind"] == "equipment"],
    )


def group_sections(steps):
    """[(section, [steps...]), ...] keeping consecutive steps with the same section together."""
    groups = []
    for step in steps:
        if groups and groups[-1][0] == step["section"]:
            groups[-1][1].append(step)
        else:
            groups.append((step["section"], [step]))
    return groups


def _rows(form, prefix, fields):
    """Zip parallel form arrays (step_title, step_details...) into row dicts, skipping blank rows."""
    columns = {f: form.getlist(f"{prefix}_{f}") for f in fields}
    count = max((len(v) for v in columns.values()), default=0)
    rows = []
    for i in range(count):
        row = {f: (columns[f][i].strip() if i < len(columns[f]) else "") for f in fields}
        if row.get("title", row.get("name")):
            rows.append(row)
    return rows


def _parse_form(form):
    data = {"name": form.get("name", "").strip(), "description": form.get("description", "").strip()}
    steps = _rows(form, "step", ("section", "title", "details", "per_machine"))
    for s in steps:
        s["per_machine"] = 1 if s["per_machine"] == "1" else 0
    software = _rows(form, "software", ("name", "details"))
    equipment = _rows(form, "equipment", ("name", "quantity", "details"))
    return data, steps, software, equipment


def _save_parts(conn, procedure_id, steps, software, equipment):
    conn.execute("DELETE FROM procedure_steps WHERE procedure_id = ?", (procedure_id,))
    conn.execute("DELETE FROM procedure_requirements WHERE procedure_id = ?", (procedure_id,))
    conn.executemany(
        "INSERT INTO procedure_steps (procedure_id, position, section, title, details, per_machine) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(procedure_id, i, s["section"], s["title"], s["details"], s["per_machine"]) for i, s in enumerate(steps)],
    )
    reqs = [("software", r) for r in software] + [("equipment", r) for r in equipment]
    conn.executemany(
        "INSERT INTO procedure_requirements (procedure_id, position, kind, name, quantity, details) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(procedure_id, i, kind, r["name"], r.get("quantity", ""), r["details"]) for i, (kind, r) in enumerate(reqs)],
    )


def save_procedure(conn, data, steps, software, equipment, procedure_id=None):
    if procedure_id is None:
        procedure_id = conn.execute(
            "INSERT INTO procedures (name, description) VALUES (?, ?)", (data["name"], data["description"])
        ).lastrowid
    else:
        conn.execute(
            "UPDATE procedures SET name = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (data["name"], data["description"], procedure_id),
        )
    _save_parts(conn, procedure_id, steps, software, equipment)
    conn.commit()
    return procedure_id


def _validate(conn, data, steps, procedure_id=None):
    errors = []
    if not data["name"]:
        errors.append("Give the checklist a name.")
    elif conn.execute(
        "SELECT 1 FROM procedures WHERE name = ? AND id != ?", (data["name"], procedure_id or 0)
    ).fetchone():
        errors.append("A checklist with that name already exists.")
    if not steps:
        errors.append("Add at least one step.")
    return errors


@bp.route("/")
def index():
    procs = get_db().execute(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM procedure_steps s WHERE s.procedure_id = p.id) AS step_count,
               (SELECT COUNT(*) FROM procedure_requirements r WHERE r.procedure_id = p.id) AS req_count,
               (SELECT COUNT(*) FROM maintenance_items i WHERE i.procedure_id = p.id AND i.archived = 0)
                   AS item_count
        FROM procedures p ORDER BY p.name
        """
    ).fetchall()
    from ..seed import TEMPLATES

    existing = {p["name"].lower() for p in procs}
    templates = [(key, t[0], t[1]) for key, t in TEMPLATES.items() if t[0].lower() not in existing]
    return render_template("procedures/index.html", procedures=procs, templates=templates)


@bp.route("/new", methods=["GET", "POST"])
def new():
    conn = get_db()
    if request.method == "POST":
        data, steps, software, equipment = _parse_form(request.form)
        errors = _validate(conn, data, steps)
        if not errors:
            procedure_id = save_procedure(conn, data, steps, software, equipment)
            flash("Checklist created.", "success")
            return redirect(url_for("procedures.detail", procedure_id=procedure_id))
        for e in errors:
            flash(e, "error")
    else:
        data, steps, software, equipment = {"name": "", "description": ""}, [], [], []
    return render_template(
        "procedures/form.html",
        proc=data,
        steps=steps,
        software=software,
        equipment=equipment,
        sections=SECTION_SUGGESTIONS,
    )


@bp.route("/<int:procedure_id>")
def detail(procedure_id):
    conn = get_db()
    proc = get_procedure(procedure_id)
    steps, software, equipment = load_parts(conn, procedure_id)
    items = conn.execute(
        """SELECT i.id, i.title, i.due_date, c.name AS client_name FROM maintenance_items i
           JOIN clients c ON c.id = i.client_id WHERE i.procedure_id = ? AND i.archived = 0
           ORDER BY i.due_date""",
        (procedure_id,),
    ).fetchall()
    return render_template(
        "procedures/detail.html",
        proc=proc,
        groups=group_sections(steps),
        step_count=len(steps),
        software=software,
        equipment=equipment,
        items=items,
    )


@bp.route("/<int:procedure_id>/edit", methods=["GET", "POST"])
def edit(procedure_id):
    conn = get_db()
    proc = get_procedure(procedure_id)
    if request.method == "POST":
        data, steps, software, equipment = _parse_form(request.form)
        errors = _validate(conn, data, steps, procedure_id)
        if not errors:
            save_procedure(conn, data, steps, software, equipment, procedure_id)
            flash("Checklist saved. Jobs already started keep their original steps.", "success")
            return redirect(url_for("procedures.detail", procedure_id=procedure_id))
        for e in errors:
            flash(e, "error")
        data["id"] = procedure_id
    else:
        data = dict(proc)
        steps, software, equipment = load_parts(conn, procedure_id)
    return render_template(
        "procedures/form.html",
        proc=data,
        steps=steps,
        software=software,
        equipment=equipment,
        sections=SECTION_SUGGESTIONS,
    )


@bp.route("/templates/<key>", methods=["POST"])
def add_template(key):
    from ..seed import TEMPLATES, ensure_template

    if key not in TEMPLATES:
        abort(404)
    procedure_id = ensure_template(get_db(), key)
    flash("Checklist added from template - adjust the steps to match how your team works.", "success")
    return redirect(url_for("procedures.detail", procedure_id=procedure_id))


@bp.route("/<int:procedure_id>/duplicate", methods=["POST"])
def duplicate(procedure_id):
    conn = get_db()
    proc = get_procedure(procedure_id)
    steps, software, equipment = load_parts(conn, procedure_id)
    name = f"{proc['name']} (copy)"
    n = 2
    while conn.execute("SELECT 1 FROM procedures WHERE name = ?", (name,)).fetchone():
        name = f"{proc['name']} (copy {n})"
        n += 1
    new_id = save_procedure(conn, {"name": name, "description": proc["description"]}, steps, software, equipment)
    flash("Copy created - rename it and adjust the steps.", "success")
    return redirect(url_for("procedures.edit", procedure_id=new_id))


@bp.route("/<int:procedure_id>/delete", methods=["POST"])
def delete(procedure_id):
    conn = get_db()
    proc = get_procedure(procedure_id)
    conn.execute("DELETE FROM procedures WHERE id = ?", (procedure_id,))
    conn.commit()
    flash(f"Deleted checklist '{proc['name']}'. Existing jobs are unaffected.", "success")
    return redirect(url_for("procedures.index"))
