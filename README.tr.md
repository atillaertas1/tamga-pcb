# pcbmcp — AI destekli KiCad PCB tasarımı (MCP)

[English](README.md) · MIT lisansı

Claude Code'un (veya herhangi bir MCP istemcisinin) KiCad kartlarını **token-verimli** biçimde
okuyup düzenlemesini, rotalamasını, doğrulamasını ve üretime çıkarmasını sağlayan MCP sunucusu.

## Mimari

```
Claude Code ──MCP/stdio──> pcbmcp (uv venv, mcp 2.x)
                            ├── kicad-cli        : DRC, ERC, gerber/drill, render, Altium import
                            ├── worker.py (alt süreç, KiCad'in Python'u + pcbnew SWIG)
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
| 20 araç, kısa açıklamalar | araç şemaları ≈ 2.4k token (hazır sunucularda 64-216 araç) |
| Ham `.kicad_pcb` yok; footprint başına 1 satır | 7 parçalı kart özeti ≈ 900 karakter |
| DRC/ERC türe göre gruplu, 3 örnek konum | 300 ihlal → ~1 KB |
| Parça başına 1 satır, basic önce | 20 sonuç yerine 8 satır |
| Kurallar/checklist skill'de | sadece PCB işinde bağlama yüklenir |

## Araçlar
**Tasarım:** `find_symbol`, `get_symbol`, `find_footprint`, `import_lcsc_part`, `sync_design`

**Kart:** `board_summary`, `get_footprint`, `place_footprints`, `set_netclass`, `set_footprint_fields`, `add_copper_zone`,
`fill_zones`, `route_net`, `autoroute`, `run_drc`, `run_erc`, `render_board`, `search_parts`, `export_fab`,
`import_board` (Altium `.PcbDoc`, Eagle, PADS, CADSTAR...).

## Kurulum
Gerekenler: KiCad 10, [uv](https://docs.astral.sh/uv/), autoroute için Java 25+. Linux, macOS ve Windows;
KiCad yolları otomatik bulunur, `PCBMCP_KICAD_CLI`, `PCBMCP_KICAD_PYTHON`, `PCBMCP_SYMBOL_DIR`,
`PCBMCP_FOOTPRINT_DIR` ile ezilebilir.
Claude Code plugin'i olarak (Claude Code içinde):
```
/plugin install pcbmcp --marketplace <owner>/<repo>
```
Eski sürümlerde: `/plugin marketplace add <owner>/<repo>`, sonra `/plugin install pcbmcp@pcbmcp`.
Plugin MCP sunucusunu ve `pcb-design` skill'ini getirir; her proje klasöründe çalışır. İlk açılışta `uv`
ortamı kurar, ilk `autoroute` Freerouting'i indirir.

Geliştirme:
```bash
git clone <repo-url> pcbmcp && cd pcbmcp
uv sync --extra lcsc
uv run pcbmcp doctor          # KiCad/pcbnew/Java kontrolü (--fix: Freerouting'i şimdi indir)
uv run pytest -q              # KiCad gerektiren testler KiCad yoksa atlanır
claude --plugin-dir .         # çalışma kopyasını plugin olarak yükle
```

İçe aktarılan parçalar ve Freerouting jar kullanıcı veri dizininde durur (`pcbmcp doctor` gösterir;
`PCBMCP_DATA_DIR` ile değişir). Stok olmayan kütüphaneler projenin `lib/` klasörüne kopyalanır, proje
tek başına taşınabilir.

## Örnek oturum
```
> examples/ldo/ldo.kicad_pcb kartını yerleştir, rotala, GND dök, DRC temiz olsun, JLC paketi çıkar.
```
Test kartı: `<KiCad Python'u> scripts/make_example_board.py examples/ldo/ldo.kicad_pcb`

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
Stok olmayan kütüphaneler (`pcbmcp_custom`, `pcbmcp_lcsc`) projenin `lib/` klasörüne kopyalanır ve
`sym-lib-table`/`fp-lib-table`'a `${KIPRJMOD}/lib/...` olarak otomatik eklenir.

KiCad'de olmayan parça (`[lcsc]` extra'sı gerekir): `import_lcsc_part("C597931")` → veri dizininde
`symbols/pcbmcp_lcsc.kicad_sym`, `footprints/pcbmcp_lcsc.pretty` (easyeda2kicad + `kicad-cli upgrade`). KiCad'de aynı parçanın
footprint'i varsa pad numaralandırması karşılaştırılır (ör. Hirose DF40'ta EasyEDA U-numaralı,
KiCad tek/çift → 100/100 pad farklı; uyarı verir).

**Devre blokları** (tekrar eden alt devreler): `blocks` bir kez tanımlanır, `instances` ile kullanılır.
Port eşlenmezse aynı adlı global nete bağlanır; diğer netler `<net_prefix><net>` olur (varsayılan
`<ad>_`). Ref'ler `ref_base` (U1→U101) ya da `refs` ile sabit — bloğa parça eklemek diğer instance'ların
ref'lerini, dolayısıyla PCB yerleşimini kaydırmaz. Mevcut tasarımı bloklara taşırken `refs` + `net_prefix`
ile eski ref ve net adları korunur: iki MAX3232'li bir kart bloklaştırıldığında netlist, PCB ve DRC
birebir aynı kaldı. Net adı değişirse `nets_gone` raporlanır; `.kicad_dru`'da o ada bakan kural varsa
`dru_rules_reference_gone_nets` uyarır (bağlantı doğru olsa da kural sessizce devre dışı kalır).

Her blok instance'ı kendi şematik sayfasına gider (`<ad>-<instance>.kicad_sch`); kök sayfada sheet kutusu
ve port eşlemesi yazılır. Bağlantı global label ile kalır, net adları hiyerarşiden etkilenmez.

Sınırlar: sheet kutularında hiyerarşik pin yok (portlar yazı olarak); şematik okunur ama "label stili" (tel çizimi yok);
`.kicad_sch` elle düzenlenirse sonraki senkronda ezilir.

## Yol haritası
- [ ] kipy (IPC) backend: açık KiCad'de canlı düzenleme
- [x] Netclass yönetimi (güç netleri için geniş iz) — `set_netclass`
- [x] Şematik üretimi (spec → .kicad_sch + PCB senkronu)
- [x] Devre blokları + hiyerarşik sheet'ler
- [x] Güç sembolleri
- [ ] Sheet kutularında hiyerarşik pinler
- [x] Kritik net için `route_net` (elle yön verilen, kilitli rota; autoroute dokunmaz)
- [ ] DeepPCB/Quilter API alternatif router
- [ ] ngspice ile analog blok simülasyonu

## Lisans
MIT. Ürettiğiniz şematik, PCB ve üretim dosyaları tamamen sizindir; hiçbir aracın lisansı çıktıya geçmez.
Opsiyonel bağımlılıklar kendi lisanslarıyla ayrıca kurulur: easyeda2kicad (AGPL-3.0, `[lcsc]` extra'sı,
ayrı süreç olarak çağrılır), Freerouting (GPL-3.0, ilk kullanımda indirilen ayrı jar). KiCad GPL-3.0.
