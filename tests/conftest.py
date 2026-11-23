import os
import tempfile
from pathlib import Path

# Testler kullanıcının veri dizinini (içe aktarılmış tamga_lcsc, canlı günlük...) görmesin: geçici veri dizini.
# Freerouting jar'ı yeniden indirilmesin diye gerçek yerinden kullanılır. tamga_pcb'nin geri kalanı
# (library, route, server) DATA_DIR'i import anında okuduğu için bu, diğer importlardan ÖNCE olmalı.
from tamga_pcb import paths as _paths  # noqa: E402

if not os.environ.get("FREEROUTING_JAR") and (_paths.DATA_DIR / "freerouting.jar").is_file():
    os.environ["FREEROUTING_JAR"] = str(_paths.DATA_DIR / "freerouting.jar")
_paths.DATA_DIR = Path(tempfile.mkdtemp(prefix="tamga-test-data-"))
os.environ["TAMGA_DATA_DIR"] = str(_paths.DATA_DIR)

import pytest  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError

from tamga_pcb import pinout


@pytest.fixture(autouse=True)
def _no_easyeda(monkeypatch, tmp_path_factory):
    """Testler EasyEDA'ya çıkmaz ve kullanıcının cache'ine dokunmaz (gerekirse test override eder)."""
    def offline(lcsc):
        raise ToolError("easyeda offline in tests")
    monkeypatch.setattr(pinout, "_fetch", offline)
    monkeypatch.setattr(pinout, "CACHE", tmp_path_factory.mktemp("cache"))
    monkeypatch.setenv("TAMGA_STEP_LOG", "0")  # testler kullanıcının canlı izleme günlüğüne yazmasın
