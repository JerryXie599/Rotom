#!/usr/bin/env python3
"""监工用的一屏状态快照(只读,不干预):python3 tools/status_now.py [--project wqb2]"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
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
    a = ap.parse_args()
    d = project_dir(a.project)
    logs = d / "logs"

    st = jload(logs / "runner_state.json")
    dl = jload(logs / "run_deadline.json").get("deadline") or 0
    board = jload(d / "board.json")

    pid = None
    pf = logs / "runner.pid"
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
        except ValueError:
            pass
    alive = False
    if pid:
        try:
            import os
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass
    try:
        out = subprocess.run(["pgrep", "-x", "pi"], capture_output=True, text=True).stdout
        pi = len(out.split())
    except Exception:
        pi = -1

    left = dl - time.time() if dl else None
    hb = st.get("heartbeat")
    hb_age = time.time() - hb if hb else None

    runner_txt = f"存活 pid={pid}" if alive else ("★掉了★" if pid else "未启动")
    hb_txt = f"心跳 {hb_age:.0f}s 前" if hb_age is not None else "无心跳"
    if left is None:
        window = "窗口未开始"
    elif left <= 0:
        window = f"窗口已结束({-left/60:.0f} 分钟前)"
    else:
        window = f"窗口剩余 {int(left // 60)}分{int(left % 60)}秒"
    print(f"时间 {time.strftime('%H:%M:%S')} | runner {runner_txt} | {hb_txt}")
    print(f"{window} | 并发上限 {st.get('limit')} | 在场 pi {pi} "
          f"{'| 额度暂停中' if st.get('paused') else ''} | 日志 {st.get('logs_mb')}MB")

    qs = board.get("questions") or {}
    by = {}
    for q in qs.values():
        by.setdefault(q.get("status"), []).append(q)
    solved = by.get("solved", [])
    print(f"题目 {len(qs)} 道 | 已解出 {len(solved)} | 运行中 {len(by.get('running', []))} "
          f"| 待派发 {len(by.get('pending', []))} | 已放弃 {len(by.get('failed', []))}")
    for q in qs.values():
        mark = {"solved": "✔", "running": "▶", "pending": "·", "failed": "✘"}.get(q.get("status"), "?")
        print(f"  {mark} {q.get('title', '?'):<10} [{q.get('category', '?')}] 尝试 {q.get('attempts', 0)}")

    facts = board.get("facts") or []
    if facts:
        print("最近事件:")
        for f in facts[-5:]:
            print("   ", str(f)[:150])
    subs = [f for f in facts if "CORRECT" in str(f) or "WRONG" in str(f) or "未被平台受理" in str(f)]
    wrong = [f for f in subs if "WRONG" in str(f)]
    busy = [f for f in subs if "未被平台受理" in str(f)]
    print(f"提交判定:CORRECT ×{len(subs) - len(wrong) - len(busy)} | WRONG ×{len(wrong)} "
          f"| 未被受理(限流) ×{len(busy)}")

    # 提交与错误
    txt = ""
    rf = logs / "runner.out"
    if rf.exists():
        try:
            size = rf.stat().st_size
            with open(rf, "rb") as fh:
                fh.seek(max(0, size - 120_000))
                txt = fh.read().decode("utf-8", "ignore")
        except OSError:
            pass
    subs = re.findall(r"提交成功|回答正确|已提交", txt)
    print(f"日志中提交成功迹象 ×{len(subs)}")
    for name, rx in (("模型网络错", r"Connection error|ECONNREFUSED|ENOTFOUND|fetch failed|ETIMEDOUT"),
                     ("限流", r"429|too many requests|rate.?limit"),
                     ("额度", r"quota|insufficient|余额|额度"),
                     ("平台异常", r"查题接口异常|提交异常|urlopen error"),
                     ("空跑", r"0 次工具调用")):
        n = len(re.findall(rx, txt, re.I))
        if n:
            print(f"  ⚠ {name} ×{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
