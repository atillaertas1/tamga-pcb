# Changelog

## 0.1.0 (unreleased)

First public release.

- **Claude Code plugin** with its own marketplace: `/plugin install tamga-pcb --marketplace atillaertas1/tamga-pcb`.
  Brings the MCP server (20 tools) and the `pcb-design` skill.
- **Spec → schematic → PCB** with `sync_design`: functional sheets, power symbols, sheet notes, circuit blocks with
  one sheet per instance, netlist and pin/pad checks, LCSC pinout check, ERC and schematic↔PCB parity.
- **Board tools:** placement, netclasses, guided locked routes (`route_net`), headless Freerouting, ground pours with
  stitching vias, grouped DRC/ERC, renders, JLCPCB fab export, Altium/Eagle/PADS/CADSTAR import.
- **Runs on Linux, macOS and Windows:** KiCad is detected per OS, data lives in the per-user data directory, all file
  and process I/O is UTF-8.
- **Fresh KiCad installs work:** missing global symbol/footprint library tables are created the way KiCad's first run
  does, so ERC/DRC are clean on a KiCad that was never opened.
- **Freerouting is downloaded on first use**; tracks keep the board's edge clearance.
- **Self-contained projects:** non-stock libraries are copied into `<project>/lib/`.
- **Example:** an ESP32-C3 USB-C board rebuilt end to end by `examples/esp32c3-devboard/build.py`.
- **Measured example:** a CH340C USB-UART adapter made by Claude Code from one prompt, with a session report (tokens, cost, tool calls, charts) and [benchmarks](docs/BENCHMARKS.md).
- **Readable schematics:** small parts are wired to the pin they belong to; series chains are columns.
- **Reroute safely:** `autoroute` rips up unlocked tracks first; `add_copper_zone` replaces an old pour.
- **Better part search:** values go to value lookups (`5.1k`, `100nF`), basic parts with unknown stock are kept.
