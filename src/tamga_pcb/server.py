"""tamga-pcb — KiCad için token-verimli MCP sunucusu.

Tasarım ilkeleri:
- Az araç (her araç tanımı her istekte bağlama girer).
- Ham dosya yok; kompakt, satır-başı-bir-öğe çıktı.
- Yazan her araç önce .tamga_backups/ altına yedek alır.
- Koordinatlar mm, açılar derece, KiCad'in sayfa koordinatları (Y aşağı doğru artar).
"""
from __future__ import annotations

import csv
import functools
import inspect
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path

import anyio.from_thread
from mcp.server.mcpserver import Context, MCPServer

from . import design, impedance, importer, library, partgen, paths, pinout, report, route
from . import parts as parts_mod
from .kicad import KicadError, abspath, cli, drc_json, erc_json, run_worker

INSTRUCTIONS = """KiCad PCB tools. Units: mm, degrees, KiCad page coords (Y grows down).
New design: find_symbol -> get_symbol(+footprint) -> search_parts -> sync_design(spec);
edits later via sync_design(patch). Never hand-edit .kicad_sch; change the spec.
Loop: board_summary -> place_footprints -> set_netclass (power nets wider) -> autoroute
-> add_copper_zone GND -> run_drc until CLEAN -> set_footprint_fields LCSC -> export_fab.
Never read .kicad_pcb files directly; use these tools. Write tools keep backups."""

mcp = MCPServer("tamga-pcb", instructions=INSTRUCTIONS, version="0.1.0")


_last_project: list[Path] = []  # klasör vermeyen araçların (search_parts...) günlüğü buraya
LIVE_LOG = paths.DATA_DIR / "live" / "steps.jsonl"  # tamga watch bunu izler


def _short(v, n: int = 60):
    if isinstance(v, (list, tuple)):
        return f"[{len(v)}]"
    if isinstance(v, dict):
        return f"{{{len(v)}}}"
    if isinstance(v, str) and ("/" in v or "\\" in v) and Path(v).suffix:
        return Path(v).name
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def log_step(name: str, bound: dict, result, error: str | None, ms: int) -> None:
    """Canlı izleme (tamga watch) için <proje>/.tamga/steps.jsonl'e tek satır. Hata asla araca yansımaz."""
    if os.environ.get("TAMGA_STEP_LOG") == "0":
        return
    try:
        where = next((bound[k] for k in ("pcb_path", "project_dir", "sch_path", "input_path") if bound.get(k)), None)
        if where:
            d = Path(abspath(where))
            d = d if d.is_dir() or not d.suffix else d.parent
            _last_project[:] = [d]
        d = _last_project[0] if _last_project else None
        summary = {}
        if isinstance(result, dict):
            for k, v in result.items():
                if k in ("backup", "note", "legend", "pcb", "out", "files", "dir", "steps"):
                    continue
                summary[k] = _short(v)
                if len(summary) >= 6:
                    break
        args = {k: _short(v) for k, v in bound.items() if k not in ("ctx",) and v not in (None, "", [], {})}
        row = {"t": time.time(), "tool": name, "args": args, "ms": ms, "project": str(d) if d else None,
               **({"error": error[:300]} if error else {"result": summary})}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        # genel canlı günlük: proje klasörü henüz yokken (ilk parça aramaları) de görünsün
        live = LIVE_LOG
        live.parent.mkdir(parents=True, exist_ok=True)
        if live.is_file() and live.stat().st_size > 5_000_000:
            live.replace(live.with_suffix(".1.jsonl"))
        with open(live, "a", encoding="utf-8") as f:
            f.write(line)
        if d:
            (d / ".tamga").mkdir(exist_ok=True)
            with open(d / ".tamga" / "steps.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:  # günlük yan iş: araç sonucunu asla bozmasın
        pass


def tool(fn):
    """MCP aracı olarak kaydet, ama sonucu tek satır kompakt JSON metin olarak gönder.

    mcp 2.x dict'leri indent=2 ile serileştiriyor (~%30 boşa token). Orijinal fonksiyon
    dict döndürmeye devam eder (testler ve Python'dan kullanım için). Her çağrı canlı izleme
    günlüğüne de yazılır (log_step).
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def logged(*args, **kwargs) -> dict:  # Python'dan çağıranlar da (build.py) günlüğe düşsün
        t0 = time.monotonic()
        try:
            bound = dict(sig.bind_partial(*args, **kwargs).arguments)
        except TypeError:
            bound = dict(kwargs)
        try:
            res = fn(*args, **kwargs)
        except Exception as e:
            log_step(fn.__name__, bound, None, str(e), int((time.monotonic() - t0) * 1000))
            raise
        log_step(fn.__name__, bound, res, None, int((time.monotonic() - t0) * 1000))
        return res

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        return json.dumps(logged(*args, **kwargs), ensure_ascii=False, separators=(",", ":"))

    del wrapper.__wrapped__  # şema dönüş tipini fn'den (dict) değil wrapper'dan (str) alsın
    wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=str)
    wrapper.__annotations__ = {**fn.__annotations__, "return": "str"}
    mcp.tool(structured_output=False)(wrapper)
    return logged


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
    """Import an LCSC part (symbol + footprint + 3D) into the tamga_lcsc library when no
    KiCad symbol/footprint fits. Checks pin<->pad and, if KiCad has the same part's footprint
    (or compare_footprint given), whether pad numbering matches. On kicad_footprint_mismatch
    do NOT mix: use the KiCad footprint with a KiCad-numbered symbol. Verify vs datasheet."""
    return importer.import_part(lcsc, compare_footprint)


@tool
def live_view(project_dir: str = "", port: int = 8765, open_browser: bool = True) -> dict:
    """Open a browser page that shows the board, schematic and every tool step live while you work
    (only for the user; costs no tokens). Call when the user wants to watch; it keeps running."""
    from . import watch
    url, new = watch.start(Path(abspath(project_dir)) if project_dir else None, port)
    if new and open_browser:
        import webbrowser
        webbrowser.open(url)
    return {"url": url, "started": new, "following": project_dir or "the latest project"}


@tool
def create_part(name: str, pins: list[dict] | None = None, package: dict | None = None,
                reference: str = "U", footprint: str = "", description: str = "") -> dict:
    """Make a symbol and/or footprint that neither KiCad nor LCSC has, in the tamga_user library.
    pins: [{number, name, type?: input|output|bidirectional|passive|power_in|power_out, side?}].
    package: {type: soic|qfn|dfn|header|pads, pins, pitch_mm, body_w_mm?, body_l_mm?, lead_span_mm?
      (soic tip-to-tip), lead_len_mm?, ep_mm? (qfn/dfn thermal pad, number pins+1), rows? (header),
      pads? ([{number,x,y,w,h,drill?}] for 'pads')}. Pads follow datasheet numbering (soic: 1 top-left,
    down then up the right side; qfn: counter-clockwise from top-left). footprint links the symbol to an
    existing footprint instead. Check the datasheet's land pattern before ordering."""
    return partgen.create(name, pins, package, reference, footprint, description)


@tool
def sync_design(project_dir: str, name: str, spec: dict | None = None,
                patch: dict | None = None) -> dict:
    """Create/update schematic + PCB from a circuit spec (single source of truth,
    saved as <name>.circuit.json). PCB placement/routing of existing parts is preserved.
    spec: {title?, board:{width_mm, height_mm, corner_radius_mm?, outline_mm?:[[x,y],..], layers?:2|4|6,
      mounting_holes?:{diameter_mm:2.2|2.7|3.2|4.3, inset_mm?, at_mm?, plated?}, silk?:[{text,x,y,size_mm?,layer?:F|B}]},
      parts:[{ref, symbol:'Lib:Name',
      footprint:'Lib:Name', value?, lcsc?, dnp?, pins:{pin_number_or_name: net}, nc?:[pins]|'*'}]}
    patch (instead of spec, for edits): {parts:[partial parts merged by ref], remove:[refs]}.
    Blocks (reusable subcircuits): spec.blocks {name:{ports:[..], parts:[..]}} + spec.instances
      [{block, name, nets?:{port:net|null}, set?:{local_ref:{value,lcsc,..}}, refs?:{local:ref},
      ref_base?, net_prefix?}]. Unmapped port = same-named global net; other nets become
      <net_prefix><net> (default '<name>_'). patch: instances (merge by name), blocks, remove_instances.
      Renaming nets can silently disable .kicad_dru rules: watch nets_gone / dru_rules_reference_gone_nets.
    Unlisted pins stay unconnected (ERC flags them) unless in nc. PWR_FLAGs are added
    automatically. Checks: netlist vs spec, ERC, schematic<->PCB parity."""
    if spec is None and patch is None:
        raise KicadError("pass spec (new design) or patch (edit)")
    return design.sync(project_dir, name, spec, patch)


# ------------------------------------------------------------------ okuma

@tool
def board_summary(pcb_path: str, max_footprints: int = 40, lengths: list[str] | None = None) -> dict:
    """Board overview: size, layers, rules, unrouted count, nets (name:pad_count),
    one line per footprint 'REF VALUE FOOTPRINT @(x,y) rDEG F|B'. Start here.
    lengths=['USB_D*', ...] instead returns routed length and vias per net, and skew of P/N pairs."""
    return run_worker("summary", {"pcb": abspath(pcb_path), "max_footprints": max_footprints,
                                  "lengths": lengths or []})


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
                 apply_to_existing: bool = False, impedance_ohm: float | None = None,
                 diff_gap_mm: float | None = None) -> dict:
    """Create/update a netclass and assign nets (exact names or wildcards like '+*V*').
    name='Default' edits the default class. Omitted values are kept. Set BEFORE autoroute;
    the router uses these widths. apply_to_existing resizes already-routed tracks/vias.
    impedance_ohm computes the track width from the board's stackup (with diff_gap_mm: a
    differential pair, e.g. 90 for USB); warns when 2 layers make it impractical."""
    imp = None
    if impedance_ohm:
        layers = run_worker("summary", {"pcb": abspath(pcb_path), "max_footprints": 0})["copper_layers"]
        imp = impedance.track_for(impedance_ohm, layers, diff_gap_mm)
        track_mm = imp["track_mm"]
    res = run_worker("set_netclass", {
        "pcb": abspath(pcb_path), "name": name, "nets": nets or [],
        "track_mm": track_mm, "clearance_mm": clearance_mm, "via_mm": via_mm,
        "via_drill_mm": via_drill_mm, "apply_to_existing": apply_to_existing,
        "dp_width_mm": track_mm if diff_gap_mm else None, "dp_gap_mm": diff_gap_mm,
    })
    if imp:
        res["impedance"] = imp
    return res


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
def route_net(pcb_path: str, net: str, from_pad: str, to_pad: str,
              points: list[list] | None = None, layer: str = "F.Cu", width_mm: float = 0,
              pair_net: str = "", pair_from_pad: str = "", pair_to_pad: str = "", gap_mm: float = 0) -> dict:
    """Hand-guided route for a critical net: from_pad ('U1.3') -> points -> to_pad.
    points: [[x,y], [x,y,'B.Cu'], ...] mm; a layer change drops a via at that point.
    Width defaults to the net's netclass. Tracks are locked so autoroute keeps them.
    Differential pair: give pair_net with its pads; points are then the pair's centre line and both
    tracks run gap_mm apart (defaults from set_netclass impedance). Reports length and skew.
    Route these BEFORE autoroute; then run_drc (no clearance check here)."""
    if pair_net and not (pair_from_pad and pair_to_pad):
        raise KicadError("pair_net needs pair_from_pad and pair_to_pad")
    return run_worker("route_net", {"pcb": abspath(pcb_path), "net": net, "from_pad": from_pad,
                                    "to_pad": to_pad, "points": points or [], "layer": layer,
                                    "width_mm": width_mm or None, "pair_net": pair_net,
                                    "pair_from_pad": pair_from_pad, "pair_to_pad": pair_to_pad,
                                    "gap_mm": gap_mm or None})


@tool
def autoroute(pcb_path: str, out_path: str = "", max_passes: int = 100,
              ignore_netclasses: list[str] | None = None, timeout_s: int = 900,
              rip_up: bool = True, ctx: Context = None) -> dict:
    """Autoroute with Freerouting (headless). Writes the board in place (backup kept) unless
    out_path given; a board under another name loses the project's rules (see run_drc). Reports unrouted before/after. Route critical nets by hand first, then run this.
    rip_up (default) first removes unlocked tracks/vias, so moving parts and rerouting is safe; locked
    route_net tracks stay. Re-run add_copper_zone afterwards (it replaces the old pour).
    Sends progress per pass; on timeout reports the pass reached (nothing is written)."""
    on_progress = None
    if ctx is not None:
        def on_progress(msg: str, n: int) -> None:
            try:  # araç thread'de çalışıyor; async bildirimi event loop'a gönder
                anyio.from_thread.run(ctx.report_progress, n, max_passes, msg)
            except Exception:  # istemci progress token vermediyse vb. — rotayı bozma
                pass
    return route.autoroute(abspath(pcb_path), abspath(out_path) if out_path else None,
                           max_passes, ignore_netclasses, timeout_s, on_progress, rip_up)


# ------------------------------------------------------------------ doğrulama

@tool
def run_drc(pcb_path: str, refill_zones: bool = True, schematic_parity: bool = False,
            max_examples: int = 3) -> dict:
    """DRC grouped by violation type: count, affected nets, a few example locations.
    'verdict: CLEAN' when nothing found. If the board has no same-named .kicad_pro, project
    netclasses and .kicad_dru rules are NOT applied: rules_warning, and CLEAN is downgraded."""
    with tempfile.TemporaryDirectory() as d:
        rep = drc_json(abspath(pcb_path), f"{d}/drc.json", refill_zones, schematic_parity)
    out = report.summarize(rep, max_examples)
    if warn := _rules_warning(Path(abspath(pcb_path))):
        out["rules_warning"] = warn
        if out.get("verdict") == "CLEAN" and "project file here" in warn:
            out["verdict"] = "CLEAN_WITHOUT_PROJECT_RULES"
    return out


def _rules_warning(pcb: Path) -> str | None:
    """KiCad kuralları kartla AYNI ADLI .kicad_pro / .kicad_dru'dan okur. Kopya/yeniden
    adlandırılmış kartta DRC varsayılan kurallarla koşar ve özel kuralları sessizce atlar."""
    if pcb.with_suffix(".kicad_pro").is_file():
        return None
    msg = f"no {pcb.with_suffix('.kicad_pro').name} next to the board: DRC used DEFAULT rules"
    if others := sorted(p.name for p in pcb.parent.glob("*.kicad_pro")):
        msg += f"; project file here is {others[0]}: run DRC on that board name"
    if drus := sorted(p.name for p in pcb.parent.glob("*.kicad_dru")):
        msg += f"; custom rules in {drus[0]} were skipped"
    return msg


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
def export_fab(pcb_path: str, out_dir: str = "", extras: list[str] | None = None) -> dict:
    """Export a JLCPCB-ready package: gerbers+drill zip, BOM.csv, CPL.csv.
    Run run_drc first; refuses nothing but reports parts missing an LCSC field.
    extras: any of schematic_pdf, step (3D), odb (ODB++), ipc2581, cost (parts cost, extended parts, stock)."""
    pcb = abspath(pcb_path)
    out = Path(abspath(out_dir) if out_dir else str(Path(pcb).with_name("fab")))
    gdir = out / "gerbers"
    gdir.mkdir(parents=True, exist_ok=True)
    # Yalnız üretimde kullanılan katmanlar (varsayılan dışa aktarma Adhesive/Courtyard/Fab da yazıyor)
    n = run_worker("summary", {"pcb": pcb, "max_footprints": 0})["copper_layers"]
    copper = ["F.Cu", *(f"In{i}.Cu" for i in range(1, n - 1)), "B.Cu"]
    layers = copper + ["F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask", "Edge.Cuts"]
    cli("pcb", "export", "gerbers", "--subtract-soldermask", "--check-zones", "--layers", ",".join(layers),
        "-o", str(gdir) + "/", pcb)
    cli("pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th",
        "-u", "mm", "--generate-map", "--map-format", "gerberx2", "-o", str(gdir) + "/", pcb)

    zpath = out / f"{Path(pcb).stem}-gerbers.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(gdir.iterdir()):
            z.write(f, f.name)

    t = run_worker("fab_tables", {"pcb": pcb})
    with open(out / "BOM.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        w.writerows(t["bom"])
    with open(out / "CPL.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writerows(t["cpl"])
    res = {
        "dir": str(out),
        "gerber_zip": str(zpath),
        "files": len(list(gdir.iterdir())),
        "bom_lines": len(t["bom"]),
        "missing_lcsc": t["missing_lcsc"],
        "note": "JLC rotations may need per-footprint offsets; check CPL in their viewer.",
    }
    stem = Path(pcb).stem
    unknown = set(extras or []) - {"schematic_pdf", "step", "odb", "ipc2581", "cost"}
    if unknown:
        raise KicadError(f"unknown extras {sorted(unknown)}; use schematic_pdf, step, odb, ipc2581, cost")
    for x in extras or []:
        if x == "schematic_pdf":
            sch = Path(pcb).with_suffix(".kicad_sch")
            if not sch.is_file():
                raise KicadError(f"no schematic next to the board ({sch.name})")
            cli("sch", "export", "pdf", "-o", str(out / f"{stem}-schematic.pdf"), str(sch))
            res["schematic_pdf"] = str(out / f"{stem}-schematic.pdf")
        elif x == "step":
            cli("pcb", "export", "step", "--force", "--subst-models", "--no-dnp",
                "-o", str(out / f"{stem}.step"), pcb, timeout=600)
            res["step"] = str(out / f"{stem}.step")
        elif x == "odb":
            cli("pcb", "export", "odb", "-o", str(out / f"{stem}-odb.zip"), pcb)
            res["odb"] = str(out / f"{stem}-odb.zip")
        elif x == "ipc2581":
            cli("pcb", "export", "ipc2581", "-o", str(out / f"{stem}.xml"), pcb)
            res["ipc2581"] = str(out / f"{stem}.xml")
        elif x == "cost":
            res["cost"] = parts_mod.bom_cost(t["bom"])
    return res


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
    if sys.argv[1:2] == ["watch"]:
        from . import watch
        sys.exit(watch.main(sys.argv[2:]))
    mcp.run()


if __name__ == "__main__":
    main()
