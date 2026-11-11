#!/usr/bin/env python3
"""拿平台提示 —— agent **自主决定**何时用(2026-10-26 策略:提示不强制注入,harness 不再代取)。

用法:
  python3 tools/get_hint.py <question_id>

行为:
  - 打印该题的平台提示原文(拿到就照着干,别再重复侦察);
  - **每道题只要取过一次,该题后续每个正确 flag 的得分按 10% 折减**(easy -20 / medium -30 / hard -50)——
    所以:能自己解就别拿;方向穷尽/卡了十来分钟再拿;拿了就用;
  - 同一道题重复调用只返回同一份提示(平台口径:看过就记一次账),但没必要重复拿;
  - 取回会写一条黑板 fact(`agent 主动取了 X 的平台提示`),方便审查对账。

退出码:0 取到(或此前已取过,原样重放);2 用法/配置错误;3 平台没给(无提示/网络)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 适配器解析与 submit_flag.py 同口径:环境变量 → .env → 兜底 tsec
try:
    import config as _cfg
    _ADAPTER = _cfg.adapter_name()
except Exception:                                               # noqa: BLE001
    _ADAPTER = os.environ.get("CONTEST_ADAPTER", "").lower()
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
import board as board_mod  # noqa: E402

RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)


def _from_env_file(key: str) -> str:
    if os.environ.get(key):
        return ""
    try:
        return str(_cfg.read_env().get(key) or "")
    except Exception:                                           # noqa: BLE001
        return ""


def _token() -> str:
    return (os.environ.get("BENCHMARK_TOKEN") or os.environ.get("TEAM_TOKEN")
            or _from_env_file("BENCHMARK_TOKEN") or _from_env_file("TEAM_TOKEN") or "").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python3 tools/get_hint.py <question_id>", file=sys.stderr)
        return 2
    qid = sys.argv[1].strip()
    if not hasattr(contest_api, "get_hint"):
        print("当前平台没有提示接口。", file=sys.stderr)
        return 2
    tok = _token()
    if not tok:
        print("未配置 BENCHMARK_TOKEN/TEAM_TOKEN,取不了提示。", file=sys.stderr)
        return 2
    try:
        hint = contest_api.get_hint(tok, qid) or ""
    except Exception as e:                                      # noqa: BLE001
        print(f"取提示失败({type(e).__name__}: {e})—— 稍后可以再试;先继续自己打。")
        return 3
    if not hint:
        print("平台没有给这道题的提示(或暂时取不到)。先自己打。")
        return 3
    with board_mod.locked() as b:
        board_mod.add_fact(b, "agent", f"agent 主动取了 {qid} 的平台提示(该题得分按 10% 折减)")
    print(f"【平台提示·{qid}·该题得分已按 10% 折减】\n{hint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())