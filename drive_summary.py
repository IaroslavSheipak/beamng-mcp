"""drive_summary.py — summarize an OutGauge drive log CSV."""
import csv
import os
import sys
from collections import defaultdict

try:  # Windows consoles often default to a non-UTF-8 codepage (e.g. cp1251)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "current_drive.csv")

rows = []
with open(path) as fh:
    for r in csv.DictReader(fh):
        try:
            rows.append({
                "t": float(r["t"]), "speed": float(r["speed_kmh"]),
                "rpm": float(r["rpm"]), "gear": r["gear"],
                "throttle": float(r["throttle"]), "brake": float(r["brake"]),
                "fuel": float(r["fuel"]), "eng": float(r["engTemp"]),
            })
        except (ValueError, KeyError):
            continue

if not rows:
    print("no samples in", path)
    sys.exit(0)

n = len(rows)
dur = rows[-1]["t"] - rows[0]["t"]
speeds = [x["speed"] for x in rows]
top, avg = max(speeds), sum(speeds) / n
maxrpm = max(x["rpm"] for x in rows)
maxeng = max(x["eng"] for x in rows)

dist = dt_tot = thr_t = brk_t = 0.0
gear_time = defaultdict(float)
for a, b in zip(rows, rows[1:]):
    dt = b["t"] - a["t"]
    if not (0 < dt < 1):
        continue
    dt_tot += dt
    dist += (a["speed"] / 3.6) * dt
    if a["throttle"] > 0.5:
        thr_t += dt
    if a["brake"] > 0.1:
        brk_t += dt
    gear_time[a["gear"]] += dt

# best 0->100 km/h from any near-standstill launch
z100 = None
launch = None
for x in rows:
    if x["speed"] < 3:
        launch = x["t"]
    elif x["speed"] >= 100 and launch is not None:
        z = x["t"] - launch
        z100 = z if z100 is None else min(z100, z)
        launch = None

fuel_used = rows[0]["fuel"] - rows[-1]["fuel"]


def gname(g):
    try:
        gi = int(float(g))
    except ValueError:
        return str(g)
    return "R" if gi == 0 else ("N" if gi == 1 else str(gi - 1))


# speed-over-time sparkline (~56 buckets)
blocks = " ▁▂▃▄▅▆▇█"
bn = 56
per = dur / bn if dur else 0
bk = [[] for _ in range(bn)]
for x in rows:
    i = min(bn - 1, int((x["t"] - rows[0]["t"]) / per)) if per else 0
    bk[i].append(x["speed"])
bavg = [(sum(b) / len(b)) if b else 0 for b in bk]
mx = max(bavg) or 1
spark = "".join(blocks[min(8, int(v / mx * 8))] for v in bavg)

print("== DRIVE SUMMARY : %s ==" % os.path.basename(path))
print("  samples       : %d  (~%.0f Hz)" % (n, n / dur if dur else 0))
print("  duration      : %.1f s  (%.1f min)" % (dur, dur / 60))
print("  distance      : %.2f km" % (dist / 1000))
print("  top speed     : %.1f km/h" % top)
print("  avg speed     : %.1f km/h" % avg)
print("  max rpm       : %.0f" % maxrpm)
print("  0-100 km/h    : %s" % ("%.2f s" % z100 if z100 else "n/a (no clean launch)"))
print("  on throttle   : %.0f%%  (%.1fs)" % (100 * thr_t / dt_tot if dt_tot else 0, thr_t))
print("  on brakes     : %.0f%%  (%.1fs)" % (100 * brk_t / dt_tot if dt_tot else 0, brk_t))
print("  fuel burned   : %.1f%% of tank" % (100 * fuel_used))
print("  max eng temp  : %.0f C" % maxeng)
gd = sorted(gear_time.items(), key=lambda kv: -kv[1])
print("  gear usage    : " + ", ".join(
    "%s=%.0f%%" % (gname(g), 100 * t / dt_tot) for g, t in gd if dt_tot and t / dt_tot > 0.02))
print("  speed trace   : |%s|" % spark)
print("                  0%s%d km/h, left=start  right=end"
      % (" " * 48, round(top)))
