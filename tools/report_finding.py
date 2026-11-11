#!/usr/bin/env python3
"""记录 finding —— 正式渗透模式下 agent 通过 bash 调用本脚本(替代 submit_flag)。

用法:
  # 记一条漏洞
  python3 tools/report_finding.py <target_id> --title "SQL 注入 @ /api/login" --severity high \
      --detail "入口: POST /api/login 参数 u
复现: curl -s -d \"u=admin' OR 1=1-- -&p=x\" http://T/api/login
证据: 返回 200 且带出 admin 的用户信息(见 out.txt 第 3 行)
影响: 绕过认证,可读全表
建议: 参数化查询 + 最小权限"

  # 该目标测完(交付)
  python3 tools/report_finding.py <target_id> --done --summary "测了 web/API 面;2 个高危已记录;数据库端口未开放"

退出码与输出(提示词里也写了,agent 据此决定下一步):
  0  RECORDED   —— 这条 finding 记上了(会写进 FINDINGS.md / findings.jsonl / 黑板)
  0  DELIVERED  —— 该目标已交付(黑板标记 solved,runner 不再派人)
  1  参数错误 / 目标不在黑板
结果会追加到 logs/findings.jsonl,并同步进 work/<目标>/FINDINGS.md 与黑板 facts。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import board as board_mod  # noqa: E402

RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)
LOG_DIR = RUN_DIR / "logs"
SEVERITIES = ("critical", "high", "medium", "low", "info")
SEV_ORDER = {s: i for i, s in enumerate(SEVERITIES)}


def target_dir(target_id: str) -> Path:
    """目标工作目录:黑板里有 workdir 就用它,否则按 id 猜一个。

    workdir 是绝对路径时直接采用(与 solver.project_dir 同一规则;涉诈场景 worker
    落在站点档案的 工作区/ 里,FINDINGS.md 也要跟过去),相对路径按 RUN_DIR/work 解析。
    """
    with board_mod.locked() as b:
        e = b["questions"].get(target_id) or {}
    name = (e.get("workdir") or "").strip()
    if name:
        p = Path(name).expanduser()
        if p.is_absolute():
            return p
        return RUN_DIR / "work" / p
    import re
    name = "pentest-" + (re.sub(r"[^A-Za-z0-9_.-]+", "-", target_id).strip("-") or "target")
    return RUN_DIR / "work" / Path(name).name


def append_findings_md(path: Path, target_id: str, title: str, severity: str,
                       detail: str, ts: str) -> None:
    """人类可读的汇总页(agent 和客户都会看),用 >> 追加、不破坏已有内容。"""
    head = "" if path.exists() else (
        f"# FINDINGS · {target_id}\n\n"
        "> 本文件由 `tools/report_finding.py` 自动维护(agent 每次记 finding 追加一段)。\n")
    block = (f"\n## [{severity.upper()}] {title}\n\n"
             f"- 记录时间: {ts}\n\n"
             f"{detail.strip()}\n")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(head + block)
    except OSError as e:
        print(f"警告:FINDINGS.md 写入失败({e}),但记账已继续", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="记录 finding / 交付目标(正式渗透模式)")
    ap.add_argument("target_id", help="目标代号(targets.json 里的 id)")
    ap.add_argument("--title", help="一句话标题(漏洞是什么、在哪)")
    ap.add_argument("--severity", default="medium", choices=SEVERITIES, help="危害等级(默认 medium)")
    ap.add_argument("--detail", default="", help="复现步骤 / 证据 / 影响 / 修复建议(多行用 heredoc)")
    ap.add_argument("--where", default="", help="(可选)入口 URL / 参数 / 端口,便于报告归类")
    ap.add_argument("--done", action="store_true", help="标记该目标测试完成并交付")
    ap.add_argument("--summary", default="", help="--done 时的整体结论")
    args = ap.parse_args()

    tid = args.target_id.strip()
    if not tid:
        print("用法: report_finding.py <target_id> --title ... --severity ... --detail ...", file=sys.stderr)
        return 1

    with board_mod.locked() as b:
        entry = b["questions"].get(tid)
        if entry is None:
            print(f"TARGET_NOT_FOUND target={tid}(黑板里没有这个目标;id 要和 targets.json 一致)", file=sys.stderr)
            return 1
        known = bool(entry)

    ts = time.strftime("%F %T")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------- 交付(--done)----------------
    if args.done:
        # 档案齐备闸门(涉诈包):工作区上级是站点档案骨架(有 证据/)时,交付前必须先把
        # 00_档案.md 与 报告/侦察报告 落盘 —— 第二轮冒烟实测 GLM 会跳过档案直接 --done。
        # 非档案布局(普通 pentest 工作区)不受影响,老行为不变。
        tdir = target_dir(tid)
        arch = tdir.parent
        if (arch / "证据").is_dir():
            missing = []
            if not (arch / "00_档案.md").exists():
                missing.append("00_档案.md(一页纸首页,模板见 00_总控/模板/站点档案范本.md)")
            if not any((arch / "报告").glob("*.md")):
                missing.append("报告/侦察报告_<目标>_<日期>.md(本目标详报)")
            if missing:
                print("DELIVERED_DEFERRED:站点档案还没齐,先补齐再 --done:")
                for m in missing:
                    print(f"  - {m}")
                print(f"(档案目录:{arch};证据已在 证据/ 的直接整理引用即可)")
                return 1
        summary = args.summary.strip() or "(未写总结)"
        with board_mod.locked() as b:
            e = b["questions"].get(tid)
            e["delivered"] = True
            e["delivered_at"] = time.time()
            e["delivered_summary"] = summary
            if e["status"] != board_mod.STATUS_SOLVED:
                e["status"] = board_mod.STATUS_SOLVED
                e["is_solved"] = True
                e["finished_at"] = time.time()
            board_mod.add_fact(b, "finding", f"{tid} 已交付(--done):{summary[:80]}")
        with open(LOG_DIR / "findings.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "target_id": tid, "event": "delivered",
                                "summary": summary}, ensure_ascii=False) + "\n")
        md = target_dir(tid) / "FINDINGS.md"
        try:
            with open(md, "a", encoding="utf-8") as f:
                f.write(f"\n---\n\n**交付结论({ts})**:{summary}\n")
        except OSError:
            pass
        print(f"DELIVERED target={tid} —— 本目标已交付,harness 不会再派人。"
              f"如果还有没记完的发现,请立即继续记录。")
        return 0

    # ---------------- 记一条 finding ----------------
    if not args.title:
        print("用法: report_finding.py <target_id> --title ... --severity ... --detail ...", file=sys.stderr)
        return 1
    detail = args.detail.strip()
    if not detail:
        print("提示:--detail 为空 —— finding 必须带可复现证据(命令/payload + 关键回显),"
              "否则报告里会被当成没有证据。", file=sys.stderr)
    rec = {"ts": ts, "target_id": tid, "event": "finding",
           "title": args.title.strip(), "severity": args.severity,
           "where": args.where.strip(), "detail": detail}
    with open(LOG_DIR / "findings.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    append_findings_md(target_dir(tid) / "FINDINGS.md", tid, rec["title"],
                       args.severity, detail, ts)
    with board_mod.locked() as b:
        e = b["questions"].get(tid)
        e["findings_count"] = int(e.get("findings_count") or 0) + 1
        e.setdefault("findings", []).append(
            {"ts": ts, "title": rec["title"], "severity": args.severity, "where": rec["where"]})
        e["findings"] = e["findings"][-200:]
        board_mod.add_fact(b, "finding",
                           f"{tid} [{args.severity}] {rec['title'][:70]}(累计 {e['findings_count']} 条)")
        n = e["findings_count"]
    print(f"RECORDED target={tid} severity={args.severity} 累计 finding {n} 条 —— "
          f"已写入 {target_dir(tid) / 'FINDINGS.md'}。继续测其它攻击面;全部测完记得 --done 收尾。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())