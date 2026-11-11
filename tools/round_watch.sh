#!/bin/bash
# 轮次看护(宿主机侧,**不依赖 ZCode/任何应用开着**;2026-10-25 第四轮教训的补丁)
#
#   bash tools/round_watch.sh <轮次目录> [容器名=rotom-round] [间隔秒=300]
#
# 盯四类异常,识别到就弹 macOS 通知 + 追加写 <轮次目录>/审查日志.md:
#   ① 容器消失(最高优先级)
#   ② runner 心跳停(runner.out 超过 15 分钟没写,且容器还在跑)
#   ③ 平台接口连续失败(runner.out 里 runner 新打的"平台接口已连续失败")
#   ④ 调度停滞(runner.out 里的"调度停滞",新镜像才有这行)
# 另:每 30 分钟写一行常规状态(进度/容器/心跳),给无人值守留痕;run_stop.json 出现 → 通知并退出。
#
# 防双开:logs/watchdog.pid;停止:kill "$(cat <轮次目录>/logs/watchdog.pid)"
set -uo pipefail
DIR="${1:-}"
NAME="${2:-rotom-round}"
IV="${3:-300}"
[ -n "$DIR" ] && [ -d "$DIR" ] || { echo "用法: round_watch.sh <轮次目录> [容器名] [间隔秒]"; exit 2; }
mkdir -p "$DIR/logs"
PIDF="$DIR/logs/watchdog.pid"
[ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null && { echo "看护已在跑(pid $(cat "$PIDF"))"; exit 0; }
echo $$ > "$PIDF"
LOG="$DIR/logs/watchdog.out"
exec >> "$LOG" 2>&1

notify() {  # 弹通知 + 记日志(通知失败不影响)
  echo "[$(date '+%F %T')] $1"
  osascript -e "display notification \"${2:-$1}\" with title \"Rotom 轮次看护(${NAME})\"" >/dev/null 2>&1 || true
}
mark() { echo "$1" >> "$DIR/审查日志.md"; }

LAST_STAT=0; LAST_HB_ALIVE=1; LAST_FAIL_N=0; LAST_STALL_N=0; LAST_CT_ALIVE=1; LOOP=0
# ↑ LAST_HB_ALIVE 初始为 1(未知视为正常):初始为 0 会在首个成功检查时误发一条"心跳恢复"(2026-10-25 首战实测)
echo "[$(date '+%F %T')] 看护启动(pid $$,目录 $DIR,容器 $NAME,间隔 ${IV}s)"
while true; do
  LOOP=$((LOOP+1))
  # ⑤ 轮次结束 → 通知并退出
  if [ -s "$DIR/logs/run_stop.json" ]; then
    notify "轮次已结束(run_stop.json 已落盘),看护退出" "轮次结束,看护退出"
    mark "- $(date '+%H:%M') 看护:轮次结束(logs/run_stop.json 已落盘),退出。"
    break
  fi
  # ① 容器消失
  if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"; then
    if [ "$LAST_CT_ALIVE" = "1" ]; then
      notify "⚠ 容器 $NAME 不见了(被删/退出被清理)—— 轮次已中断,请人工处理" "容器消失!"
      mark "- $(date '+%H:%M') ⚠ 看护:容器 $NAME 消失(docker ps -a 里没有),轮次中断。"
      LAST_CT_ALIVE=0
    fi
  else
    LAST_CT_ALIVE=1
  fi
  OUT="$DIR/logs/runner.out"
  if [ -f "$OUT" ]; then
    # ② 心跳停(runner.out 15 分钟没写)
    AGE=$(( $(date +%s) - $(stat -f %m "$OUT" 2>/dev/null || echo 0) ))
    RUNNING=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -cx "$NAME" || true)
    if [ "$AGE" -gt 900 ] && [ "$RUNNING" -ge 1 ]; then
      if [ "$LAST_HB_ALIVE" = "1" ]; then
        notify "⚠ runner 心跳停了 ${AGE}s(runner.out 没再写,容器还在跑)—— 可能卡死" "runner 心跳停!"
        mark "- $(date '+%H:%M') ⚠ 看护:runner.out 已 ${AGE}s 没写入而容器还在跑(心跳停)。"
        LAST_HB_ALIVE=0
      fi
    else
      [ "$LAST_HB_ALIVE" = "0" ] && { notify "runner 心跳恢复" "心跳恢复"; mark "- $(date '+%H:%M') 看护:runner 心跳恢复。"; }
      LAST_HB_ALIVE=1
    fi
    TAIL=$(tail -n 300 "$OUT" 2>/dev/null)
    # ③ 平台接口连续失败(runner 每次都会打新的一行,计数变化即新事件)
    N=$(echo "$TAIL" | grep -c "平台接口已连续失败" || true)
    if [ "$N" -gt "$LAST_FAIL_N" ]; then
      LASTLINE=$(echo "$TAIL" | grep "平台接口已连续失败" | tail -1 | cut -c1-140)
      notify "⚠ $LASTLINE" "平台接口连续失败"
      mark "- $(date '+%H:%M') ⚠ 看护转发:$LASTLINE"
    fi
    LAST_FAIL_N=$N
    # ④ 调度停滞
    S=$(echo "$TAIL" | grep -c "调度停滞" || true)
    if [ "$S" -gt "$LAST_STALL_N" ]; then
      LASTLINE=$(echo "$TAIL" | grep "调度停滞" | tail -1 | cut -c1-140)
      notify "⚠ $LASTLINE" "调度停滞"
      mark "- $(date '+%H:%M') ⚠ 看护转发:$LASTLINE"
    fi
    LAST_STALL_N=$S
  fi
  # 常规留痕:每 ~30 分钟一行
  if [ $((LOOP % (1800 / IV <= 0 ? 1 : 1800 / IV))) = "0" ]; then
    SOLVED=$(python3 -c "
import json,sys
try:
    b=json.load(open('$DIR/board.json'))
    print(sum(1 for e in b['questions'].values() if e['status']=='solved'))
except Exception: print('?')" 2>/dev/null)
    AGE2=$(( $(date +%s) - $(stat -f %m "$OUT" 2>/dev/null || echo 0) ))
    mark "- $(date '+%H:%M') 看护例行:已解 ${SOLVED:-?} | 容器$([ "$LAST_CT_ALIVE" = "1" ] && echo 在 || echo 无) | 心跳 ${AGE2}s 前"
  fi
  sleep "$IV"
done
rm -f "$PIDF"