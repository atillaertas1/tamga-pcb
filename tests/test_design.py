"""Spec -> şematik + PCB testleri (gerçek KiCad kütüphaneleri ve kicad-cli ile)."""
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tamga_pcb import design, library, sexpr, server
from kicad_env import JAR, needs_kicad, needs_router

ROOT = Path(__file__).resolve().parents[1]
pytestmark = needs_kicad

LED_SPEC = {
    "title": "LED", "board": {"width_mm": 20, "height_mm": 15},
    "parts": [
        {"ref": "J1", "symbol": "Connector_Generic:Conn_01x02", "value": "5V IN",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
         "pins": {"1": "+5V", "2": "GND"}},
        {"ref": "R1", "symbol": "Device:R", "value": "330", "lcsc": "C17630",
         "footprint": "Resistor_SMD:R_0805_2012Metric", "pins": {"1": "+5V", "2": "LED_A"}},
        {"ref": "D1", "symbol": "Device:LED", "value": "GREEN",
         "footprint": "LED_SMD:LED_0805_2012Metric", "pins": {"A": "LED_A", "K": "GND"}},
    ],
}


def test_sexpr_roundtrip():
    text = '(a (b "x \\"q\\"" 1 2.5) (c yes) (d))'
    t = sexpr.parse(text)
    assert sexpr.parse(sexpr.dump(t)) == t
    assert t[1][1] == 'x "q"' and isinstance(t[2][1], sexpr.Sym)


def test_symbol_lookup_and_extends_flattening():
    led = library.describe_symbol("Device:LED")
    assert led["pins"] == ["1:K:passive", "2:A:passive"]
    ams = library.describe_symbol("Regulator_Linear:AMS1117-3.3")  # AP1117-15'ten türetilmiş
    assert ams["pins"] == ["1:GND:power_in", "2:VO:power_out", "3:VI:power_in"]
    assert library.describe_symbol("Amplifier_Operational:LM358")["units"] == 3
    assert library.find_symbols("ams1117 3.3", 1)[0].startswith("Regulator_Linear:AMS1117-3.3 ")
    assert "Resistor_SMD:R_0805_2012Metric" in library.find_footprints("R 0805")
    assert library.footprint_pads("Package_TO_SOT_SMD:SOT-223-3_TabPin2") == ["1", "2", "3"]


def test_led_from_scratch(tmp_path):
    r = design.sync(str(tmp_path), "led", LED_SPEC, None)
    assert r["netlist_check"] == "OK" and r["erc"] == "CLEAN" and r["parity"] == "OK"
    assert r["nets"] == "+5V:2 GND:2 LED_A:2"
    assert r["pcb"]["created"] and r["pcb"]["added"] == ["J1", "R1", "D1"]
    assert "outside_board" not in r["pcb"]
    s = server.board_summary(r["files"]["pcb"])
    assert s["nets"] == "+5V:2 GND:2 LED_A:2" and s["size_mm"] == [20.1, 15.1]
    assert json.loads(Path(r["files"]["spec"]).read_text())["parts"][1]["lcsc"] == "C17630"


def test_patch_preserves_placement(tmp_path):
    d = str(tmp_path)
    r = design.sync(d, "led", LED_SPEC, None)
    pcb = r["files"]["pcb"]
    server.place_footprints(pcb, [{"ref": "R1", "x": 110, "y": 107, "rot": 90}])
    r = design.sync(d, "led", None, {
        "parts": [{"ref": "C1", "symbol": "Device:C", "value": "100n",
                   "footprint": "Capacitor_SMD:C_0603_1608Metric", "pins": {"1": "+5V", "2": "GND"}},
                  {"ref": "R1", "value": "470"}]})
    assert r["netlist_check"] == "OK" and r["erc"] == "CLEAN" and r["parity"] == "OK"
    assert r["pcb"]["added"] == ["C1"]
    line = server.get_footprint(pcb, "R1")["line"]
    assert line.startswith("R1 470 R_0805_2012Metric @(110.0,107.0) r90")

    r = design.sync(d, "led", None, {"parts": [{"ref": "R1", "footprint": "Resistor_SMD:R_0603_1608Metric"}],
                                     "remove": ["C1"]})
    assert r["pcb"]["replaced"] == ["R1"] and r["pcb"]["removed"] == ["C1"] and r["parity"] == "OK"
    assert "@(110.0,107.0) r90" in server.get_footprint(pcb, "R1")["line"]


def test_bad_pin_writes_nothing(tmp_path):
    d = str(tmp_path)
    design.sync(d, "led", LED_SPEC, None)
    before = (tmp_path / "led.kicad_sch").read_text()
    with pytest.raises(ToolError, match="no pin 'ANODE'.*2:A"):
        design.sync(d, "led", None, {"parts": [{"ref": "D1", "pins": {"ANODE": "X"}}]})
    assert (tmp_path / "led.kicad_sch").read_text() == before


def test_check_pin_pad():
    ok = library.check_pin_pad("Device:R", "Resistor_SMD:R_0805_2012Metric")
    assert ok == {"missing_pads": [], "unused_pads": []}
    tab = library.check_pin_pad("Regulator_Linear:AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223")
    assert tab == {"missing_pads": [], "unused_pads": ["4"]}
    # LM358: 3 birim, 8 pin (tekrarlı numara yok sayılmalı) -> 3 pad'li kılıfta 4..8 eksik
    bad = library.check_pin_pad("Amplifier_Operational:LM358", "Package_TO_SOT_SMD:SOT-223-3_TabPin2")
    assert bad == {"missing_pads": ["4", "5", "6", "7", "8"], "unused_pads": []}


def test_missing_pad_writes_nothing(tmp_path):
    d = str(tmp_path)
    design.sync(d, "led", LED_SPEC, None)
    before = (tmp_path / "led.kicad_sch").read_text()
    wrong_fp = {"ref": "R1", "symbol": "Amplifier_Operational:LM358",
                "footprint": "Resistor_SMD:R_0805_2012Metric", "pins": {"1": "+5V"}}
    with pytest.raises(ToolError, match=r"pin/pad mismatch.*R1: pins \['3'"):
        design.sync(d, "led", None, {"parts": [wrong_fp]})
    assert (tmp_path / "led.kicad_sch").read_text() == before


def test_pinout_warning_in_sync(tmp_path, monkeypatch):
    from tamga_pcb import pinout
    from test_pinout import easyeda
    db = {"C2286": easyeda("KT-0603R", {"1": "A", "2": "K"})}
    monkeypatch.setattr(pinout, "_fetch", lambda lcsc: db[lcsc] if lcsc in db else
                        (_ for _ in ()).throw(ToolError("offline")))
    spec = json.loads(json.dumps(LED_SPEC))
    spec["parts"][2]["lcsc"] = "C2286"
    r = design.sync(str(tmp_path), "led", spec, None)
    assert r["pinout_warnings"] == ["D1 (C2286 KT-0603R): 1: symbol K, part A; 2: symbol A, part K"]
    assert r["pinout_unchecked"] == ["R1"]  # C17630 "ağda yok" -> kontrol edilemedi, akış durmadı
    assert r["netlist_check"] == "OK"


def test_multi_unit_power_flags_and_nc(tmp_path):
    spec = {"board": {"width_mm": 30, "height_mm": 20}, "parts": [
        {"ref": "J1", "symbol": "Connector_Generic:Conn_01x03",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
         "pins": {"1": "+5V", "2": "GND", "3": "SIG"}},
        {"ref": "U1", "symbol": "Amplifier_Operational:LM358",
         "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
         "pins": {"V+": "+5V", "V-": "GND", "3": "SIG", "1": "OUT", "2": "OUT"}, "nc": ["5", "6", "7"]},
        {"ref": "J2", "symbol": "Connector_Generic:Conn_01x01",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical", "pins": {"1": "OUT"}},
    ]}
    r = design.sync(str(tmp_path), "amp", spec, None)
    assert r["netlist_check"] == "OK" and r["erc"] == "CLEAN" and r["parity"] == "OK"
    assert r["pwr_flags_added"] == ["+5V", "GND"]


def test_unconnected_pin_is_reported_by_erc(tmp_path):
    spec = json.loads(json.dumps(LED_SPEC))
    spec["parts"][2]["pins"] = {"A": "LED_A"}  # katot bağlı değil
    r = design.sync(str(tmp_path), "led", spec, None)
    assert r["erc"] != "CLEAN"
    assert any(t["type"] == "pin_not_connected" for t in r["erc"]["types"])


@needs_router
def test_full_flow_to_fab(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEROUTING_JAR", str(JAR))
    r = design.sync(str(tmp_path), "led", LED_SPEC, None)
    pcb = r["files"]["pcb"]
    server.place_footprints(pcb, [
        {"ref": "J1", "x": 103, "y": 106, "rot": 0},
        {"ref": "R1", "x": 109, "y": 104.5, "rot": 0},
        {"ref": "D1", "x": 115, "y": 104.5, "rot": 180},
    ])
    server.set_netclass(pcb, "POWER", ["+5V"], track_mm=0.4)
    routed = server.autoroute(pcb, out_path=pcb, max_passes=50)
    assert routed["unrouted_after"] == 0
    z = server.add_copper_zone(pcb)  # GND, iki katman + stitching via
    assert z["stitch_vias"] > 0
    assert server.run_drc(pcb, schematic_parity=True).get("verdict") == "CLEAN"
    fab = server.export_fab(pcb)
    assert fab["missing_lcsc"] and "R1" not in fab["missing_lcsc"]


def test_in_bom_follows_footprint_attrs(tmp_path):
    """Montaj deliği footprint'i exclude_from_bom: sembol de hariç olmalı, yoksa parity uyarısı."""
    spec = json.loads(json.dumps(LED_SPEC))
    spec["parts"].append({"ref": "H1", "symbol": "Mechanical:MountingHole_Pad",
                          "footprint": "MountingHole:MountingHole_2.7mm_M2.5_Pad", "pins": {"1": "GND"}})
    spec["parts"][1]["in_bom"] = False  # elle de hariç tutulabilir
    r = design.sync(str(tmp_path), "led", spec, None)
    assert r["parity"] == "OK" and r["erc"] == "CLEAN"
    sch = (tmp_path / "led.kicad_sch").read_text()
    for ref, want in (("H1", "no"), ("R1", "no"), ("D1", "yes")):
        block = sch[:sch.index(f'(property "Reference" "{ref}"')]
        assert block[block.rindex("(in_bom"):].startswith(f"(in_bom {want})"), ref


def test_sheets_power_symbols_and_notes(tmp_path):
    """Parça 'sheet' alanı -> ayrı sayfa; GND/+5V güç sembolü (etiket değil); sayfa notu; net adları aynı."""
    spec = json.loads(json.dumps(LED_SPEC))
    spec["parts"][0]["sheet"] = "Giriş"
    for p in spec["parts"][1:]:
        p["sheet"] = "LED Bölümü"
    spec["sheet_notes"] = {"LED Bölümü": "R = (5 - 2) / 9 mA = 330R"}
    r = design.sync(str(tmp_path), "led", spec, None)
    assert r["netlist_check"] == "OK" and r["erc"] == "CLEAN" and r["parity"] == "OK"
    assert r["nets"] == "+5V:2 GND:2 LED_A:2"  # net adları değişmedi
    assert {f.name for f in tmp_path.glob("*.kicad_sch")} == {"led.kicad_sch", "led-Giris.kicad_sch", "led-LED_Bolumu.kicad_sch"}
    page = (tmp_path / "led-LED_Bolumu.kicad_sch").read_text()
    assert '(lib_id "power:GND")' in page and '(lib_id "power:+5V")' in page
    assert '(global_label "GND"' not in page and '(global_label "LED_A"' in page  # sinyal neti etiket kalır
    assert "330R" in page  # not sayfada
    root = (tmp_path / "led.kicad_sch").read_text()
    assert '"Sheetname" "LED Bölümü"' in root and "[güç] +5V GND" in root  # kök: sayfalar arası arayüz


def test_satellites_are_wired_to_their_pin(tmp_path):
    """Pine bağlı küçük parçalar (pull-up, filtre C) pinden çıkan tele bağlanır: net adı tek etiketten,
    bağlantı noktasında junction; netlist adları spec'teki gibi kalır."""
    spec = {"title": "PU", "board": {"width_mm": 20, "height_mm": 15}, "parts": [
        {"ref": "J1", "symbol": "Connector_Generic:Conn_01x03", "value": "IN",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
         "pins": {"1": "+5V", "2": "SIG", "3": "GND"}},
        {"ref": "R1", "symbol": "Device:R", "value": "10k", "footprint": "Resistor_SMD:R_0603_1608Metric",
         "pins": {"1": "+5V", "2": "SIG"}},
        {"ref": "C1", "symbol": "Device:C", "value": "100n", "footprint": "Capacitor_SMD:C_0603_1608Metric",
         "pins": {"1": "SIG", "2": "GND"}},
    ]}
    r = design.sync(str(tmp_path), "pu", spec, None)
    assert r["netlist_check"] == "OK" and r["erc"] == "CLEAN" and r["parity"] == "OK"
    sch = (tmp_path / "pu.kicad_sch").read_text()
    assert sch.count('(global_label "SIG"') == 1   # eskiden her pinde bir etiket: 3
    assert "(junction" in sch and sch.count("(wire") >= 6


@needs_router
def test_move_after_routing_then_reroute_is_clean(tmp_path, monkeypatch):
    """Rotadan sonra parça taşınıp yeniden rotalanınca eski izler kalmamalı (gerçek oturumda 28 DRC hatası
    ve kısa devre böyle çıkmıştı); tekrar dökülen GND eskisinin yerine geçer."""
    monkeypatch.setenv("FREEROUTING_JAR", str(JAR))
    pcb = design.sync(str(tmp_path), "led", LED_SPEC, None)["files"]["pcb"]
    place = [{"ref": "J1", "x": 103, "y": 106, "rot": 0}, {"ref": "R1", "x": 109, "y": 104.5, "rot": 0},
             {"ref": "D1", "x": 115, "y": 104.5, "rot": 180}]
    server.place_footprints(pcb, place)
    server.autoroute(pcb, max_passes=50)
    server.add_copper_zone(pcb)
    server.place_footprints(pcb, [{"ref": "D1", "x": 115, "y": 110, "rot": 180}])
    r = server.autoroute(pcb, max_passes=50)
    assert r["ripped_up"] > 0 and r["unrouted_after"] == 0
    assert server.add_copper_zone(pcb).get("replaced_zones") == 2
    assert server.run_drc(pcb, schematic_parity=True).get("verdict") == "CLEAN"
