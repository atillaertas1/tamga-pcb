"""Devre blokları: açma kuralları (saf) + sync ile uçtan uca."""
import copy
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pcbmcp import blocks, design
from kicad_env import needs_kicad

R = "Resistor_SMD:R_0805_2012Metric"
LED_BLOCK = {"ports": ["VIN", "GND"], "parts": [
    {"ref": "R1", "symbol": "Device:R", "value": "1k", "footprint": R, "pins": {"1": "VIN", "2": "A"}},
    {"ref": "D1", "symbol": "Device:LED", "value": "RED", "footprint": "LED_SMD:LED_0805_2012Metric",
     "pins": {"A": "A", "K": "GND"}}]}


def spec(*instances, parts=()):
    return {"title": "blk", "board": {"width_mm": 40, "height_mm": 30}, "blocks": {"led": copy.deepcopy(LED_BLOCK)},
            "parts": [*parts], "instances": [*instances]}


def test_expand_ports_local_nets_and_refs():
    flat = blocks.expand(spec({"block": "led", "name": "PWR", "nets": {"VIN": "+5V"}},
                              {"block": "led", "name": "STAT", "set": {"R1": {"value": "330"}},
                               "refs": {"D1": "D7"}}))
    by = {p["ref"]: p for p in flat["parts"]}
    assert sorted(by) == ["D101", "D7", "R101", "R201"]
    assert by["R101"]["pins"] == {"1": "+5V", "2": "PWR_A"}       # eşlenen port + yerel net
    assert by["R201"]["pins"] == {"1": "VIN", "2": "STAT_A"}      # eşlenmeyen port = aynı adlı global
    assert by["R201"]["value"] == "330" and by["R101"]["value"] == "1k"
    assert by["D7"]["instance"] == "STAT" and "blocks" not in flat
    assert blocks.expand({"parts": [{"ref": "X"}]}) == {"parts": [{"ref": "X"}]}  # bloksuz: aynen


def test_ref_stability_when_block_grows():
    """Bloğa parça eklemek diğer instance'ların ref'lerini kaydırmamalı (PCB yerleşimi ref'e bağlı)."""
    s = spec({"block": "led", "name": "A"}, {"block": "led", "name": "B"})
    before = {p["ref"] for p in blocks.expand(s)["parts"] if p["instance"] == "B"}
    s["blocks"]["led"]["parts"] = [*LED_BLOCK["parts"],
                                   {"ref": "C1", "symbol": "Device:C", "footprint": R, "pins": {"1": "VIN", "2": "GND"}}]
    after = {p["ref"] for p in blocks.expand(s)["parts"] if p["instance"] == "B"}
    assert before < after and after - before == {"C201"}


@pytest.mark.parametrize("inst,msg", [
    ({"block": "nope", "name": "X"}, "needs 'name' and a 'block'"),
    ({"block": "led", "name": "X", "nets": {"VOUT": "N"}}, "not ports of block"),
    ({"block": "led", "name": "X", "set": {"R9": {"value": "1"}}}, r"no part \['R9'\]"),
    ({"block": "led", "name": "X", "set": {"R1": {"pins": {}}}}, "can't set"),
])
def test_expand_errors(inst, msg):
    with pytest.raises(ToolError, match=msg):
        blocks.expand(spec(inst))


def test_local_net_clash_with_global_net():
    clash = {"ref": "J1", "symbol": "Device:R", "footprint": R, "pins": {"1": "PWR_A", "2": "GND"}}
    with pytest.raises(ToolError, match=r"clash with global nets \['PWR_A'\]"):
        blocks.expand(spec({"block": "led", "name": "PWR"}, parts=[clash]))


def test_patch_instances_and_blocks():
    s = spec({"block": "led", "name": "A", "nets": {"VIN": "+5V"}})
    s = design.apply_patch(s, {"instances": [{"name": "A", "set": {"R1": {"value": "2k"}}},
                                             {"block": "led", "name": "B"}]})
    assert s["instances"][0] == {"block": "led", "name": "A", "nets": {"VIN": "+5V"}, "set": {"R1": {"value": "2k"}}}
    s = design.apply_patch(s, {"remove_instances": ["A"], "blocks": {"spare": LED_BLOCK}})
    assert [i["name"] for i in s["instances"]] == ["B"] and set(s["blocks"]) == {"led", "spare"}


@needs_kicad
def test_sync_with_blocks(tmp_path):
    j1 = {"ref": "J1", "symbol": "Connector_Generic:Conn_01x03",
          "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
          "pins": {"1": "+5V", "2": "+3V3", "3": "GND"}}
    r = design.sync(str(tmp_path), "blk", spec({"block": "led", "name": "PWR", "nets": {"VIN": "+5V"}},
                                               {"block": "led", "name": "STAT", "nets": {"VIN": "+3V3"}},
                                               parts=[j1]), None)
    assert r["netlist_check"] == "OK" and r["erc"] == "CLEAN" and r["parity"] == "OK"
    assert r["nets"] == "+3V3:2 +5V:2 GND:3 PWR_A:2 STAT_A:2"
    assert r["instances"] == ["PWR(led): R101 D101", "STAT(led): R201 D201"]
    saved = json.loads((tmp_path / "blk.circuit.json").read_text())
    assert "blocks" in saved and [p["ref"] for p in saved["parts"]] == ["J1"]  # diskte bloklu kalır
    # patch ile tek instance'ın değeri: yerleşim korunur, yeni parça eklenmez
    r = design.sync(str(tmp_path), "blk", None, {"instances": [{"name": "STAT", "set": {"R1": {"value": "330"}}}]})
    assert r["pcb"]["added"] == [] and r["pcb"]["updated"] == 5
    # hiyerarşi: her instance kendi sayfası; kök sayfada sheet kutusu + port listesi
    assert {f.name for f in tmp_path.glob("*.kicad_sch")} == {"blk.kicad_sch", "blk-PWR.kicad_sch", "blk-STAT.kicad_sch"}
    root = (tmp_path / "blk.kicad_sch").read_text()
    assert root.count("(sheet\n") == 2 and '"Sheetfile" "blk-PWR.kicad_sch"' in root and "VIN = +5V" in root
    assert '(reference "R101")' in (tmp_path / "blk-PWR.kicad_sch").read_text()
    # instance silinince sayfası da silinir, parçaları PCB'den çıkar
    r = design.sync(str(tmp_path), "blk", None, {"remove_instances": ["STAT"]})
    assert not (tmp_path / "blk-STAT.kicad_sch").exists() and sorted(r["pcb"]["removed"]) == ["D201", "R201"]
    assert r["parity"] == "OK"
    # +3V3 artık sadece J1'e gidiyor; güç sembolüyle çizildiği için (global net) etiket uyarısı yok
    assert r["erc"] == "CLEAN"


def test_null_port_leaves_pin_unconnected():
    flat = blocks.expand(spec({"block": "led", "name": "X", "nets": {"VIN": None}}))
    r = next(p for p in flat["parts"] if p["ref"] == "R101")
    assert r["pins"] == {"2": "X_A"} and r["nc"] == ["1"]


def test_net_prefix_keeps_legacy_names_and_detects_clash():
    flat = blocks.expand(spec({"block": "led", "name": "LINK", "net_prefix": "I"}))
    assert next(p for p in flat["parts"] if p["ref"] == "R101")["pins"]["2"] == "IA"
    with pytest.raises(ToolError, match="local net A of B clashes with A"):
        blocks.expand(spec({"block": "led", "name": "A", "net_prefix": ""},
                           {"block": "led", "name": "B", "net_prefix": ""}))


def test_dru_mentions_renamed_nets(tmp_path):
    (tmp_path / "x.kicad_dru").write_text(
        "(rule iso (condition \"A.NetName == 'IC?P' || A.NetName == 'VISO*'\") (constraint clearance (min 2mm)))")
    assert design._dru_mentions(tmp_path, ["IC1P", "C1P", "VISO5"]) == [
        "IC1P ~ IC?P (x.kicad_dru)", "VISO5 ~ VISO* (x.kicad_dru)"]
