"""compare: the tune -> re-drive -> confirm diff. Deltas are candidate - baseline;
verdicts are only claimed when both laps are valid and plausibly the same circuit."""

from beamng_mcp.analysis import compare, report
from beamng_mcp.analysis.model import sample_from_row


def _lap(n=120, dist=1000.0, speed=16.6, dt=0.1, stopped_frac=0.0):
    """Same synthetic shape as test_report's: alternating corner/straight blocks.
    ``dt`` scales lap duration; corners land at identical dist markers, so two
    laps built with the same n/dist pair match corner-for-corner."""
    n_stop = int(n * stopped_frac)
    out = []
    for i in range(n):
        v = 0.0 if i < n_stop else speed
        gy = 0.5 if (i // 10) % 2 == 0 else 0.0
        gx = -0.5 if (i % 20 == 0) else 0.1
        out.append(sample_from_row({
            "t": i * dt, "dist": dist * i / (n - 1), "speed": v,
            "gy": gy, "gx": gx, "heading": 0.0, "posx": float(i), "posy": 0.0,
        }))
    return out


def _report(**kw):
    return report.analyze_samples(_lap(**kw))


def test_faster_candidate_gets_the_verdict():
    out = compare.compare_reports(_report(dt=0.1), _report(dt=0.09))
    assert out["ok"] is True
    assert out["lap_time"]["delta"] < 0
    assert "FASTER" in out["lap_time"]["verdict"]
    assert any("FASTER" in v for v in out["verdict"])
    assert not out["warnings"]


def test_deltas_are_candidate_minus_baseline():
    out = compare.compare_reports(_report(speed=16.6), _report(speed=20.0))
    d = out["deltas"]["speed_max_kmh"]
    assert d["baseline"] < d["candidate"]
    assert d["delta"] > 0


def test_invalid_lap_kills_the_time_verdict_and_warns():
    out = compare.compare_reports(_report(), _report(stopped_frac=0.15))
    assert out["ok"] is True
    assert out["candidate"]["valid"] is False
    assert out["lap_time"]["verdict"] is None
    assert any("INVALID" in w for w in out["warnings"])


def test_distance_mismatch_means_not_the_same_lap():
    out = compare.compare_reports(_report(dist=1000.0), _report(dist=300.0))
    assert out["lap_time"]["verdict"] is None
    assert any("same circuit" in w for w in out["warnings"])
    assert out["corners"]["matched"] == []


def test_corners_match_by_distance_marker():
    out = compare.compare_reports(_report(speed=16.6), _report(speed=20.0))
    matched = out["corners"]["matched"]
    assert matched, "identical dist markers must pair up"
    assert out["corners"]["baseline_only"] == 0
    assert out["corners"]["candidate_only"] == 0
    # candidate carries more speed through every matched corner
    assert all(c["v_min_delta_kmh"] > 0 for c in matched)
    assert out["corners"]["avg_v_min_delta_kmh"] > 0


def test_compare_lap_files_roundtrip(tmp_path):
    header = "t,dist,speed,gy,gx,heading,posx,posy\n"

    def _write(name, dt):
        rows = [header]
        for i in range(120):
            rows.append(
                f"{i * dt},{1000.0 * i / 119},16.6,"
                f"{0.5 if (i // 10) % 2 == 0 else 0.0},0.1,0.0,{float(i)},0.0\n")
        p = tmp_path / name
        p.write_text("".join(rows), encoding="utf-8")
        return str(p)

    out = compare.compare_lap_files(_write("lap_a.csv", 0.1), _write("lap_b.csv", 0.09))
    assert out["ok"] is True
    assert "FASTER" in out["lap_time"]["verdict"]


def test_unreadable_file_is_a_clean_error(tmp_path):
    empty = tmp_path / "lap_empty.csv"
    empty.write_text("", encoding="utf-8")
    out = compare.compare_lap_files(str(empty), str(tmp_path / "lap_missing.csv"))
    assert out["ok"] is False
    assert "baseline" in out["error"]
