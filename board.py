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
            "attempts": 0,          # worker 启动次数(在一段"完整尝试"结束时才净增;handoff 会回滚)
            "session_elapsed": 0,     # 兼容字段:当前完整尝试累计秒数(见 attempt_started_at)
            "attempt_started_at": 0,  # 当前完整尝试的起点(跨节接力/runner 重启都保留;0=未开始)
            "slices": 0,            # 已用掉几个完整时间片(决定派题优先级)
            # 上一次真正开跑的时刻:低并发时用它做"最久没被服务的题先上"的排序键。
            # 不记这个的话,slices/level 全打平时排序是稳定的,列表里第一道题会永久占住唯一槽位
            # (2026-10-28 实测:并发被限流降到 1 后,066 连跑 2 小时,081/092 零派发)。
            "last_started_at": 0,
            # 链式安全阀专用计数:**连续几个时间片没有任何新 flag**。
            # 为什么不能用 attempts:attempts 记的是"worker 启动次数",会被 harness 自己的动作灌大
            # (runner 重启 / 收尾期增援 / 容器被回收后重派)——2026-10-08 第七次实测:b-02 手里有 2/6 个 flag,
            # 却被判 attempts=8 ≥ 链式安全阀 4 → 摘掉"钉名额"身份 → 每 45 分钟被轮换一次,
            # 容器一关整条 webshell+socat 链条清零,agent 反复从零重建(黑板上的第 7/8 次接手),最后卡在 2/6。
            # 口径回到本意:只有"连着几个片一点进展都没有"才放它走;一拿到 flag 就清零。
            "stale_slices": 0,
            "max_attempts": int(os.environ.get("MAX_ATTEMPTS", "0")),   # 0 = 不限次数
            "next_retry_at": 0,
            "worker_pid": None,
            "started_at": None,
            "finished_at": None,
            "note": "",
            # 多 flag 进度(一道题可能有 flag_count 个 flag,全拿到才算通关)
            "flag_count": 1,
            "correct_flag_count": 0,
            "found_flags": [],
            # 正式渗透用的 finding 记账(flag 模式下一直是 0)
            "findings_count": 0,
            "findings": [],
            "delivered": False,
            # 容器地址变更史(容器重开 IP/URL 会变:旧地址进这里,黑板顶部块会明确标失效)
            "dead_addrs": [],
            # 容器名额轮换(平台同时只允许 3 道题开着容器,见 runner.rotate)
            "container_ready": True,
            "container_status": "",
            # 新榜单适配器:入口票据已兑换成会话 Cookie,worker 起手写 cookies.txt
            "http_cookie": "",
            "ticket_url": "",
            # 静态附件题(阶段 2/3):全部附件地址由 solver 下载到 files/
            "attachments": [],
            "rotate_until": 0,
            "slice_start": 0,
            "slice_flags": 0,
        }
        board["questions"][qid] = entry
    # 老黑板迁移:短会话接力的"完整尝试起点"从 0 开始,不影响旧轮次。
    entry.setdefault("session_elapsed", 0)
    entry.setdefault("attempt_started_at", 0)
    entry.setdefault("last_started_at", 0)
    # 平台字段每次都刷新(附件/容器地址可能变化)
    entry.update({
        "title": q.get("title", ""),
        "category": q.get("category", ""),
        "score": q.get("score", 0),
        "description": q.get("description", ""),
        "file_url": q.get("file_url", ""),
        "file_name": q.get("file_name", entry.get("file_name", "")),
        "attachments": q.get("attachments") or entry.get("attachments") or [],
        "interactive": q.get("interactive", "false"),
        "attributes": q.get("attributes") or [],
        "capabilities": q.get("capabilities") or [],
        "extensions": q.get("extensions") or {},
        # is_solved 是**粘性**的:一旦本地判成已解(平台确认 / report_finding --done),
        # 后续每轮刷新不许把它翻回 False —— 否则正式渗透里"已交付"的目标会被反复重派。
        "is_solved": bool(q.get("is_solved")) or entry["status"] == STATUS_SOLVED,
    })
    # 会话 Cookie 只在平台给了新值时覆盖(避免刷新题单时把可用的旧会话擦掉)
    for k in ("http_cookie", "ticket_url"):
        if q.get(k):
            entry[k] = q[k]
    for k in ("flag_count", "correct_flag_count", "difficulty", "level", "container_ready"):
        if q.get(k) is not None:
            if k == "correct_flag_count":
                # 进度单调不回退:适配器可能不提供进度(新榜单每题只回 Correct/Wrong),
                # 直接覆盖会把本地已记账的 flag 刷回 0(实测顶栏 flag 0/40)。
                entry[k] = max(int(entry.get(k) or 0), int(q[k]))
            else:
                entry[k] = q[k]
    # 容器地址只在平台确认可用时才覆盖(否则会把上一轮拿到的好地址擦掉)
    if q.get("connection"):
        entry["connection"] = q["connection"]
    # workdir 同理只在目标清单显式给出时才写(涉诈场景:绝对路径指到站点档案/工作区;
    # 不给则由 runner 派发前按 resolve_folder 兜底,老行为不变)
    if q.get("workdir"):
        entry["workdir"] = q["workdir"]
    if q.get("container_addr"):
        entry["container_addr"] = q["container_addr"]
    if q.get("container_status"):
        entry["container_status"] = q["container_status"]
    return entry


def flag_progress(entry: dict) -> str:
    """给看板/日志用的一行进度:形如 `1/2 flag`(单 flag 题返回空串)。"""
    total = int(entry.get("flag_count") or 0)
    if total <= 1:
        return ""
    return f"{int(entry.get('correct_flag_count') or 0)}/{total} flag"


def add_fact(board: dict, source: str, text: str) -> None:
    """写一条事实(参考 Cairn 的 Fact)。供 submit 工具和 runner 记录关键事件。"""
    facts = board.setdefault("facts", [])       # 老版本/手工构造的黑板可能没有 facts 字段
    facts.append({"ts": time.strftime("%H:%M:%S"), "source": source, "text": text})
    board["facts"] = facts[-500:]
