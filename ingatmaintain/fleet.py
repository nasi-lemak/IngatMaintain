"""Fleet monitoring for client sites that allow outbound internet access.

A small agent on each computer checks in over HTTPS every few minutes. It only
ever *sends* information; the server never connects to it or runs anything on
it. From each check-in we keep the latest report, update the software
versions, and open or resolve alerts. A background monitor raises "offline"
alerts and sends notifications.
"""

import hashlib
import logging
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone

from .db import connect

log = logging.getLogger(__name__)

# Alert kinds raised from a check-in report (offline is raised by the monitor).
REPORT_KINDS = ("disk", "service", "cert", "storage")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def new_secret():
    return secrets.token_urlsafe(32)


def hash_secret(value):
    return hashlib.sha256(value.encode()).hexdigest()


def get_settings(conn, client_id):
    row = conn.execute("SELECT * FROM fleet_settings WHERE client_id = ?", (client_id,)).fetchone()
    if row:
        return dict(row)
    return {"client_id": client_id, "enroll_key_hash": None, "enroll_expires": None,
            "services": "", "cert_paths": "", "disk_free_percent": 10}


def save_settings(conn, settings):
    conn.execute(
        """INSERT INTO fleet_settings (client_id, enroll_key_hash, enroll_expires, services, cert_paths, disk_free_percent)
           VALUES (:client_id, :enroll_key_hash, :enroll_expires, :services, :cert_paths, :disk_free_percent)
           ON CONFLICT(client_id) DO UPDATE SET enroll_key_hash = excluded.enroll_key_hash,
             enroll_expires = excluded.enroll_expires, services = excluded.services,
             cert_paths = excluded.cert_paths, disk_free_percent = excluded.disk_free_percent""",
        settings,
    )


def watched_services(settings):
    return [s.strip() for s in settings["services"].split(",") if s.strip()]


def watched_certs(settings):
    return [p.strip() for p in settings["cert_paths"].splitlines() if p.strip()]


def agent_config(conn, settings, interval_minutes):
    """What the agent should collect, sent back on every check-in."""
    software = [r["name"] for r in conn.execute("SELECT name FROM software ORDER BY name")]
    return {
        "services": watched_services(settings),
        "cert_paths": watched_certs(settings),
        "software": software,
        "interval_minutes": interval_minutes,
    }


# ---------------------------------------------------------------- reports

def _text(value, limit=200):
    return str(value if value is not None else "")[:limit]


def _number(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def clean_report(data):
    """Keep only known fields with bounded sizes - the agent is not trusted to send sane data."""
    data = data if isinstance(data, dict) else {}

    def items(key, limit):
        value = data.get(key)
        return [v for v in value[:limit] if isinstance(v, dict)] if isinstance(value, list) else []

    memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
    software = data.get("software") if isinstance(data.get("software"), dict) else {}
    events = data.get("storage_events") if isinstance(data.get("storage_events"), dict) else {}
    return {
        "hostname": _text(data.get("hostname"), 100),
        "os": _text(data.get("os"), 200),
        "agent_version": _text(data.get("agent_version"), 20),
        "uptime_hours": _number(data.get("uptime_hours")),
        "ips": [_text(ip, 45) for ip in (data.get("ips") or [])[:10]] if isinstance(data.get("ips"), list) else [],
        "memory": {"total_gb": _number(memory.get("total_gb")), "free_gb": _number(memory.get("free_gb"))},
        "disks": [{"name": _text(d.get("name"), 50), "total_gb": _number(d.get("total_gb")),
                   "free_gb": _number(d.get("free_gb"))} for d in items("disks", 20)],
        "services": [{"name": _text(s.get("name"), 100), "status": _text(s.get("status"), 30)}
                     for s in items("services", 50)],
        "certs": [{"path": _text(c.get("path"), 300), "subject": _text(c.get("subject"), 300),
                   "not_after": _text(c.get("not_after"), 10), "error": _text(c.get("error"), 200)}
                  for c in items("certs", 20)],
        "software": {_text(k, 100): _text(v, 50) for k, v in list(software.items())[:100] if v},
        "storage": [{"name": _text(d.get("name"), 100), "model": _text(d.get("model"), 100),
                     "type": _text(d.get("type"), 30), "health": _text(d.get("health"), 20),
                     "wear_pct": _number(d.get("wear_pct")), "temp_c": _number(d.get("temp_c")),
                     "power_on_hours": _number(d.get("power_on_hours")), "errors": _number(d.get("errors")),
                     "eol": _text(d.get("eol"), 20), "notes": _text(d.get("notes"), 200)}
                    for d in items("storage", 16)],
        "storage_events": {"count": int(_number(events.get("count")) or 0),
                           "sample": _text(events.get("sample"), 300),
                           "window_hours": int(_number(events.get("window_hours")) or 0)},
        "readonly_mounts": [_text(m, 100) for m in (data.get("readonly_mounts") or [])[:10]]
        if isinstance(data.get("readonly_mounts"), list) else [],
    }


def storage_alerts(report, wear_warn_pct, temp_warn_c):
    """{(kind, key): message} for drives that are failing, worn out, erroring or too hot."""
    alerts = {}
    for d in report.get("storage", []):
        name = f"{d['name']} ({d['model']})" if d["model"] else d["name"]
        health = d["health"].lower()
        if health == "failing":
            alerts[("storage", d["name"] + ":health")] = f"Drive {name} reports it is failing - replace it soon"
        elif health == "warning":
            detail = f": {d['notes']}" if d["notes"] else ""
            alerts[("storage", d["name"] + ":health")] = f"Drive {name} reports a health warning{detail}"
        if d["eol"] in ("warning", "urgent"):
            alerts[("storage", d["name"] + ":eol")] = f"{d['type']} {name} is near end of life ({d['eol']})"
        if d["wear_pct"] is not None and d["wear_pct"] >= wear_warn_pct:
            alerts[("storage", d["name"] + ":wear")] = f"Drive {name} has used {d['wear_pct']:.0f}% of its rated life"
        if d["errors"]:
            alerts[("storage", d["name"] + ":errors")] = (
                f"Drive {name} has recorded {d['errors']:.0f} bad-sector / uncorrectable errors")
        if d["temp_c"] is not None and d["temp_c"] >= temp_warn_c:
            alerts[("storage", d["name"] + ":temp")] = f"Drive {name} is running hot ({d['temp_c']:.0f}°C)"
    events = report.get("storage_events") or {}
    if events.get("count"):
        window = f"in the last {events['window_hours']} h" if events.get("window_hours") else "since boot"
        n = events["count"]
        alerts[("storage", "log")] = (f"{n} storage error{'s' if n != 1 else ''} in the system log {window}"
                                      + (f" - e.g. \"{events['sample']}\"" if events.get("sample") else ""))
    for mount in report.get("readonly_mounts", []):
        alerts[("storage", "ro:" + mount)] = (f"{mount} has switched to read-only - "
                                              "the SD card / drive is probably failing")
    return alerts


def report_alerts(report, settings, on_date, cert_warn_days, wear_warn_pct=80, temp_warn_c=70):
    """{(kind, key): message} for problems visible in a check-in report."""
    alerts = storage_alerts(report, wear_warn_pct, temp_warn_c)
    for disk in report["disks"]:
        total, free = disk["total_gb"], disk["free_gb"]
        if total and free is not None:
            pct = 100 * free / total
            if pct < settings["disk_free_percent"]:
                alerts[("disk", disk["name"])] = f"Disk {disk['name']} only {free:.1f} GB free ({pct:.0f}%)"
    reported = {s["name"].lower(): s["status"] for s in report["services"]}
    for name in watched_services(settings):
        status = reported.get(name.lower())
        if status is None or status.lower() == "notfound":
            alerts[("service", name)] = f"Service {name} not found"
        elif status.lower() not in ("running", "active"):
            alerts[("service", name)] = f"Service {name} is {status}"
    for cert in report["certs"]:
        path = cert["path"]
        if cert["error"]:
            alerts[("cert", path)] = f"Can't read certificate {path}: {cert['error']}"
            continue
        try:
            expires = date.fromisoformat(cert["not_after"])
        except ValueError:
            continue
        days = (expires - on_date).days
        if days < 0:
            alerts[("cert", path)] = f"Certificate {path} expired on {expires:%d %b %Y}"
        elif days <= cert_warn_days:
            alerts[("cert", path)] = f"Certificate {path} expires in {days} days ({expires:%d %b %Y})"
    return alerts


def sync_alerts(conn, machine_id, current, kinds, now):
    """Open alerts in `current` that aren't open yet; resolve open ones of `kinds` that are gone."""
    open_rows = conn.execute(
        f"SELECT * FROM fleet_alerts WHERE machine_id = ? AND resolved_at IS NULL "
        f"AND kind IN ({','.join('?' * len(kinds))})",
        (machine_id, *kinds),
    ).fetchall()
    open_keys = {(r["kind"], r["key"]): r for r in open_rows}
    for (kind, key), message in current.items():
        row = open_keys.get((kind, key))
        if row is None:
            conn.execute(
                "INSERT INTO fleet_alerts (machine_id, kind, key, message, opened_at) VALUES (?, ?, ?, ?, ?)",
                (machine_id, kind, key, message, iso(now)),
            )
        elif row["message"] != message:  # e.g. disk free figure changed - keep it current
            conn.execute("UPDATE fleet_alerts SET message = ? WHERE id = ?", (message, row["id"]))
    for (kind, key), row in open_keys.items():
        if (kind, key) not in current:
            conn.execute("UPDATE fleet_alerts SET resolved_at = ? WHERE id = ?", (iso(now), row["id"]))


def check_offline(conn, offline_hours, now=None):
    """Open an offline alert for each device not seen for `offline_hours`."""
    now = now or utcnow()
    cutoff = now - timedelta(hours=offline_hours)
    for device in conn.execute("SELECT * FROM devices WHERE last_seen IS NOT NULL").fetchall():
        last_seen = parse_iso(device["last_seen"])
        current = {}
        if last_seen < cutoff:
            current[("offline", "")] = f"Not seen since {last_seen:%d %b %Y %H:%M} UTC"
        sync_alerts(conn, device["machine_id"], current, ("offline",), now)
    conn.commit()


# ---------------------------------------------------------------- notifications

def alert_rows(conn, where):
    return conn.execute(
        f"""SELECT a.*, m.name AS machine_name, c.name AS client_name, c.id AS client_id
            FROM fleet_alerts a JOIN machines m ON m.id = a.machine_id JOIN clients c ON c.id = m.client_id
            WHERE {where} ORDER BY c.name, m.name, a.id"""
    ).fetchall()


def notify_alerts(conn, config):
    """Send newly opened and newly resolved alerts as one message. Returns a summary string.

    "Resolved" is only sent for alerts that were announced; an alert that opened and cleared
    between two monitor runs is marked as handled without a message.
    """
    from .reminders import configured_channels, deliver, notify_recipients

    opened = alert_rows(conn, "a.notified_open = 0")
    resolved = alert_rows(conn, "a.notified_open = 1 AND a.resolved_at IS NOT NULL AND a.notified_resolved = 0")
    if not opened and not resolved:
        return "No fleet alerts to send."

    def mark_sent():
        conn.executemany(
            "UPDATE fleet_alerts SET notified_open = 1, "
            "notified_resolved = CASE WHEN resolved_at IS NULL THEN 0 ELSE 1 END WHERE id = ?",
            [(a["id"],) for a in opened],
        )
        conn.executemany("UPDATE fleet_alerts SET notified_resolved = 1 WHERE id = ?", [(a["id"],) for a in resolved])
        conn.commit()

    lines = [f"[ALERT] {a['client_name']} - {a['machine_name']}: {a['message']}" for a in opened if not a["resolved_at"]]
    n_open = len(lines)
    lines += [f"[RESOLVED] {a['client_name']} - {a['machine_name']}: {a['message']}" for a in resolved]
    if not lines:
        mark_sent()
        return "Only short-lived alerts; nothing sent."
    if not configured_channels(config, notify_recipients(conn)):
        # Nothing to send through; don't let a backlog build up for when a channel is added.
        mark_sent()
        return "Fleet alerts recorded (no notification channel configured)."

    n_resolved = len(lines) - n_open
    parts = ([f"{n_open} new alert{'s' if n_open != 1 else ''}"] if n_open else []) + \
            ([f"{n_resolved} resolved"] if n_resolved else [])
    subject = "IngatMaintain fleet: " + ", ".join(parts)
    body = "\n".join(lines) + f"\n\nFleet: {config['BASE_URL']}/fleet/\n"
    succeeded, errors = deliver(config, conn, subject, body)
    if not succeeded:
        return "Fleet alert sending failed: " + "; ".join(errors)  # retried next run
    mark_sent()
    return f"Sent {len(lines)} fleet alert update(s) via {', '.join(succeeded)}."


def run_monitor(conn, config):
    check_offline(conn, config["FLEET_OFFLINE_HOURS"])
    return notify_alerts(conn, config)


def start_monitor(app):
    """Background thread: offline check + alert notifications every FLEET_CHECK_MINUTES."""

    def loop():
        while True:
            conn = connect(app.config["DATABASE"])
            try:
                if conn.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
                    with app.app_context():
                        message = run_monitor(conn, app.config)
                    log.info("Fleet monitor: %s", message)
            except Exception:
                log.exception("Fleet monitor failed")
            finally:
                conn.close()
            time.sleep(app.config["FLEET_CHECK_MINUTES"] * 60)

    thread = threading.Thread(target=loop, name="fleet-monitor", daemon=True)
    thread.start()
    return thread
