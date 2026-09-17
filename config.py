#!/usr/bin/env python3
"""统一配置入口:改 token、改模型(base URL / key / 模型名 / API 类型),并可一键测通。

用法:
  python3 config.py                                  # 查看当前配置
  python3 config.py --token icqXXXX                  # 只改队伍 token
  python3 config.py --base-url https://api.xxx/v1 --key sk-xxx \
                    --model deepseek-v4-flash --api-type openai-completions
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
    "TEAM_TOKEN": "",
    # 比赛接口地址:现场若改域名/路径,只改这几项即可(留空则用内置默认值)
    "CONTEST_BASE": "https://apiterminator.ichunqiu.com",
    "CONTEST_QUERY_PATH": "/04cb510e425bd8f64fa97ba66f3935e1",
    "CONTEST_RESET_PATH": "/deed3dba39e57b7cf95ea63ddd84e0c8",
    "CONTEST_SUBMIT_PATH": "/ff874ef3172cbf4fd6ec2c5653a568e2",
    "PI_PROVIDER": PROVIDER_NAME,
    "PI_BASE_URL": "",
    "PI_API_KEY": "",
    "PI_MODEL": "",
    "PI_API_TYPE": "openai-completions",
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
    if mode == "direct":
        for k in PROXY_KEYS:
            child_env.pop(k, None)
    if (cfg.get("NETWORK_MODE") or os.environ.get("NETWORK_MODE")) == "local_only":
        for k in PROXY_KEYS:
            child_env[k] = "http://127.0.0.1:9"
    return mode


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


def show(cfg: dict) -> None:
    print(f"配置文件: {ENV_PATH}")
    print(f"  TEAM_TOKEN   = {mask(cfg.get('TEAM_TOKEN', ''))}")
    print(f"  PI_PROVIDER  = {cfg.get('PI_PROVIDER', PROVIDER_NAME)}")
    print(f"  PI_BASE_URL  = {cfg.get('PI_BASE_URL') or '(未设置,用 models.json 里已有 provider)'}")
    print(f"  PI_API_KEY   = {mask(cfg.get('PI_API_KEY', ''))}")
    print(f"  PI_MODEL     = {cfg.get('PI_MODEL') or '(未设置)'}")
    print(f"  PI_API_TYPE  = {cfg.get('PI_API_TYPE', '')}")
    print("比赛接口:")
    print(f"  CONTEST_BASE        = {cfg.get('CONTEST_BASE') or '(默认)'}")
    print(f"  CONTEST_QUERY_PATH  = {cfg.get('CONTEST_QUERY_PATH') or '(默认)'}")
    print(f"  CONTEST_RESET_PATH  = {cfg.get('CONTEST_RESET_PATH') or '(默认)'}")
    print(f"  CONTEST_SUBMIT_PATH = {cfg.get('CONTEST_SUBMIT_PATH') or '(默认)'}")
    print(f"  (可选)PI_API_KEY_VAR = {cfg.get('PI_API_KEY_VAR') or '(未设置)'}")
    print("\n并发/模式等其它参数见 .env")


def main() -> int:
    ap = argparse.ArgumentParser(description="统一配置入口(改完自动同步到 pi)")
    ap.add_argument("--token", help="比赛队伍 token")
    ap.add_argument("--base-url", help="模型 API base URL,例如 https://api.xxx/v1")
    ap.add_argument("--key", help="模型 API key")
    ap.add_argument("--model", help="模型名字")
    ap.add_argument("--api-type", choices=API_TYPES, help="API 类型")
    ap.add_argument("--provider", help=f"provider 名(默认 {PROVIDER_NAME})")
    ap.add_argument("--contest-base", help="比赛接口域名,如 https://apiterminator.ichunqiu.com")
    ap.add_argument("--contest-query-path", help="查题接口路径(或整条 URL)")
    ap.add_argument("--contest-reset-path", help="重置环境接口路径(或整条 URL)")
    ap.add_argument("--contest-submit-path", help="提交 flag 接口路径(或整条 URL)")
    ap.add_argument("--test", action="store_true", help="用 pi 实测模型连通性")
    ap.add_argument("--no-sync", action="store_true", help="只改 .env,不写 models.json")
    args = ap.parse_args()

    updates = {}
    if args.token:
        updates["TEAM_TOKEN"] = args.token.strip()
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
    for flag, key in (("contest_base", "CONTEST_BASE"),
                      ("contest_query_path", "CONTEST_QUERY_PATH"),
                      ("contest_reset_path", "CONTEST_RESET_PATH"),
                      ("contest_submit_path", "CONTEST_SUBMIT_PATH")):
        v = getattr(args, flag)
        if v:
            updates[key] = v.strip()

    if updates:
        write_env(updates)
        print("已更新 .env:", ", ".join(f"{k}={'***' if 'KEY' in k or 'TOKEN' in k else v}"
                                        for k, v in updates.items()))
    cfg = {**DEFAULTS, **read_env()}
    if updates and not args.no_sync:
        sync_provider(cfg)
    if args.test:
        if not args.no_sync:
            sync_provider(cfg, quiet=True)
        return test_model(cfg)
    if not updates:
        show(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
