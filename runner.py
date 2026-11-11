#!/usr/bin/env python3
"""Dispatcher 主循环(参考 Cairn 的 dispatcher 角色)。

策略:
  1. 先高并发抢跑:START_WORKERS 个 worker 并行,优先铺满不同的题目;
     还有空位时,同一道题可以起多个 agent(WORKERS_PER_QUESTION),通过 SHARED.md 协同。
  2. 命中模型限流/并发上限时**逐步降并发**(每次 -1,冷却后生效,降到 MIN_WORKERS 为止)。
  3. 拿到 flag 立即由 agent 调用提交工具提交;平台确认后杀掉该题所有 worker,继续推其它题。
  4. 全解完也不退出(KEEP_RUNNING=1),持续轮询等待新题。

众测模式(CONTEST_ADAPTER=tsec)额外三件事:
  - 开工前过 VPN 预检(check_vpn),不通就等待、不发任何平台请求;
  - 容器自开自关,平台硬上限 3 道 → rotate() 给每题时间片,用满无进展就换下一题(广度优先);
  - 一题多 flag:第一个 flag 命中不等于通关,要等 correct_flag_count == flag_count。

用法:
  ./run.sh                 # 全自动
  ./run.sh --once          # 干跑:只拉题目看状态
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
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
import textutil  # noqa: E402
import profile as profile_mod  # noqa: E402

PROF = profile_mod.current()          # 当前策略包:决定任务源/容器策略/派发人数(见 profiles/*.json)
_TASK_SOURCE = PROF.task_source       # 非空 = 用本地任务源(正式渗透:targets.json)
if _TASK_SOURCE:
    import importlib
    contest_api = importlib.import_module(_TASK_SOURCE)
else:
    _ADAPTER = (os.environ.get("CONTEST_ADAPTER") or PROF.get("adapter") or "").lower()
    if _ADAPTER == "arena":
        import arena_api as contest_api  # 靶场模式:NSSCTF Agent Arena(接口同名同签名)
    elif _ADAPTER == "tsec":
        import tsec_api as contest_api  # TSec Benchmark(容器自开自关 + 一题多 flag)
    elif _ADAPTER == "newrank":
        import newrank_api as contest_api  # 2026 比武「新榜单」(Web 直接入口,票据换 Cookie)
    else:
        import contest_api  # noqa: E402
import solver  # noqa: E402


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


POLL_INTERVAL = _int("POLL_INTERVAL", 5)
START_WORKERS = _int("START_WORKERS", 4)          # 起始并发(众测不是赛场,额度/带宽都省着用)
MIN_WORKERS = _int("MIN_WORKERS", 1)              # 并发下限(降到这里为止)
# 同题 agent 数是**策略**(不是运行参数):以策略包声明为准(ctf 2+4 增援 / 众测 2+3 / 渗透 1+2)
WORKERS_PER_QUESTION = PROF.dispatch_int("workers_per_question", "WORKERS_PER_QUESTION", 2)
# ---- 专注难题模式(2026-10-11,本地复盘用;不设 env 时零行为变化)----
FOCUS_QIDS = {s.strip() for s in os.environ.get("FOCUS_QIDS", "").replace("，", ",").split(",") if s.strip()}
#   只派发列表内的题(逗号/中文逗号分隔),其余题留在黑板不派发、不计入 goal_done。
FOCUS_WORKERS = _int("FOCUS_WORKERS", 0)
#   >0 时:专注列表内的题每题并发上限提到该值(每题多 agent、不同 ANGLE 分工 —— XBOW 冠军打法:
#   单题塞多个会话分头侦察/指纹/利用;平台顶是 3 个容器名额,名额内多 agent 不受平台限制)。
MAX_HELPERS_PER_QUESTION = PROF.dispatch_int("max_helpers", "MAX_HELPERS_PER_QUESTION", 3)
WORKER_TIMEOUT = _int("WORKER_TIMEOUT", 900)
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 0)            # 单题总尝试次数;0=不限(以窗口时间为界)
REDUCE_COOLDOWN = _int("REDUCE_COOLDOWN", 60)     # 两次降并发之间的冷却(秒)
RECOVER_AFTER = _int("RECOVER_AFTER", 1800)       # 限流平息多久后并发 +1(0=永不回升)
FAILED_RETRY_AFTER = _int("FAILED_RETRY_AFTER", 600)  # 放弃的题多久后重新拾起(0=不重拾)
QUOTA_BACKOFF = _int("QUOTA_BACKOFF", 900)        # 额度耗尽后的暂停时长(秒)
KEEP_RUNNING = os.environ.get("KEEP_RUNNING", "1") == "1"
API_RETRY_DELAY = 10
ROUND_WINDOW_MINUTES = _int("ROUND_WINDOW_MINUTES", 0)   # 挑战窗口总时长(分钟);0=不限
ROUND_END_AT = os.environ.get("ROUND_END_AT", "").strip()   # 绝对结束时间(本地时区 HH:MM),优先级高于时长
STOP_SPAWN_BEFORE_END = _int("STOP_SPAWN_BEFORE_END", 120)  # 结束前多少秒停止派新题
QUICK_RETRY_DELAY = _int("QUICK_RETRY_DELAY", 5)            # 秒退后的重试间隔(要"坏了立刻重试",所以很短)
KB_NUDGE_STEPS = _int("KB_NUDGE_STEPS", 12)  # 超过这么多步没提交就提醒 agent 去查知识库(0=关闭)
# ---- 循环形状(跑分特化,来自策略包)----
STOP_MODE = PROF.loop_mode                  # work=任务驱动(只有四种终止) / window=时间窗口(比赛制)
STOP_CONDITIONS = PROF.stop_conditions()    # 允许的终止原因集合
NET_GONE_STREAK = _int("NET_GONE_STREAK", 3)  # 网络/平台连续失败多少次判定"网络不可用"→ 收工(不是时间)
STOP_FILE_NAME = "run_stop.json"            # 终止原因落盘(复盘不用再扒 runner.out)
MAX_LOG_MB = _int("MAX_LOG_MB", 0)         # worker 日志总量上限(MB);0=不清理(审计需要完整轨迹)
PREP_TIMEOUT = _int("PREP_TIMEOUT", 300)   # 工作区准备(下载解压)超时,超时释放槽位
TIMEOUT_ESCALATE = _int("TIMEOUT_ESCALATE", 180)  # 每次重试给 worker 增加的单题限时(秒)
MAX_WORKER_TIMEOUT = _int("MAX_WORKER_TIMEOUT", 1800)  # 单题完整尝试预算上限(秒)
SESSION_SLICE_SECONDS = _int("SESSION_SLICE_SECONDS", 1200)  # 单个 pi 会话接力时长;0=不切片(沿用完整预算)
SESSION_SLICE_SECONDS = max(0, SESSION_SLICE_SECONDS)
# ---- 仅"容器要自己开关"的平台(TSec 众测)用得上 ----
MAX_ACTIVE_CHALLENGES = _int("MAX_ACTIVE_CHALLENGES", 3)  # 平台硬上限:同时最多 3 道题的容器开着
QUESTION_SLICE_SECONDS = _int("QUESTION_SLICE_SECONDS", 1800)  # 单题占用容器名额的时间片;0=不轮换
MIN_QUESTION_SLICE = _int("MIN_QUESTION_SLICE", 420)      # 自适应时间片的下限(题太多时别短到没意义)
HARD_SLICE_FACTOR = float(os.environ.get("HARD_SLICE_FACTOR", "1.5") or 1.5)  # 难题/多 flag 链式题的时间片倍率
ROTATE_COOLDOWN = _int("ROTATE_COOLDOWN", 60)             # 轮换让位后多久可以再来
OBS_MIN_INTERVAL = _int("OBSERVER_MIN_SECONDS", 120)      # 同一 worker 最快多久往黑板记一条动作
OBS_KEEP = _int("OBSERVER_KEEP_LINES", 15)              # Observer 段只保留最近多少条
# ---- 容器服务级探活(策略包 containers.health):平台说 available ≠ 服务活着 ----
# 实测(第四次跑分 c-02):agent 一次同步 git clone 把 ComfyUI 事件循环阻塞死,平台一直报
# available、harness 于是永远不换容器;只剩最后一题时 rotate() 也不轮换 → 那题被判死刑。
CONTAINER_HEALTH = PROF.containers_health()
# ---- 收尾期自动增援(策略包 dispatch.endgame):只剩最后几题 + 并发位空着 → 给卡住的题加人 ----
ENDGAME = PROF.endgame()
# ---- Observer 催促策略(策略包 observer):steps=旧的步数催促;progress=产出判据 ----
OBSERVER_POLICY = PROF.observer_policy()
OBS_MODE = str(OBSERVER_POLICY.get("mode") or "steps").strip().lower()


def _policy_int(key: str, env_name: str, default: int) -> int:
    """策略包字典里的整数旋钮(**0 是合法值**,表示关闭该判据),其次环境变量,最后默认值。

    别写 `int(d.get(k) or default)`:那样 0 会被当成"没配" —— 想关掉停滞提醒都关不掉。
    """
    v = OBSERVER_POLICY.get(key)
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return default


OBS_STALL_S = _policy_int("stall_s", "OBSERVER_STALL_SECONDS", 900)
OBS_REPEAT_SIG = _policy_int("repeat_sig", "OBSERVER_REPEAT_SIG", 3)
OBS_COOLDOWN_S = _policy_int("cooldown_s", "OBSERVER_COOLDOWN_SECONDS", 900)
OBS_MAX_NUDGE = _policy_int("max_per_question", "OBSERVER_MAX_NUDGES", 3)
HINT_AFTER_ATTEMPTS = _int("HINT_AFTER_ATTEMPTS", 2)      # harness 代取 hint:两个完整尝试都失败后才自动取;0=纯 agent 自主
#   2026-10-27 托管实测(101/104):**与第一名同分 flag,但被 hint 扣了 440 分**(14 道 × 10%)——第一名 0 次提示、27200 毛利。
#   排名差距 100% 来自 hint 扣分,所以把自动代取推迟到"两个完整尝试之后"(配合 1200s 短会话接力 ≈ 2 小时仍未解);
#   首发/单次失败都不扣分;真卡死的题仍会自动拿到提示。设 0 完全交给 agent(get_hint.py),设 1 回到"第一次失败即取"。


def _diffset(name: str, default: str) -> set:
    """逗号分隔的难度集合(小写),如 HINT_EARLY_DIFFS="hard,medium"。"""
    raw = os.environ.get(name, default) or ""
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


HINT_EARLY_DIFFS = _diffset("HINT_EARLY_DIFFS", "")               # 自动注入模式:这些难度首发即取(默认关,见上)
HINT_SKIP_DIFFS = _diffset("HINT_SKIP_DIFFS", "easy")            # 自动注入模式:这些难度永不代取(默认关时无效果)
# ---- 2026-10-25 第四轮教训:平台通道连续失败 5.5 小时,日志只报了 1 次、调度冻结无人察觉 → 三重告警 ----
API_FAIL_RELOG_S = _int("API_FAIL_RELOG_S", 600)     # 平台接口连续失败时,至少每这么久再报一次(默认 10 分钟)
STALL_WARN_S = _int("STALL_WARN_S", 900)             # 调度停滞告警:这么久没有新派工/新解出且并发≤1 → 告警;0=关
STALL_MIN_PENDING = _int("STALL_MIN_PENDING", 10)    # 未解题数低于这个数时不按"停滞"论(收尾期本来就慢)
# ---- 2026-10-26 第六轮教训:网关整体变慢(64token 探针 1.7→10~60s)全程没人量化 →
#      开局记基线、跑中每 GW_PROBE_INTERVAL 复测,超基线 GW_SLOW_FACTOR 倍就醒目告警 ----
GW_PROBE_INTERVAL = _int("GW_PROBE_INTERVAL", 1800)  # 模型端点延迟复测间隔(秒);0=关
GW_SLOW_FACTOR = float(os.environ.get("GW_SLOW_FACTOR", "2.0") or 2.0)   # 超基线几倍算变慢
GW_PROBE_TIMEOUT = _int("GW_PROBE_TIMEOUT", 90)      # 单次探针超时(秒)
VPN_WAIT_LOG_INTERVAL = _int("VPN_WAIT_LOG_INTERVAL", 60)  # VPN 未连通时的提示间隔(秒)
# ---- 会话时长保护(2026-09-28 实测:234 个会话里 38% 是"被 harness 杀掉的短会话")----
ADOPT_WORKERS = os.environ.get("ADOPT_WORKERS", "1") == "1"
#   runner 重启(改参数/supervisor 拉起)时**接管**上一次留下的、仍在跑的 pi 进程,而不是全部杀掉。
#   实测:那一轮 234 个 worker 会话中 88 个(38%)死于重启/停机,中位只跑了 39 秒、2 次工具调用。
#   `./start.sh stop` 语义不变(明确停机仍然全部收掉)。
ROTATE_WAIT_RUNNING = os.environ.get("ROTATE_WAIT_RUNNING", "1") == "1"
#   容器时间片用满时,如果这道题**正有 agent 在跑**,不轮换(等它跑完或超时),避免中途杀掉在干活的 agent。
#   实测:24 个会话死于"容器已释放",且都是跑得最久、投入最深的(中位 15 分钟 / 30 次调用)。
WORKER_MIN_LIFETIME = _int("WORKER_MIN_LIFETIME", 120)
#   agent 起来后的保护期:这段时间内既不被轮换,也优先保住它的容器名额(防"刚起来就被踢")。0=关。

_prep_pool = ThreadPoolExecutor(max_workers=4)  # 附件下载/解压并行化,避免阻塞主循环
_prepping: dict[tuple[str, int], tuple] = {}    # (qid, slot) -> (future, entry, slot, total, started)


def _prepping_qids() -> set[str]:
    return {qid for qid, _slot in _prepping}


class Concurrency:
    """自适应并发控制器:限流时降、安静够久再回升;额度耗尽时整体暂停等待窗口重置。"""

    def __init__(self, start: int, floor: int, cooldown: int):
        self.start = max(floor, start)      # 回升上限(起始并发)
        self.limit = self.start
        self.floor = floor
        self.cooldown = cooldown
        self.last_reduce = 0.0
        self.pause_until = 0.0
        self._pause_logged = False

    def recover(self, quiet_s: float) -> bool:
        """限流平息 RECOVER_AFTER 秒后,并发上限 +1(封顶起始值)。

        旧语义"只降不升"在网关抖动时会永久锁死:2026-10-28 实测三次限流把上限压到 1,
        三题退化成单线程串行,而网关其实只要求"别同时开 6 路"。安静够久就还回去。
        """
        if RECOVER_AFTER <= 0 or quiet_s < RECOVER_AFTER or self.limit >= self.start:
            return False
        self.limit = min(self.start, self.limit + 1)
        log(f"限流已平息 {int(quiet_s // 60)} 分钟,并发上限回升至 {self.limit}/{self.start}")
        with board_mod.locked() as b:
            board_mod.add_fact(b, "runner", f"限流平息,并发回升至 {self.limit}/{self.start}")
        return True

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
    """是否已到"不再派新题"的时间点。

    **只在 window 模式生效**(比赛制:到点收工)。work 模式(众测/正式渗透)以任务为界,
    时间只用于节奏与预算,不作为终止判据 —— 所以这里一律返回 False。
    """
    if STOP_MODE != "window":
        return False
    return bool(RUN_DEADLINE) and time_left() <= STOP_SPAWN_BEFORE_END


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class MissingToken(RuntimeError):
    """缺少平台凭证。单独一个异常类型:main() 要把它落成 token_bad 终止原因(而不是裸退出)。

    实测(托管容器冒烟):裸 sys.exit(1) 会让入口脚本每 5 秒重启一次 runner —— 容器空转刷日志。
    """


def _token_from_env_file() -> str:
    """环境变量没设时回退读 .env —— 与 `config.adapter_name()` / `tools/submit_flag.py` 同一口径。

    托管容器里"`.env` 存在但没导出到进程环境"是踩过的坑(第六次 Docker 测试 4h40m 全废的根因类);
    入口修好了导出,这里再兜一层:导出万一坏了,跑分照常开始而不是 t=0 就 token_bad 收工。
    """
    if os.environ.get("BENCHMARK_TOKEN") or os.environ.get("TEAM_TOKEN"):
        return ""
    try:
        import config as _cfg
        env = _cfg.read_env()
    except Exception:                                           # noqa: BLE001
        return ""
    return (env.get("BENCHMARK_TOKEN") or env.get("TEAM_TOKEN") or "").strip()


def env_config() -> dict:
    # 众测平台用 BENCHMARK_TOKEN,CTF 平台用 TEAM_TOKEN;正式渗透(findings)不需要任何 token
    token = (os.environ.get("BENCHMARK_TOKEN") or os.environ.get("TEAM_TOKEN")
             or _token_from_env_file() or "").strip()
    if not token and PROF.goal != "findings":
        raise MissingToken("缺少 BENCHMARK_TOKEN / TEAM_TOKEN,请先 source .env(或在网页控制台里填)")
    os.environ["TEAM_TOKEN"] = token          # worker 与 submit 工具统一读这个
    return {
        "TEAM_TOKEN": token,
        "PI_PROVIDER": os.environ.get("PI_PROVIDER", "rotom"),
        "PI_MODEL_NAME": os.environ.get("PI_MODEL", ""),
        # 本场截止时间戳:worker 靠它算"还剩几分钟"据此调整节奏。
        # 跑分包(loop.prompt_time_budget=false)默认**不灌**(用户要求:不以时间点为基准催 agent 换面)。
        "RUN_DEADLINE_TS": (str(int(RUN_DEADLINE))
                            if RUN_DEADLINE and PROF.prompt_time_budget else ""),
        # 可选:模型 key 对应的环境变量名(若 key 已写在 pi 的 models.json 里则留空)
        "PI_API_KEY_VAR": os.environ.get("PI_API_KEY_VAR", ""),
        "PI_API_KEY": os.environ.get("PI_API_KEY", ""),
        # 断网模式与白名单必须传进 worker,否则 solver 里的判断永远为假(曾经漏掉 → 全员继承死代理)
        "NETWORK_MODE": os.environ.get("NETWORK_MODE", ""),
        "NET_ALLOW": os.environ.get("NET_ALLOW", ""),
        # 直连/走代理同样要传进去(默认直连)
        "NET_PROXY": os.environ.get("NET_PROXY", "direct"),
        # 跨轮战果目录 → 知识库额外根(kb.py 读 KB_EXTRA;空则不覆盖)
        "KB_EXTRA": os.pathsep.join(PROF.kb_extra_dirs()),
        # 平台假阴性同串重试次数(0=关)
        "FALSE_NEGATIVE_RETRIES": os.environ.get("FALSE_NEGATIVE_RETRIES", "2"),
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


def _is_pi_pid(pid: int) -> bool:
    """这个 pid 现在是不是一个 pi 进程(macOS 上 ps 只给 argv[0],所以只比 comm)。"""
    try:
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return False
    return comm.split("/")[-1] == "pi"


class ForeignWorker:
    """接管上一次 runner 留下、仍在跑的 pi 进程(重启不再打断正在干活的 agent)。

    只实现 runner 需要的最小接口(pid / poll / _agent_log_path / _agent_log_f),
    这样 reap()、Observer、kill_worker 都不用为"接管的 worker"写分支。
    """

    def __init__(self, pid: int, log_path: Path):
        self.pid = pid
        self._agent_log_path = log_path
        self._agent_log_f = None

    def poll(self):
        """活着返回 None,已退出返回 0(与 subprocess.Popen.poll 同义)。"""
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            return 0
        except PermissionError:
            return None


def _reset_idle_attempts(questions: dict, busy_qids: set[str]) -> int:
    """启动时把"没人在跑"的题的尝试计时归零,返回清零的题数。

    停机时间不该算进单题预算:不清的话 `worker_timeout_for` 算出的剩余预算可能 ≤0,
    下一节只活 1 秒就被判 timeout(2026-10-28 实测:重启后 081/092 各被白判一次失败)。
    """
    n = 0
    for qid, e in questions.items():
        if qid in busy_qids:
            continue
        if float(e.get("attempt_started_at") or 0):
            e["attempt_started_at"] = 0
            n += 1
    return n


def handle_leftover_workers(env: dict) -> None:
    """启动时处理上一次运行遗留的 worker。

    默认(ADOPT_WORKERS=1)**接管**仍在跑、日志还在的那些 —— 实测 234 个会话里 88 个死于
    runner 重启(中位只跑 39 秒),把重启变成"不打断"能直接救回这部分算力;
    其余(进程已死/没有日志/关掉接管)按老逻辑清理(只杀 comm=pi 的,避免 pid 复用误杀)。

    明确停机仍走 ./start.sh stop(SIGTERM → runner 收工时会连接管的 worker 一起收掉)。
    """
    rows: list[dict] = []
    if WORKERS_JSON.exists():
        try:
            rows += [w for w in json.loads(WORKERS_JSON.read_text()) if isinstance(w, dict)]
        except Exception:
            pass
    if WORKERS_PID_FILE.exists():
        known = {int(w["pid"]) for w in rows if str(w.get("pid", "")).isdigit()}
        rows += [{"pid": int(x)} for x in WORKERS_PID_FILE.read_text().split()
                 if x.strip().isdigit() and int(x) not in known]
    if not rows:
        return
    adopted = killed = 0
    for w in rows:
        pid = int(w.get("pid") or 0)
        if not pid or not _is_pi_pid(pid):
            continue
        log_path = Path(str(w.get("log") or "")) if w.get("log") else None
        qid = str(w.get("qid") or "")
        if ADOPT_WORKERS and qid and log_path and log_path.exists():
            key = f"{qid}#{int(w.get('slot') or 1)}"
            if key not in workers:
                started = float(w.get("started") or time.time())
                with board_mod.locked() as b:
                    _e = b["questions"].get(qid) or {}
                    if not float(_e.get("attempt_started_at") or 0):
                        _e["attempt_started_at"] = started      # 接力计时别断
                    tm = worker_timeout_for(max(1, int(_e.get("attempts") or 1)), dict(_e))
                    hoff = session_handoff_for(max(1, int(_e.get("attempts") or 1)), dict(_e), tm)
                    if _e and _e["status"] == board_mod.STATUS_PENDING:
                        _e["status"] = board_mod.STATUS_RUNNING   # 它在跑,黑板别显示"待派发"
                workers[key] = {"proc": ForeignWorker(pid, log_path), "qid": qid,
                                "slot": int(w.get("slot") or 1), "started": started,
                                "timeout": tm, "adopted": True, "handoff": hoff}
                adopted += 1
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
    # 停机期间不算进单题预算:没被接管的题把"完整尝试起点"清掉。
    # 不清的话重启后 left = 预算 − (已用 + 停机时长) 可能 ≤ 0 → worker 只活 1 秒就被判 timeout
    # (2026-10-28 实测:重启后 081/092 各被这一下白判一次失败)。
    reset = 0
    with board_mod.locked() as b:
        reset = _reset_idle_attempts(b["questions"], {w["qid"] for w in workers.values()})
    if reset:
        log(f"启动清点:{reset} 道题的尝试计时归零(停机时间不计入单题预算)")
    if adopted or killed:
        log(f"启动清点:接管 {adopted} 个仍在跑的 worker(ADOPT_WORKERS={int(ADOPT_WORKERS)}),清理 {killed} 个遗留进程")


def read_board_questions() -> dict:
    with board_mod.locked() as b:
        return {qid: dict(e) for qid, e in b["questions"].items()}


def workers_of(qid: str) -> list[str]:
    return [k for k, w in workers.items() if w["qid"] == qid]


def _oldest_worker_age(qid: str, now: float) -> float:
    """这道题在跑的 worker 里最久的那个已经跑了多少秒;没有在跑的返回 -1。"""
    ages = [now - float(w.get("started") or now) for w in workers.values() if w["qid"] == qid]
    return max(ages) if ages else -1.0


def _protected(qid: str, now: float) -> bool:
    """这道题现在是不是"受保护"的:有 agent 在跑,且(Rotate 等待在跑的 / 在保护期内)。

    受保护的题:rotate() 不让位;容器调度时优先保住名额(hold_container)。
    作用(实测依据):24 个会话死于"容器已释放",都是投入最深的;另有 32% 的会话不足 1 分钟。
    """
    if not workers_of(qid):
        return False
    if ROTATE_WAIT_RUNNING:
        return True
    age = _oldest_worker_age(qid, now)
    return WORKER_MIN_LIFETIME > 0 and 0 <= age < WORKER_MIN_LIFETIME


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
    # 回收也是调度活动(2026-10-28 实测误报):并发控制/会话接力会在同一拍把 worker 全部回收再重开,
    # 那一刻 len(workers)==0;若锚点只在"新派工/新解出"刷,就会出现"并发 0 + 停 20 分钟"的假停滞告警。
    globals()["_last_activity_ts"] = time.time()


def kill_question(qid: str, reason: str) -> None:
    for key in workers_of(qid):
        kill_worker(key, reason)


def reconcile(env: dict) -> list[dict]:
    """拉题并同步黑板;平台确认已解的题,杀掉其所有 worker 并停止重试。"""
    questions = contest_api.list_questions(env["TEAM_TOKEN"])
    with board_mod.locked() as b:
        for q in questions:
            before = int((b["questions"].get(q["question_id"]) or {}).get("correct_flag_count") or 0)
            entry = board_mod.upsert_question(b, q)
            after = int(entry.get("correct_flag_count") or 0)
            if after > before:          # 一题多 flag:平台侧进度涨了(agent 刚提交成功)
                board_mod.add_fact(b, "runner",
                                   f"{entry['title']} flag 进度 {after}/{entry.get('flag_count')}")
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


OBSERVER_HEADER = "\n## Observer 自动记录(系统同步的队友动作,只看最近几条)\n"
HARNESS_PREFIX = ">> [harness]"          # harness 自己维护的行;每次重写时先全部剔除再重建
TARGET_ANCHOR = "## 已确认的事实"         # 目标地址块插在这一行之前

_obs_lines: dict[str, list[str]] = {}      # qid -> 最近的 Observer 行(有上限)
_obs_seen: dict[str, tuple] = {}           # worker key -> (上次摘要, 上次写入时刻)
_obs_nudge: dict[str, int] = {}            # worker key -> 已提醒过的步数档位
_target_sig: dict[str, str] = {}           # qid -> 目标地址块的内容签名(变了才重写黑板)
_transcode_warned: set[str] = set()        # qid -> 已提醒过"黑板混进非 UTF-8"


def _conn_values(conn: dict | None) -> list[str]:
    return [str(v) for v in (conn or {}).values() if str(v).strip()]


def _mark_dead(entry: dict, reason: str = "") -> None:
    """把当前连接地址记进"已失效"列表并清空 connection(容器没了/换地址时调用)。

    为什么必须做:容器重新拉起后 IP/URL 会变,而黑板里的旧地址会一直留着,
    agent 会反复去戳死地址(实测同一道题的黑板里攒了 5 个地址、一堆无效探测)。
    清掉 + 记进死名单,黑板顶部的 harness 块会把"当前地址 / 已失效地址"讲清楚。
    """
    dead = list(entry.get("dead_addrs") or [])
    for v in _conn_values(entry.get("connection")):
        if v not in dead:
            dead.append(v)
    entry["dead_addrs"] = dead[-20:]
    if entry.get("connection"):
        entry["addr_changed_at"] = time.time()
        entry["addr_note"] = reason
    entry["connection"] = {}


def _accept_conn(entry: dict, conn: dict | None) -> bool:
    """采用新的连接地址;与旧值不同时把旧值记进失效列表。返回是否发生了"地址变更"。"""
    new = _conn_values(conn)
    old = _conn_values(entry.get("connection"))
    if not new:
        return False
    if old and set(old) == set(new):
        return False
    if old:
        dead = list(entry.get("dead_addrs") or [])
        for v in old:
            if v not in dead:
                dead.append(v)
        entry["dead_addrs"] = dead[-20:]
        entry["addr_changed_at"] = time.time()
    entry["connection"] = conn
    return bool(old)                     # 首次拿到地址不算"变更"(不必写"旧地址已失效")


def _target_lines(entry: dict) -> list[str]:
    """给黑板写"当前目标地址"块:当前地址 + 已失效地址(agent 一眼就知道别打哪些)。"""
    conn = entry.get("connection") or {}
    lines: list[str] = []
    if conn:
        lines.append("当前目标地址:" + " · ".join(f"{k}={v}" for k, v in conn.items()))
    else:
        lines.append("当前目标地址:**暂未就绪**(容器没拉起/正在换容器;先读黑板其它结论,不要猜地址)")
    st = str(entry.get("container_status") or "").strip()
    if st:
        lines.append(f"容器状态:{st}")
    dead = [v for v in (entry.get("dead_addrs") or []) if v not in _conn_values(conn)]
    if dead:
        lines.append("已失效地址(打这些是浪费,别再试):" + " · ".join(dead[-6:]))
    wrong = list((entry.get("wrong_flags") or [])[-5:])
    if wrong:
        lines.append("已判错的 flag(平台判 WRONG 的**诱饵值**;submit 工具会拒绝重投同串):" + " · ".join(wrong))
    return lines


def _rebuild_shared(shared: Path, obs_lines: list[str],
                    target_lines: list[str] | None = None) -> tuple[bool, bool]:
    """重写 SHARED.md:保留 agent 自己写的行,harness 维护的两块(目标地址 / Observer)整体重建。

    agent 用 `>>` 追加,它写的行可能落在 Observer 段**之后** —— 不能简单按段截断,
    否则会把 agent 刚写的结论删掉。这里改成:全文过滤掉 harness 行、其余原样保留,
    再把目标地址块插到"## 已确认的事实"之前、Observer 段放到最后重建。

    编码:黑板里可能混进非 UTF-8 字节(agent 把 GBK 工具输出直接 >> 进去了,实测 0xbc)。
    这里用 textutil 逐行转码(UTF-8 → GB18030 → 兜底),**保住内容**并把文件统一成 UTF-8;
    返回 (是否发生过编码转码, 是否写入成功):转码用来提醒 agent,写入失败用来决定要不要重试。
    """
    text, transcoded = textutil.read_mixed(shared)
    body = [ln for ln in text.replace(OBSERVER_HEADER, "").splitlines()
            if not ln.lstrip().startswith("- [Observer ") and not ln.lstrip().startswith(HARNESS_PREFIX)]
    while body and not body[-1].strip():
        body.pop()
    if target_lines:
        block = [f"{HARNESS_PREFIX} 当前目标地址(harness 维护,**以此为准**;黑板上其它地址可能是旧容器的残留):"]
        block += [f"{HARNESS_PREFIX} {ln}" for ln in target_lines] + [""]   # 末尾留空行,读起来清爽
        idx = next((i for i, ln in enumerate(body) if ln.strip() == TARGET_ANCHOR), None)
        body = body + block if idx is None else body[:idx] + block + body[idx:]
    out = "\n".join(body) + "\n" + OBSERVER_HEADER + "\n".join(obs_lines) + "\n"
    try:
        shared.write_text(out, encoding="utf-8")
    except OSError:
        # 工作区还没建好(题还没派过)时会走到这里 —— 必须如实告诉调用方,
        # 否则"写失败"会被当成"已写入"而记下签名,后面地址变了也不再补写(实测踩过)
        return transcoded, False
    return transcoded, True

_q_progress: dict[str, dict] = {}   # qid -> {"flags","at","sig","nudges","last"};产出判据用
_obs_repeat: dict[str, int] = {}    # "qid#slot" -> 已提醒过的重复次数(边沿触发)


def _agent_lines_sig(shared: Path) -> str:
    """黑板里 **agent 自己写的** 内容签名(滤掉 Observer 与 harness 行)。

    这是"有没有产出"的证据源之一:agent 每写一条结论/排除项,签名就变 —— 比"工具调用次数"
    更接近"实质进展"(实测中位 2 分钟的题里,大量步骤是读源码/写脚本,不该算停滞)。
    """
    try:
        text, _ = textutil.read_mixed(shared)
    except Exception:
        return ""
    body = [ln.strip() for ln in text.splitlines()
            if ln.strip()
            and not ln.lstrip().startswith("- [Observer ")
            and not ln.lstrip().startswith(HARNESS_PREFIX)]
    return "|".join(body[-8:])[:400]


def _nudge_by_progress(qid: str, w: dict, changed: set[str]) -> bool:
    """产出判据的催促(策略包 `observer.mode=progress`,众测/正式渗透用)。

    为什么不按步数催(实测 + 同行数据):
      · CyberStrike 对"连续 N 步无产出"做过对照(XBEN-025),连编排者一起误催 ~70%,已弃用;
      · 长链路里"合法无产出"很常见(读源码、写脚本、等一条 5 分钟的命令),按步数催只会制造噪音;
      · 第四次跑分的现场就是:同一题黑板连刷 7 条一模一样的"已 N 步未提交"。
    这里换成两个真信号:
      ① **重复命令**:最近若干次工具调用里"完全相同的 (工具, 参数)"≥ repeat_sig 次 —— 字节级比较,
         `id=1/2/3` 这类正常探测不会误伤;
      ② **长期无进展**:距上次实质进展(新 flag / 黑板出现新的 agent 结论)超过 stall_s 秒。
    两条都是**边沿触发**(同一信号只提醒一次)+ 冷却 + 每题上限;容器没就绪时一律不催
    (那是环境问题,该由 harness 换容器,而不是催 agent 换思路)。
    """
    now = time.time()
    with board_mod.locked() as b:
        e = dict(b["questions"].get(qid) or {})
    if not e or not e.get("container_ready"):
        return False
    try:
        shared = solver.project_dir(e) / "SHARED.md"
    except Exception:
        return False
    flags = int(e.get("correct_flag_count") or 0)
    sig = _agent_lines_sig(shared)
    marks = _q_progress.setdefault(qid, {"flags": flags, "at": now, "sig": sig,
                                         "nudges": 0, "last": 0.0})
    if flags > int(marks.get("flags") or 0) or sig != marks.get("sig"):
        marks.update({"flags": flags, "sig": sig, "at": now, "last": 0.0})
    if int(marks.get("nudges") or 0) >= OBS_MAX_NUDGE:
        return False
    if now - float(marks.get("last") or 0) < OBS_COOLDOWN_S:
        return False
    try:
        repeats, sample = solver.tool_signatures(w["proc"]._agent_log_path, k=40)
    except Exception:
        repeats, sample = 0, ""
    key = f"{qid}#{w.get('slot')}"
    if OBS_REPEAT_SIG > 0 and repeats >= OBS_REPEAT_SIG and int(_obs_repeat.get(key) or 0) < repeats:
        _obs_repeat[key] = repeats
        _obs_lines.setdefault(qid, []).append(
            f"- [Observer {time.strftime('%H:%M')}] ⚠ w{w['slot']} 最近把**完全相同的命令**跑了 "
            f"{repeats} 次(`{sample[:80]}`)—— 同样的输入不会有新结果:换参数/换端点/换攻击面,"
            f"或把它写进黑板当「已证死」")
        marks.update({"nudges": int(marks.get("nudges") or 0) + 1, "last": now})
        changed.add(qid)
        return True
    stalled = now - float(marks.get("at") or now)
    if OBS_STALL_S > 0 and stalled >= OBS_STALL_S:
        cat = e.get("category", "")
        _obs_lines.setdefault(qid, []).append(
            f"- [Observer {time.strftime('%H:%M')}] ⚠ {qid} 已 {int(stalled // 60)} 分钟没有新进展"
            f"(没新 flag、黑板也没有新结论)—— 先做**系统性证伪**(把候选路径一次列全、批量验证),"
            f"证伪完就换面;想对套路:`python3 {ROOT / 'tools' / 'kb.py'} search \"{cat} 常见套路\"`")
        marks.update({"nudges": int(marks.get("nudges") or 0) + 1, "last": now, "at": now})
        changed.add(qid)
        return True
    return False


def endgame_targets(entries: list[dict], free: int, now: float,
                    unsolved: int | None = None) -> set[str]:
    """收尾期该给哪些题自动加派补位 agent(策略包 `dispatch.endgame`)。

    三条都满足才给某道题加人:
      ① 剩下的未解题 ≤ `unsolved_lte`(广度已经铺完,再铺也没有新题可开);
      ② 还有空闲并发位(否则加派等于抢别的题的名额);
      ③ 这道题已经在跑,且距"最后一次实质进展"(新 flag / 黑板新结论)超过 `after_running_s`。

    实测第四次跑分:全场只剩最后一题时 5 个并发位空着 4 个,而它只有一个 agent 在磨,
    增援是收工前 14 分钟才由操作者手动加上去的 —— 这段就是给那种局面兜底的。

    ⚠ `unsolved` 必须是**全局未解题数**(任务里还剩几题没解),不能拿 `entries` 的长度糊弄:
    `entries` 只是"此刻有容器名额、能派的题",众测下被名额数(3)卡死 → 恒 ≤ 3 ≤ unsolved_lte(2),
    于是收尾期从**开跑第一分钟**就误触发,把空闲并发位全灌到链式题上(2026-10-08 第七次实测:
    63 题里 60 题还没碰,日志却写"只剩 2 道未解",b-02 一口气被加了 2 个 agent)。
    调用方不传时退回旧行为(按 entries 长度),只为兼容老测试。
    """
    if not ENDGAME.get("enabled") or free <= 0:
        return set()
    lte = int(ENDGAME.get("unsolved_lte") or 2)
    after = float(ENDGAME.get("after_running_s") or 600)
    if (len(entries) if unsolved is None else int(unsolved)) > lte:
        return set()
    out: set[str] = set()
    for e in entries:
        qid = e["question_id"]
        ws = [w for w in workers.values() if w["qid"] == qid]
        if not ws:
            continue                      # 还没人在跑 → 第一轮本来就会给它一个
        started = min(float(w.get("started") or now) for w in ws)
        last_prog = float((_q_progress.get(qid) or {}).get("at") or 0)
        if now - max(started, last_prog) < after:
            continue                      # 刚接手 / 刚有进展,再给它一点时间
        out.add(qid)
    return out


def observe_shared() -> None:
    """Observer:把各 worker 最近动作同步进共享黑板,让同题 agent 互相知道进展。

    别把它写成"每轮刷一条" —— 实测 63 行黑板里 40 行是 Observer,而且摘要全是被
    `cd <工作目录>` 吃掉的废话(`bash(cd <工作目录>)`),把 agent 真正写下的
    结论彻底淹没。所以这里做了三件事:
      ① 摘要剥掉 `cd <工作目录>` 前缀(见 solver._brief);
      ② 同一 worker 最快 OBS_MIN_INTERVAL 秒记一条,且只在动作**变了**时才记;
      ③ Observer 段每次整体重建、只留最近 OBS_KEEP 条 —— 黑板不会无限膨胀。
    """
    now = time.time()
    changed: set[str] = set()
    for key, w in list(workers.items()):
        qid = w["qid"]
        try:
            summary = solver.recent_activity(w["proc"]._agent_log_path, n=2)
            tool_calls = solver.count_tool_calls(w["proc"]._agent_log_path)
        except Exception:
            continue
        if OBS_MODE == "progress":
            if _nudge_by_progress(qid, w, changed):
                pass
        else:
            # 旧行为(短周期 CTF):走了很多步还没提交 → 催一次"去查知识库"。
            tier = tool_calls // KB_NUDGE_STEPS if KB_NUDGE_STEPS > 0 else 0
            if tier >= 1 and _obs_nudge.get(key, 0) != tier:
                _obs_nudge[key] = tier
                with board_mod.locked() as b:
                    cat = (b["questions"].get(qid) or {}).get("category", "")
                _obs_lines.setdefault(qid, []).append(
                    f"- [Observer {time.strftime('%H:%M')}] ⚠ w{w['slot']} 已 {tool_calls} 步未提交 —— "
                    f"按自救流程查知识库:`python3 {ROOT / 'tools' / 'kb.py'} search \"{cat} 常见套路\"`,"
                    f"换个没试过的攻击面")
                changed.add(qid)
        prev_sig, prev_at = _obs_seen.get(key, ("", 0.0))
        if not summary or summary == prev_sig or (now - prev_at) < OBS_MIN_INTERVAL:
            continue
        _obs_seen[key] = (summary, now)
        _obs_lines.setdefault(qid, []).append(f"- [Observer {time.strftime('%H:%M')}] w{w['slot']}: {summary}")
        changed.add(qid)

    for qid in changed:
        lines = _obs_lines.setdefault(qid, [])
        del lines[:-OBS_KEEP]                     # 只留最近几条
        with board_mod.locked() as b:
            entry = b["questions"].get(qid)
        if not entry or entry["status"] == board_mod.STATUS_SOLVED:
            continue
        try:
            _rebuild_shared(solver.project_dir(entry) / "SHARED.md", lines, _target_lines(entry))
        except Exception as e:                     # Observer 只是锦上添花,永远不许拖垮调度
            log(f"Observer 写黑板失败({type(e).__name__}: {e}),跳过 {qid}")

    sync_target_blocks()          # 地址变了/容器没了 → 黑板顶部的 harness 块必须跟着变


def sync_target_blocks() -> None:
    """把"当前目标地址"块同步进黑板(harness 维护,agent 以此为准)。

    容器重新拉起会换 IP/URL:旧地址留在黑板里,agent 会反复去打(实测同一题的黑板里攒了 5 个地址)。
    这里每轮检查一次"目标块内容签名",只有内容变了(首次拿到地址 / 换地址 / 容器没了)才重写,
    顺手提醒一次"黑板里混进了非 UTF-8"。
    """
    with board_mod.locked() as b:
        items = [(qid, dict(e)) for qid, e in b["questions"].items()
                 if e["status"] != board_mod.STATUS_SOLVED]
    for qid, entry in items:
        lines = _target_lines(entry)
        sig = "|".join(lines)
        if _target_sig.get(qid) == sig:
            continue
        shared = solver.project_dir(entry) / "SHARED.md"
        try:
            transcoded, wrote = _rebuild_shared(shared, _obs_lines.get(qid, []), lines)
        except Exception as e:
            log(f"写目标地址块失败({type(e).__name__}: {e}),跳过 {qid}")
            continue
        if not wrote:
            continue                       # 工作区还没建好:不记签名,下一轮接着试
        _target_sig[qid] = sig             # **写成功之后**才记签名(否则写失败会被当成写过了)
        if transcoded and qid not in _transcode_warned:
            _transcode_warned.add(qid)
            log(f"⚠ {qid} 黑板里混进了非 UTF-8(GBK)内容,已转成 UTF-8(内容保留);提示 agent 改用 iconv/printf")
            with board_mod.locked() as b:
                board_mod.add_fact(b, "runner", f"{entry.get('title', qid)} 黑板混入非 UTF-8,已转码为 UTF-8")
            try:                                   # 顺手在黑板里教一次怎么写
                _obs_lines.setdefault(qid, []).append(
                    "- [Observer " + time.strftime("%H:%M") + "] ⚠ 黑板里混进过非 UTF-8(GBK)内容(已自动转码):"
                    "写黑板请用 UTF-8 —— 别把命令原始输出直接 >> 进来,GBK 输出先 `iconv -f gbk -t utf-8`,"
                    "或改用 `printf '%s\\n' \"...\" >> SHARED.md` / write 工具")
                _rebuild_shared(shared, _obs_lines[qid], lines)
            except Exception:
                pass


def reset_solved_for_practice() -> None:
    """练习模式(IGNORE_SOLVED=1)下,把历史遗留的 solved 题目重置回 pending 以便重跑。"""
    if not board_mod.IGNORE_SOLVED:
        return
    with board_mod.locked() as b:
        n = 0
        for entry in b["questions"].values():
            if entry["status"] == board_mod.STATUS_SOLVED or entry.get("is_solved"):
                entry.update({"status": board_mod.STATUS_PENDING, "attempts": 0,
                              "is_solved": False, "next_retry_at": 0, "worker_pid": None,
                              "attempt_started_at": 0, "session_elapsed": 0})
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
            globals()["_last_rl_ts"] = time.time()   # 供 recover() 判断"限流是否已平息"

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
            if reason == "timeout" and w.get("handoff"):
                # 短会话接力:本次 session 到点但完整预算未耗尽。保持 attempt_started_at 连续计时、
                # 回滚本次 attempts,不进失败/退避/空跑熔断,同题同容器立刻接下一节。
                entry["attempts"] = max(0, int(entry.get("attempts") or 0) - 1)
                entry["session_elapsed"] = int(_elapsed_of(entry))
                entry["status"] = board_mod.STATUS_PENDING
                entry["next_retry_at"] = time.time()
                board_mod.add_fact(b, "runner",
                                   f"{entry['title']} 会话接力(第 {int(w.get('handoff_index') or 1)} 节结束,"
                                   f"累计 {entry['session_elapsed']}s):保留工作区/黑板,不计失败")
                log(f"会话接力:{qid} 累计 {entry['session_elapsed']}s/预算 "
                    f"{full_worker_timeout_for(max(1, entry.get('attempts') or 1), entry)}s,下一节继续")
                continue
            if reason == "timeout":
                # 完整预算耗尽:一次真正失败;清掉接力计时,下一次尝试因此触发 HINT_AFTER_ATTEMPTS。
                entry["session_elapsed"] = 0
                entry["attempt_started_at"] = 0
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
                entry["attempt_started_at"] = 0
                entry["session_elapsed"] = 0
                board_mod.add_fact(b, "runner", f"{entry['title']} 重新拾起")


def vpn_gate(env: dict) -> bool:
    """众测平台的强制前置:靶场 VPN 联通预检。

    题目入口地址只在 VPN 网络内可达,所以预检不过就**什么都不做**(连题目列表都不查),
    连上之后自动继续 —— 现场先起 harness、后连 VPN 也能自己接上。
    """
    if not hasattr(contest_api, "check_vpn"):
        return True
    r = contest_api.check_vpn()
    if r.get("ok"):
        if globals().get("_vpn_bad_logged"):
            log(f"VPN 预检通过(client_ip={r.get('client_ip')}),继续跑")
        globals()["_vpn_bad_logged"] = False
        globals()["_vpn_ok_logged"] = True
        return True
    now = time.time()
    last = float(globals().get("_vpn_warn_at") or 0)
    if now - last >= VPN_WAIT_LOG_INTERVAL:
        globals()["_vpn_warn_at"] = now
        log(f"⚠ VPN 预检未通过({r.get('error')})—— 请连接靶场 VPN(README「VPN」一节),"
            f"连上前不查题、不派题、不启动容器")
        with board_mod.locked() as b:
            board_mod.add_fact(b, "runner", "VPN 未连通,等待中(不发起任何平台请求)")
    globals()["_vpn_bad_logged"] = True
    return False


def effective_slice() -> int:
    """当前该给每题多长的时间片(秒)。

    时间片自适应:剩余窗口 × 容器名额 ÷ 还没做的题数 ≈ 每题能分到多少时间。
    题多窗口短就自动缩短(保证覆盖到更多题),题少就用配置上限(保证深度)。
    没设总时长(ROUND_WINDOW_MINUTES=0)时无法推算,直接用配置值。
    """
    if QUESTION_SLICE_SECONDS <= 0:
        return 0
    left = time_left()
    if left <= 0:
        return QUESTION_SLICE_SECONDS
    with board_mod.locked() as b:
        remain = sum(1 for e in b["questions"].values() if not e["is_solved"])
    if remain <= MAX_ACTIVE_CHALLENGES:
        return QUESTION_SLICE_SECONDS
    per = int(left * MAX_ACTIVE_CHALLENGES / remain)
    return max(MIN_QUESTION_SLICE, min(QUESTION_SLICE_SECONDS, per))


def slice_for(e: dict, base: int) -> int:
    """按题给时间片:难题/多 flag 的链式题给更长时间。

    实测(2026-09-27 第一次测试):easy 题在 11~13 分钟才摸到 RCE/源码泄露,
    15 分钟一刀切会把"快做出来"的题砍掉。而难题每个 flag 的分值约是 easy 的 3 倍
    (376 vs 133),多 flag 链式题(b-01/b-02/b-03)更要连续时间(容器一关,
    上传的 webshell/建好的账号全没)。所以难题给 1.5 倍。
    """
    diff = str(e.get("difficulty") or "").lower()
    heavy = diff in ("hard", "困难") or int(e.get("flag_count") or 1) >= 3
    return int(base * HARD_SLICE_FACTOR) if heavy else base


def rotate(env: dict) -> None:
    """容器名额轮换:平台同时只允许 3 道题的容器开着,所以每题只领一个时间片。

    用满时间片且**没有新 flag 进展** → 让出名额给还没碰过的题(广度优先);
    一旦有新进展就自动续期,不会把快做出来的题赶走。
    只有"容器要自己开关"的平台(适配器带 sync_containers)才需要这套。
    """
    if not hasattr(contest_api, "sync_containers"):
        return
    slice_s = effective_slice()
    if slice_s <= 0:
        return
    now = time.time()
    with board_mod.locked() as b:
        unsolved = [e for e in b["questions"].values() if not e["is_solved"]]
        if FOCUS_QIDS:      # 专注模式:轮换只在列表内的题之间进行(列表外 90 题没碰过也不构成轮换理由)
            unsolved = [e for e in unsolved if e["question_id"] in FOCUS_QIDS]
        if len(unsolved) <= MAX_ACTIVE_CHALLENGES:
            return                      # 题比名额少,没必要轮换
        # 只有"还有没碰过的题在排队"时才轮换,否则继续深耕当前题
        if not [e for e in unsolved
                if int(e.get("attempts") or 0) == 0 and e["status"] == board_mod.STATUS_PENDING
                and now >= float(e.get("rotate_until") or 0)]:
            return
        # 链式题的"不轮换豁免"**有名额上限**(策略包 budgets.chain.max_pinned_slots,默认 2)。
        # 为什么必须在这里也限:原先只在 _mark_holds() 里限了额(管"谁优先拿名额"),rotate() 这边
        # 对**所有**链式题无条件 `continue` —— 于是链式题一旦都拿到容器就永不轮换,
        # 3 道链式题把 3 个名额全钉死,广度题(占 82% 的 flag)一道都排不上(2026-10-08 第七次实测:
        # 开跑 30 分钟 3/3 名额全在 b-01/b-02/b-03,60 道广度题 pending,flag 卡在 2/74)。
        # 口径与 _mark_holds 完全一致:按 关卡低 → flag 多 → 尝试少 取前 max_pinned_slots 道豁免。
        _chain = [e for e in unsolved
                  if PROF.is_pinned(e)
                  # 安全阀看 **stale_slices**(连续几个时间片零新 flag),不看 attempts ——
                  # attempts 会被 harness 自己的动作灌大(重启/增援/容器回收后重派),把正在出分的链式题误放走
                  and int(e.get("stale_slices") or 0) < PROF.chain_max_attempts()]
        _chain.sort(key=lambda e: (int(e.get("level") or 0),
                                   -int(e.get("flag_count") or 1),
                                   int(e.get("attempts") or 0)))
        exempt = {e["question_id"] for e in _chain[:max(0, PROF.chain_max_pinned_slots())]}
        for e in unsolved:
            if e["status"] not in (board_mod.STATUS_PENDING, board_mod.STATUS_RUNNING):
                continue
            qid = e["question_id"]
            # ① 被钉住的链式题:**先结算时间片与安全阀,再谈"保护"**。
            #    顺序不能反 —— 被钉住的题**必然**有在跑的 worker(链式会话 3 小时),
            #    而 ROTATE_WAIT_RUNNING=1 下 `_protected()` 对这种情况恒为真;
            #    原先"先判保护、keep=(qid in exempt)→ continue"会把下面这整段结算**短路成死代码**
            #    (2026-10-09 第九轮实测:b-02 的 `slice_start` 72 分钟从没被结算、`stale_slices` 恒为 0,
    #     安全阀形同不存在;单元测试当时没 mock 在场 worker,所以没照出来)。
            if qid in exempt:
                # 链式题钉死名额(容器一关链条清零,实测失分 54% 都在这),但**钉住不等于免检**:
                # 每个时间片到点结算一次"这片有没有新 flag",连着 `chain.max_attempts` 个片零产出
                # 就摘掉钉名额资格、按普通题让位(2026-10-08 第八轮:b-02 前 30 分钟拿 2/6,
                # 之后零产出钉了 5.5 小时 —— 同期广度 3.8 flag/槽时 vs 链式 1.5)。
                _start = float(e.get("slice_start") or 0)
                _limit = slice_for(e, slice_s)
                if _start and now - _start >= _limit:
                    _done = int(e.get("correct_flag_count") or 0)
                    if _done > int(e.get("slice_flags") or 0):
                        e["stale_slices"] = 0                          # 本片有新 flag
                    else:
                        e["stale_slices"] = int(e.get("stale_slices") or 0) + 1
                    if int(e.get("stale_slices") or 0) >= PROF.chain_max_attempts():
                        e["slice_flags"] = _done     # 记账对齐:下面按"本片无产出"处理 → 立刻让位
                        e["_pin_logged"] = False
                        board_mod.add_fact(b, "runner",
                                           f"{e.get('title', qid)} 连续 {e['stale_slices']} 个时间片零产出,"
                                           f"摘掉钉名额资格、让出容器给广度题")
                        log(f"安全阀:释放 {qid}(连续 {e['stale_slices']} 片没产出,不再钉名额)")
                    else:
                        e["slice_start"], e["slice_flags"] = now, _done   # 续期并留在钉住名单里
                if int(e.get("stale_slices") or 0) < PROF.chain_max_attempts():
                    if not e.get("_pin_logged"):
                        e["_pin_logged"] = True
                        board_mod.add_fact(b, "runner",
                                           f"{e.get('title', qid)} 链式题({e.get('flag_count')} flag):"
                                           f"钉住容器名额,不参与时间片轮换")
                        log(f"钉住名额:{e.get('title', qid)}({e.get('flag_count')} flag 链式题)")
                    continue
                # 落到这里 = 安全阀触发 → 摘掉钉资格,继续往下走普通时间片判据(会把它让位出去)
            # ② 保护判定:pinned_ok = "钉住且安全阀没释放它";被安全阀释放的题不再享受这份保护
            pinned_ok = qid in exempt and int(e.get("stale_slices") or 0) < PROF.chain_max_attempts()
            if _protected(qid, now):
                keep = pinned_ok or not PROF.is_pinned(e)
                if keep:
                    continue                # 受保护的普通题 / 仍被钉住的链式题 → 不动它
                # 超配额(或已被安全阀释放)的链式题:**只享受最小保护期**,不因"worker 还在跑"就无限期占名额 ——
                # 链式会话长达 3 小时,而 ROTATE_WAIT_RUNNING=1 下"有 worker 在跑"= 永不轮换,
                # 于是 3 道链式题能把 3 个名额全钉死、广度题(81% 的 flag)一道都排不上(2026-10-08 第七轮实测)。
                _age = _oldest_worker_age(qid, now)
                if WORKER_MIN_LIFETIME > 0 and 0 <= _age < WORKER_MIN_LIFETIME:
                    continue                # 刚起不久,给最小保护期
            start = float(e.get("slice_start") or 0)
            limit = slice_for(e, slice_s)
            if not start or now - start < limit:
                continue
            done = int(e.get("correct_flag_count") or 0)
            if done > int(e.get("slice_flags") or 0):        # 这一片有新收获 → 续期
                e["slice_start"], e["slice_flags"] = now, done
                e["stale_slices"] = 0                        # 有产出 → 安全阀计数清零(链式题继续被钉住)
                board_mod.add_fact(b, "runner", f"{e['title']} 有新进展,继续占用容器名额")
                continue
            e["slices"] = int(e.get("slices") or 0) + 1   # 记一次"已经用过完整机会"
            # 这一片**零新 flag** → 安全阀计数 +1(连着几片都没产出才允许链式题被放走)
            e["stale_slices"] = int(e.get("stale_slices") or 0) + 1
            e["rotate_until"] = now + ROTATE_COOLDOWN
            e["slice_start"] = 0
            e["slice_begun"] = False
            e["status"] = board_mod.STATUS_PENDING
            e["next_retry_at"] = now + ROTATE_COOLDOWN
            board_mod.add_fact(b, "runner",
                               f"{e['title']} 时间片({limit}s)用满且无新进展,"
                               f"轮换让位,{ROTATE_COOLDOWN}s 后再来")
            log(f"轮换:{e['title']} 让出容器名额(留给还没碰过的题;本题时间片 {limit}s)")


def _mark_holds(entries: list[dict], now: float) -> list[dict]:
    """给容器调度标"谁的名额优先保住":有 agent 在跑的题一律保;链式题**限量**保。

    实测(第六次 Docker 测试):3 道链式题全标 hold → 3 个名额被占满,广度题一道都排不上;
    而广度占 82% 的 flag。所以链式题的"钉死"有名额上限(策略包 budgets.chain.max_pinned_slots,
    默认 2):超出上限的链式题按普通题排队(它们仍能靠"有 agent 在跑"保住名额)。
    返回被钉住的题号(给测试/日志用)。
    """
    pinned: list[dict] = []
    for e in entries:
        # 有 agent 在跑(或在保护期)→ 优先保住它的容器名额,别让轮换把正在干活的靶子收走
        # (实测 24 个会话死于"容器已释放")。
        e["hold_container"] = _protected(e["question_id"], now)
        if PROF.is_pinned(e):
            pinned.append(e)
    cap = max(0, PROF.chain_max_pinned_slots())
    # 谁优先拿到钉死名额:关卡低 → flag 多 → 尝试次数少(先把最可能做出来的链式题钉住)
    pinned.sort(key=lambda e: (int(e.get("level") or 0),
                               -int(e.get("flag_count") or 1),
                               int(e.get("attempts") or 0)))
    held = []
    for e in pinned[:cap]:
        e["hold_container"] = True
        held.append(e.get("question_id"))
    # 超出名额上限的链式题:标一笔"超配额",让容器排序把它排到普通题之后
    # (见 tsec_api._rank 的第二位)。否则它们会凭 关卡/难度 挤掉广度题抢空出来的名额 ——
    # 广度占 82% 的 flag,而链式题超出上限后本来就该"按普通题排队"。
    for e in pinned[cap:]:
        e["chain_beyond_cap"] = True
    return held


def manage_containers(env: dict) -> None:
    """容器生命周期:把"想跑的题"的容器拉起来(≤MAX_ACTIVE_CHALLENGES),其余关掉释放名额。

    必须在 spawn() 之前调用 —— 只有 container_ready 的题才会被派 agent。
    """
    if not hasattr(contest_api, "sync_containers"):
        return
    with board_mod.locked() as b:
        entries = [dict(e) for e in b["questions"].values()]
    if FOCUS_QIDS:      # 专注难题模式:容器名额只服务列表内的题(列表外的容器会被 sync 关掉腾名额)
        entries = [e for e in entries if e["question_id"] in FOCUS_QIDS]
    if not entries:
        return
    now0 = time.time()
    _mark_holds(entries, now0)
    try:
        mapping = contest_api.sync_containers(env["TEAM_TOKEN"], entries,
                                              max_active=MAX_ACTIVE_CHALLENGES,
                                              health=CONTAINER_HEALTH)
    except contest_api.ContestError as e:
        log(f"容器调度失败:{e}")
        return
    except Exception as e:
        log(f"容器调度异常:{type(e).__name__}: {e}")
        return
    now = time.time()
    lost: list[str] = []                     # 这一轮"从有容器变成没容器"的题
    with board_mod.locked() as b:
        for qid, info in mapping.items():
            e = b["questions"].get(qid)
            if e is None:
                continue
            was_ready = bool(e.get("container_ready"))
            ready = bool(info.get("container_ready"))
            new_slice = ready and not float(e.get("slice_start") or 0)
            if new_slice:                       # 刚拿到容器名额 → 记一笔并起算时间片
                board_mod.add_fact(b, "runner", f"{qid} 容器就绪 {info.get('container_addr')}")
                log(f"容器就绪:{qid} -> {info.get('container_addr')}")
                e["slice_start"] = now
                e["slice_flags"] = int(e.get("correct_flag_count") or 0)
            note = str(info.get("note") or "")
            # 2026-10-25 第四轮教训:start/close 失败只记在 note 里,而 note 只在 status 变化时打印 ——
            # 通道坏时 status 恒不变,连续 5 小时的开容器失败一行日志都没有。现在失败类 note 至少每 10 分钟必报。
            if note and (e.get("container_status") != info.get("container_status")
                         or ("失败" in note and now - _ctr_fail_note_at.get(qid, 0) >= API_FAIL_RELOG_S)):
                if "失败" in note:
                    _ctr_fail_note_at[qid] = now
                    board_mod.add_fact(b, "runner", f"{qid} 容器操作失败:{note[:90]}")
                log(f"容器异常:{qid} {note}")
            e["container_ready"] = ready
            e["container_status"] = info.get("container_status") or ""
            if info.get("connection"):
                # 换地址了(容器重开):旧地址自动进"已失效"名单,黑板顶部的 harness 块会同步更新
                if _accept_conn(e, info["connection"]):
                    board_mod.add_fact(b, "runner",
                                       f"{e.get('title', qid)} 容器地址变更:"
                                       f"{'、'.join((e.get('dead_addrs') or [])[-1:])} → "
                                       f"{'、'.join(_conn_values(info['connection']))}"
                                       f"(旧地址已作废,黑板顶部为准)")
                    log(f"容器地址变更:{qid} -> {'、'.join(_conn_values(info['connection']))}(旧地址已标记失效)")
            elif not ready and _conn_values(e.get("connection")):
                # 容器没了:当前地址立刻作废(别让 agent 继续打这个死地址)
                _mark_dead(e, "容器已释放")
                log(f"容器释放:{qid} 当前地址已标记失效")
            if info.get("container_addr"):
                e["container_addr"] = info["container_addr"]
            if was_ready and not ready:
                lost.append(qid)
        # 本轮没被容器调度覆盖到的题(名额之外 / 冷却期)一律视为"不可派发"。
        # 少了这一步,刚下发的题会带着默认值 container_ready=True 被派 agent —— 没有目标地址可打。
        for qid, e in b["questions"].items():
            if qid not in mapping:
                if e.get("container_ready") and not e.get("is_solved"):
                    lost.append(qid)
                e["container_ready"] = False
    # 容器没了的题:正在跑的 agent 已经没有目标,立刻收掉(别让它们对着死地址烧 token)
    for qid in lost:
        if workers_of(qid):
            kill_question(qid, "容器已释放")


_ctr_fail_note_at: dict[str, float] = {}    # qid -> 上次报"容器操作失败"的时刻(节流用)


def _stall_reason(idle_s: float, active_workers: int, unsolved: int,
                  warn_s: int = -1, min_pending: int = -1) -> str:
    """调度停滞判定(纯函数,给单测)。返回告警文案;不满足条件返回空串。

    背景(2026-10-25 第四轮):平台通道坏掉后,缓存里只剩 1 个容器,harness 把同一道题
    "派工→超时→轮换→再派"循环了 6 轮、4 小时,99 道题排队零派发 —— 期间没有任何一行日志提示异常。
    """
    warn_s = STALL_WARN_S if warn_s < 0 else warn_s
    min_pending = STALL_MIN_PENDING if min_pending < 0 else min_pending
    if warn_s <= 0 or idle_s < warn_s or active_workers > 1 or unsolved < min_pending:
        return ""
    return (f"调度停滞 {int(idle_s // 60)} 分钟(并发 {active_workers}、{unsolved} 题未解)—— "
            f"多半是平台接口/容器开不出来,看『平台接口连续失败』与『容器异常』日志")


def _stall_scope(questions: dict) -> list[dict]:
    """停滞统计的范围:专注模式只看 FOCUS_QIDS 内的题。

    不这么做的话,列表外那 100 多道 pending 会让"未解数 ≥ STALL_MIN_PENDING"永远成立,
    只要有一次短会话接力(那一拍并发=0)就会弹出一条假停滞(2026-10-28 实测)。
    """
    return [e for qid, e in questions.items() if not FOCUS_QIDS or qid in FOCUS_QIDS]


def check_stall() -> None:
    """主循环里调用:距上次"新派工或新解出"太久、并发≤1、还有一堆未解 → 响亮地告警(日志 + 黑板)。

    节流:两次告警至少间隔 STALL_WARN_S。
    """
    if STALL_WARN_S <= 0:
        return
    now = time.time()
    last = globals().get("_last_activity_ts") or 0
    if not last:
        return
    with board_mod.locked() as b:
        pool = _stall_scope(b["questions"])
        unsolved = sum(1 for e in pool
                       if e["status"] not in (board_mod.STATUS_SOLVED, board_mod.STATUS_FAILED))
    min_pending = STALL_MIN_PENDING if not FOCUS_QIDS else min(STALL_MIN_PENDING, len(FOCUS_QIDS))
    msg = _stall_reason(now - last, len(workers), unsolved, min_pending=min_pending)
    if not msg or now - globals().get("_stall_note_at", 0) < STALL_WARN_S:
        return
    globals()["_stall_note_at"] = now
    log(f"⚠ {msg}")
    with board_mod.locked() as b:
        board_mod.add_fact(b, "runner", msg)


def _touch_activity_on_progress() -> None:
    """有新解出也算"活着"(给 check_stall 用;新派工在 launch_prepared 里直接刷)。"""
    with board_mod.locked() as b:
        n = sum(1 for e in b["questions"].values() if e["status"] == board_mod.STATUS_SOLVED)
    if n > globals().get("_solved_mark", 0):
        globals()["_solved_mark"] = n
        globals()["_last_activity_ts"] = time.time()


def _dispatch_order(entries: list[dict]) -> list[dict]:
    """派发顺序:没跑过的先上,然后按 时间片数 → 最久没被服务 → 关卡。

    最后那个 `last_started_at` 是 2026-10-28 补的:并发被限流降到 1 时,三题的
    slices/level 全打平、排序是"稳定"的,列表里第一道题会永久霸占唯一槽位
    (实测 066 连跑 2 小时,081/092 一次都没派到)。改成按"上次开跑时间"排,
    低并发下自然轮转,不需要额外的轮换器。
    """
    return sorted(entries, key=lambda e: (e["status"] != board_mod.STATUS_PENDING,
                                          int(e.get("slices") or 0),
                                          float(e.get("last_started_at") or 0),
                                          int(e.get("level") or 0)))


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
            and e.get("container_ready", True)      # 众测:容器没就绪(或名额轮换走了)就不派
        ]
        entries = _dispatch_order(entries)
        if FOCUS_QIDS:      # 专注难题模式:列表外的题不派发(留在黑板,不计入 goal_done)
            entries = [e for e in entries if e["question_id"] in FOCUS_QIDS]

        # 两轮分配,保证广度优先:
        #   第一轮 每道题先各给 1 个 agent(题目多时能覆盖最多道题)
        #   第二轮 若还有空位(题目数少于槽位数),再给题目加派 agent 到上限
        def used_slots(qid: str) -> set[int]:
            s = {w["slot"] for w in workers.values() if w["qid"] == qid}
            s |= {slot for (q, slot) in _prepping if q == qid}
            return s

        # 收尾期自动增援:判据见 endgame_targets()(可单测)。**门的判据是全局未解题数** ——
        # entries 只是"有容器名额、此刻能派的题"(众测下被名额数卡在 ≤3),拿它当"还剩几题"会让
        # 收尾期从开跑第一分钟就误触发(实测 63 题里 60 题没碰却判定"只剩 2 道未解")。
        unsolved_total = sum(1 for e in b["questions"].values()
                             if e["status"] not in (board_mod.STATUS_SOLVED, board_mod.STATUS_FAILED))
        endgame_qids = endgame_targets(entries, free, now, unsolved=unsolved_total)
        if endgame_qids:
            log(f"收尾期:全局只剩 {unsolved_total} 道未解,给 {sorted(endgame_qids)} 自动加派补位 agent"
                f"(空闲并发位 {free} 个)")

        def cap_for(entry: dict) -> int:
            """同题并发上限:默认 WORKERS_PER_QUESTION;agent 在黑板请求增援后放宽到 MAX_HELPERS_PER_QUESTION。

            收尾期(策略包 `dispatch.endgame`)再放宽一档:剩下的未解题不多、并发位空着,
            而某道题已经跑了一阵子还没出新 flag → 自动加派补位 agent,不必等人来写增援请求。
            实测第四次跑分:全场只剩最后一题时 5 个并发位空着 4 个,增援却是收工前 14 分钟
            才由操作者手动加上的 —— 这段就是给那种局面兜底的。
            """
            if FOCUS_WORKERS > 0 and entry.get("question_id") in FOCUS_QIDS:
                return max(FOCUS_WORKERS, WORKERS_PER_QUESTION)
            try:
                reqs = solver.read_helper_requests(entry)
            except Exception:
                reqs = []
            if len(reqs) > int(entry.get("helpers_granted", 0) or 0):
                return MAX_HELPERS_PER_QUESTION
            if entry.get("question_id") in endgame_qids:
                return MAX_HELPERS_PER_QUESTION
            return WORKERS_PER_QUESTION

        plans: list[tuple[dict, int, int]] = []  # (entry, slot, total_slots)
        added: dict[str, int] = {}
        if FOCUS_WORKERS > 0 and FOCUS_QIDS:
            # 专注模式 = 深度优先:一道题先吃满 FOCUS_WORKERS 个位子(不同 ANGLE 并行)再轮下一道,
            # 否则 14 道难题会把 3 个名额摊成广度、每题仍只有 1 个 agent(2026-10-11 复盘定策)。
            for e in entries:
                if free <= 0:
                    break
                qid = e["question_id"]
                taken = used_slots(qid)
                cap = cap_for(e)
                for slot in range(1, cap + 1):
                    if free <= 0:
                        break
                    if slot in taken:
                        continue
                    plans.append((e, slot, cap))
                    added[qid] = added.get(qid, 0) + 1
                    free -= 1
        else:
            for e in entries:  # 第一轮:每道题先各给 1 个 agent
                if free <= 0:
                    break
                qid = e["question_id"]
                if used_slots(qid):
                    continue
                first_total = cap_for(e) if (FOCUS_WORKERS > 0 and qid in FOCUS_QIDS) else WORKERS_PER_QUESTION
                plans.append((e, 1, first_total))
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
                            elif qid in endgame_qids and ENDGAME.get("brief"):
                                e2["squad_task"] = str(ENDGAME.get("brief"))   # 自动补位:策略包里的分工简报
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

# 知识库里这些路径是工具自带源码/自测,检索命中也没法用来解题
_KB_NOISE = ("/src/", "/tests/", "/test/", "rsactftool/src")
_KB_GENERIC = {"pentest", "ctf", "web", "pwn", "misc", "crypto", "reverse", "forensics",
               "penetration", "testing", "benchmark", "challenge", "difficulty", "level",
               "flag", "flags", "points", "score", "hard", "medium", "easy", "full"}


def _kb_meaningful_query(q: dict) -> str:
    """把题目的分类/标题/描述压成一句有信息量的检索词。

    这版平台的 title 就是题号、description 只有「(难度 hard · 满分 500 · 共 1 flag)」这类元信息,
    原样拼起来等于按题号检索:实测命中 1386 段、前三条是 RsaCtfTool 的素数表,纯噪声。
    这里剥掉题号和元信息,剩下的词不够就返回空 → 宁可不注入,也别把无关源码推到 agent 面前。
    """
    raw = " ".join(x for x in (q.get("category") or "", q.get("title") or "",
                               (q.get("description") or "")[:200]) if x)
    raw = re.sub(r"\(难度[^)]*\)", " ", raw)
    raw = re.sub(r"\b(?:xben|bench|task|challenge|q)[-_ ]?\d[\w-]*\b", " ", raw, flags=re.I)
    raw = re.sub(r"[·|;:：/\\()\[\]{}]+", " ", raw)
    toks = [t for t in raw.split()
            if len(t) >= 3 and not t.isdigit() and t.lower() not in _KB_GENERIC]
    return " ".join(toks)[:120]


def _kb_keep_blocks(out: str, limit: int = 3) -> str:
    """只保留可读的资料块:滤掉工具源码/自测路径,并按 limit 截断。"""
    kept = []
    for block in out.split("\n\n"):
        block = block.strip()
        head = block.splitlines()[0] if block else ""
        if not head.startswith("["):
            continue
        if any(x in head.lower() for x in _KB_NOISE):
            continue
        kept.append(block)
        if len(kept) >= limit:
            break
    return "\n\n".join(kept)


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
    kw = _kb_meaningful_query(q)
    if not kw:
        return ""            # 题目本身没有可检索的描述 → 不注入,免得推一堆工具源码
    hint = ""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "kb.py"), "search", kw, "--limit", "8"],
                           capture_output=True, text=True, timeout=25, cwd=ROOT)
        out = (r.stdout or "").strip()
        if out and "未找到" not in out:
            hint = _kb_keep_blocks(out, 3)[:1400]
    except Exception as e:
        log(f"知识库自动检索失败({type(e).__name__}),跳过注入")
    if not hint:
        return hint          # 空结果不落缓存(检索失败/超时/真没资料),下次尝试会重试
    _kb_hint_cache[qid] = hint
    with board_mod.locked() as b:
        board_mod.add_fact(b, "runner", f"为 {q.get('title', qid)} 自动注入知识库片段 {len(hint)} 字")
    log(f"已为 {q.get('title', qid)} 自动注入知识库片段({len(hint)} 字)")
    return hint


_hint_cache: dict[str, str] = {}
_hint_skipped: set = set()      # 按难度策略跳过 hint 的题(每道只打一条日志,便于审查 grep)


def _difficulty_of(q: dict) -> str:
    """题目的难度档(不同适配器放的位置不同:条目顶层或 extensions 里)。"""
    d = q.get("difficulty") or (q.get("extensions") or {}).get("difficulty") or ""
    return str(d).strip().lower()


def hint_threshold_for(q: dict) -> int:
    """同一题"失败几次后取平台 hint"——**按难度分档**(2026-10-19 用户拍板:难题尽早用、简单题不用)。

      hard / medium(HINT_EARLY_DIFFS,默认 "hard,medium") → 0 : 首次尝试就注入(难题别浪费时间硬撞);
      easy(HINT_SKIP_DIFFS,默认 "easy")                   → -1: 永不取(简单题自己应当能解,省下扣分);
      其余(未知难度 / 没配)                                → HINT_AFTER_ATTEMPTS(默认 1 = 第 2 次尝试)。
    HINT_AFTER_ATTEMPTS=0 仍是全局关闭(优先级最高,0=永不)。
    """
    if HINT_AFTER_ATTEMPTS <= 0:
        return -1
    diff = _difficulty_of(q)
    if diff and diff in HINT_SKIP_DIFFS:
        return -1
    if diff and diff in HINT_EARLY_DIFFS:
        return 0
    return HINT_AFTER_ATTEMPTS


def hint_for(q: dict) -> str:
    """按 hint_threshold_for() 的分档阈值取一次平台 hint 注入 prompt。

    平台按 hint_cost_radio 扣该题后续 flag 的得分(实测该比例 = 该题分值的 10%:easy -20 / medium -30 /
    hard -50 —— 2026-10-19 用平台点数对账算出来的,很便宜)。**只在 flag 模式有意义**(正式渗透没有平台 hint)。
    取回的提示缓存在进程内;空结果(网络失败/该题确实没 hint)不落缓存,下一次尝试会重取。
    """
    if PROF.goal != "flags":
        return ""
    if not hasattr(contest_api, "get_hint"):
        return ""
    threshold = hint_threshold_for(q)
    qid = q.get("question_id", "")
    if threshold < 0:                      # easy 不取 / HINT_AFTER_ATTEMPTS=0 全局关闭
        if qid and qid not in _hint_skipped:
            _hint_skipped.add(qid)
            log(f"按策略跳过平台 hint({qid},难度={_difficulty_of(q) or '?'})")
        return ""
    if qid in _hint_cache:
        return _hint_cache[qid]
    # 已取过的提示要从黑板复用:平台按"每次查看"记事件(2026-10-28 实测:只该取 3 条,
    # 因为重启 7 次重取,事件列表里出现 21 条「查看提示」)。进程内缓存熬不过重启,
    # 落进黑板才能保证"取过就不再打接口"。
    try:
        with board_mod.locked() as b:
            saved = str((b["questions"].get(qid) or {}).get("hint_text") or "")
    except Exception:
        saved = ""
    if saved:
        _hint_cache[qid] = saved
        return saved
    text = ""
    try:
        text = contest_api.get_hint(os.environ.get("TEAM_TOKEN", ""), qid) or ""
    except Exception as e:
        log(f"取 hint 失败({qid}): {type(e).__name__}: {e}")
    if not text:
        # 空结果不落缓存 —— 平台抖动/网络失败时,下次尝试还会再取。
        # (2026-10-13 实测:SSL 抖动期间 3 次取 hint 全失败,空结果被永久缓存,这几题的 hint 再没机会拿到)
        log(f"取 hint 未取到内容({qid}),不缓存,下次尝试重试")
        return text
    _hint_cache[qid] = text
    with board_mod.locked() as b:
        entry = b["questions"].get(qid)
        if entry is not None:
            entry["hint_text"] = text          # 重启后复用,避免重复扣分/重复记事件
        board_mod.add_fact(b, "runner", f"为 {qid} 取到平台提示(该题后续得分按比例扣减)")
    log(f"已为 {qid} 取平台提示({len(text)} 字,该题得分按比例扣减)")
    return text


def gw_latency_probe(env: dict) -> float:
    """用一次极小的流式请求量模型端点总耗时(秒);失败返回 -1。

    为什么另开一条连接:pi 的请求走的是它自己的 provider 配置,runner 侧拿不到分步延迟;
    一个小探针足以看出"整体是不是慢了一个档"(第六轮:探针 10~60s vs 健康 1.7~3s)。
    """
    import json as _json
    import urllib.request as _url
    base = (os.environ.get("PI_BASE_URL") or "").strip().rstrip("/")
    if not base:
        try:
            import config as _cfg
            base = str(_cfg.read_env().get("PI_BASE_URL") or "").strip().rstrip("/")
        except Exception:
            base = ""
    model = env.get("PI_MODEL_NAME") or os.environ.get("PI_MODEL") or ""
    if not base or not model:
        return -1.0
    key = env.get("PI_API_KEY") or os.environ.get("PI_API_KEY") or ""
    body = _json.dumps({"model": model, "stream": True, "max_tokens": 16,
                        "messages": [{"role": "user", "content": "Say OK"}]}).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
            "User-Agent": "rotom-probe/1.0"}
    # opencode 的网关要求会话头,否则 403/400(实测);pi 自己会自动加,探针得自己带。
    # 顺手给所有端点带上无害的 UA:个别网关/Cloudflare 对空 UA 直接 1010。
    if "opencode.ai" in base:
        import uuid as _uuid
        hdrs["x-opencode-session"] = str(_uuid.uuid4())
        hdrs["x-opencode-client"] = "rotom-probe"
    req = _url.Request(base + "/chat/completions", data=body, headers=hdrs)
    opener = _url.build_opener(_url.ProxyHandler({}))     # 本机代理会破坏这类请求(实测)
    t0 = time.time()
    try:
        with opener.open(req, timeout=GW_PROBE_TIMEOUT) as resp:
            resp.read(4096)                               # 读一点就够,别等完整流
    except Exception as e:                                # noqa: BLE001
        log(f"模型延迟探针失败({type(e).__name__}: {str(e)[:80]})")
        return -1.0
    return round(time.time() - t0, 2)


def _gw_check(env: dict, now: float | None = None) -> None:
    """跑中周期复测模型延迟:超基线倍数告警、恢复报一次(都写黑板,审查/看护能看到)。"""
    if GW_PROBE_INTERVAL <= 0:
        return
    now = now or time.time()
    if now - float(globals().get("_gw_probe_at") or 0) < GW_PROBE_INTERVAL:
        return
    globals()["_gw_probe_at"] = now
    cur = gw_latency_probe(env)
    base = float(globals().get("_gw_baseline") or 0)
    slow = bool(base > 0 and cur > 0 and cur >= max(5.0, base * GW_SLOW_FACTOR))
    if slow:
        log(f"⚠ 模型服务变慢:探针 {cur}s(开局基线 {base}s,{cur / base:.1f}×)—— "
            f"worker 每步都会更慢,必要时修/换网关(历史:变慢 2~7 倍会直接拖垮完成率)")
        with board_mod.locked() as b:
            board_mod.add_fact(b, "runner", f"模型服务变慢:探针 {cur}s vs 基线 {base}s({cur / base:.1f}×)")
    elif globals().get("_gw_slow"):
        log(f"模型延迟恢复:探针 {cur}s(基线 {base}s)")
    globals()["_gw_slow"] = slow


def _elapsed_of(entry: dict | None, now: float | None = None) -> float:
    """当前"完整尝试"已经累计用了多少秒(跨短会话接力/runner 重启都连续)。"""
    started = float((entry or {}).get("attempt_started_at") or 0)
    if started <= 0:
        return 0.0
    return max(0.0, (now or time.time()) - started)


def full_worker_timeout_for(attempts: int, entry: dict | None = None) -> int:
    """一次完整尝试的预算(不含短会话切片)。"""
    budget = PROF.session_budget(entry)
    if budget > 0:
        return budget + max(0, attempts - 1) * TIMEOUT_ESCALATE
    full = WORKER_TIMEOUT + max(0, attempts - 1) * TIMEOUT_ESCALATE
    return min(full, MAX_WORKER_TIMEOUT) if MAX_WORKER_TIMEOUT > 0 else full


def worker_timeout_for(attempts: int, entry: dict | None = None) -> int:
    """返回**单个 pi session** 的硬限时:完整预算减去本段尝试已用时间,再由切片封顶。"""
    full = full_worker_timeout_for(attempts, entry)
    left = full - int(_elapsed_of(entry))
    if SESSION_SLICE_SECONDS > 0:
        return max(1, min(SESSION_SLICE_SECONDS, left))
    return max(1, left)


def session_handoff_for(attempt: int, entry: dict | None = None, timeout: int | None = None) -> bool:
    """这次 session 到点后是否属于 handoff(完整预算还剩,只是到了切片点)。"""
    if SESSION_SLICE_SECONDS <= 0:
        return False
    full = full_worker_timeout_for(attempt, entry)
    tm = timeout if timeout is not None else worker_timeout_for(attempt, entry)
    return _elapsed_of(entry) + tm < full


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
            _live = b["questions"].get(qid) or {}
            prev_attempts = int(_live.get("attempts", 0) or 0)
            if not float(_live.get("attempt_started_at") or 0):
                _live["attempt_started_at"] = time.time()      # 完整尝试起点(接力/重启都保留)
            attempt_entry = dict(_live)
        this_attempt = prev_attempts + 1
        tm = worker_timeout_for(this_attempt, attempt_entry)
        handoff = session_handoff_for(this_attempt, attempt_entry, tm)
        if prev_attempts >= 1:                 # 之前已经试过没成 → 主动把知识库资料推给它
            e["kb_hint"] = kb_hint_for(e)
        hthr = hint_threshold_for(e)           # 按难度分档:hard/medium 首次尝试就给,easy 不给
        if hthr >= 0 and prev_attempts >= hthr:
            e["hint_text"] = hint_for(e)       # 会扣该题 10% 分值(实测),难题上这钱值得花
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
                if not entry.get("slice_begun"):
                    # 时间片从"agent 真正开工"起算:容器就绪到派上 worker 之间可能要等名额,
                    # 那段空等不该算进这道题的机会里(实测有题白等 5 分钟,等于少跑 1/4 时间片)
                    entry["slice_start"] = time.time()
                    entry["slice_begun"] = True
                entry["attempts"] += 1        # 真正启动才计数;不限次数时不设上限
                entry["last_started_at"] = time.time()   # 低并发时的公平轮转依据(见 _dispatch_order)
                entry["worker_pid"] = proc.pid
                if e.get("squad_task"):        # 增援 worker:记账,避免同一请求被反复满足
                    entry["helpers_granted"] = int(entry.get("helpers_granted", 0) or 0) + 1
                    board_mod.add_fact(b, "runner",
                                       f"为 {entry.get('title', qid)} 增派 agent(第 {entry['helpers_granted']} 个):"
                                       f"{e['squad_task'][:80]}")
        workers[f"{qid}#{slot}"] = {
            "proc": proc, "qid": qid, "slot": slot, "started": time.time(),
            "timeout": tm,
            # 到点后是否接力(tm 是切片且完整预算未耗尽)→ reap() 据此不把这次当失败
            "handoff": handoff,
            "handoff_index": int((attempt_entry.get("session_elapsed") or 0) // max(1, SESSION_SLICE_SECONDS)) + 1
            if handoff else 0,
        }
        register_pid(proc.pid)
        globals()["_last_activity_ts"] = time.time()      # 新派工 = 调度活着(check_stall 的锚点)
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

BACKFILL_INTERVAL = _int("BACKFILL_INTERVAL", 300)      # 秒;0=关闭补交
_backfill_last = 0.0


def maybe_backfill_flags() -> None:
    """通道可用时,把"agent 提交过、平台没受理"的 flag 补交一次(见 tools/backfill_flags.py)。

    边界由那个模块写死:只补 agent **真的提取到并尝试提交过**的值(记在 logs/pending_flags.jsonl),
    `WRONG` 永不进待办、永不重试,每条最多补 BACKFILL_MAX_ATTEMPTS 次,不做任何变体猜测。
    为什么需要:第六次 Docker 测试里 c-03 的值核对过却因通道坏掉一直交不上,agent 自己写重试循环
    烧掉几小时 —— 通道恢复后的那一次补交,应该由 harness 做,而不是让 agent 反复投。
    节流到每 BACKFILL_INTERVAL 秒一次;任何异常都不许影响主循环。
    """
    global _backfill_last
    if BACKFILL_INTERVAL <= 0:
        return
    now = time.time()
    if now - _backfill_last < BACKFILL_INTERVAL:
        return
    _backfill_last = now
    try:
        tools_dir = str(ROOT / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import backfill_flags                      # noqa: PLC0415   (懒加载:没待办时零成本)
        c = backfill_flags.run_once(log=log)
        if c["tried"] or c["dropped"]:
            log(f"[backfill] 已确证 flag 补交:试 {c['tried']} · 成功 {c['ok']} · "
                f"判错丢弃 {c['wrong']} · 仍未受理 {c['busy']} · 达上限放弃 {c['dropped']}")
    except Exception as e:                         # noqa: BLE001  补交是兜底,不许拖垮调度
        log(f"[backfill] 本轮跳过({type(e).__name__}: {e})")


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
    flags = sum(int(q.get("correct_flag_count") or 0) for q in qs)
    total_flags = sum(int(q.get("flag_count") or 1) for q in qs)
    parts = []
    for q in qs:
        prog = board_mod.flag_progress(q)
        parts.append(f"{q['title']}:{q['status']}{f'({prog})' if prog else ''}")
    left = time_left()
    tleft = f" | 剩余 {left//60}分{left%60:02d}秒" if left >= 0 else ""
    if PROF.goal == "findings":        # 正式渗透:进度看 findings,没有 flag 概念
        findings = sum(int(q.get("findings_count") or 0) for q in qs)
        flaginfo = f" · findings {findings} 条"
        unit = "个目标"
    else:
        flaginfo = f" · flag {flags}/{total_flags}" if total_flags != len(qs) else ""
        unit = "题"
    log(f"进度 {solved}/{len(qs)} {unit}{flaginfo} | 并发 {len(workers)}/{concurrency.limit}{tleft} | "
        + " | ".join(parts))


RUNNER_PID_FILE = RUN_DIR / "logs" / "runner.pid"


def _graceful_signal(*_) -> None:
    """收到 SIGTERM/SIGINT 时走 KeyboardInterrupt 分支,把 worker 一起收掉再退出。"""
    raise KeyboardInterrupt


def write_stop_reason(reason: str, detail: str = "") -> None:
    """把终止原因写进 <数据目录>/logs/run_stop.json(复盘、审计、报告都要用)。

    跑分模式只允许四种原因:goal_done / platform_dead / net_gone / token_bad
    (人工停止单独记 manual)。写之前检查一下是不是策略包允许的原因,免得以后有人随手加一个。
    """
    allowed = set(STOP_CONDITIONS) | {"manual", "window_end", "stall_fuse"}
    if reason not in allowed:
        log(f"⚠ 终止原因 {reason} 不在策略包允许的集合里({sorted(allowed)}),仍照实记录")
    try:
        with board_mod.locked() as b:
            qs = list(b["questions"].values())
        data = {
            "reason": reason, "detail": detail, "ts": time.strftime("%F %T"),
            "stop_mode": STOP_MODE, "allowed": sorted(STOP_CONDITIONS),
            "profile": PROF.name, "project": str(RUN_DIR),
            "solved": sum(1 for q in qs if q["status"] == board_mod.STATUS_SOLVED),
            "total": len(qs),
            "flags": sum(int(q.get("correct_flag_count") or 0) for q in qs),
            "total_flags": sum(int(q.get("flag_count") or 1) for q in qs),
            "findings": sum(int(q.get("findings_count") or 0) for q in qs),
            "net_failures": globals().get("_net_fail_streak", 0),
        }
        f = LOGS / STOP_FILE_NAME
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"终止原因已落盘:{reason}({detail})→ {f}")
    except OSError as e:
        log(f"写 {STOP_FILE_NAME} 失败:{e}")


def _platform_stop_reason(e) -> str:
    """把平台业务错误映射成终止原因:token 失效 vs 任务结束(其余不可恢复)。"""
    code = str(getattr(e, "code", "") or "")
    msg = str(getattr(e, "message", "") or "")
    if code in ("task_not_found", "unauthorized", "invalid_token") or "token" in msg.lower() \
            or "凭证" in msg or "暂无队伍信息" in msg:
        return "token_bad"
    return "platform_dead"


def mark_round_done(reason: str) -> None:
    """本轮**正常跑完**了(窗口到点/任务结束/全部完成):留个标记让 supervisor 别再拉起来。

    少了这一步会出大问题:上面刚给 supervisor 加了"除明确停机外一律自愈",
    而收工退出的返回码同样是 0 —— 于是 20:00 正常收工会被当成崩溃,5 秒后又被拉起来,
    新进程还把过期窗口当成"明天同一时刻"重新计时(实测踩过:收工后又多跑了 3 分钟)。
    """
    try:
        f = RUN_DIR / "logs" / "stop.flag"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{time.strftime('%F %T')} {reason}\n", encoding="utf-8")
        log(f"本轮已结束({reason}):已标记,supervisor 不会再拉起")
    except OSError:
        pass


def main() -> None:
    global idle_logged
    RUN_DIR.mkdir(parents=True, exist_ok=True)   # 先建数据目录,任何入口都不会因缺目录崩
    LOGS.mkdir(parents=True, exist_ok=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只拉取一次题目并打印,不启动 worker")
    args = ap.parse_args()
    try:
        env = env_config()
    except MissingToken as e:
        # 缺凭证属于四种终止条件之一:落盘原因后正常退出,让入口脚本(容器)知道"这是终态,别再重启"
        log(f"{e} —— 收工(原因 token_bad)")
        write_stop_reason("token_bad", str(e))
        return
    # 把 .env 里的模型配置(base URL / key / 模型名 / API 类型)同步成 pi 的 provider
    try:
        import config as config_mod
        config_mod.sync_provider(config_mod.read_env(), quiet=True)
    except Exception as e:
        log(f"模型配置同步跳过: {e}")
    handle_leftover_workers(env)
    if args.once:
        pass
    else:
        (RUN_DIR / "logs" / "stop.flag").unlink(missing_ok=True)   # 新一轮开始,清掉上一轮的收工标记

    # 众测平台的强制前置:VPN 不通常规流程一步都不走(题目入口在 VPN 内)。
    # 策略包开关:ctf/众测默认关(见 profiles/*.json);正式渗透默认开(VPN 让测试流量不可追溯到本机)。
    # --once 例外:只用来确认 token/接口能不能通,不发任何题目请求、不启容器。
    if PROF.get("containers.vpn_gate", True) and not args.once and not vpn_gate(env):
        log("等待 VPN 连通(harness 已就绪,连上后自动开跑;Ctrl+C 或 ./start.sh stop 可退出)")

    try:
        questions = reconcile(env)
    except contest_api.ContestError as e:
        if getattr(e, "task_over", False):
            reason = _platform_stop_reason(e)
            log(f"平台返回不可恢复错误:{e} —— 收工(原因 {reason};请检查 token / 跑分任务是否已结束)")
            write_stop_reason(reason, f"{getattr(e, 'code', '')}: {getattr(e, 'message', '')[:120]}")
            if PROF.get("containers.manage", True):
                _shutdown_containers(env)
            return
        # 接口通了但业务失败(token 无效 / 比赛未开始或已结束):提示清楚,不退出,继续轮询
        log(f"比赛接口返回业务错误:{e} —— 保持轮询,等待接口恢复(可在控制台点「测试接口」排查)")
        questions = list(read_board_questions().values())
    except Exception as e:
        log(f"启动时查询题目失败:{e} —— 保持轮询重试")
        questions = list(read_board_questions().values())
    reset_solved_for_practice()
    log(f"拉取到 {len(questions)} {'个目标' if PROF.goal == 'findings' else '道题目'}")
    print_status()
    if args.once:
        for q in questions:
            if PROF.goal == "findings":
                state = "已交付" if (q.get("delivered") or q.get("is_solved")) else "进行中"
                prog = f"findings={int(q.get('findings_count') or 0)}"
            else:
                state = f"solved={q.get('is_solved')}"
                prog = board_mod.flag_progress(q)
            print(f"  - {q['title']} [{q.get('category')}] {state} "
                  f"{prog} container={q.get('container_status') or '-'} "
                  f"conn={q.get('connection') or '-'}")
        return

    log(f"循环以{'任务为界' if STOP_MODE == 'work' else '时间窗口为界'}"
        f"(终止条件:{'/'.join(sorted(STOP_CONDITIONS)) or '-'});时间只用于预算与节奏")
    log(f"启动自动解题[策略包 {PROF.name} · {PROF.title}] 起始并发={concurrency.limit} 下限={MIN_WORKERS} "
        f"同题最多={WORKERS_PER_QUESTION} 单题限时={WORKER_TIMEOUT}s"
        + (f" 容器名额={MAX_ACTIVE_CHALLENGES} 时间片={QUESTION_SLICE_SECONDS}s"
           if PROF.get("containers.manage", True) and hasattr(contest_api, "sync_containers") else "")
        + (f" 目标清单={contest_api.targets_path()}" if PROF.goal == "findings" and hasattr(contest_api, "targets_path") else ""))
    solver.WORK_DIR.mkdir(exist_ok=True)
    RUNNER_PID_FILE.parent.mkdir(exist_ok=True)
    RUNNER_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    globals()["_last_activity_ts"] = time.time()
    if GW_PROBE_INTERVAL > 0:      # 开局基线:慢网关开局就能看出来(别等 2~3 小时后的审查)
        _b = gw_latency_probe(env)
        globals()["_gw_baseline"] = _b if _b > 0 else 0.0
        globals()["_gw_probe_at"] = time.time()
        if _b > 0:
            log(f"模型延迟基线:{_b}s(64token 流式;健康约 1.5~3s,>8s 就值得怀疑)")
            if _b >= 8:
                log("⚠ 开局模型就很慢 —— 本轮的完成率会被它拖低;建议先修网关再全速跑")
                with board_mod.locked() as b2:
                    board_mod.add_fact(b2, "runner", f"开局模型延迟 {_b}s 偏高(健康 1.5~3s)")
        else:
            log("模型延迟基线:探针不可用(不影响开跑;跑中会再试)")
    signal.signal(signal.SIGTERM, _graceful_signal)
    try:
        loop = 0
        while True:
            loop += 1
            reap()
            kill_submitted_workers()
            observe_shared()
            revive_failed()
            # 限流平息够久就把并发还回去(只降不升会在网关抖动后永久锁死成单线程)
            concurrency.recover(time.time() - max(concurrency.last_reduce,
                                                  globals().get("_last_rl_ts") or 0))
            if PROF.get("containers.vpn_gate", True) and not vpn_gate(env):
                # VPN 掉了就冻住:不查题、不派题、不动容器(策略包关了 VPN 预检则跳过)
                write_runner_state()
                time.sleep(POLL_INTERVAL * 2)
                continue
            if PROF.get("containers.rotate", True):
                rotate(env)              # 容器名额轮换(用满时间片且无进展 → 让位)
            if PROF.get("containers.manage", True):
                manage_containers(env)   # 拉起想跑的题的容器,关掉其余的
            spawn(env)
            launch_prepared(env)
            _touch_activity_on_progress()
            check_stall()
            _gw_check(env)
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
                if globals().get("_api_error_logged"):
                    _mins = int((time.time() - globals().get("_api_fail_since", time.time())) // 60)
                    log(f"平台接口恢复(此前连续失败 {_mins} 分钟)—— 顺手补交待办 flag")
                    with board_mod.locked() as b:
                        board_mod.add_fact(b, "runner", f"平台接口恢复(中断 {_mins} 分钟)")
                globals()["_api_error_logged"] = False
                globals()["_net_fail_streak"] = 0      # 通了就清零(只有"连续"失败才判 net_gone)
                maybe_backfill_flags()                 # 平台刚确认可达 —— 顺手把待补交的 flag 投一次
            except contest_api.ContestError as e:
                if getattr(e, "task_over", False):
                    # 跑分任务已结束 / token 失效:继续跑只会一直报错,收工并如实报告
                    reason = _platform_stop_reason(e)
                    log(f"平台返回不可恢复错误:{e} —— 收工(原因 {reason})并释放容器")
                    with board_mod.locked() as b:
                        board_mod.add_fact(b, "runner", f"平台不可恢复错误({e.code}, {reason}),停止:{e.message[:80]}")
                    write_stop_reason(reason, f"{getattr(e, 'code', '')}: {getattr(e, 'message', '')[:120]}")
                    mark_round_done(f"平台不可恢复错误:{e.code}")
                    break
                # 2026-10-25 第四轮教训:连续失败 5.5 小时只在开头报了 1 次(提示位要等成功才复位),
                # 之后 5 小时日志"风平浪静"——其实提交通道全断、调度已冻结。现在至少每 API_FAIL_RELOG_S 再报一次。
                _t = time.time()
                if not globals().get("_api_error_logged"):
                    globals()["_api_error_logged"] = True
                    globals()["_api_fail_since"] = _t
                    globals()["_api_fail_count"] = 1
                    globals()["_api_fail_note_at"] = _t
                    log(f"比赛接口业务错误:{e} —— 继续轮询")
                    with board_mod.locked() as b:
                        board_mod.add_fact(b, "runner",
                                           f"平台接口开始失败:{str(e)[:90]}(持续中每 {API_FAIL_RELOG_S // 60} 分钟提示一次)")
                else:
                    globals()["_api_fail_count"] = globals().get("_api_fail_count", 0) + 1
                    if _t - globals().get("_api_fail_note_at", 0) >= API_FAIL_RELOG_S:
                        globals()["_api_fail_note_at"] = _t
                        _mins = int((_t - globals().get("_api_fail_since", _t)) // 60)
                        _n = globals().get("_api_fail_count", 0)
                        log(f"⚠ 平台接口已连续失败 {_mins} 分钟(累计 {_n} 次)"
                            f"—— 提交通道不可用,已确证的 flag 都在 pending 等补交")
                        with board_mod.locked() as b:
                            board_mod.add_fact(b, "runner",
                                               f"平台接口连续失败 {_mins} 分钟(累计 {_n} 次),提交通道不可用")
                time.sleep(max(POLL_INTERVAL, 60) if e.code == 203 else API_RETRY_DELAY)
            except Exception as e:
                n = globals().get("_net_fail_streak", 0) + 1
                globals()["_net_fail_streak"] = n
                log(f"查题接口异常({e}),连续第 {n} 次/{NET_GONE_STREAK};{API_RETRY_DELAY}s 后重试")
                if "net_gone" in STOP_CONDITIONS and n >= NET_GONE_STREAK:
                    log(f"网络/平台连续 {n} 次不可用 —— 按终止条件 net_gone 收工(不是时间到点)")
                    with board_mod.locked() as b:
                        board_mod.add_fact(b, "runner", f"网络连续 {n} 次不可用,按 net_gone 收工:{str(e)[:80]}")
                    write_stop_reason("net_gone", f"连续 {n} 次:{str(e)[:160]}")
                    mark_round_done(f"网络连续失败 {n} 次")
                    break
                time.sleep(API_RETRY_DELAY)
            # 补交与 reconcile 解耦(2026-10-25 第四轮教训):通道坏时 reconcile 一直抛,原先挂在它后面的
            # maybe_backfill_flags() 永远不跑,agent 找到的 flag 只能躺尸 —— 现在无条件调用,按它自带的 300s 节流独立重试。
            maybe_backfill_flags()
            if STOP_MODE == "window" and RUN_DEADLINE and time.time() >= RUN_DEADLINE:
                log("挑战窗口结束,停止所有 worker。")
                for key in list(workers):
                    kill_worker(key, "窗口结束")
                write_stop_reason("window_end", "挑战窗口到点(比赛制)")
                mark_round_done("窗口结束")
                break
            with board_mod.locked() as b:
                qs = list(b["questions"].values())
            if FOCUS_QIDS:      # 专注模式:goal_done 只看列表内的题
                qs = [q for q in qs if q["question_id"] in FOCUS_QIDS]
            all_done = qs and all(
                q["status"] in (board_mod.STATUS_SOLVED, board_mod.STATUS_FAILED) for q in qs)
            if all_done and not workers:
                if STOP_MODE == "work":
                    # 任务驱动:全部完成就是第一个终止条件(平台口径 is_completed 已同步进黑板)
                    log("全部题目已完成 —— 按终止条件 goal_done 收工。")
                    write_stop_reason("goal_done", f"{len(qs)} 个任务全部完成")
                    mark_round_done("全部题目已完成")
                    break
                if not KEEP_RUNNING:
                    log("所有题目已结束,退出。")
                    write_stop_reason("goal_done", "全部题目已结束(KEEP_RUNNING=0)")
                    mark_round_done("全部题目已结束")
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
        # 人工停止也记一笔(但如果已经因为四种终止条件收过工,就别覆盖那条更重要的原因)
        if not (LOGS / STOP_FILE_NAME).exists():
            write_stop_reason("manual", "人工停止(./start.sh stop 或控制台「停止」)")
    finally:
        for key in list(workers):
            kill_worker(key, "shutdown")
        reset_running_to_pending()
        if PROF.get("containers.manage", True):
            _shutdown_containers(env)
        RUNNER_PID_FILE.unlink(missing_ok=True)
    print_status()


def _shutdown_containers(env: dict) -> None:
    """收工:把还开着的靶场容器全部关掉(平台只允许 3 个,别占着不放)。"""
    if not hasattr(contest_api, "close_all"):
        return
    try:
        n = contest_api.close_all(env.get("TEAM_TOKEN", ""))
        if n:
            log(f"已关闭 {n} 个靶场容器")
    except Exception as e:
        log(f"关闭容器失败({type(e).__name__}: {e}),可在平台侧手动关闭")


if __name__ == "__main__":
    main()
