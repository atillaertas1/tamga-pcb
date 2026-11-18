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
| 19 araç, kısa açıklamalar | araç şemaları ≈ 2.4k token (hazır sunucularda 64-216 araç) |
| Ham `.kicad_pcb` yok; footprint başına 1 satır | 7 parçalı kart özeti ≈ 900 karakter |
| DRC/ERC türe göre gruplu, 3 örnek konum | 300 ihlal → ~1 KB |
| Parça başına 1 satır, basic önce | 20 sonuç yerine 8 satır |
| Kurallar/checklist skill'de | sadece PCB işinde bağlama yüklenir |

## Araçlar
**Tasarım:** `find_symbol`, `get_symbol`, `find_footprint`, `import_lcsc_part`, `sync_design`

**Kart:** `board_summary`, `get_footprint`, `place_footprints`, `set_netclass`, `set_footprint_fields`, `add_copper_zone`,
`fill_zones`, `autoroute`, `run_drc`, `run_erc`, `render_board`, `search_parts`, `export_fab`,
`import_board` (Altium `.PcbDoc`, Eagle, PADS, CADSTAR...).

## Kurulum
```bash
cd ~/Documents/Personal/pcb-deneme
uv sync
uv run pcbmcp doctor --fix   # KiCad/pcbnew/Java kontrolü; Freerouting jar yoksa indirir (v2.5.0, Java 25+)
uv run pytest -q          # 54 test, KiCad 10 gerekir (autoroute testleri jar varsa koşar)
```
Bu klasörde `claude` açınca `.mcp.json` sunucuyu, `.claude/skills/pcb-design` iş akışını yükler.
Başka projelerde kullanmak için: `claude mcp add pcbmcp -- uv run --directory <bu klasör> pcbmcp`.

## Örnek oturum
```
> examples/ldo/ldo.kicad_pcb kartını yerleştir, rotala, GND dök, DRC temiz olsun, JLC paketi çıkar.
```
Test kartı: `/usr/bin/python3 scripts/make_example_board.py examples/ldo/ldo.kicad_pcb`

## Sıfırdan tasarım: devre tanımı → şematik + PCB
Tek doğruluk kaynağı `<name>.circuit.json`:
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
`sync_design` bundan şunları yapar:
1. `.kicad_sch` üretir: pin uçlarına tel + global label, deterministik UUID'ler,
   çok birimli semboller (LM358 vb.), `extends` ile türetilmiş semboller düzleştirilir,
   gerekli `PWR_FLAG`'ler otomatik.
2. `kicad-cli` ile netlist çıkarır ve spec ile karşılaştırır (`netlist_check`).
3. PCB'yi netlist'ten günceller (KiCad'in F8'i gibi): ekle / footprint değiştir / sil,
   **mevcut yerleşimi korur**, footprint'leri sembollere bağlar.
4. ERC + şematik↔PCB parity kontrolü.

Pin anahtarı numara (`"1"`) veya ad (`"A"`, `"VI"`) olabilir. Yanlış pin adında hiçbir dosya
yazılmaz, mevcut pinler listelenir. Düzenleme için `patch={parts:[{ref:"R1", value:"470"}], remove:["C1"]}`.

Doğrulama: her senkronda pin↔pad numara kontrolü (eksik pad = hiçbir dosya yazılmaz) ve
`lcsc` olan parçalarda sembol pinout'u ↔ gerçek LCSC parçası (EasyEDA verisi, uyarı olarak).
Stok olmayan kütüphaneler (`pcbmcp_custom`, `pcbmcp_lcsc`) projenin `sym-lib-table`/`fp-lib-table`'ına
otomatik eklenir.

KiCad'de olmayan parça: `import_lcsc_part("C597931")` → `symbols/pcbmcp_lcsc.kicad_sym`,
`footprints/pcbmcp_lcsc.pretty` (easyeda2kicad + `kicad-cli upgrade`). KiCad'de aynı parçanın
footprint'i varsa pad numaralandırması karşılaştırılır (ör. Hirose DF40'ta EasyEDA U-numaralı,
KiCad tek/çift → 100/100 pad farklı; uyarı verir).

**Devre blokları** (tekrar eden alt devreler): `blocks` bir kez tanımlanır, `instances` ile kullanılır.
Port eşlenmezse aynı adlı global nete bağlanır; diğer netler `<net_prefix><net>` olur (varsayılan
`<ad>_`). Ref'ler `ref_base` (U1→U101) ya da `refs` ile sabit — bloğa parça eklemek diğer instance'ların
ref'lerini, dolayısıyla PCB yerleşimini kaydırmaz. Mevcut tasarımı bloklara taşırken `refs` + `net_prefix`
ile eski ref ve net adları korunur: cm3-carrier'daki iki MAX3232 bloklaştırıldığında netlist, PCB ve DRC
birebir aynı kaldı. Net adı değişirse `nets_gone` raporlanır; `.kicad_dru`'da o ada bakan kural varsa
`dru_rules_reference_gone_nets` uyarır (bağlantı doğru olsa da kural sessizce devre dışı kalır).

Sınırlar: tek sayfa (hiyerarşik sheet yok); şematik okunur ama "label stili" (tel çizimi yok);
`.kicad_sch` elle düzenlenirse sonraki senkronda ezilir.

## Yol haritası
- [ ] kipy (IPC) backend: açık KiCad'de canlı düzenleme
- [x] Netclass yönetimi (güç netleri için geniş iz) — `set_netclass`
- [x] Şematik üretimi (spec → .kicad_sch + PCB senkronu)
- [ ] Hiyerarşik sheet'ler, güç sembolleri
- [ ] Kritik net için `route_net(net, waypoints)` (elle yön verilen rota)
- [ ] DeepPCB/Quilter API alternatif router
- [ ] ngspice ile analog blok simülasyonu
