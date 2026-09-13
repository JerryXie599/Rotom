#!/bin/bash
# 本地模拟比赛:起 mock 服务器 + (pwn 题)在 pwn64 里起服务 + 用独立项目目录跑 harness。
# 不影响 .env 里的正式配置,也不碰"默认项目"的数据。
#
#   ./tests/local_contest.sh start      # 起服务并开始跑
#   ./tests/local_contest.sh stop       # 停掉
#   ./tests/local_contest.sh status     # 看进度
#
# 日志: tests/localrun/logs/runner.out,mock 服务器输出 tests/mock_server.out
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
PORT=${MOCK_PORT:-8899}
RUN_DIR="$ROOT/tests/localrun"
VM=pwn64

start_pwn_services() {
  # 为 challenges.json 里带 "service" 的 pwn 题在 VM 里用 socat 起服务
  python3 - "$ROOT" "$VM" <<'PY'
import json, subprocess, sys, pathlib
root, vm = sys.argv[1], sys.argv[2]
data = json.loads(pathlib.Path(root, "tests/challenges.json").read_text())
for c in data:
    svc = c.get("service")
    if not svc:
        continue
    binary = svc["binary"]           # VM 里的绝对路径
    port = svc["port"]
    flag = c["flag"]
    workdir = f"/tmp/wqh_{c['id']}"
    # 如果题目给了本地二进制,先拷进 VM(orb cp 不可用时退回 cat 管道)
    src = svc.get("binary_local")
    if src:
        srcp = pathlib.Path(root, "tests/challenge_files", src)
        if srcp.exists():
            subprocess.run(["orb", "-m", vm, "bash", "-lc", f"mkdir -p {workdir}"], capture_output=True)
            with open(srcp, "rb") as f:
                subprocess.run(["orb", "-m", vm, "bash", "-lc",
                                f"cat > {workdir}/{pathlib.Path(src).name} && "
                                f"chmod +x {workdir}/{pathlib.Path(src).name}"], stdin=f)
            binary = f"{workdir}/{pathlib.Path(src).name}"
    cmd = (f"mkdir -p {workdir} && cd {workdir} && "
           f"printf '%s' '{flag}' > flag.txt && "
           # 按端口找 PID 再杀,避免用 pkill -f 匹配到自己这条命令把 shell 杀了
           f"for pid in $(ss -lntp 2>/dev/null | grep ':{port} ' | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); "
           f"do kill -9 $pid 2>/dev/null; done; sleep 0.2; "
           f"(setsid socat TCP-LISTEN:{port},reuseaddr,fork EXEC:'{binary}',pty,stderr "
           f">/tmp/wqh_{c['id']}/socat.log 2>&1 &) ; sleep 0.5; "
           f"echo started_{port}")
    r = subprocess.run(["orb", "-m", vm, "bash", "-lc", cmd], capture_output=True, text=True)
    print(f"[pwn] {c['title']} :{port} -> {r.stdout.strip() or r.stderr.strip()[:120]}")
PY
}

case "${1:-start}" in
  start)
    mkdir -p "$RUN_DIR/logs"
    echo "== 1/3 起 mock 比赛服务器 =="
    pkill -f mock_contest_server.py 2>/dev/null
    nohup python3 tests/mock_contest_server.py --port "$PORT" ${ONLY:+--only "$ONLY"} \
      > tests/mock_server.out 2>&1 &
    sleep 2
    head -3 tests/mock_server.out

    echo "== 2/3 起 pwn 远程服务(在 $VM 里) =="
    start_pwn_services

    echo "== 3/3 启动 harness(独立项目目录 $RUN_DIR) =="
    pkill -f "runner.py" 2>/dev/null; sleep 1
    set -a; . ./.env; set +a
    env WQH_RUN_DIR="$RUN_DIR" \
        ${MODEL_PROVIDER:+PI_PROVIDER=$MODEL_PROVIDER} ${MODEL_NAME:+PI_MODEL=$MODEL_NAME} \
        ${LOCAL_MODEL:+PI_PROVIDER=mac-local PI_MODEL=$LOCAL_MODEL} \
        CONTEST_BASE="http://127.0.0.1:$PORT" \
        CONTEST_QUERY_PATH=/query CONTEST_RESET_PATH=/reset CONTEST_SUBMIT_PATH=/submit \
        IGNORE_SOLVED=1 KEEP_RUNNING=0 \
        nohup python3 runner.py > "$RUN_DIR/logs/runner.out" 2>&1 &
    sleep 3
    tail -3 "$RUN_DIR/logs/runner.out"
    echo
    echo "看进度:   $0 status"
    echo "看结果:   cat tests/mock_server.out | tail -20"
    echo "停下:     $0 stop"
    ;;
  stop)
    pkill -f mock_contest_server.py && echo "mock 服务器已停"
    pkill -f "runner.py" && echo "runner 已停(worker 会被优雅收掉)"
    sleep 2
    ps -eo comm | grep -c "^pi$" | xargs echo "残留 pi 进程:"
    ;;
  status)
    echo "== runner =="; tail -6 "$RUN_DIR/logs/runner.out" 2>/dev/null
    echo "== 解题结果 =="; tail -16 tests/mock_server.out 2>/dev/null
    ;;
  *)
    echo "用法: $0 {start|stop|status}"; exit 1;;
esac
