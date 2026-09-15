#!/usr/bin/env python3
"""赛后复盘:从 worker 的 jsonl 里量化"磕磕绊绊"在哪,不靠印象。

  python3 tools/postmortem.py --project wqb2
  python3 tools/postmortem.py --project wqb2 --show 12    # 每类废动作多打几个样本

统计口径:
  - 每个会话(题目×slot×第几次尝试)的工具调用数、回合数、token、上下文压缩次数、报错
  - 工具类型分布;bash 命令里的分类:
      · 导航开销   —— 命令以 cd <绝对路径> 开头(已知最主要的浪费来源)
      · 路径冗长   —— 命令里出现超长绝对路径(>100 字符)
      · 重复命令   —— 同一个会话里原样重复执行的命令
      · 上网尝试   —— curl/wget/pip install/apt install(现场断网会白费)
      · 找工具     —— which/find / 之类"先找有没有这个工具"
  - runner.out 里的重试/超时/额度事件
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def project_dir(name: str) -> Path:
    if name == "默认项目":
        return ROOT
    return ROOT / "projects" / re.sub(r"[^A-Za-z0-9_.\-]+", "-", name.strip()).strip("-")


NET_RE = re.compile(r"\b(curl|wget|pip3?\s+install|apt(-get)?\s+install|git\s+clone|nmap)\b")
HUNT_RE = re.compile(r"^\s*(which|command -v|find\s+/\s|ls\s+/usr/share)", re.I)
CD_RE = re.compile(r"^\s*cd\s+(\S+)")
LONG_PATH_RE = re.compile(r"/[^\s'\"]{100,}")


def analyze(path: Path) -> dict:
    tools = collections.Counter()
    bash_cmds: list[str] = []
    turns = 0
    compactions = 0
    errors = 0
    usage = collections.Counter()
    first_ts = last_ts = None
    session = None
    for line in path.open("r", errors="ignore"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type")
        if t == "session":
            session = e.get("id")
            first_ts = e.get("timestamp")
        elif t == "turn_end":
            turns += 1
            m = e.get("message") or {}
            u = m.get("usage") or {}
            for k in ("input", "output", "cacheRead", "reasoning", "totalTokens"):
                usage[k] += int(u.get(k) or 0)
            last_ts = e.get("timestamp") or last_ts
        elif t == "tool_execution_start":
            name = e.get("toolName") or "?"
            tools[name] += 1
            args = e.get("args") or {}
            if name == "bash" and isinstance(args.get("command"), str):
                bash_cmds.append(args["command"])
            elif isinstance(args.get("command"), str):
                bash_cmds.append(args["command"])
        elif t == "tool_execution_end":
            if e.get("isError"):
                errors += 1
        elif t == "compaction_start":
            compactions += 1
    return {
        "file": path.name, "session": session, "tools": tools, "bash": bash_cmds,
        "turns": turns, "compactions": compactions, "errors": errors, "usage": usage,
        "first": first_ts, "last": last_ts,
    }


def cmd_classify(cmds: list[str]) -> dict:
    nav = [c for c in cmds if CD_RE.match(c)]
    longp = [c for c in cmds if LONG_PATH_RE.search(c)]
    net = [c for c in cmds if NET_RE.search(c)]
    hunt = [c for c in cmds if HUNT_RE.match(c)]
    dup = [c for c, n in collections.Counter(cmds).items() if n > 1]
    dup_n = sum(n - 1 for c, n in collections.Counter(cmds).items() if n > 1)
    return {"导航开销": nav, "路径冗长": longp, "上网尝试": net, "找工具": hunt,
            "重复命令": dup, "重复次数": dup_n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="wqb2")
    ap.add_argument("--show", type=int, default=5, help="每类废动作打印几条样本")
    a = ap.parse_args()
    d = project_dir(a.project)
    logs = d / "logs"
    files = sorted(logs.glob("*.jsonl"))
    if not files:
        print(f"没找到日志: {logs}")
        return 1

    sessions = [analyze(f) for f in files]
    print(f"== 项目「{a.project}」: {len(sessions)} 个 worker 会话 ==\n")

    # ---- 按题目聚合
    print("题目                worker 工具调用 回合 token(k) 压缩 报错 时长")
    byq: dict[str, list[dict]] = collections.defaultdict(list)
    for s in sessions:
        qid = s["file"].split("_w")[0]
        byq[qid].append(s)
    tot_tools = tot_tokens = tot_turns = tot_comp = tot_err = 0
    for qid, ss in byq.items():
        tk = sum(s["tools"].total() for s in ss)
        tok = sum(s["usage"]["totalTokens"] for s in ss)
        tn = sum(s["turns"] for s in ss)
        cp = sum(s["compactions"] for s in ss)
        er = sum(s["errors"] for s in ss)
        tot_tools += tk; tot_tokens += tok; tot_turns += tn; tot_comp += cp; tot_err += er
        durs = []
        for s in ss:
            if s["first"] and s["last"]:
                durs.append(s["last"][11:19])
        span = f"{min(durs)}~{max(durs)}" if durs else "-"
        print(f"{qid:<18} {len(ss):>4}   {tk:>7} {tn:>5} {tok/1000:>8.0f} {cp:>4} {er:>4}  {span}")

    all_tools = collections.Counter()
    for s in sessions:
        all_tools.update(s["tools"])
    all_bash = [c for s in sessions for c in s["bash"]]
    cls = cmd_classify(all_bash)

    print(f"\n合计: 工具调用 {tot_tools} | 回合 {tot_turns} | token {tot_tokens/1000:.0f}k "
          f"| 压缩 {tot_comp} | 报错 {tot_err}")
    print(f"工具分布: {dict(all_tools.most_common())}")
    print(f"bash 命令 {len(all_bash)} 条")
    for k in ("导航开销", "路径冗长", "重复命令", "上网尝试", "找工具"):
        v = cls[k]
        if k == "重复命令":
            print(f"  {k}: {len(v)} 种重复, 多执行 {cls['重复次数']} 次")
        else:
            pct = 100 * len(v) / max(1, len(all_bash))
            print(f"  {k}: {len(v)} 条 ({pct:.1f}%)")
        for c in v[:a.show]:
            print(f"      · {c[:130]}")

    # ---- runner.out 的重试/异常
    rf = logs / "runner.out"
    if rf.exists():
        txt = rf.read_text(errors="ignore")
        print("\n== runner 侧 ==")
        for pat, label in ((r"尝试 \d+|第 (\d+) 次尝试", "重试相关行"),
                           (r"超时", "超时"), (r"429|rate.?limit", "限流"),
                           (r"quota|额度|余额", "额度"),
                           (r"Connection error|ECONNREFUSED|ENOTFOUND", "模型网络错"),
                           (r"0 次工具调用", "空跑"),
                           (r"已解出|提交成功", "解出/提交"),
                           (r"空跑熔断|疑似模型", "熔断告警")):
            n = len(re.findall(pat, txt, re.I))
            print(f"  {label}: {n}")
        print("\n  最后 6 行:")
        for line in txt.strip().splitlines()[-6:]:
            print("   ", line[:150])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
