from beamng_mcp.analysis import report
from beamng_mcp.analysis.model import sample_from_row


def _lap(n=120, dist=1000.0, speed=16.6, stopped_frac=0.0, spike=False):
    n_stop = int(n * stopped_frac)
    out = []
    for i in range(n):
        v = 0.0 if i < n_stop else speed
        gy = 0.5 if (i // 10) % 2 == 0 else 0.0   # alternating corner / straight
        gx = -0.5 if (i % 20 == 0) else 0.1
        out.append(sample_from_row({
            "t": i * 0.1, "dist": dist * i / (n - 1), "speed": v,
            "gy": gy, "gx": gx, "heading": 0.0, "posx": float(i), "posy": 0.0,
        }))
    if spike:
        out.insert(50, sample_from_row({"t": 5.05, "dist": 415.0, "speed": speed, "gy": 16.0}))
    return out


def test_clean_lap_is_valid_no_warning():
    r = report.analyze_samples(_lap())
    assert r["ok"] and r["valid"] is True
    assert "warning" not in r
    assert r["grip"]["max_lat_g"] == 0.5
    assert r["speed"]["max_kmh"] > 0


def test_impact_spike_does_not_poison_grip():
    # THE headline integration check: a 16 g wall hit is excluded, so the grip
    # envelope/max reflect real cornering (~0.5 g), not the collision.
    r = report.analyze_samples(_lap(spike=True))
    assert r["impacts_excluded"] >= 1
    assert r["grip"]["max_lat_g"] < 3.0  # NOT 16
    assert r["grip"]["envelope_g"] < 3.0


def test_stopped_lap_is_flagged_invalid():
    r = report.analyze_samples(_lap(stopped_frac=0.15))
    assert r["valid"] is False
    assert "warning" in r
    assert "INVALID LAP" in r["warning"]


def test_empty_lap():
    assert report.analyze_rows([])["ok"] is False
