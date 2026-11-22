"""Platforma göre KiCad / kullanıcı dizini tespiti (sahte dosya sistemiyle, KiCad gerekmez)."""
from pathlib import Path

from tamga_pcb import paths


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p


def test_windows_picks_newest_kicad(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.shutil, "which", lambda _: None)
    for v in ("9.0", "10.0"):
        _touch(tmp_path / "KiCad" / v / "bin" / "kicad-cli.exe")
    k = paths.kicad_install("win32", {"ProgramFiles": str(tmp_path)})
    root = tmp_path / "KiCad" / "10.0"
    assert k["cli"] == str(root / "bin" / "kicad-cli.exe")
    assert k["python"] == str(root / "bin" / "python.exe")
    assert k["symbols"] == str(root / "share" / "kicad" / "symbols")


def test_macos_app_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.shutil, "which", lambda _: None)
    k = paths.kicad_install("darwin", {}, mac_apps=tmp_path)
    app = tmp_path / "KiCad" / "KiCad.app" / "Contents"
    assert k["cli"] == str(app / "MacOS" / "kicad-cli")
    assert Path(k["python"]).parts[-5:] == ("Python.framework", "Versions", "Current", "bin", "python3")
    assert k["footprints"] == str(app / "SharedSupport" / "footprints")


def test_kicad_env_vars_win_over_detection():
    k = paths.kicad_install("linux", {"KICAD9_SYMBOL_DIR": "/old", "KICAD10_SYMBOL_DIR": "/new"})
    assert k["symbols"] == "/new"


def test_user_dirs_per_platform(tmp_path):
    assert paths.user_dir("data", "win32", {"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "tamga-pcb"
    assert paths.user_dir("cache", "linux", {"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "tamga-pcb"
    assert paths.user_dir("data", "darwin", {}).parts[-3:] == ("Library", "Application Support", "tamga-pcb")


def test_bundled_custom_symbols_ship_in_package():
    assert (paths.BUNDLED / "symbols" / "tamga_custom.kicad_sym").is_file()
