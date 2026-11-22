"""KiCad'e erişim katmanı: kicad-cli alt süreçleri + pcbnew worker köprüsü."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .paths import KICAD_CLI, KICAD_PYTHON  # noqa: F401  (platforma göre tespit, env ile ezilir)

# pcbnew modülü KiCad'in kendi Python'unda yaşar, venv'de değil.
WORKER = Path(__file__).with_name("worker.py")
MARK = "@@TAMGA@@"


class KicadError(ToolError):
    """ToolError alt sınıfı: mcp 2.x yalnızca ToolError mesajını modele iletir,
    diğer istisnalar 'Error executing tool' olarak maskelenir."""


def abspath(p: str) -> str:
    return str(Path(p).expanduser().resolve())


def run_worker(cmd: str, args: dict, timeout: int = 300) -> dict:
    """worker.py'yi sistem Python'u ile çalıştır, son işaretli JSON satırını döndür."""
    from . import library  # döngüsel import olmasın; testlerdeki monkeypatch'ler de geçsin
    env = {**os.environ, "TAMGA_FP_DIRS": os.pathsep.join(map(str, library.FP_DIRS)),
           "PYTHONUTF8": "1"}  # Windows: KiCad Python varsayılanı cp1252, JSON/dosyalar UTF-8
    try:
        proc = subprocess.run(
            [KICAD_PYTHON, str(WORKER), cmd, json.dumps(args)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:  # ToolError değilse model sadece "Error executing tool" görür
        raise KicadError(f"worker '{cmd}' timed out after {timeout}s") from None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(MARK):
            res = json.loads(line[len(MARK):])
            if not res["ok"]:
                raise KicadError(res["error"])
            return res["result"]
    tail = (proc.stderr or proc.stdout)[-800:]
    raise KicadError(f"worker '{cmd}' crashed (exit {proc.returncode}): {tail}")


def _clear_stale_locks(args: tuple[str, ...]) -> None:
    """Çöken kicad-cli '~x.kicad_pro.lck' bırakabiliyor; aynı klasördeki kilitleri sil."""
    for a in args:
        p = Path(a)
        if p.suffix in (".kicad_pcb", ".kicad_sch") and p.parent.is_dir():
            for lck in p.parent.glob("~*.lck"):
                lck.unlink(missing_ok=True)


def cli(*args: str, timeout: int = 300, ok_codes: tuple[int, ...] = (0,),
        retries: int = 2) -> subprocess.CompletedProcess:
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run([KICAD_CLI, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise KicadError(f"kicad-cli {' '.join(args[:3])} timed out after {timeout}s") from None
        # Negatif kod = sinyal (KiCad 10.0.7'de aralıklı 'double free' çökmesi görüldü): tekrar dene
        if proc.returncode >= 0 or attempt == retries:
            break
        _clear_stale_locks(args)
    if proc.returncode not in ok_codes:
        # wx debug satırlarını at, gerçek hatayı göster
        err = "\n".join(l for l in (proc.stderr + proc.stdout).splitlines() if "Debug:" not in l)
        raise KicadError(f"kicad-cli {' '.join(args[:3])} failed ({proc.returncode}): {err[-800:]}")
    return proc


def drc_json(pcb: str, out_json: str, refill: bool = False, parity: bool = False) -> dict:
    args = ["pcb", "drc", "--format", "json", "--severity-error", "--severity-warning",
            "--exit-code-violations", "-o", out_json]
    if refill:
        args.append("--refill-zones")
    if parity:
        args.append("--schematic-parity")
    # exit 5 = ihlal bulundu (normal durum), diğer sıfır olmayanlar gerçek hata
    cli(*args, pcb, ok_codes=(0, 5))
    return json.loads(Path(out_json).read_text(encoding="utf-8"))


def erc_json(sch: str, out_json: str) -> dict:
    cli("sch", "erc", "--format", "json", "--severity-error", "--severity-warning",
        "--exit-code-violations", "-o", out_json, sch, ok_codes=(0, 5))
    return json.loads(Path(out_json).read_text(encoding="utf-8"))
