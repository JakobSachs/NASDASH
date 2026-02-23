#!/usr/bin/env python3
"""NAS Dashboard - Single-file server with ZFS, SMART, and system monitoring."""

import json, os, re, subprocess, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HOST, PORT = (
    os.environ.get("NASDASH_HOST", "0.0.0.0"),
    int(os.environ.get("NASDASH_PORT", 8080)),
)
THRESHOLDS = {
    "storage_warn": 80,
    "storage_crit": 90,
    "temp_warn": 40,
    "temp_crit": 50,
    "snap_max_age_h": 48,
}

# --- Collectors ---


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except:
        return None


def collect_zfs():
    data = {
        "pool_state": "UNKNOWN",
        "used": 0,
        "total": 0,
        "used_percent": 0,
        "last_scrub": None,
        "scrub_errors": 0,
        "snapshots": [],
    }
    if out := run(["zpool", "list", "-Hp"]):
        p = out.strip().split("\n")[0].split("\t")
        if len(p) >= 10:
            data["pool_name"], data["total"], data["used"] = p[0], int(p[1]), int(p[2])
            data["used_percent"] = (
                (data["used"] / data["total"]) * 100 if data["total"] else 0
            )
            data["pool_state"] = p[9]
    if out := run(["zpool", "status"]):
        if m := re.search(r"scan:\s+scrub repaired (\d+).*on (.+)", out):
            data["scrub_errors"], data["last_scrub"] = int(m[1]), m[2].strip()
        if "scrub in progress" in out:
            data["last_scrub"] = "In progress"
    if out := run(
        [
            "zfs",
            "list",
            "-t",
            "snapshot",
            "-Hp",
            "-o",
            "name,creation",
            "-s",
            "creation",
        ]
    ):
        for line in out.strip().split("\n")[-10:][::-1]:
            if "\t" in line:
                name, ts = line.split("\t")
                data["snapshots"].append(
                    {"name": name.split("@")[-1], "timestamp": int(ts)}
                )
    return data


def collect_drives():
    drives = []
    if not (out := run(["lsblk", "-d", "-n", "-o", "NAME,TYPE"])):
        return drives
    for line in out.strip().split("\n"):
        p = line.split()
        if len(p) >= 2 and p[1] == "disk":
            dev = f"/dev/{p[0]}"
            info = {
                "device": p[0],
                "model": None,
                "health": "Unknown",
                "temperature": None,
                "power_on_hours": None,
                "reallocated_sectors": None,
                "pending_sectors": None,
            }
            if out := run(["sudo", "smartctl", "-a", "-j", dev]):
                try:
                    d = json.loads(out)
                    info["model"] = d.get("model_name") or d.get("scsi_model_name")
                    info["health"] = (
                        "Healthy"
                        if d.get("smart_status", {}).get("passed")
                        else (
                            "Failing"
                            if d.get("smart_status", {}).get("passed") is False
                            else "Unknown"
                        )
                    )
                    info["temperature"] = d.get("temperature", {}).get("current")
                    for attr in d.get("ata_smart_attributes", {}).get("table", []):
                        raw = attr.get("raw", {}).get("value", 0)
                        if attr["id"] == 5:
                            info["reallocated_sectors"] = raw
                        elif attr["id"] == 9:
                            info["power_on_hours"] = raw
                        elif attr["id"] in (190, 194) and not info["temperature"]:
                            info["temperature"] = raw & 0xFF
                        elif attr["id"] == 197:
                            info["pending_sectors"] = raw
                    if "scsi_grown_defect_list" in d:
                        info["reallocated_sectors"] = d["scsi_grown_defect_list"]
                    if poh := d.get("power_on_time"):
                        info["power_on_hours"] = (
                            poh.get("hours") if isinstance(poh, dict) else poh
                        )
                except json.JSONDecodeError:
                    pass
            drives.append(info)
    return drives


_prev_net = None


def collect_system():
    global _prev_net
    data = {
        "uptime": 0,
        "cpu_percent": 0,
        "memory_total": 0,
        "memory_used": 0,
        "memory_percent": 0,
        "net_rx_rate": 0,
        "net_tx_rate": 0,
    }
    try:
        with open("/proc/uptime") as f:
            data["uptime"] = int(float(f.read().split()[0]))
        with open("/proc/loadavg") as f:
            data["cpu_percent"] = min(
                100, (float(f.read().split()[0]) / (os.cpu_count() or 1)) * 100
            )
        with open("/proc/meminfo") as f:
            mem = {
                k: int(v.split()[0]) * 1024
                for line in f
                for k, v in [line.split(":", 1)]
                if v.strip()
            }
            data["memory_total"], data["memory_used"] = (
                mem.get("MemTotal", 0),
                mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
            )
            data["memory_percent"] = (
                (data["memory_used"] / data["memory_total"]) * 100
                if data["memory_total"]
                else 0
            )
        with open("/proc/net/dev") as f:
            rx = tx = 0
            for line in f.readlines()[2:]:
                if ":" in line and not any(
                    x in line for x in ("lo:", "docker", "veth", "br-")
                ):
                    p = line.split(":")[1].split()
                    rx, tx = rx + int(p[0]), tx + int(p[8])
            now = time.time()
            if _prev_net:
                dt = now - _prev_net[2]
                if dt > 0:
                    data["net_rx_rate"], data["net_tx_rate"] = (
                        max(0, (rx - _prev_net[0]) / dt),
                        max(0, (tx - _prev_net[1]) / dt),
                    )
            _prev_net = (rx, tx, now)
    except:
        pass
    return data


def generate_alerts(data):
    alerts = []
    zfs = data.get("zfs", {})
    if zfs.get("pool_state") not in (None, "ONLINE"):
        alerts.append(f"ZFS pool is {zfs['pool_state']}!")
    if zfs.get("used_percent", 0) >= THRESHOLDS["storage_crit"]:
        alerts.append(f"Storage critical: {zfs['used_percent']:.1f}%")
    elif zfs.get("used_percent", 0) >= THRESHOLDS["storage_warn"]:
        alerts.append(f"Storage warning: {zfs['used_percent']:.1f}%")
    if zfs.get("scrub_errors", 0) > 0:
        alerts.append(f"Scrub found {zfs['scrub_errors']} errors!")
    if snaps := zfs.get("snapshots"):
        age_h = (time.time() - snaps[0].get("timestamp", 0)) / 3600
        if age_h > THRESHOLDS["snap_max_age_h"]:
            alerts.append(f"No snapshots in {int(age_h)}h!")
    for d in data.get("drives", []):
        if d.get("health") == "Failing":
            alerts.append(f"Drive {d['device']} FAILING!")
        if (t := d.get("temperature")) and t >= THRESHOLDS["temp_crit"]:
            alerts.append(f"Drive {d['device']} temp: {t}C!")
        elif t and t >= THRESHOLDS["temp_warn"]:
            alerts.append(f"Drive {d['device']} warm: {t}C")
        if (d.get("reallocated_sectors") or 0) > 0:
            alerts.append(
                f"Drive {d['device']}: {d['reallocated_sectors']} reallocated sectors"
            )
        if (d.get("pending_sectors") or 0) > 0:
            alerts.append(
                f"Drive {d['device']}: {d['pending_sectors']} pending sectors"
            )
    return alerts


# --- Server ---

HTML = (Path(__file__).parent / "index.html").read_text()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            data = {
                "zfs": collect_zfs(),
                "drives": collect_drives(),
                "system": collect_system(),
            }
            data["alerts"] = generate_alerts(data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print(f"NAS Dashboard: http://{HOST}:{PORT}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
