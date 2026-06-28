from beamng_mcp.sim.telemetry import compact_damage


def test_compact_damage_filters_to_damaged_parts():
    dmg = {
        "damage": 12.5,
        "lowpressure": False,
        "part_damage": {
            "bumper_F": {"damage": 3.14159},
            "door_FL": {"damage": 0},      # undamaged -> dropped
            "hood": {"damage": 0.5},
        },
    }
    out = compact_damage(dmg)
    assert out["total"] == 12.5
    assert out["damaged_parts"] == {"bumper_F": 3.142, "hood": 0.5}


def test_compact_damage_handles_null_part_damage():
    # The v1 crash: a present-but-null part 'damage' did None > 0 -> TypeError,
    # which silently downgraded the whole rich reading to fallback.
    dmg = {"damage": 1.0, "part_damage": {"x": {"damage": None}, "y": {}}}
    out = compact_damage(dmg)  # must not raise
    assert out["damaged_parts"] == {}


def test_compact_damage_missing_fields():
    out = compact_damage({})
    assert out["total"] is None
    assert out["damaged_parts"] == {}
