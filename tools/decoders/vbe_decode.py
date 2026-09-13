#!/usr/bin/env python3
"""VBE / JSE(Microsoft Script Encoder)解码器 —— 离线可用。

杂项/取证题里常见 `#@~^......== ... ......==^#~@` 形式的编码脚本:
VBE = VBScript.Encode,JSE = JScript.Encode。本工具解码它。

算法与解码表来自公有领域的 Didier Stevens `decode-vbe.py`(见同目录 _vbe_ref.py,public domain),
这里只做一个便于 agent 调用的薄封装。

用法:
  python3 tools/decoders/vbe_decode.py <文件|编码串|->   # 解码第一块
  python3 tools/decoders/vbe_decode.py --all <文件>      # 提取并解码全部块
  echo "$(cat x.vbe)" | python3 tools/decoders/vbe_decode.py -
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vbe_ref import Decode  # noqa: E402  公有领域实现

VBE_RE = re.compile(r"#@~\^......==(.+?)......==\^#~@", re.S)


def find_blocks(text: str) -> list[str]:
    return [m.group(1) for m in VBE_RE.finditer(text)]


def main() -> int:
    ap = argparse.ArgumentParser(description="VBE/JSE 解码器(离线)")
    ap.add_argument("source", help="文件路径、- 表示 stdin,或直接给编码串")
    ap.add_argument("--all", action="store_true", help="解码文件中所有块")
    args = ap.parse_args()

    if args.source == "-":
        text = sys.stdin.read()
    else:
        p = Path(args.source)
        text = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else args.source

    blocks = find_blocks(text)
    if not blocks:
        print("未找到 VBE/JSE 块(需要 #@~^......==...==^#~@)", file=sys.stderr)
        return 1
    if args.all:
        for i, b in enumerate(blocks, 1):
            print(f"===== 块 {i} =====")
            print(Decode(b))
    else:
        print(Decode(blocks[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
