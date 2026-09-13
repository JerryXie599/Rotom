#!/bin/bash
# 双击即可关闭控制台(runner/agent 请先在页面里点「停止」)
cd "$(dirname "$0")" || exit 1
pkill -f "python3 dashboard.py" && echo "控制台已关闭" || echo "控制台本来就没在运行"
sleep 1
