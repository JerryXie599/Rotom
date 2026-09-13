#!/bin/bash
# ============================================================================
#  湾区杯 AI 智能体解题赛 —— 一键启动
#
#  用法:
#    ./start.sh                    # 自动跑:检查配置 → 起控制台 → 后台开跑(正式模式)
#    ./start.sh --practice         # 练习模式(平台已解出的题也重跑,赛前调试用)
#    ./start.sh --project round1   # 在指定项目(独立工作区)里跑
#    ./start.sh --workers 8        # 本次覆盖起始并发
#    ./start.sh --foreground       # 前台运行(能直接看滚动日志,Ctrl+C 停止)
#    ./start.sh --no-dashboard     # 不起网页控制台
#    ./start.sh --check-only       # 只做配置体检,不启动
#    ./start.sh status             # 看当前状态
#    ./start.sh stop               # 停掉 runner(优雅收掉所有 agent)
#
#  配置都在 .env 里(也可在网页控制台里改)。模型/接口/token 的说明见 README.md。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")" || exit 1
ROOT=$(pwd)

PROJECT="默认项目"
PRACTICE=0
WORKERS=""
FOREGROUND=0
DASHBOARD=1
CHECK_ONLY=0
ACTION=start

while [ $# -gt 0 ]; do
  case "$1" in
    start|stop|status) ACTION="$1"; shift ;;
    --practice)   PRACTICE=1; shift ;;
    --project)    PROJECT="${2:-默认项目}"; shift 2 ;;
    --workers)    WORKERS="${2:-}"; shift 2 ;;
    --foreground|-f) FOREGROUND=1; shift ;;
    --no-dashboard) DASHBOARD=0; shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $1(用 --help 看用法)"; exit 1 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✘\033[0m %s\n' "$*"; }

run_dir() {
  if [ "$PROJECT" = "默认项目" ]; then echo "$ROOT"; else echo "$ROOT/projects/$PROJECT"; fi
}
RUN_DIR=$(run_dir)

# ---------------------------------------------------------------- stop / status
if [ "$ACTION" = stop ]; then
  pidf="$RUN_DIR/logs/runner.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
    kill -TERM "$(cat "$pidf")" 2>/dev/null
    for _ in $(seq 1 40); do sleep 0.25; kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null || break; done
    ok "runner 已停止(agent 一并收掉)"
  else
    warn "runner 未在运行($PROJECT)"
  fi
  pkill -f "python3 dashboard.py" 2>/dev/null && ok "控制台已关闭" || true
  exit 0
fi

if [ "$ACTION" = status ]; then
  say "== 项目:$PROJECT =="
  say "数据目录:$RUN_DIR"
  pidf="$RUN_DIR/logs/runner.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
    ok "runner 运行中 (pid $(cat "$pidf"))"
  else
    warn "runner 未运行"
  fi
  [ -f "$RUN_DIR/logs/runner.out" ] && { say ""; say "-- 最近调度日志 --"; tail -6 "$RUN_DIR/logs/runner.out"; }
  [ -s "$RUN_DIR/logs/submissions.log" ] && { say ""; say "-- 最近提交 --"; tail -3 "$RUN_DIR/logs/submissions.log"; }
  portf="$ROOT/logs/dashboard.port"
  if [ -f "$portf" ] && curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$(cat "$portf")/api/state"; then
    ok "控制台: http://127.0.0.1:$(cat "$portf")"
  fi
  exit 0
fi

# ---------------------------------------------------------------- 启动流程
say "=============================================================="
say " 湾区杯 AI 智能体解题赛 · 一键启动   ($(date '+%F %T'))"
say "=============================================================="

say ""
say "[1/5] 配置体检"
[ -f .env ] || { bad "缺少 .env(可复制 .env.example 后填写)"; exit 1; }
set -a; . ./.env; set +a
ok "已加载 .env"

if [ -z "${TEAM_TOKEN:-}" ] || [ "${TEAM_TOKEN}" = "<在此填入现场发放的 key / token>" ]; then
  bad "TEAM_TOKEN 未填写(网页控制台或 config.py --token 都行)"; exit 1
fi
ok "队伍 token 已配置(长度 ${#TEAM_TOKEN})"

if [ -n "${PI_BASE_URL:-}" ] && [ -n "${PI_MODEL:-}" ]; then
  python3 config.py --no-sync >/dev/null 2>&1   # 仅展示用,失败不阻塞
  ok "模型:${PI_MODEL}  (接口类型 ${PI_API_TYPE:-openai-completions})"
else
  warn "未配置 PI_BASE_URL/PI_MODEL,将使用 pi 里已有的 provider:${PI_PROVIDER:-wqh}"
fi

python3 - <<'PY' 2>/dev/null && ok "配置已同步到 pi provider" || warn "provider 同步跳过(不影响启动)"
import sys; sys.path.insert(0, ".")
import config as c
c.sync_provider({**c.DEFAULTS, **c.read_env()}, quiet=True)
PY

say ""
say "[2/5] 比赛接口连通性"
API_OUT=$(python3 - <<'PY'
import sys; sys.path.insert(0, ".")
import contest_api, os
try:
    qs = contest_api.list_questions(os.environ.get("TEAM_TOKEN", ""), timeout=15)
    print(f"OK {len(qs)}")
except contest_api.ContestError as e:
    print(f"BIZ {e}")
except Exception as e:
    print(f"NET {str(e)[:120]}")
PY
)
case "$API_OUT" in
  OK*)  ok "接口正常,当前 ${API_OUT#OK } 道题" ;;
  BIZ*) warn "接口通了但业务提示:${API_OUT#BIZ }(比赛未开始/已结束或 token 无效,runner 会自动轮询等恢复)" ;;
  NET*) bad  "接口连不上:${API_OUT#NET }"; warn "检查网络/代理后重试(比赛域名是直连的)" ;;
esac

say ""
say "[3/5] 准备工作区"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/work"
ok "$RUN_DIR"

if [ "$CHECK_ONLY" = "1" ]; then
  say ""
  ok "体检完成(--check-only 不启动)。去掉该参数即可开跑。"
  exit 0
fi

say ""
say "[4/5] 网页控制台"
if [ "$DASHBOARD" = "1" ]; then
  portf="$ROOT/logs/dashboard.port"
  if [ -f "$portf" ] && curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$(cat "$portf")/api/state"; then
    ok "控制台已在运行: http://127.0.0.1:$(cat "$portf")"
  else
    mkdir -p logs
    nohup python3 dashboard.py >> logs/dashboard.out 2>&1 &
    disown 2>/dev/null || true
    sleep 3
    if [ -f "$portf" ]; then ok "控制台已启动: http://127.0.0.1:$(cat "$portf")"; else warn "控制台启动异常,见 logs/dashboard.out"; fi
  fi
else
  warn "已跳过(--no-dashboard)"
fi

say ""
say "[5/5] 启动自动解题"
pidf="$RUN_DIR/logs/runner.pid"
if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
  warn "runner 已在运行(pid $(cat "$pidf")),先 ./start.sh stop 再启动"; exit 0
fi
# 清理上次残留的 worker(它们不在本次进程表里,会偷烧额度)
python3 - <<'PY' 2>/dev/null || true
import os, sys, json, signal
sys.path.insert(0, ".")
os.environ.setdefault("WQH_RUN_DIR", os.environ.get("WQH_RUN_DIR", ""))
import subprocess, pathlib
reg = pathlib.Path(os.environ.get("WQH_RUN_DIR") or ".", "logs", "workers.json")
if reg.exists():
    try:
        for w in json.loads(reg.read_text()):
            pid = int(w["pid"])
            comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            if comm.split("/")[-1] == "pi":
                try: os.killpg(pid, signal.SIGKILL)
                except Exception: pass
    except Exception:
        pass
    reg.write_text("[]")
PY

export WQH_RUN_DIR="$RUN_DIR"
export PI_PROVIDER="${PI_PROVIDER:-wqh}" PI_MODEL="${PI_MODEL:-}"
[ -n "$WORKERS" ] && export START_WORKERS="$WORKERS"
[ "$PRACTICE" = "1" ] && export IGNORE_SOLVED=1

if [ "$FOREGROUND" = "1" ]; then
  ok "前台运行(项目:$PROJECT,模式:$([ "$PRACTICE" = 1 ] && echo 练习 || echo 正式),并发:${START_WORKERS:-默认})"
  say "   Ctrl+C 可随时停止(会优雅收掉所有 agent)"
  say "--------------------------------------------------------------"
  exec python3 runner.py
fi

nohup python3 runner.py >> "$RUN_DIR/logs/runner.out" 2>&1 &
disown 2>/dev/null || true
sleep 4
if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
  ok "runner 已启动(pid $(cat "$pidf"))—— 项目:$PROJECT,模式:$([ "$PRACTICE" = 1 ] && echo 练习 || echo 正式),并发:${START_WORKERS:-默认}"
else
  bad "runner 启动异常,检查 $RUN_DIR/logs/runner.out"; exit 1
fi

say ""
say "=============================================================="
say " 已经在自动跑了,现在不需要再碰电脑。"
say ""
say " 看实时状态:  http://127.0.0.1:$(cat "$ROOT/logs/dashboard.port" 2>/dev/null || echo 8799)"
say " 看滚动日志:  tail -f $RUN_DIR/logs/runner.out"
say " 看提交记录:  tail -f $RUN_DIR/logs/submissions.log"
say " 查看状态:    ./start.sh status"
say " 停止:        ./start.sh stop"
say "=============================================================="
