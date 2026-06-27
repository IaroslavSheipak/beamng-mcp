"""improve_car.py — swap the rally suspension to LIFTED (taller + long-travel)
so the struts stop bottoming on jumps. Config writes are GE-side; tries a normal
vehicle connect first, then a GE-api-only fallback if the sensor socket is wedged.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beamngpy import BeamNGpy  # noqa: E402

HOST, PORT = "127.0.0.1", 25252
HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"


def get_handle(bng, vid):
    # 1) normal connect (also gives the sensor socket)
    for _ in range(4):
        try:
            v = bng.vehicles.get_current(include_config=False).get(vid)
            v.connect(bng)
            return v, "socket"
        except Exception as e:  # noqa: BLE001
            print("  (connect retry: %r)" % e, flush=True)
            time.sleep(1.0)
    # 2) GE-api only — enough for get/set_part_config without the sensor socket
    try:
        v = bng.vehicles.get_current(include_config=False).get(vid)
        v.bng = bng
        v._init_beamng_api(bng)
        v.get_part_config()  # probe
        return v, "ge-only"
    except Exception as e:  # noqa: BLE001
        print("  ge-only fallback failed: %r" % e, flush=True)
        return None, None


def first(suit, subs, ne=None):
    for s in subs:
        for p in suit:
            if s in p and p != ne:
                return p
    return None


CHANGES = []


def choose(sid, chosen, suit):
    low = ((sid or "") + " " + (chosen or "")).lower()
    if any(k in low for k in ("strut", "shock", "spring")):
        return first(suit, ["_lifted"], chosen)
    return None


def walk(n):
    sid, ch = n.get("id"), n.get("chosenPartName")
    suit = n.get("suitablePartNames") or []
    if sid and suit:
        nw = choose(sid, ch, suit)
        if nw and nw != ch:
            n["chosenPartName"] = nw
            CHANGES.append((sid, ch or "(empty)", nw))
    for c in (n.get("children") or {}).values():
        walk(c)


bng = BeamNGpy(HOST, PORT, home=HOME, quit_on_close=False)
bng.open(launch=False)
vid = bng.vehicles.get_player_vehicle_id()["vid"]
print("player:", vid, flush=True)
v, mode = get_handle(bng, vid)
if v is None:
    print("RESULT: could not get a vehicle handle — socket wedged; restart BeamNG.",
          flush=True)
    bng.close()
    sys.exit(1)
print("handle via:", mode, flush=True)

cfg = v.get_part_config()
walk(cfg["partsTree"])
print("=== planned suspension swaps (%d) ===" % len(CHANGES), flush=True)
for sid, o, nw in CHANGES:
    print("  %-24s %s -> %s" % (sid, o, nw), flush=True)
if CHANGES:
    v.set_part_config(cfg)
    print("RESULT: APPLIED (car respawns lifted + repaired).", flush=True)
else:
    print("RESULT: no _lifted options found in those slots.", flush=True)

bng.close()
print("done", flush=True)
