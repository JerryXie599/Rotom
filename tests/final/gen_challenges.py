#!/usr/bin/env python3
"""生成覆盖五个方向的最终验收题集(每类一题,都是可离线自解的真实环境)。

web       : 本地 HTTP 服务,存在路径穿越,flag 在服务目录之外
pwn       : ret2libc(已有二进制),socat 起服务
reverse   : 自编二进制,flag 以 XOR 混淆存在 .rodata,需逆向提取
crypto    : RSA e=3 小公钥指数攻击,给出 n/e/c
forensics : 真实 pcap(SQL 注入外带),flag 藏在 HTTP 响应里
"""
import json, pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
FILES = HERE / "files"
FILES.mkdir(parents=True, exist_ok=True)
FLAGS = {
    "web":       "flag{w3b_path_tr4versal_9b2c}",
    "pwn":       "flag{07570f15-6b52-4833-890a-f1872cbc89a1}",
    "reverse":   "flag{r3v_x0r_0bfusc4t10n_7f3a}",
    "crypto":    "flag{sm4ll_e_rs4_4tt4ck_5d81}",
    "forensics": "flag{a0d78ce4-8072-4e14-939b-cd78392a34f4}",
}

def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)

# ---------- reverse: 生成 C 源码,flag 逐字节 XOR 0x5A 后放数组 ----------
flag = FLAGS["reverse"]
enc = ",".join(str(b ^ 0x5A) for b in flag.encode())
src = f'''
#include <stdio.h>
#include <string.h>
static unsigned char enc[] = {{{enc},0}};   /* 每个字节 XOR 0x5A */
int main(void) {{
    char buf[128];
    printf("input the password: ");
    if (!fgets(buf, sizeof(buf), stdin)) return 1;
    buf[strcspn(buf, "\\n")] = 0;
    char dec[128];
    for (unsigned i = 0; i < sizeof(enc); i++) dec[i] = enc[i] ^ 0x5A;
    if (strcmp(buf, dec) == 0) printf("Correct! %s\\n", dec);
    else printf("Wrong!\\n");
    return 0;
}}
'''
(HERE / "rev_src.c").write_text(src, encoding="utf-8")
# 在 pwn64 里编译(i386 32 位,增加逆向难度但仍是静态分析)
sh(f"orb -m pwn64 bash -lc 'cat > /tmp/rev_src.c' < {HERE/'rev_src.c'}")
sh("orb -m pwn64 bash -lc 'gcc -m32 -O0 -o /tmp/rev_check /tmp/rev_src.c 2>/dev/null || gcc -O0 -o /tmp/rev_check /tmp/rev_src.c'")
sh(f"orb -m pwn64 bash -lc 'cat /tmp/rev_check' > {FILES/'rev_check'}")
print("reverse 二进制:", (FILES/'rev_check').stat().st_size, "字节,flag =", FLAGS["reverse"])

# ---------- crypto: e=3 小公钥指数,m^3 < n ----------
sys.path.insert(0, str(HERE.parent.parent))
from Crypto.Util.number import bytes_to_long, getPrime
m = bytes_to_long(FLAGS["crypto"].encode())
e = 3
p, q = getPrime(2048), getPrime(2048)
n = p * q
c = pow(m, e, n)
(HERE / "files" / "rsa_challenge.txt").write_text(
    f"n = {n}\ne = {e}\nc = {c}\n\n# 明文较短且 e 很小,直接开三次方即可\n", encoding="utf-8")
print("crypto: n/e/c 已生成(小公钥指数)")

# ---------- web: 起一个存在路径穿越的服务 ----------
webapp = HERE / "webapp.py"
webapp.write_text('''#!/usr/bin/env python3
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
''', encoding="utf-8")

challenges = [
  {"id": "FINAL_WEB", "title": "web-file-service", "category": "web", "score": 500,
   "description": "一个文件服务:GET /files/<文件名> 读取 public/ 下的文件。请想办法读到服务目录之外的文件。",
   "flag": FLAGS["web"], "file": "",
   "connection": {"docker_url": "http://127.0.0.1:19801", "docker_ip": "127.0.0.1", "docker_port": "19801"},
   "web_service": True},
  {"id": "FINAL_PWN", "title": "pwn-ret2libc", "category": "pwn", "score": 500,
   "description": "64 位 ELF 栈溢出(附件含二进制与 libc)。服务端拿到 shell 后 flag 在 flag.txt。",
   "flag": FLAGS["pwn"], "file": "pwn01.zip",
   "service": {"binary": "/tmp/wqh_FINAL_PWN/pwn01_chall", "binary_local": "pwn01_chall", "port": 19802},
   "connection": {"docker_url": "nc 192.168.139.136 19802", "docker_ip": "192.168.139.136", "docker_port": "19802"}},
  {"id": "FINAL_REV", "title": "reverse-xor", "category": "reverse", "score": 500,
   "description": "附件是一个校验口令的程序,passphrase 就是 flag。请逆向得到它。",
   "flag": FLAGS["reverse"], "file": "rev_check"},
  {"id": "FINAL_CRYPTO", "title": "crypto-small-e", "category": "crypto", "score": 500,
   "description": "附件给出 RSA 的 n、e、c(明文就是 flag)。",
   "flag": FLAGS["crypto"], "file": "rsa_challenge.txt"},
  {"id": "FINAL_FORENSIC", "title": "forensics-pcap", "category": "forensics", "score": 500,
   "description": "流量分析:flag 被 SQL 注入外带,藏在 HTTP 响应里(附件是抓包文件)。",
   "flag": FLAGS["forensics"], "file": "sqldata.pcapng"},
]
(HERE / "challenges.json").write_text(json.dumps(challenges, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n题集已生成: {len(challenges)} 道 -> {HERE/'challenges.json'}")
for c in challenges:
    print(f"  {c['category']:10s} {c['title']:20s} flag={c['flag'][:28]}...")
