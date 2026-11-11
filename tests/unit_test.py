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
_want_mode, runner.STOP_MODE = runner.STOP_MODE, "window"      # 窗口制(比赛)才有"到点停派"
check(runner.window_closing(), "窗口制:剩 60s(≤STOP_SPAWN_BEFORE_END)应停止派新题")
runner.STOP_MODE = "work"                                       # 跑分包:任务驱动,不看时间
check(not runner.window_closing(), "任务驱动:即使只剩 60s 也不停派(终止条件只有那四种)")
runner.STOP_MODE = _want_mode
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

print("\n== 增援机制(子 agent 按需派发) ==")
import shutil, tempfile as _tf  # noqa: E402
_ws = Path(_tf.mkdtemp()) / "work" / "pwn-hard"
_ws.mkdir(parents=True)
os.environ["ROTOM_RUN_DIR"] = str(_ws.parent.parent)
importlib = __import__("importlib"); importlib.reload(solver)
q_help = {"question_id": "H1", "title": "hard", "category": "pwn", "workdir": "pwn-hard"}
(_ws / "SHARED.md").write_text(
    "- [w1 请求增援] 需要 2 个 agent:一个爆破 key 区间,一个扫 /admin\n"
    "- [w2 进展] 已确认溢出偏移\n", encoding="utf-8")
reqs = solver.read_helper_requests(q_help)
check(len(reqs) == 1 and "爆破" in reqs[0], f"能从黑板解析增援请求({len(reqs)} 条)")
p_help = solver.build_prompt({**q_help, "squad_task": reqs[0]}, 3, 4, _ws / "w3", attempt=2)
check("本次的专门分工是" in p_help, "增援 worker 的 prompt 带上了请求里的分工")
p_norm = solver.build_prompt(q_help, 1, 4, _ws / "w1", attempt=1)
check("本次的专门分工是" not in p_norm, "普通 worker 不带分工段")
check("请求增援" in p_norm and "timeout 60" in p_norm, "prompt 含增援格式与长命令限时提示")
# zhongce / pentest 模式特有的提示词约束(哪天改回 CTF 版,这几条会先炸,正好当护栏)
check("渗透测试工程师" in p_norm and "已授权" in p_norm, "prompt 是众测/渗透视角")
check("非破坏性" in p_norm and "只允许攻击上面给出的目标地址" in p_norm, "prompt 有授权范围与破坏性红线")
check("全部拿到才算通关" in p_norm, "prompt 说明一题多 flag 的通关条件")
check("禁止爆破 flag" in p_norm, "prompt 禁止猜/爆破 flag")
check("不要自己重启容器" in p_norm, "prompt 说明平台没有重置接口")
check("本方向打法速览" in p_norm and "暂无节选" not in p_norm, "prompt 里真的注入了该方向的 playbook")
check("众测 / 漏洞挖掘 playbook" in solver.category_playbook("pentest"),
      "众测类别(pentest)取到的是 pentest.md")
shutil.rmtree(_ws.parent.parent, ignore_errors=True)

print("\n== 网络策略(直连 / 走代理 / 断网) ==")
import config  # noqa: E402
_saved = {k: os.environ.pop(k, None) for k in ("NET_PROXY", "NETWORK_MODE")}
_proxy = {"http_proxy": "http://127.0.0.1:7877", "https_proxy": "http://127.0.0.1:7877",
          "all_proxy": "socks5h://127.0.0.1:7877"}
_p = dict(_proxy)
config.apply_net_policy(_p, {})
check(_p.get("no_proxy") == _p.get("NO_PROXY") == "*"
      and not (set(_p) & {"http_proxy", "https_proxy", "all_proxy"}),
      "默认直连:代理变量摘掉 + no_proxy=*(系统代理也被绕开,agent 自己的 requests 不再吃 502)")
_p = dict(_proxy)
config.apply_net_policy(_p, {"NET_PROXY": "system"})
check(_p == _proxy, "NET_PROXY=system:保留环境里的代理设置")
_p = dict(_proxy)
os.environ["NET_PROXY"] = "system"
config.apply_net_policy(_p, {})
check(_p == _proxy, "配置里没写 NET_PROXY 时回退读环境变量")
os.environ["NET_PROXY"] = "direct"
_p = dict(_proxy)
config.apply_net_policy(_p, {})
check(not (set(_p) & {"http_proxy", "https_proxy", "all_proxy"}) and _p.get("no_proxy") == "*",
      "环境变量 NET_PROXY=direct 同样生效(摘代理变量 + no_proxy=*)")
del os.environ["NET_PROXY"]
_p = dict(_proxy)
config.apply_net_policy(_p, {"NET_PROXY": "direct", "NETWORK_MODE": "local_only"})
check(bool(_p) and all(v == "http://127.0.0.1:9" for v in _p.values()), "断网模式:代理一律指向黑洞端口")
for _k, _v in _saved.items():
    if _v is not None:
        os.environ[_k] = _v

print("\n== 提交判定(限流不算答错) ==")
import contest_api  # noqa: E402
check(contest_api.is_verdict({"code": 0, "message": "恭喜您，回答正确"}), "code 0 = 平台已受理(判定)")
check(not contest_api.is_verdict({"code": 101, "message": "对不起，您的操作太过频繁！"}), "code 101 = 未受理")
check(contest_api.is_rate_limited({"code": 101, "message": "对不起，您的操作太过频繁！"}), "识别「操作太过频繁」为限流")
check(not contest_api.is_rate_limited({"code": 101, "message": "暂无队伍信息"}), "token 无效不是限流(同为 101,靠 message 区分)")
check(not contest_api.is_rate_limited({"code": 0, "message": "答案错误", "status": 0}), "答错不是限流")
check(contest_api.is_correct({"code": 0, "message": "答案正确，该题目已被攻克，不计分"}), "重复提交已攻克题仍算 CORRECT")

_calls = []
_real_submit = contest_api.submit_flag


def _fake_submit(token, qid, ans, timeout=30, **kw):
    _calls.append(ans)
    if len(_calls) <= 2:
        return {"code": 101, "message": "对不起，您的操作太过频繁！"}
    return {"code": 0, "message": "恭喜您，回答正确"}


contest_api.submit_flag = _fake_submit
_r = contest_api.submit_with_retry("t", "q", "flag{x}", attempts=5, base_delay=0.01)
check(len(_calls) == 3 and contest_api.is_correct(_r), f"限流自动退避重试直到受理(试了 {len(_calls)} 次)")

_calls.clear()


def _fake_wrong(token, qid, ans, timeout=30, **kw):
    _calls.append(ans)
    return {"code": 0, "message": "答案错误", "status": 0}


contest_api.submit_flag = _fake_wrong
_r = contest_api.submit_with_retry("t", "q", "flag{bad}", attempts=5, base_delay=0.01)
check(len(_calls) == 1 and not contest_api.is_correct(_r), "答错立即返回、不重试(不浪费提交次数)")
contest_api.submit_flag = _real_submit

print("\n== 失败分类不许被模型散文误伤(第三轮实测) ==")
_fp = "maybe the modulus n is not balanced; what if alpha and beta differ a lot"
check(solver.QUOTA_PATTERNS.search(_fp) is None, "散文里的 balanced 不再被当成 '余额耗尽'")
check(solver.RATE_LIMIT_PATTERNS.search("our capacity is limited") is None, "散文里的 capacity 不再被当成限流")
check(solver.RATE_LIMIT_PATTERNS.search("discuss concurrency limit design") is not None, "concurrency limit 仍算限流(真错误)")
check(solver.QUOTA_PATTERNS.search('{"error":{"message":"Insufficient Balance"}}') is not None, "真正的 Insufficient Balance 仍被判额度耗尽")
check(solver.QUOTA_PATTERNS.search("5-hour usage limit") is not None, "usage limit 仍被判额度耗尽")
check(solver.RATE_LIMIT_PATTERNS.search("Error 429: too many requests") is not None, "429/too many requests 仍被判限流")

print("\n== 失败分类的上下文闸门(第三轮实测:散文不许当错误) ==")
import json as _json
import tempfile as _tf2, pathlib as _pl2


def _cls(text):
    f = _pl2.Path(_tf2.mkdtemp()) / "w.jsonl"
    f.write_text(text, encoding="utf-8")
    return solver.classify_failure(f)


check(_cls("the internal app might have a rate limit or the flag rotates?") is None, "散文里的 'rate limit' 不算限流")
check(_cls("maybe the modulus n is not balanced; alpha and beta differ") is None, "散文里的 'not balanced' 不算额度")
# 2026-10-09 第九轮改契约:`classify_failure` 只分析**报错载体**(pi 的 error 事件 / isError 工具结果),
# 裸文本行一律不看 —— 因为模型自己的思考与报错写在**同一个 jsonl** 里。那次误判:模型写
# "rate limit. But we should avoid heavy brute force",附近恰好又有状态码 → 被判成"模型侧限流" → 并发降档 5→4。
_check_ev = lambda ev: _cls(_json.dumps(ev, ensure_ascii=False))
check(_check_ev({"type": "error", "errorMessage": "429 Too Many Requests"}) == "rate_limit",
      "pi error 事件里的 429 仍判限流")
check(_check_ev({"type": "error", "errorMessage": "Insufficient Balance"}) == "quota",
      "pi error 事件里的 Insufficient Balance 仍判额度")
check(_cls('{"error":{"message":"429 Too Many Requests"}}') is None,
      "**裸 JSON 行(无 type)不再参与判定**(旧契约作废)")
# ★ 第九轮那次真实假阳性的回归:模型散文写到 rate limit + 状态码,绝不许判成模型侧限制
check(_check_ev({"type": "message_end", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "rate limit. But we should avoid heavy brute force; WAF returns 500/502, 403 bypass"}]}}) is None,
    "★ 模型散文提 rate limit + 状态码 → 不算模型侧限制(第九轮误判的回归)")
check(_check_ev({"type": "message_end", "message": {"role": "toolResult", "isError": True, "content": [
    {"type": "text", "text": "HTTP/1.1 429 Too Many Requests"}]}}) == "rate_limit",
    "工具报错里的 HTTP 429 状态行仍判限流")
check(_check_ev({"type": "message_end", "message": {"role": "toolResult", "isError": False, "content": [
    {"type": "text", "text": "notes: rate limit 要控速, 500 次上限"}]}}) is None,
    "非报错的工具输出(正常回显)不算限流")
# 2026-10-28:pi 实际不会发 type=error,限流退出只有 agent_end(stopReason=error)。
# 旧契约漏了这一支 → 429 被当成"正常结束",满并发立刻重开,整轮都在撞限流(实测 270 次)。
check(_check_ev({"type": "agent_end", "messages": [
    {"role": "assistant", "content": [], "stopReason": "error",
     "errorMessage": "429 status code (no body)"}]}) == "rate_limit",
    "★ agent_end(stopReason=error) 的 429 → 判限流")
check(_check_ev({"type": "agent_end", "messages": [
    {"role": "assistant", "content": [], "stopReason": "error",
     "errorMessage": "5-hour usage limit reached"}]}) == "quota",
    "agent_end(stopReason=error) 的额度耗尽 → 判 quota")
check(_check_ev({"type": "agent_end", "messages": [
    {"role": "assistant", "content": [{"type": "text", "text": "rate limit noted, moving on"}],
     "stopReason": "stop"}]}) is None,
    "agent_end 正常收场里的散文 'rate limit' 仍不算限流(防误判回归)")

# ---------------------------------------------------------------- hint 策略(2026-10-26 起:agent 自主,自动注入默认关)
print("\n== hint 策略(默认 agent 自主)==")
_hard = {"question_id": "q_hard", "difficulty": "hard"}
_medium = {"question_id": "q_med", "difficulty": "medium"}
_easy = {"question_id": "q_easy", "difficulty": "easy"}
_none = {"question_id": "q_none"}
_ext = {"question_id": "q_ext", "extensions": {"difficulty": "hard"}}   # 难度只挂在 extensions 里

check(runner.HINT_AFTER_ATTEMPTS == 2 and not runner.HINT_EARLY_DIFFS,
      "默认:两个完整尝试都失败后才代取(排名差距全在 hint 扣分)")
check(runner.hint_threshold_for(_hard) == 2 and runner.hint_threshold_for(_medium) == 2
      and runner.hint_threshold_for(_easy) == -1,
      "默认:hard/medium 阈值 2,easy 永不代取")

# 恢复"自动注入"模式(2026-10-19 的分档)后仍按原逻辑工作 —— env 一开即回旧行为
runner.HINT_AFTER_ATTEMPTS, runner.HINT_EARLY_DIFFS, runner.HINT_SKIP_DIFFS = 1, {"hard", "medium"}, {"easy"}
check(runner.hint_threshold_for(_hard) == 0, "自动注入模式:hard → 首发即取(阈值 0)")
check(runner.hint_threshold_for(_medium) == 0, "自动注入模式:medium → 首发即取(阈值 0)")
check(runner.hint_threshold_for(_easy) == -1, "自动注入模式:easy → 永不代取(阈值 -1)")
check(runner.hint_threshold_for(_none) == 1, "自动注入模式:没写难度 → 回退 HINT_AFTER_ATTEMPTS(1)")
check(runner.hint_threshold_for(_ext) == 0, "自动注入模式:难度只写在 extensions 里也认")
runner.HINT_AFTER_ATTEMPTS = 0
check(runner.hint_threshold_for(_hard) == -1, "HINT_AFTER_ATTEMPTS=0 → 连 hard 也不取(全局关闭优先)")
runner.HINT_AFTER_ATTEMPTS, runner.HINT_EARLY_DIFFS, runner.HINT_SKIP_DIFFS = 1, {"hard"}, set()
check(runner.hint_threshold_for(_medium) == 1, "EARLY 只剩 hard 后 medium 回退阈值")
check(runner.hint_threshold_for(_easy) == 1, "SKIP 清空后 easy 也回退(旧行为可复原)")

_want_get = runner.contest_api.get_hint
_med2 = {"question_id": "q_med_api", "difficulty": "medium"}
with board_mod.locked() as _b:      # 提示要落到真实黑板条目上,先建一条
    _b["questions"].setdefault("q_med_api", {"question_id": "q_med_api", "status": "pending",
                                             "attempts": 0, "slices": 0})
runner.contest_api.get_hint = lambda tok, qid, timeout=30: "给点提示"
check(runner.hint_for(_med2) == "给点提示", "自动注入模式:medium 走 hint_for 正常取回")


def _boom(*a, **k):
    raise RuntimeError("缓存失效才会打到这里")


runner.contest_api.get_hint = _boom
check(runner.hint_for(_med2) == "给点提示", "取回后进进程内缓存(第二次不再打接口)")
runner.contest_api.get_hint = _want_get
runner.HINT_AFTER_ATTEMPTS, runner.HINT_EARLY_DIFFS, runner.HINT_SKIP_DIFFS = 0, set(), {"easy"}
with board_mod.locked() as _b:
    _facts = [f["text"] for f in _b.get("facts", [])]
check(any("q_med_api" in t and "平台提示" in t for t in _facts), "取到 hint 写进黑板 fact")
# 2026-10-28:平台按"每次查看"记事件(实测 3 条提示因重启重取变成 21 条),提示必须落黑板复用
with board_mod.locked() as _b:
    _entry = _b["questions"].get("q_med_api") or {}
check(str(_entry.get("hint_text") or "") == "给点提示", "取到的提示落进黑板 hint_text(重启后可复用)")
runner._hint_cache.clear()                     # 模拟进程重启(只剩黑板)
runner.HINT_AFTER_ATTEMPTS = 2                 # 阈值门在缓存之前,这里恢复默认门槛
runner.contest_api.get_hint = _boom            # 再打接口就会炸
check(runner.hint_for(dict(_med2)) == "给点提示", "★ 重启后从黑板复用提示,不再打平台接口(不重复扣分)")
runner.HINT_AFTER_ATTEMPTS = 0
runner.contest_api.get_hint = _want_get

# ---------------------------------------------------------------- 提示词里的 hint 块(2026-10-26:工具说明/重试强化)
print("\n== hint 块渲染 ==")
_wdir2 = Path(tempfile.mkdtemp()) / "w"
_wdir2.mkdir(parents=True, exist_ok=True)
_base_q = {"question_id": "q_tool", "title": "T", "category": "web", "description": "d",
           "flag_count": 1, "score": 300, "connection": {"url1": "http://1.2.3.4:1"}}
_p1 = solver.build_prompt(dict(_base_q), 1, 1, _wdir2, attempt=1)
_p3 = solver.build_prompt(dict(_base_q), 1, 1, _wdir2, attempt=3)
check("get_hint.py" in _p1 and "自取自用" in _p1, "首发:渲染工具说明(不强制)")
check("强烈建议先拿提示" not in _p1, "首发:没有'强烈建议'(留给 agent 自己判断)")
check("强烈建议先拿提示" in _p3, "第 3 次尝试:渲染'强烈建议先拿提示'再动手")
_q_auto = dict(_base_q); _q_auto["hint_text"] = "看看 /tmp"
_pa = solver.build_prompt(_q_auto, 1, 1, _wdir2, attempt=1)
check("harness 已替你取回" in _pa and "看看 /tmp" in _pa, "env 开自动注入时仍显示已取回的提示")

# ---------------------------------------------------------------- 知识库自动检索的查询/过滤(2026-10-28)
print("\n== 知识库自动检索 ==")
_qnoise = {"question_id": "xben-066-24", "title": "xben-066-24", "category": "pentest",
           "description": "\n\n(难度 hard · 关卡 1 · 满分 500 · 本题共 1 个 flag)"}
check(runner._kb_meaningful_query(_qnoise) == "", "只有题号+难度元信息 → 查询为空(不注入噪声)")
check(runner.kb_hint_for(dict(_qnoise)) == "", "查询为空时直接跳过检索(kb.py 不会被调用)")
_qreal = {"question_id": "q2", "title": "Simple Notes app", "category": "pentest",
          "description": "A note taking service with a secret note"}
_kw = runner._kb_meaningful_query(_qreal)
check("notes" in _kw.lower() and "xben" not in _kw.lower() and "难度" not in _kw,
      "有真实标题/描述 → 只留描述词")
_fake = ("知识库检索「x」命中 3 段:\n\n"
         "[  90] vendor/RsaCtfTool/src/RsaCtfTool/lib/system_primes.py » 素数\n    2^1536 ...\n\n"
         "[  44] vendor/PayloadsAllTheThings/Request Smuggling/README.md » CL.TE\n    transfer-encoding ...\n\n"
         "[  30] cards/http-request-smuggling.md » 检测\n    raw socket ...")
_keep = runner._kb_keep_blocks(_fake)
check("system_primes" not in _keep, "工具源码块(…/src/…)被滤掉")
check("http-request-smuggling" in _keep and "PayloadsAllTheThings" in _keep, "真正的资料块保留")
check(len(_keep.split("\n\n")) == 2, "只保留通过过滤的块")

# 派发公平性(2026-10-28:并发降到 1 后 066 独占槽位 2 小时,081/092 零派发)
print("\n== 派发顺序 ==")
_now_ts = time.time()
_ents = [
    {"question_id": "a", "status": "pending", "slices": 0, "last_started_at": _now_ts - 600, "level": 1},
    {"question_id": "b", "status": "pending", "slices": 0, "last_started_at": 0, "level": 1},
    {"question_id": "c", "status": "pending", "slices": 1, "last_started_at": 0, "level": 1},
]
_order = [e["question_id"] for e in runner._dispatch_order(_ents)]
check(_order == ["b", "a", "c"], "没跑过的先上 → 最久没被服务的次之 → 用满时间片的最后")
_tie = [{"question_id": q, "status": "pending", "slices": 0,
         "last_started_at": (0 if q == "066" else _now_ts - 30), "level": 1}
        for q in ("066", "081", "092")]
check(runner._dispatch_order(_tie)[0]["question_id"] == "066",
      "★ 三题全打平时,没跑过的题排在已跑过的前面(不再被列表首题永久霸占)")

print("\n== 并发回升 ==")
_cc = runner.Concurrency(3, 1, 60)
_cc.limit = 1
check(_cc.recover(60) is False, "限流刚发生 → 不回升")
check(_cc.recover(runner.RECOVER_AFTER) is True and _cc.limit == 2, "安静够久 → 并发 +1")
check(_cc.recover(runner.RECOVER_AFTER) is True and _cc.limit == 3, "再安静一轮 → 回到起始并发")
check(_cc.recover(runner.RECOVER_AFTER) is False, "已到起始并发 → 不再升")
_saved_rec = runner.RECOVER_AFTER
runner.RECOVER_AFTER = 0
_cc.limit = 1
check(_cc.recover(999999) is False, "RECOVER_AFTER=0 → 关闭回升")
runner.RECOVER_AFTER = _saved_rec

# 重启时把"停机时间"从单题预算里剔掉(2026-10-28:重启后 081/092 被白判一次 timeout)
print("\n== 重启的计时归零 ==")
_qs = {"a": {"attempt_started_at": 1000.0}, "b": {"attempt_started_at": 2000.0}, "c": {}}
check(runner._reset_idle_attempts(_qs, {"b"}) == 1, "只清没人在跑的题(在跑的保留计时)")
check(_qs["a"]["attempt_started_at"] == 0 and _qs["b"]["attempt_started_at"] == 2000.0,
      "清零的是空闲题,正在跑的那道不受影响")

# ---------------------------------------------------------------- 调度停滞判定(2026-10-25 第四轮教训)
print("\n== 调度停滞判定 ==")
check(bool(runner._stall_reason(1200, 1, 50)), "停 20 分钟 + 并发 1 + 50 未解 → 告警")
check(bool(runner._stall_reason(1200, 0, 50)), "并发 0 + 50 未解 + 停 20 分钟 → 也是停滞(更严重)")
check(runner._stall_reason(1200, 2, 50) == "", "并发 2(还在干活)→ 不告警")
check(runner._stall_reason(600, 1, 50) == "", "只停 10 分钟(未到阈值)→ 不告警")
check(runner._stall_reason(1200, 1, 3) == "", "未解只剩 3 道(收尾期)→ 不告警")
check(runner._stall_reason(1200, 1, 50, warn_s=0) == "", "STALL_WARN_S=0 → 整体关闭")
# 专注模式:列表外 100 多道 pending 不能参与统计,否则告警条件永远成立(2026-10-28 实跑误报)
_all_qs = {"q1": {"status": "pending"}, "q2": {"status": "pending"},
           "q3": {"status": "pending"}, "x1": {"status": "pending"},
           "x2": {"status": "solved"}}
_saved_focus = runner.FOCUS_QIDS
runner.FOCUS_QIDS = {"q1", "q2", "q3"}
check(len(runner._stall_scope(_all_qs)) == 3, "专注模式:只统计 FOCUS_QIDS 内的题")
runner.FOCUS_QIDS = set()
check(len(runner._stall_scope(_all_qs)) == 5, "非专注模式:统计全部题")
runner.FOCUS_QIDS = _saved_focus

# ---------------------------------------------------------------- 后台作业工具(job.py,2026-10-26)
print("\n== 后台作业工具(job.py) ==")
import subprocess as _sp
_jd = Path(tempfile.mkdtemp()) / "w1"
_jd.mkdir(parents=True)
(_jd / ".qid").write_text("q-job-test", encoding="utf-8")
_job = str(ROOT / "tools" / "job.py")
r = _sp.run([sys.executable, _job, "start", "echo hello-job; sleep 2; echo done", "--name", "t1"],
            cwd=str(_jd), capture_output=True, text=True, timeout=30)
check(r.returncode == 0 and "已启动后台作业" in (r.stdout or ""), "start 立即返回(不阻塞)")
r = _sp.run([sys.executable, _job, "list"], cwd=str(_jd), capture_output=True, text=True, timeout=30)
check("t1" in (r.stdout or ""), "list 能看到作业与状态")
r = _sp.run([sys.executable, _job, "start", "sleep 60", "--name", "t2"],
            cwd=str(_jd), capture_output=True, text=True, timeout=30)
r2 = _sp.run([sys.executable, _job, "stop", "t2"], cwd=str(_jd), capture_output=True, text=True, timeout=30)
check(r2.returncode == 0 and "已停止" in (r2.stdout or ""), "stop 能停掉整个进程组")
r = _sp.run([sys.executable, _job, "status", "t1", "--tail", "5"], cwd=str(_jd),
            capture_output=True, text=True, timeout=30)
check(r.returncode == 0 and "t1" in (r.stdout or ""), "status 能读日志尾部")
r = _sp.run([sys.executable, _job, "list"], cwd="/tmp", capture_output=True, text=True, timeout=30)
check(r.returncode == 2, "不在 w<N> 目录 → 拒绝(退出码 2)")

# ---------------------------------------------------------------- 跨轮口令库(合法投机,反作弊过滤)
print("\n== 跨轮口令库与变形族 ==")
import importlib
_ch = importlib.import_module("tools.cred_harvest")
check(not _ch.verdict("flag{abc-123}")[0], "拒收 flag 样式")
check(not _ch.verdict("550e8400-e29b-41d4-a716-446655440000")[0], "拒收 uuid(实例随机值)")
check(not _ch.verdict("xben-035-24")[0], "拒收含题号的值")
check(not _ch.verdict("deadbeefdeadbeefdeadbeef")[0], "拒收长十六进制(哈希/token)")
check(_ch.verdict("Admin@2024")[0] and _ch.verdict("Weaver@2023")[0], "收通用弱口令(含 @ 年份形态)")
_gw = importlib.import_module("tools.gen_weak_variants")
_leet = _gw.leet_variants("password")
check(any("@" in x or "4" in x or "0" in x for x in _leet), "weak-leet 能产出变形(P@ssword 类)")
_aff = _gw.affix_variants("Admin")
check("Admin@123" in _aff and "Admin2024" in _aff, "weak-affix 能产出 Admin@123 / Admin2024")
WL = ROOT / "knowledge" / "wordlists"
for _n in ("observed-passwords.txt", "weak-leet.txt", "weak-affix.txt"):
    _p = WL / _n
    _txt = _p.read_text(encoding="utf-8", errors="ignore") if _p.exists() else ""
    check(_p.exists() and "flag{" not in _txt and "xben-" not in _txt, f"{_n} 存在且无答案样式")

print("\n" + ("全部通过 ✔" if not FAILED else f"失败 {len(FAILED)} 项:"))
for f in FAILED:
    print("  -", f)
sys.exit(1 if FAILED else 0)
