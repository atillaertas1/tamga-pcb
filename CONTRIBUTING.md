# Contributing to tamga-pcb

Thanks for helping. Bug reports, part fixes, new tools, docs and example boards are all welcome.

- **Questions or ideas:** start a thread in [Discussions](https://github.com/atillaertas1/tamga-pcb/discussions).
- **Bugs and feature requests:** open an [issue](https://github.com/atillaertas1/tamga-pcb/issues/new/choose); the
  forms ask for what we need.
- **First contribution?** Pick an issue labelled
  [`good first issue`](https://github.com/atillaertas1/tamga-pcb/labels/good%20first%20issue) and leave a comment so
  nobody else starts on it.

## Set up

You need [uv](https://docs.astral.sh/uv/). [KiCad 10](https://www.kicad.org/download/) is only needed for the tests
that build real boards, and Java 25+ only for autorouting.

```bash
git clone https://github.com/atillaertas1/tamga-pcb && cd tamga-pcb
uv sync --extra lcsc
uv run tamga doctor       # what tamga-pcb can and can't find on your machine; --fix downloads Freerouting
claude --plugin-dir .     # run Claude Code with your working copy as the plugin
```

## Run the tests

```bash
uv run pytest -q -rs
```

Tests that need KiCad or Freerouting are **skipped** when those aren't installed (`-rs` lists them), so you can work on
most of the code without KiCad. CI runs the rest:

| CI job | Where | What |
|---|---|---|
| `unit` | Linux, macOS, Windows × Python 3.11 / 3.13 | everything that doesn't need KiCad, in under a minute |
| `kicad` | Linux with KiCad 10 and Java | the full suite: real schematics, boards, DRC/ERC and autoroute (~12 min) |

If your change touches KiCad I/O (`kicad.py`, `worker.py`, `design.py`, `route.py`), run the full suite locally if
you can, or let the `kicad` job tell you. Tests never touch your real data directory; `tests/conftest.py` points it
at a temporary one.

## Where things are

| Path | What it does |
|---|---|
| `src/tamga_pcb/server.py` | the MCP server: every tool, and the `tamga` CLI entry point |
| `design.py`, `schematic.py`, `blocks.py` | circuit spec → schematic and PCB; reusable circuit blocks |
| `kicad.py`, `worker.py` | `kicad-cli` subprocesses and the pcbnew worker that runs in KiCad's own Python |
| `route.py` | Freerouting: PCB → DSN → route → SES → PCB |
| `parts.py`, `importer.py`, `pinout.py`, `partgen.py` | LCSC search and import, pinout checks, generated footprints |
| `library.py`, `paths.py`, `doctor.py` | finding KiCad and its libraries on each OS; the setup check |
| `report.py`, `impedance.py`, `watch.py`, `sexpr.py` | compact DRC/ERC reports, trace widths, live view, S-expression I/O |
| `skills/pcb-design/SKILL.md` | the design workflow Claude follows |
| `examples/`, `docs/` | example boards and benchmarks |

Tool output is read by a model, so keep it short: one-line summaries, not raw KiCad files. That's what keeps a whole
board inside the context window.

## Pull requests

1. Fork, then branch off `main`. `main` is protected, so every change goes through a pull request.
2. Keep one logical change per pull request. Small PRs get reviewed faster.
3. Add or update tests for what you changed.
4. If users will notice the change, add a line to [`CHANGELOG.md`](CHANGELOG.md). If a tool or its behavior changes,
   update `README.md` and `README.tr.md` as well.
5. Fill in the pull request template: what changed and how you tested it (board, KiCad version, OS).

Commit messages are short and say what changed, in the imperative or as a summary, e.g.
`CI: one run per PR, skip docs-only changes` or `Add tamga --version`.

## License

tamga-pcb is MIT licensed. By contributing you agree that your contribution is released under the same
[license](LICENSE).
