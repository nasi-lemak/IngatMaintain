"""Storage health: server-side alert rules and the Linux agent's parsing."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from ingatmaintain import fleet
from test_fleet import _checkin, _enroll, _enroll_key, _open_alerts, _report

AGENT_PATH = Path(__file__).resolve().parent.parent / "ingatmaintain" / "agent" / "agent.py"


@pytest.fixture
def agent():
    spec = importlib.util.spec_from_file_location("im_agent", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drive(**kw):
    d = {"name": "nvme0n1", "model": "Samsung SSD 980", "type": "NVMe", "health": "OK", "wear_pct": 3,
         "temp_c": 41, "power_on_hours": 12000, "errors": 0, "eol": "", "notes": ""}
    d.update(kw)
    return d


# ---------------------------------------------------------------- server

def test_storage_alerts_rules():
    report = fleet.clean_report({
        "storage": [
            _drive(),                                                      # healthy
            _drive(name="sda", model="CT500MX500", type="SSD", wear_pct=85, errors=8, temp_c=72),
            _drive(name="sdb", model="", health="Failing"),
            _drive(name="mmcblk0", model="", type="eMMC", health="Warning", eol="warning", notes="life used 80-90%"),
        ],
        "storage_events": {"count": 3, "sample": "mmc0: error -110 whilst initialising SD card", "window_hours": 24},
        "readonly_mounts": ["/"],
    })
    alerts = fleet.storage_alerts(report, wear_warn_pct=80, temp_warn_c=70)
    assert alerts == {
        ("storage", "sda:wear"): "Drive sda (CT500MX500) has used 85% of its rated life",
        ("storage", "sda:errors"): "Drive sda (CT500MX500) has recorded 8 bad-sector / uncorrectable errors",
        ("storage", "sda:temp"): "Drive sda (CT500MX500) is running hot (72°C)",
        ("storage", "sdb:health"): "Drive sdb reports it is failing - replace it soon",
        ("storage", "mmcblk0:health"): "Drive mmcblk0 reports a health warning: life used 80-90%",
        ("storage", "mmcblk0:eol"): "eMMC mmcblk0 is near end of life (warning)",
        ("storage", "log"): '3 storage errors in the system log in the last 24 h - '
                            'e.g. "mmc0: error -110 whilst initialising SD card"',
        ("storage", "ro:/"): "/ has switched to read-only - the SD card / drive is probably failing",
    }


def test_storage_alerts_open_and_resolve_via_checkin(admin, conn, app):
    key = _enroll_key(admin, conn)
    token = _enroll(app, key).json["device_token"]
    _checkin(app, token, _report(storage=[_drive(health="Failing")], readonly_mounts=["/"]))
    assert _open_alerts(conn) == [("storage", "nvme0n1:health"), ("storage", "ro:/")]
    page = admin.get("/fleet/devices/1").data.decode()
    assert "Storage health" in page and "Failing" in page and "is read-only" in page
    assert "Failing" in admin.get("/fleet/").data.decode()

    _checkin(app, token, _report(storage=[_drive()]))
    assert _open_alerts(conn) == []


def test_thresholds_are_configurable(admin, conn, app):
    app.config["FLEET_WEAR_WARN_PCT"] = 50
    key = _enroll_key(admin, conn)
    token = _enroll(app, key).json["device_token"]
    _checkin(app, token, _report(storage=[_drive(wear_pct=55)]))
    assert _open_alerts(conn) == [("storage", "nvme0n1:wear")]


def test_linux_scripts_downloadable(app):
    c = app.test_client()
    assert b"AGENT_VERSION" in c.get("/api/agent/agent.py").data
    assert b"systemd" in c.get("/api/agent/install.sh").data


# ---------------------------------------------------------------- Linux agent parsing

NVME_JSON = {
    "model_name": "Samsung SSD 980 1TB", "smart_status": {"passed": True}, "temperature": {"current": 41},
    "power_on_time": {"hours": 12000},
    "nvme_smart_health_information_log": {"critical_warning": 0, "percentage_used": 3, "media_errors": 0},
}
SATA_JSON = {
    "model_name": "CT500MX500SSD1", "smart_status": {"passed": True}, "temperature": {"current": 35},
    "power_on_time": {"hours": 20000},
    "ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "raw": {"value": 8}},
        {"id": 197, "name": "Current_Pending_Sector", "value": 100, "raw": {"value": 0}},
        {"id": 202, "name": "Percent_Lifetime_Remain", "value": 15, "raw": {"value": 85}},
    ]},
}


def _fake_smartctl(agent, monkeypatch, data):
    monkeypatch.setattr(agent.shutil, "which", lambda cmd: "/usr/sbin/" + cmd)
    monkeypatch.setattr(agent, "run", lambda cmd, timeout=20: json.dumps(data))


def test_agent_reads_nvme_smart(agent, monkeypatch):
    _fake_smartctl(agent, monkeypatch, NVME_JSON)
    d = agent.smart_health("nvme0n1", False, "")
    assert (d["type"], d["health"], d["wear_pct"], d["temp_c"], d["errors"]) == ("NVMe", "OK", 3, 41, 0)


def test_agent_reads_sata_smart(agent, monkeypatch):
    _fake_smartctl(agent, monkeypatch, SATA_JSON)
    d = agent.smart_health("sda", False, "")
    assert (d["type"], d["model"], d["wear_pct"], d["errors"], d["power_on_hours"]) == \
        ("SSD", "CT500MX500SSD1", 85, 8, 20000)
    # smartmontools 7.3+ reports wear directly - prefer it
    _fake_smartctl(agent, monkeypatch, {**SATA_JSON, "endurance_used": {"current_percent": 40}})
    assert agent.smart_health("sda", False, "")["wear_pct"] == 40
    # A failed SMART self-assessment
    _fake_smartctl(agent, monkeypatch, {**NVME_JSON, "smart_status": {"passed": False}})
    assert agent.smart_health("nvme0n1", False, "")["health"] == "Failing"


def test_agent_without_smartctl(agent, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda cmd: None)
    d = agent.smart_health("sda", False, "")
    assert d["health"] == "Unknown" and "smartmontools" in d["notes"]


def _fake_sysfs(agent, monkeypatch, files):
    monkeypatch.setattr(agent, "read", lambda path, default="": files.get(path, default))


def test_agent_sd_card_and_emmc(agent, monkeypatch):
    _fake_sysfs(agent, monkeypatch, {"/sys/block/mmcblk0/device/type": "SD", "/sys/block/mmcblk0/device/name": "SC32G"})
    sd = agent.mmc_health("mmcblk0")
    assert sd["type"] == "SD card" and sd["health"] == "Unknown" and "kernel errors" in sd["notes"]

    _fake_sysfs(agent, monkeypatch, {
        "/sys/block/mmcblk0/device/type": "MMC", "/sys/block/mmcblk0/device/life_time": "0x09 0x02",
        "/sys/block/mmcblk0/device/pre_eol_info": "0x02",
    })
    emmc = agent.mmc_health("mmcblk0")
    assert (emmc["type"], emmc["wear_pct"], emmc["eol"], emmc["health"]) == ("eMMC", 90, "warning", "Warning")
    assert emmc["notes"] == "life used 80-90% / 10-20%"

    _fake_sysfs(agent, monkeypatch, {"/sys/block/mmcblk0/device/type": "MMC",
                                     "/sys/block/mmcblk0/device/life_time": "0x00 0x00"})
    assert agent.mmc_health("mmcblk0")["wear_pct"] is None  # 0x00 = not reported


def test_agent_kernel_log_patterns(agent, monkeypatch):
    log = "\n".join([
        "mmc0: new high speed SDHC card at address aaaa",               # normal boot message
        "EXT4-fs (mmcblk0p2): mounted filesystem with ordered data mode",  # normal
        "ata1: SATA link up 6.0 Gbps (SStatus 133 SControl 300)",        # normal
        "mmc0: Timeout waiting for hardware interrupt.",
        "blk_update_request: I/O error, dev mmcblk0, sector 123456 op 0x1:(WRITE)",
        "EXT4-fs error (device mmcblk0p2): ext4_find_entry:1455: inode #2: comm systemd: reading directory lblock 0",
        "Buffer I/O error on dev mmcblk0p2, logical block 98765, lost async page write",
        "ata1.00: failed command: READ FPDMA QUEUED",
    ])
    monkeypatch.setattr(agent, "run", lambda cmd, timeout=20: log if cmd[0] == "journalctl" else "")
    events = agent.storage_events()
    assert events["count"] == 5 and events["window_hours"] == 24  # the 5 error lines, none of the boot lines
    assert events["sample"] == "ata1.00: failed command: READ FPDMA QUEUED"


def test_agent_readonly_root(agent, monkeypatch):
    mounts = "\n".join([
        "/dev/mmcblk0p2 / ext4 ro,noatime 0 0",
        "/dev/mmcblk0p1 /boot/firmware vfat ro,relatime 0 0",   # read-only boot is normal
        "tmpfs /run tmpfs rw,nosuid 0 0",
    ])
    _fake_sysfs(agent, monkeypatch, {"/proc/mounts": mounts})
    assert agent.readonly_mounts() == ["/"]


def test_agent_reads_real_certificate(agent, tmp_path):
    cert = tmp_path / "server.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "k"),
                    "-out", str(cert), "-days", "30", "-subj", "/CN=pi-gateway"], check=True, capture_output=True)
    [entry] = agent.certificates([str(cert)])
    assert entry["error"] == "" and entry["not_after"] and "pi-gateway" in entry["subject"]
    [missing] = agent.certificates([str(tmp_path / "nope.crt")])
    assert missing["error"] == "file not found"
