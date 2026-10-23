"""Fleet pages and the agent API."""

import json
from datetime import timedelta
from importlib import resources

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from ..auth import admin_required
from ..db import get_db
from ..fleet import (
    REPORT_KINDS,
    agent_config,
    alert_rows,
    clean_report,
    get_settings,
    hash_secret,
    iso,
    new_secret,
    parse_iso,
    report_alerts,
    run_monitor,
    save_settings,
    sync_alerts,
    utcnow,
)
from ..util import local_tz, today
from .software import record_versions

bp = Blueprint("fleet", __name__, url_prefix="/fleet")
api = Blueprint("agent_api", __name__, url_prefix="/api/agent")


# ---------------------------------------------------------------- helpers

def local_time(value):
    if not value:
        return ""
    return parse_iso(value).astimezone(local_tz()).strftime("%d %b %Y %H:%M")


def ago(value):
    if not value:
        return "never"
    seconds = (utcnow() - parse_iso(value)).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400 * 2:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def connection_state(last_seen):
    """online (checked in within ~3 intervals) / quiet / offline (past FLEET_OFFLINE_HOURS) / never."""
    if not last_seen:
        return "never"
    age = utcnow() - parse_iso(last_seen)
    if age <= timedelta(minutes=3 * current_app.config["FLEET_CHECKIN_MINUTES"]):
        return "online"
    if age <= timedelta(hours=current_app.config["FLEET_OFFLINE_HOURS"]):
        return "quiet"
    return "offline"


def load_devices(conn, where="1 = 1", params=()):
    rows = conn.execute(
        f"""SELECT d.*, m.name AS machine_name, m.client_id, c.name AS client_name,
                   (SELECT COUNT(*) FROM fleet_alerts a WHERE a.machine_id = d.machine_id
                    AND a.resolved_at IS NULL) AS open_alerts
            FROM devices d JOIN machines m ON m.id = d.machine_id JOIN clients c ON c.id = m.client_id
            WHERE {where} ORDER BY c.name, m.name""",
        params,
    ).fetchall()
    devices = []
    for row in rows:
        d = dict(row)
        d["report"] = json.loads(row["report"] or "{}")
        d["state"] = connection_state(row["last_seen"])
        disks = [x for x in d["report"].get("disks", []) if x.get("total_gb")]
        d["min_disk"] = min(disks, key=lambda x: (x["free_gb"] or 0) / x["total_gb"]) if disks else None
        certs = [c for c in d["report"].get("certs", []) if c.get("not_after")]
        d["next_cert"] = min(certs, key=lambda c: c["not_after"]) if certs else None
        devices.append(d)
    return devices


def install_command(key):
    server = current_app.config["BASE_URL"]
    return (
        f"Invoke-WebRequest {server}/api/agent/install.ps1 -OutFile install.ps1 -UseBasicParsing\n"
        f"powershell -ExecutionPolicy Bypass -File .\\install.ps1 -Server {server} -EnrollKey {key}"
    )


def client_fleet_context(conn, client_id):
    """What the client page needs for its monitoring card."""
    settings = get_settings(conn, client_id)
    enroll_open = bool(settings["enroll_key_hash"] and settings["enroll_expires"]
                       and parse_iso(settings["enroll_expires"]) > utcnow())
    return {
        "fleet_settings": settings,
        "fleet_enroll_open": enroll_open,
        "fleet_enroll_until": local_time(settings["enroll_expires"]) if enroll_open else "",
        "fleet_devices": {d["machine_id"]: d for d in load_devices(conn, "m.client_id = ?", (client_id,))},
    }


def _client(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        abort(404)
    return client


@bp.app_template_filter("local_time")
def _local_time_filter(value):
    return local_time(value)


@bp.app_template_filter("ago")
def _ago_filter(value):
    return ago(value)


# ---------------------------------------------------------------- pages

@bp.route("/")
def index():
    conn = get_db()
    client_id = request.args.get("client", type=int)
    devices = load_devices(conn, "m.client_id = ?", (client_id,)) if client_id else load_devices(conn)
    counts = {s: sum(1 for d in devices if d["state"] == s) for s in ("online", "quiet", "offline", "never")}
    alerts = [a for a in alert_rows(conn, "a.resolved_at IS NULL") if not client_id or a["client_id"] == client_id]
    clients = conn.execute(
        "SELECT DISTINCT c.id, c.name FROM clients c JOIN machines m ON m.client_id = c.id "
        "JOIN devices d ON d.machine_id = m.id ORDER BY c.name"
    ).fetchall()
    return render_template("fleet/index.html", devices=devices, counts=counts, alerts=alerts,
                           clients=clients, client_id=client_id)


@bp.route("/devices/<int:device_id>")
def device(device_id):
    conn = get_db()
    devices = load_devices(conn, "d.id = ?", (device_id,))
    if not devices:
        abort(404)
    d = devices[0]
    alerts = conn.execute(
        "SELECT * FROM fleet_alerts WHERE machine_id = ? ORDER BY resolved_at IS NOT NULL, id DESC LIMIT 50",
        (d["machine_id"],),
    ).fetchall()
    settings = get_settings(conn, d["client_id"])
    return render_template("fleet/device.html", d=d, alerts=alerts, settings=settings)


@bp.route("/devices/<int:device_id>/revoke", methods=["POST"])
@admin_required
def revoke(device_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        abort(404)
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    conn.execute("UPDATE fleet_alerts SET resolved_at = ?, notified_open = 1, notified_resolved = 1 "
                 "WHERE machine_id = ? AND resolved_at IS NULL", (iso(utcnow()), row["machine_id"]))
    conn.commit()
    flash("Agent removed - its token no longer works. Uninstall the scheduled task on the computer too.", "success")
    machine = conn.execute("SELECT client_id FROM machines WHERE id = ?", (row["machine_id"],)).fetchone()
    return redirect(url_for("clients.detail", client_id=machine["client_id"]) + "#monitoring")


@bp.route("/clients/<int:client_id>/enroll", methods=["POST"])
@admin_required
def open_enrollment(client_id):
    """Create a new enrollment key. It's shown on this response only - we keep just its hash."""
    conn = get_db()
    client = _client(client_id)
    settings = get_settings(conn, client_id)
    key = new_secret()
    expires = utcnow() + timedelta(days=current_app.config["FLEET_ENROLL_DAYS"])
    settings.update(enroll_key_hash=hash_secret(key), enroll_expires=iso(expires))
    save_settings(conn, settings)
    conn.commit()
    return render_template("fleet/enroll.html", client=client, key=key, command=install_command(key),
                           expires=local_time(iso(expires)))


@bp.route("/clients/<int:client_id>/enroll/close", methods=["POST"])
@admin_required
def close_enrollment(client_id):
    conn = get_db()
    _client(client_id)
    settings = get_settings(conn, client_id)
    settings.update(enroll_key_hash=None, enroll_expires=None)
    save_settings(conn, settings)
    conn.commit()
    flash("Enrollment closed. Installed agents keep working.", "success")
    return redirect(url_for("clients.detail", client_id=client_id) + "#monitoring")


@bp.route("/clients/<int:client_id>/settings", methods=["POST"])
@admin_required
def update_settings(client_id):
    conn = get_db()
    _client(client_id)
    settings = get_settings(conn, client_id)
    services = ",".join(s.strip() for s in request.form.get("services", "").replace("\n", ",").split(",") if s.strip())
    certs = "\n".join(p.strip() for p in request.form.get("cert_paths", "").splitlines() if p.strip())
    pct = request.form.get("disk_free_percent", type=int)
    settings.update(services=services, cert_paths=certs,
                    disk_free_percent=min(max(pct, 1), 90) if pct else 10)
    save_settings(conn, settings)
    conn.commit()
    flash("Monitoring settings saved. Agents pick them up at their next check-in.", "success")
    return redirect(url_for("clients.detail", client_id=client_id) + "#monitoring")


@bp.route("/check", methods=["POST"])
@admin_required
def check_now():
    flash(run_monitor(get_db(), current_app.config), "info")
    return redirect(url_for("fleet.index"))


# ---------------------------------------------------------------- agent API (token auth, no session)

def _agent_file(name):
    return resources.files("ingatmaintain").joinpath("agent", name).read_text()


@api.route("/<any('agent.ps1', 'install.ps1'):name>")
def script(name):
    return Response(_agent_file(name), mimetype="text/plain",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def _json_error(message, status):
    return jsonify({"error": message}), status


@api.route("/enroll", methods=["POST"])
def enroll():
    conn = get_db()
    data = request.get_json(silent=True) or {}
    key = str(data.get("enroll_key") or "")
    hostname = str(data.get("hostname") or "").strip()[:100]
    if not key or not hostname:
        return _json_error("enroll_key and hostname are required", 400)
    settings = conn.execute("SELECT * FROM fleet_settings WHERE enroll_key_hash = ?", (hash_secret(key),)).fetchone()
    if settings is None or not settings["enroll_expires"] or parse_iso(settings["enroll_expires"]) <= utcnow():
        return _json_error("Enrollment key is wrong or has expired - ask an admin for a new one", 401)

    client_id = settings["client_id"]
    machine = conn.execute(
        "SELECT * FROM machines WHERE client_id = ? AND (lower(hostname) = lower(?) OR lower(name) = lower(?)) "
        "ORDER BY hostname = '' LIMIT 1",
        (client_id, hostname, hostname),
    ).fetchone()
    if machine is None:
        machine_id = conn.execute(
            "INSERT INTO machines (client_id, name, hostname, os) VALUES (?, ?, ?, ?)",
            (client_id, hostname, hostname, str(data.get("os") or "")[:100]),
        ).lastrowid
    else:
        machine_id = machine["id"]
        if not machine["hostname"]:
            conn.execute("UPDATE machines SET hostname = ? WHERE id = ?", (hostname, machine_id))

    token = new_secret()
    conn.execute(
        """INSERT INTO devices (machine_id, token_hash, enrolled_at) VALUES (?, ?, ?)
           ON CONFLICT(machine_id) DO UPDATE SET token_hash = excluded.token_hash, enrolled_at = excluded.enrolled_at""",
        (machine_id, hash_secret(token), iso(utcnow())),
    )
    conn.commit()
    name = conn.execute("SELECT name FROM machines WHERE id = ?", (machine_id,)).fetchone()["name"]
    return jsonify({
        "device_token": token,
        "machine": name,
        "config": agent_config(conn, dict(settings), current_app.config["FLEET_CHECKIN_MINUTES"]),
    })


@api.route("/checkin", methods=["POST"])
def checkin():
    conn = get_db()
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    device = conn.execute(
        "SELECT d.*, m.client_id FROM devices d JOIN machines m ON m.id = d.machine_id WHERE d.token_hash = ?",
        (hash_secret(token),),
    ).fetchone() if token else None
    if device is None:
        return _json_error("Unknown or revoked device token - re-run install.ps1 with a new enrollment key", 401)

    report = clean_report(request.get_json(silent=True))
    now = utcnow()
    conn.execute(
        "UPDATE devices SET last_seen = ?, last_ip = ?, agent_version = ?, report = ? WHERE id = ?",
        (iso(now), (request.remote_addr or "")[:45], report["agent_version"], json.dumps(report), device["id"]),
    )

    # Reported versions of tracked software flow straight into version tracking.
    software = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM software")}
    changes = [(device["machine_id"], software[name.lower()], version)
               for name, version in report["software"].items() if name.lower() in software]
    if changes:
        record_versions(conn, changes, "Monitoring agent", today().isoformat(), note="Reported by agent")

    settings = get_settings(conn, device["client_id"])
    alerts = report_alerts(report, settings, today(), current_app.config["FLEET_CERT_WARN_DAYS"])
    sync_alerts(conn, device["machine_id"], alerts, REPORT_KINDS, now)
    sync_alerts(conn, device["machine_id"], {}, ("offline",), now)  # it's talking to us, so it's back
    conn.commit()
    return jsonify({"config": agent_config(conn, settings, current_app.config["FLEET_CHECKIN_MINUTES"])})
