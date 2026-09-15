#!/usr/bin/env python3
"""CTF 知识库检索工具(纯标准库,断网可用) —— 给 agent 在做题时随查随用。

知识库 = 本项目 `knowledge/` 目录:
  - `knowledge/{web,pwn,reverse,crypto,forensics,misc}.md`  自写 playbook(按方向的打法与提速要点)
  - `knowledge/vendor/PayloadsAllTheThings/`                Web 漏洞 payload 大全
  - `knowledge/vendor/ctf-wiki/`                            中文 CTF 系统知识库
  - `knowledge/vendor/RsaCtfTool/`                          RSA 自动攻击工具

用法(agent 在解题过程中直接调):
  python3 tools/kb.py list                        # 看知识库有哪些内容
  python3 tools/kb.py search "sql 注入 绕过"       # 关键词检索(支持中文/英文,空格式多词=都要命中)
  python3 tools/kb.py search "ret2libc" --limit 8
  python3 tools/kb.py show pwn                    # 看某方向 playbook 全文
  python3 tools/kb.py show vendor/ctf-wiki/docs/逆向工程/加壳技术.md   # 看某篇原文
  python3 tools/kb.py grep "0x67452301"           # 原样正则/字符串全局搜

首次检索会建索引并缓存到 knowledge/.kb_index.json(源文件变化自动重建)。
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
KB = ROOT / "knowledge"
INDEX = KB / ".kb_index.json"
PLAYBOOKS = ["web", "pwn", "reverse", "crypto", "forensics", "misc"]
TEXT_EXT = {".md", ".txt", ".rst", ".py"}


def _extra_roots() -> list[tuple[str, Path]]:
    """额外知识目录(环节一 AI 对抗资料)。默认取仓库同级的「人工智能对抗资料」,
    可用环境变量 KB_EXTRA(冒号分隔)覆盖;返回 [(rel 前缀, 绝对路径)]。"""
    raw = os.environ.get("KB_EXTRA", "").strip()
    roots: list[tuple[str, Path]] = []
    if raw:
        for item in raw.split(os.pathsep):
            p = Path(item).expanduser()
            if p.is_dir():
                roots.append((p.name + "/", p))
    else:
        p = ROOT.parent / "人工智能对抗资料"
        if p.is_dir():
            roots.append(("ai/", p))
    return roots


EXTRA_ROOTS = _extra_roots()


def rel_of(p: Path) -> str:
    """文件相对于知识库的显示名:knowledge 下不加前缀,额外目录加前缀(如 ai/)。"""
    for prefix, root in EXTRA_ROOTS:
        try:
            return prefix + str(p.relative_to(root))
        except ValueError:
            pass
    try:
        return str(p.relative_to(KB))
    except ValueError:
        return p.name


def _usable(p: Path) -> bool:
    if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
        return False
    if p.name.startswith("."):
        return False
    try:
        if p.stat().st_size > 3_000_000:   # 超大文件跳过,避免拖慢
            return False
    except OSError:
        return False
    return True


def iter_docs() -> list[Path]:
    out = []
    if KB.exists():
        out += [p for p in KB.rglob("*") if _usable(p)]
    for _prefix, root in EXTRA_ROOTS:
        out += [p for p in root.rglob("*") if _usable(p)]
    return out


def split_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切段,返回 [(heading, body)];没有标题则整篇一段。"""
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in text.splitlines():
        if re.match(r"^#{1,4}\s+\S", line):
            if heading or buf:
                sections.append((heading, "\n".join(buf)))
            heading, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    if heading or buf:
        sections.append((heading, "\n".join(buf)))
    return sections or [("", text)]


def build_index(force: bool = False) -> list[dict]:
    docs = iter_docs()
    stamp = {rel_of(p): p.stat().st_mtime for p in docs}
    if not force and INDEX.exists():
        try:
            cached = json.loads(INDEX.read_text(encoding="utf-8"))
            if cached.get("stamp") == stamp:
                return cached["sections"]
        except Exception:
            pass
    sections: list[dict] = []
    for p in docs:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = rel_of(p)
        for heading, body in split_sections(text):
            body = body.strip()
            if len(body) < 20 and not heading:
                continue
            sections.append({"file": rel, "heading": heading, "text": body[:8000]})
    try:
        INDEX.write_text(json.dumps({"stamp": stamp, "sections": sections}, ensure_ascii=False),
                         encoding="utf-8")
    except OSError:
        pass
    return sections


def tokenize(query: str) -> list[str]:
    return [t for t in re.split(r"[\s,;/|]+", query.strip().lower()) if t]


def snippet(text: str, tokens: list[str], width: int = 150) -> str:
    low = text.lower()
    pos = -1
    for t in tokens:
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return " ".join(text.split())[:width * 2]
    start = max(0, pos - width // 2)
    frag = " ".join(text[start:start + width * 2].split())
    return ("…" if start > 0 else "") + frag


def cmd_search(query: str, limit: int, playbooks_only: bool) -> int:
    sections = build_index()
    tokens = tokenize(query)
    if not tokens:
        print("用法: python3 tools/kb.py search \"关键词\"")
        return 2
    if playbooks_only:
        sections = [s for s in sections if "/" not in s["file"]]

    def score(sec: dict, require_all: bool) -> int | None:
        head, body = sec["heading"].lower(), sec["text"].lower()
        total, hit = 0, 0
        for t in tokens:
            h, b = head.count(t), body.count(t)
            if h == 0 and b == 0:
                if require_all:
                    return None
                continue
            hit += 1
            total += h * 6 + min(b, 12)
        if hit == 0:
            return None
        return total + hit * 3

    results = []
    for sec in sections:
        sc = score(sec, require_all=True)
        if sc is not None:
            results.append((sc, sec))
    if not results:   # 全部命中太少,退化为"命中任意关键词"
        for sec in sections:
            sc = score(sec, require_all=False)
            if sc is not None:
                results.append((sc, sec))
    results.sort(key=lambda x: -x[0])
    if not results:
        print(f"知识库中未找到与「{query}」相关的内容。可试:python3 tools/kb.py list 或换个关键词。")
        return 1
    print(f"知识库检索「{query}」命中 {len(results)} 段,显示前 {min(limit, len(results))} 段:\n")
    for sc, sec in results[:limit]:
        head = f" » {sec['heading']}" if sec["heading"] else ""
        print(f"[{sc:>4}] {sec['file']}{head}")
        print(f"       {snippet(sec['text'], tokens)}")
        print()
    print("查看全文: python3 tools/kb.py show <上面的路径,可省 .md>")
    return 0


def cmd_show(name: str) -> int:
    if not KB.exists():
        print("knowledge/ 不存在")
        return 2
    clean = name[:-3] if name.lower().endswith(".md") else name
    cand = [KB / name, KB / f"{name}.md", KB / name.rstrip("/")]
    for prefix, root in EXTRA_ROOTS:          # 额外目录(ai/…)优先按前缀解析
        if clean.startswith(prefix):
            cand.insert(0, root / clean[len(prefix):])
        cand += [root / clean, root / f"{clean}.md"]
    for c in cand:
        if c.is_file():
            print(c.read_text(encoding="utf-8", errors="ignore"))
            return 0
    # 模糊匹配:按文件名子串找
    hits = [p for p in iter_docs() if clean.lower() in rel_of(p).lower()]
    if len(hits) == 1:
        print(hits[0].read_text(encoding="utf-8", errors="ignore"))
        return 0
    if hits:
        print(f"「{name}」匹配到 {len(hits)} 个文件,请指定更精确的路径:")
        for p in hits[:15]:
            print("  " + rel_of(p))
        return 1
    print(f"没找到 {name}。可用:python3 tools/kb.py list")
    return 1


def cmd_grep(pattern: str, limit: int) -> int:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        print(f"正则错误: {e}")
        return 2
    n = 0
    for p in iter_docs():
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                print(f"{rel_of(p)}:{i}: {' '.join(line.split())[:200]}")
                n += 1
                if n >= limit:
                    print(f"(已达上限 {limit} 条)")
                    return 0
    if n == 0:
        print(f"未匹配: {pattern}")
        return 1
    return 0


def cmd_list() -> int:
    if not KB.exists():
        print("knowledge/ 不存在")
        return 2
    print("== 方向 playbook(自写,含打法与提速要点)==")
    for name in PLAYBOOKS:
        p = KB / f"{name}.md"
        if p.exists():
            first = next((l for l in p.read_text(encoding="utf-8").splitlines()
                          if l.startswith("#")), "")
            print(f"  {name:<10} {first.lstrip('# ').strip()}")
    if (KB / "README.md").exists():
        print("\n== 索引 ==")
        print("  " + str(KB / "README.md"))
    vend = KB / "vendor"
    if vend.exists():
        print("\n== 离线资料(vendor)==")
        for d in sorted(vend.iterdir()):
            if d.is_dir():
                cnt = sum(1 for _ in d.rglob("*.md"))
                print(f"  vendor/{d.name:<26} {cnt} 个 markdown 文档")
    for prefix, root in EXTRA_ROOTS:
        files = [p for p in root.rglob("*") if _usable(p)]
        if files:
            print(f"\n== 额外资料({prefix.rstrip('/')}/) ==")
            for p in sorted(files):
                print(f"  {prefix}{p.relative_to(root)}")
    print("\n用法: python3 tools/kb.py search \"关键词\" | show <方向|路径> | grep <正则>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CTF 知识库检索(离线可用)")
    sub = ap.add_subparsers(dest="cmd")
    p_s = sub.add_parser("search", help="关键词检索")
    p_s.add_argument("query")
    p_s.add_argument("--limit", type=int, default=6)
    p_s.add_argument("--playbooks-only", action="store_true", help="只搜自写 playbook")
    p_h = sub.add_parser("show", help="看全文")
    p_h.add_argument("name")
    p_g = sub.add_parser("grep", help="原样全局搜")
    p_g.add_argument("pattern")
    p_g.add_argument("--limit", type=int, default=40)
    sub.add_parser("list", help="列出知识库内容")
    sub.add_parser("reindex", help="强制重建索引")
    args = ap.parse_args()

    if args.cmd == "search":
        return cmd_search(args.query, args.limit, args.playbooks_only)
    if args.cmd == "show":
        return cmd_show(args.name)
    if args.cmd == "grep":
        return cmd_grep(args.pattern, args.limit)
    if args.cmd == "reindex":
        t0 = time.time()
        n = len(build_index(force=True))
        print(f"索引重建完成:{n} 段,{time.time() - t0:.1f}s")
        return 0
    return cmd_list()


if __name__ == "__main__":
    raise SystemExit(main())
