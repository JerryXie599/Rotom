#!/usr/bin/env python3
"""规模化回归测试:用大量假题验证调度器行为,不调用模型、不联网。

  python3 tests/scale_test.py [题目数]

验证的不变量:
  1. 同时在跑的 worker 数永不超过并发上限;
  2. 铺题是"广度优先"——所有题先拿到第一次尝试,再给任何题第二次;
  3. 同一题的 worker 数不超过 WORKERS_PER_QUESTION;
  4. worker 退出后槽位能被回收(不会漏派);
  5. 题目全部解出后不再派发。
"""

from __future__ import annotations

import os
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="wqh-scale-"))
os.environ["START_WORKERS"] = "6"
os.environ["MIN_WORKERS"] = "1"
os.environ["WORKERS_PER_QUESTION"] = "2"
os.environ["MAX_ATTEMPTS"] = "8"

import board as board_mod  # noqa: E402
import runner  # noqa: E402
import solver  # noqa: E402

board_mod.BOARD_PATH = TMP / "board.json"
board_mod.LOCK_PATH = TMP / "board.lock"
solver.WORK_DIR = TMP / "work"
solver.LOG_DIR = TMP / "logs"
solver.WORK_DIR.mkdir(parents=True, exist_ok=True)
solver.LOG_DIR.mkdir(parents=True, exist_ok=True)
runner.RUNNER_PID_FILE = TMP / "runner.pid"
runner.WORKERS_PID_FILE = TMP / "workers.pid"
runner.WORKERS_JSON = TMP / "workers.json"

N = int(sys.argv[1]) if len(sys.argv) > 1 else 24
CATEGORIES = ["web", "pwn", "misc", "crypto", "reverse", "forensics"]


class FakeProc:
    """伪 worker:只提供 runner 需要的接口。"""

    def __init__(self, pid: int, log_path: Path):
        self.pid = pid
        self.rc: int | None = None
        self._wqh_log_path = log_path
        self._wqh_log_f = open(log_path, "w", encoding="utf-8")

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        return self.rc


_next_pid = [10000]


def fake_launch(q, slot, total, wdir, env, timeout, attempt=1, **kw):  # 与 solver.launch 签名同步
    _next_pid[0] += 1
    log = solver.LOG_DIR / f"{q['question_id']}_w{slot}.jsonl"
    log.write_text('{"type":"agent_start"}\n', encoding="utf-8")
    return FakeProc(_next_pid[0], log)


def fake_questions(n: int) -> list[dict]:
    qs = []
    for i in range(n):
        cat = CATEGORIES[i % len(CATEGORIES)]
        qs.append({
            "question_id": f"q{i:03d}", "title": f"task{i:02d}", "category": cat,
            "score": 500, "real_score": 500, "file_url": "", "is_solved": False,
            "solved_number": 0, "description": "synthetic", "interactive": "false",
            "attributes": [], "capabilities": [], "connection": [], "extensions": {},
        })
    return qs


# ---- 打桩:不联网、不启动真进程 ----
QUESTIONS = fake_questions(N)
os.environ["IGNORE_SOLVED"] = "0"
import contest_api  # noqa: E402
contest_api.list_questions = lambda token, timeout=30: QUESTIONS
solver.prepare_project = lambda q: (
    solver.WORK_DIR / q["title"], solver.WORK_DIR / q["title"] / "files",
    solver.WORK_DIR / q["title"] / "SHARED.md")
solver.prepare_worker_dir = lambda q, slot: solver.WORK_DIR / f"{q['title']}-w{slot}"
solver.launch = fake_launch
runner.reconcile = lambda env: QUESTIONS
runner.env_config = lambda: {"TEAM_TOKEN": "t", "PI_PROVIDER": "p", "PI_MODEL_NAME": "m"}


def pump() -> None:
    """spawn 之后等待异步准备完成并启动 worker(生产环境每轮有 5s 间隔,测试需要显式等)。"""
    runner.spawn(runner.env_config())
    for _ in range(200):
        runner.launch_prepared(runner.env_config())
        if not runner._prepping:
            break
        time.sleep(0.005)


failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        failures.append(msg)


def board_state() -> dict:
    with board_mod.locked() as b:
        return {qid: dict(e) for qid, e in b["questions"].items()}


def slots_used() -> int:
    return len(runner.workers)


def questions_touched() -> int:
    return sum(1 for e in board_state().values() if e["attempts"] > 0)


print(f"=== 规模化测试: {N} 道题, 并发上限 {runner.START_WORKERS}, 每题最多 {runner.WORKERS_PER_QUESTION} 个 agent ===")

with board_mod.locked() as b:
    for q in QUESTIONS:
        board_mod.upsert_question(b, q)

# 第 1 轮:一次性拉取所有题目后派发
pump()
st = board_state()
per_question = {}
for w in runner.workers.values():
    per_question[w["qid"]] = per_question.get(w["qid"], 0) + 1

print(f"\n[第 1 轮派发] 在跑 {slots_used()} 个 worker,覆盖 {len(per_question)} 道题")
check(slots_used() <= runner.START_WORKERS, f"并发不超过上限({slots_used()} <= {runner.START_WORKERS})")
check(len(per_question) == runner.START_WORKERS, "所有槽位优先分配给不同题目(广度优先)")
check(all(v <= runner.WORKERS_PER_QUESTION for v in per_question.values()),
      f"同题 worker 数不超过 {runner.WORKERS_PER_QUESTION}")
check(questions_touched() == len(per_question), "只有已派发的题被计入尝试次数")

# 尝试次数在真正启动后才计:准备阶段(status=running 但无 worker)不应消耗次数
with board_mod.locked() as b:
    for e in b["questions"].values():
        e["attempts"] = 0
runner.workers.clear()
runner._prepping.clear()
runner.spawn(runner.env_config())
prep_attempts = sum(e["attempts"] for e in board_state().values())
check(prep_attempts == 0, "计划/准备阶段不消耗尝试次数(只有真正启动才计)")
runner.launch_prepared(runner.env_config())
import time as _t; _t.sleep(0.3); runner.launch_prepared(runner.env_config())
launched_attempts = sum(e["attempts"] for e in board_state().values())
check(launched_attempts == runner.START_WORKERS, f"启动后尝试次数等于实际启动数({launched_attempts})")

# 第 2 轮:模拟 3 个 worker 崩溃退出,槽位应被回收并继续派新题
victims = list(runner.workers.keys())[:3]
for k in victims:
    runner.workers[k]["proc"].rc = 1
runner.reap()
reaped_state = board_state()
check(slots_used() == runner.START_WORKERS - 3, f"退出后槽位释放({slots_used()} 个仍在跑)")
pump()
per_question2 = {}
for w in runner.workers.values():
    per_question2[w["qid"]] = per_question2.get(w["qid"], 0) + 1
print(f"[第 2 轮派发] 补位后在跑 {slots_used()} 个 worker,覆盖 {len(per_question2)} 道题")
check(slots_used() == runner.START_WORKERS, "崩溃后能补满到并发上限")
check(len(per_question2) == runner.START_WORKERS, "补位仍优先给没跑过的题(广度优先)")

# 第 3 轮:反复"跑挂-补位",检查最终能覆盖多少题、是否有题被饿死
rounds = 0
while rounds < 200 and any(e["status"] == board_mod.STATUS_PENDING for e in board_state().values()):
    rounds += 1
    for k in list(runner.workers.keys()):  # 全部模拟失败退出,逼迫重试
        runner.workers[k]["proc"].rc = 1
    runner.reap()
    pump()
st = board_state()
touched = sum(1 for e in st.values() if e["attempts"] > 0)
print(f"\n[压力轮转] {rounds} 轮后: 尝试过的题 {touched}/{N},在跑 {slots_used()}")
check(touched == N, "所有题目都至少被尝试过一次(没有题被饿死)")
check(slots_used() <= runner.START_WORKERS, "全程并发未超上限")

# 第 4 轮:全部解出后不再派发
with board_mod.locked() as b:
    for e in b["questions"].values():
        e["status"] = board_mod.STATUS_SOLVED
        e["is_solved"] = True
for k in list(runner.workers.keys()):
    runner.workers[k]["proc"].rc = 0
runner.reap()
before = slots_used()
pump()
print(f"\n[全部解出] 解出前在跑 {before} 个 → 派发后 {slots_used()} 个")
check(slots_used() == 0, "全部解出后不再派发新 worker")

print("\n" + ("全部通过 ✔" if not failures else f"失败 {len(failures)} 项:"))
for f in failures:
    print("  -", f)
sys.exit(1 if failures else 0)
