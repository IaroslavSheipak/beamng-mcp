"""Recorder tests — ported from v1's lap_telemetry selftest, as real pytest."""

import math
import time

from beamng_mcp.timing.recorder import RICH_FIELDS, RichLapRecorder, latest_lap, read_lap_csv


def _circular_poll():
    """Synthetic poll_fn: the car circles at ~20 m/s (so dist increases steadily)."""
    radius = 10.0
    angular_v = 20.0 / radius
    start = time.monotonic()

    def poll_fn() -> dict:
        theta = angular_v * (time.monotonic() - start)
        return {
            "speed": 20.0,
            "posx": radius * math.cos(theta),
            "posy": radius * math.sin(theta),
            "posz": 0.0,
            "heading": math.degrees(theta) % 360.0,
            "gy": (20.0**2) / radius / 9.80665,
            "gz": 1.0,
            "rpm": 3000.0,
            "gear": 3,
        }

    return poll_fn


def test_records_monotone_time_and_distance(tmp_path):
    rec = RichLapRecorder(str(tmp_path))
    started = rec.start(_circular_poll(), hz=50.0)
    assert started["ok"] and started["logging"]
    time.sleep(0.3)
    stopped = rec.stop()
    assert stopped["ok"] and stopped["stopped"]

    rows = read_lap_csv(stopped["path"])
    assert len(rows) >= 3
    ts = [r["t"] for r in rows if r.get("t") is not None]
    dists = [r["dist"] for r in rows if r.get("dist") is not None]
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))       # time strictly increases
    assert all(b > a for a, b in zip(dists, dists[1:], strict=False))  # distance integrates up
    assert all(isinstance(r["speed"], float) for r in rows if r.get("speed") is not None)


def test_header_and_latest_lap(tmp_path):
    rec = RichLapRecorder(str(tmp_path))
    rec.start(_circular_poll(), hz=50.0)
    time.sleep(0.15)
    res = rec.stop()
    with open(res["path"], encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
    assert header == RICH_FIELDS
    assert latest_lap(str(tmp_path)) == res["path"]


def test_poll_returning_non_dict_is_surfaced(tmp_path):
    rec = RichLapRecorder(str(tmp_path))
    rec.start(lambda: None, hz=50.0)  # poll_fn returns None -> captured, not a crash
    time.sleep(0.1)
    st = rec.stop()
    assert "poll_error" in st or st.get("ok") is False


def test_double_start_is_rejected(tmp_path):
    rec = RichLapRecorder(str(tmp_path))
    rec.start(_circular_poll(), hz=50.0)
    second = rec.start(_circular_poll(), hz=50.0)
    assert second["ok"] is False
    rec.stop()
