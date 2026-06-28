"""RichLapRecorder: a background-thread CSV lap recorder.

Polls an injected ``poll_fn()`` at a fixed rate, integrates distance from speed,
and writes the RICH_FIELDS column set. Ported from v1 ``lap_telemetry.py`` (the
review called it well-written): guarded CSV open, validates the poll dict to avoid
silent thread death, captures the first poll exception, bounded join, and never
holds a lock across the sleep. One change: timing uses ``time.monotonic`` (immune
to NTP/DST steps); only the CSV filename uses wall-clock.
"""

from __future__ import annotations

import csv
import os
import threading
import time
from collections.abc import Callable

RICH_FIELDS = [
    "t", "dist", "speed", "posx", "posy", "posz", "heading",
    "gx", "gy", "gz",
    "rpm", "gear", "throttle", "brake", "brakeF", "brakeR",
    "steering", "steering_input", "clutch", "boost", "wheelspeed",
    "abs_active", "tcs_active", "esc_active",
]


class RichLapRecorder:
    """Records rich per-lap telemetry to a CSV from a background daemon thread.

    ``poll_fn()`` is called each tick and must return a dict of channel -> value (a
    subset of RICH_FIELDS, WITHOUT ``t``/``dist`` which are synthesised). It may
    raise; the first exception is captured and surfaced via ``status``/``stop``.
    """

    def __init__(self, logs_dir: str, fields: list[str] = RICH_FIELDS) -> None:
        self._logs_dir = logs_dir
        self._fields = fields
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._poll_error: str | None = None
        self.path: str | None = None
        self.samples: int = 0
        self._started: float | None = None  # monotonic

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, poll_fn: Callable[[], dict], hz: float = 30.0, out: str | None = None) -> dict:
        """Spawn the daemon recording thread. Non-blocking."""
        with self._lock:
            if self.running:
                return {"ok": False, "error": "already logging", "path": self.path}
            try:
                os.makedirs(self._logs_dir, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"cannot create logs_dir: {exc!r}"}
            self.path = out or os.path.join(self._logs_dir, f"lap_{int(time.time())}.csv")
            self._stop.clear()
            self._poll_error = None
            self.samples = 0
            self._started = time.monotonic()
            self._thread = threading.Thread(target=self._run, args=(poll_fn, hz), daemon=True)
            self._thread.start()

        time.sleep(0.05)  # let the thread open the file / hit an immediate error
        if self._poll_error:
            return {"ok": False, "error": self._poll_error}
        return {"ok": True, "logging": True, "path": self.path, "hz": hz}

    def status(self) -> dict:
        elapsed = round(time.monotonic() - self._started, 1) if self._started else None
        out: dict = {
            "ok": True, "logging": self.running, "path": self.path,
            "samples": self.samples, "elapsed_s": elapsed,
        }
        if self._poll_error:
            out["poll_error"] = self._poll_error
        return out

    def stop(self) -> dict:
        """Signal the thread to stop, wait up to 3 s, return path + stats."""
        with self._lock:
            was_running = self.running
            path = self.path
            started = self._started
            if was_running:
                self._stop.set()
        if was_running and self._thread is not None:
            self._thread.join(timeout=3.0)
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "no active recording or file missing"}
        duration = round(time.monotonic() - started, 1) if started else None
        out: dict = {
            "ok": True, "stopped": True, "path": path,
            "samples": self.samples, "duration_s": duration,
        }
        if self._poll_error:
            out["poll_error"] = self._poll_error
        return out

    def _run(self, poll_fn: Callable[[], dict], hz: float) -> None:
        interval = 1.0 / max(hz, 0.1)
        t0 = time.monotonic()
        dist = 0.0
        prev_t = t0
        try:
            with open(self.path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(self._fields)
                while not self._stop.is_set():
                    now = time.monotonic()
                    try:
                        row_data = poll_fn()
                    except Exception as exc:  # noqa: BLE001
                        self._poll_error = repr(exc)
                        self._stop.set()
                        break
                    # A non-dict return (e.g. None) would crash .get below OUTSIDE
                    # the guard, silently killing the thread. Validate + surface.
                    if not isinstance(row_data, dict):
                        self._poll_error = f"poll_fn returned {type(row_data).__name__}, expected dict"
                        self._stop.set()
                        break

                    t = now - t0
                    dt = now - prev_t
                    prev_t = now
                    try:
                        speed_ms = float(row_data.get("speed", 0) or 0)
                    except (TypeError, ValueError):
                        speed_ms = 0.0
                    if dt > 0:
                        dist += speed_ms * dt

                    row: list = []
                    for field in self._fields:
                        if field == "t":
                            row.append(round(t, 4))
                        elif field == "dist":
                            row.append(round(dist, 3))
                        else:
                            val = row_data.get(field, "")
                            row.append("" if val is None else val)
                    writer.writerow(row)
                    self.samples += 1
                    if self.samples % 10 == 0:
                        fh.flush()

                    remaining = (now + interval) - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                fh.flush()
        except OSError as exc:
            self._poll_error = f"file error: {exc!r}"
            self._stop.set()


def latest_lap(logs_dir: str) -> str | None:
    """Newest ``lap_*.csv`` in ``logs_dir``, or None."""
    if not os.path.isdir(logs_dir):
        return None
    csvs = [
        os.path.join(logs_dir, f)
        for f in os.listdir(logs_dir)
        if f.startswith("lap_") and f.endswith(".csv")
    ]
    return max(csvs, key=os.path.getmtime) if csvs else None


def read_lap_csv(path: str) -> list[dict]:
    """Parse a lap CSV into row dicts (field -> float|None). Tolerant of bad cells."""
    rows: list[dict] = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for raw in csv.DictReader(fh):
                parsed: dict = {}
                for key, val in raw.items():
                    if key is None:
                        continue
                    if val is None or val == "":
                        parsed[key] = None
                    else:
                        try:
                            parsed[key] = float(val)
                        except ValueError:
                            parsed[key] = None
                rows.append(parsed)
    except OSError:
        pass
    return rows
