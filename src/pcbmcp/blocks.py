"""Devre blokları: spec'te bir kez tanımla, birden çok kez kullan.

spec:
  blocks:    {name: {ports: [..], parts: [part, ...]}}      # part = normal spec parçası
  instances: [{block, name, nets?: {port: net}, ref_base?: int, refs?: {local: global}, net_prefix?,
               set?: {local_ref: {value?, lcsc?, footprint?, dnp?}}}]

Açma (expand) kuralları:
  - Port neti: instance.nets[port], verilmemişse port adıyla aynı global net (GND, +3V3...).
    nets[port] = null -> o porta giden pinler bağlanmaz ve nc'ye eklenir (kullanılmayan kanal).
  - Port olmayan net yerel: "<instance>_<net>" (COM5_C1P) -> instance'lar birbirine karışmaz.
    ("/" değil: KiCad net adında "/"yi "{slash}" diye kaçırıyor.) Yerel ad global bir netle
    ya da başka instance'ın yerel netiyle çakışırsa hata (sessizce birleşmesin).
    net_prefix öneki değiştirir: mevcut tasarımı taşırken eski adları korumak için önemli —
    .kicad_dru kuralları ve netclass desenleri net ADINA bakar (ör. 'IC?P' izolasyon kuralı).
  - Ref: refs[local] varsa o; yoksa ref_base + yereldeki numara (U1 -> U101).
    PCB yerleşimi ref'e bağlı: ref'ler bloğa parça eklenince KAYMAMALI -> sıralı numara yok.
  - Açılan parçada "instance" alanı kalır (hiyerarşik sheet için).
Diske kaydedilen spec bloklu kalır; açma sync sırasında bellekte yapılır.
"""
from __future__ import annotations

import copy
import re

from mcp.server.mcpserver.exceptions import ToolError

_REF = re.compile(r"^([A-Za-z_#]+)(\d+)$")
_SETTABLE = {"value", "lcsc", "footprint", "dnp", "symbol", "nc"}


def _ref(local: str, inst: dict, base: int) -> str:
    if local in inst.get("refs", {}):
        return inst["refs"][local]
    m = _REF.match(local)
    if not m:
        raise ToolError(f"block ref '{local}' must look like U1/C12 (or map it in instance.refs)")
    n = int(m.group(2))
    if n >= 100:
        raise ToolError(f"block ref '{local}': local numbers must be < 100 (ref_base adds 100s)")
    return f"{m.group(1)}{base + n}"


def expand(spec: dict) -> dict:
    """Bloklu spec -> düz spec (parts). Blok yoksa aynen döner."""
    insts = spec.get("instances") or []
    if not insts:
        return spec
    blocks = spec.get("blocks") or {}
    flat = {k: v for k, v in spec.items() if k not in ("blocks", "instances")}
    flat["parts"] = list(copy.deepcopy(spec.get("parts") or []))
    names = set()
    local_nets: dict[str, str] = {}  # yerel net -> instance (çakışma kontrolü)
    for i, inst in enumerate(insts):
        name, bname = inst.get("name"), inst.get("block")
        if not name or bname not in blocks:
            raise ToolError(f"instance {inst}: needs 'name' and a 'block' in {sorted(blocks)}")
        if name in names:
            raise ToolError(f"duplicate instance name {name}")
        names.add(name)
        block = blocks[bname]
        ports = set(block.get("ports", []))
        bad = set(inst.get("nets", {})) - ports
        if bad:
            raise ToolError(f"{name}: {sorted(bad)} are not ports of block '{bname}' (ports: {sorted(ports)})")
        local_refs = {p["ref"] for p in block.get("parts", [])}
        for key in ("set", "refs"):
            if unknown := set(inst.get(key, {})) - local_refs:
                raise ToolError(f"{name}.{key}: no part {sorted(unknown)} in block '{bname}'")
        base = int(inst.get("ref_base", 100 * (i + 1)))

        prefix = inst.get("net_prefix", f"{name}_")

        def net(n: str) -> str:
            if n in ports:
                return inst.get("nets", {}).get(n, n)
            full = f"{prefix}{n}"
            if local_nets.setdefault(full, name) != name:
                raise ToolError(f"local net {full} of {name} clashes with {local_nets[full]}; "
                                f"give the instances different net_prefix")
            return full

        for bp in block.get("parts", []):
            p = copy.deepcopy(bp)
            p["ref"] = _ref(bp["ref"], inst, base)
            pins = {pin: net(str(n)) for pin, n in (bp.get("pins") or {}).items()}
            unused = [pin for pin, n in pins.items() if n is None]  # nets[port] = null
            p["pins"] = {pin: n for pin, n in pins.items() if n is not None}
            if unused and p.get("nc") != "*":
                p["nc"] = [*(p.get("nc") or []), *unused]
            over = inst.get("set", {}).get(bp["ref"], {})
            if bad := set(over) - _SETTABLE:
                raise ToolError(f"{name}.set.{bp['ref']}: can't set {sorted(bad)} (allowed: {sorted(_SETTABLE)})")
            p.update(copy.deepcopy(over))
            p["instance"] = name
            flat["parts"].append(p)
    # yerel net adı, blok dışından gelen bir global netle aynıysa iki ayrı net sessizce birleşir
    global_nets = {str(n) for p in flat["parts"] if p.get("instance") is None for n in (p.get("pins") or {}).values()}
    for i in insts:  # eşlenen portlar + eşlenmeyip aynı adla global kalan portlar
        global_nets |= {n for n in i.get("nets", {}).values() if n is not None}
        global_nets |= set(blocks[i["block"]].get("ports", [])) - set(i.get("nets", {}))
    if clash := sorted(set(local_nets) & global_nets):
        raise ToolError(f"local block nets clash with global nets {clash}; rename the global net "
                        f"or the block's internal net")
    return flat


def summary(spec: dict, flat: dict) -> list[str]:
    """'COM5(rs232): U101 C101-C104' — kompakt rapor."""
    out = []
    for inst in spec.get("instances") or []:
        refs = [p["ref"] for p in flat["parts"] if p.get("instance") == inst["name"]]
        out.append(f"{inst['name']}({inst['block']}): {' '.join(refs)}")
    return out
