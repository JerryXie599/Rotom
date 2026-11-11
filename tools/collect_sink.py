#!/usr/bin/env python3
"""本地落盘小服务:浏览器页面把抓到的平台 JSON POST 过来,直接写到磁盘。

用途:抓 TSecBench 的榜单/会话数据时,避免把大 JSON 灌进对话上下文。
页面侧用 fetch 调平台接口,再 POST 到这里落盘,之后在本地离线解析。

启动: python3 tools/collect_sink.py 8899 /tmp/tsec_dump
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/tsec_dump")
OUT.mkdir(parents=True, exist_ok=True)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST,GET,OPTIONS")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/ping":
            body = b'{"ok":true}'
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        name = (parse_qs(u.query).get("name") or ["dump"])[0]
        name = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:120] or "dump"
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        p = OUT / name
        if not p.suffix:
            p = p.with_suffix(".json")
        p.write_bytes(raw)
        sys.stderr.write(f"[sink] {p} {len(raw)}B\n")
        sys.stderr.flush()
        body = json.dumps({"saved": str(p), "bytes": len(raw)}).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"sink listening on 127.0.0.1:{PORT} -> {OUT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()