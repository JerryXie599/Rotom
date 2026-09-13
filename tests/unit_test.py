#!/usr/bin/env python3
"""离线单元测试:不联网、不调用模型,覆盖关键逻辑路径。

  python3 tests/unit_test.py

覆盖:挑战窗口(time_left/window_closing)、并发降级步长、靶场题目映射与提交判定、
比赛接口地址解析与提交判定、黑板状态机(重置/收尾)、方向 playbook 注入与时间预算。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        FAILED.append(msg)


# ---------------------------------------------------------------- 挑战窗口
print("== 挑战窗口 ==")
os.environ["ROUND_WINDOW_MINUTES"] = "30"
os.environ["STOP_SPAWN_BEFORE_END"] = "120"
import runner  # noqa: E402

runner.RUN_DEADLINE = time.time() + 1800
check(1795 <= runner.time_left() <= 1800, f"30 分钟窗口剩余秒数正确({runner.time_left()})")
check(not runner.window_closing(), "刚开始时不应停止派题")
runner.RUN_DEADLINE = time.time() + 60
check(runner.window_closing(), "剩 60s(≤STOP_SPAWN_BEFORE_END)应停止派新题")
runner.RUN_DEADLINE = 0
check(runner.time_left() == -1 and not runner.window_closing(), "未设窗口时不限时")

print("\n== 并发降级 ==")
c = runner.Concurrency(24, 4, cooldown=0)
seq = []
for _ in range(12):
    if c.reduce("test"):
        seq.append(c.limit)
check(seq[:3] == [20, 16, 13], f"高并发按 20% 递减({seq[:3]})")
check(seq[-1] == 4 and min(seq) >= 4, f"触底为下限 4(实际 {seq[-1]})")
c2 = runner.Concurrency(24, 4, cooldown=999)
c2.reduce("t")
check(not c2.reduce("t"), "冷却期内不重复降级")

# ---------------------------------------------------------------- 靶场适配
print("\n== 靶场适配(arena_api) ==")
import arena_api  # noqa: E402

att = {"id": 8322, "remaining_seconds": 3599, "wrong_count": 0,
       "problem": {"type_label": "Pwn", "rating": 1284, "content": "ret2text",
                   "annex": {"name": "ret_text_v0", "url": "https://files.nssctf.cn/x"},
                   "container": {"url": ["node4.anna.nssctf.cn:20178"]}}}
q = arena_api._to_question(att)
check(q["question_id"] == "arena-8322", "question_id 合成为 arena-<id>")
check(q["category"] == "pwn" and "Pwn" in q["title"], f"类别/标题映射正确({q['title']})")
check(q["file_url"].startswith("https://files.nssctf.cn"), "附件 URL 已带上")
check(q["connection"]["docker_port"] == "20178", "host:port 容器地址解析正确")
check(q["interactive"] == "true", "容器题标记为 interactive")
att2 = {"id": 1, "problem": {"type_label": "Web", "content": "x", "annex": None,
                             "container": {"url": ["http://host:8000"]}}}
check(arena_api._to_question(att2)["connection"]["docker_url"] == "http://host:8000", "http 容器地址原样保留")

_orig_req = arena_api._req
arena_api._req = lambda *a, **k: {"correct": True, "state_label": "solved", "message": "ok"}
r = arena_api.submit_flag("t", "arena-9", "NSSCTF{x}")
check(arena_api.is_correct(r), "靶场答对映射为 status=1")
arena_api._req = lambda *a, **k: {"correct": False, "failed": False, "message": "答案错误"}
check(not arena_api.is_correct(arena_api.submit_flag("t", "arena-9", "flag{x}")), "靶场答错映射为 status=0")
arena_api._req = _orig_req

# ---------------------------------------------------------------- 比赛接口
print("\n== 比赛接口(contest_api) ==")
import contest_api  # noqa: E402

check(contest_api.is_correct({"code": 0, "message": "恭喜您，回答正确"}), "首次答对(无 status 字段)判定为正确")
check(contest_api.is_correct({"code": 0, "message": "答案正确，该题目已被攻克，不计分", "status": 1}),
      "重复提交已攻克题判定为正确")
check(not contest_api.is_correct({"code": 0, "message": "答案错误", "status": 0}), "答错判定为错误")
check(not contest_api.is_correct({"code": 101, "message": "暂无队伍信息"}), "业务错误判定为错误")
os.environ["CONTEST_BASE"] = "https://apiterminator.ichunqiu.com"
os.environ.pop("CONTEST_QUERY_PATH", None)
check(contest_api.endpoint("QUERY").endswith("/04cb510e425bd8f64fa97ba66f3935e1"), "默认查题地址正确")
os.environ["CONTEST_SUBMIT_PATH"] = "https://other.example/submit"
check(contest_api.endpoint("SUBMIT") == "https://other.example/submit", "整条 URL 覆盖时原样使用")
os.environ["CONTEST_BASE"] = "https://x.example"
os.environ.pop("CONTEST_SUBMIT_PATH", None)
check(contest_api.endpoint("SUBMIT").startswith("https://x.example/"), "换 Base 后自动拼接路径")
for k in ("CONTEST_BASE",):
    os.environ.pop(k, None)

# ---------------------------------------------------------------- 黑板状态机
print("\n== 黑板状态机 ==")
import board as board_mod  # noqa: E402

tmp = Path(tempfile.mkdtemp())
board_mod.RUN_DIR = tmp
board_mod.BOARD_PATH = tmp / "board.json"
board_mod.LOCK_PATH = tmp / "board.lock"
with board_mod.locked() as b:
    board_mod.upsert_question(b, {"question_id": "Q1", "title": "t1", "category": "web"})
with board_mod.locked() as b:
    b["questions"]["Q1"]["status"] = board_mod.STATUS_RUNNING
runner.reset_running_to_pending()
with board_mod.locked() as b:
    check(b["questions"]["Q1"]["status"] == board_mod.STATUS_PENDING, "退出时 running 重置为 pending")
    b["questions"]["Q1"]["status"] = board_mod.STATUS_SOLVED
board_mod.IGNORE_SOLVED = True
runner.reset_solved_for_practice()
with board_mod.locked() as b:
    check(b["questions"]["Q1"]["status"] == board_mod.STATUS_PENDING, "练习模式下已解出的题被重置")
board_mod.IGNORE_SOLVED = False

# ---------------------------------------------------------------- prompt 组装
print("\n== prompt 组装 ==")
import solver  # noqa: E402

check(solver.category_playbook("pwn").startswith("# Pwn"), "pwn 方向能注入 playbook")
check(solver.category_playbook("unknown-cat") == "", "未知方向不注入")
os.environ.pop("RUN_DEADLINE_TS", None)
check(solver._time_rule() == "", "无窗口时不给时间预算")
os.environ["RUN_DEADLINE_TS"] = str(int(time.time()) + 600)
rule = solver._time_rule()
check("时间预算" in rule and "10 分钟" in rule, f"有窗口时给出剩余分钟({rule.splitlines()[1][:24]}…)")
os.environ.pop("RUN_DEADLINE_TS", None)
q_pwn = {"question_id": "x", "title": "t", "category": "pwn", "score": 500, "description": "d",
         "attributes": [], "capabilities": [], "connection": [], "extensions": {}}
p = solver.build_prompt(q_pwn, 1, 2, Path("/tmp/w1"))
check("Pwn 二进制漏洞利用 playbook" in p or "本方向打法速览" in p, "prompt 内含该方向 playbook")

print("\n" + ("全部通过 ✔" if not FAILED else f"失败 {len(FAILED)} 项:"))
for f in FAILED:
    print("  -", f)
sys.exit(1 if FAILED else 0)
