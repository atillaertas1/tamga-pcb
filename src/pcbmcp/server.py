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

import anyio.from_thread
from mcp.server.mcpserver import Context, MCPServer

from . import design, importer, library, pinout, report, route
from . import parts as parts_mod
from .kicad import KicadError, abspath, cli, drc_json, erc_json, run_worker

INSTRUCTIONS = """KiCad PCB tools. Units: mm, degrees, KiCad page coords (Y grows down).
New design: find_symbol -> get_symbol(+footprint) -> search_parts -> sync_design(spec);
edits later via sync_design(patch). Never hand-edit .kicad_sch; change the spec.
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


# ------------------------------------------------------------------ tasarım (şematik + PCB)

@tool
def find_symbol(query: str, limit: int = 10) -> dict:
    """Search KiCad symbol libraries. Returns 'Lib:Name | description | fp=default footprint'."""
    return {"symbols": library.find_symbols(query, limit)}


@tool
def get_symbol(lib_id: str, footprint: str = "", lcsc: str = "") -> dict:
    """Pins of a symbol as 'number:name:type' (+unit). Call before using a symbol in a spec.
    If footprint given, also lists its pad numbers and a pin<->pad check
    (missing_pads = error, unused_pads = pad left unconnected).
    If lcsc given, compares pin names with the real part's pinout (mismatch = wrong symbol
    variant or wrong part; part_only = extra pins such as a tab)."""
    out = library.describe_symbol(lib_id)
    if footprint:
        out["footprint_pads"] = " ".join(library.footprint_pads(footprint))
        out["pad_check"] = library.check_pin_pad(lib_id, footprint)
    if lcsc:
        out["pinout_check"] = pinout.check(lib_id, lcsc)
    return out


@tool
def find_footprint(query: str = "", pattern: str = "", limit: int = 15) -> dict:
    """Search footprints. query: words that must all appear ('R 0805', 'soic 8').
    pattern: symbol fp_filters wildcards ('R_* LED*'). Returns 'Lib:Name'."""
    return {"footprints": library.find_footprints(query, pattern, limit)}


@tool
def import_lcsc_part(lcsc: str, compare_footprint: str = "") -> dict:
    """Import an LCSC part (symbol + footprint + 3D) into the pcbmcp_lcsc library when no
    KiCad symbol/footprint fits. Checks pin<->pad and, if KiCad has the same part's footprint
    (or compare_footprint given), whether pad numbering matches. On kicad_footprint_mismatch
    do NOT mix: use the KiCad footprint with a KiCad-numbered symbol. Verify vs datasheet."""
    return importer.import_part(lcsc, compare_footprint)


@tool
def sync_design(project_dir: str, name: str, spec: dict | None = None,
                patch: dict | None = None) -> dict:
    """Create/update schematic + PCB from a circuit spec (single source of truth,
    saved as <name>.circuit.json). PCB placement/routing of existing parts is preserved.
    spec: {title?, board:{width_mm,height_mm}, parts:[{ref, symbol:'Lib:Name',
      footprint:'Lib:Name', value?, lcsc?, dnp?, pins:{pin_number_or_name: net}, nc?:[pins]|'*'}]}
    patch (instead of spec, for edits): {parts:[partial parts merged by ref], remove:[refs]}.
    Unlisted pins stay unconnected (ERC flags them) unless in nc. PWR_FLAGs are added
    automatically. Checks: netlist vs spec, ERC, schematic<->PCB parity."""
    if spec is None and patch is None:
        raise KicadError("pass spec (new design) or patch (edit)")
    return design.sync(project_dir, name, spec, patch)


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
def add_copper_zone(pcb_path: str, net: str = "GND", layer: str = "both",
                    clearance_mm: float = 0.3, stitch_pitch_mm: float = 5.0,
                    polygon_mm: list[list[float]] | None = None, priority: int = 0,
                    keepout_tracks: bool = False) -> dict:
    """Add a copper pour over the whole board (layer 'F.Cu'|'B.Cu'|'both') and fill.
    With 'both', stitching vias every stitch_pitch_mm tie the layers (0 = none); this avoids
    isolated-island / starved_thermal DRC errors. Run after autoroute.
    polygon_mm [[x,y],...] limits the pour to a region (e.g. a high-current rail into
    fine-pitch pads); give it priority > 0 so a later full-board GND pour flows around it.
    keepout_tracks=True instead adds a no-tracks rule area over polygon_mm (before autoroute,
    so other nets don't cut that pour)."""
    return run_worker("add_zone", {"pcb": abspath(pcb_path), "net": net, "layer": layer,
                                   "clearance_mm": clearance_mm, "stitch_pitch_mm": stitch_pitch_mm,
                                   "polygon_mm": polygon_mm, "priority": priority,
                                   "keepout_tracks": keepout_tracks})


@tool
def fill_zones(pcb_path: str) -> dict:
    """Refill all copper zones and save."""
    return run_worker("fill_zones", {"pcb": abspath(pcb_path)})


@tool
def autoroute(pcb_path: str, out_path: str = "", max_passes: int = 100,
              ignore_netclasses: list[str] | None = None, timeout_s: int = 900,
              ctx: Context = None) -> dict:
    """Autoroute with Freerouting (headless). Writes <name>_routed.kicad_pcb unless out_path
    given. Reports unrouted before/after. Route critical nets by hand first, then run this.
    Sends progress per pass; on timeout reports the pass reached (nothing is written)."""
    on_progress = None
    if ctx is not None:
        def on_progress(msg: str, n: int) -> None:
            try:  # araç thread'de çalışıyor; async bildirimi event loop'a gönder
                anyio.from_thread.run(ctx.report_progress, n, max_passes, msg)
            except Exception:  # istemci progress token vermediyse vb. — rotayı bozma
                pass
    return route.autoroute(abspath(pcb_path), abspath(out_path) if out_path else None,
                           max_passes, ignore_netclasses, timeout_s, on_progress)


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
    import sys
    if sys.argv[1:2] == ["doctor"]:
        from . import doctor
        sys.exit(doctor.main(sys.argv[2:]))
    mcp.run()


if __name__ == "__main__":
    main()
