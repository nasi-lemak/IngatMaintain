#!/usr/bin/env python3
"""IngatMaintain monitoring agent for Linux (Ubuntu, Debian, Raspberry Pi OS).

Run every few minutes by the ingatmaintain-agent systemd timer (see install.sh).
It only makes outgoing HTTPS requests to the server; nothing listens on this
computer and the server cannot run commands here.

Reports: hostname, OS, uptime, IP addresses, memory, disk space, the status of
the services the server asks about, the expiry of the certificate files it asks
about (public certificate only - private keys are never read), installed package
versions of tracked software, and storage health:
  * SSD / NVMe / hard drives via smartctl (package smartmontools), if installed
  * eMMC wear and end-of-life estimates from the kernel
  * SD cards: these don't report wear, so the agent watches for the signs of a
    failing card instead - storage errors in the kernel log and a root
    filesystem that has dropped to read-only.

Standard library only; Python 3.7+.
"""

import argparse
import glob
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

AGENT_VERSION = "1.1.0"
DEFAULT_CONFIG = "/etc/ingatmaintain/agent.json"
REAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3", "zfs"}
# Kernel log lines that point at failing storage (SD cards in particular).
STORAGE_ERROR = re.compile(
    r"(I/O error|Buffer I/O error|blk_update_request|mmc\d+: (?:error|timeout)|mmcblk\d+.*error|"
    r"EXT4-fs error|EXT4-fs warning.*I/O|Remounting filesystem read-only|critical medium error|"
    r"nvme\d+.*(?:I/O \d+.*timeout|controller is down)|ata\d+(?:\.\d+)?: (?:failed command|exception Emask))",
    re.IGNORECASE,
)


def run(cmd, timeout=20):
    """Run a command, return stdout ('' if it isn't installed or fails)."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


# ---------------------------------------------------------------- system

def os_name():
    for line in read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return " ".join(os.uname()[::2])


def uptime_hours():
    try:
        return round(float(read("/proc/uptime").split()[0]) / 3600, 1)
    except (ValueError, IndexError):
        return None


def memory():
    info = {}
    for line in read("/proc/meminfo").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            info[m.group(1)] = int(m.group(2))
    gb = 1024 * 1024
    return {"total_gb": round(info.get("MemTotal", 0) / gb, 2) or None,
            "free_gb": round(info.get("MemAvailable", 0) / gb, 2) or None}


def ip_addresses():
    ips = run(["hostname", "-I"]).split()
    if not ips:
        for line in run(["ip", "-4", "-o", "addr", "show"]).splitlines():
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if m and not m.group(1).startswith("127."):
                ips.append(m.group(1))
    return [ip for ip in ips if ":" not in ip][:10]


def mounts():
    """[(device, mountpoint, fstype, options)] for real filesystems on block devices."""
    result = []
    for line in read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("/dev/") and parts[2] in REAL_FILESYSTEMS:
            result.append((parts[0], parts[1].replace("\\040", " "), parts[2], parts[3].split(",")))
    return result


def disks():
    seen, result = set(), []
    for device, mountpoint, _fs, _opts in mounts():
        if device in seen or mountpoint.startswith(("/snap", "/var/lib/docker")):
            continue
        seen.add(device)
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total < 256 * 1024 * 1024:  # skip tiny boot partitions
            continue
        result.append({"name": mountpoint, "total_gb": round(total / 1024 ** 3, 2),
                       "free_gb": round(st.f_bavail * st.f_frsize / 1024 ** 3, 2)})
    return result[:20]


def services(names):
    result = []
    for name in names:
        status = "NotFound"
        out = run(["systemctl", "show", "-p", "LoadState", "-p", "ActiveState", name])
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if props.get("LoadState") and props["LoadState"] != "not-found":
            state = props.get("ActiveState", "")
            status = {"active": "Running", "inactive": "Stopped", "failed": "Failed"}.get(state, state or "Unknown")
        result.append({"name": name, "status": status})
    return result


def certificates(paths):
    result = []
    for path in paths:
        entry = {"path": path, "subject": "", "not_after": "", "error": ""}
        if not os.path.isfile(path):
            entry["error"] = "file not found"
        else:
            out = run(["openssl", "x509", "-noout", "-enddate", "-subject", "-in", path])
            m = re.search(r"notAfter=(.+)", out)
            try:
                if m:
                    expires = time.strptime(m.group(1).strip().replace("  ", " "), "%b %d %H:%M:%S %Y %Z")
                else:  # no openssl: fall back to Python's decoder
                    decoded = ssl._ssl._test_decode_cert(path)  # noqa - stdlib, reads PEM certificates
                    expires = time.strptime(decoded["notAfter"].replace("  ", " "), "%b %d %H:%M:%S %Y %Z")
                    out = "subject=" + ", ".join("=".join(x[0]) for x in decoded.get("subject", ()))
                entry["not_after"] = time.strftime("%Y-%m-%d", expires)
                s = re.search(r"subject=\s*(.+)", out)
                entry["subject"] = s.group(1).strip() if s else ""
            except Exception as exc:  # unreadable / not a certificate
                entry["error"] = str(exc)[:200] or "could not read certificate"
        result.append(entry)
    return result


def software_versions(names):
    found = {}
    if not names:
        return found
    packages = {}
    for line in run(["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"]).splitlines():
        if "\t" in line:
            pkg, version = line.split("\t", 1)
            packages[pkg.lower()] = version
    for name in names:
        key = name.lower()
        match = packages.get(key) or next((v for p, v in packages.items() if key.replace(" ", "-") in p), None)
        if match:
            found[name] = re.sub(r"^\d+:", "", match)  # drop the epoch ("1:2.0.18-1" -> "2.0.18-1")
    return found


# ---------------------------------------------------------------- storage health

def block_devices():
    """[(name, transport, rotational, model)] for physical disks."""
    out = run(["lsblk", "-d", "-J", "-o", "NAME,TYPE,TRAN,ROTA,MODEL"])
    try:
        devices = json.loads(out).get("blockdevices", []) if out else []
    except ValueError:
        devices = []
    if not devices:  # very old lsblk: fall back to sysfs
        devices = [{"name": os.path.basename(p), "type": "disk", "tran": "", "rota": read(p + "/queue/rotational"),
                    "model": read(p + "/device/model")} for p in glob.glob("/sys/block/*")]
    result = []
    for d in devices:
        name = d.get("name") or ""
        if d.get("type") != "disk" or re.match(r"(loop|ram|zram|sr|fd|md|dm-)", name):
            continue
        rota = d.get("rota") in (True, "1", 1)
        result.append((name, (d.get("tran") or "").lower(), rota, (d.get("model") or "").strip()))
    return result


def smart_health(name, rotational, model):
    """Health from smartctl's JSON output (smartmontools 7+)."""
    drive = {"name": name, "model": model, "type": "HDD" if rotational else "SSD", "health": "Unknown",
             "wear_pct": None, "temp_c": None, "power_on_hours": None, "errors": None, "eol": "", "notes": ""}
    if name.startswith("nvme"):
        drive["type"] = "NVMe"
    if not shutil.which("smartctl"):
        drive["notes"] = "Install smartmontools for drive health"
        return drive
    out = run(["smartctl", "--json", "-a", "/dev/" + name], timeout=60)
    try:
        data = json.loads(out)
    except ValueError:
        drive["notes"] = "smartctl gave no data"
        return drive
    drive["model"] = data.get("model_name") or model
    passed = (data.get("smart_status") or {}).get("passed")
    drive["health"] = "OK" if passed is True else ("Failing" if passed is False else "Unknown")
    drive["temp_c"] = (data.get("temperature") or {}).get("current")
    drive["power_on_hours"] = (data.get("power_on_time") or {}).get("hours")

    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        drive["wear_pct"] = nvme.get("percentage_used")
        drive["errors"] = nvme.get("media_errors")
        if nvme.get("critical_warning"):
            drive["health"] = "Warning" if drive["health"] == "OK" else drive["health"]
            drive["notes"] = "NVMe critical warning flag set"
    endurance = (data.get("endurance_used") or {}).get("current_percent")  # smartmontools 7.3+
    if isinstance(endurance, int):
        drive["wear_pct"] = endurance
    attrs = {a.get("id"): a for a in (data.get("ata_smart_attributes") or {}).get("table", [])}
    if attrs:
        if drive["wear_pct"] is None:
            # Approximate life used: 100 - normalised value of the vendor's wear attribute
            # (Percent_Lifetime_Remain, Media_Wearout_Indicator, Wear_Leveling_Count, Remaining_Lifetime_Perc).
            for attr_id in (202, 233, 177, 169):
                if attr_id in attrs and isinstance(attrs[attr_id].get("value"), int):
                    drive["wear_pct"] = max(0, min(100, 100 - attrs[attr_id]["value"]))
                    break
        bad = [attrs[i]["raw"]["value"] for i in (5, 187, 197, 198) if i in attrs and "raw" in attrs[i]]
        if bad:
            drive["errors"] = sum(v for v in bad if isinstance(v, int))
        if any(a.get("when_failed") for a in attrs.values()):
            drive["health"] = "Warning" if drive["health"] == "OK" else drive["health"]
            drive["notes"] = "A SMART attribute is past its failure threshold"
    return drive


EMMC_LIFE = {i: f"{(i - 1) * 10}-{i * 10}%" for i in range(1, 11)}


def mmc_health(name):
    """SD cards report nothing useful; eMMC reports wear and end-of-life estimates."""
    base = f"/sys/block/{name}/device"
    kind = read(base + "/type") or "SD"
    drive = {"name": name, "model": read(base + "/name"), "type": "eMMC" if kind == "MMC" else "SD card",
             "health": "Unknown", "wear_pct": None, "temp_c": None, "power_on_hours": None, "errors": None,
             "eol": "", "notes": ""}
    if kind != "MMC":
        drive["notes"] = "SD cards don't report wear - watching kernel errors and read-only remounts instead"
        return drive
    life = read(base + "/life_time").split()  # e.g. "0x01 0x02": estimates for two block types
    try:
        values = [int(v, 16) for v in life if int(v, 16)]  # 0x00 = not reported
        if values:
            worst = max(values)
            drive["wear_pct"] = min(100, worst * 10) if worst <= 10 else 100
            drive["notes"] = "life used " + " / ".join(EMMC_LIFE.get(v, "past end of life") for v in values)
    except ValueError:
        pass
    pre_eol = read(base + "/pre_eol_info")
    eol = {"0x01": "normal", "0x02": "warning", "0x03": "urgent"}.get(pre_eol.lower(), "")
    drive["eol"] = eol
    drive["health"] = {"normal": "OK", "warning": "Warning", "urgent": "Failing"}.get(eol, "Unknown")
    return drive


def storage_health():
    drives = []
    for name, tran, rotational, model in block_devices():
        if name.startswith("mmcblk"):
            drives.append(mmc_health(name))
        elif tran == "virtio" or re.match(r"(x?vd|hd)[a-z]$", name):
            drives.append({"name": name, "model": model, "type": "Virtual disk", "health": "Unknown",
                           "wear_pct": None, "temp_c": None, "power_on_hours": None, "errors": None, "eol": "",
                           "notes": "Virtual machine disk - the host's storage health isn't visible from inside"})
        else:
            drives.append(smart_health(name, rotational, model))
    return drives[:16]


def storage_events(hours=24):
    """Storage errors in the kernel log over the last `hours` hours (window_hours 0 = since boot)."""
    out = run(["journalctl", "-k", "--since", f"-{hours}h", "--no-pager", "-o", "cat", "-q"])
    window = hours
    if not out:  # no systemd journal: dmesg covers the time since boot
        out, window = run(["dmesg"]), 0
    lines = [line for line in out.splitlines() if STORAGE_ERROR.search(line)]
    return {"count": len(lines), "sample": lines[-1][:300] if lines else "", "window_hours": window}


def readonly_mounts():
    """Filesystems that should be writable but are mounted read-only (a classic dying-SD-card symptom)."""
    return [mp for _dev, mp, _fs, opts in mounts() if "ro" in opts and mp in ("/", "/home", "/var", "/data")]


# ---------------------------------------------------------------- talking to the server

def post_json(url, body, token=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None


def save_config(path, config):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the token is a secret
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)


def clean_list(value):
    return [str(v).strip() for v in (value or []) if v and str(v).strip()]


def enroll(server, key, config_path):
    server = server.rstrip("/")
    if not (server.startswith("https://") or re.match(r"http://(localhost|127\.0\.0\.1)", server)):
        sys.exit("The server address must start with https:// so the device token is never sent unencrypted.")
    result = post_json(server + "/api/agent/enroll",
                       {"enroll_key": key, "hostname": socket.gethostname(), "os": os_name()})
    cfg = result.get("config", {})
    save_config(config_path, {
        "server": server, "token": result["device_token"],
        "services": clean_list(cfg.get("services")), "cert_paths": clean_list(cfg.get("cert_paths")),
        "software": clean_list(cfg.get("software")), "interval_minutes": cfg.get("interval_minutes", 5),
    })
    print(f"Enrolled as '{result.get('machine')}'.")


def collect(config):
    return {
        "agent_version": AGENT_VERSION,
        "hostname": socket.gethostname(),
        "os": os_name(),
        "uptime_hours": uptime_hours(),
        "memory": memory(),
        "ips": ip_addresses(),
        "disks": disks(),
        "services": services(clean_list(config.get("services"))),
        "certs": certificates(clean_list(config.get("cert_paths"))),
        "software": software_versions(clean_list(config.get("software"))),
        "storage": storage_health(),
        "storage_events": storage_events(),
        "readonly_mounts": readonly_mounts(),
    }


def checkin(config_path, print_only=False):
    with open(config_path) as f:
        config = json.load(f)
    report = collect(config)
    if print_only:
        print(json.dumps(report, indent=2))
        return
    response = post_json(config["server"] + "/api/agent/checkin", report, config["token"])
    cfg = response.get("config", {})
    for field in ("services", "cert_paths", "software"):
        config[field] = clean_list(cfg.get(field))
    config["interval_minutes"] = cfg.get("interval_minutes", config.get("interval_minutes", 5))
    save_config(config_path, config)


def main():
    parser = argparse.ArgumentParser(description="IngatMaintain monitoring agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--enroll", action="store_true", help="enroll with --server and --key")
    parser.add_argument("--server")
    parser.add_argument("--key")
    parser.add_argument("--print", action="store_true", help="print the report instead of sending it")
    args = parser.parse_args()
    try:
        if args.enroll:
            if not args.server or not args.key:
                parser.error("--enroll needs --server and --key")
            enroll(args.server, args.key, args.config)
        else:
            checkin(args.config, print_only=args.print)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"IngatMaintain agent: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
