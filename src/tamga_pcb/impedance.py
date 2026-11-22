"""Kontrollü empedans için iz genişliği: IPC-2141 mikroşerit (dış katman, altta referans düzlemi).

Dış katmandaki izin referansı: 2 katmanda karşı yüzdeki GND dökümü (çekirdek kalınlığı uzakta),
4/6 katmanda hemen alttaki iç düzlem (ince prepreg). Lehim maskesi ihmal edilir; sonuç ±%10 civarı,
üretim için üreticinin empedans hesaplayıcısıyla doğrulanmalı.
"""
from __future__ import annotations

import math

# (h: referans düzlemine dielektrik kalınlığı mm, er, açıklama) — JLCPCB standart 1.6 mm katman yapıları
STACKUPS = {
    2: (1.53, 4.5, "JLCPCB 2-layer 1.6 mm FR-4, reference plane on the far side"),
    4: (0.2104, 4.4, "JLCPCB 4-layer JLC04161H-7628, outer layer over In1"),
}
COPPER_MM = 0.035  # 1 oz


def microstrip_z0(w: float, h: float, er: float, t: float = COPPER_MM) -> float:
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def diff_z(w: float, s: float, h: float, er: float, t: float = COPPER_MM) -> float:
    """Kenardan kuplajlı diferansiyel mikroşerit (IPC-2141A)."""
    return 2 * microstrip_z0(w, h, er, t) * (1 - 0.48 * math.exp(-0.96 * s / h))


def _solve(f, target: float, lo: float = 0.03, hi: float = 8.0) -> float:
    """f(w) w büyüdükçe azalır; f(w) = target için ikiye bölme."""
    if f(lo) < target:
        return lo
    if f(hi) > target:
        return hi
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) > target else (lo, mid)
    return (lo + hi) / 2


def track_for(impedance_ohm: float, layers: int, diff_gap_mm: float | None = None,
              h_mm: float | None = None, er: float | None = None) -> dict:
    """Hedef empedans için iz genişliği (ve diferansiyelse verilen aralıkla)."""
    if (h_mm is None or er is None) and layers not in STACKUPS:
        raise ValueError(f"no default stackup for {layers} layers; give stackup_h_mm and er")
    h0, er0, desc = STACKUPS.get(layers, (None, None, "custom"))
    h, e = h_mm or h0, er or er0
    if diff_gap_mm:
        w = _solve(lambda x: diff_z(x, diff_gap_mm, h, e), impedance_ohm)
        got = diff_z(w, diff_gap_mm, h, e)
    else:
        w = _solve(lambda x: microstrip_z0(x, h, e), impedance_ohm)
        got = microstrip_z0(w, h, e)
    out = {"track_mm": round(w, 3), "impedance_ohm": round(got, 1),
           "model": f"IPC-2141 microstrip, h={h} mm, er={e} ({desc if h_mm is None else 'custom'})",
           "note": "about ±10 %, solder mask ignored; confirm with the fab's impedance calculator"}
    if diff_gap_mm:
        out["gap_mm"] = diff_gap_mm
    if w > 1.0:
        out["warning"] = (f"{w:.2f} mm is impractical: the reference plane is {h} mm away. "
                          "Use 4 layers (set board.layers=4), or accept uncontrolled impedance for short runs.")
    return out
