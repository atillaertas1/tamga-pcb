---
name: pcb-design
description: Use when designing, placing, routing, reviewing or exporting a KiCad PCB with the tamga-pcb tools. Holds the design loop, JLCPCB fab limits and the placement/review checklist.
---

# PCB design loop (tamga-pcb)

## Rules
- **Never** Read `.kicad_pcb` / `.kicad_sch` files; use `board_summary`, `get_footprint`.
- Project files (`.kicad_pro`) carry the design rules; when copying a board, copy the whole folder.
- Write tools keep backups (`.tamga_backups/`). Still suggest a git commit before big steps.
- Renders (`render_board`) are expensive: look only when placement is done and at the final review.
- Reply in the user's language.

## Design from scratch (schematic + PCB)
The single source of truth is `<name>.circuit.json`. The `.kicad_sch` is generated from it; **manual edits**
are overwritten by the next `sync_design`, so change the spec instead.
1. Pin down the requirements (input voltage, current, connectors, board size). Ask about unknowns.
2. For each part: `find_symbol` → `get_symbol(lib_id, footprint)` to confirm pin names and that the
   footprint pad numbers match → `find_footprint` (using the symbol's `fp_filters`).
   Compute passive values and tell the user the reasoning (e.g. LED: R = (Vin−Vf)/I).
3. Pick an LCSC code with `search_parts` and put it in the spec as `lcsc`. Passives search by value:
   `search_parts('5.1k', package='0603', basic_only=True)`, `search_parts('100nF', package='0603')`. If an IC
   returns nothing, its LCSC code (`search_parts('C84681')`) still works. Then
   `get_symbol(lib_id, footprint, lcsc)`: if `pinout_check` says `mismatch`, it may be the wrong symbol
   variant, the wrong part or LED polarity; do not move on without checking the datasheet.
   If KiCad has no suitable symbol/footprint, use `import_lcsc_part(lcsc)`. If it returns
   `kicad_footprint_mismatch`, **do not mix** the EasyEDA footprint with the KiCad symbol (pad numbering differs).
   If neither KiCad nor LCSC has it, `create_part(name, pins, package)` makes both from the datasheet:
   package soic (SOP/SSOP/TSSOP/MSOP: lead_span_mm = tip to tip), qfn/dfn (ep_mm for the thermal pad),
   header, or pads for anything else. Copy dimensions from the datasheet's package drawing; tell the user
   to compare the result with the recommended land pattern before ordering.
4. `sync_design(project_dir, name, spec)` → expect `netlist_check: OK`, `erc: CLEAN`, `parity: OK`.
   - `pin_not_connected` → connect it or add it to `nc` (a deliberate decision, never `*` for everything).
   - `pwr_flags_added` is normal.
5. Later changes go through `sync_design(..., patch={parts:[...], remove:[...]})`; PCB placement is kept.
6. Continue with the loop below from placement onwards.

## Board shape and stackup (in the spec's `board`)
- `corner_radius_mm` for rounded corners, or `outline_mm` for any polygon (mm from the top-left corner).
- `mounting_holes: {diameter_mm: 3.2, inset_mm: 3.5}` puts M3 holes in the corners (2.2/2.7/3.2/4.3 = M2/M2.5/M3/M4);
  `at_mm` for other positions. Keep parts clear of them.
- `silk: [{text, x, y, size_mm?, layer?: 'B'}]` for a name, version or pinout text.
- `layers: 4` makes In1/In2 power planes (the router does not use them): pour them with
  `add_copper_zone(net='GND', layer='In1.Cu', stitch_pitch_mm=0)` and the main supply on `In2.Cu`, then the usual
  `add_copper_zone()` on the outer layers. Use 4 layers for USB/RF/fast edges or when 2 layers will not route.

## Loop
1. `board_summary` → understand nets and parts. Ask the user about missing information.
2. Part choice: prefer **[B] basic** parts from `search_parts`; write the chosen LCSC code with
   `set_footprint_fields(... field:'LCSC')`.
3. Placement (`place_footprints`, in one batch):
   - Connectors on the edge, facing out; mechanical constraints first.
   - Decoupling capacitor ≤ 2 mm from its IC power pin, same layer.
   - Regulator: input C → VIN pin, output C → VOUT pin; space between hot parts.
   - Crystal ≤ 5 mm from the MCU pins, no other signals underneath.
   - No courtyard overlaps; leave ~0.5 mm between footprints.
   - After each placement verify pad positions with `get_footprint` and fix if needed.
4. Netclasses (BEFORE autoroute, the router uses these widths):
   - Power nets: `set_netclass(name='POWER', nets=[...], track_mm=current×0.5, via_mm=0.8, via_drill_mm=0.4)`
     (e.g. 1 A → 0.5 mm). If you don't know the current, ask; don't guess.
   - If `unmatched_nets` comes back, fix the net names from `board_summary`.
   - Changing after routing: `apply_to_existing=True` + `run_drc`.
   - Controlled impedance: `set_netclass(name='USB', nets=['USB_DP','USB_DN'], impedance_ohm=90, diff_gap_mm=0.15)`
     computes the width from the stackup (50 Ω single-ended: no gap). On 2 layers it warns that it is impractical:
     tell the user, suggest `board.layers=4`, or keep USB full-speed runs short and accept it.
5. Critical nets (near an isolation barrier, power paths that must stay short, sensitive analog):
   guide them first with `route_net(net, 'U1.3', 'C5.1', points)` (they stay locked). A differential pair:
   `route_net('USB_DP', 'J1.A6', 'U2.14', centre_points, pair_net='USB_DN', pair_from_pad='J1.A7', pair_to_pad='U2.13')`;
   check `board_summary(lengths=['USB_D*'])` for skew (it sums every track of a net, so pads joined by other
   routes, like USB-C's A6/B6, add length). Then
   `autoroute` → `add_copper_zone` (default: GND, both layers + 5 mm stitching vias) → `run_drc`.
   `autoroute` writes to the board itself (with a backup) and first rips up unlocked tracks/vias, so after moving
   parts just run `autoroute` again, then `add_copper_zone` again (it replaces the old pour). Never try to edit
   tracks or restore backups by other means. Do not copy the board under another name and run
   DRC on it: KiCad reads the rules from the `.kicad_pro`/`.kicad_dru` with the same name. If `run_drc`
   returns `rules_warning`, the result is not valid.
6. Until DRC is `CLEAN`: start with the most frequent violation type and use the `examples` locations.
   Freerouting is not deterministic: if the only errors are `copper_edge_clearance` or `track_width` on
   autorouted tracks, run `autoroute` again, then `add_copper_zone` and `run_drc`.
7. `export_fab` → `missing_lcsc` should be empty; list any leftovers to the user.

## Setup problems
If a tool reports that KiCad, pcbnew or Freerouting cannot be found, run the doctor and show the user its
output: `uv run --directory "<tamga-pcb plugin root>" tamga doctor --fix`. The plugin root is two levels above
this skill's folder. It names the environment variable to set for anything it cannot detect.

## JLCPCB standard 2/4-layer limits (2026)
| Rule | Min | Recommended |
|---|---|---|
| Track width / clearance | 0.127 mm (5 mil) | 0.2 mm |
| Via diameter / drill | 0.45 / 0.2 mm (4L: 0.25 drill) | 0.6 / 0.3 mm |
| Copper to edge | 0.3 mm | 0.5 mm |
| Silk line / text height | 0.15 / 1.0 mm | 0.15 / 1.0 mm |
| Power track (1 oz, ΔT 10 °C) | ~0.3 mm/A outer layer | 0.5 mm/A |

Prices and capabilities change; on critical projects have the user confirm against JLC's current capabilities page.

## Review checklist (before export)
- [ ] DRC CLEAN, unrouted = 0
- [ ] Decoupling on every power pin of every IC
- [ ] Power tracks sized for their current; GND pour unbroken (not split by tracks)
- [ ] Mounting holes / connector positions match the mechanical drawing
- [ ] Polarity marks (diode, LED, electrolytic, pin 1) visible on silkscreen
- [ ] Every BOM part has an LCSC code; non-basic parts reported to the user
