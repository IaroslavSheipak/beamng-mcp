import pytest

from beamng_mcp.sim import pc_config


def test_write_then_read_roundtrip(tmp_path):
    root = str(tmp_path)
    cfg = {"format": 2, "model": "etk800", "parts": {}, "vars": {"$x": 1}}
    res = pc_config.write_pc("etk800", "myrace", cfg, root=root)
    assert res["ok"] is True
    back = pc_config.read_pc("etk800", "myrace", root=root)
    assert back["ok"] is True
    assert back["data"]["vars"]["$x"] == 1


def test_list_pc_finds_written(tmp_path):
    root = str(tmp_path)
    pc_config.write_pc("etk800", "a", {"parts": {}}, root=root)
    pc_config.write_pc("etk800", "b", {"parts": {}}, root=root)
    names = {c["name"] for c in pc_config.list_pc(root=root)}
    assert names == {"a", "b"}


def test_read_missing_is_not_ok(tmp_path):
    res = pc_config.read_pc("etk800", "nope", root=str(tmp_path))
    assert res["ok"] is False


@pytest.mark.parametrize("bad", ["../evil", "a/b", "c:d", ".."])
def test_confinement_rejects_traversal(tmp_path, bad):
    with pytest.raises(ValueError):
        pc_config.confined_target("etk800", bad, root=str(tmp_path))
    with pytest.raises(ValueError):
        pc_config.confined_target(bad, "ok", root=str(tmp_path))


def test_write_rejects_traversal(tmp_path):
    res = pc_config.write_pc("etk800", "../evil", {"parts": {}}, root=str(tmp_path))
    assert res["ok"] is False
