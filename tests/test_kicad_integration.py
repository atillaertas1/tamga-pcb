"""Gerçek KiCad (kicad-cli + pcbnew) ile entegrasyon testleri. KiCad yoksa atlanır."""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from pcbmcp import server
from pcbmcp.kicad import KICAD_PYTHON, KicadError

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None
    or subprocess.run([KICAD_PYTHON, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="KiCad not installed",
)


@pytest.fixture
def board(tmp_path):
    pcb = tmp_path / "ldo" / "ldo.kicad_pcb"
    subprocess.run([KICAD_PYTHON, str(ROOT / "scripts" / "make_example_board.py"), str(pcb)],
                   check=True, capture_output=True)
    return str(pcb)


def test_summary(board):
    s = server.board_summary(board)
    assert s["counts"]["footprints"] == 7
    assert s["nets"] == "+3V3:5 GND:6 LED_A:2 VIN:3"
    assert s["unrouted"] > 0
    assert s["rules_mm"]["min_track"] == 0.127


def test_footprint_and_missing(board):
    fp = server.get_footprint(board, "U1")
    assert any(p.startswith("3:VIN@") for p in fp["pads"])
    with pytest.raises(KicadError, match="not found"):
        server.get_footprint(board, "U99")


def test_place_creates_backup_each_time(board):
    r1 = server.place_footprints(board, [{"ref": "R1", "x": 110, "y": 112, "rot": 90}])
    r2 = server.place_footprints(board, [{"ref": "D1", "x": 110, "y": 116, "side": "B"}])
    assert r1["backup"] != r2["backup"]
    assert "R1 1k R_0805_2012Metric @(110.0,112.0) r90 F" in server.board_summary(board)["footprints"]
    assert server.get_footprint(board, "D1")["line"].endswith(" B")


def test_zone_drc_and_fab(board):
    server.add_copper_zone(board, "GND", "B.Cu")
    drc = server.run_drc(board)
    assert drc["errors"] > 0 and drc["types"][0]["type"] == "unconnected_items"

    server.set_footprint_fields(board, [{"ref": "U1", "field": "LCSC", "value": "C6186"}])
    fab = server.export_fab(board)
    names = zipfile.ZipFile(fab["gerber_zip"]).namelist()
    assert any(n.endswith("-F_Cu.gtl") for n in names)
    assert any(n.endswith(".drl") for n in names)
    assert "U1" not in fab["missing_lcsc"]
    bom = (Path(fab["dir"]) / "BOM.csv").read_text()
    assert "AMS1117-3.3,U1,SOT-223-3_TabPin2,C6186" in bom


def test_autoroute_without_jar_gives_clear_error(board, monkeypatch, tmp_path):
    monkeypatch.setenv("FREEROUTING_JAR", str(tmp_path / "nope.jar"))
    with pytest.raises(KicadError, match="Freerouting jar not found"):
        server.autoroute(board)


def test_netclass_persists_and_reaches_dsn(board, tmp_path):
    from pcbmcp.kicad import run_worker
    r = server.set_netclass(board, "POWER", ["VIN", "+3V3", "NOPE"], track_mm=0.5, via_mm=0.8,
                            via_drill_mm=0.4)
    assert r["netclass"].startswith("POWER(t0.5,c0.2,v0.8/0.4): +3V3 VIN")
    assert r["unmatched_nets"] == ["NOPE"]
    # sadece verilen değer değişsin, gerisi korunsun
    r = server.set_netclass(board, "POWER", clearance_mm=0.25)
    assert r["netclass"].startswith("POWER(t0.5,c0.25,")
    # .kicad_pro'ya yazıldı mı: yeni süreçte yeniden yükle
    assert any(l.startswith("POWER(") and "+3V3 VIN" in l
               for l in server.board_summary(board)["netclasses"])
    dsn = tmp_path / "b.dsn"
    run_worker("export_dsn", {"pcb": board, "dsn": str(dsn)})
    t = dsn.read_text()
    assert "(class POWER +3V3 VIN" in t and "(width 500)" in t


def test_netclass_wildcard_and_resize(board):
    from pcbmcp.kicad import run_worker  # noqa: F401
    r = server.set_netclass(board, "PWR", ["+*V*"], track_mm=0.4, apply_to_existing=True)
    assert r["netclass"].endswith(": +3V3")
    assert r["resized_items"] == 0  # henüz iz yok


@pytest.mark.skipif(not (ROOT / "tools" / "freerouting.jar").is_file()
                    or shutil.which("java") is None, reason="freerouting.jar/java missing")
def test_autoroute_end_to_end(board, monkeypatch):
    monkeypatch.setenv("FREEROUTING_JAR", str(ROOT / "tools" / "freerouting.jar"))
    server.set_netclass(board, "POWER", ["VIN", "+3V3"], track_mm=0.5)
    r = server.autoroute(board, max_passes=50)
    assert r["unrouted_before"] > 0 and r["unrouted_after"] == 0 and r["tracks"] > 0
    server.add_copper_zone(r["out"], "GND", "B.Cu")
    assert server.run_drc(r["out"]).get("verdict") == "CLEAN"


def test_dsn_export(board, tmp_path):
    from pcbmcp.kicad import run_worker
    dsn = tmp_path / "b.dsn"
    run_worker("export_dsn", {"pcb": board, "dsn": str(dsn)})
    text = dsn.read_text()
    assert text.lstrip().startswith("(pcb") and "(net VIN" in text


def _locked(path):
    out = subprocess.run([KICAD_PYTHON, "-c", (
        "import pcbnew,sys; b=pcbnew.LoadBoard(sys.argv[1]); "
        "print(sorted((t.GetClass(),t.GetNetname(),t.GetLayerName(),t.GetStart().x,t.GetStart().y,"
        "t.GetEnd().x,t.GetEnd().y) for t in b.GetTracks() if t.IsLocked()))"), path],
        capture_output=True, text=True, check=True).stdout
    return out.strip().splitlines()[-1]


def test_route_net_with_via_and_errors(board):
    r = server.route_net(board, "VIN", "J1.1", "C1.1", [[106.5, 108.0], [106.5, 104.0]])
    assert (r["segments"], r["vias"], r["unrouted_after"]) == (3, 0, r["unrouted_before"] - 1)
    r = server.route_net(board, "GND", "J1.2", "D1.1",
                         [[106, 110.54], [106, 114, "B.Cu"], [115.06, 114], [115.06, 114.5, "F.Cu"]])
    assert r["vias"] == 2 and r["unrouted_after"] == r["unrouted_before"] - 1
    assert "'VIN'" in _locked(board) and "PCB_VIA" in _locked(board)
    drc = server.run_drc(board, refill_zones=False)
    assert {t["type"] for t in drc["types"]} == {"unconnected_items"}  # el rotası çakışma üretmedi
    with pytest.raises(KicadError, match="J1.2 is on net 'GND', not 'VIN'"):
        server.route_net(board, "VIN", "J1.2", "C1.1")
    with pytest.raises(KicadError, match="C1.1 is not on B.Cu"):
        server.route_net(board, "VIN", "J1.1", "C1.1", [[105, 108, "B.Cu"]])


@pytest.mark.skipif(not (ROOT / "tools" / "freerouting.jar").is_file()
                    or shutil.which("java") is None, reason="freerouting.jar/java missing")
def test_autoroute_keeps_hand_routes(board, monkeypatch):
    monkeypatch.setenv("FREEROUTING_JAR", str(ROOT / "tools" / "freerouting.jar"))
    server.route_net(board, "GND", "J1.2", "D1.1",
                     [[106, 110.54], [106, 114, "B.Cu"], [115.06, 114], [115.06, 114.5, "F.Cu"]])
    before = _locked(board)
    r = server.autoroute(board, max_passes=20)
    assert r["unrouted_after"] == 0 and _locked(r["out"]) == before
    assert server.run_drc(r["out"], refill_zones=False).get("verdict") == "CLEAN"
