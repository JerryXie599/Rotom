#!/bin/bash
# 端到端(离线):mock TSec 平台 + 假 pi,把 harness 全链路跑一遍。
#
# 验证:VPN 预检 → 拉题 → 容器自开自关(≤3)→ 派 agent → 一题多 flag 记账 →
#       通关后关容器放名额 → 下一批题 → 全部完成收工。
#
# 用法:
#   bash tests/run_mock_e2e.sh
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
RUN_DIR="$ROOT/tests/localrun-tsec"
PORT=18860
VPNPORT=18861

rm -rf "$RUN_DIR"; mkdir -p "$RUN_DIR/logs"
python3 tests/mock_tsec_server.py --platform-port "$PORT" --vpn-port "$VPNPORT" \
        --targets 6 --env-out "$RUN_DIR/mock_env.sh" > "$RUN_DIR/mock.out" 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null' EXIT
sleep 1.2

export CONTEST_ADAPTER=tsec
export BENCHMARK_BASE_URL="http://127.0.0.1:$PORT"
export BENCHMARK_TOKEN=mock-benchmark-token
export VPN_CHECK_URL="http://127.0.0.1:$VPNPORT"
export ROTOM_RUN_DIR="$RUN_DIR"
export PI_PROVIDER=fake PI_MODEL=fake
export START_WORKERS=4 MIN_WORKERS=1 WORKERS_PER_QUESTION=2 MAX_HELPERS_PER_QUESTION=3
export MAX_ACTIVE_CHALLENGES=3 QUESTION_SLICE_SECONDS=120 ROTATE_COOLDOWN=2
export WORKER_TIMEOUT=60 TIMEOUT_ESCALATE=0 MAX_WORKER_TIMEOUT=60 SESSION_SLICE_SECONDS=30
export POLL_INTERVAL=2 QUICK_RETRY_DELAY=1 FAILED_RETRY_AFTER=0
export ROUND_WINDOW_MINUTES=3 STOP_SPAWN_BEFORE_END=10
export KEEP_RUNNING=0 MAX_LOG_MB=0
export FAKE_PI_ROOT="$ROOT"
export PATH="$ROOT/tests/fake_pi:$PATH"

echo "== 起 runner(假 pi,不花 token)=="
python3 runner.py >> "$RUN_DIR/logs/runner.out" 2>&1
RC=$?
echo "runner 退出码 $RC"

echo
echo "== 结果 =="
python3 - "$RUN_DIR" "$PORT" <<'PY'
import json, sys, pathlib, urllib.request
run_dir, port = pathlib.Path(sys.argv[1]), sys.argv[2]
board = json.loads((run_dir / "board.json").read_text(encoding="utf-8"))
qs = board["questions"]
solved = [q for q, e in qs.items() if e["status"] == "solved"]
flags = sum(int(e.get("correct_flag_count") or 0) for e in qs.values())
total = sum(int(e.get("flag_count") or 1) for e in qs.values())
print(f"题目 {len(qs)} 道,通关 {len(solved)} 道,flag {flags}/{total}")

req = urllib.request.Request(f"http://127.0.0.1:{port}/openapi/v1/challenges",
                             headers={"BENCHMARK_TOKEN": "mock-benchmark-token"})
# 必须显式绕代理:macOS 系统代理开着时 urllib 会自动走它,请求回环地址会超时/502(实测)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
plat = json.loads(opener.open(req, timeout=5).read())
p_done = sum(1 for c in plat if c["is_completed"])
p_flags = sum(c["correct_flag_count"] for c in plat)
p_avail = sum(1 for c in plat if c["container_status"] == "available")
print(f"平台侧:通关 {p_done}/{len(plat)} 道,flag {p_flags},仍开着的容器 {p_avail}")

log = (run_dir / "logs" / "runner.out").read_text(encoding="utf-8", errors="ignore")
subs = (run_dir / "logs" / "submissions.log")
ok_subs = sum(1 for l in subs.read_text(encoding="utf-8").splitlines()
              if l.strip() and json.loads(l).get("ok")) if subs.exists() else 0
print(f"提交记录 {ok_subs} 条(全部 ok)")
print(f"容器就绪日志 {log.count('容器就绪')} 次;轮换 {log.count('轮换:')} 次;"
      f"容器调度失败 {log.count('容器调度失败')} 次")

bad = []
if len(solved) != len(qs):
    bad.append(f"还有 {len(qs) - len(solved)} 道没通关")
if p_flags != total:
    bad.append(f"平台 flag 数 {p_flags} != {total}")
if p_avail != 0:
    bad.append(f"收工后还有 {p_avail} 个容器没关")
if "容器调度失败" in log:
    bad.append("出现容器调度失败(可能超了 3 个上限)")
if "VPN 预检未通过" in log:
    bad.append("VPN 预检失败")
print()
if bad:
    print("✘ 端到端失败:")
    for b in bad:
        print("   -", b)
    sys.exit(1)
print("✔ 端到端通过")
PY
E2E=$?
echo
echo "日志:$RUN_DIR/logs/runner.out"
exit $E2E
