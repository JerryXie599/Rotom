#!/usr/bin/env python3
"""提交 flag —— agent 在解题过程中通过 bash 调用本脚本。

用法:
  BENCHMARK_TOKEN=xxx python3 tools/submit_flag.py <question_id> "<flag>"

退出码与含义(提示词里也写了,agent 据此决定下一步):
  0  CORRECT       —— 这个 flag 是对的。
                      **众测平台一道题可能有多个 flag**:输出里会写明"本题还有 N 个 flag",
                      这时不能收工,要继续找下一个漏洞;只有 `本题通关 x/x` 才算真的做完。
  1  WRONG         —— 平台**判定答案不对**,才需要回去继续分析。
  2  用法错误
  3  SUBMIT_BUSY / SUBMIT_ERROR —— 平台**没受理**(靶场资源紧张/内部错误/网络)。
                    这不是答错:不要改 flag、不要重复提交,稍后重试即可。
                    **不要自己写重试循环**(实测有 agent 每 20s 重试烧掉几小时)——harness 会把它记进
                    `logs/pending_flags.jsonl`,通道恢复后由 `tools/backfill_flags.py` 补交一次。
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

# 适配器:环境变量 → .env 文件 → 空。**别只看 os.environ** —— 托管容器里 .env 可能没导出,
# 那样会静默回退到 CTF 适配器(旧域名),提交全废(第六次 Docker 测试实测)。
try:
    import config as _cfg
    _ADAPTER = _cfg.adapter_name()
except Exception:                                               # noqa: BLE001
    _ADAPTER = os.environ.get("CONTEST_ADAPTER", "").lower()
# 兜底:平台 token/base 在,但适配器没解析出来 → 按 tsec 走(宁可显式失败也别打错平台)
if not _ADAPTER and (os.environ.get("BENCHMARK_TOKEN") or os.environ.get("BENCHMARK_BASE_URL")):
    _ADAPTER = "tsec"
if _ADAPTER == "arena":
    import arena_api as contest_api      # 靶场模式
elif _ADAPTER == "tsec":
    import tsec_api as contest_api       # TSec Benchmark:一题多 flag
elif _ADAPTER == "newrank":
    import newrank_api as contest_api    # 2026 比武「新榜单」:一次性票据 + 直接 Web 入口
else:
    import contest_api  # noqa: E402
import board as board_mod  # noqa: E402

# 靶场/众测适配器缺某个接口时兜底回旧行为,保证切换适配器不会因为 AttributeError 直接挂掉。
_submit = getattr(contest_api, "submit_with_retry", None) or contest_api.submit_flag
_is_verdict = getattr(contest_api, "is_verdict", None) or (lambda r: True)
_is_rate_limited = getattr(contest_api, "is_rate_limited", None) or (lambda r: False)

# 日志必须落在**当前项目/靶场的数据目录**里,否则审计记录会散到全局 logs/
RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)
LOG_DIR = RUN_DIR / "logs"


def _from_env_file(key: str) -> str:
    """环境变量没设时回退读 .env —— 与 `config.adapter_name()` 同一口径。

    第六次 Docker 测试的根因是"worker 进程里 .env 没被导出":适配器那一半当时修了,token 这一半没有,
    于是提交工具会走到"未设置 token"——**显式失败比打错平台好,但整轮跑分照样白跑**。
    两边都回退读 `.env` 之后,这条根因就不成立了(入口导出坏掉也照常能提交)。
    """
    if os.environ.get(key):
        return ""
    try:
        return str(_cfg.read_env().get(key) or "")
    except Exception:                                           # noqa: BLE001
        return ""


def _token() -> str:
    if _ADAPTER == "arena":
        return os.environ.get("NSSCTF_AGENT_TOKEN", "") or contest_api.agent_token()
    return (os.environ.get("BENCHMARK_TOKEN") or os.environ.get("TEAM_TOKEN")
            or _from_env_file("BENCHMARK_TOKEN") or _from_env_file("TEAM_TOKEN") or "").strip()


def _record_progress(qid: str, resp: dict) -> tuple[int, int, bool]:
    """把一个正确的提交记进黑板,返回 (已拿到, 总数, 是否通关)。

    平台在 submit 响应里直接给了 correct_flag_count / total_flag_count / matched_flag_index,
    以它为准;拿不到计数(如 duplicate)就退回用 matched_flag_index 去重累加。
    """
    total = resp.get("total_flag_count")
    done = resp.get("correct_flag_count")
    idx = resp.get("matched_flag_index")
    with board_mod.locked() as b:
        e = b["questions"].get(qid)
        if e is None:                       # 平台侧还没同步到黑板(罕见),只记事实
            board_mod.add_fact(b, "submit", f"{qid} flag 命中(黑板暂无该题)")
            return (int(done or 0), int(total or 0), False)
        if idx is not None:
            found = {int(x) for x in (e.get("found_flags") or [])}
            found.add(int(idx))
            e["found_flags"] = sorted(found)
        e["stale_slices"] = 0          # 拿到新 flag → 链式安全阀计数清零(有产出的题不该被放走)
        if total is not None:
            e["flag_count"] = int(total or 1)
        if done is not None:
            e["correct_flag_count"] = int(done or 0)
        elif idx is not None:
            e["correct_flag_count"] = max(int(e.get("correct_flag_count") or 0),
                                          len(e.get("found_flags") or []))
        got = int(e.get("correct_flag_count") or 0)
        need = int(e.get("flag_count") or 1)
        complete = need > 0 and got >= need
        if complete and e["status"] != board_mod.STATUS_SOLVED:
            e["status"] = board_mod.STATUS_SOLVED
            e["finished_at"] = time.time()
            e["is_solved"] = True
            board_mod.add_fact(b, "submit", f"{qid} 全部 {need} 个 flag 已拿到,本题通关")
        return (got, need, complete)


def _pending_path() -> Path:
    return LOG_DIR / "pending_flags.jsonl"


def _remember_pending(qid: str, flag: str, reason: str, resp: dict) -> None:
    """平台**没受理**时把 (题号, flag) 记进待办,交给 harness 在通道恢复后补交一次。

    只记 agent 真的从目标读到、并**尝试提交过**的那个字符串(它自己传进来的);
    `WRONG`(平台受理了并判错)一律不记 —— 那是"回去重新提取"的信号,不是通道问题。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _pending_path()
    rows: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("question_id") == qid and e.get("flag") == flag:
                continue                       # 已有待办:不重复记,保留原 ts/attempts
            rows.append(e)
    rows.append({"ts": time.strftime("%F %T"), "question_id": qid, "flag": flag,
                 "reason": reason, "attempts": 0, "msg": str(resp.get("message") or resp)[:120]})
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def _forget_pending(qid: str, flag: str) -> None:
    """平台受理了这份提交(无论对错)→ 从待办里清掉,harness 不必再补。

    "受理但判错" 说明这个值不对(agent 取错了),补交只会重复一个已被否决的字符串 —— 直接丢。
    """
    path = _pending_path()
    if not path.exists():
        return
    keep: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            e = json.loads(s)
        except json.JSONDecodeError:
            continue
        if e.get("question_id") == qid and e.get("flag") == flag:
            continue
        keep.append(s)
    path.write_text(("\n".join(keep) + "\n") if keep else "", encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: submit_flag.py <question_id> <flag>", file=sys.stderr)
        return 2
    qid, flag = sys.argv[1], sys.argv[2].strip()
    token = _token()
    if not token:
        print("未设置 token(BENCHMARK_TOKEN / TEAM_TOKEN / NSSCTF_AGENT_TOKEN)", file=sys.stderr)
        return 2

    # 同串判错护栏(2026-10-11):这个 flag 之前交过且平台判了 WRONG → 本地直接拒绝,不打平台。
    # 背景:xben-071 的 SQLi 挖到的是**诱饵 flag**,黑板没记"已判错",轮换上来的新 agent 反复
    # 重交同一串(一场 5 次)。错误提交不扣分,但纯烧轮次、污染审计。
    try:
        with board_mod.locked() as b:
            _seen_wrong = list(((b["questions"].get(qid) or {}).get("wrong_flags") or []))
    except Exception:
        _seen_wrong = []
    if flag in _seen_wrong:
        print(f"REPEAT_WRONG question={qid} —— 这个 flag 之前已提交过且平台判 WRONG(诱饵值):"
              f"**不要重投同串**;回目标重新提取(核对来源与完整性),或换攻击面找真 flag。",
              file=sys.stderr)
        return 4

    try:
        resp = _submit(token, qid, flag)  # 平台没受理(资源紧张/抖动)会自动退避重试
    except Exception as e:  # 网络错误,让 agent 可以重试
        print(f"SUBMIT_ERROR question={qid} error={e}", file=sys.stderr)
        return 3

    accepted = _is_verdict(resp)                 # 平台受理了这份提交吗
    ok = accepted and contest_api.is_correct(resp)
    busy = (not accepted) and _is_rate_limited(resp)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "submissions.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.strftime("%F %T"), "question_id": qid,
            "flag": flag, "ok": ok, "accepted": accepted, "resp": resp,
        }, ensure_ascii=False) + "\n")

    if not accepted:
        # 平台没受理(资源紧张/业务错误):**不是答错**,不写 WRONG、不消耗"错误提交"额度,
        # 只提醒稍后重试。第二轮实测就吃过这个亏:限流被记成 WRONG,agent 会去重推 flag。
        # 同时记进待办 —— 万一 agent 的会话先结束,harness 会在通道恢复后补交一次
        # (第六次 Docker 测试:c-03 的 flag 值核对过却因通道坏掉一直交不上,agent 自己写重试
        #  循环烧掉了几小时;那次是适配器选错,已修,但"会话结束前通道没恢复"这种情况仍然需要兜一层)。
        why = "限流" if busy else "接口错误"
        _remember_pending(qid, flag, why, resp)
        with board_mod.locked() as b:
            board_mod.add_fact(b, "submit",
                               f"{qid} 提交未被平台受理({why}): {str(resp.get('message') or resp)[:60]}"
                               f" → 稍后重试即可(不是答错,别改 flag);harness 也会在通道恢复后补交一次")
        print(f"{'SUBMIT_BUSY' if busy else 'SUBMIT_ERROR'} question={qid} resp={resp}", file=sys.stderr)
        return 3

    _forget_pending(qid, flag)          # 受理了就不再需要 harness 兜底(对错都算有结论)

    if ok:
        got, need, complete = _record_progress(qid, resp)
        with board_mod.locked() as b:
            board_mod.add_fact(b, "submit", f"{qid} flag={flag!r} -> CORRECT ({got}/{need})")
        if need > 1 and not complete:
            print(f"CORRECT question={qid} 本题进度 {got}/{need} —— **还有 {need - got} 个 flag 没拿到,"
                  f"不要收工**:回到目标继续找别的漏洞(换攻击面/换接口),拿到下一个 flag 再提交。")
            print(f"CORRECT question={qid} flags={got}/{need} resp={resp}")
        else:
            print(f"CORRECT question={qid} 本题通关 {got}/{need} resp={resp}")
        return 0

    with board_mod.locked() as b:
        entry = b["questions"].get(qid)
        if entry is not None:               # 记住判错的串:再交同串会被上面护栏本地拒绝
            wf = entry.setdefault("wrong_flags", [])
            if flag not in wf:
                wf.append(flag)
                del wf[:-50]
        board_mod.add_fact(b, "submit", f"{qid} flag={flag!r} -> WRONG")
    print(f"WRONG question={qid} resp={resp}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
