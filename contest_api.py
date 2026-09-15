"""iChunQiu AI 智能体解题赛 API 封装(纯标准库)。

接口文档见 ../AI智能体解题赛接口文档.pdf:
  - 查询题目: GET /04cb510e425bd8f64fa97ba66f3935e1?token=...
  - 重置环境: GET /deed3dba39e57b7cf95ea63ddd84e0c8?token=...&question_id=...
  - 提交答案: GET /ff874ef3172cbf4fd6ec2c5653a568e2?token=...&question_id=...&answer=...
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.parse
import urllib.request

# ---- 接口地址(全部可通过环境变量覆盖,比赛现场若改域名/路径只需改 .env 或在控制台里改) ----
DEFAULT_BASE = "https://apiterminator.ichunqiu.com"
DEFAULT_PATHS = {
    "QUERY": "/04cb510e425bd8f64fa97ba66f3935e1",
    "RESET": "/deed3dba39e57b7cf95ea63ddd84e0c8",
    "SUBMIT": "/ff874ef3172cbf4fd6ec2c5653a568e2",
}

# 比赛接口直连,不走本机代理:实测经由本地代理会出现 SSL_ERROR_SYSCALL / 连接被重置
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RETRIES = 4
RETRY_DELAY = 2.0


def endpoint(kind: str) -> str:
    """拼出接口地址。CONTEST_<KIND>_PATH 可以填路径(拼在 CONTEST_BASE 后面),
    也可以直接填一个完整 URL(以 http 开头时原样使用)。"""
    kind = kind.upper()
    path = (os.environ.get(f"CONTEST_{kind}_PATH") or DEFAULT_PATHS[kind]).strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = (os.environ.get("CONTEST_BASE") or DEFAULT_BASE).strip().rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


DEFAULT_TIMEOUT = 30


class ContestError(RuntimeError):
    """接口正常返回但业务上失败(如 token 无效、比赛已结束、题目不存在)。"""

    def __init__(self, code, message: str, kind: str = ""):
        self.code = code
        self.message = message
        self.kind = kind
        super().__init__(f"[{kind or code}] {message}")


def _get(url: str, timeout: int = DEFAULT_TIMEOUT, **params) -> dict:
    url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "wqh-agent/1.0"})
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # 网络抖动重试;接口返回的业务错误不会走到这里
            last = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"请求失败({RETRIES} 次重试后): {url.split('?')[0]} -> {last}")


def list_questions(token: str, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """返回题目列表;接口异常时抛出。"""
    data = _get(endpoint("QUERY"), timeout=timeout, token=token)
    if data.get("code") != 0:
        raise ContestError(data.get("code"), str(data.get("message", "")), "查询题目")
    return data.get("data") or []


def reset_env(token: str, question_id: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """重置容器题环境(仅 interactive=true 的题目支持)。"""
    return _get(endpoint("RESET"), timeout=timeout, token=token, question_id=question_id)


def submit_flag(token: str, question_id: str, answer: str,
                timeout: int = DEFAULT_TIMEOUT) -> dict:
    """提交 flag。正确答案返回 {"code": 0, "message": "答案正确", "status": 1}。"""
    return _get(endpoint("SUBMIT"), timeout=timeout,
                token=token, question_id=question_id, answer=answer)


def is_correct(resp: dict) -> bool:
    # 首次答对返回 {"code":0,"message":"恭喜您，回答正确"}(无 status);
    # 重复提交已攻克题返回 {"code":0,"status":1,"message":"答案正确，该题目已被攻克，不计分"}
    if resp.get("code") != 0:
        return False
    return resp.get("status") == 1 or "正确" in str(resp.get("message", ""))


def is_verdict(resp: dict) -> bool:
    """这份响应是不是"平台对答案的判定"(code 0 = 受理了)。

    code != 0 是**接口/业务层没受理**(限流 101「操作太过频繁」、token 失效、比赛已结束…),
    不能当成"flag 错了"——第二轮实测就把限流当成错答记账了,agent 会因此去重推 flag、白烧错误提交次数。
    """
    return resp.get("code") == 0


RATE_LIMIT_HINTS = ("太过频繁", "操作频繁", "too many", "rate limit")


def is_rate_limited(resp: dict) -> bool:
    """平台限流:属于暂时性错误,退避后重试即可。"""
    if resp.get("code") == 0:
        return False
    return resp.get("code") in (101, 429) and any(
        h in str(resp.get("message", "")).lower() for h in RATE_LIMIT_HINTS)


def submit_with_retry(token: str, question_id: str, answer: str, timeout: int = DEFAULT_TIMEOUT,
                      attempts: int = 5, base_delay: float = 1.5) -> dict:
    """提交 flag,并对**限流/网络抖动**自动退避重试(每次加一点随机抖动,避免多 worker 同时撞限流)。

    注意:只重试"没被受理"的情况——平台明确判了答案错误(code 0 但不是正确)时立即返回,不浪费提交次数。
    """
    last: dict = {}
    for i in range(attempts):
        delay = base_delay * (i + 1) + random.uniform(0, 0.7)
        try:
            resp = submit_flag(token, question_id, answer, timeout=timeout)
        except Exception as e:
            last = {"code": -1, "message": f"网络错误: {e}"}
        else:
            last = resp
            if is_verdict(resp) or not is_rate_limited(resp):
                return resp
        if i < attempts - 1:
            time.sleep(delay)
    return last
