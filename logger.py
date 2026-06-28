"""logger.py — OutGauge UDP drive logger (background thread) + CSV summary.

Backs the MCP start_logging/stop_logging/summarize_drive tools. OutGauge does NOT
use the per-vehicle tech socket, so it is immune to the post-respawn priming issue
that affects the live Electrics telemetry path.

Enable OutGauge in-game first: Options > Other > Protocols > OutGauge,
IP 127.0.0.1, port 4444, blank ID.
"""
from __future__ import annotations

import csv
import os
import socket
import threading
import time
from collections import defaultdict

import outgauge

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(HERE, "logs")
FIELDS = ["t", "speed_kmh", "rpm", "gear", "throttle", "brake", "clutch",
          "fuel", "engTemp"]


class DriveLogger:
    """Records OutGauge packets to a CSV from a background daemon thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bind_error: str | None = None
        self.path: str | None = None
        self.samples = 0
        self.started_at: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, ip: str = "127.0.0.1", port: int = 4444,
              out: str | None = None) -> dict:
        with self._lock:
            if self.running:
                return {"ok": False, "error": "already logging", "path": self.path}
            os.makedirs(LOGS_DIR, exist_ok=True)
            self.path = out or os.path.join(LOGS_DIR, "drive_%d.csv" % int(time.time()))
            self._stop.clear()
            self._bind_error = None
            self.samples = 0
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, args=(ip, port),
                                            daemon=True)
            self._thread.start()
        time.sleep(0.35)  # let it bind / surface an immediate bind error
        if self._bind_error:
            return {"ok": False, "error": self._bind_error,
                    "hint": "is something else bound to UDP %d? OutGauge enabled?" % port}
        return {"ok": True, "logging": True, "path": self.path, "ip": ip, "port": port,
                "note": "recording; drive, then call stop_logging."}

    def _run(self, ip: str, port: int) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((ip, port))
            sock.settimeout(1.0)
        except OSError as exc:
            self._bind_error = repr(exc)
            self._stop.set()
            return
        with open(self.path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(FIELDS)
            t0 = time.time()
            while not self._stop.is_set():
                try:
                    data, _ = sock.recvfrom(512)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    p = outgauge.parse(data)
                except Exception:  # noqa: BLE001 — skip malformed packet
                    continue
                w.writerow([round(time.time() - t0, 3)] + [p.get(k) for k in FIELDS[1:]])
                self.samples += 1
                if self.samples % 10 == 0:
                    fh.flush()
            fh.flush()
        try:
            sock.close()
        except OSError:
            pass

    def status(self) -> dict:
        return {"ok": True, "logging": self.running, "path": self.path,
                "samples": self.samples,
                "elapsed_s": round(time.time() - self.started_at, 1)
                if self.started_at else None}

    def stop(self) -> dict:
        with self._lock:
            running = self.running
            path = self.path
            if running:
                self._stop.set()
        if running and self._thread is not None:
            self._thread.join(timeout=3.0)
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "no active recording"}
        return {"ok": True, "stopped": True, "path": path,
                "summary": summarize_csv(path)}


drive_logger = DriveLogger()


def _gname(g) -> str:
    try:
        gi = int(float(g))
    except (ValueError, TypeError):
        return str(g)
    return "R" if gi == 0 else ("N" if gi == 1 else str(gi - 1))


def latest_log() -> str | None:
    if not os.path.isdir(LOGS_DIR):
        return None
    csvs = [os.path.join(LOGS_DIR, f) for f in os.listdir(LOGS_DIR)
            if f.endswith(".csv")]
    return max(csvs, key=os.path.getmtime) if csvs else None


def summarize_csv(path: str) -> dict:
    rows = []
    try:
        with open(path) as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append((float(r["t"]), float(r["speed_kmh"]),
                                 float(r["rpm"]), r["gear"], float(r["throttle"]),
                                 float(r["brake"]), float(r["fuel"]),
                                 float(r["engTemp"])))
                except (ValueError, KeyError):
                    continue
    except OSError as exc:
        return {"ok": False, "error": "cannot read %s: %r" % (path, exc)}
    if not rows:
        return {"ok": False, "error": "no samples in %s" % path}

    n = len(rows)
    dur = rows[-1][0] - rows[0][0]
    speeds = [r[1] for r in rows]
    top, avg = max(speeds), sum(speeds) / n
    dist = dt = thr = brk = 0.0
    gt: dict = defaultdict(float)
    for a, b in zip(rows, rows[1:]):
        d = b[0] - a[0]
        if not (0 < d < 1):
            continue
        dt += d
        dist += (a[1] / 3.6) * d
        if a[4] > 0.5:
            thr += d
        if a[5] > 0.1:
            brk += d
        gt[a[3]] += d
    z100 = None
    launch = None
    for r in rows:
        if r[1] < 3:
            launch = r[0]
        elif r[1] >= 100 and launch is not None:
            z = r[0] - launch
            z100 = z if z100 is None else min(z100, z)
            launch = None
    fuel_used = rows[0][6] - rows[-1][6]

    blocks = " ▁▂▃▄▅▆▇█"
    bn = 56
    per = dur / bn if dur else 0
    buckets: list = [[] for _ in range(bn)]
    for r in rows:
        i = min(bn - 1, int((r[0] - rows[0][0]) / per)) if per else 0
        buckets[i].append(r[1])
    bavg = [(sum(b) / len(b)) if b else 0 for b in buckets]
    mx = max(bavg) or 1
    spark = "".join(blocks[min(8, int(v / mx * 8))] for v in bavg)

    return {
        "ok": True, "file": os.path.basename(path), "samples": n,
        "hz": round(n / dur, 1) if dur else 0, "duration_s": round(dur, 1),
        "distance_km": round(dist / 1000, 3), "top_speed_kmh": round(top, 1),
        "avg_speed_kmh": round(avg, 1), "max_rpm": round(max(r[2] for r in rows)),
        "zero_to_100_s": round(z100, 2) if z100 else None,
        "throttle_pct": round(100 * thr / dt) if dt else 0,
        "brake_pct": round(100 * brk / dt) if dt else 0,
        "fuel_used_pct": round(100 * fuel_used, 1),
        "max_eng_temp_c": round(max(r[7] for r in rows)),
        "gear_usage_pct": {_gname(g): round(100 * t / dt) for g, t in
                           sorted(gt.items(), key=lambda kv: -kv[1])
                           if dt and t / dt > 0.02},
        "speed_sparkline": spark,
    }


def render_summary(s: dict) -> str:
    if not s.get("ok"):
        return "summary failed: %s" % s.get("error")
    gstr = " · ".join("%s %d%%" % (k, v)
                           for k, v in (s.get("gear_usage_pct") or {}).items())
    z = "%.2f s" % s["zero_to_100_s"] if s.get("zero_to_100_s") else "n/a"
    return "\n".join([
        "== DRIVE SUMMARY : %s ==" % s.get("file"),
        "  samples       : %d  (~%s Hz)" % (s["samples"], s["hz"]),
        "  duration      : %.1f s  (%.1f min)" % (s["duration_s"], s["duration_s"] / 60),
        "  distance      : %.2f km" % s["distance_km"],
        "  top speed     : %.1f km/h" % s["top_speed_kmh"],
        "  avg speed     : %.1f km/h" % s["avg_speed_kmh"],
        "  max rpm       : %d" % s["max_rpm"],
        "  0-100 km/h    : %s" % z,
        "  on throttle   : %d%%" % s["throttle_pct"],
        "  on brakes     : %d%%" % s["brake_pct"],
        "  fuel burned   : %.1f%%" % s["fuel_used_pct"],
        "  max eng temp  : %d C" % s["max_eng_temp_c"],
        "  gear usage    : %s" % gstr,
        "  speed trace   : |%s|" % s["speed_sparkline"],
    ])
