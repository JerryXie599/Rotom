#!/usr/bin/env python3
"""凭据表组表器:把"厂商定向 + 已观测复用值 + 通用高频"拼成**一张有序口令面**(离线,无依赖)

为什么需要:认证服务常有硬限速 —— 实测有的目标 `>1 并发即 RESET`、单发认证 2.5~3 秒,
有效速率不到 1 发/秒;一张几千条的字典**永远跑不完**。这种情况下
**顺序就是策略**:把最可能的排最前面,命中就是几发;排错了,再厚的字典也是输。

固定顺序(不接受重排 —— 想改顺序就改表文件本身,不要在投递时临时打乱):

  ① 厂商定向表(人写人审的固定条目,含该厂商最常见的产品名/默认口令)
  ② 本目标已观测凭据(--reuse,内网横向复用率最高的一类候选)
  ③ 跨轮已观测(observed-passwords:历史交战中可复用的通用弱口令/默认值,**不含题号与答案**)
  ④ weak-leet / weak-affix(通用变形族 —— P@ssword、Admin@123 这类只在这两张表里)
  ⑤ 按指纹生成的"产品词×年份"族(gen_product_year.py 现场产,覆盖通用字典永远覆盖不到的形态)
  ⑥ 通用高频序(兜底)

  python3 tools/dict_prep.py --vendor 泛微 --reuse observed_creds.txt --out pw_ordered.txt
  python3 tools/dict_prep.py --vendor 泛微 --vendor 致远 --no-generic --out pw.txt
  python3 tools/dict_prep.py --list                       # 有哪些厂商表可用
  python3 tools/dict_prep.py --vendor 泛微 --print-order   # 只看分段与计数,不出全表

自检:出表时会拒绝任何 `flag{...}` 样式的行 —— 字典是通用装备,不是答案清单。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WL = ROOT / "knowledge" / "wordlists"
VENDOR_DIR = WL / "vendor-passwords"
BASE = VENDOR_DIR / "系统与通用弱口令_base.txt"
GEN = ROOT / "tools" / "gen_product_year.py"

FLAG_RX = re.compile(r"^\s*(?:flag|FLAG)\{[^}]*\}\s*$")


class Segment:
    """一段有序口令面;名字用于 --print-order 与落盘时的分段注释。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.items: list[str] = []

    def extend(self, items) -> None:
        self.items.extend(items)


def read_list(path: Path) -> list[str]:
    """读口令表:`#` 注释与空行丢弃;`user:pass` 取 pass 那一半。保持文件顺序。"""
    out: list[str] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" in s:  # 形如 admin:P@ssword
            s = s.split(":", 1)[1].strip()
        if s:
            out.append(s)
    return out


ALIAS_RX = re.compile(r"^#\s*aliases\s*[:：]\s*(.+)$", re.IGNORECASE)


def table_aliases(path: Path) -> list[str]:
    """读表文件头部的 `# aliases: a,b,c` 行(表自报别名,比按文件名猜可靠)。"""
    out: list[str] = []
    try:
        head = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:20]
    except OSError:
        return out
    for line in head:
        m = ALIAS_RX.match(line.strip())
        if m:
            out += [a.strip() for a in re.split(r"[,，/\s]+", m.group(1)) if a.strip()]
    return out


def vendor_tables() -> dict[str, Path]:
    """厂商名/别名 → 定向表文件(通用 base 表不算厂商表)。"""
    table: dict[str, Path] = {}
    if not VENDOR_DIR.exists():
        return table
    for p in sorted(VENDOR_DIR.glob("*.txt")):
        if p == BASE:
            continue
        stem = p.stem
        keys = {stem}
        if "_" in stem:               # `泛微OA_weaver` → 泛微OA / weaver
            a, _, b = stem.partition("_")
            keys |= {a, b}
            if a.endswith("OA"):
                keys.add(a[:-2])
        keys |= set(table_aliases(p))
        for k in keys:
            table.setdefault(k.lower(), p)
    return table


def resolve_vendor(name: str) -> Path | None:
    t = vendor_tables()
    if name.lower() in t:
        return t[name.lower()]
    near = [k for k in t if name.lower() in k or k in name.lower()]
    if near:
        return t[sorted(near)[0]]
    return None


def vocab_words(vendors: list[str]) -> tuple[list[str], list[str]]:
    """从 vendor-vocab.txt 取厂商对应的产品词根(喂生成器)。

    返回 (词根, 没查到词根的厂商名)。厂商名按"全等 → 互相包含"匹配,
    所以 `--vendor 海康`能命中词根表里的「海康威视」,而「安防」这种分类名查不到 —— 查不到就明说,
    不要拿分类名当产品词去生成(会产出一堆废候选,白白占用限速通道的每一发)。
    """
    vocab = WL / "vendor-vocab.txt"
    if not vocab.exists():
        return [], list(vendors)
    entries: list[tuple[str, list[str]]] = []
    for line in vocab.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        vendor, _, ws = line.partition("|")
        entries.append((vendor.strip(), [w.strip() for w in ws.split(",") if w.strip()]))

    words: list[str] = []
    missing: list[str] = []
    for v in vendors:
        hit = [ws for name, ws in entries if name == v] or \
              [ws for name, ws in entries if v in name or name in v]
        if hit:
            words += hit[0]
        else:
            missing.append(v)
    return list(dict.fromkeys(words)), missing


def gen_family(words: list[str], years: str | None) -> list[str]:
    """调生成器拿"产品词×年份"族(不重排;生成器自己保证顺序)。"""
    if not words or not GEN.exists():
        return []
    cmd = [sys.executable, str(GEN), *words, "--compact"]
    if years:
        cmd += ["--years", years]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.SubprocessError:
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def dedupe(segments: list[Segment]) -> list[Segment]:
    seen: set[str] = set()
    for seg in segments:
        keep: list[str] = []
        for pw in seg.items:
            if pw in seen:
                continue
            seen.add(pw)
            keep.append(pw)
        seg.items = keep
    return segments


def main() -> int:
    ap = argparse.ArgumentParser(description="凭据表组表器(顺序固定,不支持重排)")
    ap.add_argument("words", nargs="*", help="直接给产品词根(如 weaver huawei);与 --vendor 可并用")
    ap.add_argument("--vendor", action="append", default=[], help="厂商名(可重复;--list 看有哪些)")
    ap.add_argument("--reuse", help="本目标已观测凭据的文件(一行一条)——内网复用率最高的一类")
    ap.add_argument("--years", help="产品词×年份族的年份优先级(逗号或区间;缺省=生成器默认)")
    ap.add_argument("--no-generic", action="store_true", help="不要通用高频段(只投定向面)")
    ap.add_argument("--list", action="store_true", help="列出可用的厂商表后退出")
    ap.add_argument("--print-order", action="store_true", help="只打印分段与计数,不出全表")
    ap.add_argument("--out", help="输出文件(缺省 stdout)")
    args = ap.parse_args()

    if args.list:
        t = vendor_tables()
        print("可用的厂商定向表(--vendor 后面写厂商名即可):")
        by_file: dict[str, list[str]] = {}
        for k, p in t.items():
            by_file.setdefault(p.name, []).append(k)
        for name in sorted(by_file):
            aliases = sorted(by_file[name], key=len, reverse=True)[:2]
            n = len(read_list(VENDOR_DIR / name))
            print(f"  {'/'.join(aliases):<26} {name}  ({n} 条)")
        print(f"\n通用表: {BASE.name}  ({len(read_list(BASE))} 条)")
        print("词根表: vendor-vocab.txt(厂商 → 产品词,喂 gen_product_year.py)")
        return 0

    # ① 厂商定向表(把解析到的文件记下来,后面按顺序填内容)
    vendor_files: list[Path] = []
    for v in args.vendor:
        p = resolve_vendor(v)
        if not p:
            print(f"[warn] 没找到「{v}」的定向表(用 --list 看可用的);跳过", file=sys.stderr)
            continue
        if p in vendor_files:
            continue
        vendor_files.append(p)

    segs: list[Segment] = [Segment(f"厂商定向 · {p.stem}") for p in vendor_files]

    # ② 已观测复用值(本目标)
    if args.reuse:
        segs.append(Segment(f"已观测凭据(复用值) · {Path(args.reuse).name}"))
    # ③ 跨轮已观测(通用弱口令;无题号/答案)
    observed = WL / "observed-passwords.txt"
    if observed.exists() and read_list(observed):
        segs.append(Segment("跨轮已观测(observed-passwords)"))
    # ④ weak-leet / weak-affix(通用变形族)
    if not args.no_generic:
        for f in (WL / "weak-leet.txt", WL / "weak-affix.txt"):
            if f.exists() and read_list(f):
                segs.append(Segment(f"变形族 · {f.name}"))

    # ⑤ 产品词×年份族
    words, no_vocab = vocab_words(args.vendor)
    words += [w.lower() for w in args.words if w.strip()]
    if not words:
        # 只有"本身就是产品词"(纯 ASCII,如 weaver/huawei)才允许直接拿去生成
        words = [v.lower() for v in args.vendor if v.isascii()]
    if no_vocab:
        missing = [v for v in no_vocab if not v.isascii()]
        if missing:
            print(f"[note] 词根表里没有 {'、'.join(missing)} 的产品词(那是分类名而不是厂商名),"
                  f"跳过年份族生成;要生成请直接给产品词: dict_prep.py --vendor 海康 hikvision",
                  file=sys.stderr)
    words = list(dict.fromkeys(words))
    if words:
        segs.append(Segment(f"产品词×年份族 · {' '.join(words)}"))

    # ⑥ 通用高频
    if not args.no_generic:
        segs.append(Segment("通用高频序 · 系统与通用弱口令_base"))

    for seg, p in zip(segs, vendor_files):
        seg.extend(read_list(p))
    if args.reuse:
        for seg in segs:
            if seg.name.startswith("已观测"):
                seg.extend(read_list(Path(args.reuse)))
    for seg in segs:
        if seg.name.startswith("跨轮已观测"):
            seg.extend(read_list(observed))
        elif seg.name.startswith("变形族"):
            seg.extend(read_list(WL / seg.name.split(" · ")[-1]))
    for seg in segs:
        if seg.name.startswith("产品词×年份族"):
            seg.extend(gen_family(words, args.years))
    if not args.no_generic:
        for seg in segs:
            if seg.name.startswith("通用高频"):
                seg.extend(read_list(BASE))

    segs = dedupe(segs)
    total = sum(len(s.items) for s in segs)

    # 口径自检:方言/答案不进表
    for seg in segs:
        for pw in seg.items:
            if FLAG_RX.match(pw):
                print(f"[fatal] 口令表里出现 flag 样式的行:{pw!r} —— 字典只收通用弱口令,答案不进表",
                      file=sys.stderr)
                return 1

    if args.print_order:
        print(f"口令面共 {total} 条,顺序如下(不要重排):")
        for i, seg in enumerate(segs, 1):
            print(f"  {i}. {seg.name:<44} {len(seg.items):>5} 条")
        return 0

    lines: list[str] = [
        "# pw_ordered.txt —— 由 tools/dict_prep.py 生成(顺序即策略,投递时不要重排)",
        f"# 分段: " + " → ".join(f"{s.name}({len(s.items)})" for s in segs),
        "# 口径:通用弱口令与指纹生成的候选,不含题号绑定信息",
    ]
    for seg in segs:
        lines.append(f"\n# === {seg.name} ===")
        lines += seg.items
    body = "\n".join(lines) + "\n"

    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"口令面 {total} 条 → {args.out}")
        for i, seg in enumerate(segs, 1):
            print(f"  {i}. {seg.name:<44} {len(seg.items):>5} 条")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())