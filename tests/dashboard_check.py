#!/usr/bin/env python3
"""看板前端闸门:在内嵌 HTML/JS 里,把"只有浏览器才能发现"的低级错误提前抓出来。

为什么要它:控制台的 HTML/CSS/JS 全在 dashboard.py 的 PAGE 字符串里,Python 语法检查
管不到里面。上一轮就踩过——JS 里写了 TS 类型注解,`PAGE` 照样导入成功,浏览器一打开整页白屏;
还有 `$('xxx')` 引用了不存在的元素 id,页面不报错但功能静默失效。

跑三件事:
  1) 抽出 <script> 跑 `node --check`(JS 语法)
  2) 扫 TS 专属写法(Record<...> / any[] / : string 之类),内嵌 JS 里不该出现
  3) JS 里 $(...) / getElementById(...) 引用的每个 id,必须在 HTML 里真实存在

用法: python3 tests/dashboard_check.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  \033[32m✔\033[0m {name}")
    else:
        FAIL += 1
        print(f"  \033[31m✘\033[0m {name} {extra}")


def main() -> int:
    import dashboard

    page = dashboard.PAGE
    m = re.search(r"<script>(.*)</script>", page, re.S)
    check("PAGE 里有 <script> 段", bool(m))
    if not m:
        return 1
    js = m.group(1)

    # 1) node --check
    tmp = Path(tempfile.mkdtemp()) / "page.js"
    tmp.write_text(js, encoding="utf-8")
    try:
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True, timeout=30)
        check("JS 语法通过 `node --check`", r.returncode == 0,
              (r.stderr or "").strip().splitlines()[:3])
    except FileNotFoundError:
        print("  \033[33m!\033[0m 没装 node,跳过语法检查")

    # 2) TS 专属写法
    bad = re.findall(r":\s*(?:Record<|any\[\]|string\[\]|number\b)", js)
    check("内嵌 JS 里没有 TS 类型注解", not bad, str(bad[:3]))

    # 3) JS 引用的 id 必须存在
    html_part = page.split("<script>")[0]
    # 动态区块(攻击图的 svg/详情框)是 JS 模板里现生成的,所以把 JS 里的 id="..." 也算已知
    ids_html = set(re.findall(r'id="([^"]+)"', page))
    ids_js = set(re.findall(r"\$\('([^']+)'\)", js)) | set(re.findall(r"getElementById\('([^']+)'\)", js))
    ids_js = {i for i in ids_js if " " not in i}
    missing = sorted(i for i in ids_js if i not in ids_html and not i.startswith("f_"))
    check("JS 引用的元素 id 都在 HTML 里", not missing, f"缺: {missing}")

    # 4) 快捷入口:几个关键区块必须存在(重构时最容易漏)
    for need, why in (("tasks", "左栏任务列表"), ("view", "主区详情容器"),
                      ("proj", "项目选择器"), ("filters", "状态筛选"),
                      ("d_ settings".replace(" ", ""), "设置抽屉"), ("toast", "提示条")):
        check(f"骨架存在: {why}", f'id="{need}"' in html_part)

    print(f"\n{'='*46}\n通过 {PASS} 项,失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())