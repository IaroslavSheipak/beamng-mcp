"""Lap-to-lap comparison — closes the tune -> re-drive -> CONFIRM loop.

The engineer loop is drive, diagnose, apply a change, drive again; this module
answers the question the loop exists for: did the change help? Deltas are
``candidate - baseline`` throughout. Validity problems are surfaced loudly
rather than averaged away (comparing a crash lap to a clean lap is
meaningless), and lap-time verdicts are only claimed when both laps are valid
and plausibly the same circuit (distance within tolerance).
"""

from __future__ import annotations

from .ingest import load_lap
from .report import analyze_lap

#: Corners whose distance markers differ by less than this pair up A<->B.
CORNER_MATCH_M = 50.0
#: Lap distances differing by more than this fraction => probably not the same lap.
DIST_MISMATCH = 0.10
#: |understeer_index delta| beyond this is called out as a balance shift.
BALANCE_SHIFT = 0.05
#: |g| deltas beyond this are called out (grip envelope, peak braking).
G_SHIFT = 0.05
#: Average matched-corner v_min delta beyond this (km/h) is called out.
VMIN_SHIFT = 1.0


def _get(report: dict, *keys: str) -> object:
    cur: object = report
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _num(report: dict, *keys: str) -> float | None:
    val = _get(report, *keys)
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _delta(a: float | None, b: float | None, nd: int = 3) -> dict:
    """One compared metric: baseline, candidate, and candidate-minus-baseline
    (delta None when either side is honestly unknown)."""
    out: dict = {"baseline": a, "candidate": b}
    out["delta"] = round(b - a, nd) if a is not None and b is not None else None
    return out


def _match_corners(ca: list[dict], cb: list[dict]) -> tuple[list[dict], int, int]:
    """Greedy nearest-distance pairing of detected corners (same start line =>
    comparable distance markers). Returns (matched, baseline_only, candidate_only)."""
    matched: list[dict] = []
    used_b: set[int] = set()
    for a in ca:
        best_j, best_d = None, CORNER_MATCH_M
        for j, b in enumerate(cb):
            if j in used_b:
                continue
            d = abs(b["dist_m"] - a["dist_m"])
            if d < best_d:
                best_j, best_d = j, d
        if best_j is None:
            continue
        used_b.add(best_j)
        b = cb[best_j]
        matched.append({
            "dist_m": a["dist_m"],
            "direction": a["direction"],
            "v_min_delta_kmh": round(b["v_min_kmh"] - a["v_min_kmh"], 1),
            "peak_lat_g_delta": round(b["peak_lat_g"] - a["peak_lat_g"], 3),
        })
    return matched, len(ca) - len(matched), len(cb) - len(used_b)


def compare_reports(a: dict, b: dict) -> dict:
    """Diff two lap reports (``a`` = baseline / before, ``b`` = candidate / after)."""
    if not (isinstance(a, dict) and a.get("ok")):
        return {"ok": False, "error": "baseline lap did not analyze cleanly"}
    if not (isinstance(b, dict) and b.get("ok")):
        return {"ok": False, "error": "candidate lap did not analyze cleanly"}

    warnings: list[str] = []
    for label, r in (("baseline", a), ("candidate", b)):
        if not r.get("valid"):
            reasons = ", ".join(_get(r, "validity", "reasons") or [])  # type: ignore[arg-type]
            warnings.append(f"{label} lap is INVALID ({reasons}) — treat every delta as suspect")

    dist_a, dist_b = _num(a, "distance_m"), _num(b, "distance_m")
    same_lap = True
    if dist_a and dist_b:
        if abs(dist_b - dist_a) > DIST_MISMATCH * max(dist_a, dist_b):
            same_lap = False
            warnings.append(
                f"lap distances differ by more than {DIST_MISMATCH:.0%} "
                f"({dist_a:.0f} m vs {dist_b:.0f} m) — these may not be the same circuit; "
                "time and corner deltas are not comparable"
            )

    time_a, time_b = _num(a, "duration_s"), _num(b, "duration_s")
    lap_time = _delta(time_a, time_b, 2)
    comparable = same_lap and bool(a.get("valid")) and bool(b.get("valid"))
    if comparable and lap_time["delta"] is not None:
        d = lap_time["delta"]
        lap_time["verdict"] = (
            f"candidate FASTER by {-d:.2f} s" if d < 0
            else f"candidate slower by {d:.2f} s" if d > 0
            else "dead even"
        )
    else:
        lap_time["verdict"] = None

    deltas = {
        "speed_max_kmh": _delta(_num(a, "speed", "max_kmh"), _num(b, "speed", "max_kmh"), 1),
        "speed_avg_kmh": _delta(_num(a, "speed", "avg_kmh"), _num(b, "speed", "avg_kmh"), 1),
        "grip_envelope_g": _delta(_num(a, "grip", "envelope_g"), _num(b, "grip", "envelope_g")),
        "grip_max_lat_g": _delta(_num(a, "grip", "max_lat_g"), _num(b, "grip", "max_lat_g")),
        "pct_time_near_limit": _delta(
            _num(a, "grip", "pct_time_near_limit"), _num(b, "grip", "pct_time_near_limit"), 1),
        "peak_decel_g": _delta(
            _num(a, "braking", "peak_decel_g"), _num(b, "braking", "peak_decel_g")),
        "understeer_index": _delta(
            _num(a, "balance", "understeer_index"), _num(b, "balance", "understeer_index")),
        "peak_slip_deg": _delta(
            _num(a, "balance", "peak_slip_deg"), _num(b, "balance", "peak_slip_deg"), 2),
        "bottoming_events": _delta(
            _num(a, "ride", "bottoming_events"), _num(b, "ride", "bottoming_events"), 0),
    }

    tend_a = _get(a, "balance", "tendency") or "unknown"
    tend_b = _get(b, "balance", "tendency") or "unknown"
    balance_shift = {"baseline": tend_a, "candidate": tend_b}

    matched: list[dict] = []
    only_a = only_b = 0
    avg_vmin: float | None = None
    if same_lap:
        matched, only_a, only_b = _match_corners(a.get("corners") or [], b.get("corners") or [])
        if matched:
            avg_vmin = round(sum(c["v_min_delta_kmh"] for c in matched) / len(matched), 2)

    verdict: list[str] = []
    if lap_time["verdict"]:
        verdict.append(lap_time["verdict"])
    ui = deltas["understeer_index"]["delta"]
    if ui is not None and abs(ui) > BALANCE_SHIFT:
        verdict.append(
            f"balance moved toward {'understeer' if ui > 0 else 'oversteer'} "
            f"(index {ui:+.3f}; {tend_a} -> {tend_b})")
    elif tend_a != tend_b and "unknown" not in (tend_a, tend_b):
        verdict.append(f"balance tendency changed: {tend_a} -> {tend_b}")
    env = deltas["grip_envelope_g"]["delta"]
    if env is not None and abs(env) > G_SHIFT:
        verdict.append(f"grip envelope {'up' if env > 0 else 'down'} {abs(env):.3f} g")
    brk = deltas["peak_decel_g"]["delta"]
    if brk is not None and abs(brk) > G_SHIFT:
        verdict.append(f"peak braking {'up' if brk > 0 else 'down'} {abs(brk):.3f} g")
    if avg_vmin is not None and abs(avg_vmin) > VMIN_SHIFT:
        verdict.append(
            f"carrying {'more' if avg_vmin > 0 else 'less'} corner speed "
            f"({avg_vmin:+.1f} km/h avg across {len(matched)} matched corners)")
    bot = deltas["bottoming_events"]["delta"]
    if bot is not None and abs(bot) > 3:
        verdict.append(
            f"bottoming events {'up' if bot > 0 else 'down'} by {abs(bot):.0f} (gz proxy)")
    if not verdict:
        verdict.append("no clear difference beyond noise on any headline metric")

    return {
        "ok": True,
        "baseline": {"path": a.get("path"), "duration_s": time_a,
                     "distance_m": dist_a, "valid": bool(a.get("valid"))},
        "candidate": {"path": b.get("path"), "duration_s": time_b,
                      "distance_m": dist_b, "valid": bool(b.get("valid"))},
        "lap_time": lap_time,
        "deltas": deltas,
        "balance_tendency": balance_shift,
        "corners": {"matched": matched, "baseline_only": only_a, "candidate_only": only_b,
                    "avg_v_min_delta_kmh": avg_vmin},
        "verdict": verdict,
        "warnings": warnings,
        "note": ("deltas are candidate - baseline; duration_s is recording length, "
                 "which equals lap time only for line-crossing (time-trial/session) laps"),
    }


def compare_lap_files(path_a: str, path_b: str) -> dict:
    """Analyze two recorded lap CSVs and diff them (a = baseline, b = candidate)."""
    if not load_lap(path_a):
        return {"ok": False, "error": f"no usable samples in baseline {path_a}"}
    if not load_lap(path_b):
        return {"ok": False, "error": f"no usable samples in candidate {path_b}"}
    return compare_reports(analyze_lap(path_a), analyze_lap(path_b))
