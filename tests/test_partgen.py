"""Üretilen footprint'ler KiCad'in stok footprint'leriyle aynı yerde pad'e sahip olmalı."""
import pytest
from kicad_env import needs_cli

from tamga_pcb import importer, library, partgen, paths, sexpr


def test_soic_pad_geometry_matches_ipc_nominal():
    pads, body, _ = partgen.pads_for({"type": "soic", "pins": 8, "pitch_mm": 1.27})
    p1 = next(p for p in pads if p["number"] == "1")
    assert (p1["x"], p1["y"], round(p1["w"], 3), round(p1["h"], 3)) == (-2.475, -1.905, 1.95, 0.6)
    assert [p["number"] for p in pads if p["x"] > 0] == ["8", "7", "6", "5"]  # sağ sıra aşağıdan yukarı numaralı


def test_qfn_numbering_is_counter_clockwise_with_exposed_pad():
    pads, _, _ = partgen.pads_for({"type": "qfn", "pins": 16, "pitch_mm": 0.5, "body_mm": 3, "ep_mm": 1.7})
    first = {p["number"]: (p["x"], p["y"]) for p in pads}
    assert first["1"][0] < 0 and first["5"][1] > 0 and first["9"][0] > 0 and first["13"][1] < 0
    assert first["17"] == (0.0, 0.0)


def test_bad_packages_are_explained():
    with pytest.raises(Exception, match="even"):
        partgen.pads_for({"type": "soic", "pins": 7})
    with pytest.raises(Exception, match="soic, qfn"):
        partgen.pads_for({"type": "bga", "pins": 64})


@needs_cli
@pytest.mark.parametrize("pkg,stock", [
    ({"type": "soic", "pins": 8, "pitch_mm": 1.27}, "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"),
    ({"type": "soic", "pins": 14, "pitch_mm": 0.65, "body_w_mm": 4.4, "body_l_mm": 5.0, "lead_span_mm": 6.4},
     "Package_SO:TSSOP-14_4.4x5mm_P0.65mm"),
    ({"type": "qfn", "pins": 32, "pitch_mm": 0.5, "body_mm": 5.0, "ep_mm": 3.45},
     "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"),
])
def test_generated_footprints_match_kicad_stock(tmp_path, monkeypatch, pkg, stock):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(library, "FP_DIRS", [*library.FP_DIRS, tmp_path / "footprints"])
    r = partgen.create("T", None, pkg)
    got = importer.compare_pads(library.pad_positions(r["footprint"]), library.pad_positions(stock))
    assert got["match"] == got["total"] and got["rotation"] == 0


@needs_cli
def test_symbol_pins_must_have_pads(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    with pytest.raises(Exception, match="pins without a pad"):
        partgen.create("X", [{"number": 9, "name": "A"}], {"type": "soic", "pins": 8, "pitch_mm": 1.27})
    r = partgen.create("Y", [{"number": 1, "name": "GND", "type": "power_in"}, {"number": 2, "name": "GND", "type": "power_in"},
                             {"number": 3, "name": "IN", "type": "input"}], {"type": "soic", "pins": 8, "pitch_mm": 1.27})
    assert r["unused_pads"] == ["4", "5", "6", "7", "8"]
    lib = sexpr.parse((tmp_path / "symbols" / "tamga_user.kicad_sym").read_text())
    assert any(isinstance(c, list) and c[:2] == [sexpr.Sym("symbol"), "Y"] for c in lib)
