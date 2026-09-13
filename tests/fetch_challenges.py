#!/usr/bin/env python3
"""按 tests/challenges.json 下载题目附件到 tests/challenge_files/。

  python3 tests/fetch_challenges.py

每个条目:
  {"id","title","category","score","description","flag","files":["url",...]}
下载后若是 zip/tar 会就地解压到同名目录,并把 "file" 字段更新成 mock server 要分发的文件名。
"""

from __future__ import annotations

import json
import tarfile
import urllib.request
import zipfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FILES = TESTS / "challenge_files"
CHALLENGES = TESTS / "challenges.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连,避免本机代理干扰


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers=UA)
    with _opener.open(req, timeout=180) as r:
        dest.write_bytes(r.read())


def main() -> None:
    data = json.loads(CHALLENGES.read_text(encoding="utf-8"))
    FILES.mkdir(parents=True, exist_ok=True)
    for c in data:
        urls = c.pop("files", None) or ([c["file"]] if c.get("file") else [])
        if not urls:
            print(f"[skip] {c['title']}: 无附件")
            continue
        if isinstance(urls, str):
            urls = [urls]
        saved: list[Path] = []
        for url in urls:
            name = url.split("/")[-1].split("?")[0] or f"{c['id']}.bin"
            dest = FILES / f"{c['id']}_{name}"
            if not dest.exists():
                try:
                    download(url, dest)
                except Exception as e:
                    print(f"[FAIL] {c['title']} <- {url}: {e}")
                    continue
            saved.append(dest)
            # 压缩包就地解压,方便人工检查
            try:
                if zipfile.is_zipfile(dest):
                    out = FILES / f"{c['id']}_unzip"
                    out.mkdir(exist_ok=True)
                    with zipfile.ZipFile(dest) as zf:
                        zf.extractall(out)
                elif tarfile.is_tarfile(dest):
                    out = FILES / f"{c['id']}_untar"
                    out.mkdir(exist_ok=True)
                    with tarfile.open(dest) as tf:
                        tf.extractall(out)
            except Exception as e:
                print(f"[warn] {c['title']} 解压失败: {e}")
        if saved:
            c["file"] = saved[0].name          # mock server 分发的文件名
            c["local_files"] = [p.name for p in saved]
            print(f"[ok] {c['title']} ({c['category']}) -> {[p.name for p in saved]}")
        else:
            print(f"[FAIL] {c['title']}: 全部下载失败")
    CHALLENGES.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n附件目录: {FILES}")


if __name__ == "__main__":
    main()
