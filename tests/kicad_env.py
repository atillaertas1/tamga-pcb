"""Testlerin neyin kurulu olduğunu tek yerden öğrenmesi (KiCad'siz CI'da toplama aşaması çökmesin)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pcbmcp import library, paths, route


def _runs(*cmd: str) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


HAVE_CLI = shutil.which(paths.KICAD_CLI) is not None
HAVE_LIBS = library.SYM_DIR.is_dir()
HAVE_PCBNEW = _runs(paths.KICAD_PYTHON, "-c", "import pcbnew")
JAR = Path(os.environ.get("FREEROUTING_JAR", route.DEFAULT_JAR)).expanduser()
HAVE_ROUTER = JAR.is_file() and shutil.which(route.JAVA) is not None

needs_kicad = pytest.mark.skipif(not (HAVE_CLI and HAVE_LIBS and HAVE_PCBNEW), reason="KiCad not installed")
needs_cli = pytest.mark.skipif(not (HAVE_CLI and HAVE_LIBS), reason="kicad-cli / libraries not installed")
needs_libs = pytest.mark.skipif(not HAVE_LIBS, reason="KiCad libraries not installed")
needs_router = pytest.mark.skipif(not HAVE_ROUTER, reason="freerouting.jar / java missing")
