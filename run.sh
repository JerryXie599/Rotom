#!/bin/bash
# 一键启动(前台):加载 .env 并运行调度主循环
cd "$(dirname "$0")"
set -a
source .env
set +a
exec python3 runner.py "$@"
