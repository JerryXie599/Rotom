#!/usr/bin/env python3
"""已确证 flag 的补交(harness 侧,通道恢复后各补一次)

**为什么有这个东西**:第六次 Docker 测试里 c-03 的 flag 值 agent 用 `od -c` 逐字节核对过、
内容是对的,但**提交通道坏了**——agent 把它当成"平台侧故障",自己写了 `submit_loop.sh` 每 20s 重试,
把时间全烧光(那一轮 4h40m / 250+ 次提交全废,主因是选错适配器,已单独修)。
剩下的缺口是:**会话在通道恢复前结束**时,agent 手里那个已确证的值就白丢了。
harness 兜一层:它记下的就是 agent 已经提取到并尝试过的字符串,**不是猜**。

**边界(写死,不做例外)**:
- 只补 **agent 提交过、平台没受理**的值(它自己写进 `logs/pending_flags.jsonl` 的那些);
  `WRONG`(平台受理并判错)永远不会进待办,也永远不会被补交。
- 每条最多补 `BACKFILL_MAX_ATTEMPTS` 次(默认 3);仍不被受理就留着,下轮继续。
- 补交被受理但判错 → 判为"agent 取错了值",记事实后**永久丢弃**(不再占用下一次)。
- 绝不做 flag 变体/近似值/大小写改写 —— 那是"猜 flag",提示词里明确禁止,这里更不能破。

用法:
  python3 tools/backfill_flags.py            # 补一轮(查看待办 + 逐条投一次)
  python3 tools/backfill_flags.py --list     # 只看待办,不提交
  python3 tools/backfill_flags.py --dry-run  # 打印将要补交的内容,不真投
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))     # tools/,为了复用 submit_flag

MAX_ATTEMPTS = int(os.environ.get("BACKFILL_MAX_ATTEMPTS", "3") or 3)
MAX_ENTRIES = int(os.environ.get("BACKFILL_MAX_ENTRIES", "50") or 50)


def run_dir() -> Path:
    return Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)


def pending_path() -> Path:
    return run_dir() / "logs" / "pending_flags.jsonl"


def load_pending() -> list[dict]:
    p = pending_path()
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            e = json.loads(s)
        except json.JSONDecodeError:
            continue
        if e.get("question_id") and e.get("flag"):
            out.append(e)
    return out[:MAX_ENTRIES]


def save_pending(rows: list[dict]) -> None:
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n") if rows else "",
                 encoding="utf-8")


def run_once(dry_run: bool = False, log=print) -> dict:
    """补一轮。返回 {'tried','ok','wrong','busy','dropped'} 计数。

    只做一件事:对每条待办**原样**投一次平台接口,按平台回执分类处理。
    任何异常都不外抛(调用方是 runner 主循环,不能被这里搞挂)。
    """
    counts = {"tried": 0, "ok": 0, "wrong": 0, "busy": 0, "dropped": 0}
    rows = load_pending()
    if not rows:
        return counts

    try:
        import submit_flag as sf                      # 复用同一套适配器/token/判定口径
    except Exception as e:                            # noqa: BLE001
        log(f"[backfill] 无法加载提交工具({e}),本轮跳过")
        return counts

    token = sf._token()
    if not token:
        log("[backfill] 无 token,本轮跳过")
        return counts

    keep: list[dict] = []
    for e in rows:
        qid, flag = str(e["question_id"]), str(e["flag"])
        if e.get("attempts", 0) >= MAX_ATTEMPTS:
            log(f"[backfill] {qid} 已补 {e['attempts']} 次仍未被受理 → 放弃(留档:{flag[:12]}…)")
            counts["dropped"] += 1
            continue
        if dry_run:
            log(f"[backfill][dry-run] 将补交 {qid} flag={flag[:16]}…(第 {e.get('attempts', 0) + 1} 次)")
            keep.append(e)
            continue

        counts["tried"] += 1
        try:
            resp = sf._submit(token, qid, flag)       # 已是"同串原样投一次",不自造变体
        except Exception as ex:                       # noqa: BLE001  网络/超时 → 仍然没受理
            e["attempts"] = int(e.get("attempts", 0)) + 1
            e["last"] = time.strftime("%F %T")
            keep.append(e)
            counts["busy"] += 1
            log(f"[backfill] {qid} 补交异常({ex})→ 留着下轮再试")
            continue

        accepted = sf._is_verdict(resp)
        ok = accepted and sf.contest_api.is_correct(resp)
        # 与 agent 提交同一份日志格式,但标 by=harness —— 审计要能分清谁交的
        try:
            (run_dir() / "logs").mkdir(parents=True, exist_ok=True)
            with open(run_dir() / "logs" / "submissions.log", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%F %T"), "question_id": qid, "flag": flag,
                                    "ok": ok, "accepted": accepted, "by": "harness", "resp": resp},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

        if not accepted:
            e["attempts"] = int(e.get("attempts", 0)) + 1
            e["last"] = time.strftime("%F %T")
            keep.append(e)
            counts["busy"] += 1
            log(f"[backfill] {qid} 仍未被受理(第 {e['attempts']}/{MAX_ATTEMPTS} 次)→ 留着")
            continue

        if ok:
            counts["ok"] += 1
            try:
                got, need, _ = sf._record_progress(qid, resp)   # 与 agent 提交一样推进黑板
                msg = f"{qid} 补交成功 → CORRECT ({got}/{need})"
            except Exception:                                   # noqa: BLE001
                msg = f"{qid} 补交成功 → CORRECT"
            try:
                import board as board_mod
                with board_mod.locked() as b:
                    board_mod.add_fact(b, "submit", f"[harness 补交] flag={flag!r} -> CORRECT")
            except Exception:                                   # noqa: BLE001
                pass
            log(f"[backfill] {msg}")
        else:
            counts["wrong"] += 1
            try:
                import board as board_mod
                with board_mod.locked() as b:
                    board_mod.add_fact(b, "submit",
                                       f"[harness 补交] {qid} flag={flag!r} 被平台判错 → 丢弃(值取错了,别再交)")
            except Exception:                                   # noqa: BLE001
                pass
            log(f"[backfill] {qid} 补交被受理但判错 → 丢弃(agent 取错了值,不是通道问题)")

    save_pending(keep)
    return counts


def main() -> int:
    args = set(sys.argv[1:])
    rows = load_pending()
    if "--list" in args:
        if not rows:
            print("待办为空 ✓")
        for e in rows:
            print(f"  {e.get('ts')} {e['question_id']} attempts={e.get('attempts', 0)} "
                  f"reason={e.get('reason')} flag={str(e['flag'])[:20]}…")
        return 0
    if not rows:
        print("待办为空 ✓(没有需要补交的 flag)")
        return 0
    print(f"待办 {len(rows)} 条,开始补一轮(每条原样投一次,最多 {MAX_ATTEMPTS} 次):")
    c = run_once(dry_run="--dry-run" in args)
    print(f"结果:试用 {c['tried']} · 成功 {c['ok']} · 判错丢弃 {c['wrong']} · 仍未受理 {c['busy']} · 达上限放弃 {c['dropped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())