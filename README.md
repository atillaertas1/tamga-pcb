# pcbmcp — AI destekli KiCad PCB tasarımı (MCP)

Claude Code'un (veya herhangi bir MCP istemcisinin) KiCad kartlarını **token-verimli** biçimde
okuyup düzenlemesini, rotalamasını, doğrulamasını ve üretime çıkarmasını sağlayan MCP sunucusu.

## Mimari

```
Claude Code ──MCP/stdio──> pcbmcp (uv venv, mcp 2.x)
                            ├── kicad-cli        : DRC, ERC, gerber/drill, render, Altium import
                            ├── worker.py (alt süreç, /usr/bin/python3 + pcbnew SWIG)
                            │     summary, footprint, place, zone, fields, DSN/SES, BOM/CPL
                            ├── Freerouting jar  : GUI'siz autoroute (DSN → SES)
                            └── jlcsearch API    : JLCPCB/LCSC stok + fiyat (key gerekmez)
```

Neden böyle:
- **pcbnew alt süreçte**: modül sistem Python'unda yaşıyor ve segfault atabiliyor; sunucu ölmüyor.
- **kicad-cli 10'da DSN export yok** → Freerouting köprüsü pcbnew `ExportSpecctraDSN/ImportSpecctraSES` ile.
- **IPC API (kipy) yerine dosya tabanlı**: KiCad GUI açık olmadan çalışır, CI'da da koşar.
  Canlı GUI düzenleme istenirse sonraki adım kipy backend'i.

## Token verimliliği
| Önlem | Etki |
|---|---|
| 14 araç, kısa açıklamalar | araç şemaları ≈ 1.6k token (hazır sunucularda 64-216 araç) |
| Ham `.kicad_pcb` yok; footprint başına 1 satır | 7 parçalı kart özeti ≈ 900 karakter |
| DRC/ERC türe göre gruplu, 3 örnek konum | 300 ihlal → ~1 KB |
| Parça başına 1 satır, basic önce | 20 sonuç yerine 8 satır |
| Kurallar/checklist skill'de | sadece PCB işinde bağlama yüklenir |

## Araçlar
`board_summary`, `get_footprint`, `place_footprints`, `set_netclass`, `set_footprint_fields`, `add_copper_zone`,
`fill_zones`, `autoroute`, `run_drc`, `run_erc`, `render_board`, `search_parts`, `export_fab`,
`import_board` (Altium `.PcbDoc`, Eagle, PADS, CADSTAR...).

## Kurulum
```bash
cd ~/Documents/Personal/pcb-deneme
uv sync
# Freerouting (bir kez): https://github.com/freerouting/freerouting/releases
curl -L -o tools/freerouting.jar \
  https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar
uv run pytest -q          # 16 test, KiCad 10 gerekir (autoroute testi jar varsa koşar)
```
Bu klasörde `claude` açınca `.mcp.json` sunucuyu, `.claude/skills/pcb-design` iş akışını yükler.
Başka projelerde kullanmak için: `claude mcp add pcbmcp -- uv run --directory <bu klasör> pcbmcp`.

## Örnek oturum
```
> examples/ldo/ldo.kicad_pcb kartını yerleştir, rotala, GND dök, DRC temiz olsun, JLC paketi çıkar.
```
Test kartı: `/usr/bin/python3 scripts/make_example_board.py examples/ldo/ldo.kicad_pcb`

## Şematik tarafı
Şematiği koddan üretmek için iki yol:
1. **atopile** (`uv tool install atopile`, `ato build`) → KiCad projesi üretir, ardından bu araçlar.
2. KiCad eeschema'da elle çiz → `run_erc` ile kontrol → "Update PCB from schematic" → bu araçlar.

## Yol haritası
- [ ] kipy (IPC) backend: açık KiCad'de canlı düzenleme
- [x] Netclass yönetimi (güç netleri için geniş iz) — `set_netclass`
- [ ] Kritik net için `route_net(net, waypoints)` (elle yön verilen rota)
- [ ] DeepPCB/Quilter API alternatif router
- [ ] ngspice ile analog blok simülasyonu
