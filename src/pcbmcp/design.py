"""Devre tanımı (spec) -> şematik + PCB, tek doğruluk kaynağı.

<dir>/<name>.circuit.json  : spec (git'te tutulur, LLM bunu düzenler)
<dir>/<name>.kicad_sch      : spec'ten üretilir (elle düzenleme bir sonraki senkronda ezilir)
<dir>/<name>.kicad_pcb      : ilk senkronda oluşur, sonra yerleşim/rota KORUNUR
"""
from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import library, pinout, report, schematic, sexpr
from .kicad import abspath, cli, drc_json, erc_json, run_worker


def _paths(project_dir: str, name: str) -> dict[str, Path]:
    d = Path(abspath(project_dir))
    d.mkdir(parents=True, exist_ok=True)
    return {"dir": d, "spec": d / f"{name}.circuit.json", "sch": d / f"{name}.kicad_sch",
            "pcb": d / f"{name}.kicad_pcb"}


def ensure_lib_tables(d: Path, spec: dict) -> list[str]:
    """Stok olmayan kütüphaneleri (pcbmcp_custom, pcbmcp_lcsc...) projenin sym-lib-table /
    fp-lib-table'ına ${KIPRJMOD}'a göreli ekle. Var olan kayıtlara dokunmaz. -> eklenenler"""
    added = []
    for kind, table, key in (("sym", "sym-lib-table", "symbol"), ("fp", "fp-lib-table", "footprint")):
        libs = sorted({p[key].split(":", 1)[0] for p in spec["parts"] if ":" in p.get(key, "")})
        need = {lib: path for lib in libs if (path := library.extra_lib_path(kind, lib))}
        if not need:
            continue
        f = d / table
        S = sexpr.Sym
        tree = sexpr.parse(f.read_text()) if f.is_file() else \
            [S(table.replace("-", "_")), [S("version"), 7]]
        have = {str(sexpr.value(e, "name")) for e in sexpr.children(tree, "lib")}
        for lib, path in need.items():
            if lib in have:
                continue
            uri = "${KIPRJMOD}/" + os.path.relpath(path, d)
            tree.append([S("lib"), [S("name"), lib], [S("type"), "KiCad"], [S("uri"), uri],
                         [S("options"), ""], [S("descr"), "pcbmcp"]])
            added.append(f"{table}:{lib}")
        f.write_text(sexpr.dump(tree) + "\n")
    return added


def load_spec(project_dir: str, name: str) -> dict | None:
    p = _paths(project_dir, name)["spec"]
    return json.loads(p.read_text()) if p.is_file() else None


def apply_patch(spec: dict, patch: dict) -> dict:
    """patch: {parts:[...] (ref'e göre ekle/değiştir, alanları birleştir), remove:[ref], board:{...}, title}"""
    spec = json.loads(json.dumps(spec))
    by_ref = {p["ref"]: p for p in spec["parts"]}
    for p in patch.get("parts", []):
        if p["ref"] in by_ref:
            merged = {**by_ref[p["ref"]], **p}
            if "pins" in p and p.get("merge_pins", True):
                merged["pins"] = {**by_ref[p["ref"]].get("pins", {}), **p["pins"]}
            merged.pop("merge_pins", None)
            spec["parts"][spec["parts"].index(by_ref[p["ref"]])] = merged
        else:
            spec["parts"].append(p)
    rm = set(patch.get("remove", []))
    spec["parts"] = [p for p in spec["parts"] if p["ref"] not in rm]
    if "board" in patch:
        spec["board"] = {**spec.get("board", {}), **patch["board"]}
    if "title" in patch:
        spec["title"] = patch["title"]
    return spec


def _netlist(sch: Path, tmp: str) -> dict[str, list[tuple[str, str]]]:
    out = f"{tmp}/n.net"
    cli("sch", "export", "netlist", "-o", out, str(sch))
    tree = sexpr.parse(Path(out).read_text())
    nets = {}
    for n in sexpr.children(sexpr.child(tree, "nets"), "net"):
        nets[str(sexpr.value(n, "name"))] = [(str(sexpr.value(x, "ref")), str(sexpr.value(x, "pin")))
                                             for x in sexpr.children(n, "node")]
    return nets


def sync(project_dir: str, name: str, spec: dict | None, patch: dict | None) -> dict:
    paths = _paths(project_dir, name)
    if spec is None:
        spec = load_spec(project_dir, name)
        if spec is None:
            raise ToolError(f"no {paths['spec'].name}; pass a full spec first")
    if patch:
        spec = apply_patch(spec, patch)

    text, meta = schematic.build(spec, name)
    if meta["errors"]:
        # yanlış pin adı vb. — hiçbir dosyaya dokunmadan geri dön
        raise ToolError("spec errors (nothing written): " + "; ".join(meta["errors"]))
    missing_fp = [p["ref"] for p in spec["parts"] if not p.get("footprint")]

    # pin<->pad: dosya yazmadan ÖNCE. Eksik pad = hata (net PCB'ye gitmez), boş pad = uyarı.
    pad_errors, pad_warnings = [], []
    for p in spec["parts"]:
        if not p.get("footprint"):
            continue
        chk = library.check_pin_pad(p["symbol"], p["footprint"])
        if chk["missing_pads"]:
            pad_errors.append(f"{p['ref']}: pins {chk['missing_pads']} have no pad in {p['footprint']}")
        if chk["unused_pads"]:
            pad_warnings.append(f"{p['ref']}: pads {chk['unused_pads']} have no symbol pin (left unconnected)")
    if pad_errors:
        raise ToolError("pin/pad mismatch (nothing written): " + "; ".join(pad_errors))

    # sembol pinout'u <-> gerçek LCSC parçası. Sezgisel isim eşleştirmesi -> uyarı, hata değil.
    # Ağ yoksa parça 'pinout_unchecked'a düşer; sonuçlar diskte cache'lenir.
    lcsc_parts = [p for p in spec["parts"] if p.get("lcsc")]
    pinout_warnings, pinout_unchecked = [], []

    def _pin_check(p):
        try:
            return p, pinout.check(p["symbol"], p["lcsc"])
        except ToolError:
            return p, None

    with ThreadPoolExecutor(max_workers=3) as ex:  # EasyEDA ~90 hızlı istekte 403 veriyor
        for p, res in ex.map(_pin_check, lcsc_parts):
            if res is None:
                pinout_unchecked.append(p["ref"])
            elif "pinout" not in res:
                issues = res.get("mismatch", []) + [f"part-only pin {x}" for x in res.get("part_only", [])]
                if res.get("symbol_only"):
                    issues.append(f"symbol-only pins {res['symbol_only']}")
                pinout_warnings.append(f"{p['ref']} ({p['lcsc']} {res['part']}): " + "; ".join(issues))

    paths["spec"].write_text(json.dumps(spec, indent=1, ensure_ascii=False) + "\n")
    paths["sch"].write_text(text)
    libs_added = ensure_lib_tables(paths["dir"], spec)

    with tempfile.TemporaryDirectory() as tmp:
        nets = _netlist(paths["sch"], tmp)
        # netlist = gerçek bağlantı; PCB pad netleri buradan gelir (KiCad'in kendi akışı gibi)
        pad_nets: dict[str, dict[str, str]] = {}
        for net, nodes in nets.items():
            # 'unconnected-(...)' netleri de PCB'ye taşınır; KiCad'in parity kontrolü bunu bekliyor
            for ref, pin in nodes:
                pad_nets.setdefault(ref, {})[pin] = net
        intended = {(p["ref"], num): net for p in meta["parts"] for num, net in p["pads"].items()}
        actual = {(r, pin): n for r, pads in pad_nets.items() for pin, n in pads.items()
                  if not n.startswith("unconnected-")}
        mismatch = [f"{r}.{pin}: want {n}, got {actual.get((r, pin), 'unconnected')}"
                    for (r, pin), n in intended.items() if actual.get((r, pin)) != n]

        for p in meta["parts"]:
            p["pads"] = pad_nets.get(p["ref"], {})
        pcb = run_worker("sync_pcb", {
            "pcb": str(paths["pcb"]), "board": spec.get("board", {}),
            "parts": meta["parts"], "sheetfile": paths["sch"].name,
        })
        erc = report.summarize(erc_json(str(paths["sch"]), f"{tmp}/erc.json"))
        drc = drc_json(str(paths["pcb"]), f"{tmp}/drc.json", parity=True)
        # sadece şematik<->PCB uyumsuzlukları; yerleşim ihlalleri run_drc'nin işi
        parity_types = report.summarize({"schematic_parity": drc.get("schematic_parity", [])})["types"]

    out = {
        "files": {k: str(v) for k, v in paths.items() if k != "dir"},
        "parts": len(meta["parts"]),
        "nets": " ".join(f"{n}:{len(v)}" for n, v in sorted(nets.items()) if not n.startswith("unconnected-")),
        "netlist_check": "OK" if not mismatch else mismatch[:20],
        "erc": erc if erc.get("verdict") != "CLEAN" else "CLEAN",
        "pcb": pcb,
        "parity": "OK" if not parity_types else parity_types,
    }
    if meta["pwr_flags"]:
        out["pwr_flags_added"] = meta["pwr_flags"]
    if missing_fp:
        out["no_footprint"] = missing_fp
    if pad_warnings:
        out["pad_warnings"] = pad_warnings
    if libs_added:
        out["lib_tables_added"] = libs_added
    if pinout_warnings:
        out["pinout_warnings"] = pinout_warnings
    if pinout_unchecked:
        out["pinout_unchecked"] = pinout_unchecked
    return out
