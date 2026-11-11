#!/usr/bin/env python3
"""新榜单适配器(newrank_api.py)离线测试:不联网、不调模型。

  python3 tests/newrank_test.py

覆盖:
  - /api/getquestion 题单 → 一次性票据换 Cookie(303 + Set-Cookie)→ 干净 URL
  - 题单落盘缓存(第二次 list_questions 不再打接口);refresh_questions 强制重拉
  - /api/submitflag 的 Correct / Wrong / 已提交过 / 401 四种响应形状
  - 适配器补的 correct_flag_count/total_flag_count(提交工具靠它标 solved)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    print(("  \033[32m✔\033[0m " if cond else "  \033[31m✘\033[0m ") + msg)
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(msg)


class Mock(BaseHTTPRequestHandler):
    calls: dict[str, int] = {}
    tickets: dict[str, str] = {}          # ticket -> cookie value
    issued = 0

    def log_message(self, *a):            # 安静
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/getquestion":
            self.calls["getquestion"] = self.calls.get("getquestion", 0) + 1
            if body.get("token") != "tok":
                return self._json(401, {"message": "Invalid token", "result": "Error"})
            stage = int(body.get("stage") or 1)
            base = f"http://127.0.0.1:{self.server.server_port}"
            qs = []
            if stage == 1:
                for qid in (1, 2):
                    Mock.issued += 1
                    ticket = f"t{qid}-{Mock.issued}"
                    Mock.tickets[ticket] = f"cookie{qid}-{Mock.issued}"
                    qs.append({"question_id": qid, "instruction": f"题目 {qid}",
                               "url": f"{base}/q/{qid}?__ctf_ticket={ticket}"})
            elif stage == 2:
                qs = [
                    {"question_id": 41, "url": "",
                     "instruction": f"逆向 Chompy 的旗帜制作程序。\n\n附件下载 1: {base}/att/workshop"},
                    {"question_id": 42, "url": "",
                     "instruction": f"老师说程序需要数百亿年才会结束。\n\n附件下载 1: {base}/att/slow"},
                ]
            return self._json(200, {"questions": qs})
        if self.path == "/api/submitflag":
            self.calls["submit"] = self.calls.get("submit", 0) + 1
            if body.get("token") != "tok":
                return self._json(401, {"message": "Invalid token", "result": "Error"})
            flag = str(body.get("flag") or "")
            if flag == "flag{good}":
                return self._json(200, {"result": "Correct", "message": "Flag正确！+100分", "new_score": 100})
            if flag == "flag{dup}":
                return self._json(200, {"result": "Correct", "message": "Flag正确，但已经提交过",
                                        "current_score": 100})
            return self._json(200, {"result": "Wrong", "message": "Flag错误"})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path.startswith("/q/"):
            qs = urllib.parse.parse_qs(u.query)
            ticket = (qs.get("__ctf_ticket") or [""])[0]
            if ticket:
                self.calls["ticket"] = self.calls.get("ticket", 0) + 1
                if ticket not in Mock.tickets:
                    return self._json(403, {"error": "invalid runtime ticket"})
                cookie = Mock.tickets.pop(ticket)      # 一次性
                self.send_response(303)
                self.send_header("Location", u.path)
                self.send_header("Set-Cookie", f"__ctf_runtime={cookie}; Path=/; HttpOnly")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            cookie = self.headers.get("Cookie") or ""
            if "__ctf_runtime=" in cookie:
                body = b"<html>ok</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._json(403, {"error": "runtime authentication required"})
        return self._json(404, {"error": "not found"})


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    tmp = Path(tempfile.mkdtemp(prefix="newrank-test-"))
    base = f"http://127.0.0.1:{srv.server_port}"
    os.environ.update({"ROTOM_RUN_DIR": str(tmp), "NEWRANK_BASE_URL": base,
                       "BENCHMARK_BASE_URL": base, "BENCHMARK_TOKEN": "tok",
                       "NEWRANK_STAGE": "1"})
    import newrank_api as nr
    nr._CACHE.update({"at": 0.0, "stage": "", "questions": []})

    print("== 题单 + 票据换 Cookie ==")
    qs = nr.list_questions()
    check(len(qs) == 2, f"拉到 2 道题(实际 {len(qs)})")
    q1 = qs[0]
    check(q1["question_id"] == "1", "question_id 转成字符串")
    check("__ctf_ticket" not in q1["connection"]["url1"], "connection 里是去掉票据的干净 URL")
    check("__ctf_runtime=" in q1["http_cookie"], "拿到了会话 Cookie")
    check("cookies.txt" in q1["description"], "描述里告诉 agent 用 cookies.txt")
    check(Mock.calls.get("ticket") == 2, "两张票据各兑换一次")
    check((tmp / "logs" / "newrank_questions.json").exists(), "题单已落盘缓存")

    print("== 缓存 / 强制刷新 ==")
    before = Mock.calls.get("getquestion", 0)
    nr.list_questions()
    check(Mock.calls.get("getquestion", 0) == before, "第二次 list_questions 复用缓存,不打接口")
    nr.refresh_questions()
    check(Mock.calls.get("getquestion", 0) == before + 1, "refresh_questions 强制重新拉题")

    print("== 阶段 2/3:静态附件 + 类别推断 + 分阶段合并 ==")
    c1 = [q for q in nr.list_questions() if q["question_id"] == "1"][0]["http_cookie"]
    before2 = Mock.calls.get("getquestion", 0)
    os.environ["NEWRANK_STAGE"] = "1,2"
    nr._CACHE.update({"at": 0.0, "stage": "", "questions": []})       # 模拟进程重启
    merged = nr.list_questions()
    check(len(merged) == 4, f"合并后 4 道题(实际 {len(merged)})")
    check(Mock.calls.get("getquestion", 0) == before2 + 1, "只补拉缺的阶段 2,不重拉阶段 1")
    c1b = [q for q in merged if q["question_id"] == "1"][0]["http_cookie"]
    check(c1 == c1b, "阶段 1 的会话 Cookie 没被翻新")
    q41 = [q for q in merged if q["question_id"] == "41"][0]
    check(q41["category"] == "reverse", "阶段 2 静态题判成 reverse")
    check(q41["connection"] == {}, "静态附件题没有远程入口")
    check(q41["attachments"] and q41["file_url"].endswith("/att/workshop"), "解析出附件并设 file_url")
    check("静态附件题" in q41["description"], "描述里说明附件已下载到 files/")
    check(nr._infer_category("在支持多种货币的站点探索换汇逻辑", 3, True) == "web", "阶段 3 在线站点 → web")
    check(nr._infer_category("素数模数和 RSA 参数", 3, False) == "crypto", "RSA → crypto")
    check(nr._infer_category("从流量中识别攻击者所用工具", 3, False) == "forensics", "流量 → forensics")
    check(nr._infer_category("绕过图片校验并获取本题 flag", 1, True) == "web", "阶段 1 图片题仍是 web")
    os.environ["NEWRANK_STAGE"] = "1"

    print("== 提交响应形状 ==")
    ok = nr.submit_flag("tok", "1", "flag{good}")
    check(nr.is_correct(ok) and nr.is_verdict(ok), "Correct → 判定正确")
    check(ok.get("correct_flag_count") == 1 and ok.get("total_flag_count") == 1,
          "补了 1/1 计数(提交工具靠它标 solved)")
    bad = nr.submit_flag("tok", "1", "flag{bad}")
    check(nr.is_verdict(bad) and not nr.is_correct(bad), "Wrong → 受理但判错")
    dup = nr.submit_flag("tok", "1", "flag{dup}")
    check(nr.is_correct(dup) and dup.get("duplicate"), "已提交过 → 仍算正确,标记 duplicate")
    err = nr.submit_flag("bad", "1", "flag{good}")
    check(err.get("code") == "invalid_token" and err.get("task_over"), "401 → invalid_token + task_over")

    print("== 重试口径 ==")
    check(not nr.is_rate_limited(bad), "判定错误不是限流,不重试")
    check(nr.is_rate_limited({"code": 429, "message": "too many"}), "429 → 可重试")

    srv.shutdown()
    print("\n" + "=" * 46)
    status = "\033[32m" if FAIL == 0 else "\033[31m"
    print(f"{status}通过 {PASS} 项,失败 {FAIL} 项\033[0m")
    if FAILED:
        for f in FAILED:
            print("  - " + f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())