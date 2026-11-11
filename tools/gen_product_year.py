#!/usr/bin/env python3
"""产品词 × 年份 候选口令生成器(离线,无依赖)

为什么要有它:国产 OA / 网络设备 / 安防设备的口令有很强的"产品词 + 年份"设计习惯
(`weaver@2023`、`Ecology@2024`、`Huawei@2023` 这类形态)。**这类形态用通用泄露字典永远覆盖不到** ——
字典再厚也没用,因为市场上没有这份组合表;必须按指纹在现场生成。

生成成本几乎为零;漏掉的代价却是拿限速通道的每一发去补
(实测某些目标的认证服务硬限速到 ~0.6 口令/秒,且并发即拒)。

  python3 tools/gen_product_year.py weaver ecology
  python3 tools/gen_product_year.py --vendor 泛微
  python3 tools/gen_product_year.py weaver --years 2023,2024,2025   # 逗号=按优先级
  python3 tools/gen_product_year.py weaver --years 2022-2025        # 区间=降序
  python3 tools/gen_product_year.py --vendor 泛微 --compact         # 只出"第一发"的那几条
  python3 tools/gen_product_year.py weaver --out /tmp/derived.txt

输出**已按命中概率排序**(不是字典序):同族内 `产品词@年份` 优先,年份按你给的优先级,
大小写形态 `Product@year` > `product@year` > `PRODUCT@year`,分隔符 `@` > 无 > `#`/`!`/`.`/`_`。
顺序就是策略 —— 投递时**不要自行重排**(见 knowledge/playbooks/credential-wall.md)。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOCAB = ROOT / "knowledge" / "wordlists" / "vendor-vocab.txt"

# 形态族:按"命中概率"从高到低。{w}=词根(原样)、{W}=首字母大写、{U}=全大写、{y}=年份
FORMS: list[tuple[str, str]] = [
    # ① 产品词@年份 —— 国产 OA/设备最主流的一种写法
    ("{W}@{y}", "cap-year-at"),
    ("{w}@{y}", "lower-year-at"),
    ("{U}@{y}", "upper-year-at"),
    # ② 产品词+年份(无分隔符)
    ("{W}{y}", "cap-year"),
    ("{w}{y}", "lower-year"),
    # ③ 产品词@年份带尾巴
    ("{W}@{y}!", "cap-year-at-bang"),
    ("{W}@123", "cap-at-123"),
    ("{w}@123", "lower-at-123"),
    ("{W}@1234", "cap-at-1234"),
    # ④ 其他分隔符
    ("{W}#{y}", "cap-year-hash"),
    ("{W}!{y}", "cap-year-bang"),
    ("{w}#{y}", "lower-year-hash"),
    ("{W}.{y}", "cap-year-dot"),
    ("{W}_{y}", "cap-year-underscore"),
    # ⑤ 产品词+123 家族(通用弱口令里最常见的收尾)
    ("{W}123", "cap-123"),
    ("{w}123", "lower-123"),
    ("{W}123456", "cap-123456"),
    # ⑥ 产品词单用(纯产品名也常被当口令)
    ("{w}", "lower-bare"),
    ("{W}", "cap-bare"),
]

# --compact:只出"第一发就该打"的那几条(拿不准年份时用)
COMPACT_FORMS = ("cap-year-at", "lower-year-at", "upper-year-at", "cap-year", "lower-year")


def load_vocab(path: Path) -> dict[str, list[str]]:
    """读厂商→产品词表。格式: `厂商|词1,词2`,# 开头为注释。"""
    table: dict[str, list[str]] = {}
    if not path.exists():
        return table
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        vendor, _, words = line.partition("|")
        table[vendor.strip()] = [w.strip() for w in words.split(",") if w.strip()]
    return table


def resolve_years(spec: str | None) -> list[int]:
    """年份优先级列表。逗号=按给定顺序;区间 `2022-2025`=降序;缺省=今年起往前推 4 年。"""
    this_year = _dt.date.today().year
    if not spec:
        return list(range(this_year, this_year - 4, -1))
    spec = spec.strip()
    if "," in spec:
        out = [int(x) for x in spec.split(",") if x.strip().isdigit()]
    elif "-" in spec:
        a, _, b = spec.partition("-")
        if a.strip().isdigit() and b.strip().isdigit():
            lo, hi = int(a), int(b)
            out = list(range(hi, lo - 1, -1))
        else:
            out = []
    else:
        out = [int(spec)] if spec.isdigit() else []
    return out or list(range(this_year, this_year - 4, -1))


def render(word: str, year: int, forms: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按形态族渲染一个词根;返回 [(口令, 形态名)],顺序即优先级。"""
    ctx = {"w": word, "W": word[:1].upper() + word[1:], "U": word.upper(), "y": str(year)}
    out: list[tuple[str, str]] = []
    for tmpl, name in forms:
        try:
            out.append((tmpl.format(**ctx), name))
        except (KeyError, IndexError):
            continue
    return out


def generate(words: list[str], years: list[int], forms: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """词根 × 形态 × 年份,按优先级去重输出。

    循环次序是**形态在词根内层、年份在最内层** —— 这样同一个词根、同一种形态的所有年份是**连着的**
    (`Weaver@2024, Weaver@2025, Weaver@2023` 挨在一起)。限速通道下这很关键:第一发就得打到
    "产品词@年份"这一族,而不是被大小写/分隔符变体把年份冲散。
    """
    with_year = [f for f in forms if "{y}" in f[0]]
    no_year = [f for f in forms if "{y}" not in f[0]]
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for w in words:
        for tmpl, name in with_year:
            for y in years:
                pw, _ = render(w, y, [(tmpl, name)])[0]
                if pw not in seen:
                    seen.add(pw)
                    result.append((pw, name))
        for pw, name in render(w, 0, no_year):
            if pw not in seen:
                seen.add(pw)
                result.append((pw, name))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="产品词×年份 候选口令生成器")
    ap.add_argument("words", nargs="*", help="产品词根(如 weaver ecology)")
    ap.add_argument("--vendor", action="append", default=[], help="按厂商名取词根(可重复;见 vendor-vocab.txt)")
    ap.add_argument("--years", help="年份优先级: 2024,2025,2023 或区间 2022-2025(缺省=今年起往前 4 年)")
    ap.add_argument("--vocab", default=str(VOCAB), help=f"词根表路径(缺省 {VOCAB})")
    ap.add_argument("--compact", action="store_true", help="只输出第一发就该打的那几条形态")
    ap.add_argument("--out", help="写入文件(缺省打印到 stdout)")
    ap.add_argument("--show-forms", action="store_true", help="每行附带形态名(仅用于核对,投递前请去掉)")
    args = ap.parse_args()

    words = [w.strip().lower() for w in args.words if w.strip()]
    if args.vendor:
        table = load_vocab(Path(args.vocab))
        for v in args.vendor:
            if v in table:
                words += table[v]
            else:
                near = [k for k in table if v in k or k in v]
                print(f"[warn] 词根表里没有厂商「{v}」" + (f";相近的: {', '.join(near)}" if near else ""),
                      file=sys.stderr)
        words = list(dict.fromkeys(words))

    if not words:
        table = load_vocab(Path(args.vocab))
        print("需要一个或几个产品词根。例:gen_product_year.py weaver ecology", file=sys.stderr)
        if table:
            print("\n词根表里可用的厂商:", file=sys.stderr)
            for v, ws in table.items():
                print(f"  --vendor {v}  →  {', '.join(ws)}", file=sys.stderr)
        return 2

    years = resolve_years(args.years)
    forms = [f for f in FORMS if not args.compact or f[1] in COMPACT_FORMS]
    rows = generate(words, years, forms)

    lines = [f"{pw}\t{name}" if args.show_forms else pw for pw, name in rows]
    body = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"词根 {', '.join(words)} × 年份 {', '.join(map(str, years))} → {len(rows)} 条 → {args.out}")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())