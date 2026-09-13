#!/bin/bash
# 高并发压测:12 道取证副本 + 2 道 pwn 副本,默认 24 并发(现场量级的一半)
#   ./tests/load/run_load.sh [并发数]      # 默认 24,可传 30/36 加压
#   ./tests/load/run_load.sh stop|status
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
ROOT=$(pwd)
RUN_DIR="$ROOT/tests/load/run"
PORT=${MOCK_PORT:-8901}
N=${1:-24}

case "${1:-}" in
  stop)
    pkill -f "mock_contest_server.py --port $PORT" 2>/dev/null && echo "mock 已停"
    pkill -f "python3 runner.py" 2>/dev/null && echo "runner 已停"
    sleep 2; ps -eo comm | grep -c "^pi$" | xargs echo "残留 pi:"; exit 0;;
  status)
    tail -8 "$RUN_DIR/logs/runner.out" 2>/dev/null
    echo "--- 结果 ---"; tail -14 tests/load/mock.out 2>/dev/null
    exit 0;;
esac

rm -rf "$RUN_DIR"; mkdir -p "$RUN_DIR/logs"
pkill -f "mock_contest_server.py --port $PORT" 2>/dev/null

echo "== 1/3 起 pwn 服务(在 pwn64 里) =="
# 先杀掉占用端口的旧服务(否则新服务绑不上,agent 会打到旧配置上——踩过这个坑)
orb -m pwn64 bash -lc 'for pid in $(ps -eo pid,args | grep "[s]ocat TCP-LISTEN" | awk "{print \$1}"); do kill -9 $pid 2>/dev/null; done' 2>/dev/null
sleep 1
python3 - <<'PY'
import json, pathlib, subprocess
qs = json.loads(pathlib.Path("tests/load/challenges.json").read_text())
for q in qs:
    svc = q.get("service")
    if not svc:
        continue
    wd, port, flag = f"/tmp/wqh_{q['id']}", svc["port"], q["flag"]
    exec_part = "stdbuf -o0 ./pwn01_chall"
    subprocess.run(["orb", "-m", "pwn64", "bash", "-lc", f"mkdir -p {wd}"], capture_output=True)
    src = pathlib.Path("tests/load/files", svc["binary_local"])
    with open(src, "rb") as f:
        r = subprocess.run(["orb", "-m", "pwn64", "bash", "-lc",
             f"cat > {wd}/pwn01_chall && chmod +x {wd}/pwn01_chall && "
             f"printf '%s' '{flag}' > {wd}/flag.txt && cd {wd} && "
             f"(setsid socat TCP-LISTEN:{port},reuseaddr,fork EXEC:'{exec_part}',stderr >/dev/null 2>&1 &) && sleep 0.4 && "
             f"ss -lnt | grep -q ':{port} ' && echo OK"], stdin=f, capture_output=True, text=True)
    # 校验确实是新起的服务(检查 socat 命令行里没有 pty)
    chk = subprocess.run(["orb", "-m", "pwn64", "bash", "-lc",
                          f"ps -eo args | grep '[s]ocat TCP-LISTEN:{port}' | grep -c pty"],
                         capture_output=True, text=True).stdout.strip()
    state = "就绪" if ("OK" in r.stdout and chk == "0") else f"异常(pty残留={chk})"
    print(f"  [pwn] {q['title']} :{port} -> {state}")
PY

echo "== 2/3 起 mock 比赛服务器(自定义题集) =="
nohup python3 tests/mock_contest_server.py --port "$PORT" \
      --challenges "${CHALLENGES:-tests/load/challenges.json}" --files tests/load/files \
      > tests/load/mock.out 2>&1 &
sleep 2
head -2 tests/load/mock.out

echo "== 3/3 启动 runner(并发 $N,窗口 30 分钟) =="
set -a; . ./.env; set +a
env WQH_RUN_DIR="$RUN_DIR" \
    CONTEST_BASE="http://127.0.0.1:$PORT" \
    CONTEST_QUERY_PATH=/query CONTEST_RESET_PATH=/reset CONTEST_SUBMIT_PATH=/submit \
    START_WORKERS="$N" MIN_WORKERS=4 IGNORE_SOLVED=1 KEEP_RUNNING=0 \
    nohup python3 runner.py > "$RUN_DIR/logs/runner.out" 2>&1 &
sleep 5
tail -3 "$RUN_DIR/logs/runner.out"
echo
echo "观察: ./tests/load/run_load.sh status   停止: ./tests/load/run_load.sh stop"
