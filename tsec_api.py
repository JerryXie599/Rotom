#!/usr/bin/env python3
"""TSec Benchmark Platform 适配器(众测 / 漏洞挖掘评测)。

与 contest_api / arena_api 保持**同名同签名**,runner 与 tools/submit_flag.py 无需改代码即可复用:
    list_questions(token) / submit_flag(...) / reset_env(...) / is_correct / is_verdict
    submit_with_retry(...)

平台接口(见 CHALLENGES_API.md),全部走 HTTP 头 `BENCHMARK_TOKEN` 鉴权:
    GET  /openapi/v1/challenges                   题目列表 + 作答进度
    POST /openapi/v1/challenges/start?unique_code= 启动容器(返回 container_addr)
    GET  /openapi/v1/challenges/hint?unique_code=  取提示(扣分)
    POST /openapi/v1/challenges/submit             提交 flag
    POST /openapi/v1/challenges/close?unique_code= 关闭容器

与 CTF 平台的三点根本差异(本适配器专门处理):
  1. **容器要自己开、自己关,且同时最多 3 道** —— 见 sync_containers()。
  2. **一道题多个 flag** —— 提交返回 matched_flag_index / correct_flag_count,
     全部 flag 拿到才算通关,不能像 CTF 那样"第一个 flag 命中就收工"。
  3. **题目入口在靶场 VPN 内** —— 开工前必须过 check_vpn() 预检。

启用方式:CONTEST_ADAPTER=tsec(见 .env / start.sh)。
"""

from __future__ import annotations

import json
import os
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE = "https://benchmark.tsec.example.com"   # 现场由平台下发,填在 .env 的 BENCHMARK_BASE_URL
API_PREFIX = "/openapi/v1/challenges"

RETRIES = 3
RETRY_DELAY = 2.0
DEFAULT_TIMEOUT = 40
# 实测:平台 start/close 是"几十秒级"的慢接口(close 都要 28s),所以
#   ① 给足超时;② **绝不在主循环里同步等** —— 见下面的异步容器操作;③ 超时后不重试(平台可能已受理)。
START_TIMEOUT = int(os.environ.get("CONTAINER_START_TIMEOUT", "150") or 150)
CLOSE_TIMEOUT = int(os.environ.get("CONTAINER_CLOSE_TIMEOUT", "120") or 120)
OP_STALE = 300            # 在途操作超过这么久仍未返回 → 当作失败,允许重试
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE_CHALLENGES", "3") or 3)

# 平台接口必须直连:本机代理会破坏 TLS(实测教训,同 contest_api)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

VPN_CHECK_URL = os.environ.get("VPN_CHECK_URL", "http://10.0.100.58")

# 最近一次 list_challenges 的原始结果:sync_containers 靠它判断容器状态,
# 避免为了拿状态反复打接口(平台接口有频率限制)。
_LAST_LIST: list[dict] = []
_LAST_AT = 0.0                        # 上次拉列表的时间(判断缓存是否过期)

# ---- 异步容器操作:start/close 丢给线程池,调度主循环只管发单和收结果 ----
_CTR_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tsec-ctr")
_inflight: dict[str, dict] = {}      # qid -> {"op": "start"/"close", "at": ts, "fut": future}
_last_op: dict[str, dict] = {}       # qid -> {"op", "ok", "err", "at"} 最近一次操作结果
_start_at: dict[str, float] = {}     # qid -> 上次发 start 的时间(防重复开容器)
LIST_TTL = 6.0                       # 缓存超过这么久就自己重新拉一次(不依赖调用方)
START_COOLDOWN_OK = 120.0            # start 成功后多久内不再重发(平台拉起要时间,列表还没更新)
START_COOLDOWN_FAIL = 20.0           # start 失败后多久再试


class ContestError(RuntimeError):
    """接口通了但业务失败(与 contest_api.ContestError 同名同义)。

    task_over=True 表示**跑分任务已结束/凭证失效**,不该再继续派题(超时、token 无效)。
    """

    def __init__(self, code, message: str, kind: str = "", http_status: int = 0):
        self.code = code
        self.message = message
        self.kind = kind
        self.http_status = http_status
        self.task_over = False
        super().__init__(f"[{kind or code}] {message}")


def _from_env_file(key: str) -> str:
    """环境变量没设时回退读 .env —— 让 tools/netcheck.py 这类直接跑的入口也能拿到真实地址,
    不会莫名其妙连到占位域名上去(实测踩过:DNS 报错看着像网络问题,其实是配置没加载)。"""
    if os.environ.get(key):
        return ""
    try:
        import config as _config
        return str(_config.read_env().get(key) or "")
    except Exception:
        return ""


def base_url() -> str:
    url = (os.environ.get("BENCHMARK_BASE_URL") or _from_env_file("BENCHMARK_BASE_URL")
           or DEFAULT_BASE).strip().rstrip("/")
    return url


def token_of(explicit: str = "") -> str:
    tok = (explicit or os.environ.get("BENCHMARK_TOKEN") or os.environ.get("TEAM_TOKEN")
           or _from_env_file("BENCHMARK_TOKEN") or "").strip()
    if not tok:
        raise ContestError("task_not_found", "未配置 BENCHMARK_TOKEN(跑分任务凭证)", "鉴权", 404)
    return tok


def _url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base_url() + path


# ------------------------------------------------------------------ HTTP 底层

_TRANSIENT_CODES = {"resource_unavailable", "internal_error", "network_error"}


def _call(token: str, method: str, path: str, params: dict | None = None,
          body: dict | None = None, timeout: int = DEFAULT_TIMEOUT,
          retries: int = RETRIES, retry_timeouts: bool = True) -> dict | list:
    """发一次平台请求;业务错误抛 ContestError,网络抖动自动重试。

    retry_timeouts=False 用于 start/close 这类慢接口:客户端超时不代表平台没受理,
    重试只会重复下发、白等更久 —— 交给调度循环看平台状态决定要不要再发。
    """
    url = _url(path)
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "BENCHMARK_TOKEN": token_of(token),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "rotom-agent/1.0",
    })
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8", "ignore"))
            except Exception:
                payload = {}
            code = str(payload.get("code") or f"http_{e.code}")
            msg = str(payload.get("message") or f"HTTP {e.code}")
            err = ContestError(code, msg, path, e.code)
            if code == "invalid_state":
                low = msg.lower()
                # 三种 invalid_state:任务已结束 / 活跃容器达上限 / 通关后看 hint。
                # 只有"达上限"是可以靠关掉一道题自救的,其余两种必须停下来。
                if not any(k in low or k in msg for k in ("max active", "active", "上限", "活跃")):
                    err.task_over = True
            elif code == "task_not_found":
                err.task_over = True          # token 无效 → 停,别空转
            if code in _TRANSIENT_CODES and attempt < retries - 1:
                last = err
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            raise err
        except ContestError:
            raise
        except Exception as e:                # 连接失败/超时/坏 JSON
            last = e
            if attempt < retries - 1:
                if not retry_timeouts:
                    raise ContestError("network_error", f"请求超时/失败(不重试): {e}", path)
                time.sleep(RETRY_DELAY * (attempt + 1))
    raise ContestError("network_error", f"请求失败({retries} 次重试后): {last}", path)


# ------------------------------------------------------------------ VPN 预检

def check_vpn(timeout: int = 8) -> dict:
    """靶场 VPN 联通预检:GET http://10.0.100.58 必须返回 status == "ok"。

    这是整个流程的**强制前置**:地址只在 VPN 网络内可达,所以它是 VPN 是否连通的可靠判据。
    返回 {"ok": bool, "client_ip":..., "error":...},不抛异常(调用方决定怎么处置)。
    """
    url = (os.environ.get("VPN_CHECK_URL") or VPN_CHECK_URL).strip()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rotom-agent/1.0"})
        with _OPENER.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        ok = str(data.get("status", "")).lower() == "ok"
        return {"ok": ok, "client_ip": data.get("client_ip"), "raw": data,
                "error": "" if ok else f"status != ok: {data}"}
    except Exception as e:
        return {"ok": False, "client_ip": None, "raw": {}, "error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ 题目列表

# 描述里的关键词 → 方向(决定注入哪份 playbook)。众测以 web 为主,默认就是 pentest。
_CATEGORY_HINTS = (
    ("crypto", ("rsa", "aes", "密码", "加密", "cipher", "hash", "哈希")),
    ("pwn", ("pwn", "溢出", "binary", "栈", "堆", "rop", "格式化字符串")),
    ("reverse", ("逆向", "reverse", "反编译", "apk", "脱壳")),
    ("forensics", ("取证", "forensics", "流量", "pcap", "内存镜像", "隐写")),
)

# 派题优先级里的难度排序(平台给的是 easy/medium/hard):先拿能拿的分
_DIFF_RANK = {"easy": 0, "简单": 0, "medium": 1, "中等": 1, "hard": 2, "困难": 2}


def _infer_category(ch: dict) -> str:
    blob = " ".join(str(ch.get(k) or "") for k in ("unique_code", "description")).lower()
    for cat, words in _CATEGORY_HINTS:
        if any(w in blob for w in words):
            return cat
    return "pentest"


def _conn(addr_list: list) -> dict:
    """容器地址 → 提示词里的目标信息。众测目标绝大多数是 web,默认补一个 http:// 前缀。"""
    conn: dict = {}
    for i, a in enumerate(addr_list or [], start=1):
        a = str(a).strip()
        if not a:
            continue
        if a.startswith("http://") or a.startswith("https://"):
            conn[f"target{i}"] = a
            conn[f"url{i}"] = a
        else:
            conn[f"target{i}"] = a
            conn[f"url{i}"] = f"http://{a}"
    return conn


def _to_question(ch: dict) -> dict:
    qid = str(ch.get("unique_code") or "").strip()
    addrs = list(ch.get("container_addr") or [])
    status = str(ch.get("container_status") or "stopped")
    ready = status == "available" and bool(addrs)
    desc = str(ch.get("description") or "").strip()
    if ch.get("difficulty") or ch.get("level"):
        desc += f"\n\n(难度 {ch.get('difficulty') or '-'} · 关卡 {ch.get('level') or '-'} · " \
                f"满分 {ch.get('total_score') or 0} · 本题共 {ch.get('flag_count') or 1} 个 flag)"
    return {
        "question_id": qid,
        "title": qid,                      # 平台只给 unique_code,工作区目录名以它为准
        "category": _infer_category(ch),
        "score": int(ch.get("total_score") or 0),
        "description": desc,
        "difficulty": ch.get("difficulty") or "",
        "level": ch.get("level") or 0,
        "flag_count": int(ch.get("flag_count") or 1),
        "correct_flag_count": int(ch.get("correct_flag_count") or 0),
        "is_solved": bool(ch.get("is_completed")),
        "file_url": "",
        "file_name": "",
        "interactive": "true",
        "connection": _conn(addrs) if ready else {},
        "container_status": status,
        "container_addr": addrs,
        "container_ready": ready,
        "attributes": [], "capabilities": [],
        "extensions": {"difficulty": ch.get("difficulty"), "level": ch.get("level"),
                       "total_score": ch.get("total_score"), "flag_count": ch.get("flag_count")},
    }


def list_challenges(token: str = "", timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """原始题目列表(含 container_status / container_addr)。"""
    global _LAST_AT
    data = _call(token, "GET", API_PREFIX, timeout=timeout)
    if isinstance(data, dict):                 # 容错:有的实现会包一层 data
        data = data.get("data") or []
    if not isinstance(data, list):
        raise ContestError("bad_response", f"题目列表格式异常: {str(data)[:120]}", "查题")
    _LAST_LIST.clear()
    _LAST_LIST.extend([c for c in data if isinstance(c, dict)])
    _LAST_AT = time.time()
    return list(_LAST_LIST)


def list_questions(token: str = "", timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """runner 用的题目列表:平台字段 → harness 通用题目结构。"""
    return [_to_question(c) for c in list_challenges(token, timeout=timeout)]


def cached_status() -> dict[str, dict]:
    return {str(c.get("unique_code")): c for c in _LAST_LIST}


# ------------------------------------------------------------------ 容器生命周期

def start_challenge(token: str, qid: str, timeout: int = START_TIMEOUT) -> list[str]:
    """启动一道题的容器,返回 container_addr(IP:端口)列表(同步调用;慢,别在调度循环里直接用)。"""
    data = _call(token, "POST", f"{API_PREFIX}/start", params={"unique_code": qid},
                 timeout=timeout, retry_timeouts=False)
    addrs = list((data or {}).get("container_addr") or [])
    if not addrs:
        raise ContestError("bad_response", f"start 返回里没有 container_addr: {data}", "启动容器")
    return addrs


def close_challenge(token: str, qid: str, timeout: int = CLOSE_TIMEOUT) -> bool:
    """关闭并释放容器名额(同步调用;实测也要 30s 左右)。题目不存在/已关闭都算成功(幂等)。"""
    try:
        data = _call(token, "POST", f"{API_PREFIX}/close", params={"unique_code": qid},
                     timeout=timeout, retry_timeouts=False)
    except ContestError as e:
        if e.code in ("challenge_not_found", "invalid_state"):
            return False
        raise
    return bool((data or {}).get("closed"))


# ------------------------------------------------------------------ 异步容器操作
# 平台 start/close 都是几十秒级:同步调用会把 reap/observe/reconcile 全卡住,
# 所以调度循环只负责"发单",结果由后台线程回填,循环下一轮直接读平台状态。

def _reap_op(qid: str) -> str:
    """把已完成的操作结果收下来,返回该题当前在途的操作名('start'/'close'/'')。"""
    it = _inflight.get(qid)
    if not it:
        return ""
    fut = it["fut"]
    if fut.done():
        _inflight.pop(qid, None)
        try:
            fut.result()
            _last_op[qid] = {"op": it["op"], "ok": True, "err": "", "at": time.time()}
        except Exception as e:
            _last_op[qid] = {"op": it["op"], "ok": False,
                             "err": f"{type(e).__name__}: {e}", "at": time.time()}
        return ""
    if time.time() - it["at"] > OP_STALE:      # 卡太久:放弃这个在途标记,允许重发
        _inflight.pop(qid, None)
        _last_op[qid] = {"op": it["op"], "ok": False, "err": "在途超时", "at": time.time()}
        return ""
    return it["op"]


def start_async(token: str, qid: str) -> None:
    """后台拉起容器(已有在途操作则跳过)。"""
    if _reap_op(qid):
        return
    _start_at[qid] = time.time()
    _inflight[qid] = {"op": "start", "at": time.time(),
                      "fut": _CTR_POOL.submit(start_challenge, token, qid)}


def _start_gate(qid: str, now: float) -> bool:
    """现在能不能给这道题发 start。

    平台拉起容器要几十秒,期间列表仍显示 stopped —— 不加冷却就会反复重发 start
    (既浪费又可能占额度)。所以按"上次发单时间 + 冷却"来节流。
    """
    last = _last_op.get(qid) or {}
    cool = START_COOLDOWN_OK if (last.get("op") == "start" and last.get("ok")) else START_COOLDOWN_FAIL
    return now - _start_at.get(qid, 0) >= cool


def close_async(token: str, qid: str) -> None:
    """后台释放容器(已有在途操作则跳过)。"""
    if _reap_op(qid):
        return
    _inflight[qid] = {"op": "close", "at": time.time(),
                      "fut": _CTR_POOL.submit(close_challenge, token, qid)}


def pending_ops() -> dict[str, str]:
    """当前在途的容器操作 {qid: 'start'/'close'}(给看板/日志用)。"""
    return {q: it["op"] for q, it in list(_inflight.items())}


def wait_ops(timeout: float = 60.0) -> None:
    """等所有在途容器操作收尾(停机时用,避免容器没人关)。"""
    end = time.time() + timeout
    while _inflight and time.time() < end:
        for qid in list(_inflight):
            _reap_op(qid)
        if _inflight:
            time.sleep(0.5)


def get_hint(token: str, qid: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """取提示(会按 hint_cost_radio 扣该题后续得分)。默认不使用,见 runner 的 HINT_AFTER_ATTEMPTS。"""
    data = _call(token, "GET", f"{API_PREFIX}/hint", params={"unique_code": qid}, timeout=timeout)
    return (data or {}).get("hint")


def _rank(e: dict) -> tuple:
    """派题优先级:**有 agent 正在干的题先保名额** → 用掉完整机会少的优先 → 关卡低 → 难度低 → 已有进展。

    第一位 `hold_container` 由 runner 每轮标注(有 agent 在跑 / 刚起来在保护期):
    这种题的容器名额优先留着 —— 实测 24 个会话死于"容器已释放",而且都是投入最深的那个
    (中位 15 分钟 / 30 次调用),把正在干活的 agent 的靶子收走等于把深链题的努力清零。

    用 `slices`(已用掉几个完整时间片)而不是 `attempts`(worker 启动次数):
    同一题起 2 个 agent 会让 attempts=2,把"其实只跑过一个时间片"的题误判成"已经给过两次机会",
    于是被排到队尾(实测:6 道 easy 题被 15 分钟切掉后要等 5 小时才轮到)。

    这个排序只用于**抢空出来的名额**;已经拿着名额的题不受影响(见 sync_containers 的"现任优先")。
    """
    return (0 if e.get("hold_container") else 1,
            1 if e.get("chain_beyond_cap") else 0,     # 超出钉名额上限的链式题 → 排到普通题之后
            int(e.get("slices") or 0), int(e.get("level") or 0),
            _DIFF_RANK.get(str(e.get("difficulty") or "").lower(), 9),
            -int(e.get("correct_flag_count") or 0))


# ------------------------------------------------------------------ 容器服务级探活
# 平台的 container_status 只说明"资源占着",不代表容器里的服务还活着。
# 实测(2026-09-29 第四次跑分 c-02):agent 触发一次**同步 git clone** 把 ComfyUI 的事件循环阻塞死,
# 容器先 refused、后"TCP 能连但不回任何 HTTP",而平台一直报 available —— harness 于是永远不换它;
# 只剩最后一题未解时 rotate() 又不会轮换(题比名额少),这题被判死刑,最后靠人工 close 才救回来。
# 这里补一层服务级探活:TCP 通 **且**(像 HTTP 的端口)一次 GET 有响应才算活;连续 N 次判死 → 强制
# close,下一轮 sync 自然拉起新容器(地址变更仍走 runner 的 _accept_conn / dead_addrs / 黑板刷新)。

_HEALTH_FAILS: dict[str, int] = {}       # qid -> 连续判死次数
_HEALTH_LAST: dict[str, float] = {}      # qid -> 上次发探测的时间
_HEALTH_HOLD: dict[str, float] = {}      # qid -> 冷却截止(刚换过容器,等它起来再探)
_HEALTH_HEALS: dict[str, int] = {}       # qid -> 已强制换过几次容器(按题记,不清零)
_HEALTH_ADDR: dict[str, str] = {}        # qid -> 上次探测的地址(地址变了计数清零)
_HEALTH_FUT: dict[str, object] = {}      # qid -> 在途探测 future
# 这些端口按 HTTP 探。端口本身不代表协议,但跑分靶场里这些基本都是 web/HTTP 服务;
# 不在表里的端口只做 TCP 探(裸 TCP 服务探 HTTP 会误判成"死")。
HTTPISH_PORTS = {80, 81, 88, 443, 3000, 3001, 5000, 5001, 5601, 7001, 7002, 8000, 8008,
                 8080, 8081, 8088, 8090, 8161, 8188, 8443, 8787, 8888, 8983, 9000, 9090,
                 9200, 9300, 10000, 11434, 18080}


def _parse_addr(addr: str) -> tuple[str, int] | None:
    """把 "10.0.1.2:8080" / "http://10.0.1.2:8080" / "10.0.1.2" 拆成 (host, port)。"""
    s = str(addr or "").strip()
    if not s:
        return None
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0]
    if s.startswith("[") and "]" in s:               # IPv6 字面量
        host, _, rest = s[1:].partition("]")
        port = rest.lstrip(":")
        return (host, int(port)) if port.isdigit() else None
    host, sep, port = s.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return (s, 80) if s else None


def _probe_alive(addr: str, tcp_timeout: float = 3.0, http_timeout: float = 6.0,
                 http_mode: str = "auto") -> tuple[bool, str]:
    """探一次容器服务:TCP 通;若该做 HTTP 校验,再要求"任何 HTTP 响应"(不看状态码/内容)。

    只看 TCP 不够 —— 实测目标死锁时 TCP SYN 仍被内核接受(backlog),但应用层永远不回。
    `http_mode`:auto(默认,按端口猜)/ always / never。返回 (是否活着, 原因短语)。
    """
    hp = _parse_addr(addr)
    if not hp:
        return True, "地址无法解析,跳过探活"
    host, port = hp
    try:
        with socket.create_connection((host, port), timeout=tcp_timeout):
            pass
    except Exception as e:
        return False, f"TCP 不通({type(e).__name__})"
    mode = (http_mode or "auto").strip().lower()
    if mode == "never" or (mode != "always" and port not in HTTPISH_PORTS):
        return True, "TCP ok"
    req = urllib.request.Request(f"http://{host}:{port}/", method="GET",
                                 headers={"User-Agent": "rotom-health/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 探活绝不过代理
    try:
        with opener.open(req, timeout=http_timeout) as resp:
            resp.read(256)
        return True, "TCP+HTTP ok"
    except urllib.error.HTTPError:
        return True, "TCP+HTTP ok(有响应即活,4xx/5xx 不算死)"
    except Exception as e:
        return False, f"HTTP 无响应({type(e).__name__})"


def _health_forget(qid: str) -> None:
    """题目已经不是 available/已解出 → 清掉探活状态(保留 _HEALTH_HEALS,换容器次数按题记)。"""
    _HEALTH_FAILS.pop(qid, None)
    _HEALTH_LAST.pop(qid, None)
    _HEALTH_HOLD.pop(qid, None)
    _HEALTH_ADDR.pop(qid, None)
    _HEALTH_FUT.pop(qid, None)


def _hnum(health: dict, key: str, default: float) -> float:
    """读探活旋钮。**不能用 `x or default`**:0 是合法值(=每轮都探/阈值 0),`0 or 90` 会静默变 90。"""
    v = (health or {}).get(key)
    if v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _health_tick(token: str, status: dict, by_qid: dict, now: float,
                 health: dict | None) -> dict[str, str]:
    """服务级探活一步:发探测 / 收结果 / 判死换容器。返回 {qid: 说明} 给 runner 记日志。"""
    notes: dict[str, str] = {}
    if not health or not health.get("enabled", False):
        return notes
    interval = _hnum(health, "probe_interval_s", 90)
    threshold = int(_hnum(health, "fail_threshold", 3))
    max_heal = int(_hnum(health, "max_heal_per_question", 3))
    cooldown = _hnum(health, "heal_cooldown_s", 240)
    tcp_timeout = _hnum(health, "tcp_timeout_s", 3)
    http_timeout = _hnum(health, "http_timeout_s", 6)
    http_mode = str(health.get("http_check") or "auto")

    for qid, c in status.items():
        e = by_qid.get(qid)
        usable = (e is not None and not e.get("is_solved")
                  and str(c.get("container_status") or "") == "available"
                  and bool(c.get("container_addr")))
        if not usable:
            _health_forget(qid)
            continue
        addr = str((c.get("container_addr") or [""])[0])
        if _HEALTH_ADDR.get(qid) != addr:            # 换了地址(容器重开)→ 旧计数作废
            _HEALTH_ADDR[qid] = addr
            _HEALTH_FAILS[qid] = 0
        fut = _HEALTH_FUT.get(qid)
        if fut is not None and getattr(fut, "done", lambda: False)():
            _HEALTH_FUT.pop(qid, None)
            try:
                alive, why = fut.result()
            except Exception as ex:
                alive, why = False, f"探测异常 {type(ex).__name__}"
            if alive:
                if int(_HEALTH_FAILS.get(qid) or 0):
                    notes[qid] = f"服务探活恢复({why})"
                _HEALTH_FAILS[qid] = 0
            else:
                n = int(_HEALTH_FAILS.get(qid) or 0) + 1
                _HEALTH_FAILS[qid] = n
                if n >= threshold:
                    heals = int(_HEALTH_HEALS.get(qid) or 0)
                    if heals < max_heal and not _inflight.get(qid):
                        _HEALTH_HEALS[qid] = heals + 1
                        _HEALTH_FAILS[qid] = 0
                        _HEALTH_HOLD[qid] = now + cooldown
                        close_async(token, qid)          # 关掉 → 下一轮 sync 自动拉新容器
                        notes[qid] = (f"服务探活连续 {n} 次判死({why})→ 强制换容器"
                                      f"(第 {heals + 1}/{max_heal} 次;旧容器里的应用已无响应,"
                                      f"平台状态却是 available)")
                    elif heals >= max_heal:
                        notes[qid] = (f"服务探活连续 {n} 次判死({why}),已换过 {max_heal} 次容器,"
                                      f"不再自动重启(留给人工判断/换题)")
        if qid in _HEALTH_FUT:
            continue
        if now < float(_HEALTH_HOLD.get(qid) or 0):          # 刚换过容器,等它起来再探
            continue
        if now - float(_HEALTH_LAST.get(qid) or 0) < interval:
            continue
        _HEALTH_LAST[qid] = now
        _HEALTH_FUT[qid] = _CTR_POOL.submit(_probe_alive, addr, tcp_timeout, http_timeout, http_mode)
    return notes


def sync_containers(token: str, entries: list[dict], max_active: int = 0,
                    health: dict | None = None) -> dict[str, dict]:
    """容器调度:把"想跑的题"的容器拉起来(最多 max_active 个),其余的关掉释放名额。

    **现任优先**:已经开着容器的题保住自己的名额,只有 rotate()(时间片用满且无进展)能让它让位。
    空出来的名额才由排队题竞争(排序见 _rank)。
    少了"现任优先"会怎样:一道题刚拿到尝试机会、attempts 从 0 变 1,就被排到所有没碰过的题后面,
    下一轮容器立刻被关掉 —— 实测 26 秒换一批容器,agent 永远打不到活目标。

    **先关后开**:平台硬上限 3 个,如果先 start 再 close,会因为"名额还占着"吃一发
    invalid_state(max active)白等一轮。所以顺序是:关掉不在名单里的 → 再拉起名单里的。

    **全程不阻塞**:start/close 都是几十秒级的慢接口,这里只"发单"到线程池,
    下一轮循环再读平台状态收结果。所以调用方要按 POLL_INTERVAL 反复调用本函数。

    `health`(来自策略包 `containers.health`)开启后,还会对 available 的容器做**服务级探活** —— 见
    `_probe_alive`:平台状态只说明"资源占着",不代表容器里的服务还活着。

    返回 {qid: {"container_status", "container_addr", "connection", "container_ready", "note"}}。
    """
    max_active = max_active or MAX_ACTIVE
    now = time.time()
    if now - _LAST_AT > LIST_TTL:                # 缓存过期就自己刷一次,不依赖调用方先拉列表
        try:
            list_challenges(token)
        except ContestError as e:
            if getattr(e, "task_over", False):
                raise
    # 先把**所有**在途操作的结果收下来(包括已经离开名单的题)。
    # 漏收会让 closing 永远为真 → 再也不敢开新容器,整个调度卡死(实测踩过)。
    for _qid in list(_inflight):
        _reap_op(_qid)
    status = cached_status()
    out: dict[str, dict] = {}

    by_qid = {e["question_id"]: e for e in entries}

    # 服务级探活(策略包 containers.health 开关):平台说 available 不代表服务活着。
    # 判死会在这里直接发 close,下面这段调度逻辑随后会看到"有在途 close"而不去拉它,
    # 等平台回收完再由 ④ 拉起新容器。
    health_notes = _health_tick(token, status, by_qid, now, health)

    def alive(e: dict) -> bool:
        return not e.get("is_solved") and now >= float(e.get("rotate_until") or 0)

    # ① 现任:正开着容器(或正在拉起)的题保住名额
    holding = [qid for qid, c in status.items()
               if str(c.get("container_status") or "") in ("available", "pending")
               and qid in by_qid and alive(by_qid[qid])]
    holding.sort(key=lambda q: _rank(by_qid[q]))
    desired = holding[:max_active]

    # ② 空出来的名额:让排队题按优先级竞争
    room = max_active - len(desired)
    if room > 0:
        queue = sorted((e for e in entries if alive(e) and e["question_id"] not in desired),
                       key=_rank)
        desired += [e["question_id"] for e in queue[:room]]

    # ③ 不在名单里的题:把还开着的容器关掉(异步),名额让出来
    for qid, c in status.items():
        if qid in desired:
            continue
        if str(c.get("container_status") or "") in ("available", "pending"):
            close_async(token, qid)
    # close 也是异步的:还有关闭在途时**不要**急着开新的,否则会撞平台"活跃容器已达上限"
    closing = any(it["op"] == "close" for it in _inflight.values())

    # ④ 拉起名单里的(慢接口丢后台,这轮先当"待拉起")
    for qid in desired:
        c = status.get(qid) or {}
        st = str(c.get("container_status") or "stopped")
        addrs = list(c.get("container_addr") or [])
        op = _reap_op(qid)                       # 顺带收一下上一轮的结果
        note = ""
        last = _last_op.get(qid) or {}
        if last and not last.get("ok") and now - last.get("at", 0) < 120:
            note = f"上次 {last.get('op')} 失败: {last.get('err')}"
        if health_notes.get(qid):                # 探活判死/恢复比"上次操作失败"更值得记一笔
            note = health_notes[qid]
        if st == "available" and addrs:
            pass                                 # 已经在跑,直接复用(别重复 start)
        elif st in ("pending", "stop_pending") or op:
            addrs = []                           # 平台正在拉起/回收,等下一轮
        elif closing:
            addrs = []                           # 有名额正在释放,等它关完再开(否则撞上限)
        elif _start_gate(qid, now):
            start_async(token, qid)
            addrs = []
        else:
            addrs = []                           # 刚发过 start,等平台拉起(别重复发)
        out[qid] = {"container_status": st, "container_addr": addrs,
                    "connection": _conn(addrs) if addrs else {},
                    "container_ready": bool(addrs) and st == "available",
                    "note": note}

    return out


def close_all(token: str, wait: float = 90.0) -> int:
    """收工:把当前还开着的容器全部关掉(异步发出,最多等 wait 秒收结果)。"""
    n = 0
    for qid, c in cached_status().items():
        if str(c.get("container_status") or "") in ("available", "pending"):
            close_async(token, qid)
            n += 1
    if n:
        wait_ops(wait)
    return n


# ------------------------------------------------------------------ 提交 flag

def _err(code, message: str, **extra) -> dict:
    d = {"code": code, "message": message}
    d.update(extra)
    return d


def submit_flag(token: str, question_id: str, answer: str,
                timeout: int = DEFAULT_TIMEOUT) -> dict:
    """提交一个 flag。**不抛业务异常**,统一映射成 contest_api 形状的 dict:

      code == 0                        → 平台受理了(看 correct 判断对错)
      code == "duplicate" 已被消解      → 直接当"正确(此前已提交过)"
      其它 code                        → 平台没受理,由 is_verdict/is_rate_limited 分类
    """
    try:
        data = _call(token, "POST", f"{API_PREFIX}/submit",
                     body={"unique_code": question_id, "flag": answer}, timeout=timeout)
    except ContestError as e:
        if e.code == "duplicate":
            # 幂等保护:同一个 flag 之前已正确提交过 → 它就是对的,别让 agent 改格式重推
            return {"code": 0, "correct": True, "duplicate": True, "status": 1,
                    "message": "答案正确(该 flag 此前已提交过,不重复计分)"}
        return _err(e.code, e.message, http_status=e.http_status, task_over=e.task_over)
    except Exception as e:                     # 网络层异常 → 可重试
        return _err("network_error", f"网络错误: {e}")

    data = data or {}
    correct = bool(data.get("correct"))
    return {
        "code": 0,
        "correct": correct,
        "status": 1 if correct else 0,
        "message": "回答正确" if correct else "答案错误",
        "awarded": int(data.get("awarded") or 0),
        "cumulative_score": int(data.get("cumulative_score") or 0),
        "correct_flag_count": data.get("correct_flag_count"),
        "total_flag_count": data.get("total_flag_count"),
        "matched_flag_index": data.get("matched_flag_index"),
        "raw": data,
    }


def is_verdict(resp: dict) -> bool:
    """这份响应是不是"平台对答案的判定"(受理了)。code != 0 一律是没受理。"""
    return resp.get("code") == 0


def is_rate_limited(resp: dict) -> bool:
    """暂时性错误(靶场资源紧张 / 平台内部错误 / 网络抖动):退避重试即可。"""
    if resp.get("code") == 0:
        return False
    return resp.get("code") in _TRANSIENT_CODES


def is_correct(resp: dict) -> bool:
    if resp.get("code") != 0:
        return False
    return bool(resp.get("correct")) or resp.get("status") == 1 or "正确" in str(resp.get("message", ""))


FALSE_NEGATIVE_RETRIES = int(os.environ.get("FALSE_NEGATIVE_RETRIES", "2") or 0)
# 暂时性错误(限流/资源紧张)的重试预算;做成可调是为了让测试能压到 1 次 / 0 延迟
SUBMIT_ATTEMPTS = int(os.environ.get("SUBMIT_ATTEMPTS", "5") or 5)
SUBMIT_BASE_DELAY = float(os.environ.get("SUBMIT_BASE_DELAY", "1.5") or 1.5)


def submit_with_retry(token: str, question_id: str, answer: str, timeout: int = DEFAULT_TIMEOUT,
                      attempts: int | None = None, base_delay: float | None = None) -> dict:
    """提交重试,两层:

    ① 暂时性错误(限流/资源紧张)→ 退避重试;
    ② **平台假阴性**(2026-09-28 第三次测试实测):正确 flag 偶发返回 correct:false,
       **同一字符串**原样重试 1~2 次后即入账(该轮 f2-08/f2-01/f2-02 都靠这个拿的分)。
       所以对"受理了但判错"的响应再做**同串**重试(FALSE_NEGATIVE_RETRIES,默认 2,0=关)。
       注意:只重试**完全相同**的字符串 —— 换变体/近似值属于"猜 flag",提示词里是禁止的;
       重试仍错就如实返回 WRONG,让 agent 回去重新提取。
    """
    last: dict = {}
    attempts = SUBMIT_ATTEMPTS if attempts is None else attempts
    base_delay = SUBMIT_BASE_DELAY if base_delay is None else base_delay
    for i in range(attempts):
        resp = submit_flag(token, question_id, answer, timeout=timeout)
        last = resp
        if is_verdict(resp) or not is_rate_limited(resp):
            break
        if i < attempts - 1:
            time.sleep(base_delay * (i + 1) + random.uniform(0, 0.7))
    if (is_verdict(last) and not is_correct(last) and not last.get("duplicate")
            and FALSE_NEGATIVE_RETRIES > 0):
        for k in range(FALSE_NEGATIVE_RETRIES):
            time.sleep(3 + 2 * k)
            again = submit_flag(token, question_id, answer, timeout=timeout)
            if is_verdict(again):
                if is_correct(again):
                    again.setdefault("_false_negative_recovered", k + 1)
                    return again
                last = again        # 仍是 false:继续同串重试,直到用完额度
        last.setdefault("_false_negative_checked", FALSE_NEGATIVE_RETRIES)
    return last


def reset_env(token: str, question_id: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """平台没有"重置环境"接口 —— 容器由 harness 轮换(关掉再开)。

    这里返回明确提示,避免 agent 以为环境坏了却什么都做不了。
    """
    return {"code": 1, "message": "该平台不支持重置环境;容器由 harness 统一轮换(关闭后重新启动)"}
