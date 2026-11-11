#!/usr/bin/env python3
"""跑分特化循环(离线,不联网不花 token)。

  python3 tests/bench_loop_test.py

盯住三件事:
① 循环以**任务**为界:终止条件只有四种(goal_done/platform_dead/net_gone/token_bad),**不以时间点为基准**;
② 跑分特化的调度:链式题钉住名额、单题预算按难度/链式分档、"单脑"(默认 1 个 agent/题);
③ 反作弊自检:知识库/提示词里不许出现题号、答案样式或"平台语义投机"的写法。
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


RUN_DIR = Path(tempfile.mkdtemp(prefix="rotom-bench-loop-"))
os.environ.update({"ROTOM_RUN_DIR": str(RUN_DIR), "PROFILE": "zhongce",
                   "BENCHMARK_TOKEN": "mock-token", "NET_GONE_STREAK": "3"})
os.environ.pop("CONTEST_ADAPTER", None)
(RUN_DIR / "logs").mkdir(parents=True, exist_ok=True)

import board as board_mod  # noqa: E402
import profile as pm  # noqa: E402
import runner  # noqa: E402

TOK = "mock-token"

# ------------------------------------------------------------------ 策略包
section("策略包:循环形状是数据(三种包各不同)")
zc, ctf, pt = pm.load("zhongce"), pm.load("ctf"), pm.load("pentest")
check("zhongce:任务驱动(work) + 四种终止条件",
      zc.loop_mode == "work" and zc.stop_conditions() == {"goal_done", "platform_dead", "net_gone", "token_bad"},
      f"{zc.loop_mode} {sorted(zc.stop_conditions())}")
check("ctf:仍是窗口制(比赛),但也认那四种终止",
      ctf.loop_mode == "window" and "goal_done" in ctf.stop_conditions())
check("pentest:任务驱动,只认 goal_done/net_gone(没有平台)",
      pt.loop_mode == "work" and pt.stop_conditions() == {"goal_done", "net_gone"})
check("zhongce:默认 1 个 agent/题(把'调度者=解题者'的收益拿过来)", zc.dispatch_int("workers_per_question", "X", 9) == 1)
check("zhongce:不给 worker 灌剩余时间(不以时间点催换面)", zc.prompt_time_budget is False)

# ------------------------------------------------------------------ 分档预算
section("单题预算按难度/链式分档(不再一刀切)")
check("easy → 1800s", zc.session_budget({"difficulty": "easy", "flag_count": 1}) == 1800)
check("hard → 7200s", zc.session_budget({"difficulty": "hard", "flag_count": 2}) == 7200)
check("链式(4 flag)→ 10800s,覆盖难度档", zc.session_budget({"difficulty": "easy", "flag_count": 4}) == 10800)
check("ctf 包没配预算 → 0(回退老逻辑)", ctf.session_budget({"difficulty": "hard", "flag_count": 4}) == 0)
runner.PROF, _old_prof = zc, runner.PROF
_slice_old = runner.SESSION_SLICE_SECONDS
try:
    runner.SESSION_SLICE_SECONDS = 0          # 先看完整预算语义(切片关)
    check("runner:worker_timeout_for 用策略包预算", runner.worker_timeout_for(1, {"difficulty": "easy"}) == 1800)
    t2 = runner.worker_timeout_for(2, {"difficulty": "hard"})
    check("runner:重试时在预算上加递增(不封顶老 MAX)", t2 > 7200, str(t2))
    # 短会话接力:切片封顶、按已用时间递减、到片判 handoff、预算耗尽后不再 handoff
    runner.SESSION_SLICE_SECONDS = 1200
    q_easy = {"difficulty": "easy", "attempt_started_at": time.time()}
    check("切片:单节封顶 1200s", runner.worker_timeout_for(1, q_easy) == 1200)
    check("切片:到片时仍判 handoff(预算 1800 未耗尽)", runner.session_handoff_for(1, q_easy))
    q_late = {"difficulty": "easy", "attempt_started_at": time.time() - 1700}
    check("切片:剩余不足一节时按剩余算", runner.worker_timeout_for(1, q_late) <= 100)
    check("切片:预算耗尽 → 不算 handoff(要走失败路径)", not runner.session_handoff_for(1, q_late))
    q_none = {"difficulty": "easy"}
    check("切片:没有 attempt_started_at 时从 0 起算", runner.worker_timeout_for(1, q_none) == 1200)
finally:
    runner.PROF = _old_prof
    runner.SESSION_SLICE_SECONDS = _slice_old

# ------------------------------------------------------------------ 终止条件
section("终止条件:只有四种,且不看时间")
check("work 模式下 window_closing() 永远为 False(即使设了截止时间)",
      runner.STOP_MODE == "work" and runner.window_closing() is False)
runner.RUN_DEADLINE = time.time() - 10          # 假装"时间早就过了"
check("时间已过点也不触发停止(不看时间)", runner.window_closing() is False)
runner.RUN_DEADLINE = 0

# goal_done:全部完成后收工并落盘原因
seed = {"questions": {
    "q1": {"question_id": "q1", "title": "a", "status": board_mod.STATUS_SOLVED, "flag_count": 1,
           "correct_flag_count": 1, "is_solved": True, "attempts": 1},
    "q2": {"question_id": "q2", "title": "b", "status": board_mod.STATUS_SOLVED, "flag_count": 2,
           "correct_flag_count": 2, "is_solved": True, "attempts": 1}},
    "facts": [], "started_at": time.time()}
(RUN_DIR / "board.json").write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
runner.write_stop_reason("goal_done", "测试:全部完成")
stop = json.loads((RUN_DIR / "logs" / "run_stop.json").read_text(encoding="utf-8"))
check("goal_done 落盘(含 solved/flags 快照)",
      stop["reason"] == "goal_done" and stop["solved"] == 2 and stop["flags"] == 3, json.dumps(stop, ensure_ascii=False)[:120])
check("落盘里写明允许的终止集合与循环模式",
      stop["stop_mode"] == "work" and "token_bad" in stop["allowed"])

# platform_dead / token_bad 的判别
class Err(Exception):
    def __init__(self, code, message, task_over=True):
        self.code, self.message, self.task_over = code, message, task_over


check("token 失效 → token_bad",
      runner._platform_stop_reason(Err("task_not_found", "token 无效")) == "token_bad")
check("比赛已结束 → platform_dead",
      runner._platform_stop_reason(Err("invalid_state", "任务已结束")) == "platform_dead")
check("中文'暂无队伍信息' → token_bad",
      runner._platform_stop_reason(Err("101", "暂无队伍信息")) == "token_bad")

# net_gone:连续失败计数
section("net_gone:连续失败才判(通了就清零)")
code = (
    'import os, sys, json, time\n'
    'sys.path.insert(0, os.environ["PYTHONPATH"])\n'
    'import runner\n'
    'print("STOP_MODE", runner.STOP_MODE, "| streak0", getattr(runner, "_net_fail_streak", 0))\n'
)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                   env={**os.environ, "PYTHONPATH": str(ROOT), "PROFILE": "zhongce"}, timeout=60)
check("子进程里 runner 也是 work 模式(不是窗口制)",
      "STOP_MODE work" in r.stdout, r.stdout + r.stderr[-200:])
check("net_gone 阈值可配(默认 3)", runner.NET_GONE_STREAK == 3)

# ------------------------------------------------------------------ 链式钉死
section("链式题钉死名额(不再被时间片轮换)")
runner.QUESTION_SLICE_SECONDS = 60
runner.ROTATE_COOLDOWN = 1
runner.MAX_ACTIVE_CHALLENGES = 3
runner.ROTATE_WAIT_RUNNING = False       # 排除"在跑保护"的干扰,只看钉死
runner.WORKER_MIN_LIFETIME = 0
runner.workers.clear()


def q(qid, **kw):
    base = {"question_id": qid, "title": qid, "status": board_mod.STATUS_RUNNING, "attempts": 1,
            "slices": 0, "max_attempts": 0, "next_retry_at": 0, "is_solved": False,
            "flag_count": 1, "correct_flag_count": 0, "rotate_until": 0,
            "slice_start": time.time() - 99999, "slice_flags": 0, "container_ready": True}
    base.update(kw)
    return base


seed_qs = [q("chain1", flag_count=4), q("q2"), q("q3"), q("q4", attempts=0, slice_start=0,
                                                     status=board_mod.STATUS_PENDING)]
(RUN_DIR / "board.json").write_text(json.dumps({"questions": {e["question_id"]: e for e in seed_qs},
                                                "facts": [], "started_at": time.time()},
                                               ensure_ascii=False), encoding="utf-8")
runner.rotate({})
board = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))
rotated = [k for k, e in board["questions"].items() if float(e.get("rotate_until") or 0) > 0]
check("链式题(4 flag)没被轮换", "chain1" not in rotated, str(rotated))
check("普通题照常轮换", len(rotated) >= 1, str(rotated))
check("黑板里留了'钉住名额'的记录",
      any("链式题" in f["text"] for f in board.get("facts", [])), str(board.get("facts"))[-200:])
runner.ROTATE_WAIT_RUNNING = True

# ------------------------------------------------------------------ 反作弊自检
section("反作弊自检:不许出现题号/答案样式/投机写法")
import re  # noqa: E402

BANNED_PATTERNS = [
    (re.compile(r"\b[abcef]\d?-\d{1,2}\b"), "跑分题号(如 a-16/b-02/e1-03 样式)"),
    (re.compile(r"23000|17400|7700"), "跑分分数字面量"),
    # 注意:提示词里"别提交占位 flag(如 flag{test})"是**反面示例**,不算;
    # 这里只抓"真的把某个具体 flag 写进提交命令/答案里"的写法。
    (re.compile(r"submit_flag\.py\s+\S+\s+'flag\{|answer\s*=\s*flag\{|答案是\s*flag\{"), "把具体 flag 写死提交"),
    (re.compile(r"已知答案|答案是|answer\s*=|直接提交这个"), "答案/投机式写法"),
    (re.compile(r"is_completed\s*=\s*true.*(改|伪造)|伪造\s*(进度|is_completed)"), "伪造平台状态"),
]
targets = list((ROOT / "knowledge" / "cards").glob("*.md")) + list((ROOT / "knowledge" / "cves").glob("*.md")) \
    + [ROOT / "knowledge" / "ops-bench.md"] \
    + list((ROOT / "prompts" / "zhongce").glob("*.md")) + [ROOT / "prompts" / "shared" / "system-ctf.md"]
hits = []
for f in targets:
    if not f.exists():
        continue
    text = f.read_text(encoding="utf-8", errors="ignore")
    for rx, why in BANNED_PATTERNS:
        for m in rx.finditer(text):
            hits.append(f"{f.relative_to(ROOT)}: {why} → {m.group(0)[:40]!r}")
check("知识卡/提示词里没有题号、分数、答案字样(不许针对跑分投机)", not hits, "; ".join(hits[:3]))
check("技术卡目录存在且有 INDEX", (ROOT / "knowledge" / "cards" / "INDEX.md").exists())
r = subprocess.run([sys.executable, str(ROOT / "tools" / "kb.py"), "hit", "jdwp"],
                   capture_output=True, text=True, cwd=str(ROOT), timeout=60)
check("kb.py hit 能按指纹命中技术卡", r.returncode == 0 and "jdwp-rce" in r.stdout, r.stdout[:120] + r.stderr[:120])
check("跑分 prompt 里写明了'平台故障不是你的打法错'与'别钻 VPN 兔子洞'",
      "平台侧" in (ROOT / "prompts" / "zhongce" / "prompt.md").read_text(encoding="utf-8")
      and "禁止**转进 VPN" in (ROOT / "prompts" / "zhongce" / "prompt.md").read_text(encoding="utf-8"))

# ------------------------------------------------------------------ 第三次测试教训(2026-09-28 zcode 跑分 93%)
section("提交假阴性同串重试(第三次测试实测:正确 flag 偶发 correct:false,重试后入账)")
import tsec_api  # noqa: E402

_calls = []


def _fake_submit(token, qid, ans, timeout=30):
    _calls.append(ans)
    idx = len(_calls)
    # 前 2 次"假阴性",第 3 次成功 —— 模拟平台最终一致性
    if idx <= 2:
        return {"code": 0, "correct": False, "correct_flag_count": 0, "total_flag_count": 1}
    return {"code": 0, "correct": True, "matched_flag_index": 0,
            "correct_flag_count": 1, "total_flag_count": 1}


_real = tsec_api.submit_flag
tsec_api.submit_flag = _fake_submit
tsec_api.FALSE_NEGATIVE_RETRIES = 2
try:
    r = tsec_api.submit_with_retry("t", "q1", "flag{same_string}")
finally:
    tsec_api.submit_flag = _real
check("假阴性被同串重试救回(3 次调用:2 次 false + 1 次 true)",
      len(_calls) == 3 and r.get("correct") is True and r.get("_false_negative_recovered") == 2,
      f"{len(_calls)} calls, resp={r}")
check("重试用的**完全是同一个字符串**(不是变体/猜测)", set(_calls) == {"flag{same_string}"}, str(set(_calls)))

_calls2 = []
tsec_api.submit_flag = lambda *a, **k: (_calls2.append(1), {"code": 0, "correct": False})[1]
tsec_api.FALSE_NEGATIVE_RETRIES = 2
try:
    r2 = tsec_api.submit_with_retry("t", "q1", "flag{wrong}")
finally:
    tsec_api.submit_flag = _real
check("真错仍然返回 false(重试次数用完后如实报错)",
      len(_calls2) == 3 and r2.get("correct") is False and r2.get("_false_negative_checked") == 2,
      f"{len(_calls2)} calls, resp={r2}")
tsec_api.FALSE_NEGATIVE_RETRIES = 0
_calls3 = []
tsec_api.submit_flag = lambda *a, **k: (_calls3.append(1), {"code": 0, "correct": False})[1]
try:
    tsec_api.submit_with_retry("t", "q1", "flag{x}")
finally:
    tsec_api.submit_flag = _real
check("FALSE_NEGATIVE_RETRIES=0 时关掉同串重试(只调 1 次)", len(_calls3) == 1, str(len(_calls3)))
tsec_api.FALSE_NEGATIVE_RETRIES = 2

section("跨轮战果种子(profile.kb_extra → worker 的 KB_EXTRA)")
import tempfile as _tf  # noqa: E402

seed = Path(_tf.mkdtemp(prefix="seed-"))
(seed / "b-02.md").write_text("# 上一轮 b-02 结论\nSSRF 入口 /proxy.php,凭据来自厂商字典\n", encoding="utf-8")
zc2 = pm.load("zhongce")
_orig = zc2.data.get("kb_extra")
zc2.data["kb_extra"] = [str(seed)]
try:
    dirs = zc2.kb_extra_dirs()
    check("profile.kb_extra 能解析成目录列表", dirs == [str(seed)], str(dirs))
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "kb.py"), "search", "SSRF 入口"],
                       capture_output=True, text=True, cwd=str(ROOT),
                       env={**os.environ, "KB_EXTRA": str(seed)}, timeout=60)
    check("KB_EXTRA 指向的上一轮结论能被 kb.py 检索到", r.returncode == 0 and "b-02" in r.stdout,
          r.stdout[:150] + r.stderr[:150])
finally:
    zc2.data["kb_extra"] = _orig or []

section("链式钉死的安全阀(不可能题不许占死名额)")
check("chain.max_attempts 默认 4", zc.chain_max_attempts() == 4, str(zc.chain_max_attempts()))
runner.QUESTION_SLICE_SECONDS = 60
runner.ROTATE_COOLDOWN = 1
runner.MAX_ACTIVE_CHALLENGES = 3
runner.ROTATE_WAIT_RUNNING = False
runner.workers.clear()
seed_qs2 = [q("chain1", flag_count=4, attempts=4, stale_slices=4), q("q2"), q("q3"),
            q("q4", attempts=0, slice_start=0, status=board_mod.STATUS_PENDING)]
(RUN_DIR / "board.json").write_text(json.dumps({"questions": {e["question_id"]: e for e in seed_qs2},
                                                "facts": [], "started_at": time.time()},
                                               ensure_ascii=False), encoding="utf-8")
runner.rotate({})
b2 = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))
rot2 = [k for k, e in b2["questions"].items() if float(e.get("rotate_until") or 0) > 0]
# 口径(2026-10-08 改):安全阀看 **stale_slices** —— 连着 4 个时间片零新 flag 才放走卡死的链式题。
# 原先看 `attempts`,而 attempts 会被 harness 自己的动作灌大(重启/增援/容器回收后重派),把正在出分的题误放走。
check("连 4 片零产出的链式题允许让位(不再钉死)", "chain1" in rot2, str(rot2))


section("收尾期自动增援(只剩少量题 + 并发位空着 → 给卡住的题加人)")
check("zhongce 开了收尾增援,并带分工简报",
      zc.endgame().get("enabled") is True and len(str(zc.endgame().get("brief") or "")) > 40,
      json.dumps(zc.endgame(), ensure_ascii=False)[:120])
check("ctf / pentest 不开(它们的并发策略不同)", not ctf.endgame().get("enabled")
      and not pt.endgame().get("enabled"))
runner.workers.clear()
_now = time.time()
fake_entries = [{"question_id": f"q{i}"} for i in range(4)]
runner.workers["q0#1"] = {"qid": "q0", "slot": 1, "started": _now - 3600, "proc": None}
check("全局未解 6 道(超过 unsolved_lte=5)→ 不加派", runner.endgame_targets(fake_entries, 4, _now, unsolved=6) == set())
runner.workers["q1#1"] = {"qid": "q1", "slot": 1, "started": _now - 3600, "proc": None}
runner._q_progress.clear()
tgt = runner.endgame_targets(fake_entries[:2], 3, _now, unsolved=4)
check("只剩 4 道(≤ unsolved_lte=5)、跑了 1 小时没进展 → 也加派(实测:剩 3 题时老阈值 2 永不触发)", tgt == {"q0", "q1"}, str(tgt))
runner._q_progress["q1"] = {"at": _now - 60}          # q1 一分钟前刚有进展
tgt2 = runner.endgame_targets(fake_entries[:2], 3, _now)
check("刚有进展的题不加派(只给真的在磨的那道)", tgt2 == {"q0"}, str(tgt2))
check("没有空闲并发位时不加派", runner.endgame_targets(fake_entries[:2], 0, _now) == set())
runner.workers.clear(); runner._q_progress.clear()

section("链式题钉名额有上限(别占满容器名额,给广度留位)")
runner.workers.clear(); runner._q_progress.clear()
_chain = [{"question_id": "chain1", "title": "c1", "flag_count": 4, "level": 1, "attempts": 1},
          {"question_id": "chain2", "title": "c2", "flag_count": 6, "level": 1, "attempts": 1},
          {"question_id": "chain3", "title": "c3", "flag_count": 4, "level": 2, "attempts": 1}]
_marked = [dict(e) for e in _chain]
held = runner._mark_holds(_marked, time.time())
check("3 道链式题只钉 1 个名额(其余让给广度)", len(held) == 1, str(held))
check("先钉 flag 最多的那道", "chain2" in held, str(held))
check("没被钉的题不会 hold_container(按普通题排队)",
      all(not e["hold_container"] for e in _marked if e["question_id"] == "chain3"),
      str([(e["question_id"], e["hold_container"]) for e in _marked]))
_old_rwr = runner.ROTATE_WAIT_RUNNING
try:
    runner.ROTATE_WAIT_RUNNING = True          # 前面的用例把它关过,这里显式打开(保护正在跑的题)
    runner.workers["chain3#1"] = {"qid": "chain3", "slot": 1, "started": time.time(), "proc": None}
    _chain2 = [dict(e, hold_container=None) for e in _chain]
    held2 = runner._mark_holds(_chain2, time.time())
    check("有 agent 在跑时照旧保住名额(保护正在干活的题)",
          _chain2[2]["hold_container"] is True and len(held2) == 1, str(held2))
finally:
    runner.ROTATE_WAIT_RUNNING = _old_rwr
    runner.workers.clear()
check("策略包给了上限值(zhongce=1:2/3 名额留给广度)", zc.chain_max_pinned_slots() == 1, str(zc.chain_max_pinned_slots()))

section("链式豁免在 rotate() 里也受限 + 收尾期判据用全局未解数(2026-10-08 第七轮实测回归)")
# 三个 bug 叠加导致那一轮 3/3 容器名额全被链式题钉死、60 道广度题一道没跑、flag 卡在 2/74:
#   ① rotate() 对**所有**链式题无条件豁免轮换(限额只在 _mark_holds 里,管不到这里);
#   ② 收尾期判据拿"此刻能派的题数"(被名额数卡在 ≤3)当"还剩几题未解" → 开跑第一分钟就误触发;
#   ③ 超配额的链式题在抢名额时排在广度题前面。
_old = (runner.QUESTION_SLICE_SECONDS, runner.ROTATE_COOLDOWN, runner.MAX_ACTIVE_CHALLENGES,
        runner.ROTATE_WAIT_RUNNING, dict(runner.workers))
try:
    runner.workers.clear()
    runner.QUESTION_SLICE_SECONDS, runner.ROTATE_COOLDOWN = 60, 1
    runner.MAX_ACTIVE_CHALLENGES, runner.ROTATE_WAIT_RUNNING = 3, False

    def _q(qid, **kw):
        return dict({"question_id": qid, "title": qid, "status": board_mod.STATUS_RUNNING,
                     "is_solved": False, "flag_count": 1, "level": 1, "attempts": 1,
                     "container_ready": True, "slice_start": time.time() - 999, "slice_flags": 0,
                     "correct_flag_count": 0, "rotate_until": 0}, **kw)

    seed3 = [_q("chainA", flag_count=4), _q("chainB", flag_count=6), _q("chainC", flag_count=4),
             _q("plain1", flag_count=1, difficulty="easy"),
             _q("fresh1", status=board_mod.STATUS_PENDING, attempts=0, slice_start=0,
                container_ready=False)]
    (RUN_DIR / "board.json").write_text(
        json.dumps({"questions": {e["question_id"]: e for e in seed3}, "facts": [],
                    "started_at": time.time()}, ensure_ascii=False), encoding="utf-8")
    runner.rotate({})
    b3 = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))["questions"]
    held3 = {k for k, e in b3.items() if float(e.get("rotate_until") or 0) == 0 and k.startswith("chain")}
    rotated3 = sorted(k for k, e in b3.items() if float(e.get("rotate_until") or 0) > 0)
    check("3 道链式题里只有 1 道被豁免轮换(其余照常让位)", len(held3) == 1, f"held={sorted(held3)}")
    check("被豁免的是 flag 最多的那道", held3 == {"chainB"}, f"held={sorted(held3)}")
    check("超配额的两道链式题都让出了名额", {"chainC","chainA"} <= set(rotated3), f"rotated={rotated3}")

    # 更狠的一种:超配额链式题的 worker **还在跑**(链式会话 3 小时)→ 也必须让位。
    # 否则 ROTATE_WAIT_RUNNING=1 的"有 worker 就不轮换"会把它变成永久占位(实测就是这个把名额锁死的)。
    runner.workers.clear()
    _now3 = time.time()
    for qid in ("chainA", "chainB", "chainC"):
        runner.workers[f"{qid}#1"] = {"qid": qid, "slot": 1, "started": _now3 - 3000, "proc": None}
    seed4 = [_q("chainA", flag_count=4), _q("chainB", flag_count=6), _q("chainC", flag_count=4),
             _q("fresh2", status=board_mod.STATUS_PENDING, attempts=0, slice_start=0,
                container_ready=False)]
    (RUN_DIR / "board.json").write_text(
        json.dumps({"questions": {e["question_id"]: e for e in seed4}, "facts": [],
                    "started_at": time.time()}, ensure_ascii=False), encoding="utf-8")
    _old_rwr2 = runner.ROTATE_WAIT_RUNNING
    runner.ROTATE_WAIT_RUNNING = True          # 打开"保护在跑的题"这个最容易出事的分支
    try:
        runner.rotate({})
    finally:
        runner.ROTATE_WAIT_RUNNING = _old_rwr2
    b4 = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))["questions"]
    rot4 = sorted(k for k, e in b4.items() if float(e.get("rotate_until") or 0) > 0)
    check("worker 还在跑的超配额链式题也让位(不让 3 小时长会话锁死名额)", {"chainA","chainC"} <= set(rot4), f"rotated={rot4}")
    runner.workers.clear()

    # 安全阀的口径:看 **stale_slices**(连续几个时间片零新 flag),不看 attempts。
    # attempts 会被 harness 自己的动作灌大(重启/收尾增援/容器回收后重派)——第七次实测 b-02 手里有 2/6 个 flag
    # 却被判 attempts=8 ≥ 4 → 摘掉钉名额身份 → 每 45 分钟被轮换、容器一关整条 webshell+socat 链清零,卡死在 2/6。
    def _mk(qid, fc=1, att=1, stale=0, status=None, ss=None):
        return {"question_id": qid, "title": qid, "status": status or board_mod.STATUS_RUNNING,
                "is_solved": False, "flag_count": fc, "level": 1, "attempts": att,
                "stale_slices": stale, "container_ready": True,
                "slice_start": (time.time() - 999) if ss is None else ss, "slice_flags": 0,
                "correct_flag_count": 0, "rotate_until": 0}

    def _rot(seed):
        (RUN_DIR / "board.json").write_text(
            json.dumps({"questions": {e["question_id"]: e for e in seed}, "facts": [],
                        "started_at": time.time()}, ensure_ascii=False), encoding="utf-8")
        runner.workers.clear()
        runner.rotate({})
        b = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))["questions"]
        return sorted(k for k, e in b.items() if float(e.get("rotate_until") or 0) > 0)

    _fresh = _mk("fresh", att=0, status=board_mod.STATUS_PENDING, ss=0)
    r1 = _rot([_mk("chainA", 4, 8, 0), _mk("chainB", 6, 8, 0), _mk("p1"), _mk("p2"), _fresh])
    check("attempts 被 harness 灌到 8 但有产出(stale_slices=0)→ 配额内那道仍被钉住",
          "chainB" not in r1, f"rotated={r1}")
    r2 = _rot([_mk("chainA", 4, 8, 4), _mk("chainB", 6, 8, 4), _mk("p1"), _mk("p2"), _fresh])
    check("连接 4 片零产出(stale_slices=4)→ 才允许放走链式题",
          {"chainA", "chainB"} <= set(r2), f"rotated={r2}")
    runner.workers.clear()

    # 安全阀必须对**被钉住(豁免)的**链式题也生效 —— 2026-10-08 第八轮实测踩到的洞:
    # 原先 exempt 分支直接 `continue`,安全阀与 stale_slices 累加全被跳过 → b-02 前 30 分钟拿到 2/6,
    # 之后钉着 1 个名额整整 5.5 小时零产出(同期广度 3.8 flag/槽时 vs 链式 1.5)。
    def _pk(qid, stale=0, flags=0, sflags=0):
        return {"question_id": qid, "title": qid, "status": board_mod.STATUS_RUNNING, "is_solved": False,
                "flag_count": 6, "level": 1, "attempts": 1, "stale_slices": stale, "container_ready": True,
                "slice_start": time.time() - 999, "slice_flags": sflags, "correct_flag_count": flags,
                "rotate_until": 0}

    def _run(seed):
        (RUN_DIR / "board.json").write_text(
            json.dumps({"questions": {e["question_id"]: e for e in seed}, "facts": [],
                        "started_at": time.time()}, ensure_ascii=False), encoding="utf-8")
        runner.workers.clear()
        runner.rotate({})
        b = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))["questions"]
        return b, sorted(k for k, e in b.items() if float(e.get("rotate_until") or 0) > 0)

    _f = _mk("fresh", att=0, status=board_mod.STATUS_PENDING, ss=0)
    bA, rA = _run([_pk("chainA", stale=3, flags=5, sflags=2), _mk("p1"), _mk("p2"), _f])
    check("被钉住但有产出 → 续期且 stale 归零(不让位)",
          bA["chainA"]["stale_slices"] == 0 and "chainA" not in rA, str(bA["chainA"]["stale_slices"]))
    bB, rB = _run([_pk("chainA", stale=3, flags=2, sflags=2), _mk("p1"), _mk("p2"), _f])
    check("被钉住且连着 4 片零产出 → 摘掉钉名额资格并让位", "chainA" in rB, f"rotated={rB}")
    bC, rC = _run([_pk("chainA", stale=1, flags=2, sflags=2), _mk("p1"), _mk("p2"), _f])
    check("被钉住、零产出但未到阈值 → 继续钉住(stale 累加到 2)",
          bC["chainA"]["stale_slices"] == 2 and "chainA" not in rC, str(bC["chainA"]["stale_slices"]))
    runner.workers.clear()

    # ★★ 关键回归(2026-10-09 第九轮实测踩到):被钉住的链式题**有在跑的 worker** +
    # ROTATE_WAIT_RUNNING=1(zhongce 默认)时,安全阀也必须生效。
    # 当时的 bug:`_protected()` 先判、`keep=(qid in exempt)` → 直接 `continue`,
    # 把 exempt 分支的时间片结算**短路成死代码** —— b-02 的 `slice_start` 72 分钟从没被结算、
    # `stale_slices` 恒为 0,安全阀形同不存在。上面的用例没 mock 在场 worker,所以没照出来。
    runner.workers.clear()
    _old_rwr3 = runner.ROTATE_WAIT_RUNNING
    runner.ROTATE_WAIT_RUNNING = True

    def _run_with_worker(seed, worker_on):
        (RUN_DIR / "board.json").write_text(
            json.dumps({"questions": {e["question_id"]: e for e in seed}, "facts": [],
                        "started_at": time.time()}, ensure_ascii=False), encoding="utf-8")
        runner.workers.clear()
        runner.workers[f"{worker_on}#1"] = {"qid": worker_on, "slot": 1,
                                            "started": time.time() - 3000, "proc": None}
        runner.rotate({})
        b = json.loads((RUN_DIR / "board.json").read_text(encoding="utf-8"))["questions"]
        return b, sorted(k for k, e in b.items() if float(e.get("rotate_until") or 0) > 0)

    try:
        _, rD = _run_with_worker([_pk("chainA", stale=3, flags=2, sflags=2), _mk("p1"), _mk("p2"), _f], "chainA")
        check("★ 被钉住 + worker 在跑 → 安全阀仍能触发(第九轮那个死代码的回归)",
              "chainA" in rD, f"rotated={rD}")
        bE, rE = _run_with_worker([_pk("chainA", stale=0, flags=5, sflags=2), _mk("p1"), _mk("p2"), _f], "chainA")
        check("★ 被钉住 + worker 在跑 + 本片有产出 → 续期、stale 归零、不让位",
              "chainA" not in rE and bE["chainA"]["stale_slices"] == 0,
              f"rotated={rE} stale={bE['chainA']['stale_slices']}")
    finally:
        runner.ROTATE_WAIT_RUNNING = _old_rwr3
        runner.workers.clear()

    # 收尾期判据:2 道"能派的题" + 60 道没解的 → 绝不该触发
    runner.workers.clear()
    _now2 = time.time()
    ents = [{"question_id": "x1"}, {"question_id": "x2"}]
    runner.workers["x1#1"] = {"qid": "x1", "slot": 1, "started": _now2 - 3600, "proc": None}
    runner._q_progress.clear()
    check("全局还有 60 道未解 → 收尾期不触发(哪怕此刻只有 2 道能派)",
          runner.endgame_targets(ents, 3, _now2, unsolved=60) == set())
    check("全局只剩 2 道未解 → 才触发增援",
          runner.endgame_targets(ents, 3, _now2, unsolved=2) == {"x1"})
    runner.workers.clear(); runner._q_progress.clear()

    # 抢名额:超配额的链式题要排在广度题之后
    import tsec_api as _api
    check("超配额链式题排到广度题之后(_rank 第二位)",
          _api._rank({"chain_beyond_cap": True, "slices": 0, "level": 1, "difficulty": "easy"}) >
          _api._rank({"slices": 0, "level": 1, "difficulty": "easy"}))
finally:
    (runner.QUESTION_SLICE_SECONDS, runner.ROTATE_COOLDOWN, runner.MAX_ACTIVE_CHALLENGES,
     runner.ROTATE_WAIT_RUNNING) = _old[:4]
    runner.workers.clear(); runner.workers.update(_old[4])

section("Observer 催促:判据从'步数'换成'产出'(长链路场景不刷屏)")
check("zhongce/pentest 用产出判据,ctf 保留步数判据",
      runner.OBS_MODE == "progress" and zc.observer_policy().get("mode") == "progress"
      and pt.observer_policy().get("mode") == "progress" and ctf.observer_policy().get("mode") == "steps",
      f"{runner.OBS_MODE}")
import tempfile as _tf  # noqa: E402
wdir = RUN_DIR / "work" / "obsq"; wdir.mkdir(parents=True, exist_ok=True)
(wdir / "SHARED.md").write_text("# board\n- [w1 计划] x\n", encoding="utf-8")
log = Path(_tf.mkdtemp(prefix="rotom-obs-")) / "w1.jsonl"


def _write_calls(commands: list[str]) -> None:
    with log.open("w", encoding="utf-8") as f:
        for c in commands:
            f.write(json.dumps({"type": "tool_execution_start", "toolName": "bash",
                                "args": {"command": c}}) + "\n")


def _seed_board(ready: bool) -> None:
    (RUN_DIR / "board.json").write_text(json.dumps(
        {"questions": {"obsq": {"question_id": "obsq", "workdir": "obsq", "status": board_mod.STATUS_RUNNING,
                                "flag_count": 1, "correct_flag_count": 0, "container_ready": ready,
                                "attempts": 1}}, "facts": [], "started_at": time.time()}, ensure_ascii=False),
        encoding="utf-8")


fake_w = {"qid": "obsq", "slot": 3, "proc": type("P", (), {"_agent_log_path": log})()}
runner._obs_lines.clear(); runner._q_progress.clear(); runner._obs_repeat.clear()
runner.OBS_REPEAT_SIG, runner.OBS_STALL_S, runner.OBS_COOLDOWN_S, runner.OBS_MAX_NUDGE = 3, 3600, 0, 3  # 冷却 0 = 允许连续判据
_seed_board(True)
_write_calls(["curl http://T/a"] * 30)                 # 30 步全是一样的命令 → 重复触发
ch1 = set(); runner._nudge_by_progress("obsq", fake_w, ch1)
lines = " ".join(runner._obs_lines.get("obsq", []))
check("反复跑同一条命令 → 催一次(提示换参数/换面)", bool(ch1) and "完全相同的命令" in lines, lines[:140])
ch2 = set(); runner._nudge_by_progress("obsq", fake_w, ch2)
check("同一信号不重复催(边沿触发)", not ch2, str(runner._obs_lines.get("obsq"))[:140])
runner._obs_lines.clear(); runner._q_progress.clear(); runner._obs_repeat.clear()
_seed_board(True)
_write_calls([f"curl http://T/{i}" for i in range(30)])  # 每条都不同 = 正常探测
ch3 = set(); runner._nudge_by_progress("obsq", fake_w, ch3)
check("30 步但每条命令都不同(正常探测)→ 不催(旧的步数判据会在这里催)", not ch3)
# 模拟"一小时没有任何新证据":把进度标记推回 1 小时前(签名与当前黑板一致,才不会被判成"有新结论")
runner.OBS_STALL_S = 900
runner._q_progress["obsq"] = {"flags": 0, "sig": runner._agent_lines_sig(wdir / "SHARED.md"),
                              "at": time.time() - 3600, "nudges": 0, "last": 0.0}
ch4 = set(); runner._nudge_by_progress("obsq", fake_w, ch4)
lines4 = " ".join(runner._obs_lines.get("obsq", []))
check("长期没有任何新证据 → 催一次(提示系统性证伪 + 换面)", bool(ch4) and "没有新进展" in lines4, lines4[:140])
_seed_board(False)                                      # 容器没就绪 = 环境问题
runner._obs_lines.clear(); runner._q_progress.clear(); runner._obs_repeat.clear()
ch5 = set(); runner._nudge_by_progress("obsq", fake_w, ch5)
check("容器不可用时不催 agent(该换容器,不是催它换思路)", not ch5 and not runner._obs_lines.get("obsq"))
runner.OBS_STALL_S, runner.OBS_COOLDOWN_S = 900, 900


print(f"\n{'='*50}\n通过 {PASS} 项,失败 {FAIL} 项")
if FAILED:
    print("失败项:")
    for f in FAILED:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)