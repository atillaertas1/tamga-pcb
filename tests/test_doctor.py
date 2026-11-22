"""tamga doctor: sürüm tespiti ve çıkış kodu (ağsız)."""
import struct
import sys
import zipfile

import httpx
import pytest

from tamga_pcb import doctor


def test_jar_java_version_from_class_file(tmp_path):
    jar = tmp_path / "x.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("app/Main.class", b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 69))  # 69 -> Java 25
    assert doctor.jar_java_version(jar) == 25
    (tmp_path / "bad.jar").write_text("not a zip")
    assert doctor.jar_java_version(tmp_path / "bad.jar") is None


@pytest.mark.parametrize("banner,want", [('openjdk version "27.0.1" 2026-10-24', 27),
                                         ('java version "1.8.0_402"', 8)])
def test_java_version_parsing(tmp_path, banner, want):
    if sys.platform == "win32":
        pytest.skip("shebang script cannot stand in for java on Windows")
    java = tmp_path / "java"
    java.write_text(f"#!{sys.executable}\nimport sys\nprint({banner!r}, file=sys.stderr)\n")
    java.chmod(0o755)
    assert doctor.java_version(str(java)) == want


def test_missing_kicad_fails_with_hint(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "KICAD_CLI", "/nonexistent/kicad-cli")
    monkeypatch.setattr(doctor.httpx, "get", lambda *a, **k: httpx.Response(200))
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "✗ kicad-cli" in out and "TAMGA_KICAD_CLI" in out and "required checks failed" in out
