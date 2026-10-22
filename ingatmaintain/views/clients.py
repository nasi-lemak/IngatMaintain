from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..auth import admin_required
from ..db import get_db
from .items import load_items
from .jobs import load_jobs

bp = Blueprint("clients", __name__, url_prefix="/clients")

CLIENT_FIELDS = ("name", "site_address", "contact_name", "contact_phone", "contact_email", "access_notes", "notes")
MACHINE_FIELDS = ("name", "hostname", "ip_address", "location", "os", "notes")


def get_client(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        abort(404)
    return client


def get_machine(machine_id):
    machine = get_db().execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if machine is None:
        abort(404)
    return machine


def _fields(form, names):
    return {name: form.get(name, "").strip() for name in names}


@bp.route("/")
def index():
    conn = get_db()
    clients = conn.execute(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM machines m WHERE m.client_id = c.id) AS machine_count,
               (SELECT COUNT(*) FROM maintenance_items i WHERE i.client_id = c.id AND i.archived = 0) AS item_count,
               (SELECT MIN(i.due_date) FROM maintenance_items i
                WHERE i.client_id = c.id AND i.archived = 0 AND i.due_date IS NOT NULL) AS next_due
        FROM clients c ORDER BY c.name
        """
    ).fetchall()
    return render_template("clients/index.html", clients=clients)


@bp.route("/new", methods=["GET", "POST"])
def new():
    conn = get_db()
    client = {}
    if request.method == "POST":
        client = _fields(request.form, CLIENT_FIELDS)
        if not client["name"]:
            flash("Client name is required.", "error")
        elif conn.execute("SELECT 1 FROM clients WHERE name = ?", (client["name"],)).fetchone():
            flash("A client with that name already exists.", "error")
        else:
            cur = conn.execute(
                f"INSERT INTO clients ({', '.join(CLIENT_FIELDS)}) VALUES ({', '.join('?' * len(CLIENT_FIELDS))})",
                [client[f] for f in CLIENT_FIELDS],
            )
            conn.commit()
            flash("Client added. Now add the computers installed on site.", "success")
            return redirect(url_for("clients.detail", client_id=cur.lastrowid))
    return render_template("clients/form.html", client=client)


@bp.route("/<int:client_id>")
def detail(client_id):
    conn = get_db()
    client = get_client(client_id)
    machines = conn.execute("SELECT * FROM machines WHERE client_id = ? ORDER BY name", (client_id,)).fetchall()
    items = load_items(conn, "i.client_id = ? AND i.archived = 0", (client_id,))
    jobs = load_jobs(conn, "i.client_id = ?", (client_id,), limit=20)
    return render_template("clients/detail.html", client=client, machines=machines, items=items, jobs=jobs)


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
def edit(client_id):
    conn = get_db()
    client = dict(get_client(client_id))
    if request.method == "POST":
        client.update(_fields(request.form, CLIENT_FIELDS))
        clash = conn.execute(
            "SELECT 1 FROM clients WHERE name = ? AND id != ?", (client["name"], client_id)
        ).fetchone()
        if not client["name"]:
            flash("Client name is required.", "error")
        elif clash:
            flash("A client with that name already exists.", "error")
        else:
            conn.execute(
                f"UPDATE clients SET {', '.join(f + ' = ?' for f in CLIENT_FIELDS)} WHERE id = ?",
                [client[f] for f in CLIENT_FIELDS] + [client_id],
            )
            conn.commit()
            flash("Saved.", "success")
            return redirect(url_for("clients.detail", client_id=client_id))
    return render_template("clients/form.html", client=client)


@bp.route("/<int:client_id>/delete", methods=["POST"])
@admin_required
def delete(client_id):
    conn = get_db()
    client = get_client(client_id)
    if request.form.get("confirm_name", "").strip().lower() != client["name"].lower():
        flash("Type the client name exactly to confirm deletion.", "error")
        return redirect(url_for("clients.edit", client_id=client_id))
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    flash(f"Deleted {client['name']} with all its computers, items and jobs.", "success")
    return redirect(url_for("clients.index"))


@bp.route("/<int:client_id>/machines", methods=["POST"])
def add_machine(client_id):
    conn = get_db()
    get_client(client_id)
    machine = _fields(request.form, MACHINE_FIELDS)
    if not machine["name"]:
        flash("Give the computer a name, e.g. 'Consult Room 1 PC'.", "error")
    else:
        conn.execute(
            f"INSERT INTO machines (client_id, {', '.join(MACHINE_FIELDS)}) "
            f"VALUES (?, {', '.join('?' * len(MACHINE_FIELDS))})",
            [client_id] + [machine[f] for f in MACHINE_FIELDS],
        )
        conn.commit()
        flash(f"Added {machine['name']}.", "success")
    return redirect(url_for("clients.detail", client_id=client_id) + "#machines")


@bp.route("/machines/<int:machine_id>/edit", methods=["GET", "POST"])
def edit_machine(machine_id):
    conn = get_db()
    machine = dict(get_machine(machine_id))
    client = get_client(machine["client_id"])
    if request.method == "POST":
        machine.update(_fields(request.form, MACHINE_FIELDS))
        if not machine["name"]:
            flash("Name is required.", "error")
        else:
            conn.execute(
                f"UPDATE machines SET {', '.join(f + ' = ?' for f in MACHINE_FIELDS)} WHERE id = ?",
                [machine[f] for f in MACHINE_FIELDS] + [machine_id],
            )
            conn.commit()
            flash("Saved.", "success")
            return redirect(url_for("clients.detail", client_id=client["id"]) + "#machines")
    return render_template("clients/machine_form.html", machine=machine, client=client)


@bp.route("/machines/<int:machine_id>/delete", methods=["POST"])
def delete_machine(machine_id):
    conn = get_db()
    machine = get_machine(machine_id)
    conn.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
    conn.commit()
    flash(f"Removed {machine['name']}.", "success")
    return redirect(url_for("clients.detail", client_id=machine["client_id"]) + "#machines")
