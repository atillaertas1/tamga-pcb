"""Devre tanımından (spec) KiCad şematiği üret.

Bağlantı stratejisi: her bağlı pinin ucuna kısa bir tel + global label. Net adı = label adı,
böylece şematik, netlist ve PCB'de aynı ad kullanılır ('/VIN' değil 'VIN').
UUID'ler deterministik (uuid5), yeniden üretimde PCB bağları ve git diff'leri bozulmaz.
"""
from __future__ import annotations

import math
import re
import unicodedata
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


_POWER_RE = re.compile(r"^(GND|\+|V(IN|CC|DD|ISO|BAT|BUS))")
_POWER_LIB = {"+5V", "+3V3", "+1V8", "+12V", "+24V", "+3V0", "+2V5", "+1V2", "+1V0", "+15V"}


def power_net_set(spec: dict, nets: set[str]) -> set[str]:
    """Etiket yerine güç sembolüyle çizilecek netler. spec['power_nets'] verilirse o, yoksa ad deseni
    (GND*, +*, VIN*, VCC*, VDD*, VISO*). Net adı sembolün Value'sundan gelir: ad değişmez."""
    if spec.get("power_nets") is not None:
        return set(spec["power_nets"]) & nets
    return {n for n in nets if _POWER_RE.match(n)}


def _power_lib_id(net: str) -> str:
    if net.startswith("GND"):
        return "power:GND"
    return f"power:{net}" if net in _POWER_LIB else "power:VCC"


def _rot_xy(px: float, py: float, angle: float) -> tuple[float, float]:
    """Sembol koordinatını (Y yukarı) CCW döndür."""
    a = math.radians(angle)
    return px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)


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


# --------------------------------------------------------------------------- yerleşim (okunur şematik)
# Fikir: ana parça (IC, modül, konnektör) ortada; bir pine bağlı küçük parçalar (R, C, LED, buton) o pinin
# hizasında bir "ada"da telle bağlı: pinden çıkan yatay tel, parçalar telden dikey sarkar (besleme yukarı,
# toprak aşağı). İki besleme arasındaki seri zincirler (3V3-R-LED-GND) dikey sütun. Etiket yalnız net başka
# yere de gidiyorsa. Bağlantı doğruluğunu sync'teki netlist karşılaştırması ve ERC denetler.

PASSIVE_RE = re.compile(r"^(R|C|L|D|LED|SW|FB|F|RV|TH|BZ|Y)\d", re.I)
GAP = 2.54


def _pin_pos(p: dict, sx: float, sy: float, angle: float) -> tuple[float, float]:
    rx, ry = _rot_xy(p["x"], p["y"], angle)
    return round(sx + rx, 4), round(sy - ry, 4)


def _outward(p: dict, angle: float = 0.0) -> tuple[int, int]:
    a = math.radians(p["angle"] + angle)
    return -round(math.cos(a)), round(math.sin(a))


def _label_len(net: str) -> float:
    return 2 + len(net) * 1.1


def _ceil_grid(v: float) -> float:
    return math.ceil(v / GRID - 1e-6) * GRID


class _Cluster:
    """Bir grup çizim işlemi, yerel koordinatta; sonra sayfaya kaydırılır."""

    def __init__(self, key: str):
        self.key = key
        self.ops: list[tuple] = []
        self.box = [math.inf, math.inf, -math.inf, -math.inf]

    def grow(self, x0, y0, x1, y1):
        b = self.box
        b[0], b[1] = min(b[0], x0, x1), min(b[1], y0, y1)
        b[2], b[3] = max(b[2], x0, x1), max(b[3], y0, y1)

    def wire(self, a, b):
        if abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6:
            self.ops.append(("wire", a, b))
            self.grow(a[0], a[1], b[0], b[1])

    def junction(self, a):
        self.ops.append(("junction", a))

    def term(self, net: str, a, d, power: bool):
        """Güç sembolü ya da global etiket; d = dışa doğru yön."""
        self.ops.append(("power" if power else "label", net, a, d))
        length = 5.08 if power else _label_len(net)
        half = (len(net) * 0.55 + 1) if power else 1.0
        ex, ey = a[0] + d[0] * length, a[1] + d[1] * length
        if d[0]:
            self.grow(a[0], a[1] - half, ex, a[1] + half)
        else:
            self.grow(a[0] - half, a[1], a[0] + half, ey)

    def nc(self, a):
        self.ops.append(("nc", a))

    def symbol(self, entry, unit: int, sx: float, sy: float, angle: float, fields: list | None):
        self.ops.append(("sym", entry, unit, sx, sy, angle, fields))
        pts = [_rot_xy(x, y, angle) for x, y in library.body_points(entry[1], unit)]
        self.grow(sx + min(p[0] for p in pts), sy - max(p[1] for p in pts),
                  sx + max(p[0] for p in pts), sy - min(p[1] for p in pts))
        for _name, val, fx, fy, just in fields or []:
            w = len(str(val)) * 1.05
            self.grow(fx - (w if just == "right" else 0), fy - 1.0, fx + (w if just == "left" else 0), fy + 1.0)


def _vertical_angle(near: dict, far: dict, v: int) -> float:
    """near pin üstte (v=+1, aşağı sarkar) ya da altta (v=-1) kalacak dönüş açısı."""
    for ang in (0.0, 90.0, 180.0, 270.0):
        nx, ny = _rot_xy(near["x"], near["y"], ang)
        fx, fy = _rot_xy(far["x"], far["y"], ang)
        if abs(nx - fx) < 1e-6 and (ny - fy) * v > 0:  # sembol Y yukarı: aşağı sarkmak = far daha aşağıda
            return ang
    return 0.0


def _half_width(entry, angle: float) -> float:
    pts = [_rot_xy(x, y, angle) for x, y in library.body_points(entry[1], 1)]
    return max(abs(p[0]) for p in pts)


def _chain_len(chain, term_net: str, power: bool) -> float:
    span = sum(GAP + math.dist((n["x"], n["y"]), (f["x"], f["y"])) for _e, n, f, _fn in chain)
    return span + GAP * len(chain) + (6.0 if power else _label_len(term_net))


def _chain_width(chain, term_net: str, power: bool, v: int) -> tuple[float, float]:
    """(iç yarı genişlik, dış genişlik: gövde + ref/değer yazısı)."""
    half, text = 1.27, 0.0
    for entry, near, far, _fnet in chain:
        ang = _vertical_angle(near, far, v)
        half = max(half, _half_width(entry, ang))
        part = entry[0]
        val = str(part.get("value") or part["symbol"].split(":")[1])
        text = max(text, len(part["ref"]) * 1.05, len(val) * 1.05)
    if power:
        half = max(half, len(term_net) * 0.55 + 1)
    return half, half + 1.27 + text


def _draw_chain(cl: _Cluster, chain, start, v: int, term_net: str, power: bool, side: int):
    """start noktasından v yönüne (+1 aşağı) parçaları dikey diz; sonda güç sembolü/etiket.
    side: ref/değer yazısının tarafı (+1 sağ, -1 sol). Parçalar arası net, telin üstünde karşı tarafa
    bakan etiketle adlanır (net adı spec'teki gibi kalsın; KiCad tellere 'Net-(R1-2)' adı verir)."""
    x, y = start
    pending = None
    for entry, near, far, fnet in chain:
        if pending:
            mid = (x, round(y + v * GAP, 4))
            cl.wire((x, y), mid)
            cl.term(pending, mid, (-side, 0), False)
            x, y = mid
        pending = fnet
        ang = _vertical_angle(near, far, v)
        nrx, nry = _rot_xy(near["x"], near["y"], ang)
        ny = round(y + v * GAP, 4)
        sx, sy = round(x - nrx, 4), round(ny + nry, 4)
        cl.wire((x, y), (x, ny))
        fx, fy = _pin_pos(far, sx, sy, ang)
        part = entry[0]
        val = str(part.get("value") or part["symbol"].split(":")[1])
        tx = round(x + side * (_half_width(entry, ang) + 1.27), 4)
        just = "left" if side > 0 else "right"
        cy = (ny + fy) / 2
        cl.symbol(entry, 1, sx, sy, ang, [("Reference", part["ref"], tx, round(cy - 1.27, 4), just),
                                          ("Value", val, tx, round(cy + 1.27, 4), just)])
        x, y = fx, fy
    end = (x, round(y + v * GAP, 4))
    cl.wire((x, y), end)
    cl.term(term_net, end, (0, v), power)


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
    power_nets = power_net_set(spec, set(net_types))
    for n in power_nets:  # güç sembolünün pini power_in: sürücüsü yoksa PWR_FLAG gerekir
        net_types[n].add("power_in")
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
        groups.setdefault(entry[0].get("instance") or entry[0].get("sheet"), []).append(entry)
    sheets = [g for g in groups if g is not None]

    pwr_count = [0]
    notes_of = spec.get("sheet_notes") or {}
    page_nets = {g: {n for (_p, _s, _pins, pm) in groups[g] for n in pm.values()} for g in groups}

    def page(entries, inst_path, sheet_path, sheetname, sheetfile, extra_area=0.0, notes="", group=None):
        """Bir sayfayı yerleştir; (items, kullanılan lib_id'ler, kağıt, alt kenar) döndür."""
        items: list = []
        used: dict[str, list] = {}
        external = set().union(*(page_nets[g] for g in groups if g != group)) if len(groups) > 1 else set()

        def pkey(p):
            return (p["number"], p["unit"], p["x"], p["y"])

        # --- sınıflandır: 2 pinli pasifler (R, C, LED, buton...) ve ana parçalar (birim birim)
        anchors: list[tuple] = []
        passives: dict[str, tuple] = {}
        for entry in entries:
            part, sym, pins, pmap = entry
            used[part["symbol"]] = sym
            n_units = library.unit_count(sym)
            conn = [(p, pmap.get(pkey(p))) for p in pins]
            if (not part.get("_virtual") and n_units == 1 and len(pins) == 2 and PASSIVE_RE.match(part["ref"])
                    and all(n for _p, n in conn) and conn[0][1] != conn[1][1]):
                passives[part["ref"]] = (entry, conn)
            else:
                anchors += [(entry, u) for u in range(1, n_units + 1)]
        members: dict[str, list] = {}
        for ai, (entry, unit) in enumerate(anchors):
            seen = set()
            for p in entry[2]:
                net = entry[3].get(pkey(p))
                if p["unit"] in (0, unit) and net and (p["x"], p["y"], net) not in seen:
                    seen.add((p["x"], p["y"], net))
                    members.setdefault(net, []).append(("A", ai, (p["x"], p["y"])))
        for ref, (_entry, conn) in passives.items():
            for _p, net in conn:
                members.setdefault(net, []).append(("P", ref))
        assigned: set[str] = set()

        def other(ref, net):
            (p0, n0), (p1, n1) = passives[ref][1]
            return (p0, p1, n1) if n0 == net else (p1, p0, n0)

        def next_passive(ref, net):
            """net yalnız ref ile tek bir boştaki pasifi bağlıyorsa o pasif (zincir devam eder)."""
            if net in power_nets or net in external:
                return None
            rest = [m for m in members.get(net, []) if m != ("P", ref)]
            if len(rest) == 1 and rest[0][0] == "P" and rest[0][1] not in assigned:
                return rest[0][1]
            return None

        def walk(ref, net):
            chain = []
            while True:
                near, far, fnet = other(ref, net)
                chain.append((passives[ref][0], near, far, fnet))
                assigned.add(ref)
                nxt = next_passive(ref, fnet)
                if nxt is None:
                    return chain, fnet
                ref, net = nxt, fnet

        def direction(tnet, power):
            return -1 if power and not tnet.startswith("GND") else 1

        # --- ana parça kümesi: sembol + pin adaları + kalan pinlerde etiket/güç sembolü
        def anchor_cluster(ai, entry, unit):
            part, sym, pins, pmap = entry
            virtual = part.get("_virtual", False)
            cl = _Cluster(f"{part['ref']}:{unit}")
            cl.symbol(entry, unit, 0.0, 0.0, 0.0, None)
            unit_pins = [p for p in pins if p["unit"] in (0, unit)]
            islands = []
            if not virtual:
                done = set()
                for p in sorted(unit_pins, key=lambda q: -q["y"]):
                    net = pmap.get(pkey(p))
                    d = _outward(p)
                    if not net or net in power_nets or d[1] != 0 or net in done:
                        continue
                    sats = [m[1] for m in members.get(net, []) if m[0] == "P" and m[1] not in assigned]
                    if not sats:
                        continue
                    done.add(net)
                    chains = []
                    for ref in sats:
                        if ref in assigned:
                            continue
                        ch, tnet = walk(ref, net)
                        pw = tnet in power_nets
                        chains.append((ch, tnet, pw, direction(tnet, pw)))
                    first = {c[0][0][0][0]["ref"] for c in chains}  # zincir -> ilk (entry, near, far) -> part
                    rest = [m for m in members[net] if not (m[0] == "P" and m[1] in first)
                            and m != ("A", ai, (p["x"], p["y"]))]
                    islands.append({"pin": p, "net": net, "side": d[0], "chains": chains,
                                    "label": bool(rest) or net in external, "aligned": False})
            for side in (-1, 1):
                side_isl = [i for i in islands if i["side"] == side]
                if not side_isl:
                    continue
                side_pins = [p for p in unit_pins if _outward(p) == (side, 0)]
                lz = max([STUB + (6.5 if n in power_nets else _label_len(n))
                          for p in side_pins if (n := pmap.get(pkey(p)))] + [STUB])
                edge = (min if side < 0 else max)(p["x"] for p in side_pins)
                base = round(edge + side * _ceil_grid(lz + 5.08), 4)
                pins_bottom = max(-p["y"] for p in side_pins)

                def geom(isl, xc, bus_y, aligned):
                    """Adanın asma noktaları, etiket ucu ve kapladığı dikdörtgenler."""
                    hxs, x, prev_out = [], xc, 0.0
                    for c, t, w, v in isl["chains"]:
                        half, out = _chain_width(c, t, w, v)
                        if hxs:
                            x = round(x + side * _ceil_grid(prev_out + GAP + half), 4)
                        hxs.append(x)
                        prev_out = out
                    lx = round(hxs[-1] + side * _ceil_grid(prev_out + GAP), 4)
                    start = isl["pin"]["x"] if aligned else hxs[0]
                    far = lx + side * _label_len(isl["net"])
                    rects = [(min(start, lx), bus_y - 0.3, max(start, lx), bus_y + 0.3),  # tel
                             (min(lx, far), bus_y - 1.2, max(lx, far), bus_y + 1.2)]      # etiket
                    for (c, t, w, v), hx in zip(isl["chains"], hxs):
                        half, out = _chain_width(c, t, w, v)
                        x0, x1 = (hx - half, hx + out) if side > 0 else (hx - out, hx + half)
                        y1 = bus_y + v * _chain_len(c, t, w)
                        rects.append((x0, min(bus_y, y1), x1, max(bus_y, y1)))
                    return hxs, lx, rects

                def hit(rects, occ):
                    m = 0.8
                    return any(a[0] < b[2] + m and b[0] < a[2] + m and a[1] < b[3] + m and b[1] < a[3] + m
                               for a in rects for b in occ)

                def arrange(order):
                    """Her adayı pin hizasında, IC'ye en yakın çakışmasız yere koy; olmazsa alta diz."""
                    occ, plan = [], {}
                    for isl in order:
                        py = -isl["pin"]["y"]
                        for k in range(24):
                            xc = round(base + side * k * 2 * GRID, 4)
                            hxs, lx, rects = geom(isl, xc, py, True)
                            if not hit(rects, occ):
                                plan[id(isl)] = (py, True, hxs, lx)
                                occ += rects
                                break
                    bottom = max([r[3] for r in occ] + [pins_bottom])
                    for isl in order:
                        if id(isl) in plan:
                            continue
                        up = max([_chain_len(c, t, w) for c, t, w, v in isl["chains"] if v < 0] + [0.0])
                        down = max([_chain_len(c, t, w) for c, t, w, v in isl["chains"] if v > 0] + [0.0])
                        bus_y = round(_ceil_grid(bottom + 2 * GAP + up), 4)
                        hxs, lx, rects = geom(isl, base, bus_y, False)
                        plan[id(isl)] = (bus_y, False, hxs, lx)
                        occ += rects
                        bottom = bus_y + down
                    width = max(abs(r[0] - base) + abs(r[2] - base) for r in occ)
                    return sum(a for _y, a, _h, _l in plan.values()), -width, plan

                by_y = sorted(side_isl, key=lambda i: -i["pin"]["y"])
                *_, plan = max(arrange(by_y), arrange(by_y[::-1]), key=lambda r: (r[0], r[1]))
                for isl in side_isl:
                    bus_y, aligned, hxs, lx = plan[id(isl)]
                    isl["aligned"] = aligned
                    px, py = isl["pin"]["x"], -isl["pin"]["y"]
                    pts = ([(px, py)] if aligned else []) + [(hx, bus_y) for hx in hxs] + [(lx, bus_y)]
                    for a, b in zip(pts, pts[1:]):
                        cl.wire(a, b)
                    for i, hx in enumerate(hxs):
                        if (i > 0 or aligned):  # bus hem içe hem dışa devam ediyor + zincir = 3 tel
                            cl.junction((hx, bus_y))
                    # net adı her zaman bir global etiketten gelir (bkz. _draw_chain)
                    cl.term(isl["net"], (lx, bus_y), (side, 0), False)
                    for (c, t, w, v), hx in zip(isl["chains"], hxs):
                        _draw_chain(cl, c, (hx, bus_y), v, t, w, side)
            wired = {(i["pin"]["x"], i["pin"]["y"], i["net"]) for i in islands if i["aligned"]}
            nc = part.get("nc") or []
            for p in unit_pins:
                net = pmap.get(pkey(p))
                px, py = p["x"], -p["y"]
                if net is None:
                    if (nc == "*" or p["number"] in nc or p["name"] in nc) and p["type"] != "no_connect":
                        cl.nc((px, py))
                    continue
                if (p["x"], p["y"], net) in wired:  # aynı noktadaki gizli pinler (USB-C VBUS/GND) bir kez
                    continue
                wired.add((p["x"], p["y"], net))
                d = _outward(p)
                # Yatay güç pini: dik duran sembol okunur, ama komşu pinler (konnektörde 2.54 adım) yakınsa
                # etiketlerinin üstüne biner; o zaman aynı adlı global etiket (güç netine bağlanır).
                crowded = any(_outward(q) == d and abs(q["y"] - p["y"]) < 7.6 and (q["x"], q["y"]) != (p["x"], p["y"])
                              and pmap.get(pkey(q)) for q in unit_pins)
                upright = d[1] == 0 and net in power_nets and not virtual and not crowded
                stub = 3 * STUB if upright else STUB
                e = (round(px + d[0] * stub, 4), round(py + d[1] * stub, 4))
                cl.wire((px, py), e)
                if d[1] == 0 and net in power_nets and not virtual and crowded:
                    cl.term(net, e, d, False)
                    continue
                if upright:
                    # yan yatmış güç sembolünün yazısı dikey okunur: yatay telin ucunda dik dursun
                    cl.term(net, e, (0, 1 if net.startswith("GND") else -1), True)
                    continue
                # PWR_FLAG'in ucu etiket kalır: yalnız güç sembollerinden oluşan alt-ağı KiCad ERC
                # "pin not connected" sayıyor. Aynı adlı global etiket güç netine bağlanır.
                cl.term(net, e, d, net in power_nets and not virtual)
            return cl

        def column_cluster(ref):
            """Boşta kalan pasiften iki yöne zincir: üstte besleme/etiket, altta toprak."""
            (_p0, n0), (_p1, n1) = passives[ref][1]

            def extend(start, net):
                seq, cur, cnet = [], start, net
                while (nxt := next_passive(cur, cnet)) is not None and nxt not in seq and nxt != ref:
                    seq.append(nxt)
                    cur, cnet = nxt, other(nxt, cnet)[2]
                return seq, cnet

            left, top = extend(ref, n0)
            right, bottom = extend(ref, n1)
            order = list(reversed(left)) + [ref] + right

            def rank(n):
                return 0 if n in power_nets and not n.startswith("GND") else 2 if n.startswith("GND") else 1
            if rank(top) > rank(bottom):
                order, top, bottom = list(reversed(order)), bottom, top
            chain, net = [], top
            for r in order:
                near, far, fnet = other(r, net)
                chain.append((passives[r][0], near, far, fnet))
                assigned.add(r)
                net = fnet
            cl = _Cluster(f"col:{order[0]}")
            cl.term(top, (0.0, 0.0), (0, -1), top in power_nets)
            _draw_chain(cl, chain, (0.0, 0.0), 1, bottom, bottom in power_nets, 1)
            return cl

        clusters = [anchor_cluster(ai, e, u) for ai, (e, u) in enumerate(anchors)]
        for entry in entries:
            ref = entry[0]["ref"]
            if ref in passives and ref not in assigned:
                clusters.append(column_cluster(ref))

        # --- kümeleri sayfaya diz (satır satır), kağıt boyunu seç
        note_lines = notes.splitlines() if notes else []
        note_h = (len(note_lines) * 2.1 + 5.08) if note_lines else 0.0
        start_y = snap(30.48 + note_h)
        sizes = [(c.box[2] - c.box[0], c.box[3] - c.box[1]) for c in clusters]
        total_area = sum((w + 7.62) * (h + 7.62) for w, h in sizes) + extra_area + note_h * 200
        spacing = 10.16

        def fits(pp):  # kuru yerleşim: alt kenar başlık bloğuna/çerçeveye taşmasın
            cx, cy, rh = 25.4, start_y, 0.0
            for w_, h_ in sizes:
                if cx + w_ > pp[1] - 20 and cx > 25.4:
                    cx, cy, rh = 25.4, cy + rh + spacing, 0.0
                cx += w_ + spacing
                rh = max(rh, h_)
            return cy + rh <= pp[2] - 45

        first = next((i for i, pp in enumerate(PAPERS) if total_area * 1.6 < pp[1] * pp[2]), len(PAPERS) - 1)
        paper = next((pp for pp in PAPERS[first:] if fits(pp)), PAPERS[-1])
        if note_lines:  # tasarım notları (hesaplar, gerekçeler) sayfanın başına
            items.append([Sym("text"), "\n".join(note_lines), [Sym("exclude_from_sim"), Sym("no")],
                          [Sym("at"), 25.4, 25.4, 0], _font(False, "left top"),
                          [Sym("uuid"), uid(project, "notes", sheetname)]])
        cur_x, cur_y, row_h = 25.4, start_y, 0.0
        for cl, (w, h) in zip(clusters, sizes):
            if cur_x + w > paper[1] - 20 and cur_x > 25.4:
                cur_x, cur_y, row_h = 25.4, cur_y + row_h + spacing, 0.0
            ox, oy = snap(cur_x - cl.box[0]), snap(cur_y - cl.box[1])
            cur_x += w + spacing
            row_h = max(row_h, h)
            emit(cl, ox, oy, items, used, inst_path, sheet_path, sheetname, sheetfile)
        return items, used, paper, cur_y + row_h

    def emit(cl, ox, oy, items, used, inst_path, sheet_path, sheetname, sheetfile):
        """Kümenin işlemlerini sayfa koordinatında KiCad öğelerine çevir."""
        for i, op in enumerate(cl.ops):
            kind = op[0]
            okey = (sheetname, cl.key, i)
            if kind == "wire":
                (x1, y1), (x2, y2) = op[1], op[2]
                items.append([Sym("wire"), [Sym("pts"), [Sym("xy"), round(x1 + ox, 4), round(y1 + oy, 4)],
                                            [Sym("xy"), round(x2 + ox, 4), round(y2 + oy, 4)]],
                              [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("default")]],
                              [Sym("uuid"), uid(project, *okey, "wire")]])
            elif kind == "junction":
                x, y = op[1]
                items.append([Sym("junction"), [Sym("at"), round(x + ox, 4), round(y + oy, 4)],
                              [Sym("diameter"), 0], [Sym("color"), 0, 0, 0, 0],
                              [Sym("uuid"), uid(project, *okey, "junction")]])
            elif kind == "nc":
                x, y = op[1]
                items.append([Sym("no_connect"), [Sym("at"), round(x + ox, 4), round(y + oy, 4)],
                              [Sym("uuid"), uid(project, *okey, "nc")]])
            elif kind == "label":
                net, (x, y), (dx, dy) = op[1], op[2], op[3]
                ex, ey = round(x + ox, 4), round(y + oy, 4)
                angle = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[(dx, dy)]
                just = "left" if angle in (0, 90) else "right"
                items.append([Sym("global_label"), net, [Sym("shape"), Sym("passive")],
                              [Sym("at"), ex, ey, angle], [Sym("fields_autoplaced"), Sym("yes")],
                              _font(False, just), [Sym("uuid"), uid(project, *okey, "label")],
                              [Sym("property"), "Intersheetrefs", "${INTERSHEET_REFS}",
                               [Sym("at"), ex, ey, 0], _font(True)]])
            elif kind == "power":
                net, (x, y), (dx, dy) = op[1], op[2], op[3]
                ex, ey = round(x + ox, 4), round(y + oy, 4)
                lib_id = _power_lib_id(net)
                psym = library.resolve_symbol(lib_id)
                used[lib_id] = psym
                ground = lib_id == "power:GND"
                # sembol gövdesi dışarı baksın (toprak varsayılan aşağı, besleme yukarı; CCW)
                rot = ({(0, 1): 0, (1, 0): 90, (0, -1): 180, (-1, 0): 270} if ground else
                       {(0, -1): 0, (-1, 0): 90, (0, 1): 180, (1, 0): 270})[(dx, dy)]
                pwr_count[0] += 1
                pref = f"#PWR{pwr_count[0]:03d}"
                vprop = next((q for q in children(psym, "property") if q[1] == "Value"), None)
                vat = child(vprop, "at") if vprop else None
                vx, vy = _rot_xy(float(vat[1]), float(vat[2]), rot) if vat else (0.0, 0.0)
                pu = uid(project, *okey, "pwr")
                items.append([Sym("symbol"), [Sym("lib_id"), lib_id], [Sym("at"), ex, ey, rot], [Sym("unit"), 1],
                              [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("no")],
                              [Sym("on_board"), Sym("no")], [Sym("dnp"), Sym("no")], [Sym("uuid"), pu],
                              _prop("Reference", pref, ex, ey, True),
                              _prop("Value", net, snap(ex + vx, 0.01), snap(ey - vy, 0.01), False),
                              _prop("Footprint", "", ex, ey, True), _prop("Datasheet", "", ex, ey, True),
                              [Sym("pin"), "1", [Sym("uuid"), uid(project, *okey, "pwr", "pin")]],
                              [Sym("instances"), [Sym("project"), project,
                               [Sym("path"), inst_path, [Sym("reference"), pref], [Sym("unit"), 1]]]]])
            elif kind == "sym":
                entry, unit, sx, sy, angle, fields = op[1:]
                part, sym, pins, pmap = entry
                sx, sy = round(sx + ox, 4), round(sy + oy, 4)
                u = uid(project, part["ref"], unit)
                unit_pins = [p for p in pins if p["unit"] in (0, unit)]
                placed.append(Placed(part["ref"], part["symbol"], unit, sx, sy, u, unit_pins))
                props = {p[1]: p for p in children(sym, "property")}

                def ppos(name):
                    at = child(props.get(name, []), "at") if name in props else None
                    if at:
                        eff = child(props[name], "effects")
                        js = child(eff, "justify") if eff else None
                        just = " ".join(str(j) for j in js[1:]) if js else None
                        return (snap(sx + float(at[1]), 0.01), snap(sy - float(at[2]), 0.01),
                                float(at[3]) if len(at) > 3 else 0, just)
                    return sx, sy, 0, None
                # KiCad alan yazısını sembolle birlikte döndürür: 90/270'te yatay okunsun diye açıyı eşitle
                # Görünen açı = alan + sembol açısı; 180 çıktığında KiCad yazıyı düz çevirip hizalamayı tersler.
                # 90/270'te alan açısını eşitle (toplam 180), 180'de alan 0 (toplam 180): her ikisinde de ters hizala.
                fang = angle if angle in (90.0, 270.0) else 0
                flip = {"left": "right", "right": "left"}
                fpos = {f[0]: (round(f[2] + ox, 4), round(f[3] + oy, 4), fang, flip[f[4]] if angle else f[4])
                        for f in fields or []}
                ref_x, ref_y, ref_a, ref_j = fpos.get("Reference") or ppos("Reference")
                val_x, val_y, val_a, val_j = fpos.get("Value") or ppos("Value")
                virtual = part.get("_virtual", False)
                # BOM: footprint 'exclude_from_bom' ise sembol de hariç (montaj deliği, lehim köprüsü);
                # yoksa KiCad parity 'footprint_symbol_mismatch' verir. Spec'te "in_bom": false da olur.
                fp_attrs = library.footprint_attrs(part["footprint"]) if part.get("footprint") else set()
                in_bom = not virtual and part.get("in_bom", True) and "exclude_from_bom" not in fp_attrs
                value = str(part.get("value") or part["symbol"].split(":")[1])
                inst = [Sym("symbol"), [Sym("lib_id"), part["symbol"]], [Sym("at"), sx, sy, angle],
                        [Sym("unit"), unit], [Sym("exclude_from_sim"), Sym("no")],
                        [Sym("in_bom"), Sym("yes" if in_bom else "no")],
                        [Sym("on_board"), Sym("no" if virtual else "yes")],
                        [Sym("dnp"), Sym("yes" if part.get("dnp") else "no")],
                        [Sym("uuid"), u],
                        _prop("Reference", part["ref"], ref_x, ref_y, virtual, ref_a, ref_j),
                        _prop("Value", value, val_x, val_y, False, val_a, val_j),
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
                if not virtual and part["ref"] not in part_info:
                    part_info[part["ref"]] = {
                        "ref": part["ref"], "value": value,
                        "footprint": part.get("footprint", ""), "lcsc": part.get("lcsc", ""),
                        "sym_uuid": uid(project, part["ref"], 1),
                        "pads": {num: net for (num, _u, _x, _y), net in pmap.items()},
                        "path": f"{sheet_path}/{uid(project, part['ref'], 1)}",
                        "sheetname": sheetname, "sheetfile": sheetfile,
                        "dnp": bool(part.get("dnp")), "in_bom": bool(in_bom),
                    }

    # sayfa dosyaları: <proje>-<instance>.kicad_sch
    pages: dict[str, str] = {}
    sheet_boxes = []
    for i, name in enumerate(sheets, 2):
        suid = uid(project, "sheet", name)
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # İzoleli -> Izoleli
        fname = f"{project}-{re.sub(r'[^A-Za-z0-9_.-]', '_', ascii_name)}.kicad_sch"
        items, used, paper, _ = page(groups[name], f"/{root}/{suid}", f"/{suid}", f"/{name}/", fname,
                                     notes=notes_of.get(name, ""), group=name)
        sub = [Sym("kicad_sch"), [Sym("version"), 20250114], [Sym("generator"), "tamga-pcb"],
               [Sym("generator_version"), "0.2"], [Sym("uuid"), uid(project, "page", name)],
               [Sym("paper"), paper[0]], [Sym("title_block"), [Sym("title"), name]],
               [Sym("lib_symbols"), *used.values()], *items, [Sym("embedded_fonts"), Sym("no")]]
        pages[fname] = dump(sub) + "\n"
        sheet_boxes.append((name, suid, fname, i))

    # kök sayfa: kalan parçalar + her instance için sheet kutusu (portlar kutu içinde yazı)
    # blocks.expand her instance'ın port eşlemesini "sheets"e yazar: ["TX = UART5_TX", ...]
    nets_on = {g: {n for (_p, _s, _pins, pm) in groups[g] for n in pm.values()} for g in groups}
    ports_of = {}
    for name in sheets:
        if name in spec.get("sheets", {}):  # blok instance'ı: port = net eşlemesi
            ports_of[name] = spec["sheets"][name].get("ports", [])
            continue
        others = set().union(*(nets_on[g] for g in groups if g != name))
        shared = sorted(nets_on[name] & others)
        sig = [n for n in shared if n not in power_nets]
        pwr = [n for n in shared if n in power_nets]
        ports_of[name] = sig + ([f"[güç] {' '.join(pwr)}"] if pwr else [])
    box_area = sum(40 * (10 + 2.54 * len(ports_of[n])) for n in sheets)
    items, used, paper, bottom = page(groups[None], f"/{root}", "", "/", f"{project}.kicad_sch", box_area,
                                      group=None)
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

    doc = [Sym("kicad_sch"), [Sym("version"), 20250114], [Sym("generator"), "tamga-pcb"],
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
