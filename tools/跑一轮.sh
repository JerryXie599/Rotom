#!/bin/bash
# 跑一轮(本地整轮容器模式)—— 众测/TSec 用
#
# 用法:把下面两行填好(或从命令行传),然后 bash 跑一轮.sh
#   bash 跑一轮.sh <BENCHMARK_TOKEN> [ovpn 路径] [容器名]
#
# VPN 两种模式(默认走宿主机 VPN):
#   · 宿主机 VPN(默认):你先把 VPN 连上,容器借宿主机的路由直达靶场 ——
#     实测 macOS Docker Desktop 下容器出口与宿主机同一 client_ip,靶场可达。
#   · 容器自连:加 VPN_SELF=1 + 传 ovpn 路径,容器里自己起 openvpn(托管上传走的是这条)。
#
# 它做的事:用镜像 rotom-agent:latest 起容器,带出网锁(只放行靶场/局域网/平台/模型网关/DNS),
# 数据落在本目录(所以你在这里能看到 board.json / logs / work)。
set -euo pipefail

TOKEN="${1:-}"
OVPN="${2:-}"                       # 仅 VPN_SELF=1 时需要
NAME="${3:-rotom-round}"
VPN_SELF="${VPN_SELF:-0}"           # 1 = 容器内自连 openvpn

ROUND_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-rotom-agent:latest}"

if [ -z "$TOKEN" ]; then
  echo "少了 token。用法: bash 跑一轮.sh <BENCHMARK_TOKEN> [ovpn 路径] [容器名]" >&2
  echo "  (VPN 连在宿主机上时不用给 ovpn;要容器自连则 VPN_SELF=1 并给 ovpn 路径)" >&2
  exit 2
fi

VPN_ENV=()
if [ "$VPN_SELF" = "1" ]; then
  [ -n "$OVPN" ] && [ -f "$OVPN" ] || { echo "VPN_SELF=1 需要 ovpn 文件: $OVPN" >&2; exit 2; }
  VPN_ENV=(-e VPN_AUTOCONNECT=1 -e VPN_OVPN_FILE=/ovpn/task.ovpn --mount type=bind,source="$OVPN",target=/ovpn/task.ovpn,readonly)
  echo "VPN 模式:容器自连($OVPN)"
else
  echo "VPN 模式:借宿主机 VPN(请确认宿主机已连上;容器不自己连)"
fi

# 每轮换新目录:这个目录里一旦有上一轮的 board.json,新一轮会带着旧进度进场
if [ -s "$ROUND_DIR/board.json" ]; then
  echo "⚠ $ROUND_DIR/board.json 非空 —— 这轮会带着上一轮进度进场。要干净开跑先备份并删掉它。" >&2
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true

docker run -d --name "$NAME" --platform linux/amd64 \
  --cap-add=CAP_NET_ADMIN \
  -e ROTOM_RUN_DIR=/rotrun -e PROFILE=zhongce \
  -e BENCHMARK_TOKEN="$TOKEN" \
  ${FOCUS_QIDS:+-e FOCUS_QIDS="$FOCUS_QIDS"} \
  ${FOCUS_WORKERS:+-e FOCUS_WORKERS="$FOCUS_WORKERS"} \
  ${HINT_AFTER_ATTEMPTS:+-e HINT_AFTER_ATTEMPTS="$HINT_AFTER_ATTEMPTS"} \
  ${START_WORKERS:+-e START_WORKERS="$START_WORKERS"} \
  ${SESSION_SLICE_SECONDS:+-e SESSION_SLICE_SECONDS="$SESSION_SLICE_SECONDS"} \
  ${GW_PROBE_INTERVAL:+-e GW_PROBE_INTERVAL="$GW_PROBE_INTERVAL"} \
  -e BENCHMARK_BASE_URL=https://tsecbench.zc.tencent.com \
  -e VPN_CHECK_URL=http://10.0.100.58 \
  -e EGRESS_LOCKDOWN=1 \
  -e PI_BASE_URL="${PI_BASE_URL:?请先用 export 设置 PI_BASE_URL(模型网关地址)}" \
  -e PI_API_KEY="${PI_API_KEY:?请先用 export 设置 PI_API_KEY(模型密钥)}" \
  -e PI_MODEL="${PI_MODEL:-deepseek-v4.1-flash}" \
  ${VPN_ENV[@]+"${VPN_ENV[@]}"} \
  --mount type=bind,source="$ROUND_DIR",target=/rotrun \
  "$IMAGE"

echo "已起容器:$NAME"

# 让本机 8799 控制台**看得见这一轮**:① 项目列表读 projects/ 下的目录 → 建软链接;
# ② meta.json 给出项目显示名与策略包;③ logs/EXTERNAL_RUNNER.json 声明"数据目录由外部 runner 在用",
#    控制台据此拒绝在本机再起一个 runner(同 token 双跑会互相抢容器,踩过)。
PROJ_DIR="$(cd "$ROUND_DIR/../projects" 2>/dev/null && pwd || true)"
ROUND_NAME="$(basename "$ROUND_DIR")"
python3 - "$ROUND_DIR" "$NAME" <<'PY'
import json, sys, time
from pathlib import Path
R, cname = Path(sys.argv[1]), sys.argv[2]
(R / "logs").mkdir(parents=True, exist_ok=True)
(R / "meta.json").write_text(json.dumps(
    {"name": R.name, "profile": "zhongce", "created": time.strftime("%F %T"),
     "note": f"本地整轮容器模式({cname});数据实时写在宿主机这个目录"}, ensure_ascii=False, indent=2), encoding="utf-8")
(R / "logs" / "EXTERNAL_RUNNER.json").write_text(json.dumps(
    {"container": cname, "run_dir": "/rotrun", "by": "docker", "ts": time.strftime("%F %T"),
     "note": f"由容器 {cname} 驱动;收工: docker stop {cname}"}, ensure_ascii=False, indent=2), encoding="utf-8")
print("  已写 meta.json + logs/EXTERNAL_RUNNER.json")
PY
if [ -n "${PROJ_DIR:-}" ]; then
  ln -sfn "$ROUND_DIR" "$PROJ_DIR/$ROUND_NAME" && echo "  控制台项目已就位: $ROUND_NAME(在 8799 控制台项目列表里选它)"
fi

# 主机侧看护(2026-10-25 第四轮教训):不依赖任何应用开着,盯 容器消失/心跳停/接口连续失败/调度停滞,
# 异常弹 macOS 通知并写 审查日志.md;轮次结束(run_stop.json)自动退出。手动停:kill "$(cat logs/watchdog.pid)"
WATCH="$ROUND_DIR/../Rotom_beta/tools/round_watch.sh"
if [ -x "$WATCH" ]; then
  nohup bash "$WATCH" "$ROUND_DIR" "$NAME" 300 >/dev/null 2>&1 &
  echo "  看护已启动(pid $!,每 5 分钟巡检,异常弹通知)"
else
  echo "  ⚠ 未找到 $WATCH —— 跳过看护"
fi

echo "  看日志: docker logs -f $NAME"
echo "  看进度: bash \"$ROUND_DIR/看进度.sh\"   或 8799 控制台里选项目: ${ROUND_NAME}"
echo "  收工:   docker stop $NAME"