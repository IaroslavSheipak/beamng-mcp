"""DriveLogger tests — ported from v1's logger.py (no test file existed for it;
this brings it to the same real-socket-roundtrip rigor as test_outgauge.py)."""

import socket
import struct
import time

from beamng_mcp.sim import drivelog, outgauge


def _packet(speed=10.0, rpm=3000.0, gear=2, throttle=0.0, brake=0.0, fuel=0.5, eng_temp=90.0):
    fields = (
        1000, b"sun", 0, gear, 0,
        speed, rpm, 0.0, eng_temp, fuel, 0.0, 90.0,
        0, 0,
        throttle, brake, 0.0,
        b"D1", b"",
    )
    return struct.pack(outgauge.FMT_92, *fields)


def _send(port, **kwargs):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(_packet(**kwargs), ("127.0.0.1", port))
    finally:
        sock.close()


def test_logs_real_udp_packets(tmp_path):
    logger = drivelog.DriveLogger(str(tmp_path))
    PORT = 41444
    started = logger.start(port=PORT)
    assert started["ok"] and started["logging"]
    for i in range(5):
        _send(PORT, speed=10.0 + i, rpm=3000.0 + i * 100)
        time.sleep(0.05)
    stopped = logger.stop()
    assert stopped["ok"] and stopped["stopped"]
    assert stopped["summary"]["ok"]
    assert stopped["summary"]["samples"] >= 1


def test_status_shape(tmp_path):
    logger = drivelog.DriveLogger(str(tmp_path))
    assert logger.status() == {"ok": True, "logging": False, "path": None,
                                "samples": 0, "elapsed_s": None}


def test_stop_without_start_is_graceful(tmp_path):
    logger = drivelog.DriveLogger(str(tmp_path))
    res = logger.stop()
    assert res["ok"] is False


def test_double_start_is_rejected(tmp_path):
    logger = drivelog.DriveLogger(str(tmp_path))
    started = logger.start(port=41445)
    try:
        assert started["ok"]
        second = logger.start(port=41445)
        assert second["ok"] is False
    finally:
        logger.stop()


def _write_drive_csv(path, rows):
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(drivelog.FIELDS)
        for r in rows:
            w.writerow(r)


def test_summarize_csv_basics(tmp_path):
    path = tmp_path / "drive_1.csv"
    rows = [[i * 0.1, i * 5.0, 2000 + i * 100, 2, 1.0, 0.0, 0.0, 0.5 - i * 0.001, 90]
            for i in range(20)]
    _write_drive_csv(path, rows)
    s = drivelog.summarize_csv(str(path))
    assert s["ok"] and s["samples"] == 20
    assert s["top_speed_kmh"] == max(r[1] for r in rows)
    assert s["throttle_pct"] == 100  # full throttle every row


def test_summarize_csv_missing_file():
    s = drivelog.summarize_csv("/nonexistent/path.csv")
    assert s["ok"] is False


def test_render_summary_renders_text(tmp_path):
    path = tmp_path / "drive_1.csv"
    rows = [[i * 0.1, 50.0, 3000, 3, 0.5, 0.0, 0.0, 0.5, 90] for i in range(10)]
    _write_drive_csv(path, rows)
    s = drivelog.summarize_csv(str(path))
    text = drivelog.render_summary(s)
    assert "DRIVE SUMMARY" in text and "top speed" in text


def test_latest_log_prefers_drive_prefix(tmp_path):
    # A lap_*.csv in the same dir must NOT be picked up by latest_log.
    (tmp_path / "lap_1.csv").write_text("x")
    assert drivelog.latest_log(str(tmp_path)) is None
    (tmp_path / "drive_1.csv").write_text("x")
    assert drivelog.latest_log(str(tmp_path)) == str(tmp_path / "drive_1.csv")


def test_gname_mapping():
    assert drivelog._gname(0) == "R"
    assert drivelog._gname(1) == "N"
    assert drivelog._gname(3) == "2"
    assert drivelog._gname("bogus") == "bogus"
