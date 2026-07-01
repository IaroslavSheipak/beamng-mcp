import struct

from beamng_mcp.sim import outgauge


def _packet(*, with_id: bool, gear: int = 2, speed: float = 10.0, rpm: float = 3000.0):
    fields = (
        1000,            # time_ms
        b"sun",          # car (4s, auto null-padded)
        0x4000,          # flags: km bit
        gear,            # gear (2 = First)
        0,               # plid
        speed, rpm, 0.0, 80.0, 0.5, 0.0, 90.0,   # 7 floats
        16,              # dashLights: tc bit
        0,               # showLights
        0.5, 0.0, 0.0,   # throttle, brake, clutch
        b"D1",           # display1 (16s)
        b"",             # display2 (16s)
    )
    if with_id:
        return struct.pack(outgauge.FMT_96, *fields, 7)
    return struct.pack(outgauge.FMT_92, *fields)


def test_parse_92():
    pkt = _packet(with_id=False)
    assert len(pkt) == 92
    d = outgauge.parse(pkt)
    assert d["car"] == "sun"
    assert d["rpm"] == 3000.0
    assert round(d["speed_kmh"], 1) == 36.0      # 10 m/s -> 36 km/h
    assert d["gear"] == 2                          # raw byte
    assert d["forward_gear"] == 1                  # First = 1 (the v1 gear-2 fix)
    assert d["flags"]["km"] is True
    assert d["dashLights"]["tc"] is True
    assert "id" not in d


def test_parse_96_has_id():
    pkt = _packet(with_id=True)
    assert len(pkt) == 96
    d = outgauge.parse(pkt)
    assert d["id"] == 7


def test_gear_mapping():
    # Reverse=0 -> -1, Neutral=1 -> 0, First=2 -> 1
    assert outgauge.parse(_packet(with_id=False, gear=0))["forward_gear"] == -1
    assert outgauge.parse(_packet(with_id=False, gear=1))["forward_gear"] == 0
    assert outgauge.parse(_packet(with_id=False, gear=3))["forward_gear"] == 2


def test_bad_length_raises():
    try:
        outgauge.parse(b"\x00" * 40)
    except ValueError as exc:
        assert "92 or 96" in str(exc)
    else:
        raise AssertionError("expected ValueError on bad packet length")
