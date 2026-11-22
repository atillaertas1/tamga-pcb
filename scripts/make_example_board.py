"""Test kartı üret: AMS1117 3.3V LDO + güç LED'i, 30x20 mm, 2 katman, rotasız.

Çalıştır (KiCad'in Python'u ile; Linux'ta /usr/bin/python3, bkz. `tamga doctor`):
  <kicad-python> scripts/make_example_board.py examples/ldo/ldo.kicad_pcb
"""
import os
import sys

import pcbnew



def _stock_footprints() -> str:
    """KiCad Python'unun yanındaki stok footprint dizini (Windows/macOS paketleri) ya da Linux yolu."""
    if os.environ.get("TAMGA_FOOTPRINT_DIR"):
        return os.environ["TAMGA_FOOTPRINT_DIR"]
    exe = os.path.realpath(sys.executable)
    for rel in (("..", "share", "kicad", "footprints"),                       # Windows: KiCad/<v>/bin
                ("..", "..", "..", "..", "..", "SharedSupport", "footprints")):  # macOS: KiCad.app
        cand = os.path.normpath(os.path.join(os.path.dirname(exe), *rel))
        if os.path.isdir(cand):
            return cand
    return "/usr/share/kicad/footprints"


FP = _stock_footprints()
out = sys.argv[1] if len(sys.argv) > 1 else "examples/ldo/ldo.kicad_pcb"
os.makedirs(os.path.dirname(out), exist_ok=True)

b = pcbnew.NewBoard(out)
mm = pcbnew.FromMM

# Kart sınırı 30x20 mm, (100,100) mm köşesinden
x0, y0, w, h = 100, 100, 30, 20
corners = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(pcbnew.VECTOR2I(mm(ax), mm(ay)))
    s.SetEnd(pcbnew.VECTOR2I(mm(bx), mm(by)))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(mm(0.1))
    b.Add(s)

nets = {}
for name in ["VIN", "GND", "+3V3", "LED_A"]:
    n = pcbnew.NETINFO_ITEM(b, name)
    b.Add(n)
    nets[name] = n

# (ref, value, lib, footprint, (x,y), {pad: net})
parts = [
    ("J1", "VIN", "Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical",
     (104, 108), {"1": "VIN", "2": "GND"}),
    ("C1", "10u", "Capacitor_SMD", "C_0805_2012Metric", (110, 104), {"1": "VIN", "2": "GND"}),
    ("U1", "AMS1117-3.3", "Package_TO_SOT_SMD", "SOT-223-3_TabPin2",
     (116, 110), {"1": "GND", "2": "+3V3", "3": "VIN"}),
    ("C2", "22u", "Capacitor_SMD", "C_0805_2012Metric", (122, 104), {"1": "+3V3", "2": "GND"}),
    ("R1", "1k", "Resistor_SMD", "R_0805_2012Metric", (122, 116), {"1": "+3V3", "2": "LED_A"}),
    ("D1", "GREEN", "LED_SMD", "LED_0805_2012Metric", (116, 116), {"1": "GND", "2": "LED_A"}),
    ("J2", "3V3", "Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical",
     (127, 108), {"1": "+3V3", "2": "GND"}),
]
for ref, val, lib, name, (x, y), padnets in parts:
    fp = pcbnew.FootprintLoad(f"{FP}/{lib}.pretty", name)
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    fp.SetReference(ref)
    fp.SetValue(val)
    fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    b.Add(fp)
    for pad in fp.Pads():
        net = padnets.get(pad.GetNumber())
        if net:
            pad.SetNet(nets[net])

# JLCPCB 2 katman standart kuralları
ds = b.GetDesignSettings()
ds.m_TrackMinWidth = mm(0.127)
ds.m_MinClearance = mm(0.127)
ds.m_ViasMinSize = mm(0.45)
ds.m_MinThroughDrill = mm(0.2)

pcbnew.SaveBoard(out, b)
print("wrote", out)
