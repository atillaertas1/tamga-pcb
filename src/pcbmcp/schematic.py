"""Devre tanımından (spec) KiCad şematiği üret.

Bağlantı stratejisi: her bağlı pinin ucuna kısa bir tel + global label. Net adı = label adı,
böylece şematik, netlist ve PCB'de aynı ad kullanılır ('/VIN' değil 'VIN').
UUID'ler deterministik (uuid5), yeniden üretimde PCB bağları ve git diff'leri bozulmaz.
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field

from mcp.server.mcpserver.exceptions import ToolError

from . import library
from .sexpr import Sym, child, children, dump

GRID = 2.54
STUB = 2.54
NS = uuid.UUID("6f1c2b1e-4c55-4b0e-9a8e-2f3d5c7a9b10")
PAPERS = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594)]


def uid(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(map(str, parts))))


def snap(v: float, g: float = GRID) -> float:
    return round(round(v / g) * g, 4)


@dataclass
class Placed:
    ref: str
    lib_id: str
    unit: int
    x: float
    y: float
    uuid: str
    pins: list[dict] = field(default_factory=list)  # bu birimin pinleri (+ unit 0)


def _font(hide: bool = False, justify: str | None = None) -> list:
    eff = [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]]]
    if justify:
        eff.append([Sym("justify"), *[Sym(j) for j in justify.split()]])
    if hide:
        eff.append([Sym("hide"), Sym("yes")])
    return eff


def _prop(name: str, val: str, x: float, y: float, hide: bool, angle: float = 0,
          justify: str | None = None) -> list:
    return [Sym("property"), name, val, [Sym("at"), x, y, angle], _font(hide, justify)]


def normalize(spec: dict) -> dict:
    """Spec'i doğrula; her parçaya çözümlenmiş sembol ve pin->net eşlemesi ekle."""
    if not spec.get("parts"):
        raise ToolError("spec.parts is empty")
    refs = set()
    for p in spec["parts"]:
        for k in ("ref", "symbol"):
            if not p.get(k):
                raise ToolError(f"part {p} missing '{k}'")
        if p["ref"] in refs:
            raise ToolError(f"duplicate ref {p['ref']}")
        refs.add(p["ref"])
    return spec


def _pin_net_map(part: dict, pins: list[dict]) -> tuple[dict[tuple, str], list[str]]:
    """part.pins anahtarları pin numarası veya adı olabilir. (number, unit, x, y) -> net."""
    out: dict[tuple, str] = {}
    errors = []
    for key, net in (part.get("pins") or {}).items():
        key = str(key)
        match = [p for p in pins if p["number"] == key] or [p for p in pins if p["name"] == key]
        if not match:
            avail = " ".join(sorted({f"{p['number']}:{p['name']}" for p in pins}))
            errors.append(f"{part['ref']}: no pin '{key}' (pins: {avail[:200]})")
            continue
        for p in match:
            out[(p["number"], p["unit"], p["x"], p["y"])] = str(net)
    return out, errors


def build(spec: dict, project: str) -> tuple[str, dict]:
    """Şematik metni + PCB senkronu için bilgi döndür."""
    spec = normalize(spec)
    root = uid(project, "root")
    lib_symbols: dict[str, list] = {}
    items: list = []
    placed: list[Placed] = []
    errors: list[str] = []
    part_info: dict[str, dict] = {}

    # --- 1) sembolleri çöz, pin->net eşle
    resolved = []
    for part in spec["parts"]:
        sym = library.resolve_symbol(part["symbol"])
        lib_symbols[part["symbol"]] = sym
        pins = library.symbol_pins(sym)
        pmap, errs = _pin_net_map(part, pins)
        errors += errs
        resolved.append((part, sym, pins, pmap))

    # --- 2) güç bayrağı: power_in pini olup power_out sürücüsü olmayan netler
    net_types: dict[str, set] = {}
    for part, sym, pins, pmap in resolved:
        for (num, unit, x, y), net in pmap.items():
            t = next(p["type"] for p in pins if (p["number"], p["unit"], p["x"], p["y"]) == (num, unit, x, y))
            net_types.setdefault(net, set()).add(t)
    flag_nets = sorted(n for n, ts in net_types.items() if "power_in" in ts and "power_out" not in ts)
    if flag_nets:
        flag_sym = library.resolve_symbol("power:PWR_FLAG")
        lib_symbols["power:PWR_FLAG"] = flag_sym
        fpins = library.symbol_pins(flag_sym)
        for i, net in enumerate(flag_nets, 1):
            fp = {"ref": f"#FLG{i:02d}", "symbol": "power:PWR_FLAG", "value": "PWR_FLAG",
                  "pins": {"1": net}, "_virtual": True}
            pmap, _ = _pin_net_map(fp, fpins)
            resolved.append((fp, flag_sym, fpins, pmap))

    # --- 3) sayfalara ayır: kök (instance'sız parçalar + PWR_FLAG) ve her blok instance'ı ayrı sayfa.
    # Bağlantı yine global label: net adları hiyerarşiden etkilenmez ('/COM5/X' olmaz) -> netlist,
    # PCB ve .kicad_dru kuralları aynı kalır. Hiyerarşi görsel düzen içindir.
    groups: dict[str | None, list] = {None: []}
    for entry in resolved:
        groups.setdefault(entry[0].get("instance"), []).append(entry)
    sheets = [g for g in groups if g is not None]

    def page(entries, inst_path, sheet_path, sheetname, sheetfile, extra_area=0.0):
        """Bir sayfanın sembollerini diz; (items, kullanılan lib_id'ler, kağıt) döndür."""
        items: list = []
        used: dict[str, list] = {}
        boxes = []
        for part, sym, pins, pmap in entries:
            used[part["symbol"]] = sym
            n_units = library.unit_count(sym)
            for unit in range(1, n_units + 1):
                pts = library.body_points(sym, unit)
                xs = [p[0] for p in pts]
                ys = [-p[1] for p in pts]  # sembol Y yukarı -> şematik Y aşağı
                longest = max((len(n) for (num, u, *_), n in pmap.items() if u in (0, unit)), default=0)
                margin = STUB + 2 + longest * 1.1
                boxes.append((part, sym, pins, pmap, unit,
                              min(xs) - margin, max(xs) + margin, min(ys) - 4, max(ys) + 4))
        total_area = sum((b[6] - b[5]) * (b[8] - b[7]) for b in boxes) + extra_area
        paper = next((p for p in PAPERS if total_area * 2.2 < p[1] * p[2]), PAPERS[-1])
        cur_x, cur_y, row_h = 25.4, 30.48, 0.0
        for part, sym, pins, pmap, unit, x0, x1, y0, y1 in boxes:
            w, h = x1 - x0, y1 - y0
            if cur_x + w > paper[1] - 20 and cur_x > 25.4:
                cur_x, cur_y, row_h = 25.4, cur_y + row_h + 5.08, 0.0
            sx, sy = snap(cur_x - x0), snap(cur_y - y0)
            cur_x += w + 5.08
            row_h = max(row_h, h)
            u = uid(project, part["ref"], unit)
            unit_pins = [p for p in pins if p["unit"] in (0, unit)]
            placed.append(Placed(part["ref"], part["symbol"], unit, sx, sy, u, unit_pins))

            # sembol örneği
            props = {p[1]: p for p in children(sym, "property")}

            def ppos(name, dx=0.0, dy=0.0):
                at = child(props.get(name, []), "at") if name in props else None
                if at:
                    return snap(sx + float(at[1]), 0.01), snap(sy - float(at[2]), 0.01), float(at[3]) if len(at) > 3 else 0
                return sx + dx, sy + dy, 0

            virtual = part.get("_virtual", False)
            ref_x, ref_y, ref_a = ppos("Reference")
            val_x, val_y, val_a = ppos("Value")
            inst = [Sym("symbol"), [Sym("lib_id"), part["symbol"]], [Sym("at"), sx, sy, 0],
                    [Sym("unit"), unit], [Sym("exclude_from_sim"), Sym("no")],
                    [Sym("in_bom"), Sym("no" if virtual else "yes")],
                    [Sym("on_board"), Sym("no" if virtual else "yes")],
                    [Sym("dnp"), Sym("yes" if part.get("dnp") else "no")],
                    [Sym("uuid"), u],
                    _prop("Reference", part["ref"], ref_x, ref_y, virtual, ref_a),
                    _prop("Value", str(part.get("value") or part["symbol"].split(":")[1]), val_x, val_y, False, val_a),
                    _prop("Footprint", part.get("footprint", ""), sx, sy, True),
                    _prop("Datasheet", part.get("datasheet", "~"), sx, sy, True),
                    _prop("Description", part.get("description", ""), sx, sy, True)]
            if part.get("lcsc"):
                inst.append(_prop("LCSC", part["lcsc"], sx, sy, True))
            for num in sorted({p["number"] for p in unit_pins}):
                inst.append([Sym("pin"), num, [Sym("uuid"), uid(project, part["ref"], unit, "pin", num)]])
            inst.append([Sym("instances"), [Sym("project"), project,
                         [Sym("path"), inst_path, [Sym("reference"), part["ref"]], [Sym("unit"), unit]]]])
            items.append(inst)

            # tel + global label / no-connect
            nc = part.get("nc") or []
            for p in unit_pins:
                px, py = snap(sx + p["x"], 0.01), snap(sy - p["y"], 0.01)
                net = pmap.get((p["number"], p["unit"], p["x"], p["y"]))
                if net is None:
                    if (nc == "*" or p["number"] in nc or p["name"] in nc) and p["type"] != "no_connect":
                        items.append([Sym("no_connect"), [Sym("at"), px, py],
                                      [Sym("uuid"), uid(project, part["ref"], unit, "nc", p["number"], p["x"], p["y"])]])
                    continue
                a = math.radians(p["angle"])
                dx, dy = -round(math.cos(a)), round(math.sin(a))  # dışa doğru (şematik koordinatı)
                ex, ey = snap(px + dx * STUB, 0.01), snap(py + dy * STUB, 0.01)
                key = (part["ref"], unit, p["number"], p["x"], p["y"])
                items.append([Sym("wire"), [Sym("pts"), [Sym("xy"), px, py], [Sym("xy"), ex, ey]],
                              [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("default")]],
                              [Sym("uuid"), uid(project, *key, "wire")]])
                angle = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[(dx, dy)]
                just = "left" if angle in (0, 90) else "right"
                items.append([Sym("global_label"), net, [Sym("shape"), Sym("passive")],
                              [Sym("at"), ex, ey, angle], [Sym("fields_autoplaced"), Sym("yes")],
                              _font(False, just), [Sym("uuid"), uid(project, *key, "label")],
                              [Sym("property"), "Intersheetrefs", "${INTERSHEET_REFS}",
                               [Sym("at"), ex, ey, 0], _font(True)]])

            if not virtual and part["ref"] not in part_info:
                pad_nets = {}
                for (num, _u, _x, _y), net in pmap.items():
                    pad_nets[num] = net
                part_info[part["ref"]] = {
                    "ref": part["ref"], "value": str(part.get("value") or part["symbol"].split(":")[1]),
                    "footprint": part.get("footprint", ""), "lcsc": part.get("lcsc", ""),
                    "sym_uuid": uid(project, part["ref"], 1), "pads": pad_nets,
                    "path": f"{sheet_path}/{uid(project, part['ref'], 1)}",
                    "sheetname": sheetname, "sheetfile": sheetfile,
                    "dnp": bool(part.get("dnp")),
                }

        bottom = cur_y + row_h
        return items, used, paper, bottom

    # sayfa dosyaları: <proje>-<instance>.kicad_sch
    pages: dict[str, str] = {}
    sheet_boxes = []
    for i, name in enumerate(sheets, 2):
        suid = uid(project, "sheet", name)
        fname = f"{project}-{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.kicad_sch"
        items, used, paper, _ = page(groups[name], f"/{root}/{suid}", f"/{suid}", f"/{name}/", fname)
        sub = [Sym("kicad_sch"), [Sym("version"), 20250114], [Sym("generator"), "pcbmcp"],
               [Sym("generator_version"), "0.2"], [Sym("uuid"), uid(project, "page", name)],
               [Sym("paper"), paper[0]], [Sym("title_block"), [Sym("title"), name]],
               [Sym("lib_symbols"), *used.values()], *items, [Sym("embedded_fonts"), Sym("no")]]
        pages[fname] = dump(sub) + "\n"
        sheet_boxes.append((name, suid, fname, i))

    # kök sayfa: kalan parçalar + her instance için sheet kutusu (portlar kutu içinde yazı)
    # blocks.expand her instance'ın port eşlemesini "sheets"e yazar: ["TX = UART5_TX", ...]
    ports_of = {name: spec.get("sheets", {}).get(name, {}).get("ports", []) for name in sheets}
    box_area = sum(40 * (10 + 2.54 * len(ports_of[n])) for n in sheets)
    items, used, paper, bottom = page(groups[None], f"/{root}", "", "/", f"{project}.kicad_sch", box_area)
    bx, by, row_h = 25.4, snap(bottom + 10.16), 0.0
    for name, suid, fname, page_no in sheet_boxes:
        w, h = 38.1, snap(10.16 + 2.54 * len(ports_of[name]))
        if bx + w > paper[1] - 20 and bx > 25.4:
            bx, by, row_h = 25.4, snap(by + row_h + 10.16), 0.0
        items.append([Sym("sheet"), [Sym("at"), bx, by], [Sym("size"), w, h],
                      [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")],
                      [Sym("on_board"), Sym("yes")], [Sym("dnp"), Sym("no")],
                      [Sym("fields_autoplaced"), Sym("yes")],
                      [Sym("stroke"), [Sym("width"), 0.1524], [Sym("type"), Sym("solid")]],
                      [Sym("fill"), [Sym("color"), 0, 0, 0, 0.0]],
                      [Sym("uuid"), suid],
                      _prop("Sheetname", name, bx, snap(by - 0.71, 0.01), False, 0, "left bottom"),
                      _prop("Sheetfile", fname, bx, snap(by + h + 0.59, 0.01), False, 0, "left top"),
                      [Sym("instances"), [Sym("project"), project,
                                          [Sym("path"), f"/{root}", [Sym("page"), str(page_no)]]]]])
        if ports_of[name]:
            items.append([Sym("text"), "\n".join(ports_of[name]), [Sym("exclude_from_sim"), Sym("no")],
                          [Sym("at"), snap(bx + 1.27, 0.01), snap(by + 2.54, 0.01), 0],
                          _font(False, "left top"), [Sym("uuid"), uid(project, "sheet-ports", name)]])
        bx += w + 12.7
        row_h = max(row_h, h)

    doc = [Sym("kicad_sch"), [Sym("version"), 20250114], [Sym("generator"), "pcbmcp"],
           [Sym("generator_version"), "0.2"], [Sym("uuid"), root], [Sym("paper"), paper[0]]]
    if spec.get("title"):
        doc.append([Sym("title_block"), [Sym("title"), spec["title"]]])
    doc.append([Sym("lib_symbols"), *used.values()])
    doc += items
    doc.append([Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]])
    doc.append([Sym("embedded_fonts"), Sym("no")])

    meta = {"root_uuid": root, "parts": list(part_info.values()), "errors": errors,
            "pwr_flags": flag_nets, "paper": paper[0], "pages": pages}
    return dump(doc) + "\n", meta
