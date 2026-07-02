"""Engineer-brain tests — adapted from v1's engineer selftest to v2's report shape."""

from beamng_mcp.engineer import advisor, knowledge

AVAIL = {"$arb_spring_F": 45000, "$arb_spring_R": 25000, "$damp_rebound_F": 18000, "$brakebias": 0.68}


def _v2_report(symptom_type="understeer", confidence="medium"):
    return {
        "ok": True,
        "symptoms": [{"type": symptom_type, "confidence": confidence, "evidence": "telemetry"}],
        "balance": {"tendency": symptom_type, "slip_angle_deg": 3.0, "confidence": confidence,
                    "note": "self-calibrated"},
    }


def test_driver_only_understeer_softens_front_arb():
    d = advisor.diagnose("understeer on entry", None, AVAIL)
    assert d["ok"]
    top = d["plan"][0]
    assert top["lever"] == "arb_F" and top["var"] == "$arb_spring_F"
    assert top["priority"] == 1 and top["dir"] == "-"      # soften front ARB
    assert top["proposed"] < 45000
    assert top["delta_pct"] == -12.0
    assert top["source"] == "driver"


def test_all_proposed_values_within_clamp():
    d = advisor.diagnose("understeer on entry", None, AVAIL)
    for it in d["plan"]:
        lo, hi = knowledge.classify_var(it["var"])["clamp"]
        assert lo <= it["proposed"] <= hi


def test_plan_to_vars_only_dollar_keys():
    d = advisor.diagnose("understeer on entry", None, AVAIL)
    vmap = advisor.plan_to_vars(d["plan"], AVAIL)
    assert vmap and all(k.startswith("$") for k in vmap)
    assert vmap["$arb_spring_F"] < 45000


def test_pressure_dropped_from_var_map():
    # pressure is applied live via Lua, never through set_part_config.
    assert advisor.plan_to_vars([{"var": "$pressure_F", "proposed": 28.0}], {}) == {}


def test_telemetry_agreement_yields_both():
    d = advisor.diagnose("understeer on entry", _v2_report("understeer"), AVAIL)
    assert d["ok"]
    assert any(c["source"] == "both" for c in d["complaints"]), d["complaints"]


def test_telemetry_only_drives_plan():
    d = advisor.diagnose("", _v2_report("understeer"), AVAIL)
    assert d["complaints"] and all(c["source"] == "telemetry" for c in d["complaints"])
    assert d["plan"]


def test_no_vars_is_graceful():
    d = advisor.diagnose("understeer on entry", None, {})
    assert d["ok"] and d["plan"] == []
    assert any("No live $vars" in c for c in d["caveats"])


def test_format_report_renders_brief():
    d = advisor.diagnose("understeer on entry", _v2_report("understeer"), AVAIL)
    brief = advisor.format_report(_v2_report("understeer"), d)
    assert "RACE ENGINEER" in brief and "telemetry:" in brief


def test_never_raises_on_junk():
    assert advisor.diagnose("", None, None)["ok"] in (True, False)


def test_no_headroom_item_is_dropped_not_reversed():
    """The live P2 bug: front ARB at 175000 with spec clamp hi 100000 turned
    'stiffen +12%' into a -43% SOFTENING. Such an item must be dropped with a
    caveat, never emitted moving against its own rationale."""
    avail = {"$arb_spring_F": 175000.0, "$arb_spring_R": 30000.0}
    d = advisor.diagnose("oversteer in corners", None, avail)
    assert d["ok"]
    vars_in_plan = {it["var"] for it in d["plan"]}
    assert "$arb_spring_F" not in vars_in_plan          # dropped, not reversed
    assert "$arb_spring_R" in vars_in_plan              # the in-range lever survives
    rear = next(it for it in d["plan"] if it["var"] == "$arb_spring_R")
    assert rear["dir"] == "-" and rear["proposed"] < 30000
    assert any("no headroom" in c for c in d["caveats"])


def test_every_plan_item_moves_in_its_own_direction():
    for feedback in ("understeer on entry", "oversteer in corners",
                     "unstable under braking", "bottoming over kerbs"):
        for cur_f in (45000.0, 175000.0, 1000.0):
            d = advisor.diagnose(feedback, None, {"$arb_spring_F": cur_f,
                                                  "$arb_spring_R": 30000.0,
                                                  "$brakebias": 0.68})
            for it in d["plan"]:
                move = it["proposed"] - it["current"]
                assert move != 0
                assert (move > 0) == (it["dir"] == "+"), it


def test_two_telemetry_signals_do_not_impersonate_the_driver():
    # Live: an EMPTY-feedback diagnosis showed source "both" because two
    # telemetry symptoms (balance index + slip) mapped to the same phase/symptom.
    # "both" must mean driver + telemetry; telemetry x2 stays telemetry, unboosted.
    rep = {"ok": True, "symptoms": [
        {"type": "oversteer", "confidence": "medium", "evidence": "understeer_index"},
        {"type": "sliding", "confidence": "medium", "evidence": "peak slip"},
    ], "balance": {"tendency": "oversteer (loose)", "confidence": "medium"}}
    d = advisor.diagnose("", rep, AVAIL)
    assert d["ok"] and d["complaints"]
    assert all(c["source"] == "telemetry" for c in d["complaints"])
    assert all(c["confidence"] != "high" for c in d["complaints"])
