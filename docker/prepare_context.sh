#!/bin/bash
# 构建上下文准备:把"运行环境拿不到、但又必须离线可用"的资料收进构建上下文。
#
# 为什么需要:托管容器**无外网** —— nuclei 模板、字典这类东西必须在构建期就放进镜像。
# 本脚本只做"拷贝/整理",不做下载(下载都在 Dockerfile 里显式列出,便于审计)。
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

# ① nuclei 模板(本机已有就直接用;没有则留待 Dockerfile 只装引擎,selfcheck 会告警)
if [ -d "$HOME/nuclei-templates" ]; then
  mkdir -p knowledge/vendor/nuclei-templates
  rsync -a --delete "$HOME/nuclei-templates/" knowledge/vendor/nuclei-templates/ 2>/dev/null \
    || cp -R "$HOME/nuclei-templates/." knowledge/vendor/nuclei-templates/
  echo "  nuclei 模板:$(find knowledge/vendor/nuclei-templates -name '*.yaml' | wc -l | tr -d ' ') 个已就位"
else
  mkdir -p knowledge/vendor/nuclei-templates
  echo "  ⚠ 本机没有 ~/nuclei-templates:镜像里 nuclei 将没有模板(selfcheck 会告警)"
fi

# ② 公开资料(vendor)必须存在 —— PayloadsAllTheThings / RsaCtfTool
for d in PayloadsAllTheThings RsaCtfTool; do
  [ -d "knowledge/vendor/$d" ] && echo "  vendor/$d 已就位" || echo "  ⚠ 缺 knowledge/vendor/$d(跑 knowledge/fetch_vendor.sh 补齐)"
done

# ③ 尺寸提示(构建上下文会整个发给 docker daemon)
echo "  构建上下文大小:$(du -sh --exclude=logs --exclude=work --exclude=.git . 2>/dev/null | cut -f1 \
  || du -sh . | cut -f1)"
echo "  资料目录大小:$(du -sh knowledge/vendor | cut -f1)"