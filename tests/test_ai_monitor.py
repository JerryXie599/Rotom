#!/usr/bin/env python3
"""tools/ai_monitor.py 的离线测试(不联网、不调用模型)。

运行:
  python3 -m unittest tests/test_ai_monitor.py -v
  python3 -m unittest discover -s tests -p 'test_ai_monitor.py' -v
  python3 -m pytest tests/test_ai_monitor.py -q        # 装了 pytest 时

覆盖:token/usage 聚合、turn_end 去重、滞后半行、截断/原地重写/替换、
多 worker 与题目汇总、P50/P95、窗口速率、坏行容忍、状态跨实例持久化、
CLI --once 与 dashboard 约定的 ai_usage.json 顶层结构。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import ai_monitor as am  # noqa: E402

BASE = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def iso_ts(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def make_usage(inp: int = 100, out: int = 50, cache_read: int = 10,
               cache_write: int = 5, reasoning: int = 7,
               total: int | None = None, cost: dict | None = None) -> dict:
    if total is None:
        total = inp + out + cache_read + cache_write
    u = {
        "input": inp,
        "output": out,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "reasoning": reasoning,
        "totalTokens": total,
        "cost": cost if cost is not None else {
            "input": 0.01, "output": 0.02,
            "cacheRead": 0.001, "cacheWrite": 0.002, "total": 0.033,
        },
    }
    return u


def ev_session(ts: float) -> dict:
    return {"type": "session", "version": 3, "id": "s1",
            "timestamp": iso_ts(ts), "cwd": "/tmp/work"}


def ev_start(ts: float | None = None, role: str = "assistant",
             msg_ts: float | None = None) -> dict:
    ev = {"type": "message_start", "message": {"role": role, "content": []}}
    if ts is not None:
        ev["timestamp"] = iso_ts(ts)
    if msg_ts is not None:
        ev["message"]["timestamp"] = int(msg_ts * 1000)
    return ev


def ev_end(usage: dict, ts: float | None = None, role: str = "assistant",
           msg_ts: float | None = None, response_id: str = "r1") -> dict:
    ev = {
        "type": "message_end",
        "message": {
            "role": role,
            "content": [],
            "usage": usage,
            "stopReason": "toolUse",
            "responseId": response_id,
        },
    }
    if ts is not None:
        ev["timestamp"] = iso_ts(ts)
    if msg_ts is not None:
        ev["message"]["timestamp"] = int(msg_ts * 1000)
    return ev


def ev_turn_end(usage: dict, ts: float | None = None) -> dict:
    ev = {
        "type": "turn_end",
        "message": {"role": "assistant", "content": [], "usage": usage,
                    "stopReason": "toolUse"},
        "toolResults": [],
    }
    if ts is not None:
        ev["timestamp"] = iso_ts(ts)
    return ev


def ev_tool_start(name: str = "bash", call_id: str = "c1") -> dict:
    return {"type": "tool_execution_start", "toolCallId": call_id,
            "toolName": name, "args": {"command": "echo hi"}}


def ev_tool_end(name: str = "bash", call_id: str = "c1",
                is_error: bool = False) -> dict:
    return {"type": "tool_execution_end", "toolCallId": call_id,
            "toolName": name, "isError": is_error,
            "result": {"content": [{"type": "text", "text": "ok"}]}}


def ev_error(message: str = "boom") -> dict:
    return {"type": "error", "errorMessage": message}


def call_line(duration_ms: float, ttfb_ms: float | None = None,
              usage: dict | None = None, start_ms: float | None = None,
              status: int = 200, model: str = "deepseek-v4.1-flash",
              error: str = "") -> dict:
    start = int(start_ms if start_ms is not None else (BASE + 10) * 1000)
    end = start + int(round(duration_ms))
    return {
        "kind": "ai_call",
        "pid": 23711,
        "start_ms": start,
        "end_ms": end,
        "duration_ms": duration_ms,
        "ttfb_ms": ttfb_ms if ttfb_ms is not None else duration_ms,
        "status": status,
        "provider": "rotom",
        "api": "openai-completions",
        "model": model,
        "request_bytes": 5866,
        "usage": usage if usage is not None else make_usage(total=999),
        "error": error,
    }


def write_jsonl(path: Path, events: list[dict]) -> None:
    text = "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in events)
    path.write_text(text, encoding="utf-8")


class AiMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rotom-ai-monitor-")
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        self.logs = self.run_dir / "logs"
        self.logs.mkdir()
        os.environ.pop("ROTOM_RUN_DIR", None)

    def monitor(self, **kwargs) -> am.AiMonitor:
        return am.AiMonitor(self.run_dir, **kwargs)

    def one_call(self, usage: dict | None = None, start: float = BASE + 1,
                 end: float = BASE + 2.5) -> list[dict]:
        u = usage if usage is not None else make_usage()
        return [ev_start(start), ev_end(u, end)]

    # ------------------------------------------------------------------ 基础
    def test_basic_aggregation_and_turn_end_dedupe(self) -> None:
        path = self.logs / "alpha_w1_1700000000.jsonl"
        usage = make_usage()
        write_jsonl(path, [
            ev_session(BASE),
            ev_start(BASE + 1),
            ev_end(usage, BASE + 2.5),
            # turn_end 带同一份 usage,必须忽略
            ev_turn_end(usage, BASE + 2.5),
            ev_turn_end(usage, BASE + 2.5),
            ev_tool_start(),
            ev_tool_end(),
            ev_error("boom"),
        ])

        report = self.monitor().scan(now=BASE + 10)
        t = report["totals"]
        self.assertEqual(t["requests"], 1)
        self.assertEqual(t["tokens"], {
            "input": 100, "output": 50, "cache_read": 10,
            "cache_write": 5, "reasoning": 7, "total": 165,
        })
        self.assertAlmostEqual(t["cost"]["total"], 0.033, places=6)
        self.assertEqual(t["latency_ms"]["count"], 1)
        self.assertAlmostEqual(t["latency_ms"]["avg_ms"], 1500.0)
        self.assertAlmostEqual(t["latency_ms"]["p50_ms"], 1500.0)
        self.assertAlmostEqual(t["latency_ms"]["p95_ms"], 1500.0)
        self.assertAlmostEqual(t["latency_ms"]["max_ms"], 1500.0)
        self.assertEqual(t["latency_ms"]["unknown"], 0)
        self.assertEqual(t["thinking_time_ms"], 1500.0)
        self.assertEqual(t["tool_calls"], 1)
        self.assertEqual(t["tool_errors"], 0)
        self.assertEqual(t["error_events"], 1)
        self.assertEqual(t["bad_lines"], 0)
        self.assertEqual(t["active_workers"], 1)
        self.assertEqual(t["present_workers"], 1)
        self.assertAlmostEqual(t["duration_seconds"], 2.5, places=3)

        self.assertEqual(len(report["questions"]), 1)
        q = report["questions"][0]
        self.assertEqual(q["qid"], "alpha")
        self.assertEqual(q["requests"], 1)
        self.assertEqual(q["tokens"]["total"], 165)

        self.assertEqual(len(report["workers"]), 1)
        w = report["workers"][0]
        self.assertEqual(w["qid"], "alpha")
        self.assertEqual(w["slot"], 1)
        self.assertEqual(w["requests"], 1)
        self.assertEqual(w["tokens"]["total"], 165)

        lifetime = report["throughput"]["lifetime"]
        self.assertAlmostEqual(lifetime["tokens_per_minute"], 3960.0, places=3)
        self.assertAlmostEqual(lifetime["requests_per_minute"], 24.0, places=3)

        # dashboard 契约
        c = am.build_contract(report)
        for key in ("generated_at", "generated_at_local", "elapsed_s",
                    "active_workers", "totals", "rates", "latency",
                    "by_question", "by_worker"):
            self.assertIn(key, c)
        self.assertEqual(c["totals"]["requests"], 1)
        self.assertEqual(c["totals"]["total_tokens"], 165)
        self.assertEqual(c["totals"]["errors"], 1)
        self.assertEqual(c["active_workers"], 1)
        self.assertEqual(c["by_worker"]["logs/alpha_w1_1700000000.jsonl"]["requests"], 1)
        self.assertEqual(c["by_question"]["alpha"]["total_tokens"], 165)
        self.assertRegex(c["generated_at_local"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertTrue(str(
            c["by_worker"]["logs/alpha_w1_1700000000.jsonl"]["last_seen_utc"]
        ).endswith("Z"))
        self.assertAlmostEqual(c["latency"]["avg_ms"], 1500.0)

    def test_missing_timestamps(self) -> None:
        path = self.logs / "beta_w1_1700000000.jsonl"
        write_jsonl(path, [
            ev_start(None),
            ev_end(make_usage(), None),
        ])
        now = time.time()
        report = self.monitor().scan(now=now)
        t = report["totals"]
        self.assertEqual(t["requests"], 1)
        self.assertEqual(t["tokens"]["total"], 165)
        self.assertEqual(t["latency_ms"]["count"], 0)
        self.assertEqual(t["latency_ms"]["unknown"], 1)
        self.assertIsNone(t["latency_ms"]["avg_ms"])
        w = report["workers"][0]
        self.assertEqual(w["last_event_source"], "mtime")
        self.assertTrue(w["active"])
        c = am.build_contract(report)
        self.assertEqual(c["latency"]["avg_ms"], 0.0)
        self.assertEqual(c["latency"]["samples"], 0)
        self.assertEqual(c["latency"]["unknown"], 1)
        self.assertIsNotNone(
            c["by_worker"]["logs/beta_w1_1700000000.jsonl"]["last_seen"])

    def test_latency_percentiles(self) -> None:
        path = self.logs / "epsilon_w1_1700000000.jsonl"
        events: list[dict] = []
        for i, duration in enumerate((0.1, 0.2, 0.3, 0.4), start=1):
            events.append(ev_start(BASE + i))
            events.append(ev_end(make_usage(total=10), BASE + i + duration))
        write_jsonl(path, events)
        report = self.monitor().scan(now=BASE + 10)
        lat = report["totals"]["latency_ms"]
        self.assertEqual(lat["count"], 4)
        self.assertEqual(lat["unknown"], 0)
        self.assertAlmostEqual(lat["avg_ms"], 250.0, places=3)
        self.assertAlmostEqual(lat["p50_ms"], 200.0, places=3)
        self.assertAlmostEqual(lat["p95_ms"], 400.0, places=3)
        self.assertAlmostEqual(lat["max_ms"], 400.0, places=3)
        self.assertAlmostEqual(report["totals"]["thinking_time_ms"], 1000.0, places=3)
        self.assertEqual(report["totals"]["tokens"]["total"], 40)

    def test_usage_total_fallback_and_aliases(self) -> None:
        usage = {
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "cache_read": 3,
            "cache_write": 4,
            "reasoning_tokens": 5,
        }
        path = self.logs / "alias_w1_1700000000.jsonl"
        write_jsonl(path, [ev_start(BASE + 1), ev_end(usage, BASE + 2)])
        report = self.monitor().scan(now=BASE + 10)
        self.assertEqual(report["totals"]["tokens"], {
            "input": 11, "output": 22, "cache_read": 3,
            "cache_write": 4, "reasoning": 5, "total": 40,
        })

    # ------------------------------------------------------------ .calls 旁车
    def test_calls_sidecar_latency_preferred_and_usage_not_double_counted(self) -> None:
        main = self.logs / "alpha_w1_1700000000.jsonl"
        events: list[dict] = []
        for _ in range(3):
            events.append(ev_start(None))
            events.append(ev_end(make_usage(total=10), None))
        write_jsonl(main, events)

        calls_path = Path(str(main) + ".calls")
        calls = [
            call_line(1000, 900, make_usage(total=999), start_ms=(BASE + 10) * 1000),
            call_line(2000, 1900, make_usage(total=999), start_ms=(BASE + 20) * 1000),
            call_line(3000, 2900, make_usage(total=999), start_ms=(BASE + 30) * 1000),
        ]
        # 让 calls 的 usage 明显不同,证明主 jsonl 有 usage 时绝不双算
        for c in calls:
            c["usage"]["cost"] = {"total": 100.0}
        write_jsonl(calls_path, calls)

        report = self.monitor().scan(now=time.time())
        t = report["totals"]
        self.assertEqual(t["requests"], 3)
        self.assertEqual(t["tokens"]["total"], 30)       # 主 jsonl 的 usage
        self.assertEqual(t["cost"]["total"], 0.099)      # 0.033 * 3,不是 calls 的 300
        self.assertEqual(t["latency_ms"]["source"], "calls")
        self.assertEqual(t["latency_ms"]["count"], 3)
        self.assertEqual(t["latency_ms"]["unknown"], 0)
        self.assertAlmostEqual(t["latency_ms"]["avg_ms"], 2000.0)
        self.assertAlmostEqual(t["latency_ms"]["p50_ms"], 2000.0)
        self.assertAlmostEqual(t["latency_ms"]["p95_ms"], 3000.0)
        self.assertAlmostEqual(t["latency_ms"]["max_ms"], 3000.0)
        self.assertAlmostEqual(t["latency_ms"]["ttfb_ms"]["avg_ms"], 1900.0)
        self.assertAlmostEqual(t["latency_ms"]["ttfb_ms"]["p95_ms"], 2900.0)
        self.assertAlmostEqual(t["thinking_time_ms"], 6000.0)

        w = report["workers"][0]
        self.assertEqual(w["requests"], 3)
        self.assertEqual(w["tokens"]["total"], 30)
        self.assertEqual(w["usage_source"], "jsonl")
        self.assertEqual(w["latency_source"], "calls")
        self.assertEqual(w["calls"]["count"], 3)
        self.assertTrue(w["calls"]["present"])
        self.assertEqual(w["last_event_source"], "calls")

        c = am.build_contract(report)
        self.assertEqual(c["latency"]["source"], "calls")
        self.assertEqual(c["latency"]["samples"], 3)
        self.assertAlmostEqual(c["latency"]["avg_ms"], 2000.0)
        self.assertAlmostEqual(c["latency"]["p50_ms"], 2000.0)
        self.assertAlmostEqual(c["latency"]["p95_ms"], 3000.0)
        self.assertAlmostEqual(c["latency"]["ttfb_p95_ms"], 2900.0)
        bw = c["by_worker"]["logs/alpha_w1_1700000000.jsonl"]
        self.assertEqual(bw["latency_source"], "calls")
        self.assertEqual(bw["usage_source"], "jsonl")
        self.assertEqual(bw["calls_count"], 3)
        self.assertAlmostEqual(bw["avg_latency_ms"], 2000.0)

    def test_calls_usage_fallback_when_jsonl_has_no_usage(self) -> None:
        main = self.logs / "gamma_calls_w1_1700000000.jsonl"
        write_jsonl(main, [ev_session(BASE)])  # 没有任何 assistant message_end/usage
        calls_path = Path(str(main) + ".calls")

        def simple_usage(n: int) -> dict:
            return make_usage(inp=n, out=0, cache_read=0, cache_write=0,
                              reasoning=0, total=n, cost={"total": 0})
        write_jsonl(calls_path, [
            call_line(500, 450, simple_usage(10), start_ms=(BASE + 1) * 1000),
            call_line(1500, 1400, simple_usage(20), start_ms=(BASE + 2) * 1000),
        ])

        report = self.monitor().scan(now=BASE + 10)
        t = report["totals"]
        self.assertEqual(t["requests"], 2)          # 主日志没有,才用 calls 计数
        self.assertEqual(t["tokens"]["total"], 30)  # calls usage 兜底
        self.assertEqual(t["latency_ms"]["source"], "calls")
        self.assertAlmostEqual(t["latency_ms"]["avg_ms"], 1000.0)
        self.assertAlmostEqual(t["latency_ms"]["p50_ms"], 500.0)
        self.assertAlmostEqual(t["latency_ms"]["p95_ms"], 1500.0)

        w = report["workers"][0]
        self.assertEqual(w["usage_source"], "calls")
        self.assertEqual(w["requests_source"], "calls")
        self.assertEqual(w["tokens"]["total"], 30)

        c = am.build_contract(report)
        self.assertEqual(c["totals"]["requests"], 2)
        self.assertEqual(c["totals"]["total_tokens"], 30)
        bw = c["by_worker"]["logs/gamma_calls_w1_1700000000.jsonl"]
        self.assertEqual(bw["requests"], 2)
        self.assertEqual(bw["total_tokens"], 30)
        self.assertEqual(bw["usage_source"], "calls")
        self.assertEqual(bw["latency_source"], "calls")
        self.assertAlmostEqual(bw["avg_latency_ms"], 1000.0)

    def test_calls_file_appearing_after_first_scan(self) -> None:
        main = self.logs / "late_calls_w1_1700000000.jsonl"
        write_jsonl(main, [ev_start(None), ev_end(make_usage(total=10), None)])
        mon = self.monitor()
        r1 = mon.scan(now=BASE + 10)
        self.assertEqual(r1["totals"]["latency_ms"]["source"], "none")
        self.assertEqual(r1["totals"]["latency_ms"]["unknown"], 1)

        calls_path = Path(str(main) + ".calls")
        write_jsonl(calls_path, [
            call_line(1234, 1200, make_usage(total=999),
                      start_ms=(BASE + 10) * 1000),
        ])
        r2 = mon.scan(now=BASE + 20)
        self.assertEqual(r2["totals"]["tokens"]["total"], 10)  # 仍以主日志为准
        self.assertEqual(r2["totals"]["latency_ms"]["source"], "calls")
        self.assertEqual(r2["totals"]["latency_ms"]["unknown"], 0)
        self.assertAlmostEqual(r2["totals"]["latency_ms"]["avg_ms"], 1234.0)
        self.assertEqual(r2["totals"]["calls_count"], 1)

    def test_calls_partial_line_and_incremental_state(self) -> None:
        main = self.logs / "delta_calls_w1_1700000000.jsonl"
        write_jsonl(main, [ev_start(None), ev_end(make_usage(total=10), None)])
        calls_path = Path(str(main) + ".calls")
        line1 = json.dumps(call_line(1000, 900, make_usage(total=1))) + "\n"
        line2 = json.dumps(call_line(2000, 1800, make_usage(total=1))) + "\n"
        half = len(line2) // 2
        calls_path.write_text(line1 + line2[:half], encoding="utf-8")

        now = time.time()
        mon1 = self.monitor()
        r1 = mon1.scan(now=now)
        self.assertEqual(r1["workers"][0]["calls"]["count"], 1)
        self.assertEqual(r1["totals"]["latency_ms"]["count"], 1)
        self.assertEqual(r1["totals"]["tokens"]["total"], 10)  # 主日志 usage

        # 换实例:半行和 offset 必须从状态恢复
        mon2 = self.monitor()
        with open(calls_path, "a", encoding="utf-8") as f:
            f.write(line2[half:])
        r2 = mon2.scan(now=now + 5)
        self.assertEqual(r2["workers"][0]["calls"]["count"], 2)
        self.assertEqual(r2["totals"]["latency_ms"]["count"], 2)
        self.assertAlmostEqual(r2["totals"]["latency_ms"]["avg_ms"], 1500.0)
        self.assertAlmostEqual(r2["totals"]["latency_ms"]["p95_ms"], 2000.0)

        # .calls 被截断重写 -> 从头计数,不叠加
        calls_path.write_text(json.dumps(call_line(3000)) + "\n", encoding="utf-8")
        r3 = mon2.scan(now=now + 10)
        self.assertEqual(r3["workers"][0]["calls"]["count"], 1)
        self.assertAlmostEqual(r3["totals"]["latency_ms"]["avg_ms"], 3000.0)

    def test_calls_end_ms_counts_as_activity(self) -> None:
        main = self.logs / "epsilon_calls_w1_1700000000.jsonl"
        write_jsonl(main, [ev_start(BASE - 3600), ev_end(make_usage(), BASE - 3500)])
        calls_path = Path(str(main) + ".calls")
        write_jsonl(calls_path, [
            call_line(1000, 900, make_usage(total=10), start_ms=(BASE + 5) * 1000),
        ])
        # calls 文件 mtime 也是旧的,只有 end_ms 能证明活跃
        old = BASE - 3600
        os.utime(calls_path, (old, old))

        report = self.monitor().scan(now=BASE + 10)
        w = report["workers"][0]
        self.assertTrue(w["active"])
        self.assertEqual(w["last_event_source"], "calls")
        self.assertAlmostEqual(w["last_event_epoch"], BASE + 6, places=3)
        self.assertEqual(report["totals"]["active_workers"], 1)
        c = am.build_contract(report)
        self.assertTrue(c["by_worker"]["logs/epsilon_calls_w1_1700000000.jsonl"]["active"])

    # ------------------------------------------------------------ 半行/截断
    def test_partial_line_survives_new_monitor(self) -> None:
        path = self.logs / "gamma_w1_1700000000.jsonl"
        path.write_text(json.dumps(ev_start(BASE + 1)) + "\n", encoding="utf-8")

        mon1 = self.monitor()
        r1 = mon1.scan(now=BASE + 2)
        self.assertEqual(r1["totals"]["requests"], 0)
        self.assertEqual(r1["totals"]["pending_calls"], 1)

        end_line = json.dumps(ev_end(make_usage(), BASE + 2)) + "\n"
        half = len(end_line) // 2
        with open(path, "a", encoding="utf-8") as f:
            f.write(end_line[:half])

        # 换一个实例:状态文件里的 pending_start 必须恢复
        mon2 = self.monitor()
        r2 = mon2.scan(now=BASE + 3)
        self.assertEqual(r2["totals"]["requests"], 0)
        self.assertEqual(r2["totals"]["pending_calls"], 1)

        with open(path, "a", encoding="utf-8") as f:
            f.write(end_line[half:])
        r3 = mon2.scan(now=BASE + 4)
        self.assertEqual(r3["totals"]["requests"], 1)
        self.assertEqual(r3["totals"]["tokens"]["total"], 165)
        self.assertEqual(r3["totals"]["latency_ms"]["count"], 1)
        self.assertAlmostEqual(r3["totals"]["latency_ms"]["avg_ms"], 1000.0)

    def test_complete_json_without_trailing_newline_not_double_counted(self) -> None:
        path = self.logs / "gamma2_w1_1700000000.jsonl"
        text = (json.dumps(ev_start(BASE + 1)) + "\n" +
                json.dumps(ev_end(make_usage(), BASE + 2)))
        path.write_text(text, encoding="utf-8")  # 最后一行没有换行

        mon = self.monitor()
        r1 = mon.scan(now=BASE + 3)
        self.assertEqual(r1["totals"]["requests"], 1)
        r2 = mon.scan(now=BASE + 4)
        self.assertEqual(r2["totals"]["requests"], 1)

    def test_truncation_and_in_place_rewrite(self) -> None:
        path = self.logs / "delta_w1_1700000000.jsonl"
        write_jsonl(path, [ev_start(BASE + 1), ev_end(make_usage(total=165), BASE + 2)])
        mon = self.monitor()
        self.assertEqual(mon.scan(now=BASE + 10)["totals"]["tokens"]["total"], 165)

        # 原地重写为更短内容 -> 截断检测
        write_jsonl(path, [ev_start(BASE + 1), ev_end(make_usage(total=200), BASE + 2)])
        r2 = mon.scan(now=BASE + 11)
        self.assertEqual(r2["totals"]["tokens"]["total"], 200)
        self.assertEqual(r2["totals"]["requests"], 1)
        self.assertGreaterEqual(r2["scan"]["reset_files"], 1)

        # 原地重写为更长内容(大小 > 旧 offset)-> 文件头 hash 检测
        write_jsonl(path, [
            ev_start(BASE + 1), ev_end(make_usage(total=165), BASE + 2),
            ev_start(BASE + 3), ev_end(make_usage(total=300), BASE + 4),
        ])
        r3 = mon.scan(now=BASE + 12)
        self.assertEqual(r3["totals"]["tokens"]["total"], 465)
        self.assertEqual(r3["totals"]["requests"], 2)

        # 原子替换(inode 变化)-> 从头重读,不叠加旧值
        new_path = self.logs / ".delta.new"
        write_jsonl(new_path, [ev_start(BASE + 1), ev_end(make_usage(total=50), BASE + 2)])
        os.replace(new_path, path)
        r4 = mon.scan(now=BASE + 13)
        self.assertEqual(r4["totals"]["tokens"]["total"], 50)
        self.assertEqual(r4["totals"]["requests"], 1)

        state = json.loads((self.logs / ".ai_monitor_state.json").read_text(encoding="utf-8"))
        st = state["files"]["delta_w1_1700000000.jsonl"]
        self.assertEqual(st["offset"], path.stat().st_size)

    # ---------------------------------------------------------- 多 worker/qid
    def test_multi_worker_and_question_aggregation(self) -> None:
        write_jsonl(self.logs / "web_sql_injection_01_w1_1700000000.jsonl", [
            ev_start(BASE + 1), ev_end(make_usage(inp=60, out=40, cache_read=0,
                                                  cache_write=0, reasoning=1,
                                                  total=100), BASE + 2),
        ])
        write_jsonl(self.logs / "web_sql_injection_01_w2_1700000000.jsonl", [
            ev_start(BASE + 1), ev_end(make_usage(inp=120, out=80, cache_read=0,
                                                  cache_write=0, reasoning=2,
                                                  total=200), BASE + 3),
        ])
        write_jsonl(self.logs / "crypto_weak_token_06_w1_1700000000.jsonl", [
            ev_start(BASE + 1), ev_end(make_usage(inp=30, out=20, cache_read=0,
                                                  cache_write=0, reasoning=1,
                                                  total=50), BASE + 1.5),
        ])

        report = self.monitor().scan(now=BASE + 10)
        self.assertEqual(report["totals"]["requests"], 3)
        self.assertEqual(report["totals"]["tokens"]["total"], 350)

        qs = {q["qid"]: q for q in report["questions"]}
        self.assertEqual(qs["web_sql_injection_01"]["workers"], 2)
        self.assertEqual(qs["web_sql_injection_01"]["requests"], 2)
        self.assertEqual(qs["web_sql_injection_01"]["tokens"]["total"], 300)
        self.assertEqual(qs["crypto_weak_token_06"]["requests"], 1)
        self.assertEqual(qs["crypto_weak_token_06"]["tokens"]["total"], 50)

        slots = {(w["qid"], w["slot"]) for w in report["workers"]}
        self.assertEqual(slots, {
            ("web_sql_injection_01", 1),
            ("web_sql_injection_01", 2),
            ("crypto_weak_token_06", 1),
        })

        c = am.build_contract(report)
        self.assertEqual(c["by_question"]["web_sql_injection_01"]["requests"], 2)
        self.assertEqual(c["by_question"]["web_sql_injection_01"]["total_tokens"], 300)
        self.assertEqual(c["by_question"]["web_sql_injection_01"]["workers"], 2)
        self.assertIn("logs/web_sql_injection_01_w1_1700000000.jsonl", c["by_worker"])

    # ---------------------------------------------------------- 坏行 / 活跃
    def test_bad_lines_are_tolerated(self) -> None:
        path = self.logs / "zeta_w1_1700000000.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(ev_session(BASE)) + "\n")
            f.write("this is not json\n")
            f.write("[1, 2, 3]\n")
            f.write(json.dumps(ev_start(BASE + 1)) + "\n")
            f.write(json.dumps(ev_end(make_usage(), BASE + 2)) + "\n")
        report = self.monitor().scan(now=BASE + 10)
        self.assertEqual(report["totals"]["requests"], 1)
        self.assertEqual(report["totals"]["tokens"]["total"], 165)
        self.assertEqual(report["totals"]["bad_lines"], 2)

    def test_active_workers_use_event_time_when_available(self) -> None:
        write_jsonl(self.logs / "old_w1_1700000000.jsonl", [
            ev_start(BASE - 3600), ev_end(make_usage(), BASE - 3500),
        ])
        write_jsonl(self.logs / "new_w1_1700000000.jsonl", [
            ev_start(BASE + 1), ev_end(make_usage(), BASE + 2),
        ])
        report = self.monitor().scan(now=BASE + 10)
        self.assertEqual(report["totals"]["active_workers"], 1)
        by_qid = {w["qid"]: w for w in report["workers"]}
        self.assertFalse(by_qid["old"]["active"])
        self.assertTrue(by_qid["new"]["active"])
        self.assertEqual(by_qid["old"]["last_event_source"], "event")
        self.assertLess(by_qid["old"]["last_event_age_seconds"], 60 * 60)

        c = am.build_contract(report)
        self.assertFalse(c["by_worker"]["logs/old_w1_1700000000.jsonl"]["active"])
        self.assertTrue(c["by_worker"]["logs/new_w1_1700000000.jsonl"]["active"])

    # ---------------------------------------------------------- 增量/窗口
    def test_incremental_scan_and_window_rate(self) -> None:
        path = self.logs / "eta_w1_1700000000.jsonl"
        write_jsonl(path, [ev_start(BASE + 1), ev_end(make_usage(), BASE + 2)])
        mon = self.monitor()
        r1 = mon.scan(now=BASE + 10)
        self.assertEqual(r1["totals"]["requests"], 1)
        self.assertEqual(r1["scan"]["bytes_read"], path.stat().st_size)

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev_start(BASE + 11)) + "\n")
            f.write(json.dumps(ev_end(make_usage(), BASE + 12)) + "\n")
        r2 = mon.scan(now=BASE + 15)
        self.assertEqual(r2["totals"]["requests"], 2)
        self.assertEqual(r2["totals"]["tokens"]["total"], 330)
        self.assertEqual(r2["scan"]["changed_files"], 1)
        self.assertGreater(r2["scan"]["bytes_read"], 0)
        win = r2["throughput"]["window"]
        self.assertEqual(win["basis"], "observation")
        self.assertGreater(win["tokens_per_minute"], 0)
        self.assertGreater(win["requests_per_minute"], 0)

        r3 = mon.scan(now=BASE + 20)
        self.assertEqual(r3["totals"]["requests"], 2)
        self.assertEqual(r3["scan"]["changed_files"], 0)
        self.assertEqual(r3["scan"]["bytes_read"], 0)

        # 新实例从状态文件续扫,不重复计数
        mon2 = self.monitor()
        r4 = mon2.scan(now=BASE + 25)
        self.assertEqual(r4["totals"]["requests"], 2)
        self.assertEqual(r4["totals"]["tokens"]["total"], 330)
        self.assertEqual(r4["scan"]["bytes_read"], 0)

        c = am.build_contract(r4)
        self.assertEqual(c["totals"]["requests"], 2)
        self.assertEqual(c["rates"]["basis"], "observation")
        self.assertGreater(c["rates"]["tokens_per_min"], 0)

    # ---------------------------------------------------------- 空目录/契约
    def test_contract_has_all_keys_on_empty_run(self) -> None:
        report = self.monitor().scan(now=BASE + 10)
        c = am.build_contract(report)
        self.assertEqual(set(c), {
            "generated_at", "generated_at_local", "elapsed_s",
            "active_workers", "totals", "rates", "latency",
            "by_question", "by_worker",
        })
        self.assertEqual(set(c["totals"]), {
            "requests", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "reasoning_tokens", "total_tokens",
            "cost_total", "errors",
        })
        self.assertEqual(set(c["rates"]) >= {"tokens_per_min", "requests_per_min"}, True)
        self.assertEqual(set(c["latency"]) >= {"avg_ms", "p50_ms", "p95_ms", "max_ms"}, True)
        self.assertEqual(c["elapsed_s"], 0.0)
        self.assertEqual(c["active_workers"], 0)
        self.assertEqual(c["by_question"], {})
        self.assertEqual(c["by_worker"], {})
        self.assertEqual(c["rates"]["tokens_per_min"], 0.0)
        self.assertEqual(c["latency"]["avg_ms"], 0.0)
        self.assertEqual(c["latency"]["source"], "none")
        self.assertEqual(c["latency"]["samples"], 0)
        self.assertTrue(c["generated_at"] > 0)
        self.assertRegex(c["generated_at_local"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_ai_usage_json_files_written_atomically(self) -> None:
        write_jsonl(self.logs / "theta_w1_1700000000.jsonl", self.one_call())
        report = self.monitor().scan(write_outputs=True, now=BASE + 10)
        self.assertIsNotNone(report)
        json_path = self.logs / "ai_usage.json"
        md_path = self.logs / "ai_usage.md"
        state_path = self.logs / ".ai_monitor_state.json"
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        self.assertTrue(state_path.exists())
        data = json.loads(json_path.read_text(encoding="utf-8"))
        for key in ("generated_at", "generated_at_local", "elapsed_s",
                    "active_workers", "totals", "rates", "latency",
                    "by_question", "by_worker"):
            self.assertIn(key, data)
        self.assertEqual(data["totals"]["requests"], 1)
        self.assertEqual(data["by_worker"]["logs/theta_w1_1700000000.jsonl"]["qid"],
                         "theta")
        self.assertIn("AI 用量监控", md_path.read_text(encoding="utf-8"))
        # 没有遗留的临时文件
        self.assertEqual(list(self.logs.glob("*.tmp")), [])

    # ------------------------------------------------------------------ CLI
    def test_cli_once_json_and_summary(self) -> None:
        write_jsonl(self.logs / "iota_w1_1700000000.jsonl", [
            ev_session(BASE), ev_start(BASE + 1), ev_end(make_usage(), BASE + 2),
        ])
        script = ROOT / "tools" / "ai_monitor.py"
        env = dict(os.environ)
        env.pop("ROTOM_RUN_DIR", None)

        proc = subprocess.run(
            [sys.executable, str(script), "--once", "--json",
             "--run-dir", str(self.run_dir)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["totals"]["requests"], 1)
        self.assertEqual(data["totals"]["total_tokens"], 165)
        self.assertIn("logs/iota_w1_1700000000.jsonl", data["by_worker"])
        self.assertTrue((self.logs / "ai_usage.json").exists())
        self.assertTrue((self.logs / "ai_usage.md").exists())

        # 再跑一次 --once:从状态续扫,不能双算
        proc2 = subprocess.run(
            [sys.executable, str(script), "--once", "--json",
             "--run-dir", str(self.run_dir)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        data2 = json.loads(proc2.stdout)
        self.assertEqual(data2["totals"]["requests"], 1)

        proc3 = subprocess.run(
            [sys.executable, str(script), "--once",
             "--run-dir", str(self.run_dir)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        self.assertIn("AI 用量监控", proc3.stdout)
        self.assertIn("按题目", proc3.stdout)
        self.assertIn("按 worker", proc3.stdout)
        self.assertIn("iota", proc3.stdout)

    def test_cli_run_dir_env_fallback(self) -> None:
        write_jsonl(self.logs / "kappa_w1_1700000000.jsonl", self.one_call())
        script = ROOT / "tools" / "ai_monitor.py"
        env = dict(os.environ)
        env["ROTOM_RUN_DIR"] = str(self.run_dir)
        proc = subprocess.run(
            [sys.executable, str(script), "--once", "--json",
             "--no-write", "--reset-state"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["totals"]["requests"], 1)
        self.assertFalse((self.logs / "ai_usage.json").exists())


class TimestampAndUsageTest(unittest.TestCase):
    def test_parse_timestamp_formats(self) -> None:
        expected = datetime(2026, 9, 28, 10, 0, 1, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(am.parse_timestamp("2026-09-28T10:00:01Z"),
                               expected, places=3)
        self.assertAlmostEqual(am.parse_timestamp("2026-09-28T10:00:01.000+00:00"),
                               expected, places=3)
        self.assertAlmostEqual(am.parse_timestamp(expected), expected, places=3)
        self.assertAlmostEqual(am.parse_timestamp(expected * 1000), expected, places=3)
        self.assertAlmostEqual(am.parse_timestamp(str(expected * 1000)), expected, places=3)
        self.assertIsNone(am.parse_timestamp("not-a-time"))

    def test_normalize_usage_and_cost(self) -> None:
        u = am.normalize_usage({
            "input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4,
            "reasoning": 5, "totalTokens": 10,
        })
        self.assertEqual(u, {"input": 1, "output": 2, "cache_read": 3,
                             "cache_write": 4, "reasoning": 5, "total": 10})
        c = am.normalize_cost({"input": 1.5, "output": 0.5})
        self.assertEqual(c["total"], 2.0)
        self.assertEqual(am.normalize_cost(0.25)["total"], 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)