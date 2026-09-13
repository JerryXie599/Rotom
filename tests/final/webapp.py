#!/usr/bin/env python3
"""有漏洞的静态文件服务:把 URL 解码后直接拼路径,存在 ../ 穿越(CTF 常见题型)。"""
import http.server, socketserver, urllib.parse, pathlib, sys
BASE = pathlib.Path(__file__).resolve().parent / "public"
BASE.mkdir(exist_ok=True)
(BASE / "index.html").write_text("<h1>NSS-style file service</h1><p>try /files/xxx</p>")
SECRET = pathlib.Path(__file__).resolve().parent / "secret_flag.txt"

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/files/"):
            target = BASE / urllib.parse.unquote(p[len("/files/"):])   # 漏洞:未做路径规范化
            try:
                data = target.read_bytes()
            except Exception as e:
                self.send_error(404, str(e)); return
            self.send_response(200); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        if p == "/":
            data = (BASE / "index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        self.send_error(404)
    def log_message(self, *a): pass

port = int(sys.argv[1]) if len(sys.argv) > 1 else 19801
SECRET.write_text(pathlib.Path(sys.argv[2]).read_text() if len(sys.argv) > 2 else "flag{missing}")
print(f"web 服务已起: http://127.0.0.1:{port} (flag 在 {SECRET})", flush=True)
socketserver.TCPServer(("127.0.0.1", port), H).serve_forever()
