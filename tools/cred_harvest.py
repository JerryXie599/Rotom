#!/usr/bin/env python3
"""把"历史交战中遇到的口令"收进跨轮泛化口令库(只收通用弱口令,不形成作弊)。

边界(平台规则 = "Agent 不可内置针对题目的历史答题记忆"):
  ✅ 收:公开可知的默认口令、厂商默认值、真实泄露高频序里出现过的口令、
        以及**人可读、可跨系统复用**的弱口令(如 admin/admin、Admin@123、Weaver@2023)。
  ❌ 不收:flag 值、含题号/任务 ID 的行、uuid/哈希样式的随机值、
        只对某一个容器实例有效的临时口令(那是"题目答案",不是"手法")。
  过滤在入库时执行,并打印拒绝原因 —— 宁可少收,不许把答案混进来。

用法:
  python3 tools/cred_harvest.py --file notes.txt              # 一行一条:"pass" 或 "user:pass"
  python3 tools/cred_harvest.py "admin:Admin@123" "Weaver@2023"
  python3 tools/cred_harvest.py --dry-run --file notes.txt     # 只报告会收什么

写入:knowledge/wordlists/observed-passwords.txt(插入式去重、追加在末尾)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WL = ROOT / "knowledge" / "wordlists"
OUT = WL / "observed-passwords.txt"
HEADER = """# 跨轮泛化口令(observed-passwords)
#
# 口径:只收**人可读、可跨系统复用的弱口令/默认值**(公开默认口令 + 真实泄露高频序 + 交战中反复出现的形态)。
#       **不含** flag、题号、任务 ID、uuid/哈希样式的随机值 —— 那些属于"题目答案",不属于"手法"。
# 用法:dict_prep.py 会把它作为"③ 跨轮已观测"段插在「本目标已观测」之后、「weak-leet / weak-affix」之前。
# 维护:python3 tools/cred_harvest.py --file <笔记> 或直接追加一行一条;入库前会过一遍过滤器。
"""

FLAG_RX = re.compile(r"(?i)^\s*flag\{.*\}\s*$")
UUID_RX = re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX_RX = re.compile(r"(?i)^[0-9a-f]{24,}$")
QCODE_RX = re.compile(r"(?i)(xben-\d+|task_[a-z0-9]{6,}|^[abcef]\d?-\d{1,2}$|^r\d{1,3}$)")
BASE64ISH_RX = re.compile(r"^[A-Za-z0-9+/=_-]{28,}$")


def existing() -> set[str]:
    """全库已有哪些口令(跨文件去重)。"""
    known: set[str] = set()
    for p in list(WL.glob("*.txt")) + list((WL / "vendor-passwords").glob("*.txt")):
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            known.add(s.split(":", 1)[1].strip() if ":" in s else s)
    return known


def verdict(pw: str) -> tuple[bool, str]:
    """能不能收:返回 (ok, 原因)。"""
    if not pw:
        return False, "空"
    if len(pw) < 3:
        return False, "太短(<3)"
    if len(pw) > 32:
        return False, "太长(>32,多半是临时 token)"
    if FLAG_RX.match(pw):
        return False, "flag 样式"
    if UUID_RX.match(pw):
        return False, "uuid 样式(实例随机值)"
    if HEX_RX.match(pw):
        return False, "长十六进制(哈希/token)"
    if QCODE_RX.search(pw):
        return False, "含题号/任务 ID"
    if BASE64ISH_RX.match(pw) and not re.search(r"[A-Za-z]{3,}[0-9]{2,}", pw):
        return False, "疑似 base64 随机串"
    return True, ""


def candidates(args) -> list[str]:
    out: list[str] = list(args.pairs or [])
    for f in args.file or []:
        p = Path(f)
        if not p.exists():
            print(f"[warn] 读不到 {p}", file=sys.stderr)
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="跨轮泛化口令库维护(带反作弊过滤)")
    ap.add_argument("pairs", nargs="*", help="直接给口令(\"pass\" 或 \"user:pass\")")
    ap.add_argument("--file", action="append", help="从文件读(一行一条;可重复)")
    ap.add_argument("--dry-run", action="store_true", help="只报告,不落盘")
    args = ap.parse_args()

    known = existing()
    claimed = []
    rejected: dict[str, list[str]] = {}
    seen: set[str] = set()
    for raw in candidates(args):
        pw = raw.split(":", 1)[1].strip() if ":" in raw else raw.strip()
        ok, why = verdict(pw)
        if not ok:
            rejected.setdefault(why, []).append(pw[:40])
            continue
        if pw in known or pw in seen:
            rejected.setdefault("已存在", []).append(pw)
            continue
        seen.add(pw)
        claimed.append(pw)

    print(f"候选 {len(candidates(args))} 条 → 可收 {len(claimed)} 条;拒收明细:")
    for why, items in sorted(rejected.items(), key=lambda kv: -len(kv[1])):
        print(f"  - {why}: {len(items)} 条,例: {', '.join(items[:4])}")
    if claimed:
        print("将写入(前 20):")
        for pw in claimed[:20]:
            print("   ", pw)
    if args.dry_run or not claimed:
        return 0

    WL.mkdir(parents=True, exist_ok=True)
    if not OUT.exists():
        OUT.write_text(HEADER, encoding="utf-8")
    with OUT.open("a", encoding="utf-8") as f:
        for pw in claimed:
            f.write(pw + "\n")
    print(f"已写入 {OUT}(+{len(claimed)} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())