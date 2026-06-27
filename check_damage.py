"""check_damage.py — read suspension damage after a jump."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beamngpy import BeamNGpy  # noqa: E402
from beamngpy.sensors import Damage  # noqa: E402

HOST, PORT = "127.0.0.1", 25252
HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"

bng = BeamNGpy(HOST, PORT, home=HOME, quit_on_close=False)
bng.open(launch=False)
vid = bng.vehicles.get_player_vehicle_id()["vid"]
v = None
for _ in range(8):
    try:
        v = bng.vehicles.get_current(include_config=False).get(vid)
        v.connect(bng)
        break
    except Exception as e:  # noqa: BLE001
        print("  (retry: %r)" % e, flush=True)
        time.sleep(1.0)
        v = None
if v is None:
    print("RESULT: socket wedged — can't read damage. Did it land intact?", flush=True)
    bng.close()
    sys.exit(1)

v.attach_sensor("damage", Damage())
v.poll_sensors()
d = dict(v.sensors["damage"])
dgd = d.get("deform_group_damage") or {}
pd = d.get("part_damage") or {}
susp_groups = {k: round(x.get("damage", 0), 4) for k, x in dgd.items()
               if isinstance(x, dict) and any(t in k.lower()
                                              for t in ("susp", "steer", "axle"))}
susp_parts = {k: round(x.get("damage", 0), 3) for k, x in pd.items()
              if isinstance(x, dict) and x.get("damage", 0) > 0
              and any(t in k.lower()
                      for t in ("susp", "strut", "steer", "wheel", "shock", "spring"))}
print("total damage : %s" % d.get("damage"), flush=True)
print("susp groups  : %s" % susp_groups, flush=True)
print("damaged susp : %s" % (susp_parts or "none"), flush=True)
print("all damaged  : %s" % {k: round(x.get("damage", 0), 2) for k, x in pd.items()
                             if isinstance(x, dict) and x.get("damage", 0) > 0}, flush=True)
bng.close()
print("done", flush=True)
