"""Kütüphanede olmayan parça için footprint ve sembol üret (tamga_user kütüphanesi).

Paketler: soic (SOP/SSOP/TSSOP/MSOP, iki sıra martı bacak), qfn (4 kenar, termal pad), dfn (2 kenar,
termal pad), header (THT, 1-2 sıra), pads (pad'leri tek tek ver). Ölçüler mm; verilmeyenler IPC-7351
nominaline yakın varsayılanlardan. Sonuç kicad-cli ile KiCad'in güncel biçimine çevrilip doğrulanır.
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import library, paths, sexpr
from .kicad import cli
from .sexpr import Sym

LIB = "tamga_user"
S = Sym


def _paths() -> dict[str, Path]:
    return {"sym": paths.DATA_DIR / "symbols" / f"{LIB}.kicad_sym", "fp": paths.DATA_DIR / "footprints" / f"{LIB}.pretty"}


def _r(v: float) -> float:
    return round(v, 4)


# ------------------------------------------------------------------ footprint geometrisi

def _lead_width(pitch: float) -> float:
    """Bacak genişliği (gull-wing için en büyük, QFN için nominal) — JEDEC tipik değerleri."""
    return 0.51 if pitch >= 1.0 else 0.30 if pitch >= 0.65 else 0.25 if pitch >= 0.5 else 0.2


def pads_for(pkg: dict) -> tuple[list[dict], tuple[float, float], str]:
    """-> (pad listesi {number,x,y,w,h,shape,type,drill?}, gövde (w,l), açıklama)"""
    kind = pkg.get("type")
    pitch = float(pkg.get("pitch_mm", 2.54 if kind == "header" else 1.27 if kind == "soic" else 0.5))
    if kind == "soic":
        n = int(pkg["pins"])
        if n % 2:
            raise ToolError("soic needs an even pin count")
        bw = float(pkg.get("body_w_mm", 3.9))
        bl = float(pkg.get("body_l_mm", (n // 2) * pitch + 0.1))
        span = float(pkg.get("lead_span_mm", bw + 2.1))         # bacak ucundan ucuna
        lead = float(pkg.get("lead_len_mm", 1.27 if pitch >= 1.0 else 0.75))  # en uzun bacak (L max)
        # IPC-7351 nominal payları (KiCad'in kendi üreteci gibi); ince adımda parmak ucu kısa, topuk uzun
        toe, heel, extra = (0.45, 0.23, 0.09) if pitch >= 1.0 else (0.4, 0.325, 0.1)
        outer, inner = span / 2 + toe, span / 2 - lead - heel
        pw = min(_lead_width(pitch) + extra, pitch - 0.2)
        cx, plen = (outer + inner) / 2, outer - inner
        rows = n // 2
        pads = []
        for i in range(rows):
            y = (i - (rows - 1) / 2) * pitch
            pads.append({"number": str(i + 1), "x": -cx, "y": y, "w": plen, "h": pw})
            pads.append({"number": str(n - i), "x": cx, "y": y, "w": plen, "h": pw})
        return pads, (bw, bl), f"{n}-pin gull-wing, {pitch} mm pitch, {span} mm lead span"
    if kind in ("qfn", "dfn"):
        n = int(pkg["pins"])
        sides = 4 if kind == "qfn" else 2
        if n % sides:
            raise ToolError(f"{kind} pin count must be a multiple of {sides}")
        bw = float(pkg.get("body_w_mm", pkg.get("body_mm", 3.0)))
        bl = float(pkg.get("body_l_mm", pkg.get("body_mm", bw)))
        lead = float(pkg.get("lead_len_mm", 0.4))
        pw = min(_lead_width(pitch), pitch - 0.2)
        toe, heel = 0.375, 0.05                                   # gövde dışına parmak ucu, içe topuk
        plen = lead + toe + heel
        per = n // sides
        pads, num = [], 1
        offs = [(i - (per - 1) / 2) * pitch for i in range(per)]
        # sol kenar yukarıdan aşağı, alt kenar soldan sağa, sağ aşağıdan yukarı, üst sağdan sola (CCW)
        for y in offs:
            pads.append({"number": str(num), "x": -bw / 2 - toe + plen / 2, "y": y, "w": plen, "h": pw}); num += 1
        if sides == 4:
            for x in offs:
                pads.append({"number": str(num), "x": x, "y": bl / 2 + toe - plen / 2, "w": pw, "h": plen}); num += 1
        for y in reversed(offs):
            pads.append({"number": str(num), "x": bw / 2 + toe - plen / 2, "y": y, "w": plen, "h": pw}); num += 1
        if sides == 4:
            for x in reversed(offs):
                pads.append({"number": str(num), "x": x, "y": -bl / 2 - toe + plen / 2, "w": pw, "h": plen}); num += 1
        ep = pkg.get("ep_mm")
        if ep:
            ew, el = (ep, ep) if not isinstance(ep, list) else ep
            pads.append({"number": str(n + 1), "x": 0.0, "y": 0.0, "w": float(ew), "h": float(el)})
        return pads, (bw, bl), f"{n}-pin {kind.upper()}, {pitch} mm pitch, {bw}x{bl} mm" + (", exposed pad" if ep else "")
    if kind == "header":
        rows, cols = int(pkg.get("rows", 1)), int(pkg["pins"]) // int(pkg.get("rows", 1))
        drill, size = float(pkg.get("drill_mm", 1.0)), float(pkg.get("pad_mm", 1.7))
        pads = []
        for c in range(cols):
            for r in range(rows):
                num = c * rows + r + 1
                pads.append({"number": str(num), "x": r * pitch, "y": c * pitch, "w": size, "h": size,
                             "type": "thru_hole", "drill": drill, "shape": "rect" if num == 1 else "circle"})
        return pads, (rows * pitch, cols * pitch), f"{rows}x{cols} header, {pitch} mm pitch"
    if kind == "pads":
        pads = [{"number": str(p["number"]), "x": float(p["x"]), "y": float(p["y"]), "w": float(p["w"]),
                 "h": float(p["h"]), "shape": p.get("shape", "roundrect"),
                 **({"type": "thru_hole", "drill": float(p["drill"])} if p.get("drill") else {})} for p in pkg["pads"]]
        xs = [p["x"] for p in pads]
        ys = [p["y"] for p in pads]
        bw = float(pkg.get("body_w_mm", max(xs) - min(xs) + 1))
        bl = float(pkg.get("body_l_mm", max(ys) - min(ys) + 1))
        return pads, (bw, bl), f"{len(pads)} custom pads"
    raise ToolError("package.type must be soic, qfn, dfn, header or pads")


def footprint(name: str, pkg: dict, description: str = "") -> list:
    pads, (bw, bl), desc = pads_for(pkg)
    cx = (max(p["x"] for p in pads) + min(p["x"] for p in pads)) / 2   # gövde pad'lerin ortasında
    cy = (max(p["y"] for p in pads) + min(p["y"] for p in pads)) / 2
    x0, x1, y0, y1 = cx - bw / 2, cx + bw / 2, cy - bl / 2, cy + bl / 2
    px0 = min(p["x"] - p["w"] / 2 for p in pads)
    px1 = max(p["x"] + p["w"] / 2 for p in pads)
    py0 = min(p["y"] - p["h"] / 2 for p in pads)
    py1 = max(p["y"] + p["h"] / 2 for p in pads)
    cy0, cy1 = min(y0, py0) - 0.25, max(y1, py1) + 0.25
    cx0, cx1 = min(x0, px0) - 0.25, max(x1, px1) + 0.25
    tht = any(p.get("type") == "thru_hole" for p in pads)
    stroke = lambda w: [S("stroke"), [S("width"), w], [S("type"), S("solid")]]  # noqa: E731

    def line(a, b, layer, w):
        return [S("fp_line"), [S("start"), _r(a[0]), _r(a[1])], [S("end"), _r(b[0]), _r(b[1])], stroke(w), [S("layer"), layer]]

    items = [S("footprint"), name, [S("version"), 20241229], [S("generator"), "tamga-pcb"], [S("layer"), "F.Cu"],
             [S("descr"), (description + "; " if description else "") + desc], [S("tags"), pkg.get("type", "")],
             [S("property"), "Reference", "REF**", [S("at"), _r(cx), _r(cy0 - 1.0), 0], [S("layer"), "F.SilkS"],
              [S("effects"), [S("font"), [S("size"), 1, 1], [S("thickness"), 0.15]]]],
             [S("property"), "Value", name, [S("at"), _r(cx), _r(cy1 + 1.0), 0], [S("layer"), "F.Fab"],
              [S("effects"), [S("font"), [S("size"), 1, 1], [S("thickness"), 0.15]]]],
             [S("attr"), S("through_hole" if tht else "smd")]]
    # fab: gövde, pin 1 köşesi pahlı
    ch = min(1.0, bw / 4, bl / 4)
    fab = [(x0 + ch, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0 + ch)]
    for a, b in zip(fab, fab[1:] + fab[:1]):
        items.append(line(a, b, "F.Fab", 0.1))
    # courtyard
    cyd = [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1)]
    for a, b in zip(cyd, cyd[1:] + cyd[:1]):
        items.append(line(a, b, "F.CrtYd", 0.05))
    # silk: gövde çevresi, pad'lere 0.2 mm'den yakın parçalar atlanır
    so = 0.12
    edges = [((x0 - so, y0 - so), (x1 + so, y0 - so)), ((x1 + so, y0 - so), (x1 + so, y1 + so)),
             ((x1 + so, y1 + so), (x0 - so, y1 + so)), ((x0 - so, y1 + so), (x0 - so, y0 - so))]
    keep = [(p["x"] - p["w"] / 2 - 0.2, p["y"] - p["h"] / 2 - 0.2, p["x"] + p["w"] / 2 + 0.2, p["y"] + p["h"] / 2 + 0.2)
            for p in pads]

    def clear(x, y):
        return not any(k[0] <= x <= k[2] and k[1] <= y <= k[3] for k in keep)
    for (ax, ay), (bx, by) in edges:
        steps = max(1, int(math.hypot(bx - ax, by - ay) / 0.05))
        run = None
        for i in range(steps + 1):
            t = i / steps
            x, y = ax + (bx - ax) * t, ay + (by - ay) * t
            if clear(x, y):
                run = run or (x, y)
                last = (x, y)
            elif run:
                if math.dist(run, last) > 0.2:
                    items.append(line(run, last, "F.SilkS", 0.12))
                run = None
        if run and math.dist(run, last) > 0.2:
            items.append(line(run, last, "F.SilkS", 0.12))
    # pin 1 işareti: pad 1'in dışında küçük bir nokta
    p1 = next(p for p in pads if p["number"] == "1")
    mx = p1["x"] - p1["w"] / 2 - 0.45 if p1["x"] <= cx else p1["x"]
    my = p1["y"] if p1["x"] <= cx else p1["y"] - p1["h"] / 2 - 0.45
    items.append([S("fp_circle"), [S("center"), _r(mx), _r(my)], [S("end"), _r(mx + 0.12), _r(my)],
                  stroke(0.12), [S("fill"), S("yes")], [S("layer"), "F.SilkS"]])
    for p in pads:
        th = p.get("type") == "thru_hole"
        shape = p.get("shape", "roundrect")
        pad = [S("pad"), p["number"], S("thru_hole" if th else "smd"), S(shape),
               [S("at"), _r(p["x"]), _r(p["y"])], [S("size"), _r(p["w"]), _r(p["h"])]]
        if th:
            pad += [[S("drill"), p["drill"]], [S("layers"), "*.Cu", "*.Mask"]]
        else:
            pad += [[S("layers"), "F.Cu", "F.Paste", "F.Mask"]]
        if shape == "roundrect":
            pad.append([S("roundrect_rratio"), 0.25])
        items.append(pad)
    return items


# ------------------------------------------------------------------ sembol

_DEFAULT_SIDE = {"input": "left", "output": "right", "power_out": "right"}


def symbol(name: str, pins: list[dict], reference: str, footprint_id: str, description: str) -> list:
    sides: dict[str, list[dict]] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        typ = p.get("type", "passive")
        side = p.get("side") or ("bottom" if typ == "power_in" and str(p["name"]).upper().startswith(("GND", "VSS", "AGND", "PGND", "EP"))
                                 else "top" if typ == "power_in" else _DEFAULT_SIDE.get(typ))
        if side is None:  # pasif / çift yönlü: kısa olan yana
            side = "left" if len(sides["left"]) <= len(sides["right"]) else "right"
        sides[side].append({**p, "type": typ})
    # Aynı adlı güç pinleri (iki GND, iki VCC) tek noktada üst üste: biri görünür, diğerleri gizli
    for side in sides:
        merged: list[dict] = []
        for p in sides[side]:
            twin = next((m for m in merged if m["type"].startswith("power") and m["type"] == p["type"]
                         and str(m["name"]) == str(p["name"])), None)
            if twin is not None:
                twin.setdefault("stack", []).append(p)
            else:
                merged.append(p)
        sides[side] = merged
    g = 2.54
    rows = max(len(sides["left"]), len(sides["right"]), 1)
    name_w = lambda ps: max([len(str(p["name"])) for p in ps] + [0]) * 0.75  # noqa: E731
    # üst/alt pinler 2 ızgara aralıklı (güç sembolleri yan yana sığsın); adları kutuya dikey girer
    half_w = max(5.08, math.ceil((name_w(sides["left"]) + name_w(sides["right"]) + 2.54) / 2 / g) * g,
                 math.ceil(max(len(sides["top"]), len(sides["bottom"])) * 2 * g / 2 / g) * g + g)
    vert = name_w(sides["top"]) + name_w(sides["bottom"]) + (1.27 if sides["top"] or sides["bottom"] else 0)
    half_h = math.ceil(((rows + 1) * g + vert) / 2 / g) * g
    font = [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]

    def prop(k, v, x, y, hide=False):
        eff = [S("effects"), [S("font"), [S("size"), 1.27, 1.27]], *([[S("hide"), S("yes")]] if hide else [])]
        return [S("property"), k, v, [S("at"), x, y, 0], eff]

    def pin(p, x, y, ang, hidden=False):
        return [S("pin"), S(p["type"]), S("line"), [S("at"), _r(x), _r(y), ang], [S("length"), g],
                *([[S("hide"), S("yes")]] if hidden else []),
                [S("name"), str(p["name"]), font], [S("number"), str(p["number"]), font]]

    body = [S("symbol"), f"{name}_0_1",
            [S("rectangle"), [S("start"), -half_w, half_h], [S("end"), half_w, -half_h],
             [S("stroke"), [S("width"), 0.254], [S("type"), S("default")]], [S("fill"), [S("type"), S("background")]]]]
    unit = [S("symbol"), f"{name}_1_1"]

    def spread(n, step=g):
        return [((n - 1) / 2 - i) * step for i in range(n)]

    def place(p, x, y, ang):
        unit.append(pin(p, x, y, ang))
        for q in p.get("stack", []):
            unit.append(pin(q, x, y, ang, hidden=True))
    # sol/sağ: üstten aşağı; dikey adlar kutuya girdiği için yan pinler üst adların altından başlar
    y_shift = -(name_w(sides["top"]) - name_w(sides["bottom"])) / 2
    for y, p in zip(spread(len(sides["left"])), sides["left"]):
        place(p, -half_w - g, round((y + y_shift) / 1.27) * 1.27, 0)
    for y, p in zip(spread(len(sides["right"])), sides["right"]):
        place(p, half_w + g, round((y + y_shift) / 1.27) * 1.27, 180)
    for x, p in zip(spread(len(sides["top"]), 2 * g), sides["top"]):
        place(p, -round(x / 1.27) * 1.27, half_h + g, 270)
    for x, p in zip(spread(len(sides["bottom"]), 2 * g), sides["bottom"]):
        place(p, -round(x / 1.27) * 1.27, -half_h - g, 90)
    return [S("symbol"), name, [S("pin_names"), [S("offset"), 1.016]], [S("exclude_from_sim"), S("no")],
            [S("in_bom"), S("yes")], [S("on_board"), S("yes")],
            prop("Reference", reference, -half_w, half_h + 1.27),
            prop("Value", name, half_w, half_h + 1.27),
            prop("Footprint", footprint_id, 0, 0, True), prop("Datasheet", "", 0, 0, True),
            prop("Description", description, 0, 0, True), body, unit]


# ------------------------------------------------------------------ yazma

def create(name: str, pins: list[dict] | None, package: dict | None, reference: str = "U",
           footprint_id: str = "", description: str = "") -> dict:
    if not pins and not package:
        raise ToolError("give pins (symbol), package (footprint) or both")
    if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ToolError("name: letters, digits, - _ . only")
    out = {}
    p = _paths()
    if package:
        p["fp"].mkdir(parents=True, exist_ok=True)
        fp = footprint(name, package, description)
        dst = p["fp"] / f"{name}.kicad_mod"
        dst.write_text(sexpr.dump(fp) + "\n", encoding="utf-8", newline="\n")
        with tempfile.TemporaryDirectory() as t:  # biçimi KiCad'e doğrulat/güncelle
            tmp = Path(t) / f"{LIB}.pretty"
            tmp.mkdir()
            (tmp / dst.name).write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
            cli("fp", "upgrade", "--force", str(tmp))
            dst.write_text((tmp / dst.name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        footprint_id = f"{LIB}:{name}"
        pads = [x for x in pads_for(package)[0]]
        out.update({"footprint": footprint_id, "pads": len(pads)})
    if pins:
        numbers = [str(q["number"]) for q in pins]
        if len(set(numbers)) != len(numbers):
            raise ToolError("duplicate pin numbers")
        if package:
            pad_nums = {x["number"] for x in pads_for(package)[0]}
            if missing := sorted(set(numbers) - pad_nums, key=library._natural):
                raise ToolError(f"pins without a pad: {missing}; pads are {sorted(pad_nums, key=library._natural)}")
            if unused := sorted(pad_nums - set(numbers), key=library._natural):
                out["unused_pads"] = unused
        p["sym"].parent.mkdir(parents=True, exist_ok=True)
        lib = (sexpr.parse(p["sym"].read_text(encoding="utf-8")) if p["sym"].is_file() else
               [S("kicad_symbol_lib"), [S("version"), 20241209], [S("generator"), "tamga-pcb"],
                [S("generator_version"), "1"]])
        lib = [c for c in lib if not (isinstance(c, list) and c and c[0] == S("symbol") and c[1] == name)]
        lib.append(symbol(name, pins, reference, footprint_id, description))
        p["sym"].write_text(sexpr.dump(lib) + "\n", encoding="utf-8", newline="\n")
        cli("sym", "upgrade", "--force", str(p["sym"]))
        out.update({"symbol": f"{LIB}:{name}", "pins": len(pins)})
    library._lib_tree.cache_clear()
    out["use"] = (f"symbol '{out.get('symbol', '?')}' footprint '{footprint_id or '?'}' in the spec; "
                  "check with get_symbol(lib_id, footprint) and render_board after placing")
    return out
