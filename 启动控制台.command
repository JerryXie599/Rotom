#!/bin/bash
# 双击即可启动控制台(已启动则直接打开页面)
cd "$(dirname "$0")" || exit 1
mkdir -p logs
PORT_FILE=logs/dashboard.port
if [ -f "$PORT_FILE" ]; then
  PORT=$(cat "$PORT_FILE")
  if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/api/state"; then
    echo "控制台已在运行: http://127.0.0.1:$PORT"
    open "http://127.0.0.1:$PORT"
    read -n 1 -s -r -p "按任意键关闭本窗口(控制台继续在后台运行)…"
    exit 0
  fi
fi
nohup python3 dashboard.py >> logs/dashboard.out 2>&1 &
disown
sleep 3
PORT=$(cat "$PORT_FILE" 2>/dev/null || echo 8799)
echo "控制台已启动: http://127.0.0.1:$PORT"
open "http://127.0.0.1:$PORT"
read -n 1 -s -r -p "按任意键关闭本窗口(控制台继续在后台运行)…"
