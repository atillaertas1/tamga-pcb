"""pcbnew worker: KiCad'in SWIG bağlamalarıyla çalışan, tek komutluk alt süreç.

MCP sunucusu bunu sistem Python'u (/usr/bin/python3) ile alt süreç olarak çalıştırır:
    python3 worker.py <komut> '<json argümanlar>'
ve stdout'tan tek bir JSON satırı okur.

Neden alt süreç?
- pcbnew modülü sistem Python'una bağlı (venv'de yok).
- pcbnew bazen segfault atar; ayrı süreçte olursa MCP sunucusu ölmez.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time

import pcbnew

MM = 1_000_000  # KiCad iç birimi: nanometre


def mm(v: int) -> float:
    return round(v / MM, 3)


def load(path: str) -> "pcbnew.BOARD":
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return pcbnew.LoadBoard(path)


def backup(path: str) -> str:
    """Yazmadan önce .tamga_backups/ altına zaman damgalı kopya al."""
    d = os.path.join(os.path.dirname(os.path.abspath(path)), ".tamga_backups")
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(d, f"{stamp}-{os.path.basename(path)}")
    i = 1
    while os.path.exists(dst):  # aynı saniyedeki yazmalar birbirini ezmesin
        dst = os.path.join(d, f"{stamp}.{i}-{os.path.basename(path)}")
        i += 1
    shutil.copy2(path, dst)
    return dst


def unrouted(board) -> int:
    board.BuildConnectivity()
    return int(board.GetConnectivity().GetUnconnectedCount(False))


def side(fp) -> str:
    return "B" if fp.IsFlipped() else "F"


def fp_line(fp) -> str:
    """Bir footprint'i tek satırda, token dostu biçimde anlat."""
    p = fp.GetPosition()
    lib = fp.GetFPID().GetLibItemName()
    return (f"{fp.GetReference()} {fp.GetValue()} {lib} "
            f"@({mm(p.x)},{mm(p.y)}) r{fp.GetOrientationDegrees():g} {side(fp)}")


# ---------------------------------------------------------------- komutlar

def _net_lengths(b, patterns: list[str]) -> dict:
    """Desene uyan netlerin iz uzunluğu (mm) ve via sayısı; _P/_N, +/-, DP/DN çiftlerinde skew."""
    import fnmatch
    names = sorted({t.GetNetname() for t in b.GetTracks() if t.GetNetname()})
    want = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns)]
    out = {}
    for n in want:
        length = sum(mm(t.GetLength()) for t in b.GetTracks() if t.GetNetname() == n and t.GetClass() == "PCB_TRACK")
        vias = sum(1 for t in b.GetTracks() if t.GetNetname() == n and t.GetClass() == "PCB_VIA")
        out[n] = f"{length:.2f} mm, {vias} vias"
    pairs = {}
    for n in want:
        for p_sfx, n_sfx in (("_P", "_N"), ("+", "-"), ("DP", "DN"), ("D+", "D-")):
            if n.endswith(p_sfx) and (m := n[: -len(p_sfx)] + n_sfx) in want:
                lp = float(out[n].split()[0]); ln = float(out[m].split()[0])
                pairs[f"{n}/{m}"] = f"skew {abs(lp - ln):.2f} mm"
    return {"lengths": out, **({"pairs": pairs} if pairs else {})}


def cmd_summary(a: dict) -> dict:
    b = load(a["pcb"])
    if a.get("lengths"):
        return _net_lengths(b, a["lengths"])
    bb = b.GetBoardEdgesBoundingBox()
    fps = list(b.GetFootprints())
    tracks = [t for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]

    # net -> pad sayısı (boş net "" hariç)
    pads_per_net: dict[str, int] = {}
    for fp in fps:
        for pad in fp.Pads():
            n = pad.GetNetname()
            if n and not n.startswith("unconnected-"):
                pads_per_net[n] = pads_per_net.get(n, 0) + 1

    max_fp = int(a.get("max_footprints", 40))
    ds = b.GetDesignSettings()
    nc = ds.m_NetSettings.GetDefaultNetclass()
    out = {
        "file": os.path.basename(a["pcb"]),
        "size_mm": [mm(bb.GetWidth()), mm(bb.GetHeight())],
        "origin_mm": [mm(bb.GetX()), mm(bb.GetY())],
        "copper_layers": b.GetCopperLayerCount(),
        "counts": {
            "footprints": len(fps), "nets": len(pads_per_net),
            "tracks": len(tracks), "vias": len(vias), "zones": len(list(b.Zones())),
        },
        "unrouted": unrouted(b),
        "rules_mm": {
            "min_track": mm(ds.m_TrackMinWidth),
            "min_clearance": mm(ds.m_MinClearance),
            "min_via": mm(ds.m_ViasMinSize),
            "min_drill": mm(ds.m_MinThroughDrill),
        },
        # Router/çizimde fiilen kullanılan değerler netclass'tan gelir
        "default_netclass_mm": {
            "track": mm(nc.GetTrackWidth()), "clearance": mm(nc.GetClearance()),
            "via": mm(nc.GetViaDiameter()), "via_drill": mm(nc.GetViaDrill()),
        },
        "nets": " ".join(f"{n}:{c}" for n, c in sorted(pads_per_net.items())),
        "netclasses": [_netclass_line(ds.m_NetSettings, cls, nets)
                       for cls, nets in sorted(netclass_map(b).items())],
        "footprints": [fp_line(f) for f in fps[:max_fp]],
    }
    if len(fps) > max_fp:
        out["footprints_truncated"] = f"{len(fps) - max_fp} more; use get_footprint(ref)"
    return out


def cmd_footprint(a: dict) -> dict:
    b = load(a["pcb"])
    fp = b.FindFootprintByReference(a["ref"])
    if fp is None:
        raise KeyError(f"footprint {a['ref']} not found")
    bb = fp.GetBoundingBox(False)
    pads = []
    for pad in fp.Pads():
        p = pad.GetPosition()
        pads.append(f"{pad.GetNumber()}:{pad.GetNetname() or '-'}@({mm(p.x)},{mm(p.y)})")
    return {
        "line": fp_line(fp),
        "bbox_mm": [mm(bb.GetX()), mm(bb.GetY()), mm(bb.GetWidth()), mm(bb.GetHeight())],
        "pads": pads,
    }


def cmd_place(a: dict) -> dict:
    """moves: [{ref, x, y, rot?, side?}] — mm ve derece."""
    path = a["pcb"]
    b = load(path)
    done, errors = [], []
    for m in a["moves"]:
        fp = b.FindFootprintByReference(m["ref"])
        if fp is None:
            errors.append(f"{m['ref']}: not found")
            continue
        want_side = m.get("side")
        if want_side and want_side != side(fp):
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(m["x"]), pcbnew.FromMM(m["y"])))
        if "rot" in m:
            fp.SetOrientationDegrees(float(m["rot"]))
        done.append(m["ref"])
    bk = backup(path)
    pcbnew.SaveBoard(path, b)
    return {"moved": done, "errors": errors, "backup": bk, "unrouted": unrouted(b)}


def cmd_fill_zones(a: dict) -> dict:
    path = a["pcb"]
    b = load(path)
    zones = list(b.Zones())
    pcbnew.ZONE_FILLER(b).Fill(zones)
    bk = backup(path)
    pcbnew.SaveBoard(path, b)
    return {"filled": len(zones), "backup": bk}


def _stitch(b, net, pitch_mm: float, via_mm: float = 0.6, drill_mm: float = 0.3) -> int:
    """İki katmanda da aynı net dökümünün dolu olduğu ızgara noktalarına via koy.

    Via dairesi her iki katmandaki dolu poligonun tamamen içindeyse eklenir; dolgu zaten
    diğer bakırlardan clearance kadar uzak olduğu için çakışma olmaz.
    """
    zones = [z for z in b.Zones() if z.GetNetCode() == net.GetNetCode()]
    by_layer = {}
    for z in zones:
        for lyr in (pcbnew.F_Cu, pcbnew.B_Cu):
            if z.IsOnLayer(lyr):
                by_layer.setdefault(lyr, []).append(z.GetFilledPolysList(lyr))
    if pcbnew.F_Cu not in by_layer or pcbnew.B_Cu not in by_layer:
        return 0
    r = pcbnew.FromMM(via_mm / 2 + 0.05)
    ring = [(int(r * dx), int(r * dy)) for dx, dy in
            [(1, 0), (-1, 0), (0, 1), (0, -1), (.7, .7), (-.7, .7), (.7, -.7), (-.7, -.7), (0, 0)]]

    # Aynı netin pad'leri de dolgunun "içinde" sayılır (ör. GND montaj deliği); via'yı
    # deliğin/pad'in üstüne koymamak için tüm pad'lerin kutusunu via yarıçapı kadar büyüt.
    keep = []
    for p in b.GetPads():
        bb = p.GetBoundingBox()
        bb.Inflate(r)
        keep.append(bb)

    def inside(polys, x, y):
        if any(bb.Contains(pcbnew.VECTOR2I(x, y)) for bb in keep):
            return False
        return all(any(p.Contains(pcbnew.VECTOR2I(x + dx, y + dy)) for p in polys) for dx, dy in ring)

    placed = [t.GetPosition() for t in b.GetTracks()
              if t.GetClass() == "PCB_VIA" and t.GetNetCode() == net.GetNetCode()]

    def add_via(x, y):
        v = pcbnew.PCB_VIA(b)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetPosition(pcbnew.VECTOR2I(x, y))
        v.SetWidth(pcbnew.FromMM(via_mm))
        v.SetDrill(pcbnew.FromMM(drill_mm))
        v.SetNet(net)
        v.SetIsFree(True)
        b.Add(v)
        placed.append(pcbnew.VECTOR2I(x, y))

    clear = pcbnew.FromMM(via_mm + 0.3)

    def ok(x, y):  # iki katmanda da dolu ve (tekrar dökümde) mevcut bir via'nın üstü değil
        if any(abs(v.x - x) < clear and abs(v.y - y) < clear for v in placed):
            return False
        return inside(by_layer[pcbnew.F_Cu], x, y) and inside(by_layer[pcbnew.B_Cu], x, y)

    bb = b.GetBoardEdgesBoundingBox()
    step, inset = pcbnew.FromMM(pitch_mm), pcbnew.FromMM(1.5)
    n = 0
    y = bb.GetTop() + inset
    while y <= bb.GetBottom() - inset:
        x = bb.GetLeft() + inset
        while x <= bb.GetRight() - inset:
            if ok(x, y):
                add_via(x, y)
                n += 1
            x += step
        y += step

    # İkinci geçiş: ızgaraya düşmeyen dolgu adacıkları (izlerin ayırdığı küçük parçalar) diğer katmana
    # bağlanmazsa DRC 'unconnected_items Zone <-> Zone' verir. Her via'sız parçaya, ince ızgarada
    # iki katmanda da dolu ilk noktaya bir via.
    fine = pcbnew.FromMM(0.5)
    for lyr in (pcbnew.F_Cu, pcbnew.B_Cu):
        for polys in by_layer[lyr]:
            for i in range(polys.OutlineCount()):
                chain = polys.COutline(i)
                if any(chain.PointInside(v) for v in placed):
                    continue
                box = chain.BBox()
                y, done = box.GetTop(), False
                while y <= box.GetBottom() and not done:
                    x = box.GetLeft()
                    while x <= box.GetRight():
                        if chain.PointInside(pcbnew.VECTOR2I(x, y)) and ok(x, y):
                            add_via(x, y)
                            n += 1
                            done = True
                            break
                        x += fine
                    y += fine
    return n


def cmd_add_zone(a: dict) -> dict:
    """Kart sınırını kaplayan bakır dökümü (ör. GND). layer='both' -> F.Cu + B.Cu.
    stitch_pitch_mm > 0 ise iki katmanı stitching via'larla bağla."""
    path = a["pcb"]
    b = load(path)
    net = b.FindNet(a["net"])
    if net is None and not a.get("keepout_tracks"):  # keepout'un neti yok; ad için kullanılır
        raise KeyError(f"net {a['net']} not found")
    layer_arg = a.get("layer", "B.Cu")
    layers = ["F.Cu", "B.Cu"] if layer_arg == "both" else [layer_arg]
    bb = b.GetBoardEdgesBoundingBox()
    # polygon_mm verilirse bölgesel döküm (ör. ince adımlı konnektöre giden yüksek akımlı
    # güç hattı: iz genişliği pad'e sığmaz, döküm pad'i doğrudan kaplar). Yoksa tüm kart.
    poly = a.get("polygon_mm")
    pts = ([(pcbnew.FromMM(x), pcbnew.FromMM(y)) for x, y in poly] if poly else
           [(bb.GetLeft(), bb.GetTop()), (bb.GetRight(), bb.GetTop()),
            (bb.GetRight(), bb.GetBottom()), (bb.GetLeft(), bb.GetBottom())])
    if a.get("keepout_tracks"):
        # Kural alanı: bu bölgeden (bu katmanda) iz geçmesin; router başka katmana geçer.
        # Bölgesel güç dökümünün izlerle ikiye bölünmesini önlemek için autoroute'tan önce.
        for lname in layers:
            z = pcbnew.ZONE(b)
            z.SetIsRuleArea(True)
            z.SetDoNotAllowTracks(True)
            for setter in ("SetDoNotAllowVias", "SetDoNotAllowPads", "SetDoNotAllowFootprints",
                           "SetDoNotAllowZoneFills", "SetDoNotAllowCopperPour"):
                if hasattr(z, setter):
                    getattr(z, setter)(False)
            z.SetLayer(b.GetLayerID(lname))
            z.SetZoneName(f"keepout_{a['net']}")
            ol = z.Outline()
            ol.NewOutline()
            for x, y in pts:
                ol.Append(x, y)
            b.Add(z)
        out = {"keepout": f"tracks on {'+'.join(layers)}", "backup": backup(path)}
        pcbnew.SaveBoard(path, b)
        return out
    replaced = 0
    if not poly:  # tam kart dökümü tekrar çağrılırsa (rotadan sonra) eskisinin yerine geçsin, üst üste binmesin
        for z in list(b.Zones()):
            if (not z.GetIsRuleArea() and z.GetNetname() == a["net"] and z.GetAssignedPriority() == int(a.get("priority", 0))
                    and b.GetLayerName(z.GetLayer()) in layers):  # z.GetLayerName() hep 'F.Cu' döner
                b.Delete(z)
                replaced += 1
    for lname in layers:
        z = pcbnew.ZONE(b)
        z.SetLayer(b.GetLayerID(lname))
        z.SetNet(net)
        z.SetLocalClearance(pcbnew.FromMM(float(a.get("clearance_mm", 0.3))))
        z.SetMinThickness(pcbnew.FromMM(0.25))
        z.SetAssignedPriority(int(a.get("priority", 0)))
        # Bölge dökümü: küçük pad'lere thermal spoke sığmaz -> tam bağlantı. Tam kart dökümü: SMD pad'ler
        # tam, delikli pad'ler thermal. Zone ayarı .kicad_dru'yu ezdiği için (starved_thermal) burada seçilir.
        # İç katman düzlemi: delikli pad'lerin thermal kolları komşu delik boşluklarına sığmıyor
        # (starved_thermal) -> düzlemde tam bağlantı.
        inner = lname not in ("F.Cu", "B.Cu")
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL if poly or inner else pcbnew.ZONE_CONNECTION_THT_THERMAL)
        ol = z.Outline()
        ol.NewOutline()
        for x, y in pts:
            ol.Append(x, y)
        b.Add(z)
    filler = pcbnew.ZONE_FILLER(b)
    filler.Fill(list(b.Zones()))
    out = {"zone": f"{a['net']} on {'+'.join(layers)}"}
    if replaced:
        out["replaced_zones"] = replaced
    pitch = float(a.get("stitch_pitch_mm") or 0)
    if pitch > 0:
        out["stitch_vias"] = _stitch(b, net, pitch)
        filler.Fill(list(b.Zones()))  # via'lar dolguya bağlansın
    out["backup"] = backup(path)
    pcbnew.SaveBoard(path, b)
    out["unrouted"] = unrouted(b)
    return out


def cmd_export_dsn(a: dict) -> dict:
    b = load(a["pcb"])
    edge_um = b.GetDesignSettings().m_CopperEdgeClearance / 1000.0  # izleri silmeden önce (SWIG tipi kayboluyor)
    ripped = 0
    if a.get("rip_up"):
        # Kilitli olmayan iz/via'ları (önceki autoroute, stitching) yalnız bellekte sil: DSN'de sabit
        # kablo olarak kalırlarsa parça taşındıktan sonra eski izler kartta kalır (kısa devre).
        # SES içe aktarımı kartın rotasını oturumla değiştirir; kilitli route_net izleri korunur.
        for t in list(b.GetTracks()):
            if not t.IsLocked():
                b.Delete(t)
                ripped += 1
    ok = pcbnew.ExportSpecctraDSN(b, a["dsn"])
    if not ok:
        raise RuntimeError("ExportSpecctraDSN failed")
    return {"dsn": a["dsn"], "boundary_width_um": _widen_boundary(a["dsn"], edge_um), "ripped_up": ripped,
            "plane_keepouts": _plane_keepouts(a["dsn"])}


def _plane_keepouts(dsn_path: str) -> int:
    """'(type power)' katmanlarını Freerouting yine de izle kullanıyor (iç düzlemde VBUS izi, üstüne
    gelen GND via'ları kısa devre). Bu katmanların tamamına wire_keepout: iz yok, via geçebilir."""
    with open(dsn_path, encoding="utf-8") as f:
        text = f.read()
    planes = re.findall(r"\(layer (\S+)\s*\(type power\)", text)
    m = re.search(r"\(boundary\s*\(path \S+ [\d.]+\s+([-\d.\s]+)\)", text)
    if not planes or not m:
        return 0
    coords = " ".join(m.group(1).split())
    ko = "".join(f"    (wire_keepout (polygon {lyr} 0 {coords}))\n" for lyr in planes)
    text = re.sub(r"(\n\s*\(via )", "\n" + ko.rstrip("\n") + r"\1", text, count=1)
    with open(dsn_path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(planes)


def _widen_boundary(dsn_path: str, edge_um: float, margin_um: float = 100) -> float:
    """KiCad kart sınırını 0 genişlikli path olarak yazar; Freerouting sınıra yalnız iz boşluğunu
    (ör. 0.2) uygular, kartın kenar boşluğunu (ör. 0.3) bilmez -> copper_edge_clearance ihlali.
    Sınır çizgisine 2*(kenar - boşluk + pay) genişlik ver: iz merkezden boşluk + yarı genişlik uzak
    kalır, yani kenardan en az kenar boşluğu + pay. Çoğu koşuda yeterli; Freerouting nadiren kalın bir
    izi yine de kenara yakın bırakıyor (sebebi bilinmiyor): DRC yakalar, autoroute tekrar edilir. -> genişlik (um)"""
    with open(dsn_path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"\(rule\s*\(width [\d.]+\)\s*\(clearance ([\d.]+)\)", text)
    clearance = float(m.group(1)) if m else 200.0
    width = round(max(0.0, 2 * (edge_um - clearance + margin_um)), 1)
    if width > 0:
        text = re.sub(r"(\(boundary\s*\(path \S+ )0 ", lambda mm: f"{mm.group(1)}{width:g} ", text)
        with open(dsn_path, "w", encoding="utf-8") as f:
            f.write(text)
    return width


def cmd_import_ses(a: dict) -> dict:
    b = load(a["pcb"])
    before = unrouted(b)
    ok = pcbnew.ImportSpecctraSES(b, a["ses"])
    if not ok:
        raise RuntimeError("ImportSpecctraSES failed")
    out_path = a.get("out") or a["pcb"]
    if os.path.abspath(out_path) == os.path.abspath(a["pcb"]):
        backup(a["pcb"])
    pcbnew.SaveBoard(out_path, b)
    tracks = sum(1 for t in b.GetTracks() if t.GetClass() == "PCB_TRACK")
    vias = sum(1 for t in b.GetTracks() if t.GetClass() == "PCB_VIA")
    return {"out": out_path, "unrouted_before": before, "unrouted_after": unrouted(b),
            "tracks": tracks, "vias": vias}


def _field(fp, name: str) -> str:
    for f in fp.GetFields():
        if f.GetName().lower() == name.lower():
            return f.GetText()
    return ""


def cmd_fab_tables(a: dict) -> dict:
    """JLCPCB formatında BOM + CPL satırları (PCB'den, şematik gerekmez)."""
    b = load(a["pcb"])
    bom: dict[tuple, list[str]] = {}
    cpl = []
    for fp in b.GetFootprints():
        if fp.IsDNP() or fp.IsExcludedFromBOM():
            continue
        ref = fp.GetReference()
        lib = str(fp.GetFPID().GetLibItemName())
        lcsc = _field(fp, "LCSC") or _field(fp, "LCSC Part #") or _field(fp, "JLCPCB")
        bom.setdefault((fp.GetValue(), lib, lcsc), []).append(ref)
        p = fp.GetPosition()
        cpl.append([ref, f"{mm(p.x)}mm", f"{-mm(p.y)}mm",
                    "Bottom" if fp.IsFlipped() else "Top", f"{fp.GetOrientationDegrees():g}"])
    bom_rows = [[v, ",".join(sorted(refs)), lib, lcsc] for (v, lib, lcsc), refs in bom.items()]
    missing = [r[1] for r in bom_rows if not r[3]]
    return {"bom": bom_rows, "cpl": cpl, "missing_lcsc": missing}


def cmd_set_field(a: dict) -> dict:
    """Footprint'lere alan yaz (ör. LCSC=C6186). items: [{ref, field, value}]"""
    path = a["pcb"]
    b = load(path)
    done = []
    for it in a["items"]:
        fp = b.FindFootprintByReference(it["ref"])
        if fp is None:
            continue
        f = fp.GetField(it["field"]) if hasattr(fp, "GetField") else None
        if f is None:
            f = pcbnew.PCB_FIELD(fp, fp.GetNextFieldOrdinal(), it["field"])
            f.SetVisible(False)
            f.SetLayer(pcbnew.F_Fab)
            fp.Add(f)
        f.SetText(it["value"])
        done.append(f"{it['ref']}.{it['field']}={it['value']}")
    bk = backup(path)
    pcbnew.SaveBoard(path, b)
    return {"set": done, "backup": bk}


def _netclass_line(ns, name: str, nets: list[str]) -> str:
    nc = ns.GetNetClassByName(name) if name != "Default" else ns.GetDefaultNetclass()
    return (f"{name}(t{mm(nc.GetTrackWidth())},c{mm(nc.GetClearance())},"
            f"v{mm(nc.GetViaDiameter())}/{mm(nc.GetViaDrill())}): {' '.join(sorted(nets))}")


def netclass_map(b) -> dict[str, list[str]]:
    """Etkin netclass -> net listesi (boş net hariç)."""
    ns = b.GetDesignSettings().m_NetSettings
    out: dict[str, list[str]] = {}
    for name in b.GetNetsByName().keys():
        name = str(name)
        if not name or name.startswith("unconnected-"):
            continue
        cls = ns.GetEffectiveNetClass(name).GetName()
        out.setdefault(str(cls), []).append(name)
    return out


def cmd_set_netclass(a: dict) -> dict:
    """Netclass oluştur/güncelle ve netleri ata. Kurallar .kicad_pro'ya yazılır.

    nets: tam net adları veya joker desenler ('+*V*', 'USB_D?').
    apply_to_existing: bu netlerin mevcut izlerini/via'larını da yeni ölçüye getir.
    """
    path = a["pcb"]
    b = load(path)
    ns = b.GetDesignSettings().m_NetSettings
    name = a["name"]
    if name == "Default":
        nc = ns.GetDefaultNetclass()
    else:
        nc = pcbnew.NETCLASS(name)
        if ns.HasNetclass(name):  # var olanın değerlerini koru, sadece verilenleri değiştir
            old = ns.GetNetClassByName(name)
            nc.SetTrackWidth(old.GetTrackWidth()); nc.SetClearance(old.GetClearance())
            nc.SetViaDiameter(old.GetViaDiameter()); nc.SetViaDrill(old.GetViaDrill())
            if old.HasDiffPairWidth():
                nc.SetDiffPairWidth(old.GetDiffPairWidth()); nc.SetDiffPairGap(old.GetDiffPairGap())
        else:
            dflt = ns.GetDefaultNetclass()
            nc.SetTrackWidth(dflt.GetTrackWidth()); nc.SetClearance(dflt.GetClearance())
            nc.SetViaDiameter(dflt.GetViaDiameter()); nc.SetViaDrill(dflt.GetViaDrill())
    for key, setter in [("track_mm", nc.SetTrackWidth), ("clearance_mm", nc.SetClearance),
                        ("via_mm", nc.SetViaDiameter), ("via_drill_mm", nc.SetViaDrill),
                        ("dp_width_mm", nc.SetDiffPairWidth), ("dp_gap_mm", nc.SetDiffPairGap)]:
        if a.get(key) is not None:
            setter(pcbnew.FromMM(float(a[key])))
    if name != "Default":
        ns.SetNetclass(name, nc)

    for pat in a.get("nets", []):
        ns.SetNetclassPatternAssignment(pat, name)
    ns.ClearAllCaches()
    ns.RecomputeEffectiveNetclasses()
    b.SynchronizeNetsAndNetClasses(True)

    members = netclass_map(b).get(name, [])
    missing = [p for p in a.get("nets", []) if not any(c in p for c in "*?") and p not in members]

    resized = 0
    if a.get("apply_to_existing"):
        eff = ns.GetEffectiveNetClass
        for t in b.GetTracks():
            n = t.GetNetname()
            if n not in members:
                continue
            cls = eff(n)
            if t.GetClass() == "PCB_TRACK":
                t.SetWidth(cls.GetTrackWidth())
            elif t.GetClass() == "PCB_VIA":
                t.SetWidth(cls.GetViaDiameter())
                t.SetDrill(cls.GetViaDrill())
            resized += 1

    bk = backup(path)
    pcbnew.SaveBoard(path, b)
    out = {"netclass": _netclass_line(ns, name, members), "backup": bk}
    if missing:
        out["unmatched_nets"] = missing
    if a.get("apply_to_existing"):
        out["resized_items"] = resized
        out["note"] = "Wider tracks may now violate clearance; run run_drc."
    return out


# Sunucu library.FP_DIRS'ı TAMGA_FP_DIRS ile geçirir (worker KiCad Python'unda, paketi import edemez).
FP_DIRS = [p for name in ("TAMGA_FP_DIRS", "TAMGA_EXTRA_FOOTPRINT_DIRS")
           for p in os.environ.get(name, "").split(os.pathsep) if p]


def _edge_rect(b, x0: float, y0: float, w: float, h: float) -> None:
    corners = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(ax), pcbnew.FromMM(ay)))
        s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(bx), pcbnew.FromMM(by)))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(pcbnew.FromMM(0.1))
        b.Add(s)


def _seg(b, ax, ay, bx, by) -> None:
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(ax), pcbnew.FromMM(ay)))
    s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(bx), pcbnew.FromMM(by)))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(pcbnew.FromMM(0.1))
    b.Add(s)


def _arc(b, start, mid, end) -> None:
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_ARC)
    v = lambda p: pcbnew.VECTOR2I(pcbnew.FromMM(p[0]), pcbnew.FromMM(p[1]))  # noqa: E731
    s.SetArcGeometry(v(start), v(mid), v(end))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(pcbnew.FromMM(0.1))
    b.Add(s)


def _draw_outline(b, x0: float, y0: float, board: dict) -> tuple[int, int]:
    """Kart kenarı: outline_mm çokgeni (sol üst köşeye göre) ya da (yuvarlak köşeli) dikdörtgen.
    -> (segment, yay) sayısı"""
    w, h = float(board.get("width_mm", 50)), float(board.get("height_mm", 40))
    if board.get("outline_mm"):
        pts = [(x0 + float(x), y0 + float(y)) for x, y in board["outline_mm"]]
        for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
            _seg(b, ax, ay, bx, by)
        return len(pts), 0
    r = min(float(board.get("corner_radius_mm", 0)), w / 2, h / 2)
    if r <= 0:
        _edge_rect(b, x0, y0, w, h)
        return 4, 0
    k = r * (1 - 0.7071067811865476)
    x1, y1 = x0 + w, y0 + h
    _seg(b, x0 + r, y0, x1 - r, y0)
    _seg(b, x1, y0 + r, x1, y1 - r)
    _seg(b, x1 - r, y1, x0 + r, y1)
    _seg(b, x0, y1 - r, x0, y0 + r)
    _arc(b, (x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0))
    _arc(b, (x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r))
    _arc(b, (x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1))
    _arc(b, (x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r))
    return 4, 4


def _apply_outline(b, board: dict) -> str | None:
    """Spec'teki kart şekli mevcuttan farklıysa kenarı sol üst köşeyi koruyarak yeniden çiz.
    Elle çizilmiş (daire, eğri içeren) kenarlara dokunmaz."""
    edges = [d for d in b.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
    bb = b.GetBoardEdgesBoundingBox()
    cur_w, cur_h = round(mm(bb.GetWidth()) - 0.1, 2), round(mm(bb.GetHeight()) - 0.1, 2)
    segs = sum(1 for d in edges if d.GetShape() == pcbnew.SHAPE_T_SEGMENT)
    arcs = sum(1 for d in edges if d.GetShape() == pcbnew.SHAPE_T_ARC)
    cur_r = next((mm(d.GetRadius()) for d in edges if d.GetShape() == pcbnew.SHAPE_T_ARC), 0.0)
    if board.get("outline_mm"):
        xs = [float(x) for x, _ in board["outline_mm"]]
        ys = [float(y) for _, y in board["outline_mm"]]
        want = (len(board["outline_mm"]), 0, max(xs) - min(xs), max(ys) - min(ys))
    else:
        want = (4, 4 if float(board.get("corner_radius_mm", 0)) > 0 else 0,
                float(board.get("width_mm", 50)), float(board.get("height_mm", 40)))
    want_r = 0.0 if board.get("outline_mm") else min(float(board.get("corner_radius_mm", 0)), want[2] / 2, want[3] / 2)
    if ((segs, arcs) == want[:2] and abs(cur_w - want[2]) < 0.2 and abs(cur_h - want[3]) < 0.2
            and abs(cur_r - want_r) < 0.05):
        return None
    if len(edges) != segs + arcs:
        return "board outline has hand-drawn shapes; outline from spec skipped"
    x0, y0 = mm(bb.GetLeft()) + 0.05, mm(bb.GetTop()) + 0.05
    for d in edges:
        b.Delete(d)
    _draw_outline(b, x0, y0, board)
    shape = "polygon" if board.get("outline_mm") else f"r{want_r:g}" if want_r else "rect"
    return f"board outline -> {want[2]:.1f}x{want[3]:.1f} mm {shape}"


def _apply_layers(b, n: int) -> str | None:
    """2/4/6 bakır katman. İç katmanlar güç düzlemi: Freerouting onlara iz çizmez, GND/besleme dökülür."""
    if n not in (2, 4, 6):
        raise ValueError(f"layers must be 2, 4 or 6, not {n}")
    note = None
    if b.GetCopperLayerCount() != n:
        if n < b.GetCopperLayerCount() and any(
                t.GetLayer() not in (pcbnew.F_Cu, pcbnew.B_Cu) for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"):
            raise ValueError("inner layers carry tracks; remove them before reducing the layer count")
        note = f"copper layers {b.GetCopperLayerCount()} -> {n}"
        b.SetCopperLayerCount(n)
    for i in range(1, n - 1):
        lid = b.GetLayerID(f"In{i}.Cu")
        if b.GetLayerType(lid) != pcbnew.LT_POWER:
            b.SetLayerType(lid, pcbnew.LT_POWER)
    return note


HOLE_FP = {2.2: "M2", 2.7: "M2.5", 3.2: "M3", 4.3: "M4"}


def _apply_board_items(b, board: dict) -> list[str]:
    """Spec'ten montaj delikleri ve silk yazıları; 'tamga:board' grubunda, her senkronda yeniden üretilir."""
    bb = b.GetBoardEdgesBoundingBox()  # silmeden önce: sonra SWIG tipi kayboluyor
    x0, y0 = mm(bb.GetLeft()) + 0.05, mm(bb.GetTop()) + 0.05
    w, h = mm(bb.GetWidth()) - 0.1, mm(bb.GetHeight()) - 0.1
    # Remove() öğeyi sahipsiz bırakıyor ve sonraki SWIG çağrıları (FootprintLoad...) bozuluyor: Delete()
    for g in [g for g in b.Groups() if g.GetName() == "tamga:board"]:
        for it in list(g.GetItems()):
            b.Delete(it)
        b.Delete(g)
    items, made = [], []
    mh = board.get("mounting_holes")
    if mh:
        d = float(mh.get("diameter_mm", 3.2))
        if d not in HOLE_FP:
            raise ValueError(f"mounting hole diameter {d} mm: use one of {sorted(HOLE_FP)} (M2, M2.5, M3, M4)")
        fid = f"MountingHole:MountingHole_{d:g}mm_{HOLE_FP[d]}{'_Pad' if mh.get('plated') else ''}"
        inset = float(mh.get("inset_mm", 3.5))
        pts = mh.get("at_mm") or [(inset, inset), (w - inset, inset), (w - inset, h - inset), (inset, h - inset)]
        for i, (x, y) in enumerate(pts, 1):
            fp = _load_fp(fid)
            fp.SetReference(f"MH{i}")
            fp.Reference().SetVisible(False)
            fp.SetValue(f"{HOLE_FP[d]} hole")
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x0 + float(x)), pcbnew.FromMM(y0 + float(y))))
            fp.SetAttributes(fp.GetAttributes() | pcbnew.FP_BOARD_ONLY | pcbnew.FP_EXCLUDE_FROM_BOM
                             | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
            b.Add(fp)
            items.append(fp)
        made.append(f"{len(pts)} x {HOLE_FP[d]} holes")
    for t in board.get("silk") or []:
        txt = pcbnew.PCB_TEXT(b)
        txt.SetText(str(t["text"]))
        back = str(t.get("layer", "F")).upper().startswith("B")
        txt.SetLayer(pcbnew.B_SilkS if back else pcbnew.F_SilkS)
        size = pcbnew.FromMM(float(t.get("size_mm", 1.0)))
        txt.SetTextSize(pcbnew.VECTOR2I(size, size))
        txt.SetTextThickness(max(pcbnew.FromMM(0.15), int(size * 0.15)))
        txt.SetMirrored(back)
        txt.SetTextAngleDegrees(float(t.get("rot", 0)))
        txt.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x0 + float(t["x"])), pcbnew.FromMM(y0 + float(t["y"]))))
        b.Add(txt)
        items.append(txt)
    if board.get("silk"):
        made.append(f"{len(board['silk'])} silk texts")
    if items:
        g = pcbnew.PCB_GROUP(b)
        g.SetName("tamga:board")
        b.Add(g)
        for it in items:
            g.AddItem(it)
    return made


def _new_board(path: str, board: dict):
    """Boş kart: kenar (100,100)'den başlar + JLCPCB standart kuralları."""
    b = pcbnew.NewBoard(path)
    _draw_outline(b, 100.0, 100.0, board)
    ds = b.GetDesignSettings()
    ds.m_TrackMinWidth = pcbnew.FromMM(0.127)
    ds.m_MinClearance = pcbnew.FromMM(0.127)
    ds.m_ViasMinSize = pcbnew.FromMM(0.45)
    ds.m_MinThroughDrill = pcbnew.FromMM(0.2)
    ds.m_CopperEdgeClearance = pcbnew.FromMM(0.3)
    return b


def _load_fp(fid: str):
    lib, name = fid.split(":", 1)
    fp = None
    for root in FP_DIRS:  # library.FP_DIRS ile aynı sıra (stok KiCad önce)
        if os.path.isfile(f"{root}/{lib}.pretty/{name}.kicad_mod"):
            fp = pcbnew.FootprintLoad(f"{root}/{lib}.pretty", name)
            break
    if fp is None:
        raise KeyError(f"footprint {fid} not found")
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp


def _set_field(fp, name: str, val: str) -> None:
    f = fp.GetField(name)
    if f is None:
        f = pcbnew.PCB_FIELD(fp, fp.GetNextFieldOrdinal(), name)
        f.SetVisible(False)
        f.SetLayer(pcbnew.F_Fab)
        fp.Add(f)
    f.SetText(val)


def cmd_sync_pcb(a: dict) -> dict:
    """Şematikten PCB'ye güncelle (KiCad'in F8'i gibi): ekle / değiştir / sil, yerleşimi koru.

    parts: [{ref, value, footprint, lcsc, sym_uuid, dnp, pads: {pad_no: net}}]
    """
    path = a["pcb"]
    created = not os.path.isfile(path)
    resized = None
    board = a.get("board") or {}
    if created:
        b = _new_board(path, board)
    else:
        b = load(path)
        resized = _apply_outline(b, board) if board else None
    layer_note = _apply_layers(b, int(board.get("layers", b.GetCopperLayerCount())))

    # Bakır taşıyan netler (iz/via/döküm): senkron sonrası pad'siz kalırsa ad değişmiş/silinmiş demek
    copper_nets = {t.GetNetname() for t in b.GetTracks()} | {z.GetNetname() for z in b.Zones()}

    nets = {}
    for p in a["parts"]:
        for net in p["pads"].values():
            if net not in nets:
                n = b.FindNet(net)
                if n is None:
                    n = pcbnew.NETINFO_ITEM(b, net)
                    b.Add(n)
                nets[net] = n

    want = {p["ref"]: p for p in a["parts"] if p.get("footprint")}
    added, replaced, removed, updated, warnings = [], [], [], [], []
    existing = {fp.GetReference(): fp for fp in b.GetFootprints()}

    for ref, fp in existing.items():
        # "Not in schematic" (board-only) footprint'ler (logo, fiducial, montaj işareti)
        # şematikten gelmez; senkronizasyon onlara dokunmaz.
        if fp.GetAttributes() & pcbnew.FP_BOARD_ONLY:
            continue
        if ref not in want:
            b.Delete(fp)
            removed.append(ref)

    bb = b.GetBoardEdgesBoundingBox()
    cur_x, cur_y, row_h = bb.GetLeft() + pcbnew.FromMM(2), bb.GetTop() + pcbnew.FromMM(2), 0
    kept = [fp for r, fp in existing.items() if r in want]
    if kept:  # yeni parçaları mevcut yerleşimin altından başlat, üstüne koyma
        cur_y = max(fp.GetBoundingBox(False).GetBottom() for fp in kept) + pcbnew.FromMM(1.5)
    for ref, p in want.items():
        old = existing.get(ref)
        if old is not None and str(old.GetFPID().GetUniStringLibId()) == p["footprint"]:
            fp = old
            updated.append(ref)
        else:
            try:
                fp = _load_fp(p["footprint"])
            except KeyError as e:
                warnings.append(f"{ref}: {e}")
                continue
            fp.SetReference(ref)
            if old is not None:  # footprint değişti: konumu ve yüzü koru
                fp.SetPosition(old.GetPosition())
                fp.SetOrientation(old.GetOrientation())
                if old.IsFlipped():
                    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
                b.Delete(old)
                replaced.append(ref)
            else:  # yeni: kart içinde ızgaraya diz
                fbb = fp.GetBoundingBox(False)
                w, h = fbb.GetWidth(), fbb.GetHeight()
                if cur_x + w > bb.GetRight() - pcbnew.FromMM(1):
                    cur_x, cur_y, row_h = bb.GetLeft() + pcbnew.FromMM(2), cur_y + row_h + pcbnew.FromMM(1.5), 0
                off = fp.GetPosition() - fbb.GetOrigin()
                fp.SetPosition(pcbnew.VECTOR2I(int(cur_x + off.x), int(cur_y + off.y)))
                cur_x += w + pcbnew.FromMM(1.5)
                row_h = max(row_h, h)
                added.append(ref)
            b.Add(fp)
        fp.SetValue(p.get("value") or fp.GetValue())
        # hiyerarşik sayfadaki sembol: yol /<sheet_uuid>/<sym_uuid>, sheetname /<instance>/
        fp.SetPath(pcbnew.KIID_PATH(p.get("path") or f"/{p['sym_uuid']}"))
        fp.SetSheetname(p.get("sheetname") or "/")
        fp.SetSheetfile(p.get("sheetfile") or a["sheetfile"])
        fp.SetDNP(bool(p.get("dnp")))
        fp.SetExcludedFromBOM(not p.get("in_bom", True))  # sembolle aynı (parity)
        if p.get("lcsc"):
            _set_field(fp, "LCSC", p["lcsc"])

        # pin<->pad uyumu design.sync'te dosya yazılmadan önce kontrol edilir (check_pin_pad)
        for pad in fp.Pads():
            net = p["pads"].get(pad.GetNumber())
            if net:
                pad.SetNet(nets[net])
            else:
                pad.SetNetCode(0)

    board_items = _apply_board_items(b, board)
    bk = None if created else backup(path)
    pcbnew.SaveBoard(path, b)
    out = {"pcb": path, "created": created, "added": added, "updated": len(updated),
           "replaced": replaced, "removed": removed, "unrouted": unrouted(b)}
    if board_items:
        out["board_items"] = board_items
    if layer_note:
        out["layers"] = layer_note
    if resized:
        out["resized"] = resized
    pad_nets = {pad.GetNetname() for f in b.GetFootprints() for pad in f.Pads()}
    if gone := sorted(n for n in copper_nets - pad_nets if n):
        out["nets_gone"] = gone
    if bk:
        out["backup"] = bk
    if warnings:
        out["warnings"] = warnings
    outside = [f.GetReference() for f in b.GetFootprints()
               if not b.GetBoardEdgesBoundingBox().Contains(f.GetPosition())]
    if outside:
        out["outside_board"] = outside
    return out


def _pad(b, spec: str, net: str):
    """'U1.3' -> pad; netin üzerinde olmalı. Aynı numaralı birden çok pad (tab) varsa hepsi aday."""
    ref, _, num = spec.partition(".")
    fp = b.FindFootprintByReference(ref)
    if fp is None or not num:
        raise KeyError(f"pad '{spec}': use REF.PAD (e.g. U1.3)")
    pads = [p for p in fp.Pads() if p.GetNumber() == num]
    if not pads:
        raise KeyError(f"{ref} has no pad {num}")
    on_net = [p for p in pads if p.GetNetname() == net]
    if not on_net:
        raise KeyError(f"{spec} is on net '{pads[0].GetNetname()}', not '{net}'")
    return on_net


def _route_one(b, netname: str, from_pad: str, to_pad: str, points: list, layer: str, width_mm) -> dict:
    """Tek net: from_pad -> points -> to_pad, kilitli iz; katman değişiminde via."""
    net = b.FindNet(netname)
    if net is None:
        raise KeyError(f"net {netname} not found")
    ns = b.GetDesignSettings().m_NetSettings
    cls = ns.GetEffectiveNetClass(netname)
    width = pcbnew.FromMM(width_mm) if width_mm else cls.GetTrackWidth()
    starts, ends = _pad(b, from_pad, netname), _pad(b, to_pad, netname)

    # nokta listesi: (x, y, katman); katman verilmezse bir öncekinin katmanı
    pts = []
    for q in points:
        layer = q[2] if len(q) > 2 else layer
        pts.append((pcbnew.FromMM(float(q[0])), pcbnew.FromMM(float(q[1])), layer))
    first_layer = pts[0][2] if pts else layer
    last_layer = pts[-1][2] if pts else layer

    def pick(pads, lname, near):
        ok = [p for p in pads if p.IsOnLayer(b.GetLayerID(lname))]
        if not ok:
            raise ValueError(f"pad {pads[0].GetParentFootprint().GetReference()}.{pads[0].GetNumber()} "
                             f"is not on {lname}; start/end on its layer or add a via point")
        return min(ok, key=lambda p: (p.GetPosition() - near).EuclideanNorm())

    first_target = pcbnew.VECTOR2I(*pts[0][:2]) if pts else ends[0].GetPosition()
    sp = pick(starts, first_layer, first_target).GetPosition()
    last_from = pcbnew.VECTOR2I(*pts[-1][:2]) if pts else sp
    ep = pick(ends, last_layer, last_from).GetPosition()

    chain = [(sp.x, sp.y, first_layer), *pts, (ep.x, ep.y, last_layer)]
    segs = vias = 0
    length = 0.0
    for (x0, y0, l0), (x1, y1, l1) in zip(chain, chain[1:]):
        if l1 != l0:  # katman geçişi: önceki noktaya via, segment yeni katmanda
            v = pcbnew.PCB_VIA(b)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetPosition(pcbnew.VECTOR2I(x0, y0))
            v.SetWidth(cls.GetViaDiameter())
            v.SetDrill(cls.GetViaDrill())
            v.SetNet(net)
            v.SetLocked(True)
            b.Add(v)
            vias += 1
        if (x0, y0) == (x1, y1):
            continue
        s = pcbnew.PCB_TRACK(b)
        s.SetStart(pcbnew.VECTOR2I(x0, y0))
        s.SetEnd(pcbnew.VECTOR2I(x1, y1))
        s.SetWidth(width)
        s.SetLayer(b.GetLayerID(l1))
        s.SetNet(net)
        s.SetLocked(True)
        b.Add(s)
        segs += 1
        length += mm(int(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5))
    return {"segments": segs, "vias": vias, "length_mm": round(length, 2), "width_mm": mm(width)}


def _offset_path(points: list, d: float) -> list:
    """Merkez hattını (mm, [x,y,(katman)]) d kadar sola kaydır (köşelerde gönye)."""
    xy = [(float(q[0]), float(q[1])) for q in points]
    out = []
    for i, (x, y) in enumerate(xy):
        dirs = []
        if i > 0:
            dirs.append((x - xy[i - 1][0], y - xy[i - 1][1]))
        if i < len(xy) - 1:
            dirs.append((xy[i + 1][0] - x, xy[i + 1][1] - y))
        nx = ny = 0.0
        for dx, dy in dirs:
            ln = (dx * dx + dy * dy) ** 0.5 or 1.0
            nx, ny = nx - dy / ln, ny + dx / ln
        ln = (nx * nx + ny * ny) ** 0.5 or 1.0
        nx, ny = nx / ln, ny / ln
        miter = 1.0
        if len(dirs) == 2:  # gönye: iç açının yarısının kosinüsüne böl
            (ax, ay), (bx, by) = dirs
            la, lb = (ax * ax + ay * ay) ** 0.5 or 1.0, (bx * bx + by * by) ** 0.5 or 1.0
            cos_half = max(0.3, (((ax / la) * (bx / lb) + (ay / la) * (by / lb)) + 1) / 2) ** 0.5
            miter = 1 / cos_half
        out.append([round(x + nx * d * miter, 4), round(y + ny * d * miter, 4), *points[i][2:]])
    return out


def cmd_route_net(a: dict) -> dict:
    """Elle yönlendirilmiş rota: from_pad -> points -> to_pad. Nokta [x, y] ya da [x, y, layer];
    katman değişince o noktaya via. İzler KİLİTLİ: sonraki autoroute bunlara dokunmaz.
    pair_net verilirse diferansiyel çift: points çiftin merkez hattı, iki iz gap_mm aralıkla."""
    path = a["pcb"]
    b = load(path)
    before = unrouted(b)
    layer = a.get("layer", "F.Cu")
    if not a.get("pair_net"):
        res = _route_one(b, a["net"], a["from_pad"], a["to_pad"], a.get("points", []), layer, a.get("width_mm"))
    else:
        cls = b.GetDesignSettings().m_NetSettings.GetEffectiveNetClass(a["net"])
        width = float(a.get("width_mm") or (mm(cls.GetDiffPairWidth()) if cls.HasDiffPairWidth() else 0)
                      or mm(cls.GetTrackWidth()))
        gap = float(a.get("gap_mm") or (mm(cls.GetDiffPairGap()) if cls.HasDiffPairGap() else 0) or 0.2)
        pts = a.get("points") or []
        p_start = _pad(b, a["from_pad"], a["net"])[0].GetPosition()
        n_start = _pad(b, a["pair_from_pad"], a["pair_net"])[0].GetPosition()
        p_end = _pad(b, a["to_pad"], a["net"])[0].GetPosition()
        n_end = _pad(b, a["pair_to_pad"], a["pair_net"])[0].GetPosition()
        mid = lambda u, v: [mm((u.x + v.x) // 2), mm((u.y + v.y) // 2)]  # noqa: E731
        center = [mid(p_start, n_start) + [layer], *pts, mid(p_end, n_end)]
        # P izi, başlangıç pad'inin olduğu tarafa. _offset_path'te +d = yön vektörünün (-dy, dx) normali;
        # P pad'i o normal yönündeyse (iç çarpım > 0) +, değilse -.
        (cx, cy), (nx_, ny_) = center[0][:2], center[1][:2]
        dot = -(ny_ - cy) * (mm(p_start.x) - cx) + (nx_ - cx) * (mm(p_start.y) - cy)
        side = 1 if dot > 0 else -1
        off = (gap + width) / 2
        guide_p = _offset_path(center, side * off)[1:-1]
        guide_n = _offset_path(center, -side * off)[1:-1]
        rp = _route_one(b, a["net"], a["from_pad"], a["to_pad"], guide_p, layer, width)
        rn = _route_one(b, a["pair_net"], a["pair_from_pad"], a["pair_to_pad"], guide_n, layer, width)
        res = {"pair": f"{a['net']}/{a['pair_net']}", "width_mm": round(width, 3), "gap_mm": gap,
               "length_mm": [rp["length_mm"], rn["length_mm"]],
               "skew_mm": round(abs(rp["length_mm"] - rn["length_mm"]), 2),
               "segments": rp["segments"] + rn["segments"], "vias": rp["vias"] + rn["vias"]}
    after = unrouted(b)
    bk = backup(path)
    pcbnew.SaveBoard(path, b)
    return {"net": a["net"], **res, "unrouted_before": before, "unrouted_after": after,
            "locked": True, "backup": bk,
            "note": "Run run_drc: points are taken as given (no clearance check here)."}


COMMANDS = {
    "route_net": cmd_route_net,
    "sync_pcb": cmd_sync_pcb,
    "set_netclass": cmd_set_netclass,
    "fab_tables": cmd_fab_tables,
    "set_field": cmd_set_field,
    "summary": cmd_summary,
    "footprint": cmd_footprint,
    "place": cmd_place,
    "fill_zones": cmd_fill_zones,
    "add_zone": cmd_add_zone,
    "export_dsn": cmd_export_dsn,
    "import_ses": cmd_import_ses,
}


def main() -> None:
    cmd, args = sys.argv[1], json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}")
    try:
        res = {"ok": True, "result": COMMANDS[cmd](args)}
    except Exception as e:  # noqa: BLE001 — hatayı JSON olarak geri ver
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # pcbnew stdout'a log basabilir; sonucu ayırt edilebilir son satıra yaz
    sys.stdout.write("\n@@TAMGA@@" + json.dumps(res, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
