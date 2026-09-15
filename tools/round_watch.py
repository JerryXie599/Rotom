#!/usr/bin/env python3
"""事件哨兵:平时静静等着,一旦"有新题出现 / runner 掉了 / 窗口到点"就退出并唤醒监工。

  python3 tools/round_watch.py --project wqb2 [--interval 20]

退出码:0 有新情况(需人看一眼) / 3 窗口结束
它自己不做补救(补救由 tools/watchdog.py 负责),只负责"该醒的时候醒"。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def project_dir(name: str) -> Path:
    if name == "默认项目":
        return ROOT
    return ROOT / "projects" / re.sub(r"[^A-Za-z0-9_.\-]+", "-", name.strip()).strip("-")


def jload(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="wqb2")
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--max-minutes", type=int, default=45, help="最长守候时间(兜底,防止永久挂着)")
    a = ap.parse_args()
    d = project_dir(a.project)
    logs = d / "logs"
    started = time.time()
    print(f"[哨兵] 守候项目「{a.project}」,每 {a.interval}s 检查一次", flush=True)

    while True:
        board = jload(d / "board.json")
        qs = board.get("questions") or {}
        unsolved = [q for q in qs.values() if q.get("status") != "solved"]
        deadline = jload(logs / "run_deadline.json").get("deadline") or 0

        try:
            pid = int((logs / "runner.pid").read_text().strip())
            os.kill(pid, 0)
            alive = True
        except Exception:
            alive = False

        if unsolved:
            titles = ", ".join(str(q.get("title")) for q in unsolved[:6])
            print(f"[哨兵] 有 {len(unsolved)} 道题未解出 → 唤醒监工: {titles}", flush=True)
            return 0
        if not alive:
            print("[哨兵] runner 不在运行 → 唤醒监工", flush=True)
            return 0
        if deadline and time.time() > deadline:
            print(f"[哨兵] 挑战窗口已结束({time.strftime('%H:%M:%S')}) → 唤醒监工", flush=True)
            return 3
        if (time.time() - started) > a.max_minutes * 60:
            print(f"[哨兵] 守候满 {a.max_minutes} 分钟,结束", flush=True)
            return 0

        solved = sum(1 for q in qs.values() if q.get("status") == "solved")
        left = int((deadline - time.time()) // 60) if deadline else -1
        print(f"[哨兵 {time.strftime('%H:%M:%S')}] {solved}/{len(qs)} 已解出 | runner 存活 | 剩余 {left} 分钟",
              flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
