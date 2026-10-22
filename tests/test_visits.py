from datetime import date

from ingatmaintain import reminders
from ingatmaintain.db import create_user
from ingatmaintain.seed import seed_example


def _job(admin, conn, visit_date="2028-05-04"):
    seed_example(conn)
    resp = admin.post("/jobs/start/1", {"procedure_id": "1", "scheduled_date": visit_date,
                                        "assigned_to": ["Ada Admin"]})
    return int(resp.location.rstrip("/").split("/")[-1])


def _visits(conn, job_id):
    return conn.execute(
        "SELECT id, visit_date, assigned_to, purpose FROM job_visits WHERE job_id = ? ORDER BY position", (job_id,)
    ).fetchall()


def _notes(conn, job_id):
    return [r[0] for r in conn.execute("SELECT body FROM job_notes WHERE job_id = ? ORDER BY id", (job_id,))]


def test_new_job_has_one_visit(admin, conn):
    job_id = _job(admin, conn)
    assert [tuple(v)[1:] for v in _visits(conn, job_id)] == [("2028-05-04", "Ada Admin", "")]
    page = admin.get(f"/jobs/{job_id}").data.decode()
    assert "4 May 2028" in page and "Add another visit" in page


def test_reschedule_is_logged_with_reason(admin, conn):
    job_id = _job(admin, conn)
    visit_id = _visits(conn, job_id)[0]["id"]
    admin.post(f"/jobs/{job_id}/visits/{visit_id}", {
        "visit_date": "2028-05-11", "assigned_to": ["Ada Admin"], "reason": "Clinic asked to postpone",
    })
    assert _visits(conn, job_id)[0]["visit_date"] == "2028-05-11"
    assert "Visit moved from 4 May 2028 to 11 May 2028.\nReason: Clinic asked to postpone" in _notes(conn, job_id)
    # Saving without changes logs nothing new
    before = len(_notes(conn, job_id))
    admin.post(f"/jobs/{job_id}/visits/{visit_id}", {"visit_date": "2028-05-11", "assigned_to": ["Ada Admin"]})
    assert len(_notes(conn, job_id)) == before
    # Bad date is rejected
    admin.post(f"/jobs/{job_id}/visits/{visit_id}", {"visit_date": "soon"})
    assert _visits(conn, job_id)[0]["visit_date"] == "2028-05-11"


def test_optional_second_visit(admin, conn):
    job_id = _job(admin, conn)
    first = _visits(conn, job_id)[0]["id"]
    # The only visit can't be removed
    admin.post(f"/jobs/{job_id}/visits/{first}/delete")
    assert len(_visits(conn, job_id)) == 1

    admin.post(f"/jobs/{job_id}/visits", {"visit_date": "2028-05-18", "purpose": "Install certificates",
                                          "assigned_to": ["Ada Admin"]})
    visits = _visits(conn, job_id)
    assert len(visits) == 2 and visits[1]["purpose"] == "Install certificates"
    assert "Visit 2 added (Install certificates) for 18 May 2028. Team: Ada Admin." in _notes(conn, job_id)
    page = admin.get(f"/jobs/{job_id}").data.decode()
    assert "Visit 2 · Install certificates" in page
    printed = " ".join(admin.get(f"/jobs/{job_id}/print").data.decode().split())
    assert "Visit 2 (Install certificates): 18 May 2028" in printed

    admin.post(f"/jobs/{job_id}/visits/{visits[1]['id']}/delete", {"reason": "Synapxe was fast"})
    assert len(_visits(conn, job_id)) == 1


def test_jobs_list_shows_next_upcoming_visit(admin, conn):
    job_id = _job(admin, conn, visit_date="2020-01-01")  # visit 1 already happened
    admin.post(f"/jobs/{job_id}/visits", {"visit_date": "2099-01-02"})
    html = admin.get("/jobs/").data.decode()
    assert "2 Jan 2099" in html and "visit 2 of 2" in html


def test_closed_job_visits_are_read_only(admin, conn):
    job_id = _job(admin, conn)
    admin.post(f"/jobs/{job_id}/status", {"action": "cancel"})
    visit_id = _visits(conn, job_id)[0]["id"]
    admin.post(f"/jobs/{job_id}/visits/{visit_id}", {"visit_date": "2028-06-01"})
    admin.post(f"/jobs/{job_id}/visits", {"visit_date": "2028-06-02"})
    assert [v["visit_date"] for v in _visits(conn, job_id)] == ["2028-05-04"]


def test_calendar_has_each_visit(admin, conn, app):
    job_id = _job(admin, conn)
    admin.post(f"/jobs/{job_id}/visits", {"visit_date": "2028-05-18"})
    admin.get("/settings")
    token = conn.execute("SELECT value FROM settings WHERE key = 'calendar_token'").fetchone()[0]
    body = app.test_client().get(f"/calendar/{token}.ics").data.decode()
    assert "DTSTART;VALUE=DATE:20280504" in body and "DTSTART;VALUE=DATE:20280518" in body
    assert "(visit 2 of 2)" in body


def test_day_before_visit_reminder(admin, conn, app, monkeypatch):
    create_user(conn, "tech", "password123", "Tech", "tech@example.com")
    # 2027, so the certificate (expiring Oct 2028) has no reminder of its own yet.
    job_id = _job(admin, conn, visit_date="2027-05-04")
    app.config["SMTP_HOST"] = "smtp.example.com"
    sent = []
    monkeypatch.setattr(reminders, "send_email", lambda cfg, to, subject, body: sent.append((subject, body)))

    with app.app_context():
        assert reminders.run_reminders(conn, app.config, on_date=date(2027, 5, 2))["sent"] == 0
        assert reminders.run_reminders(conn, app.config, on_date=date(2027, 5, 3))["sent"] == 1
        assert reminders.run_reminders(conn, app.config, on_date=date(2027, 5, 4))["sent"] == 0  # already sent
    subject, body = sent[0]
    assert "1 site visit coming up" in subject
    assert "[VISIT TOMORROW] Tue 4 May 2027 - NHG Khatib Polyclinic" in body
    assert "Team: Ada Admin" in body and "Still to pack: Lenovo secure drive x1" in body
    assert "Check laptop has: OpenSSL" in body

    # Rescheduling sends a fresh reminder for the new date
    visit_id = _visits(conn, job_id)[0]["id"]
    admin.post(f"/jobs/{job_id}/visits/{visit_id}", {"visit_date": "2027-05-11", "assigned_to": ["Ada Admin"]})
    with app.app_context():
        assert reminders.run_reminders(conn, app.config, on_date=date(2027, 5, 10))["sent"] == 1
    assert "[VISIT TOMORROW] Tue 11 May 2027" in sent[-1][1]
