"""outgauge.py — pure-stdlib OutGauge UDP telemetry reader.

NO beamngpy and NO license required. OutGauge is the license-free dashboard
protocol (LFS-compatible) that BeamNG.drive emits when enabled under
Options > Other > Protocols. Packets are 92 bytes (no OutGauge ID configured)
or 96 bytes (a trailing int32 ID is configured).
"""

from __future__ import annotations

import socket
import struct

# 92-byte layout (little-endian):
#   I    time_ms        (unsigned int, ms)
#   4s   car            (car name)
#   H    flags          (OG_* bitmask)
#   b    gear           (Reverse=0, Neutral=1, First=2, ...)
#   b    plid           (player id)
#   7f   speed,rpm,turbo,engTemp,fuel,oilPressure,oilTemp
#   I    dashLights     (available warning-light bitmask)
#   I    showLights     (lit warning-light bitmask)
#   3f   throttle,brake,clutch
#   16s  display1
#   16s  display2
# size check: 4+4+2+1+1+(7*4)+4+4+(3*4)+16+16 = 92
FMT_92 = "<I4sHbb7fII3f16s16s"
FMT_96 = FMT_92 + "i"  # trailing OutGauge ID (int32)

FIELDS = [
    "time_ms",
    "car",
    "flags",
    "gear",
    "plid",
    "speed",
    "rpm",
    "turbo",
    "engTemp",
    "fuel",
    "oilPressure",
    "oilTemp",
    "dashLights",
    "showLights",
    "throttle",
    "brake",
    "clutch",
    "display1",
    "display2",
]

# Dash-light bit masks.
DL_MASKS = {
    "shift": 1,
    "fullbeam": 2,
    "handbrake": 4,
    "pitspeed": 8,
    "tc": 16,
    "signal_l": 32,
    "signal_r": 64,
    "signal_any": 128,
    "oilwarn": 256,
    "battery": 512,
    "abs": 1024,
    "spare": 2048,
}

# OutGauge flag (OG_*) masks.
OG_MASKS = {
    "shift": 0x1,
    "ctrl": 0x2,
    "turbo": 0x2000,
    "km": 0x4000,
    "bar": 0x8000,
}


def _decode_str(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").rstrip()


def _expand(mask: int, table: dict[str, int]) -> dict[str, bool]:
    return {name: bool(mask & bit) for name, bit in table.items()}


def parse(data: bytes) -> dict:
    """Parse a 92- or 96-byte OutGauge packet into a dict of named fields."""
    n = len(data)
    if n == 96:
        fmt, has_id = FMT_96, True
    elif n == 92:
        fmt, has_id = FMT_92, False
    else:
        raise ValueError(f"unexpected OutGauge packet length: {n} (want 92 or 96)")

    values = struct.unpack(fmt, data)
    out = dict(zip(FIELDS, values[: len(FIELDS)]))
    if has_id:
        out["id"] = values[len(FIELDS)]

    out["car"] = _decode_str(out["car"])
    out["display1"] = _decode_str(out["display1"])
    out["display2"] = _decode_str(out["display2"])

    out["speed_kmh"] = out["speed"] * 3.6
    # gear: Reverse=0, Neutral=1, First=2, ... (per spec section D: forward_gear = gear - 2)
    out["forward_gear"] = out["gear"] - 2

    out["dashLights"] = _expand(out["dashLights"], DL_MASKS)
    out["showLights"] = _expand(out["showLights"], DL_MASKS)
    out["flags"] = _expand(out["flags"], OG_MASKS)
    return out


def listen_once(ip: str = "127.0.0.1", port: int = 4444, timeout: float = 2.0) -> dict | None:
    """Bind a UDP socket, read ONE OutGauge packet, parse it. None on timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, port))
        sock.settimeout(timeout)
        try:
            data, _addr = sock.recvfrom(96)
        except socket.timeout:
            return None
        return parse(data)
    finally:
        sock.close()
