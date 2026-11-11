#!/bin/bash
# 用 mock 平台验收真实的用户入口 ./start.sh(不动真实 .env,跑完还原)
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PORT=18870
VPNPORT=18871

cp .env .env.real.bak
cleanup() {
  ./start.sh stop >/dev/null 2>&1
  kill $MOCK 2>/dev/null
  mv .env.real.bak .env
  rm -rf tests/localrun-startsh
  echo "[cleanup] .env 已还原"
}
trap cleanup EXIT

python3 tests/mock_tsec_server.py --platform-port $PORT --vpn-port $VPNPORT --targets 5 \
        --env-out /tmp/mock_env.sh > /tmp/mock_startsh.out 2>&1 &
MOCK=$!
sleep 1.2

cat > .env <<EOF
CONTEST_ADAPTER=tsec
BENCHMARK_BASE_URL=http://127.0.0.1:$PORT
BENCHMARK_TOKEN=mock-benchmark-token
VPN_CHECK_URL=http://127.0.0.1:$VPNPORT
PI_PROVIDER=fake
PI_MODEL=fake
START_WORKERS=4
MIN_WORKERS=1
WORKERS_PER_QUESTION=2
MAX_ACTIVE_CHALLENGES=3
QUESTION_SLICE_SECONDS=120
ROTATE_COOLDOWN=2
WORKER_TIMEOUT=60
MAX_WORKER_TIMEOUT=60
POLL_INTERVAL=2
ROUND_WINDOW_MINUTES=3
KEEP_RUNNING=0
NET_PROXY=direct
EOF
export FAKE_PI_ROOT="$ROOT"
export PATH="$ROOT/tests/fake_pi:$PATH"

echo "########## 1) ./start.sh --check-only ##########"
./start.sh --check-only
echo "(check-only 退出码 $?)"

echo
echo "########## 2) ./start.sh --no-dashboard(真跑) ##########"
./start.sh --no-dashboard --project startsh-test
sleep 12
echo
echo "########## 3) ./start.sh status ##########"
./start.sh status --project startsh-test 2>/dev/null || ./start.sh status
echo
echo "########## 4) 结果 ##########"
python3 - <<PY
import json, pathlib, urllib.request
run = pathlib.Path("$ROOT/projects/startsh-test")
board = json.loads((run / "board.json").read_text(encoding="utf-8"))
qs = board["questions"]
solved = [q for q, e in qs.items() if e["status"] == "solved"]
flags = sum(int(e.get("correct_flag_count") or 0) for e in qs.values())
total = sum(int(e.get("flag_count") or 1) for e in qs.values())
print(f"题目 {len(qs)} 道,通关 {len(solved)} 道,flag {flags}/{total}")
req = urllib.request.Request("http://127.0.0.1:$PORT/openapi/v1/challenges",
                             headers={"BENCHMARK_TOKEN": "mock-benchmark-token"})
# 必须显式绕代理:macOS 系统代理开着时 urllib 会自动走它,请求回环地址会被代理拒(实测 502)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
plat = json.loads(opener.open(req, timeout=5).read())
print("平台侧通关:", sum(1 for c in plat if c["is_completed"]), "/", len(plat),
      " 仍开着容器:", sum(1 for c in plat if c["container_status"] == "available"))
PY
echo
echo "########## 5) ./start.sh stop ##########"
./start.sh stop
echo "退出码 $?"
