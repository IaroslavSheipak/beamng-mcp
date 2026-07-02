"""plots: the delta-T math must be exact on synthetic laps, and the renderer
must produce a real PNG without a display."""

import os

from beamng_mcp.analysis import plots
from beamng_mcp.analysis.model import sample_from_row


def _lap(n=200, dist=1000.0, dt=0.1, speed=None, stall_at=None):
    """Synthetic lap with position trace; dist increases linearly, so
    t(d) is exactly linear -> delta-T is analytically known."""
    out = []
    for i in range(n):
        d = dist * i / (n - 1)
        if stall_at is not None and stall_at[0] <= i < stall_at[1]:
            d = dist * stall_at[0] / (n - 1)  # distance frozen (car stopped)
        out.append(sample_from_row({
            "t": i * dt, "dist": d, "speed": speed if speed is not None else 16.6,
            "gy": 0.5 if (i // 10) % 2 == 0 else 0.0, "gx": 0.1,
            "heading": 0.0, "posx": float(i), "posy": float(i % 7),
            "posz": 0.0, "throttle": 0.8, "brake": 0.0,
        }))
    return out


def test_time_over_distance_drops_stalls():
    lap = _lap(stall_at=(50, 60))
    ds, ts = plots.time_over_distance(lap)
    assert all(ds[i] < ds[i + 1] for i in range(len(ds) - 1))  # strictly increasing
    assert len(ds) < 200


def test_delta_time_is_linear_for_uniform_speed_difference():
    # candidate covers the same distance in 90% of the time -> delta at the end
    # = -0.1 * total_time, growing linearly
    a = _lap(dt=0.10)
    b = _lap(dt=0.09)
    out = plots.delta_time(a, b)
    assert not out["warnings"]
    d, delta = out["d"], out["delta"]
    assert delta[0] == 0.0
    total_a = 199 * 0.10
    assert abs(delta[-1] - (-0.1 * total_a)) < 0.05
    mid = len(delta) // 2
    assert abs(delta[mid] - delta[-1] / 2) < 0.05  # linear growth


def test_delta_time_warns_on_distance_mismatch():
    out = plots.delta_time(_lap(dist=1000.0), _lap(dist=500.0))
    assert any("differ" in w for w in out["warnings"])


def test_corner_markers_resolve_positions():
    lap = _lap()
    marks = plots.corner_markers(lap)
    assert marks, "the synthetic lap alternates corners"
    assert all("x" in m and "y" in m and m["v_min_kmh"] > 0 for m in marks)
    assert [m["n"] for m in marks] == list(range(1, len(marks) + 1))


def _write_lap_csv(tmp_path, name, dt):
    header = "t,dist,speed,gy,gx,heading,posx,posy,posz,throttle,brake\n"
    rows = [header]
    for i in range(200):
        rows.append(f"{i * dt},{1000.0 * i / 199},16.6,"
                    f"{0.5 if (i // 10) % 2 == 0 else 0.0},0.1,0.0,"
                    f"{float(i)},{float(i % 7)},0.0,0.8,0.0\n")
    p = tmp_path / name
    p.write_text("".join(rows), encoding="utf-8")
    return str(p)


def test_render_two_lap_debrief_writes_png(tmp_path):
    a = _write_lap_csv(tmp_path, "lap_a.csv", 0.10)
    b = _write_lap_csv(tmp_path, "lap_b.csv", 0.09)
    out = plots.render_debrief(a, b, out_png=str(tmp_path / "debrief.png"))
    assert out["ok"] is True
    assert os.path.getsize(out["png"]) > 20_000  # a real figure, not a stub
    assert out["stats"]["delta_final_s"] < 0     # candidate faster
    assert not out["warnings"]


def test_render_single_lap_debrief(tmp_path):
    a = _write_lap_csv(tmp_path, "lap_a.csv", 0.10)
    out = plots.render_debrief(a, None, out_png=str(tmp_path / "single.png"))
    assert out["ok"] is True and os.path.isfile(out["png"])
    assert "candidate" not in out


def test_render_rejects_empty_file(tmp_path):
    empty = tmp_path / "lap_empty.csv"
    empty.write_text("", encoding="utf-8")
    out = plots.render_debrief(str(empty), None)
    assert out["ok"] is False


def test_latest_debrief_paths_defaults(tmp_path):
    a, b, err = plots.latest_debrief_paths(str(tmp_path), None, None)
    assert err and a is None
    p1 = _write_lap_csv(tmp_path, "lap_1.csv", 0.1)
    os.utime(p1, (1, 1))
    p2 = _write_lap_csv(tmp_path, "lap_2.csv", 0.1)
    a, b, err = plots.latest_debrief_paths(str(tmp_path), None, None)
    assert err is None and (a, b) == (p1, p2)  # older = baseline
