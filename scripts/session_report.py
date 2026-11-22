"""Turn a Claude Code `--output-format stream-json --verbose` log into a metrics JSON and a Markdown
report with tables and Mermaid charts (GitHub renders them, no image dependencies).

    uv run python scripts/session_report.py <session.jsonl> <out_dir> [--prompt-file PROMPT.txt]
        [--wall-seconds N] [--title "..."]

Writes <out_dir>/session-metrics.json and <out_dir>/SESSION.md. Absolute paths are replaced by
<project> / <repo> so the report can be published.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MCP_PREFIX = re.compile(r"^mcp__plugin_[^_]+(?:-[^_]+)*_[^_]+__")


def short(name: str) -> str:
    return MCP_PREFIX.sub("", name)


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def scrub(s: str, project: Path) -> str:
    s = s.replace(str(project), "<project>").replace(str(REPO), "<repo>")
    return re.sub(r"/(?:home|Users)/[^/\s\"']+", "~", s)


def parse(path: Path, project: Path) -> dict:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    init = next((r for r in records if r.get("type") == "system" and r.get("subtype") == "init"), {})
    result = next((r for r in reversed(records) if r.get("type") == "result"), {})

    calls: "OrderedDict[str, dict]" = OrderedDict()   # one entry per API call (message id)
    tool_uses: dict[str, dict] = {}
    steps: list[dict] = []
    for r in records:
        if r.get("type") == "assistant":
            m = r["message"]
            calls[m["id"]] = m.get("usage", {}) or calls.get(m["id"], {})
            for c in m.get("content", []):
                if c.get("type") == "tool_use":
                    tool_uses[c["id"]] = {"name": short(c["name"]), "input": c.get("input", {}), "call": m["id"]}
        elif r.get("type") == "user":
            content = r["message"].get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if c.get("type") != "tool_result":
                    continue
                use = tool_uses.get(c.get("tool_use_id"), {"name": "?", "input": {}})
                out = text_of(c.get("content"))
                steps.append({"tool": use["name"], "chars": len(out), "error": bool(c.get("is_error")),
                              "input": scrub(json.dumps(use["input"], ensure_ascii=False), project)[:160],
                              "result": scrub(out.replace("\n", " "), project)[:160]})

    per_call = []
    for i, (mid, u) in enumerate(calls.items(), 1):
        ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        per_call.append({"call": i, "context_tokens": ctx, "output_tokens": u.get("output_tokens", 0)})

    usage = result.get("usage", {})
    tools = Counter(s["tool"] for s in steps)
    result_chars = Counter()
    for s in steps:
        result_chars[s["tool"]] += s["chars"]
    return {
        "model": init.get("model"),
        "claude_code_version": init.get("claude_code_version"),
        "tools_available": len(init.get("tools", [])),
        "mcp_servers_loaded": len(init.get("mcp_servers", [])),
        "num_turns": result.get("num_turns"),
        "api_calls": len(per_call),
        "tool_calls": sum(tools.values()),
        "tool_errors": sum(1 for s in steps if s["error"]),
        "tools": dict(tools.most_common()),
        "tool_result_chars": dict(result_chars.most_common()),
        "tokens": {
            "input": usage.get("input_tokens", 0),
            "cache_write": usage.get("cache_creation_input_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        },
        "cost_usd": result.get("total_cost_usd"),
        "duration_s": round((result.get("duration_ms") or 0) / 1000),
        "api_duration_s": round((result.get("duration_api_ms") or 0) / 1000),
        "is_error": result.get("is_error"),
        "final_message": scrub(result.get("result") or "", project),
        "per_call": per_call,
        "steps": steps,
    }


def fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def report(m: dict, prompt: str, title: str, appended: str = "") -> str:
    t = m["tokens"]
    total_in = t["input"] + t["cache_write"] + t["cache_read"]
    peak = max((c["context_tokens"] for c in m["per_call"]), default=0)
    pc = m["per_call"]
    growth = round((pc[-1]["context_tokens"] - pc[0]["context_tokens"]) / max(1, len(pc) - 1) / 1000, 1) if pc else 0
    tools = list(m["tools"].items())
    xs = ", ".join(str(c["call"]) for c in m["per_call"])
    ys = ", ".join(str(round(c["context_tokens"] / 1000, 1)) for c in m["per_call"])
    lines = [
        f"# {title}", "",
        "Measured from a real, unattended Claude Code session: one prompt in, a DRC-clean board and fab files out.",
        "Numbers come from the session's own `stream-json` log, turned into this page by "
        "[`scripts/session_report.py`](../../scripts/session_report.py).", "",
        "## The prompt", "", "> " + prompt.strip().replace("\n", "\n> "), "",
        "## At a glance", "",
        "| | |", "| --- | --- |",
        "| Prompts from the user | **1** |",
        f"| Model | `{m['model']}` |",
        f"| Agent turns | {m['num_turns']} |",
        f"| API calls | {m['api_calls']} |",
        f"| Tool calls | {m['tool_calls']} ({m['tool_errors']} returned an error) |",
        f"| Wall time | {m['duration_s'] // 60} min {m['duration_s'] % 60} s |",
        f"| Tokens read (input + cache) | {fmt(total_in)} |",
        f"| of which served from cache | {fmt(t['cache_read'])} ({round(100 * t['cache_read'] / total_in) if total_in else 0} %) |",
        f"| Tokens written (output) | {fmt(t['output'])} |",
        f"| Largest context in a single call | {fmt(peak)} tokens |",
        f"| API cost (list price) | ${m['cost_usd']:.2f} |" if m.get("cost_usd") is not None else "| API cost | n/a |",
        "",
        "## Context per API call", "",
        f"How much the model had in front of it at each step, in thousand tokens. It starts at "
        f"{round(m['per_call'][0]['context_tokens'] / 1000, 1) if m['per_call'] else 0}k (system prompt, tools, skill) "
        f"and grows by about {growth}k per call, because tools answer with one-line summaries instead of KiCad files. "
        "Each call re-reads the whole context, which is why the total above runs into millions; "
        "almost all of it comes from the prompt cache at a tenth of the price.", "",
        "```mermaid", "xychart-beta", '    title "Context size per API call (k tokens)"',
        f"    x-axis [{xs}]", f'    y-axis "k tokens" 0 --> {max(10, round(peak / 1000) + 5)}', f"    line [{ys}]", "```", "",
        "## Where the tokens went", "",
        "```mermaid", "pie showData", '    title "Tokens by kind"',
        f'    "Cache read" : {t["cache_read"]}', f'    "Cache write" : {t["cache_write"]}',
        f'    "Fresh input" : {t["input"]}', f'    "Output" : {t["output"]}', "```", "",
        "## Tool calls", "",
        "```mermaid", "xychart-beta", '    title "Calls per tool"',
        "    x-axis [" + ", ".join(f'"{n}"' for n, _ in tools) + "]",
        f'    y-axis "calls" 0 --> {max([c for _, c in tools] + [1]) + 1}',
        "    bar [" + ", ".join(str(c) for _, c in tools) + "]", "```", "",
        "| Tool | Calls | Result size, total | per call |", "| --- | ---: | ---: | ---: |",
    ]
    for name, n in tools:
        ch = m["tool_result_chars"].get(name, 0)
        lines.append(f"| `{name}` | {n} | {fmt(ch)} chars | {fmt(round(ch / n))} |")
    lines += ["", "## Step by step", "", "<details>", "<summary>Every tool call, in order</summary>", "",
              "| # | Tool | Input (start) | Result (start) |", "| ---: | --- | --- | --- |"]
    for i, s in enumerate(m["steps"], 1):
        cell = lambda v: v.replace("|", "\\|")  # noqa: E731
        lines.append(f"| {i} | `{s['tool']}`{' ⚠' if s['error'] else ''} | {cell(s['input'])} | {cell(s['result'])} |")
    lines += ["", "</details>", "", "## Claude's final message", "", m["final_message"].strip(), "",
              "## Environment", "",
              f"Claude Code {m['claude_code_version']}, run headless with `claude -p` in the author's environment: "
              f"{m['tools_available']} tools and {m['mcp_servers_loaded']} MCP servers were loaded (most of them "
              "unrelated connectors, kept deferred), so a clean install starts from a slightly smaller context. "
              "Only tamga-pcb tools and file read/write were allowed.", ""]
    if appended:
        lines += ["The author's global instructions ask Claude to pause for approval after every step; to measure an "
                  "unattended run, this was appended to the system prompt (the user prompt above is unchanged):", "",
                  "> " + appended.strip(), ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--prompt-file", type=Path)
    ap.add_argument("--title", default="Session report")
    ap.add_argument("--appended-file", type=Path)
    a = ap.parse_args()
    m = parse(a.log, a.out_dir.resolve())
    prompt = a.prompt_file.read_text(encoding="utf-8") if a.prompt_file else ""
    (a.out_dir / "session-metrics.json").write_text(
        json.dumps({k: v for k, v in m.items() if k != "steps"}, indent=1) + "\n", encoding="utf-8")
    appended = a.appended_file.read_text(encoding="utf-8") if a.appended_file else ""
    (a.out_dir / "SESSION.md").write_text(report(m, prompt, a.title, appended), encoding="utf-8")
    print(json.dumps({k: m[k] for k in ("model", "num_turns", "api_calls", "tool_calls", "tokens", "cost_usd",
                                         "duration_s")}, indent=1))


if __name__ == "__main__":
    main()
