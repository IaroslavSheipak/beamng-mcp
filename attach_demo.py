"""attach_demo.py — passive attach demo (does NOT launch or close your game).

Start BeamNG.drive yourself first (with -tcom -tport 25252), get in a car, then:
    .venv\\Scripts\\python.exe attach_demo.py

It attaches to your running game, lists the current vehicles, reads telemetry of
the car you're driving, prints the key channels, and detaches — leaving your game
running untouched.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session as S  # noqa: E402


def show(label, d):
    print("\n===== %s =====" % label, flush=True)
    print(json.dumps(d, default=str, indent=2)[:3000], flush=True)


def main():
    r = S.session.connect(launch=False)  # ATTACH only — never launches
    show("connect", r)
    if not r.get("ok"):
        print(">>> Could not attach. Is BeamNG running with '-tcom -tport 25252'?",
              flush=True)
        return 1

    show("current_vehicles", S.session.current_vehicles())

    t = S.session.telemetry()  # the car you're currently driving
    if t.get("ok"):
        e = t.get("electrics", {})
        keys = ("rpm", "wheelspeed", "gear", "throttle", "brake", "fuel",
                "water_temperature", "oil_temperature", "engine_load", "boost")
        show("telemetry (key channels)",
             {"vid": t.get("vid"),
              "electrics": {k: e.get(k) for k in keys},
              "state": t.get("state")})
    else:
        show("telemetry", t)

    # quit_on_close=False for attached sessions -> this only drops the socket.
    show("disconnect (leaves your game running)", S.session.disconnect())
    return 0


if __name__ == "__main__":
    sys.exit(main())
