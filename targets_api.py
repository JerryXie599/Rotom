"""任务源:本地目标清单(正式渗透 profile 用,替代平台适配器)。

和 contest_api / tsec_api / arena_api 对外**同名同签名**,runner 不用改就能切换:
    list_questions(token) -> [question, ...]     # 从 targets.json 读目标
    ContestError                                 # 业务错误(清单缺失/格式错)

目标清单(targets.json,默认放在数据目录下;环境变量 TARGETS_FILE 可覆盖路径):
    {
      "engagement": "客户A-外网渗透",
      "targets": [
        {"id": "web-01", "name": "官网", "address": "https://www.example.com",
         "notes": "登录口与 API 均可测"},
        {"id": "net-02", "name": "办公网段", "address": "10.0.0.0/24",
         "notes": "仅限 10.0.0.0/24"}
      ]
    }

每个目标会被转成 runner 认识的一道"题":question_id=id、title=name、category=pentest、
description=notes、connection={target: address};没有平台、没有 flag、没有容器名额。
"完成"由 agent 调 tools/report_finding.py --done 标记(见该脚本)。
"""

from __future__ import annotations

import json
import os

import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)

_DEFAULT_FILE = "targets.json"


class ContestError(RuntimeError):
    """业务错误(清单缺失/格式错),runner 会保持轮询而不是崩。"""

    def __init__(self, message: str, code: str = "targets_error"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.task_over = False


def targets_path() -> Path:
    """目标清单路径:TARGETS_FILE 优先,否则数据目录下的 targets.json。"""
    p = (os.environ.get("TARGETS_FILE") or "").strip()
    if p:
        return Path(p).expanduser()
    return RUN_DIR / _DEFAULT_FILE


def _slug(s: str, n: int = 40) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(s or "")).strip("-")[:n] or "target"


def _read_targets() -> list[dict]:
    p = targets_path()
    if not p.exists():
        raise ContestError(f"目标清单不存在:{p}(可复制 targets.example.json 改成 {_DEFAULT_FILE})")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ContestError(f"目标清单不是合法 JSON({p}): {e}")
    items = data.get("targets") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ContestError(f"目标清单里没有 targets 列表({p})")
    out: list[dict] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        addr = str(it.get("address") or it.get("url") or "").strip()
        if not addr:
            continue
        tid = str(it.get("id") or it.get("name") or f"t{i + 1}")
        out.append({
            "id": tid,
            "name": str(it.get("name") or tid),
            "address": addr,
            "notes": str(it.get("notes") or it.get("description") or ""),
            # workdir:绝对路径 = worker 直接落在该目录(涉诈场景:站点档案/工作区);
            # category:走策略包 playbooks 映射选 playbook(缺省 pentest 兼容老清单)
            "workdir": str(it.get("workdir") or "").strip(),
            "category": str(it.get("category") or "pentest").strip(),
        })
    if not out:
        raise ContestError(f"目标清单里没有带 address 的目标({p})")
    return out


# ---------------------------------------------------------------- 与 contest_api 同名的接口

def list_questions(token: str = "", timeout: int = 15) -> list[dict]:
    """把目标清单转成 runner 的题目结构。每次调用都重读文件(新增目标即时生效)。"""
    questions = []
    for t in _read_targets():
        questions.append({
            "question_id": t["id"],
            "title": t["name"],
            "category": t["category"],
            "score": 0,
            "description": t["notes"],
            "file_url": "",
            "interactive": "false",
            "connection": {"target": t["address"]},
            "workdir": t["workdir"],     # 空 = 老行为(solver 按 大类-题名 建目录)
            "is_solved": False,          # 是否交付以黑板为准(board 里 is_solved 是粘性的)
            "flag_count": 0,
            "difficulty": "",
            "level": 0,
            "container_ready": True,     # 没有容器名额概念:永远可派发
            "container_status": "",
        })
    return questions


def check_vpn(timeout: int = 8) -> dict:
    """正式渗透的 VPN 预检(让测试流量不可追溯到本机)。

    配了 VPN_CHECK_URL 就要求它可达(返回 200 即算连通);没配则视为"无需预检"直接放行 ——
    这样默认不挡流程,现场想强制走 VPN 就填一个只有连上 VPN 才通的地址。
    """
    url = (os.environ.get("VPN_CHECK_URL") or "").strip()
    if not url:
        return {"ok": True, "note": "未配置 VPN_CHECK_URL,跳过预检"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rotom-agent/1.0"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连,不走本机代理
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(400).decode("utf-8", "ignore")
        ok = 200 <= resp.status < 400
        return {"ok": ok, "status": resp.status, "body": body[:120]} if ok \
            else {"ok": False, "error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def submit_flag(*_args, **_kwargs):        # 正式渗透没有 flag:被误调时给出清晰错误
    raise ContestError("正式渗透模式没有 flag,请用 tools/report_finding.py 记录 finding")


def reset_env(*_args, **_kwargs):
    raise ContestError("正式渗透模式没有「重置环境」:目标现场由委托方管理")


def get_hint(*_args, **_kwargs) -> str | None:
    return None