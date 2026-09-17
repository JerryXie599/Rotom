"""黑板(Blackboard):参考 Cairn 的共享事实板,持久化到 board.json。

runner 与 submit 工具都通过它交换状态,文件加锁(flock)保证并发安全。

每道题目的状态机:
  pending -> running -> solved (平台确认 is_solved / 提交返回正确)
                   `-> failed  (worker 超时或退出,还有重试次数则回到 pending)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 数据目录(黑板/工作区/日志)按项目隔离:每个项目一个目录,由 ROTOM_RUN_DIR 指定,
# 不设则用代码根目录(即"默认项目",兼容命令行单独运行)。
RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)
BOARD_PATH = RUN_DIR / "board.json"
LOCK_PATH = RUN_DIR / "board.lock"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SOLVED = "solved"
STATUS_FAILED = "failed"

# 练习模式:平台已标记 is_solved 的题也当作未解继续跑(测试日复练用,正式比赛保持 0)
IGNORE_SOLVED = os.environ.get("IGNORE_SOLVED", "0") == "1"


@contextlib.contextmanager
def locked():
    """持有黑板锁,进入时读盘,退出时写盘。用法: with locked() as board: ..."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)   # 数据目录可能还没建(换项目/靶场首跑)
    LOCK_PATH.touch(exist_ok=True)
    with open(LOCK_PATH, "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        board = _read()
        yield board
        _write(board)


def _read() -> dict:
    if BOARD_PATH.exists():
        try:
            return json.loads(BOARD_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"questions": {}, "facts": [], "started_at": time.time()}


def _write(board: dict) -> None:
    tmp = BOARD_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(board, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, BOARD_PATH)


def upsert_question(board: dict, q: dict) -> dict:
    """把平台返回的题目并入黑板,返回黑板中的条目。"""
    if IGNORE_SOLVED:
        q = {**q, "is_solved": False}
    qid = q["question_id"]
    entry = board["questions"].get(qid)
    if entry is None:
        entry = {
            "question_id": qid,
            "status": STATUS_PENDING,
            "attempts": 0,
            "max_attempts": int(os.environ.get("MAX_ATTEMPTS", "0")),   # 0 = 不限次数
            "next_retry_at": 0,
            "worker_pid": None,
            "started_at": None,
            "finished_at": None,
            "note": "",
        }
        board["questions"][qid] = entry
    # 平台字段每次都刷新(附件/容器地址可能变化)
    entry.update({
        "title": q.get("title", ""),
        "category": q.get("category", ""),
        "score": q.get("score", 0),
        "description": q.get("description", ""),
        "file_url": q.get("file_url", ""),
        "interactive": q.get("interactive", "false"),
        "connection": q.get("connection") or {},
        "attributes": q.get("attributes") or [],
        "capabilities": q.get("capabilities") or [],
        "extensions": q.get("extensions") or {},
        "is_solved": bool(q.get("is_solved")),
    })
    return entry


def add_fact(board: dict, source: str, text: str) -> None:
    """写一条事实(参考 Cairn 的 Fact)。供 submit 工具和 runner 记录关键事件。"""
    board["facts"].append({"ts": time.strftime("%H:%M:%S"), "source": source, "text": text})
    board["facts"] = board["facts"][-500:]
