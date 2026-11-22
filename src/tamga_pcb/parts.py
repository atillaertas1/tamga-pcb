"""JLCPCB/LCSC parça arama — tscircuit'in key istemeyen public jlcsearch API'si.

Çıktı bilinçli olarak dar: LLM'e her parça için tek satır gider.
"""
from __future__ import annotations

import os
import re

import httpx
from mcp.server.mcpserver.exceptions import ToolError

API = os.environ.get("TAMGA_JLCSEARCH_URL", "https://jlcsearch.tscircuit.com")


def _unit_price(price) -> float | None:
    """'1-49:0.2003,50-149:0.1553' -> 0.2003 ; sayıysa aynen."""
    if isinstance(price, (int, float)):
        return float(price)
    if isinstance(price, str):
        m = re.search(r":([\d.]+)", price)
        return float(m.group(1)) if m else None
    return None


def _short_desc(desc: str, n: int = 90) -> str:
    desc = re.sub(r"\s*ROHS\s*$", "", desc or "").strip()
    return desc if len(desc) <= n else desc[: n - 1] + "…"


_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "": 1.0, "r": 1.0, "R": 1.0}
_RES = re.compile(r"^(?:(?:resistor|res|r)\s+)?(\d+(?:\.\d+)?)\s*([kKmMrR]?)(\d*)\s*(?:ohm|Ω)?(?:\s+\d+(?:\.\d+)?%)?$")
# 'm' tek başına megaohm ('1M') ile karışır: mili-farad yalnız 'mF' ile
_CAP = re.compile(r"^(?:(?:capacitor|cap|c)\s+)?(\d+(?:\.\d+)?)\s*([pnuµ](?=F?(?:\s|$))|m(?=F))F?(?:\s+\d+V)?$", re.I)


def value_query(query: str) -> tuple[str, float] | None:
    """'5.1k', 'resistor 10k', '4k7', '100R' -> ('resistors', ohm); '100nF', 'capacitor 10uF' -> ('capacitors', F).
    jlcsearch'ün metin araması değerleri bulanık eşler ('1k' -> 68.1k); değer uç noktası tam eşler."""
    q = query.strip()
    if m := _CAP.match(q):
        unit = m.group(2) if m.group(2) in "µ" else m.group(2).lower()
        return "capacitors", float(m.group(1)) * _MULT[unit]
    if m := _RES.match(q):
        unit = m.group(2)
        unit = "M" if unit == "M" else unit.lower()
        whole = m.group(1) + (("." + m.group(3)) if m.group(3) else "")  # 4k7 -> 4.7k
        return "resistors", float(whole) * _MULT[unit]
    return None


def _get(c: httpx.Client, path: str, params: dict, query: str) -> dict:
    r = c.get(f"{API}/{path}", params=params)
    if r.status_code == 400:  # sorgu reddi (ör. tek başına "5.1k"): API'nin önerisini modele ilet
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise ToolError(f"search rejected for {query!r}: {msg[:300]} "
                        "(add a part type, e.g. 'resistor 5.1k')")
    r.raise_for_status()
    return r.json()


def search(query: str, package: str | None = None, basic_only: bool = False,
           min_stock: int = 100, limit: int = 8, client: httpx.Client | None = None) -> dict:
    vq = value_query(query)
    c = client or httpx.Client(timeout=20)
    try:
        if vq:
            kind, val = vq
            params = {"package": package} if package else {}
            params["resistance" if kind == "resistors" else "capacitance"] = f"{val:g}"
            comps = _get(c, f"{kind}/list.json", params, query).get(kind, [])
        else:
            params = {"search": query, "limit": max(limit * 4, 20)}
            if package:
                params["package"] = package
            comps = _get(c, "components/list.json", params, query).get("components", [])
    except (httpx.HTTPError, ValueError) as e:
        raise ToolError(f"jlcsearch unavailable: {e}") from e
    finally:
        if client is None:
            c.close()

    rows = []
    for p in comps:
        if package and package.lower() not in str(p.get("package", "")).lower():
            continue
        if basic_only and not p.get("is_basic"):
            continue
        # jlcsearch basic parçalarda stoğu boş (None) döndürüyor: bilinmeyen stok elenmez
        if p.get("stock") is not None and p["stock"] < min_stock:
            continue
        rows.append(p)
    # Basic parça (JLC'de montaj ücreti yok) önce, sonra stok
    rows.sort(key=lambda p: (not p.get("is_basic"), not p.get("is_preferred"), -(p.get("stock") or 0)))

    lines = []
    for p in rows[:limit]:
        tag = "B" if p.get("is_basic") else ("P" if p.get("is_preferred") else "E")
        price = _unit_price(p.get("price", p.get("price1")))
        stock = p.get("stock")
        lines.append(
            f"C{p['lcsc']} [{tag}] {p.get('mfr', '?')} | {p.get('package', '?')} | "
            f"stock {stock if stock is not None else '?'} | ${price if price is not None else '?'} | "
            f"{_short_desc(p.get('description', ''))}"
        )
    out = {
        "query": query,
        "legend": "B=basic (no setup fee), P=preferred, E=extended (+$3 setup)",
        "parts": lines,
        "shown": len(lines),
        "matched": len(rows),
    }
    if not lines:
        out["hint"] = ("no match in jlcsearch (its catalogue misses some parts, e.g. CH340C). Search a known LCSC "
                       "code directly ('C84681'), a shorter part name, or a part type + value.")
    return out
