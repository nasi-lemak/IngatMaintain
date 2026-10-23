import json
import re
from datetime import date, timedelta

from ingatmaintain import fleet, reminders
from ingatmaintain.seed import seed_example


def _enroll_key(admin, conn):
    seed_example(conn)
    conn.execute("UPDATE machines SET hostname = 'KHATIB-PC1' WHERE name = 'Computer 1'")
    conn.commit()
    page = admin.post("/fleet/clients/1/enroll").data.decode()
    return re.search(r'<p class="secret">([^<]+)</p>', page).group(1)


def _enroll(app, key, hostname="KHATIB-PC1"):
    return app.test_client().post("/api/agent/enroll", json={"enroll_key": key, "hostname": hostname, "os": "Windows"})


def _report(**overrides):
    report = {
        "agent_version": "1.0.0", "hostname": "KHATIB-PC1", "os": "Windows 10 Pro 10.0.19045", "uptime_hours": 50,
        "ips": ["10.1.2.3"], "memory": {"total_gb": 8, "free_gb": 3},
        "disks": [{"name": "C:", "total_gb": 100, "free_gb": 40}],
        "services": [{"name": "mosquitto", "status": "Running"}],
        "certs": [{"path": r"C:\mosquitto\certs\server.crt", "subject": "CN=khatib", "not_after": "2028-10-21", "error": ""}],
        "software": {},
    }
    report.update(overrides)
    return report


def _checkin(app, token, report):
    return app.test_client().post("/api/agent/checkin", json=report, headers={"Authorization": f"Bearer {token}"})


def _open_alerts(conn):
    return sorted((r["kind"], r["key"]) for r in conn.execute("SELECT * FROM fleet_alerts WHERE resolved_at IS NULL"))


def test_enrollment_key_rules(admin, conn, app):
    key = _enroll_key(admin, conn)
    assert len(key) > 30
    # Only the hash is stored
    stored = conn.execute("SELECT enroll_key_hash FROM fleet_settings").fetchone()[0]
    assert stored != key and stored == fleet.hash_secret(key)

    assert _enroll(app, "wrong").status_code == 401
    r = _enroll(app, key)
    assert r.status_code == 200 and r.json["machine"] == "Computer 1"  # matched by hostname
    assert r.json["config"]["interval_minutes"] == 5
    r2 = _enroll(app, key, hostname="KHATIB-PC9")  # unknown -> new computer added to the client
    assert r2.json["machine"] == "KHATIB-PC9"
    assert conn.execute("SELECT client_id FROM machines WHERE name = 'KHATIB-PC9'").fetchone()[0] == 1

    conn.execute("UPDATE fleet_settings SET enroll_expires = '2000-01-01T00:00:00Z'")
    conn.commit()
    assert _enroll(app, key, hostname="KHATIB-PC2").status_code == 401

    admin.post("/fleet/clients/1/enroll/close")
    assert conn.execute("SELECT enroll_key_hash FROM fleet_settings").fetchone()[0] is None


def test_checkin_stores_report_and_syncs_versions(admin, conn, app):
    key = _enroll_key(admin, conn)
    admin.post("/versions/new", {"name": "Mosquitto", "version": "2.0.18"})
    token = _enroll(app, key).json["device_token"]
    assert _checkin(app, "nope", _report()).status_code == 401

    r = _checkin(app, token, _report(software={"Mosquitto": "2.0.15"}))
    assert r.status_code == 200 and r.json["config"]["software"] == ["Mosquitto"]
    device = conn.execute("SELECT * FROM devices").fetchone()
    assert device["agent_version"] == "1.0.0" and json.loads(device["report"])["os"].startswith("Windows 10")
    inst = conn.execute("SELECT version, updated_by FROM installations").fetchone()
    assert tuple(inst) == ("2.0.15", "Monitoring agent")
    # The same version again isn't logged twice
    _checkin(app, token, _report(software={"Mosquitto": "2.0.15"}))
    assert conn.execute("SELECT COUNT(*) FROM version_history").fetchone()[0] == 1
    # ...and the site now shows as behind on Mosquitto
    assert "Sites behind on software" in admin.get("/").data.decode()


def test_alerts_open_and_resolve_from_reports(admin, conn, app):
    key = _enroll_key(admin, conn)
    admin.post("/fleet/clients/1/settings", {
        "services": "mosquitto, MyClinicApp", "cert_paths": r"C:\mosquitto\certs\server.crt", "disk_free_percent": "15",
    })
    token = _enroll(app, key).json["device_token"]
    soon = (date.today() + timedelta(days=10)).isoformat()
    _checkin(app, token, _report(
        disks=[{"name": "C:", "total_gb": 100, "free_gb": 5}],
        services=[{"name": "mosquitto", "status": "Stopped"}],
        certs=[{"path": r"C:\mosquitto\certs\server.crt", "subject": "CN=x", "not_after": soon, "error": ""}],
    ))
    assert _open_alerts(conn) == [("cert", r"C:\mosquitto\certs\server.crt"), ("disk", "C:"),
                                  ("service", "MyClinicApp"), ("service", "mosquitto")]
    messages = {r["key"]: r["message"] for r in conn.execute("SELECT * FROM fleet_alerts")}
    assert messages["mosquitto"] == "Service mosquitto is Stopped"
    assert messages["MyClinicApp"] == "Service MyClinicApp not found"
    assert "only 5.0 GB free (5%)" in messages["C:"]

    _checkin(app, token, _report(services=[{"name": "mosquitto", "status": "Running"},
                                           {"name": "MyClinicApp", "status": "Running"}]))
    assert _open_alerts(conn) == []
    page = admin.get("/fleet/devices/1").data.decode()
    assert "resolved" in page and "10.1.2.3" in page


def test_offline_alert_and_recovery(admin, conn, app):
    key = _enroll_key(admin, conn)
    token = _enroll(app, key).json["device_token"]
    _checkin(app, token, _report())
    two_days_ago = fleet.iso(fleet.utcnow() - timedelta(days=2))
    conn.execute("UPDATE devices SET last_seen = ?", (two_days_ago,))
    conn.commit()
    fleet.check_offline(conn, offline_hours=24)
    assert _open_alerts(conn) == [("offline", "")]
    assert "Offline" in admin.get("/fleet/").data.decode()
    _checkin(app, token, _report())
    assert _open_alerts(conn) == []


def test_alert_notifications(admin, conn, app, monkeypatch):
    key = _enroll_key(admin, conn)
    admin.post("/fleet/clients/1/settings", {"services": "mosquitto"})
    token = _enroll(app, key).json["device_token"]
    app.config["WEBHOOK_URL"] = "https://hooks.example.com/x"
    sent = []
    monkeypatch.setattr(reminders, "send_webhook", lambda cfg, subject, body, *rest: sent.append((subject, body)))

    _checkin(app, token, _report(services=[{"name": "mosquitto", "status": "Stopped"}]))
    with app.app_context():
        fleet.notify_alerts(conn, app.config)
        fleet.notify_alerts(conn, app.config)  # nothing new
    assert len(sent) == 1 and sent[0][0] == "IngatMaintain fleet: 1 new alert"
    assert "[ALERT] NHG Khatib Polyclinic - Computer 1: Service mosquitto is Stopped" in sent[0][1]

    _checkin(app, token, _report())
    with app.app_context():
        fleet.notify_alerts(conn, app.config)
    assert sent[1][0] == "IngatMaintain fleet: 1 resolved"

    # Opened and cleared between two monitor runs: no message at all
    _checkin(app, token, _report(services=[{"name": "mosquitto", "status": "Stopped"}]))
    _checkin(app, token, _report())
    with app.app_context():
        assert fleet.notify_alerts(conn, app.config) == "Only short-lived alerts; nothing sent."
    assert len(sent) == 2


def test_no_channel_marks_alerts_handled(admin, conn, app):
    key = _enroll_key(admin, conn)
    admin.post("/fleet/clients/1/settings", {"services": "mosquitto"})
    token = _enroll(app, key).json["device_token"]
    _checkin(app, token, _report(services=[]))
    with app.app_context():
        assert "no notification channel" in fleet.notify_alerts(conn, app.config)
    assert conn.execute("SELECT COUNT(*) FROM fleet_alerts WHERE notified_open = 0").fetchone()[0] == 0


def test_revoke_and_pages(admin, conn, app):
    key = _enroll_key(admin, conn)
    token = _enroll(app, key).json["device_token"]
    _checkin(app, token, _report())
    for url in ["/fleet/", "/fleet/devices/1", "/clients/1", "/"]:
        assert admin.get(url).status_code == 200, url
    assert "Remote monitoring" in admin.get("/clients/1").data.decode()
    admin.post("/fleet/devices/1/revoke")
    assert _checkin(app, token, _report()).status_code == 401
    assert conn.execute("SELECT COUNT(*) FROM machines WHERE name = 'Computer 1'").fetchone()[0] == 1


def test_agent_scripts_download_without_login(app):
    c = app.test_client()
    for name in ("agent.ps1", "install.ps1"):
        r = c.get(f"/api/agent/{name}")
        assert r.status_code == 200 and b"IngatMaintain" in r.data
    assert c.get("/api/agent/other.ps1").status_code == 404
    assert c.get("/fleet/").status_code == 302  # the pages still need a login


def test_non_admin_cannot_create_enrollment_keys(client, conn):
    from ingatmaintain.db import create_user

    seed_example(conn)
    create_user(conn, "tech", "password123")
    client.get("/login")
    client.post("/login", {"username": "tech", "password": "password123"})
    client.get("/")
    assert client.post("/fleet/clients/1/enroll").status_code == 403


def test_clean_report_rejects_junk():
    r = fleet.clean_report({"disks": "nope", "ips": ["1.2.3.4"] * 50, "hostname": "x" * 500,
                            "services": [{"name": "a", "status": "Running"}, "junk"], "software": {"A": "1", "B": ""}})
    assert r["disks"] == [] and len(r["ips"]) == 10 and len(r["hostname"]) == 100
    assert r["services"] == [{"name": "a", "status": "Running"}] and r["software"] == {"A": "1"}
    assert fleet.clean_report(None)["disks"] == []
