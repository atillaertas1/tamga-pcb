"""LCSC parçasını KiCad kütüphanesine içe aktar (easyeda2kicad + kicad-cli upgrade).

Çıktı (paths.DATA_DIR altında): symbols/pcbmcp_lcsc.kicad_sym, footprints/pcbmcp_lcsc.pretty/, footprints/pcbmcp_lcsc.3dshapes/

EasyEDA dönüşümünün doğruluğu garanti değil. Gerçek örnek: Hirose DF40C-100DS için EasyEDA
pad'leri U şeklinde (bir sıra 1..50) numaralar, KiCad/Hirose tek-çift (1 ve 2 karşılıklı).
Bu yüzden KiCad'de aynı parçanın footprint'i varsa pad numarası+konum karşılaştırılır.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import library, paths, sexpr
from .kicad import cli
from .sexpr import children, child

LIB = "pcbmcp_lcsc"


def _paths() -> dict[str, Path]:
    root = Path(os.environ.get("PCBMCP_IMPORT_DIR", paths.DATA_DIR))
    return {"sym": root / "symbols" / f"{LIB}.kicad_sym",
            "fp": root / "footprints" / f"{LIB}.pretty",
            "3d": root / "footprints" / f"{LIB}.3dshapes"}


def _require_easyeda() -> None:
    """easyeda2kicad opsiyonel ([lcsc] extra, AGPL). Kurulu değilse nasıl kurulacağını söyleyen
    bir ToolError fırlat; kuruluysa hiçbir şey yapma."""
    if importlib.util.find_spec("easyeda2kicad") is None:
        raise ToolError("import_lcsc_part needs easyeda2kicad (optional, AGPL-3.0, installed separately): "
                        "pip install 'pcbmcp[lcsc]'  or  uv sync --extra lcsc")


def _convert(lcsc: str, out: Path) -> None:
    """easyeda2kicad -> out/x.kicad_sym, out/x.pretty, out/x.3dshapes (ağ). Testlerde monkeypatch edilir."""
    _require_easyeda()
    proc = subprocess.run([sys.executable, "-m", "easyeda2kicad", "--full", f"--lcsc_id={lcsc}",
                           "--output", str(out / "x")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0 or not (out / "x.kicad_sym").is_file():
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise ToolError(f"{lcsc}: easyeda2kicad failed: {' | '.join(tail)}")


def _set_prop(sym: list, name: str, val: str) -> None:
    for p in children(sym, "property"):
        if p[1] == name:
            p[2] = val
            return
    S = sexpr.Sym  # düğüm adları çıplak sembol olmalı; str olursa ("property" ...) yazılır, KiCad açamaz
    sym.insert(sym.index(children(sym, "property")[-1]) + 1,
               [S("property"), name, val, [S("at"), 0, 0, 0],
                [S("effects"), [S("font"), [S("size"), 1.27, 1.27]], [S("hide"), S("yes")]]])


def compare_pads(a: dict[str, tuple[float, float]], b: dict[str, tuple[float, float]],
                 tol: float = 0.05) -> dict:
    """İki footprint'in pad numaralandırması aynı mı? Merkez hizalanır, 0/90/180/270 denenir.
    -> {'match': n, 'total': n, 'rotation': deg, 'differ': [ilk birkaç pad]}"""
    common = sorted(set(a) & set(b), key=library._natural)
    if not common:
        return {"match": 0, "total": 0, "rotation": 0, "differ": []}

    def centered(p):
        cx = sum(x for x, _ in p.values()) / len(p)
        cy = sum(y for _, y in p.values()) / len(p)
        return {k: (x - cx, y - cy) for k, (x, y) in p.items()}

    ca, cb = centered(a), centered(b)
    best = None
    for rot, (c, s) in {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}.items():
        differ = [k for k in common
                  if abs(ca[k][0] * c - ca[k][1] * s - cb[k][0]) > tol
                  or abs(ca[k][0] * s + ca[k][1] * c - cb[k][1]) > tol]
        if best is None or len(differ) < len(best[1]):
            best = (rot, differ)
    rot, differ = best
    return {"match": len(common) - len(differ), "total": len(common), "rotation": rot, "differ": differ[:6]}


def _kicad_twin(title: str, n_pads: int) -> str | None:
    """KiCad stok kütüphanesinde aynı parçanın footprint'i (MPN ile ara, pad sayısı tutmalı)."""
    mpn = re.split(r"[(_\s]", title)[0]
    if len(mpn) < 4:
        return None
    for fid in library.find_footprints(mpn, limit=5):
        if not fid.startswith(LIB + ":") and len(library.pad_positions(fid)) == n_pads:
            return fid
    return None


def import_part(lcsc: str, compare_footprint: str = "") -> dict:
    lcsc = lcsc.upper()
    dst = _paths()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        _convert(lcsc, t)
        cli("sym", "upgrade", "--force", str(t / "x.kicad_sym"))
        cli("fp", "upgrade", "--force", str(t / "x.pretty"))

        new = sexpr.parse((t / "x.kicad_sym").read_text(encoding="utf-8"))
        syms = children(new, "symbol")
        mods = sorted((t / "x.pretty").glob("*.kicad_mod"))
        if len(syms) != 1 or len(mods) != 1:
            raise ToolError(f"{lcsc}: expected 1 symbol + 1 footprint, got {len(syms)} + {len(mods)}")
        sym, mod = syms[0], mods[0]
        fp_name = mod.stem
        _set_prop(sym, "Footprint", f"{LIB}:{fp_name}")
        _set_prop(sym, "LCSC", lcsc)
        # EasyEDA pin tipleri hep "unspecified" -> ERC'de her bağlantıda pin_to_pin uyarısı.
        # KiCad'in genel tipi passive'e çevir (unspecified de zaten elektriksel kontrol yapmıyordu).
        for unit in children(sym, "symbol"):
            for pin in children(unit, "pin"):
                if pin[1] == "unspecified":
                    pin[1] = sexpr.Sym("passive")

        # sembolü kütüphaneye ekle (aynı adlı varsa değiştir)
        dst["sym"].parent.mkdir(parents=True, exist_ok=True)
        lib = sexpr.parse(dst["sym"].read_text(encoding="utf-8")) if dst["sym"].is_file() else \
            [c for c in new if not (isinstance(c, list) and c and c[0] == "symbol")]
        lib[:] = [c for c in lib if not (isinstance(c, list) and c[:2] == ["symbol", sym[1]])]
        lib.append(sym)
        dst["sym"].write_text(sexpr.dump(lib) + "\n", encoding="utf-8", newline="\n")

        # footprint + 3D model (model yolu mutlak: kütüphane nereden açılırsa açılsın bulunur)
        dst["fp"].mkdir(parents=True, exist_ok=True)
        dst["3d"].mkdir(parents=True, exist_ok=True)
        fp_tree = sexpr.parse(mod.read_text(encoding="utf-8"))
        for m in children(fp_tree, "model"):
            name = Path(str(m[1])).name
            for src in (t / "x.3dshapes").glob(Path(name).stem + ".*"):  # .wrl + .step
                shutil.copy2(src, dst["3d"] / src.name)
            m[1] = str(dst["3d"] / name)
        (dst["fp"] / mod.name).write_text(sexpr.dump(fp_tree) + "\n", encoding="utf-8", newline="\n")

    library._lib_tree.cache_clear()
    lib_id, fid = f"{LIB}:{sym[1]}", f"{LIB}:{fp_name}"
    desc = library.describe_symbol(lib_id)
    out = {
        "symbol": lib_id,
        "footprint": fid,
        "pins": desc["pins"] if len(desc["pins"]) <= 16 else f"{len(desc['pins'])} pins",
        "pad_check": library.check_pin_pad(lib_id, fid),
        "note": "EasyEDA conversion is not guaranteed; verify pinout vs datasheet",
    }
    pads = library.pad_positions(fid)
    twin = compare_footprint or _kicad_twin(str(sym[1]), len(pads))
    if twin:
        cmp = compare_pads(pads, library.pad_positions(twin))
        if cmp["match"] == cmp["total"] == len(pads):
            out["kicad_footprint"] = f"{twin}: same pad numbering (rot {cmp['rotation']})"
        else:
            out["kicad_footprint_mismatch"] = (
                f"{twin}: {cmp['total'] - cmp['match']}/{cmp['total']} pads at different positions "
                f"(e.g. {cmp['differ']}). Prefer the KiCad footprint; if the symbol came from EasyEDA "
                f"its pin numbers follow EasyEDA's pad numbering.")
    return out
