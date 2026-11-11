#!/usr/bin/env python3
"""一条命令把"能不能开跑"体检完:VPN / 跑分平台 / 模型端点 / 代理设置 / 靶场目标。

用法:
    python3 tools/netcheck.py            # 全量体检
    python3 tools/netcheck.py --no-model # 跳过模型端点探测(不想动 pi 配置时)

设计原则:**只报事实 + 明确下一步**,不猜。每一项都写清"不通时你该做什么"。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "\033[32m✔\033[0m", "\033[33m!\033[0m", "\033[31m✘\033[0m"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连,不走本机代理


def env() -> dict:
    import config as config_mod
    return {**config_mod.DEFAULTS, **config_mod.read_env()}


def step(name: str, ok: bool, detail: str, hint: str = "") -> bool:
    print(f"  {OK if ok else BAD} {name}: {detail}")
    if not ok and hint:
        print(f"      → {hint}")
    return ok


def check_proxy(cfg: dict) -> bool:
    keys = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    present = {k: os.environ[k] for k in keys if os.environ.get(k)}
    mode = cfg.get("NET_PROXY") or "direct"
    print(f"  网络模式 NET_PROXY={mode}")
    if mode == "direct":
        if present:
            print(f"  {WARN} 环境里有代理变量,但 direct 模式会在子进程里摘掉它们:{list(present)}")
            print("      → 这是预期行为(本机代理客户端关掉后,带着代理变量发请求会瞬间 Connection error)")
        else:
            print(f"  {OK} 没有代理变量干扰")
        return True
    if present:
        print(f"  {OK} system 模式,沿用系统代理:{present}")
        return True
    print(f"  {WARN} system 模式但没有代理变量 —— 如果模型/平台域名需要走代理,这里会连不上")
    return True


def check_vpn(cfg: dict) -> bool:
    url = (cfg.get("VPN_CHECK_URL") or "http://10.0.100.58").strip()
    print(f"  预检地址 {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rotom-agent/1.0"})
        with _OPENER.open(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        return step("VPN 联通", False, f"{type(e).__name__}: {e}",
                    "先连靶场 VPN(平台下发的 .ovpn)。本机没装 openvpn 时:brew install openvpn,"
                    "或用 Tunnelblick / OpenVPN Connect 打开 .ovpn;连上后跑 ./start.sh vpn 复验")
    return step("VPN 联通", str(data.get("status", "")).lower() == "ok",
                f"status={data.get('status')} client_ip={data.get('client_ip')}",
                "拿到的是别的响应 → 可能被网关/代理劫持,检查 VPN 是否真连上")


def check_platform(cfg: dict) -> tuple[bool, list]:
    base = (cfg.get("BENCHMARK_BASE_URL") or "").strip()
    token = (cfg.get("BENCHMARK_TOKEN") or "").strip()
    if not base:
        return step("跑分平台", False, "BENCHMARK_BASE_URL 未配置",
                    "在 TSecBench 创建跑分任务后会下发,填进 .env 或网页控制台"), []
    print(f"  平台地址 {base}")
    if not token:
        return step("跑分平台", False, "BENCHMARK_TOKEN 未配置", "同上,创建跑分任务时一起下发"), []
    if (cfg.get("CONTEST_ADAPTER") or "tsec").lower() == "tsec":
        import tsec_api as api
    else:
        import contest_api as api
    try:
        qs = api.list_questions(token, timeout=20)
    except Exception as e:
        code = getattr(e, "code", "")
        return step("跑分平台", False, f"{code or type(e).__name__}: {e}",
                    "task_not_found=token 无效;invalid_state=任务已结束;网络类错误=地址/代理问题"), []
    done = sum(1 for q in qs if q.get("is_solved"))
    flags = sum(int(q.get("correct_flag_count") or 0) for q in qs)
    total = sum(int(q.get("flag_count") or 1) for q in qs)
    step("跑分平台", True, f"{len(qs)} 道题,已通关 {done} 道,flag {flags}/{total}")
    return True, qs


def check_targets(qs: list) -> None:
    addrs = []
    for q in qs:
        addrs += [str(a) for a in (q.get("container_addr") or [])]
    if not addrs:
        print(f"  {WARN} 当前没有已启动的靶场容器(正常:harness 会自动开;也可以看下面的模型端点)")
        return
    for a in addrs[:5]:
        host, _, port = a.rpartition(":")
        try:
            with socket.create_connection((host, int(port or 80)), timeout=5):
                print(f"  {OK} 靶场目标 {a} 可达")
        except Exception as e:
            print(f"  {BAD} 靶场目标 {a} 不可达({type(e).__name__})")
            print("      → 容器地址只在 VPN 内可达:确认 VPN 已连、且没有被本机代理拦掉")


def check_model(cfg: dict) -> bool:
    provider = cfg.get("PI_PROVIDER") or "rotom"
    models_json = Path.home() / ".pi" / "agent" / "models.json"
    if not models_json.exists():
        return step("模型端点", False, f"找不到 {models_json}", "先装 pi CLI 并配置 provider")
    try:
        entry = (json.loads(models_json.read_text(encoding="utf-8")).get("providers") or {}).get(provider) or {}
    except Exception as e:
        return step("模型端点", False, f"读 models.json 失败: {e}")
    base = (cfg.get("PI_BASE_URL") or entry.get("baseUrl") or "").strip()
    model = cfg.get("PI_MODEL") or (entry.get("models") or [{}])[0].get("id") or ""
    if not base:
        return step("模型端点", False, f"provider '{provider}' 没有 baseUrl",
                    "在网页控制台填 Base URL + Key + 模型名,或 config.py --base-url/--key/--model")
    print(f"  provider {provider} → {base}  模型 {model or '(未指定)'}")
    u = urllib.parse.urlparse(base)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=6):
            pass
    except Exception as e:
        return step("模型端点", False, f"TCP 连不上 {host}:{port}({type(e).__name__})",
                    "检查域名/DNS/网络;若必须走代理,把 .env 的 NET_PROXY 设成 system")
    try:
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=6) as s:
                with ctx.wrap_socket(s, server_hostname=host):
                    pass
        step("模型端点", True, f"{host}:{port} TCP{'/TLS' if u.scheme == 'https' else ''} 可达")
        print("      (只验连通性;真正的模型调用用网页控制台的「测试模型」)")
        return True
    except Exception as e:
        return step("模型端点", False, f"TLS 握手失败:{e}", "常见于本机代理劫持 HTTPS:把 NET_PROXY 设成 direct")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-model", action="store_true", help="跳过模型端点探测")
    args = ap.parse_args()
    cfg = env()

    print("=" * 60)
    print(" Rotom_beta · 开跑前体检")
    print("=" * 60)
    print("\n[1] 网络与代理")
    check_proxy(cfg)
    print("\n[2] 靶场 VPN(众测的强制前置)")
    vpn_ok = check_vpn(cfg)
    print("\n[3] 跑分平台")
    plat_ok, qs = check_platform(cfg)
    print("\n[4] 靶场目标")
    check_targets(qs)
    if not args.no_model:
        print("\n[5] 模型端点")
        check_model(cfg)

    print("\n" + "=" * 60)
    if vpn_ok and plat_ok:
        print(f" {OK} 可以开跑:./start.sh")
        return 0
    print(f" {BAD} 还有前置条件没满足,先按上面的 → 提示处理")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
