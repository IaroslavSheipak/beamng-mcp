"""lap_analysis.py — pure-stdlib telemetry metrics from a rich-lap row list.

Turns a list of rich-lap rows (see lap_telemetry.RICH_FIELDS) into setup-actionable,
engineer-grade scalars, implementing the *B-tier* (Electrics/GForces/State-only)
methods from research report R3 — no beamngpy, no numpy, no Lua channels:

  (R3 §1) friction-circle / g-g + grip-utilisation %
  (R3 §2) understeer / balance index per phase from yaw-rate-vs-neutral.
          Yaw rate r = unwrap(Δheading)/Δt (heading is logged); the neutral
          (Ackermann) reference r_neutral = v·δ/L uses δ derived from the
          *normalized* `steering` channel (−1..1, NOT road-wheel degrees), so the
          balance index is RELATIVE/trend-only, never absolute degrees. This is the
          single biggest accuracy caveat (R3 §2 CAVEAT) and is surfaced in notes.
  (R3 §5) braking peak decel + straight-line yaw instability (aggregate; per-wheel
          lockup / bias inference needs a Lua probe and is NOT done here).
  (R3 §6) corner segmentation (|gy| > 0.25 g), per-corner v_min (apex) and
          entry/mid/exit phase tags.
  (R3 §3/§4) suspension travel has NO clean channel on the consumer build, so the
          true travel/damper-velocity histograms degrade to a gz-based bottoming +
          settle PROXY only (documented in notes).

Sign conventions (gx forward+, gy lateral, decel = gx<0; gy>0 = left) are ASSUMED
and should be sign-checked against a known corner — flagged in notes.

`analyze_lap` returns the exact report dict from the implementation contract and
embeds `detect_symptoms(report)`, whose symptom vocabulary
(understeer / oversteer + phase, brake_instability, bottoming) matches what
engineer_kb expects.
"""
from __future__ import annotations

import math

G = 9.80665                  # m/s^2 per g — gx/gy/gz are in g (gravity multiples)

# --- Tunable thresholds (documented; affect segmentation, not raw channels). ---
MAX_STEER_RAD = 0.4          # assumed road-wheel angle at full normalized lock (δ = steering*MAX_STEER_RAD)
CORNER_LAT_G = 0.25          # |gy| above this (g) => "in a corner" (R3 §6)
AX_THR = 0.05                # |gx| (g) considered meaningful long. accel/decel
BRAKE_THR = 0.10             # brake input considered "on the brakes"
THROTTLE_THR = 0.10          # throttle input considered "on the gas"
STRAIGHT_STEER = 0.05        # |steering| below this => effectively straight-line
YAW_INSTAB_RAD = 0.15        # |yaw rate| while straight + braking => instability (rad/s)
MIN_RNEU = 0.05              # ignore balance where |r_neutral| < this (rad/s) — div guard
BALANCE_THR = 0.08           # |balance index| beyond which a phase symptom is raised
GZ_SPIKE_G = 0.8             # gz deviation above baseline (g) flagged as a bottoming spike
SETTLE_REF_G = 0.5           # gz rms (g) at which settle_quality proxy hits 0
DIR_BINS = 12                # g-g envelope direction bins
MAX_DT = 1.0                 # ignore samples spaced further apart than this (s)


def _f(row: dict, key: str) -> float | None:
    """Tolerant float read of `row[key]` — None for missing/blank/unparseable."""
    v = row.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _wrap(a: float) -> float:
    """Wrap an angle delta into (-pi, pi] — the unwrap step for logged heading."""
    return math.atan2(math.sin(a), math.cos(a))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _rms(xs: list[float]) -> float:
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else 0.0


def detect_symptoms(report: dict) -> list[dict]:
    """Build report["symptoms"] from a finished report (callable standalone too).

    Maps balance sign -> phase symptom, gz proxy -> bottoming, straight-line yaw
    -> brake_instability, using the engineer_kb symptom vocabulary:
    understeer / oversteer (+phase ENTRY/MID/EXIT), bottoming, brake_instability.
    """
    if not isinstance(report, dict) or not report.get("ok"):
        return []
    out: list[dict] = []
    bal = report.get("balance") or {}
    for phase, key in (("ENTRY", "entry_index"), ("MID", "mid_index"), ("EXIT", "exit_index")):
        idx = bal.get(key)
        if idx is None:
            continue
        if idx > BALANCE_THR:
            out.append({
                "phase": phase, "symptom": "understeer",
                "evidence": "balance index %+.2f (>%.2f) — car yaws less than steer demands" % (idx, BALANCE_THR),
                "confidence": "medium" if idx > 2 * BALANCE_THR else "low",
            })
        elif idx < -BALANCE_THR:
            out.append({
                "phase": phase, "symptom": "oversteer",
                "evidence": "balance index %+.2f (<%.2f) — car rotates more than steer demands" % (idx, -BALANCE_THR),
                "confidence": "medium" if idx < -2 * BALANCE_THR else "low",
            })
    ride = report.get("ride") or {}
    n_bot = ride.get("bottoming_events") or 0
    if n_bot > 0:
        out.append({
            "phase": "KERB", "symptom": "bottoming",
            "evidence": "%d gz spike(s) past baseline+%.1fg (proxy — no travel channel)" % (n_bot, GZ_SPIKE_G),
            "confidence": "low",
        })
    brk = report.get("braking") or {}
    yaw = brk.get("straightline_yaw_instability") or 0.0
    if yaw > YAW_INSTAB_RAD:
        out.append({
            "phase": "BRAKING", "symptom": "brake_instability",
            "evidence": "yaw rate %.2f rad/s while braking near-straight (>%.2f)" % (yaw, YAW_INSTAB_RAD),
            "confidence": "medium",
        })
    return out


def analyze_lap(rows: list[dict], wheelbase_m: float = 2.6) -> dict:
    """Compute the full B-tier telemetry report from rich-lap rows (R3 §§1,2,5,6).

    `rows` is a list of dicts keyed by lap_telemetry.RICH_FIELDS (values float or
    None). `wheelbase_m` (L) feeds the neutral yaw-rate model. Never raises;
    returns {"ok": False, "error": ...} on degenerate input.
    """
    try:
        return _analyze(rows, wheelbase_m)
    except Exception as exc:  # noqa: BLE001 — server contract: never raise
        return {"ok": False, "error": "analyze_lap failed: %r" % exc,
                "samples": len(rows) if isinstance(rows, list) else 0}


def _analyze(rows: list[dict], L: float) -> dict:
    # ---- 1. Parse rows into clean per-sample arrays (R3 §0). ----------------
    S: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _f(r, "t")
        if t is None:
            continue
        S.append({
            "t": t,
            "v": _f(r, "speed") or 0.0,            # m/s (|vel|, immune to wheelspin)
            "head": _f(r, "heading"),              # rad, may be None
            "gx": _f(r, "gx") or 0.0,              # long g (forward +, decel -)
            "gy": _f(r, "gy") or 0.0,              # lateral g (left +)
            "gz": _f(r, "gz") or 0.0,              # vertical g (~1.0 static)
            "thr": _f(r, "throttle") or 0.0,
            "brk": _f(r, "brake") or 0.0,
            "str": _f(r, "steering") or 0.0,       # normalized -1..1
        })
    n = len(S)
    if n < 3:
        return {"ok": False, "error": "need >=3 timestamped samples, got %d" % n, "samples": n}

    L = L if L and L > 0 else 2.6
    notes: list[str] = []

    # cumulative distance from speed (robust to a missing 'dist' channel).
    cum = [0.0] * n
    for i in range(1, n):
        dt = S[i]["t"] - S[i - 1]["t"]
        cum[i] = cum[i - 1] + (S[i - 1]["v"] * dt if 0 < dt < MAX_DT else 0.0)
    duration_s = S[-1]["t"] - S[0]["t"]

    # ---- 2. Yaw rate per interval (R3 §0/§2): r = unwrap(Δheading)/Δt. ------
    # yaw[i] = yaw rate over interval [i, i+1], aligned to sample i.
    yaw: list[float | None] = [None] * n
    for i in range(n - 1):
        dt = S[i + 1]["t"] - S[i]["t"]
        ha, hb = S[i]["head"], S[i + 1]["head"]
        if ha is None or hb is None or not (0 < dt < MAX_DT):
            continue
        yaw[i] = _wrap(hb - ha) / dt

    # ---- 3. Friction circle / grip util % (R3 §1). -------------------------
    combined = [math.hypot(s["gx"], s["gy"]) for s in S]
    max_lat = max((abs(s["gy"]) for s in S), default=0.0)
    accel_gs = [s["gx"] for s in S if s["gx"] > 0]
    brake_gs = [-s["gx"] for s in S if s["gx"] < 0]
    max_accel = max(accel_gs, default=0.0)
    max_brake = max(brake_gs, default=0.0)
    max_combined = max(combined, default=0.0)
    # per-direction envelope: bin by theta = atan2(gx, gy), take max combined/bin.
    bin_max = [0.0] * DIR_BINS
    for s, c in zip(S, combined):
        b = int((math.atan2(s["gx"], s["gy"]) + math.pi) / (2 * math.pi) * DIR_BINS) % DIR_BINS
        if c > bin_max[b]:
            bin_max[b] = c
    above = 0
    for s, c in zip(S, combined):
        b = int((math.atan2(s["gx"], s["gy"]) + math.pi) / (2 * math.pi) * DIR_BINS) % DIR_BINS
        if bin_max[b] > 0 and c >= 0.9 * bin_max[b]:
            above += 1
    grip = {
        "max_lat_g": round(max_lat, 3),
        "max_accel_g": round(max_accel, 3),
        "max_brake_g": round(max_brake, 3),
        "max_combined_g": round(max_combined, 3),
        "envelope_g": round(max_combined, 3),       # first-cut g_limit ~= max combined observed
        "pct_time_above_90": round(100.0 * above / n, 1),
    }

    # ---- 4. Corner segmentation + phase tags (R3 §6). ----------------------
    in_corner = [abs(s["gy"]) > CORNER_LAT_G for s in S]
    corners_idx: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if in_corner[i]:
            j = i
            while j + 1 < n and in_corner[j + 1]:
                j += 1
            corners_idx.append((i, j))
            i = j + 1
        else:
            i += 1

    phase_of: list[str | None] = [None] * n  # ENTRY/MID/EXIT per cornering sample
    corners: list[dict] = []
    for ci, (a, b) in enumerate(corners_idx):
        seg = range(a, b + 1)
        apex = min(seg, key=lambda k: S[k]["v"])
        for k in seg:
            s = S[k]
            if k < apex:
                ph = "ENTRY" if (s["brk"] > BRAKE_THR or s["gx"] < -AX_THR) else "MID"
            elif k > apex:
                ph = "EXIT" if (s["thr"] > THROTTLE_THR or s["gx"] > AX_THR) else "MID"
            else:
                ph = "MID"
            phase_of[k] = ph
        mean_gy = _mean([S[k]["gy"] for k in seg])
        corners.append({
            "i": ci,
            "dist_m": round(cum[apex], 1),
            "v_min_kmh": round(S[apex]["v"] * 3.6, 1),
            "peak_lat_g": round(max(abs(S[k]["gy"]) for k in seg), 3),
            "direction": "left" if mean_gy >= 0 else "right",
            "phase_balance": {"entry": None, "mid": None, "exit": None},  # filled below
        })

    # ---- 5. Balance / understeer index per phase (R3 §2a). -----------------
    # B = 1 - r_actual/r_neutral, r_neutral = v*δ/L, δ = steering*MAX_STEER_RAD.
    all_B: list[float] = []
    phase_B: dict[str, list[float]] = {"ENTRY": [], "MID": [], "EXIT": []}
    # also collect per-corner phase B
    corner_phase_B: list[dict[str, list[float]]] = [
        {"ENTRY": [], "MID": [], "EXIT": []} for _ in corners
    ]
    sample_corner = [None] * n
    for ci, (a, b) in enumerate(corners_idx):
        for k in range(a, b + 1):
            sample_corner[k] = ci
    for i in range(n - 1):
        ph = phase_of[i]
        r_act = yaw[i]
        if ph is None or r_act is None:
            continue
        s = S[i]
        delta = s["str"] * MAX_STEER_RAD
        r_neu = s["v"] * delta / L
        if abs(r_neu) < MIN_RNEU:
            continue
        B = 1.0 - r_act / r_neu
        all_B.append(B)
        phase_B[ph].append(B)
        ci = sample_corner[i]
        if ci is not None:
            corner_phase_B[ci][ph].append(B)

    for ci, cpb in enumerate(corner_phase_B):
        pb = corners[ci]["phase_balance"]
        for ph, key in (("ENTRY", "entry"), ("MID", "mid"), ("EXIT", "exit")):
            pb[key] = round(_mean(cpb[ph]), 3) if cpb[ph] else None

    entry_i = round(_mean(phase_B["ENTRY"]), 4) if phase_B["ENTRY"] else 0.0
    mid_i = round(_mean(phase_B["MID"]), 4) if phase_B["MID"] else 0.0
    exit_i = round(_mean(phase_B["EXIT"]), 4) if phase_B["EXIT"] else 0.0
    overall_i = round(_mean(all_B), 4) if all_B else 0.0

    def _phase_word(x: float) -> str:
        if x > BALANCE_THR:
            return "understeer"
        if x < -BALANCE_THR:
            return "oversteer (loose)"
        return "neutral"
    interp = "idx>0 = understeer, idx<0 = oversteer (RELATIVE, steering uncalibrated). " \
             "entry %s / mid %s / exit %s" % (_phase_word(entry_i), _phase_word(mid_i), _phase_word(exit_i))
    balance = {
        "overall_index": overall_i, "entry_index": entry_i,
        "mid_index": mid_i, "exit_index": exit_i, "interpretation": interp,
    }

    # ---- 6. Braking analysis (R3 §5, aggregate). ---------------------------
    brake_events = 0
    on_brakes = False
    for s in S:
        braking = s["brk"] > BRAKE_THR
        if braking and not on_brakes:
            brake_events += 1
        on_brakes = braking
    peak_decel = round(max_brake, 3)
    # straight-line yaw instability: yaw building while braking and steering ~0.
    sl_yaw = 0.0
    for i in range(n - 1):
        s = S[i]
        if s["brk"] > BRAKE_THR and abs(s["str"]) < STRAIGHT_STEER and yaw[i] is not None:
            sl_yaw = max(sl_yaw, abs(yaw[i]))
    braking = {
        "events": brake_events,
        "peak_decel_g": peak_decel,
        "straightline_yaw_instability": round(sl_yaw, 3),
    }

    # ---- 7. Ride / rebound gz PROXY (R3 §3/§4 degraded — no travel channel). -
    gzs = [s["gz"] for s in S]
    baseline = _mean(gzs)                    # ~1 g static; smoothed baseline
    dev = [g - baseline for g in gzs]        # deviation in g
    gz_rms = _rms(dev)
    # bottoming = rising edges past baseline + GZ_SPIKE_G, measured in m/s^2 (uses G).
    thr_mps2 = GZ_SPIKE_G * G
    bottoming = 0
    spiking = False
    for d in dev:
        hit = (d * G) > thr_mps2
        if hit and not spiking:
            bottoming += 1
        spiking = hit
    settle_quality = round(max(0.0, 1.0 - gz_rms / SETTLE_REF_G), 3)  # crude post-event-settle proxy
    ride = {
        "bottoming_events": bottoming,
        "gz_rms": round(gz_rms, 4),
        "settle_quality": settle_quality,
    }

    # ---- 8. Notes / caveats (the accuracy story the engineer must hear). ----
    notes.append("Balance index is RELATIVE/trend only: `steering` is normalized -1..1, "
                 "not road-wheel degrees, so δ=steering*%.2frad is an UNCALIBRATED assumption "
                 "(R3 §2 CAVEAT — the top accuracy risk). Use for before/after deltas, not absolute degrees."
                 % MAX_STEER_RAD)
    notes.append("g-force sign convention (gx forward+, gy left+, decel gx<0) is ASSUMED; "
                 "sign-check gy against a known corner before trusting direction labels.")
    notes.append("No suspension-travel channel on the consumer build: ride/rebound (R3 §3/§4) "
                 "degrades to a gz-based bottoming + settle PROXY only — not a true damper-velocity histogram.")
    notes.append("Braking is aggregate (R3 §5): per-wheel lockup and brake-bias inference need a Lua probe and are not computed.")
    if not any(s["head"] is not None for s in S):
        notes.append("No heading channel present — balance/yaw metrics are zeroed.")

    report = {
        "ok": True,
        "samples": n,
        "duration_s": round(duration_s, 2),
        "distance_m": round(cum[-1], 1),
        "speed": {
            "max_kmh": round(max(s["v"] for s in S) * 3.6, 1),
            "avg_kmh": round(_mean([s["v"] for s in S]) * 3.6, 1),
            "min_kmh": round(min(s["v"] for s in S) * 3.6, 1),
        },
        "grip": grip,
        "balance": balance,
        "corners": corners,
        "braking": braking,
        "ride": ride,
        "notes": notes,
    }
    report["symptoms"] = detect_symptoms(report)
    return report


# ---------------------------------------------------------------------------
# Offline selftest: synthetic skidpad / understeer / oversteer row lists.
# ---------------------------------------------------------------------------
def _synth_corner(ratio: float, hz: float = 30.0, dur: float = 6.0,
                  v0: float = 22.0, dip: float = 8.0, steer: float = 0.3,
                  L: float = 2.6) -> list[dict]:
    """Steady-cornering rows whose actual yaw = ratio * neutral yaw.

    ratio==1 -> neutral skidpad (B~=0); ratio<1 -> understeer (B>0);
    ratio>1 -> oversteer (B<0). Speed dips to a mid apex (entry braking, exit
    throttle) so ENTRY/MID/EXIT phases populate. Heading is stored WRAPPED to
    (-pi, pi] to exercise the unwrap path.
    """
    n = int(hz * dur)
    dt = 1.0 / hz
    rows: list[dict] = []
    heading = 0.0
    for k in range(n):
        t = k * dt
        v = v0 - dip * math.sin(math.pi * t / dur)           # min at t = dur/2
        delta = steer * MAX_STEER_RAD
        r_neu = v * delta / L
        r_act = ratio * r_neu
        dvdt = -dip * (math.pi / dur) * math.cos(math.pi * t / dur)  # decel then accel
        gx = dvdt / G
        gy = (v * r_act) / G                                 # lateral g consistent with actual yaw
        rows.append({
            "t": round(t, 4), "speed": v, "heading": _wrap(heading),
            "gx": gx, "gy": gy, "gz": 1.0,
            "throttle": 0.6 if gx > 0 else 0.0,
            "brake": 0.6 if gx < 0 else 0.0,
            "steering": steer,
        })
        heading += r_act * dt
    return rows


def _selftest() -> None:
    # 1) Neutral skidpad: |overall_index| small, no balance symptom.
    skid = analyze_lap(_synth_corner(1.0))
    assert skid["ok"], skid
    assert skid["samples"] > 50, skid["samples"]
    assert len(skid["corners"]) >= 1, "expected a corner"
    assert abs(skid["balance"]["overall_index"]) < 0.05, skid["balance"]
    bsyms = [s["symptom"] for s in skid["symptoms"]]
    assert "understeer" not in bsyms and "oversteer" not in bsyms, skid["symptoms"]

    # 2) Understeer: actual yaw < neutral -> overall_index > 0 and an understeer symptom.
    us = analyze_lap(_synth_corner(0.8))
    assert us["ok"], us
    assert us["balance"]["overall_index"] > 0.05, us["balance"]
    assert any(s["symptom"] == "understeer" for s in us["symptoms"]), us["symptoms"]

    # 3) Oversteer: actual yaw > neutral -> overall_index < 0 and an oversteer symptom.
    ov = analyze_lap(_synth_corner(1.25))
    assert ov["ok"], ov
    assert ov["balance"]["overall_index"] < -0.05, ov["balance"]
    assert any(s["symptom"] == "oversteer" for s in ov["symptoms"]), ov["symptoms"]

    # 4) detect_symptoms is callable on a report and tolerant of junk.
    assert detect_symptoms(us) == us["symptoms"]
    assert detect_symptoms({}) == []
    assert detect_symptoms({"ok": False}) == []

    # 5) bottoming proxy fires on a gz spike; instability fires on straight-line yaw.
    rows = _synth_corner(1.0)
    rows[40]["gz"] = 2.5            # sharp vertical spike
    rep = analyze_lap(rows)
    assert rep["ride"]["bottoming_events"] >= 1, rep["ride"]
    assert any(s["symptom"] == "bottoming" for s in rep["symptoms"]), rep["symptoms"]

    inst = [{"t": round(i / 30.0, 4), "speed": 40.0,
             "heading": _wrap(0.4 * i / 30.0),   # yaw building...
             "gx": -1.0, "gy": 0.0, "gz": 1.0,
             "throttle": 0.0, "brake": 0.8, "steering": 0.0}  # ...while straight + braking
            for i in range(60)]
    irep = analyze_lap(inst)
    assert irep["braking"]["straightline_yaw_instability"] > YAW_INSTAB_RAD, irep["braking"]
    assert any(s["symptom"] == "brake_instability" for s in irep["symptoms"]), irep["symptoms"]

    # 6) Degenerate input returns ok:False, never raises.
    assert analyze_lap([])["ok"] is False
    assert analyze_lap([{"t": 0.0}])["ok"] is False

    print("SELFTEST OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
