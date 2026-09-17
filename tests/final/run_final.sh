#!/bin/bash
# 最终验收测试:起 5 类题目环境 + 模拟比赛接口。**不启动 runner** —— runner 请在网页控制台点「开始跑」。
#   ./tests/final/run_final.sh start    # 起环境(web/pwn 服务 + mock 接口)
#   ./tests/final/run_final.sh stop     # 停环境
#   ./tests/final/run_final.sh status   # 看结果
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
ROOT=$(pwd)
D="$ROOT/tests/final"
PORT=${MOCK_PORT:-19701}
WEB_PORT=19801
PWN_PORT=19802

case "${1:-start}" in
  stop)
    pkill -f "mock_contest_server.py --port $PORT" 2>/dev/null && echo "mock 已停"
    pkill -f "tests/final/webapp.py" 2>/dev/null && echo "web 服务已停"
    pkill -f "python3 runner.py" 2>/dev/null && echo "runner 已停"
    orb -m pwn64 bash -lc 'for pid in $(ps -eo pid,args | grep "[s]ocat TCP-LISTEN" | awk "{print \$1}"); do kill -9 $pid 2>/dev/null; done' 2>/dev/null
    echo "pwn 服务已停"; exit 0;;
  status)
    echo "== runner =="; tail -4 "$ROOT/logs/runner.out" 2>/dev/null || tail -4 "$ROOT/projects/final-test/logs/runner.out" 2>/dev/null
    echo "== 成绩 =="; tail -14 "$D/mock.out" 2>/dev/null; exit 0;;
esac

echo "== 1/3 起 pwn 服务(在 pwn64 里,stdio 无 pty) =="
orb -m pwn64 bash -lc 'for pid in $(ps -eo pid,args | grep "[s]ocat TCP-LISTEN" | awk "{print \$1}"); do kill -9 $pid 2>/dev/null; done' 2>/dev/null
python3 - <<'PY'
import json, pathlib, subprocess
qs = json.loads(pathlib.Path("tests/final/challenges.json").read_text())
for q in qs:
    svc = q.get("service")
    if not svc: continue
    wd, port, flag = f"/tmp/rotom_{q['id']}", svc["port"], q["flag"]
    subprocess.run(["orb","-m","pwn64","bash","-lc",f"mkdir -p {wd}"], capture_output=True)
    with open(pathlib.Path("tests/final/files", svc["binary_local"]), "rb") as f:
        r = subprocess.run(["orb","-m","pwn64","bash","-lc",
            f"cat > {wd}/pwn01_chall && chmod +x {wd}/pwn01_chall && printf '%s' '{flag}' > {wd}/flag.txt && cd {wd} && "
            f"(setsid socat TCP-LISTEN:{port},reuseaddr,fork EXEC:'stdbuf -o0 ./pwn01_chall',stderr >/dev/null 2>&1 &) && sleep 0.4 && "
            f"ss -lnt | grep -q ':{port} ' && ps -eo args | grep '[s]ocat TCP-LISTEN:{port}' | grep -c pty"],
            stdin=f, capture_output=True, text=True)
    print(f"  [pwn] {q['title']} :{port} -> {'就绪(无 pty)' if r.stdout.strip().endswith('0') else '异常'}")
PY

echo "== 2/3 起 web 服务(存在路径穿越) =="
pkill -f "tests/final/webapp.py" 2>/dev/null; sleep 0.5
printf '%s' "flag{w3b_path_tr4versal_9b2c}" > "$D/secret_flag.txt"
nohup python3 "$D/webapp.py" "$WEB_PORT" "$D/secret_flag.txt" > "$D/web.out" 2>&1 &
sleep 1.5
curl -s -o /dev/null -w "  web 服务: HTTP %{http_code}(首页)\n" "http://127.0.0.1:$WEB_PORT/"
echo "  穿越验证: $(curl -s "http://127.0.0.1:$WEB_PORT/files/..%2fsecret_flag.txt" || echo '(需编码尝试)')"

echo "== 3/3 起 mock 比赛接口 =="
pkill -f "mock_contest_server.py --port $PORT" 2>/dev/null; sleep 0.5
nohup python3 tests/mock_contest_server.py --port "$PORT" --challenges tests/final/challenges.json \
      --files tests/final/files > "$D/mock.out" 2>&1 &
sleep 2
head -2 "$D/mock.out"

cat <<EOF

环境已就绪(5 道题:web/pwn/reverse/crypto/forensics)
  mock 接口: http://127.0.0.1:$PORT   (控制台里填 CONTEST_BASE=$PORT, 路径 /query /reset /submit)
  web 题目 : http://127.0.0.1:$WEB_PORT
  pwn 题目 : nc 192.168.139.136:$PWN_PORT

下一步请在网页控制台操作(不要用命令行起 runner):
  1) 控制台 → 填接口与模型 → 保存配置 → 测试接口 / 测试模型
  2) 总览 → 新建项目 final-test → 开始跑
EOF
