#!/bin/bash
# 双击:一键启动整个 Agent(VPN 预检 → 平台连通 → 控制台 → 自动开跑,崩溃自动重启)
# 之后在打开的网页里填平台凭证 / 模型信息,点「开始跑」即可。
cd "$(dirname "$0")" || exit 1
echo "=============================================="
echo " Rotom_beta · 腾讯众测(TSec Benchmark)· 一键启动"
echo "=============================================="
echo " 提醒:题目入口在靶场 VPN 内,请先连上 VPN(./tools/vpn.sh up 或图形客户端)。"
echo
./start.sh
echo
echo "----------------------------------------------"
echo "控制台已就绪。之后的操作都在网页上完成:"
echo "  1) 控制台 → 填 BENCHMARK_BASE_URL / BENCHMARK_TOKEN(平台创建跑分任务时下发)"
echo "  2) 点「测试 VPN」「测试平台接口」确认连通"
echo "  3) 总览 → 「开始跑」"
echo
echo "本窗口可以直接关掉(Agent 在后台跑)。"
read -n 1 -s -r -p "按任意键关闭本窗口…"
