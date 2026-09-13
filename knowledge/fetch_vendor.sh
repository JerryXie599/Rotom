#!/bin/bash
# 拉取离线资料到 knowledge/vendor/(仓库不含,约 170MB)
set -u
cd "$(dirname "$0")"
mkdir -p vendor && cd vendor
clone() { [ -d "$2" ] && { echo "$2 已存在,跳过"; return; }; echo "--- 克隆 $2 ---"; git clone --depth 1 -q "$1" "$2" && rm -rf "$2/.git"; }
clone https://github.com/swisskyrepo/PayloadsAllTheThings.git PayloadsAllTheThings
clone https://github.com/ctf-wiki/ctf-wiki.git ctf-wiki
clone https://github.com/RsaCtfTool/RsaCtfTool.git RsaCtfTool
cd .. && python3 ../tools/kb.py reindex 2>/dev/null || true
du -sh vendor
echo "完成。检索示例: python3 tools/kb.py search \"sql 注入\""
