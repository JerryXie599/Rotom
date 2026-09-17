#!/usr/bin/env python3
"""Dispatcher 主循环(参考 Cairn 的 dispatcher 角色)。

策略:
  1. 先高并发抢跑:START_WORKERS 个 worker 并行,优先铺满不同的题目;
     还有空位时,同一道题可以起多个 agent(WORKERS_PER_QUESTION),通过 SHARED.md 协同。
  2. 命中模型限流/并发上限时**逐步降并发**(每次 -1,冷却后生效,降到 MIN_WORKERS 为止)。
  3. 拿到 flag 立即由 agent 调用提交工具提交;平台确认后杀掉该题所有 worker,继续推其它题。
  4. 全解完也不退出(KEEP_RUNNING=1),持续轮询等待新题。

用法:
  ./run.sh                 # 全自动
  ./run.sh --once          # 干跑:只拉题目看状态
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)
sys.path.insert(0, str(ROOT))

import board as board_mod  # noqa: E402
if os.environ.get("CONTEST_ADAPTER", "").lower() == "arena":
    import arena_api as contest_api  # 靶场模式:NSSCTF Agent Arena(接口同名同签名)
else:
    import contest_api  # noqa: E402
import solver  # noqa: E402


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


POLL_INTERVAL = _int("POLL_INTERVAL", 5)
START_WORKERS = _int("START_WORKERS", 6)          # 起始并发(高并发抢跑)
MIN_WORKERS = _int("MIN_WORKERS", 1)              # 并发下限(降到这里为止)
WORKERS_PER_QUESTION = _int("WORKERS_PER_QUESTION", 2)  # 同题默认几个 agent
MAX_HELPERS_PER_QUESTION = _int("MAX_HELPERS_PER_QUESTION", 4)  # agent 在黑板请求增援后,同题最多几个 agent
WORKER_TIMEOUT = _int("WORKER_TIMEOUT", 1500)
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 0)            # 单题总尝试次数;0=不限(以窗口时间为界)
REDUCE_COOLDOWN = _int("REDUCE_COOLDOWN", 60)     # 两次降并发之间的冷却(秒)
FAILED_RETRY_AFTER = _int("FAILED_RETRY_AFTER", 600)  # 放弃的题多久后重新拾起(0=不重拾)
QUOTA_BACKOFF = _int("QUOTA_BACKOFF", 900)        # 额度耗尽后的暂停时长(秒)
KEEP_RUNNING = os.environ.get("KEEP_RUNNING", "1") == "1"
API_RETRY_DELAY = 10
ROUND_WINDOW_MINUTES = _int("ROUND_WINDOW_MINUTES", 0)   # 挑战窗口总时长(分钟);0=不限
ROUND_END_AT = os.environ.get("ROUND_END_AT", "").strip()   # 绝对结束时间(本地时区 HH:MM),优先级高于时长
STOP_SPAWN_BEFORE_END = _int("STOP_SPAWN_BEFORE_END", 120)  # 结束前多少秒停止派新题
QUICK_RETRY_DELAY = _int("QUICK_RETRY_DELAY", 5)            # 秒退后的重试间隔(要"坏了立刻重试",所以很短)
KB_NUDGE_STEPS = _int("KB_NUDGE_STEPS", 12)  # 超过这么多步没提交就提醒 agent 去查知识库(0=关闭)
MAX_LOG_MB = _int("MAX_LOG_MB", 0)         # worker 日志总量上限(MB);0=不清理(审计需要完整轨迹)
PREP_TIMEOUT = _int("PREP_TIMEOUT", 300)   # 工作区准备(下载解压)超时,超时释放槽位
TIMEOUT_ESCALATE = _int("TIMEOUT_ESCALATE", 120)  # 每次重试给 worker 增加的单题限时(秒)
MAX_WORKER_TIMEOUT = _int("MAX_WORKER_TIMEOUT", 900)  # 单题单 worker 限时上限(秒)

_prep_pool = ThreadPoolExecutor(max_workers=4)  # 附件下载/解压并行化,避免阻塞主循环
_prepping: dict[tuple[str, int], tuple] = {}    # (qid, slot) -> (future, entry, slot, total, started)


def _prepping_qids() -> set[str]:
    return {qid for qid, _slot in _prepping}


class Concurrency:
    """自适应并发控制器:只降不升;额度耗尽时整体暂停等待窗口重置。"""

    def __init__(self, start: int, floor: int, cooldown: int):
        self.limit = max(floor, start)
        self.floor = floor
        self.cooldown = cooldown
        self.last_reduce = 0.0
        self.pause_until = 0.0
        self._pause_logged = False

    def reduce(self, reason: str) -> bool:
        now = time.time()
        if now - self.last_reduce < self.cooldown or self.limit <= self.floor:
            return False
        # 高并发时按 20% 退(否则从 24 退到安全值要 20 分钟,一场 30 分钟根本来不及)
        step = max(1, self.limit // 5) if self.limit >= 10 else 1
        self.limit = max(self.floor, self.limit - step)
        self.last_reduce = now
        log(f"检测到模型侧限制({reason}),并发上限降至 {self.limit}(本步 -{step})")
        with board_mod.locked() as b:
            board_mod.add_fact(b, "runner", f"限流({reason}),并发降至 {self.limit}")
        return True

    def pause(self, seconds: int, reason: str) -> None:
        until = time.time() + seconds
        if until <= self.pause_until:
            return
        self.pause_until = until
        self._pause_logged = False
        log(f"模型额度不可用({reason}),暂停派发 {seconds}s 至 {time.strftime('%H:%M:%S', time.localtime(until))}")
        with board_mod.locked() as b:
            board_mod.add_fact(b, "runner", f"额度不可用({reason}),暂停 {seconds}s")

    @property
    def paused(self) -> bool:
        return time.time() < self.pause_until


# key = "<qid>#<slot>";value = {"proc","qid","slot","started","attempts_at_spawn"}
workers: dict[str, dict] = {}
concurrency = Concurrency(START_WORKERS, MIN_WORKERS, REDUCE_COOLDOWN)
idle_logged = False
RUN_STARTED = time.time()
DEADLINE_FILE = RUN_DIR / "logs" / "run_deadline.json"


def _resolve_deadline() -> float:
    """确定本轮截止时间(epoch);0 表示不限时。

    优先级:ROUND_END_AT(绝对时间,如 14:00)> ROUND_WINDOW_MINUTES(从启动算起的时长)。
    **截止时间会持久化到数据目录**:runner 崩溃/被重启(supervise、控制台「重启」)后沿用同一个
    截止点,不会因为重启而重新计时(否则重启一次就多跑一轮的时间)。
    """
    if DEADLINE_FILE.exists():
        try:
            saved = json.loads(DEADLINE_FILE.read_text(encoding="utf-8"))
            dl = float(saved.get("deadline") or 0)
            # 只在"同一轮尚未结束"时沿用:重启(supervise/控制台)不能重置计时;
            # 但已过期的 deadline 说明上一轮早已结束 → 视为新一轮,重新计时(否则第二天起来会不干活)
            if dl > time.time():
                return dl
        except Exception:
            pass
    dl = 0.0
    if ROUND_END_AT:
        try:
            hh, mm = (int(x) for x in ROUND_END_AT.split(":")[:2])
            now = datetime.datetime.now()
            end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if end <= now:                       # 已过点则视为明天该时刻
                end += datetime.timedelta(days=1)
            dl = end.timestamp()
        except Exception as e:
            log(f"ROUND_END_AT 解析失败({ROUND_END_AT}): {e},回退到 ROUND_WINDOW_MINUTES")
    if not dl and ROUND_WINDOW_MINUTES:
        dl = RUN_STARTED + ROUND_WINDOW_MINUTES * 60
    if dl:
        try:
            DEADLINE_FILE.parent.mkdir(parents=True, exist_ok=True)
            DEADLINE_FILE.write_text(json.dumps({"deadline": dl, "set_at": RUN_STARTED}),
                                     encoding="utf-8")
        except OSError:
            pass
    return dl


RUN_DEADLINE = _resolve_deadline()


def time_left() -> int:
    """本场剩余秒数;未设窗口返回 -1。"""
    return int(RUN_DEADLINE - time.time()) if RUN_DEADLINE else -1


def window_closing() -> bool:
    """是否已到"不再派新题"的时间点。"""
    return bool(RUN_DEADLINE) and time_left() <= STOP_SPAWN_BEFORE_END


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def env_config() -> dict:
    if not os.environ.get("TEAM_TOKEN"):
        sys.exit("缺少 TEAM_TOKEN,请先 source .env")
    return {
        "TEAM_TOKEN": os.environ["TEAM_TOKEN"],
        "PI_PROVIDER": os.environ.get("PI_PROVIDER", "kimi"),
        "PI_MODEL_NAME": os.environ.get("PI_MODEL", "kimi-for-coding"),
        # 可选:模型 key 对应的环境变量名(若 key 已写在 pi 的 models.json 里则留空)
        "PI_API_KEY_VAR": os.environ.get("PI_API_KEY_VAR", ""),
        "PI_API_KEY": os.environ.get("PI_API_KEY", ""),
        # 断网模式与白名单必须传进 worker,否则 solver 里的判断永远为假(曾经漏掉 → 全员继承死代理)
        "NETWORK_MODE": os.environ.get("NETWORK_MODE", ""),
        "NET_ALLOW": os.environ.get("NET_ALLOW", ""),
        # 直连/走代理同样要传进去(默认直连)
        "NET_PROXY": os.environ.get("NET_PROXY", "direct"),
    }


WORKERS_PID_FILE = RUN_DIR / "logs" / "workers.pid"
WORKERS_JSON = RUN_DIR / "logs" / "workers.json"


def _write_workers_json() -> None:
    """给看板(dashboard.py)用的 worker 注册表:谁在跑哪道题、日志在哪。"""
    data = [
        {"pid": w["proc"].pid, "qid": w["qid"], "slot": w["slot"],
         "started": w["started"], "log": str(w["proc"]._agent_log_path)}
        for w in workers.values()
    ]
    tmp = WORKERS_JSON.with_suffix(".tmp")
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, WORKERS_JSON)


def register_pid(pid: int) -> None:
    WORKERS_PID_FILE.parent.mkdir(exist_ok=True)
    with open(WORKERS_PID_FILE, "a", encoding="utf-8") as f:
        f.write(f"{pid}\n")
    _write_workers_json()


def kill_orphan_workers() -> None:
    """启动时清理上一次运行遗留的 worker:它们不在本进程的 workers 表里,会偷偷烧模型额度。"""
    pids: list[int] = []
    if WORKERS_JSON.exists():
        try:
            pids += [int(w["pid"]) for w in json.loads(WORKERS_JSON.read_text())]
        except Exception:
            pass
    if WORKERS_PID_FILE.exists():
        pids += [int(x) for x in WORKERS_PID_FILE.read_text().split() if x.strip().isdigit()]
    if not pids:
        return
    killed = 0
    for pid in pids:
        try:
            comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            comm = ""
        if comm.split("/")[-1] != "pi":  # 只杀确实是 pi 的进程,避免 pid 被复用后误杀
            continue
        try:
            os.killpg(pid, signal.SIGKILL)
            killed += 1
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except Exception:
                pass
    WORKERS_PID_FILE.write_text("")
    _write_workers_json()
    if pids:
        log(f"清理遗留 worker: {killed}/{len(pids)} 个")


def read_board_questions() -> dict:
    with board_mod.locked() as b:
        return {qid: dict(e) for qid, e in b["questions"].items()}


def workers_of(qid: str) -> list[str]:
    return [k for k, w in workers.items() if w["qid"] == qid]


def kill_worker(key: str, reason: str) -> None:
    w = workers.pop(key, None)
    if w is None:
        return
    proc = w["proc"]
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    finally:
        try:
            proc._agent_log_f.close()
        except Exception:
            pass
    _write_workers_json()
    log(f"worker 结束 {key} ({reason})")


def kill_question(qid: str, reason: str) -> None:
    for key in workers_of(qid):
        kill_worker(key, reason)


def reconcile(env: dict) -> list[dict]:
    """拉题并同步黑板;平台确认已解的题,杀掉其所有 worker 并停止重试。"""
    questions = contest_api.list_questions(env["TEAM_TOKEN"])
    with board_mod.locked() as b:
        for q in questions:
            entry = board_mod.upsert_question(b, q)
            if entry["is_solved"] and entry["status"] != board_mod.STATUS_SOLVED:
                entry["status"] = board_mod.STATUS_SOLVED
                entry["finished_at"] = time.time()
                entry["is_solved"] = True
                board_mod.add_fact(b, "runner", f"{q['title']} 平台确认已解出")
    if not board_mod.IGNORE_SOLVED:
        for q in questions:
            if q.get("is_solved"):
                kill_question(q["question_id"], "solved")

    # 靶场模式:attempt 一旦结束(解出/失败/超时)就不再出现在 current/ 里。
    # 黑板里还挂着 running/pending 的旧题必须收尾,否则 worker 会对着已失效的 attempt 空转。
    if os.environ.get("CONTEST_ADAPTER", "").lower() == "arena":
        alive = {q["question_id"] for q in questions}
        stale = []
        with board_mod.locked() as b:
            for qid, e in b["questions"].items():
                if qid not in alive and e["status"] in (board_mod.STATUS_RUNNING, board_mod.STATUS_PENDING):
                    e["status"] = board_mod.STATUS_FAILED
                    e["finished_at"] = time.time()
                    board_mod.add_fact(b, "runner", f"{e.get('title', qid)} attempt 已结束(未提交),标记失败")
                    stale.append(qid)
        for qid in stale:
            kill_question(qid, "attempt 已结束")
            log(f"attempt 已结束,收尾旧题:{qid}")
    return questions


OBSERVER_HEADER = "\n## Observer 自动记录(队友动作,仅供参考,勿删除本行)\n"


def observe_shared() -> None:
    """Observer:把各 worker 最近动作同步进共享黑板,让同题 agent 互相知道进展。

    agent 自己懒于写黑板时,这是兜底的信息同步手段(对应 Cairn 的 Observer 角色)。
    """
    seen: dict[str, str] = getattr(observe_shared, "_seen", {})
    observe_shared._seen = seen  # type: ignore[attr-defined]
    per_question: dict[str, list[str]] = {}
    for key, w in list(workers.items()):
        try:
            summary = solver.recent_activity(w["proc"]._agent_log_path, n=3)
            tool_calls = solver.count_tool_calls(w["proc"]._agent_log_path)
        except Exception:
            continue
        # 走了很多步还没提交 → 往黑板写一条"去查知识库"的提醒(绕过模型自我判断,每 12 步提醒一次)
        tier = tool_calls // KB_NUDGE_STEPS
        if tier >= 1 and seen.get(key + "#nudge") != tier:
            seen[key + "#nudge"] = tier
            cat = ""
            with board_mod.locked() as b:
                cat = (b["questions"].get(w["qid"]) or {}).get("category", "")
            per_question.setdefault(w["qid"], []).append(
                f"- [Observer {time.strftime('%H:%M')}] ⚠ w{w['slot']} 已执行 {tool_calls} 步仍未提交 —— "
                f"现在按自救流程查知识库:`python3 {ROOT/'tools'/'kb.py'} search \"{cat} 常见套路\"`,"
                f"或 `show {cat or 'web'}` 通读该方向打法,然后换一个没试过的攻击面")
        if not summary or seen.get(key) == summary:
            continue
        seen[key] = summary
        per_question.setdefault(w["qid"], []).append(f"- [Observer {time.strftime('%H:%M')}] w{w['slot']}: {summary}")
    for qid, notes in per_question.items():
        with board_mod.locked() as b:
            entry = b["questions"].get(qid)
        if not entry or entry["status"] == board_mod.STATUS_SOLVED:
            continue
        try:
            shared = solver.project_dir(entry) / "SHARED.md"
            if not shared.exists():
                continue
            text = shared.read_text(encoding="utf-8")
            if "Observer 自动记录" not in text:
                text += OBSERVER_HEADER
            text += "\n".join(notes) + "\n"
            shared.write_text(text, encoding="utf-8")
        except Exception:
            continue


def reset_solved_for_practice() -> None:
    """练习模式(IGNORE_SOLVED=1)下,把历史遗留的 solved 题目重置回 pending 以便重跑。"""
    if not board_mod.IGNORE_SOLVED:
        return
    with board_mod.locked() as b:
        n = 0
        for entry in b["questions"].values():
            if entry["status"] == board_mod.STATUS_SOLVED or entry.get("is_solved"):
                entry.update({"status": board_mod.STATUS_PENDING, "attempts": 0,
                              "is_solved": False, "next_retry_at": 0, "worker_pid": None})
                n += 1
        if n:
            board_mod.add_fact(b, "runner", f"练习模式:重置 {n} 道已解出题目")
    if n:
        log(f"练习模式:重置 {n} 道已解出题目,准备重跑")


def reset_running_to_pending() -> None:
    """退出/中断时把仍标记为 running 的题目重置为 pending,避免看板显示僵尸"运行中"。"""
    with board_mod.locked() as b:
        for entry in b["questions"].values():
            if entry["status"] == board_mod.STATUS_RUNNING:
                entry["status"] = board_mod.STATUS_PENDING
                entry["worker_pid"] = None


def kill_submitted_workers() -> None:
    """agent 通过提交工具标记为 solved 的题,立刻停掉该题所有 worker。"""
    with board_mod.locked() as b:
        solved_qids = [qid for qid, e in b["questions"].items()
                       if e["status"] == board_mod.STATUS_SOLVED]
    for qid in solved_qids:
        if workers_of(qid):
            kill_question(qid, "submitted")


def inspect_failure(w: dict) -> str:
    """判断 worker 退出的原因:timeout / rate_limit / quota / exit=N。"""
    proc = w["proc"]
    if proc.poll() is None and time.time() - w["started"] > w.get("timeout", WORKER_TIMEOUT):
        return "timeout"
    return solver.classify_failure(proc._agent_log_path) or f"exit={proc.poll()}"


def reap() -> None:
    """回收已退出的 worker。模型侧限制导致的退出不消耗重试次数;额度耗尽整体暂停。"""
    for key, w in list(workers.items()):
        proc = w["proc"]
        due_timeout = proc.poll() is None and time.time() - w["started"] > w.get("timeout", WORKER_TIMEOUT)
        if proc.poll() is None and not due_timeout:
            continue
        reason = inspect_failure(w)
        kill_worker(key, reason)

        # 模型侧限制先处理:降并发 / 暂停。必须在题目状态记账之前,避免被 continue 跳过。
        if reason == "quota":
            concurrency.pause(QUOTA_BACKOFF, "额度耗尽")
        elif reason == "rate_limit":
            concurrency.reduce("worker 日志命中限流特征")

        qid = w["qid"]
        with board_mod.locked() as b:
            entry = b["questions"].get(qid)
            if not entry or entry["status"] == board_mod.STATUS_SOLVED:
                continue
            if workers_of(qid):  # 同题还有别的 agent 在跑,不影响题目状态
                continue
            entry["worker_pid"] = None
            if reason == "net":
                # 模型端点连不上:退避重试(次数不限,但要避免热循环)
                n = entry.get("net_failures", 0) + 1
                entry["net_failures"] = n
                wait = min(300, 20 * n)
                entry["status"] = board_mod.STATUS_PENDING
                entry["next_retry_at"] = time.time() + wait
                board_mod.add_fact(b, "runner",
                                   f"{entry['title']} 模型连接失败(第 {n} 次),{wait}s 后重试")
                if n == 1:
                    log(f"⚠ 模型端点连不上(Connection error)—— 检查网络/代理设置;{wait}s 后重试")
                continue
            if reason in ("rate_limit", "quota"):
                entry["attempts"] = max(0, entry["attempts"] - 1)  # 基础设施问题,不算失败
                entry["status"] = board_mod.STATUS_PENDING
                entry["next_retry_at"] = time.time() + 30
                board_mod.add_fact(b, "runner", f"{entry['title']} 模型侧限制({reason}),退避重试")
                continue
            # 空跑检测:起来了但一次工具都没调就退出 → 多半是环境/模型故障,退避并累计熔断
            try:
                did_work = solver.count_tool_calls(w["proc"]._agent_log_path) > 0
            except Exception:
                did_work = True
            if not did_work:
                zw = entry.get("zero_work", 0) + 1
                entry["zero_work"] = zw
                wait = min(300, 20 * zw)
                entry["status"] = board_mod.STATUS_PENDING
                entry["next_retry_at"] = time.time() + wait
                if zw >= 3:
                    board_mod.add_fact(b, "runner",
                                       f"⚠ {entry['title']} 连续 {zw} 次空跑(0 工具调用),疑似模型/环境故障,退避 {wait}s")
                    log(f"⚠ {entry['title']} 连续 {zw} 次空跑(worker 没执行任何命令),退避 {wait}s —— 请检查模型连通性")
                continue
            if entry.get("zero_work"):
                entry["zero_work"] = 0
            entry["net_failures"] = 0
            runtime = time.time() - w["started"]
            unlimited = entry.get("max_attempts", 0) <= 0 or MAX_ATTEMPTS <= 0
            if unlimited or entry["attempts"] < entry["max_attempts"]:
                entry["status"] = board_mod.STATUS_PENDING
                entry["next_retry_at"] = time.time() + (QUICK_RETRY_DELAY if runtime < 30 else 0)
                board_mod.add_fact(b, "runner", f"{entry['title']} {reason},待重试(窗口内不限次数)")
            else:
                entry["status"] = board_mod.STATUS_FAILED
                entry["finished_at"] = time.time()
                board_mod.add_fact(b, "runner", f"{entry['title']} {reason},尝试用尽")


def revive_failed() -> None:
    """尝试用尽的题目,过一段时间重新拾起(比赛窗口内持续努力)。"""
    if FAILED_RETRY_AFTER <= 0:
        return
    now = time.time()
    with board_mod.locked() as b:
        for entry in b["questions"].values():
            if (entry["status"] == board_mod.STATUS_FAILED
                    and now - (entry.get("finished_at") or now) > FAILED_RETRY_AFTER):
                entry["status"] = board_mod.STATUS_PENDING
                entry["attempts"] = 0
                entry["next_retry_at"] = 0
                board_mod.add_fact(b, "runner", f"{entry['title']} 重新拾起")


def spawn(env: dict) -> None:
    """按当前并发上限派题:先铺满不同题目,再考虑同题加派 agent。"""
    if window_closing():
        if not getattr(spawn, "_closing_logged", False):
            log(f"窗口剩余 {time_left()}s,停止派发新题(已有 worker 继续跑完)")
            spawn._closing_logged = True  # type: ignore[attr-defined]
        return
    if concurrency.paused:
        now = time.time()
        if not concurrency._pause_logged and now % 60 < POLL_INTERVAL:
            left = int(concurrency.pause_until - now)
            log(f"模型额度暂停中,剩余 {left}s,不派发新任务")
            concurrency._pause_logged = True
        return
    free = concurrency.limit - len(workers) - len(_prepping)  # 正在准备中的槽位也算已占用
    if free <= 0:
        return
    now = time.time()
    with board_mod.locked() as b:
        entries = [
            dict(e) for e in b["questions"].values()
            if not e["is_solved"] and e["status"] in (board_mod.STATUS_PENDING, board_mod.STATUS_RUNNING)
            and (e.get("max_attempts", 0) <= 0 or e["attempts"] < e["max_attempts"])
            and now >= e.get("next_retry_at", 0)
        ]
        # 未启动过的题优先,其次尝试次数少的题
        entries.sort(key=lambda e: (e["status"] != board_mod.STATUS_PENDING, e["attempts"]))

        # 两轮分配,保证广度优先:
        #   第一轮 每道题先各给 1 个 agent(题目多时能覆盖最多道题)
        #   第二轮 若还有空位(题目数少于槽位数),再给题目加派 agent 到上限
        def used_slots(qid: str) -> set[int]:
            s = {w["slot"] for w in workers.values() if w["qid"] == qid}
            s |= {slot for (q, slot) in _prepping if q == qid}
            return s

        def cap_for(entry: dict) -> int:
            """同题并发上限:默认 WORKERS_PER_QUESTION;agent 在黑板请求增援后放宽到 MAX_HELPERS_PER_QUESTION。"""
            try:
                reqs = solver.read_helper_requests(entry)
            except Exception:
                reqs = []
            return MAX_HELPERS_PER_QUESTION if len(reqs) > int(entry.get("helpers_granted", 0) or 0) \
                else WORKERS_PER_QUESTION

        plans: list[tuple[dict, int, int]] = []  # (entry, slot, total_slots)
        added: dict[str, int] = {}
        for e in entries:  # 第一轮:每道题先各给 1 个 agent
            if free <= 0:
                break
            qid = e["question_id"]
            if used_slots(qid):
                continue
            plans.append((e, 1, WORKERS_PER_QUESTION))
            added[qid] = 1
            free -= 1
        for e in entries:  # 第二轮:有空位再给题目加派(有增援请求的题可以加到 MAX_HELPERS)
            if free <= 0:
                break
            qid = e["question_id"]
            taken = used_slots(qid) | {s for (x, s, _t) in plans if x["question_id"] == qid}
            cap = cap_for(e)
            for slot in range(1, cap + 1):
                if free <= 0:
                    break
                if slot in taken:
                    continue
                e2 = dict(e)
                if cap > WORKERS_PER_QUESTION:      # 增援 worker:带上请求里的分工
                    try:
                        reqs = solver.read_helper_requests(e)
                        idx = added.get(qid, 0)
                        if idx < len(reqs):
                            e2["squad_task"] = reqs[idx]
                    except Exception:
                        pass
                plans.append((e2, slot, cap))
                added[qid] = added.get(qid, 0) + 1
                free -= 1
        for qid, n in added.items():
            # 先占位(避免重复派发),但尝试次数等真正启动时才计——见 launch_prepared,
            # 否则准备失败/进程崩溃会白白烧掉重试次数,题还没跑就被判死。
            orig = b["questions"][qid]
            orig["status"] = board_mod.STATUS_RUNNING
            orig["started_at"] = now
            orig["worker_pid"] = None
            if not orig.get("workdir"):
                orig["workdir"] = solver.resolve_folder(orig)
        for e, _slot, _total in plans:
            e["workdir"] = b["questions"][e["question_id"]].get("workdir")

    # 附件下载/解压放到线程池:题目多、附件大时不会卡住主循环(reap/查题仍按时进行)
    for e, slot, total in plans:
        qid = e["question_id"]
        fut = _prep_pool.submit(_prepare_workspace, e, slot)
        _prepping[(qid, slot)] = (fut, e, slot, total, time.time())


_kb_hint_cache: dict[str, str] = {}


def kb_hint_for(q: dict) -> str:
    """自动检索知识库并把最相关片段交给 agent。

    只在**重试(第 2 次及以后)**时做:首轮先让它自己干(简单题自己就会),
    卡住时由 harness 主动把资料推到它面前,而不是指望它想起来去查。
    """
    if os.environ.get("KB_AUTO_SEARCH", "1") != "1":
        return ""
    qid = q.get("question_id", "")
    if qid in _kb_hint_cache:
        return _kb_hint_cache[qid]
    kw = " ".join(x for x in (q.get("category", ""), q.get("title", ""),
                              (q.get("description") or "")[:60]) if x)
    hint = ""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "kb.py"), "search", kw, "--limit", "3"],
                           capture_output=True, text=True, timeout=25, cwd=ROOT)
        out = (r.stdout or "").strip()
        if out and "未找到" not in out:
            hint = out[:1400]
    except Exception as e:
        log(f"知识库自动检索失败({type(e).__name__}),跳过注入")
    _kb_hint_cache[qid] = hint
    if hint:
        with board_mod.locked() as b:
            board_mod.add_fact(b, "runner", f"为 {q.get('title', qid)} 自动注入知识库片段 {len(hint)} 字")
        log(f"已为 {q.get('title', qid)} 自动注入知识库片段({len(hint)} 字)")
    return hint


def worker_timeout_for(attempts: int) -> int:
    """单题限时随尝试次数递增:第 1 次 WORKER_TIMEOUT,之后每次 +TIMEOUT_ESCALATE。

    理由:简单题首轮 7 分钟足够;难题被超时打断后,后续轮次应给更多时间,
    否则永远卡在同一处(实测 pwn 四次都在 7 分钟处被砍)。
    """
    t = WORKER_TIMEOUT + max(0, attempts - 1) * TIMEOUT_ESCALATE
    return min(t, MAX_WORKER_TIMEOUT) if MAX_WORKER_TIMEOUT > 0 else t


def _prepare_workspace(e: dict, slot: int):
    solver.prepare_project(e)
    return solver.prepare_worker_dir(e, slot)


def launch_prepared(env: dict) -> None:
    """把已经准备好工作区的题目真正启动起来(由 spawn 提交的准备任务完成后调用)。"""
    for (qid, _s), (fut, e, slot, total, started) in list(_prepping.items()):
        if not fut.done():
            if time.time() - started > PREP_TIMEOUT:  # 下载卡死时释放槽位,别把并发额度占死
                _prepping.pop((qid, _s), None)
                log(f"工作区准备超时({PREP_TIMEOUT}s),释放槽位:{qid} w{slot}")
                with board_mod.locked() as b:
                    entry = b["questions"].get(qid)
                    if entry and entry["status"] != board_mod.STATUS_SOLVED:
                        entry["status"] = board_mod.STATUS_PENDING  # 未启动,不消耗尝试次数
                        entry["next_retry_at"] = time.time() + 60
            continue
        _prepping.pop((qid, _s), None)
        try:
            wdir = fut.result()
        except Exception as ex:
            log(f"工作区准备失败 {qid}: {type(ex).__name__}: {ex}")
            with board_mod.locked() as b:
                entry = b["questions"].get(qid)
                if entry and entry["status"] != board_mod.STATUS_SOLVED:
                    entry["status"] = board_mod.STATUS_PENDING  # 未启动,不消耗尝试次数
                    entry["next_retry_at"] = time.time() + 30
            continue
        # 用黑板里的**实时**计数,别用 spawn 时的旧快照(否则次数与注入判断都不准)
        with board_mod.locked() as b:
            prev_attempts = int((b["questions"].get(qid) or {}).get("attempts", 0) or 0)
        this_attempt = prev_attempts + 1
        tm = worker_timeout_for(this_attempt)
        if prev_attempts >= 1:                 # 之前已经试过没成 → 主动把知识库资料推给它
            e["kb_hint"] = kb_hint_for(e)
        try:
            proc = solver.launch(e, slot, total, wdir, env, tm, attempt=this_attempt)
        except Exception as ex:
            log(f"启动失败 {qid} w{slot}: {ex}")
            with board_mod.locked() as b:
                entry = b["questions"].get(qid)
                if entry and entry["status"] != board_mod.STATUS_SOLVED:
                    entry["status"] = board_mod.STATUS_PENDING  # 启动失败,不消耗尝试次数
                    entry["next_retry_at"] = time.time() + 15
            continue
        with board_mod.locked() as b:
            entry = b["questions"].get(qid)
            if entry:
                entry["attempts"] += 1        # 真正启动才计数;不限次数时不设上限
                entry["worker_pid"] = proc.pid
                if e.get("squad_task"):        # 增援 worker:记账,避免同一请求被反复满足
                    entry["helpers_granted"] = int(entry.get("helpers_granted", 0) or 0) + 1
                    board_mod.add_fact(b, "runner",
                                       f"为 {entry.get('title', qid)} 增派 agent(第 {entry['helpers_granted']} 个):"
                                       f"{e['squad_task'][:80]}")
        workers[f"{qid}#{slot}"] = {
            "proc": proc, "qid": qid, "slot": slot, "started": time.time(),
            "timeout": tm,
        }
        register_pid(proc.pid)
        log(f"启动 worker: {e['title']} [{e['category']}] w{slot} pid={proc.pid} "
            f"(并发 {len(workers)}/{concurrency.limit})")


def prune_logs() -> None:
    """日志总量超阈值时删最旧的(不动在跑 worker 的日志)。

    注意:新版规则要求上交智能体做审计,需要完整轨迹,因此 MAX_LOG_MB 默认为 0(不清理)。
    磁盘紧张时才设成 MB 数,并务必先跑 tools/export_trace.py 把轨迹导出成可读 Markdown。
    """
    if MAX_LOG_MB <= 0:
        return
    files = sorted(LOGS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    live = {w["proc"]._agent_log_path for w in workers.values()}
    removed = 0
    while total > MAX_LOG_MB * 1_000_000 and files:
        f = files.pop(0)
        if f in live:
            continue
        try:
            size = f.stat().st_size
            f.unlink()
            total -= size
            removed += 1
        except OSError:
            break
    if removed:
        log(f"清理旧日志 {removed} 个(保留最近 {total // 1_000_000}MB)")


LOGS = RUN_DIR / "logs"
RUNNER_STATE = LOGS / "runner_state.json"


def write_runner_state() -> None:
    """给看板用的运行态:当前并发上限、是否因额度暂停。"""
    logs_mb = 0
    try:
        logs_mb = sum(f.stat().st_size for f in LOGS.glob("*.jsonl")) // 1_000_000
    except OSError:
        pass
    data = {"limit": concurrency.limit, "paused": concurrency.paused,
            "pause_until": concurrency.pause_until, "running": len(workers),
            "heartbeat": time.time(), "loop": getattr(write_runner_state, "_loop", 0),
            "logs_mb": logs_mb, "deadline": RUN_DEADLINE, "time_left": time_left(),
            "ts": time.time()}
    try:
        tmp = RUNNER_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, RUNNER_STATE)
    except OSError:
        pass


def print_status() -> None:
    with board_mod.locked() as b:
        qs = list(b["questions"].values())
    if not qs:
        log("(暂无题目)")
        return
    solved = sum(1 for q in qs if q["status"] == board_mod.STATUS_SOLVED)
    line = " | ".join(f"{q['title']}:{q['status']}" for q in qs)
    left = time_left()
    tleft = f" | 剩余 {left//60}分{left%60:02d}秒" if left >= 0 else ""
    log(f"进度 {solved}/{len(qs)} | 并发 {len(workers)}/{concurrency.limit}{tleft} | {line}")


RUNNER_PID_FILE = RUN_DIR / "logs" / "runner.pid"


def _graceful_signal(*_) -> None:
    """收到 SIGTERM/SIGINT 时走 KeyboardInterrupt 分支,把 worker 一起收掉再退出。"""
    raise KeyboardInterrupt


def main() -> None:
    global idle_logged
    RUN_DIR.mkdir(parents=True, exist_ok=True)   # 先建数据目录,任何入口都不会因缺目录崩
    LOGS.mkdir(parents=True, exist_ok=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只拉取一次题目并打印,不启动 worker")
    args = ap.parse_args()
    env = env_config()
    # 把 .env 里的模型配置(base URL / key / 模型名 / API 类型)同步成 pi 的 provider
    try:
        import config as config_mod
        config_mod.sync_provider(config_mod.read_env(), quiet=True)
    except Exception as e:
        log(f"模型配置同步跳过: {e}")
    kill_orphan_workers()

    try:
        questions = reconcile(env)
    except contest_api.ContestError as e:
        # 接口通了但业务失败(token 无效 / 比赛未开始或已结束):提示清楚,不退出,继续轮询
        log(f"比赛接口返回业务错误:{e} —— 保持轮询,等待接口恢复(可在控制台点「测试接口」排查)")
        questions = list(read_board_questions().values())
    except Exception as e:
        log(f"启动时查询题目失败:{e} —— 保持轮询重试")
        questions = list(read_board_questions().values())
    reset_solved_for_practice()
    log(f"拉取到 {len(questions)} 道题目")
    print_status()
    if args.once:
        for q in questions:
            print(f"  - {q['title']} [{q.get('category')}] solved={q.get('is_solved')} "
                  f"file={'yes' if q.get('file_url') else 'no'} conn={q.get('connection') or '-'}")
        return

    log(f"启动自动解题: 起始并发={concurrency.limit} 下限={MIN_WORKERS} "
        f"同题最多={WORKERS_PER_QUESTION} 单题限时={WORKER_TIMEOUT}s")
    solver.WORK_DIR.mkdir(exist_ok=True)
    RUNNER_PID_FILE.parent.mkdir(exist_ok=True)
    RUNNER_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    signal.signal(signal.SIGTERM, _graceful_signal)
    try:
        loop = 0
        while True:
            loop += 1
            reap()
            kill_submitted_workers()
            observe_shared()
            revive_failed()
            spawn(env)
            launch_prepared(env)
            write_runner_state._loop = loop  # type: ignore[attr-defined]
            if loop % 60 == 1:  # 每约 3 分钟报告一次日志体积(审计需要完整日志,默认不清理)
                with board_mod.locked() as b:
                    pass
                try:
                    mb = sum(f.stat().st_size for f in LOGS.glob("*.jsonl")) // 1_000_000
                    if mb > 2000:
                        log(f"⚠ 日志已 {mb}MB(审计需要完整轨迹,未自动清理);磁盘紧张时手动归档:python3 tools/export_trace.py")
                except OSError:
                    pass
            write_runner_state()
            print_status()
            if loop == 1 and len(questions) > concurrency.limit * 2:
                log(f"题目较多({len(questions)} 道 / 并发 {concurrency.limit}),"
                    f"建议把 WORKER_TIMEOUT 调低(如 600s),让排队的题也能轮上")
            time.sleep(POLL_INTERVAL)
            try:
                reconcile(env)
                globals()["_api_error_logged"] = False
            except contest_api.ContestError as e:
                if not globals().get("_api_error_logged"):
                    log(f"比赛接口业务错误:{e} —— 继续轮询(每分钟提示一次)")
                    globals()["_api_error_logged"] = True
                time.sleep(max(POLL_INTERVAL, 60) if e.code == 203 else API_RETRY_DELAY)
            except Exception as e:
                log(f"查题接口异常({e}),{API_RETRY_DELAY}s 后重试")
                time.sleep(API_RETRY_DELAY)
            if RUN_DEADLINE and time.time() >= RUN_DEADLINE:
                log(f"挑战窗口结束({ROUND_WINDOW_MINUTES} 分钟),停止所有 worker。")
                for key in list(workers):
                    kill_worker(key, "窗口结束")
                break
            with board_mod.locked() as b:
                qs = list(b["questions"].values())
            all_done = qs and all(
                q["status"] in (board_mod.STATUS_SOLVED, board_mod.STATUS_FAILED) for q in qs)
            if all_done and not workers:
                if not KEEP_RUNNING:
                    log("所有题目已结束,退出。")
                    break
                if not idle_logged:
                    log("当前题目已全部处理完,保持驻留等待新题(KEEP_RUNNING=1)……")
                    idle_logged = True
                # 无题可做时也继续轮询,避免频繁查接口
                time.sleep(POLL_INTERVAL * 2)
            else:
                idle_logged = False
    except KeyboardInterrupt:
        log("收到中断,停止所有 worker……")
    finally:
        for key in list(workers):
            kill_worker(key, "shutdown")
        reset_running_to_pending()
        RUNNER_PID_FILE.unlink(missing_ok=True)
    print_status()


if __name__ == "__main__":
    main()
