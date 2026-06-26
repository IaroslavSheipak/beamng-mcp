"""rally_build.py — transform the player's current car into a rally build.

Attaches to the running game, reads the live part-config tree, and (over a few
passes so interdependent parts like the front diff resolve after AWD is fitted)
swaps each slot to its rally/offroad/race variant — choosing ONLY from that
slot's own suitablePartNames, so the config always stays valid. Then locks the
diffs for rally and applies. Leaves the game running.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beamngpy import BeamNGpy  # noqa: E402

HOST, PORT = "127.0.0.1", 25252
HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"


def first(suit, subs, not_eq=None):
    for sub in subs:
        for p in suit:
            if sub in p and p != not_eq:
                return p
    return None


def side_of(sid, chosen):
    s = (sid or "") + " " + (chosen or "")
    if "_F" in s or "front" in s.lower():
        return "_F"
    if "_R" in s or "rear" in s.lower():
        return "_R"
    return ""


def choose(sid, chosen, suit):
    low = ((sid or "") + " " + (chosen or "")).lower()
    if "transfer_case" in low:                     # RWD -> AWD
        return first(suit, ["transfer_case_AWD"], chosen)
    if "wheel" in low and chosen:                  # offroad wheels (prefer 17", then 15")
        side = side_of(sid, chosen)
        for size in ("17x9", "17x7", "15x9", "15x8", "15x6"):
            for p in suit:
                if "offroadwheel" in p and size in p and (side == "" or p.endswith(side)):
                    return p if p != chosen else None
        return None
    if any(k in low for k in ("strut", "shock", "spring", "suspension")):
        return first(suit, ["_rally"], chosen)     # rally susp only (race would lower it)
    if "rollcage" in low or "cage" in low:
        return first(suit, ["rollcage", "cage"], chosen)
    if any(k in low for k in ("differential", "brake", "swaybar", "radiator",
                              "strutbrace", "seat")):
        return first(suit, ["_rally", "_race", "_tt"], chosen)
    return None


CHANGES = []


def walk(node):
    sid, chosen = node.get("id"), node.get("chosenPartName")
    suit = node.get("suitablePartNames") or []
    if sid is not None and suit:
        new = choose(sid, chosen, suit)
        if new and new != chosen:
            node["chosenPartName"] = new
            CHANGES.append((sid, chosen or "(empty)", new))
    for child in (node.get("children") or {}).values():
        walk(child)


def reconnect(bng, vid):
    for _ in range(6):
        try:
            v = bng.vehicles.get_current(include_config=False).get(vid)
            v.connect(bng)
            return v
        except Exception as e:  # noqa: BLE001 — priming retry
            print("  (reconnect retry: %r)" % e, flush=True)
            time.sleep(1.0)
    return None


def tune_vars(vd):
    """Conservative rally tune: lock the diffs. Geometry/ride-height come from the
    rally suspension parts. Only touch vars that exist and are clearly bounded."""
    changed = {}
    for k in list(vd.keys()):
        kl = k.lower()
        if "lsdlockcoef" in kl and "rev" not in kl:   # 0..1 lock coefficient
            vd[k], changed[k] = 0.55, 0.55
        elif "lsdpreload" in kl:
            vd[k], changed[k] = 150.0, 150.0
    return changed


bng = BeamNGpy(HOST, PORT, home=HOME, quit_on_close=False)
bng.open(launch=False)
print("connected", flush=True)
vid = bng.vehicles.get_player_vehicle_id()["vid"]
print("player vehicle:", vid, flush=True)
v = reconnect(bng, vid)
if v is None:
    print("FAILED to connect to vehicle", flush=True)
    sys.exit(1)

before = v.get_part_config()
print("starting config:", before.get("partConfigFilename"), flush=True)

for p in range(1, 4):
    CHANGES.clear()
    cfg = v.get_part_config()
    walk(cfg["partsTree"])
    if not CHANGES:
        print("pass %d: converged (no further suitable swaps)" % p, flush=True)
        break
    print("=== pass %d: %d part swaps ===" % (p, len(CHANGES)), flush=True)
    for sid, old, new in CHANGES:
        print("  %-26s %s -> %s" % (sid, old, new), flush=True)
    v.set_part_config(cfg)
    print("  applied, respawning...", flush=True)
    time.sleep(3.0)
    v = reconnect(bng, vid) or v

# Rally tune (diff lock) on the final, fully-parted config.
cfg = v.get_part_config()
tuned = tune_vars(cfg.get("vars") or {})
if tuned:
    print("=== tuning vars ===", flush=True)
    for k, val in tuned.items():
        print("  %-22s -> %s" % (k, val), flush=True)
    v.set_part_config(cfg)
    time.sleep(3.0)
    v = reconnect(bng, vid) or v

# Verify
final = v.get_part_config()
print("=== FINAL key parts ===", flush=True)


def find(node, want, out):
    sid = node.get("id")
    if sid and want in sid:
        out.append((sid, node.get("chosenPartName")))
    for c in (node.get("children") or {}).values():
        find(c, want, out)


seen = set()
for key in ("transfer", "strut", "shock", "spring", "wheel", "differential",
            "brake", "swaybar", "rollcage", "radiator", "seat_F"):
    out = []
    find(final["partsTree"], key, out)
    for sid, val in out:
        if sid not in seen and val:
            seen.add(sid)
            print("  %-26s %s" % (sid, val), flush=True)

bng.close()  # quit_on_close=False -> drops socket, leaves the game running
print("done", flush=True)
