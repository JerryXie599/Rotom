#!/usr/bin/env python3
"""Rotom AI 用量 / 计时监控(独立工具,不依赖 runner/dashboard/solver/board)。

监控对象
--------
Rotom 每个 worker 的 ``pi --mode json -p ...`` stdout 会落盘到::

    <ROTOM_RUN_DIR>/logs/<qid>_w<slot>_<ts>.jsonl

本工具只读这些 JSONL(以及自己的状态文件),做增量聚合:

  python3 tools/ai_monitor.py --once
  python3 tools/ai_monitor.py --watch --interval 5
  python3 tools/ai_monitor.py --once --run-dir /path/to/project
  python3 tools/ai_monitor.py --once --json          # 打印 JSON(便于脚本消费)

旁车计时数据(优先):
  solver 会给每个 worker 设置 ``ROTOM_AI_TRACE_FILE=<worker日志>.calls``,
  例如 ``logs/1_w1_1793112341.jsonl.calls``。每行一次模型调用,含
  ``duration_ms`` / ``ttfb_ms`` / ``start_ms`` / ``end_ms`` / ``usage``。
  它只用于计时;主 jsonl 完全没有 usage 时才用来兜底 token,不会双算。

输出(仅在 --watch / 默认 --once 写文件时):
  <ROTOM_RUN_DIR>/logs/ai_usage.json   dashboard 约定结构(见下方"JSON 契约")
  <ROTOM_RUN_DIR>/logs/ai_usage.md     人类可读摘要
  <ROTOM_RUN_DIR>/logs/.ai_monitor_state.json
                                       增量扫描状态(每个文件的 offset / 半行 / 累计值)

JSON 契约(ai_usage.json 顶层固定键)
-----------------------------------
  generated_at / generated_at_local / elapsed_s / active_workers /
  totals / rates / latency / by_question / by_worker
  - totals: requests,input_tokens,output_tokens,cache_read_tokens,
    cache_write_tokens,reasoning_tokens,total_tokens,cost_total,errors
  - rates: tokens_per_min,requests_per_min(窗口速率,首次扫描回退 lifetime)
  - latency: avg_ms,p50_ms,p95_ms,max_ms,samples,source,unknown,ttfb_*
  - by_question[qid]: requests,total_tokens,avg_latency_ms,elapsed_s + 明细
  - by_worker["logs/<file>"]: qid,slot,requests,total_tokens,avg_latency_ms,
    last_seen,active + 明细(含 latency_source / usage_source / calls_*)
  缺失数值给 0,缺失时间给 null,不省略键。

统计口径(重要)
--------------
* 一次模型调用 = 一条 assistant ``message_start`` 到对应的 assistant
  ``message_end``。请求数、token、成本都只按 **assistant message_end** 累加。
* ``turn_end`` 里会重复带一份相同 usage,因此 **不参与任何累加**(只可能在缺少
  逐行时间戳时用来更新时间;不会重复计数)。
* usage 字段:input / output / cacheRead / cacheWrite / reasoning / totalTokens。
  reasoning 通常是 output 的子集,不额外加进 total;totalTokens 缺失时才用
  input+output+cacheRead+cacheWrite 估算。
* 延迟优先取 ``.calls`` 的 ``duration_ms``(TTFB 单独统计 ``ttfb_ms``)。
  ``.calls`` 缺失时才回退主 jsonl:message_end 时间戳 - message_start 时间戳;
  时间戳优先顶层 ``timestamp``(ISO8601/epoch),其次 ``message.timestamp``。
  pi 有些版本给 start/end 写的是同一个 ``message.timestamp``,这时主日志无法
  测量,会记入 ``latency_ms.unknown`` 而不是伪造 0;输出里 ``latency.source``
  标明当前用的是 calls / jsonl / mixed / none。
* 活跃 worker = 最后活动时间在 60 秒内。证据取主日志事件时间 / mtime 与
  ``.calls`` 的 ``end_ms`` / mtime 的最大值;否则正在写的 worker 可能被误判为停摆。
* 窗口速率 = 监控自身每轮扫描观测到的增量,最近 ``--window-seconds``(默认 300s)
  的 token/分钟、请求/分钟;首次 --once 没有历史时回退到 lifetime 速率。

字段说明也写进了 ai_usage.json 的 ``field_docs``,避免 dashboard 端猜字段含义。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # macOS / Linux 自带 tzdata;没有时退化为固定 UTC+8
    from zoneinfo import ZoneInfo

    LOCAL_TZ: timezone | Any = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 仅极端环境
    LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_WINDOW_SECONDS = 300.0
ACTIVE_WINDOW_SECONDS = 60.0
CHUNK_SIZE = 1024 * 1024          # 每次最多读 1MB,绝不整文件进内存
HEAD_BYTES = 4096                 # 替换检测:只比对文件头
MAX_LINE_BYTES = 32 * 1024 * 1024  # 单行上限(防御性,正常 message_end 远小于此)
MAX_PENDING_STARTS = 4096
MAX_OBSERVATIONS = 5000
STATE_VERSION = 1

# 文件名:<qid>_w<slot>_<epoch>.jsonl(qid 里允许下划线,贪婪匹配)
_WORKER_RE = re.compile(r"^(?P<qid>.+)_w(?P<slot>\d+)(?:_(?P<ts>\d+))?\.jsonl$")
# 事件对象一般以 {"type":"..."} 开头;锚定开头,避免把 content 里的 "type" 误当事件类型
_TYPE_RE = re.compile(r'^\s*\{\s*"type"\s*:\s*"([^"]+)"')
_ROLE_RE = re.compile(r'"role"\s*:\s*"([^"]+)"')

# 不需要解析的事件:体积大且不含我们需要的信息。
# turn_end 的 usage 是 message_end 的重复,必须忽略,否则会双算。
_SKIP_TYPES = {"message_update", "tool_execution_update", "turn_end"}

_TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")
_COST_FIELDS = ("input", "output", "cache_read", "cache_write", "total")

FIELD_DOCS = {
    "generated_at": "扫描完成时刻的 Unix epoch 秒。",
    "generated_at_local": "扫描完成时刻的 Asia/Shanghai 本地时间(YYYY-MM-DD HH:MM:SS)。",
    "generated_at_utc": "扫描完成时刻的 UTC ISO8601 字符串(附加字段)。",
    "elapsed_s": "全程运行时长:最早 worker 开始 -> 最后活动,秒;无数据为 0。",
    "totals.requests": "assistant message_end 的条数(一次模型调用);turn_end 不重复计入。",
    "totals.*_tokens": "assistant message_end 的 usage 累加;reasoning 通常包含在 output 内,不重复计入 total。",
    "totals.total_tokens": "usage.totalTokens;缺失时按 input+output+cache_read+cache_write 估算。",
    "totals.errors": "type=error 的事件数。",
    "rates.tokens_per_min": "窗口内 token/分钟;首次扫描无观测历史时回退到 lifetime 速率。",
    "rates.requests_per_min": "窗口内请求/分钟;首次扫描无观测历史时回退到 lifetime 速率。",
    "latency": "assistant message_start -> message_end;无可用时间戳的样本不计入,缺失值为 0。",
    "latency.source": "calls=来自 <worker>.jsonl.calls 的 duration_ms;jsonl=回退主日志配对;mixed;none。",
    "latency.ttfb_*": "来自 .calls 的 ttfb_ms(首字节时间),缺失为 0。",
    "by_question": "按题目汇总;键为 qid,指标为其所有 worker 之和。",
    "by_worker": "按 worker 汇总;键为 logs/<文件名>,slot 未知时为 null。",
    "by_worker.usage_source": "jsonl=usage 来自主日志 assistant message_end;calls=主日志没有 usage 时用 .calls 兜底。",
    "by_worker.calls_*": ".calls 旁车文件的计时/兜底数据;计数与 usage 仍以主日志为准,不双算。",
    "thinking_time_ms": "可测量的模型调用延迟之和(毫秒)。",
    "cost": "provider 在 usage.cost 上报的估算成本;通常全为 0。",
    "throughput.lifetime": "总 token / 请求除以(最后活动时间 - 最早开始时间),不是模型调用耗时。",
    "throughput.window": "最近 window_seconds 秒的增量观测;首次扫描无历史时 basis=lifetime。",
    "active_workers": "最后活动在 60 秒内的 worker 数;无逐行 timestamp 时用文件 mtime。",
    "tool_calls": "tool_execution_start 事件数;tool_errors 为 tool_execution_end.isError=true 的次数。",
    "error_events": "type=error 的事件数。",
    "bad_lines": "无法解析的 JSONL 行数(不影响其它行)。",
    "pending_calls": "已看到 assistant message_start、尚未看到对应 message_end 的调用数。",
}


# --------------------------------------------------------------------------- #
# 时间 / 数字工具
# --------------------------------------------------------------------------- #
_MAX_EPOCH = 253402300799.0  # 9999-12-31T23:59:59Z,防止 datetime 溢出


def _valid_epoch(v: float) -> bool:
    return math.isfinite(v) and 0 < v < _MAX_EPOCH


def parse_timestamp(value: Any) -> float | None:
    """把 ISO8601 / epoch(秒、毫秒、微秒、纳秒)统一转成 Unix 秒。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v) or v <= 0:
            return None
        if v > 1e17:        # ns
            v /= 1e9
        elif v > 1e14:      # us
            v /= 1e6
        elif v > 1e11:      # ms
            v /= 1e3
        return v if _valid_epoch(v) else None
    s = str(value).strip()
    if not s:
        return None
    try:
        return parse_timestamp(float(s))
    except ValueError:
        pass
    t = s
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    return ts if _valid_epoch(ts) else None


def _epoch_dt(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    v = _as_float(ts, -1.0)
    if not _valid_epoch(v):
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def iso_utc(ts: float | None) -> str | None:
    dt = _epoch_dt(ts)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def local_str(ts: float | None) -> str | None:
    dt = _epoch_dt(ts)
    if dt is None:
        return None
    dt = dt.astimezone(LOCAL_TZ)
    return dt.isoformat(sep=" ", timespec="seconds")


def local_str_short(ts: float | None) -> str | None:
    """dashboard 契约里用的本地时间:YYYY-MM-DD HH:MM:SS(Asia/Shanghai,不带时区后缀)。"""
    dt = _epoch_dt(ts)
    if dt is None:
        return None
    dt = dt.astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_int_exact(v: Any, default: int = 0) -> int:
    """文件大小/mtime_ns/inode 用:不能先转 float,否则 1.79e18 会丢精度。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# usage / cost / 文件名
# --------------------------------------------------------------------------- #
def parse_worker_name(name: str) -> tuple[str, int | None, float | None]:
    """返回 (qid, slot, 文件名里的 spawn epoch)。不匹配时 qid=stem、slot=None。"""
    m = _WORKER_RE.match(name)
    if not m:
        stem = name[:-6] if name.endswith(".jsonl") else name
        return stem, None, None
    qid = m.group("qid")
    slot = int(m.group("slot"))
    spawn: float | None = None
    raw_ts = m.group("ts")
    if raw_ts:
        try:
            v = float(raw_ts)
            if v > 1e9:  # 只接受看起来像 Unix 秒的值
                spawn = v
        except ValueError:
            pass
    return qid, slot, spawn


def normalize_usage(raw: Any) -> dict[str, int]:
    """把 pi / 各 provider 的 usage 归一成 6 个整数字段(不含 reasoning 重复计算)。"""
    u = raw if isinstance(raw, dict) else {}

    def pick(*keys: str) -> int:
        for k in keys:
            if k in u and u[k] is not None:
                n = _as_int(u.get(k))
                if n:
                    return n
        return 0

    details = u.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    prompt_details = u.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}

    inp = pick("input", "prompt_tokens", "promptTokens")
    out = pick("output", "completion_tokens", "completionTokens")
    cache_read = pick("cacheRead", "cache_read", "cache_read_tokens", "cached_tokens")
    if not cache_read:
        cache_read = _as_int(prompt_details.get("cached_tokens"))
    cache_write = pick("cacheWrite", "cache_write", "cache_write_tokens")
    reasoning = pick("reasoning", "reasoning_tokens", "reasoningTokens")
    if not reasoning:
        reasoning = _as_int(details.get("reasoning_tokens"))
    total = pick("totalTokens", "total_tokens", "total")
    if not total:
        total = inp + out + cache_read + cache_write
    return {
        "input": inp,
        "output": out,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "reasoning": reasoning,
        "total": total,
    }


def normalize_cost(raw: Any) -> dict[str, float]:
    """usage.cost 归一化;允许数值或 {input,output,cacheRead,cacheWrite,total}。"""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        total = _as_float(raw)
        return {"input": 0.0, "output": 0.0, "cache_read": 0.0,
                "cache_write": 0.0, "total": round(total, 6)}
    c = raw if isinstance(raw, dict) else {}

    def pick(*keys: str) -> float:
        for k in keys:
            if k in c and c[k] is not None:
                return _as_float(c.get(k))
        return 0.0

    inp = pick("input")
    out = pick("output")
    cr = pick("cacheRead", "cache_read")
    cw = pick("cacheWrite", "cache_write")
    total = pick("total")
    if not total:
        total = inp + out + cr + cw
    return {
        "input": round(inp, 6),
        "output": round(out, 6),
        "cache_read": round(cr, 6),
        "cache_write": round(cw, 6),
        "total": round(total, 6),
    }


def _event_clock(ev: dict, msg: dict | None = None) -> tuple[float | None, str | None]:
    """返回 (epoch 秒, 来源)。顶层 timestamp 优先,其次 message.timestamp。"""
    if "timestamp" in ev:
        ts = parse_timestamp(ev.get("timestamp"))
        if ts is not None:
            return ts, "event"
    if msg is None:
        m = ev.get("message")
        msg = m if isinstance(m, dict) else None
    if msg is not None and "timestamp" in msg:
        ts = parse_timestamp(msg.get("timestamp"))
        if ts is not None:
            return ts, "message"
    return None, None


# --------------------------------------------------------------------------- #
# 状态结构
# --------------------------------------------------------------------------- #
def _zero_tokens() -> dict[str, int]:
    return {k: 0 for k in _TOKEN_FIELDS}


def _zero_cost() -> dict[str, float]:
    return {k: 0.0 for k in _COST_FIELDS}


def _new_calls_state() -> dict:
    """一个 worker 的 .calls 旁车文件状态(只用于计时;usage 仅兜底)。"""
    return {
        "present": False,
        "offset": 0,
        "size": 0,
        "mtime_ns": 0,
        "inode": 0,
        "head_sha1": "",
        "partial_b64": "",
        "skip_until_newline": False,
        "count": 0,
        "bad_lines": 0,
        "duration_count": 0,
        "duration_sum_ms": 0.0,
        "duration_max_ms": 0.0,
        "duration_histogram": {},
        "ttfb_count": 0,
        "ttfb_sum_ms": 0.0,
        "ttfb_max_ms": 0.0,
        "ttfb_histogram": {},
        "usage": _zero_tokens(),
        "cost": _zero_cost(),
        "first_start_ts": None,
        "last_end_ts": None,
        "errors": 0,
        "last_error": "",
        "models": {},
    }


def _ensure_calls_state(raw: Any) -> dict:
    c = raw if isinstance(raw, dict) else {}
    base = _new_calls_state()
    for key, default in base.items():
        if key not in c:
            c[key] = default
    for key in ("offset", "size", "mtime_ns", "inode"):
        c[key] = _as_int_exact(c.get(key))
    for key in ("count", "bad_lines", "duration_count", "ttfb_count", "errors"):
        c[key] = _as_int(c.get(key))
    for key in ("duration_sum_ms", "duration_max_ms",
                "ttfb_sum_ms", "ttfb_max_ms"):
        c[key] = round(_as_float(c.get(key)), 3)
    for key in ("first_start_ts", "last_end_ts"):
        c[key] = _as_float(c[key]) if c.get(key) is not None else None
    c["partial_b64"] = str(c.get("partial_b64") or "")
    c["skip_until_newline"] = bool(c.get("skip_until_newline"))
    c["head_sha1"] = str(c.get("head_sha1") or "")

    usage = c.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    for k in _TOKEN_FIELDS:
        usage[k] = _as_int(usage.get(k))
    c["usage"] = usage

    cost = c.get("cost")
    cost = cost if isinstance(cost, dict) else {}
    for k in _COST_FIELDS:
        cost[k] = round(_as_float(cost.get(k)), 6)
    c["cost"] = cost

    for hist_key in ("duration_histogram", "ttfb_histogram"):
        hist = c.get(hist_key)
        hist = hist if isinstance(hist, dict) else {}
        c[hist_key] = {
            str(_as_int(k)): _as_int(v)
            for k, v in hist.items() if _as_int(v)
        }
    models = c.get("models")
    models = models if isinstance(models, dict) else {}
    c["models"] = {str(k): _as_int(v) for k, v in models.items() if _as_int(v)}
    c["last_error"] = str(c.get("last_error") or "")[:300]
    return c


def _new_file_state(name: str, now: float) -> dict:
    qid, slot, spawn = parse_worker_name(name)
    return {
        "file": name,
        "qid": qid,
        "slot": slot,
        "spawn_ts": spawn,
        "present": True,
        "offset": 0,
        "size": 0,
        "mtime_ns": 0,
        "inode": 0,
        "head_sha1": "",
        "partial_b64": "",
        "skip_until_newline": False,
        "started_ts": None,
        "first_ts": None,
        "last_ts": None,
        "last_line_ts": None,
        "line_ts_count": 0,
        "first_seen": now,
        "last_read": now,
        "requests": 0,
        "tokens": _zero_tokens(),
        "cost": _zero_cost(),
        "latency": {"count": 0, "unknown": 0, "sum_ms": 0.0,
                    "max_ms": 0.0, "histogram": {}},
        "pending_starts": [],
        "tool_calls": 0,
        "tool_errors": 0,
        "tool_names": {},
        "error_events": 0,
        "bad_lines": 0,
        "last_error": "",
        "calls": _new_calls_state(),
    }


def _ensure_file_state(st: dict) -> dict:
    """补齐/纠正从 JSON 读回来的状态(兼容手工编辑或旧版本缺字段)。"""
    st.setdefault("present", False)
    for key in ("offset", "size", "mtime_ns", "inode"):
        st[key] = _as_int_exact(st.get(key))
    for key in ("line_ts_count", "requests", "tool_calls", "tool_errors",
                "error_events", "bad_lines"):
        st[key] = _as_int(st.get(key))
    for key in ("started_ts", "first_ts", "last_ts", "last_line_ts",
                "first_seen", "last_read", "spawn_ts"):
        st[key] = _as_float(st[key]) if st.get(key) is not None else None
    st["partial_b64"] = str(st.get("partial_b64") or "")
    st["skip_until_newline"] = bool(st.get("skip_until_newline"))
    st["head_sha1"] = str(st.get("head_sha1") or "")

    tokens = st.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    for k in _TOKEN_FIELDS:
        tokens[k] = _as_int(tokens.get(k))
    st["tokens"] = tokens

    cost = st.get("cost")
    cost = cost if isinstance(cost, dict) else {}
    for k in _COST_FIELDS:
        cost[k] = round(_as_float(cost.get(k)), 6)
    st["cost"] = cost

    lat = st.get("latency")
    lat = lat if isinstance(lat, dict) else {}
    lat["count"] = _as_int(lat.get("count"))
    lat["unknown"] = _as_int(lat.get("unknown"))
    lat["sum_ms"] = round(_as_float(lat.get("sum_ms")), 3)
    lat["max_ms"] = round(_as_float(lat.get("max_ms")), 3)
    hist = lat.get("histogram")
    hist = hist if isinstance(hist, dict) else {}
    clean_hist: dict[str, int] = {}
    for k, v in hist.items():
        n = _as_int(v)
        if n:
            clean_hist[str(_as_int(k))] = n
    lat["histogram"] = clean_hist
    st["latency"] = lat

    pending = st.get("pending_starts")
    pending = pending if isinstance(pending, list) else []
    # 只保留结构正确的 pending(容忍手工改坏)
    st["pending_starts"] = [
        p for p in pending
        if isinstance(p, dict) and ("ts" in p)
    ][-MAX_PENDING_STARTS:]

    names = st.get("tool_names")
    names = names if isinstance(names, dict) else {}
    st["tool_names"] = {str(k): _as_int(v) for k, v in names.items() if _as_int(v)}
    st["last_error"] = str(st.get("last_error") or "")[:300]
    st["calls"] = _ensure_calls_state(st.get("calls"))
    st.setdefault("file", "")
    st.setdefault("qid", st.get("file") or "?")
    st.setdefault("slot", None)
    return st


def _fresh_state(run_dir: Path) -> dict:
    now = time.time()
    return {
        "schema_version": STATE_VERSION,
        "run_dir": str(run_dir),
        "created_at": now,
        "updated_at": now,
        "window_seconds": DEFAULT_WINDOW_SECONDS,
        "files": {},
        "observations": [],
    }


def _sum_raw(files: dict[str, dict]) -> dict:
    """只用于计算一轮扫描前后的增量,喂给窗口速率。"""
    out = {
        "requests": 0,
        "tokens": _zero_tokens(),
        "cost": _zero_cost(),
        "error_events": 0,
    }
    for st in files.values():
        requests = _as_int(st.get("requests"))
        tokens = st.get("tokens") or {}
        cost = st.get("cost") or {}
        calls = st.get("calls") or {}
        calls_tokens = calls.get("usage") or {}
        # 与报告口径保持一致:主 jsonl 没有 usage 时,窗口速率也用 .calls 兜底
        if _as_int(tokens.get("total")) == 0 and _as_int(calls_tokens.get("total")) > 0:
            tokens = calls_tokens
            cost = calls.get("cost") or {}
            if requests == 0:
                requests = _as_int(calls.get("count"))
        out["requests"] += requests
        for k in _TOKEN_FIELDS:
            out["tokens"][k] += _as_int(tokens.get(k))
        for k in _COST_FIELDS:
            out["cost"][k] += _as_float(cost.get(k))
        out["error_events"] += _as_int(st.get("error_events"))
    return out


def _add_usage(st: dict, usage: dict[str, int], cost: dict[str, float]) -> None:
    for k in _TOKEN_FIELDS:
        st["tokens"][k] += usage.get(k, 0)
    for k in _COST_FIELDS:
        st["cost"][k] = round(st["cost"].get(k, 0.0) + cost.get(k, 0.0), 6)


def _add_latency(st: dict, ms: float | None) -> None:
    lat = st["latency"]
    if ms is None:
        lat["unknown"] += 1
        return
    value = max(0.0, float(ms))
    lat["count"] += 1
    lat["sum_ms"] = round(lat["sum_ms"] + value, 3)
    lat["max_ms"] = round(max(lat["max_ms"], value), 3)
    hist = lat["histogram"]
    key = str(int(round(value)))
    hist[key] = hist.get(key, 0) + 1


def _update_clock(st: dict, ts: float | None, source: str | None, event_type: str) -> None:
    if ts is None:
        return
    if st["first_ts"] is None or ts < st["first_ts"]:
        st["first_ts"] = ts
    if st["last_ts"] is None or ts > st["last_ts"]:
        st["last_ts"] = ts
    if source == "event" and event_type != "session":
        st["line_ts_count"] += 1
        if st["last_line_ts"] is None or ts > st["last_line_ts"]:
            st["last_line_ts"] = ts


# --------------------------------------------------------------------------- #
# 监控器
# --------------------------------------------------------------------------- #
class AiMonitor:
    """增量扫描 logs/*.jsonl,产出汇总报告。"""

    def __init__(self, run_dir: str | Path, window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 state_path: str | Path | None = None):
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.logs_dir = self.run_dir / "logs"
        self.window_seconds = float(window_seconds)
        self.state_path = (Path(state_path).expanduser().resolve()
                           if state_path else self.logs_dir / ".ai_monitor_state.json")
        self.state = self._load_state()
        self.last_report: dict | None = None

    # ---------------------------------------------------------------- state
    def _load_state(self) -> dict:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return _fresh_state(self.run_dir)
        if not isinstance(data, dict) or data.get("schema_version") != STATE_VERSION:
            return _fresh_state(self.run_dir)
        try:
            same = Path(str(data.get("run_dir"))).expanduser().resolve() == self.run_dir
        except OSError:
            same = False
        if not same:
            return _fresh_state(self.run_dir)
        files = data.get("files")
        if not isinstance(files, dict):
            files = {}
        clean_files: dict[str, dict] = {}
        for name, st in files.items():
            if isinstance(st, dict):
                st.setdefault("file", name)
                clean_files[str(name)] = _ensure_file_state(st)
        data["files"] = clean_files
        obs = data.get("observations")
        data["observations"] = obs if isinstance(obs, list) else []
        data["window_seconds"] = _as_float(data.get("window_seconds"),
                                           DEFAULT_WINDOW_SECONDS)
        return data

    def _save_state(self) -> None:
        self.state["updated_at"] = time.time()
        _atomic_write_text(
            self.state_path,
            json.dumps(self.state, ensure_ascii=False, indent=1, allow_nan=False),
        )

    # ---------------------------------------------------------------- scan
    def scan(self, now: float | None = None, write_state: bool = True,
             write_outputs: bool = False) -> dict:
        """扫描一轮。now 可注入(测试/回放用),默认当前时间。"""
        started = time.time()
        now = float(now if now is not None else started)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        had_files_before = bool(self.state.get("files"))
        before = _sum_raw(self.state["files"])
        scan_info: dict[str, Any] = {
            "scanned_files": 0,
            "changed_files": 0,
            "reset_files": 0,
            "bytes_read": 0,
            "bad_lines_new": 0,
            "calls_scanned": 0,
            "calls_changed": 0,
            "calls_reset": 0,
            "calls_bytes_read": 0,
            "calls_bad_lines_new": 0,
            "errors": [],
        }

        paths = sorted(self.logs_dir.glob("*.jsonl"))
        found: set[str] = set()
        for path in paths:
            if not path.is_file():
                continue
            found.add(path.name)
            self._scan_file(path, now, scan_info)

        # .calls 旁车(文件名去掉 .calls 就是 worker jsonl 名)
        found_calls: set[str] = set()
        for path in sorted(self.logs_dir.glob("*.calls")):
            if not path.is_file():
                continue
            main_name = path.name[:-len(".calls")]
            if not main_name:
                continue
            found_calls.add(main_name)
            st = self.state["files"].get(main_name)
            if st is None:
                st = _new_file_state(main_name, now)
                st["present"] = False
                self.state["files"][main_name] = st
            st["calls"] = _ensure_calls_state(st.get("calls"))
            self._scan_calls_file(path, main_name, st, now, scan_info)

        for name, st in self.state["files"].items():
            if name not in found:
                st["present"] = False
            calls = st.get("calls")
            if isinstance(calls, dict) and name not in found_calls:
                calls["present"] = False

        after = _sum_raw(self.state["files"])
        token_delta = max(0, after["tokens"]["total"] - before["tokens"]["total"])
        request_delta = max(0, after["requests"] - before["requests"])
        error_delta = max(0, after["error_events"] - before["error_events"])
        observations = self.state.get("observations") or []
        had_observations = bool(observations) and had_files_before
        last_obs = (_as_float(observations[-1].get("t"), 0.0)
                    if observations and isinstance(observations[-1], dict) else 0.0)
        # 首次扫描(或监控停了很久)会把历史日志的全部 token 一次性读进来;
        # 标记为 backfill,避免窗口速率出现几十亿 tok/min。
        backfill = (not had_observations or
                    (last_obs > 0 and now - last_obs > max(60.0, self.window_seconds * 2)))
        self._append_observation(now, token_delta, request_delta, error_delta,
                                 backfill=backfill)

        self.state["updated_at"] = now
        self.state["window_seconds"] = self.window_seconds

        scan_info["elapsed_ms"] = round((time.time() - started) * 1000.0, 2)
        report = build_report(
            self.state, self.run_dir, now, self.window_seconds,
            scan_info=scan_info, state_path=self.state_path,
        )
        self.last_report = report
        if write_state:
            self._save_state()
        if write_outputs:
            self.write_outputs(report)
        return report

    def _append_observation(self, now: float, tokens: int, requests: int,
                            errors: int, backfill: bool = False) -> None:
        obs = self.state.get("observations")
        if not isinstance(obs, list):
            obs = []
        obs.append({"t": now, "tokens": int(tokens),
                    "requests": int(requests), "errors": int(errors),
                    "backfill": bool(backfill)})
        cutoff = now - max(self.window_seconds * 2.0, 900.0)
        obs = [o for o in obs
               if isinstance(o, dict) and _as_float(o.get("t"), -1e18) >= cutoff]
        self.state["observations"] = obs[-MAX_OBSERVATIONS:]

    def _scan_file(self, path: Path, now: float, scan_info: dict) -> None:
        name = path.name
        st = self.state["files"].get(name)
        scan_info["scanned_files"] += 1
        try:
            stat = path.stat()
        except FileNotFoundError:
            if st is not None:
                st["present"] = False
            return
        except OSError as exc:
            scan_info["errors"].append(f"{name}: stat: {exc}")
            return

        if st is None:
            st = _new_file_state(name, now)
            self.state["files"][name] = st

        reset = False
        old_offset = _as_int_exact(st.get("offset"))
        old_inode = _as_int_exact(st.get("inode"))
        old_mtime = _as_int_exact(st.get("mtime_ns"))
        if stat.st_size < old_offset:
            reset = True                      # 截断
        elif old_inode and stat.st_ino and old_inode != stat.st_ino:
            reset = True                      # 被替换(inode 变化)
        elif old_mtime and stat.st_mtime_ns < old_mtime and stat.st_size <= old_offset:
            reset = True                      # 时间倒退且没有新增

        meta_changed = (stat.st_size != _as_int_exact(st.get("size")) or
                        stat.st_mtime_ns != old_mtime)
        head_hash: str | None = None
        if not reset and meta_changed:
            try:
                head_hash = _head_sha1(path)
            except OSError as exc:
                scan_info["errors"].append(f"{name}: head: {exc}")
            if st.get("head_sha1") and head_hash and head_hash != st["head_sha1"]:
                reset = True                  # 同大小/更大小的原地重写
        if reset:
            st = _new_file_state(name, now)
            self.state["files"][name] = st
            scan_info["reset_files"] += 1

        st["present"] = True

        # 没有任何变化:连 4KB 头都不用读
        if (stat.st_size == _as_int_exact(st.get("offset")) and
                stat.st_mtime_ns == _as_int_exact(st.get("mtime_ns")) and
                not st.get("partial_b64") and not st.get("skip_until_newline")):
            return

        # 只有 mtime 变了(例如 touch)但没有新字节:更新元数据后返回
        if stat.st_size == _as_int_exact(st.get("offset")):
            st["size"] = stat.st_size
            st["mtime_ns"] = stat.st_mtime_ns
            st["inode"] = stat.st_ino
            if head_hash:
                st["head_sha1"] = head_hash
            return

        if head_hash is None:
            try:
                head_hash = _head_sha1(path)
            except OSError as exc:
                scan_info["errors"].append(f"{name}: head: {exc}")
                head_hash = st.get("head_sha1") or ""

        bytes_read, bad_new = self._read_new_bytes(path, st, now)
        scan_info["bytes_read"] += bytes_read
        scan_info["bad_lines_new"] += bad_new
        if bytes_read:
            scan_info["changed_files"] += 1
        st["head_sha1"] = head_hash
        try:
            stat2 = path.stat()
            st["size"] = stat2.st_size
            st["mtime_ns"] = stat2.st_mtime_ns
            st["inode"] = stat2.st_ino
        except OSError:
            st["size"] = max(_as_int_exact(st.get("size")),
                             _as_int_exact(st.get("offset")))
        st["last_read"] = now

    def _scan_calls_file(self, path: Path, main_name: str, st: dict,
                         now: float, scan_info: dict) -> None:
        """增量扫描 <worker>.jsonl.calls 旁车,只提取计时/兜底 usage。"""
        calls = _ensure_calls_state(st.get("calls"))
        st["calls"] = calls
        scan_info["calls_scanned"] += 1
        try:
            stat = path.stat()
        except FileNotFoundError:
            calls["present"] = False
            return
        except OSError as exc:
            scan_info["errors"].append(f"{path.name}: stat: {exc}")
            return

        reset = False
        old_offset = _as_int_exact(calls.get("offset"))
        old_inode = _as_int_exact(calls.get("inode"))
        old_mtime = _as_int_exact(calls.get("mtime_ns"))
        if stat.st_size < old_offset:
            reset = True
        elif old_inode and stat.st_ino and old_inode != stat.st_ino:
            reset = True
        elif old_mtime and stat.st_mtime_ns < old_mtime and stat.st_size <= old_offset:
            reset = True

        meta_changed = (stat.st_size != _as_int_exact(calls.get("size")) or
                        stat.st_mtime_ns != old_mtime)
        head_hash: str | None = None
        if not reset and meta_changed:
            try:
                head_hash = _head_sha1(path)
            except OSError as exc:
                scan_info["errors"].append(f"{path.name}: head: {exc}")
            if calls.get("head_sha1") and head_hash and head_hash != calls["head_sha1"]:
                reset = True
        if reset:
            calls = _new_calls_state()
            st["calls"] = calls
            scan_info["calls_reset"] += 1

        calls["present"] = True
        if (stat.st_size == _as_int_exact(calls.get("offset")) and
                stat.st_mtime_ns == _as_int_exact(calls.get("mtime_ns")) and
                not calls.get("partial_b64") and not calls.get("skip_until_newline")):
            return
        if stat.st_size == _as_int_exact(calls.get("offset")):
            calls["size"] = stat.st_size
            calls["mtime_ns"] = stat.st_mtime_ns
            calls["inode"] = stat.st_ino
            if head_hash:
                calls["head_sha1"] = head_hash
            return

        if head_hash is None:
            try:
                head_hash = _head_sha1(path)
            except OSError as exc:
                scan_info["errors"].append(f"{path.name}: head: {exc}")
                head_hash = calls.get("head_sha1") or ""

        bytes_read, bad_new = self._read_stream(path, calls, now,
                                                self._process_call_line)
        scan_info["calls_bytes_read"] += bytes_read
        scan_info["calls_bad_lines_new"] += bad_new
        if bytes_read:
            scan_info["calls_changed"] += 1
        calls["head_sha1"] = head_hash
        try:
            stat2 = path.stat()
            calls["size"] = stat2.st_size
            calls["mtime_ns"] = stat2.st_mtime_ns
            calls["inode"] = stat2.st_ino
        except OSError:
            calls["size"] = max(_as_int_exact(calls.get("size")),
                                _as_int_exact(calls.get("offset")))
        calls["last_read"] = now

    def _process_call_line(self, calls: dict, raw: bytes, now: float) -> None:
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
        if not text:
            return
        try:
            obj = json.loads(text)
        except ValueError:
            calls["bad_lines"] += 1
            return
        if not isinstance(obj, dict):
            calls["bad_lines"] += 1
            return
        kind = obj.get("kind")
        if kind not in (None, "", "ai_call"):
            return  # 未来可能追加其它 kind;只统计模型调用

        calls["count"] += 1
        start_ts = parse_timestamp(obj.get("start_ms"))
        end_ts = parse_timestamp(obj.get("end_ms"))
        if start_ts is not None:
            if calls["first_start_ts"] is None or start_ts < calls["first_start_ts"]:
                calls["first_start_ts"] = start_ts
        if end_ts is not None:
            if calls["last_end_ts"] is None or end_ts > calls["last_end_ts"]:
                calls["last_end_ts"] = end_ts

        duration = obj.get("duration_ms")
        if duration is None and start_ts is not None and end_ts is not None:
            duration = (end_ts - start_ts) * 1000.0
        duration = _as_float(duration, -1.0) if duration is not None else -1.0
        if duration >= 0:
            calls["duration_count"] += 1
            calls["duration_sum_ms"] = round(
                calls["duration_sum_ms"] + duration, 3)
            calls["duration_max_ms"] = round(
                max(calls["duration_max_ms"], duration), 3)
            key = str(int(round(duration)))
            hist = calls["duration_histogram"]
            hist[key] = hist.get(key, 0) + 1

        ttfb = obj.get("ttfb_ms")
        if ttfb is not None:
            ttfb_value = _as_float(ttfb, -1.0)
            if ttfb_value >= 0:
                calls["ttfb_count"] += 1
                calls["ttfb_sum_ms"] = round(calls["ttfb_sum_ms"] + ttfb_value, 3)
                calls["ttfb_max_ms"] = round(
                    max(calls["ttfb_max_ms"], ttfb_value), 3)
                key = str(int(round(ttfb_value)))
                hist = calls["ttfb_histogram"]
                hist[key] = hist.get(key, 0) + 1

        raw_usage = obj.get("usage")
        usage = normalize_usage(raw_usage)
        cost = normalize_cost(raw_usage.get("cost")
                              if isinstance(raw_usage, dict) else None)
        for k in _TOKEN_FIELDS:
            calls["usage"][k] += usage.get(k, 0)
        for k in _COST_FIELDS:
            calls["cost"][k] = round(calls["cost"].get(k, 0.0) + cost.get(k, 0.0), 6)

        status = _as_int(obj.get("status"))
        error_text = str(obj.get("error") or "").strip()
        if (status and status >= 400) or error_text:
            calls["errors"] += 1
            calls["last_error"] = (error_text or f"status={status}")[:300]

        model = str(obj.get("model") or "").strip()
        if model:
            calls["models"][model] = _as_int(calls["models"].get(model)) + 1

    def _read_new_bytes(self, path: Path, st: dict, now: float) -> tuple[int, int]:
        """从 st['offset'] 读新增字节,按行处理;返回 (读入字节数, 新增坏行数)。"""
        return self._read_stream(path, st, now, self._process_line)

    def _read_stream(self, path: Path, meta: dict, now: float,
                     on_line) -> tuple[int, int]:
        """通用增量行读取:主 jsonl 和 .calls 共用同一套半行/截断容忍逻辑。"""
        partial_b64 = meta.get("partial_b64") or ""
        try:
            partial = base64.b64decode(partial_b64.encode("ascii")) if partial_b64 else b""
        except Exception:
            partial = b""
        skip_until_newline = bool(meta.get("skip_until_newline"))
        bytes_read = 0
        bad_before = meta["bad_lines"]

        try:
            with open(path, "rb") as f:
                f.seek(_as_int_exact(meta.get("offset")))
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if skip_until_newline:
                        idx = chunk.find(b"\n")
                        if idx < 0:
                            continue
                        chunk = chunk[idx + 1:]
                        skip_until_newline = False

                    data = partial + chunk
                    parts = data.split(b"\n")
                    partial = parts.pop()
                    if len(partial) > MAX_LINE_BYTES:
                        # 异常超长行:丢掉,后续字节跳到下一个换行
                        partial = b""
                        skip_until_newline = True
                        meta["bad_lines"] += 1
                    for raw in parts:
                        on_line(meta, raw, now)
        except OSError as exc:
            meta["last_error"] = f"read: {exc}"[:300]
        finally:
            meta["offset"] = _as_int_exact(meta.get("offset")) + bytes_read

        if partial and not skip_until_newline:
            if not partial.strip():
                partial = b""
            elif self._accept_final_partial(meta, partial, now, on_line):
                partial = b""

        meta["partial_b64"] = base64.b64encode(partial).decode("ascii") if partial else ""
        meta["skip_until_newline"] = skip_until_newline
        meta["last_read"] = now
        return bytes_read, meta["bad_lines"] - bad_before

    def _accept_final_partial(self, meta: dict, raw: bytes, now: float,
                              on_line) -> bool:
        """文件末尾没有换行的完整 JSON 也要吃掉;半行则保留到下次。"""
        try:
            text = raw.decode("utf-8", errors="replace").strip()
            obj = json.loads(text)
        except ValueError:
            return False
        if not isinstance(obj, dict):
            meta["bad_lines"] += 1
            return True
        on_line(meta, raw, now)
        return True

    def _process_line(self, st: dict, raw: bytes, now: float) -> None:
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
        if not text:
            return
        prefix = text[:512]
        m = _TYPE_RE.match(text)
        event_type = m.group(1) if m else None

        if event_type is not None:
            # 跳过体积大、且不提供所需指标的事件。
            # 注意:不要在截断的前缀里用正则抓 timestamp —— 数字可能被截断,
            # 或命中 content 里的 message.timestamp,把最后活动时间算到几百年后。
            if event_type in _SKIP_TYPES:
                return
            if event_type in ("message_start", "message_end"):
                rm = _ROLE_RE.search(prefix)
                if rm and rm.group(1) != "assistant":
                    return
            obj = _json_loads(text)
            if obj is None:
                st["bad_lines"] += 1
                return
            if not isinstance(obj, dict):
                st["bad_lines"] += 1
                return
        else:
            obj = _json_loads(text)
            if obj is None:
                st["bad_lines"] += 1
                return
            if not isinstance(obj, dict):
                st["bad_lines"] += 1
                return
        self._process_event(st, obj, now)

    def _process_event(self, st: dict, ev: dict, now: float) -> None:
        event_type = ev.get("type")
        msg = ev.get("message")
        msg = msg if isinstance(msg, dict) else None
        ts, ts_source = _event_clock(ev, msg)
        _update_clock(st, ts, ts_source, str(event_type or ""))

        if event_type == "session":
            if ts is not None and st.get("started_ts") is None:
                st["started_ts"] = ts
            return

        if event_type == "message_start":
            if msg is not None and msg.get("role") == "assistant":
                pending = st["pending_starts"]
                pending.append({"ts": ts, "src": ts_source, "obs": now})
                if len(pending) > MAX_PENDING_STARTS:
                    pending.pop(0)
                    st["latency"]["unknown"] += 1
            return

        if event_type == "message_end":
            if msg is not None and msg.get("role") == "assistant":
                usage = normalize_usage(msg.get("usage"))
                cost = normalize_cost((msg.get("usage") or {}).get("cost")
                                      if isinstance(msg.get("usage"), dict) else None)
                st["requests"] += 1
                _add_usage(st, usage, cost)

                start = None
                pending = st["pending_starts"]
                if pending:
                    start = pending.pop(0)
                latency: float | None = None
                if isinstance(start, dict):
                    s_ts = parse_timestamp(start.get("ts"))
                    s_src = start.get("src")
                    if s_ts is not None and ts is not None:
                        delta = (ts - s_ts) * 1000.0
                        if delta < 0:
                            latency = None
                        elif delta == 0 and s_src == "message" and ts_source == "message":
                            # pi 有些版本 start/end 共用 message.timestamp,测不出来
                            latency = None
                        else:
                            latency = delta
                _add_latency(st, latency)
            return

        if event_type == "tool_execution_start":
            st["tool_calls"] += 1
            name = str(ev.get("toolName") or "?")
            names = st["tool_names"]
            names[name] = _as_int(names.get(name)) + 1
            return

        if event_type == "tool_execution_end":
            if ev.get("isError"):
                st["tool_errors"] += 1
            return

        if event_type == "error":
            st["error_events"] += 1
            detail = (ev.get("errorMessage") or ev.get("error") or
                      ev.get("message") or ev.get("code") or "")
            st["last_error"] = str(detail)[:300]
            return

    # ---------------------------------------------------------------- output
    def write_outputs(self, report: dict) -> None:
        # ai_usage.json 用 dashboard 约定结构;完整明细在 ai_usage.md 的表格里
        contract = build_contract(report)
        _atomic_write_text(
            self.logs_dir / "ai_usage.json",
            json.dumps(contract, ensure_ascii=False, indent=2, allow_nan=False),
        )
        _atomic_write_text(
            self.logs_dir / "ai_usage.md",
            render_markdown(report),
        )


def _json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _head_sha1(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(HEAD_BYTES)).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                              dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# 聚合 / 报告
# --------------------------------------------------------------------------- #
def _percentile(histogram: dict[str, int], pct: float) -> float | None:
    """nearest-rank 百分位;histogram 的 key 是整数毫秒。"""
    n = sum(_as_int(v) for v in histogram.values())
    if n <= 0:
        return None
    rank = max(1, int(math.ceil(pct / 100.0 * n)))
    acc = 0
    for key in sorted(histogram, key=lambda x: _as_int(x)):
        acc += _as_int(histogram[key])
        if acc >= rank:
            return float(_as_int(key))
    return float(_as_int(sorted(histogram, key=lambda x: _as_int(x))[-1]))


def _ttfb_view(count: int, sum_ms: float, max_ms: float,
               histogram: dict[str, int]) -> dict:
    count = _as_int(count)
    return {
        "count": count,
        "avg_ms": round(float(sum_ms) / count, 3) if count else None,
        "p50_ms": _percentile(histogram, 50) if count else None,
        "p95_ms": _percentile(histogram, 95) if count else None,
        "max_ms": round(float(max_ms), 3) if count else None,
        "sum_ms": round(float(sum_ms), 3),
        "histogram_ms": {str(k): _as_int(v) for k, v in histogram.items()
                         if _as_int(v)},
    }


def _empty_ttfb_view() -> dict:
    return _ttfb_view(0, 0.0, 0.0, {})


def _latency_view(count: int, unknown: int, sum_ms: float, max_ms: float,
                  histogram: dict[str, int], source: str = "jsonl",
                  ttfb: dict | None = None) -> dict:
    count = _as_int(count)
    return {
        "count": count,
        "unknown": _as_int(unknown),
        "avg_ms": round(float(sum_ms) / count, 3) if count else None,
        "p50_ms": _percentile(histogram, 50) if count else None,
        "p95_ms": _percentile(histogram, 95) if count else None,
        "max_ms": round(float(max_ms), 3) if count else None,
        "sum_ms": round(float(sum_ms), 3),
        "source": source,
        "histogram_ms": {str(k): _as_int(v) for k, v in histogram.items()
                         if _as_int(v)},
        "ttfb_ms": ttfb if ttfb is not None else _empty_ttfb_view(),
    }


def _merge_latency(parts: list[dict]) -> dict:
    count = unknown = 0
    sum_ms = max_ms = 0.0
    hist: dict[str, int] = {}
    sources: set[str] = set()
    ttfb_count = 0
    ttfb_sum = ttfb_max = 0.0
    ttfb_hist: dict[str, int] = {}
    for p in parts:
        count += _as_int(p.get("count"))
        unknown += _as_int(p.get("unknown"))
        sum_ms += _as_float(p.get("sum_ms"))
        max_ms = max(max_ms, _as_float(p.get("max_ms")))
        h = p.get("histogram_ms") or p.get("histogram") or {}
        if isinstance(h, dict):
            for k, v in h.items():
                ik = str(_as_int(k))
                hist[ik] = hist.get(ik, 0) + _as_int(v)
        src = str(p.get("source") or "")
        if _as_int(p.get("count")) > 0 and src in ("calls", "jsonl"):
            sources.add(src)
        t = p.get("ttfb_ms") or {}
        if isinstance(t, dict):
            ttfb_count += _as_int(t.get("count"))
            ttfb_sum += _as_float(t.get("sum_ms"))
            ttfb_max = max(ttfb_max, _as_float(t.get("max_ms")))
            th = t.get("histogram_ms") or {}
            if isinstance(th, dict):
                for k, v in th.items():
                    ik = str(_as_int(k))
                    ttfb_hist[ik] = ttfb_hist.get(ik, 0) + _as_int(v)
    if len(sources) == 1:
        source = next(iter(sources))
    elif len(sources) > 1:
        source = "mixed"
    else:
        source = "none"
    return _latency_view(
        count, unknown, sum_ms, max_ms, hist, source=source,
        ttfb=_ttfb_view(ttfb_count, ttfb_sum, ttfb_max, ttfb_hist),
    )


def _merge_tokens(workers: list[dict]) -> dict[str, int]:
    out = _zero_tokens()
    for w in workers:
        t = w.get("tokens") or {}
        for k in _TOKEN_FIELDS:
            out[k] += _as_int(t.get(k))
    return out


def _merge_cost(workers: list[dict]) -> dict[str, float]:
    out = _zero_cost()
    for w in workers:
        c = w.get("cost") or {}
        for k in _COST_FIELDS:
            out[k] = round(out[k] + _as_float(c.get(k)), 6)
    return out


def _worker_view(name: str, st: dict, now: float) -> dict:
    calls = _ensure_calls_state(st.get("calls"))
    st["calls"] = calls
    calls_count = _as_int(calls.get("count"))

    main_tokens = {k: _as_int((st.get("tokens") or {}).get(k))
                   for k in _TOKEN_FIELDS}
    main_cost = {k: round(_as_float((st.get("cost") or {}).get(k)), 6)
                 for k in _COST_FIELDS}
    calls_tokens = {k: _as_int((calls.get("usage") or {}).get(k))
                    for k in _TOKEN_FIELDS}
    calls_cost = {k: round(_as_float((calls.get("cost") or {}).get(k)), 6)
                  for k in _COST_FIELDS}

    # usage/请求数以主 jsonl 为准;主 jsonl 完全没有 usage 时才用 .calls 兜底
    tokens, cost = main_tokens, main_cost
    usage_source = "jsonl"
    if main_tokens["total"] == 0 and calls_tokens["total"] > 0:
        tokens, cost = calls_tokens, calls_cost
        usage_source = "calls"
    requests = _as_int(st.get("requests"))
    requests_source = "jsonl"
    if requests == 0 and calls_count > 0:
        requests = calls_count
        requests_source = "calls"

    # 延迟:有 .calls 的 duration_ms 就优先用它;否则回退主 jsonl 的
    # message_start -> message_end 配对。
    calls_latency = _latency_view(
        calls.get("duration_count", 0), 0,
        calls.get("duration_sum_ms", 0), calls.get("duration_max_ms", 0),
        calls.get("duration_histogram", {}),
        source="calls",
        ttfb=_ttfb_view(calls.get("ttfb_count", 0),
                        calls.get("ttfb_sum_ms", 0),
                        calls.get("ttfb_max_ms", 0),
                        calls.get("ttfb_histogram", {})),
    )
    lat_raw = st.get("latency") or {}
    main_latency = _latency_view(
        lat_raw.get("count", 0), lat_raw.get("unknown", 0),
        lat_raw.get("sum_ms", 0), lat_raw.get("max_ms", 0),
        lat_raw.get("histogram", {}), source="jsonl",
    )
    if _as_int(calls.get("duration_count")) > 0:
        latency = calls_latency
        latency["unknown"] = max(0, requests - _as_int(calls.get("duration_count")))
    else:
        latency = main_latency
        latency["source"] = "jsonl" if _as_int(main_latency.get("count")) else "none"

    calls_start = calls.get("first_start_ts")
    started = (st.get("started_ts") or st.get("first_ts") or
               calls_start or st.get("spawn_ts"))
    if st.get("line_ts_count", 0) > 0:
        last = max(st.get("last_line_ts") or 0.0, st.get("last_ts") or 0.0) or None
        source = "event" if last is not None else None
    else:
        # pi 的常见格式:只有 session 行带顶层 timestamp,其余事件没有。
        # 用 mtime 判断最后活动,否则正在跑的 worker 会被误判为沉默。
        last = max((st.get("mtime_ns") or 0) / 1e9,
                   st.get("last_ts") or 0.0) or None
        source = "mtime" if last is not None else None
    if last is None:
        last = st.get("first_seen")
        source = "first_seen" if last is not None else None

    # .calls 的 end_ms(以及正在追加时的 mtime)也是活跃证据
    calls_last = calls.get("last_end_ts")
    if calls.get("present"):
        calls_last = max(calls_last or 0.0,
                         (calls.get("mtime_ns") or 0) / 1e9) or None
    if calls_last is not None and (last is None or calls_last > last):
        last = calls_last
        source = "calls"

    duration = (last - started) if (last is not None and started is not None
                                    and last >= started) else None
    age = (now - last) if last is not None else None
    active = ((bool(st.get("present")) or bool(calls.get("present")))
              and age is not None and age < ACTIVE_WINDOW_SECONDS)

    pending = st.get("pending_starts") or []
    calls_view = {
        "present": bool(calls.get("present")),
        "count": calls_count,
        "bad_lines": _as_int(calls.get("bad_lines")),
        "errors": _as_int(calls.get("errors")),
        "usage": calls_tokens,
        "cost": calls_cost,
        "first_start_at": iso_utc(calls.get("first_start_ts")),
        "first_start_at_local": local_str(calls.get("first_start_ts")),
        "last_end_at": iso_utc(calls.get("last_end_ts")),
        "last_end_at_local": local_str(calls.get("last_end_ts")),
        "last_end_epoch": calls.get("last_end_ts"),
        "ttfb_ms": latency["ttfb_ms"],
        "models": dict(sorted((calls.get("models") or {}).items())),
    }

    return {
        "qid": st.get("qid") or "?",
        "slot": st.get("slot"),
        "file": name,
        "path": f"logs/{name}",
        "present": bool(st.get("present")),
        "active": active,
        "started_at": iso_utc(started),
        "started_at_local": local_str(started),
        "started_epoch": started,
        "last_event_at": iso_utc(last),
        "last_event_at_local": local_str(last),
        "last_event_epoch": last,
        "last_event_source": source,
        "last_event_age_seconds": round(age, 3) if age is not None else None,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "requests": requests,
        "requests_source": requests_source,
        "usage_source": usage_source,
        "tokens": tokens,
        "cost": cost,
        "latency_ms": latency,
        "latency_source": latency["source"],
        "thinking_time_ms": latency["sum_ms"],
        "tool_calls": _as_int(st.get("tool_calls")),
        "tool_errors": _as_int(st.get("tool_errors")),
        "tool_names": dict(sorted((st.get("tool_names") or {}).items())),
        "error_events": _as_int(st.get("error_events")),
        "bad_lines": _as_int(st.get("bad_lines")),
        "pending_calls": len(pending),
        "last_error": st.get("last_error") or calls.get("last_error") or "",
        "calls": calls_view,
        "size_bytes": _as_int_exact(st.get("size")),
        "offset": _as_int_exact(st.get("offset")),
    }


def _question_view(qid: str, workers: list[dict]) -> dict:
    tokens = _merge_tokens(workers)
    cost = _merge_cost(workers)
    latency = _merge_latency([w["latency_ms"] for w in workers])
    starts = [w["started_epoch"] for w in workers
              if w.get("started_epoch") is not None]
    lasts = [w["last_event_epoch"] for w in workers
             if w.get("last_event_epoch") is not None]
    started = min(starts) if starts else None
    last = max(lasts) if lasts else None
    duration = (last - started) if (last is not None and started is not None
                                    and last >= started) else None
    return {
        "qid": qid,
        "workers": len(workers),
        "active_workers": sum(1 for w in workers if w.get("active")),
        "worker_files": [w["file"] for w in workers],
        "started_at": iso_utc(started),
        "started_at_local": local_str(started),
        "last_event_at": iso_utc(last),
        "last_event_at_local": local_str(last),
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "requests": sum(w["requests"] for w in workers),
        "tokens": tokens,
        "cost": cost,
        "latency_ms": latency,
        "latency_source": latency["source"],
        "thinking_time_ms": latency["sum_ms"],
        "tool_calls": sum(w["tool_calls"] for w in workers),
        "tool_errors": sum(w["tool_errors"] for w in workers),
        "error_events": sum(w["error_events"] for w in workers),
        "bad_lines": sum(w["bad_lines"] for w in workers),
        "pending_calls": sum(w["pending_calls"] for w in workers),
        "calls_count": sum(w["calls"]["count"] for w in workers),
        "calls_present_workers": sum(1 for w in workers if w["calls"]["present"]),
    }


def _window_throughput(state: dict, now: float, window_seconds: float,
                       lifetime_tokens_pm: float | None,
                       lifetime_requests_pm: float | None) -> dict:
    cutoff = now - max(1.0, window_seconds)
    samples = []
    for o in state.get("observations") or []:
        if not isinstance(o, dict):
            continue
        t = _as_float(o.get("t"), -1e18)
        if t >= cutoff:
            samples.append(o)
    samples.sort(key=lambda x: _as_float(x.get("t")))
    span = (now - _as_float(samples[0].get("t"))) if samples else 0.0
    active = [o for o in samples if not o.get("backfill")]
    base = {
        "seconds": window_seconds,
        "samples": len(active),
        "backfill_samples": len(samples) - len(active),
    }
    if not active or span <= 0.001:
        base.update({
            "basis": "lifetime" if lifetime_tokens_pm is not None else "none",
            "tokens_per_minute": lifetime_tokens_pm,
            "requests_per_minute": lifetime_requests_pm,
        })
        return base
    tokens = sum(_as_int(o.get("tokens")) for o in active)
    requests = sum(_as_int(o.get("requests")) for o in active)
    base.update({
        "basis": "observation",
        "tokens_per_minute": round(tokens / span * 60.0, 3),
        "requests_per_minute": round(requests / span * 60.0, 3),
    })
    return base


def build_report(state: dict, run_dir: Path, now: float, window_seconds: float,
                 scan_info: dict | None = None,
                 state_path: Path | None = None) -> dict:
    files = state.get("files") or {}
    workers = [_worker_view(name, st, now) for name, st in files.items()]
    workers.sort(key=lambda w: (
        str(w.get("qid") or ""),
        w["slot"] if w.get("slot") is not None else -1,
        w["file"],
    ))

    groups: dict[str, list[dict]] = {}
    for w in workers:
        groups.setdefault(str(w["qid"]), []).append(w)
    questions = [_question_view(qid, ws) for qid, ws in groups.items()]
    questions.sort(key=lambda q: (-q["tokens"]["total"], q["qid"]))

    tokens = _merge_tokens(workers)
    cost = _merge_cost(workers)
    latency = _merge_latency([w["latency_ms"] for w in workers])
    starts = [w["started_epoch"] for w in workers
              if w.get("started_epoch") is not None]
    lasts = [w["last_event_epoch"] for w in workers
             if w.get("last_event_epoch") is not None]
    started = min(starts) if starts else None
    last = max(lasts) if lasts else None
    duration = (last - started) if (last is not None and started is not None
                                    and last >= started) else None

    lifetime_tokens_pm = None
    lifetime_requests_pm = None
    output_tps = None
    if duration and duration > 0:
        lifetime_tokens_pm = round(tokens["total"] / duration * 60.0, 3)
        lifetime_requests_pm = round(
            sum(w["requests"] for w in workers) / duration * 60.0, 3)
    if latency["sum_ms"] and latency["sum_ms"] > 0:
        output_tps = round(tokens["output"] / (latency["sum_ms"] / 1000.0), 3)

    total_requests = sum(w["requests"] for w in workers)
    total_tool_calls = sum(w["tool_calls"] for w in workers)
    total_tool_errors = sum(w["tool_errors"] for w in workers)
    total_error_events = sum(w["error_events"] for w in workers)
    total_bad_lines = sum(w["bad_lines"] for w in workers)
    total_pending = sum(w["pending_calls"] for w in workers)
    total_calls = sum(w["calls"]["count"] for w in workers)
    calls_present_workers = sum(1 for w in workers if w["calls"]["present"])
    calls_usage_fallback = sum(1 for w in workers if w["usage_source"] == "calls")

    totals = {
        "workers": len(workers),
        "present_workers": sum(1 for w in workers if w["present"]),
        "active_workers": sum(1 for w in workers if w["active"]),
        "requests": total_requests,
        "tokens": tokens,
        "cost": cost,
        "latency_ms": latency,
        "latency_source": latency["source"],
        "thinking_time_ms": latency["sum_ms"],
        "tool_calls": total_tool_calls,
        "tool_errors": total_tool_errors,
        "error_events": total_error_events,
        "bad_lines": total_bad_lines,
        "pending_calls": total_pending,
        "calls_count": total_calls,
        "calls_present_workers": calls_present_workers,
        "calls_usage_fallback_workers": calls_usage_fallback,
        "started_at": iso_utc(started),
        "started_at_local": local_str(started),
        "last_event_at": iso_utc(last),
        "last_event_at_local": local_str(last),
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "bytes": sum(w["size_bytes"] for w in workers),
    }

    latest_obs = state.get("observations") or []
    window = _window_throughput(state, now, window_seconds,
                                lifetime_tokens_pm, lifetime_requests_pm)

    return {
        "schema_version": STATE_VERSION,
        "generated_at": float(now),
        "generated_at_iso": iso_utc(now),
        "generated_at_local": local_str(now),
        "run_dir": str(run_dir),
        "state_file": str(state_path) if state_path else None,
        "window_seconds": window_seconds,
        "field_docs": FIELD_DOCS,
        "totals": totals,
        "throughput": {
            "lifetime": {
                "duration_seconds": round(duration, 3) if duration is not None else None,
                "tokens_per_minute": lifetime_tokens_pm,
                "requests_per_minute": lifetime_requests_pm,
                "output_tokens_per_second": output_tps,
            },
            "window": window,
        },
        "questions": questions,
        "workers": workers,
        "scan": scan_info or {},
        "observation_samples": len(latest_obs),
    }


def _round_or_zero(v: Any) -> float:
    return 0.0 if v is None else round(_as_float(v), 3)


def _elapsed_or_zero(v: Any) -> float:
    return 0.0 if v is None else round(_as_float(v), 3)


def build_contract(report: dict) -> dict:
    """把内部富报告压成 dashboard 约定的 ai_usage.json 顶层结构。

    顶层固定键:
      generated_at / generated_at_local / elapsed_s / active_workers /
      totals / rates / latency / by_question / by_worker
    缺失值给 0 / null,不省略键。by_question、by_worker 内额外带明细字段。
    """
    t = report.get("totals") or {}
    tk = t.get("tokens") or {}
    cost = t.get("cost") or {}
    lat = t.get("latency_ms") or {}
    th = report.get("throughput") or {}
    lt = th.get("lifetime") or {}
    win = th.get("window") or {}

    basis = str(win.get("basis") or "none")
    if basis == "observation":
        tokens_per_min = win.get("tokens_per_minute")
        requests_per_min = win.get("requests_per_minute")
    else:
        tokens_per_min = lt.get("tokens_per_minute")
        requests_per_min = lt.get("requests_per_minute")
        basis = "lifetime" if tokens_per_min is not None else "none"

    by_question: dict[str, dict] = {}
    for q in report.get("questions") or []:
        qid = str(q.get("qid") or "?")
        qt = q.get("tokens") or {}
        ql = q.get("latency_ms") or {}
        qc = q.get("cost") or {}
        by_question[qid] = {
            # ---- dashboard 必需 ----
            "requests": _as_int(q.get("requests")),
            "total_tokens": _as_int(qt.get("total")),
            "avg_latency_ms": _round_or_zero(ql.get("avg_ms")),
            "elapsed_s": _elapsed_or_zero(q.get("duration_seconds")),
            # ---- 明细(可选) ----
            "workers": _as_int(q.get("workers")),
            "active_workers": _as_int(q.get("active_workers")),
            "worker_files": list(q.get("worker_files") or []),
            "input_tokens": _as_int(qt.get("input")),
            "output_tokens": _as_int(qt.get("output")),
            "cache_read_tokens": _as_int(qt.get("cache_read")),
            "cache_write_tokens": _as_int(qt.get("cache_write")),
            "reasoning_tokens": _as_int(qt.get("reasoning")),
            "cost_total": round(_as_float(qc.get("total")), 6),
            "p50_latency_ms": _round_or_zero(ql.get("p50_ms")),
            "p95_latency_ms": _round_or_zero(ql.get("p95_ms")),
            "max_latency_ms": _round_or_zero(ql.get("max_ms")),
            "latency_samples": _as_int(ql.get("count")),
            "latency_unknown": _as_int(ql.get("unknown")),
            "latency_source": q.get("latency_source") or ql.get("source") or "none",
            "ttfb_avg_ms": _round_or_zero((ql.get("ttfb_ms") or {}).get("avg_ms")),
            "ttfb_p95_ms": _round_or_zero((ql.get("ttfb_ms") or {}).get("p95_ms")),
            "thinking_time_ms": _round_or_zero(q.get("thinking_time_ms")),
            "tool_calls": _as_int(q.get("tool_calls")),
            "tool_errors": _as_int(q.get("tool_errors")),
            "errors": _as_int(q.get("error_events")),
            "bad_lines": _as_int(q.get("bad_lines")),
            "pending_calls": _as_int(q.get("pending_calls")),
            "calls_count": _as_int(q.get("calls_count")),
            "calls_present_workers": _as_int(q.get("calls_present_workers")),
            "started_at_utc": q.get("started_at"),
            "started_at_local": q.get("started_at_local"),
            "last_event_at_utc": q.get("last_event_at"),
            "last_event_at_local": q.get("last_event_at_local"),
        }

    by_worker: dict[str, dict] = {}
    for w in report.get("workers") or []:
        file_name = str(w.get("file") or "")
        key = str(w.get("path") or f"logs/{file_name}")
        wt = w.get("tokens") or {}
        wl = w.get("latency_ms") or {}
        wc = w.get("cost") or {}
        last_epoch = w.get("last_event_epoch")
        by_worker[key] = {
            # ---- dashboard 必需 ----
            "qid": str(w.get("qid") or "?"),
            "slot": w.get("slot"),
            "requests": _as_int(w.get("requests")),
            "total_tokens": _as_int(wt.get("total")),
            "avg_latency_ms": _round_or_zero(wl.get("avg_ms")),
            "last_seen": local_str_short(last_epoch),
            "active": bool(w.get("active")),
            # ---- 明细(可选) ----
            "file": file_name,
            "present": bool(w.get("present")),
            "input_tokens": _as_int(wt.get("input")),
            "output_tokens": _as_int(wt.get("output")),
            "cache_read_tokens": _as_int(wt.get("cache_read")),
            "cache_write_tokens": _as_int(wt.get("cache_write")),
            "reasoning_tokens": _as_int(wt.get("reasoning")),
            "cost_total": round(_as_float(wc.get("total")), 6),
            "p50_latency_ms": _round_or_zero(wl.get("p50_ms")),
            "p95_latency_ms": _round_or_zero(wl.get("p95_ms")),
            "max_latency_ms": _round_or_zero(wl.get("max_ms")),
            "latency_samples": _as_int(wl.get("count")),
            "latency_unknown": _as_int(wl.get("unknown")),
            "latency_source": w.get("latency_source") or wl.get("source") or "none",
            "ttfb_avg_ms": _round_or_zero((wl.get("ttfb_ms") or {}).get("avg_ms")),
            "ttfb_p95_ms": _round_or_zero((wl.get("ttfb_ms") or {}).get("p95_ms")),
            "usage_source": w.get("usage_source") or "jsonl",
            "requests_source": w.get("requests_source") or "jsonl",
            "thinking_time_ms": _round_or_zero(w.get("thinking_time_ms")),
            "tool_calls": _as_int(w.get("tool_calls")),
            "tool_errors": _as_int(w.get("tool_errors")),
            "errors": _as_int(w.get("error_events")),
            "bad_lines": _as_int(w.get("bad_lines")),
            "pending_calls": _as_int(w.get("pending_calls")),
            "calls_present": bool((w.get("calls") or {}).get("present")),
            "calls_count": _as_int((w.get("calls") or {}).get("count")),
            "calls_last_end_utc": (w.get("calls") or {}).get("last_end_at"),
            "last_error": w.get("last_error") or "",
            "started_at_utc": w.get("started_at"),
            "started_at_local": w.get("started_at_local"),
            "last_seen_utc": w.get("last_event_at"),
            "last_seen_local": w.get("last_event_at_local"),
            "last_event_age_seconds": w.get("last_event_age_seconds"),
            "duration_seconds": w.get("duration_seconds"),
            "size_bytes": _as_int(w.get("size_bytes")),
            "offset": _as_int(w.get("offset")),
        }

    generated_epoch = _as_float(report.get("generated_at"))
    return {
        # ---- dashboard 约定的固定顶层结构 ----
        "generated_at": generated_epoch,
        "generated_at_local": local_str_short(generated_epoch),
        "elapsed_s": _elapsed_or_zero(t.get("duration_seconds")),
        "active_workers": _as_int(t.get("active_workers")),
        "totals": {
            "requests": _as_int(t.get("requests")),
            "input_tokens": _as_int(tk.get("input")),
            "output_tokens": _as_int(tk.get("output")),
            "cache_read_tokens": _as_int(tk.get("cache_read")),
            "cache_write_tokens": _as_int(tk.get("cache_write")),
            "reasoning_tokens": _as_int(tk.get("reasoning")),
            "total_tokens": _as_int(tk.get("total")),
            "cost_total": round(_as_float(cost.get("total")), 6),
            "errors": _as_int(t.get("error_events")),
        },
        "rates": {
            "tokens_per_min": _round_or_zero(tokens_per_min),
            "requests_per_min": _round_or_zero(requests_per_min),
            "basis": basis,
            "window_seconds": _as_float(win.get("seconds"), DEFAULT_WINDOW_SECONDS),
            "samples": _as_int(win.get("samples")),
            "backfill_samples": _as_int(win.get("backfill_samples")),
        },
        "latency": {
            "avg_ms": _round_or_zero(lat.get("avg_ms")),
            "p50_ms": _round_or_zero(lat.get("p50_ms")),
            "p95_ms": _round_or_zero(lat.get("p95_ms")),
            "max_ms": _round_or_zero(lat.get("max_ms")),
            "samples": _as_int(lat.get("count")),
            "unknown": _as_int(lat.get("unknown")),
            "source": lat.get("source") or "none",
            "ttfb_avg_ms": _round_or_zero((lat.get("ttfb_ms") or {}).get("avg_ms")),
            "ttfb_p50_ms": _round_or_zero((lat.get("ttfb_ms") or {}).get("p50_ms")),
            "ttfb_p95_ms": _round_or_zero((lat.get("ttfb_ms") or {}).get("p95_ms")),
            "ttfb_max_ms": _round_or_zero((lat.get("ttfb_ms") or {}).get("max_ms")),
            "ttfb_samples": _as_int((lat.get("ttfb_ms") or {}).get("count")),
        },
        "by_question": by_question,
        "by_worker": by_worker,
    }


# --------------------------------------------------------------------------- #
# 人类可读输出
# --------------------------------------------------------------------------- #
def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_ms(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f / 1000.0:.2f}s" if f >= 1000 else f"{f:.0f}ms"


def _fmt_rate(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f:,.1f}"


def _fmt_duration(seconds: Any) -> str:
    if seconds is None:
        return "n/a"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "n/a"
    if s < 0:
        return "n/a"
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s"


def _fmt_age(seconds: Any) -> str:
    if seconds is None:
        return "n/a"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "n/a"
    if s < 0:
        return "刚刚"
    if s < 90:
        return f"{s:.0f}s"
    return _fmt_duration(s)


def render_summary(report: dict) -> str:
    t = report.get("totals") or {}
    tk = t.get("tokens") or {}
    lat = t.get("latency_ms") or {}
    cost = t.get("cost") or {}
    th = report.get("throughput") or {}
    lt = th.get("lifetime") or {}
    win = th.get("window") or {}
    lines: list[str] = []

    lines.append("AI 用量监控")
    lines.append(f"  运行目录  : {report.get('run_dir')}")
    lines.append(f"  生成时间  : {report.get('generated_at_local')}  "
                 f"(UTC {report.get('generated_at_iso')})")
    lines.append(f"  worker    : {t.get('present_workers', 0)}/"
                 f"{t.get('workers', 0)} 在场, 活跃 {t.get('active_workers', 0)}"
                 f"（最后活动 < {int(ACTIVE_WINDOW_SECONDS)}s）")
    lines.append(f"  运行时长  : {_fmt_duration(t.get('duration_seconds'))}")
    lines.append(f"  请求      : {_fmt_int(t.get('requests'))}"
                 f"    错误事件 {_fmt_int(t.get('error_events'))}"
                 f"    坏行 {_fmt_int(t.get('bad_lines'))}"
                 f"    进行中调用 {_fmt_int(t.get('pending_calls'))}")
    lines.append(f"  Token     : 输入 {_fmt_int(tk.get('input'))}"
                 f" | 输出 {_fmt_int(tk.get('output'))}"
                 f" | 缓存读 {_fmt_int(tk.get('cache_read'))}"
                 f" | 缓存写 {_fmt_int(tk.get('cache_write'))}"
                 f" | reasoning {_fmt_int(tk.get('reasoning'))}"
                 f" | 合计 {_fmt_int(tk.get('total'))}")
    if cost.get("total"):
        lines.append(f"  估算成本  : {float(cost['total']):.6f}"
                     f"（input {float(cost.get('input', 0)):.6f}"
                     f" / output {float(cost.get('output', 0)):.6f}"
                     f" / cacheR {float(cost.get('cache_read', 0)):.6f}"
                     f" / cacheW {float(cost.get('cache_write', 0)):.6f}）")
    lines.append(f"  速率      : {_fmt_rate(lt.get('tokens_per_minute'))} tok/min"
                 f" | {_fmt_rate(lt.get('requests_per_minute'))} req/min"
                 f" | 输出 {_fmt_rate(lt.get('output_tokens_per_second'))} tok/s")
    lines.append(f"  窗口速率  : {_fmt_rate(win.get('tokens_per_minute'))} tok/min"
                 f" | {_fmt_rate(win.get('requests_per_minute'))} req/min"
                 f"（最近 {_fmt_duration(win.get('seconds'))},"
                 f" basis={win.get('basis')}, samples={win.get('samples')}）")
    lat_ttfb = lat.get("ttfb_ms") or {}
    lines.append(f"  模型延迟  : 平均 {_fmt_ms(lat.get('avg_ms'))}"
                 f" | P50 {_fmt_ms(lat.get('p50_ms'))}"
                 f" | P95 {_fmt_ms(lat.get('p95_ms'))}"
                 f" | 最大 {_fmt_ms(lat.get('max_ms'))}"
                 f" | 样本 {_fmt_int(lat.get('count'))}"
                 f", 未知 {_fmt_int(lat.get('unknown'))}"
                 f" | 来源 {lat.get('source') or 'none'}")
    if _as_int(lat_ttfb.get("count")):
        lines.append(f"  TTFB      : 平均 {_fmt_ms(lat_ttfb.get('avg_ms'))}"
                     f" | P50 {_fmt_ms(lat_ttfb.get('p50_ms'))}"
                     f" | P95 {_fmt_ms(lat_ttfb.get('p95_ms'))}"
                     f" | 最大 {_fmt_ms(lat_ttfb.get('max_ms'))}"
                     f" | 样本 {_fmt_int(lat_ttfb.get('count'))}")
    lines.append(f"  思考耗时  : {_fmt_ms(t.get('thinking_time_ms'))}"
                 f" | 工具调用 {_fmt_int(t.get('tool_calls'))}"
                 f"，工具错误 {_fmt_int(t.get('tool_errors'))}"
                 f" | .calls {_fmt_int(t.get('calls_count'))}"
                 f"（{_fmt_int(t.get('calls_present_workers'))} 个 worker 有旁车）")

    lines.append("")
    lines.append("按题目:")
    questions = report.get("questions") or []
    if not questions:
        lines.append("  (无)")
    for q in questions:
        qt = q.get("tokens") or {}
        ql = q.get("latency_ms") or {}
        lines.append(
            f"  {q.get('qid')}  worker {q.get('workers')}"
            f"（活跃 {q.get('active_workers')}）"
            f"  请求 {_fmt_int(q.get('requests'))}"
            f"  token {_fmt_int(qt.get('total'))}"
            f"（输入 {_fmt_int(qt.get('input'))}"
            f" / 输出 {_fmt_int(qt.get('output'))}"
            f" / 缓存读 {_fmt_int(qt.get('cache_read'))}）"
            f"  延迟[{ql.get('source') or 'none'}] avg/P95 "
            f"{_fmt_ms(ql.get('avg_ms'))}/{_fmt_ms(ql.get('p95_ms'))}"
            f"  工具 {_fmt_int(q.get('tool_calls'))}"
            f"  错误 {_fmt_int(q.get('error_events'))}"
            f"  calls {_fmt_int(q.get('calls_count'))}"
            f"  时长 {_fmt_duration(q.get('duration_seconds'))}"
        )

    lines.append("")
    lines.append("按 worker:")
    workers = report.get("workers") or []
    if not workers:
        lines.append("  (无)")
    for w in workers:
        slot = f"w{w['slot']}" if w.get("slot") is not None else "-"
        if w.get("active"):
            status = "活跃"
        elif not w.get("present"):
            status = "已删除"
        else:
            status = f"静默 {_fmt_age(w.get('last_event_age_seconds'))}"
        wt = w.get("tokens") or {}
        wl = w.get("latency_ms") or {}
        lines.append(
            f"  {w.get('qid')} {slot}"
            f"  请求 {_fmt_int(w.get('requests'))}"
            f"  token {_fmt_int(wt.get('total'))}"
            f"  P95[{wl.get('source') or 'none'}] {_fmt_ms(wl.get('p95_ms'))}"
            f"  工具 {_fmt_int(w.get('tool_calls'))}"
            f"  错误 {_fmt_int(w.get('error_events'))}"
            f"  calls {_fmt_int((w.get('calls') or {}).get('count'))}"
            f"  {status}"
            f"  {w.get('file')}"
        )
        if w.get("last_error"):
            lines.append(f"      最近错误: {w['last_error'][:160]}")
    return "\n".join(lines)


def _md_cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    t = report.get("totals") or {}
    tk = t.get("tokens") or {}
    lat = t.get("latency_ms") or {}
    cost = t.get("cost") or {}
    th = report.get("throughput") or {}
    lt = th.get("lifetime") or {}
    win = th.get("window") or {}

    out: list[str] = []
    out.append("# AI 用量监控摘要\n")
    out.append(f"- 运行目录: `{report.get('run_dir')}`")
    out.append(f"- 生成时间: {report.get('generated_at_local')}"
               f"（UTC {report.get('generated_at_iso')}）")
    out.append(f"- worker: {t.get('present_workers', 0)}/{t.get('workers', 0)} 在场，"
               f"活跃 {t.get('active_workers', 0)}（最后活动 < {int(ACTIVE_WINDOW_SECONDS)}s）")
    out.append(f"- 运行时长: {_fmt_duration(t.get('duration_seconds'))}")
    out.append("")
    out.append("## 总量\n")
    out.append("| 指标 | 值 |")
    out.append("| --- | --- |")
    out.append(f"| 请求数 | {_fmt_int(t.get('requests'))} |")
    out.append(f"| 输入 token | {_fmt_int(tk.get('input'))} |")
    out.append(f"| 输出 token | {_fmt_int(tk.get('output'))} |")
    out.append(f"| 缓存读 | {_fmt_int(tk.get('cache_read'))} |")
    out.append(f"| 缓存写 | {_fmt_int(tk.get('cache_write'))} |")
    out.append(f"| reasoning | {_fmt_int(tk.get('reasoning'))} |")
    out.append(f"| 总 token | {_fmt_int(tk.get('total'))} |")
    out.append(f"| 估算成本 | {float(cost.get('total', 0) or 0):.6f} |")
    out.append(f"| 平均延迟 | {_fmt_ms(lat.get('avg_ms'))} |")
    out.append(f"| P50 延迟 | {_fmt_ms(lat.get('p50_ms'))} |")
    out.append(f"| P95 延迟 | {_fmt_ms(lat.get('p95_ms'))} |")
    out.append(f"| 最大延迟 | {_fmt_ms(lat.get('max_ms'))} |")
    out.append(f"| 延迟来源 | {lat.get('source') or 'none'} |")
    lat_ttfb = lat.get("ttfb_ms") or {}
    out.append(f"| TTFB 平均 / P95 | {_fmt_ms(lat_ttfb.get('avg_ms'))} / "
               f"{_fmt_ms(lat_ttfb.get('p95_ms'))} |")
    out.append(f"| 不可测延迟样本 | {_fmt_int(lat.get('unknown'))} |")
    out.append(f"| .calls 旁车请求数 | {_fmt_int(t.get('calls_count'))}"
               f"（{_fmt_int(t.get('calls_present_workers'))} 个 worker 有旁车） |")
    out.append(f"| 累计思考耗时 | {_fmt_ms(t.get('thinking_time_ms'))} |")
    out.append(f"| 工具调用 / 工具错误 | {_fmt_int(t.get('tool_calls'))} / "
               f"{_fmt_int(t.get('tool_errors'))} |")
    out.append(f"| type=error 事件 | {_fmt_int(t.get('error_events'))} |")
    out.append(f"| lifetime 速率 | {_fmt_rate(lt.get('tokens_per_minute'))} tok/min，"
               f"{_fmt_rate(lt.get('requests_per_minute'))} req/min |")
    out.append(f"| 窗口速率 | {_fmt_rate(win.get('tokens_per_minute'))} tok/min，"
               f"{_fmt_rate(win.get('requests_per_minute'))} req/min"
               f"（{win.get('basis')}，samples={win.get('samples')}） |")
    out.append("")

    out.append("## 按题目\n")
    out.append("| 题目 | worker | 活跃 | 请求 | 输入 | 输出 | 缓存读 | 总 token | "
               "平均延迟 | P95 | 延迟源 | calls | 工具 | 错误 | 时长 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for q in report.get("questions") or []:
        qt = q.get("tokens") or {}
        ql = q.get("latency_ms") or {}
        out.append(
            f"| {_md_cell(q.get('qid'))} | {q.get('workers')} | {q.get('active_workers')} | "
            f"{_fmt_int(q.get('requests'))} | {_fmt_int(qt.get('input'))} | "
            f"{_fmt_int(qt.get('output'))} | {_fmt_int(qt.get('cache_read'))} | "
            f"{_fmt_int(qt.get('total'))} | {_fmt_ms(ql.get('avg_ms'))} | "
            f"{_fmt_ms(ql.get('p95_ms'))} | {ql.get('source') or 'none'} | "
            f"{_fmt_int(q.get('calls_count'))} | {_fmt_int(q.get('tool_calls'))} | "
            f"{_fmt_int(q.get('error_events'))} | "
            f"{_fmt_duration(q.get('duration_seconds'))} |"
        )
    if not report.get("questions"):
        out.append("| (无) | | | | | | | | | | | | | | |")
    out.append("")

    out.append("## 按 worker\n")
    out.append("| 题目 | slot | 文件 | 请求 | 总 token | 平均延迟 | P95 | 延迟源 | "
               "calls | 工具 | 错误 | 最后活动 | 状态 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for w in report.get("workers") or []:
        wt = w.get("tokens") or {}
        wl = w.get("latency_ms") or {}
        if w.get("active"):
            status = "活跃"
        elif not w.get("present"):
            status = "已删除"
        else:
            status = f"静默 {_fmt_age(w.get('last_event_age_seconds'))}"
        out.append(
            f"| {_md_cell(w.get('qid'))} | {w.get('slot') if w.get('slot') is not None else '-'} | "
            f"`{_md_cell(w.get('file'))}` | {_fmt_int(w.get('requests'))} | "
            f"{_fmt_int(wt.get('total'))} | {_fmt_ms(wl.get('avg_ms'))} | "
            f"{_fmt_ms(wl.get('p95_ms'))} | {wl.get('source') or 'none'} | "
            f"{_fmt_int((w.get('calls') or {}).get('count'))} | "
            f"{_fmt_int(w.get('tool_calls'))} | "
            f"{_fmt_int(w.get('error_events'))} | "
            f"{w.get('last_event_at_local') or 'n/a'} | {status} |"
        )
    if not report.get("workers"):
        out.append("| (无) | | | | | | | | | | | | |")
    out.append("")

    out.append("## 统计口径\n")
    out.append("- 一次模型调用 = assistant `message_start` → 对应 `message_end`；"
               "请求数、token、成本只累加 assistant `message_end`。")
    out.append("- `turn_end` 会重复携带同一份 usage，**不参与累加**，避免双算。")
    out.append("- `reasoning` 通常是 output 的子集，只展示，不重复计入 total。")
    out.append("- 延迟优先读 `.calls` 旁车的 `duration_ms`（以及 `ttfb_ms`）；"
               "没有旁车才回退主日志的 `message_start` → `message_end` 配对。")
    out.append("- 主日志 `message_start`/`message_end` 共用同一 `message.timestamp` 时无法测延迟，"
               "计入 `unknown`，不伪造 0。")
    out.append("- 请求数/usage 只取主日志 assistant `message_end`；只有在主日志完全没有 usage 时，"
               "才用 `.calls` 的 usage 兜底并在 `usage_source` 标注。")
    out.append("- 无逐行 timestamp 时（pi 常见格式）用文件 mtime 判断最后活动；"
               "活跃 = 最后活动 < 60s。")
    out.append("- 窗口速率来自监控自身的增量观测；首次扫描没有历史时回退到 lifetime 速率。")
    out.append("")
    return "\n".join(out)


def render_compact(report: dict) -> str:
    t = report.get("totals") or {}
    tk = t.get("tokens") or {}
    lat = t.get("latency_ms") or {}
    th = report.get("throughput") or {}
    lt = th.get("lifetime") or {}
    win = th.get("window") or {}
    if win.get("basis") == "observation":
        tokens_pm = win.get("tokens_per_minute")
        requests_pm = win.get("requests_per_minute")
    else:
        tokens_pm = lt.get("tokens_per_minute")
        requests_pm = lt.get("requests_per_minute")
    local = report.get("generated_at_local") or ""
    clock = local[11:19] if len(local) >= 19 else local
    return (f"[{clock}] worker {t.get('present_workers', 0)}/"
            f"{t.get('workers', 0)} 在场（活跃 {t.get('active_workers', 0)}）"
            f" | 请求 {_fmt_int(t.get('requests'))}"
            f" | token {_fmt_int(tk.get('total'))}"
            f" | {_fmt_rate(tokens_pm)} tok/min"
            f" | {_fmt_rate(requests_pm)} req/min"
            f" | P95[{lat.get('source') or 'none'}] {_fmt_ms(lat.get('p95_ms'))}"
            f" | 错误 {_fmt_int(t.get('error_events'))}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="监控 Rotom worker 的 AI 用量与计时（只读 logs/*.jsonl）。")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="扫描一次并打印摘要（默认）")
    mode.add_argument("--watch", action="store_true",
                      help="常驻，每 --interval 秒增量扫描并写 ai_usage.json/md")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="--watch 的扫描间隔秒数（默认 5）")
    ap.add_argument("--run-dir", default=None,
                    help="ROTOM_RUN_DIR 覆盖；默认取环境变量或项目根目录")
    ap.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS,
                    help=f"窗口速率窗口大小（默认 {int(DEFAULT_WINDOW_SECONDS)}s）")
    ap.add_argument("--state-file", default=None,
                    help="增量状态文件路径（默认 logs/.ai_monitor_state.json）")
    ap.add_argument("--reset-state", action="store_true",
                    help="删除增量状态后全量重扫")
    ap.add_argument("--no-write", action="store_true",
                    help="不写 ai_usage.json/md（仍更新增量状态）")
    ap.add_argument("--json", action="store_true",
                    help="--once 时直接输出 JSON（方便脚本消费）")
    ap.add_argument("--quiet", action="store_true",
                    help="--watch 时不打印每轮状态行")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.interval <= 0:
        ap.error("--interval 必须大于 0")
    if args.window_seconds <= 0:
        ap.error("--window-seconds 必须大于 0")

    run_dir = Path(args.run_dir or os.environ.get("ROTOM_RUN_DIR") or ROOT)
    monitor = AiMonitor(run_dir, window_seconds=args.window_seconds,
                        state_path=args.state_file)
    if args.reset_state:
        try:
            monitor.state_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[ai_monitor] 删除状态失败: {exc}", file=sys.stderr)
        monitor.state = _fresh_state(monitor.run_dir)

    if args.watch:
        try:
            while True:
                try:
                    report = monitor.scan(write_outputs=True)
                    if not args.quiet:
                        print(render_compact(report), flush=True)
                except Exception as exc:  # 常驻进程不应因单轮异常退出
                    print(f"[ai_monitor] 扫描失败: {exc}", file=sys.stderr, flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[ai_monitor] 已停止", file=sys.stderr)
        return 0

    report = monitor.scan(write_outputs=not args.no_write)
    if args.json:
        print(json.dumps(build_contract(report), ensure_ascii=False,
                         indent=2, allow_nan=False))
    else:
        print(render_summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())