#!/bin/bash
# 一键构建 + 导出可上传的镜像包
#
#   bash docker/build.sh                 # 构建 linux/amd64 镜像并导出 dist/rotom-agent.tar.gz
#   bash docker/build.sh --no-export     # 只构建,不导出
#
# 产出:dist/rotom-agent.tar.gz(平台上传要求 .tar / .tar.gz / .tgz,上限 3GB)
# 平台是 x86_64 → 这里显式 --platform linux/amd64(在 arm64 Mac 上走 QEMU 模拟,慢但一致)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
IMAGE=${IMAGE:-rotom-agent:latest}
EXPORT=1; [ "${1:-}" = "--no-export" ] && EXPORT=0

echo "== 0/4 准备构建上下文(把可离线用的资料收进来)=="
bash docker/prepare_context.sh

echo "== 1/4 构建镜像(${IMAGE}, linux/amd64)=="
docker build --platform linux/amd64 -f docker/Dockerfile -t "$IMAGE" .

echo "== 2/4 体积检查(平台上限 3GB)=="
docker image inspect -f '{{.Size}}' "$IMAGE" | awk '{printf "  镜像 %.2f GB\n", $1/1024/1024/1024}'

if [ "$EXPORT" = "0" ]; then
  echo "== 3/4 跳过导出(--no-export)=="
  exit 0
fi

echo "== 3/4 导出并压缩 =="
mkdir -p dist
docker save "$IMAGE" | gzip -1 > dist/rotom-agent.tar.gz
ls -lh dist/rotom-agent.tar.gz | awk '{print "  产出:", $9, $5}'
echo "  上传:平台的「制作并上传 Docker 镜像」→ 选这个文件"

echo "== 4/4 冒烟:容器里跑一次审计自检 =="
docker run --rm --platform linux/amd64 --entrypoint bash "$IMAGE" -lc 'cd /app && bash docker/selfcheck.sh | tail -12'
echo
echo "提示:上传前先在本地用真实 token 跑一次「本地模式」验证(python3 runner.py 或 ./start.sh),"
echo "      托管模式的模型地址必须走平台网关(<域名>.tsecbench.gw),见 docs/DOCKER.md。"