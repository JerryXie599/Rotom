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
SUPERVISE=1     # 默认开:崩溃自动重启(无人值守更安全);--no-supervise 关闭
ACTION=start

while [ $# -gt 0 ]; do
  case "$1" in
    start|stop|status) ACTION="$1"; shift ;;
    --practice)   PRACTICE=1; shift ;;
    --project)    PROJECT="${2:-默认项目}"; shift 2 ;;
    --workers)    WORKERS="${2:-}"; shift 2 ;;
    --foreground|-f) FOREGROUND=1; shift ;;
    --no-dashboard) DASHBOARD=0; shift ;;
    --supervise)  SUPERVISE=1; shift ;;
    --no-supervise) SUPERVISE=0; shift ;;
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
  supf="$RUN_DIR/logs/supervisor.pid"
  if [ -f "$supf" ] && kill -0 "$(cat "$supf" 2>/dev/null)" 2>/dev/null; then
    kill -TERM "$(cat "$supf")" 2>/dev/null; rm -f "$supf"; ok "已停 supervisor(不再自动重启)"
  fi
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

# 网络:默认直连(.env 里 NET_PROXY=direct)。本机 launchd 设了全局代理,代理客户端一关,
# 模型/接口请求会瞬间 Connection error;直连模式下把代理变量摘掉,dashboard/runner/pi 一律直连。
if [ "${NET_PROXY:-direct}" = "direct" ]; then
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
  ok "网络:直连(已忽略本机代理设置;要走代理改 .env 的 NET_PROXY=system)"
fi

if [ -z "${TEAM_TOKEN:-}" ] || [ "${TEAM_TOKEN}" = "<在此填入现场发放的 key / token>" ]; then
  bad "TEAM_TOKEN 未填写(网页控制台或 config.py --token 都行)"; exit 1
fi
ok "队伍 token 已配置(长度 ${#TEAM_TOKEN})"

if [ -n "${PI_BASE_URL:-}" ] && [ -n "${PI_MODEL:-}" ]; then
  python3 config.py --no-sync >/dev/null 2>&1   # 仅展示用,失败不阻塞
  ok "模型:${PI_MODEL}  (接口类型 ${PI_API_TYPE:-openai-completions})"
else
  warn "未配置 PI_BASE_URL/PI_MODEL,将使用 pi 里已有的 provider:${PI_PROVIDER:-rotom}"
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
say "[2.5/5] 环境依赖"
python3 --version >/dev/null 2>&1 && ok "python3 $(python3 --version 2>&1 | awk '{print $2}')" || bad "缺 python3"
command -v pi >/dev/null && ok "pi CLI $(pi --version 2>/dev/null | head -1)" || bad "缺 pi CLI(模型接入层)"
command -v tshark >/dev/null && ok "tshark(取证题)" || warn "缺 tshark(取证题会吃力)"
command -v orb >/dev/null && ok "orb(可用 pwn64 虚拟机跑 amd64 二进制)" || warn "缺 orb,本机 arm64 跑不了 ELF"
[ -f "$ROOT/knowledge/README.md" ] && ok "知识库存在($(ls "$ROOT"/knowledge/*.md 2>/dev/null | wc -l | tr -d ' ') 个方向)" || warn "知识库缺失"
python3 tools/kb.py list >/dev/null 2>&1 && ok "知识库检索可用" || warn "知识库索引异常(跑 python3 tools/kb.py reindex)"
FREE=$(df -g "$ROOT" 2>/dev/null | awk 'NR==2{print $4}')
[ -n "$FREE" ] && { [ "$FREE" -gt 5 ] && ok "磁盘剩余 ${FREE}GB" || warn "磁盘仅剩 ${FREE}GB(日志会增长,注意空间)"; }

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
os.environ.setdefault("ROTOM_RUN_DIR", os.environ.get("ROTOM_RUN_DIR", ""))
import subprocess, pathlib
reg = pathlib.Path(os.environ.get("ROTOM_RUN_DIR") or ".", "logs", "workers.json")
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

export ROTOM_RUN_DIR="$RUN_DIR"
export PI_PROVIDER="${PI_PROVIDER:-rotom}" PI_MODEL="${PI_MODEL:-}"
[ -n "$WORKERS" ] && export START_WORKERS="$WORKERS"
[ "$PRACTICE" = "1" ] && export IGNORE_SOLVED=1

if [ "$FOREGROUND" = "1" ]; then
  ok "前台运行(项目:$PROJECT,模式:$([ "$PRACTICE" = 1 ] && echo 练习 || echo 正式),并发:${START_WORKERS:-默认})"
  say "   Ctrl+C 可随时停止(会优雅收掉所有 agent)"
  say "--------------------------------------------------------------"
  exec python3 runner.py
fi

if [ "$SUPERVISE" = "1" ]; then
  # 崩溃自动重启:runner 正常收工(exit 0)才退出循环;异常退出等 5 秒重来
  nohup bash -c 'while true; do python3 runner.py >> "'"$RUN_DIR"'/logs/runner.out" 2>&1; rc=$?; [ $rc -eq 0 ] && exit 0; echo "[supervisor] runner 异常退出(rc=$rc),5 秒后重启 $(date +%H:%M:%S)" >> "'"$RUN_DIR"'/logs/runner.out"; sleep 5; done' > /dev/null 2>&1 &
  echo $! > "$RUN_DIR/logs/supervisor.pid"
  disown 2>/dev/null || true
  ok "已启用崩溃自动重启(supervisor pid $(cat "$RUN_DIR/logs/supervisor.pid"))"
else
  nohup python3 runner.py >> "$RUN_DIR/logs/runner.out" 2>&1 &
  disown 2>/dev/null || true
fi
sleep 4
if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
  ok "runner 已启动(pid $(cat "$pidf"))—— 项目:$PROJECT,模式:$([ "$PRACTICE" = 1 ] && echo 练习 || echo 正式),并发:${START_WORKERS:-默认}"
else
  bad "runner 启动异常,检查 $RUN_DIR/logs/runner.out"; exit 1
fi

DEADLINE_INFO=$(python3 - <<'PY' 2>/dev/null
import json, time, pathlib, os
f = pathlib.Path(os.environ.get("ROTOM_RUN_DIR") or ".", "logs", "run_deadline.json")
if f.exists():
    try:
        dl = float(json.loads(f.read_text()).get("deadline") or 0)
        if dl:
            left = int(dl - time.time())
            print(f"本轮截止 {time.strftime('%H:%M:%S', time.localtime(dl))}(剩余 {left//60} 分 {left%60} 秒);"
                  f"期间坏了会自动重试,不限次数,到点自动收工")
    except Exception:
        pass
PY
)

say ""
say "=============================================================="
say " 已经在自动跑了,现在不需要再碰电脑。"
[ -n "$DEADLINE_INFO" ] && say " $DEADLINE_INFO"
say ""
say " 看实时状态:  http://127.0.0.1:$(cat "$ROOT/logs/dashboard.port" 2>/dev/null || echo 8799)"
say " 看滚动日志:  tail -f $RUN_DIR/logs/runner.out"
say " 看提交记录:  tail -f $RUN_DIR/logs/submissions.log"
say " 查看状态:    ./start.sh status"
say " 停止:        ./start.sh stop"
say "=============================================================="
