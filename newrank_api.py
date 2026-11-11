#!/usr/bin/env python3
"""「新榜单」平台适配器(2026 全国网安比武 · 人工智能攻防赛)。

接口(见平台 API 文档,和旧 CTF 适配器同形但字段不同):
    POST /api/getquestion   {"stage": 1, "token": ...}  → {"questions":[{question_id,url,instruction}]}
    POST /api/submitflag    {"questionid": 1, "flag": ..., "token": ...}
                            → {"result": "Correct"/"Wrong", "message": ..., "new_score"/"current_score"}

与 contest_api / tsec_api / arena_api **同名同签名**,runner 与 tools/submit_flag.py 不用改:
    list_questions / submit_flag / is_correct / is_verdict / is_rate_limited / submit_with_retry

两个平台特有的坑(实测,2026-10-27):
  1. **题单里的 URL 带一次性票据 `__ctf_ticket`**:第一次 GET 返回 303 + Set-Cookie,票据当场作废;
     之后必须带 Cookie 访问干净 URL。agent 若用不带 cookie jar 的 curl 打开票据链接,会拿到 303 却丢会话,
     接下来全是 403 `runtime authentication required`。→ 本适配器在拉题时**替 agent 兑换好 Cookie**,
     并把 Netscape 格式的 cookies.txt 写进工作区(见 cookie_jar_text),提示词里只给干净 URL。
  2. **票据每次都换**:直接反复调 getquestion 会让黑板上的地址一直变。→ 题单/ Cookie 落盘缓存
     (`logs/newrank_questions.json`),进程重启也复用;`NEWRANK_REFRESH=1` 才重新拉。

启用:CONTEST_ADAPTER=newrank(见 .env);题目全部是 Web 直接入口,不需要容器生命周期接口。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://172.16.109.32:5007"
DEFAULT_STAGE = "1"

RETRIES = 3
RETRY_DELAY = 2.0
DEFAULT_TIMEOUT = 30
SUBMIT_ATTEMPTS = int(os.environ.get("SUBMIT_ATTEMPTS", "4") or 4)
SUBMIT_BASE_DELAY = float(os.environ.get("SUBMIT_BASE_DELAY", "1.0") or 1.0)
FALSE_NEGATIVE_RETRIES = int(os.environ.get("FALSE_NEGATIVE_RETRIES", "0") or 0)

# 平台与靶场都要求直连:本机代理会破坏 TLS/把内网地址打去外网(历史教训,同 contest_api)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ContestError(RuntimeError):
    """接口通了但业务失败(task_over=True 表示凭证失效/考试结束,调度该停)。"""

    def __init__(self, code, message: str, kind: str = "", http_status: int = 0):
        self.code = code
        self.message = message
        self.kind = kind
        self.http_status = http_status
        self.task_over = False
        super().__init__(f"[{kind or code}] {message}")


def _from_env_file(key: str) -> str:
    if os.environ.get(key):
        return ""
    try:
        import config as _config
        return str(_config.read_env().get(key) or "")
    except Exception:                                   # noqa: BLE001
        return ""


def base_url() -> str:
    url = (os.environ.get("NEWRANK_BASE_URL") or os.environ.get("BENCHMARK_BASE_URL")
           or _from_env_file("NEWRANK_BASE_URL") or _from_env_file("BENCHMARK_BASE_URL")
           or DEFAULT_BASE).strip().rstrip("/")
    return url


def token_of(explicit: str = "") -> str:
    tok = (explicit or os.environ.get("BENCHMARK_TOKEN") or os.environ.get("TEAM_TOKEN")
           or _from_env_file("BENCHMARK_TOKEN") or _from_env_file("TEAM_TOKEN") or "").strip()
    if not tok:
        raise ContestError("task_not_found", "未配置 BENCHMARK_TOKEN(队伍 token)", "鉴权", 404)
    return tok


def stages() -> list[int]:
    raw = (os.environ.get("NEWRANK_STAGE") or _from_env_file("NEWRANK_STAGE") or DEFAULT_STAGE).strip()
    out: list[int] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or [1]


def _run_dir() -> Path:
    return Path(os.environ.get("ROTOM_RUN_DIR") or Path(__file__).resolve().parent)


def _cache_path() -> Path:
    logdir = _run_dir() / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    return logdir / "newrank_questions.json"


# ------------------------------------------------------------------ HTTP 底层

_TRANSIENT_CODES = {"network_error", "internal_error", "busy", "rate_limited"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """票据兑换必须停在 303 上读 Set-Cookie,不能让 urllib 自动跟过去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D401
        return None


_NO_REDIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _json_call(token: str, path: str, body: dict, timeout: int = DEFAULT_TIMEOUT,
               retries: int = RETRIES) -> dict:
    url = path if path.startswith("http") else base_url() + path
    data = json.dumps(body).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "rotom-agent/1.0",
        })
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8", "ignore"))
            except Exception:                           # noqa: BLE001
                payload = {}
            if e.code == 401:
                err = ContestError("invalid_token", str(payload.get("message") or "Token 无效"),
                                   path, 401)
                err.task_over = True
                raise err
            err = ContestError(str(payload.get("result") or e.code),
                               str(payload.get("message") or f"HTTP {e.code}"), path, e.code)
            if e.code >= 500 and attempt < retries - 1:
                last = err
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            raise err
        except Exception as e:                          # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    raise ContestError("network_error", f"请求失败({retries} 次重试后): {last}", path)


# ------------------------------------------------------------------ 票据 → 会话 Cookie

def _clean_url(url: str) -> str:
    """去掉一次性票据,留稳定入口。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
          if k != "__ctf_ticket"]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(qs), parts.fragment))


def _cookie_header_from_response(headers) -> list[tuple[str, str]]:
    """把一条或多条 Set-Cookie 拆成 (name, value)。"""
    out: list[tuple[str, str]] = []
    for raw in headers.get_all("Set-Cookie") or []:
        first = raw.split(";", 1)[0]
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            out.append((name, value))
    return out


def _exchange_ticket(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[str, list[tuple[str, str]]]:
    """用一次性票据换会话 Cookie。

    返回 (干净 URL, [(cookie 名, 值)])。403/网络失败抛 ContestError,由上层重试整份题单。
    """
    clean = _clean_url(url)
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "rotom-agent/1.0", "Accept": "*/*"})
    try:
        with _NO_REDIRECT.open(req, timeout=timeout) as resp:
            cookies = _cookie_header_from_response(resp.headers)
            status = resp.status
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            cookies = _cookie_header_from_response(e.headers)
            status = e.code
        else:
            raise ContestError("ticket_exchange", f"票据兑换失败 HTTP {e.code}", "开题", e.code)
    except Exception as e:                              # noqa: BLE001
        raise ContestError("network_error", f"票据兑换失败: {e}", "开题")

    if not cookies:
        raise ContestError("ticket_exchange", f"票据兑换没有拿到 Set-Cookie(HTTP {status})", "开题")

    # 再访问一次干净 URL,确认 Cookie 真的能用(顺便收下应用自己下发的会话 Cookie)
    header = "; ".join(f"{k}={v}" for k, v in cookies)
    req2 = urllib.request.Request(clean, method="GET",
                                  headers={"User-Agent": "rotom-agent/1.0",
                                           "Cookie": header, "Accept": "*/*"})
    extra: list[tuple[str, str]] = []
    status2 = 0
    try:
        with _OPENER.open(req2, timeout=timeout) as resp:
            body = resp.read(64)
            extra = _cookie_header_from_response(resp.headers)
            status2 = resp.status
        # 只有 401/403 才是"会话不被接受";404/500 等说明 runtime 已放行(页面本身不存在/报错)
        if status2 in (401, 403):
            raise ContestError("ticket_exchange",
                               f"Cookie 校验失败(HTTP {status2})", "开题")
    except ContestError:
        raise
    except urllib.error.HTTPError as e:
        # 401/403 才是会话不被接受;404/500 说明 runtime 已放行,页面本身不存在/报错
        if e.code in (401, 403):
            raise ContestError("ticket_exchange", f"Cookie 校验失败(HTTP {e.code})", "开题", e.code)
        extra = _cookie_header_from_response(e.headers)
        status2 = e.code
    except Exception as e:                              # noqa: BLE001
        raise ContestError("network_error", f"Cookie 校验失败: {e}", "开题")

    for name, value in extra:
        if all(name != n for n, _ in cookies):
            cookies.append((name, value))
    return clean, cookies


def cookie_jar_text(cookie_header: str) -> str:
    """把 `a=b; c=d` 或多条 Set-Cookie 转成 curl 能直接用的 Netscape cookies.txt。

    一个题目一个文件(写在 worker 工作目录),避免不同题目的应用会话(常见同名 PHPSESSID)串味。
    """
    cookies = parse_cookie_header(cookie_header)
    host = urllib.parse.urlsplit(base_url()).hostname or "127.0.0.1"
    lines = ["# Netscape HTTP Cookie File", "# 由 Rotom 在开题时生成:入口票据已兑换成会话 Cookie"]
    for name, value in cookies:
        lines.append("\t".join([host, "FALSE", "/", "FALSE", "0", name, value]))
    return "\n".join(lines) + "\n"


def parse_cookie_header(header: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for part in str(header or "").split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            out.append((k, v))
    return out


# ------------------------------------------------------------------ 题目列表

_CACHE: dict = {"at": 0.0, "stage": "", "questions": []}


def _slug(src: str) -> str:
    """从入口 URL 或附件文件名里取一个短标识(题目目录名/看板标题用)。"""
    try:
        parts = urllib.parse.urlsplit(src)
    except ValueError:
        return "q"
    seg = [s for s in parts.path.split("/") if s]
    if seg:
        name = seg[-1].replace(".php", "").replace(".html", "")
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:32] or "q"
    return f"port{parts.port or 80}" if parts.port else "q"


_ATTACH_RE = re.compile(r"附件下载\s*\d*\s*[:：]\s*(https?://\S+)")
_TRAIL = ").,;，。；）]】>"

# 方向关键词:决定注入哪份 playbook(stage 2 基本是逆向,stage 3 是密码/取证/Web 源码审计)。
_WEB_WORDS = ("站点", "页面", "网页", "表单", "博客", "xml", "api key", "会话", "换汇", "反馈", "后台", "登录")
_CRYPTO_WORDS = ("密码", "加密", "rsa", "替换", "base64", "证书", "素数", "矩阵", "置换", "sage", "密钥", "密码学")
_FORENSICS_WORDS = ("取证", "流量", "pcap", "内存", "隐写", "音频", "图片", "png", "邮件", "组策略",
                    "dtmf", "icmp", "wav", "磁盘", "镜像")
_PWN_WORDS = ("服务题", "远程服务", "溢出", "栈", "堆", "rop", "nc ")


def _attachments(text: str) -> list[str]:
    out: list[str] = []
    for u in _ATTACH_RE.findall(text or ""):
        u = u.rstrip(_TRAIL)
        if u and u not in out:
            out.append(u)
    return out


def _file_name(url: str) -> str:
    try:
        name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        name = urllib.parse.unquote(name)
        return name or "attachment.bin"
    except Exception:                                   # noqa: BLE001
        return "attachment.bin"


def _infer_category(text: str, stage: int, has_url: bool = False) -> str:
    low = (text or "").lower()
    if stage == 1:
        return "web"
    # 有存活实例的题优先按 Web/服务题处理(阶段 3 的 75-80 是"源码 + 在线站点";阶段 2 只有 51 是服务题)
    if has_url and stage == 3:
        return "web"
    if has_url and stage == 2:
        return "pwn" if any(w in low for w in _PWN_WORDS) else "web"
    if any(w in low for w in _WEB_WORDS):
        return "web"
    if stage == 2:
        if any(w in low for w in _PWN_WORDS):
            return "pwn"
        return "reverse"
    if any(w in low for w in _CRYPTO_WORDS):
        return "crypto"
    if any(w in low for w in _FORENSICS_WORDS):
        return "forensics"
    if any(w in low for w in _PWN_WORDS):
        return "pwn"
    return "web" if stage == 1 else "misc"


def _to_question(row: dict) -> dict:
    qid = str(row.get("question_id") or "").strip()
    stage = int(row.get("stage") or 1)
    instruction = str(row.get("instruction") or "").strip()
    ticket_url = str(row.get("url") or "").strip()
    clean = str(row.get("clean_url") or _clean_url(ticket_url))
    cookie = str(row.get("cookie") or "").strip()
    attachments = _attachments(instruction)
    slug_src = clean or (attachments[0] if attachments else "")
    name = f"q{int(qid):02d}-{_slug(slug_src)}" if (qid.isdigit() and slug_src) else (
        f"q{int(qid):02d}" if qid.isdigit() else qid)
    category = _infer_category(instruction, stage, bool(clean))

    if not clean:
        # 静态附件题(阶段 2/3 的逆向/密码/取证):没有远程入口,文件由 solver 下到 files/
        desc = (
            f"{instruction}\n\n"
            f"本题是**静态附件题**(没有远程入口)。附件已由 harness 下载到本题的 files/ 目录:\n"
            + "\n".join(f"  {i}. {u}" for i, u in enumerate(attachments, 1))
            + "\n\n如果 files/ 里缺文件,可以自己重新下载(无需 Cookie):\n"
            + "\n".join(f"  curl -sS -o files/{_file_name(u)} \"{u}\"" for u in attachments)
            + "\n\n不要去找 cookies.txt / 远程靶机;直接在 files/ 里分析。\n"
        )
        conn: dict = {}
        file_url = attachments[0] if attachments else ""
        interactive = "false"
    elif cookie:
        desc = (
            f"{instruction}\n\n"
            f"入口(直接访问,**不要**再去点带 __ctf_ticket 的链接):\n  {clean}\n\n"
            f"本题会话 Cookie 已由 harness 兑换好,并写进当前工作目录的 cookies.txt。\n"
            f"所有请求都要带上它(curl 用 -b/-c 自动读写同一个 jar):\n"
            f"  curl -sS -b cookies.txt -c cookies.txt -i \"{clean}\"\n"
            f"Cookie 头(手工发请求时用):\n  {cookie}\n"
        )
        conn = {"target1": clean, "url1": clean}
        file_url = attachments[0] if attachments else ""
        interactive = "true"
    else:
        desc = (
            f"{instruction}\n\n"
            f"入口(一次性票据,首次访问必须自己留存 Cookie):\n  {ticket_url}\n\n"
            f"起手先兑换票据,之后只用干净地址:\n"
            f"  curl -sS -L -c cookies.txt -b cookies.txt -o index.html \"{ticket_url}\"\n"
            f"  curl -sS -b cookies.txt -c cookies.txt -i \"{clean}\"\n"
            f"⚠ 票据只能用一次;丢掉 Cookie 就会 403 runtime authentication required。\n"
        )
        conn = {"target1": clean, "url1": clean}
        file_url = attachments[0] if attachments else ""
        interactive = "true"

    return {
        "question_id": qid,
        "title": name,
        "category": category,
        "score": int(row.get("score") or 100),
        "description": desc,
        "difficulty": row.get("difficulty") or "",
        "level": int(row.get("level") or 0),
        "flag_count": int(row.get("flag_count") or 1),
        "correct_flag_count": 0,
        "is_solved": False,
        "file_url": file_url,
        "file_name": _file_name(file_url) if file_url else "",
        "attachments": attachments,
        "interactive": interactive,
        "connection": conn,
        "container_status": "ready",
        "container_addr": [],
        "container_ready": True,
        "attributes": [], "capabilities": [],
        "extensions": {"instruction": instruction, "stage": stage, "attachments": attachments},
        # 适配器内部字段:不参与黑板合并的是 http_cookie / ticket_url
        "http_cookie": cookie,
        "ticket_url": ticket_url,
        "stage": stage,
    }


def _fetch_all(token: str, stage_list: list[int]) -> list[dict]:
    rows: list[dict] = []
    for stage in stage_list:
        data = _json_call(token, "/api/getquestion", {"stage": stage, "token": token})
        qs = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(qs, list):
            raise ContestError("bad_response", f"题单格式异常: {str(data)[:160]}", "查题")
        for r in qs:
            if not isinstance(r, dict) or r.get("question_id") in (None, ""):
                continue
            r = dict(r)
            r["stage"] = stage
            rows.append(r)

    out: list[dict] = []
    for r in rows:
        url = str(r.get("url") or "").strip()
        if not url:
            # 阶段 2/3 的静态附件题:url 为空,只有 instruction 里的附件地址
            r["clean_url"], r["cookie"] = "", ""
            out.append(_to_question(r))
            continue
        try:
            clean, cookies = _exchange_ticket(url)
            cookie = "; ".join(f"{k}={v}" for k, v in cookies)
        except ContestError as e:
            # 单题兑换失败不拖垮整份题单:退回"票据直给"模式,提示词里教 agent 自己换。
            clean, cookie = _clean_url(url), ""
            print(f"[newrank] 题目 {r.get('question_id')} 票据兑换失败:{e};退回票据直给", flush=True)
        r["clean_url"] = clean
        r["cookie"] = cookie
        out.append(_to_question(r))
    return out


def _load_cache() -> list[dict] | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        return None
    if not data.get("questions"):
        return None
    return [dict(q) for q in data["questions"]]


def _save_cache(questions: list[dict], stage_list: list[int]) -> None:
    p = _cache_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": time.time(), "stage": ",".join(map(str, stage_list)),
                               "questions": questions}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)


def _cached_stages(questions: list[dict]) -> set[int]:
    out: set[int] = set()
    for q in questions:
        try:
            out.add(int(q.get("stage") or 1))
        except (TypeError, ValueError):
            out.add(1)
    return out


def _merge_fetch(tok: str, cached: list[dict], want: set[int]) -> tuple[list[dict], list[int]]:
    """按阶段增量拉题:缓存里已有的阶段不重拉(避免把在跑题目的票据/Cookie 翻新)。"""
    have = _cached_stages(cached)
    missing = sorted(want - have)
    if not missing:
        return cached, sorted(have)
    extra = _fetch_all(tok, missing)
    by = {str(q.get("question_id")): q for q in cached}
    for q in extra:
        by[str(q.get("question_id"))] = q
    merged = sorted(by.values(),
                    key=lambda q: int(q["question_id"]) if str(q.get("question_id", "")).isdigit() else 0)
    st = sorted(have | set(missing))
    _CACHE.update({"at": time.time(), "stage": ",".join(map(str, st)), "questions": merged})
    _save_cache(merged, st)
    return merged, st


def refresh_questions(token: str = "") -> list[dict]:
    """强制重新拉全部目标阶段并兑换新一轮 Cookie(控制台/运维用)。"""
    tok = token_of(token)
    st = stages()
    qs = _fetch_all(tok, st)
    _CACHE.update({"at": time.time(), "stage": ",".join(map(str, st)), "questions": qs})
    _save_cache(qs, st)
    return list(qs)


def list_questions(token: str = "", timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """runner 用的题目列表。

    默认复用落盘缓存;请求的阶段比缓存多时(例如先跑 stage 1、再补 2/3),
    只拉缺的阶段并合并 —— 已有阶段的票据/Cookie 不会被翻新。
    """
    want = set(stages())
    if str(os.environ.get("NEWRANK_REFRESH") or "").strip() in ("1", "true", "yes"):
        return [dict(q) for q in refresh_questions(token)]
    tok = token_of(token)
    if _CACHE["questions"]:
        qs, _ = _merge_fetch(tok, _CACHE["questions"], want)
        return [dict(q) for q in qs]
    cached = _load_cache()
    if cached:
        qs, _ = _merge_fetch(tok, cached, want)
        return [dict(q) for q in qs]
    return [dict(q) for q in refresh_questions(token)]


def cached_status() -> dict[str, dict]:
    return {str(q.get("question_id")): q for q in _CACHE.get("questions") or []}


# ------------------------------------------------------------------ 提交 flag

def _err(code, message: str, **extra) -> dict:
    d = {"code": code, "message": message}
    d.update(extra)
    return d


def submit_flag(token: str, question_id: str, answer: str,
                timeout: int = DEFAULT_TIMEOUT) -> dict:
    """提交一个 flag。判错也返回 code=0(平台受理了),只有没受理才是非 0。"""
    try:
        qid = int(question_id) if str(question_id).isdigit() else question_id
        data = _json_call(token, "/api/submitflag",
                          {"questionid": qid, "flag": answer, "token": token_of(token)},
                          timeout=timeout)
    except ContestError as e:
        return _err(e.code, e.message, http_status=e.http_status, task_over=e.task_over)
    except Exception as e:                              # noqa: BLE001
        return _err("network_error", f"网络错误: {e}")

    data = data or {}
    result = str(data.get("result") or "").strip()
    msg = str(data.get("message") or "").strip()
    correct = result.lower() == "correct" or "正确" in msg
    duplicate = correct and any(k in msg for k in ("已经提交过", "已提交过", "提交过"))
    if result.lower() == "error":
        return _err(data.get("code") or "submit_error", msg or "平台拒绝受理")
    return {
        "code": 0,
        "correct": correct,
        "status": 1 if correct else 0,
        "message": msg or ("回答正确" if correct else "答案错误"),
        "awarded": 0,
        "cumulative_score": data.get("new_score") or data.get("current_score"),
        "duplicate": duplicate,
        # 平台不给多 flag 计数;本题库每题 1 个 flag,补上计数让提交工具把黑板标成 solved
        "correct_flag_count": 1 if correct else 0,
        "total_flag_count": 1,
        "matched_flag_index": 0 if correct else None,
        "raw": data,
    }


def is_verdict(resp: dict) -> bool:
    return resp.get("code") == 0


def is_correct(resp: dict) -> bool:
    if resp.get("code") != 0:
        return False
    return bool(resp.get("correct")) or resp.get("status") == 1 or "正确" in str(resp.get("message", ""))


def is_rate_limited(resp: dict) -> bool:
    if resp.get("code") == 0:
        return False
    code = resp.get("code")
    msg = str(resp.get("message", "")).lower()
    return code in _TRANSIENT_CODES or code in (429, "429") or any(
        k in msg for k in ("频繁", "too many", "rate limit"))


def submit_with_retry(token: str, question_id: str, answer: str, timeout: int = DEFAULT_TIMEOUT,
                      attempts: int | None = None, base_delay: float | None = None) -> dict:
    """暂时性错误退避重试;**不**对"判定错误"重试(本题库没有假阴性证据,别浪费提交次数)。"""
    last: dict = {}
    attempts = SUBMIT_ATTEMPTS if attempts is None else attempts
    base_delay = SUBMIT_BASE_DELAY if base_delay is None else base_delay
    for i in range(attempts):
        resp = submit_flag(token, question_id, answer, timeout=timeout)
        last = resp
        if is_verdict(resp) or not is_rate_limited(resp):
            break
        if i < attempts - 1:
            time.sleep(base_delay * (i + 1) + random.uniform(0, 0.5))
    return last


def reset_env(token: str, question_id: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    return {"code": 1, "message": "该平台不支持重置环境;每个题目的入口是独立的,直接重试即可"}