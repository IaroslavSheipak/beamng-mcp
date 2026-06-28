import struct

import pytest

from beamng_mcp.sim import motionsim


def _packet(sig=b"BNG1"):
    # 21 floats valued 0..20 so each group's indices are obvious.
    return struct.pack(motionsim.FMT, sig, *[float(i) for i in range(21)])


def test_size_is_88():
    assert motionsim.SIZE == 88


def test_parse_groups_vec3s():
    d = motionsim.parse(_packet())
    assert d["pos"] == (0.0, 1.0, 2.0)
    assert d["vel"] == (3.0, 4.0, 5.0)
    assert d["acc"] == (6.0, 7.0, 8.0)        # gravity-excluded
    assert d["up"] == (9.0, 10.0, 11.0)
    assert d["angle"] == (12.0, 13.0, 14.0)
    assert d["ang_vel"] == (15.0, 16.0, 17.0)
    assert d["ang_acc"] == (18.0, 19.0, 20.0)


def test_yaw_rate_is_ang_vel_z():
    d = motionsim.parse(_packet())
    assert d["ang_vel"][2] == 17.0  # the channel that fixes the balance metric


def test_bad_signature_raises():
    with pytest.raises(ValueError):
        motionsim.parse(_packet(sig=b"XXXX"))


def test_short_packet_raises():
    with pytest.raises(ValueError):
        motionsim.parse(b"BNG1" + b"\x00" * 10)
