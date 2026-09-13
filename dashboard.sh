#!/bin/bash
# 启动本地看板(只读,不影响正在跑的比赛进程): http://127.0.0.1:8788
cd "$(dirname "$0")"
exec python3 dashboard.py "$@"
