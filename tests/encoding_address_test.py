#!/usr/bin/env python3
"""黑板编码 + 容器换地址(离线,不联网不花 token)。

  python3 tests/encoding_address_test.py

背景:
① 编码 —— agent 把 GBK 工具输出直接 `>>` 进 SHARED.md(实测 0xbc 字节),旧代码严格 utf-8 解码直接崩,
   后来 `errors="ignore"` 能跑但**内容静默丢失**。现在要:保住内容 + 输出统一 UTF-8 + 从源头少产 GBK。
② 地址 —— 容器重开 IP/URL 会变,黑板里的旧地址不失效 → agent 反复打死地址(实测同一题攒了 5 个地址)。
   现在要:黑板顶部有 harness 维护的「当前目标地址」块,换地址时旧地址自动进"已失效"名单。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
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


RUN_DIR = Path(tempfile.mkdtemp(prefix="rotom-enc-test-"))
os.environ["ROTOM_RUN_DIR"] = str(RUN_DIR)
os.environ["PROFILE"] = "zhongce"
os.environ["BENCHMARK_TOKEN"] = "mock-token"
(RUN_DIR / "logs").mkdir(parents=True, exist_ok=True)

import board as board_mod  # noqa: E402
import runner  # noqa: E402
import solver  # noqa: E402
import textutil  # noqa: E402

# ------------------------------------------------------------------ 编码:转码读
section("编码:GBK 混进 UTF-8(逐行转码,内容不许丢)")
gbk_line = "中文日志:注入成功".encode("gbk")
mixed = ("# 本题共享黑板(SHARED)\n\n正常的一行 UTF-8 内容\n".encode("utf-8")
         + gbk_line + b"\n" + "又一行 UTF-8\n".encode("utf-8"))
text, transcoded = textutil.read_mixed(_write := (RUN_DIR / "mixed.md"))
(RUN_DIR / "mixed.md").write_bytes(mixed)
text, transcoded = textutil.read_mixed(RUN_DIR / "mixed.md")
check("混合内容被识别为需要转码", transcoded is True)
check("UTF-8 行原样保留", "正常的一行 UTF-8 内容" in text and "又一行 UTF-8" in text)
check("GBK 行按 GB18030 正确还原(内容没丢)", "中文日志:注入成功" in text, repr(text.splitlines()[3]))
check("转码结果是合法 UTF-8", textutil.is_utf8(text.encode("utf-8")))

pure = RUN_DIR / "pure.md"
pure.write_text("全是 UTF-8\n", encoding="utf-8")
_, t2 = textutil.read_mixed(pure)
check("干净 UTF-8 文件不会被误判转码", t2 is False)
check("文件不存在返回空串不抛异常", textutil.read_text_lossless(RUN_DIR / "nope.md") == "")

# ------------------------------------------------------------------ 编码:黑板重写
section("编码:黑板重写后整个文件是合法 UTF-8,且 agent 写的行还在")
shared = RUN_DIR / "work" / "q-enc" / "SHARED.md"
shared.parent.mkdir(parents=True, exist_ok=True)
shared.write_bytes("# 本题共享黑板(SHARED)\n\n## 已确认的事实\n".encode("utf-8")
                   + b"- [w1 12:00] " + "端口是 8080".encode("gbk") + b"\n"
                   + "- [w1 12:01] 已确认是 Go 写的\n".encode("utf-8"))
warned = runner._rebuild_shared(shared, ["- [Observer 12:02] w1: 跑了一次 curl"])
raw = shared.read_bytes()
check("重写后文件是合法 UTF-8(不再是混合编码)", textutil.is_utf8(raw))
first = runner._rebuild_shared(shared, [])
check("写成功时返回 (转码=False, 写入=True)", first == (True, True) or first == (False, True), str(first))
body = raw.decode("utf-8")
check("编码规则:GBK 那行被转码后保留下来", "端口是 8080" in body, body[:200])
check("agent 自己的行没被删", "已确认是 Go 写的" in body)
check("Observer 段照常重建", "- [Observer 12:02] w1: 跑了一次 curl" in body)
check("_rebuild_shared 返回(是否转码, 是否写入成功):修好后不再报转码",
      runner._rebuild_shared(shared, []) == (False, True))          # 已经修好了 → 第二次不再报转码

# ------------------------------------------------------------------ 编码:worker 环境
section("编码:worker 进程注入 UTF-8 环境(从源头少产 GBK)")
class FakeProc:
    pid = 999999

    def __init__(self, *a, **kw):
        self._agent_log_path = RUN_DIR / "logs" / "x.jsonl"
        self._agent_log_f = open(RUN_DIR / "logs" / "x.jsonl", "a", encoding="utf-8")


captured = {}
_real_popen = solver.subprocess.Popen
solver.subprocess.Popen = lambda cmd, **kw: (captured.update(kw), FakeProc())[1]
try:
    q = {"question_id": "q-enc", "title": "编码题", "category": "web", "flag_count": 1}
    (solver.project_dir(q) / "w1").mkdir(parents=True, exist_ok=True)
    solver.launch(q, 1, 1, solver.project_dir(q) / "w1",
                  {"TEAM_TOKEN": "t", "PI_PROVIDER": "p", "PI_MODEL_NAME": "m"}, 60)
finally:
    solver.subprocess.Popen = _real_popen
env = captured.get("env", {})
check("LC_ALL/LANG 设为 UTF-8", env.get("LC_ALL") == "en_US.UTF-8" and env.get("LANG") == "en_US.UTF-8")
check("PYTHONUTF8 / PYTHONIOENCODING 已设", env.get("PYTHONUTF8") == "1"
      and env.get("PYTHONIOENCODING") == "utf-8")
cmd = captured.get("env") is not None
check("prompt 里给了容器地址以黑板顶部为准的说明",
      "以黑板顶部「当前目标地址" in solver.build_prompt(q, 1, 1, solver.project_dir(q) / "w1"))

# ------------------------------------------------------------------ 地址:harness 块
section("地址:黑板顶部 harness 维护块(当前地址 / 已失效地址)")
entry = {"question_id": "q-addr", "title": "地址题", "workdir": "q-addr",
         "status": board_mod.STATUS_RUNNING,
         "container_status": "available", "connection": {"url1": "http://10.0.0.1:80"}}
lines = runner._target_lines(entry)
check("块里有当前地址", any("当前目标地址" in ln and "http://10.0.0.1:80" in ln for ln in lines), str(lines))
check("没有失效地址时不提这一项", not any("已失效" in ln for ln in lines))

# 换地址:旧地址自动进名单
changed = runner._accept_conn(entry, {"url1": "http://10.0.0.2:80"})
check("换地址被识别为变更", changed is True and entry["connection"] == {"url1": "http://10.0.0.2:80"})
check("旧地址进了 dead_addrs", entry["dead_addrs"] == ["http://10.0.0.1:80"], str(entry["dead_addrs"]))
lines = runner._target_lines(entry)
check("块里同时给出新地址与失效地址",
      any("10.0.0.2:80" in ln for ln in lines) and any("已失效" in ln and "10.0.0.1:80" in ln for ln in lines),
      str(lines))
runner._accept_conn(entry, {"url1": "http://10.0.0.2:80"})
check("地址没变时不重复记录", entry["dead_addrs"] == ["http://10.0.0.1:80"])

# 容器没了:当前地址立刻作废
runner._mark_dead(entry, "容器已释放")
check("容器释放后当前地址被清空", entry["connection"] == {})
check("释放前用的地址也进了失效名单", "http://10.0.0.2:80" in entry["dead_addrs"], str(entry["dead_addrs"]))
check("块里显示「暂未就绪」而不是一个死地址",
      any("暂未就绪" in ln for ln in runner._target_lines(entry)))

# ------------------------------------------------------------------ 地址:写进黑板
section("地址:地址变更后黑板里的旧地址被标失效")
board = {"questions": {entry["question_id"]: dict(entry, dead_addrs=["http://10.0.0.1:80"],
                                                 connection={"url1": "http://10.0.0.2:80"})},
         "facts": [], "started_at": time.time()}
(RUN_DIR / "board.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
shared2 = RUN_DIR / "work" / "q-addr" / "SHARED.md"
shared2.parent.mkdir(parents=True, exist_ok=True)
shared2.write_text("# 本题共享黑板(SHARED)\n\n## 已确认的事实\n- [w1] 我先试 10.0.0.1:80\n\n## 正在尝试的方向\n",
                   encoding="utf-8")
runner._target_sig.clear()
runner.sync_target_blocks()
body = shared2.read_text(encoding="utf-8")
check("黑板里写入了 harness 块(harness 前缀)", ">> [harness]" in body)
check("块里给出当前地址", "http://10.0.0.2:80" in body)
check("块里把旧地址标成已失效", "已失效" in body and "http://10.0.0.1:80" in body)
check("块插在「已确认的事实」之前(agent 一眼看到)", body.index(">> [harness]") < body.index("## 已确认的事实"))
check("agent 原有的行还在", "我先试 10.0.0.1:80" in body)
# 每轮都会调 sync_target_blocks:内容没变就不该重复写(签名机制)
first = shared2.stat().st_mtime_ns
time.sleep(0.01)
runner.sync_target_blocks()
check("目标块内容没变时不重写文件(避免每轮都动黑板)", shared2.stat().st_mtime_ns == first)
# 地址再变 → 会重写
with board_mod.locked() as b:
    e = b["questions"]["q-addr"]
    runner._accept_conn(e, {"url1": "http://10.0.0.3:80"})
runner.sync_target_blocks()
body = shared2.read_text(encoding="utf-8")
check("再次换地址后黑板同步成最新地址", "http://10.0.0.3:80" in body and "当前目标地址" in body)

# ------------------------------------------------------------------ 地址:工作区未建好时要重试
section("地址:题还没派过(工作区不存在)时不把写失败当成已写入")
e2 = {"question_id": "q-late", "title": "后来的题", "workdir": "q-late",
      "status": board_mod.STATUS_PENDING, "container_status": "available",
      "connection": {"url1": "http://10.0.9.9:80"}, "dead_addrs": []}
with board_mod.locked() as b:
    b["questions"]["q-late"] = dict(e2)
runner._target_sig.pop("q-late", None)
runner.sync_target_blocks()
late = RUN_DIR / "work" / "q-late" / "SHARED.md"
check("工作区不存在时不写文件、也不记签名(等下一轮)",
      not late.exists() and "q-late" not in runner._target_sig)
late.parent.mkdir(parents=True, exist_ok=True)
runner.sync_target_blocks()
check("工作区建好后立刻补写目标块",
      late.exists() and "http://10.0.9.9:80" in late.read_text(encoding="utf-8"))

print(f"\n{'='*50}\n通过 {PASS} 项,失败 {FAIL} 项")
if FAILED:
    print("失败项:")
    for f in FAILED:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)