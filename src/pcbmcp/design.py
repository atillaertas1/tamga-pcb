"""Devre tanımı (spec) -> şematik + PCB, tek doğruluk kaynağı.

<dir>/<name>.circuit.json  : spec (git'te tutulur, LLM bunu düzenler)
<dir>/<name>.kicad_sch      : spec'ten üretilir (elle düzenleme bir sonraki senkronda ezilir)
<dir>/<name>.kicad_pcb      : ilk senkronda oluşur, sonra yerleşim/rota KORUNUR
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import blocks, library, pinout, report, schematic, sexpr
from .kicad import abspath, cli, drc_json, erc_json, run_worker


def _paths(project_dir: str, name: str) -> dict[str, Path]:
    d = Path(abspath(project_dir))
    d.mkdir(parents=True, exist_ok=True)
    return {"dir": d, "spec": d / f"{name}.circuit.json", "sch": d / f"{name}.kicad_sch",
            "pcb": d / f"{name}.kicad_pcb"}


LOCAL_LIBS = "lib"  # stok olmayan kütüphanelerin proje içi kopyası: proje tek başına taşınabilir


def _copy_lib(src: Path, dst: Path) -> None:
    """Kütüphaneyi (dosya ya da .pretty dizini) projeye kopyala; yalnız eksik/yeni dosyalar."""
    files = [(f, dst / f.name) for f in src.iterdir() if f.is_file()] if src.is_dir() else [(src, dst)]
    for a, b in files:
        if not b.exists() or a.stat().st_mtime > b.stat().st_mtime:
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(a, b)


def ensure_lib_tables(d: Path, spec: dict) -> list[str]:
    """Stok olmayan kütüphaneleri (pcbmcp_custom, pcbmcp_lcsc...) projenin lib/ klasörüne kopyala ve
    sym-lib-table / fp-lib-table'a ${KIPRJMOD}/lib/... olarak ekle. Kullanıcının kendi kayıtlarına
    dokunmaz; pcbmcp'nin eski kayıtlarını (descr 'pcbmcp...') yeni yola taşır. -> eklenen/taşınanlar"""
    added = []
    for kind, table, key in (("sym", "sym-lib-table", "symbol"), ("fp", "fp-lib-table", "footprint")):
        libs = sorted({p[key].split(":", 1)[0] for p in spec["parts"] if ":" in p.get(key, "")})
        need = {lib: path for lib in libs if (path := library.extra_lib_path(kind, lib))}
        if not need:
            continue
        f = d / table
        S = sexpr.Sym
        tree = sexpr.parse(f.read_text(encoding="utf-8")) if f.is_file() else \
            [S(table.replace("-", "_")), [S("version"), 7]]
        have = {str(sexpr.value(e, "name")): e for e in sexpr.children(tree, "lib")}
        changed = False
        for lib, path in need.items():
            _copy_lib(path, d / LOCAL_LIBS / path.name)
            uri = "${KIPRJMOD}/" + LOCAL_LIBS + "/" + path.name
            if (e := have.get(lib)) is None:
                tree.append([S("lib"), [S("name"), lib], [S("type"), "KiCad"], [S("uri"), uri],
                             [S("options"), ""], [S("descr"), "pcbmcp"]])
            elif str(sexpr.value(e, "descr") or "").startswith("pcbmcp") and sexpr.value(e, "uri") != uri:
                sexpr.child(e, "uri")[1] = uri
            else:
                continue
            changed = True
            added.append(f"{table}:{lib}")
        if changed:
            f.write_text(sexpr.dump(tree) + "\n", encoding="utf-8", newline="\n")
    return added


def _dru_mentions(d: Path, nets: list[str]) -> list[str]:
    """Projedeki .kicad_dru dosyalarında bu net adlarına uyan tırnaklı desenler ('IC?P' gibi)."""
    hits = []
    for f in d.glob("*.kicad_dru"):
        pats = set(re.findall(r"NetName\s*==\s*'([^']+)'", f.read_text(encoding="utf-8")))
        for n in nets:
            if m := [pt for pt in pats if fnmatch.fnmatchcase(n, pt)]:
                hits.append(f"{n} ~ {m[0]} ({f.name})")
    return hits


def load_spec(project_dir: str, name: str) -> dict | None:
    p = _paths(project_dir, name)["spec"]
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def apply_patch(spec: dict, patch: dict) -> dict:
    """patch: {parts:[...] (ref'e göre ekle/değiştir, alanları birleştir), remove:[ref], board:{...}, title,
    blocks:{name: def|null} (değiştir/sil), instances:[...] (name'e göre ekle/birleştir; nets/set/refs
    alan alan birleşir), remove_instances:[name]}"""
    spec = json.loads(json.dumps(spec))
    spec.setdefault("parts", [])
    for bname, bdef in (patch.get("blocks") or {}).items():
        if bdef is None:
            spec.get("blocks", {}).pop(bname, None)
        else:
            spec.setdefault("blocks", {})[bname] = bdef
    insts = {i["name"]: i for i in spec.get("instances", [])}
    for inst in patch.get("instances", []):
        old = insts.get(inst["name"])
        if old is None:
            spec.setdefault("instances", []).append(inst)
            continue
        for k, v in inst.items():
            old[k] = {**old.get(k, {}), **v} if k in ("nets", "set", "refs") else v
    if rm_i := set(patch.get("remove_instances", [])):
        spec["instances"] = [i for i in spec.get("instances", []) if i["name"] not in rm_i]
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
    tree = sexpr.parse(Path(out).read_text(encoding="utf-8"))
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
    source = spec  # diske bloklu hali yazılır; geri kalan her şey açılmış (düz) parçalarla çalışır
    spec = blocks.expand(source)

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

    paths["spec"].write_text(json.dumps(source, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    paths["sch"].write_text(text, encoding="utf-8", newline="\n")
    # blok instance sayfaları; artık kullanılmayan (bizim ürettiğimiz) sayfaları sil
    for fname, page_text in meta["pages"].items():
        (paths["dir"] / fname).write_text(page_text, encoding="utf-8", newline="\n")
    for old in paths["dir"].glob(f"{name}-*.kicad_sch"):
        if old.name not in meta["pages"] and '(generator "pcbmcp")' in old.read_text(encoding="utf-8")[:300]:
            old.unlink()
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
        **({"instances": blocks.summary(source, spec)} if source.get("instances") else {}),
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
    if gone := pcb.get("nets_gone"):
        # İz/döküm taşıyan net pad'siz kaldı: ad değişti ya da silindi. Bağlantı doğru olabilir ama
        # .kicad_dru kuralları ve netclass desenleri net ADINA bakar -> sessizce devre dışı kalır.
        hits = _dru_mentions(paths["dir"], gone)
        out["nets_gone"] = gone[:20]
        if hits:
            out["dru_rules_reference_gone_nets"] = hits
    if pinout_warnings:
        out["pinout_warnings"] = pinout_warnings
    if pinout_unchecked:
        out["pinout_unchecked"] = pinout_unchecked
    return out
