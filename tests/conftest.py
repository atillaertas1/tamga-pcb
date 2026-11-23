import pytest
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
