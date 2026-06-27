"""tune_and_verify.py — live Lua power read (jsonEncode), then apply lifted susp."""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beamngpy import BeamNGpy  # noqa: E402

HOST, PORT = "127.0.0.1", 25252
HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"


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


def luaj(v, code):
    """Run Lua that ends in `return jsonEncode(<table>)`; parse the JSON back."""
    try:
        r = v.queue_lua_command(code, response=True)
    except Exception as e:  # noqa: BLE001
        return {"__err__": repr(e)}
    if isinstance(r, str) and r.strip()[:1] in "{[":
        try:
            return json.loads(r)
        except Exception:  # noqa: BLE001
            return r
    return r


def show(label, x):
    print("\n== %s ==" % label, flush=True)
    print(json.dumps(x, default=str, indent=2)[:1600], flush=True)


bng = BeamNGpy(HOST, PORT, home=HOME, quit_on_close=False)
bng.open(launch=False)
vid = bng.vehicles.get_player_vehicle_id()["vid"]
print("player:", vid, flush=True)
v = reconnect(bng, vid)
if not v:
    print("RESULT: socket still wedged — aborting.", flush=True)
    bng.close()
    sys.exit(1)
print("socket OK", flush=True)

show("engine", luaj(v,
    "local e=powertrain.getDevice('mainEngine') if not e then return jsonEncode({err='none'}) end "
    "local td=e.torqueData or {} return jsonEncode({maxPower=e.maxPower or td.maxPower,"
    "maxTorque=e.maxTorque or td.maxTorque,maxPowerRPM=e.maxPowerRPM or td.maxPowerRPM,"
    "maxTorqueRPM=e.maxTorqueRPM or td.maxTorqueRPM,hasTorqueData=(e.torqueData~=nil),"
    "hasTurbo=(e.turbocharger~=nil),maxRPM=e.maxRPM})"))
show("turbo", luaj(v,
    "local e=powertrain.getDevice('mainEngine') local tc=e and e.turbocharger "
    "if not tc then return jsonEncode({turbo='none'}) end local o={} "
    "for k,val in pairs(tc) do if type(val)=='number' then o[k]=val end end return jsonEncode(o)"))
show("electrics", luaj(v,
    "return jsonEncode({rpm=electrics.values.rpm,boost=electrics.values.boost,"
    "turboBoost=electrics.values.turboBoost,throttle=electrics.values.throttle,"
    "engineLoad=electrics.values.engineLoad,wheelspeed=electrics.values.wheelspeed})"))

print("\n== applying LIFTED suspension ==", flush=True)
cfg = v.get_part_config()
CHANGES = []


def first(suit, subs, ne=None):
    for s in subs:
        for p in suit:
            if s in p and p != ne:
                return p
    return None


def walk(n):
    sid, ch = n.get("id"), n.get("chosenPartName")
    suit = n.get("suitablePartNames") or []
    if sid and suit and any(k in (sid or "").lower()
                            for k in ("strut", "shock", "spring")):
        nw = first(suit, ["_lifted"], ch)
        if nw and nw != ch:
            n["chosenPartName"] = nw
            CHANGES.append((sid, ch, nw))
    for c in (n.get("children") or {}).values():
        walk(c)


walk(cfg["partsTree"])
for sid, o, nw in CHANGES:
    print("  %s: %s -> %s" % (sid, o, nw), flush=True)
if CHANGES:
    v.set_part_config(cfg)
    print("APPLIED — respawns lifted + repaired.", flush=True)
    time.sleep(2.0)

info = bng.vehicles.get_current_info(include_config=True).get(vid, {})
ps = {}


def w2(n):
    if isinstance(n, dict):
        sid, ch = n.get("id"), n.get("chosenPartName")
        if sid and ch and any(k in sid for k in ("strut", "shock", "spring")):
            ps[sid] = ch
        for c in (n.get("children") or {}).values():
            w2(c)


w2(info.get("config", {}).get("partsTree"))
show("verify suspension now", ps)

bng.close()
print("\ndone", flush=True)
