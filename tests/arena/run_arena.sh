#!/bin/bash
# ============================================================================
#  NSSCTF Agent Arena 压测(隔离运行,不影响 8799 控制台与比赛配置)
#
#    ./tests/arena/run_arena.sh             # 后台开跑(单题串行,连续领题)
#    ./tests/arena/run_arena.sh -f          # 前台跑,直接看日志
#    ./tests/arena/run_arena.sh status|stop
#
#  与正式比赛的三点差异:
#    1. 接口适配器 = arena_api(CONTEST_ADAPTER=arena),用 Agent Token 领题/提交
#    2. 模型 = DeepSeek 官方(api.deepseek.com,deepseek-flash)
#    3. 断网模式(NETWORK_MODE=local_only):agent 只能访问本机工具/知识库与题目目标,
#       其余出网一律黑洞(搜索资料会被拒)
#  数据目录独立在 tests/arena/run/,不碰默认项目。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
ROOT=$(pwd)
RUN_DIR="$ROOT/tests/arena/run"
PIDF="$RUN_DIR/logs/runner.pid"

say(){ printf '%s\n' "$*"; }
ok(){ printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
bad(){ printf '  \033[31m✘\033[0m %s\n' "$*"; }

case "${1:-start}" in
  stop)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
      kill -TERM "$(cat "$PIDF")"; sleep 3; ok "已停止(agent 一并收掉)"
    else warn "未在运行"; fi
    exit 0;;
  status)
    say "== 靶场压测状态 =="
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then ok "runner 运行中 pid $(cat "$PIDF")"; else warn "runner 未运行"; fi
    say ""; say "-- 调度日志 --"; tail -8 "$RUN_DIR/logs/runner.out" 2>/dev/null
    [ -s "$RUN_DIR/logs/submissions.log" ] && { say ""; say "-- 提交记录 --"; tail -5 "$RUN_DIR/logs/submissions.log"; }
    exit 0;;
esac

FOREGROUND=0; [ "${1:-}" = "-f" ] && FOREGROUND=1

say "=============================================================="
say " NSSCTF Agent Arena 压测   $(date '+%F %T')"
say "=============================================================="

# token
TOK=$(cat "$ROOT/.nssctf_token" 2>/dev/null || echo "")
[ -n "$TOK" ] || { bad "缺少 .nssctf_token(去靶场创建 Agent 拿 token)"; exit 1; }
ok "Agent token 已加载(长度 ${#TOK})"

# 接口连通性
CODE=$(curl -sS -m 20 --noproxy '*' -o /tmp/arena_chk.json -w '%{http_code}' \
  -H "Authorization: Bearer $TOK" "https://www.nssctf.cn/api/skill/agent/arena/current/" 2>/dev/null)
if [ "$CODE" = "200" ] && grep -q '"code": *200' /tmp/arena_chk.json 2>/dev/null; then
  ATT=$(python3 -c "import json;d=json.load(open('/tmp/arena_chk.json'));a=d['data'].get('attempt');print(f\"进行中: {a['problem']['type_label']} #{a['id']} 剩余 {a['remaining_seconds']}s\" if a else '当前无进行中的题(开跑后会领新题)')")
  ok "靶场接口正常 · $ATT"
else
  bad "靶场接口异常(HTTP $CODE)"; exit 1
fi

# 模型连通性
ok "模型:DeepSeek 官方 / deepseek-flash(provider ds)"

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/work"
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  warn "已在运行,先 ./tests/arena/run_arena.sh stop"; exit 0
fi

export ROTOM_RUN_DIR="$RUN_DIR"
export CONTEST_ADAPTER=arena
export NSSCTF_AGENT_TOKEN="$TOK"
export PI_PROVIDER=ds PI_MODEL=deepseek-flash
export NETWORK_MODE=local_only
export START_WORKERS=2 WORKERS_PER_QUESTION=2 MAX_ATTEMPTS=0 KEEP_RUNNING=1 POLL_INTERVAL=3
export ROUND_WINDOW_MINUTES="${ROUND_WINDOW_MINUTES:-60}"   # 本场时长(分钟),到点自动收工
set -a; . ./.env 2>/dev/null; set +a      # 只借并发等通用参数;接口/模型以本脚本为准
export ROTOM_RUN_DIR="$RUN_DIR" CONTEST_ADAPTER=arena NETWORK_MODE=local_only
export ROUND_WINDOW_MINUTES="${ROUND_WINDOW_MINUTES:-60}"    # .env 之后重新应用,避免被覆盖

say ""; say "断网模式:agent 仅可访问本机工具/知识库 + 题目目标(其余出网黑洞)"
if [ "$FOREGROUND" = "1" ]; then
  exec python3 runner.py
fi
nohup python3 runner.py >> "$RUN_DIR/logs/runner.out" 2>&1 &
disown 2>/dev/null || true
sleep 4
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  ok "已开跑 pid $(cat "$PIDF")"
  say ""
  say "  看日志: tail -f $RUN_DIR/logs/runner.out"
  say "  状态:   ./tests/arena/run_arena.sh status    停止: ./tests/arena/run_arena.sh stop"
  say "  审计:   python3 tools/export_trace.py --run-dir $RUN_DIR"
else
  bad "启动失败,见 $RUN_DIR/logs/runner.out"; exit 1
fi
