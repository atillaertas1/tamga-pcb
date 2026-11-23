"""tamga watch: canlı günlükten adımlar, kart/şematik SVG'leri, HTTP uçları."""
import json
import socket
import time
import urllib.request

from kicad_env import needs_kicad

from tamga_pcb import design, watch


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _state(url, want, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = json.loads(urllib.request.urlopen(url + "state", timeout=3).read())
        if want(st):
            return st
        time.sleep(0.2)
    raise AssertionError(f"timed out: {st}")


def test_steps_from_live_log_filtered_by_project(tmp_path, monkeypatch):
    log = tmp_path / "live" / "steps.jsonl"
    log.parent.mkdir()
    monkeypatch.setattr(watch, "LIVE_LOG", log)
    proj, other = tmp_path / "proj", tmp_path / "other"
    proj.mkdir()
    url, new = watch.start(proj, _port())
    assert new
    now = time.time()
    rows = [{"t": now - 3600, "tool": "old", "project": str(proj)},      # izleme başlamadan önce
            {"t": now, "tool": "search_parts", "project": None},           # proje henüz yok: göster
            {"t": now, "tool": "elsewhere", "project": str(other)},        # başka proje: gizle
            {"t": now, "tool": "run_drc", "project": str(proj), "result": {"verdict": "CLEAN"}}]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    st = _state(url, lambda s: len(s["steps"]) >= 2)
    assert [s["tool"] for s in st["steps"]] == ["search_parts", "run_drc"]
    page = urllib.request.urlopen(url, timeout=3).read().decode()
    assert "EventSource" in page and "tamga watch" in page


@needs_kicad
def test_board_and_schematic_are_rendered(tmp_path, monkeypatch):
    from test_design import LED_SPEC
    monkeypatch.setattr(watch, "LIVE_LOG", tmp_path / "none.jsonl")
    design.sync(str(tmp_path / "p"), "led", LED_SPEC, None)
    url, _ = watch.start(tmp_path / "p", _port())
    st = _state(url, lambda s: s["layers"] and s["pages"], timeout=30)
    assert {"F.Cu", "Edge.Cuts"} <= set(st["layers"]) and st["pcb"] == "led.kicad_pcb"
    svg = urllib.request.urlopen(url + "board/F.Cu.svg", timeout=3).read().decode()
    assert "<svg" in svg and "#c83434" in svg   # KiCad rengiyle boyanmış
