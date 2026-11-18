---
name: pcb-design
description: Use when designing, placing, routing, reviewing or exporting a KiCad PCB with the pcbmcp tools. Holds the design loop, JLCPCB fab limits and the placement/review checklist.
---

# PCB tasarım döngüsü (pcbmcp)

## Kurallar
- `.kicad_pcb` / `.kicad_sch` dosyalarını **asla** Read ile okuma; `board_summary`, `get_footprint` kullan.
- Proje dosyaları (`.kicad_pro`) kuralları taşır; kartı kopyalarken tüm klasörü kopyala.
- Yazan araçlar yedek alır (`.pcbmcp_backups/`). Yine de büyük adımlardan önce git commit öner.
- Görsel (`render_board`) pahalı: sadece yerleşim bittiğinde ve son kontrolde bak.

## Sıfırdan tasarım (şematik + PCB)
Tek doğruluk kaynağı `<name>.circuit.json`. `.kicad_sch` ondan üretilir; **elle düzenleme**
bir sonraki `sync_design`'da ezilir, değişikliği spec'e yap.
1. Gereksinimi netleştir (giriş gerilimi, akım, konnektör, kart boyutu). Bilinmeyeni sor.
2. Her parça için: `find_symbol` → `get_symbol(lib_id, footprint)` ile pin adlarını ve
   footprint pad numaralarının eşleştiğini doğrula → `find_footprint` (sembolün `fp_filters`'ı ile).
   Pasiflerin değerini hesapla ve gerekçesini kullanıcıya yaz (ör. LED: R = (Vin−Vf)/I).
3. `search_parts` ile LCSC kodu seç, spec'e `lcsc` olarak koy. Sonra
   `get_symbol(lib_id, footprint, lcsc)` → `pinout_check` `mismatch` verirse yanlış sembol
   varyantı / yanlış parça / LED polaritesi olabilir: datasheet'e bakmadan geçme.
   KiCad'de uygun sembol/footprint yoksa `import_lcsc_part(lcsc)`. `kicad_footprint_mismatch`
   dönerse EasyEDA footprint'ini KiCad sembolüyle **karıştırma** (pad numaralandırması farklı).
4. `sync_design(project_dir, name, spec)` → `netlist_check: OK`, `erc: CLEAN`, `parity: OK` olmalı.
   - `pin_not_connected` → ya bağla ya `nc` listesine ekle (bilinçli karar ver, hepsine `*` deme).
   - `pwr_flags_added` normaldir.
5. Sonraki değişiklikler `sync_design(..., patch={parts:[...], remove:[...]})` ile; PCB yerleşimi korunur.
6. Sonra aşağıdaki döngü (yerleşimden itibaren).

## Döngü
1. `board_summary` → netleri ve parçaları anla. Eksik bilgi varsa kullanıcıya sor.
2. Parça seçimi: `search_parts` ile **[B] basic** parçaları tercih et; seçtiğin LCSC kodunu
   `set_footprint_fields(... field:'LCSC')` ile yaz.
3. Yerleşim (`place_footprints`, tek batch'te):
   - Konnektörler kenarda, kenara dik; mekanik kısıt varsa önce onlar.
   - Decoupling kondansatörü ilgili IC güç pinine ≤ 2 mm, aynı katmanda.
   - Regülatör: giriş C → VIN pini, çıkış C → VOUT pini; sıcak parçalar arasında boşluk.
   - Kristal: MCU pinlerine ≤ 5 mm, altında başka sinyal yok.
   - Footprint'ler arası courtyard çakışması yok; ~0.5 mm boşluk bırak.
   - Her yerleşimden sonra `get_footprint` ile pad konumlarını doğrula, gerekirse düzelt.
4. Netclass'lar (autoroute'tan ÖNCE, router bu genişlikleri kullanır):
   - Güç netleri: `set_netclass(name='POWER', nets=[...], track_mm=akım×0.5, via_mm=0.8, via_drill_mm=0.4)`
     (ör. 1 A → 0.5 mm). Akımı bilmiyorsan kullanıcıya sor, tahmin etme.
   - `unmatched_nets` dönerse net adını `board_summary`'den düzelt.
   - Rotadan sonra değiştirirsen `apply_to_existing=True` + `run_drc`.
   - Kritik netler (USB diff pair, RF, yüksek hız) → kullanıcıya bırak veya açıkça işaretle.
5. Kritik netler (izolasyon bariyeri yakını, kısa tutulması gereken güç yolu, hassas analog):
   önce `route_net(net, 'U1.3', 'C5.1', points)` ile elle yönlendir (kilitli kalır). Sonra
   `autoroute` → `add_copper_zone` (varsayılan: GND, iki katman + 5 mm stitching via) → `run_drc`.
6. DRC `CLEAN` olana kadar: en çok sayıdaki ihlal türünden başla, `examples` konumlarını kullan.
7. `export_fab` → `missing_lcsc` boş olmalı; kalan varsa kullanıcıya listele.

## JLCPCB standart 2/4 katman sınırları (2026)
| Kural | Min | Önerilen |
|---|---|---|
| İz genişliği / boşluk | 0.127 mm (5 mil) | 0.2 mm |
| Via çap / delik | 0.45 / 0.2 mm (4L: 0.25 delik) | 0.6 / 0.3 mm |
| Bakır–kenar | 0.3 mm | 0.5 mm |
| Silk çizgi / yazı yüksekliği | 0.15 / 1.0 mm | 0.15 / 1.0 mm |
| Güç izi (1 oz, ΔT 10 °C) | ~0.3 mm/A dış katman | 0.5 mm/A |

Fiyat/kapasite değişir; kritik projede JLC'nin güncel kapasite sayfasını kullanıcıya doğrulat.

## Gözden geçirme checklist'i (export öncesi)
- [ ] DRC CLEAN, unrouted = 0
- [ ] Her IC'nin her güç pininde decoupling
- [ ] Güç izleri akıma göre genişlikte; GND dökümü kesintisiz (izlerle bölünmemiş)
- [ ] Montaj delikleri / konnektör konumları mekanik çizimle uyumlu
- [ ] Polarite işaretleri (diyot, LED, elektrolitik, pin 1) silkte görünür
- [ ] BOM'da tüm parçaların LCSC kodu var, basic olmayanlar kullanıcıya bildirildi
