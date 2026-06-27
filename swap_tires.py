"""swap_tires.py — drop the 33" offroad tires to a smaller (~29") offroad tire
to undo the ~30% gearing penalty, keeping offroad capability."""
import os
import sys
import re
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beamngpy import BeamNGpy  # noqa: E402

HOST, PORT = "127.0.0.1", 25252
HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"
RE = re.compile(r"tire_[FR]_(\d+)_\d+_\d+_offroad")


def reconnect(bng, vid):
    for _ in range(8):
        try:
            v = bng.vehicles.get_current(include_config=False).get(vid)
            v.connect(bng)
            return v
        except Exception as e:  # noqa: BLE001
            print("  (retry: %r)" % e, flush=True)
            time.sleep(1.0)
    return None


def pick_smaller(chosen, suit):
    m = RE.match(chosen or "")
    cur = int(m.group(1)) if m else None
    opts = [(int(mm.group(1)), p) for p in suit for mm in [RE.match(p)] if mm]
    smaller = [(d, p) for d, p in opts if cur is None or d < cur]
    if not smaller:
        return None
    return min(smaller, key=lambda dp: abs(dp[0] - 29))[1]  # closest to ~29"


bng = BeamNGpy(HOST, PORT, home=HOME, quit_on_close=False)
bng.open(launch=False)
vid = bng.vehicles.get_player_vehicle_id()["vid"]
print("player:", vid, flush=True)
v = reconnect(bng, vid)
if not v:
    print("RESULT: socket wedged — recover the car in-game and retry.", flush=True)
    bng.close()
    sys.exit(1)

cfg = v.get_part_config()
CHANGES = []


def walk(n, apply=False):
    sid, ch = n.get("id"), n.get("chosenPartName")
    suit = n.get("suitablePartNames") or []
    if sid and "tire" in sid and suit:
        offroad = [p for p in suit if "offroad" in p]
        if not apply:
            print("  %s chosen=%s" % (sid, ch), flush=True)
            print("     offroad opts: %s" % offroad, flush=True)
        else:
            nw = pick_smaller(ch, suit)
            if nw and nw != ch:
                n["chosenPartName"] = nw
                CHANGES.append((sid, ch, nw))
    for c in (n.get("children") or {}).values():
        walk(c, apply)


print("\n== tire options ==", flush=True)
walk(cfg["partsTree"], apply=False)
print("\n== applying smaller offroad tires ==", flush=True)
walk(cfg["partsTree"], apply=True)
for sid, o, nw in CHANGES:
    print("  %s: %s -> %s" % (sid, o, nw), flush=True)
if CHANGES:
    v.set_part_config(cfg)
    print("APPLIED.", flush=True)
    time.sleep(2.0)
else:
    print("no smaller offroad tire found in those slots.", flush=True)

info = bng.vehicles.get_current_info(include_config=True).get(vid, {})
ts = {}


def w2(n):
    if isinstance(n, dict):
        sid, ch = n.get("id"), n.get("chosenPartName")
        if sid and "tire" in sid and ch:
            ts[sid] = ch
        for c in (n.get("children") or {}).values():
            w2(c)


w2(info.get("config", {}).get("partsTree"))
print("\nverify tires:", ts, flush=True)
bng.close()
print("done", flush=True)
