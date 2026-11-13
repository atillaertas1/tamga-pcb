"""KiCad sembol/footprint kütüphanelerinde arama ve sembol çözümleme.

- Arama: 222 MB'lık sembol kütüphanesini her seferinde ayrıştırmak yerine regex ile
  çıkarılan küçük bir indeks (~/.cache/pcbmcp) kullanılır; kütüphane mtime'ı değişince yenilenir.
- Çözümleme: `extends` ile türetilmiş semboller ebeveynle düzleştirilir, çünkü şematiğe
  gömülen sembol tam gövdeyi içermelidir.
"""
from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
from functools import lru_cache
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import sexpr
from .sexpr import Sym, child, children, value

SYM_DIR = Path(os.environ.get("PCBMCP_SYMBOL_DIR", "/usr/share/kicad/symbols"))
FP_DIR = Path(os.environ.get("PCBMCP_FOOTPRINT_DIR", "/usr/share/kicad/footprints"))
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pcbmcp"

_TOP_SYM = re.compile(r'^\t\(symbol "([^"]+)"', re.M)
_PROP = re.compile(r'^\t\t\(property "(Description|ki_keywords|ki_fp_filters|Footprint)" "([^"]*)"', re.M)


# ------------------------------------------------------------------ indeks

def _index_lib(path: Path) -> list[list[str]]:
    """[[ad, açıklama, anahtar kelimeler, footprint], ...] — tam ayrıştırma yok."""
    text = path.read_text(encoding="utf-8")
    starts = [(m.start(), m.group(1)) for m in _TOP_SYM.finditer(text)]
    out = []
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        props = dict(_PROP.findall(text, pos, end))
        out.append([name, props.get("Description", ""), props.get("ki_keywords", ""),
                    props.get("Footprint", "")])
    return out


@lru_cache(maxsize=1)
def symbol_index() -> dict[str, list[list[str]]]:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / "symbol_index.json"
    stamp = {p.name: p.stat().st_mtime for p in sorted(SYM_DIR.glob("*.kicad_sym"))}
    if cache_file.is_file():
        data = json.loads(cache_file.read_text())
        if data.get("stamp") == stamp:
            return data["libs"]
    libs = {p.stem: _index_lib(p) for p in sorted(SYM_DIR.glob("*.kicad_sym"))}
    cache_file.write_text(json.dumps({"stamp": stamp, "libs": libs}))
    return libs


def _score(tokens: list[str], *fields: str) -> int:
    hay = [f.lower() for f in fields]
    if not all(any(t in h for h in hay) for t in tokens):
        return 0
    name = hay[0]
    return sum(3 if t == name.split(":")[-1] else 2 if t in name else 1 for t in tokens)


def find_symbols(query: str, limit: int = 10) -> list[str]:
    tokens = query.lower().split()
    hits = []
    for lib, syms in symbol_index().items():
        for name, desc, kw, fp in syms:
            s = _score(tokens, f"{lib}:{name}", desc, kw)
            if s:
                hits.append((-s, len(name), f"{lib}:{name}", desc, fp))
    hits.sort()
    return [f"{lid} | {d[:70]}" + (f" | fp={fp}" if fp else "") for _, _, lid, d, fp in hits[:limit]]


@lru_cache(maxsize=1)
def footprint_index() -> list[str]:
    return sorted(f"{d.stem}:{f.stem}" for d in FP_DIR.glob("*.pretty") for f in d.glob("*.kicad_mod"))


def find_footprints(query: str, pattern: str = "", limit: int = 15) -> list[str]:
    """query: kelimeler (hepsi geçmeli); pattern: KiCad ki_fp_filters tarzı joker."""
    tokens = query.lower().split()
    pats = [p for p in pattern.split() if p]
    out = []
    for fid in footprint_index():
        low = fid.lower()
        words = re.split(r"[_:\-.]", low)
        # kısa jetonlar (r, c, 2) kelime başında eşleşmeli, yoksa her şeye uyar
        if tokens and not all((t in low) if len(t) > 2 else any(w.startswith(t) for w in words)
                              for t in tokens):
            continue
        if pats and not any(fnmatch.fnmatch(fid.split(":")[1], p.split(":")[-1]) for p in pats):
            continue
        out.append(fid)
    out.sort(key=len)
    return out[:limit]


def footprint_file(fid: str) -> Path:
    lib, name = fid.split(":", 1)
    p = FP_DIR / f"{lib}.pretty" / f"{name}.kicad_mod"
    if not p.is_file():
        raise ToolError(f"footprint {fid} not found; use find_footprint")
    return p


# ------------------------------------------------------------------ sembol çözümleme

@lru_cache(maxsize=64)
def _lib_tree(lib: str):
    p = SYM_DIR / f"{lib}.kicad_sym"
    if not p.is_file():
        raise ToolError(f"symbol library '{lib}' not found")
    return sexpr.parse(p.read_text(encoding="utf-8"))


def _raw_symbol(lib: str, name: str):
    for s in children(_lib_tree(lib), "symbol"):
        if s[1] == name:
            return s
    raise ToolError(f"symbol {lib}:{name} not found; use find_symbol")


def resolve_symbol(lib_id: str) -> list:
    """Şematiğe gömülecek, düzleştirilmiş sembol: adı 'Lib:Name', alt birimler 'Name_U_S'."""
    lib, name = lib_id.split(":", 1)
    sym = copy.deepcopy(_raw_symbol(lib, name))
    parent_name = value(sym, "extends")
    if parent_name:
        parent = copy.deepcopy(resolve_symbol(f"{lib}:{parent_name}"))
        own_props = {p[1]: p for p in children(sym, "property")}
        merged = [Sym("symbol"), lib_id]
        for c in parent[2:]:
            h = sexpr.head(c)
            if h == "property" and c[1] in own_props:
                merged.append(own_props.pop(c[1]))
            elif h == "symbol":  # alt birim: 'Parent_1_1' -> 'Name_1_1'
                c[1] = name + c[1][len(parent_name):]
                merged.append(c)
            else:
                merged.append(c)
        merged.extend(own_props.values())
        return merged
    sym[1] = lib_id
    return sym


def symbol_pins(sym: list) -> list[dict]:
    """Tüm pinler: number, name, type, unit, (x, y) bağlantı noktası, açı, gizli mi."""
    base = str(sym[1]).split(":", 1)[1]
    pins = []
    for sub in children(sym, "symbol"):
        m = re.match(re.escape(base) + r"_(\d+)_(\d+)$", sub[1])
        if not m:
            continue
        unit, style = int(m.group(1)), int(m.group(2))
        if style > 1:  # De Morgan alternatif gövdeyi atla
            continue
        for p in children(sub, "pin"):
            at = child(p, "at")
            pins.append({
                "number": str(value(p, "number", "")),
                "name": str(value(p, "name", "")),
                "type": str(p[1]),
                "unit": unit,
                "x": float(at[1]), "y": float(at[2]), "angle": float(at[3]) if len(at) > 3 else 0.0,
                "hidden": any(c == Sym("hide") or (isinstance(c, list) and c[:2] == ["hide", Sym("yes")])
                              for c in p),
            })
    return pins


def unit_count(sym: list) -> int:
    base = str(sym[1]).split(":", 1)[1]
    units = {int(m.group(1)) for sub in children(sym, "symbol")
             if (m := re.match(re.escape(base) + r"_(\d+)_\d+$", sub[1]))}
    return max(units | {1})


def body_points(sym: list, unit: int) -> list[tuple[float, float]]:
    """Gövde grafiği ve pin uçlarından sınır kutusu için noktalar (sembol koordinatı, Y yukarı)."""
    base = str(sym[1]).split(":", 1)[1]
    pts: list[tuple[float, float]] = []

    def walk(n):
        for c in n:
            if not isinstance(c, list) or not c:
                continue
            h = c[0]
            if h in ("xy", "start", "end", "mid") and len(c) >= 3:
                pts.append((float(c[1]), float(c[2])))
            elif h == "center" and len(c) >= 3:
                r = value(n, "radius", 0) or 0
                pts.extend([(c[1] - r, c[2] - r), (c[1] + r, c[2] + r)])
            elif h == "pin":
                at = child(c, "at")
                pts.append((float(at[1]), float(at[2])))
            elif h not in ("property", "effects", "font"):
                walk(c)

    for sub in children(sym, "symbol"):
        m = re.match(re.escape(base) + r"_(\d+)_(\d+)$", sub[1])
        if m and int(m.group(1)) in (0, unit) and int(m.group(2)) <= 1:
            walk(sub)
    return pts or [(0.0, 0.0)]


def describe_symbol(lib_id: str) -> dict:
    sym = resolve_symbol(lib_id)
    props = {p[1]: p[2] for p in children(sym, "property")}
    pins = symbol_pins(sym)
    n_units = unit_count(sym)
    seen = set()
    lines = []
    for p in sorted(pins, key=lambda p: (p["unit"], _natural(p["number"]))):
        key = (p["number"], p["name"], p["unit"])
        if key in seen:
            continue
        seen.add(key)
        u = f" u{p['unit']}" if n_units > 1 else ""
        lines.append(f"{p['number']}:{p['name']}:{p['type']}{u}{' hidden' if p['hidden'] else ''}")
    out = {
        "lib_id": lib_id,
        "description": props.get("Description", "")[:120],
        "units": n_units,
        "pins": lines,
        "fp_filters": props.get("ki_fp_filters", ""),
    }
    if props.get("Footprint"):
        out["default_footprint"] = props["Footprint"]
    return out


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def footprint_pads(fid: str) -> list[str]:
    tree = sexpr.parse(footprint_file(fid).read_text(encoding="utf-8"))
    return sorted({str(p[1]) for p in children(tree, "pad") if str(p[1])}, key=_natural)
