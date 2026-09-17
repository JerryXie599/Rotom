"""NSSCTF Agent Arena 适配器 —— 对外暴露与 contest_api 完全相同的接口,便于 runner 直接复用。

靶场接口(官方文档):
  GET  /api/skill/agent/arena/current/                当前进行中的题
  POST /api/skill/agent/arena/next/                   随机领题(Body 无;已有进行中的题则复用)
  GET  /api/skill/agent/arena/attempt/{id}/           题目详情
  POST /api/skill/agent/arena/attempt/{id}/submit/    提交 flag
  POST /api/skill/agent/arena/attempt/{id}/abandon/   放弃
全部需要 `Authorization: Bearer nss_agent_xxx`。

注意:靶场**不返回题目 ID / 名称 / 标签**,所以这里合成:
  question_id = "arena-<attempt_id>",title = "<类型>-<attempt_id>"。
启用方式:CONTEST_ADAPTER=arena(见 tests/arena/run_arena.sh)。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = os.environ.get("NSSCTF_API", "https://www.nssctf.cn/api").rstrip("/")
TOKEN_FILE = ROOT / ".nssctf_token"

RETRIES = 3
RETRY_DELAY = 2.0
DEFAULT_TIMEOUT = 40

# 靶场接口必须直连(本机代理会破坏 TLS);模型/附件另说
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ContestError(RuntimeError):
    """接口通了但业务失败(如 token 无效、无题可领)。与 contest_api.ContestError 同名同义。"""

    def __init__(self, code, message: str, kind: str = ""):
        self.code = code
        self.message = message
        self.kind = kind
        super().__init__(f"[{kind or code}] {message}")


def agent_token(explicit: str = "") -> str:
    tok = explicit or os.environ.get("NSSCTF_AGENT_TOKEN", "")
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not tok:
        raise ContestError(401, "未配置 NSSCTF Agent Token(环境变量 NSSCTF_AGENT_TOKEN 或 .nssctf_token)", "鉴权")
    return tok


def _req(method: str, path: str, body: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    url = API + path
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {agent_token()}",
        "Content-Type": "application/json",
        "User-Agent": "rotom-agent/1.0",
    })
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("code") not in (200, 0):
                raise ContestError(payload.get("code"), str(payload.get("data", {}).get("message") or payload.get("message")), path)
            return payload.get("data") or {}
        except ContestError:
            raise
        except Exception as e:  # 网络抖动重试
            last = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"请求失败({RETRIES} 次重试后): {url} -> {last}")


# ---------------------------------------------------------------- 靶场原生接口

def current() -> dict:
    return _req("GET", "/skill/agent/arena/current/")


def claim() -> dict:
    return _req("POST", "/skill/agent/arena/next/")


def attempt_detail(attempt_id) -> dict:
    return _req("GET", f"/skill/agent/arena/attempt/{attempt_id}/")


def abandon(attempt_id) -> dict:
    return _req("POST", f"/skill/agent/arena/attempt/{attempt_id}/abandon/")


# ---------------------------------------------------------------- 与 contest_api 同签名

CATEGORY_MAP = {
    "web": "web", "pwn": "pwn", "reverse": "reverse", "re": "reverse", "crypto": "crypto",
    "misc": "misc", "forensics": "forensics", "blockchain": "misc", "iot": "misc",
    "ai": "misc", "stego": "forensics", "osint": "misc",
}


def _to_question(att: dict) -> dict:
    """把靶场 attempt 转成 harness 通用的题目结构。"""
    prob = att.get("problem") or {}
    aid = att.get("id")
    label = (prob.get("type_label") or "Misc").strip()
    cat = CATEGORY_MAP.get(label.lower(), "misc")
    annex = prob.get("annex") or {}
    cont = prob.get("container") or {}
    urls = cont.get("url") or []
    conn = {}
    if urls:
        u = urls[0]
        if u.startswith("http"):
            conn = {"docker_url": u}
        else:  # host:port 形式
            host, _, port = u.rpartition(":")
            conn = {"docker_url": f"nc {host} {port}", "docker_ip": host, "docker_port": port}
    desc = prob.get("content") or ""
    if prob.get("hint"):
        desc += f"\n\n[hint] {prob['hint']}"
    if cont.get("remaining_seconds"):
        desc += f"\n\n(靶场容器剩余 {cont.get('remaining_seconds')} 秒)"
    return {
        "question_id": f"arena-{aid}",
        "title": f"{label}-{aid}",
        "score": prob.get("rating", 0),
        "real_score": prob.get("rating", 0),
        "file_url": annex.get("url", "") or "",
        "file_name": annex.get("name", "") or "",
        "is_solved": False,
        "solved_number": 0,
        "category": cat,
        "attributes": [], "capabilities": [],
        "description": desc.strip(),
        "interactive": "true" if (cont or conn) else "false",
        "connection": conn,
        "extensions": {"arena_attempt_id": aid, "type_label": label,
                       "remaining_seconds": att.get("remaining_seconds"),
                       "wrong_count": att.get("wrong_count")},
    }


def list_questions(token: str = "", timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """当前有题就返回当前题;没有就领一道新题。"""
    data = current()
    att = data.get("attempt")
    if not att:
        data = claim()
        att = data.get("attempt")
    if not att:
        return []
    return [_to_question(att)]


def submit_flag(token: str, question_id: str, answer: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    aid = str(question_id).replace("arena-", "")
    data = _req("POST", f"/skill/agent/arena/attempt/{aid}/submit/", {"flag": answer.strip()}, timeout)
    # 靶场返回里带正确与否的信息,统一映射成 contest_api 的形状
    ok = bool(data.get("correct") or data.get("solved") or data.get("state") == 2
              or str(data.get("state_label", "")).lower() in ("solved", "correct"))
    msg = data.get("message") or ("回答正确" if ok else "答案错误")
    return {"code": 0, "message": msg, "status": 1 if ok else 0, "raw": data}


def reset_env(token: str, question_id: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """靶场不支持重置环境;当前题坏了就放弃(下次轮询会自动领新题)。"""
    aid = str(question_id).replace("arena-", "")
    try:
        abandon(aid)
        return {"code": 0, "message": "已放弃该题(靶场不支持重置,下轮自动领新题)"}
    except Exception as e:
        return {"code": 1, "message": f"放弃失败: {e}"}


def is_correct(resp: dict) -> bool:
    if resp.get("code") != 0:
        return False
    return resp.get("status") == 1 or "正确" in str(resp.get("message", ""))
