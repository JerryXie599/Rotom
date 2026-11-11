#!/usr/bin/env python3
"""统一配置入口:改跑分平台凭证、改模型(base URL / key / 模型名 / API 类型),并可一键测通。

用法:
  python3 config.py                                  # 查看当前配置
  python3 config.py --benchmark-url https://xxx --benchmark-token <UUID>   # 跑分平台
  python3 config.py --base-url https://api.xxx/v1 --key sk-xxx \
                    --model deepseek-v4-flash --api-type openai-completions
  python3 config.py --vpn                            # 靶场 VPN 联通预检
  python3 config.py --test                           # 用 pi 实际跑一次,验证模型连通

--api-type 可选(对应 pi 的 provider api 类型):
  openai-completions   OpenAI Chat Completions(最通用,大多数中转/国产模型选这个)
  anthropic-messages   Anthropic Messages API(Claude / 部分中转的 anthropic 入口)
  openai-responses     OpenAI Responses API
  google-generative-ai Google Generative AI

改完写入 .env,并把 provider 同步进 ~/.pi/agent/models.json(provider 名固定为 "rotom"),
harness 启动时也会自动同步一次,所以现场只要改 .env 或跑这个脚本即可。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
MODELS_JSON = Path.home() / ".pi" / "agent" / "models.json"
PROVIDER_NAME = "rotom"

API_TYPES = ("openai-completions", "anthropic-messages", "openai-responses", "google-generative-ai")

DEFAULTS = {
    # 策略包(见 profiles/*.json 与 docs/PROFILES.md):
    #   zhongce = 众测/跑分平台(本仓库默认);ctf = CTF 比赛;pentest = 正式渗透(目标来自 targets.json)
    "PROFILE": "zhongce",
    # 平台适配器:tsec = TSec Benchmark(容器自开自关 + 一题多 flag);
    # 留空 = 原 CTF 平台(contest_api);arena = NSSCTF Agent Arena
    # (PROFILE=pentest 时本项被忽略:任务源是本地 targets.json)
    "CONTEST_ADAPTER": "tsec",
    # ---- 跑分平台(创建跑分任务后由平台下发)----
    "BENCHMARK_BASE_URL": "",
    "BENCHMARK_TOKEN": "",
    "VPN_CHECK_URL": "http://10.0.100.58",      # 靶场 VPN 联通预检地址(只在 VPN 内可达)
    # 兼容旧字段(切回 CTF 适配器时才用)
    "TEAM_TOKEN": "",
    "CONTEST_BASE": "https://apiterminator.ichunqiu.com",
    "CONTEST_QUERY_PATH": "/04cb510e425bd8f64fa97ba66f3935e1",
    "CONTEST_RESET_PATH": "/deed3dba39e57b7cf95ea63ddd84e0c8",
    "CONTEST_SUBMIT_PATH": "/ff874ef3172cbf4fd6ec2c5653a568e2",
    # ---- 模型 ----
    "PI_PROVIDER": PROVIDER_NAME,
    "PI_BASE_URL": "",
    "PI_API_KEY": "",
    "PI_MODEL": "",
    "PI_API_TYPE": "openai-completions",
    # ---- 容器名额与节奏(众测平台同时最多 3 道题的容器)----
    "MAX_ACTIVE_CHALLENGES": "3",
    "QUESTION_SLICE_SECONDS": "1800",
    "ROTATE_COOLDOWN": "60",
    # 网络:direct=直连(忽略本机代理变量,默认);system=沿用系统 http_proxy 等设置
    "NET_PROXY": "direct",
}

PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def apply_net_policy(child_env: dict, cfg: dict | None = None) -> str:
    """按 NET_PROXY / NETWORK_MODE 决定子进程的代理变量,返回实际生效的网络模式。

    网络策略只此一处,worker(solver)、控制台的「测试模型」(dashboard)和 start.sh 共用同一套语义:
      direct(默认) 摘掉代理变量。本机代理客户端一关,带着代理变量发请求会瞬间 Connection error
                    (实测 pi 直接报错),而且比赛接口域名本来就要求直连。
      system       保留环境里已有的代理设置。
    另外 NETWORK_MODE=local_only 时把代理指向黑洞端口,让白名单之外的流量一律出不去。
    """
    cfg = cfg or {}
    mode = (cfg.get("NET_PROXY") or os.environ.get("NET_PROXY") or "direct").strip() or "direct"
    local_only = (cfg.get("NETWORK_MODE") or os.environ.get("NETWORK_MODE")) == "local_only"
    if mode == "direct":
        for k in PROXY_KEYS:
            child_env.pop(k, None)
        # 系统级免疫:macOS 的**系统代理**(scutil --proxy,不是环境变量)会被 python requests 自动继承,
        # 打 10.x 靶场会莫名 502(2026-09-28 第三次测试实测踩到,当时靠手工 trust_env=False 绕过)。
        # 设 no_proxy=* 时 urllib/requests/curl 都判定"全部直连",连系统代理设置一起绕开。
        # ⚠ 断网模式(local_only)例外:那里靠"黑洞代理 + no_proxy 白名单"实现隔离,设 * 会把隔离打穿。
        if not local_only:
            child_env["no_proxy"] = child_env["NO_PROXY"] = "*"
    if local_only:
        for k in PROXY_KEYS:
            child_env[k] = "http://127.0.0.1:9"
    return mode


def adapter_name() -> str:
    """当前平台适配器(tsec / arena / 空=原 CTF 平台)。**环境变量 → .env 文件 → 空**。

    为什么要这个函数:worker 里的工具(tools/submit_flag.py 等)以前只看 os.environ ——
    托管容器里如果 .env 没被导出到环境,就会**静默回退**到 CTF 适配器(旧域名 apiterminator),
    提交全部被拒(第六次 Docker 测试实测:4 小时 40 分、250+ 次提交全废)。读 .env 之后,
    即使环境没导出也选得对;真选不出来时交给调用方显式报错,不再静默猜。
    """
    v = (os.environ.get("CONTEST_ADAPTER") or "").strip()
    if not v:
        try:
            v = (read_env().get("CONTEST_ADAPTER") or "").strip()
        except Exception:                                       # noqa: BLE001
            v = ""
    return v.lower()


def read_env() -> dict:
    data: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            data[k.strip()] = v.split("#")[0].strip()
    return data


def write_env(updates: dict[str, str]) -> None:
    """原地更新 .env:已有键改值,缺失键追加到文件末尾(保留原有注释与顺序)。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key = s.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines.append("")
        lines.append("# ---- 由 config.py 追加 ----")
        for k, v in remaining.items():
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask(v: str) -> str:
    if not v:
        return "(未设置)"
    return v[:8] + "…" + v[-4:] if len(v) > 14 else v[:4] + "…"


def sync_provider(cfg: dict, quiet: bool = False) -> bool:
    """把 .env 里的模型配置写成 pi 的 provider 条目(models.json 里的 "rotom")。"""
    base_url, key, model = cfg.get("PI_BASE_URL", ""), cfg.get("PI_API_KEY", ""), cfg.get("PI_MODEL", "")
    api = cfg.get("PI_API_TYPE", "openai-completions") or "openai-completions"
    if not (base_url and model):
        if not quiet:
            print("跳过同步:PI_BASE_URL / PI_MODEL 未设置(将继续使用 models.json 里已有的 provider)")
        return False
    if api not in API_TYPES:
        print(f"警告:未知 api 类型 {api},将按 openai-completions 处理")
        api = "openai-completions"

    data = {}
    if MODELS_JSON.exists():
        try:
            data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"警告:{MODELS_JSON} 不是合法 JSON,已跳过同步")
            return False
    providers = data.setdefault("providers", {})

    model_cfg = {"id": model, "name": model, "reasoning": True, "input": ["text"],
                 "contextWindow": 131072, "maxTokens": 32768,
                 "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    entry = {
        "displayName": f"{model} (比赛模型)",
        "baseUrl": base_url,
        "api": api,
        "apiKey": key or "unused",
        "models": [model_cfg],
    }
    if api.startswith("openai"):  # 中转/国产服务常见的兼容性处理
        entry["compat"] = {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}
    if providers.get(PROVIDER_NAME) == entry:
        if not quiet:
            print(f"provider '{PROVIDER_NAME}' 配置未变化,无需同步")
        return True
    if MODELS_JSON.exists():  # 内容确实要变才备份
        bak = MODELS_JSON.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(MODELS_JSON, bak)
        for old in sorted(MODELS_JSON.parent.glob("models.json.bak-*"))[:-3]:
            old.unlink(missing_ok=True)  # 只保留最近 3 份备份
    providers[PROVIDER_NAME] = entry
    MODELS_JSON.parent.mkdir(parents=True, exist_ok=True)   # 全新环境(容器)里 ~/.pi/agent 还不存在,写之前先建
    MODELS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if not quiet:
        print(f"已同步 provider '{PROVIDER_NAME}' -> {MODELS_JSON}")
        print(f"  baseUrl = {base_url}\n  api     = {api}\n  model   = {model}")
    return True


def test_model(cfg: dict) -> int:
    provider = cfg.get("PI_PROVIDER") or PROVIDER_NAME
    model = cfg.get("PI_MODEL") or ""
    if not model:
        print("未设置 PI_MODEL,无法测试")
        return 2
    print(f"用 pi 实测: --provider {provider} --model {model}")
    env = {**os.environ}
    if cfg.get("PI_API_KEY_VAR") and cfg.get("PI_API_KEY"):
        env[cfg["PI_API_KEY_VAR"]] = cfg["PI_API_KEY"]
    cmd = ["pi", "-p", "--provider", provider, "--model", model, "--no-session",
           "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files",
           "只回复两个字:正常"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        print("测试超时(120s):检查 base URL / 网络 / 代理")
        return 3
    out = (r.stdout or "").strip()
    print("stdout:", out[:300] or "(空)")
    if r.returncode == 0 and out:
        print("模型连通正常")
        return 0
    print("stderr:", (r.stderr or "").strip()[:500])
    print("模型测试失败")
    return 1


def test_vpn(cfg: dict) -> int:
    """靶场 VPN 联通预检:GET <VPN_CHECK_URL> 必须返回 {"status":"ok"}。

    这是整个跑分流程的强制前置 —— 题目入口地址只在 VPN 内可达。
    """
    import urllib.request
    url = (cfg.get("VPN_CHECK_URL") or "http://10.0.100.58").strip()
    print(f"VPN 预检:GET {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rotom-agent/1.0"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连,不走本机代理
        with opener.open(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"  ✘ 不可达:{type(e).__name__}: {e}")
        print("  → 说明 VPN 没连上(或刚断开)。连上靶场 VPN 后再试;本机没装 openvpn 时见 README「VPN」一节。")
        return 1
    print(f"  原始响应:{raw[:200]}")
    try:
        data = json.loads(raw)
    except Exception:
        print("  ✘ 响应不是 JSON,可能被网关/代理劫持")
        return 1
    if str(data.get("status", "")).lower() == "ok":
        print(f"  ✔ VPN 已连通(client_ip={data.get('client_ip')})")
        return 0
    print("  ✘ status 不是 ok —— VPN 未真正连通")
    return 1


def show(cfg: dict) -> None:
    print(f"配置文件: {ENV_PATH}")
    try:
        import profile as profile_mod
        prof = profile_mod.load((cfg.get("PROFILE") or "").strip() or None)
        print(f"  策略包       = {prof.name} · {prof.title}")
        print(f"    {prof.get('description') or ''}")
        print(f"    目标记账 = {prof.goal}   容器策略 = "
              f"manage:{prof.get('containers.manage')} rotate:{prof.get('containers.rotate')} "
              f"vpn_gate:{prof.get('containers.vpn_gate')}")
    except Exception as e:
        print(f"  策略包       = (读取失败: {e})")
    print(f"  平台适配器   = {cfg.get('CONTEST_ADAPTER') or 'contest(原 CTF 平台)'}")
    print("跑分平台(TSec Benchmark):")
    print(f"  BENCHMARK_BASE_URL = {cfg.get('BENCHMARK_BASE_URL') or '(未设置)'}")
    print(f"  BENCHMARK_TOKEN    = {mask(cfg.get('BENCHMARK_TOKEN', ''))}")
    print(f"  VPN_CHECK_URL      = {cfg.get('VPN_CHECK_URL') or '(默认 http://10.0.100.58)'}")
    print("模型:")
    print(f"  PI_PROVIDER  = {cfg.get('PI_PROVIDER', PROVIDER_NAME)}")
    print(f"  PI_BASE_URL  = {cfg.get('PI_BASE_URL') or '(未设置,用 models.json 里已有 provider)'}")
    print(f"  PI_API_KEY   = {mask(cfg.get('PI_API_KEY', ''))}")
    print(f"  PI_MODEL     = {cfg.get('PI_MODEL') or '(未设置)'}")
    print(f"  PI_API_TYPE  = {cfg.get('PI_API_TYPE', '')}")
    print("容器名额:")
    print(f"  MAX_ACTIVE_CHALLENGES = {cfg.get('MAX_ACTIVE_CHALLENGES') or 3}(平台硬上限 3)")
    print(f"  QUESTION_SLICE_SECONDS = {cfg.get('QUESTION_SLICE_SECONDS') or 0}(0=不轮换)")
    print(f"  (可选)PI_API_KEY_VAR = {cfg.get('PI_API_KEY_VAR') or '(未设置)'}")
    print("\n并发/超时等其它参数见 .env")


def main() -> int:
    ap = argparse.ArgumentParser(description="统一配置入口(改完自动同步到 pi)")
    ap.add_argument("--benchmark-url", help="跑分平台 BASE_URL(创建跑分任务时下发)")
    ap.add_argument("--benchmark-token", help="跑分平台 BENCHMARK_TOKEN(UUID)")
    ap.add_argument("--vpn-check-url", help="VPN 预检地址(默认 http://10.0.100.58)")
    ap.add_argument("--adapter", help="平台适配器:tsec(众测)/arena(靶场)/留空(原 CTF 平台)")
    ap.add_argument("--profile", help="策略包:zhongce(众测)/ctf(CTF 比赛)/pentest(正式渗透)")
    ap.add_argument("--token", help="兼容:等同 --benchmark-token")
    ap.add_argument("--base-url", help="模型 API base URL,例如 https://api.xxx/v1")
    ap.add_argument("--key", help="模型 API key")
    ap.add_argument("--model", help="模型名字")
    ap.add_argument("--api-type", choices=API_TYPES, help="API 类型")
    ap.add_argument("--provider", help=f"provider 名(默认 {PROVIDER_NAME})")
    ap.add_argument("--test", action="store_true", help="用 pi 实测模型连通性")
    ap.add_argument("--vpn", action="store_true", help="靶场 VPN 联通预检")
    ap.add_argument("--no-sync", action="store_true", help="只改 .env,不写 models.json")
    args = ap.parse_args()

    updates = {}
    if args.benchmark_url:
        updates["BENCHMARK_BASE_URL"] = args.benchmark_url.strip().rstrip("/")
    for tok in (args.benchmark_token, args.token):
        if tok:
            updates["BENCHMARK_TOKEN"] = tok.strip()
    if args.vpn_check_url:
        updates["VPN_CHECK_URL"] = args.vpn_check_url.strip()
    if args.adapter:
        updates["CONTEST_ADAPTER"] = args.adapter.strip().lower()
    if args.profile:
        name = args.profile.strip().lower()
        try:                                     # 写之前先确认策略包真的存在,别把 .env 写坏
            import profile as profile_mod
            profile_mod.load(name)
        except Exception as e:
            print(f"策略包不存在或不可用({name}): {e}")
            return 2
        updates["PROFILE"] = name
    if args.base_url:
        updates["PI_BASE_URL"] = args.base_url.strip().rstrip("/")
    if args.key:
        updates["PI_API_KEY"] = args.key.strip()
    if args.model:
        updates["PI_MODEL"] = args.model.strip()
    if args.api_type:
        updates["PI_API_TYPE"] = args.api_type
    if args.provider:
        updates["PI_PROVIDER"] = args.provider.strip()

    if updates:
        write_env(updates)
        print("已更新 .env:", ", ".join(f"{k}={'***' if 'KEY' in k or 'TOKEN' in k else v}"
                                        for k, v in updates.items()))
    cfg = {**DEFAULTS, **read_env()}
    if updates and not args.no_sync:
        sync_provider(cfg)
    if args.vpn:
        return test_vpn(cfg)
    if args.test:
        if not args.no_sync:
            sync_provider(cfg, quiet=True)
        return test_model(cfg)
    if not updates:
        show(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
