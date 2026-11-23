"""Replay a finished session through the tamga watch page and save it as a GIF.

The frames are real: the board states come from the project's .tamga_backups (taken before every write)
plus the final board, the steps from the live log with their original times. Each step is fed to a
watch page in order and screenshotted with headless Chromium.

    uv run python scripts/replay_gif.py <project_dir> <out.gif> [--width 1000] [--port 8790]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tamga_pcb import paths, watch


def snapshots(project: Path, stem: str) -> list[tuple[float, Path]]:
    """(time, file) for every board state: each backup is the state before a write; the last is the final board."""
    out = []
    for f in (project / ".tamga_backups").glob(f"*-{stem}.kicad_pcb"):
        stamp = f.name[: -len(f"-{stem}.kicad_pcb")].split(".")[0]
        out.append((dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp(), f))
    out.sort()
    final = project / f"{stem}.kicad_pcb"
    out.append((final.stat().st_mtime, final))
    return out


def session_steps(project: Path) -> list[dict]:
    """The project's steps plus the project-less ones (part searches) just before them."""
    live = paths.DATA_DIR / "live" / "steps.jsonl"
    rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines() if line.strip()]
    own = [r for r in rows if r.get("project") and Path(r["project"]) == project]
    if not own:
        raise SystemExit(f"no steps for {project} in {live}")
    t0, t1 = own[0]["t"], own[-1]["t"]
    lead = []
    for r in reversed([r for r in rows if r["t"] < t0]):  # contiguous project-less steps before the first
        if r.get("project") is not None or t0 - r["t"] > 900:
            break
        lead.insert(0, r)
    return lead + [r for r in rows if t0 <= r["t"] <= t1 and r.get("project") and Path(r["project"]) == project]


def shot(url: str, png: Path, width: int) -> None:
    subprocess.run(["chromium", "--headless=new", "--disable-gpu", f"--window-size={width},{int(width * 0.574)}",
                    "--virtual-time-budget=8000", f"--screenshot={png}", url + "?static=1"],
                   capture_output=True, timeout=60)


def to_gif(frames: list[tuple[Path, int]], out: Path, tmp: Path, width: int = 1000) -> None:
    """Variable-duration frames -> GIF with ffmpeg's two-pass palette (full frames, no transparency tricks:
    ImageMagick's frame optimisation dropped the page background)."""
    concat = tmp / "frames.txt"
    lines = []
    for png, centis in frames:
        lines += [f"file '{png}'", f"duration {centis / 100:.2f}"]
    lines.append(f"file '{frames[-1][0]}'")  # concat demuxer ignores the last duration otherwise
    concat.write_text("\n".join(lines) + "\n")
    scale = f"scale={width}:-1:flags=lanczos"
    palette = tmp / "palette.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-vf", f"{scale},palettegen=max_colors=128:stats_mode=full", str(palette)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-i", str(palette), "-lavfi", f"{scale}[x];[x][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
                    "-loop", "0", str(out)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    project = a.project.resolve()
    stem = next(project.glob("*.kicad_pcb")).stem
    snaps, steps = snapshots(project, stem), session_steps(project)

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / project.name
        stage.mkdir()
        log = Path(tmp) / "steps.jsonl"
        log.write_text("")
        watch.LIVE_LOG = log
        st = watch.State(stage)
        st.started = 0  # keep the original step times
        threading.Thread(target=watch.watcher, args=(st, 0.2), daemon=True).start()
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), watch.handler(st))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{a.port}/"
        frames, current = [], None
        sch = project / f"{stem}.kicad_sch"
        for i, row in enumerate(steps):
            # board state after this step: the first snapshot taken at or after it
            snap = next((f for t, f in snaps if t >= row["t"] - 0.5), snaps[-1][1]) if row.get("project") else None
            changed = snap is not None and snap != current
            if changed:
                shutil.copyfile(snap, stage / f"{stem}.kicad_pcb")
                if current is None and sch.is_file():
                    shutil.copyfile(sch, stage / sch.name)
                current = snap
            v = st.version
            row = {**row, "project": str(stage)}
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            deadline = time.time() + 20
            while time.time() < deadline:  # wait for the step (and a new board) to land
                state = json.loads(urllib.request.urlopen(url + "state", timeout=3).read())
                if state["version"] > v and len(state["steps"]) > i and (not changed or state["pcb"]):
                    break
                time.sleep(0.2)
            time.sleep(0.6 if changed else 0.2)
            png = Path(tmp) / f"f{i:03d}.png"
            shot(url, png, a.width)
            frames.append((png, 120 if changed else 35))
            print(f"{i + 1}/{len(steps)} {row['tool']}{' (board)' if changed else ''}", flush=True)
        frames[-1] = (frames[-1][0], 500)
        srv.shutdown()
        to_gif(frames, a.out, Path(tmp))
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
