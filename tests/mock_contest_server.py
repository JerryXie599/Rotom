#!/usr/bin/env python3
"""本地模拟比赛服务器:和官方接口**同构**,用于在平台关闭/赛前做端到端压测。

  python3 tests/mock_contest_server.py --port 8899 [--only crypto,web]

接口(路径可自定义,默认与官方一致):
  GET /query?token=...                          -> {"code":0,"message":"查询成功","data":[...]}
  GET /reset?token=...&question_id=...          -> {"code":0,"message":"操作成功"}
  GET /submit?token=...&question_id=...&answer= -> 正确:{"code":0,"message":"恭喜您，回答正确","status":1}
                                                   错误:{"code":0,"message":"答案错误","status":0}
  GET /files/<name>                             -> 题目附件下载

题目定义在 tests/challenges.json(由 fetch_challenges.py 生成):
  [{"id","title","category","score","description","file","flag","connection"(可选)}]

服务器会记录每题**首次提交时间 / 正确提交时间**,结束时打印排行榜式的耗时表,
用来衡量 agent 的解题速度。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
CHALLENGES = TESTS / "challenges.json"
FILES_DIR = TESTS / "challenge_files"
LOG = TESTS / "mock_server.log"

STATE: dict[str, dict] = {}
CHALLENGES_DATA: list[dict] = []
START_TS = time.time()


def load_challenges() -> list[dict]:
    data = json.loads(CHALLENGES.read_text(encoding="utf-8"))
    out = []
    for i, c in enumerate(data):
        out.append({
            "question_id": c.get("id") or f"Q{i:03d}",
            "title": c["title"], "category": c.get("category", "misc"),
            "score": c.get("score", 500), "real_score": c.get("score", 500),
            "file": c.get("file", ""), "flag": c["flag"],
            "description": c.get("description", ""),
            "connection": c.get("connection") or [],
            "attributes": c.get("attributes") or [],
            "capabilities": c.get("capabilities") or [],
            "extensions": c.get("extensions") or {},
        })
    return out


def public_view(q: dict, base: str) -> dict:
    """按官方返回结构输出(含 file_url 的转义形式)。"""
    file_url = ""
    if q.get("file"):
        file_url = f"{base}/files/{urllib.parse.quote(q['file'])}"
    return {
        "question_id": q["question_id"], "title": q["title"], "score": q["score"],
        "real_score": q["real_score"], "file_url": file_url,
        "is_solved": bool(STATE[q["question_id"]]["solved"]),
        "solved_number": sum(1 for s in STATE.values() if s["solved"]),
        "category": q["category"], "attributes": q["attributes"],
        "description": q["description"], "interactive": "true" if q["connection"] else "false",
        "capabilities": q["capabilities"], "connection": q["connection"],
        "extensions": q["extensions"],
    }


def record(event: str, **kw) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%F %T"), "event": event, **kw},
                           ensure_ascii=False) + "\n")


def report() -> str:
    rows = []
    for q in CHALLENGES_DATA:
        st = STATE[q["question_id"]]
        t0 = st.get("first_seen") or st.get("first_submit")
        dur = (st["solved_at"] - t0) if (st["solved"] and t0) else None
        rows.append((q["category"], q["title"], st["solved"], st["attempts"], dur))
    lines = ["", "=" * 62, f"{'类别':<10}{'题目':<26}{'结果':<6}{'提交':<6}耗时"]
    for cat, title, solved, attempts, dur in rows:
        lines.append(f"{cat:<10}{title[:24]:<26}{'✔' if solved else '✘':<6}{attempts:<6}"
                     f"{f'{dur:.0f}s' if dur else '-'}")
    ok = sum(1 for r in rows if r[2])
    durs = [r[4] for r in rows if r[4]]
    lines.append(f"共 {len(rows)} 题,解出 {ok} 题"
                 + (f",平均 {sum(durs)/len(durs):.0f}s,最快 {min(durs):.0f}s,最慢 {max(durs):.0f}s" if durs else ""))
    lines.append("=" * 62)
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        base = f"http://{self.headers.get('Host') or '127.0.0.1'}"

        if u.path == "/query":
            now = time.time()
            for q in CHALLENGES_DATA:  # 记录每道题第一次被下发的时刻,用于算解题耗时
                st = STATE[q["question_id"]]
                if st.get("first_seen") is None:
                    st["first_seen"] = now
            self._json({"code": 0, "message": "查询成功",
                        "data": [public_view(q, base) for q in CHALLENGES_DATA]})
        elif u.path == "/reset":
            self._json({"code": 0, "message": "操作成功"})
        elif u.path == "/submit":
            qid = (qs.get("question_id") or [""])[0]
            ans = (qs.get("answer") or [""])[0]
            q = next((x for x in CHALLENGES_DATA if x["question_id"] == qid), None)
            if not q:
                self._json({"code": 0, "message": "题目不存在", "status": 0})
                return
            st = STATE[qid]
            st["attempts"] += 1
            if st["first_submit"] is None:
                st["first_submit"] = time.time()
            if ans.strip() == q["flag"].strip():
                if not st["solved"]:
                    st["solved"] = True
                    st["solved_at"] = time.time()
                    t0 = st.get("first_seen") or st["solved_at"]
                    record("solved", title=q["title"], category=q["category"],
                           seconds=round(st["solved_at"] - t0, 1), attempts=st["attempts"])
                    print(f"[SOLVED] {q['title']} ({q['category']}) "
                          f"用时 {st['solved_at'] - t0:.0f}s / {st['attempts']} 次提交", flush=True)
                    print(report(), flush=True)
                self._json({"code": 0, "message": "恭喜您，回答正确", "status": 1})
            else:
                record("wrong", title=q["title"], answer=ans[:80])
                self._json({"code": 0, "message": "答案错误", "status": 0})
        elif u.path.startswith("/files/"):
            name = urllib.parse.unquote(u.path[len("/files/"):])
            p = (FILES_DIR / name).resolve()
            if not str(p).startswith(str(FILES_DIR.resolve())) or not p.exists():
                self.send_error(404)
                return
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            record("download", file=name, bytes=len(data))
        else:
            self.send_error(404)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    global CHALLENGES_DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--only", help="只启用某些类别,逗号分隔,如 crypto,web")
    args = ap.parse_args()

    CHALLENGES_DATA = load_challenges()
    if args.only:
        keep = {c.strip() for c in args.only.split(",")}
        CHALLENGES_DATA = [q for q in CHALLENGES_DATA if q["category"] in keep]
    STATE.clear()
    for q in CHALLENGES_DATA:
        STATE[q["question_id"]] = {"solved": False, "attempts": 0,
                                   "first_seen": None, "first_submit": None, "solved_at": None}
    LOG.write_text("", encoding="utf-8")
    print(f"模拟比赛服务器: http://{args.host}:{args.port}")
    print(f"题目 {len(CHALLENGES_DATA)} 道: " +
          ", ".join(f"{q['title']}[{q['category']}]" for q in CHALLENGES_DATA))
    print("把 harness 指过来: CONTEST_BASE=http://127.0.0.1:%d "
          "CONTEST_QUERY_PATH=/query CONTEST_RESET_PATH=/reset CONTEST_SUBMIT_PATH=/submit"
          % args.port)
    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print(report())


if __name__ == "__main__":
    main()
