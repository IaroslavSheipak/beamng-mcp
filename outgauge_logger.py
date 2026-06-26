"""outgauge_logger.py — record BeamNG OutGauge UDP telemetry to a CSV.

Robust historical-telemetry path: OutGauge does NOT use the per-vehicle tech
socket, so it is immune to the post-respawn priming bug. Enable it in-game first:
Options > Other > Protocols > OutGauge, IP 127.0.0.1, port 4444, blank ID.

Usage:
    python outgauge_logger.py --seconds 6          # short verify
    python outgauge_logger.py                       # run until killed (background)
"""
import argparse
import csv
import os
import signal
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import outgauge  # noqa: E402

FIELDS = ["t", "speed_kmh", "rpm", "gear", "throttle", "brake", "clutch",
          "fuel", "engTemp"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4444)
    ap.add_argument("--seconds", type=float, default=0.0)  # 0 = until killed
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    out = args.out or os.path.join(here, "logs", "drive_%d.csv" % int(time.time()))
    os.makedirs(os.path.dirname(out), exist_ok=True)

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))
    signal.signal(signal.SIGINT, lambda *a: stop.__setitem__("v", True))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.ip, args.port))
    sock.settimeout(1.0)

    f = open(out, "w", newline="")
    w = csv.writer(f)
    w.writerow(FIELDS)

    print("listening on %s:%d -> %s" % (args.ip, args.port, out), flush=True)
    t0 = time.time()
    n = 0
    first = True
    deadline = (t0 + args.seconds) if args.seconds > 0 else None
    idle_warned = False
    try:
        while not stop["v"]:
            if deadline and time.time() >= deadline:
                break
            try:
                data, _ = sock.recvfrom(512)
            except socket.timeout:
                if n == 0 and not idle_warned and time.time() - t0 > 3:
                    print("(no packets yet — in a vehicle? OutGauge on, port 4444?)",
                          flush=True)
                    idle_warned = True
                continue
            try:
                p = outgauge.parse(data)
            except Exception as e:  # noqa: BLE001
                print("parse error (%d bytes): %r" % (len(data), e), flush=True)
                continue
            w.writerow([round(time.time() - t0, 3)] + [p.get(k) for k in FIELDS[1:]])
            n += 1
            if first:
                first = False
                print("OK first packet: speed=%.1f km/h rpm=%.0f gear=%s fuel=%.2f"
                      % (p.get("speed_kmh", 0), p.get("rpm", 0), p.get("gear"),
                         p.get("fuel", 0)), flush=True)
            if n % 10 == 0:
                f.flush()
    finally:
        f.flush()
        f.close()
        sock.close()
        print("STOPPED: %d samples over %.1fs -> %s"
              % (n, time.time() - t0, out), flush=True)


if __name__ == "__main__":
    main()
