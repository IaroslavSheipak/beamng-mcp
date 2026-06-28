"""engineer_kb.py — the race-engineer brain DATA (no game, pure stdlib).

This is the knowledge base half of the "AI race engineer": the static
complaint -> cause -> remedy matrix (translated from research R2) plus the
lever -> `$var` map and per-var apply specs (translated from research R4).
`engineer.py` consumes this module; nothing here touches beamngpy or telemetry.

SIGN CONVENTION (R2 section 0 — the master rule, encoded once, never flipped):
  Stiffer SPRING or ANTI-ROLL BAR on an axle => that axle TRANSFERS more load
  => that axle LOSES grip.  Front stiffer => understeer; rear stiffer =>
  oversteer.  This is OPPOSITE to camber/pressure (more negative camber / lower
  pressure => more grip, to a point).  Rate vars (spring, damper, ARB, LSD) and
  brake bias have an UNAMBIGUOUS numeric direction (higher = stiffer / more lock
  / more front).  Geometry vars (camber/toe/caster) are unitless multipliers
  whose jbeam min/max are often reversed => direction AMBIGUOUS => confidence
  "low", small nudge, "verify in-game" note.

Remedy `dir` is always the direction to move the VAR VALUE:
  "+" = increase the var number, "-" = decrease it.  For geometry levers the
  intent is encoded as: camber "+" = more negative camber (more grip);
  toe "+" = more toe-IN (more stability); caster "+" = more caster.
"""
from __future__ import annotations

import re

PHASES = ["ENTRY", "MID", "EXIT", "BRAKING", "KERB", "THERMAL"]


# --------------------------------------------------------------------------- #
# VAR_SPECS — match a car's real `$var` to its apply spec (R4 names + contract
# clamp ranges).  Anchored regexes (optional leading "$") so they are disjoint;
# ordered specific-first regardless.  Each spec:
#   {kind, unit, stiffer_is_plus, rel_step, abs_step, clamp:[lo,hi],
#    confidence, note}
# rel_step is used for "rate" kinds (multiplicative); abs_step for the rest.
# --------------------------------------------------------------------------- #
VAR_SPECS: list[tuple[str, dict]] = [
    (r"^\$?arb_spring_[FR]$", {
        "kind": "rate", "unit": "N/m", "stiffer_is_plus": True,
        "rel_step": 0.12, "abs_step": None, "clamp": [2000, 100000],
        "confidence": "high", "note": ""}),
    (r"^\$?damp_bump_[FR](?:_fast)?$", {
        "kind": "rate", "unit": "N/m/s", "stiffer_is_plus": True,
        "rel_step": 0.12, "abs_step": None, "clamp": [500, 25000],
        "confidence": "high", "note": ""}),
    (r"^\$?damp_rebound_[FR](?:_fast)?$", {
        "kind": "rate", "unit": "N/m/s", "stiffer_is_plus": True,
        "rel_step": 0.12, "abs_step": None, "clamp": [500, 25000],
        "confidence": "high", "note": ""}),
    (r"^\$?(?:spring|ride)height_[FR]$", {
        "kind": "height", "unit": "m", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.005, "clamp": [-0.06, 0.06],
        "confidence": "high", "note": "higher var = raise that end"}),
    (r"^\$?spring_[FR]$", {
        "kind": "rate", "unit": "N/m", "stiffer_is_plus": True,
        "rel_step": 0.10, "abs_step": None, "clamp": [15000, 160000],
        "confidence": "high", "note": ""}),
    (r"^\$?camber_[FR][RL]?$", {
        "kind": "angle_mult", "unit": "", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.01, "clamp": [0.95, 1.05],
        "confidence": "low",
        "note": "unitless multiplier; jbeam min/max may be reversed — "
                "'+' intends MORE negative camber; verify in-game"}),
    (r"^\$?toe_[FR][RL]?$", {
        "kind": "angle_mult", "unit": "", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.005, "clamp": [0.98, 1.02],
        "confidence": "low",
        "note": "multiplier; min/max often reversed — '+' intends more "
                "toe-IN (stability); verify in-game"}),
    (r"^\$?caster_[FR][RL]?$", {
        "kind": "angle_mult", "unit": "", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.005, "clamp": [0.97, 1.03],
        "confidence": "low",
        "note": "multiplier; min/max often reversed — '+' intends MORE "
                "caster; verify in-game"}),
    (r"^\$?steer_center_[FR]$", {
        "kind": "angle_mult", "unit": "", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.0005, "clamp": [-0.002, 0.002],
        "confidence": "low", "note": "fine steering-center trim"}),
    (r"^\$?brakebias$", {
        "kind": "bias", "unit": "frac_front", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.02, "clamp": [0.0, 1.0],
        "confidence": "high", "note": "fraction to FRONT; higher = more front"}),
    (r"^\$?brakestrength$", {
        "kind": "bias", "unit": "mult", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.05, "clamp": [0.6, 1.2],
        "confidence": "high", "note": "overall brake-force multiplier"}),
    (r"^\$?braketorque(?:_[FR])?$", {
        "kind": "bias", "unit": "Nm", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 50.0, "clamp": [0.0, 100000.0],
        "confidence": "high", "note": "absolute brake torque cap"}),
    (r"^\$?lsdpreload_[FR]$", {
        "kind": "lsd", "unit": "N/m", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 25.0, "clamp": [0.0, 500.0],
        "confidence": "high", "note": "baseline lock (entry+mid+exit)"}),
    (r"^\$?lsdlockcoefrev_[FR]$", {
        "kind": "lsd", "unit": "", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.05, "clamp": [0.0, 0.5],
        "confidence": "high", "note": "coast (off-throttle) lock"}),
    (r"^\$?lsdlockcoef_[FR]$", {
        "kind": "lsd", "unit": "", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 0.05, "clamp": [0.0, 0.5],
        "confidence": "high", "note": "power (on-throttle) lock"}),
    (r"^\$?(?:tire)?pressure_[FR]$", {
        "kind": "pressure", "unit": "psi", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 1.0, "clamp": [18.0, 40.0],
        "confidence": "high",
        "note": "NOT a $var — apply LIVE via setGroupPressure; higher = "
                "less grip in-window"}),
    (r"^\$?(?:spoiler|wing)\w*_[FR]$", {
        "kind": "aero", "unit": "deg", "stiffer_is_plus": True,
        "rel_step": None, "abs_step": 1.0, "clamp": [0.0, 30.0],
        "confidence": "low", "note": "car-specific aero element; '+' = more wing"}),
]


# --------------------------------------------------------------------------- #
# LEVERS — canonical lever id -> resolution + semantics.  var_patterns are
# matched against the car's actual `$var` keys at runtime (resolve_lever).
# --------------------------------------------------------------------------- #
def _lever(label, patterns, axle, kind, stiffer_is_plus, confidence):
    return {"label": label, "var_patterns": patterns, "axle": axle,
            "kind": kind, "stiffer_is_plus": stiffer_is_plus,
            "confidence": confidence}


LEVERS: dict[str, dict] = {
    "arb_F": _lever("Front anti-roll bar", [r"^\$?arb_spring_F$"],
                    "F", "rate", True, "high"),
    "arb_R": _lever("Rear anti-roll bar", [r"^\$?arb_spring_R$"],
                    "R", "rate", True, "high"),
    "spring_F": _lever("Front spring rate", [r"^\$?spring_F$"],
                       "F", "rate", True, "high"),
    "spring_R": _lever("Rear spring rate", [r"^\$?spring_R$"],
                       "R", "rate", True, "high"),
    "bump_F": _lever("Front bump (compression) damping",
                     [r"^\$?damp_bump_F(?:_fast)?$"], "F", "rate", True, "high"),
    "bump_R": _lever("Rear bump (compression) damping",
                     [r"^\$?damp_bump_R(?:_fast)?$"], "R", "rate", True, "high"),
    "rebound_F": _lever("Front rebound (extension) damping",
                        [r"^\$?damp_rebound_F(?:_fast)?$"], "F", "rate", True, "high"),
    "rebound_R": _lever("Rear rebound (extension) damping",
                        [r"^\$?damp_rebound_R(?:_fast)?$"], "R", "rate", True, "high"),
    "camber_F": _lever("Front camber", [r"^\$?camber_F[RL]?$"],
                       "F", "angle_mult", True, "low"),
    "camber_R": _lever("Rear camber", [r"^\$?camber_R[RL]?$"],
                       "R", "angle_mult", True, "low"),
    "toe_F": _lever("Front toe", [r"^\$?toe_F[RL]?$"],
                    "F", "angle_mult", True, "low"),
    "toe_R": _lever("Rear toe", [r"^\$?toe_R[RL]?$"],
                    "R", "angle_mult", True, "low"),
    "caster_F": _lever("Front caster", [r"^\$?caster_F[RL]?$"],
                       "F", "angle_mult", True, "low"),
    "rideheight_F": _lever("Front ride height",
                           [r"^\$?(?:spring|ride)height_F$"], "F", "height", True, "high"),
    "rideheight_R": _lever("Rear ride height",
                           [r"^\$?(?:spring|ride)height_R$"], "R", "height", True, "high"),
    "brakebias": _lever("Brake bias", [r"^\$?brakebias$"],
                        None, "bias", True, "high"),
    "diff_preload_F": _lever("Front diff preload", [r"^\$?lsdpreload_F$"],
                             "F", "lsd", True, "high"),
    "diff_preload_R": _lever("Rear diff preload", [r"^\$?lsdpreload_R$"],
                             "R", "lsd", True, "high"),
    "diff_power_F": _lever("Front diff power lock", [r"^\$?lsdlockcoef_F$"],
                           "F", "lsd", True, "high"),
    "diff_power_R": _lever("Rear diff power lock", [r"^\$?lsdlockcoef_R$"],
                           "R", "lsd", True, "high"),
    "diff_coast_F": _lever("Front diff coast lock", [r"^\$?lsdlockcoefrev_F$"],
                           "F", "lsd", True, "high"),
    "diff_coast_R": _lever("Rear diff coast lock", [r"^\$?lsdlockcoefrev_R$"],
                           "R", "lsd", True, "high"),
    "pressure_F": _lever("Front tyre pressure", [r"^\$?(?:tire)?pressure_F$"],
                         "F", "pressure", True, "high"),
    "pressure_R": _lever("Rear tyre pressure", [r"^\$?(?:tire)?pressure_R$"],
                         "R", "pressure", True, "high"),
    "aero_F": _lever("Front wing / aero",
                     [r"^\$?(?:spoiler|wing)\w*_F$"], "F", "aero", True, "low"),
    "aero_R": _lever("Rear wing / aero",
                     [r"^\$?(?:spoiler|wing)\w*_R$"], "R", "aero", True, "low"),
}


# --------------------------------------------------------------------------- #
# RULES — every R2 section-1 table row, priority order preserved.  Each remedy
# dir is the VAR direction (see module docstring).
# --------------------------------------------------------------------------- #
def _r(lever, direction, priority, rationale):
    return {"lever": lever, "dir": direction, "priority": priority,
            "rationale": rationale}


RULES: list[dict] = [
    # ----------------------------- A. ENTRY ----------------------------- #
    {"phase": "ENTRY", "symptom": "understeer",
     "aliases": ["understeer on entry", "won't turn in", "wont turn in",
                 "pushes to apox", "pushes to apex", "push on entry",
                 "won't bite", "wont bite", "front washes out on turn-in",
                 "not turning in", "plows into the corner"],
     "remedies": [
         _r("arb_F", "-", 1, "soften front ARB → front keeps grip on turn-in"),
         _r("arb_R", "+", 2, "stiffen rear ARB → shift balance rearward"),
         _r("brakebias", "-", 3, "brake bias rearward → more entry rotation"),
         _r("diff_coast_R", "-", 4, "less coast lock → more off-throttle rotation"),
         _r("rebound_F", "-", 5, "soften front rebound → front stays planted on brake-release"),
         _r("toe_F", "-", 6, "more front toe-OUT → sharper turn-in"),
         _r("camber_F", "+", 7, "more front negative camber → more front grip"),
         _r("spring_F", "-", 8, "soften front spring"),
         _r("pressure_F", "-", 9, "lower front pressure → bigger contact patch"),
         _r("rideheight_F", "-", 10, "lower front ride height → more front grip")]},
    {"phase": "ENTRY", "symptom": "oversteer",
     "aliases": ["oversteer on entry", "rear steps out on turn-in",
                 "snap on brake-release", "snap on brake release",
                 "loose on entry", "rear stepping out", "trail-brake instability",
                 "rear comes around on entry", "rear loose on entry"],
     "remedies": [
         _r("arb_R", "-", 1, "soften rear ARB → rear keeps grip"),
         _r("arb_F", "+", 2, "stiffen front ARB → shift balance forward"),
         _r("brakebias", "+", 3, "brake bias forward → stabilise entry"),
         _r("diff_coast_R", "+", 4, "more coast lock → stabilise off-throttle"),
         _r("rebound_R", "-", 5, "soften rear rebound → let rear settle and keep load"),
         _r("toe_R", "+", 6, "more rear toe-IN → calmer, more stable rear"),
         _r("spring_R", "-", 7, "soften rear spring"),
         _r("pressure_R", "-", 8, "lower rear pressure → more rear grip")]},
    {"phase": "ENTRY", "symptom": "wont_rotate",
     "aliases": ["won't rotate", "wont rotate", "lazy mid-to-apex",
                 "lazy turn-in", "too stable on entry", "car won't rotate",
                 "won't turn to apex"],
     "remedies": [
         _r("diff_preload_R", "-", 1, "reduce diff preload → frees entry rotation"),
         _r("diff_coast_R", "-", 2, "reduce coast lock → more rotation"),
         _r("arb_R", "+", 3, "stiffen rear ARB → loosen rear for rotation"),
         _r("brakebias", "-", 4, "brake bias rearward → more rotation"),
         _r("toe_R", "-", 5, "rear toe-OUT trim → more rotation (use sparingly)")]},
    {"phase": "ENTRY", "symptom": "nervous",
     "aliases": ["nervous on entry", "darty", "hard to place", "twitchy on entry",
                 "unstable turn-in", "too pointy on entry"],
     "remedies": [
         _r("toe_F", "+", 1, "more front toe-IN → straight-line stability"),
         _r("caster_F", "+", 2, "more caster → more stability"),
         _r("arb_F", "+", 3, "slightly stiffen front ARB → calmer front"),
         _r("diff_coast_R", "+", 4, "more coast lock → stabler off-throttle"),
         _r("rebound_F", "+", 5, "more front rebound → slow the turn-in")]},

    # ------------------------------ B. MID ------------------------------ #
    {"phase": "MID", "symptom": "understeer",
     "aliases": ["understeer mid-corner", "no front grip mid-corner",
                 "steady-state understeer", "steady state understeer",
                 "pushing mid corner", "won't hold the line", "midcorner push",
                 "no front grip"],
     "remedies": [
         _r("arb_F", "-", 1, "soften front ARB → less front load transfer"),
         _r("arb_R", "+", 2, "stiffen rear ARB → shift balance rearward"),
         _r("camber_F", "+", 3, "more front negative camber → more mid grip"),
         _r("spring_F", "-", 4, "soften front spring"),
         _r("pressure_F", "-", 5, "front pressure toward optimum (usually lower)"),
         _r("caster_F", "+", 6, "more caster → dynamic camber gain"),
         _r("rideheight_F", "-", 7, "lower front / raise rear → more front grip"),
         _r("aero_F", "+", 8, "more front wing (if aero & speed-dependent)")]},
    {"phase": "MID", "symptom": "oversteer",
     "aliases": ["oversteer mid-corner", "steady-state oversteer",
                 "steady state oversteer", "loose mid corner",
                 "rear sliding mid-corner", "too much rotation mid",
                 "rear won't grip mid"],
     "remedies": [
         _r("arb_R", "-", 1, "soften rear ARB → rear keeps grip"),
         _r("arb_F", "+", 2, "stiffen front ARB → shift balance forward"),
         _r("camber_R", "+", 3, "more rear negative camber → more mid grip"),
         _r("spring_R", "-", 4, "soften rear spring"),
         _r("pressure_R", "-", 5, "rear pressure toward optimum"),
         _r("aero_R", "+", 6, "more rear wing (if aero)"),
         _r("diff_preload_R", "+", 7, "more preload → stabilise")]},
    {"phase": "MID", "symptom": "understeer_highspeed",
     "aliases": ["understeer in high-speed corners", "push in fast corners",
                 "no front in high speed", "understeer only in fast corners"],
     "remedies": [
         _r("aero_F", "+", 1, "more front wing → cure aero front deficit (scales v²)"),
         _r("rideheight_R", "+", 2, "raise rear → more rake → more front aero"),
         _r("arb_F", "-", 3, "soften front ARB (mechanical trim)")]},
    {"phase": "MID", "symptom": "oversteer_highspeed",
     "aliases": ["oversteer in high-speed corners", "loose in fast corners",
                 "rear nervous at speed", "oversteer only in fast corners"],
     "remedies": [
         _r("aero_R", "+", 1, "more rear wing → cure high-speed rear deficit"),
         _r("rideheight_R", "-", 2, "lower rear → less rake / steadier platform"),
         _r("spring_R", "+", 3, "stiffen rear spring → platform stability at speed")]},
    {"phase": "MID", "symptom": "understeer_lowspeed",
     "aliases": ["understeer in low-speed corners", "push in slow corners",
                 "won't turn in slow corners", "understeer only in slow corners"],
     "remedies": [
         _r("arb_F", "-", 1, "soften front ARB → mechanical front grip"),
         _r("diff_preload_R", "-", 2, "reduce preload → more low-speed rotation"),
         _r("diff_coast_R", "-", 3, "reduce coast lock → more rotation"),
         _r("toe_F", "-", 4, "more front toe-OUT → sharper turn-in"),
         _r("camber_F", "+", 5, "more front negative camber")]},
    {"phase": "MID", "symptom": "oversteer_lowspeed",
     "aliases": ["oversteer in low-speed corners", "loose in slow corners",
                 "rear loose slow corners", "oversteer only in slow corners"],
     "remedies": [
         _r("arb_R", "-", 1, "soften rear ARB → less low-speed oversteer"),
         _r("diff_preload_R", "+", 2, "more preload → stabilise"),
         _r("toe_R", "+", 3, "more rear toe-IN → calmer rear")]},

    # ------------------------------ C. EXIT ----------------------------- #
    {"phase": "EXIT", "symptom": "oversteer",
     "aliases": ["power oversteer", "rear loose on throttle", "lights up on exit",
                 "wheelspin on exit", "rear steps out on power",
                 "loose on throttle", "oversteer on exit", "snaps on power",
                 "rear loose on power"],
     "remedies": [
         _r("diff_power_R", "+", 1, "more power-ramp lock → put the power down"),
         _r("arb_R", "-", 2, "soften rear ARB → rear keeps grip"),
         _r("bump_R", "-", 3, "soften rear bump → softer squat, more contact on pickup"),
         _r("spring_R", "-", 4, "soften rear spring"),
         _r("pressure_R", "-", 5, "rear pressure toward optimum"),
         _r("aero_R", "+", 6, "more rear wing (if aero & high-speed)"),
         _r("toe_R", "+", 7, "more rear toe-IN → traction")]},
    {"phase": "EXIT", "symptom": "understeer",
     "aliases": ["understeer on exit", "won't rotate on throttle",
                 "pushes wide out of slow corners", "push on exit",
                 "won't rotate on power", "wont rotate on throttle",
                 "pushes wide on exit"],
     "remedies": [
         _r("diff_power_R", "-", 1, "less power lock → frees rotation on throttle"),
         _r("diff_preload_R", "-", 2, "reduce preload → more exit rotation"),
         _r("rebound_F", "-", 3, "soften front rebound → keep front planted as it lightens"),
         _r("arb_R", "+", 4, "stiffen rear ARB → rotate the car"),
         _r("rideheight_R", "+", 5, "raise rear → more front bite")]},
    {"phase": "EXIT", "symptom": "inside_wheelspin",
     "aliases": ["inside wheel spinning", "no traction off slow corners",
                 "inside tyre lights up", "spinning inside wheel",
                 "inside wheel spins"],
     "remedies": [
         _r("diff_preload_R", "+", 1, "more preload → load the inside wheel"),
         _r("diff_power_R", "+", 2, "more power lock → reduce inside spin"),
         _r("rebound_R", "-", 3, "soften rear rebound → reduce inside-wheel lift")]},
    {"phase": "EXIT", "symptom": "poor_drive",
     "aliases": ["bogs on exit", "poor drive", "traction inconsistent on exit",
                 "no drive off the corner", "won't hook up on exit"],
     "remedies": [
         _r("bump_R", "-", 1, "soften rear bump → better compliance under squat"),
         _r("spring_R", "-", 2, "soften rear spring"),
         _r("diff_power_R", "+", 3, "more power lock → consistent drive")]},

    # ---------------------------- D. BRAKING ---------------------------- #
    {"phase": "BRAKING", "symptom": "front_lock",
     "aliases": ["front locks under braking", "front lockup", "fronts locking",
                 "front wheels lock", "locking the fronts"],
     "remedies": [
         _r("brakebias", "-", 1, "brake bias rearward → less front lock"),
         _r("bump_F", "+", 2, "more front bump → slow the dive, keep load progressive"),
         _r("pressure_F", "-", 3, "front pressure toward optimum")]},
    {"phase": "BRAKING", "symptom": "rear_lock",
     "aliases": ["rear locks under braking", "rear lockup", "rears locking",
                 "rear wheels lock", "locking the rears"],
     "remedies": [
         _r("brakebias", "+", 1, "brake bias forward → stabilise rear"),
         _r("rebound_R", "-", 2, "soften rear rebound → rear settles, holds load"),
         _r("toe_R", "+", 3, "more rear toe-IN → stability"),
         _r("diff_coast_R", "+", 4, "more coast lock → stabilise")]},
    {"phase": "BRAKING", "symptom": "dive",
     "aliases": ["car dives under braking", "hits bump stops", "excessive dive",
                 "nose dives braking", "front bottoms under braking",
                 "dives too much"],
     "remedies": [
         _r("bump_F", "+", 1, "more front bump → slow the dive"),
         _r("spring_F", "+", 2, "stiffen front spring"),
         _r("rideheight_F", "+", 3, "raise front ride height")]},
    {"phase": "BRAKING", "symptom": "instability",
     "aliases": ["unstable under braking", "wandering under braking",
                 "instability under heavy braking", "squirms under braking",
                 "rear light under braking", "won't stop straight",
                 "rear unstable under braking", "twitchy under braking"],
     "remedies": [
         _r("rebound_R", "-", 1, "soften rear rebound → rear holds load under braking"),
         _r("brakebias", "+", 2, "brake bias forward → stabilise"),
         _r("diff_coast_R", "+", 3, "more coast lock → stabilise"),
         _r("toe_R", "+", 4, "more rear toe-IN → stability")]},

    # ----------------------------- E. KERB ------------------------------ #
    {"phase": "KERB", "symptom": "skip_understeer",
     "aliases": ["skips over bumps", "deflects over kerbs",
                 "understeers over bumpy turn", "front skips on kerb",
                 "won't follow the surface", "front deflects on bumps",
                 "skates over kerbs"],
     "remedies": [
         _r("bump_F", "-", 1, "soften front (fast) bump → wheel follows surface"),
         _r("spring_F", "-", 2, "soften front spring"),
         _r("rideheight_F", "+", 3, "raise front ride height"),
         _r("rebound_F", "-", 4, "soften front rebound")]},
    {"phase": "KERB", "symptom": "rear_hop",
     "aliases": ["rear hops over kerbs", "rear loses traction over kerbs",
                 "unsettled on exit kerb", "rear skips on kerb",
                 "rear bounces on kerbs", "rear hop"],
     "remedies": [
         _r("rebound_R", "-", 1, "soften rear (fast) rebound → wheel stays down"),
         _r("bump_R", "-", 2, "soften rear bump"),
         _r("spring_R", "-", 3, "soften rear spring"),
         _r("arb_R", "-", 4, "soften rear ARB")]},
    {"phase": "KERB", "symptom": "bottoming",
     "aliases": ["bottoming out", "scraping over crests", "scraping over kerbs",
                 "hitting the floor", "grounding out", "bottoms over crests",
                 "bottoming", "scrapes the floor"],
     "remedies": [
         _r("rideheight_F", "+", 1, "raise front ride height"),
         _r("rideheight_R", "+", 2, "raise rear ride height"),
         _r("spring_F", "+", 3, "stiffen front spring"),
         _r("spring_R", "+", 4, "stiffen rear spring"),
         _r("bump_F", "+", 5, "stiffen front bump (last resort — hurts compliance)")]},
    {"phase": "KERB", "symptom": "float",
     "aliases": ["car floats after kerb", "oscillates after kerb",
                 "wallows after a bump", "keeps bouncing after kerb",
                 "under-damped over kerbs", "floats after the bump"],
     "remedies": [
         _r("rebound_F", "+", 1, "more front rebound → damp the float"),
         _r("rebound_R", "+", 2, "more rear rebound → damp the float")]},
    {"phase": "KERB", "symptom": "harsh_jolt",
     "aliases": ["sharp jolt over kerb", "harsh over kerbs",
                 "single hard hit over kerb", "crashes over kerbs",
                 "jolts over kerbs"],
     "remedies": [
         _r("bump_F", "-", 1, "soften front bump"),
         _r("bump_R", "-", 2, "soften rear bump")]},

    # ---------------------------- F. THERMAL ---------------------------- #
    {"phase": "THERMAL", "symptom": "front_overheat",
     "aliases": ["front tyres overheating", "front tires overheating",
                 "front graining", "greasy front", "front tyres dropping off",
                 "fronts too hot", "front overheating"],
     "remedies": [
         _r("arb_F", "-", 1, "cure the understeer (soften front ARB) → fronts stop scrubbing"),
         _r("pressure_F", "-", 2, "front pressure toward window"),
         _r("camber_F", "+", 3, "if outer edge hot → more negative camber"),
         _r("toe_F", "+", 4, "less front toe-OUT → less heat-scrubbing")]},
    {"phase": "THERMAL", "symptom": "rear_overheat",
     "aliases": ["rear tyres overheating", "rear tires overheating",
                 "rear blistering", "rears dropping off", "rear tyres too hot",
                 "rear graining on exit", "rear overheating"],
     "remedies": [
         _r("diff_power_R", "+", 1, "reduce power oversteer/wheelspin → less rear scrub"),
         _r("pressure_R", "-", 2, "rear pressure toward window"),
         _r("camber_R", "+", 3, "set rear camber per edge temps"),
         _r("toe_R", "-", 4, "less rear toe-IN → less heat-scrubbing")]},
    {"phase": "THERMAL", "symptom": "inner_edge_hot",
     "aliases": ["inner edge hotter", "inside edge of tyre hot",
                 "inner tyre temp high", "too much camber", "inner edge hot"],
     "remedies": [
         _r("camber_F", "-", 1, "less negative front camber (that corner)"),
         _r("camber_R", "-", 2, "less negative rear camber (that corner)")]},
    {"phase": "THERMAL", "symptom": "outer_edge_hot",
     "aliases": ["outer edge hotter", "outside edge of tyre hot",
                 "outer tyre temp high", "not enough camber", "outer edge hot"],
     "remedies": [
         _r("camber_F", "+", 1, "more negative front camber"),
         _r("camber_R", "+", 2, "more negative rear camber"),
         _r("pressure_F", "+", 3, "raise pressure slightly")]},
    {"phase": "THERMAL", "symptom": "pressure_climb",
     "aliases": ["pressures climbing too high", "tyre pressures too high",
                 "tire pressures too high", "whole tyre overheating",
                 "pressure building up", "hot pressures too high"],
     "remedies": [
         _r("pressure_F", "-", 1, "lower front cold/start pressure"),
         _r("pressure_R", "-", 2, "lower rear cold/start pressure")]},
]


# --------------------------------------------------------------------------- #
# match_complaint keyword tables.
# --------------------------------------------------------------------------- #
PHASE_KEYWORDS: dict[str, list[str]] = {
    "ENTRY": ["entry", "turn-in", "turn in", "turning in", "brake-release",
              "brake release", "corner entry", "into the corner", "on turn-in"],
    "MID": ["mid-corner", "mid corner", "midcorner", "apex", "steady state",
            "steady-state", "middle of the corner", "mid "],
    "EXIT": ["exit", "on throttle", "on-throttle", "on the throttle", "on power",
             "on-power", "corner exit", "out of the corner", "acceleration",
             "on the gas"],
    "BRAKING": ["braking", "under braking", "on the brakes", "braking zone",
                "threshold brak", "stopping", "the brakes"],
    "KERB": ["kerb", "curb", "bump", "crest", "ripple", "sausage", "over bumps"],
    "THERMAL": ["overheat", "temp", "temperature", "graining", "blister",
                "greasy", "pressure", "wear", "tyre temp", "tire temp"],
}
_UNDERSTEER_KW = ["understeer", "understeering", "push", "pushes", "pushing",
                  "won't turn", "wont turn", "plows", "plough", "plow", "tight",
                  "not turning", "won't bite", "wont bite", "washes out",
                  "washing out", "scrubs the front", "lazy"]
_OVERSTEER_KW = ["oversteer", "oversteering", "loose", "snap", "snaps",
                 "steps out", "stepping out", "slides", "sliding", "spin",
                 "spinning", "rotates", "fishtail", "tail happy", "lights up",
                 "comes around", "wags"]
_STOP = {"the", "and", "for", "with", "into", "out", "too", "very", "this",
         "that", "over", "under", "from", "your", "its", "a", "an", "on", "in",
         "of", "to", "is", "it"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9' -]", " ", (text or "").lower())


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[\s'-]+", text) if len(w) > 3 and w not in _STOP}


def classify_var(varname: str) -> dict | None:
    """Match a `$var` name to its VAR_SPEC. Returns spec + {var, axle} or None."""
    if not varname:
        return None
    key = varname.strip()
    for pat, spec in VAR_SPECS:
        if re.search(pat, key):
            out = dict(spec)
            out["var"] = key
            m = re.search(r"_([FR])[RL]?(?:_fast)?$", key)
            out["axle"] = m.group(1) if m else None
            return out
    return None


def match_complaint(text: str) -> list[dict]:
    """Driver words -> ranked [{phase, symptom, score}] (score>0, desc)."""
    t = _norm(text)
    tt = " " + t + " "
    toks = _tokens(t)
    phases_hit = {ph for ph, kws in PHASE_KEYWORDS.items()
                  if any(k in tt for k in kws)}
    is_us = any(k in tt for k in _UNDERSTEER_KW)
    is_ovr = any(k in tt for k in _OVERSTEER_KW)
    out: list[dict] = []
    for rule in RULES:
        score = 0.0
        for alias in rule["aliases"]:
            if alias in t:
                score += 5.0
            else:
                shared = toks & _tokens(alias)
                if shared:
                    score += 0.6 * len(shared)
        if rule["phase"] in phases_hit:
            score += 2.0
        if is_us and "understeer" in rule["symptom"]:
            score += 2.0
        if is_ovr and "oversteer" in rule["symptom"]:
            score += 2.0
        if score > 0:
            out.append({"phase": rule["phase"], "symptom": rule["symptom"],
                        "score": round(score, 2)})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out


def remedies_for(phase: str, symptom: str) -> list[dict]:
    """Ordered remedy list for (phase, symptom). Falls back to base us/ovr."""
    ph = (phase or "").upper()
    sym = (symptom or "").lower()
    for rule in RULES:
        if rule["phase"] == ph and rule["symptom"] == sym:
            return [dict(r) for r in rule["remedies"]]
    # fall back: a phase-qualified base symptom for understeer/oversteer
    for base in ("understeer", "oversteer"):
        if base in sym:
            for rule in RULES:
                if rule["phase"] == ph and rule["symptom"] == base:
                    return [dict(r) for r in rule["remedies"]]
    return []


def resolve_lever(lever: str, available_vars: dict) -> dict | None:
    """Resolve a canonical lever to the car's actual `$var`, or None if absent."""
    spec = LEVERS.get(lever)
    if spec is None or not available_vars:
        return None
    for pat in spec["var_patterns"]:
        for key in available_vars:
            if re.search(pat, str(key)):
                return {"lever": lever, "var": key,
                        "stiffer_is_plus": spec["stiffer_is_plus"],
                        "kind": spec["kind"], "confidence": spec["confidence"]}
    return None


# --------------------------------------------------------------------------- #
# Self-test.
# --------------------------------------------------------------------------- #
def _dir_of(phase: str, symptom: str, lever: str) -> str | None:
    for rem in remedies_for(phase, symptom):
        if rem["lever"] == lever:
            return rem["dir"]
    return None


def _selftest() -> None:
    # --- VAR_SPECS / classify_var ---
    assert classify_var("$damp_rebound_R")["kind"] == "rate", "rebound is a rate var"
    assert classify_var("$damp_bump_F_fast")["kind"] == "rate"
    assert classify_var("$arb_spring_F")["kind"] == "rate"
    assert classify_var("$spring_F")["clamp"] == [15000, 160000]
    assert classify_var("$camber_FR")["confidence"] == "low", "camber must be low confidence"
    assert classify_var("$toe_RR")["confidence"] == "low"
    assert classify_var("$caster_FR")["confidence"] == "low"
    assert classify_var("$brakebias")["kind"] == "bias"
    assert classify_var("$lsdlockcoefrev_R")["note"].startswith("coast")
    assert classify_var("$lsdlockcoef_R")["note"].startswith("power")
    assert classify_var("$springheight_F")["kind"] == "height"
    assert classify_var("$not_a_var") is None
    assert classify_var("$arb_spring_R")["axle"] == "R"

    # --- resolve_lever ---
    sample = {"$arb_spring_F": 45000, "$arb_spring_R": 25000,
              "$damp_rebound_F": 18000, "$brakebias": 0.68,
              "$camber_FR": 1.0, "$lsdlockcoef_R": 0.2}
    rl = resolve_lever("arb_F", sample)
    assert rl is not None and rl["var"] == "$arb_spring_F", "must find $arb_spring_F"
    assert rl["kind"] == "rate" and rl["stiffer_is_plus"] is True
    assert resolve_lever("arb_R", sample)["var"] == "$arb_spring_R"
    assert resolve_lever("camber_F", sample)["var"] == "$camber_FR"
    assert resolve_lever("camber_F", sample)["confidence"] == "low"
    assert resolve_lever("diff_power_R", sample)["var"] == "$lsdlockcoef_R"
    assert resolve_lever("spring_F", sample) is None, "car lacks $spring_F here"
    assert resolve_lever("spring_F", {"$arb_spring_F": 1}) is None, \
        "spring_F must NOT match $arb_spring_F"

    # --- master-rule monotonicity (NO sign flips) ---
    # Front ARB stiffer (+) is the remedy for oversteer, NOT understeer.
    assert _dir_of("MID", "oversteer", "arb_F") == "+", "front ARB stiffer cures oversteer"
    assert _dir_of("MID", "understeer", "arb_F") == "-", "front ARB must SOFTEN for understeer"
    # Rear ARB stiffer (+) appears for understeer.
    assert _dir_of("MID", "understeer", "arb_R") == "+", "rear ARB stiffer cures understeer"
    assert _dir_of("MID", "oversteer", "arb_R") == "-"
    # Springs follow the same master rule.
    assert _dir_of("MID", "understeer", "spring_F") == "-"
    assert _dir_of("MID", "oversteer", "spring_R") == "-"
    # Across every entry/mid/exit phase, ARB direction is consistent (no flips).
    for ph in ("ENTRY", "MID", "EXIT"):
        for lev, us_dir, ov_dir in (("arb_F", "-", "+"), ("arb_R", "+", "-")):
            du = _dir_of(ph, "understeer", lev)
            do = _dir_of(ph, "oversteer", lev)
            if du is not None:
                assert du == us_dir, f"{ph}/{lev} understeer dir flipped: {du}"
            if do is not None:
                assert do == ov_dir, f"{ph}/{lev} oversteer dir flipped: {do}"

    # --- brake bias forward for rear instability ---
    assert _dir_of("BRAKING", "instability", "brakebias") == "+", "bias forward for instability"
    assert _dir_of("BRAKING", "rear_lock", "brakebias") == "+"
    assert _dir_of("BRAKING", "front_lock", "brakebias") == "-"

    # --- bump/rebound on the correct axle per phase ---
    assert _dir_of("BRAKING", "dive", "bump_F") == "+", "front bump slows the dive"
    assert _dir_of("EXIT", "oversteer", "bump_R") == "-", "soften rear bump on power oversteer"
    assert _dir_of("ENTRY", "understeer", "rebound_F") == "-"
    assert _dir_of("KERB", "rear_hop", "rebound_R") == "-"
    assert _dir_of("KERB", "float", "rebound_F") == "+", "more rebound to damp float"

    # --- diff phase ownership ---
    assert _dir_of("EXIT", "oversteer", "diff_power_R") == "+", "power lock cures power oversteer"
    assert _dir_of("EXIT", "understeer", "diff_power_R") == "-"

    # --- priority ordering preserved (ascending, ARB first for balance) ---
    mid_us = remedies_for("MID", "understeer")
    assert mid_us[0]["lever"] == "arb_F" and mid_us[0]["priority"] == 1
    assert [r["priority"] for r in mid_us] == sorted(r["priority"] for r in mid_us)

    # --- telemetry-vocabulary symptoms resolve ---
    for ph, sym in (("ENTRY", "understeer"), ("MID", "oversteer"),
                    ("EXIT", "understeer"), ("KERB", "bottoming"),
                    ("BRAKING", "instability")):
        assert remedies_for(ph, sym), f"no remedies for {ph}/{sym}"
    assert _dir_of("KERB", "bottoming", "rideheight_F") == "+"

    # --- match_complaint ---
    m = match_complaint("understeer on entry")
    assert m and m[0]["phase"] == "ENTRY" and m[0]["symptom"] == "understeer", \
        f"entry understeer not top: {m[:2]}"
    m2 = match_complaint("rear loose on throttle")
    assert m2 and m2[0]["phase"] == "EXIT" and m2[0]["symptom"] == "oversteer", \
        f"power oversteer not top: {m2[:2]}"
    m3 = match_complaint("bottoming out over the kerbs")
    assert any(x["phase"] == "KERB" and x["symptom"] == "bottoming" for x in m3)
    assert match_complaint("") == []

    # --- every remedy references a real lever, every lever resolvable spec ---
    for rule in RULES:
        assert rule["phase"] in PHASES
        for rem in rule["remedies"]:
            assert rem["lever"] in LEVERS, f"unknown lever {rem['lever']}"
            assert rem["dir"] in ("+", "-")
    for lev, spec in LEVERS.items():
        for pat in spec["var_patterns"]:
            re.compile(pat)

    print("SELFTEST OK")


if __name__ == "__main__":
    import sys
    _selftest() if "--selftest" in sys.argv else None
