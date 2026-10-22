import sqlite3
from datetime import timedelta

from conftest import Client

from ingatmaintain import create_app
from ingatmaintain.seed import seed_example

JSON = {"Accept": "application/json"}


def _start_job(admin, conn):
    seed_example(conn)
    machines = [r["id"] for r in conn.execute("SELECT id FROM machines ORDER BY id")]
    resp = admin.post("/jobs/start/1", {"procedure_id": "1", "machine_ids": machines})
    return int(resp.location.rstrip("/").split("/")[-1])


def _share(admin, conn, job_id):
    admin.post(f"/jobs/{job_id}/share", {"action": "create"})
    return conn.execute("SELECT share_token FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def _once_step(conn, job_id):
    return conn.execute(
        "SELECT id FROM job_steps WHERE job_id = ? AND per_machine = 0 ORDER BY position LIMIT 1", (job_id,)
    ).fetchone()[0]


def test_sessions_last_a_year_by_default(app):
    assert app.permanent_session_lifetime == timedelta(days=365)


def test_guest_can_tick_via_share_link_only(admin, conn, app):
    job_id = _start_job(admin, conn)
    token = _share(admin, conn, job_id)
    assert token and len(token) >= 30
    assert token in admin.get(f"/jobs/{job_id}").data.decode()

    guest = Client(app.test_client())
    page = guest.get(f"/s/{token}")
    assert page.status_code == 200
    html = page.data.decode()
    assert "Your name" in html and "Lenovo secure drive" in html and "disabled" in html

    step = _once_step(conn, job_id)
    # No name yet -> can't tick
    assert guest.post(f"/s/{token}/toggle", {"kind": "step", "ref_id": step}, headers=JSON).status_code == 400

    guest.post(f"/s/{token}/name", {"name": "  Sam   Lee "})
    r = guest.post(f"/s/{token}/toggle", {"kind": "step", "ref_id": step}, headers=JSON)
    assert r.status_code == 200 and r.json["checked"] and r.json["by"] == "Sam Lee (shared link)"
    guest.post(f"/s/{token}/notes", {"body": "Computer 3 was off"})
    assert "Computer 3 was off" in admin.get(f"/jobs/{job_id}").data.decode()

    # The link opens nothing else.
    for url in [f"/jobs/{job_id}", "/clients/", "/", "/settings", "/jobs/2"]:
        resp = guest.get(url)
        assert resp.status_code == 302 and "/login" in resp.location, url
    assert guest.post(f"/jobs/{job_id}/toggle", {"kind": "step", "ref_id": step}).status_code == 302
    assert guest.get("/s/not-the-token").status_code == 404


def test_link_dies_on_revoke_renew_expiry_and_completion(admin, conn, app):
    job_id = _start_job(admin, conn)
    guest = Client(app.test_client())

    token = _share(admin, conn, job_id)
    admin.post(f"/jobs/{job_id}/share", {"action": "revoke"})
    assert guest.get(f"/s/{token}").status_code == 404

    old = _share(admin, conn, job_id)
    new = _share(admin, conn, job_id)  # renewing replaces the link
    assert old != new
    assert guest.get(f"/s/{old}").status_code == 404
    assert guest.get(f"/s/{new}").status_code == 200

    conn.execute("UPDATE jobs SET share_expires_at = '2000-01-01' WHERE id = ?", (job_id,))
    conn.commit()
    resp = guest.get(f"/s/{new}")
    assert resp.status_code == 404 and b"no longer works" in resp.data

    token = _share(admin, conn, job_id)
    admin.post(f"/jobs/{job_id}/complete", {"next_due_date": "2030-10-20", "force": "1"})
    assert guest.get(f"/s/{token}").status_code == 404
    assert conn.execute("SELECT share_token FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] is None


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, item_id INTEGER, title TEXT, status TEXT, created_at TEXT, "
        "scheduled_date TEXT, assigned_to TEXT NOT NULL DEFAULT '')"
    )
    old.execute("INSERT INTO jobs (item_id, title, status, created_at, scheduled_date, assigned_to) "
                "VALUES (1, 'Old job', 'open', 'x', '2028-05-04', 'Ada')")
    old.commit()
    old.close()
    create_app({"TESTING": True, "SECRET_KEY": "x", "DATABASE": str(path)})
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(jobs)")}
    assert {"share_token", "share_expires_at"} <= cols
    visit = sqlite3.connect(path).execute("SELECT job_id, visit_date, assigned_to FROM job_visits").fetchall()
    assert visit == [(1, "2028-05-04", "Ada")]
