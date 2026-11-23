"""tamga watch: Claude kartı çizerken tarayıcıdan canlı izle.

    tamga watch [project_dir] [--port 8765] [--no-open]

Her araç çağrısı genel canlı günlüğe (paths.DATA_DIR/live/steps.jsonl) yazılır; bu sunucu onu ve kart/şematik
dosyalarını izler, değişince kicad-cli ile SVG üretir ve tarayıcıya Server-Sent Events ile bildirir.
Klasör verilmezse Claude'un en son çalıştığı projeyi takip eder. Claude'un bağlamına hiçbir şey eklemez.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from . import paths

LIVE_LOG = paths.DATA_DIR / "live" / "steps.jsonl"
# (katman, renk) — KiCad'in varsayılan koyu temasına yakın; alttan üste çizilir
LAYERS = [("B.Cu", "#4d7fc4"), ("F.Cu", "#c83434"), ("F.Courtyard", "#ff26e2"), ("F.Fab", "#afafaf"),
          ("F.Silkscreen", "#f2eda1"), ("Edge.Cuts", "#d0d2cd")]
SKIP = {".tamga_backups", ".tamga", "fab", "lib"}


class State:
    def __init__(self, root: Path | None):
        self.root = root.resolve() if root else None
        self.started = time.time()
        self.project: Path | None = self.root
        self.pcb: Path | None = None
        self.board: dict[str, str] = {}
        self.pages: dict[str, str] = {}
        self.steps: list[dict] = []
        self.version = 0
        self.cond = threading.Condition()

    def bump(self):
        with self.cond:
            self.version += 1
            self.cond.notify_all()


def _newest(d: Path, pattern: str) -> Path | None:
    files = [f for f in d.rglob(pattern) if not (set(f.relative_to(d).parts[:-1]) & SKIP)]
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def _render_board(pcb: Path) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as t:
        subprocess.run([paths.KICAD_CLI, "pcb", "export", "svg", "--mode-multi", "--layers",
                        ",".join(n for n, _ in LAYERS), "--fit-page-to-board", "--exclude-drawing-sheet",
                        "-o", t, str(pcb)], capture_output=True, timeout=60, check=True)
        out = {}
        for name, color in LAYERS:
            f = Path(t) / f"{pcb.stem}-{name.replace('.', '_')}.svg"
            if f.is_file():
                out[name] = f.read_text(encoding="utf-8").replace("#000000", color)
        return out


def _render_sch(sch: Path) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as t:
        subprocess.run([paths.KICAD_CLI, "sch", "export", "svg", "--exclude-drawing-sheet", "-o", t, str(sch)],
                       capture_output=True, timeout=60, check=True)
        pages = sorted(Path(t).glob("*.svg"), key=lambda f: (f.stem != sch.stem, f.stem))
        return {f.stem.removeprefix(sch.stem + "-") if f.stem != sch.stem else "root": f.read_text(encoding="utf-8")
                for f in pages}


def watcher(st: State, poll: float = 0.5) -> None:
    seen: dict[Path, float] = {}
    log_pos = 0
    while True:
        changed = False
        try:  # yeni adımlar
            if LIVE_LOG.is_file():
                size = LIVE_LOG.stat().st_size
                if size < log_pos:  # döndürüldü
                    log_pos = 0
                if size > log_pos:
                    with open(LIVE_LOG, encoding="utf-8") as f:
                        f.seek(log_pos)
                        chunk = f.read()
                        log_pos = f.tell()
                    for line in chunk.splitlines():
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if row.get("t", 0) < st.started - 2:
                            continue
                        proj = Path(row["project"]) if row.get("project") else None
                        if st.root and proj and st.root not in (proj, *proj.parents) and proj not in st.root.parents:
                            continue  # başka bir projenin adımı
                        if proj and not st.root:
                            st.project = proj
                        st.steps.append(row)
                        changed = True
                    st.steps = st.steps[-500:]
        except OSError:
            pass
        try:  # kart ve şematik
            proj = st.project
            if proj and proj.is_dir():
                pcb = _newest(proj, "*.kicad_pcb")
                if pcb:
                    sch = pcb.with_suffix(".kicad_sch")
                    for f, render, attr in ((pcb, _render_board, "board"), (sch, _render_sch, "pages")):
                        if not f.is_file():
                            continue
                        m = f.stat().st_mtime
                        if seen.get(f) != m and time.time() - m > 0.3:  # yazma bitsin
                            try:
                                setattr(st, attr, render(f))
                                seen[f] = m
                                st.pcb = pcb
                                changed = True
                            except (subprocess.SubprocessError, OSError):
                                pass  # yarım yazılmış dosya: bir sonraki turda tekrar
        except OSError:
            pass
        if changed:
            st.bump()
        time.sleep(poll)


def handler(st: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # sessiz
            pass

        def send(self, body: str | bytes, ctype: str, code: int = 200):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            path = unquote(self.path.split("?")[0])
            if path == "/":
                return self.send(PAGE, "text/html; charset=utf-8")
            if path == "/state":
                return self.send(json.dumps({
                    "version": st.version, "project": st.project.name if st.project else None,
                    "pcb": st.pcb.name if st.pcb else None, "layers": [n for n, _ in LAYERS if n in st.board],
                    "pages": list(st.pages), "steps": st.steps[-300:]}), "application/json")
            if path.startswith("/board/"):
                svg = st.board.get(path[len("/board/"):].removesuffix(".svg"))
                return self.send(svg, "image/svg+xml") if svg else self.send("", "text/plain", 404)
            if path.startswith("/sch/"):
                svg = st.pages.get(path[len("/sch/"):].removesuffix(".svg"))
                return self.send(svg, "image/svg+xml") if svg else self.send("", "text/plain", 404)
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last = -1
                try:
                    while True:
                        with st.cond:
                            if st.version == last:
                                st.cond.wait(timeout=15)
                            v = st.version
                        self.wfile.write(f"data: {v}\n\n".encode() if v != last else b": ping\n\n")
                        self.wfile.flush()
                        last = v
                except (BrokenPipeError, ConnectionResetError):
                    return
            return self.send("not found", "text/plain", 404)
    return H


def start(root: Path | None = None, port: int = 8765) -> tuple[str, bool]:
    """Sunucuyu arka plan iş parçacığında başlat (MCP sürecinin içinden de). -> (url, yeni mi)"""
    url = f"http://127.0.0.1:{port}/"
    if port in _RUNNING:
        return url, False
    st = State(root)
    threading.Thread(target=watcher, args=(st,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _RUNNING[port] = srv
    return url, True


_RUNNING: dict[int, ThreadingHTTPServer] = {}


def main(argv: list[str]) -> int:
    port, open_browser, root = 8765, True, None
    it = iter(argv)
    for a in it:
        if a == "--port":
            port = int(next(it))
        elif a == "--no-open":
            open_browser = False
        else:
            root = Path(a).expanduser()
    url, _ = start(root, port)
    print(f"tamga watch: {url}  (following {'the latest project Claude works on' if not root else root})",
          flush=True)
    print("Ctrl+C to stop", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tamga watch</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--dim:#8b949e;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--copper:#d9894a}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:10px 16px;border-bottom:1px solid var(--line)}
header b{color:var(--copper);letter-spacing:.02em}.pill{padding:2px 10px;border-radius:99px;background:var(--panel);border:1px solid var(--line);color:var(--dim);font-size:12px}
.pill.ok{color:var(--ok);border-color:#238636}.pill.bad{color:var(--bad);border-color:#da3633}
.live{margin-left:auto;display:flex;align-items:center;gap:6px;color:var(--dim);font-size:12px}.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--ok);box-shadow:0 0 0 0 rgba(63,185,80,.6);animation:pulse 1.6s infinite}@keyframes pulse{70%{box-shadow:0 0 0 8px rgba(63,185,80,0)}}
main{display:grid;grid-template-columns:1fr 380px;height:calc(100% - 49px)}
.view{display:flex;flex-direction:column;min-width:0}.tabs{display:flex;gap:4px;padding:8px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap;align-items:center}
.tabs button{background:none;border:1px solid transparent;color:var(--dim);padding:4px 10px;border-radius:6px;cursor:pointer;font:inherit}
.tabs button.on{color:var(--text);border-color:var(--line);background:var(--panel)}.tabs label{color:var(--dim);font-size:12px;margin-left:8px;cursor:pointer}
.stage{flex:1;position:relative;overflow:auto;padding:20px;display:flex;align-items:center;justify-content:center}
#board{position:relative;width:min(100%,1100px);background:#001023;border-radius:6px;box-shadow:0 0 0 1px var(--line)}
#board img{position:absolute;inset:0;width:100%;height:100%}#board img.base{position:relative}#board img.cu{opacity:.78}
#sch{max-width:100%;background:#f5f4ef;border-radius:6px;display:none}
.empty{color:var(--dim);text-align:center}
aside{border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0}aside h2{font-size:13px;margin:0;padding:12px 14px;border-bottom:1px solid var(--line);color:var(--dim);font-weight:600}
#steps{list-style:none;margin:0;padding:0;overflow:auto;flex:1}
#steps li{padding:8px 14px;border-bottom:1px solid #21262d;animation:in .4s ease}@keyframes in{from{background:#1f2a1f}}
#steps .t{color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums}#steps .n{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--copper)}
#steps .r{color:var(--dim);font-size:12px;word-break:break-word}#steps .err .r{color:var(--bad)}
@media (max-width:900px){main{grid-template-columns:1fr;grid-template-rows:1fr 40%}aside{border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header><b>tamga watch</b><span id="proj" class="pill">waiting for Claude…</span><span id="drc" class="pill">DRC –</span>
<span id="unr" class="pill">unrouted –</span><span class="live"><span id="dot" class="dot"></span><span id="ago">connecting</span></span></header>
<main><section class="view"><div class="tabs" id="tabs"></div><div class="stage">
<div id="board"><p class="empty" style="padding:80px 20px">The board appears here as soon as Claude creates it.</p></div>
<img id="sch" alt="schematic"></div></section>
<aside><h2>Steps</h2><ul id="steps"></ul></aside></main>
<script>
let view="board", page=null, hidden=new Set(["F.Fab"]), lastStep=0, state=null;
const $=id=>document.getElementById(id);
function fmt(t){return new Date(t*1000).toLocaleTimeString()}
function res(s){if(s.error)return s.error;return Object.entries(s.result||{}).map(([k,v])=>k+" "+v).join(" · ")}
function tabs(){const el=$("tabs");el.innerHTML="";
 const add=(label,on,fn)=>{const b=document.createElement("button");b.textContent=label;b.className=on?"on":"";b.onclick=fn;el.appendChild(b)};
 add("Board",view=="board",()=>{view="board";render()});
 (state.pages||[]).forEach(p=>add("Schematic: "+p,view=="sch"&&page==p,()=>{view="sch";page=p;render()}));
 if(view=="board")(state.layers||[]).forEach(l=>{const lb=document.createElement("label");const c=document.createElement("input");
  c.type="checkbox";c.checked=!hidden.has(l);c.onchange=()=>{c.checked?hidden.delete(l):hidden.add(l);render()};lb.append(c," "+l);el.appendChild(lb)})}
function render(){if(!state)return;tabs();const v=state.version;
 $("board").style.display=view=="board"?"block":"none";$("sch").style.display=view=="sch"?"block":"none";
 if(view=="board"&&state.layers.length){const b=$("board");b.innerHTML="";
  state.layers.filter(l=>!hidden.has(l)).forEach((l,i)=>{const im=new Image();im.src="/board/"+encodeURIComponent(l)+".svg?v="+v;im.className=(i==0?"base ":"")+(l.endsWith(".Cu")?"cu":"");b.appendChild(im)})}
 if(view=="sch"&&page)$("sch").src="/sch/"+encodeURIComponent(page)+".svg?v="+v}
function steps(){const ul=$("steps");for(const s of state.steps){if(s.t<=lastStep)continue;lastStep=s.t;
  const li=document.createElement("li");if(s.error)li.className="err";
  li.innerHTML=`<div><span class="n"></span> <span class="t"></span></div><div class="r"></div>`;
  li.querySelector(".n").textContent=s.tool;li.querySelector(".t").textContent=fmt(s.t)+" · "+(s.ms/1000).toFixed(1)+" s";
  li.querySelector(".r").textContent=res(s);ul.appendChild(li);ul.scrollTop=ul.scrollHeight;
  if(s.tool=="run_drc"&&s.result){const v=s.result.verdict;$("drc").textContent="DRC "+(v||((s.result.total||"?")+" issues"));$("drc").className="pill "+(v=="CLEAN"?"ok":"bad")}
  const u=s.result&&(s.result.unrouted_after??s.result.unrouted);if(u!==undefined){$("unr").textContent="unrouted "+u;$("unr").className="pill "+(u=="0"?"ok":"")}}}
async function load(){state=await (await fetch("/state")).json();
 if(state.project)$("proj").textContent=state.project+(state.pcb?" / "+state.pcb:"");steps();render()}
// ?static=1: canlı bağlantı yok (ekran görüntüsü / replay için tarayıcı yüklemenin bitmesini bekleyebilsin)
if(!location.search.includes("static")){const es=new EventSource("/events");
 es.onmessage=()=>{load();$("ago").textContent="updated "+new Date().toLocaleTimeString()};
 es.onopen=()=>{$("dot").className="dot on";$("ago").textContent="live"};es.onerror=()=>{$("dot").className="dot";$("ago").textContent="reconnecting"}}
else{$("dot").className="dot on";$("ago").textContent="replay"}
load();
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
