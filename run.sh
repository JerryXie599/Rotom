#!/bin/bash
# 一键启动:加载 .env 并运行 dispatcher
cd "$(dirname "$0")"
set -a
source .env
set +a
exec python3 runner.py "$@"
