from beamng_mcp.sim.tuning import _prepare_vars, parts_summary, walk_part_tree

TREE = {
    "id": "root",
    "chosenPartName": "etk800",
    "children": {
        "a": {
            "id": "engine",
            "chosenPartName": "etk_engine_2.0",
            "suitablePartNames": ["etk_engine_2.0", "etk_engine_2.4"],
        },
        "b": {
            "id": "wheels",
            "chosenPartName": "",  # empty -> not an "installed" part
            "children": {"c": {"id": "tire_F", "chosenPartName": "tire_sport"}},
        },
    },
}


def test_walk_visits_every_node():
    seen = []
    walk_part_tree(TREE, lambda n: seen.append(n.get("id")))
    assert seen == ["root", "engine", "wheels", "tire_F"]


def test_parts_summary_installed_only():
    # 'wheels' has an empty chosenPartName -> excluded.
    assert parts_summary(TREE) == {
        "root": "etk800",
        "engine": "etk_engine_2.0",
        "tire_F": "tire_sport",
    }


def test_parts_summary_empty_tree():
    assert parts_summary(None) == {}


def test_prepare_vars_clamps_and_skips():
    applied, skipped = _prepare_vars(
        {"$arb_F": 9.0, "$ride": "x", "notvar": 1, "$free": 2},
        {"$arb_F": (0.0, 3.0)},  # only $arb_F has a known range
    )
    assert applied == {"$arb_F": 3.0, "$free": 2.0}  # $arb_F clamped, $free passes
    assert skipped == {"$ride": "non-numeric", "notvar": "not a $var"}


def test_prepare_vars_all_skipped():
    applied, skipped = _prepare_vars({"x": 1, "$y": None}, {})
    assert applied == {}
    assert set(skipped) == {"x", "$y"}
