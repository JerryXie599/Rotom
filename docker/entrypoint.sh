#!/bin/bash
# 托管容器入口(平台 Docker 起容器后自动执行 —— 不需要任何人敲命令)
#
# 做四件事:
#   ① 把运行时环境变量整理成 .env(含把模型 API 改写成平台的 .tsecbench.gw 网关);
#   ② 配置体检(start.sh --check-only --no-vpn;托管环境没有靶场 VPN,题目地址直连);
#   ③ **前台**跑 runner,崩溃自动重启(容器里没人能执行 ./start.sh stop,所以这里自带守护);
#   ④ 判定"任务真的结束"(run_stop.json 里的四种终止原因)→ 退出容器,不再空转重启。
set -uo pipefail

cd /app || exit 1
export ROTOM_HOSTED=1
export ROTOM_RUN_DIR="${ROTOM_RUN_DIR:-/app}"
# pi 启动时会做联网操作(模型目录刷新/遥测)。托管环境没有公网 —— 关掉,
# 免得每个 worker 启动都卡在超时上(pi 自带开关:PI_OFFLINE / PI_TELEMETRY)。
export PI_OFFLINE=1
export PI_TELEMETRY=0
mkdir -p logs work

echo "=============================================================="
echo " Rotom 托管启动  $(date '+%F %T')"
echo "=============================================================="

# ① 运行时配置(密钥只在容器内,镜像里没有)
python3 tools/platform_env.py --write || { echo "[entrypoint] 运行时配置生成失败"; exit 1; }

# ①.4 靶场 VPN 自连(按需,本地 Docker 测试用):VPN_AUTOCONNECT=1 + VPN_OVPN_FILE=<平台下发的 .ovpn>
#     平台托管环境不需要(沙箱里题目地址直连),但"完全脱离宿主机运行"时容器得自己连。
if [ "${VPN_AUTOCONNECT:-0}" = "1" ] && [ -n "${VPN_OVPN_FILE:-}" ] && [ -f "$VPN_OVPN_FILE" ]; then
  if command -v openvpn >/dev/null 2>&1; then
    openvpn --config "$VPN_OVPN_FILE" --daemon --log /tmp/openvpn.log --writepid /tmp/openvpn.pid
    echo "[entrypoint] 已发起 VPN 连接(${VPN_OVPN_FILE##*/}),等待隧道就绪…"
    for i in $(seq 1 30); do
      if curl -s -m 5 --noproxy '*' "${VPN_CHECK_URL:-http://10.0.100.58}" 2>/dev/null | grep -q ok; then
        echo "[entrypoint] VPN 就绪(第 ${i} 次探测)"
        break
      fi
      sleep 3
    done
  else
    echo "[entrypoint] ⚠ VPN_AUTOCONNECT=1 但镜像里没有 openvpn"
  fi
fi

# ①.5 出网锁(按需):EGRESS_LOCKDOWN=1 → 只放行"靶场/局域网 + 模型网关 + 平台域名 + DNS",
#      其余公网出口在内核层直接丢 —— 复刻平台托管环境"程序不能访问任何公网地址"的硬约束。
#      (需要 --cap-add=NET_ADMIN;没装 iptables 时只告警不阻塞)
if [ "${EGRESS_LOCKDOWN:-0}" = "1" ]; then
  if command -v iptables >/dev/null 2>&1; then
    PLAT_HOST=$(python3 - <<'PY' 2>/dev/null || true
import os, urllib.parse
print(urllib.parse.urlparse(os.environ.get("BENCHMARK_BASE_URL", "")).hostname or "")
PY
)
    iptables -F OUTPUT 2>/dev/null || true
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
    for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
      iptables -A OUTPUT -d "$cidr" -j ACCEPT
    done
    # VPN 服务器本身是公网地址 —— 不放行的话隧道也出不去(从 .ovpn 里读 remote)
    if [ -n "${VPN_OVPN_FILE:-}" ] && [ -f "${VPN_OVPN_FILE:-}" ]; then
      for ip in $(awk '/^remote[[:space:]]/ {print $2}' "$VPN_OVPN_FILE" | sort -u); do
        iptables -A OUTPUT -d "$ip" -j ACCEPT 2>/dev/null || true
        echo "[entrypoint] 出网锁:放行 VPN 服务器 $ip"
      done
    fi
    # 放行"平台"与"模型网关"两个域名(托管环境里模型网关是公网域名,如 api.deepseek.com.tsecbench.gw)
    PI_HOST=$(python3 - <<'PY' 2>/dev/null || true
import os, urllib.parse
print(urllib.parse.urlparse(os.environ.get("PI_BASE_URL", "")).hostname or "")
PY
)
    for host in "$PLAT_HOST" "$PI_HOST"; do
      [ -n "$host" ] || continue
      for ip in $(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u); do
        iptables -A OUTPUT -d "$ip" -j ACCEPT
      done
      echo "[entrypoint] 出网锁:已放行域名 $host 的解析地址"
    done
    iptables -A OUTPUT -j REJECT
    echo "[entrypoint] 出网锁已启用(EGRESS_LOCKDOWN=1):仅靶场/局域网 + 平台 + DNS 可出"
    # 2026-10-25 第四轮教训:启动时解析的 IP 被钉死 —— 平台域名有多个 A 记录,地址一变就是硬故障。
    # 后台每 5 分钟重解析,新 IP 用 -I 插到 REJECT **前面**(追加在后面不会生效)。
    (
      while true; do
        sleep 300
        for host in "$PLAT_HOST" "$PI_HOST"; do
          [ -n "$host" ] || continue
          for ip in $(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u); do
            if ! iptables -C OUTPUT -d "$ip" -j ACCEPT 2>/dev/null; then
              iptables -I OUTPUT 1 -d "$ip" -j ACCEPT 2>/dev/null || true
              echo "[entrypoint] 出网锁:域名 $host 解析到新地址 $ip,已放行(周期刷新)"
            fi
          done
        done
      done
    ) &
  else
    echo "[entrypoint] ⚠ EGRESS_LOCKDOWN=1 但镜像里没有 iptables(如果是老镜像,重建一次即可)"
  fi
fi

# ①.6 **把生成的 .env 导出到进程环境**(关键):平台只注入 token/base_url,
#      其余配置(PROFILE/CONTEST_ADAPTER/并发/超时…)必须走 .env —— 不导出的话 worker 里的
#      submit_flag.py 会回退到 CTF 适配器(旧域名,必然提交失败),并发/超时也会落回默认值。
set -a; . /app/.env; set +a
echo "[entrypoint] 已导出 .env 到进程环境(PROFILE=${PROFILE:-?} · ADAPTER=${CONTEST_ADAPTER:-?} · 并发=${START_WORKERS:-?})"

# ② 体检(不启动 runner)。缺 token 之类会在这一步暴露出来,但**不要因此退出**
#    —— 平台可能稍后才注入 BENCHMARK_TOKEN,让它进循环里重试更稳。
./start.sh --check-only --no-vpn || echo "[entrypoint] 体检有告警(继续启动,runner 会自己重试)"

# ③ + ④ 前台守护循环
FINAL_REASONS="goal_done platform_dead token_bad"
tries=0
fast_crashes=0                       # 连续"秒退"次数:防热循环把容器刷爆日志
last_start=0
while true; do
  tries=$((tries + 1))
  echo "[entrypoint] 启动 runner(第 ${tries} 次) $(date '+%H:%M:%S')"
  now=$(date +%s); [ $((now - last_start)) -lt 10 ] && fast_crashes=$((fast_crashes + 1)) || fast_crashes=0
  last_start=$now
  if [ "$fast_crashes" -ge 5 ]; then
    echo "[entrypoint] runner 连续 ${fast_crashes} 次秒退 —— 配置或环境有问题,停止空转(见上面日志)"
    exit 1
  fi
  # ── 调度日志同时落盘到挂载目录 ────────────────────────────────────────────────
  # 为什么:容器模式原先只有 docker stdout 一份日志 —— **容器一重建(每次重启都是 rm -f + run)就全丢**,
  # 而 `tools/status_now.py` / `tools/postmortem.py` / `start.sh status` 都读 `logs/runner.out`
  # (本地模式 start.sh 会写,容器模式从来没人写)。2026-10-08 实测代价:我为部署修复重启了 3 次,
  # 把前 3 小时(含 bug 现场)的调度日志自己毁掉了,复盘时无法回溯。
  # 用 tee:docker logs 里照旧实时可见,挂载目录同时留一份可审计的副本。
  # rc 必须取 PIPESTATUS[0] —— tee 自己永远返回 0,直接取会把 runner 的真实退出码吃掉。
  RUN="${ROTOM_RUN_DIR:-/app}"
  mkdir -p "$RUN/logs"
  python3 runner.py 2>&1 | tee -a "$RUN/logs/runner.out"
  rc=${PIPESTATUS[0]}

  if [ -f "$RUN/logs/stop.flag" ]; then
    echo "[entrypoint] 收到 $RUN/logs/stop.flag(明确停机)→ 退出容器"
    exit 0
  fi
  if [ -f "$RUN/logs/run_stop.json" ]; then
    reason=$(python3 - "$RUN/logs/run_stop.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("reason", ""))
except Exception:
    print("")
PY
)
    for r in $FINAL_REASONS; do
      if [ "$reason" = "$r" ]; then
        echo "[entrypoint] 任务已结束(reason=${reason})→ 退出容器,不再重启"
        exit 0
      fi
    done
    echo "[entrypoint] 上次退出原因:${reason:-未知}(非终态)→ 继续守护"
  fi

  echo "[entrypoint] runner 退出(rc=${rc}),5 秒后重启 $(date '+%H:%M:%S')"
  sleep 5
done