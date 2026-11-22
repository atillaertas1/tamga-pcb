"""LCSC içe aktarma (ağsız: easyeda2kicad çıktısı fixture'dan) + pad numaralandırma karşılaştırması."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tamga_pcb import design, importer, library
from kicad_env import needs_cli, needs_kicad

FIX = Path(__file__).parent / "fixtures" / "easyeda"
pytestmark = needs_cli


@pytest.fixture
def imp(monkeypatch, tmp_path):
    """İçe aktarma tmp'ye yazar; kütüphane aramaları tmp'yi de görür."""
    root = tmp_path / "lib"
    monkeypatch.setenv("TAMGA_IMPORT_DIR", str(root))
    monkeypatch.setattr(library, "SYM_DIRS", [*library.SYM_DIRS, root / "symbols"])
    monkeypatch.setattr(library, "FP_DIRS", [*library.FP_DIRS, root / "footprints"])
    monkeypatch.setattr(importer, "_convert",
                        lambda lcsc, out: shutil.copytree(FIX / lcsc, out, dirs_exist_ok=True))
    library._lib_tree.cache_clear()
    yield root
    library._lib_tree.cache_clear()


def test_import_writes_kicad10_library(imp):
    r = importer.import_part("c20526")
    assert r["symbol"] == "tamga_lcsc:MMBT3904-C20526"
    assert r["footprint"] == "tamga_lcsc:SOT-23-3_L2.9-W1.3-P1.90-LS2.4-BR"
    assert r["pins"] == ["1:B:passive", "2:E:passive", "3:C:passive"]  # unspecified -> passive
    assert r["pad_check"] == {"missing_pads": [], "unused_pads": []}
    sym = (imp / "symbols" / "tamga_lcsc.kicad_sym").read_text()
    assert '(property "Footprint" "tamga_lcsc:SOT-23-3' in sym and '(property "LCSC" "C20526"' in sym
    # KiCad 10 açabilmeli (sexpr.dump çıktısı)
    subprocess.run(["kicad-cli", "sym", "export", "svg", str(imp / "symbols" / "tamga_lcsc.kicad_sym"),
                    "-o", str(imp / "svg")], check=True, capture_output=True)
    importer.import_part("C20526")  # tekrar: kopya değil, değiştirir
    assert (imp / "symbols" / "tamga_lcsc.kicad_sym").read_text().count('(symbol "MMBT3904-C20526"') == 1


@needs_kicad
def test_imported_part_in_design_adds_lib_tables(imp, tmp_path, monkeypatch):
    importer.import_part("C20526")
    spec = {"title": "T", "board": {"width_mm": 20, "height_mm": 15}, "parts": [
        {"ref": "Q1", "symbol": "tamga_lcsc:MMBT3904-C20526",
         "footprint": "tamga_lcsc:SOT-23-3_L2.9-W1.3-P1.90-LS2.4-BR", "pins": {"B": "IN", "E": "GND", "C": "OUT"}},
        {"ref": "J1", "symbol": "Connector_Generic:Conn_01x03",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
         "pins": {"1": "IN", "2": "GND", "3": "OUT"}}]}
    proj = tmp_path / "proj"
    monkeypatch.setenv("TAMGA_EXTRA_FOOTPRINT_DIRS", str(imp / "footprints"))  # worker alt süreci için
    r = design.sync(str(proj), "t", spec, None)
    assert r["netlist_check"] == "OK" and r["erc"] == "CLEAN" and r["parity"] == "OK"
    assert r["lib_tables_added"] == ["sym-lib-table:tamga_lcsc", "fp-lib-table:tamga_lcsc"]
    assert "${KIPRJMOD}/lib/tamga_lcsc.kicad_sym" in (proj / "sym-lib-table").read_text()
    assert "${KIPRJMOD}/lib/tamga_lcsc.pretty" in (proj / "fp-lib-table").read_text()
    assert (proj / "lib" / "tamga_lcsc.kicad_sym").is_file()  # proje tek başına taşınabilir
    assert "Q1" in r["pcb"]["added"]
    assert "lib_tables_added" not in design.sync(str(proj), "t", None, {"parts": []})


def test_compare_pads_rotation_and_renumbering():
    kicad = library.pad_positions("Package_TO_SOT_SMD:SOT-23")
    rot90 = {k: (-y + 5, x - 3) for k, (x, y) in kicad.items()}  # döndür + kaydır: aynı numaralandırma
    assert importer.compare_pads(rot90, kicad)["match"] == 3
    swapped = {**kicad, "1": kicad["2"], "2": kicad["1"]}
    r = importer.compare_pads(swapped, kicad)
    assert r["match"] == 1 and r["differ"] == ["1", "2"]


def test_df40_u_numbering_is_detected():
    """Gerçek vaka: EasyEDA DF40 = U numaralandırma (bir sıra 1..50), KiCad = tek/çift sıralar."""
    kicad = library.pad_positions("Connector_Hirose_DF40:Hirose_DF40C-100DS-0.4V_2x50_P0.4mm")
    easyeda = {str(i + 1): (9.8 - 0.4 * i, 1.6) for i in range(50)}
    easyeda |= {str(51 + i): (-9.8 + 0.4 * i, -1.6) for i in range(50)}
    r = importer.compare_pads(easyeda, kicad)
    assert r["total"] == 100 and r["match"] < 10


def test_missing_easyeda_says_how_to_install(monkeypatch):
    """easyeda2kicad yoksa alt süreç hatası değil, kurulum talimatı."""
    real = importer.importlib.util.find_spec
    monkeypatch.setattr(importer.importlib.util, "find_spec",
                        lambda name, *a: None if name == "easyeda2kicad" else real(name, *a))
    with pytest.raises(importer.ToolError, match=r"tamga-pcb\[lcsc\]"):
        importer._require_easyeda()
