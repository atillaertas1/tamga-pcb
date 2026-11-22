"""Stok olmayan kütüphaneler projeye kopyalanır; eski tamga-pcb kayıtları taşınır (KiCad gerekmez)."""
from tamga_pcb import design, library, sexpr

SPEC = {"parts": [{"ref": "J1", "symbol": "tamga_custom:Conn_02x50_Odd_Even"}]}


def _uri(proj, lib):
    tree = sexpr.parse((proj / "sym-lib-table").read_text())
    return next(sexpr.value(e, "uri") for e in sexpr.children(tree, "lib") if sexpr.value(e, "name") == lib)


def test_custom_lib_copied_into_project(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "SYM_DIR", tmp_path / "no-stock")
    assert design.ensure_lib_tables(tmp_path, SPEC) == ["sym-lib-table:tamga_custom"]
    assert _uri(tmp_path, "tamga_custom") == "${KIPRJMOD}/lib/tamga_custom.kicad_sym"
    assert (tmp_path / "lib" / "tamga_custom.kicad_sym").is_file()
    assert design.ensure_lib_tables(tmp_path, SPEC) == []  # ikinci sefer değişiklik yok


def test_old_tamga_entry_migrated_user_entry_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "SYM_DIR", tmp_path / "no-stock")
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)'
        ' (lib (name "tamga_custom") (type "KiCad") (uri "${KIPRJMOD}/../../symbols/tamga_custom.kicad_sym")'
        ' (options "") (descr "tamga-pcb project symbols"))'
        ' (lib (name "mine") (type "KiCad") (uri "/x/mine.kicad_sym") (options "") (descr "")))')
    assert design.ensure_lib_tables(tmp_path, SPEC) == ["sym-lib-table:tamga_custom"]
    assert _uri(tmp_path, "tamga_custom") == "${KIPRJMOD}/lib/tamga_custom.kicad_sym"
    assert _uri(tmp_path, "mine") == "/x/mine.kicad_sym"
