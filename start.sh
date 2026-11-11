#!/bin/bash
# ============================================================================
#  Rotom 一键启动
#
#  用法:
#    ./start.sh                    # 自动跑:VPN 预检 → 平台连通 → 起控制台 → 后台开跑
#    ./start.sh vpn                # 只做靶场 VPN 联通预检
#    ./start.sh --project round1   # 在指定项目(独立工作区)里跑
#    ./start.sh --workers 4        # 本次覆盖起始并发
#    ./start.sh --foreground       # 前台运行(能直接看滚动日志,Ctrl+C 停止)
#    ./start.sh --no-dashboard     # 不起网页控制台
#    ./start.sh --check-only       # 只做配置体检,不启动
#    ./start.sh status             # 看当前状态
#    ./start.sh stop               # 停掉 runner(优雅收掉所有 agent,并关闭靶场容器)
#
#  配置都在 .env 里(也可在网页控制台里改)。平台/模型/VPN 的说明见 README.md。
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
SKIP_VPN_CHECK=0
SUPERVISE=1     # 默认开:崩溃自动重启(无人值守更安全);--no-supervise 关闭
ACTION=start

while [ $# -gt 0 ]; do
  case "$1" in
    start|stop|status|vpn) ACTION="$1"; shift ;;
    --practice)   PRACTICE=1; shift ;;
    --project)    PROJECT="${2:-默认项目}"; shift 2 ;;
    --workers)    WORKERS="${2:-}"; shift 2 ;;
    --foreground|-f) FOREGROUND=1; shift ;;
    --no-dashboard) DASHBOARD=0; shift ;;
    --supervise)  SUPERVISE=1; shift ;;
    --no-supervise) SUPERVISE=0; shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --no-vpn)     SKIP_VPN_CHECK=1; shift ;;
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
# ROTOM_RUN_DIR 显式给了就以它为准(托管容器里把数据目录挂到卷上时要用;以前会被这里覆盖掉)
RUN_DIR="${ROTOM_RUN_DIR:-$(run_dir)}"
export ROTOM_RUN_DIR="$RUN_DIR"

# 按注册表收掉遗留 worker(只杀 comm=pi 的进程,避免 pid 复用误杀)。
# 用在两处:stop(runner 已经不在了也要收干净)与 start 前清场。
kill_registered_workers() {
  python3 - "$RUN_DIR" <<'PY' 2>/dev/null || true
import json, os, pathlib, signal, subprocess, sys
run_dir = pathlib.Path(sys.argv[1])
reg = run_dir / "logs" / "workers.json"
if not reg.exists():
    raise SystemExit(0)
killed = 0
try:
    rows = json.loads(reg.read_text())
except Exception:
    rows = []
for w in rows:
    try:
        pid = int(w["pid"])
    except Exception:
        continue
    comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()
    if comm.split("/")[-1] != "pi":
        continue
    try:
        os.killpg(pid, signal.SIGKILL); killed += 1
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL); killed += 1
        except Exception:
            pass
reg.write_text("[]")
if killed:
    print(f"  收掉 {killed} 个遗留 agent 进程")
PY
}

# ---------------------------------------------------------------- vpn 预检
# 靶场 VPN 是整个流程的强制前置:题目入口地址只在 VPN 内可达。
if [ "$ACTION" = vpn ]; then
  python3 config.py --vpn
  exit $?
fi

# ---------------------------------------------------------------- stop / status
if [ "$ACTION" = stop ]; then
  mkdir -p "$RUN_DIR/logs"; touch "$RUN_DIR/logs/stop.flag"   # 明确停机标记:supervisor 看到它才真退出
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
  kill_registered_workers          # runner 已经不在了也可能有接管/遗留的 agent 在偷跑
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
say " Rotom 一键启动   ($(date '+%F %T'))"
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

ADAPTER="${CONTEST_ADAPTER:-tsec}"
PROFILE="${PROFILE:-zhongce}"
# 项目级策略包(在网页控制台创建项目时选的,存在 meta.json)优先于 .env 的全局设置
if [ "$PROJECT" != "默认项目" ] && [ -f "$RUN_DIR/meta.json" ]; then
  META_PROFILE=$(python3 - "$RUN_DIR/meta.json" <<'PY' 2>/dev/null || true
import json, sys
print(((json.load(open(sys.argv[1], encoding="utf-8"))) or {}).get("profile") or "")
PY
)
  if [ -n "${META_PROFILE:-}" ]; then
    PROFILE="$META_PROFILE"
    export PROFILE
  fi
fi
case "$PROFILE" in
  ctf)     ok "策略包:ctf(CTF 比赛:平台托管容器、六方向工具链、无 VPN 预检)" ;;
  zhongce) ok "策略包:zhongce(众测/跑分:容器自管+名额轮换、一题多 flag)" ;;
  pentest) ok "策略包:pentest(正式渗透:目标来自 targets.json、产出 REPORT.md)" ;;
  shezha)  ok "策略包:shezha(涉诈网站侦查:自动批次、出口合规闸门、产出站点档案)" ;;
  *)       warn "策略包:${PROFILE}(未知名字,runner 会退回默认包;可用 ctf/zhongce/pentest/shezha)" ;;
esac
if [ "$PROFILE" = "pentest" ]; then
  warn "平台适配器(${ADAPTER})在 pentest 包下被忽略:任务源是本地目标清单"
else
  case "$ADAPTER" in
    tsec)  ok "平台适配器:tsec(TSec Benchmark · 容器自开自关 + 一题多 flag)" ;;
    arena) ok "平台适配器:arena(NSSCTF 靶场)" ;;
    *)     warn "平台适配器:${ADAPTER}(原 CTF 平台;众测请设 CONTEST_ADAPTER=tsec)" ;;
  esac
fi

if [ "$PROFILE" = "pentest" ]; then
  # 正式渗透:没有平台 token,检查目标清单
  TARGETS_FILE="${TARGETS_FILE:-}"
  if [ -n "$TARGETS_FILE" ]; then
    TARGETS_PATH="$TARGETS_FILE"
  elif [ "$PROJECT" = "默认项目" ]; then
    TARGETS_PATH="$ROOT/targets.json"
  else
    TARGETS_PATH="$RUN_DIR/targets.json"
  fi
  if [ -f "$TARGETS_PATH" ]; then
    N=$(python3 - "$TARGETS_PATH" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ts = d.get("targets") if isinstance(d, dict) else d
print(len([t for t in (ts or []) if isinstance(t, dict) and (t.get("address") or t.get("url"))]))
PY
)
    ok "目标清单:${TARGETS_PATH}(${N:-?} 个目标)"
  else
    bad "缺目标清单:${TARGETS_PATH}"
    say "  ① 复制 targets.example.json 改成 targets.json(每个目标要有 id/name/address)"
    say "  ② 或设 TARGETS_FILE=<绝对路径> 指向别处的清单"
    exit 1
  fi
  [ -n "${VPN_CHECK_URL:-}" ] && ok "VPN 预检地址:${VPN_CHECK_URL}(渗透包默认启用 VPN 预检)" \
    || warn "未设 VPN_CHECK_URL:渗透包跳过 VPN 预检(现场要走 VPN 就填一个只有连 VPN 才通的地址)"
elif [ "$ADAPTER" = "tsec" ]; then
  if [ -z "${BENCHMARK_TOKEN:-}" ]; then
    bad "BENCHMARK_TOKEN 未填写(创建跑分任务时平台下发;网页控制台或 config.py --benchmark-token 都行)"; exit 1
  fi
  ok "BENCHMARK_TOKEN 已配置(长度 ${#BENCHMARK_TOKEN})"
  [ -n "${BENCHMARK_BASE_URL:-}" ] && ok "跑分平台:${BENCHMARK_BASE_URL}" \
    || { bad "BENCHMARK_BASE_URL 未填写(平台下发的跑分 API 地址)"; exit 1; }
else
  if [ -z "${TEAM_TOKEN:-}" ]; then
    bad "TEAM_TOKEN 未填写"; exit 1
  fi
  ok "队伍 token 已配置(长度 ${#TEAM_TOKEN})"
fi

if [ -n "${PI_BASE_URL:-}" ] && [ -n "${PI_MODEL:-}" ]; then
  python3 config.py --no-sync >/dev/null 2>&1   # 仅展示用,失败不阻塞
  ok "模型:${PI_MODEL}  (接口类型 ${PI_API_TYPE:-openai-completions})"
else
  warn "未配置 PI_BASE_URL,将使用 pi 里已有的 provider:${PI_PROVIDER:-rotom}(模型 ${PI_MODEL:-未设置})"
fi

python3 - <<'PY' 2>/dev/null && ok "配置已同步到 pi provider" || warn "provider 同步跳过(不影响启动)"
import sys; sys.path.insert(0, ".")
import config as c
c.sync_provider({**c.DEFAULTS, **c.read_env()}, quiet=True)
PY

say ""
say "[2/5] VPN 预检 + 任务源连通性"
if [ "$PROFILE" = "pentest" ]; then
  say "  (正式渗透:无平台接口;目标清单检查见上一步)"
elif [ "$ADAPTER" = "tsec" ]; then
  if [ "${ROTOM_HOSTED:-0}" = "1" ]; then
    ok "托管模式:跳过 VPN 预检(平台沙箱里题目地址直连,没有靶场 VPN)"
  else
  python3 config.py --vpn || {
    bad "VPN 预检未通过 —— 题目入口地址只在靶场 VPN 内可达"
    say "  ① 先连上靶场 VPN(平台下发的 .ovpn;本机没装 openvpn 见 README「VPN」一节)"
    say "  ② 连上后重跑 ./start.sh vpn 确认拿到 {\"status\":\"ok\"}"
    say "  ③ 也可以先 ./start.sh --no-vpn 起 harness:它会等你连上 VPN 后自动开跑"
    if [ "${SKIP_VPN_CHECK:-0}" != "1" ]; then exit 1; fi
  }
  fi
fi
if [ "$PROFILE" = "pentest" ]; then
  # 正式渗透:任务源是本地清单,不做平台连通性探测(上一步已检查过清单)
  API_OUT="SKIP"
else
API_OUT=$(python3 - <<'PY'
import sys, os; sys.path.insert(0, ".")
if os.environ.get("CONTEST_ADAPTER", "tsec").lower() == "tsec":
    import tsec_api as api
else:
    import contest_api as api
tok = os.environ.get("BENCHMARK_TOKEN") or os.environ.get("TEAM_TOKEN", "")
try:
    qs = api.list_questions(tok, timeout=15)
    done = sum(1 for q in qs if q.get("is_solved"))
    flags = sum(int(q.get("correct_flag_count") or 0) for q in qs)
    tf = sum(int(q.get("flag_count") or 1) for q in qs)
    print(f"OK {len(qs)} {done} {flags} {tf}")
except api.ContestError as e:
    print(f"BIZ {e}")
except Exception as e:
    print(f"NET {str(e)[:120]}")
PY
)
case "$API_OUT" in
  OK*)  ok "平台接口正常:$(printf '%s' "$API_OUT" | awk '{print "共 "$2" 道题,已通关 "$3" 道,flag "$4"/"$5}')" ;;
  BIZ*) warn "接口通了但业务提示:${API_OUT#BIZ }(任务未开始/已结束或 token 无效,runner 会自动轮询等恢复)" ;;
  NET*) bad  "接口连不上:${API_OUT#NET }"; warn "检查网络/代理后重试(平台域名强制直连)" ;;
esac
fi

say ""
say "[2.5/5] 环境依赖"
python3 --version >/dev/null 2>&1 && ok "python3 $(python3 --version 2>&1 | awk '{print $2}')" || bad "缺 python3"
command -v pi >/dev/null && ok "pi CLI $(pi --version 2>/dev/null | head -1)" || bad "缺 pi CLI(模型接入层)"
command -v curl >/dev/null && ok "curl(探目标)" || bad "缺 curl"
command -v nmap >/dev/null && ok "nmap(端口/服务)" || warn "缺 nmap"
command -v dirsearch >/dev/null && ok "dirsearch(目录爆破)" || warn "缺 dirsearch(web 题会吃力)"
command -v sqlmap >/dev/null && ok "sqlmap(注入)" || warn "缺 sqlmap"
command -v nuclei >/dev/null && ok "nuclei(已知 CVE 模板)" || warn "缺 nuclei"
command -v orb >/dev/null && ok "orb(pwn64 虚拟机:ffuf/nikto 等)" || warn "缺 orb(部分工具只能在本机跑)"
command -v openvpn >/dev/null && ok "openvpn(可用 tools/vpn.sh 连靶场)" || warn "缺 openvpn(可用 Tunnelblick/OpenVPN Connect 连 VPN)"
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
# 清场:上一次遗留的 worker 由 runner 启动时接管/清理(ADOPT_WORKERS=1 会接着跑,不打断)
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
  # 只有 ./start.sh stop(留下 stop.flag)才算真收工;其它任何退出(含被外部 SIGTERM 收掉的 rc=0)
  # 都自动重拉 —— 否则一次误杀就让整条链子静默下线,跑分白白空转(实测踩过)
  rm -f "$RUN_DIR/logs/stop.flag"
  nohup bash -c 'while true; do python3 runner.py >> "'"$RUN_DIR"'/logs/runner.out" 2>&1; rc=$?; if [ -f "'"$RUN_DIR"'/logs/stop.flag" ]; then exit 0; fi; echo "[supervisor] runner 退出(rc=$rc),5 秒后重启 $(date +%H:%M:%S)" >> "'"$RUN_DIR"'/logs/runner.out"; sleep 5; done' > /dev/null 2>&1 &
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
if [ "$PROFILE" = "pentest" ]; then
  say " 看记录:      tail -f $RUN_DIR/logs/findings.jsonl"
  say " 出报告:      python3 tools/pentest_report.py --run-dir $RUN_DIR"
else
  say " 看提交记录:  tail -f $RUN_DIR/logs/submissions.log"
fi
say " 查看状态:    ./start.sh status"
say " 停止:        ./start.sh stop   (会一并收掉所有 agent)"
say ""
if [ "$PROFILE" = "zhongce" ]; then
  say " 提示:平台同时只允许 3 道题的容器开着,harness 会自动轮换;"
  say "      没有进度条也不代表卡住,看 runner.out 的「容器就绪 / 轮换」日志即可。"
elif [ "$PROFILE" = "pentest" ]; then
  say " 提示:正式渗透不抢分 —— agent 会把每个漏洞记成 finding,测完用 --done 收尾;"
  say "      收工后跑一遍 pentest_report.py 就是交给客户的报告。"
fi
say "=============================================================="
