from beamng_mcp.analysis.ingest import samples_from_rows
from beamng_mcp.analysis.model import sample_from_row


def test_sample_from_row_coerces():
    s = sample_from_row({"t": "1.5", "speed": 20.0, "gy": "1.1", "gear": 3})
    assert s.t == 1.5
    assert s.speed == 20.0
    assert round(s.speed_kmh, 1) == 72.0
    assert s.gy == 1.1
    assert s.gear == 3.0


def test_missing_fields_default_safely():
    s = sample_from_row({})  # totally empty row
    assert s.speed == 0.0
    assert s.steering is None  # optional channels -> None, not 0
    assert s.combined_g == 0.0


def test_combined_g():
    s = sample_from_row({"gx": 3.0, "gy": 4.0})
    assert s.combined_g == 5.0  # 3-4-5


def test_samples_from_rows_length():
    rows = [{"t": i, "speed": 10} for i in range(5)]
    assert len(samples_from_rows(rows)) == 5
