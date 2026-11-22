"""Build the ESP32-C3 example from its spec with the same tools the agent calls, then render the
README images. Run from the repo root:  uv run python examples/esp32c3-devboard/build.py

Steps: sync_design (schematic + PCB from esp32c3.circuit.json) -> place_footprints -> set_netclass
-> autoroute (GND left to the pour) -> add_copper_zone -> run_drc -> export_fab -> images.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from tamga_pcb import server as s
from tamga_pcb.paths import KICAD_CLI

HERE = Path(__file__).resolve().parent
NAME = "esp32c3"
PCB = HERE / f"{NAME}.kicad_pcb"
IMG = HERE.parents[1] / "assets" / "examples"

# Board is 60 x 32 mm with its top-left corner at (100, 100).
PLACEMENT = [
    # USB-C on the left edge, opening outwards; CC resistors right behind it
    {"ref": "J1", "x": 104.0, "y": 116.0, "rot": 270},
    {"ref": "R1", "x": 111.4, "y": 111.6, "rot": 90},
    {"ref": "R2", "x": 111.4, "y": 120.4, "rot": 90},
    # 3.3 V LDO with its input and output capacitors, power LED
    {"ref": "U1", "x": 118.0, "y": 106.5, "rot": 0},
    {"ref": "C1", "x": 112.6, "y": 107.0, "rot": 90},
    {"ref": "C2", "x": 124.6, "y": 104.6, "rot": 90},
    {"ref": "R3", "x": 128.0, "y": 102.6, "rot": 0},
    {"ref": "D1", "x": 131.2, "y": 102.6, "rot": 180},
    # ESP32-C3 module on the right, antenna flush with the right edge
    {"ref": "U2", "x": 146.5, "y": 117.0, "rot": 270},
    {"ref": "R7", "x": 136.0, "y": 102.6, "rot": 0},
    {"ref": "D2", "x": 139.2, "y": 102.6, "rot": 180},
    {"ref": "C4", "x": 143.2, "y": 103.0, "rot": 270},
    {"ref": "R4", "x": 147.2, "y": 102.6, "rot": 0},
    {"ref": "C3", "x": 151.4, "y": 103.4, "rot": 270},
    {"ref": "R5", "x": 134.2, "y": 106.4, "rot": 90},
    {"ref": "R6", "x": 149.5, "y": 129.6, "rot": 0},
    # Buttons in the middle, GPIO header along the bottom edge
    {"ref": "SW1", "x": 117.0, "y": 123.0, "rot": 0},
    {"ref": "SW2", "x": 128.0, "y": 123.0, "rot": 0},
    {"ref": "J2", "x": 109.0, "y": 129.4, "rot": 90},
]


def step(title: str, result: dict, keys: tuple[str, ...] = ()) -> dict:
    shown = {k: result.get(k) for k in keys} if keys else result
    print(f"{title}: {json.dumps(shown, ensure_ascii=False)[:400]}")
    return result


def kicad(*args: str) -> None:
    subprocess.run([KICAD_CLI, *args], check=True, capture_output=True)


def copper_view() -> None:
    """Routed tracks before the GND pour (the pour would hide them): red = top, blue = bottom."""
    IMG.mkdir(parents=True, exist_ok=True)
    kicad("pcb", "export", "svg", "--layers", "F.Cu,B.Cu,Edge.Cuts", "--mode-single", "--fit-page-to-board",
          "--exclude-drawing-sheet", "-o", str(IMG / f"{NAME}-routed.svg"), str(PCB))


def images() -> None:
    # KiCad ships no 3D body for the HRO TYPE-C-31-M-12; the GCT USB4105 body has the same locating
    # holes and outline, so the render borrows it. Cosmetic only, after DRC and fab export.
    text = PCB.read_text(encoding="utf-8")
    PCB.write_text(text.replace("USB_C_Receptacle_HRO_TYPE-C-31-M-12.step",
                                "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.step"),
                   encoding="utf-8", newline="\n")
    kicad("pcb", "render", "--quality", "high", "--background", "transparent", "-w", "1800", "-h", "1100",
          "--perspective", "--rotate", "-38,0,-32", "--zoom", "0.95",
          "-o", str(IMG / f"{NAME}-3d.png"), str(PCB))
    sch = IMG / f"{NAME}-schematic"
    kicad("sch", "export", "svg", "--exclude-drawing-sheet", "-o", str(sch), str(HERE / f"{NAME}.kicad_sch"))
    if shutil.which("magick") and shutil.which("rsvg-convert"):  # optional: trim and rasterize
        subprocess.run(["magick", str(IMG / f"{NAME}-3d.png"), "-trim", "+repage", "-bordercolor", "none",
                        "-border", "24", str(IMG / f"{NAME}-3d.png")], check=True)
        for svg in sorted(sch.glob("*.svg")):
            png = svg.with_suffix(".png")
            subprocess.run(["rsvg-convert", "-w", "2400", "-b", "white", str(svg), "-o", str(png)], check=True)
            # first trim drops the white margin, the second the empty part of the cream sheet
            subprocess.run(["magick", str(png), "-trim", "+repage", "-fuzz", "2%", "-trim", "+repage",
                            "-bordercolor", "#f5f4ef", "-border", "48", "-resize", "1400x>", str(png)], check=True)
        routed = IMG / f"{NAME}-routed.svg"
        subprocess.run(["rsvg-convert", "-w", "1400", "-b", "#0d1117", str(routed), "-o",
                        str(routed.with_suffix(".png"))], check=True)


def main() -> int:
    spec = json.loads((HERE / f"{NAME}.circuit.json").read_text(encoding="utf-8"))
    step("sync_design", s.sync_design(str(HERE), NAME, spec), ("netlist_check", "erc", "parity"))
    step("place_footprints", s.place_footprints(str(PCB), PLACEMENT), ("moved", "errors"))
    step("set_netclass", s.set_netclass(str(PCB), "POWER", ["VBUS", "+3V3"], track_mm=0.5,
                                        via_mm=0.8, via_drill_mm=0.4), ("netclass",))
    step("set_netclass", s.set_netclass(str(PCB), "GND", ["GND"], track_mm=0.4), ("netclass",))
    step("autoroute", s.autoroute(str(PCB), max_passes=100, ignore_netclasses=["GND"]),
         ("unrouted_before", "unrouted_after", "tracks", "vias"))
    copper_view()
    step("add_copper_zone", s.add_copper_zone(str(PCB)), ("zone", "stitch_vias", "unrouted"))
    drc = step("run_drc", s.run_drc(str(PCB), schematic_parity=True))
    if drc.get("verdict") != "CLEAN":
        return 1
    step("export_fab", s.export_fab(str(PCB)), ("zip", "missing_lcsc"))
    images()
    print(f"images -> {IMG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
