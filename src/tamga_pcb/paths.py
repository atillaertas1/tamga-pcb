"""Platforma göre yollar: KiCad kurulumu (kicad-cli, pcbnew'li Python, stok kütüphaneler) ve
kullanıcı veri/önbellek dizinleri. Her değer TAMGA_* env ile ezilebilir; env yoksa tespit edilir.

Linux   : PATH / /usr/bin/python3 / /usr/share/kicad
macOS   : /Applications/KiCad/KiCad.app (Contents/MacOS, Frameworks/Python.framework, SharedSupport)
Windows : %ProgramFiles%\\KiCad\\<sürüm>\\bin (kicad-cli.exe, python.exe), ...\\share\\kicad
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
BUNDLED = PACKAGE / "data"  # wheel ile gelen kütüphaneler (tamga_custom)


def _platform(system: str | None = None) -> str:
    s = system or sys.platform
    return "win" if s.startswith("win") else "mac" if s == "darwin" else "linux"


def _version_key(p: Path) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", p.name)) or (0,)


def kicad_install(system: str | None = None, env: os._Environ | dict | None = None,
                  mac_apps: Path = Path("/Applications")) -> dict[str, str]:
    """Tespit edilen KiCad yolları: {'cli', 'python', 'symbols', 'footprints', 'template'}.
    Bulunamayan değer için makul varsayılan döner (doctor ne eksik olduğunu söyler)."""
    env = os.environ if env is None else env
    plat = _platform(system)
    if plat == "win":
        base = Path(env.get("ProgramFiles", r"C:\Program Files")) / "KiCad"
        vers = sorted((d for d in base.glob("*") if (d / "bin" / "kicad-cli.exe").is_file()),
                      key=_version_key, reverse=True) if base.is_dir() else []
        root = vers[0] if vers else base / "10.0"
        cli = shutil.which("kicad-cli") or str(root / "bin" / "kicad-cli.exe")
        python, share = root / "bin" / "python.exe", root / "share" / "kicad"
    elif plat == "mac":
        app = mac_apps / "KiCad" / "KiCad.app" / "Contents"
        cli = shutil.which("kicad-cli") or str(app / "MacOS" / "kicad-cli")
        python = app / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3"
        share = app / "SharedSupport"
    else:
        cli = shutil.which("kicad-cli") or "kicad-cli"
        python = Path("/usr/bin/python3")
        share = next((p for p in (Path("/usr/share/kicad"), Path("/usr/local/share/kicad"))
                      if p.is_dir()), Path("/usr/share/kicad"))

    # KiCad'in kendi değişkenleri (KICAD10_SYMBOL_DIR ...) varsa en yeni sürümünkünü kullan
    def kicad_var(kind: str) -> str | None:
        for n in range(20, 5, -1):
            if v := env.get(f"KICAD{n}_{kind}_DIR"):
                return v
        return None

    return {"cli": cli, "python": str(python),
            "symbols": kicad_var("SYMBOL") or str(share / "symbols"),
            "footprints": kicad_var("FOOTPRINT") or str(share / "footprints"),
            "template": kicad_var("TEMPLATE") or str(share / "template")}


def kicad_config_dir(major: int, system: str | None = None, env: os._Environ | dict | None = None) -> Path:
    """KiCad'in kullanıcı ayar dizini (genel sym-lib-table / fp-lib-table burada), ör. ~/.config/kicad/10.0."""
    env = os.environ if env is None else env
    home = Path.home()
    plat = _platform(system)
    if env.get("KICAD_CONFIG_HOME"):
        base = Path(env["KICAD_CONFIG_HOME"])
    elif plat == "win":
        base = Path(env.get("APPDATA", home / "AppData" / "Roaming")) / "kicad"
    elif plat == "mac":
        base = home / "Library" / "Preferences" / "kicad"
    else:
        base = Path(env.get("XDG_CONFIG_HOME", home / ".config")) / "kicad"
    return base / f"{major}.0"


def user_dir(kind: str, system: str | None = None, env: os._Environ | dict | None = None) -> Path:
    """kind 'data' | 'cache': platformun kullanıcıya ait tamga-pcb dizini."""
    env = os.environ if env is None else env
    home = Path.home()
    plat = _platform(system)
    if plat == "win":
        base = Path(env.get("LOCALAPPDATA", home / "AppData" / "Local")) / "tamga-pcb"
        return base / "cache" if kind == "cache" else base
    if plat == "mac":
        return home / "Library" / ("Caches" if kind == "cache" else "Application Support") / "tamga-pcb"
    if kind == "cache":
        return Path(env.get("XDG_CACHE_HOME", home / ".cache")) / "tamga-pcb"
    return Path(env.get("XDG_DATA_HOME", home / ".local" / "share")) / "tamga-pcb"


_K = kicad_install()
KICAD_CLI = os.environ.get("TAMGA_KICAD_CLI", _K["cli"])
KICAD_PYTHON = os.environ.get("TAMGA_KICAD_PYTHON", _K["python"])
SYMBOL_DIR = Path(os.environ.get("TAMGA_SYMBOL_DIR", _K["symbols"]))
FOOTPRINT_DIR = Path(os.environ.get("TAMGA_FOOTPRINT_DIR", _K["footprints"]))
TEMPLATE_DIR = Path(os.environ.get("TAMGA_TEMPLATE_DIR", _K["template"]))
# İçe aktarılan parçalar (tamga_lcsc) ve indirilen Freerouting jar burada durur.
DATA_DIR = Path(os.environ.get("TAMGA_DATA_DIR", user_dir("data"))).expanduser()
CACHE_DIR = Path(os.environ.get("TAMGA_CACHE_DIR", user_dir("cache"))).expanduser()


def env_dirs(name: str) -> list[Path]:
    return [Path(p) for p in os.environ.get(name, "").split(os.pathsep) if p]
