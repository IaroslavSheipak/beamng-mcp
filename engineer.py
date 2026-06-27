"""engineer.py — the AI race engineer ORCHESTRATOR (no game, pure stdlib).

Ties the two halves of the "race engineer" together:

  * driver words  -> engineer_kb.match_complaint()          (the complaint)
  * telemetry     -> lap_analysis report["symptoms"]         (the evidence)
  * complaint     -> engineer_kb.remedies_for() + resolve_lever() + VAR_SPECS
                     -> a concrete, clamped `$var` setup change (the fix)

`diagnose()` merges driver feedback with the auto-detected telemetry symptoms
(agreement boosts confidence and labels the source "both"), turns each complaint
into resolvable levers, computes a concrete proposed value per lever from
engineer_kb.VAR_SPECS, then NETS duplicate / opposing levers across complaints
(summing signed, confidence-weighted priority and flagging genuine conflicts).
`plan_to_vars()` reduces the plan to the `{"$var": value}` map `set_part_config`
wants; `format_report()` renders the pit-wall radio brief.

Nothing here imports beamngpy or touches the network — it is pure data wrangling
on top of engineer_kb (the brain) and lap_analysis (the eyes).

SIGN CONVENTION is inherited verbatim from engineer_kb (R2 §0): remedy `dir`
is the direction to move the VAR NUMBER ("+" = increase, "-" = decrease); higher
rate var = stiffer, higher brakebias = more front. We never flip a sign here.
"""
from __future__ import annotations

import engineer_kb
import lap_analysis

# Named race engineer on the pit wall (persona voice for the radio brief).
ENGINEER = "Mara"

# --- Netting / confidence tunables (documented; do not change channel maths). -
CONF_RANK = {"low": 1, "medium": 2, "high": 3}
RANK_CONF = {1: "low", 2: "medium", 3: "high"}
PRIO_SPAN = 10                 # priorities run ~1..10; weight = span - prio + 1
RELATIVE_KEEP = 0.55           # keep driver complaints scoring >= this * top score
MIN_KEEP_SCORE = 2.0           # ...but never below this absolute floor
LOW_CONF_STEP = 0.5            # geometry / low-confidence levers move a half step


# --------------------------------------------------------------------------- #
# Small helpers.
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _score_conf(score: float | None) -> str:
    """Map a match_complaint score to a confidence label."""
    if score is None:
        return "low"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low"


def _boost(a: str, b: str) -> str:
    """Confidence when driver + telemetry AGREE: max of the two, bumped one notch."""
    rank = min(3, max(CONF_RANK.get(a, 1), CONF_RANK.get(b, 1)) + 1)
    return RANK_CONF[rank]


def _norm_symptom(symptom: str) -> str:
    """Map telemetry symptom vocabulary onto engineer_kb's RULES vocabulary."""
    if symptom == "brake_instability":
        return "instability"
    return symptom


def _round(kind: str, val: float) -> float:
    """Round a proposed value to a sensible resolution for its var kind."""
    if kind == "rate":                       # N/m or N/m/s — integers
        return float(round(val))
    return round(val, 4)                      # bias/lsd/height/angle/pressure/aero


def _verb(kind: str, direction: str) -> str:
    """Plain-language verb for a lever move (for the radio brief)."""
    plus = direction == "+"
    if kind == "rate":
        return "stiffen" if plus else "soften"
    if kind == "height":
        return "raise" if plus else "lower"
    if kind == "bias":
        return "shift forward" if plus else "shift rearward"
    if kind == "lsd":
        return "add lock to" if plus else "free up"
    if kind == "pressure":
        return "raise pressure on" if plus else "drop pressure on"
    if kind == "angle_mult":
        return "add to" if plus else "reduce"
    if kind == "aero":
        return "add wing to" if plus else "trim"
    return "increase" if plus else "decrease"


def _label(lever: str) -> str:
    spec = engineer_kb.LEVERS.get(lever)
    return spec["label"] if spec else lever


# --------------------------------------------------------------------------- #
# Proposed-value computation (the apply layer, per VAR_SPECS).
# --------------------------------------------------------------------------- #
def _propose(current: float, spec: dict, direction: str) -> dict:
    """Concrete proposed value for one lever move.

    rate  -> current * (1 +/- rel_step);  everything else -> current +/- abs_step.
    Geometry / low-confidence levers take a SMALLER step. Always clamped to
    spec["clamp"]. Returns {proposed, delta_pct|delta} with the right delta key.
    """
    kind = spec["kind"]
    lo, hi = spec["clamp"]
    low_conf = spec.get("confidence") == "low"
    sign = 1.0 if direction == "+" else -1.0

    if kind == "rate":
        step = (spec.get("rel_step") or 0.0) * (LOW_CONF_STEP if low_conf else 1.0)
        proposed = current * (1.0 + sign * step)
        proposed = _round(kind, _clamp(proposed, lo, hi))
        delta_pct = round(100.0 * (proposed - current) / current, 1) if current else None
        return {"proposed": proposed, "delta_pct": delta_pct}

    step = (spec.get("abs_step") or 0.0) * (LOW_CONF_STEP if low_conf else 1.0)
    proposed = current + sign * step
    proposed = _round(kind, _clamp(proposed, lo, hi))
    return {"proposed": proposed, "delta": round(proposed - current, 4)}


# --------------------------------------------------------------------------- #
# Complaint merge (driver words + telemetry symptoms).
# --------------------------------------------------------------------------- #
def _merge_complaints(feedback: str, report: dict | None) -> list[dict]:
    """Build the merged complaint list, labelling source driver/telemetry/both."""
    # 1) Driver complaints, filtered to the strong interpretations only (so a
    #    weak spurious opposite reading can't manufacture a fake conflict).
    raw = engineer_kb.match_complaint(feedback or "")
    kept: list[dict] = []
    if raw:
        top = raw[0]["score"]
        floor = max(MIN_KEEP_SCORE, top * RELATIVE_KEEP)
        kept = [d for d in raw if d["score"] >= floor]

    complaints: list[dict] = []
    index: dict[tuple, dict] = {}
    for d in kept:
        c = {"phase": d["phase"], "symptom": d["symptom"], "source": "driver",
             "score": d["score"], "confidence": _score_conf(d["score"]),
             "evidence": "driver report"}
        index[(d["phase"], d["symptom"])] = c
        complaints.append(c)

    # 2) Telemetry symptoms (auto-detected). Agreement upgrades to "both".
    tele: list[dict] = []
    if isinstance(report, dict) and report.get("ok"):
        tele = report.get("symptoms")
        if tele is None:
            tele = lap_analysis.detect_symptoms(report)
    for ts in tele or []:
        ph = ts.get("phase")
        sym = _norm_symptom(ts.get("symptom", ""))
        key = (ph, sym)
        if key in index:
            c = index[key]
            c["source"] = "both"
            c["confidence"] = _boost(c["confidence"], ts.get("confidence", "low"))
            c["evidence"] = ts.get("evidence") or c["evidence"]
        else:
            c = {"phase": ph, "symptom": sym, "source": "telemetry",
                 "score": None, "confidence": ts.get("confidence", "low"),
                 "evidence": ts.get("evidence", "telemetry")}
            index[key] = c
            complaints.append(c)
    return complaints


# --------------------------------------------------------------------------- #
# Public API.
# --------------------------------------------------------------------------- #
def diagnose(feedback: str, report: dict | None = None,
             available_vars: dict | None = None) -> dict:
    """Driver words + telemetry -> a concrete, netted setup plan.

    Returns {ok, persona, complaints, plan, caveats}. Never raises (server
    contract); on internal error returns {ok:False, error, ...}.
    """
    try:
        return _diagnose(feedback, report, available_vars)
    except Exception as exc:  # noqa: BLE001 — server contract: never raise
        return {"ok": False, "error": "diagnose failed: %r" % exc,
                "persona": "%s: lost telemetry on that one, say again." % ENGINEER,
                "complaints": [], "plan": [], "caveats": []}


def _diagnose(feedback: str, report: dict | None, available_vars: dict | None) -> dict:
    avail = available_vars or {}
    complaints = _merge_complaints(feedback, report)
    caveats: list[str] = []

    # --- 1. Expand every complaint into resolvable lever candidates. ----------
    # candidates grouped by the apply target ($var) so opposing pulls collide.
    groups: dict[str, list[dict]] = {}
    for c in complaints:
        for rem in engineer_kb.remedies_for(c["phase"], c["symptom"]):
            rl = engineer_kb.resolve_lever(rem["lever"], avail)
            if rl is None:
                continue                     # car lacks this lever — skip it
            var = rl["var"]
            spec = engineer_kb.classify_var(var)
            if spec is None:
                continue
            try:
                current = float(avail[var])
            except (TypeError, ValueError):
                continue
            groups.setdefault(var, []).append({
                "lever": rem["lever"], "var": var, "current": current,
                "kind": spec["kind"], "spec": spec, "dir": rem["dir"],
                "priority": rem["priority"], "rationale": rem["rationale"],
                "source": c["source"], "conf": c["confidence"],
            })

    # --- 2. Net each lever: sum signed, confidence-weighted priority. ---------
    plan: list[dict] = []
    for var, cands in groups.items():
        pos = neg = 0.0
        for k in cands:
            w = CONF_RANK.get(k["conf"], 1) * (PRIO_SPAN - k["priority"] + 1)
            if k["dir"] == "+":
                pos += w
            else:
                neg += w
        conflict = pos > 0 and neg > 0
        if pos > neg:
            net_dir = "+"
        elif neg > pos:
            net_dir = "-"
        else:                                # exact tie -> follow best priority
            net_dir = min(cands, key=lambda k: k["priority"])["dir"]

        winners = [k for k in cands if k["dir"] == net_dir]
        rep = min(winners, key=lambda k: (k["priority"], -CONF_RANK.get(k["conf"], 1)))
        spec = rep["spec"]

        # source across the winning pulls.
        srcs = {k["source"] for k in winners}
        if "both" in srcs or {"driver", "telemetry"} <= srcs:
            source = "both"
        else:
            source = srcs.pop()

        # plan-item confidence = min(complaint, var-spec) — geometry caps low.
        conf_rank = min(CONF_RANK.get(rep["conf"], 1),
                        CONF_RANK.get(spec.get("confidence", "high"), 3))
        confidence = RANK_CONF[conf_rank]

        prop = _propose(rep["current"], spec, net_dir)
        item = {
            "lever": rep["lever"], "var": var, "current": rep["current"],
            "proposed": prop["proposed"], "dir": net_dir,
            "priority": rep["priority"], "confidence": confidence,
            "rationale": rep["rationale"], "source": source,
        }
        if "delta_pct" in prop:
            item["delta_pct"] = prop["delta_pct"]
        else:
            item["delta"] = prop["delta"]
        plan.append(item)

        if spec.get("confidence") == "low" and spec.get("note"):
            caveats.append("%s (%s): %s" % (_label(rep["lever"]), var, spec["note"]))
        if conflict:
            caveats.append(
                "Conflict on %s (%s): complaints pull both ways — went '%s' (%s)."
                % (_label(rep["lever"]), var, net_dir, rep["rationale"]))

    # --- 3. Order the plan: most important (lowest priority) first. ----------
    plan.sort(key=lambda it: (it["priority"], -CONF_RANK.get(it["confidence"], 1)))

    # --- 4. Caveats: apply mechanics + telemetry honesty. --------------------
    if not avail:
        caveats.insert(0, "No live $vars supplied — cannot compute concrete values; "
                          "read the car's tuning first.")
    if plan:
        caveats.append("Applying these vars RESPAWNS the car (resets pose/damage) — "
                       "apply between runs, then re-drive to confirm.")
    if isinstance(report, dict) and report.get("notes"):
        caveats.append(report["notes"][0])     # the balance-is-relative caveat
    caveats = _dedupe(caveats)

    persona = _persona(complaints, plan)
    return {"ok": True, "persona": persona, "complaints": complaints,
            "plan": plan, "caveats": caveats}


def plan_to_vars(plan: list[dict], current_vars: dict) -> dict:
    """Reduce a plan to the {"$var": newval} map for set_part_config.

    Only real `$`-prefixed tuning vars are emitted (tyre pressure is applied LIVE
    via Lua, never through set_part_config, so it is dropped here). Each value is
    re-clamped to its VAR_SPEC range as a final guard.
    """
    out: dict = {}
    for item in plan or []:
        var = item.get("var")
        if not isinstance(var, str) or not var.startswith("$"):
            continue
        spec = engineer_kb.classify_var(var)
        if spec is None or spec.get("kind") == "pressure":
            continue
        val = item.get("proposed")
        if val is None:
            continue
        lo, hi = spec["clamp"]
        out[var] = _round(spec["kind"], _clamp(float(val), lo, hi))
    return out


def format_report(report: dict | None, diagnosis: dict) -> str:
    """Render the pit-wall radio brief from a diagnosis (+ optional telemetry)."""
    if not isinstance(diagnosis, dict) or not diagnosis.get("ok"):
        err = (diagnosis or {}).get("error", "no diagnosis")
        return "%s: no call to make — %s" % (ENGINEER, err)

    lines: list[str] = ["== RACE ENGINEER : %s ==" % ENGINEER,
                        diagnosis.get("persona", "")]

    comps = diagnosis.get("complaints") or []
    if comps:
        lines.append("  read:")
        for c in comps:
            lines.append("    - %s %s  [%s, %s]"
                         % (c.get("phase"), c.get("symptom"),
                            c.get("source"), c.get("confidence")))

    plan = diagnosis.get("plan") or []
    if plan:
        lines.append("  plan:")
        for it in plan:
            if "delta_pct" in it and it["delta_pct"] is not None:
                chg = "%+.1f%%" % it["delta_pct"]
            elif "delta" in it:
                chg = "%+g" % it["delta"]
            else:
                chg = ""
            lines.append("    P%-2d %s %s: %s %g -> %g (%s) [%s, %s]"
                         % (it["priority"], _verb(_kind_of(it["var"]), it["dir"]),
                            _label(it["lever"]), it["var"], it["current"],
                            it["proposed"], chg, it["source"], it["confidence"]))
    else:
        lines.append("  plan: nothing actionable — no resolvable levers for this car.")

    if isinstance(report, dict) and report.get("ok"):
        bal = report.get("balance") or {}
        if bal.get("interpretation"):
            lines.append("  telemetry: %s" % bal["interpretation"])

    cav = diagnosis.get("caveats") or []
    if cav:
        lines.append("  caveats:")
        for c in cav:
            lines.append("    ! %s" % c)
    return "\n".join(l for l in lines if l)


def _kind_of(var: str) -> str:
    spec = engineer_kb.classify_var(var)
    return spec["kind"] if spec else "rate"


def _persona(complaints: list[dict], plan: list[dict]) -> str:
    """One-line radio call from the named engineer."""
    if not complaints:
        return "%s: telemetry's clean and you said nothing — no change called." % ENGINEER
    read = ", ".join("%s %s" % (c["phase"].lower(), c["symptom"])
                     for c in complaints[:2])
    if not plan:
        return ("%s: copy — reading %s, but this car doesn't expose the levers I'd "
                "reach for. Standing pat." % (ENGINEER, read))
    top = plan[0]
    extra = len(plan) - 1
    tail = " %d more queued." % extra if extra > 0 else ""
    return ("%s: copy — reading %s. First move, %s the %s (%s %g->%g).%s"
            % (ENGINEER, read, _verb(_kind_of(top["var"]), top["dir"]),
               _label(top["lever"]), top["var"], top["current"],
               top["proposed"], tail))


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# --------------------------------------------------------------------------- #
# Self-test.
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    avail = {"$arb_spring_F": 45000, "$arb_spring_R": 25000,
             "$damp_rebound_F": 18000, "$brakebias": 0.68}

    # --- core contract case: understeer on entry, driver-only. ---------------
    d = diagnose("understeer on entry", None, avail)
    assert d["ok"], d
    plan = d["plan"]
    assert plan, "expected a plan"
    top = plan[0]
    assert top["lever"] == "arb_F", "front ARB must be the priority-1 fix: %s" % top
    assert top["var"] == "$arb_spring_F"
    assert top["priority"] == 1
    assert top["dir"] == "-", "front ARB must SOFTEN for understeer"
    assert top["proposed"] < 45000, "softening front ARB must lower the value: %s" % top
    assert top["source"] == "driver"

    # every proposed value is inside its var-spec clamp.
    for it in plan:
        spec = engineer_kb.classify_var(it["var"])
        lo, hi = spec["clamp"]
        assert lo <= it["proposed"] <= hi, "%s out of clamp: %s" % (it["var"], it)
        assert "delta_pct" in it or "delta" in it, "missing delta key: %s" % it
        assert it["source"] in ("driver", "telemetry", "both")

    # rate move uses the multiplicative step (12% on ARB).
    assert top["delta_pct"] == -12.0, top.get("delta_pct")

    # plan_to_vars returns ONLY "$"-keys, clamped.
    vmap = plan_to_vars(plan, avail)
    assert vmap, "expected a var map"
    assert all(k.startswith("$") for k in vmap), vmap
    assert "$arb_spring_F" in vmap and vmap["$arb_spring_F"] < 45000, vmap
    for k, v in vmap.items():
        lo, hi = engineer_kb.classify_var(k)["clamp"]
        assert lo <= v <= hi, (k, v)

    # persona + report are non-empty strings.
    assert isinstance(d["persona"], str) and d["persona"]
    assert "soften" in d["persona"].lower(), d["persona"]
    brief = format_report(None, d)
    assert isinstance(brief, str) and "RACE ENGINEER" in brief, brief

    # --- telemetry merge: agreement -> source "both", no spurious conflict. ---
    us_report = lap_analysis.analyze_lap(_us_rows())
    assert us_report["ok"], us_report
    assert any(s["symptom"] == "understeer" for s in us_report["symptoms"]), us_report["symptoms"]
    d2 = diagnose("understeer on entry", us_report, avail)
    assert d2["ok"], d2
    assert any(c["source"] == "both" for c in d2["complaints"]), \
        "driver+telemetry agreement should yield a 'both' complaint: %s" % d2["complaints"]
    top2 = d2["plan"][0]
    assert top2["lever"] == "arb_F" and top2["proposed"] < 45000, top2
    # agreement should not have produced a conflict caveat on the front ARB.
    assert not any("Conflict on Front anti-roll bar" in c for c in d2["caveats"]), d2["caveats"]

    # --- telemetry-only: empty feedback, symptoms drive the plan. ------------
    d3 = diagnose("", us_report, avail)
    assert d3["ok"], d3
    assert d3["complaints"], "telemetry symptoms should stand in as complaints"
    assert all(c["source"] == "telemetry" for c in d3["complaints"]), d3["complaints"]
    assert d3["plan"], "telemetry-only understeer should still produce a plan"

    # --- opposing complaints flag a conflict and still pick a side. ----------
    d4 = diagnose("understeer mid-corner and oversteer mid-corner", None, avail)
    assert d4["ok"], d4
    assert any("Conflict on" in c for c in d4["caveats"]), \
        "opposite mid-corner complaints must flag a conflict: %s" % d4["caveats"]

    # --- no levers available -> graceful, no crash, caveat present. ----------
    d5 = diagnose("understeer on entry", None, {})
    assert d5["ok"] and d5["plan"] == [], d5
    assert any("No live $vars" in c for c in d5["caveats"]), d5["caveats"]
    assert plan_to_vars([], {}) == {}

    # --- pressure is dropped from plan_to_vars (Lua-applied, not a $var). -----
    fake = [{"var": "$pressure_F", "proposed": 28.0}]
    assert plan_to_vars(fake, {}) == {}, "pressure must not go through set_part_config"

    # --- never raises on junk. -----------------------------------------------
    assert diagnose("", None, None)["ok"] in (True, False)

    print("SELFTEST OK")


def _us_rows() -> list[dict]:
    """Synthetic steady understeer lap (actual yaw < neutral) for the merge test."""
    return lap_analysis._synth_corner(0.8)


if __name__ == "__main__":
    import sys
    _selftest() if "--selftest" in sys.argv else None
