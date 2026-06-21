"""Regression / smoke tests for the GOD Mode add-on pure logic.

Every module is imported without HA / MQTT / network: behaviour is asserted on
in-memory inputs (or files in a tmp data dir). These pin down the value-deriving
code so refactors can't silently change it.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from conftest import load_module


# --------------------------------------------------------------------------- #
#  Importability — the testability invariant from the QA standard.            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fname",
    ["god-collector.py", "god-pve-sync.py", "god-webui.py", "god-generate-yaml.py"],
)
def test_modules_import_without_side_effects(fname, tmp_path):
    mod = load_module(fname, data_dir=str(tmp_path))
    assert mod is not None


# --------------------------------------------------------------------------- #
#  god-collector.py                                                            #
# --------------------------------------------------------------------------- #
def _collector(tmp_path):
    return load_module("god-collector.py", data_dir=str(tmp_path))


def test_collector_data_dir_is_configurable(tmp_path):
    col = _collector(tmp_path)
    assert col.DATA_DIR == Path(str(tmp_path))
    assert col.METRICS_DIR == Path(str(tmp_path)) / "metrics"


def test_load_json_missing_and_malformed(tmp_path):
    col = _collector(tmp_path)
    assert col._load_json(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert col._load_json(bad) == {}


def test_load_metrics_includes_unpolled_hosts_as_offline(tmp_path):
    col = _collector(tmp_path)
    # categories.json lists a host that has not produced metrics yet.
    (tmp_path / "ansible").mkdir(parents=True)
    (tmp_path / "ansible" / "categories.json").write_text(json.dumps({"zeratul": "pve_node"}))
    out = col.load_metrics_from_disk()
    assert out["zeratul"]["_ok"] is False
    assert out["zeratul"]["_error"] == "not_polled_yet"
    assert out["zeratul"]["_category"] == "pve_node"


def test_load_metrics_marks_error_payload_not_ok(tmp_path):
    col = _collector(tmp_path)
    (tmp_path / "metrics").mkdir(parents=True)
    # A host that replied OK...
    (tmp_path / "metrics" / "plex.json").write_text(json.dumps({"cpu_pct": 12.5}))
    # ...and one whose gather reported an error.
    (tmp_path / "metrics" / "frigate.json").write_text(json.dumps({"_error": "ssh timeout"}))
    out = col.load_metrics_from_disk()
    assert out["plex"]["_ok"] is True
    assert out["frigate"]["_ok"] is False  # presence of _error forces not-ok


def test_record_history_skips_not_ok_and_non_numeric(tmp_path):
    col = _collector(tmp_path)
    col.HISTORY.clear()
    col.record_history(
        {
            "ok_host": {"_ok": True, "cpu_pct": "7.5", "mem_pct": None, "temp_max_c": "n/a"},
            "down_host": {"_ok": False, "cpu_pct": 99.0},
        }
    )
    assert "down_host" not in col.HISTORY  # not-ok hosts are not recorded
    series = col.HISTORY["ok_host"]
    assert series["cpu_pct"][-1][1] == 7.5  # numeric string coerced to float
    assert "mem_pct" not in series  # None skipped
    assert "temp_max_c" not in series  # non-numeric skipped


def test_record_history_ring_buffer_caps(tmp_path):
    col = _collector(tmp_path)
    col.HISTORY.clear()
    col.HISTORY_MAX_POINTS = 5  # shrink for the test
    for i in range(20):
        col.record_history({"h": {"_ok": True, "cpu_pct": float(i)}})
    series = col.HISTORY["h"]["cpu_pct"]
    assert len(series) == 5
    assert [v for _, v in series] == [15.0, 16.0, 17.0, 18.0, 19.0]


def test_load_pve_children_skips_summary(tmp_path):
    col = _collector(tmp_path)
    pcd = tmp_path / "pve_children"
    pcd.mkdir(parents=True)
    (pcd / "_summary.json").write_text(json.dumps({"nodes": []}))
    (pcd / "zeratul.json").write_text(json.dumps({"node": "zeratul", "ok": True}))
    out = col.load_pve_children()
    assert set(out.keys()) == {"zeratul"}


# --------------------------------------------------------------------------- #
#  god-pve-sync.py — fetch_node_snapshot (network call monkeypatched out)      #
# --------------------------------------------------------------------------- #
def _fake_cluster_resources():
    return {
        "data": [
            {"type": "node", "node": "zeratul", "status": "online",
             "cpu": 0.25, "mem": 4_000_000_000, "maxmem": 8_000_000_000,
             "disk": 50_000_000_000, "maxdisk": 100_000_000_000, "uptime": 3600},
            {"type": "node", "node": "other", "status": "online", "cpu": 0.1},
            {"type": "qemu", "node": "zeratul", "vmid": 100, "name": "ha",
             "status": "running", "cpu": 0.5, "mem": 1_000_000_000,
             "maxmem": 2_000_000_000, "disk": 0, "maxdisk": 0, "uptime": 60},
            {"type": "lxc", "node": "zeratul", "vmid": 104, "name": "jarvis",
             "status": "stopped", "cpu": 0.0, "mem": 0, "maxmem": 0},
            {"type": "qemu", "node": "other", "vmid": 999, "name": "elsewhere",
             "status": "running"},  # belongs to another node, must be excluded
        ]
    }


def test_fetch_node_snapshot_parses_node_and_guests(monkeypatch, tmp_path):
    pve = load_module("god-pve-sync.py", data_dir=str(tmp_path))
    monkeypatch.setattr(pve, "pve_get", lambda *a, **k: _fake_cluster_resources())
    snap = pve.fetch_node_snapshot("zeratul", "192.168.1.122", "god@pve!t", "secret")

    assert snap["ok"] is True
    assert snap["node"] == "zeratul"
    assert snap["node_status"]["cpu_pct"] == 25.0           # 0.25 * 100
    assert snap["node_status"]["mem_pct"] == 50.0           # 4/8
    assert snap["node_status"]["disk_pct"] == 50.0          # 50/100

    # Only zeratul's guests, foreign-node guest excluded.
    vmids = {g["vmid"] for g in snap["guests"]}
    assert vmids == {100, 104}
    assert snap["counts"] == {"total": 2, "running": 1, "stopped": 1, "vms": 1, "lxcs": 1}
    # Running guests sort before stopped ones.
    assert snap["guests"][0]["status"] == "running"
    # Zero maxmem -> mem_pct None instead of ZeroDivisionError.
    stopped = next(g for g in snap["guests"] if g["vmid"] == 104)
    assert stopped["mem_pct"] is None


def test_fetch_node_snapshot_handles_api_failure(monkeypatch, tmp_path):
    pve = load_module("god-pve-sync.py", data_dir=str(tmp_path))
    monkeypatch.setattr(pve, "pve_get", lambda *a, **k: None)
    snap = pve.fetch_node_snapshot("zeratul", "192.168.1.122", "id", "secret")
    assert snap["ok"] is False
    assert "error" in snap


# --------------------------------------------------------------------------- #
#  god-generate-yaml.py — gen_package produces valid YAML                      #
# --------------------------------------------------------------------------- #
def test_gen_package_is_valid_yaml_with_expected_sensors(tmp_path):
    yaml = importlib.import_module("yaml")
    gen = load_module("god-generate-yaml.py", data_dir=str(tmp_path))
    hosts = [
        {"name": "zeratul", "category": "pve_node", "addr": "192.168.1.122"},
        {"name": "plex", "category": "server", "addr": "192.168.1.123", "parent": "zeratul"},
    ]
    text = gen.gen_package(hosts)
    doc = yaml.safe_load(text)  # raises on malformed YAML -> regression guard
    assert "rest" in doc and "template" in doc
    # Every host gets its 4 metric sensors + an online_raw sensor.
    rest_sensor_names = {s["name"] for s in doc["rest"][0]["sensor"]}
    assert "god_host_zeratul_cpu" in rest_sensor_names
    assert "god_host_plex_online_raw" in rest_sensor_names


def test_gen_package_empty_hosts_still_valid_yaml(tmp_path):
    yaml = importlib.import_module("yaml")
    gen = load_module("god-generate-yaml.py", data_dir=str(tmp_path))
    doc = yaml.safe_load(gen.gen_package([]))
    assert "rest" in doc


# --------------------------------------------------------------------------- #
#  god-webui.py — pure helpers (no socket / no playbook)                       #
# --------------------------------------------------------------------------- #
def test_send_wol_rejects_invalid_mac(tmp_path):
    web = load_module("god-webui.py", data_dir=str(tmp_path))
    ok, msg = web.send_wol("zz:zz")  # too short / not hex
    assert ok is False
    assert "invalid MAC" in msg


def test_power_action_unknown_host(tmp_path):
    web = load_module("god-webui.py", data_dir=str(tmp_path))
    # Point OPTS_FILE at a tmp options.json with no matching host.
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"hosts": [{"name": "plex"}]}))
    web.OPTS_FILE = opts
    code, msg = web.power_action("ghost", "wake")
    assert code == 404
    assert "not in inventory" in msg


def test_power_action_unknown_action(tmp_path):
    web = load_module("god-webui.py", data_dir=str(tmp_path))
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"hosts": [{"name": "plex", "mac": "aa:bb:cc:dd:ee:ff"}]}))
    web.OPTS_FILE = opts
    code, msg = web.power_action("plex", "explode")
    assert code == 400
    assert "unknown action" in msg


def test_power_action_wake_without_mac(tmp_path):
    web = load_module("god-webui.py", data_dir=str(tmp_path))
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"hosts": [{"name": "plex"}]}))
    web.OPTS_FILE = opts
    code, msg = web.power_action("plex", "wake")
    assert code == 400
    assert "no 'mac'" in msg
