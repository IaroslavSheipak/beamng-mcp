"""DriveLogger: a plain OutGauge-only CSV drive recorder + a human summary.

Independent of the rich per-lap recorder (``timing/recorder.py``): no per-vehicle
socket, no BeamNGpy at all, so it needs no live session and is immune to the
post-respawn priming issue that affects the Electrics telemetry path. Ported
from v1 ``logger.py``. Two changes: ``logs_dir`` is a constructor param instead
of a module-global (so it shares ``Settings.logs_dir`` without a second
directory convention), and elapsed-time uses ``time.monotonic`` (only the CSV
filename keeps wall-clock, for a human-sortable name).
"""

from __future__ import annotations

import csv
import os
import socket
import threading
import time
from collections import defaultdict

from . import outgauge

FIELDS = ["t", "speed_kmh", "rpm", "gear", "throttle", "brake", "clutch", "fuel", "engTemp"]


class DriveLogger:
    """Records OutGauge packets to a CSV from a background daemon thread."""

    def __init__(self, logs_dir: str) -> None:
        self._logs_dir = logs_dir
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bind_error: str | None = None
        self.path: str | None = None
        self.samples = 0
        self._started: float | None = None  # monotonic

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, ip: str = "127.0.0.1", port: int = 4444, out: str | None = None) -> dict:
        with self._lock:
            if self.running:
                return {"ok": False, "error": "already logging", "path": self.path}
            try:
                os.makedirs(self._logs_dir, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"cannot create logs_dir: {exc!r}"}
            self.path = out or os.path.join(self._logs_dir, f"drive_{int(time.time())}.csv")
            self._stop.clear()
            self._bind_error = None
            self.samples = 0
            self._started = time.monotonic()
            self._thread = threading.Thread(target=self._run, args=(ip, port), daemon=True)
            self._thread.start()
        time.sleep(0.35)  # let it bind / surface an immediate bind error
        if self._bind_error:
            return {
                "ok": False, "error": self._bind_error,
                "hint": f"is something else bound to UDP {port}? OutGauge enabled?",
            }
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
        try:
            with open(self.path, "w", newline="", encoding="utf-8") as fh:  # type: ignore[arg-type]
                w = csv.writer(fh)
                w.writerow(FIELDS)
                t0 = time.monotonic()
                while not self._stop.is_set():
                    try:
                        data, _addr = sock.recvfrom(512)
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    try:
                        p = outgauge.parse(data)
                    except Exception:  # noqa: BLE001 — skip a malformed packet
                        continue
                    w.writerow([round(time.monotonic() - t0, 3)] + [p.get(k) for k in FIELDS[1:]])
                    self.samples += 1
                    if self.samples % 10 == 0:
                        fh.flush()
                fh.flush()
        finally:
            sock.close()

    def status(self) -> dict:
        elapsed = round(time.monotonic() - self._started, 1) if self._started else None
        return {"ok": True, "logging": self.running, "path": self.path,
                "samples": self.samples, "elapsed_s": elapsed}

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
        return {"ok": True, "stopped": True, "path": path, "summary": summarize_csv(path)}


def _gname(g: object) -> str:
    try:
        gi = int(float(g))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return str(g)
    return "R" if gi == 0 else ("N" if gi == 1 else str(gi - 1))


def latest_log(logs_dir: str) -> str | None:
    """Newest ``drive_*.csv`` in ``logs_dir`` (prefix-filtered so it can share a
    directory with the rich recorder's ``lap_*.csv`` files), or None."""
    if not os.path.isdir(logs_dir):
        return None
    csvs = [
        os.path.join(logs_dir, f)
        for f in os.listdir(logs_dir)
        if f.startswith("drive_") and f.endswith(".csv")
    ]
    return max(csvs, key=os.path.getmtime) if csvs else None


def summarize_csv(path: str) -> dict:
    """Distance/speed/0-100/throttle-brake%/gear-usage/sparkline summary of a
    plain drive CSV (the FIELDS this module writes)."""
    rows: list[tuple[float, float, float, str, float, float, float, float]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in csv.DictReader(fh):
                try:
                    rows.append((
                        float(raw["t"]), float(raw["speed_kmh"]), float(raw["rpm"]), raw["gear"],
                        float(raw["throttle"]), float(raw["brake"]), float(raw["fuel"]),
                        float(raw["engTemp"]),
                    ))
                except (ValueError, KeyError):
                    continue
    except OSError as exc:
        return {"ok": False, "error": f"cannot read {path}: {exc!r}"}
    if not rows:
        return {"ok": False, "error": f"no samples in {path}"}

    n = len(rows)
    dur = rows[-1][0] - rows[0][0]
    speeds = [r[1] for r in rows]
    top, avg = max(speeds), sum(speeds) / n
    dist = dt = thr = brk = 0.0
    gt: dict = defaultdict(float)
    for a, b in zip(rows, rows[1:], strict=False):
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
        "gear_usage_pct": {
            _gname(g): round(100 * t / dt)
            for g, t in sorted(gt.items(), key=lambda kv: -kv[1])
            if dt and t / dt > 0.02
        },
        "speed_sparkline": spark,
    }


def render_summary(s: dict) -> str:
    """Plain-text rendering of a :func:`summarize_csv` result."""
    if not s.get("ok"):
        return f"summary failed: {s.get('error')}"
    gstr = " · ".join(f"{k} {v}%" for k, v in (s.get("gear_usage_pct") or {}).items())
    z = f"{s['zero_to_100_s']:.2f} s" if s.get("zero_to_100_s") else "n/a"
    return "\n".join([
        f"== DRIVE SUMMARY : {s.get('file')} ==",
        f"  samples       : {s['samples']}  (~{s['hz']} Hz)",
        f"  duration      : {s['duration_s']:.1f} s  ({s['duration_s'] / 60:.1f} min)",
        f"  distance      : {s['distance_km']:.2f} km",
        f"  top speed     : {s['top_speed_kmh']:.1f} km/h",
        f"  avg speed     : {s['avg_speed_kmh']:.1f} km/h",
        f"  max rpm       : {s['max_rpm']}",
        f"  0-100 km/h    : {z}",
        f"  on throttle   : {s['throttle_pct']}%",
        f"  on brakes     : {s['brake_pct']}%",
        f"  fuel burned   : {s['fuel_used_pct']:.1f}%",
        f"  max eng temp  : {s['max_eng_temp_c']} C",
        f"  gear usage    : {gstr}",
        f"  speed trace   : |{s['speed_sparkline']}|",
    ])
