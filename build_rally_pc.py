"""build_rally_pc.py — build a proper rally config (AWD + race diffs + lifted
suspension + offroad wheels + smallest offroad tire + roll cage), apply it live,
and SAVE it as a .pc so it persists across restarts.

Reuses ONE connected Vehicle for every set_part_config so the writes keep working
across respawns (a fresh Vehicle re-runs the handshake that wedges the socket).
"""
import os
import sys
import re
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beamngpy import BeamNGpy  # noqa: E402
import pc_config  # noqa: E402

HOST, PORT = "127.0.0.1", 25252
HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"
CONFIG_NAME = "Claude Rally"
TRE = re.compile(r"tire_[FR]_(\d+)_\d+_\d+_offroad")


def connect_once(bng, vid):
    for _ in range(8):
        try:
            v = bng.vehicles.get_current(include_config=False).get(vid)
            v.connect(bng)
            return v
        except Exception as e:  # noqa: BLE001
            print("  (retry: %r)" % e, flush=True)
            time.sleep(1.0)
    return None


def first(suit, subs, ne=None):
    for s in subs:
        for p in suit:
            if s in p and p != ne:
                return p
    return None


def smallest_offroad_tire(chosen, suit):
    opts = sorted((int(m.group(1)), p) for p in suit for m in [TRE.match(p)] if m)
    return opts[0][1] if opts and opts[0][1] != chosen else None


def choose(sid, chosen, suit):
    low = (sid + " " + (chosen or "")).lower()
    if "transfer_case" in low:
        return first(suit, ["transfer_case_AWD"], chosen)
    if any(k in low for k in ("strut", "shock", "spring")):
        return first(suit, ["_lifted"], chosen)
    if "wheel" in low and chosen:
        side = "_F" if chosen.endswith("_F") else ("_R" if chosen.endswith("_R") else "")
        for size in ("15x8", "15x9", "15x6", "17x7", "17x9"):
            for p in suit:
                if "offroadwheel" in p and size in p and (side == "" or p.endswith(side)):
                    return p if p != chosen else None
        return None
    if "tire" in low:
        return smallest_offroad_tire(chosen, suit)
    if "differential" in low:
        return first(suit, ["_race"], chosen)
    if "rollcage" in low or "cage" in low:
        return first(suit, ["rollcage", "cage"], chosen)
    return None


CHANGES = []


def walk(n):
    sid, ch = n.get("id"), n.get("chosenPartName")
    suit = n.get("suitablePartNames") or []
    if sid and suit:
        nw = choose(sid, ch, suit)
        if nw and nw != ch:
            n["chosenPartName"] = nw
            CHANGES.append((sid, ch or "(none)", nw))
    for c in (n.get("children") or {}).values():
        walk(c)


def flatten(tree):
    out = {}

    def w(n):
        if isinstance(n, dict):
            sid, ch = n.get("id"), n.get("chosenPartName")
            if sid and ch:
                out[sid] = ch
            for c in (n.get("children") or {}).values():
                w(c)
    w(tree)
    return out


bng = BeamNGpy(HOST, PORT, home=HOME, quit_on_close=False)
bng.open(launch=False)
vid = bng.vehicles.get_player_vehicle_id()["vid"]
print("player:", vid, flush=True)
v = connect_once(bng, vid)
if not v:
    print("RESULT: socket wedged — recover the car and retry.", flush=True)
    bng.close()
    sys.exit(1)
print("socket OK (reusing this handle for all writes)", flush=True)

for p in range(1, 6):
    CHANGES.clear()
    cfg = v.get_part_config()
    walk(cfg["partsTree"])
    if not CHANGES:
        print("pass %d: converged" % p, flush=True)
        break
    print("=== pass %d (%d swaps) ===" % (p, len(CHANGES)), flush=True)
    for sid, o, nw in CHANGES:
        print("  %-24s %s -> %s" % (sid, o, nw), flush=True)
    v.set_part_config(cfg)
    time.sleep(2.5)

final = v.get_part_config()
parts = flatten(final["partsTree"])
varz = final.get("vars", {})
print("\n== FINAL key parts ==", flush=True)
for k in sorted(parts):
    if any(t in k for t in ("transfer", "strut", "shock", "spring", "wheel",
                            "tire", "differential", "rollcage", "brake")):
        print("  %-26s %s" % (k, parts[k]), flush=True)

pc = {"format": 2, "model": "etk800", "parts": parts, "vars": varz}
res = pc_config.write_pc("etk800", CONFIG_NAME, pc)
print("\nwrite_pc:", res, flush=True)
bng.close()
print("done", flush=True)
