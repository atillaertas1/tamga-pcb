"""DRC/ERC JSON raporlarını LLM için sıkıştır.

Ham rapor yüzlerce ihlal × UUID × açıklama içerir. LLM'in ihtiyacı:
  1) türe göre kaç tane var, 2) her türden birkaç örnek konum, 3) hangi netler etkileniyor.
"""
from __future__ import annotations

import re
from collections import defaultdict

NET_RE = re.compile(r"\[([^\]]+)\]")


def _item_short(it: dict) -> str:
    """'Pad 1 [VIN] of C1 on F.Cu' + konum -> 'Pad 1 [VIN] of C1 on F.Cu @(109.05,104.0)'"""
    p = it.get("pos") or {}
    loc = f" @({p.get('x')},{p.get('y')})" if p else ""
    return f"{it.get('description', '?')}{loc}"


def _violations(report: dict) -> list[dict]:
    """DRC'de üst seviye, ERC'de (KiCad 9+) sheets[].violations altında."""
    out = list(report.get("violations", []))
    out += report.get("unconnected_items", [])
    out += report.get("schematic_parity", [])
    for sheet in report.get("sheets", []):
        for v in sheet.get("violations", []):
            v = dict(v)
            v.setdefault("sheet", sheet.get("path", ""))
            out.append(v)
    return out


def summarize(report: dict, max_examples: int = 3, max_types: int = 15) -> dict:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for v in _violations(report):
        groups[(v.get("severity", "?"), v.get("type", "?"))].append(v)

    sev_rank = {"error": 0, "warning": 1}
    ordered = sorted(groups.items(), key=lambda kv: (sev_rank.get(kv[0][0], 9), -len(kv[1])))

    types = []
    for (sev, typ), vs in ordered[:max_types]:
        nets: set[str] = set()
        for v in vs:
            for it in v.get("items", []):
                nets.update(NET_RE.findall(it.get("description", "")))
        types.append({
            "type": typ,
            "severity": sev,
            "count": len(vs),
            "what": vs[0].get("description", ""),
            "nets": sorted(nets)[:10],
            "examples": [" <-> ".join(_item_short(i) for i in v.get("items", [])[:2])
                         for v in vs[:max_examples]],
        })

    total = sum(len(v) for v in groups.values())
    errors = sum(len(v) for (s, _), v in groups.items() if s == "error")
    out = {"total": total, "errors": errors, "warnings": total - errors, "types": types}
    if len(ordered) > max_types:
        out["more_types"] = len(ordered) - max_types
    if total == 0:
        out["verdict"] = "CLEAN"
    return out
