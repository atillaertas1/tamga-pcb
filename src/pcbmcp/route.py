"""Freerouting ile otomatik rota: PCB -> DSN -> freerouting (GUI'siz) -> SES -> PCB.

kicad-cli 10'da DSN export/SES import YOK; bu yüzden pcbnew worker'ı kullanılıyor.
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import paths
from .kicad import KicadError, run_worker

JAVA = os.environ.get("PCBMCP_JAVA", "java")
DEFAULT_JAR = paths.DATA_DIR / "freerouting.jar"


def jar_path() -> Path:
    """Freerouting jar. Varsayılan yerde yoksa sabit sürümü bir kez indirir (plugin kullanıcısı
    doctor çalıştırmak zorunda kalmasın); FREEROUTING_JAR açıkça verilmişse indirmez."""
    explicit = os.environ.get("FREEROUTING_JAR")
    p = Path(explicit or DEFAULT_JAR).expanduser()
    if not p.is_file() and not explicit:
        from . import doctor  # doctor route'u import ediyor; döngü olmasın
        try:
            doctor.download_jar(p)
        except Exception as e:  # ağ yok, GitHub erişilemez...
            raise KicadError(f"Freerouting jar missing and download failed ({e}). Download "
                             f"{doctor.FREEROUTING_URL} to {p} or set FREEROUTING_JAR.") from None
    if not p.is_file():
        raise KicadError(f"Freerouting jar not found at {p} (FREEROUTING_JAR). Unset it to download "
                         "automatically, or run `pcbmcp doctor --fix`.")
    return p


# "Auto-routing pass #3 on board '..' was completed in 0.14 seconds with score 1000.00 (0 unrouted and 0 violations)."
_PASS = re.compile(r"(Fanout|Auto-routing|Optimizer) pass #(\d+) .*?\((\d+) unrouted and (\d+) violations\)")


class Progress:
    """Freerouting log'undan son durum: aşama, geçiş no, unrouted, violations."""

    def __init__(self, on_update: Callable[[str, int], None] | None = None):
        self.stage, self.n, self.unrouted, self.violations = "starting", 0, None, None
        self.routing_passes = self.optimizer_passes = 0
        self.tail: list[str] = []
        self._on_update = on_update

    def feed(self, line: str) -> None:
        self.tail = (self.tail + [line.rstrip()])[-20:]
        m = _PASS.search(line)
        if not m:
            return
        self.stage, self.n = m.group(1).lower(), int(m.group(2))
        self.unrouted, self.violations = int(m.group(3)), int(m.group(4))
        if self.stage == "auto-routing":
            self.routing_passes = self.n
        elif self.stage == "optimizer":
            self.optimizer_passes = self.n
        if self._on_update:
            self._on_update(self.summary(), self.routing_passes)

    def summary(self) -> str:
        if self.unrouted is None:
            return self.stage
        return f"{self.stage} pass {self.n}: {self.unrouted} unrouted, {self.violations} violations"


def autoroute(pcb: str, out: str | None = None, max_passes: int = 100,
              ignore_netclasses: list[str] | None = None, timeout: int = 900,
              on_progress: Callable[[str, int], None] | None = None) -> dict:
    """on_progress(message, routing_pass) her geçişte çağrılır (MCP progress bildirimi için)."""
    jar = jar_path()
    # Varsayılan: kartın kendisine yaz (import_ses yedek alır). Ayrı bir <ad>_routed.kicad_pcb
    # yazmak tehlikeliydi: KiCad kuralları AYNI ADLI .kicad_pro/.kicad_dru'dan okur, o dosyada DRC
    # varsayılan kurallarla koşup netclass'ları ve özel kuralları (ör. izolasyon) sessizce atlıyordu.
    out = out or pcb
    work = Path(tempfile.mkdtemp(prefix="pcbmcp-route-"))
    dsn, ses = work / "board.dsn", work / "board.ses"
    try:
        run_worker("export_dsn", {"pcb": pcb, "dsn": str(dsn)})
        cmd = [JAVA, "-jar", str(jar), "-de", str(dsn), "-do", str(ses),
               "--gui.enabled=false", f"--router.autorouter.max_passes={max_passes}",
               "--logging.file.enabled=false", "-da"]
        if ignore_netclasses:
            cmd += ["-inc", ",".join(ignore_netclasses)]
        # Çıktıyı canlı oku: ilerleme + zaman aşımında "nerede kaldı" bilgisi.
        # Freerouting'in süre sınırı yok ve SES'i sadece sonda yazar -> kesilirse rota kaybolur.
        # on_progress ÇAĞIRANIN thread'inde çağrılmalı (MCP: anyio.from_thread sadece aracın
        # worker thread'inden çalışır) -> okuyucu thread kuyruğa yazar, burada boşaltılır.
        updates: queue.Queue = queue.Queue()
        prog = Progress(lambda msg, n: updates.put((msg, n)))
        t0 = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        reader = threading.Thread(target=lambda: [prog.feed(l) for l in proc.stdout], daemon=True)
        reader.start()

        def drain():
            while not updates.empty():
                msg, n = updates.get_nowait()
                if on_progress:
                    on_progress(msg, n)

        while proc.poll() is None and time.monotonic() - t0 < timeout:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            drain()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            raise KicadError(
                f"freerouting timed out after {timeout}s at {prog.summary()}; nothing was written. "
                f"Retry with fewer max_passes (now {max_passes}), a larger timeout_s, or route "
                f"critical/power nets first and exclude their netclass (ignore_netclasses).")
        reader.join(timeout=5)
        drain()
        if not ses.is_file():
            tail = "\n".join(l for l in prog.tail if "WARNING" not in l)[-800:]
            raise KicadError(f"freerouting produced no SES (exit {proc.returncode}): {tail}")
        res = run_worker("import_ses", {"pcb": pcb, "ses": str(ses), "out": out})
        res["freerouting"] = (f"{prog.routing_passes} routing + {prog.optimizer_passes} optimizer passes "
                              f"in {time.monotonic() - t0:.0f}s")
        res["note"] = "Run run_drc on the routed board; review power/critical nets by eye."
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)
