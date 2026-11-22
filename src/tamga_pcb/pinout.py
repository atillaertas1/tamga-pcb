"""Sembol pinout'u <-> gerçek LCSC parçasının pinout'u (EasyEDA sembol verisi).

Numara uyumu (check_pin_pad) anlamı garanti etmez: Q_NPN_CBE sembolü + BEC dizilimli
MMBT3904, ya da yanlış LCSC kodu DRC/ERC'den temiz geçer. Burada pin NUMARASI başına
sembol pin adı ile EasyEDA'daki pin adı karşılaştırılır.

İsimler kaynaklar arasında farklı yazılır (VO/VOUT, PC13/PC13-TAMPER-RTC, VSS/VSS_1),
bu yüzden eşleştirme sezgiseldir -> sonuç UYARI, hata değil.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .library import CACHE, _natural, resolve_symbol, symbol_pins

API = os.environ.get("TAMGA_EASYEDA_URL", "https://easyeda.com/api/products/{lcsc}/components")

# Aynı işlevin farklı yazılışları (normalize edilmiş, büyük harf)
ALIASES = [
    {"VO", "VOUT", "OUT", "OUTPUT"},
    {"VI", "VIN", "IN", "INPUT"},
    {"GND", "VSS", "AGND", "DGND", "PGND", "EP", "EPAD"},  # EP: exposed pad (çoğunlukla GND)
    {"VCC", "VDD", "V+"},
    {"VEE", "V-"},
    {"V-", "GND", "VSS"},  # tek beslemeli op-amp: LM358 V- = GND
    {"VS+", "V+"}, {"VS-", "V-"},  # MAX3232 şarj pompası
    {"A", "ANODE", "+"},  # LED'lerde EasyEDA bazen +/- yazar
    {"K", "C", "CATHODE", "-"},  # diyot katodu bazı kütüphanelerde "C"
    {"ADJ", "ADJ/GND", "GND/ADJ"},
]


def _fetch(lcsc: str) -> dict:
    """EasyEDA'dan ham bileşen JSON'u (ağ). Testlerde monkeypatch edilir.
    EasyEDA kısa sürede çok istekte 403/429 döner: geri çekilip tekrar dene."""
    try:
        for wait in (0, 2, 5):
            time.sleep(wait)
            r = httpx.get(API.format(lcsc=lcsc), params={"version": "6.4.19.5"},
                          headers={"User-Agent": "tamga-pcb"}, timeout=20)
            if r.status_code not in (403, 429):
                break
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ToolError(f"easyeda unavailable: {e}") from e
    if not data.get("success") or not data.get("result"):
        raise ToolError(f"{lcsc}: not found on easyeda")
    return data["result"]


def part_pins(lcsc: str) -> dict:
    """{'title': 'AMS1117-3.3', 'pins': {'1': 'GND', ...}} — diskte cache'li (parça verisi değişmez)."""
    lcsc = lcsc.upper()
    cache = CACHE / "easyeda" / f"{lcsc}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    res = _fetch(lcsc)
    pins = {}
    # Pin şekli: "P~show~elec~NUM~x~y~rot~id~lock ^^ ... ^^ 1~x~y~rot~NAME~..." (3. segment = ad)
    for s in res["dataStr"]["shape"]:
        if s.startswith("P~"):
            seg = s.split("^^")
            pins[seg[0].split("~")[3]] = seg[3].split("~")[4]
    out = {"title": res.get("title", ""), "pins": pins}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out), encoding="utf-8", newline="\n")
    return out


def _norm(name: str) -> str:
    # Aktif-düşük yazımları eşitle: KiCad ~{RE}, EasyEDA RE#, /RE, !RE -> RE ; boşluk at
    return re.sub(r"[~{}\s#!]|^/", "", name).upper()


def _tokens(name: str) -> set[str]:
    # PC13-TAMPER-RTC -> {PC13, TAMPER, RTC} ; VSS_1 -> {VSS} (salt sayı token'ları atılır)
    return {t for t in re.split(r"[-_/,.()]", name) if t and not t.isdigit()}


def same_pin(a: str, b: str) -> bool:
    """İki pin adı aynı işlevi mi gösteriyor? (sezgisel)"""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    if any(a in g and b in g for g in ALIASES):
        return True
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and (ta <= tb or tb <= ta):
        return True
    # op-amp: "+" / "-" <-> "IN+", "1IN-", "IN1+" ...
    return a in "+-" and len(a) == 1 and b.endswith(a) and "IN" in b or \
        b in "+-" and len(b) == 1 and a.endswith(b) and "IN" in a


def compare(sym: dict[str, str], part: dict[str, str]) -> dict:
    """sym/part: {pin_no: name}. Adsız / numarayla aynı adlı pinler (pasifler) atlanır."""
    mismatch = []
    for num in sorted(set(sym) & set(part), key=_natural):
        s, p = sym[num], part[num]
        if not s or s == num or not p or p == num:
            continue
        if not same_pin(s, p):
            mismatch.append(f"{num}: symbol {s}, part {p}")
    out = {}
    if mismatch:
        out["mismatch"] = mismatch
    if extra := [f"{n}:{part[n]}" for n in sorted(set(part) - set(sym), key=_natural)]:
        out["part_only"] = extra  # ör. SOT-223 tab'ı (4:VOUT) — footprint pad'i bağlanmalı mı?
    if missing := sorted(set(sym) - set(part), key=_natural):
        out["symbol_only"] = missing
    return out


def check(lib_id: str, lcsc: str) -> dict:
    """{'part': title, ...compare()} ; sorun yoksa sadece part + 'pinout': 'OK'."""
    pp = part_pins(lcsc)
    sym = {}
    for p in symbol_pins(resolve_symbol(lib_id)):
        if p["number"]:
            sym.setdefault(p["number"], p["name"])
    res = compare(sym, pp["pins"])
    return {"part": pp["title"], **(res or {"pinout": "OK"})}
