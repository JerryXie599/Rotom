#!/usr/bin/env python3
"""生成 weak-leet / weak-affix 口令族(头部选手点名的两类"通用字典覆盖不到"的形态)。

为什么:真实弱口令大量落在"词 + 变形"上 —— `P@ssword`/`Admin@123`/`1qaz@WSX` 这类值
只存在于 weak-leet / weak-affix 表里,通用高频序(rockyou 序)与厂商表都覆盖不到。
这两张表是**通用装备**(与具体题目无关),生成成本几乎为零,却能显著提高凭据墙的命中率。

  python3 tools/gen_weak_variants.py                # 从 base 表前 300 条 + vendor-vocab 生成
  python3 tools/gen_weak_variants.py --top 500 --max 30000
  python3 tools/gen_weak_variants.py --dry-run

产出:knowledge/wordlists/weak-leet.txt / weak-affix.txt(带口径头、去重、顺序稳定)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WL = ROOT / "knowledge" / "wordlists"
BASE = WL / "vendor-passwords" / "系统与通用弱口令_base.txt"
VOCAB = WL / "vendor-vocab.txt"
OUT_LEET = WL / "weak-leet.txt"
OUT_AFFIX = WL / "weak-affix.txt"

LEET = {"a": ["@", "4"], "o": ["0"], "s": ["$", "5"], "e": ["3"], "i": ["1", "!"],
        "l": ["1"], "t": ["7"], "g": ["9"], "b": ["8"]}

AFFIXES = ["123", "1234", "12345", "123456", "2023", "2024", "2025", "2026",
           "@123", "@2024", "@2026", "!", "01", "2023!", "2024!", "888", "666"]

FLAG_RX = re.compile(r"(?i)flag\{")


def base_words(top: int) -> list[str]:
    out: list[str] = []
    if BASE.exists():
        for line in BASE.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s.split(":", 1)[1].strip() if ":" in s else s)
            if len(out) >= top:
                break
    if VOCAB.exists():
        for line in VOCAB.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "|" not in s:
                continue
            for w in s.split("|", 1)[1].split(","):
                w = w.strip()
                if w and w.isascii() and w.isalpha() and len(w) >= 3:
                    out.append(w)
    return list(dict.fromkeys(out))


def leet_variants(word: str, cap: int = 3) -> list[str]:
    """单词的单点 / 双点 leet 变形(有界,避免组合爆炸)。"""
    outs: list[str] = []
    lower = word.lower()
    for i, ch in enumerate(lower):
        for rep in LEET.get(ch, []):
            outs.append(lower[:i] + rep + lower[i + 1:])
    # 首字母大写 + 单点变形
    cap_w = word[:1].upper() + word[1:].lower()
    outs.append(cap_w)
    for i, ch in enumerate(lower[:6]):
        for rep in LEET.get(ch, [])[:1]:
            outs.append(cap_w[:i] + rep + cap_w[i + 1:])
    return list(dict.fromkeys(outs))[:cap]


def affix_variants(word: str) -> list[str]:
    outs: list[str] = []
    cap_w = word[:1].upper() + word[1:].lower()
    for a in AFFIXES:
        outs.append(word + a)
        outs.append(cap_w + a)
    return outs


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 weak-leet / weak-affix(通用装备,与题目无关)")
    ap.add_argument("--top", type=int, default=300, help="基础词取前多少个(base 表 + 词根表)")
    ap.add_argument("--max", type=int, default=30000, help="单表条目上限")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    words = base_words(max(1, args.top))
    if not words:
        print("没有基础词可生成(缺 base 表/vendor-vocab)", file=sys.stderr)
        return 2
    leet: list[str] = []
    affix: list[str] = []
    for w in words:
        if FLAG_RX.search(w):
            continue
        leet.extend(leet_variants(w))
        affix.extend(affix_variants(w))
    leet = list(dict.fromkeys(leet))[: args.max]
    affix = list(dict.fromkeys(affix))[: args.max]
    print(f"基础词 {len(words)} 个 → weak-leet {len(leet)} 条 / weak-affix {len(affix)} 条")
    for name, items in (("weak-leet", leet[:8]), ("weak-affix", affix[:8])):
        print(f"  {name} 样例: {', '.join(items)}")
    if args.dry_run:
        return 0
    WL.mkdir(parents=True, exist_ok=True)
    OUT_LEET.write_text(
        "# weak-leet:通用弱口令的 leet 变形族(头部选手点名:P@ssword/1qaz@WSX 这类只存在于本类表)\n"
        "# 口径:由 base 表与厂商词根机械变形而来,与具体题目无关;不含题号/答案。\n"
        + "\n".join(leet) + "\n", encoding="utf-8")
    OUT_AFFIX.write_text(
        "# weak-affix:通用弱口令 + 常见后缀族(Admin@123/Password2024 这类形态)\n"
        "# 口径:机械拼接,与具体题目无关;不含题号/答案。\n"
        + "\n".join(affix) + "\n", encoding="utf-8")
    print(f"已写入 {OUT_LEET.name}({len(leet)}) / {OUT_AFFIX.name}({len(affix)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())