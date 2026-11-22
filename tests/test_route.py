"""autoroute: Freerouting log ayrıştırma, ilerleme ve zaman aşımı (Java/KiCad gerektirmez)."""
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pcbmcp import route

LINE = ("2026-11-18 10:47:36.044 INFO   [E7\\A2] Auto-routing pass #3 on board 'bef2' was completed "
        "in 0.14 seconds with score 990.00 (5 unrouted and 1 violations).")


def test_progress_parses_freerouting_log():
    seen = []
    p = route.Progress(lambda msg, n: seen.append((msg, n)))
    p.feed("2026-11-18 INFO   Freerouting v2.5.0")
    p.feed(LINE)
    p.feed(LINE.replace("Auto-routing", "Optimizer").replace("#3", "#1").replace("5 unrouted", "0 unrouted"))
    assert seen == [("auto-routing pass 3: 5 unrouted, 1 violations", 3),
                    ("optimizer pass 1: 0 unrouted, 1 violations", 3)]
    assert (p.routing_passes, p.optimizer_passes) == (3, 1)


@pytest.fixture
def fake_freerouting(monkeypatch, tmp_path):
    """'java' yerine: bir geçiş satırı yazıp takılan script. SES hiç oluşmaz."""
    if sys.platform == "win32":
        pytest.skip("shebang script cannot stand in for java on Windows")
    script = tmp_path / "java"
    script.write_text(f"#!{sys.executable}\nimport sys, time\nprint({LINE!r}, flush=True)\ntime.sleep(30)\n")
    script.chmod(0o755)
    jar = tmp_path / "freerouting.jar"
    jar.write_text("")
    monkeypatch.setattr(route, "JAVA", str(script))
    monkeypatch.setenv("FREEROUTING_JAR", str(jar))
    monkeypatch.setattr(route, "run_worker", lambda cmd, args: {})


def test_timeout_reports_where_it_stopped(fake_freerouting, tmp_path):
    seen = []
    with pytest.raises(ToolError, match=r"timed out after 2s at auto-routing pass 3: 5 unrouted.*max_passes"):
        route.autoroute(str(tmp_path / "b.kicad_pcb"), timeout=2, on_progress=lambda m, n: seen.append(n))
    assert seen == [3]  # ilerleme, zaman aşımından önce çağıranın thread'inde iletildi


def test_missing_default_jar_is_downloaded_once(monkeypatch, tmp_path):
    from pcbmcp import doctor
    calls = []
    monkeypatch.delenv("FREEROUTING_JAR", raising=False)
    monkeypatch.setattr(route, "DEFAULT_JAR", tmp_path / "freerouting.jar")
    monkeypatch.setattr(doctor, "download_jar", lambda p: (calls.append(p), p.write_bytes(b"jar")))
    assert route.jar_path() == tmp_path / "freerouting.jar"
    route.jar_path()
    assert calls == [tmp_path / "freerouting.jar"]


def test_explicit_jar_is_never_downloaded(monkeypatch, tmp_path):
    from pcbmcp import doctor
    monkeypatch.setenv("FREEROUTING_JAR", str(tmp_path / "nope.jar"))
    monkeypatch.setattr(doctor, "download_jar", lambda p: pytest.fail("must not download"))
    with pytest.raises(ToolError, match="FREEROUTING_JAR"):
        route.jar_path()
