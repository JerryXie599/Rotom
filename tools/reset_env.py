#!/usr/bin/env python3
"""重置容器题环境 —— 仅 interactive=true 的题目可用。

用法:
  TEAM_TOKEN=xxx python3 tools/reset_env.py <question_id>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 适配器:环境变量 → .env 文件 → 空。**别只看 os.environ** —— 托管容器里 .env 可能没导出,
# 那样会静默回退到 CTF 适配器(旧域名),提交全废(第六次 Docker 测试实测)。
try:
    import config as _cfg
    _ADAPTER = _cfg.adapter_name()
except Exception:                                               # noqa: BLE001
    _ADAPTER = os.environ.get("CONTEST_ADAPTER", "").lower()
# 兜底:平台 token/base 在,但适配器没解析出来 → 按 tsec 走(宁可显式失败也别打错平台)
if not _ADAPTER and (os.environ.get("BENCHMARK_TOKEN") or os.environ.get("BENCHMARK_BASE_URL")):
    _ADAPTER = "tsec"
if _ADAPTER == "arena":
    import arena_api as contest_api
elif _ADAPTER == "tsec":
    import tsec_api as contest_api
elif _ADAPTER == "newrank":
    import newrank_api as contest_api
else:
    import contest_api  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: reset_env.py <question_id>", file=sys.stderr)
        return 2
    if _ADAPTER == "tsec":
        # 众测平台没有重置接口:容器由 harness 统一轮换(关闭后重新启动),agent 不需要也不能自己重启
        print("该平台不支持重置环境;目标不可用时请在 SHARED.md 记录,harness 会轮换容器")
        return 1
    token = (os.environ.get("NSSCTF_AGENT_TOKEN", "") or contest_api.agent_token()
             if _ADAPTER == "arena"
             else os.environ.get("TEAM_TOKEN", ""))
    if not token:
        print("未设置 token", file=sys.stderr)
        return 2
    try:
        resp = contest_api.reset_env(token, sys.argv[1])
    except Exception as e:
        print(f"RESET_ERROR {e}", file=sys.stderr)
        return 3
    print(resp)
    return 0 if resp.get("code") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
