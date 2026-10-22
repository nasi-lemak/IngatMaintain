from datetime import date, timedelta

from ingatmaintain import reminders
from ingatmaintain.db import create_user
from ingatmaintain.seed import DEPLOY_PROCEDURE, seed_example


def _site_on(admin, conn, version="2.3.0", latest="2.4.0"):
    seed_example(conn)
    admin.post("/versions/new", {"name": "Queue App", "version": version})
    admin.post("/versions/client/1", {"add": "1", "all_1": version})
    if latest:
        admin.post("/versions/1/releases", {"version": latest})


def test_hold_stops_site_being_flagged(admin, conn):
    _site_on(admin, conn)
    assert "Sites behind on software" in admin.get("/").data.decode()

    # Hold needs a reason
    admin.post("/versions/holds", {"client_id": "1", "software_id": "1", "version": "2.3.0"})
    assert conn.execute("SELECT COUNT(*) FROM version_holds").fetchone()[0] == 0

    admin.post("/versions/holds", {"client_id": "1", "software_id": "1", "version": "2.3.0",
                                   "reason": "Waiting for change approval"})
    assert "Sites behind on software" not in admin.get("/").data.decode()
    page = admin.get("/clients/1").data.decode()
    assert "held at" in page and "Waiting for change approval" in page and "ver-held" in page

    # A computer below the held version is still flagged, against the held version
    machine = conn.execute("SELECT id FROM machines ORDER BY id").fetchone()[0]
    admin.post("/versions/client/1", {"add": "1", f"v_{machine}_1": "2.2.0"})
    dash = admin.get("/").data.decode()
    assert "Sites behind on software" in dash and "held" in dash

    # Releasing the hold flags the whole site against 2.4.0 again
    hold_id = conn.execute("SELECT id FROM version_holds").fetchone()[0]
    admin.post(f"/versions/holds/{hold_id}/delete")
    assert "Queue App: 5 computers" in admin.get("/clients/1").data.decode()


def test_upgrade_reminders_first_notice_then_every_30_days(admin, conn, app, monkeypatch):
    create_user(conn, "tech", "password123", "Tech", "tech@example.com")
    _site_on(admin, conn)
    app.config["SMTP_HOST"] = "smtp.example.com"
    sent = []
    monkeypatch.setattr(reminders, "send_email", lambda cfg, to, subject, body: sent.append((subject, body)))
    start = date(2026, 1, 5)
    with app.app_context():
        results = [reminders.run_reminders(conn, app.config, on_date=start + timedelta(days=d))["sent"]
                   for d in (0, 1, 29, 30, 31, 60)]
    assert results == [1, 0, 0, 1, 0, 1]
    subject, body = sent[0]
    assert "1 site behind on software" in subject
    assert "[UPGRADE] NHG Khatib Polyclinic - Queue App: 5 computers on 2.3.0, latest 2.4.0" in body
    assert "No upgrade planned yet" in body
    assert "(behind since 5 Jan 2026)" in sent[1][1]


def test_upgrade_reminder_mentions_planned_item_and_new_release_restarts(admin, conn, app, monkeypatch):
    create_user(conn, "tech", "password123", "Tech", "tech@example.com")
    _site_on(admin, conn)
    admin.post("/items/new", {"client_id": "1", "title": "Upgrade Queue App to 2.4.0"})
    app.config["SMTP_HOST"] = "smtp.example.com"
    app.config["OUTDATED_REMINDER_DAYS"] = 0  # first notice only
    sent = []
    monkeypatch.setattr(reminders, "send_email", lambda cfg, to, subject, body: sent.append(body))
    with app.app_context():
        reminders.run_reminders(conn, app.config, on_date=date(2026, 1, 5))
        assert reminders.run_reminders(conn, app.config, on_date=date(2026, 3, 5))["sent"] == 0
        admin.post("/versions/1/releases", {"version": "2.5.0"})
        assert reminders.run_reminders(conn, app.config, on_date=date(2026, 3, 6))["sent"] == 1
    assert "Planned: Upgrade Queue App to 2.4.0" in sent[0]
    assert "latest 2.5.0" in sent[1]


def test_deployment_checklist_template_and_plan_upgrade(admin, conn):
    _site_on(admin, conn)
    proc = conn.execute("SELECT id FROM procedures WHERE name = ?", (DEPLOY_PROCEDURE,)).fetchone()
    assert proc is not None  # the example loads both built-in checklists
    page = admin.get("/clients/1").data.decode()
    assert f"procedure={proc[0]}" in page  # Plan upgrade preselects it
    form = admin.get(f"/items/new?client=1&procedure={proc[0]}").data.decode()
    assert f'<option value="{proc[0]}" selected>' in form


def test_template_can_be_added_from_checklists_page(admin, conn):
    page = admin.get("/checklists/").data.decode()
    assert "Start from a template" in page and DEPLOY_PROCEDURE in page
    r = admin.post("/checklists/templates/software-deployment")
    assert r.status_code == 302
    steps = conn.execute("SELECT COUNT(*) FROM procedure_steps").fetchone()[0]
    assert steps > 10
    admin.post("/checklists/templates/software-deployment")  # adding again doesn't duplicate
    assert conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 1
    assert admin.post("/checklists/templates/nope").status_code == 404


def test_completing_deployment_job_prompts_for_versions(admin, conn):
    _site_on(admin, conn)
    proc = conn.execute("SELECT id FROM procedures WHERE name = ?", (DEPLOY_PROCEDURE,)).fetchone()[0]
    admin.post("/items/new", {"client_id": "1", "title": "Upgrade Queue App to 2.4.0",
                              "category": "Software deployment", "procedure_id": str(proc)})
    item_id = conn.execute("SELECT MAX(id) FROM maintenance_items").fetchone()[0]
    machines = [r[0] for r in conn.execute("SELECT id FROM machines")]
    resp = admin.post(f"/jobs/start/{item_id}", {"procedure_id": str(proc), "machine_ids": machines})
    job_id = int(resp.location.rstrip("/").split("/")[-1])
    page = admin.get(f"/jobs/{job_id}/complete").data.decode()
    assert "No software versions have been recorded on this job yet" in page
    assert 'name="retire" value="1" checked' in page
    admin.post(f"/versions/job/{job_id}", {"add": "1", "all_1": "2.4.0"})
    assert "No software versions have been recorded" not in admin.get(f"/jobs/{job_id}/complete").data.decode()
