"""MotionSim (BNG1) UDP parser — license-free per-frame vehicle DYNAMICS.

The motion-platform feed BeamNG.drive emits alongside OutGauge (Options > Other >
Protocols). No BeamNG.tech license and no per-vehicle tech socket — just the
player car's physics each frame: world position/velocity, gravity-EXCLUDED
acceleration, the up vector + euler angles, and angular velocity/acceleration.

Why this matters: unlike the classic GForces sensor (gravity-inclusive, axis-
swapped, sign-guessed) this gives true yaw RATE (``ang_vel`` z) and clean accel
directly — the inputs a trustworthy understeer/slip analysis needs.

Endianness is assumed little-endian (BeamNG is x86 + OutGauge is LE); the 4-byte
``BNG1`` signature confirms a good parse. Layout per the official protocol docs:
https://documentation.beamng.com/modding/protocols/
"""

from __future__ import annotations

import socket
import struct

SIGNATURE = b"BNG1"
# format(4s) + 21 floats: pos(3) vel(3) acc(3) up(3) anglePos(3) angleVel(3) angleAcc(3)
FMT = "<4s21f"
SIZE = struct.calcsize(FMT)  # 88


def parse(data: bytes) -> dict:
    """Parse a MotionSim ``BNG1`` packet into grouped vec3s. Raises ``ValueError`` on
    a short packet or a bad signature.

    ``angle``/``ang_vel``/``ang_acc`` are ``(roll, pitch, yaw)``; ``ang_vel[2]`` is
    the yaw rate. ``acc`` excludes gravity.
    """
    if len(data) < SIZE:
        raise ValueError(f"short MotionSim packet: {len(data)} bytes (want {SIZE})")
    vals = struct.unpack(FMT, data[:SIZE])
    if vals[0] != SIGNATURE:
        raise ValueError(f"bad MotionSim signature {vals[0]!r} (want b'BNG1')")
    f = vals[1:]

    def vec3(i: int) -> tuple[float, float, float]:
        return (f[i], f[i + 1], f[i + 2])

    return {
        "pos": vec3(0),       # world position
        "vel": vec3(3),       # world velocity
        "acc": vec3(6),       # acceleration, GRAVITY EXCLUDED
        "up": vec3(9),        # up vector (vehicle orientation)
        "angle": vec3(12),    # roll, pitch, yaw position
        "ang_vel": vec3(15),  # roll, pitch, yaw rate  (yaw rate = [2])
        "ang_acc": vec3(18),  # roll, pitch, yaw acceleration
    }


def listen_once(ip: str = "127.0.0.1", port: int = 4445, timeout: float = 2.0) -> dict | None:
    """Bind a UDP socket, read ONE MotionSim packet, parse it. None on timeout.

    ``port`` must match the in-game MotionSim setting (OutGauge defaults to 4444;
    set the MotionSim port in the same Protocols menu). Always binds loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, port))
        sock.settimeout(timeout)
        try:
            data, _addr = sock.recvfrom(256)
        except socket.timeout:
            return None
        return parse(data)
    finally:
        sock.close()
