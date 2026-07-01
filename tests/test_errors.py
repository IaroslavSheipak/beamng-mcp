from beamng_mcp.errors import HINT, NotConnected, err, from_exc, ok


def test_ok():
    assert ok(vid="ego") == {"ok": True, "vid": "ego"}


def test_err_plain():
    r = err("boom", code=3)
    assert r == {"ok": False, "error": "boom", "code": 3}
    assert "hint" not in r


def test_err_with_hint():
    r = err("not connected", hint=True)
    assert r["ok"] is False
    assert r["hint"] == HINT


def test_from_exc_keeps_type():
    r = from_exc(NotConnected("nope"))
    assert r["ok"] is False
    assert "NotConnected" in r["error"]
    assert r["hint"] == HINT


def test_from_exc_no_hint():
    r = from_exc(ValueError("x"), hint=False)
    assert "hint" not in r
