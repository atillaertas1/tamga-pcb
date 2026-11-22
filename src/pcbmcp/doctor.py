"""`pcbmcp doctor [--fix]`: kurulum kontrolü. Rakiplerde en sık şikayet sessiz kurulum hataları.

Zorunlu (yoksa çıkış kodu 1): kicad-cli, pcbnew (sistem Python'u), KiCad kütüphaneleri.
Autoroute için: Java + Freerouting jar (+ jar'ın istediği Java sürümü, class dosyasından okunur).
İsteğe bağlı: easyeda2kicad, jlcsearch / EasyEDA erişimi.
--fix: Freerouting jar'ı yoksa sabit sürümü indirir.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx

from . import library, parts, paths, pinout, route
from .kicad import KICAD_CLI, KICAD_PYTHON

FREEROUTING_VERSION = "2.5.0"
FREEROUTING_URL = (f"https://github.com/freerouting/freerouting/releases/download/"
                   f"v{FREEROUTING_VERSION}/freerouting-{FREEROUTING_VERSION}.jar")
MIN_KICAD = 10


def _run(*cmd: str) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def jar_java_version(jar: Path) -> int | None:
    """Jar'daki ilk .class dosyasının major sürümü -> gereken Java (major - 44)."""
    try:
        with zipfile.ZipFile(jar) as z:
            name = next(n for n in z.namelist() if n.endswith(".class") and not n.startswith("META-INF"))
            return struct.unpack(">H", z.read(name)[6:8])[0] - 44
    except (OSError, zipfile.BadZipFile, StopIteration):
        return None


def java_version(java: str) -> int | None:
    code, out = _run(java, "-version")
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
    if code != 0 or not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 else major  # "1.8" -> 8


def download_jar(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", FREEROUTING_URL, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    if jar_java_version(tmp) is None:
        tmp.unlink()
        raise RuntimeError("downloaded file is not a valid jar")
    tmp.replace(dest)


def checks(fix: bool = False) -> list[tuple[str, str, str, str]]:
    """[(level, name, status, hint)] ; level: ok | fail | warn | info."""
    out = []

    def add(ok, name, status, hint="", required=True):
        out.append(("ok" if ok else ("fail" if required else "warn"), name, status, "" if ok else hint))

    code, ver = _run(KICAD_CLI, "version")
    m = re.match(r"(\d+)\.", ver)
    if code != 0 or not m:
        add(False, "kicad-cli", f"not runnable ({KICAD_CLI})",
            f"install KiCad {MIN_KICAD}+ or set PCBMCP_KICAD_CLI")
    else:
        add(int(m.group(1)) >= MIN_KICAD, "kicad-cli", ver, f"KiCad {MIN_KICAD}+ required (tested on 10.0.x)")

    code, pv = _run(KICAD_PYTHON, "-c", "import pcbnew; print(pcbnew.Version())")
    add(code == 0, "pcbnew", f"{pv} ({KICAD_PYTHON})" if code == 0 else f"import failed ({KICAD_PYTHON})",
        "pcbnew lives in KiCad's own Python (Linux: /usr/bin/python3, macOS: KiCad.app/Contents/"
        "Frameworks/Python.framework/Versions/Current/bin/python3, Windows: KiCad\\<ver>\\bin\\python.exe);"
        " set PCBMCP_KICAD_PYTHON to it")

    n_sym = len(list(library.SYM_DIR.glob("*.kicad_sym")))
    add(n_sym > 0, "symbol libs", f"{n_sym} in {library.SYM_DIR}", "set PCBMCP_SYMBOL_DIR")
    n_fp = len(list(library.FP_DIR.glob("*.pretty")))
    add(n_fp > 0, "footprint libs", f"{n_fp} in {library.FP_DIR}", "set PCBMCP_FOOTPRINT_DIR")

    out.append(("info", "data dir", f"{paths.DATA_DIR} (imported parts, Freerouting jar)", ""))

    # autoroute: jar + java
    jar = Path(route.os.environ.get("FREEROUTING_JAR", route.DEFAULT_JAR)).expanduser()
    if not jar.is_file() and fix:
        try:
            download_jar(jar)
            out.append(("info", "freerouting", f"downloaded v{FREEROUTING_VERSION} -> {jar}", ""))
        except (httpx.HTTPError, OSError, RuntimeError) as e:
            out.append(("warn", "freerouting", f"download failed: {e}", f"download {FREEROUTING_URL}"))
    need = jar_java_version(jar) if jar.is_file() else None
    add(jar.is_file(), "freerouting jar", f"{jar} (needs Java {need}+)" if need else f"missing: {jar}",
        "run `pcbmcp doctor --fix` or set FREEROUTING_JAR (autoroute only)", required=False)
    have = java_version(route.JAVA) if shutil.which(route.JAVA) or Path(route.JAVA).is_file() else None
    if have is None:
        add(False, "java", f"not found ({route.JAVA})", "install a JDK or set PCBMCP_JAVA (autoroute only)",
            required=False)
    else:
        add(need is None or have >= need, "java", f"{have}", f"Freerouting jar needs Java {need}+", required=False)

    # isteğe bağlı
    add(importlib.util.find_spec("easyeda2kicad") is not None, "easyeda2kicad", "importable",
        "pip install 'pcbmcp[lcsc]' or uv sync --extra lcsc (import_lcsc_part only)", required=False)
    for name, url in (("jlcsearch", f"{parts.API}/components/list.json?search=ams1117&limit=1"),
                      ("easyeda", pinout.API.format(lcsc="C6186") + "?version=6.4.19.5")):
        try:
            code = httpx.get(url, headers={"User-Agent": "pcbmcp"}, timeout=10).status_code
            add(code == 200, name, f"HTTP {code}", "search_parts / pinout check need network",
                required=False)
        except httpx.HTTPError as e:
            add(False, name, f"unreachable ({type(e).__name__})", "search_parts / pinout check need network",
                required=False)
    return out


def main(argv: list[str]) -> int:
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)  # "HTTP Request: ..." satırları çıktıyı kirletir
    fix = "--fix" in argv
    rows = checks(fix)
    mark = {"ok": "✓", "fail": "✗", "warn": "!", "info": "→"}
    for level, name, status, hint in rows:
        print(f"{mark[level]} {name:16} {status}" + (f"\n    fix: {hint}" if hint else ""))
    failed = [r for r in rows if r[0] == "fail"]
    print("\nrequired checks failed" if failed else "\nready" +
          (" (some optional features unavailable)" if any(r[0] == "warn" for r in rows) else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
