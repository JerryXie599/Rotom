#!/usr/bin/env python3
"""提交 flag —— agent 在解题过程中通过 bash 调用本脚本。

用法:
  TEAM_TOKEN=xxx python3 tools/submit_flag.py <question_id> "<flag>"

退出码与含义(提示词里也写了,agent 据此决定下一步):
  0  CORRECT       —— 答对,本题结束
  1  WRONG         —— 平台**判定答案不对**,才需要回去继续分析
  2  用法错误
  3  SUBMIT_BUSY / SUBMIT_ERROR —— 平台**没受理**(限流「操作太过频繁」/接口异常/网络)。
                    这不是答错:不要改 flag、不要重复提交,稍后重试即可。
结果会追加到 logs/submissions.log,并写入黑板 facts。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.environ.get("CONTEST_ADAPTER", "").lower() == "arena":
    import arena_api as contest_api  # 靶场模式
else:
    import contest_api  # noqa: E402
import board as board_mod  # noqa: E402

# 靶场适配器(arena_api)目前只实现了 submit_flag/is_correct —— 缺新接口时兜底回旧行为,
# 保证切到靶场模式不会因为 AttributeError 直接挂掉。
_submit = getattr(contest_api, "submit_with_retry", None) or contest_api.submit_flag
_is_verdict = getattr(contest_api, "is_verdict", None) or (lambda r: True)
_is_rate_limited = getattr(contest_api, "is_rate_limited", None) or (lambda r: False)

# 日志必须落在**当前项目/靶场的数据目录**里,否则审计记录会散到全局 logs/
RUN_DIR = Path(os.environ.get("WQH_RUN_DIR") or ROOT)
LOG_DIR = RUN_DIR / "logs"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: submit_flag.py <question_id> <flag>", file=sys.stderr)
        return 2
    qid, flag = sys.argv[1], sys.argv[2].strip()
    if os.environ.get("CONTEST_ADAPTER", "").lower() == "arena":
        token = os.environ.get("NSSCTF_AGENT_TOKEN", "") or contest_api.agent_token()
    else:
        token = os.environ.get("TEAM_TOKEN", "")
    if not token:
        print("未设置 token(TEAM_TOKEN 或 NSSCTF_AGENT_TOKEN)", file=sys.stderr)
        return 2

    try:
        resp = _submit(token, qid, flag)  # 限流会自动退避重试
    except Exception as e:  # 网络错误,让 agent 可以重试
        print(f"SUBMIT_ERROR question={qid} error={e}", file=sys.stderr)
        return 3

    accepted = _is_verdict(resp)                 # 平台受理了这份提交吗
    ok = accepted and contest_api.is_correct(resp)
    busy = (not accepted) and _is_rate_limited(resp)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "submissions.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.strftime("%F %T"), "question_id": qid,
            "flag": flag, "ok": ok, "accepted": accepted, "resp": resp,
        }, ensure_ascii=False) + "\n")

    if not accepted:
        # 平台没受理(限流/业务错误):**不是答错**,不写 WRONG、不消耗"错误提交"额度,
        # 只提醒稍后重试。第二轮实测就吃过这个亏:限流被记成 WRONG,agent 会去重推 flag。
        why = "限流" if busy else "接口错误"
        with board_mod.locked() as b:
            board_mod.add_fact(b, "submit",
                               f"{qid} 提交未被平台受理({why}): {str(resp.get('message') or resp)[:60]}"
                               f" → 稍后重试即可(不是答错,别改 flag)")
        print(f"{'SUBMIT_BUSY' if busy else 'SUBMIT_ERROR'} question={qid} resp={resp}", file=sys.stderr)
        return 3

    with board_mod.locked() as b:
        board_mod.add_fact(b, "submit",
                           f"{qid} flag={flag!r} -> {'CORRECT' if ok else 'WRONG'}")
        if ok and qid in b["questions"]:
            b["questions"][qid]["status"] = board_mod.STATUS_SOLVED
            b["questions"][qid]["finished_at"] = time.time()

    if ok:
        print(f"CORRECT question={qid} resp={resp}")
        return 0
    print(f"WRONG question={qid} resp={resp}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
