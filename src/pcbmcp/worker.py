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
    """Yazmadan önce .pcbmcp_backups/ altına zaman damgalı kopya al."""
    d = os.path.join(os.path.dirname(os.path.abspath(path)), ".pcbmcp_backups")
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

def cmd_summary(a: dict) -> dict:
    b = load(a["pcb"])
    bb = b.GetBoardEdgesBoundingBox()
    fps = list(b.GetFootprints())
    tracks = [t for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]

    # net -> pad sayısı (boş net "" hariç)
    pads_per_net: dict[str, int] = {}
    for fp in fps:
        for pad in fp.Pads():
            n = pad.GetNetname()
            if n:
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


def cmd_add_zone(a: dict) -> dict:
    """Kart sınırını kaplayan bir bakır dökümü (ör. GND) ekle."""
    path = a["pcb"]
    b = load(path)
    net = b.FindNet(a["net"])
    if net is None:
        raise KeyError(f"net {a['net']} not found")
    layer = b.GetLayerID(a.get("layer", "B.Cu"))
    bb = b.GetBoardEdgesBoundingBox()
    z = pcbnew.ZONE(b)
    z.SetLayer(layer)
    z.SetNet(net)
    z.SetLocalClearance(pcbnew.FromMM(float(a.get("clearance_mm", 0.3))))
    z.SetMinThickness(pcbnew.FromMM(0.25))
    ol = z.Outline()
    ol.NewOutline()
    for x, y in [(bb.GetLeft(), bb.GetTop()), (bb.GetRight(), bb.GetTop()),
                 (bb.GetRight(), bb.GetBottom()), (bb.GetLeft(), bb.GetBottom())]:
        ol.Append(x, y)
    b.Add(z)
    pcbnew.ZONE_FILLER(b).Fill(list(b.Zones()))
    bk = backup(path)
    pcbnew.SaveBoard(path, b)
    return {"zone": f"{a['net']} on {a.get('layer', 'B.Cu')}", "backup": bk, "unrouted": unrouted(b)}


def cmd_export_dsn(a: dict) -> dict:
    b = load(a["pcb"])
    ok = pcbnew.ExportSpecctraDSN(b, a["dsn"])
    if not ok:
        raise RuntimeError("ExportSpecctraDSN failed")
    return {"dsn": a["dsn"]}


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
        if not name:
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
        else:
            dflt = ns.GetDefaultNetclass()
            nc.SetTrackWidth(dflt.GetTrackWidth()); nc.SetClearance(dflt.GetClearance())
            nc.SetViaDiameter(dflt.GetViaDiameter()); nc.SetViaDrill(dflt.GetViaDrill())
    for key, setter in [("track_mm", nc.SetTrackWidth), ("clearance_mm", nc.SetClearance),
                        ("via_mm", nc.SetViaDiameter), ("via_drill_mm", nc.SetViaDrill)]:
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


COMMANDS = {
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
    sys.stdout.write("\n@@PCBMCP@@" + json.dumps(res, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
