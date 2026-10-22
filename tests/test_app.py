from ingatmaintain.seed import seed_example


def test_first_run_redirects_to_setup_and_creates_admin(client, conn):
    resp = client.get("/")
    assert resp.status_code == 302 and "/login" in resp.location
    resp = client.get("/login")
    assert resp.status_code == 302 and "/setup" in resp.location
    client.get("/setup")
    resp = client.post("/setup", {"username": "boss", "password": "longenough", "full_name": "Boss"})
    assert resp.status_code == 302
    assert conn.execute("SELECT is_admin FROM users WHERE username = 'boss'").fetchone()[0] == 1
    # Setup is closed once a user exists.
    assert "/login" in client.get("/setup").location


def test_post_without_csrf_is_rejected(admin):
    resp = admin.c.post("/clients/new", data={"name": "Sneaky"})
    assert resp.status_code == 400


def test_login_rejects_wrong_password(client, conn):
    from ingatmaintain.db import create_user

    create_user(conn, "u", "password123")
    client.get("/login")
    resp = client.post("/login", {"username": "u", "password": "nope"})
    assert resp.status_code == 200 and b"Wrong username or password" in resp.data


def test_pages_render(admin, conn):
    seed_example(conn)
    for url in ["/", "/clients/", "/clients/1", "/items/", "/items/1", "/items/new", "/checklists/",
                "/checklists/1", "/checklists/1/edit", "/checklists/new", "/jobs/", "/jobs/start/1",
                "/settings", "/account"]:
        resp = admin.get(url)
        assert resp.status_code == 200, url
    dash = admin.get("/").data.decode()
    assert "NHG Khatib Polyclinic" in dash and "21 Oct 2028" in dash


def test_full_job_flow(admin, conn):
    seed_example(conn)
    machines = [r["id"] for r in conn.execute("SELECT id FROM machines ORDER BY id")]
    assert len(machines) == 5

    resp = admin.post("/jobs/start/1", {
        "procedure_id": "1", "scheduled_date": "2028-05-04", "machine_ids": machines[:3],
        "assigned_to": ["Ada Admin"],
    })
    assert resp.status_code == 302
    job_id = int(resp.location.rstrip("/").split("/")[-1])
    page = admin.get(f"/jobs/{job_id}").data.decode()
    assert "Lenovo secure drive" in page and "OpenSSL" in page and "Portable monitor" in page
    assert "Computer 3" in page and "Computer 4" not in page

    per_machine = conn.execute(
        "SELECT id FROM job_steps WHERE job_id = ? AND per_machine = 1 ORDER BY position LIMIT 1", (job_id,)
    ).fetchone()[0]
    once = conn.execute(
        "SELECT id FROM job_steps WHERE job_id = ? AND per_machine = 0 ORDER BY position LIMIT 1", (job_id,)
    ).fetchone()[0]
    jm = [r[0] for r in conn.execute("SELECT id FROM job_machines WHERE job_id = ? ORDER BY position", (job_id,))]

    json_headers = {"Accept": "application/json"}
    r = admin.post(f"/jobs/{job_id}/toggle", {"kind": "step", "ref_id": per_machine, "machine_id": jm[0]},
                   headers=json_headers)
    assert r.status_code == 200 and r.json["checked"] is True and r.json["by"] == "Ada Admin"
    # 9 one-off steps... total = once-steps + 3 * per-machine steps + requirements
    steps = conn.execute("SELECT per_machine FROM job_steps WHERE job_id = ?", (job_id,)).fetchall()
    reqs = conn.execute("SELECT COUNT(*) FROM job_requirements WHERE job_id = ?", (job_id,)).fetchone()[0]
    assert r.json["total"] == sum(3 if s[0] else 1 for s in steps) + reqs
    assert r.json["done"] == 1

    # Untick
    r = admin.post(f"/jobs/{job_id}/toggle", {"kind": "step", "ref_id": per_machine, "machine_id": jm[0]},
                   headers=json_headers)
    assert r.json["checked"] is False and r.json["done"] == 0

    # Invalid: per-machine step with no machine, one-off step with a machine, step from another job
    assert admin.post(f"/jobs/{job_id}/toggle", {"kind": "step", "ref_id": per_machine, "machine_id": 0},
                      headers=json_headers).status_code == 400
    assert admin.post(f"/jobs/{job_id}/toggle", {"kind": "step", "ref_id": once, "machine_id": jm[0]},
                      headers=json_headers).status_code == 400

    # Non-JS fallback redirects back to the step
    r = admin.post(f"/jobs/{job_id}/toggle", {"kind": "step", "ref_id": once, "machine_id": 0})
    assert r.status_code == 302 and r.location.endswith(f"#step-{once}")

    admin.post(f"/jobs/{job_id}/notes", {"body": "CSRs sent to Synapxe, ticket #42"})
    assert "ticket #42" in admin.get(f"/jobs/{job_id}").data.decode()
    assert admin.get(f"/jobs/{job_id}/print").status_code == 200

    # Completing with unticked boxes needs confirmation
    r = admin.post(f"/jobs/{job_id}/complete", {"next_due_date": "2030-10-20"})
    assert r.status_code == 200 and b"Complete anyway" in r.data
    r = admin.post(f"/jobs/{job_id}/complete", {"next_due_date": "2030-10-20", "force": "1"})
    assert r.status_code == 302
    assert conn.execute("SELECT due_date FROM maintenance_items WHERE id = 1").fetchone()[0] == "2030-10-20"
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "completed" and job["previous_due_date"] == "2028-10-21"

    # Closed jobs can't be ticked
    r = admin.post(f"/jobs/{job_id}/toggle", {"kind": "step", "ref_id": once, "machine_id": 0}, headers=json_headers)
    assert r.status_code == 409


def test_checklist_editor_saves_rows_in_order(admin, conn):
    resp = admin.post("/checklists/new", {
        "name": "Quarterly patching",
        "step_section": ["Prep", "On site", ""],
        "step_title": ["Download patches", "Install patches", ""],
        "step_details": ["", "Reboot after", ""],
        "step_per_machine": ["0", "1", "0"],
        "software_name": ["WSUS offline", ""],
        "software_details": ["", ""],
        "equipment_name": ["USB drive", ""],
        "equipment_quantity": ["2", ""],
        "equipment_details": ["", ""],
    })
    assert resp.status_code == 302
    steps = conn.execute("SELECT section, title, per_machine FROM procedure_steps ORDER BY position").fetchall()
    assert [tuple(s) for s in steps] == [("Prep", "Download patches", 0), ("On site", "Install patches", 1)]
    reqs = conn.execute("SELECT kind, name, quantity FROM procedure_requirements ORDER BY position").fetchall()
    assert [tuple(r) for r in reqs] == [("software", "WSUS offline", ""), ("equipment", "USB drive", "2")]


def test_item_validation_and_create(admin, conn):
    admin.post("/clients/new", {"name": "Clinic A"})
    r = admin.post("/items/new", {"client_id": "1", "title": "Licence", "due_date": "not-a-date"})
    assert r.status_code == 200 and b"valid date" in r.data
    r = admin.post("/items/new", {"client_id": "1", "title": "Licence", "due_date": "2027-01-31",
                                  "reminder_days": "30, 60"})
    assert r.status_code == 302
    assert conn.execute("SELECT reminder_days FROM maintenance_items").fetchone()[0] == "60,30"


def test_calendar_feed(admin, conn, client, app):
    seed_example(conn)
    admin.get("/settings")
    token = conn.execute("SELECT value FROM settings WHERE key = 'calendar_token'").fetchone()[0]
    anon = app.test_client()
    assert anon.get("/calendar/wrong.ics").status_code == 404
    resp = anon.get(f"/calendar/{token}.ics")
    assert resp.status_code == 200 and resp.mimetype == "text/calendar"
    body = resp.data.decode()
    assert "DTSTART;VALUE=DATE:20281021" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))


def test_non_admin_cannot_open_settings(client, conn):
    from ingatmaintain.db import create_user

    create_user(conn, "tech", "password123")
    client.get("/login")
    client.post("/login", {"username": "tech", "password": "password123"})
    assert client.get("/settings").status_code == 403
    assert client.get("/").status_code == 200
