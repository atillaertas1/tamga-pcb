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


def search(query: str, package: str | None = None, basic_only: bool = False,
           min_stock: int = 100, limit: int = 8, client: httpx.Client | None = None) -> dict:
    params = {"search": query, "limit": max(limit * 4, 20)}
    if package:
        params["package"] = package
    c = client or httpx.Client(timeout=20)
    try:
        r = c.get(f"{API}/components/list.json", params=params)
        if r.status_code == 400:  # sorgu reddi (ör. tek başına "5.1k"): API'nin önerisini modele ilet
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise ToolError(f"search rejected for {query!r}: {msg[:300]} "
                            "(add a part type, e.g. 'resistor 5.1k')")
        r.raise_for_status()
        comps = r.json().get("components", [])
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
        if (p.get("stock") or 0) < min_stock:
            continue
        rows.append(p)
    # Basic parça (JLC'de montaj ücreti yok) önce, sonra stok
    rows.sort(key=lambda p: (not p.get("is_basic"), not p.get("is_preferred"), -(p.get("stock") or 0)))

    lines = []
    for p in rows[:limit]:
        tag = "B" if p.get("is_basic") else ("P" if p.get("is_preferred") else "E")
        price = _unit_price(p.get("price"))
        lines.append(
            f"C{p['lcsc']} [{tag}] {p.get('mfr', '?')} | {p.get('package', '?')} | "
            f"stock {p.get('stock', 0)} | ${price if price is not None else '?'} | "
            f"{_short_desc(p.get('description', ''))}"
        )
    return {
        "query": query,
        "legend": "B=basic (no setup fee), P=preferred, E=extended (+$3 setup)",
        "parts": lines,
        "shown": len(lines),
        "matched": len(rows),
    }
