from beamng_mcp.analysis import validity
from beamng_mcp.analysis.model import sample_from_row


def _lap(n=100, dist=1100.0, speed=16.0, stopped_frac=0.0):
    """Build a synthetic lap: n samples, total `dist`, steady `speed` (m/s),
    with `stopped_frac` of samples at 0 km/h."""
    n_stop = int(n * stopped_frac)
    out = []
    for i in range(n):
        v = 0.0 if i < n_stop else speed
        out.append(sample_from_row({"t": i * 0.1, "dist": dist * i / (n - 1), "speed": v}))
    return out


def test_clean_full_lap_is_valid():
    v = validity.assess(_lap(dist=1100.0, speed=16.0))
    assert v.valid is True
    assert v.reasons == []
    assert 1090 < v.distance_m < 1110
    assert v.stopped is False


def test_short_lap_rejected_on_distance():
    v = validity.assess(_lap(dist=150.0))
    assert v.valid is False
    assert any("distance" in r for r in v.reasons)


def test_stopped_lap_rejected():
    # The lap-4/5/6 case: long enough, but the car stopped in it.
    v = validity.assess(_lap(dist=1100.0, stopped_frac=0.15))
    assert v.valid is False
    assert v.stopped is True
    assert any("stopped" in r for r in v.reasons)


def test_empty_lap_invalid():
    v = validity.assess([])
    assert v.valid is False
    assert v.n_samples == 0


def test_brief_dip_not_flagged_as_stop():
    # A single near-zero sample (e.g. one noisy reading) is under the 5% gate.
    v = validity.assess(_lap(n=100, dist=1100.0, stopped_frac=0.01))
    assert v.stopped is False
