"""analyze_car.py — GE-side car analysis (parts + tuning vars). No vehicle socket."""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beamngpy import BeamNGpy  # noqa: E402

HOST, PORT = "127.0.0.1", 25252
HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"

bng = BeamNGpy(HOST, PORT, home=HOME, quit_on_close=False)
bng.open(launch=False)
vid = bng.vehicles.get_player_vehicle_id()["vid"]
print("player:", vid, flush=True)
info = bng.vehicles.get_current_info(include_config=True).get(vid, {})
cfg = info.get("config", {}) or {}


def parts_summary(tree):
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


ps = parts_summary(cfg.get("partsTree"))
print("\nconfig_file:", cfg.get("partConfigFilename"), flush=True)
print("model:", cfg.get("model"), flush=True)

pw = ("engine", "turbo", "ecu", "transmission", "transfer", "differential",
      "intake", "exhaust", "radiator", "intercooler", "fuel", "flywheel",
      "oilpan", "n2o", "enginemount")
sus = ("strut", "shock", "spring", "swaybar", "suspension", "wheel", "tire",
       "brake", "steering", "axle")
print("\n== POWERTRAIN parts ==", flush=True)
for k in sorted(ps):
    if any(t in k for t in pw):
        print("  %-26s %s" % (k, ps[k]), flush=True)
print("\n== SUSPENSION / running gear parts ==", flush=True)
for k in sorted(ps):
    if any(t in k for t in sus):
        print("  %-26s %s" % (k, ps[k]), flush=True)
print("\n== OTHER parts ==", flush=True)
for k in sorted(ps):
    if not any(t in k for t in pw + sus):
        print("  %-26s %s" % (k, ps[k]), flush=True)

print("\n== TUNING VARS (%d) ==" % len(cfg.get("vars") or {}), flush=True)
print(json.dumps(cfg.get("vars") or {}, indent=1), flush=True)

bng.close()
print("\ndone", flush=True)
