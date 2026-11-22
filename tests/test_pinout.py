"""Sembol <-> LCSC pinout karşılaştırması (ağsız; EasyEDA yanıtı taklit edilir)."""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kicad_env import needs_libs
from tamga_pcb import pinout

pytestmark = needs_libs


def easyeda(title: str, pins: dict[str, str]) -> dict:
    """EasyEDA bileşen yanıtının pinout'a yeten minimal hali (gerçek formatla aynı alanlar)."""
    shape = [f"P~show~0~{n}~0~0~0~g{n}~0^^0~0^^M0,0h10~#000^^1~0~0~0~{name}~start~~~#000^^x"
             for n, name in pins.items()]
    return {"title": title, "dataStr": {"shape": ["R~0~0", *shape]}}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    db = {
        "C20526": easyeda("MMBT3904", {"1": "B", "2": "E", "3": "C"}),
        "C6186": easyeda("AMS1117-3.3", {"1": "GND", "2": "VOUT", "3": "VIN", "4": "VOUT"}),
        "C7950": easyeda("LM358DR2G", {"1": "1OUT", "2": "1IN-", "3": "1IN+", "4": "GND",
                                       "5": "2IN+", "6": "2IN-", "7": "2OUT", "8": "VCC"}),
        "C2286": easyeda("KT-0603R", {"1": "A", "2": "K"}),
        "C17630": easyeda("0805W8F3300T5E", {"1": "1", "2": "2"}),
    }

    def fetch(lcsc):
        if lcsc not in db:
            raise ToolError(f"{lcsc}: not found on easyeda")
        return db[lcsc]

    monkeypatch.setattr(pinout, "_fetch", fetch)
    monkeypatch.setattr(pinout, "CACHE", tmp_path)


def test_correct_pinouts_pass(fake):
    assert pinout.check("Transistor_BJT:Q_NPN_BEC", "C20526") == {"part": "MMBT3904", "pinout": "OK"}
    assert pinout.check("Amplifier_Operational:LM358", "C7950")["pinout"] == "OK"  # V-=GND, +=1IN+
    assert pinout.check("Device:R", "C17630")["pinout"] == "OK"  # adsız pasif atlanır


def test_wrong_symbol_variant_is_caught(fake):
    r = pinout.check("Transistor_BJT:Q_NPN_CBE", "C20526")
    assert r["mismatch"] == ["1: symbol C, part B", "2: symbol B, part E", "3: symbol E, part C"]


def test_led_polarity_and_tab(fake):
    assert pinout.check("Device:LED", "C2286")["mismatch"] == ["1: symbol K, part A", "2: symbol A, part K"]
    assert pinout.check("Regulator_Linear:AMS1117-3.3", "C6186") == {
        "part": "AMS1117-3.3", "part_only": ["4:VOUT"]}


def test_cache_avoids_refetch(fake, monkeypatch):
    pinout.part_pins("c20526")  # küçük harf de kabul
    monkeypatch.setattr(pinout, "_fetch", lambda lcsc: pytest.fail("should hit cache"))
    assert pinout.part_pins("C20526")["pins"] == {"1": "B", "2": "E", "3": "C"}


def test_name_matching():
    assert pinout.same_pin("PC13", "PC13-TAMPER-RTC") and pinout.same_pin("VSS", "VSS_1")
    assert pinout.same_pin("VO", "VOUT") and pinout.same_pin("~{RESET}", "RESET")
    assert pinout.same_pin("~{RE}", "RE#") and pinout.same_pin("VS+", "V+") and pinout.same_pin("GND", "EP")
    assert pinout.same_pin("A", "+") and not pinout.same_pin("K", "+")  # LED polaritesi
    assert not pinout.same_pin("B", "E") and not pinout.same_pin("VIN", "VOUT")
