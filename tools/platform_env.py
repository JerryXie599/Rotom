#!/usr/bin/env python3
"""托管环境适配:把**运行时环境变量**整理成 harness 要的 `.env`,并按平台规则改写大模型网关地址。

平台规则(托管运行模式,见上传页「接入大模型网关」):
  1. 用的大模型 API 必须在平台的《大模型 API 白名单》里;
  2. 把原域名加上 `.tsecbench.gw` 后缀;
  3. 原 URL 是 https 的,改成 http。
即:https://api.deepseek.com/v1 → http://api.deepseek.com.tsecbench.gw/v1

用法(通常由 docker/entrypoint.sh 调用):
  python3 tools/platform_env.py --write     # 生成/覆盖 .env(密钥不回显)
  python3 tools/platform_env.py --dry-run   # 只打印将要写入什么(全部脱敏)

约定(也是交给平台填的变量清单):
  BENCHMARK_TOKEN / BENCHMARK_BASE_URL   平台自动注入,程序内部照常调用答题 API
  PI_UPSTREAM_BASE_URL                   你原本要用的模型 API(如 https://api.deepseek.com/v1)
                                          → 本脚本按平台规则改写成 .tsecbench.gw 形式(托管模式用这个)
  PI_BASE_URL                            显式给则**原样使用**(本地模式 / 已写好的网关地址用这个)
  PI_API_KEY                             模型 API key(只从环境变量读,不进镜像)
  PI_MODEL / PI_PROVIDER / PI_API_TYPE   模型名 / provider 名(默认 rotom)/ 接口类型
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GW_SUFFIX = ".tsecbench.gw"

# .env 模板(默认值;可用同名环境变量覆盖)。密钥类一律留空,由平台运行时注入。
DEFAULTS = {
    "PROFILE": "zhongce",
    "CONTEST_ADAPTER": "tsec",
    "BENCHMARK_BASE_URL": "",
    "BENCHMARK_TOKEN": "",
    "VPN_CHECK_URL": "",
    "VPN_OVPN_FILE": "",
    "PI_PROVIDER": "rotom",
    "PI_BASE_URL": "",
    "PI_API_KEY": "",
    "PI_MODEL": "",
    "PI_API_TYPE": "openai-completions",
    "MAX_ACTIVE_CHALLENGES": "3",
    "QUESTION_SLICE_SECONDS": "1800",
    "ROTATE_COOLDOWN": "60",
    "START_WORKERS": "5",
    "MIN_WORKERS": "1",
    "WORKER_TIMEOUT": "1200",
    "TIMEOUT_ESCALATE": "300",
    "MAX_WORKER_TIMEOUT": "2400",
    "SESSION_SLICE_SECONDS": "1200",   # 单节 pi 会话上限;到片接力(不计失败),0=不切片
    "MAX_ATTEMPTS": "0",
    "QUICK_RETRY_DELAY": "5",
    "FAILED_RETRY_AFTER": "600",
    "POLL_INTERVAL": "5",
    "KB_NUDGE_STEPS": "12",
    "KB_AUTO_SEARCH": "1",
    # 2026-10-27:排名差距全在 hint 扣分(101 flag 与我们相同的第一名 0 次提示、27200 毛利,我们被扣 440)。
    # 自动代取推迟到**两个完整尝试都失败之后**(配合 1200s 短会话接力 ≈2 小时);agent 仍可随时 get_hint.py 自取。
    # HINT_AFTER_ATTEMPTS=0 纯自主;=1 回到"第一次失败即取";HINT_SKIP_DIFFS=easy 简单题永不代取。
    "HINT_AFTER_ATTEMPTS": "2",
    "HINT_EARLY_DIFFS": "",
    "HINT_SKIP_DIFFS": "easy",
    # 2026-10-25 第四轮教训(平台通道连挂 5.5h 无声无息):故障必须周期性可听见
    "API_FAIL_RELOG_S": "600",           # 平台接口连续失败时,至少每这么久再报一次(秒)
    "STALL_WARN_S": "900",               # 调度停滞告警:这么久没有新派工/新解出且并发≤1(秒;0=关)
    "STALL_MIN_PENDING": "10",           # 未解题少于这个数不按停滞论(收尾期本来就慢)
    # 2026-10-26 第六轮教训:网关整体变慢(探针 1.7→10~60s)全程无人量化 → 开局记基线 + 周期复测
    "GW_PROBE_INTERVAL": "1800",         # 模型端点延迟复测间隔(秒);0=关
    "GW_SLOW_FACTOR": "2.0",             # 超基线几倍算"变慢"(且 ≥5s 才告警)
    "ADOPT_WORKERS": "1",
    "ROTATE_WAIT_RUNNING": "1",
    "WORKER_MIN_LIFETIME": "120",
    "ROUND_WINDOW_MINUTES": "0",
    "STOP_SPAWN_BEFORE_END": "300",
    "KEEP_RUNNING": "1",
    "MAX_LOG_MB": "0",
    "QUOTA_BACKOFF": "900",
    "NET_PROXY": "direct",
    "NET_ALLOW": "",
    "FALSE_NEGATIVE_RETRIES": "2",
}

# 托管环境里不该出现的键(本地专用 / 会指向宿主机路径)
FORBID_IN_HOSTED = ("KB_EXTRA",)


def gw_rewrite(url: str) -> str:
    """按平台规则改写模型网关地址:加 `.tsecbench.gw` 后缀 + 强制 http。

    - 已经是 `.tsecbench.gw` → 只把 scheme 改成 http(幂等);
    - https://api.deepseek.com/v1        → http://api.deepseek.com.tsecbench.gw/v1
    - 带端口/无路径的也照办:https://api.kimi.com → http://api.kimi.com.tsecbench.gw
    """
    s = (url or "").strip()
    if not s:
        return ""
    m = re.match(r"^(?P<scheme>https?)://(?P<host>[^/:?#]+)(?P<rest>[:/?#].*)?$", s)
    if not m:
        return s                                   # 认不出来就原样返回,别猜
    host, rest = m.group("host"), (m.group("rest") or "")
    if not host.endswith(GW_SUFFIX):
        host = host + GW_SUFFIX
    return f"http://{host}{rest}"


def from_environ(env: dict | None = None) -> dict:
    """环境变量 → .env 字典(含模型网关改写)。"""
    e = dict(os.environ if env is None else env)
    out = dict(DEFAULTS)
    for k in list(out):
        if e.get(k):
            out[k] = str(e[k]).strip()
    # 模型网关:**只有**给了 PI_UPSTREAM_BASE_URL(原始地址)才按平台规则改写成 .gw 形式;
    # 显式给的 PI_BASE_URL 一律原样使用 —— 本地模式/中转网关走的都是原地址,不能瞎改。
    if e.get("PI_UPSTREAM_BASE_URL"):
        out["PI_BASE_URL"] = gw_rewrite(e["PI_UPSTREAM_BASE_URL"])
    out["ROTOM_HOSTED"] = "1"
    out["SKIP_VPN_CHECK"] = "1"        # 托管容器内没有靶场 VPN(直连题目地址)
    for k in FORBID_IN_HOSTED:
        out.pop(k, None)               # 跨轮战果目录绝不留(平台明令:不得内置历史答题记忆)
    return out


def mask(v: str) -> str:
    s = str(v or "")
    if not s:
        return "(空)"
    return s[:6] + "…" + s[-4:] if len(s) > 12 else "***"


def render(d: dict) -> str:
    lines = ["# ============================================================",
             "#  本文件由 tools/platform_env.py 在容器启动时生成(托管模式)",
             "#  来源:运行时环境变量;密钥只存在于容器内,镜像里没有",
             "# ============================================================", ""]
    for k, v in d.items():
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入 .env(覆盖)")
    ap.add_argument("--dry-run", action="store_true", help="只显示(脱敏)")
    ap.add_argument("--path", default=str(ROOT / ".env"))
    args = ap.parse_args()

    cfg = from_environ()
    target = Path(args.path)
    print("== 托管环境配置(tools/platform_env.py) ==")
    for k in ("PROFILE", "CONTEST_ADAPTER", "BENCHMARK_BASE_URL", "BENCHMARK_TOKEN",
              "PI_PROVIDER", "PI_BASE_URL", "PI_MODEL", "PI_API_TYPE", "PI_API_KEY",
              "MAX_ACTIVE_CHALLENGES", "START_WORKERS", "NET_PROXY", "ROTOM_HOSTED"):
        v = cfg.get(k, "")
        shown = mask(v) if "KEY" in k or "TOKEN" in k else (v or "(空)")
        print(f"  {k:24} = {shown}")
    miss = [k for k in ("BENCHMARK_TOKEN", "BENCHMARK_BASE_URL", "PI_MODEL", "PI_API_KEY")
            if not cfg.get(k)]
    if miss:
        print(f"  ⚠ 还缺:{', '.join(miss)}(平台自动注入 token/base_url;模型那两项要在上传页的运行环境变量里填)")
    if args.dry_run or not args.write:
        print("(dry-run:没有写文件。加 --write 才会写)")
        return 0
    target.write_text(render(cfg), encoding="utf-8")
    print(f"已写入 {target}")
    # 顺手把模型 provider 同步进 pi 的 models.json(pi CLI 只认自己的配置)
    try:
        import config as cfgmod
        ok = cfgmod.sync_provider({**cfgmod.DEFAULTS, **cfg, **cfgmod.read_env()}, quiet=True)
        print(f"pi provider 同步:{'成功' if ok else '跳过(缺 PI_BASE_URL/PI_MODEL)'}")
    except Exception as ex:                                     # noqa: BLE001
        print(f"pi provider 同步失败(不阻塞启动):{type(ex).__name__}: {ex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())