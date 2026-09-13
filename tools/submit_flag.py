#!/usr/bin/env python3
"""提交 flag —— agent 在解题过程中通过 bash 调用本脚本。

用法:
  TEAM_TOKEN=xxx python3 tools/submit_flag.py <question_id> "<flag>"

仅在答案正确时 exit 0;答案错误或接口异常时 exit 非 0。
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

import contest_api  # noqa: E402
import board as board_mod  # noqa: E402

LOG_DIR = ROOT / "logs"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: submit_flag.py <question_id> <flag>", file=sys.stderr)
        return 2
    qid, flag = sys.argv[1], sys.argv[2].strip()
    token = os.environ.get("TEAM_TOKEN", "")
    if not token:
        print("TEAM_TOKEN 未设置", file=sys.stderr)
        return 2

    try:
        resp = contest_api.submit_flag(token, qid, flag)
    except Exception as e:  # 网络错误,让 agent 可以重试
        print(f"SUBMIT_ERROR question={qid} error={e}", file=sys.stderr)
        return 3

    ok = contest_api.is_correct(resp)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "submissions.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.strftime("%F %T"), "question_id": qid,
            "flag": flag, "ok": ok, "resp": resp,
        }, ensure_ascii=False) + "\n")

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
