#!/usr/bin/env python3
"""按新版规则打包**可交付、可复测、可审计**的智能体部署包。

规则要求(2026 决赛手册第七条):挑战轮次结束后工作人员统一收集智能体用于复测与审计,
需提交 .zip 或 .tar.gz,根目录附 README.md 说明:
  (1) 运行环境与依赖   (2) 配置与启动指南(含 API Key/代理/目标地址说明与一键启动脚本)
  (3) 日志与审计路径(便于调取 Thought/Action/Observation 完整轨迹)

用法:
  python3 tools/package_submission.py                  # 默认项目,输出 dist/*.tar.gz
  python3 tools/package_submission.py --project round1
  python3 tools/package_submission.py --zip            # 额外产出 .zip

产出: dist/wqh-agent-<项目>-<时间戳>.tar.gz
包内: 代码 + README.md + RUNBOOK.md + traces/(审计轨迹) + .env.example(脱敏) + board.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CODE_FILES = ["runner.py", "solver.py", "board.py", "contest_api.py", "config.py",
              "dashboard.py", "run.sh", "dashboard.sh", "启动控制台.command", "停止控制台.command",
              "README.md"]
CODE_DIRS = ["tools", "tests"]
SECRET_KEYS = ("PI_API_KEY", "KIMI_API_KEY", "TEAM_TOKEN", "OPENAI_API_KEY")


def find_run_dir(project: str) -> Path:
    if project in ("默认项目", "default"):
        return ROOT
    p = ROOT / "projects" / project
    return p if p.exists() else ROOT


def env_example() -> str:
    """脱敏后的配置模板:key 全部换成占位符,保留字段名与注释。"""
    src = ROOT / ".env"
    if not src.exists():
        return "# 未找到 .env\n"
    out = []
    for line in src.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            if k.strip() in SECRET_KEYS or "KEY" in k or "TOKEN" in k:
                line = f"{k.strip()}=<在此填入现场发放的 key / token>"
        out.append(line)
    return "\n".join(out) + "\n"


def versions() -> dict:
    v = {}
    for cmd, name in (("python3", "python3"), ("pi", "pi"), ("git", "git")):
        try:
            v[name] = subprocess.run([cmd, "--version"], capture_output=True, text=True,
                                     timeout=10).stdout.strip()
        except Exception:
            v[name] = "未安装"
    v["platform"] = f"{os.uname().sysname} {os.uname().machine}"
    return v


def readme_for_package(project: str, rundir: Path, v: dict) -> str:
    return f"""# wqh 智能体部署包(湾区杯 AI 专项挑战赛 · 环节二/三)

本项目为 AI 智能体自动解题框架:通过比赛平台接口拉题 → 用 pi CLI 驱动大模型自主解题(可调用
本机与本地虚拟机中的 CTF 工具链) → 拿到 flag 后自动提交,全程无人干预。

- 项目名:`{project}`
- 数据目录:`{rundir}`
- 打包时间:{time.strftime('%F %T')}

---

## 1. 运行环境与依赖

| 项目 | 版本/说明 |
|---|---|
| 操作系统 | {v['platform']} |
| Python | {v['python3']}(仅用标准库,无需 pip 安装即可运行主程序) |
| pi CLI | {v['pi']}(模型接入层,负责驱动大模型解题) |
| git | {v['git']}(仅打包/更新用，运行不需要) |

**解题工具链(非必需,但缺失会显著降低解题率)**
- macOS 本机:tshark、binwalk、sqlmap、nmap、hashcat、john、ROPgadget、ropper、patchelf、
  checksec、volatility3(vol),以及 Python 库 pwntools/z3-sympy/gmpy2/pycryptodome/numpy/PIL
- Linux 虚拟机(OrbStack,amd64):`pwn64` 机器内已装 gdb/gdbserver/pwntools/radare2/binwalk/
  foremost/exiftool/steghide/sleuthkit/yara/hashcat/john/tshark/ffuf/nikto/socat
- 可选第三方库(仅当需要用 pi 之外的脚本时):无。主程序零第三方依赖。

安装示例(如需重建工具链):
```bash
brew install tshark binwalk sqlmap nmap hashcat john
pip3 install pwntools z3-solver pycryptodome gmpy2 numpy pillow volatility3
orb -m pwn64 bash -lc 'sudo apt-get install -y gdb binwalk foremost exiftool steghide sleuthkit yara hashcat john tshark ffuf nikto radare2'
```

---

## 2. 配置与启动指南

### 2.1 配置文件
所有配置集中在根目录 **`.env`**(包内提供脱敏模板 `.env.example`,复制为 `.env` 后填写)。

| 字段 | 含义 |
|---|---|
| `TEAM_TOKEN` | 比赛队伍 token(平台"参赛信息"处获取) |
| `CONTEST_BASE` / `CONTEST_QUERY_PATH` / `CONTEST_RESET_PATH` / `CONTEST_SUBMIT_PATH` | 比赛接口域名与三个接口路径,现场若变更只改这里 |
| `PI_BASE_URL` / `PI_API_KEY` / `PI_MODEL` / `PI_API_TYPE` | 模型接入信息(现场发放的 Base URL、Key、模型名、接口类型) |
| `START_WORKERS` / `WORKERS_PER_QUESTION` | 并发与同题 agent 数 |
| `IGNORE_SOLVED` | 1=练习模式(重跑已解出的题),0=正式比赛 |

**代理说明**:若所在网络需要代理才能访问模型服务,请在 `.env` 中设置 `http_proxy`/`https_proxy`;
比赛接口域名(`*.ichunqiu.com`)框架内部**强制直连**,不受代理影响。

### 2.2 一键启动(二选一)

```bash
./启动控制台.command        # 方式 A:双击/执行后打开网页控制台(自动选端口,打印地址)
./run.sh                    # 方式 B:直接命令行开跑(无界面)
```

网页控制台(http://127.0.0.1:8799,端口被占用会自动顺延)可完成全部操作:
填写 token 与模型信息 → 点「测试接口」「测试模型」验证连通 → 点「开始跑」;
也可新建项目(独立工作区并行对照)与随时「停止」。

### 2.3 目录结构
```
runner.py            调度主循环:拉题→派发→回收→重试→对账
solver.py            单题解题 worker:准备独立工作区、组装 prompt、启动 pi
board.py             黑板:题目状态机与事件记录(board.json)
contest_api.py       比赛三接口封装(查题/重置/提交),支持任意地址
config.py            配置入口(命令行改 token/模型/接口地址)
dashboard.py         网页控制台与实时看板
tools/               submit_flag.py(提交)、reset_env.py(重置)、export_trace.py(轨迹导出)
tests/               本地模拟比赛服务器与调度回归测试(离线自测用)
.env                 配置(含密钥,勿外传)
board.json           题目状态与事件流(运行时生成)
work/<大类>-<题名>/  每题独立工作区(SHARED.md 共享黑板 / files 附件 / w1,w2 各 agent 目录)
logs/*.jsonl         每个 agent 的原始运行日志(Thought/Action/Observation 事件流)
logs/submissions.log 提交记录
traces/              审计轨迹(由 tools/export_trace.py 生成)
```

---

## 3. 日志与审计路径

| 内容 | 位置 | 说明 |
|---|---|---|
| 原始事件流 | `logs/<question_id>_w<槽位>_<时间戳>.jsonl` | pi 的完整会话事件:模型思考、工具调用与参数、工具输出、错误 |
| 可读轨迹 | `traces/<question_id>_w<槽位>.md` | 已转成 Thought / Action / Observation 三段式 Markdown |
| 轨迹汇总 | `traces/SUMMARY.md` | 每题的 agent 数、状态、尝试次数与提交记录 |
| 调度日志 | `logs/runner.out` | 拉题、派发、重试、限流降并发等调度过程 |
| 提交记录 | `logs/submissions.log` | 每次提交的时间、题目、flag、平台返回 |

重新导出轨迹(任何时候可复现):
```bash
python3 tools/export_trace.py            # 结果写入 traces/
```

审计对应关系:一次工具调用 = `tool_execution_start` 事件(Action)+ 紧随其后的 `toolResult`
(Observation);模型发言/推理 = `message_end` 中的 assistant 文本(Thought)。导出脚本已按此语义转换。

---

## 4. 复测建议

- 复测相同题目:`IGNORE_SOLVED=1` 后可重跑已解出的题(每题工作区相互隔离,不会互相覆盖)。
- 离线自测(不连比赛平台):`python3 tests/mock_contest_server.py --port 8899` 起本地同构接口,
  再用 `.tests/local_contest.sh start` 跑一遍完整闭环。
- 调度回归测试(不调用模型):`python3 tests/scale_test.py 30`。
"""


def collect(project: str, rundir: Path, with_knowledge: bool) -> Path:
    stage = ROOT / "dist" / f"_stage_{int(time.time())}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    for f in CODE_FILES:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, stage / f)
    if with_knowledge and (ROOT / "knowledge").exists():
        shutil.copytree(ROOT / "knowledge", stage / "knowledge",
                        ignore=shutil.ignore_patterns("__pycache__", ".kb_index.json"))
    for d in CODE_DIRS:
        src = ROOT / d
        if src.exists():
            shutil.copytree(src, stage / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "localrun"))
    # 轨迹(含导出)
    try:
        subprocess.run([sys.executable, str(ROOT / "tools" / "export_trace.py"),
                        "--run-dir", str(rundir)], capture_output=True, timeout=300)
    except Exception:
        pass
    if (rundir / "traces").exists():
        shutil.copytree(rundir / "traces", stage / "traces", dirs_exist_ok=True)
    for f in ("board.json", "logs/submissions.log", "logs/runner.out"):
        src = rundir / f
        if src.exists():
            (stage / "logs").mkdir(exist_ok=True)
            shutil.copy2(src, stage / "logs" / Path(f).name)
    # 配置模板 + 说明
    (stage / ".env.example").write_text(env_example(), encoding="utf-8")
    (stage / "README.md").write_text(readme_for_package(project, rundir, versions()), encoding="utf-8")
    return stage


def archive(stage: Path, project: str, also_zip: bool) -> list[Path]:
    outdir = ROOT / "dist"
    outdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    name = f"wqh-agent-{project}-{stamp}"
    made = []
    tgz = outdir / f"{name}.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(stage, arcname=name)
    made.append(tgz)
    if also_zip:
        zp = outdir / f"{name}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in stage.rglob("*"):
                if p.is_file():
                    zf.write(p, arcname=f"{name}/{p.relative_to(stage)}")
        made.append(zp)
    shutil.rmtree(stage)
    return made


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="默认项目")
    ap.add_argument("--zip", action="store_true", help="额外产出 .zip")
    ap.add_argument("--with-knowledge", action="store_true", help="附带 CTF 知识库(约 170MB)")
    args = ap.parse_args()

    rundir = find_run_dir(args.project)
    stage = collect(args.project, rundir, args.with_knowledge)
    made = archive(stage, args.project, args.zip)
    print("打包完成:")
    for p in made:
        print(f"  {p}  ({p.stat().st_size / 1e6:.1f} MB)")
    print("\n包内已含:代码 / README.md(环境·配置·日志审计) / traces(审计轨迹) / .env.example(脱敏)")


if __name__ == "__main__":
    main()
