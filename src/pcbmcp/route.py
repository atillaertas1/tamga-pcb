"""Freerouting ile otomatik rota: PCB -> DSN -> freerouting (GUI'siz) -> SES -> PCB.

kicad-cli 10'da DSN export/SES import YOK; bu yüzden pcbnew worker'ı kullanılıyor.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .kicad import KicadError, run_worker

JAVA = os.environ.get("PCBMCP_JAVA", "java")
DEFAULT_JAR = Path(__file__).resolve().parents[2] / "tools" / "freerouting.jar"


def jar_path() -> Path:
    p = Path(os.environ.get("FREEROUTING_JAR", DEFAULT_JAR)).expanduser()
    if not p.is_file():
        raise KicadError(
            f"Freerouting jar not found at {p}. Download freerouting-<ver>.jar from "
            "https://github.com/freerouting/freerouting/releases to tools/freerouting.jar "
            "or set FREEROUTING_JAR."
        )
    return p


def autoroute(pcb: str, out: str | None = None, max_passes: int = 100,
              ignore_netclasses: list[str] | None = None, timeout: int = 900) -> dict:
    jar = jar_path()
    out = out or str(Path(pcb).with_name(Path(pcb).stem + "_routed.kicad_pcb"))
    work = Path(tempfile.mkdtemp(prefix="pcbmcp-route-"))
    dsn, ses = work / "board.dsn", work / "board.ses"
    try:
        run_worker("export_dsn", {"pcb": pcb, "dsn": str(dsn)})
        cmd = [JAVA, "-jar", str(jar), "-de", str(dsn), "-do", str(ses),
               "--gui.enabled=false", f"--router.autorouter.max_passes={max_passes}",
               "--logging.file.enabled=false", "-da"]
        if ignore_netclasses:
            cmd += ["-inc", ",".join(ignore_netclasses)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if not ses.is_file():
            raise KicadError(f"freerouting produced no SES (exit {proc.returncode}): "
                             f"{(proc.stderr or proc.stdout)[-800:]}")
        res = run_worker("import_ses", {"pcb": pcb, "ses": str(ses), "out": out})
        res["note"] = "Run run_drc on the routed board; review power/critical nets by eye."
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)
