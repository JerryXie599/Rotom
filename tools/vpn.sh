#!/bin/bash
# 靶场 VPN 助手(macOS / Linux 通用)。
#
#   ./tools/vpn.sh up       用 .env 里的 VPN_OVPN_FILE 连上靶场 VPN(需要 sudo)
#   ./tools/vpn.sh down     断开
#   ./tools/vpn.sh status   看当前是否连通(等效 ./start.sh vpn)
#
# 平台下发的 .ovpn 填在 .env 的 VPN_OVPN_FILE 里(默认已指向本机那份)。
# 不想用命令行也行:用 Tunnelblick / OpenVPN Connect 打开同一个 .ovpn,再跑 ./start.sh vpn 复验。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)

[ -f .env ] && { set -a; . ./.env; set +a; }

OVPN="${VPN_OVPN_FILE:-}"
PIDFILE="$ROOT/logs/openvpn.pid"
LOGFILE="$ROOT/logs/openvpn.log"
mkdir -p "$ROOT/logs"

say() { printf '%s\n' "$*"; }
ok()  { printf '  \033[32m✔\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✘\033[0m %s\n' "$*"; }

alive() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

case "${1:-status}" in
  up)
    if [ -z "$OVPN" ]; then
      bad "没配置 VPN_OVPN_FILE —— 在 .env 里写上平台下发的 .ovpn 绝对路径"; exit 1
    fi
    [ -f "$OVPN" ] || { bad "找不到 .ovpn 文件:$OVPN"; exit 1; }
    if alive; then ok "openvpn 已在运行(pid $(cat "$PIDFILE"))"; exit 0; fi
    if ! command -v openvpn >/dev/null; then
      bad "本机没装 openvpn。二选一:"
      say "    1) brew install openvpn      然后重跑 ./tools/vpn.sh up"
      say "    2) 用图形客户端打开这个文件,再跑 ./start.sh vpn 复验:"
      say "       $OVPN"
      exit 1
    fi
    say "启动 openvpn(要建 tun 设备,会提示输入密码)…"
    sudo openvpn --config "$OVPN" --daemon --writepid "$PIDFILE" --log "$LOGFILE"
    sleep 6
    if python3 config.py --vpn; then
      ok "VPN 已连通(日志:$LOGFILE)"
    else
      bad "openvpn 起来了但预检没过 —— 看日志:tail -30 $LOGFILE"
      exit 1
    fi
    ;;
  down)
    if alive; then
      sudo kill "$(cat "$PIDFILE")" 2>/dev/null || kill "$(cat "$PIDFILE")" 2>/dev/null
      rm -f "$PIDFILE"
      ok "已断开 VPN"
    else
      # 不是本脚本起的(比如图形客户端):只提示,不擅自杀
      say "本脚本没在跑 openvpn。如果是 Tunnelblick/OpenVPN Connect 连的,请在客户端里断开。"
    fi
    ;;
  status)
    python3 config.py --vpn
    say ""
    say "utun 网卡(连上 VPN 后通常能看到一条 utun):"
    ifconfig 2>/dev/null | grep -E "^utun[0-9]+" | head -5 || true
    if alive; then ok "openvpn 进程在跑(pid $(cat "$PIDFILE"))"; fi
    ;;
  *)
    sed -n '2,12p' "$0"; exit 1 ;;
esac
