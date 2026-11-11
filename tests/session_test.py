#!/usr/bin/env python3
"""会话时长保护 + 重启接管 + 前端 JS 语法(离线,不联网不花 token)。

  python3 tests/session_test.py

背景(2026-09-27 那轮实测):234 个 worker 会话里 88 个(38%)死于 runner 重启/停机(中位只跑 39 秒、
2 次工具调用);24 个死于"容器已释放"(都是投入最深的,中位 15 分钟/30 次调用)。本测试盯住这三件事的修法。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✔\033[0m {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \033[31m✘\033[0m {name} {extra}")


def section(t: str) -> None:
    print(f"\n== {t} ==")


RUN_DIR = Path(tempfile.mkdtemp(prefix="rotom-session-test-"))
os.environ["ROTOM_RUN_DIR"] = str(RUN_DIR)
os.environ["PROFILE"] = "zhongce"
os.environ["BENCHMARK_TOKEN"] = "mock-token"
os.environ.pop("CONTEST_ADAPTER", None)
(RUN_DIR / "logs").mkdir(parents=True, exist_ok=True)

import board as board_mod  # noqa: E402
import runner  # noqa: E402


class FakeProc:
    def __init__(self, pid: int = 999999):
        self.pid = pid
        self._agent_log_path = RUN_DIR / "logs" / "fake.jsonl"
        self._agent_log_f = None

    def poll(self):
        return None


def add_worker(qid: str, slot: int, age: float):
    key = f"{qid}#{slot}"
    runner.workers[key] = {"proc": FakeProc(), "qid": qid, "slot": slot,
                           "started": time.time() - age, "timeout": 1200}
    return key


def seed_board(entries: list[dict]) -> None:
    board = {"questions": {}, "facts": [], "started_at": time.time()}
    for e in entries:
        board["questions"][e["question_id"]] = e
    (RUN_DIR / "board.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")


def unsolved(qid: str, **kw) -> dict:
    base = {"question_id": qid, "title": qid, "status": board_mod.STATUS_RUNNING,
            "attempts": 1, "slices": 0, "max_attempts": 0, "next_retry_at": 0,
            "is_solved": False, "flag_count": 1, "correct_flag_count": 0,
            "rotate_until": 0, "slice_start": time.time() - 99999, "slice_flags": 0,
            "container_ready": True}
    base.update(kw)
    return base


# ------------------------------------------------------------------ 轮换保护
section("轮换不再杀掉正在干活的 agent(ROTATE_WAIT_RUNNING)")
runner.QUESTION_SLICE_SECONDS = 60
runner.ROTATE_COOLDOWN = 1
runner.MAX_ACTIVE_CHALLENGES = 3
runner.ROTATE_WAIT_RUNNING = True
runner.WORKER_MIN_LIFETIME = 120
runner.workers.clear()
# 4 道题:3 道占着名额(时间片用满)、1 道排队 → 满足轮换条件
seed_board([unsolved("q1"), unsolved("q2"), unsolved("q3"),
            unsolved("q4", attempts=0, status=board_mod.STATUS_PENDING, slice_start=0)])
add_worker("q1", 1, age=600)          # 有 agent 在跑
add_worker("q2", 1, age=600)          # 有 agent 在跑
runner.rotate({})
board = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))
rotated = [q for q, e in board["questions"].items() if float(e.get("rotate_until") or 0) > 0]
check("有 agent 在跑的题没被轮换", set(rotated).isdisjoint({"q1", "q2"}), str(rotated))
check("没有 agent 在跑的题照常轮换", "q3" in rotated, str(rotated))

runner.ROTATE_WAIT_RUNNING = False     # 关掉开关 = 旧行为(时间片到就让位)
runner.workers.clear()
seed_board([unsolved("q1"), unsolved("q2"), unsolved("q3"),
            unsolved("q4", attempts=0, status=board_mod.STATUS_PENDING, slice_start=0)])
runner.WORKER_MIN_LIFETIME = 0
add_worker("q1", 1, age=600)
runner.rotate({})
board = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))
check("关掉开关后恢复旧行为(在跑的题也会让位)",
      float(board["questions"]["q1"].get("rotate_until") or 0) > 0)

section("新 agent 保护期(WORKER_MIN_LIFETIME)")
runner.WORKER_MIN_LIFETIME = 120
runner.workers.clear()
seed_board([unsolved("q1"), unsolved("q2"), unsolved("q3"),
            unsolved("q4", attempts=0, status=board_mod.STATUS_PENDING, slice_start=0)])
add_worker("q1", 1, age=10)            # 刚起来 10 秒 → 受保护
runner.rotate({})
board = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))
check("保护期内的新 agent 不被轮换",
      float(board["questions"]["q1"].get("rotate_until") or 0) == 0)
add_worker("q2", 1, age=200)           # 过了保护期
seed_board([unsolved("q1"), unsolved("q2"), unsolved("q3"),
            unsolved("q4", attempts=0, status=board_mod.STATUS_PENDING, slice_start=0)])
runner.rotate({})
board = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))
check("过了保护期(且开关关闭时)可以轮换",
      float(board["questions"]["q2"].get("rotate_until") or 0) > 0
      or float(board["questions"]["q3"].get("rotate_until") or 0) > 0)

# ------------------------------------------------------------------ 容器名额
section("容器名额:有 agent 在跑的题优先保住(hold_container)")
runner.workers.clear()
runner.ROTATE_WAIT_RUNNING = True
seed_board([unsolved("q1"), unsolved("q2")])
add_worker("q2", 1, age=300)
seen: dict[str, dict] = {}


class FakeApi:
    class ContestError(RuntimeError):
        pass

    @staticmethod
    def sync_containers(token, entries, max_active=0, **_kw):   # 兼容 harness 后续加参数(如 health)
        for e in entries:
            seen[e["question_id"]] = dict(e)
        return {e["question_id"]: {"container_ready": True, "container_status": "available",
                                   "container_addr": "127.0.0.1:1", "connection": {}} for e in entries}


_real_api = runner.contest_api
runner.contest_api = FakeApi
try:
    runner.manage_containers({"TEAM_TOKEN": "x"})
finally:
    runner.contest_api = _real_api
check("在跑的题带 hold_container=True(适配器据此保名额)", seen.get("q2", {}).get("hold_container") is True,
      str(seen.get("q2", {}).get("hold_container")))
check("没在跑的题 hold_container=False", seen.get("q1", {}).get("hold_container") is False)

# ------------------------------------------------------------------ 短会话接力(handoff)
section("短会话接力:到片不计失败,同题继续;预算耗尽才算一次失败")
runner.SESSION_SLICE_SECONDS = 1200
runner.WORKER_MIN_LIFETIME = 0
runner.ROTATE_WAIT_RUNNING = False
runner.workers.clear()
seed_board([unsolved("h1", attempts=1, attempt_started_at=time.time() - 1199)])
add_worker("h1", 1, age=1201)
runner.workers["h1#1"]["handoff"] = True          # 启动时按切片判定:到点=接力
runner.reap()
e1 = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))["questions"]["h1"]
check("接力:题目回到 pending(不停在 running)", e1["status"] == board_mod.STATUS_PENDING)
check("接力:attempts 被回滚(不计失败)", e1["attempts"] == 0, str(e1["attempts"]))
check("接力:attempt_started_at 保留(整段预算连续计时)", float(e1.get("attempt_started_at") or 0) > 0)
check("接力:立即重派(next_retry_at 已到期)", float(e1.get("next_retry_at") or 0) <= time.time())

runner.workers.clear()
seed_board([unsolved("h2", attempts=1, attempt_started_at=time.time() - 1900)])
add_worker("h2", 1, age=1201)
runner.workers["h2#1"]["handoff"] = False        # 完整预算耗尽:这是真正失败
runner.reap()
e2 = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))["questions"]["h2"]
check("预算耗尽:attempts 保留(下一次尝试才会触发 hint)", e2["attempts"] == 1, str(e2["attempts"]))
check("预算耗尽:attempt_started_at 清零(下一段重新计时)", float(e2.get("attempt_started_at") or 0) == 0)

# ------------------------------------------------------------------ 重启接管
section("重启接管在跑的 agent(ADOPT_WORKERS)")
proc = subprocess.Popen(["sleep", "60"])
log = RUN_DIR / "logs" / "adopt_case.jsonl"
log.write_text('{"type":"tool_execution_start","toolName":"bash","args":{"command":"ls"}}\n', encoding="utf-8")
(RUN_DIR / "logs" / "workers.json").write_text(json.dumps(
    [{"pid": proc.pid, "qid": "adopt-1", "slot": 1, "started": time.time() - 30, "log": str(log)}]),
    encoding="utf-8")
seed_board([unsolved("adopt-1")])

runner.workers.clear()
runner.ADOPT_WORKERS = True
_real_is_pi = runner._is_pi_pid
runner._is_pi_pid = lambda pid: pid == proc.pid      # 测试环境里用 sleep 冒充 pi
try:
    runner.handle_leftover_workers({})
finally:
    runner._is_pi_pid = _real_is_pi
check("接管:仍在跑的 worker 进了 workers 表", "adopt-1#1" in runner.workers, str(list(runner.workers)))
w = runner.workers.get("adopt-1#1") or {}
check("接管:用 ForeignWorker 包着(只实现 poll/日志路径)",
      type(w.get("proc")).__name__ == "ForeignWorker" and w.get("proc").poll() is None)
check("接管:黑板里该题保持 running(不是待派发)",
      json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))
      ["questions"]["adopt-1"]["status"] == board_mod.STATUS_RUNNING)
check("接管:退出后 poll() 能发现进程已死", (lambda: (proc.kill(), proc.wait(), w["proc"].poll() == 0)[-1])())

# 关掉开关 = 旧行为(杀掉遗留进程)
proc2 = subprocess.Popen(["sleep", "60"])
(RUN_DIR / "logs" / "workers.json").write_text(json.dumps(
    [{"pid": proc2.pid, "qid": "adopt-2", "slot": 1, "started": time.time(), "log": str(log)}]),
    encoding="utf-8")
runner.workers.clear()
runner.ADOPT_WORKERS = False
runner._is_pi_pid = lambda pid: pid == proc2.pid
try:
    runner.handle_leftover_workers({})
finally:
    runner._is_pi_pid = _real_is_pi
time.sleep(0.3)
check("关掉接管后:遗留进程被清掉(旧行为)", proc2.poll() is not None and not runner.workers)
if proc2.poll() is None:
    proc2.kill()

# ------------------------------------------------------------------ 前端 JS
section("前端 JS 语法(改 dashboard 的 JS 最容易静默白屏)")
try:
    import importlib
    import dashboard as dash
    importlib.reload(dash)
    import re
    scripts = re.findall(r"<script>(.*?)</script>", dash.PAGE, re.S)
    js = Path(tempfile.mkdtemp()) / "page.js"
    js.write_text("\n;\n".join(scripts), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    if r.returncode == 127 or "not found" in (r.stderr or ""):
        print("  (跳过:本机没有 node)")
    else:
        check("node --check 通过(无语法错误)", r.returncode == 0, r.stderr[:300])
    check("页面含策略包下拉框与项目删除入口",
          "f_PROFILE" in dash.PAGE and "deleteProject(" in dash.PAGE and "newproj_profile" in dash.PAGE)
except Exception as e:
    check("dashboard 可导入", False, f"{type(e).__name__}: {e}")

print(f"\n{'='*50}\n通过 {PASS} 项,失败 {FAIL} 项")
if FAILED:
    print("失败项:")
    for f in FAILED:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)