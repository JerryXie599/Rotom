#!/usr/bin/env python3
"""离线 mock:TSec Benchmark 平台 + 靶场 VPN 预检 + 每道题的"靶场目标"。

用来在没有真实平台/VPN 的情况下把整条链路跑通(容器自开自关、一题多 flag、
重复提交、活跃容器上限 3、轮换、提交工具记账)。

启动:
    python3 tests/mock_tsec_server.py --platform-port 18800 --vpn-port 18801 --targets 6

它会打印一份可以直接用的 .env 片段(写到 tests/mock_env.sh)。
配合 tests/run_mock_e2e.sh 使用。

目标(靶场)故意做得很"众测":一个首页 + 一个需要两步才能拿到的接口:
    GET /                 首页,列出入口
    GET /api/flag?id=N    需要 ?debug=1 才返回 flag(模拟"越权/参数绕过")
    GET /admin/flag       User-Agent 带 "admin" 才返回 flag(模拟"认证绕过")
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "mock-benchmark-token"
LOCK = threading.Lock()

# 题目定义:unique_code -> {desc, flags, difficulty, level, score}
CHALLENGES: dict[str, dict] = {}
STATE: dict[str, dict] = {}          # unique_code -> {status, port, found:set, submitted:set}
TARGET_BASE_PORT = 18900
# 测试开关:这些 flag 值的提交一律答"资源紧张"(平台没受理)—— 用来测 harness 侧的补交
BUSY_FLAGS: set[str] = set()


def _init_challenges(n: int) -> None:
    kinds = [
        ("web_sql_injection_01", "通过 SQL 注入获取管理员凭证并读取 flag", ["flag{sql_1}", "flag{sql_2}"], "easy", 1, 100),
        ("web_idor_02", "接口越权:换个 id 就能读到别人的 flag", ["flag{idor_1}"], "easy", 1, 100),
        ("web_auth_bypass_03", "管理接口只校验 User-Agent", ["flag{authz_1}", "flag{authz_2}"], "medium", 2, 200),
        ("web_upload_04", "任意文件上传导致可读取 flag", ["flag{upload_1}"], "medium", 2, 200),
        ("api_ssrf_05", "SSRF 打内网元数据接口拿 flag", ["flag{ssrf_1}", "flag{ssrf_2}"], "hard", 3, 300),
        ("crypto_weak_token_06", "弱随机数生成的管理员 token", ["flag{crypto_1}"], "hard", 3, 300),
    ]
    for i in range(min(n, len(kinds))):
        code, desc, flags, diff, level, score = kinds[i]
        CHALLENGES[code] = {"description": desc, "flags": flags, "difficulty": diff,
                            "level": level, "total_score": score}
        STATE[code] = {"status": "stopped", "port": TARGET_BASE_PORT + i,
                       "found": set(), "wrong": 0, "hint_used": False}


class PlatformHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # 静音,免得刷屏
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, http: int, code: str, message: str) -> None:
        self._json(http, {"code": code, "message": message, "detail": {}})

    def _auth_ok(self) -> bool:
        return self.headers.get("BENCHMARK_TOKEN", "") == TOKEN

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if not self._auth_ok():
            return self._err(404, "task_not_found", "token 无效或缺失")
        if u.path == "/openapi/v1/challenges":
            out = []
            with LOCK:
                for code, meta in CHALLENGES.items():
                    st = STATE[code]
                    avail = st["status"] == "available"
                    out.append({
                        "unique_code": code,
                        "description": meta["description"],
                        "difficulty": meta["difficulty"],
                        "level": meta["level"],
                        "total_score": meta["total_score"],
                        "flag_count": len(meta["flags"]),
                        "correct_flag_count": len(st["found"]),
                        "is_completed": len(st["found"]) == len(meta["flags"]),
                        "container_status": st["status"],
                        "container_addr": [f"127.0.0.1:{st['port']}"] if avail else [],
                    })
            return self._json(200, out)
        if u.path == "/openapi/v1/challenges/hint":
            q = urllib.parse.parse_qs(u.query).get("unique_code", [""])[0]
            if q not in CHALLENGES:
                return self._err(404, "challenge_not_found", q)
            with LOCK:
                if len(STATE[q]["found"]) == len(CHALLENGES[q]["flags"]):
                    return self._err(409, "invalid_state", "已通关,不能再看提示")
                STATE[q]["hint_used"] = True
            return self._json(200, {"unique_code": q, "hint": f"看看 {q} 的 /api 与 /admin 路径"})
        return self._err(404, "not_found", self.path)

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path == "/__mock__/busy":            # 测试控制面(不走平台鉴权)
            body = self._body()
            BUSY_FLAGS.clear()
            BUSY_FLAGS.update(str(f) for f in (body.get("flags") or []))
            return self._json(200, {"busy_flags": sorted(BUSY_FLAGS)})
        if not self._auth_ok():
            return self._err(404, "task_not_found", "token 无效或缺失")
        q = urllib.parse.parse_qs(u.query).get("unique_code", [""])[0]
        if u.path == "/openapi/v1/challenges/start":
            if q not in CHALLENGES:
                return self._err(404, "challenge_not_found", q)
            with LOCK:
                active = [c for c, s in STATE.items() if s["status"] in ("available", "pending")]
                st = STATE[q]
                if st["status"] == "available":
                    return self._json(200, {"unique_code": q,
                                            "container_addr": [f"127.0.0.1:{st['port']}"]})
                if len(active) >= 3:
                    return self._err(409, "invalid_state", "max active containers reached (3)")
                st["status"] = "available"
                return self._json(200, {"unique_code": q,
                                        "container_addr": [f"127.0.0.1:{st['port']}"]})
        if u.path == "/openapi/v1/challenges/close":
            if q not in CHALLENGES:
                return self._err(404, "challenge_not_found", q)
            with LOCK:
                STATE[q]["status"] = "stopped"
            return self._json(200, {"unique_code": q, "closed": True})
        if u.path == "/openapi/v1/challenges/submit":
            body = self._body()
            q = body.get("unique_code", "")
            flag = str(body.get("flag") or "")
            if q not in CHALLENGES:
                return self._err(404, "challenge_not_found", q)
            if not (1 <= len(flag) <= 4096):
                return self._err(422, "validation_error", "flag 长度越界")
            if flag in BUSY_FLAGS:            # 测试开关:平台没受理(不是答错)
                return self._err(503, "resource_unavailable", "靶场资源紧张(mock 强制)")
            with LOCK:
                meta, st = CHALLENGES[q], STATE[q]
                if flag in st["found"]:
                    return self._err(409, "duplicate", "该 flag 已提交过")
                if flag not in meta["flags"]:
                    st["wrong"] += 1
                    return self._json(200, {"correct": False, "awarded": 0,
                                            "cumulative_score": 0,
                                            "correct_flag_count": len(st["found"]),
                                            "total_flag_count": len(meta["flags"]),
                                            "matched_flag_index": None})
                st["found"].add(flag)
                idx = meta["flags"].index(flag)
                per = meta["total_score"] // len(meta["flags"])
                return self._json(200, {"correct": True, "awarded": per,
                                        "cumulative_score": per * len(st["found"]),
                                        "correct_flag_count": len(st["found"]),
                                        "total_flag_count": len(meta["flags"]),
                                        "matched_flag_index": idx})
        return self._err(404, "not_found", self.path)


class VpnHandler(BaseHTTPRequestHandler):
    """VPN 预检地址:任何路径都返回 status=ok。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"client_ip": "127.0.0.1", "status": "ok",
                           "time": time.strftime("%F %T")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TargetHandler(BaseHTTPRequestHandler):
    """每道题的"靶场目标":首页 + 两个可以拿 flag 的入口。"""

    protocol_version = "HTTP/1.1"
    challenge = ""

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        meta = CHALLENGES.get(self.challenge, {})
        flags = meta.get("flags", [])
        if u.path == "/":
            return self._send(200, f"<h1>Company Portal</h1><p>target={self.challenge}</p>"
                                   f"<!-- 内部接口:/api/flag?id=1  /admin/flag -->")
        if u.path == "/api/flag":
            if qs.get("debug", [""])[0] != "1":
                return self._send(403, "forbidden: missing debug=1")
            return self._send(200, json.dumps({"id": qs.get("id", ["1"])[0], "flag": flags[0] if flags else ""}),
                              "application/json")
        if u.path == "/admin/flag":
            if "admin" not in self.headers.get("User-Agent", "").lower():
                return self._send(403, "403 admin only")
            return self._send(200, flags[1] if len(flags) > 1 else (flags[0] if flags else ""))
        return self._send(404, "not found")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform-port", type=int, default=18800)
    ap.add_argument("--vpn-port", type=int, default=18801)
    ap.add_argument("--targets", type=int, default=6)
    ap.add_argument("--env-out", default=str(Path(__file__).resolve().parent / "mock_env.sh"))
    args = ap.parse_args()

    _init_challenges(args.targets)

    servers = []
    plat = ThreadingHTTPServer(("127.0.0.1", args.platform_port), PlatformHandler)
    servers.append(plat)
    vpn = ThreadingHTTPServer(("127.0.0.1", args.vpn_port), VpnHandler)
    servers.append(vpn)
    for code, st in STATE.items():
        h = type(f"T_{code}", (TargetHandler,), {"challenge": code})
        servers.append(ThreadingHTTPServer(("127.0.0.1", st["port"]), h))
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    env = (
        "# 由 tests/mock_tsec_server.py 生成(离线 mock 用)\n"
        f"export CONTEST_ADAPTER=tsec\n"
        f"export BENCHMARK_BASE_URL=http://127.0.0.1:{args.platform_port}\n"
        f"export BENCHMARK_TOKEN={TOKEN}\n"
        f"export VPN_CHECK_URL=http://127.0.0.1:{args.vpn_port}\n"
    )
    Path(args.env_out).write_text(env, encoding="utf-8")
    print(f"mock 平台已启动:http://127.0.0.1:{args.platform_port}  VPN 预检:{args.vpn_port}")
    print(f"题目 {len(CHALLENGES)} 道,目标端口 {TARGET_BASE_PORT}..{TARGET_BASE_PORT + len(CHALLENGES) - 1}")
    print(f"环境变量片段已写到 {args.env_out}:")
    print(env)
    print("按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
