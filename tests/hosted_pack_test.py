#!/usr/bin/env python3
"""托管打包(平台 Docker)的离线回归断言。

  python3 tests/hosted_pack_test.py

盯住四件事:
① 模型网关改写严格按平台规则(`.tsecbench.gw` + http),否则托管环境连不上模型;
② 运行时环境变量 → .env 的映射正确,且**密钥只从环境变量来**;
③ 打包排除清单真的把"运行数据 / 跨轮战果 / 凭据 / 假 flag 夹具"挡在镜像外;
④ 平台差异文案:macOS 渲染与基线逐字一致,容器里换成本地工具链那套。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✔\033[0m {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \033[31m✘\033[0m {name} {extra}")


def section(t: str) -> None:
    print(f"\n== {t} ==")


# ------------------------------------------------------------------ ① 网关改写
section("大模型网关改写(平台规则:加 .tsecbench.gw + https→http)")
from tools.platform_env import from_environ, gw_rewrite  # noqa: E402

cases = {
    "https://api.deepseek.com/v1": "http://api.deepseek.com.tsecbench.gw/v1",
    "https://api.moonshot.cn/v1": "http://api.moonshot.cn.tsecbench.gw/v1",
    "https://api.kimi.com": "http://api.kimi.com.tsecbench.gw",
    "https://dashscope.aliyuncs.com/compatible-mode/v1":
        "http://dashscope.aliyuncs.com.tsecbench.gw/compatible-mode/v1",
    "http://api.deepseek.com.tsecbench.gw/v1": "http://api.deepseek.com.tsecbench.gw/v1",   # 幂等
    "": "",
}
for src, want in cases.items():
    got = gw_rewrite(src)
    check(f"{src or '(空)'} → {want or '(空)'}", got == want, f"实得 {got!r}")
check("改出来的地址一定是 http(平台要求,https 在网关下不通)",
      gw_rewrite("https://api.deepseek.com/v1").startswith("http://"))
check("认不出来的字符串原样返回(不瞎猜)", gw_rewrite("不是URL") == "不是URL")

# ------------------------------------------------------------------ ② 环境变量 → .env
section("运行时环境变量 → .env(密钥只从环境变量来)")
env = {"BENCHMARK_TOKEN": "tok-1234567890", "BENCHMARK_BASE_URL": "https://tsecbench.zc.tencent.com",
       "PI_UPSTREAM_BASE_URL": "https://api.deepseek.com/v1", "PI_API_KEY": "sk-secret-abcdefghijkl",
       "PI_MODEL": "deepseek-chat", "KB_EXTRA": "<工作目录>",
       "START_WORKERS": "6"}
cfg = from_environ(env)
check("BENCHMARK_TOKEN/BASE_URL 透传(平台注入的那两个)",
      cfg["BENCHMARK_TOKEN"] == "tok-1234567890" and cfg["BENCHMARK_BASE_URL"].startswith("https://"))
check("PI_BASE_URL 自动改写为网关地址", cfg["PI_BASE_URL"] == "http://api.deepseek.com.tsecbench.gw/v1")
check("PI_API_KEY/PI_MODEL 来自环境变量", cfg["PI_API_KEY"].startswith("sk-") and cfg["PI_MODEL"] == "deepseek-chat")
check("KB_EXTRA 被剔除(平台明令:不得内置历史答题记忆)", "KB_EXTRA" not in cfg, str(cfg.get("KB_EXTRA")))
check("标记托管模式 + 跳过靶场 VPN 预检", cfg["ROTOM_HOSTED"] == "1" and cfg["SKIP_VPN_CHECK"] == "1")
check("可调旋钮生效(START_WORKERS=6)", cfg["START_WORKERS"] == "6")
check("模型 key 不进文件头/不落盘到别的键", all("sk-secret" not in str(v) for k, v in cfg.items() if k != "PI_API_KEY"))

# ------------------------------------------------------------------ ③ 排除清单
section("镜像排除清单(运行数据 / 跨轮战果 / 凭据 / 假 flag 夹具)")
di = (ROOT / ".dockerignore").read_text(encoding="utf-8")
must_exclude = ["logs/", "work/", "projects/", "traces/", "board.json", ".env", "tests/", "backup/", "kb_extra/"]
for pat in must_exclude:
    check(f".dockerignore 排除 {pat}", pat in di, di[:80])
check("knowledge/vendor 没被排除(公开资料要进镜像)",
      not re.search(r"^knowledge/vendor/?$", di, re.M))
check("构建时会清空 kb_extra(Dockerfile 里有显式步骤)",
      "kb_extra" in (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"))
check("构建末尾跑审计自检(不干净就不出镜像)",
      "selfcheck.sh --build" in (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"))
check("selfcheck 断言了 kb_extra 为空", "kb_extra 为空" in (ROOT / "docker" / "selfcheck.sh").read_text(encoding="utf-8"))
check("selfcheck 扫 flag 样式字符串", "flag\\{" in (ROOT / "docker" / "selfcheck.sh").read_text(encoding="utf-8"))
check("入口脚本只从环境变量建 .env(不读镜像里的密钥)",
      "platform_env.py --write" in (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8"))
check("Dockerfile 在 COPY 后跑镜像内脱敏(sanitize.sh)",
      "bash docker/sanitize.sh" in (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"))
_san = (ROOT / "docker" / "sanitize.sh").read_text(encoding="utf-8")
_rules = json.loads((ROOT / "docker" / "sensitive_rules.example.json").read_text(encoding="utf-8"))
for key, why in (("identity_words", "身份/话术类禁词"), ("contact_email_domains", "联系方式"),
                 ("host_path_prefix", "宿主机路径"), ("internal_endpoints", "自建内网地址"),
                 ("credential_shapes", "凭据形态"), ("kb_extra", "跨轮战果目录")):
    if key == "kb_extra":
        check("脱敏脚本覆盖:跨轮战果目录", "kb_extra" in _san, key)
    else:
        check(f"规则配置覆盖:{why}", key in _rules and key in _san, key)
check("脱敏脚本从 sensitive_rules*.json 读规则(字面规则不入库)",
      "sensitive_rules.json" in _san and "sensitive_rules.example.json" in _san)
check("敏感类清理在构建第一阶段硬失败(规则不随镜像发布)",
      "交付物不干净" in _san and "sys.exit(1)" in _san)
check("镜像内自检不携带规则细节(改由阶段1 把关)",
      "sensitive_rules" not in (ROOT / "docker" / "selfcheck.sh").read_text(encoding="utf-8"))
check("Dockerfile 用多阶段:最终镜像只拷阶段1 的干净树",
      "COPY --from=appsrc /app /app" in (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"))
check("脱敏脚本在阶段1 里被删除(规则文本不进最终镜像)",
      "rm -f docker/sanitize.sh" in (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"))
check("入口脚本对终态原因不再重启(避免空转)",
      "goal_done platform_dead token_bad" in (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8"))

# ------------------------------------------------------------------ ④ 平台差异文案
section("平台差异文案(macOS 基线不变 / 容器内换成本地工具链)")
os.environ.pop("ROTOM_HOSTED", None)
import solver  # noqa: E402

check("macOS 上 @@PLATFORM_NOTE@@ 渲染为空串(基线逐字不变)", solver.platform_note() == "")
check("macOS 上 @@PLATFORM_TOOLS@@ 渲染为空串", solver.platform_tools_note() == "")
for var, val in (("ROTOM_HOSTED", "1"),):
    os.environ[var] = val
    note, tools = solver.platform_note(), solver.platform_tools_note()
    check("容器内 PLATFORM_NOTE 说明 Linux 原生/无外网", "Linux 容器" in note and "无外网" in note, note[:60])
    check("容器内 PLATFORM_TOOLS 说明工具本地可用 + nmap 有 root",
          "全部本地可用" in tools and "root" in tools, tools[:60])
    os.environ.pop(var, None)
os.environ.pop("ROTOM_HOSTED", None)

# orb 兼容层
orb = ROOT / "tools" / "bin" / "orb"
check("orb 兼容层存在且可执行", orb.exists() and os.access(orb, os.X_OK))
r = subprocess.run([str(orb), "-m", "pwn64", "bash", "-lc", "echo orb-ok"],
                   capture_output=True, text=True, timeout=30)
check("orb -m pwn64 bash -lc '<cmd>' 等价本地执行", r.returncode == 0 and "orb-ok" in r.stdout,
      f"rc={r.returncode} out={r.stdout[:60]} err={r.stderr[:60]}")
r2 = subprocess.run([str(orb), "-m", "pwn64", "echo", "直接执行"], capture_output=True, text=True, timeout=30)
check("orb -m pwn64 <cmd> 形态也能用", r2.returncode == 0 and "直接执行" in r2.stdout, r2.stdout[:40])
r3 = subprocess.run([str(orb), "list"], capture_output=True, text=True, timeout=30)
check("orb list 给出解释性输出(不影响 agent 判断)", "兼容层" in r3.stdout)

# 提示词里挂了占位符(否则容器内文案没地方落)
zc = (ROOT / "prompts" / "zhongce" / "prompt.md").read_text(encoding="utf-8")
check("zhongce 提示词含 @@PLATFORM_NOTE@@ / @@PLATFORM_TOOLS@@",
      "@@PLATFORM_NOTE@@" in zc and "@@PLATFORM_TOOLS@@" in zc)
check("solver 占位符表已接线", '"PLATFORM_NOTE": platform_note()' in (ROOT / "solver.py").read_text(encoding="utf-8"))

# ------------------------------------------------------------------ ⑤ 适配器/token 的 .env 回退
# 第六次 Docker 测试的致命 bug:worker 进程里 `.env` 没被导出 → 提交工具**静默**用 CTF 适配器
# (旧域名)→ 4h40m、250+ 次提交全废。修法是适配器与 token 都走"环境变量 → .env → 显式失败"。
# 这里把 `.env` 指到临时文件、清空相关环境变量,断言**只靠 .env 也能选对平台并拿到 token**。
section("适配器 / token 的 .env 回退(第六次那个致命 bug 的回归断言)")
import importlib.util  # noqa: E402

import config as cfg_mod  # noqa: E402

_saved_env_path = cfg_mod.ENV_PATH
_tmpdir = Path(tempfile.mkdtemp(prefix="rotom-envfile-"))
cfg_mod.ENV_PATH = _tmpdir / ".env"
cfg_mod.ENV_PATH.write_text(
    "CONTEST_ADAPTER=tsec\n"
    "BENCHMARK_BASE_URL=https://benchmark.example.invalid\n"
    "BENCHMARK_TOKEN=tok-from-env-file\n", encoding="utf-8")
_saved_os = {k: os.environ.pop(k, None) for k in ("CONTEST_ADAPTER", "BENCHMARK_TOKEN", "TEAM_TOKEN")}
try:
    check("config.adapter_name() 只读 .env 也解析出 tsec(不静默回退 CTF)",
          cfg_mod.adapter_name() == "tsec", cfg_mod.adapter_name())

    spec = importlib.util.spec_from_file_location("_sf_probe", ROOT / "tools" / "submit_flag.py")
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)
    check("submit_flag 的适配器同样来自 .env(不打错平台)", sf._ADAPTER == "tsec", sf._ADAPTER)
    check("submit_flag 的 token 也回退读 .env(不再'未设置 token'白跑)",
          sf._token() == "tok-from-env-file", repr(sf._token())[:40])

    import runner as runner_mod  # noqa: E402
    check("runner 的 token 也回退读 .env(导出坏掉也不至于 t=0 收工)",
          runner_mod._token_from_env_file() == "tok-from-env-file",
          repr(runner_mod._token_from_env_file())[:40])
finally:
    cfg_mod.ENV_PATH = _saved_env_path
    for k, v in _saved_os.items():
        if v is not None:
            os.environ[k] = v
    shutil.rmtree(_tmpdir, ignore_errors=True)

print(f"\n{'='*50}\n通过 {PASS} 项,失败 {FAIL} 项")
if FAILED:
    print("失败项:")
    for f in FAILED:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)