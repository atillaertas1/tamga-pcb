# pcbmcp — AI-assisted KiCad PCB design over MCP

[Türkçe](README.tr.md) · MIT licensed

An MCP server that lets Claude Code (or any MCP client) design, place, route, check and export
KiCad boards **token-efficiently**: from a short JSON circuit description to a DRC-clean board and a
JLCPCB fabrication package.

```
> Make a 20x15 mm board: 5 V input header, 330 Ω resistor and a green 0805 LED.
  Place it, route it, pour GND, get DRC clean and export the JLCPCB package.
```

## Requirements
- **KiCad 10** (provides `kicad-cli` and the `pcbnew` Python module)
- **[uv](https://docs.astral.sh/uv/)** (Python 3.11+)
- **Java 25+** for autorouting (Freerouting), optional

Linux, macOS and Windows. KiCad is located automatically (`/usr/share/kicad`, `KiCad.app`,
`%ProgramFiles%\KiCad\<version>`). Linux is the most tested platform; on macOS/Windows please run
`pcbmcp doctor` and open an issue if something is not found.

## Install (Claude Code plugin)
In Claude Code:
```
/plugin install pcbmcp --marketplace <owner>/<repo>
```
On older Claude Code versions: `/plugin marketplace add <owner>/<repo>`, then `/plugin install pcbmcp@pcbmcp`.

The plugin brings the MCP server (20 tools) and the `pcb-design` skill (design loop, JLCPCB limits,
review checklist). On first start `uv` builds its environment, and the first `autoroute` downloads
Freerouting. Nothing else to set up; it works in any project folder.

If something is not found, ask Claude to "run the pcbmcp doctor": it checks KiCad, pcbnew, Java and the
network and names the environment variable to set for anything it cannot detect.

**Without the plugin system** (other MCP clients): clone the repo and register
`uv run --directory /path/to/pcbmcp --extra lcsc pcbmcp` as a stdio server.

If detection fails, set any of `PCBMCP_KICAD_CLI`, `PCBMCP_KICAD_PYTHON` (KiCad's own Python, the one that
can `import pcbnew`), `PCBMCP_SYMBOL_DIR`, `PCBMCP_FOOTPRINT_DIR`, `PCBMCP_JAVA`, `FREEROUTING_JAR`.

## Tools
**Design:** `find_symbol`, `get_symbol`, `find_footprint`, `import_lcsc_part`, `sync_design`

**Board:** `board_summary`, `get_footprint`, `place_footprints`, `set_netclass`, `set_footprint_fields`,
`add_copper_zone`, `fill_zones`, `route_net`, `autoroute`, `run_drc`, `run_erc`, `render_board`,
`search_parts`, `export_fab`, `import_board` (Altium `.PcbDoc`, Eagle, PADS, CADSTAR…)

## Design from a spec
The single source of truth is `<name>.circuit.json`:
```json
{"title": "LED", "board": {"width_mm": 20, "height_mm": 15},
 "parts": [
  {"ref": "J1", "symbol": "Connector_Generic:Conn_01x02", "value": "5V IN",
   "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
   "pins": {"1": "+5V", "2": "GND"}},
  {"ref": "R1", "symbol": "Device:R", "value": "330", "lcsc": "C17630",
   "footprint": "Resistor_SMD:R_0805_2012Metric", "pins": {"1": "+5V", "2": "LED_A"}},
  {"ref": "D1", "symbol": "Device:LED", "value": "GREEN",
   "footprint": "LED_SMD:LED_0805_2012Metric", "pins": {"A": "LED_A", "K": "GND"}}]}
```
`sync_design` then:
1. writes the `.kicad_sch` (deterministic UUIDs, multi-unit symbols, power symbols, `PWR_FLAG`s);
2. exports the netlist with `kicad-cli` and checks it against the spec;
3. updates the PCB from the netlist like KiCad's F8, **keeping existing placement**;
4. runs ERC and a schematic↔PCB parity check.

Pins can be keyed by number (`"1"`) or name (`"A"`). A wrong pin name or a missing pad writes nothing
and lists the valid pins. Edits go through `patch={parts:[{ref:"R1", value:"470"}], remove:["C1"]}`.
Parts with an `lcsc` number are checked against the real LCSC pinout.

**Circuit blocks:** define a repeated sub-circuit once in `blocks` and use it from `instances`; each instance
gets its own schematic sheet, stable refs (`ref_base` / `refs`) and prefixed nets.

**Parts not in KiCad:** `import_lcsc_part("C597931")` (needs the `lcsc` extra) converts the EasyEDA part
into the `pcbmcp_lcsc` library in your data directory and compares its pad numbering with KiCad's
footprint when one exists. Non-stock libraries are copied into `<project>/lib/`, so a project folder is
self-contained and can be shared as is.

## Why it is built this way
| Choice | Reason |
|---|---|
| `pcbnew` runs in a worker subprocess | it lives in KiCad's Python and can segfault; the server survives |
| File based, not KiCad's IPC API | works without the GUI, and in CI |
| Freerouting via pcbnew DSN/SES | `kicad-cli` 10 has no DSN export |
| 20 tools, one-line results | tool schemas ≈ 2.4k tokens; a 7-part board summary ≈ 900 chars; 300 DRC violations → ~1 KB |
| Rules and checklist in a skill | loaded into context only for PCB work |

## Development
```bash
git clone <repo-url> pcbmcp && cd pcbmcp
uv sync --extra lcsc
uv run pytest -q          # KiCad-dependent tests are skipped when KiCad is not installed
claude --plugin-dir .     # run Claude Code with your working copy as the plugin
```
Example board: `<KiCad Python> scripts/make_example_board.py examples/ldo/ldo.kicad_pcb`

## Roadmap
- [ ] kipy (IPC) backend for live editing in an open KiCad
- [ ] Hierarchical pins on sheet symbols
- [ ] Alternative routers (DeepPCB / Quilter)
- [ ] ngspice simulation of analog blocks

## License
MIT, see [LICENSE](LICENSE). **The schematics, boards and fabrication files you make are entirely yours**;
no tool's license carries over to its output.

Optional components are installed separately under their own licenses: easyeda2kicad (AGPL-3.0, the `lcsc`
extra, run as a separate process) and Freerouting (GPL-3.0, a separate jar downloaded on first use).
KiCad itself is GPL-3.0.
