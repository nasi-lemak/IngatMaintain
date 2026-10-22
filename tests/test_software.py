from ingatmaintain.seed import seed_example
from ingatmaintain.util import version_status


def test_version_comparison():
    cases = {
        ("2.9", "2.10"): "behind",
        ("v1.2", "1.2"): "current",
        ("1.2", "1.2.0"): "current",
        ("2.4.1", "2.4"): "ahead",
        ("2.4.0-rc1", "2.4.0"): "behind",
        ("2.4.0", "2.4.0.1"): "behind",
        ("1.0", ""): "unknown",
    }
    for (installed, latest), expected in cases.items():
        assert version_status(installed, latest) == expected, (installed, latest)


def _setup(admin, conn):
    seed_example(conn)
    admin.post("/versions/new", {"name": "Queue App", "version": "2.3.0"})
    machines = [r["id"] for r in conn.execute("SELECT id FROM machines ORDER BY id")]
    return 1, machines


def _installed(conn):
    return {r["machine_id"]: r["version"] for r in conn.execute("SELECT machine_id, version FROM installations")}


def test_record_versions_for_whole_site_then_one_machine(admin, conn):
    sw_id, machines = _setup(admin, conn)
    # Grid starts empty; add the column, then set all computers at once.
    page = admin.get(f"/versions/client/1?add={sw_id}").data.decode()
    assert f'name="all_{sw_id}"' in page and f'name="v_{machines[0]}_{sw_id}"' in page
    r = admin.post("/versions/client/1", {"add": str(sw_id), f"all_{sw_id}": "2.3.0", "note": "Initial audit"})
    assert r.status_code == 302
    assert set(_installed(conn).values()) == {"2.3.0"} and len(_installed(conn)) == 5

    # Computer 3 only gets 2.3.1 (the others' fields unchanged)
    form = {"add": str(sw_id)}
    for m in machines:
        form[f"v_{m}_{sw_id}"] = "2.3.1" if m == machines[2] else "2.3.0"
    admin.post("/versions/client/1", form)
    assert _installed(conn)[machines[2]] == "2.3.1"
    history = conn.execute("SELECT old_version, new_version, note FROM version_history ORDER BY id").fetchall()
    assert len(history) == 6  # 5 initial + 1 real change; unchanged rows are not logged
    assert tuple(history[0]) == ("", "2.3.0", "Initial audit") and tuple(history[-1])[:2] == ("2.3.0", "2.3.1")


def test_new_release_flags_outdated_sites(admin, conn):
    sw_id, machines = _setup(admin, conn)
    admin.post("/versions/client/1", {"add": str(sw_id), f"all_{sw_id}": "2.3.0"})
    assert "Sites behind on software" not in admin.get("/").data.decode()

    r = admin.post(f"/versions/{sw_id}/releases", {"version": "2.4.0", "notes": "New queue screen"})
    assert r.status_code == 302
    dash = admin.get("/").data.decode()
    assert "Sites behind on software" in dash and "Plan upgrade" in dash
    client_page = admin.get("/clients/1").data.decode()
    assert "Queue App: 5 computers" in client_page and "latest is 2.4.0" in client_page

    # "Plan upgrade" pre-fills a maintenance item
    form = admin.get("/items/new?client=1&title=Upgrade+Queue+App+to+2.4.0&category=Software+deployment")
    assert 'value="Upgrade Queue App to 2.4.0"' in form.data.decode()

    # A duplicate release is refused
    admin.post(f"/versions/{sw_id}/releases", {"version": "2.4.0"})
    assert conn.execute("SELECT COUNT(*) FROM software_releases").fetchone()[0] == 2


def test_versions_recorded_from_a_job_are_linked_to_it(admin, conn):
    sw_id, machines = _setup(admin, conn)
    resp = admin.post("/jobs/start/1", {"procedure_id": "1", "machine_ids": machines[:2]})
    job_id = int(resp.location.rstrip("/").split("/")[-1])
    page = admin.get(f"/versions/job/{job_id}?add={sw_id}").data.decode()
    assert "Computer 1" in page and "Computer 3" not in page  # only the job's computers

    admin.post(f"/versions/job/{job_id}", {"add": str(sw_id), f"all_{sw_id}": "2.3.0"})
    assert len(_installed(conn)) == 2
    assert {r[0] for r in conn.execute("SELECT job_id FROM version_history")} == {job_id}
    notes = [r[0] for r in conn.execute("SELECT body FROM job_notes WHERE job_id = ?", (job_id,))]
    assert "Recorded 2 software version changes: Queue App 2.3.0 on all computers." in notes
    assert "2.3.0" in admin.get(f"/jobs/{job_id}").data.decode()


def test_future_date_rejected_and_clearing_removes(admin, conn):
    sw_id, machines = _setup(admin, conn)
    admin.post("/versions/client/1", {"add": str(sw_id), f"all_{sw_id}": "2.3.0", "changed_on": "2999-01-01"})
    assert _installed(conn) == {}
    admin.post("/versions/client/1", {"add": str(sw_id), f"all_{sw_id}": "2.3.0"})
    admin.post("/versions/client/1", {"add": str(sw_id), f"v_{machines[0]}_{sw_id}": ""})
    assert machines[0] not in _installed(conn) and len(_installed(conn)) == 4
    last = conn.execute("SELECT old_version, new_version FROM version_history ORDER BY id DESC").fetchone()
    assert tuple(last) == ("2.3.0", "")


def test_software_pages_render(admin, conn):
    sw_id, _ = _setup(admin, conn)
    for url in ["/versions/", f"/versions/{sw_id}", f"/versions/{sw_id}/edit", "/versions/new", "/versions/client/1"]:
        assert admin.get(url).status_code == 200, url
