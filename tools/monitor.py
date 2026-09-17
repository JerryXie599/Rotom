#!/usr/bin/env python3
"""监控面板:一屏看清各 worker 在做什么(读 logs/*.jsonl)。

用法:
  python3 tools/monitor.py            # 每 10 秒刷新
  python3 tools/monitor.py --lines 8  # 每个 worker 显示最近 8 个动作
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)


def summarize(path: str, n: int) -> dict:
    tools: list[str] = []
    last_text = ""
    errors: list[str] = []
    tokens = 0
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        if size > 512_000:  # 只解析尾部,日志增长很快
            f.seek(size - 512_000)
            f.readline()
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "message_end":
                msg = ev.get("message") or {}
                if msg.get("role") == "assistant":
                    for c in msg.get("content") or []:
                        if isinstance(c, dict):
                            if c.get("type") == "toolCall":
                                tools.append(c.get("name", "?"))
                            elif c.get("type") == "text" and c.get("text", "").strip():
                                last_text = c["text"].strip().replace("\n", " ")[:160]
                    u = msg.get("usage") or {}
                    tokens = u.get("totalTokens") or tokens
            elif t == "error":
                errors.append(str(ev.get("errorMessage", ""))[:120])
    return {"tools": tools[-n:], "tool_count": len(tools), "last_text": last_text,
            "error": errors[-1] if errors else "", "tokens": tokens,
            "mtime": os.path.getmtime(path), "size": os.path.getsize(path)}


def render(lines: int) -> None:
    logs = sorted(glob.glob(str(RUN_DIR / "logs" / "*.jsonl")))
    if not logs:
        print("还没有 worker 日志")
        return
    # 同一 qid+slot 只保留最新一个日志
    latest: dict[str, str] = {}
    for p in logs:
        key = os.path.basename(p).rsplit("_", 1)[0]
        latest[key] = p
    print(f"=== {time.strftime('%H:%M:%S')}  worker 监控({len(latest)} 个)===")
    for key in sorted(latest):
        s = summarize(latest[key], lines)
        idle = int(time.time() - s["mtime"])
        print(f"\n[{key}] 动作数={s['tool_count']} tokens≈{s['tokens']} "
              f"静默={idle}s 日志={s['size']//1024}KB")
        print(f"  最近动作: {' -> '.join(s['tools']) or '(无)'}")
        if s["last_text"]:
            print(f"  最后发言: {s['last_text']}")
        if s["error"]:
            print(f"  !! 错误: {s['error']}")
    sub = RUN_DIR / "logs" / "submissions.log"
    if sub.exists():
        rows = sub.read_text(encoding="utf-8").strip().splitlines()[-5:]
        print("\n=== 最近提交 ===")
        for r in rows:
            try:
                d = json.loads(r)
                print(f"  {d['ts']} {d['question_id']} ok={d['ok']} {d['flag'][:50]}")
            except json.JSONDecodeError:
                print("  " + r[:120])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=6)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    while True:
        os.system("clear")
        render(args.lines)
        if args.once:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
