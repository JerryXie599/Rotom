#!/usr/bin/env python3
"""把 worker 的原始 jsonl 日志导出成**可读的 Thought / Action / Observation 轨迹**(Markdown)。

新版规则要求:挑战轮次结束后要上交智能体做复测与审计,需要能调取完整解题轨迹。
pi 的 jsonl 里三类信息都有,但格式是给机器看的;这个脚本把它转成人能直接读的报告。

  python3 tools/export_trace.py                       # 导出全部题目(默认项目)
  python3 tools/export_trace.py --project round1
  python3 tools/export_trace.py --run-dir /path/to/project
  python3 tools/export_trace.py --out traces          # 输出目录

输出:
  <run_dir>/traces/<题目>-w<槽位>.md     每个 agent 一份轨迹
  <run_dir>/traces/SUMMARY.md            全部题目的汇总(含耗时/提交/结果)
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def default_run_dir() -> Path:
    return Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)


def find_run_dir(project: str | None) -> Path:
    if not project or project in ("默认项目", "default"):
        return default_run_dir()
    p = ROOT / "projects" / project
    return p if p.exists() else default_run_dir()


def short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def parse_log(path: Path) -> list[tuple[str, str]]:
    """返回 [(角色, 内容)]:thought / action / observation / result / error。"""
    events: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "message_end":
            msg = ev.get("message") or {}
            role = msg.get("role")
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    if role == "assistant":
                        events.append(("thought", c["text"].strip()))
                    elif role == "user":
                        events.append(("prompt", c["text"].strip()))
                elif c.get("type") == "toolResult":
                    pass
            if role == "toolResult":
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "text":
                        tag = "error" if msg.get("isError") else "observation"
                        events.append((tag, c.get("text", "").strip()))
        elif t == "tool_execution_start":
            args = ev.get("args") or {}
            detail = args.get("command") or args.get("path") or args.get("pattern") or args
            events.append(("action", f"{ev.get('toolName', '?')}  {short(detail, 400)}"))
        elif t == "error":
            events.append(("error", str(ev.get("errorMessage", ""))[:500]))
    return events


def trace_markdown(title: str, qid: str, slot: int, log: Path) -> str:
    events = parse_log(log)
    out = [f"# 解题轨迹:{title} · agent w{slot}",
           f"\n- question_id: `{qid}`\n- 日志文件: `{log.name}`"
           f"\n- 事件数: {len(events)}\n- 导出时间: {time.strftime('%F %T')}\n",
           "\n> 说明:Thought = 模型思考/发言,Action = 工具调用,Observation = 工具输出。\n"]
    for kind, content in events:
        if kind == "thought":
            out.append(f"\n### 🧠 Thought\n\n{content}\n")
        elif kind == "action":
            out.append(f"\n**⚙️ Action** `{content}`\n")
        elif kind == "observation":
            out.append(f"\n<details><summary>👁 Observation ({len(content)} 字符)</summary>\n\n```\n{content[:4000]}\n```\n</details>\n")
        elif kind == "error":
            out.append(f"\n> ⚠️ **Error**: {content}\n")
        elif kind == "prompt":
            out.append(f"\n<details><summary>📄 下发给 agent 的题目 prompt</summary>\n\n```\n{content[:2000]}\n```\n</details>\n")
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="项目名(默认项目/round1/…)")
    ap.add_argument("--run-dir", help="直接指定数据目录")
    ap.add_argument("--out", default="traces", help="输出目录名(相对数据目录)")
    args = ap.parse_args()

    rundir = Path(args.run_dir) if args.run_dir else find_run_dir(args.project)
    logs = rundir / "logs"
    outdir = rundir / args.out
    outdir.mkdir(parents=True, exist_ok=True)

    board = {}
    if (rundir / "board.json").exists():
        try:
            board = json.loads((rundir / "board.json").read_text(encoding="utf-8")).get("questions", {})
        except json.JSONDecodeError:
            pass

    # 同一题同一槽位可能有多次尝试,取最新一份(文件名带时间戳)
    latest: dict[tuple[str, int], Path] = {}
    for p in sorted(logs.glob("*.jsonl")):
        parts = p.stem.split("_")
        if len(parts) < 3 or not parts[-2].startswith("w"):
            continue
        qid, slot = "_".join(parts[:-2]), int(parts[-2][1:])
        latest[(qid, slot)] = p

    rows = []
    for (qid, slot), log in sorted(latest.items()):
        entry = board.get(qid, {})
        title = entry.get("title") or qid
        md = trace_markdown(title, qid, slot, log)
        dest = outdir / f"{qid}_w{slot}.md"
        dest.write_text(md, encoding="utf-8")
        rows.append((title, entry.get("category", ""), slot, entry.get("status", ""),
                     entry.get("attempts", ""), dest.name))

    summary = ["# 解题轨迹汇总", f"\n- 数据目录: `{rundir}`\n- 导出时间: {time.strftime('%F %T')}\n",
               "\n\n| 题目 | 类别 | agent | 状态 | 尝试 | 轨迹文件 |\n|---|---|---|---|---|---|"]
    for title, cat, slot, status, attempts, name in rows:
        summary.append(f"\n| {title} | {cat} | w{slot} | {status} | {attempts} | [{name}]({name}) |")
    subs = logs / "submissions.log"
    if subs.exists():
        summary.append("\n## 提交记录\n\n```\n" + subs.read_text(encoding="utf-8").strip()[-4000:] + "\n```\n")
    (outdir / "SUMMARY.md").write_text("".join(summary), encoding="utf-8")

    print(f"已导出 {len(rows)} 份轨迹 -> {outdir}")
    print(f"汇总: {outdir / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
