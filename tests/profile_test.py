#!/usr/bin/env python3
"""策略包(profile)离线测试:三个包 + 黄金对照 + 正式渗透链路 + 策略门控。

  python3 tests/profile_test.py

不联网、不调用模型;依赖:
  - tests/golden/prompt_{ctf,zhongce}.txt —— 改造前的 prompt 存档(黄金对照)
  - profiles/*.json / prompts/*/ —— 策略包与提示词包
"""

from __future__ import annotations

import json
import os
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


def run_isolated(code: str, profile: str, run_dir: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """在子进程里跑一段代码(PROFILE 必须在 import 前设好)。"""
    env = {**os.environ, "PROFILE": profile, "ROTOM_RUN_DIR": run_dir,
           "PYTHONPATH": str(ROOT)}
    for k in ("RUN_DEADLINE_TS", "NETWORK_MODE", "CONTEST_ADAPTER", "BENCHMARK_TOKEN", "TEAM_TOKEN"):
        env.pop(k, None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(ROOT), env=env, timeout=120)


# ------------------------------------------------------------------ 策略包本身
section("策略包(profiles/*.json)")
os.environ["ROTOM_RUN_DIR"] = tempfile.mkdtemp(prefix="rotom-profile-test-")
os.environ.pop("PROFILE", None)
import profile as profile_mod  # noqa: E402

check("默认包是 zhongce(本仓库默认=众测/跑分模式)", profile_mod.current().name == "zhongce",
      profile_mod.current().name)
names = profile_mod.names()
check("三个包都在:ctf / zhongce / pentest",
      {"ctf", "zhongce", "pentest"} <= set(names), str(names))

ctf, zc, pt = (profile_mod.load(n) for n in ("ctf", "zhongce", "pentest"))
check("ctf:目标=flags、容器全托管(不轮换/不 VPN)", ctf.goal == "flags"
      and ctf.get("containers.manage") is False and ctf.get("containers.rotate") is False
      and ctf.get("containers.vpn_gate") is False, json.dumps(ctf.data.get("containers")))
check("zhongce:目标=flags、容器自管+轮换、VPN 预检默认关(用户要求)", zc.goal == "flags"
      and zc.get("containers.manage") is True and zc.get("containers.rotate") is True
      and zc.get("containers.vpn_gate") is False, json.dumps(zc.data.get("containers")))
check("pentest:目标=findings、任务源=targets_api、VPN 预检开", pt.goal == "findings"
      and pt.task_source == "targets_api" and pt.get("containers.vpn_gate") is True)
check("pentest:同题 1 个 agent / 增援上限 2", pt.dispatch_int("workers_per_question", "X", 9) == 1
      and pt.dispatch_int("max_helpers", "X", 9) == 2)
check("ctf:同题 2 个 agent / 增援上限 4", ctf.dispatch_int("workers_per_question", "X", 9) == 2
      and ctf.dispatch_int("max_helpers", "X", 9) == 4)
os.environ["WORKERS_PER_QUESTION"] = "7"
check("策略参数以策略包为准(不被 .env 残留带偏)",
      pt.dispatch_int("workers_per_question", "WORKERS_PER_QUESTION", 9) == 1)
os.environ.pop("WORKERS_PER_QUESTION", None)

# ------------------------------------------------------------------ 黄金对照
section("提示词基线(ctf / zhongce 的 prompt 必须与人工确认过的基线一致)")
# 基线 = 上一次人工确认过的 prompt 渲染结果。有意改提示词后,用
#   python3 -c "..."(见 docs/PROFILES.md「提示词模板怎么写」)重新生成基线,并在这里说明改了什么。
GOLDEN = ROOT / "tests" / "golden"
GOLDEN_RUN = {"ctf": "/private/tmp/rotom-golden-base", "zhongce": "/private/tmp/rotom-golden-beta"}
# 上游 Rotom 目录(ctf 基线里出现的路径要归一化成 <REPO>):算出来,不写绝对路径
GOLDEN_REPO = {"ctf": str(ROOT.parent.parent / "Rotom"), "zhongce": str(ROOT)}
# 有意改提示词后重新生成基线:python3 tests/profile_test.py --update-golden
# (2026-10-08:zhongce 基线的上一次更新是加入「凭据墙」一节)
UPDATE_GOLDEN = "--update-golden" in sys.argv


def normalize_for(pack: str, run_dir: str, s: str) -> str:
    """把渲染结果里的路径归一化(与基线同一口径),否则逐字比对必然失败。"""
    s = s.replace(GOLDEN_REPO[pack], "<REPO>").replace(str(ROOT), "<REPO>")
    s = s.replace(GOLDEN_RUN[pack], "<RUN>").replace(run_dir, "<RUN>")
    return s.replace("/private/tmp/", "/tmp/")

PROMPT_CODE = r'''
import os, sys, json
from pathlib import Path
sys.path.insert(0, os.environ["PYTHONPATH"])
import solver
q = {"question_id": "web01", "title": "示例题", "category": "web", "score": 1000,
     "description": "一个示例题描述", "attributes": ["web", "sql"], "capabilities": ["注入"],
     "connection": {"url1": "http://127.0.0.1:8080"}, "difficulty": "easy", "level": 1,
     "flag_count": 1, "correct_flag_count": 0}
wdir = solver.project_dir(q) / "w1"; wdir.mkdir(parents=True, exist_ok=True)
sys.stdout.write(solver.build_prompt(q, 1, 2, wdir, attempt=1, worker_timeout=420))
'''

for pack in ("ctf", "zhongce"):
    run_dir = tempfile.mkdtemp(prefix=f"golden-{pack}-")
    r = run_isolated(PROMPT_CODE, pack, run_dir)
    if r.returncode != 0:
        check(f"{pack}: prompt 能组装(rc=0)", False, r.stderr[-300:])
        continue
    golden = (GOLDEN / f"prompt_{pack}.txt").read_text(encoding="utf-8")

    def normalize(s: str) -> str:
        return normalize_for(pack, run_dir, s)

    if UPDATE_GOLDEN:
        dest = GOLDEN / f"prompt_{pack}.txt"
        new = normalize(r.stdout)
        if new != golden:
            dest.write_text(new, encoding="utf-8")
            print(f"  已更新基线: {dest.relative_to(ROOT)}({len(golden)} → {len(new)} 字符)")
        else:
            print(f"  基线无需更新: {dest.relative_to(ROOT)}")
        continue

    check(f"{pack}: 与基线逐字一致({len(r.stdout)} 字符)",
          normalize(golden) == normalize(r.stdout))
    if normalize(golden) != normalize(r.stdout):
        import difflib
        diff = list(difflib.unified_diff(normalize(golden).splitlines(),
                                         normalize(r.stdout).splitlines(), lineterm=""))[:12]
        print("      " + "\n      ".join(diff))

# ------------------------------------------------------------------ 正式渗透 prompt
section("正式渗透 prompt(授权声明 + findings 语义 + --done)")
PT_CODE = r'''
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"])
import solver
q = {"question_id": "web-01", "title": "官网", "category": "pentest",
     "description": "登录口与 API 均可测", "connection": {"target": "https://www.example.com"},
     "findings_count": 2, "delivered": False, "flag_count": 0}
wdir = solver.project_dir(q) / "w1"; wdir.mkdir(parents=True, exist_ok=True)
sys.stdout.write(solver.build_prompt(q, 1, 2, wdir, attempt=3, worker_timeout=900))
'''
run_dir = tempfile.mkdtemp(prefix="golden-pentest-")
r = run_isolated(PT_CODE, "pentest", run_dir)
pt_text = r.stdout
check("pentest prompt 能组装(rc=0)", r.returncode == 0, r.stderr[-300:])
import re as _re
leftover = sorted(set(_re.findall(r"@@[A-Z_]+@@", pt_text)))
check("没有未替换的占位符", not leftover, str(leftover))
check("含授权声明(缓解道德限制的系统提示注入)", "授权声明" in pt_text and "已授权" in pt_text
      and "不要因为" in pt_text)
check("含 finding 记录工具与证据要求", "report_finding.py" in pt_text and "RECORDED" in pt_text
      and "证据" in pt_text)
check("含 --done 收尾语义(DELIVERED)", "--done" in pt_text and "DELIVERED" in pt_text)
check("明确说明没有 flag / 不要调用 submit_flag",
      "没有 flag" in pt_text and "不要调用 submit_flag" in pt_text)
check("进度显示 finding 数(不是 flag)", "已记录 2 条 finding" in pt_text, pt_text[:200])
check("非破坏红线在", "非破坏性" in pt_text and "授权范围" in pt_text)
check("不含提交工具的实际用法(只作为禁止项出现)",
      "submit_flag.py" not in pt_text and "不要调用 submit_flag" in pt_text)
check("注入了正式渗透自己的 playbook(不是众测的找 flag 版)",
      "正式渗透 playbook" in pt_text and "找 flag" not in pt_text.split("本方向打法速览")[1][:200])

# ------------------------------------------------------------------ 任务源与并发门控
section("任务源与策略门控(runner 侧)")
GATE_CODE = r'''
import os, sys, json
sys.path.insert(0, os.environ["PYTHONPATH"])
import runner
out = {"profile": runner.PROF.name, "goal": runner.PROF.goal,
       "adapter": runner.contest_api.__name__,
       "manage": runner.PROF.get("containers.manage"),
       "rotate": runner.PROF.get("containers.rotate"),
       "vpn_gate": runner.PROF.get("containers.vpn_gate"),
       "workers_per_q": runner.WORKERS_PER_QUESTION,
       "max_helpers": runner.MAX_HELPERS_PER_QUESTION,
       "has_sync": hasattr(runner.contest_api, "sync_containers"),
       "env_ok": bool(runner.env_config())}
print(json.dumps(out))
'''
run_dir = tempfile.mkdtemp(prefix="gate-pentest-")
Path(run_dir, "targets.json").write_text(json.dumps(
    {"targets": [{"id": "web-01", "name": "官网", "address": "https://x.example"}]}), encoding="utf-8")
r = run_isolated(GATE_CODE, "pentest", run_dir)
check("pentest:runner 能启动(无 token 也不退出)", r.returncode == 0, r.stderr[-400:])
if r.returncode == 0:
    g = json.loads(r.stdout.strip().splitlines()[-1])
    check("pentest:任务源切到 targets_api(忽略 CONTEST_ADAPTER)",
          g["adapter"] == "targets_api" and g["goal"] == "findings", str(g))
    check("pentest:容器管理/轮换关闭(不碰容器名额)",
          g["manage"] is False and g["rotate"] is False and g["has_sync"] is False, str(g))
    check("pentest:VPN 预检开启(流量不可追溯)", g["vpn_gate"] is True)
    check("pentest:同题 1 agent、增援 2", g["workers_per_q"] == 1 and g["max_helpers"] == 2, str(g))

run_dir = tempfile.mkdtemp(prefix="gate-zhongce-")
r = run_isolated(GATE_CODE, "zhongce", run_dir, extra_env={"BENCHMARK_TOKEN": "mock-token"})
check("zhongce:runner 能启动(有 token)", r.returncode == 0, r.stderr[-400:])
if r.returncode == 0:
    g = json.loads(r.stdout.strip().splitlines()[-1])
    check("zhongce:仍是 tsec 适配器 + 容器自管 + 轮换", g["adapter"] == "tsec_api"
          and g["manage"] is True and g["rotate"] is True and g["has_sync"] is True, str(g))
    check("zhongce:VPN 预检关(用户要求:ctf/众测不预检)", g["vpn_gate"] is False)
    # 2026-09-28 跑分特化:众测改成"单脑"(调度者=解题者) + 按需增援(学满分轨迹的'默认单 agent')
    check("zhongce:同题 1 agent、增援 3(单脑 + 按需增援)",
          g["workers_per_q"] == 1 and g["max_helpers"] == 3, str(g))

# ------------------------------------------------------------------ 正式渗透链路
section("正式渗透链路:targets.json → finding → --done → REPORT.md")
run_dir = tempfile.mkdtemp(prefix="pt-flow-")
Path(run_dir, "targets.json").write_text(json.dumps({
    "engagement": "测试项目",
    "targets": [{"id": "web-01", "name": "官网", "address": "https://www.example.com",
                 "notes": "登录口可测"}]}), encoding="utf-8")

r = run_isolated(
    'import os,sys,json; sys.path.insert(0,os.environ["PYTHONPATH"]);'
    'import board as b, targets_api as t;'
    'qs=t.list_questions("");'
    'print(json.dumps(qs, ensure_ascii=False))', "pentest", run_dir)
check("targets_api 能读清单", r.returncode == 0 and "web-01" in r.stdout, r.stderr[-200:])

# 把目标写进黑板(模拟 runner 已拉取)
seed = ('import os,sys,json; sys.path.insert(0,os.environ["PYTHONPATH"]);'
        'import board as bm, targets_api as ta;'
        'qs=ta.list_questions("");'
        '\nwith bm.locked() as b:\n'
        '    for q in qs: bm.upsert_question(b, q)\n')
r = run_isolated(seed, "pentest", run_dir)
check("目标写进黑板", r.returncode == 0, r.stderr[-200:])

env = {**os.environ, "ROTOM_RUN_DIR": run_dir, "PROFILE": "pentest", "PYTHONPATH": str(ROOT)}
env.pop("TEAM_TOKEN", None)
env.pop("BENCHMARK_TOKEN", None)
p = subprocess.run([sys.executable, str(ROOT / "tools" / "report_finding.py"), "web-01",
                    "--title", "SQL 注入 @ /api/login", "--severity", "high",
                    "--detail", "复现:curl ... ;证据:200 且带出 admin;影响:绕过认证"],
                   capture_output=True, text=True, env=env, cwd=str(ROOT))
check("report_finding 记一条 → RECORDED(rc=0)", p.returncode == 0 and "RECORDED" in p.stdout,
      p.stdout + p.stderr)
md = Path(run_dir, "work", "pentest-web-01", "FINDINGS.md")
check("FINDINGS.md 写出来了", md.exists() and "SQL 注入" in md.read_text(encoding="utf-8"),
      str(md))
board = json.loads(Path(run_dir, "board.json").read_text(encoding="utf-8"))
e = board["questions"]["web-01"]
check("黑板记账 findings_count=1 + 级别留档",
      int(e["findings_count"]) == 1 and e["findings"][0]["severity"] == "high", str(e.get("findings")))
check("未 --done 前不算交付", e["status"] != "solved" and not e.get("delivered"), e["status"])

p2 = subprocess.run([sys.executable, str(ROOT / "tools" / "report_finding.py"), "web-01",
                     "--done", "--summary", "web 面测完;1 高危"],
                    capture_output=True, text=True, env=env, cwd=str(ROOT))
check("--done → DELIVERED 且黑板标 solved", p2.returncode == 0 and "DELIVERED" in p2.stdout
      and json.loads(Path(run_dir, "board.json").read_text(encoding="utf-8"))
      ["questions"]["web-01"]["status"] == "solved", p2.stdout + p2.stderr)

# 已交付的目标不被下一轮刷新翻回未交付(is_solved 粘性)
r = run_isolated('import os,sys; sys.path.insert(0,os.environ["PYTHONPATH"]);'
                 'import board as bm, targets_api as ta;'
                 '\nwith bm.locked() as b:\n'
                 '    for q in ta.list_questions(""): bm.upsert_question(b, q)\n'
                 '    print(b["questions"]["web-01"]["is_solved"])\n', "pentest", run_dir)
check("已交付目标在刷新后仍是 solved(粘性)", r.stdout.strip().endswith("True"), r.stdout[-100:])

p3 = subprocess.run([sys.executable, str(ROOT / "tools" / "pentest_report.py"), "--run-dir", run_dir],
                    capture_output=True, text=True, env=env, cwd=str(ROOT))
rep = Path(run_dir, "REPORT.md")
check("pentest_report 生成 REPORT.md", p3.returncode == 0 and rep.exists(), p3.stdout + p3.stderr)
if rep.exists():
    text = rep.read_text(encoding="utf-8")
    check("报告含 finding 与交付结论", "SQL 注入 @ /api/login" in text and "已交付" in text
          and "HIGH" in text)

# 目标不存在时给出清晰错误(不是崩)
p4 = subprocess.run([sys.executable, str(ROOT / "tools" / "report_finding.py"), "nope",
                     "--title", "x"], capture_output=True, text=True, env=env, cwd=str(ROOT))
check("目标不在黑板 → TARGET_NOT_FOUND(rc=1)", p4.returncode == 1 and "TARGET_NOT_FOUND" in p4.stderr,
      p4.stderr[-200:])

# 清单缺失时 targets_api 抛业务错误(runner 会保持轮询而不是崩)
r = run_isolated('import os,sys; sys.path.insert(0,os.environ["PYTHONPATH"]);'
                 'import targets_api as t\n'
                 'try:\n'
                 '    t.list_questions("")\n'
                 '    print("NO_ERROR")\n'
                 'except t.ContestError as e:\n'
                 '    print("CONTEST_ERROR", str(e)[:40])\n',
                 "pentest", tempfile.mkdtemp(prefix="pt-empty-"))
check("清单缺失 → ContestError(不崩)", "CONTEST_ERROR" in r.stdout, r.stdout + r.stderr[-200:])

print(f"\n{'='*50}\n通过 {PASS} 项,失败 {FAIL} 项")
if FAILED:
    print("失败项:")
    for f in FAILED:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)