#!/bin/bash
# VPN 连接诊断:用 openvpn CLI 带 --verb 6 把握手过程完整打出来。
#
# 关键技巧:用 `--dev null` —— tun 设备是在握手+PUSH_REPLY 之后才创建的,
# 所以这个模式能把握手全过程跑完并打印,**不需要 sudo**(纯诊断,不改路由/不建隧道)。
#
#   bash tools/vpn_debug.sh                        # 默认用 .env 里的 VPN_OVPN_FILE
#   bash tools/vpn_debug.sh /path/to/xxx.ovpn      # 指定配置
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && { set -a; . ./.env; set +a; }

OVPN="${1:-${VPN_OVPN_FILE:-}}"
[ -n "$OVPN" ] && [ -f "$OVPN" ] || { echo "用法: bash tools/vpn_debug.sh <xxx.ovpn>"; exit 1; }

OPENVPN=$(command -v openvpn || echo <工作目录>)
[ -x "$OPENVPN" ] || { echo "没装 openvpn CLI:brew install openvpn"; exit 1; }

LOG="/tmp/vpn_debug_$(date +%H%M%S).log"
echo "配置:$OVPN"
echo "日志:$LOG"
echo "跑 45 秒(纯握手诊断,不建隧道、不改路由)…"
echo "--------------------------------------------------------------"

# timeout 替身(macOS 没有 timeout)
( "$OPENVPN" --config "$OVPN" --dev null --verb 6 \
      --route-noexec --ifconfig-noexec --pull-filter ignore "route-" \
      --log "$LOG" & echo $! > /tmp/vpn_debug.pid ) 2>/dev/null
sleep 45
kill "$(cat /tmp/vpn_debug.pid 2>/dev/null)" 2>/dev/null

echo "=== 关键结论 ==="
grep -nE "TLS Error|VERIFY|SSL Handshake|Control Channel|Peer Connection|PUSH_REPLY|Initialization Sequence|AUTH_FAILED|Cannot|error|failed|SIGUSR1" "$LOG" | head -25
echo
echo "=== 握手/控制通道细节(最后 40 行)==="
tail -40 "$LOG"
echo
echo "完整日志:$LOG"