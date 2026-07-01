"""Assemble the lap report.

Pipeline: ingest -> validity gate -> impact cleaning -> grip/balance/braking/ride/
corners -> symptoms. Validity is surfaced PROMINENTLY: an invalid lap (stopped /
too short / crash-contaminated) still gets metrics, but flagged so it is never
mistaken for a clean hot lap (the live lap-6 mistake).
"""

from __future__ import annotations

from . import balance, braking, cleaning, corners, grip, ride, validity
from .ingest import load_lap, samples_from_rows
from .model import Sample
from .util import mean


def _symptoms(report: dict) -> list[dict]:
    out: list[dict] = []
    b = report["balance"]
    if b.get("understeer_index") is not None and b.get("tendency") not in (None, "neutral", "unknown"):
        out.append({
            "type": b["tendency"],
            "evidence": f"self-calibrated understeer_index {b['understeer_index']:+.2f}",
            "confidence": b.get("confidence", "low"),
        })
    if b.get("peak_slip_deg") is not None and b["peak_slip_deg"] > 8.0:
        out.append({
            "type": "sliding",
            "evidence": f"peak slip angle {b['peak_slip_deg']:.1f} deg",
            "confidence": "high",
        })
    if report["braking"].get("unstable"):
        out.append({
            "type": "brake_instability",
            "evidence": f"yaw {report['braking']['straightline_yaw_instability']:.2f} rad/s under straight braking",
            "confidence": "medium",
        })
    if report["ride"].get("bottoming_events", 0) > 5:
        out.append({
            "type": "bottoming",
            "evidence": f"{report['ride']['bottoming_events']} gz spikes past baseline (proxy)",
            "confidence": "low",
        })
    return out


def analyze_samples(samples: list[Sample]) -> dict:
    """Full report from in-memory samples."""
    if not samples:
        return {"ok": False, "error": "no samples to analyze"}
    v = validity.assess(samples)
    clean = cleaning.detect_impacts(samples)
    cleaned = cleaning.clean_samples(samples, clean)
    yaw = balance.yaw_rates(samples)
    speeds = [s.speed_kmh for s in samples]

    report: dict = {
        "ok": True,
        "valid": v.valid,
        "validity": {
            "valid": v.valid, "distance_m": v.distance_m,
            "stopped": v.stopped, "reasons": v.reasons,
        },
        "samples": len(samples),
        "duration_s": round(samples[-1].t - samples[0].t, 2),
        "distance_m": v.distance_m,
        "speed": {
            "max_kmh": round(max(speeds), 1),
            "avg_kmh": round(mean(speeds), 1),
            "min_kmh": round(min(speeds), 1),
        } if speeds else {},
        "impacts_excluded": clean.n_impacts,
        "grip": grip.grip(cleaned),
        "balance": balance.balance(samples),
        "braking": braking.braking(samples, yaw),
        "ride": ride.ride(samples),
        "corners": corners.corners(samples),
    }
    report["symptoms"] = _symptoms(report)
    if not v.valid:
        report["warning"] = (
            "INVALID LAP — " + "; ".join(v.reasons) + ". Metrics are shown but this is "
            "NOT a clean representative lap; do not compare its time to full laps."
        )
    return report


def analyze_lap(path: str) -> dict:
    """Full report from a recorded lap CSV (the engineer/tool entry point)."""
    samples = load_lap(path)
    if not samples:
        return {"ok": False, "error": f"no usable samples in {path}"}
    out = analyze_samples(samples)
    out["path"] = path
    return out


def analyze_rows(rows: list[dict]) -> dict:
    """Full report from raw CSV row dicts."""
    return analyze_samples(samples_from_rows(rows))
