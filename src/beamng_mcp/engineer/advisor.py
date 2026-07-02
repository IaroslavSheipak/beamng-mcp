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

from . import knowledge as engineer_kb

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

    # 2) Telemetry symptoms (v2 report). Agreement upgrades to "both".
    tele = report.get("symptoms") if isinstance(report, dict) and report.get("ok") else []
    for ts in tele or []:
        conf = ts.get("confidence", "low")
        ev = ts.get("evidence", "telemetry")
        for ph, sym in _v2_phase_symptoms(ts):
            key = (ph, sym)
            if key in index:
                c = index[key]
                if c["source"] in ("driver", "both"):
                    # driver + telemetry agreement — the real confidence boost
                    c["source"] = "both"
                    c["confidence"] = _boost(c["confidence"], conf)
                else:
                    # telemetry + telemetry (e.g. balance index AND slip angle
                    # both reading oversteer): same sensor family agreeing —
                    # keep the stronger confidence, do NOT claim source "both"
                    # (seen live: an empty-feedback diagnosis showed "both").
                    if CONF_RANK.get(conf, 1) > CONF_RANK.get(c["confidence"], 1):
                        c["confidence"] = conf
                c["evidence"] = ev or c["evidence"]
            else:
                c = {"phase": ph, "symptom": sym, "source": "telemetry",
                     "score": None, "confidence": conf, "evidence": ev}
                index[key] = c
                complaints.append(c)
    return complaints


def _v2_phase_symptoms(ts: dict) -> list[tuple[str, str]]:
    """Map a v2 report symptom ({type, phase?}) onto engineer (phase, symptom)
    pairs. v2 balance is overall (no phase), so understeer/oversteer expand across
    the cornering phases; brake/bottoming map to their phase."""
    typ = (ts.get("type") or ts.get("symptom") or "").lower()
    explicit = ts.get("phase")
    if "understeer" in typ:
        sym = "understeer"
    elif "oversteer" in typ or "sliding" in typ or "loose" in typ:
        sym = "oversteer"
    elif "brake" in typ or "instab" in typ:
        return [(explicit or "BRAKING", "instability")]
    elif "bottom" in typ:
        return [(explicit or "KERB", "bottoming")]
    else:
        sym = typ
    if explicit:
        return [(explicit, sym)]
    return [("ENTRY", sym), ("MID", sym), ("EXIT", sym)]


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
        # A current value at/outside the spec's safe range makes the clamp
        # REVERSE the intended move (seen live: front ARB 175000 with clamp hi
        # 100000 turned "stiffen +12%" into a -43% softening). A plan item that
        # moves opposite its own rationale is worse than no item — drop it.
        move = float(prop["proposed"]) - rep["current"]
        if move == 0 or (move > 0) != (net_dir == "+"):
            caveats.append(
                f"{_label(rep['lever'])} ({var}): current {rep['current']:g} is at/outside "
                f"the safe range {spec['clamp']}, so a '{net_dir}' move has no headroom "
                "— dropped from the plan.")
            continue
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
    if isinstance(report, dict) and report.get("ok"):
        bnote = (report.get("balance") or {}).get("note")
        if bnote:
            caveats.append(bnote)              # the balance self-calibration caveat
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
        if bal.get("tendency") and bal["tendency"] != "unknown":
            slip = bal.get("slip_angle_deg")
            extra = ", slip %.1f deg" % slip if slip is not None else ""
            lines.append("  telemetry: %s (%s confidence%s)"
                         % (bal["tendency"], bal.get("confidence", "low"), extra))

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
