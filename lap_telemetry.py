"""lap_telemetry.py — RichLapRecorder: background-thread CSV lap recorder + helpers.

Mirrors the DriveLogger pattern in logger.py (daemon thread, _stop Event, _lock,
immediate-error surface) but polls an injected poll_fn() instead of a UDP socket,
integrates distance from speed, and writes the full RICH_FIELDS column set.

Usage (inside session.py):
    recorder = RichLapRecorder(logs_dir)
    recorder.start(poll_fn, hz=30.0)   # non-blocking
    recorder.stop()                    # join ≤3s, returns dict with path+stats
"""
from __future__ import annotations

import csv
import math
import os
import tempfile
import threading
import time
from typing import Callable

RICH_FIELDS = [
    "t", "dist", "speed", "posx", "posy", "posz", "heading",
    "gx", "gy", "gz",
    "rpm", "gear", "throttle", "brake", "brakeF", "brakeR",
    "steering", "steering_input", "clutch", "boost", "wheelspeed",
    "abs_active", "tcs_active", "esc_active",
]


class RichLapRecorder:
    """Records rich per-lap telemetry to a CSV from a background daemon thread.

    poll_fn() is called each tick; it must return a dict mapping channel names
    to values (a subset of RICH_FIELDS, WITHOUT 't' and 'dist' — those are
    synthesised here).  poll_fn() may raise; the first exception is captured in
    _poll_error, the loop stops, and the error is surfaced in status()/stop().
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
        self.started_at: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while the background recording thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, poll_fn: Callable[[], dict],
              hz: float = 30.0,
              out: str | None = None) -> dict:
        """Spawn the daemon recording thread.

        Args:
            poll_fn: Callable that returns a channel→value dict each tick.
            hz:      Sample rate in Hz (default 30).
            out:     Override output CSV path; auto-generated if None.

        Returns:
            {"ok": True, "logging": True, "path": ..., "hz": ...}
            or {"ok": False, "error": ...} on failure.
        """
        with self._lock:
            if self.running:
                return {"ok": False, "error": "already logging", "path": self.path}
            try:
                os.makedirs(self._logs_dir, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": "cannot create logs_dir: %r" % exc}
            self.path = out or os.path.join(
                self._logs_dir, "lap_%d.csv" % int(time.time())
            )
            self._stop.clear()
            self._poll_error = None
            self.samples = 0
            self.started_at = time.time()
            self._thread = threading.Thread(
                target=self._run, args=(poll_fn, hz), daemon=True
            )
            self._thread.start()

        # Give the thread a moment to open the file / hit an immediate error.
        time.sleep(0.05)
        if self._poll_error:
            return {"ok": False, "error": self._poll_error}
        return {
            "ok": True,
            "logging": True,
            "path": self.path,
            "hz": hz,
        }

    def status(self) -> dict:
        """Return current recorder state (safe to call from any thread)."""
        elapsed = (
            round(time.time() - self.started_at, 1) if self.started_at else None
        )
        result: dict = {
            "ok": True,
            "logging": self.running,
            "path": self.path,
            "samples": self.samples,
            "elapsed_s": elapsed,
        }
        if self._poll_error:
            result["poll_error"] = self._poll_error
        return result

    def stop(self) -> dict:
        """Signal the thread to stop and wait up to 3 s for it to finish.

        Returns:
            {"ok": True, "stopped": True, "path": ..., "samples": ..., "duration_s": ...}
            or {"ok": False, "error": ...}.
        """
        with self._lock:
            was_running = self.running
            path = self.path
            started_at = self.started_at
            if was_running:
                self._stop.set()

        if was_running and self._thread is not None:
            self._thread.join(timeout=3.0)

        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "no active recording or file missing"}

        duration = (
            round(time.time() - started_at, 1) if started_at else None
        )
        result: dict = {
            "ok": True,
            "stopped": True,
            "path": path,
            "samples": self.samples,
            "duration_s": duration,
        }
        if self._poll_error:
            result["poll_error"] = self._poll_error
        return result

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run(self, poll_fn: Callable[[], dict], hz: float) -> None:
        """Main recording loop — runs entirely in a daemon thread."""
        interval = 1.0 / max(hz, 0.1)
        t0 = time.time()
        dist = 0.0
        prev_t = t0

        try:
            with open(self.path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(self._fields)

                while not self._stop.is_set():
                    now = time.time()
                    try:
                        row_data = poll_fn()
                    except Exception as exc:  # noqa: BLE001
                        self._poll_error = repr(exc)
                        self._stop.set()
                        break
                    # A non-dict return (e.g. None) would make row_data.get below
                    # raise AttributeError OUTSIDE the guard above, silently killing
                    # the thread. Validate so the error is captured + surfaced.
                    if not isinstance(row_data, dict):
                        self._poll_error = (
                            "poll_fn returned %s, expected dict"
                            % type(row_data).__name__
                        )
                        self._stop.set()
                        break

                    t = now - t0
                    dt = now - prev_t
                    prev_t = now

                    # Integrate distance: speed channel is in m/s per contract.
                    try:
                        speed_ms = float(row_data.get("speed", 0) or 0)
                    except (TypeError, ValueError):
                        speed_ms = 0.0
                    if dt > 0:
                        dist += speed_ms * dt

                    # Build the ordered row; missing fields → empty string.
                    csv_row: list = []
                    for field in self._fields:
                        if field == "t":
                            csv_row.append(round(t, 4))
                        elif field == "dist":
                            csv_row.append(round(dist, 3))
                        else:
                            val = row_data.get(field, "")
                            csv_row.append("" if val is None else val)

                    writer.writerow(csv_row)
                    self.samples += 1

                    if self.samples % 10 == 0:
                        fh.flush()

                    # Release lock before sleeping — CRITICAL per R5 notes.
                    sleep_until = now + interval
                    remaining = sleep_until - time.time()
                    if remaining > 0:
                        time.sleep(remaining)

                fh.flush()
        except OSError as exc:
            self._poll_error = "file error: %r" % exc
            self._stop.set()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def latest_lap(logs_dir: str) -> str | None:
    """Return the newest lap_*.csv path in *logs_dir*, or None if none exist."""
    if not os.path.isdir(logs_dir):
        return None
    csvs = [
        os.path.join(logs_dir, f)
        for f in os.listdir(logs_dir)
        if f.startswith("lap_") and f.endswith(".csv")
    ]
    return max(csvs, key=os.path.getmtime) if csvs else None


def read_lap_csv(path: str) -> list[dict]:
    """Parse a lap CSV written by RichLapRecorder.

    Returns a list of row dicts with field→float|None; missing or
    non-numeric cells become None.  Tolerant of extra/missing columns.
    """
    rows: list[dict] = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
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


# ---------------------------------------------------------------------------
# Offline selftest
# ---------------------------------------------------------------------------

def _selftest() -> None:
    """Feed a synthetic circular-motion poll_fn for ~0.4 s; assert invariants."""
    import tempfile  # noqa: PLC0415  (stdlib re-import is fine in tests)

    tmpdir = tempfile.mkdtemp(prefix="lap_selftest_")

    # Synthetic poll_fn: vehicle moves in a circle at ~20 m/s.
    radius = 10.0
    angular_v = 20.0 / radius  # rad/s
    _start = time.time()

    def poll_fn() -> dict:
        elapsed = time.time() - _start
        theta = angular_v * elapsed
        vx = -math.sin(theta) * 20.0
        vy = math.cos(theta) * 20.0
        return {
            "speed": 20.0,                      # m/s
            "posx": radius * math.cos(theta),
            "posy": radius * math.sin(theta),
            "posz": 0.0,
            "heading": math.degrees(theta) % 360.0,
            "gx": 0.0,
            "gy": (20.0 ** 2) / radius / 9.80665,  # centripetal in g
            "gz": 1.0,
            "rpm": 3000.0,
            "gear": 3,
            "throttle": 0.5,
            "brake": 0.0,
            "brakeF": 0.0,
            "brakeR": 0.0,
            "steering": 0.1,
            "steering_input": 0.1,
            "clutch": 0.0,
            "boost": 0.0,
            "wheelspeed": 20.0,
            "abs_active": 0,
            "tcs_active": 0,
            "esc_active": 0,
        }

    rec = RichLapRecorder(tmpdir)
    result = rec.start(poll_fn, hz=30.0)
    assert result.get("ok"), "start() failed: %r" % result
    assert result.get("logging") is True, "logging not True: %r" % result
    assert result.get("path"), "no path in result"

    # Record for ~0.4 s (should give ≥12 samples at 30 Hz).
    time.sleep(0.45)

    stop_result = rec.stop()
    assert stop_result.get("ok"), "stop() failed: %r" % stop_result
    assert stop_result.get("stopped") is True
    path = stop_result["path"]

    # --- Assert CSV exists with correct header ---
    assert os.path.isfile(path), "CSV file not created at %s" % path
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
    assert header == RICH_FIELDS, (
        "Header mismatch.\n  got : %r\n  want: %r" % (header, RICH_FIELDS)
    )

    # --- Assert ≥3 data rows ---
    rows = read_lap_csv(path)
    assert len(rows) >= 3, "Expected ≥3 rows, got %d" % len(rows)

    # --- Assert 't' is increasing ---
    ts = [r["t"] for r in rows if r.get("t") is not None]
    assert all(b > a for a, b in zip(ts, ts[1:])), "timestamps not monotone: %r" % ts[:5]

    # --- Assert dist is increasing ---
    dists = [r["dist"] for r in rows if r.get("dist") is not None]
    assert len(dists) >= 3, "Not enough dist values"
    assert all(b > a for a, b in zip(dists, dists[1:])), (
        "dist not monotone: %r" % dists[:5]
    )

    # --- Assert read_lap_csv round-trips numeric values ---
    assert all(isinstance(r["speed"], float) for r in rows if r.get("speed") is not None), (
        "speed values are not float"
    )

    # --- latest_lap ---
    found = latest_lap(tmpdir)
    assert found == path, "latest_lap returned %r, expected %r" % (found, path)

    print("SELFTEST OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
