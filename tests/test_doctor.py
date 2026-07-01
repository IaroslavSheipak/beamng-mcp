"""doctor: every check must catch the failure it exists for — above all the
non-numeric protocol-setting corruption that live-broke every vehicle spawn
(REBUILD.md Phase 4) — without ever needing the game."""

import json
import socket

from beamng_mcp.config import Settings
from beamng_mcp.sim import doctor


def _settings(tmp_path, port=1, host="127.0.0.1"):
    game = tmp_path / "game"
    (game / "Bin64").mkdir(parents=True, exist_ok=True)
    user = tmp_path / "user"
    (user / "settings" / "cloud").mkdir(parents=True, exist_ok=True)
    return Settings(
        game_home=str(game), user_folder=str(user), userpath_root=str(tmp_path),
        host=host, port=port, logs_dir=str(tmp_path / "logs"),
    )


def _write_settings(settings, local=None, cloud=None):
    base = settings.user_folder + "/settings"
    if local is not None:
        with open(base + "/settings.json", "w", encoding="utf-8") as fh:
            json.dump(local, fh)
    if cloud is not None:
        with open(base + "/cloud/settings.json", "w", encoding="utf-8") as fh:
            json.dump(cloud, fh)


def _by_name(result, name):
    return [c for c in result["checks"] if c["check"] == name]


def test_the_j_corruption_is_a_fail_with_the_exact_fix(tmp_path):
    s = _settings(tmp_path)
    _write_settings(s, local={"protocols_motionSim_enabled": True,
                              "protocols_motionSim_maxUpdateRate": "j"})
    out = doctor.run_doctor(s, connected=True, probe_outgauge=False)
    assert out["ok"] is False
    fails = [c for c in _by_name(out, "protocol settings") if c["status"] == "fail"]
    assert fails and "maxUpdateRate" in fails[0]["detail"]
    assert "number" in fails[0]["fix"]


def test_numeric_strings_and_ip_strings_are_not_corruption(tmp_path):
    s = _settings(tmp_path)
    _write_settings(
        s,
        local={"protocols_motionSim_enabled": True,
               "protocols_motionSim_port": "4445",       # LuaJIT coerces — fine
               "protocols_motionSim_maxUpdateRate": 60},
        cloud={"protocols_outgauge_enabled": True,
               "protocols_outgauge_ip": "127.0.0.1"},    # legitimately a string
    )
    out = doctor.run_doctor(s, connected=True, probe_outgauge=False)
    assert all(c["status"] == "ok" for c in _by_name(out, "protocol settings"))


def test_motionsim_blank_port_collides_with_outgauge(tmp_path):
    s = _settings(tmp_path)
    _write_settings(s, local={"protocols_motionSim_enabled": True},
                    cloud={"protocols_outgauge_enabled": True})
    out = doctor.run_doctor(s, connected=True, probe_outgauge=False)
    ms = _by_name(out, "MotionSim")[0]
    assert ms["status"] == "warn"
    assert "collide" in ms["detail"]
    assert "4445" in ms["fix"]


def test_outgauge_disabled_is_a_warn_not_a_fail(tmp_path):
    s = _settings(tmp_path)
    _write_settings(s, local={}, cloud={})
    out = doctor.run_doctor(s, connected=True, probe_outgauge=False)
    og = _by_name(out, "OutGauge")[0]
    assert og["status"] == "warn"
    assert out["ok"] is True  # warns never block


def test_tech_socket_listening_and_refused(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        s = _settings(tmp_path, port=port)
        _write_settings(s, local={}, cloud={})
        out = doctor.run_doctor(s, connected=False, probe_outgauge=False)
        assert _by_name(out, "tech socket")[0]["status"] == "ok"
    finally:
        srv.close()
    # now nothing is listening on that port
    out = doctor.run_doctor(_settings(tmp_path, port=port), connected=False,
                            probe_outgauge=False)
    sockcheck = _by_name(out, "tech socket")[0]
    assert sockcheck["status"] == "fail"
    assert "techCore" in sockcheck["fix"]


def test_already_connected_skips_the_probe(tmp_path):
    s = _settings(tmp_path, port=9)  # nothing listens on 9; must not matter
    _write_settings(s, local={}, cloud={})
    out = doctor.run_doctor(s, connected=True, probe_outgauge=False)
    assert _by_name(out, "tech socket")[0]["status"] == "ok"


def test_missing_paths_fail_with_pointers(tmp_path):
    s = Settings(game_home=str(tmp_path / "nope"), user_folder=str(tmp_path / "also_nope"),
                 userpath_root=str(tmp_path), host="127.0.0.1", port=1,
                 logs_dir=str(tmp_path / "logs"))
    out = doctor.run_doctor(s, connected=True, probe_outgauge=False)
    assert out["ok"] is False
    assert _by_name(out, "game install")[0]["status"] == "fail"
    assert _by_name(out, "user folder")[0]["status"] == "fail"
    # missing settings files -> protocol checks degrade to a warn, not a crash
    assert _by_name(out, "protocol settings")[0]["status"] == "warn"
