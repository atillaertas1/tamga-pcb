"""pcbmcp — KiCad için token-verimli MCP sunucusu.

Tasarım ilkeleri:
- Az araç (her araç tanımı her istekte bağlama girer).
- Ham dosya yok; kompakt, satır-başı-bir-öğe çıktı.
- Yazan her araç önce .pcbmcp_backups/ altına yedek alır.
- Koordinatlar mm, açılar derece, KiCad'in sayfa koordinatları (Y aşağı doğru artar).
"""
from __future__ import annotations

import csv
import functools
import inspect
import json
import tempfile
import zipfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import parts as parts_mod
from . import report, route
from .kicad import abspath, cli, drc_json, erc_json, run_worker

INSTRUCTIONS = """KiCad PCB tools. Units: mm, degrees, KiCad page coords (Y grows down).
Loop: board_summary -> place_footprints -> set_netclass (power nets wider) -> autoroute
-> add_copper_zone GND -> run_drc until CLEAN -> set_footprint_fields LCSC -> export_fab.
Never read .kicad_pcb files directly; use these tools. Write tools keep backups."""

mcp = MCPServer("pcbmcp", instructions=INSTRUCTIONS, version="0.1.0")


def tool(fn):
    """MCP aracı olarak kaydet, ama sonucu tek satır kompakt JSON metin olarak gönder.

    mcp 2.x dict'leri indent=2 ile serileştiriyor (~%30 boşa token). Orijinal fonksiyon
    dict döndürmeye devam eder (testler ve Python'dan kullanım için).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False, separators=(",", ":"))

    del wrapper.__wrapped__  # şema dönüş tipini fn'den (dict) değil wrapper'dan (str) alsın
    wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=str)
    wrapper.__annotations__ = {**fn.__annotations__, "return": "str"}
    mcp.tool(structured_output=False)(wrapper)
    return fn


# ------------------------------------------------------------------ okuma

@tool
def board_summary(pcb_path: str, max_footprints: int = 40) -> dict:
    """Board overview: size, layers, rules, unrouted count, nets (name:pad_count),
    one line per footprint 'REF VALUE FOOTPRINT @(x,y) rDEG F|B'. Start here."""
    return run_worker("summary", {"pcb": abspath(pcb_path), "max_footprints": max_footprints})


@tool
def get_footprint(pcb_path: str, ref: str) -> dict:
    """One footprint: bbox [x,y,w,h] mm and pads as 'num:net@(x,y)'."""
    return run_worker("footprint", {"pcb": abspath(pcb_path), "ref": ref})


# ------------------------------------------------------------------ yazma

@tool
def place_footprints(pcb_path: str, moves: list[dict]) -> dict:
    """Move/rotate/flip footprints in one batch. moves: [{ref, x, y, rot?, side?('F'|'B')}],
    mm/degrees, absolute. Saves the board (backup kept)."""
    return run_worker("place", {"pcb": abspath(pcb_path), "moves": moves})


@tool
def set_netclass(pcb_path: str, name: str, nets: list[str] | None = None,
                 track_mm: float | None = None, clearance_mm: float | None = None,
                 via_mm: float | None = None, via_drill_mm: float | None = None,
                 apply_to_existing: bool = False) -> dict:
    """Create/update a netclass and assign nets (exact names or wildcards like '+*V*').
    name='Default' edits the default class. Omitted values are kept. Set BEFORE autoroute;
    the router uses these widths. apply_to_existing resizes already-routed tracks/vias."""
    return run_worker("set_netclass", {
        "pcb": abspath(pcb_path), "name": name, "nets": nets or [],
        "track_mm": track_mm, "clearance_mm": clearance_mm, "via_mm": via_mm,
        "via_drill_mm": via_drill_mm, "apply_to_existing": apply_to_existing,
    })


@tool
def set_footprint_fields(pcb_path: str, items: list[dict]) -> dict:
    """Set fields on footprints, e.g. LCSC part numbers for assembly.
    items: [{ref, field, value}] e.g. {ref:'U1', field:'LCSC', value:'C6186'}."""
    return run_worker("set_field", {"pcb": abspath(pcb_path), "items": items})


@tool
def add_copper_zone(pcb_path: str, net: str = "GND", layer: str = "B.Cu",
                    clearance_mm: float = 0.3) -> dict:
    """Add a copper pour covering the whole board outline on a layer, then fill."""
    return run_worker("add_zone", {"pcb": abspath(pcb_path), "net": net,
                                   "layer": layer, "clearance_mm": clearance_mm})


@tool
def fill_zones(pcb_path: str) -> dict:
    """Refill all copper zones and save."""
    return run_worker("fill_zones", {"pcb": abspath(pcb_path)})


@tool
def autoroute(pcb_path: str, out_path: str = "", max_passes: int = 100,
              ignore_netclasses: list[str] | None = None) -> dict:
    """Autoroute with Freerouting (headless). Writes <name>_routed.kicad_pcb unless out_path
    given. Reports unrouted before/after. Route critical nets by hand first, then run this."""
    return route.autoroute(abspath(pcb_path), abspath(out_path) if out_path else None,
                           max_passes, ignore_netclasses)


# ------------------------------------------------------------------ doğrulama

@tool
def run_drc(pcb_path: str, refill_zones: bool = True, schematic_parity: bool = False,
            max_examples: int = 3) -> dict:
    """DRC grouped by violation type: count, affected nets, a few example locations.
    'verdict: CLEAN' when nothing found."""
    with tempfile.TemporaryDirectory() as d:
        rep = drc_json(abspath(pcb_path), f"{d}/drc.json", refill_zones, schematic_parity)
    return report.summarize(rep, max_examples)


@tool
def run_erc(sch_path: str, max_examples: int = 3) -> dict:
    """Schematic ERC grouped by violation type (same format as run_drc)."""
    with tempfile.TemporaryDirectory() as d:
        rep = erc_json(abspath(sch_path), f"{d}/erc.json")
    return report.summarize(rep, max_examples)


@tool
def render_board(pcb_path: str, side: str = "top", out_png: str = "") -> dict:
    """Render a PNG of the board (top|bottom) for visual review; returns the file path.
    Only view it when geometry matters — images cost many tokens."""
    pcb = abspath(pcb_path)
    out = abspath(out_png) if out_png else str(Path(pcb).with_name(Path(pcb).stem + f"_{side}.png"))
    cli("pcb", "render", "--side", side, "-w", "1000", "-h", "700", "--quality", "basic",
        "--background", "opaque", "-o", out, pcb)
    return {"png": out}


# ------------------------------------------------------------------ parçalar

@tool
def search_parts(query: str, package: str = "", basic_only: bool = False,
                 min_stock: int = 100, limit: int = 8) -> dict:
    """Search JLCPCB/LCSC stock. One line per part: 'Cxxxx [B|P|E] MPN | pkg | stock | $ | desc'.
    Prefer [B] basic parts. Example: query='10k 1%', package='0402'."""
    return parts_mod.search(query, package or None, basic_only, min_stock, limit)


# ------------------------------------------------------------------ çıktı

@tool
def export_fab(pcb_path: str, out_dir: str = "") -> dict:
    """Export a JLCPCB-ready package: gerbers+drill zip, BOM.csv, CPL.csv.
    Run run_drc first; refuses nothing but reports parts missing an LCSC field."""
    pcb = abspath(pcb_path)
    out = Path(abspath(out_dir) if out_dir else str(Path(pcb).with_name("fab")))
    gdir = out / "gerbers"
    gdir.mkdir(parents=True, exist_ok=True)
    cli("pcb", "export", "gerbers", "--subtract-soldermask", "--check-zones", "-o", str(gdir) + "/", pcb)
    cli("pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th",
        "-u", "mm", "--generate-map", "--map-format", "gerberx2", "-o", str(gdir) + "/", pcb)

    zpath = out / f"{Path(pcb).stem}-gerbers.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(gdir.iterdir()):
            z.write(f, f.name)

    t = run_worker("fab_tables", {"pcb": pcb})
    with open(out / "BOM.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        w.writerows(t["bom"])
    with open(out / "CPL.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writerows(t["cpl"])
    return {
        "dir": str(out),
        "gerber_zip": str(zpath),
        "files": len(list(gdir.iterdir())),
        "bom_lines": len(t["bom"]),
        "missing_lcsc": t["missing_lcsc"],
        "note": "JLC rotations may need per-footprint offsets; check CPL in their viewer.",
    }


@tool
def import_board(input_path: str, fmt: str = "auto", out_path: str = "") -> dict:
    """Convert a foreign PCB (altium .PcbDoc, eagle, pads, cadstar...) to .kicad_pcb."""
    src = abspath(input_path)
    out = abspath(out_path) if out_path else str(Path(src).with_suffix(".kicad_pcb"))
    proc = cli("pcb", "import", "--format", fmt, "--report-format", "text", "-o", out, src)
    msgs = [l for l in proc.stdout.splitlines() if l.strip() and "Debug:" not in l]
    return {"out": out, "report": msgs[-20:]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
